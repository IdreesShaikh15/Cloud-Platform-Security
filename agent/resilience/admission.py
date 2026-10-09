"""Admission policy: a single compromised agent can no longer act alone.

The same pure function is used by
  * the real validating admission webhook (webhook.py, called by the Kubernetes API server), and
  * the simulator's fake cluster (response.FakeBackend), so the whole detect -> isolate ->
    recover -> validate -> reintegrate pipeline can be tested under enforcement.

WHO IS HELD TO THE RULE
  Only the requests made by the agents' own identity (config `certificates.webhook_enforced_users`,
  default the `resilience-agent` service account) are checked. Everyone else (cluster admins,
  Kubernetes' own controllers) passes through to ordinary RBAC. The threat is a compromised AGENT.

WHAT AN AGENT MAY DO IN THE healthcare NAMESPACE, AND ONLY WITH A VALID CERTIFICATE
  NetworkPolicy  resilience-isolate-<workload>
      CREATE  at stage QUARANTINE         needs a CONTAIN certificate for EXACTLY the next incident
                                          (workload's current incident version + 1, read from the cluster)
      UPDATE  to stage S                  needs ADVANCE_STAGE(S) (or CONTAIN to re-apply QUARANTINE)
      DELETE  only after an UPDATE to stage FULL carrying an ADVANCE_STAGE(FULL) certificate
      The policy body must be EXACTLY the platform's own policy for that stage, so a valid
      certificate cannot be used to install a weaker policy. Stages cannot go backwards within
      an incident and a new incident must start at QUARANTINE (replay / superseded checks).
  Deployment of one of the 4 workloads
      UPDATE only; the only changes allowed are the recovery fields (image -> the known-good image,
      resilience.io/* annotations, the recovery timestamp); every other change is refused. Needs a
      certificate matching the state being written (CONTAIN / VALIDATE / ADVANCE_STAGE; a recovery RETRY
      needs its own RETRY_RECOVERY certificate for that attempt number), and the
      state may never move backwards within an incident (so an old certificate cannot rewind it).
      CREATE and DELETE are refused outright.
  Anything else an enforced agent tries in this namespace is refused.

FAIL-SAFE
  Any internal error while reviewing is a DENY. The webhook configuration uses failurePolicy: Fail,
  so if the webhook is down the API server rejects the agents' changes instead of silently
  allowing them (see docs/SECURITY.md for the trade-off).
"""
from __future__ import annotations

import copy
import json
import logging
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .certificate import ANNOTATION, Certificate, CertificateError, Expect, verify_certificate
from .crypto import KeyRegistry
from .reintegration import POLICY_NAME_FMT, STAGES, network_policy

log = logging.getLogger(__name__)

DEFAULT_ENFORCED = ("system:serviceaccount:resilience:resilience-agent",)
PREFIX = "resilience.io/"
_VOLATILE_META = ("resourceVersion", "generation", "managedFields", "uid", "creationTimestamp",
                  "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds", "ownerReferences")


def _anns(obj: Optional[dict]) -> dict:
    return dict(((obj or {}).get("metadata") or {}).get("annotations") or {})


def _norm_spec(spec: dict) -> dict:
    """A NetworkPolicy spec with the API server's omitted-empty-list defaults filled in."""
    d = json.loads(json.dumps(spec or {}))
    d.setdefault("ingress", [])
    d.setdefault("egress", [])
    return d


class AdmissionPolicy:
    def __init__(self, workloads: Iterable[str], registry: KeyRegistry, *, healthcare_ns: str = "healthcare",
                 resilience_ns: str = "resilience", known_good_image: str = "cr-healthcare-app:known-good",
                 quorum: int = 3, enforced_users: Iterable[str] = DEFAULT_ENFORCED,
                 revoked: Iterable[str] = (), max_ttl_s: float = 900.0,
                 clock: Callable[[], float] = time.time,
                 epoch_source: Optional[Callable[[str], Optional[int]]] = None):
        self.workloads = set(workloads)
        self.registry = registry
        self.hc, self.res = healthcare_ns, resilience_ns
        self.image = known_good_image
        self.quorum = quorum
        self.enforced = set(enforced_users)
        self.revoked = set(revoked)
        self.max_ttl_s = max_ttl_s
        self.clock = clock
        # workload -> its current incident version, read from the cluster (None = cannot tell).
        # Used to stop a still-valid old certificate from being replayed to create a NEW isolation.
        self.epoch_source = epoch_source

    @classmethod
    def from_config(cls, cfg, registry: KeyRegistry, clock: Callable[[], float] = time.time,
                    epoch_source=None) -> "AdmissionPolicy":
        cp = cfg.certificates
        return cls([s.workload for s in cfg.nodes.values()], registry,
                   healthcare_ns=cfg.healthcare_namespace, resilience_ns=cfg.resilience_namespace,
                   known_good_image=cfg.known_good_image, quorum=cfg.quorum.quorum,
                   enforced_users=cp.webhook_enforced_users, revoked=cp.revoked_signers,
                   max_ttl_s=cp.max_ttl_s, clock=clock, epoch_source=epoch_source)

    # ---- entry point -------------------------------------------------------------
    def review(self, req: dict) -> Tuple[bool, str]:
        """(allowed, reason). Never raises: any error is a denial."""
        try:
            return self._review(req)
        except Exception as exc:                      # fail safe
            log.exception("admission review failed")
            return False, f"internal error while reviewing, denied for safety: {exc}"

    def _review(self, req: dict) -> Tuple[bool, str]:
        user = ((req.get("userInfo") or {}).get("username")) or ""
        ns = req.get("namespace") or ""
        if ns != self.hc or user not in self.enforced:
            return True, "not an enforced principal / namespace"
        kind = ((req.get("kind") or {}).get("kind")) or req.get("kind_name") or ""
        if kind == "NetworkPolicy":
            return self._networkpolicy(req)
        if kind == "Deployment":
            return self._deployment(req)
        return False, f"agents may not change {kind or 'this resource'} in {self.hc}"

    # ---- helpers -------------------------------------------------------------------
    def _verify(self, anns: dict, expect, current_epoch: Optional[int]) -> Tuple[bool, str, Optional[Certificate]]:
        raw = anns.get(ANNOTATION)
        if not raw:
            return False, "no quorum certificate attached (annotation resilience.io/qc)", None
        try:
            cert = Certificate.from_b64(raw)
        except CertificateError as exc:
            return False, str(exc), None
        ok, why = verify_certificate(cert, self.registry, self.clock(), quorum=self.quorum, expect=expect,
                                     revoked=self.revoked, current_epoch=current_epoch,
                                     max_ttl_s=self.max_ttl_s)
        return ok, ("certificate ok" if ok else f"invalid quorum certificate: {why}"), cert

    @staticmethod
    def _int(value, default=None):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ---- NetworkPolicy -------------------------------------------------------------
    def _workload_of_policy(self, name: str) -> Optional[str]:
        for w in self.workloads:
            if name == POLICY_NAME_FMT.format(workload=w):
                return w
        return None

    def _networkpolicy(self, req: dict) -> Tuple[bool, str]:
        op = req.get("operation")
        name = req.get("name") or ((req.get("object") or {}).get("metadata") or {}).get("name") or ""
        w = self._workload_of_policy(name)
        if w is None:
            return False, (f"agents may only manage the platform's isolation policies "
                           f"(resilience-isolate-<workload>), not {name!r}")
        old, new = req.get("oldObject"), req.get("object")
        if op == "DELETE":
            oa = _anns(old)
            if oa.get(PREFIX + "stage") != "FULL":
                return False, "a policy may only be deleted after it was updated to stage FULL with a certificate"
            epoch = self._int(oa.get(PREFIX + "epoch"))
            ok, why, _ = self._verify(oa, Expect(w, "ADVANCE_STAGE", "FULL", epoch), None)
            return ok, why
        if op not in ("CREATE", "UPDATE"):
            return False, f"operation {op} is not allowed for agents"
        na = _anns(new)
        stage = na.get(PREFIX + "stage")
        if stage not in STAGES:
            return False, f"unknown stage {stage!r}"
        epoch = self._int(na.get(PREFIX + "epoch"))
        if epoch is None:
            return False, "policy has no resilience.io/epoch annotation"
        old_epoch = self._int(_anns(old).get(PREFIX + "epoch")) if (op == "UPDATE" and old) else None
        old_stage = _anns(old).get(PREFIX + "stage") if (op == "UPDATE" and old) else None
        if op == "CREATE" and stage != "QUARANTINE":
            return False, "a new isolation policy must start at QUARANTINE"
        if op == "CREATE":
            if self.epoch_source is None:
                return False, "cannot determine the workload's incident version, denied for safety"
            cur = self.epoch_source(w)
            if cur is None:
                return False, "cannot read the workload's incident version, denied for safety"
            if epoch != cur + 1:
                return False, (f"replay or stale: a new isolation must be for incident version {cur + 1}, "
                               f"the certificate is for {epoch}")
        if old_epoch is not None:
            if epoch < old_epoch:
                return False, f"superseded: incident version {epoch} is older than the current {old_epoch}"
            if epoch > old_epoch and stage != "QUARANTINE":
                return False, "a new incident must start at QUARANTINE"
            if epoch == old_epoch and old_stage in STAGES and STAGES.index(stage) < STAGES.index(old_stage):
                return False, f"stage may not go backwards ({old_stage} -> {stage})"
        expect = Expect(w, "CONTAIN", "", epoch) if stage == "QUARANTINE" else Expect(w, "ADVANCE_STAGE", stage, epoch)
        ok, why, _ = self._verify(na, expect, old_epoch)
        if not ok:
            return False, why
        want_stage = "PEER_VALIDATED" if stage == "FULL" else stage       # FULL = pending removal
        want = _norm_spec(network_policy(w, want_stage, self.hc, self.res)["spec"])
        if _norm_spec((new or {}).get("spec")) != want:
            return False, f"policy body is not the platform's own {want_stage} policy"
        return True, "certificate ok; policy is the platform's own"

    # ---- Deployment -----------------------------------------------------------------
    @staticmethod
    def _rank(anns: dict) -> int:
        """Where in an incident the recorded state is: ISOLATED=0, validated=1, then each stage."""
        phase, stage = anns.get(PREFIX + "phase"), anns.get(PREFIX + "stage")
        if phase == "ISOLATED":
            return 0
        if phase == "REINTEGRATING" and stage == "QUARANTINE":
            return 1
        if phase in ("REINTEGRATING", "HEALTHY") and stage in STAGES[1:]:
            return 1 + STAGES.index(stage)
        return -1

    def _strip(self, d: dict) -> dict:
        d = copy.deepcopy(d or {})
        meta = d.get("metadata") or {}
        for k in _VOLATILE_META:
            meta.pop(k, None)
        meta["annotations"] = {k: v for k, v in (meta.get("annotations") or {}).items()
                               if not k.startswith(PREFIX) and not k.startswith("deployment.kubernetes.io/")}
        d["metadata"] = meta
        d.pop("status", None)
        tmpl = (d.get("spec") or {}).get("template") or {}
        tm = tmpl.get("metadata") or {}
        tm["annotations"] = {k: v for k, v in (tm.get("annotations") or {}).items() if not k.startswith(PREFIX)}
        tmpl["metadata"] = tm
        for c in (tmpl.get("spec") or {}).get("containers") or []:
            if c.get("name") == "app":
                c.pop("image", None)
        return d

    @staticmethod
    def _app_image(d: dict) -> Optional[str]:
        for c in ((d.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []:
            if c.get("name") == "app":
                return c.get("image")
        return None

    def _deployment(self, req: dict) -> Tuple[bool, str]:
        op = req.get("operation")
        name = req.get("name") or ((req.get("object") or {}).get("metadata") or {}).get("name") or ""
        if op != "UPDATE":
            return False, f"agents may not {str(op).lower()} workloads"
        if name not in self.workloads:
            return False, f"agents may not change deployment {name!r}"
        old, new = req.get("oldObject") or {}, req.get("object") or {}
        if self._strip(old) != self._strip(new):
            return False, "agents may only change the recovery fields (image, resilience.io/* annotations)"
        new_img, old_img = self._app_image(new), self._app_image(old)
        image_changed = new_img != old_img
        if image_changed and new_img != self.image:
            return False, f"image may only be set to the known-good image {self.image!r}, not {new_img!r}"
        oa, na = _anns(old), _anns(new)
        tmpl_changed = ((((old.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations")
                        != (((new.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations"))
        recovered = (na.get(PREFIX + "recovered-epoch") != oa.get(PREFIX + "recovered-epoch")
                     or image_changed or tmpl_changed
                     or na.get(PREFIX + "recovery-requested-at") != oa.get(PREFIX + "recovery-requested-at"))
        epoch = self._int(na.get(PREFIX + "epoch"), 0)
        old_epoch = self._int(oa.get(PREFIX + "epoch"), 0)
        if epoch < old_epoch:
            return False, f"superseded: incident version {epoch} is older than the current {old_epoch}"
        if recovered:
            rec = self._int(na.get(PREFIX + "recovered-epoch"))
            if rec is None:
                return False, "a recovery must carry resilience.io/recovered-epoch"
            attempt = self._int(na.get(PREFIX + "recovery-attempt"), 1)
            old_attempt = self._int(oa.get(PREFIX + "recovery-attempt"), 0)
            old_rec = self._int(oa.get(PREFIX + "recovered-epoch"), -1)
            if rec == old_rec and attempt < old_attempt:
                return False, f"recovery attempt may not go backwards ({old_attempt} -> {attempt})"
            # attempt 1 is authorised by the CONTAIN certificate; every RETRY needs its own quorum decision
            expect: List[Expect] = ([Expect(name, "CONTAIN", "", rec)] if attempt <= 1
                                    else [Expect(name, "RETRY_RECOVERY", str(attempt), rec)])
            current = old_rec if old_rec >= 0 else None
        else:
            phase, stage = na.get(PREFIX + "phase"), na.get(PREFIX + "stage")
            if phase == "ISOLATED":
                expect = [Expect(name, "CONTAIN", "", epoch)]
            elif phase == "REINTEGRATING" and stage == "QUARANTINE":
                expect = [Expect(name, "VALIDATE", "", epoch)]
            elif phase in ("REINTEGRATING", "HEALTHY") and stage in STAGES[1:]:
                expect = [Expect(name, "ADVANCE_STAGE", stage, epoch)]
            else:
                return False, f"unrecognised state change (phase={phase!r}, stage={stage!r})"
            current = old_epoch
            if epoch == old_epoch and self._rank(na) < self._rank(oa):
                return False, "state may not go backwards within an incident"
        ok, why, _ = self._verify(na, expect, current)
        return ok, why if not ok else "certificate ok; only recovery fields changed"


# --------------------------------------------------------------------------- AdmissionReview glue
def review_to_response(policy: AdmissionPolicy, review: dict) -> dict:
    """Wrap policy.review() in an AdmissionReview v1 response (used by the webhook server)."""
    req = (review or {}).get("request") or {}
    uid = req.get("uid", "")
    allowed, reason = policy.review(req)
    resp: dict = {"uid": uid, "allowed": bool(allowed)}
    if not allowed:
        resp["status"] = {"code": 403, "reason": "Forbidden", "message": f"cr-quorum-webhook: {reason}"}
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": resp}
