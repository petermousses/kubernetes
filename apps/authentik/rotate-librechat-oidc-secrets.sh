#!/usr/bin/env bash
set -euo pipefail

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
blueprint_path=/blueprints/mounted/cm-authentik-librechat-oidc-blueprint/librechat.yaml

for binary in kubectl openssl; do
  if ! command -v "${binary}" >/dev/null; then
    printf '%s is required\n' "${binary}" >&2
    exit 1
  fi
done

for namespace_secret in authentik/authentik-librechat-oidc librechat/librechat-oidc; do
  namespace="${namespace_secret%%/*}"
  secret="${namespace_secret#*/}"
  if ! kubectl -n "${namespace}" get secret "${secret}" >/dev/null 2>&1; then
    printf '%s is missing; rotation requires both existing Secrets\n' "${namespace_secret}" >&2
    exit 1
  fi
done

deployed="$(kubectl -n librechat get deployment librechat --ignore-not-found -o name)"
if [[ -n "${deployed}" ]]; then
  printf 'LibreChat is deployed; rotating its session secret requires a planned cutover\n' >&2
  exit 1
fi

client_id="lc-$(openssl rand -hex 12)"
client_secret="$(openssl rand -hex 48)"
session_secret="$(openssl rand -hex 32)"

if ! {
  printf 'LIBRECHAT_OIDC_CLIENT_ID=%s\n' "${client_id}"
  printf 'LIBRECHAT_OIDC_CLIENT_SECRET=%s\n' "${client_secret}"
} | kubectl -n authentik create secret generic authentik-librechat-oidc \
      --from-env-file=/dev/stdin --dry-run=client -o yaml |
    kubectl -n authentik apply --server-side --force-conflicts -f - >/dev/null; then
  printf 'Authentik Secret update failed; stop before deploying the OIDC provider\n' >&2
  exit 1
fi

if ! {
  printf 'OPENID_CLIENT_ID=%s\n' "${client_id}"
  printf 'OPENID_CLIENT_SECRET=%s\n' "${client_secret}"
  printf 'OPENID_SESSION_SECRET=%s\n' "${session_secret}"
} | kubectl -n librechat create secret generic librechat-oidc \
      --from-env-file=/dev/stdin --dry-run=client -o yaml |
    kubectl -n librechat apply --server-side --force-conflicts -f - >/dev/null; then
  printf 'partial rotation: Authentik was updated but LibreChat was not; stop and rerun this script before deployment\n' >&2
  exit 1
fi

unset client_id client_secret session_secret

if ! kubectl apply -f "${app_root}/librechat-oidc-blueprint.yaml" >/dev/null; then
  printf 'Secrets rotated, but the corrected blueprint ConfigMap was not applied; stop and repair before using OIDC\n' >&2
  exit 1
fi

kubectl -n authentik rollout restart deployment/authentik-worker >/dev/null
kubectl -n authentik rollout status deployment/authentik-worker --timeout=5m

if ! kubectl -n authentik exec deployment/authentik-worker -c worker -- \
     ak apply_blueprint --dry-run "${blueprint_path}" >/dev/null 2>&1; then
  printf 'blueprint validation failed; output is suppressed because Authentik may log the client secret\n' >&2
  exit 1
fi
if ! kubectl -n authentik exec deployment/authentik-worker -c worker -- \
     ak apply_blueprint "${blueprint_path}" >/dev/null 2>&1; then
  printf 'blueprint application failed; output is suppressed because Authentik may log the client secret\n' >&2
  exit 1
fi

printf 'rotated matching OIDC Secrets and applied the corrected blueprint; values were not printed\n'
