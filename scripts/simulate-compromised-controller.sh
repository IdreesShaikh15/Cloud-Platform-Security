#!/usr/bin/env bash
# Same experiment against the centralized baseline controller.
#   scripts/simulate-compromised-controller.sh B        # controller "accuses" B
#   scripts/simulate-compromised-controller.sh restore
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:?target node A-D or 'restore'}"
if [ "$TARGET" = "restore" ]; then
  kubectl -n resilience exec deploy/central-controller -- python -m resilience restore; exit 0
fi
"$ROOT/scripts/mark-incident.sh" false-accusation "$TARGET" CENTRAL
kubectl -n resilience exec deploy/central-controller -- python -m resilience compromise --target "$TARGET"
