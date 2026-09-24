#!/usr/bin/env bash
# Build the three images with the host Docker and load them into every Minikube node.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${PROFILE:-cr-platform}"

docker build -t cr-healthcare-app:1.0 -t cr-healthcare-app:known-good "$ROOT/apps"
docker build -t cr-agent:1.0 "$ROOT/agent"
docker build -t cr-dashboard:1.0 "$ROOT/dashboard"

for img in cr-healthcare-app:1.0 cr-healthcare-app:known-good cr-agent:1.0 cr-dashboard:1.0; do
  echo ">> loading $img into all nodes of $PROFILE"
  minikube -p "$PROFILE" image load "$img"
done

echo ">> generating known-good file hash manifest"
python3 "$ROOT/scripts/gen-baseline-hashes.py"
