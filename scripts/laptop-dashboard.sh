#!/usr/bin/env bash
# The presenter's dashboard: Demo Mode (local simulator) AND Live Monitoring (your Minikube cluster) on http://127.0.0.1:8091.
#   scripts/laptop-dashboard.sh            # both pages
#   scripts/laptop-dashboard.sh live       # Live Monitoring only (no simulator is started)
# Live data is READ through `kubectl port-forward` to the four agents' status ports. The two pages are separate channels in the
# dashboard backend: simulated data can never reach the Live page and the scenario controls exist only on the Demo page
# (docs/DASHBOARD.md, docs/LIVE_DEMO.md). Everything binds to 127.0.0.1. Ctrl-C stops everything.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8091}"; BASE="${FORWARD_BASE:-18081}"; NS="${NS:-resilience}"
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT INT TERM

LIVE=""
for i in 0 1 2 3; do
  n=$(echo abcd | cut -c$((i+1)))
  lp=$((BASE+i))
  # A forward dies when its pod does (agent-outage demo); the loop re-opens it so the agent reappears on the page by itself.
  ( while true; do kubectl -n "$NS" port-forward --address 127.0.0.1 "svc/agent-$n" "$lp:8081" >/dev/null 2>&1; sleep 1; done ) &
  PIDS+=($!)
  LIVE="${LIVE:+$LIVE,}http://127.0.0.1:$lp/status"
done
# the centralized baseline controller (only exists after scripts/switch-mode.sh baseline); its forward simply keeps retrying otherwise
( while true; do kubectl -n "$NS" port-forward --address 127.0.0.1 "svc/central-controller" "$((BASE+4)):8081" >/dev/null 2>&1; sleep 3; done ) &
PIDS+=($!)
export LIVE_STATUS_URLS="$LIVE" LIVE_BASELINE_URL="http://127.0.0.1:$((BASE+4))" PORT BIND=127.0.0.1
echo ">> Live agents via port-forward: $LIVE_STATUS_URLS"

if [ "${1:-}" = "live" ]; then
  echo ">> Live Monitoring only: http://127.0.0.1:$PORT/?mode=live"
  python3 "$ROOT/dashboard/server.py" &
else
  echo ">> Demo Mode + Live Monitoring: http://127.0.0.1:$PORT/"
  python3 "$ROOT/sim/demo_server.py" --dashboard --dashboard-port "$PORT" &
fi
PIDS+=($!)
wait "${PIDS[-1]}"
