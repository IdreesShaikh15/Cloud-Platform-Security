"""Host-level telemetry exported by every healthcare workload at /_telemetry.

Everything is read from /proc of the container's own namespaces, so it
reflects what is *actually* running (an attacker's extra process, extra
outbound sockets, modified files), not what the application code believes.

`hash_tree()` is also imported by scripts/gen-baseline-hashes.py so the
known-good baseline and the runtime check use the same function.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, List

SKIP_DIRS = {"__pycache__"}


def hash_tree(root: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            h = hashlib.sha256()
            try:
                with open(full, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
            except OSError:
                continue
            out[rel] = h.hexdigest()
    return out


def processes() -> List[dict]:
    procs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as fh:
                comm = fh.read().strip()
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = ""
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode(errors="replace").strip()
            procs.append({"pid": int(pid), "comm": comm, "exe": exe, "cmdline": cmd[:200]})
        except OSError:
            continue  # process exited while we were reading it
    return procs


def _parse_tcp(path: str, listen_port: int) -> List[str]:
    remotes = []
    try:
        with open(path) as fh:
            next(fh)
            for line in fh:
                parts = line.split()
                if len(parts) < 4 or parts[3] != "01":  # 01 = ESTABLISHED
                    continue
                local_port = int(parts[1].split(":")[1], 16)
                if local_port == listen_port:
                    continue  # inbound connection to our server
                remotes.append(parts[2])
    except OSError:
        pass
    return remotes


def outbound_connections(listen_port: int) -> List[str]:
    return _parse_tcp("/proc/net/tcp", listen_port) + _parse_tcp("/proc/net/tcp6", listen_port)


def tx_bytes() -> int:
    total = 0
    try:
        with open("/proc/net/dev") as fh:
            for line in fh.readlines()[2:]:
                iface, data = line.split(":", 1)
                if iface.strip() == "lo":
                    continue
                total += int(data.split()[8])
    except OSError:
        pass
    return total


def snapshot(app_root: str, listen_port: int) -> dict:
    conns = outbound_connections(listen_port)
    return {
        "outbound_connections": len(conns),
        "outbound_remotes": sorted(set(conns))[:50],
        "tx_bytes": tx_bytes(),
        "processes": processes(),
        "file_hashes": hash_tree(app_root),
    }
