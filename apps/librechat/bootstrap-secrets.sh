#!/usr/bin/env bash
set -euo pipefail

for binary in kubectl openssl; do
  if ! command -v "${binary}" >/dev/null; then
    printf '%s is required\n' "${binary}" >&2
    exit 1
  fi
done

kubectl get namespace librechat >/dev/null
kubectl -n librechat get secret librechat-oidc >/dev/null
if kubectl -n librechat get secret librechat-env >/dev/null 2>&1; then
  printf 'librechat-env already exists; refusing to replace persistent encryption keys\n' >&2
  exit 1
fi

litellm_api_key=''
trap 'unset litellm_api_key' EXIT
if ! IFS= read -rs -p 'restricted LiteLLM virtual key: ' litellm_api_key; then
  printf '\nno LiteLLM virtual key received\n' >&2
  exit 1
fi
printf '\n' >&2
if [[ -z "${litellm_api_key}" ]]; then
  printf 'the LiteLLM virtual key cannot be empty\n' >&2
  exit 1
fi

umask 077
{
  printf 'CREDS_KEY=%s\n' "$(openssl rand -hex 32)"
  printf 'CREDS_IV=%s\n' "$(openssl rand -hex 16)"
  printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)"
  printf 'JWT_REFRESH_SECRET=%s\n' "$(openssl rand -hex 32)"
  printf 'MEILI_MASTER_KEY=%s\n' "$(openssl rand -hex 32)"
  printf 'LITELLM_API_KEY=%s\n' "${litellm_api_key}"
} | kubectl -n librechat create secret generic librechat-env --from-env-file=/dev/stdin

printf 'created librechat-env without printing credential values\n'
