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
* Commit **`c45ddec`** ("Phase 5: evaluation harness, results and graphs"), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## FINISH: final verification and documentation

* README.md, demo.md and SETUP.md updated for everything new (11 scenarios, new docs, verify scripts, evaluation).
* **Final full run (after all code changes):** `python -m pytest -q tests` = **306 passed, 1 failed** of 307 (685 s); `python sim/local_demo.py` = **11 of 11 PASS**.
* **The one failure was not a flaky product test.** `test_ctrl_c_during_isolation_still_cleans_up` failed in both of my full runs (Phase 4 and this one) and passed when run alone. Cause found: I launched those full runs in the background (`nohup`), where the shell sets SIGINT to "ignored", the test's child process inherited that, and the Ctrl-C the test sends was never seen. This corrects my earlier "timing-sensitive, cause not investigated" note in the Phase 4 and 5 entries. Fix: the test now restores the default SIGINT handler in the child (`tests/test_verify_isolation.py`); the file passes (15/15) when run in the background too. The full suite was not re-run after that one-line test fix; the 15 tests of the file were.
* Baseline vs final: **57 tests / 6 scenarios -> 307 tests / 11 scenarios**.

## Dashboard improvements, Part 1: source labelling and simulator demo controls

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Full write-up: `docs/DASHBOARD.md`. No architecture, quorum policy, workload default or agent capability was changed; all four agents remain identical.

### What was done
* **Source metadata, stamped by the producer** (`agent/resilience/provenance.py`): `K8sBackend` / `HttpTelemetrySource` declare `LIVE_CLUSTER`, `FakeBackend` / `FakeTelemetry` declare `SIMULATOR`; an agent's source is derived from its backend **and** telemetry and is `UNKNOWN` if they disagree or declare nothing. Stamped as `data_source` on every event, decision (also in the hash-chained decision log), action-audit record, evidence snapshot, metric row and `/status`, for agents and the central controller.
* **Dashboard**: copies the stamp (disagreeing stamps become `UNKNOWN`, decisions of different sources are never merged, overall label SIMULATOR / LIVE_CLUSTER / MIXED / UNKNOWN); persistent banner (`DEMO — SIMULATED DATA` etc.); a text chip on every timeline event, decision row and metrics row.
* **Simulator demo controls** (pick scenario, Start, Reset; 9 scenarios) via `sim/demo_server.py` + `sim/scenarios.py`, with backend-enforced isolation from live: not configured in the live manifests (403), loopback-only control address, a gate that requires EVERY reachable agent to report SIMULATOR at the moment of each request (otherwise 403 and nothing is forwarded), POST+JSON+custom header+origin check+login, fixed scenario vocabulary (only the id is forwarded), and the control server itself refuses to run in a Kubernetes pod, never imports the Kubernetes client, asserts SIMULATOR before every build/start, binds 127.0.0.1 with a per-run token.
* **Bug fixed, "centralized baseline running" in distributed mode:** the live dashboard manifest always sets `BASELINE_URL`; the page printed the text whenever a URL was configured. The backend now returns `baseline_up` (the controller actually answers) and the page uses it.
* `LocalCluster.stop()` now also shuts down its status servers (the demo server rebuilds the cluster on Reset).

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **349 passed in 669 s** (307 before + 15 `tests/test_provenance.py` + 27 `tests/test_sim_control.py`; 0 failed).
* `python3 sim/local_demo.py` -> **11 of 11 PASS**.
* Safety tests at the backend boundary: refused-and-never-forwarded for LIVE / UNKNOWN / MIXED / one live agent among simulated / nobody reachable; unconfigured live dashboard refuses even when the data looks simulated; non-loopback control address refused; only catalogue scenarios and only the id forwarded; header / content-type / origin / GET checks; dashboard login required; control server refuses a non-simulator cluster, a Kubernetes pod, a backend swapped after build, a missing/wrong token; clean-subprocess check that the Kubernetes client is never imported; live manifests contain no `SIM_CONTROL`; one end-to-end test dashboard -> control server -> real simulated cluster -> real agents' `/status`.
* Browser check (Playwright/Chromium, real `python3 sim/demo_server.py --dashboard`): started `false-accusation` from the page, it ran, ended PASS, the page then asked for Reset; banner shown; no JavaScript errors. The LIVE / MIXED / UNKNOWN banners and "controls hidden" were checked by rewriting the API answer in the browser, which verifies the rendering only; the real gate is the pytest suite above.
* Existing test changed (disclosed): `tests/test_observability.py::test_event_structure_and_seq` asserted the exact key set of an event; it now includes `data_source` (and asserts a bare `EventLog` is `UNKNOWN`).

### Files changed
New: `agent/resilience/provenance.py`, `sim/scenarios.py`, `sim/demo_server.py`, `docs/DASHBOARD.md`, `tests/test_provenance.py`, `tests/test_sim_control.py`, `docs/img/dashboard-p1-*.png`. Modified: `agent/resilience/{agent,actions,baseline,forensics,metrics,monitoring,observability,response}.py`, `dashboard/{server.py,index.html}`, `sim/world.py`, `tests/test_observability.py`, `README.md`, `demo.md`, `docs/PROGRESS.md`.

### Limitations
* A stamp is a claim by the producer; a fully compromised agent could claim SIMULATOR, which is why the controls also require configuration that the live manifests never set and a loopback-only control server.
* One scenario per Reset by design. The two centralized-baseline scenarios are not offered in the dashboard.
* Not run on a real cluster: the live path (LIVE_CLUSTER stamps from real `K8sBackend` agents, the green banner) is covered by unit tests with stand-in classes only.

### Cluster rebuild / restart for Part 1
* **Rebuild** `cr-agent:1.0` (agents, central controller and the webhook now stamp `data_source`) and `cr-dashboard:1.0` (new server and page). **Restart** the agents, the dashboard and (same image) the quorum-webhook. No manifest or ConfigMap changes; no new install. Until the agents are rebuilt, a new dashboard shows `SOURCE UNKNOWN` for their records (they send no stamp): that is the intended fail-safe.

### Commit / push
* Commit **`91afecc`** ("Dashboard Part 1: ..."), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Dashboard improvements, Part 2: navigation and overview

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Write-up: `docs/DASHBOARD.md` sections 4-5. No architecture, quorum policy or agent capability changed; all four agents remain identical.

### Correction to Part 1 (found while doing Part 2)
`docs/DASHBOARD.md` was listed as a Part 1 deliverable and referenced from the README, demo.md and this file, but **it was not actually created in Part 1** (a failing earlier command in the same shell line stopped the file from being written). It now exists, containing the Part 1 sections (1-3, written from the Part 1 implementation) and the Part 2 sections. The Part 1 code and tests were unaffected.

### What was done
* **Six tabs**: Overview, Incidents, Investigations, Recovery, Evaluation, Audit; hash-routed deep links, arrow/Home/End keys, ARIA tab roles, count badges. Dark theme, source banner, header and agent drawer unchanged. **Every existing panel kept**, moved: Overview (counters, 4 node cards, simulator controls, event timeline), Incidents (decisions, pending votes, incident metrics), Investigations, Recovery (snapshots, Kubernetes actions), Audit (rejected messages, new per-agent decision-log table, event export). Evaluation is a labelled placeholder until Part 4.
* **Overview counters** (backend `summary_view`): healthy / unhealthy workloads, active incidents, active investigations (+ review needed), recoveries succeeded, recoveries failed (attempts, and gave up), agents connected / unreachable (named). Each tile shows an icon and a word (not colour alone) and has an accessible label. "No data" is never counted as healthy.
* **Agent counters** (`incidents_opened`, `recoveries_succeeded`, `recovery_attempts_failed`, `recoveries_abandoned`) kept in memory by each agent since it started, exposed on `/status`; observability only, never read by a decision. The dashboard shows the maximum across agents; agents that report none are not guessed.

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **359 passed in 670 s** (349 before + 10 new in `tests/test_dashboard_tabs.py`; 0 failed).
* `python3 sim/local_demo.py` -> **11 of 11 PASS**.
* New tests: every pre-existing element is inside exactly the right tab and none is lost; six accessible tabs; counters on synthetic statuses (max across agents, a duplicate investigation counted once, no data = unhealthy, unreachable agents named); agent counters through a CONTAIN -> RETRY -> attention -> HEALTHY lifecycle (counted once each); the real page in Chromium (every tab, arrow/Home/End keys, deep link, counter words incl. UNHEALTHY / NEEDS HUMAN / UNREACHABLE / GAVE UP, badges, no JavaScript errors).
* Browser check against the real `python3 sim/demo_server.py --dashboard`: ran app-compromise from the page; Overview showed Unhealthy 1 / Active incidents 1 while C was recovering, then Recoveries succeeded 1 and Healthy 4 of 4 afterwards; Incidents and Recovery tabs rendered; no JavaScript errors.

### Files changed
New: `tests/test_dashboard_tabs.py`, `docs/DASHBOARD.md` (see correction), `docs/img/dashboard-p2-*.png`. Modified: `agent/resilience/agent.py` (counters), `dashboard/{server.py,index.html}`, `docs/PROGRESS.md`.

### Limitations
* Recovery counters reset when an agent restarts (the largest among agents is shown); they are not a long-term history.
* The Evaluation tab is a placeholder until Part 4. Nothing in Part 2 was exercised on a real cluster.

### Cluster rebuild / restart for Part 2
* **Rebuild** `cr-agent:1.0` (new `counters` on `/status`) and `cr-dashboard:1.0` (tabs, counters). **Restart** the agents and the dashboard. No manifest or ConfigMap change. With an old agent image the new dashboard still works but the recovery counters show 0 / "none reported".

### Commit / push
* Commit **`dbe446c`** ("Dashboard Part 2: ..."), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Dashboard improvements, Part 3: incident detail view

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Write-up: `docs/DASHBOARD.md` sections 6-8. No architecture, quorum policy or agent capability changed; all four agents remain identical.

### What was done
* **Incidents list + one detail view** (Incidents tab; deep link `#incident/<SOURCE>/<target>/<n>`; backend `GET /api/incident/<source>/<target>/<epoch>`). An incident = one quorum containment of one workload; false accusations and closed investigations are not incidents. Sections: where it stands now (phase, stage, attempt, **next step and what blocks it**), what triggered it (signers, W(T), evidence per agent, deduplicated), the **four-stage action table**, votes and who signed (and who did not vote and why), recovery attempts (with snapshots and the reason for each retry), investigations, trust changes and why, rejected evidence, per-agent observations.
* **Proposed / authorized / attempted / verified are separate columns with separate meanings:** attempted = an executor really called the Kubernetes API (an agent that found the effect already done did **not** attempt it); verified = the cluster was read and shows the effect; `applied_unconfirmed` is attempted but not verified; an unreadable timeout is UNKNOWN until a later read settles it; refusals (stale incident, invalid certificate) are listed, not counted as attempts; "escalate to a human" is n/a for the cluster stages.
* **Agent: `next_step` on `/status`** (what is blocking each phase, using the same predicates as voting: stage dwell, workload trust, anomaly, validation, cluster confirmation). Observability only.
* **Dashboard history store:** the agents' rings are short (15 decisions, 40 action records, 300 events), so the dashboard remembers what it polled (bounded; lost on dashboard restart).

### Bug found and fixed while testing with real agent data
My first version showed the "escalate to a human" record as an ATTEMPTED cluster action (4 tries, VERIFIED NO), because escalation records share an outcome name with failed attempts. Now it is a separate row kind with n/a stages, and a test asserts it. Also checked: the "REFUSED" entries on a retry are real `refused_stale` records from lower-ranked executors dropping a task that a higher-ranked one had already completed.

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **380 passed in 779 s** (359 before + 21 new in `tests/test_incident_detail.py`; 0 failed).
* `python3 sim/local_demo.py` -> **11 of 11 PASS**.
* New tests: the four-stage semantics on synthetic histories (already-applied, waiting, timeout-unknown then resolved, unconfirmed, refused, proposed-only), status words, event windowing and source separation, trust / rejected / investigation sections, recovery attempts and retry reasons, bad ids rejected (404), history survives the agents' short rings and is bounded, the agent's `next_step` for every phase, **three end-to-end runs through the real simulated agents** (genuine compromise: every action passes all four stages with 3+ signers and ends RESOLVED; false accusation: no incident; failed validation: 3 attempts each with a snapshot, retry reasons, NEEDS HUMAN, why it is stuck), and the real page in Chromium (list, open, every section, UNKNOWN and unverified shown, back, deep link, unknown incident, no JavaScript errors).
* Browser check against the real `python3 sim/demo_server.py --dashboard` for validation-retry and failed-validation: detail opened by clicking the incident; screenshots in `docs/img/dashboard-p3-*.png`; no JavaScript errors.

### Files changed
New: `tests/test_incident_detail.py`, `docs/img/dashboard-p3-*.png`. Modified: `agent/resilience/agent.py` (`next_step`), `dashboard/{server.py,index.html}`, `docs/DASHBOARD.md`, `docs/PROGRESS.md`.

### Limitations
* History is only what the dashboard has seen since it started polling; an earlier incident is incomplete or absent (the page says so). Events are tied to an incident by time window (up to 120 s before containment until the next incident of that workload), not by an incident number. Trust changes are agent-trust changes in the window; only the current workload trust is available. Not run on a real cluster.

### Cluster rebuild / restart for Part 3
* **Rebuild** `cr-agent:1.0` (new `next_step` on `/status`) and `cr-dashboard:1.0`. **Restart** the agents and the dashboard. No manifest or ConfigMap change. With old agents the incident view still works but "next step / blocked because" is empty.

### Commit / push
* Commit **`a57850d`** ("Dashboard Part 3: ..."), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Dashboard improvements, Part 4: Evaluation tab and configurable workloads

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Write-ups: `docs/DASHBOARD.md` sections 9-10, `docs/WORKLOADS.md`. No architecture, quorum policy or defaults changed; all four agents remain identical.

### What was done
* **4a Evaluation tab:** `docs/RESULTS.md` rendered as written with the eight figures from `results/` and every table, under a "SIMULATOR RESULTS" banner. Strict figure-name check; dashboard image gets only RESULTS.md, summary.md and figures (`scripts/stage-dashboard.sh`, used by `scripts/build-images.sh`).
* **4b Workloads in one file:** `config/workloads.json` (defaults = original four names/roles), validated by `agent/resilience/workloads.py` (all problems listed; duplicates/unknown roles/invalid or reserved names rejected; explicit missing file is an error). `scripts/gen-workloads.py --check/--write/--names` renders `10-healthcare-apps.yaml`, the node entries of `10-config.yaml`, and the RBAC `resourceNames`. Simulator, scenarios, evaluate.py and reset.sh read the same file. Agents report `role` on `/status`; node cards show a role chip only when it differs from the name. Agent config loader rejects two nodes naming one workload.
* Default output equals the committed manifests apart from: a "GENERATED" comment line, `"role"` keys in the agent config, and RBAC lists collapsed to one marked line (same values).

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **437 passed in 791 s** (380 + 21 evaluation + 36 workloads; 0 failed).
* `python3 sim/local_demo.py` -> **11 of 11 PASS**.
* `python3 scripts/gen-workloads.py --check` -> configuration OK, all 3 generated files up to date.

### Limitations
* Only the four existing roles can be renamed/reassigned; no fifth workload. Renamed manifests are tested by generation and simulation, not applied to a real cluster. Evaluation figures are simulator results.

### Cluster rebuild / restart for Part 4
* **Rebuild** `cr-agent:1.0` and `cr-dashboard:1.0` (`scripts/build-images.sh`). App image unchanged. **Apply** `k8s/resilience/10-config.yaml` (adds `role` keys), `k8s/resilience/00-rbac.yaml` (same values), `k8s/base/10-healthcare-apps.yaml` (comment only). **Restart** agents, dashboard, quorum-webhook (config reread). Run `python3 scripts/gen-workloads.py --check` first.
* Commit **`f76a37c`** (Dashboard Part 4), pushed (no force) to both branches.

## Healthcare focus, Phase A: investigation only (no code changed)

**Status: investigation DONE; no code, manifest or test changed (docs only). Nothing here was run against a real cluster.** The user's request for phases A-F is the plan; B-F are NOT started.

### A1. Recovery attempt 1 failed, attempt 2 passed; "REFUSED 1"
* **Expected, not a timing bug, and not every run.** The incident-detail screenshot (`docs/img/dashboard-p3-incident-detail-full.png`) came from the demo scenario **validation-retry**, which deliberately makes the first replacement pod unhealthy (`sim/scenarios.py: bad_recoveries[...] = 1`; the simulated pod logs "readiness check failing"). Plain **app-compromise sets no such fault**: two fresh runs showed every agent at attempt 1 (RETRY_RECOVERY never recorded), and `local_demo.py` app-compromise passes with attempts 1.
* The failure is judged only after `validate_timeout_s` (10 s in the demo server, 6 s in tests, **90 s default on a real cluster**), on purpose, so a slow start is not mistaken for a bad pod. Observed gap in the screenshot: contained 8:22:46, retry authorized 8:22:59 (about 10 s timeout + voting). Risk on a real cluster: a pod that takes longer than 90 s to become ready (image pull) would be retried needlessly (safe, bounded by `max_retries`); untested live.
* **"REFUSED 1"** is a `refused_stale` audit record from the lowest-ranked executor (D, rank 3, acts only after 3 x 1.5 s if nobody has done the step). By then the incident had moved on (reproduced: "C is now REINTEGRATING, not RECOVERING/VALIDATING"), so D correctly did nothing. It also appears on isolate/redeploy in plain app-compromise (D: 2 `refused_stale`; B and C: `already_applied`). Harmless, but the word REFUSED in the UI is misleading; a genuine refusal is `refused_certificate`. Proposed: relabel "skipped: already done by another agent" (Phase D).
* Commands: `python3 /tmp/.../probe.py app-compromise` x2 and `validation-retry` x2 (scratch script, not committed): all returned success; audit counts as above.

### A2. Investigation did not cut false isolations; 50% of 3+-agent transients isolated
* Why: the investigation engine only acts when W(T) is in the uncertain band [0.3, 0.6). A transient seen by 3+ agents is above 0.6, so agents vote at once (`quorum.should_vote_contain`).
* Recorded data (`results/raw/trials.jsonl`, mixed/benign, 15 isolated of 30): **6 were short blips (2.5-5.2 s), 9 were long bursts (8.7-11.9 s)**, all seen by 3-4 agents. All benign inputs are network-only.
* **Smallest fix (proposed, not built):** `quorum.persist_recheck_s` (default 0 = off, so nothing changes unless enabled). In `agent._vote`, before the direct CONTAIN vote, require this agent's own anomaly to have been continuously >= `local_min_conf` for that long, tracked per target (reset when it drops); skip the wait when this agent itself sees a FILE_INTEGRITY or PROCESS detection (0.95, persistent by nature). About 25 lines in `agent.py`/`config.py` plus tests and an `evaluate.py` variant "recheck-on/off". Each honest agent applies it independently; the 3-of-4 rule is untouched.
* **Cost (projection from the recorded durations, NOT measured with a fix):** containment of network-only attacks is delayed by about P (now median 1.06 s, max 1.09 s in the mixed run, so about 7 s at P = 6 s); multi-signal/file/process attacks are not delayed. P = 6 s would have filtered all 6 blips (isolations 15 -> 9 of 30, 50% -> 30%); the 9 bursts need P >= 12-13 s (0 of 30, but network-only attacks isolate after about 13 s). A threshold detector still cannot separate a sustained unanimous network burst from a slow exfiltration, and an attacker can stay below P (evasion trade-off). Must be measured (20 trials per variant) before any claim.

### A3. Live telemetry (what the agents read on a real cluster)
Source: `HttpTelemetrySource` polls each workload's `/health` and `/_telemetry` over plain HTTP; the **workload itself** produces the data from its own `/proc` (`apps/src/telemetry.py`).
* **NETWORK:** established TCP sockets from `/proc/net/tcp[6]` of the pod's network namespace, excluding sockets whose local port is the app port (inbound); plus transmit bytes summed over non-loopback interfaces from `/proc/net/dev` (cumulative; the agent turns successive polls into bytes/s). Thresholds: >8 connections or >256 KB/s; the simulator's "normal ~2" has **not** been measured on the real cluster.
* **PROCESS:** every `/proc/<pid>` in the app **container's own PID namespace** (name, exe, first 200 chars of command line); allowlist = `python`, `python3`; known-bad names and `/tmp`, `/dev/shm`, `/var/tmp` executables rate 0.95, other unknown names 0.6. A shell started by `kubectl exec` (`sh`) is therefore "unknown" while it runs; use `python -c` for harmless demo commands.
* **FILE_INTEGRITY:** SHA-256 of every file under `APP_ROOT=/app` (excluding `__pycache__`/`.pyc`) against the known-good hash ConfigMap built from the same source; modified 0.95, missing 0.8, new 0.7.
* **AUTH:** `auth_failures_by_ip` counters kept in memory by the services that authenticate callers, attributed to the target's **pod IP**, over a 10 s window (>10 failures). Counters reset when a service restarts; correct attribution assumes the source IP is preserved pod to pod (true for in-cluster ClusterIP traffic with Calico; not verified here).
* **Unavailable:** kernel/syscall/eBPF events, DNS, files outside `/app`, fileless/in-memory activity, other containers or the node, the Kubernetes audit log, per-connection byte counts or content, inbound traffic volume. **Self-reported:** a fully compromised workload could falsify its own `/_telemetry`, and all four agents read the same endpoint (inherent to this design, not new).
* No live run has been done: `HttpTelemetrySource` is covered by unit tests only (see Phase 2 limitations).

### A4. File-level plan and conflicts (see the Phase A report to the user for the full table)
Conflicts flagged for decision: (1) obstruct's "obstructer loses trust" is NOT achievable with current trust rules (trust decays only when a peer's *positive* claim is contradicted; there is no "all clear" evidence type, below-threshold evidence is never emitted); it needs a new, symmetric trust rule, i.e. a security-behaviour change; (2) one dashboard with both pages cannot live in the cluster without breaking the Part 1 rule that SIM_CONTROL is never in live manifests; (3) the tripwire is a real file change on a live workload that triggers real containment; the ground rule "attacks only in the simulator" needs an explicit decision that this harmless marker is allowed; (4) "recovery priority" cannot change behaviour without a protocol change (incidents already recover in parallel), so it can only be presentation; (5) agent logs are on `emptyDir`, so a deleted agent pod rejoins with empty history; (6) no live-verified results exist yet, so the guide's "live-verified" section can only contain what the user runs and reports.

## Healthcare focus, Phase B: Demo Mode and Live Monitoring as separate pages

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Write-up: `docs/DASHBOARD.md` sections 11-14, `docs/LIVE_DEMO.md` section 1. No quorum policy, workload default or agent decision logic changed; all four agents remain identical. User decisions applied: the two-page dashboard runs on the presenter's laptop with Live read through port-forwards; the in-cluster dashboard shows the Live page only.

### What was done
* **Two channels in the backend** (`dashboard/server.py: Channel`): each has its own agents, collector, history, incidents, exports and (demo only) simulator controls. A channel accepts only its own `data_source` (demo SIMULATOR, live LIVE_CLUSTER): a wrong-source status is discarded before any view, a wrong-source record inside an accepted status is dropped and counted (`Collector._scrub`), metrics and snapshot downloads are filtered the same way. In two-page mode every data request must name its channel (400 otherwise).
* **Isolation of controls:** a live channel is built without simulator controls even if `SIM_CONTROL_*` is set; `sim_gate` refuses it again; nothing is forwarded (tests use a counting stand-in control server). Part 1 rules on the demo channel are unchanged.
* **Page:** persistent toggle "Demo Mode | Live Monitoring" (URL `?mode=`, remembered in the browser, full reload on switch). Demo banner "DEMO MODE — SIMULATED DATA. No real healthcare workload is being attacked."; Live banner "LIVE MONITORING — CONNECTED TEST CLUSTER" with cluster identity, agents reachable (N of 4, which are down), telemetry age (STALE after 10 s), "DEGRADED" if some agents are down; "NOT CONNECTED — Live monitoring is unavailable." (everything else hidden, no fallback). No scenario buttons or requests on the Live page. The green "LIVE" dot is now "Auto-refresh on" / "Paused".
* **Agent (observability only):** `/status` gains `cluster` (configured `cluster_name` + the API server's `/version` read in the background) and `telemetry` (age of the agent's last workload poll). New optional config key `cluster_name` (set to `cr-platform` in `10-config.yaml`).
* **Deployment:** `scripts/laptop-dashboard.sh` (port-forwards re-opened in a loop, everything on 127.0.0.1) + `docs/LIVE_DEMO.md` section 1. `sim/demo_server.py --dashboard` now passes `DEMO_STATUS_URLS` and binds the dashboard to 127.0.0.1. The in-cluster manifest is unchanged (no `DEMO_`/`LIVE_STATUS_URLS` -> Live channel from the config file). `STATUS_URLS` (single source) still works and can only pose as the page matching what its agents report; combining it with `DEMO_`/`LIVE_STATUS_URLS` is an error.

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **464 passed in 858 s** (437 before + 27 new in `tests/test_two_pages.py`; 0 failed).
* `python3 sim/local_demo.py` -> **11 of 11 PASS**.
* Negative tests: simulated agents wired to the live channel are ignored entirely (no state, events, history, incidents, export); live agents wired to the demo channel likewise; UNKNOWN / missing / MIXED / garbage labels accepted by neither; a simulated record smuggled into a live status is dropped; live page with the cluster down shows nothing from the demo; `POST /api/sim/start|reset?channel=live` -> 403 with 0 requests at the control server; no channel -> 400; demo controls refused when the "demo" agents report LIVE_CLUSTER; a browser test switches pages, checks both banners, that "simulated scan" never appears on the live page and that the live page makes no `/api/sim/` request.
* Two existing tests were edited: the banner text in `test_provenance.py` (new wording) and `test_evaluation_tab.py` (its browser test now gives the dashboard one simulated status per agent, because the tabs are hidden while the page is not connected).

### Limitations
* Not run on a real cluster; the connected Live page is exercised only with stand-in agents. The Live-page screenshot of a connected cluster must come from your own run. `docs/img/dashboard-pB-02-live-not-connected.png` is the real not-connected behaviour.
* The telemetry age needs the rebuilt agent image; with old agents it says "not reported". Cluster name needs the updated `resilience-config`.
* "Auto-refresh" only says the screen is refreshing.

### Cluster rebuild / restart for Phase B
* **Rebuild** `cr-agent:1.0` (new `/status` fields, `cluster_name`) and `cr-dashboard:1.0` (channels, pages). **Apply** `k8s/resilience/10-config.yaml` (adds `cluster_name`). **Restart** the agents and the dashboard. App image and other manifests unchanged. The laptop dashboard needs only `scripts/laptop-dashboard.sh`.
* Commit **`da1ba8c`** (Healthcare Phase B), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Healthcare focus, Phase C: healthcare presentation, persistence re-check, obstruct

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Write-ups: `docs/HEALTHCARE.md`, `docs/RESULTS.md` sections 4.9-4.10, `docs/DASHBOARD.md` (Phase C), `docs/WORKLOADS.md`. Quorum policy (3 of 4), workload defaults, internal names/roles and all existing security behaviour are unchanged; the new agent behaviours are OFF by default or simulation-only switches. All four agents remain identical.

### What was done
* **Healthcare presentation (config only):** `config/workloads.json` gained optional presentation fields (`display_name`, `provides`, `depends_on`, `recovery_priority`, `priority_rationale`), validated (unknown/self/duplicate/cyclic dependencies, priorities 1-4 all different or all absent, length limits) and ignored by the manifest generator except in the agents' config. Shown as A Patient Portal, B Login & Access Service, C Electronic Health Records (EHR) API, D Clinical Database. Dependencies are checked against the application code by a test (portal -> auth + records; records -> auth + database). Dashboard: display names with the internal name beside them, a Services panel (calls, what stops working while it is isolated, derived from the dependencies; suggested recovery order with rationale, **display only**), a permanent "SYNTHETIC DATA ONLY" chip. No clinical urgency scores.
* **Scenarios in healthcare terms** (`sim/scenarios.py`; ids unchanged) and a new scenario **obstruct** in the Demo Mode picker, `sim/local_demo.py`, tests and `scripts/evaluate.py`.
* **obstruct (as you decided: no new trust rule, no "all clear" evidence):** compromise mode `obstruct` makes one agent refuse to sign or send any vote about the named workload (`Agent._cast`); the reason is on record ("[simulated compromise] obstruction: this agent is withholding its vote to ...") in the agent's `vote_reasons`, the event timeline and the incident view's "did not vote, and why". It changes only that agent's own behaviour, so it is **live-capable and safe**: `scripts/simulate-compromised-agent.sh <agent> <target> obstruct` (same switch as the existing compromise modes; nothing is fabricated or sent, no cluster resource touched). Not run live.
* **`quorum.persist_recheck_s` (default 0 = off):** an agent whose own reading is a network or failed-login anomaly waits until it has lasted that long before voting to isolate; an agent that itself sees a file change or an unexpected process never waits; each agent decides alone, a missing vote is never approval, the wait and its reason are shown ("re-checking ... waiting Ns more").
* `docs/HEALTHCARE.md`: requirements mapped to HIPAA Security Rule categories (reference only, **no compliance claim**, gaps listed).

### Measured results (simulator; 200 new trials, all completed, 0 errors; full tables in docs/RESULTS.md 4.9-4.10)
* **Re-check, benign mix (20 trials, same inputs for both arms):** false isolations **12/20 (60%) with 0 s -> 9/20 (45%) with 6 s**. Every short blip filtered (3/11 -> 0/11); **none of the 9 long bursts** (8-12 s) was (9/9 both).
* **Cost for real attacks (paired by seed):** random mix of attack kinds contained 20/20 both ways; median extra time 0.0 s (the 3 failed-logins-only attacks: median 5.5 s extra; the 17 with a file change or unexpected process: not delayed). Sustained network-only exfiltration: isolation **median 1.04 s -> 7.01 s** (extra 5.5-6.0 s), contained 20/20 both ways.
* **An attacker can stay under the wait:** network-only exfiltration in 3 s bursts every 5 s was contained **20/20 with the wait off and 0/20 with 6 s**.
* **Obstruct (20 trials, random obstructing agent / target / attack kinds, against a control with the same inputs):** contained and fully recovered by the other three **20/20**, exactly 3 signers each, the obstructing agent's vote counted **0/20**, no other workload isolated; isolation median 1.5 s vs 1.1 s control, full access 19.0 s vs 18.5 s. **The obstructing agent was not penalised** (lowest trust any honest agent gave it: 100): no rule exists for a missing vote.
* `results/raw/trials.jsonl` now has 960 trials (760 earlier + 200); the earlier tables and figures were regenerated from the same data and did not change.

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> first full run **493 passed, 1 failed** (`test_every_catalogue_scenario_has_a_driver_and_description` hard-coded the old 9 scenario ids); fixed; the two affected files re-run -> 35 passed; then the whole suite re-run: **494 passed in 926 s, 0 failed**.
* `python3 sim/local_demo.py` -> **12 of 12 PASS** (incl. obstruct).
* `python3 scripts/gen-workloads.py --check` -> all 3 generated files up to date.
* New tests: `tests/test_healthcare.py` (scenario wording, panel and names in a browser, hidden when no metadata, re-check delays and says why, short spike ignored, file/process never delayed, pulsing evades and is caught with the re-check off, obstruct end-to-end, obstruct only for the named workload, CLI/switch) and additions to `tests/test_workloads.py` (defaults, dependencies vs application code, 12 invalid-metadata rejections, manifests unchanged by presentation fields). Existing tests edited: scenario-id list in `test_sim_control.py`; trial count (now computed from the raw file), the 12-line summary and sections 4.9-4.10 in `test_evaluation_tab.py`.

### Limitations
* **Not verified on a live cluster:** obstruct live (needs a real detection to vote on; the tripwire in Phase E provides one), the re-check on real traffic, the Services panel against real agents.
* The re-check is a trade-off, not a fix: it delays network-only attacks by about the wait and can be evaded by bursts shorter than the wait; it does not touch the 9 long benign bursts. It is **off by default**; enable it per deployment (`quorum.persist_recheck_s` in the agents' config, not set in the shipped manifests).
* The obstruct-trial "vote counted" column in the control arm only shows whether a randomly chosen agent happened to be among the 3 signers.
* The presentation names appear on node cards, the Services panel, scenario text and docs; the incident view, drawers, audit and logs still show internal names (Phase D reworks wording). The application image still does not label its records "synthetic" in its JSON (changing it needs an image rebuild and a hash regeneration; done together with the tripwire in Phase E).

### Cluster rebuild / restart for Phase C
* **Rebuild** `cr-agent:1.0` (re-check, obstruct, status metadata, config fields) and `cr-dashboard:1.0` (names, Services panel). **Apply** `k8s/resilience/10-config.yaml` (adds display names, dependencies, priorities per node; no `persist_recheck_s`, so the re-check stays off). **Restart** the agents and the dashboard. **Important order:** apply the config only together with the new agent image (the old image rejects unknown node keys). The app image, RBAC, webhook and `10-healthcare-apps.yaml` are unchanged. To try the re-check live, add `"quorum": {"persist_recheck_s": 6}` to the config JSON, apply and restart the agents.
* Commit **`04e059f`** (Healthcare Phase C), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Healthcare focus, Phase D: plain-English interface

**Status: DONE in the simulator and tests; NOT run against a real cluster.** Write-up: `docs/DASHBOARD.md` sections 15-19. Presentation only: no agent code, protocol name, signed content, log format, quorum policy or security behaviour changed; the backend additions are read-only fields on the dashboard's own JSON (`response` per agent, `responses` per incident proposal). All four agents remain identical.

### What was done
* **One vocabulary** (a single table, `const V`, in `dashboard/index.html`): tabs Overview / Security Alerts / Investigate / Recovery / Performance Results / Activity Log; Network Activity, Running Processes, File Changes (and Failed Logins); Evidence Shared, Agent Decision, Agreement Reached (3 of 4), Isolation, Recovery Check, Normal Access Restored; used on the cards, timeline filters and rows, stage strip, tables, incident view, recovery tab, drawer and docs. Raw ids remain in tooltips and "Technical details". Element ids, tab ids and hash routes unchanged.
* **Tooltips** (keyboard-reachable, with ⓘ) for workload trust, agent trust, agreement (3 of 4), evidence score, evidence confidence, Recovery Check. **W(T), confidence numbers, proposal keys, signers' scores and trust views** moved into collapsed **Technical details** (they stay open across the refresh).
* **Four separate explanations** (workload trust, agent trust, evidence confidence, Recovery Check) in a collapsible Overview section; no combined "security score" (a test asserts the phrase appears once, in the sentence that denies it).
* **Five honest agent states** (Agrees, Disagrees, Unavailable, Not yet responded, Message failed verification), in the Waiting-for-agreement panel and the incident's "Who agreed" table; **only a counted signed vote is "Agrees"**; waiting out the persistence re-check is "Not yet responded", not "Disagrees"; a missing vote is never approval.
* **Incident view:** evidence grouped by agent and signal (latest reading + number of readings, full list expandable).
* **Your decision 1:** the "REFUSED 1" on a step another agent had already done is now **"skipped: already done by another agent"**; only a missing valid 3-of-4 agreement (`refused_certificate`) is "REFUSED".
* **Performance Results:** summary, graphs and tables stay visible; long explanations (and the limitations) are collapsed.

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **504 passed in 981 s, 0 failed** (494 + 10 new in `tests/test_plain_english.py`).
* `python3 sim/local_demo.py` -> **12 of 12 PASS**.
* New tests: the 14 requested terms and the single vocabulary table; tooltips; four explanations and no combined score; `agent_response` precedence (five states, missing vote never approval, the re-check wording matches the agent's); pending-votes view with all five states and with a failed-verification sender; the same in a browser; an end-to-end **obstruct** run through the real simulated agents showing the obstructing agent as "Disagrees" with its reason on every proposal and B, C, D as "Agrees"; the incident view rendered with crafted data (grouped evidence with counts, raw values only in technical details, "skipped 1" vs "REFUSED 1", all five state labels); outcome/stage/action words; the overview in a browser (plain words, tooltips, technical details open without opening the drawer and surviving the refresh).
* Existing tests edited for the new wording: `test_incident_detail.py` (browser test strings), `test_evaluation_tab.py` (explanations are collapsed by default: text read with `text_content`, collapsed/visible assertions added).
* Screenshots: `docs/img/dashboard-pD-01..04-*.png` (simulator).

### Limitations
* The agents' own event sentences and the backend's "next step / blocked because" wording are unchanged technical text (they appear in the activity list and the incident's "Where it stands now"). The agent drawer is deliberately the technical view.
* "Disagrees" means the agent recorded a reason for not voting; the protocol has no explicit "no" vote. Past states are reconstructed from what the dashboard recorded.
* Not run on a real cluster.

### Cluster rebuild / restart for Phase D
* **Rebuild `cr-dashboard:1.0` only** (`scripts/build-images.sh`) and **restart the dashboard** (`kubectl -n resilience rollout restart deploy/dashboard`). **No agent rebuild, no manifest or config change, no app image change.** The laptop dashboard (`scripts/laptop-dashboard.sh`) needs only a `git pull`. It works with the old agents too (the five states are computed by the dashboard), but the new agent image from Phase B/C is still needed for the cluster identity, telemetry age, display names and obstruct.
* Commit **`3f34c4f`** (Healthcare Phase D), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Healthcare focus, Phase E: live demonstration support

**Status: DONE in code, scripts, docs and tests; NOT run against a real cluster.** Runbook: `docs/LIVE_DEMO.md`. Your decisions applied: the tripwire is the narrow, explicit exception to "attacks only in the simulator" (one harmless, operator-initiated change to ONE designated marker file, via `python -c`, never `sh`); agent restart losing history is documented; the live run log is a placeholder table for your own runs. No quorum policy, detector threshold, workload default or agent decision logic changed.

### What was done
* **Tripwire marker:** `apps/src/static/TRIPWIRE-TEST.txt` (one file, in the shared app image, so present in all four workloads). It is part of the known-good manifest, so the existing **File Changes** check covers it; nothing in the application reads it.
* **`scripts/tripwire-test.sh <A-D|workload> [--yes]`:** dry run by default; with `--yes` it runs exactly `kubectl -n healthcare exec deploy/<workload> -- python -c 'open("/app/static/TRIPWIRE-TEST.txt","a").write(...)'`. Refuses anything but the four workloads (not the client probe), refuses a kubectl context other than `cr-platform`, makes no other kubectl call. Restore steps are in the runbook (normally: do nothing, the platform replaces the pod; otherwise `scripts/reset.sh` or a `rollout restart`).
* **Safety net for the manifest (a real risk I found):** the agents treat any difference from `known-good-hashes` as tampering, and `k8s/generated/` is git-ignored, so an OLD manifest (without the new file, or after any app change) would make the agents **isolate healthy workloads**. New `scripts/verify-baseline.py` (read-only pre-flight: compares each running workload's files with the applied ConfigMap), new `scripts/upgrade-app.sh` (agents OFF -> apply manifest -> roll workloads -> verify -> agents ON; leaves them off on a mismatch), and `scripts/deploy.sh` now always regenerates the manifest instead of reusing a stale one.
* **Synthetic label in the application:** the database, records API and portal JSON responses carry `"synthetic": true` (the portal page already said so).
* **Simulator parity:** a `tripwire` change kind (not in the default attack kinds, so existing scenarios are unchanged) and the marker in the simulated baseline.
* **Live Page in centralized mode:** it was reported NOT CONNECTED when only the baseline controller exists (no agents); now it says "CENTRALIZED BASELINE MODE" (`connection_view` counts the controller). `scripts/laptop-dashboard.sh` also forwards `svc/central-controller` (`LIVE_BASELINE_URL`).
* **`docs/LIVE_DEMO.md`:** step-by-step runbook (healthy state, agent outage, tripwire, compromised agent, forgery, obstruct + tripwire, centralized comparison): exact commands, what to point at, expected behaviour (marked expected, not observed), reset, limits, and an empty run-log table for you.

### Test evidence (commands run, actual results)
* `python3 -m pytest -q tests` -> **524 passed in 1006 s, 0 failed** (504 + 20 new in `tests/test_live_demo_support.py`).
* `python3 sim/local_demo.py` -> **12 of 12 PASS**.
* New tests: the REAL application process + the REAL detector with the documented command run through the script (stand-in `kubectl`, `/app` mapped to a temp copy): a clean workload reports no file change; after the command the detector reports exactly one `FILE_INTEGRITY` (0.95, "1 modified file(s): static/TRIPWIRE-TEST.txt"); exactly that one file changed on disk; restoring the file clears it. The script: exactly one `exec` of `python` on the marker and no other kubectl call, dry run changes nothing, refuses `client` / unknown names / injection attempts / the wrong context / an unknown flag, follows configured workload names. The simulator runs the whole pipeline (3+ voters, only File Changes seen, full return, no other workload touched). `verify-baseline` passes when matching and flags modified / new / missing files and a STALE manifest, exits 2 when it cannot check. `upgrade-app.sh` order (agents off before the manifest, verify before agents on). Synthetic flag. Centralized baseline shown as connected.
* The detection test cannot assert on network/process readings: the test process shares `/proc` with the machine, so only the file-integrity result is asserted (documented in the test).

### Limitations
* **Nothing in Phase E has been run on a real cluster.** The tripwire's end-to-end effect (detection by four real agents, real isolation by Calico, real pod start) is expected, not observed; the timings in the runbook are derived from the configured timers, not measured.
* The tripwire file is a marker, not a model of a real attack; it triggers real containment of a synthetic-data workload (by design).
* `kubectl exec` needs a working exec path to the pod; if the pod's `/app/static` were not writable by the container user the command fails visibly (the image owns `/app` as uid 10001).
* A deleted agent pod rejoins with empty history and fresh trust (agents use `emptyDir`); documented.

### Cluster rebuild / restart for Phase E (this one changes the APP IMAGE)
1. `scripts/build-images.sh` (rebuilds `cr-healthcare-app:1.0` and `:known-good`, `cr-agent:1.0`, `cr-dashboard:1.0`, and regenerates the known-good manifest).
2. **`scripts/upgrade-app.sh`** (do NOT just roll the workloads: it stops the agents, applies the new manifest, rolls the workloads onto the new image, verifies, starts the agents).
3. `kubectl apply -f k8s/resilience/10-config.yaml` and `kubectl -n resilience rollout restart deploy/agent-a deploy/agent-b deploy/agent-c deploy/agent-d deploy/dashboard` (needed if you have not yet applied the Phase B/C config and agent image).
4. `python3 scripts/verify-baseline.py` -> four `ok`; then follow `docs/LIVE_DEMO.md` section 2.
* Commit **`ec6b451`** (Healthcare Phase E), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Healthcare focus, Phase F: presentation guide and final check

**Status: DONE; NOT run against a real cluster.** Your decision 7 applied: the live-verified section of the guide is a clearly marked EMPTY placeholder for your own runs.

### What was done
* **`docs/presentation-guide.pdf`** (11 pages, regenerated by `python3 scripts/make-presentation-guide.py`): plain English for a non-expert, healthcare-focused: the problem, what was built (four workloads, one agent each, 3-of-4 agreement, Isolation, Recovery Check, staged return, the impact table), **two pages never mixed** (fresh screenshots of Demo Mode and of Live Monitoring when not connected), how to read the dashboard, **simulator results** (a clearly labelled box and table with the numbers from `docs/RESULTS.md`), **live-verified results (placeholder, every row "not run")**, a suggested demonstration and the tripwire exception, limitations stated plainly, 13 likely faculty questions with honest answers, a glossary. A placeholder box marks where a connected Live Monitoring screenshot goes (none is claimed: it can only come from your cluster).
* Fresh screenshots: `docs/img/dashboard-pF-01..05-*.png` (simulator and the real not-connected page).
* `tests/test_presentation_guide.py` (4 tests): the live table is an empty placeholder, no simulator number is called a live one, every number quoted in the guide matches the same fact in `docs/RESULTS.md`, and the required sections and screenshots exist.

### Final regression (commands run, actual results)
* `python3 -m pytest -q tests` -> **528 passed in 1007 s, 0 failed**.
* `python3 sim/local_demo.py` -> **12 of 12 PASS** (app-compromise, false-accusation, forge-evidence, agent-crash, transient-blip, slow-burn, ambiguous, validation-retry, failed-validation, obstruct, baseline, controller-crash).
* `python3 scripts/gen-workloads.py --check` -> up to date.

### Limitations (unchanged)
Nothing in Phases B-F has been run on a real cluster; all results are simulator results; the guide's live table is empty.

### Exact cluster steps for everything since the last deployment (Phases B-F; safe order)
1. `git pull`, then `python3 scripts/gen-workloads.py --check`.
2. `scripts/build-images.sh` (rebuilds `cr-healthcare-app:1.0` and `:known-good`, `cr-agent:1.0`, `cr-dashboard:1.0`; regenerates the known-good manifest).
3. `kubectl apply -f k8s/resilience/00-rbac.yaml -f k8s/resilience/10-config.yaml` (new config keys: `cluster_name`, display names, dependencies, priorities; the persistence re-check stays OFF). Apply the config BEFORE the agents restart on the new image.
4. `scripts/upgrade-app.sh` (stops the agents, applies the new known-good manifest, rolls the workloads onto the new app image, verifies, starts the agents on the new image and config). Do not roll the workloads by hand: an old manifest makes the agents isolate healthy workloads.
5. `kubectl -n resilience rollout restart deploy/dashboard deploy/quorum-webhook` and wait for them (`kubectl -n resilience get pods`).
6. `python3 scripts/verify-baseline.py` -> four `ok`; `scripts/verify-isolation.sh` once (see its header); then `scripts/laptop-dashboard.sh` and follow `docs/LIVE_DEMO.md` section 2.
Nothing else changed: no new manifests, no new RBAC rules, no new CRDs. To try the re-check live: add `"quorum": {"persist_recheck_s": 6}` to the config JSON, apply, restart the agents.
* Commit **`04854be`** (Healthcare Phase F), pushed (no force) to `claude/cyber-resilience-platform-xxs3oh` and `claude/inspiring-thompson-ik54a3`.

## Stretch Phase G: six healthcare workloads

**Status: DONE in the simulator and by tests; NOT run on a real cluster / Minikube.** Rollback point = the Phase F version, commit **`c311502`**.

### What was done
* **Agents and workloads are now separate.** Still exactly **4 identical peer agents (A-D)** and the **3-of-4 quorum, unchanged**. Every agent watches every workload (A-F); E and F have no agent of their own. Code: `ClusterConfig.extra_workloads` / `.targets` (`agent/resilience/config.py`); the agent state, status, certificate target check, admission policy, baseline controller and telemetry source use `targets`; everything about voting, trust, executor ranking and peers still uses the four agents (`nodes`).
* **Added through the workload config file only** (`config/workloads.json`, now six workloads; validated: E/F optional, `zone` required for them, `needs_for` must be a subset of `depends_on`, priorities 1..N). `config/workloads-four.json` is the exact Phase F profile; `scripts/evaluate.py` pins it so the recorded results (RESULTS.md) remain reproducible.
* **New workloads, synthetic data only** (`apps/src/app.py`): Laboratory Results Service (`lab-results`, node E, Minikube node b) and Medication Service (`medication-service`, node F, node c); invented lab values and generic medication names; token-protected like the EHR API; the portal shows them at `/api/labs`, `/api/medications` (`/api/patients` and the availability probe are unchanged).
* **Dependency / impact information**: Laboratory Results and Medication depend on the Login & Access Service and the Clinical Database; the portal depends on them; `needs_for` words the impact ("the Patient Portal cannot show laboratory results"). Presentation only. Priorities: 1 Clinical Database, 2 Login, 3 EHR API, 4 Laboratory, 5 Medication, 6 Portal.
* **Generator** renders the six Deployments/Services (with `LAB_URL`/`MEDICATION_URL`), the `extra_workloads` block and the RBAC `resourceNames`; with `workloads-four.json` the Phase F files are reproduced exactly (checked before regenerating). `tripwire-test.sh` accepts A-F; `verify-baseline.py` checks every workload.
* **Simulator**: six workloads; three new healthcare scenarios (`lab-results-tampered`, `medication-failed-logins`, `database-cascade`) listed in the demo picker only for the six-workload profile.
* **Dashboard**: E/F cards ("watched by all four agents (no agent of their own)"), colours, six-workload summary counts, impact wording from `needs_for`, incident route accepts E/F.

### Load on Minikube (docs/WORKLOADS.md, "Load")
* Measured on THIS machine (not Minikube), `python3 scripts/measure-load.py --seconds 25` (`results/load-local.json`): agent HTTP requests 32 -> 48 per second; one full poll median 47.8 -> 80.0 ms; workload memory 87.3 -> 131.3 MiB; workload CPU 9.2 -> 17.2 % of one core; 0 failed readings in either run. One run, sandbox machine: order of magnitude only.
* From the manifests: +100m CPU / +128 MiB requested (limit +1000m / +512 MiB); nodes are 2 CPU / 2200 MiB each.
* **Not measured: real Minikube.** `scripts/minikube-load.sh` (read-only, needs metrics-server) is for your own cluster.

### Test evidence (commands run, actual results)
* Simulator, all scenarios: `python3 sim/local_demo.py` -> **15 of 15 PASS** (the 10 earlier cluster scenarios, the 3 new ones, baseline, controller-crash).
* New tests `tests/test_six_workloads.py` (12) plus updated assumptions in 8 existing test files (four-workload profile where the test is about the historical four).
* Full suite: `python3 -m pytest -q tests` -> **543 passed in 1010 s, 0 failed** (Phase F: 528). `python3 scripts/gen-workloads.py --check` -> up to date.
* Problems found and fixed on the way: importing `scripts/evaluate.py` leaked `WORKLOADS_CONFIG` into the test process (9 failures, fixed by restoring the variable after the import); a pre-existing timing race in the tripwire simulator test (evidence is cleared when the isolation commits, so the test now records it while waiting).

### Limitations
* Nothing in Phase G has run on a real cluster. At most six workloads (A-F); more are not supported. E and F share Minikube nodes b and c with other workloads (placement, not a security boundary). The agents still restart without history (unchanged). The presentation guide and its screenshots still describe the four-workload view (not regenerated in this phase).

### Cluster rebuild / restart for Phase G (the APP IMAGE changes: two new roles)
1. `git pull`; `python3 scripts/gen-workloads.py --check`.
2. `scripts/build-images.sh` (new app image and known-good manifest, agent and dashboard images).
3. `kubectl apply -f k8s/resilience/00-rbac.yaml -f k8s/resilience/10-config.yaml`.
4. `scripts/upgrade-app.sh` (safe order: stops agents, applies the known-good manifest, applies `k8s/base/10-healthcare-apps.yaml` so E and F are created and the other four roll onto the new image, verifies, restarts agents).
5. `kubectl -n resilience rollout restart deploy/dashboard deploy/quorum-webhook`; `kubectl -n healthcare get pods` (six workload pods + client; a Pending pod means a node is short of memory).
6. `python3 scripts/verify-baseline.py` -> six `ok`; then `scripts/minikube-load.sh`.
