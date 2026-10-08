#!/usr/bin/env bash
# verify-rbac.sh - on a REAL cluster, check the agents' service account has exactly the permissions it
# needs and nothing more. Read-only: it only asks the API server `kubectl auth can-i` (nothing changes).
#
#   scripts/verify-rbac.sh --namespace healthcare --target records-api
#
# Exit codes: 0 all PASS | 1 a check failed | 2 usage / preflight error
set -u -o pipefail
KUBECTL="${KUBECTL:-kubectl}"
NS=""; TARGET=""; RES_NS="resilience"; SA="resilience-agent"; CONTEXT=""
usage() { echo "usage: $0 --namespace NS --target WORKLOAD [--resilience-namespace NS] [--service-account NAME] [--context CTX]" >&2; }
while [ $# -gt 0 ]; do case "$1" in
  --namespace) NS="${2:-}"; shift 2 ;; --target) TARGET="${2:-}"; shift 2 ;;
  --resilience-namespace) RES_NS="${2:-}"; shift 2 ;; --service-account) SA="${2:-}"; shift 2 ;;
  --context) CONTEXT="${2:-}"; shift 2 ;; -h|--help) usage; exit 0 ;; *) usage; exit 2 ;; esac; done
[ -n "$NS" ] && [ -n "$TARGET" ] || { usage; exit 2; }
AS="system:serviceaccount:$RES_NS:$SA"
kc() { "$KUBECTL" ${CONTEXT:+--context "$CONTEXT"} --request-timeout=20s "$@"; }
kc get namespace "$NS" >/dev/null 2>&1 || { echo "[FAIL] namespace '$NS' not found"; exit 2; }
PASSES=0; FAILS=0
check() {  # want(yes|no) description  -n ns verb resource[/name]
  local want="$1" desc="$2"; shift 2
  local got; got="$(kc auth can-i --as="$AS" "$@" 2>/dev/null | tail -1)"
  if [ "$got" = "$want" ]; then PASSES=$((PASSES + 1)); echo "[PASS] $desc -> $got"
  else FAILS=$((FAILS + 1)); echo "[FAIL] $desc -> got '$got', expected '$want'"; fi
}
POL="networkpolicies.networking.k8s.io/resilience-isolate-$TARGET"
echo "=== verify-rbac: $AS ==="
echo "--- must be ALLOWED (everything the platform really does) ---"
check yes "create NetworkPolicy in $NS"                    -n "$NS" create networkpolicies.networking.k8s.io
check yes "get / update / delete its own isolation policy" -n "$NS" get "$POL"
check yes "  update isolation policy"                      -n "$NS" update "$POL"
check yes "  delete isolation policy"                      -n "$NS" delete "$POL"
check yes "get workload deployment"                        -n "$NS" get "deployments.apps/$TARGET"
check yes "patch workload deployment"                      -n "$NS" patch "deployments.apps/$TARGET"
check yes "read the attack-marker ConfigMap"               -n "$RES_NS" get configmaps/cr-attack-marker
echo "--- must be DENIED (everything else) ---"
check no  "list pods in $NS"                               -n "$NS" list pods
check no  "get pods in $NS"                                -n "$NS" get pods
check no  "delete the workload deployment"                 -n "$NS" delete "deployments.apps/$TARGET"
check no  "create a deployment"                            -n "$NS" create deployments.apps
check no  "patch the client deployment"                    -n "$NS" patch deployments.apps/client
check no  "update some other NetworkPolicy"                -n "$NS" update networkpolicies.networking.k8s.io/allow-all
check no  "delete some other NetworkPolicy"                -n "$NS" delete networkpolicies.networking.k8s.io/allow-all
check no  "patch (not update) the isolation policy"        -n "$NS" patch "$POL"
check no  "list NetworkPolicies"                           -n "$NS" list networkpolicies.networking.k8s.io
check no  "read secrets in $NS"                            -n "$NS" get secrets
check no  "read the platform config ConfigMap"             -n "$RES_NS" get configmaps/resilience-config
check no  "read the agents' signing-key Secrets"           -n "$RES_NS" get secrets/agent-a-signing
check no  "list ConfigMaps in $RES_NS"                     -n "$RES_NS" list configmaps
check no  "read pods in kube-system"                       -n kube-system get pods
check no  "list nodes (cluster scope)"                     list nodes
echo; echo "=== summary: $PASSES passed, $FAILS failed ==="
[ "$FAILS" -eq 0 ] && exit 0 || exit 1
