"""Where does a piece of data come from? (docs/DASHBOARD.md)

Every event, decision, action record and metric row is stamped by the process that PRODUCES it, never by the
dashboard or the browser:

    SIMULATOR     the agent talks to the in-memory fake cluster and fake telemetry (sim/world.py)
    LIVE_CLUSTER  the agent talks to the real Kubernetes API and polls real workloads
    UNKNOWN       provenance cannot be established (for example: the backend says one thing, the telemetry another,
                  or an object declares nothing). Never silently treated as either of the two.

The source is derived from the two things that feed an agent: its response backend (what it changes) and its telemetry
source (what it sees). They must AGREE; if they do not, the result is UNKNOWN.
"""
from __future__ import annotations

from typing import Iterable

SIMULATOR = "SIMULATOR"
LIVE_CLUSTER = "LIVE_CLUSTER"
UNKNOWN = "UNKNOWN"
MIXED = "MIXED"                      # only ever produced by aggregate(): several different sources are present
VALID = (SIMULATOR, LIVE_CLUSTER)


def declared(obj) -> str:
    """What an object (backend or telemetry source) declares about itself: its class attribute SOURCE."""
    s = getattr(obj, "SOURCE", None)
    return s if s in VALID else UNKNOWN


def derive(backend, telemetry) -> str:
    b, t = declared(backend), declared(telemetry)
    return b if (b in VALID and b == t) else UNKNOWN


def aggregate(sources: Iterable[str]) -> str:
    """One label for a set of sources. No data at all -> UNKNOWN; more than one distinct value -> MIXED."""
    s = {x if x in VALID else UNKNOWN for x in sources}
    if not s:
        return UNKNOWN
    return next(iter(s)) if len(s) == 1 else MIXED
