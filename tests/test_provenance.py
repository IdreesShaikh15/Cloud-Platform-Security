"""Part 1: every event, decision, action record and metric row carries trustworthy source metadata."""
import importlib.util
import json
import os
import time

import pytest

from resilience import provenance as prov
from resilience.crypto import KeyRegistry, Signer
from resilience.agent import ResilienceAgent
from resilience.response import FakeBackend, K8sBackend
from resilience.monitoring import HttpTelemetrySource
from world import FakeTelemetry, FakeWorld, LocalBaseline, fast_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dash(monkeypatch, **env):
    for k in ("SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "BASELINE_URL", "DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATUS_URLS", ",".join(f"http://127.0.0.1:1{i}/status" for i in range(4)))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("dash_prov_mod", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LiveFakeBackend(FakeBackend):
    SOURCE = "LIVE_CLUSTER"          # a stand-in that DECLARES live; it never touches Kubernetes


class LiveFakeTelemetry(FakeTelemetry):
    SOURCE = "LIVE_CLUSTER"


def agent(backend=None, telemetry=None, port=52901):
    world = FakeWorld()
    return ResilienceAgent(fast_config(port), "A", Signer.generate("A"), KeyRegistry({}),
                           telemetry or FakeTelemetry(world), backend or FakeBackend())


# ------------------------------------------------------------------ derivation
def test_declared_sources_of_the_real_classes():
    assert K8sBackend.SOURCE == "LIVE_CLUSTER" and HttpTelemetrySource.SOURCE == "LIVE_CLUSTER"
    assert FakeBackend.SOURCE == "SIMULATOR" and FakeTelemetry.SOURCE == "SIMULATOR"


def test_derive_requires_backend_and_telemetry_to_agree():
    assert prov.derive(FakeBackend(), FakeTelemetry(FakeWorld())) == "SIMULATOR"
    assert prov.derive(LiveFakeBackend(), LiveFakeTelemetry(FakeWorld())) == "LIVE_CLUSTER"
    assert prov.derive(LiveFakeBackend(), FakeTelemetry(FakeWorld())) == "UNKNOWN"     # live changes, simulated eyes
    assert prov.derive(FakeBackend(), LiveFakeTelemetry(FakeWorld())) == "UNKNOWN"
    assert prov.derive(object(), object()) == "UNKNOWN"                               # declares nothing

    class Weird:
        SOURCE = "production"
    assert prov.derive(Weird(), Weird()) == "UNKNOWN"                                  # not a known value


def test_aggregate():
    assert prov.aggregate([]) == "UNKNOWN"
    assert prov.aggregate(["SIMULATOR", "SIMULATOR"]) == "SIMULATOR"
    assert prov.aggregate(["SIMULATOR", "LIVE_CLUSTER"]) == "MIXED"
    assert prov.aggregate(["LIVE_CLUSTER", "UNKNOWN"]) == "MIXED"                      # unknown is never absorbed
    assert prov.aggregate(["garbage"]) == "UNKNOWN"


def test_dashboard_copy_of_the_rules_matches_the_agent_package(monkeypatch):
    d = load_dash(monkeypatch)
    cases = [[], ["SIMULATOR"], ["LIVE_CLUSTER"], ["SIMULATOR", "LIVE_CLUSTER"], ["UNKNOWN"], ["SIMULATOR", "UNKNOWN"],
             ["x"], [None], ["SIMULATOR", None, "SIMULATOR"]]
    for c in cases:
        assert d.aggregate_source(c) == prov.aggregate(c), c
    assert (d.SIMULATOR, d.LIVE_CLUSTER, d.UNKNOWN, d.MIXED) == (prov.SIMULATOR, prov.LIVE_CLUSTER, prov.UNKNOWN, prov.MIXED)


def test_dashboard_never_trusts_disagreeing_or_missing_claims(monkeypatch):
    d = load_dash(monkeypatch)
    assert d.clean_source("SIMULATOR", "SIMULATOR") == "SIMULATOR"
    assert d.clean_source("SIMULATOR", "LIVE_CLUSTER") == "UNKNOWN"      # event and status disagree: trust neither
    assert d.clean_source(None, None) == "UNKNOWN" and d.clean_source("SIMULATOR", None) == "SIMULATOR"
    assert d.clean_source("<script>", "SIMULATOR") == "UNKNOWN"          # junk is never passed through


# ------------------------------------------------------------------ stamping at the producer
def test_simulated_agent_stamps_everything_it_produces():
    a = agent()
    assert a.source == "SIMULATOR"
    a.events.emit("ACTION", "x")
    assert all(e["data_source"] == "SIMULATOR" for e in a.events.recent())
    a.audit.record(action="t", task="t", workload="w", target="C", epoch=1, outcome="applied")
    assert a.audit.recent[-1]["data_source"] == "SIMULATOR"
    a.metrics.set_marker({"id": "m1", "scenario": "s", "target": "C", "attacker": None, "injected_at": time.time()})
    a.metrics.event("first_detection", time.time(), "C")
    assert a.metrics.summary()[0]["data_source"] == "SIMULATOR"
    assert all(r["data_source"] == "SIMULATOR" for r in a.metrics.events)
    st = a.status()
    assert st["data_source"] == "SIMULATOR"


def test_decisions_and_snapshots_are_stamped_and_live_stand_in_is_labelled_live():
    live = agent(LiveFakeBackend(), LiveFakeTelemetry(FakeWorld()), port=52911)
    assert live.source == "LIVE_CLUSTER" and live.status()["data_source"] == "LIVE_CLUSTER"
    live.events.emit("ACTION", "x")
    assert live.events.recent()[-1]["data_source"] == "LIVE_CLUSTER"
    key = live.forensics.capture_async("C", 1, "contain", "t", None, time.time())
    end = time.time() + 5
    while time.time() < end and live.forensics.get(key) is None:
        time.sleep(0.05)
    assert live.forensics.get(key)["incident"]["data_source"] == "LIVE_CLUSTER"
    assert live.forensics.status()[0]["data_source"] == "LIVE_CLUSTER"
    mixed = agent(LiveFakeBackend(), FakeTelemetry(FakeWorld()), port=52921)
    assert mixed.source == "UNKNOWN" and mixed.status()["data_source"] == "UNKNOWN"


def test_decision_records_carry_the_source(tmp_path):
    from resilience.quorum import Commit
    from resilience.evidence import Action, Vote
    a = ResilienceAgent(fast_config(52931), "A", Signer.generate("A"), KeyRegistry({}), FakeTelemetry(FakeWorld()),
                        FakeBackend(), decision_log_path=str(tmp_path / "d.jsonl"))
    v = Vote(voter="A", target="C", action=Action.CONTAIN, epoch=1, stage="", score=1.0, timestamp=time.time(), evidence_ids=())
    c = Commit(action_key="CONTAIN:C:1:", votes=[v])
    d = a._record_decision(c, time.time(), "test")
    assert d["data_source"] == "SIMULATOR"
    rec = json.loads(open(tmp_path / "d.jsonl").read().splitlines()[-1])
    assert rec["data_source"] == "SIMULATOR"


def test_central_controller_is_stamped_too():
    b = LocalBaseline(cfg=fast_config(52941))
    assert b.ctl.source == "SIMULATOR"
    b.ctl.events.emit("ACTION", "x")
    assert b.ctl.events.recent()[-1]["data_source"] == "SIMULATOR"
    assert b.ctl.status()["data_source"] == "SIMULATOR"
    b.ctl._decide(time.time(), "C", "CONTAIN", "t")
    assert b.ctl.decisions[-1]["data_source"] == "SIMULATOR"


# ------------------------------------------------------------------ dashboard aggregation
def status(src, node="A", events=None, decisions=None):
    return {"node": node, "mode": "distributed", "time": time.time(), "data_source": src, "targets": {
        n: {"phase": "HEALTHY", "stage": "FULL", "workload": n, "epoch": 0, "last_decision": None,
            "detections": [], "workload_trust": 100} for n in "ABCD"},
        "agent_trust": {n: 100 for n in "ABCD"}, "decisions": decisions or [], "rejections": [], "pending_votes": {},
        "event_boot": "b", "event_seq": 0, "events": events or []}


def test_aggregate_labels_sources_and_never_silently_mixes(monkeypatch):
    d = load_dash(monkeypatch)
    d.COLLECTOR.status = {k: status("SIMULATOR", k) for k in "ABCD"}
    a = d.aggregate()
    assert a["data_source"] == "SIMULATOR" and set(a["data_sources"].values()) == {"SIMULATOR"}
    d.COLLECTOR.status = {**{k: status("SIMULATOR", k) for k in "ABC"}, "D": status("LIVE_CLUSTER", "D")}
    assert d.aggregate()["data_source"] == "MIXED"
    d.COLLECTOR.status = {k: status(None, k) for k in "ABCD"}
    assert d.aggregate()["data_source"] == "UNKNOWN"


def test_decisions_of_different_sources_are_never_merged(monkeypatch):
    d = load_dash(monkeypatch)
    dec = lambda src: {"t": 1.0, "action": "CONTAIN", "target": "C", "epoch": 1, "stage": "", "voters": ["A", "B", "C"],
                       "note": "n", "justification": {}, "data_source": src}
    d.COLLECTOR.status = {"A": status("SIMULATOR", "A", decisions=[dec("SIMULATOR")]),
                          "B": status("LIVE_CLUSTER", "B", decisions=[dec("LIVE_CLUSTER")])}
    rows = d.aggregate()["decisions"]
    assert sorted(r["data_source"] for r in rows) == ["LIVE_CLUSTER", "SIMULATOR"]


def test_events_are_stamped_by_the_dashboard_only_from_what_the_producer_said(monkeypatch):
    d = load_dash(monkeypatch)
    ev = lambda seq, src: {"seq": seq, "ts": float(seq), "node": "A", "category": "ACTION", "target": None,
                           "summary": "s", "details": {}, **({"data_source": src} if src else {})}
    answers = {"http://127.0.0.1:10/status": status("SIMULATOR", "A", events=[ev(1, "SIMULATOR"), ev(2, "LIVE_CLUSTER"), ev(3, None)])}
    monkeypatch.setattr(d, "get", lambda url: dict(answers.get(url) or {}) or None)
    monkeypatch.setattr(d, "SOURCES", {"A": "http://127.0.0.1:10/status"})
    d.COLLECTOR.poll_once()
    got = [e["data_source"] for e in d.COLLECTOR.all_events()]
    assert got == ["SIMULATOR", "UNKNOWN", "SIMULATOR"]       # contradicting stamp -> UNKNOWN; absent event stamp -> the status's


def test_metric_rows_are_sanitised(monkeypatch):
    d = load_dash(monkeypatch)
    monkeypatch.setattr(d, "get", lambda url: [{"incident": "i", "data_source": "SIMULATOR"}, {"incident": "j"},
                                               {"incident": "k", "data_source": "<b>x</b>"}])
    assert [r["data_source"] for r in d.metrics()] == ["SIMULATOR", "UNKNOWN", "UNKNOWN"] * 4


# ------------------------------------------------------------------ the header bug
def test_baseline_banner_requires_a_controller_that_actually_answers(monkeypatch):
    # The live manifest always configures BASELINE_URL, even when no central controller is deployed.
    manifest = open(os.path.join(ROOT, "k8s", "resilience", "30-dashboard.yaml")).read()
    assert "BASELINE_URL" in manifest
    d = load_dash(monkeypatch, BASELINE_URL="http://central-controller.resilience.svc.cluster.local:8081")
    d.COLLECTOR.status = {k: status("LIVE_CLUSTER", k) for k in "ABCD"}              # distributed, no controller answering
    a = d.aggregate()
    assert a["baseline"] and a["baseline_up"] is False                                # configured, but not running
    d.COLLECTOR.status["CENTRAL"] = {**status("LIVE_CLUSTER", "CENTRAL"), "mode": "centralized"}
    assert d.aggregate()["baseline_up"] is True
    html = open(os.path.join(ROOT, "dashboard", "index.html")).read()
    assert "s.baseline_up ?" in html and "s.baseline ? \" · centralized" not in html   # the page uses the live signal


def test_page_script_is_valid_and_has_banner_and_source_chips():
    import re, shutil, subprocess, tempfile
    html = open(os.path.join(ROOT, "dashboard", "index.html")).read()
    assert 'id="srcbanner"' in html and "DEMO — SIMULATED DATA" in html and "srcTag(e.data_source)" in html
    node = shutil.which("node")
    if node:
        js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
        assert subprocess.run([node, "--check", f.name], capture_output=True).returncode == 0
