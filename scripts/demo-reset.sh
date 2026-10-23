#!/usr/bin/env bash
# CLEAN RESET BETWEEN DEMO RUNS. Returns the LIVE cluster to a clean, healthy, all-trust-100 starting state WITHOUT redeploying anything
# (no image rebuild, no certificates, no RBAC/config/webhook changes), then runs scripts/preflight.sh.
#
#   bash scripts/demo-reset.sh              # do it (about 1-2 minutes)
#   bash scripts/demo-reset.sh --dry-run    # print every command it would run, change nothing
#
# What it does, in this order (so nothing can react to a half-cleaned state):
#   1. if the centralized baseline is running, switch back to the four distributed agents
#   2. switch every simulated agent compromise OFF ("restore")
#   3. stop the four agents (scale to 0)
#   4. delete the isolation NetworkPolicies and the experiment marker; clear the incident annotations on the workloads
#   5. re-apply the workload manifest (puts back any image an incident changed; a no-op when nothing differs)
#   6. replace ONLY the workloads whose files differ from the known-good manifest (e.g. after the tripwire), nothing else
#   7. start the agents again (fresh pods = trust back to 100 and an empty alert history) and restart the in-cluster dashboard
#   8. run scripts/preflight.sh --wait 120
# It refuses to run on any kubectl context other than $EXPECT_CONTEXT (default cr-platform).
# AFTER it: restart `bash scripts/laptop-dashboard.sh` (Ctrl-C, run again) so the Live page's Security Alerts list starts empty.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"; DRY=0
case "${1:-}" in
  "") ;; --dry-run) DRY=1 ;;
  -h|--help) sed -n 2,19p "$0"; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
run() { if [ "$DRY" = 1 ]; then echo "   [dry-run] $*"; else "$@"; fi; }
step() { printf '\n>> %s\n' "$1"; }

CTX="$(kubectl config current-context 2>/dev/null || true)"
if [ "$CTX" != "$EXPECT_CONTEXT" ]; then
  echo "refused: kubectl context is '${CTX:-none}', expected '$EXPECT_CONTEXT'. Run: kubectl config use-context $EXPECT_CONTEXT" >&2; exit 3
fi
mapfile -t NAMES < <(python3 "$ROOT/scripts/gen-workloads.py" --names 2>/dev/null)
[ "${#NAMES[@]}" -ge 5 ] || { echo "cannot read the workload names from config/workloads.json" >&2; exit 2; }
WORK=("${NAMES[@]:0:$(( ${#NAMES[@]} - 1 ))}")
AGENTS=(agent-a agent-b agent-c agent-d); NEED_AGENTS=0
echo "context $CTX; workloads: ${WORK[*]}${DRY:+}"; [ "$DRY" = 1 ] && echo "(dry run: nothing will be changed)"

step "1/8 centralized baseline? (the four distributed agents must be the decision-makers)"
if kubectl -n resilience get deploy central-controller >/dev/null 2>&1; then
  echo "   the central controller is deployed: removing it (the agents are started again in step 7, after the state is clean)"
  run kubectl delete -f "$ROOT/k8s/baseline/central-controller.yaml" --ignore-not-found
  NEED_AGENTS=1
else echo "   no: distributed mode is already active"; fi

step "2/8 switching every simulated compromise OFF (idempotent)"
for a in "${AGENTS[@]}"; do run kubectl -n resilience exec "deploy/$a" -- python -m resilience restore 2>/dev/null || echo "   ($a not answering: it is restarted in step 7 anyway)"; done

step "3/8 stopping the four agents so nothing reacts while the state is cleaned"
if [ "$NEED_AGENTS" = 0 ]; then
  run kubectl -n resilience scale deploy "${AGENTS[@]}" --replicas=0
  run kubectl -n resilience wait --for=delete pod -l app=resilience-agent --timeout=90s
else echo "   the agents were removed together with the baseline mode: nothing to stop"; fi

step "4/8 removing isolation policies, the experiment marker and incident annotations"
for np in $(kubectl -n healthcare get networkpolicy -o name 2>/dev/null | grep 'resilience-isolate' || true); do run kubectl -n healthcare delete "$np" --ignore-not-found; done
run kubectl -n resilience delete configmap cr-attack-marker --ignore-not-found
for d in "${WORK[@]}"; do
  if [ "$DRY" = 1 ]; then echo "   [dry-run] remove the resilience.io/{epoch,phase,stage,isolated-epoch,recovered-epoch,recovery-*,qc*,action,authorized-by} annotations from deploy/$d"; continue; fi
  for k in epoch phase stage isolated-epoch recovered-epoch recovery-requested-at recovery-attempt qc action authorized-by qc-votes; do
    kubectl -n healthcare annotate deploy "$d" "resilience.io/$k-" >/dev/null 2>&1 || true
  done
done

step "5/8 re-applying the workload manifest (restores any image an incident changed; a no-op otherwise)"
run kubectl apply -f "$ROOT/k8s/base/10-healthcare-apps.yaml"
run kubectl -n healthcare rollout status deploy --timeout=180s

step "6/8 replacing only workloads whose files differ from the known-good manifest"
if [ "$DRY" = 1 ]; then echo "   [dry-run] python3 scripts/verify-baseline.py, then rollout restart for each workload that does not match"
else
  python3 "$ROOT/scripts/verify-baseline.py" > /tmp/demo-reset-baseline.$$ 2>&1 || true
  REPLACED=0
  for d in "${WORK[@]}"; do
    if ! grep -q "^ok  *$d:" /tmp/demo-reset-baseline.$$; then
      echo "   replacing $d (its files differ from the manifest, or it could not be checked)"
      kubectl -n healthcare rollout restart "deploy/$d"; kubectl -n healthcare rollout status "deploy/$d" --timeout=180s; REPLACED=1
    fi
  done
  [ "$REPLACED" = 0 ] && echo "   all ${#WORK[@]} workloads match the manifest: nothing to replace"
  rm -f /tmp/demo-reset-baseline.$$
fi

step "7/8 starting the agents again (fresh pods: trust 100, empty alert history) and restarting the in-cluster dashboard"
if [ "$NEED_AGENTS" = 1 ]; then run kubectl apply -f "$ROOT/k8s/resilience/20-agents.yaml"; else run kubectl -n resilience scale deploy "${AGENTS[@]}" --replicas=1; fi
for a in "${AGENTS[@]}"; do run kubectl -n resilience rollout status "deploy/$a" --timeout=180s; done
run kubectl -n resilience rollout restart deploy/dashboard
run kubectl -n resilience rollout status deploy/dashboard --timeout=180s

if [ "$DRY" = 1 ]; then echo; echo "dry run finished: nothing was changed."; exit 0; fi
step "8/8 preflight (waits up to 120 s for the agents to report trust 100)"
bash "$ROOT/scripts/preflight.sh" --wait 120; RC=$?
echo
echo "Now restart the laptop dashboard (Ctrl-C the window running scripts/laptop-dashboard.sh, then run it again) so the Security Alerts list is empty."
exit $RC
