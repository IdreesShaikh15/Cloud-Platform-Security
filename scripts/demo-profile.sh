#!/usr/bin/env bash
# TIMING PROFILE for the LIVE cluster (docs/DEMO-PROFILE.md). One switch, three words:
#
#   bash scripts/demo-profile.sh status                 # which profile the cluster is on right now (read-only)
#   bash scripts/demo-profile.sh presentation           # shorter stage dwell, faster workload-trust recovery, 2 s pod shutdown wait
#   bash scripts/demo-profile.sh default                # back to the shipped timing
#   bash scripts/demo-profile.sh presentation --dry-run # print every command, change nothing
#
# It changes ONLY what config/timing-profiles.json lists:
#   resilience-config:  timers.stage_dwell_s, trust.workload_recover_per_s
#   healthcare Deployments (the six workloads, not the client):  spec.template.spec.terminationGracePeriodSeconds
# Nothing about the quorum, trust updates, certificates, webhook, RBAC or any agent code. In this order, so nothing reacts to a half-changed state:
#   1. stop the four agents (scale to 0)   2. set the pod shutdown wait and let the six workloads roll   3. write the configuration
#   4. start the agents again (fresh pods: trust 100, empty alert history) and restart the in-cluster dashboard
# Takes about 1-2 minutes. Run it BEFORE scripts/preflight.sh, never during a demo. scripts/deploy.sh / upgrade-app.sh re-apply the shipped
# configuration: run this again afterwards. It refuses any kubectl context other than $EXPECT_CONTEXT (default cr-platform).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KC="${KUBECTL:-kubectl}"; EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"; DRY=0
PY="${PYTHON:-python3}"; TP="$ROOT/scripts/timing_profile.py"
AGENTS=(agent-a agent-b agent-c agent-d)
usage() { sed -n 2,5p "$0" >&2; echo "usage: $0 status|default|presentation [--dry-run]" >&2; exit 2; }
case "${1:-}" in
  status|default|presentation) MODE="$1" ;;
  -h|--help) sed -n 2,19p "$0"; exit 0 ;;
  *) usage ;;
esac
case "${2:-}" in "") ;; --dry-run) DRY=1 ;; *) usage ;; esac
run() { if [ "$DRY" = 1 ]; then echo "   [dry-run] $*"; else "$@"; fi; }
step() { printf '\n>> %s\n' "$1"; }

CTX="$($KC config current-context 2>/dev/null || true)"
if [ "$CTX" != "$EXPECT_CONTEXT" ]; then
  echo "refused: kubectl context is '${CTX:-none}', expected '$EXPECT_CONTEXT'. Run: kubectl config use-context $EXPECT_CONTEXT" >&2; exit 3
fi
mapfile -t NAMES < <($PY "$ROOT/scripts/gen-workloads.py" --names 2>/dev/null)
[ "${#NAMES[@]}" -ge 5 ] || { echo "cannot read the workload names from config/workloads.json" >&2; exit 2; }
WORK=("${NAMES[@]:0:$(( ${#NAMES[@]} - 1 ))}")          # every name but the last (the client probe)

current_config() { $KC -n resilience get configmap resilience-config -o jsonpath='{.data.config\.json}' 2>/dev/null; }

if [ "$MODE" = status ]; then
  CFG="$(current_config)"
  if [ -z "$CFG" ]; then echo "cannot read the resilience-config ConfigMap (is the cluster up and the context $EXPECT_CONTEXT?)" >&2; exit 1; fi
  PROFILE="$(printf '%s' "$CFG" | $PY "$TP" detect)" || exit 1
  echo "timing profile now: $PROFILE"
  printf '%s' "$CFG" | $PY -c 'import json,sys; c=json.load(sys.stdin); print("  stage_dwell_s            =", c["timers"].get("stage_dwell_s")); print("  workload_recover_per_s   =", c["trust"].get("workload_recover_per_s"))'
  for d in "${WORK[@]}"; do
    g="$($KC -n healthcare get deploy "$d" -o jsonpath='{.spec.template.spec.terminationGracePeriodSeconds}' 2>/dev/null)"
    echo "  $d: pod shutdown wait = ${g:-30 (Kubernetes default)}"
  done
  exit 0
fi

GRACE="$($PY "$TP" grace "$MODE")" || exit 2
if [ "$GRACE" = default ]; then GPATCH='{"spec":{"template":{"spec":{"terminationGracePeriodSeconds":null}}}}'
else GPATCH="{\"spec\":{\"template\":{\"spec\":{\"terminationGracePeriodSeconds\":$GRACE}}}}"; fi
echo "context $CTX; profile: $MODE; workloads: ${WORK[*]}"; [ "$DRY" = 1 ] && echo "(dry run: nothing will be changed)"

step "1/4 stopping the four agents so nothing reacts while the workloads restart"
run $KC -n resilience scale deploy "${AGENTS[@]}" --replicas=0
run $KC -n resilience wait --for=delete pod -l app=resilience-agent --timeout=90s

step "2/4 setting the pod shutdown wait to $GRACE on the six workloads"
for d in "${WORK[@]}"; do run $KC -n healthcare patch deploy "$d" --type merge -p "$GPATCH"; done
run $KC -n healthcare rollout status deploy --timeout=180s

step "3/4 writing the timing keys into the resilience-config ConfigMap (nothing else in it changes)"
if [ "$DRY" = 1 ]; then echo "   [dry-run] read config.json, set only the keys of profile '$MODE' (python3 scripts/timing_profile.py apply $MODE), patch the ConfigMap"
else
  CFG="$(current_config)"
  [ -n "$CFG" ] || { echo "cannot read the resilience-config ConfigMap; the agents stay stopped. Fix the cluster, then run: bash scripts/demo-reset.sh" >&2; exit 1; }
  NEW="$(printf '%s' "$CFG" | $PY "$TP" apply "$MODE")" || exit 1
  PATCH="$(printf '%s' "$NEW" | $PY -c 'import json,sys; print(json.dumps({"data": {"config.json": sys.stdin.read()}}))')"
  $KC -n resilience patch configmap resilience-config --type merge -p "$PATCH" || { echo "could not write the ConfigMap" >&2; exit 1; }
fi

step "4/4 starting the agents again (fresh pods: trust 100, empty alert history) and restarting the in-cluster dashboard"
run $KC -n resilience scale deploy "${AGENTS[@]}" --replicas=1
for a in "${AGENTS[@]}"; do run $KC -n resilience rollout status "deploy/$a" --timeout=180s; done
run $KC -n resilience rollout restart deploy/dashboard
run $KC -n resilience rollout status deploy/dashboard --timeout=180s

echo
if [ "$DRY" = 1 ]; then echo "dry run finished: nothing was changed."; exit 0; fi
echo "profile '$MODE' is active. Next: bash scripts/preflight.sh --wait 120   (then restart scripts/laptop-dashboard.sh so its alert list starts empty)"
