#!/usr/bin/env bash
# PRE-PRESENTATION CHECK. Run about 30 minutes before presenting, on the laptop, in the repository directory:
#
#   bash scripts/preflight.sh                  # every check, PASS / WARN / FAIL with a plain-English fix hint
#   bash scripts/preflight.sh --wait 90        # keep re-checking the live-state items for up to 90 s (used by demo-reset.sh)
#   bash scripts/preflight.sh --no-ports       # skip the two port-forward checks (dashboard 8091, portal 18090)
#
# READ-ONLY. It never changes the cluster, the project or any file in it: only `get`, the API-server read proxy, a read-only
# `python -c` hash inside each workload (scripts/verify-baseline.py), `--dry-run=server` requests made AS the agents' identity (the webhook is asked and must refuse; nothing is stored)
# and HTTP GETs. Exit code 0 = no FAIL (WARN is allowed), 1 = at least one FAIL, 2 = bad usage.
# Environment: EXPECT_CONTEXT (cr-platform), EXPECT_NODES (4), DASH_PORT (8091), PORTAL_PORT (18090), KUBECTL (kubectl),
# RESTART_WINDOW_MIN (30: a pod restart within this many minutes is a FAIL, an older one a WARN).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"; EXPECT_NODES="${EXPECT_NODES:-4}"
DASH_PORT="${DASH_PORT:-8091}"; PORTAL_PORT="${PORTAL_PORT:-18090}"; KC="${KUBECTL:-kubectl}"
WAIT=0; PORTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT="${2:?seconds}"; shift 2 ;;
    --no-ports) PORTS=0; shift ;;
    -h|--help) sed -n 2,13p "$0"; exit 0 ;;
    *) echo "usage: $0 [--wait SECONDS] [--no-ports]" >&2; exit 2 ;;
  esac
done
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
NPASS=0; NWARN=0; NFAIL=0
kc() { timeout "${KC_TIMEOUT:-25}" "$KC" "$@"; }
pass() { NPASS=$((NPASS+1)); printf 'PASS  %s\n' "$1"; }
warn() { NWARN=$((NWARN+1)); printf 'WARN  %s\n        -> %s\n' "$1" "$2"; }
fail() { NFAIL=$((NFAIL+1)); printf 'FAIL  %s\n        -> %s\n' "$1" "$2"; }
section() { printf '\n== %s\n' "$1"; }
py() { python3 - "$@"; }

# ------------------------------------------------------------------ 1. Docker, cluster, nodes, Calico
section "1. Docker, cluster, nodes, Calico"
if docker info >/dev/null 2>&1; then pass "Docker is running"
else fail "Docker is not running" "Start Docker Desktop (and make sure its WSL integration is on), wait until it says 'running', then re-run this."; fi

CTX="$($KC config current-context 2>/dev/null || true)"
if [ "$CTX" = "$EXPECT_CONTEXT" ]; then pass "kubectl points at the '$EXPECT_CONTEXT' cluster"
else fail "kubectl context is '${CTX:-none}', expected '$EXPECT_CONTEXT'" "Run: kubectl config use-context $EXPECT_CONTEXT   (if it does not exist: minikube start -p $EXPECT_CONTEXT)"; fi

if kc get --raw /readyz >/dev/null 2>&1; then pass "the cluster API server answers"
else fail "the cluster does not answer" "Run: minikube start -p $EXPECT_CONTEXT   (wait for 'Done!'), then re-run. If it hangs, 'minikube status -p $EXPECT_CONTEXT' shows which part is down."; fi

kc get nodes -o json > "$T/nodes.json" 2>/dev/null || echo '{"items":[]}' > "$T/nodes.json"
py "$T/nodes.json" "$EXPECT_NODES" > "$T/nodes.out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); want = int(sys.argv[2]); ok = []; bad = []
for n in d["items"]:
    r = [c["status"] for c in n["status"]["conditions"] if c["type"] == "Ready"]
    (ok if r and r[0] == "True" else bad).append(n["metadata"]["name"])
print(f"{len(ok)} {len(bad)} {want} {' '.join(bad)}")
PY
read -r NOK NBAD NWANT NBADNAMES < "$T/nodes.out" || true
if [ "${NOK:-0}" = "$EXPECT_NODES" ] && [ "${NBAD:-1}" = "0" ]; then pass "all $EXPECT_NODES nodes are Ready"
else fail "${NOK:-0} of $EXPECT_NODES nodes Ready (not ready: ${NBADNAMES:-unknown})" "Wait a minute and re-run. If a node stays NotReady: minikube node list -p $EXPECT_CONTEXT, then 'minikube stop -p $EXPECT_CONTEXT && minikube start -p $EXPECT_CONTEXT'."; fi

kc get pods -A -l k8s-app=calico-node -o json > "$T/calico.json" 2>/dev/null || echo '{"items":[]}' > "$T/calico.json"
py "$T/calico.json" > "$T/calico.out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); tot = len(d["items"]); ok = 0
for p in d["items"]:
    cs = p["status"].get("containerStatuses") or []
    if p["status"].get("phase") == "Running" and cs and all(c["ready"] for c in cs): ok += 1
print(tot, ok)
PY
read -r CTOT COK < "$T/calico.out" || true
if [ "${CTOT:-0}" -ge 1 ] && [ "${COK:-0}" = "${CTOT:-0}" ]; then pass "Calico (network policy engine) is Running on ${COK} node(s)"
else fail "Calico: ${COK:-0} of ${CTOT:-0} calico-node pods Running" "Isolation needs Calico. Wait a minute; if it stays down: kubectl -n kube-system get pods -l k8s-app=calico-node, or recreate the cluster with scripts/setup-cluster.sh."; fi

# ------------------------------------------------------------------ 2. pods
section "2. Pods Running, Ready, no recent restarts"
mapfile -t NAMES < <(python3 "$ROOT/scripts/gen-workloads.py" --names 2>/dev/null)
if [ "${#NAMES[@]}" -lt 5 ]; then fail "cannot read the workload list from config/workloads.json" "Run: git status; python3 scripts/gen-workloads.py --check"; NAMES=(); fi
NWORK=$(( ${#NAMES[@]} > 0 ? ${#NAMES[@]} - 1 : 0 ))

pod_check() {   # pod_check <namespace> <label>=<value> <display name> <expected count>
  local ns="$1" sel="$2" name="$3" want="$4"
  kc -n "$ns" get pods -l "$sel" -o json > "$T/p.json" 2>/dev/null || echo '{"items":[]}' > "$T/p.json"
  py "$T/p.json" "$want" "${RESTART_WINDOW_MIN:-30}" > "$T/p.out" <<'PY'
import json, sys, datetime
d = json.load(open(sys.argv[1])); want = int(sys.argv[2]); win = float(sys.argv[3]); now = datetime.datetime.now(datetime.timezone.utc)
items = [p for p in d["items"] if not p["metadata"].get("deletionTimestamp")]
bad = []; recent = []; old = []
for p in items:
    cs = p["status"].get("containerStatuses") or []
    if not (p["status"].get("phase") == "Running" and cs and all(c["ready"] for c in cs)):
        bad.append(p["metadata"]["name"] + " (" + p["status"].get("phase", "?") + ")")
    for c in cs:
        if c["restartCount"] > 0:
            t = ((c.get("lastState") or {}).get("terminated") or {}).get("finishedAt")
            age = (now - datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds() / 60 if t else 0
            (recent if age <= win else old).append(f'{p["metadata"]["name"]} restarted {c["restartCount"]}x, last {age:.0f} min ago')
print(len(items), want, "|".join(bad), "|".join(recent), "|".join(old), sep="#")
PY
  local n w b r o; IFS="#" read -r n w b r o < "$T/p.out" || true
  if [ "${n:-0}" != "$want" ] || [ -n "${b:-}" ]; then
    fail "$name: ${n:-0} pod(s) found, $want expected; not Running/Ready: ${b:-none}" "Wait 1-2 minutes. If it persists: kubectl -n $ns describe pod -l $sel | tail -30 (Pending usually means too little Docker Desktop memory: give it 6 GB+)."
  elif [ -n "${r:-}" ]; then
    fail "$name restarted in the last 30 minutes: ${r//|/; }" "Something crashed, or Docker/the laptop was restarted. Look: kubectl -n $ns logs -l $sel --previous --tail=20. If you know the cause (e.g. you restarted Docker) and everything else is green, it is safe to continue: re-run with RESTART_WINDOW_MIN=1 bash scripts/preflight.sh, or wait 30 minutes."
  elif [ -n "${o:-}" ]; then
    warn "$name: ${o//|/; }" "An older restart; harmless if you know why (e.g. Docker was restarted yesterday). Check: kubectl -n $ns logs -l $sel --previous --tail=20."
  else pass "$name: Running and Ready, no restarts"; fi
}
for ((i=0; i<NWORK; i++)); do pod_check healthcare "app=${NAMES[$i]}" "${NAMES[$i]}" 1; done
[ "${#NAMES[@]}" -ge 5 ] && pod_check healthcare "app=${NAMES[$NWORK]}" "${NAMES[$NWORK]} (traffic generator)" 1
for n in a b c d; do pod_check resilience "app=resilience-agent,resilience.io/node=${n^^}" "agent-$n" 1; done
pod_check resilience "app=dashboard" "in-cluster dashboard" 1
pod_check resilience "app=quorum-webhook" "admission webhook (2 replicas)" 2
if kc -n resilience get deploy central-controller >/dev/null 2>&1; then
  fail "the centralized baseline controller is deployed (the agents are normally off in that mode)" "Run: bash scripts/demo-reset.sh   (it switches back to the four distributed agents)."
fi

# ------------------------------------------------------------------ 3. webhook
section "3. Admission webhook"
if kc get validatingwebhookconfiguration cr-quorum-webhook >/dev/null 2>&1; then pass "webhook configuration 'cr-quorum-webhook' is installed"
else fail "webhook configuration 'cr-quorum-webhook' is missing" "Re-install it from your existing certificates (do NOT regenerate them): kubectl apply -f k8s/generated/webhook-config.json   (if that file is missing, send me the output of: ls k8s/generated)."; fi
kc -n resilience get configmap resilience-config -o jsonpath='{.data.config\.json}' > "$T/cfg.json" 2>/dev/null || echo '{}' > "$T/cfg.json"
py "$T/cfg.json" "${NAMES[@]:0:$NWORK}" > "$T/cfg.out" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); want = sorted(sys.argv[2:]); have = []
for n in (c.get("nodes") or {}).values(): have.append(n.get("workload") or n.get("target_workload") or "")
for n in (c.get("extra_workloads") or {}).values(): have.append(n.get("workload") or "")
have = sorted(x for x in have if x)
print("OK" if have == want else "DIFF", ",".join(have), ",".join(want))
PY
read -r CFGOK CFGHAVE CFGWANT < "$T/cfg.out" || true
if [ "${CFGOK:-}" = "OK" ]; then pass "the agents' and webhook's configuration lists exactly the ${NWORK} workloads in config/workloads.json"
else fail "configuration lists [${CFGHAVE:-?}] but config/workloads.json has [${CFGWANT:-?}]" "Run: bash scripts/upgrade-app.sh   (applies the configuration, restarts the webhook before the agents)."; fi
DRYBAD=""
for w in "${NAMES[@]:0:$NWORK}"; do
  out="$(kc --as=system:serviceaccount:resilience:resilience-agent -n healthcare annotate deploy "$w" resilience.io/preflight-probe=1 --overwrite --dry-run=server 2>&1)"
  echo "$out" | grep -qi "denied the request\|admission webhook" || DRYBAD="$DRYBAD $w"
done
if [ -z "$DRYBAD" ]; then pass "the webhook refuses an unauthorised change (server dry-run, nothing stored) on all ${NWORK} workloads"
else fail "the webhook did not refuse an unauthorised change to:$DRYBAD (dry run)" "The webhook may be down or have an old configuration. Run: kubectl -n resilience rollout restart deploy/quorum-webhook, wait 30 s, re-run."; fi

# ------------------------------------------------------------------ 4. fingerprints
section "4. Known-good fingerprints (verify-baseline)"
if python3 "$ROOT/scripts/verify-baseline.py" > "$T/vb.txt" 2>&1; then
  pass "every workload matches the known-good manifest ($(grep -c '^ok' "$T/vb.txt") ok lines)"
else
  fail "fingerprint check failed: $(grep -v '^ok' "$T/vb.txt" | head -3 | tr '\n' ' ')" "The agents would treat that workload as tampered. If a workload was just attacked, wait for recovery or run bash scripts/demo-reset.sh. After a rebuild: bash scripts/build-images.sh && bash scripts/upgrade-app.sh."
fi

# ------------------------------------------------------------------ 5. leftover state
section "5. No leftover isolation or incident state"
kc -n healthcare get networkpolicy -o json > "$T/np.json" 2>/dev/null || echo '{"items":[]}' > "$T/np.json"
NP="$(py "$T/np.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); print(",".join(i["metadata"]["name"] for i in d["items"] if i["metadata"]["name"].startswith("resilience-isolate")))
PY
)"
if [ -z "$NP" ]; then pass "no isolation NetworkPolicy exists"
else fail "leftover isolation policy: $NP" "A workload is still cut off. Wait for recovery to finish, or run: bash scripts/demo-reset.sh"; fi
kc -n healthcare get deploy -o json > "$T/dep.json" 2>/dev/null || echo '{"items":[]}' > "$T/dep.json"
ST="$(py "$T/dep.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); out = []
for i in d["items"]:
    a = i["metadata"].get("annotations") or {}
    keys = [k.split("/")[1] for k in a if k.startswith("resilience.io/") and k.split("/")[1] in ("epoch", "isolated-epoch", "recovered-epoch", "recovery-attempt", "qc", "action")]
    if keys: out.append(i["metadata"]["name"] + ":" + "+".join(sorted(keys)))
print(",".join(out))
PY
)"
if [ -z "$ST" ]; then pass "no workload carries incident annotations"
else fail "stale incident state on: $ST" "Left over from an earlier run. Run: bash scripts/demo-reset.sh"; fi
if kc -n resilience get configmap cr-attack-marker >/dev/null 2>&1; then
  fail "an attack marker ConfigMap (cr-attack-marker) from an earlier experiment exists" "Run: bash scripts/demo-reset.sh   (or kubectl -n resilience delete configmap cr-attack-marker)"
else pass "no experiment marker (cr-attack-marker) exists"; fi

# ------------------------------------------------------------------ 6. agents
section "6. Each agent: reachable, fresh telemetry, everything healthy, trust 100"
agents_check() {
  AFAIL=0; : > "$T/agents.txt"
  for n in a b c d; do
    if ! kc get --raw "/api/v1/namespaces/resilience/services/agent-$n:8081/proxy/status" > "$T/st-$n.json" 2>/dev/null; then
      echo "agent-$n: not reachable" >> "$T/agents.txt"; AFAIL=1; continue; fi
    py "$T/st-$n.json" "$n" >> "$T/agents.txt" <<'PY'
import json, sys
try: s = json.load(open(sys.argv[1]))
except Exception: print(f"agent-{sys.argv[2]}: answered with something unreadable"); sys.exit(0)
p = []
age = (s.get("telemetry") or {}).get("age_s")
if age is None or age > 10: p.append(f"telemetry {'missing' if age is None else str(round(age)) + ' s old'}")
if s.get("data_source") != "LIVE_CLUSTER": p.append(f"labelled {s.get('data_source')}, not LIVE_CLUSTER")
if s.get("mode") != "distributed": p.append(f"mode {s.get('mode')}")
if s.get("compromised"): p.append(f"compromise switch '{s['compromised']}' is ON")
low = {k: v for k, v in (s.get("agent_trust") or {}).items() if v != 100}
if low: p.append("agent trust not 100: " + ", ".join(f"{k}={round(v)}" for k, v in sorted(low.items())))
for k, t in sorted((s.get("targets") or {}).items()):
    if t.get("phase") != "HEALTHY" or t.get("stage") not in (None, "", "FULL") or t.get("workload_trust") != 100:
        p.append(f"workload {k}: {t.get('phase')}/{t.get('stage')} trust {t.get('workload_trust')}")
if len(s.get("targets") or {}) < 1: p.append("watches no workloads")
if p: print(f"agent-{sys.argv[2]}: " + "; ".join(p))
PY
  done
  [ "$AFAIL" = 0 ] && [ ! -s "$T/agents.txt" ]
}
DEADLINE=$(( $(date +%s) + WAIT )); until agents_check; do [ "$(date +%s)" -ge "$DEADLINE" ] && break; sleep 5; done
if [ ! -s "$T/agents.txt" ]; then pass "all 4 agents answer, telemetry is fresh (<10 s), every workload HEALTHY at full access, all trust values 100"
else
  while IFS= read -r l; do fail "$l" "If it just restarted, wait 1 minute and re-run. A compromise switch: bash scripts/simulate-compromised-agent.sh <A-D> restore. Otherwise: bash scripts/demo-reset.sh"; done < "$T/agents.txt"
fi

# ------------------------------------------------------------------ 7. dataset + traffic
section "7. Synthea dataset loaded, client generating traffic"
kc get --raw "/api/v1/namespaces/healthcare/services/patient-portal:8080/proxy/api/patients?limit=3" > "$T/pat.json" 2>/dev/null || echo '{}' > "$T/pat.json"
py "$T/pat.json" > "$T/pat.out" <<'PY'
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: d = {}
print(d.get("source", "?"), d.get("synthetic", "?"), len(d.get("patients") or []))
PY
read -r PSRC PSYN PCNT < "$T/pat.out" || true
if [ "$PSRC" = "Synthea" ] && [ "$PSYN" = "True" ] && [ "${PCNT:-0}" -ge 1 ]; then pass "the patient portal serves Synthea synthetic records (source=Synthea, synthetic=true)"
else fail "the portal did not return Synthea records (got: ${PSRC:-?} ${PSYN:-?} ${PCNT:-0})" "The images may be old. Run: bash scripts/build-images.sh && bash scripts/upgrade-app.sh (and read docs/LAPTOP-UPGRADE.md, section 'FROM 713d0ea')."; fi
rt() { kc get --raw "/api/v1/namespaces/healthcare/services/database:8080/proxy/_telemetry" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("requests_total",-1))' 2>/dev/null || echo -1; }
R1="$(rt)"; sleep 6; R2="$(rt)"
if [ "${R1:--1}" -ge 0 ] && [ "${R2:--1}" -gt "${R1:-0}" ]; then pass "the client is generating traffic (database served $((R2-R1)) requests in 6 s)"
else fail "no new requests reached the database in 6 s ($R1 -> $R2)" "The client pod may be stuck: kubectl -n healthcare rollout restart deploy/client, wait 30 s, re-run."; fi

# ------------------------------------------------------------------ 8. port-forwards
section "8. Dashboard and portal through port-forwards"
if [ "$PORTS" = 1 ]; then
  if curl -fsS -m 6 "http://127.0.0.1:${DASH_PORT}/api/state?channel=live" > "$T/dash.json" 2>/dev/null || curl -fsS -m 6 "http://127.0.0.1:${DASH_PORT}/api/state" > "$T/dash.json" 2>/dev/null; then
    pass "the laptop dashboard answers on http://127.0.0.1:${DASH_PORT}/?mode=live"
    if curl -fsS -m 6 "http://127.0.0.1:${DASH_PORT}/api/series?channel=live" 2>/dev/null | grep -q '"available": *true'; then pass "the Live page has chart data (LIVE_CLUSTER series available)"
    else warn "the Live page has no chart data yet" "The charts need about 10 s of readings after the dashboard starts. Re-run in a moment; if still empty, restart bash scripts/laptop-dashboard.sh."; fi
  else fail "the dashboard is not reachable on port ${DASH_PORT}" "In a second terminal run: bash scripts/laptop-dashboard.sh   (leave it running), then re-run this."; fi
  if curl -fsS -m 6 "http://127.0.0.1:${PORTAL_PORT}/api/patients?limit=1" 2>/dev/null | grep -q Synthea; then pass "the patient portal answers through its port-forward (http://127.0.0.1:${PORTAL_PORT}/)"
  else fail "the patient portal is not reachable on port ${PORTAL_PORT}" "bash scripts/laptop-dashboard.sh opens it; or run: kubectl -n healthcare port-forward svc/patient-portal ${PORTAL_PORT}:8080"; fi
else
  echo "SKIP  port-forward checks (--no-ports)"
fi

# ------------------------------------------------------------------ summary
printf '\n== Summary: %d PASS, %d WARN, %d FAIL\n' "$NPASS" "$NWARN" "$NFAIL"
if [ "$NFAIL" -eq 0 ]; then echo "READY TO PRESENT."; exit 0
else echo "NOT READY: fix the FAIL lines above (each has a hint), then re-run: bash scripts/preflight.sh"; exit 1; fi
