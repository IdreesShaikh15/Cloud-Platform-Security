"""Mock cloud-healthcare application (stdlib only).

One image, selectable roles ($ROLE). The first four are always deployed; the next two are the optional healthcare workloads
(config/workloads.json). ALL DATA IS SYNTHETIC: it comes from the Synthea synthetic patient generator (docs/DATASET.md);
no real patient is involved anywhere.
  patient-portal     (Node A)  web UI; /api/patients, /api/patient, /api/labs, /api/medications, /api/login (calls the services below)
  auth-service       (Node B)  /login, /verify - counts failed logins per source IP
  records-api        (Node C)  /records (token-protected; reads the database)
  database           (Node D)  the Synthea dataset (data/synthea-sample.json): /query, /patient, /labs, /medications
  lab-results        (Node E)  /results (token-protected; laboratory results from the database)
  medication-service (Node F)  /medications (token-protected; active medications from the database)
  client                       synthetic users: an availability probe (once a second) and a few "clinicians" who log in and look up
                               records, laboratory results and medications at a realistic pace; its /stats is the availability
                               measurement plus a summary of that traffic

Every role serves /health and /_telemetry (see telemetry.py). Telemetry only COUNTS requests (a number); the agents never see a record.
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
LAB_URL = os.environ.get("LAB_URL", "http://lab-results:8080")
MEDICATION_URL = os.environ.get("MEDICATION_URL", "http://medication-service:8080")
PORTAL_URL = os.environ.get("PORTAL_URL", "http://patient-portal:8080")
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "demo-secret-change-me").encode()
CLINICIANS = int(os.environ.get("TRAFFIC_CLINICIANS", "3"))          # simulated staff in the client; 0 = availability probe only
INSTANCE_ID = uuid.uuid4().hex[:12]           # changes on every (re)start
STARTED_AT = time.time()
SOURCE = "Synthea"
DATASET_FILE = os.path.join(APP_ROOT, "data", "synthea-sample.json")

USERS = {"dr.smith": "correct-horse", "nurse.jones": "battery-staple", "dr.patel": "orange-pencil",
         "nurse.lee": "quiet-harbor", "portal-svc": "portal-svc-pass"}
CLINICIAN_LOGINS = [u for u in USERS if u != "portal-svc"]

_failed_auth = defaultdict(int)             # source ip -> cumulative failures
_failed_lock = threading.Lock()
_requests = 0                               # served requests (not counting /health and /_telemetry): a number, never content
_req_lock = threading.Lock()


# --------------------------------------------------------------------------- data (database role only)
class Dataset:
    """The Synthea sample, loaded once. Only the database role holds it; every other service asks the database."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.meta = raw["meta"]
        self.patients = raw["patients"]
        self.by_mrn = {p["mrn"]: p for p in self.patients}
        key = lambda t: (t[0], t[1], t[2].get("loinc") or t[2].get("rxnorm") or "")          # noqa: E731  (never compare the dicts themselves)
        self.recent_labs = sorted(((l["date"], p["mrn"], l) for p in self.patients for l in p["labs"]), key=key, reverse=True)
        self.recent_meds = sorted(((m["since"], p["mrn"], m) for p in self.patients for m in p["medications"]), key=key, reverse=True)

    @staticmethod
    def summary(p: dict) -> dict:
        return {"mrn": p["mrn"], "name": p["name"], "dob": p["dob"], "gender": p["gender"], "city": p["city"],
                "condition": p["conditions"][0]["description"] if p["conditions"] else "No active chronic condition recorded",
                "source": SOURCE}


DATASET = Dataset(DATASET_FILE) if ROLE == "database" else None


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


def page(q: dict, default: int = 25, cap: int = 50):
    """?limit=&offset= with sane bounds, so no single answer can be large."""
    def num(k, d):
        try:
            return max(0, int(q.get(k, [d])[0]))
        except ValueError:
            return d
    return min(cap, max(1, num("limit", default))), num("offset", 0)


# --------------------------------------------------------------------------- client role
class Traffic:
    """What the client has done, as numbers (shown by /stats)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_kind = defaultdict(int)
        self.requests = self.errors = self.logins = self.failed_logins = 0

    def add(self, kind: str, ok: bool = True) -> None:
        with self.lock:
            self.by_kind[kind] += 1
            self.requests += 1
            self.errors += 0 if ok else 1

    def summary(self) -> dict:
        with self.lock:
            return {"clinicians": CLINICIANS, "requests": self.requests, "errors": self.errors, "logins": self.logins,
                    "failed_logins": self.failed_logins, "by_kind": dict(self.by_kind), "since": STARTED_AT}


TRAFFIC = Traffic()


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
                ok = len(http_json(f"{PORTAL_URL}/api/patients?limit=5", timeout=1.5)["patients"]) > 0
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
                "samples": s[-600:], "traffic": TRAFFIC.summary()}


class Clinician(threading.Thread):
    """One simulated member of staff: logs in now and then (rarely mistyping the password), looks up patient lists, opens a record and,
    often, its laboratory results and medications, at human speed (a request every 1-3 seconds). Reads only; invents nothing."""
    TYPO_RATE, RELOGIN_S = 0.05, (90, 180)

    def __init__(self, name: str, seed: int):
        super().__init__(daemon=True)
        self.user, self.rng, self.token, self.token_until = name, random.Random(seed), None, 0.0
        self.total = 200                                # refined from the first answer

    def get(self, kind: str, path: str):
        try:
            h = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            out = http_json(f"{PORTAL_URL}{path}", headers=h, timeout=3.0)
            TRAFFIC.add(kind)
            return out
        except Exception:
            TRAFFIC.add(kind, ok=False)
            return None

    def login(self):
        pw = USERS[self.user]
        if self.rng.random() < self.TYPO_RATE:
            pw = pw[:-1] + "x"                          # a mistyped password
        try:
            self.token = http_json(f"{PORTAL_URL}/api/login", {"username": self.user, "password": pw}, timeout=3.0)["token"]
            with TRAFFIC.lock:
                TRAFFIC.logins += 1
        except Exception:
            self.token = None
            with TRAFFIC.lock:
                TRAFFIC.failed_logins += 1
        TRAFFIC.add("login", ok=self.token is not None)
        self.token_until = time.time() + self.rng.uniform(*self.RELOGIN_S)

    def run(self):
        time.sleep(self.rng.uniform(0.0, 3.0))
        while True:
            if not self.token or time.time() > self.token_until:
                self.login()
                if not self.token:
                    time.sleep(self.rng.uniform(2, 5))
                    continue
            r = self.rng.random()
            if r < 0.30:
                out = self.get("patient-list", f"/api/patients?limit=20&offset={self.rng.randrange(0, max(1, self.total - 20))}")
                self.total = (out or {}).get("total", self.total)
            elif r < 0.85:
                out = self.get("patient-list", f"/api/patients?limit=10&offset={self.rng.randrange(0, max(1, self.total - 10))}")
                rows = (out or {}).get("patients") or []
                if rows:
                    mrn = self.rng.choice(rows)["mrn"]
                    self.get("record", f"/api/patient?mrn={mrn}")
                    if self.rng.random() < 0.7:
                        time.sleep(self.rng.uniform(0.5, 1.5))
                        self.get("labs", f"/api/labs?mrn={mrn}")
                    if self.rng.random() < 0.5:
                        time.sleep(self.rng.uniform(0.5, 1.5))
                        self.get("medications", f"/api/medications?mrn={mrn}")
            elif r < 0.95:
                self.get("recent-labs", "/api/labs?limit=15")
            else:
                self.get("recent-medications", "/api/medications?limit=15")
            time.sleep(self.rng.uniform(1.0, 3.0))


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

    def _count(self, path: str) -> None:
        global _requests
        if path not in ("/health", "/_telemetry"):
            with _req_lock:
                _requests += 1

    def _verify(self):
        """Check the caller's bearer token with the auth service. Returns the token, or None after sending the refusal."""
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        try:
            urllib.request.urlopen(f"{AUTH_URL}/verify?token={urllib.parse.quote(token)}", timeout=2)
            return token
        except urllib.error.HTTPError:
            record_auth_failure(self.client_address[0])
            self._send(401, {"error": "invalid token"})
        except Exception as exc:
            self._send(502, {"error": f"auth unavailable: {exc}"})
        return None

    def _ask(self, base: str, path: str):
        """GET a JSON answer from another service; on failure send 502 and return None."""
        try:
            return http_json(f"{base}{path}")
        except Exception as exc:
            self._send(502, {"error": f"upstream unavailable: {exc}"})
            return None

    def _service_token(self) -> str:
        return http_json(f"{AUTH_URL}/login", {"username": "portal-svc", "password": "portal-svc-pass"})["token"]

    # ---- GET
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path
        self._count(path)
        if path == "/health":
            return self._send(200, {"status": "ok", "role": ROLE, "instance_id": INSTANCE_ID})
        if path == "/_telemetry":
            return self._send(200, self._telemetry())
        one = lambda k: (q.get(k, [""])[0])       # noqa: E731

        # ---- database: the Synthea dataset
        if ROLE == "database":
            d = DATASET
            if path == "/query":
                limit, offset = page(q)
                rows = [d.summary(p) for p in d.patients[offset: offset + limit]]
                return self._send(200, {"rows": rows, "total": len(d.patients), "offset": offset, "synthetic": True, "source": SOURCE})
            if path == "/patient":
                p = d.by_mrn.get(one("mrn"))
                return self._send(200 if p else 404, {"patient": {**d.summary(p), "conditions": p["conditions"]}, "synthetic": True, "source": SOURCE}
                                  if p else {"error": "unknown patient"})
            if path in ("/labs", "/medications"):
                limit, _ = page(q, 12, 30)
                key = "labs" if path == "/labs" else "medications"
                if one("mrn"):
                    p = d.by_mrn.get(one("mrn"))
                    if not p:
                        return self._send(404, {"error": "unknown patient"})
                    return self._send(200, {"mrn": p["mrn"], "items": p[key][:limit], "synthetic": True, "source": SOURCE})
                recent = d.recent_labs if key == "labs" else d.recent_meds
                return self._send(200, {"items": [{"mrn": m, **row} for _, m, row in recent[:limit]], "synthetic": True, "source": SOURCE})

        # ---- auth
        if ROLE == "auth-service" and path == "/verify":
            return self._send(200 if token_valid(q.get("token", [""])[0]) else 401, {})

        # ---- records-api: token-protected demographics + conditions
        if ROLE == "records-api" and path == "/records":
            if self._verify() is None:
                return
            if one("mrn"):
                out = self._ask(DATABASE_URL, f"/patient?mrn={urllib.parse.quote(one('mrn'))}")
                return None if out is None else self._send(200, {"record": out["patient"], "synthetic": True, "source": SOURCE})
            limit, offset = page(q)
            out = self._ask(DATABASE_URL, f"/query?limit={limit}&offset={offset}")
            return None if out is None else self._send(200, {"records": out["rows"], "total": out["total"], "synthetic": True, "source": SOURCE})

        # ---- lab-results / medication-service: token-protected, both read the database
        if ROLE in ("lab-results", "medication-service") and path in ("/results", "/medications"):
            if self._verify() is None:
                return
            what, key, ok_path = (("labs", "results", "/results") if ROLE == "lab-results" else ("medications", "medications", "/medications"))
            if path == ok_path:
                limit, _ = page(q, 12, 30)
                suffix = f"mrn={urllib.parse.quote(one('mrn'))}&" if one("mrn") else ""
                out = self._ask(DATABASE_URL, f"/{what}?{suffix}limit={limit}")
                return None if out is None else self._send(200, {key: out["items"], "mrn": out.get("mrn"), "synthetic": True, "source": SOURCE})

        # ---- patient portal
        if ROLE == "patient-portal":
            if path in ("/", "/index.html"):
                with open(os.path.join(APP_ROOT, "static", "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            routes = {"/api/patients": (RECORDS_URL, "/records", "patients"), "/api/patient": (RECORDS_URL, "/records", "record"),
                      "/api/labs": (LAB_URL, "/results", "results"), "/api/medications": (MEDICATION_URL, "/medications", "medications")}
            if path in routes:
                base, rel, key = routes[path]
                try:
                    # a signed-in clinician's own token is passed on; otherwise the portal uses its service account
                    tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() or self._service_token()
                    if tok and not token_valid(tok):
                        tok = self._service_token()
                    keep = {k: v[0] for k, v in q.items() if k in ("mrn", "limit", "offset")}
                    out = http_json(f"{base}{rel}" + ("?" + urllib.parse.urlencode(keep) if keep else ""), headers={"Authorization": f"Bearer {tok}"})
                    first = next(k for k in ("records", "record", "results", "medications") if k in out)
                    return self._send(200, {key: out[first], "total": out.get("total"), "served_by": INSTANCE_ID, "synthetic": True, "source": SOURCE})
                except Exception as exc:
                    return self._send(503, {"error": f"upstream failure: {exc}"})

        if ROLE == "client" and path == "/stats":
            return self._send(200, PROBE.stats())

        return self._send(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        self._count(self.path)
        if ROLE == "auth-service" and self.path == "/login":
            body = self._json_body()
            user, pw = body.get("username", ""), body.get("password", "")
            if USERS.get(user) == pw:
                return self._send(200, {"token": make_token(user)})
            record_auth_failure(self.client_address[0])
            return self._send(401, {"error": "bad credentials"})
        if ROLE == "patient-portal" and self.path == "/api/login":          # a clinician signs in through the portal
            try:
                return self._send(200, http_json(f"{AUTH_URL}/login", self._json_body()))
            except urllib.error.HTTPError as exc:
                return self._send(exc.code, {"error": "bad credentials"})
            except Exception as exc:
                return self._send(503, {"error": f"auth unavailable: {exc}"})
        return self._send(404, {"error": "not found"})

    def _telemetry(self) -> dict:
        with _failed_lock:
            failed = dict(_failed_auth)
        data = telemetry.snapshot(APP_ROOT, PORT)
        data.update({
            "role": ROLE, "instance_id": INSTANCE_ID, "started_at": STARTED_AT,
            "pod_ip": os.environ.get("POD_IP") or socket.gethostbyname(socket.gethostname()),
            "pod_name": os.environ.get("POD_NAME", socket.gethostname()),
            "auth_failures_by_ip": failed, "requests_total": _requests, "time": time.time(),
        })
        return data


def main():
    if PROBE:
        PROBE.start()
        for i, user in enumerate(CLINICIAN_LOGINS[:CLINICIANS]):
            Clinician(user, seed=1000 + i).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"[{ROLE}] listening on :{PORT} instance={INSTANCE_ID}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
