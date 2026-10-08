"""End to end in the simulator: the whole pipeline under certificate enforcement, and a single
compromised agent trying to act alone (the simulated cluster runs the SAME admission policy as the
real webhook)."""
import dataclasses
import time

import pytest

from resilience.certificate import ANNOTATION, Certificate, QC_KIND, Statement
from resilience.config import CertParams
from resilience.response import AdmissionDenied
from world import LocalCluster


@pytest.fixture()
def cluster():
    c = LocalCluster(base_port=51101).start()
    time.sleep(1.5)
    yield c
    c.stop()


def genuine_attack_completed(c, target="C", timeout=90, alive="ABCD"):
    c.mark("app-compromise", target)
    c.world.attack(target)
    assert c.wait_until(lambda: all(c.agents[n].states[target].phase == "HEALTHY"
                                    and c.agents[n].states[target].epoch == 1 for n in alive), timeout)
    assert c.wait_until(lambda: any(x[1] == "apply_stage" and x[3] == "FULL" for x in c.backend.log), 20)


def test_whole_pipeline_runs_under_enforcement_and_every_change_carried_a_certificate(cluster):
    c = cluster
    genuine_attack_completed(c)
    assert c.backend.denied == [], f"the platform's own actions were refused: {c.backend.denied}"
    stages = [x[3] for x in c.backend.log if x[1] == "apply_stage" and x[2] == "records-api"]
    assert stages == ["QUARANTINE", "RESTRICTED", "MONITORED", "PEER_VALIDATED", "FULL"]
    kinds = {(k, op) for _, k, op, _, _ in c.backend.admitted}
    assert ("NetworkPolicy", "CREATE") in kinds and ("NetworkPolicy", "UPDATE") in kinds
    assert ("NetworkPolicy", "DELETE") in kinds and ("Deployment", "UPDATE") in kinds
    assert len(c.backend.admitted) >= 12
    # every agent holds the certificates it needed, each signed by at least 3 distinct agents
    for a in c.agents.values():
        cert = a.certs.get("CONTAIN:C:1:")
        assert cert is not None and len(set(cert.signers())) >= 3
    # and the decision log recorded the incident intact
    for a in c.agents.values():
        st = a.decision_log.status()
        assert st["ok"] and st["length"] >= 6


def test_a_lone_compromised_agent_cannot_isolate_a_healthy_workload(cluster):
    c = cluster
    a = c.agents["A"]
    # 1. straight to the API with no certificate at all
    with pytest.raises(AdmissionDenied, match="no quorum certificate"):
        c.backend.apply_stage("auth-service", "QUARANTINE", {"resilience.io/epoch": "1"})
    # 2. with a "certificate" carrying only its own signature
    st = Statement("auth-service", "B", "CONTAIN", "", 1, "0" * 64, int((time.time() + 300) * 1000))
    raw = st.canonical()
    one = Certificate(raw, [("A", a.signer.sign(QC_KIND, raw))]).to_b64()
    with pytest.raises(AdmissionDenied, match="need 3"):
        c.backend.apply_stage("auth-service", "QUARANTINE", {"resilience.io/epoch": "1", ANNOTATION: one})
    # 3. signing three times, claiming to be B and C as well
    forged = Certificate(raw, [(n, a.signer.sign(QC_KIND, raw)) for n in "ABC"]).to_b64()
    with pytest.raises(AdmissionDenied, match="invalid signature"):
        c.backend.apply_stage("auth-service", "QUARANTINE", {"resilience.io/epoch": "1", ANNOTATION: forged})
    # 4. harming the workload some other way
    with pytest.raises(AdmissionDenied):
        c.backend._patch_deployment("auth-service", {"resilience.io/epoch": "1"}, image="attacker/miner:latest")
    with pytest.raises(AdmissionDenied):
        c.backend._patch_deployment("database", {}, template_ann={})                 # no certificate, no change
    assert c.backend.policies == {} and c.backend.log == [], "nothing was applied"
    assert len(c.backend.denied) == 5


def test_a_compromised_agent_whose_own_checks_are_off_is_still_stopped_and_the_others_carry_on(cluster):
    """Agent A (executor rank 0) has its in-agent certificate check switched off, as a compromised agent
    would. The admission webhook (the second, independent layer) refuses its changes; agent B fails over
    and completes the pipeline with a real certificate."""
    c = cluster
    a = c.agents["A"]
    a.cfg = dataclasses.replace(a.cfg, certificates=CertParams(enforce=False))
    genuine_attack_completed(c)
    refused = [d for d in c.backend.denied if "no quorum certificate" in d[4]]
    assert refused, "A's uncertified attempts must have been refused by the webhook"
    acted = {n: [e for e in c.agents[n].events.by_category("ACTION") if e["details"].get("action") == "isolate"]
             for n in "ABCD"}
    assert not acted["A"], "A never managed to isolate anything"
    assert acted["B"] or acted["C"] or acted["D"], "a certified agent performed the isolation"
    stages = [x[3] for x in c.backend.log if x[1] == "apply_stage"]
    assert stages[0] == "QUARANTINE" and stages[-1] == "FULL"


def test_false_accusation_never_yields_a_certificate_and_nothing_is_changed(cluster):
    c = cluster
    c.compromise_agent("A", "B")
    assert c.wait_until(lambda: all(c.agents[n].agent_trust.get("A") < 40 for n in "BCD"), 45)
    time.sleep(1.5)
    for a in c.agents.values():
        assert not [k for k in a.certs.complete if ":B:" in k], "a certificate for the healthy node exists!"
    assert c.backend.denied == [] and c.backend.log == []
    c.restore_agent("A")
    # once excluded, A's further claims are rejected entirely at the others
    b = c.agents["C"]
    assert b.excluded_pool.recent(time.time(), 30, origin="A"), "A's later evidence was quarantined, not scored"
    assert any(e["details"].get("flag") == "EVIDENCE_REJECTED" for e in b.events.by_category("FLAG"))


def test_a_certificate_for_one_workload_cannot_be_reused_on_another(cluster):
    c = cluster
    genuine_attack_completed(c)
    cert = c.agents["B"].certs.get("CONTAIN:C:1:")
    assert cert is not None
    with pytest.raises(AdmissionDenied):                                       # same signatures, other workload
        c.backend.apply_stage("database", "QUARANTINE", {"resilience.io/epoch": "1", ANNOTATION: cert.to_b64()})
    # the finished incident's certificate cannot start a new isolation of the same workload either
    with pytest.raises(AdmissionDenied, match="replay or stale"):
        c.backend.apply_stage("records-api", "QUARANTINE", {"resilience.io/epoch": "1", ANNOTATION: cert.to_b64()})


def test_with_one_agent_crashed_three_still_certify(cluster):
    c = cluster
    c.agents["D"].tick()
    c.crash_agent("D")
    genuine_attack_completed(c, alive="ABC")
    cert = c.agents["A"].certs.get("CONTAIN:C:1:")
    assert cert is not None and set(cert.signers()) <= {"A", "B", "C"} and len(cert.signers()) == 3
    assert c.backend.denied == []
