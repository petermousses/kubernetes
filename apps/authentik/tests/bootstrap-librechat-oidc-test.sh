#!/usr/bin/env bash
set -euo pipefail

test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd -- "${test_root}/.." && pwd)"
test_dir="$(mktemp -d)"
cleanup() {
  rm -f -- "${test_dir}/calls" "${test_dir}/namespace-ready"
  rmdir -- "${test_dir}"
}
trap cleanup EXIT

export OIDC_TEST_CALLS="${test_dir}/calls"
export OIDC_TEST_NAMESPACE_READY="${test_dir}/namespace-ready"

PATH="${test_root}/bin:${PATH}" "${app_root}/bootstrap-librechat-oidc-secrets.sh"

rg -q '^apply -f .*/librechat/namespace.yaml$' "${OIDC_TEST_CALLS}"
rg -q '^-n authentik create secret generic authentik-librechat-oidc ' "${OIDC_TEST_CALLS}"
rg -q '^-n librechat create secret generic librechat-oidc ' "${OIDC_TEST_CALLS}"

rm -f -- "${OIDC_TEST_CALLS}" "${OIDC_TEST_NAMESPACE_READY}"
if OIDC_TEST_APPLY_FAIL=true PATH="${test_root}/bin:${PATH}" \
   "${app_root}/bootstrap-librechat-oidc-secrets.sh" >/dev/null 2>&1; then
  printf 'namespace creation failure must stop bootstrap\n' >&2
  exit 1
fi
if rg -q 'create secret' "${OIDC_TEST_CALLS}"; then
  printf 'no Secret may be created when the namespace cannot be created\n' >&2
  exit 1
fi

rm -f -- "${OIDC_TEST_CALLS}" "${OIDC_TEST_NAMESPACE_READY}"
if OIDC_TEST_EXISTING_SECRET=true PATH="${test_root}/bin:${PATH}" \
   "${app_root}/bootstrap-librechat-oidc-secrets.sh" >/dev/null 2>&1; then
  printf 'existing OIDC Secret must stop bootstrap\n' >&2
  exit 1
fi
if rg -q 'create secret' "${OIDC_TEST_CALLS}"; then
  printf 'no Secret may be overwritten by bootstrap\n' >&2
  exit 1
fi

printf 'OIDC bootstrap creates the missing LibreChat namespace before either Secret\n'
