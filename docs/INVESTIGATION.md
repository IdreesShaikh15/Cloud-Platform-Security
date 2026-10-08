# Targeted investigation before containment

*Phase 2. Plain-English explanation, the exact rules, how it is tested, and an honest statement
of what is and is not new.*

---

## 1. The problem in one paragraph

Before this phase the four agents could only do two things with evidence about a workload:
**vote to isolate it** (if the combined score W(T) reached 0.6 and they saw the problem
themselves) or **do nothing**. If the evidence was borderline, or two agents saw something and two
did not, *nothing happened until the evidence expired* (20-30 seconds). Nobody asked the obvious
question: **"is it still there? look again, and tell me what you see."**

An **investigation** is that question: a short, time-boxed second look, carried out by the same
four peer agents, before anyone acts.

> **Everyday analogy.** Two of four security guards report a noise in room C; the other two hear
> nothing. Today the building waits. With investigation, all four go and listen at the door for ten
> seconds, then write down what they heard and sign it. If three still hear it: lock the room (the
> normal 3-of-4 vote). If three hear nothing: it was a false alarm. If it is still unclear: keep a
> closer eye on the room and ask a human, **but do not lock anything.**

An investigation never isolates anything by itself. Isolation still needs the signed 3-of-4 vote.

---

## 2. When an investigation starts

Every agent checks, once per tick, each *healthy* workload. An investigation is opened when one of
these situations has lasted for `trigger_grace_s` (3 s live) without being resolved:

| Trigger | Plain English | Rule |
|---|---|---|
| `single_sender` | Only one agent reports an anomaly; nobody confirms it | exactly 1 agent with a strong report (confidence >= 0.5) |
| `split_view` | Some agents see it, others do not | 2 agents with a strong report (fewer than the 3 needed for a quorum) |
| `uncertain_band` | Widely seen but weak: the score W(T) is too low to act and too high to ignore | 3+ agents report it **and** 0.3 <= W(T) < 0.6 |

**It never starts when the case is clear.** If 3 or more agents report strongly and W(T) >= 0.6, the
normal quorum runs with no delay. (Tested: `test_genuine_attack_is_not_delayed_by_the_investigation_machinery`:
a real attack is isolated in under 4 s with zero investigations.) The grace period also stops
a one-tick timing difference between agents from causing a pointless investigation.

To avoid four agents opening four investigations at once, agents wait a little longer by rank
(A first, then B, ...). If two do start anyway, they merge (section 5).

---

## 3. What happens during an investigation

The disputed signal types are known (for example only NETWORK). Time budget: `budget_s` (10 s live).

1. **Each agent re-measures only the disputed signals**, on its own, at a higher rate than usual:
   every 0.25 s instead of once per second, at most 120 samples (the *bounded higher sampling rate*).
   A failed sample is recorded as *no data*, not as "normal".
2. **Agents ask each other for fresh, signed observations** over the existing gRPC/mTLS channel
   (new `Investigate` call: `InvestigateRequest` / `InvestigateResponse` in `proto/resilience.proto`).
   Both are signed with the sender's Ed25519 key and checked exactly like evidence and votes: identity of
   the connection, signature, freshness. A peer answers with what *it* measured: for each signal, how
   many of its recent samples still showed the anomaly.
3. **Results are recorded as signed evidence.** Peers' readings that still show the anomaly are added to
   the evidence pool (signed by the peer); each signed answer's hash is kept in the investigation record
   and shown on the dashboard.
4. **Rounds:** an agent asks again every `peer_poll_s` (2 s) and once more just before the deadline.
5. **Early finish:** once everyone has answered and the picture is clear (confirmed or gone), it ends early
   instead of waiting for the full budget. (Measured in the simulator: a blip closed after 3.0 s instead of 6.5 s.)

### How one agent judges the other agents' answers

For each agent (including itself) the *recent* part of its samples (the last 40 %) is classified:

| Verdict | Meaning |
|---|---|
| **persist** | the anomaly is in at least 80 % of its recent samples (it is still there *now*) |
| **gone** | it is in none of its recent samples |
| **flapping** | comes and goes |
| **unknown** | too few valid samples (telemetry down, silent peer, ...) |

---

## 4. The four outcomes (decided by every agent independently, from signed data)

Let *n* = number of agents with a usable answer (vote-excluded agents' answers are ignored).

| # | Outcome | Condition | What happens | Reversible? |
|---|---|---|---|---|
| 1 | **CORROBORATED** | 3 or more agents still see it | The normal CONTAIN quorum proceeds. In addition, an agent that *itself* still sees the anomaly may now vote on a **weak but persistent** signal that W(T) alone would never convict (a short-lived authorisation, section 5). | The isolation itself is the existing pipeline (isolate, recover, validate, staged return) |
| 2 | **FALSE POSITIVE** | 3 or more agents see it gone, and fewer than 2 still see it | Closed. No action. Leftover uncommitted CONTAIN votes and the stale evidence about that workload are dropped. | nothing was done |
| 3 | **AMBIGUOUS** | the agents disagree and it is not confirmed (for example 2 see it, 2 do not) | **Watch state**: heightened monitoring (detection thresholds x0.85) and a **human-review flag** on the dashboard. Nothing is isolated. Ends by itself after `watch_clear_s` (30 s) without any anomaly, or when a normal incident takes over. | yes |
| 4 | **UNCERTAIN** | fewer than 3 agents could re-measure at all (peers silent, telemetry unavailable, ...) | Configured safe policy (`on_uncertain`, default "watch": same as 3). Nothing is isolated. | yes |

**Missing evidence or a timeout is never proof of attack or of safety.** Silent or unreachable agents are
simply *not counted*. Two agents that both see the anomaly are **not** enough to corroborate it
(that is outcome 4, not 1), and two agents that both see nothing are **not** enough to close it as a false
positive (also outcome 4). Tested in `test_missing_data_is_never_proof_of_attack_or_of_safety`.

Two further results exist for bookkeeping: **SUPERSEDED** (the incident or the workload changed while
investigating, so the result is thrown away) and **MERGED** (a duplicate that yielded).

Not implemented (deliberately): the optional "also apply the existing RESTRICTED NetworkPolicy while
watching". That policy blocks all user traffic to the workload, so it must be a signed quorum decision of its
own; building that safely needs a new action type and was out of scope. The safe partial version
(heightened monitoring + human-review flag) is implemented and documented. Setting it up later is a
well-defined extension.

---

## 5. Race safety: every case in the specification

| Case | What the code does | Test |
|---|---|---|
| Unique id, target, **incident version (epoch)**, signals, start time, deadline | every investigation carries all of them; requests and answers repeat target + epoch | `test_trigger_single_sender_after_grace_only` |
| **Two triggers must not start two investigations** of the same target | one active investigation per target per agent; across agents the **earliest id wins** (ids start with the start time) and the later one *merges* into it, with no cooldown for a merge | `test_no_duplicate_investigation_for_the_same_target_and_limits`, `test_earlier_investigation_wins_when_two_start_at_once` |
| **Duplicate or late peer answers** | answers are matched by id and timestamp: a repeat or older one is ignored, with **no trust penalty**; an answer after the close is ignored | `test_duplicate_and_late_responses_are_idempotent` |
| A repeated *request* | answered again from the same investigation (or from its stored final answer after it closed, so early finishers still answer slower peers) | `test_request_is_signed_peers_adopt_it_and_return_signed_readings` |
| **Peers that never answer** | recorded as *unknown*, never as normal/attack; with fewer than 3 usable answers the outcome is UNCERTAIN | `test_peers_that_never_answer_leave_an_uncertain_outcome_not_a_verdict` |
| **Invalidly signed response** | rejected, the sender loses 20 trust (same rule as forged evidence), not counted | `test_invalid_signature_response_is_rejected_and_penalised`, `test_response_signed_by_someone_else_is_rejected` |
| **Peer becomes vote-excluded mid-investigation** | its answers are ignored at decision time, and its *requests* are refused | `test_vote_excluded_peer_mid_investigation_is_not_counted`, `test_requests_from_a_vote_excluded_or_impersonating_agent_are_refused` |
| **Incident resolves first** (a quorum isolates the workload meanwhile) | the investigation is closed SUPERSEDED, its authorisation and watch are discarded | `test_incident_resolving_first_supersedes_the_investigation` |
| **Stale result must never authorise containment of a changed workload or superseded incident** | the authorisation is bound to the incident epoch **and** the workload instance id, and expires after 30 s; it is re-checked at the moment of every vote. A request for an old epoch is refused. If the pod is replaced during an investigation, the investigation is discarded. | `test_stale_result_never_authorises_containment_of_a_changed_workload`, `test_workload_replaced_during_investigation_discards_it` |
| **Agent restarts mid-investigation** | investigations live in memory only, so the restarted agent forgets; it re-adopts the same investigation on the next request from a peer, or starts a new one after the grace period | `test_agent_restart_mid_investigation_rejoins_on_the_next_request` |
| **A requester cannot make us work for longer than our own budget** | the responder clamps the budget to its own configuration | `test_responder_never_samples_longer_than_its_own_budget` |
| **Limits** | at most `max_concurrent` (2) investigations at once per agent, and a per-target `cooldown_s` (60 s) after each | `test_no_duplicate_investigation_for_the_same_target_and_limits`, `test_cooldown_per_target_after_an_investigation_closes` |
| **A lying agent** | its fabricated "it persists" counts as at most one agent; honest agents re-measure for themselves and each voter needs its *own* persistent measurement, so a single liar can neither corroborate its own claim nor earn anyone a containment authorisation | `test_a_lying_agent_cannot_corroborate_its_own_accusation` (end to end) |

---

## 6. What you can see

* **Timeline:** a new category **INVESTIGATION** (🔎). It tells the story in plain sentences: opened (with the reason and the
  question), joined, each signed answer received, closed (with the outcome and reasoning), watch started or cleared,
  human review needed. Filter chip and search work as for other categories.
* **Dashboard panel "Investigations"** (appears when the feature is on): one row per investigation with the
  *question being checked*, **time left** (a shrinking bar), the **outcome**, and each agent's own verdict. A yellow
  **HUMAN REVIEW NEEDED** banner and a **WATCH · review** tag on the node card appear for outcomes 3 and 4. The
  agent drawer lists the agent's own re-measurements and what each peer reported, including "no answer" (unknown)
  and invalidly signed answers.
  Screenshots from the simulator: [`docs/img/investigation-active.png`](img/investigation-active.png),
  [`docs/img/investigation-watch-review.png`](img/investigation-watch-review.png).
* **Metrics** (per incident, `/metrics`, `scripts/collect-metrics.py` CSV): `investigations` (how many),
  `investigation_outcome`, `inv_start_s` and `inv_end_s` (seconds since injection), `human_review` (true after
  AMBIGUOUS/UNCERTAIN). In "transient-blip" and "ambiguous" scenarios, `false_isolation` is true if the workload
  was ever contained.

---

## 7. The simulator scenarios (simulated telemetry only)

All three use *simulated* telemetry only (`world.inject_connections`, a parameterised version of the existing
simulated network signal: it can be visible from only some agents' vantage points, brief, or growing). Nothing
real is attacked. `python3 sim/local_demo.py transient-blip|slow-burn|ambiguous`, add `--no-investigation` to run the
same input with the feature off.

| Scenario | Input | Investigation ON | Investigation OFF |
|---|---|---|---|
| **transient-blip** | 40 connections on one workload for 2.5 s, visible to agents A and B only | Opened at 2.5 s, **closed FALSE POSITIVE after 3.0 s** by all 4 agents; no isolation; the two leftover votes are dropped | No isolation either, but nothing ever resolves it: two votes (2 of 3) stay pending until the evidence expires |
| **slow-burn** | weak anomaly (9 to 12 connections, confidence about 0.53 to 0.62) visible to A,B at once and to C,D after 5 s | Opened at 2.5 s, **CORROBORATED at 7.5 s**; contained at **8.5 s** (3 agents) | Contained only at **20.5 s**, when the signal had grown enough for W(T) to reach 0.6 |
| **ambiguous** | steady weak signal (9.5 connections) visible to A,B only | Opened at 2.5 s, closed **AMBIGUOUS** at 9.0 s by all 4; **WATCH + human review**; never isolated | Nothing happens, nothing is flagged |

*These are single simulator runs on accelerated timers, shown to explain the behaviour, not statistics. The
repeated-run evaluation (20+ seeded trials per scenario) is Phase 5.* A test asserts the slow-burn speed-up
(`test_investigation_makes_slow_burn_containment_faster_than_without`: ON faster by more than 3 s).

---

## 8. Settings (`investigation` block of `k8s/resilience/10-config.yaml`; defaults shown)

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | **The switch.** `false` = the system behaves exactly as before: no triggers, requests refused, no extra events, metrics or dashboard panel |
| `budget_s` | 10 | longest an investigation may run |
| `sample_interval_s` / `max_samples` | 0.25 s / 120 | the bounded higher sampling rate |
| `peer_poll_s` / `request_timeout_s` | 2 s / 2 s | how often to ask peers / how long to wait for one answer |
| `trigger_grace_s` / `trigger_stagger_s` | 3 s / 0.4 s per rank | how long a trigger must hold / spacing so usually only one agent starts it |
| `recent_s` | 4 s | evidence newer than this counts as "currently reported" |
| `band_lo` | 0.3 | lower edge of the uncertain W(T) band (upper edge = the 0.6 vote threshold) |
| `persist_ratio` / `tail_fraction` / `min_tail_samples` | 0.8 / 0.4 / 3 | what "still there" means |
| `max_concurrent` / `cooldown_s` | 2 / 60 s | limits |
| `authorization_ttl_s` | 30 s | how long a CORROBORATED result may authorise weak-signal votes |
| `on_corroborated` | `contain` | `none` = confirm and record only; never lowers the vote bar |
| `on_uncertain` | `watch` | safe policy for outcomes 3 and 4: `watch` or `none` |
| `watch_sensitivity` / `watch_clear_s` | 0.85 / 30 s | heightened monitoring strength / when a watch ends |
| `early_close` | `true` | finish early once everyone answered and it is clear |

---

## 9. How this differs from the usual approaches

| | What it does | Where it falls short for our situation | What investigation adds |
|---|---|---|---|
| **Simple majority vote** | Count the votes; act if more than half (or 2f+1) agree | Votes are taken once, on whatever each voter happened to see at that instant. A transient seen by 2 voters stalls; a weak signal seen by all never reaches the threshold; nobody is asked to look *again* | Voters re-measure on purpose, for a bounded time, and the **same** signed measurements decide between "confirmed", "gone" and "unresolved" |
| **Fixed threshold** | Alert (or act) when a value exceeds a limit | Instantaneous. A one-second spike and a ten-minute leak look the same; a value just under the limit is invisible forever | The question becomes **persistence across several independent observers**, not the size of one reading. A weak but persistent signal can be confirmed; a brief strong one can be dismissed |
| **Standard SOAR playbook** | A scripted workflow, triggered by an alert: enrich (query other tools), then decide or ask a human | One central orchestrator runs it: it, or the data it queries, can be wrong or compromised, and a script usually trusts what its tools return | The "enrichment" is done by **four independent, mutually-distrusting peers** that sign what they saw; a compromised peer is outvoted, and missing answers are treated as *unknown*, not as safe |

### What is established, and what is new (honest statement)

**Established, not claimed as ours:**
Byzantine-fault-tolerant voting with n = 3f + 1 (Castro and Liskov), re-checking or "verifying" an alert before
acting on it (standard in alert-triage and SOAR practice), cross-observer corroboration in distributed
intrusion detection, hysteresis/persistence checks (do not act on a single sample), and reversible
"watch/quarantine-lite" states. Nothing in this phase is a new algorithm.

**What is specific to our setting (the combination, not any single idea):**

1. The re-check is performed by the **same leaderless BFT peers** that vote, over the **same mutually authenticated
   channel**, and its results are **signed evidence**. A verification step can therefore be forged only by
   compromising f + 1 agents, the same bound as the vote itself.
2. The outcome space treats **missing data as its own result** (UNCERTAIN). A silent or blinded agent can
   neither confirm an attack nor clear one, which a plain vote cannot express.
3. **Persistence substitutes for magnitude, with safeguards:** a weak signal confirmed by 3 independent agents
   can be voted on, but only by agents that themselves still see it, only for one incident version and one
   workload instance, and only for a short time.
4. The result plugs into an existing **trust-gated, staged recovery pipeline** (isolate, recover, validate,
   staged return) and a **reversible watch state** with a human-review flag, so "unsure" has a safe place to go.
5. Leaderless **de-duplication** (earliest id wins) so that peers that notice the same thing at the same moment
   converge on one investigation without a coordinator.

We have **not** shown this is better than alternatives in general; Phase 5 compares it with the system
without it, on identical simulated inputs.

---

## 10. Limitations (stated plainly)

* **Simulator evidence only.** Not run on the real cluster; real telemetry may be noisier. No threshold has been tuned against real load.
* **A genuine anomaly seen by only some agents costs the seers some trust:** the existing trust rule lowers an agent's
  trust when its claim is not confirmed by the others' own readings. In the "ambiguous" scenario A and B lose trust
  although they are right. The investigation does not change trust; it only records the disagreement. Using the
  investigation to *shield* honest seers is a possible follow-up.
* **A transient seen by 3 or more agents long enough for the quorum to commit is still contained.** That case is
  "clear" by the rules above; investigation is for the uncertain cases only.
* **A lying agent can waste effort:** it can request investigations. Limits (2 at a time, one per target per 60 s,
  vote-excluded agents refused) bound this, but do not remove it.
* **The weak-signal vote lowers the bar for the 3-agent case.** It is controlled by `on_corroborated`; set it to
  `none` to keep the old bar and use investigation only for closing false positives and flagging.
* The optional *RESTRICTED-while-watching* policy is not implemented (section 4).
* Watch state and investigations are in memory: an agent restart forgets them.
* The review flag can only be seen, not acknowledged: the dashboard is read-only by design.
