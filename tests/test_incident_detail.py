"""Part 3: the incident detail view. Proposed / authorized / attempted / verified are kept apart, and every claim is
built from what the agents reported."""
import importlib.util
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import demo_server as ds
from resilience.agent import (HEALTHY, ISOLATED, RECOVERING, REINTEGRATING, VALIDATING, ResilienceAgent)
from resilience.crypto import KeyRegistry, Signer
from resilience.response import FakeBackend
from world import FakeTelemetry, FakeWorld, fast_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = "SIMULATOR"


def load_dash(monkeypatch, urls=None):
    for k in ("SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "BASELINE_URL", "DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATUS_URLS", urls or ",".join(f"http://127.0.0.1:1{i}/status" for i in range(4)))
    spec = importlib.util.spec_from_file_location("dash_inc_mod", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ synthetic history builders
def dec(action, epoch, stage="", voters=("A", "B", "C"), t=100.0, node="A", just=None, target="C"):
    return {"t": t, "action": action, "target": target, "epoch": epoch, "stage": stage, "voters": list(voters), "note": "n",
            "justification": just or {}, "data_source": SRC, "node": node}


def rec(task, node="A", outcome="applied", t=101.0, epoch=1, attempt=1, detail="", action="CONTAIN/isolate", target="C", i=[0]):
    i[0] += 1
    return {"id": f"r{i[0]}", "t": t, "node": node, "action": action, "task": task, "workload": "records-api", "target": target,
            "epoch": epoch, "attempt": attempt, "outcome": outcome, "detail": detail, "error": "", "duration_s": 0.1, "data_source": SRC}


def event(cat, node, ts, summary="s", target="C", details=None):
    return {"seq": 1, "ts": ts, "node": node, "category": cat, "target": target, "summary": summary, "details": details or {}, "data_source": SRC}


def hist(decisions=(), audit=(), snaps=(), invs=()):
    return {"decisions": list(decisions), "audit": list(audit), "snaps": list(snaps), "invs": list(invs)}


def tgt_status(phase="HEALTHY", stage="FULL", epoch=1, attempt=1, attention=None, next_step=None):
    return {"phase": phase, "stage": stage, "epoch": epoch, "attempt": attempt, "attention": attention, "next_step": next_step,
            "workload": "records-api", "workload_trust": 50}


def statuses(**kw):
    return {n: {"data_source": SRC, "targets": {"C": tgt_status(**kw)}} for n in "ABCD"}


CONTAIN_JUST = {"votes": [{"voter": v, "score": 1.0, "ts": 100.0} for v in "ABC"], "score": {"W": 1.3},
                "evidence": [{"origin": "A", "type": "NETWORK", "confidence": 1.0, "summary": "40 outbound conns", "timestamp": 99.0},
                             {"origin": "A", "type": "NETWORK", "confidence": 0.9, "summary": "40 outbound conns", "timestamp": 99.5},
                             {"origin": "B", "type": "PROCESS", "confidence": 0.95, "summary": "xmrig", "timestamp": 99.1}]}


def build(d, monkeypatch, decs, audit=(), events=(), st=None, snaps=(), invs=()):
    h = hist(decs, audit, snaps, invs)
    return d.build_incident(SRC, "C", 1, st if st is not None else statuses(), h, list(events))


def action(inc, step_prefix):
    return next(a for a in inc["actions"] if a["step"].startswith(step_prefix))


# ------------------------------------------------------------------ task mapping
def test_audit_tasks_map_to_proposals(monkeypatch):
    d = load_dash(monkeypatch)
    assert d.parse_task("isolate:C:1") == ("CONTAIN:C:1:", "isolate (quarantine policy)")
    assert d.parse_task("recover:C:1:1")[0] == "CONTAIN:C:1:"
    assert d.parse_task("recover:C:1:3")[0] == "RETRY_RECOVERY:C:1:3"
    assert d.parse_task("validated:C:1")[0] == "VALIDATE:C:1:"
    assert d.parse_task("stage:C:1:RESTRICTED")[0] == "ADVANCE_STAGE:C:1:RESTRICTED"
    assert d.parse_task("attention:C:1") is None and d.parse_task("garbage") is None and d.parse_task("recover:C:1:x") is None


# ------------------------------------------------------------------ the four stages are different claims
def test_attempted_means_a_call_was_made_verified_means_the_cluster_was_read(monkeypatch):
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, just=CONTAIN_JUST)]
    audit = [rec("isolate:C:1", "A", "applied"), rec("isolate:C:1", "B", "already_applied"), rec("isolate:C:1", "D", "already_applied")]
    a = action(build(d, monkeypatch, decs, audit), "isolate")
    assert a["proposed"]["state"] == "yes" and a["authorized"]["state"] == "yes"
    assert a["attempted"]["by"] == ["A"] and a["attempted"]["tries"] == 1          # B and D found it done: they did NOT attempt it
    assert a["verified"]["state"] == "yes" and a["verified"]["by"] == ["A", "B", "D"]


def test_authorized_but_never_attempted_is_not_attempted_or_verified(monkeypatch):
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, just=CONTAIN_JUST)]
    audit = [rec("isolate:C:1", "A", "waiting", detail="cannot read the cluster yet")]
    a = action(build(d, monkeypatch, decs, audit), "isolate")
    assert a["authorized"]["state"] == "yes" and a["attempted"]["state"] == "no" and a["verified"]["state"] == "no"


def test_a_timeout_that_could_not_be_read_is_unknown_not_verified(monkeypatch):
    d = load_dash(monkeypatch)
    audit = [rec("isolate:C:1", "A", "unknown", detail="timeout and the cluster cannot be read")]
    a = action(build(d, monkeypatch, [dec("CONTAIN", 1, just=CONTAIN_JUST)], audit), "isolate")
    assert a["attempted"]["state"] == "yes" and a["verified"]["state"] == "unknown"
    audit.append(rec("isolate:C:1", "A", "unknown_resolved", t=103.0))
    a = action(build(d, monkeypatch, [dec("CONTAIN", 1, just=CONTAIN_JUST)], audit), "isolate")
    assert a["verified"]["state"] == "yes"                                           # a later read settled it


def test_applied_unconfirmed_is_attempted_but_not_verified(monkeypatch):
    d = load_dash(monkeypatch)
    a = action(build(d, monkeypatch, [dec("CONTAIN", 1, just=CONTAIN_JUST)], [rec("isolate:C:1", "A", "applied_unconfirmed")]), "isolate")
    assert a["attempted"]["state"] == "yes" and a["verified"]["state"] == "no"


def test_refusals_are_listed_and_are_not_attempts(monkeypatch):
    d = load_dash(monkeypatch)
    audit = [rec("recover:C:1:1", "A", "refused_certificate", detail="invalid certificate"), rec("recover:C:1:1", "B", "refused_stale", detail="attempt changed")]
    a = action(build(d, monkeypatch, [dec("CONTAIN", 1, just=CONTAIN_JUST)], audit), "redeploy")
    assert a["attempted"]["state"] == "no" and len(a["refused"]) == 2
    assert {r["outcome"] for r in a["refused"]} == {"refused_certificate", "refused_stale"}


def test_proposed_but_not_authorized(monkeypatch):
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, just=CONTAIN_JUST)]                                      # the incident exists...
    events = [event("VOTE_CAST", n, 120.0, details={"proposal": "ADVANCE_STAGE:C:1:RESTRICTED"}) for n in "AB"] + \
             [event("VOTE_WITHHELD", "C", 121.0, details={"proposal": "ADVANCE_STAGE:C:1:RESTRICTED", "reason": "must stay in QUARANTINE another 5s"})]
    inc = build(d, monkeypatch, decs, events=events)
    p = next(x for x in inc["proposals"] if x["proposal"] == "ADVANCE_STAGE:C:1:RESTRICTED")
    assert not p["committed"] and [v["voter"] for v in p["votes"]] == ["A", "B"] and p["quorum_needed"] == 3
    assert p["withheld"] == [{"agent": "C", "reason": "must stay in QUARANTINE another 5s", "ts": 121.0}]
    assert not any(a["proposal"] == "ADVANCE_STAGE:C:1:RESTRICTED" for a in inc["actions"])   # nothing was attempted yet


def test_trigger_dedupes_resent_evidence_and_names_the_signers(monkeypatch):
    d = load_dash(monkeypatch)
    inc = build(d, monkeypatch, [dec("CONTAIN", 1, voters=("A", "B", "D"), just=CONTAIN_JUST)])
    ev = inc["trigger"]["evidence_by_agent"]
    assert len(ev["A"]) == 1 and ev["A"][0]["count"] == 2 and ev["A"][0]["confidence"] == 1.0 and ev["B"][0]["summary"] == "xmrig"
    assert inc["trigger"]["decision"]["voters"] == ["A", "B", "D"]


# ------------------------------------------------------------------ status, window, sections
def test_incident_status_words(monkeypatch):
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, just=CONTAIN_JUST)]
    assert build(d, monkeypatch, decs, st=statuses(phase="RECOVERING", stage="QUARANTINE"))["status"] == "ACTIVE"
    assert build(d, monkeypatch, decs, st=statuses(phase="VALIDATING", stage="QUARANTINE", attention={"reason": "x"}))["status"] == "NEEDS_HUMAN"
    assert build(d, monkeypatch, decs, st=statuses())["status"] == "RESOLVED"
    assert build(d, monkeypatch, decs, st={})["status"] == "UNKNOWN"                   # nobody reports: never guessed
    full = decs + [dec("ADVANCE_STAGE", 1, "FULL")]
    assert build(d, monkeypatch, full, st={})["status"] == "RESOLVED"
    assert build(d, monkeypatch, decs, st=statuses(epoch=2))["status"] == "SUPERSEDED"


def test_why_the_next_stage_is_blocked_comes_from_the_agents(monkeypatch):
    d = load_dash(monkeypatch)
    ns = {"step": "advance to RESTRICTED", "ready": False, "blocked_by": "must stay in QUARANTINE another 4s (minimum 10s per stage)"}
    st = statuses(phase="REINTEGRATING", stage="QUARANTINE", next_step=ns)
    st["D"]["targets"]["C"]["next_step"] = {**ns, "blocked_by": "workload trust 5 is below the 20 needed for RESTRICTED"}
    cur = build(d, monkeypatch, [dec("CONTAIN", 1, just=CONTAIN_JUST)], st=st)["current"]["consensus"]
    assert cur["next_step"]["blocked_by"].startswith("must stay in QUARANTINE") and cur["next_step"]["agents_agreeing"] == 3
    assert cur["next_step"]["agents_reporting"] == 4


def test_events_are_limited_to_this_incident_and_this_source(monkeypatch):
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, t=1000.0, just=CONTAIN_JUST), dec("CONTAIN", 2, t=2000.0)]
    ev = [event("OBSERVE", "A", 10.0, "too early"), event("OBSERVE", "A", 950.0, "in the pre-window"), event("OBSERVE", "A", 1500.0, "during"),
          event("OBSERVE", "A", 2500.0, "next incident"), event("OBSERVE", "B", 1500.0, "other workload", target="B"),
          {**event("OBSERVE", "A", 1500.0, "live data"), "data_source": "LIVE_CLUSTER"}]
    inc = build(d, monkeypatch, decs, events=ev)
    assert [e["summary"] for e in inc["observations"]] == ["in the pre-window", "during"]
    assert inc["window"]["to"] == 2000.0


def test_trust_rejected_and_investigations_sections(monkeypatch):
    d = load_dash(monkeypatch)
    ev = [event("TRUST_CHANGE", "B", 105.0, "Agent B's trust in A: 100 -> 90 (A reported trouble on C)", target="A"),
          event("TRUST_CHANGE", "B", 106.0, "Agent B's trust in D: 100 -> 99", target="D"),
          event("FLAG", "B", 107.0, "evidence from A about C was ignored", target="A", details={"flag": "EVIDENCE_REJECTED", "about": "C"}),
          event("REJECTED", "C", 108.0, "bad signature", target="A")]
    inv = {"id": "inv1", "target": "C", "question": "still present?", "trigger": "BORDERLINE", "started_at": 95.0, "initiator": "A", "node": "A",
           "state": "CLOSED", "outcome": "CORROBORATED", "reason": "r", "data_source": SRC}
    inc = build(d, monkeypatch, [dec("CONTAIN", 1, just=CONTAIN_JUST)], events=ev, invs=[inv, {**inv, "node": "B"}])
    assert [e["mentions_target"] for e in inc["trust_changes"]] == [True, False]       # the one that names C is marked
    assert len(inc["rejected"]) == 2 and not any(e["category"] == "FLAG" for e in inc["trust_changes"])
    assert len(inc["investigations"]) == 1 and inc["investigations"][0]["outcomes"] == {"CORROBORATED": 2}


def test_recovery_attempts_pair_snapshots_with_the_reason_for_retry(monkeypatch):
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, just=CONTAIN_JUST), dec("RETRY_RECOVERY", 1, "2", t=120.0)]
    audit = [rec("recover:C:1:1", "A"), rec("recover:C:1:2", "A", t=125.0, attempt=1, action="RETRY_RECOVERY")]
    snaps = [{"key": "C:1:contain", "target": "C", "epoch": 1, "tag": "contain", "reason": "containment", "agent": "A", "pods": 1, "log_lines": 5,
              "not_captured": [], "data_source": SRC},
             {"key": "C:1:failed1", "target": "C", "epoch": 1, "tag": "failed1", "reason": "recovery attempt 1 failed: health check failed",
              "agent": "A", "pods": 1, "log_lines": 3, "not_captured": [{"item": "logs of x", "reason": "gone"}], "data_source": SRC}]
    at = build(d, monkeypatch, decs, audit, snaps=snaps)["recovery"]["attempts"]
    assert [a["attempt"] for a in at] == [1, 2]
    assert at[0]["snapshots"][0]["tag"] == "contain" and at[0]["retry_reason"] is None
    assert at[1]["snapshots"][0]["tag"] == "failed1" and "health check failed" in at[1]["retry_reason"]


def test_incident_list_and_api_validation(monkeypatch):
    d = load_dash(monkeypatch)
    h = hist([dec("CONTAIN", 1, t=100.0, just=CONTAIN_JUST), dec("CONTAIN", 2, t=200.0), dec("CONTAIN", 1, t=100.0, target="B")])
    lst = d.incident_list(statuses(), h)
    assert [(i["target"], i["epoch"]) for i in lst] == [("C", 2), ("B", 1), ("C", 1)] or [(i["target"], i["epoch"]) for i in lst][0] == ("C", 2)
    assert all(i["source"] == SRC and i["signers"] for i in lst)
    assert d.incident_list({}, hist([])) == []                                        # no containment, no incident
    # an investigation or a false accusation alone is never an incident
    assert d.incident_list(statuses(), hist([dec("VALIDATE", 1), dec("ADVANCE_STAGE", 1, "FULL")])) == []


def test_http_endpoint_rejects_bad_ids(monkeypatch):
    d = load_dash(monkeypatch)
    d.COLLECTOR.status = statuses()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request, urllib.error
    try:
        for path in ("/api/incident/SIMULATOR/C/9", "/api/incident/NOPE/C/1", "/api/incident/SIMULATOR/Z/1", "/api/incident/SIMULATOR/C/x",
                     "/api/incident/SIMULATOR/C/1/extra", "/api/incident/SIMULATOR/../etc/1", "/api/incident/SIMULATOR/C/-1"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}{path}", timeout=5)
            assert e.value.code == 404, path
    finally:
        srv.shutdown()


def test_history_survives_the_agents_short_rings_and_is_bounded(monkeypatch):
    d = load_dash(monkeypatch)
    s1 = {"data_source": SRC, "decisions": [dec("CONTAIN", 1)], "actions": {"recent": [rec("isolate:C:1")]}, "snapshots": [], "investigations": {}}
    d.COLLECTOR._remember("A", s1)
    d.COLLECTOR._remember("A", {"data_source": SRC, "decisions": [], "actions": {"recent": []}})           # the agent's ring has moved on
    h = d.COLLECTOR.history()
    assert len(h["decisions"]) == 1 and len(h["audit"]) == 1                                              # but the dashboard remembers
    for i in range(1200):
        d.COLLECTOR._remember("A", {"data_source": SRC, "decisions": [dec("CONTAIN", i + 10, target="B")], "actions": {"recent": []}})
    assert len(d.COLLECTOR.history()["decisions"]) == 1000                                                # bounded


# ------------------------------------------------------------------ agent side: what happens next
def agent(port=54901):
    return ResilienceAgent(fast_config(port), "A", Signer.generate("A"), KeyRegistry({}), FakeTelemetry(FakeWorld()), FakeBackend())


def test_agent_reports_what_blocks_each_phase():
    a = agent()
    st, now = a.states["C"], time.time()
    assert a._next_step("C", st, now) is None                                          # HEALTHY: nothing pending
    st.phase = ISOLATED
    assert "quarantine" in a._next_step("C", st, now)["blocked_by"]
    st.phase, st.attempt = RECOVERING, 2
    n = a._next_step("C", st, now)
    assert "attempt 2" in n["blocked_by"] and not n["ready"]
    st.phase = VALIDATING
    assert a._next_step("C", st, now)["blocked_by"].startswith("validation is failing")     # the fake world has no telemetry yet
    st.phase, st.stage, st.stage_entered = REINTEGRATING, "QUARANTINE", now
    n = a._next_step("C", st, now)
    assert n["step"] == "advance to RESTRICTED" and "must stay in QUARANTINE" in n["blocked_by"]
    st.stage = "FULL"
    assert "isolation policy is gone" in a._next_step("C", st, now)["blocked_by"]
    st.attention = {"reason": "all 4 recovery attempts failed"}
    assert a._next_step("C", st, now)["blocked_by"] == "all 4 recovery attempts failed"
    assert a.status()["targets"]["C"]["next_step"]["step"] == "a human has to act"


# ------------------------------------------------------------------ end to end, real agents (simulator)
def run_scenario(monkeypatch, base, scenario, timeout):
    monkeypatch.setenv("STATUS_URLS", ",".join(f"http://localhost:{base + 100 + i}/status" for i in range(4)))
    ctl = ds.DemoController(ds.default_factory(base), check_env=False)
    d = load_dash(monkeypatch, urls=",".join(f"http://localhost:{base + 100 + i}/status" for i in range(4)))
    assert ctl.start(scenario)[0] == 202
    end = time.time() + timeout
    while time.time() < end and not (ctl.state()["status"] == "idle" and ctl.state()["last"]):
        time.sleep(1)
        d.COLLECTOR.poll_once()
    for _ in range(3):
        time.sleep(1)
        d.COLLECTOR.poll_once()
    return ctl, d


def test_real_genuine_compromise_incident_is_complete(monkeypatch):
    ctl, d = run_scenario(monkeypatch, 54700, "app-compromise", 150)
    try:
        assert ctl.state()["last"]["ok"] is True
        lst = d.incident_list()
        assert [(i["target"], i["epoch"], i["status"]) for i in lst] == [("C", 1, "RESOLVED")] and lst[0]["source"] == "SIMULATOR"
        inc = d.build_incident("SIMULATOR", "C", 1)
        assert len(inc["trigger"]["decision"]["voters"]) >= 3 and inc["trigger"]["evidence_by_agent"]
        steps = [a["step"] for a in inc["actions"]]
        for want in ("isolate", "redeploy", "record validation", "move to RESTRICTED", "move to FULL"):
            assert any(s.startswith(want) for s in steps), (want, steps)
        for a in inc["actions"]:                                                         # every real action went through all four stages
            assert a["proposed"]["state"] == a["authorized"]["state"] == a["attempted"]["state"] == a["verified"]["state"] == "yes", a["step"]
            assert len(a["authorized"]["signers"]) >= 3
            assert len(a["attempted"]["by"]) >= 1 and len(a["attempted"]["by"]) <= len(a["verified"]["by"])
        assert inc["recovery"]["attempts"][0]["snapshots"] and inc["current"]["consensus"]["phase"] == "HEALTHY"
        assert all(p["committed"] and len(p["signers"]) >= 3 for p in inc["proposals"])
    finally:
        ctl.stop()


def test_real_false_accusation_is_not_an_incident(monkeypatch):
    ctl, d = run_scenario(monkeypatch, 54720, "false-accusation", 90)
    try:
        assert ctl.state()["last"]["ok"] is True
        assert d.incident_list() == []                                                    # nothing was contained
        assert any(e["category"] in ("TRUST_CHANGE", "FLAG") for e in d.COLLECTOR.all_events())
    finally:
        ctl.stop()


def test_real_escalated_incident_shows_attempts_snapshots_and_why_it_is_stuck(monkeypatch):
    ctl, d = run_scenario(monkeypatch, 54740, "failed-validation", 200)
    try:
        assert ctl.state()["last"]["ok"] is True, ctl.state()["last"]
        inc = d.build_incident("SIMULATOR", "C", 1)
        assert inc["status"] == "NEEDS_HUMAN" and inc["current"]["consensus"]["attention"]
        att = inc["recovery"]["attempts"]
        assert [a["attempt"] for a in att] == [1, 2, 3] and all(a["snapshots"] for a in att)
        assert all("failed" in (a["retry_reason"] or "") for a in att[1:])
        assert {p["proposal"] for p in inc["proposals"]} >= {"RETRY_RECOVERY:C:1:2", "RETRY_RECOVERY:C:1:3"}
        redeploys = [a for a in inc["actions"] if a["step"].startswith("redeploy")]
        assert len(redeploys) == 3 and all(a["verified"]["state"] == "yes" for a in redeploys)    # the redeploys WERE applied; validation is what failed
        ns = inc["current"]["consensus"]["next_step"]
        assert ns["step"] == "a human has to act" and "recovery attempts failed" in ns["blocked_by"]
        esc = next(a for a in inc["actions"] if a["step"] == "escalate to a human")
        assert esc["kind"] == "escalation" and esc["attempted"]["state"] == "n/a" and esc["verified"]["state"] == "n/a"   # not a cluster call
        assert not any(r["outcome"] == "unknown" for a in inc["actions"] for r in a["records"])
    finally:
        ctl.stop()


# ------------------------------------------------------------------ the page, in a browser (skipped if unavailable)
def test_incident_view_in_a_browser(monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    d = load_dash(monkeypatch)
    decs = [dec("CONTAIN", 1, just=CONTAIN_JUST, voters=("A", "B", "C")), dec("RETRY_RECOVERY", 1, "2", t=120.0)]
    audit = [rec("isolate:C:1", "A", "applied"), rec("isolate:C:1", "B", "already_applied"),
             rec("recover:C:1:1", "A", "unknown", t=102.0), rec("stage:C:1:RESTRICTED", "A", "applied_unconfirmed", t=130.0)]
    ns = {"step": "advance to RESTRICTED", "ready": False, "blocked_by": "workload trust 5 is below the 20 needed for RESTRICTED"}
    from test_dashboard_tabs import status as full_status          # a complete /status, so the whole page can render
    full = {}
    for n in "ABCD":
        st = full_status(n, {"C": "REINTEGRATING"})
        st["targets"]["C"].update(stage="QUARANTINE", epoch=1, next_step=ns)
        full[n] = st
    d.COLLECTOR.status = full
    for x in decs:
        d.COLLECTOR._remember("A", {"data_source": SRC, "decisions": [x], "actions": {"recent": []}})
    d.COLLECTOR._remember("A", {"data_source": SRC, "decisions": [], "actions": {"recent": audit}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page(viewport={"width": 1300, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            base = f"http://127.0.0.1:{srv.server_address[1]}/"
            pg.goto(base + "#incidents")
            pg.wait_for_selector("#inctable tr.click")
            assert "#1" in pg.inner_text("#inctable") and "IN PROGRESS" in pg.inner_text("#inctable")
            pg.click("#inctable tr.click")
            pg.wait_for_selector("#incdetail h2")
            assert pg.evaluate("location.hash") == "#incident/SIMULATOR/C/1" and pg.is_visible("#incdetail") and not pg.is_visible("#inclist")
            text = pg.inner_text("#incdetail")
            for want in ("Where it stands now", "What triggered it", "PROPOSED", "AUTHORIZED", "ATTEMPTED", "VERIFIED", "Votes and who signed",
                         "Recovery attempts", "Investigations", "Trust changes and why", "Rejected evidence",
                         "Blocked because", "workload trust 5 is below the 20 needed", "UNKNOWN", "(sent ×2)"):
                assert want in text, want
            rows = pg.locator("#incdetail table").nth(1).locator("tr").all_inner_texts()
            iso = next(r for r in rows if r.startswith("isolate"))
            assert iso.count("YES") == 4                                                      # proposed, authorized, attempted, verified
            assert any("UNKNOWN" in r for r in rows if r.startswith("redeploy"))              # a timeout nobody could read is shown as unknown
            assert any("NO" in r.split("YES")[-1] for r in rows if r.startswith("move to RESTRICTED"))   # attempted but not verified
            pg.click("#incback")
            pg.wait_for_selector("#inclist:not([hidden])")
            assert pg.is_visible("#inclist") and not pg.is_visible("#incdetail")
            pg.goto(base + "#incident/SIMULATOR/C/1")                                          # deep link
            pg.wait_for_selector("#incdetail h2")
            pg.click("#tab-incidents")
            pg.wait_for_selector("#inclist:not([hidden])")
            assert pg.is_visible("#inclist")                                                   # the tab itself returns to the list
            pg.goto(base + "#incident/SIMULATOR/C/77")
            pg.wait_for_selector("#incdetail .mut")
            assert "not (or no longer) known" in pg.inner_text("#incdetail")
            assert errs == [], errs
            b.close()
    finally:
        srv.shutdown()
