# III. PROPOSED SYSTEM – corrected paper text (source of truth for the paper)

Verified against the source code (see the addendum in `docs/PAPER-SECTION-III.md` for file/function references).
Figures: `docs/paper-figures/fig1_architecture.{svg,pdf,png}`, `fig2_workflow.{svg,pdf,png}` (generator `make_figures.py`, renderer `render.js`).
Items marked *(unverified)* could not be confirmed from the code.

---------------------------------------------------------------------------------------------------

## A. System Overview

The proposed system introduces a distributed cyber-resilience architecture designed to protect containerized healthcare applications against malicious activity, compromised monitoring agents, and security-related service disruptions. Unlike conventional intrusion detection architectures that rely on a centralized monitoring or decision-making entity, the proposed system distributes security monitoring, evidence validation, and response authorization across multiple independent security agents. Protective decisions are therefore based on corroborated observations rather than on the report of a single monitoring component.

As illustrated in Fig. 1, the architecture consists of four distributed security agents responsible for monitoring six logical healthcare application workloads. Each agent independently observes workload-related security events and generates structured evidence when suspicious behaviour is identified. The agents exchange authenticated evidence through gRPC channels secured with mutual Transport Layer Security (mTLS). Each evidence message is digitally signed with the Ed25519 algorithm to support message-origin authentication and integrity verification.

Upon receiving evidence, an agent verifies the sender's identity and digital signature before evaluating the reliability of the reporting agent. An observer-specific trust-management mechanism maintains an independent trust assessment of every other agent. Validated evidence is then processed by a trust-weighted correlation mechanism, followed by a three-out-of-four application-level authorization quorum.

When the required quorum is achieved, the system authorizes the corresponding protective action. The response workflow (Fig. 2) comprises evidence preservation and workload isolation, replacement from a configured known-good container image, recovery validation, and staged reintegration; every state-changing step of this workflow is itself authorized by a separate quorum. The architecture thereby combines distributed detection with coordinated containment and recovery rather than limiting itself to intrusion identification.

The architecture is implemented using Python-based security agents, authenticated inter-agent communication, and Kubernetes-oriented workload management. Its evaluation uses a simulation that runs the implemented agent logic and a four-node Minikube environment configured with Calico for network-policy enforcement.

## B. Distributed Security Agent Architecture

The system comprises four logically independent security agents, A, B, C and D, which collectively monitor six logical healthcare application workloads, A–F. Each agent maintains its own monitoring state, evidence collection, peer-trust information and decision state.

Four workloads (A–D) have a co-located agent; the remaining two (E and F) are monitored by all four agents. Observations about every workload are therefore available from several agents even when a workload has no dedicated agent. *Agents* (A–D) are the quorum parties; *workloads* (A–F) are the protected assets.

Each agent performs the following functions: (1) workload monitoring of network activity, running processes, file-integrity information and authentication events; (2) rule-based anomaly detection with configured thresholds; (3) evidence generation (observer, target workload, signal category, confidence, timestamp); (4) evidence authentication with the agent's Ed25519 private key; (5) collaborative communication of evidence, votes and certificate shares over gRPC with mTLS; (6) trust management, i.e. observer-specific trust scores for the other agents; and (7) response coordination, i.e. participation in quorum authorization and execution of containment and recovery operations.

The agents do not depend on a permanent leader for security decisions. Each agent maintains its own view of peer reliability and processes received evidence independently; different agents may therefore temporarily hold different trust assessments or evidence states. The collective decision capability nevertheless depends on the availability and reliability of the participating agents: the configured quorum requires three eligible agents.

## C. Workload Monitoring and Evidence Generation

Detection is rule-based; no machine-learning component is used. The detector evaluates four categories of security signals: network activity, process behaviour, file integrity and authentication activity. Each agent periodically retrieves workload telemetry and evaluates it against configured conditions; a satisfied condition produces an evidence report. Telemetry is reported by the workload itself (from `/proc`-derived counters over an unauthenticated in-cluster HTTP endpoint); the design relies on cross-observation by several agents rather than on trusting a single report, but a fully compromised workload could misreport its own telemetry.

1) *Network activity.* Outbound connection counts and transmission rates are evaluated. An observation is suspicious when the number of outbound connections exceeds eight or the transmission rate exceeds 256 000 B/s; higher values increase the confidence assigned.

2) *Process behaviour.* Running processes are compared with configured suspicious names and directories and with an allowlist. A suspicious indicator yields a higher confidence; a process absent from the allowlist yields a lower confidence.

3) *File integrity.* Observed file information is compared with a known-good SHA-256 baseline manifest; modified, missing and newly detected files receive different configured confidences.

4) *Authentication.* Failed authentication events associated with the target workload are evaluated; a condition is raised when more than ten failures occur within ten seconds.

For threshold-based signals the confidence is

c(x) = 0 for x ≤ θ;  c(x) = 0.5 + 0.5·min(1, (x − θ)/s) for x > θ,   (1)

where x is the observed metric, θ the detection threshold and s the confidence-scaling parameter. The confidence is capped at 1.0. Process and file-integrity signals use configured confidence values instead of (1). Each evidence report contains the reporting agent, target workload, signal category, confidence and observation timestamp, and is subsequently authenticated and sent to the other agents.

**Table I** lists the implemented detection conditions. The thresholds and confidence values are configurable engineering parameters; they are not claimed to be statistically optimized or validated against real-world healthcare attack traces.

| Signal category | Monitoring condition | Configured detection parameter |
|---|---|---|
| Network | Outbound connection count | Threshold: 8 connections |
| Network | Outbound transmission rate | Threshold: 256,000 B/s |
| Process | Suspicious process name or directory | Confidence: 0.95 |
| Process | Process not present in allowlist | Confidence: 0.60 |
| File integrity | Modified file | Confidence: 0.95 |
| File integrity | Missing file | Confidence: 0.80 |
| File integrity | Newly detected file | Confidence: 0.70 |
| Authentication | Failed authentication attempts | More than 10 attempts within 10 s |

## D. Secure Evidence Exchange and Trust Management

### 1) Secure evidence exchange
When an agent identifies suspicious activity it generates an evidence report, signs it with its Ed25519 private key, encapsulates it in a structured envelope and sends it over gRPC. All inter-agent communication is protected by mTLS; the X.509 certificate identity is mapped to the agent identifier.

A receiving agent performs, in this order: (1) peer authentication, i.e. the mTLS identity must equal the claimed signer; (2) signature verification with the signer's configured public key; (3) message validation of type, payload structure and consistency between the claimed author and the authenticated sender; (4) timestamp validation against a 15-second clock-skew tolerance; (5) replay detection with a 120-second replay cache; and (6) for evidence, an eligibility check of the sender's trust (Section III-D-2) before the evidence enters collective evaluation.

The signature is computed over a domain-separated representation (message type, separator, serialized payload), which prevents a signed payload from being interpreted as a different message category. Messages that fail validation are rejected and the delivering peer receives a one-time trust penalty of 20 points. A valid signature establishes origin and integrity; it does not establish that the reported observation is accurate.

### 2) Trust management (agent trust)
Each agent independently maintains an *agent trust* score, from 0 to 100, for each of the other agents (initial value 100). The score evolves as

T(t + Δt) = clamp(T(t) + r·Δt, 0, 100),   (2)

where r is the adjustment rate. A peer's claim counts as *contradicted* when its confidence is at least 0.5, the claim is at least 8 s old, and the receiving agent's own maximum confidence for the same target since shortly before the claim (2 s) is below 0.2. While a peer's claims are contradicted, r = −2.5 per second; otherwise r = +0.5 per second. A rejected message subtracts 20 once.

Trust thresholds and their exact effect in the implementation:
- **Trust below 50:** the peer is flagged SUSPECT. This raises a flag event; it does not change how the peer's messages are processed.
- **Trust between 40 and 50:** the peer is *not* excluded. Its evidence is accepted and weighted by its current trust score (Eq. 3), and its vote counts as one full eligible vote. SUSPECT status itself does not reduce voting weight.
- **Trust below 40:** the peer is vote-excluded. Its evidence is rejected on receipt and its votes are not counted.
- Eligibility is re-evaluated against the current trust values at every cycle, so an agent whose trust recovers becomes eligible again, and an agent whose trust falls loses eligibility, including for votes already received.

Agent trust is distinct from *workload trust* (Section III-F), which measures the health of a protected workload and gates its reintegration. Trust values are maintained independently by each observer; the system does not assume identical or globally synchronized trust assessments.

## E. Trust-Weighted Evidence Correlation and Quorum-Based Decision-Making

### 1) Trust-weighted evidence correlation
Each receiving agent evaluates the available reports for a target workload independently. For each signal category k, the maximum confidence reported by each eligible sender is taken and weighted by the receiver's current trust in that sender (an agent's own observations carry trust 100):

w_{i,k} = c_{i,k} · T_i / 100,   (3)

S_k = mean_i(w_{i,k}) · min(1, |S*_k| / 2),   (4)

W = max_k S_k + 0.15 · max(0, |C| − 1),   (5)

where S*_k is the set of distinct eligible senders for category k (the factor 2 equals f + 1 for f = 1) and C is the set of categories reported by at least two distinct senders. Eq. (3) applies to every peer according to its actual trust score; it is not specific to the SUSPECT band.

An agent casts a CONTAIN vote when its own local maximum confidence is at least 0.5 and W ≥ 0.6. A second path, available only after a targeted investigation has authorized it, accepts a local confidence of at least 0.3 and W ≥ 0.3. For network- and authentication-only anomalies an agent additionally waits until the anomaly has persisted for a configured re-check period before voting *(period value unverified here)*. The thresholds and weighting constants are implementation-level choices and are not presented as optimal.

### 2) Quorum-based authorization
The architecture comprises N = 4 agents and requires at least three distinct eligible agents to vote for the same action:

Q = Σ_{i=1..N} V_i ≥ 3,   (6)

where V_i = 1 when agent i provides an eligible vote for that action and 0 otherwise. Votes are keyed by an action identifier containing the action type, target workload, epoch and (where applicable) stage or attempt number, so votes for different actions cannot be combined. The same rule authorizes all four state-changing actions: CONTAIN, VALIDATE, RETRY_RECOVERY and ADVANCE_STAGE. Once three eligible agents have voted for the same action, an authorization certificate carrying their Ed25519 signatures can be assembled and the operation can proceed.

The mechanism prevents a single agent from authorizing a disruptive operation and withholds action when fewer than three eligible agents are available. It is, however, an application-level threshold authorization scheme and not a formal Byzantine Fault Tolerant consensus protocol: there is no leader, view change, ordered replicated log or PBFT message phase. It cannot guarantee correctness when three or more agents collude or provide coordinated false evidence.

## F. Automated Workload Containment and Recovery

Following authorization, the system executes the workflow of Fig. 2. Its objective is to restrict the suspicious workload while preserving evidence, to restore it from a known-good baseline, and to re-admit it gradually.

**1) Evidence preservation and isolation.** When the CONTAIN quorum commits, the executing agent starts a forensic snapshot of the workload and applies the isolation NetworkPolicy concurrently. The replacement is not started until the isolation is confirmed in the cluster and the snapshot has completed. The snapshot wait is bounded by a 15-second timeout; after it recovery may proceed while the capture continues in the background, so completeness of the snapshot is not guaranteed.

**2) Workload replacement.** The workload is redeployed from a configured known-good container image using a replacement (Recreate) deployment strategy.

**3) Recovery validation.** Each agent evaluates the replacement against four conditions: (i) the workload's health endpoint returns HTTP 200; (ii) its current instance identifier differs from the identifier of the instance recorded at containment; (iii) if a known-good manifest is configured, the file hashes reported by the workload equal the manifest exactly; and (iv) the agent's own anomaly confidence for the workload is below 0.3. An agent votes VALIDATE only if all four hold, and three eligible agents must do so. Workload identity is not independently authenticated (the instance-identifier test shows only that the instance is a different one), and the file hashes are self-reported by the workload.

**4) Recovery retries.** A validation failure must persist for more than 90 s, or the replacement must remain unready for 90 s after redeployment, before agents vote for a retry. Each retry needs its own three-agent quorum, is preceded by a forensic snapshot of the failed instance and is delayed by a backoff that starts at 15 s, doubles per retry and is capped at 120 s. At most three retries are allowed, i.e. four recovery attempts in total; if all fail the workload remains quarantined and is marked as needing human attention. Failed individual Kubernetes operations are retried by a separate mechanism (bounded backoff of 2–30 s, at most ten attempts, then the operation is abandoned and attention is requested).

**5) Staged reintegration.** A successful validation quorum places the workload in the QUARANTINE stage. The workload then advances through RESTRICTED, MONITORED and PEER_VALIDATED to FULL. *Every* transition requires its own three-agent quorum. An agent votes for the next stage only when the workload has remained at least 10 s in the current stage, the workload trust has reached the threshold of the *next* stage (20, 40, 60 and 80 respectively; 0 for QUARANTINE), and the agent's local anomaly confidence is below 0.3. Each stage applies a progressively less restrictive NetworkPolicy; reaching FULL removes the policy. A workload at FULL is considered HEALTHY only after the cluster confirms that the isolation policy has been removed.

*Workload trust* here is the per-agent health score of the workload: it falls at 10 points per second while the agent's own observation reaches the local-confidence threshold of 0.5, recovers at 2 points per second otherwise, and is set to zero when containment commits. It is unrelated to agent trust.

Executor ranking with staggered execution (3 s per rank) reduces simultaneous conflicting operations; actions are idempotent and ambiguous outcomes are rechecked before retrying.

## G. Prototype Architecture and Implementation

The prototype is a modular Python 3.11 implementation comprising agents, authenticated communication, trust management, evidence correlation, quorum authorization, certificate generation, admission control, response coordination, forensic preservation and decision logging. Principal technologies: gRPC and Protocol Buffers for messaging; mutual TLS with X.509 certificates; Ed25519 signatures through the Python `cryptography` library; Kubernetes with the Kubernetes Python client; Calico for NetworkPolicy enforcement.

The evaluation uses two environments. A simulation executes the implemented agent logic (including real signatures, gRPC/mTLS and admission logic) against a synthetic workload model, with a fake Kubernetes backend. A four-node Minikube cluster with Calico is used to test the Kubernetes containment and recovery operations. Results from the two environments are reported separately.

Six logical workloads represent the healthcare application context; synthetic records generated with Synthea provide application data. They are not real patient records and are not labelled intrusion-detection samples.

A Kubernetes admission webhook validates selected operations performed by the `resilience-agent` service account (workload-isolation NetworkPolicies and recovery-field updates of Deployments), requiring a quorum certificate. Its enforcement scope is limited to that service account and namespace and it must not be read as a cluster-wide restriction on every administrative principal.

The implementation demonstrates the integration of distributed monitoring, cryptographic evidence exchange, trust-weighted decision-making and coordinated recovery. It is not a clinical deployment or production-scale evaluation, detection thresholds remain configurable engineering parameters, and the system implements neither machine-learning-based detection nor PBFT.

## H. System Requirements

**Table II.** Summary of the proposed system requirements.

| Category | Requirement |
|---|---|
| Functional | Monitor six logical healthcare workloads using four distributed security agents. |
| Functional | Observe network, process, authentication and file-integrity indicators. |
| Functional | Generate structured evidence reports for suspicious events. |
| Functional | Authenticate evidence reports using Ed25519 signatures. |
| Functional | Secure inter-agent communication through gRPC and mTLS. |
| Functional | Maintain observer-specific agent trust scores. |
| Functional | Reject evidence and votes from agents whose trust is below 40. |
| Functional | Correlate evidence using trust-weighted confidence and corroboration. |
| Functional | Require a three-of-four quorum before every state-changing action (containment, validation, recovery retry, stage advance). |
| Functional | Preserve evidence, isolate and replace a contained workload from a known-good image. |
| Functional | Validate recovered workloads before staged reintegration. |
| Non-functional | Reduce dependence on a centralized security decision-maker. |
| Non-functional | Keep monitoring, authentication, decision-making and recovery modular. |
| Non-functional | Remain operational with one unavailable agent; take no action with two or fewer. |
| Non-functional | Provide traceable evidence and decision outcomes (hash-chained decision log). |
| Non-functional | Support Kubernetes deployments with NetworkPolicy enforcement. |

Optional **Table III** (agent trust, for Section III-D):

| Agent trust T | Class | Effect in the implementation |
|---|---|---|
| 50 ≤ T ≤ 100 | Normal | Evidence weighted by T; one full vote |
| 40 ≤ T < 50 | SUSPECT | Flag event only; evidence weighted by T; one full vote |
| T < 40 | Vote-excluded | Evidence rejected; votes not counted |

Rates: contradicted claim −2.5/s, otherwise +0.5/s; rejected message −20.

---------------------------------------------------------------------------------------------------

## Figures

**Fig. 1.** Overall architecture of the proposed distributed cyber-resilience system for containerized healthcare applications. *(placement: after III-A)*
**Fig. 2.** Workflow of evidence validation, quorum-authorized containment, known-good workload replacement, recovery validation, and staged reintegration. *(placement: after III-F)*

## IEEE LaTeX

Preamble: `\documentclass[journal]{IEEEtran}`, `\usepackage{graphicx,booktabs,array,tabularx,amsmath}`.

```latex
\begin{figure*}[!t]
  \centering
  \includegraphics[width=0.92\textwidth]{fig1_architecture.pdf}
  \caption{Overall architecture of the proposed distributed cyber-resilience system for containerized healthcare applications.}
  \label{fig:architecture}
\end{figure*}

\begin{figure}[!t]
  \centering
  \includegraphics[width=\columnwidth]{fig2_workflow.pdf}
  \caption{Workflow of evidence validation, quorum-authorized containment, known-good workload replacement, recovery validation, and staged reintegration.}
  \label{fig:workflow}
\end{figure}
```

```latex
\begin{table}[!t]
\caption{Implemented Monitoring Categories and Detection Conditions}
\label{tab:monitoring}
\centering\small
\begin{tabularx}{\columnwidth}{@{}l X X@{}}
\toprule
Signal & Condition & Configured parameter \\
\midrule
Network & Outbound connection count & Threshold: 8 \\
Network & Outbound transmission rate & Threshold: 256\,000 B/s \\
Process & Suspicious name or directory & Confidence 0.95 \\
Process & Not in allowlist & Confidence 0.60 \\
File integrity & Modified file & Confidence 0.95 \\
File integrity & Missing file & Confidence 0.80 \\
File integrity & New file & Confidence 0.70 \\
Authentication & Failed attempts & $>10$ within 10\,s \\
\bottomrule
\end{tabularx}
\end{table}

\begin{table}[!t]
\caption{Agent Trust Classification}
\label{tab:trust}
\centering\small
\begin{tabularx}{\columnwidth}{@{}l l X@{}}
\toprule
Trust $T$ & Class & Effect \\
\midrule
$50\le T\le100$ & Normal & Evidence weighted by $T$; one full vote \\
$40\le T<50$ & SUSPECT & Flag event only; evidence weighted by $T$; one full vote \\
$T<40$ & Vote-excluded & Evidence rejected; votes not counted \\
\bottomrule
\end{tabularx}
\end{table}
```
Table II (requirements) follows the same `tabularx` pattern with two columns (Category, Requirement); use rows from the table in Section H.

Equations (1)–(6) in LaTeX:
```latex
c(x)=\begin{cases}0,&x\le\theta\\0.5+0.5\min\!\left(1,\frac{x-\theta}{s}\right),&x>\theta\end{cases}\quad(1)
T(t+\Delta t)=\operatorname{clamp}\big(T(t)+r\,\Delta t,\,0,\,100\big)\quad(2)
w_{i,k}=c_{i,k}\,T_i/100\quad(3)\qquad
S_k=\operatorname{mean}_i(w_{i,k})\,\min\!\big(1,|S^{*}_k|/2\big)\quad(4)
W=\max_k S_k+0.15\max(0,|C|-1)\quad(5)\qquad
Q=\sum_{i=1}^{N}V_i\ge 3\quad(6)
```

---------------------------------------------------------------------------------------------------

## Consistency audit (Tasks 4)

| Check | Result |
|---|---|
| III-A to III-H contradictions | One found and fixed: the earlier draft said quorum authorization applied to "protective actions" and reintegration actions loosely; now stated precisely as four quorum-authorized actions (III-E-2, III-F). The earlier III-D sentence that SUSPECT agents are "flagged for reduced confidence" was removed; III-D-2 and III-E-1 now say flag only. |
| Fig. 1 vs Fig. 2 terminology | Aligned. Fig. 1 now shows forensic snapshot initiation and NetworkPolicy isolation as a concurrent pair (a technical inconsistency with the code in the earlier Fig. 1, which drew them in sequence). Both figures use "Three-of-Four Quorum", "Known-Good Container Image", "Recovery Validation", "Reintegration". |
| PBFT / BFT | Not claimed anywhere; III-E-2 states the contrary. Residual risk: `README.md` and the `quorum.py` docstring still say "BFT-style"; do not cite them. |
| Agent trust vs workload trust | Kept separate: III-D-2 defines agent trust; III-F defines workload trust; Fig. 2 says "Workload trust to enter". |
| Unsupported claims | None introduced. Two items are marked unverified: the value of the re-check period for network/auth-only anomalies; any statement about detection sensitivity multipliers is deliberately not made. |

## Limitations the paper should disclose

1. Telemetry, including file hashes and instance identifier, is self-reported by the workload over unauthenticated in-cluster HTTP; a fully compromised workload can misreport.
2. Validation does not authenticate workload identity; and file-hash validation runs only if a baseline manifest is configured.
3. All thresholds, rates and weights are hand-chosen and untuned; no ML detection; no validation against real healthcare attack traces.
4. Three or more colluding or compromised agents can reach quorum and obtain a valid certificate; the scheme is not PBFT/BFT.
5. No key rotation or revocation; static PKI.
6. Clock skew of more than 15 s causes honest messages to be rejected and the delivering agent to lose trust.
7. The forensic snapshot wait is bounded (15 s); the snapshot may be incomplete when replacement begins.
8. The admission webhook constrains only the agent service account in its namespace; certificate replay-consumption is not wired (`certificate.py` docstring item 6 is not enforced), so replay protection rests on epoch and expiry.
9. Results come from the simulator (synthetic) and a sandbox cluster; no results exist for other clusters; not a clinical or production evaluation.
10. Two dashboard cosmetic issues (stale alerts after Reset; story-box wording) are documented and unrelated to the paper's claims.
