#!/usr/bin/env bash
set -euo pipefail

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
rendered="$(kubectl kustomize "${app_root}")"

# LibreChat now owns the canonical chat hostname.
rg -q 'host: chat\.omv\.mousses\.xyz' <<<"${rendered}"
if rg -q 'host: librechat\.omv\.mousses\.xyz' <<<"${rendered}"; then
  printf 'LibreChat must not retain its former hostname\n' >&2
  exit 1
fi

rg -q 'name: librechat-oidc' "${app_root}/deployment.yaml"
rg -q 'key: LITELLM_API_KEY' "${app_root}/deployment.yaml"
rg -q 'mousses\.xyz/authentik-oidc-client: "true"' "${app_root}/deployment.yaml"
rg -q 'ip: 10\.43\.204\.62' "${app_root}/deployment.yaml"
rg -q 'auth\.omv\.mousses\.xyz' "${app_root}/deployment.yaml"
rg -q 'OPENID_ISSUER: https://auth\.omv\.mousses\.xyz/application/o/librechat/\.well-known/openid-configuration' "${app_root}/configmap.yaml"
rg -q 'OPENID_REQUIRED_ROLE: librechat_users' "${app_root}/configmap.yaml"
rg -q 'OPENID_ADMIN_ROLE: librechat_admin' "${app_root}/configmap.yaml"
rg -q 'ALLOW_REGISTRATION: "false"' "${app_root}/configmap.yaml"
rg -q 'apiKey: '\''\$\{LITELLM_API_KEY\}'\''' "${app_root}/configmap.yaml"
rg -q 'baseURL: http://litellm\.litellm\.svc\.cluster\.local:4000/v1' "${app_root}/configmap.yaml"
rg -q 'default: \[qwen3\.8-27b\]' "${app_root}/configmap.yaml"
rg -q 'fetch: true' "${app_root}/configmap.yaml"
if rg -q -- '- (gemma-4-e4b-it|gemma-4-12b-it|gemma-4-26b-a4b-it|qwen3\.6-35b-a3b)' "${app_root}/configmap.yaml"; then
  printf 'LibreChat must discover chat models instead of hardcoding a picker allowlist\n' >&2
  exit 1
fi
rg -q 'IMAGE_GEN_OAI_MODEL: qwen-image-2\.1' "${app_root}/configmap.yaml"
if rg -q 'IMAGE_GEN_OAI_MODEL: qwen-image-2\.1-uncensored' "${app_root}/configmap.yaml"; then
  printf 'LibreChat must not default its global image tool to the uncensored model\n' >&2
  exit 1
fi
if rg -q 'tower\.mousses\.xyz|10\.9\.20\.7|11434|name: Ollama' "${app_root}/configmap.yaml" "${app_root}/networkpolicy.yaml"; then
  printf 'LibreChat still references the direct Ollama backend\n' >&2
  exit 1
fi

rg -q 'kubernetes\.io/metadata\.name: litellm' "${app_root}/networkpolicy.yaml"
rg -q 'kubernetes\.io/metadata\.name: librechat' "${app_root}/../litellm/networkpolicy.yaml"
rg -q '_shared/network-policy/authentik-oidc-egress' "${app_root}/kustomization.yaml"
rg -q 'name: allow-authentik-oidc-egress' <<<"${rendered}"
rg -q 'cidr: 10\.43\.204\.62/32' <<<"${rendered}"
rg -q 'port: websecure' <<<"${rendered}"
if rg -q '10\.9\.20\.14' "${app_root}/deployment.yaml" "${app_root}/networkpolicy.yaml"; then
  printf 'LibreChat must not send pod OIDC traffic to the NAS host IP\n' >&2
  exit 1
fi

printf 'LibreChat deployment contract passed\n'
