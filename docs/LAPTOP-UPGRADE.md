# Laptop upgrade: from commit 8d53075 (4 workloads) to the latest (6 workloads)

Your starting point: WSL, project at `/mnt/c/Users/IDREES SHAIKH/resilience-platform`, venv `.venv`, Minikube profile `cr-platform`, running the deploy
from commit `8d53075` (4 workloads, admission webhook installed). Everything below was **rehearsed on a real sandbox cluster** (docs/SANDBOX-CLUSTER.md)
starting from exactly that state, from a project path containing a space and with Windows line endings in the scripts. The "expect" lines are what that
rehearsal printed. What I could **not** test: WSL itself, Docker Desktop, your Minikube/Kubernetes version (the sandbox ran Kubernetes 1.34.12), and
`scripts/minikube-load.sh` output (the sandbox had no Metrics API). Times are from the sandbox; your laptop will differ.

Always run the scripts with `bash scripts/...` (not `./scripts/...`): it works even if the Windows drive hides the executable bit.

## A. Update the project and fix line endings
```bash
cd "/mnt/c/Users/IDREES SHAIKH/resilience-platform"
source .venv/bin/activate
git status --short                      # expect nothing important (see note 1 if it lists files)
git fetch origin
git checkout claude/cyber-resilience-platform-xxs3oh
git pull origin claude/cyber-resilience-platform-xxs3oh
git log --oneline -1                    # expect the newest commit, "Phase J: final regression counts ..." or later
sed -i 's/\r$//' scripts/*.sh           # = dos2unix, nothing to install (or: sudo apt-get install -y dos2unix && dos2unix scripts/*.sh)
chmod +x scripts/*.sh
bash scripts/verify-rbac.sh --help      # expect a usage line. If you see "pipefail^M: invalid option name", the line endings are still wrong
```
Note 1: if `git pull` refuses because of local changes (usually the line-ending conversion from an earlier run), run `git checkout -- scripts`
and then the pull, then the `sed` line again. Your generated files (`k8s/generated/`, certificates) are ignored by git and stay as they are: do **not** run `gen-certs.py` again.

## B. Pre-checks (nothing changes)
```bash
kubectl config current-context          # expect: cr-platform
minikube status -p cr-platform          # expect host / kubelet / apiserver Running on every node
kubectl -n healthcare get pods          # expect 5 pods: your 4 workloads + client
python3 scripts/gen-workloads.py --check
                                        # expect: configuration OK (.../config/workloads.json); all 3 generated files are up to date
```

## C. Rebuild the images
```bash
bash scripts/build-images.sh
```
Expect three docker builds, then `>> loading cr-healthcare-app:1.0 into all nodes of cr-platform` (and `:known-good`, `cr-agent:1.0`, `cr-dashboard:1.0`),
then `>> generating known-good file hash manifest` and **`4 files hashed`**: `app.py`, `static/TRIPWIRE-TEST.txt`, `static/index.html`, `telemetry.py`
(your old build hashed 3 files). About 1.5-3 minutes. If Docker Hub answers `429 Too Many Requests`, wait a few minutes and run it again.

## D. Upgrade the cluster (this is the deploy; do NOT run scripts/deploy.sh afterwards)
```bash
bash scripts/upgrade-app.sh
```
Expect, in order: `>> 1/7 regenerating the manifest ...`, `2/7 stopping the deciders ... agent-a agent-b agent-c agent-d`,
`3/7 applying RBAC + configuration + the manifest, and rolling the workloads onto the new image`, `4/7 checking the running workloads against the manifest`
followed by **six** lines `ok  <workload>: 4 files match the known-good manifest` (patient-portal, auth-service, records-api, database, **lab-results, medication-service**),
`5/7 restarting the admission webhook ...`, `6/7 starting them again`, `7/7 restarting the in-cluster dashboard`, `>> done.`; exit code 0; about 1-2 minutes
(sandbox: 67 s). The agents are OFF during this time on purpose: the dashboard will show them down for a minute.
* If step 4 prints `mismatch` the script stops and keeps the agents OFF (safe): run `bash scripts/build-images.sh` again, then this script again.
* `refused: kubectl context is ...` -> `kubectl config use-context cr-platform`.
* Why no separate `deploy.sh`: this script applies everything that is new (RBAC, configuration, the two new workloads, the new images) and restarts the webhook **before** the agents return. Without that restart the old webhook refuses to isolate the new workloads (a bug found in the rehearsal and fixed in this commit). Running `deploy.sh` on top still works but needlessly rolls every workload and agent again.

## E. Check the result
```bash
kubectl -n healthcare get pods -o wide   # expect 7 Running: 6 workloads + client; lab-results on a node labelled zone b, medication-service on zone c
kubectl -n resilience get pods           # expect agent-a..d, dashboard, two quorum-webhook pods, all Running (old pods may show Terminating for a moment)
python3 scripts/verify-baseline.py       # expect six lines "ok ... 4 files match the known-good manifest"
```

## F. The verify scripts (run these when no incident is in progress)
```bash
for t in patient-portal auth-service records-api database lab-results medication-service; do
  bash scripts/verify-rbac.sh --namespace healthcare --target $t | tail -1
done                                      # expect six lines: === summary: 27 passed, 0 failed ===

for t in lab-results medication-service; do
  bash scripts/verify-webhook.sh --namespace healthcare --target $t | tail -3
done                                      # expect: === summary: 7 passed, 0 failed === (the "delete" line says "refused by RBAC": that is a PASS)
bash scripts/verify-webhook.sh --namespace healthcare --target lab-results --test-failsafe | tail -4
                                          # expect: ... === summary: 8 passed, 0 failed ===  (it scales the webhook to 0 for a few seconds and restores it)
kubectl -n resilience rollout status deploy/quorum-webhook --timeout=120s

CP=$(kubectl -n healthcare get pod -l app=client -o jsonpath='{.items[0].metadata.name}')
AP=$(kubectl -n resilience get pod -l app=resilience-agent -o name | head -1 | cut -d/ -f2)
bash scripts/verify-isolation.sh --namespace healthcare --client-pod "$CP" --target lab-results --agent-pod "$AP" --egress-peer auth-service | tail -3
                                          # expect: === summary: 16 passed, 0 failed, 0 skipped ===  (about 1 minute; it briefly cuts lab-results off and then removes its own policy)
```

## G. Resource use
```bash
minikube addons enable metrics-server -p cr-platform
kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s
sleep 60
bash scripts/minikube-load.sh
```
Expect: `kubectl top` tables (not verified by me), then the requests/limits table: 7 pods, each `50m / 64Mi` requested and `500m / 256Mi` limited, and the per-node allocation.
For orientation, the sandbox idle readings were about 20-26 m CPU and 18 MiB per workload, about 24 m and 80 MiB per agent.

## H. Start the two-page dashboard (second terminal), then run the tripwire test
Terminal 2:
```bash
cd "/mnt/c/Users/IDREES SHAIKH/resilience-platform" && source .venv/bin/activate
bash scripts/laptop-dashboard.sh
```
Open `http://127.0.0.1:8091/?mode=live`. Expect: Healthy workloads **6 of 6**, Agents connected **4 of 4**, six workload cards (E Laboratory Results Service and F Medication Service say
"Watched by all four agents (no agent of its own)"), no demo controls on this page. `http://127.0.0.1:8091/` is Demo Mode (simulator). Ctrl-C stops everything.

Terminal 1, the controlled tripwire (one harmless line appended to one marker file in `lab-results`, nothing else touched):
```bash
bash scripts/tripwire-test.sh E                  # dry run: prints exactly what it would do
kubectl -n resilience logs -f deploy/agent-a | grep -E "QUORUM|EXECUTOR"      # optional, in a third terminal
bash scripts/tripwire-test.sh E --yes
```
Expect (rehearsal, real timers): within about 2 s `QUORUM CONTAIN on E (epoch 1) by A,B,C,D`, `EXECUTOR A: isolate lab-results`, `redeploy lab-results from known-good image`
(this line may repeat about once a second for a few seconds: harmless); about 35 s later the new pod (the old one takes its 30 s shutdown grace) and `QUORUM VALIDATE`;
then `ADVANCE_STAGE` to RESTRICTED, MONITORED, PEER_VALIDATED and FULL about 10 s apart (a stage voted "by A,C,D" is normal: 3 of 4 is enough);
**total about 80 s** from the tripwire to normal access. On the Live page: a Security Alert "E Laboratory Results Service #1", then `RESOLVED, Healthy, Normal Access Restored`.
Afterwards `python3 scripts/verify-baseline.py` shows six `ok` again (the tripwire line disappears with the old pod). Do the same with `F` for the Medication Service if you like.

## FALLBACK: only the original four workloads, everything else kept
Use this if six workloads will not run on your laptop (for example a Pending pod from lack of memory). It keeps the 3-of-4 quorum, the staged return, the admission webhook, the
laptop dashboard, the guided walkthrough and the baselines; only the Laboratory Results and Medication services are left out. Rehearsed on the sandbox cluster, **on the latest code, starting
from the six-workload deployment** (the harder case; if you never deployed the two extra services, the removal lines below simply do not appear).
```bash
cd "/mnt/c/Users/IDREES SHAIKH/resilience-platform" && source .venv/bin/activate
# do sections A (pull, line endings) and C (build-images) first, exactly as above, then:
git checkout -b fallback-4-workloads                 # a LOCAL branch, so the main branch stays clean
cp config/workloads-four.json config/workloads.json  # the exact four-workload profile of the earlier phases
python3 scripts/gen-workloads.py --write             # expect: wrote k8s/base/10-healthcare-apps.yaml, k8s/resilience/10-config.yaml, k8s/resilience/00-rbac.yaml
python3 scripts/gen-workloads.py --check             # expect: configuration OK (...); all 3 generated files are up to date
git add -A && git commit -q -m "local: four-workload fallback"
bash scripts/upgrade-app.sh
```
Expect from `upgrade-app.sh`: `>> removing lab-results: it is deployed but not in config/workloads.json` (and the same for `medication-service`) **only if they were deployed**, each followed by
`... deleted from healthcare namespace`; then **four** `ok` lines in step 4/7 (patient-portal, auth-service, records-api, database); `>> done.`; exit code 0.
Then check it the same way as sections E, F, H, with these differences:
```bash
for t in patient-portal auth-service records-api database; do bash scripts/verify-rbac.sh --namespace healthcare --target $t | tail -1; done   # four lines: 27 passed, 0 failed
bash scripts/verify-webhook.sh --namespace healthcare --target records-api | tail -1                                                           # 7 passed, 0 failed
bash scripts/tripwire-test.sh C --yes      # C = records-api. Expect CONTAIN within about 2 s and normal access after about 80 s (rehearsal: 11:48:05 -> FULL 11:49:22)
bash scripts/tripwire-test.sh E            # expect a refusal: 'E' is not one of the healthcare workloads (patient-portal auth-service records-api database)
```
The dashboard shows four workload cards and "Agents connected 4 of 4"; Demo Mode runs the same four-workload profile (the three six-workload-only scenarios are not offered).
To return to six workloads later: `git checkout claude/cyber-resilience-platform-xxs3oh && bash scripts/upgrade-app.sh` (not rehearsed; it is the same upgrade as section D).
Note: interrupting `upgrade-app.sh` (for example a lost connection) leaves the agents OFF, which is the safe state; just run the script again.

## Things that can differ on your laptop
Kubernetes version and Minikube driver; Docker Desktop memory (6 workloads fit easily in the 2200 MiB per node `scripts/setup-cluster.sh` uses); WSL networking (the dashboard
binds 127.0.0.1; Windows normally forwards WSL's localhost); corporate proxies. If anything here prints something different, send me the output.
