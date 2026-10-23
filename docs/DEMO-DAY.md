# Demo day: a 20-minute live presentation, minute by minute

All patient data is **synthetic (Synthea)**; nothing real is ever shown. Everything below was **rehearsed on the author's sandbox cluster** (`docs/SANDBOX-CLUSTER.md`):
the timings in "Expect" are what happened there, in **seconds from the command**. Your laptop will differ by a few seconds to tens of seconds: leave slack and
talk over the waiting. What is **not** verified: WSL, Docker Desktop and your Minikube version. That is exactly what the preflight (section 1) is for.

## 0. Setup (30 minutes before)

**Two terminals** (both in the repository folder, `source .venv/bin/activate`), **four browser tabs** (put them in this order):

| Tab | Address | Used for |
|---|---|---|
| 1 | `http://127.0.0.1:8091/?mode=live` | **Live Monitoring**: the real cluster (green banner). Default **Simple** view. |
| 2 | `http://127.0.0.1:8091/?mode=demo` | **Demo Mode**: the simulator (amber banner). Safe fallback for every live step. |
| 3 | `http://127.0.0.1:18090/` | The **patient portal** (Synthea invented patients). |
| 4 | `docs/presentation-guide.pdf` (open the file) | Slides/fallback screenshots (pages 1-2 problem, section 6 results, section 7 sandbox). |

**Terminal 2 (leave it running all talk):**
```bash
bash scripts/laptop-dashboard.sh        # opens Live + Demo on :8091 and the portal on :18090
```
**Terminal 1 (commands):**
```bash
bash scripts/demo-reset.sh              # known clean start (about 75 s), ends by running the preflight
bash scripts/preflight.sh               # expect: "READY TO PRESENT."  (about 15 s; every FAIL line prints its fix)
```
Then **restart Terminal 2** (Ctrl-C, run it again) so the Security Alerts list starts empty, and reload tabs 1-3.
`RESTART_WINDOW_MIN=1 bash scripts/preflight.sh` only if a recent pod restart is explained (you restarted Docker) and everything else is green.
**Do not** run `scripts/deploy.sh`, `scripts/gen-certs.py` or `scripts/reset.sh` during the day: use `demo-reset.sh`.

**Golden rule: if anything live looks wrong, say "let me show the same thing in Demo Mode" and click tab 2. Never debug in front of the audience.**

## The 20 minutes at a glance

| Time | Part | Tab / terminal | Wait for the system |
|---|---|---|---|
| 0:00 | 1. The problem and the idea | slides / guide pp. 1-2 | none |
| 1:30 | 2. Live: the healthy hospital | tab 1, tab 3 | none |
| 3:30 | 3. One security agent goes down | tab 1 + T1 | about 5 s (forced) |
| 5:00 | 4. The real incident (tripwire) while that agent is still down | tab 1 + T1 | **about 90 s** |
| 8:30 | 5. A lying agent | tab 1 + T1 | about 60 s |
| 11:00 | 6. The centralized comparison (the single point of failure) | tab 1 + T1 | about 40 s, then reset 75-110 s |
| 14:00 | 7. The measured results (Demo Mode) | tab 2 | none (covers the reset) |
| 17:00 | 8. What is and is not proven | guide section 7 + 10 | none |
| 19:00 | 9. Close and questions | | |

Spare (only if time): forgery (section 11), obstruct (section 11).

---

## 1. 0:00-1:30 The problem and the idea (no commands)
**Say:** "Hospitals run patient portals, login, health-record, lab, medication and database services in the cloud. If one is taken over, someone must cut it off quickly, replace it with a clean copy, check the copy and bring it back carefully. Usually one central program decides, and that program is the weak point: if it is fooled, it cuts off a healthy service; if it stops, nothing is protected. I replaced it with four identical security agents that must agree, three out of four, before anything is cut off."
**Show:** guide page 2 (the six workloads and the four agents).
**If it fails:** nothing live here.

## 2. 1:30-3:30 Live Monitoring: the healthy hospital
**Show:** tab 1. Point at: the **green banner** (LIVE MONITORING, cluster `cr-platform`, "agents reachable: 4 of 4", telemetry age a few seconds); the **"SYNTHETIC DATA ONLY"** label; six workload cards all Healthy, trust 100; scroll to **"Live telemetry per service"** (24 small charts: requests per second, outbound connections, data sent, failed logins, labelled LIVE_CLUSTER). Then **tab 3**: the portal lists invented Synthea patients.
**Say:** "This is a real Kubernetes cluster. Three simulated clinicians are looking at invented patients all the time, so the charts move. The agents only count behaviour (connections, programs, files, failed logins); they never read a patient record. Nothing on this page can come from the simulator."
**Optional command (shows the tamper-evident log):** `kubectl -n resilience exec deploy/agent-a -- python -m resilience verify-log` -> `decision log OK`.
**If it fails** (banner red / NOT CONNECTED / charts empty): `bash scripts/preflight.sh` in T1 tells you why; meanwhile click **tab 2** (Demo Mode) and use `dashboard-pK-08-live-charts-sandbox.png` / `dashboard-pK-09-live-overview-sandbox.png` in `docs/img/` (labelled as the author's sandbox cluster).

## 3. 3:30-5:00 One security agent goes down; the others carry on
**Command (T1):**
```bash
kubectl -n resilience scale deploy/agent-b --replicas=0
kubectl -n resilience delete pod -l resilience.io/node=B --grace-period=0 --force
```
**Expect (sandbox):** the banner shows **"— DEGRADED"** and "agents reachable: 3 of 4 (unreachable: B)" about **4 s** later (without the forced delete the agent needs its 30 s shutdown wait first).
**Say:** "I just killed the Login-service agent. Protection continues: any three of the four are enough. Note it stays down until I bring it back."
**Do not restore yet:** the next part (the real incident) runs with this agent down.
**Restore (when told, section 4):** `kubectl -n resilience scale deploy/agent-b --replicas=1` -> **4 of 4** again after **8-30 s**, trust back at 100 (it restarts with fresh history: say so, it is a stated limitation).
**If it fails** (still 4 of 4 after 20 s): wait a little; or just carry on to section 4: the incident also works with all 4 agents. Fallback: Demo Mode scenario **"A security agent goes offline, then the Electronic Health Records (EHR) API is attacked"**.

## 4. 5:00-8:30 The real incident (tripwire test) while one agent is down
**What it is (say it):** "One harmless line appended to an unused marker file in the Laboratory Results Service, by me, with `python -c`, on purpose. It is the only 'attack' in the live demo; the real attacks are in the simulator."
**Command (T1):**
```bash
bash scripts/tripwire-test.sh E            # dry run: prints exactly what it will do, changes nothing
bash scripts/tripwire-test.sh E --yes      # does it
```
**Expect (sandbox, with agent B down; seconds from the command):**

| Seconds | What the page shows |
|---|---|
| 0-4 | File Changes detected by A, C, D; **Agreement reached 3 of 4** (B = **Unavailable**, never counted); the Laboratory Results card turns **Isolated** (a NetworkPolicy exists) |
| about 4-38 | recovering: evidence saved, a clean pod is created (most of it is Kubernetes' 30 s shutdown wait) |
| about 38 | **Recovery Check** passes (VALIDATING) |
| about 40-86 | access returns in four steps, about 10 s each: Limited -> Monitored -> Confirmed by Peers -> **Normal Access Restored** |
| about 92 | workload trust back to 100; the Security Alert is **RESOLVED** |

**Point at, while you wait:** tab 1 -> *Security Alerts* -> open the alert: the **story at the top** ("What happened: ... 3 of the 4 security agents agreed ... cut off ... replaced with a clean copy"); the Services table ("ISOLATED NOW: the Patient Portal cannot show laboratory results"); Detailed view (top-right switch) -> *Who agreed*: A, C, D agree, B Unavailable. Terminal: `kubectl -n healthcare get networkpolicy` (the isolation policy exists during the incident) and `kubectl -n healthcare get pods -l app=lab-results` (a new pod).
**Say:** "Three agents agreed, so it was isolated within seconds. A clean copy was started from the known-good image, tested, and access came back in four careful steps, not all at once."
**Restore:** nothing for the incident: it heals itself (about 92 s). Then bring agent B back: `kubectl -n resilience scale deploy/agent-b --replicas=1` (4 of 4 in 8-30 s).
**If it fails** (nothing happens in 10 s): `python3 scripts/verify-baseline.py` (must print six `ok`); is the agent count 3+? If still nothing: switch to tab 2 and run the Demo Mode scenario **"A file in the Laboratory Results Service is changed"**, or press **Start walkthrough** (the guided demonstration); screenshots: `docs/img/dashboard-pK-05-security-alert-story.png`. After a failed or stuck incident: `bash scripts/demo-reset.sh`.

## 5. 8:30-11:00 A lying agent (false accusation)
**Command (T1):** `bash scripts/simulate-compromised-agent.sh A B`
(agent A signs made-up evidence that the Login service is attacked and votes to isolate it; it changes only agent A's own behaviour).
**Expect (sandbox):** the Login & Access Service is **never isolated** (no NetworkPolicy appears); in *Waiting for agreement* (Detailed view) A "Agrees" alone and B, C, D "Disagree" ("own observation of B normal"); the other agents' trust in A **starts falling after about 20-30 s and reaches 0 within about 60 s** (A is marked **SUSPECT**, then ignored).
**Say:** "One compromised agent is not three. It lied, nobody agreed, nothing healthy was cut off, and the others noticed and stopped believing it. With a central controller that lie would have worked: next."
**Restore:** `bash scripts/simulate-compromised-agent.sh A restore` -> "compromise simulation removed". **Trust climbs back slowly** (minutes); the section 6 command restarts every agent anyway.
**If it fails** (A does not look SUSPECT after 90 s): keep talking, it does not affect the story; the key fact (B not isolated) is visible immediately. Fallback: Demo Mode scenario **"A security agent falsely accuses the Login & Access Service"**.

## 6. 11:00-14:00 The centralized comparison: one liar, one wrong decision
**Say first:** "Now the design this project argues against: one central controller."
**Commands (T1):**
```bash
bash scripts/switch-mode.sh baseline                  # stops the four agents, starts the single central controller (about 6 s)
bash scripts/simulate-compromised-controller.sh B     # the controller falsely accuses the Login service
```
**Expect (sandbox):** the Live banner says **"— CENTRALIZED BASELINE MODE"** (the four agents need about 30 s to disappear: they have a 30 s shutdown wait; start talking, do not wait); within **about 3 s** of the second command the **healthy Login & Access Service (`auth-service`) is isolated** (`kubectl -n healthcare get networkpolicy` shows `resilience-isolate-auth-service`): nobody was there to disagree.
**Say:** "Same lie, opposite result: the single controller isolated a healthy service. In my simulator this happened 20 times out of 20; with the distributed agents, 0 of 20."
**Restore (start it NOW, then switch to tab 2 while it runs):**
```bash
bash scripts/demo-reset.sh        # removes the controller, clears isolation and markers, restarts the four agents; 75-110 s; ends with the preflight
```
Then restart Terminal 2 (the dashboard) to empty the alert list. **Do not** use `switch-mode.sh distributed` on its own: it would leave the isolated service and old incident state behind.
**If it fails** (the controller does not isolate B, or switch-mode errors): say "the live cluster is slow, here is the same result measured in the simulator", go to tab 2 and part 7 (the 20/20 versus 0/20 numbers are on the key-result cards and in section 4.1/4.5 of the results). Run `bash scripts/demo-reset.sh` afterwards.

## 7. 14:00-17:00 The measured results (Demo Mode) while the reset runs
**Show:** tab 2 -> *Performance Results*: the five big cards: **80 of 80** trials went all the way through detection, isolation, recovery and return; **1.2 s** to isolate a genuine attack (simulator time); **0/20** healthy workloads wrongly isolated because one agent lied; **3 of 4** compromised agents are needed to break the design; and the **SIMULATOR** card (nothing here was measured on a real cluster). Click "Show all twelve lines" if asked. Optionally scroll to **4.11 Baselines**.
**Say:** "These numbers come from 1,140 seeded trials in the simulator. The agents, signatures and agreement rules are the real code; the telemetry and Kubernetes are simulated. The design tolerates one bad agent, not two: three compromised agents can isolate a healthy service."
**Optional (60 s):** press **Start walkthrough** (Demo Mode) and play the first steps.
**If the page is slow:** use `docs/presentation-guide.pdf` section 6.

## 8. 17:00-19:00 What is and is not proven (be honest)
**Show:** guide section 7 (sandbox) and 10 (limitations).
**Say:** "I ran the real cluster in a cloud sandbox: all six workloads, the verification scripts, the isolation really enforced, the harmless tripwire test, and eleven minutes of realistic Synthea traffic with no false alarm. Today's live parts are on my laptop. What I have not proven: production hospital conditions, more than one compromised agent, and threshold tuning on real traffic."

## 9. 19:00-20:00 Close and questions
Answers are in guide section 11 (the likely questions). **After the talk:** `bash scripts/demo-reset.sh` to leave the cluster clean.

---

## 10. If something breaks mid-talk (one page)
| Symptom | Do |
|---|---|
| Live page red / NOT CONNECTED | tab 2 (Demo Mode) + screenshots; `bash scripts/preflight.sh` afterwards to see why |
| A workload stays Isolated more than 3 minutes | `bash scripts/demo-reset.sh` (75-110 s); show tab 2 meanwhile |
| An agent is not back after 60 s | `kubectl -n resilience get pods`; `kubectl -n resilience scale deploy/agent-b --replicas=1` (use the right letter); else demo-reset |
| `kubectl` hangs | Docker/Minikube stalled: switch to tab 2 and the guide; fix after the talk |
| Portal tab empty | the port-forward died: restart Terminal 2 (`laptop-dashboard.sh`) |
| Anything unexplained | say "let me show the same in the simulator", tab 2, continue; do not debug live |

## 11. Spare segments (not in the 20 minutes)
* **Forgery:** `bash scripts/simulate-compromised-agent.sh A D forge-evidence`; expect messages in *Activity Log -> Messages that failed verification* (Detailed view) and A's trust falling; restore `bash scripts/simulate-compromised-agent.sh A restore`. Not rehearsed on the sandbox cluster in this form.
* **Obstruct:** `bash scripts/simulate-compromised-agent.sh A E obstruct`, then `bash scripts/tripwire-test.sh E --yes`: B, C, D still contain E; A is shown as "Disagrees ... withholding its vote" and is not penalised. Restore with `... A restore`. Not rehearsed in this form.
* **Other incident:** `bash scripts/tripwire-test.sh F --yes` (Medication Service), same sequence as section 4.

## 12. Rehearsal record (sandbox cluster, 22-23 Oct 2026)
See `docs/SANDBOX-CLUSTER.md` section 8: what was run, with timings, and what went wrong.
