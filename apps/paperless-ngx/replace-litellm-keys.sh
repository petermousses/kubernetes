#!/usr/bin/env bash
set -euo pipefail
umask 0077

for binary in base64 kubectl mktemp tr; do
  if ! command -v "${binary}" >/dev/null; then
    printf '%s is required\n' "${binary}" >&2
    exit 1
  fi
done

kubectl get namespace paperless-ngx >/dev/null

paperless_ai_key=''
paperless_gpt_key=''
encoded_ai_key=''
encoded_gpt_key=''
patch_file=''
cleanup() {
  unset paperless_ai_key paperless_gpt_key encoded_ai_key encoded_gpt_key
  if [[ -n "${patch_file}" && -e "${patch_file}" ]]; then
    rm -f -- "${patch_file}"
  fi
}
trap cleanup EXIT

if ! IFS= read -rs -p 'Paperless-AI restricted LiteLLM virtual key: ' paperless_ai_key; then
  printf '\nno Paperless-AI LiteLLM key received\n' >&2
  exit 1
fi
printf '\n' >&2
if ! IFS= read -rs -p 'Paperless-GPT restricted LiteLLM virtual key: ' paperless_gpt_key; then
  printf '\nno Paperless-GPT LiteLLM key received\n' >&2
  exit 1
fi
printf '\n' >&2

for key_name in paperless_ai_key paperless_gpt_key; do
  key_value="${!key_name}"
  if [[ ! "${key_value}" =~ ^sk-[A-Za-z0-9_-]{8,}$ ]]; then
    printf '%s has an unexpected LiteLLM virtual-key format\n' "${key_name}" >&2
    unset key_value
    exit 1
  fi
  unset key_value
done
if [[ "${paperless_ai_key}" == "${paperless_gpt_key}" ]]; then
  printf 'Paperless-AI and Paperless-GPT must use separate LiteLLM virtual keys\n' >&2
  exit 1
fi

encoded_ai_key="$(printf '%s' "${paperless_ai_key}" | base64 | tr -d '\r\n')"
encoded_gpt_key="$(printf '%s' "${paperless_gpt_key}" | base64 | tr -d '\r\n')"
patch_file="$(mktemp "${TMPDIR:-/tmp}/paperless-litellm-keys.XXXXXXXX")"
chmod 0600 "${patch_file}"
printf '{"data":{"PAPERLESS_AI_LITELLM_API_KEY":"%s","PAPERLESS_GPT_LITELLM_API_KEY":"%s"}}\n' \
  "${encoded_ai_key}" "${encoded_gpt_key}" >"${patch_file}"
unset paperless_ai_key paperless_gpt_key

if kubectl -n paperless-ngx get secret paperless-litellm-env >/dev/null 2>&1; then
  kubectl -n paperless-ngx patch secret paperless-litellm-env \
    --type=merge --patch-file "${patch_file}"
else
  printf '{"apiVersion":"v1","kind":"Secret","metadata":{"name":"paperless-litellm-env","namespace":"paperless-ngx"},"type":"Opaque","data":{"PAPERLESS_AI_LITELLM_API_KEY":"%s","PAPERLESS_GPT_LITELLM_API_KEY":"%s"}}\n' \
    "${encoded_ai_key}" "${encoded_gpt_key}" >"${patch_file}"
  kubectl create --filename "${patch_file}"
fi
unset encoded_ai_key encoded_gpt_key
rm -f -- "${patch_file}"
patch_file=''
kubectl -n paperless-ngx rollout restart deployment/paperless-ai
kubectl -n paperless-ngx rollout restart deployment/paperless-gpt
kubectl -n paperless-ngx rollout status deployment/paperless-ai --timeout=180s
kubectl -n paperless-ngx rollout status deployment/paperless-gpt --timeout=180s

printf 'updated only the dedicated Paperless LiteLLM Secret; paperless-env was untouched\n'
