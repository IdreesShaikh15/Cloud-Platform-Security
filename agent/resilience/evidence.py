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
    investigation_id: str = ""

    @property
    def action_key(self) -> str:
        """Votes are counted per action_key: identical keys = same proposal."""
        return f"{self.action.value}:{self.target}:{self.epoch}:{self.stage}"

    def to_pb(self) -> pb.Vote:
        return pb.Vote(
            vote_id=self.vote_id, voter=self.voter, target=self.target,
            action=self.action.pb, epoch=self.epoch, stage=self.stage,
            score=self.score, timestamp=self.timestamp,
            evidence_ids=list(self.evidence_ids), investigation_id=self.investigation_id)

    @classmethod
    def from_pb(cls, m: pb.Vote) -> "Vote":
        return cls(voter=m.voter, target=m.target, action=Action.from_pb(m.action),
                   epoch=m.epoch, stage=m.stage, score=m.score,
                   timestamp=m.timestamp, evidence_ids=tuple(m.evidence_ids),
                   vote_id=m.vote_id, investigation_id=m.investigation_id)

    def to_dict(self) -> dict:
        return {"voter": self.voter, "target": self.target, "action": self.action.value,
                "epoch": self.epoch, "stage": self.stage, "score": round(self.score, 3),
                "timestamp": self.timestamp}


@dataclass(frozen=True)
class InvestigateRequest:
    requester: str
    investigation_id: str
    target: str
    epoch: int
    signals: Tuple[ObsType, ...]
    started_at: float
    budget_s: float
    trigger: str = ""
    timestamp: float = field(default_factory=time.time)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_pb(self) -> pb.InvestigateRequest:
        return pb.InvestigateRequest(
            request_id=self.request_id, investigation_id=self.investigation_id,
            requester=self.requester, target=self.target, epoch=self.epoch,
            signals=[s.pb for s in self.signals], started_at=self.started_at,
            budget_s=self.budget_s, trigger=self.trigger, timestamp=self.timestamp)

    @classmethod
    def from_pb(cls, m: pb.InvestigateRequest) -> "InvestigateRequest":
        return cls(requester=m.requester, investigation_id=m.investigation_id, target=m.target,
                   epoch=m.epoch, signals=tuple(ObsType.from_pb(x) for x in m.signals),
                   started_at=m.started_at, budget_s=m.budget_s, trigger=m.trigger,
                   timestamp=m.timestamp, request_id=m.request_id)


@dataclass(frozen=True)
class SignalReading:
    """What one agent measured for ONE signal type during an investigation."""
    observation: ObsType
    last_confidence: float = 0.0
    max_confidence: float = 0.0
    samples: int = 0
    positive_samples: int = 0
    tail_samples: int = 0
    tail_positive: int = 0
    summary: str = ""

    def to_pb(self) -> pb.SignalReading:
        return pb.SignalReading(
            observation=self.observation.pb, last_confidence=self.last_confidence,
            max_confidence=self.max_confidence, samples=self.samples,
            positive_samples=self.positive_samples, tail_samples=self.tail_samples,
            tail_positive=self.tail_positive, summary=self.summary)

    @classmethod
    def from_pb(cls, m: pb.SignalReading) -> "SignalReading":
        return cls(ObsType.from_pb(m.observation), max(0.0, min(1.0, m.last_confidence)),
                   max(0.0, min(1.0, m.max_confidence)), m.samples, m.positive_samples,
                   m.tail_samples, m.tail_positive, m.summary)

    def to_dict(self) -> dict:
        return {"type": self.observation.value, "last": round(self.last_confidence, 3),
                "max": round(self.max_confidence, 3), "samples": self.samples,
                "positive": self.positive_samples, "tail": self.tail_samples,
                "tail_positive": self.tail_positive, "summary": self.summary}


@dataclass(frozen=True)
class InvestigateResponse:
    responder: str
    investigation_id: str
    target: str
    epoch: int
    readings: Tuple[SignalReading, ...] = ()
    telemetry_failures: int = 0
    instance_id: str = ""
    status: str = "ok"
    timestamp: float = field(default_factory=time.time)
    response_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_pb(self) -> pb.InvestigateResponse:
        return pb.InvestigateResponse(
            response_id=self.response_id, investigation_id=self.investigation_id,
            responder=self.responder, target=self.target, epoch=self.epoch,
            readings=[r.to_pb() for r in self.readings],
            telemetry_failures=self.telemetry_failures, instance_id=self.instance_id,
            timestamp=self.timestamp, status=self.status)

    @classmethod
    def from_pb(cls, m: pb.InvestigateResponse) -> "InvestigateResponse":
        return cls(responder=m.responder, investigation_id=m.investigation_id, target=m.target,
                   epoch=m.epoch, readings=tuple(SignalReading.from_pb(r) for r in m.readings),
                   telemetry_failures=m.telemetry_failures, instance_id=m.instance_id,
                   status=m.status, timestamp=m.timestamp, response_id=m.response_id)


Claim = Union[Evidence, Vote, InvestigateRequest, InvestigateResponse]

KINDS = ("evidence", "vote", "inv_request", "inv_response", "cert_share")


def seal(signer: Signer, claim: Claim, *, claimed_signer: Optional[str] = None) -> pb.SignedEnvelope:
    """Serialize + sign. `claimed_signer` exists only so the attack simulator
    can try to impersonate another node (the signature will not verify)."""
    kind = {Evidence: "evidence", Vote: "vote", InvestigateRequest: "inv_request",
            InvestigateResponse: "inv_response"}[type(claim)]
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
    if env.kind not in KINDS:
        return None, f"bad kind {env.kind!r}"
    if not registry.verify(env.signer, env.kind, env.payload, env.signature):
        return None, "invalid Ed25519 signature"
    try:
        if env.kind == "evidence":
            m = pb.Evidence()
            m.ParseFromString(env.payload)
            claim: Claim = Evidence.from_pb(m)
            author, msg_id = claim.origin, claim.evidence_id
        elif env.kind == "vote":
            m = pb.Vote()
            m.ParseFromString(env.payload)
            claim = Vote.from_pb(m)
            author, msg_id = claim.voter, claim.vote_id
        elif env.kind == "cert_share":
            m = pb.CertShare()
            m.ParseFromString(env.payload)
            claim = m                                  # used as-is: certificate.py parses the statement
            author, msg_id = m.signer, m.share_id
        elif env.kind == "inv_request":
            m = pb.InvestigateRequest()
            m.ParseFromString(env.payload)
            claim = InvestigateRequest.from_pb(m)
            author, msg_id = claim.requester, claim.request_id
        else:
            m = pb.InvestigateResponse()
            m.ParseFromString(env.payload)
            claim = InvestigateResponse.from_pb(m)
            author, msg_id = claim.responder, claim.response_id
    except Exception as exc:  # malformed payload / unknown enum
        return None, f"malformed payload: {exc}"
    if author != env.signer:
        return None, f"payload author {author!r} != signer {env.signer!r}"
    if abs(now - claim.timestamp) > max_skew_s:
        return None, f"stale or future timestamp ({now - claim.timestamp:+.1f}s)"
    if replay is not None and replay.seen(msg_id, now):
        return None, "replayed message id"
    return claim, "ok"
