# Why the attack demos felt slow, and what was changed (for 17 November)

Nothing here touches the security logic: the four identical agents, the 3-of-4 quorum, the trust model, the quorum certificates and the admission
webhook are unchanged (their tests all still pass; `git diff` shows no change to `quorum.py`, `trust.py`, `certificate.py`, `admission.py`, `webhook.py`,
`detection.py`, `reintegration.py`). The new pieces are: a timing profile (docs/DEMO-PROFILE.md), a cheaper dashboard, `scripts/demo-health.sh`, `scripts/demo-traffic.sh`,
and one observability-only addition to the agents (how late their 1-second loop runs).

## 1. Diagnosis: where the time goes

### 1a. The live tripwire test, step by step (measured on the sandbox cluster, docs/SANDBOX-CLUSTER.md section 4: 83 s in total)

| # | Step | Time | Why | Kind |
|---|---|---|---|---|
| 1 | Tripwire line written -> every agent sees "File Changes" | <= 1 s | each agent reads the workloads' telemetry once per second (`tick_s = 1`) | **design** |
| 2 | Evidence signed and exchanged, three agents vote `CONTAIN`, quorum commits | about 2 s | needs 3 signed votes; a file change (confidence 0.95) is never delayed by the optional persistence re-check | **design** |
| 3 | Isolation NetworkPolicy applied and read back; forensic snapshot started at the same time | about 1 s | replacement waits for both (snapshot wait capped at 15 s) | **design** |
| 4 | Old pod shut down | **30 s** | Kubernetes sends SIGTERM and waits `terminationGracePeriodSeconds` (30 s default); the demo app (`CMD ["python","app.py"]`, PID 1, no signal handler) ignores SIGTERM, so the pod is only killed when the wait ends | **not security design: a workload property** |
| 5 | New pod created, started, readiness probe passes (probe every 2 s) | about 4 s | container start on the laptop; grows when Docker is short of CPU/memory | **load** |
| 6 | `VALIDATE` quorum (health, new instance, file hashes, no anomaly, 3 votes) | about 2 s | agents check every second | **design** |
| 7 | Four stage changes (QUARANTINE -> RESTRICTED -> MONITORED -> PEER_VALIDATED -> FULL) | **4 x about 10.5 s = 42 s** | each needs `stage_dwell_s = 10` **and** the workload trust to reach the next threshold; trust restarts at 0 and climbs 2 per second, the thresholds are 20 points apart (10 s), so the dwell and the trust climb coincide; plus a tick and a quorum round per stage | **design (timers)** |
| 8 | FULL -> HEALTHY: the cluster confirms the isolation policy is gone | about 3 s | read-back from the Kubernetes API | **design** |
| | **Total** | **83 s** | | |

So 72 of the 83 seconds (steps 4 and 7) are *waiting by design of the timers or by the Kubernetes default*, not slowness. What **is** load-dependent is step 5,
the Kubernetes API round trips behind steps 3, 6 and 8, how late an agent's tick starts (a late tick delays detection and every vote by the same amount),
and how quickly the dashboard shows what happened (section 2). On a loaded laptop those can add several seconds and make the sequence look uneven.
`scripts/demo-health.sh` measures them (section 3). The **timing profile** removes steps 4 and most of step 7 for the demo (docs/DEMO-PROFILE.md).

### 1b. Demo Mode (simulator): each scenario, by design

The simulator runs the real agent code on short timers: tick 0.5 s, stage dwell 3 s, workload-trust recovery 8 per second, 2 s simulated pod
replacement, executor stagger 1.5 s, agent-trust loss 5 per second, evidence window 20 s, validation timeout 30 s, investigation budget 6 s. Its times are
**fixed by those timers**, not by the laptop (a loaded laptop only delays what the *page* shows by a second or two). The "~N s" in the scenario list is a ceiling
(`sim/scenarios.py est_s`). Measured simulator medians (docs/RESULTS.md section 4.1, simulator seconds): detected 0.1-0.6 s, isolated 1.2-1.9 s, back at
full access 18.5-19.2 s for the ordinary attacks; 26.2 s for the slow burn; 29.1 s for the validation retry.

| Scenario | Where the time goes (by design) |
|---|---|
| application compromise (about 19 s to full access) | quorum ~1 s; simulated replacement 2 s; four stages of 3 s plus a trust-gated wait each; policy removal |
| false accusation (~25 s) | nothing is isolated: the other three agents see a normal workload and the liar's agent trust falls (5 per second after a 4 s grace) until B, C and D all rate it below 50; the run waits for that |
| forged evidence (~5 s of action, ceiling 12 s) | the compromised agent forges messages for 4 s; each forged envelope is rejected on receipt (and the sender that delivered it loses 20 trust points); the driver then counts the rejections |
| agent crash (~45 s) | agent D is stopped, then C is attacked and the three remaining agents run a normal incident; if the first-ranked executor is the dead agent, the next rank waits its stagger (1.5 s per rank) |
| transient blip (~20 s) | a 2.5 s spike seen by two agents never reaches 3 votes; the run waits until three agents have opened and closed a targeted investigation (grace 1.5 s, budget 6 s) |
| slow burn (~26-40 s) | the signal starts weak and grows (9 + 0.15 per second); two agents see it first and the other two only after 5 s; the run waits until three agree (median 9.7 s to isolate) |
| ambiguous (~30 s) | a steady weak signal (9.5 connections, just over the threshold) seen by two agents opens a targeted investigation (budget 6 s) and ends as a human-review flag on all four agents; nothing is isolated |
| validation retry (~29 s to full access) | the first replacement fails validation: validation is judged only after `validate_timeout_s`, then a new quorum, a back-off (2 s), a second replacement |
| failed validation (~90 s) | every replacement fails: three attempts, each waiting for the validation timeout (30 s in the simulator), with back-off 2 s and 4 s, then NEEDS_ATTENTION |
| lab results tampered / medication failed logins / database cascade (45-60 s) | the same pipeline on workloads E, F and D; the cascade checks three dependants stay un-isolated |
| obstruct (~45 s) | one agent refuses to vote; the other three still reach quorum |

What is *not* by design in Demo Mode: the page's own cost. The Demo and Live pages shared one process, both channels polled their agents every second
whether or not they were on screen, and the page drew up to 400 log rows and re-ran every refresh step each second. That is what section 2 fixes.

## 2. Dashboard performance (what changed, measured)

Changes in `dashboard/index.html` and `dashboard/server.py`:

* **Auto-refresh can no longer pile up.** One refresh cycle runs at a time (a tick that finds the previous cycle still running is skipped); a hidden tab fetches nothing;
  every GET request is abandoned after 8 s instead of waiting forever; the independent steps of one cycle run side by side (two round trips instead of five on a slow link).
* **The activity log is cheap.** Only the newest 200 rows are drawn, with a "Show 200 older events" button (before: 400 rows, sorted and rebuilt every second);
  nothing is recomputed when no event or filter changed; the search text of an event is built once; typing in the search box waits 150 ms.
* **Unchanged markup is not written again** (`innerHTML` skip), which also stops open `<details>` boxes from being closed every second.
* **A channel nobody is looking at is polled every 5 s** instead of every second (`IDLE_AFTER_S` 15, `IDLE_POLL_S` 5 in `dashboard/server.py`); the first request wakes it at once.
  The agents keep 300 events and their last decisions themselves, so nothing is lost.
* The backend records how long each agent's `/status` took (`collector` in `/api/state`), for `demo-health.sh`.

Measured with `node scripts/bench-dashboard.js` (headless Chromium in the cloud sandbox, the Demo Mode `app-compromise` scenario, 20-25 s per run;
the cloud CPU is faster than your laptop, which is why a 4x CPU throttle row is included). **Rendering was never the biggest problem on a fast CPU: the win on a
slow link is the pile-up; on a slow CPU it is the render time.**

| Measurement | before | after |
|---|---|---|
| **Slow link** (1.5 s added to every request, 25 s): requests sent | 94 | **30** |
| ... refresh cycles started | 30 | **8** |
| ... most requests in flight at once | 7 | **4** |
| ... log rows in the page / page elements | 400 / 4,285 | **200 / 2,688** |
| **3,000 buffered events**, normal CPU, 20 s: time in the log redraw | 151 ms (max 11.1) | **94 ms (max 8.2)** |
| ... time in the state redraw | 52 ms | **38 ms** |
| **3,000 buffered events, CPU throttled 4x** (a busy laptop), 20 s: log redraw | 645 ms (max 56.7) | **334 ms (max 29.5)** |
| ... state redraw | 292 ms (max 25.2) | **169 ms (max 20)** |
| ... long tasks (> 50 ms) | 3 (168 ms) | **2 (131 ms)** |

Re-run it yourself on any machine with node + playwright: `node scripts/bench-dashboard.js --seconds 20 --latency 1500 --prefill 3000 --throttle 4`.
(`tests/test_dashboard_perf.py` runs a short version and fails if the pile-up comes back.)

## 3. Is the laptop the bottleneck? `scripts/demo-health.sh`

```bash
bash scripts/demo-health.sh            # one report (read-only, about 20 s)
bash scripts/demo-health.sh --watch    # again every 10 s; leave it in a spare window while you rehearse
```
It prints PASS / WARN / FAIL with a fix for each WARN/FAIL:

1. **The laptop**: free memory, swap in use, load average (WSL), what Docker gives the Minikube nodes, and whether the Demo Mode simulator is running next to the live cluster.
2. **Minikube nodes and pods**: CPU as a share of what each node may allocate, memory, the busiest pods, any pod near its memory limit. It reads the kubelet's own stats
   (`/api/v1/nodes/<node>/proxy/stats/summary`), so the Metrics API / `kubectl top` is **not** needed.
3. **Agent tick delay**: each agent now reports how late its 1-second loop runs (`tick_timing` in `/status`: p95 and maximum lag, late ticks, work per tick). WARN at p95 >= 250 ms, FAIL at a
   tick that started 1.5 s late. A late tick delays detection and every vote by the same amount, so this is the number that tells you the laptop is struggling.
4. **Dashboard latency**: the laptop dashboard's `/api/state` round trip (WARN at 0.5 s, FAIL at 1.5 s) and how long its own poll of each agent takes (WARN at 600 ms; a dead port-forward shows as "no answer").
5. **Synthea traffic**: how many simulated clinicians the client pod runs.

How to read it: if sections 1-2 or the agent ticks WARN/FAIL, the **laptop** is the bottleneck. If only the dashboard section WARNs, restart `scripts/laptop-dashboard.sh live`.
(The agents must run the new image for step 3: `bash scripts/build-images.sh` then `bash scripts/upgrade-app.sh`, see section 6; an older agent shows "no tick timing reported".)

### Pausing the Synthea traffic during attack demos
```bash
bash scripts/demo-traffic.sh pause     # 0 simulated clinicians (the client keeps its availability probe); the client pod restarts once, about 10 s
bash scripts/demo-traffic.sh resume    # back to 3
bash scripts/demo-traffic.sh set 1     # or any number 0-20
bash scripts/demo-traffic.sh status
```
Each clinician sends about one request every 1-3 seconds, so the default 3 are light (about 1-2 requests a second across the workloads); pausing helps only if
`demo-health.sh` shows the CPU is already saturated. While paused the Live charts show no requests, and `scripts/preflight.sh` section 7 FAILs ("no new requests"): resume before the final check.
The agents' decisions never depend on the client (it is not a monitored workload).

## 4. Recommended laptop settings (HP Omen 16, WSL2, Docker Desktop)

I could not test your laptop; these are the standard settings for four 2-CPU / 2200 MiB Minikube nodes (8 CPUs and about 9 GiB for the nodes alone, plus WSL, Windows and a browser).

* **Power**: plugged in, Windows power mode "Best performance", and the Omen's performance profile on. On battery the CPU is throttled, which is the most common cause of late ticks.
* **WSL memory and CPUs** (Docker Desktop with the WSL2 backend uses the WSL2 VM, so it is limited here, not in Docker's settings). Create/edit `C:\Users\<you>\.wslconfig`:
  ```
  [wsl2]
  memory=<your RAM minus 6 GB>      # 16 GB laptop: 10GB   32 GB laptop: 22GB   (never below 10GB for this cluster)
  processors=<your threads minus 2> # at least 8 for this cluster (an Omen 16 usually has 12-24 threads)
  swap=2GB
  ```
  then `wsl --shutdown`, start Docker Desktop, `minikube start -p cr-platform`, and run `bash scripts/preflight.sh --wait 120`. Do this days before, not on the day.
* **Docker Desktop**: Settings > Resources. With the WSL2 backend the limits come from `.wslconfig`; if you use the Hyper-V backend instead, set CPUs >= 8 and Memory >= 10 GB.
* **Close** browsers with many tabs, IDEs, OneDrive sync and games while presenting; keep one dashboard tab open (a hidden tab now fetches nothing).
* **Dashboard**: present the live cluster with `bash scripts/laptop-dashboard.sh live` (section 5).
* **Optional, after the 17th**: the project lives under `/mnt/c/...`; reading files across the Windows drive is slower than WSL's own disk. Moving a copy to `~/resilience-platform` speeds up script start-up (it does not change decisions or timing inside the cluster).
* Check with `bash scripts/demo-health.sh` before and during a rehearsal.

## 5. Demo Mode and Live Monitoring cannot slow each other down

* **Different processes for different jobs.** `bash scripts/laptop-dashboard.sh live` starts **only** the live dashboard: no simulator process (the simulator runs four more agents, ticking twice a second, with real signatures).
  `bash scripts/laptop-dashboard.sh` (both pages) keeps the Demo Mode tab as the safe fallback that docs/DEMO-DAY.md relies on, and is fine for a normal presentation now that the page not on screen is nearly idle.
  Switch to `live` if `demo-health.sh` shows late agent ticks or a saturated CPU, and rehearse the fallback with the terminal scenarios instead.
* **The page not on screen is nearly idle.** In both-pages mode the backend polls a channel nobody has asked about for 15 s only every 5 s (and the browser asks for the open page only), then wakes it on the first request.
* The two channels still share nothing for data: simulated records can never reach the Live page (unchanged).
* `demo-health.sh` warns if the simulator is running next to the live cluster.

## 6. Exact steps to apply it on your laptop

In the WSL terminal, project directory, venv active (the same commands as docs/LAPTOP-UPGRADE.md section A):
```bash
cd "/mnt/c/Users/IDREES SHAIKH/resilience-platform" && source .venv/bin/activate
git status --short                                   # expect nothing important
git fetch origin && git checkout claude/cyber-resilience-platform-xxs3oh && git pull origin claude/cyber-resilience-platform-xxs3oh
git log --oneline -1                                 # expect the commit named at the top of docs/PROGRESS.md
sed -i 's/\r$//' scripts/*.sh scripts/*.py && chmod +x scripts/*.sh
bash scripts/demo-health.sh --help | head -3         # expect the usage text
```
**A. Minimal path (about 3 minutes, nothing is rebuilt): the dashboard fixes and the timing profile.**
```bash
bash scripts/demo-profile.sh presentation --dry-run  # read the 10-odd commands it would run
bash scripts/demo-profile.sh presentation            # about 1-2 minutes; stops the agents, rolls the six workloads, restarts the agents
bash scripts/preflight.sh --wait 120                 # expect READY TO PRESENT and "timing profile: presentation"
bash scripts/laptop-dashboard.sh live                # second terminal; open http://127.0.0.1:8091/?mode=live
bash scripts/demo-health.sh                          # the agents' "tick timing" line says "no tick timing reported" until you do path B: that is expected
```
**B. Full path (adds the agent tick-delay numbers; about 5 more minutes).** The agent and in-cluster dashboard images changed, so rebuild and re-apply:
```bash
bash scripts/build-images.sh                         # 1.5-3 minutes; expect "5 files hashed" (the demo app files are unchanged, so the fingerprint is the same)
bash scripts/upgrade-app.sh                          # expect 1/7 ... 7/7 and ">> done."; this re-applies the SHIPPED timing, so:
bash scripts/demo-profile.sh presentation
bash scripts/preflight.sh --wait 120
bash scripts/demo-health.sh
```
**Check the timing (once, in your rehearsal):**
```bash
bash scripts/tripwire-test.sh E --yes                # then watch Live Monitoring -> Security Alerts; expect normal access again in about 30-40 s
bash scripts/demo-profile.sh status                  # prints the active profile
```
**Go back to the shipped timing any time:** `bash scripts/demo-profile.sh default`. **After a tripwire / between rehearsals:** `bash scripts/demo-reset.sh` (it keeps the profile).

## 7. What I could and could not test

Tested (in the cloud sandbox, `python3 -m pytest`): the profile rules and the script's order against a recording `kubectl` (no cluster), the health report's parsing and verdicts,
the tick-lag metric, the collector's idle polling and latency record, the page's guards, and the browser benchmark above. **Not tested:** a real cluster with the profile
(the sandbox cluster from October no longer exists), your laptop, WSL, Docker Desktop. The "presentation" timings are therefore **calculated**, not measured; the default timings are measured.
Please run the tripwire once with the profile on and keep the numbers you see.
