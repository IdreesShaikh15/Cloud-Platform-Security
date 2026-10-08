# Security hardening (Phase 3)

*What was added, how it works in plain English, what it protects against, and, just as important,
what it does NOT protect against.*

The goal of this phase: **a single compromised resilience agent can no longer act alone**, and what
the agents decided can be checked afterwards.

---

## 1. The threat model, in one paragraph

The platform has four identical agents. Each agent has (a) a private signing key, (b) a TLS identity,
and (c) a Kubernetes service-account token that lets it change network rules and redeploy the four
workloads. Before this phase the "3 of 4 must agree" rule lived **only inside each agent's own code**.
A fully compromised agent (an attacker running arbitrary code in it) could ignore that rule and use its
Kubernetes token to isolate a healthy workload by itself. Now Kubernetes **itself** refuses such a
change unless it carries proof that three agents agreed.

> **Assumptions, stated honestly**
> * **One compromised agent is tolerated** (f = 1 of n = 4): a lone agent has one signature; a valid
>   certificate needs three.
> * **Two compromised agents can break it.** Two signatures plus one honest-but-deceived signature is a
>   valid certificate. That is the Byzantine-fault-tolerance bound (n >= 3f + 1), not a bug.
>   `tests/test_certificate.py::test_one_compromised_agent_cannot_make_a_certificate_but_two_can` shows both sides.
> * A certificate proves the agents **agreed**; it does not prove they were **right**.
> * Keys are only as safe as the Kubernetes Secrets that hold them.
> * People with cluster-admin rights are outside the rule (see section 4).

---

## 2. The quorum certificate

### 2.1 What a valid certificate is (exactly)

A **statement** plus signatures over that exact statement:

```
statement = { version, workload, target, action, stage, epoch, evidence_digest, expires_ms }
```

| Field | Meaning |
|---|---|
| `workload` / `target` | the Kubernetes workload (e.g. `records-api`) and its node id (`C`) |
| `action` + `stage` | what was agreed: `CONTAIN`, `VALIDATE`, or `ADVANCE_STAGE` into the named stage |
| `epoch` | the incident version of that workload |
| `evidence_digest` | SHA-256 of the sorted, de-duplicated evidence ids cited by the three votes |
| `expires_ms` | after this instant the certificate is void |

It is valid only if **all** of the following hold (`agent/resilience/certificate.py: verify_certificate`):

| # | Rule | Rejected case | Test |
|---|---|---|---|
| 1 | at least **3 signatures** | fewer than 3 | `test_rejects_too_few_signatures` |
| 2 | from **3 distinct** peers | the same signer listed twice (even "3 signatures" from 2 agents) | `test_rejects_duplicate_signers` |
| 3 | every signer is a **known** peer | a valid key that is not in the registry | `test_rejects_unknown_peers` |
| 4 | every signature is valid over the **same canonical bytes** | tampered / bad / different-statement signature | `test_rejects_invalid_signatures`, `test_signatures_are_domain_separated` |
| 5 | not **expired** (and not implausibly long-lived) | expired; expiry far in the future | `test_rejects_expired_certificates`, `test_rejects_implausibly_long_lived_certificates` |
| 6 | matches the **target, action, stage** the caller expects | a certificate for another workload / action / stage | `test_rejects_certificates_for_a_different_target_action_or_stage` |
| 7 | not for a **superseded incident version** | an older epoch | `test_rejects_certificates_for_a_superseded_incident_version` |
| 8 | not **replayed** | already used (caller keeps a consumed set) | `test_rejects_replayed_certificates` |
| 9 | signers are not **revoked** | a human-revoked agent's signature does not count | `test_revoked_signers_do_not_count` |

Verification is **strict**: a certificate that contains a duplicate, an unknown signer or a bad signature is rejected as a
whole, not "repaired" by ignoring the bad part. The statement is canonical JSON (sorted keys, no spaces) and any other
encoding of the same data is rejected, so every agent and the webhook check byte-identical data. Signatures use their own domain
(`qc-v1`), so an evidence or vote signature can never be replayed as a certificate share.

### 2.2 How three agents produce one (no leader)

1. Votes are cast and counted exactly as before. When an agent holds a quorum of eligible votes it builds the statement
   **deterministically** from the three lowest-id voters' votes (so every agent that holds those votes computes the identical statement).
2. It signs the statement and broadcasts a `CertShare` (new `SubmitShare` RPC over the existing gRPC/mTLS channel).
3. An agent signs a statement it receives **only if** it voted for the same proposal, holds the three named votes, finds every
   voter eligible, and **recomputes the identical statement** itself. A statement it cannot reproduce is refused and logged.
4. Anyone holding three distinct valid shares holds the certificate. It adds about one tick (0.3-0.5 s) to TTI in the simulator.

### 2.3 Where it is checked: two independent layers

| Layer | What it does | If this layer is compromised |
|---|---|---|
| **Executor (inside each agent)** | will not call Kubernetes without a verified certificate for exactly that action; waits briefly for shares; refuses on an invalid one | the second layer still stops the change |
| **Admission webhook (Kubernetes side)** | the API server asks it about every change the agents make; it checks the certificate itself | needs the attacker to also control the webhook / its config (agents' RBAC cannot touch either) |

---

## 3. The admission webhook

`agent/resilience/admission.py` (the rules, one pure function), `webhook.py` (HTTPS server),
`k8s/resilience/40-webhook.yaml` (2 replicas, same image as the agents, no new image). Certificates for it are made by
`scripts/gen-certs.py`, which also writes `k8s/generated/webhook-config.json` (the `ValidatingWebhookConfiguration`).

### 3.1 What an agent may do, and only with a valid certificate

| Resource (healthcare namespace) | Allowed for the agents' identity | Everything else |
|---|---|---|
| NetworkPolicy `resilience-isolate-<workload>` | **create** at QUARANTINE (CONTAIN certificate, for *exactly the next incident version*); **update** to a stage (ADVANCE_STAGE certificate; never backwards; a new incident starts at QUARANTINE); **delete** only after a certified update to stage FULL | any other policy name -> refused |
| the policy body | must be **exactly the platform's own policy for that stage**: a valid certificate cannot be used to install a weaker policy | refused |
| Deployment of one of the 4 workloads | **update** only the recovery fields (image -> the known-good image, `resilience.io/*` annotations), with the certificate matching the state written; state can never move backwards within an incident | any other field (replicas, env, volumes...), CREATE, DELETE, the `client` deployment -> refused |
| any other resource | | refused |

Why FULL is two calls: a DELETE request cannot carry an annotation, so the executor first **updates** the policy to stage FULL
(certificate attached) and then deletes it; the webhook accepts the DELETE only for a policy carrying a valid FULL certificate.

### 3.2 Who is held to the rule

Only the agents' own service account (`certificates.webhook_enforced_users`). Cluster admins and Kubernetes' own controllers pass
through to ordinary RBAC. **This is a deliberate scope choice:** the threat is a compromised *agent*. It means a human with
cluster-admin (e.g. `kubectl` as `minikube`) can still act alone; that is outside this threat model and would need separate controls.

### 3.3 Fail-safe behaviour (what happens when the webhook is unavailable)

* The configuration uses **`failurePolicy: Fail`**: if the API server cannot reach the webhook it **rejects** the agents' change; it does **not**
  silently allow it. Inside the webhook, any error while reviewing is a **DENY**.
* **The price of failing closed:** while the webhook is down, the agents cannot isolate or recover anything (and nobody, including admins,
  can change NetworkPolicies/Deployments in the healthcare namespace). That is the intended safe side. The webhook runs with 2 replicas to make
  this rare.
* **Break-glass** (a human decision, logged by Kubernetes): `kubectl delete validatingwebhookconfiguration cr-quorum-webhook`. Re-apply with
  `kubectl apply -f k8s/generated/webhook-config.json`. `scripts/reset.sh` and ordinary `kubectl` use by an admin work while the webhook is up.
* Proven by `tests/test_admission.py::test_internal_errors_deny_for_safety`, `test_https_webhook_end_to_end_with_platform_ca` (garbage in -> deny) and,
  on your cluster, `scripts/verify-webhook.sh --test-failsafe` (scales the webhook to 0, checks the agent is still refused, restores it).

### 3.4 Replay: how it is limited, and what remains

* A certificate cannot be used for a different workload, action, stage or an older incident (rules 6-7).
* **A still-valid CONTAIN certificate cannot re-isolate a workload after its incident ended:** CREATE must be exactly `current incident version + 1`.
  The webhook reads that version from the Deployment (its only Kubernetes access: read-only `get` of the 4 workloads; if the read fails the CREATE is denied).
  Found while writing the tests: without this check a lone compromised agent holding an unexpired certificate could have re-isolated a healthy workload
  (`test_old_certificate_cannot_create_a_new_isolation`).
* The recorded state can never move backwards (`test_old_certificate_cannot_rewind_the_recorded_state`).
* **Remaining:** within a certificate's lifetime (`ttl_s`, 5 minutes) the *same* decision can be re-applied (that is what makes retries safe). The effect
  is limited to re-asserting what the quorum already decided.
* The webhook keeps no consumed-certificate memory (it is stateless on purpose: two replicas, restarts), so "already used" replay detection is available
  in `verify_certificate(consumed=...)` for a stateful caller but is **not** what protects the webhook; the epoch/stage ordering is.

### 3.5 Revoking an agent

If humans conclude an agent is compromised, add its id to `certificates.revoked_signers` in the ConfigMap and restart the agents and webhook:
its signatures stop counting everywhere. (Manual by design: revoking automatically would let an attacker revoke honest agents.)

---

## 4. Least-privilege RBAC (`k8s/resilience/00-rbac.yaml`)

| | Before | After |
|---|---|---|
| NetworkPolicies | get, list, create, update, patch, delete **on all** | create; get/update/delete **only** the four `resilience-isolate-<workload>` |
| Deployments | get, list, patch **on all** (incl. `client`) | get, patch **only** the 4 workloads |
| Pods | get, list (unused) | **removed** |
| ConfigMaps (resilience ns) | get **all** | get **only** `cr-attack-marker` |
| Identities | one account shared by agents and the baseline controller | `resilience-agent`, `resilience-baseline`, `quorum-webhook` (read-only on 4 deployments) |

Tests: `tests/test_rbac.py` derives every Kubernetes call the platform makes from the real `K8sBackend` code and checks the manifest allows each
one (so tightening cannot silently break the platform), and checks 40 forbidden operations are refused. On a live cluster: `scripts/verify-rbac.sh`.
**Limit:** `create` of a NetworkPolicy cannot be restricted by name in Kubernetes RBAC; the webhook is what restricts *what* may be created.

---

## 5. Vote-excluded agents

Once an agent's trust falls below the exclusion threshold (40):

* **Its new evidence is rejected entirely**, not down-weighted: it never enters any score (`on_envelope`, test
  `test_excluded_agents_evidence_is_rejected_entirely`). Its votes count toward no quorum, it cannot be a named voter in a certificate,
  and its investigation answers are ignored.
* Its claims are still **watched** (kept in a separate pool that is never scored) so peers keep judging whether it is still lying.
* **Trust is regained only through what peers observe over time:** +0.5 points per second while its claims are not contradicted by the others' own
  measurements. Nothing it sends can raise its own trust (`test_nothing_B_sends_can_raise_its_own_trust`), and ten quiet seconds do not make it eligible again.
* **A restart does not wipe an exclusion.** Each agent saves its view of its peers' trust (`/var/log/resilience/trust-state.json`, an `emptyDir` that survives a
  container restart) and reloads it. A restarted *excluded* agent cannot reset anything: peers hold their own ledgers.
* **Remaining gaps:** if a pod is *deleted* (not just restarted) its `emptyDir` is lost and that agent starts from "everyone trusted" until it re-observes.
  The state file is ordinary local storage: someone who can write it inside the pod can change it. Keeping it in a ConfigMap/CRD would need extra RBAC.

---

## 6. Tamper-evident decision log

Every quorum decision an agent records stores the hash of the previous record: `hash_i = SHA-256(record_i including prev_hash)`.

* Each agent appends to `/var/log/resilience/decisions.jsonl` (and keeps the records in memory).
* **Verify:** `kubectl -n resilience exec deploy/agent-a -- python -m resilience verify-log` prints `decision log OK: N record(s)` or
  `decision log BROKEN at record #k: <reason>` (exit code 1). `k` is the **first** broken record.
* **Dashboard:** a header chip, green "decision logs intact (4 agents)" or red "BROKEN: A" (hover for the first broken record). Screenshots: [`img/dashboard-decision-log-chip.png`](img/dashboard-decision-log-chip.png), login page [`img/dashboard-token-login.png`](img/dashboard-token-login.png).
* Tested: **modified** record, **deleted** record, **re-ordered** records, **inserted** record, unreadable record, tampering with the file on disk, and a broken log
  stays flagged after reload and further appends (`tests/test_decision_log.py`).

**What a hash chain cannot do (also tested, so it is not overclaimed):**
* It **detects** tampering; it cannot **prevent** it.
* Someone with **full write access can rewrite the entire chain** consistently and the result verifies
  (`test_whole_chain_rewrite_is_not_detected`). Defending against that needs the head hash stored where the attacker cannot write
  (peers, a write-once store, external logging).
* Deleting records from the **end** is not detectable from the log alone (`test_truncating_the_tail_is_not_detected_by_the_chain_alone`).
* Each agent keeps its own log; comparing the four is a useful extra check but is not automated.

---

## 7. Dashboard access token

Optional and **off by default**, so local demos stay open.

* Enable: `kubectl -n resilience create secret generic dashboard-token --from-literal=token="$(openssl rand -hex 24)"` then
  `kubectl -n resilience rollout restart deploy/dashboard`. Locally: set `DASHBOARD_TOKEN` (or `DASHBOARD_TOKEN_FILE`).
* The token comes from the environment or a mounted file only; it is **never hardcoded, never logged, never sent back**
  (`test_the_token_is_never_hardcoded_or_logged`). If a token *file* is configured but unreadable or empty the dashboard **refuses to start** rather than
  silently opening up.
* **The API is protected, not only the page:** every `/api/*` endpoint (state, events, metrics, export, agent view) returns 401 without it; only `/healthz`
  (no data) and the login page are public. Browsers sign in once (random, expiring, HttpOnly, SameSite=Strict session cookie that is not the token);
  scripts send `Authorization: Bearer <token>` (`scripts/collect-metrics.py` reads `DASHBOARD_TOKEN`). Five wrong attempts per minute per address are rate-limited.
* **Limits:** one shared token (no per-user accounts); plain HTTP unless you add TLS in front (set `DASHBOARD_COOKIE_SECURE=1` once you do); the agents'
  own `/status` ports (8081) are still unauthenticated and reachable by pods in the cluster. Do not pass the token on the command line (visible in `ps`).

---

## 8. What is NOT verified, and remaining gaps

* **Not run on a real cluster.** Everything above is proven by tests and the simulator, where the fake cluster runs the *same* admission code as the webhook.
  Run `scripts/verify-webhook.sh` and `scripts/verify-rbac.sh` on your cluster; their logic is tested against fake `kubectl`s, which proves the scripts, not your cluster.
* The real `ValidatingWebhookConfiguration` has not been applied to a real API server by me. Possible live surprises: the API server's exact object shape
  (the policy comparison normalises omitted empty lists, but a real server may add other defaults), webhook TLS/CA wiring, and timing (5 s timeout).
  `verify-webhook.sh` is designed to expose these safely (dry runs only).
* **Cluster admins are not constrained** (section 3.2). A compromised *node* or *API server* is out of scope.
* A certificate proves agreement, not correctness: three agents that are all fooled by the same bad evidence still produce a valid certificate.
* **2 of 4 compromised agents break the guarantee** (and so would 2 compromised signing keys).
* Signing keys and the CA key sit in ordinary Secrets / on the host that ran `gen-certs.py`; no rotation or revocation of TLS identities.
* `CREATE` of a NetworkPolicy cannot be limited by name in RBAC (the webhook covers it).
* The agents' status endpoints (:8081) and the dashboard (without a token) remain readable by any pod.
* While the webhook is down, the agents cannot act (fail-closed); a prolonged webhook outage therefore stalls incident response.
* A certificate adds one share round (measured: isolation took about 0.5 s longer in the simulator).
* Validation after a stalled recovery re-uses the original CONTAIN certificate; if it has expired (5 minutes) the action is refused and a human must intervene. Phase 4 handles retries and "needs human attention".

## 9. Commands

```bash
# verify, on the live cluster (all read-only or dry-run):
scripts/verify-rbac.sh    --namespace healthcare --target records-api
scripts/verify-webhook.sh --namespace healthcare --target records-api            # add --test-failsafe to also test fail-closed
kubectl -n resilience exec deploy/agent-a -- python -m resilience verify-log
# tests for all of the above (no cluster needed):
python3 -m pytest -q tests/test_certificate.py tests/test_admission.py tests/test_rbac.py tests/test_decision_log.py \
        tests/test_trust_hardening.py tests/test_dashboard_auth.py tests/test_enforcement.py tests/test_verify_webhook.py
```
