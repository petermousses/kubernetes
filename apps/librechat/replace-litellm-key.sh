#!/usr/bin/env bash
set -euo pipefail
umask 0077

for binary in base64 kubectl mktemp tr; do
  if ! command -v "${binary}" >/dev/null; then
    printf '%s is required\n' "${binary}" >&2
    exit 1
  fi
done

kubectl -n librechat get secret librechat-env >/dev/null

litellm_api_key=''
encoded_key=''
patch_file=''
cleanup() {
  unset litellm_api_key encoded_key
  if [[ -n "${patch_file}" && -e "${patch_file}" ]]; then
    rm -f -- "${patch_file}"
  fi
}
trap cleanup EXIT

if ! IFS= read -rs -p 'restricted LiteLLM virtual key: ' litellm_api_key; then
  printf '\nno LiteLLM virtual key received\n' >&2
  exit 1
fi
printf '\n' >&2
if [[ -z "${litellm_api_key}" ]]; then
  printf 'the LiteLLM virtual key cannot be empty\n' >&2
  exit 1
fi
if [[ ! "${litellm_api_key}" =~ ^sk-[A-Za-z0-9_-]{8,}$ ]]; then
  printf 'the LiteLLM virtual key has an unexpected format\n' >&2
  exit 1
fi

encoded_key="$(printf '%s' "${litellm_api_key}" | base64 | tr -d '\r\n')"
patch_file="$(mktemp "${TMPDIR:-/tmp}/librechat-key-patch.XXXXXXXX")"
chmod 0600 "${patch_file}"
printf '{"data":{"LITELLM_API_KEY":"%s"}}\n' "${encoded_key}" \
  >"${patch_file}"
unset litellm_api_key encoded_key

kubectl -n librechat patch secret librechat-env \
  --type=merge --patch-file "${patch_file}"
rm -f -- "${patch_file}"
patch_file=''
kubectl -n librechat rollout restart deployment/librechat
kubectl -n librechat rollout status deployment/librechat --timeout=180s

printf 'replaced only LITELLM_API_KEY; persistent LibreChat encryption secrets were preserved\n'
