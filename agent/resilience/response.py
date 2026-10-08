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

from .certificate import ANNOTATION as QC_ANNOTATION
from .reintegration import POLICY_NAME_FMT, STAGES, network_policy

log = logging.getLogger(__name__)

MARKER_CONFIGMAP = "cr-attack-marker"


class ResponseBackend(Protocol):
    def apply_stage(self, workload: str, stage: str, annotations: Dict[str, str]) -> None: ...
    def current_stage(self, workload: str) -> Optional[str]: ...
    def recover(self, workload: str, epoch: int, qc: str = "") -> None: ...
    def recovery_done(self, workload: str, epoch: int) -> bool: ...
    def write_state(self, workload: str, state: Dict[str, str], qc: str = "") -> None: ...
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
            # A DELETE request cannot carry annotations, and the admission webhook must see the
            # quorum certificate. So first UPDATE the policy to stage FULL (same rules as
            # PEER_VALIDATED, certificate attached), THEN delete it; the webhook accepts the
            # DELETE only for a policy that carries a valid FULL certificate.
            pending = network_policy(workload, "PEER_VALIDATED", self.hc_ns, self.res_ns, annotations)
            pending["metadata"]["annotations"]["resilience.io/stage"] = "FULL"
            try:
                self.net.replace_namespaced_network_policy(name, self.hc_ns, pending, **self.t)
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
        stage = (pol.metadata.annotations or {}).get("resilience.io/stage")
        return "PEER_VALIDATED" if stage == "FULL" else stage    # FULL on an existing policy = removal pending

    def recover(self, workload: str, epoch: int, qc: str = "") -> None:
        """Redeploy from the known-good image: the pod is replaced (Recreate
        strategy) so any tampered filesystem / rogue process is discarded."""
        ts = f"{time.time():.3f}"
        patch = {
            "metadata": {"annotations": {"resilience.io/recovered-epoch": str(epoch),
                                         "resilience.io/recovery-requested-at": ts,
                                         **({QC_ANNOTATION: qc} if qc else {})}},
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

    def write_state(self, workload: str, state: Dict[str, str], qc: str = "") -> None:
        ann = {f"resilience.io/{k}": str(v) for k, v in state.items()}
        if qc:
            ann[QC_ANNOTATION] = qc
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
class AdmissionDenied(Exception):
    """The (simulated) admission webhook refused a change."""


class FakeBackend:
    """Shared by all simulated agents (it plays the role of the cluster).

    With `admission` set (a callable taking an AdmissionRequest-like dict and returning
    (allowed, reason), normally AdmissionPolicy.review) every change an agent makes is first
    reviewed exactly as the real webhook would review it: denied changes raise AdmissionDenied
    and are recorded in `denied`."""

    ENFORCED_USER = "system:serviceaccount:resilience:resilience-agent"

    def __init__(self, recovery_delay_s: float = 3.0,
                 on_recover: Optional[Callable[[str], None]] = None,
                 admission: Optional[Callable[[dict], tuple]] = None,
                 healthcare_ns: str = "healthcare", resilience_ns: str = "resilience",
                 known_good_image: str = "cr-healthcare-app:known-good"):
        self._lock = threading.RLock()      # re-entrant: the admission callback reads state
        self.policies: Dict[str, dict] = {}
        self.state: Dict[str, Dict[str, str]] = {}
        self.recoveries: Dict[str, tuple] = {}     # workload -> (epoch, ready_at)
        self.marker: Optional[dict] = None
        self.delay = recovery_delay_s
        self.on_recover = on_recover
        self.log: list = []
        self.admission = admission
        self.hc, self.res, self.image = healthcare_ns, resilience_ns, known_good_image
        self.objects: Dict[str, dict] = {}         # NetworkPolicy manifests as the cluster holds them
        self.deployments: Dict[str, dict] = {}
        self.denied: list = []                     # (time, kind, operation, name, reason)
        self.admitted: list = []

    def incident_epoch(self, workload: str) -> int:
        """What the real webhook reads from the Deployment annotation resilience.io/epoch."""
        with self._lock:
            return int(self.state.get(workload, {}).get("epoch", 0))

    # ---- the simulated admission webhook
    def _admit(self, kind: str, op: str, name: str, old: Optional[dict], new: Optional[dict],
               user: Optional[str] = None) -> None:
        if self.admission is None:
            return
        req = {"uid": f"{kind}-{name}-{time.time()}", "operation": op, "namespace": self.hc,
               "kind": {"kind": kind}, "name": name,
               "userInfo": {"username": user or self.ENFORCED_USER}, "object": new, "oldObject": old}
        ok, reason = self.admission(req)
        (self.admitted if ok else self.denied).append((time.time(), kind, op, name, reason))
        if not ok:
            raise AdmissionDenied(reason)

    def _deployment(self, workload: str) -> dict:
        return self.deployments.setdefault(workload, {
            "metadata": {"name": workload, "annotations": {}},
            "spec": {"template": {"metadata": {"annotations": {}},
                                  "spec": {"containers": [{"name": "app", "image": "cr-healthcare-app:1.0"}]}}}})

    def _patch_deployment(self, workload: str, annotations: Dict[str, str], image: Optional[str] = None,
                          template_ann: Optional[Dict[str, str]] = None, user: Optional[str] = None) -> None:
        import copy
        old = copy.deepcopy(self._deployment(workload))
        new = copy.deepcopy(old)
        new["metadata"].setdefault("annotations", {}).update(annotations)
        if template_ann:
            new["spec"]["template"]["metadata"].setdefault("annotations", {}).update(template_ann)
        if image:
            new["spec"]["template"]["spec"]["containers"][0]["image"] = image
        self._admit("Deployment", "UPDATE", workload, old, new, user)
        self.deployments[workload] = new

    # ---- ResponseBackend interface
    def apply_stage(self, workload, stage, annotations):
        with self._lock:
            name = POLICY_NAME_FMT.format(workload=workload)
            old = self.objects.get(name)
            if stage == "FULL":
                pending = network_policy(workload, "PEER_VALIDATED", self.hc, self.res, dict(annotations))
                pending["metadata"]["annotations"]["resilience.io/stage"] = "FULL"
                if old is not None:
                    self._admit("NetworkPolicy", "UPDATE", name, old, pending)
                    self.objects[name] = pending
                    self._admit("NetworkPolicy", "DELETE", name, pending, None)
                    self.objects.pop(name, None)
                self.log.append((time.time(), "apply_stage", workload, stage, dict(annotations)))
                self.policies.pop(workload, None)
                return
            body = network_policy(workload, stage, self.hc, self.res, dict(annotations))
            self._admit("NetworkPolicy", "UPDATE" if old is not None else "CREATE", name, old, body)
            self.objects[name] = body
            self.log.append((time.time(), "apply_stage", workload, stage, dict(annotations)))
            self.policies[workload] = {"stage": stage, **annotations}

    def current_stage(self, workload):
        with self._lock:
            p = self.policies.get(workload)
            return p["stage"] if p else None

    def recover(self, workload, epoch, qc=""):
        with self._lock:
            ts = f"{time.time():.3f}"
            self._patch_deployment(workload, {"resilience.io/recovered-epoch": str(epoch),
                                              "resilience.io/recovery-requested-at": ts,
                                              **({QC_ANNOTATION: qc} if qc else {})},
                                   image=self.image, template_ann={"resilience.io/recovered-at": ts})
            self.log.append((time.time(), "recover", workload, epoch))
            self.recoveries[workload] = (epoch, time.time() + self.delay)
            self.state.setdefault(workload, {})["recovered-epoch"] = str(epoch)
        if self.on_recover:
            threading.Timer(self.delay, self.on_recover, args=(workload,)).start()

    def recovery_done(self, workload, epoch):
        with self._lock:
            r = self.recoveries.get(workload)
            return bool(r and r[0] >= epoch and time.time() >= r[1])

    def write_state(self, workload, state, qc=""):
        with self._lock:
            ann = {f"resilience.io/{k}": str(v) for k, v in state.items()}
            if qc:
                ann[QC_ANNOTATION] = qc
            self._patch_deployment(workload, ann)
            self.state.setdefault(workload, {}).update({k: str(v) for k, v in state.items()})

    def read_state(self, workload):
        with self._lock:
            return dict(self.state.get(workload, {}))

    def read_marker(self):
        with self._lock:
            return dict(self.marker) if self.marker else None
