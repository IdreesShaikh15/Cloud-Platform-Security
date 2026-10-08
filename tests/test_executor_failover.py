"""Executor fail-over: the rank-0 executor (agent A) is dead, so the next-ranked agent must
do the isolation/recovery. test_crash_scenarios only kills D (rank 3), which never
exercises fail-over, so this is the test that proves it."""
import time

import pytest

from world import LocalCluster


@pytest.fixture()
def cluster():
    c = LocalCluster(base_port=50651).start()
    time.sleep(1.5)
    yield c
    c.stop()


def test_dead_rank0_executor_is_replaced_by_rank1(cluster):
    c = cluster
    c.agents["A"].tick()
    c.crash_agent("A")                       # A is executor rank 0 for every action
    c.mark("agent-crash", "C", attacker="A")
    c.world.attack("C")
    assert c.wait_until(lambda: all(c.agents[n].states["C"].phase != "HEALTHY" for n in "BCD"), 20), \
        "survivors never contained C"
    assert c.wait_until(lambda: all(c.agents[n].states["C"].phase == "HEALTHY"
                                    and c.agents[n].states["C"].epoch == 1 for n in "BCD"), 90), \
        "pipeline did not complete with the rank-0 executor dead"
    # "HEALTHY" is the agents' replicated *intent* and flips when the last quorum commits;
    # the fail-over executor applies the final stage rank*stagger seconds later.
    assert c.wait_until(lambda: any(x[1] == "apply_stage" and x[3] == "FULL" for x in c.backend.log), 20)
    stages = [x[3] for x in c.backend.log if x[1] == "apply_stage" and x[2] == "records-api"]
    assert stages == ["QUARANTINE", "RESTRICTED", "MONITORED", "PEER_VALIDATED", "FULL"]
    # B (rank 1 once A is... still rank 1 by order) did the work; A, dead, did none of it.
    acted = {n: [e for e in c.agents[n].events.by_category("ACTION") if e["details"].get("action") == "isolate"]
             for n in "BCD"}
    assert acted["B"], "B should have performed the isolation as fail-over"
    assert not acted["C"] and not acted["D"], "lower-ranked agents must stay out once B has acted"


def test_recovery_waits_for_isolation_to_succeed():
    """If applying the quarantine policy keeps failing, the pod must NOT be replaced
    (the new pod would come up un-quarantined and unvalidated)."""
    c = LocalCluster(base_port=50671).start()
    time.sleep(1.5)
    try:
        real_apply = c.backend.apply_stage
        fails = {"n": 0}

        def flaky(workload, stage, annotations):
            if stage == "QUARANTINE" and fails["n"] < 4:
                fails["n"] += 1
                raise RuntimeError("API server unavailable")
            return real_apply(workload, stage, annotations)

        c.backend.apply_stage = flaky
        c.mark("app-compromise", "C")
        c.world.attack("C")
        assert c.wait_until(lambda: any(x[1] == "recover" for x in c.backend.log), 60), "never recovered"
        order = [x[1] for x in c.backend.log if x[1] in ("apply_stage", "recover")]
        assert fails["n"] == 4, "isolation should have been attempted (and failed) 4 times first"
        assert order[0] == "apply_stage", f"recover ran before isolation succeeded: {order}"
        assert c.backend.log[0][3] == "QUARANTINE"
        assert c.wait_until(lambda: all(a.states["C"].phase == "HEALTHY" for a in c.agents.values()), 90)
    finally:
        c.stop()
