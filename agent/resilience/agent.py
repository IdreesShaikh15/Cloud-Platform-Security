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

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .config import ClusterConfig
from .crypto import KeyRegistry, Signer
from .detection import Detection, Detector, max_confidence
from .evidence import Action, Evidence, ObsType, ReplayCache, Vote, open_envelope, seal
from .metrics import MetricsRecorder
from .monitoring import Snapshot, TelemetrySource, fetch_availability
from .proto import resilience_pb2 as pb
from .quorum import Commit, EvidencePool, ScoreBreakdown, VoteBook, should_vote_contain, weighted_score
from .reintegration import STAGES, STAGE_SENSITIVITY, STAGE_THRESHOLDS, can_advance, next_stage
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
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.id = node_id
        self.signer = signer
        self.registry = registry
        self.telemetry = telemetry
        self.backend = backend
        self.metrics = metrics or MetricsRecorder(node_id)
        self.compromise = compromise or CompromiseSource(path=None)
        self.clock = clock
        self.transport = None                      # PeerClient, attached later

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
    def on_envelope(self, env: pb.SignedEnvelope, transport_identity: Optional[str]) -> Tuple[bool, str]:
        now = self.clock()
        claim, reason = open_envelope(env, self.registry, transport_identity=transport_identity,
                                      now=now, max_skew_s=self.cfg.timers.max_clock_skew_s,
                                      replay=self.replay)
        if claim is None:
            culprit = transport_identity
            if culprit and culprit != self.id:
                self.agent_trust.penalize(culprit, self.cfg.trust.invalid_message_penalty)
            self.rejections.append({"t": now, "from": culprit, "claimed_signer": env.signer,
                                    "kind": env.kind, "reason": reason})
            log.warning("REJECTED %s from %s (claimed %s): %s", env.kind, culprit, env.signer, reason)
            return False, reason
        if isinstance(claim, Evidence):
            self.pool.add(claim)
        else:
            self.votes.add(claim)
        return True, "ok"

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

    def _cast(self, now: float, target: str, action: Action, epoch: int, stage: str = "",
              score: float = 0.0, evidence_ids=()) -> None:
        key = f"{action.value}:{target}:{epoch}:{stage}"
        if self.votes.is_committed(key):
            return
        if now - self.my_votes.get(key, -1e9) < REVOTE_EVERY_S:
            return
        self.my_votes[key] = now
        self._emit(Vote(voter=self.id, target=target, action=action, epoch=epoch, stage=stage,
                        score=score, timestamp=now, evidence_ids=tuple(evidence_ids)[:20]))

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

    def _observe(self, now: float) -> None:
        self.snaps = self.telemetry.collect()
        sens = {t: STAGE_SENSITIVITY.get(st.stage, 1.0)
                for t, st in self.states.items() if st.phase == REINTEGRATING}
        dets = self.detector.analyze(self.snaps, now, sens)
        q = self.cfg.quorum
        for target, ds in dets.items():
            self.latest[target] = ds
            m = max_confidence(ds)
            self.local_hist[target].append((now, m))
            if m >= q.local_min_conf:
                self.metrics.event("first_detection", now, target,
                                   types=[d.observation.value for d in ds])
            if self.states[target].phase not in (HEALTHY, REINTEGRATING):
                continue
            for d in ds:
                if d.confidence >= q.evidence_min_conf:
                    self._emit(Evidence(origin=self.id, target=target, observation=d.observation,
                                        confidence=d.confidence, timestamp=now, summary=d.summary))

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
            self._cast(now, c.target, Action.CONTAIN, st.epoch + 1, score=1.0)

    def _update_trust(self, now: float, dt: float) -> None:
        tp, tm, q = self.cfg.trust, self.cfg.timers, self.cfg.quorum
        # --- agent (evidence-source) trust
        recent = self.pool.recent(now, tm.evidence_window_s, min_conf=q.local_min_conf)
        contradicted = set()
        for e in recent:
            if e.origin == self.id or now - e.timestamp < tm.contradiction_grace_s:
                continue
            if self.local_max_since(e.target, e.timestamp - 2.0) < CLEAN_CONF:
                contradicted.add(e.origin)
        for nid in self.cfg.nodes:
            if nid == self.id:
                continue
            if nid in contradicted:
                self.agent_trust.decay(nid, tp.agent_decay_per_s, dt)
            else:
                self.agent_trust.recover(nid, tp.agent_recover_per_s, dt)
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

    def _vote(self, now: float) -> None:
        q, tm = self.cfg.quorum, self.cfg.timers
        for target, st in self.states.items():
            if st.phase in (HEALTHY, REINTEGRATING):
                ev = self.pool.recent(now, tm.evidence_window_s, target=target,
                                      min_conf=q.evidence_min_conf)
                score = weighted_score(ev, self.trust_of, q)
                self.scores[target] = score
                if should_vote_contain(self.local_max(target), score, q):
                    self._cast(now, target, Action.CONTAIN, st.epoch + 1, score=score.total,
                               evidence_ids=score.evidence_ids)
            else:
                self.scores[target] = ScoreBreakdown(0.0)

            if st.phase == VALIDATING:
                ok, _why = self.validation_check(target)
                if ok:
                    self._cast(now, target, Action.VALIDATE, st.epoch)

            if st.phase == REINTEGRATING and can_advance(
                    st.stage, now - st.stage_entered, tm.stage_dwell_s,
                    self.workload_trust.get(target), self.local_max(target), q.evidence_min_conf):
                self._cast(now, target, Action.ADVANCE_STAGE, st.epoch, stage=next_stage(st.stage))

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
    def _record_decision(self, c: Commit, now: float, note: str) -> dict:
        v = c.sample
        d = {"t": now, "action": v.action.value, "target": v.target, "epoch": v.epoch,
             "stage": v.stage, "voters": c.voters, "note": note}
        self.decisions.append(d)
        self.states[v.target].last_decision = d
        log.info("QUORUM %s on %s (epoch %d%s) by %s", v.action.value, v.target, v.epoch,
                 f", stage {v.stage}" if v.stage else "", ",".join(c.voters))
        return d

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

        if v.action == Action.CONTAIN and v.epoch == st.epoch + 1 and st.phase in (HEALTHY, REINTEGRATING):
            s = self.snaps.get(v.target)
            st.phase, st.epoch, st.phase_entered = ISOLATED, v.epoch, now
            st.stage, st.stage_entered = "QUARANTINE", now
            st.contaminated_instance = s.instance_id if s else ""
            self.workload_trust.set(v.target, 0.0)
            self.pool.clear_target(v.target)
            self._record_decision(c, now, "isolate + recover")
            self.metrics.event("contain_committed", now, v.target, voters=c.voters)
            ann, epoch = self._qc_annotations(c), v.epoch

            def isolate():
                self.backend.apply_stage(w, "QUARANTINE", ann)
                self.backend.write_state(w, {"epoch": epoch, "phase": ISOLATED, "stage": "QUARANTINE",
                                             "isolated-epoch": epoch})
                self.metrics.event("isolation_applied", self.clock(), v.target, executor=self.id)

            def recover():
                self.backend.recover(w, epoch)

            self._schedule(now, v.target, f"isolate:{v.target}:{epoch}",
                           lambda: int(self.backend.read_state(w).get("isolated-epoch", -1)) >= epoch,
                           isolate, f"isolate {w}")
            self._schedule(now, v.target, f"recover:{v.target}:{epoch}",
                           lambda: int(self.backend.read_state(w).get("recovered-epoch", -1)) >= epoch,
                           recover, f"redeploy {w} from known-good image")

        elif v.action == Action.VALIDATE and v.epoch == st.epoch and st.phase == VALIDATING:
            st.phase, st.stage, st.stage_entered = REINTEGRATING, "QUARANTINE", now
            self._record_decision(c, now, "validated; start staged reintegration")
            self.metrics.event("validated", now, v.target, voters=c.voters)
            self.metrics.event("stage:QUARANTINE", now, v.target)
            epoch = v.epoch
            self._schedule(now, v.target, f"validated:{v.target}:{epoch}",
                           lambda: self.backend.read_state(w).get("phase") == REINTEGRATING
                           and int(self.backend.read_state(w).get("epoch", -1)) >= epoch,
                           lambda: self.backend.write_state(w, {"phase": REINTEGRATING, "stage": "QUARANTINE"}),
                           f"mark {w} validated")

        elif (v.action == Action.ADVANCE_STAGE and v.epoch == st.epoch and st.phase == REINTEGRATING
              and v.stage == next_stage(st.stage)):
            st.stage, st.stage_entered = v.stage, now
            self.metrics.event(f"stage:{v.stage}", now, v.target)
            if v.stage == "FULL":
                st.phase = HEALTHY
                self.metrics.event("reintegrated", now, v.target)
            self._record_decision(c, now, f"advance to {v.stage}")
            ann, stage, phase = self._qc_annotations(c), v.stage, st.phase

            def expected_stage():
                # Monotonic: a fail-over executor must never roll a target back
                # to an older stage that has since been superseded.
                if self.states[v.target].stage != stage:
                    return True
                cur = self.backend.current_stage(w)
                cur_i = STAGES.index(cur) if cur in STAGES else STAGES.index("FULL")
                return cur_i >= STAGES.index(stage)

            def apply():
                self.backend.apply_stage(w, stage, ann)
                self.backend.write_state(w, {"phase": phase, "stage": stage})

            self._schedule(now, v.target, f"stage:{v.target}:{v.epoch}:{stage}", expected_stage, apply,
                           f"move {w} to {stage}")
        else:
            log.info("ignoring stale/out-of-order commit %s (local phase %s epoch %d)",
                     c.action_key, st.phase, st.epoch)

    # ------------------------------------------------------------------ executor
    def executor_rank(self) -> int:
        """Deterministic, leaderless fail-over: agents ordered A<B<C<D (skipping
        peers this agent no longer trusts). Rank r acts after r*stagger seconds
        if nobody before it has produced the effect."""
        order = [n for n in sorted(self.cfg.nodes) if self.eligible_voter(n)]
        return order.index(self.id) if self.id in order else len(order)

    def _schedule(self, now: float, target: str, key: str, effect_present, run, desc: str) -> None:
        due = now + self.executor_rank() * self.cfg.timers.executor_stagger_s
        self.tasks.append(_Task(due, key, target, self.states[target].epoch, effect_present, run, desc))

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
                    continue
                log.info("EXECUTOR %s: %s", self.id, t.desc)
                t.run()
            except Exception as exc:
                log.error("executor task %s failed: %s (retrying)", t.key, exc)
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
                if st.phase == RECOVERING and self.backend.recovery_done(w, st.epoch):
                    st.phase, st.phase_entered = VALIDATING, now
                    self.metrics.event("recovered", now, nid)
                if st.phase == VALIDATING and now - st.phase_entered > self.cfg.timers.validate_timeout_s:
                    log.warning("validation of %s stalled; re-running recovery", w)
                    st.phase_entered = now
                    self._schedule(now, nid, f"re-recover:{nid}:{st.epoch}:{int(now)}",
                                   lambda: False, lambda w=w, e=st.epoch: self.backend.recover(w, e),
                                   f"re-redeploy {w}")
            except Exception as exc:
                log.debug("cluster tracking for %s failed: %s", w, exc)

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
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
            }

    def metrics_summary(self) -> List[dict]:
        return self.metrics.summary(fetch_availability(self.cfg.client_stats_url))
