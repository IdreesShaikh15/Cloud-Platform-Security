#!/usr/bin/env python3
"""Simulator demo server: runs the simulated four-agent cluster and lets the dashboard start / reset scenarios.

    python3 sim/demo_server.py --dashboard        # simulator + dashboard on http://localhost:8091 (with demo controls)
    python3 sim/demo_server.py                    # simulator + control API only; prints the dashboard command

THIS PROCESS CAN ONLY EVER DRIVE THE SIMULATOR (docs/DASHBOARD.md):
  * it builds `LocalCluster` (FakeBackend + FakeWorld) and never imports the Kubernetes client;
  * it refuses to start inside a Kubernetes pod (KUBERNETES_SERVICE_HOST set);
  * after every build and before every start it asserts that the backend and every agent declare SIMULATOR;
  * it binds 127.0.0.1 only, answers only loopback clients, and requires a random per-run token;
  * a request can only name a scenario from the fixed catalogue (sim/scenarios.py); there are no free-form arguments.
Live scenarios stay terminal scripts (scripts/).
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios as sc  # noqa: E402

SIMULATOR = "SIMULATOR"
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class SafetyError(RuntimeError):
    """Raised instead of ever acting on something that is not provably the simulator."""


def refuse_in_cluster(env=None) -> None:
    env = os.environ if env is None else env
    if env.get("KUBERNETES_SERVICE_HOST"):
        raise SafetyError("refusing to run: this looks like a Kubernetes pod (KUBERNETES_SERVICE_HOST is set). "
                          "The simulator demo server is for a developer machine only.")


def assert_simulator(cluster) -> None:
    b = getattr(getattr(cluster, "backend", None), "SOURCE", None)
    if b != SIMULATOR:
        raise SafetyError(f"refusing: the cluster backend declares {b!r}, not {SIMULATOR!r}")
    agents = getattr(cluster, "agents", {})
    if not agents or any(getattr(a, "source", None) != SIMULATOR for a in agents.values()):
        raise SafetyError("refusing: not every agent declares SIMULATOR as its data source")


def default_factory(base_port: int = 50151):
    def build():
        from world import LocalCluster, fast_config, sim_investigation
        cfg = fast_config(base_port, sim_investigation(), validate_timeout_s=10)
        c = LocalCluster(base_port=base_port, with_status=True, cfg=cfg).start()
        time.sleep(2)
        return c
    return build


class DemoController:
    def __init__(self, factory: Callable[[], object], catalogue=None, check_env: bool = True):
        if check_env:
            refuse_in_cluster()
        self.catalogue = sc.CATALOGUE if catalogue is None else catalogue
        self.factory = factory
        self.lock = threading.RLock()
        self.gen = 0
        self.status = "idle"                       # idle | running | resetting
        self.dirty = False                         # a scenario ran since the last reset
        self.scenario: Optional[str] = None
        self.started_at: Optional[float] = None
        self.last: Optional[dict] = None
        self.cluster = None
        self._build()

    # ---- lifecycle
    def _build(self) -> None:
        c = self.factory()
        try:
            assert_simulator(c)
        except SafetyError:
            try:
                c.stop()
            except Exception:
                pass
            raise
        self.cluster = c

    def start(self, scenario_id: str):
        with self.lock:
            if scenario_id not in self.catalogue:
                return 400, {"error": "unknown scenario"}
            if self.status != "idle":
                return 409, {"error": f"busy ({self.status})"}
            if self.dirty:
                return 409, {"error": "a scenario already ran on this cluster: press Reset first"}
            assert_simulator(self.cluster)           # again, immediately before acting
            self.gen += 1
            gen, c = self.gen, self.cluster
            self.status, self.dirty, self.scenario, self.started_at, self.last = "running", True, scenario_id, time.time(), None
            threading.Thread(target=self._run, args=(gen, c, scenario_id), daemon=True).start()
            return 202, {"started": scenario_id}

    def _run(self, gen: int, cluster, scenario_id: str) -> None:
        meta, fn = self.catalogue[scenario_id]
        try:
            ok, detail = fn(sc.Runner(cluster, lambda: self.gen != gen))
        except Exception as exc:
            ok, detail = False, f"scenario driver error: {exc.__class__.__name__}"
        with self.lock:
            if self.gen != gen:                      # a Reset happened meanwhile: this result is stale, drop it
                return
            self.last = {"scenario": scenario_id, "ok": bool(ok), "detail": detail, "finished_at": time.time(),
                         "duration_s": round(time.time() - (self.started_at or time.time()), 1)}
            self.status = "idle"

    def reset(self):
        with self.lock:
            if self.status == "resetting":
                return 409, {"error": "already resetting"}
            self.gen += 1                            # cancels any running scenario and drops its result
            self.status, old = "resetting", self.cluster
        try:
            try:
                old.stop()
            except Exception:
                pass
            self._build()
        except SafetyError as exc:
            with self.lock:
                self.status = "idle"
            return 500, {"error": str(exc)}
        with self.lock:
            self.status, self.dirty, self.scenario, self.started_at, self.last = "idle", False, None, None, None
        return 200, {"reset": True}

    def state(self) -> dict:
        with self.lock:
            return {"source": SIMULATOR, "status": self.status, "running": self.scenario if self.status == "running" else None,
                    "started_at": self.started_at, "needs_reset": self.dirty and self.status == "idle",
                    "last": self.last,
                    "scenarios": [{"id": s.id, "title": s.title, "description": s.description, "expect": s.expect,
                                   "est_s": s.est_s} for s, _ in self.catalogue.values()]}

    def stop(self) -> None:
        with self.lock:
            self.gen += 1
        try:
            self.cluster.stop()
        except Exception:
            pass


def make_server(ctl: DemoController, token: str, port: int = 50270) -> ThreadingHTTPServer:
    if not token or len(token) < 16:
        raise SafetyError("a control token of at least 16 characters is required")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if self.client_address[0] not in LOOPBACK:
                self._send(403, {"error": "loopback clients only"})
                return False
            if not hmac.compare_digest(self.headers.get("X-CR-Sim-Token", ""), token):
                self._send(401, {"error": "bad or missing control token"})
                return False
            return True

        def do_GET(self):
            if not self._authorised():
                return
            if self.path == "/state":
                return self._send(200, ctl.state())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorised():
                return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 1024)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad JSON"})
            try:
                if self.path == "/start":
                    sid = body.get("scenario")
                    return self._send(*(ctl.start(sid) if isinstance(sid, str) else (400, {"error": "scenario must be a string"})))
                if self.path == "/reset":
                    return self._send(*ctl.reset())
            except SafetyError as exc:
                return self._send(500, {"error": str(exc)})
            return self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)          # loopback only, never 0.0.0.0
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control-port", type=int, default=50270)
    ap.add_argument("--dashboard", action="store_true", help="also start the dashboard on --dashboard-port")
    ap.add_argument("--dashboard-port", type=int, default=8091)
    a = ap.parse_args()
    refuse_in_cluster()
    token = secrets.token_hex(16)
    ctl = DemoController(default_factory())
    srv = make_server(ctl, token, a.control_port)
    urls = ",".join(f"http://localhost:{50251 + i}/status" for i in range(4))
    # The dashboard gets this simulator as its DEMO channel. If LIVE_STATUS_URLS is set in the environment (the presenter's
    # laptop, docs/LIVE_DEMO.md) it also gets the Live page, which stays a separate channel that never sees simulated data.
    env = dict(os.environ, DEMO_STATUS_URLS=urls, SIM_CONTROL_URL=f"http://127.0.0.1:{a.control_port}",
               SIM_CONTROL_TOKEN=token, PORT=str(a.dashboard_port), BIND="127.0.0.1")
    env.pop("STATUS_URLS", None)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = None
    if a.dashboard:
        proc = subprocess.Popen([sys.executable, os.path.join(root, "dashboard", "server.py")], env=env)
        print(f"simulator + dashboard with demo controls: http://localhost:{a.dashboard_port}  (Ctrl-C to stop)", flush=True)
    else:
        print("simulator running. Start the dashboard with:\n  "
              f"DEMO_STATUS_URLS={urls} SIM_CONTROL_URL={env['SIM_CONTROL_URL']} SIM_CONTROL_TOKEN={token} "
              f"PORT={a.dashboard_port} python3 dashboard/server.py", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if proc:
            proc.terminate()
        srv.shutdown()
        ctl.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
