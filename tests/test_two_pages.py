"""Healthcare Phase B: Demo Mode and Live Monitoring are two channels that share nothing.

Every channel accepts only its own data_source (demo: SIMULATOR, live: LIVE_CLUSTER); only the demo channel can have simulator
controls. These tests use real HTTP servers standing in for agents (and for the simulator's control server)."""
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
HTML = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
TOKEN = "test-control-token"


# ------------------------------------------------------------------ stand-ins
class Fake:
    """A tiny HTTP server. `.data` is what GET /status (or any GET) returns; `.hits` counts every request."""

    def __init__(self, data=None, post_ok=True):
        self.data, self.hits, self.posts, self.post_ok = data, 0, [], post_ok
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                if outer.data is None:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps(outer.data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                outer.hits += 1
                n = int(self.headers.get("Content-Length") or 0)
                outer.posts.append((self.path, self.rfile.read(n)))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def agent_status(node, src, events=(), decisions=(), cluster=None, telemetry=None):
    tg = {n: {"phase": "HEALTHY", "stage": "FULL", "workload": f"wl-{n}", "epoch": 0, "last_decision": None, "detections": [],
              "workload_trust": 100, "attempt": 1, "attention": None, "display_state": "HEALTHY"} for n in "ABCD"}
    return {"node": node, "mode": "distributed", "time": time.time(), "data_source": src, "targets": tg,
            "agent_trust": {n: 100 for n in "ABCD"}, "decisions": list(decisions), "rejections": [], "pending_votes": {},
            "event_boot": "b", "event_seq": len(events), "events": list(events), "counters": None,
            "investigations": {"enabled": True, "active": [], "recent": [], "watch": {}},
            "cluster": cluster or {}, "telemetry": telemetry or {}}


def ev(seq, node, src, text="x"):
    return {"seq": seq, "ts": time.time(), "node": node, "category": "OBSERVE", "summary": text, "target": "C", "data_source": src}


def decision(src, epoch=1, target="C"):
    return {"t": time.time(), "action": "CONTAIN", "target": target, "epoch": epoch, "stage": "", "voters": ["A", "B", "C"],
            "note": "", "justification": {}, "data_source": src}


class Rig:
    """Four fake agents per channel plus a dashboard module built from the environment."""

    def __init__(self, monkeypatch, demo=True, live=True, control=False, extra_env=None):
        self.fakes = {"demo": [Fake() for _ in range(4)], "live": [Fake() for _ in range(4)]}
        self.ctl = Fake({"scenarios": [{"id": "app-compromise", "title": "t", "description": "d", "expect": "e", "est_s": 1}],
                         "status": "idle", "running": None, "last": None, "needs_reset": False})
        for k in ("STATUS_URLS", "DEMO_STATUS_URLS", "LIVE_STATUS_URLS", "SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "BASELINE_URL",
                  "DEMO_BASELINE_URL", "LIVE_BASELINE_URL", "DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE", "EVAL_DIR", "RESILIENCE_CONFIG"):
            monkeypatch.delenv(k, raising=False)
        if demo:
            monkeypatch.setenv("DEMO_STATUS_URLS", ",".join(f.url + "/status" for f in self.fakes["demo"]))
        if live:
            monkeypatch.setenv("LIVE_STATUS_URLS", ",".join(f.url + "/status" for f in self.fakes["live"]))
        if control:
            monkeypatch.setenv("SIM_CONTROL_URL", self.ctl.url)
            monkeypatch.setenv("SIM_CONTROL_TOKEN", TOKEN)
        for k, v in (extra_env or {}).items():
            monkeypatch.setenv(k, v)
        spec = importlib.util.spec_from_file_location("dash_two_pages", os.path.join(ROOT, "dashboard", "server.py"))
        self.d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.d)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.d.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def serve(self, channel, src, **kw):
        for i, f in enumerate(self.fakes[channel]):
            node = "ABCD"[i]
            f.data = agent_status(node, src, **{k: (v(node) if callable(v) else v) for k, v in kw.items()})

    def poll(self):
        for c in self.d.CHANNELS.values():
            c.collector.poll_once()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post(self, path, body=None):
        req = urllib.request.Request(self.url + path, method="POST", data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json", "X-CR-Control": "1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def close(self):
        self.srv.shutdown()
        for fs in self.fakes.values():
            for f in fs:
                f.stop()
        self.ctl.stop()


@pytest.fixture()
def rig(monkeypatch):
    made = []

    def make(**kw):
        r = Rig(monkeypatch, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        r.close()


# ------------------------------------------------------------------ configuration
def test_two_channels_share_nothing_and_only_demo_has_controls(rig):
    r = rig(control=True)
    d = r.d
    assert d.MODE == "strict" and set(d.CHANNELS) == {"demo", "live"}
    demo, live = d.CHANNELS["demo"], d.CHANNELS["live"]
    assert demo.expect == "SIMULATOR" and live.expect == "LIVE_CLUSTER"
    assert demo.collector is not live.collector and demo.urls != live.urls
    assert demo.collector.events is not live.collector.events and demo.collector.decisions is not live.collector.decisions
    # a live channel is built WITHOUT controls even though the environment configures them
    assert demo.sim_url and demo.sim_token
    assert live.sim_url == "" and live.sim_token == ""


def test_in_cluster_deployment_serves_the_live_page_only(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"nodes": {n: {"status_url": f"http://127.0.0.1:1/{n}"} for n in "ABCD"}}))
    for k in ("STATUS_URLS", "DEMO_STATUS_URLS", "LIVE_STATUS_URLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESILIENCE_CONFIG", str(cfg))
    spec = importlib.util.spec_from_file_location("dash_incluster", os.path.join(ROOT, "dashboard", "server.py"))
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    assert d.MODE == "strict" and list(d.CHANNELS) == ["live"] and d.CHANNELS["live"].expect == "LIVE_CLUSTER"
    v = d.channels_view()
    assert v["channels"]["live"]["configured"] and not v["channels"]["demo"]["configured"] and v["default"] == "live"
    assert "not available in this deployment" in v["channels"]["demo"]["reason"]
    assert d.resolve("demo")[0] is None


def test_strict_mode_needs_an_explicit_channel(rig):
    r = rig()
    r.serve("demo", "SIMULATOR")
    r.serve("live", "LIVE_CLUSTER")
    r.poll()
    for path in ("/api/events", "/api/export", "/api/metrics"):
        code, body = r.get(path)
        assert code == 400 and "channel" in body["error"], path
    code, body = r.get("/api/state")                      # the state endpoint answers "not configured" instead, with no data in it
    assert code == 200 and body["configured"] is False and "channel" in body["connection"]["reason"] and "nodes" not in body
    assert r.get("/api/state?channel=bogus")[1]["configured"] is False
    assert r.get("/api/channels")[1]["default"] == "demo"


def test_both_single_and_two_page_settings_is_an_error(monkeypatch):
    for k in ("DEMO_STATUS_URLS", "LIVE_STATUS_URLS", "STATUS_URLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATUS_URLS", "http://x:1/status")
    monkeypatch.setenv("LIVE_STATUS_URLS", "http://x:2/status")
    spec = importlib.util.spec_from_file_location("dash_both", os.path.join(ROOT, "dashboard", "server.py"))
    d = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit):
        spec.loader.exec_module(d)


# ------------------------------------------------------------------ the two pages show their own data
def test_each_page_has_its_own_events_decisions_and_incidents(rig):
    r = rig()
    r.serve("demo", "SIMULATOR", events=lambda n: [ev(1, n, "SIMULATOR", "demo-only")], decisions=[decision("SIMULATOR", epoch=1)])
    r.serve("live", "LIVE_CLUSTER", events=lambda n: [ev(1, n, "LIVE_CLUSTER", "live-only")], decisions=[decision("LIVE_CLUSTER", epoch=1)])
    r.poll()
    dc, dm = r.get("/api/state?channel=demo")[1], r.get("/api/events?channel=demo")[1]
    lc, lv = r.get("/api/state?channel=live")[1], r.get("/api/events?channel=live")[1]
    assert dc["data_source"] == "SIMULATOR" and lc["data_source"] == "LIVE_CLUSTER"
    assert dm["events"] and {e["data_source"] for e in dm["events"]} == {"SIMULATOR"}
    assert lv["events"] and {e["data_source"] for e in lv["events"]} == {"LIVE_CLUSTER"}
    assert all("demo-only" in e["summary"] for e in dm["events"]) and all("live-only" in e["summary"] for e in lv["events"])
    assert {i["source"] for i in dc["incidents"]} == {"SIMULATOR"} and {i["source"] for i in lc["incidents"]} == {"LIVE_CLUSTER"}
    # an incident that exists only on the demo page cannot be opened through the live page, and the reverse
    r.serve("demo", "SIMULATOR", decisions=[decision("SIMULATOR", epoch=7)])
    r.poll()
    assert r.get("/api/incident/SIMULATOR/C/7?channel=demo")[0] == 200
    assert r.get("/api/incident/SIMULATOR/C/7?channel=live")[0] == 404
    assert {i["epoch"] for i in r.get("/api/state?channel=live")[1]["incidents"]} == {1}


# ------------------------------------------------------------------ negative tests: simulated data can never appear on the live page
def test_simulated_agents_wired_to_the_live_channel_are_ignored_entirely(rig):
    r = rig()
    r.serve("demo", "SIMULATOR")
    r.serve("live", "SIMULATOR", events=lambda n: [ev(1, n, "SIMULATOR", "simulated")], decisions=[decision("SIMULATOR")])   # misconfigured
    r.poll()
    code, st = r.get("/api/state?channel=live")
    assert code == 200
    c = st["connection"]
    assert c["connected"] is False and c["state"] == "NOT_CONNECTED"
    assert c["rejected_sources"] == {n: "SIMULATOR" for n in "ABCD"}
    assert st["decisions"] == [] and st["incidents"] == [] and st["data_source"] == "UNKNOWN"
    assert all(not n["agent_up"] for n in st["nodes"].values())
    assert r.get("/api/events?channel=live")[1]["events"] == []
    live = r.d.CHANNELS["live"].collector
    assert live.status == {} and not live.decisions and not live.audit and not live.events
    assert r.get("/api/export?channel=live")[1]["events"] == []


def test_live_agents_wired_to_the_demo_channel_are_ignored_entirely(rig):
    r = rig()
    r.serve("demo", "LIVE_CLUSTER", events=lambda n: [ev(1, n, "LIVE_CLUSTER", "real")], decisions=[decision("LIVE_CLUSTER")])
    r.serve("live", "LIVE_CLUSTER")
    r.poll()
    st = r.get("/api/state?channel=demo")[1]
    assert st["connection"]["connected"] is False and st["connection"]["rejected_sources"] == {n: "LIVE_CLUSTER" for n in "ABCD"}
    assert st["incidents"] == [] and r.get("/api/events?channel=demo")[1]["events"] == []


@pytest.mark.parametrize("claim", ["UNKNOWN", None, "MIXED", "garbage"])
def test_unlabelled_or_unknown_data_is_never_accepted_by_either_page(rig, claim):
    r = rig()
    r.serve("demo", claim)
    r.serve("live", claim)
    r.poll()
    for name in ("demo", "live"):
        c = r.get(f"/api/state?channel={name}")[1]["connection"]
        assert c["connected"] is False and set(c["rejected_sources"]) == set("ABCD")


def test_a_simulated_record_inside_a_live_status_is_dropped_not_shown(rig):
    r = rig()
    r.serve("demo", "SIMULATOR")
    r.serve("live", "LIVE_CLUSTER",
            events=lambda n: [ev(1, n, "LIVE_CLUSTER", "real"), ev(2, n, "SIMULATOR", "smuggled")],
            decisions=[decision("LIVE_CLUSTER", epoch=1), decision("SIMULATOR", epoch=9)])
    r.poll()
    evs = r.get("/api/events?channel=live")[1]["events"]
    assert evs and all(e["data_source"] == "LIVE_CLUSTER" and "smuggled" not in e["summary"] for e in evs)
    st = r.get("/api/state?channel=live")[1]
    assert {i["epoch"] for i in st["incidents"]} == {1} and all(d["data_source"] == "LIVE_CLUSTER" for d in st["decisions"])
    assert st["connection"]["dropped_records"] >= 8                                      # 4 agents x (1 event + 1 decision)
    assert r.get("/api/incident/SIMULATOR/C/9?channel=live")[0] == 404


def test_live_page_never_falls_back_to_the_demo_data(rig):
    r = rig()
    r.serve("demo", "SIMULATOR", events=lambda n: [ev(1, n, "SIMULATOR", "demo")])
    for f in r.fakes["live"]:
        f.data = None                                            # the cluster is unreachable (503)
    r.poll()
    st = r.get("/api/state?channel=live")[1]
    assert st["connection"]["state"] == "NOT_CONNECTED" and st["connection"]["agents_up"] == 0
    assert st["data_source"] == "UNKNOWN" and st["incidents"] == [] and st["decisions"] == []
    assert r.get("/api/events?channel=live")[1]["events"] == []
    assert r.get("/api/state?channel=demo")[1]["connection"]["connected"] is True           # and the demo page is unaffected


# ------------------------------------------------------------------ negative tests: demo controls cannot affect live resources
def test_simulator_controls_are_refused_for_the_live_page_and_nothing_is_forwarded(rig):
    r = rig(control=True)
    r.serve("demo", "SIMULATOR")
    r.serve("live", "LIVE_CLUSTER")
    r.poll()
    base = r.ctl.hits
    code, body = r.post("/api/sim/start?channel=live", {"scenario": "app-compromise"})
    assert code == 403 and "no scenario controls" in body["error"]
    assert r.post("/api/sim/reset?channel=live")[0] == 403
    assert r.get("/api/sim/state?channel=live")[1]["enabled"] is False
    assert r.ctl.hits == base and r.ctl.posts == []                                          # never reached the control server
    # strict mode never guesses a channel for a control request
    assert r.post("/api/sim/start", {"scenario": "app-compromise"})[0] == 400
    assert r.ctl.posts == []
    # a channel that does not exist is refused too
    assert r.post("/api/sim/start?channel=bogus", {"scenario": "app-compromise"})[0] in (400, 403)
    assert r.ctl.posts == []


def test_simulator_controls_work_only_on_the_demo_page(rig):
    r = rig(control=True)
    r.serve("demo", "SIMULATOR")
    r.serve("live", "LIVE_CLUSTER")
    r.poll()
    assert r.get("/api/sim/state?channel=demo")[1]["enabled"] is True
    code, _ = r.post("/api/sim/start?channel=demo", {"scenario": "app-compromise"})
    assert code == 200 and [p for p, _b in r.ctl.posts] == ["/start"]
    code, body = r.post("/api/sim/start?channel=demo", {"scenario": "not-in-the-catalogue"})
    assert code == 400 and len(r.ctl.posts) == 1


def test_demo_controls_are_refused_when_the_demo_agents_do_not_report_simulator(rig):
    r = rig(control=True)
    r.serve("demo", "LIVE_CLUSTER")                      # misconfigured: the 'demo' URLs actually point at a live cluster
    r.serve("live", "LIVE_CLUSTER")
    r.poll()
    code, body = r.post("/api/sim/start?channel=demo", {"scenario": "app-compromise"})
    assert code == 403 and r.ctl.posts == []
    assert r.get("/api/sim/state?channel=demo")[1]["enabled"] is False


def test_live_channel_with_a_control_url_in_the_environment_still_has_none(rig):
    r = rig(control=True, demo=False)                   # only a live page, but SIM_CONTROL_* are set
    r.serve("live", "LIVE_CLUSTER")
    r.poll()
    assert r.d.CHANNELS["live"].sim_url == ""
    assert r.post("/api/sim/start?channel=live", {"scenario": "app-compromise"})[0] == 403
    assert r.ctl.posts == []
    assert r.get("/api/sim/state?channel=demo")[1]["enabled"] is False            # no demo page exists here


# ------------------------------------------------------------------ what the live page reports about its connection
def test_live_connection_block_reports_cluster_identity_freshness_and_availability(rig):
    r = rig()
    r.serve("live", "LIVE_CLUSTER", cluster={"name": "cr-platform", "kubernetes_version": "v1.30.0", "platform": "linux/amd64"},
            telemetry={"age_s": 2.0, "oldest_age_s": 2.5, "reachable": 4, "total": 4})
    r.serve("demo", "SIMULATOR")
    r.poll()
    c = r.get("/api/state?channel=live")[1]["connection"]
    assert c["state"] == "CONNECTED" and c["agents_up"] == 4 and c["agents_total"] == 4 and c["agents_down"] == []
    assert c["cluster"] == {"name": "cr-platform", "kubernetes_version": "v1.30.0", "platform": "linux/amd64"}
    assert 2.0 <= c["telemetry_age_s"] < 6.0
    # one agent down: DEGRADED, and it is named
    r.fakes["live"][2].data = None
    r.poll()
    c = r.get("/api/state?channel=live")[1]["connection"]
    assert c["state"] == "DEGRADED" and c["agents_up"] == 3 and c["agents_down"] == ["C"] and c["connected"] is True


def test_agents_that_disagree_about_the_cluster_say_so(rig):
    r = rig()
    r.serve("live", "LIVE_CLUSTER", cluster=lambda n: {"name": "one" if n == "A" else "two"})
    r.serve("demo", "SIMULATOR")
    r.poll()
    assert r.get("/api/state?channel=live")[1]["connection"]["cluster"]["name"] == "agents disagree"


def test_missing_cluster_identity_is_reported_as_missing_not_invented(rig):
    r = rig()
    r.serve("live", "LIVE_CLUSTER")
    r.serve("demo", "SIMULATOR")
    r.poll()
    c = r.get("/api/state?channel=live")[1]["connection"]
    assert c["cluster"]["name"] == "" and c["telemetry_age_s"] is None


# ------------------------------------------------------------------ single-source compatibility mode
@pytest.mark.parametrize("src,page,other", [("SIMULATOR", "demo", "live"), ("LIVE_CLUSTER", "live", "demo")])
def test_legacy_single_source_answers_only_as_the_page_matching_its_data(monkeypatch, src, page, other):
    f = [Fake(agent_status(n, src)) for n in "ABCD"]
    for k in ("DEMO_STATUS_URLS", "LIVE_STATUS_URLS", "SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "BASELINE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATUS_URLS", ",".join(x.url + "/status" for x in f))
    spec = importlib.util.spec_from_file_location("dash_legacy", os.path.join(ROOT, "dashboard", "server.py"))
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    try:
        assert d.MODE == "legacy"
        d.DEFAULT.collector.poll_once()
        assert d.resolve(page)[0] is d.DEFAULT and d.resolve(other)[0] is None and d.resolve(None)[0] is d.DEFAULT
        v = d.channels_view()
        assert v["channels"][page]["configured"] and not v["channels"][other]["configured"] and v["default"] == (
            "demo" if page == "demo" else "live")
    finally:
        for x in f:
            x.stop()


# ------------------------------------------------------------------ the page
def test_page_has_the_toggle_the_banners_and_the_renamed_refresh_indicator():
    for text in ("Demo Mode", "Live Monitoring", 'id="mode-demo"', 'id="mode-live"',
                 "DEMO MODE — SIMULATED DATA. No real healthcare workload is being attacked.",
                 "LIVE MONITORING — CONNECTED TEST CLUSTER", "NOT CONNECTED — Live monitoring is unavailable.",
                 "Auto-refresh on", "Paused"):
        assert text in HTML, text
    assert "● LIVE" not in HTML and "PAUSED\"" not in HTML.replace("PAUSED - the screen", "")
    # every data request carries the chosen channel; only mode-independent files do not
    assert 'a[0] = U(a[0])' in HTML and "channels|evaluation" in HTML
    assert 'if (CH !== "demo")' in HTML                       # Live Monitoring never even asks for scenario controls


def _browser():
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists("/opt/pw-browsers/chromium"):
        pytest.skip("chromium not available")
    return pw


def test_the_two_pages_in_a_browser(rig):
    pw = _browser()
    r = rig(control=True)
    r.serve("demo", "SIMULATOR", events=lambda n: [ev(1, n, "SIMULATOR", "simulated scan")])
    r.serve("live", "LIVE_CLUSTER", events=lambda n: [ev(1, n, "LIVE_CLUSTER", "real scan")],
            cluster={"name": "cr-platform", "kubernetes_version": "v1.30.0"}, telemetry={"age_s": 1.0})
    r.poll()
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        pg = b.new_page(viewport={"width": 1300, "height": 900})
        errs, reqs = [], []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("request", lambda q: reqs.append(q.url))
        pg.goto(r.url + "/?mode=demo")
        pg.wait_for_selector("#counters")
        assert "DEMO MODE — SIMULATED DATA. No real healthcare workload is being attacked." in pg.inner_text("#srcbanner")
        assert pg.locator("#simctl").is_visible()                         # scenario picker is on the demo page
        pg.wait_for_function("document.querySelectorAll('#tl .ev, #timeline .ev, .ev').length > 0")
        assert "real scan" not in pg.inner_text("body")
        assert pg.inner_text("#live").strip() == "● Auto-refresh on"
        pg.click("#pause")
        assert pg.inner_text("#live").strip() == "❚❚ Paused"
        pg.click("#pause")
        # toggle to the live page
        pg.click("#mode-live")
        pg.wait_for_function("document.getElementById('srcbanner').textContent.includes('LIVE MONITORING')")
        banner = pg.inner_text("#srcbanner")
        assert "LIVE MONITORING — CONNECTED TEST CLUSTER" in banner and "cr-platform" in banner and "4 of 4" in banner and "telemetry age" in banner
        assert "channel=live" in " ".join(u for u in reqs if "/api/state" in u)
        assert not pg.locator("#simctl").is_visible()                       # no scenario buttons on the live page
        pg.wait_for_function("document.querySelectorAll('.ev').length > 0")
        body = pg.inner_text("body")
        assert "real scan" in body and "simulated scan" not in body
        n0 = len(reqs)
        pg.wait_for_timeout(3000)                                           # a few refresh cycles of the live page
        assert len(reqs) > n0
        assert not any("/api/sim/" in u for u in reqs[n0:])                  # the live page never even asks for scenario controls
        assert all("channel=live" in u for u in reqs[n0:] if "/api/" in u and "/api/channels" not in u and "/api/eval" not in u)
        # live cluster goes away: the page says so and shows nothing from the demo
        for f in r.fakes["live"]:
            f.data = None
        r.poll()
        pg.wait_for_function("document.getElementById('srcbanner').textContent.includes('NOT CONNECTED')", timeout=15000)
        assert "NOT CONNECTED — Live monitoring is unavailable." in pg.inner_text("#srcbanner")
        assert not pg.locator(".tabs").is_visible() and "simulated scan" not in pg.inner_text("body")
        assert errs == [], errs
        b.close()


# ------------------------------------------------------------------ the agent side
def test_agent_status_reports_cluster_and_telemetry_age():
    sys.path.insert(0, os.path.join(ROOT, "sim"))
    from world import LocalCluster, fast_config
    c = LocalCluster(base_port=50820, cfg=fast_config(50820)).start()
    try:
        time.sleep(2)
        st = c.agents["A"].status()
        assert st["cluster"]["kubernetes_version"] == "simulated" and st["cluster"]["name"] == ""
        assert st["telemetry"]["total"] == 6 and st["telemetry"]["reachable"] == 6      # every agent reads all six workloads
        assert st["telemetry"]["age_s"] is not None and 0 <= st["telemetry"]["age_s"] < 5
    finally:
        c.stop()


def test_cluster_name_is_read_from_the_config(tmp_path):
    from resilience.config import load_cluster_config
    raw = json.load(open(os.path.join(ROOT, "k8s", "resilience", "10-config.yaml")).__class__ and open(os.devnull)) if False else None
    text = open(os.path.join(ROOT, "k8s", "resilience", "10-config.yaml"), encoding="utf-8").read()
    body = text.split("config.json: |\n", 1)[1]
    j = json.loads("\n".join(l[4:] for l in body.splitlines() if l.startswith("    ")))
    assert j["cluster_name"] == "cr-platform"
    p = tmp_path / "c.json"
    p.write_text(json.dumps(j))
    assert load_cluster_config(str(p)).cluster_name == "cr-platform"
    del j["cluster_name"]
    p.write_text(json.dumps(j))
    assert load_cluster_config(str(p)).cluster_name == ""


def test_k8s_backend_cluster_info_never_blocks_and_is_cached():
    from resilience.response import K8sBackend
    calls = []

    class V:
        git_version, platform = "v1.30.1", "linux/arm64"

    class Api:
        def get_code(self, **kw):
            calls.append(1)
            time.sleep(0.3)
            return V()

    class Client:
        VersionApi = staticmethod(lambda: Api())
    b = K8sBackend.__new__(K8sBackend)
    b.client, b.t = Client, {}
    b._ci, b._ci_at, b._ci_busy, b._ci_lock = {}, 0.0, False, threading.Lock()
    t0 = time.time()
    assert b.cluster_info() == {}                       # first call returns at once; the lookup runs in the background
    assert time.time() - t0 < 0.2
    time.sleep(0.6)
    assert b.cluster_info() == {"kubernetes_version": "v1.30.1", "platform": "linux/arm64"}
    b.cluster_info()
    assert len(calls) == 1                              # cached
    assert K8sBackend.SOURCE == "LIVE_CLUSTER"
