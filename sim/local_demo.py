#!/usr/bin/env python3
"""Run the platform end-to-end on your laptop, no Kubernetes required.

    python3 sim/local_demo.py                 # all scenarios
    python3 sim/local_demo.py app-compromise  # just one
    python3 sim/local_demo.py --serve         # keep running + expose /status on :50251-50254
                                              # (point the dashboard at them, see demo.md)

Scenarios
  app-compromise     genuine compromise of Node C's workload (exfil+tamper+bruteforce)
  false-accusation   agent A is compromised and accuses healthy Node B
  forge-evidence     agent A tries to impersonate B's signature
  agent-crash        agent D is killed, then Node C is genuinely compromised: A, B, C
                     still reach 3-of-4 quorum and complete the full pipeline without D
  transient-blip     one signal spikes briefly on one workload, seen by only some agents, then
                     stops: investigation, closed as a false positive, no isolation
  slow-burn          a weak anomaly seen by only some agents at first, which persists and grows:
                     investigation confirms it, containment follows
  ambiguous          a weak, persistent signal only some agents confirm: reversible WATCH state
                     plus a human-review flag, no isolation
                     (add --no-investigation to switch the feature off for ANY scenario, to compare)
  validation-retry   Node C is compromised and the FIRST replacement pod fails its health check:
                     an evidence snapshot is saved, recovery is retried with back-off, the second
                     replacement validates, and C is reintegrated (never shown healthy before that)
  failed-validation  every replacement pod of Node C fails validation: bounded retries, then the
                     incident is marked NEEDS HUMAN ATTENTION; C stays quarantined, never healthy
  baseline           the centralized controller under the same two attacks
  controller-crash   the centralized controller is killed, then Node C is genuinely
                     compromised: no detection or response occurs at all (single point
                     of failure)
Timers are accelerated (0.5 s ticks, 3 s stage dwell) so a full cycle takes ~30 s.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from world import LocalBaseline, LocalCluster, fast_config, sim_investigation  # noqa: E402

import grpc  # noqa: E402 - after world.py has put agent/ on sys.path
from resilience.proto import resilience_pb2 as pb  # noqa: E402


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


def agent_crash(c: LocalCluster) -> bool:
    banner("SCENARIO 5: agent D crashes, then Node C is genuinely compromised")
    # This scenario tests fault tolerance to a crashed peer, not trust
    # recovery timing or evidence-window expiry from an earlier scenario.
    # The forge-evidence scenario (run just before this one in "all") has A
    # genuinely, validly sign fabricated evidence about D; that evidence sits
    # in every peer's pool for the full evidence window and correctly keeps
    # decaying A's trust for as long as it's there. Resetting the trust score
    # alone isn't enough - the still-fresh fabricated evidence would just
    # decay it right back down within seconds. Clear it too, using the same
    # per-target pool-clearing the platform already does on every real
    # containment (EvidencePool.clear_target), so this scenario starts clean.
    # First let any evidence broadcast from that prior scenario, still
    # in-flight on the network (broadcast() is fire-and-forget), actually
    # land - otherwise it can arrive just after our clear and undo it.
    time.sleep(2.0)
    for a in c.agents.values():
        for other in c.cfg.nodes:
            if other != a.id:
                a.agent_trust.set(other, 100.0)
            a.pool.clear_target(other)
    c.agents["D"].tick()  # make sure D has run at least once before we kill it
    c.crash_agent("D")

    try:
        c.agents["A"].transport.stubs["D"].Ping(pb.PingRequest(from_node="A"), timeout=2.0)
        down = False
    except grpc.RpcError:
        down = True
    print(f"  agent D crashed; A can no longer reach D over gRPC: {down}")

    # Node C may already have an incident history from an earlier scenario
    # (e.g. app-compromise), so compare epochs relatively, not against a
    # hardcoded absolute value.
    epoch_before = c.agents["A"].states["C"].epoch
    c.mark("agent-crash", "C", attacker="D")
    c.world.attack("C")
    ok = c.wait_until(lambda: all(c.agents[n].states["C"].phase != "HEALTHY" for n in "ABC"), 15)
    survivors = c.agents["B"].states["C"].last_decision
    quorum_excludes_d = bool(survivors) and "D" not in survivors["voters"]
    print(f"  contained using only surviving agents: {ok}  decision: {survivors}")
    print(f"  D (crashed) did not vote: {quorum_excludes_d}")
    ok2 = c.wait_until(lambda: all(c.agents[n].states["C"].phase == "HEALTHY"
                                   and c.agents[n].states["C"].epoch == epoch_before + 1 for n in "ABC"), 90)
    print(f"  A, B, C completed the full pipeline (D still down): {ok2}")
    show_metrics(c.agents["B"])
    return down and ok and ok2 and quorum_excludes_d and (len(survivors["voters"]) >= 3 if survivors else False)



# ---- investigation scenarios: each runs on its own fresh 4-agent cluster ------------
_INV_PORT = [50801]


def _inv_cluster(enabled: bool) -> LocalCluster:
    port = _INV_PORT[0]
    _INV_PORT[0] += 10
    c = LocalCluster(base_port=port, cfg=fast_config(port, sim_investigation(enabled=enabled))).start()
    time.sleep(2)
    return c


def _outcomes(c: LocalCluster, target: str) -> dict:
    return {n: [r["outcome"] for r in a.inv.recent if r["target"] == target] for n, a in c.agents.items()}


def _show_inv(c: LocalCluster, target: str) -> None:
    for n, a in c.agents.items():
        st = a.inv.status()
        print(f"  [{n}] investigations={_outcomes(c, target)[n]} watch={list(st['watch'])} "
              f"epoch={a.states[target].epoch}")
    row = c.agents["B"].metrics.summary()[0]
    keep = {k: row[k] for k in ("scenario", "target", "ttd_s", "tti_s", "inv_start_s", "inv_end_s",
                                "investigations", "investigation_outcome", "human_review",
                                "false_isolation") if row.get(k) is not None}
    print(f"  [B] {json.dumps(keep)}")


def transient_blip(enabled: bool = True) -> bool:
    banner(f"SCENARIO 7: transient blip on Node C, seen by agents A and B only "
           f"(investigation {'ON' if enabled else 'OFF'})")
    c = _inv_cluster(enabled)
    try:
        c.mark("transient-blip", "C")
        c.world.inject_connections("C", lambda t: 40, observers={"A", "B"}, duration=2.5)
        if enabled:
            done = c.wait_until(lambda: sum(1 for v in _outcomes(c, "C").values() if v) >= 3, 25)
        else:
            time.sleep(12)
            done = True
        _show_inv(c, "C")
        never = all(a.states["C"].epoch == 0 for a in c.agents.values())
        print(f"  C never isolated: {never}")
        if not enabled:
            print(f"  pending CONTAIN votes left hanging: {[k for k in c.agents['C'].votes.pending() if ':C:' in k]}")
            return never
        fp = sum(1 for v in _outcomes(c, "C").values() if v and v[-1] == "FALSE_POSITIVE")
        print(f"  agents that closed it as a false positive: {fp}")
        return done and never and fp >= 3
    finally:
        c.stop()


def slow_burn(enabled: bool = True) -> bool:
    banner(f"SCENARIO 8: slow burn on Node C: weak anomaly, visible to A,B first, C,D after 5s, "
           f"growing (investigation {'ON' if enabled else 'OFF'})")
    c = _inv_cluster(enabled)
    try:
        c.mark("slow-burn", "C")
        grow = lambda t: 9 + 0.15 * t          # noqa: E731  connections: 9 -> 12 in 20 s
        c.world.inject_connections("C", grow, observers={"A", "B"})
        c.world.inject_connections("C", grow, observers={"C", "D"}, start_after=5)
        t0 = time.time()
        contained = c.wait_until(lambda: sum(1 for a in c.agents.values()
                                             if a.states["C"].phase != "HEALTHY") >= 3, 45)
        print(f"  contained: {contained} after {time.time() - t0:.1f}s "
              f"(weak signals alone never reach W>=0.6 until they grow past ~11 connections)")
        _show_inv(c, "C")
        d = c.agents["B"].states["C"].last_decision
        if d:
            print(f"  decision signed by {d['voters']}; votes cite investigations: "
                  f"{sorted({v.get('investigation_id', '') for v in []}) or 'see events'}")
        return contained
    finally:
        c.stop()


def ambiguous(enabled: bool = True) -> bool:
    banner(f"SCENARIO 9: weak persistent signal confirmed by only A and B "
           f"(investigation {'ON' if enabled else 'OFF'})")
    c = _inv_cluster(enabled)
    try:
        c.mark("ambiguous", "C")
        c.world.inject_connections("C", lambda t: 9.5, observers={"A", "B"})
        if enabled:
            done = c.wait_until(lambda: all("C" in a.inv.watch and not a.inv.active
                                            for a in c.agents.values()), 30)
        else:
            time.sleep(12)
            done = True
        _show_inv(c, "C")
        never = all(a.states["C"].epoch == 0 for a in c.agents.values())
        print(f"  C never isolated: {never}")
        if not enabled:
            return never
        flagged = [n for n, a in c.agents.items() if a.inv.watch.get("C") and a.inv.watch["C"].review_needed]
        print(f"  agents showing 'human review needed': {flagged}")
        row = c.agents["B"].metrics.summary()[0]
        return done and never and len(flagged) == 4 and row["human_review"] is True
    finally:
        c.stop()


def _rec_cluster(bad_recoveries: int, port: int) -> LocalCluster:
    c = LocalCluster(base_port=port, cfg=fast_config(port, validate_timeout_s=6)).start()
    time.sleep(2)
    c.world.bad_recoveries["records-api"] = bad_recoveries
    return c


def _recoveries(c: LocalCluster) -> int:
    return len([x for x in c.backend.log if x[1] == "recover"])


def _snapshot_report(c: LocalCluster) -> tuple:
    """(tags captured by agent B, whether any snapshot text contains the simulated secret)."""
    snaps = list(c.agents["B"].forensics.items.values())
    tags = sorted(s["incident"]["tag"] for s in snaps)
    leaked = any("SIMULATED-SECRET" in json.dumps(s) for a in c.agents.values()
                 for s in a.forensics.items.values())
    return tags, leaked


def validation_retry() -> bool:
    banner("SCENARIO: first replacement of Node C fails validation, the second passes")
    c = _rec_cluster(1, 50701)
    try:
        c.mark("validation-retry", "C")
        c.world.attack("C")
        ok = c.wait_until(lambda: all(a.states["C"].epoch == 1 and a.states["C"].phase == "HEALTHY"
                                      and a.states["C"].stage == "FULL" for a in c.agents.values()), 150)
        att = {n: a.states["C"].attempt for n, a in c.agents.items()}
        tags, leaked = _snapshot_report(c)
        rec = _recoveries(c)
        ev = [e for e in c.agents["B"].events.by_category("QUORUM") if "redeploy" in e["summary"]]
        print(f"  reintegrated after retry: {ok}  attempts={att}  redeploys={rec}  snapshots={tags}  "
              f"secret leaked into a snapshot: {leaked}")
        unknown = any(a.audit.unresolved for a in c.agents.values())
        return bool(ok and set(att.values()) == {2} and rec == 2 and "failed1" in tags and "contain" in tags
                    and not leaked and not unknown and ev)
    finally:
        c.stop()


def failed_validation() -> bool:
    banner("SCENARIO: every replacement of Node C fails validation -> human attention")
    c = _rec_cluster(99, 50711)
    try:
        c.mark("failed-validation", "C")
        c.world.attack("C")
        ok = c.wait_until(lambda: all(a.states["C"].attention for a in c.agents.values()), 150)
        time.sleep(3)
        att = {n: a.states["C"].attempt for n, a in c.agents.items()}
        phases = c.phases("C")
        tags, leaked = _snapshot_report(c)
        rec = _recoveries(c)
        quarantined = (c.backend.policies.get("records-api") or {}).get("stage") == "QUARANTINE"
        print(f"  needs human attention on all agents: {ok}  attempts={att}  phases={phases}  redeploys={rec}  "
              f"still quarantined: {quarantined}  snapshots={tags}  secret leaked: {leaked}")
        print("  reason:", c.agents["B"].states["C"].attention)
        return bool(ok and rec == 3 and set(att.values()) == {3} and quarantined
                    and all(p != "HEALTHY" for p in phases.values()) and not leaked
                    and {"contain", "failed1", "failed2"} <= set(tags))
    finally:
        c.stop()


def controller_crash(with_status: bool = False) -> bool:
    banner("SCENARIO 6: centralized controller crashes, then Node C is genuinely compromised")
    b = LocalBaseline(with_status=with_status).start()
    time.sleep(1)
    b.crash()
    print("  central controller crashed (single point of failure)")

    marker = {"id": uuid.uuid4().hex[:8], "scenario": "controller-crash", "target": "C",
             "attacker": None, "injected_at": time.time()}
    b.backend.marker = marker
    # The crashed controller's own tick loop never runs, so it would never
    # pick the marker up (that IS the finding) - record it directly here so
    # the incident, and the fact that nothing happened after it, is logged.
    b.ctl.metrics.set_marker(marker)
    b.world.attack("C")
    time.sleep(8)  # long enough that a live controller would easily have detected+contained this
    no_response = b.ctl.states["C"].phase == "HEALTHY" and b.ctl.states["C"].epoch == 0
    b.ctl.metrics.event("no_response_confirmed", time.time(), "C", crashed=True)
    print(f"  no detection/response occurred while the controller was down: {no_response}")
    show_metrics(b.ctl)
    if with_status:
        print("\ncontroller (crashed) stays up for inspection; status at http://localhost:50261/status (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    b.stop()
    return no_response


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


CLUSTER_SCENARIOS = ("app-compromise", "false-accusation", "forge-evidence", "agent-crash")
INVESTIGATION_SCENARIOS = ("transient-blip", "slow-burn", "ambiguous")
RECOVERY_SCENARIOS = ("validation-retry", "failed-validation")
BASELINE_SCENARIOS = ("baseline", "controller-crash")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="all",
                    choices=["all", *CLUSTER_SCENARIOS, *INVESTIGATION_SCENARIOS, *RECOVERY_SCENARIOS,
                             *BASELINE_SCENARIOS])
    ap.add_argument("--no-investigation", action="store_true",
                    help="run the investigation scenarios with the feature switched off (comparison)")
    ap.add_argument("--serve", action="store_true",
                    help="keep agents/controller running with /status exposed "
                         "(agents on :50251-4, baseline controller on :50261)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.v else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    results = {}
    inv_on = not a.no_investigation
    if a.scenario in ("all", *CLUSTER_SCENARIOS):
        c = LocalCluster(with_status=a.serve,
                         cfg=fast_config(50151, sim_investigation(enabled=inv_on))).start()
        time.sleep(2)
        try:
            if a.scenario in ("all", "app-compromise"):
                results["app-compromise"] = app_compromise(c)
            if a.scenario in ("all", "false-accusation"):
                results["false-accusation"] = false_accusation(c)
            if a.scenario in ("all", "forge-evidence"):
                results["forge-evidence"] = forge(c)
            # agent-crash kills node D for the rest of this cluster's life, so
            # it must run last among the scenarios that share this LocalCluster.
            if a.scenario in ("all", "agent-crash"):
                results["agent-crash"] = agent_crash(c)
            if a.serve:
                print("\nagents keep running; status at http://localhost:50251-50254/status (Ctrl-C to stop)")
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            c.stop()
    if a.scenario in ("all", "transient-blip"):
        results["transient-blip"] = transient_blip(inv_on)
    if a.scenario in ("all", "slow-burn"):
        results["slow-burn"] = slow_burn(inv_on)
    if a.scenario in ("all", "ambiguous"):
        results["ambiguous"] = ambiguous(inv_on)
    if a.scenario in ("all", "validation-retry"):
        results["validation-retry"] = validation_retry()
    if a.scenario in ("all", "failed-validation"):
        results["failed-validation"] = failed_validation()
    if a.scenario in ("all", "baseline"):
        results["baseline"] = baseline()
    if a.scenario in ("all", "controller-crash"):
        # --serve only blocks-and-serves for a single, explicitly chosen scenario
        # (matching the cluster branch above), not when running "all" of them.
        results["controller-crash"] = controller_crash(with_status=a.serve and a.scenario == "controller-crash")
    banner("RESULTS")
    for k, v in results.items():
        print(f"  {k:18s} {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
