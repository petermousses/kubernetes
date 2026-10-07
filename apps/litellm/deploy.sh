#!/usr/bin/env bash
set -euo pipefail

readonly app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly namespace=litellm
readonly migration_job=litellm-migrations-v1-104-0
readonly migration_poll_seconds="${LITELLM_MIGRATION_POLL_SECONDS:-5}"
readonly migration_timeout_seconds="${LITELLM_MIGRATION_TIMEOUT_SECONDS:-1800}"

for value_name in migration_poll_seconds migration_timeout_seconds; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s must be a positive integer, got %q\n' "${value_name}" "${value}" >&2
    exit 1
  fi
done

print_migration_diagnostics() {
  printf 'migration Job diagnostics:\n' >&2
  kubectl -n "${namespace}" get "job/${migration_job}" -o wide || true
  kubectl -n "${namespace}" get pods \
    -l "job-name=${migration_job}" -o wide || true
  kubectl -n "${namespace}" logs \
    -l "job-name=${migration_job}" \
    --all-containers --prefix --timestamps --tail=-1 || true
  kubectl -n "${namespace}" describe "job/${migration_job}" || true
}

wait_for_migration() {
  local conditions
  local deadline=$((SECONDS + migration_timeout_seconds))
  local kubectl_error

  kubectl_error="$(mktemp "${TMPDIR:-/tmp}/litellm-migration-kubectl.XXXXXX")"
  while (( SECONDS < deadline )); do
    if ! conditions="$(
      kubectl -n "${namespace}" get "job/${migration_job}" \
        -o 'jsonpath={range .status.conditions[*]}{.type}={.status}{"\n"}{end}' \
        2>"${kubectl_error}"
    )"; then
      printf 'could not read migration Job status:\n' >&2
      cat "${kubectl_error}" >&2
      rm -f -- "${kubectl_error}"
      return 1
    fi

    case "${conditions}" in
      *Failed=True*)
        printf 'migration Job failed\n' >&2
        rm -f -- "${kubectl_error}"
        return 1
        ;;
      *Complete=True*)
        rm -f -- "${kubectl_error}"
        return 0
        ;;
    esac

    sleep "${migration_poll_seconds}"
  done

  printf 'migration Job did not finish within %s seconds\n' \
    "${migration_timeout_seconds}" >&2
  rm -f -- "${kubectl_error}"
  return 1
}

if ! command -v kubectl >/dev/null; then
  printf 'kubectl is required\n' >&2
  exit 1
fi
if ! kubectl -n "${namespace}" get secret litellm-env >/dev/null 2>&1; then
  printf 'run %s/bootstrap-secrets.sh first\n' "${app_root}" >&2
  exit 1
fi
if ! kubectl -n "${namespace}" get secret litellm-admin-oidc >/dev/null 2>&1; then
  printf 'run %s/../authentik/bootstrap-litellm-admin-oidc-secrets.sh first\n' \
    "${app_root}" >&2
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
if ! wait_for_migration; then
  print_migration_diagnostics
  exit 1
fi

# The public router changed from a Kubernetes Ingress to a Traefik
# IngressRoute. kubectl apply does not prune resources whose kind changed.
kubectl -n "${namespace}" delete ingress litellm --ignore-not-found
kubectl apply -k "${app_root}"
kubectl -n "${namespace}" rollout status deployment/litellm --timeout=10m
kubectl -n "${namespace}" get pods,svc,ingressroute,certificate,endpointslice
