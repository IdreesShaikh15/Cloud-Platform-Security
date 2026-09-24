"""Metrics logging: time-to-detect / isolate / recover (+ reintegration, trust).

The attack-injection script writes a marker (ConfigMap `cr-attack-marker`)
with the injection timestamp. Every agent (and the centralized baseline)
opens an *incident* for that marker and records the first time it saw each
pipeline event for the marker's target. All durations are measured from the
injection time:

  TTD  first_detection      - injected_at
  TTI  isolation_applied    - injected_at
  TTR  recovered            - injected_at    (clean pod from known-good image is Ready)
  TTV  validated            - injected_at    (health + hash check passed by quorum)
  TTF  reintegrated         - injected_at    (back to FULL access)
  trust_recovery_s          reintegrated - validated

For a false-accusation incident the interesting numbers are instead
  false_isolation     True if the accused (healthy) node was ever contained
  time_to_flag_s      attacker's trust (in this agent's view) < suspect threshold
Events are appended as JSON lines to `path` (and kept in memory for /metrics).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

DURATIONS = {
    "ttd_s": "first_detection",
    "tti_s": "isolation_applied",
    "ttr_s": "recovered",
    "ttv_s": "validated",
    "ttf_s": "reintegrated",
    "time_to_flag_s": "attacker_flagged",
}


class MetricsRecorder:
    def __init__(self, node_id: str, mode: str = "distributed", path: Optional[str] = None):
        self.node_id = node_id
        self.mode = mode
        self.path = path
        self._lock = threading.Lock()
        self.incidents: Dict[str, dict] = {}
        self.events: List[dict] = []

    def _write(self, rec: dict) -> None:
        self.events.append(rec)
        self.events = self.events[-2000:]
        log.info("METRIC %s", json.dumps(rec))
        if self.path:
            try:
                with open(self.path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except OSError:
                pass

    def set_marker(self, marker: Optional[dict]) -> None:
        if not marker or not marker.get("id"):
            return
        with self._lock:
            if marker["id"] in self.incidents:
                return
            inc = {k: marker.get(k) for k in ("id", "scenario", "target", "attacker", "injected_at")}
            inc.update({"events": {}, "false_isolation": False})
            self.incidents[marker["id"]] = inc
            self._write({"node": self.node_id, "mode": self.mode, "event": "incident_opened", **inc,
                         "events": None})

    def _active_for(self, subject: Optional[str], as_agent: bool) -> List[dict]:
        key = "attacker" if as_agent else "target"
        return [i for i in self.incidents.values() if subject is None or i.get(key) == subject]

    def event(self, name: str, t: float, target: Optional[str] = None,
              as_agent: bool = False, **extra) -> None:
        """Record the first occurrence of `name` for each open incident whose
        target (or, with as_agent=True, whose compromised attacker) is `target`."""
        with self._lock:
            for inc in self._active_for(target, as_agent):
                if inc["injected_at"] and t < inc["injected_at"]:
                    continue
                if name == "contain_committed" and inc["scenario"] in (
                        "false-accusation", "forge-evidence") and target == inc["target"]:
                    inc["false_isolation"] = True
                if name not in inc["events"]:
                    inc["events"][name] = t
                    self._write({"node": self.node_id, "mode": self.mode, "incident": inc["id"],
                                 "event": name, "target": target, "t": t,
                                 "since_injection_s": round(t - (inc["injected_at"] or t), 3),
                                 **extra})

    def summary(self, availability: Optional[dict] = None) -> List[dict]:
        out = []
        with self._lock:
            for inc in self.incidents.values():
                row = {"incident": inc["id"], "scenario": inc["scenario"], "target": inc["target"],
                       "attacker": inc.get("attacker"), "injected_at": inc["injected_at"],
                       "node": self.node_id, "mode": self.mode,
                       "false_isolation": inc["false_isolation"]}
                ev = inc["events"]
                for key, name in DURATIONS.items():
                    row[key] = round(ev[name] - inc["injected_at"], 3) if name in ev else None
                if "validated" in ev and "reintegrated" in ev:
                    row["trust_recovery_s"] = round(ev["reintegrated"] - ev["validated"], 3)
                else:
                    row["trust_recovery_s"] = None
                row["stages"] = {k: round(v - inc["injected_at"], 3)
                                 for k, v in ev.items() if k.startswith("stage:")}
                if availability and availability.get("samples"):
                    end = ev.get("reintegrated") or float("inf")
                    s = [ok for t, ok in availability["samples"] if inc["injected_at"] <= t <= end]
                    row["availability"] = round(sum(s) / len(s), 4) if s else None
                out.append(row)
        return out
