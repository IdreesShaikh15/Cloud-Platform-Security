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
