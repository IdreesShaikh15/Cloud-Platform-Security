"""Healthcare Phase C: scenarios described in healthcare terms, dependency/impact panel, persistence re-check, obstruct."""
import importlib.util
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()


# ------------------------------------------------------------------ scenarios in healthcare terms
def test_every_scenario_is_described_in_healthcare_terms_and_keeps_its_id():
    import scenarios as sc
    from world import WL
    assert set(sc.CATALOGUE) == {"app-compromise", "false-accusation", "forge-evidence", "agent-crash", "transient-blip", "slow-burn",
                                 "ambiguous", "validation-retry", "failed-validation", "obstruct"}
    ehr, login = WL.display("C"), WL.display("B")
    for sid, (s, _fn) in sc.CATALOGUE.items():
        text = f"{s.title} {s.description} {s.expect}"
        assert s.id == sid and s.title and s.description and s.expect, sid
        assert "records-api" not in text and "auth-service" not in text, sid          # internal names stay out of the prose
    t = {sid: s.title for sid, (s, _f) in sc.CATALOGUE.items()}
    assert t["app-compromise"] == f"Patient records being copied out of the {ehr}"
    assert t["false-accusation"] == f"A security agent falsely accuses the {login}"
    assert "synthetic" in sc.CATALOGUE["app-compromise"][0].description
    assert ehr == "Electronic Health Records (EHR) API" and login == "Login & Access Service"


def test_the_demo_picker_lists_the_obstruct_scenario():
    import scenarios as sc
    assert [x["id"] for x in sc.listing()][-1] == "obstruct"
    d = sc.CATALOGUE["obstruct"][0]
    assert "refuses to vote" in d.title and "trust is not reduced" in d.expect


# ------------------------------------------------------------------ the page
def test_page_has_the_synthetic_label_and_the_dependency_panel():
    for text in ("SYNTHETIC DATA ONLY: no real patient information", 'id="depsec"', "function dependentsOf", "cannot ${esc(nodes[d].provides",
                 "does not change what the agents do", "suggested recovery order"):
        assert text in HTML, text


def _rig():
    import test_two_pages as T
    return T


def test_dependency_panel_and_display_names_in_a_browser(monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    T = _rig()
    r = T.Rig(monkeypatch)
    try:
        meta = {"A": ("patient-portal", "Patient Portal", "show patient records", ["B", "C"], 4),
                "B": ("auth-service", "Login & Access Service", "sign people in and check their access", [], 2),
                "C": ("records-api", "Electronic Health Records (EHR) API", "serve patient records", ["B", "D"], 1),
                "D": ("database", "Clinical Database", "store patient records", [], 3)}
        for node in "ABCD":
            f = r.fakes["demo"]["ABCD".index(node)]
            st = T.agent_status(node, "SIMULATOR")
            for n, (wl, dn, prov, dep, pri) in meta.items():
                st["targets"][n].update(workload=wl, display_name=dn, provides=prov, depends_on=dep, recovery_priority=pri,
                                        priority_rationale=f"why {n}")
            st["targets"]["C"].update(phase="ISOLATED", stage="QUARANTINE", display_state="ISOLATED")
            f.data = st
        r.serve("live", "LIVE_CLUSTER")
        r.poll()
        agg = r.get("/api/state?channel=demo")[1]
        assert agg["nodes"]["C"]["display_name"] == "Electronic Health Records (EHR) API" and agg["nodes"]["C"]["depends_on"] == ["B", "D"]
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page(viewport={"width": 1400, "height": 1100})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(r.url + "/?mode=demo")
            pg.wait_for_selector("#deptable tr")
            text = pg.inner_text("#depsec")
            assert "the Patient Portal cannot show patient records" in text          # isolating the EHR API (or the login service)
            assert "Electronic Health Records (EHR) API" in pg.inner_text("#nodes") and "(records-api)" in pg.inner_text("#nodes")
            assert "ISOLATED NOW" in text and "does not change what the agents do" in text
            rows = pg.locator("#deptable tr").all_inner_texts()
            assert rows[1].startswith("C") and "1st" in rows[1] and "why C" in rows[1]           # sorted by the configured order
            assert "no other service depends on it" in rows[-1] and "Patient Portal" in rows[-1]
            assert pg.inner_text("#synth").startswith("SYNTHETIC DATA ONLY")
            assert errs == [], errs
            b.close()
    finally:
        r.close()


def test_dependency_panel_stays_hidden_when_the_agents_report_no_metadata(monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    T = _rig()
    r = T.Rig(monkeypatch)
    try:
        r.serve("demo", "SIMULATOR")
        r.serve("live", "LIVE_CLUSTER")
        r.poll()
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page()
            pg.goto(r.url + "/?mode=demo")
            pg.wait_for_selector("#counters")
            pg.wait_for_timeout(1500)
            assert pg.locator("#depsec").is_hidden()
            b.close()
    finally:
        r.close()


# ------------------------------------------------------------------ persist_recheck_s
def _cluster(port, persist):
    from world import LocalCluster, fast_config, sim_investigation
    cfg = fast_config(port, sim_investigation())
    cfg.quorum.persist_recheck_s = persist
    c = LocalCluster(base_port=port, cfg=cfg).start()
    time.sleep(2)
    return c


def test_the_recheck_is_off_by_default():
    from resilience.config import QuorumParams
    from world import fast_config
    assert QuorumParams().persist_recheck_s == 0.0 and fast_config(50900).quorum.persist_recheck_s == 0.0


def test_recheck_delays_a_network_only_anomaly_and_says_why():
    c = _cluster(50910, 4.0)
    try:
        t0 = time.time()
        c.mark("recheck", "C")
        c.world.inject_connections("C", lambda x: 30.0)                 # sustained, network only
        time.sleep(2.0)
        assert all(a.states["C"].epoch == 0 for a in c.agents.values())      # still waiting after 2 s
        st = c.agents["B"].status()
        vr = st["vote_reasons"]["CONTAIN:C"]
        assert vr["voted"] is False and "re-checking" in vr["reason"] and "persist_recheck_s=4" in vr["reason"]
        assert any(e["category"] == "VOTE_WITHHELD" and "re-checking" in e["summary"] for e in c.agents["B"].events.recent())
        assert not any(k.startswith("CONTAIN:C") for k in c.agents["B"].votes.pending())      # a missing vote is never approval
        assert c.wait_until(lambda: all(a.states["C"].epoch >= 1 for a in c.agents.values()), 20)
        assert 3.5 <= time.time() - t0 <= 12
    finally:
        c.stop()


def test_recheck_ignores_a_short_spike():
    c = _cluster(50920, 4.0)
    try:
        c.world.inject_connections("C", lambda x: 30.0, duration=2.0)
        time.sleep(9)
        assert all(a.states["C"].epoch == 0 for a in c.agents.values())
    finally:
        c.stop()


def test_recheck_never_delays_a_file_or_process_change():
    c = _cluster(50930, 8.0)
    try:
        t0 = time.time()
        c.world.attack("C", ("tamper",))
        assert c.wait_until(lambda: all(a.states["C"].epoch >= 1 for a in c.agents.values()), 6)
        assert time.time() - t0 < 6
        c.world.attack("D", ("exfil",))                                 # exfil also starts an unexpected process: not delayed either
        assert c.wait_until(lambda: all(a.states["D"].epoch >= 1 for a in c.agents.values()), 6)
    finally:
        c.stop()


def test_an_attacker_who_pulses_under_the_wait_is_not_contained_and_the_limit_is_real():
    c = _cluster(50940, 5.0)
    try:
        c.world.inject_connections("C", lambda x: 30.0 if (x % 5.0) < 3.0 else 2.0)      # 3 s on, 2 s off
        time.sleep(14)
        assert all(a.states["C"].epoch == 0 for a in c.agents.values())      # evades the 5 s re-check (documented limit)
    finally:
        c.stop()
    c = _cluster(50950, 0.0)
    try:
        c.world.inject_connections("C", lambda x: 30.0 if (x % 5.0) < 3.0 else 2.0)
        assert c.wait_until(lambda: any(a.states["C"].epoch >= 1 for a in c.agents.values()), 10)      # with the re-check off it is caught
    finally:
        c.stop()


# ------------------------------------------------------------------ obstruct
def test_obstruct_the_other_three_still_contain_and_the_withheld_votes_are_shown():
    import scenarios as sc
    from world import LocalCluster, fast_config, sim_investigation
    c = LocalCluster(base_port=50960, cfg=fast_config(50960, sim_investigation())).start()
    try:
        time.sleep(2)
        ok, detail = sc.obstruct(sc.Runner(c, lambda: False))
        assert ok, detail
        f = sc.obstruct_facts(c)
        assert f["attacker_voted"] is False and all(sorted(v) == ["B", "C", "D"] for v in f["contain_voters"].values())
        assert f["withheld_events"] >= 3
        # no new trust rule: A is NOT penalised for a missing vote
        assert all(t == 100.0 for t in f["trust_in_attacker"].values())
        # the reason is on record on A itself
        vr = c.agents["A"].vote_reasons
        assert any(v["voted"] is False and "obstruction" in v["reason"] for v in vr.values())
        assert any(e["category"] == "VOTE_WITHHELD" and e["details"].get("obstruction") for e in c.agents["A"].events.recent())
    finally:
        c.stop()


def test_obstruct_withholds_only_the_named_workload_and_changes_nothing_else():
    from world import LocalCluster, fast_config, sim_investigation
    c = LocalCluster(base_port=50970, cfg=fast_config(50970, sim_investigation())).start()
    try:
        time.sleep(2)
        c.compromise_agent("A", "C", mode="obstruct")
        assert c.wait_until(lambda: c.agents["A"].status()["compromised"] == "obstruct", 5)
        c.world.attack("B", ("tamper",))                                      # a different workload: A is not obstructing it
        assert c.wait_until(lambda: all(a.states["B"].epoch >= 1 for a in c.agents.values()), 15)
        assert "A" in next(d["voters"] for d in c.agents["B"].decisions if d["action"] == "CONTAIN" and d["target"] == "B")
        assert c.agents["A"].obstructed == []
    finally:
        c.stop()


def test_obstruct_is_an_agent_side_switch_only_and_the_cli_accepts_it():
    from resilience.simhooks import Compromise
    c = Compromise(mode="obstruct", target="C")
    assert c.obstructing("C") and not c.obstructing("B") and not c.active           # not a "false accusation": nothing is fabricated
    src = open(os.path.join(ROOT, "agent", "resilience", "__main__.py"), encoding="utf-8").read()
    assert '"obstruct"' in src
    sh = open(os.path.join(ROOT, "scripts", "simulate-compromised-agent.sh"), encoding="utf-8").read()
    assert "obstruct" in sh
