#!/usr/bin/env bash
# Deploy workloads + (by default) the distributed resilience agents + dashboard.
#   scripts/deploy.sh             # distributed mode
#   scripts/deploy.sh baseline    # centralized baseline instead of agents
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-distributed}"

[ -f "$ROOT/k8s/generated/pki.json" ] || python3 "$ROOT/scripts/gen-certs.py"
[ -f "$ROOT/k8s/generated/known-good-hashes.json" ] || python3 "$ROOT/scripts/gen-baseline-hashes.py"
# The admission webhook's certificate + configuration are generated together with the PKI.
# (Re-running gen-certs.py rotates EVERY key; restart the agents afterwards.)
[ -f "$ROOT/k8s/generated/webhook-config.json" ] || python3 "$ROOT/scripts/gen-certs.py"

kubectl apply -f "$ROOT/k8s/base/00-namespaces.yaml"
kubectl apply -f "$ROOT/k8s/generated/pki.json"
kubectl apply -f "$ROOT/k8s/generated/known-good-hashes.json"
kubectl apply -f "$ROOT/k8s/resilience/00-rbac.yaml"
kubectl apply -f "$ROOT/k8s/resilience/10-config.yaml"
kubectl apply -f "$ROOT/k8s/base/10-healthcare-apps.yaml"
kubectl -n healthcare rollout status deploy --timeout=180s

"$ROOT/scripts/switch-mode.sh" "$MODE"
kubectl apply -f "$ROOT/k8s/resilience/30-dashboard.yaml"
kubectl -n resilience rollout status deploy/dashboard --timeout=120s
if [ "$MODE" = "distributed" ]; then
  # LAST: once this exists, the agents' changes need a valid 3-of-4 quorum certificate.
  # failurePolicy is Fail: if the webhook is down the agents' changes are REFUSED (safe, not silent).
  # Break-glass if you must change things without it:  kubectl delete validatingwebhookconfiguration cr-quorum-webhook
  kubectl apply -f "$ROOT/k8s/resilience/40-webhook.yaml"
  kubectl -n resilience rollout status deploy/quorum-webhook --timeout=120s
  kubectl apply -f "$ROOT/k8s/generated/webhook-config.json"
fi
echo ">> deployed ($MODE). Pods:"
kubectl get pods -A -o wide | grep -E "healthcare|resilience"
