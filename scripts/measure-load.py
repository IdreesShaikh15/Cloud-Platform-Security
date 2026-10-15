#!/usr/bin/env python3
"""Measure the extra load of six healthcare workloads compared with four, on THIS machine (not on Minikube).

    python3 scripts/measure-load.py                 # 4 vs 6 workloads, 20 s each, prints a table and writes results/load-local.json
    python3 scripts/measure-load.py --seconds 40

What is real: the workload processes are the real apps/src/app.py (one process per workload, plus the auth/database they call);
the polling is done by the real agent code (resilience.monitoring.HttpTelemetrySource) exactly as each of the four agents does it
once per tick, so four pollers each read every workload. What it measures per configuration: resident memory (RSS) of each workload
process, CPU time the workload processes used, the HTTP requests the polling generated, and how long one poll of all workloads took.

What it is NOT: a Minikube measurement. Containers, the Kubernetes API, the agents' gRPC traffic, TLS and the other pods are not
included. Use scripts/minikube-load.sh on the cluster for those numbers (docs/WORKLOADS.md, "Load").
Nothing here attacks anything; it only reads /health and /_telemetry, like the agents do.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
TICK_S = 1.0                      # the agents' monitoring period (k8s/resilience/10-config.yaml timers.tick_s)
AGENTS = 4


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def proc_stat(pid: int):
    """(rss_bytes, cpu_seconds) from /proc."""
    with open(f"/proc/{pid}/statm") as fh:
        rss = int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    with open(f"/proc/{pid}/stat") as fh:
        f = fh.read().rsplit(")", 1)[1].split()
    return rss, (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def run(profile: str, seconds: float) -> dict:
    from resilience import workloads as W
    from resilience.config import ClusterConfig, NodeSpec
    from resilience.monitoring import HttpTelemetrySource
    wl = W.load(os.path.join(ROOT, "config", profile))
    ports = {w.node: free_port() for w in wl.workloads}
    url = {w.role: f"http://127.0.0.1:{ports[w.node]}" for w in wl.workloads}
    env0 = dict(os.environ, AUTH_URL=url["auth-service"], RECORDS_URL=url["records-api"], DATABASE_URL=url["database"],
                PORTAL_URL=url["patient-portal"], APP_ROOT=os.path.join(ROOT, "apps", "src"), PYTHONUNBUFFERED="1")
    if "lab-results" in url:
        env0.update(LAB_URL=url["lab-results"], MEDICATION_URL=url["medication-service"])
    procs = {}
    try:
        for w in wl.workloads:
            procs[w.node] = subprocess.Popen([sys.executable, os.path.join(ROOT, "apps", "src", "app.py")],
                                             env=dict(env0, ROLE=w.role, PORT=str(ports[w.node])),
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        nodes = {n: NodeSpec(node_id=n, agent_name=f"agent-{n.lower()}", agent_addr="", status_url="",
                             workload=wl.name(n), telemetry_url=f"http://127.0.0.1:{ports[n]}") for n in "ABCD"}
        extra = {n: NodeSpec(node_id=n, agent_name="", agent_addr="", status_url="", workload=wl.name(n),
                             telemetry_url=f"http://127.0.0.1:{ports[n]}") for n in wl.nodes() if n not in "ABCD"}
        cfg = ClusterConfig(nodes=nodes, extra_workloads=extra)
        pollers = [HttpTelemetrySource(cfg) for _ in range(AGENTS)]       # one per agent: each agent reads EVERY workload
        base = {n: proc_stat(p.pid) for n, p in procs.items()}
        durations, unreachable, polls = [], 0, 0
        t_end = time.time() + seconds
        while time.time() < t_end:
            t0 = time.time()
            for src in pollers:
                snaps = src.collect()
                unreachable += sum(1 for s in snaps.values() if not s.reachable)
                polls += 1
            durations.append(time.time() - t0)
            time.sleep(max(0.0, TICK_S - (time.time() - t0)))
        end = {n: proc_stat(p.pid) for n, p in procs.items()}
        ticks = len(durations)
        n_work = len(wl.workloads)
        return {
            "profile": profile, "workloads": n_work, "agents": AGENTS, "seconds": seconds, "ticks": ticks,
            "http_requests_per_second": round(AGENTS * n_work * 2 / TICK_S, 1),     # /health + /_telemetry per workload per agent per tick
            "poll_unreachable_readings": unreachable,
            "full_poll_ms_median": round(1000 * statistics.median(durations), 1),
            "full_poll_ms_max": round(1000 * max(durations), 1),
            "rss_mib": {wl.name(n): round(end[n][0] / 2**20, 1) for n in procs},
            "rss_mib_total": round(sum(v[0] for v in end.values()) / 2**20, 1),
            "cpu_seconds": {wl.name(n): round(end[n][1] - base[n][1], 3) for n in procs},
            "cpu_percent_of_one_core_total": round(100 * sum(end[n][1] - base[n][1] for n in procs) / seconds, 2),
        }
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(5)
            except Exception:
                p.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "load-local.json"))
    a = ap.parse_args()
    rows = [run(p, a.seconds) for p in ("workloads-four.json", "workloads.json")]
    four, six = rows
    print(f"{'':34s}{'4 workloads':>14s}{'6 workloads':>14s}{'difference':>14s}")
    for label, k, unit in (("HTTP requests/s from the agents", "http_requests_per_second", ""),
                           ("one full poll, median (ms)", "full_poll_ms_median", ""),
                           ("one full poll, worst (ms)", "full_poll_ms_max", ""),
                           ("workload memory, total (MiB RSS)", "rss_mib_total", ""),
                           ("workload CPU (% of one core)", "cpu_percent_of_one_core_total", "")):
        print(f"{label:34s}{four[k]:>14}{six[k]:>14}{round(six[k] - four[k], 2):>14}")
    print(f"unreachable readings during the runs: 4 workloads {four['poll_unreachable_readings']}, 6 workloads {six['poll_unreachable_readings']}")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump({"measured_on": "this machine, real app processes + real polling client; NOT Minikube", "rows": rows}, fh, indent=2)
    print(f"wrote {os.path.relpath(a.out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
