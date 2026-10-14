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
