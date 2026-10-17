"""Healthcare Phase D: one plain-English vocabulary, tooltips, technical details, five honest agent states, grouped evidence,
'skipped' instead of 'REFUSED' for a step another agent had already done, collapsible evaluation text."""
import json
import os
import re
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
import test_two_pages as T          # noqa: E402  (stand-in agents + dashboard rig)


def shown(pg, sel):
    """The text a person SEES: like innerText, but without the contents of <details> that are closed."""
    return pg.evaluate("""(sel) => { const r = document.querySelector(sel).cloneNode(true);
      r.querySelectorAll('details:not([open])').forEach(d => [...d.children].forEach(c => { if (c.tagName !== 'SUMMARY') c.remove(); }));
      return r.textContent; }""", sel)


def _pw():
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    return pw


# ------------------------------------------------------------------ vocabulary
def test_the_requested_vocabulary_is_used_everywhere_from_one_table():
    for term in ("Security Alerts", "Investigate", "Recovery", "Performance Results", "Activity Log", "Network Activity", "Running Processes",
                 "File Changes", "Evidence Shared", "Agent Decision", "Agreement Reached (3 of 4)", "Isolation", "Recovery Check",
                 "Normal Access Restored"):
        assert term in HTML, term
    assert html_has_one("const V = {")                                              # ONE vocabulary table
    for old in (">Incidents <span", ">Investigations <span", ">Evaluation</button>", ">Audit <span"):
        assert old not in HTML, old


def html_has_one(s):
    return HTML.count(s) == 1


def test_tooltips_explain_trust_agreement_evidence_score_and_confidence():
    m = re.search(r"const HELP = \{(.*?)\n\};", HTML, re.S).group(1)
    for key in ("workloadTrust", "agentTrust", "quorum", "evidenceScore", "confidence", "recoveryCheck"):
        assert key + ":" in m, key
    assert "Agreement Reached (3 of 4)" in m and "never approval" in m and "no combined score" in m


def test_four_separate_explanations_and_no_combined_score():
    box = re.search(r'<details class="evd meaning[^"]*".*?</details>', HTML, re.S).group(0)
    for term in ("Workload trust", "Agent trust", "Evidence confidence", "Recovery Check"):
        assert f"<b>{term}" in box, term
    assert "no single combined" in box
    assert HTML.lower().count("security score") == 1                                 # only the sentence that denies it exists


# ------------------------------------------------------------------ five honest agent states (pure function + views)
def test_agent_response_has_five_states_and_a_missing_vote_is_never_approval():
    d = T.Rig.__init__ and None
    import importlib.util
    spec = importlib.util.spec_from_file_location("dash_plain_unit", os.path.join(ROOT, "dashboard", "server.py"))
    os.environ.setdefault("STATUS_URLS", "http://127.0.0.1:1/status")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    r = m.agent_response
    assert r(voted=True, up=True)["response"] == "agrees"
    assert r(voted=False, up=False)["response"] == "unavailable"
    assert r(voted=False, up=True, failed="bad signature")["response"] == "failed_verification"
    assert r(voted=False, up=True, withheld_reason="own observation normal")["response"] == "disagrees"
    assert r(voted=False, up=True, withheld_reason="re-checking: waiting 3s more")["response"] == "not_yet"      # waiting is not refusing
    assert r(voted=False, up=True)["response"] == "not_yet"
    # precedence: a counted vote is a yes whatever else is said; an unavailable agent is unavailable; failure beats a recorded reason
    assert r(voted=True, up=False, failed="x", withheld_reason="y")["response"] == "agrees"
    assert r(voted=False, up=False, failed="x", withheld_reason="y")["response"] == "unavailable"
    assert r(voted=False, up=True, failed="x", withheld_reason="y")["response"] == "failed_verification"
    assert set(m.RESPONSES) == {"agrees", "disagrees", "unavailable", "not_yet", "failed_verification"}
    # exactly one state counts as approval
    assert [k for k in m.RESPONSES if k == "agrees"] == ["agrees"]
    agent_src = open(os.path.join(ROOT, "agent", "resilience", "agent.py"), encoding="utf-8").read()
    assert f'"{m.RECHECK_WORDING}:' in agent_src            # the dashboard's "waiting" test matches the agent's own wording


def _pending_statuses(rejection_from=None):
    key = "CONTAIN:C:1:"
    out = {}
    for n in "ABC":                                          # D is not answering
        st = T.agent_status(n, "SIMULATOR")
        st["pending_votes_detail"] = {key: {"voters": ["A"], "excluded": [], "counted": ["A"], "needed": 3}}
        st["pending_votes"] = {key: ["A"]}
        st["vote_reasons"] = {}
        if n == "B":
            st["vote_reasons"]["CONTAIN:C"] = {"proposal": key, "voted": False, "reason": "own observation of C normal (confidence 0.10 < 0.5)"}
        if n == "C":
            st["vote_reasons"]["CONTAIN:C"] = {"proposal": key, "voted": False, "reason": "re-checking: the anomaly has lasted 1s; waiting 5s more"}
        if rejection_from and n == "A":
            st["rejections"] = [{"t": time.time(), "from": rejection_from, "claimed_signer": rejection_from, "kind": "vote", "reason": "bad signature"}]
        out[n] = st
    return out


def test_pending_votes_show_each_agent_in_exactly_one_of_five_states(monkeypatch):
    r = T.Rig(monkeypatch)
    try:
        v = r.d._vote_view("CONTAIN:C:1:", _pending_statuses(), list("ABCD"))
        got = {a["agent"]: a["response"] for a in v["agents"]}
        assert got == {"A": "agrees", "B": "disagrees", "C": "not_yet", "D": "unavailable"}
        assert v["voters"] == ["A"]                                                     # only the signed vote counts
        v = r.d._vote_view("CONTAIN:C:1:", _pending_statuses(rejection_from="B"), list("ABCD"))
        got = {a["agent"]: a["response"] for a in v["agents"]}
        assert got["B"] == "failed_verification" and got["A"] == "agrees" and got["D"] == "unavailable"
        assert "bad signature" in next(a["reason"] for a in v["agents"] if a["agent"] == "B")
    finally:
        r.close()


def test_the_pending_panel_in_a_browser(monkeypatch):
    pw = _pw()
    r = T.Rig(monkeypatch)
    try:
        sts = _pending_statuses(rejection_from="B")
        for n in "ABC":
            r.fakes["demo"]["ABCD".index(n)].data = sts[n]
        r.fakes["demo"][3].data = None
        r.serve("live", "LIVE_CLUSTER")
        r.poll()
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page(viewport={"width": 1300, "height": 900})
            pg.add_init_script("try { localStorage.setItem('cr-view', 'detailed') } catch (e) {}")
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(r.url + "/?mode=demo#incidents")
            pg.wait_for_selector("#pend table")
            text = pg.inner_text("#pend")
            for want in ("Isolate the", "1 of the 3 needed agree", "✔ Agrees", "⚠ Message failed verification", "⛔ Unavailable", "A missing vote"):
                assert want in text or want in pg.inner_text("#panel-incidents"), want
            assert "Not yet responded" in text or "Disagrees" in text
            assert errs == [], errs
            b.close()
    finally:
        r.close()


def test_obstruct_incident_shows_the_obstructing_agent_as_disagrees_end_to_end(monkeypatch):
    import test_incident_detail as TI
    ctl, d = TI.run_scenario(monkeypatch, 54760, "obstruct", 150)
    try:
        assert ctl.state()["last"]["ok"] is True, ctl.state()["last"]
        inc = d.build_incident("SIMULATOR", "C", 1)
        assert inc["proposals"] and all(p["committed"] for p in inc["proposals"])
        for p in inc["proposals"]:
            r = p["responses"]
            assert {a for a, x in r.items() if x["response"] == "agrees"} == {"B", "C", "D"}, p["proposal"]
            assert r["A"]["response"] == "disagrees" and "obstruction" in r["A"]["reason"], r["A"]
    finally:
        ctl.stop()


# ------------------------------------------------------------------ the incident view, rendered with crafted data
def _incident(**over):
    prop = lambda k, resp: {"proposal": k, "text": "x", "committed": True, "committed_at": 1.0, "signers": ["B", "C", "D"], "note": "",   # noqa: E731
                            "votes": [{"voter": v, "score": 1.0, "ts": 1.0} for v in "BCD"], "withheld": [], "responses": resp}
    base = {"id": "SIMULATOR:C:1", "source": "SIMULATOR", "target": "C", "epoch": 1, "workload": "records-api", "status": "ACTIVE",
            "window": {"from": 1.0, "contained_at": 2.0, "to": None}, "limits": "x",
            "current": {"consensus": {"phase": "REINTEGRATING", "stage": "RESTRICTED", "attempt": 1,
                                      "next_step": {"step": "advance to MONITORED", "blocked_by": "workload trust 5 is below the 20 needed", "agents_agreeing": 3, "agents_reporting": 3}},
                        "per_agent": {"A": {"phase": "REINTEGRATING", "stage": "RESTRICTED", "workload_trust": 5}}},
            "trigger": {"decision": {}, "score": {"W": 1.3}, "text": "Agents B, C, D signed votes.", "seen_by": ["A"],
                        "evidence_by_agent": {"A": [
                            {"type": "NETWORK", "confidence": 1.0, "summary": "40 outbound conns, tx 10 KiB/s", "timestamp": 10.0, "last_timestamp": 10.0, "count": 1},
                            {"type": "NETWORK", "confidence": 1.0, "summary": "40 outbound conns, tx 1954 KiB/s", "timestamp": 12.0, "last_timestamp": 15.0, "count": 3},
                            {"type": "FILE_INTEGRITY", "confidence": 0.95, "summary": "1 modified file(s): static/index.html", "timestamp": 11.0, "last_timestamp": 11.0, "count": 1}]}},
            "actions": [{"proposal": "CONTAIN:C:1:", "step": "isolate (quarantine policy)", "kind": "cluster-action",
                         "proposed": {"state": "yes", "by": ["B"]}, "authorized": {"state": "yes", "signers": ["B", "C", "D"], "at": 3.0},
                         "attempted": {"state": "yes", "by": ["B"], "tries": 1}, "verified": {"state": "yes", "by": ["B"], "at": 3.0},
                         "refused": [{"node": "D", "outcome": "refused_stale", "detail": "C is now REINTEGRATING, not RECOVERING/VALIDATING"}],
                         "records": [{"node": "B", "t": 3.0, "outcome": "applied", "attempt": 1, "detail": "", "error": "", "duration_s": 0.1},
                                     {"node": "D", "t": 4.0, "outcome": "refused_stale", "attempt": 1, "detail": "C is now REINTEGRATING", "error": "", "duration_s": 0}]},
                        {"proposal": "ADVANCE_STAGE:C:1:RESTRICTED", "step": "move to RESTRICTED", "kind": "cluster-action",
                         "proposed": {"state": "yes", "by": ["B"]}, "authorized": {"state": "yes", "signers": ["B", "C", "D"], "at": 5.0},
                         "attempted": {"state": "no", "by": [], "tries": 0}, "verified": {"state": "no", "by": [], "at": None},
                         "refused": [{"node": "B", "outcome": "refused_certificate", "detail": "invalid quorum certificate"}],
                         "records": [{"node": "B", "t": 5.0, "outcome": "refused_certificate", "attempt": 1, "detail": "invalid", "error": "", "duration_s": 0}]}],
            "proposals": [prop("CONTAIN:C:1:", {"A": {"response": "failed_verification", "reason": "a message from this agent failed verification and was not counted: bad signature"},
                                                "B": {"response": "agrees", "reason": ""}, "C": {"response": "agrees", "reason": ""}, "D": {"response": "agrees", "reason": ""}}),
                          prop("VALIDATE:C:1:", {"A": {"response": "unavailable", "reason": "this agent is not answering"}, "B": {"response": "agrees", "reason": ""},
                                                 "C": {"response": "not_yet", "reason": "re-checking: waiting"}, "D": {"response": "disagrees", "reason": "obstruction"}})],
            "recovery": {"attempts": [], "snapshots": []}, "investigations": [], "trust_changes": [], "rejected": [], "observations": [],
            "evidence_sent": [], "evidence_received": []}
    base.update(over)
    return base


def test_incident_view_groups_evidence_hides_technical_details_and_says_skipped_not_refused(monkeypatch):
    pw = _pw()
    r = T.Rig(monkeypatch)
    try:
        r.serve("demo", "SIMULATOR")
        r.serve("live", "LIVE_CLUSTER")
        r.poll()
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page(viewport={"width": 1300, "height": 900})
            pg.add_init_script("try { localStorage.setItem('cr-view', 'detailed') } catch (e) {}")
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(r.url + "/?mode=demo")
            pg.wait_for_selector("#counters")
            pg.evaluate("(i) => { const box = document.getElementById('incdetail'); box.hidden = false; box.innerHTML = renderIncident(i); }", _incident())
            main = shown(pg, "#incdetail")
            full = pg.text_content("#incdetail")
            # evidence is grouped by agent and signal: one row per signal with the LATEST reading and a count, the rest on request
            rows = pg.locator("#incdetail table").first.locator("tr").all_inner_texts()
            assert len(rows) == 3                                                       # header + Network Activity + File Changes (4 readings, 2 rows)
            net = next(x for x in rows if "Network Activity" in x)
            assert "1954 KiB/s" in net and "\t4\t" in net.replace("  ", "\t") or " 4" in net                  # the latest reading, 4 readings in total
            assert "all 2 different readings" in net and "File Changes" in " ".join(rows)
            # raw numbers live in technical details, not in the main view
            assert "W(T)" not in main and "0.95" not in main and "CONTAIN:C:1:" not in main and "REINTEGRATING" not in main
            assert "W(T)" in full and "0.95" in full and "CONTAIN:C:1:" in full
            assert main.count("Technical details") >= 3
            assert pg.locator("#incdetail details.tech[open]").count() == 0
            # plain words
            assert "Agreed (3 of 4)" in main and "Normal Access Restored" not in main and "Limited Access" in main and "Isolation: block the workload" in main
            assert "Move to Monitored Access" in main
            # a step another agent had already done is SKIPPED, a missing 3-of-4 agreement is REFUSED
            assert "skipped 1 (already done by another agent)" in main and "REFUSED 1" in main and main.count("REFUSED 1") == 1
            assert "REFUSED: no valid 3-of-4 agreement" in full and "skipped: already done by another agent" in full
            # the five states
            for want in ("⚠ Message failed verification", "⛔ Unavailable", "… Not yet responded", "✖ Disagrees", "✔ Agrees", "A missing vote is never approval"):
                assert want in main, want
            assert errs == [], errs
            b.close()
    finally:
        r.close()


def test_outcome_words(monkeypatch):
    pw = _pw()
    r = T.Rig(monkeypatch)
    try:
        r.serve("demo", "SIMULATOR")
        r.serve("live", "LIVE_CLUSTER")
        r.poll()
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page()
            pg.add_init_script("try { localStorage.setItem('cr-view', 'detailed') } catch (e) {}")
            pg.goto(r.url + "/?mode=demo")
            pg.wait_for_selector("#counters")
            assert pg.evaluate("outcomeTxt('refused_stale')") == "skipped: already done by another agent"
            assert pg.evaluate("outcomeTxt('refused_certificate')").startswith("REFUSED")
            assert pg.evaluate("outcomeTxt('already_applied')") == "already done"
            assert pg.evaluate("[stageName('QUARANTINE'), stageName('FULL'), phaseName('VALIDATING'), sigName('FILE_INTEGRITY'), catName('QUORUM')]") == [
                "Isolation", "Normal Access Restored", "Recovery Check", "File Changes", "Agreement Reached (3 of 4)"]
            assert pg.evaluate("[actName('CONTAIN/isolate'), actName('VALIDATE'), actName('ADVANCE_STAGE/FULL')]") == [
                "Isolation", "Recovery Check", "Access step: Normal Access Restored"]
            b.close()
    finally:
        r.close()


# ------------------------------------------------------------------ the overview
def test_overview_uses_plain_words_with_tooltips_and_keeps_raw_values_in_technical_details(monkeypatch):
    pw = _pw()
    r = T.Rig(monkeypatch)
    try:
        for n in "ABCD":
            st = T.agent_status(n, "SIMULATOR")
            st["targets"]["C"].update(phase="REINTEGRATING", stage="QUARANTINE", display_state="REINTEGRATING", workload_trust=12,
                                      detections=[{"type": "NETWORK", "confidence": 1.0, "summary": "x"}, {"type": "FILE_INTEGRITY", "confidence": 0.95, "summary": "y"}],
                                      score={"W": 1.3, "items": []}, last_decision={"action": "CONTAIN", "stage": "", "voters": ["A", "B", "C"], "t": time.time(), "epoch": 1})
            r.fakes["demo"]["ABCD".index(n)].data = st
        r.serve("live", "LIVE_CLUSTER")
        r.poll()
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page(viewport={"width": 1400, "height": 1000})
            pg.add_init_script("try { localStorage.setItem('cr-view', 'detailed') } catch (e) {}")
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(r.url + "/?mode=demo")
            pg.wait_for_selector("#nodes .card")
            tabs = pg.locator(".tabs button").all_inner_texts()
            assert [re.sub(r"\s*\d+$", "", x.split("\n")[0].strip()) for x in tabs] == ["Overview", "Security Alerts", "Investigate", "Recovery", "Performance Results", "Activity Log"]
            cards = shown(pg, "#nodes")
            for want in ("Returning to normal access", "Isolation", "Workload trust", "Agent trust", "Evidence confidence now", "Network Activity 1", "File Changes 0.95"):
                assert want in cards, want
            for raw in ("W(T)", "QUARANTINE", "REINTEGRATING", "FILE_INTEGRITY"):
                assert raw not in cards, raw
            tips = pg.locator("#nodes .tip")
            assert tips.count() >= 12 and "0-100" in tips.first.get_attribute("title")
            assert "W(T)" in pg.text_content("#nodes")                                      # still there, inside technical details
            pg.locator("#nodes details summary").nth(2).click()                              # open C's technical details; must not open the drawer
            assert not pg.locator("#drawer.open").count()
            pg.wait_for_timeout(2500)                                                        # survives the one-second refresh
            assert pg.locator("#nodes details[open]").count() == 1 and "W(T) = 1.3" in pg.inner_text("#nodes")
            assert pg.locator("#meaning").is_visible() and not pg.locator("#meaning[open]").count()
            assert errs == [], errs
            b.close()
    finally:
        r.close()
