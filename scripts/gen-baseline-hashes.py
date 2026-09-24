#!/usr/bin/env python3
"""Compute the known-good SHA-256 manifest of the healthcare app and write it
as a ConfigMap (k8s/generated/known-good-hashes.json). Agents use it for
file-integrity detection and post-recovery hash validation.

It hashes apps/src/ with the *same* function the app uses at runtime
(apps/src/telemetry.py:hash_tree), and apps/Dockerfile copies exactly that
directory to /app, so the hashes match a clean container byte for byte.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "apps", "src")
sys.path.insert(0, SRC)

from telemetry import hash_tree  # noqa: E402


def main():
    hashes = hash_tree(SRC)
    gen = os.path.join(ROOT, "k8s", "generated")
    os.makedirs(gen, exist_ok=True)
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "known-good-hashes", "namespace": "resilience"},
          "data": {"known-good-hashes.json": json.dumps(hashes, indent=2, sort_keys=True)}}
    with open(os.path.join(gen, "known-good-hashes.json"), "w") as fh:
        json.dump(cm, fh, indent=2)
    print(f"{len(hashes)} files hashed -> k8s/generated/known-good-hashes.json")
    for p, h in sorted(hashes.items()):
        print(f"  {h[:16]}  {p}")


if __name__ == "__main__":
    main()
