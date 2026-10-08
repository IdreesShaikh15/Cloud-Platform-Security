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
