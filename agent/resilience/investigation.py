"""Targeted investigation before containment (docs/INVESTIGATION.md).

When the evidence about a workload is borderline, or the agents disagree, nothing
used to happen until the evidence window expired. An *investigation* is a short,
bounded step carried out by the same four peer agents:

  trigger  -> every agent re-measures ONLY the disputed signals at a higher rate
              -> agents ask each other for fresh SIGNED observations over gRPC/mTLS
              -> after the time budget each agent reaches one of four outcomes

  CORROBORATED   >= 3 agents independently still see it -> the normal CONTAIN quorum
                 proceeds (and a weak-but-persistent signal may now be voted on)
  FALSE_POSITIVE >= 3 agents see it gone, < 2 still see it -> closed, no action
  AMBIGUOUS      anything in between -> reversible WATCH state + human-review flag
  UNCERTAIN      too few agents could re-measure -> safe policy (WATCH), never an attack
                 verdict and never an all-clear

Missing data (a silent peer, unreachable telemetry, a timeout) is recorded as
*unknown*. It is never counted as "attack" and never as "safe".

Nothing here isolates anything by itself: isolation still needs a signed 3-of-4 quorum.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import observability as obs
from .config import InvestigationParams, QuorumParams
from .detection import Detector
from .evidence import (Evidence, InvestigateRequest, InvestigateResponse, ObsType,
                       SignalReading, open_envelope, seal)
from .proto import resilience_pb2 as pb

log = logging.getLogger(__name__)

CORROBORATED = "CORROBORATED"
FALSE_POSITIVE = "FALSE_POSITIVE"
AMBIGUOUS = "AMBIGUOUS"
UNCERTAIN = "UNCERTAIN"
SUPERSEDED = "SUPERSEDED"      # the incident / workload changed underneath it
MERGED = "MERGED"              # a duplicate that yielded to a lower-id investigation

TRIGGERS = ("uncertain_band", "split_view", "single_sender")

# per-agent view of a target after an investigation
PERSIST, GONE, FLAPPING, UNKNOWN = "persist", "gone", "flapping", "unknown"


# --------------------------------------------------------------------------- pure logic
def classify(readings: List[SignalReading], signals, p: InvestigationParams) -> str:
    """One agent's verdict on the disputed signals, from ITS OWN re-measurements.

    persist   at least one disputed signal still shows the anomaly in nearly all of the
              most recent samples (the 'tail' of the window)
    gone      every disputed signal shows the anomaly in none of the recent samples
    flapping  neither (comes and goes)
    unknown   too few valid samples to say (telemetry down, agent just started, ...)
    """
    by = {r.observation: r for r in readings}
    usable = [by[s] for s in signals if s in by and by[s].tail_samples >= p.min_tail_samples]
    if not usable:
        return UNKNOWN
    if any(r.tail_positive / r.tail_samples >= p.persist_ratio for r in usable):
        return PERSIST
    if len(usable) == len(list(signals)) and all(r.tail_positive == 0 for r in usable):
        return GONE
    return FLAPPING


def decide(views: Dict[str, str], q: QuorumParams) -> Tuple[str, str]:
    """views: agent -> persist|gone|flapping|unknown for every agent whose answer is
    valid and counted. Agents that did not answer (or are vote-excluded) are simply
    absent. Returns (outcome, plain-English reason)."""
    valid = {n: v for n, v in views.items() if v != UNKNOWN}
    persist = sorted(n for n, v in valid.items() if v == PERSIST)
    gone = sorted(n for n, v in valid.items() if v == GONE)
    flap = sorted(n for n, v in valid.items() if v == FLAPPING)
    detail = (f"still see it: {', '.join(persist) or 'nobody'}; see it gone: "
              f"{', '.join(gone) or 'nobody'}"
              + (f"; flapping: {', '.join(flap)}" if flap else "")
              + f"; no usable answer: {', '.join(sorted(set(q_all(q)) - set(valid))) or 'nobody'}")
    if len(valid) < q.quorum:
        return UNCERTAIN, (f"only {len(valid)} of {q.n} agents could re-measure (need {q.quorum}); "
                           f"missing data is neither proof of attack nor of safety. {detail}")
    if len(persist) >= q.quorum:
        return CORROBORATED, f"{len(persist)} agents independently still see it. {detail}"
    if len(gone) >= q.quorum and len(persist) < q.f + 1:
        return FALSE_POSITIVE, f"the anomaly is gone for {len(gone)} agents. {detail}"
    return AMBIGUOUS, f"the agents do not agree and it is not confirmed. {detail}"


def q_all(q: QuorumParams) -> List[str]:
    return [chr(ord("A") + i) for i in range(q.n)]


def make_readings(samples: List["Sample"], signals, p: InvestigationParams,
                  positive_conf: float) -> Tuple[SignalReading, ...]:
    ok = [s for s in samples if s.ok]
    out = []
    for sig in signals:
        confs = [s.conf.get(sig, 0.0) for s in ok]
        if not confs:
            out.append(SignalReading(sig))
            continue
        n_tail = min(len(confs), max(p.min_tail_samples, math.ceil(p.tail_fraction * len(confs))))
        tail = confs[-n_tail:]
        last_summary = next((s.summary.get(sig, "") for s in reversed(ok)), "")
        out.append(SignalReading(
            observation=sig, last_confidence=confs[-1], max_confidence=max(confs),
            samples=len(confs), positive_samples=sum(c >= positive_conf for c in confs),
            tail_samples=len(tail), tail_positive=sum(c >= positive_conf for c in tail),
            summary=last_summary))
    return tuple(out)


# --------------------------------------------------------------------------- data
@dataclass
class Sample:
    t: float
    ok: bool
    conf: Dict[ObsType, float] = field(default_factory=dict)
    summary: Dict[ObsType, str] = field(default_factory=dict)
    instance_id: str = ""


@dataclass
class Authorization:
    """Permission, earned by a CORROBORATED investigation, to vote CONTAIN on a weak but
    persistent signal. Bound to one incident version and one workload instance, and it
    expires: a stale result can never authorise containment of something that changed."""
    investigation_id: str
    target: str
    epoch: int
    instance_id: str
    expires: float
    own_persistent: bool
    seers: List[str]


@dataclass
class Watch:
    target: str
    epoch: int
    since: float
    outcome: str
    reason: str
    investigation_id: str
    review_needed: bool = True


class Investigation:
    def __init__(self, inv_id: str, target: str, epoch: int, signals: Tuple[ObsType, ...],
                 trigger: str, initiator: str, started_at: float, budget_s: float,
                 instance_id: str = ""):
        self.id, self.target, self.epoch = inv_id, target, epoch
        self.signals, self.trigger, self.initiator = tuple(signals), trigger, initiator
        self.started_at, self.budget_s = started_at, budget_s
        self.deadline = started_at + budget_s
        self.instance_id = instance_id
        self.instance_changed = False
        self.state = "ACTIVE"
        self.outcome: Optional[str] = None
        self.reason = ""
        self.closed_at: Optional[float] = None
        self.lock = threading.Lock()
        self.samples: List[Sample] = []
        self.responses: Dict[str, InvestigateResponse] = {}
        self.digests: Dict[str, str] = {}          # responder -> short hash of the signed answer
        self.silent: Dict[str, float] = {}         # peer -> last time a request got no answer
        self.refused: Dict[str, str] = {}
        self.invalid: Dict[str, int] = {}
        self.late_or_duplicate = 0
        self.excluded: List[str] = []
        self.next_poll = started_at
        self.final_sent = False
        self.round = 0
        self.last_round_at = started_at
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.evidence_added: set = set()
        self.views: Dict[str, str] = {}

    @property
    def active(self) -> bool:
        return self.state == "ACTIVE"


# --------------------------------------------------------------------------- manager
class InvestigationManager:
    """Per-agent investigation logic. One instance per ResilienceAgent."""

    def __init__(self, agent):
        self.a = agent
        self.p: InvestigationParams = agent.cfg.investigation
        self.lock = threading.RLock()
        self.active: Dict[str, Investigation] = {}       # target -> running investigation
        self.recent: List[dict] = []                     # finished, newest last
        self.cooldown_until: Dict[str, float] = {}
        self.trigger_since: Dict[str, Tuple[str, float]] = {}
        self.auth: Dict[str, Authorization] = {}
        self.watch: Dict[str, Watch] = {}
        self._seen_requests: Dict[str, float] = {}
        self.finished: Dict[str, pb.SignedEnvelope] = {}   # investigation id -> signed FINAL answer
        self.detector = Detector(agent.cfg.baseline_hashes, agent.cfg.process_allowlist)
        self.counters = {"started": 0, "adopted": 0, "merged": 0, "refused_busy": 0,
                         "refused_cooldown": 0, "refused_stale": 0, "invalid_responses": 0}

    # ---- small helpers
    @property
    def enabled(self) -> bool:
        return bool(self.p.enabled)

    @property
    def id(self) -> str:
        return self.a.id

    def _now(self) -> float:
        return self.a.clock()

    def _emit(self, summary: str, target: Optional[str], details: dict, **kw):
        return self.a.events.emit(obs.INVESTIGATION, summary, target, details, **kw)

    def _workload(self, target: str) -> str:
        return self.a.workload(target)

    def _rank(self) -> int:
        return sorted(self.a.cfg.nodes).index(self.id)

    def _new_id(self, now: float, target: str) -> str:
        # time first, so that "lowest id wins" means "started first"; node + target break ties
        return f"inv-{int(now * 1000):014d}-{self.id}-{target}"

    def reset(self) -> None:
        """Forget everything (models an agent restart: investigations live in memory only)."""
        with self.lock:
            for inv in self.active.values():
                inv.stop.set()
            self.active.clear()
            self.cooldown_until.clear()
            self.trigger_since.clear()
            self.auth.clear()
            self.watch.clear()
            self._seen_requests.clear()
            self.finished.clear()

    # ---- hooks used by the agent --------------------------------------------------
    def sensitivity_overrides(self) -> Dict[str, float]:
        if not self.enabled:
            return {}
        with self.lock:
            return {t: self.p.watch_sensitivity for t in self.watch}

    def on_incident_change(self, target: str, now: float) -> None:
        """The target's incident version/phase changed (e.g. a CONTAIN quorum committed):
        every investigation, authorisation and watch for it is now stale."""
        with self.lock:
            inv = self.active.get(target)
            if inv is not None:
                self._close(inv, now, SUPERSEDED, "the incident was resolved by a quorum first")
            if self.auth.pop(target, None) is not None:
                self._emit(f"Agent {self.id} discarded the investigation authorisation for "
                           f"{self._workload(target)}: the incident moved on.", target,
                           {"event": "authorization_discarded", "reason": "incident changed"})
            w = self.watch.pop(target, None)
            if w is not None:
                self._emit(f"Agent {self.id} ended the watch on {self._workload(target)}: it is now "
                           f"handled as a normal incident.", target,
                           {"event": "watch_ended", "reason": "incident handled"})
            self.trigger_since.pop(target, None)

    def vote_authorization(self, target: str, now: float) -> Optional[Authorization]:
        """A still-valid authorisation for this target, else None (stale ones are dropped)."""
        if not self.enabled:
            return None
        with self.lock:
            au = self.auth.get(target)
            if au is None:
                return None
            st = self.a.states[target]
            snap = self.a.snaps.get(target)
            cur_inst = snap.instance_id if snap else ""
            why = None
            if now >= au.expires:
                why = "it expired"
            elif au.epoch != st.epoch or st.phase != "HEALTHY":
                why = "the incident version changed"
            elif au.instance_id and cur_inst and au.instance_id != cur_inst:
                why = "the workload was replaced"
            if why:
                del self.auth[target]
                self._emit(f"Agent {self.id} discarded the investigation authorisation for "
                           f"{self._workload(target)}: {why}.", target,
                           {"event": "authorization_discarded", "reason": why,
                            "investigation": au.investigation_id})
                return None
            return au

    # ---- triggers -----------------------------------------------------------------
    def evaluate(self, now: float, target: str, score) -> None:
        """Called once per tick for a HEALTHY target. Starts an investigation when the
        situation has been uncertain for `trigger_grace_s`."""
        if not self.enabled:
            return
        q, p = self.a.cfg.quorum, self.p
        with self.lock:
            if target in self.active or now < self.cooldown_until.get(target, 0.0):
                return
            strong = self.a.pool.recent(now, p.recent_s, target=target, min_conf=q.local_min_conf)
            senders = sorted({e.origin for e in strong})
            kind: Optional[str] = None
            if senders:
                n = len(senders)
                if n >= q.quorum:
                    if p.band_lo <= score.total < q.score_threshold:
                        kind = "uncertain_band"       # widely seen but weak
                elif n == 1:
                    kind = "single_sender"
                else:
                    kind = "split_view"
            if kind is None:
                self.trigger_since.pop(target, None)
                return
            prev = self.trigger_since.get(target)
            if prev is None or prev[0] != kind:
                self.trigger_since[target] = (kind, now)
                return
            if now - prev[1] < p.trigger_grace_s + self._rank() * p.trigger_stagger_s:
                return
            if len(self.active) >= p.max_concurrent:
                self.counters["refused_busy"] += 1
                return
            signals = tuple(sorted({e.observation for e in strong}, key=lambda s: s.value))
            self._start(now, target, kind, signals, senders, score.total)

    def _trigger_text(self, kind: str, senders: List[str], w: float) -> str:
        q = self.a.cfg.quorum
        if kind == "uncertain_band":
            return (f"the combined evidence score W={w:.2f} is in the uncertain zone "
                    f"({self.p.band_lo:g} to {q.score_threshold:g}): too weak to isolate, too strong to ignore")
        if kind == "split_view":
            return f"agents {', '.join(senders)} report an anomaly but the other agents do not"
        return f"only agent {senders[0] if senders else '?'} reports an anomaly and nobody has corroborated it"

    def _start(self, now: float, target: str, kind: str, signals, senders, w: float) -> Investigation:
        st = self.a.states[target]
        snap = self.a.snaps.get(target)
        inv = Investigation(self._new_id(now, target), target, st.epoch, signals, kind, self.id,
                            now, self.p.budget_s, snap.instance_id if snap else "")
        self.active[target] = inv
        self.counters["started"] += 1
        self.trigger_since.pop(target, None)
        text = self._trigger_text(kind, senders, w)
        self._emit(f"Agent {self.id} opened an investigation of {self._workload(target)} ({target}): "
                   f"{text}. Question: is the {', '.join(s.value.lower() for s in signals)} anomaly still "
                   f"there, and do the other agents see it independently? Time budget "
                   f"{self.p.budget_s:g}s.", target,
                   {"event": "started", "investigation": inv.id, "trigger": kind, "trigger_text": text,
                    "signals": [s.value for s in signals], "epoch": inv.epoch, "budget_s": inv.budget_s,
                    "deadline": inv.deadline, "initiator": self.id, "w": round(w, 3)})
        self.a.metrics.event("investigation_started", now, target, investigation=inv.id, trigger=kind)
        self._launch(inv)
        return inv

    def _launch(self, inv: Investigation) -> None:
        inv.thread = threading.Thread(target=self._sample_loop, args=(inv,), daemon=True,
                                      name=f"inv-{self.id}-{inv.target}")
        inv.thread.start()
        self._send_round(inv, self._now())

    # ---- sampling (own re-measurements, higher rate, only the disputed signals) ----
    def _sample_loop(self, inv: Investigation) -> None:
        p = self.p
        while not inv.stop.is_set() and inv.active and len(inv.samples) < p.max_samples \
                and self._now() < inv.deadline:
            self.take_sample(inv)
            inv.stop.wait(p.sample_interval_s)

    def take_sample(self, inv: Investigation) -> Sample:
        now = self._now()
        sample = Sample(t=now, ok=False)
        try:
            tel = self.a.telemetry
            if ObsType.AUTH in inv.signals:
                snaps = tel.collect()               # AUTH counts failures reported by other workloads
            else:
                snaps = {inv.target: tel.collect_target(inv.target)}
            s = snaps.get(inv.target)
            if s is not None and s.reachable:
                sample.ok, sample.instance_id = True, s.instance_id
                for sig, (conf, summ) in self.detector.measure(inv.target, snaps, now, inv.signals).items():
                    sample.conf[sig], sample.summary[sig] = conf, summ
        except Exception as exc:                     # unreachable / error: UNKNOWN, not normal
            log.debug("investigation sample failed: %s", exc)
        comp = getattr(self.a, "_comp", None)
        if comp is not None and comp.active and comp.target == inv.target:
            # the simulated compromised agent keeps lying while "investigating"
            sample.ok = True
            for sig in inv.signals:
                if sig.value in comp.types:
                    sample.conf[sig], sample.summary[sig] = comp.confidence, "[fabricated] anomaly persists"
        with inv.lock:
            inv.samples.append(sample)
            if sample.ok and inv.instance_id and sample.instance_id and sample.instance_id != inv.instance_id:
                inv.instance_changed = True
        return sample

    def _own_readings(self, inv: Investigation) -> Tuple[Tuple[SignalReading, ...], int, str]:
        with inv.lock:
            samples = list(inv.samples)
        readings = make_readings(samples, inv.signals, self.p, self.a.cfg.quorum.local_min_conf)
        failures = sum(1 for s in samples if not s.ok)
        inst = next((s.instance_id for s in reversed(samples) if s.ok), "")
        return readings, failures, inst

    def _own_response(self, inv: Investigation, status: str = "ok") -> pb.SignedEnvelope:
        readings, failures, inst = self._own_readings(inv)
        resp = InvestigateResponse(responder=self.id, investigation_id=inv.id, target=inv.target,
                                   epoch=inv.epoch, readings=readings, telemetry_failures=failures,
                                   instance_id=inst, timestamp=self._now(), status=status)
        return seal(self.a.signer, resp)

    # ---- asking peers for fresh signed observations -------------------------------
    def _send_round(self, inv: Investigation, now: float) -> None:
        tr = self.a.transport
        inv.round += 1
        inv.last_round_at = now
        if tr is None or not hasattr(tr, "investigate_async"):
            return
        req = InvestigateRequest(requester=self.id, investigation_id=inv.id, target=inv.target,
                                 epoch=inv.epoch, signals=inv.signals, started_at=inv.started_at,
                                 budget_s=inv.budget_s, trigger=inv.trigger, timestamp=now)
        env = seal(self.a.signer, req)
        for nid in self.a.cfg.nodes:
            if nid == self.id:
                continue
            tr.investigate_async(nid, env, lambda n, res, i=inv: self._on_result(i, n, res),
                                 self.p.request_timeout_s)

    def _on_result(self, inv: Investigation, nid: str, res) -> None:
        """A peer's answer (or silence) to our request. Safe to call twice with the same data."""
        try:
            now = self._now()
            if res is None:                                  # no answer: unknown, not "normal"
                with inv.lock:
                    inv.silent[nid] = now
                return
            if not res.accepted:
                with inv.lock:
                    inv.refused[nid] = res.reason
                return
            claim, why = open_envelope(res.response, self.a.registry, transport_identity=nid, now=now,
                                       max_skew_s=self.a.cfg.timers.max_clock_skew_s, replay=None)
            if not isinstance(claim, InvestigateResponse):
                self._invalid_response(inv, nid, res.response, why if claim is None else "wrong message kind")
                return
            if claim.target != inv.target or claim.epoch != inv.epoch:
                with inv.lock:
                    inv.refused[nid] = "answer is for a different incident version"
                return
            if claim.investigation_id != inv.id:
                if claim.investigation_id < inv.id:          # a duplicate that started earlier wins
                    with self.lock:
                        if self.active.get(inv.target) is inv:
                            self.counters["merged"] += 1
                            self._close(inv, now, MERGED, f"yielded to the earlier investigation "
                                        f"{claim.investigation_id}")
                else:
                    with inv.lock:
                        inv.late_or_duplicate += 1
                return
            with inv.lock:
                if not inv.active:                           # late: the investigation already closed
                    inv.late_or_duplicate += 1
                    return
                prev = inv.responses.get(nid)
                if prev is not None and claim.timestamp <= prev.timestamp:
                    inv.late_or_duplicate += 1               # duplicate / out-of-order: idempotent
                    return
                inv.responses[nid] = claim
                inv.digests[nid] = hashlib.sha256(res.response.payload + res.response.signature).hexdigest()[:16]
                inv.silent.pop(nid, None)
                inv.refused.pop(nid, None)
            self._record_as_evidence(inv, claim)
            self._emit(f"Agent {self.id} received agent {nid}'s signed re-measurement for the "
                       f"investigation of {self._workload(inv.target)}: "
                       f"{self._readings_text(claim.readings)}.", inv.target,
                       {"event": "peer_response", "investigation": inv.id, "peer": nid,
                        "readings": [r.to_dict() for r in claim.readings],
                        "signed_digest": inv.digests[nid]},
                       key=("invresp", inv.id, nid), every=3.0)
        except Exception:
            log.exception("investigation result handling failed")

    def _invalid_response(self, inv: Investigation, nid: str, env, why: str) -> None:
        with inv.lock:
            inv.invalid[nid] = inv.invalid.get(nid, 0) + 1
        self.counters["invalid_responses"] += 1
        self.a._reject_envelope(env, nid, why, self._now())

    def _record_as_evidence(self, inv: Investigation, resp: InvestigateResponse) -> None:
        """Positive readings join the normal evidence pool, signed like all other evidence."""
        q = self.a.cfg.quorum
        for r in resp.readings:
            if r.last_confidence >= q.evidence_min_conf and r.tail_positive > 0:
                eid = f"{resp.response_id}:{r.observation.value}"
                if eid in inv.evidence_added:
                    continue
                inv.evidence_added.add(eid)
                self.a.pool.add(Evidence(
                    origin=resp.responder, target=resp.target, observation=r.observation,
                    confidence=r.last_confidence, timestamp=resp.timestamp,
                    summary=f"[investigation {inv.id[-12:]}] {r.summary}", evidence_id=eid))

    @staticmethod
    def _readings_text(readings) -> str:
        bits = []
        for r in readings:
            if r.tail_samples:
                bits.append(f"{r.observation.value.lower()}: anomaly in {r.tail_positive} of its last "
                            f"{r.tail_samples} samples")
            else:
                bits.append(f"{r.observation.value.lower()}: no usable samples")
        return "; ".join(bits) or "no readings"

    # ---- answering a peer's request -----------------------------------------------
    def on_request(self, env: pb.SignedEnvelope, identity: Optional[str]
                   ) -> Tuple[bool, str, Optional[pb.SignedEnvelope]]:
        a, p = self.a, self.p
        if not self.enabled:
            return False, "investigation is disabled on this agent", None
        now = self._now()
        claim, why = open_envelope(env, a.registry, transport_identity=identity, now=now,
                                   max_skew_s=a.cfg.timers.max_clock_skew_s, replay=None)
        if not isinstance(claim, InvestigateRequest):
            a._reject_envelope(env, identity, why if claim is None else "wrong message kind", now)
            return False, why if claim is None else "wrong message kind", None
        if claim.requester == self.id or claim.target not in a.states or not claim.signals:
            return False, "malformed request", None
        if not a.eligible_voter(claim.requester):
            return False, "requester is vote-excluded", None
        with self.lock:
            done = self.finished.get(claim.investigation_id)
        if done is not None:                     # we already finished this one: repeat our final answer
            return True, "ok (final)", done
        st = a.states[claim.target]
        if st.epoch != claim.epoch:
            self.counters["refused_stale"] += 1
            return False, f"stale: incident version {claim.epoch} != {st.epoch}", None
        if st.phase != "HEALTHY":
            self.counters["refused_stale"] += 1
            return False, f"target is {st.phase}", None
        with self.lock:
            self._seen_requests[claim.request_id] = now
            if len(self._seen_requests) > 2000:
                self._seen_requests = {k: t for k, t in self._seen_requests.items() if now - t < 60}
            inv = self.active.get(claim.target)
            if inv is not None and inv.id == claim.investigation_id:
                return True, "ok", self._own_response(inv)             # repeat request: idempotent
            if inv is not None:
                if claim.investigation_id < inv.id:                    # the earlier one wins
                    self.counters["merged"] += 1
                    self._close(inv, now, MERGED, f"yielded to the earlier investigation "
                                f"{claim.investigation_id}")
                else:                                                  # ours is earlier: tell them
                    return True, "duplicate: yielding to ours", self._own_response(inv)
            if len(self.active) >= p.max_concurrent:
                self.counters["refused_busy"] += 1
                return False, "busy: too many investigations running", None
            if now < self.cooldown_until.get(claim.target, 0.0):
                self.counters["refused_cooldown"] += 1
                return False, "cooldown: this target was investigated recently", None
            snap = a.snaps.get(claim.target)
            parts = claim.investigation_id.split("-")        # inv-<ms>-<initiator node>-<target>
            initiator = parts[2] if len(parts) >= 4 and parts[2] in a.cfg.nodes else claim.requester
            inv = Investigation(claim.investigation_id, claim.target, claim.epoch, claim.signals,
                                claim.trigger, initiator, now, min(claim.budget_s, p.budget_s),
                                snap.instance_id if snap else "")
            self.active[claim.target] = inv
            self.counters["adopted"] += 1
            self._emit(f"Agent {self.id} joined agent {initiator}'s investigation of "
                       f"{self._workload(claim.target)} ({claim.target}): re-measuring "
                       f"{', '.join(s.value.lower() for s in claim.signals)} at a higher rate.",
                       claim.target, {"event": "adopted", "investigation": inv.id,
                                      "initiator": initiator, "signals": [s.value for s in claim.signals],
                                      "epoch": inv.epoch, "deadline": inv.deadline, "trigger": claim.trigger})
            a.metrics.event("investigation_started", now, claim.target, investigation=inv.id,
                            trigger=claim.trigger)
            inv.thread = threading.Thread(target=self._sample_loop, args=(inv,), daemon=True)
            inv.thread.start()
        self.take_sample(inv)                                          # a fresh reading right now
        self._send_round(inv, now)
        return True, "ok", self._own_response(inv)

    # ---- progress, early close, deadline -----------------------------------------
    def tick(self, now: float) -> None:
        if not self.enabled:
            return
        p = self.p
        with self.lock:
            for target, inv in list(self.active.items()):
                st = self.a.states[target]
                if st.epoch != inv.epoch or st.phase != "HEALTHY":
                    self._close(inv, now, SUPERSEDED, "the incident changed underneath it")
                    continue
                if inv.instance_changed:
                    self._close(inv, now, SUPERSEDED, "the workload was replaced during the investigation")
                    continue
                final_lead = 1.5 * self.a.cfg.timers.tick_s
                if now >= inv.next_poll and now < inv.deadline - final_lead:
                    inv.next_poll = now + p.peer_poll_s
                    self._send_round(inv, now)
                elif not inv.final_sent and now >= inv.deadline - final_lead:
                    inv.final_sent = True
                    self._send_round(inv, now)
                if now >= inv.deadline:
                    self._close(inv, now, None)
                elif p.early_close and now - inv.started_at >= 0.4 * inv.budget_s \
                        and self._everyone_answered(inv, now):
                    outcome, _ = self._conclude(inv)
                    if outcome in (CORROBORATED, FALSE_POSITIVE):
                        self._close(inv, now, None)
            for target, w in list(self.watch.items()):
                self._maybe_clear_watch(target, w, now)

    def _everyone_answered(self, inv: Investigation, now: float) -> bool:
        peers = [n for n in self.a.cfg.nodes if n != self.id and self.a.eligible_voter(n)]
        with inv.lock:
            return bool(peers) and all(
                n in inv.responses and (inv.responses[n].status == "final"
                                        or inv.responses[n].timestamp >= inv.last_round_at - 0.01)
                for n in peers) and not inv.silent

    # ---- conclusion ---------------------------------------------------------------
    def _views(self, inv: Investigation) -> Tuple[Dict[str, str], List[str]]:
        views: Dict[str, str] = {}
        excluded: List[str] = []
        own, failures, _ = self._own_readings(inv)
        views[self.id] = classify(list(own), inv.signals, self.p)
        with inv.lock:
            responses = dict(inv.responses)
        for nid, resp in responses.items():
            if not self.a.eligible_voter(nid):               # became vote-excluded mid-investigation
                excluded.append(nid)
                continue
            views[nid] = classify(list(resp.readings), inv.signals, self.p) \
                if resp.status in ("ok", "final") else UNKNOWN
        return views, sorted(excluded)

    def _conclude(self, inv: Investigation) -> Tuple[str, str]:
        views, excluded = self._views(inv)
        inv.views, inv.excluded = views, excluded
        outcome, reason = decide(views, self.a.cfg.quorum)
        if excluded:
            reason += f" (answers from vote-excluded agents ignored: {', '.join(excluded)})"
        return outcome, reason

    def _close(self, inv: Investigation, now: float, outcome: Optional[str], reason: str = "") -> None:
        with self.lock:
            if not inv.active:
                return
            if outcome is None:
                outcome, reason = self._conclude(inv)
            inv.state, inv.outcome, inv.reason, inv.closed_at = "CLOSED", outcome, reason, now
            inv.stop.set()
            if self.active.get(inv.target) is inv:
                del self.active[inv.target]
            if outcome != MERGED:           # a merged duplicate must not block adopting the winner
                self.cooldown_until[inv.target] = now + self.p.cooldown_s
            self.trigger_since.pop(inv.target, None)
            if outcome not in (MERGED, SUPERSEDED):
                self.finished[inv.id] = self._own_response(inv, status="final")
                if len(self.finished) > 20:
                    self.finished.pop(next(iter(self.finished)))
            self._apply(inv, now)
            rec = self.summary(inv, now)
            self.recent.append(rec)
            self.recent = self.recent[-30:]
        self.a.metrics.event("investigation_closed", now, inv.target, investigation=inv.id,
                             outcome=outcome, duration_s=round(now - inv.started_at, 3))

    def _apply(self, inv: Investigation, now: float) -> None:
        a, p = self.a, self.p
        t, w = inv.target, self._workload(inv.target)
        o = inv.outcome
        sentence = {
            CORROBORATED: (f"Investigation of {w} ({t}) CONFIRMED: {inv.reason}. "
                           + ("The normal CONTAIN quorum may now proceed." if p.on_corroborated == "contain"
                              else "Policy is set to take no action.")),
            FALSE_POSITIVE: f"Investigation of {w} ({t}) closed as a FALSE POSITIVE, no action taken: {inv.reason}.",
            AMBIGUOUS: (f"Investigation of {w} ({t}) is AMBIGUOUS: {inv.reason}. "
                        + ("Placed under watch (heightened monitoring) and flagged for human review; "
                           "nothing was isolated." if p.on_uncertain == "watch" else "No action taken.")),
            UNCERTAIN: (f"Investigation of {w} ({t}) is UNCERTAIN: {inv.reason}. "
                        + ("Safe policy: placed under watch and flagged for human review; nothing was isolated."
                           if p.on_uncertain == "watch" else "Safe policy: no action taken.")),
            SUPERSEDED: f"Investigation of {w} ({t}) was superseded and its result discarded: {inv.reason}.",
            MERGED: f"Investigation {inv.id[-24:]} of {w} ({t}) merged into an earlier one: {inv.reason}.",
        }[o]
        self._emit(f"Agent {self.id}: {sentence}", t,
                   {"event": "closed", "investigation": inv.id, "outcome": o, "reason": inv.reason,
                    "duration_s": round(now - inv.started_at, 2), "views": dict(inv.views)})
        if o == CORROBORATED and p.on_corroborated == "contain":
            own = inv.views.get(self.id) == PERSIST
            seers = sorted(n for n, v in inv.views.items() if v == PERSIST)
            self.auth[t] = Authorization(inv.id, t, inv.epoch, inv.instance_id, now + p.authorization_ttl_s,
                                         own, seers)
            self.watch.pop(t, None)
        elif o == FALSE_POSITIVE:
            a.votes.forget_target(t)                 # drop the uncommitted CONTAIN votes it left behind
            a.pool.clear_target(t)
            if self.watch.pop(t, None) is not None:
                self._emit(f"Agent {self.id} ended the watch on {w}: the anomaly is gone.", t,
                           {"event": "watch_ended", "reason": "false positive"})
        elif o in (AMBIGUOUS, UNCERTAIN) and p.on_uncertain == "watch":
            self.watch[t] = Watch(t, inv.epoch, now, o, inv.reason, inv.id, review_needed=True)
            self._emit(f"HUMAN REVIEW NEEDED: {w} ({t}) is under watch after an {o.lower()} investigation. "
                       f"Nothing was isolated; heightened monitoring is on.", t,
                       {"event": "review_flag", "investigation": inv.id, "outcome": o})

    def _maybe_clear_watch(self, target: str, w: Watch, now: float) -> None:
        st = self.a.states[target]
        if st.epoch != w.epoch or st.phase != "HEALTHY":
            del self.watch[target]
            return
        quiet_for = now - max(w.since, self._last_anomaly(target, now))
        if quiet_for >= self.p.watch_clear_s:
            del self.watch[target]
            self._emit(f"Agent {self.id} cleared the watch on {self._workload(target)} ({target}): no "
                       f"anomaly for {self.p.watch_clear_s:g}s. The review flag is lifted.", target,
                       {"event": "watch_ended", "reason": "quiet", "investigation": w.investigation_id})

    def _last_anomaly(self, target: str, now: float) -> float:
        hist = self.a.local_hist.get(target, ())
        last = 0.0
        for t, c in hist:
            if c >= self.a.cfg.quorum.evidence_min_conf:
                last = max(last, t)
        return last

    # ---- reporting ----------------------------------------------------------------
    def summary(self, inv: Investigation, now: float) -> dict:
        own, failures, _ = self._own_readings(inv)
        with inv.lock:
            peers = {n: {"readings": [r.to_dict() for r in r_.readings], "view": inv.views.get(n),
                         "age_s": round(now - r_.timestamp, 2), "signed_digest": inv.digests.get(n)}
                     for n, r_ in inv.responses.items()}
            silent, refused = sorted(inv.silent), dict(inv.refused)
            invalid, late = dict(inv.invalid), inv.late_or_duplicate
            n_samples = len(inv.samples)
        return {
            "id": inv.id, "target": inv.target, "workload": self._workload(inv.target), "epoch": inv.epoch,
            "state": inv.state, "outcome": inv.outcome, "reason": inv.reason, "trigger": inv.trigger,
            "signals": [s.value for s in inv.signals], "initiator": inv.initiator,
            "started_at": inv.started_at, "deadline": inv.deadline, "closed_at": inv.closed_at,
            "time_left_s": round(max(0.0, inv.deadline - now), 1) if inv.active else 0.0,
            "question": (f"Is the {', '.join(s.value.lower() for s in inv.signals)} anomaly on "
                         f"{self._workload(inv.target)} still present, and do other agents see it independently?"),
            "own": {"view": inv.views.get(self.id), "samples": n_samples, "telemetry_failures": failures,
                    "readings": [r.to_dict() for r in own]},
            "peers": peers, "silent": silent, "refused": refused, "invalid": invalid,
            "late_or_duplicate": late, "excluded": list(inv.excluded), "views": dict(inv.views),
        }

    def status(self) -> dict:
        now = self._now()
        with self.lock:
            return {
                "enabled": self.enabled,
                "params": self.p.to_dict(),
                "active": [self.summary(i, now) for i in self.active.values()],
                "recent": list(self.recent[-10:]),
                "watch": {t: {"since": w.since, "outcome": w.outcome, "reason": w.reason,
                              "investigation": w.investigation_id, "review_needed": w.review_needed,
                              "workload": self._workload(t)} for t, w in self.watch.items()},
                "authorizations": {t: {"investigation": au.investigation_id, "expires_in_s":
                                       round(max(0.0, au.expires - now), 1), "seers": au.seers}
                                   for t, au in self.auth.items()},
                "counters": dict(self.counters),
            }
