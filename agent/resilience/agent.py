"""The Resilience Agent: one per resilience node, no leader.

Per tick (default 1 s) every agent:
  1. polls all four workloads           (monitoring.py)
  2. runs threshold detection           (detection.py)
  3. signs + broadcasts evidence        (evidence.py / peer.py)
  4. updates its trust views            (trust.py)
  5. computes W(T) and casts signed votes when its rules are met (quorum.py)
  6. applies committed decisions to its replicated per-target state machine
  7. performs executor duties with ranked fail-over (response.py)

Per-target state machine (replicated on every agent, driven only by quorum
certificates):

  HEALTHY --CONTAIN QC--> ISOLATED --policy seen--> RECOVERING --pod ready-->
  VALIDATING --VALIDATE QC--> REINTEGRATING(stage) --ADVANCE QC ...--> HEALTHY
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .config import ClusterConfig
from .crypto import KeyRegistry, Signer
from .decisionlog import DecisionLog, digest_of
from .detection import Detection, Detector, max_confidence
from .evidence import Action, Evidence, ObsType, ReplayCache, Vote, open_envelope, seal
from .certificate import (ANNOTATION as QC_ANNOTATION, ActionPending, CertificateInvalid,
                          CertificateManager, CertificatePending, Expect, verify_certificate)
from .investigation import InvestigationManager
from .metrics import MetricsRecorder
from .monitoring import Snapshot, TelemetrySource, fetch_availability
from .proto import resilience_pb2 as pb
from .quorum import Commit, EvidencePool, ScoreBreakdown, VoteBook, should_vote_contain, weighted_score
from .reintegration import (STAGES, STAGE_SENSITIVITY, STAGE_THRESHOLDS, advance_block_reason,
                             can_advance, next_stage)
from . import observability as obs
from .response import ResponseBackend
from .simhooks import Compromise, CompromiseSource
from .trust import TrustLedger

log = logging.getLogger(__name__)

HEALTHY, ISOLATED, RECOVERING, VALIDATING, REINTEGRATING = (
    "HEALTHY", "ISOLATED", "RECOVERING", "VALIDATING", "REINTEGRATING")
REVOTE_EVERY_S = 4.0
CLEAN_CONF = 0.2          # an observation below this counts as "target looks clean"


@dataclass
class TargetState:
    phase: str = HEALTHY
    epoch: int = 0
    stage: str = "FULL"
    phase_entered: float = 0.0
    stage_entered: float = 0.0
    contaminated_instance: str = ""
    last_decision: Optional[dict] = None


@dataclass
class _Task:
    due: float
    key: str
    target: str
    epoch: int
    effect_present: Callable[[], bool]
    run: Callable[[], None]
    desc: str


class ResilienceAgent:
    def __init__(self, cfg: ClusterConfig, node_id: str, signer: Signer,
                 registry: KeyRegistry, telemetry: TelemetrySource,
                 backend: ResponseBackend, metrics: Optional[MetricsRecorder] = None,
                 compromise: Optional[CompromiseSource] = None,
                 clock: Callable[[], float] = time.time,
                 decision_log_path: Optional[str] = None, trust_state_path: Optional[str] = None):
        self.cfg = cfg
        self.id = node_id
        self.signer = signer
        self.registry = registry
        self.telemetry = telemetry
        self.backend = backend
        self.metrics = metrics or MetricsRecorder(node_id)
        self.compromise = compromise or CompromiseSource(path=None)
        self.clock = clock
        self.events = obs.EventLog(node_id, clock)  # observability only
        self._transport = None                     # PeerClient, attached later

        q = cfg.quorum
        self.detector = Detector(cfg.baseline_hashes, cfg.process_allowlist)
        self.pool = EvidencePool()
        self.votes = VoteBook(q.quorum)
        self.replay = ReplayCache()
        self.agent_trust = TrustLedger(cfg.nodes)
        self.workload_trust = TrustLedger(cfg.nodes)
        self.states: Dict[str, TargetState] = {n: TargetState() for n in cfg.nodes}
        self.snaps: Dict[str, Snapshot] = {}
        self.latest: Dict[str, List[Detection]] = {n: [] for n in cfg.nodes}
        self.local_hist: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=300))
        self.scores: Dict[str, ScoreBreakdown] = {}
        self.decisions: Deque[dict] = deque(maxlen=50)
        self.rejections: Deque[dict] = deque(maxlen=50)
        self.tasks: List[_Task] = []
        self.my_votes: Dict[str, float] = {}
        self.lock = threading.RLock()
        self._last_tick: Optional[float] = None
        self._comp = Compromise()
        # Observability-only bookkeeping (never read by decisions).
        self.vote_reasons: Dict[str, dict] = {}
        self._flags: Dict[str, Tuple[bool, bool]] = {n: (False, False) for n in cfg.nodes}
        self._score_seen: Dict[str, bool] = {}
        self.inv = InvestigationManager(self)       # targeted investigation (docs/INVESTIGATION.md)
        self.certs = CertificateManager(self)       # quorum certificates (docs/SECURITY.md)
        # Evidence from vote-excluded agents is NOT counted, but still watched: it is how
        # peers keep judging whether such an agent is still lying (trust moves only through
        # what peers observe, never through what the agent says about itself).
        self.excluded_pool = EvidencePool()
        self.decision_log = DecisionLog(decision_log_path)       # tamper-evident (docs/SECURITY.md)
        self._trust_path = trust_state_path
        self._trust_saved: Optional[str] = None
        self._trust_saved_at = 0.0
        self._load_trust()

    # ------------------------------------------------------------------ transport
    @property
    def transport(self):
        return self._transport

    @transport.setter
    def transport(self, t) -> None:
        self._transport = t
        if t is not None and hasattr(t, "link_listener"):
            t.link_listener = self._on_peer_link

    def _on_peer_link(self, peer: str, up: bool, detail: str) -> None:
        if up:
            self.events.emit(obs.PEER_LINK, f"Agent {self.id} can reach agent {peer} again.",
                             peer, {"peer": peer, "state": "up"})
        else:
            self.events.emit(obs.PEER_LINK, f"Agent {self.id} lost its link to agent {peer}; "
                             f"messages to {peer} are not being delivered.",
                             peer, {"peer": peer, "state": "down", "error": detail})

    # ------------------------------------------------------------------ helpers
    def workload(self, nid: str) -> str:
        return self.cfg.workload_of(nid)

    def trust_of(self, nid: str) -> float:
        return 100.0 if nid == self.id else self.agent_trust.get(nid)

    def eligible_voter(self, nid: str) -> bool:
        return nid == self.id or self.agent_trust.get(nid) >= self.cfg.trust.vote_min_trust

    def local_max(self, target: str) -> float:
        return max_confidence(self.latest.get(target, []))

    def local_max_since(self, target: str, t0: float) -> float:
        return max((c for t, c in self.local_hist[target] if t >= t0), default=0.0)

    def resync_from_cluster(self) -> None:
        """Recover replicated state after an agent restart (annotations written
        by executors on the workload Deployments)."""
        for nid in self.cfg.nodes:
            try:
                s = self.backend.read_state(self.workload(nid))
            except Exception:
                continue
            if not s:
                continue
            st = self.states[nid]
            st.epoch = int(s.get("epoch", 0))
            st.phase = s.get("phase", HEALTHY)
            st.stage = s.get("stage", "FULL")
            st.phase_entered = st.stage_entered = self.clock()
            if st.phase != HEALTHY:
                self.workload_trust.set(nid, STAGE_THRESHOLDS.get(st.stage, 0.0))

    # ------------------------------------------------------------------ inbound
    def on_investigate(self, env: pb.SignedEnvelope, transport_identity: Optional[str]):
        """gRPC Investigate: a peer asks for fresh signed observations of one target."""
        return self.inv.on_request(env, transport_identity)

    def _reject_envelope(self, env, culprit: Optional[str], reason: str, now: float) -> None:
        old = self.agent_trust.get(culprit) if culprit else None
        if culprit and culprit != self.id:
            self.agent_trust.penalize(culprit, self.cfg.trust.invalid_message_penalty)
        self.rejections.append({"t": now, "from": culprit, "claimed_signer": env.signer,
                                "kind": env.kind, "reason": reason})
        log.warning("REJECTED %s from %s (claimed %s): %s", env.kind, culprit, env.signer, reason)
        self._log_rejection(env, culprit, reason, old)

    def on_envelope(self, env: pb.SignedEnvelope, transport_identity: Optional[str]) -> Tuple[bool, str]:
        now = self.clock()
        claim, reason = open_envelope(env, self.registry, transport_identity=transport_identity,
                                      now=now, max_skew_s=self.cfg.timers.max_clock_skew_s,
                                      replay=self.replay)
        if claim is None:
            self._reject_envelope(env, transport_identity, reason, now)
            return False, reason
        if isinstance(claim, pb.CertShare):
            ok, why, penalise = self.certs.on_share(claim, transport_identity, now)
            if not ok and penalise:
                self._reject_envelope(env, transport_identity, why, now)
            return ok, why
        if not isinstance(claim, (Evidence, Vote)):         # investigation kinds have their own RPC
            return False, f"unexpected {env.kind} on this channel"
        if isinstance(claim, Evidence):
            if claim.origin != self.id and not self.eligible_voter(claim.origin):
                # A vote-excluded agent's evidence is rejected entirely (not merely down-weighted).
                self.excluded_pool.add(claim)
                self.events.emit(
                    obs.FLAG,
                    f"Agent {self.id} ignored {claim.observation.value.lower()} evidence from {claim.origin}: "
                    f"{claim.origin} is vote-excluded (trust {self.agent_trust.get(claim.origin):.0f} < "
                    f"{self.cfg.trust.vote_min_trust:g}), so nothing it says counts toward any score or quorum.",
                    claim.origin, {"peer": claim.origin, "flag": "EVIDENCE_REJECTED", "about": claim.target,
                                   "reason": "sender is vote-excluded", "type": claim.observation.value},
                    key=("excl-ev", claim.origin, claim.target), every=10.0)
                return False, "sender is vote-excluded: evidence not counted"
            self.pool.add(claim)
            self.events.emit(
                obs.EVIDENCE_RECEIVED,
                f"Agent {self.id} accepted {claim.observation.value.lower()} evidence from {claim.origin} "
                f"about {claim.target} (confidence {claim.confidence:.2f}); signature valid and "
                f"sender identity matches its mTLS certificate.",
                claim.target,
                {"sender": claim.origin, "type": claim.observation.value,
                 "confidence": claim.confidence, "evidence_id": claim.evidence_id,
                 "signature": "valid", "mtls_identity": transport_identity or "n/a",
                 "mtls_match": transport_identity is None or transport_identity == env.signer,
                 "claim_summary": claim.summary},
                key=("recv", claim.origin, claim.target, claim.observation.value))
        else:
            self.votes.add(claim)
        return True, "ok"

    def _log_rejection(self, env, culprit: Optional[str], reason: str, old: Optional[float]) -> None:
        self.events.emit(
            obs.REJECTED,
            f"Agent {self.id} rejected {'an evidence message' if env.kind == 'evidence' else 'a vote'} "
            f"that claimed to come from {env.signer} "
            f"(delivered by {culprit or 'an unauthenticated peer'}): {reason}.",
            None,
            {"sender_mtls": culprit, "claimed_signer": env.signer, "kind": env.kind,
             "reason": reason},
            key=("rej", culprit, env.signer, reason))
        if culprit and culprit != self.id and old is not None:
            new = self.agent_trust.get(culprit)
            self.events.emit(
                obs.TRUST_CHANGE,
                (f"Agent {self.id} lowered its trust in {culprit} from {old:.0f} to {new:.0f} "
                 if new < old else f"Agent {self.id} keeps its trust in {culprit} at {new:.0f} ")
                + f"because {culprit} sent an invalid or forged message.",
                culprit, {"peer": culprit, "old": round(old, 1), "new": round(new, 1),
                          "reason": "forged/invalid message", "detail": reason,
                          "penalty": self.cfg.trust.invalid_message_penalty},
                force=True)

    # ------------------------------------------------------------------ outbound
    def _emit(self, claim, claimed_signer: Optional[str] = None) -> None:
        env = seal(self.signer, claim, claimed_signer=claimed_signer)
        if claimed_signer is None:  # our own genuine claims go straight into our pools
            if isinstance(claim, Evidence):
                self.pool.add(claim)
            else:
                self.votes.add(claim)
        if self.transport is not None:
            self.transport.broadcast(env)
        if isinstance(claim, Evidence):
            forged = claimed_signer is not None
            self.events.emit(
                obs.EVIDENCE_SENT,
                (f"[simulated compromise] Agent {self.id} sent evidence pretending to be "
                 f"{claimed_signer} about {claim.target}." if forged else
                 f"Agent {self.id} signed and sent {claim.observation.value.lower()} evidence about "
                 f"{claim.target} to its peers (confidence {claim.confidence:.2f}): {claim.summary}."),
                claim.target,
                {"type": claim.observation.value, "confidence": claim.confidence,
                 "evidence_id": claim.evidence_id, "summary": claim.summary,
                 "impersonating": claimed_signer, "fabricated": claim.summary.startswith("[")},
                key=("sent", claim.target, claim.observation.value, forged))

    def _cast(self, now: float, target: str, action: Action, epoch: int, stage: str = "",
              score: float = 0.0, evidence_ids=(), why: str = "", investigation_id: str = "") -> None:
        key = f"{action.value}:{target}:{epoch}:{stage}"
        if self.votes.is_committed(key):
            return
        if now - self.my_votes.get(key, -1e9) < REVOTE_EVERY_S:
            return
        resend = key in self.my_votes
        self.my_votes[key] = now
        self._emit(Vote(voter=self.id, target=target, action=action, epoch=epoch, stage=stage,
                        score=score, timestamp=now, evidence_ids=tuple(evidence_ids)[:20],
                        investigation_id=investigation_id))
        verb = {"CONTAIN": f"contain {target}", "VALIDATE": f"accept {target} as validated",
                "ADVANCE_STAGE": f"move {target} to {stage}"}[action.value]
        self.events.emit(
            obs.VOTE_CAST,
            f"Agent {self.id} {'re-sent its' if resend else 'cast a'} signed vote to {verb} "
            f"(epoch {epoch}){': ' + why if why else ''}.",
            target, {"proposal": key, "action": action.value, "epoch": epoch, "stage": stage,
                     "score": round(score, 3), "reason": why, "resend": resend})

    # ------------------------------------------------------------------ main loop
    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            dt = 0.0 if self._last_tick is None else max(0.0, now - self._last_tick)
            self._last_tick = now
            self._comp = self.compromise.load()

            try:
                self.metrics.set_marker(self.backend.read_marker())
            except Exception as exc:
                log.debug("marker read failed: %s", exc)

            self._observe(now)
            if self._comp.active:
                self._misbehave(now, self._comp)
            self._update_trust(now, dt)
            self._vote(now)
            for c in self.votes.new_commits(self.eligible_voter):
                self._on_commit(c, now)
            self._track_cluster(now)
            self._run_tasks(now)
            self.inv.tick(now)
            self.certs.tick(now)
            self._save_trust(now)

    def _observe(self, now: float) -> None:
        self.snaps = self.telemetry.collect()
        sens = {t: STAGE_SENSITIVITY.get(st.stage, 1.0)
                for t, st in self.states.items() if st.phase == REINTEGRATING}
        for t, v in self.inv.sensitivity_overrides().items():     # heightened monitoring (watch)
            sens[t] = min(sens.get(t, 1.0), v)
        dets = self.detector.analyze(self.snaps, now, sens)
        q = self.cfg.quorum
        for target, ds in dets.items():
            self.latest[target] = ds
            m = max_confidence(ds)
            self.local_hist[target].append((now, m))
            if m >= q.local_min_conf:
                self.metrics.event("first_detection", now, target,
                                   types=[d.observation.value for d in ds])
            self._log_observation(target, ds, m)
            if self.states[target].phase not in (HEALTHY, REINTEGRATING):
                continue
            for d in ds:
                if d.confidence >= q.evidence_min_conf:
                    self._emit(Evidence(origin=self.id, target=target, observation=d.observation,
                                        confidence=d.confidence, timestamp=now, summary=d.summary))

    def _log_observation(self, target: str, ds: List[Detection], m: float) -> None:
        meas = self.detector.measurements.get(target, {})
        fp = (meas.get("reachable"), meas.get("healthy"),
              tuple(sorted((d.observation.value, round(d.confidence, 1)) for d in ds)))
        self.events.emit(
            obs.OBSERVE,
            obs.describe_observation(self.id, target, self.workload(target), meas, m),
            target, {"confidence": m, "measurements": meas,
                     "detections": [{"type": d.observation.value, "confidence": d.confidence,
                                     "summary": d.summary} for d in ds]},
            key=("observe", target), fingerprint=fp)

    def _misbehave(self, now: float, c: Compromise) -> None:
        """Compromised-resilience-node simulation (see simhooks.py)."""
        st = self.states.get(c.target)
        if st is None:
            return
        for t in c.types:
            try:
                obs = ObsType(t)
            except ValueError:
                continue
            self._emit(Evidence(origin=self.id, target=c.target, observation=obs,
                                confidence=c.confidence, timestamp=now,
                                summary=f"[fabricated] {obs.value.lower()} anomaly"))
            if c.mode == "forge-evidence" and c.impersonate in self.cfg.nodes:
                forged = Evidence(origin=c.impersonate, target=c.target, observation=obs,
                                  confidence=c.confidence, timestamp=now,
                                  summary="[forged] impersonated evidence")
                self._emit(forged, claimed_signer=c.impersonate)
        if st.phase in (HEALTHY, REINTEGRATING):
            self._cast(now, c.target, Action.CONTAIN, st.epoch + 1, score=1.0,
                       why="[simulated compromise] fabricated accusation, no real observation")

    def _update_trust(self, now: float, dt: float) -> None:
        tp, tm, q = self.cfg.trust, self.cfg.timers, self.cfg.quorum
        # --- agent (evidence-source) trust
        recent = (self.pool.recent(now, tm.evidence_window_s, min_conf=q.local_min_conf)
                  + self.excluded_pool.recent(now, tm.evidence_window_s, min_conf=q.local_min_conf))
        contradicted = set()
        example: Dict[str, Evidence] = {}          # observability only
        for e in recent:
            if e.origin == self.id or now - e.timestamp < tm.contradiction_grace_s:
                continue
            if self.local_max_since(e.target, e.timestamp - 2.0) < CLEAN_CONF:
                contradicted.add(e.origin)
                example.setdefault(e.origin, e)
        for nid in self.cfg.nodes:
            if nid == self.id:
                continue
            old = self.agent_trust.get(nid)
            if nid in contradicted:
                self.agent_trust.decay(nid, tp.agent_decay_per_s, dt)
            else:
                self.agent_trust.recover(nid, tp.agent_recover_per_s, dt)
            self._log_trust(nid, old, self.agent_trust.get(nid), example.get(nid))
            if self.agent_trust.get(nid) < tp.suspect_below:
                self.metrics.event("attacker_flagged", now, nid, as_agent=True,
                                   trust=round(self.agent_trust.get(nid), 1))
        # --- workload trust
        for nid, st in self.states.items():
            if st.phase in (HEALTHY, REINTEGRATING):
                if self.local_max(nid) >= q.local_min_conf:
                    self.workload_trust.decay(nid, tp.workload_decay_per_s, dt)
                else:
                    self.workload_trust.recover(nid, tp.workload_recover_per_s, dt)
            else:
                self.workload_trust.set(nid, 0.0)
        self.agent_trust.record(now)
        self.workload_trust.record(now)
        self._check_flags()

    # ---- persisted trust: a restart must not wipe a vote-exclusion
    def _load_trust(self) -> None:
        if not self._trust_path or not os.path.exists(self._trust_path):
            return
        try:
            with open(self._trust_path) as fh:
                saved = json.load(fh).get("agent_trust", {})
            for nid, v in saved.items():
                if nid in self.cfg.nodes and nid != self.id:
                    self.agent_trust.set(nid, float(v))
            log.info("restored peer trust from %s: %s", self._trust_path, saved)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not read trust state %s: %s (starting from defaults)", self._trust_path, exc)

    def _save_trust(self, now: float) -> None:
        if not self._trust_path or now - self._trust_saved_at < 3.0:
            return
        snap = {n: round(v, 1) for n, v in self.agent_trust.all().items() if n != self.id}
        blob = json.dumps(snap, sort_keys=True)
        if blob == self._trust_saved:
            return
        try:
            os.makedirs(os.path.dirname(self._trust_path) or ".", exist_ok=True)
            tmp = self._trust_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"saved_at": now, "agent_trust": snap}, fh)
            os.replace(tmp, self._trust_path)
            self._trust_saved, self._trust_saved_at = blob, now
        except OSError as exc:
            log.warning("could not save trust state: %s", exc)

    def _trust_band(self, v: float) -> int:
        tp = self.cfg.trust
        return 2 if v >= tp.suspect_below else (1 if v >= tp.vote_min_trust else 0)

    def _log_trust(self, peer: str, old: float, new: float, e: Optional[Evidence]) -> None:
        if abs(new - old) < 1e-9:
            return
        tp = self.cfg.trust
        if e is not None:
            why = (f"{e.origin} reported {e.observation.value.lower()} trouble on {e.target} "
                   f"(confidence {e.confidence:.2f}) that {self.id} does not see itself")
            reason = "contradicted evidence"
            rate = -tp.agent_decay_per_s
        else:
            why = "none of its recent claims are contradicted, so trust recovers slowly"
            reason = "recovery tick"
            rate = tp.agent_recover_per_s
        self.events.emit(
            obs.TRUST_CHANGE,
            f"Agent {self.id}'s trust in {peer}: {old:.1f} -> {new:.1f} ({why}).",
            peer, {"peer": peer, "old": round(old, 2), "new": round(new, 2), "reason": reason,
                   "rate_per_s": rate,
                   "example_evidence": e.to_dict() if e is not None else None},
            key=("trust", peer), fingerprint=self._trust_band(new))

    def _check_flags(self) -> None:
        tp = self.cfg.trust
        for peer in self.cfg.nodes:
            if peer == self.id:
                continue
            v = self.agent_trust.get(peer)
            suspect, excluded = v < tp.suspect_below, v < tp.vote_min_trust
            v = math.floor(v * 10) / 10 if (suspect or excluded) else math.ceil(v * 10) / 10  # display only
            was_suspect, was_excluded = self._flags.get(peer, (False, False))
            self._flags[peer] = (suspect, excluded)
            msgs = []
            if suspect and not was_suspect:
                msgs.append(("SUSPECT", f"Agent {self.id} marks {peer} as SUSPECT: trust {v:.1f} "
                                        f"fell below {tp.suspect_below:g}."))
            if excluded and not was_excluded:
                msgs.append(("VOTE_EXCLUDED", f"Agent {self.id} will no longer count {peer}'s votes: "
                                              f"trust {v:.1f} is below the vote-exclusion threshold "
                                              f"{tp.vote_min_trust:g}."))
            if was_excluded and not excluded:
                msgs.append(("VOTE_RESTORED", f"Agent {self.id} counts {peer}'s votes again: trust "
                                              f"{v:.1f} is back above {tp.vote_min_trust:g}."))
            if was_suspect and not suspect:
                msgs.append(("CLEARED", f"Agent {self.id} clears the SUSPECT flag on {peer}: trust "
                                        f"{v:.1f} is back above {tp.suspect_below:g}."))
            for flag, text in msgs:
                self.events.emit(obs.FLAG, text, peer,
                                 {"peer": peer, "flag": flag, "trust": round(v, 1)})

    def _vote(self, now: float) -> None:
        q, tm = self.cfg.quorum, self.cfg.timers
        pending = self.votes.pending()                        # observability only
        for target, st in self.states.items():
            if st.phase in (HEALTHY, REINTEGRATING):
                ev = self.pool.recent(now, tm.evidence_window_s, target=target,
                                      min_conf=q.evidence_min_conf)
                score = weighted_score(ev, self.trust_of, q)
                self.scores[target] = score
                self._log_score(target, score)
                local = self.local_max(target)
                if should_vote_contain(local, score, q):
                    self._note_vote("CONTAIN", target, st.epoch + 1, "", True,
                                    f"sees the anomaly itself (confidence {local:.2f}) and "
                                    f"W({target})={score.total:.2f} >= {q.score_threshold:g}")
                    self._cast(now, target, Action.CONTAIN, st.epoch + 1, score=score.total,
                               evidence_ids=score.evidence_ids,
                               why=self.vote_reasons[f"CONTAIN:{target}"]["reason"])
                elif self._investigation_vote(now, target, st, score, local):
                    pass
                else:
                    if local < q.local_min_conf:
                        code, why = "own_normal", (f"own observation of {target} normal "
                                                   f"(confidence {local:.2f} < {q.local_min_conf:g})")
                    else:
                        code, why = "below_threshold", (f"W({target})={score.total:.2f} below "
                                                        f"{q.score_threshold:g}")
                    self._note_vote("CONTAIN", target, st.epoch + 1, "", False, why)
                    peers_want = any(k.startswith(f"CONTAIN:{target}:") for k in pending)
                    if score.total > 0 or peers_want:
                        self._log_withheld("CONTAIN", target, st.epoch + 1, "", code, why)
                if st.phase == HEALTHY:
                    self.inv.evaluate(now, target, score)
            else:
                self.scores[target] = ScoreBreakdown(0.0)
                self._note_vote("CONTAIN", target, st.epoch + 1, "", False,
                                f"{target} is already being handled (phase {st.phase})")

            if st.phase == VALIDATING:
                ok, _why = self.validation_check(target)
                self._note_vote("VALIDATE", target, st.epoch, "", ok,
                                "health, file hashes and behaviour all check out" if ok else _why)
                if ok:
                    self._cast(now, target, Action.VALIDATE, st.epoch,
                               why=self.vote_reasons[f"VALIDATE:{target}"]["reason"])
                else:
                    self._log_withheld("VALIDATE", target, st.epoch, "", _why, _why)

            if st.phase == REINTEGRATING and can_advance(
                    st.stage, now - st.stage_entered, tm.stage_dwell_s,
                    self.workload_trust.get(target), self.local_max(target), q.evidence_min_conf):
                nxt = next_stage(st.stage)
                self._note_vote("ADVANCE_STAGE", target, st.epoch, nxt, True,
                                f"{now - st.stage_entered:.0f}s in {st.stage}, workload trust "
                                f"{self.workload_trust.get(target):.0f} >= {STAGE_THRESHOLDS[nxt]:g}, "
                                f"no anomaly")
                self._cast(now, target, Action.ADVANCE_STAGE, st.epoch, stage=nxt,
                           why=self.vote_reasons[f"ADVANCE_STAGE:{target}"]["reason"])
            elif st.phase == REINTEGRATING:
                why = advance_block_reason(st.stage, now - st.stage_entered, tm.stage_dwell_s,
                                           self.workload_trust.get(target), self.local_max(target),
                                           q.evidence_min_conf) or "waiting"
                nxt = next_stage(st.stage) or ""
                self._note_vote("ADVANCE_STAGE", target, st.epoch, nxt, False, why)
                self._log_withheld("ADVANCE_STAGE", target, st.epoch, nxt, why.split(" ")[0], why)
        self._log_excluded_votes(pending)

    def _investigation_vote(self, now: float, target: str, st: TargetState, score: ScoreBreakdown,
                            local: float) -> bool:
        """A weak but PERSISTENT anomaly that a CORROBORATED investigation confirmed may be
        voted on even though W(T) is below the usual bar. Still required: this agent's own
        persistent re-measurements and a current anomaly (never hearsay), the authorisation
        is for this incident version and workload instance, and it expires."""
        q = self.cfg.quorum
        au = self.inv.vote_authorization(target, now)
        if au is None or not au.own_persistent or local < q.evidence_min_conf \
                or score.total < self.cfg.investigation.band_lo:
            return False
        why = (f"investigation {au.investigation_id[-12:]} confirmed the anomaly persists "
               f"(still seen by {', '.join(au.seers)}) and this agent still sees it itself "
               f"(confidence {local:.2f}); W({target})={score.total:.2f}")
        self._note_vote("CONTAIN", target, st.epoch + 1, "", True, why)
        self._cast(now, target, Action.CONTAIN, st.epoch + 1, score=score.total,
                   evidence_ids=score.evidence_ids, why=why, investigation_id=au.investigation_id)
        return True

    # ---- observability helpers for voting (never feed back into decisions)
    def _note_vote(self, action: str, target: str, epoch: int, stage: str, voted: bool,
                   reason: str) -> None:
        self.vote_reasons[f"{action}:{target}"] = {
            "action": action, "target": target, "epoch": epoch, "stage": stage,
            "proposal": f"{action}:{target}:{epoch}:{stage}", "voted": voted,
            "reason": reason, "ts": self.clock()}

    def _log_withheld(self, action: str, target: str, epoch: int, stage: str, code: str,
                      why: str) -> None:
        verb = {"CONTAIN": f"contain {target}", "VALIDATE": f"validate {target}",
                "ADVANCE_STAGE": f"move {target} to {stage}"}[action]
        self.events.emit(
            obs.VOTE_WITHHELD, f"Agent {self.id} is not voting to {verb}: {why}.", target,
            {"proposal": f"{action}:{target}:{epoch}:{stage}", "action": action, "reason": why},
            key=("withheld", action, target), fingerprint=code)

    def _log_excluded_votes(self, pending: Dict[str, List[str]]) -> None:
        tp = self.cfg.trust
        for key, voters in pending.items():
            for v in voters:
                if v != self.id and not self.eligible_voter(v):
                    t = math.floor(self.agent_trust.get(v) * 10) / 10   # display only
                    self.events.emit(
                        obs.VOTE_WITHHELD,
                        f"Agent {self.id} is not counting {v}'s vote to {obs.proposal_text(key)}: "
                        f"sender {v} is below the vote-exclusion threshold {tp.vote_min_trust:g} "
                        f"(trust {t:.1f}).",
                        key.split(":")[1], {"proposal": key, "excluded_voter": v,
                                            "voter_trust": round(t, 1),
                                            "reason": f"sender {v} below vote-exclusion threshold "
                                                      f"{tp.vote_min_trust:g}"},
                        key=("excluded", key, v), every=10.0)

    def _log_score(self, target: str, score: ScoreBreakdown) -> None:
        q = self.cfg.quorum
        if score.total <= 0 and not self._score_seen.get(target):
            return
        self._score_seen[target] = score.total > 0
        above = score.total >= q.score_threshold
        if score.total <= 0:
            text = f"Agent {self.id}: no current evidence about {target}; W({target}) back to 0."
        else:
            terms = ", ".join(f"{i['sender']} {i['type'].lower()} {i['confidence']:.2f} x trust "
                              f"{i['sender_trust'] / 100:.2f}" for i in score.items[:6])
            text = (f"Agent {self.id} computes W({target})={score.total:.2f} "
                    f"({'at or above' if above else 'below'} the {q.score_threshold:g} needed to vote "
                    f"to contain) from {len(score.items)} item(s) [{terms}]"
                    f"{', diversity bonus +%.2f' % score.diversity_bonus if score.diversity_bonus else ''}.")
        self.events.emit(obs.SCORE, text, target, {"threshold": q.score_threshold, **score.to_dict()},
                         key=("score", target), fingerprint=(score.total > 0, above))

    def validation_check(self, target: str) -> Tuple[bool, str]:
        """Post-recovery validation: health + file-hash check + clean behaviour
        + it really is a new instance."""
        s = self.snaps.get(target)
        st = self.states[target]
        if s is None or not s.reachable or not s.healthy:
            return False, "health check failed"
        if st.contaminated_instance and s.instance_id == st.contaminated_instance:
            return False, "still the contaminated instance"
        if self.cfg.baseline_hashes and s.file_hashes != self.cfg.baseline_hashes:
            return False, "file hashes differ from known-good baseline"
        if self.local_max(target) >= self.cfg.quorum.evidence_min_conf:
            return False, "anomalies still observed"
        return True, "ok"

    # ------------------------------------------------------------------ decisions
    def _record_decision(self, c: Commit, now: float, note: str,
                         justification: Optional[dict] = None) -> dict:
        v = c.sample
        d = {"t": now, "action": v.action.value, "target": v.target, "epoch": v.epoch,
             "stage": v.stage, "voters": c.voters, "note": note,
             "justification": justification or {}}
        self.decisions.append(d)
        self.decision_log.append({"t": now, "action": v.action.value, "target": v.target,
                                  "workload": self.workload(v.target), "epoch": v.epoch, "stage": v.stage,
                                  "voters": c.voters, "note": note, "proposal": c.action_key,
                                  "justification_digest": digest_of(justification or {})})
        self.states[v.target].last_decision = d
        log.info("QUORUM %s on %s (epoch %d%s) by %s", v.action.value, v.target, v.epoch,
                 f", stage {v.stage}" if v.stage else "", ",".join(c.voters))
        what = {"CONTAIN": f"CONTAIN {v.target}", "VALIDATE": f"VALIDATE {v.target}",
                "ADVANCE_STAGE": f"move {v.target} to {v.stage}"}[v.action.value]
        self.events.emit(
            obs.QUORUM,
            f"Quorum reached: {', '.join(c.voters)} signed {what} (epoch {v.epoch}) -> {note}.",
            v.target, {"action": v.action.value, "epoch": v.epoch, "stage": v.stage,
                       "signers": c.voters, "note": note, "proposal": c.action_key,
                       "justification": justification or {}})
        return d

    def _justification(self, c: Commit, now: float) -> dict:
        """What this agent knew when the decision committed (observability only;
        captured before _on_commit mutates any state)."""
        v = c.sample
        votes = [{"voter": x.voter, "score": round(x.score, 3), "ts": x.timestamp,
                  "evidence_ids": len(x.evidence_ids)} for x in c.votes]
        out: Dict[str, object] = {"votes": votes}
        if v.action == Action.CONTAIN:
            ids = {i for x in c.votes for i in x.evidence_ids}
            pool = self.pool.recent(now, self.cfg.timers.evidence_window_s, target=v.target)
            cited = [e.to_dict() for e in pool if e.evidence_id in ids]
            out["evidence"] = cited or [e.to_dict() for e in pool][-20:]
            out["score"] = self.scores.get(v.target, ScoreBreakdown(0.0)).to_dict()
        elif v.action == Action.VALIDATE:
            out["my_validation"] = self.validation_check(v.target)[1]
        else:
            out["my_workload_trust"] = round(self.workload_trust.get(v.target), 1)
            out["stage_threshold"] = STAGE_THRESHOLDS.get(v.stage)
        return out

    def _qc_annotations(self, c: Commit) -> Dict[str, str]:
        v = c.sample
        return {"resilience.io/action": v.action.value, "resilience.io/epoch": str(v.epoch),
                "resilience.io/authorized-by": ",".join(c.voters),
                "resilience.io/qc-votes": ",".join(x.vote_id[:12] for x in c.votes)}

    def _on_commit(self, c: Commit, now: float) -> None:
        v = c.sample
        st = self.states.get(v.target)
        if st is None:
            return
        w = self.workload(v.target)
        just = self._justification(c, now)

        if v.action == Action.CONTAIN and v.epoch == st.epoch + 1 and st.phase in (HEALTHY, REINTEGRATING):
            s = self.snaps.get(v.target)
            st.phase, st.epoch, st.phase_entered = ISOLATED, v.epoch, now
            st.stage, st.stage_entered = "QUARANTINE", now
            st.contaminated_instance = s.instance_id if s else ""
            self.workload_trust.set(v.target, 0.0)
            self.pool.clear_target(v.target)
            self.inv.on_incident_change(v.target, now)
            self._record_decision(c, now, "isolate + recover", just)
            self.metrics.event("contain_committed", now, v.target, voters=c.voters)
            ann, epoch = self._qc_annotations(c), v.epoch

            key = c.action_key
            self.certs.on_commit(c, now)

            def isolate():
                qc = self._qc(key, "CONTAIN", v.target, epoch, "")
                self.backend.apply_stage(w, "QUARANTINE", {**ann, QC_ANNOTATION: qc} if qc else ann)
                self.backend.write_state(w, {"epoch": epoch, "phase": ISOLATED, "stage": "QUARANTINE",
                                             "isolated-epoch": epoch}, qc)
                self.metrics.event("isolation_applied", self.clock(), v.target, executor=self.id)
                self.events.emit(obs.ACTION, f"Agent {self.id} isolated {w} ({v.target}): quarantine "
                                 f"NetworkPolicy applied, signed off by {', '.join(c.voters)}.",
                                 v.target, {"action": "isolate", "workload": w, "result": "ok",
                                            "stage": "QUARANTINE", "authorized_by": c.voters})

            def recover():
                # Isolation first: never replace the pod while the quarantine policy is not
                # confirmed in the cluster, or the new pod would come up un-quarantined and
                # un-validated. Raising makes the executor retry in 2 s (see _run_tasks).
                if int(self.backend.read_state(w).get("isolated-epoch", -1)) < epoch:
                    raise ActionPending("isolation is not confirmed in the cluster yet")
                self.backend.recover(w, epoch, self._qc(key, "CONTAIN", v.target, epoch, ""))
                self.events.emit(obs.ACTION, f"Agent {self.id} started recovery of {w} "
                                 f"({v.target}): redeploying it from the known-good image.",
                                 v.target, {"action": "recover", "workload": w, "result": "ok",
                                            "epoch": epoch})

            self._schedule(now, v.target, f"isolate:{v.target}:{epoch}",
                           lambda: int(self.backend.read_state(w).get("isolated-epoch", -1)) >= epoch,
                           isolate, f"isolate {w}")
            self._schedule(now, v.target, f"recover:{v.target}:{epoch}",
                           lambda: int(self.backend.read_state(w).get("recovered-epoch", -1)) >= epoch,
                           recover, f"redeploy {w} from known-good image")

        elif v.action == Action.VALIDATE and v.epoch == st.epoch and st.phase == VALIDATING:
            st.phase, st.stage, st.stage_entered = REINTEGRATING, "QUARANTINE", now
            self._record_decision(c, now, "validated; start staged reintegration", just)
            self.metrics.event("validated", now, v.target, voters=c.voters)
            self.metrics.event("stage:QUARANTINE", now, v.target)
            epoch, key = v.epoch, c.action_key
            self.certs.on_commit(c, now)
            self._schedule(now, v.target, f"validated:{v.target}:{epoch}",
                           lambda: self.backend.read_state(w).get("phase") == REINTEGRATING
                           and int(self.backend.read_state(w).get("epoch", -1)) >= epoch,
                           lambda: self.backend.write_state(
                               w, {"phase": REINTEGRATING, "stage": "QUARANTINE"},
                               self._qc(key, "VALIDATE", v.target, epoch, "")),
                           f"mark {w} validated")

        elif (v.action == Action.ADVANCE_STAGE and v.epoch == st.epoch and st.phase == REINTEGRATING
              and v.stage == next_stage(st.stage)):
            st.stage, st.stage_entered = v.stage, now
            self.metrics.event(f"stage:{v.stage}", now, v.target)
            if v.stage == "FULL":
                st.phase = HEALTHY
                self.metrics.event("reintegrated", now, v.target)
            self._record_decision(c, now, f"advance to {v.stage}", just)
            ann, stage, phase = self._qc_annotations(c), v.stage, st.phase
            key, epoch = c.action_key, v.epoch
            self.certs.on_commit(c, now)

            def expected_stage():
                # Monotonic: a fail-over executor must never roll a target back
                # to an older stage that has since been superseded.
                if self.states[v.target].stage != stage:
                    return True
                cur = self.backend.current_stage(w)
                cur_i = STAGES.index(cur) if cur in STAGES else STAGES.index("FULL")
                return cur_i >= STAGES.index(stage)

            def apply():
                qc = self._qc(key, "ADVANCE_STAGE", v.target, epoch, stage)
                self.backend.apply_stage(w, stage, {**ann, QC_ANNOTATION: qc} if qc else ann)
                self.backend.write_state(w, {"phase": phase, "stage": stage}, qc)
                self.events.emit(obs.ACTION, f"Agent {self.id} moved {w} ({v.target}) to stage "
                                 f"{stage}" + (": isolation removed, full access restored."
                                               if stage == "FULL" else "."),
                                 v.target, {"action": "stage_change", "workload": w,
                                            "stage": stage, "result": "ok"})

            self._schedule(now, v.target, f"stage:{v.target}:{v.epoch}:{stage}", expected_stage, apply,
                           f"move {w} to {stage}")
        else:
            log.info("ignoring stale/out-of-order commit %s (local phase %s epoch %d)",
                     c.action_key, st.phase, st.epoch)
            self.events.emit(
                obs.QUORUM, f"Agent {self.id} ignored a quorum to {obs.proposal_text(c.action_key)} signed by "
                f"{', '.join(c.voters)}: it no longer matches its state ({st.phase}, epoch {st.epoch}).",
                v.target, {"proposal": c.action_key, "signers": c.voters, "ignored": True,
                           "local_phase": st.phase, "local_epoch": st.epoch})

    # ------------------------------------------------------------------ certificates (executor side)
    def _qc(self, action_key: str, action: str, target: str, epoch: int, stage: str) -> str:
        """The verified quorum certificate (base64) the executor attaches to its Kubernetes call.
        Raises CertificatePending (not complete yet: retry next tick) or CertificateInvalid
        (the action must NOT be performed)."""
        cp = self.cfg.certificates
        if not cp.enforce:
            return ""
        cert = self.certs.get(action_key, wait_s=0.3)
        if cert is None:
            raise CertificatePending(f"waiting for 3 signatures on {action_key}")
        ok, why = verify_certificate(cert, self.registry, self.clock(), quorum=self.cfg.quorum.quorum,
                                     expect=Expect(self.workload(target), action, stage, epoch),
                                     revoked=cp.revoked_signers, current_epoch=self.states[target].epoch,
                                     max_ttl_s=cp.max_ttl_s)
        if not ok:
            raise CertificateInvalid(why)
        return cert.to_b64()

    # ------------------------------------------------------------------ executor
    def executor_rank(self) -> int:
        """Deterministic, leaderless fail-over: agents ordered A<B<C<D (skipping
        peers this agent no longer trusts). Rank r acts after r*stagger seconds
        if nobody before it has produced the effect."""
        order = [n for n in sorted(self.cfg.nodes) if self.eligible_voter(n)]
        return order.index(self.id) if self.id in order else len(order)

    def _schedule(self, now: float, target: str, key: str, effect_present, run, desc: str) -> None:
        rank = self.executor_rank()
        due = now + rank * self.cfg.timers.executor_stagger_s
        self.tasks.append(_Task(due, key, target, self.states[target].epoch, effect_present, run, desc))
        self.events.emit(
            obs.ACTION,
            f"Agent {self.id} is executor rank {rank} for '{desc}': "
            + ("it acts now." if rank == 0 else
               f"it will act in {due - now:.0f}s only if no higher-ranked agent has done it."),
            target, {"action": "scheduled", "task": key, "desc": desc, "rank": rank,
                     "delay_s": round(due - now, 1)})

    def _run_tasks(self, now: float) -> None:
        remaining = []
        for t in self.tasks:
            if self.states[t.target].epoch != t.epoch:
                continue  # superseded by a newer incident
            if t.due > now:
                remaining.append(t)
                continue
            try:
                if t.effect_present():
                    self.events.emit(obs.ACTION, f"Agent {self.id} skipped '{t.desc}': already done "
                                     f"(by itself or a higher-ranked agent).", t.target,
                                     {"action": "skipped", "task": t.key, "desc": t.desc,
                                      "result": "already applied"})
                    continue
                log.info("EXECUTOR %s: %s", self.id, t.desc)
                t.run()
            except ActionPending as exc:                  # a prerequisite is not met yet: retry next tick
                self.events.emit(obs.ACTION, f"Agent {self.id} is holding back '{t.desc}': {exc}.", t.target,
                                 {"action": "waiting", "task": t.key, "detail": str(exc)},
                                 key=("certwait", t.key), every=5.0)
                t.due = now
                remaining.append(t)
            except CertificateInvalid as exc:             # never act on a bad certificate
                log.error("executor REFUSED %s: invalid certificate: %s", t.key, exc)
                self.events.emit(obs.ACTION, f"Agent {self.id} REFUSED to '{t.desc}': the quorum certificate "
                                 f"is not valid ({exc}). Nothing was changed.", t.target,
                                 {"action": "refused", "task": t.key, "desc": t.desc,
                                  "result": f"invalid certificate: {exc}"})
            except Exception as exc:
                log.error("executor task %s failed: %s (retrying)", t.key, exc)
                self.events.emit(obs.ACTION, f"Agent {self.id} failed to '{t.desc}' ({exc}); "
                                 f"retrying in 2s.", t.target,
                                 {"action": "failed", "task": t.key, "desc": t.desc,
                                  "result": f"error: {exc}"}, key=("taskfail", t.key), every=10.0)
                t.due = now + 2.0
                remaining.append(t)
        self.tasks = remaining

    def _track_cluster(self, now: float) -> None:
        """Follow the physical progress of isolation/recovery."""
        for nid, st in self.states.items():
            w = self.workload(nid)
            try:
                if st.phase == ISOLATED and int(self.backend.read_state(w).get(
                        "isolated-epoch", -1)) >= st.epoch:
                    st.phase, st.phase_entered = RECOVERING, now
                    self.metrics.event("isolation_applied", now, nid)
                    self.events.emit(obs.ACTION, f"Agent {self.id} confirms {w} ({nid}) is isolated; "
                                     f"waiting for the clean replacement pod.", nid,
                                     {"action": "isolation_confirmed", "workload": w})
                if st.phase == RECOVERING and self.backend.recovery_done(w, st.epoch):
                    st.phase, st.phase_entered = VALIDATING, now
                    self.metrics.event("recovered", now, nid)
                    self.events.emit(obs.ACTION, f"Agent {self.id} sees {w} ({nid}) recovered: the new "
                                     f"pod from the known-good image is ready; validating it now.",
                                     nid, {"action": "recovered", "workload": w})
                if st.phase == VALIDATING and now - st.phase_entered > self.cfg.timers.validate_timeout_s:
                    log.warning("validation of %s stalled; re-running recovery", w)
                    self.events.emit(obs.ACTION, f"Agent {self.id}: validation of {w} ({nid}) stalled "
                                     f"for {self.cfg.timers.validate_timeout_s:g}s; re-running recovery.",
                                     nid, {"action": "validate_stalled", "workload": w,
                                           "result": self.validation_check(nid)[1]})
                    st.phase_entered = now
                    self._schedule(now, nid, f"re-recover:{nid}:{st.epoch}:{int(now)}",
                                   lambda: False,
                                   lambda w=w, e=st.epoch, n=nid: self.backend.recover(
                                       w, e, self._qc(f"CONTAIN:{n}:{e}:", "CONTAIN", n, e, "")),
                                   f"re-redeploy {w}")
            except Exception as exc:
                # Not silent: a persistent failure here leaves the target stuck in its
                # current phase, so say so (throttled, once per 10 s per target).
                if self.events.emit(
                        obs.ACTION, f"Agent {self.id} could not read the cluster state of {w} "
                        f"({nid}) while it is {st.phase}: {exc}. It will keep retrying.", nid,
                        {"action": "cluster_read_failed", "workload": w, "phase": st.phase,
                         "result": f"error: {exc}"}, key=("trackfail", nid), every=10.0):
                    log.warning("cluster tracking for %s failed: %s", w, exc)

    # ------------------------------------------------------------------ status
    def status(self, events_since: Optional[int] = None, include_events: bool = True) -> dict:
        with self.lock:
            now = self.clock()
            targets = {}
            for nid, st in self.states.items():
                s = self.snaps.get(nid)
                targets[nid] = {
                    **asdict(st), "workload": self.workload(nid),
                    "workload_trust": round(self.workload_trust.get(nid), 1),
                    "reachable": bool(s and s.reachable), "healthy": bool(s and s.healthy),
                    "instance_id": s.instance_id if s else "",
                    "detections": [{"type": d.observation.value, "confidence": d.confidence,
                                    "summary": d.summary} for d in self.latest.get(nid, [])],
                    "score": self.scores.get(nid, ScoreBreakdown(0.0)).to_dict(),
                    "validation": self.validation_check(nid)[1] if st.phase == VALIDATING else None,
                }
            return {
                "node": self.id, "mode": "distributed", "time": now,
                "agent_name": self.cfg.nodes[self.id].agent_name,
                "compromised": self._comp.mode if self._comp.active else None,
                "agent_trust": {n: round(self.trust_of(n), 1) for n in self.cfg.nodes},
                "targets": targets,
                "pending_votes": self.votes.pending(),
                "decisions": list(self.decisions)[-15:],
                "rejections": list(self.rejections)[-10:],
                "recent_evidence": [e.to_dict() for e in self.pool.recent(
                    now, self.cfg.timers.evidence_window_s)][-20:],
                "peers": self.transport.peer_status() if self.transport else {},
                "executor_rank": self.executor_rank(),
                # ---- observability
                "thresholds": {"score_threshold": self.cfg.quorum.score_threshold,
                               "local_min_conf": self.cfg.quorum.local_min_conf,
                               "evidence_min_conf": self.cfg.quorum.evidence_min_conf,
                               "vote_min_trust": self.cfg.trust.vote_min_trust,
                               "suspect_below": self.cfg.trust.suspect_below,
                               "quorum": self.cfg.quorum.quorum, "n": self.cfg.quorum.n},
                "observations": {t: {**m, "confidence": self.local_max(t)}
                                 for t, m in self.detector.measurements.items()},
                "agent_trust_history": {
                    n: obs.downsample(self.agent_trust.history(n), now - 60.0)
                    for n in self.cfg.nodes if n != self.id},
                "vote_reasons": dict(self.vote_reasons),
                "pending_votes_detail": self._pending_detail(),
                "flags": {n: {"suspect": f[0], "vote_excluded": f[1]}
                          for n, f in self._flags.items() if n != self.id},
                "investigations": self.inv.status(),
                "certificates": self.certs.status(),
                "decision_log": self.decision_log.status(),
                "event_boot": self.events.boot,
                "event_seq": self.events.seq,
                "events": self.events.recent(since=events_since) if include_events else [],
            }

    def _pending_detail(self) -> Dict[str, dict]:
        tp = self.cfg.trust
        out = {}
        for key, voters in self.votes.pending().items():
            counted = [v for v in voters if self.eligible_voter(v)]
            excluded = [{"voter": v, "trust": round(self.agent_trust.get(v), 1),
                         "reason": f"trust below vote-exclusion threshold {tp.vote_min_trust:g}"}
                        for v in voters if not self.eligible_voter(v)]
            action, target = key.split(":")[0], key.split(":")[1]
            mine = self.vote_reasons.get(f"{action}:{target}")
            out[key] = {"voters": voters, "counted": counted, "excluded": excluded,
                        "needed": self.votes.quorum,
                        "not_voted": [n for n in self.cfg.nodes if n not in voters],
                        "my_reason": mine["reason"] if mine and self.id not in voters else None}
        return out

    def metrics_summary(self) -> List[dict]:
        return self.metrics.summary(fetch_availability(self.cfg.client_stats_url))
