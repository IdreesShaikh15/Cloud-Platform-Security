# Configuring workload names and roles

**One file: `config/workloads.json`.** It defines the four protected workloads. The defaults are the original four and are unchanged.

```json
{
  "client": "client",
  "workloads": {
    "A": {"name": "patient-portal", "role": "patient-portal", "description": "..."},
    "B": {"name": "auth-service",   "role": "auth-service",   "description": "..."},
    "C": {"name": "records-api",    "role": "records-api",    "description": "..."},
    "D": {"name": "database",       "role": "database",       "description": "..."}
  }
}
```

| Field | Meaning |
|---|---|
| node `A`-`D` | The resilience node (peer agent) co-located with the workload. **Fixed**: four identical peer agents, one workload each, 3-of-4 quorum. Exactly A, B, C, D are required. |
| `name` | The Kubernetes Deployment and Service name (what the agents isolate, snapshot and redeploy; what the NetworkPolicy and RBAC `resourceNames` are named after). |
| `role` | The behaviour the container runs. The app image implements exactly four roles (`patient-portal`, `auth-service`, `records-api`, `database`) and each is used by exactly one workload, because the services find each other by role. |
| `client` | Name of the synthetic availability probe. |
| `description` | Free text (at most 200 characters). |
| `display_name` | *(optional, presentation only)* What the dashboard calls it, at most 60 characters, e.g. "Patient Portal". |
| `provides` | *(optional, presentation only)* What it does for patients or staff, at most 80 characters, e.g. "show patient records"; used in the "while it is isolated" sentences. |
| `depends_on` | *(optional, presentation only)* The **roles** this workload calls. Must be known roles, no duplicates, not itself, no cycles. The impact of isolating X is everything that calls X, directly or through another. |
| `recovery_priority` | *(optional, presentation only)* 1-4, all different (or all absent): the suggested order for a human operator. It does **not** change what the agents do. |
| `priority_rationale` | *(optional, presentation only)* Why, at most 200 characters; shown next to the order. Write about dependencies, not clinical urgency. |

The presentation fields never name, select or isolate anything and never change the Kubernetes manifests or RBAC (a test renders the manifests with different display names and checks they are identical); they reach the dashboard through the agents' config (`10-config.yaml`) and `/status`. See `docs/HEALTHCARE.md`.

Namespaces (`healthcare`, `resilience`), the quorum, thresholds and workload defaults are **not** configurable here.

## Validation (invalid or conflicting definitions are rejected)

Every problem is listed, not only the first: missing/unknown nodes; duplicate names or roles ("conflict: name ... is used by more than one workload"); unknown roles; names that are not valid Kubernetes service names (lower-case letters, digits, `-`, start with a letter) or exceed 40 characters; names reserved by the platform (`dashboard`, `central-controller`, `quorum-webhook`, `kubernetes`, `default`, `healthcare`, `resilience`, prefixes `agent-`, `resilience-`, `kube-`); a workload named like the client; unknown keys. A file that was asked for (`--config`, `$WORKLOADS_CONFIG`) but is missing or unreadable is an error, never a silent fallback to the defaults. The agent also rejects a cluster config in which two nodes name the same workload ("conflicting workload definitions").

## How it is applied

`scripts/gen-workloads.py` renders the Kubernetes pieces that carry the names; nothing is hand-edited:

```
python3 scripts/gen-workloads.py --check            # validate; exit 1 if a generated file is out of date, 2 if the config is rejected
python3 scripts/gen-workloads.py --write            # regenerate
python3 scripts/gen-workloads.py --names            # names for shell scripts (scripts/reset.sh uses it)
python3 scripts/gen-workloads.py --config my.json --check
```

Generated: `k8s/base/10-healthcare-apps.yaml` (whole file), `k8s/resilience/10-config.yaml` (per-node `workload`, `role`, `telemetry_url`, `client_stats_url` inside the config JSON), the `resourceNames` lists in `k8s/resilience/00-rbac.yaml` marked `# gen:workload-names` / `# gen:policy-names`. With the default file the output equals the committed files (tested). The simulator (`sim/world.py`) reads the same file (`WORKLOADS_CONFIG` overrides the path), so scenarios and the evaluation use the configured names.

## Changing names on a cluster

1. Edit `config/workloads.json`; run `--write`; commit.
2. `kubectl delete -n healthcare deployment,service <old names>` (renamed workloads are new objects), then apply `k8s/base/10-healthcare-apps.yaml`, `k8s/resilience/00-rbac.yaml`, `k8s/resilience/10-config.yaml`; restart the agents, the controller (if used), the dashboard and `quorum-webhook`.
3. Rebuilding the app image is not needed for a rename; it is needed only if you change what a role does.

## Limits (honest)

* Only renaming/reassigning the four existing roles is supported; adding a fifth workload or a new behaviour would need a new app role and a different node count, which is outside the architecture.
* The renamed deployment was exercised in the simulator and by generated-manifest tests, **not** applied to a real cluster.
