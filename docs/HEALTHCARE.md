# Healthcare focus

This project protects, recovers and safely reintegrates **healthcare application workloads** on Kubernetes, using four identical peer
security agents (A-D) that must agree (3 of 4) before anything is isolated. It runs on **synthetic patient data only**
(`apps/src/app.py: synthetic_patients`: 40 made-up records; nothing real is stored, shown or attacked), and the dashboard says so on
every page ("SYNTHETIC DATA ONLY").

> **This is not a compliance claim.** The tables below map what the platform does to the *categories* of the HIPAA Security Rule
> (45 CFR 164 Subpart C) so the design can be discussed in those terms. Nothing here means the system is, or would make an organisation,
> HIPAA compliant. Compliance needs a risk analysis, policies, workforce training, business associate agreements, physical safeguards,
> encryption at rest, breach procedures and much more than this project contains.

## 1. The four workloads (exactly one per agent)
Names are presentation only (`config/workloads.json`; internal Kubernetes names, roles and every security rule are unchanged).

| Node | Shown as | Internal name | What it does for patients and staff | Calls | While it is isolated |
|---|---|---|---|---|---|
| A | Patient Portal | `patient-portal` | shows patient records | Login & Access Service, EHR API | no other service depends on it; the portal itself is unavailable |
| B | Login & Access Service | `auth-service` | signs people in and checks their access | nothing | the Patient Portal and the EHR API cannot work (no sign-in, no access check) |
| C | Electronic Health Records (EHR) API | `records-api` | serves patient records | Login & Access Service, Clinical Database | the Patient Portal cannot show patient records |
| D | Clinical Database | `database` | stores patient records | nothing | the EHR API cannot serve records, so the Patient Portal cannot show them |

The "Calls" column is checked against the application code by a test (`tests/test_workloads.py`): the portal calls auth and records;
the records API calls auth (`/verify`) and the database. "While it is isolated" is **derived from those dependencies** (everything that
calls it, directly or through another service), never typed in. The dashboard shows the same table on the Overview, marks a workload
"ISOLATED NOW" while it is, and says nothing if the agents do not report the metadata.

**Suggested recovery order** (display only, configurable): 1 Clinical Database, 2 Login & Access Service, 3 EHR API, 4 Patient Portal, each with its
rationale (`priority_rationale`), which is about dependencies (what others need first), not about clinical urgency: **no clinical urgency
scores are invented.** It does **not** change what the agents do: every incident is recovered independently and in parallel; the order is a suggestion for a
human operator. Change it in `config/workloads.json` (priorities must be 1-4, all different, or all absent).

## 2. Requirements and what the platform does about them
| Requirement (plain English) | HIPAA Security Rule category (reference only) | What the platform does | Where to see it | Gap / not covered |
|---|---|---|---|---|
| **Confidentiality of patient data in transit** | Technical: transmission security, 164.312(e) | Agent-to-agent traffic is gRPC over mutual TLS with per-agent certificates; messages are signed (Ed25519). | `docs/AUDIT.md`, `tests/test_crypto_evidence.py` | Traffic between the workloads themselves is plain HTTP in this demo; no encryption at rest; no real PHI. |
| **Stop a compromised workload from leaking or spreading** | Technical: access control, 164.312(a); Administrative: security incident procedures, 164.308(a)(6) | A quorum of 3 of 4 agents quarantines the workload with a NetworkPolicy (blocks ingress except the agents, blocks all egress), then lifts it in stages. | `docs/AUDIT.md`, `k8s/resilience/`, scenario "Patient records being copied out of the EHR API" | Depends on a CNI that enforces NetworkPolicy (Calico); only simulated and unit-tested so far, not verified on a live cluster by the author. |
| **Only authorised parties may change protections** | Technical: access control, 164.312(a); person or entity authentication, 164.312(d) | Agents have least-privilege RBAC; an admission webhook accepts a change only with a valid 3-of-4 quorum certificate; one compromised agent cannot isolate anything alone. | `tests/test_admission.py`, `tests/test_rbac.py`, scenario "A security agent falsely accuses the Login & Access Service" | The webhook has not been exercised against a real API server by the author. |
| **Detect tampering with the application** | Technical: integrity, 164.312(c) | Each workload's files are hashed and compared with a known-good manifest; unexpected processes and unusual network or login activity are measured; four agents measure independently. | `agent/resilience/detection.py` | Only what the workload itself reports; kernel, DNS and files outside `/app` are not seen (docs/PROGRESS.md, Phase A). |
| **Resist a lying or failed security agent** | Administrative: contingency plan, 164.308(a)(7) (emergency mode operation) | 3-of-4 agreement; one liar, one crashed agent, or one agent that refuses to vote cannot stop a real containment or cause a false one. | `docs/RESULTS.md`, scenarios "A security agent goes offline..." and "A compromised agent refuses to vote..." | Two compromised agents break it; documented in `docs/RESULTS.md`. |
| **Keep a trustworthy record of what happened** | Technical: audit controls, 164.312(b); Administrative: information system activity review, 164.308(a)(1)(ii)(D) | Hash-chained decision logs, an action audit with real results, signed evidence, read-only evidence snapshots saved before a workload is replaced, and an incident view that separates proposed / authorised / attempted / verified. | `docs/DASHBOARD.md` sections 6-8, `docs/RECOVERY.md` | Logs live on the agents' ephemeral storage: a deleted agent pod loses its history (see docs/LIVE_DEMO.md). |
| **Recover safely and prove the recovery** | Administrative: contingency plan, 164.308(a)(7) (data backup, disaster recovery) | A clean replacement is created from the known-good image, validated (health, file hashes, behaviour), retried a limited number of times, and returned to normal access in stages; if it cannot be recovered it is marked "needs human attention" and stays isolated. | `docs/RECOVERY.md`, scenarios "The first / Every replacement ... fails its health check" | Recovers the application, not its data: the synthetic database is stateless; real data backup and restore is out of scope. |
| **Don't mistake a busy moment for an attack** | Administrative: risk management, 164.308(a)(1)(ii)(B) (reduce risks to a reasonable level) | A targeted investigation for weak signals; an optional persistence re-check (`quorum.persist_recheck_s`, off by default) for short network spikes. | `docs/RESULTS.md` sections 4.9-4.10 | A threshold detector cannot tell a sustained unanimous spike from an attack; an attacker can pulse under the wait. |

## 3. Scenarios in healthcare terms
| Demo scenario | What it shows |
|---|---|
| Patient records being copied out of the EHR API | Real compromise: 3 of 4 agents isolate, validate a clean replacement, restore access in stages. |
| A security agent falsely accuses the Login & Access Service | One liar cannot isolate a healthy service and is marked suspect. |
| A compromised security agent pretends to be another agent | Forged messages fail verification. |
| A security agent goes offline, then the EHR API is attacked | The remaining three still agree and finish the cycle. |
| A short traffic spike on the EHR API seen by two agents | Investigated and closed as a false alarm. |
| Slow, quiet copying out of the EHR API | A weak but persistent signal is confirmed and contained. |
| An unexplained weak signal on the EHR API | Human review is requested; nothing is isolated. |
| The first / every replacement EHR API fails its health check | Retry, then "needs human attention" while it stays isolated. |
| **A compromised agent refuses to vote while the EHR API is attacked** | New: the other three still contain and recover it; the withheld votes are shown with their reason. |

## 4. Not in this version
More than four workloads; a guided tour; new runtime or network sensors; real datasets; encryption at rest; real PHI. See docs/PROGRESS.md "Future work".
