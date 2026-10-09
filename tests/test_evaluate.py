"""scripts/evaluate.py: the parts that must be right for the results to be trustworthy (no cluster is started)."""
import importlib.util
import json
import os
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("evaluate", os.path.join(ROOT, "scripts", "evaluate.py"))
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_wilson_interval_is_sane():
    lo, hi = ev.wilson(0, 20)
    assert lo == 0 and 0.15 < hi < 0.17                    # 0 of 20 still allows ~16%
    lo, hi = ev.wilson(20, 20)
    assert hi == 1 and 0.83 < lo < 0.85
    assert ev.wilson(5, 10)[0] < 0.5 < ev.wilson(5, 10)[1]
    assert all(x != x for x in ev.wilson(0, 0))             # n = 0 -> NaN, never a made-up number


def test_quantiles_and_empty_distribution_never_invent_a_number():
    assert ev.quant([1, 2, 3, 4], .5) == 2.5 and ev.quant([], .5) is None
    assert "no trial reached it" in ev.dist([None, None])
    assert "n=2" in ev.dist([1.0, None, 3.0])               # None (endpoint never reached) is excluded, and counted


def test_errored_trials_are_excluded_from_rates_but_kept():
    rows = [{"outcome": "completed"}, {"outcome": "error: timeout"}, {"outcome": "not_contained"}]
    assert len(ev.ok(rows)) == 2


def test_paired_variants_share_seeds_so_inputs_are_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RAW", str(tmp_path / "none.jsonl"))
    args = types.SimpleNamespace(trials=3, only="investigation,dvc,scenario", quick=True)
    jobs = ev.plan(args)
    seeds = {}
    for j in jobs:
        seeds.setdefault((j["exp"], j["variant"]), []).append(j["seed"])
    assert seeds[("investigation", "transient-on")] == seeds[("investigation", "transient-off")]
    assert seeds[("investigation", "transient-on")] != seeds[("investigation", "slow-burn-on")]
    assert seeds[("dvc", "accuse-dist")] == seeds[("dvc", "accuse-central")]
    assert seeds[("dvc", "accuse-dist")] != seeds[("dvc", "genuine-dist")]
    assert len({s for (e, _), v in seeds.items() if e == "scenario" for s in v}) == 12   # scenarios never share seeds


def test_inputs_are_a_pure_function_of_the_seed():
    assert ev._inv_params("transient", 4001) == ev._inv_params("transient", 4001)
    assert ev._inv_params("transient", 4001) != ev._inv_params("transient", 4002)


def test_plan_skips_finished_trials_so_a_run_can_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RAW", str(tmp_path / "trials.jsonl"))
    args = types.SimpleNamespace(trials=2, only="crash", quick=True)
    first = ev.plan(args)
    assert len(first) == 6
    with open(ev.RAW, "w") as fh:
        for j in first[:4]:
            fh.write(json.dumps({"exp": j["exp"], "variant": j["variant"], "seed": j["seed"], "outcome": "completed"}) + "\n")
        j = first[4]
        fh.write(json.dumps({"exp": j["exp"], "variant": j["variant"], "seed": j["seed"], "outcome": "error: timeout"}) + "\n")
    assert [(j["variant"], j["seed"]) for j in ev.plan(args)] == [(j["variant"], j["seed"]) for j in first[4:]]   # errored one is re-run


def test_availability_and_isolation_seconds():
    rows = [(0.0, set()), (0.5, {"records-api"}), (1.0, {"records-api"}), (1.5, set())]
    assert ev.availability(rows, ["records-api"]) == 0.5
    assert ev.availability(rows, ["records-api", "database"]) == 0.75
    assert ev.seconds_isolated(rows, "records-api") == 1.0


def test_report_builds_tables_and_never_crashes_on_partial_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "OUT_DIR", str(tmp_path))
    rows = [{"exp": "scenario", "variant": "app-compromise", "seed": 1, "outcome": "not_contained", "ttd_s": 0.3,
             "tti_s": None, "ttr_s": None, "ttv_s": None, "ttf_s": None, "reintegrated": False, "contained": False},
            {"exp": "mixed", "variant": "benign", "seed": 2, "outcome": "completed", "alarm": True, "isolated": False,
             "level": 20, "observers": "AB"}]
    t = ev.tables(rows)
    assert "0/1" in t["scenario"] and "no trial reached it" in t["scenario"]       # a trial that never got there is shown, not dropped
    assert "1/1" in t["mixed"]
    ev.write_csvs(rows)
    assert (tmp_path / "scenario_trials.csv").exists()
