"""Least-privilege RBAC. A tiny evaluator reads k8s/resilience/00-rbac.yaml with Kubernetes' own rule
semantics; the permissions the platform NEEDS are derived from what K8sBackend really calls.

This checks the manifest's logic; it is not a real API server. scripts/verify-rbac.sh does the same
check against a live cluster with `kubectl auth can-i`."""
import os
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import yaml

from resilience.response import K8sBackend, MARKER_CONFIGMAP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = list(yaml.safe_load_all(open(os.path.join(ROOT, "k8s", "resilience", "00-rbac.yaml"))))
WORKLOADS = ["patient-portal", "auth-service", "records-api", "database"]
AGENT = ("resilience", "resilience-agent")
BASELINE = ("resilience", "resilience-baseline")


def roles_for(sa, namespace):
    """Role rules bound to service account `sa` in `namespace` (RoleBindings only; no ClusterRoles exist)."""
    rules = []
    for b in (d for d in DOCS if d["kind"] == "RoleBinding" and d["metadata"]["namespace"] == namespace):
        if any(s["kind"] == "ServiceAccount" and (s["namespace"], s["name"]) == sa for s in b["subjects"]):
            role = next(d for d in DOCS if d["kind"] == "Role" and d["metadata"]["name"] == b["roleRef"]["name"]
                        and d["metadata"]["namespace"] == namespace)
            rules += role["rules"]
    return rules


def allowed(sa, namespace, verb, group, resource, name=None):
    for r in roles_for(sa, namespace):
        if verb not in r["verbs"] and "*" not in r["verbs"]:
            continue
        if group not in r["apiGroups"] and "*" not in r["apiGroups"]:
            continue
        if resource not in r["resources"] and "*" not in r["resources"]:
            continue
        names = r.get("resourceNames")
        if names:
            # resourceNames never match requests that carry no name (create, list, watch)
            if name is None or name not in names:
                continue
        return True
    return False


def test_no_cluster_wide_permissions_exist():
    assert not [d for d in DOCS if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]


# --------------------------------------------------------------------------- derived from the real code
VERB = {"read": "get", "replace": "update", "patch": "patch", "create": "create", "delete": "delete"}
RESOURCE = {"network_policy": ("networking.k8s.io", "networkpolicies"),
            "deployment": ("apps", "deployments"), "config_map": ("", "configmaps")}


def needed_permissions():
    """Every Kubernetes call K8sBackend makes (for every stage and action) as (ns, verb, group, resource, name)."""
    b = object.__new__(K8sBackend)
    b.hc_ns, b.res_ns, b.image, b.t = "healthcare", "resilience", "known-good", {}
    b.net, b.apps, b.core = MagicMock(), MagicMock(), MagicMock()
    b.apps.read_namespaced_deployment.return_value = NS(
        metadata=NS(annotations={"resilience.io/recovered-epoch": "1"}, generation=1), spec=NS(replicas=1),
        status=NS(observed_generation=1, updated_replicas=1, ready_replicas=1, replicas=1))
    b.core.read_namespaced_config_map.return_value = NS(data={"marker": "{}"})
    b.net.read_namespaced_network_policy.return_value = NS(metadata=NS(annotations={"resilience.io/stage": "X"}))
    for w in WORKLOADS:
        for stage in ("QUARANTINE", "RESTRICTED", "MONITORED", "PEER_VALIDATED", "FULL"):
            b.apply_stage(w, stage, {})
        b.current_stage(w)
        b.recover(w, 1, "qc")
        b.recovery_done(w, 1)
        b.write_state(w, {"phase": "X"}, "qc")
        b.read_state(w)
    b.read_marker()
    # a first isolation: replace -> 404 -> create
    from kubernetes.client.rest import ApiException
    b.net.replace_namespaced_network_policy.side_effect = ApiException(status=404)
    b.apply_stage("records-api", "QUARANTINE", {})
    need = set()
    for api, ns_arg in ((b.net, "healthcare"), (b.apps, "healthcare"), (b.core, "resilience")):
        for call in api.method_calls:
            method = call[0]
            verb_word, rest = method.split("_", 1)
            res = rest.replace("namespaced_", "")
            group, resource = RESOURCE[res]
            args = call[1]
            ns = ns_arg
            if verb_word == "create":
                name, ns = None, args[0]
            else:
                name, ns = args[0], args[1]
            need.add((ns, VERB[verb_word], group, resource, name))
    return need


def test_every_call_the_platform_makes_is_allowed():
    need = needed_permissions()
    assert len(need) >= 8
    for sa in (AGENT, BASELINE):
        for ns, verb, group, resource, name in need:
            assert allowed(sa, ns, verb, group, resource, name), \
                f"{sa[1]} lacks {verb} {resource}/{name} in {ns}"


@pytest.mark.parametrize("sa", [AGENT, BASELINE])
@pytest.mark.parametrize("ns,verb,group,resource,name", [
    ("healthcare", "list", "", "pods", None),                                   # pods: unused, removed
    ("healthcare", "get", "", "pods", "records-api-abc"),
    ("healthcare", "delete", "apps", "deployments", "records-api"),             # may never delete a workload
    ("healthcare", "create", "apps", "deployments", None),
    ("healthcare", "patch", "apps", "deployments", "client"),                   # the client is not ours
    ("healthcare", "update", "apps", "deployments", "records-api"),             # patch only
    ("healthcare", "list", "apps", "deployments", None),
    ("healthcare", "update", "networking.k8s.io", "networkpolicies", "allow-all"),
    ("healthcare", "delete", "networking.k8s.io", "networkpolicies", "allow-all"),
    ("healthcare", "get", "networking.k8s.io", "networkpolicies", "someone-elses-policy"),
    ("healthcare", "patch", "networking.k8s.io", "networkpolicies", "resilience-isolate-records-api"),
    ("healthcare", "list", "networking.k8s.io", "networkpolicies", None),
    ("healthcare", "get", "", "secrets", "anything"),
    ("resilience", "get", "", "configmaps", "resilience-config"),                # only the attack marker
    ("resilience", "get", "", "configmaps", "peer-pubkeys"),
    ("resilience", "list", "", "configmaps", None),
    ("resilience", "get", "", "secrets", "agent-a-signing"),                     # signing keys stay unreachable
    ("resilience", "create", "apps", "deployments", None),
    ("kube-system", "get", "", "pods", "coredns"),                                # no other namespace at all
    ("default", "create", "networking.k8s.io", "networkpolicies", None),
])
def test_forbidden_operations_are_refused(sa, ns, verb, group, resource, name):
    assert not allowed(sa, ns, verb, group, resource, name)


def test_the_agent_and_baseline_are_separate_identities_and_other_pods_have_none():
    sas = {d["metadata"]["name"] for d in DOCS if d["kind"] == "ServiceAccount"}
    assert sas == {"resilience-agent", "resilience-baseline", "quorum-webhook"}
    assert not allowed(("resilience", "dashboard"), "healthcare", "get", "apps", "deployments", "records-api")
    assert not allowed(("resilience", "default"), "resilience", "get", "", "configmaps", MARKER_CONFIGMAP)


def test_manifests_use_the_right_service_accounts():
    agents = open(os.path.join(ROOT, "k8s", "resilience", "20-agents.yaml")).read()
    base = open(os.path.join(ROOT, "k8s", "baseline", "central-controller.yaml")).read()
    web = open(os.path.join(ROOT, "k8s", "resilience", "40-webhook.yaml")).read()
    assert agents.count("serviceAccountName: resilience-agent") == 4
    assert "serviceAccountName: resilience-baseline" in base and "resilience-agent" not in base
    assert "serviceAccountName: quorum-webhook" in web
    WEB = ("resilience", "quorum-webhook")
    for w in WORKLOADS:                                  # the webhook may only READ the four workloads
        assert allowed(WEB, "healthcare", "get", "apps", "deployments", w)
        assert not allowed(WEB, "healthcare", "patch", "apps", "deployments", w)
    assert not allowed(WEB, "healthcare", "get", "apps", "deployments", "client")
    assert not allowed(WEB, "healthcare", "create", "networking.k8s.io", "networkpolicies")
    assert not allowed(WEB, "resilience", "get", "", "secrets", "agent-a-signing")


def test_resource_name_lists_cover_exactly_the_four_workloads():
    text = open(os.path.join(ROOT, "k8s", "base", "10-healthcare-apps.yaml")).read()
    for w in WORKLOADS:
        assert f"name: {w}\n" in text
        assert allowed(AGENT, "healthcare", "patch", "apps", "deployments", w)
        assert allowed(AGENT, "healthcare", "delete", "networking.k8s.io", "networkpolicies", f"resilience-isolate-{w}")
