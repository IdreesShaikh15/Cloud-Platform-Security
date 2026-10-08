"""Peer communication: gRPC over mutual TLS.

* Server requires a client certificate signed by the platform CA
  (`require_client_auth=True`); the certificate CN (agent-a ... agent-d) is
  mapped to the node id and handed to the agent as the *transport identity*.
* Client presents its own certificate and verifies the server against the CA.
* Messages are SignedEnvelopes (see evidence.py); the agent rejects any
  envelope whose signer differs from the transport identity.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent import futures
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import grpc

from .config import NodeSpec
from .proto import resilience_pb2 as pb
from .proto import resilience_pb2_grpc as pb_grpc

log = logging.getLogger(__name__)

# (envelope, transport node id or None) -> (accepted, reason)
EnvelopeHandler = Callable[[pb.SignedEnvelope, Optional[str]], Tuple[bool, str]]
# (signed investigate request, transport node id) -> (accepted, reason, signed response or None)
InvestigateHandler = Callable[[pb.SignedEnvelope, Optional[str]],
                              Tuple[bool, str, Optional[pb.SignedEnvelope]]]


@dataclass
class TlsMaterial:
    ca: bytes
    cert: bytes
    key: bytes

    @classmethod
    def from_dir(cls, path: str) -> "TlsMaterial":
        def rd(name):
            with open(os.path.join(path, name), "rb") as fh:
                return fh.read()
        return cls(ca=rd("ca.crt"), cert=rd("tls.crt"), key=rd("tls.key"))


class _Servicer(pb_grpc.ResiliencePeerServicer):
    def __init__(self, node_id: str, cn_to_node: Callable[[str], Optional[str]],
                 on_envelope: EnvelopeHandler, on_investigate: Optional[InvestigateHandler] = None):
        self.node_id = node_id
        self.cn_to_node = cn_to_node
        self.on_envelope = on_envelope
        self.on_investigate = on_investigate

    def _identity(self, context) -> Optional[str]:
        cns = context.auth_context().get("x509_common_name") or []
        if not cns:
            return None
        return self.cn_to_node(cns[0].decode())

    def _handle(self, request, context, expected_kind: str) -> pb.Ack:
        ident = self._identity(context)
        if ident is None:
            return pb.Ack(accepted=False, reason="unauthenticated peer")
        if request.kind != expected_kind:
            return pb.Ack(accepted=False, reason=f"expected {expected_kind}, got {request.kind}")
        ok, reason = self.on_envelope(request, ident)
        return pb.Ack(accepted=ok, reason=reason)

    def SubmitEvidence(self, request, context):
        return self._handle(request, context, "evidence")

    def SubmitVote(self, request, context):
        return self._handle(request, context, "vote")

    def SubmitShare(self, request, context):
        return self._handle(request, context, "cert_share")

    def Ping(self, request, context):
        return pb.PingReply(node=self.node_id, timestamp=time.time())

    def Investigate(self, request, context):
        ident = self._identity(context)
        if ident is None:
            return pb.InvestigateResult(accepted=False, reason="unauthenticated peer")
        if request.kind != "inv_request":
            return pb.InvestigateResult(accepted=False, reason=f"expected inv_request, got {request.kind}")
        if self.on_investigate is None:
            return pb.InvestigateResult(accepted=False, reason="investigation not supported")
        try:
            ok, reason, resp = self.on_investigate(request, ident)
        except Exception as exc:          # a bug in the handler must not crash the peer server
            log.exception("investigate handler failed")
            return pb.InvestigateResult(accepted=False, reason=f"internal error: {exc}")
        return pb.InvestigateResult(accepted=ok, reason=reason, response=resp)


class PeerServer:
    def __init__(self, node_id: str, bind: str, tls: TlsMaterial,
                 cn_to_node: Callable[[str], Optional[str]], on_envelope: EnvelopeHandler,
                 on_investigate: Optional[InvestigateHandler] = None):
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
        pb_grpc.add_ResiliencePeerServicer_to_server(
            _Servicer(node_id, cn_to_node, on_envelope, on_investigate), self.server)
        creds = grpc.ssl_server_credentials([(tls.key, tls.cert)], root_certificates=tls.ca,
                                            require_client_auth=True)
        self.port = self.server.add_secure_port(bind, creds)
        if self.port == 0:
            raise RuntimeError(f"could not bind gRPC server to {bind}")

    def start(self):
        self.server.start()
        return self

    def stop(self, grace: float = 0.5):
        self.server.stop(grace)


class PeerClient:
    def __init__(self, node_id: str, peers: Dict[str, NodeSpec], tls: TlsMaterial,
                 timeout_s: float = 2.0):
        self.node_id = node_id
        self.timeout = timeout_s
        creds = grpc.ssl_channel_credentials(root_certificates=tls.ca, private_key=tls.key,
                                             certificate_chain=tls.cert)
        self.stubs: Dict[str, pb_grpc.ResiliencePeerStub] = {}
        for nid, spec in peers.items():
            if nid == node_id:
                continue
            ch = grpc.secure_channel(spec.agent_addr, creds, options=[
                ("grpc.ssl_target_name_override", spec.agent_name),
                ("grpc.keepalive_time_ms", 10000)])
            self.stubs[nid] = pb_grpc.ResiliencePeerStub(ch)
        self._pool = futures.ThreadPoolExecutor(max_workers=max(4, 2 * len(self.stubs)))
        self._lock = threading.Lock()
        self.last_ok: Dict[str, float] = {}
        self.last_err: Dict[str, str] = {}
        self._down: Dict[str, bool] = {}
        # Optional observability hook: fn(peer_id, up: bool, detail: str),
        # called on each link down / restored transition.
        self.link_listener: Optional[Callable[[str, bool, str], None]] = None

    def _send(self, nid: str, env: pb.SignedEnvelope):
        stub = self.stubs[nid]
        call = {"evidence": stub.SubmitEvidence, "vote": stub.SubmitVote,
                "cert_share": stub.SubmitShare}[env.kind]
        try:
            ack = call(env, timeout=self.timeout)
            with self._lock:
                self.last_ok[nid] = time.time()
                was_down = self._down.pop(nid, False)
            if was_down:
                log.info("peer link to %s restored", nid)
                self._notify(nid, True, "link restored")
            if not ack.accepted:
                log.debug("peer %s rejected our %s: %s", nid, env.kind, ack.reason)
            return ack
        except grpc.RpcError as exc:
            with self._lock:
                self.last_err[nid] = f"{exc.code().name}: {exc.details()}"
                newly_down = not self._down.get(nid)
                self._down[nid] = True
            # Warn once per outage; an unreachable peer means our evidence and
            # votes are silently lost, so it must be visible in the logs.
            if newly_down:
                log.warning("peer link to %s DOWN (%s): %s", nid, env.kind, self.last_err[nid])
                self._notify(nid, False, self.last_err[nid])
            return None

    def investigate(self, nid: str, env: pb.SignedEnvelope, timeout_s: Optional[float] = None
                    ) -> Optional[pb.InvestigateResult]:
        """Ask peer `nid` for fresh signed observations. None = no answer (peer down,
        timeout): the caller must treat that as *unknown*, never as normal or as an attack."""
        try:
            res = self.stubs[nid].Investigate(env, timeout=timeout_s or self.timeout)
        except grpc.RpcError as exc:
            with self._lock:
                self.last_err[nid] = f"{exc.code().name}: {exc.details()}"
            return None
        with self._lock:
            self.last_ok[nid] = time.time()
        return res

    def investigate_async(self, nid: str, env: pb.SignedEnvelope, callback,
                          timeout_s: Optional[float] = None):
        """Run investigate() on the client's thread pool; callback(nid, result_or_None)."""
        def run():
            callback(nid, self.investigate(nid, env, timeout_s))
        return self._pool.submit(run)

    def _notify(self, nid: str, up: bool, detail: str) -> None:
        if self.link_listener is not None:
            try:
                self.link_listener(nid, up, detail)
            except Exception:
                log.debug("link listener failed", exc_info=True)

    def broadcast(self, env: pb.SignedEnvelope, wait: bool = False):
        futs = [self._pool.submit(self._send, nid, env) for nid in self.stubs]
        if wait:
            return [f.result() for f in futs]
        return futs

    def peer_status(self) -> Dict[str, dict]:
        now = time.time()
        with self._lock:
            return {nid: {"last_ok_ago_s": round(now - self.last_ok[nid], 1) if nid in self.last_ok else None,
                          "last_error": self.last_err.get(nid)} for nid in self.stubs}
