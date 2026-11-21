# Demo Mode: choose the service, follow the attack, see this run's graphs

Everything here is the **simulator** (Demo Mode, amber banner). It never touches a real cluster; Live Monitoring still has no scenario controls (the live attack is the
terminal tripwire test). The agents' decision logic is exactly the same as before: the quorum, trust model, certificates and webhook are not changed.

## 1. Pick the attack and the service
**Demo controls** has two drop-downs: the **scenario** and **"on the" service** (the six workloads A-F with their names). The description and the expected result under them
change with the service you pick. The service defaults to the one the scenario always used (for example the EHR API for "Patient records being copied out"; the Laboratory Results
Service for "A file is changed"). Three scenarios that used to be tied to one service are now general attack types you can aim at any service:

| Scenario (id) | What it does to the chosen service |
|---|---|
| A file in the service is changed (`lab-results-tampered`) | a file is modified at runtime, after a few odd connection attempts |
| Repeated failed logins from the service (`medication-failed-logins`) | the service starts guessing passwords at the Login & Access Service |
| The service is compromised while others depend on it (`database-cascade`) | a full compromise; the services that depend on it must not be isolated by mistake (the page says so when it has no dependants) |

Scenarios that involve an agent (a liar, a crashed or an obstructing agent) never use the attacked service's own agent for that role: the page picks another one automatically.
"A security agent pretends to be another agent" is not about one service, so the service list shows "no service" for it. The server accepts only the workloads in `config/workloads.json`
and only for scenarios that list them (`tests/test_run_graphs.py`).

## 2. Timing of every scenario (simulator seconds, from pressing Start)
1. **0-5 s: lead-in.** Everything is healthy; the page counts down ("the attack begins in 4 s…").
2. **Attack, about 5-15 s.** The attack is *scripted to progress* so the audience can watch evidence build: for the common attack, outbound connections start creeping up (too weak to vote on
   by themselves), then 5 s later a file is changed and a hidden process starts copying records out. The agents are never slowed down: they react as soon as their own rules say so
   (about 1 s after the decisive step). The slow burn takes longer because the signal really is slow (about 15-20 s).
3. **Recovery, about 16 s** for an isolating scenario: evidence saved, isolation, replacement, recovery check, four access stages, normal access (stage dwell 3 s in the simulator).
A typical isolating scenario therefore finishes in about 30 s (measured 29 s for "Patient records being copied out"). Scenarios that isolate nothing end when the agents have decided not to act
(false accusation about 20-25 s, transient blip about 25 s). Retry scenarios take longer because the failed replacement waits for the validation timeout (8 s in Demo Mode).

## 3. "This run" graphs (Performance Results tab, top)
After you start a scenario, the **Performance Results** tab starts with **This run**: graphs drawn from a recording of that one run (the agents' own status every half second:
what they measured, the evidence score each computed, the votes, trust, the access stage), with vertical lines for the attack steps and for what the system did. **Which graphs appear depends on the
scenario, and their data are different every run:**

| Scenario | Graphs |
|---|---|
| Patient records copied, file changed, failed logins, cascade | what the agents measured per signal; evidence score; signed votes (3 needed); service trust and access stage (cascade: services isolated at once) |
| Slow, quiet copying | connections each agent sees (A and B first, C and D later); evidence score; votes; access |
| False accusation | evidence score about the accused service (stays low); how much B, C, D trust the liar (SUSPECT below 50, ignored below 40); votes |
| Forged messages | forged messages rejected; trust in the sender |
| Agent goes offline | agents answering; evidence; votes; access |
| Obstructing agent | votes withheld; votes; trust in the agent; access |
| Brief spike / faint signal | connections each agent sees; investigations; evidence score |
| First / every replacement fails | recovery attempt and replacement health; access (and evidence for the retry) |

Below the graphs: a small table with **this run's** times (detected, isolated, recovery check passed, normal access) next to the **median of the 20 evaluation runs** of the same kind of scenario when the evaluation has one,
and the **evaluation graph** that matches the scenario (for example the timing box plots, the trust trajectory or the agent-crash chart). **The evaluation graphs and numbers are results of many runs (1,140 seeded trials); they are fixed and
are never changed by a run.** A single run is not a statistic, and the page says so.

## 4. Nothing empty any more: suspicious activity and recovery
A **security alert** starts only when 3 of the 4 agents agree to isolate a workload. Four scenarios correctly isolate nothing (false accusation, forged messages, a brief spike, a faint signal), so the alert list and the
Recovery tab used to look empty. Now:
* **Security Alerts tab -> "Suspicious activity that did NOT lead to an isolation"** lists, as they happen: claims the other agents dispute ("Agent A reports network on the Login Service; B, C and D measure it and see nothing wrong, so no vote is cast"),
  targeted investigations (not those that ended in an isolation: they belong to the alert), agents flagged SUSPECT, and messages that failed verification. It is built only from what the agents report. It is **not** an alert.
* **Recovery tab** says "Nothing was isolated, so nothing needed recovery" and what the agents did instead; as soon as something is isolated, the evidence and action rows appear and keep updating through the recovery.

## 5. Check it yourself
```bash
node scripts/check-demo-scenarios.js                       # every scenario through the real page in headless Chromium (needs node + playwright), about 12 minutes
node scripts/check-demo-scenarios.js --only slow-burn,obstruct --target A
```
It prints, per scenario: the service, seconds, whether the countdown showed, alerts / suspicious rows / evidence rows / action rows, and the graphs drawn. `tests/test_run_graphs.py` checks the rules without a browser.
