#!/usr/bin/env bash
set -euo pipefail

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
rendered="$(kubectl kustomize "${app_root}")"

[[ "${rendered}" == *'name: authentik-postgres'* ]]
[[ "${rendered}" == *'name: authentik-tls'* ]]
[[ "${rendered}" == *'name: default-deny-all'* ]]
[[ "${rendered}" != *'kind: IngressRoute'* ]]

rg -q 'version: 2026\.8\.3' "${app_root}/helmchart.yaml"
rg -q 'postgresql:[[:space:]]*$' "${app_root}/helmchart.yaml"
rg -q 'enabled: false' "${app_root}/helmchart.yaml"
rg -q 'existingSecret:' "${app_root}/helmchart.yaml"
rg -q 'serviceAccountName: default' "${app_root}/helmchart.yaml"
rg -q 'automountServiceAccountToken: false' "${app_root}/helmchart.yaml"
if [[ "$(rg -c 'name: ndots' "${app_root}/helmchart.yaml")" != 2 ]] ||
   [[ "$(rg -c 'value: "1"' "${app_root}/helmchart.yaml")" != 2 ]]; then
  printf 'server and worker must each set ndots=1\n' >&2
  exit 1
fi

rg -q 'get secret authentik-env' "${app_root}/bootstrap-secrets.sh"
rg -q 'rollout status statefulset/postgres' "${app_root}/deploy.sh"
rg -q 'kubectl apply -f "\$\{app_root\}/helmchart.yaml"' "${app_root}/deploy.sh"
! rg -q 'ingress\.yaml' "${app_root}/deploy.sh" "${app_root}/kustomization.yaml"

printf 'authentik pre-MFA deployment contract passed\n'
