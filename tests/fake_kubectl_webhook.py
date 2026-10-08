#!/usr/bin/env python3
"""Pretend kubectl for scripts/verify-webhook.sh. It answers the few commands the script uses and,
for server-side dry runs, calls the REAL AdmissionPolicy, exactly as the API server would call the
webhook. Failure injection via environment variables:
  FAKE_ALLOW_ALL=1   the webhook is not enforcing (everything is allowed)
  FAKE_RBAC_DENY=1   RBAC refuses the request before the webhook is ever consulted
  FAKE_FAILOPEN=1    with the webhook scaled to 0, requests are still allowed (failurePolicy Ignore)
  FAKE_NO_HOOK=1     the ValidatingWebhookConfiguration is not installed
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(os.path.dirname(HERE), "agent")]
from resilience.admission import AdmissionPolicy          # noqa: E402
from resilience.crypto import KeyRegistry, Signer          # noqa: E402

STATE = os.environ["FAKE_STATE"]
WORKLOADS = ["patient-portal", "auth-service", "records-api", "database"]


def state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"replicas": 2, "log": []}


def save(s):
    json.dump(s, open(STATE, "w"))


def die(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def policy():
    reg = KeyRegistry({n: Signer.generate(n).public_key() for n in "ABCD"})
    return AdmissionPolicy(WORKLOADS, reg, epoch_source=lambda w: 0)


def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a.get(k), v)
        return out
    if isinstance(a, list) and isinstance(b, list) and a and isinstance(a[0], dict) and "name" in a[0]:
        by = {x["name"]: x for x in a}
        for x in b:
            by[x["name"]] = merge(by.get(x["name"], {}), x)
        return list(by.values())
    return b


def main(argv):
    user, args = "kubernetes-admin", []
    it = iter(argv)
    for a in it:
        if a.startswith("--as="):
            user = a[5:]
        elif a == "--context":
            next(it)
        elif a.startswith("--request-timeout"):
            continue
        else:
            args.append(a)
    st = state()
    st["log"].append(" ".join(args))
    save(st)
    ns = None
    if args[:1] == ["-n"]:
        ns, args = args[1], args[2:]
    if args[:2] == ["get", "namespace"]:
        sys.exit(0)
    if args[:2] == ["get", "deployment"]:
        sys.exit(0)
    if args[:2] == ["get", "validatingwebhookconfiguration"]:
        sys.exit(1 if os.environ.get("FAKE_NO_HOOK") else 0)
    if args[:2] == ["get", "deploy/quorum-webhook"]:
        print(st["replicas"], end="")
        sys.exit(0)
    if args[:1] == ["scale"]:
        st["replicas"] = int([a for a in args if a.startswith("--replicas=")][0].split("=")[1])
        save(st)
        sys.exit(0)

    # ---- a server-side dry run: authenticate -> RBAC -> admission webhook
    if os.environ.get("FAKE_RBAC_DENY") and user != "kubernetes-admin":
        die(f'Error from server (Forbidden): User "{user}" cannot do this in the namespace "healthcare"')
    if st["replicas"] == 0 and user != "kubernetes-admin" and os.environ.get("FAKE_FAILOPEN"):
        print("networkpolicy/x created (server dry run)")      # failurePolicy Ignore: webhook down => allowed
        sys.exit(0)
    if st["replicas"] == 0 and user != "kubernetes-admin":
        die('Error from server (InternalError): Internal error occurred: failed calling webhook '
            '"quorum.resilience.io": no endpoints available for service "quorum-webhook"')
    if st["replicas"] == 0 and user == "kubernetes-admin":
        die('Error from server (InternalError): failed calling webhook "quorum.resilience.io"')
    req = {"uid": "x", "namespace": ns or "healthcare", "userInfo": {"username": user}}
    if args[:1] == ["create"]:
        obj = json.loads(sys.stdin.read())
        req.update(operation="CREATE", kind={"kind": obj["kind"]}, name=obj["metadata"]["name"], object=obj)
        what = "created"
    elif args[:2] == ["patch", "deployment"]:
        name = args[2]
        patch = json.loads(args[args.index("-p") + 1])
        old = {"metadata": {"name": name, "annotations": {}},
               "spec": {"replicas": 1, "template": {"metadata": {"annotations": {}},
                                                    "spec": {"containers": [{"name": "app", "image": "cr-healthcare-app:1.0"}]}}}}
        req.update(operation="UPDATE", kind={"kind": "Deployment"}, name=name, oldObject=old, object=merge(old, patch))
        what = "patched"
    elif args[:2] == ["delete", "deployment"]:
        req.update(operation="DELETE", kind={"kind": "Deployment"}, name=args[2], oldObject={"metadata": {"name": args[2]}})
        what = "deleted"
    else:
        die(f"fake kubectl: unsupported {args}", 99)
    if os.environ.get("FAKE_ALLOW_ALL"):
        ok, why = True, "not enforcing"
    else:
        ok, why = policy().review(req)
    if not ok:
        die(f'Error from server (Forbidden): admission webhook "quorum.resilience.io" denied the request: '
            f'cr-quorum-webhook: {why}')
    print(f"{req['kind']['kind'].lower()}/{req['name']} {what} (server dry run)")


if __name__ == "__main__":
    main(sys.argv[1:])
