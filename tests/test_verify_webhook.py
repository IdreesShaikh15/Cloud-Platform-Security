"""scripts/verify-webhook.sh logic, against a fake kubectl that runs the REAL admission policy.
(This checks the script; scripts/verify-webhook.sh on your cluster checks the cluster.)"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "verify-webhook.sh")
FAKE = os.path.join(ROOT, "tests", "fake_kubectl_webhook.py")
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
ARGS = ["--namespace", "healthcare", "--target", "records-api"]


@pytest.fixture()
def env(tmp_path):
    os.chmod(FAKE, 0o755)
    return dict(os.environ, KUBECTL=FAKE, FAKE_STATE=str(tmp_path / "s.json"), PYTHON=sys.executable)


def run(env, extra=()):
    return subprocess.run(["bash", SCRIPT, *ARGS, *extra], env=env, capture_output=True, text=True, timeout=120)


def test_all_attempts_denied_by_the_webhook_passes(env):
    p = run(env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.count("[PASS]") == 7 and "[FAIL]" not in p.stdout
    assert "denied by the webhook" in p.stdout and "no quorum certificate" in p.stdout
    assert "forged" in p.stdout.lower() and "invalid signature" in p.stdout
    assert "control" in p.stdout


def test_a_webhook_that_does_not_enforce_is_reported_as_failure(env):
    env["FAKE_ALLOW_ALL"] = "1"
    p = run(env)
    assert p.returncode == 1 and p.stdout.count("[FAIL]") >= 6
    assert "ALLOWED" in p.stdout


def test_denial_by_rbac_alone_does_not_count_as_proof(env):
    env["FAKE_RBAC_DENY"] = "1"
    p = run(env)
    assert p.returncode == 1
    assert "NOT by the webhook" in p.stdout and "webhook is unproven" in p.stdout


def test_missing_webhook_configuration_is_a_preflight_error(env):
    env["FAKE_NO_HOOK"] = "1"
    p = run(env)
    assert p.returncode == 2 and "not installed" in p.stdout


def test_failsafe_check_scales_the_webhook_down_and_always_restores_it(env):
    p = run(env, ["--test-failsafe"])
    assert p.returncode == 0, p.stdout
    assert "webhook DOWN" in p.stdout and "restored to 2 replica(s)" in p.stdout
    assert json.load(open(env["FAKE_STATE"]))["replicas"] == 2


def test_a_fail_open_webhook_is_caught_by_the_failsafe_check(env):
    env["FAKE_FAILOPEN"] = "1"
    p = run(env, ["--test-failsafe"])
    assert p.returncode == 1 and "[FAIL] 7" in p.stdout
    assert json.load(open(env["FAKE_STATE"]))["replicas"] == 2, "restored even when the check fails"


def test_usage_errors(env):
    p = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)
    assert p.returncode == 2
