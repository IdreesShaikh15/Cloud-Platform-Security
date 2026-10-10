"""scripts/verify-isolation.sh: control-flow tests against a fake kubectl.

These check the SCRIPT (it refuses to start when it should, never deletes a policy it
did not create, cleans up after itself even when interrupted, exit codes). They do NOT
and cannot check that Calico enforces the policy - that is what running the script on
the real cluster is for.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "verify-isolation.sh")
FAKE = os.path.join(ROOT, "tests", "fake_kubectl.py")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

BASE_ARGS = ["--namespace", "healthcare", "--client-pod", "client-1", "--target", "records-api",
             "--agent-pod", "agent-c-1", "--egress-peer", "auth-service",
             "--settle", "0", "--hold", "0", "--deadline", "3", "--request-timeout", "2"]


@pytest.fixture()
def env(tmp_path):
    os.chmod(FAKE, 0o755)
    e = dict(os.environ, KUBECTL=FAKE, FAKE_STATE=str(tmp_path / "state.json"),
             PYTHON=sys.executable)
    return e


def run(env, extra=(), args=BASE_ARGS, timeout=120):
    p = subprocess.run(["bash", SCRIPT, *args, *extra], env=env, capture_output=True, text=True,
                       timeout=timeout)
    return p


def state(env):
    with open(env["FAKE_STATE"]) as fh:
        return json.load(fh)


def commands(env, verb):
    return [c for c in state(env)["log"] if c.startswith(verb)]


def test_happy_path_creates_and_removes_only_its_own_policy(env):
    p = run(env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "[FAIL]" not in p.stdout
    assert state(env)["policies"] == {}, "policy left behind"
    assert len(commands(env, "create")) == 1
    assert len(commands(env, "-n healthcare delete networkpolicy")) == 1
    for needle in ("before: client pod", "isolated: client pod", "isolated: agent pod",
                   "isolated: records-api pod -> auth-service by IP", "DNS lookup",
                   "all 'records-api' pods are still Ready", "removed NetworkPolicy", "after: client pod"):
        assert needle in p.stdout, needle
    assert "is blocked" in p.stdout and "is reachable" in p.stdout


def test_restricted_stage_expectations(env):
    p = run(env, ["--stage", "RESTRICTED"])
    assert p.returncode == 0, p.stdout
    assert "isolated: records-api pod -> auth-service by IP (its own outbound traffic, egress) is reachable" in p.stdout
    assert "isolated: client pod -> records-api (user traffic, ingress) is blocked" in p.stdout


def test_refuses_when_policy_already_exists_and_never_touches_it(env):
    env["FAKE_PREEXIST"] = "1"
    p = run(env)
    assert p.returncode == 2, p.stdout
    assert "ALREADY EXISTS" in p.stdout
    assert commands(env, "create") == [] and commands(env, "-n healthcare delete") == []


def test_isolation_that_does_not_block_is_reported_as_failure_and_cleaned_up(env):
    env["FAKE_NOBLOCK"] = "1"
    p = run(env)
    assert p.returncode == 1, p.stdout
    assert "[FAIL] isolated: client pod -> records-api" in p.stdout
    assert state(env)["policies"] == {}


def test_agent_path_blocked_is_a_failure(env):
    env["FAKE_AGENT_BLOCKED"] = "1"
    p = run(env)
    assert p.returncode == 1
    assert "[FAIL] isolated: agent pod" in p.stdout
    assert state(env)["policies"] == {}


def test_egress_left_open_is_a_failure(env):
    env["FAKE_NOEGRESS"] = "1"
    p = run(env)
    assert p.returncode == 1
    assert "[FAIL] isolated: records-api pod -> auth-service by IP" in p.stdout


def test_failed_cleanup_exits_3_and_tells_you_how_to_fix_it(env):
    env["FAKE_DELETE_FAIL"] = "1"
    p = run(env)
    assert p.returncode == 3, p.stdout
    assert "[FAIL] cleanup" in p.stdout and "kubectl -n healthcare delete networkpolicy" in p.stdout
    assert "resilience-isolate-records-api" in state(env)["policies"]


def test_never_deletes_a_policy_that_is_no_longer_ours(env):
    env["FAKE_REPLACE_ON_CREATE"] = "1"
    p = run(env)
    assert p.returncode == 3, p.stdout
    assert "UNTOUCHED" in p.stdout or "untouched" in p.stdout
    assert commands(env, "-n healthcare delete") == []
    assert "resilience-isolate-records-api" in state(env)["policies"]


def test_ctrl_c_during_isolation_still_cleans_up(env):
    env["FAKE_SLOW"] = "1"
    proc = subprocess.Popen(["bash", SCRIPT, *BASE_ARGS], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True,
                            # a test runner started in the background (nohup, CI) inherits SIGINT = ignored, and so would
                            # the script, which would then never see the Ctrl-C this test sends: restore the default
                            preexec_fn=lambda: __import__("signal").signal(__import__("signal").SIGINT, __import__("signal").SIG_DFL))
    try:
        end = time.time() + 30
        while time.time() < end:
            try:
                if state(env)["policies"]:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail("script never created the policy")
        time.sleep(0.5)
        os.killpg(proc.pid, signal.SIGINT)         # like Ctrl-C in a terminal
        out, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert proc.returncode == 130, out
    assert state(env)["policies"] == {}, "policy left behind after Ctrl-C:\n" + out
    assert "cleanup: policy resilience-isolate-records-api removed" in out


def test_usage_errors_change_nothing(env):
    for args in ([], ["--namespace", "healthcare"],
                 [*BASE_ARGS[:6], "--egress-peer", "auth-service"],     # no agent decision
                 [*BASE_ARGS[:6], "--agent-pod", "agent-c-1"]):          # no egress decision
        p = run(env, args=args)
        assert p.returncode == 2, (args, p.stdout, p.stderr)
    assert not os.path.exists(env["FAKE_STATE"]) or commands(env, "create") == []


def test_missing_client_pod_is_a_preflight_failure(env):
    args = list(BASE_ARGS)
    args[args.index("client-1")] = "no-such-pod"
    p = run(env, args=args)
    assert p.returncode == 2 and "client pod 'no-such-pod'" in p.stdout
    assert commands(env, "create") == []


def test_refuses_while_platform_is_handling_an_incident_on_the_target(env):
    env["FAKE_PHASE"] = "ISOLATED"
    p = run(env)
    assert p.returncode == 2 and "handling an incident" in p.stdout
    assert commands(env, "create") == []


def test_explicit_skips_are_reported(env):
    args = [a for a in BASE_ARGS if a not in ("--agent-pod", "agent-c-1", "--egress-peer", "auth-service")]
    p = run(env, ["--skip-agent-check", "--skip-egress-check"], args=args)
    assert p.returncode == 0, p.stdout
    assert p.stdout.count("[SKIP]") >= 2


def test_print_policy_needs_no_cluster(env):
    p = run(env, ["--print-policy"], args=["--namespace", "healthcare", "--client-pod", "c",
                                            "--target", "records-api", "--skip-agent-check",
                                            "--skip-egress-check"])
    pol = json.loads(p.stdout)
    assert pol["metadata"]["name"] == "resilience-isolate-records-api"
    assert pol["spec"]["egress"] == []


def test_unexpected_abort_is_never_reported_as_success(env, tmp_path):
    """A crash inside the script (here: kubectl missing a command it needs) must not exit 0."""
    broken = tmp_path / "broken_kubectl"
    broken.write_text("#!/bin/sh\nexit 0\n")      # says yes to everything, returns no data
    broken.chmod(0o755)
    env["KUBECTL"] = str(broken)
    p = run(env)
    assert p.returncode != 0, p.stdout
