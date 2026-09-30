#!/usr/bin/env bash
set -euo pipefail

for binary in kubectl openssl; do
  if ! command -v "${binary}" >/dev/null; then
    printf '%s is required\n' "${binary}" >&2
    exit 1
  fi
done

for namespace in authentik litellm; do
  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    printf '%s namespace is missing; deploy Authentik and LiteLLM first\n' \
      "${namespace}" >&2
    exit 1
  fi
done

if kubectl -n authentik get secret authentik-litellm-admin-oidc >/dev/null 2>&1 ||
   kubectl -n litellm get secret litellm-admin-oidc >/dev/null 2>&1; then
  printf 'one or both LiteLLM Admin UI OIDC Secrets already exist; refusing to generate mismatched credentials\n' >&2
  exit 1
fi

client_id="llm-$(openssl rand -hex 12)"
client_secret="$(openssl rand -hex 48)"

{
  printf 'LITELLM_ADMIN_OIDC_CLIENT_ID=%s\n' "${client_id}"
  printf 'LITELLM_ADMIN_OIDC_CLIENT_SECRET=%s\n' "${client_secret}"
} | kubectl -n authentik create secret generic authentik-litellm-admin-oidc \
  --from-env-file=/dev/stdin

if ! {
  printf 'GENERIC_CLIENT_ID=%s\n' "${client_id}"
  printf 'GENERIC_CLIENT_SECRET=%s\n' "${client_secret}"
} | kubectl -n litellm create secret generic litellm-admin-oidc \
  --from-env-file=/dev/stdin; then
  printf 'partial bootstrap: Authentik Secret exists, but LiteLLM Secret was not created; stop and repair the matching Secrets before deployment\n' >&2
  exit 1
fi

unset client_id client_secret
printf 'created matching Authentik and LiteLLM Admin UI OIDC Secrets; values were not printed\n'
