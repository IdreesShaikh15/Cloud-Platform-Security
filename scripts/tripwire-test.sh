#!/usr/bin/env bash
# CONTROLLED VALIDATION EVENT (docs/LIVE_DEMO.md, "Tripwire test"): append ONE line to ONE designated, unused marker file in ONE
# healthcare workload, so the real agents genuinely detect a file change on the live cluster and run the real pipeline
# (isolate -> replace from the known-good image -> Recovery Check -> staged return to normal access).
#
#   scripts/tripwire-test.sh <A-F | workload name>            # dry run: prints exactly what it would do, changes nothing
#   scripts/tripwire-test.sh <A-F | workload name> --yes      # does it
#
# What it does, and ONLY this:   kubectl -n healthcare exec deploy/<workload> -- python -c 'open("/app/static/TRIPWIRE-TEST.txt","a").write(...)'
#   * one of the healthcare workloads (four, or six with the optional Laboratory Results and Medication services) (never the client probe, never a namespace other than `healthcare`);
#   * the marker file /app/static/TRIPWIRE-TEST.txt only (nothing reads it; it holds no data); no other file, service or setting is touched;
#   * `python`, not a shell: the agents' process allowlist contains python, so the only signal is the file change;
#   * the change lives in that pod's own writable layer and disappears when the pod is replaced.
# It refuses to run against any kubectl context other than $EXPECT_CONTEXT (default cr-platform).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECT_CONTEXT="${EXPECT_CONTEXT:-cr-platform}"
NS=healthcare
FILE=/app/static/TRIPWIRE-TEST.txt
KUBECTL="${KUBECTL:-kubectl}"

usage() { echo "usage: $0 <A|B|C|D|E|F|workload-name> [--yes]" >&2; exit 2; }
[ $# -ge 1 ] || usage
WHO="$1"; GO="${2:-}"
[ -z "$GO" ] || [ "$GO" = "--yes" ] || usage

# resolve to a workload name from config/workloads.json (every line but the last is a workload, A-D or A-F; the last is the client probe)
mapfile -t NAMES < <(python3 "$ROOT/scripts/gen-workloads.py" --names)
[ "${#NAMES[@]}" -ge 5 ] || { echo "cannot read the workload names" >&2; exit 2; }
TARGET=""
case "$WHO" in
  [A-F]) idx=$(( $(printf '%d' "'$WHO") - 65 )); [ "$idx" -lt $(( ${#NAMES[@]} - 1 )) ] && TARGET="${NAMES[$idx]}" ;;
  *) for n in "${NAMES[@]:0:$(( ${#NAMES[@]} - 1 ))}"; do [ "$n" = "$WHO" ] && TARGET="$n"; done ;;
esac
[ -n "$TARGET" ] || { echo "refused: '$WHO' is not one of the healthcare workloads (${NAMES[*]:0:$(( ${#NAMES[@]} - 1 ))}); the client probe and everything else are never touched" >&2; exit 2; }

CTX="$($KUBECTL config current-context 2>/dev/null || true)"
if [ "$CTX" != "$EXPECT_CONTEXT" ]; then
  echo "refused: kubectl context is '${CTX:-none}', expected '$EXPECT_CONTEXT' (set EXPECT_CONTEXT to change it on purpose)" >&2; exit 3
fi

PY="open(\"$FILE\",\"a\").write(\"tripwire test (operator-initiated, harmless)\\n\")"
echo "workload : $TARGET   (namespace $NS, context $CTX)"
echo "file     : $FILE   (the designated marker; nothing reads it)"
echo "command  : $KUBECTL -n $NS exec deploy/$TARGET -- python -c '$PY'"
if [ "$GO" != "--yes" ]; then
  echo "dry run: nothing was changed. Run again with --yes to do it."
  exit 0
fi
$KUBECTL -n "$NS" exec "deploy/$TARGET" -- python -c "$PY"
echo "done: one line appended to $FILE in $TARGET. Within a few seconds the four agents should report 'File Changes' on it."
echo "Watch: Live Monitoring -> Security Alerts. Restore: do nothing (the platform replaces the pod from the known-good image); if the agents are not running, scripts/reset.sh."
