#!/usr/bin/env python3
"""Generate the platform PKI and turn it into Kubernetes Secrets/ConfigMaps.

  python3 scripts/gen-certs.py            # writes certs/ and k8s/generated/pki.json

Creates:
  certs/ca.{crt,key}                       platform CA (keep ca.key private)
  certs/agent-x/{ca.crt,tls.crt,tls.key}   mTLS identity of each agent (CN=agent-x)
  certs/agent-x/signing.key                Ed25519 evidence-signing key
  certs/pubkeys.json                       Ed25519 public-key registry
  k8s/generated/pki.json                   Secrets agent-x-tls, agent-x-signing
                                           + ConfigMap peer-pubkeys (kubectl apply -f)
Re-running rotates every key (restart the agents afterwards).
"""
import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))

from resilience.pki import generate, make_webhook_cert  # noqa: E402

AGENTS = {"A": "agent-a", "B": "agent-b", "C": "agent-c", "D": "agent-d"}
NS = "resilience"


def b64file(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def main():
    outdir = os.path.join(ROOT, "certs")
    pubkeys = generate(outdir, AGENTS, NS)
    items = []
    for node_id, name in AGENTS.items():
        d = os.path.join(outdir, name)
        items.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                      "metadata": {"name": f"{name}-tls", "namespace": NS},
                      "data": {f: b64file(os.path.join(d, f)) for f in ("ca.crt", "tls.crt", "tls.key")}})
        items.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                      "metadata": {"name": f"{name}-signing", "namespace": NS},
                      "data": {"signing.key": b64file(os.path.join(d, "signing.key"))}})
    # ---- admission webhook: server certificate signed by the platform CA, plus its Secret
    ca_pem = make_webhook_cert(outdir, "quorum-webhook", NS)
    wd = os.path.join(outdir, "quorum-webhook")
    items.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": "webhook-tls", "namespace": NS},
                  "data": {"tls.crt": b64file(os.path.join(wd, "tls.crt")),
                           "tls.key": b64file(os.path.join(wd, "tls.key"))}})
    items.append({"apiVersion": "v1", "kind": "ConfigMap",
                  "metadata": {"name": "peer-pubkeys", "namespace": NS},
                  "data": {"pubkeys.json": json.dumps(pubkeys, indent=2)}})
    gen = os.path.join(ROOT, "k8s", "generated")
    os.makedirs(gen, exist_ok=True)
    with open(os.path.join(gen, "pki.json"), "w") as fh:
        json.dump({"apiVersion": "v1", "kind": "List", "items": items}, fh, indent=2)
    # The ValidatingWebhookConfiguration embeds the CA that signed the webhook's certificate.
    hook = {
        "apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingWebhookConfiguration",
        "metadata": {"name": "cr-quorum-webhook"},
        "webhooks": [{
            "name": "quorum.resilience.io", "admissionReviewVersions": ["v1"],
            "sideEffects": "None",           # lets `kubectl --dry-run=server` exercise it safely
            "failurePolicy": "Fail",         # webhook down => the agents' changes are REFUSED (fail safe)
            "timeoutSeconds": 5, "matchPolicy": "Equivalent",
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "healthcare"}},
            "rules": [
                {"apiGroups": ["networking.k8s.io"], "apiVersions": ["v1"],
                 "operations": ["CREATE", "UPDATE", "DELETE"], "resources": ["networkpolicies"]},
                {"apiGroups": ["apps"], "apiVersions": ["v1"],
                 "operations": ["CREATE", "UPDATE", "DELETE"], "resources": ["deployments"]}],
            "clientConfig": {"service": {"name": "quorum-webhook", "namespace": NS, "path": "/validate", "port": 443},
                             "caBundle": base64.b64encode(ca_pem.encode()).decode()}}]}
    with open(os.path.join(gen, "webhook-config.json"), "w") as fh:
        json.dump(hook, fh, indent=2)
    print(f"PKI written to {outdir}/ and {gen}/pki.json (+ webhook-config.json)")
    for nid, pk in pubkeys.items():
        print(f"  node {nid} ({AGENTS[nid]}) Ed25519 pubkey {pk}")


if __name__ == "__main__":
    main()
