"""Read-only status dashboard.

Polls every agent's /status (and the baseline controller's, if running),
aggregates, and serves one HTML page. It has no write path to anything.

  GET /             page
  GET /api/state    aggregated view (JSON)
  GET /api/metrics  per-incident metrics from every agent/controller (JSON)

Local use (against the simulator):  STATUS_URLS=http://localhost:50251/status,... python3 dashboard/server.py
"""
from __future__ import annotations

import json
import os
import statistics
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8090"))
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


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def aggregate() -> dict:
    ids = list(URLS)
    statuses = dict(zip(ids, POOL.map(get, URLS.values())))
    base = get(f"{BASELINE}/status") if BASELINE else None
    live = {k: v for k, v in statuses.items() if v}
    nodes = {}
    for nid in ids:
        views = [s["targets"].get(nid) for s in live.values() if nid in s.get("targets", {})]
        phase = Counter(v["phase"] for v in views).most_common(1)[0][0] if views else "UNKNOWN"
        stage = Counter(v["stage"] for v in views).most_common(1)[0][0] if views else "-"
        decisions = [v["last_decision"] for v in views if v.get("last_decision")]
        last = max(decisions, key=lambda d: d["t"]) if decisions else None
        # agent trust: median of *peers'* views (a node's view of itself is ignored)
        peer_views = [s["agent_trust"].get(nid) for p, s in live.items() if p != nid]
        own = live.get(nid, {}).get("targets", {}).get(nid, {})
        nodes[nid] = {
            "workload": views[0]["workload"] if views else "?",
            "agent_up": nid in live,
            "agent_compromised_sim": (live.get(nid) or {}).get("compromised"),
            "phase": phase, "stage": stage,
            "workload_trust": median(v["workload_trust"] for v in views),
            "agent_trust": median(peer_views),
            "agent_trust_views": {p: s["agent_trust"].get(nid) for p, s in live.items() if p != nid},
            "detections": own.get("detections", []),
            "score": own.get("score"),
            "last_decision": last,
        }
    decisions = sorted((d for s in live.values() for d in s.get("decisions", [])),
                       key=lambda d: d["t"], reverse=True)
    seen, uniq = set(), []
    for d in decisions:
        key = (d["action"], d["target"], d["epoch"], d["stage"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    rejections = sorted(({**r, "seen_by": p} for p, s in live.items() for r in s.get("rejections", [])),
                        key=lambda r: r["t"], reverse=True)[:10]
    pending = {}
    for s in live.values():
        for k, voters in s.get("pending_votes", {}).items():
            pending.setdefault(k, set()).update(voters)
    return {"mode": "centralized" if base and not live else "distributed",
            "nodes": nodes, "decisions": uniq[:15], "rejections": rejections,
            "pending_votes": {k: sorted(v) for k, v in pending.items()},
            "baseline": base}


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

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            return self._send(200, json.dumps(aggregate(), default=str).encode(), "application/json")
        if self.path.startswith("/api/metrics"):
            return self._send(200, json.dumps(metrics(), default=str).encode(), "application/json")
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"dashboard on :{PORT}, agents={URLS}, baseline={BASELINE or '-'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
