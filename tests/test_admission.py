"""Admission policy + the real HTTPS webhook: a single compromised agent can no longer act alone."""
import copy
import json
import ssl
import tempfile
import urllib.request

import pytest

from resilience.admission import AdmissionPolicy, review_to_response
from resilience.certificate import ANNOTATION, Certificate, QC_KIND, Statement
from resilience.crypto import KeyRegistry, Signer
from resilience.pki import generate, make_webhook_cert
from resilience.reintegration import network_policy
from resilience.webhook import serve

NOW = 2_000_000.0
AGENT = "system:serviceaccount:resilience:resilience-agent"
WORKLOADS = ["patient-portal", "auth-service", "records-api", "database"]
W = "records-api"


@pytest.fixture()
def env():
    signers = {n: Signer.generate(n) for n in "ABCD"}
    reg = KeyRegistry({n: s.public_key() for n, s in signers.items()})
    pol = AdmissionPolicy(WORKLOADS, reg, clock=lambda: NOW, epoch_source=lambda w: 0)
    return signers, reg, pol


def qc(signers, action="CONTAIN", stage="", epoch=1, workload=W, names="ABC", expires=NOW + 300):
    st = Statement(workload, "C", action, stage, epoch, "e" * 64, int(expires * 1000))
    raw = st.canonical()
    return Certificate(raw, [(n, signers[n].sign(QC_KIND, raw)) for n in names]).to_b64()


def policy_obj(stage="QUARANTINE", epoch=1, cert=None, workload=W, spec_of=None, name=None):
    ann = {"resilience.io/epoch": str(epoch)}
    if cert:
        ann[ANNOTATION] = cert
    p = network_policy(workload, stage if stage != "FULL" else "PEER_VALIDATED", "healthcare", "resilience", ann)
    p["metadata"]["annotations"]["resilience.io/stage"] = stage
    if spec_of:
        p["spec"] = network_policy(workload, spec_of, "healthcare", "resilience")["spec"]
    if name:
        p["metadata"]["name"] = name
    return p


def req(kind, op, obj=None, old=None, name=None, user=AGENT):
    name = name or ((obj or old or {}).get("metadata") or {}).get("name") or ""
    return {"uid": "u", "operation": op, "namespace": "healthcare", "kind": {"kind": kind}, "name": name,
            "userInfo": {"username": user}, "object": obj, "oldObject": old}


def deployment(name=W, image="cr-healthcare-app:1.0", replicas=1, ann=None, tmpl_ann=None, extra_env=False):
    c = {"name": "app", "image": image}
    if extra_env:
        c["env"] = [{"name": "EVIL", "value": "1"}]
    return {"metadata": {"name": name, "annotations": dict(ann or {})},
            "spec": {"replicas": replicas, "template": {"metadata": {"annotations": dict(tmpl_ann or {})},
                                                        "spec": {"containers": [c]}}}}


def allowed(pol, r):
    ok, why = pol.review(r)
    return ok, why


# --------------------------------------------------------------------------- NetworkPolicy
def test_isolation_with_a_valid_certificate_is_admitted(env):
    signers, _, pol = env
    obj = policy_obj(cert=qc(signers))
    ok, why = allowed(pol, req("NetworkPolicy", "CREATE", obj))
    assert ok, why


@pytest.mark.parametrize("label,make,fragment", [
    ("no certificate", lambda s: policy_obj(), "no quorum certificate"),
    ("garbage certificate", lambda s: policy_obj(cert="AAAA"), "malformed"),
    ("two signatures only", lambda s: policy_obj(cert=qc(s, names="AB")), "need 3"),
    ("duplicate signer", lambda s: policy_obj(cert=qc(s, names="AAB")), "duplicate signer"),
    ("expired", lambda s: policy_obj(cert=qc(s, expires=NOW - 5)), "expired"),
    ("another workload's certificate", lambda s: policy_obj(cert=qc(s, workload="database")), "workload"),
    ("an ADVANCE certificate for CONTAIN", lambda s: policy_obj(cert=qc(s, action="ADVANCE_STAGE", stage="RESTRICTED")), "authorises"),
    ("an older incident's certificate", lambda s: policy_obj(epoch=2, cert=qc(s, epoch=1)), "incident version"),
])
def test_isolation_rejected_without_a_valid_certificate(env, label, make, fragment):
    signers, _, pol = env
    ok, why = allowed(pol, req("NetworkPolicy", "CREATE", make(signers)))
    assert not ok and fragment in why, (label, why)


def test_valid_certificate_cannot_install_a_weaker_policy(env):
    signers, _, pol = env
    weak = policy_obj(cert=qc(signers), spec_of="PEER_VALIDATED")          # QUARANTINE stage, looser rules
    ok, why = allowed(pol, req("NetworkPolicy", "CREATE", weak))
    assert not ok and "not the platform's own" in why
    tampered = policy_obj(cert=qc(signers))
    tampered["spec"]["egress"] = [{}]                                       # allow-all egress
    assert not allowed(pol, req("NetworkPolicy", "CREATE", tampered))[0]


def test_agents_may_only_manage_isolation_policies(env):
    signers, _, pol = env
    other = policy_obj(cert=qc(signers), name="allow-all")
    ok, why = allowed(pol, req("NetworkPolicy", "CREATE", other))
    assert not ok and "isolation policies" in why
    unknown_workload = policy_obj(cert=qc(signers), name="resilience-isolate-client")
    assert not allowed(pol, req("NetworkPolicy", "CREATE", unknown_workload))[0]


def test_stage_progression_replay_and_regression(env):
    signers, _, pol = env
    q = policy_obj("QUARANTINE", 1, qc(signers))
    r = policy_obj("RESTRICTED", 1, qc(signers, "ADVANCE_STAGE", "RESTRICTED", 1))
    assert allowed(pol, req("NetworkPolicy", "UPDATE", r, old=q))[0]
    # replaying the old QUARANTINE certificate to push the stage back is refused
    back = policy_obj("QUARANTINE", 1, qc(signers))
    ok, why = allowed(pol, req("NetworkPolicy", "UPDATE", back, old=r))
    assert not ok and "backwards" in why
    # a certificate for another stage cannot move this stage
    wrong = policy_obj("MONITORED", 1, qc(signers, "ADVANCE_STAGE", "RESTRICTED", 1))
    assert "stage 'RESTRICTED', not 'MONITORED'" in allowed(pol, req("NetworkPolicy", "UPDATE", wrong, old=r))[1]
    # an older incident can never overwrite a newer one
    old_inc = policy_obj("QUARANTINE", 1, qc(signers, epoch=1))
    newer = policy_obj("QUARANTINE", 2, qc(signers, epoch=2))
    ok, why = allowed(pol, req("NetworkPolicy", "UPDATE", old_inc, old=newer))
    assert not ok and "superseded" in why
    # a new incident must start at QUARANTINE even with a valid certificate for a later stage
    skip = policy_obj("MONITORED", 2, qc(signers, "ADVANCE_STAGE", "MONITORED", 2))
    assert "must start at QUARANTINE" in allowed(pol, req("NetworkPolicy", "UPDATE", skip, old=r))[1]
    assert "must start at QUARANTINE" in allowed(pol, req("NetworkPolicy", "CREATE", r))[1]


def test_deleting_a_policy_needs_a_certified_update_to_full_first(env):
    signers, _, pol = env
    pm = policy_obj("PEER_VALIDATED", 1, qc(signers, "ADVANCE_STAGE", "PEER_VALIDATED", 1))
    ok, why = allowed(pol, req("NetworkPolicy", "DELETE", old=pm, name=pm["metadata"]["name"]))
    assert not ok and "FULL" in why                                          # lifting isolation without FULL
    full = policy_obj("FULL", 1, qc(signers, "ADVANCE_STAGE", "FULL", 1))
    assert allowed(pol, req("NetworkPolicy", "UPDATE", full, old=pm))[0]
    assert allowed(pol, req("NetworkPolicy", "DELETE", old=full, name=full["metadata"]["name"]))[0]
    uncertified = policy_obj("FULL", 1)
    assert not allowed(pol, req("NetworkPolicy", "DELETE", old=uncertified, name=uncertified["metadata"]["name"]))[0]


# --------------------------------------------------------------------------- Deployment
def recover_patch(signers, **kw):
    old = deployment(ann={"resilience.io/epoch": "1"})
    new = deployment(image="cr-healthcare-app:known-good", tmpl_ann={"resilience.io/recovered-at": "1.0"},
                     ann={"resilience.io/epoch": "1", "resilience.io/recovered-epoch": "1",
                          "resilience.io/recovery-requested-at": "1.0", ANNOTATION: qc(signers, **kw)})
    return old, new


def test_recovery_with_certificate_is_admitted_and_without_is_not(env):
    signers, _, pol = env
    old, new = recover_patch(signers)
    assert allowed(pol, req("Deployment", "UPDATE", new, old=old))[0]
    new["metadata"]["annotations"].pop(ANNOTATION)
    ok, why = allowed(pol, req("Deployment", "UPDATE", new, old=old))
    assert not ok and "no quorum certificate" in why


def test_agents_cannot_change_anything_but_the_recovery_fields(env):
    signers, _, pol = env
    old, new = recover_patch(signers)
    scaled = copy.deepcopy(new)
    scaled["spec"]["replicas"] = 0
    assert "only change the recovery fields" in allowed(pol, req("Deployment", "UPDATE", scaled, old=old))[1]
    sneaky = copy.deepcopy(new)
    sneaky["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "EVIL", "value": "1"}]
    assert not allowed(pol, req("Deployment", "UPDATE", sneaky, old=old))[0]
    evil_image = copy.deepcopy(new)
    evil_image["spec"]["template"]["spec"]["containers"][0]["image"] = "attacker/miner:latest"
    ok, why = allowed(pol, req("Deployment", "UPDATE", evil_image, old=old))
    assert not ok and "known-good image" in why
    client = copy.deepcopy(new)
    client["metadata"]["name"] = "client"
    assert "may not change deployment 'client'" in allowed(pol, req("Deployment", "UPDATE", client, old=old))[1]
    assert not allowed(pol, req("Deployment", "DELETE", old=old, name=W))[0]
    assert not allowed(pol, req("Deployment", "CREATE", new, name=W))[0]


def test_deployment_state_writes_need_the_matching_certificate(env):
    signers, _, pol = env
    base = deployment(ann={"resilience.io/epoch": "1", "resilience.io/phase": "ISOLATED"})
    # VALIDATE: phase REINTEGRATING at QUARANTINE
    val = deployment(ann={"resilience.io/epoch": "1", "resilience.io/phase": "REINTEGRATING",
                          "resilience.io/stage": "QUARANTINE", ANNOTATION: qc(signers, "VALIDATE", "", 1)})
    assert allowed(pol, req("Deployment", "UPDATE", val, old=base))[0]
    bad = copy.deepcopy(val)
    bad["metadata"]["annotations"][ANNOTATION] = qc(signers, "CONTAIN", "", 1)
    assert "authorises CONTAIN, not VALIDATE" in allowed(pol, req("Deployment", "UPDATE", bad, old=base))[1]
    # a compromised agent faking "the incident is over" cannot do it without ADVANCE_STAGE(FULL)
    fake = deployment(ann={"resilience.io/epoch": "1", "resilience.io/phase": "HEALTHY",
                           "resilience.io/stage": "FULL"})
    assert not allowed(pol, req("Deployment", "UPDATE", fake, old=base))[0]
    # epochs only move forward
    rollback = deployment(ann={"resilience.io/epoch": "0", "resilience.io/phase": "ISOLATED",
                               ANNOTATION: qc(signers, "CONTAIN", "", 0)})
    assert "superseded" in allowed(pol, req("Deployment", "UPDATE", rollback, old=base))[1]


# --------------------------------------------------------------------------- scope and fail-safe
def test_only_the_agent_identity_is_held_to_the_rule(env):
    _, _, pol = env
    r = req("NetworkPolicy", "CREATE", policy_obj(), user="kubernetes-admin")
    assert pol.review(r) == (True, "not an enforced principal / namespace")
    other_ns = req("NetworkPolicy", "CREATE", policy_obj())
    other_ns["namespace"] = "kube-system"
    assert pol.review(other_ns)[0]
    assert pol.review(req("NetworkPolicy", "CREATE", policy_obj(), user=AGENT))[0] is False


def test_other_resources_are_refused_for_agents(env):
    _, _, pol = env
    r = req("Pod", "CREATE", {"metadata": {"name": "x"}}, name="x")
    ok, why = pol.review(r)
    assert not ok and "may not change Pod" in why


def test_internal_errors_deny_for_safety(env):
    _, _, pol = env
    bad = {"operation": "CREATE", "namespace": "healthcare", "kind": {"kind": "NetworkPolicy"},
           "userInfo": {"username": AGENT}, "name": "resilience-isolate-records-api", "object": "not-a-dict"}
    ok, why = pol.review(bad)
    assert not ok and "denied for safety" in why
    assert review_to_response(pol, {"request": bad})["response"]["allowed"] is False


# --------------------------------------------------------------------------- the real HTTPS webhook
def post(url, body, ctx):
    r = urllib.request.Request(url, data=body if isinstance(body, bytes) else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=5, context=ctx) as resp:
        return json.loads(resp.read())


def test_https_webhook_end_to_end_with_platform_ca():
    d = tempfile.mkdtemp()
    pub = generate(d, {"A": "agent-a", "B": "agent-b", "C": "agent-c", "D": "agent-d"})
    make_webhook_cert(d)
    reg = KeyRegistry.from_b64_map(pub)
    pol = AdmissionPolicy(WORKLOADS, reg, epoch_source=lambda w: 0)            # real clock
    srv = serve(pol, 0, f"{d}/quorum-webhook/tls.crt", f"{d}/quorum-webhook/tls.key", host="127.0.0.1")
    try:
        port = srv.server_address[1]
        ctx = ssl.create_default_context(cafile=f"{d}/ca.crt")                 # trust ONLY the platform CA
        base = f"https://localhost:{port}"
        assert json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=5, context=ctx).read()) == {"ok": True}
        review = {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                  "request": req("NetworkPolicy", "CREATE", policy_obj())}
        out = post(f"{base}/validate", review, ctx)
        assert out["response"]["uid"] == "u" and out["response"]["allowed"] is False
        assert "no quorum certificate" in out["response"]["status"]["message"]
        assert "cr-quorum-webhook" in out["response"]["status"]["message"]
        admin = copy.deepcopy(review)
        admin["request"]["userInfo"]["username"] = "kubernetes-admin"
        assert post(f"{base}/validate", admin, ctx)["response"]["allowed"] is True
        # garbage in -> DENY (fail safe), never an error that a client could mistake for "allowed"
        out = post(f"{base}/validate", b"{not json", ctx)
        assert out["response"]["allowed"] is False and "denied" in out["response"]["status"]["message"]
        # a client that does not trust the platform CA cannot even connect
        with pytest.raises(Exception):
            urllib.request.urlopen(f"{base}/healthz", timeout=5, context=ssl.create_default_context())
    finally:
        srv.shutdown()


def test_generated_webhook_configuration_fails_closed():
    """scripts/gen-certs.py writes failurePolicy: Fail; the manifest is checked by reading the generator."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "scripts", "gen-certs.py")).read()
    assert '"failurePolicy": "Fail"' in text and '"sideEffects": "None"' in text
    assert "networkpolicies" in text and "deployments" in text


# --------------------------------------------------------------------------- replay of an old certificate
def test_old_certificate_cannot_create_a_new_isolation(env):
    """A still-valid CONTAIN certificate from a FINISHED incident must not let one compromised
    agent re-isolate a healthy workload: CREATE must be exactly the next incident version."""
    signers, reg, _ = env
    cert_e1 = qc(signers, epoch=1)
    fresh = AdmissionPolicy(WORKLOADS, reg, clock=lambda: NOW, epoch_source=lambda w: 0)
    assert fresh.review(req("NetworkPolicy", "CREATE", policy_obj(cert=cert_e1)))[0]
    after = AdmissionPolicy(WORKLOADS, reg, clock=lambda: NOW, epoch_source=lambda w: 1)   # incident 1 already happened
    ok, why = after.review(req("NetworkPolicy", "CREATE", policy_obj(cert=cert_e1)))
    assert not ok and "replay or stale" in why
    assert after.review(req("NetworkPolicy", "CREATE", policy_obj(epoch=2, cert=qc(signers, epoch=2))))[0]
    unreadable = AdmissionPolicy(WORKLOADS, reg, clock=lambda: NOW, epoch_source=lambda w: None)
    assert "denied for safety" in unreadable.review(req("NetworkPolicy", "CREATE", policy_obj(cert=cert_e1)))[1]
    no_source = AdmissionPolicy(WORKLOADS, reg, clock=lambda: NOW)
    assert not no_source.review(req("NetworkPolicy", "CREATE", policy_obj(cert=cert_e1)))[0]


def test_old_certificate_cannot_rewind_the_recorded_state(env):
    signers, _, pol = env
    done = deployment(ann={"resilience.io/epoch": "1", "resilience.io/phase": "HEALTHY", "resilience.io/stage": "FULL"})
    rewind = deployment(ann={"resilience.io/epoch": "1", "resilience.io/phase": "ISOLATED",
                             ANNOTATION: qc(signers, "CONTAIN", "", 1)})
    ok, why = allowed(pol, req("Deployment", "UPDATE", rewind, old=done))
    assert not ok and "backwards" in why
    forward = deployment(ann={"resilience.io/epoch": "2", "resilience.io/phase": "ISOLATED",
                              ANNOTATION: qc(signers, "CONTAIN", "", 2)})
    assert allowed(pol, req("Deployment", "UPDATE", forward, old=done))[0]
