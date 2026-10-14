"""Part 2: tabs + Overview counters. Every existing panel survives, in the right tab; counters are derived from what the
agents report; severity is a word and an icon, not only a colour."""
import importlib.util
import os
import threading
import time
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer

import pytest

from resilience.agent import HEALTHY, REINTEGRATING, VALIDATING, RECOVERING, ResilienceAgent
from resilience.crypto import KeyRegistry, Signer
from resilience.evidence import Action, Vote
from resilience.quorum import Commit
from resilience.response import FakeBackend
from world import FakeTelemetry, FakeWorld, fast_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "dashboard", "index.html")).read()

TABS = ["overview", "incidents", "investigations", "recovery", "evaluation", "audit"]
# every panel/element the dashboard had before Part 2, and the tab it must now live in
PLACEMENT = {
    "overview": ["counters", "nodes", "simctl", "timeline", "nodefilters", "catfilters", "q", "evcount"],
    "incidents": ["dec", "pend", "met"],
    "investigations": ["invsec", "invreview", "inv"],
    "recovery": ["recsec", "recunk", "snaps", "acts"],
    "audit": ["rej", "dlogtab"],
}


class Tree(HTMLParser):
    """Records, for every id, the data-panel it is inside (None if outside any panel)."""

    def __init__(self):
        super().__init__()
        self.stack, self.where, self.panels = [], {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("br", "input", "link", "meta", "img", "hr"):
            if a.get("id"):
                self.where[a["id"]] = self._panel()
            return
        self.stack.append((tag, a.get("data-panel")))
        if a.get("data-panel"):
            self.panels.append(a["data-panel"])
        if a.get("id"):
            self.where[a["id"]] = self._panel()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def _panel(self):
        for _, p in reversed(self.stack):
            if p:
                return p
        return None


def tree():
    t = Tree()
    t.feed(HTML)
    return t


def test_six_tabs_with_accessible_roles():
    for t in TABS:
        assert f'id="tab-{t}"' in HTML and f'data-tab="{t}"' in HTML and f'aria-controls="panel-{t}"' in HTML
        assert f'id="panel-{t}"' in HTML and f'role="tabpanel"' in HTML
    assert 'role="tablist"' in HTML and 'aria-selected' in HTML
    assert tree().panels == TABS                                   # panels exist exactly once, in this order


def test_every_existing_panel_is_in_the_right_tab_and_none_is_lost():
    t = tree()
    for panel, ids in PLACEMENT.items():
        for i in ids:
            assert t.where.get(i) == panel, f"#{i} should be in the {panel} tab, found {t.where.get(i)!r}"
    # the global pieces stay outside every tab: banner, header, pause banner, drawer and the shade
    for i in ("srcbanner", "sub", "dlog", "pause", "live", "pausebanner", "drawer", "shade"):
        assert t.where.get(i) is None, i
    assert 'id="nodes"' in HTML and "grid" in HTML                  # the 4 node cards container is still there


def test_overview_has_node_cards_and_counters_first():
    ov = HTML[HTML.index('id="panel-overview"'):HTML.index('id="panel-incidents"')]
    assert ov.index('id="counters"') < ov.index('id="nodes"') < ov.index('id="timeline"')


def test_severity_is_a_word_and_an_icon_not_only_colour():
    assert "NOT ALL HEALTHY" in HTML and "UNREACHABLE" in HTML and "NEEDS HUMAN" in HTML and "REVIEW NEEDED" in HTML
    assert '{ok: "✔", warn: "⚠", bad: "✖"}' in HTML               # an icon for every severity
    assert "aria-label" in HTML[HTML.index("function tile"):HTML.index("function renderCounters")]


def test_page_script_is_valid_javascript(tmp_path):
    import re, shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
    f = tmp_path / "page.js"
    f.write_text(js)
    assert subprocess.run([node, "--check", str(f)], capture_output=True).returncode == 0


# ------------------------------------------------------------------ the counters
def load_dash(monkeypatch):
    for k in ("SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "BASELINE_URL", "DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATUS_URLS", ",".join(f"http://127.0.0.1:1{i}/status" for i in range(4)))
    spec = importlib.util.spec_from_file_location("dash_tabs_mod", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def inv_row(i="inv1", target="B", active=True):
    return {"id": i, "target": target, "workload": "auth-service", "epoch": 0, "question": "q", "trigger": "t", "signals": ["NETWORK"],
            "initiator": "A", "started_at": time.time(), "deadline": time.time() + 5, "state": "ACTIVE" if active else "CLOSED",
            "outcome": None if active else "AMBIGUOUS", "reason": "", "time_left_s": 3.0, "own": {"view": None}, "views": {}, "silent": []}


def status(node, phases=None, counters=None, inv=None, watch=None, attention=None):
    phases = phases or {}
    targets = {}
    for n in "ABCD":
        ph = phases.get(n, "HEALTHY")
        targets[n] = {"phase": ph, "stage": "FULL" if ph == "HEALTHY" else "QUARANTINE", "workload": n, "epoch": 0, "last_decision": None,
                      "detections": [], "workload_trust": 100, "attempt": 1,
                      "attention": {"reason": "r"} if n == attention else None,
                      "display_state": "NEEDS_ATTENTION" if n == attention else ph}
    return {"node": node, "mode": "distributed", "time": time.time(), "data_source": "SIMULATOR", "targets": targets,
            "agent_trust": {n: 100 for n in "ABCD"}, "decisions": [], "rejections": [{"t": 1.0, "from": "x", "claimed_signer": "B", "reason": "r"}],
            "pending_votes": {}, "event_boot": "b", "event_seq": 0, "events": [], "counters": counters,
            "investigations": {"enabled": True, "active": [inv] if inv else [], "recent": [], "watch": watch or {}}}


def test_summary_counts_are_derived_from_what_agents_report(monkeypatch):
    d = load_dash(monkeypatch)
    ctr = {"incidents_opened": 3, "recoveries_succeeded": 2, "recovery_attempts_failed": 3, "recoveries_abandoned": 1}
    d.COLLECTOR.status = {
        "A": status("A", {"C": "VALIDATING"}, ctr, inv=inv_row(), attention="C"),
        "B": status("B", {"C": "VALIDATING"}, {**ctr, "recoveries_succeeded": 1}, inv=inv_row()),
        "C": status("C", {"C": "VALIDATING"}, None)}                      # D is silent
    m = d.aggregate()["summary"]
    assert (m["workloads_total"], m["workloads_healthy"], m["workloads_unhealthy"]) == (4, 3, 1) and m["unhealthy"] == ["C"]
    assert m["incidents_active"] == ["C"] and m["needs_attention"] == ["C"]
    assert m["investigations_active"] == 1                                # one investigation, seen by two agents: counted once
    assert m["recoveries_succeeded"] == 2 and m["recovery_attempts_failed"] == 3 and m["recoveries_abandoned"] == 1   # max across agents
    assert (m["agents_total"], m["agents_connected"], m["agents_unreachable"]) == (4, 3, ["D"])
    assert m["counters_reported_by"] == ["A", "B"]                        # C sent none: not guessed


def test_summary_when_everything_is_quiet_and_when_nothing_answers(monkeypatch):
    d = load_dash(monkeypatch)
    d.COLLECTOR.status = {n: status(n, counters={"incidents_opened": 0, "recoveries_succeeded": 0, "recovery_attempts_failed": 0,
                                                 "recoveries_abandoned": 0}) for n in "ABCD"}
    m = d.aggregate()["summary"]
    assert m["workloads_unhealthy"] == 0 and m["incidents_active"] == [] and m["agents_unreachable"] == []
    assert m["investigations_active"] == 0 and m["recoveries_succeeded"] == 0
    d.COLLECTOR.poll_once = lambda: None                                   # nobody reachable
    d.COLLECTOR.status = {}
    m = d.aggregate()["summary"]
    assert m["agents_connected"] == 0 and m["agents_unreachable"] == list("ABCD")
    assert m["workloads_unknown"] == list("ABCD") and m["workloads_unhealthy"] == 4    # no data is never reported as healthy


def test_review_needed_counts_as_needing_attention_in_the_summary(monkeypatch):
    d = load_dash(monkeypatch)
    w = {"B": {"workload": "auth-service", "review_needed": True, "reason": "r", "outcome": "AMBIGUOUS"}}
    d.COLLECTOR.status = {"A": status("A", watch=w)}
    assert d.aggregate()["summary"]["investigations_review"] == ["B"]


# ------------------------------------------------------------------ agent-side counters
def agent(port=53901):
    return ResilienceAgent(fast_config(port), "A", Signer.generate("A"), KeyRegistry({}), FakeTelemetry(FakeWorld()), FakeBackend())


def commit(action, epoch, stage=""):
    v = Vote(voter="A", target="C", action=action, epoch=epoch, stage=stage, score=1.0, timestamp=time.time(), evidence_ids=())
    return Commit(action_key=f"{action.value}:C:{epoch}:{stage}", votes=[v])


def test_agent_counters_follow_the_recovery_lifecycle():
    a = agent()
    assert a.status()["counters"] == {"incidents_opened": 0, "recoveries_succeeded": 0, "recovery_attempts_failed": 0, "recoveries_abandoned": 0}
    a._on_commit(commit(Action.CONTAIN, 1), time.time())
    assert a.counters["incidents_opened"] == 1
    st = a.states["C"]
    st.phase = VALIDATING
    a._on_commit(commit(Action.RETRY_RECOVERY, 1, "2"), time.time())
    assert a.counters["recovery_attempts_failed"] == 1 and st.phase == RECOVERING
    a._attention("C", time.time(), "gave up")
    a._attention("C", time.time(), "again")                                # once per incident, not per call
    assert a.counters["recoveries_abandoned"] == 1
    st.phase, st.stage, st.attention = REINTEGRATING, "FULL", None
    a._track_cluster(time.time())                                          # no isolation policy left in the (fake) cluster
    assert st.phase == HEALTHY and a.counters["recoveries_succeeded"] == 1
    a._track_cluster(time.time())
    assert a.counters["recoveries_succeeded"] == 1                         # counted once
    assert a.status()["counters"]["incidents_opened"] == 1


# ------------------------------------------------------------------ the real page in a real browser (skipped if unavailable)
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    exe = "/opt/pw-browsers/chromium"
    if not os.path.exists(exe):
        pytest.skip("chromium not available")
    return pw, exe


def test_tabs_and_counters_in_a_browser(monkeypatch):
    pw, exe = browser()
    d = load_dash(monkeypatch)
    ctr = {"incidents_opened": 3, "recoveries_succeeded": 2, "recovery_attempts_failed": 3, "recoveries_abandoned": 1}
    d.COLLECTOR.status = {"A": status("A", {"C": "VALIDATING"}, ctr, inv=inv_row(), attention="C"),
                          "B": status("B", {"C": "VALIDATING"}, ctr, inv=inv_row()), "C": status("C", {"C": "VALIDATING"}, ctr)}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(executable_path=exe)
            pg = b.new_page(viewport={"width": 1300, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
            pg.wait_for_selector("#counters .tile")
            visible = lambda t: pg.is_visible(f"#panel-{t}")           # noqa: E731
            assert visible("overview") and not any(visible(t) for t in TABS[1:])
            assert pg.locator("#nodes .card").count() == 4             # the 4 node cards
            text = pg.inner_text("#counters")
            for word in ("UNHEALTHY", "NEEDS HUMAN", "UNREACHABLE", "3 of 4", "GAVE UP"):
                assert word in text, (word, text)
            assert "D" in pg.inner_text("#counters")                   # the unreachable agent is named
            assert pg.inner_text("#badge-incidents") == "1" and pg.inner_text("#badge-recovery") == "1"
            # click through every tab
            for t in TABS[1:]:
                pg.click(f"#tab-{t}")
                assert visible(t) and not visible("overview")
                assert pg.get_attribute(f"#tab-{t}", "aria-selected") == "true" and pg.get_attribute("#tab-overview", "aria-selected") == "false"
                assert pg.evaluate("location.hash") == "#" + t
            pg.click("#tab-incidents")
            assert pg.is_visible("#dec") and pg.is_visible("#met")
            pg.click("#tab-audit")
            assert pg.is_visible("#rej") and pg.is_visible("#dlogtab")
            # keyboard: arrows move between tabs, Home goes to the first
            pg.focus("#tab-audit")
            pg.keyboard.press("ArrowRight")
            assert visible("overview")                                  # wraps around
            pg.keyboard.press("End")
            assert visible("audit")
            pg.keyboard.press("Home")
            assert visible("overview")
            # deep link
            pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/#recovery")
            pg.wait_for_selector("#panel-recovery:not([hidden])")
            assert pg.is_visible("#recsec")
            assert errs == [], errs
            b.close()
    finally:
        srv.shutdown()
