"""Observability: the per-agent event log, its throttling, every event category
in the scenario that should produce it, the /status event stream and the
dashboard's collector. Nothing here changes decisions - the existing suites
(and sim/local_demo.py) verify the behaviour is unchanged."""
import importlib.util
import itertools
import json
import os
import time
import urllib.request
import uuid

import pytest

from resilience import observability as obs
from resilience.config import QuorumParams
from resilience.detection import Detector
from resilience.evidence import Evidence, ObsType
from resilience.monitoring import Snapshot
from resilience.quorum import weighted_score
from resilience.reintegration import STAGES, advance_block_reason, can_advance
from resilience.status_server import serve_status
from world import LocalBaseline, LocalCluster

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------------ EventLog unit tests
def test_event_structure_and_seq():
    log = obs.EventLog("B", Clock())
    e = log.emit(obs.OBSERVE, "Agent B sees x.", "C", {"v": 1})
    assert set(e) == {"seq", "ts", "node", "category", "target", "summary", "details"}
    assert (e["node"], e["category"], e["target"], e["details"]) == ("B", "OBSERVE", "C", {"v": 1})
    log.emit(obs.ACTION, "two")
    log.emit(obs.ACTION, "three")
    assert [x["seq"] for x in log.recent()] == [1, 2, 3]
    assert [x["summary"] for x in log.recent(since=1)] == ["two", "three"]


def test_ring_buffer_keeps_last_300():
    log = obs.EventLog("A", Clock())
    for i in range(350):
        log.emit(obs.ACTION, f"event {i}")
    evs = log.recent()
    assert len(evs) == obs.RING_SIZE == 300
    assert evs[0]["summary"] == "event 50" and evs[-1]["summary"] == "event 349"
    assert log.seq == 350


def test_throttle_interval_suppressed_count_fingerprint_and_force():
    clk = Clock()
    log = obs.EventLog("A", clk)
    key = ("trust", "B")
    assert log.emit(obs.TRUST_CHANGE, "1", key=key, every=2.0) is not None
    clk.t += 0.5
    assert log.emit(obs.TRUST_CHANGE, "2", key=key, every=2.0) is None
    clk.t += 0.5
    assert log.emit(obs.TRUST_CHANGE, "3", key=key, every=2.0) is None
    clk.t += 1.1                                   # 2.1 s after the first
    e = log.emit(obs.TRUST_CHANGE, "4", key=key, every=2.0)
    assert e is not None and e["details"]["suppressed_repeats"] == 2
    # a fingerprint change (e.g. threshold crossing) goes straight through
    clk.t += 0.1
    assert log.emit(obs.TRUST_CHANGE, "5", key=key, every=2.0, fingerprint="band-1") is not None
    clk.t += 0.1
    assert log.emit(obs.TRUST_CHANGE, "6", key=key, every=2.0, fingerprint="band-1") is None
    assert log.emit(obs.TRUST_CHANGE, "7", key=key, every=2.0, force=True) is not None
    # default interval comes from THROTTLE_S when a key is given
    assert log.emit(obs.SCORE, "s1", key="s") is not None
    clk.t += obs.THROTTLE_S[obs.SCORE] / 2
    assert log.emit(obs.SCORE, "s2", key="s") is None
    # unkeyed events are never throttled
    assert all(log.emit(obs.QUORUM, "q") for _ in range(5))


def test_emit_never_raises():
    log = obs.EventLog("A", Clock())
    assert log.emit(obs.ACTION, "bad details", details=42) is None   # dict(42) fails inside
    assert log.emit(obs.ACTION, "still works") is not None


def test_sentence_helpers():
    assert obs.proposal_text("CONTAIN:B:1:") == "CONTAIN B (epoch 1)"
    assert obs.proposal_text("ADVANCE_STAGE:C:2:MONITORED") == "move C to MONITORED (epoch 2)"
    normal = obs.describe_observation("B", "C", "records-api", {"reachable": True, "healthy": True}, 0.0)
    assert normal == "Agent B sees records-api (C) as normal on all 4 signals."
    m = {"reachable": True, "network": {"outbound_connections": 40, "conn_threshold": 8,
                                        "conn_confidence": 1.0, "tx_rate_Bps": 0, "tx_threshold": 256000,
                                        "tx_confidence": 0, "confidence": 1.0},
         "auth": {"failures_in_window": 60, "window_s": 10, "threshold": 10, "confidence": 1.0}}
    s = obs.describe_observation("B", "C", "records-api", m, 1.0)
    assert "40 outbound connections (limit 8)" in s and "60 failed logins in 10s (limit 10)" in s
    assert s.endswith("confidence 1.00.")
    assert "unreachable" in obs.describe_observation("B", "C", "x", {"reachable": False}, 0)


def test_advance_block_reason_agrees_with_can_advance():
    """The explanation must never disagree with the actual decision."""
    for stage, dwell, trust, anomaly in itertools.product(
            STAGES, (0, 5, 10, 30), (0, 19, 20, 39, 40, 59, 60, 79, 80, 100), (0.0, 0.29, 0.3, 0.9)):
        decision = can_advance(stage, dwell, 10, trust, anomaly, 0.3)
        reason = advance_block_reason(stage, dwell, 10, trust, anomaly, 0.3)
        assert decision == (reason is None), (stage, dwell, trust, anomaly, reason)


def test_score_breakdown_recombines_to_W():
    p = QuorumParams()
    ev = [Evidence(origin=o, target="C", observation=ObsType(t), confidence=c, timestamp=time.time())
          for o, t, c in [("A", "NETWORK", 0.9), ("B", "NETWORK", 0.7), ("B", "PROCESS", 0.95),
                          ("D", "PROCESS", 0.6), ("A", "AUTH", 0.8)]]
    trust = {"A": 100.0, "B": 60.0, "D": 90.0}.get
    sc = weighted_score(ev, trust, p)
    for i in sc.items:
        assert i["weight"] == pytest.approx(i["confidence"] * i["sender_trust"] / 100, abs=1e-3)
    best = max(d["score"] for d in sc.type_detail.values())
    assert sc.total == pytest.approx(best + sc.diversity_bonus, abs=1e-3)
    assert sc.diversity_bonus == pytest.approx(p.diversity_bonus * (len(sc.corroborated_types) - 1))
    assert sc.to_dict()["items"] and sc.to_dict()["type_detail"]


def test_detector_records_measurements_without_changing_detections():
    base = {"app.py": "1" * 64}
    snap = Snapshot(target="C", time=100.0, reachable=True, healthy=True, instance_id="i",
                    pod_ip="10.0.0.3", outbound_connections=40, tx_bytes=0,
                    processes=[{"pid": 9, "comm": "xmrig", "exe": "/tmp/xmrig"}],
                    file_hashes={"app.py": "f" * 64})
    d = Detector(base, ["python"])
    dets = d.analyze({"C": snap}, 100.0)["C"]
    assert {x.observation.value for x in dets} == {"NETWORK", "PROCESS", "FILE_INTEGRITY"}
    m = d.measurements["C"]
    assert m["network"]["outbound_connections"] == 40 and m["network"]["conn_threshold"] == 8
    assert m["network"]["confidence"] == max(x.confidence for x in dets if x.observation == ObsType.NETWORK)
    assert m["process"]["suspicious"] == ["xmrig"] and m["process"]["confidence"] == 0.95
    assert m["file_integrity"]["modified"] == ["app.py"]
    # sensitivity is reflected in the recorded threshold
    d.analyze({"C": snap}, 101.0, {"C": 0.7})
    assert d.measurements["C"]["network"]["conn_threshold"] == pytest.approx(8 * 0.7)


# ------------------------------------------------------------------ scenario tests
@pytest.fixture(scope="module")
def cluster():
    c = LocalCluster(base_port=50751).start()
    time.sleep(1.5)
    yield c
    c.stop()


def events(c, nodes="ABCD", category=None):
    out = [e for n in nodes for e in c.agents[n].events.recent()]
    return [e for e in out if category is None or e["category"] == category]


def test_app_compromise_emits_the_pipeline_categories(cluster):
    c = cluster
    c.mark("app-compromise", "C")
    c.world.attack("C")
    assert c.wait_until(lambda: all(a.states["C"].phase == "HEALTHY" and a.states["C"].epoch == 1
                                    for a in c.agents.values()), 90)
    cats = {e["category"] for e in events(c)}
    for cat in (obs.OBSERVE, obs.EVIDENCE_SENT, obs.EVIDENCE_RECEIVED, obs.SCORE, obs.VOTE_CAST,
                obs.VOTE_WITHHELD, obs.QUORUM, obs.ACTION):
        assert cat in cats, f"{cat} was never emitted"
    q = [e for e in events(c, category=obs.QUORUM) if e["details"].get("action") == "CONTAIN"]
    assert q and all(len(e["details"]["signers"]) >= 3 for e in q)
    assert q[0]["details"]["justification"]["evidence"], "CONTAIN must carry its evidence"
    acts = {e["details"]["action"] for e in events(c, category=obs.ACTION)}
    assert {"isolate", "recover", "stage_change", "scheduled"} <= acts
    recv = events(c, category=obs.EVIDENCE_RECEIVED)[0]["details"]
    assert recv["signature"] == "valid" and recv["mtls_match"] is True and recv["evidence_id"]
    sent = events(c, category=obs.EVIDENCE_SENT)[0]["details"]
    assert {"type", "confidence", "evidence_id"} <= set(sent)
    obs_ev = [e for e in events(c, category=obs.OBSERVE) if e["target"] == "C" and e["details"]["confidence"] > 0]
    assert obs_ev and "network" in obs_ev[0]["details"]["measurements"]
    wh = [e["details"]["reason"] for e in events(c, category=obs.VOTE_WITHHELD)
          if e["details"].get("action") == "ADVANCE_STAGE"]
    assert any("must stay in" in r or "workload trust" in r for r in wh)
    for e in events(c):
        assert isinstance(e["summary"], str) and e["summary"].endswith(".")


def test_false_accusation_emits_withheld_trust_and_flags(cluster):
    c = cluster
    c.compromise_agent("A", "B")
    assert c.wait_until(lambda: all(c.agents[n].agent_trust.get("A") < 38 for n in "BCD"), 40)
    time.sleep(1.5)
    c.restore_agent("A")
    honest = "BCD"
    wh = [e for e in events(c, honest, obs.VOTE_WITHHELD) if e["target"] == "B"
          and e["details"].get("action") == "CONTAIN"]
    assert any("own observation of B normal" in e["details"]["reason"] for e in wh)
    excl = [e for e in events(c, honest, obs.VOTE_WITHHELD) if e["details"].get("excluded_voter") == "A"]
    assert excl and "below vote-exclusion threshold 40" in excl[0]["details"]["reason"]
    tc = [e for e in events(c, honest, obs.TRUST_CHANGE) if e["target"] == "A"]
    assert any(e["details"]["reason"] == "contradicted evidence" for e in tc)
    flags = {(e["node"], e["details"]["flag"]) for e in events(c, honest, obs.FLAG)}
    for n in honest:
        assert (n, "SUSPECT") in flags and (n, "VOTE_EXCLUDED") in flags
    cast = [e for e in events(c, "A", obs.VOTE_CAST) if e["target"] == "B"]
    assert cast and "[simulated compromise]" in cast[0]["summary"]


def test_trust_change_throttled_to_one_per_peer_per_2s_plus_crossings(cluster):
    tc = [e for e in events(cluster, "C", obs.TRUST_CHANGE) if e["target"] == "A"
          and e["details"]["reason"] != "forged/invalid message"]
    assert len(tc) >= 3
    band = lambda v: 2 if v >= 50 else (1 if v >= 40 else 0)  # noqa: E731
    for prev, cur in zip(tc, tc[1:]):
        crossed = band(prev["details"]["new"]) != band(cur["details"]["new"])
        assert crossed or cur["ts"] - prev["ts"] >= 2.0 - 1e-6, (prev, cur)


def test_forgery_emits_rejected_with_reason(cluster):
    c = cluster
    c.compromise_agent("A", "D", mode="forge-evidence")
    time.sleep(2.5)
    c.restore_agent("A")
    rej = events(c, "BC", obs.REJECTED)
    assert rej
    d = rej[0]["details"]
    assert d["sender_mtls"] == "A" and d["claimed_signer"] == "B" and "mTLS identity" in d["reason"]
    forged = [e for e in events(c, "BC", obs.TRUST_CHANGE) if e["details"]["reason"] == "forged/invalid message"]
    assert forged and forged[0]["details"]["penalty"] == 20


def test_status_exposes_event_stream_and_since(cluster):
    a = cluster.agents["B"]
    srv = serve_status(a, 50799)
    try:
        full = json.loads(urllib.request.urlopen("http://localhost:50799/status").read())
        assert full["events"] and full["event_boot"] == a.events.boot
        for k in ("observations", "agent_trust_history", "vote_reasons", "pending_votes_detail",
                  "flags", "thresholds"):
            assert k in full
        since = full["events"][-1]["seq"]
        newer = json.loads(urllib.request.urlopen(
            f"http://localhost:50799/status?events_since={since}").read())
        assert all(e["seq"] > since for e in newer["events"])
        none = json.loads(urllib.request.urlopen("http://localhost:50799/status?events=0").read())
        assert none["events"] == []
    finally:
        srv.shutdown()


def test_dashboard_collector_merges_events_and_explains_votes(cluster, monkeypatch):
    ports = [50791, 50792, 50793, 50794]
    servers = [serve_status(cluster.agents[n], p) for n, p in zip("ABCD", ports)]
    try:
        monkeypatch.setenv("STATUS_URLS", ",".join(f"http://localhost:{p}/status" for p in ports))
        spec = importlib.util.spec_from_file_location("dash_server", os.path.join(ROOT, "dashboard", "server.py"))
        dash = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dash)
        col = dash.COLLECTOR
        col.poll_once()
        n1 = len(col.all_events())
        assert n1 > 0 and {e["node"] for e in col.all_events()} == set("ABCD")
        gseqs = [e["gseq"] for e in col.all_events()]
        assert gseqs == sorted(gseqs) and len(set(gseqs)) == len(gseqs)
        col.poll_once()                          # only new events are appended, no duplicates
        keys = [(e["node"], e["seq"]) for e in col.all_events()]
        assert len(keys) == len(set(keys))
        st = dash.aggregate()
        assert set(st["nodes"]) == set("ABCD") and "pending_detail" in st
        view = dash.agent_view("C")
        assert view["up"] and all(e["node"] == "C" for e in view["events"])
    finally:
        for s in servers:
            s.shutdown()


def test_agent_crash_emits_peer_link_down(cluster):
    c = cluster
    c.crash_agent("D")
    c.world.attack("A", kinds=("tamper",))          # forces broadcasts, which now fail towards D
    assert c.wait_until(lambda: any(e["details"].get("peer") == "D" and e["details"]["state"] == "down"
                                    for e in events(c, "ABC", obs.PEER_LINK)), 15)


def test_baseline_controller_emits_events():
    b = LocalBaseline()
    b.world.attack("C")
    b.backend.marker = {"id": uuid.uuid4().hex[:8], "scenario": "app-compromise", "target": "C",
                        "attacker": None, "injected_at": time.time()}
    for _ in range(3):
        b.ctl.tick()
        time.sleep(0.2)
    cats = {e["category"] for e in b.ctl.events.recent()}
    assert {obs.OBSERVE, obs.QUORUM, obs.ACTION} <= cats
    q = b.ctl.events.by_category(obs.QUORUM)[0]
    assert q["details"]["signers"] == ["CENTRAL"]
    st = b.ctl.status(events_since=0)
    assert st["events"] and "observations" in st
