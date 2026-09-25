#!/usr/bin/env bash
# Start the dashboard against the local simulator (sim/local_demo.py --serve).
#   scripts/sim-dashboard.sh            # the four simulated agents (status on :50251-50254)
#   scripts/sim-dashboard.sh baseline   # the simulated central controller (status on :50261)
# Opens on http://localhost:8091 (override with PORT=...), so it can run next to the
# cluster dashboard port-forwarded on 8090.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PORT="${PORT:-8091}"
S=http://localhost
if [ "${1:-}" = "baseline" ]; then
  export STATUS_URLS="$S:50261/status" BASELINE_URL="$S:50261"
else
  export STATUS_URLS="$S:50251/status,$S:50252/status,$S:50253/status,$S:50254/status"
fi
echo "dashboard for the simulator: http://localhost:$PORT"
exec python3 "$ROOT/dashboard/server.py"
