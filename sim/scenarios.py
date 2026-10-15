"""Scenario drivers for the dashboard's simulator demo controls (sim/demo_server.py).

Each driver acts on an already running `LocalCluster` (real agents, simulated cluster and telemetry) and returns
(ok, detail). They only use the simulator's own mechanics: the fake world's attack/overlay hooks and the existing
agent-compromise switch. Nothing here can touch a real cluster: `LocalCluster` is built on `FakeBackend`/`FakeWorld`.
The same inputs as the terminal scenarios in sim/local_demo.py are used.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from world import WL, WORKLOADS  # noqa: E402  (names come from config/workloads.json)

NODES = ("A", "B", "C", "D")
WORKLOAD_C = WORKLOADS["C"]
D = {n: WL.display(n) for n in NODES}            # what the dashboard calls each workload (Patient Portal, ...)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    expect: str
    est_s: int
    degrades: bool = False           # leaves the cluster in a state that needs Reset before the next scenario


class Runner:
    """What a driver may use: the cluster, and cancellable waits."""

    def __init__(self, cluster, cancelled: Callable[[], bool]):
        self.c = cluster
        self.cancelled = cancelled

    def wait(self, pred, timeout: float, poll: float = 0.25) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.cancelled():
                return False
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(poll)
        return False

    def sleep(self, s: float) -> None:
        self.wait(lambda: False, s)

    def all(self, f, nodes=NODES) -> bool:
        return all(f(self.c.agents[n]) for n in nodes)


def app_compromise(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.mark("app-compromise", "C")
    c.world.attack("C")
    contained = r.wait(lambda: r.all(lambda a: a.states["C"].phase != "HEALTHY"), 25)
    full = contained and r.wait(lambda: r.all(lambda a: a.states["C"].phase == "HEALTHY" and a.states["C"].epoch == 1
                                              and a.states["C"].stage == "FULL"), 120)
    return bool(full), ("contained by quorum, recovered, validated and reintegrated through every stage" if full
                        else f"contained: {contained}; full cycle did not complete in time")


def false_accusation(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.compromise_agent("A", "B")
    flagged = r.wait(lambda: all(c.agents[n].agent_trust.get("A") < 50 for n in "BCD"), 60)
    r.sleep(2)
    never = all(a.states["B"].epoch == 0 for a in c.agents.values())
    c.restore_agent("A")
    return bool(flagged and never), (f"agent A flagged SUSPECT by B, C and D: {flagged}; B never isolated: {never}")


def forge_evidence(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.compromise_agent("A", "D", mode="forge-evidence")
    r.sleep(4)
    c.restore_agent("A")
    rej = [x for x in c.agents["C"].rejections if x["claimed_signer"] == "B"]
    return bool(rej), f"C rejected {len(rej)} forged envelope(s)"


def agent_crash(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.agents["D"].tick()
    c.crash_agent("D")
    c.mark("agent-crash", "C")
    c.world.attack("C")
    alive = "ABC"
    contained = r.wait(lambda: r.all(lambda a: a.states["C"].phase != "HEALTHY", alive), 25)
    full = contained and r.wait(lambda: r.all(lambda a: a.states["C"].phase == "HEALTHY" and a.states["C"].epoch == 1, alive), 120)
    return bool(full), ("A, B and C reached the 3-of-4 quorum without D and completed the full cycle" if full
                        else f"contained: {contained}; full cycle did not complete in time")


def transient_blip(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.mark("transient-blip", "C")
    c.world.inject_connections("C", lambda t: 40, observers={"A", "B"}, duration=2.5)
    done = r.wait(lambda: sum(1 for a in c.agents.values() if any(x["target"] == "C" for x in a.inv.recent)) >= 3, 25)
    never = all(a.states["C"].epoch == 0 for a in c.agents.values())
    return bool(done and never), f"investigated by the agents: {done}; C never isolated: {never}"


def slow_burn(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.mark("slow-burn", "C")
    grow = lambda t: 9 + 0.15 * t                                   # noqa: E731
    c.world.inject_connections("C", grow, observers={"A", "B"})
    c.world.inject_connections("C", grow, observers={"C", "D"}, start_after=5)
    contained = r.wait(lambda: sum(1 for a in c.agents.values() if a.states["C"].phase != "HEALTHY") >= 3, 60)
    return bool(contained), "a weak anomaly that persisted and grew was confirmed and contained" if contained else "not contained in time"


def ambiguous(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.mark("ambiguous", "C")
    c.world.inject_connections("C", lambda t: 9.5, observers={"A", "B"})
    done = r.wait(lambda: r.all(lambda a: "C" in a.inv.watch and not a.inv.active), 40)
    never = all(a.states["C"].epoch == 0 for a in c.agents.values())
    flagged = [n for n, a in c.agents.items() if a.inv.watch.get("C") and a.inv.watch["C"].review_needed]
    return bool(done and never and len(flagged) == 4), f"human-review flag on {len(flagged)} of 4 agents; nothing isolated: {never}"


def obstruct_facts(c, attacker: str = "A", target: str = "C") -> dict:
    """What happened to the CONTAIN decision on `target` while `attacker` withheld its votes (read from the agents themselves)."""
    honest = [n for n in NODES if n != attacker]
    voters = {n: next((d["voters"] for d in c.agents[n].decisions if d["action"] == "CONTAIN" and d["target"] == target), None) for n in honest}
    withheld = list(c.agents[attacker].obstructed)
    return {"contain_voters": voters, "attacker_voted": any(attacker in (v or []) for v in voters.values()),
            "withheld_events": len(withheld), "withheld_example": withheld[0] if withheld else "",
            "trust_in_attacker": {n: round(c.agents[n].agent_trust.get(attacker), 1) for n in honest}}


def obstruct(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.compromise_agent("A", "C", mode="obstruct")           # A refuses to vote about C (it changes only A's own behaviour)
    c.mark("obstruct", "C", attacker="A")
    c.world.attack("C")                                     # a genuine compromise of C
    honest = "BCD"
    contained = r.wait(lambda: r.all(lambda a: a.states["C"].phase != "HEALTHY", honest), 30)
    full = contained and r.wait(lambda: r.all(lambda a: a.states["C"].phase == "HEALTHY" and a.states["C"].epoch == 1
                                              and a.states["C"].stage == "FULL"), 120)
    f = obstruct_facts(c)
    c.restore_agent("A")
    ok = bool(full and not f["attacker_voted"] and f["withheld_events"] > 0 and all(v and len(v) >= 3 for v in f["contain_voters"].values()))
    return ok, (f"B, C and D contained C without A (voters {sorted(next(iter(f['contain_voters'].values())) or [])}); A's withheld votes are on record "
                f"({f['withheld_events']} events); full cycle completed: {bool(full)}" if ok else
                f"contained: {contained}; full cycle: {bool(full)}; A voted: {f['attacker_voted']}; withheld events: {f['withheld_events']}")


def validation_retry(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.world.bad_recoveries[WORKLOAD_C] = 1
    c.mark("validation-retry", "C")
    c.world.attack("C")
    ok = r.wait(lambda: r.all(lambda a: a.states["C"].epoch == 1 and a.states["C"].phase == "HEALTHY"
                              and a.states["C"].stage == "FULL"), 150)
    att = {n: a.states["C"].attempt for n, a in c.agents.items()}
    return bool(ok and set(att.values()) == {2}), f"first replacement failed validation, second passed (attempts {att})"


def failed_validation(r: Runner) -> Tuple[bool, str]:
    c = r.c
    c.world.bad_recoveries[WORKLOAD_C] = 99
    c.mark("failed-validation", "C")
    c.world.attack("C")
    ok = r.wait(lambda: r.all(lambda a: a.states["C"].attention), 150)
    quarantined = (c.backend.policies.get(WORKLOAD_C) or {}).get("stage") == "QUARANTINE"
    return bool(ok and quarantined), f"needs human attention on all agents: {ok}; still quarantined: {quarantined}"


CATALOGUE: Dict[str, Tuple[Scenario, Callable[[Runner], Tuple[bool, str]]]] = {s.id: (s, f) for s, f in [
    (Scenario("app-compromise", f"Patient records being copied out of the {D['C']}",
              f"Simulated copying of (synthetic) patient records out of the {D['C']}, a tampered file and repeated failed logins.",
              f"Three of the four security agents agree to isolate the {D['C']}; a clean replacement is checked; normal access is restored in stages.", 40), app_compromise),
    (Scenario("false-accusation", f"A security agent falsely accuses the {D['B']}",
              f"Agent A, which is compromised, signs made-up evidence that the {D['B']} is under attack.",
              f"One liar cannot reach 3 agreeing agents: the {D['B']} is never isolated and agent A is marked SUSPECT.", 25, True), false_accusation),
    (Scenario("forge-evidence", "A compromised security agent pretends to be another agent",
              "Agent A claims to be agent B while signing with its own key.",
              "The forged messages fail verification and are rejected; A's trust drops.", 12, True), forge_evidence),
    (Scenario("agent-crash", f"A security agent goes offline, then the {D['C']} is attacked",
              f"One of the four agents is stopped, then the {D['C']} is attacked.",
              "The three remaining agents still reach agreement and finish the whole cycle. Press Reset afterwards (agent D stays down).", 45, True), agent_crash),
    (Scenario("transient-blip", f"A short traffic spike on the {D['C']} seen by two agents",
              f"A brief jump in connections from the {D['C']} that only agents A and B see, like a busy moment.",
              "The agents investigate, close it as a false alarm and isolate nothing.", 20), transient_blip),
    (Scenario("slow-burn", f"Slow, quiet copying out of the {D['C']}",
              "A weak signal that A and B see first and C and D see five seconds later, and that keeps growing.",
              f"The investigation confirms it persists and the agents agree to isolate the {D['C']}.", 40), slow_burn),
    (Scenario("ambiguous", f"An unexplained weak signal on the {D['C']}",
              "No attack is injected; only two agents keep seeing something faint.",
              "A human-review flag is raised; nothing is isolated.", 30), ambiguous),
    (Scenario("validation-retry", f"The first replacement {D['C']} fails its health check",
              "The attack is real; the first clean replacement does not come up healthy.",
              "Evidence is saved, recovery is retried, the second replacement passes and normal access is restored in stages.", 60), validation_retry),
    (Scenario("failed-validation", f"Every replacement {D['C']} fails its health check",
              "Recovery is retried a limited number of times.",
              f"The incident is marked NEEDS HUMAN ATTENTION and the {D['C']} stays isolated. Press Reset afterwards.", 90, True), failed_validation),
    (Scenario("obstruct", f"A compromised agent refuses to vote while the {D['C']} is attacked",
              f"Agent A withholds every vote about the {D['C']} while its records are being copied out. No made-up evidence is sent: A simply does not take part.",
              f"The other three agents still reach agreement and isolate and recover the {D['C']}. A's withheld votes are shown with the reason. "
              "A's trust is not reduced (the platform has no rule for a missing vote).", 45), obstruct),
]}


def listing() -> List[dict]:
    return [{"id": s.id, "title": s.title, "description": s.description, "expect": s.expect, "est_s": s.est_s}
            for s, _ in CATALOGUE.values()]
