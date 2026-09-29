#!/usr/bin/env bash
set -euo pipefail

readonly app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly namespace=litellm
readonly migration_job=litellm-migrations-v1-103-0

if ! command -v kubectl >/dev/null; then
  printf 'kubectl is required\n' >&2
  exit 1
fi
if ! kubectl -n "${namespace}" get secret litellm-env >/dev/null 2>&1; then
  printf 'run %s/bootstrap-secrets.sh first\n' "${app_root}" >&2
  exit 1
fi

kubectl apply \
  -f "${app_root}/storage-class.yaml" \
  -f "${app_root}/persistent-volume.yaml" \
  -f "${app_root}/persistent-volume-claim.yaml" \
  -f "${app_root}/postgres.yaml" \
  -f "${app_root}/redqueen-backends.yaml" \
  -f "${app_root}/configmap.yaml" \
  -f "${app_root}/networkpolicy.yaml"
kubectl kustomize "${app_root}/../_shared/network-policy/baseline" | \
  kubectl apply -n "${namespace}" -f -
kubectl kustomize "${app_root}/../_shared/network-policy/dns-egress" | \
  kubectl apply -n "${namespace}" -f -
kubectl -n "${namespace}" rollout status statefulset/postgres --timeout=5m

if kubectl -n "${namespace}" get deployment litellm >/dev/null 2>&1; then
  kubectl -n "${namespace}" scale deployment/litellm --replicas=0
  kubectl -n "${namespace}" rollout status deployment/litellm --timeout=5m
fi

kubectl -n "${namespace}" delete job "${migration_job}" --ignore-not-found
kubectl apply -f "${app_root}/migration-job.yaml"
if ! kubectl -n "${namespace}" wait \
  --for=condition=complete "job/${migration_job}" --timeout=30m; then
  kubectl -n "${namespace}" logs "job/${migration_job}" --all-containers
  exit 1
fi

kubectl apply -k "${app_root}"
kubectl -n "${namespace}" rollout status deployment/litellm --timeout=10m
kubectl -n "${namespace}" get pods,svc,ingress,certificate,endpointslice
