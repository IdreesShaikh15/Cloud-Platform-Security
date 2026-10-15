"""Read-only status dashboard.

A background poller reads every agent's /status (and the baseline
controller's, if running) once a second, caches it, and merges each agent's
new events into one timeline buffer. The dashboard has no write path to
anything.

  GET /                 page
  GET /api/channels     which pages (demo / live) this deployment has
  GET /api/state        aggregated view (JSON); every data endpoint takes ?channel=demo|live
  GET /api/events       merged event timeline; ?since=<gseq>&limit=<n>
  GET /api/agent/<id>   one agent's full view + its recent events
  GET /api/export       every buffered event from every agent, as a JSON download
  GET /api/metrics      per-incident metrics from every agent/controller
  GET /api/snapshot/<agent>/<key>   one evidence snapshot, as a JSON download
  GET  /api/sim/state   demo controls (SIMULATOR only; see docs/DASHBOARD.md)
  POST /api/sim/start   {"scenario": id}   both refused unless the dashboard is wired to the simulator
  POST /api/sim/reset                      AND every reachable agent reports data_source SIMULATOR

Two pages, never mixed (docs/DASHBOARD.md, docs/LIVE_DEMO.md): each channel has its own agents, collector, history and
incidents and accepts ONLY its own data_source (demo: SIMULATOR, live: LIVE_CLUSTER); only the demo channel can have simulator
controls.
  DEMO_STATUS_URLS=http://127.0.0.1:50251/status,... [SIM_CONTROL_URL=... SIM_CONTROL_TOKEN=...] \\
  LIVE_STATUS_URLS=http://127.0.0.1:18081/status,...  python3 dashboard/server.py       # presenter's laptop: both pages
  (nothing set: the in-cluster dashboard reads /etc/resilience/config.json and serves the Live page only)
Single source (compatibility, scripts/sim-dashboard.sh):
  STATUS_URLS=http://localhost:50251/status,... [BASELINE_URL=http://localhost:50261] python3 dashboard/server.py
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict, deque
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


# Data provenance (mirrors agent/resilience/provenance.py; the dashboard image does not contain the agent package,
# tests/test_provenance.py checks the two stay identical).
SIMULATOR, LIVE_CLUSTER, UNKNOWN, MIXED = "SIMULATOR", "LIVE_CLUSTER", "UNKNOWN", "MIXED"
VALID_SOURCES = (SIMULATOR, LIVE_CLUSTER)


def clean_source(*claims) -> str:
    """The provenance of one record. Each claim is what a producer stamped on it. Anything that is not exactly
    SIMULATOR or LIVE_CLUSTER is UNKNOWN, and claims that DISAGREE (a record stamped differently from the status it
    arrived in) are UNKNOWN too: never trust either one."""
    got = {c if c in VALID_SOURCES else UNKNOWN for c in claims if c is not None}
    return next(iter(got)) if len(got) == 1 else UNKNOWN


def aggregate_source(sources) -> str:
    s = {x if x in VALID_SOURCES else UNKNOWN for x in sources}
    if not s:
        return UNKNOWN
    return next(iter(s)) if len(s) == 1 else MIXED


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

    def __init__(self, channel: "Channel" = None):
        self.channel = channel
        self.lock = threading.Lock()
        self.rejected: dict = {}                  # source id -> the data_source it claimed, when that is not what this channel accepts
        self.dropped_events = 0                   # events / records refused because their source is not this channel's
        self.received: dict = {}                  # source id -> local time its latest status arrived (freshness, clock-skew free)
        self.status: dict = {}                    # source id -> latest status (events stripped)
        self.cursor: dict = {}                    # source id -> (boot, last seq)
        self.events: deque = deque(maxlen=BUFFER)
        # History for the incident view. The agents only keep short rings (last 15 decisions, 40 action records, 300
        # events), so the dashboard remembers what it has SEEN (bounded; lost when the dashboard restarts).
        self.decisions: "OrderedDict" = OrderedDict()   # (agent, action, target, epoch, stage) -> decision
        self.audit: "OrderedDict" = OrderedDict()       # (agent, record id) -> action record
        self.snaps: "OrderedDict" = OrderedDict()       # (agent, key) -> snapshot summary
        self.invs: "OrderedDict" = OrderedDict()        # (agent, investigation id) -> investigation row
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
        results = list(POOL.map(lambda kv: self._fetch(*kv), self.channel.sources.items()))
        with self.lock:
            for sid, res in results:
                if res is None:
                    self.status.pop(sid, None)
                    continue
                if not self._accepts(clean_source(res.get("data_source"))):
                    # THE SEPARATION RULE: a channel only ever holds data stamped with its own source. Anything else
                    # (simulated data on the live channel, live data on the demo channel, unknown, mixed) is discarded
                    # here, before it can reach any view, history, event list or incident.
                    self.status.pop(sid, None)
                    self.rejected[sid] = clean_source(res.get("data_source"))
                    continue
                self.rejected.pop(sid, None)
                self._scrub(res)
                self.received[sid] = time.time()
                evs = res.pop("events", []) or []
                boot = res.get("event_boot")
                last_boot, last_seq = self.cursor.get(sid, (None, None))
                if boot != last_boot:
                    last_seq = None
                for e in evs:
                    if last_seq is not None and e["seq"] <= last_seq:
                        continue
                    if not self._accepts(clean_source(e.get("data_source"), res.get("data_source"))):
                        self.dropped_events += 1
                        continue
                    self.gseq += 1
                    self.events.append({**e, "gseq": self.gseq, "source": sid,
                                        "data_source": clean_source(e.get("data_source"), res.get("data_source"))})
                if evs:
                    last_seq = max(e["seq"] for e in evs)
                self.cursor[sid] = (boot, last_seq if last_seq is not None else res.get("event_seq"))
                self.status[sid] = res
                self._remember(sid, res)
            self.last_poll = time.time()

    def _scrub(self, res: dict) -> None:
        """Remove, from the status this channel is about to keep, every stamped record that belongs to another source (a
        simulated decision inside a live agent's status, say). The status as a whole was already accepted by its own stamp."""
        for key in ("decisions", "snapshots"):
            rows = res.get(key)
            if isinstance(rows, list):
                keep = [r for r in rows if not isinstance(r, dict) or self._accepts(clean_source(r.get("data_source"), res.get("data_source")))]
                self.dropped_events += len(rows) - len(keep)
                res[key] = keep
        acts = res.get("actions")
        if isinstance(acts, dict) and isinstance(acts.get("recent"), list):
            keep = [r for r in acts["recent"] if not isinstance(r, dict) or self._accepts(clean_source(r.get("data_source"), res.get("data_source")))]
            self.dropped_events += len(acts["recent"]) - len(keep)
            acts["recent"] = keep
        for k, v in list((res.get("targets") or {}).items()):          # the 'last decision' shown on a node card
            ld = v.get("last_decision") if isinstance(v, dict) else None
            if isinstance(ld, dict) and not self._accepts(clean_source(ld.get("data_source"), res.get("data_source"))):
                v["last_decision"] = None
                self.dropped_events += 1

    def _accepts(self, source: str) -> bool:
        exp = self.channel.expect if self.channel else None
        return exp is None or source == exp

    @staticmethod
    def _keep(store: "OrderedDict", key, value, cap: int) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > cap:
            store.popitem(last=False)

    def _remember(self, sid: str, res: dict) -> None:
        ds = clean_source(res.get("data_source"))
        for d in res.get("decisions", []) or []:
            src = clean_source(d.get("data_source"), res.get("data_source"))
            if not self._accepts(src):
                self.dropped_events += 1
                continue
            self._keep(self.decisions, (sid, d["action"], d["target"], d["epoch"], d["stage"]), {**d, "node": sid, "data_source": src}, 1000)
        for r in ((res.get("actions") or {}).get("recent") or []):
            src = clean_source(r.get("data_source"), res.get("data_source"))
            if not self._accepts(src):
                self.dropped_events += 1
                continue
            self._keep(self.audit, (sid, r["id"]), {**r, "data_source": src}, 4000)
        for x in res.get("snapshots") or []:
            src = clean_source(x.get("data_source"), res.get("data_source"))
            if not self._accepts(src):
                self.dropped_events += 1
                continue
            self._keep(self.snaps, (sid, x["key"]), {**x, "agent": sid, "data_source": src}, 300)
        inv = res.get("investigations") or {}
        for r in list(inv.get("active", [])) + list(inv.get("recent", [])):
            self._keep(self.invs, (sid, r["id"]), {**r, "node": sid, "data_source": ds}, 600)

    def history(self) -> dict:
        with self.lock:
            return {"decisions": list(self.decisions.values()), "audit": list(self.audit.values()),
                    "snaps": list(self.snaps.values()), "invs": list(self.invs.values())}

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


# --------------------------------------------------------------------------- channels (Demo Mode / Live Monitoring)
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class Channel:
    """ONE data source with everything that belongs to it: its agents, its collector (events, history, incidents) and, for
    the demo channel only, the simulator controls. `expect` is the only data_source this channel accepts: SIMULATOR for
    the Demo page, LIVE_CLUSTER for the Live page (None = legacy single-source mode, see build_channels). Two channels
    share NOTHING, so simulated records can never reach the live page and demo controls can never reach live resources."""

    def __init__(self, name: str, expect, urls: dict, baseline: str = "", sim_url: str = "", sim_token: str = ""):
        self.name, self.expect = name, expect
        self.urls = dict(urls)
        self.baseline = (baseline or "").rstrip("/")
        self.sources = dict(self.urls, **({"CENTRAL": f"{self.baseline}/status"} if self.baseline else {}))
        live = expect == LIVE_CLUSTER
        self.sim_url = "" if live else (sim_url or "").rstrip("/")        # a live channel can never carry simulator controls
        self.sim_token = "" if live else (sim_token or "")
        self.collector = Collector(self)


def _urls(raw: str) -> dict:
    return {chr(ord("A") + i): u.strip() for i, u in enumerate((raw or "").split(",")) if u.strip()}


def build_channels(env=None):
    """(mode, {name: Channel}).
      strict : DEMO_STATUS_URLS and/or LIVE_STATUS_URLS (the presenter's laptop), or neither and the in-cluster config file,
               which yields the Live channel only. Each channel accepts only its own data_source.
      legacy : STATUS_URLS (one source, whatever it reports; the pre-existing behaviour, used by scripts/sim-dashboard.sh)."""
    env = os.environ if env is None else env
    demo, live = _urls(env.get("DEMO_STATUS_URLS")), _urls(env.get("LIVE_STATUS_URLS"))
    if env.get("STATUS_URLS") and (demo or live):
        raise SystemExit("set STATUS_URLS (one source) OR DEMO_STATUS_URLS / LIVE_STATUS_URLS (two pages), not both")
    if env.get("STATUS_URLS"):
        return "legacy", {"legacy": Channel("legacy", None, _urls(env["STATUS_URLS"]), env.get("BASELINE_URL", ""),
                                            env.get("SIM_CONTROL_URL", ""), env.get("SIM_CONTROL_TOKEN", ""))}
    if demo or live:
        chans = {}
        if demo:
            chans["demo"] = Channel("demo", SIMULATOR, demo, env.get("DEMO_BASELINE_URL", ""),
                                    env.get("SIM_CONTROL_URL", ""), env.get("SIM_CONTROL_TOKEN", ""))
        if live:
            chans["live"] = Channel("live", LIVE_CLUSTER, live, env.get("LIVE_BASELINE_URL", ""))
        return "strict", chans
    return "strict", {"live": Channel("live", LIVE_CLUSTER, agent_urls(), env.get("BASELINE_URL", ""))}


MODE, CHANNELS = build_channels()
DEFAULT = CHANNELS.get("demo") or CHANNELS.get("live") or CHANNELS["legacy"]
# Compatibility names for the default channel (the legacy single-source mode and the tests use these).
URLS, BASELINE, SOURCES, COLLECTOR = DEFAULT.urls, DEFAULT.baseline, DEFAULT.sources, DEFAULT.collector
SIM_CONTROL_URL, SIM_CONTROL_TOKEN = DEFAULT.sim_url, DEFAULT.sim_token
_ctx = threading.local()


def ch() -> Channel:
    """The channel the current request is about (set by the request handler); the default channel outside a request."""
    return getattr(_ctx, "channel", None) or DEFAULT


def resolve(name):
    """(Channel | None, reason). STRICT mode needs an explicit channel. LEGACY mode has one source: it answers as the Demo page
    only while its agents report SIMULATOR and as the Live page only while they report LIVE_CLUSTER, so it can never pose as
    the wrong one."""
    if MODE == "strict":
        if not name:
            return None, "a channel (demo or live) is required"
        c = CHANNELS.get(name)
        return (c, "ok") if c else (None, "Demo Mode is not available in this deployment" if name == "demo"
                                    else "Live Monitoring is not configured" if name == "live" else "unknown channel")
    legacy = CHANNELS["legacy"]
    if not name:
        return legacy, "ok"
    if name not in ("demo", "live"):
        return None, "unknown channel"
    want = SIMULATOR if name == "demo" else LIVE_CLUSTER
    st = legacy.collector.snapshot()
    if not st:
        legacy.collector.poll_once()
        st = legacy.collector.snapshot()
    got = aggregate_source(clean_source(s.get("data_source")) for s in st.values()) if st else UNKNOWN
    return (legacy, "ok") if got == want else (None, f"this dashboard is wired to one source ({got}), not {name}")


def channels_view() -> dict:
    out = {}
    for name in ("demo", "live"):
        c, why = resolve(name) if MODE == "legacy" else (CHANNELS.get(name), "")
        out[name] = {"configured": c is not None,
                     "reason": "" if c is not None else (why or ("Demo Mode is not available in this deployment (it runs on the presenter's laptop)"
                                                                  if name == "demo" else "Live Monitoring is not configured"))}
    default = "demo" if out["demo"]["configured"] else "live"
    return {"mode": MODE, "default": default, "channels": out}




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
    statuses = ch().collector.snapshot()
    if not statuses and not ch().collector.last_poll:     # poller not warmed up yet
        ch().collector.poll_once()
        statuses = ch().collector.snapshot()
    ids = list(ch().urls)
    live = {k: v for k, v in statuses.items() if k in ch().urls}
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
            ds = clean_source(d.get("data_source"), s.get("data_source"))
            k = (d["action"], d["target"], d["epoch"], d["stage"], ds)      # never merge records of different sources
            m = merged.setdefault(k, {**d, "seen_by": [], "data_source": ds})
            m["seen_by"].append(sid)
            if not m.get("justification") and d.get("justification"):
                m["justification"] = d["justification"]
    uniq = sorted(merged.values(), key=lambda d: d["t"], reverse=True)
    rejections = sorted(({**r, "seen_by": p} for p, s in live.items() for r in s.get("rejections", [])),
                        key=lambda r: r["t"], reverse=True)[:10]
    keys = sorted({k for s in live.values() for k in (s.get("pending_votes") or {})})
    pending = {k: _vote_view(k, live, ids) for k in keys}
    per_source = {sid: clean_source(s.get("data_source")) for sid, s in statuses.items()}
    return {"mode": "centralized" if base and not live else "distributed",
            "data_source": aggregate_source(per_source.values()), "data_sources": per_source,
            # The baseline controller is "running" only if it ANSWERS, not merely because a URL is configured
            # (the live dashboard manifest always configures one, even when no controller is deployed).
            "baseline_up": base is not None,
            "nodes": nodes, "decisions": uniq[:15], "rejections": rejections,
            "investigations": investigations_view(live),
            "incidents": incident_list(statuses),
            "summary": summary_view(nodes, live, ids, investigations_view(live)),
            "decision_logs": decision_logs_view(live),
            "snapshots": snapshots_view(live), "actions": actions_view(live),
            "pending_votes": {k: v["voters"] for k, v in pending.items()},
            "pending_detail": pending, "baseline": ch().baseline or None,
            "central_compromised": (base or {}).get("compromised"),
            "last_poll": ch().collector.last_poll, "gseq": ch().collector.gseq,
            "channel": ch().name, "configured": True, "connection": connection_view(statuses)}


def connection_view(statuses: dict) -> dict:
    """What the page needs to say honestly whether it is connected: agent availability, telemetry freshness and cluster identity
    as REPORTED by the agents (never filled in by the dashboard). `connected` is true only while at least one agent of THIS
    channel answers with this channel's data source."""
    c = ch()
    up = [k for k in c.urls if k in statuses]
    now = time.time()
    tel = []
    for k in up:
        t_ = statuses[k].get("telemetry") or {}
        if t_.get("age_s") is not None:
            # the agent's own age of its last workload poll, plus how long ago the dashboard received that report
            # (both are durations, so no clock needs to agree between the cluster and this machine)
            tel.append(round(t_["age_s"] + max(0.0, now - c.collector.received.get(k, now)), 1))
    clusters = [(statuses[k].get("cluster") or {}) for k in up]
    names = {x.get("name") for x in clusters if x.get("name")}
    vers = {x.get("kubernetes_version") for x in clusters if x.get("kubernetes_version")}
    state = "NOT_CONNECTED" if not up else "CONNECTED" if len(up) == len(c.urls) else "DEGRADED"
    return {"channel": c.name, "expect": c.expect, "state": state, "connected": bool(up),
            "agents_total": len(c.urls), "agents_up": len(up), "agents_down": [k for k in c.urls if k not in statuses],
            "rejected_sources": dict(c.collector.rejected), "dropped_records": c.collector.dropped_events,
            "telemetry_age_s": max(tel) if tel else None, "telemetry_reporting": len(tel),
            "cluster": {"name": next(iter(names)) if len(names) == 1 else ("agents disagree" if names else ""),
                        "kubernetes_version": next(iter(vers)) if len(vers) == 1 else ("agents disagree" if vers else ""),
                        "platform": next((x.get("platform") for x in clusters if x.get("platform")), "")}}


def not_configured_state(name, why: str) -> dict:
    return {"channel": name, "configured": False, "connection": {"channel": name, "state": "NOT_CONFIGURED", "connected": False,
                                                               "reason": why, "agents_total": 0, "agents_up": 0, "agents_down": [],
                                                               "rejected_sources": {}, "dropped_records": 0}}


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
    if sid not in ch().urls or not SNAP_KEY.match(key):
        return None
    snap = get(ch().urls[sid].replace("/status", f"/snapshot/{key}"))
    exp = ch().expect
    if exp and isinstance(snap, dict) and clean_source(snap.get("data_source")) != exp:
        return None                                  # a snapshot of another source is never served through this channel
    return snap


def summary_view(nodes: dict, live: dict, ids: list, inv: dict) -> dict:
    """The Overview counters. Every number is derived from what the agents themselves report; nothing is estimated.

    workloads      healthy = phase HEALTHY and not flagged for a human; anything else is unhealthy ("unknown" when no agent
                   reports on it)
    incidents      workloads whose incident is not over (phase other than HEALTHY)
    investigations rows still ACTIVE, plus workloads under watch that need human review
    recoveries     counted by the agents since they started (the largest count among agents, so one restarted agent does
                   not hide history): succeeded = confirmed HEALTHY again after an incident; failed attempts = replacement
                   pods that failed validation (each retry); abandoned = incidents that now need a human
    agents         connected = answered the dashboard just now; unreachable = configured but silent"""
    unhealthy = [n for n, v in nodes.items() if v["phase"] != "HEALTHY" or v.get("display_state") == "NEEDS_ATTENTION"]
    unknown = [n for n, v in nodes.items() if v["phase"] == "UNKNOWN"]
    active_inv = [r["target"] for r in (inv or {}).get("rows", []) if r.get("active")]
    review = [w["target"] for w in (inv or {}).get("watch", []) if w.get("review_needed")]
    ctr = [s.get("counters") or {} for s in live.values()]
    top = lambda k: max([c.get(k, 0) for c in ctr] or [0])             # noqa: E731
    connected = [i for i in ids if i in live]
    return {"workloads_total": len(nodes), "workloads_healthy": len(nodes) - len(unhealthy), "workloads_unhealthy": len(unhealthy),
            "unhealthy": unhealthy, "workloads_unknown": unknown,
            "incidents_active": [n for n, v in nodes.items() if v["phase"] not in ("HEALTHY", "UNKNOWN")],
            "needs_attention": [n for n, v in nodes.items() if v.get("display_state") == "NEEDS_ATTENTION"],
            "investigations_active": len(active_inv), "investigations_review": review,
            "recoveries_succeeded": top("recoveries_succeeded"), "recovery_attempts_failed": top("recovery_attempts_failed"),
            "recoveries_abandoned": top("recoveries_abandoned"), "incidents_opened": top("incidents_opened"),
            "agents_total": len(ids), "agents_connected": len(connected), "agents_unreachable": [i for i in ids if i not in live],
            "counters_reported_by": [sid for sid, s in live.items() if s.get("counters")]}


# --------------------------------------------------------------------------- evaluation tab (Part 4)
# Shows the committed evaluation (results/ graphs, docs/RESULTS.md text and tables) exactly as written. Nothing is
# recomputed or rephrased here, so the tab cannot say more than the evaluation does. It is ALWAYS labelled simulator
# results (the evaluation only ever ran in the simulator).
FIG_NAME = __import__("re").compile(r"^[A-Za-z0-9_]{1,60}\.png$")


def eval_locations() -> dict:
    """Where the evaluation files are: $EVAL_DIR, the image's ./evaluation, or the repository layout (results/ + docs/)."""
    cands = []
    if os.environ.get("EVAL_DIR"):
        d = os.environ["EVAL_DIR"]
        cands.append({"figs": d, "md": os.path.join(d, "RESULTS.md"), "summary": os.path.join(d, "summary.md")})
    d = os.path.join(HERE, "evaluation")
    cands.append({"figs": d, "md": os.path.join(d, "RESULTS.md"), "summary": os.path.join(d, "summary.md")})
    root = os.path.dirname(HERE)
    cands.append({"figs": os.path.join(root, "results"), "md": os.path.join(root, "docs", "RESULTS.md"),
                  "summary": os.path.join(root, "results", "summary.md")})
    for c in cands:
        if os.path.isfile(c["md"]):
            return c
    return {}


def parse_markdown_blocks(lines: list) -> list:
    """The small Markdown subset docs/RESULTS.md uses -> ordered blocks (paragraph, bullets, numbered, table, image, code, quote)."""
    out, i, n = [], 0, len(lines)
    img = __import__("re").compile(r"^!\[(.*?)\]\((.*?)\)\s*$")
    while i < n:
        ln = lines[i]
        if not ln.strip() or ln.strip().startswith("<!--"):
            i += 1
            continue
        if ln.startswith("```"):
            j = i + 1
            while j < n and not lines[j].startswith("```"):
                j += 1
            out.append({"type": "code", "text": "\n".join(lines[i + 1:j])})
            i = j + 1
            continue
        m = img.match(ln.strip())
        if m:
            out.append({"type": "image", "alt": m.group(1), "name": os.path.basename(m.group(2))})
            i += 1
            continue
        if ln.lstrip().startswith("|"):
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):          # skip the |---|---| separator row
                    rows.append(cells)
                i += 1
            if rows:
                out.append({"type": "table", "head": rows[0], "rows": rows[1:]})
            continue
        if ln.startswith(">"):
            q = []
            while i < n and lines[i].startswith(">"):
                q.append(lines[i].lstrip("> ").rstrip())
                i += 1
            out.append({"type": "quote", "text": " ".join(x for x in q if x)})
            continue
        bullet = __import__("re").match(r"^(\*|-) (.*)$", ln)
        number = __import__("re").match(r"^\d+\. (.*)$", ln)
        if bullet or number:
            items, kind = [], ("bullets" if bullet else "numbered")
            rx = __import__("re").compile(r"^(\*|-) (.*)$" if bullet else r"^\d+\. (.*)$")
            while i < n and rx.match(lines[i]):
                item = rx.match(lines[i]).groups()[-1]
                i += 1
                while i < n and lines[i].startswith("  ") and lines[i].strip() and not rx.match(lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(item)
            out.append({"type": kind, "items": items})
            continue
        para = []
        while i < n and lines[i].strip() and not lines[i].startswith(("```", "|", ">", "![")) \
                and not __import__("re").match(r"^(\*|-) |^\d+\. ", lines[i]) and not lines[i].strip().startswith("<!--"):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append({"type": "p", "text": " ".join(para)})
        else:
            i += 1
    return out


def parse_results_md(text: str) -> dict:
    re = __import__("re")
    title, intro, sections, cur, sub = "", [], [], None, None
    buf: list = []

    def flush():
        nonlocal buf
        blocks = parse_markdown_blocks(buf)
        if sub is not None:
            sub["blocks"] = blocks
        elif cur is not None:
            cur["blocks"] = blocks
        else:
            intro.extend(blocks)
        buf = []
    for ln in text.split("\n"):
        if ln.startswith("# ") and not title:
            title = ln[2:].strip()
        elif ln.startswith("## "):
            flush()
            m = re.match(r"^## (\d+)\.\s*(.*)$", ln)
            cur = {"id": m.group(1) if m else "", "title": (m.group(2) if m else ln[3:]).strip(), "blocks": [], "subsections": []}
            sub = None
            sections.append(cur)
        elif ln.startswith("### ") and cur is not None:
            flush()
            m = re.match(r"^### ([\d.]+)\s*(.*)$", ln)
            sub = {"id": m.group(1) if m else "", "title": (m.group(2) if m else ln[4:]).strip(), "blocks": []}
            cur["subsections"].append(sub)
        else:
            buf.append(ln)
    flush()
    return {"title": title, "intro": intro, "sections": sections}


_EVAL_CACHE: dict = {}


def evaluation_view() -> dict:
    loc = eval_locations()
    note = ("SIMULATOR RESULTS. Measured in the simulator (simulated telemetry and Kubernetes API; real agents, quorum and certificates). "
            "Nothing here was measured on a real cluster.")
    if not loc:
        return {"available": False, "data_source": SIMULATOR, "notice": note,
                "message": "The evaluation results are not included in this deployment. Generate them with `python3 scripts/evaluate.py` "
                           "(docs/RESULTS.md and results/), then rebuild the dashboard image."}
    try:
        mt = os.path.getmtime(loc["md"])
        key = (loc["md"], mt)
        if _EVAL_CACHE.get("key") != key:
            with open(loc["md"], encoding="utf-8") as fh:
                doc = parse_results_md(fh.read())
            figs = sorted(f for f in os.listdir(loc["figs"]) if FIG_NAME.match(f)) if os.path.isdir(loc["figs"]) else []
            meta = {"generated_at": mt, "figures": figs, "trials": None, "errors": None, "summary_file": os.path.isfile(loc["summary"])}
            if meta["summary_file"]:
                with open(loc["summary"], encoding="utf-8") as fh:
                    st = fh.read()
                tot = 0
                for ln in st.split("\n"):
                    cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                    if ln.startswith("|") and len(cells) == 5 and cells[2].isdigit() and cells[3].isdigit():
                        tot += int(cells[2])
                meta["trials"] = tot or None
                m = __import__("re").search(r"(All (\d+) trials produced a result \(0 errors\)|(\d+) of (\d+) trials errored)", st)
                meta["errors_text"] = m.group(1) if m else None
            _EVAL_CACHE.update(key=key, doc=doc, meta=meta)
        return {"available": True, "data_source": SIMULATOR, "notice": note, **_EVAL_CACHE["doc"], "meta": _EVAL_CACHE["meta"]}
    except (OSError, ValueError) as exc:
        return {"available": False, "data_source": SIMULATOR, "notice": note, "message": f"The evaluation files could not be read: {exc.__class__.__name__}"}


def eval_figure(name: str):
    """(bytes) of one PNG from the evaluation figure directory, or None. Only plain *.png names that exist there."""
    loc = eval_locations()
    if not loc or not FIG_NAME.match(name or ""):
        return None
    path = os.path.join(loc["figs"], name)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(loc["figs"]) or not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- incident detail (Part 3)
# An INCIDENT is one containment of one workload: (data source, target, epoch >= 1). Everything below is assembled from what
# the agents reported (decisions, action-audit records, events, investigations, snapshots), never invented.
# Four things are kept apart for every action, because they are different claims:
#   PROPOSED    agents signed votes for it
#   AUTHORIZED  a 3-of-4 quorum committed it (recorded in the decision log)
#   ATTEMPTED   an executor actually called the Kubernetes API for it
#   VERIFIED    the cluster was READ afterwards and shows the effect
VERIFIED_OUTCOMES = {"applied", "already_applied", "applied_after_timeout", "unknown_resolved"}
ATTEMPTED_OUTCOMES = {"applied", "applied_unconfirmed", "applied_after_timeout", "failed", "not_applied", "unknown",
                      "unknown_resolved", "abandoned"}
PRE_WINDOW_S = 120.0                      # events up to this long before containment belong to the incident


def parse_task(task: str):
    """audit task id -> (proposal key, step label), or None for tasks that are not part of a proposal."""
    p = str(task).split(":")
    try:
        if p[0] == "isolate" and len(p) == 3:
            return f"CONTAIN:{p[1]}:{p[2]}:", "isolate (quarantine policy)"
        if p[0] == "recover" and len(p) == 4:
            n = int(p[3])
            return (f"CONTAIN:{p[1]}:{p[2]}:" if n <= 1 else f"RETRY_RECOVERY:{p[1]}:{p[2]}:{n}"), f"redeploy from the known-good image (attempt {n})"
        if p[0] == "validated" and len(p) == 3:
            return f"VALIDATE:{p[1]}:{p[2]}:", "record validation in the cluster"
        if p[0] == "stage" and len(p) == 4:
            return f"ADVANCE_STAGE:{p[1]}:{p[2]}:{p[3]}", f"move to {p[3]}"
    except ValueError:
        return None
    return None


def proposal_text(key: str) -> str:
    a, tg, ep, st = (str(key).split(":") + ["", "", "", ""])[:4]
    return {"CONTAIN": f"contain {tg} (isolate and redeploy)", "VALIDATE": f"accept the recovered {tg} as validated",
            "ADVANCE_STAGE": f"move {tg} to {st}", "RETRY_RECOVERY": f"redeploy {tg} again (attempt {st})"}.get(a, key)


def _incidents_from(hist: dict) -> dict:
    """(source, target, epoch) -> {"decisions": [...]} for every CONTAIN decision seen."""
    inc: dict = {}
    for d in hist["decisions"]:
        if d["action"] == "CONTAIN" and d["epoch"] >= 1:
            k = (clean_source(d.get("data_source")), d["target"], d["epoch"])
            inc.setdefault(k, []).append(d)
    return inc


def _current(statuses: dict, source: str, target: str, epoch: int) -> dict:
    per, cur = {}, {}
    for sid, s in statuses.items():
        if sid not in ch().urls or clean_source(s.get("data_source")) != source:
            continue
        v = (s.get("targets") or {}).get(target)
        if v:
            per[sid] = {"phase": v.get("phase"), "stage": v.get("stage"), "epoch": v.get("epoch"), "attempt": v.get("attempt"),
                        "attention": v.get("attention"), "next_step": v.get("next_step"), "workload_trust": v.get("workload_trust")}
    mine = {k: v for k, v in per.items() if v["epoch"] == epoch}
    if mine:
        ph = Counter(v["phase"] for v in mine.values()).most_common(1)[0][0]
        cur = {"phase": ph, "stage": Counter(v["stage"] for v in mine.values()).most_common(1)[0][0],
               "attempt": max((v["attempt"] or 1) for v in mine.values()),
               "attention": next((v["attention"] for v in mine.values() if v["attention"]), None)}
        blockers = Counter((v["next_step"] or {}).get("blocked_by") for v in mine.values() if v.get("next_step"))
        steps = Counter((v["next_step"] or {}).get("step") for v in mine.values() if v.get("next_step"))
        cur["next_step"] = ({"step": steps.most_common(1)[0][0], "blocked_by": blockers.most_common(1)[0][0],
                             "agents_agreeing": blockers.most_common(1)[0][1], "agents_reporting": sum(blockers.values())}
                            if steps else None)
    return {"per_agent": per, "consensus": cur, "newer_epoch_exists": any((v["epoch"] or 0) > epoch for v in per.values())}


def incident_status(decs_all: list, source: str, target: str, epoch: int, current: dict) -> str:
    full = any(d["action"] == "ADVANCE_STAGE" and d["stage"] == "FULL" and d["epoch"] == epoch and d["target"] == target
               and clean_source(d.get("data_source")) == source for d in decs_all)
    c = current.get("consensus") or {}
    if c.get("attention"):
        return "NEEDS_HUMAN"
    if c:
        return "RESOLVED" if (c.get("phase") == "HEALTHY") else "ACTIVE"
    if full or current.get("newer_epoch_exists"):
        return "RESOLVED" if full else "SUPERSEDED"
    return "UNKNOWN"


def incident_list(statuses: dict = None, hist: dict = None) -> list:
    statuses = ch().collector.snapshot() if statuses is None else statuses
    hist = ch().collector.history() if hist is None else hist
    out = []
    for (src, tgt, ep), decs in _incidents_from(hist).items():
        cur = _current(statuses, src, tgt, ep)
        first = min(decs, key=lambda d: d["t"])
        cons = cur["consensus"]
        out.append({"id": f"{src}:{tgt}:{ep}", "source": src, "target": tgt, "epoch": ep, "contained_at": first["t"],
                    "signers": sorted(set(first.get("voters", []))), "status": incident_status(hist["decisions"], src, tgt, ep, cur),
                    "phase": cons.get("phase"), "stage": cons.get("stage"), "attempt": cons.get("attempt"),
                    "workload": next((v.get("workload") for s in statuses.values() for v in [(s.get("targets") or {}).get(tgt)] if v), None)})
    return sorted(out, key=lambda i: i["contained_at"], reverse=True)[:50]


def build_incident(source: str, target: str, epoch: int, statuses: dict = None, hist: dict = None, events: list = None):
    statuses = ch().collector.snapshot() if statuses is None else statuses
    hist = ch().collector.history() if hist is None else hist
    events = ch().collector.all_events() if events is None else events
    incs = _incidents_from(hist)
    decs = incs.get((source, target, epoch))
    if not decs:
        return None
    contained_at = min(d["t"] for d in decs)
    later = [d["t"] for (s_, t_, e_), ds in incs.items() if s_ == source and t_ == target and e_ > epoch for d in ds]
    earlier = [d["t"] for (s_, t_, e_), ds in incs.items() if s_ == source and t_ == target and e_ < epoch for d in ds]
    t_end = min(later) if later else float("inf")
    t_from = max(contained_at - PRE_WINDOW_S, max(earlier) if earlier else 0.0)
    ev = [e for e in events if clean_source(e.get("data_source")) == source and t_from <= e["ts"] < t_end]
    cur = _current(statuses, source, target, epoch)
    status = incident_status(hist["decisions"], source, target, epoch, cur)
    workload = next((v.get("workload") for s in statuses.values() for v in [(s.get("targets") or {}).get(target)] if v), target)

    # ---- trigger
    first = min(decs, key=lambda d: d["t"])
    just = next((d["justification"] for d in sorted(decs, key=lambda d: d["t"]) if d.get("justification")), {}) or {}
    by_agent: dict = {}
    for e in just.get("evidence", []) or []:                    # the same observation is re-sent every tick: show it once, with a count
        rows = by_agent.setdefault(e.get("origin", "?"), [])
        same = next((r for r in rows if r["type"] == e.get("type") and r["summary"] == e.get("summary")), None)
        if same:
            same["count"] += 1
            same["confidence"] = max(same["confidence"] or 0, e.get("confidence") or 0)
            same["last_timestamp"] = e.get("timestamp")
        else:
            rows.append({"type": e.get("type"), "confidence": e.get("confidence"), "summary": e.get("summary"),
                         "timestamp": e.get("timestamp"), "last_timestamp": e.get("timestamp"), "count": 1})
    trigger = {"decision": {k: first.get(k) for k in ("t", "action", "epoch", "voters", "note")}, "score": just.get("score"),
               "evidence_by_agent": by_agent, "seen_by": sorted({d["node"] for d in decs}),
               "text": f"Agents {', '.join(first.get('voters', []))} signed votes to contain {workload} ({target}); "
                       "the evidence they cited is listed per agent."}
    trig_inv = sorted({v.get("investigation_id") for d in decs for v in (d.get("justification", {}).get("votes") or []) if v.get("investigation_id")})
    if trig_inv:
        trigger["investigation_ids"] = trig_inv

    # ---- raw event sections
    of = lambda cat: [e for e in ev if e["category"] == cat]               # noqa: E731
    mine = lambda e: e.get("target") == target                              # noqa: E731
    flag = lambda e: (e.get("details") or {}).get("flag")                   # noqa: E731
    obs_ = [e for e in of("OBSERVE") if mine(e)]
    sent = [e for e in of("EVIDENCE_SENT") if mine(e)]
    recv = [e for e in of("EVIDENCE_RECEIVED") if mine(e)]
    trust = [e for e in ev if e["category"] in ("TRUST_CHANGE", "FLAG") and flag(e) != "EVIDENCE_REJECTED"]
    rejected = of("REJECTED") + [e for e in of("FLAG") if flag(e) == "EVIDENCE_REJECTED"]
    slim = lambda e: {k: e.get(k) for k in ("ts", "node", "category", "target", "summary", "details")}   # noqa: E731
    sections = {
        "observations": [slim(e) for e in obs_][-60:],
        "evidence_sent": [slim(e) for e in sent][-60:], "evidence_received": [slim(e) for e in recv][-60:],
        "trust_changes": [{**slim(e), "mentions_target": target in (e.get("summary") or "") or (e.get("details") or {}).get("about") == target}
                          for e in trust][-80:],
        "rejected": [slim(e) for e in rejected][-60:]}

    # ---- investigations of this workload around the incident
    invs, seen = [], set()
    for r in hist["invs"]:
        if r["target"] == target and clean_source(r.get("data_source")) == source and t_from <= r["started_at"] < t_end and r["id"] not in seen:
            seen.add(r["id"])
            agents = {x["node"]: {"state": x["state"], "outcome": x["outcome"], "reason": x["reason"]} for x in hist["invs"] if x["id"] == r["id"]}
            invs.append({"id": r["id"], "question": r["question"], "trigger": r["trigger"], "started_at": r["started_at"], "initiator": r["initiator"],
                         "per_agent": agents, "outcomes": dict(Counter(a["outcome"] for a in agents.values() if a["outcome"]))})

    # ---- proposals: votes, who signed, who did not and why
    keys = {f"{d['action']}:{d['target']}:{d['epoch']}:{d['stage']}" for d in decs_for(hist, source, target, epoch)}
    for e in ev:
        pk = (e.get("details") or {}).get("proposal")
        if pk and e["category"] in ("VOTE_CAST", "VOTE_WITHHELD") and pk.split(":")[1:3] == [target, str(epoch)]:
            keys.add(pk)
    for r in hist["audit"]:
        pt = parse_task(r["task"])
        if pt and r["target"] == target and r["epoch"] == epoch and clean_source(r.get("data_source")) == source:
            keys.add(pt[0])
    proposals = {}
    for k in sorted(keys, key=lambda k: (decs_t(hist, source, k), k)):
        ds = [d for d in decs_for(hist, source, target, epoch) if f"{d['action']}:{d['target']}:{d['epoch']}:{d['stage']}" == k]
        dec = min(ds, key=lambda d: d["t"]) if ds else None
        votes = {}
        for d in ds:
            for v in (d.get("justification", {}).get("votes") or []):
                votes.setdefault(v["voter"], {"voter": v["voter"], "score": v.get("score"), "ts": v.get("ts")})
        casts = [e for e in ev if e["category"] == "VOTE_CAST" and (e.get("details") or {}).get("proposal") == k]
        for e in casts:
            votes.setdefault(e["node"], {"voter": e["node"], "score": (e.get("details") or {}).get("score"), "ts": e["ts"]})
        withheld = {}
        for e in ev:
            if e["category"] == "VOTE_WITHHELD" and (e.get("details") or {}).get("proposal") == k:
                withheld[e["node"]] = {"agent": e["node"], "reason": (e.get("details") or {}).get("reason") or e["summary"], "ts": e["ts"]}
        proposals[k] = {"proposal": k, "text": proposal_text(k), "committed": bool(dec), "committed_at": dec["t"] if dec else None,
                        "signers": sorted(dec.get("voters", [])) if dec else [], "note": dec.get("note") if dec else None,
                        "votes": sorted(votes.values(), key=lambda v: v.get("ts") or 0),
                        "withheld": [w for a, w in sorted(withheld.items()) if a not in votes], "quorum_needed": 3}

    # ---- the four-stage lifecycle of every action
    rows = {}
    for r in sorted(hist["audit"], key=lambda r: r["t"]):
        if r["target"] != target or r["epoch"] != epoch or clean_source(r.get("data_source")) != source:
            continue
        pt = parse_task(r["task"])
        if pt is None:
            if r["action"] == "needs_human_attention":
                rows.setdefault(("", "escalate to a human"), []).append(r)
            continue
        rows.setdefault(pt, []).append(r)
    actions = []
    for (pk, step), recs in rows.items():
        prop = proposals.get(pk)
        attempted = [r for r in recs if r["outcome"] in ATTEMPTED_OUTCOMES]
        verified = [r for r in recs if r["outcome"] in VERIFIED_OUTCOMES]
        unresolved = [r for r in recs if r["outcome"] == "unknown"] and not verified
        refused = [r for r in recs if r["outcome"].startswith("refused")]
        actions.append({
            "proposal": pk, "step": step,
            "proposed": {"state": "yes" if prop and prop["votes"] else ("n/a" if not pk else "no"),
                         "by": [v["voter"] for v in (prop or {}).get("votes", [])]},
            "authorized": {"state": "yes" if prop and prop["committed"] else ("n/a" if not pk else "no"),
                           "signers": (prop or {}).get("signers", []), "at": (prop or {}).get("committed_at")},
            "attempted": ({"state": "n/a", "by": sorted({r["node"] for r in recs}), "tries": 0} if not pk else
                          {"state": "yes" if attempted else "no", "by": sorted({r["node"] for r in attempted}), "tries": len(attempted)}),
            "verified": ({"state": "n/a", "by": [], "at": None} if not pk else
                         {"state": "yes" if verified else ("unknown" if unresolved else "no"),
                          "by": sorted({r["node"] for r in verified}), "at": min((r["t"] for r in verified), default=None)}),
            "kind": "escalation" if not pk else "cluster-action",     # an escalation is a record that a human is needed, not a Kubernetes call
            "refused": [{"node": r["node"], "outcome": r["outcome"], "detail": r["detail"]} for r in refused],
            "records": [{k: r.get(k) for k in ("node", "t", "outcome", "attempt", "detail", "error", "duration_s")} for r in recs][-12:]})
    actions.sort(key=lambda a: (min((x["t"] for x in a["records"]), default=0), a["step"]))

    # ---- recovery attempts and the evidence saved before each replacement
    snaps = [x for x in hist["snaps"] if x["target"] == target and x["epoch"] == epoch and clean_source(x.get("data_source")) == source]
    attempts = {}
    for a in actions:
        if a["step"].startswith("redeploy"):
            n = int(a["step"].split("attempt ")[1].rstrip(")"))
            attempts[n] = {"attempt": n, "proposal": a["proposal"], "action": a, "snapshots": [], "retry_reason": None}
    for x in snaps:
        tag = x["tag"]
        n = 1 if tag == "contain" else int(tag.replace("failed", "")) + 1
        attempts.setdefault(n, {"attempt": n, "proposal": None, "action": None, "snapshots": [], "retry_reason": None})["snapshots"].append(x)
    for n, at in attempts.items():
        if n >= 2:
            at["retry_reason"] = next((x["reason"] for x in snaps if x["tag"] == f"failed{n - 1}"), None)
    return {"id": f"{source}:{target}:{epoch}", "source": source, "target": target, "epoch": epoch, "workload": workload, "status": status,
            "window": {"from": t_from, "contained_at": contained_at, "to": None if t_end == float("inf") else t_end},
            "trigger": trigger, **sections, "investigations": invs, "proposals": list(proposals.values()), "actions": actions,
            "recovery": {"attempts": [attempts[k] for k in sorted(attempts)], "snapshots": snaps},
            "current": cur, "limits": "Built from what the dashboard has seen since it started polling; events older than the agents' "
                                      "rings at that moment are not available."}


def decs_for(hist: dict, source: str, target: str, epoch: int) -> list:
    return [d for d in hist["decisions"] if d["target"] == target and d["epoch"] == epoch and clean_source(d.get("data_source")) == source]


def decs_t(hist: dict, source: str, key: str) -> float:
    a, tg, ep, st = (key.split(":") + ["", "", "", ""])[:4]
    ts = [d["t"] for d in hist["decisions"] if (d["action"], d["target"], str(d["epoch"]), d["stage"]) == (a, tg, ep, st)
          and clean_source(d.get("data_source")) == source]
    return min(ts) if ts else float("inf")



def agent_view(sid: str) -> dict:
    s = ch().collector.snapshot().get(sid)
    evs = [e for e in ch().collector.all_events() if e["node"] == sid][-150:]
    return {"id": sid, "up": s is not None, "status": s, "events": evs}


# --------------------------------------------------------------------------- simulator demo controls
# SAFETY MODEL (docs/DASHBOARD.md). These endpoints can only ever talk to the simulator's own control server:
#   0. Only the DEMO channel can have controls at all: a live channel is built without them (Channel.__init__), whatever
#      the environment says, and sim_gate refuses it again.
#   1. SIM_CONTROL_URL + SIM_CONTROL_TOKEN must be configured. The live Kubernetes manifest sets neither.
#   2. The control URL must be a loopback address, so it can never point at anything on a cluster network.
#   3. EVERY reachable agent must report data_source == SIMULATOR right now. LIVE_CLUSTER, UNKNOWN, MIXED or "no data"
#      all refuse, and the request is never forwarded.
#   4. Only POST with JSON, a custom header (blocks cross-site form posts) and the dashboard's own login.
#   5. The request can only name a scenario from the controller's own list; nothing else is forwarded.
# The simulator control server (sim/demo_server.py) re-checks its own invariants independently.


def sim_gate(statuses: dict = None):
    """(allowed, reason). Pure function of configuration and what the agents themselves report."""
    if ch().expect == LIVE_CLUSTER:
        return False, "refused: Live Monitoring has no scenario controls"
    if not ch().sim_url or not ch().sim_token:
        return False, "simulator controls are not configured on this dashboard (live mode)"
    host = urlparse(ch().sim_url).hostname
    if host not in LOOPBACK:
        return False, "refused: the simulator control address is not a loopback address"
    st = ch().collector.snapshot() if statuses is None else statuses
    live = {k: v for k, v in st.items() if k in ch().urls or k == "CENTRAL"}
    if not live:
        return False, "refused: no agent is reachable, so the data source cannot be established"
    src = aggregate_source(clean_source(s.get("data_source")) for s in live.values())
    if src != SIMULATOR:
        return False, f"refused: the data source is {src}, not SIMULATOR"
    return True, "ok"


def sim_call(method: str, path: str, body: dict = None, timeout: float = 5.0):
    req = urllib.request.Request(f"{ch().sim_url}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-CR-Sim-Token": ch().sim_token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {"error": "simulator control server refused the request"}
    except Exception as exc:
        return 502, {"error": f"simulator control server unreachable: {exc.__class__.__name__}"}


def sim_state() -> dict:
    ok, why = sim_gate()
    if not ok:
        return {"enabled": False, "reason": why, "scenarios": [], "running": None, "last": None}
    code, body = sim_call("GET", "/state")
    if code != 200:
        return {"enabled": False, "reason": body.get("error", "control server error"), "scenarios": [],
                "running": None, "last": None}
    return {"enabled": True, "reason": "ok", **body}


def sim_action(kind: str, payload: dict):
    """Returns (http_status, body). Never forwards unless sim_gate() passes at this very moment."""
    ok, why = sim_gate()
    if not ok:
        return 403, {"error": why}
    if kind == "reset":
        return sim_call("POST", "/reset", {})
    code, body = sim_call("GET", "/state")
    if code != 200:
        return code, body
    sid = payload.get("scenario")
    if not isinstance(sid, str) or sid not in {s["id"] for s in body.get("scenarios", [])}:
        return 400, {"error": "unknown scenario"}
    return sim_call("POST", "/start", {"scenario": sid})


def metrics() -> list:
    urls = [u.replace("/status", "/metrics") for u in ch().urls.values()]
    if ch().baseline:
        urls.append(f"{ch().baseline}/metrics")
    rows = []
    exp = ch().expect
    for res in POOL.map(get, urls):
        for r in (res if isinstance(res, list) else []):
            src = clean_source(r.get("data_source")) if isinstance(r, dict) else UNKNOWN
            if not isinstance(r, dict) or (exp and src != exp):         # this channel shows only its own source
                continue
            rows.append({**r, "data_source": src})
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

    def _sim_post(self, kind: str, channel=None):
        if not AUTH.request_ok(self.headers):
            return self._send(401, b'{"error":"authentication required"}', "application/json")
        if self.headers.get("X-CR-Control") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(400, b'{"error":"missing X-CR-Control header or JSON content type"}', "application/json")
        host, origin = self.headers.get("Host", ""), self.headers.get("Origin")
        if origin and urlparse(origin).netloc != host:
            return self._send(403, b'{"error":"cross-origin request refused"}', "application/json")
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 1024)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return self._send(400, b'{"error":"bad JSON body"}', "application/json")
        chn, why = resolve(channel)
        if chn is None:
            return self._send(400 if MODE == "strict" and not channel else 403, json.dumps({"error": f"refused: {why}"}).encode(), "application/json")
        _ctx.channel = chn
        code, body = sim_action(kind, payload)
        return self._send(code, json.dumps(body, default=str).encode(), "application/json")

    def do_POST(self):
        try:
            return self._post()
        finally:
            _ctx.channel = None

    def do_GET(self):
        try:
            return self._get()
        finally:
            _ctx.channel = None

    def _post(self):
        url = urlparse(self.path)
        if url.path in ("/api/sim/start", "/api/sim/reset"):
            return self._sim_post(url.path.rsplit("/", 1)[1], (parse_qs(url.query).get("channel") or [""])[0] or None)
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

    def _get(self):
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
        if p == "/api/channels":
            return self._json(channels_view())
        if p in ("/api/state", "/api/events", "/api/export", "/api/metrics", "/api/sim/state") or p.startswith(
                ("/api/agent/", "/api/snapshot/", "/api/incident/")):
            name = (q.get("channel") or [""])[0] or None
            chn, why = resolve(name)
            if chn is None:
                if p == "/api/state":
                    return self._json(not_configured_state(name, why))
                if p == "/api/sim/state":
                    return self._json({"enabled": False, "reason": why, "scenarios": [], "running": None, "last": None})
                return self._send(400 if MODE == "strict" and not name else 404, json.dumps({"error": why}).encode(), "application/json")
            _ctx.channel = chn
        if p == "/api/state":
            return self._json(aggregate())
        if p == "/api/events":
            since = int(q.get("since", ["0"])[0] or 0)
            limit = int(q.get("limit", ["1000"])[0] or 1000)
            return self._json({"gseq": ch().collector.gseq, "events": ch().collector.events_since(since, limit)})
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
            return self._json({"exported_at": time.time(), "sources": list(ch().sources),
                               "events": ch().collector.all_events()},
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if p == "/api/metrics":
            return self._json(metrics())
        if p == "/api/sim/state":
            return self._json(sim_state())
        if p == "/api/evaluation":
            return self._json(evaluation_view())
        if p.startswith("/api/eval/figure/"):
            png = eval_figure(p.rsplit("/", 1)[1])
            if png is None:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, png, "image/png")
        if p.startswith("/api/incident/"):
            parts = p.split("/")
            if len(parts) != 6 or parts[3] not in VALID_SOURCES or parts[4] not in ch().urls or not parts[5].isdigit():
                return self._send(404, b'{"error":"no such incident"}', "application/json")
            inc = build_incident(parts[3], parts[4], int(parts[5]))
            if inc is None:
                return self._send(404, b'{"error":"no such incident"}', "application/json")
            return self._json(inc)
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    for c_ in CHANNELS.values():
        print(f"dashboard channel {c_.name} (accepts {c_.expect or 'whatever it reports'}): agents={c_.urls}, baseline={c_.baseline or '-'}, "
              f"simulator controls={'yes' if c_.sim_url else 'no'}", flush=True)
        threading.Thread(target=c_.collector.run, daemon=True).start()
    print(f"dashboard on :{PORT} ({MODE}); access token: {'REQUIRED' if AUTH.enabled else 'off (open)'}", flush=True)
    ThreadingHTTPServer((os.environ.get("BIND", "0.0.0.0"), PORT), Handler).serve_forever()
