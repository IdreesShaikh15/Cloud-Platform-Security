"""In-memory stand-in for the Kubernetes cluster + healthcare workloads.

Used by the local simulator and the integration tests. Everything *above*
the cluster boundary is the real code: agents, Ed25519 signatures, gRPC over
mTLS on localhost, weighted evidence, quorum, trust, the pipeline state
machine and the executors. Only telemetry (FakeWorld) and the actuators
(FakeBackend) are simulated.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))

from resilience.agent import ResilienceAgent  # noqa: E402
from resilience.baseline import CentralController  # noqa: E402
from resilience.config import (NODE_IDS, ClusterConfig, InvestigationParams, NodeSpec,  # noqa: E402
                               QuorumParams, Timers, TrustParams)
from resilience.crypto import KeyRegistry, Signer  # noqa: E402
from resilience.metrics import MetricsRecorder  # noqa: E402
from resilience.monitoring import Snapshot  # noqa: E402
from resilience.peer import PeerClient, PeerServer, TlsMaterial  # noqa: E402
from resilience.pki import generate  # noqa: E402
from resilience.admission import AdmissionPolicy  # noqa: E402
from resilience.response import FakeBackend  # noqa: E402
from resilience.simhooks import Compromise, CompromiseSource  # noqa: E402

WORKLOADS = {"A": "patient-portal", "B": "auth-service", "C": "records-api", "D": "database"}
BASELINE_HASHES = {"app.py": "a" * 64, "telemetry.py": "b" * 64, "static/index.html": "c" * 64}
ATTACK_KINDS = ("exfil", "tamper", "bruteforce")


@dataclass
class FakeApp:
    node: str
    ip: str
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    attacks: Set[str] = field(default_factory=set)
    attack_started: float = 0.0
    healthy: bool = True


@dataclass
class Overlay:
    """A simulated, observer-specific disturbance of ONE telemetry signal (outbound
    connections) of a simulated workload. Pure simulated telemetry: nothing real is touched.
    `level_fn(seconds_since_start)` gives the connection count; `observers` limits which
    agents' vantage sees it (None = all); `duration` makes it a transient blip."""
    target: str
    t0: float
    level_fn: object
    observers: Optional[Set[str]] = None
    duration: Optional[float] = None


class FakeWorld:
    def __init__(self):
        self.lock = threading.Lock()
        self.apps = {n: FakeApp(n, f"10.244.0.{10 + i}") for i, n in enumerate(NODE_IDS)}
        self.overlays: List[Overlay] = []

    def inject_connections(self, target: str, level_fn, observers=None, duration=None,
                           start_after: float = 0.0) -> None:
        """Simulated network-signal disturbance (see Overlay). Uses the same telemetry path
        as world.attack, so agents measure it exactly like any other reading."""
        with self.lock:
            self.overlays.append(Overlay(target, time.time() + start_after, level_fn,
                                         set(observers) if observers else None, duration))

    def clear_overlays(self) -> None:
        with self.lock:
            self.overlays.clear()

    def attack(self, target: str, kinds=ATTACK_KINDS) -> None:
        with self.lock:
            a = self.apps[target]
            a.attacks = set(kinds)
            a.attack_started = time.time()

    def recover_workload(self, workload: str) -> None:
        node = next(n for n, w in WORKLOADS.items() if w == workload)
        with self.lock:
            old = self.apps[node]
            self.apps[node] = FakeApp(node, old.ip)  # fresh instance, clean state
            self.overlays = [o for o in self.overlays if o.target != node]   # a clean pod has none

    def snapshot(self, nid: str, now: float, observer: Optional[str] = None) -> Snapshot:
        with self.lock:
            a = self.apps[nid]
            el = max(0.0, now - a.attack_started) if a.attacks else 0.0
            procs = [{"pid": 1, "comm": "python", "exe": "/usr/local/bin/python3.11"}]
            hashes = dict(BASELINE_HASHES)
            conns, tx = 2, int(1000 * now) % 10_000_000
            if "exfil" in a.attacks:
                conns = 40
                tx += int(2_000_000 * el)
                procs.append({"pid": 77, "comm": "xmrig", "exe": "/tmp/xmrig"})
            for ov in self.overlays:
                if ov.target != nid or now < ov.t0:
                    continue
                if ov.duration is not None and now - ov.t0 > ov.duration:
                    continue
                if ov.observers is not None and observer not in ov.observers:
                    continue
                conns = max(conns, int(ov.level_fn(now - ov.t0)))
            if "tamper" in a.attacks:
                hashes["static/index.html"] = "f" * 64
            auth = {}
            if nid == "B":  # auth-service counts failures by source ip
                for other in self.apps.values():
                    if "bruteforce" in other.attacks:
                        auth[other.ip] = int(20 * max(0.0, now - other.attack_started))
            return Snapshot(target=nid, time=now, reachable=True, healthy=a.healthy,
                            instance_id=a.instance_id, pod_ip=a.ip, outbound_connections=conns,
                            tx_bytes=tx, processes=procs, file_hashes=hashes,
                            auth_failures_by_ip=auth)


class FakeTelemetry:
    """What one agent (`observer`) measures. Observers share one world, but an Overlay may
    be visible from only some vantage points (models partial / noisy visibility)."""

    def __init__(self, world: FakeWorld, observer: Optional[str] = None):
        self.world = world
        self.observer = observer

    def collect(self) -> Dict[str, Snapshot]:
        now = time.time()
        return {n: self.world.snapshot(n, now, self.observer) for n in NODE_IDS}

    def collect_target(self, target: str) -> Snapshot:
        return self.world.snapshot(target, time.time(), self.observer)


def sim_investigation(**overrides) -> InvestigationParams:
    """Investigation timing accelerated like the rest of the simulator."""
    base = dict(budget_s=6.0, sample_interval_s=0.2, peer_poll_s=1.0, request_timeout_s=1.5,
                trigger_grace_s=1.5, trigger_stagger_s=0.2, recent_s=2.0, cooldown_s=8.0,
                authorization_ttl_s=15.0, watch_clear_s=8.0)
    base.update(overrides)
    return InvestigationParams(**base)


def fast_config(base_port: int, investigation: Optional[InvestigationParams] = None) -> ClusterConfig:
    nodes = {n: NodeSpec(node_id=n, agent_name=f"agent-{n.lower()}",
                         agent_addr=f"localhost:{base_port + i}",
                         status_url=f"http://localhost:{base_port + 100 + i}/status",
                         workload=WORKLOADS[n], telemetry_url="sim://")
             for i, n in enumerate(NODE_IDS)}
    return ClusterConfig(
        nodes=nodes, baseline_hashes=dict(BASELINE_HASHES),
        timers=Timers(tick_s=0.5, evidence_window_s=20, contradiction_grace_s=4,
                      executor_stagger_s=1.5, stage_dwell_s=3, validate_timeout_s=30),
        trust=TrustParams(agent_decay_per_s=5, agent_recover_per_s=0.5,
                          workload_decay_per_s=20, workload_recover_per_s=8),
        quorum=QuorumParams(), investigation=investigation or sim_investigation())


class LocalCluster:
    """Four agents with real gRPC/mTLS peers on localhost."""

    def __init__(self, base_port: int = 50151, with_status: bool = False, cfg: Optional[ClusterConfig] = None,
                 enforce_admission: bool = True, state_dir: Optional[str] = None):
        self.cfg = cfg or fast_config(base_port)
        self.world = FakeWorld()
        self.pki_dir = tempfile.mkdtemp(prefix="cr-pki-")
        pubkeys = generate(self.pki_dir, {n: s.agent_name for n, s in self.cfg.nodes.items()})
        registry = KeyRegistry.from_b64_map(pubkeys)
        self.registry = registry
        # The simulated cluster runs the SAME admission policy as the real webhook: every change an
        # agent makes must carry a valid 3-signature quorum certificate or it is refused.
        self.backend = FakeBackend(recovery_delay_s=2.0, on_recover=self.world.recover_workload,
                                   known_good_image=self.cfg.known_good_image)
        if enforce_admission:
            self.admission = AdmissionPolicy.from_config(self.cfg, registry,
                                                         epoch_source=self.backend.incident_epoch)
            self.backend.admission = self.admission.review
        else:
            self.admission = None
        self.compromise: Dict[str, CompromiseSource] = {}
        self.agents: Dict[str, ResilienceAgent] = {}
        self.servers: List[PeerServer] = []
        self.status_servers: Dict[str, object] = {}
        for n, spec in self.cfg.nodes.items():
            d = os.path.join(self.pki_dir, spec.agent_name)
            signer = Signer.from_pem_file(n, os.path.join(d, "signing.key"))
            tls = TlsMaterial.from_dir(d)
            comp = CompromiseSource(path=None)
            agent = ResilienceAgent(self.cfg, n, signer, registry, FakeTelemetry(self.world, n),
                                    self.backend, MetricsRecorder(n), comp,
                                    decision_log_path=(os.path.join(state_dir, f"decisions-{n}.jsonl")
                                                       if state_dir else None),
                                    trust_state_path=(os.path.join(state_dir, f"trust-{n}.json")
                                                      if state_dir else None))
            srv = PeerServer(n, spec.agent_addr, tls, self.cfg.node_of_agent, agent.on_envelope,
                             agent.on_investigate).start()
            agent.transport = PeerClient(n, self.cfg.nodes, tls)
            self.agents[n], self.compromise[n] = agent, comp
            self.servers.append(srv)
            if with_status:
                from resilience.status_server import serve_status
                self.status_servers[n] = serve_status(agent, int(spec.status_url.split(":")[2].split("/")[0]))
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        # Per-agent stop events, so a single agent can be "crashed" mid-run
        # while the other three keep ticking (the agent-crash scenario).
        self._crashed: Dict[str, threading.Event] = {n: threading.Event() for n in self.agents}
        self._servers_by_node: Dict[str, PeerServer] = dict(zip(self.agents, self.servers))

    def start(self):
        for n, a in self.agents.items():
            t = threading.Thread(target=self._loop, args=(n, a), daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def _loop(self, node, agent):
        while not self._stop.is_set():
            t0 = time.time()
            if not self._crashed[node].is_set():
                try:
                    agent.tick()
                except Exception:
                    import traceback
                    traceback.print_exc()
            self._stop.wait(max(0.01, self.cfg.timers.tick_s - (time.time() - t0)))

    def crash_agent(self, node: str) -> None:
        """Stop agent `node` entirely: it stops ticking (no monitoring, no
        evidence, no votes), its gRPC peer server is shut down so peers see
        its link go DOWN, and its status page (if served) stops answering, as
        for a real killed pod. Models a killed/partitioned resilience node."""
        self._crashed[node].set()
        self._servers_by_node[node].stop(0)
        if node in self.status_servers:          # its read-only status page dies with it
            self.status_servers.pop(node).shutdown()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        for s in self.servers:
            s.stop(0)

    def mark(self, scenario: str, target: str, attacker: Optional[str] = None) -> None:
        self.backend.marker = {"id": uuid.uuid4().hex[:8], "scenario": scenario, "target": target,
                               "attacker": attacker, "injected_at": time.time()}

    def compromise_agent(self, node: str, target: str, mode: str = "false-accusation") -> None:
        self.mark(mode, target, attacker=node)
        self.compromise[node].set(Compromise(mode=mode, target=target))

    def restore_agent(self, node: str) -> None:
        self.compromise[node].set(None)

    def wait_until(self, pred, timeout: float, poll: float = 0.25) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(poll)
        return False

    def phases(self, target: str) -> Dict[str, str]:
        return {n: a.states[target].phase for n, a in self.agents.items()}


class LocalBaseline:
    def __init__(self, cfg: Optional[ClusterConfig] = None, with_status: bool = False,
                 status_port: int = 50261):
        self.cfg = cfg or fast_config(50351)
        self.world = FakeWorld()
        self.backend = FakeBackend(recovery_delay_s=2.0, on_recover=self.world.recover_workload)
        self.comp = CompromiseSource(path=None)
        self.ctl = CentralController(self.cfg, FakeTelemetry(self.world), self.backend,
                                     MetricsRecorder("CENTRAL", "centralized"), self.comp)
        if with_status:
            from resilience.status_server import serve_status
            self.status_server = serve_status(self.ctl, status_port)
        self._stop = threading.Event()
        self._crashed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Run the controller's tick loop in a background thread, so it can
        later be "crashed" (stopped) independently of the calling code."""
        def loop():
            while not self._stop.is_set():
                t0 = time.time()
                if not self._crashed.is_set():
                    try:
                        self.ctl.tick()
                    except Exception:
                        import traceback
                        traceback.print_exc()
                self._stop.wait(max(0.01, self.cfg.timers.tick_s - (time.time() - t0)))
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def crash(self) -> None:
        """Stop the single central controller entirely: no more ticks, so no
        detection, no isolation, no recovery for as long as it is down -
        the single point of failure the distributed design avoids."""
        self._crashed.set()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
