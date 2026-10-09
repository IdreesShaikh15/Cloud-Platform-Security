# AUDIT: what is actually proven, and what is not

*Phase 1 of the hardening plan. Written for someone presenting this project to faculty:
every claim says where the code is, what test proves it, and what is NOT proven.*

Baseline before this phase: **57 tests passed, 6 simulator scenarios PASS** (measured, not copied).

---

## 0. How to read this document

**Status labels**

| Label | Meaning |
|---|---|
| **VERIFIED LIVE** | Shown working on your real Minikube cluster. |
| **VERIFIED IN SIMULATOR/TESTS** | Proven by an automated test or a simulator scenario. The simulator runs the real agent code, real Ed25519 signatures and real gRPC/mTLS on one machine, but fake telemetry and a fake cluster. |
| **PARTIAL** | Part of it is proven; the gap is stated. |
| **SIMULATED ONLY** | Exists only inside the simulator's fake world. |
| **MISSING** | Not implemented. |

**How "live" was decided.** You told me you live-verified three scenarios: false accusation,
forgery, and the centralized baseline. The numbers in your presentation guide (section 6) agree:
attacker flagged SUSPECT in ~29-31 s, forger flagged in ~1.1 s, healthy auth-service falsely
isolated by the compromised controller within ~4 s with a NetworkPolicy created. I mark something
LIVE only if one of those three runs necessarily exercises it. I have not seen your raw output
and I cannot reach your cluster, so where I *infer* live behaviour I say "inferred".
Everything else is not claimed live, even where it probably works.

**What I changed in this phase** is listed in section 7. Everything else is documentation.

---

## 1. Feature-by-feature classification

| # | Feature | Status | Where in the code | Proof (test / scenario) | Gap in plain English |
|---|---|---|---|---|---|
| 1 | **Monitoring**: every agent polls all 4 workloads (`/health`, `/_telemetry`) | **VERIFIED LIVE** (inferred: honest agents correctly saw the accused node as normal in your false-accusation run) | `monitoring.py: HttpTelemetrySource`; `apps/src/telemetry.py` | **New:** `tests/test_http_monitoring.py` runs the real mock app and reads it through the real HTTP path (before this, only the live cluster used that path; the simulator uses `FakeTelemetry`) | Unreachable workload yields *no* evidence (not "anomaly"). See limitations. |
| 2a | **Signal: NETWORK** (outbound connections, bytes/s) | **VERIFIED IN SIMULATOR/TESTS** | `detection.py: Detector.network` | `test_detection_reintegration.py::test_network_connections_and_rate`; simulator scenarios use `FakeWorld.attack("exfil")` | Live: only the "normal" side was seen. A real anomalous reading has never been produced on the cluster (no attack injection exists, by design). Real normal-traffic rates were never characterised, so the 256 KiB/s limit is unproven against real load. |
| 2b | **Signal: PROCESS** (allow-list; suspicious names/paths) | **VERIFIED IN SIMULATOR/TESTS** | `detection.py: Detector.process` | `test_suspicious_and_unknown_processes` | Same gap as 2a. |
| 2c | **Signal: FILE_INTEGRITY** (SHA-256 vs known-good manifest) | **VERIFIED IN SIMULATOR/TESTS** (+ real hashing path) | `detection.py: Detector.file_integrity`; `scripts/gen-baseline-hashes.py` | `test_file_integrity`; **new** `test_http_monitoring.py` hashes the real app files and detects a hash mismatch through real HTTP | Live anomaly side not exercised. |
| 2d | **Signal: AUTH** (failed logins attributed to source pod IP) | **VERIFIED IN SIMULATOR/TESTS** | `detection.py: Detector.auth` | `test_auth_failures_attributed_to_source_pod`; **new** `test_audit_fixes.py::test_auth_history_resets_when_target_gets_a_new_pod_ip` | Same gap as 2a. A bug in this signal was fixed (section 7). |
| 3 | **Evidence signing** (Ed25519, domain separated, freshness, replay cache) | **VERIFIED LIVE** for the accept path (every live agent accepted peers' signed evidence) | `evidence.py: seal, open_envelope`; `crypto.py` | `test_crypto_evidence.py` (tamper, impersonation by signature, domain separation, stale, replay, unknown signer) | Live runs show valid signatures accepted and the *mTLS-binding* rejection. A bad *signature* being rejected is proven by tests only. |
| 4 | **mTLS** between agents (CA, CN to node id) | **VERIFIED LIVE** (agents only talk over it; forgery run showed the identity check) | `peer.py`; `pki.py`; `scripts/gen-certs.py` | `test_integration_sim.py::test_mtls_rejects_client_without_platform_cert`; `test_live_wiring.py` | No certificate rotation or revocation; certificates last 365 days. |
| 5a | **Trust decay** of a lying agent | **VERIFIED LIVE** (SUSPECT at ~29-31 s) | `agent.py: _update_trust`; `trust.py` | `test_trust_quorum.py::test_linear_decay_and_recovery`; `test_integration_sim::test_false_accusation_is_withheld_and_liar_loses_trust` | none |
| 5b | **Trust recovery** (+0.5/s of a restored agent) | **PARTIAL** | same | Only the arithmetic is unit-tested. There is **no end-to-end test** that a liar's trust climbs back after `restore`, and I cannot confirm you saw it live. | Phase 5 will plot and test it. |
| 5c | **Workload trust** (drops on anomaly, 0 on isolation, +2/s while reintegrating) | **VERIFIED IN SIMULATOR/TESTS** | `agent.py: _update_trust` | `test_genuine_compromise_full_cycle` | Not live. |
| 6 | **Weighted score W(T)** | **VERIFIED LIVE** for the "lone liar stays below 0.6" side; **SIMULATOR/TESTS** for the corroborated side | `quorum.py: weighted_score, should_vote_contain` | `test_trust_quorum.py` (6 tests: single liar capped at 0.495, diversity bonus, trust weighting, hearsay never votes) | The above-threshold path has never run on the cluster. |
| 7 | **Quorum (3 of 4 signed votes)** | **VERIFIED IN SIMULATOR/TESTS** (live: only "stays at 1 of 3") | `quorum.py: VoteBook`; `agent.py: _on_commit` | `test_quorum_requires_three_distinct_eligible_voters`, `test_votes_for_different_epochs_do_not_mix`, `test_genuine_compromise_full_cycle` | No cryptographic quorum certificate is checked by anything outside the agents (Phase 3). |
| 8 | **Vote exclusion** (< 40 trust) | **VERIFIED IN SIMULATOR/TESTS** | `agent.py: eligible_voter` | `test_untrusted_voter_excluded` | Excluded agents' *evidence* still counts, only down-weighted (Phase 3). |
| 9 | **Isolate** (NetworkPolicy QUARANTINE) | **PARTIAL.** *Creating* the policy is **VERIFIED LIVE** (baseline run, same `K8sBackend.apply_stage`). The distributed executor path is simulator only. **Calico actually blocking traffic is not systematically verified.** | `response.py: K8sBackend.apply_stage`; `reintegration.py: network_policy`; `agent.py: _on_commit` | `test_audit_fixes.py` (policy shape); `test_genuine_compromise_full_cycle` | Run `scripts/verify-isolation.sh` on your cluster. Section 3. |
| 10 | **Recover** (redeploy from known-good image) | **PARTIAL** (code shared with the baseline; your guide claims only "policy created" live) | `response.py: K8sBackend.recover, recovery_done` | `FakeBackend` in the simulator; mock-client test that every call has a timeout | The real Deployment patch is not claimed live for recovery. |
| 11 | **Validate** (health + hashes + new instance + no anomaly) | **VERIFIED IN SIMULATOR/TESTS** | `agent.py: validation_check` | `test_genuine_compromise_full_cycle` | Not live. If the pod's telemetry was unreachable at the commit instant, "is it still the old instance" cannot be checked (section 5). |
| 12 | **5 reintegration stages** | **VERIFIED IN SIMULATOR/TESTS**; Calico enforcement of stages 2-4 **not verified** | `reintegration.py`; `agent.py` | Stage order asserted in `test_genuine_compromise_full_cycle`; `test_network_policies_per_stage` | `verify-isolation.sh --stage RESTRICTED` checks stage 2. |
| 13 | **Executor fail-over** (ranked A<B<C<D, stagger) | **VERIFIED IN SIMULATOR/TESTS** (new). Before this audit it was **untested**: the agent-crash scenario kills D, rank 3, which never needs fail-over. | `agent.py: executor_rank, _schedule, _run_tasks` | **New:** `tests/test_executor_failover.py` (A dead, B isolates, recovers and reintegrates; C and D stay out) | Not live. |
| 14 | **Centralized baseline** | **VERIFIED LIVE** (false isolation in ~4 s); crash case simulator only | `baseline.py` | `test_centralized_baseline_false_isolates`; `test_crash_scenarios.py` (controller crash) | While the controller stays "compromised" it re-isolates every time it reintegrates the workload (expected for a compromised controller). |
| 15 | **Agent-crash tolerance** | **VERIFIED IN SIMULATOR/TESTS** | `world.py: crash_agent` | `test_crash_scenarios.py` | Not live. Network partitions (2+2 split) are not tested at all (MISSING). |
| 16a | **Dashboard: node cards** | **VERIFIED LIVE** (used in your runs) | `dashboard/index.html`, `server.py: aggregate` | `test_dashboard_collector_merges_events_and_explains_votes` (server side) | The page's JavaScript has no automated test. |
| 16b | **Dashboard: event timeline** (12 categories, filters, export) | **VERIFIED LIVE** | `observability.py`; `server.py: Collector` | `test_observability.py` (16 tests) | same |
| 16c | **Dashboard: agent drawer** (measurements, trust trend, W(T) breakdown, vote reasons) | **VERIFIED LIVE** (the guide has screenshots) | `agent.py: status` | `test_status_exposes_event_stream_and_since` | same |
| 16d | **Dashboard: decisions, pending votes, rejections, metrics table** | **VERIFIED LIVE** | `server.py` | observability tests | same |
| 17 | **Metrics** TTD/TTI/TTR/TTV/TTF, time-to-flag, false isolation, availability | **VERIFIED LIVE** for time-to-flag and baseline false isolation; **SIMULATOR** for TTD to TTF of a genuine attack | `metrics.py` | `test_genuine_compromise_full_cycle` (ordering ttd <= tti <= ttr <= ttv <= ttf) | A genuine-attack timeline has never been measured on the cluster. |
| 18 | **Genuine app compromise on the live cluster** | **MISSING** (deliberate: no attack injection exists; the ground rules forbid writing one) | n/a | n/a | The simulator is the only evidence for the full pipeline. |
| 19 | Compromised-agent variants "goes silent" / "blocks a legitimate isolation" | **MISSING** | n/a | n/a | Listed as future work in the README. |
| 20 | Quorum certificates + admission webhook (added in Phase 3; see `docs/SECURITY.md`) | **VERIFIED IN SIMULATOR/TESTS**, not live | `agent/resilience/{certificate,admission,webhook}.py`; `k8s/resilience/40-webhook.yaml` | `tests/test_certificate.py` (22), `test_admission.py` (23), `test_enforcement.py` (6: whole pipeline under enforcement, lone agent refused, B fails over), `test_verify_webhook.py` (7) | Never applied to a real API server. `scripts/verify-webhook.sh` checks it safely (dry runs). Admins are exempt by design; 2 of 4 compromised agents break it. |
| 20b | Least-privilege RBAC (Phase 3) | **VERIFIED IN SIMULATOR/TESTS** (manifest logic) | `k8s/resilience/00-rbac.yaml` | `tests/test_rbac.py` (derived from the real backend calls; 40 forbidden cases) | `scripts/verify-rbac.sh` checks a live cluster. |
| 20c | Excluded agents rejected, trust persisted, tamper-evident decision log, dashboard token (Phase 3) | **VERIFIED IN SIMULATOR/TESTS** | `agent.py`, `decisionlog.py`, `dashboard/server.py` | `test_trust_hardening.py` (8), `test_decision_log.py` (11), `test_dashboard_auth.py` (7) | Hash chain cannot stop a full-access rewrite; trust file lost if the pod is deleted. |
| 21 | 2-of-4 compromised experiment | **MISSING** | n/a | n/a | Phase 5. |
| 22 | **Targeted investigation** (added in Phase 2; see `docs/INVESTIGATION.md`) | **VERIFIED IN SIMULATOR/TESTS**, not live | `agent/resilience/investigation.py`; `Investigate` RPC in `proto/resilience.proto` | `tests/test_investigation.py` (35), `tests/test_investigation_scenarios.py` (9), `tests/test_dashboard_investigations.py` (3); scenarios `transient-blip`, `slow-burn`, `ambiguous` | Never run on the real cluster; sampling rates and thresholds are untuned against real telemetry. |

---

## 2. What changed in the test count

57 tests before this phase, **84 after** (27 new: 15 for `verify-isolation.sh`, 7 for the audit fixes, 3 for real HTTP monitoring, 2 for executor fail-over / isolation ordering). All 6 simulator scenarios still PASS.

---

## 3. The isolation NetworkPolicy under Kubernetes / Calico semantics

### 3.1 How NetworkPolicy works (the three facts everything below depends on)

1. A pod that is selected by *any* policy with `policyTypes: [Ingress]` accepts **only** what some policy allows in. Same for `Egress`. **Ingress and egress are separate**: isolating one does not isolate the other. Our policy lists both types, with an *empty* egress list in QUARANTINE, which means "no outbound traffic at all".
2. Policies are **stateful**: when an inbound connection is allowed, the *reply* packets flow back even if egress is blocked. This is why an agent can keep asking a quarantined workload "are you healthy?" while the workload cannot start any connection of its own.
3. Policies apply to **pod-to-pod traffic as the CNI sees it**. Traffic that originates on the node itself (kubelet readiness probes, `kubectl port-forward`) is *not* filtered by Calico NetworkPolicy.

### 3.2 What the policy does, stage by stage

The policy is named `resilience-isolate-<workload>` and selects pods labelled `app=<workload>` in the `healthcare` namespace. Code: `agent/resilience/reintegration.py: network_policy`.

| Traffic | QUARANTINE | RESTRICTED | MONITORED / PEER_VALIDATED | Why |
|---|---|---|---|---|
| **Ingress** from the 4 agents (and the baseline controller), TCP 8080 | allowed | allowed | allowed | Agents must monitor, validate and judge the workload. |
| Ingress from other resilience-namespace pods (e.g. the dashboard) | **blocked** (changed in this phase; was "whole namespace") | blocked | blocked | The dashboard reads agents' status pages, never the workloads. |
| Ingress from other healthcare pods incl. the **client pod** (users) | blocked | blocked | allowed | Quarantine means no user traffic. Peers return at stage 3. |
| Ingress from any other namespace | blocked | blocked | blocked | |
| **Egress** to anything (the workload's own outbound traffic) | **blocked** | only healthcare namespace + DNS | same as RESTRICTED | A compromised workload must not exfiltrate or move sideways. |
| **DNS** lookups by the workload | **blocked** | allowed (UDP/TCP 53 to kube-system) | allowed | DNS can leak data; it returns with RESTRICTED. |
| Replies to the agents' monitoring requests | allowed (stateful) | allowed | allowed | See fact 2. |
| Kubelet readiness probes | **not filtered** by Calico | same | same | See fact 3; recovery needs the new pod to become Ready. |
| `kubectl port-forward` to the workload | **not filtered** | same | same | **Honest caveat:** port-forwarding to `patient-portal` still works while it is "isolated". The portal *looks* broken in the demo only because its backends are cut off. |

### 3.3 Gaps found and what I did

| Gap | Severity | Action |
|---|---|---|
| Ingress allowed from the **entire** `resilience` namespace, on **any** port | low | **Fixed.** Now only pods labelled `app=resilience-agent` or `app=central-controller`, TCP 8080 (`AGENT_APP_LABELS`, `WORKLOAD_PORT`). A test checks the labels match the manifests so a rename cannot silently lock the agents out. |
| Later stages allow the **whole** healthcare namespace (all pods, all ports) | low | **Documented, not changed.** It is the intended "peers may talk again" step and tightening it blind could break the demo. |
| Recovery could start while the quarantine policy had **not** been applied (executor tasks were independent) | medium | **Fixed.** Recovery now waits until isolation is confirmed in the cluster (`agent.py: recover()` gate). Test: `test_recovery_waits_for_isolation_to_succeed` (fails without the fix). |
| Agent traffic, DNS, client pod, kubelet probes: do they get blocked by mistake? | n/a | By design no (table above). **Not verified on a real Calico**, so use the script below. |
| Port-forward / node-originated traffic bypass NetworkPolicy | inherent | Documented. |

### 3.4 Verifying it yourself: `scripts/verify-isolation.sh`

I cannot run Calico here. The script checks, on your cluster and for **one** workload you name, that:

* **before** isolation everything is reachable (otherwise it stops, nothing changed);
* **during** isolation: the client pod is blocked (ingress); the agent pod is still allowed; the workload cannot reach a peer service by IP (egress) and cannot resolve DNS (QUARANTINE); the target pod stays Ready (kubelet probe not blocked);
* **after** removal everything is reachable again and the policy is gone.

It prints PASS/FAIL per step. It refuses to start if the policy name already exists, deletes
only a policy carrying its own run id, restores the cluster on exit/Ctrl-C/error, and exits
non-zero on any failure (exit 3 if cleanup itself failed). All probes are benign HTTP GETs and
DNS lookups. Its **logic** is tested with a fake `kubectl` (`tests/test_verify_isolation.py`,
15 tests, including Ctrl-C cleanup and a policy that is no longer ours); that proves the script
behaves, **not** that Calico enforces anything.

How to find the arguments:

```bash
kubectl -n healthcare get pods                         # pick the 'client-...' pod and note the target workload
kubectl -n resilience get pods -l app=resilience-agent # pick any agent pod
scripts/verify-isolation.sh --namespace healthcare \
    --client-pod <client-pod-name> --target records-api \
    --agent-pod <agent-pod-name> --egress-peer auth-service
# other stages:  add  --stage RESTRICTED   (stage MONITORED/PEER_VALIDATED cannot prove enforcement by itself)
```

Run it on a demo cluster while no incident is in progress; it briefly cuts the target off from users.

Expected: every line `[PASS]`, final line `=== summary: N passed, 0 failed ...`, exit code 0.
If a `[FAIL]` says "expected blocked but saw reachable", Calico is not enforcing that rule
(check `kubectl -n kube-system get pods -l k8s-app=calico-node`).

---

## 4. RBAC: every permission, and whether it is used

There is **one** service account for the whole platform's actors: `resilience-agent` (namespace
`resilience`), used by `agent-a..d` **and** by the baseline `central-controller`. The dashboard
uses the namespace's *default* service account, which has **no** permissions bound.
There are **no** ClusterRoles or cluster-wide bindings (good).

| Role (namespace) | API group / resource | Verbs granted | Used? | Where used | Broader than needed? |
|---|---|---|---|---|---|
| `resilience-actuator` (healthcare) | `networking.k8s.io` / `networkpolicies` | `get` | yes | `current_stage` | |
| | | `create` | yes | `apply_stage` (first isolation) | `create` cannot be limited by name; allows creating any policy |
| | | `update` | yes | `apply_stage` (replace) | applies to **all** policies in the namespace |
| | | `delete` | yes | `apply_stage(FULL)` | can delete **any** policy in the namespace, including ones the platform did not create |
| | | `list`, `patch` | **no** | | remove |
| | `apps` / `deployments` | `get` | yes | `recovery_done`, `read_state` | all deployments, incl. `client` |
| | | `patch` | yes | `recover`, `write_state` | **any field** of any deployment: a compromised agent could change an image, command or env. This is the biggest single power. |
| | | `list` | **no** | | remove |
| | core / `pods` | `get`, `list` | **no** (nothing reads pods) | | remove |
| `resilience-reader` (resilience) | core / `configmaps` | `get` | yes (one object) | `read_marker` reads `cr-attack-marker` | can read all ConfigMaps there (`resilience-config`, `peer-pubkeys`, `known-good-hashes`); `resourceNames: [cr-attack-marker]` would be enough |

What the agents can **not** do: read Secrets through the API (their keys arrive as mounted
volumes), touch other namespaces, create pods, or change RBAC.

Other privilege notes:
* The service-account token is mounted in every agent container; whoever owns an agent container owns these permissions. Four agents share one identity, so one compromised agent holds the powers of all.
* Nothing protects the `resilience` namespace with a NetworkPolicy; any pod can reach the agents' unauthenticated read-only status port 8081 and the dashboard (port 8090). The gRPC port needs a client certificate.

**Update (Phase 3): done.** The least-privilege changes below were made and tested (`docs/SECURITY.md` section 4,
`tests/test_rbac.py`, `scripts/verify-rbac.sh`); the table above describes the state *before* them. Planned then,
implemented now: drop the unused verbs/resources; `resourceNames` on deployments (the 4
workloads) and on networkpolicy get/update/delete (`resilience-isolate-*`); one `resourceNames` rule for
the marker ConfigMap; a separate, smaller identity for the baseline controller.

---

## 5. Error handling: failed or timed-out Kubernetes API calls, and stale identifiers

| Situation | Behaviour found | Status |
|---|---|---|
| API call hangs | **Bug:** the Kubernetes client had no timeout. The agent's tick holds one lock that the status page also needs, so one hung call froze the whole agent (no monitoring, no votes, dashboard shows it down). | **Fixed:** every call has a (3 s connect, 10 s read) timeout. Tests: `test_hung_api_server_cannot_block_the_agent_forever` (real client against a server that never answers), `test_every_api_call_passes_a_request_timeout`. |
| API error while performing an isolation / recovery / stage change | Caught per task; an ACTION event "failed ... retrying in 2s" is logged; retried **forever** every 2 s, no back-off, no limit. | **Fixed in Phase 4:** back-off, an attempt limit (10) and "needs human attention"; see `docs/RECOVERY.md` section 3. |
| API error while *tracking* progress (phase stuck in ISOLATED / RECOVERING) | **Bug:** swallowed at debug log level, so the dashboard showed a target stuck for no visible reason. | **Fixed:** a throttled (1 per 10 s per target) ACTION event and a warning. Test: `test_unreadable_cluster_state_is_reported_not_silent`. |
| A timeout on a *write* | Treated as failure and retried. | Safe for NetworkPolicy replace/create/delete and annotation patches (idempotent). **Was not safe for `recover()`** (a timestamp in the pod template meant a repeat started a *second* rollout). **Fixed in Phase 4:** the template value is derived from (incident, attempt), a timeout triggers a read of the cluster before anything is repeated, and an unreadable cluster shows UNKNOWN (`docs/RECOVERY.md`). |
| A *read* fails (`read_state`) | Returns an empty dict, which looks like "no state / not done yet". | Executor repeats the idempotent action, fine. After an **agent restart with the API down**, `resync_from_cluster` gets nothing and the agent believes `HEALTHY, epoch 0`; its CONTAIN votes then carry a stale epoch and cannot commit with peers at a newer epoch. Documented. |
| Acting on a stale **pod** identifier | The platform never acts on a pod id: NetworkPolicies select by label and recovery patches the Deployment by name, so an old pod name cannot be hit. Pod-derived values are only used for *detection*: `instance_id` (new-instance check) and `pod_ip` (AUTH attribution). | OK for actions. |
| Stale `pod_ip` in the AUTH signal | **Bug:** failure counters are cumulative per IP; a recovered pod landing on an IP an old attacker pod once used would inherit that IP's old failures as a burst. | **Fixed:** the window restarts when the target's pod IP changes. Test: `test_auth_history_resets_when_target_gets_a_new_pod_ip`. |
| Stale `contaminated_instance` | Captured from each agent's *own* last snapshot at commit time; if telemetry was unreachable then, it is empty and "still the old instance" cannot be detected; two agents can also capture different instances if a restart straddles their polls. The hash and anomaly checks still apply. | Documented. |
| Superseded incident | Every executor task carries the epoch and is dropped if the local epoch moved on. Commits for the wrong epoch/phase are ignored and logged. | OK (existing). |
| Telemetry timeout (1.5 s) | Snapshot is `reachable=False`: **no evidence is produced** for it, and validation fails closed ("health check failed"). | OK for validation. A crashed or silenced workload is not itself treated as an attack. |
| "HEALTHY" shown too early | An agent flips a target to HEALTHY the instant the final quorum commits; the executor removes the policy up to `rank x stagger` seconds later (seen in the fail-over test). The dashboard therefore shows *intent*, not the observed cluster. | **Fixed in Phase 4:** HEALTHY is set only after the cluster is read and the isolation policy is gone (`docs/RECOVERY.md` section 2). |

---

## 6. Known limitations, stated plainly

1. **No live attack injection.** The genuine-compromise pipeline has been shown only in the simulator. On the cluster only the false-accusation, forgery and baseline scenarios were run.
2. **Simulator numbers are not real-cluster numbers.** Timers are accelerated and the "cluster" is in memory.
3. **Calico enforcement is unproven** from my side. `verify-isolation.sh` exists so you can prove it.
4. **Threshold detection only**, with limits never calibrated against real load. Possible false alarms or misses on a busy cluster.
5. ~~**One agent's RBAC can act alone.**~~ *Addressed in Phase 3:* an admission webhook now refuses an agent's changes without a valid 3-signature certificate (not yet run on the real cluster; admins are exempt; 2 of 4 compromised agents break it).
6. ~~**An excluded agent's evidence still counts**~~ *Addressed in Phase 3:* it is rejected entirely, trust returns only over time through peers' observations, and a restart no longer wipes an exclusion (container restart; a deleted pod still loses the state file).
7. **Agent restart loses memory:** trust scores, pending votes, queued tasks. Only phase/epoch/stage are restored from Deployment annotations.
8. **Unauthenticated status endpoints:** the agents' :8081 remain open to any pod. The dashboard :8090 now supports an optional access token (Phase 3, off by default).
9. **The compromise switch is a file** (`/tmp/cr-sim/compromise.json`) writable via `kubectl exec`. It exists to demonstrate a lying agent and must not ship in a real deployment.
10. **Isolation does not stop** node-originated traffic (kubelet probes, port-forward).
11. **No network-partition testing**, no "goes silent" / "blocks isolation" attacker variants, no 2-of-4 compromise run.
12. **Keys and PKI:** the CA key lives on the host that ran `gen-certs.py`; private keys sit in ordinary Kubernetes Secrets; no rotation or revocation; clocks must agree within 15 s.
13. **Assumes one container named `app`** per workload (the recovery patch targets it by name).
14. **The synthetic healthcare data is random;** nothing here is real patient data.

---

## 7. Changes made in this phase (all small; nothing else touched)

| File | Change | Why |
|---|---|---|
| `agent/resilience/response.py` | Request timeout on every Kubernetes call | A hung API call froze the agent |
| `agent/resilience/agent.py` | Recovery waits for confirmed isolation; cluster-tracking failures are reported | Ordering bug; silent stall |
| `agent/resilience/detection.py` | AUTH window resets on pod-IP change | Stale identifier |
| `agent/resilience/reintegration.py` | Ingress only from agent / controller pods on TCP 8080 | Isolation gap (dashboard could reach quarantined pods) |
| `scripts/verify-isolation.sh` | New | Lets you verify Calico on the real cluster |
| `tests/` | `test_audit_fixes.py`, `test_executor_failover.py`, `test_http_monitoring.py`, `test_verify_isolation.py`, `fake_kubectl.py` | Proof for the above |

**Rebuild required for your cluster:** the agent image (`scripts/build-images.sh`, then restart agents) to get
the new policy shape, timeouts and ordering gate. The dashboard and app images are unchanged.
