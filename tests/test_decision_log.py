"""Tamper-evident decision log (docs/SECURITY.md): modified, deleted and reordered records are caught."""
import json
import os
import subprocess
import sys

import pytest

from resilience.decisionlog import GENESIS, DecisionLog, hash_record

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def filled(n=6, path=None):
    log = DecisionLog(path)
    for i in range(n):
        log.append({"t": 1000 + i, "action": "CONTAIN" if i % 2 == 0 else "ADVANCE_STAGE", "target": "C",
                    "epoch": i, "stage": "", "voters": ["A", "B", "C"], "note": f"decision {i}"})
    return log


def test_an_untouched_chain_verifies():
    log = filled()
    ok, idx, reason, n = DecisionLog.verify(log.records)
    assert ok and idx is None and n == 6
    assert log.records[0]["prev_hash"] == GENESIS
    for a, b in zip(log.records, log.records[1:]):
        assert b["prev_hash"] == a["hash"]
    assert log.status()["ok"] and log.status()["length"] == 6


def test_a_modified_record_is_reported_at_that_record():
    log = filled()
    recs = [dict(r) for r in log.records]
    recs[3]["voters"] = ["A", "B", "D"]                         # change who authorised it
    ok, idx, reason, _ = DecisionLog.verify(recs)
    assert not ok and idx == 3 and "modified" in reason


def test_a_modified_record_with_a_fixed_up_hash_breaks_the_next_link():
    log = filled()
    recs = [dict(r) for r in log.records]
    recs[2]["note"] = "forged"
    recs[2]["hash"] = hash_record(recs[2])                       # attacker recomputes just this hash
    ok, idx, reason, _ = DecisionLog.verify(recs)
    assert not ok and idx == 3 and "chain link broken" in reason


def test_a_deleted_record_is_detected():
    log = filled()
    recs = [r for i, r in enumerate(log.records) if i != 2]
    ok, idx, reason, _ = DecisionLog.verify(recs)
    assert not ok and idx == 2 and "deleted" in reason
    first = log.records[1:]                                      # deleting the very first record too
    assert DecisionLog.verify(first)[1] == 0


def test_reordered_records_are_detected():
    log = filled()
    recs = list(log.records)
    recs[1], recs[2] = recs[2], recs[1]
    ok, idx, reason, _ = DecisionLog.verify(recs)
    assert not ok and idx == 1 and "out of order" in reason


def test_an_inserted_record_is_detected():
    log = filled()
    recs = list(log.records)
    extra = {"index": 2, "prev_hash": recs[1]["hash"], "action": "CONTAIN", "target": "B", "epoch": 9}
    extra["hash"] = hash_record(extra)
    recs.insert(2, extra)
    ok, idx, _, _ = DecisionLog.verify(recs)
    assert not ok and idx == 3


def test_file_round_trip_and_the_verify_command(tmp_path):
    path = str(tmp_path / "decisions.jsonl")
    log = filled(5, path)
    assert DecisionLog.verify_file(path)[0]
    assert DecisionLog(path).head == log.head and len(DecisionLog(path).records) == 5   # reloads intact
    run = lambda: subprocess.run([sys.executable, "-m", "resilience", "verify-log", path],  # noqa: E731
                                 cwd=os.path.join(ROOT, "agent"), capture_output=True, text=True)
    r = run()
    assert r.returncode == 0 and "chain intact" in r.stdout and "5 record(s)" in r.stdout
    lines = open(path).read().splitlines()
    rec = json.loads(lines[2])
    rec["epoch"] = 99                                              # tamper with the file on disk
    lines[2] = json.dumps(rec, sort_keys=True)
    open(path, "w").write("\n".join(lines) + "\n")
    r = run()
    assert r.returncode == 1 and "BROKEN at record #2" in r.stdout and "modified" in r.stdout
    open(path, "w").write("\n".join(lines[:1] + ["{ not json"] + lines[2:]) + "\n")
    assert DecisionLog.verify_file(path)[1] == 1                    # unreadable record reported too


def test_a_broken_log_stays_flagged_when_reloaded_and_extended(tmp_path):
    path = str(tmp_path / "d.jsonl")
    filled(4, path)
    lines = open(path).read().splitlines()
    rec = json.loads(lines[1])
    rec["note"] = "x"
    lines[1] = json.dumps(rec, sort_keys=True)
    open(path, "w").write("\n".join(lines) + "\n")
    log = DecisionLog(path)
    assert not log.status()["ok"] and log.status()["first_broken"] == 1
    log.append({"action": "CONTAIN", "target": "C", "epoch": 5})
    assert not log.status()["ok"] and log.status()["first_broken"] == 1, "new records must not hide the damage"


# ---- the limits, demonstrated honestly
def test_whole_chain_rewrite_is_not_detected():
    """A hash chain detects tampering; it cannot stop someone with full access from rewriting
    EVERYTHING consistently. This test documents that limitation."""
    log = filled()
    forged, prev = [], GENESIS
    for i, rec in enumerate(log.records):
        r = {k: v for k, v in rec.items() if k not in ("hash", "prev_hash")}
        r["voters"] = ["A", "B", "D"]                              # rewrite history
        r["index"], r["prev_hash"] = i, prev
        r["hash"] = hash_record(r)
        prev = r["hash"]
        forged.append(r)
    assert DecisionLog.verify(forged)[0], "a complete consistent rewrite verifies (known limitation)"
    assert forged[-1]["hash"] != log.head                          # ... but the head hash differs from the real one


def test_truncating_the_tail_is_not_detected_by_the_chain_alone():
    log = filled()
    assert DecisionLog.verify(log.records[:-2])[0], "records removed from the END leave a valid shorter chain"


# ---- the agent writes it
def test_agent_records_every_quorum_decision_in_the_chain(tmp_path):
    from test_certificate import Cluster4
    from resilience.agent import ResilienceAgent
    c = Cluster4()
    a = ResilienceAgent(c.cfg, "A", c.signers["A"], c.reg, c.agents["A"].telemetry, c.backend,
                        clock=lambda: c.t[0], decision_log_path=str(tmp_path / "A.jsonl"))
    a.transport = c.agents["A"].transport
    c.agents["A"] = a
    c.cast()
    c.commit("A")
    assert len(a.decision_log.records) == 1
    rec = a.decision_log.records[0]
    assert rec["action"] == "CONTAIN" and rec["workload"] == "records-api" and rec["proposal"] == "CONTAIN:C:1:"
    assert len(rec["justification_digest"]) == 64
    assert a.status()["decision_log"]["ok"] and a.status()["decision_log"]["length"] == 1
    assert DecisionLog.verify_file(str(tmp_path / "A.jsonl"))[0]
