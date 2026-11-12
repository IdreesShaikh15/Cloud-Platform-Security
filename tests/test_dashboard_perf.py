"""Dashboard performance and Demo/Live separation of effort (docs/DEMO-TIMING.md):
 * the page never piles up refresh cycles, draws only the newest events, skips redrawing unchanged markup and abandons stuck requests;
 * the backend polls a channel nobody is looking at only every few seconds, and wakes it the moment someone asks;
 * the backend records how long each agent's /status took (scripts/demo-health.sh shows it).
The browser measurement (scripts/bench-dashboard.js) runs only when node + playwright + chromium are available."""
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()


@pytest.fixture()
def srv(monkeypatch):
    monkeypatch.setenv("STATUS_URLS", "http://x:1/status,http://x:2/status,http://x:3/status,http://x:4/status")
    spec = importlib.util.spec_from_file_location("dash_server_perf", os.path.join(ROOT, "dashboard", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ the page (static checks; the browser run below measures it)
def test_refresh_cycles_cannot_pile_up_and_a_hidden_tab_fetches_nothing():
    assert "async function tickRefresh()" in HTML and "if (refreshing) { refreshSkipped++; return; }" in HTML
    assert "if (document.hidden) return;" in HTML
    assert "setInterval(tickRefresh, 1000)" in HTML and "setInterval(refresh, 1000)" not in HTML
    assert "setInterval(tickMetrics, 5000)" in HTML and "setInterval(refreshMetrics, 5000)" not in HTML


def test_stuck_get_requests_are_abandoned():
    assert "AbortSignal.timeout(GET_TIMEOUT_MS)" in HTML and "const GET_TIMEOUT_MS = 8000" in HTML


def test_the_activity_log_draws_only_the_newest_rows_and_loads_more_on_request():
    assert "const TL_PAGE = 200;" in HTML and "shown = f.slice(-tlLimit)" in HTML and "f.slice(-400)" not in HTML
    assert 'id="tlmore"' in HTML and "tlLimit += TL_PAGE" in HTML
    assert "if (!force && key === tlKey) return;" in HTML                       # nothing recomputed when nothing changed


def test_identical_markup_is_not_written_twice():
    assert "if (this.__html === v && this.firstChild !== null) return;" in HTML


def test_the_independent_refresh_steps_run_side_by_side():
    assert "await Promise.all([pullEvents, refreshSim, refreshSeries, refreshIncident" in HTML


# ------------------------------------------------------------------ the backend
def test_a_channel_nobody_watches_is_polled_slowly_and_woken_at_once(srv, monkeypatch):
    monkeypatch.setattr(srv, "POLL_S", 0.05)
    monkeypatch.setattr(srv, "IDLE_AFTER_S", 0.3)
    monkeypatch.setattr(srv, "IDLE_POLL_S", 0.6)
    chn = next(iter(srv.CHANNELS.values()))
    polls = []
    monkeypatch.setattr(chn.collector, "poll_once", lambda: polls.append(time.time()))
    threading.Thread(target=chn.collector.run, daemon=True).start()
    time.sleep(0.25)
    busy = len(polls)
    assert busy >= 3, "while someone is looking it polls at POLL_S"
    chn.touch()                                       # (resets the idle clock; keeps polling fast)
    time.sleep(0.9)                                   # nobody asks for > IDLE_AFTER_S: the interval grows to IDLE_POLL_S
    idle_polls = len([p for p in polls if p > time.time() - 0.5])
    assert idle_polls <= 2, polls[-6:]
    assert chn.collector.idle() is True
    before = len(polls)
    chn.touch()                                       # a request arrives: the sleeping poller wakes immediately
    time.sleep(0.2)
    assert len(polls) > before and chn.collector.idle() is False


def test_collector_records_how_long_each_agent_took(srv, monkeypatch):
    chn = next(iter(srv.CHANNELS.values()))
    monkeypatch.setattr(srv, "get", lambda url: (time.sleep(0.05), {"data_source": "SIMULATOR", "event_boot": "b", "event_seq": 0, "events": []})[1])
    chn.collector._fetch("A", "http://x:1/status")
    chn.collector._fetch("B", "http://x:2/status")
    monkeypatch.setattr(srv, "get", lambda url: None)
    chn.collector._fetch("C", "http://x:3/status")
    h = chn.collector.health()
    assert h["poll_ms"]["A"] >= 40 and h["poll_ok"] == {"A": True, "B": True, "C": False}
    assert set(h) == {"poll_ms", "poll_ok", "last_poll_age_s", "idle", "poll_every_s"}


def test_state_answer_carries_the_collector_health(srv):
    chn = next(iter(srv.CHANNELS.values()))
    chn.collector.last_poll = time.time()
    srv._ctx.channel = chn
    try:
        assert "collector" in srv.aggregate() and "poll_ms" in srv.aggregate()["collector"]
    finally:
        srv._ctx.channel = None


# ------------------------------------------------------------------ the browser measurement
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_measurement_on_a_slow_link_stays_bounded():
    npm = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip() if shutil.which("npm") else ""
    env = dict(os.environ, NODE_PATH=npm)
    probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, env=env)
    if probe.returncode != 0:
        pytest.skip("node playwright not available")
    r = subprocess.run(["node", os.path.join(ROOT, "scripts", "bench-dashboard.js"), "--seconds", "12", "--latency", "1500", "--json",
                        "--port", "18193", "--control-port", "50473", "--base-port", "50653"],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=170)
    assert r.returncode == 0, r.stderr[-400:]
    m = json.loads(r.stdout.strip().splitlines()[-1])
    assert m["timeline_rows_in_dom"] <= 200
    assert m["max_requests_in_flight"] <= 6, m                  # before the fix: 7 and growing with the delay
    assert m["state_fetches"] <= 6, m                            # one cycle needs two 1.5 s round trips; a new one every second used to be started regardless
