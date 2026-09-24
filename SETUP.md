# SETUP — prerequisites (do this once before `demo.md`)

Tested target: Linux or macOS host (Windows works via WSL2), **4+ CPU cores, 12+ GB RAM, 20 GB free disk**.
The cluster is 4 Minikube nodes × 2 CPU × 2.2 GB. With fewer resources, lower `CPUS`/`MEMORY`
when running `setup-cluster.sh` (minimum: 1 CPU / 1800 MB per node).

## 1. Install the tools

| Tool | Version | Check | Install |
|------|---------|-------|---------|
| Docker Engine / Docker Desktop | 24+ | `docker ps` | https://docs.docker.com/engine/install/ |
| Minikube | 1.33+ | `minikube version` | https://minikube.sigs.k8s.io/docs/start/ |
| kubectl | 1.29+ | `kubectl version --client` | https://kubernetes.io/docs/tasks/tools/ |
| Python | 3.10+ | `python3 --version` | your OS package manager |
| openssl (optional, for inspecting certs) | any | `openssl version` | usually pre-installed |

Linux quick install for Minikube and kubectl:

```bash
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install kubectl /usr/local/bin/kubectl
```

Linux only: let your user run Docker without sudo (`sudo usermod -aG docker $USER`, then log out and back in).
Minikube's docker driver refuses to run as root. If you must run as root, use
`MINIKUBE_EXTRA_ARGS=--force` with `setup-cluster.sh`.

## 2. Python environment (host-side scripts, tests, simulator)

```bash
cd Cloud-Platform-Security
python3 -m venv .venv
source .venv/bin/activate          # Windows WSL: same; PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

Every later `python3 ...` command in `demo.md` assumes this venv is active.

## 3. Sanity check without Kubernetes (≈ 2 minutes)

```bash
python3 -m pytest -q tests            # expect: 35 passed
python3 sim/local_demo.py             # expect: 4 × PASS
```

If both pass, the core logic (signatures, mTLS, trust, quorum, the pipeline) works on your machine.

## 4. Create the cluster, build and load images

```bash
scripts/setup-cluster.sh     # 4-node Minikube + Calico, nodes labelled resilience.io/zone=a..d  (5-10 min first time)
scripts/build-images.sh      # docker build ×3, `minikube image load` onto all nodes, known-good hash manifest
python3 scripts/gen-certs.py # CA + mTLS certs + Ed25519 keys -> certs/ and k8s/generated/pki.json
```

Useful variables: `PROFILE` (default `cr-platform`), `DRIVER` (default `docker`), `CPUS`, `MEMORY`.

Make sure kubectl points at the new cluster: `kubectl config current-context` should print `cr-platform`.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `429 Too Many Requests` from Docker Hub during build | `docker login`, or pull the base image via the mirror: `docker pull mirror.gcr.io/library/python:3.11-slim && docker tag mirror.gcr.io/library/python:3.11-slim python:3.11-slim` |
| Pods stuck in `ErrImageNeverPull` / `ImagePullBackOff` | re-run `scripts/build-images.sh` (images must be loaded into **every** node) |
| Pods `Pending` with "didn't match node selector" | nodes not labelled: re-run the labelling loop at the end of `setup-cluster.sh` |
| Agents log `peer link to X DOWN` right after deploy | normal for a few seconds while peers start; if it persists, check `kubectl -n resilience get endpoints` and that port 50051 is listening in the agent pod |
| Isolation "doesn't block traffic" | Calico isn't enforcing: `kubectl -n kube-system get pods -l k8s-app=calico-node` must all be Running |
| `pip install` fails inside `docker build` behind a corporate proxy | `docker build --build-arg HTTPS_PROXY=$HTTPS_PROXY --network host ...` |
| Changed `apps/src/*` | re-run `scripts/build-images.sh` (it regenerates the hash manifest), then `kubectl apply -f k8s/generated/known-good-hashes.json` and restart the agents |
