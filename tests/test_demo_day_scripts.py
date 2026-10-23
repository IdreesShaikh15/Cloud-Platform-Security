"""Demo day: preflight.sh must be read-only and tell the truth when the cluster is not there; demo-reset.sh must guard the context and keep its
order; the runbook may only mention scripts and files that exist. Everything runs against a recording stand-in for kubectl (no cluster)."""
import json
import os
import re
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRE = os.path.join(ROOT, "scripts", "preflight.sh")
RESET = os.path.join(ROOT, "scripts", "demo-reset.sh")
DOC = open(os.path.join(ROOT, "docs", "DEMO-DAY.md"), encoding="utf-8").read()

STUB = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ["STUB_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:2] == ["config", "current-context"]:
    print(os.environ.get("STUB_CONTEXT", "cr-platform")); sys.exit(0)
sys.exit(1)           # every other call fails: "the cluster is not there"
'''


def stub(tmp_path):
    p = tmp_path / "kubectl"
    p.write_text(STUB)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), str(tmp_path / "calls.log")


def verb(c):
    i = 0
    while i < len(c) and c[i].startswith("-"):
        i += 2 if c[i] in ("-n", "--namespace", "--as", "-l", "--context") else 1
    return c[i] if i < len(c) else ""


def run(script, args, tmp_path, ctx="cr-platform", timeout=120):
    k, log = stub(tmp_path)
    env = dict(os.environ, KUBECTL=k, STUB_LOG=log, STUB_CONTEXT=ctx, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    r = subprocess.run(["bash", script, *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=timeout)
    calls = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    return r, calls


def test_scripts_exist_are_executable_and_parse():
    for s in (PRE, RESET):
        assert os.stat(s).st_mode & stat.S_IXUSR, s
        assert subprocess.run(["bash", "-n", s]).returncode == 0
        assert b"\r" not in open(s, "rb").read()


def test_preflight_is_read_only_and_fails_loudly_without_a_cluster(tmp_path):
    r, calls = run(PRE, ["--no-ports"], tmp_path, timeout=180)
    assert r.returncode == 1 and "NOT READY" in r.stdout and "READY TO PRESENT" not in r.stdout
    assert r.stdout.count("FAIL") >= 5 and "-> " in r.stdout                                      # every FAIL carries a fix hint
    verbs = {verb(c) for c in calls}
    assert verbs <= {"config", "get", "annotate", "exec"}, verbs                                    # no apply/delete/patch/scale/rollout/...
    for c in calls:
        if verb(c) == "annotate":
            assert "--dry-run=server" in c and "--as=system:serviceaccount:resilience:resilience-agent" in c, c
        if verb(c) == "exec":
            assert "python" in c and "-c" in c, c                                                  # only the read-only hash used by verify-baseline


def test_preflight_source_contains_no_mutating_kubectl_verbs():
    src = open(PRE, encoding="utf-8").read()
    for verb in ("apply", "delete", "patch", "scale", "rollout", "create", "replace", "edit", "label", "cordon", "drain"):
        assert not re.search(rf"\bkc?\b[^\n#]*\b{verb}\b", src.replace("kc get", "").replace("get --raw", "")), verb


def test_reset_refuses_the_wrong_context_and_dry_run_changes_nothing(tmp_path):
    r, calls = run(RESET, [], tmp_path, ctx="some-other-cluster")
    assert r.returncode == 3 and "refused" in r.stderr and all(verb(c) == "config" for c in calls)
    r, calls = run(RESET, ["--dry-run"], tmp_path)
    assert "dry run finished" in r.stdout
    changing = {verb(c) for c in calls} - {"config", "get"}
    assert not changing, changing                                                                   # a dry run issues no change at all


def test_reset_order_stops_agents_before_clearing_and_starts_them_last():
    src = open(RESET, encoding="utf-8").read()
    order = [src.index(x) for x in ("1/8", "2/8", "3/8", "4/8", "5/8", "6/8", "7/8", "8/8")]
    assert order == sorted(order)
    assert "--replicas=0" in src[src.index("3/8"):src.index("4/8")] or "NEED_AGENTS" in src
    assert "delete networkpolicy" in src[src.index("4/8"):src.index("5/8")] or "delete \"$np\"" in src[src.index("4/8"):src.index("5/8")]
    assert "preflight.sh" in src[src.index("8/8"):]
    for forbidden in ("build-images", "gen-certs", "deploy.sh", "00-rbac", "10-config", "40-webhook", "docker "):
        assert forbidden not in src.replace("(no image rebuild, no certificates, no RBAC/config/webhook changes)", ""), forbidden    # nothing is redeployed


def test_runbook_mentions_only_things_that_exist():
    for s in set(re.findall(r"scripts/([A-Za-z0-9_.-]+\.(?:sh|py))", DOC)):
        assert os.path.isfile(os.path.join(ROOT, "scripts", s)), s
    for f in set(re.findall(r"docs/img/(dashboard-[A-Za-z0-9_.-]+\.png)", DOC)):
        assert os.path.isfile(os.path.join(ROOT, "docs", "img", f)), f
    assert os.path.isfile(os.path.join(ROOT, "docs", "presentation-guide.pdf"))
    for needle in ("demo-reset.sh", "preflight.sh", "tripwire-test.sh E --yes", "simulate-compromised-agent.sh A B", "switch-mode.sh baseline",
                   "simulate-compromised-controller.sh B", "scale deploy/agent-b --replicas=0", "If it fails", "Demo Mode"):
        assert needle in DOC, needle
    sec = re.findall(r"^## (\d+)\. (\d+:\d+)-(\d+:\d+)", DOC, re.M)
    assert [int(x[0]) for x in sec] == [1, 2, 3, 4, 5, 6, 7, 8, 9] and sec[-1][2] == "20:00"        # nine timed parts, ending at minute 20


def test_runbook_scenario_titles_exist_in_the_simulator():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "agent")); sys.path.insert(0, os.path.join(ROOT, "sim"))
    import scenarios
    titles = {v.title if hasattr(v, "title") else v[0].title for v in scenarios.CATALOGUE.values()}
    for t in re.findall(r'scenario \*\*"([^"]+)"\*\*', DOC):
        assert t in titles, t
