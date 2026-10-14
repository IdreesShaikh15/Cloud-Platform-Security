#!/usr/bin/env bash
# Prepare the dashboard image's build context: the dashboard itself plus the committed evaluation
# (graphs, docs/RESULTS.md, results/summary.md) for the Evaluation tab. Nothing else from the repository is
# copied (in particular not certs/ or k8s/generated/).
#   scripts/stage-dashboard.sh DEST_DIR
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?usage: $0 DEST_DIR}"
mkdir -p "$DEST/evaluation"
cp "$ROOT/dashboard/server.py" "$ROOT/dashboard/index.html" "$ROOT/dashboard/Dockerfile" "$DEST/"
if [ -f "$ROOT/docs/RESULTS.md" ]; then
  cp "$ROOT/docs/RESULTS.md" "$DEST/evaluation/RESULTS.md"
  [ -f "$ROOT/results/summary.md" ] && cp "$ROOT/results/summary.md" "$DEST/evaluation/summary.md"
  cp "$ROOT"/results/fig*.png "$DEST/evaluation/" 2>/dev/null || true
else
  echo ">> no docs/RESULTS.md: the dashboard image will say the evaluation is not included" >&2
fi
