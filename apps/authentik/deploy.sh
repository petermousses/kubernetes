#!/usr/bin/env bash
set -euo pipefail

readonly app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v kubectl >/dev/null; then
  printf 'kubectl is required\n' >&2
  exit 1
fi
if ! kubectl -n authentik get secret authentik-env >/dev/null 2>&1; then
  printf 'run %s/bootstrap-secrets.sh first\n' "${app_root}" >&2
  exit 1
fi

kubectl apply -k "${app_root}"
kubectl -n authentik rollout status statefulset/postgres --timeout=5m
kubectl apply -f "${app_root}/helmchart.yaml"
kubectl -n authentik wait --for=create deployment/authentik-server --timeout=15m
kubectl -n authentik wait --for=create deployment/authentik-worker --timeout=15m
kubectl -n authentik rollout status deployment/authentik-server --timeout=15m
kubectl -n authentik rollout status deployment/authentik-worker --timeout=15m
kubectl -n authentik get pods,svc,pvc,certificate,servicemonitor

printf 'public ingress remains unpublished; enroll akadmin MFA via the NAS port-forward before applying ingress.yaml\n'
