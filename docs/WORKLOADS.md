# Configuring workload names and roles

**One file: `config/workloads.json`.** It defines the protected workloads: the original four (A-D) and, since Phase G, two optional ones (E, F).
The default file now defines **six**; `config/workloads-four.json` is the exact four-workload profile of Phases A-F (kept so the recorded
evaluation stays reproducible: `scripts/evaluate.py` pins it unless `$WORKLOADS_CONFIG` says otherwise).

**Agents and workloads are separate things.** There are always exactly **four identical peer agents (A, B, C, D)** and the 3-of-4 quorum is
unchanged. Every agent watches **every** workload (A-F); E and F have no agent of their own, so a change to E or F needs 3 of the same
4 agents to agree, signed, exactly like A-D. Nothing about the quorum, trust, certificates or the admission webhook depends on how many workloads there are.

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
| node `A`-`D` | The resilience node (peer agent) co-located with the workload. **Fixed**: four identical peer agents, 3-of-4 quorum. Exactly A, B, C, D are required. |
| node `E`, `F` | *(optional, Phase G)* Extra workloads with **no agent of their own**; all four agents watch them. `E` is the Laboratory Results Service, `F` the Medication Service in the default file. Either, both or neither may be present; each needs a `zone`. |
| `name` | The Kubernetes Deployment and Service name (what the agents isolate, snapshot and redeploy; what the NetworkPolicy and RBAC `resourceNames` are named after). |
| `role` | The behaviour the container runs. The app image implements these roles: `patient-portal`, `auth-service`, `records-api`, `database` (always used, each by exactly one workload) and the optional `lab-results` (Laboratory Results Service) and `medication-service` (Medication Service). Services find each other by role. |
| `client` | Name of the synthetic availability probe. |
| `description` | Free text (at most 200 characters). |
| `display_name` | *(optional, presentation only)* What the dashboard calls it, at most 60 characters, e.g. "Patient Portal". |
| `provides` | *(optional, presentation only)* What it does for patients or staff, at most 80 characters, e.g. "show patient records"; used in the "while it is isolated" sentences. |
| `depends_on` | *(optional, presentation only)* The **roles** this workload calls. Must be known roles, no duplicates, not itself, no cycles. The impact of isolating X is everything that calls X, directly or through another. |
| `zone` | *(E and F only)* Which Minikube node (`a`-`d`, the `resilience.io/zone` label) the workload runs on. A-D run on the node named after them. |
| `needs_for` | *(optional, presentation only)* For a dependency in `depends_on`: what this workload needs it for, e.g. `{"lab-results": "show laboratory results"}`. Used only to word the impact sentence ("the Patient Portal cannot show laboratory results"). |
| `recovery_priority` | *(optional, presentation only)* 1 to the number of workloads, all different (or all absent): the suggested order for a human operator. It does **not** change what the agents do. |
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

Generated: `k8s/base/10-healthcare-apps.yaml` (whole file), `k8s/resilience/10-config.yaml` (per-node `workload`, `role`, `telemetry_url`, `client_stats_url`, and the `extra_workloads` block for E/F inside the config JSON), the `resourceNames` lists in `k8s/resilience/00-rbac.yaml` marked `# gen:workload-names` / `# gen:policy-names`. With the default file the output equals the committed files (tested). The simulator (`sim/world.py`) reads the same file (`WORKLOADS_CONFIG` overrides the path), so scenarios and the evaluation use the configured names.

## Changing names on a cluster

1. Edit `config/workloads.json`; run `--write`; commit.
2. `kubectl delete -n healthcare deployment,service <old names>` (renamed workloads are new objects), then apply `k8s/base/10-healthcare-apps.yaml`, `k8s/resilience/00-rbac.yaml`, `k8s/resilience/10-config.yaml`; restart the agents, the controller (if used), the dashboard and `quorum-webhook`.
3. Rebuilding the app image is not needed for a rename; it is needed only if you change what a role does.

## Limits (honest)

* At most six workloads (A-F). More would need more letters, more Minikube capacity and a re-check of the load; they are not supported. The workload set is *not* hot-reloaded: change the file, regenerate, apply, restart (below).
* A new behaviour needs a new app role in `apps/src/app.py` (and an image rebuild); the two optional roles were added that way in Phase G.
* The agents run on four Minikube nodes; E shares node b and F node c with the workloads already there. That is a placement choice, not a security boundary.
* The renamed deployment was exercised in the simulator and by generated-manifest tests, **not** applied to a real cluster.

## Six workloads: what changes and what does not (Phase G)

| | Four workloads (profile `workloads-four.json`, Phases A-F) | Six workloads (default `workloads.json`) |
|---|---|---|
| Agents | 4 identical peers, A-D | **same**: 4 identical peers, A-D |
| Quorum | 3 of 4 | **same**: 3 of 4, unchanged |
| Workloads watched by each agent | 4 | 6 (every agent reads every workload every tick) |
| Telemetry requests per second, whole platform | 32 (4 agents x 4 workloads x 2) | 48 (4 x 6 x 2) |
| Pods | 4 + client | 6 + client |

How the two new services behave (all data synthetic, `apps/src/app.py`): `lab-results` serves `/results` and `medication-service` serves
`/medications`, both token-protected (they verify the token with the Login & Access Service and read the synthetic patient list from the
Clinical Database, exactly like the EHR API). The Patient Portal shows them at `/api/labs` and `/api/medications`; its original
`/api/patients` and the availability probe are unchanged. Impact therefore reads, for the Clinical Database: "the EHR API cannot serve records, the
Laboratory Results Service cannot serve laboratory results, the Medication Service cannot serve medication lists", and the Patient Portal
can no longer show any of them.

Adding them to a running cluster: `scripts/gen-workloads.py --write`; rebuild the app image (`scripts/build-images.sh`, the app gained two roles);
then follow `scripts/upgrade-app.sh` (it applies in the safe order and re-checks the known-good manifest) and apply `k8s/resilience/00-rbac.yaml`
and `10-config.yaml`; restart the four agents, the dashboard and `quorum-webhook`; then `python3 scripts/verify-baseline.py`. Not applied by me to a real cluster.

## Load: what two more workloads cost (Phase G)

**Measured here (this machine, not Minikube)**: `python3 scripts/measure-load.py --seconds 25` starts the real `apps/src/app.py` processes
(4, then 6) and has four copies of the real agent polling code (`HttpTelemetrySource`) read every workload once a second, as the four agents do.
Result of one run (`results/load-local.json`; single run, a laptop-class sandbox, so treat the CPU figure as an order of magnitude):

| | 4 workloads | 6 workloads | difference |
|---|---|---|---|
| HTTP requests per second from the agents (4 agents x workloads x 2) | 32 | 48 | +16 |
| One full poll of all workloads, median | 47.8 ms | 80.0 ms | +32.2 ms |
| One full poll, worst | 148.8 ms | 91.2 ms | not comparable (single worst sample) |
| Workload processes, resident memory (total) | 87.3 MiB | 131.3 MiB | +44.0 MiB |
| Workload processes, CPU (share of one core) | 9.2 % | 17.2 % | +8.0 points (includes the extra polling work they answer) |
| Readings that failed | 0 | 0 | |

**What the manifests ask of Minikube** (arithmetic from `k8s/base/10-healthcare-apps.yaml`, each workload pod requests 50m CPU / 64 MiB and is limited to 500m / 256 MiB):
four workloads request 200m / 256 MiB (limit 2000m / 1024 MiB); six request 300m / 384 MiB (limit 3000m / 1536 MiB), i.e. **+100m CPU and +128 MiB requested, at most +1000m and +512 MiB if both ran at their limit**. The cluster is four nodes of 2 CPUs and 2200 MiB (`scripts/setup-cluster.sh`); E is placed on node `b` and F on node `c`, so those two nodes carry one extra pod each. The agents' own cost grows only by the polling above (each agent does 12 instead of 8 HTTP reads per tick); gRPC traffic between agents does not depend on the number of workloads except that status and evidence cover six targets.

**Not measured by me**: real Minikube usage. I had no cluster in the build environment. On your cluster run `minikube addons enable metrics-server -p cr-platform` and then `scripts/minikube-load.sh` (read-only: `kubectl top`, requests/limits, allocation per node) with four and with six workloads and record the numbers; if a node is short of memory the pod stays Pending, which `kubectl get pods -n healthcare` shows at once. The live-verified results table in the presentation guide stays a placeholder for your own runs.
