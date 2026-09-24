"""Typed, signed evidence and votes.

Python-side dataclasses mirror the protobuf messages; `seal()` produces a
SignedEnvelope and `open_envelope()` performs every acceptance check a
receiver must do (identity binding, signature, origin match, freshness,
replay).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Union

from .crypto import KeyRegistry, Signer
from .proto import resilience_pb2 as pb


class ObsType(str, Enum):
    NETWORK = "NETWORK"
    PROCESS = "PROCESS"
    FILE_INTEGRITY = "FILE_INTEGRITY"
    AUTH = "AUTH"

    @property
    def pb(self) -> int:
        return pb.ObservationType.Value(self.value)

    @classmethod
    def from_pb(cls, v: int) -> "ObsType":
        return cls(pb.ObservationType.Name(v))


class Action(str, Enum):
    CONTAIN = "CONTAIN"
    VALIDATE = "VALIDATE"
    ADVANCE_STAGE = "ADVANCE_STAGE"

    @property
    def pb(self) -> int:
        return pb.ActionKind.Value(self.value)

    @classmethod
    def from_pb(cls, v: int) -> "Action":
        return cls(pb.ActionKind.Name(v))


@dataclass(frozen=True)
class Evidence:
    origin: str
    target: str
    observation: ObsType
    confidence: float
    timestamp: float = field(default_factory=time.time)
    summary: str = ""
    evidence_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_pb(self) -> pb.Evidence:
        return pb.Evidence(
            evidence_id=self.evidence_id, origin=self.origin, target=self.target,
            observation=self.observation.pb, confidence=self.confidence,
            timestamp=self.timestamp, summary=self.summary)

    @classmethod
    def from_pb(cls, m: pb.Evidence) -> "Evidence":
        return cls(origin=m.origin, target=m.target,
                   observation=ObsType.from_pb(m.observation),
                   confidence=max(0.0, min(1.0, m.confidence)),
                   timestamp=m.timestamp, summary=m.summary,
                   evidence_id=m.evidence_id)

    def to_dict(self) -> dict:
        return {"id": self.evidence_id, "origin": self.origin, "target": self.target,
                "type": self.observation.value, "confidence": round(self.confidence, 3),
                "timestamp": self.timestamp, "summary": self.summary}


@dataclass(frozen=True)
class Vote:
    voter: str
    target: str
    action: Action
    epoch: int
    stage: str = ""
    score: float = 0.0
    timestamp: float = field(default_factory=time.time)
    evidence_ids: Tuple[str, ...] = ()
    vote_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def action_key(self) -> str:
        """Votes are counted per action_key: identical keys = same proposal."""
        return f"{self.action.value}:{self.target}:{self.epoch}:{self.stage}"

    def to_pb(self) -> pb.Vote:
        return pb.Vote(
            vote_id=self.vote_id, voter=self.voter, target=self.target,
            action=self.action.pb, epoch=self.epoch, stage=self.stage,
            score=self.score, timestamp=self.timestamp,
            evidence_ids=list(self.evidence_ids))

    @classmethod
    def from_pb(cls, m: pb.Vote) -> "Vote":
        return cls(voter=m.voter, target=m.target, action=Action.from_pb(m.action),
                   epoch=m.epoch, stage=m.stage, score=m.score,
                   timestamp=m.timestamp, evidence_ids=tuple(m.evidence_ids),
                   vote_id=m.vote_id)

    def to_dict(self) -> dict:
        return {"voter": self.voter, "target": self.target, "action": self.action.value,
                "epoch": self.epoch, "stage": self.stage, "score": round(self.score, 3),
                "timestamp": self.timestamp}


Claim = Union[Evidence, Vote]


def seal(signer: Signer, claim: Claim, *, claimed_signer: Optional[str] = None) -> pb.SignedEnvelope:
    """Serialize + sign. `claimed_signer` exists only so the attack simulator
    can try to impersonate another node (the signature will not verify)."""
    kind = "evidence" if isinstance(claim, Evidence) else "vote"
    payload = claim.to_pb().SerializeToString()
    return pb.SignedEnvelope(signer=claimed_signer or signer.node_id, kind=kind,
                             payload=payload, signature=signer.sign(kind, payload))


class ReplayCache:
    """Remembers message ids for the freshness window."""

    def __init__(self, ttl_s: float = 120.0):
        self.ttl = ttl_s
        self._seen: dict = {}

    def seen(self, msg_id: str, now: float) -> bool:
        if len(self._seen) > 5000:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl}
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False


def open_envelope(env: pb.SignedEnvelope, registry: KeyRegistry, *,
                  transport_identity: Optional[str], now: float,
                  max_skew_s: float, replay: Optional[ReplayCache] = None
                  ) -> Tuple[Optional[Claim], str]:
    """Return (claim, "ok") or (None, reason). `transport_identity` is the node
    id bound to the mTLS connection (None skips the check, e.g. in unit tests)."""
    if transport_identity is not None and env.signer != transport_identity:
        return None, f"signer {env.signer!r} != mTLS identity {transport_identity!r}"
    if not registry.knows(env.signer):
        return None, f"unknown signer {env.signer!r}"
    if env.kind not in ("evidence", "vote"):
        return None, f"bad kind {env.kind!r}"
    if not registry.verify(env.signer, env.kind, env.payload, env.signature):
        return None, "invalid Ed25519 signature"
    try:
        if env.kind == "evidence":
            m = pb.Evidence()
            m.ParseFromString(env.payload)
            claim: Claim = Evidence.from_pb(m)
            author, msg_id = claim.origin, claim.evidence_id
        else:
            m = pb.Vote()
            m.ParseFromString(env.payload)
            claim = Vote.from_pb(m)
            author, msg_id = claim.voter, claim.vote_id
    except Exception as exc:  # malformed payload / unknown enum
        return None, f"malformed payload: {exc}"
    if author != env.signer:
        return None, f"payload author {author!r} != signer {env.signer!r}"
    if abs(now - claim.timestamp) > max_skew_s:
        return None, f"stale or future timestamp ({now - claim.timestamp:+.1f}s)"
    if replay is not None and replay.seen(msg_id, now):
        return None, "replayed message id"
    return claim, "ok"
