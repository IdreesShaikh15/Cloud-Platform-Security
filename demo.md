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
python3 sim/local_demo.py            # 11 scenarios, prints metrics, ends with 11 × PASS
```

To watch one on the dashboard (any of: app-compromise, false-accusation, forge-evidence, agent-crash):

(Other scenarios: `transient-blip`, `slow-burn`, `ambiguous` (investigation), `validation-retry`, `failed-validation` (safer recovery), `baseline`, `controller-crash`. Run one by name, e.g. `python3 sim/local_demo.py failed-validation`; they use their own ports and need no `--serve`.)


```bash
# T3
python3 sim/local_demo.py app-compromise --serve
# T1
STATUS_URLS=http://localhost:50251/status,http://localhost:50252/status,http://localhost:50253/status,http://localhost:50254/status \
  PORT=8090 python3 dashboard/server.py
# browser: http://localhost:8090
```

For the centralized-controller-crash scenario, serve the controller instead:

```bash
python3 sim/local_demo.py controller-crash --serve                     # T3
STATUS_URLS=http://localhost:50261/status BASELINE_URL=http://localhost:50261 \
  PORT=8090 python3 dashboard/server.py                                 # T1
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

## 2b. Reading the dashboard (use this while presenting)

The dashboard only *shows* what the agents do - it never decides anything. Top to bottom:

**Node cards (one per resilience node).** Coloured square + letter = which node (the letter is
what identifies it; colour is a helper). Left border = the workload's state: green healthy,
red isolated/recovering, amber validating/reintegrating. *Workload trust* is how healthy the
app is believed to be; *agent trust* is how much the other three agents believe this node's
agent (median of their views). **SUSPECT** appears when that falls below 50. **Click a card** to
open that agent's own view.

**Live event timeline.** Every agent narrates what it does, newest first, one plain sentence
per event. Because each agent logs its *own* view, the same moment usually appears 3-4 times,
once per agent; that repetition is the point: independent agents reaching the same conclusion.
Filter by agent (the chips at the top), by category, or type in the search box (e.g. `SUSPECT`,
`W(B)`, `records-api`). Click an event to see its raw details.

| Icon | Category | Read it as |
|---|---|---|
| 👁 | OBSERVE | "Here's what I measured on this workload" (4 signals vs their limits) |
| ➚ | EVIDENCE_SENT | "I signed a report of what I saw and sent it to the others" |
| ➘ | EVIDENCE_RECEIVED | "I got a peer's report; its signature and certificate check out" |
| ⛔ | REJECTED | "I threw a message away" - forged signature or wrong identity |
| Σ | SCORE | "Adding up all the reports, weighted by who sent them: W(T) = …" |
| ✔ | VOTE_CAST | "I vote yes to contain / validate / move on, because …" |
| ✋ | VOTE_WITHHELD | "I am *not* voting, because …" (usually: I don't see it myself) |
| ↕ | TRUST_CHANGE | "My trust in that agent went from X to Y, because …" |
| ⚑ | FLAG | "I now consider that agent SUSPECT / I stop counting its votes" |
| ⚖ | QUORUM | "3 of 4 agents signed it - the decision is made" |
| ⚙ | ACTION | "I applied the isolation / started the redeploy / changed the stage" |
| 🔗 | PEER_LINK | "I lost (or regained) my connection to another agent" |

**Agent detail panel (click a card).** Four questions, answered from that agent's point of view:
1. *What does it measure?* A table of the 4 signals for every workload, value vs limit; red = over the limit.
2. *Whom does it trust?* Its trust in each peer with a 60-second trend line. Dashed lines mark 50
   (SUSPECT) and 40 (votes ignored). Hover the line for exact values.
3. *How does it score the evidence?* For each workload: every report it holds, `confidence × sender's
   trust = weight`, then `× support` (a type reported by only one agent counts half), then W(T).
   It votes to contain only at W ≥ 0.6 **and** if it sees the problem itself.
4. *Why has it voted or not?* One line per proposal with the exact reason.

**Recent quorum decisions.** Every decision with the agents that signed it. Click **why?** to see the
signed votes and the evidence that justified it.

**Pending votes.** Proposals that have not reached 3 signatures yet. For each agent: voted, not voted
(with its reason), or unreachable, plus any *not counted* votes from agents below trust 40.

**Pause / Resume** freezes the whole screen (data is still collected and appears on resume).
**Export events** downloads every agent's events as JSON for the report.

What to point at in each scenario:
- *Genuine compromise (A)*: OBSERVE → EVIDENCE_SENT from several agents → SCORE crosses 0.6 →
  three VOTE_CASTs → QUORUM (A, B, C) → ACTIONs (isolate, recover) → stage-by-stage QUORUMs.
- *False accusation (B)*: only A sends evidence about B; B, C, D log VOTE_WITHHELD "own observation of
  B normal"; their TRUST_CHANGE lines show A dropping; FLAG "SUSPECT", then "votes ignored".
  Pending votes shows CONTAIN B stuck at 1/3. In A's panel you can even see A's *honest* logic saying
  B looks normal while its compromised logic votes anyway.
- *Forgery (B′)*: REJECTED "signer 'B' != mTLS identity 'A'" and a forced TRUST_CHANGE of −20.
- *Agent crash*: PEER_LINK "lost its link to agent D"; QUORUM still signed by A, B, C.

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

## 6b. Safer recovery (simulator; 2 minutes)

```bash
python3 sim/local_demo.py validation-retry     # first replacement pod fails validation, the second passes
python3 sim/local_demo.py failed-validation    # every replacement fails: 3 retries, then NEEDS HUMAN ATTENTION
```

What to point out: an **evidence snapshot** of the compromised pod is saved before it is replaced (dashboard section "Evidence snapshots & Kubernetes actions",
with an export-JSON button and a list of anything that could not be captured); retries are spaced by back-off; after the last one the card turns red
**NEEDS HUMAN ATTENTION**, the workload stays quarantined and is never shown healthy; every Kubernetes action shows its result (applied, applied after timeout, UNKNOWN ...).
Details: `docs/RECOVERY.md`. A screenshot is in `docs/img/dashboard-needs-attention.png`.

## 6c. Targeted investigation (simulator)

```bash
python3 sim/local_demo.py transient-blip       # seen by 2 agents, then gone: investigated, closed as a false positive
python3 sim/local_demo.py slow-burn            # weak at first, persists and grows: confirmed, then contained
python3 sim/local_demo.py ambiguous            # unresolved: a human-review flag, nothing isolated
python3 sim/local_demo.py transient-blip --no-investigation   # same input with the feature off, for comparison
```

## 6d. Verify the security claims on your cluster

```bash
scripts/verify-isolation.sh --namespace healthcare --client-pod <client-pod> --target records-api --agent-pod <agent-pod> --egress-peer auth-service
scripts/verify-rbac.sh      --namespace healthcare --target records-api
scripts/verify-webhook.sh   --namespace healthcare --target records-api            # add --test-failsafe to also test the fail-closed behaviour
```

## 7. Collect the numbers

```bash
python3 scripts/collect-metrics.py --url http://localhost:8090
```

This prints a table and writes `results/metrics-<timestamp>.csv` (git-ignored; the evaluation's committed output is separate, see below) with, per incident: TTD, TTI, TTR,
validated, full reintegration, trust-recovery time, time-to-flag the lying agent, false isolation,
and availability (from the synthetic client). Metrics live in agent memory, so collect them
**before** running `reset.sh`. Raw event logs are also in each agent at `/var/log/resilience/metrics.jsonl`.

### 7b. Repeatable evaluation (simulator)

```bash
python3 -m pip install matplotlib
python3 scripts/evaluate.py            # 20 seeded trials per variant, about 2 hours on 4 cores; resumable
python3 scripts/evaluate.py --report-only    # rebuild graphs and tables from results/raw/trials.jsonl
```

Graphs are in `results/`, explained in `docs/RESULTS.md`.

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
| recovery retries / back-off | 3 retries; 15 s, 30 s, 60 s (cap 120 s); failure judged after `validate_timeout_s` (90 s) |

Edit the ConfigMap and `kubectl -n resilience rollout restart deploy` to change them.

### Note on attack injection

The on-demand attack-injection script for making an *application* workload actually misbehave
(deliverable 10) is not part of this codebase, deliberately: nothing in this repository makes a real workload misbehave. Genuine-compromise scenarios run in the simulator. Scenarios B, B′ and the baseline comparison, which
target the security layer itself, are fully scripted above.
