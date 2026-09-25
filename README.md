# Distributed Cyber-Resilience Platform for Containerized Cloud Apps

Final-year CSE project. A leaderless, trust-aware platform that completes the whole
**Detect → Verify → Isolate → Recover → Validate → Reintegrate** cycle for a mock
cloud-healthcare app on Kubernetes. The platform keeps making correct decisions
when **one of its own security nodes is compromised**.

- **Setup / prerequisites:** [SETUP.md](SETUP.md)
- **Step-by-step live demo:** [demo.md](demo.md)
- **Try it without Kubernetes:** `python3 sim/local_demo.py`

## Architecture

```
          Minikube (4 nodes, Calico CNI enforces NetworkPolicies)
 ┌──────────────┬──────────────┬──────────────┬──────────────┐
 │ zone a       │ zone b       │ zone c       │ zone d       │
 │ patient-     │ auth-service │ records-api  │ database     │  namespace: healthcare
 │ portal (A)   │ (B)          │ (C)          │ (D)          │  (+ synthetic client = availability probe)
 │ agent-a      │ agent-b      │ agent-c      │ agent-d      │  namespace: resilience
 └──────┬───────┴──────┬───────┴──────┬───────┴──────┬───────┘
        └──── gRPC over mTLS, Ed25519-signed evidence + votes ───┘
                     read-only dashboard (NodePort 30090)
```

A **resilience node** is one workload plus the agent pinned to the same Minikube
node. There is no leader. Each agent runs the ten modules from the spec:

| # | Module | File |
|---|--------|------|
| 1 | Monitoring (network, processes, file hashes, auth failures, health) | `agent/resilience/monitoring.py`, `apps/src/telemetry.py` |
| 2 | Detection: thresholds with linear confidence ramps | `agent/resilience/detection.py` |
| 3 | Threat scoring: weighted evidence score W(T) | `agent/resilience/quorum.py` |
| 4 | Evidence: typed, Ed25519-signed | `agent/resilience/evidence.py`, `crypto.py` |
| 5 | Peer communication: gRPC + mTLS | `proto/resilience.proto`, `agent/resilience/peer.py` |
| 6 | Quorum: BFT-style, n=4, f=1, needs 3 signed votes | `agent/resilience/quorum.py` |
| 7 | Isolation: NetworkPolicy quarantine | `agent/resilience/response.py`, `reintegration.py` |
| 8 | Recovery: redeploy from known-good image | `agent/resilience/response.py` |
| 9 | Validation: health + SHA-256 hash check + new instance | `agent/resilience/agent.py::validation_check` |
| 10 | Reintegration: 5 trust-gated stages | `agent/resilience/reintegration.py` |
| - | Trust scores (0-100, linear decay/recovery) | `agent/resilience/trust.py` |
| - | Metrics: TTD / TTI / TTR / trust recovery / false isolation | `agent/resilience/metrics.py` |
| - | Centralized baseline | `agent/resilience/baseline.py` |
| - | Observability: per-agent event log (last 300, throttled), exposed on `/status` | `agent/resilience/observability.py` |
| - | Dashboard (read-only): node cards, live event timeline, per-agent detail panel | `dashboard/` |

### Decision rule (what feeds the quorum)

For target T, every agent computes over the last 30 s of evidence:

```
w(e)      = confidence(e) × trust(sender)/100
score_k   = mean_senders(max w) × min(1, senders_k / (f+1))      per observation type k
W(T)      = max_k score_k + 0.15 × (corroborated_types − 1)        corroborated = ≥ 2 senders
```

An agent votes `CONTAIN(T)` only if **its own** observation of T has confidence ≥ 0.5
**and** W(T) ≥ 0.6. An action commits when 3 of 4 distinct voters have sent matching
signed votes. Votes from agents whose trust is below 40 (in the receiver's view) don't
count. The quorum mechanism is standard BFT (n = 3f+1). The contribution is the
typed, trust-weighted evidence that feeds it, plus trust-gated reintegration.

Why one liar can't isolate a healthy node:
- A single sender's claims get at most half weight per type, and earn no diversity bonus, so W < 0.6.
- Honest agents never vote on hearsay.
- 1 vote is less than 3.
- The liar's claims are contradicted by what the peers observe, so its trust decays linearly (2.5 pts/s). It is flagged SUSPECT below 50, and its votes are ignored below 40.

### Trust (simple linear model)

```
T(t+dt) = clamp(T + r·dt, 0, 100),   r = −decay while misbehaving/anomalous, +recover otherwise
```

- **Agent trust:** decays 2.5/s while contradicted and recovers 0.5/s. Any forged or mismatched envelope costs −20 immediately.
- **Workload trust:** drops to 0 on isolation and climbs 2/s during reintegration. The stages need 20 / 40 / 60 / 80, and at least 10 s in each stage.

### Reintegration stages

| Stage | Ingress | Egress | Notes |
|---|---|---|---|
| QUARANTINE | agents only | none | set on isolation |
| RESTRICTED | agents only | healthcare ns + DNS | trust ≥ 20 |
| MONITORED | agents + healthcare | healthcare ns + DNS | trust ≥ 40, detection thresholds ×0.7 |
| PEER_VALIDATED | agents + healthcare | healthcare ns + DNS | trust ≥ 60 |
| FULL | policy removed | policy removed | trust ≥ 80 |

Every stage transition is itself a 3-of-4 quorum decision. Each NetworkPolicy is
annotated with the agents that signed for it (`resilience.io/authorized-by`).

## Repository layout

```
proto/                 gRPC/protobuf definitions
agent/resilience/      agent package (+ generated stubs in proto/)
apps/                  mock healthcare app image (stdlib only, synthetic data)
dashboard/             read-only dashboard
k8s/                   manifests (base/, resilience/, baseline/, generated/ ← created by scripts)
scripts/               cluster setup, build, deploy, PKI, experiment helpers, metrics
sim/                   in-process 4-agent simulator (real gRPC/mTLS, fake cluster)
tests/                 pytest suite (unit + integration via simulator)
```

## Honest positioning

This project builds on BFT quorum theory (Castro–Liskov PBFT), Dempster–Shafer-style
evidence fusion, and distributed IDS trust frameworks. It does **not** claim to be the
first distributed security system or to always identify the malicious node. See the spec,
sections 8 and 12.

## Future work (explicitly out of scope for this version)

- Real clinical datasets (Synthea / MIMIC). The app currently uses seeded synthetic records.
- Compromised-agent variants **"goes silent"** and **"blocks a legitimate isolation"**.
  Ranked executor fail-over already exists (`executor_rank`), but these variants are not simulated or measured.
- The **2-of-4 compromised** breaking-point experiment (BFT predicts failure beyond f=1).
- Prometheus / Grafana visualization. There is a custom dashboard plus CSV export instead.
- Isolating the compromised *agent* itself, not just down-weighting it.
- An admission webhook that rejects NetworkPolicy changes lacking a valid quorum certificate.
  Today any agent's RBAC could technically act alone.
- Statistical (non-threshold) detection.
