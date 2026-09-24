#!/usr/bin/env bash
# Headline experiment: make resilience agent <node> behave as a compromised
# insider that falsely accuses a healthy <target> (or tries to forge B's
# signature). It only flips a behaviour switch inside our own agent.
#   scripts/simulate-compromised-agent.sh A B                   # false accusation
#   scripts/simulate-compromised-agent.sh A C forge-evidence    # impersonation attempt
#   scripts/simulate-compromised-agent.sh A restore             # back to honest
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE="${1:?agent node A-D}"; TARGET="${2:?target node A-D or 'restore'}"; MODE="${3:-false-accusation}"
DEP="deploy/agent-$(echo "$NODE" | tr 'A-D' 'a-d')"
if [ "$TARGET" = "restore" ]; then
  kubectl -n resilience exec "$DEP" -- python -m resilience restore
  exit 0
fi
"$ROOT/scripts/mark-incident.sh" "$MODE" "$TARGET" "$NODE"
kubectl -n resilience exec "$DEP" -- python -m resilience compromise --mode "$MODE" --target "$TARGET"
