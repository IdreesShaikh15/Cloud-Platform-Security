# Live demonstration runbook

> **Status: partly written.** Phase B wrote section 1 (the laptop dashboard). Phase E adds the step-by-step demonstration
> (healthy state, agent outage, tripwire test, compromised agent, forgery, obstruct, centralized comparison) and the reset steps.
> Nothing in this file has been run against a real cluster by the author; mark each step "verified" yourself only after you have run it.

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
