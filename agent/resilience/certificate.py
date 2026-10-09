"""Quorum certificates: the proof that 3 of 4 agents agreed to an action.

WHAT A VALID CERTIFICATE IS (exactly)
-------------------------------------
A certificate is one *statement* plus signatures over that statement:

    statement = { version, workload, target, action, stage, epoch,
                  evidence_digest, expires_ms }

  workload         the Kubernetes workload it is about (e.g. "records-api")
  target           the resilience node id of that workload ("C")
  action           CONTAIN | VALIDATE | ADVANCE_STAGE | RETRY_RECOVERY
  stage            the stage being entered (ADVANCE_STAGE) or QUARANTINE (CONTAIN) or "" (VALIDATE)
  epoch            the incident version of the target
  evidence_digest  SHA-256 of the sorted, de-duplicated evidence ids cited by the three votes
  expires_ms       after this instant the certificate is void

It is valid only if ALL of these hold (see verify_certificate):
  1. it carries at least 3 signatures, from 3 DISTINCT peers, none listed twice;
  2. every signer is a known peer (its public key is in the registry) and not revoked;
  3. every signature is a valid Ed25519 signature over the SAME canonical statement bytes;
  4. it has not expired;
  5. it matches what the caller expects (same workload, action, stage, incident version);
  6. it is not for a superseded incident version, and has not been consumed already (replay).

Any one failure rejects it. The canonical bytes are JSON with sorted keys and no spaces,
so every agent (and the admission webhook) signs and checks byte-identical data.

HOW IT IS BUILT (leaderless, no coordinator)
--------------------------------------------
The statement is derived deterministically from the three lowest-id eligible votes of the
quorum, so every agent that holds those votes computes the identical statement. Each agent
that voted for the action signs it and broadcasts a CertShare; any agent (or the webhook)
that holds three distinct valid shares holds the certificate.

ASSUMPTIONS (stated honestly)
-----------------------------
* It tolerates ONE compromised agent (f = 1 of n = 4): a lone agent cannot produce a valid
  certificate, because it has one signature, not three.
* TWO compromised agents can break it: two signatures plus one honest, deceived or coerced
  signature is a valid certificate. 2 of 4 is beyond the BFT bound; this is expected.
* It proves the agents *agreed*; it does not prove they were *right*.
* Signing keys are only as safe as the Kubernetes Secrets that hold them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .crypto import KeyRegistry
from .evidence import Vote
from .proto import resilience_pb2 as pb

log = logging.getLogger(__name__)

QC_KIND = "qc-v1"                       # signature domain (cannot be replayed as evidence / vote)
ANNOTATION = "resilience.io/qc"         # where the executor attaches the certificate
ACTIONS = ("CONTAIN", "VALIDATE", "ADVANCE_STAGE", "RETRY_RECOVERY")
_FIELDS = ("action", "epoch", "evidence_digest", "expires_ms", "stage", "target", "version", "workload")


class CertificateError(Exception):
    pass


class ActionPending(CertificateError):
    """A prerequisite is not met yet (certificate still being assembled, isolation not confirmed):
    the executor retries on the next tick, quietly."""


class CertificatePending(ActionPending):
    """The certificate for this decision is not complete yet (shares still arriving)."""


class CertificateInvalid(CertificateError):
    """A certificate exists but fails verification: the action must NOT be performed."""


# --------------------------------------------------------------------------- statement
@dataclass(frozen=True)
class Statement:
    workload: str
    target: str
    action: str
    stage: str
    epoch: int
    evidence_digest: str
    expires_ms: int
    version: int = 1

    def to_dict(self) -> dict:
        return {"action": self.action, "epoch": int(self.epoch), "evidence_digest": self.evidence_digest,
                "expires_ms": int(self.expires_ms), "stage": self.stage, "target": self.target,
                "version": int(self.version), "workload": self.workload}

    def canonical(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    @property
    def action_key(self) -> str:
        return f"{self.action}:{self.target}:{self.epoch}:{self.stage}"

    @property
    def expires_at(self) -> float:
        return self.expires_ms / 1000.0

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Statement":
        try:
            d = json.loads(raw.decode())
            if not isinstance(d, dict) or set(d) != set(_FIELDS):
                raise ValueError("unexpected fields")
            st = cls(workload=str(d["workload"]), target=str(d["target"]), action=str(d["action"]),
                     stage=str(d["stage"]), epoch=int(d["epoch"]), evidence_digest=str(d["evidence_digest"]),
                     expires_ms=int(d["expires_ms"]), version=int(d["version"]))
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise CertificateError(f"malformed statement: {exc}") from exc
        if st.canonical() != raw:                 # forbid any non-canonical encoding of the same data
            raise CertificateError("statement is not in canonical form")
        if st.version != 1 or st.action not in ACTIONS:
            raise CertificateError("unsupported statement version or action")
        return st


def evidence_digest(evidence_ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(evidence_ids))).encode()).hexdigest()


def build_statement(votes: Sequence[Vote], workload: str, ttl_s: float,
                    quorum: int = 3) -> Tuple[Statement, Tuple[str, ...]]:
    """Deterministic statement from a quorum's votes: the `quorum` lowest-id voters."""
    chosen = sorted(votes, key=lambda v: v.voter)[:quorum]
    if len(chosen) < quorum:
        raise CertificateError(f"need {quorum} votes, have {len(chosen)}")
    keys = {v.action_key for v in chosen}
    if len(keys) != 1:
        raise CertificateError("votes are for different proposals")
    v0 = chosen[0]
    ids = [i for v in chosen for i in v.evidence_ids]
    expires_ms = int((max(v.timestamp for v in chosen) + ttl_s) * 1000)
    st = Statement(workload=workload, target=v0.target, action=v0.action.value, stage=v0.stage,
                   epoch=int(v0.epoch), evidence_digest=evidence_digest(ids), expires_ms=expires_ms)
    return st, tuple(v.voter for v in chosen)


# --------------------------------------------------------------------------- certificate
@dataclass
class Certificate:
    statement_bytes: bytes
    signatures: List[Tuple[str, bytes]]

    @property
    def statement(self) -> Statement:
        return Statement.from_bytes(self.statement_bytes)

    def signers(self) -> List[str]:
        return [s for s, _ in self.signatures]

    def to_b64(self) -> str:
        doc = {"s": base64.b64encode(self.statement_bytes).decode(),
               "sigs": [[s, base64.b64encode(sig).decode()] for s, sig in self.signatures]}
        return base64.urlsafe_b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()

    @classmethod
    def from_b64(cls, text: str) -> "Certificate":
        try:
            doc = json.loads(base64.urlsafe_b64decode(text.encode()))
            return cls(base64.b64decode(doc["s"]),
                       [(str(s), base64.b64decode(sig)) for s, sig in doc["sigs"]])
        except Exception as exc:
            raise CertificateError(f"malformed certificate: {exc}") from exc


@dataclass(frozen=True)
class Expect:
    """What the caller requires the certificate to be about. None = do not check that field."""
    workload: Optional[str] = None
    action: Optional[str] = None
    stage: Optional[str] = None
    epoch: Optional[int] = None


def verify_certificate(cert: Certificate, registry: KeyRegistry, now: float, *, quorum: int = 3,
                       expect: Union[Expect, Sequence[Expect], None] = None,
                       revoked: Iterable[str] = (), consumed: Optional[Set[str]] = None,
                       current_epoch: Optional[int] = None, max_ttl_s: Optional[float] = None
                       ) -> Tuple[bool, str]:
    """(True, "ok") or (False, reason). Strict: a certificate carrying a duplicate, unknown or
    invalidly signed signature is rejected outright, not 'repaired'."""
    try:
        st = cert.statement
    except CertificateError as exc:
        return False, str(exc)
    sigs = cert.signatures
    if not sigs:
        return False, "no signatures"
    names = [s for s, _ in sigs]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        return False, f"duplicate signer(s): {', '.join(dup)}"
    revoked = set(revoked)
    counted = 0
    for signer, sig in sigs:
        if not registry.knows(signer):
            return False, f"unknown peer {signer!r}"
        if not registry.verify(signer, QC_KIND, cert.statement_bytes, sig):
            return False, f"invalid signature from {signer}"
        if signer not in revoked:
            counted += 1
    if counted < quorum:
        return False, (f"only {counted} valid signature(s) from distinct non-revoked peers; "
                       f"need {quorum}")
    if now * 1000 > st.expires_ms:
        return False, f"expired {now - st.expires_at:.0f}s ago"
    if max_ttl_s is not None and st.expires_at > now + max_ttl_s:
        return False, "implausible expiry (too far in the future)"
    if expect is not None:
        options = [expect] if isinstance(expect, Expect) else list(expect)
        why = []
        for e in options:
            if e.workload is not None and e.workload != st.workload:
                why.append(f"certificate is for workload {st.workload!r}, not {e.workload!r}")
            elif e.action is not None and e.action != st.action:
                why.append(f"certificate authorises {st.action}, not {e.action}")
            elif e.stage is not None and e.stage != st.stage:
                why.append(f"certificate is for stage {st.stage!r}, not {e.stage!r}")
            elif e.epoch is not None and e.epoch != st.epoch:
                why.append(f"certificate is for incident version {st.epoch}, not {e.epoch}")
            else:
                why = []
                break
        if why:
            return False, why[0]
    if current_epoch is not None and st.epoch < current_epoch:
        return False, f"superseded: certificate is for incident version {st.epoch}, current is {current_epoch}"
    if consumed is not None and st.digest() in consumed:
        return False, "replayed: this certificate was already used"
    return True, "ok"


# --------------------------------------------------------------------------- manager
class CertificateManager:
    """Per-agent: builds the statement after a commit, signs it, collects peers' signatures."""

    RESEND_S = 2.0

    def __init__(self, agent):
        self.a = agent
        self.lock = threading.RLock()
        self.statements: Dict[str, Tuple[Statement, Tuple[str, ...]]] = {}
        self.shares: Dict[str, Dict[str, bytes]] = defaultdict(dict)
        self.signed: Set[str] = set()
        self.last_sent: Dict[str, float] = {}
        self.by_key: Dict[str, List[str]] = defaultdict(list)
        self.complete: Dict[str, Certificate] = {}
        self.refused: Deque[dict] = deque(maxlen=30)
        self.cv = threading.Condition(self.lock)

    @property
    def p(self):
        return self.a.cfg.certificates

    # ---- producing
    def on_commit(self, commit, now: float) -> None:
        """Our local VoteBook reached a quorum: build the statement and sign it."""
        a = self.a
        workload = a.cfg.workload_of(commit.sample.target)
        try:
            st, voters = build_statement(commit.votes, workload, self.p.ttl_s, a.cfg.quorum.quorum)
        except CertificateError as exc:
            log.warning("cannot build certificate statement: %s", exc)
            return
        with self.lock:
            self._remember(st, voters)
            self._cosign(st.digest(), now)

    def _remember(self, st: Statement, voters: Tuple[str, ...]) -> None:
        d = st.digest()
        if d not in self.statements:
            self.statements[d] = (st, voters)
            self.by_key[st.action_key].append(d)

    def _cosign(self, d: str, now: float) -> bool:
        """Sign statement `d` if (and only if) this agent voted for the same proposal, holds the
        named votes, finds every voter eligible, and recomputes the identical statement."""
        if d in self.signed:
            return True
        a = self.a
        st, voters = self.statements[d]
        if not a.votes.has_voted(a.id, st.action_key):
            return False
        held = a.votes.votes_of(st.action_key)
        if any(v not in held for v in voters):
            return False                                  # a named vote has not reached us yet
        if any(not a.eligible_voter(v) for v in voters):
            self._refuse(st, "a named voter is vote-excluded in this agent's view")
            return False
        try:
            mine, _ = build_statement([held[v] for v in voters], st.workload, self.p.ttl_s,
                                      a.cfg.quorum.quorum)
        except CertificateError:
            return False
        if mine.canonical() != st.canonical():
            self._refuse(st, "statement does not match the votes this agent holds")
            return False
        cur = a.states.get(st.target)
        if cur is None or st.epoch < cur.epoch:
            self._refuse(st, "superseded incident version")
            return False
        sig = a.signer.sign(QC_KIND, st.canonical())
        self.signed.add(d)
        self.shares[d][a.id] = sig
        self._broadcast(st, voters, sig, now)
        self._check_complete(d)
        return True

    def _broadcast(self, st: Statement, voters, sig: bytes, now: float) -> None:
        self.last_sent[st.digest()] = now
        tr = self.a.transport
        if tr is None:
            return
        msg = pb.CertShare(share_id=uuid.uuid4().hex, signer=self.a.id, statement=st.canonical(),
                           statement_signature=sig, voters=list(voters), timestamp=now)
        tr.broadcast(pb.SignedEnvelope(signer=self.a.id, kind="cert_share", payload=msg.SerializeToString(),
                                       signature=self.a.signer.sign("cert_share", msg.SerializeToString())))

    # ---- receiving
    def on_share(self, msg: pb.CertShare, identity: Optional[str], now: float) -> Tuple[bool, str, bool]:
        """(accepted, reason, penalise_sender)."""
        a = self.a
        if msg.signer != identity and identity is not None:
            return False, f"share signer {msg.signer!r} != mTLS identity {identity!r}", True
        try:
            st = Statement.from_bytes(bytes(msg.statement))
        except CertificateError as exc:
            return False, str(exc), True
        if not a.registry.verify(msg.signer, QC_KIND, bytes(msg.statement), bytes(msg.statement_signature)):
            return False, "invalid statement signature", True
        if st.target not in a.cfg.nodes or st.workload != a.cfg.workload_of(st.target):
            return False, "statement names a workload that does not match its target", True
        if now * 1000 > st.expires_ms:
            return False, "statement already expired", False
        if st.expires_at > now + self.p.max_ttl_s:
            return False, "implausible expiry", True
        with self.lock:
            self._remember(st, tuple(msg.voters))
            d = st.digest()
            self.shares[d][msg.signer] = bytes(msg.statement_signature)
            self._cosign(d, now)
            self._check_complete(d)
        return True, "ok", False

    def _refuse(self, st: Statement, why: str) -> None:
        self.refused.append({"t": self.a.clock(), "key": st.action_key, "why": why})
        self.a.events.emit("QUORUM", f"Agent {self.a.id} did not sign a certificate for "
                           f"{st.action} {st.target} (epoch {st.epoch}): {why}.", st.target,
                           {"certificate": st.digest()[:16], "reason": why},
                           key=("certrefuse", st.digest(), why), every=10.0)

    def _check_complete(self, d: str) -> None:
        st, _ = self.statements[d]
        good = [(s, sig) for s, sig in self.shares[d].items()
                if self.a.registry.knows(s) and s not in self.p.revoked_signers
                and self.a.eligible_voter(s)]
        if len(good) >= self.a.cfg.quorum.quorum and st.action_key not in self.complete:
            cert = Certificate(st.canonical(), sorted(good)[:max(self.a.cfg.quorum.quorum, len(good))])
            ok, why = verify_certificate(cert, self.a.registry, self.a.clock(), quorum=self.a.cfg.quorum.quorum,
                                         revoked=self.p.revoked_signers)
            if ok:
                self.complete[st.action_key] = cert
                self.cv.notify_all()
                self.a.events.emit("QUORUM", f"Agent {self.a.id} holds a quorum certificate for "
                                   f"{st.action} {st.target} (epoch {st.epoch}"
                                   f"{', ' + st.stage if st.stage else ''}) signed by "
                                   f"{', '.join(c for c, _ in cert.signatures)}.", st.target,
                                   {"certificate": d[:16], "signers": [c for c, _ in cert.signatures],
                                    "expires_in_s": round(st.expires_at - self.a.clock(), 1)})

    # ---- using
    def get(self, action_key: str, wait_s: float = 0.0) -> Optional[Certificate]:
        """The complete certificate for this proposal; waits up to wait_s for the shares."""
        with self.cv:
            if action_key not in self.complete and wait_s > 0:
                self.cv.wait_for(lambda: action_key in self.complete, wait_s)
            return self.complete.get(action_key)

    def tick(self, now: float) -> None:
        """Retry statements waiting for votes; re-send our share until the certificate completes."""
        with self.lock:
            for d, (st, voters) in list(self.statements.items()):
                if now * 1000 > st.expires_ms + 60_000:
                    self.statements.pop(d, None)
                    self.shares.pop(d, None)
                    self.signed.discard(d)
                    self.last_sent.pop(d, None)
                    if d in self.by_key.get(st.action_key, []):
                        self.by_key[st.action_key].remove(d)
                    if self.complete.get(st.action_key) and self.complete[st.action_key].statement_bytes == st.canonical():
                        self.complete.pop(st.action_key, None)
                    continue
                if d not in self.signed:
                    self._cosign(d, now)
                elif st.action_key not in self.complete and now - self.last_sent.get(d, 0) > self.RESEND_S:
                    self._broadcast(st, voters, self.shares[d][self.a.id], now)

    def status(self) -> dict:
        with self.lock:
            return {"complete": len(self.complete), "tracking": len(self.statements),
                    "refused": list(self.refused)[-5:], "enforced_in_executor": bool(self.p.enforce)}
