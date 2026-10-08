"""Vote-excluded agents: their evidence is rejected entirely, they count toward no quorum, and trust
returns only through what peers observe over time (never by self-declaration, never by a restart)."""
import json
import os
from collections import deque

import pytest

from resilience.agent import ResilienceAgent
from resilience.evidence import Action, Evidence, ObsType, Vote, seal
from resilience.quorum import weighted_score
from test_certificate import Cluster4

NET = ObsType.NETWORK


def evidence(origin, target="C", conf=0.9, ts=None):
    return Evidence(origin=origin, target=target, observation=NET, confidence=conf, timestamp=ts)


def deliver(c, sender, ev, to="A"):
    env = seal(c.signers[sender], ev)
    return c.agents[to].on_envelope(env, sender)


def test_excluded_agents_evidence_is_rejected_entirely():
    c = Cluster4()
    a = c.agents["A"]
    a.agent_trust.set("B", 10.0)                                     # A has excluded B (below 40)
    accepted, why = deliver(c, "B", evidence("B", ts=c.t[0]))
    assert not accepted and "vote-excluded" in why
    assert a.pool.recent(c.t[0], 30, origin="B") == [], "nothing from B may enter the scoring pool"
    assert weighted_score(a.pool.recent(c.t[0], 30, target="C"), a.trust_of, c.cfg.quorum).total == 0.0
    assert len(a.excluded_pool.recent(c.t[0], 30, origin="B")) == 1  # kept ONLY to keep judging B
    ev = [e for e in a.events.by_category("FLAG") if e["details"].get("flag") == "EVIDENCE_REJECTED"]
    assert ev and "nothing it says counts" in ev[0]["summary"]
    # a healthy sender is still accepted
    assert deliver(c, "C", evidence("C", ts=c.t[0]))[0]
    assert len(a.pool.recent(c.t[0], 30, origin="C")) == 1


def test_it_is_rejected_not_down_weighted_even_when_trust_is_just_below_the_line():
    c = Cluster4()
    a = c.agents["A"]
    a.agent_trust.set("B", 39.9)
    assert not deliver(c, "B", evidence("B", ts=c.t[0]))[0]
    a.agent_trust.set("B", 40.0)                                     # back on the line: counted again
    assert deliver(c, "B", evidence("B", ts=c.t[0]))[0]


def test_excluded_agents_vote_does_not_count_toward_any_quorum():
    c = Cluster4()
    a = c.agents["A"]
    a.agent_trust.set("B", 10.0)
    for v in "ABC":
        a.votes.add(Vote(voter=v, target="C", action=Action.CONTAIN, epoch=1))
    assert a.votes.new_commits(a.eligible_voter) == [], "A, B(excluded), C is only two eligible votes"
    a.votes.add(Vote(voter="D", target="C", action=Action.CONTAIN, epoch=1))
    commits = a.votes.new_commits(a.eligible_voter)
    assert commits and commits[0].voters == ["A", "C", "D"]
    # and it can not co-sign a certificate that names it
    c2 = Cluster4()
    a2 = c2.agents["A"]
    a2.agent_trust.set("B", 10.0)
    c2.cast(voters="ABC", to="A")
    assert a2.certs.statements == {} or not a2.certs.signed


def test_it_keeps_being_judged_and_regains_trust_only_through_peers_observations_over_time():
    c = Cluster4()
    a = c.agents["A"]
    cfg = c.cfg
    a.local_hist["C"] = deque([(c.t[0], 0.0)], maxlen=300)           # A itself sees C as perfectly normal
    a.agent_trust.set("B", 30.0)                                      # B is excluded
    # B keeps lying: the claims are rejected from scoring but STILL contradicted -> trust keeps falling
    t = c.t[0]
    for step in range(12):
        t += 1.0
        c.t[0] = t
        a.local_hist["C"].append((t, 0.0))
        deliver(c, "B", evidence("B", ts=t - 6))                      # a claim old enough to be judged
        a._update_trust(t, 1.0)
    assert a.agent_trust.get("B") < 30.0, "a liar that stays excluded keeps losing trust"
    low = a.agent_trust.get("B")
    # B stops. Its old claims age out; trust returns slowly (0.5/s), never faster, never by itself
    c.t[0] += cfg.timers.evidence_window_s + 2
    a.excluded_pool._items.clear()
    before = a.agent_trust.get("B")
    for _ in range(10):
        c.t[0] += 1.0
        a._update_trust(c.t[0], 1.0)
    gained = a.agent_trust.get("B") - before
    assert gained == pytest.approx(10 * cfg.trust.agent_recover_per_s)
    assert a.agent_trust.get("B") < cfg.trust.vote_min_trust, "10 quiet seconds do not make B eligible again"
    assert low < 40


def test_nothing_B_sends_can_raise_its_own_trust():
    c = Cluster4()
    a = c.agents["A"]
    a.agent_trust.set("B", 10.0)
    before = a.agent_trust.get("B")
    for conf in (0.0, 0.1, 0.99):
        deliver(c, "B", evidence("B", conf=conf, ts=c.t[0]))
        deliver(c, "B", evidence("B", target="B", conf=0.0, ts=c.t[0]))     # "I am fine" about itself
    a.votes.add(Vote(voter="B", target="B", action=Action.VALIDATE, epoch=0))
    assert a.agent_trust.get("B") <= before, "no message type moves trust up; only elapsed time + observation"


def test_trust_state_survives_a_restart_so_exclusion_is_not_wiped(tmp_path):
    c = Cluster4()
    path = str(tmp_path / "trust.json")
    a = ResilienceAgent(c.cfg, "A", c.signers["A"], c.reg, c.agents["A"].telemetry, c.backend,
                        clock=lambda: c.t[0], trust_state_path=path)
    a.agent_trust.set("B", 12.0)
    a.agent_trust.set("C", 77.0)
    a._save_trust(c.t[0])
    assert json.load(open(path))["agent_trust"]["B"] == 12.0
    again = ResilienceAgent(c.cfg, "A", c.signers["A"], c.reg, c.agents["A"].telemetry, c.backend,
                            clock=lambda: c.t[0] + 5, trust_state_path=path)        # "the pod restarted"
    assert again.agent_trust.get("B") == 12.0 and again.agent_trust.get("C") == 77.0
    assert not again.eligible_voter("B"), "a restart must not give an excluded agent a clean slate"
    assert again.agent_trust.get("D") == 100.0


def test_unreadable_trust_state_falls_back_to_defaults_without_crashing(tmp_path):
    c = Cluster4()
    path = tmp_path / "trust.json"
    path.write_text("{ definitely not json")
    a = ResilienceAgent(c.cfg, "A", c.signers["A"], c.reg, c.agents["A"].telemetry, c.backend,
                        clock=lambda: c.t[0], trust_state_path=str(path))
    assert a.agent_trust.get("B") == 100.0


def test_a_restarted_excluded_agent_does_not_regain_standing_with_its_peers():
    """B restarts (fresh process, same keys). Its peers' ledgers are THEIR memory; B cannot touch them."""
    c = Cluster4()
    for n in "ACD":
        c.agents[n].agent_trust.set("B", 10.0)
    c.agents["B"] = ResilienceAgent(c.cfg, "B", c.signers["B"], c.reg, c.agents["B"].telemetry, c.backend,
                                    clock=lambda: c.t[0])
    c.agents["B"].transport = c.agents["A"].transport.__class__(c.agents, "B")
    for n in "ACD":
        assert not c.agents[n].eligible_voter("B")
        assert not deliver(c, "B", evidence("B", ts=c.t[0]), to=n)[0]
