"""Ed25519 signatures and the envelope acceptance rules."""
import time

import pytest

from resilience.crypto import KeyRegistry, Signer
from resilience.evidence import (Action, Evidence, ObsType, ReplayCache, Vote,
                                 open_envelope, seal)


@pytest.fixture()
def keys():
    signers = {n: Signer.generate(n) for n in "ABCD"}
    reg = KeyRegistry.from_b64_map({n: s.public_key_b64() for n, s in signers.items()})
    return signers, reg


def ev(origin="A", target="C", conf=0.88, ts=None):
    return Evidence(origin=origin, target=target, observation=ObsType.NETWORK,
                    confidence=conf, timestamp=ts or time.time(), summary="abnormal outbound")


def test_valid_evidence_roundtrip(keys):
    signers, reg = keys
    e = ev()
    claim, reason = open_envelope(seal(signers["A"], e), reg, transport_identity="A",
                                  now=time.time(), max_skew_s=10)
    assert reason == "ok"
    assert claim == e


def test_tampered_payload_rejected(keys):
    signers, reg = keys
    env = seal(signers["A"], ev(conf=0.1))
    tampered = Evidence.from_pb(type(ev().to_pb()).FromString(env.payload))
    env.payload = Evidence(**{**tampered.__dict__, "confidence": 0.99}).to_pb().SerializeToString()
    claim, reason = open_envelope(env, reg, transport_identity="A", now=time.time(), max_skew_s=10)
    assert claim is None and "signature" in reason


def test_impersonation_rejected_by_signature(keys):
    signers, reg = keys
    # A signs a payload claiming to come from B.
    env = seal(signers["A"], ev(origin="B"), claimed_signer="B")
    claim, reason = open_envelope(env, reg, transport_identity=None, now=time.time(), max_skew_s=10)
    assert claim is None and "signature" in reason


def test_impersonation_rejected_by_mtls_binding(keys):
    signers, reg = keys
    env = seal(signers["A"], ev(origin="B"), claimed_signer="B")
    claim, reason = open_envelope(env, reg, transport_identity="A", now=time.time(), max_skew_s=10)
    assert claim is None and "mTLS identity" in reason


def test_origin_must_match_signer(keys):
    signers, reg = keys
    env = seal(signers["A"], ev(origin="B"))  # signer A, payload says B
    claim, reason = open_envelope(env, reg, transport_identity="A", now=time.time(), max_skew_s=10)
    assert claim is None and "author" in reason


def test_domain_separation(keys):
    signers, reg = keys
    env = seal(signers["A"], ev())
    env.kind = "vote"  # evidence signature must not verify as a vote
    claim, reason = open_envelope(env, reg, transport_identity="A", now=time.time(), max_skew_s=10)
    assert claim is None


def test_stale_and_replay(keys):
    signers, reg = keys
    now = time.time()
    claim, reason = open_envelope(seal(signers["A"], ev(ts=now - 100)), reg,
                                  transport_identity="A", now=now, max_skew_s=15)
    assert claim is None and "stale" in reason
    cache = ReplayCache()
    env = seal(signers["A"], ev(ts=now))
    assert open_envelope(env, reg, transport_identity="A", now=now, max_skew_s=15, replay=cache)[0]
    assert open_envelope(env, reg, transport_identity="A", now=now, max_skew_s=15,
                         replay=cache)[1] == "replayed message id"


def test_vote_roundtrip_and_key(keys):
    signers, reg = keys
    v = Vote(voter="B", target="C", action=Action.CONTAIN, epoch=1, score=0.9)
    claim, reason = open_envelope(seal(signers["B"], v), reg, transport_identity="B",
                                  now=time.time(), max_skew_s=10)
    assert reason == "ok" and claim.action_key == "CONTAIN:C:1:"


def test_unknown_signer(keys):
    _, reg = keys
    stranger = Signer.generate("E")
    claim, reason = open_envelope(seal(stranger, ev(origin="E")), reg, transport_identity="E",
                                  now=time.time(), max_skew_s=10)
    assert claim is None and "unknown signer" in reason
