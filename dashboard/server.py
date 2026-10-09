"""Read-only status dashboard.

A background poller reads every agent's /status (and the baseline
controller's, if running) once a second, caches it, and merges each agent's
new events into one timeline buffer. The dashboard has no write path to
anything.

  GET /                 page
  GET /api/state        aggregated view (JSON)
  GET /api/events       merged event timeline; ?since=<gseq>&limit=<n>
  GET /api/agent/<id>   one agent's full view + its recent events
  GET /api/export       every buffered event from every agent, as a JSON download
  GET /api/metrics      per-incident metrics from every agent/controller
  GET /api/snapshot/<agent>/<key>   one evidence snapshot, as a JSON download

Local use (against the simulator):
  STATUS_URLS=http://localhost:50251/status,... [BASELINE_URL=http://localhost:50261] \\
      python3 dashboard/server.py
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import statistics
import threading
import time
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8090"))
POLL_S = float(os.environ.get("POLL_S", "1.0"))
BUFFER = int(os.environ.get("EVENT_BUFFER", "5000"))
POOL = ThreadPoolExecutor(max_workers=8)


def agent_urls() -> dict:
    if os.environ.get("STATUS_URLS"):
        urls = os.environ["STATUS_URLS"].split(",")
        return {chr(ord("A") + i): u.strip() for i, u in enumerate(urls)}
    with open(os.environ.get("RESILIENCE_CONFIG", "/etc/resilience/config.json")) as fh:
        cfg = json.load(fh)
    return {nid: spec["status_url"] for nid, spec in cfg["nodes"].items()}


URLS = agent_urls()
BASELINE = os.environ.get("BASELINE_URL", "").rstrip("/")
SOURCES = dict(URLS, **({"CENTRAL": f"{BASELINE}/status"} if BASELINE else {}))


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


# --------------------------------------------------------------------------- collector
class Collector:
    """Polls every source, caches its latest status, merges events."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status: dict = {}                    # source id -> latest status (events stripped)
        self.cursor: dict = {}                    # source id -> (boot, last seq)
        self.events: deque = deque(maxlen=BUFFER)
        self.gseq = 0
        self.last_poll = 0.0

    def _fetch(self, sid: str, url: str):
        boot, seq = self.cursor.get(sid, (None, None))
        sep = "&" if "?" in url else "?"
        res = get(f"{url}{sep}events_since={seq}" if seq is not None else url)
        if res and boot is not None and res.get("event_boot") != boot:
            res = get(url)                       # agent restarted: take its whole ring
        return sid, res

    def poll_once(self) -> None:
        results = list(POOL.map(lambda kv: self._fetch(*kv), SOURCES.items()))
        with self.lock:
            for sid, res in results:
                if res is None:
                    self.status.pop(sid, None)
                    continue
                evs = res.pop("events", []) or []
                boot = res.get("event_boot")
                last_boot, last_seq = self.cursor.get(sid, (None, None))
                if boot != last_boot:
                    last_seq = None
                for e in evs:
                    if last_seq is not None and e["seq"] <= last_seq:
                        continue
                    self.gseq += 1
                    self.events.append({**e, "gseq": self.gseq, "source": sid})
                if evs:
                    last_seq = max(e["seq"] for e in evs)
                self.cursor[sid] = (boot, last_seq if last_seq is not None else res.get("event_seq"))
                self.status[sid] = res
            self.last_poll = time.time()

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.poll_once()
            except Exception:
                pass
            time.sleep(max(0.1, POLL_S - (time.time() - t0)))

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def events_since(self, since: int, limit: int) -> list:
        with self.lock:
            evs = [e for e in self.events if e["gseq"] > since]
        return evs[-limit:]

    def all_events(self) -> list:
        with self.lock:
            return list(self.events)


COLLECTOR = Collector()


# --------------------------------------------------------------------------- access control
class Auth:
    """Optional shared-token login. OFF unless a token is configured, so local demos stay open.

    The token comes from the environment (DASHBOARD_TOKEN) or a file (DASHBOARD_TOKEN_FILE, e.g. a
    mounted Kubernetes Secret). It is never hardcoded, never logged and never sent back. A browser
    logs in once (POST /login) and gets a random, expiring, HttpOnly session cookie; scripts send
    `Authorization: Bearer <token>`. EVERY endpoint except /healthz and the login page itself
    requires it, including all of /api/*.
    """
    SESSION_TTL_S = 8 * 3600
    MAX_FAILS, FAIL_WINDOW_S = 5, 60.0

    def __init__(self, token: str = ""):
        self._token = token or ""
        self._sessions: dict = {}
        self._fails: dict = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "Auth":
        tok = os.environ.get("DASHBOARD_TOKEN", "")
        path = os.environ.get("DASHBOARD_TOKEN_FILE", "")
        if not tok and path:
            # A token file was asked for. If it cannot be read, FAIL CLOSED: never fall back to an
            # open dashboard because of a misconfigured secret mount.
            try:
                with open(path) as fh:
                    tok = fh.read().strip()
            except OSError as exc:
                raise RuntimeError(f"DASHBOARD_TOKEN_FILE is set but unreadable ({exc.strerror}); "
                                   f"refusing to start without access control") from exc
            if not tok:
                raise RuntimeError("DASHBOARD_TOKEN_FILE is empty; refusing to start without access control")
        return cls(tok)

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def token_ok(self, candidate: str) -> bool:
        return self.enabled and hmac.compare_digest(candidate.encode(), self._token.encode())

    def locked_out(self, ip: str, now: float = None) -> bool:
        now = now or time.time()
        with self._lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < self.FAIL_WINDOW_S]
            self._fails[ip] = recent
            return len(recent) >= self.MAX_FAILS

    def record_fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions = {k: v for k, v in self._sessions.items() if v > time.time()}
            self._sessions[sid] = time.time() + self.SESSION_TTL_S
        return sid

    def session_ok(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.get(sid, 0) > time.time()

    def drop_session(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def request_ok(self, headers) -> bool:
        """True if auth is off, or the request carries a valid bearer token or session cookie."""
        if not self.enabled:
            return True
        m = headers.get("Authorization", "")
        if m.startswith("Bearer ") and self.token_ok(m[7:].strip()):
            return True
        raw = headers.get("Cookie", "")
        if raw:
            try:
                c = SimpleCookie(raw)
                if "cr_session" in c and self.session_ok(c["cr_session"].value):
                    return True
            except Exception:
                return False
        return False


AUTH = Auth.from_env()

LOGIN_PAGE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resilience Status - sign in</title><style>
body{margin:0;display:grid;place-items:center;min-height:100vh;background:#0f1419;color:#e6edf3;font:14px system-ui,sans-serif}
form{background:#1a2129;padding:24px;border-radius:8px;width:min(340px,90vw)}h1{font-size:16px;margin:0 0 12px}
input,button{width:100%;box-sizing:border-box;padding:8px;margin-top:8px;border-radius:6px;border:1px solid #30363d;background:#0f1419;color:#e6edf3;font:inherit}
button{background:#58a6ff;color:#000;border:0;cursor:pointer}.m{color:#8b949e;font-size:12px;margin-top:8px}.e{color:#f85149}
</style></head><body><form method="post" action="login"><h1>Distributed Cyber-Resilience Platform</h1>
<div class="m">This dashboard needs an access token.</div>
<input type="password" name="token" placeholder="access token" autocomplete="current-password" autofocus>
<button type="submit">Sign in</button>__ERR__</form></body></html>"""


# --------------------------------------------------------------------------- aggregation
def _vote_view(key: str, statuses: dict, ids: list) -> dict:
    """Who voted for proposal `key`, who didn't and why, who excludes whom."""
    action, target = key.split(":")[0], key.split(":")[1]
    voters = set()
    excluded = []
    for sid, s in statuses.items():
        d = (s.get("pending_votes_detail") or {}).get(key)
        if d:
            voters.update(d["voters"])
            for x in d["excluded"]:
                excluded.append({"viewer": sid, **x})
    rows = []
    for n in ids:
        s = statuses.get(n)
        if n in voters:
            rows.append({"agent": n, "state": "voted", "reason": ""})
        elif s is None:
            rows.append({"agent": n, "state": "unreachable",
                         "reason": "agent is not responding (crashed or partitioned)"})
        else:
            r = (s.get("vote_reasons") or {}).get(f"{action}:{target}")
            phase = (s.get("targets", {}).get(target) or {}).get("phase", "?")
            reason = r["reason"] if r and not r["voted"] else (
                f"its local view of {target} is {phase}" if not r else "about to vote")
            rows.append({"agent": n, "state": "not voted", "reason": reason})
    return {"proposal": key, "voters": sorted(voters), "agents": rows, "excluded": excluded}


OUTCOME_TEXT = {
    "CORROBORATED": "confirmed (quorum may proceed)",
    "FALSE_POSITIVE": "false positive, closed",
    "AMBIGUOUS": "ambiguous: under watch, human review",
    "UNCERTAIN": "uncertain: under watch, human review",
    "SUPERSEDED": "superseded (incident changed)",
    "MERGED": "merged into an earlier one",
}


def investigations_view(live: dict) -> dict:
    """One row per investigation id (the four agents each run their own copy of it), plus the
    set of workloads currently under watch / flagged for human review."""
    rows: dict = {}
    watch: dict = {}
    enabled = False
    for sid, s in live.items():
        inv = s.get("investigations") or {}
        enabled = enabled or bool(inv.get("enabled"))
        for r in list(inv.get("active", [])) + list(inv.get("recent", [])):
            m = rows.setdefault(r["id"], {
                "id": r["id"], "target": r["target"], "workload": r["workload"], "epoch": r["epoch"],
                "question": r["question"], "trigger": r["trigger"], "signals": r["signals"],
                "initiator": r["initiator"], "started_at": r["started_at"], "deadline": r["deadline"],
                "per_agent": {}})
            m["per_agent"][sid] = {"state": r["state"], "outcome": r["outcome"], "reason": r["reason"],
                                   "time_left_s": r["time_left_s"], "own_view": (r.get("own") or {}).get("view"),
                                   "views": r.get("views") or {}, "silent": r.get("silent") or []}
        for tgt, w in (inv.get("watch") or {}).items():
            e = watch.setdefault(tgt, {"target": tgt, "workload": w["workload"], "agents": [],
                                       "review_needed": False, "reason": w["reason"], "outcome": w["outcome"]})
            e["agents"].append(sid)
            e["review_needed"] = e["review_needed"] or bool(w["review_needed"])
    out = []
    for m in rows.values():
        pa = m["per_agent"]
        active = [a for a in pa.values() if a["state"] == "ACTIVE"]
        outcomes = Counter(a["outcome"] for a in pa.values() if a["outcome"])
        m["active"] = bool(active)
        m["time_left_s"] = max((a["time_left_s"] for a in active), default=0.0)
        m["outcomes"] = dict(outcomes)
        top = outcomes.most_common(1)[0][0] if outcomes else None
        m["outcome"] = top
        m["outcome_text"] = OUTCOME_TEXT.get(top, "in progress") if top else "in progress"
        out.append(m)
    out.sort(key=lambda m: (not m["active"], -m["started_at"]))
    return {"enabled": enabled, "rows": out[:12], "watch": list(watch.values())}


def decision_logs_view(live: dict) -> dict:
    """Is every agent's tamper-evident decision log intact? (hash chain, see decisionlog.py)"""
    per = {sid: s["decision_log"] for sid, s in live.items() if s.get("decision_log")}
    broken = sorted(sid for sid, d in per.items() if not d.get("ok"))
    return {"agents": per, "broken": broken, "checked": len(per),
            "ok": (not broken) if per else None}


def aggregate() -> dict:
    statuses = COLLECTOR.snapshot()
    if not statuses:                             # poller not warmed up yet
        COLLECTOR.poll_once()
        statuses = COLLECTOR.snapshot()
    ids = list(URLS)
    live = {k: v for k, v in statuses.items() if k in URLS}
    base = statuses.get("CENTRAL")
    nodes = {}
    for nid in ids:
        views = [s["targets"].get(nid) for s in live.values() if nid in s.get("targets", {})]
        if not views and base:
            views = [base["targets"].get(nid)] if nid in base.get("targets", {}) else []
        phase = Counter(v["phase"] for v in views).most_common(1)[0][0] if views else "UNKNOWN"
        stage = Counter(v["stage"] for v in views).most_common(1)[0][0] if views else "-"
        decisions = [v["last_decision"] for v in views if v.get("last_decision")]
        last = max(decisions, key=lambda d: d["t"]) if decisions else None
        peer_views = [s["agent_trust"].get(nid) for p, s in live.items() if p != nid]
        own = live.get(nid, {}).get("targets", {}).get(nid, {})
        nodes[nid] = {
            "workload": views[0]["workload"] if views else "?",
            "agent_up": nid in live,
            "agent_compromised_sim": (live.get(nid) or {}).get("compromised"),
            "phase": phase, "stage": stage,
            "workload_trust": median(v.get("workload_trust") for v in views),
            "agent_trust": median(peer_views),
            "agent_trust_views": {p: s["agent_trust"].get(nid) for p, s in live.items() if p != nid},
            "detections": own.get("detections", []),
            "score": own.get("score"),
            "last_decision": last,
        }
        disp = [v.get("display_state") or v["phase"] for v in views]
        nodes[nid]["display_state"] = ("NEEDS_ATTENTION" if "NEEDS_ATTENTION" in disp
                                       else "UNKNOWN" if "UNKNOWN" in disp else phase)
        nodes[nid]["attention"] = next((v["attention"] for v in views if v.get("attention")), None)
        nodes[nid]["attempt"] = max((v.get("attempt", 1) for v in views), default=1)
        nodes[nid]["recovery"] = next((v["recovery"] for v in views if v.get("recovery")), None)
        nodes[nid]["watch"] = any(nid in ((s_.get("investigations") or {}).get("watch") or {})
                                  for s_ in live.values())
    # committed decisions, de-duplicated, with who saw them and the justification
    merged = {}
    srcs = dict(live, **({"CENTRAL": base} if base else {}))
    for sid, s in srcs.items():
        for d in s.get("decisions", []):
            k = (d["action"], d["target"], d["epoch"], d["stage"])
            m = merged.setdefault(k, {**d, "seen_by": []})
            m["seen_by"].append(sid)
            if not m.get("justification") and d.get("justification"):
                m["justification"] = d["justification"]
    uniq = sorted(merged.values(), key=lambda d: d["t"], reverse=True)
    rejections = sorted(({**r, "seen_by": p} for p, s in live.items() for r in s.get("rejections", [])),
                        key=lambda r: r["t"], reverse=True)[:10]
    keys = sorted({k for s in live.values() for k in (s.get("pending_votes") or {})})
    pending = {k: _vote_view(k, live, ids) for k in keys}
    return {"mode": "centralized" if base and not live else "distributed",
            "nodes": nodes, "decisions": uniq[:15], "rejections": rejections,
            "investigations": investigations_view(live),
            "decision_logs": decision_logs_view(live),
            "snapshots": snapshots_view(live), "actions": actions_view(live),
            "pending_votes": {k: v["voters"] for k, v in pending.items()},
            "pending_detail": pending, "baseline": BASELINE or None,
            "central_compromised": (base or {}).get("compromised"),
            "last_poll": COLLECTOR.last_poll, "gseq": COLLECTOR.gseq}


def snapshots_view(live: dict) -> list:
    """Evidence snapshots every agent saved (summary only; the full JSON is /api/snapshot/<agent>/<key>)."""
    rows = [{**x, "agent": sid} for sid, s in live.items() for x in (s.get("snapshots") or [])]
    return sorted(rows, key=lambda r: r.get("captured_at") or 0, reverse=True)[:20]


def actions_view(live: dict) -> dict:
    """Every Kubernetes action and its observable result, plus the outcomes still UNKNOWN."""
    recent, unknown = [], []
    for sid, s in live.items():
        a = s.get("actions") or {}
        recent += a.get("recent", [])
        unknown += a.get("unknown", [])
    recent.sort(key=lambda r: r["t"], reverse=True)
    return {"recent": recent[:40], "unknown": unknown}


SNAP_KEY = __import__("re").compile(r"^[A-Za-z0-9_:-]{1,80}$")


def snapshot_export(sid: str, key: str):
    if sid not in URLS or not SNAP_KEY.match(key):
        return None
    return get(URLS[sid].replace("/status", f"/snapshot/{key}"))


def agent_view(sid: str) -> dict:
    s = COLLECTOR.snapshot().get(sid)
    evs = [e for e in COLLECTOR.all_events() if e["node"] == sid][-150:]
    return {"id": sid, "up": s is not None, "status": s, "events": evs}


def metrics() -> list:
    urls = [u.replace("/status", "/metrics") for u in URLS.values()]
    if BASELINE:
        urls.append(f"{BASELINE}/metrics")
    rows = []
    for res in POOL.map(get, urls):
        rows.extend(res or [])
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype: str, extra: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, extra=None):
        return self._send(200, json.dumps(obj, default=str).encode(), "application/json", extra)

    def _ip(self) -> str:
        return self.client_address[0]

    def _login_page(self, code: int = 200, err: str = ""):
        body = LOGIN_PAGE.replace(b"__ERR__", (b'<div class="m e">' + err.encode() + b"</div>") if err else b"")
        return self._send(code, body, "text/html; charset=utf-8")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/login" or not AUTH.enabled:
            return self._send(404, b"not found", "text/plain")
        if AUTH.locked_out(self._ip()):
            return self._login_page(429, "Too many attempts. Wait a minute and try again.")
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(n).decode(errors="replace"))
        if AUTH.token_ok((form.get("token", [""])[0])):
            sid = AUTH.new_session()
            secure = "; Secure" if os.environ.get("DASHBOARD_COOKIE_SECURE") == "1" else ""
            return self._send(303, b"", "text/plain", {
                "Location": "./", "Set-Cookie": f"cr_session={sid}; HttpOnly; SameSite=Strict; Path=/{secure}"})
        AUTH.record_fail(self._ip())
        return self._login_page(401, "Wrong token.")

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        p = url.path
        if p == "/healthz":                      # liveness only: no data
            return self._json({"ok": True})
        if p == "/logout":
            m = SimpleCookie(self.headers.get("Cookie", ""))
            if "cr_session" in m:
                AUTH.drop_session(m["cr_session"].value)
            return self._send(303, b"", "text/plain", {"Location": "./",
                                                      "Set-Cookie": "cr_session=; Max-Age=0; Path=/"})
        if not AUTH.request_ok(self.headers):
            if p in ("/", "/index.html"):
                return self._login_page()
            return self._send(401, b'{"error":"authentication required"}', "application/json",
                              {"WWW-Authenticate": "Bearer"})
        if p == "/api/state":
            return self._json(aggregate())
        if p == "/api/events":
            since = int(q.get("since", ["0"])[0] or 0)
            limit = int(q.get("limit", ["1000"])[0] or 1000)
            return self._json({"gseq": COLLECTOR.gseq, "events": COLLECTOR.events_since(since, limit)})
        if p.startswith("/api/agent/"):
            return self._json(agent_view(p.rsplit("/", 1)[1]))
        if p.startswith("/api/snapshot/"):
            parts = p.split("/")
            snap = snapshot_export(parts[3], parts[4]) if len(parts) == 5 else None
            if snap is None:
                return self._send(404, b'{"error":"no such snapshot"}', "application/json")
            return self._json(snap, {"Content-Disposition": f'attachment; filename="snapshot-{parts[4].replace(":", "-")}-{parts[3]}.json"'})
        if p == "/api/export":
            name = f"resilience-events-{time.strftime('%Y%m%d-%H%M%S')}.json"
            return self._json({"exported_at": time.time(), "sources": list(SOURCES),
                               "events": COLLECTOR.all_events()},
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if p == "/api/metrics":
            return self._json(metrics())
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"dashboard on :{PORT}, agents={URLS}, baseline={BASELINE or '-'}, "
          f"access token: {'REQUIRED' if AUTH.enabled else 'off (open)'}", flush=True)
    threading.Thread(target=COLLECTOR.run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
