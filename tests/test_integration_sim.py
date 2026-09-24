"""End-to-end: four real agents, real gRPC + mTLS on localhost, simulated cluster.

These take ~1 minute in total.
"""
import time

import grpc
import pytest

from resilience.peer import PeerClient, TlsMaterial
from world import LocalBaseline, LocalCluster


@pytest.fixture(scope="module")
def cluster():
    c = LocalCluster(base_port=50451).start()
    time.sleep(1.5)
    yield c
    c.stop()


def test_mtls_rejects_client_without_platform_cert(cluster, tmp_path):
    from resilience.pki import generate
    other = generate(str(tmp_path), {"A": "agent-a"})  # a *different* CA
    tls = TlsMaterial.from_dir(str(tmp_path / "agent-a"))
    client = PeerClient("A", cluster.cfg.nodes, tls, timeout_s=1.0)
    stub = client.stubs["B"]
    from resilience.proto import resilience_pb2 as pb
    with pytest.raises(grpc.RpcError):
        stub.Ping(pb.PingRequest(from_node="A"), timeout=1.0)
    assert other


def test_genuine_compromise_full_cycle(cluster):
    cluster.mark("app-compromise", "C")
    cluster.world.attack("C")
    assert cluster.wait_until(lambda: all(p != "HEALTHY" for p in cluster.phases("C").values()), 15)
    d = cluster.agents["D"].states["C"].last_decision
    assert d["action"] == "CONTAIN" and len(d["voters"]) >= 3
    assert cluster.wait_until(lambda: all(a.states["C"].phase == "HEALTHY" and a.states["C"].epoch == 1
                                          for a in cluster.agents.values()), 90)
    stages = [x[3] for x in cluster.backend.log if x[1] == "apply_stage" and x[2] == "records-api"]
    assert stages == ["QUARANTINE", "RESTRICTED", "MONITORED", "PEER_VALIDATED", "FULL"]
    row = cluster.agents["B"].metrics.summary()[0]
    for k in ("ttd_s", "tti_s", "ttr_s", "ttv_s", "ttf_s"):
        assert row[k] is not None and row[k] >= 0
    assert row["ttd_s"] <= row["tti_s"] <= row["ttr_s"] <= row["ttv_s"] <= row["ttf_s"]


def test_false_accusation_is_withheld_and_liar_loses_trust(cluster):
    cluster.compromise_agent("A", "B")
    assert cluster.wait_until(lambda: all(cluster.agents[n].agent_trust.get("A") < 50 for n in "BCD"), 40)
    assert all(a.states["B"].epoch == 0 for a in cluster.agents.values()), "healthy B was isolated!"
    assert not [x for x in cluster.backend.log if x[2] == "auth-service"]
    row = [r for r in cluster.agents["C"].metrics.summary() if r["scenario"] == "false-accusation"][0]
    assert row["false_isolation"] is False and row["time_to_flag_s"] is not None
    cluster.restore_agent("A")


def test_forged_signer_rejected_and_penalised(cluster):
    before = cluster.agents["D"].agent_trust.get("A")
    cluster.compromise_agent("A", "C", mode="forge-evidence")
    time.sleep(2)
    cluster.restore_agent("A")
    assert any(r["claimed_signer"] == "B" for r in cluster.agents["D"].rejections)
    assert cluster.agents["D"].agent_trust.get("A") < before or before == 0


def test_centralized_baseline_false_isolates():
    b = LocalBaseline()
    from resilience.simhooks import Compromise
    b.comp.set(Compromise(mode="false-accusation", target="B"))
    b.ctl.tick()
    assert b.ctl.states["B"].epoch == 1
    assert b.backend.policies["auth-service"]["stage"] == "QUARANTINE"
