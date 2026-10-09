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
from . import actions as act
from .actions import ActionAudit, classify_error
from .forensics import SnapshotManager
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
    attempt: int = 1                      # which recovery attempt of this incident is current
    attention: Optional[dict] = None      # set when a human is needed (validation keeps failing, ...)


@dataclass
class _Task:
    due: float
    key: str
    target: str
    epoch: int
    effect_present: Callable[[], bool]
    run: Callable[[], None]
    desc: str
    guard: Optional[Callable[[], Optional[str]]] = None   # returns a reason if the task is now stale
    action: str = ""                                      # name used in the audit trail
    attempt: int = 1                                      # recovery attempt this task belongs to
    tries: int = 0                                        # executions attempted so far
    ran_ok: bool = False                                  # the write call returned success
    waiting_noted: bool = False
    unknown: bool = False                                 # an earlier outcome could not be determined
    ambiguous: bool = False                               # the last failure may or may not have been applied


class ResilienceAgent:
    def __init__(self, cfg: ClusterConfig, node_id: str, signer: Signer,
                 registry: KeyRegistry, telemetry: TelemetrySource,
                 backend: ResponseBackend, metrics: Optional[MetricsRecorder] = None,
                 compromise: Optional[CompromiseSource] = None,
                 clock: Callable[[], float] = time.time,
                 decision_log_path: Optional[str] = None, trust_state_path: Optional[str] = None,
                 snapshot_dir: Optional[str] = None, audit_log_path: Optional[str] = None):
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
        self.forensics = SnapshotManager(self, snapshot_dir)     # evidence before replacement (docs/RECOVERY.md)
        self.audit = ActionAudit(node_id, audit_log_path, clock)  # every cluster action + its observable result
        self._rec_seen: Dict[Tuple[str, int, int], float] = {}   # (target, epoch, attempt) -> redeploy first seen in cluster
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
            st.attempt = max(1, int(s.get("recovery-attempt", 1) or 1))
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
                "ADVANCE_STAGE": f"move {target} to {stage}",
                "RETRY_RECOVERY": f"redeploy {target} again (attempt {stage})"}[action.value]
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

            if st.phase in (RECOVERING, VALIDATING):
                self._vote_recovery(now, target, st)

            if st.phase == REINTEGRATING and st.stage == "FULL":
                pass                                  # waiting for the cluster to confirm the policy is gone
            elif st.phase == REINTEGRATING and can_advance(
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

    def _recovery_failure(self, now: float, target: str, st: TargetState) -> Optional[str]:
        """Why the current recovery attempt has FAILED (None = not failed (yet)). Failure is judged only
        after `validate_timeout_s`, so a slow start is not mistaken for a bad recovery."""
        tm = self.cfg.timers
        if st.phase == VALIDATING:
            ok, why = self.validation_check(target)
            if not ok and now - st.phase_entered > tm.validate_timeout_s:
                return f"validation still failing after {tm.validate_timeout_s:g}s: {why}"
        elif st.phase == RECOVERING:
            seen = self._rec_seen.get((target, st.epoch, st.attempt))
            if seen is not None and now - seen > tm.validate_timeout_s:
                return f"the replacement pod was not ready {tm.validate_timeout_s:g}s after the redeploy"
        return None

    def _vote_recovery(self, now: float, target: str, st: TargetState) -> None:
        why = self._recovery_failure(now, target, st)
        if not why:
            return
        rc = self.cfg.recovery
        if st.attempt > rc.max_retries:             # every allowed attempt has been used
            self._attention(target, now, f"all {st.attempt} recovery attempts failed; last: {why}")
            return
        nxt = str(st.attempt + 1)
        self._note_vote("RETRY_RECOVERY", target, st.epoch, nxt, True, why)
        self._cast(now, target, Action.RETRY_RECOVERY, st.epoch, stage=nxt, why=why)

    def _attention(self, target: str, now: float, reason: str) -> None:
        """Mark the incident as needing a human. The workload stays quarantined; it is never shown healthy."""
        st = self.states[target]
        if st.attention is not None:
            return
        st.attention = {"since": now, "reason": reason, "attempts": st.attempt, "phase": st.phase}
        w = self.workload(target)
        self.audit.record(action="needs_human_attention", task=f"attention:{target}:{st.epoch}", workload=w,
                          target=target, epoch=st.epoch, outcome=act.ABANDONED, attempt=st.attempt, detail=reason)
        self.events.emit(obs.ACTION, f"Agent {self.id}: {w} ({target}) NEEDS HUMAN ATTENTION - {reason}. "
                         f"It stays quarantined and is NOT shown as healthy.", target,
                         {"action": "needs_attention", "workload": w, "reason": reason, "attempts": st.attempt})

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
                "ADVANCE_STAGE": f"move {target} to {stage}",
                "RETRY_RECOVERY": f"redeploy {target} again"}[action]
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
                "ADVANCE_STAGE": f"move {v.target} to {v.stage}",
                "RETRY_RECOVERY": f"redeploy {v.target} again (attempt {v.stage})"}[v.action.value]
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
        elif v.action == Action.RETRY_RECOVERY:
            out["failure"] = self._recovery_failure(now, v.target, self.states[v.target]) or "not failing on this agent's view"
            out["attempt"] = v.stage
        else:
            out["my_workload_trust"] = round(self.workload_trust.get(v.target), 1)
            out["stage_threshold"] = STAGE_THRESHOLDS.get(v.stage)
        return out

    def _qc_annotations(self, c: Commit) -> Dict[str, str]:
        v = c.sample
        return {"resilience.io/action": v.action.value, "resilience.io/epoch": str(v.epoch),
                "resilience.io/authorized-by": ",".join(c.voters),
                "resilience.io/qc-votes": ",".join(x.vote_id[:12] for x in c.votes)}

    @staticmethod
    def _snap_tag(attempt: int) -> str:
        """Snapshot of the pods that attempt `attempt` is about to replace."""
        return "contain" if attempt <= 1 else f"failed{attempt - 1}"

    def _make_recover(self, target: str, epoch: int, key: str, attempt: int, snap_key: str):
        """(effect_present, run) for recovery attempt `attempt` of incident `epoch`."""
        w = self.workload(target)
        action = "CONTAIN" if attempt <= 1 else "RETRY_RECOVERY"
        stage = "" if attempt <= 1 else str(attempt)

        def present() -> bool:
            s = self._cluster_state(w)
            return (int(s.get("recovered-epoch", -1)) >= epoch
                    and int(s.get("recovery-attempt", 1)) >= attempt)

        def recover() -> None:
            # 1. isolation first: never replace the pod while the quarantine policy is not confirmed.
            if int(self._cluster_state(w).get("isolated-epoch", -1)) < epoch:
                raise ActionPending("isolation is not confirmed in the cluster yet")
            # 2. evidence first: the replacement destroys it (bounded wait: never blocks forever).
            ok, why = self.forensics.ready(snap_key, self.clock())
            if not ok:
                raise ActionPending(why)
            # 3. never two recoveries of the same incident at once.
            if self.backend.rollout_in_progress(w):
                raise ActionPending("a rollout of this workload is still replacing pods; not starting a second one")
            self.backend.recover(w, epoch, self._qc(key, action, target, epoch, stage), attempt)
            self.events.emit(obs.ACTION, f"Agent {self.id} started recovery of {w} ({target}), attempt "
                             f"{attempt}: redeploying it from the known-good image.", target,
                             {"action": "recover", "workload": w, "result": "ok", "epoch": epoch,
                              "attempt": attempt})
        return present, recover

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
            st.attempt, st.attention = 1, None
            st.contaminated_instance = s.instance_id if s else ""
            self.workload_trust.set(v.target, 0.0)
            self.pool.clear_target(v.target)
            self.inv.on_incident_change(v.target, now)
            d = self._record_decision(c, now, "isolate + recover", just)
            self.metrics.event("contain_committed", now, v.target, voters=c.voters)
            ann, epoch = self._qc_annotations(c), v.epoch
            key = c.action_key
            self.certs.on_commit(c, now)
            # Evidence BEFORE replacement: values are taken now, the cluster reads happen in the background.
            snap_key = self.forensics.capture_async(v.target, epoch, "contain", "containment", d, now)

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

            present, recover = self._make_recover(v.target, epoch, key, 1, snap_key)
            self._schedule(now, v.target, f"isolate:{v.target}:{epoch}",
                           lambda: int(self._cluster_state(w).get("isolated-epoch", -1)) >= epoch,
                           isolate, f"isolate {w}", action="CONTAIN/isolate",
                           guard=lambda: self._guard(v.target, epoch, (ISOLATED, RECOVERING, VALIDATING)))
            self._schedule(now, v.target, f"recover:{v.target}:{epoch}:1", present, recover,
                           f"redeploy {w} from known-good image", action="CONTAIN/recover", attempt=1,
                           guard=lambda: self._guard(v.target, epoch, (ISOLATED, RECOVERING, VALIDATING), 1))

        elif (v.action == Action.RETRY_RECOVERY and v.epoch == st.epoch and st.phase in (RECOVERING, VALIDATING)
              and v.stage == str(st.attempt + 1)):
            n, epoch, key = int(v.stage), v.epoch, c.action_key
            failure = self._recovery_failure(now, v.target, st) or "validation failed (as judged by the quorum)"
            st.attempt, st.phase, st.phase_entered, st.attention = n, RECOVERING, now, None
            d = self._record_decision(c, now, f"recovery attempt {n - 1} failed validation; redeploy again", just)
            self.metrics.event("validation_failed", now, v.target, attempt=n - 1)
            self.certs.on_commit(c, now)
            snap_key = self.forensics.capture_async(v.target, epoch, self._snap_tag(n),
                                                    f"recovery attempt {n - 1} failed: {failure}", d, now)
            present, recover = self._make_recover(v.target, epoch, key, n, snap_key)
            delay = self.cfg.recovery.backoff(n - 1)
            self._schedule(now, v.target, f"recover:{v.target}:{epoch}:{n}", present, recover,
                           f"redeploy {w} again (attempt {n})", delay=delay, action="RETRY_RECOVERY", attempt=n,
                           guard=lambda: self._guard(v.target, epoch, (RECOVERING, VALIDATING), n))

        elif v.action == Action.VALIDATE and v.epoch == st.epoch and st.phase == VALIDATING:
            st.phase, st.stage, st.stage_entered = REINTEGRATING, "QUARANTINE", now
            st.attention = None
            self._record_decision(c, now, "validated; start staged reintegration", just)
            self.metrics.event("validated", now, v.target, voters=c.voters)
            self.metrics.event("stage:QUARANTINE", now, v.target)
            epoch, key = v.epoch, c.action_key
            self.certs.on_commit(c, now)
            self._schedule(now, v.target, f"validated:{v.target}:{epoch}",
                           lambda: self._cluster_state(w).get("phase") == REINTEGRATING
                           and int(self._cluster_state(w).get("epoch", -1)) >= epoch,
                           lambda: self.backend.write_state(
                               w, {"phase": REINTEGRATING, "stage": "QUARANTINE"},
                               self._qc(key, "VALIDATE", v.target, epoch, "")),
                           f"mark {w} validated", action="VALIDATE")

        elif (v.action == Action.ADVANCE_STAGE and v.epoch == st.epoch and st.phase == REINTEGRATING
              and v.stage == next_stage(st.stage)):
            st.stage, st.stage_entered = v.stage, now
            self.metrics.event(f"stage:{v.stage}", now, v.target)
            # At FULL the target is NOT yet HEALTHY: it becomes HEALTHY only once the cluster confirms the
            # isolation policy is really gone (_track_cluster).
            self._record_decision(c, now, f"advance to {v.stage}", just)
            ann, stage = self._qc_annotations(c), v.stage
            phase = HEALTHY if stage == "FULL" else REINTEGRATING
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
                           f"move {w} to {stage}", action=f"ADVANCE_STAGE/{stage}")
        else:
            log.info("ignoring stale/out-of-order commit %s (local phase %s epoch %d)",
                     c.action_key, st.phase, st.epoch)
            self.events.emit(
                obs.QUORUM, f"Agent {self.id} ignored a quorum to {obs.proposal_text(c.action_key)} signed by "
                f"{', '.join(c.voters)}: it no longer matches its state ({st.phase}, epoch {st.epoch}).",
                v.target, {"proposal": c.action_key, "signers": c.voters, "ignored": True,
                           "local_phase": st.phase, "local_epoch": st.epoch})

    def _cluster_state(self, workload: str) -> dict:
        """Deployment state annotations; RAISES if the cluster cannot be read, so that "could not read"
        is never mistaken for "the effect is absent"."""
        return getattr(self.backend, "read_state_strict", self.backend.read_state)(workload)

    def _guard(self, target: str, epoch: int, phases, attempt: Optional[int] = None) -> Optional[str]:
        """Is this task still about the same incident and the same stage of it? (None = yes.)"""
        st = self.states[target]
        if st.epoch != epoch:
            return f"incident version changed ({epoch} -> {st.epoch})"
        if st.phase not in phases:
            return f"{target} is now {st.phase}, not {'/'.join(phases)}"
        if attempt is not None and st.attempt != attempt:
            return f"recovery attempt changed ({attempt} -> {st.attempt})"
        return None

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

    def _schedule(self, now: float, target: str, key: str, effect_present, run, desc: str,
                  delay: float = 0.0, guard=None, action: str = "", attempt: int = 1) -> None:
        rank = self.executor_rank()
        due = now + delay + rank * self.cfg.timers.executor_stagger_s
        self.tasks.append(_Task(due, key, target, self.states[target].epoch, effect_present, run, desc,
                                guard=guard, action=action or key.split(":")[0], attempt=attempt))
        self.events.emit(
            obs.ACTION,
            f"Agent {self.id} is executor rank {rank} for '{desc}': "
            + ("it acts now." if rank == 0 and delay == 0 else
               f"it will act in {due - now:.0f}s only if no higher-ranked agent has done it."),
            target, {"action": "scheduled", "task": key, "desc": desc, "rank": rank,
                     "delay_s": round(due - now, 1)})

    def _audit(self, t: _Task, outcome: str, detail: str = "", error: str = "", duration: float = 0.0) -> dict:
        return self.audit.record(action=t.action, task=t.key, workload=self.workload(t.target), target=t.target,
                                 epoch=t.epoch, outcome=outcome, attempt=max(1, t.tries), detail=detail,
                                 error=error, duration_s=duration)

    def _run_tasks(self, now: float) -> None:
        remaining = []
        for t in self.tasks:
            if self.states[t.target].epoch != t.epoch:
                self._audit(t, act.REFUSED_STALE, "this incident was superseded by a newer one; nothing done")
                continue
            if t.due > now:
                remaining.append(t)
                continue
            stale = t.guard() if t.guard else None
            if stale:
                self._audit(t, act.REFUSED_STALE, stale)
                self.events.emit(obs.ACTION, f"Agent {self.id} dropped '{t.desc}': {stale}.", t.target,
                                 {"action": "refused_stale", "task": t.key, "desc": t.desc, "result": stale})
                continue
            if self._step(t, now):
                remaining.append(t)
        self.tasks = remaining

    def _step(self, t: _Task, now: float) -> bool:
        """One execution step of an action. Returns True while the task must stay queued.

        A timeout is NOT a failure: after any ambiguous error the actual cluster state decides what happened.
        Every step writes an audit record with an explicit outcome."""
        rc = self.cfg.recovery
        t0 = time.monotonic()

        def backoff() -> float:
            return min(rc.action_backoff_max_s, rc.action_backoff_base_s * (2 ** max(0, t.tries - 1)))

        # 1. look at the cluster BEFORE doing anything
        try:
            present = t.effect_present()
        except Exception as exc:
            t.due = now + rc.unknown_recheck_s
            if t.tries > 0:
                t.unknown = True
                self._audit(t, act.UNKNOWN, "an earlier call had an unknown result and the cluster cannot be "
                            "read now; will check again", str(exc))
                self.events.emit(obs.ACTION, f"Agent {self.id}: result of '{t.desc}' is UNKNOWN (the cluster "
                                 f"cannot be read: {exc}); checking again in {rc.unknown_recheck_s:g}s.", t.target,
                                 {"action": "unknown", "task": t.key, "desc": t.desc, "result": "unknown"},
                                 key=("unknown", t.key), every=10.0)
            elif not t.waiting_noted:
                t.waiting_noted = True
                self._audit(t, act.WAITING, "cannot read the cluster yet; nothing done", str(exc))
            return True
        if present:
            if t.unknown:
                out, det = act.UNKNOWN_RESOLVED, "the earlier UNKNOWN is settled: the effect IS in the cluster"
            elif t.tries == 0:
                out, det = act.ALREADY_APPLIED, "already in place (this or another agent did it); nothing to do"
            elif t.ambiguous and not t.ran_ok:
                out, det = act.APPLIED_AFTER_TIMEOUT, "the call timed out, but the cluster shows the effect"
            else:
                out, det = act.APPLIED, "the cluster shows the effect"
            self._audit(t, out, det)
            if out == act.ALREADY_APPLIED:
                self.events.emit(obs.ACTION, f"Agent {self.id} skipped '{t.desc}': already done "
                                 f"(by itself or a higher-ranked agent).", t.target,
                                 {"action": "skipped", "task": t.key, "desc": t.desc, "result": "already applied"})
            return False

        # 2. act
        t.tries += 1
        if t.tries > rc.action_max_attempts:
            self._audit(t, act.ABANDONED, f"gave up after {rc.action_max_attempts} attempts")
            self._attention(t.target, now, f"'{t.desc}' could not be completed after {rc.action_max_attempts} attempts")
            return False
        try:
            log.info("EXECUTOR %s: %s (try %d)", self.id, t.desc, t.tries)
            t.run()
        except ActionPending as exc:                       # prerequisite not met: not an attempt
            t.tries -= 1
            if not t.waiting_noted:
                t.waiting_noted = True
                self._audit(t, act.WAITING, str(exc))
            self.events.emit(obs.ACTION, f"Agent {self.id} is holding back '{t.desc}': {exc}.", t.target,
                             {"action": "waiting", "task": t.key, "detail": str(exc)},
                             key=("certwait", t.key), every=5.0)
            t.due = now
            return True
        except CertificateInvalid as exc:                  # never act on a bad certificate
            log.error("executor REFUSED %s: invalid certificate: %s", t.key, exc)
            self._audit(t, act.REFUSED_CERT, f"invalid quorum certificate: {exc}")
            self.events.emit(obs.ACTION, f"Agent {self.id} REFUSED to '{t.desc}': the quorum certificate "
                             f"is not valid ({exc}). Nothing was changed.", t.target,
                             {"action": "refused", "task": t.key, "desc": t.desc,
                              "result": f"invalid certificate: {exc}"})
            return False
        except Exception as exc:
            dur = time.monotonic() - t0
            if classify_error(exc) == act.DEFINITE:
                t.ambiguous = False
                t.due = now + backoff()
                self._audit(t, act.FAILED, f"refused; retrying in {t.due - now:.0f}s", str(exc), dur)
                self.events.emit(obs.ACTION, f"Agent {self.id} failed to '{t.desc}' ({exc}); retrying in "
                                 f"{t.due - now:.0f}s.", t.target,
                                 {"action": "failed", "task": t.key, "desc": t.desc, "result": f"error: {exc}"},
                                 key=("taskfail", t.key), every=10.0)
                return True
            # ambiguous: the request may have been applied. Look.
            t.ambiguous = True
            try:
                present = t.effect_present()
            except Exception as exc2:
                t.unknown, t.due = True, now + rc.unknown_recheck_s
                self._audit(t, act.UNKNOWN, "the call's result is unknown and the cluster cannot be read; "
                            "will check again before doing anything", f"{exc}; read: {exc2}", dur)
                self.events.emit(obs.ACTION, f"Agent {self.id}: result of '{t.desc}' is UNKNOWN (the call timed "
                                 f"out and the cluster cannot be read).", t.target,
                                 {"action": "unknown", "task": t.key, "desc": t.desc, "result": "unknown"},
                                 key=("unknown", t.key), every=10.0)
                return True
            if present:
                self._audit(t, act.APPLIED_AFTER_TIMEOUT, "the call timed out but the cluster shows the effect; "
                            "not repeated", str(exc), dur)
                return False
            t.due = now + backoff()
            self._audit(t, act.NOT_APPLIED, f"the call timed out and the effect is NOT in the cluster; "
                        f"retrying in {t.due - now:.0f}s", str(exc), dur)
            return True

        # 3. the call returned: confirm in the cluster
        t.ran_ok = True
        dur = time.monotonic() - t0
        try:
            present = t.effect_present()
        except Exception as exc:
            self._audit(t, act.APPLIED_UNCONFIRMED, "the call succeeded; confirming read failed", str(exc), dur)
            return False
        if present:
            self._audit(t, act.APPLIED, "the call succeeded and the cluster shows the effect", duration=dur)
            return False
        t.due = now + rc.unknown_recheck_s
        self._audit(t, act.NOT_APPLIED, "the call succeeded but the effect is not visible yet; checking again", duration=dur)
        return True

    def _old_pods_gone(self, target: str, st: TargetState) -> bool:
        """Recovery counts as done only when the pods that existed when the snapshot was taken are gone."""
        if not self.cfg.recovery.check_replacement_pods:
            return True
        uids = self.forensics.pod_uids(self.forensics.key(target, st.epoch, self._snap_tag(st.attempt)))
        if not uids:
            return True
        live = {p.get("uid") for p in self.backend.list_pods(self.workload(target))}
        return not (set(uids) & live)

    def _track_cluster(self, now: float) -> None:
        """Follow the physical progress of isolation/recovery. Reads are strict: an unreadable cluster is
        'unknown', never 'the effect is absent'."""
        for nid, st in self.states.items():
            w = self.workload(nid)
            try:
                if st.phase == ISOLATED and int(self._cluster_state(w).get("isolated-epoch", -1)) >= st.epoch:
                    st.phase, st.phase_entered = RECOVERING, now
                    self.metrics.event("isolation_applied", now, nid)
                    self.events.emit(obs.ACTION, f"Agent {self.id} confirms {w} ({nid}) is isolated; "
                                     f"waiting for the clean replacement pod.", nid,
                                     {"action": "isolation_confirmed", "workload": w})
                if st.phase == RECOVERING:
                    cs = self._cluster_state(w)
                    if (int(cs.get("recovered-epoch", -1)) >= st.epoch
                            and int(cs.get("recovery-attempt", 1)) >= st.attempt):
                        self._rec_seen.setdefault((nid, st.epoch, st.attempt), now)
                    if self.backend.recovery_done(w, st.epoch, st.attempt) and self._old_pods_gone(nid, st):
                        st.phase, st.phase_entered = VALIDATING, now
                        self.metrics.event("recovered", now, nid)
                        self.events.emit(obs.ACTION, f"Agent {self.id} sees {w} ({nid}) recovered: the new "
                                         f"pod from the known-good image is ready; validating it now.",
                                         nid, {"action": "recovered", "workload": w, "attempt": st.attempt})
                if st.phase == REINTEGRATING and st.stage == "FULL" and self.backend.current_stage(w) is None:
                    st.phase, st.attention = HEALTHY, None
                    self.metrics.event("reintegrated", now, nid)
                    self.events.emit(obs.ACTION, f"Agent {self.id} confirms {w} ({nid}) is HEALTHY: validated, "
                                     f"and the cluster shows the isolation policy is gone.", nid,
                                     {"action": "healthy_confirmed", "workload": w})
            except Exception as exc:
                # Not silent: a persistent failure here leaves the target stuck in its
                # current phase, so say so (throttled, once per 10 s per target).
                if self.events.emit(
                        obs.ACTION, f"Agent {self.id} could not read the cluster state of {w} "
                        f"({nid}) while it is {st.phase}: {exc}. Its state is UNKNOWN until it can; it will "
                        f"keep retrying.", nid,
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
                    "recovery": {"attempt": st.attempt, "max_attempts": self.cfg.recovery.max_retries + 1,
                                 "failure": self._recovery_failure(now, nid, st)},
                    "unknown_action": any(r["target"] == nid for r in self.audit.unresolved.values()),
                }
                targets[nid]["display_state"] = (
                    "NEEDS_ATTENTION" if st.attention else "UNKNOWN" if targets[nid]["unknown_action"] else st.phase)
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
                "snapshots": self.forensics.status(),
                "actions": self.audit.status(),
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
