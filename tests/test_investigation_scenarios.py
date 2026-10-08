"""End-to-end investigation behaviour: 4 real agents, real gRPC + mTLS, simulated telemetry.

Covers the three required scenarios, the on/off switch, and the two safety properties that
matter most: a genuine attack is NOT delayed, and a lying agent cannot corroborate itself.
"""
import time

import pytest

import local_demo
from world import LocalCluster, fast_config, sim_investigation


def cluster(port, enabled=True, **kw):
    c = LocalCluster(base_port=port, cfg=fast_config(port, sim_investigation(enabled=enabled, **kw))).start()
    time.sleep(1.5)
    return c


def contain_time(enabled):
    """Seconds from the start of the slow-burn until 3 agents leave HEALTHY."""
    c = cluster(51011 if enabled else 51021, enabled)
    try:
        grow = lambda t: 9 + 0.15 * t                       # noqa: E731
        c.world.inject_connections("C", grow, observers={"A", "B"})
        c.world.inject_connections("C", grow, observers={"C", "D"}, start_after=5)
        t0 = time.time()
        assert c.wait_until(lambda: sum(a.states["C"].phase != "HEALTHY" for a in c.agents.values()) >= 3, 60)
        return time.time() - t0
    finally:
        c.stop()


def test_scenario_transient_blip_is_closed_as_false_positive():
    assert local_demo.transient_blip(True)


def test_scenario_slow_burn_is_confirmed_and_contained():
    assert local_demo.slow_burn(True)


def test_scenario_ambiguous_goes_to_watch_with_a_review_flag():
    assert local_demo.ambiguous(True)


def test_switch_off_scenarios_behave_like_the_system_without_investigation():
    assert local_demo.transient_blip(False)
    assert local_demo.ambiguous(False)


def test_investigation_makes_slow_burn_containment_faster_than_without():
    on, off = contain_time(True), contain_time(False)
    print(f"slow-burn containment: investigation ON {on:.1f}s, OFF {off:.1f}s")
    assert on < off - 3, f"expected a clear speed-up (on {on:.1f}s, off {off:.1f}s)"


def test_genuine_attack_is_not_delayed_by_the_investigation_machinery():
    c = cluster(51031)
    try:
        c.mark("app-compromise", "C")
        c.world.attack("C")
        assert c.wait_until(lambda: all(a.states["C"].phase != "HEALTHY" for a in c.agents.values()), 15)
        assert all(a.inv.counters["started"] == 0 for a in c.agents.values()), \
            "a clearly corroborated attack must go straight to the quorum, with no investigation"
        assert c.wait_until(lambda: c.agents["B"].metrics.summary()[0]["tti_s"] is not None, 15)
        row = c.agents["B"].metrics.summary()[0]
        assert row["tti_s"] < 4 and row["investigations"] == 0
    finally:
        c.stop()


def test_disabled_investigation_leaves_no_trace():
    c = cluster(51041, enabled=False)
    try:
        c.mark("transient-blip", "C")
        c.world.inject_connections("C", lambda t: 40, observers={"A", "B"}, duration=2.5)
        time.sleep(9)
        for a in c.agents.values():
            assert not a.events.by_category("INVESTIGATION")
            assert a.inv.status()["active"] == [] and a.inv.counters["started"] == 0
            assert a.metrics.summary()[0]["investigations"] == 0
        assert all(a.states["C"].epoch == 0 for a in c.agents.values())
    finally:
        c.stop()


def test_peers_exchange_signed_readings_over_grpc_and_record_them():
    c = cluster(51051)
    try:
        c.mark("transient-blip", "C")
        c.world.inject_connections("C", lambda t: 40, observers={"A", "B"}, duration=2.5)
        assert c.wait_until(lambda: all(a.inv.recent for a in c.agents.values()), 30)
        rec = c.agents["B"].inv.recent[-1]
        assert set(rec["peers"]) >= {"A", "C", "D"}
        for peer, v in rec["peers"].items():
            assert v["signed_digest"] and v["readings"], peer
        cats = {e["details"].get("event") for e in c.agents["B"].events.by_category("INVESTIGATION")}
        assert {"started", "peer_response", "closed"} <= cats | {"adopted"} or {"adopted", "peer_response", "closed"} <= cats
        # every agent that took part ended with the same verdict
        assert {a.inv.recent[-1]["outcome"] for a in c.agents.values()} == {"FALSE_POSITIVE"}
    finally:
        c.stop()


def test_a_lying_agent_cannot_corroborate_its_own_accusation():
    c = cluster(51061)
    try:
        c.compromise_agent("A", "B")                   # A fabricates evidence against healthy B, and
        assert c.wait_until(lambda: all(c.agents[n].agent_trust.get("A") < 50 for n in "BCD"), 40)
        time.sleep(2)
        assert all(a.states["B"].epoch == 0 for a in c.agents.values()), "healthy B was isolated"
        for n in "BCD":
            outcomes = [r["outcome"] for r in c.agents[n].inv.recent if r["target"] == "B"]
            assert "CORROBORATED" not in outcomes, (n, outcomes)
            assert not c.agents[n].inv.auth, "a liar must never earn a containment authorisation"
        # at least one honest agent investigated A's claim and found it contradicted
        assert any(r["outcome"] == "FALSE_POSITIVE" for n in "BCD" for r in c.agents[n].inv.recent
                   if r["target"] == "B")
        c.restore_agent("A")
    finally:
        c.stop()
