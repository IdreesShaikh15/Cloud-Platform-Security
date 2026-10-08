"""Tamper-evident decision log: every quorum decision record stores the hash of the previous one.

    record_i = { index: i, prev_hash: hash(record_{i-1}), ...decision fields..., hash: H(record_i) }

where H is SHA-256 over the canonical JSON of the record without its own "hash" field.
Changing, deleting, inserting or re-ordering any record breaks the chain at that point, and
`verify()` reports the FIRST broken record.

WHAT THIS DOES NOT DO (stated honestly)
  * A hash chain DETECTS tampering; it cannot PREVENT it.
  * Someone with full write access to the log can rewrite the ENTIRE chain (recompute every hash)
    and the result verifies. test_whole_chain_rewrite_is_not_detected demonstrates this.
  * Deleting records from the END of the log is not detectable from the log alone.
  Stronger protection needs the head hash stored somewhere the attacker cannot write (peers, a
  write-once store). Each agent keeps its own log, so comparing the agents' logs also helps.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import List, Optional, Tuple

DEFAULT_PATH = "/var/log/resilience/decisions.jsonl"
GENESIS = "0" * 64


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def hash_record(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def digest_of(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


class DecisionLog:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._lock = threading.Lock()
        self.records: List[dict] = []
        self.load_error: Optional[str] = None
        if path and os.path.exists(path):
            self.records, self.load_error = self._read(path)
        self._status_cache: Optional[dict] = None

    # ---- reading / verifying ---------------------------------------------------------
    @staticmethod
    def _read(path: str) -> Tuple[List[dict], Optional[str]]:
        recs: List[dict] = []
        err = None
        with open(path, "r", encoding="utf-8") as fh:
            for n, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    err = f"line {n + 1} is not valid JSON"
                    recs.append({"index": -1, "unparseable": True})
        return recs, err

    @staticmethod
    def verify(records: List[dict]) -> Tuple[bool, Optional[int], str, int]:
        """(ok, first_broken_position, reason, count)."""
        prev = GENESIS
        for pos, rec in enumerate(records):
            if rec.get("unparseable"):
                return False, pos, "record is not valid JSON", len(records)
            if rec.get("index") != pos:
                return False, pos, (f"records out of order or deleted: found index {rec.get('index')}, "
                                    f"expected {pos}"), len(records)
            if rec.get("prev_hash") != prev:
                return False, pos, "chain link broken: prev_hash does not match the previous record", len(records)
            if hash_record(rec) != rec.get("hash"):
                return False, pos, "record was modified: its hash does not match its content", len(records)
            prev = rec["hash"]
        return True, None, "ok", len(records)

    @classmethod
    def verify_file(cls, path: str) -> Tuple[bool, Optional[int], str, int]:
        if not os.path.exists(path):
            return False, None, "no such log file", 0
        recs, _ = cls._read(path)
        return cls.verify(recs)

    # ---- writing -------------------------------------------------------------------
    @property
    def head(self) -> str:
        return self.records[-1].get("hash", GENESIS) if self.records and not self.records[-1].get("unparseable") else GENESIS

    def append(self, body: dict) -> dict:
        with self._lock:
            rec = {"index": len(self.records), "prev_hash": self.head, **body}
            rec["hash"] = hash_record(rec)
            self.records.append(rec)
            self._status_cache = None
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
                    fh.flush()
            return rec

    def status(self) -> dict:
        with self._lock:
            if self._status_cache is None:
                ok, idx, reason, n = self.verify(self.records)
                self._status_cache = {"length": n, "head": self.head[:16], "ok": ok, "first_broken": idx,
                                      "reason": reason, "persistent": bool(self.path)}
            return dict(self._status_cache)
