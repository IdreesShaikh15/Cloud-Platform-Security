# PROGRESS LOG

Plain-English log of each phase: what was done, real test results, files, commit, push status, and
what is **not** verified. Newest phase at the bottom.

## Baseline (before any change)

Measured on the starting commit `6e75930`:

| Check | Result |
|---|---|
| `python -m pytest -q tests` | **57 passed** (130 s) |
| `python sim/local_demo.py` | **6 of 6 scenarios PASS** (app-compromise, false-accusation, forge-evidence, agent-crash, baseline, controller-crash) |

This matches the last-known numbers you gave me. Environment: Python 3.13, grpcio 1.84, in a fresh virtualenv.

---

## Phase 1: Honest audit

**Status: DONE.** Safety tag: `pre-phase-1` (at `6e75930`; no tags existed before).

### What was done
* `docs/AUDIT.md`: every feature classified (VERIFIED LIVE / SIMULATOR-TESTS / PARTIAL / MISSING) with code location, proving test and the gap; isolation-policy analysis (ingress and egress separately, DNS, client pod, agent traffic, kubelet, port-forward); RBAC permission table; error-handling review; known limitations.
* `scripts/verify-isolation.sh`: for you to run on the live cluster (see AUDIT.md section 3.4).
* Five real bugs found and fixed (each has a regression test):
  1. Kubernetes API calls had **no timeout**, so one hung call froze an agent entirely. Now (3 s, 10 s).
  2. **Recovery could run before isolation succeeded**, so the replacement pod could come up un-quarantined. Recovery now waits for confirmed isolation (the test fails if the fix is removed; I checked).
  3. A target stuck because the cluster could not be read was **silent**; now a throttled dashboard event.
  4. **Stale pod IP** in the AUTH signal could look like a burst of failures after recovery.
  5. Isolation let the whole `resilience` namespace (e.g. the dashboard) reach a quarantined workload; now only agent / controller pods on TCP 8080.
* Test blind spots closed: the real HTTP monitoring path (the simulator bypassed it) and executor fail-over (the old agent-crash scenario killed rank 3 and never needed fail-over) are now tested.
* While writing the script's tests I found and fixed two bugs **in the script itself** (an unset variable, and an abnormal abort that exited 0). A test now guards "never report success after an unexpected abort".
* `SETUP.md` said "35 passed / 4 × PASS"; corrected.

### Results (measured)

| Check | Result |
|---|---|
| `python -m pytest -q tests` | **84 passed** (245 s) = 57 baseline + 27 new, 0 failed |
| `python sim/local_demo.py` | **6 of 6 PASS**, same as baseline |
| Regression introduced? | None observed; baseline tests unchanged and green |

### Files changed
`agent/resilience/{agent,detection,reintegration,response}.py` (66 lines added, 18 removed),
`SETUP.md` (2 lines), new: `docs/AUDIT.md`, `docs/PROGRESS.md`, `scripts/verify-isolation.sh`,
`tests/{fake_kubectl.py,test_audit_fixes.py,test_executor_failover.py,test_http_monitoring.py,test_verify_isolation.py}`.
No secrets in the diff (checked).

### What is NOT verified
* **Calico enforcement of the isolation policy.** Nothing here proves it. `verify-isolation.sh` is untested against a real cluster: its logic is tested only with a fake `kubectl`. Please run it and tell me the output.
* The tightened ingress rule (agents only, TCP 8080) has **not** been seen on a real cluster. If `verify-isolation.sh` reports the agent path blocked, that rule is the first suspect.
* The K8s timeouts were tested against a local black-hole server and a mock, not against a hung real API server.
* Your **running pods still have the old code** until you rebuild the agent image and restart the agents (see the end of AUDIT.md).
* Everything classified "SIMULATOR/TESTS" or "PARTIAL" in AUDIT.md remains unproven live.

### Commit / push
* Code + docs commit: **`2bbb89b`** ("Phase 1: honest audit, isolation verification script, five bug fixes").
* Pushed (no force) to **`claude/cyber-resilience-platform-xxs3oh`** (your branch, accepted) and to
  `claude/inspiring-thompson-ik54a3` (the branch this session was started on; same commit).
* **Tag `pre-phase-1`: created locally (points to `6e75930`) but could NOT be pushed**; the remote
  rejected the tag ("remote end hung up", 3 attempts) while branch pushes succeed. The tag lives only in
  the session's container, which is temporary. `6e75930` is permanent in the branch history, so
  you can recreate it anywhere with `git tag pre-phase-1 6e75930`. Later phases use the same mechanism
  and will have the same limitation unless you push the tags yourself.

---

## Phase 2: Targeted investigation before containment

**Status: DONE** (one optional item deliberately not built, see "Not done"). Safety tag: `pre-phase-2`
(at `c03cdde`, the end of Phase 1; local only, tag pushes are rejected by the remote).

### What was done
* **Investigation step** carried out by the same 4 identical peer agents (no specialised agents, still threshold-based, no ML, no database):
  triggers (uncertain W(T) band, split view, single sender), bounded time budget, each agent re-measures only the
  disputed signals at a higher bounded rate, agents request fresh **signed** observations from peers over the existing
  gRPC/mTLS channel (new `Investigate` RPC with `InvestigateRequest` / `InvestigateResponse`), four outcomes
  (CORROBORATED, FALSE_POSITIVE, AMBIGUOUS/watch + human review, UNCERTAIN). Code: `agent/resilience/investigation.py`.
* **Race safety** for every case in the specification (unique id/target/epoch/deadline, dedupe with earliest-id-wins merge,
  idempotent duplicate/late answers, silent peers, invalid signatures, mid-way vote exclusion, incident resolving first,
  stale results bound to epoch + workload instance + expiry, restart, concurrency and cooldown limits).
* **Visibility:** new `INVESTIGATION` timeline category; dashboard "Investigations" panel (question, time left, outcome,
  per-agent verdicts), `WATCH · review` node tag and a HUMAN REVIEW banner; investigation fields in incident metrics
  (`investigations`, `investigation_outcome`, `inv_start_s`, `inv_end_s`, `human_review`) and in `scripts/collect-metrics.py`.
  Verified visually in a real browser against the running simulator (screenshots in `docs/img/`).
* **Simulator scenarios** (simulated telemetry only): `transient-blip`, `slow-burn`, `ambiguous`; `--no-investigation` switches the feature
  off for any scenario. Added `world.inject_connections` (observer-specific, brief or growing simulated network signal).
* **Config switch** `investigation.enabled` (default on) and all parameters in `k8s/resilience/10-config.yaml`.
* **`docs/INVESTIGATION.md`**: plain-English explanation, rules, race table, settings, comparison with majority vote /
  fixed threshold / SOAR playbook, and an honest statement of what is established vs what is specific to this setting.
* Two issues found and fixed while building it: an early-finishing agent refused its peers' later requests (so they ran to the
  deadline) and now serves its signed final answer; a merged duplicate must not start a cooldown.

### Results (measured)

| Check | Result |
|---|---|
| `python -m pytest -q tests` | **131 passed** (406 s) = 84 (Phase 1) + 47 new (35 + 9 + 3); 0 failed |
| `python sim/local_demo.py` | **9 of 9 PASS** (the original 6 + transient-blip, slow-burn, ambiguous) |
| `python sim/local_demo.py --no-investigation` (all 9) | **9 of 9 PASS**; original app-compromise timings unchanged (TTD 0.5 s, TTI 1.5 s, TTR 4.0 s, TTV 5.0 s, TTF 19.0 s) |
| Regression introduced? | none; the 84 earlier tests and the 6 original scenarios passed with investigation ON before the new tests were added |

Scenario comparison (single simulator runs, accelerated timers; statistics come in Phase 5):

| Scenario | Investigation ON | OFF |
|---|---|---|
| transient-blip | closed FALSE POSITIVE 3.0 s after opening, all 4 agents, no isolation | no isolation, two votes stay pending, never resolved |
| slow-burn | confirmed at 7.5 s, **contained at 8.5 s** | contained at **20.5 s** |
| ambiguous | AMBIGUOUS at 9.0 s, watch + human review, no isolation | nothing flagged |
| genuine attack (app-compromise) | **no investigation started**, TTI 1.5 s (not delayed) | same |

### Files changed
New: `agent/resilience/investigation.py`, `docs/INVESTIGATION.md`, `docs/img/*.png`, `tests/test_investigation.py` (35),
`tests/test_investigation_scenarios.py` (9), `tests/test_dashboard_investigations.py` (3).
Modified: `proto/resilience.proto` + regenerated stubs, `agent/resilience/{agent,config,detection,evidence,metrics,monitoring,observability,peer,__main__}.py`,
`dashboard/{server.py,index.html}`, `k8s/resilience/10-config.yaml`, `scripts/collect-metrics.py`, `sim/{world,local_demo}.py`, `docs/{AUDIT,PROGRESS}.md`.
No secrets in the diff (checked).

### Not done / not verified
* **Not done (deliberate, documented):** the optional "apply the existing RESTRICTED NetworkPolicy while watching". It blocks
  all user traffic, so it would have to be its own signed quorum action; the safe partial version (heightened monitoring + human-review flag) is implemented.
* **Not verified on the real cluster.** Everything here is simulator/tests. The sampling rate (4/s for up to 10 s) and the thresholds are untuned against real telemetry; `HttpTelemetrySource.collect_target` is covered only by the existing HTTP test, not by a live run.
* The investigation does not change the trust rule: agents that honestly saw a vantage-limited anomaly still lose some trust when the others do not corroborate it (visible in the "ambiguous" scenario). Documented as a limitation.
* A lying agent can still waste effort by requesting investigations (bounded by max 2 concurrent, 60 s per-target cooldown, refusal of vote-excluded requesters).
* To use it on the cluster: rebuild the **agent** and **dashboard** images, re-apply the ConfigMap, restart the agents (protocol change; mixed old/new agents degrade safely: an old agent simply never answers, which counts as "unknown").

### Commit / push
* Commit **`91ea556`** ("Phase 2: targeted investigation before containment"), pushed (no force) to
  `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.
* Tag `pre-phase-2` exists locally only (the remote rejects tag pushes; recreate with `git tag pre-phase-2 c03cdde`).

---

## Phase 3: Security hardening

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Safety tag `pre-phase-3` (at `7992562`; local only,
tag pushes are rejected by the remote). Full write-up: `docs/SECURITY.md`.

### What was done
* **Quorum certificate** (`agent/resilience/certificate.py`): defined exactly (3 signatures from 3 distinct known peers, all over the same canonical
  statement: workload, action + stage, incident epoch, evidence digest, expiry). Rejects duplicate signers, invalid signatures, unknown peers, expired,
  replayed, wrong target/action/stage, superseded version, revoked signers, non-canonical encodings. Built leaderlessly: after a quorum each agent that voted
  signs the identical, deterministically derived statement (new `SubmitShare` RPC). Assumptions documented: tolerates 1 compromised agent; **2 of 4 break it**.
* **Enforcement, two independent layers:** the executor refuses to act without a verified certificate, **and** a validating admission webhook
  (`admission.py`, `webhook.py`, `k8s/resilience/40-webhook.yaml`, certificates by `scripts/gen-certs.py`) refuses an agent's change unless it carries one:
  isolation policies must be the platform's own policy for the stage, recovery may only touch the recovery fields and set the known-good image, stages and state
  never go backwards, a new isolation must be exactly the next incident version. **Fails closed** (`failurePolicy: Fail`, any error = deny). The simulator's fake
  cluster runs the *same* admission code, so the whole pipeline is tested under enforcement.
* **Least-privilege RBAC** (`00-rbac.yaml`): removed unused pods/list/patch verbs and cluster-wide-in-namespace powers; resourceNames on the 4 workloads, the 4 isolation
  policies and the marker ConfigMap; separate identities for agents, baseline controller and webhook. Test derives needed permissions from the real backend calls.
* **Vote-excluded agents:** evidence rejected entirely (not down-weighted), not counted in any quorum/certificate, still watched so trust moves only through peers'
  observations over time; nothing a sender says raises its own trust; trust state persists across a container restart.
* **Tamper-evident decision log** (`decisionlog.py`): hash chain; `python -m resilience verify-log` reports the first broken record; dashboard chip. Tested for modified,
  deleted, reordered, inserted records; limits (whole-chain rewrite, tail truncation) demonstrated, not hidden.
* **Dashboard token:** optional, off by default, from env/secret file only, protects every API endpoint, session cookie not the token, rate-limited, fails closed on a bad token file.
* **Scripts for you:** `scripts/verify-webhook.sh` (dry runs only; optional `--test-failsafe`), `scripts/verify-rbac.sh` (`kubectl auth can-i`, read-only).

### Results (measured, single clean run on the final code)

| Check | Result |
|---|---|
| `python -m pytest -q tests` | **260 passed** (552 s) = 131 (Phase 2) + 129 new; 0 failed |
| `python sim/local_demo.py` | **9 of 9 PASS** under certificate enforcement |
| Cost of certificates | app-compromise in the simulator: TTD 0.5 s, TTI 1.5 s, TTR 3.5 s, TTV 4.5 s, TTF 18.0 s (before this phase 0.5 / 1.0-1.5 / 3.5-4.0 / 4.5-5.0 / 18.5-19.0); other runs showed TTI up to 2.0 s, so about 0 to 0.5 s extra |
| New tests | certificate 22, admission 23, RBAC 45, trust 8, decision log 11, dashboard auth 7, enforcement 6, verify-webhook 7 |

### Bugs found and fixed while building it (each caught by a test)
1. **Replay hole in my own design:** a still-valid CONTAIN certificate could re-create an isolation after the incident ended (a lone compromised agent could have re-isolated a healthy workload). Fixed: CREATE must be exactly `current incident version + 1` (webhook gets read-only `get` of the 4 workloads), state cannot go backwards.
2. The platform's TLS certificates lacked Authority/Subject Key Identifier, so strict TLS clients (Python 3.13) rejected them. Fixed in `pki.py`.
3. A deadlock in the simulator's fake backend (lock re-entered by the admission callback): fixed with a re-entrant lock.
4. `verify-webhook.sh` exited 0 after a preflight failure (cleanup trap overwrote the status); fixed, same class as Phase 1's script bug.
5. A dashboard token file that was set but unreadable silently left the dashboard open; now it refuses to start.
6. My new "ignored evidence from an excluded agent" event reused the `REJECTED` category and broke an existing test that treats REJECTED as forgery; moved to `FLAG`.
7. **Existing test changed (disclosed):** `test_genuine_compromise_full_cycle` asserted the final stage the instant all agents reported HEALTHY; HEALTHY is the agents' intent and the executor applies the last stage a moment later (the race noted in Phase 1), which the extra certificate round makes more likely. The test now waits for the cluster effect. No product behaviour changed.

### Files changed
New: `agent/resilience/{certificate,admission,webhook,decisionlog}.py`, `k8s/resilience/40-webhook.yaml`, `scripts/verify-{webhook,rbac}.sh`, `docs/SECURITY.md`, `docs/img/dashboard-*.png`,
8 test files + `tests/fake_kubectl_webhook.py`. Modified: `proto/resilience.proto` (+stubs), `agent/resilience/{agent,config,evidence,peer,pki,quorum,response,__main__}.py`, `dashboard/{server.py,index.html}`,
`k8s/resilience/{00-rbac,10-config,30-dashboard}.yaml`, `k8s/baseline/central-controller.yaml`, `scripts/{deploy.sh,gen-certs.py,collect-metrics.py}`, `sim/world.py`, `tests/test_integration_sim.py` (see 7), `docs/{AUDIT,PROGRESS}.md`.
No secrets in the diff (checked).

### What is NOT verified / remaining gaps
* **Never applied to a real API server.** Run `scripts/verify-webhook.sh` and `scripts/verify-rbac.sh` on your cluster. Possible live surprises: webhook TLS/CA wiring, the real shape of objects the API server sends (the policy comparison normalises omitted empty lists, other defaults are untested), the 5 s timeout.
* The scripts' logic is tested with fake `kubectl`s only.
* Cluster admins are **not** held to the rule (by design); 2 of 4 compromised agents break the guarantee; a certificate proves agreement, not correctness.
* While the webhook is down the agents cannot act (fail-closed); break-glass is documented.
* After a stalled recovery the original CONTAIN certificate may have expired (5 min); then a human must intervene (Phase 4 adds retries and "needs human attention").
* Trust state is lost if a pod is deleted (an `emptyDir`); the hash chain cannot stop a full-access rewrite; agents' `:8081` status endpoints stay open to pods in the cluster.
* **To use it:** rebuild the agent image (the webhook runs from it); re-run `python3 scripts/gen-certs.py` (rotates all keys) then `scripts/deploy.sh` (applies RBAC, ConfigMap, webhook, then the webhook configuration last); restart agents and the dashboard.

### Commit / push
* Commit **`dc50f4e`** ("Phase 3: security hardening ..."), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.
* Tag `pre-phase-3` exists locally only (recreate with `git tag pre-phase-3 7992562`).

## Phase 4: Safer recovery

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Safety tag `pre-phase-4` (at `95aa88a`; local only). Full write-up: `docs/RECOVERY.md`.

### What was done
* **Evidence snapshot before replacement** (`forensics.py`): read-only, only pods labelled `app=<workload>`, pod metadata without env/volumes/commands, last 200 log lines with credentials redacted, observed signal values, monitored-file hashes vs known-good, the quorum decision, a digest, and an explicit `not_captured` list (nothing faked). Saved as a `0600` JSON file per agent; listed on the dashboard with an export-JSON button (`/api/snapshot/<agent>/<key>`); a late snapshot never blocks recovery for more than 15 s.
* **Validation failure handling:** a failed replacement triggers a new quorum action `RETRY_RECOVERY` (own 3-signature certificate, checked by the webhook, attempt number can only increase), with exponential back-off (15/30/60 s, cap 120 s), at most 3 retries, then **NEEDS HUMAN ATTENTION** (dashboard card + audit record); the workload stays quarantined throughout. Never two recoveries at once (isolation confirmed, snapshot ready and no rollout in progress are prerequisites; one idempotent task per incident+attempt). HEALTHY is now set only after the cluster is read and the isolation policy is gone.
* **Honest Kubernetes actions** (`actions.py`, `agent.py::_step`): every step writes a hash-chained audit record with an explicit outcome (applied, already_applied, applied_after_timeout, not_applied, failed, unknown, unknown_resolved, refused_stale, refused_certificate, waiting, abandoned). A timeout triggers a read of the actual cluster before anything is repeated; an unreadable cluster shows UNKNOWN. Redeploy is idempotent (template value derived from incident+attempt, no longer a timestamp). Stale incident/attempt tasks are refused and recorded.
* **RBAC:** agents (not the baseline) get read-only `pods` get/list and `pods/log` get in the healthcare namespace; `verify-rbac.sh` checks both sides.
* **Simulator:** 2 new scenarios, `validation-retry` and `failed-validation` (a replacement that reports unhealthy; nothing real misbehaves).

### Results (measured on the final code)

| Check | Result |
|---|---|
| `python -m pytest -q tests` | **298 passed, 1 failed** (685 s): `test_verify_isolation.py::test_ctrl_c_during_isolation_still_cleans_up` timed out at 60 s in this run; it **passes when run alone** (all 15 tests in that file pass, 55 s). It tests `verify-isolation.sh` (not touched in this phase), is timing-sensitive, and I did not investigate the cause further. 260 before + 38 new = 298. |
| `python sim/local_demo.py` | **11 of 11 PASS** (9 before + 2 new) |
| Detection/response timings (app-compromise) | TTD 0.5 s, TTI 1.5 s, TTR 3.5 s, TTV 4.5 s, TTF 18.5 s (unchanged from Phase 3) |
| New tests | `tests/test_recovery.py` 26 (incl. 2 end-to-end scenarios), RBAC +12 |

### Bugs found while building it
1. A patch of mine left `response.py` with a duplicated, truncated `K8sBackend` class; caught when reading the file, repaired before any test run.
2. **Existing test changed (disclosed):** `test_unreadable_cluster_state_is_reported_not_silent` stubbed `read_state`; tracking now uses the strict read (an unreadable cluster must not look like "absent"), so the test stubs `read_state_strict`. Same behaviour asserted.
3. Dashboard screenshot (`docs/img/dashboard-needs-attention.png`) is from the real simulator `failed-validation` run; no JavaScript errors.

### Files changed
New: `agent/resilience/{forensics,actions}.py`, `docs/RECOVERY.md`, `docs/img/dashboard-needs-attention.png`, `tests/test_recovery.py`. Modified: `proto/resilience.proto` (+stubs: `RETRY_RECOVERY`), `agent/resilience/{agent,admission,certificate,config,evidence,observability,response,status_server,__main__}.py`, `dashboard/{server.py,index.html}`, `k8s/resilience/{00-rbac,10-config}.yaml`, `scripts/verify-rbac.sh`, `sim/{world,local_demo}.py`, `tests/{test_rbac,test_audit_fixes}.py`, `docs/AUDIT.md`. No secrets in the diff (checked; the only "secret" strings are fake simulator data used to prove redaction).

### What is NOT verified / remaining gaps
* **Never run on a real API server:** real timeout behaviour, pod readiness, the rollout-in-progress and pods-gone checks against a real Deployment controller, the webhook accepting `RETRY_RECOVERY` with real objects. `K8sBackend` is checked only with mocks. Run `scripts/verify-rbac.sh` (new pod checks) on your cluster.
* Snapshots are per agent in an `emptyDir` (lost if the agent pod is deleted) and redaction is pattern-based.
* "Needs human attention" appears on the dashboard and audit log only; nothing pages anyone.
* The flaky test above.
* **To use it:** rebuild the agent image and dashboard image; `kubectl apply` `00-rbac.yaml` and `10-config.yaml`; restart the agents and the dashboard (the webhook runs from the agent image, so restart it too).

### Commit / push
* Commit **`52f828c`** ("Phase 4: safer recovery ..."), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.
* Tag `pre-phase-4` exists locally only (recreate with `git tag pre-phase-4 95aa88a`).

## Phase 5: Evaluation and graphs

**Status: DONE (simulator only).** Safety tag `pre-phase-5` at `fd15eaa` (local only; recreate with `git tag pre-phase-5 fd15eaa`). Full write-up: `docs/RESULTS.md`.

### What was done
* `scripts/evaluate.py`: runs 8 experiments (scenario timings, trust trajectory, distributed vs centralized, investigation ON/OFF on identical seeds, fault-tolerance boundary k=0..3, agent crash 4/3/2 alive, mixed genuine/benign run, CPU/memory) with **20 trials per variant** (60 in the mixed run), each trial in its own process with a **recorded seed** (seed fixes inputs, not timings), resumable, and never drops or invents a result (non-reached endpoints are empty cells with an `outcome`; crashed trials are `error` and counted).
* Output (committed): `results/*_trials.csv` (trial level), `results/trust_trajectories.csv`, `results/raw/trials.jsonl`, `results/summary.md`, 8 PNG graphs `results/fig1..fig8`.
* `docs/RESULTS.md`: every metric with start event, end event and denominator; false ALARM vs false ISOLATION; how to read each graph; findings; limitations. The tables inside are rewritten by the script, the prose is hand-written.
* `tests/test_evaluate.py` (8 tests): intervals, no invented numbers, paired seeds, resume, availability maths, report on partial data.

### Results (760 trials, 0 errors; ~2 h of wall time on 4 cores; the first launch was killed at 560 trials by a session restart and resumed from the recorded trials)
See `docs/RESULTS.md` section 0 for the ten-line summary. Headlines: lying agent 0/20 false isolation vs lying central controller 20/20; controller crashed 0/20 contained vs one agent crashed 20/20; breaks at 3 accusers (20/20), at 2 liars + 1 deceived honest agent (20/20) and at 2 silent agents (0/20 contained); the investigation did **not** reduce false isolations in my transient mix (4/20 both ways) but isolated slow-burn attacks sooner (20/20 pairs, median 4.2 s) and flagged ambiguity for review; mixed run: 0/30 missed, 15/30 benign transients isolated (all seen by >= 3 agents).

### Problems found while building it (and what I did)
1. My first benign-transient mix produced 82% false isolation in a smoke test, and my first description of the detector's confidence curve was wrong (it is 0 up to 8 connections and then 0.5-1.0, not linear from 0). Fixed the benign mix and the stratification, and corrected the doc before the full run.
2. A first trust experiment (15 s of lying) was too short to ever mark the liar suspect; lengthened to 30 s lying + 90 s recovery.
3. ON and OFF (and distributed / centralized) initially got different seeds, so they did not see identical inputs; fixed with paired seed series and a test.
4. The `.gitignore` excluded `results/`; changed to ignore only the metrics collector's `results/metrics-*.csv`.

### What is NOT verified / honest limits
* Simulator only; all times are accelerated simulator times. The benign/attack mixes are my choice, so false-positive/negative rates illustrate behaviour under a stated mix. "Silent" agents are crashed agents. CPU/memory numbers are one-process approximations. Details: `docs/RESULTS.md` section 5.
* The full test suite was **not re-run** after this phase (no product code changed; `.gitignore`, `scripts/evaluate.py`, `tests/test_evaluate.py`, `docs/RESULTS.md`, `results/` added). The last full run (Phase 4) was 298 passed, 1 timing-sensitive failure (passes alone); the new file's 8 tests pass.

### Commit / push
* See the next line (filled after the push).
