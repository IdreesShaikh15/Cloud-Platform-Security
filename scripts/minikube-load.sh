#!/usr/bin/env bash
# READ-ONLY load report for the cluster (docs/WORKLOADS.md, "Load"): what the six workloads cost on Minikube.
#   scripts/minikube-load.sh            # needs the metrics-server addon:  minikube addons enable metrics-server -p cr-platform
# Prints node and pod usage (kubectl top) and the CPU/memory requests and limits the manifests ask for. Changes nothing.
# I could not run this from the build environment (no Minikube there): numbers must come from your own cluster.
set -uo pipefail
KUBECTL="${KUBECTL:-kubectl}"
echo "== nodes (usage now)";            $KUBECTL top nodes 2>&1 || echo "(metrics-server not ready: minikube addons enable metrics-server)"
echo; echo "== healthcare pods (usage now)"; $KUBECTL -n healthcare top pods 2>&1 || true
echo; echo "== resilience pods (usage now)"; $KUBECTL -n resilience top pods 2>&1 || true
echo; echo "== requests and limits per healthcare pod"
$KUBECTL -n healthcare get pods -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,CPU_REQ:.spec.containers[*].resources.requests.cpu,MEM_REQ:.spec.containers[*].resources.requests.memory,CPU_LIM:.spec.containers[*].resources.limits.cpu,MEM_LIM:.spec.containers[*].resources.limits.memory' 2>&1
echo; echo "== allocated on each node (requests/limits as a share of the node)"
$KUBECTL describe nodes 2>/dev/null | grep -E "^Name:|Allocated resources|cpu  |memory  " || true
