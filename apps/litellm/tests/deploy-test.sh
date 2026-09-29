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
grep -Fq 'logs -l job-name=litellm-migrations-v1-103-0' "${failure_log}"
grep -Fq 'describe job/litellm-migrations-v1-103-0' "${failure_log}"
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
grep -Fq "apply -k ${app_root}" "${success_log}"

grep -Fq 'backoffLimit: 0' "${app_root}/migration-job.yaml"
grep -Fq 'restartPolicy: Never' "${app_root}/migration-job.yaml"
grep -Fq 'runAsUser: 65532' "${app_root}/migration-job.yaml"
grep -Fq 'name: DATABASE_URL' "${app_root}/migration-job.yaml"
grep -Fq 'ghcr.io/berriai/litellm-migrations@sha256:dac0d22bbb18c418f45a5d1bf7c2cc044c812b77de23171df106d1cbb7fcbed5' \
  "${app_root}/migration-job.yaml"
if grep -Eq '^[[:space:]]+envFrom:' "${app_root}/migration-job.yaml"; then
  printf 'migration Job receives secrets it does not need\n' >&2
  exit 1
fi
if grep -Eq '^[[:space:]]+(command|workingDir):' "${app_root}/migration-job.yaml"; then
  printf 'migration Job overrides the dedicated image entrypoint\n' >&2
  exit 1
fi

printf 'litellm deploy tests passed\n'
