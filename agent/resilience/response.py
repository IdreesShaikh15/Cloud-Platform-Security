"""Isolation / Recovery / Validation actuators.

`K8sBackend` talks to the Kubernetes API (NetworkPolicies, Deployments,
ConfigMaps). `FakeBackend` implements the same interface in memory for the
local simulator and unit tests.

All actuator calls are idempotent and every change is annotated with the
quorum certificate (the voters) that authorized it, so the cluster itself
holds an audit trail of *who* agreed to isolate *what*.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional, Protocol

from .reintegration import POLICY_NAME_FMT, network_policy

log = logging.getLogger(__name__)

MARKER_CONFIGMAP = "cr-attack-marker"


class ResponseBackend(Protocol):
    def apply_stage(self, workload: str, stage: str, annotations: Dict[str, str]) -> None: ...
    def current_stage(self, workload: str) -> Optional[str]: ...
    def recover(self, workload: str, epoch: int) -> None: ...
    def recovery_done(self, workload: str, epoch: int) -> bool: ...
    def write_state(self, workload: str, state: Dict[str, str]) -> None: ...
    def read_state(self, workload: str) -> Dict[str, str]: ...
    def read_marker(self) -> Optional[dict]: ...


# --------------------------------------------------------------------------- Kubernetes
# Every Kubernetes API call is bounded: (connect, read) seconds. Without this the
# python client waits forever on a hung API server, and because the agent's tick
# loop (and its status page) share one lock, a single hung call froze the whole
# agent. A timeout does NOT mean the call failed - the request may still have been
# applied - so every actuator below is written to be safe to repeat.
API_TIMEOUT_S = (3.0, 10.0)


class K8sBackend:
    def __init__(self, healthcare_ns: str, resilience_ns: str, known_good_image: str,
                 api_timeout_s: tuple = API_TIMEOUT_S):
        from kubernetes import client, config  # imported lazily: not needed for local sim
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.client = client
        self.net = client.NetworkingV1Api()
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()
        self.hc_ns = healthcare_ns
        self.res_ns = resilience_ns
        self.image = known_good_image
        self.t = {"_request_timeout": api_timeout_s}

    def _policy_name(self, workload: str) -> str:
        return POLICY_NAME_FMT.format(workload=workload)

    def apply_stage(self, workload: str, stage: str, annotations: Dict[str, str]) -> None:
        from kubernetes.client.rest import ApiException
        name = self._policy_name(workload)
        body = network_policy(workload, stage, self.hc_ns, self.res_ns, annotations)
        if body is None:  # FULL -> remove restriction
            try:
                self.net.delete_namespaced_network_policy(name, self.hc_ns, **self.t)
            except ApiException as exc:
                if exc.status != 404:
                    raise
            return
        try:
            self.net.replace_namespaced_network_policy(name, self.hc_ns, body, **self.t)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.net.create_namespaced_network_policy(self.hc_ns, body, **self.t)

    def current_stage(self, workload: str) -> Optional[str]:
        from kubernetes.client.rest import ApiException
        try:
            pol = self.net.read_namespaced_network_policy(self._policy_name(workload), self.hc_ns,
                                                        **self.t)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return (pol.metadata.annotations or {}).get("resilience.io/stage")

    def recover(self, workload: str, epoch: int) -> None:
        """Redeploy from the known-good image: the pod is replaced (Recreate
        strategy) so any tampered filesystem / rogue process is discarded."""
        ts = f"{time.time():.3f}"
        patch = {
            "metadata": {"annotations": {"resilience.io/recovered-epoch": str(epoch),
                                         "resilience.io/recovery-requested-at": ts}},
            "spec": {"template": {
                "metadata": {"annotations": {"resilience.io/recovered-at": ts}},
                "spec": {"containers": [{"name": "app", "image": self.image}]}}},
        }
        self.apps.patch_namespaced_deployment(workload, self.hc_ns, patch, **self.t)

    def recovery_done(self, workload: str, epoch: int) -> bool:
        d = self.apps.read_namespaced_deployment(workload, self.hc_ns, **self.t)
        ann = d.metadata.annotations or {}
        if int(ann.get("resilience.io/recovered-epoch", "-1")) < epoch:
            return False
        st, want = d.status, d.spec.replicas or 1
        return ((st.observed_generation or 0) >= d.metadata.generation
                and (st.updated_replicas or 0) == want
                and (st.ready_replicas or 0) == want
                and (st.replicas or 0) == want)

    def write_state(self, workload: str, state: Dict[str, str]) -> None:
        ann = {f"resilience.io/{k}": str(v) for k, v in state.items()}
        self.apps.patch_namespaced_deployment(workload, self.hc_ns,
                                              {"metadata": {"annotations": ann}}, **self.t)

    def read_state(self, workload: str) -> Dict[str, str]:
        try:
            d = self.apps.read_namespaced_deployment(workload, self.hc_ns, **self.t)
        except Exception:
            return {}
        return {k.split("/", 1)[1]: v for k, v in (d.metadata.annotations or {}).items()
                if k.startswith("resilience.io/")}

    def read_marker(self) -> Optional[dict]:
        try:
            cm = self.core.read_namespaced_config_map(MARKER_CONFIGMAP, self.res_ns, **self.t)
        except Exception:
            return None
        raw = (cm.data or {}).get("marker")
        return json.loads(raw) if raw else None


# --------------------------------------------------------------------------- in-memory
class FakeBackend:
    """Shared by all simulated agents (it plays the role of the cluster)."""

    def __init__(self, recovery_delay_s: float = 3.0,
                 on_recover: Optional[Callable[[str], None]] = None):
        self._lock = threading.Lock()
        self.policies: Dict[str, dict] = {}
        self.state: Dict[str, Dict[str, str]] = {}
        self.recoveries: Dict[str, tuple] = {}     # workload -> (epoch, ready_at)
        self.marker: Optional[dict] = None
        self.delay = recovery_delay_s
        self.on_recover = on_recover
        self.log: list = []

    def apply_stage(self, workload, stage, annotations):
        with self._lock:
            self.log.append((time.time(), "apply_stage", workload, stage, dict(annotations)))
            if stage == "FULL":
                self.policies.pop(workload, None)
            else:
                self.policies[workload] = {"stage": stage, **annotations}

    def current_stage(self, workload):
        with self._lock:
            p = self.policies.get(workload)
            return p["stage"] if p else None

    def recover(self, workload, epoch):
        with self._lock:
            self.log.append((time.time(), "recover", workload, epoch))
            self.recoveries[workload] = (epoch, time.time() + self.delay)
            self.state.setdefault(workload, {})["recovered-epoch"] = str(epoch)
        if self.on_recover:
            threading.Timer(self.delay, self.on_recover, args=(workload,)).start()

    def recovery_done(self, workload, epoch):
        with self._lock:
            r = self.recoveries.get(workload)
            return bool(r and r[0] >= epoch and time.time() >= r[1])

    def write_state(self, workload, state):
        with self._lock:
            self.state.setdefault(workload, {}).update({k: str(v) for k, v in state.items()})

    def read_state(self, workload):
        with self._lock:
            return dict(self.state.get(workload, {}))

    def read_marker(self):
        with self._lock:
            return dict(self.marker) if self.marker else None
