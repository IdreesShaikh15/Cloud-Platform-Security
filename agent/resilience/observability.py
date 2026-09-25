"""Observability: a structured, throttled event log per agent.

This module only *records* what the agent does; nothing in the decision path
reads from it. Every event is

    {seq, ts, node, category, target, summary, details{...}}

where `summary` is one plain-English sentence. Events live in an in-memory
ring buffer (last 300) exposed through /status, and are merged by the
dashboard into a live timeline.

Throttling keeps the log readable: repetitive events (per-tick trust drift,
the same evidence re-sent every tick, unchanged observations) are emitted at
most once per interval per key, or immediately when their *fingerprint*
changes (e.g. a threshold is crossed). Suppressed repeats are counted and
reported on the next emitted event of that key.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

OBSERVE = "OBSERVE"
EVIDENCE_SENT = "EVIDENCE_SENT"
EVIDENCE_RECEIVED = "EVIDENCE_RECEIVED"
REJECTED = "REJECTED"
SCORE = "SCORE"
VOTE_CAST = "VOTE_CAST"
VOTE_WITHHELD = "VOTE_WITHHELD"
TRUST_CHANGE = "TRUST_CHANGE"
FLAG = "FLAG"
QUORUM = "QUORUM"
ACTION = "ACTION"
PEER_LINK = "PEER_LINK"

CATEGORIES = (OBSERVE, EVIDENCE_SENT, EVIDENCE_RECEIVED, REJECTED, SCORE, VOTE_CAST,
              VOTE_WITHHELD, TRUST_CHANGE, FLAG, QUORUM, ACTION, PEER_LINK)

# Default minimum spacing (seconds) between repeats of the same throttle key.
THROTTLE_S = {
    OBSERVE: 15.0,            # heartbeat; any change of what is seen emits at once
    EVIDENCE_SENT: 3.0,
    EVIDENCE_RECEIVED: 3.0,
    REJECTED: 2.0,
    SCORE: 2.0,
    VOTE_WITHHELD: 5.0,       # a change of reason emits at once
    TRUST_CHANGE: 2.0,        # threshold crossings are forced through
}

RING_SIZE = 300


class EventLog:
    def __init__(self, node: str, clock: Callable[[], float] = time.time, maxlen: int = RING_SIZE):
        self.node = node
        self.clock = clock
        self.boot = uuid.uuid4().hex[:8]         # lets the dashboard notice a restart
        self._lock = threading.Lock()
        self._ring: Deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        # throttle key -> (last emitted ts, last fingerprint, suppressed count)
        self._last: Dict[Any, Tuple[float, Any, int]] = {}

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def emit(self, category: str, summary: str, target: Optional[str] = None,
             details: Optional[dict] = None, *, key: Any = None, every: Optional[float] = None,
             fingerprint: Any = None, force: bool = False) -> Optional[dict]:
        """Record an event. Returns it, or None if throttled.

        key          throttle identity (None = never throttled)
        every        min seconds between repeats with the same key
                     (defaults to THROTTLE_S[category] when key is given)
        fingerprint  if it differs from the last emitted one for this key, the
                     event is emitted immediately regardless of `every`
        force        always emit (threshold crossings, penalties)

        Never raises: observability must not be able to break the agent."""
        try:
            now = self.clock()
            with self._lock:
                suppressed = 0
                if key is not None and not force:
                    last = self._last.get(key)
                    if last is not None:
                        interval = every if every is not None else THROTTLE_S.get(category, 0.0)
                        changed = fingerprint is not None and fingerprint != last[1]
                        if not changed and now - last[0] < interval:
                            self._last[key] = (last[0], last[1], last[2] + 1)
                            return None
                        suppressed = last[2]
                if key is not None:
                    self._last[key] = (now, fingerprint, 0)
                self._seq += 1
                ev = {"seq": self._seq, "ts": now, "node": self.node, "category": category,
                      "target": target, "summary": summary, "details": dict(details or {})}
                if suppressed:
                    ev["details"]["suppressed_repeats"] = suppressed
                self._ring.append(ev)
                return ev
        except Exception:
            return None

    def recent(self, since: Optional[int] = None, limit: Optional[int] = None) -> List[dict]:
        with self._lock:
            evs = [e for e in self._ring if since is None or e["seq"] > since]
        return evs[-limit:] if limit else evs

    def by_category(self, category: str) -> List[dict]:
        with self._lock:
            return [e for e in self._ring if e["category"] == category]


# --------------------------------------------------------------------------- sentences
def _fmt(v: float, nd: int = 2) -> str:
    return f"{v:.{nd}f}"


def describe_observation(node: str, target: str, workload: str, m: dict, conf: float) -> str:
    """One sentence describing what `node` measured on `workload`."""
    who = f"Agent {node} sees {workload} ({target})"
    if not m.get("reachable", True):
        return f"{who}: telemetry unreachable, nothing could be measured."
    parts = []
    net = m.get("network") or {}
    if net.get("confidence", 0) > 0:
        bits = []
        if net.get("conn_confidence", 0) > 0:
            bits.append(f"{net['outbound_connections']} outbound connections (limit {net['conn_threshold']:g})")
        if net.get("tx_confidence", 0) > 0:
            bits.append(f"sending {net['tx_rate_Bps'] / 1024:.0f} KiB/s (limit {net['tx_threshold'] / 1024:.0f} KiB/s)")
        parts.append(" and ".join(bits))
    proc = m.get("process") or {}
    if proc.get("confidence", 0) > 0:
        names = sorted(set(proc.get("suspicious", []) + proc.get("unexpected", [])))
        parts.append(f"unexpected process{'es' if len(names) != 1 else ''} {', '.join(names)}")
    fi = m.get("file_integrity") or {}
    if fi.get("confidence", 0) > 0:
        n = len(fi.get("modified", [])) + len(fi.get("missing", [])) + len(fi.get("new", []))
        parts.append(f"{n} file{'s' if n != 1 else ''} differing from the known-good hashes")
    au = m.get("auth") or {}
    if au.get("confidence", 0) > 0:
        parts.append(f"{au['failures_in_window']} failed logins in {au['window_s']:g}s "
                     f"(limit {au['threshold']:g})")
    if not parts:
        extra = "" if m.get("healthy", True) else " (but its health check is failing)"
        return f"{who} as normal on all 4 signals{extra}."
    return f"{who}: {'; '.join(parts)} -> confidence {_fmt(conf)}."


def proposal_text(key: str) -> str:
    """'CONTAIN:B:1:' -> 'CONTAIN B (epoch 1)'; 'ADVANCE_STAGE:C:1:MONITORED' -> 'move C to MONITORED (epoch 1)'."""
    try:
        action, target, epoch, stage = key.split(":")
    except ValueError:
        return key
    if action == "ADVANCE_STAGE":
        return f"move {target} to {stage} (epoch {epoch})"
    return f"{action} {target} (epoch {epoch})"


def downsample(points: List[Tuple[float, float]], since: float, max_points: int = 60) -> List[List[float]]:
    pts = [p for p in points if p[0] >= since]
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
    return [[round(t, 2), v] for t, v in pts]
