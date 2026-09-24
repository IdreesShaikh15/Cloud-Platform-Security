#!/usr/bin/env bash
# Return to a clean state between experiments: remove isolation policies,
# redeploy workloads from the known-good image, clear markers, restart agents.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
kubectl -n healthcare delete networkpolicy -l app.kubernetes.io/managed-by=resilience-agents --ignore-not-found
kubectl -n resilience delete configmap cr-attack-marker --ignore-not-found
for d in patient-portal auth-service records-api database client; do
  for k in epoch phase stage isolated-epoch recovered-epoch recovery-requested-at; do
    kubectl -n healthcare annotate deploy "$d" "resilience.io/$k-" >/dev/null 2>&1 || true
  done
done
kubectl apply -f "$ROOT/k8s/base/10-healthcare-apps.yaml"
kubectl -n healthcare rollout restart deploy
kubectl -n healthcare rollout status deploy --timeout=180s
kubectl -n resilience rollout restart deploy
kubectl -n resilience rollout status deploy --timeout=180s
echo ">> reset complete"
