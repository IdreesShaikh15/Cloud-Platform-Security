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
from resilience.config import (NODE_IDS, ClusterConfig, NodeSpec, QuorumParams,  # noqa: E402
                               Timers, TrustParams)
from resilience.crypto import KeyRegistry, Signer  # noqa: E402
from resilience.metrics import MetricsRecorder  # noqa: E402
from resilience.monitoring import Snapshot  # noqa: E402
from resilience.peer import PeerClient, PeerServer, TlsMaterial  # noqa: E402
from resilience.pki import generate  # noqa: E402
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


class FakeWorld:
    def __init__(self):
        self.lock = threading.Lock()
        self.apps = {n: FakeApp(n, f"10.244.0.{10 + i}") for i, n in enumerate(NODE_IDS)}

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

    def snapshot(self, nid: str, now: float) -> Snapshot:
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
    def __init__(self, world: FakeWorld):
        self.world = world

    def collect(self) -> Dict[str, Snapshot]:
        now = time.time()
        return {n: self.world.snapshot(n, now) for n in NODE_IDS}


def fast_config(base_port: int) -> ClusterConfig:
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
        quorum=QuorumParams())


class LocalCluster:
    """Four agents with real gRPC/mTLS peers on localhost."""

    def __init__(self, base_port: int = 50151, with_status: bool = False, cfg: Optional[ClusterConfig] = None):
        self.cfg = cfg or fast_config(base_port)
        self.world = FakeWorld()
        self.backend = FakeBackend(recovery_delay_s=2.0, on_recover=self.world.recover_workload)
        self.pki_dir = tempfile.mkdtemp(prefix="cr-pki-")
        pubkeys = generate(self.pki_dir, {n: s.agent_name for n, s in self.cfg.nodes.items()})
        registry = KeyRegistry.from_b64_map(pubkeys)
        self.compromise: Dict[str, CompromiseSource] = {}
        self.agents: Dict[str, ResilienceAgent] = {}
        self.servers: List[PeerServer] = []
        for n, spec in self.cfg.nodes.items():
            d = os.path.join(self.pki_dir, spec.agent_name)
            signer = Signer.from_pem_file(n, os.path.join(d, "signing.key"))
            tls = TlsMaterial.from_dir(d)
            comp = CompromiseSource(path=None)
            agent = ResilienceAgent(self.cfg, n, signer, registry, FakeTelemetry(self.world),
                                    self.backend, MetricsRecorder(n), comp)
            srv = PeerServer(n, spec.agent_addr, tls, self.cfg.node_of_agent, agent.on_envelope).start()
            agent.transport = PeerClient(n, self.cfg.nodes, tls)
            self.agents[n], self.compromise[n] = agent, comp
            self.servers.append(srv)
            if with_status:
                from resilience.status_server import serve_status
                serve_status(agent, int(spec.status_url.split(":")[2].split("/")[0]))
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def start(self):
        for a in self.agents.values():
            t = threading.Thread(target=self._loop, args=(a,), daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def _loop(self, agent):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                agent.tick()
            except Exception:
                import traceback
                traceback.print_exc()
            self._stop.wait(max(0.01, self.cfg.timers.tick_s - (time.time() - t0)))

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
    def __init__(self, cfg: Optional[ClusterConfig] = None):
        self.cfg = cfg or fast_config(50351)
        self.world = FakeWorld()
        self.backend = FakeBackend(recovery_delay_s=2.0, on_recover=self.world.recover_workload)
        self.comp = CompromiseSource(path=None)
        self.ctl = CentralController(self.cfg, FakeTelemetry(self.world), self.backend,
                                     MetricsRecorder("CENTRAL", "centralized"), self.comp)
