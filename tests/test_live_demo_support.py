"""Healthcare Phase E: the tripwire test and its safety, the baseline pre-flight, the safe app upgrade order, the synthetic label.

The tripwire is exercised against the REAL application process and the REAL detector; only `kubectl exec` is replaced by a stand-in that
runs the same command locally with /app mapped to a temporary copy of apps/src. Nothing here touches a cluster."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "apps", "src"))
sys.path.insert(0, os.path.join(ROOT, "sim"))
SCRIPT = os.path.join(ROOT, "scripts", "tripwire-test.sh")
TRIP = "static/TRIPWIRE-TEST.txt"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def appcopy(tmp_path):
    dst = tmp_path / "app"
    shutil.copytree(os.path.join(ROOT, "apps", "src"), dst, ignore=shutil.ignore_patterns("__pycache__"))
    return str(dst)


STUB = r'''#!/usr/bin/env python3
# Stand-in for kubectl: records every call, answers `config current-context`, maps /app to $FAKE_APP and runs `exec ... python -c CODE` locally.
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:2] == ["config", "current-context"]:
    print(os.environ.get("FAKE_CONTEXT", "cr-platform")); sys.exit(0)
if args[:2] == ["-n", "resilience"] and "configmap" in args:
    sys.stdout.write(open(os.environ["FAKE_BASELINE"]).read()); sys.exit(0)
if "exec" in args:
    i = args.index("--")
    cmd = [a.replace("/app", os.environ["FAKE_APP"]) if k > 0 else a for k, a in enumerate(args[i + 1:])]
    if cmd[0] == "python":
        cmd[0] = sys.executable
    sys.exit(subprocess.run(cmd).returncode)
sys.exit(1)
'''


@pytest.fixture()
def stub(tmp_path, appcopy):
    path = tmp_path / "kubectl"
    path.write_text(STUB)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    from telemetry import hash_tree
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(hash_tree(appcopy)))
    env = dict(os.environ, KUBECTL=str(path), FAKE_LOG=str(log), FAKE_APP=appcopy, FAKE_BASELINE=str(base))
    return env, log, base


def calls(log):
    return [json.loads(l) for l in open(log) if l.strip()]


def run_script(env, *args):
    return subprocess.run([SCRIPT, *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)


# ------------------------------------------------------------------ the tripwire file and the real detector
def test_the_tripwire_file_is_part_of_the_known_good_manifest():
    from telemetry import hash_tree
    h = hash_tree(os.path.join(ROOT, "apps", "src"))
    assert TRIP in h and "app.py" in h
    body = open(os.path.join(ROOT, "apps", "src", TRIP), encoding="utf-8").read()
    assert "NOT USED BY ANY SERVICE" in body
    for f in ("app.py", "telemetry.py"):                                   # nothing in the application reads the marker
        assert "TRIPWIRE" not in open(os.path.join(ROOT, "apps", "src", f), encoding="utf-8").read()
    gen = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen-baseline-hashes.py")], capture_output=True, text=True, cwd=ROOT)
    assert gen.returncode == 0 and "TRIPWIRE-TEST.txt" in gen.stdout


def start_app(appcopy, role="database"):
    port = free_port()
    env = dict(os.environ, ROLE=role, PORT=str(port), APP_ROOT=appcopy, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.Popen([sys.executable, os.path.join(appcopy, "app.py")], env=env, cwd=appcopy, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
            return p, port
        except Exception:
            time.sleep(0.1)
    p.terminate()
    raise RuntimeError("the application did not start")


def telemetry(port):
    from resilience.monitoring import Snapshot
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_telemetry", timeout=5) as r:
        return Snapshot.from_telemetry("D", time.time(), True, json.loads(r.read()))


def test_the_documented_command_is_detected_as_exactly_one_modified_file_by_the_real_agent_code(stub, appcopy):
    from resilience.detection import Detector
    from telemetry import hash_tree
    env, log, base = stub
    baseline = json.load(open(base))
    app, port = start_app(appcopy)
    try:
        det = Detector(baseline, ["python", "python3"])
        before = telemetry(port)
        fim = lambda snap: [d for d in det.analyze({"D": snap}, time.time()).get("D", []) if d.observation.value == "FILE_INTEGRITY"]   # noqa: E731
        # (this test process shares /proc with the rest of the machine, so network/process readings are not a clean signal here; the
        #  file-integrity check hashes only the copy of the application, which is what the tripwire is about)
        assert fim(before) == []                                                                  # a clean workload: no file change
        files_before = hash_tree(appcopy)
        r = run_script(env, "database", "--yes")                                                 # the operator command (kubectl replaced by the stand-in)
        assert r.returncode == 0, r.stderr
        after = telemetry(port)
        found = fim(after)
        assert [(d.observation.value, d.confidence) for d in found] == [("FILE_INTEGRITY", 0.95)], found
        assert "1 modified file(s): static/TRIPWIRE-TEST.txt" in found[0].summary
        changed = {p for p, h in hash_tree(appcopy).items() if files_before.get(p) != h}
        assert changed == {TRIP}                                                                  # nothing else on disk changed
        assert open(os.path.join(appcopy, TRIP)).read().rstrip().endswith("tripwire test (operator-initiated, harmless)")
        # the platform's replacement (a fresh copy from the known-good image) restores the file and the check passes again
        shutil.copy(os.path.join(ROOT, "apps", "src", TRIP), os.path.join(appcopy, TRIP))
        assert fim(telemetry(port)) == []
    finally:
        app.terminate()
        app.wait(timeout=10)


def test_the_tripwire_script_does_exactly_one_exec_of_python_on_the_marker(stub):
    env, log, _ = stub
    r = run_script(env, "C", "--yes")
    assert r.returncode == 0, r.stderr
    ex = [c for c in calls(log) if "exec" in c]
    assert len(ex) == 1
    c = ex[0]
    assert c[:5] == ["-n", "healthcare", "exec", "deploy/records-api", "--"] and c[5] == "python" and c[6] == "-c"
    assert "/app/static/TRIPWIRE-TEST.txt" in c[7] and c[7].count("open(") == 1 and ',"a")' in c[7]              # append to the one marker
    assert not any(w in " ".join(c) for w in ("sh -c", "bash", "rm ", "curl", "wget", "kubectl delete", "/etc", "/tmp"))
    assert [x for x in calls(log) if x not in ([ "config", "current-context"], c)] == []                    # no other kubectl call at all


def test_the_dry_run_changes_nothing(stub):
    env, log, _ = stub
    r = run_script(env, "A")
    assert r.returncode == 0 and "dry run: nothing was changed" in r.stdout and "kubectl" in r.stdout and "deploy/patient-portal" in r.stdout
    assert not [c for c in calls(log) if "exec" in c]


@pytest.mark.parametrize("who", ["client", "kubernetes", "dashboard", "G", "records-api; rm -rf /", "", "../database"])
def test_only_the_healthcare_workloads_are_accepted(stub, who):
    env, log, _ = stub
    r = run_script(env, who, "--yes")
    assert r.returncode != 0 and not [c for c in calls(log) if "exec" in c]


@pytest.mark.parametrize("who,name", [("E", "deploy/lab-results"), ("F", "deploy/medication-service")])
def test_the_two_optional_workloads_are_accepted_in_a_dry_run(stub, who, name):
    env, log, _ = stub
    r = run_script(env, who)
    assert r.returncode == 0 and "dry run: nothing was changed" in r.stdout and name in r.stdout
    assert not [c for c in calls(log) if "exec" in c]


def test_it_refuses_the_wrong_cluster_and_an_unknown_flag(stub):
    env, log, _ = stub
    env = dict(env, FAKE_CONTEXT="production-eks")
    r = run_script(env, "C", "--yes")
    assert r.returncode == 3 and "expected 'cr-platform'" in r.stderr and not [c for c in calls(log) if "exec" in c]
    assert run_script(env, "C", "--force").returncode == 2
    assert run_script(dict(env, FAKE_CONTEXT="cr-platform"), "C", "--yes").returncode == 0


def test_letters_follow_the_configured_workload_names(stub, tmp_path):
    env, log, _ = stub
    cfg = json.load(open(os.path.join(ROOT, "config", "workloads.json")))
    for n, name in zip("ABCD", ["portal-web", "login-svc", "records-svc", "patient-db"]):
        cfg["workloads"][n]["name"] = name
    p = tmp_path / "w.json"
    p.write_text(json.dumps(cfg))
    r = run_script(dict(env, WORKLOADS_CONFIG=str(p)), "C", "--yes")
    assert r.returncode == 0 and [c for c in calls(log) if "exec" in c][0][3] == "deploy/records-svc"


# ------------------------------------------------------------------ the simulator runs the same pipeline for the same kind of change
def test_a_tripwire_change_runs_the_whole_pipeline_in_the_simulator():
    from world import ATTACK_KINDS, BASELINE_HASHES, TRIPWIRE, LocalCluster, fast_config, sim_investigation
    assert TRIPWIRE in BASELINE_HASHES and "tripwire" not in ATTACK_KINDS               # existing scenarios are unaffected
    c = LocalCluster(base_port=51010, cfg=fast_config(51010, sim_investigation())).start()
    try:
        time.sleep(2)
        c.mark("tripwire", "C")
        c.world.attack("C", ("tripwire",))
        seen = set()                      # evidence is cleared when the isolation is committed, so record it WHILE waiting (not after)

        def isolated():
            seen.update(e.observation.value for e in c.agents["B"].pool.recent(time.time(), 60, target="C"))
            return all(a.states["C"].epoch >= 1 for a in c.agents.values())
        assert c.wait_until(isolated, 30, poll=0.05)
        dec = next(d for d in c.agents["B"].decisions if d["action"] == "CONTAIN" and d["target"] == "C")
        assert len(dec["voters"]) >= 3
        assert seen == {"FILE_INTEGRITY"}                                                   # nothing but the file change was seen
        assert c.wait_until(lambda: all(a.states["C"].phase == "HEALTHY" and a.states["C"].stage == "FULL" for a in c.agents.values()), 120)
        assert all(a.states[o].epoch == 0 for a in c.agents.values() for o in "ABD" + ("EF" if "E" in c.cfg.targets else ""))      # no other workload was touched
    finally:
        c.stop()


# ------------------------------------------------------------------ pre-flight and upgrade order
def verify(env):
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "verify-baseline.py")], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)


def test_verify_baseline_passes_when_the_workloads_match_and_flags_every_kind_of_difference(stub, appcopy):
    env, log, base = stub
    r = verify(env)
    assert r.returncode == 0 and r.stdout.count("ok ") == 6, r.stdout + r.stderr
    open(os.path.join(appcopy, TRIP), "a").write("x\n")
    r = verify(env)
    assert r.returncode == 1 and "modified=['static/TRIPWIRE-TEST.txt']" in r.stdout
    shutil.copy(os.path.join(ROOT, "apps", "src", TRIP), os.path.join(appcopy, TRIP))
    open(os.path.join(appcopy, "static", "extra.txt"), "w").write("new")
    assert "new=['static/extra.txt']" in verify(env).stdout
    os.remove(os.path.join(appcopy, "static", "extra.txt"))
    stale = json.load(open(base))
    stale.pop(TRIP)                                                         # an OLD manifest without the tripwire: the agents would call it a new file
    base.write_text(json.dumps(stale))
    r = verify(env)
    assert r.returncode == 1 and "new=['static/TRIPWIRE-TEST.txt']" in r.stdout and "isolate these workloads" in r.stderr


def test_verify_baseline_exits_2_when_it_cannot_check(stub):
    env, log, base = stub
    base.write_text("")
    assert verify(env).returncode == 2


def test_upgrade_script_keeps_the_agents_off_until_the_manifest_is_verified():
    t = open(os.path.join(ROOT, "scripts", "upgrade-app.sh"), encoding="utf-8").read()
    order = [t.index(x) for x in ("scale \"deploy/$d\" --replicas=0", "kubectl apply -f \"$ROOT/k8s/generated/known-good-hashes.json\"",
                                  "rollout restart deploy", "verify-baseline.py", "--replicas=1")]
    assert order == sorted(order), order
    assert "EXPECT_CONTEXT" in t and "the agents stay OFF" in t
    d = open(os.path.join(ROOT, "scripts", "deploy.sh"), encoding="utf-8").read()
    assert '[ -f "$ROOT/k8s/generated/known-good-hashes.json" ] ||' not in d and "gen-baseline-hashes.py" in d      # always regenerated


def test_upgrade_restarts_the_webhook_on_the_new_configuration_before_the_agents_return(tmp_path, stub):
    """Found on the sandbox cluster: a webhook still on the OLD configuration refused to isolate a NEW workload (lab-results). The upgrade
    must apply RBAC + configuration and restart the webhook while the agents are still off. Run against a recording stand-in for kubectl."""
    env, log, base = stub
    fake = tmp_path / "bin"
    fake.mkdir()
    k = fake / "kubectl"
    k.write_text(STUB.replace("sys.exit(1)\n", "sys.exit(1 if 'central-controller' in args else 0)\n"))
    k.chmod(k.stat().st_mode | stat.S_IEXEC)
    env = dict(env, PATH=f"{fake}{os.pathsep}{env['PATH']}")
    p = subprocess.run(["bash", os.path.join(ROOT, "scripts", "upgrade-app.sh")], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    cmds = [" ".join(c) for c in calls(log)]

    def first(*needles):
        return next(i for i, c in enumerate(cmds) if all(n in c for n in needles))
    off, cfg = first("scale", "agent-a", "--replicas=0"), first("apply", "10-config.yaml")
    rbac, wl = first("apply", "00-rbac.yaml"), first("apply", "10-healthcare-apps.yaml")
    hook, on = first("rollout restart", "deploy/quorum-webhook"), first("scale", "agent-a", "--replicas=1")
    dash = first("rollout restart", "deploy/dashboard")
    assert off < rbac < wl and off < cfg < wl                            # config + RBAC only while the agents are off
    assert wl < hook < on < dash                                          # webhook restarted (and rolled out) BEFORE the agents return
    assert first("rollout status", "deploy/quorum-webhook") < on
    assert "6/7 starting them again" in p.stdout and "OK" not in p.stderr
    assert p.stdout.count("ok       ") == len(__import__("subprocess").run([sys.executable, os.path.join(ROOT, "scripts", "gen-workloads.py"), "--names"], capture_output=True, text=True).stdout.split()) - 1


def test_upgrade_removes_a_deployed_workload_that_is_no_longer_in_the_configuration(tmp_path, stub):
    """Switching from six workloads back to the four-workload profile must not leave lab-results / medication-service behind."""
    env, log, base = stub
    fake = tmp_path / "bin2"
    fake.mkdir()
    k = fake / "kubectl"
    k.write_text(STUB.replace("sys.exit(1)\n", "sys.exit(1 if 'central-controller' in args else 0)\n").replace(
        'if "exec" in args:', 'if "deploy" in args and "-l" in args and "jsonpath" in " ".join(args):\n'
        '    print("patient-portal\\nauth-service\\nrecords-api\\ndatabase\\nlab-results\\nmedication-service"); sys.exit(0)\nif "exec" in args:'))
    k.chmod(k.stat().st_mode | stat.S_IEXEC)
    env = dict(env, PATH=f"{fake}{os.pathsep}{env['PATH']}", WORKLOADS_CONFIG=os.path.join(ROOT, "config", "workloads-four.json"))
    p = subprocess.run(["bash", os.path.join(ROOT, "scripts", "upgrade-app.sh")], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    cmds = [" ".join(c) for c in calls(log)]
    deleted = [c for c in cmds if " delete " in c]
    assert any("delete deploy,svc lab-results" in c for c in deleted) and any("delete deploy,svc medication-service" in c for c in deleted)
    assert any("networkpolicy resilience-isolate-lab-results" in c for c in deleted)
    assert not any(w in c for c in deleted for w in ("patient-portal", "auth-service", "records-api", "database", "client"))
    assert cmds.index(next(c for c in cmds if "delete deploy,svc lab-results" in c)) > cmds.index(next(c for c in cmds if "--replicas=0" in c))   # agents already off
    assert p.stdout.count("ok       ") == 4                               # the baseline check covers only the four configured workloads


# ------------------------------------------------------------------ synthetic label in the application itself
def test_every_patient_response_says_it_is_synthetic(appcopy):
    app, port = start_app(appcopy, "database")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/query", timeout=5) as r:
            body = json.loads(r.read())
        assert body["synthetic"] is True and len(body["rows"]) == 40
    finally:
        app.terminate()
        app.wait(timeout=10)
    src = open(os.path.join(ROOT, "apps", "src", "app.py"), encoding="utf-8").read()
    assert src.count('"synthetic": True') == 6                                           # database, records API, portal, lab results, medications, portal labs-and-medications
    assert "Synthetic data only" in open(os.path.join(ROOT, "apps", "src", "static", "index.html"), encoding="utf-8").read()


def test_the_runbook_quotes_the_exact_command_the_script_runs():
    doc = open(os.path.join(ROOT, "docs", "LIVE_DEMO.md"), encoding="utf-8").read()
    sh = open(SCRIPT, encoding="utf-8").read()
    assert '/app/static/TRIPWIRE-TEST.txt' in doc and "scripts/tripwire-test.sh C --yes" in doc and "python -c" in doc
    assert 'python -c' in sh and "NOT" in doc


# ------------------------------------------------------------------ centralized comparison on the Live page
def test_live_page_is_connected_in_centralized_baseline_mode_with_no_agents(monkeypatch):
    import test_two_pages as T
    r = T.Rig(monkeypatch, demo=False, extra_env={"LIVE_BASELINE_URL": ""})
    ctl = T.Fake()
    try:
        monkeypatch.setenv("LIVE_BASELINE_URL", ctl.url)
        import importlib.util
        spec = importlib.util.spec_from_file_location("dash_central", os.path.join(ROOT, "dashboard", "server.py"))
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        ctl.data = T.agent_status("CENTRAL", "LIVE_CLUSTER")
        ctl.data["mode"] = "centralized"
        for f in r.fakes["live"]:
            f.data = None                                              # no agents exist in this mode
        d.CHANNELS["live"].collector.poll_once()
        c = d.connection_view(d.CHANNELS["live"].collector.snapshot()) if d.ch() is d.CHANNELS["live"] else None
        d._ctx.channel = d.CHANNELS["live"]
        try:
            c = d.connection_view(d.CHANNELS["live"].collector.snapshot())
        finally:
            d._ctx.channel = None
        assert c["connected"] is True and c["central"] is True and c["agents_up"] == 0 and c["state"] == "CONNECTED"
        assert "CENTRALIZED BASELINE MODE" in open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
    finally:
        ctl.stop()
        r.close()
