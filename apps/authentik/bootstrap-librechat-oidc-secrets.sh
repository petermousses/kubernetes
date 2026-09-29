#!/usr/bin/env bash
set -euo pipefail

for binary in kubectl openssl; do
  if ! command -v "${binary}" >/dev/null; then
    printf '%s is required\n' "${binary}" >&2
    exit 1
  fi
done

for namespace in authentik librechat; do
  if ! kubectl get namespace "${namespace}" >/dev/null; then
    printf '%s namespace is missing\n' "${namespace}" >&2
    exit 1
  fi
done

if kubectl -n authentik get secret authentik-librechat-oidc >/dev/null 2>&1 ||
   kubectl -n librechat get secret librechat-oidc >/dev/null 2>&1; then
  printf 'one or both OIDC Secrets already exist; refusing to generate mismatched credentials\n' >&2
  exit 1
fi

client_id="lc-$(openssl rand -hex 12)"
client_secret="$(openssl rand -hex 48)"
session_secret="$(openssl rand -hex 32)"

{
  printf 'LIBRECHAT_OIDC_CLIENT_ID=%s\n' "${client_id}"
  printf 'LIBRECHAT_OIDC_CLIENT_SECRET=%s\n' "${client_secret}"
} | kubectl -n authentik create secret generic authentik-librechat-oidc --from-env-file=/dev/stdin

if ! {
  printf 'OPENID_CLIENT_ID=%s\n' "${client_id}"
  printf 'OPENID_CLIENT_SECRET=%s\n' "${client_secret}"
  printf 'OPENID_SESSION_SECRET=%s\n' "${session_secret}"
} | kubectl -n librechat create secret generic librechat-oidc --from-env-file=/dev/stdin; then
  printf 'partial bootstrap: authentik-librechat-oidc exists, but librechat-oidc was not created; stop and repair the matching Secrets before deployment\n' >&2
  exit 1
fi

unset client_id client_secret session_secret
printf 'created matching Authentik and LibreChat OIDC Secrets; values were not printed\n'
