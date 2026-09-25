"""Fault tolerance to a crashed resilience node (agent-crash) and to a crashed
centralized controller (controller-crash) - the structural argument for why
the distributed design degrades gracefully and the centralized one doesn't.

Each test gets its own fresh cluster/controller (rather than reusing the
shared fixture in test_integration_sim.py) so a crashed node never lingers
into another test.
"""
import time
import uuid

import grpc
import pytest

from resilience.proto import resilience_pb2 as pb
from world import LocalBaseline, LocalCluster


@pytest.fixture()
def crash_cluster():
    c = LocalCluster(base_port=50551).start()
    time.sleep(1.5)
    yield c
    c.stop()


def test_agent_crash_link_actually_goes_down(crash_cluster):
    c = crash_cluster
    c.agents["D"].tick()
    c.crash_agent("D")
    with pytest.raises(grpc.RpcError):
        c.agents["A"].transport.stubs["D"].Ping(pb.PingRequest(from_node="A"), timeout=2.0)


def test_agent_crash_still_reaches_3_of_4_quorum_and_completes_pipeline(crash_cluster):
    """The headline claim: kill one resilience node, then genuinely compromise
    an app workload. The surviving 3 (>= 2f+1 for f=1) still detect, isolate,
    recover, validate and fully reintegrate - without the crashed node ever
    voting."""
    c = crash_cluster
    c.agents["D"].tick()
    c.crash_agent("D")

    epoch_before = c.agents["A"].states["C"].epoch
    c.mark("agent-crash", "C", attacker="D")
    c.world.attack("C")

    assert c.wait_until(lambda: all(c.agents[n].states["C"].phase != "HEALTHY" for n in "ABC"), 15), \
        "surviving agents never contained the compromised workload"

    decision = c.agents["B"].states["C"].last_decision
    assert decision is not None and decision["action"] == "CONTAIN"
    assert len(decision["voters"]) >= 3, "quorum requires 3 of 4 (BFT, n=4, f=1)"
    assert "D" not in decision["voters"], "the crashed node cannot have voted"
    assert set(decision["voters"]) <= {"A", "B", "C"}

    assert c.wait_until(
        lambda: all(c.agents[n].states["C"].phase == "HEALTHY"
                   and c.agents[n].states["C"].epoch == epoch_before + 1 for n in "ABC"),
        90), "surviving agents never completed recovery/validation/reintegration"

    # D, still crashed, must not have been required or silently counted.
    assert c.agents["A"].states["C"].epoch == epoch_before + 1
    row = [r for r in c.agents["B"].metrics.summary() if r["scenario"] == "agent-crash"][0]
    for k in ("ttd_s", "tti_s", "ttr_s", "ttv_s", "ttf_s"):
        assert row[k] is not None and row[k] >= 0
    assert row["false_isolation"] is False


def test_controller_crash_produces_no_detection_or_response():
    """The contrasting baseline case: kill the single centralized controller,
    then genuinely compromise an app workload. Nothing detects or responds -
    the single point of failure the distributed design avoids."""
    b = LocalBaseline().start()
    time.sleep(1)
    b.crash()

    marker = {"id": uuid.uuid4().hex[:8], "scenario": "controller-crash", "target": "C",
             "attacker": None, "injected_at": time.time()}
    b.backend.marker = marker
    b.ctl.metrics.set_marker(marker)  # the crashed controller's own loop never picks this up
    b.world.attack("C")
    time.sleep(8)  # far longer than the ~1s TTD a live controller shows in test_integration_sim.py

    assert b.ctl.states["C"].phase == "HEALTHY"
    assert b.ctl.states["C"].epoch == 0
    assert b.backend.policies == {}, "no NetworkPolicy should have been created"
    row = b.ctl.metrics.summary()[0]
    assert row["scenario"] == "controller-crash"
    for k in ("ttd_s", "tti_s", "ttr_s", "ttv_s", "ttf_s"):
        assert row[k] is None, f"{k} should be unset - the crashed controller never responded"
    b.stop()


def test_controller_resumes_detecting_after_recovering_from_crash():
    """Once the controller is un-crashed, it picks back up and handles the
    same attack normally - confirming the crash is the only reason nothing
    happened above, not some other break."""
    b = LocalBaseline().start()
    time.sleep(1)
    b.crash()
    b.backend.marker = {"id": "still-crashed", "scenario": "controller-crash", "target": "C",
                        "attacker": None, "injected_at": time.time()}
    b.world.attack("C")
    time.sleep(3)
    assert b.ctl.states["C"].epoch == 0

    b._crashed.clear()  # controller "restarts"
    end = time.time() + 20
    while time.time() < end and b.ctl.states["C"].epoch < 1:
        time.sleep(0.5)
    assert b.ctl.states["C"].epoch >= 1, "controller never resumed detecting after un-crashing"
    b.stop()
