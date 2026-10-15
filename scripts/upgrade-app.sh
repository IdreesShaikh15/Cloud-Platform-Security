#!/usr/bin/env bash
# Roll out a NEW healthcare app image together with its new known-good hash manifest WITHOUT the agents ever seeing a mismatch.
#   scripts/upgrade-app.sh            # after scripts/build-images.sh
#
# Why this exists: the agents compare every workload's files with the known-good-hashes ConfigMap. If the manifest and the running pods are
# ever out of step while the agents are running, they see "modified files" on healthy workloads and isolate them. So: agents OFF,
# manifest applied, workloads rolled onto the new image, manifest verified against the running pods, agents back ON.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"
CTX="$(kubectl config current-context)"
[ "$CTX" = "$EXPECT_CONTEXT" ] || { echo "refused: kubectl context is '$CTX', expected '$EXPECT_CONTEXT'" >&2; exit 3; }

echo ">> 1/5 regenerating the manifest from apps/src (the same source the image was built from)"
python3 "$ROOT/scripts/gen-baseline-hashes.py" >/dev/null
if kubectl -n resilience get deploy central-controller >/dev/null 2>&1; then DECIDERS="central-controller"; else DECIDERS="agent-a agent-b agent-c agent-d"; fi
echo ">> 2/5 stopping the deciders that are deployed: $DECIDERS"
for d in $DECIDERS; do
  kubectl -n resilience scale "deploy/$d" --replicas=0 >/dev/null 2>&1 || true
done
echo ">> 3/5 applying the manifest and rolling the workloads onto the new image"
kubectl apply -f "$ROOT/k8s/generated/known-good-hashes.json"
kubectl apply -f "$ROOT/k8s/base/10-healthcare-apps.yaml"
kubectl -n healthcare rollout restart deploy
kubectl -n healthcare rollout status deploy --timeout=240s
echo ">> 4/5 checking the running workloads against the manifest (read-only)"
python3 "$ROOT/scripts/verify-baseline.py" || { echo "!! mismatch: the agents stay OFF. Fix it (rebuild with scripts/build-images.sh), then run this script again." >&2; exit 1; }
echo ">> 5/5 starting them again: $DECIDERS"
for d in $DECIDERS; do
  kubectl -n resilience scale "deploy/$d" --replicas=1
done
for d in $DECIDERS; do
  kubectl -n resilience rollout status "deploy/$d" --timeout=180s
done
echo ">> done. Restart the agents on the new image only if you also rebuilt cr-agent (scripts/build-images.sh): kubectl -n resilience rollout restart deploy/agent-a deploy/agent-b deploy/agent-c deploy/agent-d"
