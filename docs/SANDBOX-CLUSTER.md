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

## 7. Synthea data and clinician traffic on the cluster (SANDBOX-CLUSTER, item 3)
Same sandbox cluster as above, rebuilt with the Synthea build (`build-images.sh`: **5 files hashed**; `upgrade-app.sh`: agents off, manifest applied, `verify-baseline` OK, webhook restarted, agents on).
* **The new data does not make the agents isolate healthy services.** An 11-minute window (142 samples, one every ~4.6 s, taken from the dashboard's own Live channel; the client pod running its three clinicians the whole time): every workload stayed `HEALTHY`, telemetry from all 6 workloads was reachable in every sample (oldest reading at most 1.0 s), no agent log line mentions containment or isolation, no NetworkPolicy was created. `verify-baseline.py` afterwards: six `ok ... 5 files match the known-good manifest`.
* The measured behaviour against the detection thresholds (largest value seen in the window):

| workload | outbound conns max (limit 8) | data sent max / mean KiB/s (limit 256) | requests/s max / mean | failed logins max (limit 10) | evidence confidence max |
|---|---|---|---|---|---|
| A patient-portal | 1 | 38.6 / 21.7 | 9.0 / 3.63 | 0 | 0.0 |
| B auth-service | 0 | 18.3 / 12.5 | 17.0 / 7.24 | 0 | 0.0 |
| C records-api | 1 | 31.4 / 17.7 | 7.0 / 2.73 | 0 | 0.0 |
| D database | 0 | 24.2 / 15.2 | 8.0 / 3.57 | 0 | 0.0 |
| E lab-results | 1 | 20.7 / 10.4 | 3.0 / 0.52 | 0 | 0.0 |
| F medication-service | 1 | 17.6 / 9.3 | 2.0 / 0.33 | 0 | 0.0 |

* Headroom is large: at most 1 outbound connection (threshold 8), at most 39 KiB/s sent (threshold 256), no failed logins (the 5 % mistyped logins are spread across the three clinicians and never reach 10 in a 10 s window). The request rate (about 3 requests/s on the portal, 7 on the login service) is a chart value only; no rule uses it.
* The services answer with real Synthea records (`source: Synthea`, `synthetic: true`); the Laboratory Results and Medication services read the dataset from the database over HTTP, behind a token from the Login service.
* The Live page's chart section (24 small charts, labelled `LIVE_CLUSTER`) was screenshotted from this cluster through the laptop dashboard script, with "telemetry age 0 s". A bug found while doing it: a helper function named `spark` collided with an older one, which flattened the charts; renamed and a test added.
* Not measured here: kubelet CPU/memory with the new traffic (the Metrics API is not available in the sandbox; the idle numbers in section 5 were taken before the traffic existed), and nothing on WSL.

## 8. Demo-day rehearsal (SANDBOX-CLUSTER, 22 Oct 2026)
The same sandbox cluster, after its VM had been restarted (all pods restarted once; Docker, Minikube and the runc wrapper were brought back with the restart recipe, about 8 minutes until every pod was Ready). Then `scripts/preflight.sh`, `scripts/demo-reset.sh` and every live part of `docs/DEMO-DAY.md` were run in order, exactly as written. Times are seconds from the command.

| Step | What happened (measured) |
|---|---|
| `preflight.sh` on the freshly restarted cluster | caught the real state: all pods "restarted in the last 30 minutes" (the VM restart), leftover incident annotations on `records-api`, and workload E at trust 92. All three were genuine. `RESTART_WINDOW_MIN=1` was needed for the rehearsal because of the VM restart. Runtime about 13 s (with the port checks about 20 s). |
| `demo-reset.sh` from a clean state | 75 s; ended `25 PASS, 0 WARN, 0 FAIL ... READY TO PRESENT` (apart from the explained restarts). From the dirty state left by the centralized comparison (controller running, `auth-service` isolated, annotations, marker): 72 s, all six workloads still matched the manifest, final preflight clean, all trust values 100. |
| Agent down (`scale ... --replicas=0` + forced pod delete) | page showed DEGRADED, "3 of 4 (unreachable: B)" after **4 s**. Without the forced delete it takes **17-33 s** (the agent's 30 s shutdown wait). Back to 4 of 4 **7-31 s** after scaling up. |
| Tripwire on E with agent B down | contained **4 s**, Agreement by A, C, D (B unavailable); clean pod and Recovery Check at **39 s**; Limited **52 s**, Monitored **62 s**, Confirmed by Peers **71 s**, Normal Access Restored **84 s**; the alert is RESOLVED with signers A, C, D; no NetworkPolicy remains. A first run took 92 s to workload trust 100. |
| Lying agent (A accuses B) | `auth-service` never isolated, no NetworkPolicy; the other agents' trust in A fell from 100 starting at about 13-30 s to **0 at 51-60 s**. |
| Centralized comparison | `switch-mode.sh baseline` 6 s; the page says centralized mode (the four agents take about 30 s to disappear); `simulate-compromised-controller.sh B` isolated the **healthy `auth-service` within 2-3 s**. |

**Problems found and fixed during the rehearsal (each is in the commit history):**
1. `scripts/laptop-dashboard.sh` left orphan processes after Ctrl-C or `kill`: the simulator server's child dashboard and the `kubectl port-forward` children kept their ports, and the next start silently showed the **old** page with the old Security Alerts. It now stops every child process. Verified: after a plain `kill` nothing is left; a fresh start shows 0 alerts. (A SIGINT sent to a *background* job is ignored by the shell, so it was verified with SIGTERM; in a real terminal Ctrl-C is the normal way to stop it.)
2. `demo-reset.sh` first switched back to the distributed agents *before* cleaning (they briefly saw the old incident state); it now removes the controller, cleans, then starts the agents.
3. `preflight.sh`: a parsing bug hid the real pod problem as a count mismatch; restart handling now FAILs within a window and WARNs after it; if the cluster does not answer it stops after the first section instead of printing 30 follow-on failures.
4. The runbook originally said deleting an agent pod shows "3 of 4" for a while; it does so for only 2 s because the replacement starts quickly, so the runbook now scales the agent to 0 and force-deletes it.

**Not rehearsed:** forgery and obstruct as live steps (listed as spare segments), anything on WSL/Docker Desktop, the browser clicks themselves (the dashboard API was read instead, plus the earlier screenshots).
