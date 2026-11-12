"""Timing profile for the live cluster (docs/DEMO-PROFILE.md): it may change ONLY the three timing keys listed in config/timing-profiles.json,
the shipped values in it must equal the shipped configuration, and scripts/demo-profile.sh must keep its order and guard the context.
Everything runs against a recording stand-in for kubectl (no cluster)."""
import json
import os
import re
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import timing_profile as tp  # noqa: E402
from resilience.config import Timers, TrustParams  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "demo-profile.sh")


def shipped_config() -> dict:
    y = open(os.path.join(ROOT, "k8s", "resilience", "10-config.yaml"), encoding="utf-8").read()
    body = y.split("config.json: |\n", 1)[1]
    lines = []
    for l in body.split("\n"):
        if l.strip() and not l.startswith("    "):
            break
        lines.append(l[4:] if l.startswith("    ") else l)
    return json.loads("\n".join(lines))


def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = json.dumps(v, sort_keys=True)
    return out


def test_default_profile_repeats_the_shipped_values():
    cfg, prof = shipped_config(), tp.load()["default"]
    for g in tp.GROUPS:
        for k, v in (prof.get(g) or {}).items():
            assert cfg[g][k] == v, (g, k)                                   # equals k8s/resilience/10-config.yaml ...
    assert prof["timers"]["stage_dwell_s"] == Timers().stage_dwell_s          # ... and the dataclass default
    assert prof["trust"]["workload_recover_per_s"] == TrustParams().workload_recover_per_s
    assert prof["healthcare_termination_grace_s"] is None                    # Kubernetes' own default (30 s)


def test_presentation_changes_only_the_listed_keys():
    cfg = shipped_config()
    new = tp.apply(cfg, "presentation")
    a, b = flat(cfg), flat(new)
    changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    assert changed == {"timers.stage_dwell_s", "trust.workload_recover_per_s"}, changed
    assert new["timers"]["stage_dwell_s"] == 3 and new["trust"]["workload_recover_per_s"] == 8


def test_no_security_setting_is_in_any_profile():
    allowed = {"timers": {"stage_dwell_s"}, "trust": {"workload_recover_per_s"}}
    assert tp.touched_keys() == allowed
    for p in tp.load().values():
        assert set(p) <= {"description", "timers", "trust", "healthcare_termination_grace_s"}      # no quorum / certificates / recovery / investigation
    cfg = shipped_config()
    new = tp.apply(cfg, "presentation")
    for section in ("quorum", "certificates", "investigation", "recovery", "known_good_image", "process_allowlist", "nodes", "extra_workloads"):
        assert new[section] == cfg[section], section
    for k in ("agent_decay_per_s", "agent_recover_per_s", "invalid_message_penalty", "vote_min_trust", "suspect_below", "workload_decay_per_s"):
        assert new["trust"][k] == cfg["trust"][k], k                          # the trust model's own numbers are untouched
    for k in ("tick_s", "evidence_window_s", "contradiction_grace_s", "executor_stagger_s", "validate_timeout_s", "max_clock_skew_s"):
        assert new["timers"][k] == cfg["timers"][k], k
    assert new["quorum"]["n"] == 4 and new["quorum"]["f"] == 1


def test_switching_back_restores_the_shipped_configuration_exactly():
    cfg = shipped_config()
    assert tp.apply(tp.apply(cfg, "presentation"), "default") == cfg


def test_detect_and_unknown_profile():
    cfg = shipped_config()
    assert tp.detect(cfg) == "default" and tp.detect(tp.apply(cfg, "presentation")) == "presentation"
    odd = tp.apply(cfg, "presentation"); odd["timers"]["stage_dwell_s"] = 5
    assert tp.detect(odd) == "custom"
    with pytest.raises(KeyError):
        tp.apply(cfg, "nonsense")


def test_the_simulator_already_uses_the_presentation_values():
    """Why the profile is safe: the same stage dwell and workload-trust recovery are what sim/world.py's fast_config() runs the 1,140 evaluation trials with."""
    sys.path.insert(0, os.path.join(ROOT, "sim"))
    from world import fast_config
    c = fast_config(50901)
    prof = tp.load()["presentation"]
    assert c.timers.stage_dwell_s == prof["timers"]["stage_dwell_s"]
    assert c.trust.workload_recover_per_s == prof["trust"]["workload_recover_per_s"]


# ------------------------------------------------------------------ the script, against a recording kubectl
STUB = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(json.dumps(a) + "\n")
if a[:2] == ["config", "current-context"]:
    print(os.environ.get("STUB_CONTEXT", "cr-platform")); sys.exit(0)
if "configmap" in a and "get" in a:
    sys.stdout.write(open(os.environ["STUB_CONFIG"]).read()); sys.exit(0)
if "get" in a and "deploy" in a and "jsonpath={.spec.template.spec.terminationGracePeriodSeconds}" in " ".join(a):
    print(os.environ.get("STUB_GRACE", "")); sys.exit(0)
sys.exit(0)
'''


def run(args, tmp_path, ctx="cr-platform", cfg=None):
    k = tmp_path / "kubectl"
    k.write_text(STUB); k.chmod(k.stat().st_mode | stat.S_IEXEC)
    c = tmp_path / "config.json"; c.write_text(json.dumps(cfg or shipped_config()))
    log = tmp_path / "calls.log"
    env = dict(os.environ, KUBECTL=str(k), STUB_LOG=str(log), STUB_CONTEXT=ctx, STUB_CONFIG=str(c))
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    calls = [json.loads(l) for l in open(log)] if log.exists() else []
    return r, calls


def verb(c):
    i = 0
    while i < len(c) and c[i].startswith("-"):
        i += 2 if c[i] in ("-n", "--namespace") else 1
    return c[i] if i < len(c) else ""


def test_script_is_executable_parses_and_has_unix_line_endings():
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", SCRIPT]).returncode == 0
    assert b"\r" not in open(SCRIPT, "rb").read()


def test_wrong_context_is_refused_before_anything_runs(tmp_path):
    r, calls = run(["presentation"], tmp_path, ctx="minikube")
    assert r.returncode == 3 and "refused" in r.stderr
    assert {verb(c) for c in calls} == {"config"}


def test_bad_usage_exits_2(tmp_path):
    assert run([], tmp_path)[0].returncode == 2
    assert run(["turbo"], tmp_path)[0].returncode == 2


def test_dry_run_changes_nothing(tmp_path):
    r, calls = run(["presentation", "--dry-run"], tmp_path)
    assert r.returncode == 0 and "dry run finished" in r.stdout and "[dry-run]" in r.stdout
    assert {verb(c) for c in calls} <= {"config", "get"}, calls                  # no scale/patch/rollout reached kubectl


def test_status_is_read_only_and_names_the_profile(tmp_path):
    r, calls = run(["status"], tmp_path)
    assert r.returncode == 0 and "timing profile now: default" in r.stdout and "stage_dwell_s            = 10" in r.stdout
    assert {verb(c) for c in calls} <= {"config", "get"}
    r, _ = run(["status"], tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, cfg=tp.apply(shipped_config(), "presentation"))
    assert "timing profile now: presentation" in r.stdout


def test_presentation_runs_in_the_safe_order_and_patches_only_timing(tmp_path):
    r, calls = run(["presentation"], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    verbs = [verb(c) for c in calls]
    i_stop = next(i for i, c in enumerate(calls) if verb(c) == "scale" and "--replicas=0" in c)
    i_grace = [i for i, c in enumerate(calls) if verb(c) == "patch" and "deploy" in c]
    i_cfg = next(i for i, c in enumerate(calls) if verb(c) == "patch" and "configmap" in c)
    i_start = next(i for i, c in enumerate(calls) if verb(c) == "scale" and "--replicas=1" in c)
    assert i_stop < min(i_grace) <= max(i_grace) < i_cfg < i_start, "agents off -> workloads -> configuration -> agents on"
    assert len(i_grace) == 6
    for i in i_grace:
        assert calls[i][-1] == '{"spec":{"template":{"spec":{"terminationGracePeriodSeconds":2}}}}'
        assert calls[i][calls[i].index("-n") + 1] == "healthcare" and calls[i][calls[i].index("deploy") + 1] != "client"
    patch = json.loads(calls[i_cfg][calls[i_cfg].index("-p") + 1])
    new = json.loads(patch["data"]["config.json"])
    assert flat(new).keys() == flat(shipped_config()).keys()
    diff = {k for k in flat(new) if flat(new)[k] != flat(shipped_config())[k]}
    assert diff == {"timers.stage_dwell_s", "trust.workload_recover_per_s"}
    assert set(verbs) <= {"config", "get", "scale", "wait", "patch", "rollout"}, set(verbs)      # never apply / delete / exec / annotate


def test_default_removes_the_pod_shutdown_override(tmp_path):
    r, calls = run(["default"], tmp_path, cfg=tp.apply(shipped_config(), "presentation"))
    assert r.returncode == 0
    patches = [c[-1] for c in calls if verb(c) == "patch" and "deploy" in c]
    assert patches and all(p == '{"spec":{"template":{"spec":{"terminationGracePeriodSeconds":null}}}}' for p in patches)
    cm = next(c for c in calls if verb(c) == "patch" and "configmap" in c)
    new = json.loads(json.loads(cm[cm.index("-p") + 1])["data"]["config.json"])
    assert new == shipped_config()


def test_doc_exists_and_lists_exactly_the_changed_timers():
    doc = open(os.path.join(ROOT, "docs", "DEMO-PROFILE.md"), encoding="utf-8").read()
    for needle in ("stage_dwell_s", "workload_recover_per_s", "terminationGracePeriodSeconds", "demo-profile.sh", "presentation", "default"):
        assert needle in doc, needle
