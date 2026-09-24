#!/usr/bin/env python3
"""Run the platform end-to-end on your laptop, no Kubernetes required.

    python3 sim/local_demo.py                 # all scenarios
    python3 sim/local_demo.py app-compromise  # just one
    python3 sim/local_demo.py --serve         # keep running + expose /status on :50251-50254
                                              # (point the dashboard at them, see demo.md)

Scenarios
  app-compromise    genuine compromise of Node C's workload (exfil+tamper+bruteforce)
  false-accusation  agent A is compromised and accuses healthy Node B
  forge-evidence    agent A tries to impersonate B's signature
  baseline          the centralized controller under the same two attacks
Timers are accelerated (0.5 s ticks, 3 s stage dwell) so a full cycle takes ~30 s.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from world import LocalBaseline, LocalCluster  # noqa: E402


def banner(msg):
    print("\n" + "=" * 78 + f"\n{msg}\n" + "=" * 78, flush=True)


def show_metrics(agent_or_ctl):
    for row in agent_or_ctl.metrics.summary():
        keep = {k: row[k] for k in ("scenario", "target", "attacker", "ttd_s", "tti_s", "ttr_s",
                                    "ttv_s", "ttf_s", "trust_recovery_s", "false_isolation",
                                    "time_to_flag_s") if row.get(k) is not None}
        print(f"  [{row['node']}] {json.dumps(keep)}")


def app_compromise(c: LocalCluster) -> bool:
    banner("SCENARIO 1: genuine compromise of Node C (records-api)")
    c.mark("app-compromise", "C")
    c.world.attack("C")
    ok = c.wait_until(lambda: all(p != "HEALTHY" for p in c.phases("C").values()), 15)
    print(f"  contained by quorum: {ok}  decisions: {c.agents['B'].decisions[-1] if c.agents['B'].decisions else None}")
    ok2 = c.wait_until(lambda: all(a.states['C'].phase == 'HEALTHY' and a.states['C'].epoch == 1
                                   for a in c.agents.values()), 90)
    print(f"  recovered, validated and reintegrated through all stages: {ok2}")
    print("  cluster actions:", [(round(x[0] % 1000, 1),) + x[1:4] for x in c.backend.log])
    show_metrics(c.agents["B"])
    return ok and ok2


def false_accusation(c: LocalCluster) -> bool:
    banner("SCENARIO 2: resilience node A compromised, falsely accuses healthy Node B")
    c.compromise_agent("A", "B")
    flagged = c.wait_until(lambda: all(c.agents[n].agent_trust.get("A") < 50 for n in "BCD"), 40)
    time.sleep(2)
    never = all(a.states["B"].epoch == 0 for a in c.agents.values())
    print(f"  honest agents' view of A's trust: "
          f"{ {n: round(c.agents[n].agent_trust.get('A'), 1) for n in 'BCD'} }")
    print(f"  B never isolated (no false isolation): {never}; A flagged as suspect: {flagged}")
    print(f"  pending votes on B: {[k for k in c.agents['C'].votes.pending() if ':B:' in k]}")
    show_metrics(c.agents["C"])
    c.restore_agent("A")
    return never and flagged


def forge(c: LocalCluster) -> bool:
    banner("SCENARIO 3: agent A tries to forge evidence as B")
    before = c.agents["C"].agent_trust.get("A")
    c.compromise_agent("A", "D", mode="forge-evidence")
    time.sleep(3)
    c.restore_agent("A")
    rej = [r for r in c.agents["C"].rejections if r["claimed_signer"] == "B"]
    print(f"  C rejected {len(rej)} forged envelopes, e.g. {rej[0]['reason'] if rej else None}")
    print(f"  C's trust in A: {before:.1f} -> {c.agents['C'].agent_trust.get('A'):.1f}")
    return bool(rej)


def baseline() -> bool:
    banner("SCENARIO 4: centralized baseline under the same attacks")
    b = LocalBaseline()
    b.world.attack("C")
    b.backend.marker = {"id": "b1", "scenario": "app-compromise", "target": "C",
                        "attacker": None, "injected_at": time.time()}
    end = time.time() + 20
    while time.time() < end:
        b.ctl.tick()
        time.sleep(0.5)
        if b.ctl.states["C"].epoch > 0 and b.ctl.states["C"].phase == "HEALTHY":
            break
    print(f"  genuine attack handled: phase={b.ctl.states['C'].phase} epoch={b.ctl.states['C'].epoch}")
    b.backend.marker = {"id": "b2", "scenario": "false-accusation", "target": "B",
                        "attacker": "CENTRAL", "injected_at": time.time()}
    from resilience.simhooks import Compromise
    b.comp.set(Compromise(mode="false-accusation", target="B"))
    for _ in range(4):
        b.ctl.tick()
        time.sleep(0.5)
    print(f"  compromised controller isolated healthy B: {b.ctl.states['B'].epoch > 0}  <- single point of compromise")
    show_metrics(b.ctl)
    return b.ctl.states["B"].epoch > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="all",
                    choices=["all", "app-compromise", "false-accusation", "forge-evidence", "baseline"])
    ap.add_argument("--serve", action="store_true", help="keep agents running with /status on :50251-4")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.v else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    results = {}
    if a.scenario != "baseline":
        c = LocalCluster(with_status=a.serve).start()
        time.sleep(2)
        try:
            if a.scenario in ("all", "app-compromise"):
                results["app-compromise"] = app_compromise(c)
            if a.scenario in ("all", "false-accusation"):
                results["false-accusation"] = false_accusation(c)
            if a.scenario in ("all", "forge-evidence"):
                results["forge-evidence"] = forge(c)
            if a.serve:
                print("\nagents keep running; status at http://localhost:50251-50254/status (Ctrl-C to stop)")
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            c.stop()
    if a.scenario in ("all", "baseline"):
        results["baseline"] = baseline()
    banner("RESULTS")
    for k, v in results.items():
        print(f"  {k:18s} {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
