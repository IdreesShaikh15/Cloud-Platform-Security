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

Local use (against the simulator):
  STATUS_URLS=http://localhost:50251/status,... [BASELINE_URL=http://localhost:50261] \\
      python3 dashboard/server.py
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
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
            "pending_votes": {k: v["voters"] for k, v in pending.items()},
            "pending_detail": pending, "baseline": BASELINE or None,
            "last_poll": COLLECTOR.last_poll, "gseq": COLLECTOR.gseq}


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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, extra=None):
        return self._send(200, json.dumps(obj, default=str).encode(), "application/json", extra)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        p = url.path
        if p == "/api/state":
            return self._json(aggregate())
        if p == "/api/events":
            since = int(q.get("since", ["0"])[0] or 0)
            limit = int(q.get("limit", ["1000"])[0] or 1000)
            return self._json({"gseq": COLLECTOR.gseq, "events": COLLECTOR.events_since(since, limit)})
        if p.startswith("/api/agent/"):
            return self._json(agent_view(p.rsplit("/", 1)[1]))
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
    print(f"dashboard on :{PORT}, agents={URLS}, baseline={BASELINE or '-'}", flush=True)
    threading.Thread(target=COLLECTOR.run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
