"""Scenario drivers for the dashboard's simulator demo controls (sim/demo_server.py).

Each driver acts on an already running `LocalCluster` (real agents, simulated cluster and telemetry) and returns
(ok, detail). They only use the simulator's own mechanics: the fake world's attack/overlay hooks and the existing
agent-compromise switch. Nothing here can touch a real cluster: `LocalCluster` is built on `FakeBackend`/`FakeWorld`.

Every scenario now follows the same shape, so a demo is easy to follow:
  1. a lead-in (5 s, a countdown on the page) with everything healthy;
  2. a scripted attack that PROGRESSES over several seconds (a weak signal first, the decisive one later), so the audience sees evidence build up;
     the agents are never slowed down: they react as soon as their own rules say so;
  3. the response and recovery, exactly as before.
The service the attack hits can be chosen (`target`, one of the workloads A-F); the default is the one the old fixed scenario used.
The same inputs as the terminal scenarios in sim/local_demo.py are used (those call the drivers without a lead-in).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from world import WL, WORKLOADS  # noqa: E402  (names come from config/workloads.json)

NODES = ("A", "B", "C", "D")
WORKLOAD_C = WORKLOADS["C"]
TARGETS = tuple(WL.nodes())                      # every workload: A-D, plus E and F in the six-workload profile
D = {n: WL.display(n) for n in TARGETS}          # what the dashboard calls each workload (Patient Portal, ...)
SIX = len(TARGETS) == 6
LEAD_IN_S = 5.0                                  # the dashboard starts every scenario with this healthy countdown


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    expect: str
    est_s: int
    degrades: bool = False           # leaves the cluster in a state that needs Reset before the next scenario
    default_target: str = ""         # the workload the scenario hits unless the presenter chooses another ("" = no service is attacked)
    targets: Tuple[str, ...] = ()    # the workloads that may be chosen (empty = none: the scenario is not about one service)
    title_tpl: str = ""              # title / description / expectation with {svc} for the chosen service's name
    description_tpl: str = ""
    expect_tpl: str = ""

    def text(self, target: Optional[str] = None) -> dict:
        svc = D.get(target or self.default_target, "")
        f = lambda tpl, plain: tpl.format(svc=svc) if tpl else plain      # noqa: E731
        return {"title": f(self.title_tpl, self.title), "description": f(self.description_tpl, self.description),
                "expect": f(self.expect_tpl, self.expect)}


def other(exclude: str, prefer: str) -> str:
    """An agent id that is not `exclude` (the presenter may aim the attack at the service next to the agent that would otherwise play the liar)."""
    return next(n for n in (prefer, *[x for x in NODES if x != prefer]) if n != exclude)


class Runner:
    """What a driver may use: the cluster, cancellable waits, the chosen target and (in the dashboard) the run recorder."""

    def __init__(self, cluster, cancelled: Callable[[], bool], target: Optional[str] = None, run=None, lead_in_s: float = 0.0):
        self.c = cluster
        self.cancelled = cancelled
        self.target = target
        self.run = run                  # sim/runlog.RunRecorder or None (terminal use)
        self.lead_in_s = lead_in_s

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

    # -- the shape every scenario shares
    def note(self, label: str, kind: str = "attack") -> None:
        if self.run is not None:
            self.run.note(label, kind)

    def phase(self, name: str) -> None:
        if self.run is not None:
            self.run.set_phase(name)

    def lead_in(self) -> None:
        """Everything healthy for a few seconds (the page counts down), then the attack begins."""
        self.phase("lead-in")
        self.sleep(self.lead_in_s)
        self.phase("attack")

    def script(self, steps: List[Tuple[float, str, Callable[[], None]]]) -> None:
        """Run the attack steps at their offsets (seconds after the attack began), noting each on the run's timeline."""
        t0 = time.time()
        for off, label, fn in sorted(steps, key=lambda x: x[0]):
            self.sleep(max(0.0, t0 + off - time.time()))
            if self.cancelled():
                return
            fn()
            self.note(label)


def name(n: str) -> str:
    return D.get(n, n)


def _attack_steps(r: Runner, v: str) -> None:
    """The common attack: outbound connections creep up (weak, below the vote level on their own), then a file is changed and a hidden process starts
    copying records out (decisive). The agents decide on what they measure, when they measure it."""
    w = r.c.world
    r.script([
        (0.0, f"Outbound connections from the {name(v)} start creeping up", lambda: w.inject_connections(v, lambda t: 8.5 + 0.35 * t)),
        (5.0, "A file is modified and a hidden process starts copying records out", lambda: w.attack(v, kinds=("tamper", "exfil"))),
    ])


def _contained_and_back(r: Runner, target: str, contain_s: float = 30, full_s: float = 120):
    contained = r.wait(lambda: r.all(lambda a: a.states[target].phase != "HEALTHY"), contain_s)
    if contained:
        r.phase("recovery")
    full = contained and r.wait(lambda: r.all(lambda a: a.states[target].phase == "HEALTHY" and a.states[target].epoch == 1
                                              and a.states[target].stage == "FULL"), full_s)
    return contained, bool(full)


def _nothing_else_isolated(r: Runner, v: str) -> bool:
    return all(a.states[n].epoch == 0 for a in r.c.agents.values() for n in TARGETS if n != v)


# ---------------------------------------------------------------------- the scenarios
def app_compromise(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    c.mark("app-compromise", v)
    r.lead_in()
    _attack_steps(r, v)
    contained, full = _contained_and_back(r, v, 25, 120)
    return full, ("contained by quorum, recovered, validated and reintegrated through every stage" if full
                  else f"contained: {contained}; full cycle did not complete in time")


def false_accusation(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "B"
    liar = other(v, "A")
    judges = [n for n in NODES if n != liar]
    if r.run is not None:
        r.run.meta["roles"] = {"liar": liar}
    r.lead_in()
    c.compromise_agent(liar, v)
    r.note(f"Agent {liar} (compromised) starts signing made-up evidence against the {name(v)}")
    flagged = r.wait(lambda: all(c.agents[n].agent_trust.get(liar) < 50 for n in judges), 60)
    r.sleep(2)
    never = all(a.states[v].epoch == 0 for a in c.agents.values())
    c.restore_agent(liar)
    return bool(flagged and never), (f"agent {liar} flagged SUSPECT by {', '.join(judges[:-1])} and {judges[-1]}: {flagged}; the {name(v)} never isolated: {never}")


def forge_evidence(r: Runner) -> Tuple[bool, str]:
    c = r.c
    if r.run is not None:
        r.run.meta["roles"] = {"liar": "A"}
    r.lead_in()
    c.compromise_agent("A", "D", mode="forge-evidence")
    r.note("Agent A starts sending messages that claim to come from agent B")
    r.sleep(4)
    c.restore_agent("A")
    rej = [x for x in c.agents["C"].rejections if x["claimed_signer"] == "B"]
    return bool(rej), f"C rejected {len(rej)} forged envelope(s)"


def agent_crash(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    crashed = other(v, "D")
    alive = [n for n in NODES if n != crashed]
    c.mark("agent-crash", v)
    r.lead_in()
    c.agents[crashed].tick()
    c.crash_agent(crashed)
    r.note(f"Agent {crashed} goes offline")
    r.sleep(3)
    _attack_steps(r, v)
    contained = r.wait(lambda: r.all(lambda a: a.states[v].phase != "HEALTHY", alive), 25)
    if contained:
        r.phase("recovery")
    full = contained and r.wait(lambda: r.all(lambda a: a.states[v].phase == "HEALTHY" and a.states[v].epoch == 1, alive), 120)
    return bool(full), (f"{', '.join(alive[:-1])} and {alive[-1]} reached the 3-of-4 quorum without {crashed} and completed the full cycle" if full
                        else f"contained: {contained}; full cycle did not complete in time")


def transient_blip(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    c.mark("transient-blip", v)
    r.lead_in()
    r.script([(0.0, "A brief spike: 40 connections for 2.5 s, seen only by agents A and B",
               lambda: c.world.inject_connections(v, lambda t: 40, observers={"A", "B"}, duration=2.5))])
    done = r.wait(lambda: sum(1 for a in c.agents.values() if any(x["target"] == v for x in a.inv.recent)) >= 3, 25)
    never = all(a.states[v].epoch == 0 for a in c.agents.values())
    return bool(done and never), f"investigated by the agents: {done}; the {name(v)} never isolated: {never}"


def slow_burn(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    c.mark("slow-burn", v)
    grow = lambda t: 9 + 0.15 * t                                   # noqa: E731
    r.lead_in()
    r.script([
        (0.0, "A weak, steadily growing signal starts; agents A and B see it", lambda: c.world.inject_connections(v, grow, observers={"A", "B"})),
        (5.0, "Agents C and D now see it too", lambda: c.world.inject_connections(v, grow, observers={"C", "D"})),
    ])
    contained = r.wait(lambda: sum(1 for a in c.agents.values() if a.states[v].phase != "HEALTHY") >= 3, 60)
    if contained:
        r.phase("recovery")
    full = contained and r.wait(lambda: r.all(lambda a: a.states[v].phase == "HEALTHY" and a.states[v].epoch == 1 and a.states[v].stage == "FULL"), 120)
    return bool(contained), (("a weak anomaly that persisted and grew was confirmed and contained" + ("; recovered and returned in stages" if full else "")) if contained
                             else "not contained in time")


def ambiguous(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    c.mark("ambiguous", v)
    r.lead_in()
    r.script([(0.0, "A faint, steady signal (9.5 connections) that only agents A and B see",
               lambda: c.world.inject_connections(v, lambda t: 9.5, observers={"A", "B"}))])
    done = r.wait(lambda: r.all(lambda a: v in a.inv.watch and not a.inv.active), 40)
    never = all(a.states[v].epoch == 0 for a in c.agents.values())
    flagged = [n for n, a in c.agents.items() if a.inv.watch.get(v) and a.inv.watch[v].review_needed]
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
    c, v = r.c, r.target or "C"
    attacker = other(v, "A")
    honest = [n for n in NODES if n != attacker]
    if r.run is not None:
        r.run.meta["roles"] = {"attacker": attacker}
    c.compromise_agent(attacker, v, mode="obstruct")        # the attacker refuses to vote about v (it changes only its own behaviour)
    c.mark("obstruct", v, attacker=attacker)
    r.lead_in()
    r.note(f"Agent {attacker} is compromised and will withhold every vote about the {name(v)}", "setup")
    _attack_steps(r, v)                                      # a genuine compromise of v
    contained = r.wait(lambda: r.all(lambda a: a.states[v].phase != "HEALTHY", honest), 30)
    if contained:
        r.phase("recovery")
    full = contained and r.wait(lambda: r.all(lambda a: a.states[v].phase == "HEALTHY" and a.states[v].epoch == 1
                                              and a.states[v].stage == "FULL", honest), 120)
    f = obstruct_facts(c, attacker, v)
    c.restore_agent(attacker)
    ok = bool(full and not f["attacker_voted"] and f["withheld_events"] > 0 and all(x and len(x) >= 3 for x in f["contain_voters"].values()))
    return ok, (f"{', '.join(honest[:-1])} and {honest[-1]} contained the {name(v)} without {attacker} (voters {sorted(next(iter(f['contain_voters'].values())) or [])}); "
                f"{attacker}'s withheld votes are on record ({f['withheld_events']} events); full cycle completed: {bool(full)}" if ok else
                f"contained: {contained}; full cycle: {bool(full)}; {attacker} voted: {f['attacker_voted']}; withheld events: {f['withheld_events']}")


def validation_retry(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    c.world.bad_recoveries[WORKLOADS[v]] = 1
    c.mark("validation-retry", v)
    r.lead_in()
    _attack_steps(r, v)
    ok = r.wait(lambda: r.all(lambda a: a.states[v].epoch == 1 and a.states[v].phase == "HEALTHY" and a.states[v].stage == "FULL"), 150)
    att = {n: a.states[v].attempt for n, a in c.agents.items()}
    return bool(ok and set(att.values()) == {2}), f"first replacement failed validation, second passed (attempts {att})"


def failed_validation(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or "C"
    c.world.bad_recoveries[WORKLOADS[v]] = 99
    c.mark("failed-validation", v)
    r.lead_in()
    _attack_steps(r, v)
    ok = r.wait(lambda: r.all(lambda a: a.states[v].attention), 150)
    quarantined = (c.backend.policies.get(WORKLOADS[v]) or {}).get("stage") == "QUARANTINE"
    return bool(ok and quarantined), f"needs human attention on all agents: {ok}; still quarantined: {quarantined}"


def file_tampering(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or WL.node_of_role("lab-results")
    c.mark("lab-results-tampered", v)
    r.lead_in()
    r.script([
        (0.0, f"A few odd outbound connection attempts from the {name(v)} (still below the alert level)",
         lambda: c.world.inject_connections(v, lambda t: 2 + 0.6 * t)),
        (6.0, f"A file inside the {name(v)} is modified at runtime", lambda: c.world.attack(v, kinds=("tamper",))),
    ])
    contained, full = _contained_and_back(r, v)
    others = _nothing_else_isolated(r, v)
    return bool(full and others), (f"{name(v)} contained by 3-of-4 agreement, recovered and returned in stages: {full}; "
                                   f"no other workload isolated: {others}")


def failed_logins(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or WL.node_of_role("medication-service")
    c.mark("medication-failed-logins", v)
    r.lead_in()
    r.script([
        (0.0, f"A few odd outbound connection attempts from the {name(v)} (still below the alert level)",
         lambda: c.world.inject_connections(v, lambda t: 2 + 0.6 * t)),
        (6.0, f"The {name(v)} starts guessing passwords at the Login & Access Service", lambda: c.world.attack(v, kinds=("bruteforce",))),
    ])
    contained, full = _contained_and_back(r, v)
    others = _nothing_else_isolated(r, v)
    return bool(full and others), (f"repeated failed logins from the {name(v)} confirmed and contained: {contained}; "
                                   f"recovered and returned in stages: {full}; no other workload isolated: {others}")


def dependency_impact(r: Runner) -> Tuple[bool, str]:
    c, v = r.c, r.target or WL.node_of_role("database")
    dependants = [n for n in TARGETS if v in WL.node_depends_on(n)]
    c.mark("database-cascade", v)
    r.lead_in()
    _attack_steps(r, v)
    contained, full = _contained_and_back(r, v, 30, 150)
    others = _nothing_else_isolated(r, v)
    who = (f"the {len(dependants)} service{'s' if len(dependants) != 1 else ''} that depend{'' if len(dependants) != 1 else 's'} on it ({', '.join(name(n) for n in dependants)}) {'were' if len(dependants) != 1 else 'was'} not isolated: {others}" if dependants
           else f"it has no dependants; nothing else was isolated: {others}")
    return bool(full and others), f"{name(v)} contained and recovered: {full}; {who}"


ALL = tuple(WL.nodes())


def _S(id, title_tpl, description_tpl, expect_tpl, est, driver, default, degrades=False, targets=ALL):
    s = Scenario(id, title_tpl.format(svc=D.get(default, "")), description_tpl.format(svc=D.get(default, "")), expect_tpl.format(svc=D.get(default, "")),
                 est, degrades, default, targets if default else (), title_tpl, description_tpl, expect_tpl)
    return s, driver


CATALOGUE: Dict[str, Tuple[Scenario, Callable[[Runner], Tuple[bool, str]]]] = {s.id: (s, f) for s, f in [
    _S("app-compromise", "Patient records being copied out of the {svc}",
       "Simulated copying of (synthetic) patient records out of the {svc}: outbound connections creep up, then a file is tampered with and a hidden process starts copying.",
       "Three of the four security agents agree to isolate the {svc}; a clean replacement is checked; normal access is restored in stages.", 40, app_compromise, "C"),
    _S("false-accusation", "A security agent falsely accuses the {svc}",
       "A compromised agent signs made-up evidence that the {svc} is under attack.",
       "One liar cannot reach 3 agreeing agents: the {svc} is never isolated and the lying agent is marked SUSPECT.", 30, false_accusation, "B", True),
    _S("forge-evidence", "A compromised security agent pretends to be another agent",
       "Agent A claims to be agent B while signing with its own key.",
       "The forged messages fail verification and are rejected; A's trust drops.", 15, forge_evidence, "", True),
    _S("agent-crash", "A security agent goes offline, then the {svc} is attacked",
       "One of the four agents is stopped, then the {svc} is attacked.",
       "The three remaining agents still reach agreement and finish the whole cycle. Press Reset afterwards (the stopped agent stays down).", 45, agent_crash, "C", True),
    _S("transient-blip", "A short traffic spike on the {svc} seen by two agents",
       "A brief jump in connections from the {svc} that only agents A and B see, like a busy moment.",
       "The agents investigate, close it as a false alarm and isolate nothing.", 30, transient_blip, "C"),
    _S("slow-burn", "Slow, quiet copying out of the {svc}",
       "A weak signal that A and B see first and C and D see five seconds later, and that keeps growing.",
       "The investigation confirms it persists and the agents agree to isolate the {svc}.", 45, slow_burn, "C"),
    _S("ambiguous", "An unexplained weak signal on the {svc}",
       "No attack is injected; only two agents keep seeing something faint.",
       "A human-review flag is raised; nothing is isolated.", 35, ambiguous, "C"),
    _S("validation-retry", "The first replacement {svc} fails its health check",
       "The attack is real; the first clean replacement does not come up healthy.",
       "Evidence is saved, recovery is retried, the second replacement passes and normal access is restored in stages.", 60, validation_retry, "C"),
    _S("failed-validation", "Every replacement {svc} fails its health check",
       "Recovery is retried a limited number of times.",
       "The incident is marked NEEDS HUMAN ATTENTION and the {svc} stays isolated. Press Reset afterwards.", 70, failed_validation, "C", True),
    *(([
        _S("lab-results-tampered", "A file in the {svc} is changed",
           "A file inside the {svc} (synthetic data only) is modified at runtime, after a few odd connection attempts.",
           "The four agents agree 3-of-4 to isolate only the {svc}; it is replaced, checked and returned in stages. Nothing else is isolated.", 45, file_tampering, "E"),
        _S("medication-failed-logins", "Repeated failed logins from the {svc}",
           "The {svc} starts hammering the Login & Access Service with wrong passwords.",
           "The failed logins are traced to the {svc}; it is isolated by 3-of-4 agreement, recovered and returned in stages.", 45, failed_logins, "F"),
        _S("database-cascade", "The {svc} is compromised while other services depend on it",
           "Records are copied out of the {svc}; the services that depend on it are watched to make sure they are not isolated by mistake.",
           "Only the {svc} is isolated; the services that depend on it are not isolated by mistake.", 50, dependency_impact, "D"),
    ]) if SIX else []),
    _S("obstruct", "A compromised agent refuses to vote while the {svc} is attacked",
       "One agent withholds every vote about the {svc} while it is being attacked. No made-up evidence is sent: the agent simply does not take part.",
       "The other three agents still reach agreement and isolate and recover the {svc}. The withheld votes are shown with the reason. "
       "The agent's trust is not reduced (the platform has no rule for a missing vote).", 45, obstruct, "C"),
]}


def listing() -> List[dict]:
    return [{"id": s.id, "title": s.title, "description": s.description, "expect": s.expect, "est_s": s.est_s,
             "default_target": s.default_target, "targets": list(s.targets), "texts": {t: s.text(t) for t in s.targets}}
            for s, _ in CATALOGUE.values()]
