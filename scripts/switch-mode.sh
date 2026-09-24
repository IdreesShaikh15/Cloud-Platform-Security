#!/usr/bin/env bash
# Switch between the distributed agents and the centralized baseline.
# (Never run both at once: both would act on the same workloads.)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-}" in
  distributed)
    kubectl delete -f "$ROOT/k8s/baseline/central-controller.yaml" --ignore-not-found
    kubectl apply -f "$ROOT/k8s/resilience/20-agents.yaml"
    for n in a b c d; do kubectl -n resilience rollout status "deploy/agent-$n" --timeout=180s; done ;;
  baseline)
    kubectl delete -f "$ROOT/k8s/resilience/20-agents.yaml" --ignore-not-found
    kubectl apply -f "$ROOT/k8s/baseline/central-controller.yaml"
    kubectl -n resilience rollout status deploy/central-controller --timeout=180s ;;
  *) echo "usage: $0 distributed|baseline" >&2; exit 1 ;;
esac
