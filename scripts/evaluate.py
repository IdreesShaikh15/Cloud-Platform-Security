#!/usr/bin/env python3
"""Evaluation harness: runs the simulator scenarios many times and writes CSVs + PNG graphs.

    python3 scripts/evaluate.py                       # everything, 20 trials per variant (about 1.5 h on 4 cores)
    python3 scripts/evaluate.py --quick               # 3 trials per variant, a smoke test (about 10 min)
    python3 scripts/evaluate.py --only scenario,trust # some experiments
    python3 scripts/evaluate.py --report-only         # rebuild CSVs / graphs / docs tables from results/raw/trials.jsonl

SIMULATOR RESULTS ONLY. Telemetry and the Kubernetes API are simulated (sim/world.py); everything above that line is
the real code (agents, signatures, gRPC/mTLS on localhost, quorum, certificates, admission policy, pipeline).
Definitions of every metric, and how to read every graph, are in docs/RESULTS.md.

Reproducibility: every trial has a recorded integer SEED. The seed fixes the trial's INPUTS (which workload, which
agents, signal levels, durations, delays). It does not make timings bit-identical: thread scheduling, the OS and
random ids still vary, so re-running a seed gives similar, not identical, numbers.

A trial that never reaches an endpoint is recorded as such (empty cell, an `outcome` explaining why); a time is
never invented, and a trial that crashed or timed out is recorded as `error`, never dropped.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import random
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))

OUT_DIR = os.path.join(ROOT, "results")
RAW = os.path.join(OUT_DIR, "raw", "trials.jsonl")
NODES = ["A", "B", "C", "D"]
WORKLOADS = {"A": "patient-portal", "B": "auth-service", "C": "records-api", "D": "database"}
KINDS = ("exfil", "tamper", "bruteforce")
PORT_BASE = 52000
MARKER = "TRIAL_RESULT "

# --------------------------------------------------------------------------------------------- experiment plan
# name -> (variants, per-trial timeout seconds, serial?)
EXPERIMENTS = {
    "scenario": (["app-compromise", "single-signal", "slow-burn", "validation-retry"], 420, False),
    "trust": (["lying-agent"], 260, False),
    "dvc": (["genuine-dist", "genuine-central", "accuse-dist", "accuse-central", "crash-dist", "crash-central"],
            300, False),
    "investigation": ([f"{t}-{m}" for t in ("transient", "slow-burn", "ambiguous", "genuine") for m in ("on", "off")],
                      300, False),
    "fault": ([f"accuse-k{k}" for k in range(4)] + [f"silent-k{k}" for k in range(3)] +
              [f"deceived-k{k}" for k in range(3)], 240, False),
    "crash": (["alive4", "alive3", "alive2"], 420, False),
    "mixed": (["genuine", "benign"], 240, False),
    "overhead": (["idle", "attack", "central-idle"], 120, True),
}
EXTRA_TRIALS = {("mixed", "genuine"): 10, ("mixed", "benign"): 10}     # mixed run uses 30 per class by default
TRUST_LIE_AT, TRUST_RESTORE_AT, TRUST_END = 3.0, 33.0, 123.0      # seconds: lying starts / stops, trial ends
SEED_BASE = {"scenario": 1000, "trust": 2000, "dvc": 3000, "investigation": 4000, "fault": 5000, "crash": 6000,
             "mixed": 7000, "overhead": 8000}


# --------------------------------------------------------------------------------------------- worker helpers
def vmrss_mb(field: str = "VmRSS") -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024.0
    return float("nan")


def cpu_s() -> float:
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def build(port: int, investigation: bool = True, validate_timeout_s: float = 30):
    from world import LocalCluster, fast_config, sim_investigation
    cfg = fast_config(port, sim_investigation(enabled=investigation), validate_timeout_s=validate_timeout_s)
    c = LocalCluster(base_port=port, cfg=cfg).start()
    time.sleep(2)
    return c


def build_central(port: int):
    from world import LocalBaseline, fast_config
    b = LocalBaseline(cfg=fast_config(port)).start()
    time.sleep(1)
    return b


class Sampler:
    """Samples `fn()` every `interval` seconds in a background thread, with times relative to start."""

    def __init__(self, fn, interval: float = 0.5):
        self.fn, self.interval, self.rows = fn, interval, []
        self.t0 = time.time()
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.rows.append((time.time() - self.t0, self.fn()))
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self._th.join(timeout=2)
        return self.rows


def isolated_set(backend) -> set:
    return {w for w in list(backend.policies)}


def tap_evidence(cluster, log: list) -> None:
    """Record every Evidence an agent signs (time, agent, target, confidence). Observation only."""
    from resilience.evidence import Evidence
    for a in cluster.agents.values():
        orig = a._emit

        def wrapped(claim, claimed_signer=None, _orig=orig, _id=a.id):
            if isinstance(claim, Evidence):
                log.append((time.time(), _id, claim.target, float(claim.confidence)))
            return _orig(claim, claimed_signer)
        a._emit = wrapped


def incident_min(cluster, scenario: str) -> dict:
    """Earliest time any agent reached each pipeline event of this incident (the dashboard's 'min across agents')."""
    rows = [r for a in cluster.agents.values() for r in a.metrics.summary() if r["scenario"] == scenario]
    out = {}
    for k in ("ttd_s", "tti_s", "ttr_s", "ttv_s", "ttf_s"):
        vals = [r[k] for r in rows if r.get(k) is not None]
        out[k] = min(vals) if vals else None
    out["human_review"] = any(r.get("human_review") for r in rows)
    return out


def max_epoch(cluster, target: str) -> int:
    return max(a.states[target].epoch for a in cluster.agents.values())


def fully_reintegrated(cluster, target: str, alive) -> bool:
    return all(cluster.agents[n].states[target].epoch >= 1 and cluster.agents[n].states[target].phase == "HEALTHY"
               and cluster.agents[n].states[target].stage == "FULL" for n in alive)


def availability(samples, workloads) -> float:
    """Fraction of (sample, workload) pairs in which the workload had NO isolation policy."""
    tot = ok = 0
    for _, iso in samples:
        for w in workloads:
            tot += 1
            ok += 0 if w in iso else 1
    return ok / tot if tot else float("nan")


def seconds_isolated(samples, workload, interval: float = 0.5) -> float:
    return interval * sum(1 for _, iso in samples if workload in iso)


# --------------------------------------------------------------------------------------------- workers
def w_scenario(variant, seed, port):
    rng = random.Random(seed)
    target = rng.choice(NODES)
    delay = rng.uniform(0, 3)
    bad = 1 if variant == "validation-retry" else 0
    c = build(port, validate_timeout_s=6 if bad else 30)
    try:
        c.world.bad_recoveries[WORKLOADS[target]] = bad
        time.sleep(delay)
        params = {"target": target, "delay_s": round(delay, 2)}
        if variant == "slow-burn":
            first = rng.sample(NODES, 2)
            rest = [n for n in NODES if n not in first]
            l0, slope, later = rng.uniform(8.5, 10.5), rng.uniform(0.1, 0.35), rng.uniform(3, 8)
            params.update(l0=round(l0, 2), slope=round(slope, 3), later_s=round(later, 1), first="".join(first))
            c.mark("scenario", target)
            grow = lambda t: l0 + slope * t                       # noqa: E731
            c.world.inject_connections(target, grow, observers=set(first))
            c.world.inject_connections(target, grow, observers=set(rest), start_after=later)
            wait_c, wait_f = 60, 120
        else:
            kinds = KINDS if variant != "single-signal" else (rng.choice(KINDS),)
            params["kinds"] = "+".join(kinds)
            c.mark("scenario", target)
            c.world.attack(target, kinds)
            wait_c, wait_f = 40, 150 if bad else 120
        contained = c.wait_until(lambda: max_epoch(c, target) >= 1, wait_c)
        full = contained and c.wait_until(lambda: fully_reintegrated(c, target, NODES), wait_f)
        res = incident_min(c, "scenario")
        res.update(params, contained=bool(contained), reintegrated=bool(full),
                   attempts=max(a.states[target].attempt for a in c.agents.values()),
                   outcome="completed" if full else ("contained_not_reintegrated" if contained else "not_contained"))
        return res
    finally:
        c.stop()


def w_trust(variant, seed, port):
    rng = random.Random(seed)
    liar = rng.choice(NODES)
    victim = rng.choice([n for n in NODES if n != liar])
    honest = [n for n in NODES if n != liar]
    c = build(port)
    LIE_AT, RESTORE_AT, END = TRUST_LIE_AT, TRUST_RESTORE_AT, TRUST_END
    try:
        def sample():
            v = [c.agents[h].agent_trust.get(liar) for h in honest]
            return {h: round(x, 2) for h, x in zip(honest, v)}
        s = Sampler(sample, 0.5)
        time.sleep(LIE_AT)
        c.compromise_agent(liar, victim)
        time.sleep(RESTORE_AT - LIE_AT)
        c.restore_agent(liar)
        time.sleep(END - RESTORE_AT)
        rows = s.stop()
        series = [(round(t, 2), min(v.values()), round(sum(v.values()) / len(v), 2), max(v.values())) for t, v in rows]

        def first_below(thr, t_from, t_to=1e9):
            for t, mn, _, _ in series:
                if t_from <= t <= t_to and mn < thr:
                    return round(t - t_from, 2)
            return None

        def first_above_after(thr, t_from):
            for t, _, mean, _ in series:
                if t >= t_from and mean >= thr:
                    return round(t - t_from, 2)
            return None
        return {"liar": liar, "victim": victim, "series": series,
                "t_suspect_s": first_below(50, LIE_AT), "t_excluded_s": first_below(40, LIE_AT),
                "min_trust": min(r[1] for r in series),
                "t_back_above_50_s": first_above_after(50, RESTORE_AT),
                "final_mean_trust": series[-1][2],
                "false_isolation": max_epoch(c, victim) >= 1,
                "outcome": "completed"}
    finally:
        c.stop()


def w_dvc(variant, seed, port):
    rng = random.Random(seed)
    kind, mode = variant.split("-")
    target = rng.choice(NODES)
    other = [n for n in NODES if n != target]
    window = 40.0
    if mode == "central":
        b = build_central(port)
        cluster = None
    else:
        cluster = build(port)
        b = None
    backend = (b or cluster).backend
    try:
        samples = Sampler(lambda: isolated_set(backend), 0.5)
        res = {"target": target}
        if kind == "genuine":
            if b:
                backend.marker = {"id": "g1", "scenario": "genuine", "target": target, "attacker": None,
                                  "injected_at": time.time()}
                b.world.attack(target)
                done = lambda: b.ctl.states[target].epoch >= 1              # noqa: E731
            else:
                cluster.mark("genuine", target)
                cluster.world.attack(target)
                done = lambda: max_epoch(cluster, target) >= 1              # noqa: E731
            t0 = time.time()
            end = time.time() + window
            while time.time() < end and not done():
                time.sleep(0.25)
            contained = bool(done())
            res.update(contained=contained, time_to_contain_s=round(time.time() - t0, 2) if contained else None)
            time.sleep(max(0.0, end - time.time()))
            benign = [WORKLOADS[n] for n in other]
        elif kind == "accuse":
            victim = rng.choice(other)
            if b:
                from resilience.simhooks import Compromise
                backend.marker = {"id": "a1", "scenario": "false-accusation", "target": victim,
                                  "attacker": "CENTRAL", "injected_at": time.time()}
                b.comp.set(Compromise(mode="false-accusation", target=victim))
                time.sleep(window)
                false_iso = b.ctl.states[victim].epoch >= 1
            else:
                liar = rng.choice([n for n in NODES if n != victim])
                cluster.compromise_agent(liar, victim)
                time.sleep(window)
                false_iso = max_epoch(cluster, victim) >= 1
                res["liar"] = liar
            res.update(victim=victim, false_isolation=bool(false_iso))
            benign = list(WORKLOADS.values())          # nothing is really wrong: every workload should stay up
        else:                                           # crash of the decision-maker, then a genuine attack
            if b:
                b.crash()
                backend.marker = {"id": "c1", "scenario": "crash", "target": target, "attacker": None,
                                  "injected_at": time.time()}
                b.world.attack(target)
                time.sleep(25)
                contained = b.ctl.states[target].epoch >= 1
            else:
                dead = rng.choice(NODES)
                cluster.crash_agent(dead)
                cluster.mark("crash", target)
                cluster.world.attack(target)
                contained = cluster.wait_until(lambda: max_epoch(cluster, target) >= 1, 25)
                res["crashed"] = dead
            res.update(contained=bool(contained))
            benign = [WORKLOADS[n] for n in other]
            window = 25.0
        rows = samples.stop()
        res.update(availability=round(availability(rows, benign), 4),
                   benign_unavailable_s=round(0.5 * sum(1 for _, iso in rows for w in benign if w in iso), 1),
                   window_s=window, outcome="completed")
        return res
    finally:
        (b or cluster).stop()


def _inv_params(typ, seed):
    rng = random.Random(seed)
    target = rng.choice(NODES)
    p = {"target": target}
    if typ == "transient":
        p.update(observers="".join(sorted(rng.sample(NODES, rng.choice([1, 2, 2, 3])))),
                 level=round(rng.uniform(25, 60), 1), duration=round(rng.uniform(1.5, 4.0), 2))
    elif typ == "slow-burn":
        first = rng.sample(NODES, 2)
        p.update(first="".join(sorted(first)), l0=round(rng.uniform(8.5, 10.5), 2), slope=round(rng.uniform(0.1, 0.35), 3),
                 later_s=round(rng.uniform(3, 8), 1))
    elif typ == "ambiguous":
        p.update(observers="".join(sorted(rng.sample(NODES, 2))), level=round(rng.uniform(9.0, 10.5), 2))
    else:
        p.update(kinds="+".join(rng.sample(KINDS, rng.choice([1, 2, 3]))))
    return p


def apply_inputs(c, typ, p):
    """Identical for the ON and OFF run of one seed."""
    t = p["target"]
    if typ == "transient":
        c.world.inject_connections(t, lambda x, l=p["level"]: l, observers=set(p["observers"]), duration=p["duration"])
    elif typ == "slow-burn":
        grow = lambda x: p["l0"] + p["slope"] * x                    # noqa: E731
        first = set(p["first"])
        c.world.inject_connections(t, grow, observers=first)
        c.world.inject_connections(t, grow, observers=set(NODES) - first, start_after=p["later_s"])
    elif typ == "ambiguous":
        c.world.inject_connections(t, lambda x, l=p["level"]: l, observers=set(p["observers"]))
    else:
        c.world.attack(t, tuple(p["kinds"].split("+")))


def w_investigation(variant, seed, port):
    typ, mode = variant.rsplit("-", 1)
    p = _inv_params(typ, seed)
    t = p["target"]
    malicious = typ in ("slow-burn", "genuine")
    c = build(port, investigation=(mode == "on"))
    try:
        s = Sampler(lambda: isolated_set(c.backend), 0.5)
        c.mark("inv", t)
        t0 = time.time()
        apply_inputs(c, typ, p)
        if malicious:
            contained = c.wait_until(lambda: max_epoch(c, t) >= 1, 60 if typ == "slow-burn" else 25)
            time.sleep(3)
            window = time.time() - t0
        else:
            window = 18.0
            time.sleep(window)
            contained = max_epoch(c, t) >= 1
        rows = s.stop()
        m = incident_min(c, "inv")
        review = any(a.inv.watch.get(t) is not None and a.inv.watch[t].review_needed for a in c.agents.values())
        return {**p, "malicious": malicious, "isolated": bool(contained), "decision_latency_s": m["tti_s"],
                "ttd_s": m["ttd_s"], "isolation_seconds": seconds_isolated(rows, WORKLOADS[t]),
                "human_review_flag": bool(review), "window_s": round(window, 1),
                "outcome": "completed" if (contained or not malicious) else "not_contained"}
    finally:
        c.stop()


def w_fault(variant, seed, port):
    kind, k = variant.rsplit("-k", 1)
    k = int(k)
    rng = random.Random(seed)
    c = build(port)
    try:
        if kind == "accuse":
            liars = rng.sample(NODES, k)
            victim = rng.choice([n for n in NODES if n not in liars])
            c.mark("accuse", victim)
            for l in liars:
                c.compromise_agent(l, victim)
            time.sleep(16)
            honest = [n for n in NODES if n not in liars]
            return {"liars": "".join(sorted(liars)), "victim": victim, "false_isolation": max_epoch(c, victim) >= 1,
                    "liars_flagged": all(any(c.agents[h].agent_trust.get(l) < 50 for h in honest) for l in liars) if liars else None,
                    "outcome": "completed"}
        if kind == "silent":
            dead = rng.sample(NODES, k)
            for d in dead:
                c.crash_agent(d)
            alive = [n for n in NODES if n not in dead]
            target = rng.choice(NODES)
            c.mark("silent", target)
            c.world.attack(target)
            contained = c.wait_until(lambda: max_epoch(c, target) >= 1, 25)
            pend = max([len(v) for a in (c.agents[n] for n in alive) for key, v in a.votes.pending().items()
                        if f":{target}:" in key and key.startswith("CONTAIN")] or [0])
            m = incident_min(c, "silent")
            return {"silent": "".join(sorted(dead)), "target": target, "contained": bool(contained),
                    "tti_s": m["tti_s"], "max_votes_seen": pend,
                    "outcome": "completed" if contained else "not_contained"}
        # deceived: a weak REAL anomaly on a benign workload that exactly one honest agent sees, plus k liars accusing it
        liars = rng.sample(NODES, k)
        honest = [n for n in NODES if n not in liars]
        seer = rng.choice(honest)
        victim = rng.choice([n for n in NODES if n not in liars])
        c.mark("deceived", victim)
        c.world.inject_connections(victim, lambda x: 40, observers={seer})
        for l in liars:
            c.compromise_agent(l, victim)
        time.sleep(20)
        return {"liars": "".join(sorted(liars)), "seer": seer, "victim": victim,
                "false_isolation": max_epoch(c, victim) >= 1, "outcome": "completed"}
    finally:
        c.stop()


def w_crash(variant, seed, port):
    n_alive = int(variant[-1])
    rng = random.Random(seed)
    dead = rng.sample(NODES, 4 - n_alive)
    alive = [n for n in NODES if n not in dead]
    target = rng.choice(NODES)
    c = build(port)
    try:
        for d in dead:
            c.crash_agent(d)
        time.sleep(1)
        c.mark("crash", target)
        c.world.attack(target)
        contained = c.wait_until(lambda: max_epoch(c, target) >= 1, 30)
        full = bool(contained) and n_alive >= 3 and c.wait_until(lambda: fully_reintegrated(c, target, alive), 100)
        pend = max([len(v) for n in alive for key, v in c.agents[n].votes.pending().items()
                    if f":{target}:" in key and key.startswith("CONTAIN")] or [0])
        other_iso = any(max(a.states[o].epoch for a in c.agents.values()) >= 1 for o in NODES if o != target)
        m = incident_min(c, "crash")
        return {"crashed": "".join(sorted(dead)), "target": target, "contained": bool(contained),
                "reintegrated": bool(full), "max_contain_votes_seen": pend, "other_workload_isolated": other_iso,
                "tti_s": m["tti_s"], "ttf_s": m["ttf_s"],
                "outcome": "completed" if full else ("contained_not_reintegrated" if contained else "not_contained")}
    finally:
        c.stop()


def w_mixed(variant, seed, port):
    rng = random.Random(seed)
    target = rng.choice(NODES)
    c = build(port)
    log: list = []
    try:
        tap_evidence(c, log)
        c.mark("mixed", target)
        thr = c.cfg.quorum.evidence_min_conf
        if variant == "genuine":
            kinds = tuple(rng.sample(KINDS, rng.choice([1, 2, 3])))
            c.world.attack(target, kinds)
            contained = c.wait_until(lambda: max_epoch(c, target) >= 1, 25)
            res = {"kinds": "+".join(kinds)}
        else:
            if rng.random() < 0.7:
                res = {"benign_kind": "blip", "observers": "".join(sorted(rng.sample(NODES, rng.choice([1, 2, 3, 4])))),
                       "level": round(rng.uniform(6, 36), 1), "duration": round(rng.uniform(1, 6), 2)}
                c.world.inject_connections(target, lambda x, l=res["level"]: l, observers=set(res["observers"]),
                                           duration=res["duration"])
            else:
                res = {"benign_kind": "burst", "observers": "ABCD", "level": round(rng.uniform(9, 16), 1),
                       "duration": round(rng.uniform(8, 12), 2)}
                c.world.inject_connections(target, lambda x, l=res["level"]: l, duration=res["duration"])
            time.sleep(18)
            contained = max_epoch(c, target) >= 1
        alarm = any(tgt == target and conf >= thr for _, _, tgt, conf in log)
        m = incident_min(c, "mixed")
        res.update(target=target, alarm=bool(alarm), isolated=bool(contained), tti_s=m["tti_s"], ttd_s=m["ttd_s"],
                   outcome="completed")
        return res
    finally:
        c.stop()


def w_overhead(variant, seed, port):
    from world import LocalBaseline, fast_config
    rss0, hwm0 = vmrss_mb(), vmrss_mb("VmHWM")
    if variant == "central-idle":
        b = LocalBaseline(cfg=fast_config(port)).start()
        n_units, stop, world = 1, b.stop, b.world
    else:
        c = build(port)
        n_units, stop, world = 4, c.stop, c.world
    try:
        time.sleep(1)
        rss1 = vmrss_mb()
        cpu0, w0 = cpu_s(), time.time()
        if variant == "attack":
            c.mark("overhead", "C")
            world.attack("C")
        time.sleep(14 if variant == "attack" else 10)
        cpu1, w1 = cpu_s(), time.time()
        rss2, hwm = vmrss_mb(), vmrss_mb("VmHWM")
        return {"units": n_units, "cpu_percent_of_one_core_per_unit": round(100 * (cpu1 - cpu0) / (w1 - w0) / n_units, 2),
                "rss_added_mb_per_unit": round((rss2 - rss0) / n_units, 1), "rss_total_mb": round(rss2, 1),
                "rss_start_mb": round(rss0, 1), "peak_rss_mb": round(hwm, 1), "outcome": "completed"}
    finally:
        stop()


WORKERS = {"scenario": w_scenario, "trust": w_trust, "dvc": w_dvc, "investigation": w_investigation,
           "fault": w_fault, "crash": w_crash, "mixed": w_mixed, "overhead": w_overhead}


def run_worker(spec_json: str) -> None:
    spec = json.loads(spec_json)
    t0 = time.time()
    try:
        res = WORKERS[spec["exp"]](spec["variant"], spec["seed"], spec["port"])
    except Exception as exc:                       # recorded, never dropped
        import traceback
        traceback.print_exc(file=sys.stderr)
        res = {"outcome": f"error: {type(exc).__name__}: {exc}"[:200]}
    res.update(exp=spec["exp"], variant=spec["variant"], seed=spec["seed"], elapsed_s=round(time.time() - t0, 1))
    sys.stdout.write("\n" + MARKER + json.dumps(res, default=str) + "\n")
    sys.stdout.flush()
    os._exit(0)                                    # do not wait for lingering gRPC threads


# --------------------------------------------------------------------------------------------- runner
def load_raw() -> list:
    if not os.path.exists(RAW):
        return []
    out = []
    with open(RAW) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def group_index(exp: str, variant: str, variants: list) -> int:
    """Variants that must see IDENTICAL inputs share a seed series: investigation ON/OFF (same type) and
    distributed/centralized (same kind). Every other variant has its own seeds."""
    if exp == "investigation":
        return [t for t in dict.fromkeys(v.rsplit("-", 1)[0] for v in variants)].index(variant.rsplit("-", 1)[0])
    if exp == "dvc":
        return [t for t in dict.fromkeys(v.split("-")[0] for v in variants)].index(variant.split("-")[0])
    return variants.index(variant)


def plan(args) -> list:
    done = {(r["exp"], r["variant"], r["seed"]) for r in load_raw() if not str(r.get("outcome", "")).startswith("error")}
    only = set(args.only.split(",")) if args.only else set(EXPERIMENTS)
    jobs = []
    for exp, (variants, timeout, serial) in EXPERIMENTS.items():
        if exp not in only:
            continue
        for v in variants:
            vi = group_index(exp, v, variants)
            n = args.trials + (EXTRA_TRIALS.get((exp, v), 0) if not args.quick else 0)
            for i in range(n):
                seed = SEED_BASE[exp] + vi * 100 + i
                if (exp, v, seed) not in done:
                    jobs.append({"exp": exp, "variant": v, "seed": seed, "timeout": timeout, "serial": serial})
    return jobs


def run_trial(job, slot: int) -> dict:
    spec = {"exp": job["exp"], "variant": job["variant"], "seed": job["seed"], "port": PORT_BASE + slot * 10}
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--trial", json.dumps(spec)],
                           capture_output=True, text=True, timeout=job["timeout"], cwd=ROOT)
        line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith(MARKER)), None)
        if line:
            return json.loads(line[len(MARKER):])
        return {**{k: spec[k] for k in ("exp", "variant", "seed")}, "outcome": f"error: no result (exit {p.returncode})",
                "elapsed_s": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {**{k: spec[k] for k in ("exp", "variant", "seed")}, "outcome": f"error: timeout after {job['timeout']}s",
                "elapsed_s": round(time.time() - t0, 1)}


def run_all(args) -> None:
    jobs = plan(args)
    os.makedirs(os.path.dirname(RAW), exist_ok=True)
    print(f"{len(jobs)} trials to run ({args.jobs} in parallel; results append to {os.path.relpath(RAW, ROOT)})", flush=True)
    lock = threading.Lock()
    slots: "queue.Queue[int]" = queue.Queue()
    for i in range(args.jobs):
        slots.put(i)
    counter = [0]
    t_start = time.time()

    def do(job):
        slot = slots.get()
        try:
            res = run_trial(job, slot)
        finally:
            slots.put(slot)
        with lock:
            with open(RAW, "a") as fh:
                fh.write(json.dumps(res, default=str) + "\n")
            counter[0] += 1
            print(f"[{counter[0]}/{len(jobs)} {time.time() - t_start:6.0f}s] {res['exp']}/{res['variant']} seed={res['seed']}"
                  f" -> {res.get('outcome')}", flush=True)

    parallel = [j for j in jobs if not j["serial"]]
    serial = [j for j in jobs if j["serial"]]
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        list(ex.map(do, parallel))
    if serial:                                      # CPU/memory trials run alone, so other trials do not distort them
        saved = args.jobs
        slots = queue.Queue()
        slots.put(0)
        for j in serial:
            do(j)
        args.jobs = saved


# --------------------------------------------------------------------------------------------- statistics
def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def quant(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    i = (len(xs) - 1) * q
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def fmt(x, nd=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def pct(k, n):
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.0f}% (95% CI {100 * lo:.0f}-{100 * hi:.0f}%)" if n else "n/a"


def ok(rows):
    """Trials that ran to a result (not `error`): the denominator for rates. Errors are reported separately."""
    return [r for r in rows if not str(r.get("outcome", "")).startswith("error")]


def dist(xs, nd=1):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n/a (no trial reached it)"
    return f"median {fmt(quant(xs, .5), nd)} (IQR {fmt(quant(xs, .25), nd)}-{fmt(quant(xs, .75), nd)}, min {fmt(min(xs), nd)}, max {fmt(max(xs), nd)}, n={len(xs)})"


# --------------------------------------------------------------------------------------------- report: CSVs
def by(rows, exp):
    d = {}
    for r in rows:
        if r.get("exp") == exp:
            d.setdefault(r["variant"], []).append(r)
    return d


def write_csvs(rows):
    os.makedirs(OUT_DIR, exist_ok=True)
    for exp in EXPERIMENTS:
        rs = [r for r in rows if r.get("exp") == exp]
        if not rs:
            continue
        cols = ["exp", "variant", "seed", "outcome"] + sorted({k for r in rs for k in r if k not in
                                                              ("exp", "variant", "seed", "outcome", "series")})
        with open(os.path.join(OUT_DIR, f"{exp}_trials.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in sorted(rs, key=lambda r: (r["variant"], r["seed"])):
                w.writerow(r)
    ts = [r for r in rows if r.get("exp") == "trust" and r.get("series")]
    if ts:
        with open(os.path.join(OUT_DIR, "trust_trajectories.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["seed", "liar", "t_s", "min_trust_in_honest_agents", "mean_trust", "max_trust"])
            for r in sorted(ts, key=lambda r: r["seed"]):
                for t, mn, mean, mx in r["series"]:
                    w.writerow([r["seed"], r["liar"], t, mn, mean, mx])


# --------------------------------------------------------------------------------------------- report: graphs
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]          # reference categorical slots 1-4 (docs/RESULTS.md)
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
SIM_NOTE = "Simulator results (simulated telemetry and Kubernetes API; real agents, quorum and certificates)"


def mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
                         "grid.linewidth": 0.7, "axes.axisbelow": True, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.facecolor": "white", "axes.facecolor": "white",
                         "savefig.dpi": 130})
    return plt


def finish(plt, fig, name, title):
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.text(0.01, 0.005, SIM_NOTE, fontsize=8, color=MUTED, ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    fig.savefig(os.path.join(OUT_DIR, name))
    plt.close(fig)


def bars_rate(ax, labels, ks, ns, colors=None, ylabel="rate"):
    for i, (k, n) in enumerate(zip(ks, ns)):
        p = k / n if n else 0
        lo, hi = wilson(k, n)
        ax.bar(i, p, color=(colors or [SERIES[0]] * len(labels))[i], width=0.6, zorder=2)
        if n:
            ax.errorbar(i, p, yerr=[[max(0, p - lo)], [max(0, hi - p)]], color=INK, capsize=3, lw=1, zorder=3)
        ax.text(i, min(1.02, hi + 0.03), f"{k}/{n}", ha="center", fontsize=9)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.15)
    ax.set_ylabel(ylabel)


def boxes(ax, data, labels, color=None, ylabel=""):
    pos = list(range(1, len(data) + 1))
    nonempty = [(p, d) for p, d in zip(pos, data) if d]
    if nonempty:
        bp = ax.boxplot([d for _, d in nonempty], positions=[p for p, _ in nonempty], widths=0.55, patch_artist=True,
                        medianprops={"color": INK}, flierprops={"markersize": 3, "markeredgecolor": MUTED})
        for b in bp["boxes"]:
            b.set_facecolor(color or SERIES[0])
            b.set_alpha(0.55)
            b.set_edgecolor(INK)
    rnd = random.Random(0)
    for p, d in zip(pos, data):               # every trial as a dot, so ties and small samples are visible
        if d:
            ax.scatter([p + rnd.uniform(-0.18, 0.18) for _ in d], d, s=9, color=INK, alpha=0.45, zorder=4, linewidths=0)
    for p, d in zip(pos, data):
        if not d:
            ax.text(p, 0.5, "no data", ha="center", fontsize=8, color=MUTED, transform=ax.get_xaxis_transform())
    ax.set_xticks(pos)
    ax.set_xticklabels(labels)
    ax.set_ylabel(ylabel)


def fig_scenarios(plt, rows):
    d = by(rows, "scenario")
    if not d:
        return
    names = [v for v in EXPERIMENTS["scenario"][0] if v in d]
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5))
    for ax, (key, lab) in zip(axes.flat, [("ttd_s", "TTD: time to detect (s)"), ("tti_s", "TTI: time to isolate (s)"),
                                          ("ttr_s", "TTR: time to recover (s)"), ("ttf_s", "TTF: time to full access (s)")]):
        data = [[r[key] for r in ok(d[v]) if r.get(key) is not None] for v in names]
        boxes(ax, data, [f"{v}\n{len(x)}/{len(ok(d[v]))} reached" for v, x in zip(names, data)], ylabel=lab)
        ax.tick_params(axis="x", labelsize=8)
    finish(plt, fig, "fig1_timing_boxplots.png", "Time to detect / isolate / recover / reach full access, per scenario")


def fig_trust(plt, rows):
    ts = [r for r in ok(by(rows, "trust").get("lying-agent", [])) if r.get("series")]
    if not ts:
        return
    import numpy as np
    grid = np.arange(0, TRUST_END, 0.5)
    mats = []
    for r in ts:
        t = np.array([x[0] for x in r["series"]])
        m = np.array([x[2] for x in r["series"]])
        mats.append(np.interp(grid, t, m))
    M = np.vstack(mats)
    fig, ax = plt.subplots(figsize=(10, 5.2))
    for row in M:
        ax.plot(grid, row, color=SERIES[0], alpha=0.12, lw=1)
    ax.fill_between(grid, np.percentile(M, 10, axis=0), np.percentile(M, 90, axis=0), color=SERIES[0], alpha=0.18, lw=0)
    ax.plot(grid, np.median(M, axis=0), color=SERIES[0], lw=2.2, label=f"median over {len(ts)} trials (band = 10th-90th percentile)")
    ax.axvline(TRUST_LIE_AT, color=SERIES[1], lw=1.4)
    ax.axvline(TRUST_RESTORE_AT, color=SERIES[2], lw=1.4)
    ax.axhline(50, color=MUTED, ls="--", lw=1)
    ax.axhline(40, color=MUTED, ls=":", lw=1)
    ax.text(TRUST_LIE_AT + 0.6, 4, "agent starts lying", color=INK, fontsize=9)
    ax.text(TRUST_RESTORE_AT + 0.6, 4, "agent stops lying (restored)", color=INK, fontsize=9)
    ax.text(TRUST_END - 0.5, 51.5, "SUSPECT below 50", ha="right", fontsize=8, color=MUTED)
    ax.text(TRUST_END - 0.5, 36, "votes ignored below 40", ha="right", fontsize=8, color=MUTED)
    ax.set_xlabel("seconds since the start of the trial")
    ax.set_ylabel("trust in the lying agent (0-100), as its peers see it")
    ax.set_ylim(-3, 105)
    ax.legend(loc="upper right", frameon=False)
    finish(plt, fig, "fig2_trust_trajectory.png", "Trust in a lying agent: decay while it lies, slow recovery after")


def fig_dvc(plt, rows):
    d = by(rows, "dvc")
    if not d:
        return
    fig, axes = plt.subplots(1, 3, figsize=(13, 5))
    ax = axes[0]
    ks = [sum(1 for r in ok(d.get(v, [])) if r.get("false_isolation")) for v in ("accuse-dist", "accuse-central")]
    ns = [len(ok(d.get(v, []))) for v in ("accuse-dist", "accuse-central")]
    bars_rate(ax, ["distributed\n(1 of 4 agents lies)", "centralized\n(the controller lies)"], ks, ns, SERIES[:2],
              "false isolation rate")
    ax.set_title("A healthy workload wrongly isolated", fontsize=10, loc="left")
    ax = axes[1]
    data = [[r["availability"] for r in ok(d.get(v, [])) if r.get("availability") is not None] for v in ("accuse-dist", "accuse-central")]
    boxes(ax, data, ["distributed", "centralized"], ylabel="availability of the workloads (fraction of time not isolated)")
    ax.set_ylim(0.4, 1.03)
    ax.set_title("Availability while the decision-maker lies", fontsize=10, loc="left")
    ax = axes[2]
    ks = [sum(1 for r in ok(d.get(v, [])) if r.get("contained")) for v in ("genuine-dist", "crash-dist", "crash-central")]
    ns = [len(ok(d.get(v, []))) for v in ("genuine-dist", "crash-dist", "crash-central")]
    bars_rate(ax, ["distributed\nall alive", "distributed\n1 agent crashed", "centralized\ncontroller crashed"], ks, ns,
              [SERIES[0], SERIES[0], SERIES[1]], "genuine attack contained within window")
    ax.set_title("A real attack is contained", fontsize=10, loc="left")
    ax.tick_params(axis="x", labelsize=8)
    finish(plt, fig, "fig3_distributed_vs_centralized.png", "Distributed (3-of-4 quorum) vs centralized controller")


def fig_investigation(plt, rows):
    d = by(rows, "investigation")
    if not d:
        return
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5))
    types_b = ["transient", "ambiguous"]
    ax = axes[0]
    w = 0.38
    for j, mode in enumerate(("on", "off")):
        ks = [sum(1 for r in ok(d.get(f"{t}-{mode}", [])) if r.get("isolated")) for t in types_b]
        ns = [len(ok(d.get(f"{t}-{mode}", []))) for t in types_b]
        for i, (k, n) in enumerate(zip(ks, ns)):
            p = k / n if n else 0
            lo, hi = wilson(k, n)
            ax.bar(i + (j - .5) * w, p, width=w, color=SERIES[j], zorder=2, label=f"investigation {mode.upper()}" if i == 0 else None)
            if n:
                ax.errorbar(i + (j - .5) * w, p, yerr=[[max(0, p - lo)], [max(0, hi - p)]], color=INK, capsize=3, lw=1, zorder=3)
            ax.text(i + (j - .5) * w, min(1.02, hi + 0.03), f"{k}/{n}", ha="center", fontsize=8)
    ax.set_xticks(range(len(types_b)))
    ax.set_xticklabels(["transient blip\n(benign)", "weak persistent\n(no attack injected)"])
    ax.set_ylim(0, 1.15)
    ax.set_ylabel("false isolation rate")
    ax.legend(frameon=False, loc="upper left")
    ax.set_title("Healthy workload wrongly isolated", fontsize=10, loc="left")
    ax = axes[1]
    types_m = ["slow-burn", "genuine"]
    data, labels = [], []
    for t in types_m:
        for mode in ("on", "off"):
            rs = ok(d.get(f"{t}-{mode}", []))
            data.append([r["decision_latency_s"] for r in rs if r.get("decision_latency_s") is not None])
            labels.append(f"{t}\n{mode.upper()}\n{len(data[-1])}/{len(rs)}")
    boxes(ax, data, labels, ylabel="time from onset to isolation (s)")
    ax.tick_params(axis="x", labelsize=8)
    ax.set_title("Decision latency, real attacks", fontsize=10, loc="left")
    ax = axes[2]
    data, labels = [], []
    for t in types_b:
        for mode in ("on", "off"):
            rs = ok(d.get(f"{t}-{mode}", []))
            data.append([r["isolation_seconds"] for r in rs])
            labels.append(f"{t}\n{mode.upper()}")
    boxes(ax, data, labels, ylabel="seconds the healthy workload was isolated (within 18 s)")
    ax.tick_params(axis="x", labelsize=8)
    ax.set_title("Unnecessary disruption", fontsize=10, loc="left")
    finish(plt, fig, "fig4_investigation_on_off.png", "Targeted investigation ON vs OFF on identical inputs (same seeds)")


def fig_fault(plt, rows):
    d = by(rows, "fault")
    if not d:
        return
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5))
    spec = [("accuse", "false_isolation", range(4), "k agents falsely accuse a healthy workload", "false isolation rate"),
            ("silent", "contained", range(3), "k agents silent; a real attack happens", "attack contained rate"),
            ("deceived", "false_isolation", range(3), "k liars + 1 honest agent that sees a weak real blip", "false isolation rate")]
    for ax, (kind, key, ks_, title, yl) in zip(axes, spec):
        ks, ns = [], []
        for k in ks_:
            rs = ok(d.get(f"{kind}-k{k}", []))
            ks.append(sum(1 for r in rs if r.get(key)))
            ns.append(len(rs))
        bars_rate(ax, [str(k) for k in ks_], ks, ns, [SERIES[1] if (kind != "silent") else SERIES[0]] * len(ks), yl)
        ax.set_xlabel("number of compromised / silent agents (of 4)")
        ax.set_title(title, fontsize=9.5, loc="left")
    finish(plt, fig, "fig5_fault_tolerance_boundary.png", "Fault-tolerance boundary: 0, 1, 2 (and 3) of 4 agents compromised or silent")


def fig_crash(plt, rows):
    d = by(rows, "crash")
    if not d:
        return
    names = [v for v in ("alive4", "alive3", "alive2") if v in d]
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5))
    ks = [sum(1 for r in ok(d[v]) if r.get("contained")) for v in names]
    ns = [len(ok(d[v])) for v in names]
    bars_rate(axes[0], [v[-1] for v in names], ks, ns, [SERIES[0]] * len(names), "attack contained within 30 s")
    axes[0].set_xlabel("agents alive (of 4)")
    ks = [sum(1 for r in ok(d[v]) if r.get("reintegrated")) for v in names]
    bars_rate(axes[1], [v[-1] for v in names], ks, ns, [SERIES[2]] * len(names), "full cycle completed (back to full access)")
    axes[1].set_xlabel("agents alive (of 4)")
    boxes(axes[2], [[r["tti_s"] for r in ok(d[v]) if r.get("tti_s") is not None] for v in names], [v[-1] for v in names],
          ylabel="time to isolate (s)")
    axes[2].set_xlabel("agents alive (of 4)")
    finish(plt, fig, "fig6_agent_crash.png", "Agent crash: 4, 3 and 2 of 4 agents alive")


def fig_overhead(plt, rows):
    d = by(rows, "overhead")
    if not d:
        return
    names = [v for v in ("idle", "attack", "central-idle") if v in d]
    labels = {"idle": "4 agents\nidle", "attack": "4 agents\nunder attack", "central-idle": "1 central\ncontroller idle"}
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))
    boxes(axes[0], [[r["cpu_percent_of_one_core_per_unit"] for r in ok(d[v])] for v in names], [labels[v] for v in names],
          ylabel="CPU, % of one core, per agent/controller")
    boxes(axes[1], [[r["rss_added_mb_per_unit"] for r in ok(d[v])] for v in names], [labels[v] for v in names],
          color=SERIES[1], ylabel="memory added (MB) per agent/controller")
    finish(plt, fig, "fig7_overhead.png", "Per-agent CPU and memory in the simulator (indicative only)")


def confusion(rows):
    d = by(rows, "mixed")
    g, b = ok(d.get("genuine", [])), ok(d.get("benign", []))
    return {"genuine": g, "benign": b,
            "tp_alarm": sum(1 for r in g if r.get("alarm")), "fn_alarm": sum(1 for r in g if not r.get("alarm")),
            "fp_alarm": sum(1 for r in b if r.get("alarm")), "tn_alarm": sum(1 for r in b if not r.get("alarm")),
            "tp_iso": sum(1 for r in g if r.get("isolated")), "fn_iso": sum(1 for r in g if not r.get("isolated")),
            "fp_iso": sum(1 for r in b if r.get("isolated")), "tn_iso": sum(1 for r in b if not r.get("isolated"))}


def fig_confusion(plt, rows):
    cf = confusion(rows)
    if not cf["genuine"] and not cf["benign"]:
        return
    fig, ax = plt.subplots(figsize=(8.5, 5))
    labels = ["false-alarm rate\n(benign trials where an\nagent raised evidence)", "false-isolation rate\n(benign trials where the\nworkload was isolated)",
              "missed-alarm rate\n(genuine attacks with\nno evidence raised)", "missed-isolation rate\n(genuine attacks\nnot isolated in 25 s)"]
    ks = [cf["fp_alarm"], cf["fp_iso"], cf["fn_alarm"], cf["fn_iso"]]
    ns = [len(cf["benign"]), len(cf["benign"]), len(cf["genuine"]), len(cf["genuine"])]
    bars_rate(ax, labels, ks, ns, [SERIES[1], SERIES[1], SERIES[0], SERIES[0]], "rate")
    ax.tick_params(axis="x", labelsize=8)
    finish(plt, fig, "fig8_false_positive_negative.png", "Mixed run: genuine attacks and benign transients")


def make_figs(rows):
    plt = mpl()
    for f in (fig_scenarios, fig_trust, fig_dvc, fig_investigation, fig_fault, fig_crash, fig_overhead, fig_confusion):
        try:
            f(plt, rows)
        except Exception as exc:
            print(f"figure {f.__name__} failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------------------------- report: tables
def tables(rows) -> dict:
    t = {}
    # --- overview of every experiment
    ov = ["| Experiment | Variant | Trials run | Trials that errored | Seeds |", "|---|---|---|---|---|"]
    for exp, (variants, _, _) in EXPERIMENTS.items():
        for v, rs in by(rows, exp).items():
            err = sum(1 for r in rs if str(r.get("outcome", "")).startswith("error"))
            seeds = sorted(r["seed"] for r in rs)
            ov.append(f"| {exp} | {v} | {len(rs)} | {err} | {seeds[0]}-{seeds[-1]} |")
    t["overview"] = "\n".join(ov)
    # --- scenarios
    d = by(rows, "scenario")
    s = ["| Scenario | Trials | Reached full access | TTD (s) | TTI (s) | TTR (s) | TTF (s) |", "|---|---|---|---|---|---|---|"]
    for v in EXPERIMENTS["scenario"][0]:
        rs = ok(d.get(v, []))
        if not rs:
            continue
        s.append(f"| {v} | {len(rs)} | {sum(1 for r in rs if r.get('reintegrated'))}/{len(rs)} | "
                 + " | ".join(dist([r.get(k) for r in rs]) .replace(", min", "<br>min") for k in ("ttd_s", "tti_s", "ttr_s", "ttf_s")) + " |")
    t["scenario"] = "\n".join(s)
    # --- trust
    ts = ok(by(rows, "trust").get("lying-agent", []))
    t["trust"] = "\n".join([
        "| Quantity | Result |", "|---|---|",
        f"| Trials | {len(ts)} |",
        f"| Time until the first honest agent marks the liar SUSPECT (<50), s after it starts lying | {dist([r.get('t_suspect_s') for r in ts])} |",
        f"| Time until a vote-exclusion (<40) | {dist([r.get('t_excluded_s') for r in ts])} |",
        f"| Lowest trust any honest agent reached | {dist([r.get('min_trust') for r in ts])} |",
        f"| Time after it stops lying until mean trust is back above 50, s | {dist([r.get('t_back_above_50_s') for r in ts])} |",
        f"| Mean trust at the end (90 s after it stopped) | {dist([r.get('final_mean_trust') for r in ts])} |",
        f"| Healthy victim wrongly isolated | {pct(sum(1 for r in ts if r.get('false_isolation')), len(ts))} |"])
    # --- dvc
    d = by(rows, "dvc")
    s = ["| Condition | Result |", "|---|---|"]
    for v, lab in [("accuse-dist", "Distributed, 1 agent lies about a healthy workload: false isolation"),
                   ("accuse-central", "Centralized, the controller lies: false isolation")]:
        rs = ok(d.get(v, []))
        s.append(f"| {lab} | {pct(sum(1 for r in rs if r.get('false_isolation')), len(rs))} |")
    for v, lab in [("accuse-dist", "Distributed, availability while an agent lies"), ("accuse-central", "Centralized, availability while the controller lies")]:
        s.append(f"| {lab} | {dist([r.get('availability') for r in ok(d.get(v, []))], 3)} |")
    for v, lab in [("genuine-dist", "Distributed, all alive: real attack contained"), ("crash-dist", "Distributed, 1 agent crashed first: real attack contained"),
                   ("genuine-central", "Centralized, controller alive: real attack contained"), ("crash-central", "Centralized, controller crashed first: real attack contained")]:
        rs = ok(d.get(v, []))
        s.append(f"| {lab} | {pct(sum(1 for r in rs if r.get('contained')), len(rs))} |")
    for v, lab in [("genuine-dist", "Distributed, time to contain a real attack (s)"), ("genuine-central", "Centralized, time to contain a real attack (s)")]:
        s.append(f"| {lab} | {dist([r.get('time_to_contain_s') for r in ok(d.get(v, []))], 2)} |")
    t["dvc"] = "\n".join(s)
    # --- investigation
    d = by(rows, "investigation")
    s = ["| Input | Investigation | Trials | Workload isolated | Isolated seconds (median, of 18 s) | Onset-to-isolation latency (s) | Human-review flag |", "|---|---|---|---|---|---|---|"]
    for typ in ("transient", "ambiguous", "slow-burn", "genuine"):
        for mode in ("on", "off"):
            rs = ok(d.get(f"{typ}-{mode}", []))
            if not rs:
                continue
            lat = dist([r.get("decision_latency_s") for r in rs]) if typ in ("slow-burn", "genuine") else "n/a (benign input)"
            iso_s = fmt(quant([r["isolation_seconds"] for r in rs], .5)) if typ in ("transient", "ambiguous") else "n/a"
            s.append(f"| {typ} ({'real attack' if typ in ('slow-burn', 'genuine') else 'benign, no attack'}) | {mode.upper()} | {len(rs)} | "
                     f"{pct(sum(1 for r in rs if r.get('isolated')), len(rs))} | {iso_s} | {lat} | "
                     f"{sum(1 for r in rs if r.get('human_review_flag'))}/{len(rs)} |")
    t["investigation"] = "\n".join(s)
    # --- fault tolerance
    d = by(rows, "fault")
    s = ["| Test | Compromised / silent agents (k) | Trials | Outcome |", "|---|---|---|---|"]
    for kind, key, lab, ks_ in [("accuse", "false_isolation", "k liars accuse a healthy workload: **false isolation**", range(4)),
                                ("deceived", "false_isolation", "k liars + 1 honest agent that sees a weak real blip: **false isolation**", range(3)),
                                ("silent", "contained", "k agents silent, real attack: **contained**", range(3))]:
        for k in ks_:
            rs = ok(d.get(f"{kind}-k{k}", []))
            if rs:
                s.append(f"| {lab} | {k} | {len(rs)} | {pct(sum(1 for r in rs if r.get(key)), len(rs))} |")
    t["fault"] = "\n".join(s)
    # --- crash
    d = by(rows, "crash")
    s = ["| Agents alive | Trials | Attack contained (30 s) | Full cycle completed | Time to isolate (s) | Time to full access (s) | Most CONTAIN votes seen | Other workload wrongly isolated |", "|---|---|---|---|---|---|---|---|"]
    for v in ("alive4", "alive3", "alive2"):
        rs = ok(d.get(v, []))
        if rs:
            s.append(f"| {v[-1]} | {len(rs)} | {pct(sum(1 for r in rs if r.get('contained')), len(rs))} | "
                     f"{pct(sum(1 for r in rs if r.get('reintegrated')), len(rs))} | {dist([r.get('tti_s') for r in rs])} | "
                     f"{dist([r.get('ttf_s') for r in rs])} | {max([r.get('max_contain_votes_seen', 0) for r in rs] or [0])} | "
                     f"{sum(1 for r in rs if r.get('other_workload_isolated'))}/{len(rs)} |")
    t["crash"] = "\n".join(s)
    # --- overhead
    d = by(rows, "overhead")
    s = ["| Configuration | Trials | CPU, % of one core, per unit | Memory added per unit (MB) | Whole process RSS (MB) |", "|---|---|---|---|---|"]
    for v, lab in [("idle", "4 agents, idle"), ("attack", "4 agents, during an attack + recovery"), ("central-idle", "1 central controller, idle")]:
        rs = ok(d.get(v, []))
        if rs:
            s.append(f"| {lab} | {len(rs)} | {dist([r['cpu_percent_of_one_core_per_unit'] for r in rs])} | "
                     f"{dist([r['rss_added_mb_per_unit'] for r in rs])} | {dist([r['rss_total_mb'] for r in rs])} |")
    t["overhead"] = "\n".join(s)
    # --- confusion
    cf = confusion(rows)
    g, b = len(cf["genuine"]), len(cf["benign"])
    s = ["| Level | Quantity | Result |", "|---|---|---|",
         f"| Alarm (evidence signed above 0.3 confidence) | false-alarm rate = FP / benign trials | {pct(cf['fp_alarm'], b)} |",
         f"| Alarm | missed-alarm rate (false negative) = FN / genuine trials | {pct(cf['fn_alarm'], g)} |",
         f"| Isolation (quorum committed) | false-isolation rate = FP / benign trials | {pct(cf['fp_iso'], b)} |",
         f"| Isolation | missed-isolation rate (false negative) = FN / genuine trials | {pct(cf['fn_iso'], g)} |",
         f"| Isolation | precision = TP / (TP + FP) | {fmt(100 * cf['tp_iso'] / (cf['tp_iso'] + cf['fp_iso']), 0) + '%' if (cf['tp_iso'] + cf['fp_iso']) else 'n/a (nothing isolated)'} |",
         f"| Isolation | recall = TP / genuine trials | {pct(cf['tp_iso'], g)} |"]
    t["mixed"] = "\n".join(s)
    # benign trials stratified by peak strength. The detector's connection rule (detection.py::ramp) is 0 up to 8
    # connections, then 0.5 + 0.5 * min(1, (connections - 8) / 16): 9 connections already rate 0.53, 24 or more rate 1.0.
    bins = [("at or below the alert threshold (<= 8 connections)", lambda l: l <= 8),
            ("moderate (9-16 connections, confidence 0.53-0.75)", lambda l: 8 < l <= 16),
            ("strong (> 16 connections, confidence > 0.75)", lambda l: l > 16)]
    s = ["| Benign transient strength | Seen by | Trials | False alarm | False isolation |", "|---|---|---|---|---|"]
    for lab, f in bins:
        for vis, vf in (("1-2 agents", lambda r: len(r.get("observers", "")) <= 2), ("3-4 agents", lambda r: len(r.get("observers", "")) >= 3)):
            rs = [r for r in cf["benign"] if f(r.get("level", 0)) and vf(r)]
            if rs:
                s.append(f"| {lab} | {vis} | {len(rs)} | {sum(1 for r in rs if r.get('alarm'))}/{len(rs)} | {sum(1 for r in rs if r.get('isolated'))}/{len(rs)} |")
    t["mixed_strata"] = "\n".join(s)
    errs = [r for r in rows if str(r.get("outcome", "")).startswith("error")]
    t["errors"] = (f"{len(errs)} of {len(rows)} trials errored (crash/timeout) and are excluded from every rate; "
                   "they are listed in `results/raw/trials.jsonl`." if errs else
                   f"All {len(rows)} trials produced a result (0 errors).")
    return t


def fill_docs(t: dict) -> None:
    path = os.path.join(ROOT, "docs", "RESULTS.md")
    if not os.path.exists(path):
        return
    text = open(path).read()
    for key, body in t.items():
        a, b = f"<!-- BEGIN:{key} -->", f"<!-- END:{key} -->"
        if a in text and b in text:
            i, j = text.index(a) + len(a), text.index(b)
            text = text[:i] + "\n" + body + "\n" + text[j:]
    open(path, "w").write(text)


def report() -> None:
    rows = load_raw()
    if not rows:
        print("no results yet")
        return
    rows = list({(r["exp"], r["variant"], r["seed"]): r for r in rows}.values())     # latest record wins
    write_csvs(rows)
    make_figs(rows)
    t = tables(rows)
    with open(os.path.join(OUT_DIR, "summary.md"), "w") as fh:
        fh.write("# Evaluation summary (simulator results)\n\nGenerated by scripts/evaluate.py. Metric definitions: docs/RESULTS.md.\n\n")
        for k, v in t.items():
            fh.write(f"## {k}\n\n{v}\n\n")
    fill_docs(t)
    print(f"wrote CSVs, figures and summary to {os.path.relpath(OUT_DIR, ROOT)}/ and updated docs/RESULTS.md tables")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=20, help="trials per variant (default 20)")
    ap.add_argument("--quick", action="store_true", help="3 trials per variant: a smoke test, not for the thesis")
    ap.add_argument("--only", help="comma list of: " + ",".join(EXPERIMENTS))
    ap.add_argument("--jobs", type=int, default=3, help="trials in parallel (default 3; the overhead trials always run alone)")
    ap.add_argument("--out", help="output directory (default: results/)")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--trial", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.trial:
        return run_worker(args.trial)
    global OUT_DIR, RAW
    if args.out:
        OUT_DIR = os.path.abspath(args.out)
        RAW = os.path.join(OUT_DIR, "raw", "trials.jsonl")
    if args.quick:
        args.trials = 3
    if not args.report_only:
        run_all(args)
    report()


if __name__ == "__main__":
    main()
