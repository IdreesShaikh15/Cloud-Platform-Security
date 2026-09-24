#!/usr/bin/env bash
# Create a 4-node Minikube cluster with Calico (NetworkPolicy enforcement) and
# label each node with the resilience zone it hosts (a..d = Node A..D).
set -euo pipefail
PROFILE="${PROFILE:-cr-platform}"
DRIVER="${DRIVER:-docker}"
CPUS="${CPUS:-2}"
MEMORY="${MEMORY:-2200}"      # MiB per node; 4 nodes -> ~9 GiB total

echo ">> starting minikube profile '$PROFILE' (4 nodes, Calico CNI, driver=$DRIVER)"
minikube start -p "$PROFILE" --nodes 4 --cni calico --driver "$DRIVER" \
  --cpus "$CPUS" --memory "$MEMORY" --kubernetes-version stable ${MINIKUBE_EXTRA_ARGS:-}

echo ">> waiting for all nodes to be Ready"
kubectl wait --for=condition=Ready nodes --all --timeout=300s

echo ">> waiting for Calico"
kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s

echo ">> labelling nodes with resilience zones"
mapfile -t NODES < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
ZONES=(a b c d)
for i in 0 1 2 3; do
  kubectl label node "${NODES[$i]}" "resilience.io/zone=${ZONES[$i]}" --overwrite
done
kubectl get nodes -L resilience.io/zone
echo ">> cluster ready"
