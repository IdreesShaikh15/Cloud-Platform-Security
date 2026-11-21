"""Demo Mode: choose the service, the 5 s lead-in, per-run graphs that differ by scenario, suspicious-activity rows for scenarios that isolate nothing."""
import importlib.util
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import demo_server as ds  # noqa: E402
import runlog as rl  # noqa: E402
import scenarios as sc  # noqa: E402

HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()


# ------------------------------------------------------------------ the catalogue: service choice
def test_every_scenario_has_graphs_and_a_valid_default_service():
    for sid, (s, _fn) in sc.CATALOGUE.items():
        assert sid in rl.PANELS, sid
        if s.default_target:
            assert s.default_target in s.targets and set(s.targets) <= set(sc.TARGETS)
            for t in s.targets:
                txt = s.text(t)
                assert sc.D[t] in txt["title"] or "{svc}" not in s.title_tpl, (sid, t)
                assert "{" not in txt["title"] + txt["description"] + txt["expect"], (sid, t)
        else:
            assert s.targets == ()                                  # forged evidence is not about one service


def test_default_titles_are_unchanged_so_the_runbook_still_matches():
    s, _ = sc.CATALOGUE["lab-results-tampered"]
    assert s.title == f"A file in the {sc.D['E']} is changed"
    assert sc.CATALOGUE["agent-crash"][0].title == f"A security agent goes offline, then the {sc.D['C']} is attacked"
    assert sc.CATALOGUE["false-accusation"][0].title == f"A security agent falsely accuses the {sc.D['B']}"


def test_the_liar_or_crashed_agent_is_never_the_attacked_services_own_agent():
    for v in sc.NODES:
        assert sc.other(v, "A") != v and sc.other(v, "D") != v
    assert sc.other("C", "D") == "D" and sc.other("D", "D") == "A" or sc.other("D", "D") in sc.NODES


def test_listing_carries_the_service_texts():
    row = next(x for x in sc.listing() if x["id"] == "app-compromise")
    assert row["default_target"] == "C" and set(row["targets"]) == set(sc.TARGETS) and row["texts"]["E"]["title"].endswith(sc.D["E"])


# ------------------------------------------------------------------ the controller
class FakeAgent:
    source = "SIMULATOR"

    def status(self, include_events=True):
        return {"targets": {}, "observations": {}, "agent_trust": {}, "pending_votes": {}}


class FakeCluster:
    def __init__(self):
        class B:
            SOURCE = "SIMULATOR"
        self.backend = B()
        self.agents = {n: FakeAgent() for n in "ABCD"}

    def stop(self):
        pass


def quick(r):
    r.lead_in()
    return True, f"hit {r.target}"


CAT = {"quick": (sc.Scenario("quick", "Quick", "d", "e", 1, False, "C", ("A", "C", "E"), "Quick {svc}", "d {svc}", "e {svc}"), quick),
       "nosvc": (sc.Scenario("nosvc", "No service", "d", "e", 1), quick)}


def wait(pred, t=10):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_start_validates_the_service_and_reports_the_countdown(monkeypatch):
    monkeypatch.setattr(sc, "LEAD_IN_S", 0.6)
    ctl = ds.DemoController(lambda: FakeCluster(), CAT, check_env=False)
    try:
        assert ctl.start("quick", "Z")[0] == 400 and ctl.start("quick", "B")[0] == 400        # not a workload / not allowed for this scenario
        assert ctl.start("nosvc", "C")[0] == 400                                               # this scenario takes no service
        assert ctl.start("quick", {"a": 1})[0] == 400
        code, body = ctl.start("quick", "E")
        assert code == 202 and body["target"] == "E"
        st = ctl.state()
        assert st["running"] == "quick" and st["target"] == "E" and st["phase"] == "lead-in" and 0 < st["countdown_s"] <= 0.6
        assert [x["id"] for x in st["services"]] == list(sc.TARGETS)
        assert wait(lambda: ctl.state()["status"] == "idle")
        assert ctl.state()["last"]["detail"] == "hit E" and ctl.state()["last"]["target"] == "E"
        assert ctl.run.snapshot()["meta"]["target"] == "E" and ctl.run.snapshot()["meta"]["phase"] == "done"
        assert ctl.reset()[0] == 200 and ctl.run.snapshot() == {"available": False}
        assert ctl.start("quick")[1]["target"] == "C"                                          # no service named: the scenario's default
    finally:
        ctl.stop()


def test_dashboard_forwards_only_a_listed_service(monkeypatch):
    monkeypatch.setenv("STATUS_URLS", "http://x:1/status")
    spec = importlib.util.spec_from_file_location("dash_run_graphs", os.path.join(ROOT, "dashboard", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    sent = []
    monkeypatch.setattr(srv, "sim_gate", lambda statuses=None: (True, "ok"))

    def call(method, path, body=None, timeout=5.0):
        if path == "/state":
            return 200, {"scenarios": [{"id": "app-compromise", "targets": ["A", "C"]}, {"id": "forge-evidence", "targets": []}]}
        sent.append((path, body))
        return 202, {"started": body["scenario"]}
    monkeypatch.setattr(srv, "sim_call", call)
    assert srv.sim_action("start", {"scenario": "app-compromise", "target": "C"})[0] == 202 and sent[-1][1] == {"scenario": "app-compromise", "target": "C"}
    assert srv.sim_action("start", {"scenario": "app-compromise"})[0] == 202 and sent[-1][1] == {"scenario": "app-compromise"}
    n = len(sent)
    for bad in ({"scenario": "app-compromise", "target": "E"}, {"scenario": "app-compromise", "target": "C; rm -rf"}, {"scenario": "forge-evidence", "target": "A"},
                {"scenario": "app-compromise", "target": 5}, {"scenario": "nope"}):
        assert srv.sim_action("start", bad)[0] == 400, bad
    assert len(sent) == n, "a refused request is never forwarded"


# ------------------------------------------------------------------ graphs differ by scenario
def synth(n=60, victim="C", liar="A", isolate=True):
    out = []
    for i in range(n):
        t = i * 0.5
        iso = isolate and t >= 12
        ph = "HEALTHY" if not iso else ("ISOLATED" if t < 13 else "RECOVERING" if t < 15 else "VALIDATING" if t < 16 else "REINTEGRATING")
        out.append({"t": t, "alive": 4, "attempt": 1, "inv_active": 1 if 8 <= t < 14 else 0, "inv_total": 1 if t >= 8 else 0, "rejected": int(t // 4),
                    "withheld": int(t // 5), "healthy": True, "isolated": [victim] if iso else [],
                    "obs": {n_: {"conns": 9 + t * 0.3, "net": min(1, t / 20), "proc": 0.95 if t > 10 else 0, "file": 0.95 if t > 11 else 0, "auth": 0, "conf": min(1, t / 20)} for n_ in "ABCD"},
                    "W": {n_: min(1, t / 14) for n_ in "ABCD"}, "wt": {n_: min(100, max(0, (t - 16) * 8)) for n_ in "ABCD"},
                    "phase": {n_: ph for n_ in "ABCD"}, "stage": {n_: "QUARANTINE" for n_ in "ABCD"},
                    "votes": {n_: (3 if 11 <= t < 12 else 0) for n_ in "ABCD"}, "trust": {n_: {m: max(0, 100 - 5 * t) if m == liar else 100 for m in "ABCD"} for n_ in "ABCD"}})
    return out


def test_every_scenario_draws_its_own_set_of_graphs():
    meta = {"scenario": "x", "target": "C", "target_name": "EHR API", "roles": {"liar": "A", "attacker": "A"}, "attack_at": 5.0, "finished_at": 30, "phase": "done"}
    sets = {}
    for sid, ids in rl.PANELS.items():
        panels = rl.build_panels({**meta, "scenario": sid}, synth(), ids)
        assert [p["id"] for p in panels] == ids
        for p in panels:
            assert p["title"] and p["series"] and all(s["points"] for s in p["series"]), (sid, p["id"])
            assert len({s["name"] for s in p["series"]}) == len(p["series"])
        sets[sid] = tuple(ids)
    assert len(set(sets.values())) >= 9                         # thirteen scenarios, at least nine different sets of graphs
    assert sets["false-accusation"] != sets["app-compromise"] and sets["transient-blip"] != sets["slow-burn"] and sets["obstruct"] != sets["agent-crash"]
    assert "liar_trust" in sets["false-accusation"] and "liar_trust" not in sets["app-compromise"]
    assert "investigation" in sets["transient-blip"] and "agents_alive" in sets["agent-crash"] and "withheld" in sets["obstruct"] and "attempts" in sets["validation-retry"]


def test_the_data_in_the_same_graph_differ_by_scenario():
    meta = {"scenario": "app-compromise", "target": "C", "target_name": "EHR API", "roles": {"liar": "A"}, "attack_at": 5.0}
    a = rl.build_panels(meta, synth(isolate=True), ["evidence"])[0]["series"][0]["points"]
    b = rl.build_panels(meta, [dict(s, W={n: 0.1 for n in "ABCD"}) for s in synth()], ["evidence"])[0]["series"][0]["points"]
    assert a != b


def test_run_summary_and_markers_come_from_the_recording():
    meta = {"scenario": "app-compromise", "target": "C", "target_name": "EHR API", "roles": {}, "attack_at": 5.0}
    s = rl.summary(meta, synth())
    assert s["detected_s"] is not None and s["isolated_s"] == 7.0 and s["recovery_check_s"] == 11.0 and s["full_access_s"] is None
    labels = [m["label"] for m in rl.derive_markers(meta, synth(), [{"t": 5, "label": "attack begins", "kind": "attack"}])]
    assert labels[0] == "attack begins" and "3 of 4 agents agreed" in labels and "isolated" in labels and "recovery check" in labels
    run = rl.build_run(dict(meta, phase="attack", finished_at=None, started_at=0, title="t", ok=None, detail="", source="SIMULATOR"), synth(), [], True)
    assert run["available"] and run["bands"] and run["meta"]["source"] == "SIMULATOR" and run["panels"]


def test_recorder_only_reads_the_agents_status():
    ticked = []

    class A(FakeAgent):
        def tick(self):
            ticked.append(1)

        def status(self, include_events=True):
            return {"targets": {"C": {"phase": "HEALTHY", "stage": "-", "workload_trust": 100, "score": {"W": 0.2}, "healthy": True, "epoch": 0}},
                    "observations": {"C": {"network": {"outbound_connections": 3, "confidence": 0.0}, "confidence": 0.0}}, "agent_trust": {"A": 100},
                    "pending_votes": {"CONTAIN:C:1:": ["A", "B"]}, "investigations": {}, "rejections": [], "obstructed_votes": []}
    c = FakeCluster()
    c.agents = {n: A() for n in "ABCD"}
    r = rl.RunRecorder(lambda: c)
    r.begin("app-compromise", "t", "C")
    time.sleep(1.3)
    r.finish(True, "ok")
    snap = r.snapshot()
    r.stop()
    assert snap["samples"] >= 2 and not ticked
    s = r.samples[0]
    assert s["W"]["A"] == 0.2 and s["votes"]["A"] == 2 and s["alive"] == 4 and s["obs"]["B"]["conns"] == 3


# ------------------------------------------------------------------ suspicious activity
@pytest.fixture()
def srv(monkeypatch):
    monkeypatch.setenv("STATUS_URLS", "http://x:1/status,http://x:2/status,http://x:3/status,http://x:4/status")
    spec = importlib.util.spec_from_file_location("dash_susp", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def status(me, sees_b=False, flags=None):
    return {"targets": {"B": {"phase": "HEALTHY", "epoch": 0, "display_name": "Login & Access Service",
                              "detections": ([{"type": "NETWORK", "confidence": 0.9}] if sees_b else [])}},
            "recent_evidence": [{"origin": "A", "target": "B", "type": "NETWORK", "confidence": 0.95, "timestamp": 100.0}] if me != "A" else [],
            "flags": flags or {}}


def test_a_disputed_accusation_shows_up_even_though_nothing_is_isolated(srv):
    live = {n: status(n, flags={"A": {"suspect": True, "vote_excluded": n == "D"}} if n != "A" else {}) for n in "ABCD"}
    rows = srv.suspicious_view(live, {"rows": []}, [{"t": 101, "from": "A", "claimed_signer": "B", "reason": "bad signature", "seen_by": "C"}])
    kinds = {r["kind"] for r in rows}
    assert kinds == {"accusation", "agent-flag", "rejected"}
    acc = next(r for r in rows if r["kind"] == "accusation")
    assert "Agent A reports" in acc["title"] and "see nothing wrong" in acc["detail"] and acc["agents"] == ["B", "C", "D"]
    assert "SUSPECT" in next(r for r in rows if r["kind"] == "agent-flag")["title"]


def test_agreed_claims_and_isolated_workloads_are_not_listed_as_suspicious(srv):
    live = {n: status(n, sees_b=True) for n in "ABCD"}                   # everyone sees it: that is an attack, not a doubt
    assert srv.suspicious_view(live, {"rows": []}, []) == []
    live = {n: status(n) for n in "ABCD"}
    for s in live.values():
        s["targets"]["B"]["epoch"] = 1
    inv = {"rows": [{"target": "B", "workload": "auth", "question": "q", "active": False, "outcome_text": "confirmed", "started_at": 1, "per_agent": {"A": {}}}]}
    assert srv.suspicious_view(live, inv, []) == []                      # an investigation that ended in an isolation belongs to the alert
    live2 = {n: status(n) for n in "ABCD"}
    assert [r["kind"] for r in srv.suspicious_view(live2, inv, []) if r["kind"] == "investigation"] == ["investigation"]


# ------------------------------------------------------------------ the page
def test_the_page_has_the_service_chooser_countdown_graphs_and_suspicious_section():
    for needle in ('id="simtarget"', "function chartSvg", "function renderRun", "async function refreshRun", 'id="suspsec"', "renderSuspicious(s.suspicious)",
                   "renderRecoveryNone(s)", "the attack begins in", 'api/sim/run', 'id="runbox"', "api/eval/figure/", "never changed by a run"):
        assert needle in HTML, needle
    assert "simPost(\"api/sim/start\", {scenario: document.getElementById(\"simsel\").value, ...(tv ? {target: tv} : {})})" in HTML


# ------------------------------------------------------------------ the alert list no longer keeps the previous scenario
def test_a_reset_or_restarted_agent_clears_what_the_dashboard_remembered_from_it(srv, monkeypatch):
    chn = next(iter(srv.CHANNELS.values()))
    dec = {"action": "CONTAIN", "target": "C", "epoch": 1, "stage": "", "t": 1.0, "voters": ["A", "B", "C"], "data_source": "SIMULATOR"}

    def status(boot, decisions):
        return {"data_source": "SIMULATOR", "event_boot": boot, "event_seq": 1, "events": [], "decisions": decisions, "actions": {"recent": []},
                "snapshots": [], "investigations": {}}
    answers = {}
    monkeypatch.setattr(srv, "get", lambda url: answers.get(url.split("?")[0]))
    srcs = dict(chn.sources)
    for sid, url in srcs.items():
        answers[url] = status("life-1", [dec])
    chn.collector.poll_once()
    assert len(chn.collector.decisions) == len(srcs)                    # one remembered record per agent
    for url in srcs.values():
        answers[url] = status("life-2", [])                              # the simulator was reset: new agents, nothing happened yet
    chn.collector.poll_once()
    assert len(chn.collector.decisions) == 0
    assert [r for r in srv.incident_list(chn.collector.snapshot(), chn.collector.history())] == []
    # the same agents answering again (no restart) keep what they reported
    for url in srcs.values():
        answers[url] = status("life-2", [dec])
    chn.collector.poll_once()
    chn.collector.poll_once()
    assert len(chn.collector.decisions) == len(srcs)
