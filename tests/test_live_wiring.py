"""Regression: the live entrypoint must keep its gRPC server alive.

run_agent() used to call `PeerServer(...).start()` without keeping the
object; grpcio stops a server when it is garbage-collected, so in the pods
no agent accepted peer connections and evidence/votes were silently dropped.
The simulator never showed it because it holds its servers in a list.
"""
import gc
import os
import time

from resilience.__main__ import wire_agent
from resilience.crypto import KeyRegistry, Signer
from resilience.metrics import MetricsRecorder
from resilience.peer import TlsMaterial
from resilience.proto import resilience_pb2 as pb
from resilience.response import FakeBackend
from resilience.pki import generate
from resilience.simhooks import Compromise, CompromiseSource
from world import FakeTelemetry, FakeWorld, fast_config


def _build(tmp_path, base_port):
    cfg = fast_config(base_port)
    pki = str(tmp_path)
    registry = KeyRegistry.from_b64_map(generate(pki, {n: s.agent_name for n, s in cfg.nodes.items()}))
    world, backend = FakeWorld(), FakeBackend()
    agents = {}
    for n, spec in cfg.nodes.items():
        d = os.path.join(pki, spec.agent_name)
        agents[n] = wire_agent(cfg, n, Signer.from_pem_file(n, os.path.join(d, "signing.key")),
                               registry, TlsMaterial.from_dir(d), FakeTelemetry(world), backend,
                               MetricsRecorder(n), CompromiseSource(path=None), spec.agent_addr)
    return agents


def test_peer_servers_survive_garbage_collection(tmp_path):
    agents = _build(tmp_path, 50551)
    try:
        gc.collect()
        time.sleep(0.2)
        for n, a in agents.items():
            for peer, stub in a.transport.stubs.items():
                reply = stub.Ping(pb.PingRequest(from_node=n), timeout=2.0)
                assert reply.node == peer
    finally:
        for a in agents.values():
            a.server.stop(0)


def test_live_wired_false_accusation_reaches_peers(tmp_path):
    agents = _build(tmp_path, 50561)
    try:
        gc.collect()
        agents["A"].compromise.set(Compromise(mode="false-accusation", target="B"))
        end = time.time() + 30
        while time.time() < end and agents["C"].agent_trust.get("A") >= 50:
            for a in agents.values():
                a.tick()
            time.sleep(0.5)
        assert agents["C"].pool.recent(time.time(), 30, origin="A"), "A's evidence never arrived"
        assert agents["C"].agent_trust.get("A") < 50
        assert all(a.states["B"].epoch == 0 for a in agents.values())
    finally:
        for a in agents.values():
            a.server.stop(0)
