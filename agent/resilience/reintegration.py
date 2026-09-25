"""Staged, trust-gated reintegration.

After a contained workload has been recovered and validated it climbs:

  QUARANTINE -> RESTRICTED -> MONITORED -> PEER_VALIDATED -> FULL

Each step is itself a quorum action (ADVANCE_STAGE). An agent votes for the
next stage only when (a) the workload has dwelt in the current stage for at
least `stage_dwell_s`, (b) the agent's own workload-trust view has crossed the
next stage's threshold, and (c) it currently observes no anomaly.

Network exposure per stage (enforced by Calico via NetworkPolicy):

  stage           ingress allowed from            egress allowed to
  QUARANTINE      resilience ns (agents) only     nothing
  RESTRICTED      resilience ns only              healthcare ns + DNS
  MONITORED       resilience + healthcare ns      healthcare ns + DNS    (detection thresholds x0.7)
  PEER_VALIDATED  resilience + healthcare ns      healthcare ns + DNS
  FULL            (policy removed)                (policy removed)
"""
from __future__ import annotations

from typing import Dict, List, Optional

STAGES: List[str] = ["QUARANTINE", "RESTRICTED", "MONITORED", "PEER_VALIDATED", "FULL"]

# Workload trust a stage requires (per-agent view) before an agent votes to enter it.
STAGE_THRESHOLDS: Dict[str, float] = {
    "QUARANTINE": 0.0,
    "RESTRICTED": 20.0,
    "MONITORED": 40.0,
    "PEER_VALIDATED": 60.0,
    "FULL": 80.0,
}

# Detection sensitivity multiplier per stage (<1 = stricter).
STAGE_SENSITIVITY: Dict[str, float] = {"MONITORED": 0.7}

POLICY_NAME_FMT = "resilience-isolate-{workload}"


def next_stage(stage: str) -> Optional[str]:
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def can_advance(stage: str, dwell_s: float, min_dwell_s: float, workload_trust: float,
                anomaly_conf: float, anomaly_limit: float = 0.3) -> bool:
    nxt = next_stage(stage)
    return (nxt is not None and dwell_s >= min_dwell_s
            and workload_trust >= STAGE_THRESHOLDS[nxt] and anomaly_conf < anomaly_limit)


def advance_block_reason(stage: str, dwell_s: float, min_dwell_s: float, workload_trust: float,
                         anomaly_conf: float, anomaly_limit: float = 0.3) -> Optional[str]:
    """Plain-English reason can_advance() is False (None when it is True).
    Observability only - the decision itself is always can_advance()."""
    nxt = next_stage(stage)
    if nxt is None:
        return "already at FULL access"
    if dwell_s < min_dwell_s:
        return (f"must stay in {stage} another {min_dwell_s - dwell_s:.0f}s "
                f"(minimum {min_dwell_s:g}s per stage)")
    if workload_trust < STAGE_THRESHOLDS[nxt]:
        return (f"workload trust {workload_trust:.0f} is below the {STAGE_THRESHOLDS[nxt]:g} "
                f"needed for {nxt}")
    if anomaly_conf >= anomaly_limit:
        return f"an anomaly is still observed (confidence {anomaly_conf:.2f})"
    return None


def _ns_peer(ns: str) -> dict:
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}}


def network_policy(workload: str, stage: str, healthcare_ns: str, resilience_ns: str,
                   annotations: Optional[Dict[str, str]] = None) -> Optional[dict]:
    """NetworkPolicy manifest for a stage (None for FULL = no restriction)."""
    if stage == "FULL":
        return None
    ingress = [{"from": [_ns_peer(resilience_ns)]}]
    egress: List[dict] = []
    if stage in ("MONITORED", "PEER_VALIDATED"):
        ingress.append({"from": [_ns_peer(healthcare_ns)]})
    if stage != "QUARANTINE":
        egress = [
            {"to": [_ns_peer(healthcare_ns)]},
            {"to": [_ns_peer("kube-system")],
             "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
        ]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": POLICY_NAME_FMT.format(workload=workload),
            "namespace": healthcare_ns,
            "labels": {"app.kubernetes.io/managed-by": "resilience-agents",
                       "resilience.io/target": workload},
            "annotations": {"resilience.io/stage": stage, **(annotations or {})},
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": workload}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress,
            "egress": egress,
        },
    }
