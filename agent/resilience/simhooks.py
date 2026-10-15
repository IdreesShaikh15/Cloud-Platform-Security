"""Compromise simulation for a *resilience* node (the headline experiment).

A compromised agent is simulated by dropping a JSON file into the agent
container (the attack script does this with `kubectl exec`), e.g.

    {"mode": "false-accusation", "target": "C",
     "types": ["NETWORK", "PROCESS", "FILE_INTEGRITY", "AUTH"], "confidence": 0.95}

Modes:
  false-accusation  sign and broadcast fabricated high-confidence evidence
                    about a healthy node, and vote CONTAIN against it
  forge-evidence    additionally try to impersonate another node (claim
                    signer=B while signing with A's key) - receivers reject it
  obstruct          withhold every vote about the target workload (CONTAIN, VALIDATE, stage moves, retries). Nothing is signed or
                    sent for it, and no "all clear" evidence is invented: the agent simply refuses to take part. The other three
                    agents are a quorum without it. It only changes THIS agent's own behaviour; it never touches the cluster.
  (anything else / file absent)  behave honestly

The agent still uses its *real* key and mTLS identity - this models an
insider whose credentials are intact but whose logic is malicious, which is
exactly the case signatures alone cannot stop and the quorum must.

Out of scope (future work): "go silent" (stop sending evidence too) and blocking the executor's own action.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_PATH = os.environ.get("COMPROMISE_FILE", "/tmp/cr-sim/compromise.json")


@dataclass
class Compromise:
    mode: str = "honest"
    target: str = ""
    types: List[str] = field(default_factory=lambda: ["NETWORK", "PROCESS",
                                                      "FILE_INTEGRITY", "AUTH"])
    confidence: float = 0.95
    impersonate: str = "B"

    def obstructing(self, target: str) -> bool:
        return self.mode == "obstruct" and self.target == target

    @property
    def active(self) -> bool:
        return self.mode in ("false-accusation", "forge-evidence") and bool(self.target)


class CompromiseSource:
    """Reads the compromise file (cluster) or holds an in-memory value (sim)."""

    def __init__(self, path: Optional[str] = DEFAULT_PATH):
        self.path = path
        self._override: Optional[Compromise] = None
        self._lock = threading.Lock()

    def set(self, c: Optional[Compromise]) -> None:
        with self._lock:
            self._override = c

    def load(self) -> Compromise:
        with self._lock:
            if self._override is not None:
                return self._override
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path) as fh:
                    raw = json.load(fh)
                return Compromise(**{k: v for k, v in raw.items()
                                     if k in Compromise.__dataclass_fields__})
            except (OSError, ValueError, TypeError):
                pass
        return Compromise()
