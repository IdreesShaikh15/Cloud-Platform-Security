"""Phase G: six healthcare workloads (A-F) watched by exactly four identical agents (A-D), 3-of-4 quorum unchanged."""
import copy
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "sim"))
from resilience import workloads as W          # noqa: E402
from resilience.config import load_cluster_config   # noqa: E402

FOUR = os.path.join(ROOT, "config", "workloads-four.json")


def errors_of(raw):
    with pytest.raises(W.WorkloadConfigError) as e:
        W.validate(raw, "t")
    return e.value.errors


def test_default_is_six_and_four_profile_is_the_phase_f_one():
    six, four = W.load(), W.load(FOUR)
    assert six.nodes() == list("ABCDEF") and four.nodes() == list("ABCD")
    assert six.name("E") == "lab-results" and six.name("F") == "medication-service"
    assert six.display("E") == "Laboratory Results Service" and six.display("F") == "Medication Service"
    assert [four.name(n) for n in "ABCD"] == ["patient-portal", "auth-service", "records-api", "database"]
    assert sorted(w.recovery_priority for w in six.workloads) == [1, 2, 3, 4, 5, 6]


def test_optional_workloads_may_be_left_out_but_need_a_zone_and_known_dependencies():
    d = copy.deepcopy(W.DEFAULTS)
    d["workloads"]["E"].pop("zone", None)
    assert any("zone" in e for e in errors_of(d))
    d = copy.deepcopy(W.DEFAULTS)
    d["workloads"]["E"]["depends_on"] = ["nonexistent-role"]
    assert errors_of(d)
    d = copy.deepcopy(W.DEFAULTS)
    d["workloads"]["A"]["needs_for"] = {"database": "x"}              # not one of A's dependencies
    assert errors_of(d)


def test_only_a_to_f_exist():
    d = copy.deepcopy(W.DEFAULTS)
    d["workloads"]["G"] = copy.deepcopy(d["workloads"]["F"])
    assert errors_of(d)


def test_impact_wording_comes_from_the_dependencies():
    wl = W.load()
    assert wl.node_depends_on("E") == ["B", "D"] and wl.node_depends_on("F") == ["B", "D"]
    assert wl.node_needs_for("A")["E"] == "show laboratory results"
    assert wl.node_needs_for("A")["F"] == "show medications"


def test_generated_files_are_current_and_four_profile_is_byte_identical_to_phase_f_apps():
    r = subprocess.run([sys.executable, "scripts/gen-workloads.py", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    apps = open(os.path.join(ROOT, "k8s/base/10-healthcare-apps.yaml"), encoding="utf-8").read()
    assert "name: lab-results" in apps and "name: medication-service" in apps
    assert 'resilience.io/zone: "b"' in apps and 'resilience.io/zone: "c"' in apps
    assert "LAB_URL" in apps and "MEDICATION_URL" in apps


def test_rbac_and_config_list_all_six():
    rbac = open(os.path.join(ROOT, "k8s/resilience/00-rbac.yaml"), encoding="utf-8").read()
    assert rbac.count('"lab-results"') >= 2 and rbac.count('"medication-service"') >= 2
    assert "resilience-isolate-lab-results" in rbac
    cm = open(os.path.join(ROOT, "k8s/resilience/10-config.yaml"), encoding="utf-8").read()
    block = cm.split("config.json: |\n", 1)[1]
    cfg = json.loads("\n".join(l[4:] for l in block.split("\n")))
    assert sorted(cfg["nodes"]) == list("ABCD") and sorted(cfg["extra_workloads"]) == ["E", "F"]
    assert cfg["quorum"]["n"] == 4 and "agent_name" not in cfg["extra_workloads"]["E"]


def test_cluster_config_has_four_agents_and_six_targets(tmp_path):
    cm = open(os.path.join(ROOT, "k8s/resilience/10-config.yaml"), encoding="utf-8").read()
    block = cm.split("config.json: |\n", 1)[1]
    p = tmp_path / "c.json"
    p.write_text("\n".join(l[4:] for l in block.split("\n")))
    c = load_cluster_config(str(p))
    assert list(c.nodes) == list("ABCD") and list(c.targets) == list("ABCDEF")
    assert c.workload_of("E") == "lab-results" and c.node_of_workload("medication-service") == "F"
    assert c.node_of_agent("agent-e") is None


def test_agent_reference_in_extra_workloads_is_refused(tmp_path):
    cm = open(os.path.join(ROOT, "k8s/resilience/10-config.yaml"), encoding="utf-8").read()
    block = cm.split("config.json: |\n", 1)[1]
    raw = json.loads("\n".join(l[4:] for l in block.split("\n")))
    raw["extra_workloads"]["A"] = raw["extra_workloads"]["E"]
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    with pytest.raises((ValueError, TypeError)):
        load_cluster_config(str(p))


def test_app_roles_serve_synthetic_data_over_real_processes():
    import socket
    import urllib.request

    def port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    roles = ["auth-service", "database", "lab-results", "medication-service", "patient-portal"]
    ports = {r: port() for r in roles}
    env = dict(os.environ, AUTH_URL=f"http://127.0.0.1:{ports['auth-service']}", DATABASE_URL=f"http://127.0.0.1:{ports['database']}",
               LAB_URL=f"http://127.0.0.1:{ports['lab-results']}", MEDICATION_URL=f"http://127.0.0.1:{ports['medication-service']}",
               RECORDS_URL="http://127.0.0.1:1", APP_ROOT=os.path.join(ROOT, "apps", "src"))
    procs = [subprocess.Popen([sys.executable, os.path.join(ROOT, "apps", "src", "app.py")], env=dict(env, ROLE=r, PORT=str(ports[r])),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for r in roles]
    try:
        time.sleep(1.5)
        get = lambda r, path: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{ports[r]}{path}", timeout=5).read())   # noqa: E731
        labs = get("patient-portal", "/api/labs?limit=15")
        meds = get("patient-portal", "/api/medications?limit=15")
        assert labs["synthetic"] and labs["source"] == "Synthea" and len(labs["results"]) == 15 and meds["synthetic"] and len(meds["medications"]) == 15
        assert {"mrn", "date", "loinc", "test", "value", "units"} <= set(labs["results"][0])
        with pytest.raises(Exception):                                     # no token -> refused
            get("lab-results", "/results")
    finally:
        for p in procs:
            p.terminate()


def test_simulator_every_agent_watches_every_workload_and_quorum_is_unchanged():
    code = ("import world; c=world.LocalCluster(base_port=50951,cfg=world.fast_config(50951)); "
            "print(sorted(c.agents), sorted(c.agents['A'].states), sorted(c.agents['D'].states), c.cfg.quorum.quorum, c.cfg.quorum.n); c.stop()")
    r = subprocess.run([sys.executable, "-c", code], cwd=os.path.join(ROOT, "sim"), capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "agent")})
    assert r.returncode == 0, r.stderr[-800:]
    out = r.stdout.strip().splitlines()[-1]
    assert out == "['A', 'B', 'C', 'D'] ['A', 'B', 'C', 'D', 'E', 'F'] ['A', 'B', 'C', 'D', 'E', 'F'] 3 4"


def test_scenarios_are_listed_only_for_the_six_workload_profile():
    code = "import scenarios; print(sorted(scenarios.CATALOGUE))"
    out = lambda extra: subprocess.run([sys.executable, "-c", code], cwd=os.path.join(ROOT, "sim"), capture_output=True, text=True,   # noqa: E731
                                       env={**os.environ, **extra, "PYTHONPATH": os.path.join(ROOT, "agent")}).stdout
    assert "lab-results-tampered" in out({}) and "database-cascade" in out({}) and "medication-failed-logins" in out({})
    assert "lab-results-tampered" not in out({"WORKLOADS_CONFIG": FOUR})


def test_evaluation_harness_pins_the_four_workload_profile():
    src = open(os.path.join(ROOT, "scripts", "evaluate.py"), encoding="utf-8").read()
    assert "workloads-four.json" in src


def test_dashboard_has_cards_and_colours_for_e_and_f():
    html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
    assert "--node-E" in html and "--node-F" in html and "n.has_agent === false" in html and "needs_for" in html
