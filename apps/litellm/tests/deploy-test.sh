#!/usr/bin/env bash
set -euo pipefail

readonly test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly app_root="$(cd -- "${test_root}/.." && pwd)"
readonly fake_bin="${test_root}/bin"
readonly scratch="$(mktemp -d "${TMPDIR:-/tmp}/litellm-deploy-test.XXXXXX")"

cleanup() {
  rm -r -- "${scratch}"
}
trap cleanup EXIT

run_deploy() {
  local result="$1"
  local log="$2"
  local timeout_seconds="${3:-5}"

  PATH="${fake_bin}:${PATH}" \
    FAKE_KUBECTL_LOG="${log}" \
    FAKE_MIGRATION_RESULT="${result}" \
    LITELLM_MIGRATION_POLL_SECONDS=1 \
    LITELLM_MIGRATION_TIMEOUT_SECONDS="${timeout_seconds}" \
    "${app_root}/deploy.sh"
}

failure_log="${scratch}/failed-kubectl.log"
if run_deploy failed "${failure_log}" >"${scratch}/failed.out" 2>"${scratch}/failed.err"; then
  printf 'deploy unexpectedly succeeded after a failed migration\n' >&2
  exit 1
fi
grep -Fq 'logs -l job-name=litellm-migrations-v1-104-2' "${failure_log}"
grep -Fq 'describe job/litellm-migrations-v1-104-2' "${failure_log}"
if grep -Fq "apply -k ${app_root}" "${failure_log}"; then
  printf 'gateway manifests were applied after a failed migration\n' >&2
  exit 1
fi

timeout_log="${scratch}/timeout-kubectl.log"
if run_deploy pending "${timeout_log}" 1 >"${scratch}/timeout.out" 2>"${scratch}/timeout.err"; then
  printf 'deploy unexpectedly succeeded after a migration timeout\n' >&2
  exit 1
fi
grep -Fq 'did not finish within 1 seconds' "${scratch}/timeout.err"
if grep -Fq "apply -k ${app_root}" "${timeout_log}"; then
  printf 'gateway manifests were applied after a migration timeout\n' >&2
  exit 1
fi

success_log="${scratch}/successful-kubectl.log"
run_deploy complete "${success_log}" >"${scratch}/successful.out" 2>"${scratch}/successful.err"
grep -Fq 'delete ingress litellm --ignore-not-found' "${success_log}"
grep -Fq "apply -k ${app_root}" "${success_log}"
grep -Fq 'get pods\,svc\,ingressroute\,certificate\,endpointslice' "${success_log}"

grep -Fq 'backoffLimit: 0' "${app_root}/migration-job.yaml"
grep -Fq 'restartPolicy: Never' "${app_root}/migration-job.yaml"
grep -Fq 'runAsUser: 65532' "${app_root}/migration-job.yaml"
grep -Fq 'name: DATABASE_URL' "${app_root}/migration-job.yaml"
grep -Fq 'ghcr.io/berriai/litellm-migrations@sha256:35c0d71472914586ad683fa853403509b8a57a9975c0d1bc5d02b4adebfed91d' \
  "${app_root}/migration-job.yaml"
if grep -Eq '^[[:space:]]+envFrom:' "${app_root}/migration-job.yaml"; then
  printf 'migration Job receives secrets it does not need\n' >&2
  exit 1
fi
if awk '
  $0 == "        - name: migrations" { in_migrations = 1; next }
  in_migrations && $0 ~ /^        - name:/ { in_migrations = 0 }
  in_migrations && ($0 == "          command:" || $0 ~ /^          workingDir:/) {
    found_override = 1
  }
  END { exit found_override ? 0 : 1 }
' "${app_root}/migration-job.yaml"; then
  printf 'migration Job overrides the dedicated image entrypoint\n' >&2
  exit 1
fi

require_manifest_line() {
  local pattern="$1"
  local manifest="$2"

  if ! grep -Fq "${pattern}" "${app_root}/${manifest}"; then
    printf '%s is missing required PostgreSQL readiness configuration: %s\n' \
      "${manifest}" "${pattern}" >&2
    exit 1
  fi
}

for manifest in migration-job.yaml deployment.yaml; do
  require_manifest_line 'name: wait-for-postgres' "${manifest}"
  require_manifest_line 'readiness_timeout_seconds=600' "${manifest}"
  require_manifest_line 'pg_isready -h "$POSTGRES_HOST"' "${manifest}"
done

if grep -Fq 'name: DISABLE_ADMIN_UI' "${app_root}/deployment.yaml"; then
  printf 'the LiteLLM Admin UI must remain enabled for private tunnel access\n' >&2
  exit 1
fi

for metrics_contract in \
  '            - --prometheus_metrics_port' \
  '            - "4001"' \
  '              containerPort: 4001'; do
  if ! grep -Fq -- "${metrics_contract}" "${app_root}/deployment.yaml"; then
    printf 'LiteLLM is missing its dedicated metrics listener: %s\n' \
      "${metrics_contract}" >&2
    exit 1
  fi
done
grep -Fq 'kind: ServiceMonitor' "${app_root}/service-monitor.yaml"
grep -Fq 'path: /metrics/' "${app_root}/service-monitor.yaml"
grep -Fq 'name: litellm-metrics' "${app_root}/metrics-service.yaml"
grep -Fq 'port: 4001' "${app_root}/metrics-service.yaml"
grep -Fq '  - metrics-service.yaml' "${app_root}/kustomization.yaml"
grep -Fq '  - service-monitor.yaml' "${app_root}/kustomization.yaml"
grep -Fq 'name: allow-prometheus-metrics-ingress' "${app_root}/networkpolicy.yaml"
grep -Fq 'name: allow-litellm-metrics-egress' \
  "${app_root}/../monitoring/networkpolicy.yaml"
for typesafe_contract in \
  'name: TYPESAFE_API_BASE' \
  'value: http://jevk5-redqueen:8191'; do
  if ! grep -Fq "${typesafe_contract}" "${app_root}/deployment.yaml"; then
    printf 'LiteLLM is missing its temporary TypeSafe pass-through configuration: %s\n' \
      "${typesafe_contract}" >&2
    exit 1
  fi
done
for decisions_contract in \
  'name: REDQUEEN_DECISIONS_API_BASE' \
  'value: http://jevk5-redqueen:8191/v1' \
  'name: REDQUEEN_DECISIONS_API_KEY' \
  'key: TYPESAFE_API_KEY'; do
  if ! grep -Fq "${decisions_contract}" "${app_root}/deployment.yaml"; then
    printf 'LiteLLM is missing its OpenAI-compatible decisions configuration: %s\n' \
      "${decisions_contract}" >&2
    exit 1
  fi
done
grep -Fq 'TYPESAFE_API_KEY "${typesafe_api_key}"' \
  "${app_root}/bootstrap-secrets.sh"
grep -Fq 'name: jevk5-redqueen' "${app_root}/redqueen-backends.yaml"
grep -Fq 'port: 8191' "${app_root}/redqueen-backends.yaml"
grep -Fq 'port: 8191' "${app_root}/networkpolicy.yaml"

readonly -a configured_models=(
  qwen3.8-27b
  gemma-4-e4b-it
  gemma-4-12b-it
  gemma-4-26b-a4b-it
  qwen3.6-35b-a3b
  glm-5.3-flash-abliterated
  qwen-image-2.1
  qwen-image-2.1-uncensored
  jevk5-4b-v0.3
  clef-flash-bf16
  clef-flash-q8
  clef-flash-q4
  clef-q4
)
for model in "${configured_models[@]}"; do
  if ! grep -Fq "      - model_name: ${model}" "${app_root}/configmap.yaml"; then
    printf 'LiteLLM config is missing static model alias: %s\n' "${model}" >&2
    exit 1
  fi
done
glm_model_config="$(awk '
  /^      - model_name: glm-5.3-flash-abliterated$/ { capture = 1 }
  capture && /^      - model_name:/ && $0 !~ /glm-5[.]3-flash-abliterated/ { exit }
  capture { print }
' "${app_root}/configmap.yaml")"
for reasoning_contract in \
  '          supports_reasoning: true' \
  '          supported_reasoning_efforts:' \
  '            - low' \
  '            - high' \
  '            - max'; do
  if ! grep -Fq -- "${reasoning_contract}" <<<"${glm_model_config}"; then
    printf 'GLM LiteLLM metadata is missing reasoning contract: %s\n' \
      "${reasoning_contract}" >&2
    exit 1
  fi
done
if grep -Fq 'model_name: deepseek-v4.1-flash-q2' "${app_root}/configmap.yaml"; then
  printf 'DeepSeek must not be registered before its weights are available\n' >&2
  exit 1
fi
grep -Fq '      store_model_in_db: false' "${app_root}/configmap.yaml"
for priced_openai_model in \
  'openai/jevk5-4b-v0.3' \
  'openai/clef-flash-bf16' \
  'openai/clef-flash-q8' \
  'openai/clef-flash-q4' \
  'openai/clef-q4'; do
  if ! grep -Fq "${priced_openai_model}" "${app_root}/configmap.yaml"; then
    printf 'LiteLLM custom cost registry is missing %s\n' "${priced_openai_model}" >&2
    exit 1
  fi
done
if [[ "$(grep -Fc 'api_base: http://qwen-redqueen:8081/v1' "${app_root}/configmap.yaml")" -ne 7 ]]; then
  printf 'all six chat aliases and EmbeddingGemma must use the shared redqueen text endpoint\n' >&2
  exit 1
fi
if [[ "$(grep -Fc 'api_base: http://qwen-image-redqueen:8190/v1' "${app_root}/configmap.yaml")" -ne 2 ]]; then
  printf 'both Qwen Image aliases must use the shared authenticated image adapter\n' >&2
  exit 1
fi

grep -Fxq 'kind: IngressRoute' "${app_root}/ingress.yaml"
if grep -Fxq 'kind: Ingress' "${app_root}/ingress.yaml"; then
  printf 'the public API must use IngressRoute for HTTP method matching\n' >&2
  exit 1
fi

readonly public_host='Host(`api.ai.omv.mousses.xyz`)'
readonly -a public_api_rules=(
  "${public_host} && (Method(\`GET\`) || Method(\`POST\`)) && (Path(\`/v1/responses\`) || PathPrefix(\`/v1/responses/\`))"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/chat/completions\`)"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/chat/completions/input_tokens\`)"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/embeddings\`)"
  "${public_host} && Method(\`GET\`) && (Path(\`/v1/models\`) || PathPrefix(\`/v1/models/\`))"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/systemone\`)"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/decisions\`)"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/images/generations\`)"
  "${public_host} && Method(\`POST\`) && Path(\`/v1/images/edits\`)"
  "${public_host} && Method(\`POST\`) && Path(\`/typesafe/v1/systemone\`)"
  "${public_host} && Method(\`GET\`) && Path(\`/typesafe/v1/models\`)"
)
for public_api_rule in "${public_api_rules[@]}"; do
  if ! grep -Fq -- "match: ${public_api_rule}" "${app_root}/ingress.yaml"; then
    printf 'public API IngressRoute is missing its rule: %s\n' \
      "${public_api_rule}" >&2
    exit 1
  fi
done

public_rule_count="$(grep -Ec '^[[:space:]]+match: ' "${app_root}/ingress.yaml")"
if [[ "${public_rule_count}" -ne "${#public_api_rules[@]}" ]]; then
  printf 'public API IngressRoute must contain only the approved exact route rules\n' >&2
  exit 1
fi
if grep -Fq 'PathPrefix(`/v1/responses`)' "${app_root}/ingress.yaml" \
  || grep -Fq 'PathPrefix(`/v1/models`)' "${app_root}/ingress.yaml"; then
  printf 'route-family prefixes must end in / to prevent prefix confusion\n' >&2
  exit 1
fi

for required_line in \
  'apiVersion: traefik.io/v1alpha1' \
  '    - websecure' \
  'name: security-headers' \
  'name: request-limits' \
  'name: litellm' \
  'port: http' \
  'secretName: litellm-tls'; do
  if ! grep -Fq "${required_line}" "${app_root}/ingress.yaml"; then
    printf 'public API IngressRoute is missing required configuration: %s\n' \
      "${required_line}" >&2
    exit 1
  fi
done

for repeated_line in \
  '        - name: security-headers' \
  '        - name: request-limits' \
  '        - name: litellm' \
  '          port: http'; do
  repeated_line_count="$(grep -Fxc "${repeated_line}" "${app_root}/ingress.yaml")"
  if [[ "${repeated_line_count}" -ne "${#public_api_rules[@]}" ]]; then
    printf 'each public route must contain required configuration: %s\n' \
      "${repeated_line}" >&2
    exit 1
  fi
done

tcp_probe_count="$(
  grep -Fc 'command: [pg_isready, -h, 127.0.0.1, -U, litellm, -d, litellm]' \
    "${app_root}/postgres.yaml"
)"
if [[ "${tcp_probe_count}" -ne 3 ]]; then
  printf 'all three PostgreSQL health probes must verify TCP; found %s\n' \
    "${tcp_probe_count}" >&2
  exit 1
fi

PYTHONDONTWRITEBYTECODE=1 python3 "${test_root}/test_validate_inference.py"

printf 'litellm deploy tests passed\n'
