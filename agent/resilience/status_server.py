"""Read-only HTTP status endpoint (consumed by the dashboard and metrics collector).

GET /status   -> agent/controller state (JSON), incl. its event log (last 300)
               ?events_since=<seq>  only events newer than seq
               ?events=0            omit events
GET /metrics  -> per-incident metric summary (JSON)
GET /healthz  -> liveness
There is deliberately no write endpoint.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)


def serve_status(node, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path.startswith("/status"):
                    q = parse_qs(urlparse(self.path).query)
                    since = q.get("events_since", [None])[0]
                    return self._send(200, node.status(
                        events_since=int(since) if since not in (None, "") else None,
                        include_events=q.get("events", ["1"])[0] != "0"))
                if self.path.startswith("/metrics"):
                    return self._send(200, node.metrics_summary())
                if self.path.startswith("/healthz"):
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # never crash the agent because of the dashboard
                log.exception("status handler failed")
                return self._send(500, {"error": str(exc)})

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
