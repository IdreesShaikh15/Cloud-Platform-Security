"""Entrypoint.

  python -m resilience agent        # run a resilience agent (NODE_ID env var)
  python -m resilience baseline     # run the centralized baseline controller
  python -m resilience compromise --mode false-accusation --target C
  python -m resilience restore      # remove the compromise simulation file
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time


def _setup_logging():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("kubernetes").setLevel(logging.WARNING)


def _loop(node, tick_s: float):
    log = logging.getLogger("loop")
    while True:
        t0 = time.time()
        try:
            node.tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(max(0.05, tick_s - (time.time() - t0)))


def wire_agent(cfg, node_id, signer, registry, tls, telemetry, backend, metrics,
               compromise, bind: str, **agent_kwargs):
    """Build an agent and attach its gRPC server + client.

    The server is stored on the agent (agent.server): grpcio stops a server
    as soon as its Python object is garbage-collected, so a discarded
    `PeerServer(...).start()` silently stops accepting peer connections."""
    from .agent import ResilienceAgent
    from .peer import PeerClient, PeerServer

    agent = ResilienceAgent(cfg, node_id, signer, registry, telemetry, backend, metrics, compromise,
                            **agent_kwargs)
    agent.server = PeerServer(node_id, bind, tls, cfg.node_of_agent, agent.on_envelope,
                              agent.on_investigate).start()
    agent.transport = PeerClient(node_id, cfg.nodes, tls)
    return agent


def run_agent():
    from .config import load_cluster_config
    from .crypto import KeyRegistry, Signer
    from .metrics import MetricsRecorder
    from .monitoring import HttpTelemetrySource
    from .peer import TlsMaterial
    from .response import K8sBackend
    from .simhooks import CompromiseSource
    from .status_server import serve_status

    cfg = load_cluster_config()
    node_id = os.environ["NODE_ID"]
    signer = Signer.from_pem_file(node_id, os.environ.get("SIGNING_KEY", "/etc/resilience/keys/signing.key"))
    registry = KeyRegistry.from_env_or_default()
    tls = TlsMaterial.from_dir(os.environ.get("TLS_DIR", "/etc/resilience/tls"))
    backend = K8sBackend(cfg.healthcare_namespace, cfg.resilience_namespace, cfg.known_good_image)
    metrics = MetricsRecorder(node_id, path=os.environ.get("METRICS_FILE", "/var/log/resilience/metrics.jsonl"))
    bind = os.environ.get("GRPC_BIND", "0.0.0.0:50051")
    from .decisionlog import DEFAULT_PATH
    agent = wire_agent(cfg, node_id, signer, registry, tls, HttpTelemetrySource(cfg), backend,
                       metrics, CompromiseSource(), bind,
                       decision_log_path=os.environ.get("DECISION_LOG", DEFAULT_PATH),
                       trust_state_path=os.environ.get("TRUST_STATE", "/var/log/resilience/trust-state.json"))
    agent.resync_from_cluster()
    logging.getLogger("agent").info("gRPC/mTLS peer server listening on %s", bind)
    serve_status(agent, int(os.environ.get("STATUS_PORT", "8081")))
    logging.getLogger("agent").info("agent %s up (quorum %d of %d)", node_id,
                                    cfg.quorum.quorum, cfg.quorum.n)
    _loop(agent, cfg.timers.tick_s)


def run_baseline():
    from .baseline import CentralController
    from .config import load_cluster_config
    from .metrics import MetricsRecorder
    from .monitoring import HttpTelemetrySource
    from .response import K8sBackend
    from .simhooks import CompromiseSource
    from .status_server import serve_status

    cfg = load_cluster_config()
    backend = K8sBackend(cfg.healthcare_namespace, cfg.resilience_namespace, cfg.known_good_image)
    ctl = CentralController(cfg, HttpTelemetrySource(cfg), backend,
                            MetricsRecorder("CENTRAL", mode="centralized",
                                            path=os.environ.get("METRICS_FILE",
                                                                "/var/log/resilience/metrics.jsonl")),
                            CompromiseSource())
    serve_status(ctl, int(os.environ.get("STATUS_PORT", "8081")))
    logging.getLogger("baseline").info("centralized controller up")
    _loop(ctl, cfg.timers.tick_s)


def run_webhook():
    from .webhook import run_webhook as _run
    _run()


def verify_log(args):
    """python -m resilience verify-log [path]: exit 0 if the chain is intact, 1 (and the first
    broken record) if not."""
    from .decisionlog import DEFAULT_PATH, DecisionLog
    path = args.path or os.environ.get("DECISION_LOG", DEFAULT_PATH)
    ok, idx, reason, n = DecisionLog.verify_file(path)
    if ok:
        print(f"decision log OK: {n} record(s), chain intact ({path})")
        return 0
    print(f"decision log BROKEN at record #{idx}: {reason} ({path})")
    return 1


def compromise(args):
    from .simhooks import DEFAULT_PATH
    os.makedirs(os.path.dirname(DEFAULT_PATH), exist_ok=True)
    data = {"mode": args.mode, "target": args.target, "confidence": args.confidence,
            "impersonate": args.impersonate}
    if args.types:
        data["types"] = args.types.split(",")
    with open(DEFAULT_PATH, "w") as fh:
        json.dump(data, fh)
    print(f"compromise simulation active: {data}")


def restore(_args):
    from .simhooks import DEFAULT_PATH
    try:
        os.remove(DEFAULT_PATH)
    except FileNotFoundError:
        pass
    print("compromise simulation removed; node behaves honestly again")


def main(argv=None):
    _setup_logging()
    p = argparse.ArgumentParser(prog="resilience")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agent")
    sub.add_parser("baseline")
    sub.add_parser("webhook")
    vl = sub.add_parser("verify-log", help="check the tamper-evident decision log of an agent")
    vl.add_argument("path", nargs="?", default=None)
    c = sub.add_parser("compromise")
    c.add_argument("--mode", default="false-accusation",
                   choices=["false-accusation", "forge-evidence"])
    c.add_argument("--target", required=True, help="node id to falsely accuse (A-D)")
    c.add_argument("--confidence", type=float, default=0.95)
    c.add_argument("--types", default="", help="comma list, default all four types")
    c.add_argument("--impersonate", default="B", help="node to impersonate (forge-evidence)")
    sub.add_parser("restore")
    args = p.parse_args(argv)
    handlers = {"agent": lambda a: run_agent(), "baseline": lambda a: run_baseline(),
                "webhook": lambda a: run_webhook(), "verify-log": verify_log,
                "compromise": compromise, "restore": restore}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
