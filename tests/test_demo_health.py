"""scripts/demo-health.sh and scripts/demo-traffic.sh: read-only health report, the pure helpers, the agent's tick-lag metric, the pause switch."""
import json
import os
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import demo_health as dh  # noqa: E402
from resilience.ticktiming import TickTiming  # noqa: E402

HEALTH, TRAFFIC = os.path.join(ROOT, "scripts", "demo-health.sh"), os.path.join(ROOT, "scripts", "demo-traffic.sh")

STUB = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(json.dumps(a) + "\n")
if a[:2] == ["config", "current-context"]:
    print(os.environ.get("STUB_CONTEXT", "cr-platform")); sys.exit(0)
sys.exit(1)
'''


def run(script, args, tmp_path, ctx="cr-platform"):
    k = tmp_path / "kubectl"
    k.write_text(STUB); k.chmod(k.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = dict(os.environ, KUBECTL=str(k), STUB_LOG=str(log), STUB_CONTEXT=ctx)
    r = subprocess.run(["bash", script, *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    return r, [json.loads(l) for l in open(log)] if log.exists() else []


def verb(c):
    i = 0
    while i < len(c) and c[i].startswith("-"):
        i += 2 if c[i] in ("-n", "--namespace") else 1
    return c[i] if i < len(c) else ""


def test_scripts_are_executable_parse_and_have_unix_line_endings():
    for s in (HEALTH, TRAFFIC, os.path.join(ROOT, "scripts", "demo_health.py")):
        assert os.stat(s).st_mode & stat.S_IXUSR, s
        assert b"\r" not in open(s, "rb").read()
    assert subprocess.run(["bash", "-n", HEALTH]).returncode == 0 and subprocess.run(["bash", "-n", TRAFFIC]).returncode == 0


def test_health_refuses_the_wrong_context_and_is_read_only(tmp_path):
    r, calls = run(HEALTH, [], tmp_path, ctx="minikube")
    assert r.returncode == 3 and "refused" in r.stderr
    r, calls = run(HEALTH, ["--samples", "1"], tmp_path)             # the stub answers nothing: every cluster check must FAIL or WARN, loudly
    assert r.returncode == 1 and "FAIL" in r.stdout and "summary:" in r.stdout
    assert {verb(c) for c in calls} <= {"config", "get"}, calls


def test_quantity_node_and_pod_parsing():
    assert dh.quantity("250m") == 0.25 and dh.quantity("2") == 2 and dh.quantity("2200Mi") == 2200 * 1024 ** 2 and dh.quantity("1500000Ki") == 1500000 * 1024
    assert dh.quantity("junk") == 0.0 and dh.quantity(None) == 0.0
    u = dh.node_usage({"node": {"cpu": {"usageNanoCores": 1_500_000_000}, "memory": {"workingSetBytes": 1100 * 1024 ** 2}}}, {"cpu": "2", "memory": "2200Mi"})
    assert round(u["cpu"], 2) == 0.75 and round(u["mem"], 2) == 0.5
    rows = dh.pod_rows({"pods": [
        {"podRef": {"name": "a", "namespace": "kube-system"}, "cpu": {"usageNanoCores": 9e9}, "memory": {"workingSetBytes": 1}},
        {"podRef": {"name": "agent-a", "namespace": "resilience"}, "cpu": {"usageNanoCores": 20e6}, "memory": {"workingSetBytes": 100 * 1024 ** 2}},
        {"podRef": {"name": "portal", "namespace": "healthcare"}, "cpu": {"usageNanoCores": 50e6}, "memory": {"workingSetBytes": 230 * 1024 ** 2}}]},
        {"healthcare/portal": 256 * 1024 ** 2})
    assert [r[0] for r in rows] == ["healthcare/portal", "resilience/agent-a"]           # kube-system ignored, busiest first
    assert rows[0][3] > 0.85 and rows[1][3] is None


def test_tick_and_latency_verdicts():
    ok = {"window": 100, "lag_ms_p95": 10, "lag_ms_max": 60, "late_ticks": 0, "work_ms_avg": 8}
    assert dh.judge_ticks(ok)[0] == "PASS"
    assert dh.judge_ticks({**ok, "lag_ms_p95": 400})[0] == "WARN"
    assert dh.judge_ticks({**ok, "lag_ms_max": 2500})[0] == "FAIL"
    assert dh.judge_ticks({})[0] == "WARN"
    assert dh.judge_latency("x", [100, 120, 90], 500, 1500)[0] == "PASS"
    assert dh.judge_latency("x", [600, 700, 650], 500, 1500)[0] == "WARN"
    assert dh.judge_latency("x", [2000, 2100, 1900], 500, 1500)[0] == "FAIL"
    assert dh.judge_latency("x", [], 500, 1500)[0] == "FAIL"


def test_tick_timing_measures_lateness_without_touching_decisions():
    t = TickTiming(1.0)
    for start in (0.0, 1.0, 2.0, 3.6, 4.6):                  # the tick at 3.6 s started 0.6 s late
        t.record(start, 0.02)
    s = t.summary()
    assert s["ticks"] == 5 and s["lag_ms_max"] == 600.0 and s["late_ticks"] == 1 and s["work_ms_avg"] == 20.0
    assert TickTiming(1.0).summary()["window"] == 0


def test_agent_status_reports_tick_timing():
    sys.path.insert(0, os.path.join(ROOT, "sim"))
    from test_investigation import Cluster
    a = Cluster().agents["A"]
    a.tick_timing.record(100.0, 0.01); a.tick_timing.record(101.0, 0.01)
    st = a.status()
    assert st["tick_timing"]["ticks"] == 2 and "lag_ms_p95" in st["tick_timing"]


def test_traffic_script_pauses_only_the_client_variable(tmp_path):
    r, calls = run(TRAFFIC, ["pause", "--dry-run"], tmp_path)
    assert r.returncode == 0 and "set env deploy/client TRAFFIC_CLINICIANS=0" in r.stdout
    r, calls = run(TRAFFIC, ["set", "99", "--dry-run"], tmp_path)
    assert r.returncode == 2
    r, calls = run(TRAFFIC, ["pause"], tmp_path, ctx="minikube")
    assert r.returncode == 3
    r, calls = run(TRAFFIC, ["resume"], tmp_path)
    assert any(verb(c) == "set" and c[-1] == "TRAFFIC_CLINICIANS=3" for c in calls)
    assert {verb(c) for c in calls} <= {"config", "set", "rollout"}
