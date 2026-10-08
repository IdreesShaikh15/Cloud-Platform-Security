#!/usr/bin/env bash
# verify-webhook.sh - prove, on a REAL cluster, that the quorum-certificate admission webhook stops a
# single agent from acting alone.
#
# It impersonates the agents' service account (kubectl --as) and attempts the things a compromised
# agent might try, ALL AS SERVER-SIDE DRY RUNS: the API server runs authentication, RBAC and the
# admission webhook, but NOTHING is ever stored. Each attempt must be DENIED BY THE WEBHOOK
# (not merely by RBAC, which would leave the webhook unproven).
#
#   1. isolate a workload with NO certificate                      -> denied
#   2. isolate a workload with a FORGED certificate                -> denied
#   3. create a NetworkPolicy that is not an isolation policy      -> denied
#   4. patch a workload's replicas / image with NO certificate     -> denied
#   5. delete a workload                                           -> denied
#   6. control: the same request as a cluster ADMIN (not an enforced principal) is allowed, which also
#      proves the webhook is reachable and answering
#   7. (--test-failsafe) with the webhook scaled to 0 the agent's request is STILL refused (failurePolicy: Fail)
#
# Needs: kubectl with admin rights (it uses --as), the repo's Python environment (for the forged certificate).
# Exit codes: 0 all PASS | 1 a check FAILED | 2 usage / preflight error | 3 --test-failsafe could not restore the webhook
set -u -o pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECTL="${KUBECTL:-kubectl}"; PYTHON="${PYTHON:-python3}"
NS=""; TARGET=""; RES_NS="resilience"; FAILSAFE=0; CONTEXT=""
usage() { echo "usage: $0 --namespace NS --target WORKLOAD [--resilience-namespace NS] [--context CTX] [--test-failsafe]" >&2; }
while [ $# -gt 0 ]; do case "$1" in
  --namespace) NS="${2:-}"; shift 2 ;; --target) TARGET="${2:-}"; shift 2 ;;
  --resilience-namespace) RES_NS="${2:-}"; shift 2 ;; --context) CONTEXT="${2:-}"; shift 2 ;;
  --test-failsafe) FAILSAFE=1; shift ;; -h|--help) usage; exit 0 ;;
  *) echo "unknown argument: $1" >&2; usage; exit 2 ;; esac; done
[ -n "$NS" ] && [ -n "$TARGET" ] || { usage; exit 2; }
AS="system:serviceaccount:$RES_NS:resilience-agent"
kc() { "$KUBECTL" ${CONTEXT:+--context "$CONTEXT"} --request-timeout=30s "$@"; }

PASSES=0; FAILS=0
pass() { PASSES=$((PASSES + 1)); echo "[PASS] $*"; }
fail() { FAILS=$((FAILS + 1)); echo "[FAIL] $*"; }
RESTORE_REPLICAS=""
cleanup() {
  trap - EXIT INT TERM
  if [ -n "$RESTORE_REPLICAS" ]; then
    if kc -n "$RES_NS" scale deploy/quorum-webhook --replicas="$RESTORE_REPLICAS" >/dev/null 2>&1; then
      echo "[info] quorum-webhook restored to $RESTORE_REPLICAS replica(s)"
    else
      echo "[FAIL] COULD NOT RESTORE quorum-webhook: run: kubectl -n $RES_NS scale deploy/quorum-webhook --replicas=$RESTORE_REPLICAS"; exit 3
    fi
  fi
  echo; echo "=== summary: $PASSES passed, $FAILS failed ==="
  if [ -n "${ABORT_RC:-}" ]; then exit "$ABORT_RC"; fi
  if [ "$FINISHED" != 1 ]; then echo "[FAIL] the script stopped before finishing all checks"; exit 1; fi
  [ "$FAILS" -eq 0 ] && exit 0 || exit 1
}
FINISHED=0; ABORT_RC=""
pre_fail() { echo "[FAIL] preflight: $*"; ABORT_RC=2; exit 2; }
trap cleanup EXIT; trap 'FAILS=$((FAILS + 1)); exit 130' INT TERM

echo "=== verify-webhook: namespace '$NS', workload '$TARGET' (server-side dry runs only, nothing is stored) ==="
kc get namespace "$NS" >/dev/null 2>&1 || pre_fail "namespace '$NS' not found"
kc -n "$NS" get deployment "$TARGET" >/dev/null 2>&1 || pre_fail "deployment '$TARGET' not found in '$NS'"
kc get validatingwebhookconfiguration cr-quorum-webhook >/dev/null 2>&1 || \
  pre_fail "ValidatingWebhookConfiguration cr-quorum-webhook is not installed (run scripts/deploy.sh)"

policy_json() {  # $1 = certificate annotation value or "" ; $2 = policy name
  RESNS="$RES_NS" NS="$NS" TARGET="$TARGET" QC="$1" NAME="$2" PYTHONPATH="$ROOT/agent" "$PYTHON" - <<'PY'
import json, os
from resilience.reintegration import network_policy
ann = {"resilience.io/epoch": "1", "resilience.io/authorized-by": "verify-webhook.sh DRY RUN"}
if os.environ["QC"]:
    ann["resilience.io/qc"] = os.environ["QC"]
pol = network_policy(os.environ["TARGET"], "QUARANTINE", os.environ["NS"], os.environ["RESNS"], ann)
pol["metadata"]["name"] = os.environ["NAME"]
print(json.dumps(pol))
PY
}
forged_cert() {
  PYTHONPATH="$ROOT/agent" TARGET="$TARGET" "$PYTHON" - <<'PY'
import os, time
from resilience.certificate import Certificate, Statement
st = Statement(os.environ["TARGET"], "A", "CONTAIN", "", 1, "0" * 64, int((time.time() + 600) * 1000))
print(Certificate(st.canonical(), [(n, b"\x00" * 64) for n in "ABC"]).to_b64())
PY
}

# expect_denied DESCRIPTION INPUT-OR-EMPTY kubectl-args...
expect_denied() {
  local desc="$1" input="$2"; shift 2
  local out rc
  if [ -n "$input" ]; then out="$(printf '%s' "$input" | kc "$@" 2>&1)"; rc=$?; else out="$(kc "$@" 2>&1)"; rc=$?; fi
  if [ $rc -eq 0 ]; then fail "$desc: the request was ALLOWED (it would have been stored without a dry run!)"
  elif printf '%s' "$out" | grep -q "cr-quorum-webhook"; then pass "$desc: denied by the webhook ($(printf '%s' "$out" | sed -n 's/.*cr-quorum-webhook: //p' | head -1 | cut -c1-110))"
  else fail "$desc: refused, but NOT by the webhook (so the webhook is unproven): $(printf '%s' "$out" | head -1 | cut -c1-160)"; fi
}

POL="resilience-isolate-$TARGET"
expect_denied "1 isolate $TARGET with NO certificate" "$(policy_json "" "$POL")" \
  --as="$AS" create --dry-run=server -f -
expect_denied "2 isolate $TARGET with a FORGED certificate" "$(policy_json "$(forged_cert)" "$POL")" \
  --as="$AS" create --dry-run=server -f -
expect_denied "3 create a policy that is not an isolation policy" "$(policy_json "" "allow-everything-demo")" \
  --as="$AS" create --dry-run=server -f -
expect_denied "4a scale $TARGET to 0 replicas with NO certificate" "" \
  --as="$AS" -n "$NS" patch deployment "$TARGET" --dry-run=server -p '{"spec":{"replicas":0}}'
expect_denied "4b change $TARGET's image with NO certificate" "" \
  --as="$AS" -n "$NS" patch deployment "$TARGET" --dry-run=server \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"app","image":"cr-healthcare-app:known-good"}]}}}}'
expect_denied "5 delete $TARGET" "" --as="$AS" -n "$NS" delete deployment "$TARGET" --dry-run=server

# control: an admin is not an enforced principal; this also proves the webhook answers
if policy_json "" "$POL" | kc create --dry-run=server -f - >/dev/null 2>&1; then
  pass "6 control: the same uncertified request as a cluster ADMIN is allowed (admins are outside the rule; webhook is reachable)"
else
  fail "6 control: an admin dry run failed; the webhook may be unreachable (failurePolicy Fail also blocks admins while it is down)"
fi

if [ "$FAILSAFE" = 1 ]; then
  RESTORE_REPLICAS="$(kc -n "$RES_NS" get deploy/quorum-webhook -o 'jsonpath={.spec.replicas}' 2>/dev/null)"
  [ -n "$RESTORE_REPLICAS" ] || { fail "7 cannot read the webhook's replica count"; exit 1; }
  echo "[info] scaling quorum-webhook to 0 for a few seconds (restored automatically)"
  kc -n "$RES_NS" scale deploy/quorum-webhook --replicas=0 >/dev/null 2>&1
  sleep 8
  out="$(policy_json "" "$POL" | kc --as="$AS" create --dry-run=server -f - 2>&1)"; rc=$?
  if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qiE "webhook|failed calling|no endpoints"; then
    pass "7 webhook DOWN: the agent's request is still refused (failurePolicy Fail): $(printf '%s' "$out" | head -1 | cut -c1-120)"
  else fail "7 webhook DOWN: the request was not refused by the failure policy: $(printf '%s' "$out" | head -1 | cut -c1-140)"; fi
fi
FINISHED=1
