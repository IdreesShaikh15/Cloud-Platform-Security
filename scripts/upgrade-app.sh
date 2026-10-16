#!/usr/bin/env bash
# Roll out a NEW healthcare app image together with its new known-good hash manifest WITHOUT the agents ever seeing a mismatch.
#   scripts/upgrade-app.sh            # after scripts/build-images.sh
#
# It also applies the RBAC and the platform configuration, and restarts the admission webhook BEFORE the agents come back. Found on a real
# cluster (docs/SANDBOX-CLUSTER.md): after an upgrade that adds workloads, a webhook still running on the OLD configuration refuses every
# isolation of a workload it does not know ("not an isolation policy of the platform"), so a new workload would not be contained at all.
#
# Why this exists: the agents compare every workload's files with the known-good-hashes ConfigMap. If the manifest and the running pods are
# ever out of step while the agents are running, they see "modified files" on healthy workloads and isolate them. So: agents OFF,
# manifest applied, workloads rolled onto the new image, manifest verified against the running pods, agents back ON.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"
CTX="$(kubectl config current-context)"
[ "$CTX" = "$EXPECT_CONTEXT" ] || { echo "refused: kubectl context is '$CTX', expected '$EXPECT_CONTEXT'" >&2; exit 3; }

echo ">> 1/7 regenerating the manifest from apps/src (the same source the image was built from)"
python3 "$ROOT/scripts/gen-baseline-hashes.py" >/dev/null
if kubectl -n resilience get deploy central-controller >/dev/null 2>&1; then DECIDERS="central-controller"; else DECIDERS="agent-a agent-b agent-c agent-d"; fi
echo ">> 2/7 stopping the deciders that are deployed: $DECIDERS"
for d in $DECIDERS; do
  kubectl -n resilience scale "deploy/$d" --replicas=0 >/dev/null 2>&1 || true
done
echo ">> 3/7 applying RBAC + configuration + the manifest, and rolling the workloads onto the new image"
kubectl apply -f "$ROOT/k8s/resilience/00-rbac.yaml" -f "$ROOT/k8s/resilience/10-config.yaml"
kubectl apply -f "$ROOT/k8s/generated/known-good-hashes.json"
kubectl apply -f "$ROOT/k8s/base/10-healthcare-apps.yaml"
# A workload that is deployed but no longer in config/workloads.json (for example after switching from six workloads back to the four-workload
# profile, docs/LAPTOP-UPGRADE.md "fallback") would be left behind and, with the agents back on, seen as an unknown workload. Remove it, its
# Service and its isolation policy now, while the agents are off. Only objects labelled resilience.io/node (never the client) are considered.
WANT=" $(python3 "$ROOT/scripts/gen-workloads.py" --names | tr '\n' ' ')"
for d in $(kubectl -n healthcare get deploy -l resilience.io/node -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
  case "$WANT" in *" $d "*) ;; *)
    echo ">> removing $d: it is deployed but not in config/workloads.json"
    kubectl -n healthcare delete deploy,svc "$d" --ignore-not-found
    kubectl -n healthcare delete networkpolicy "resilience-isolate-$d" --ignore-not-found ;;
  esac
done
kubectl -n healthcare rollout restart deploy
kubectl -n healthcare rollout status deploy --timeout=240s
echo ">> 4/7 checking the running workloads against the manifest (read-only)"
python3 "$ROOT/scripts/verify-baseline.py" || { echo "!! mismatch: the agents stay OFF. Fix it (rebuild with scripts/build-images.sh), then run this script again." >&2; exit 1; }
if kubectl -n resilience get deploy quorum-webhook >/dev/null 2>&1; then
  echo ">> 5/7 restarting the admission webhook so it knows the current workloads (the agents are still off, so nothing is refused meanwhile)"
  kubectl -n resilience rollout restart deploy/quorum-webhook
  kubectl -n resilience rollout status deploy/quorum-webhook --timeout=180s
else
  echo ">> 5/7 no admission webhook deployed (centralized mode): nothing to restart"
fi
echo ">> 6/7 starting them again: $DECIDERS"
for d in $DECIDERS; do
  kubectl -n resilience scale "deploy/$d" --replicas=1
done
for d in $DECIDERS; do
  kubectl -n resilience rollout status "deploy/$d" --timeout=180s
done
if kubectl -n resilience get deploy dashboard >/dev/null 2>&1; then
  echo ">> 7/7 restarting the in-cluster dashboard on the new image"
  kubectl -n resilience rollout restart deploy/dashboard
  kubectl -n resilience rollout status deploy/dashboard --timeout=120s
fi
echo ">> done. The agents started on the image and configuration applied above (rebuilt by scripts/build-images.sh)."
