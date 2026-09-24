# demo.md — running the platform live

Assumes everything in [SETUP.md](SETUP.md) is done: tools installed, venv active,
`scripts/setup-cluster.sh`, `scripts/build-images.sh` and `python3 scripts/gen-certs.py` have run.
All commands run from the repository root.

Suggested screen layout: **T1** commands · **T2** agent log · **T3** port-forwards · browser with the dashboard.

---

## 0. (Optional, 1 min) Show the pipeline without Kubernetes

The simulator runs the 4 real agents (real Ed25519, real gRPC over mTLS, real quorum)
against a fake cluster. It's a good backup if the live cluster misbehaves during a presentation.

```bash
python3 sim/local_demo.py            # 4 scenarios, prints metrics, ends with 4 × PASS
```

To watch it on the dashboard:

```bash
# T3
python3 sim/local_demo.py app-compromise --serve
# T1
STATUS_URLS=http://localhost:50251/status,http://localhost:50252/status,http://localhost:50253/status,http://localhost:50254/status \
  PORT=8090 python3 dashboard/server.py
# browser: http://localhost:8090
```

---

## 1. Deploy (distributed mode)

```bash
kubectl config use-context cr-platform
scripts/deploy.sh
```

Expected: 5 pods in `healthcare` (patient-portal, auth-service, records-api, database, client)
and 5 in `resilience` (agent-a..d, dashboard), all `Running`/`Ready`. Check placement:

```bash
kubectl get pods -A -o wide | grep -E "healthcare|resilience"
```

Each `agent-x` shares a node with its workload (zone a = portal, b = auth, c = records, d = database).

## 2. Open the UIs

```bash
# T3 (leave running)
kubectl -n resilience port-forward svc/dashboard 8090:8090 &
kubectl -n healthcare port-forward svc/patient-portal 8080:8080 &
```

- Dashboard: http://localhost:8090. All four nodes should be `HEALTHY`, with trust 100 everywhere.
- Patient portal: http://localhost:8080. Shows synthetic patient records.

Alternative without port-forward: `minikube -p cr-platform service -n resilience dashboard --url` (NodePort 30090).

```bash
# T2: follow quorum decisions, executor actions and rejected messages
kubectl -n resilience logs -f deploy/agent-b | grep -E "QUORUM|EXECUTOR|REJECTED|METRIC"
```

Show the peers really use mTLS and signed evidence:

```bash
kubectl -n resilience get secret agent-a-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -subject -issuer
kubectl -n resilience get configmap peer-pubkeys -o jsonpath='{.data.pubkeys\.json}'
```

## 3. Scenario A: genuine compromise of an app node

> **Not included in this delivery:** the attack-injection step that makes a workload
> actually misbehave was not generated (see the note at the end of this file). Steps 3a and 3b
> are the bookkeeping around it. Run 3a immediately before whatever injection method you use,
> so the agents measure TTD/TTI/TTR from that moment. The full pipeline for this scenario can be
> demonstrated with the simulator (step 0).

3a. Start the incident clock:

```bash
scripts/mark-incident.sh app-compromise C
```

3b. What to point out once Node C's workload misbehaves:

1. Within seconds every agent's card for Node C shows detections and a rising `W(T)`.
2. `Recent quorum decisions` shows **CONTAIN C epoch 1 signed by ≥ 3 agents**.
3. The NetworkPolicy appears, annotated with who authorized it:
   ```bash
   kubectl -n healthcare get networkpolicy
   kubectl -n healthcare get networkpolicy resilience-isolate-records-api -o jsonpath='{.metadata.annotations}'; echo
   ```
4. Recovery: the records-api pod is replaced from `cr-healthcare-app:known-good`:
   `kubectl -n healthcare get pods -w`
5. **VALIDATE** (health + SHA-256 hash check by each agent), then the staged climb
   QUARANTINE → RESTRICTED → MONITORED → PEER_VALIDATED → FULL, about 10 s per stage.
   Each step is its own 3-of-4 quorum decision. Watch the NetworkPolicy's `resilience.io/stage` change, then disappear at FULL.
6. The portal returns errors while C is quarantined (records unavailable) and recovers after reintegration.

## 4. Scenario B (headline): a compromised resilience node falsely accuses a healthy node

```bash
scripts/simulate-compromised-agent.sh A B
```

This writes the incident marker, then flips agent A into "compromised insider" mode. A keeps its
real key and certificate, but signs and broadcasts fabricated high-confidence evidence of all four
types against the healthy auth-service (B), and votes to contain it.

What to show (over about 30 s):

1. `Pending votes`: `CONTAIN:B:1:` has **only A's vote (1/3)**. It never commits.
2. The Node A card: *agent trust (peers' median)* falls linearly (about 2.5 pts/s after an 8 s grace period).
   The **SUSPECT** tag appears below 50. Below 40, A's votes stop counting at all.
3. Nothing is isolated, and the portal keeps working:
   ```bash
   kubectl -n healthcare get networkpolicy        # -> No resources found
   ```
4. The per-agent logic in the log: `kubectl -n resilience logs deploy/agent-c | tail -20`

Restore A (trust then recovers slowly at 0.5 pts/s, since reintegration of trust is gradual):

```bash
scripts/simulate-compromised-agent.sh A restore
```

## 5. Scenario B′: forgery attempt (signature + mTLS binding)

```bash
scripts/simulate-compromised-agent.sh A C forge-evidence
```

A now also sends evidence that *claims* to be signed by B. The `Rejected messages` table fills with
`signer 'B' != mTLS identity 'A'`, and every rejection costs A −20 trust. Then:

```bash
scripts/simulate-compromised-agent.sh A restore
```

## 6. Centralized baseline under the same attack

```bash
scripts/reset.sh                         # clean slate
scripts/switch-mode.sh baseline          # stop agents, start central-controller
scripts/simulate-compromised-controller.sh B
kubectl -n healthcare get networkpolicy  # -> resilience-isolate-auth-service  (authorized-by: CENTRAL)
```

The single controller isolates the healthy auth-service immediately: the **false isolation** the
distributed version withheld. Logins, and therefore the whole portal, fail (open http://localhost:8080).
The dashboard's metrics table marks `false isolation: YES`.

Put things back:

```bash
scripts/simulate-compromised-controller.sh restore
scripts/reset.sh
scripts/switch-mode.sh distributed
```

## 7. Collect the numbers

```bash
python3 scripts/collect-metrics.py --url http://localhost:8090
```

This prints a table and writes `results/metrics-<timestamp>.csv` with, per incident: TTD, TTI, TTR,
validated, full reintegration, trust-recovery time, time-to-flag the lying agent, false isolation,
and availability (from the synthetic client). Metrics live in agent memory, so collect them
**before** running `reset.sh`. Raw event logs are also in each agent at `/var/log/resilience/metrics.jsonl`.

## 8. Tear down

```bash
kill %1 %2 2>/dev/null   # port-forwards
scripts/teardown.sh      # minikube delete -p cr-platform
```

---

### Timing reference (default config, `k8s/resilience/10-config.yaml`)

| Parameter | Value |
|---|---|
| monitoring tick | 1 s |
| evidence window | 30 s |
| contradiction grace | 8 s |
| agent trust decay / recovery | 2.5 / 0.5 per s |
| SUSPECT / vote-exclusion threshold | 50 / 40 |
| stage dwell | 10 s; workload trust +2/s (stage thresholds 20/40/60/80) |
| executor fail-over stagger | 3 s per rank |

Edit the ConfigMap and `kubectl -n resilience rollout restart deploy` to change them.

### Note on attack injection

The on-demand attack-injection script for making an *application* workload actually misbehave
(deliverable 10) is not part of this codebase. Scenarios B, B′ and the baseline comparison, which
target the security layer itself, are fully scripted above.
