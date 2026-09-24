"""Configuration for resilience agents, the baseline controller and the dashboard.

In the cluster a single JSON document (ConfigMap `resilience-config`) describes
all four resilience nodes; each agent learns *which* node it is from the
NODE_ID environment variable. The local simulator builds the same structure
in code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

NODE_IDS: List[str] = ["A", "B", "C", "D"]


@dataclass
class NodeSpec:
    node_id: str               # "A"
    agent_name: str            # "agent-a"  (also the mTLS certificate CN)
    agent_addr: str            # host:port of the agent's gRPC server
    status_url: str            # http://.../status (read-only, for the dashboard)
    workload: str              # "patient-portal" (Deployment + Service name)
    telemetry_url: str         # http://patient-portal.healthcare.svc:8080


@dataclass
class Timers:
    tick_s: float = 1.0                # monitoring/decision loop period
    evidence_window_s: float = 30.0    # evidence older than this is ignored
    contradiction_grace_s: float = 8.0 # time peers get to corroborate a claim
    executor_stagger_s: float = 3.0    # failover delay per executor rank
    stage_dwell_s: float = 10.0        # minimum time spent in each reintegration stage
    validate_timeout_s: float = 90.0   # re-run recovery if validation stalls
    max_clock_skew_s: float = 15.0     # reject envelopes from the future / too old


@dataclass
class TrustParams:
    # Agent (evidence-source) trust, as seen by each peer.
    agent_decay_per_s: float = 2.5     # while a peer's claims are contradicted
    agent_recover_per_s: float = 0.5   # otherwise
    invalid_message_penalty: float = 20.0  # forged / mismatched envelope
    vote_min_trust: float = 40.0       # votes from peers below this are ignored
    suspect_below: float = 50.0        # dashboard flags the agent as SUSPECT
    # Workload trust (governs reintegration).
    workload_decay_per_s: float = 10.0 # while anomalies are observed
    workload_recover_per_s: float = 2.0


@dataclass
class QuorumParams:
    n: int = 4
    f: int = 1                         # floor((n-1)/3)
    evidence_min_conf: float = 0.3     # below this no evidence is emitted
    local_min_conf: float = 0.5        # own observation needed before voting
    score_threshold: float = 0.6       # weighted score W(T) needed to vote CONTAIN
    diversity_bonus: float = 0.15      # per extra corroborated observation type

    @property
    def quorum(self) -> int:
        return 2 * self.f + 1          # 3 of 4


@dataclass
class ClusterConfig:
    nodes: Dict[str, NodeSpec]
    healthcare_namespace: str = "healthcare"
    resilience_namespace: str = "resilience"
    known_good_image: str = "cr-healthcare-app:known-good"
    baseline_hashes: Dict[str, str] = field(default_factory=dict)
    process_allowlist: List[str] = field(default_factory=lambda: ["python", "python3"])
    timers: Timers = field(default_factory=Timers)
    trust: TrustParams = field(default_factory=TrustParams)
    quorum: QuorumParams = field(default_factory=QuorumParams)
    client_stats_url: Optional[str] = None  # synthetic client (availability probe)

    def workload_of(self, node_id: str) -> str:
        return self.nodes[node_id].workload

    def node_of_workload(self, workload: str) -> Optional[str]:
        for nid, spec in self.nodes.items():
            if spec.workload == workload:
                return nid
        return None

    def node_of_agent(self, agent_name: str) -> Optional[str]:
        for nid, spec in self.nodes.items():
            if spec.agent_name == agent_name:
                return nid
        return None


def _dc(cls, raw: Optional[dict]):
    raw = raw or {}
    known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def load_cluster_config(path: Optional[str] = None) -> ClusterConfig:
    path = path or os.environ.get("RESILIENCE_CONFIG", "/etc/resilience/config.json")
    with open(path) as fh:
        raw = json.load(fh)
    nodes = {nid: NodeSpec(node_id=nid, **spec) for nid, spec in raw["nodes"].items()}
    cfg = ClusterConfig(
        nodes=nodes,
        healthcare_namespace=raw.get("healthcare_namespace", "healthcare"),
        resilience_namespace=raw.get("resilience_namespace", "resilience"),
        known_good_image=raw.get("known_good_image", "cr-healthcare-app:known-good"),
        process_allowlist=raw.get("process_allowlist", ["python", "python3"]),
        timers=_dc(Timers, raw.get("timers")),
        trust=_dc(TrustParams, raw.get("trust")),
        quorum=_dc(QuorumParams, raw.get("quorum")),
        client_stats_url=raw.get("client_stats_url"),
    )
    hashes_path = raw.get("baseline_hashes_path") or os.environ.get(
        "BASELINE_HASHES", "/etc/resilience/hashes/known-good-hashes.json")
    if hashes_path and os.path.exists(hashes_path):
        with open(hashes_path) as fh:
            cfg.baseline_hashes = json.load(fh)
    return cfg
