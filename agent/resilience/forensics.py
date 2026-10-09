"""Evidence snapshot taken BEFORE a compromised workload is replaced (docs/RECOVERY.md).

Redeploying from the known-good image destroys the compromised pod, and with it the only copy of
what was wrong. So the platform keeps one first.

CONTENT (one JSON document per incident attempt, per agent)
  pods            metadata of the workload's pods: name, uid, IP, node, phase, start time, labels,
                  per-container image / image id / restarts / state
  logs            the last lines of each pod's log (secrets scrubbed)
  observed        what THIS agent measured: the 4 signals, detections with confidence, the running
                  process list (name + executable only), recent evidence about the target
  file_hashes     the monitored files: observed hash vs the known-good baseline, with the modified /
                  missing / new files named
  decision        the quorum decision that triggered the recovery (voters, proposal, justification digest,
                  certificate digest)
  not_captured    everything that could NOT be captured, and why. Nothing is faked or left blank silently.

SAFETY (enforced in code and tested)
  * READ ONLY: it only lists pods and reads logs; it changes nothing in the cluster.
  * SCOPED: only pods labelled app=<this workload> in its own namespace; at most `max_pods`.
  * NO SECRETS: it never reads Secrets or ConfigMaps; pod environment variables, volumes, commands and
    arguments are not copied; process command lines are dropped; log text is scrubbed of tokens,
    passwords, bearer values and key-like strings.
  * BOUNDED: at most `log_tail_lines` lines and `log_max_bytes` bytes per pod.
The snapshot carries a SHA-256 digest of its own content so a later change is detectable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

SCOPE_STATEMENT = ("read-only; only pods of this workload; no Secrets, ConfigMaps, environment variables, "
                   "volumes or command lines; log text scrubbed of credentials")

_REDACTIONS = [
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^\r\n]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
                r"(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"), r"\1\2[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED-JWT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
     "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), "[REDACTED-HEX]"),
]


def redact(text: str) -> Tuple[str, int]:
    """(scrubbed text, number of substitutions)."""
    n = 0
    for pat, repl in _REDACTIONS:
        text, k = pat.subn(repl, text)
        n += k
    return text, n


def _g(obj, *path, default=None):
    """getattr/dict access along a path, tolerant of both kubernetes objects and plain dicts."""
    for p in path:
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


def _iso(ts) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts is not None else "")


def pod_summary(pod) -> dict:
    """Metadata of one pod, deliberately WITHOUT env, volumes, command, args or annotations."""
    containers = []
    for cs in _g(pod, "status", "container_statuses", default=[]) or []:
        state = _g(cs, "state")
        kind, reason = "unknown", ""
        for k in ("running", "waiting", "terminated"):
            if _g(state, k) is not None:
                kind, reason = k, _g(state, k, "reason", default="") or ""
        containers.append({"name": _g(cs, "name", default=""), "image": _g(cs, "image", default=""),
                           "image_id": _g(cs, "image_id", default=""), "ready": bool(_g(cs, "ready", default=False)),
                           "restart_count": int(_g(cs, "restart_count", default=0) or 0),
                           "state": kind, "reason": reason})
    labels = dict(_g(pod, "metadata", "labels", default={}) or {})
    return {"name": _g(pod, "metadata", "name", default=""), "uid": _g(pod, "metadata", "uid", default=""),
            "phase": _g(pod, "status", "phase", default=""), "pod_ip": _g(pod, "status", "pod_ip", default=""),
            "node": _g(pod, "spec", "node_name", default=""),
            "start_time": _iso(_g(pod, "status", "start_time")),
            "deletion_timestamp": _iso(_g(pod, "metadata", "deletion_timestamp")),
            "labels": labels, "containers": containers,
            "owner_kinds": [_g(o, "kind", default="") for o in (_g(pod, "metadata", "owner_references", default=[]) or [])]}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest_of(snapshot: dict) -> str:
    body = {k: v for k, v in snapshot.items() if k != "digest"}
    return hashlib.sha256(canonical(body)).hexdigest()


def file_hash_report(baseline: Dict[str, str], observed: Dict[str, str]) -> dict:
    if not baseline:
        return {"baseline_files": 0, "observed_files": len(observed), "modified": [], "missing": [], "new": [],
                "observed": dict(observed), "note": "no baseline manifest configured"}
    return {"baseline_files": len(baseline), "observed_files": len(observed),
            "modified": sorted(p for p, h in baseline.items() if p in observed and observed[p] != h),
            "missing": sorted(p for p in baseline if p not in observed),
            "new": sorted(p for p in observed if p not in baseline),
            "observed": dict(observed), "baseline": dict(baseline)}


class SnapshotManager:
    """Per-agent: captures, stores and serves evidence snapshots."""

    MAX_KEPT = 20

    def __init__(self, agent, directory: Optional[str] = None):
        self.a = agent
        self.dir = directory
        self.lock = threading.RLock()
        self.items: "OrderedDict[str, dict]" = OrderedDict()
        self.started: Dict[str, float] = {}
        self.done: Dict[str, threading.Event] = {}
        if directory and os.path.isdir(directory):
            self._load()

    @property
    def p(self):
        return self.a.cfg.recovery

    @staticmethod
    def key(target: str, epoch: int, tag: str = "contain") -> str:
        """tag: "contain" = the compromised pod(s) before the first replacement; "failed<N>" = the
        replacement pod(s) of recovery attempt N that failed validation."""
        return f"{target}:{epoch}:{tag}"

    # ---- capture ------------------------------------------------------------------
    def observed_now(self, target: str, now: float) -> dict:
        """What this agent sees right now. Call it synchronously, at the moment of the decision:
        the replacement is about to overwrite the evidence."""
        a = self.a
        s = a.snaps.get(target)
        det = a.detector.measurements.get(target, {})
        procs = [{"pid": p.get("pid"), "comm": p.get("comm", ""), "exe": p.get("exe", "")}
                 for p in (s.processes if s else [])]                # command lines are NOT copied
        return {
            "captured_by": a.id, "at": now,
            "telemetry": {"reachable": bool(s and s.reachable), "healthy": bool(s and s.healthy),
                          "instance_id": s.instance_id if s else "", "pod_ip": s.pod_ip if s else "",
                          "outbound_connections": s.outbound_connections if s else None,
                          "tx_bytes": s.tx_bytes if s else None, "processes": procs,
                          "auth_failures_by_ip": dict(s.auth_failures_by_ip) if s else {}},
            "detections": [{"type": d.observation.value, "confidence": d.confidence, "summary": d.summary}
                           for d in a.latest.get(target, [])],
            "measurements": json.loads(json.dumps(det, default=str)),
            "recent_evidence": [e.to_dict() for e in a.pool.recent(now, a.cfg.timers.evidence_window_s,
                                                                   target=target)][-30:],
            "file_hashes": file_hash_report(a.cfg.baseline_hashes, dict(s.file_hashes) if s else {}),
            "telemetry_available": bool(s and s.reachable),
        }

    def capture_async(self, target: str, epoch: int, tag: str, reason: str, decision: Optional[dict],
                      now: float) -> str:
        """Start a capture; returns its key. The observed values are taken synchronously."""
        key = self.key(target, epoch, tag)
        if not self.p.snapshot_enabled:
            return key
        with self.lock:
            if key in self.started:
                return key
            self.started[key] = now
            self.done[key] = threading.Event()
        observed = self.observed_now(target, now)
        threading.Thread(target=self._capture, name=f"snap-{key}", daemon=True,
                         args=(key, target, epoch, tag, reason, decision, observed, now)).start()
        return key

    def _capture(self, key, target, epoch, tag, reason, decision, observed, now) -> None:
        a, p = self.a, self.p
        workload = a.workload(target)
        missing: List[dict] = []
        pods: List[dict] = []
        logs: Dict[str, dict] = {}
        try:
            try:
                raw = a.backend.list_pods(workload)
                pods = list(raw)[: p.max_pods]
                if len(raw) > p.max_pods:
                    missing.append({"item": "pods", "reason": f"{len(raw) - p.max_pods} more pod(s) not captured (limit {p.max_pods})"})
                if not pods:
                    missing.append({"item": "pods", "reason": "no pod of this workload was found"})
            except Exception as exc:
                missing.append({"item": "pod metadata", "reason": f"could not list pods: {exc}"})
            for pod in pods:
                name = pod.get("name", "")
                try:
                    text = a.backend.read_logs(name, p.log_tail_lines, p.log_max_bytes)
                except Exception as exc:
                    status = getattr(exc, "status", None)
                    why = ("the pod disappeared (it was replaced) before its logs could be read"
                           if status == 404 else f"could not read logs: {exc}")
                    missing.append({"item": f"logs of {name}", "reason": why})
                    continue
                raw_bytes = len(text.encode())
                lines = text.splitlines()[-p.log_tail_lines:]
                clean, n = redact("\n".join(lines))
                clean = clean.encode()[: p.log_max_bytes].decode(errors="ignore")
                logs[name] = {"lines": clean.splitlines(), "redactions": n,
                              "truncated": raw_bytes > len(clean.encode()) or len(text.splitlines()) > len(lines)}
            if pods:                                    # stale-identifier check: did the pods change meanwhile?
                try:
                    after = {x.get("uid") for x in a.backend.list_pods(workload)}
                    gone = sorted({x.get("uid") for x in pods} - after)
                    if gone:
                        missing.append({"item": "consistency", "reason":
                                        f"pod(s) {', '.join(gone)} were replaced while the snapshot was being taken"})
                except Exception:
                    pass
            if not observed.get("telemetry_available"):
                missing.append({"item": "observed signals", "reason": "the workload's telemetry endpoint was unreachable"})
            if not a.cfg.baseline_hashes:
                missing.append({"item": "file hash comparison", "reason": "no known-good hash manifest is configured"})
            snap = {
                "incident": {"target": target, "workload": workload, "epoch": epoch, "tag": tag,
                             "reason": reason, "captured_by": a.id, "captured_at": now,
                             "finished_at": a.clock()},
                "decision": decision or {},
                "pods": pods, "logs": logs,
                "observed": {k: v for k, v in observed.items() if k != "file_hashes"},
                "file_hashes": observed["file_hashes"],
                "not_captured": missing, "scope": SCOPE_STATEMENT,
            }
            snap["digest"] = digest_of(snap)
            self._store(key, snap)
            a.metrics.event("snapshot_captured", a.clock(), target, snapshot=key, missing=len(missing))
            a.events.emit("ACTION", f"Agent {a.id} saved an evidence snapshot of {workload} ({target}) before it is "
                          f"replaced: {len(pods)} pod(s), {sum(len(l['lines']) for l in logs.values())} log line(s)"
                          + (f"; NOT captured: {'; '.join(m['item'] for m in missing)}" if missing else "") + ".",
                          target, {"action": "snapshot", "key": key, "pods": len(pods),
                                   "not_captured": missing, "digest": snap["digest"][:16]})
        except Exception as exc:                        # a snapshot problem must never break recovery
            log.exception("snapshot failed")
            snap = {"incident": {"target": target, "workload": workload, "epoch": epoch, "tag": tag,
                                 "reason": reason, "captured_by": a.id, "captured_at": now},
                    "decision": decision or {}, "pods": [], "logs": {}, "observed": {},
                    "file_hashes": {}, "scope": SCOPE_STATEMENT,
                    "not_captured": [{"item": "everything", "reason": f"snapshot failed: {exc}"}]}
            snap["digest"] = digest_of(snap)
            self._store(key, snap)
        finally:
            self.done[key].set()

    # ---- storage ----------------------------------------------------------------------
    def _path(self, key: str) -> str:
        t, e, tag = key.split(":")
        return os.path.join(self.dir, f"snapshot-{t}-e{e}-{tag}-{self.a.id}.json")

    def _store(self, key: str, snap: dict) -> None:
        with self.lock:
            self.items[key] = snap
            while len(self.items) > self.MAX_KEPT:
                self.items.popitem(last=False)
        if self.dir:
            try:
                os.makedirs(self.dir, exist_ok=True)
                path = self._path(key)
                fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    json.dump(snap, fh, indent=1, sort_keys=True, default=str)
                os.replace(path + ".tmp", path)
            except OSError as exc:
                log.warning("could not persist snapshot %s: %s", key, exc)

    def _load(self) -> None:
        try:
            for name in sorted(os.listdir(self.dir)):
                if name.startswith("snapshot-") and name.endswith(f"-{self.a.id}.json"):
                    with open(os.path.join(self.dir, name)) as fh:
                        snap = json.load(fh)
                    i = snap["incident"]
                    self.items[self.key(i["target"], i["epoch"], i.get("tag", "contain"))] = snap
        except (OSError, ValueError, KeyError):
            log.warning("could not reload snapshots from %s", self.dir)

    # ---- use ------------------------------------------------------------------------------
    def ready(self, key: str, now: float) -> Tuple[bool, str]:
        """Has the capture finished (or been waited for long enough)? Recovery calls this: evidence is
        important but must never block recovery forever."""
        if not self.p.snapshot_enabled or key not in self.started:
            return True, "snapshots disabled"
        ev = self.done.get(key)
        if ev is not None and ev.is_set():
            return True, "captured"
        waited = now - self.started[key]
        if waited >= self.p.snapshot_timeout_s:
            return True, f"gave up waiting after {waited:.0f}s (the snapshot continues in the background)"
        return False, f"waiting for the evidence snapshot ({waited:.0f}s of {self.p.snapshot_timeout_s:g}s)"

    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            return self.items.get(key)

    def pod_uids(self, key: str) -> List[str]:
        """uids of the pods that existed when snapshot `key` was taken (empty if unknown)."""
        s = self.get(key)
        return [p.get("uid") for p in (s or {}).get("pods", []) if p.get("uid")]

    def status(self) -> List[dict]:
        with self.lock:
            out = []
            for key, s in reversed(self.items.items()):
                i = s["incident"]
                out.append({"key": key, "target": i["target"], "workload": i["workload"], "epoch": i["epoch"],
                            "tag": i.get("tag", "contain"), "reason": i.get("reason", ""),
                            "captured_at": i.get("captured_at"), "agent": i.get("captured_by"),
                            "pods": len(s.get("pods", [])),
                            "log_lines": sum(len(l["lines"]) for l in s.get("logs", {}).values()),
                            "not_captured": s.get("not_captured", []), "digest": s.get("digest", "")[:16],
                            "bytes": len(canonical(s))})
            return out[:10]
