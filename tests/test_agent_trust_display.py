"""Agent trust on the cards and the "Who trusts whom" grid: the card value is the MEDIAN of the other agents' own scores for that agent,
the grid shows exactly what each agent reports (no diagonal, a missing row when an agent is down), thresholds come from the agents."""
import os
import statistics
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_evaluation_tab import ROOT, load_dash, serve   # noqa: E402,F401

IDS = "ABCD"
# what each agent (row) reports about each agent (column); the diagonal is the agent's own 100 (never used)
TABLE = {"A": {"A": 100, "B": 100, "C": 90, "D": 100}, "B": {"A": 80, "B": 100, "C": 70, "D": 100},
         "C": {"A": 40, "B": 100, "C": 100, "D": 100}, "D": {"A": 20, "B": 100, "C": 45, "D": 100}}


def status(n, table=TABLE):
    tg = {t: {"phase": "HEALTHY", "stage": "FULL", "workload": f"w{t}", "epoch": 0, "last_decision": None, "detections": [], "workload_trust": 100,
              "attempt": 1, "attention": None, "display_state": "HEALTHY"} for t in "ABCDEF"}
    flags = {p: {"suspect": table[n][p] < 50, "vote_excluded": table[n][p] < 40} for p in IDS if p != n}
    return {"node": n, "mode": "distributed", "time": 1.0, "data_source": "SIMULATOR", "targets": tg, "agent_trust": dict(table[n]), "flags": flags,
            "thresholds": {"suspect_below": 50.0, "vote_min_trust": 40.0}, "decisions": [], "rejections": [], "pending_votes": {},
            "event_boot": "b", "event_seq": 0, "events": [], "counters": None, "investigations": {"enabled": True, "active": [], "recent": [], "watch": {}}}


def setup(monkeypatch, drop=()):
    d = load_dash(monkeypatch)
    d.COLLECTOR.status = {n: status(n) for n in IDS if n not in drop}
    return d


def test_card_value_is_the_median_of_the_other_agents_own_scores(monkeypatch):
    d = setup(monkeypatch)
    nodes = d.aggregate()["nodes"]
    for j in IDS:
        others = [TABLE[i][j] for i in IDS if i != j]
        assert nodes[j]["agent_trust"] == round(statistics.median(others), 1), j                 # median of the three others (own opinion excluded)
        assert nodes[j]["agent_trust_views"] == {i: TABLE[i][j] for i in IDS if i != j}
    assert nodes["A"]["agent_trust"] == 40 and nodes["C"]["agent_trust"] == 70 and nodes["D"]["agent_trust"] == 100
    assert nodes["E"]["agent_trust"] is None and nodes["E"]["has_agent"] is False and nodes["F"]["has_agent"] is False        # services without an agent


def test_median_uses_only_agents_that_answer(monkeypatch):
    d = setup(monkeypatch, drop=("D",))                                                          # agent D is down: its opinion is simply missing
    n = d.aggregate()["nodes"]
    assert n["A"]["agent_trust_views"] == {"B": 80, "C": 40} and n["A"]["agent_trust"] == 60.0  # median of two values = their mean


def test_trust_matrix_has_only_reported_values(monkeypatch):
    d = setup(monkeypatch, drop=("D",))
    m = d.aggregate()["trust_matrix"]
    assert m["agents"] == list(IDS) and m["suspect_below"] == 50.0 and m["vote_min_trust"] == 40.0
    assert sorted(m["rows"]) == ["A", "B", "C"]                                                  # D reports nothing: no row, nothing invented
    for judge, row in m["rows"].items():
        assert judge not in row                                                                  # no diagonal
        assert {j: c["trust"] for j, c in row.items()} == {j: TABLE[judge][j] for j in IDS if j != judge}
    assert m["rows"]["B"]["C"] == {"trust": 70, "suspect": False, "vote_excluded": False}
    assert m["rows"]["C"]["A"] == {"trust": 40, "suspect": True, "vote_excluded": False}


def open_page(monkeypatch, serve, view=None, drop=()):
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    d = setup(monkeypatch, drop)
    url = serve(d)
    p = pw.sync_playwright().start()
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    pg = b.new_page(viewport={"width": 1500, "height": 1100})
    if view:
        pg.add_init_script("try { localStorage.setItem('cr-view', '%s') } catch (e) {}" % view)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url + "/")
    pg.wait_for_selector("#nodes .card")
    pg.wait_for_function("document.querySelectorAll('#trustgrid-body table').length === 1")
    return p, b, pg, errs


def test_cards_name_the_agent_and_the_calculation_in_simple_view(monkeypatch, serve):
    p, b, pg, errs = open_page(monkeypatch, serve)
    try:
        assert pg.evaluate("document.body.dataset.view") == "simple"
        a = pg.inner_text('#nodes [data-node="A"]')
        assert "Agent A (security agent on this node)" in a and "trust 40" in a and "median of B, C, D" in a
        assert "B: 80 · C: 40 · D: 20" in a                                                       # the three individual scores, visible in Simple view
        assert "Workload trust (this service's own health)" in a and "SUSPECT" in a                # median 40 is below 50
        e = pg.inner_text('#nodes [data-node="E"]')
        assert "No security agent on this service" in e and "watched by all four agents (A, B, C, D)" in e and "median of" not in e
        d_ = pg.inner_text('#nodes [data-node="D"]')
        assert "Agent D (security agent on this node)" in d_ and "trust 100" in d_
        tips = pg.evaluate("[...document.querySelectorAll('#nodes .tip')].map(t => t.title)")
        assert any("about the SERVICE itself" in t for t in tips) and any("about a SECURITY AGENT" in t and "median" in t for t in tips)
        assert errs == [], errs
    finally:
        b.close()
        p.stop()


def test_who_trusts_whom_grid_collapsed_in_simple_open_in_detailed(monkeypatch, serve):
    p, b, pg, errs = open_page(monkeypatch, serve)
    try:
        assert pg.evaluate("document.getElementById('trustgrid').open") is False                  # Simple view: collapsed
        pg.click("#view-detailed")
        assert pg.evaluate("document.getElementById('trustgrid').open") is True                   # Detailed view: open
        pg.click("#view-simple")
        assert pg.evaluate("document.getElementById('trustgrid').open") is False
        pg.click("#trustgrid summary")                                                            # the presenter can still open it in Simple view
        assert pg.is_visible("#trustgrid-body table")
        cells = pg.evaluate("[...document.querySelectorAll('#trustgrid-body tr')].map(r => [...r.children].map(c => c.innerText.replace(/\\s+/g, ' ').trim()))")
        assert len(cells) == 5 and len(cells[0]) == 5                                              # header + 4 rows, label + 4 columns
        for k, judge in enumerate(IDS, start=1):
            assert f"Agent {judge}'s trust in:" in cells[k][0]
            for m_, judged in enumerate(IDS, start=1):
                c = cells[k][m_]
                if judge == judged:
                    assert c == "\u2014", (judge, judged, c)                                      # diagonal
                else:
                    assert c.startswith(str(TABLE[judge][judged])), (judge, judged, c)
        assert "SUSPECT" in cells[3][1] and "votes ignored" not in cells[3][1]                    # C's 40 for A: below 50, not below 40
        assert "votes ignored" in cells[4][1]                                                      # D's 20 for A
        assert "SUSPECT" in cells[4][3] and "SUSPECT" not in cells[1][3]                           # D's 45 for C is below 50; A's 90 is not
        assert errs == [], errs
    finally:
        b.close()
        p.stop()


def test_grid_says_not_reported_for_an_agent_that_is_down(monkeypatch, serve):
    p, b, pg, errs = open_page(monkeypatch, serve, view="detailed", drop=("D",))
    try:
        cells = pg.evaluate("[...document.querySelectorAll('#trustgrid-body tr')].map(r => [...r.children].map(c => c.innerText.replace(/\\s+/g, ' ').trim()))")
        assert cells[4][1:] == ["not reported"] * 3 + ["\u2014"]                                   # D's row: nothing invented, diagonal still a dash
        assert cells[1][4].startswith("100")                                                       # A still reports its own score for D
        a = pg.inner_text('#nodes [data-node="D"]')
        assert "Agent unavailable" in a
        assert errs == [], errs
    finally:
        b.close()
        p.stop()
