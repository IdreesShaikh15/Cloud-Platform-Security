# Dashboard (post-Phase-5 improvements)

This document grows one part at a time. **Part 1: source labelling and simulator demo controls. Part 2: tabs and the Overview counters.**

## 1. Every record says where it came from

Each event, decision, action record, evidence snapshot and metric row carries `data_source`, one of:

| Value | Meaning |
|---|---|
| `SIMULATOR` | The agent talks to the in-memory fake cluster and fake telemetry (`sim/world.py`). |
| `LIVE_CLUSTER` | The agent talks to the real Kubernetes API and polls real workloads. |
| `UNKNOWN` | Provenance cannot be established. Never treated as either of the other two. |
| `MIXED` | Only on the dashboard's overall label: several different sources are present at once. |

**Who stamps it: the producer, never the dashboard or the browser.** An agent (and the central controller) derives its source
from the two things that feed it, its response backend and its telemetry source (`agent/resilience/provenance.py`). Each class
declares `SOURCE` (`K8sBackend` and `HttpTelemetrySource` say `LIVE_CLUSTER`; `FakeBackend` and the simulator's `FakeTelemetry`
say `SIMULATOR`). **They must agree**; if the backend says one thing and the telemetry another, or a class declares nothing,
the agent's source is `UNKNOWN`. That value is then stamped on every event (`EventLog`), decision, action-audit record,
snapshot and metric row, and on `/status`.

**The dashboard does not silently mix.** It copies the stamp, and if a record's own stamp disagrees with the status it arrived in,
the record becomes `UNKNOWN`. Decisions of different sources are never merged into one row. The overall label is
`SIMULATOR`, `LIVE_CLUSTER`, `MIXED` (more than one value, `UNKNOWN` included) or `UNKNOWN` (nothing reachable). Every
timeline event, decision row and metrics row shows a text chip (SIMULATOR / LIVE CLUSTER / SOURCE UNKNOWN), not only a colour.

A **persistent banner** at the top of the page shows `DEMO — SIMULATED DATA` for the simulator, a green `LIVE CLUSTER DATA` bar for
a real cluster, and a red bar for `MIXED` or `UNKNOWN`.

Limit: a stamp is a claim by the producer. A fully compromised agent could claim `SIMULATOR`. That is why the controls below
do not rely on the stamp alone (section 2).

## 2. Simulator demo controls, and why they cannot reach a live cluster

In simulator mode the dashboard shows a panel: pick a scenario, **Start scenario**, **Reset simulator**. Live scenarios stay
terminal scripts (`scripts/`). One scenario per reset: after a scenario finishes, press Reset (about 3 s) before the next.

```bash
python3 sim/demo_server.py --dashboard        # simulator + dashboard with controls on http://localhost:8091
```

Nine scenarios are offered (the same inputs as `sim/local_demo.py`): app-compromise, false-accusation, forge-evidence, agent-crash,
transient-blip, slow-burn, ambiguous, validation-retry, failed-validation. The two centralized-baseline scenarios are not offered
because the baseline is a separate controller.

**The isolation is enforced in the backend, in layers. Hiding the buttons is only cosmetic.**

1. **Configuration.** The dashboard exposes the controls only if `SIM_CONTROL_URL` and `SIM_CONTROL_TOKEN` are set. The live
   Kubernetes manifests set neither (a test checks this), so in the cluster every `/api/sim/*` request is answered `403`.
2. **Loopback only.** The control URL must be a loopback address, so it can never point at a cluster network.
3. **Data-source gate, checked at the moment of every request.** Every reachable agent must currently report `SIMULATOR`.
   `LIVE_CLUSTER`, `UNKNOWN`, `MIXED`, or "nobody reachable" all answer `403` and the request is **not forwarded**.
4. **Request hardening.** POST only; JSON content type; a custom header (blocks cross-site form posts); a cross-origin
   `Origin` is refused; the dashboard's own login applies when a token is set.
5. **Fixed vocabulary.** A request can only name a scenario from the controller's own list. Only the scenario id is forwarded;
   every other field is dropped.
6. **The simulator control server re-checks everything itself** (`sim/demo_server.py`). It builds only `LocalCluster`
   (fake backend, fake world) and never imports the Kubernetes client (tested in a clean subprocess). It refuses to start inside a Kubernetes pod
   (`KUBERNETES_SERVICE_HOST`). After every build and immediately before every start it asserts that the backend and every agent
   declare `SIMULATOR`. It binds `127.0.0.1`, answers loopback clients only, and needs a random per-run token.

`tests/test_sim_control.py` (27 tests) checks these at the HTTP boundary of the dashboard and of the control server, with a counting
stub that proves a refused request never reaches the simulator, plus one end-to-end test through a real simulated cluster.

Limits, stated plainly: this protects against the controls being used on a live cluster through the dashboard. It does not
make the simulator itself safe to run on a machine that also has cluster credentials; it simply never uses them.

## 3. Bug fixed: "centralized baseline running" in distributed mode

**Cause.** The cluster dashboard manifest (`k8s/resilience/30-dashboard.yaml`) always sets `BASELINE_URL`. The backend returned that
configured URL as `baseline`, and the page printed "centralized baseline running" whenever it was non-empty, whether or not any
controller answered. **Fix.** The backend now also returns `baseline_up`, true only if the central controller's status actually
answers, and the page uses that. (`tests/test_provenance.py::test_baseline_banner_requires_a_controller_that_actually_answers`.)

---

# Part 2: tabs and the Overview

## 4. Six tabs, nothing removed

The page is split into **Overview, Incidents, Investigations, Recovery, Evaluation, Audit** (hash-routed: `#incidents` is a deep link;
arrow keys, Home and End move between tabs; `role="tablist"`/`tabpanel`, `aria-selected`). The dark theme, the source banner, the
header (Pause, export, decision-log chip) and the agent drawer are unchanged and stay outside the tabs. Every existing panel was moved, none
removed:

| Tab | Contents |
|---|---|
| **Overview** | summary counters, the 4 node cards (click one for that agent's own view), simulator demo controls (simulator only), live event timeline with its filters |
| **Incidents** | recent quorum decisions (click a row for the votes and evidence), pending votes, incident metrics |
| **Investigations** | the investigations table and human-review flags (says so if the feature is switched off) |
| **Recovery** | evidence snapshots (with export) and the Kubernetes action audit with its UNKNOWN results |
| **Evaluation** | placeholder for now: the graphs and tables from `results/` arrive in Part 4 |
| **Audit** | per-agent decision-log integrity, rejected messages, event export |

Tabs carry a count badge (active incidents; active investigations + review flags; recovery items needing a human or with an UNKNOWN result; rejected messages).

## 5. The Overview counters: exact definitions

Computed by the backend (`summary_view` in `dashboard/server.py`) from what the agents report. No number is estimated.

| Counter | Definition |
|---|---|
| Healthy workloads | phase `HEALTHY` and not flagged "needs human attention" |
| Unhealthy workloads | everything else, including a workload no agent reports on (**no data is never shown as healthy**) |
| Active incidents | workloads whose incident is not over (phase other than HEALTHY) |
| Active investigations | investigations still ACTIVE (the four agents' copies of one investigation count once); workloads under watch that need human review are shown as REVIEW NEEDED |
| Recoveries succeeded | incidents that ended with the cluster confirming the workload HEALTHY again |
| Recoveries failed | replacement pods that failed validation (each retry), and separately how many incidents were abandoned for a human |
| Agents connected / unreachable | agents that answered the dashboard just now / configured but silent (named) |

The recovery counts are kept by each agent **since it started** (in memory, shown as "since the agents started"); the dashboard shows the
largest count among agents so one restarted agent does not hide history. Agents that report no counters (for example an older image) are
not guessed at; `counters_reported_by` lists who did.

**Severity is never colour alone:** every tile shows an icon and a word (✔ ALL HEALTHY / ⚠ IN PROGRESS / ✖ UNREACHABLE / NEEDS HUMAN / GAVE UP ...) and has an accessible label.

`tests/test_dashboard_tabs.py` (10 tests) checks that every element the page had before is inside exactly the right tab, the counter
definitions on synthetic and real agent states, the agent-side counters through a recovery lifecycle, and drives the real page in
Chromium (clicking every tab, arrow/Home/End keys, deep link, counter words, no JavaScript errors).

---

# Part 3: the incident detail view

## 6. What an "incident" is, and how to open one

An **incident** is one containment of one workload: a quorum committed CONTAIN for it (incident number = the workload's incident version, `#1`, `#2`...).
A lone accusation, a false alarm or an investigation that closed as a false positive is **not** an incident (it stays in the Investigations tab and the timeline).

Incidents tab -> **Incidents** table (newest first; source, workload, contained at, status, current phase/stage, recovery attempt, who signed). Click a row
(or use the deep link `#incident/<SOURCE>/<target>/<number>`, e.g. `#incident/SIMULATOR/C/1`) to open one view; "All incidents" or the Incidents tab returns to the list.
The view refreshes while open. Status words: IN PROGRESS, RESOLVED, NEEDS HUMAN, SUPERSEDED (a newer incident replaced it), UNKNOWN (no agent reports it and it never finished).

## 7. What the view shows

| Section | What it answers | Where the data comes from |
|---|---|---|
| Where it stands now | phase, stage, recovery attempt, **the next step and exactly what is blocking it** (and how many agents give that reason) | each agent's new `next_step` on `/status`, computed with the same predicates the voting uses (dwell time, workload trust, anomaly, validation, cluster confirmation) |
| What triggered it | who signed the CONTAIN, the weighted score W(T), the evidence each agent cited (repeats collapsed with a count), investigations the votes cite | the committed decision's justification |
| Actions: **proposed -> authorized -> attempted -> verified** | the four stages of every isolate / redeploy / validate / stage change, separately (below) | votes, the decision log, and each agent's action-audit records |
| Votes and who signed | per proposal: committed or not, the 3+ signers, every signed vote, and which agents did **not** vote and why | decisions, VOTE_CAST / VOTE_WITHHELD events |
| Recovery attempts | per attempt: the redeploy's four stages, why it was retried, the evidence snapshot(s) saved before it (what could **not** be captured, export button) | audit records, snapshots |
| Investigations | investigations of this workload around the incident and each agent's outcome | investigation rows |
| Trust changes and why | agent-trust changes and suspicion flags while the incident ran, with their stated reason; those whose reason names this workload are marked | TRUST_CHANGE / FLAG events |
| Rejected evidence and messages | evidence ignored from an excluded agent, signature / identity rejections | REJECTED events, FLAG `EVIDENCE_REJECTED` |
| What each agent observed and exchanged | per-agent observations, evidence sent, evidence received and checked | OBSERVE / EVIDENCE_* events |

### The four stages are four different claims

| Stage | Means | Is NOT |
|---|---|---|
| **PROPOSED** | agents signed votes for the action | not yet agreed |
| **AUTHORIZED** | a 3-of-4 quorum committed it (certificate issued, recorded in the decision log) | not yet done |
| **ATTEMPTED** | an executor really called the Kubernetes API for it. Only the executor(s) that tried count: an agent that found the effect already in place did **not** attempt it | an agent that waited, or whose request was refused (stale incident, invalid certificate) did not attempt it; refusals are listed separately |
| **VERIFIED** | after acting (or before, if it was already done) the cluster was **read** and shows the effect | `applied_unconfirmed` (the call returned but the confirming read failed) is attempted, **not** verified. A timeout that could not be read is **UNKNOWN**, shown as such, until a later read settles it |

"Escalate to a human" is shown as an action row with n/a for the cluster stages, because it is a record that a person is needed, not a Kubernetes call.

## 8. Honest limits of the incident view

* **History is what the dashboard has seen.** The agents only keep short rings (15 decisions, 40 action records, 300 events), so the dashboard remembers what it polled (bounded: 1000 decisions, 4000 action records, 5000 events; lost when the dashboard restarts). An incident that happened before the dashboard started polling is incomplete or absent; the page says so.
* **Events are tied to an incident by time**, from up to 120 s before containment (the evidence window) until the next incident of that workload. Events do not carry an incident number.
* **Trust changes** are agent-trust changes during the window, not a per-incident history of workload trust (only its current value is available).
* Each agent reports its own copy of the incident; where agents differ, "agents report this reason" shows how many agree.
* Tested in the simulator and with synthetic data; not run on a real cluster.

Tests: `tests/test_incident_detail.py` (21 tests): the four-stage semantics (including already-applied, waiting, timeout-unknown, unconfirmed, refused, proposed-only), the status words,
event windowing and source separation, sections, bounded history, bad ids, the agent's `next_step`, three end-to-end runs through the real simulated agents
(genuine compromise: every action goes through all four stages with 3+ signers; false accusation: no incident; failed validation: 3 attempts with snapshots, NEEDS HUMAN, why it is stuck),
and the page in Chromium (list, open, all sections, UNKNOWN and unverified shown, back, deep link, unknown incident, no JavaScript errors).


# Part 4: the Evaluation tab and configurable workloads

## 9. The Evaluation tab

Shows `docs/RESULTS.md` as written (headings, plain-English explanations, tables) with the eight committed figures from `results/`, under a banner **"SIMULATOR RESULTS"**. Nothing is computed or invented by the dashboard: `GET /api/evaluation` parses the committed markdown (every table cell is checked against it in `tests/test_evaluation_tab.py`) and `GET /api/eval/figure/<name>.png` serves only files matching `^[A-Za-z0-9_]{1,60}\.png$` that resolve inside the evaluation directory. Files are looked up in `$EVAL_DIR`, `./evaluation` (inside the dashboard image, staged by `scripts/stage-dashboard.sh` - only RESULTS.md, summary.md and figures, no certificates), or the repository layout. If the files are missing the tab says so. These are simulator results, not measurements of a live cluster, and the tab says so; live runs stay terminal scripts.

## 10. Configurable workloads

One file, `config/workloads.json`, documented in `docs/WORKLOADS.md`. Node cards show a `role:` chip when the role differs from the workload name (agents report `role` on `/status`).

# Healthcare Phase B: Demo Mode and Live Monitoring are two separate pages

A toggle at the top, **Demo Mode | Live Monitoring**, opens two pages with the same layout. They are two **channels** in the dashboard
backend (`dashboard/server.py: Channel`), and they share nothing: their own agents, collector, event history, decision/action history,
incidents and exports. The page you are on is part of the URL (`?mode=demo` / `?mode=live`) and every data request carries it, so
switching pages is a full reload with no state carried over.

## 11. The rules the backend enforces (not the page)
| Rule | Where | Test |
|---|---|---|
| A channel accepts only its own `data_source` (demo: `SIMULATOR`, live: `LIVE_CLUSTER`). A status stamped anything else (including `UNKNOWN`, `MIXED`, missing) is discarded before it reaches any view and shows up only as "rejected" in the connection block. | `Collector.poll_once` | `test_simulated_agents_wired_to_the_live_channel_are_ignored_entirely`, `..._live_agents_wired_to_the_demo_channel...`, `test_unlabelled_or_unknown_data...` |
| Inside an accepted status, every stamped record (event, decision, action record, snapshot, a node's last decision) of another source is dropped and counted. | `Collector._scrub`, `poll_once` | `test_a_simulated_record_inside_a_live_status_is_dropped_not_shown` |
| Metrics and snapshot downloads (fetched straight from agents) are filtered the same way. | `metrics()`, `snapshot_export()` | same file |
| The live page never falls back: if no live agent answers it shows **NOT CONNECTED** and no data. | `connection_view`, page | `test_live_page_never_falls_back_to_the_demo_data` |
| Only the demo channel can have simulator controls. A live channel is built without them even if `SIM_CONTROL_*` is set, and `sim_gate` refuses it again. Nothing is forwarded to the control server. | `Channel.__init__`, `sim_gate` | `test_simulator_controls_are_refused_for_the_live_page_and_nothing_is_forwarded` (a counting stand-in control server sees 0 requests), `test_live_channel_with_a_control_url_in_the_environment_still_has_none`, `test_demo_controls_are_refused_when_the_demo_agents_do_not_report_simulator` |
| In two-page mode a request without a channel is refused (400); the dashboard never guesses. | `resolve()` | `test_strict_mode_needs_an_explicit_channel` |
| The Part 1 rules still hold on the demo channel (loopback control URL, per-run token, POST + JSON + `X-CR-Control`, fixed scenario vocabulary, login). | unchanged | `tests/test_sim_control.py` |

## 12. What each page shows
* **Demo Mode:** banner **"DEMO MODE — SIMULATED DATA. No real healthcare workload is being attacked."**; scenario picker with Start / Reset (only here).
* **Live Monitoring:** banner **"LIVE MONITORING — CONNECTED TEST CLUSTER"** with the cluster identity, agent availability (N of 4, which are down) and telemetry age; "— DEGRADED" if some agents are down; **"NOT CONNECTED — Live monitoring is unavailable."** (everything else hidden) when none answers or when the agents answered with the wrong source. No scenario buttons; the page does not even request them.
* The cluster identity is what the **agents report**: `cluster_name` from `resilience-config` (`10-config.yaml`, display only) and the API server's `/version`. If the agents disagree the page says so; if nothing is configured it says "name not set", it never invents one. Telemetry age = each agent's own age of its last workload poll plus how long ago the dashboard received that report (two durations, so the laptop and the cluster clocks need not agree); more than 10 s is marked STALE.
* The green "LIVE" dot is now **"Auto-refresh on"** / **"Paused"**: it says whether the screen is refreshing, not that the data is live.

## 13. Where it runs
* **Presenter's laptop (both pages):** `scripts/laptop-dashboard.sh` starts the simulator + dashboard on `127.0.0.1:8091` and reads the real agents through `kubectl port-forward` (`LIVE_STATUS_URLS`). See `docs/LIVE_DEMO.md`.
* **In the cluster (Live page only):** the `dashboard` Deployment is unchanged; with no `DEMO_`/`LIVE_STATUS_URLS` it reads `/etc/resilience/config.json` and builds the Live channel alone. The Demo page there says "not available in this deployment". This keeps the Part 1 rule that simulator controls are never in a live manifest.
* **Single source (compatibility):** `STATUS_URLS=...` (`scripts/sim-dashboard.sh`, `demo.md`) behaves as before, but its one source answers only as the page that matches what its agents report (SIMULATOR -> Demo, LIVE_CLUSTER -> Live), so it cannot pose as the other. `STATUS_URLS` together with `DEMO_`/`LIVE_STATUS_URLS` is an error.

## 14. Limits
* Not run against a real cluster: the connected Live page is covered by stand-in agents that serve the same JSON (the tests), and screenshots of it can only come from your cluster.
* The port-forwards of `scripts/laptop-dashboard.sh` are re-opened in a loop, but a forward to a deleted agent pod takes about a second or two to re-attach.
* Switching pages reloads the page; the demo page's history is kept by the dashboard process, the live page's too (both lost when the dashboard restarts).

# Healthcare Phase C: healthcare presentation on the dashboard

* **Names:** each node card shows the presentation name (Patient Portal, Login & Access Service, Electronic Health Records (EHR) API,
  Clinical Database) with the internal Kubernetes name beside it in grey. The names, roles, dependencies and recovery order come from
  `config/workloads.json` (see `docs/WORKLOADS.md`) through the agents' `/status`, so nothing is hard-coded in the page. Internal ids, signed messages,
  logs and every security rule are unchanged (the incident view, drawers and audit still use the internal names; Phase D reworks that wording).
* **Services panel (Overview):** one row per workload: what it calls, what stops working while it is isolated (computed from the dependencies: every
  service that calls it, directly or through another), "ISOLATED NOW" while it is, and the suggested recovery order with its rationale. The recovery order is **display only**: it does not change what the agents do. If the agents do not report this metadata (older image) the panel stays hidden; nothing is invented.
* **Synthetic data:** a permanent chip "SYNTHETIC DATA ONLY: no real patient information" sits beside the Demo / Live toggle on both pages.
* **Scenarios** in the Demo Mode picker are described in healthcare terms (`sim/scenarios.py`), and a new one, "A compromised agent refuses to vote while the EHR API is attacked", shows the withheld votes in the incident view's "did not vote, and why" column with the reason (`docs/img/dashboard-pC-04-obstruct-incident.png`).
