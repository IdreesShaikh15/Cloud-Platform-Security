"""Regression tests for the real bugs found by the Phase 1 audit (docs/AUDIT.md)."""
import socket
import threading
import time

import pytest

from resilience.detection import Detector
from resilience.evidence import ObsType
from resilience.monitoring import Snapshot
from resilience.reintegration import AGENT_APP_LABELS, WORKLOAD_PORT, network_policy

BASE = {"app.py": "1" * 64}


# --------------------------------------------------------------------------- K8s timeouts
class _BlackHole:
    """A TCP server that accepts connections and never answers (a hung API server)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
                self.conns.append(c)
            except OSError:
                return

    def close(self):
        self.sock.close()
        for c in self.conns:
            c.close()


def _backend_against(port):
    from kubernetes import client
    from resilience.response import K8sBackend
    cfg = client.Configuration()
    cfg.host = f"http://127.0.0.1:{port}"
    api = client.ApiClient(cfg)
    b = object.__new__(K8sBackend)            # skip loading a kube-config
    b.client, b.net, b.apps, b.core = client, client.NetworkingV1Api(api), client.AppsV1Api(api), client.CoreV1Api(api)
    b.hc_ns, b.res_ns, b.image = "healthcare", "resilience", "img"
    b.t = {"_request_timeout": (1.0, 1.0)}
    return b


def test_hung_api_server_cannot_block_the_agent_forever():
    pytest.importorskip("kubernetes")
    hole = _BlackHole()
    try:
        b = _backend_against(hole.port)
        t0 = time.time()
        with pytest.raises(Exception):
            b.recover("records-api", 1)                     # patch deployment
        with pytest.raises(Exception):
            b.apply_stage("records-api", "QUARANTINE", {})  # replace/create NetworkPolicy
        assert b.read_state("records-api") == {}            # reads swallow errors
        assert b.read_marker() is None
        assert time.time() - t0 < 15, "calls did not honour the request timeout"
    finally:
        hole.close()


def test_every_api_call_passes_a_request_timeout():
    pytest.importorskip("kubernetes")
    from unittest.mock import MagicMock
    from resilience.response import K8sBackend
    b = object.__new__(K8sBackend)
    b.net, b.apps, b.core = MagicMock(), MagicMock(), MagicMock()
    b.hc_ns, b.res_ns, b.image = "healthcare", "resilience", "img"
    b.t = {"_request_timeout": (3.0, 10.0)}
    from types import SimpleNamespace as NS
    b.apps.read_namespaced_deployment.return_value = NS(
        metadata=NS(annotations={"resilience.io/recovered-epoch": "1"}, generation=1),
        spec=NS(replicas=1), status=NS(observed_generation=1, updated_replicas=1,
                                       ready_replicas=1, replicas=1))
    b.core.read_namespaced_config_map.return_value = NS(data={"marker": "{}"})
    b.apply_stage("w", "QUARANTINE", {})
    b.apply_stage("w", "FULL", {})
    b.current_stage("w")
    b.recover("w", 1)
    b.recovery_done("w", 1)
    b.write_state("w", {"phase": "X"})
    b.read_state("w")
    b.read_marker()
    calls = [c for m in (b.net, b.apps, b.core) for c in m.method_calls]
    assert len(calls) >= 8
    for c in calls:
        assert c.kwargs.get("_request_timeout") == (3.0, 10.0), c


# --------------------------------------------------------------------------- AUTH window
def _snap(target, ip, t, failures_by_ip=None):
    return Snapshot(target=target, time=t, reachable=True, healthy=True, instance_id="i", pod_ip=ip,
                    processes=[{"pid": 1, "comm": "python", "exe": "/usr/bin/python3"}],
                    file_hashes=dict(BASE), auth_failures_by_ip=failures_by_ip or {})


def test_auth_history_resets_when_target_gets_a_new_pod_ip():
    """Counters are cumulative per IP. A recovered pod that lands on an IP an old attacker
    pod once used must not inherit that IP's old failures as a 'burst'."""
    d = Detector(BASE, ["python", "python3"])
    old_ip, new_ip = "10.0.0.5", "10.0.0.9"
    # before recovery: quiet, history belongs to old_ip
    d.analyze({"B": _snap("B", "10.0.0.2", 100.0, {old_ip: 0, new_ip: 400}),
               "C": _snap("C", old_ip, 100.0)}, 100.0)
    # recovery: C is now at new_ip, whose cumulative counter is 400 from some older pod
    out = d.analyze({"B": _snap("B", "10.0.0.2", 101.0, {old_ip: 0, new_ip: 400}),
                     "C": _snap("C", new_ip, 101.0)}, 101.0)
    assert [x for x in out["C"] if x.observation == ObsType.AUTH] == []
    # a real burst from the new IP afterwards is still detected
    out = d.analyze({"B": _snap("B", "10.0.0.2", 103.0, {old_ip: 0, new_ip: 460}),
                     "C": _snap("C", new_ip, 103.0)}, 103.0)
    assert [x.observation for x in out["C"]] == [ObsType.AUTH]


# --------------------------------------------------------------------------- policy shape
def test_isolation_ingress_is_limited_to_agent_pods_on_the_workload_port():
    for stage in ("QUARANTINE", "RESTRICTED", "MONITORED", "PEER_VALIDATED"):
        pol = network_policy("records-api", stage, "healthcare", "resilience")
        first = pol["spec"]["ingress"][0]
        peer = first["from"][0]
        assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "resilience"}
        assert peer["podSelector"]["matchExpressions"] == [
            {"key": "app", "operator": "In", "values": AGENT_APP_LABELS}]
        assert first["ports"] == [{"protocol": "TCP", "port": WORKLOAD_PORT}]
        assert pol["spec"]["policyTypes"] == ["Ingress", "Egress"], "egress must be isolated separately"


def test_quarantine_blocks_all_egress_including_dns():
    pol = network_policy("records-api", "QUARANTINE", "healthcare", "resilience")
    assert pol["spec"]["egress"] == []
    restricted = network_policy("records-api", "RESTRICTED", "healthcare", "resilience")
    ports = [p for rule in restricted["spec"]["egress"] for p in rule.get("ports", [])]
    assert {"protocol": "UDP", "port": 53} in ports      # DNS only comes back at RESTRICTED


def test_agent_labels_match_the_manifests():
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "k8s", "resilience", "20-agents.yaml")).read() + \
        open(os.path.join(root, "k8s", "baseline", "central-controller.yaml")).read()
    for label in AGENT_APP_LABELS:
        assert re.search(rf"labels: \{{app: {label}\b", text), f"no pod labelled app={label}"


# --------------------------------------------------------------------------- visible failures
def test_unreadable_cluster_state_is_reported_not_silent():
    from resilience.agent import ISOLATED, ResilienceAgent
    from resilience.crypto import KeyRegistry, Signer
    from resilience.response import FakeBackend
    from world import FakeTelemetry, FakeWorld, fast_config

    class Broken(FakeBackend):
        def read_state_strict(self, workload):
            raise RuntimeError("API timeout")

    cfg = fast_config(50901)
    a = ResilienceAgent(cfg, "A", Signer.generate("A"), KeyRegistry({}), FakeTelemetry(FakeWorld()), Broken())
    a.states["C"].phase = ISOLATED
    a._track_cluster(time.time())
    ev = [e for e in a.events.by_category("ACTION") if e["details"].get("action") == "cluster_read_failed"]
    assert ev and "API timeout" in ev[0]["summary"] and ev[0]["details"]["phase"] == ISOLATED
    for _ in range(5):                                  # throttled: not one event per tick
        a._track_cluster(time.time())
    assert len([e for e in a.events.by_category("ACTION")
                if e["details"].get("action") == "cluster_read_failed"]) == 1
