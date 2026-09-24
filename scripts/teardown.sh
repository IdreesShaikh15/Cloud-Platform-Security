#!/usr/bin/env bash
set -euo pipefail
minikube delete -p "${PROFILE:-cr-platform}"
