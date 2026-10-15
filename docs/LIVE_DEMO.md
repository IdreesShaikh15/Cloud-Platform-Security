# Live demonstration runbook (Live Monitoring on your Minikube cluster)

> **Status of this document: written, NOT yet run on a real cluster.** Every command below exists in the repository and its logic is tested
> (against stand-ins and the simulator), but the author has not run any step on a live cluster. Expected behaviour is stated as
> "expected", never as "observed". Fill in the table in section 9 with your own runs; do not present an expectation as a result.
>
> Everything here uses **synthetic patient data** only. The four workloads are a Patient Portal, a Login & Access Service, an EHR API and a Clinical Database
> (internal names `patient-portal`, `auth-service`, `records-api`, `database`; agents A-D, one each).

## 0. What this runbook never does
* No attack code. The only change made to a workload is **one appended line in one unused marker file** (section 5), made by you with an explicit command.
* The "compromised agent" modes (sections 6-8) only change **one of our own agents' behaviour** (what it signs and sends); they never touch the cluster.
* Nothing is reached from the simulator: Live Monitoring never shows simulated data (docs/DASHBOARD.md, Phase B).

## 1. Start the presenter's dashboard on your laptop (Demo Mode + Live Monitoring)

Why a laptop: Demo Mode drives the **simulator**, which must never run inside the cluster or share a process with simulator controls
in a live manifest. The in-cluster dashboard therefore shows the Live page only (NodePort 30090). On your laptop one dashboard process
holds both pages as two separate channels (`docs/DASHBOARD.md`, "Healthcare Phase B").

Prerequisites: the cluster is up (`minikube -p cr-platform status`), `kubectl` points at it (`kubectl config current-context` is
`cr-platform`), the resilience agents are running (`kubectl -n resilience get pods`), and the Python dependencies of the repository
are installed (`python3 -m pip install -r requirements-dev.txt`).

```bash
cd Cloud-Platform-Security
scripts/laptop-dashboard.sh            # both pages on http://127.0.0.1:8091
scripts/laptop-dashboard.sh live       # Live Monitoring only
```

What it does: opens `kubectl port-forward svc/agent-{a,b,c,d} 18081-18084:8081` (re-opened automatically if an agent pod is replaced),
exports `LIVE_STATUS_URLS`, then starts `sim/demo_server.py --dashboard` (the simulator, its control server on loopback with a random
per-run token, and the dashboard, all bound to `127.0.0.1`). Ctrl-C stops everything.

Check it:
1. `http://127.0.0.1:8091/?mode=demo` shows the amber banner "DEMO MODE — SIMULATED DATA. No real healthcare workload is being attacked." and the scenario picker.
2. `http://127.0.0.1:8091/?mode=live` shows the green banner "LIVE MONITORING — CONNECTED TEST CLUSTER", the cluster name (`cr-platform`), "agents reachable: 4 of 4" and the telemetry age. There are no scenario buttons.
3. If the cluster is not reachable the Live page shows "NOT CONNECTED — Live monitoring is unavailable." and nothing else; it never shows the simulator's data.
4. Environment variables (rarely needed): `PORT` (dashboard, default 8091), `FORWARD_BASE` (first local forward port, default 18081), `NS` (agent namespace, default `resilience`).

Other ways to run it (same code):
* Simulator only: `python3 sim/demo_server.py --dashboard` (the Live page then says it is not configured / not connected).
* Live only, without the helper: `LIVE_STATUS_URLS=http://127.0.0.1:18081/status,...  BIND=127.0.0.1 PORT=8091 python3 dashboard/server.py` after starting the port-forwards yourself.
* In the cluster: `kubectl -n resilience port-forward svc/dashboard 8090:8090` (Live page only; Demo Mode is "not available in this deployment").

If the cluster identity says "name not set", the agents are running an older `resilience-config`: apply `k8s/resilience/10-config.yaml` and restart the agents.
The script also forwards the centralized baseline controller (`svc/central-controller`, local port 18085) and sets `LIVE_BASELINE_URL`, so the Live page works in centralized mode too (step 7); that forward simply keeps retrying while no controller exists.


## 2. Pre-flight (do this before anyone is watching)
1. **Cluster and pods:** `minikube -p cr-platform status`, `kubectl config current-context` (must be `cr-platform`), `kubectl -n healthcare get pods`, `kubectl -n resilience get pods` (four `agent-*`, `dashboard`, `quorum-webhook`, all Running).
2. **Images and config on this version** (see the "rebuild / apply / restart" list in docs/PROGRESS.md, Phase E): `scripts/build-images.sh`, then **`scripts/upgrade-app.sh`** (stops the agents, applies the new known-good hash manifest, rolls the workloads onto the new image, verifies, starts the agents). Then `kubectl apply -f k8s/resilience/10-config.yaml` and `kubectl -n resilience rollout restart deploy/agent-a deploy/agent-b deploy/agent-c deploy/agent-d deploy/dashboard`.
   *Why this order matters:* the agents compare every workload's files with the `known-good-hashes` ConfigMap. If the manifest and the running pods are out of step while the agents run, they see "modified files" on healthy workloads and **isolate them**.
3. **Baseline check (read-only):** `python3 scripts/verify-baseline.py` must print four `ok` lines. A `MISMATCH` means the agents would treat that workload as tampered: run `scripts/upgrade-app.sh`.
4. **Isolation really blocks traffic** (Calico): run `scripts/verify-isolation.sh` as described in its header and docs/AUDIT.md, once, before the demo. It briefly cuts one workload off.
5. **Dashboard:** `scripts/laptop-dashboard.sh`, open `http://127.0.0.1:8091/?mode=live`: green banner, `agents reachable: 4 of 4`, cluster `cr-platform`, telemetry age a few seconds.
6. **Where each command runs:** every `kubectl` and `scripts/` command below runs on your laptop in the repository directory. The dashboard stays open in a browser the whole time.

## 3. Step 1: the healthy state (2 minutes)
* **Show:** Live Monitoring, Overview: "4 of 4" healthy workloads, "4 of 4" agents connected, the four cards (Patient Portal, Login & Access Service, EHR API, Clinical Database), "SYNTHETIC DATA ONLY". Open the **Services** table: what each service calls and what stops working if it is isolated.
* **Say:** four identical peer agents, one next to each workload; nothing is isolated unless 3 of 4 agree. Point at the tooltip on *Workload trust* and *Agent trust*.
* **Command (optional, shows the real tamper-evident logs):** `kubectl -n resilience exec deploy/agent-a -- python -m resilience verify-log`
* **Expected:** `decision log OK` (an empty chain is fine).

## 4. Step 2: an agent goes offline, the others carry on
* **Goal:** show that one failed agent does not stop protection (3 of 4) and that it rejoins.
* **Do:** `kubectl -n resilience delete pod -l resilience.io/node=B` (the Login & Access agent's pod; the Deployment recreates it).
* **Show (expected):** the banner turns to "— DEGRADED", `agents reachable: 3 of 4 (unreachable: B)`; the Overview counter "Agents connected 3 of 4"; the B card says "Agent unavailable (the other agents carry on without it)". Within about a minute the pod is Running again and the page returns to 4 of 4 (the laptop script re-opens its port-forward by itself).
* **Combine with step 3:** while B is down, run the tripwire test on C (section 5). Expected: A, C and D (3 of 4) still agree and isolate it; the Who agreed table shows B as **Unavailable**, never as agreeing.
* **Honest limitation (state it):** the agents keep their history in the pod's temporary storage (`emptyDir`). A pod you delete **rejoins with an empty decision log and fresh trust values**; the other three agents keep theirs. The tamper chip then covers the new, short log.
* **Reset:** nothing (it heals itself). Check: `kubectl -n resilience get pods -l app=resilience-agent`.

## 5. Step 3: the tripwire test (a real detection, the real pipeline)
**What it is.** A designated, clearly named marker file, `/app/static/TRIPWIRE-TEST.txt`, exists in every healthcare workload (same image). It is part of the known-good manifest, so the agents' existing **File Changes** check covers it. Nothing reads it; it holds no data. The test appends **one line to that file in one workload**, so the real agents genuinely detect a file change and run the real pipeline: **Isolation -> replace from the known-good copy -> Recovery Check -> staged return to Normal Access Restored**.

**The one command** (a controlled validation event; this is a narrow, explicit exception to "attacks only in the simulator": it is harmless, operator-initiated and limited to that file):
```bash
scripts/tripwire-test.sh C            # dry run: prints exactly what it would do, changes nothing
scripts/tripwire-test.sh C --yes      # does it (C = the EHR API; you may also use A, B, D or the workload name)
```
which runs exactly: `kubectl -n healthcare exec deploy/records-api -- python -c 'open("/app/static/TRIPWIRE-TEST.txt","a").write("tripwire test (operator-initiated, harmless)\n")'`

**Why it is safe.** It is `python -c`, **not a shell** (the agents' process allowlist contains `python`, so the only signal is the file change); one of the four workload names only (never the client probe; refused otherwise); the `healthcare` namespace only; only if `kubectl` points at `cr-platform`; it never touches another file, data or service; the change exists only in that pod's own writable layer and disappears when the pod is replaced. The tests run the script against the real application and the real detector with a stand-in `kubectl` (`tests/test_live_demo_support.py`).

**Expected sequence** (from the configured timers in `10-config.yaml`; *not measured live*): within a few seconds all four agents report **File Changes** on the EHR API (confidence 0.95); **Agreement Reached (3 of 4)** to isolate; the EHR API turns **Isolated** and a NetworkPolicy `resilience-isolate-records-api` appears; evidence is saved; a clean pod is created from the known-good image; **Recovery Check** passes (file hashes match again); then the workload climbs Limited Access -> Monitored Access -> Confirmed by Peers -> **Normal Access Restored** (about 10 s per step by configuration, plus the real pod start time, which the simulator cannot predict).

**Point at:**
1. *Security Alerts* tab: the new alert, "What triggered it": File Changes, "1 modified file(s): static/TRIPWIRE-TEST.txt" (open the reading list / Technical details).
2. The Services table: "ISOLATED NOW" and the sentence "the Patient Portal cannot show patient records".
3. *Who agreed*: at least three agents "Agrees", each with a signed vote; *Actions*: Proposed -> Agreed (3 of 4) -> Attempted -> Verified, with the real Kubernetes results.
4. Terminal: `kubectl -n healthcare get networkpolicy` (the isolation policy exists while isolated), `kubectl -n healthcare get pods -l app=records-api` (a new pod), `kubectl -n resilience logs deploy/agent-c --tail=20`.
5. Recovery tab: the evidence saved before replacement.

**Restore.** Do nothing: the platform replaces the pod from the known-good image and removes the policy in stages. If the agents are not running, or something is stuck: `scripts/reset.sh` (removes isolation policies, clears markers and annotations, redeploys the workloads from the known-good image, restarts the agents). To only undo the file change without the platform: `kubectl -n healthcare rollout restart deployment/records-api` (a new pod has the original file).

**If nothing happens:** run `python3 scripts/verify-baseline.py`; check that the agents are running; remember the first detection needs the file to differ from the manifest: a `MISMATCH` before the test would already have isolated that workload.

## 6. Step 4: a compromised agent lies (false accusation)
* **Do:** `scripts/simulate-compromised-agent.sh A B` (agent A signs made-up evidence that the Login & Access Service is attacked and votes to isolate it). It changes only agent A's own behaviour.
* **Expected:** the other agents do not agree; the Login & Access Service is **never isolated** (one liar is never 3 of 4); within roughly a minute the other agents mark A **SUSPECT** (Agent trust falls); the Waiting for agreement panel shows A "Agrees" alone, B, C, D "Disagrees" with the reason "own observation of B normal".
* **Restore:** `scripts/simulate-compromised-agent.sh A restore`. Trust climbs back slowly (see the tooltip).

## 7. Step 5: forgery
* **Do:** `scripts/simulate-compromised-agent.sh A D forge-evidence` (A claims to be B while signing with its own key).
* **Expected:** receivers reject the messages: *Activity Log -> Messages that failed verification*; A's trust falls; the forged claims never count; the Services panel unchanged.
* **Restore:** `scripts/simulate-compromised-agent.sh A restore`.

## 8. Step 6: a compromised agent refuses to vote ("obstruct"), and step 7: the centralized comparison
**Obstruct (live-capable, safe: it only changes agent A's own behaviour; nothing is fabricated or sent).**
1. `scripts/simulate-compromised-agent.sh A C obstruct`
2. `scripts/tripwire-test.sh C --yes` (a real detection; agent A withholds every vote about C)
3. **Expected:** B, C and D (exactly 3 of 4) agree and isolate/recover C; **Who agreed** shows A as **Disagrees** with the reason "[simulated compromise] obstruction: this agent is withholding its vote to ..." and B, C, D as Agrees. A is **not penalised** (the platform has no rule for a missing vote; docs/RESULTS.md 4.10). Note there is no slack: a second silent or obstructing agent would stop containment.
4. `scripts/simulate-compromised-agent.sh A restore`

**Centralized comparison (the baseline the thesis argues against).**
1. `scripts/switch-mode.sh baseline` (stops the four agents, starts the single central controller; never run both).
2. Live Monitoring now says "— CENTRALIZED BASELINE MODE" (no agents, no agreement). `scripts/tripwire-test.sh C --yes` is detected and handled by the one controller.
3. The single point of compromise: `scripts/simulate-compromised-controller.sh B` makes the controller isolate the healthy Login & Access Service alone (no one to disagree). `scripts/simulate-compromised-controller.sh restore`.
4. Back: `scripts/switch-mode.sh distributed`, then `scripts/reset.sh`.

## 9. Your run log (fill this in; leave "not run" until you have run it)
| Step | Date / time | Command(s) | What the dashboard showed | Pass / fail | Notes |
|---|---|---|---|---|---|
| Pre-flight (verify-baseline, verify-isolation) | not run | | | | |
| 1 Healthy state | not run | | | | |
| 2 Agent offline | not run | | | | |
| 3 Tripwire test | not run | | | | |
| 4 Compromised agent (false accusation) | not run | | | | |
| 5 Forgery | not run | | | | |
| 6 Obstruct + tripwire | not run | | | | |
| 7 Centralized comparison | not run | | | | |

## 10. Resetting
* Between steps: `scripts/simulate-compromised-agent.sh <agent> restore` after any compromise mode; wait until the dashboard shows "4 of 4" healthy and every workload "Normal Access Restored".
* Everything: `scripts/reset.sh`; confirm with `kubectl -n healthcare get networkpolicy` (none) and `python3 scripts/verify-baseline.py` (four `ok`).
* Stop the laptop dashboard with Ctrl-C (it stops its port-forwards).

## 11. Limits to state honestly
* Not verified on a live cluster by the author: Calico enforcement, the admission webhook against the real API server, real pod start times, the sampling of the real telemetry (docs/PROGRESS.md, Phase A).
* The agents' history is in `emptyDir`: a deleted agent pod loses it (step 2).
* The file check covers `/app` inside each workload only; the tripwire is a marker, not a model of a real attack.
* The telemetry is reported by the workload itself; a fully compromised workload could falsify it.

## Six workloads (Phase G)
With the default `config/workloads.json` the cluster runs six workloads; the tripwire test accepts `A`-`F` (`scripts/tripwire-test.sh E` is the Laboratory Results Service, `F` the Medication Service) and `verify-baseline.py` should print six `ok`. E and F have no agent of their own: the Live page marks them "watched by all four agents". See docs/WORKLOADS.md for the rebuild steps and the load note.
