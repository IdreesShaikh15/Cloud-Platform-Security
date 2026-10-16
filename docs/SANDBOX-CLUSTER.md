# SANDBOX-CLUSTER results (Phase J)

> **These are SANDBOX-CLUSTER results.** They come from a real Kubernetes cluster (Minikube, 4 nodes, Calico) that I built inside my own
> cloud sandbox, on 2026-10-16. They are **not** simulator results and they are **not** the live-verified results from your own cluster:
> the live table in the presentation guide stays empty for your runs. Differences between this sandbox and your laptop are listed in section 2;
> read them before relying on any number here.

## 1. What was done

| Step | Result |
|---|---|
| Registry access check | Reachable: Docker Hub (rate-limited, HTTP 429 more than once), gcr.io, ghcr.io, `storage.googleapis.com` (Minikube and its preloaded images), `dl.k8s.io`, PyPI. **Denied (HTTP 403 from the egress proxy): `registry.k8s.io`, `quay.io`.** Not a full block: Kubernetes' own images come from Minikube's preloaded tarball, Calico's from Docker Hub. |
| Cluster | `scripts/setup-cluster.sh` (unmodified): **4 nodes, Calico, zones a-d labelled -> "cluster ready"**. Minikube v1.39.0, Kubernetes **v1.34.12** (not "stable"), containerd 2.3.4, Calico v3.32.2. |
| Images | `scripts/build-images.sh` (unmodified): the three images built and loaded into all 4 nodes. |
| Deploy | `scripts/deploy.sh` (unmodified): **6 workloads + client, 4 agents, dashboard, 2 webhook replicas, all Running**; E on node b, F on node c as designed. |
| `verify-baseline.py` | **6 of 6 ok** (4 files match the known-good manifest each), before and after both incidents and after the upgrade. |
| `verify-rbac.sh` | **27 of 27 PASS for each of the 6 workloads** (confirms the generated `resourceNames` for E and F work on a real API server). |
| `verify-webhook.sh` | **7 of 7 PASS** (6 of 6 steps + control) for `records-api`, `lab-results`, `medication-service`; with `--test-failsafe`: **8 of 8** (webhook down: the agent's request is still refused). **Two failures at first, both bugs in the script (section 3).** |
| `verify-isolation.sh` | Real Calico enforcement on `lab-results` and `medication-service`: **16 of 16 PASS each** (before: reachable; isolated: user traffic and the workload's own egress blocked, agents still reach it, kubelet readiness unaffected; after removal: reachable). **One failure at first (DNS probe bug, section 3).** |
| Controlled tripwire test | **Workload E and workload F, both completed the real pipeline** (section 4). |
| `minikube-load.sh` | Requests/limits and node allocation printed; **`kubectl top` (Metrics API) is NOT available in this sandbox**, so usage was read from the kubelet instead (section 5). |
| Upgrade from commit `8d53075` | Reproduced your starting point (4 workloads + webhook from that commit), then upgraded to the latest: **found and fixed a real bug (section 3)**. |
| Laptop two-page dashboard | `scripts/laptop-dashboard.sh` against the live cluster: Live page shows 6 workload cards, **4 of 4 agents connected**, the two real tripwire alerts labelled LIVE CLUSTER, and no simulator controls or walkthrough on the Live page. |

## 2. How this sandbox differs from a normal Minikube setup (honest list)

1. **Kubernetes v1.34.12, not the newest.** The sandbox kernel uses cgroups v1; kubelet 1.35+ refuses it by default.
2. **A `runc` wrapper inside every node container** (sandbox only, not in the project): the sandbox's root has `CAP_SYS_RESOURCE` dropped, so nothing can lower `oom_score_adj` and runc failed for system pods that ask for -997. The wrapper clamps a negative value to 0. Without it the control plane does not start.
3. **Images**: Minikube's `registry.k8s.io` pulls were denied, so I downloaded Minikube's preloaded tarball by hand; Calico images were pulled from Docker Hub (`calico/*`), re-tagged `quay.io/calico/*` and loaded with `minikube image load`; the Python base image came through `mirror.gcr.io` with the proxy's CA added (so pip can verify TLS during the image build) and `docker build --network=host`.
4. **No Metrics API**: a metrics-server I installed from a Docker Hub copy could not be reached by the API server (a sandbox network rule), and it also blocked namespace deletion until removed.
5. The cluster runs inside Docker inside a sandbox VM with 4 CPUs and 16 GB; timings are therefore *not* what a laptop gives.
None of this changes a project file. Your laptop (WSL + Minikube) will not need 1-4; it may need its own proxy or Docker settings, which I cannot see.

## 3. Bugs found on the real cluster (all fixed, each with a test)

| # | Where | What happened on the cluster | Fix | Test |
|---|---|---|---|---|
| 1 | `scripts/verify-webhook.sh` step 5 | "delete a workload" was refused by **RBAC** (the agents have no delete verb since Phase 3) before the webhook was consulted; the script reported "refused, but NOT by the webhook" as a failure. RBAC refusing is stronger protection, not a gap. | Step 5 now accepts a webhook refusal or an RBAC "cannot delete" refusal; steps 1-4 still demand the webhook itself. | `test_delete_refused_by_rbac_counts_as_protection` (new fake-kubectl mode with the real API server's message) |
| 2 | `scripts/verify-isolation.sh` DNS probe | A blocked DNS lookup fails after about 5 s, close to the 6 s alarm; the alarm fired **inside** the error handler and the probe crashed with a traceback instead of printing "blocked" (1 FAIL in 16). | The alarm is cancelled first in every path. | Deterministic slow-error test; **it fails on the old code and passes on the new** |
| 3 | `scripts/upgrade-app.sh` (the real deployment bug) | After upgrading from the 4-workload deployment the **admission webhook still ran on the old configuration** and denied every isolation of a workload it did not know ("agents may only manage the platform's isolation policies ... not 'resilience-isolate-lab-results'"). A tripwire on E was **not contained** until the webhook was restarted (fail-closed, so safe, but the new workloads were unprotected after the documented upgrade). | `upgrade-app.sh` now applies RBAC + configuration (agents off), restarts the webhook and waits for it **before** the agents return, and restarts the dashboard on the new image. | `test_upgrade_restarts_the_webhook_on_the_new_configuration_before_the_agents_return` (recording kubectl stand-in; **fails on the old script**) |
| 4 | `scripts/verify-webhook.sh` step 4b | On a workload that had already been recovered (image already `cr-healthcare-app:known-good`) the "change the image to known-good" patch changed nothing, so the webhook correctly allowed it and the check failed. | The check patches to an image no deployment uses, so it is always a real change. | `test_the_image_check_still_proves_the_webhook_after_a_recovery` |
After fix 3, on the cluster: the isolation of `lab-results` succeeded on the next retry after the webhook restart, and the incident then completed (contain -> validate -> four stages -> normal access).

Not fixed, noted: while the isolation was being refused, the agent logged "redeploy ... (try 1)" about once per second (a tight retry loop, log noise, no harm).

## 4. The real incident, timed (SANDBOX-CLUSTER, real timers: 10 s stage dwell, 1 s tick)

Controlled tripwire (`scripts/tripwire-test.sh E --yes`: one harmless line appended to one marker file with `python -c`), workload E = Laboratory Results Service:

| Time (UTC) | What the agents did |
|---|---|
| 07:55:50 | tripwire written |
| 07:55:52 | `QUORUM CONTAIN on E (epoch 1) by A,B,C,D` (2 s); executor A isolates (NetworkPolicy `QUARANTINE`) and redeploys from the known-good image |
| 07:56:26 | old pod gone, new pod `lab-results-fc4d84895-ldct8` Running (**34 s, almost all of it the pod's 30 s termination grace**: the demo app does not exit on SIGTERM) |
| 07:56:28 | `QUORUM VALIDATE ... by A,B,C,D` (Recovery Check passed) |
| 07:56:38 | RESTRICTED (A,C,D) -> 07:56:49 MONITORED (A,B,C,D) -> 07:56:59 PEER_VALIDATED (A,C,D) -> 07:57:10 FULL (A,B,C,D) |
| 07:57:13 | agent A: **HEALTHY, FULL, epoch 1, no policy** |
Total **83 s** from tripwire to normal access; every step carried a 3-of-4 or 4-of-4 quorum certificate and every cluster change passed the admission webhook. Workload F (Medication Service): 07:57:32 -> normal access about 07:58:54 (**about 82 s**), same sequence. After the replacement `verify-baseline.py` was 6 of 6 ok (the tripwire line is gone with the old pod). A third incident on E after the upgrade (with the corrected webhook) completed the same way (08:13:33 tripwire -> FULL 08:15:55).
For comparison, the simulator's timings are shorter (3 s stages, 2 s fake pod replacement); do not mix the two.

## 5. Resource use with six workloads (SANDBOX-CLUSTER, idle, read from the kubelet)

Source: each node's `/stats/summary` (the Metrics API was not reachable here).
| | CPU (millicores) | Memory (MiB) |
|---|---|---|
| each healthcare workload | 20-26 m | about 18 |
| client probe | 2 m | 19 |
| each agent | about 24 m | 79-83 |
| dashboard | 12 m | 30 |
| each webhook replica | 1 m | 70 |
| **healthcare namespace total** | **142 m** | **127** |
| **resilience namespace total** | **109 m** | **490** |
| whole nodes (incl. Kubernetes system pods) | cr-platform 496 m, others 219-228 m | cr-platform 1410, others 789-842 |
Requests/limits from `scripts/minikube-load.sh` (requests 50m/64Mi, limits 500m/256Mi per workload) and allocation per node: the busiest node requests 1150m of its CPU (28%) and 424Mi of memory (2%). Six workloads fit comfortably in 4 x 2200 MiB; no pod was Pending. **Not measured here:** the same cluster with four workloads (the comparison for the fallback), and anything on your laptop.

## 6. What this does not prove

One cluster, one sandbox, one run of each check; Kubernetes 1.34, not the newest; the sandbox workarounds in section 2; no attack was launched except the harmless tripwire; nothing was run on WSL. Your own live table remains yours to fill.
