"""Phase I: evaluation-only baselines. They must never exist in the platform, only in the evaluation harness."""
import glob
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(ROOT, "scripts", "evaluate.py")


def load_eval():
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    sys.path.insert(0, os.path.join(ROOT, "sim"))
    spec = importlib.util.spec_from_file_location("evaluate_mod", EVAL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_platform_code_has_no_baseline_switch():
    for path in glob.glob(os.path.join(ROOT, "agent", "**", "*.py"), recursive=True):
        text = open(path, encoding="utf-8").read().lower()
        for word in ("immediate reintegration", "single-agent", "baseline_policy", "evaluate.py", "w_baselines"):
            assert word not in text, f"{word!r} found in {os.path.relpath(path, ROOT)}"
    from importlib import import_module
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    assert import_module("resilience.config").QuorumParams().quorum == 3          # shipped default: 3 of 4, unchanged


def test_importing_the_harness_changes_nothing_in_the_platform():
    code = ("import sys; sys.path[:0]=['agent','sim','scripts']; import evaluate, resilience.agent as A, resilience.reintegration as R; "
            "from world import fast_config; "
            "print(A.can_advance is R.can_advance, fast_config(50991).quorum.quorum)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip().splitlines()[-1] == "True 3"


def test_baselines_are_applied_inside_one_trial_process_only():
    code = ("import sys; sys.path[:0]=['agent','sim','scripts']; import evaluate as E, resilience.agent as A, resilience.reintegration as R; "
            "c=E._baseline_cluster(50981,'single'); q1=c.cfg.quorum.quorum; c.stop(); "
            "ok_before = A.can_advance is R.can_advance; "
            "c=E._baseline_cluster(50991,'immediate'); patched = A.can_advance is not R.can_advance; q2=c.cfg.quorum.quorum; "
            "free = A.can_advance('QUARANTINE', 0.0, 99.0, 0.0, 1.0); c.stop(); "
            "print(q1, ok_before, patched, q2, free)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip().splitlines()[-1] == "1 True True 3 True"        # single: quorum 1; immediate: gates gone, quorum still 3
    # and the parent process (this test) is untouched
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    import resilience.agent as A
    import resilience.reintegration as R
    assert A.can_advance is R.can_advance


def test_plan_has_20_trials_per_variant_with_identical_seeds_per_scenario():
    E = load_eval()
    variants = E.EXPERIMENTS["baselines"][0]
    assert variants == ["genuine-real", "genuine-immediate", "genuine-single", "reinfect-real", "reinfect-immediate",
                        "accuse-real", "accuse-single", "blip-real", "blip-single"]
    seeds = {}
    for v in variants:
        vi = E.group_index("baselines", v, variants)
        seeds[v] = [E.SEED_BASE["baselines"] + vi * 100 + i for i in range(20)]
    for scen in ("genuine", "reinfect", "accuse", "blip"):
        group = [seeds[v] for v in variants if v.startswith(scen + "-")]
        assert all(g == group[0] for g in group) and len(group[0]) == 20


def test_a_baseline_trial_shows_what_it_should(tmp_path):
    spec = {"exp": "baselines", "variant": "accuse-single", "seed": 9700, "port": 51770}
    r = subprocess.run([sys.executable, EVAL, "--trial", json.dumps(spec)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    line = next(l for l in reversed(r.stdout.splitlines()) if l.startswith("TRIAL_RESULT "))
    res = json.loads(line[len("TRIAL_RESULT "):])
    assert res["false_isolation"] is True and res["outcome"] == "completed"          # quorum 1: one false accusation isolates


def test_recorded_results_meet_the_trial_count_and_the_docs_tables_match_the_data():
    E = load_eval()
    rows = list({(r["exp"], r["variant"], r["seed"]): r for r in E.load_raw()}.values())
    d = E.by(rows, "baselines")
    for v in E.EXPERIMENTS["baselines"][0]:
        assert len(E.ok(d.get(v, []))) >= 20, v
    t = E.tables(rows)
    doc = open(os.path.join(ROOT, "docs", "RESULTS.md"), encoding="utf-8").read()
    for key in ("baselines_genuine", "baselines_reinfect", "baselines_false", "baselines_wall"):
        assert f"<!-- BEGIN:{key} -->\n{t[key]}\n<!-- END:{key} -->" in doc, key


def test_the_docs_distinguish_simulator_time_from_wall_clock_and_state_the_negative_results():
    doc = open(os.path.join(ROOT, "docs", "RESULTS.md"), encoding="utf-8").read()
    sec = doc[doc.index("### 4.11"):doc.index("## 5. Honest limitations")]
    for text in ("Simulator time versus wall-clock time", "compressed timeline", "evaluation-only", "does not help once it is over",
                 "close to **true by construction**", "costs roughly half a second of isolation time", "Denominator"):
        assert text in sec, text
