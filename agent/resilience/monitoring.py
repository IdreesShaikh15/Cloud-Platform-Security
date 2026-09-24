"""Monitoring module: polls every protected workload's /health and /_telemetry.

Each agent polls *all four* workloads independently, so every observation of
a target comes from four separate observers that must later agree.
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from .config import ClusterConfig


@dataclass
class Snapshot:
    target: str
    time: float
    reachable: bool
    healthy: bool = False
    instance_id: str = ""
    pod_ip: str = ""
    outbound_connections: int = 0
    tx_bytes: int = 0
    processes: List[dict] = field(default_factory=list)
    file_hashes: Dict[str, str] = field(default_factory=dict)
    auth_failures_by_ip: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_telemetry(cls, target: str, now: float, healthy: bool, t: dict) -> "Snapshot":
        return cls(target=target, time=now, reachable=True, healthy=healthy,
                   instance_id=t.get("instance_id", ""), pod_ip=t.get("pod_ip", ""),
                   outbound_connections=int(t.get("outbound_connections", 0)),
                   tx_bytes=int(t.get("tx_bytes", 0)),
                   processes=t.get("processes", []),
                   file_hashes=t.get("file_hashes", {}),
                   auth_failures_by_ip={k: int(v) for k, v in
                                        t.get("auth_failures_by_ip", {}).items()})


class TelemetrySource(Protocol):
    def collect(self) -> Dict[str, Snapshot]: ...


def _get_json(url: str, timeout: float) -> tuple:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


class HttpTelemetrySource:
    def __init__(self, cfg: ClusterConfig, timeout_s: float = 1.5):
        self.cfg = cfg
        self.timeout = timeout_s
        self._pool = ThreadPoolExecutor(max_workers=len(cfg.nodes))

    def _one(self, node_id: str) -> Snapshot:
        base = self.cfg.nodes[node_id].telemetry_url.rstrip("/")
        now = time.time()
        try:
            code, _ = _get_json(f"{base}/health", self.timeout)
            healthy = code == 200
        except Exception:
            healthy = False
        try:
            _, tel = _get_json(f"{base}/_telemetry", self.timeout)
        except Exception:
            return Snapshot(target=node_id, time=now, reachable=False)
        return Snapshot.from_telemetry(node_id, now, healthy, tel)

    def collect(self) -> Dict[str, Snapshot]:
        ids = list(self.cfg.nodes)
        return dict(zip(ids, self._pool.map(self._one, ids)))


def fetch_availability(url: Optional[str], timeout_s: float = 1.5) -> Optional[dict]:
    if not url:
        return None
    try:
        return _get_json(url, timeout_s)[1]
    except Exception:
        return None
