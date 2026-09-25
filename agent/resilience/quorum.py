"""BFT-style quorum over typed, weighted evidence (n = 4, f = 1).

We do not invent a consensus protocol: with n = 3f + 1 nodes, any action needs
2f + 1 = 3 matching signed votes, which guarantees that at least f + 1 = 2
honest nodes agreed (classic Byzantine quorum intersection). Our contribution
is *what feeds a node's vote*:

Weighted evidence score for target T (computed by every agent locally)
---------------------------------------------------------------------
For every observation type k with evidence in the window:

    w(e)      = confidence(e) * trust(sender)/100
    mean_k    = mean over distinct senders s of max_e w(e)       (s's claim about k)
    support_k = min(1, senders_k / (f + 1))                       (one sender = half weight)
    score_k   = mean_k * support_k

    W(T) = max_k score_k + diversity_bonus * (corroborated_types - 1)

where a type is *corroborated* if at least f + 1 = 2 distinct senders
reported it (so at least one of them is honest). Consequences:

* a single node - however confident and however many types it claims - can
  contribute at most 0.5 per type and no diversity bonus -> W < 0.6;
* independent types corroborating each other push W up faster than the same
  type repeated;
* a sender whose trust has decayed contributes proportionally less.

Vote rule: an agent votes CONTAIN(T) only if
    (1) its OWN latest observation of T has confidence >= local_min_conf, and
    (2) W(T) >= score_threshold.
It never votes on hearsay alone, so one liar cannot recruit honest votes.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .config import QuorumParams
from .evidence import Evidence, ObsType, Vote


@dataclass
class ScoreBreakdown:
    total: float
    type_scores: Dict[str, float] = field(default_factory=dict)
    senders_per_type: Dict[str, List[str]] = field(default_factory=dict)
    corroborated_types: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    # Observability only: the individual terms that produced `total`.
    items: List[dict] = field(default_factory=list)
    type_detail: Dict[str, dict] = field(default_factory=dict)
    diversity_bonus: float = 0.0

    def to_dict(self) -> dict:
        return {"W": round(self.total, 3),
                "type_scores": {k: round(v, 3) for k, v in self.type_scores.items()},
                "senders": self.senders_per_type,
                "corroborated": self.corroborated_types,
                "items": self.items, "type_detail": self.type_detail,
                "diversity_bonus": round(self.diversity_bonus, 3)}


def weighted_score(evidence: Iterable[Evidence], trust_of: Callable[[str], float],
                   params: QuorumParams) -> ScoreBreakdown:
    best: Dict[ObsType, Dict[str, float]] = defaultdict(dict)
    ids: List[str] = []
    for e in evidence:
        per_sender = best[e.observation]
        if e.confidence > per_sender.get(e.origin, -1.0):
            per_sender[e.origin] = e.confidence
        ids.append(e.evidence_id)

    type_scores: Dict[str, float] = {}
    senders: Dict[str, List[str]] = {}
    corroborated: List[str] = []
    items: List[dict] = []
    type_detail: Dict[str, dict] = {}
    needed = params.f + 1
    for obs, per_sender in best.items():
        weights = [conf * trust_of(s) / 100.0 for s, conf in per_sender.items()]
        mean_w = sum(weights) / len(weights)
        support = min(1.0, len(per_sender) / needed)
        type_scores[obs.value] = mean_w * support
        senders[obs.value] = sorted(per_sender)
        if len(per_sender) >= needed:
            corroborated.append(obs.value)
        for (s, conf), w in zip(per_sender.items(), weights):
            items.append({"type": obs.value, "sender": s, "confidence": round(conf, 3),
                          "sender_trust": round(trust_of(s), 1), "weight": round(w, 3)})
        type_detail[obs.value] = {"mean_weight": round(mean_w, 3), "senders": len(per_sender),
                                  "support": round(support, 3),
                                  "score": round(mean_w * support, 3)}

    if not type_scores:
        return ScoreBreakdown(0.0)
    bonus = params.diversity_bonus * max(0, len(corroborated) - 1)
    total = max(type_scores.values()) + bonus
    return ScoreBreakdown(total, type_scores, senders, sorted(corroborated), ids,
                          items=items, type_detail=type_detail, diversity_bonus=bonus)


def should_vote_contain(local_max_conf: float, score: ScoreBreakdown,
                        params: QuorumParams) -> bool:
    return local_max_conf >= params.local_min_conf and score.total >= params.score_threshold


class EvidencePool:
    """Thread-safe store of accepted evidence (own + peers)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: List[Evidence] = []

    def add(self, e: Evidence) -> None:
        with self._lock:
            self._items.append(e)

    def recent(self, now: float, window_s: float, target: Optional[str] = None,
               origin: Optional[str] = None, min_conf: float = 0.0) -> List[Evidence]:
        with self._lock:
            self._items = [e for e in self._items if now - e.timestamp <= window_s * 2]
            return [e for e in self._items
                    if now - e.timestamp <= window_s
                    and (target is None or e.target == target)
                    and (origin is None or e.origin == origin)
                    and e.confidence >= min_conf]

    def clear_target(self, target: str) -> None:
        """Drop evidence about a target (after containment, so stale evidence
        from before recovery doesn't re-trigger)."""
        with self._lock:
            self._items = [e for e in self._items if e.target != target]


@dataclass
class Commit:
    action_key: str
    votes: List[Vote]

    @property
    def voters(self) -> List[str]:
        return sorted(v.voter for v in self.votes)

    @property
    def sample(self) -> Vote:
        return self.votes[0]


class VoteBook:
    """Collects signed votes; an action commits once `quorum` distinct eligible
    voters agree on the same action_key. The list of those signed votes is the
    quorum certificate (QC) attached to the resulting action."""

    def __init__(self, quorum: int):
        self.quorum = quorum
        self._lock = threading.Lock()
        self._votes: Dict[str, Dict[str, Vote]] = defaultdict(dict)
        self._committed: Set[str] = set()

    def add(self, v: Vote) -> None:
        with self._lock:
            self._votes[v.action_key][v.voter] = v

    def has_voted(self, voter: str, action_key: str) -> bool:
        with self._lock:
            return voter in self._votes.get(action_key, {})

    def is_committed(self, action_key: str) -> bool:
        with self._lock:
            return action_key in self._committed

    def tally(self, action_key: str, eligible: Callable[[str], bool]) -> List[str]:
        with self._lock:
            return sorted(v for v in self._votes.get(action_key, {}) if eligible(v))

    def pending(self) -> Dict[str, List[str]]:
        with self._lock:
            return {k: sorted(v) for k, v in self._votes.items() if k not in self._committed}

    def new_commits(self, eligible: Callable[[str], bool]) -> List[Commit]:
        out: List[Commit] = []
        with self._lock:
            for key, by_voter in self._votes.items():
                if key in self._committed:
                    continue
                good = [v for voter, v in by_voter.items() if eligible(voter)]
                if len(good) >= self.quorum:
                    self._committed.add(key)
                    out.append(Commit(key, sorted(good, key=lambda v: v.voter)))
        return out

    def forget_target(self, target: str, keep_prefixes: Tuple[str, ...] = ()) -> None:
        with self._lock:
            for key in list(self._votes):
                if f":{target}:" in key and key not in self._committed \
                        and not key.startswith(keep_prefixes):
                    del self._votes[key]
