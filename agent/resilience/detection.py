"""Detection module: simple thresholds, one rule per observation type.

Detection is deliberately *not* the contribution of this project (see the
spec, section 3): each rule is a threshold with a linear confidence ramp

    confidence = 0.5 + 0.5 * min(1, (x - threshold) / full_scale)    if x > threshold
               = 0                                                   otherwise

so a value just over the threshold gives 0.5 and a clearly anomalous value
saturates at 1.0. `sensitivity` < 1 lowers thresholds (used while a workload
is in the MONITORED reintegration stage).
"""
from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

from .evidence import ObsType
from .monitoring import Snapshot

SUSPICIOUS_NAMES = {"xmrig", "minerd", "nc", "ncat", "netcat", "socat", "nmap",
                    "hydra", "cryptominer", "kworkerds"}
SUSPICIOUS_DIRS = ("/tmp/", "/dev/shm/", "/var/tmp/")


@dataclass
class Thresholds:
    outbound_conns: float = 8          # established outbound TCP connections
    outbound_conns_scale: float = 16
    tx_rate_Bps: float = 256_000       # bytes/s transmitted (excluding lo)
    tx_rate_scale: float = 1_000_000
    auth_failures: float = 10          # failed auths attributed to the target
    auth_failures_scale: float = 40
    auth_window_s: float = 10.0
    unknown_process_conf: float = 0.6
    suspicious_process_conf: float = 0.95
    modified_file_conf: float = 0.95
    missing_file_conf: float = 0.8
    new_file_conf: float = 0.7


@dataclass
class Detection:
    target: str
    observation: ObsType
    confidence: float
    summary: str


def ramp(x: float, threshold: float, scale: float) -> float:
    if x <= threshold:
        return 0.0
    return round(0.5 + 0.5 * min(1.0, (x - threshold) / scale), 3)


class Detector:
    def __init__(self, baseline_hashes: Dict[str, str], process_allowlist: List[str],
                 thresholds: Optional[Thresholds] = None):
        self.baseline = baseline_hashes
        self.allow = set(process_allowlist)
        self.th = thresholds or Thresholds()
        self._last_tx: Dict[str, Tuple[float, int, str]] = {}
        self._auth_hist: Dict[str, Deque[Tuple[float, int]]] = defaultdict(lambda: deque(maxlen=120))
        self._auth_ip: Dict[str, str] = {}   # target -> pod ip the history above belongs to
        # Observability only: the raw value, effective threshold and resulting
        # confidence of every rule for every target, as last evaluated. Nothing
        # reads this back into a decision.
        self.measurements: Dict[str, dict] = {}

    # -- individual rules -------------------------------------------------
    def network(self, s: Snapshot, sens: float) -> Optional[Detection]:
        conf_c = ramp(s.outbound_connections, self.th.outbound_conns * sens,
                      self.th.outbound_conns_scale)
        rate = 0.0
        prev = self._last_tx.get(s.target)
        if prev and prev[2] == s.instance_id and s.time > prev[0] and s.tx_bytes >= prev[1]:
            rate = (s.tx_bytes - prev[1]) / (s.time - prev[0])
        self._last_tx[s.target] = (s.time, s.tx_bytes, s.instance_id)
        conf_r = ramp(rate, self.th.tx_rate_Bps * sens, self.th.tx_rate_scale)
        conf = max(conf_c, conf_r)
        self.measurements.setdefault(s.target, {})["network"] = {
            "outbound_connections": s.outbound_connections,
            "conn_threshold": self.th.outbound_conns * sens, "conn_confidence": conf_c,
            "tx_rate_Bps": round(rate, 1), "tx_threshold": self.th.tx_rate_Bps * sens,
            "tx_confidence": conf_r, "confidence": conf}
        if conf <= 0:
            return None
        return Detection(s.target, ObsType.NETWORK, conf,
                         f"{s.outbound_connections} outbound conns, tx {rate / 1024:.0f} KiB/s")

    def process(self, s: Snapshot, sens: float) -> Optional[Detection]:
        conf, bad = 0.0, []
        for p in s.processes:
            comm, exe = p.get("comm", ""), p.get("exe", "") or ""
            if comm in SUSPICIOUS_NAMES or exe.startswith(SUSPICIOUS_DIRS):
                conf = max(conf, self.th.suspicious_process_conf)
                bad.append(comm)
            elif comm not in self.allow:
                conf = max(conf, self.th.unknown_process_conf)
                bad.append(comm)
        self.measurements.setdefault(s.target, {})["process"] = {
            "process_count": len(s.processes), "allowlist": sorted(self.allow),
            "suspicious": sorted({p.get("comm", "") for p in s.processes
                                  if p.get("comm", "") in SUSPICIOUS_NAMES
                                  or (p.get("exe", "") or "").startswith(SUSPICIOUS_DIRS)}),
            "unexpected": sorted({c for c in bad}), "confidence": conf}
        if conf <= 0:
            return None
        return Detection(s.target, ObsType.PROCESS, conf,
                         f"unexpected processes: {', '.join(sorted(set(bad)))}")

    def file_integrity(self, s: Snapshot, sens: float) -> Optional[Detection]:
        if not self.baseline or not s.file_hashes:
            self.measurements.setdefault(s.target, {})["file_integrity"] = {
                "files_checked": 0, "modified": [], "missing": [], "new": [], "confidence": 0.0,
                "note": "no baseline or no hashes reported"}
            return None
        modified = [p for p, h in self.baseline.items() if p in s.file_hashes and s.file_hashes[p] != h]
        missing = [p for p in self.baseline if p not in s.file_hashes]
        new = [p for p in s.file_hashes if p not in self.baseline]
        conf = max(self.th.modified_file_conf if modified else 0.0,
                   self.th.missing_file_conf if missing else 0.0,
                   self.th.new_file_conf if new else 0.0)
        self.measurements.setdefault(s.target, {})["file_integrity"] = {
            "files_checked": len(self.baseline), "modified": modified, "missing": missing,
            "new": new, "confidence": conf}
        if conf <= 0:
            return None
        parts = [f"{n} {lbl}" for n, lbl in ((len(modified), "modified"),
                                             (len(missing), "missing"), (len(new), "new")) if n]
        return Detection(s.target, ObsType.FILE_INTEGRITY, conf,
                         f"{', '.join(parts)} file(s): {', '.join((modified + missing + new)[:3])}")

    def auth(self, target: str, target_ip: str, snaps: Dict[str, Snapshot],
             now: float, sens: float) -> Optional[Detection]:
        """Failed authentications *originating from* the target's pod IP, as
        counted by every service that authenticates callers."""
        if not target_ip:
            return None
        total = sum(s.auth_failures_by_ip.get(target_ip, 0) for s in snaps.values() if s.reachable)
        hist = self._auth_hist[target]
        if self._auth_ip.get(target) != target_ip:
            # The target is a different pod now (recovery gave it a new IP). The failure
            # counters are cumulative per IP, so an IP that was used by an older pod
            # could carry a large old total; comparing it with the previous pod's
            # history would look like a burst of failures. Start a fresh window.
            hist.clear()
            self._auth_ip[target] = target_ip
        hist.append((now, total))
        while hist and now - hist[0][0] > self.th.auth_window_s:
            hist.popleft()
        in_window = max(0, total - hist[0][1]) if hist else 0
        conf = ramp(in_window, self.th.auth_failures * sens, self.th.auth_failures_scale)
        self.measurements.setdefault(target, {})["auth"] = {
            "source_ip": target_ip, "failures_in_window": in_window,
            "threshold": self.th.auth_failures * sens, "window_s": self.th.auth_window_s,
            "confidence": conf}
        if conf <= 0:
            return None
        return Detection(target, ObsType.AUTH, conf,
                         f"{in_window} failed auths from {target_ip} in {self.th.auth_window_s:.0f}s")

    # -- all rules ---------------------------------------------------------
    def analyze(self, snaps: Dict[str, Snapshot], now: float,
                sensitivity: Optional[Dict[str, float]] = None) -> Dict[str, List[Detection]]:
        sensitivity = sensitivity or {}
        out: Dict[str, List[Detection]] = {}
        for target, s in snaps.items():
            sens = sensitivity.get(target, 1.0)
            self.measurements[target] = {"reachable": s.reachable, "healthy": s.healthy,
                                         "sensitivity": sens}
            dets: List[Detection] = []
            if s.reachable:
                for rule in (self.network, self.process, self.file_integrity):
                    d = rule(s, sens)
                    if d:
                        dets.append(d)
            d = self.auth(target, s.pod_ip, snaps, now, sens)
            if d:
                dets.append(d)
            out[target] = dets
        return out


def max_confidence(dets: List[Detection]) -> float:
    return max((d.confidence for d in dets), default=0.0)
