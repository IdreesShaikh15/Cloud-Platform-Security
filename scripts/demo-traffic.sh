#!/usr/bin/env bash
# Pause / resume the simulated Synthea clinician traffic (the `client` pod) so the laptop has less to do during attack demos.
#   bash scripts/demo-traffic.sh status
#   bash scripts/demo-traffic.sh pause          # 0 simulated clinicians: the client keeps only its availability probe
#   bash scripts/demo-traffic.sh resume         # back to the shipped 3 clinicians
#   bash scripts/demo-traffic.sh set N          # N clinicians (each is about one request every 1-3 seconds)
#   (add --dry-run to any of them to print the command and change nothing)
# It changes ONLY the TRAFFIC_CLINICIANS variable of deploy/client in the healthcare namespace (the pod restarts once, about 10 s). The four
# agents' decisions never depend on the client: it is not a monitored workload. While paused the Live charts show no requests and
# scripts/preflight.sh section 7 will FAIL ("no new requests"): resume before the final check.
set -uo pipefail
KC="${KUBECTL:-kubectl}"; EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"; DRY=0
ARGS=(); for a in "$@"; do if [ "$a" = "--dry-run" ]; then DRY=1; else ARGS+=("$a"); fi; done
usage() { sed -n 2,8p "$0" >&2; exit 2; }
CMD="${ARGS[0]:-}"; N=""
case "$CMD" in
  status) ;;
  pause) N=0 ;;
  resume) N=3 ;;
  set) N="${ARGS[1]:-}"; [[ "$N" =~ ^[0-9]+$ ]] && [ "$N" -le 20 ] || { echo "set needs a number from 0 to 20" >&2; exit 2; } ;;
  *) usage ;;
esac
CTX="$($KC config current-context 2>/dev/null || true)"
[ "$CTX" = "$EXPECT_CONTEXT" ] || { echo "refused: kubectl context is '${CTX:-none}', expected '$EXPECT_CONTEXT'" >&2; exit 3; }
if [ "$CMD" = status ]; then
  CUR="$($KC -n healthcare get deploy client -o jsonpath='{.spec.template.spec.containers[0].env}' 2>/dev/null | grep -o 'TRAFFIC_CLINICIANS","value":"[0-9]*' | grep -o '[0-9]*$')"
  echo "simulated clinicians: ${CUR:-3 (shipped default)}"; exit 0
fi
CMDLINE=("$KC" -n healthcare set env deploy/client "TRAFFIC_CLINICIANS=$N")
if [ "$DRY" = 1 ]; then echo "[dry-run] ${CMDLINE[*]}"; exit 0; fi
"${CMDLINE[@]}" && "$KC" -n healthcare rollout status deploy/client --timeout=120s && echo "simulated clinicians: $N"
