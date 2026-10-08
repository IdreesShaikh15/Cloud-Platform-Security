"""Targeted investigation: decision logic, triggers, and race safety (docs/INVESTIGATION.md).

The unit tests use four real ResilienceAgent objects joined by a loopback transport and a
manual clock, so they are fast and deterministic. End-to-end behaviour over real gRPC/mTLS is
covered by the simulator scenarios in tests/test_investigation_scenarios.py.
"""
import time

import pytest

from resilience.agent import ResilienceAgent
from resilience.config import InvestigationParams, QuorumParams
from resilience.crypto import KeyRegistry, Signer
from resilience.evidence import (Evidence, InvestigateRequest, InvestigateResponse, ObsType,
                                 SignalReading, seal)
from resilience.investigation import (AMBIGUOUS, CORROBORATED, FALSE_POSITIVE, FLAPPING, GONE,
                                      MERGED, PERSIST, SUPERSEDED, UNCERTAIN, UNKNOWN, classify,
                                      decide, make_readings, Sample)
from resilience.proto import resilience_pb2 as pb
from resilience.quorum import weighted_score
from resilience.response import FakeBackend
from world import FakeTelemetry, FakeWorld, fast_config, sim_investigation

NET = ObsType.NETWORK
P = InvestigationParams()
Q = QuorumParams()


def reading(tail, pos, sig=NET, samples=10):
    return SignalReading(sig, 0.8 if pos else 0.0, 0.8 if pos else 0.0, samples, pos, tail, pos, "x")


# --------------------------------------------------------------------------- classify
def test_classify_persist_gone_flapping_unknown():
    assert classify([reading(4, 4)], [NET], P) == PERSIST
    assert classify([reading(5, 5)], [NET], P) == PERSIST
    assert classify([reading(4, 0)], [NET], P) == GONE
    assert classify([reading(4, 2)], [NET], P) == FLAPPING
    assert classify([reading(2, 2)], [NET], P) == UNKNOWN      # fewer than min_tail_samples
    assert classify([], [NET], P) == UNKNOWN
    # several signals: any one persisting is "persist"; "gone" needs ALL gone
    r = [reading(4, 0, NET), reading(4, 4, ObsType.PROCESS)]
    assert classify(r, [NET, ObsType.PROCESS], P) == PERSIST
    # a disputed signal with no usable reading means we cannot call the whole thing "gone"
    assert classify([reading(4, 0, NET)], [NET, ObsType.PROCESS], P) == FLAPPING


def test_make_readings_uses_only_valid_samples_and_a_tail():
    samples = [Sample(t=i, ok=True, conf={NET: 0.0}) for i in range(10)] + \
              [Sample(t=10 + i, ok=True, conf={NET: 0.9}) for i in range(10)]
    samples.insert(3, Sample(t=3.5, ok=False))                     # telemetry failure: not a sample
    r = make_readings(samples, [NET], P, 0.5)[0]
    assert r.samples == 20 and r.positive_samples == 10
    assert r.tail_samples == 8 and r.tail_positive == 8            # last 40%
    assert make_readings([Sample(t=1, ok=False)], [NET], P, 0.5)[0].samples == 0


# --------------------------------------------------------------------------- decide
@pytest.mark.parametrize("views,expected", [
    ({"A": PERSIST, "B": PERSIST, "C": PERSIST, "D": PERSIST}, CORROBORATED),
    ({"A": PERSIST, "B": PERSIST, "C": PERSIST, "D": GONE}, CORROBORATED),
    ({"A": PERSIST, "B": GONE, "C": GONE, "D": GONE}, FALSE_POSITIVE),       # a lone liar
    ({"A": GONE, "B": GONE, "C": GONE, "D": GONE}, FALSE_POSITIVE),
    ({"A": GONE, "B": GONE, "C": GONE}, FALSE_POSITIVE),                      # 3 of 4 is enough
    ({"A": PERSIST, "B": PERSIST, "C": GONE, "D": GONE}, AMBIGUOUS),          # 2 / 2 split
    ({"A": PERSIST, "B": FLAPPING, "C": GONE, "D": GONE}, AMBIGUOUS),
    ({"A": PERSIST, "B": PERSIST, "C": GONE, "D": UNKNOWN}, AMBIGUOUS),
])
def test_decide_table(views, expected):
    assert decide(views, Q)[0] == expected


def test_missing_data_is_never_proof_of_attack_or_of_safety():
    # Only two agents could re-measure. Both see it -> still NOT corroborated.
    assert decide({"A": PERSIST, "B": PERSIST}, Q)[0] == UNCERTAIN
    # Only two agents could re-measure. Both see nothing -> still NOT a false positive.
    assert decide({"A": GONE, "B": GONE}, Q)[0] == UNCERTAIN
    # "unknown" answers do not count as either.
    assert decide({"A": PERSIST, "B": UNKNOWN, "C": UNKNOWN, "D": UNKNOWN}, Q)[0] == UNCERTAIN
    assert decide({}, Q)[0] == UNCERTAIN
    assert "neither proof of attack nor of safety" in decide({"A": GONE}, Q)[1]


# --------------------------------------------------------------------------- harness
class Net:
    """Loopback 'network': delivers an investigate request straight to the peer agent."""

    def __init__(self, agents, me, drop=()):
        self.agents, self.me, self.drop = agents, me, set(drop)
        self.sent = []

    def investigate_async(self, nid, env, cb, timeout_s=None):
        self.sent.append(nid)
        if nid in self.drop:
            return cb(nid, None)
        ok, reason, resp = self.agents[nid].on_investigate(env, self.me)
        cb(nid, pb.InvestigateResult(accepted=ok, reason=reason, response=resp))

    def broadcast(self, env, wait=False):
        return []

    def peer_status(self):
        return {}


class Cluster:
    def __init__(self, **inv):
        self.t = [1000.0]
        self.cfg = fast_config(50990, sim_investigation(**inv))
        self.world = FakeWorld()
        signers = {n: Signer.generate(n) for n in self.cfg.nodes}
        reg = KeyRegistry({n: s.public_key() for n, s in signers.items()})
        self.backend = FakeBackend()
        self.agents = {n: ResilienceAgent(self.cfg, n, signers[n], reg, FakeTelemetry(self.world, n),
                                          self.backend, clock=lambda: self.t[0]) for n in self.cfg.nodes}
        self.nets = {}
        for n, a in self.agents.items():
            self.nets[n] = a.transport = Net(self.agents, n)
            a.inv._sample_loop = lambda inv: None        # tests take samples by hand
        self.signers = signers

    @property
    def now(self):
        return self.t[0]

    def advance(self, s):
        self.t[0] += s

    def poll(self, inv, node="A"):
        """A new polling round by `node` (time must move on, or the fresher answers would be
        treated as duplicates of the old ones)."""
        self.advance(0.5)
        self.agents[node].inv._send_round(inv, self.now)

    def evidence(self, senders, target="C", conf=0.8, obs=NET, to=None):
        for a in (to or self.agents.values()):
            for s in senders:
                a.pool.add(Evidence(origin=s, target=target, observation=obs, confidence=conf,
                                    timestamp=self.now))

    def score(self, agent, target="C"):
        ev = agent.pool.recent(self.now, 30, target=target, min_conf=0.3)
        return weighted_score(ev, agent.trust_of, agent.cfg.quorum)

    def evaluate(self, node, target="C"):
        a = self.agents[node]
        a.inv.evaluate(self.now, target, self.score(a, target))
        return a.inv.active.get(target)

    def sample_all(self, n=5, nodes="ABCD"):
        for _ in range(n):
            for node in nodes:
                inv = self.agents[node].inv.active.get("C")
                if inv:
                    self.agents[node].inv.take_sample(inv)


def started(cl, node="A", target="C", kind_evidence=("A",)):
    """Make `node` open an investigation via the real trigger path."""
    cl.evidence(kind_evidence, target=target)
    cl.evaluate(node, target)                         # starts the grace timer
    cl.advance(cl.cfg.investigation.trigger_grace_s + 1.0)
    cl.evidence(kind_evidence, target=target)         # keep the evidence "recent"
    return cl.evaluate(node, target)


# --------------------------------------------------------------------------- triggers
def test_trigger_single_sender_after_grace_only():
    cl = Cluster()
    cl.evidence(["A"])
    assert cl.evaluate("A") is None                    # first sighting: only starts the clock
    cl.advance(cl.cfg.investigation.trigger_grace_s - 0.5)
    cl.evidence(["A"])
    assert cl.evaluate("A") is None                    # still inside the grace period
    cl.advance(1.0)
    cl.evidence(["A"])
    inv = cl.evaluate("A")
    assert inv is not None and inv.trigger == "single_sender" and inv.signals == (NET,)
    assert inv.epoch == 0 and inv.deadline == inv.started_at + cl.cfg.investigation.budget_s


def test_trigger_split_view_and_uncertain_band_and_no_trigger_when_clear():
    cl = Cluster()
    cl.evidence(["A", "B"])
    cl.evaluate("A")
    cl.advance(5)
    cl.evidence(["A", "B"])
    assert cl.evaluate("A").trigger == "split_view"

    cl = Cluster()                                      # 3 agents see it, but weakly: W in [0.3, 0.6)
    cl.evidence(["A", "B", "C"], conf=0.55)
    assert 0.3 <= cl.score(cl.agents["A"]).total < 0.6
    cl.evaluate("A")
    cl.advance(5)
    cl.evidence(["A", "B", "C"], conf=0.55)
    assert cl.evaluate("A").trigger == "uncertain_band"

    cl = Cluster()                                      # clearly corroborated: NEVER delay the quorum
    cl.evidence(["A", "B", "C"], conf=0.9)
    cl.evaluate("A")
    cl.advance(10)
    cl.evidence(["A", "B", "C"], conf=0.9)
    assert cl.evaluate("A") is None


def test_no_duplicate_investigation_for_the_same_target_and_limits():
    cl = Cluster(max_concurrent=2, cooldown_s=30)
    inv = started(cl)
    assert inv is not None
    cl.evidence(["A"])
    assert cl.evaluate("A") is inv                      # a second trigger returns the SAME one
    assert len(cl.agents["A"].inv.active) == 1
    assert cl.agents["A"].inv.counters["started"] == 1
    # max concurrent: a third target cannot start while two run
    cl2 = Cluster(max_concurrent=2)
    for tgt in ("B", "C", "D"):
        cl2.evidence(["A"], target=tgt)
        cl2.evaluate("A", tgt)
    cl2.advance(5)
    for tgt in ("B", "C", "D"):
        cl2.evidence(["A"], target=tgt)
        cl2.evaluate("A", tgt)
    assert len(cl2.agents["A"].inv.active) == 2


def test_cooldown_per_target_after_an_investigation_closes():
    cl = Cluster(cooldown_s=30)
    inv = started(cl)
    cl.sample_all()
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    for a in cl.agents.values():          # every agent closes its copy, as in a real run
        a.inv.tick(cl.now)
    assert not cl.agents["A"].inv.active
    cl.evidence(["A"])
    cl.evaluate("A")
    cl.advance(5)
    cl.evidence(["A"])
    assert cl.evaluate("A") is None, "must respect the per-target cooldown"
    cl.advance(30)
    cl.evidence(["A"])
    cl.evaluate("A")
    cl.advance(5)
    cl.evidence(["A"])
    assert cl.evaluate("A") is not None, "allowed again after the cooldown"


# --------------------------------------------------------------------------- the exchange
def test_request_is_signed_peers_adopt_it_and_return_signed_readings():
    cl = Cluster()
    inv = started(cl)
    for n in "BCD":                                     # the loopback round ran at start
        assert cl.agents[n].inv.active["C"].id == inv.id, "peers adopt the initiator's investigation"
        assert cl.agents[n].inv.active["C"].initiator == "A"
    cl.sample_all(6)
    cl.poll(inv)
    assert set(inv.responses) == {"B", "C", "D"}
    for resp in inv.responses.values():
        assert resp.investigation_id == inv.id and resp.epoch == 0 and resp.readings
    assert all(len(d) == 16 for d in inv.digests.values())           # signed answers are recorded


def test_invalid_signature_response_is_rejected_and_penalised():
    cl = Cluster()
    inv = started(cl)
    cl.sample_all(3)
    ok, _, resp = cl.agents["B"].on_investigate(
        seal(cl.signers["A"], InvestigateRequest("A", inv.id, "C", 0, (NET,), cl.now, 10.0, "x", cl.now)), "A")
    forged = pb.SignedEnvelope(signer=resp.signer, kind=resp.kind, payload=resp.payload + b"x",
                               signature=resp.signature)
    before = cl.agents["A"].agent_trust.get("B")
    inv.responses.clear()
    cl.agents["A"].inv._on_result(inv, "B", pb.InvestigateResult(accepted=True, response=forged))
    assert "B" not in inv.responses
    assert inv.invalid.get("B") == 1
    assert cl.agents["A"].agent_trust.get("B") == before - cl.cfg.trust.invalid_message_penalty
    assert any(r["from"] == "B" for r in cl.agents["A"].rejections)


def test_response_signed_by_someone_else_is_rejected():
    cl = Cluster()
    inv = started(cl)
    cl.sample_all(3)
    # C relays a perfectly valid answer that D signed, pretending it is B's
    _, _, resp_d = cl.agents["D"].on_investigate(
        seal(cl.signers["A"], InvestigateRequest("A", inv.id, "C", 0, (NET,), cl.now, 10.0, "x", cl.now)), "A")
    inv.responses.clear()
    cl.agents["A"].inv._on_result(inv, "B", pb.InvestigateResult(accepted=True, response=resp_d))
    assert "B" not in inv.responses and inv.invalid.get("B") == 1


def test_duplicate_and_late_responses_are_idempotent():
    cl = Cluster()
    inv = started(cl)
    cl.sample_all(4)
    _, _, env = cl.agents["B"].on_investigate(
        seal(cl.signers["A"], InvestigateRequest("A", inv.id, "C", 0, (NET,), cl.now, 10.0, "x", cl.now)), "A")
    res = pb.InvestigateResult(accepted=True, response=env)
    inv.responses.clear()
    a = cl.agents["A"]
    trust = a.agent_trust.get("B")
    for _ in range(3):
        a.inv._on_result(inv, "B", res)                  # the same signed answer delivered 3 times
    assert list(inv.responses) == ["B"] and inv.late_or_duplicate == 2
    assert a.agent_trust.get("B") == trust, "a duplicate is not misbehaviour"
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)                                    # closes
    n = len(a.inv.recent)
    a.inv._on_result(inv, "B", res)                       # a late answer after the close
    assert len(a.inv.recent) == n and inv.late_or_duplicate == 3


def test_peers_that_never_answer_leave_an_uncertain_outcome_not_a_verdict():
    cl = Cluster()
    for n in "ABCD":
        cl.nets[n].drop = {"B", "C", "D"} - {n}          # nobody can reach anybody
    inv = started(cl)
    cl.sample_all(8, nodes="A")
    assert set(inv.silent) == {"B", "C", "D"}
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    cl.agents["A"].inv.tick(cl.now)
    assert inv.outcome == UNCERTAIN
    a = cl.agents["A"]
    assert "C" in a.inv.watch and a.inv.watch["C"].review_needed      # safe policy: watch + review
    assert a.states["C"].epoch == 0 and "C" not in a.inv.auth          # no containment authority


def test_vote_excluded_peer_mid_investigation_is_not_counted():
    cl = Cluster()
    inv = started(cl, kind_evidence=("A", "B"))
    cl.world.inject_connections("C", lambda t: 40)       # all four really see it
    cl.sample_all(8)
    cl.poll(inv)
    a = cl.agents["A"]
    assert a.inv._views(inv)[0] == {"A": PERSIST, "B": PERSIST, "C": PERSIST, "D": PERSIST}
    a.agent_trust.set("B", 10.0)                          # B and C become vote-excluded mid-way
    a.agent_trust.set("C", 10.0)
    views, excluded = a.inv._views(inv)
    assert excluded == ["B", "C"] and set(views) == {"A", "D"}
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)
    assert inv.outcome == UNCERTAIN, "two valid answers are not enough to corroborate"
    assert "vote-excluded" in inv.reason


def test_incident_resolving_first_supersedes_the_investigation():
    cl = Cluster()
    inv = started(cl)
    a = cl.agents["A"]
    a.states["C"].epoch = 1                               # a CONTAIN quorum committed first
    a.states["C"].phase = "ISOLATED"
    a.inv.on_incident_change("C", cl.now)
    assert inv.outcome == SUPERSEDED and "C" not in a.inv.active and "C" not in a.inv.auth
    # peers also drop theirs when their state changes; a late request for the old epoch is refused
    b = cl.agents["B"]
    b.states["C"].epoch = 1
    ok, reason, _ = b.on_investigate(
        seal(cl.signers["A"], InvestigateRequest("A", "inv-old", "C", 0, (NET,), cl.now, 10.0, "x", cl.now)), "A")
    assert not ok and "stale" in reason


def test_stale_result_never_authorises_containment_of_a_changed_workload():
    cl = Cluster()
    inv = started(cl, kind_evidence=("A", "B"))
    cl.world.inject_connections("C", lambda t: 40)
    cl.sample_all(8)
    cl.poll(inv)
    a = cl.agents["A"]
    a.inv.tick(cl.now + 0)                                # nothing yet
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)
    assert inv.outcome == CORROBORATED
    au = a.inv.vote_authorization("C", cl.now)
    assert au is not None and au.own_persistent and au.epoch == 0
    # (1) the incident version moved on
    a.states["C"].epoch = 1
    assert a.inv.vote_authorization("C", cl.now) is None
    # (2) the workload instance was replaced
    a.states["C"].epoch = 0
    a.inv.auth["C"] = au
    from resilience.monitoring import Snapshot
    a.snaps["C"] = Snapshot(target="C", time=cl.now, reachable=True, healthy=True, instance_id="a-different-pod")
    au.instance_id = "the-old-pod"
    assert a.inv.vote_authorization("C", cl.now) is None
    # (3) it expired
    a.inv.auth["C"] = au
    au.instance_id = ""
    cl.advance(cl.cfg.investigation.authorization_ttl_s + 1)
    assert a.inv.vote_authorization("C", cl.now) is None


def test_workload_replaced_during_investigation_discards_it():
    cl = Cluster()
    inv = started(cl)
    inv.instance_id = "pod-1"
    inv.samples.append(Sample(t=cl.now, ok=True, conf={NET: 0.0}, instance_id="pod-2"))
    inv.instance_changed = True
    cl.agents["A"].inv.tick(cl.now)
    assert inv.outcome == SUPERSEDED and "replaced" in inv.reason


def test_earlier_investigation_wins_when_two_start_at_once():
    cl = Cluster()
    # B and C both opened their own investigation of the same target
    for n in "BC":
        cl.evidence([n], to=[cl.agents[n]])
    a_inv = started(cl, node="A")
    # A's investigation was adopted by everyone; now fake a *later* duplicate from D arriving at A
    late = seal(cl.signers["D"], InvestigateRequest("D", "inv-99999999999999-D-C", "C", 0, (NET,), cl.now,
                                                    10.0, "split_view", cl.now))
    ok, reason, resp = cl.agents["A"].on_investigate(late, "D")
    assert ok and "yielding" in reason                    # A's earlier one stays; D is told so
    assert cl.agents["A"].inv.active["C"] is a_inv
    # whereas an EARLIER duplicate makes A yield (merge) and adopt it
    early = seal(cl.signers["D"], InvestigateRequest("D", "inv-00000000000001-D-C", "C", 0, (NET,), cl.now,
                                                     10.0, "split_view", cl.now))
    ok, _, _ = cl.agents["A"].on_investigate(early, "D")
    assert ok and a_inv.outcome == MERGED
    assert cl.agents["A"].inv.active["C"].id == "inv-00000000000001-D-C"
    assert len(cl.agents["A"].inv.active) == 1
    assert cl.agents["A"].inv.cooldown_until.get("C", 0) == 0, "a merge must not start a cooldown"


def test_agent_restart_mid_investigation_rejoins_on_the_next_request():
    cl = Cluster()
    inv = started(cl)
    b = cl.agents["B"]
    assert "C" in b.inv.active
    b.inv.reset()                                         # B restarts: investigations live in memory only
    assert not b.inv.active
    cl.poll(inv)           # A's next polling round
    assert b.inv.active["C"].id == inv.id, "B re-adopts the same investigation"
    assert "B" in inv.responses


def test_requests_from_a_vote_excluded_or_impersonating_agent_are_refused():
    cl = Cluster()
    b = cl.agents["B"]
    req = InvestigateRequest("A", "inv-1", "C", 0, (NET,), cl.now, 10.0, "x", cl.now)
    b.agent_trust.set("A", 10.0)
    ok, reason, _ = b.on_investigate(seal(cl.signers["A"], req), "A")
    assert not ok and "vote-excluded" in reason
    b.agent_trust.set("A", 100.0)
    before = b.agent_trust.get("D")
    ok, _, _ = b.on_investigate(seal(cl.signers["A"], req), "D")      # signed by A, delivered by D
    assert not ok and b.agent_trust.get("D") < before


def test_responder_never_samples_longer_than_its_own_budget():
    cl = Cluster()
    b = cl.agents["B"]
    greedy = InvestigateRequest("A", "inv-1", "C", 0, (NET,), cl.now, 10_000.0, "x", cl.now)
    ok, _, _ = b.on_investigate(seal(cl.signers["A"], greedy), "A")
    assert ok and b.inv.active["C"].budget_s == cl.cfg.investigation.budget_s


# --------------------------------------------------------------------------- outcomes' effects
def test_false_positive_forgets_leftover_votes_and_evidence():
    cl = Cluster()
    inv = started(cl, kind_evidence=("A", "B"))
    a = cl.agents["A"]
    from resilience.evidence import Action, Vote
    a.votes.add(Vote(voter="B", target="C", action=Action.CONTAIN, epoch=1))
    assert a.votes.pending()
    cl.sample_all(8)                                      # nothing anomalous in the world: all GONE
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)
    assert inv.outcome == FALSE_POSITIVE
    assert not a.votes.pending() and not a.pool.recent(cl.now, 30, target="C")
    assert "C" not in a.inv.watch and "C" not in a.inv.auth


def test_corroborated_authorises_a_vote_for_a_weak_but_persistent_signal():
    cl = Cluster()
    # a weak anomaly (confidence ~0.53) seen by everyone: W stays below 0.6 forever without this
    cl.world.inject_connections("C", lambda t: 9)
    cl.evidence(["A", "B", "C", "D"], conf=0.53)
    a = cl.agents["A"]
    assert cl.score(a).total < 0.6
    cl.evaluate("A")
    cl.advance(5)
    cl.evidence(["A", "B", "C", "D"], conf=0.53)
    inv = cl.evaluate("A")
    assert inv is not None and inv.trigger == "uncertain_band"
    cl.sample_all(8)
    cl.poll(inv)
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)
    assert inv.outcome == CORROBORATED
    a.latest["C"] = []                                    # the regular detector also sees it now
    from resilience.detection import Detection
    a.latest["C"] = [Detection("C", NET, 0.53, "9 conns")]
    cl.evidence(["A", "B", "C", "D"], conf=0.53)
    a._vote(cl.now)
    assert any(k.startswith("CONTAIN:C:1") for k in a.votes.pending()), "weak-but-confirmed signal is voted on"
    voted = [e for e in a.events.by_category("VOTE_CAST")]
    assert voted and "investigation" in voted[-1]["summary"]


def test_ambiguous_sets_watch_with_heightened_monitoring_and_clears_when_quiet():
    cl = Cluster(watch_clear_s=8)
    cl.world.inject_connections("C", lambda t: 40, observers={"A", "B"})
    inv = started(cl, kind_evidence=("A", "B"))
    cl.sample_all(8)
    a = cl.agents["A"]
    cl.poll(inv)
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)
    assert inv.outcome == AMBIGUOUS
    w = a.inv.watch["C"]
    assert w.review_needed and a.inv.sensitivity_overrides() == {"C": cl.cfg.investigation.watch_sensitivity}
    assert a.states["C"].epoch == 0, "AMBIGUOUS never isolates"
    cl.advance(9)                                          # no anomaly recorded locally for watch_clear_s
    a.inv.tick(cl.now)
    assert "C" not in a.inv.watch
    assert any("cleared the watch" in e["summary"] for e in a.events.by_category("INVESTIGATION"))


# --------------------------------------------------------------------------- the switch
def test_disabled_means_nothing_happens_and_requests_are_refused():
    cl = Cluster(enabled=False)
    cl.evidence(["A"])
    cl.evaluate("A")
    cl.advance(20)
    cl.evidence(["A"])
    assert cl.evaluate("A") is None
    assert cl.agents["A"].inv.sensitivity_overrides() == {} and not cl.agents["A"].inv.active
    ok, reason, _ = cl.agents["B"].on_investigate(
        seal(cl.signers["A"], InvestigateRequest("A", "inv-1", "C", 0, (NET,), cl.now, 10.0, "x", cl.now)), "A")
    assert not ok and "disabled" in reason
    assert not cl.agents["A"].events.by_category("INVESTIGATION")
    assert cl.agents["A"].inv.vote_authorization("C", cl.now) is None


def test_metrics_record_investigation_fields():
    cl = Cluster()
    a = cl.agents["A"]
    a.metrics.set_marker({"id": "m1", "scenario": "transient-blip", "target": "C", "attacker": None,
                          "injected_at": cl.now - 1})
    inv = started(cl)
    cl.sample_all(8)
    cl.advance(cl.cfg.investigation.budget_s + 0.1)
    a.inv.tick(cl.now)
    row = a.metrics.summary()[0]
    assert row["investigations"] == 1 and row["investigation_outcome"] in (FALSE_POSITIVE, AMBIGUOUS, UNCERTAIN)
    assert row["inv_start_s"] is not None and row["inv_end_s"] is not None
    assert row["inv_end_s"] >= row["inv_start_s"]
    assert row["human_review"] == (row["investigation_outcome"] in ("AMBIGUOUS", "UNCERTAIN"))
    assert row["false_isolation"] is False


def test_status_exposes_investigations_for_the_dashboard():
    cl = Cluster()
    inv = started(cl)
    st = cl.agents["A"].status()["investigations"]
    assert st["enabled"] and len(st["active"]) == 1
    row = st["active"][0]
    assert row["target"] == "C" and row["signals"] == ["NETWORK"] and 0 < row["time_left_s"] <= 10
    assert "still present" in row["question"] and row["state"] == "ACTIVE"


def test_cluster_configmap_investigation_block_loads_and_matches_defaults(tmp_path):
    import json, os
    from resilience.config import load_cluster_config
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "k8s", "resilience", "10-config.yaml")).read()
    raw = json.loads(text.split("config.json: |", 1)[1])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    cfg = load_cluster_config(str(path))
    assert cfg.investigation.enabled is True
    # the deployed values equal the code defaults, so the docs table is the single truth
    assert cfg.investigation.to_dict() == InvestigationParams().to_dict()
    assert set(raw["investigation"]) <= set(InvestigationParams().to_dict())
