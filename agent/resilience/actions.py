"""Audit trail and error classification for every Kubernetes action (docs/RECOVERY.md).

THE RULE: A TIMEOUT DOES NOT MEAN THE ACTION FAILED.
When a write to the Kubernetes API times out, the request may still have been applied. So the
executor never retries blindly. It looks at the ACTUAL cluster state first:

    write raised                       what we know               what we do
    ---------------------------------  -------------------------  -------------------------------------
    a definite refusal (403/404/409/   it was NOT applied         record "failed", retry with back-off
      422, webhook denial, bad request)
    an ambiguous error (timeout,       unknown                    read the cluster:
      connection reset, 5xx, 429)                                   effect present  -> "applied_after_timeout"
                                                                    effect absent   -> "not_applied", retry
                                                                    cannot read it  -> "unknown" (shown as
                                                                                       UNKNOWN, re-checked)

All actions are idempotent (they set a state, they do not "toggle"), so repeating one is safe.

Every attempt writes an audit record; the records are hash-chained like the decision log.
"""
from __future__ import annotations

import socket
import threading
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional

from .decisionlog import DecisionLog

# ---- outcomes (the vocabulary the dashboard shows)
APPLIED = "applied"                         # the call succeeded and the cluster now shows the effect
APPLIED_UNCONFIRMED = "applied_unconfirmed"  # the call succeeded; the confirming read failed
ALREADY_APPLIED = "already_applied"          # nothing to do: the cluster already had the effect
APPLIED_AFTER_TIMEOUT = "applied_after_timeout"  # the call timed out, but the effect IS there
NOT_APPLIED = "not_applied"                  # the call timed out and the effect is NOT there
FAILED = "failed"                            # definitely refused / failed
UNKNOWN = "unknown"                          # timed out and the cluster could not be read
UNKNOWN_RESOLVED = "unknown_resolved"        # an earlier UNKNOWN was settled by a later read
REFUSED_STALE = "refused_stale"              # target changed / incident superseded: not performed
REFUSED_CERT = "refused_certificate"         # no valid quorum certificate: not performed
WAITING = "waiting"                          # a prerequisite is not met yet
ABANDONED = "abandoned"                      # too many failures: a human is needed

DEFINITE, AMBIGUOUS = "definite", "ambiguous"
_DEFINITE_STATUS = {400, 401, 403, 404, 405, 409, 410, 413, 415, 422}


class ApiTimeout(Exception):
    """Used by the fake cluster to model a request whose outcome is not known."""


def classify_error(exc: BaseException) -> str:
    """DEFINITE (the request was refused: it did not take effect) or AMBIGUOUS (it may have)."""
    from .response import AdmissionDenied          # local import: response imports this module's constants
    if isinstance(exc, AdmissionDenied):
        return DEFINITE
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status:
        return DEFINITE if status in _DEFINITE_STATUS else AMBIGUOUS
    if isinstance(exc, (ApiTimeout, TimeoutError, socket.timeout, ConnectionError)):
        return AMBIGUOUS
    name = type(exc).__name__
    if any(k in name for k in ("Timeout", "MaxRetry", "ProtocolError", "ConnectionError", "NewConnection", "SSLError")):
        return AMBIGUOUS
    if isinstance(exc, OSError):
        return AMBIGUOUS
    return AMBIGUOUS                                  # unknown error type: do NOT assume it did nothing


class ActionAudit:
    """In-memory ring + hash-chained file of every executor action and its observable result."""

    def __init__(self, node: str, path: Optional[str] = None, clock=time.time, keep: int = 200):
        self.node, self.clock = node, clock
        self.chain = DecisionLog(path)
        self.recent: Deque[dict] = deque(maxlen=keep)
        self.unresolved: Dict[str, dict] = {}              # task key -> latest UNKNOWN record
        self._lock = threading.Lock()

    def record(self, *, action: str, task: str, workload: str, target: str, epoch: int, outcome: str,
               attempt: int = 1, detail: str = "", error: str = "", duration_s: float = 0.0,
               extra: Optional[dict] = None) -> dict:
        rec = {"id": uuid.uuid4().hex[:12], "t": self.clock(), "node": self.node, "action": action,
               "task": task, "workload": workload, "target": target, "epoch": epoch, "attempt": attempt,
               "outcome": outcome, "detail": detail, "error": error[:300], "duration_s": round(duration_s, 3)}
        if extra:
            rec["extra"] = extra
        with self._lock:
            self.recent.append(rec)
            if outcome == UNKNOWN:
                self.unresolved[task] = rec
            elif task in self.unresolved and outcome not in (WAITING,):
                self.unresolved.pop(task, None)
            self.chain.append(dict(rec))
        return rec

    def status(self, n: int = 40) -> dict:
        with self._lock:
            return {"recent": list(self.recent)[-n:], "unknown": list(self.unresolved.values()),
                    "log": self.chain.status()}
