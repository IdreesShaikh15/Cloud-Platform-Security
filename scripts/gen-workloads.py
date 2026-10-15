#!/usr/bin/env python3
"""Render the Kubernetes pieces that name the workloads from the ONE configuration file, config/workloads.json.

    python3 scripts/gen-workloads.py --check      # validate, and fail if any generated file is out of date (default)
    python3 scripts/gen-workloads.py --write      # regenerate the files below
    python3 scripts/gen-workloads.py --names      # print the workload names and the client name, one per line (for shell scripts)
    python3 scripts/gen-workloads.py --config my-workloads.json --check

Generated (and only these; everything else in them is left as it is):
  k8s/base/10-healthcare-apps.yaml      the four workloads, their Services, the client probe   (whole file)
  k8s/resilience/10-config.yaml         nodes.<A-D>.workload / role / telemetry_url, client_stats_url   (inside the config.json block)
  k8s/resilience/00-rbac.yaml           the resourceNames lists marked `# gen:workload-names` / `# gen:policy-names`

With the default configuration the output is byte-identical to the committed files (tests/test_workloads.py checks it).
The configuration is validated first; an invalid or conflicting one is rejected and nothing is written.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from resilience import workloads as W  # noqa: E402

APPS = os.path.join(ROOT, "k8s", "base", "10-healthcare-apps.yaml")
CONFIG = os.path.join(ROOT, "k8s", "resilience", "10-config.yaml")
RBAC = os.path.join(ROOT, "k8s", "resilience", "00-rbac.yaml")

APPS_HEADER = ("# Healthcare workloads: one per resilience node (Minikube node labelled resilience.io/zone).\n"
               "# GENERATED from config/workloads.json by scripts/gen-workloads.py: edit the configuration, not this file.\n")

ENV_BLOCK = """        - {{name: ROLE, value: "{role}"}}
        - {{name: AUTH_URL, value: "http://{auth}:8080"}}
        - {{name: RECORDS_URL, value: "http://{records}:8080"}}
        - {{name: DATABASE_URL, value: "http://{database}:8080"}}
        - {{name: PORTAL_URL, value: "http://{portal}:8080"}}
        - name: POD_IP
          valueFrom: {{fieldRef: {{fieldPath: status.podIP}}}}
        - name: POD_NAME
          valueFrom: {{fieldRef: {{fieldPath: metadata.name}}}}
"""

DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: healthcare
  labels: {labels}
spec:
  replicas: 1
  strategy: {{type: Recreate}}          # recovery must fully replace the compromised pod
  selector: {{matchLabels: {{app: {name}}}}}
  template:
    metadata:
      labels: {tlabels}
    spec:
      nodeSelector: {{resilience.io/zone: "{zone}"}}
      containers:
      - name: app
        image: cr-healthcare-app:1.0
        imagePullPolicy: IfNotPresent
        ports: [{{containerPort: 8080}}]
        env:
{env}        readinessProbe:
          httpGet: {{path: /health, port: 8080}}
          periodSeconds: 2
        resources:
          requests: {{cpu: 50m, memory: 64Mi}}
          limits: {{cpu: 500m, memory: 256Mi}}
---
apiVersion: v1
kind: Service
metadata: {{name: {name}, namespace: healthcare}}
spec:
  selector: {{app: {name}}}
  ports: [{{port: 8080, targetPort: 8080}}]
"""


def render_apps(wl: "W.Workloads") -> str:
    urls = dict(auth=wl.name_of_role("auth-service"), records=wl.name_of_role("records-api"),
                database=wl.name_of_role("database"), portal=wl.name_of_role("patient-portal"))
    docs = []
    for w in wl.workloads:
        labels = f'{{app: {w.name}, resilience.io/node: "{w.node}"}}'
        docs.append(DEPLOYMENT.format(name=w.name, labels=labels, tlabels=labels, zone=w.node.lower(),
                                      env=ENV_BLOCK.format(role=w.role, **urls)))
    docs.append(DEPLOYMENT.format(name=wl.client, labels=f"{{app: {wl.client}}}", tlabels=f'{{app: {wl.client}, resilience.io/node: "client"}}',
                                  zone="a", env=ENV_BLOCK.format(role="client", **urls)))
    return APPS_HEADER + "---\n".join(docs)


def _split_config(text: str):
    head, _, body = text.partition("config.json: |\n")
    if not body:
        raise SystemExit("10-config.yaml: no 'config.json: |' block found")
    lines = body.split("\n")
    n = 0
    while n < len(lines) and (lines[n].startswith("    ") or not lines[n].strip()):
        n += 1
    region = "\n".join(lines[:n])
    block = "\n".join(l[4:] if l.startswith("    ") else l for l in region.rstrip().split("\n"))
    trailing = region[len(region.rstrip()):]            # whatever whitespace followed the JSON, kept as it was
    tail = ("\n" + "\n".join(lines[n:])) if lines[n:] else ""     # the separator exists only if something follows the block
    return head + "config.json: |\n", block, trailing, tail


def render_config(wl: "W.Workloads", text: str) -> str:
    head, block, trailing, tail = _split_config(text)
    cfg = json.loads(block)
    if json.dumps(cfg, indent=2) != block:
        raise SystemExit("10-config.yaml: the config.json block is not in the canonical json.dumps(indent=2) layout, refusing to rewrite it")
    for w in wl.workloads:
        node = cfg["nodes"][w.node]
        new = {}
        for k, v in node.items():                      # keep the key order; add 'role' right after 'workload'
            if k == "role":
                continue
            new[k] = v
            if k == "workload":
                new["workload"], new["role"] = w.name, w.role
        new["telemetry_url"] = f"http://{w.name}.healthcare.svc.cluster.local:8080"
        for k in ("display_name", "provides", "depends_on", "recovery_priority", "priority_rationale"):     # presentation only
            new.pop(k, None)
        if w.display_name:
            new["display_name"] = w.display_name
        if w.provides:
            new["provides"] = w.provides
        if w.depends_on:
            new["depends_on"] = wl.node_depends_on(w.node)
        if w.recovery_priority:
            new["recovery_priority"] = w.recovery_priority
        if w.priority_rationale:
            new["priority_rationale"] = w.priority_rationale
        cfg["nodes"][w.node] = new
    if "client_stats_url" in cfg:
        cfg["client_stats_url"] = f"http://{wl.client}.healthcare.svc.cluster.local:8080/stats"
    body = json.dumps(cfg, indent=2)
    return head + "\n".join("    " + l for l in body.split("\n")) + trailing + tail


MARK_NAMES = re.compile(r"^(?P<indent>\s*)resourceNames: \[.*\](?P<c>\s+# gen:workload-names)\s*$")
MARK_POLICY = re.compile(r"^(?P<indent>\s*)resourceNames: \[.*\](?P<c>\s+# gen:policy-names)\s*$")


def render_rbac(wl: "W.Workloads", text: str) -> str:
    names = ", ".join(f'"{n}"' for n in wl.names())
    pols = ", ".join(f'"{wl.policy_name(w.node)}"' for w in wl.workloads)
    out, seen = [], {"names": 0, "policy": 0}
    for ln in text.split("\n"):
        m = MARK_NAMES.match(ln)
        if m:
            out.append(f"{m['indent']}resourceNames: [{names}]{m['c']}")
            seen["names"] += 1
            continue
        m = MARK_POLICY.match(ln)
        if m:
            out.append(f"{m['indent']}resourceNames: [{pols}]{m['c']}")
            seen["policy"] += 1
            continue
        out.append(ln)
    if seen["names"] < 2 or seen["policy"] < 1:
        raise SystemExit("00-rbac.yaml: the `# gen:workload-names` / `# gen:policy-names` marker lines are missing")
    return "\n".join(out)


def generate(wl: "W.Workloads", apps=APPS, config=CONFIG, rbac=RBAC) -> dict:
    """path -> new text (nothing is written)."""
    cur = lambda p: open(p, encoding="utf-8").read()          # noqa: E731
    return {apps: render_apps(wl), config: render_config(wl, cur(config)), rbac: render_rbac(wl, cur(rbac))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="workload configuration file (default: config/workloads.json or $WORKLOADS_CONFIG)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="validate and fail if generated files are out of date (default)")
    g.add_argument("--write", action="store_true", help="regenerate the files")
    g.add_argument("--names", action="store_true", help="print the workload names and the client name, one per line")
    a = ap.parse_args(argv)
    try:
        wl = W.load(a.config)
    except W.WorkloadConfigError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    if a.names:
        print("\n".join(wl.names() + [wl.client]))
        return 0
    want = generate(wl)
    drift = []
    for path, new in want.items():
        old = open(path, encoding="utf-8").read()
        if old != new:
            drift.append((path, old, new))
    if a.write:
        for path, _, new in drift:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            print(f"wrote {os.path.relpath(path, ROOT)}")
        print(f"configuration OK ({wl.source}); {len(drift)} file(s) updated, {len(want) - len(drift)} already up to date")
        return 0
    if drift:
        for path, old, new in drift:
            print(f"OUT OF DATE: {os.path.relpath(path, ROOT)}")
            for line in list(difflib.unified_diff(old.splitlines(), new.splitlines(), "committed", "generated", lineterm="", n=0))[:12]:
                print("   " + line)
        print("run: python3 scripts/gen-workloads.py --write", file=sys.stderr)
        return 1
    print(f"configuration OK ({wl.source}); all {len(want)} generated files are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
