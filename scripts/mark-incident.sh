#!/usr/bin/env bash
# Record the start of an experiment so agents can measure TTD/TTI/TTR from it.
#   scripts/mark-incident.sh <scenario> <target-node A-D> [attacker-node]
# Writes ConfigMap resilience/cr-attack-marker with a fresh id and timestamp.
set -euo pipefail
SCENARIO="${1:?scenario}"; TARGET="${2:?target node id}"; ATTACKER="${3:-}"
ID="$(date +%s)-$RANDOM"
NOW="$(python3 -c 'import time; print(time.time())')"
MARKER=$(printf '{"id":"%s","scenario":"%s","target":"%s","attacker":%s,"injected_at":%s}' \
  "$ID" "$SCENARIO" "$TARGET" "$( [ -n "$ATTACKER" ] && echo "\"$ATTACKER\"" || echo null )" "$NOW")
kubectl -n resilience create configmap cr-attack-marker --from-literal=marker="$MARKER" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "marker: $MARKER"
