"""Phase D2 readability: Simple (default) / Detailed view, plain-English readings and alert story, one top banner, key-result cards."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_evaluation_tab import ROOT, load_dash, serve   # noqa: E402,F401

HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()


def test_static_structure():
    assert "font:16px/1.5" in HTML and "max-width:1280px" in HTML                         # larger text, bounded line length
    assert HTML.count('id="srcbanner"') == 1 and HTML.count('id="topbar"') == 1           # one merged banner block
    assert 'id="view-simple"' in HTML and 'id="view-detailed"' in HTML
    assert 'VIEW = "simple"' in HTML                                                      # the default
    assert HTML.count("alertStory(i, groups)") == 2 and "keyCards(ten)" in HTML


def open_page(monkeypatch, serve, init=None, path="/"):
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    d = load_dash(monkeypatch)
    tg = {n: {"phase": "HEALTHY", "stage": "FULL", "workload": n, "epoch": 0, "last_decision": None, "detections": [], "workload_trust": 100,
              "attempt": 1, "attention": None, "display_state": "HEALTHY"} for n in "ABCD"}
    d.COLLECTOR.status = {n: {"node": n, "mode": "distributed", "time": 1.0, "data_source": "SIMULATOR", "targets": tg,
                              "agent_trust": {x: 100 for x in "ABCD"}, "decisions": [], "rejections": [], "pending_votes": {},
                              "event_boot": "b", "event_seq": 0, "events": [], "counters": None,
                              "investigations": {"enabled": True, "active": [], "recent": [], "watch": {}}} for n in "ABCD"}
    url = serve(d)
    p = pw.sync_playwright().start()
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    pg = b.new_page(viewport={"width": 1500, "height": 900})
    if init:
        pg.add_init_script(init)
    pg.goto(url + path)
    pg.wait_for_selector("#counters")
    return p, b, pg


def test_simple_is_the_default_and_hides_the_technical_parts(monkeypatch, serve):
    p, b, pg = open_page(monkeypatch, serve)
    try:
        assert pg.evaluate("document.body.dataset.view") == "simple"
        assert pg.is_visible("#tab-overview") and pg.is_visible("#tab-incidents") and pg.is_visible("#tab-evaluation")
        assert not pg.is_visible("#tab-audit") and not pg.is_visible("#tab-investigations") and not pg.is_visible("#meaning")
        assert pg.evaluate("parseFloat(getComputedStyle(document.body).fontSize)") == 16
        assert pg.evaluate("document.querySelector('#topbar').children.length") == 2        # banner + mode bar, one block
        pg.click("#view-detailed")
        assert pg.evaluate("document.body.dataset.view") == "detailed" and pg.is_visible("#tab-audit") and pg.is_visible("#meaning")
        pg.reload()
        pg.wait_for_selector("#counters")
        assert pg.evaluate("document.body.dataset.view") == "detailed"                       # remembered in this browser
        pg.click("#view-simple")
        assert not pg.is_visible("#tab-audit")
    finally:
        b.close()
        p.stop()


def test_view_query_overrides_the_remembered_choice(monkeypatch, serve):
    p, b, pg = open_page(monkeypatch, serve, init="try { localStorage.setItem('cr-view', 'simple') } catch (e) {}", path="/?view=detailed")
    try:
        assert pg.evaluate("document.body.dataset.view") == "detailed"
    finally:
        b.close()
        p.stop()


def test_readings_are_translated_into_sentences(monkeypatch, serve):
    p, b, pg = open_page(monkeypatch, serve)
    try:
        r = lambda sig, s: pg.evaluate("([a, b]) => readingText(a, b)", [sig, s])            # noqa: E731
        assert r("NETWORK", "25 outbound conns, tx 900 KiB/s") == "It had 25 open connections to other machines and was sending about 900 KiB of data every second."
        assert "failed login attempts" in r("AUTH", "40 failed auths from 10.0.0.9 in 10s") and "10.0.0.9" in r("AUTH", "40 failed auths from 10.0.0.9 in 10s")
        assert r("PROCESS", "unexpected processes: nc").startswith("A program that should not be running")
        assert "Files that should never change" in r("FILE_INTEGRITY", "1 modified file(s): app.py")
        assert r("NETWORK", "something new") == "something new"                                # unknown wording is shown as it is
        story = pg.evaluate("""() => { const i = {target: 'A', status: 'RESOLVED', window: {contained_at: 1}, trigger: {seen_by: ['A','B','C']}};
            return alertStory(i, [['A', [{sig: 'AUTH', count: 3, latest: {summary: '40 failed auths from 10.0.0.9 in 10s'}}]]]) }""")
        assert "What happened:" in story and "3 of the 4 security agents agreed" in story and "40 failed login attempts" in story and "back to normal" in story
    finally:
        b.close()
        p.stop()


def test_performance_results_show_key_cards_not_the_twelve_lines(monkeypatch, serve):
    p, b, pg = open_page(monkeypatch, serve)
    try:
        pg.click("#tab-evaluation")
        pg.wait_for_selector("#evalbox .kcard")
        cards = pg.locator("#evalbox .kcard")
        assert cards.count() == 5
        vals = [cards.nth(i).locator(".kv").inner_text() for i in range(5)]
        assert "see below" not in vals and vals[4] == "SIMULATOR", vals                        # every number was found in the results text
        assert pg.locator("#evalbox details[data-k='ev-twelve']").count() == 1 and pg.locator("#evalbox details[data-k='ev-twelve'][open]").count() == 0
    finally:
        b.close()
        p.stop()
