#!/usr/bin/env python3
"""Is the LAPTOP the bottleneck? (docs/DEMO-TIMING.md). Read-only: it only runs `kubectl get ...`, the API server's read proxy and HTTP GETs.

    bash scripts/demo-health.sh                  # one report
    bash scripts/demo-health.sh --watch          # repeat every 10 s until Ctrl-C
    bash scripts/demo-health.sh --dash http://127.0.0.1:8091     # where the laptop dashboard listens (default)

It reports: CPU and memory per Minikube node and per pod (from the kubelet's own stats, so the Metrics API is NOT needed), how late each agent's
1-second loop runs (the agents report it in /status: `tick_timing`), how long the dashboard takes to answer and to poll each agent, and the
WSL / Docker limits. Every line is PASS / WARN / FAIL; a WARN or FAIL carries a plain-English fix. Exit code 0 = no FAIL, 1 = at least one FAIL.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

KC = os.environ.get("KUBECTL", "kubectl")
EXPECT_CONTEXT = os.environ.get("EXPECT_CONTEXT", "cr-platform")
NS_WATCH = ("healthcare", "resilience")

# thresholds (documented in docs/DEMO-TIMING.md)
CPU_WARN, MEM_WARN = 0.80, 0.85                 # fraction of what a node may allocate
TICK_P95_WARN_MS, TICK_MAX_FAIL_MS = 250, 1500  # an agent tick that starts this late after the previous one
DASH_WARN_S, DASH_FAIL_S = 0.5, 1.5             # dashboard /api/state round trip
POLL_WARN_MS, POLL_FAIL_MS = 600, 1400          # the dashboard's own poll of one agent (its timeout is 1.5 s)
POD_MEM_WARN = 0.85                             # fraction of a pod's memory limit


# ------------------------------------------------------------------ pure helpers (unit-tested)
_UNITS = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
          "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def quantity(q) -> float:
    """Kubernetes quantity ('250m', '2200Mi', '2', '1500000Ki') -> float (cores or bytes)."""
    if q is None or q == "":
        return 0.0
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(q).strip())
    if not m or m.group(2) not in _UNITS:
        return 0.0
    return float(m.group(1)) * _UNITS[m.group(2)]


def percentile(vals, p: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, int(p * len(v)))] if v else 0.0


def node_usage(summary: dict, alloc: dict) -> dict:
    """{'cpu': fraction of allocatable, 'mem': fraction, 'cpu_cores': used, 'mem_mib': used} from a kubelet /stats/summary and the node's allocatable."""
    n = summary.get("node") or {}
    cores = float((n.get("cpu") or {}).get("usageNanoCores") or 0) / 1e9
    mem = float((n.get("memory") or {}).get("workingSetBytes") or 0)
    ac, am = quantity(alloc.get("cpu")), quantity(alloc.get("memory"))
    return {"cpu_cores": cores, "mem_mib": mem / 1024 ** 2,
            "cpu": cores / ac if ac else 0.0, "mem": mem / am if am else 0.0}


def pod_rows(summary: dict, limits: dict) -> list:
    """[(ns/name, cpu_millicores, mem_mib, mem_fraction_of_limit_or_None)] for the watched namespaces, busiest first."""
    out = []
    for p in summary.get("pods") or []:
        ref = p.get("podRef") or {}
        if ref.get("namespace") not in NS_WATCH:
            continue
        key = f'{ref["namespace"]}/{ref["name"]}'
        cpu = float((p.get("cpu") or {}).get("usageNanoCores") or 0) / 1e6
        mem = float((p.get("memory") or {}).get("workingSetBytes") or 0)
        lim = limits.get(key)
        out.append((key, cpu, mem / 1024 ** 2, (mem / lim) if lim else None))
    return sorted(out, key=lambda r: -r[1])


def judge_ticks(t: dict) -> tuple:
    """(level, message) for one agent's `tick_timing` summary."""
    if not t or not t.get("window"):
        return "WARN", "no tick timing reported (an older agent image? rebuild with scripts/build-images.sh)"
    p95, mx, work = t.get("lag_ms_p95", 0), t.get("lag_ms_max", 0), t.get("work_ms_avg", 0)
    msg = f'tick lag p95 {p95:.0f} ms, max {mx:.0f} ms, late ticks {t.get("late_ticks", 0)}/{t["window"]}, work {work:.0f} ms/tick'
    if mx >= TICK_MAX_FAIL_MS:
        return "FAIL", msg
    if p95 >= TICK_P95_WARN_MS:
        return "WARN", msg
    return "PASS", msg


def judge_latency(label: str, ms_values: list, warn_ms: float, fail_ms: float) -> tuple:
    if not ms_values:
        return "FAIL", f"{label}: no answer"
    med, mx = statistics.median(ms_values), max(ms_values)
    msg = f"{label}: median {med:.0f} ms, max {mx:.0f} ms over {len(ms_values)} samples"
    return ("FAIL" if med >= fail_ms else "WARN" if med >= warn_ms or mx >= fail_ms else "PASS"), msg


# ------------------------------------------------------------------ I/O
def kc(*args, timeout=25):
    try:
        r = subprocess.run([KC, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception as exc:                                    # missing kubectl, timeout
        return 127, str(exc)


def kc_json(*args, timeout=25):
    rc, out = kc(*args, timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def http_json(url, timeout=5.0):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
        return json.loads(body), 1000 * (time.time() - t0)
    except Exception:
        return None, 1000 * (time.time() - t0)


class Report:
    def __init__(self):
        self.n = {"PASS": 0, "WARN": 0, "FAIL": 0}

    def line(self, level, msg, hint=""):
        self.n[level] += 1
        print(f"{level:<5} {msg}")
        if hint and level != "PASS":
            print(f"        -> {hint}")

    def section(self, title):
        print(f"\n== {title}")


def wsl_info() -> dict:
    out = {"wsl": False}
    try:
        out["wsl"] = "microsoft" in open("/proc/version").read().lower()
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l and len(l.split()) > 1}
        out.update(mem_total_mib=mem.get("MemTotal", 0) / 1024, mem_avail_mib=mem.get("MemAvailable", 0) / 1024,
                   swap_used_mib=(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024)
        out["load1"] = float(open("/proc/loadavg").read().split()[0])
        out["cpus"] = os.cpu_count() or 1
    except Exception:
        pass
    return out


def run_once(dash: str, samples: int) -> int:
    rep = Report()
    rc, ctx = kc("config", "current-context", timeout=10)
    ctx = ctx.strip() if rc == 0 else ""
    if ctx != EXPECT_CONTEXT:
        print(f"refused: kubectl context is '{ctx or 'none'}', expected '{EXPECT_CONTEXT}'. Run: kubectl config use-context {EXPECT_CONTEXT}", file=sys.stderr)
        return 3
    print(f"demo-health  context {ctx}  {time.strftime('%H:%M:%S')}")

    # ---- 1. laptop / WSL
    rep.section("1. The laptop (this shell's view)")
    w = wsl_info()
    if w.get("mem_total_mib"):
        msg = (f'{"WSL2" if w["wsl"] else "Linux"}: {w["mem_avail_mib"]/1024:.1f} GiB free of {w["mem_total_mib"]/1024:.1f} GiB, '
               f'swap in use {w["swap_used_mib"]:.0f} MiB, load {w["load1"]:.1f} on {w["cpus"]} CPUs')
        if w["mem_avail_mib"] < 1500 or w["swap_used_mib"] > 1024:
            rep.line("WARN", msg, "WSL is short of memory: close browsers/IDEs, or raise memory= in C:\\Users\\<you>\\.wslconfig and run `wsl --shutdown` (docs/DEMO-TIMING.md section 6).")
        elif w["load1"] > 1.5 * w["cpus"]:
            rep.line("WARN", msg, "The CPU is saturated: close other programs, pause the Synthea traffic during attack demos (bash scripts/demo-traffic.sh pause).")
        else:
            rep.line("PASS", msg)
    else:
        rep.line("WARN", "cannot read /proc/meminfo here", "Run this script inside WSL.")
    rc, out = subprocess.getstatusoutput("pgrep -f 'sim/demo_server.py' >/dev/null && echo yes || echo no")
    if out.strip() == "yes":
        rep.line("WARN", "the Demo Mode simulator is running next to the live cluster",
                 "For a live-cluster presentation start the dashboard with: bash scripts/laptop-dashboard.sh live   (no simulator process).")
    rc, out = subprocess.getstatusoutput("docker info --format '{{.NCPU}} {{.MemTotal}}' 2>/dev/null")
    if rc == 0 and out.split() and out.split()[0].isdigit():
        ncpu, mem = out.split()[0], float(out.split()[1]) / 1024 ** 3
        rep.line("PASS" if mem >= 9 and int(ncpu) >= 6 else "WARN",
                 f"Docker gives the Minikube nodes {ncpu} CPUs and {mem:.1f} GiB (four nodes of 2 CPU / 2200 MiB need about 9 GiB and 8 CPUs)",
                 "Docker Desktop > Settings > Resources: raise CPUs / Memory (docs/DEMO-TIMING.md section 6).")

    # ---- 2. nodes and pods (kubelet stats: no Metrics API needed)
    rep.section("2. Minikube nodes and pods (CPU as % of what the node may allocate; memory = working set)")
    nodes = kc_json("get", "nodes", "-o", "json")
    limits = {}
    pods = kc_json("get", "pods", "-A", "-o", "json")
    for p in ((pods or {}).get("items") or []):
        md = p.get("metadata", {})
        tot = sum(quantity(((c.get("resources") or {}).get("limits") or {}).get("memory")) for c in p.get("spec", {}).get("containers", []))
        if tot:
            limits[f'{md.get("namespace")}/{md.get("name")}'] = tot
    allpods = []
    if not nodes:
        rep.line("FAIL", "cannot list the nodes", "Is Minikube running? minikube status -p cr-platform")
    for n in (nodes or {}).get("items", []):
        name = n["metadata"]["name"]
        s = kc_json("get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary", timeout=20)
        if not s:
            rep.line("WARN", f"{name}: the kubelet stats could not be read", "The API server may be slow; re-run in a minute.")
            continue
        u = node_usage(s, n.get("status", {}).get("allocatable", {}))
        msg = f'{name}: CPU {100*u["cpu"]:.0f}% ({u["cpu_cores"]:.2f} cores), memory {100*u["mem"]:.0f}% ({u["mem_mib"]:.0f} MiB)'
        if u["cpu"] >= CPU_WARN or u["mem"] >= MEM_WARN:
            rep.line("WARN", msg, "This Minikube node is nearly full: give Docker more CPU/memory, or stop other work (docs/DEMO-TIMING.md section 6).")
        else:
            rep.line("PASS", msg)
        allpods += pod_rows(s, limits)
    print("   busiest pods:")
    for key, cpu, mem, frac in allpods[:8]:
        warn = "  <- near its memory limit" if frac is not None and frac >= POD_MEM_WARN else ""
        print(f"      {key:<48} {cpu:6.0f} m CPU {mem:7.0f} MiB{warn}")

    # ---- 3. agents: how late does the 1 s loop run?
    rep.section("3. Agents: how late does each agent's 1-second loop run?")
    for a in "abcd":
        t0 = time.time()
        rc, out = kc("get", "--raw", f"/api/v1/namespaces/resilience/services/agent-{a}:8081/proxy/status?events_since=2000000000", timeout=15)
        rtt = 1000 * (time.time() - t0)
        if rc != 0:
            rep.line("FAIL", f"agent {a.upper()}: /status not reachable", f"kubectl -n resilience get pods; kubectl -n resilience logs deploy/agent-{a} --tail=30")
            continue
        try:
            st = json.loads(out)
        except ValueError:
            rep.line("FAIL", f"agent {a.upper()}: /status is not JSON", "Look at the agent's log.")
            continue
        level, msg = judge_ticks(st.get("tick_timing") or {})
        rep.line(level, f"agent {a.upper()}: {msg}; /status via the API server {rtt:.0f} ms",
                 "The agent is running late: the laptop is overloaded (sections 1-2). Late ticks delay detection and voting by the same amount.")

    # ---- 4. dashboard
    rep.section(f"4. Laptop dashboard ({dash}), Live Monitoring channel")
    lat, last = [], None
    for _ in range(samples):
        j, ms = http_json(f"{dash}/api/state?channel=live")
        if j is not None:
            lat.append(ms)
            last = j
        time.sleep(0.2)
    if not lat:
        rep.line("WARN", "the dashboard did not answer", "Start it: bash scripts/laptop-dashboard.sh   (ignore this line if you are not presenting from it).")
    else:
        level, msg = judge_latency("/api/state round trip", lat, DASH_WARN_S * 1000, DASH_FAIL_S * 1000)
        rep.line(level, msg, "The dashboard process is slow or the browser tab is not the problem: check section 1, close extra tabs, use `laptop-dashboard.sh live`.")
        col = (last or {}).get("collector") or {}
        for sid, ms in (col.get("poll_ms") or {}).items():
            ok = (col.get("poll_ok") or {}).get(sid, True)
            level = "FAIL" if not ok or ms >= POLL_FAIL_MS else "WARN" if ms >= POLL_WARN_MS else "PASS"
            rep.line(level, f"dashboard polling agent {sid}: {ms:.0f} ms{'' if ok else '  (no answer)'}",
                     "The `kubectl port-forward` to this agent is slow or dead; scripts/laptop-dashboard.sh restarts it. If all four are slow, the laptop is busy.")
        if col:
            print(f'   last poll {col.get("last_poll_age_s")} s ago; polling every {col.get("poll_every_s")} s; idle={col.get("idle")}')

    # ---- 5. synthetic traffic
    rep.section("5. Synthea client traffic")
    rc, out = kc("-n", "healthcare", "get", "deploy", "client", "-o", "jsonpath={.spec.replicas} {.spec.template.spec.containers[0].env}", timeout=15)
    m = re.search(r'TRAFFIC_CLINICIANS","value":"(\d+)"', out or "")
    n = m.group(1) if m else "3 (default)"
    print(f"   simulated clinicians: {n}   (pause for attack demos: bash scripts/demo-traffic.sh pause)")
    print(f'\nsummary: {rep.n["PASS"]} PASS, {rep.n["WARN"]} WARN, {rep.n["FAIL"]} FAIL')
    if rep.n["FAIL"] or rep.n["WARN"]:
        print("A WARN/FAIL in sections 1-2 or late agent ticks means the laptop, not the design, is the bottleneck.")
    return 1 if rep.n["FAIL"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", action="store_true", help="repeat every 10 s until Ctrl-C")
    ap.add_argument("--dash", default=os.environ.get("DASH", "http://127.0.0.1:8091"))
    ap.add_argument("--samples", type=int, default=8, help="dashboard requests to time")
    a = ap.parse_args(argv)
    if not a.watch:
        return run_once(a.dash.rstrip("/"), a.samples)
    try:
        while True:
            run_once(a.dash.rstrip("/"), a.samples)
            time.sleep(10)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
