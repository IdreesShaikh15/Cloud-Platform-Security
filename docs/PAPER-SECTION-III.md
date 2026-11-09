# Paper Section III – source inspection and implementation verification

> **Parts 3–7 of the earlier draft are superseded.** The corrected paper text, figure captions, tables and LaTeX are in `docs/PAPER-SECTION-III-FINAL.md`. Parts 1–2 below remain the source-inspection record; the verification addendum at the end records the four follow-up checks.

Paper: *Distributed Cyber-Resilience for Containerized Healthcare Applications*.
Everything below was derived from reading the repository source. Where a statement could not be confirmed from code it is marked **(unverified)**.
Evidence classes used throughout: **Implemented** (code exists and is exercised by tests), **Simulated** (exercised only against the in-process simulator / `FakeWorld` / `FakeBackend`), **Sandbox-cluster** (exercised on the 4-node Minikube/Calico sandbox, K8s v1.34.12), **Proposed** (design or documentation only). The user's own laptop cluster (K8s v1.37.0) has **no** recorded results (unverified).

---------------------------------------------------------------------------------------------------

# PART 1 – PROJECT INSPECTION

## 1.1 Overall architecture
* Four identical peer security agents A–D (`NODE_IDS` in `agent/resilience/config.py`), one per node/zone. No leader, no central controller (a centralized baseline exists only as an *experimental comparison*, `k8s/baseline/`).
* Six protected workloads A–F (`config/workloads.json`; `ClusterConfig.nodes` = the four agent-hosted workloads, `extra_workloads` = E and F, watched by all four agents but with no agent of their own; `targets` = all six). Agents A–D are the *quorum parties*; workloads A–F are the *protected assets*. These are different sets.
* Each agent runs the same loop (`Agent.tick`, `agent/resilience/agent.py`): observe own workload and the other workloads → derive evidence → sign and broadcast → update peer-trust → weighted score → vote → collect votes → on quorum, execute (executor chosen by rank) → track the incident through the recovery pipeline.
* Decision state is replicated by exchange of signed messages (evidence, votes, certificate shares), not by a shared database. No database is used: state lives in memory, ConfigMaps / Deployment annotations, and `emptyDir` files.

## 1.2 Monitoring and evidence generation
* Telemetry: each workload reports its own `/proc`-derived counters through plain-HTTP `/health` and `/_telemetry` (`apps/src/telemetry.py`); `HttpTelemetrySource` (`monitoring.py`) polls with a 1.5 s timeout once per tick (1 s cluster, 0.5 s simulator). Telemetry is self-reported by the workload being observed (a trust assumption, see Limitations).
* Detection (`detection.py`, class `Detector`, `Thresholds`) is **threshold/rule-based only – no machine learning**. Four signal types:
  * NETWORK – outbound connections (θ = 8, s = 16) and transmit rate (θ = 256 000 B/s, s = 10⁶); the larger confidence is kept.
  * PROCESS – suspicious names/directories 0.95; process not in the allowlist 0.6.
  * FILE_INTEGRITY – SHA-256 against a known-good manifest (`baseline.py`): modified 0.95, missing 0.8, new 0.7.
  * AUTH – failed authentications from the target pod IP (θ = 10 within 10 s, s = 40).
* Confidence ramp: `c = 0.5 + 0.5·min(1, (x − θ)/s)` for `x > θ`, else 0. A sensitivity multiplier is applied for workloads in reintegration (0.7 for MONITORED, 0.85 for the watch stage).
* An `Evidence` message carries observer, target, signal type, confidence and timestamp (`evidence.py`, `proto/resilience.proto`).

## 1.3 Ed25519 signing
* Every `Evidence`, `Vote`, and certificate share is wrapped in a protobuf `SignedEnvelope` (`evidence.py: seal`). The Ed25519 signature (`crypto.py`, `Signer`/`KeyRegistry`, `cryptography` library) is over `kind || 0x00 || payload`, i.e. the whole serialized claim with a domain-separation prefix; quorum-certificate signatures use the domain `qc-v1`.
* `open_envelope` verification order: mTLS peer identity equals the claimed signer → signer known → kind valid → signature valid → payload parses → payload author equals signer → timestamp skew ≤ 15 s → replay cache (TTL 120 s). Any rejection **penalizes the delivering mTLS identity by 20 trust points**.
* Each agent holds its own private key; all public keys are in a `KeyRegistry` distributed through configuration. Key rotation/revocation: **not present**.

## 1.4 gRPC / mTLS
* Transport (`peer.py`, `PeerServer`/`PeerClient`; `pki.py`): gRPC with mutual TLS (`require_client_auth=True`), ECDSA P-256 CA and leaf certificates, CN `agent-x` mapped to node id. RPCs: `SubmitEvidence`, `SubmitVote`, `SubmitShare`, `Ping`, `Investigate`.
* 2 s per-call timeout, keepalive 10 s. There is **no explicit retry policy**; reliability comes from periodic re-broadcast (votes re-sent every 4 s, certificate shares every 2 s).
* Telemetry polling between agent and workload is **not** mTLS (plain HTTP inside the cluster).

## 1.5 Trust management (exact formulas)
Source: `trust.py` (`TrustLedger`, `linear_step`), `agent.py: _update_trust`, `config.py: TrustParams`.
* Linear update: `T(t+Δt) = clamp(T(t) + r·Δt, 0, 100)`; initial `T = 100`.
* **Agent (peer) trust**, each agent keeps its own view of the other three. A peer's claim is *contradicted* when its confidence ≥ 0.5, its age ≥ `contradiction_grace_s` (8 s), and this agent's own maximum confidence for that target since `e.ts − 2 s` is < 0.2. A contradicted peer decays at 2.5 /s (5 /s in the simulator); otherwise it recovers at +0.5 /s. A rejected message costs −20 (one-off).
* Thresholds: SUSPECT < 50 (flag and event only); vote-excluded < 40 (the peer's votes are not counted and its evidence is rejected entirely on receipt).
* **Workload trust**: decays 10 /s while the workload is anomalous (local anomaly score ≥ 0.3), recovers +2 /s otherwise; forced to 0 while isolated/recovering/validating; climbs back during reintegration under stage thresholds 0/20/40/60/80 with a 10 s dwell.
* Dashboard "Agent Trust" card = median of the *other three* agents' views of that agent (`dashboard/server.py: trust_matrix_view`). Median-of-all-views in the `trust.py` docstring and the unused helper `consensus_trust()` do not describe what the dashboard computes.

### Weighted evidence score (`quorum.py: weighted_score`)
For each target and signal type *k*, per sender the maximum confidence `c` is taken; `w = c·T/100` where `T` is the *local* trust in that sender;
`mean_k = mean(w)`, `support_k = min(1, |S_k|/2)`, `score_k = mean_k·support_k`,
`W = max_k score_k + 0.15·max(0, |corroborated| − 1)`, where *corroborated* is the set of signal types with at least two distinct senders.
Vote rule (`should_vote_contain`): vote CONTAIN iff own local maximum confidence ≥ 0.5 **and** `W ≥ 0.6`; investigation-authorized path: own persistent anomaly, local ≥ 0.3 and `W ≥ 0.3`.

## 1.6 Quorum mechanism
* `QuorumParams`: n = 4, f = 1, `quorum = 2f+1 = 3` distinct eligible voters (`config.py`). `VoteBook` (`quorum.py`) keys votes by `action_key = ACTION:target:epoch:stage`; `new_commits()` fires once ≥ 3 distinct eligible voters have voted for the same key. No negative votes exist: an agent either votes for the action or stays silent.
* **Cases asked about:**
  * *3 agents available* – commit is possible (3 distinct votes).
  * *2 agents available* – **no commit** (strict ≥ 3); the system fails safe by *not acting*.
  * *3 compromised agents* – three colluding signers can produce 3 valid votes and a valid certificate; the mechanism offers **no protection beyond f = 1** (exactly as for any threshold scheme).
  * *1 compromised agent* – it cannot reach quorum alone; its false evidence is down-weighted by contradiction-driven trust decay and its votes are excluded below 40.
* **Distinction from PBFT:** this is *application-level threshold voting* over signed messages. There is no leader/primary, no views, no pre-prepare/prepare/commit phases, no total ordering of requests, no view change, no state-machine replication. The "BFT-style / n = 3f+1" wording in `README.md` and the `quorum.py` docstring refers only to the sizing n = 3f+1 and 2f+1 intersection argument; **the system does not implement or claim PBFT or formal BFT consensus guarantees** (documentation discrepancy, see Part 2).

## 1.7 Containment and recovery
* Phases `HEALTHY → ISOLATED → RECOVERING → VALIDATING → REINTEGRATING → HEALTHY` (`agent.py: _step`, `_track_cluster`); stages `QUARANTINE → RESTRICTED → MONITORED → PEER_VALIDATED → FULL` (`reintegration.py: STAGES`).
* Executor selection: `executor_rank` (`agent.py`) ranks agents per incident; later ranks wait 3 s per rank (failover stagger). Actions are idempotent; ambiguous outcomes are re-checked before retry.
* Containment (`response.py: K8sBackend`): create NetworkPolicy `resilience-isolate-<workload>`; evidence snapshot (`forensics.py`) is taken **before** the pod is replaced; recovery replaces the workload from the known-good image `cr-healthcare-app:known-good` (Deployment `Recreate` strategy); retries ≤ 3 with backoff, then `NEEDS_ATTENTION`.
* Reintegration: per-stage NetworkPolicy (`reintegration.py: network_policy`), `can_advance` requires dwell 10 s, workload trust ≥ stage threshold, no anomaly < 0.3, and validation checks (`agent.py: validation_check`).
* Targeted investigation (`investigation.py`): `classify` → CORROBORATED / FALSE_POSITIVE / AMBIGUOUS / UNCERTAIN, `decide` gates authorization; the `Investigate` RPC asks peers for a fresh opinion.
* Audit: hash-chained decision log (`decisionlog.py`).
* Status split: containment and recovery are **Implemented** and **Sandbox-cluster** tested (K8sBackend + Calico enforcement) and exhaustively **Simulated**. The user's own cluster: unverified.

## 1.8 Kubernetes / Minikube / Calico / admission webhook
* Quorum certificates (`certificate.py`): a `Statement` (workload, target, action, stage, epoch, evidence_digest, expires_ms) signed by ≥ 3 distinct agents (Ed25519, domain `qc-v1`); TTL 300 s, max 900 s. Assembled by `CertificateManager` from `SubmitShare` messages.
* Admission webhook (`webhook.py`, `admission.py`): enforces only for requests by `system:serviceaccount:resilience:resilience-agent` in namespace `healthcare` – it permits NetworkPolicy `resilience-isolate-*` objects and Deployment updates limited to recovery fields only when a valid certificate (annotation) accompanies them. Fail-safe deny, `failurePolicy: Fail`. Other principals (e.g. an administrator) are **not** constrained by this webhook.
* RBAC: least-privilege roles in `k8s/resilience/00-rbac.yaml`.
* Calico (set up by `scripts/setup-cluster.sh`, 4-node Minikube, zones a–d, 2 CPU / 2200 MiB per node) provides NetworkPolicy enforcement. Real-cluster validation requires: a CNI that enforces NetworkPolicy, the webhook CA bundle injected, cluster-time sync across nodes (±15 s skew), and the baseline image loaded into every node.
* Tooling pins: Python 3.11-slim images, grpcio 1.84.0, protobuf 7.36.2, cryptography 50.0.1, kubernetes 36.0.3, stdlib HTTP dashboard.

## 1.9 Software architecture and modularity
`agent/resilience/`: `config`, `crypto`, `pki`, `evidence`, `peer`, `monitoring`, `detection`, `baseline`, `trust`, `quorum`, `investigation`, `reintegration`, `certificate`, `admission`, `webhook`, `response`, `forensics`, `decisionlog`, `agent`, `simhooks`; plus `proto/`, `sim/world.py` (simulator that runs the real agent code), `dashboard/`, `apps/`, `k8s/`, `scripts/`, `tests/`. Backends are swappable (`K8sBackend` vs `FakeBackend`), which is what allows the same decision code to run in simulator and cluster.

---------------------------------------------------------------------------------------------------

# PART 2 – IMPLEMENTATION VERIFICATION REPORT

| Component | Intended functionality | Actual implementation | Source file / function | Status | Limitations |
|---|---|---|---|---|---|
| Peer agents A–D | Four identical decentralized agents | Same code, per-node identity | `agent.py: Agent` | Fully implemented | Only n = 4 tested |
| Telemetry collection | Observe workload behaviour | Workload self-reports from /proc via HTTP | `apps/src/telemetry.py`, `monitoring.py: HttpTelemetrySource` | Partially implemented | Self-reported, unauthenticated, plain HTTP; a compromised workload can lie |
| Detection | Convert telemetry to evidence | Threshold/rule ramp, 4 signals, SHA-256 manifest | `detection.py: Detector` | Fully implemented | No ML; fixed thresholds; no zero-day capability claimed |
| Evidence messages | Signed, verifiable claims | protobuf `SignedEnvelope`, Ed25519 | `evidence.py: seal/open_envelope`, `crypto.py` | Fully implemented | No key rotation/revocation |
| Replay/skew defence | Reject stale/replayed messages | 15 s skew, 120 s replay cache | `evidence.py: ReplayCache` | Fully implemented | Clock skew can penalize honest senders |
| gRPC + mTLS | Authenticated channel | Mutual TLS, ECDSA P-256 CA/leaf | `peer.py`, `pki.py` | Fully implemented | No explicit retry policy; static PKI, no rotation |
| Peer trust | Credibility of peers | Linear decay/recovery, contradiction rule | `trust.py`, `agent.py: _update_trust` | Fully implemented | Parameters hand-chosen, not tuned; `consensus_trust()` unused |
| Weighted score | Combine evidence | `weighted_score` | `quorum.py` | Fully implemented | Heuristic; no probabilistic justification |
| Quorum voting | 3-of-4 agreement | `VoteBook`, `action_key`, ≥ 3 distinct voters | `quorum.py` | Fully implemented | Not PBFT/BFT protocol; no negative votes; tolerates f = 1 only |
| Quorum certificate | Transferable proof of decision | ≥ 3 Ed25519 signatures on `Statement` | `certificate.py` | Partially implemented | Replay-consumption check (`consumed`) exists in `verify_certificate` but is never supplied by `_qc`/webhook |
| Admission webhook | Cluster refuses unauthorized containment | Validating webhook for the agent service account | `webhook.py`, `admission.py`, `k8s/resilience/40-webhook.yaml` | Partially implemented | Sandbox-cluster tested only; constrains only the agent SA; fail-closed can block recovery if the webhook is down |
| Containment | Isolate compromised workload | Per-workload NetworkPolicy | `response.py: K8sBackend`, `reintegration.py: network_policy` | Fully implemented (sandbox) / Simulated (sim) | Enforcement depends on Calico; laptop unverified |
| Evidence preservation | Forensic snapshot before replacement | Snapshot then replace | `forensics.py`, `agent.py` | Fully implemented | Snapshot content limited to what telemetry exposes |
| Recovery | Replace with known-good | Image `cr-healthcare-app:known-good`, Recreate, retries ≤ 3 | `response.py`, `agent.py: _step` | Fully implemented | Image provenance/signing not verified |
| Staged reintegration | Gradual restoration | 5 stages, thresholds 0/20/40/60/80, dwell 10 s | `reintegration.py` | Fully implemented | Fixed schedule |
| Targeted investigation | Reduce false positives | classify/decide + `Investigate` RPC | `investigation.py` | Fully implemented | Heuristic outcome classes |
| Executor failover | Survive executor loss | `executor_rank`, 3 s stagger | `agent.py: executor_rank` | Fully implemented | Duplicate action possible, handled by idempotence |
| Decision log | Tamper-evident audit | Hash chain | `decisionlog.py` | Partially implemented | Local per agent; not anchored externally |
| Simulator | Reproducible experiments | Real agent code with `FakeWorld`/`FakeBackend` | `sim/world.py`, `simhooks.py` | Simulated | Not live data; attacks exist only here (plus an operator-initiated harmless tripwire) |
| Dashboard | Operator view | stdlib HTTP server, trust matrix | `dashboard/server.py`, `index.html` | Fully implemented | Two documented cosmetic issues (stale alerts after Reset; story box text) |
| Centralized baseline | Comparison | Single controller deployment | `k8s/baseline/` | Fully implemented (comparison only) | Not part of the proposed system |
| Machine learning | – | – | – | Not present | Detection is rule-based |
| PBFT / BFT protocol | – | – | – | Not present | Terminology only |
| Key rotation / revocation | Long-lived deployment | – | – | Not present | Proposed future work |
| Clinical deployment / PHI | – | Synthetic Synthea data only | `docs/RESULTS.md` | Not present | No claim of clinical use |

### Code-versus-documentation discrepancies
1. `README.md` ("BFT-style", "standard BFT (n = 3f+1)") and `quorum.py` docstring ("classic Byzantine quorum intersection") overstate; the code is threshold voting (see §1.6).
2. `certificate.py` docstring item 6 ("has not been consumed already (replay)") – the `consumed` argument is never passed by `_qc` or the webhook; replay protection of certificates rests on epoch/expiry only.
3. `docs/AUDIT.md` #8 says excluded agents' evidence still counts, down-weighted; `agent.py: on_envelope` rejects it entirely.
4. `trust.py` docstring says dashboard shows the median of all agents' views; the dashboard uses the median of the *other three*.
5. `config.py` comment "dashboard flags SUSPECT" – flags are computed by the agents.
6. `docs/AUDIT.md` "VERIFIED LIVE" labels refer to an earlier user-cluster run, not the sandbox cluster; they are not reproduced in this repository's recorded results.
7. Rejection of stale/replayed messages penalizes the delivering identity, so a skewed honest sender loses trust (limitation, not a bug in the stated design).
8. Earlier statement in chat that the repo contains no literature references was wrong: `README.md` ("Honest positioning") and `docs/INVESTIGATION.md` name Castro–Liskov PBFT, Dempster–Shafer-style evidence fusion and distributed-IDS trust frameworks. There is **no bibliography**; the paper's reference list must be built separately.

---------------------------------------------------------------------------------------------------


---------------------------------------------------------------------------------------------------

# ADDENDUM – follow-up verification (recovery retry, reintegration quorum, SUSPECT, validation)

| Point | Verified behaviour | Source |
|---|---|---|
| Recovery retry | A retry is itself a quorum action (`RETRY_RECOVERY`). Each agent votes for it when its own `_recovery_failure` is set: validation still failing more than `validate_timeout_s` (90 s) in VALIDATING, or the replacement not ready 90 s after the redeploy in RECOVERING. Backoff 15 s x 2^(n-1), cap 120 s. `max_retries = 3`, so at most 4 attempts; afterwards `_attention` (NEEDS_ATTENTION, workload stays quarantined). Separate mechanism: failed Kubernetes operations are retried by `_step` with 2-30 s backoff, up to `action_max_attempts = 10`, then abandoned and `_attention`. | `agent.py` `_recovery_failure`, `_vote_recovery`, `_on_commit` (RETRY_RECOVERY), `_make_recover`, `_step`, `_attention`; `config.py` `RecoveryParams`, `Timers.validate_timeout_s` |
| Reintegration quorum | VALIDATE is a quorum action and starts reintegration at QUARANTINE. Every later stage change is its own ADVANCE_STAGE quorum; an agent votes only if `can_advance` (dwell >= 10 s, workload trust >= threshold of the NEXT stage, own anomaly confidence < 0.3). FULL is not HEALTHY until `_track_cluster` sees the isolation policy gone. | `agent.py` `_vote`, `_on_commit`, `_track_cluster`; `reintegration.py` `can_advance`, `STAGE_THRESHOLDS` |
| SUSPECT (40 <= T < 50) | Flag event only (`_check_flags`, `attacker_flagged` metric). `eligible_voter` tests only `vote_min_trust` (40). Evidence accepted and weighted `conf*T/100`; the vote counts as one full vote. Below 40: evidence rejected in `on_envelope`, votes ignored in `VoteBook.new_commits`; re-evaluated every tick. | `agent.py` `eligible_voter`, `on_envelope`, `_check_flags`; `quorum.py` `weighted_score`, `new_commits` |
| Recovery validation | (1) telemetry reachable and `/health` returned HTTP 200; (2) `instance_id` differs from the instance recorded at containment; (3) only if a baseline manifest is loaded, reported file hashes equal it exactly; (4) own maximum anomaly confidence < `evidence_min_conf` (0.3). Identity is not authenticated; hashes are self-reported. | `agent.py` `validation_check`; `monitoring.py` `_one`; `config.py` hash-manifest loading |
| Evidence/isolation order | The forensic snapshot is started (`capture_async`) at commit, concurrently with isolation. The replacement waits for isolation confirmation and `forensics.ready` (snapshot done or `snapshot_timeout_s` = 15 s elapsed; capture then continues in the background). | `agent.py` `_on_commit`, `_make_recover`; `forensics.py` `ready` |
