"""Mock cloud-healthcare application (stdlib only).

One image, five roles selected by $ROLE:
  patient-portal  (Node A)  web UI + /api/patients (calls auth + records)
  auth-service    (Node B)  /login, /verify - counts failed logins per source IP
  records-api     (Node C)  /records (token-protected, reads from database)
  database        (Node D)  /query - synthetic patient records (no real data)
  client                    synthetic user hitting the portal once a second;
                            its /stats is the availability measurement

Every role serves /health and /_telemetry (see telemetry.py).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telemetry

ROLE = os.environ.get("ROLE", "database")
PORT = int(os.environ.get("PORT", "8080"))
APP_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.abspath(__file__)))
AUTH_URL = os.environ.get("AUTH_URL", "http://auth-service:8080")
RECORDS_URL = os.environ.get("RECORDS_URL", "http://records-api:8080")
DATABASE_URL = os.environ.get("DATABASE_URL", "http://database:8080")
PORTAL_URL = os.environ.get("PORTAL_URL", "http://patient-portal:8080")
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "demo-secret-change-me").encode()
INSTANCE_ID = uuid.uuid4().hex[:12]           # changes on every (re)start
STARTED_AT = time.time()

USERS = {"dr.smith": "correct-horse", "nurse.jones": "battery-staple",
         "portal-svc": "portal-svc-pass"}

_failed_auth = defaultdict(int)             # source ip -> cumulative failures
_failed_lock = threading.Lock()


# --------------------------------------------------------------------------- data
def synthetic_patients(n: int = 40) -> list:
    rng = random.Random(42)
    first = ["Asha", "Ben", "Chen", "Divya", "Elena", "Farid", "Grace", "Hiro",
             "Imani", "Jonas", "Kavya", "Liam", "Maya", "Noah", "Olu", "Priya"]
    last = ["Rao", "Smith", "Li", "Garcia", "Okafor", "Khan", "Novak", "Tanaka"]
    conditions = ["Hypertension", "Type 2 diabetes", "Asthma", "Healthy",
                  "Hyperlipidemia", "Migraine", "Anemia", "Hypothyroidism"]
    return [{"mrn": f"MRN{100000 + i}",
             "name": f"{rng.choice(first)} {rng.choice(last)}",
             "dob": f"19{rng.randint(40, 99)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
             "condition": rng.choice(conditions)} for i in range(n)]


PATIENTS = synthetic_patients()


def make_token(user: str) -> str:
    sig = hmac.new(TOKEN_SECRET, user.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user}.{sig}"


def token_valid(token: str) -> bool:
    user, _, _sig = token.partition(".")
    return bool(user) and hmac.compare_digest(make_token(user), token)


def record_auth_failure(ip: str) -> None:
    with _failed_lock:
        _failed_auth[ip] += 1


def http_json(url: str, data: dict = None, headers: dict = None, timeout: float = 2.0):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# --------------------------------------------------------------------------- client role
class AvailabilityProbe(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples = deque(maxlen=3600)
        self.lock = threading.Lock()

    def run(self):
        while True:
            t = time.time()
            ok = False
            try:
                ok = len(http_json(f"{PORTAL_URL}/api/patients", timeout=1.5)["patients"]) > 0
            except Exception:
                ok = False
            with self.lock:
                self.samples.append((round(t, 2), ok))
            time.sleep(max(0.0, 1.0 - (time.time() - t)))

    def stats(self) -> dict:
        with self.lock:
            s = list(self.samples)
        ok = sum(1 for _, v in s if v)
        return {"total": len(s), "ok": ok, "fail": len(s) - ok,
                "availability": round(ok / len(s), 4) if s else None,
                "samples": s[-600:]}


PROBE = AvailabilityProbe() if ROLE == "client" else None


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"cr-{ROLE}"

    def log_message(self, fmt, *args):  # keep container logs quiet
        pass

    def _send(self, code: int, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # ---- GET
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path
        if path == "/health":
            return self._send(200, {"status": "ok", "role": ROLE, "instance_id": INSTANCE_ID})
        if path == "/_telemetry":
            return self._send(200, self._telemetry())

        if ROLE == "database" and path == "/query":
            return self._send(200, {"rows": PATIENTS})

        if ROLE == "auth-service" and path == "/verify":
            return self._send(200 if token_valid(q.get("token", [""])[0]) else 401, {})

        if ROLE == "records-api" and path == "/records":
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            try:
                urllib.request.urlopen(
                    f"{AUTH_URL}/verify?token={urllib.parse.quote(token)}", timeout=2)
            except urllib.error.HTTPError:
                record_auth_failure(self.client_address[0])
                return self._send(401, {"error": "invalid token"})
            except Exception as exc:
                return self._send(502, {"error": f"auth unavailable: {exc}"})
            try:
                return self._send(200, {"records": http_json(f"{DATABASE_URL}/query")["rows"]})
            except Exception as exc:
                return self._send(502, {"error": f"database unavailable: {exc}"})

        if ROLE == "patient-portal":
            if path in ("/", "/index.html"):
                with open(os.path.join(APP_ROOT, "static", "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if path == "/api/patients":
                try:
                    tok = http_json(f"{AUTH_URL}/login",
                                    {"username": "portal-svc", "password": "portal-svc-pass"})["token"]
                    recs = http_json(f"{RECORDS_URL}/records",
                                     headers={"Authorization": f"Bearer {tok}"})["records"]
                    return self._send(200, {"patients": recs, "served_by": INSTANCE_ID})
                except Exception as exc:
                    return self._send(503, {"error": f"upstream failure: {exc}"})

        if ROLE == "client" and path == "/stats":
            return self._send(200, PROBE.stats())

        return self._send(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        if ROLE == "auth-service" and self.path == "/login":
            body = self._json_body()
            user, pw = body.get("username", ""), body.get("password", "")
            if USERS.get(user) == pw:
                return self._send(200, {"token": make_token(user)})
            record_auth_failure(self.client_address[0])
            return self._send(401, {"error": "bad credentials"})
        return self._send(404, {"error": "not found"})

    def _telemetry(self) -> dict:
        with _failed_lock:
            failed = dict(_failed_auth)
        data = telemetry.snapshot(APP_ROOT, PORT)
        data.update({
            "role": ROLE, "instance_id": INSTANCE_ID, "started_at": STARTED_AT,
            "pod_ip": os.environ.get("POD_IP") or socket.gethostbyname(socket.gethostname()),
            "pod_name": os.environ.get("POD_NAME", socket.gethostname()),
            "auth_failures_by_ip": failed, "time": time.time(),
        })
        return data


def main():
    if PROBE:
        PROBE.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"[{ROLE}] listening on :{PORT} instance={INSTANCE_ID}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
