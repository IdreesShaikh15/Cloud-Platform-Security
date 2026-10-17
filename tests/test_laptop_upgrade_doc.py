"""The laptop upgrade sequence must only mention scripts and flags that exist."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = open(os.path.join(ROOT, "docs", "LAPTOP-UPGRADE.md"), encoding="utf-8").read()


def test_every_script_and_flag_in_the_sequence_exists():
    scripts = set(re.findall(r"scripts/([A-Za-z0-9_.-]+\.(?:sh|py))", DOC))
    assert {"build-images.sh", "upgrade-app.sh", "verify-baseline.py", "verify-rbac.sh", "verify-webhook.sh", "verify-isolation.sh",
            "minikube-load.sh", "tripwire-test.sh", "laptop-dashboard.sh", "gen-workloads.py"} <= scripts
    for s in scripts:
        if s == "deploy.sh" or s == "gen-certs.py":                       # mentioned only to say NOT to run them
            continue
        assert os.path.isfile(os.path.join(ROOT, "scripts", s)), s
    for flag, script in (("--test-failsafe", "verify-webhook.sh"), ("--egress-peer", "verify-isolation.sh"), ("--client-pod", "verify-isolation.sh"),
                         ("--agent-pod", "verify-isolation.sh"), ("--target", "verify-rbac.sh"), ("--namespace", "verify-webhook.sh"), ("--check", "gen-workloads.py")):
        assert flag in open(os.path.join(ROOT, "scripts", script), encoding="utf-8").read(), (flag, script)


def test_the_documented_expectations_match_the_scripts():
    up = open(os.path.join(ROOT, "scripts", "upgrade-app.sh"), encoding="utf-8").read()
    for step in ("1/7", "2/7", "3/7", "4/7", "5/7", "6/7", "7/7"):
        assert step in up and step in DOC
    assert "do NOT run scripts/deploy.sh afterwards" in DOC and "quorum-webhook" in up
    assert 'sed -i \'s/\\r$//\' scripts/*.sh' in DOC and "bash scripts/" in DOC
    assert "/mnt/c/Users/IDREES SHAIKH/resilience-platform" in DOC                  # the path with a space is quoted everywhere it is used
    assert not re.search(r'cd /mnt/c/Users/IDREES SHAIKH', DOC)


def test_the_four_workload_fallback_uses_files_that_exist_and_matches_the_script():
    assert "FALLBACK: only the original four workloads" in DOC
    assert os.path.isfile(os.path.join(ROOT, "config", "workloads-four.json"))
    assert "cp config/workloads-four.json config/workloads.json" in DOC and "gen-workloads.py --write" in DOC
    up = open(os.path.join(ROOT, "scripts", "upgrade-app.sh"), encoding="utf-8").read()
    assert "it is deployed but not in config/workloads.json" in up and "it is deployed but not in config/workloads.json" in DOC


def test_upgrade_from_713d0ea_section_matches_the_build():
    assert "FROM 713d0ea" in DOC and "5 files match the known-good manifest" in DOC and "5 files hashed" in DOC
    gen = open(os.path.join(ROOT, "scripts", "gen-baseline-hashes.py"), encoding="utf-8").read()
    assert "hash_tree" in gen and os.path.isfile(os.path.join(ROOT, "apps", "src", "data", "synthea-sample.json"))
    assert "4 files hashed" not in DOC and "4 files match" not in DOC
