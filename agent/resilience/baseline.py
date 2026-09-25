"""Centralized-controller baseline (spec section 9).

One controller does detection -> isolation -> recovery -> validation ->
(binary) reintegration by itself. It uses the *same* monitoring, detection,
actuators and metrics as the distributed agents so the comparison is fair;
the only differences are:

* no evidence corroboration, no quorum: one detection >= threshold isolates;
* no trust scores: reintegration is all-or-nothing once validation passes;
* a single point of compromise: if the controller is compromised (same
  compromise file as the agents) it isolates a healthy node immediately.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

from .config import ClusterConfig
from .detection import Detector, max_confidence
from .metrics import MetricsRecorder
from .monitoring import Snapshot, TelemetrySource, fetch_availability
from .response import ResponseBackend
from .simhooks import CompromiseSource
from . import observability as obs

log = logging.getLogger(__name__)
CONTAIN_THRESHOLD = 0.6


@dataclass
class CentralState:
    phase: str = "HEALTHY"
    epoch: int = 0
    stage: str = "FULL"
    phase_entered: float = 0.0
    contaminated_instance: str = ""
    last_decision: Optional[dict] = None


class CentralController:
    def __init__(self, cfg: ClusterConfig, telemetry: TelemetrySource, backend: ResponseBackend,
                 metrics: Optional[MetricsRecorder] = None,
                 compromise: Optional[CompromiseSource] = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.telemetry = telemetry
        self.backend = backend
        self.metrics = metrics or MetricsRecorder("CENTRAL", mode="centralized")
        self.compromise = compromise or CompromiseSource(path=None)
        self.clock = clock
        self.detector = Detector(cfg.baseline_hashes, cfg.process_allowlist)
        self.states: Dict[str, CentralState] = {n: CentralState() for n in cfg.nodes}
        self.snaps: Dict[str, Snapshot] = {}
        self.latest: Dict[str, list] = {}
        self.decisions: List[dict] = []
        self.lock = threading.RLock()
        self._comp_mode = None
        self.events = obs.EventLog("CENTRAL", clock)     # observability only

    def _decide(self, now: float, target: str, action: str, note: str) -> None:
        d = {"t": now, "action": action, "target": target, "epoch": self.states[target].epoch,
             "stage": self.states[target].stage, "voters": ["CENTRAL"], "note": note}
        self.decisions = (self.decisions + [d])[-30:]
        self.states[target].last_decision = d
        log.info("CENTRAL %s on %s: %s", action, target, note)
        self.events.emit(obs.QUORUM, f"The central controller alone decided to {action} "
                         f"{target} ({note}); no second opinion is required or possible.",
                         target, {"action": action, "signers": ["CENTRAL"], "note": note,
                                  "epoch": d["epoch"]})

    def _valid(self, target: str) -> bool:
        s, st = self.snaps.get(target), self.states[target]
        return bool(s and s.reachable and s.healthy
                    and s.instance_id != st.contaminated_instance
                    and (not self.cfg.baseline_hashes or s.file_hashes == self.cfg.baseline_hashes)
                    and max_confidence(self.latest.get(target, [])) < 0.3)

    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            comp = self.compromise.load()
            self._comp_mode = comp.mode if comp.active else None
            try:
                self.metrics.set_marker(self.backend.read_marker())
            except Exception:
                pass
            self.snaps = self.telemetry.collect()
            self.latest = self.detector.analyze(self.snaps, now)
            for nid, st in self.states.items():
                w = self.cfg.workload_of(nid)
                conf = max_confidence(self.latest.get(nid, []))
                ds = self.latest.get(nid, [])
                self.events.emit(
                    obs.OBSERVE,
                    obs.describe_observation("CENTRAL", nid, w, self.detector.measurements.get(nid, {}),
                                             conf).replace("Agent CENTRAL", "The central controller"),
                    nid, {"confidence": conf, "measurements": self.detector.measurements.get(nid, {})},
                    key=("observe", nid),
                    fingerprint=tuple(sorted((d.observation.value, round(d.confidence, 1)) for d in ds)))
                if conf >= 0.5:
                    self.metrics.event("first_detection", now, nid)
                forced = comp.active and comp.target == nid
                try:
                    if st.phase == "HEALTHY" and (conf >= CONTAIN_THRESHOLD or forced):
                        st.epoch += 1
                        st.phase, st.stage, st.phase_entered = "ISOLATED", "QUARANTINE", now
                        s = self.snaps.get(nid)
                        st.contaminated_instance = s.instance_id if s else ""
                        self._decide(now, nid, "CONTAIN", "compromised controller order" if forced
                                     else f"detection confidence {conf:.2f}")
                        self.metrics.event("contain_committed", now, nid)
                        self.backend.apply_stage(w, "QUARANTINE", {
                            "resilience.io/authorized-by": "CENTRAL",
                            "resilience.io/epoch": str(st.epoch)})
                        self.metrics.event("isolation_applied", self.clock(), nid)
                        self.backend.recover(w, st.epoch)
                        st.phase = "RECOVERING"
                        self.events.emit(obs.ACTION, f"The central controller isolated {w} ({nid}) "
                                         f"and started redeploying it from the known-good image.",
                                         nid, {"action": "isolate+recover", "workload": w,
                                               "result": "ok"})
                    elif st.phase == "RECOVERING" and self.backend.recovery_done(w, st.epoch):
                        st.phase, st.phase_entered = "VALIDATING", now
                        self.metrics.event("recovered", now, nid)
                        self.events.emit(obs.ACTION, f"The central controller sees {w} ({nid}) "
                                         f"recovered; validating.", nid,
                                         {"action": "recovered", "workload": w})
                    elif st.phase == "VALIDATING" and self._valid(nid):
                        # Binary reintegration: straight back to full access.
                        self.backend.apply_stage(w, "FULL", {})
                        st.phase, st.stage = "HEALTHY", "FULL"
                        self._decide(now, nid, "REINTEGRATE", "validated -> full access")
                        self.metrics.event("validated", now, nid)
                        self.metrics.event("reintegrated", now, nid)
                except Exception as exc:
                    log.error("central action on %s failed: %s", w, exc)
                    self.events.emit(obs.ACTION, f"The central controller failed an action on {w}: "
                                     f"{exc}.", nid, {"action": "failed", "result": str(exc)},
                                     key=("fail", nid), every=10.0)

    def status(self, events_since: Optional[int] = None, include_events: bool = True) -> dict:
        with self.lock:
            targets = {}
            for nid, st in self.states.items():
                s = self.snaps.get(nid)
                targets[nid] = {**asdict(st), "workload": self.cfg.workload_of(nid),
                                "workload_trust": None,
                                "reachable": bool(s and s.reachable), "healthy": bool(s and s.healthy),
                                "detections": [{"type": d.observation.value, "confidence": d.confidence,
                                                "summary": d.summary} for d in self.latest.get(nid, [])]}
            return {"node": "CENTRAL", "mode": "centralized", "time": self.clock(),
                    "compromised": self._comp_mode, "targets": targets,
                    "decisions": self.decisions[-15:], "agent_trust": {},
                    "observations": {t: {**m, "confidence": max_confidence(self.latest.get(t, []))}
                                     for t, m in self.detector.measurements.items()},
                    "event_boot": self.events.boot, "event_seq": self.events.seq,
                    "events": self.events.recent(since=events_since) if include_events else []}

    def metrics_summary(self) -> List[dict]:
        return self.metrics.summary(fetch_availability(self.cfg.client_stats_url))
