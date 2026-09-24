"""Continuous trust scores (0-100) with simple linear decay / recovery.

    T(t + dt) = clamp(T(t) + r * dt, 0, 100)
    r = -decay_rate   while the node is misbehaving (or its workload is anomalous)
    r = +recover_rate otherwise

Two kinds of subjects are tracked by every agent, each from its *own* point
of view (there is no global trust oracle):

* agent trust    - how credible a peer's evidence/votes are. Falls while the
                   peer's claims are contradicted by what this agent observes.
* workload trust - how healthy a protected workload is. Drops to 0 on
                   isolation and climbs back during staged reintegration.

The dashboard shows the *median* of all agents' views, which a single lying
agent cannot move far.
"""
from __future__ import annotations

import statistics
import threading
from collections import deque
from typing import Deque, Dict, Iterable, List, Tuple

MAX_TRUST = 100.0
MIN_TRUST = 0.0


def clamp(v: float) -> float:
    return max(MIN_TRUST, min(MAX_TRUST, v))


def linear_step(value: float, rate_per_s: float, dt: float) -> float:
    """One linear decay (negative rate) or recovery (positive rate) step."""
    return clamp(value + rate_per_s * dt)


class TrustLedger:
    def __init__(self, subjects: Iterable[str], initial: float = MAX_TRUST,
                 history_len: int = 600):
        self._lock = threading.Lock()
        self._scores: Dict[str, float] = {s: clamp(initial) for s in subjects}
        self._history: Dict[str, Deque[Tuple[float, float]]] = {
            s: deque(maxlen=history_len) for s in self._scores}

    def get(self, subject: str) -> float:
        with self._lock:
            return self._scores.get(subject, MAX_TRUST)

    def all(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._scores)

    def step(self, subject: str, rate_per_s: float, dt: float) -> float:
        with self._lock:
            v = linear_step(self._scores.get(subject, MAX_TRUST), rate_per_s, dt)
            self._scores[subject] = v
            return v

    def decay(self, subject: str, rate_per_s: float, dt: float) -> float:
        return self.step(subject, -abs(rate_per_s), dt)

    def recover(self, subject: str, rate_per_s: float, dt: float) -> float:
        return self.step(subject, abs(rate_per_s), dt)

    def penalize(self, subject: str, amount: float) -> float:
        with self._lock:
            v = clamp(self._scores.get(subject, MAX_TRUST) - abs(amount))
            self._scores[subject] = v
            return v

    def set(self, subject: str, value: float) -> None:
        with self._lock:
            self._scores[subject] = clamp(value)

    def record(self, now: float) -> None:
        """Append the current values to the trajectory history (for plots)."""
        with self._lock:
            for s, v in self._scores.items():
                self._history.setdefault(s, deque(maxlen=600)).append((now, round(v, 2)))

    def history(self, subject: str) -> List[Tuple[float, float]]:
        with self._lock:
            return list(self._history.get(subject, ()))


def consensus_trust(views: Iterable[float]) -> float:
    """Median of several agents' views (robust to one outlier when n=4)."""
    vals = [v for v in views if v is not None]
    return statistics.median(vals) if vals else MAX_TRUST
