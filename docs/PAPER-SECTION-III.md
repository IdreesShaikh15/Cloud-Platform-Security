# Paper Section III – PROPOSED SYSTEM: inspection, verification and draft

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

# PART 3 – FORMAL TEXT: III. PROPOSED SYSTEM

## III. PROPOSED SYSTEM

### A. System Overview
The proposed platform protects containerized healthcare workloads with a set of four identical, peer security agents that jointly decide whether a workload must be contained. No agent has authority over another and there is no central controller. Each agent observes the protected workloads, converts observations into signed evidence, exchanges that evidence with its peers, and independently evaluates it using a locally maintained trust view of every peer. When at least three distinct agents independently vote for the same containment action, the action is committed, a quorum certificate carrying their signatures is assembled, and one agent, selected by a deterministic rank, executes the action. The workload is then recovered from a known-good image and re-admitted through staged reintegration (Fig. 1, Fig. 2).

Four agents (A–D) form the decision group; six workloads (A–F) are protected, two of which (E, F) have no co-located agent and are observed by all four. The design target is tolerance of a single misbehaving agent (f = 1 of n = 4); it does not tolerate a majority of compromised agents. The evaluated prototype is described in Section III-G and in Section IV; the platform is a research prototype validated with a simulator and a four-node sandbox cluster using synthetic data, not a clinically deployed product.

### B. Distributed Security Agent Architecture
Each agent is an instance of the same program, deployed as one pod per node. Its functional modules are: (i) a *monitor* that polls workload telemetry; (ii) a *detector* that maps telemetry to per-signal confidences; (iii) an *evidence layer* that signs, verifies and de-duplicates messages; (iv) a *peer-communication layer* using gRPC over mutual TLS; (v) a *trust ledger*; (vi) a *quorum engine* (weighted scoring, voting and vote book); (vii) a *response executor* (containment, forensic snapshot, recovery); (viii) a *reintegration controller*; and (ix) a tamper-evident *decision log*. A deterministic tick drives all modules. Because all agents run identical code and exchange only signed messages, the design avoids a single point of control; the response executor is selected per incident by rank, with later-ranked agents acting only if earlier ones have not (a 3 s stagger per rank), and actions are idempotent.

### C. Secure Evidence Exchange and Validation
Evidence is produced from four signal families – network, process, file integrity and authentication – by threshold rules. For a measurement x exceeding its threshold θ, the confidence is
`c = 0.5 + 0.5·min(1, (x − θ)/s)` (1)
and c = 0 otherwise, where s is the scale parameter of the signal. Process and file-integrity signals take fixed confidences from the rule that matched.

Each Evidence, Vote and certificate share is serialized with Protocol Buffers and signed with Ed25519 over `kind || 0x00 || payload`, which binds the signature to the message type. Messages travel over gRPC channels secured by mutual TLS with ECDSA P-256 certificates. A received envelope is accepted only if, in order: the mTLS identity equals the claimed signer; the signer is registered; the signature verifies; the payload parses and its author equals the signer; its timestamp is within a 15 s skew; and it is not in the 120 s replay cache. Any rejection reduces the delivering peer's trust by 20 points. Telemetry from the workload to its observing agent is self-reported over an unauthenticated in-cluster channel; the design therefore relies on cross-observation by several agents rather than on trusting a single report.

### D. Trust Management and Agent Reliability Assessment
Every agent keeps its own trust score T ∈ [0,100] for each peer, initialised to 100, and updated linearly:
`T(t+Δt) = clamp(T(t) + r·Δt, 0, 100)` (2)
For a peer, r = −2.5 s⁻¹ while its claims are contradicted by the local observation (claim confidence ≥ 0.5, claim age ≥ 8 s, and the local maximum confidence for the same target < 0.2), and r = +0.5 s⁻¹ otherwise. A rejected message subtracts 20. Table III lists the resulting classes: a peer whose score is below 50 is flagged as suspect; below 40 its votes are not counted and its evidence is discarded.

Evidence from different peers is combined by a trust-weighted score. For a target and signal type k with sender set S_k, each sender contributes its maximum confidence c_s weighted by the receiver's trust in it:
`w_s = c_s · T_s / 100` (3)
`score_k = mean_{s∈S_k}(w_s) · min(1, |S_k|/2)` (4)
`W = max_k score_k + 0.15 · max(0, |C| − 1)` (5)
where C is the set of signal types reported by at least two distinct senders. An agent votes to contain when its own maximum local confidence is at least 0.5 and W ≥ 0.6; an additional path, enabled only after a targeted investigation authorizes it, accepts local confidence ≥ 0.3 and W ≥ 0.3. The constants (0.5, 0.6, 0.15, 2.5, 0.5, 20, 40, 50) are engineering choices of the prototype and have not been derived analytically or tuned on field data.

Workload trust uses the same form (2): it decreases at 10 s⁻¹ while the workload is anomalous, recovers at 2 s⁻¹ otherwise, and is set to zero while the workload is isolated, recovering or being validated.

### E. Quorum-Based Collaborative Decision-Making
The system uses n = 4 agents and a quorum q = 2f+1 = 3 with f = 1. A decision is identified by `ACTION:target:epoch:stage`. The vote book commits an action when at least three distinct, eligible agents (trust ≥ 40) have submitted a signed vote for it. Agents never cast negative votes; absence of a vote means no support. With two or fewer available agents no action can commit and the workload stays as it is (fail-safe in the sense of not acting). With a single misbehaving agent, the remaining three honest agents can still reach quorum and the misbehaving agent cannot reach it alone. If three or more agents are compromised, they can reach quorum and produce a valid certificate; the design makes no claim in that case.

The mechanism is an application-level threshold-voting scheme over signed messages. It is *not* PBFT: there is no primary, no view change, no pre-prepare/prepare/commit exchange and no ordered replicated log, and the paper makes no formal Byzantine-fault-tolerance claim. The choice n = 3f+1, q = 2f+1 is used so that any two quorums intersect in at least one honest agent when f = 1.

On commit, the agents sign a *quorum certificate* over a statement containing the workload, target, action, stage, epoch, evidence digest and an expiry (default TTL 300 s, maximum 900 s). A certificate is valid with at least three distinct valid signatures. A Kubernetes validating admission webhook accepts the isolation and recovery operations of the agent service account only when a valid certificate accompanies them.

### F. Automated Workload Containment and Recovery
After commit the executor (i) takes a forensic snapshot, (ii) applies a per-workload NetworkPolicy isolating the target (`resilience-isolate-<workload>`), (iii) replaces the workload from the known-good image using a Recreate strategy, retrying at most three times with backoff before raising a NEEDS_ATTENTION state, and (iv) hands the workload to a validation phase. The workload then moves through five reintegration stages (QUARANTINE, RESTRICTED, MONITORED, PEER_VALIDATED, FULL). Advancing from a stage requires a minimum dwell of 10 s, workload trust at least 0/20/40/60/80 for the successive stages, a local anomaly score below 0.3, and the validation checks to pass. Each stage applies a progressively less restrictive NetworkPolicy; during MONITORED and the next watch stage detection sensitivity is reduced by factors 0.7 and 0.85 respectively. Targeted investigation can classify a suspected workload as CORROBORATED, FALSE_POSITIVE, AMBIGUOUS or UNCERTAIN and gate the authorization of a lower-threshold vote. Every decision is appended to a hash-chained log. The recovery path (Fig. 2) follows the order of the implemented pipeline; network enforcement depends on a NetworkPolicy-capable CNI (Calico in the evaluated cluster).

### G. Prototype Architecture and Implementation
The prototype is written in Python 3.11 with gRPC 1.84, Protocol Buffers 7.36, `cryptography` 50.0 (Ed25519, ECDSA P-256) and the official Kubernetes client 36.0; the dashboard uses the Python standard library. The same agent code runs in two environments: (a) a *simulator* that substitutes a synthetic world for telemetry and a fake backend for Kubernetes while using real signatures, real gRPC/mTLS and the real admission logic; and (b) a four-node Minikube cluster with Calico, a validating webhook and least-privilege RBAC. The protected workloads are six containerized services (A–F) whose data are synthetic records generated with Synthea; no real patient data are used. Results obtained in the simulator, in the sandbox cluster and (when available) on the authors' own cluster must be reported separately. Only the sandbox cluster has recorded results in the repository; the simulator results are not live measurements.

### H. System Requirements
Table I lists the functional and non-functional requirements that the design addresses. Operating assumptions: the PKI and public keys are distributed before start-up; at most one agent is faulty; node clocks agree within 15 s; the Kubernetes API server and CNI behave correctly; the known-good image and the file-integrity manifest are trusted.

---------------------------------------------------------------------------------------------------

# PART 4 – FIGURES AND TABLES

Placement: Fig. 1 after III-A (first paragraph references it); Fig. 2 after III-F; Table I after III-H; Table II after III-B; Table III after III-D; Table IV after III-G.

## Fig. 1 – Overall architecture
**Caption:** "Fig. 1. Architecture of the proposed distributed cyber-resilience platform: four peer agents exchange signed evidence and votes over mutual-TLS gRPC, reach a 3-of-4 quorum, and enforce containment through certificate-checked Kubernetes operations."
**Explanation:** shows protected workloads A–F, agents A–D, the signed-message mesh, the quorum/certificate step, the admission webhook and the Kubernetes API with Calico. Referenced in III-A.

## Fig. 2 – Containment and recovery workflow
**Caption:** "Fig. 2. Containment and staged recovery workflow of a workload, from evidence to full reintegration."
**Order (follows the source):** 1 Telemetry polled → 2 Detection (confidence) → 3 Signed evidence broadcast → 4 Peer trust update → 5 Weighted score W → 6 Vote CONTAIN (local ≥ 0.5, W ≥ 0.6) → 7 ≥ 3 distinct votes? (no → keep observing) → 8 Quorum certificate (≥ 3 signatures) → 9 Executor by rank → 10 Admission webhook verifies certificate → 11 Forensic snapshot + NetworkPolicy isolation (ISOLATED) → 12 Replace from known-good image (RECOVERING) → 13 Validation (VALIDATING) → 14 Reintegration stages QUARANTINE→RESTRICTED→MONITORED→PEER_VALIDATED→FULL (REINTEGRATING) → 15 HEALTHY. Failure path: recovery retried ≤ 3 times → NEEDS_ATTENTION. Referenced in III-F.

## Table I – Requirements
| ID | Requirement | Addressed by | Status |
|---|---|---|---|
| FR1 | Detect anomalous workload behaviour | Threshold detector, four signals | Implemented |
| FR2 | Authenticate and integrity-protect agent messages | Ed25519 + mTLS | Implemented |
| FR3 | Weigh evidence by source reliability | Trust ledger, weighted score | Implemented |
| FR4 | Require agreement of ≥ 3 of 4 agents before containment | VoteBook | Implemented |
| FR5 | Contain a workload without a central controller | Executor rank + NetworkPolicy | Implemented (sandbox) |
| FR6 | Recover from known-good state and re-admit gradually | Recovery + 5-stage reintegration | Implemented (sandbox) |
| FR7 | Prove decisions to the cluster | Quorum certificate + webhook | Partial |
| FR8 | Keep an auditable record | Hash-chained decision log | Partial |
| NFR1 | Tolerate one faulty agent (f = 1) | q = 2f+1 = 3 | Implemented |
| NFR2 | Fail safe when fewer than 3 agents are available | No commit below quorum | Implemented |
| NFR3 | Least privilege | RBAC, webhook scoped to agent SA | Implemented |
| NFR4 | No real patient data | Synthea synthetic data | Implemented |

## Table II – Components and responsibilities
| Component | Function | Inputs | Processing | Outputs | Technologies |
|---|---|---|---|---|---|
| Monitor | Collect telemetry | Workload `/health`, `/_telemetry` | HTTP poll each tick | Snapshot | Python, HTTP |
| Detector | Score anomalies | Snapshot, baseline manifest | Threshold ramp, SHA-256 check | Evidence | Python |
| Evidence layer | Sign/verify | Evidence, Vote, Share | Ed25519, skew, replay checks | SignedEnvelope | protobuf, cryptography |
| Peer layer | Exchange messages | Envelopes | gRPC mTLS broadcast | Received envelopes | gRPC, ECDSA P-256 |
| Trust ledger | Peer/workload trust | Contradictions, rejections | Linear update | Trust scores | Python |
| Quorum engine | Decide | Evidence, trust | Weighted score, vote book | Commit | Python |
| Certificate manager | Prove decision | Votes/shares | ≥ 3 Ed25519 signatures | Quorum certificate | Python |
| Admission webhook | Enforce proof | AdmissionReview | Verify certificate | Allow/deny | K8s admission |
| Response executor | Act | Commit | NetworkPolicy, snapshot, replace | Phase change | K8s client |
| Reintegration controller | Restore | Trust, anomaly, validation | Stage thresholds + dwell | Stage/NetworkPolicy | Python, Calico |
| Decision log | Audit | Events | Hash chain | Log | Python |

## Table III – Agent trust classification
| Class | Trust range | Behaviour in code |
|---|---|---|
| Trusted | 50 – 100 | Evidence and votes counted, weight = c·T/100 |
| Suspect | 40 – < 50 | Flag and event raised; evidence and votes still counted with reduced weight |
| Vote-excluded | < 40 | Votes not counted; evidence rejected on receipt |
| Transitions | – | Contradicted claim −2.5/s (sim −5); otherwise +0.5/s; rejected message −20 |

## Table IV – Proposed system configuration (prototype defaults)
| Parameter | Value |
|---|---|
| Agents n / faults f / quorum q | 4 / 1 / 3 |
| Tick (cluster / simulator) | 1 s / 0.5 s |
| Telemetry timeout | 1.5 s |
| gRPC timeout / keepalive | 2 s / 10 s |
| Vote re-send / share re-send | 4 s / 2 s |
| Message skew / replay TTL | 15 s / 120 s |
| Vote threshold: local / W | 0.5 / 0.6 (investigation path 0.3 / 0.3) |
| Corroboration bonus | 0.15 per extra type |
| Peer trust decay / recover / reject | 2.5 / 0.5 per s / 20 |
| Suspect / exclude thresholds | 50 / 40 |
| Contradiction grace | 8 s |
| Workload trust decay / recover | 10 / 2 per s |
| Stage thresholds / dwell | 0, 20, 40, 60, 80 / 10 s |
| Certificate TTL / max | 300 s / 900 s |
| Recovery retries | 3 |
| Executor stagger | 3 s per rank |

---------------------------------------------------------------------------------------------------

# PART 5 – IMAGE-GENERATION PROMPTS AND EDITABLE TABLES

Figures should be generated as vector/clean diagrams. Tables must remain text (Part 6 LaTeX); do not rasterize them.

## Prompt – Fig. 1
```
Create a clean, flat, IEEE-journal-style system architecture diagram on a white background, landscape, aspect ratio 16:9 (3200x1800 px, 300 dpi), sans-serif font (Helvetica/Arial), black text, thin 1.5 pt lines, grayscale plus one accent blue (#1f4e79) and one accent orange (#c55a11). No gradients, no 3D, no icons beyond simple boxes, no decorative clutter.
Layout, top to bottom, three horizontal bands with labels at the left edge:
BAND 1 "Protected healthcare workloads": six equal rounded boxes in a row labelled "Workload A" ... "Workload F". Under E and F add the small note "no co-located agent".
BAND 2 "Distributed security agents (peer-to-peer, no leader)": four identical boxes "Agent A" "Agent B" "Agent C" "Agent D". Inside each box three stacked small labels: "Monitor + Detector", "Trust ledger", "Quorum engine". Draw a fully connected mesh of thin double-headed blue arrows between the four agents labelled once, centered: "Signed evidence / votes / shares — Ed25519 over gRPC + mTLS".
BAND 3 "Kubernetes control plane": left box "Admission webhook (verifies quorum certificate)", right box "API server + Calico NetworkPolicy".
Arrows: single-headed gray arrows from each workload up to its observing agents labelled "telemetry (self-reported)" (draw A->Agent A, B->Agent B, C->Agent C, D->Agent D, and E and F with dashed arrows to all four agents). From the agent mesh a bold orange arrow down labelled "3-of-4 quorum → quorum certificate (>=3 signatures)" to the webhook, then a bold orange arrow from the webhook to the API server, then an orange dashed arrow from the API server back up to the affected workload labelled "isolate / recover from known-good image".
Add a small legend bottom-right: blue = signed peer messages, gray = telemetry, orange = enforced action. Title text inside the image is not needed. Keep at least 40 px padding between boxes, labels no smaller than 24 px at full size, no overlapping arrows.
```

## Prompt – Fig. 2
```
Create a clean IEEE-style flowchart on white background, portrait aspect ratio 3:4 (2400x3200 px, 300 dpi), Helvetica/Arial, black text, 1.5 pt lines, one accent blue (#1f4e79) for process boxes, one accent orange (#c55a11) for the failure path, light gray for phase labels. No clutter, no icons, no shadows.
Vertical flow, rectangular process boxes, one diamond decision, arrows pointing downward unless stated. Boxes in this exact order with these exact labels:
1 "Poll workload telemetry"
2 "Detect: confidence c per signal"
3 "Sign and broadcast Evidence (Ed25519, mTLS)"
4 "Update peer trust (contradiction check)"
5 "Weighted score W"
6 "Vote CONTAIN (local c >= 0.5 and W >= 0.6)"
Diamond 7 "At least 3 distinct eligible votes?" — "No" arrow loops right and back up to box 1 labelled "keep observing"; "Yes" continues down.
8 "Quorum certificate (>= 3 signatures)"
9 "Executor selected by rank (3 s stagger)"
10 "Admission webhook verifies certificate"
11 "Forensic snapshot + NetworkPolicy isolation"
12 "Replace from known-good image"
13 "Validation checks"
14 "Staged reintegration: QUARANTINE > RESTRICTED > MONITORED > PEER_VALIDATED > FULL"
15 "HEALTHY"
Add a gray bracket on the left grouping boxes 1-6 as "Detection and evidence", 7-8 as "Quorum decision", 9-12 as "Containment and recovery", 13-15 as "Validation and reintegration". Add phase tags to the right of boxes 11-15: "ISOLATED", "RECOVERING", "VALIDATING", "REINTEGRATING", "HEALTHY". Add an orange dashed arrow from box 12 to a side box "Retry (max 3, backoff) then NEEDS_ATTENTION". Keep text >= 26 px, no overlapping lines.
```

## Table layout-preview prompt (optional, preview only)
```
Draw a preview of an IEEE two-column journal table: thin horizontal rules (top, below header, bottom) only, bold header row, 8 pt serif body text (Times), small-caps title above the table in the form "TABLE I  REQUIREMENTS OF THE PROPOSED SYSTEM", white background, no vertical lines, no colours. This image is only a visual preview; the real table is typeset in LaTeX.
```

Markdown content of Tables I–IV is in Part 4; the editable LaTeX is in Part 6.

---------------------------------------------------------------------------------------------------

# PART 6 – IEEE LaTeX INTEGRATION

Packages (preamble): `\usepackage{graphicx}`, `\usepackage{booktabs}`, `\usepackage{array}`, `\usepackage{amsmath}`, `\usepackage{tabularx}`, `\usepackage{cite}`. Document class: `\documentclass[journal]{IEEEtran}`.

## Figures
Save generated images as `fig1_architecture.pdf` (or `.png`) and `fig2_workflow.pdf`.
```latex
\begin{figure*}[!t]
  \centering
  \includegraphics[width=0.9\textwidth]{fig1_architecture}
  \caption{Architecture of the proposed distributed cyber-resilience platform: four peer agents exchange signed evidence and votes over mutual-TLS gRPC, reach a 3-of-4 quorum, and enforce containment through certificate-checked Kubernetes operations.}
  \label{fig:architecture}
\end{figure*}

\begin{figure}[!t]
  \centering
  \includegraphics[width=\columnwidth]{fig2_workflow}
  \caption{Containment and staged recovery workflow of a workload, from evidence to full reintegration.}
  \label{fig:workflow}
\end{figure}
```
In text: "…as shown in Fig.~\ref{fig:architecture}" (III-A) and "Fig.~\ref{fig:workflow}" (III-F).

## Equations
```latex
\begin{equation}
c = 0.5 + 0.5\min\!\left(1,\frac{x-\theta}{s}\right),\quad x>\theta \label{eq:conf}
\end{equation}
\begin{equation}
T(t+\Delta t)=\min\{100,\max\{0,\;T(t)+r\,\Delta t\}\} \label{eq:trust}
\end{equation}
\begin{equation}
w_s=\frac{c_s T_s}{100},\qquad
\mathrm{score}_k=\Big(\tfrac{1}{|S_k|}\sum_{s\in S_k} w_s\Big)\min\!\big(1,\tfrac{|S_k|}{2}\big) \label{eq:score}
\end{equation}
\begin{equation}
W=\max_k \mathrm{score}_k + 0.15\,\max(0,|C|-1) \label{eq:W}
\end{equation}
```

## Table I
```latex
\begin{table}[!t]
\caption{Requirements of the Proposed System}
\label{tab:requirements}
\centering
\small
\begin{tabularx}{\columnwidth}{@{}l X l@{}}
\toprule
ID & Requirement & Status \\
\midrule
FR1 & Detect anomalous workload behaviour & Implemented \\
FR2 & Authenticate and integrity-protect agent messages & Implemented \\
FR3 & Weigh evidence by source reliability & Implemented \\
FR4 & Require $\geq 3$ of 4 agents before containment & Implemented \\
FR5 & Contain a workload without a central controller & Implemented (sandbox) \\
FR6 & Recover from known-good state, re-admit gradually & Implemented (sandbox) \\
FR7 & Prove decisions to the cluster (certificate, webhook) & Partial \\
FR8 & Auditable record (hash-chained log) & Partial \\
NFR1 & Tolerate one faulty agent ($f=1$) & Implemented \\
NFR2 & No action when fewer than 3 agents available & Implemented \\
NFR3 & Least privilege (RBAC, scoped webhook) & Implemented \\
NFR4 & No real patient data (synthetic) & Implemented \\
\bottomrule
\end{tabularx}
\end{table}
```

## Table II
```latex
\begin{table*}[!t]
\caption{Components and Responsibilities}
\label{tab:components}
\centering
\small
\begin{tabularx}{\textwidth}{@{}l l l l l l@{}}
\toprule
Component & Function & Inputs & Processing & Outputs & Technologies \\
\midrule
Monitor & Collect telemetry & \texttt{/health}, \texttt{/\_telemetry} & HTTP poll per tick & Snapshot & Python, HTTP \\
Detector & Score anomalies & Snapshot, manifest & Threshold ramp, SHA-256 & Evidence & Python \\
Evidence layer & Sign/verify & Evidence, Vote, Share & Ed25519, skew, replay & SignedEnvelope & protobuf, cryptography \\
Peer layer & Exchange messages & Envelopes & gRPC mTLS broadcast & Received envelopes & gRPC, ECDSA P-256 \\
Trust ledger & Peer/workload trust & Contradictions, rejections & Linear update & Trust scores & Python \\
Quorum engine & Decide & Evidence, trust & Weighted score, vote book & Commit & Python \\
Certificate manager & Prove decision & Votes, shares & $\geq 3$ Ed25519 signatures & Certificate & Python \\
Admission webhook & Enforce proof & AdmissionReview & Verify certificate & Allow/deny & K8s admission \\
Response executor & Act & Commit & NetworkPolicy, snapshot, replace & Phase change & Kubernetes client \\
Reintegration ctrl. & Restore & Trust, anomaly, checks & Stage thresholds, dwell & Stage, policy & Python, Calico \\
Decision log & Audit & Events & Hash chain & Log & Python \\
\bottomrule
\end{tabularx}
\end{table*}
```

## Table III
```latex
\begin{table}[!t]
\caption{Agent Trust Classification}
\label{tab:trustclasses}
\centering
\small
\begin{tabularx}{\columnwidth}{@{}l l X@{}}
\toprule
Class & Trust $T$ & Behaviour \\
\midrule
Trusted & $50\le T\le100$ & Evidence and votes counted, $w=cT/100$ \\
Suspect & $40\le T<50$ & Flag and event raised; still counted \\
Vote-excluded & $T<40$ & Votes not counted; evidence rejected \\
\bottomrule
\end{tabularx}
\\[2pt]
{\footnotesize Contradicted claim: $-2.5$\,s$^{-1}$ (simulator $-5$); otherwise $+0.5$\,s$^{-1}$; rejected message: $-20$.}
\end{table}
```

## Table IV
```latex
\begin{table}[!t]
\caption{Prototype Configuration (Defaults)}
\label{tab:config}
\centering
\small
\begin{tabularx}{\columnwidth}{@{}X l@{}}
\toprule
Parameter & Value \\
\midrule
Agents $n$ / faults $f$ / quorum $q$ & 4 / 1 / 3 \\
Tick (cluster / simulator) & 1\,s / 0.5\,s \\
gRPC timeout / keepalive & 2\,s / 10\,s \\
Vote / share re-send & 4\,s / 2\,s \\
Skew / replay TTL & 15\,s / 120\,s \\
Vote thresholds (local / $W$) & 0.5 / 0.6 \\
Corroboration bonus & 0.15 \\
Peer trust decay / recover / reject & 2.5 / 0.5 per s / 20 \\
Suspect / exclude thresholds & 50 / 40 \\
Workload trust decay / recover & 10 / 2 per s \\
Stage thresholds / dwell & 0,20,40,60,80 / 10\,s \\
Certificate TTL / max & 300\,s / 900\,s \\
Recovery retries & 3 \\
\bottomrule
\end{tabularx}
\end{table}
```
Placement: Table I after Section III-H text, Table II (`table*`) at the top of the page after III-B, Table III after III-D, Table IV after III-G. Reference in text with `Table~\ref{tab:...}`.

---------------------------------------------------------------------------------------------------

# PART 7 – FINAL CONSISTENCY AUDIT

| Check | Result |
|---|---|
| Section agrees with code | Yes, for every numeric constant and formula quoted (source functions named in Part 1). Line-level references were not re-verified after editing; exact default values should be re-checked against `config.py` before submission. |
| Suspect vs vote-excluded behaviour | In Table III "Suspect (40–<50) still counted" follows the code summary: flag at < 50, vote exclusion at < 40. Re-check `agent.py: eligible_voter` before submission **(unverified wording of "still counted with reduced weight": the weight is c·T/100, so reduced by construction)**. |
| Figures/tables match text | Fig. 2 order follows `Agent.tick`/`_step`; Table IV equals Part 1 values; Table III equals §1.5. |
| Equations match implemented logic | Eq. (1)–(5) correspond to `detection.py` and `quorum.py`; Eq. (2) is `linear_step`. Process/file signals use fixed values, not Eq. (1). |
| Unsupported claims | None intentionally. No ML, no PBFT, no formal BFT guarantee, no clinical deployment, no claim of defence against ≥ 3 compromised agents, no claim against a compromised Kubernetes control plane. |
| Simulation vs live | Simulator results are labelled as simulated; sandbox-cluster results are separate; no results exist for the user's own cluster. |
| Terminology | "Agent" = A–D quorum party; "workload" = A–F protected asset; "trust" = per-observer view; "quorum" = ≥ 3 distinct votes. Avoid "BFT consensus" in the paper. |
| Known caveats the paper should state | Self-reported telemetry; hand-set parameters; clock-skew sensitivity; no key rotation; webhook scoped to the agent service account; certificate replay-consumption not wired; documentation contains stale "BFT" wording (fix `README.md` and `quorum.py` docstring before citing them). |
| References | The repo has **no bibliography**. Candidate works to cite (to be verified by the authors, not taken from the repo): Castro–Liskov PBFT (OSDI 1999) for the contrast; Dempster–Shafer evidence theory; surveys of trust management in distributed IDS. |
