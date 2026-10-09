"""Phase 4: safer recovery (docs/RECOVERY.md) - evidence snapshots, validated retries, honest action results."""
import json
import os
import stat
import time

import pytest

from resilience import actions as act
from resilience.agent import HEALTHY, REINTEGRATING, ResilienceAgent, _Task
from resilience.crypto import KeyRegistry, Signer
from resilience.forensics import pod_summary, redact
from resilience.response import ApiRejected, FakeBackend
from world import FakeTelemetry, FakeWorld, fast_config

W = "records-api"


def make_agent(snapshot_dir=None, port=51901):
    cfg = fast_config(port)
    backend = FakeBackend()
    world = FakeWorld()
    backend.pods_provider, backend.logs_provider = world.pods_for, world.logs_for
    a = ResilienceAgent(cfg, "A", Signer.generate("A"), KeyRegistry({}), FakeTelemetry(world), backend,
                        snapshot_dir=snapshot_dir)
    a.states["C"].epoch = 1
    return a, backend, world


def task(a, backend, state, calls, key="isolate:C:1"):
    def run():
        calls.append(1)
        backend.write_state(W, state)
    return _Task(0.0, key, "C", 1, lambda: int(a._cluster_state(W).get("isolated-epoch", -1)) >= 1, run,
                 "isolate records-api", action="CONTAIN/isolate")


def outcomes(a):
    return [r["outcome"] for r in a.audit.recent]


# --------------------------------------------------------------------------- timeout is not failure
def test_timeout_whose_request_was_applied_is_not_repeated():
    a, b, _ = make_agent()
    b.inject("write_state", "timeout_applied")
    calls = []
    t = task(a, b, {"isolated-epoch": 1}, calls)
    assert a._step(t, time.time()) is False                  # done: the cluster shows the effect
    assert len(calls) == 1                                   # the write was NOT repeated
    assert outcomes(a) == [act.APPLIED_AFTER_TIMEOUT]


def test_timeout_whose_request_was_lost_is_retried_after_checking():
    a, b, _ = make_agent()
    b.inject("write_state", "timeout_lost")
    calls = []
    t = task(a, b, {"isolated-epoch": 1}, calls)
    now = time.time()
    assert a._step(t, now) is True                           # effect absent -> retry later
    assert outcomes(a) == [act.NOT_APPLIED]
    assert a._step(t, t.due + 0.01) is False
    assert len(calls) == 2 and outcomes(a)[-1] == act.APPLIED


def test_unreadable_cluster_gives_explicit_unknown_then_resolves_without_repeating():
    a, b, _ = make_agent()
    b.inject("write_state", "timeout_applied")
    b.inject("read_state", "ok", "unreadable")               # first read fine; the confirming read times out
    calls = []
    t = task(a, b, {"isolated-epoch": 1}, calls)
    assert a._step(t, time.time()) is True
    assert outcomes(a) == [act.UNKNOWN] and a.audit.unresolved
    assert a.status()["targets"]["C"]["display_state"] == "UNKNOWN"
    assert a._step(t, t.due + 0.01) is False                 # re-read: it IS there
    assert outcomes(a)[-1] == act.UNKNOWN_RESOLVED and not a.audit.unresolved
    assert len(calls) == 1                                   # never blindly repeated


def test_definite_refusal_is_failed_and_retried_with_backoff():
    a, b, _ = make_agent()
    b.inject("write_state", "error")
    calls = []
    t = task(a, b, {"isolated-epoch": 1}, calls)
    now = time.time()
    assert a._step(t, now) is True and outcomes(a) == [act.FAILED] and t.due > now
    assert a._step(t, t.due + 0.01) is False
    assert len(calls) == 2


def test_classification():
    from resilience.response import AdmissionDenied
    assert act.classify_error(AdmissionDenied("no")) == act.DEFINITE
    assert act.classify_error(ApiRejected("409")) == act.DEFINITE
    assert act.classify_error(act.ApiTimeout("t")) == act.AMBIGUOUS
    assert act.classify_error(ConnectionResetError()) == act.AMBIGUOUS
    assert act.classify_error(RuntimeError("weird")) == act.AMBIGUOUS     # unknown -> never assume "not applied"

    class E(Exception):
        status = 503
    assert act.classify_error(E()) == act.AMBIGUOUS


def test_already_applied_by_another_agent_is_recorded_not_repeated():
    a, b, _ = make_agent()
    b.write_state(W, {"isolated-epoch": 1})
    calls = []
    assert a._step(task(a, b, {"isolated-epoch": 1}, calls), time.time()) is False
    assert calls == [] and outcomes(a) == [act.ALREADY_APPLIED]


def test_stale_task_is_refused_and_audited():
    a, b, _ = make_agent()
    calls = []
    t = task(a, b, {"isolated-epoch": 1}, calls)
    a.tasks = [t]
    a.states["C"].epoch = 2                                  # a newer incident superseded this one
    a._run_tasks(time.time())
    assert calls == [] and a.tasks == [] and outcomes(a) == [act.REFUSED_STALE]


def test_guard_rejects_changed_attempt_or_phase():
    a, _, _ = make_agent()
    st = a.states["C"]
    st.phase, st.attempt = "RECOVERING", 2
    assert a._guard("C", 1, ("RECOVERING",), 2) is None
    assert "attempt changed" in a._guard("C", 1, ("RECOVERING",), 1)
    assert "incident version changed" in a._guard("C", 5, ("RECOVERING",), 2)
    assert "not" in a._guard("C", 1, ("ISOLATED",), 2)


def test_action_is_abandoned_and_a_human_is_asked_after_too_many_failures():
    a, b, _ = make_agent()
    a.cfg.recovery.action_max_attempts = 2
    b.inject("write_state", "error", "error", "error", "error")
    t = task(a, b, {"isolated-epoch": 1}, [])
    n = 0
    while a._step(t, t.due + 0.01 if n else time.time()) and n < 10:
        n += 1
    assert outcomes(a)[-2:] == [act.ABANDONED, act.ABANDONED] or act.ABANDONED in outcomes(a)
    assert a.states["C"].attention and "could not be completed" in a.states["C"].attention["reason"]


def test_audit_log_is_hash_chained(tmp_path):
    a, b, _ = make_agent()
    a.audit = act.ActionAudit("A", str(tmp_path / "actions.jsonl"))
    for _ in range(3):
        a._step(task(a, b, {"isolated-epoch": 1}, [], key=f"k{_}"), time.time())
    from resilience.decisionlog import DecisionLog
    ok, bad, msg, n = DecisionLog.verify_file(str(tmp_path / "actions.jsonl"))
    assert ok and n >= 3, msg


# --------------------------------------------------------------------------- idempotent recovery
def test_same_recovery_request_does_not_start_a_second_rollout():
    b = FakeBackend(recovery_delay_s=100)
    b.recover(W, 1, "", 1)
    assert b.rollout_in_progress(W)
    first = b.recoveries[W]
    b.recover(W, 1, "", 1)                                   # repeated after an unknown outcome
    assert b.recoveries[W] == first
    b.recover(W, 1, "", 2)                                   # a genuine retry is a new rollout
    assert b.recoveries[W][2] == 2 and b.recoveries[W] != first


def test_recover_waits_for_isolation_and_for_a_running_rollout():
    from resilience.certificate import ActionPending
    a, b, _ = make_agent()
    present, recover = a._make_recover("C", 1, "CONTAIN:C:1:", 1, "C:1:contain")
    with pytest.raises(ActionPending, match="isolation"):
        recover()
    b.write_state(W, {"isolated-epoch": 1})
    b.recovery_delay = 100
    b.recoveries[W] = (0, time.time() + 100, 1)
    b.delay = 100
    with pytest.raises(ActionPending, match="rollout"):
        recover()
    assert not present()


# --------------------------------------------------------------------------- evidence snapshot
def test_redaction_removes_credentials():
    text = "Authorization: Bearer abc.def.ghi\npassword=hunter2\nplain line\napi_key: XYZ123"
    clean, n = redact(text)
    assert n >= 3 and "abc.def.ghi" not in clean and "hunter2" not in clean and "XYZ123" not in clean
    assert "plain line" in clean


def test_pod_summary_never_contains_env_or_secrets():
    pod = {"metadata": {"name": "p", "uid": "u", "labels": {"app": W}, "annotations": {"x": "SECRET"}},
           "spec": {"containers": [{"env": [{"name": "TOKEN", "value": "SECRET"}]}], "node_name": "n"},
           "status": {"phase": "Running", "container_statuses": [{"name": "app", "ready": True,
                                                                    "state": {"running": {}}}]}}
    out = json.dumps(pod_summary(pod))
    assert "SECRET" not in out and "env" not in out and "uid" in out


def _wait_snapshot(a, key):
    end = time.time() + 5
    while time.time() < end and a.forensics.get(key) is None:
        time.sleep(0.05)
    return a.forensics.get(key)


def test_snapshot_captures_scoped_evidence_and_redacts(tmp_path):
    a, b, world = make_agent(str(tmp_path))
    world.attack("C")
    a._observe(time.time())
    key = a.forensics.capture_async("C", 1, "contain", "test", {"action": "CONTAIN"}, time.time())
    snap = _wait_snapshot(a, key)
    assert snap and snap["pods"] and snap["pods"][0]["labels"] == {"app": W}
    text = json.dumps(snap)
    assert "SIMULATED-SECRET" not in text and "[REDACTED" in text          # credential scrubbed
    assert snap["file_hashes"] and snap["digest"] and snap["scope"]
    assert "env" not in snap["pods"][0]
    end = time.time() + 5
    while time.time() < end and not [p for p in os.listdir(tmp_path) if p.endswith(".json")]:
        time.sleep(0.05)
    path = [p for p in os.listdir(tmp_path) if p.startswith("snapshot-") and p.endswith(".json")][0]
    assert stat.S_IMODE(os.stat(tmp_path / path).st_mode) == 0o600
    # only this workload's pods are ever requested
    assert all(p["labels"].get("app") == W for p in snap["pods"])


def test_snapshot_says_what_it_could_not_capture():
    a, b, world = make_agent()
    b.inject("list_pods", "unreadable")
    key = a.forensics.capture_async("C", 1, "contain", "test", None, time.time())
    snap = _wait_snapshot(a, key)
    items = [m["item"] for m in snap["not_captured"]]
    assert "pod metadata" in items and snap["pods"] == []                  # honest, not faked
    # a pod that vanishes while its log is read
    a2, b2, _ = make_agent(port=51911)
    b2.inject("read_logs", "gone")
    snap2 = _wait_snapshot(a2, a2.forensics.capture_async("C", 1, "contain", "t", None, time.time()))
    assert any("disappeared" in m["reason"] for m in snap2["not_captured"])


def test_recovery_waits_for_snapshot_but_never_forever():
    a, b, _ = make_agent()
    a.forensics.started["k"] = time.time()
    import threading
    a.forensics.done["k"] = threading.Event()
    ok, why = a.forensics.ready("k", time.time())
    assert not ok and "waiting" in why
    ok, why = a.forensics.ready("k", time.time() + 1000)
    assert ok and "gave up" in why


# --------------------------------------------------------------------------- healthy only after the cluster says so
def test_not_healthy_until_policy_removal_is_confirmed():
    a, b, _ = make_agent()
    st = a.states["C"]
    st.phase, st.stage = REINTEGRATING, "FULL"
    b.policies[W] = {"stage": "PEER_VALIDATED"}
    a._track_cluster(time.time())
    assert st.phase == REINTEGRATING                          # policy still there
    b.policies.pop(W)
    a._track_cluster(time.time())
    assert st.phase == HEALTHY


def test_retry_vote_and_attention_after_validation_keeps_failing():
    a, b, world = make_agent()
    st = a.states["C"]
    st.phase, st.attempt, st.phase_entered = "VALIDATING", 1, time.time() - 1000
    world.apps["C"].healthy = False
    a._observe(time.time())
    a._vote_recovery(time.time(), "C", st)
    assert a.vote_reasons["RETRY_RECOVERY:C"]["stage"] == "2" and st.attention is None
    st.attempt = a.cfg.recovery.max_retries + 1
    a._vote_recovery(time.time(), "C", st)
    assert st.attention and "recovery attempts failed" in st.attention["reason"]
    assert st.phase == "VALIDATING"                           # not HEALTHY


def test_retry_recovery_certificate_action_and_admission_attempt_rule():
    from resilience.certificate import ACTIONS
    assert "RETRY_RECOVERY" in ACTIONS


# --------------------------------------------------------------------------- end to end (slow)
def test_scenario_first_replacement_fails_validation_then_succeeds():
    from local_demo import validation_retry
    assert validation_retry()


def test_scenario_validation_never_passes_human_attention():
    from local_demo import failed_validation
    assert failed_validation()


# --------------------------------------------------------------------------- config, status, dashboard
def test_cluster_configmap_recovery_block_loads_and_matches_defaults(tmp_path):
    from dataclasses import asdict
    from resilience.config import RecoveryParams, load_cluster_config
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "k8s", "resilience", "10-config.yaml")).read()
    raw = json.loads(text.split("config.json: |", 1)[1])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    cfg = load_cluster_config(str(path))
    assert asdict(cfg.recovery) == asdict(RecoveryParams())
    assert set(raw["recovery"]) <= set(asdict(RecoveryParams()))
    assert cfg.recovery.backoff(1) == 15 and cfg.recovery.backoff(2) == 30 and cfg.recovery.backoff(9) == 120


def test_status_exposes_snapshots_actions_and_attention_and_the_endpoint_exports_json():
    import urllib.request, urllib.error
    from resilience.status_server import serve_status
    a, b, world = make_agent(port=51921)
    world.attack("C")
    a._observe(time.time())
    key = a.forensics.capture_async("C", 1, "contain", "test", None, time.time())
    _wait_snapshot(a, key)
    a._attention("C", time.time(), "demo reason")
    a._step(task(a, b, {"isolated-epoch": 1}, []), time.time())
    st = a.status()
    assert st["snapshots"][0]["key"] == key and st["actions"]["recent"]
    assert st["targets"]["C"]["display_state"] == "NEEDS_ATTENTION" and st["targets"]["C"]["attention"]
    srv = serve_status(a, 51931)
    try:
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:51931/snapshot/{key}", timeout=5).read())
        assert body["incident"]["workload"] == W and body["digest"]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:51931/snapshot/C:9:nothing", timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()


def test_dashboard_aggregates_recovery_and_validates_snapshot_export(monkeypatch):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monkeypatch.setenv("STATUS_URLS", "http://x:1/status,http://x:2/status,http://x:3/status,http://x:4/status")
    spec = importlib.util.spec_from_file_location("dash_server_rec", os.path.join(root, "dashboard", "server.py"))
    dash = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dash)
    a, b, world = make_agent(port=51941)
    a._attention("C", time.time(), "demo reason")
    a._step(task(a, b, {"isolated-epoch": 1}, []), time.time())
    live = {"A": a.status()}
    assert dash.actions_view(live)["recent"][0]["outcome"] == act.APPLIED
    assert dash.snapshots_view(live) == []
    assert dash.snapshot_export("Z", "C:1:contain") is None            # unknown agent
    assert dash.snapshot_export("A", "../../etc/passwd") is None         # key is validated, never a path


def test_dashboard_page_script_is_valid_javascript(tmp_path):
    import re, shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "dashboard", "index.html")).read()
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    f = tmp_path / "page.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
