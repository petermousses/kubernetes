#!/usr/bin/env bash
set -euo pipefail
umask 0077

readonly namespace=litellm
readonly secret_name=litellm-env

for command in kubectl openssl; do
  if ! command -v "${command}" >/dev/null; then
    printf 'required command not found: %s\n' "${command}" >&2
    exit 1
  fi
done

kubectl apply -f "$(dirname -- "${BASH_SOURCE[0]}")/namespace.yaml"
if kubectl -n "${namespace}" get secret "${secret_name}" >/dev/null 2>&1; then
  printf '%s/%s already exists; refusing to rotate durable credentials\n' \
    "${namespace}" "${secret_name}" >&2
  exit 1
fi

read -r -s -p 'qwen38 upstream key from redqueen: ' qwen_api_key
printf '\n'
read -r -s -p 'qwen image adapter key from redqueen: ' qwen_image_api_key
printf '\n'
read -r -s -p 'jevk5 adapter key from redqueen: ' typesafe_api_key
printf '\n'

for value in "${qwen_api_key}" "${qwen_image_api_key}" "${typesafe_api_key}"; do
  if [[ ! "${value}" =~ ^[[:xdigit:]]{64}$ ]]; then
    printf 'every redqueen upstream key must be exactly 64 hexadecimal characters\n' >&2
    exit 1
  fi
done
if [[ "${qwen_api_key}" == "${qwen_image_api_key}" \
  || "${qwen_api_key}" == "${typesafe_api_key}" \
  || "${qwen_image_api_key}" == "${typesafe_api_key}" ]]; then
  printf 'redqueen upstream keys must be distinct\n' >&2
  exit 1
fi

postgres_password="$(openssl rand -hex 32)"
litellm_master_key="sk-$(openssl rand -hex 32)"
litellm_salt_key="sk-$(openssl rand -hex 32)"
database_url="postgresql://litellm:${postgres_password}@postgres.litellm.svc.cluster.local:5432/litellm"
secret_env="$(mktemp)"
cleanup() {
  rm -f -- "${secret_env}"
}
trap cleanup EXIT

printf '%s=%s\n' \
  POSTGRES_PASSWORD "${postgres_password}" \
  DATABASE_URL "${database_url}" \
  LITELLM_MASTER_KEY "${litellm_master_key}" \
  LITELLM_SALT_KEY "${litellm_salt_key}" \
  QWEN_API_KEY "${qwen_api_key}" \
  QWEN_IMAGE_API_KEY "${qwen_image_api_key}" \
  TYPESAFE_API_KEY "${typesafe_api_key}" >"${secret_env}"
kubectl -n "${namespace}" create secret generic "${secret_name}" \
  --from-env-file="${secret_env}"

unset qwen_api_key qwen_image_api_key typesafe_api_key postgres_password
unset litellm_master_key litellm_salt_key database_url
printf 'created %s/%s; do not rotate LITELLM_SALT_KEY after credentials are stored\n' \
  "${namespace}" "${secret_name}"
