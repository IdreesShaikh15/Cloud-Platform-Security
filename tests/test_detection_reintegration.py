"""Threshold detection, stage gating and NetworkPolicy generation."""
import pytest

from resilience.detection import Detector, ramp
from resilience.evidence import ObsType
from resilience.monitoring import Snapshot
from resilience.reintegration import (STAGES, STAGE_THRESHOLDS, can_advance, network_policy,
                                      next_stage)

BASE = {"app.py": "1" * 64, "static/index.html": "2" * 64}


def snap(target="C", **kw):
    d = dict(target=target, time=kw.pop("time", 100.0), reachable=True, healthy=True,
             instance_id="i1", pod_ip="10.0.0.3", outbound_connections=1, tx_bytes=0,
             processes=[{"pid": 1, "comm": "python", "exe": "/usr/local/bin/python3"}],
             file_hashes=dict(BASE))
    d.update(kw)
    return Snapshot(**d)


def det():
    return Detector(BASE, ["python", "python3"])


def test_ramp():
    assert ramp(5, 8, 16) == 0
    assert ramp(9, 8, 16) == pytest.approx(0.531, abs=1e-3)
    assert ramp(100, 8, 16) == 1.0


def test_clean_snapshot_has_no_detections():
    assert det().analyze({"C": snap()}, 100.0)["C"] == []


def test_network_connections_and_rate():
    d = det()
    d.analyze({"C": snap(tx_bytes=0, time=100.0)}, 100.0)
    out = d.analyze({"C": snap(outbound_connections=40, tx_bytes=3_000_000, time=101.0)}, 101.0)["C"]
    net = [x for x in out if x.observation == ObsType.NETWORK][0]
    assert net.confidence == 1.0


def test_suspicious_and_unknown_processes():
    procs = [{"pid": 1, "comm": "python", "exe": "/usr/local/bin/python3"},
             {"pid": 9, "comm": "xmrig", "exe": "/tmp/xmrig"}]
    out = det().analyze({"C": snap(processes=procs)}, 100.0)["C"]
    assert out[0].observation == ObsType.PROCESS and out[0].confidence == 0.95
    procs[1] = {"pid": 9, "comm": "bash", "exe": "/bin/bash"}
    assert det().analyze({"C": snap(processes=procs)}, 100.0)["C"][0].confidence == 0.6


def test_file_integrity():
    out = det().analyze({"C": snap(file_hashes={**BASE, "app.py": "f" * 64})}, 100.0)["C"]
    assert out[0].observation == ObsType.FILE_INTEGRITY and out[0].confidence == 0.95
    out = det().analyze({"C": snap(file_hashes={**BASE, "backdoor.py": "0" * 64})}, 100.0)["C"]
    assert out[0].confidence == 0.7


def test_auth_failures_attributed_to_source_pod():
    d = det()
    auth0 = snap("B", pod_ip="10.0.0.2", auth_failures_by_ip={"10.0.0.3": 0})
    d.analyze({"B": auth0, "C": snap()}, 100.0)
    auth1 = snap("B", pod_ip="10.0.0.2", auth_failures_by_ip={"10.0.0.3": 60}, time=105.0)
    out = d.analyze({"B": auth1, "C": snap(time=105.0)}, 105.0)
    assert [x.observation for x in out["C"]] == [ObsType.AUTH]
    assert out["B"] == []   # the victim service is not blamed


def test_sensitivity_lowers_threshold():
    s = snap(outbound_connections=7)
    assert det().analyze({"C": s}, 100.0)["C"] == []
    assert det().analyze({"C": s}, 100.0, {"C": 0.7})["C"][0].observation == ObsType.NETWORK


def test_stage_order_and_gating():
    assert STAGES[0] == "QUARANTINE" and STAGES[-1] == "FULL"
    assert [STAGE_THRESHOLDS[s] for s in STAGES] == sorted(STAGE_THRESHOLDS[s] for s in STAGES)
    assert next_stage("PEER_VALIDATED") == "FULL" and next_stage("FULL") is None
    assert not can_advance("QUARANTINE", 5, 10, 50, 0)     # dwell not met
    assert not can_advance("QUARANTINE", 11, 10, 10, 0)    # trust below 20
    assert not can_advance("QUARANTINE", 11, 10, 50, 0.9)  # anomaly present
    assert can_advance("QUARANTINE", 11, 10, 25, 0)


def test_network_policies_per_stage():
    q = network_policy("records-api", "QUARANTINE", "healthcare", "resilience", {"x": "y"})
    assert q["spec"]["egress"] == [] and len(q["spec"]["ingress"]) == 1
    assert q["metadata"]["annotations"]["resilience.io/stage"] == "QUARANTINE"
    assert q["spec"]["podSelector"] == {"matchLabels": {"app": "records-api"}}
    r = network_policy("records-api", "RESTRICTED", "healthcare", "resilience")
    assert len(r["spec"]["ingress"]) == 1 and len(r["spec"]["egress"]) == 2
    m = network_policy("records-api", "MONITORED", "healthcare", "resilience")
    assert len(m["spec"]["ingress"]) == 2
    assert network_policy("records-api", "FULL", "healthcare", "resilience") is None
