#!/usr/bin/env bash
# Is the LAPTOP the bottleneck? Read-only report: CPU/memory per Minikube node and pod, how late each agent's 1-second loop runs, dashboard
# poll latency, WSL/Docker limits. See docs/DEMO-TIMING.md.
#   bash scripts/demo-health.sh                # one report
#   bash scripts/demo-health.sh --watch        # every 10 s until Ctrl-C
#   bash scripts/demo-health.sh --dash http://127.0.0.1:8091
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON:-python3}" "$ROOT/scripts/demo_health.py" "$@"
