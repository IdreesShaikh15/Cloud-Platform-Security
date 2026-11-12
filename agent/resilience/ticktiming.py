"""How late does the agent's own 1-second loop run? Observability only: nothing in a decision ever reads this.

`record(start, duration)` is called once per tick by the loop in __main__.py. A tick is LATE when it starts more than one period after the
previous one started (the tick before it took too long, or the process was not scheduled: a busy laptop, a throttled container).
scripts/demo-health.sh reads the summary from /status to tell whether the laptop, not the design, is the bottleneck.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Optional


class TickTiming:
    def __init__(self, period_s: float, keep: int = 120):
        self.period_s = float(period_s)
        self._lock = threading.Lock()
        self._lags: Deque[float] = deque(maxlen=keep)       # seconds a tick started later than one period after the previous start
        self._work: Deque[float] = deque(maxlen=keep)       # seconds the tick itself took
        self._last: Optional[float] = None
        self.ticks = 0

    def record(self, start: float, duration: float) -> None:
        with self._lock:
            lag = 0.0 if self._last is None else max(0.0, start - self._last - self.period_s)
            self._last = start
            self._lags.append(lag)
            self._work.append(max(0.0, duration))
            self.ticks += 1

    def summary(self) -> dict:
        with self._lock:
            lags, work = sorted(self._lags), list(self._work)
            n = len(lags)
            if not n:
                return {"period_s": self.period_s, "ticks": self.ticks, "window": 0}
            p95 = lags[min(n - 1, int(0.95 * n))]
            return {"period_s": self.period_s, "ticks": self.ticks, "window": n,
                    "lag_ms_avg": round(1000 * sum(lags) / n, 1), "lag_ms_p95": round(1000 * p95, 1), "lag_ms_max": round(1000 * lags[-1], 1),
                    "work_ms_avg": round(1000 * sum(work) / n, 1), "work_ms_max": round(1000 * max(work), 1),
                    "late_ticks": sum(1 for x in lags if x > 0.25 * self.period_s)}
