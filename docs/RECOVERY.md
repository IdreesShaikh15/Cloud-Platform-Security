# Safer recovery (Phase 4)

*What the platform now does before, during and after replacing a compromised workload, in plain English,
and what it still cannot do.*

Before this phase recovery was: isolate, redeploy once, and if the new pod looked wrong wait 90 s and
redeploy again, forever, with no record of what had been destroyed. Three things were missing. This phase
adds them.

| Need | What was added | Where |
|---|---|---|
| Do not destroy the evidence | A read-only **evidence snapshot** is saved before the workload is replaced | `forensics.py` |
| A bad replacement must not be trusted | Validation failure -> **bounded retries with back-off**, then **"needs human attention"**; never shown healthy | `agent.py`, `RETRY_RECOVERY` |
| Cluster calls must be honest | Every Kubernetes action has an **audit record with an observable result**; a timeout is **not** treated as a failure | `actions.py`, `agent.py::_step` |

---

## 1. Evidence snapshot (before replacement)

When the quorum decides to contain a workload, every agent immediately (and again before each retry)
saves a snapshot of **only that workload**:

| Captured | Notes |
|---|---|
| Incident: which agent, when, why, the quorum decision and its justification | |
| Pod metadata (name, uid, phase, IP, image id, container state, restarts) | Only pods labelled `app=<that workload>`. **No env vars, volumes, commands or annotations.** |
| Recent log lines of each of those pods (last 200 lines / 64 KiB) | Credential-looking text (`Bearer ...`, `password=...`, `api_key:` ...) is **redacted** and counted |
| The signal values the agent observed (connections, bytes, processes, auth failures, detections, recent evidence) | Process *command lines* are not copied |
| SHA-256 of every monitored file vs the known-good manifest | Which files differ |
| `not_captured` | **Everything it could not get, and why** (pod list failed, a pod vanished before its log was read, telemetry unreachable, no hash manifest). Nothing is invented. |
| `digest` | SHA-256 of the whole snapshot, so a later edit is visible |

* **Read-only:** it only calls `list pods` and `read pod log`. Nothing is written to the cluster.
* **Scoped:** it never touches another workload, the client pod, Secrets, or another namespace.
* **Never blocks recovery forever:** recovery waits for the snapshot at most `snapshot_timeout_s` (15 s), then
  proceeds and the snapshot says it was late. Values from the agent's own memory are taken synchronously at the
  moment of the decision; only the cluster reads happen in the background.
* **Where it lives:** `/var/log/resilience/snapshots/snapshot-<target>-e<incident>-<tag>-<agent>.json`, mode `0600`.
  Tag `contain` = the compromised pods; `failed<N>` = the replacement pods of attempt N that failed validation.
* **Viewing / export:** dashboard section "Evidence snapshots & Kubernetes actions" lists them (what is missing is
  shown inline) with an **export JSON** button (`/api/snapshot/<agent>/<key>`); each agent also serves
  `/snapshot/<key>` on its status port.

Limits: each of the four agents keeps its own copy (they see slightly different telemetry); the directory is an
`emptyDir`, so a snapshot is lost if the *agent* pod is deleted (copy it out, or mount a volume).
Redaction is pattern-based and can miss unusual secret formats. Logs only exist if the app writes them.

---

## 2. Validation failure: retries, then a human

```
 CONTAIN --> ISOLATED --> RECOVERING --> VALIDATING --VALIDATE quorum--> REINTEGRATING --> ... --> HEALTHY
                              ^               |
                              |   still failing after validate_timeout_s
                              +-- RETRY_RECOVERY quorum (attempt n+1), after back-off
                                      ...after max_retries retries: NEEDS HUMAN ATTENTION
```

* **What counts as failed:** the replacement is not ready `validate_timeout_s` after the redeploy, or it is up but
  fails validation (health, file hashes vs known-good, still the old instance, anomalies) for that long.
* **A retry is itself a quorum action** (`RETRY_RECOVERY`, stage = attempt number, with its own 3-signature
  certificate that the webhook checks; it can only re-apply the known-good image to the same Deployment and the
  attempt number can never go backwards). One agent cannot trigger a redeploy loop.
* **Back-off:** wait `backoff_base_s * backoff_factor^(n-1)` (15 s, 30 s, 60 s ... capped at 120 s) before attempt n+1.
* **Never two recoveries at once:** a recovery is only started when (a) isolation is confirmed in the cluster,
  (b) the evidence snapshot is ready (or timed out), and (c) no rollout of that workload is still in progress.
  Each (incident, attempt) pair is a separate idempotent task; a stale attempt is dropped.
* **Stays quarantined** through all of this: the isolation policy is never removed before validation passes.
* **After `max_retries` (3) retries** (4 attempts in total) the incident is marked **NEEDS HUMAN ATTENTION**:
  dashboard card turns red with the reason, an audit record `needs_human_attention` is written, the workload stays
  quarantined and is **not** shown as healthy. If a human fixes it and validation then passes, the pipeline resumes by itself.
* **HEALTHY means confirmed.** Reaching the last stage (FULL) no longer flips the target to HEALTHY: it becomes HEALTHY
  only when the cluster is read and the isolation policy is really gone. (This also removes the Phase 1 note
  that the dashboard showed intent rather than the cluster.)
* "Replaced" is checked too: recovery is only "done" when the pods that existed at snapshot time are gone
  (`check_replacement_pods`).

---

## 3. Every Kubernetes action has a result; a timeout is not a failure

Each action (isolate, redeploy, mark validated, change stage) is a small task that runs **by this algorithm**:

1. **Look first.** Read the cluster. If the effect is already there (another agent did it) record `already_applied`.
2. **Check it is still the right target.** The task carries the incident version (epoch) and recovery attempt; if a newer
   incident exists or the attempt changed it is **refused** (`refused_stale`) and nothing is done.
   Actions never address a pod by name (they patch the Deployment / policy by name), so a stale pod id cannot be hit;
   the only pod ids used are in the read-only snapshot, where a vanished pod is reported as such.
3. **Act.** Then read back to confirm.
4. **Classify any error:**

| What happened | Meaning | What is recorded / done |
|---|---|---|
| Refused (HTTP 400/401/403/404/409/422, webhook denial) | Definitely **not applied** | `failed`, retry with back-off (2 s, 4 s ... max 30 s) |
| Timeout, connection reset, 5xx, 429, unknown error | **May or may not have been applied** | **Read the cluster**: effect present -> `applied_after_timeout` (not repeated); absent -> `not_applied`, retry |
| Timeout **and** the cluster cannot be read | Genuinely unknown | `unknown` (shown on the dashboard as UNKNOWN); re-checked every 3 s; a later read records `unknown_resolved`. **Never blindly repeated.** |
| Invalid / missing quorum certificate | Must not act | `refused_certificate` |
| 10 failed attempts | Give up | `abandoned` + needs human attention |
| A prerequisite is not met (isolation not confirmed, snapshot pending, rollout running) | Wait | `waiting` (not counted as an attempt) |

* **Idempotent redeploy:** the pod-template change that triggers a rollout is derived from `(incident, attempt)` (`e1-a2`),
  not from the clock. Repeating the same request changes nothing and starts no second rollout. (Before this phase it used a timestamp,
  so repeating after a timeout could roll the workload twice: see `docs/AUDIT.md` section 5.)
* **Audit record** per step: id, time, agent, action, workload, incident, attempt, outcome, detail, error text, duration.
  Kept in memory (last 200, dashboard table) and appended to a **hash-chained** file `/var/log/resilience/actions.jsonl`
  (same tamper-evidence as the decision log; check with `python -m resilience verify-log <file>`).

---

## 4. Settings (`recovery` block of `k8s/resilience/10-config.yaml`)

| Setting | Default | Meaning |
|---|---|---|
| `snapshot_enabled` | true | Save evidence before replacing |
| `snapshot_timeout_s` | 15 | Max time recovery waits for it |
| `log_tail_lines` / `log_max_bytes` / `max_pods` | 200 / 65536 / 5 | Size limits |
| `max_retries` | 3 | Redeploys after the first (total attempts = 1 + this) |
| `backoff_base_s` / `backoff_factor` / `backoff_max_s` | 15 / 2 / 120 | Wait before retry n |
| `check_replacement_pods` | true | Done only when the snapshotted pods are gone |
| `action_backoff_base_s` / `_max_s` / `action_max_attempts` | 2 / 30 / 10 | Retries of a refused cluster call |
| `unknown_recheck_s` | 3 | How often an UNKNOWN result is re-read |

How long before a replacement counts as failed is the existing `timers.validate_timeout_s` (90 s).

---

## 5. Permissions added (read-only)

`resilience-agent` only (not the baseline controller) in the `healthcare` namespace: `pods` get/list and `pods/log` get
(`k8s/resilience/00-rbac.yaml`, role `resilience-evidence`). Kubernetes RBAC cannot limit these to the four
workloads' pods (pod names are generated), so **the code** limits itself to pods labelled `app=<workload>`; that restriction is code, not RBAC. No
`exec`, create, delete or watch on pods. `scripts/verify-rbac.sh` checks both the allowed and denied sides on a real cluster.

---

## 6. Simulator scenarios and tests

| Scenario (`python3 sim/local_demo.py <name>`) | Shows |
|---|---|
| `validation-retry` | First replacement fails its health check -> snapshot of it, back-off, 2nd redeploy validates -> reintegration -> HEALTHY (attempt 2) |
| `failed-validation` | Every replacement fails -> 3 redeploys, then NEEDS HUMAN ATTENTION on all four agents, still quarantined, never HEALTHY, 3 snapshots, simulated secret redacted |

`tests/test_recovery.py` (26 tests): timeout-applied is not repeated, timeout-lost is retried, unreadable cluster gives UNKNOWN then resolves without
repeating, definite refusal backs off, stale incident/attempt refused, abandonment, hash-chained audit, idempotent redeploy, isolation/snapshot/rollout gates,
redaction, pod summary has no env, snapshot honesty (`not_captured`), HEALTHY only after policy removal, retry vote and attention, status/export endpoint,
dashboard aggregation, page JavaScript syntax, config block equals code defaults, plus the two end-to-end scenarios above.

---

## 7. Limits (stated plainly)

* **Simulator only.** Not run against a real API server: real timeout behaviour, pod readiness, webhook latency and the
  rollout-in-progress check on a real Deployment controller are tested with fakes. The `K8sBackend` code paths are
  checked with mocks (`tests/test_rbac.py`, `tests/test_audit_fixes.py`) only.
* Validation failure is **simulated** by a replacement pod that reports unhealthy. Nothing real is made to misbehave.
* The retry certificate has a 5-minute life; the 120 s back-off cap keeps retries inside it, but if the API is down for longer, new
  signatures are needed (the agents re-sign on their next vote).
* Needs-attention is shown on the dashboard and written to the audit log; it does not page anyone.
* Snapshots are per agent, `emptyDir`, and pattern-redacted (see section 1).
* The 2-of-4-compromised limit of Phase 3 still applies: a certificate proves agreement, not correctness.
