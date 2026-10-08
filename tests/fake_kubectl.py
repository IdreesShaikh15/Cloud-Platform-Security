#!/usr/bin/env python3
"""A tiny pretend `kubectl` used ONLY to test scripts/verify-isolation.sh's logic.

It keeps NetworkPolicies in a JSON file ($FAKE_STATE) and answers the handful of
commands the script issues. When a policy exists it answers the in-pod HTTP/DNS
probes according to what Calico is *supposed* to do for that stage, so the test can
check the script reports PASS when the world behaves and FAIL when it does not.

This proves nothing about real Calico - it only tests the script (argument checks,
never deleting a policy it did not create, cleanup, exit codes).

Failure injection (environment variables):
  FAKE_PREEXIST=1        a policy with the same name already exists (not ours)
  FAKE_NOBLOCK=1         isolation "does nothing": client -> target stays reachable
  FAKE_AGENT_BLOCKED=1   isolation wrongly blocks the agent -> target path
  FAKE_NOEGRESS=1        isolation wrongly leaves egress open
  FAKE_DELETE_FAIL=1     `delete networkpolicy` always fails
  FAKE_REPLACE_ON_CREATE=1  after create, someone else swaps in a policy with another run-id
  FAKE_NOTREADY=1        pods report not Ready while a policy exists
  FAKE_SLOW=1            probes sleep 30 s while a policy exists (to test Ctrl-C)
  FAKE_PHASE=ISOLATED    deployment annotation resilience.io/phase
"""
import json
import os
import sys
import time

STATE = os.environ["FAKE_STATE"]
NS = os.environ.get("FAKE_NS", "healthcare")
CLIENT = os.environ.get("FAKE_CLIENT", "client-1")
AGENT = os.environ.get("FAKE_AGENT", "agent-c-1")
TARGET = os.environ.get("FAKE_TARGET", "records-api")
TARGET_POD = f"{TARGET}-abc"
PEER = os.environ.get("FAKE_PEER", "auth-service")


def load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"policies": {}, "log": []}


def save(s):
    with open(STATE, "w") as fh:
        json.dump(s, fh)


def out(text="", code=0):
    sys.stdout.write(text)
    sys.stdout.flush()
    sys.exit(code)


def err(text, code=1):
    sys.stderr.write(text + "\n")
    sys.exit(code)


def main(argv):
    args = []
    it = iter(argv)
    for a in it:
        if a == "--context":
            next(it)
        elif a.startswith("--request-timeout"):
            continue
        else:
            args.append(a)
    st = load()
    st["log"].append(" ".join(args))
    save(st)
    pname = f"resilience-isolate-{TARGET}"
    if args[:2] == ["version", "--client"]:
        out("Client Version: fake\n")
    if args[:2] == ["config", "current-context"]:
        out("fake-context\n")
    ns = None
    if args and args[0] == "-n":
        ns, args = args[1], args[2:]
    if args[:2] == ["get", "namespace"]:
        sys.exit(0 if args[2] == NS else 1)
    if args[:2] == ["get", "pod"]:
        known = {CLIENT, AGENT, TARGET_POD}
        out("Running" if args[2] in known else "", 0 if args[2] in known else 1)
    if args[:2] == ["get", "deployment"]:
        if args[2] != TARGET:
            err(f'Error from server (NotFound): deployments.apps "{args[2]}" not found')
        if any("annotations" in a for a in args):
            out(os.environ.get("FAKE_PHASE", ""))
        out("")
    if args[:2] == ["get", "service"]:
        if args[2] not in (TARGET, PEER):
            err(f'Error from server (NotFound): services "{args[2]}" not found')
        out("10.96.0.7" if any("clusterIP" in a for a in args) else "8080")
    if args[:2] == ["get", "pods"]:
        if any("range" in a for a in args):
            ready = "false" if (os.environ.get("FAKE_NOTREADY") and st["policies"]) else "true"
            out(f"{TARGET_POD}={ready} ")
        out(TARGET_POD)
    if args[:2] == ["get", "networkpolicy"]:
        if len(args) >= 3 and args[2] == "-o":       # list
            out("".join(f"networkpolicy.networking.k8s.io/{n}\n" for n in st["policies"]))
        name = args[2]
        pol = st["policies"].get(name)
        if os.environ.get("FAKE_PREEXIST") and pol is None and name == pname:
            pol = {"metadata": {"labels": {}}}
        if pol is None:
            err(f'Error from server (NotFound): networkpolicies.networking.k8s.io "{name}" not found')
        out(pol["metadata"].get("labels", {}).get("resilience.io/verify-run", ""))
    if args[:1] == ["create"]:
        pol = json.loads(sys.stdin.read())
        name = pol["metadata"]["name"]
        if name in st["policies"] or (os.environ.get("FAKE_PREEXIST") and name == pname):
            err(f'Error from server (AlreadyExists): networkpolicies "{name}" already exists')
        st["policies"][name] = pol
        if os.environ.get("FAKE_REPLACE_ON_CREATE"):
            pol["metadata"]["labels"]["resilience.io/verify-run"] = "someone-else"
        save(st)
        out(f"networkpolicy/{name} created\n")
    if args[:2] == ["delete", "networkpolicy"]:
        name = args[2]
        if os.environ.get("FAKE_DELETE_FAIL"):
            err("Error from server (Forbidden): cannot delete")
        if name not in st["policies"]:
            err(f'Error from server (NotFound): networkpolicies "{name}" not found')
        del st["policies"][name]
        save(st)
        out(f'networkpolicy "{name}" deleted\n')
    if args[:1] == ["exec"]:
        pod = args[1]
        i = args.index("sh", args.index("-c"))
        script, rest = args[args.index("-c") + 1], args[i + 1:]
        is_dns = 'name="$2"' in script
        dest = rest[1]
        pol = st["policies"].get(pname)
        if pol is not None and os.environ.get("FAKE_SLOW"):
            time.sleep(30)
        stage = pol["metadata"]["annotations"]["resilience.io/stage"] if pol else None
        blocked = False
        if stage:
            to_target = TARGET in dest and not is_dns
            if pod == CLIENT and to_target:
                blocked = stage in ("QUARANTINE", "RESTRICTED") and not os.environ.get("FAKE_NOBLOCK")
            elif pod == AGENT and to_target:
                blocked = bool(os.environ.get("FAKE_AGENT_BLOCKED"))
            elif pod == TARGET_POD:
                blocked = stage == "QUARANTINE" and not os.environ.get("FAKE_NOEGRESS")
        out("PROBE=blocked reason=Timeout\n" if blocked else
            ("PROBE=reachable resolved\n" if is_dns else "PROBE=reachable code=200\n"))
    err(f"fake kubectl: unsupported command {args}", 99)


if __name__ == "__main__":
    main(sys.argv[1:])
