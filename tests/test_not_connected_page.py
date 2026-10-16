"""The Live (and Demo) page, when not connected, shows ONLY the clean NOT CONNECTED message: no 'dashboard backend unreachable: TypeError ...',
no page errors, no failed requests. Regression for: Live channel not configured -> /api/events answered 404 {"error"} -> `r.events.length` threw."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMIUM = "/opt/pw-browsers/chromium"
pytestmark = pytest.mark.skipif(not os.path.exists(CHROMIUM), reason="chromium not available")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Labelled(BaseHTTPRequestHandler):
    """An 'agent' whose status carries the wrong data source (SIMULATOR offered to the Live page)."""
    def do_GET(self):
        body = json.dumps({"node": "A", "mode": "distributed", "data_source": "SIMULATOR", "targets": {}, "agent_trust": {}, "time": time.time()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def dash():
    procs = []

    def make(**env):
        port = free_port()
        e = {k: v for k, v in os.environ.items() if not k.startswith(("LIVE_", "DEMO_", "STATUS_URLS", "SIM_CONTROL"))}
        e.update(env, PORT=str(port), BIND="127.0.0.1")
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "server.py")], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/channels", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        return f"http://127.0.0.1:{port}"
    yield make
    for p in procs:
        p.terminate()


def probe(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        try:
            pg = b.new_page()
            errs, bad = [], []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("response", lambda r: bad.append((r.status, r.url)) if r.status >= 400 else None)
            pg.goto(url)
            pg.wait_for_timeout(4500)                                    # several refresh cycles (1 s) and one metrics cycle (5 s) start
            return {"sub": pg.inner_text("#sub"), "notconn": pg.inner_text("#notconn"), "errs": errs, "bad": bad,
                    "tabs_hidden": not pg.locator(".tabs").is_visible()}
        finally:
            b.close()


def assert_clean(r, why):
    assert "unreachable" not in r["sub"] and "TypeError" not in r["sub"], r
    assert "NOT CONNECTED" in r["notconn"] and why in r["notconn"], r
    assert r["errs"] == [] and r["tabs_hidden"], r
    assert [x for x in r["bad"] if "/api/events" in x[1] or "/api/metrics" in x[1]] == [], r      # a not-connected page asks for nothing else


def test_live_not_configured(dash):
    url = dash(DEMO_STATUS_URLS="http://127.0.0.1:1/status")                     # only Demo exists: Live is not configured here
    assert_clean(probe(url + "/?mode=live"), "Live Monitoring is not configured")


def test_live_configured_but_no_agent_reachable(dash):
    url = dash(LIVE_STATUS_URLS="http://127.0.0.1:1/status,http://127.0.0.1:2/status")
    assert_clean(probe(url + "/?mode=live"), "No agent of the cluster is reachable")


def test_live_agents_answer_with_simulated_data_which_is_refused(dash):
    srv = HTTPServer(("127.0.0.1", 0), Labelled)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = dash(LIVE_STATUS_URLS=f"http://127.0.0.1:{srv.server_address[1]}/status")
        assert_clean(probe(url + "/?mode=live"), "labelled SIMULATOR")
    finally:
        srv.shutdown()


def test_demo_not_running_is_also_clean(dash):
    url = dash(LIVE_STATUS_URLS="http://127.0.0.1:1/status")                     # Demo not configured here
    r = probe(url + "/?mode=demo")
    assert "unreachable" not in r["sub"] and "TypeError" not in r["sub"] and r["errs"] == [], r
    assert "DEMO MODE NOT RUNNING" in r["notconn"] or "NOT CONNECTED" in r["notconn"], r
