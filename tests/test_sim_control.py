"""Part 1: the simulator demo controls can NEVER reach a live cluster. Enforced and tested at the BACKEND boundary:
the dashboard's HTTP handler, and the simulator control server, not the buttons in the page."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import demo_server as ds
import scenarios as sc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "t0ken-for-the-test-0123456789abcdef"


# ------------------------------------------------------------------ a stub control server that counts what reaches it
class Stub:
    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else None
                outer.calls.append((method, self.path, body, self.headers.get("X-CR-Sim-Token")))
                data = json.dumps({"scenarios": [{"id": "app-compromise", "title": "t", "description": "d", "expect": "e",
                                                  "est_s": 1}], "status": "idle", "running": None, "needs_reset": False,
                                   "last": None} if self.path == "/state" else {"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def dash_status(src, nodes="ABCD"):
    return {n: {"data_source": src, "targets": {}, "node": n} for n in nodes}


@pytest.fixture()
def stub():
    s = Stub()
    yield s
    s.srv.shutdown()


@pytest.fixture()
def dash(monkeypatch):
    servers = []

    def make(control_url=None, token=TOKEN, statuses=None, dash_token=None):
        for k in ("SIM_CONTROL_URL", "SIM_CONTROL_TOKEN", "DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE", "BASELINE_URL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("STATUS_URLS", ",".join(f"http://127.0.0.1:1{i}/status" for i in range(4)))
        if control_url:
            monkeypatch.setenv("SIM_CONTROL_URL", control_url)
        if control_url and token:
            monkeypatch.setenv("SIM_CONTROL_TOKEN", token)
        if dash_token:
            monkeypatch.setenv("DASHBOARD_TOKEN", dash_token)
        spec = importlib.util.spec_from_file_location("dash_sim_mod", os.path.join(ROOT, "dashboard", "server.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.COLLECTOR.status = statuses if statuses is not None else {}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return mod, f"http://127.0.0.1:{srv.server_address[1]}"
    yield make
    for s in servers:
        s.shutdown()


def post(url, path, body=None, headers=None, raw=None):
    h = {"Content-Type": "application/json", "X-CR-Control": "1", **(headers or {})}
    h = {k: v for k, v in h.items() if v is not None}
    req = urllib.request.Request(url + path, data=raw if raw is not None else json.dumps(body or {}).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def get(url, path):
    try:
        with urllib.request.urlopen(url + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")}


# ------------------------------------------------------------------ the dashboard backend gate
@pytest.mark.parametrize("path,body", [("/api/sim/start", {"scenario": "app-compromise"}), ("/api/sim/reset", {})])
@pytest.mark.parametrize("label,statuses", [
    ("live", dash_status("LIVE_CLUSTER")),
    ("unknown", dash_status(None)),
    ("mixed", {**dash_status("SIMULATOR", "ABC"), "D": {"data_source": "LIVE_CLUSTER", "targets": {}}}),
    ("one live agent among simulated", {**dash_status("SIMULATOR", "AB"), "C": {"data_source": "LIVE_CLUSTER", "targets": {}}}),
    ("no agent reachable", {}),
])
def test_controls_are_refused_and_never_forwarded_unless_every_agent_says_simulator(dash, stub, path, body, label, statuses):
    mod, url = dash(control_url=stub.url, statuses=statuses)
    code, resp = post(url, path, body)
    assert code == 403 and "refused" in resp["error"], label
    assert stub.calls == [], f"{label}: a refused request must not reach the simulator control server"


@pytest.mark.parametrize("path,body", [("/api/sim/start", {"scenario": "app-compromise"}), ("/api/sim/reset", {})])
def test_live_dashboard_without_control_configuration_refuses_everything(dash, stub, path, body):
    mod, url = dash(control_url=None, statuses=dash_status("SIMULATOR"))     # even if the data LOOKS simulated
    code, resp = post(url, path, body)
    assert code == 403 and "not configured" in resp["error"]
    assert get(url, "/api/sim/state")[1]["enabled"] is False
    assert stub.calls == []


def test_the_live_manifest_cannot_enable_controls():
    for f in ("30-dashboard.yaml", "20-agents.yaml", "40-webhook.yaml"):
        text = open(os.path.join(ROOT, "k8s", "resilience", f)).read()
        assert "SIM_CONTROL" not in text
    assert "SIM_CONTROL" not in open(os.path.join(ROOT, "dashboard", "Dockerfile")).read()


def test_non_loopback_control_address_is_refused(dash, stub):
    mod, url = dash(control_url="http://10.0.0.5:50270", statuses=dash_status("SIMULATOR"))
    assert post(url, "/api/sim/reset")[0] == 403
    assert mod.sim_gate()[1].startswith("refused: the simulator control address is not a loopback")


def test_missing_token_configuration_refuses(dash, stub):
    mod, url = dash(control_url=stub.url, token="", statuses=dash_status("SIMULATOR"))
    assert post(url, "/api/sim/reset")[0] == 403 and stub.calls == []


def test_controls_work_when_every_reachable_agent_says_simulator(dash, stub):
    mod, url = dash(control_url=stub.url, statuses=dash_status("SIMULATOR", "ABC"))     # a crashed agent D is fine
    st = get(url, "/api/sim/state")[1]
    assert st["enabled"] is True and st["scenarios"][0]["id"] == "app-compromise"
    assert post(url, "/api/sim/start", {"scenario": "app-compromise"})[0] == 200
    assert post(url, "/api/sim/reset")[0] == 200
    assert [(c[0], c[1]) for c in stub.posts()] == [("POST", "/start"), ("POST", "/reset")]
    assert all(c[3] == TOKEN for c in stub.calls)                    # the per-run token is attached by the dashboard
    assert stub.posts()[0][2] == {"scenario": "app-compromise"}      # ONLY the scenario id is forwarded


def test_only_catalogue_scenarios_can_be_named_and_nothing_else_is_forwarded(dash, stub):
    mod, url = dash(control_url=stub.url, statuses=dash_status("SIMULATOR"))
    assert post(url, "/api/sim/start", {"scenario": "rm -rf /"})[0] == 400
    assert post(url, "/api/sim/start", {"scenario": 5})[0] == 400
    assert post(url, "/api/sim/start", {})[0] == 400
    assert post(url, "/api/sim/start", {"scenario": "app-compromise", "namespace": "healthcare", "kubectl": "x"})[0] == 200
    assert stub.posts()[-1][2] == {"scenario": "app-compromise"}     # extra fields are dropped, never forwarded
    assert len(stub.posts()) == 1


def test_request_hardening(dash, stub):
    mod, url = dash(control_url=stub.url, statuses=dash_status("SIMULATOR"))
    assert post(url, "/api/sim/reset", headers={"X-CR-Control": None})[0] == 400                    # no custom header
    assert post(url, "/api/sim/reset", headers={"Content-Type": "text/plain"})[0] == 400
    assert post(url, "/api/sim/reset", raw=b"not json")[0] == 400
    assert post(url, "/api/sim/reset", headers={"Origin": "http://evil.example"})[0] == 403         # cross-origin
    # a GET can never start or reset anything
    assert get(url, "/api/sim/start")[0] == 404 and get(url, "/api/sim/reset")[0] == 404
    assert stub.posts() == []


def test_controls_require_the_dashboard_login_when_a_token_is_set(dash, stub):
    mod, url = dash(control_url=stub.url, statuses=dash_status("SIMULATOR"), dash_token="d" * 24)
    assert post(url, "/api/sim/reset")[0] == 401 and post(url, "/api/sim/start", {"scenario": "app-compromise"})[0] == 401
    assert get(url, "/api/sim/state")[0] == 401
    assert stub.calls == []


# ------------------------------------------------------------------ the simulator control server's own invariants
class FakeCluster:
    def __init__(self, source="SIMULATOR", agent_source="SIMULATOR"):
        class B:
            SOURCE = source
        self.backend = B()
        self.agents = {n: type("A", (), {"source": agent_source})() for n in "ABCD"}
        self.stopped = False

    def stop(self):
        self.stopped = True


def quick(r):
    return True, "ok"


def slow(r):
    r.sleep(30)
    return True, "late"


CAT = {"quick": (sc.Scenario("quick", "Quick", "d", "e", 1), quick), "slow": (sc.Scenario("slow", "Slow", "d", "e", 1), slow)}


def wait(pred, t=10):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_controller_refuses_a_cluster_that_is_not_the_simulator():
    built = []

    def factory(source="LIVE_CLUSTER", agent_source="SIMULATOR"):
        c = FakeCluster(source, agent_source)
        built.append(c)
        return c
    with pytest.raises(ds.SafetyError):
        ds.DemoController(lambda: factory("LIVE_CLUSTER"), CAT, check_env=False)
    assert built[0].stopped                                          # and it is shut down, not left running
    with pytest.raises(ds.SafetyError):
        ds.DemoController(lambda: factory("SIMULATOR", "LIVE_CLUSTER"), CAT, check_env=False)
    with pytest.raises(ds.SafetyError):
        ds.DemoController(lambda: factory("SIMULATOR", "UNKNOWN"), CAT, check_env=False)


def test_controller_refuses_to_run_inside_a_kubernetes_pod(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    with pytest.raises(ds.SafetyError, match="Kubernetes pod"):
        ds.DemoController(lambda: FakeCluster(), CAT)
    with pytest.raises(ds.SafetyError):
        ds.refuse_in_cluster({"KUBERNETES_SERVICE_HOST": "x"})
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    ds.DemoController(lambda: FakeCluster(), CAT).stop()


def test_controller_flips_to_live_after_a_start_is_refused_again_before_acting():
    c = FakeCluster()
    ctl = ds.DemoController(lambda: c, CAT, check_env=False)
    c.backend.SOURCE = "LIVE_CLUSTER"                                # something swapped the backend underneath us
    with pytest.raises(ds.SafetyError):
        ctl.start("quick")
    assert ctl.state()["status"] == "idle"
    ctl.stop()


def test_controller_lifecycle_start_busy_reset_and_stale_results():
    builds = []

    def factory():
        builds.append(1)
        return FakeCluster()
    ctl = ds.DemoController(factory, CAT, check_env=False)
    assert ctl.start("nope")[0] == 400
    assert ctl.start("quick")[0] == 202
    assert wait(lambda: ctl.state()["status"] == "idle")
    st = ctl.state()
    assert st["last"]["ok"] is True and st["needs_reset"] is True
    assert ctl.start("quick")[0] == 409                              # one scenario per reset
    assert ctl.reset()[0] == 200 and len(builds) == 2 and ctl.state()["needs_reset"] is False
    # a slow scenario is cancelled by Reset and its late result is dropped
    assert ctl.start("slow")[0] == 202 and ctl.start("quick")[0] == 409
    assert ctl.reset()[0] == 200 and len(builds) == 3
    time.sleep(0.6)
    assert ctl.state()["last"] is None and ctl.state()["status"] == "idle"
    ctl.stop()


def test_control_server_binds_loopback_and_needs_the_token():
    ctl = ds.DemoController(lambda: FakeCluster(), CAT, check_env=False)
    srv = ds.make_server(ctl, TOKEN, 0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(method, path, token=TOKEN, body=None):
            h = {"Content-Type": "application/json", **({"X-CR-Sim-Token": token} if token else {})}
            req = urllib.request.Request(url + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        assert call("GET", "/state", token=None) == 401 and call("GET", "/state", token="wrong") == 401
        assert call("POST", "/start", token=None, body={"scenario": "quick"}) == 401
        assert call("POST", "/reset", token="wrong", body={}) == 401
        assert ctl.state()["status"] == "idle" and ctl.state()["last"] is None          # nothing happened
        assert call("GET", "/state") == 200 and call("POST", "/start", body={"scenario": "quick"}) == 202
        assert call("POST", "/start", body={"scenario": 3}) == 400
        assert call("GET", "/nope") == 404
        with pytest.raises(ds.SafetyError):
            ds.make_server(ctl, "short", 0)
    finally:
        srv.shutdown()
        ctl.stop()


def test_simulator_code_cannot_reach_kubernetes():
    code = ("import sys; sys.path[:0] = [%r, %r]; import demo_server, scenarios, world; "
            "bad = [m for m in sys.modules if m == 'kubernetes' or m.startswith('kubernetes.')]; "
            "assert not bad, bad; print('ok')" % (os.path.join(ROOT, "sim"), os.path.join(ROOT, "agent")))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
    for f in ("demo_server.py", "scenarios.py"):
        text = open(os.path.join(ROOT, "sim", f)).read()
        code_lines = [l for l in text.splitlines() if not l.strip().startswith(("#", '"""'))]
        assert not any(("K8sBackend" in l or "import kubernetes" in l or "from kubernetes" in l or "load_kube_config" in l) for l in code_lines), f


def test_every_catalogue_scenario_has_a_driver_and_description():
    assert set(sc.CATALOGUE) == {"app-compromise", "false-accusation", "forge-evidence", "agent-crash", "transient-blip",
                                 "slow-burn", "ambiguous", "validation-retry", "failed-validation", "obstruct",
                                 "lab-results-tampered", "medication-failed-logins", "database-cascade"}
    for s, fn in sc.CATALOGUE.values():
        assert callable(fn) and s.description and s.expect and s.est_s > 0
    assert [x["id"] for x in sc.listing()] == list(sc.CATALOGUE)


# ------------------------------------------------------------------ end to end with the REAL simulated cluster
def test_end_to_end_dashboard_to_real_simulated_cluster(monkeypatch, tmp_path):
    base = 52600
    monkeypatch.setenv("STATUS_URLS", ",".join(f"http://localhost:{base + 100 + i}/status" for i in range(4)))
    ctl = ds.DemoController(ds.default_factory(base), check_env=False)
    srv = ds.make_server(ctl, TOKEN, 0)
    try:
        for k in ("DASHBOARD_TOKEN", "DASHBOARD_TOKEN_FILE", "BASELINE_URL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("SIM_CONTROL_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        monkeypatch.setenv("SIM_CONTROL_TOKEN", TOKEN)
        spec = importlib.util.spec_from_file_location("dash_e2e", os.path.join(ROOT, "dashboard", "server.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.COLLECTOR.poll_once()
        agg = mod.aggregate()
        assert agg["data_source"] == "SIMULATOR" and set(agg["data_sources"].values()) == {"SIMULATOR"}   # from the REAL agents
        dsrv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        threading.Thread(target=dsrv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{dsrv.server_address[1]}"
        assert get(url, "/api/sim/state")[1]["enabled"] is True
        assert post(url, "/api/sim/start", {"scenario": "false-accusation"})[0] == 202
        assert post(url, "/api/sim/start", {"scenario": "false-accusation"})[0] == 409
        assert wait(lambda: ctl.state()["status"] == "idle" and ctl.state()["last"], 90)
        assert ctl.state()["last"]["ok"] is True, ctl.state()["last"]
        mod.COLLECTOR.poll_once()
        evs = mod.COLLECTOR.all_events()
        assert evs and {e["data_source"] for e in evs} == {"SIMULATOR"}
        assert post(url, "/api/sim/reset")[0] == 200
        assert ctl.state()["last"] is None and ctl.state()["needs_reset"] is False
        mod.COLLECTOR.poll_once()
        assert mod.aggregate()["data_source"] == "SIMULATOR"
        dsrv.shutdown()
    finally:
        srv.shutdown()
        ctl.stop()
