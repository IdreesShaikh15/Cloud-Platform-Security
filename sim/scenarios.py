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

NODES = ("A", "B", "C", "D")
WORKLOAD_C = "records-api"


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
    (Scenario("app-compromise", "Genuine compromise of records-api (C)",
              "Simulated exfiltration, file tampering and failed logins on node C.",
              "The quorum contains C, a clean replacement is validated, then C climbs back through every stage.", 40), app_compromise),
    (Scenario("false-accusation", "A compromised agent falsely accuses a healthy node",
              "Agent A signs fabricated evidence about auth-service (B).",
              "A lone liar cannot reach 3 votes: B is never isolated and A is flagged SUSPECT.", 25, True), false_accusation),
    (Scenario("forge-evidence", "An agent tries to forge another agent's signature",
              "Agent A claims to be B while signing with its own key.",
              "The forged envelopes are rejected and A's trust drops.", 12, True), forge_evidence),
    (Scenario("agent-crash", "Agent D crashes, then C is compromised",
              "One of four agents is killed, then C is attacked.",
              "The three survivors still form a quorum and finish the whole cycle. Press Reset afterwards (D stays down).", 45, True), agent_crash),
    (Scenario("transient-blip", "A short blip seen by two agents",
              "A brief connection spike on C that only A and B see.",
              "The agents investigate, close it as a false positive, and isolate nothing.", 20), transient_blip),
    (Scenario("slow-burn", "A weak anomaly that persists and grows",
              "Visible to A and B first, to C and D five seconds later.",
              "The investigation confirms it persists and the quorum contains C.", 40), slow_burn),
    (Scenario("ambiguous", "A weak persistent signal that only two agents confirm",
              "No attack is injected; the evidence stays unresolved.",
              "A human-review flag is raised; nothing is isolated.", 30), ambiguous),
    (Scenario("validation-retry", "The first replacement pod fails validation",
              "Then the retry passes.",
              "An evidence snapshot is saved, recovery is retried with back-off, and C is reintegrated.", 60), validation_retry),
    (Scenario("failed-validation", "Every replacement pod fails validation",
              "Recovery is retried a bounded number of times.",
              "The incident is marked NEEDS HUMAN ATTENTION and C stays quarantined. Press Reset afterwards.", 90, True), failed_validation),
]}


def listing() -> List[dict]:
    return [{"id": s.id, "title": s.title, "description": s.description, "expect": s.expect, "est_s": s.est_s}
            for s, _ in CATALOGUE.values()]
