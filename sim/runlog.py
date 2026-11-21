"""What happened during the scenario the presenter just started: a recording of the simulated run, and the graphs drawn from it.

The Demo Mode page shows these graphs ("This run") in the Performance Results tab. They are SIMULATOR data from ONE run, labelled as such; the
evaluation graphs next to them (1,140 seeded trials) are never changed by a run.

`RunRecorder` samples the four agents every 0.5 s while a scenario runs (only reads their own status; it changes nothing). `build_panels()` turns the
samples into chart descriptions (plain JSON: series, thresholds, phase bands, markers). WHICH panels a scenario gets depends on what that scenario is about
(PANELS below), and the data in them differ, so every scenario draws a different set of graphs. The browser only draws what it is given.
"""
from __future__ import annotations

import statistics
import threading
import time
from typing import Callable, Dict, List, Optional

NODES = ("A", "B", "C", "D")
SAMPLE_S = 0.5
TAIL_S = 3.0                       # keep sampling this long after the scenario driver returns, so the last state is on the graph
MAX_SAMPLES = 400                  # 200 s: far longer than any scenario
COLORS = {"A": "#4f8df7", "B": "#e8743b", "C": "#2ea56f", "D": "#d99a1c"}      # the same agent colours as the dashboard

# scenario id -> the panels it draws, in order. Different scenarios are about different things, so they draw different graphs.
PANELS: Dict[str, List[str]] = {
    "app-compromise": ["signals", "evidence", "agreement", "access"],
    "slow-burn": ["conns_by_agent", "evidence", "agreement", "access"],
    "lab-results-tampered": ["signals", "evidence", "agreement", "access"],
    "medication-failed-logins": ["signals", "evidence", "agreement", "access"],
    "database-cascade": ["signals", "isolated_count", "agreement", "access"],
    "false-accusation": ["evidence", "liar_trust", "agreement"],
    "forge-evidence": ["rejections", "liar_trust"],
    "agent-crash": ["agents_alive", "evidence", "agreement", "access"],
    "obstruct": ["withheld", "agreement", "liar_trust", "access"],
    "transient-blip": ["conns_by_agent", "investigation", "evidence"],
    "ambiguous": ["conns_by_agent", "investigation", "evidence"],
    "validation-retry": ["attempts", "evidence", "access"],
    "failed-validation": ["attempts", "access"],
}
DEFAULT_PANELS = ["signals", "evidence", "agreement", "access"]


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _vote_count(pending: dict, target: str) -> int:
    n = 0
    for key, voters in (pending or {}).items():
        parts = key.split(":")
        if len(parts) >= 2 and parts[0] == "CONTAIN" and parts[1] == target:
            n = max(n, len(voters))
    return n


class RunRecorder:
    """One recording at a time. `cluster_getter()` returns the current simulated cluster (it is rebuilt on Reset)."""

    def __init__(self, cluster_getter: Callable[[], object], clock: Callable[[], float] = time.time):
        self._get = cluster_getter
        self._clock = clock
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.reset()

    # ------------------------------------------------------------------ lifecycle
    def reset(self) -> None:
        self.stop()
        with self._lock:
            self.meta: Optional[dict] = None
            self.samples: List[dict] = []
            self.notes: List[dict] = []
            self._t0 = None
            self.active = False

    def begin(self, scenario_id: str, title: str, target: str, roles: Optional[dict] = None, target_name: str = "") -> None:
        self.stop()
        with self._lock:
            self._t0 = self._clock()
            self.samples, self.notes = [], []
            self.meta = {"scenario": scenario_id, "title": title, "target": target, "target_name": target_name or target,
                         "roles": dict(roles or {}), "started_at": self._t0, "attack_at": None, "finished_at": None,
                         "phase": "lead-in", "ok": None, "detail": "", "source": "SIMULATOR"}
            self.active = True
            self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def rel(self) -> float:
        return round(self._clock() - (self._t0 or self._clock()), 2)

    def note(self, label: str, kind: str = "attack") -> None:
        """A step of the scripted attack (or of the scenario), stamped with the time it happened."""
        with self._lock:
            if self.meta is not None:
                self.notes.append({"t": self.rel(), "label": label, "kind": kind})

    def set_phase(self, name: str) -> None:
        with self._lock:
            if self.meta is not None:
                self.meta["phase"] = name
                if name == "attack" and self.meta["attack_at"] is None:
                    self.meta["attack_at"] = self.rel()

    def finish(self, ok: bool, detail: str) -> None:
        with self._lock:
            if self.meta is not None:
                self.meta.update(ok=bool(ok), detail=detail, finished_at=self.rel(), phase="done")
        threading.Timer(TAIL_S, self.stop).start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        with self._lock:
            self.active = False

    # ------------------------------------------------------------------ sampling (read-only)
    def _loop(self) -> None:
        while not self._stop.is_set() and len(self.samples) < MAX_SAMPLES:
            t0 = self._clock()
            try:
                s = self.sample_once()
                if s:
                    with self._lock:
                        self.samples.append(s)
            except Exception:
                pass                                                   # a sample that fails is simply missing; the run is never affected
            self._stop.wait(max(0.05, SAMPLE_S - (self._clock() - t0)))

    def sample_once(self) -> Optional[dict]:
        c = self._get()
        meta = self.meta
        if c is None or meta is None:
            return None
        v = meta["target"]
        crashed = getattr(c, "_crashed", {})
        s = {"t": self.rel(), "obs": {}, "W": {}, "wt": {}, "phase": {}, "stage": {}, "votes": {}, "trust": {}, "alive": 0, "attempt": 1,
             "inv_active": 0, "inv_total": 0, "rejected": 0, "withheld": 0, "isolated": [], "healthy": True}
        stat = {}
        for n, a in c.agents.items():
            if n in crashed and crashed[n].is_set():
                continue
            st = a.status(include_events=False)
            stat[n] = st
        s["alive"] = len(stat)
        for n, st in stat.items():
            tv = (st.get("targets") or {}).get(v) or {}
            ob = (st.get("observations") or {}).get(v) or {}
            net, proc, fi, au = ob.get("network") or {}, ob.get("process") or {}, ob.get("file_integrity") or {}, ob.get("auth") or {}
            s["obs"][n] = {"conns": net.get("outbound_connections"), "net": net.get("confidence", 0.0), "proc": proc.get("confidence", 0.0),
                           "file": fi.get("confidence", 0.0), "auth": au.get("confidence", 0.0), "auth_n": au.get("failures_in_window"),
                           "conf": ob.get("confidence", 0.0)}
            s["W"][n] = (tv.get("score") or {}).get("W", 0.0)
            s["wt"][n] = tv.get("workload_trust")
            s["phase"][n], s["stage"][n] = tv.get("phase"), tv.get("stage")
            s["votes"][n] = _vote_count(st.get("pending_votes"), v)
            s["trust"][n] = st.get("agent_trust") or {}
            s["attempt"] = max(s["attempt"], tv.get("attempt") or 1)
            inv = st.get("investigations") or {}
            s["inv_active"] = max(s["inv_active"], len(inv.get("active") or []))
            s["inv_total"] = max(s["inv_total"], len(inv.get("recent") or []) + len(inv.get("active") or []))
            s["rejected"] = max(s["rejected"], len(st.get("rejections") or []))
            s["withheld"] = max(s["withheld"], len(st.get("obstructed_votes") or []))
            s["healthy"] = s["healthy"] and bool(tv.get("healthy", True))
            for t_id, t_v in (st.get("targets") or {}).items():
                if (t_v.get("epoch") or 0) > 0 and t_v.get("phase") != "HEALTHY" and t_id not in s["isolated"]:
                    s["isolated"].append(t_id)
        return s

    # ------------------------------------------------------------------ the result
    def snapshot(self) -> dict:
        with self._lock:
            if self.meta is None:
                return {"available": False}
            meta = dict(self.meta)
            samples, notes = list(self.samples), list(self.notes)
            active = self.active
        return build_run(meta, samples, notes, active)


# ---------------------------------------------------------------------- derived facts
def majority(d: Dict[str, Optional[str]]) -> Optional[str]:
    vals = [x for x in d.values() if x]
    return max(set(vals), key=vals.count) if vals else None


def phase_track(samples: List[dict]) -> List[dict]:
    """[{from, to, label, key}]: what the victim was doing over time (HEALTHY, ISOLATED, RECOVERING, VALIDATING, then the access stages)."""
    out: List[dict] = []
    for s in samples:
        ph, stg = majority(s.get("phase", {})), majority(s.get("stage", {}))
        if ph is None:
            continue
        key = stg if ph == "REINTEGRATING" and stg else ph
        if out and out[-1]["key"] == key:
            out[-1]["to"] = s["t"]
        else:
            out.append({"from": s["t"], "to": s["t"], "key": key})
    for i, b in enumerate(out):
        if i + 1 < len(out):
            b["to"] = out[i + 1]["from"]
    return out


LABELS = {"HEALTHY": "healthy", "ISOLATED": "isolated", "RECOVERING": "replacing", "VALIDATING": "recovery check", "QUARANTINE": "limited access",
          "RESTRICTED": "restricted", "MONITORED": "monitored", "PEER_VALIDATED": "confirmed by peers", "FULL": "normal access"}


def derive_markers(meta: dict, samples: List[dict], notes: List[dict]) -> List[dict]:
    """Vertical lines on the graphs: the scripted attack steps, then what the system did about them (all read from the recording)."""
    m = [{"t": n["t"], "label": n["label"], "kind": n.get("kind", "attack")} for n in notes]
    for s in samples:
        if s["t"] < (meta.get("attack_at") or 0):
            continue
        if any(v >= 3 for v in s["votes"].values()):
            m.append({"t": s["t"], "label": "3 of 4 agents agreed", "kind": "decision"})
            break
    seen = set()
    for b in phase_track(samples):
        k = b["key"]
        if k in seen or k == "HEALTHY" and not seen:
            continue
        seen.add(k)
        if k == "HEALTHY":
            m.append({"t": b["from"], "label": "normal access restored", "kind": "recovery"})
        elif k == "ISOLATED":
            m.append({"t": b["from"], "label": "isolated", "kind": "response"})
        elif k == "RECOVERING":
            m.append({"t": b["from"], "label": "replacing from known-good copy", "kind": "response"})
        elif k == "VALIDATING":
            m.append({"t": b["from"], "label": "recovery check", "kind": "response"})
        elif k == "FULL":
            m.append({"t": b["from"], "label": "full access (policy removed)", "kind": "recovery"})
    return sorted(m, key=lambda x: x["t"])


def summary(meta: dict, samples: List[dict]) -> dict:
    """Times measured from the moment the attack began (the same definitions the evaluation uses): None = did not happen."""
    t_atk = meta.get("attack_at")
    out = {"attack_at": t_atk, "detected_s": None, "isolated_s": None, "recovery_check_s": None, "full_access_s": None}
    if t_atk is None:
        return out
    seen_iso = False
    for s in samples:
        if s["t"] < t_atk:
            continue
        rel = round(s["t"] - t_atk, 1)
        if out["detected_s"] is None and any(o.get("conf", 0) >= 0.3 for o in s["obs"].values()):
            out["detected_s"] = rel
        ph, stg = majority(s["phase"]), majority(s["stage"])
        if ph == "ISOLATED" or ph in ("RECOVERING", "VALIDATING", "REINTEGRATING"):
            seen_iso = True
            if out["isolated_s"] is None:
                out["isolated_s"] = rel
        if ph == "REINTEGRATING" and out["recovery_check_s"] is None:
            out["recovery_check_s"] = rel
        if seen_iso and ph == "HEALTHY" and out["full_access_s"] is None:
            out["full_access_s"] = rel
    return out


def build_run(meta: dict, samples: List[dict], notes: List[dict], active: bool) -> dict:
    markers = derive_markers(meta, samples, notes)
    tmax = max([s["t"] for s in samples] + [meta.get("finished_at") or 0, 20.0])
    ids = PANELS.get(meta["scenario"], DEFAULT_PANELS)
    return {"available": True, "active": active, "meta": meta, "summary": summary(meta, samples), "markers": markers, "t_max": round(tmax + 1, 1),
            "bands": [{"from": b["from"], "to": b["to"], "label": LABELS.get(b["key"], b["key"]), "key": b["key"]} for b in phase_track(samples)],
            "panels": build_panels(meta, samples, ids), "samples": len(samples)}


# ---------------------------------------------------------------------- panels
def _line(name: str, samples: List[dict], f, color: str, style: str = "line") -> dict:
    pts = []
    for s in samples:
        v = f(s)
        if v is not None:
            pts.append([s["t"], round(float(v), 3)])
    return {"name": name, "color": color, "style": style, "points": pts}


def build_panels(meta: dict, samples: List[dict], ids: List[str]) -> List[dict]:
    v, nm = meta["target"], meta.get("target_name") or meta["target"]
    roles = meta.get("roles") or {}
    liar = roles.get("liar") or roles.get("attacker") or "A"
    judges = [n for n in NODES if n != liar]
    out = []

    def agent_series(key, sub=None):
        def get(s, n):
            d = s[key].get(n)
            return d.get(sub) if (sub is not None and d is not None) else d
        return [_line(f"Agent {n}", samples, lambda s, n=n: get(s, n), COLORS[n]) for n in NODES]

    for pid in ids:
        if pid == "signals":
            sig = lambda k: (lambda s: max([o.get(k) or 0.0 for o in s["obs"].values()] or [0.0]))
            out.append({"id": pid, "title": f"What the agents measured on {nm}", "ylabel": "confidence of each signal (0 = normal, 1 = certain)",
                        "ymin": 0, "ymax": 1, "series": [_line("Outbound traffic", samples, sig("net"), "#4f8df7"), _line("Running processes", samples, sig("proc"), "#a23bd1"),
                                                         _line("File changes", samples, sig("file"), "#d6402b"), _line("Failed logins", samples, sig("auth"), "#d99a1c")],
                        "thresholds": [{"y": 0.5, "label": "an agent only votes at 0.5 or more"}],
                        "note": "Each line is the strongest reading any agent had for that kind of signal."})
        elif pid == "conns_by_agent":
            out.append({"id": pid, "title": f"Outbound connections each agent sees on {nm}", "ylabel": "connections",
                        "ymin": 0, "ymax": None, "series": agent_series("obs", "conns"), "thresholds": [{"y": 8, "label": "alert threshold (8)"}],
                        "note": "Agents may see different things: a vantage point can miss a short spike."})
        elif pid == "evidence":
            out.append({"id": pid, "title": f"Evidence score W each agent computed about {nm}", "ylabel": "W (an agent votes to isolate at 0.6 or more)",
                        "ymin": 0, "ymax": 1, "series": agent_series("W"), "thresholds": [{"y": 0.6, "label": "vote threshold 0.6"}],
                        "note": "W combines what each agent reports, weighted by how much it trusts that agent."})
        elif pid == "agreement":
            out.append({"id": pid, "title": f"Agents that signed the vote to isolate {nm}", "ylabel": "signed votes (3 of 4 needed)", "ymin": 0, "ymax": 4,
                        "series": [_line("Signed votes seen", samples, lambda s: max(s["votes"].values() or [0]), "#2b6cb0", "step")],
                        "thresholds": [{"y": 3, "label": "3 of 4 needed"}], "note": "The count drops to 0 once the decision is made and the votes are cleared."})
        elif pid == "access":
            out.append({"id": pid, "title": f"{nm}: workload trust and access", "ylabel": "workload trust (0-100)", "ymin": 0, "ymax": 100,
                        "series": [_line("Workload trust", samples, lambda s: _median(s["wt"].values()), "#2ea56f")],
                        "thresholds": [{"y": 20, "label": ""}, {"y": 40, "label": ""}, {"y": 60, "label": ""}, {"y": 80, "label": "stage thresholds 20/40/60/80"}],
                        "bands": True, "note": "Shaded: what the agents were doing to the service. Trust must reach each threshold before access widens."})
        elif pid == "liar_trust":
            out.append({"id": pid, "title": f"How much the other agents trust agent {liar}", "ylabel": "trust in agent " + liar + " (0-100)", "ymin": 0, "ymax": 100,
                        "series": [_line(f"Agent {j}'s view", samples, lambda s, j=j: (s["trust"].get(j) or {}).get(liar), COLORS[j]) for j in judges],
                        "thresholds": [{"y": 50, "label": "SUSPECT below 50"}, {"y": 40, "label": "votes ignored below 40"}],
                        "note": "Trust falls only while the agent's claims are contradicted by what the others see themselves."})
        elif pid == "agents_alive":
            out.append({"id": pid, "title": "Security agents answering", "ylabel": "agents (of 4)", "ymin": 0, "ymax": 4,
                        "series": [_line("Agents answering", samples, lambda s: s["alive"], "#2b6cb0", "step")], "thresholds": [{"y": 3, "label": "3 needed for agreement"}],
                        "note": "One agent is stopped; the remaining three still reach 3 of 4."})
        elif pid == "investigation":
            out.append({"id": pid, "title": "Targeted investigation (agents re-measure before acting)", "ylabel": "investigations open", "ymin": 0, "ymax": None,
                        "series": [_line("Open investigations", samples, lambda s: s["inv_active"], "#a23bd1", "step"),
                                   _line("Investigations so far", samples, lambda s: s["inv_total"], "#7c8794", "step")],
                        "thresholds": [], "note": "The agents asked each other to look again; a lone or brief signal ends as a false alarm or a human-review flag."})
        elif pid == "rejections":
            out.append({"id": pid, "title": "Forged messages rejected", "ylabel": "messages rejected (cumulative)", "ymin": 0, "ymax": None,
                        "series": [_line("Rejected by the honest agents", samples, lambda s: s["rejected"], "#d6402b", "step")], "thresholds": [],
                        "note": "A message that claims to be another agent fails the signature check; its sender loses 20 trust points each time."})
        elif pid == "withheld":
            out.append({"id": pid, "title": f"Votes the obstructing agent {liar} withheld", "ylabel": "votes withheld (cumulative)", "ymin": 0, "ymax": None,
                        "series": [_line("Withheld by " + liar, samples, lambda s: s["withheld"], "#d6402b", "step")], "thresholds": [],
                        "note": "Silence is never approval: the other three still reach 3 of 4 without it."})
        elif pid == "attempts":
            out.append({"id": pid, "title": f"Recovery attempts for {nm}", "ylabel": "attempt number", "ymin": 0, "ymax": 4,
                        "series": [_line("Recovery attempt", samples, lambda s: s["attempt"], "#2b6cb0", "step"),
                                   _line("Replacement healthy (1 = yes)", samples, lambda s: 1 if s["healthy"] else 0, "#2ea56f", "step")],
                        "thresholds": [], "bands": True, "note": "A replacement that fails its health check is retried (with back-off) a limited number of times."})
        elif pid == "isolated_count":
            out.append({"id": pid, "title": "Services isolated at the same time", "ylabel": "services isolated", "ymin": 0, "ymax": 6,
                        "series": [_line("Isolated services", samples, lambda s: len(s["isolated"]), "#d6402b", "step")], "thresholds": [{"y": 1, "label": "only the attacked service"}],
                        "note": "The services that depend on the attacked one must not be isolated by mistake."})
    return out
