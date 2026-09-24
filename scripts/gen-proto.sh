#!/usr/bin/env bash
# Regenerate the Python gRPC stubs from proto/resilience.proto.
# The generated files are committed, so you only need this if you edit the .proto.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/agent/resilience/proto"
mkdir -p "$OUT"
python3 -m grpc_tools.protoc \
  -I "$ROOT/proto" \
  --python_out="$OUT" \
  --grpc_python_out="$OUT" \
  "$ROOT/proto/resilience.proto"
# grpc_tools emits an absolute import; make it package-relative.
sed -i.bak 's/^import resilience_pb2 as/from . import resilience_pb2 as/' "$OUT/resilience_pb2_grpc.py"
rm -f "$OUT/resilience_pb2_grpc.py.bak"
touch "$OUT/__init__.py"
echo "Generated stubs in $OUT"
