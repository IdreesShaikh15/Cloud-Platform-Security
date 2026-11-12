# Presentation timing profile (live cluster)

One switch for the **live** Minikube cluster. The default timing stays exactly as shipped; the *presentation* profile only shortens three
demo-friendly timers so a tripwire test finishes in roughly **30-40 s instead of about 80 s**. It changes **no security logic**.

```bash
bash scripts/demo-profile.sh status                 # which profile is active now (read-only)
bash scripts/demo-profile.sh presentation           # shorter timing (about 1-2 minutes to apply)
bash scripts/demo-profile.sh default                # back to the shipped timing
bash scripts/demo-profile.sh presentation --dry-run # print every command, change nothing
```
Run it **before** `bash scripts/preflight.sh`, never during a demo. `scripts/preflight.sh` prints the active profile. `scripts/deploy.sh` and
`scripts/upgrade-app.sh` re-apply the shipped configuration (the profile goes back to *default*): run the switch again afterwards.
`scripts/demo-reset.sh` keeps the profile.

## Exactly what changes (and nothing else)

The three keys live in `config/timing-profiles.json`; `scripts/demo-profile.sh` refuses to touch anything not named there.

| Where | Key | default | presentation | What it controls |
|---|---|---|---|---|
| ConfigMap `resilience-config` | `timers.stage_dwell_s` | 10 | **3** | minimum time a workload spends in each of the four reintegration stages |
| ConfigMap `resilience-config` | `trust.workload_recover_per_s` | 2 | **8** | how fast a recovered workload's *workload trust* climbs back (it must reach 20 / 40 / 60 / 80 to enter the next stage) |
| the six healthcare Deployments | `spec.template.spec.terminationGracePeriodSeconds` | not set (Kubernetes' 30 s) | **2** | how long Kubernetes waits after SIGTERM before it kills the old pod |

The switch order is: stop the four agents, set the pod shutdown wait and let the six workloads roll, write the two configuration keys,
start the agents again (fresh pods, trust back to 100, empty alert history), restart the in-cluster dashboard. Stopping the agents first means
nothing reacts to the workloads restarting. The client pod is not touched.

## Why each change is safe

* **Pod shutdown wait 30 s -> 2 s.** The demo workloads are stateless Python web servers whose process (PID 1) ignores SIGTERM, so the 30 s was
  pure waiting: Kubernetes sends SIGTERM, nothing happens, and 30 s later it sends SIGKILL (the sandbox rehearsal measured 34 s for the replacement,
  30 s of it this wait: docs/SANDBOX-CLUSTER.md section 4). It runs *after* the evidence is safe: the agents wait for the forensic snapshot
  (up to `snapshot_timeout_s` = 15 s, unchanged) **before** they patch the Deployment, so the old pod is still running while the snapshot is taken.
  The isolation NetworkPolicy is applied and confirmed before the replacement starts, so the old pod is cut off the whole time it is shutting down.
* **Stage dwell 10 s -> 3 s and workload-trust recovery 2 -> 8 per second.** These are the *same two values the simulator already uses*
  (`sim/world.py fast_config`: `stage_dwell_s=3`, `workload_recover_per_s=8`), so the 1,140 evaluation trials and the paper's staged-return results
  were produced with exactly this ratio (`tests/test_demo_profile.py::test_the_simulator_already_uses_the_presentation_values`). The two values are
  changed together on purpose: a stage needs *both* its dwell time and the next trust threshold, and the thresholds are 20 points apart, so at 2 per
  second the trust climb (20 / 2 = 10 s per stage) would still take 10 s even with a 3 s dwell. At 8 per second the climb takes 2.5 s, so the 3 s dwell
  is the limit. Still required for every stage change, unchanged: the **three-of-four quorum**, the **workload-trust thresholds 20/40/60/80**,
  **no anomaly of 0.3 or more**, the **dwell time** itself (only shorter), the NetworkPolicy of each stage, and the cluster confirming the policy is gone before HEALTHY.
* **Not changed on purpose:** `validate_timeout_s` (90 s). It only matters when a replacement *fails* validation (it decides when to retry);
  on the normal path nothing waits for it, so shortening it would save no time and could trigger a spurious retry on a slow laptop pod start.
  Also unchanged: tick (1 s), executor stagger (3 s per rank, only used when the first executor is down), snapshot wait, retry back-off, evidence window,
  the contradiction grace, every vote threshold, certificates (TTL 300 s), the webhook, RBAC and all agent code.
* `tests/test_demo_profile.py` proves it: the profile changes exactly the two configuration keys, every other key (quorum, certificates, trust rates,
  thresholds, recovery, investigation, nodes) compares equal before and after, and switching back restores the shipped configuration byte for byte.

This is a demonstration setting. It is not claimed to be the right timing for a production system, and the paper's cluster numbers
(83 s, single run) come from the **default** timing.

## Expected timings (tripwire on one workload, `bash scripts/tripwire-test.sh E --yes`)

*Default* is **measured** (docs/SANDBOX-CLUSTER.md section 4, real timers, 16 Oct 2026). *Presentation* is **calculated** from the same timeline by
replacing only the three constants; it has **not** been measured on your laptop. Measure it yourself (below) and keep the numbers you see.

| Step | default | presentation | by design? |
|---|---|---|---|
| tripwire written -> `CONTAIN` quorum (3 of 4 signed) | 2 s | 2 s | yes: agents look once per second; three signed votes are needed |
| isolation applied + snapshot taken | ~1 s | ~1 s | yes (concurrent; snapshot wait capped at 15 s) |
| old pod shut down | **30 s** | **2 s** | no: Kubernetes' default wait, SIGTERM ignored by the demo app |
| new pod created, started and Ready | ~4 s | ~4 s | load-dependent (image start, readiness probe every 2 s) |
| `VALIDATE` quorum | ~2 s | ~2 s | yes: health, new instance, file hashes, no anomaly, then 3 votes |
| four stage changes (each needs dwell **and** quorum **and** trust) | 4 x ~10.5 s = **42 s** | 4 x ~4 s = **16 s** | the dwell is the setting; the quorum round is about 1-2 s |
| FULL -> HEALTHY (cluster confirms the policy is gone) | ~3 s | ~3 s | yes |
| **total, tripwire to normal access** | **~83 s (measured)** | **~30 s (calculated); expect 30-40 s on a busy laptop** | |

How to measure it on the laptop: run the tripwire, then read the times in the dashboard's **Security Alerts -> open the alert** (each step is time-stamped), or
`kubectl -n resilience logs deploy/agent-a | grep -E "QUORUM|HEALTHY"`. Compare the first `QUORUM CONTAIN` line with the line that shows the workload HEALTHY.

## Demo Mode (simulator) is already on short timers

The simulator uses 0.5 s ticks, 3 s stage dwell, workload-trust recovery 8 per second and a 2 s simulated pod replacement, so it needs no profile.
Its scenario durations are by design (see docs/DEMO-TIMING.md section 1b).
