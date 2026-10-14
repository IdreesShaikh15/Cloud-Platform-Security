"""The workload definitions: ONE configuration file (config/workloads.json), validated.

    {
      "client": "client",                                   # name of the synthetic availability probe
      "workloads": {                                        # exactly four, one per resilience node A-D
        "A": {"name": "patient-portal", "role": "patient-portal", "description": "..."},
        ...
      }
    }

name   the Kubernetes Deployment and Service name (what the agents isolate and redeploy).
role   which behaviour the container runs: the app image implements exactly four roles (KNOWN_ROLES), selected by its ROLE
       environment variable. Each role is used by exactly one workload because the services find each other by role.
node   A-D: the resilience node (and agent) that is co-located with the workload. Fixed by the architecture: four identical
       peer agents, one workload each.

Defaults are the original four (name == role). docs/WORKLOADS.md describes how the file is used and how the Kubernetes manifests
are generated from it (scripts/gen-workloads.py). Invalid or conflicting definitions are REJECTED with every problem listed;
an explicitly requested file that is missing or unreadable is an error, never a silent fallback to the defaults.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

NODES: Tuple[str, ...] = ("A", "B", "C", "D")
KNOWN_ROLES: Tuple[str, ...] = ("patient-portal", "auth-service", "records-api", "database")
NAME_MAX = 40                                   # "resilience-isolate-" + name must stay a valid 63-character label
# DNS-1035 label: Kubernetes Service names must start with a letter
DNS_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
RESERVED_NAMES = {"dashboard", "central-controller", "quorum-webhook", "kubernetes", "default", "healthcare", "resilience"}
RESERVED_PREFIXES = ("agent-", "resilience-", "kube-")
ALLOWED_TOP = {"client", "workloads"}
ALLOWED_WORKLOAD = {"name", "role", "description"}
ENV_VAR = "WORKLOADS_CONFIG"

DEFAULTS = {
    "client": "client",
    "workloads": {
        "A": {"name": "patient-portal", "role": "patient-portal", "description": "web front end; calls the auth service and the records API"},
        "B": {"name": "auth-service", "role": "auth-service", "description": "login and token verification; counts failed logins per source IP"},
        "C": {"name": "records-api", "role": "records-api", "description": "token-protected patient records; reads from the database"},
        "D": {"name": "database", "role": "database", "description": "synthetic patient records store"},
    },
}


class WorkloadConfigError(ValueError):
    """The configuration is invalid. `.errors` lists EVERY problem found, not only the first."""

    def __init__(self, errors: List[str], source: str = ""):
        self.errors, self.source = list(errors), source
        super().__init__(f"invalid workload configuration{f' ({source})' if source else ''}:\n  - " + "\n  - ".join(self.errors))


@dataclass(frozen=True)
class Workload:
    node: str
    name: str
    role: str
    description: str = ""


@dataclass(frozen=True)
class Workloads:
    client: str
    workloads: Tuple[Workload, ...]
    source: str = "defaults"

    def by_node(self) -> Dict[str, Workload]:
        return {w.node: w for w in self.workloads}

    def name(self, node: str) -> str:
        return self.by_node()[node].name

    def role(self, node: str) -> str:
        return self.by_node()[node].role

    def names(self) -> List[str]:
        return [w.name for w in self.workloads]

    def node_of_role(self, role: str) -> str:
        return next(w.node for w in self.workloads if w.role == role)

    def name_of_role(self, role: str) -> str:
        return next(w.name for w in self.workloads if w.role == role)

    def node_of_name(self, name: str) -> Optional[str]:
        return next((w.node for w in self.workloads if w.name == name), None)

    def policy_name(self, node: str) -> str:
        return f"resilience-isolate-{self.name(node)}"

    def as_dict(self) -> dict:
        return {"client": self.client, "workloads": {w.node: {"name": w.name, "role": w.role, "description": w.description}
                                                     for w in self.workloads}}


def validate(raw, source: str = "") -> Workloads:
    """Parse and validate. Raises WorkloadConfigError listing every problem."""
    errs: List[str] = []
    if not isinstance(raw, dict):
        raise WorkloadConfigError(["the file must contain a JSON object"], source)
    for k in raw:
        if k not in ALLOWED_TOP:
            errs.append(f"unknown top-level key {k!r} (allowed: {', '.join(sorted(ALLOWED_TOP))}; namespaces are not configurable)")
    client = raw.get("client", "client")

    def check_name(label: str, v) -> bool:
        if not isinstance(v, str):
            errs.append(f"{label} must be a string")
            return False
        ok = True
        if not DNS_LABEL.match(v):
            errs.append(f"{label} {v!r} is not a valid Kubernetes service name (lower-case letters, digits and '-', starting with a letter)")
            ok = False
        if len(v) > NAME_MAX:
            errs.append(f"{label} {v!r} is longer than {NAME_MAX} characters")
            ok = False
        return ok
    client_ok = check_name("client", client)
    if client_ok and (client in RESERVED_NAMES or client.startswith(RESERVED_PREFIXES)):
        errs.append(f"client name {client!r} is reserved by the platform")
    wl = raw.get("workloads")
    out: List[Workload] = []
    if not isinstance(wl, dict):
        errs.append("'workloads' must be an object with exactly the nodes A, B, C and D")
    else:
        missing = [n for n in NODES if n not in wl]
        extra = [n for n in wl if n not in NODES]
        if missing:
            errs.append(f"workloads missing for node(s) {', '.join(missing)}: there must be exactly one workload per resilience node A-D")
        if extra:
            errs.append(f"unknown node(s) {', '.join(map(str, extra))}: the platform has exactly four peer agents, A-D")
        for n in NODES:
            w = wl.get(n)
            if w is None:
                continue
            if not isinstance(w, dict):
                errs.append(f"workload {n} must be an object")
                continue
            for k in w:
                if k not in ALLOWED_WORKLOAD:
                    errs.append(f"workload {n}: unknown key {k!r} (allowed: {', '.join(sorted(ALLOWED_WORKLOAD))})")
            name, role, desc = w.get("name"), w.get("role"), w.get("description", "")
            if name is None:
                errs.append(f"workload {n}: 'name' is required")
            elif check_name(f"workload {n} name", name):
                if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES):
                    errs.append(f"workload {n}: name {name!r} is reserved by the platform")
                if name == client:
                    errs.append(f"workload {n}: name {name!r} collides with the client probe's name")
            if role is None:
                errs.append(f"workload {n}: 'role' is required")
            elif role not in KNOWN_ROLES:
                errs.append(f"workload {n}: unknown role {role!r} (the app image implements: {', '.join(KNOWN_ROLES)})")
            if not isinstance(desc, str) or len(desc) > 200:
                errs.append(f"workload {n}: 'description' must be a string of at most 200 characters")
            if isinstance(name, str) and isinstance(role, str):
                out.append(Workload(n, name, role, desc if isinstance(desc, str) else ""))
        for label, vals in (("name", [x.name for x in out]), ("role", [x.role for x in out])):
            for v in sorted({v for v in vals if vals.count(v) > 1}):
                who = ", ".join(x.node for x in out if getattr(x, label) == v)
                errs.append(f"conflict: {label} {v!r} is used by more than one workload (nodes {who}); each {label} must be unique")
        if not any(e.startswith("unknown role") for e in errs) and not missing:
            absent = [r for r in KNOWN_ROLES if r not in [x.role for x in out]]
            if absent and len(out) == 4 and not any("conflict: role" in e for e in errs):
                errs.append(f"roles not used: {', '.join(absent)}")
    if errs:
        raise WorkloadConfigError(errs, source)
    return Workloads(client=client, workloads=tuple(sorted(out, key=lambda w: NODES.index(w.node))), source=source or "defaults")


def default_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config", "workloads.json")


def load(path: Optional[str] = None) -> Workloads:
    """Load the configuration. Order: explicit `path`, then $WORKLOADS_CONFIG, then the repository's config/workloads.json,
    and only when NONE was asked for explicitly and the repository file does not exist (for example inside the agent image, which does
    not need it) the built-in defaults. A file that was asked for but is missing or invalid is an error."""
    explicit = path or os.environ.get(ENV_VAR)
    p = explicit or default_path()
    if not os.path.isfile(p):
        if explicit:
            raise WorkloadConfigError([f"file not found: {p}"], p)
        return validate(DEFAULTS, "built-in defaults")
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise WorkloadConfigError([f"cannot read {p}: {exc.__class__.__name__}: {exc}"], p)
    return validate(raw, p)
