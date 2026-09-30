#!/usr/bin/env bash
set -euo pipefail

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
rendered="$(kubectl kustomize "${app_root}")"

# The staging deployment must not steal Open WebUI's canonical host.
rg -q 'host: librechat\.omv\.mousses\.xyz' <<<"${rendered}"
if rg -q 'host: chat\.omv\.mousses\.xyz' <<<"${rendered}"; then
  printf 'LibreChat staging must not claim chat.omv.mousses.xyz\n' >&2
  exit 1
fi

rg -q 'name: librechat-oidc' "${app_root}/deployment.yaml"
rg -q 'key: LITELLM_API_KEY' "${app_root}/deployment.yaml"
rg -q 'ip: 10\.9\.20\.14' "${app_root}/deployment.yaml"
rg -q 'auth\.omv\.mousses\.xyz' "${app_root}/deployment.yaml"
rg -q 'OPENID_ISSUER: https://auth\.omv\.mousses\.xyz/application/o/librechat/\.well-known/openid-configuration' "${app_root}/configmap.yaml"
rg -q 'OPENID_REQUIRED_ROLE: librechat_users' "${app_root}/configmap.yaml"
rg -q 'OPENID_ADMIN_ROLE: librechat_admin' "${app_root}/configmap.yaml"
rg -q 'ALLOW_REGISTRATION: "false"' "${app_root}/configmap.yaml"
rg -q 'apiKey: '\''\$\{LITELLM_API_KEY\}'\''' "${app_root}/configmap.yaml"
rg -q 'baseURL: http://litellm\.litellm\.svc\.cluster\.local:4000/v1' "${app_root}/configmap.yaml"
rg -q 'qwen3\.8-27b' "${app_root}/configmap.yaml"
rg -q 'IMAGE_GEN_OAI_MODEL: qwen-image-2\.1' "${app_root}/configmap.yaml"
if rg -q 'tower\.mousses\.xyz|10\.9\.20\.7|11434|name: Ollama' "${app_root}/configmap.yaml" "${app_root}/networkpolicy.yaml"; then
  printf 'staging still references the direct Ollama backend\n' >&2
  exit 1
fi

rg -q 'kubernetes\.io/metadata\.name: litellm' "${app_root}/networkpolicy.yaml"
rg -q 'kubernetes\.io/metadata\.name: librechat' "${app_root}/../litellm/networkpolicy.yaml"
rg -q 'cidr: 10\.9\.20\.14/32' "${app_root}/networkpolicy.yaml"

printf 'LibreChat staging contract passed\n'
