"""The quorum-certificate validating admission webhook (HTTPS server).

Run as `python -m resilience webhook` in a small Deployment (same image as the agents). The
Kubernetes API server sends it an AdmissionReview for every NetworkPolicy / Deployment change in
the healthcare namespace; it answers allow/deny using admission.AdmissionPolicy.

Almost everything it checks is in the request itself. The one exception: to stop an old, still-valid
certificate from being replayed to CREATE a new isolation, it reads each workload's current incident
version (annotation resilience.io/epoch) with a read-only `get` of the 4 workload Deployments (its own
service account, see k8s/resilience/00-rbac.yaml). If that read fails the CREATE is denied. It keeps no
state. It trusts only the platform's public-key registry (ConfigMap peer-pubkeys). It never logs a
certificate, only its decision.

FAIL-SAFE: any error while handling a request answers DENY (HTTP 200 with allowed=false). The
ValidatingWebhookConfiguration uses failurePolicy: Fail, so if this process is down the API server
rejects the agents' changes rather than letting them through.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .admission import AdmissionPolicy, review_to_response

log = logging.getLogger("webhook")
MAX_BODY = 2 * 1024 * 1024


def make_handler(policy: AdmissionPolicy):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):          # no access log: never write request bodies anywhere
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/healthz"):
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            uid = ""
            try:
                if not self.path.startswith("/validate"):
                    return self._send(404, {"error": "not found"})
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_BODY:
                    raise ValueError("bad body size")
                review = json.loads(self.rfile.read(n))
                uid = ((review.get("request") or {}).get("uid")) or ""
                out = review_to_response(policy, review)
                r = out["response"]
                req = review.get("request") or {}
                log.info("%s %s %s/%s by %s -> %s%s", req.get("operation"),
                         (req.get("kind") or {}).get("kind"), req.get("namespace"), req.get("name"),
                         (req.get("userInfo") or {}).get("username"),
                         "ALLOW" if r["allowed"] else "DENY",
                         "" if r["allowed"] else f" ({r['status']['message']})")
                return self._send(200, out)
            except Exception as exc:            # fail safe: deny
                log.exception("webhook request failed")
                return self._send(200, {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                                        "response": {"uid": uid, "allowed": False,
                                                     "status": {"code": 403, "message":
                                                                f"cr-quorum-webhook: internal error, denied: {exc}"}}})
    return Handler


def serve(policy: AdmissionPolicy, port: int, certfile: Optional[str] = None,
          keyfile: Optional[str] = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(policy))
    srv.daemon_threads = True
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile, keyfile)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def kubernetes_epoch_source(namespace: str, timeout=(2.0, 3.0)):
    """workload -> current incident version from the Deployment's resilience.io/epoch annotation
    (0 if it has never had an incident); None if the cluster cannot be read."""
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    apps = client.AppsV1Api()

    def epoch_of(workload: str) -> Optional[int]:
        try:
            d = apps.read_namespaced_deployment(workload, namespace, _request_timeout=timeout)
            return int((d.metadata.annotations or {}).get("resilience.io/epoch", "0"))
        except Exception as exc:
            log.warning("could not read incident version of %s: %s", workload, exc)
            return None
    return epoch_of


def run_webhook() -> None:
    from .config import load_cluster_config
    from .crypto import KeyRegistry
    cfg = load_cluster_config()
    registry = KeyRegistry.from_env_or_default()
    policy = AdmissionPolicy.from_config(cfg, registry,
                                         epoch_source=kubernetes_epoch_source(cfg.healthcare_namespace))
    tls = os.environ.get("WEBHOOK_TLS_DIR", "/etc/resilience/webhook-tls")
    port = int(os.environ.get("WEBHOOK_PORT", "8443"))
    serve(policy, port, os.path.join(tls, "tls.crt"), os.path.join(tls, "tls.key"))
    log.info("quorum webhook listening on :%d (enforced users: %s)", port, ", ".join(sorted(policy.enforced)))
    import time
    while True:
        time.sleep(3600)
