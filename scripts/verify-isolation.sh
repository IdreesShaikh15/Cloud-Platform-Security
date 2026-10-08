#!/usr/bin/env bash
# verify-isolation.sh - check, on a REAL cluster, that the platform's isolation
# NetworkPolicy really blocks what it should and still allows what it must.
#
# What it does (to ONE workload, in ONE namespace, nothing else):
#   1. checks everything you named exists (it never guesses a name),
#   2. measures benign HTTP GET reachability BEFORE isolation,
#   3. creates the platform's isolation policy for that one workload,
#   4. measures again: who is blocked, who is still allowed,
#   5. removes ONLY the policy it created, and measures a third time.
# It uses only HTTP GET /health requests and DNS lookups between demo pods.
#
# Safety:
#   * it refuses to start if a policy with the same name already exists, and it
#     never deletes a policy it did not create (it checks its own run-id label);
#   * a cleanup trap removes what it created on exit, Ctrl-C, SIGTERM or error;
#   * every step prints PASS/FAIL; exit code is non-zero on ANY failure,
#     including a failed cleanup.
#
# Exit codes: 0 all PASS | 1 at least one check FAILED | 2 usage / preflight error
#             (nothing was changed) | 3 CLEANUP FAILED (a policy may be left behind!)
#
# The checks briefly cut the target workload off from users (that is the point),
# so run it on a demo cluster, while no real incident is in progress.
#
# Example (see docs/AUDIT.md section 3 for how to find the pod names):
#   scripts/verify-isolation.sh --namespace healthcare --client-pod client-7d9f6c-abcde \
#       --target records-api --agent-pod agent-c-6c7d8-xyz12 --egress-peer auth-service
set -u -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECTL="${KUBECTL:-kubectl}"
PYTHON="${PYTHON:-python3}"

NS=""; CLIENT_POD=""; TARGET=""; STAGE="QUARANTINE"
AGENT_POD=""; AGENT_NS="resilience"; SKIP_AGENT=0
EGRESS_PEER=""; SKIP_EGRESS=0; PEER_IP=""; PEER_PORT=""
CONTEXT=""; REQ_TIMEOUT=4; SETTLE=6; DEADLINE=40; HOLD=8; PRINT_ONLY=0

usage() {
  cat >&2 <<'EOF'
usage: verify-isolation.sh --namespace NS --client-pod POD --target WORKLOAD
                           (--agent-pod POD | --skip-agent-check)
                           (--egress-peer SERVICE | --skip-egress-check)
                           [--agent-namespace NS]   (default: resilience)
                           [--stage QUARANTINE|RESTRICTED|MONITORED|PEER_VALIDATED]
                           [--context KUBE_CONTEXT] [--request-timeout SECS]
                           [--settle SECS] [--deadline SECS] [--hold SECS] [--print-policy]

  --namespace      namespace of the workload (normally "healthcare")
  --client-pod     a pod in that namespace that is NOT isolated, used as the "user"
                   (the synthetic client pod; name from `kubectl -n healthcare get pods`)
  --target         the workload (Deployment/Service name, label app=<name>) to isolate
  --agent-pod      one resilience agent pod: checks the monitoring/validation path is
                   still allowed while isolated   (kubectl -n resilience get pods)
  --egress-peer    a Service in the same namespace the target would call: checks the
                   target's own outbound traffic (egress) and DNS
  --skip-*         explicitly skip a check (it is then reported as SKIPPED)
  --stage          which stage's policy to test (default QUARANTINE)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NS="${2:-}"; shift 2 ;;
    --client-pod) CLIENT_POD="${2:-}"; shift 2 ;;
    --target) TARGET="${2:-}"; shift 2 ;;
    --stage) STAGE="${2:-}"; shift 2 ;;
    --agent-pod) AGENT_POD="${2:-}"; shift 2 ;;
    --agent-namespace) AGENT_NS="${2:-}"; shift 2 ;;
    --skip-agent-check) SKIP_AGENT=1; shift ;;
    --egress-peer) EGRESS_PEER="${2:-}"; shift 2 ;;
    --skip-egress-check) SKIP_EGRESS=1; shift ;;
    --context) CONTEXT="${2:-}"; shift 2 ;;
    --request-timeout) REQ_TIMEOUT="${2:-}"; shift 2 ;;
    --settle) SETTLE="${2:-}"; shift 2 ;;
    --deadline) DEADLINE="${2:-}"; shift 2 ;;
    --hold) HOLD="${2:-}"; shift 2 ;;
    --print-policy) PRINT_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

die_usage() { echo "ERROR: $*" >&2; usage; exit 2; }
[ -n "$NS" ] || die_usage "--namespace is required"
[ -n "$CLIENT_POD" ] || die_usage "--client-pod is required"
[ -n "$TARGET" ] || die_usage "--target is required"
case "$STAGE" in QUARANTINE|RESTRICTED|MONITORED|PEER_VALIDATED) ;;
  *) die_usage "--stage must be QUARANTINE, RESTRICTED, MONITORED or PEER_VALIDATED" ;; esac
if [ "$SKIP_AGENT" = 0 ] && [ -z "$AGENT_POD" ]; then
  die_usage "give --agent-pod, or say --skip-agent-check (the monitoring path is the most important check)"; fi
if [ "$SKIP_EGRESS" = 0 ] && [ -z "$EGRESS_PEER" ]; then
  die_usage "give --egress-peer, or say --skip-egress-check"; fi
[ "$EGRESS_PEER" != "$TARGET" ] || die_usage "--egress-peer must be a different service than --target"

RUN_ID="verify-$(date +%s)-$$"
POLICY_NAME="resilience-isolate-$TARGET"       # same name the platform's agents use

# --------------------------------------------------------------------------- policy
render_policy() {
  RUN_ID="$RUN_ID" TARGET="$TARGET" STAGE="$STAGE" NS="$NS" RES_NS="$AGENT_NS" \
  PYTHONPATH="$ROOT/agent" "$PYTHON" - <<'PY'
import json, os
from resilience.reintegration import network_policy   # the platform's own generator
ann = {"resilience.io/created-by": "verify-isolation.sh",
       "resilience.io/verify-run": os.environ["RUN_ID"],
       "resilience.io/authorized-by": "MANUAL TEST (no quorum)"}
pol = network_policy(os.environ["TARGET"], os.environ["STAGE"], os.environ["NS"],
                     os.environ["RES_NS"], ann)
pol["metadata"]["labels"]["resilience.io/verify-run"] = os.environ["RUN_ID"]
print(json.dumps(pol))
PY
}

if [ "$PRINT_ONLY" = 1 ]; then render_policy; exit $?; fi

# --------------------------------------------------------------------------- helpers
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
TIMEOUT_FLAGS=""
# --foreground keeps Ctrl-C working: without it a wrapped kubectl ignores the terminal's
# SIGINT and the cleanup trap would only run after that call finishes.
if [ -n "$TIMEOUT_BIN" ] && "$TIMEOUT_BIN" --foreground 1 true >/dev/null 2>&1; then TIMEOUT_FLAGS="--foreground"; fi
run_to() { local s="$1"; shift; if [ -n "$TIMEOUT_BIN" ]; then "$TIMEOUT_BIN" $TIMEOUT_FLAGS "$s" "$@"; else "$@"; fi; }
kc() {  # kubectl with a hard request timeout
  if [ -n "$CONTEXT" ]; then run_to 60 "$KUBECTL" --context "$CONTEXT" --request-timeout=20s "$@"
  else run_to 60 "$KUBECTL" --request-timeout=20s "$@"; fi
}

PASSES=0; FAILS=0; SKIPS=0
pass() { PASSES=$((PASSES + 1)); echo "[PASS] $*"; }
fail() { FAILS=$((FAILS + 1)); echo "[FAIL] $*"; }
skip() { SKIPS=$((SKIPS + 1)); echo "[SKIP] $*"; }
info() { echo "[info] $*"; }

# In-pod probes. They print one line "PROBE=reachable|blocked|error ..." so that a
# kubectl failure (no marker) is never mistaken for "blocked".
PY_HTTP='
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2]))
    print("PROBE=reachable code=%d" % r.status)
except urllib.error.HTTPError as e:
    print("PROBE=reachable code=%d" % e.code)
except Exception as e:
    print("PROBE=blocked reason=%s" % type(e).__name__)
'
SH_HTTP='
py="$1"; url="$2"; t="$3"
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1; then exec "$p" -c "$py" "$url" "$t"; fi
done
if command -v wget >/dev/null 2>&1; then
  if wget -q -T "$t" -O /dev/null "$url"; then echo "PROBE=reachable code=200"
  else echo "PROBE=blocked reason=wget"; fi; exit 0
fi
if command -v curl >/dev/null 2>&1; then
  if curl -sf -m "$t" -o /dev/null "$url"; then echo "PROBE=reachable code=200"
  else echo "PROBE=blocked reason=curl"; fi; exit 0
fi
echo "PROBE=error reason=no-http-client-in-pod"
'
PY_DNS='
import signal, socket, sys
def _h(*a): raise TimeoutError("dns timeout")
signal.signal(signal.SIGALRM, _h); signal.alarm(int(sys.argv[2]))
try:
    socket.getaddrinfo(sys.argv[1], None)
    print("PROBE=reachable resolved")
except Exception as e:
    print("PROBE=blocked reason=%s" % type(e).__name__)
'
SH_DNS='
py="$1"; name="$2"; t="$3"
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1; then exec "$p" -c "$py" "$name" "$t"; fi
done
echo "PROBE=error reason=no-python-in-pod"
'

PROBE_STATE=""; PROBE_DETAIL=""
_classify() {
  local line; line="$(printf '%s\n' "$1" | grep '^PROBE=' | tail -1)"
  case "$line" in
    PROBE=reachable*) PROBE_STATE=reachable ;;
    PROBE=blocked*) PROBE_STATE=blocked ;;
    *) PROBE_STATE=error ;;
  esac
  PROBE_DETAIL="${line:-no probe output: $(printf '%s' "$1" | tr '\n' ' ' | cut -c1-200)}"
}
probe_http() {  # POD NAMESPACE URL
  local out; out="$(run_to $((REQ_TIMEOUT + 20)) "$KUBECTL" ${CONTEXT:+--context "$CONTEXT"} \
      -n "$2" exec "$1" -- sh -c "$SH_HTTP" sh "$PY_HTTP" "$3" "$REQ_TIMEOUT" 2>&1)"
  _classify "$out"
}
probe_dns() {   # POD NAMESPACE HOSTNAME
  local out; out="$(run_to 60 "$KUBECTL" ${CONTEXT:+--context "$CONTEXT"} \
      -n "$2" exec "$1" -- sh -c "$SH_DNS" sh "$PY_DNS" "$3" 6 2>&1)"
  _classify "$out"
}

# Poll until the probe shows the wanted state (policy programming takes a moment).
# $1 = description, $2 = wanted state, remaining = probe command + args
expect() {
  local desc="$1" want="$2"; shift 2
  local end=$(( $(date +%s) + DEADLINE ))
  while :; do
    "$@"
    if [ "$PROBE_STATE" = "$want" ]; then pass "$desc  [$PROBE_DETAIL]"; return 0; fi
    if [ "$PROBE_STATE" = "error" ]; then fail "$desc  - could not run the probe: $PROBE_DETAIL"; return 1; fi
    [ "$(date +%s)" -lt "$end" ] || break
    sleep 2
  done
  fail "$desc  - expected $want but saw $PROBE_STATE [$PROBE_DETAIL]"
  return 1
}

# What each stage's policy should do (see agent/resilience/reintegration.py).
want_client_to_target() { case "$STAGE" in QUARANTINE|RESTRICTED) echo blocked ;; *) echo reachable ;; esac; }
want_target_egress()    { case "$STAGE" in QUARANTINE) echo blocked ;; *) echo reachable ;; esac; }

# --------------------------------------------------------------------------- cleanup
CREATED=0; CLEANED=0; CLEANUP_FAILED=0; INTERRUPTED=0; FINISHED=0
policy_run_id() {  # prints the run-id label of the policy, "<none>" if missing, "<absent>" if not found
  local out rc
  out="$(kc -n "$NS" get networkpolicy "$POLICY_NAME" -o "jsonpath={.metadata.labels.resilience\.io/verify-run}" 2>&1)"; rc=$?
  if [ $rc -ne 0 ]; then
    case "$out" in *NotFound*|*"not found"*) echo "<absent>"; return 0 ;; esac
    echo "<error: $out>"; return 1
  fi
  echo "${out:-<none>}"
}

cleanup() {
  trap - EXIT INT TERM HUP
  if [ "$CREATED" = 1 ] && [ "$CLEANED" = 0 ]; then
    local rid attempt
    echo "--- cleanup: removing the policy this run created ---"
    for attempt in 1 2 3; do
      rid="$(policy_run_id)"
      case "$rid" in
        "<absent>") echo "[info] policy $POLICY_NAME is already gone"; CLEANED=1; break ;;
        "$RUN_ID")
          if kc -n "$NS" delete networkpolicy "$POLICY_NAME" --wait=true >/dev/null 2>&1; then
            [ "$(policy_run_id)" = "<absent>" ] && { CLEANED=1; break; }
          fi ;;
        "<error:"*) : ;;                       # API hiccup: retry
        *) echo "[FAIL] policy $POLICY_NAME now has run-id '$rid', not ours ($RUN_ID); leaving it UNTOUCHED"
           CLEANUP_FAILED=1; break ;;
      esac
      sleep 2
    done
    if [ "$CLEANED" = 1 ]; then echo "[PASS] cleanup: policy $POLICY_NAME removed (cluster back to its original state)"
    else
      CLEANUP_FAILED=1
      echo "[FAIL] cleanup: could not confirm removal of policy $POLICY_NAME in namespace $NS."
      echo "       Remove it by hand ONLY if it carries label resilience.io/verify-run=$RUN_ID:"
      echo "       kubectl -n $NS get networkpolicy $POLICY_NAME --show-labels"
      echo "       kubectl -n $NS delete networkpolicy $POLICY_NAME"
    fi
  fi
  echo
  echo "=== summary: $PASSES passed, $FAILS failed, $SKIPS skipped$([ "$CLEANUP_FAILED" = 1 ] && echo ', CLEANUP FAILED') ==="
  if [ "$CLEANUP_FAILED" = 1 ]; then exit 3; fi
  if [ "$INTERRUPTED" = 1 ]; then echo "interrupted"; exit 130; fi
  if [ "${ABORT_RC:-}" != "" ]; then exit "$ABORT_RC"; fi
  if [ "$FINISHED" != 1 ]; then
    # The script died before reaching its normal end (a bug or an unexpected error):
    # never report success in that case.
    echo "[FAIL] the script aborted unexpectedly before finishing all steps"; exit 1
  fi
  [ "$FAILS" -eq 0 ] && exit 0 || exit 1
}
trap cleanup EXIT
trap 'INTERRUPTED=1; FAILS=$((FAILS + 1)); exit 130' INT TERM HUP

preflight_fail() { echo "[FAIL] preflight: $*"; ABORT_RC=2; exit 2; }

# --------------------------------------------------------------------------- 1. preflight
echo "=== verify-isolation: stage $STAGE of workload '$TARGET' in namespace '$NS' (run $RUN_ID) ==="
command -v "$KUBECTL" >/dev/null 2>&1 || preflight_fail "'$KUBECTL' not found"
command -v "$PYTHON" >/dev/null 2>&1 || preflight_fail "'$PYTHON' not found (needed to render the policy)"
kc version --client >/dev/null 2>&1 || preflight_fail "kubectl does not work"
info "kubectl context: $( (kc config current-context 2>/dev/null) || echo unknown)"
kc get namespace "$NS" >/dev/null 2>&1 || preflight_fail "namespace '$NS' not found"
pod_phase() { kc -n "$1" get pod "$2" -o "jsonpath={.status.phase}" 2>/dev/null; }
[ "$(pod_phase "$NS" "$CLIENT_POD")" = "Running" ] || preflight_fail "client pod '$CLIENT_POD' not found or not Running in '$NS'"
kc -n "$NS" get deployment "$TARGET" >/dev/null 2>&1 || preflight_fail "deployment '$TARGET' not found in '$NS'"
SVC_PORT="$(kc -n "$NS" get service "$TARGET" -o "jsonpath={.spec.ports[0].port}" 2>/dev/null)"
[ -n "$SVC_PORT" ] || preflight_fail "service '$TARGET' not found in '$NS'"
TARGET_POD="$(kc -n "$NS" get pods -l "app=$TARGET" --field-selector=status.phase=Running \
              -o "jsonpath={.items[0].metadata.name}" 2>/dev/null)"
[ -n "$TARGET_POD" ] || preflight_fail "no Running pod with label app=$TARGET in '$NS' (the policy selects pods by that label)"
[ "$TARGET_POD" != "$CLIENT_POD" ] || preflight_fail "the client pod must not be the target workload"
PHASE_ANN="$(kc -n "$NS" get deployment "$TARGET" -o "jsonpath={.metadata.annotations.resilience\.io/phase}" 2>/dev/null)"
case "${PHASE_ANN:-HEALTHY}" in HEALTHY) ;;
  *) preflight_fail "the platform is handling an incident on '$TARGET' (phase $PHASE_ANN); not touching it" ;; esac
if [ "$SKIP_AGENT" = 0 ]; then
  [ "$(pod_phase "$AGENT_NS" "$AGENT_POD")" = "Running" ] || preflight_fail "agent pod '$AGENT_POD' not found or not Running in '$AGENT_NS'"
fi
if [ "$SKIP_EGRESS" = 0 ]; then
  PEER_IP="$(kc -n "$NS" get service "$EGRESS_PEER" -o "jsonpath={.spec.clusterIP}" 2>/dev/null)"
  PEER_PORT="$(kc -n "$NS" get service "$EGRESS_PEER" -o "jsonpath={.spec.ports[0].port}" 2>/dev/null)"
  [ -n "$PEER_IP" ] && [ -n "$PEER_PORT" ] || preflight_fail "service '$EGRESS_PEER' not found in '$NS'"
fi
# Never touch what is already there.
EXISTING="$(policy_run_id)"
[ "$EXISTING" = "<absent>" ] || preflight_fail "a NetworkPolicy named '$POLICY_NAME' ALREADY EXISTS in '$NS' (run-id: $EXISTING). Not touching it. (Is an isolation in progress?)"
info "existing NetworkPolicies in $NS (left untouched): $(kc -n "$NS" get networkpolicy -o name 2>/dev/null | tr '\n' ' ')"
pass "preflight: namespace, client pod, target '$TARGET' (pod $TARGET_POD, service port $SVC_PORT) all exist; no policy '$POLICY_NAME' yet"

TARGET_URL="http://$TARGET.$NS.svc.cluster.local:$SVC_PORT/health"
AGENT_TO_TARGET_URL="$TARGET_URL"
PEER_URL="http://$PEER_IP:${PEER_PORT:-0}/health"
PEER_DNS="$EGRESS_PEER.$NS.svc.cluster.local"

run_checks() {  # $1 = label, $2 = client->target wanted, $3 = egress wanted (blocked|reachable)
  local label="$1" wc="$2" we="$3"
  expect "$label: client pod -> $TARGET (user traffic, ingress) is $wc" "$wc" probe_http "$CLIENT_POD" "$NS" "$TARGET_URL"
  if [ "$SKIP_AGENT" = 0 ]; then
    expect "$label: agent pod -> $TARGET (monitor/validate path, ingress from agents) is reachable" reachable \
      probe_http "$AGENT_POD" "$AGENT_NS" "$AGENT_TO_TARGET_URL"
  else skip "$label: agent -> target path (--skip-agent-check)"; fi
  if [ "$SKIP_EGRESS" = 0 ]; then
    expect "$label: $TARGET pod -> $EGRESS_PEER by IP (its own outbound traffic, egress) is $we" "$we" \
      probe_http "$TARGET_POD" "$NS" "$PEER_URL"
    expect "$label: $TARGET pod DNS lookup of $PEER_DNS is $([ "$we" = blocked ] && echo blocked || echo allowed)" "$we" \
      probe_dns "$TARGET_POD" "$NS" "$PEER_DNS"
  else skip "$label: target egress + DNS (--skip-egress-check)"; fi
}

# --------------------------------------------------------------------------- 2. BEFORE
echo "--- step 1: BEFORE isolation: everything must be reachable ---"
run_checks "before" reachable reachable
if [ "$FAILS" -gt 0 ]; then
  echo "[FAIL] the baseline is already broken, so isolation results would be meaningless. Nothing was changed."
  ABORT_RC=1; exit 1
fi

# --------------------------------------------------------------------------- 3. APPLY
echo "--- step 2: apply the $STAGE isolation policy to '$TARGET' only ---"
POLICY_JSON="$(render_policy)" || preflight_fail "could not render the policy"
if printf '%s' "$POLICY_JSON" | kc create -f - >/dev/null 2>&1; then
  CREATED=1
  pass "created NetworkPolicy $POLICY_NAME (run-id $RUN_ID)"
else
  # It may exist now (race with an agent). Re-check: if it is not ours we must not touch it.
  rid="$(policy_run_id)"
  if [ "$rid" = "$RUN_ID" ]; then CREATED=1; fi
  fail "could not create NetworkPolicy $POLICY_NAME (current run-id: $rid)"
  ABORT_RC=1; exit 1
fi
info "waiting ${SETTLE}s for the CNI (Calico) to program the rules"
sleep "$SETTLE"

# --------------------------------------------------------------------------- 4. DURING
echo "--- step 3: DURING isolation ---"
run_checks "isolated" "$(want_client_to_target)" "$(want_target_egress)"
if [ "$STAGE" = MONITORED ] || [ "$STAGE" = PEER_VALIDATED ]; then
  info "note: at $STAGE every expected result is 'reachable', so this stage cannot by itself prove the policy is enforced; use QUARANTINE or RESTRICTED for that."
fi
info "holding ${HOLD}s, then checking the kubelet readiness probe is unaffected (recovery needs the new pod to become Ready)"
sleep "$HOLD"
NOT_READY="$(kc -n "$NS" get pods -l "app=$TARGET" -o "jsonpath={range .items[*]}{.metadata.name}={.status.containerStatuses[0].ready} {end}" 2>/dev/null | tr ' ' '\n' | grep -v '=true$' | grep -v '^$' || true)"
if [ -z "$NOT_READY" ]; then pass "isolated: all '$TARGET' pods are still Ready (kubelet probes are not blocked)"
else fail "isolated: pods not Ready while isolated: $NOT_READY (recovery would stall)"; fi

# --------------------------------------------------------------------------- 5. REMOVE
echo "--- step 4: remove ONLY the policy this run created ---"
rid="$(policy_run_id)"
if [ "$rid" != "$RUN_ID" ]; then
  fail "policy $POLICY_NAME no longer carries our run-id (now '$rid'); leaving it untouched"
  CLEANUP_FAILED=1; CREATED=0
else
  if kc -n "$NS" delete networkpolicy "$POLICY_NAME" --wait=true >/dev/null 2>&1 && [ "$(policy_run_id)" = "<absent>" ]; then
    CLEANED=1; pass "removed NetworkPolicy $POLICY_NAME (only the one this run created)"
  else
    fail "could not remove NetworkPolicy $POLICY_NAME (cleanup will retry)"
  fi
fi

# --------------------------------------------------------------------------- 6. AFTER
echo "--- step 5: AFTER removal: everything must be reachable again ---"
run_checks "after" reachable reachable
FINISHED=1
exit "$([ "$FAILS" -eq 0 ] && echo 0 || echo 1)"
