"""Part 4b: workload names and roles come from ONE validated configuration file; the defaults are unchanged."""
import copy
import importlib.util
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from resilience import workloads as W          # noqa: E402
from resilience.config import load_cluster_config   # noqa: E402

GEN = os.path.join(ROOT, "scripts", "gen-workloads.py")
_spec = importlib.util.spec_from_file_location("gen_workloads", GEN)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

HISTORICAL = {"A": "patient-portal", "B": "auth-service", "C": "records-api", "D": "database"}


def custom():
    d = copy.deepcopy(W.DEFAULTS)
    for n, name in zip("ABCD", ["portal-web", "login-svc", "records-svc", "patient-db"]):
        d["workloads"][n]["name"] = name
    d["client"] = "probe"
    return d


def errors_of(raw):
    with pytest.raises(W.WorkloadConfigError) as e:
        W.validate(raw, "t")
    return e.value.errors


def test_defaults_are_the_historical_names():
    wl = W.load()
    assert {w.node: w.name for w in wl.workloads} == HISTORICAL
    assert {w.node: w.role for w in wl.workloads} == HISTORICAL
    assert wl.client == "client"
    assert W.validate(W.DEFAULTS).as_dict() == wl.as_dict()      # built-in fallback == the committed file


def test_committed_file_matches_builtin_defaults():
    raw = json.load(open(W.default_path()))
    assert W.validate(raw).as_dict() == W.validate(W.DEFAULTS).as_dict()


def test_custom_valid_config_accepted():
    wl = W.validate(custom())
    assert wl.name("A") == "portal-web" and wl.node_of_role("auth-service") == "B" and wl.policy_name("D") == "resilience-isolate-patient-db"


@pytest.mark.parametrize("mut,frag", [
    (lambda d: d["workloads"].pop("D"), "missing"),
    (lambda d: d["workloads"].update(E=dict(d["workloads"]["A"])), "unknown node"),
    (lambda d: d["workloads"]["B"].update(name="patient-portal"), "name 'patient-portal' is used by more than one"),
    (lambda d: d["workloads"]["B"].update(role="patient-portal"), "role 'patient-portal' is used by more than one"),
    (lambda d: d["workloads"]["B"].update(role="mystery"), "unknown role"),
    (lambda d: d["workloads"]["B"].update(name="Bad_Name"), "not a valid Kubernetes"),
    (lambda d: d["workloads"]["B"].update(name="1abc"), "not a valid Kubernetes"),
    (lambda d: d["workloads"]["B"].update(name="x" * 41), "longer than"),
    (lambda d: d["workloads"]["B"].update(name="dashboard"), "reserved"),
    (lambda d: d["workloads"]["B"].update(name="agent-z"), "reserved"),
    (lambda d: d["workloads"]["B"].update(name="client"), "collides with the client"),
    (lambda d: d["workloads"]["B"].pop("name"), "'name' is required"),
    (lambda d: d["workloads"]["B"].pop("role"), "'role' is required"),
    (lambda d: d["workloads"]["B"].update(color="red"), "unknown key"),
    (lambda d: d.update(namespace="x"), "namespaces are not configurable"),
    (lambda d: d.update(client="kube-x"), "reserved"),
    (lambda d: d["workloads"]["A"].update(name=5), "must be a string"),
])
def test_invalid_rejected(mut, frag):
    d = copy.deepcopy(W.DEFAULTS)
    mut(d)
    assert any(frag in e for e in errors_of(d)), errors_of(d)


def test_all_problems_listed_not_only_first():
    d = copy.deepcopy(W.DEFAULTS)
    d["workloads"]["B"].update(name="patient-portal", role="nope")
    d["workloads"]["C"]["name"] = "BAD"
    assert len(errors_of(d)) >= 3


def test_non_object_rejected():
    assert errors_of([]) == ["the file must contain a JSON object"]


def test_explicit_missing_file_is_error_not_fallback(tmp_path, monkeypatch):
    with pytest.raises(W.WorkloadConfigError):
        W.load(str(tmp_path / "nope.json"))
    monkeypatch.setenv("WORKLOADS_CONFIG", str(tmp_path / "nope.json"))
    with pytest.raises(W.WorkloadConfigError):
        W.load()


def test_unparseable_file_is_error(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{not json")
    with pytest.raises(W.WorkloadConfigError):
        W.load(str(p))


def test_env_var_selects_file(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    p.write_text(json.dumps(custom()))
    monkeypatch.setenv("WORKLOADS_CONFIG", str(p))
    assert W.load().name("C") == "records-svc"


# ---- generator

def test_generator_default_output_is_committed_files():
    out = gen.generate(W.load())
    for path, new in out.items():
        assert open(path, encoding="utf-8").read() == new, path


def test_generated_apps_has_one_notice_line():
    cur = open(os.path.join(ROOT, "k8s/base/10-healthcare-apps.yaml"), encoding="utf-8").read().splitlines()
    assert sum(1 for l in cur if l.startswith("# GENERATED from config/workloads.json")) == 1


def test_generator_custom_config_renders_every_name():
    wl = W.validate(custom())
    out = gen.generate(wl)
    apps = out[gen.APPS]
    for old in HISTORICAL.values():
        assert f"name: {old}\n" not in apps and f"app: {old}\n" not in apps
    for new in wl.names():
        assert f"name: {new}" in apps
    cfg = out[gen.CONFIG]
    jtxt = cfg.split("config.json: |\n", 1)[1]
    j = json.loads("\n".join(l[4:] for l in jtxt.splitlines() if l.startswith("    ")))
    assert {n: s["workload"] for n, s in j["nodes"].items()} == dict(zip("ABCD", wl.names()))
    assert j["nodes"]["A"]["telemetry_url"] == "http://portal-web.healthcare.svc.cluster.local:8080"
    assert j["client_stats_url"].startswith("http://probe.")
    rbac = out[gen.RBAC]
    assert '"resilience-isolate-portal-web"' in rbac and '"patient-portal"' not in rbac


def test_generated_config_loads_with_roles_and_rejects_duplicates(tmp_path):
    wl = W.validate(custom())
    cfg = gen.generate(wl)[gen.CONFIG]
    jtxt = cfg.split("config.json: |\n", 1)[1]
    j = json.loads("\n".join(l[4:] for l in jtxt.splitlines() if l.startswith("    ")))
    p = tmp_path / "c.json"
    p.write_text(json.dumps(j))
    c = load_cluster_config(str(p))
    assert {n: s.workload for n, s in c.nodes.items()} == dict(zip("ABCD", wl.names()))
    assert c.nodes["B"].role == "auth-service"
    j["nodes"]["B"]["workload"] = j["nodes"]["A"]["workload"]
    p.write_text(json.dumps(j))
    with pytest.raises(ValueError, match="conflicting workload definitions"):
        load_cluster_config(str(p))


def run_gen(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, GEN, *args], cwd=ROOT, capture_output=True, text=True, env=e, timeout=60)


def test_cli_check_passes_on_defaults():
    r = run_gen("--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_names():
    assert run_gen("--names").stdout.split() == list(HISTORICAL.values()) + ["client"]


def test_cli_rejects_invalid_config_with_exit_2_and_writes_nothing(tmp_path):
    d = copy.deepcopy(W.DEFAULTS)
    d["workloads"]["B"]["name"] = "patient-portal"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(d))
    before = {f: open(os.path.join(ROOT, f), "rb").read() for f in ("k8s/base/10-healthcare-apps.yaml", "k8s/resilience/10-config.yaml")}
    r = run_gen("--config", str(p), "--write")
    assert r.returncode == 2 and "REJECTED" in r.stderr and "conflict" in r.stderr
    assert before == {f: open(os.path.join(ROOT, f), "rb").read() for f in before}


def test_cli_custom_config_reports_drift_without_writing(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps(custom()))
    r = run_gen("--config", str(p), "--check")
    assert r.returncode == 1 and "OUT OF DATE" in r.stdout


# ---- the simulator and agents use the configured names

def test_simulator_uses_configured_names(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps(custom()))
    code = ("import sys; sys.path.insert(0,'sim'); sys.path.insert(0,'agent'); import world, scenarios;"
            "print(world.WORKLOADS); print(world.AUTH_NODE); print(scenarios.CATALOGUE['app-compromise'][0].title)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "WORKLOADS_CONFIG": str(p)})
    assert r.returncode == 0, r.stderr
    assert "portal-web" in r.stdout and "records-svc" in r.stdout and "B" in r.stdout


def test_dashboard_card_shows_role_only_when_it_differs():
    html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
    assert "n.role && n.role !== n.workload" in html


def test_docs_exist_and_name_the_one_file():
    t = open(os.path.join(ROOT, "docs", "WORKLOADS.md"), encoding="utf-8").read()
    assert "config/workloads.json" in t and "gen-workloads.py" in t
