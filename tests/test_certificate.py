"""Quorum certificates: what is valid, and every way it can be invalid (docs/SECURITY.md)."""
import base64
import time

import pytest

from resilience.agent import ResilienceAgent
from resilience.certificate import (ANNOTATION, Certificate, CertificateError, Expect, QC_KIND,
                                    Statement, build_statement, evidence_digest, verify_certificate)
from resilience.crypto import KeyRegistry, Signer
from resilience.evidence import Action, Vote
from resilience.proto import resilience_pb2 as pb
from resilience.response import FakeBackend
from world import FakeTelemetry, FakeWorld, fast_config

NOW = 1_000_000.0
NODES = "ABCD"


@pytest.fixture()
def keys():
    signers = {n: Signer.generate(n) for n in NODES}
    return signers, KeyRegistry({n: s.public_key() for n, s in signers.items()})


def stmt(**kw):
    d = dict(workload="records-api", target="C", action="CONTAIN", stage="", epoch=1,
             evidence_digest="d" * 64, expires_ms=int((NOW + 300) * 1000))
    d.update(kw)
    return Statement(**d)


def cert(st, signers, names="ABC", sign_bytes=None):
    raw = st.canonical()
    return Certificate(raw, [(n, signers[n].sign(QC_KIND, sign_bytes or raw)) for n in names])


def ok(c, reg, **kw):
    kw.setdefault("now", NOW)
    return verify_certificate(c, reg, **kw)


# --------------------------------------------------------------------------- valid
def test_a_valid_certificate_is_three_distinct_signatures_over_one_statement(keys):
    signers, reg = keys
    good, why = ok(cert(stmt(), signers), reg, expect=Expect("records-api", "CONTAIN", "", 1))
    assert good, why
    assert ok(cert(stmt(), signers, "ABCD"), reg)[0]                 # four signers is fine too


# --------------------------------------------------------------------------- the rejection cases
def test_rejects_too_few_signatures(keys):
    signers, reg = keys
    assert "need 3" in ok(cert(stmt(), signers, "AB"), reg)[1]
    assert "no signatures" in ok(Certificate(stmt().canonical(), []), reg)[1]


def test_rejects_duplicate_signers(keys):
    signers, reg = keys
    c = cert(stmt(), signers, "AAB")                                  # A twice, B once: "3 signatures"
    good, why = ok(c, reg)
    assert not good and "duplicate signer" in why
    assert not ok(cert(stmt(), signers, "AAAA"), reg)[0]


def test_rejects_invalid_signatures(keys):
    signers, reg = keys
    c = cert(stmt(), signers)
    c.signatures[1] = ("B", b"\x00" * 64)
    good, why = ok(c, reg)
    assert not good and "invalid signature from B" in why
    # every signature must be over the SAME bytes: C signs a different statement
    c2 = cert(stmt(), signers, "AB")
    c2.signatures.append(("C", signers["C"].sign(QC_KIND, stmt(epoch=2).canonical())))
    assert "invalid signature from C" in ok(c2, reg)[1]


def test_rejects_unknown_peers(keys):
    signers, reg = keys
    rogue = Signer.generate("Z")                                      # a real key, but not in the registry
    c = cert(stmt(), signers, "AB")
    c.signatures.append(("Z", rogue.sign(QC_KIND, stmt().canonical())))
    good, why = ok(c, reg)
    assert not good and "unknown peer 'Z'" in why


def test_rejects_expired_certificates(keys):
    signers, reg = keys
    c = cert(stmt(expires_ms=int((NOW - 1) * 1000)), signers)
    good, why = ok(c, reg)
    assert not good and "expired" in why
    assert ok(c, reg, now=NOW - 100)[0]                               # the same certificate was fine earlier


def test_rejects_implausibly_long_lived_certificates(keys):
    signers, reg = keys
    c = cert(stmt(expires_ms=int((NOW + 86400) * 1000)), signers)
    assert "implausible expiry" in ok(c, reg, max_ttl_s=900)[1]


def test_rejects_replayed_certificates(keys):
    signers, reg = keys
    c = cert(stmt(), signers)
    used = set()
    assert ok(c, reg, consumed=used)[0]
    used.add(c.statement.digest())                                    # the caller recorded its use
    good, why = ok(c, reg, consumed=used)
    assert not good and "replayed" in why


def test_rejects_certificates_for_a_different_target_action_or_stage(keys):
    signers, reg = keys
    c = cert(stmt(), signers)
    assert "workload 'records-api', not 'database'" in ok(c, reg, expect=Expect("database"))[1]
    assert "authorises CONTAIN, not VALIDATE" in ok(c, reg, expect=Expect(action="VALIDATE"))[1]
    adv = cert(stmt(action="ADVANCE_STAGE", stage="RESTRICTED"), signers)
    assert "stage 'RESTRICTED', not 'FULL'" in ok(adv, reg, expect=Expect(action="ADVANCE_STAGE", stage="FULL"))[1]
    assert ok(adv, reg, expect=Expect("records-api", "ADVANCE_STAGE", "RESTRICTED", 1))[0]
    # a list of acceptable expectations: any one may match
    assert ok(c, reg, expect=[Expect(action="VALIDATE"), Expect(action="CONTAIN")])[0]


def test_rejects_certificates_for_a_superseded_incident_version(keys):
    signers, reg = keys
    old = cert(stmt(epoch=1), signers)
    good, why = ok(old, reg, current_epoch=2)
    assert not good and "superseded" in why
    assert "incident version 1, not 2" in ok(old, reg, expect=Expect(epoch=2))[1]
    assert ok(old, reg, current_epoch=1)[0]


def test_revoked_signers_do_not_count(keys):
    signers, reg = keys
    c = cert(stmt(), signers, "ABC")
    assert ok(c, reg, revoked=["C"])[1].startswith("only 2 valid")
    assert ok(cert(stmt(), signers, "ABCD"), reg, revoked=["C"])[0]


def test_rejects_malformed_and_non_canonical_statements(keys):
    signers, reg = keys
    good = cert(stmt(), signers)
    for raw in (b"not json", b'{"a":1}', stmt().canonical().replace(b'"epoch":1', b'"epoch": 1')):
        c = Certificate(raw, good.signatures)
        assert not ok(c, reg)[0]
    with pytest.raises(CertificateError):
        Certificate.from_b64("%%%not base64%%%")
    with pytest.raises(CertificateError):
        Statement.from_bytes(stmt(action="DELETE_EVERYTHING").canonical())


def test_signatures_are_domain_separated(keys):
    """An evidence or vote signature over the same bytes can never be reused as a certificate share."""
    signers, reg = keys
    raw = stmt().canonical()
    c = Certificate(raw, [(n, signers[n].sign("vote", raw)) for n in "ABC"])
    assert "invalid signature" in ok(c, reg)[1]


def test_certificate_survives_the_annotation_round_trip(keys):
    signers, reg = keys
    c = cert(stmt(), signers)
    text = c.to_b64()
    assert ANNOTATION == "resilience.io/qc" and len(text) < 2000      # fits comfortably in an annotation
    back = Certificate.from_b64(text)
    assert back.statement == c.statement and ok(back, reg)[0]


# --------------------------------------------------------------------------- the honest boundary
def test_one_compromised_agent_cannot_make_a_certificate_but_two_can(keys):
    """f = 1 of n = 4. A lone agent has one signature (< 3). Two compromised agents plus ONE
    honest signature is a valid certificate: that is the BFT bound, not a bug."""
    signers, reg = keys
    evil = stmt(workload="auth-service", target="B", epoch=7)
    lone = cert(evil, signers, "A")
    assert not ok(lone, reg)[0]
    # the lone agent signs three times under different names? every name is checked against its key
    forged = Certificate(evil.canonical(), [("A", signers["A"].sign(QC_KIND, evil.canonical())),
                                            ("B", signers["A"].sign(QC_KIND, evil.canonical())),
                                            ("C", signers["A"].sign(QC_KIND, evil.canonical()))])
    assert "invalid signature" in ok(forged, reg)[1]
    two_plus_one = cert(evil, signers, "ABC")      # A and B compromised; C honest but deceived/coerced
    assert ok(two_plus_one, reg)[0], "2 of 4 compromised is beyond what BFT tolerates"


# --------------------------------------------------------------------------- building the statement
def vote(voter, ids=("e1", "e2"), ts=NOW, epoch=1, action=Action.CONTAIN, stage=""):
    return Vote(voter=voter, target="C", action=action, epoch=epoch, stage=stage, timestamp=ts,
                evidence_ids=tuple(ids))


def test_statement_is_deterministic_and_built_from_the_three_lowest_voters():
    votes = [vote("D", ts=NOW + 3), vote("B", ts=NOW + 1), vote("A", ts=NOW), vote("C", ts=NOW + 2)]
    s1, voters1 = build_statement(votes, "records-api", 300)
    s2, voters2 = build_statement(list(reversed(votes)), "records-api", 300)
    assert s1 == s2 and voters1 == voters2 == ("A", "B", "C")
    assert s1.expires_ms == int((NOW + 2 + 300) * 1000)                # newest of A,B,C + ttl; D is not in it
    assert s1.evidence_digest == evidence_digest(["e1", "e2"]) and s1.epoch == 1 and s1.workload == "records-api"
    # a different evidence set changes the digest, hence the certificate
    other, _ = build_statement([vote(v, ids=("e1", "e9")) for v in "ABC"], "records-api", 300)
    assert other.evidence_digest != s1.evidence_digest
    with pytest.raises(CertificateError):
        build_statement(votes[:2], "records-api", 300)
    with pytest.raises(CertificateError):
        build_statement([vote("A"), vote("B"), vote("C", epoch=2)], "records-api", 300)


# --------------------------------------------------------------------------- the share exchange (4 real agents)
class ShareNet:
    """Loopback transport: delivers every broadcast envelope to the other agents."""

    def __init__(self, agents, me):
        self.agents, self.me, self.sent = agents, me, []

    def broadcast(self, env, wait=False):
        self.sent.append(env.kind)
        for nid, a in self.agents.items():
            if nid != self.me:
                a.on_envelope(env, self.me)
        return []

    def peer_status(self):
        return {}


class Cluster4:
    def __init__(self):
        self.t = [time.time()]
        self.cfg = fast_config(50991)
        signers = {n: Signer.generate(n) for n in NODES}
        self.signers = signers
        self.reg = KeyRegistry({n: s.public_key() for n, s in signers.items()})
        self.backend = FakeBackend()
        self.agents = {n: ResilienceAgent(self.cfg, n, signers[n], self.reg, FakeTelemetry(FakeWorld(), n),
                                          self.backend, clock=lambda: self.t[0]) for n in NODES}
        for n, a in self.agents.items():
            a.transport = ShareNet(self.agents, n)

    def cast(self, voters="ABCD", target="C", epoch=1, ids=("e1", "e2"), to="ABCD"):
        for v in voters:
            vt = Vote(voter=v, target=target, action=Action.CONTAIN, epoch=epoch, timestamp=self.t[0],
                      evidence_ids=tuple(ids))
            for n in to:
                self.agents[n].votes.add(vt)

    def commit(self, nodes="ABCD"):
        for n in nodes:
            a = self.agents[n]
            for c in a.votes.new_commits(a.eligible_voter):
                a._on_commit(c, self.t[0])


def test_four_agents_exchange_shares_and_each_holds_the_same_valid_certificate():
    c = Cluster4()
    c.cast()
    c.commit()
    key = "CONTAIN:C:1:"
    certs = {n: a.certs.get(key) for n, a in c.agents.items()}
    assert all(certs.values()), "every agent should hold a certificate"
    statements = {x.statement_bytes for x in certs.values()}
    assert len(statements) == 1, "all agents sign byte-identical canonical data"
    for n, x in certs.items():
        assert ok(x, c.reg, now=c.t[0], quorum=3, expect=Expect("records-api", "CONTAIN", "", 1))[0]
        assert len(set(x.signers())) >= 3
    assert c.agents["A"].certs.status()["complete"] >= 1


def test_certificate_needs_three_agents_two_alive_never_complete():
    c = Cluster4()
    c.cast(voters="AB", to="AB")                      # only 2 agents voted at all
    for n in "AB":
        for cm in c.agents[n].votes.new_commits(c.agents[n].eligible_voter):
            raise AssertionError("two votes must not commit")
    # even if A and B sign a statement by force, two signatures are not a certificate
    st = stmt(expires_ms=int((c.t[0] + 300) * 1000))
    assert not ok(cert(st, c.signers, "AB"), c.reg, now=c.t[0])[0]


def test_an_agent_that_did_not_vote_never_signs():
    c = Cluster4()
    c.cast(voters="ABC", to="ABCD")                   # D received the votes but did not vote itself
    c.commit("ABC")
    d = c.agents["D"]
    assert "D" not in (d.certs.get("CONTAIN:C:1:").signers() if d.certs.get("CONTAIN:C:1:") else [])
    assert d.certs.signed == set()
    assert d.certs.get("CONTAIN:C:1:") is not None, "D still receives the certificate A, B, C produced"


def test_agent_refuses_to_sign_a_statement_that_does_not_match_its_own_votes():
    c = Cluster4()
    c.cast()                                          # A..D vote with evidence (e1, e2)
    lie = Statement("records-api", "C", "CONTAIN", "", 1, evidence_digest(["something", "else"]),
                    int((c.t[0] + 300) * 1000))
    msg = pb.CertShare(share_id="x", signer="B", statement=lie.canonical(),
                       statement_signature=c.signers["B"].sign(QC_KIND, lie.canonical()),
                       voters=["A", "B", "C"], timestamp=c.t[0])
    env = pb.SignedEnvelope(signer="B", kind="cert_share", payload=msg.SerializeToString(),
                            signature=c.signers["B"].sign("cert_share", msg.SerializeToString()))
    a = c.agents["A"]
    assert a.on_envelope(env, "B")[0] is True         # well-formed, accepted as a share ...
    assert lie.digest() not in a.certs.signed         # ... but A never co-signs a statement it can't reproduce
    assert a.certs.get("CONTAIN:C:1:") is None
    assert a.certs.refused


def test_forged_share_signature_is_rejected_and_penalised():
    c = Cluster4()
    c.cast()
    st = stmt(expires_ms=int((c.t[0] + 300) * 1000))
    msg = pb.CertShare(share_id="y", signer="B", statement=st.canonical(),
                       statement_signature=b"\x01" * 64, voters=["A", "B", "C"], timestamp=c.t[0])
    env = pb.SignedEnvelope(signer="B", kind="cert_share", payload=msg.SerializeToString(),
                            signature=c.signers["B"].sign("cert_share", msg.SerializeToString()))
    a = c.agents["A"]
    before = a.agent_trust.get("B")
    ok_, why = a.on_envelope(env, "B")
    assert not ok_ and "invalid statement signature" in why
    assert a.agent_trust.get("B") == before - c.cfg.trust.invalid_message_penalty


def test_share_for_another_signer_is_rejected():
    c = Cluster4()
    st = stmt(expires_ms=int((c.t[0] + 300) * 1000))
    msg = pb.CertShare(share_id="z", signer="B", statement=st.canonical(),
                       statement_signature=c.signers["B"].sign(QC_KIND, st.canonical()),
                       voters=["A", "B", "C"], timestamp=c.t[0])
    env = pb.SignedEnvelope(signer="B", kind="cert_share", payload=msg.SerializeToString(),
                            signature=c.signers["B"].sign("cert_share", msg.SerializeToString()))
    assert not c.agents["A"].on_envelope(env, "D")[0]          # delivered over D's connection, claims B
