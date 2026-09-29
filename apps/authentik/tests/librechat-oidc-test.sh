#!/usr/bin/env bash
set -euo pipefail

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
rendered="$(kubectl kustomize "${app_root}")"

[[ "${rendered}" == *'name: authentik-librechat-oidc-blueprint'* ]]
rg -q 'blueprints:' "${app_root}/helmchart.yaml"
rg -q 'authentik-librechat-oidc-blueprint' "${app_root}/helmchart.yaml"
rg -q 'authentik-librechat-oidc' "${app_root}/helmchart.yaml"
rg -q 'client_secret: !Env LIBRECHAT_OIDC_CLIENT_SECRET' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'client_id: !Env LIBRECHAT_OIDC_CLIENT_ID' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'https://librechat\.omv\.mousses\.xyz/oauth/openid/callback' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'matching_mode: strict' "${app_root}/librechat-oidc-blueprint.yaml"
[[ "$(rg -c 'url: https://' "${app_root}/librechat-oidc-blueprint.yaml")" == 1 ]]
rg -q 'grant_types: \[authorization_code\]' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'include_claims_in_id_token: true' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'name: librechat_users' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'name: librechat_admin' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'group: !KeyOf librechat-users' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'scope_name, profile' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'signing_key: !Find' "${app_root}/librechat-oidc-blueprint.yaml"
rg -q 'authentik-librechat-oidc' "${app_root}/deploy.sh"

printf 'authentik LibreChat OIDC contract passed\n'
