#!/usr/bin/env python3
"""Pre-flight for the file-integrity check: do the RUNNING workloads match the known-good hash ConfigMap the agents hold?

    python3 scripts/verify-baseline.py            # exit 0 = every workload matches; 1 = a mismatch (read the list); 2 = could not check

Why it matters: the agents treat any difference from `known-good-hashes` as a file change. If the app image and the ConfigMap are out of
step (after rebuilding the app image, say), the agents would isolate perfectly healthy workloads. Run this before a demonstration, and after
scripts/upgrade-app.sh. It only READS: `kubectl get configmap` and a read-only `python -c` inside each workload that hashes /app.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KUBECTL = os.environ.get("KUBECTL", "kubectl")
HASH_CODE = ("import json,sys; sys.path.insert(0,'/app'); import telemetry; print(json.dumps(telemetry.hash_tree('/app'), sort_keys=True))")


def run(*args, timeout=60):
    return subprocess.run([KUBECTL, *args], capture_output=True, text=True, timeout=timeout)


def names():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen-workloads.py"), "--names"], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"cannot read the workload names: {out.stderr.strip()}")
    return out.stdout.split()[:-1]                        # every line but the last is a workload (four or six); the last is the client probe


def main() -> int:
    cm = run("-n", "resilience", "get", "configmap", "known-good-hashes", "-o", "jsonpath={.data.known-good-hashes\\.json}")
    if cm.returncode or not cm.stdout.strip():
        print(f"could not read the known-good-hashes ConfigMap: {cm.stderr.strip() or 'empty'}", file=sys.stderr)
        return 2
    baseline = json.loads(cm.stdout)
    bad = 0
    for w in names():
        r = run("-n", "healthcare", "exec", f"deploy/{w}", "--", "python", "-c", HASH_CODE)
        if r.returncode:
            print(f"{w}: could not hash /app: {r.stderr.strip()[:200]}", file=sys.stderr)
            return 2
        actual = json.loads(r.stdout)
        modified = sorted(p for p in baseline if p in actual and actual[p] != baseline[p])
        missing = sorted(p for p in baseline if p not in actual)
        new = sorted(p for p in actual if p not in baseline)
        if modified or missing or new:
            bad += 1
            print(f"MISMATCH {w}: modified={modified} missing={missing} new={new}")
        else:
            print(f"ok       {w}: {len(actual)} files match the known-good manifest")
    if bad:
        print("\nThe agents would treat this as tampering and isolate these workloads. Fix first: scripts/upgrade-app.sh", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
