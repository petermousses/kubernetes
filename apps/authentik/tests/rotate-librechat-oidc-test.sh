#!/usr/bin/env bash
set -euo pipefail

test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd -- "${test_root}/.." && pwd)"
test_dir="$(mktemp -d)"
cleanup() {
  rm -f -- "${test_dir}/calls" "${test_dir}/auth-env" "${test_dir}/librechat-env" \
    "${test_dir}/success-output" "${test_dir}/failure-output"
  rmdir -- "${test_dir}"
}
trap cleanup EXIT

export OIDC_TEST_CALLS="${test_dir}/calls"
export OIDC_TEST_AUTH_ENV="${test_dir}/auth-env"
export OIDC_TEST_LIBRECHAT_ENV="${test_dir}/librechat-env"

if PATH="${test_root}/bin:${PATH}" OIDC_TEST_EXISTING_SECRET=true \
   "${app_root}/rotate-librechat-oidc-secrets.sh" >"${test_dir}/success-output" 2>&1; then
  :
else
  printf 'rotation of existing Secrets must succeed\n' >&2
  exit 1
fi

rg -q '^-n authentik apply --server-side --force-conflicts -f -$' "${OIDC_TEST_CALLS}"
rg -q '^-n librechat apply --server-side --force-conflicts -f -$' "${OIDC_TEST_CALLS}"
rg -q '^-n authentik rollout restart deployment/authentik-worker$' "${OIDC_TEST_CALLS}"
rg -q '^-n authentik rollout status deployment/authentik-worker --timeout=5m$' "${OIDC_TEST_CALLS}"
rg -q '^apply -f .*/authentik/librechat-oidc-blueprint.yaml$' "${OIDC_TEST_CALLS}"
rg -q '^-n authentik exec deployment/authentik-worker -c worker -- ak apply_blueprint --dry-run ' "${OIDC_TEST_CALLS}"
rg -q '^-n authentik exec deployment/authentik-worker -c worker -- ak apply_blueprint /blueprints/' "${OIDC_TEST_CALLS}"

auth_id="$(sed -n 's/^LIBRECHAT_OIDC_CLIENT_ID=//p' "${OIDC_TEST_AUTH_ENV}")"
auth_secret="$(sed -n 's/^LIBRECHAT_OIDC_CLIENT_SECRET=//p' "${OIDC_TEST_AUTH_ENV}")"
librechat_id="$(sed -n 's/^OPENID_CLIENT_ID=//p' "${OIDC_TEST_LIBRECHAT_ENV}")"
librechat_secret="$(sed -n 's/^OPENID_CLIENT_SECRET=//p' "${OIDC_TEST_LIBRECHAT_ENV}")"
[[ -n "${auth_id}" && -n "${auth_secret}" ]]
[[ "${auth_id}" == "${librechat_id}" && "${auth_secret}" == "${librechat_secret}" ]]
rg -q '^OPENID_SESSION_SECRET=[[:xdigit:]]{64}$' "${OIDC_TEST_LIBRECHAT_ENV}"
if rg -Fq -- "${auth_secret}" "${test_dir}/success-output"; then
  printf 'rotated client secret must not appear in script output\n' >&2
  exit 1
fi
unset auth_id auth_secret librechat_id librechat_secret

rm -f -- "${OIDC_TEST_CALLS}" "${OIDC_TEST_AUTH_ENV}" "${OIDC_TEST_LIBRECHAT_ENV}"
if PATH="${test_root}/bin:${PATH}" "${app_root}/rotate-librechat-oidc-secrets.sh" >/dev/null 2>&1; then
  printf 'rotation must refuse missing OIDC Secrets\n' >&2
  exit 1
fi
if rg -q 'apply --server-side|rollout restart|apply_blueprint' "${OIDC_TEST_CALLS}"; then
  printf 'missing Secrets must not be mutated\n' >&2
  exit 1
fi

rm -f -- "${OIDC_TEST_CALLS}" "${OIDC_TEST_AUTH_ENV}" "${OIDC_TEST_LIBRECHAT_ENV}"
if OIDC_TEST_EXISTING_SECRET=true OIDC_TEST_LIBRECHAT_DEPLOYED=true \
   PATH="${test_root}/bin:${PATH}" "${app_root}/rotate-librechat-oidc-secrets.sh" >/dev/null 2>&1; then
  printf 'rotation must refuse a deployed LibreChat instance\n' >&2
  exit 1
fi
if rg -q 'apply --server-side|rollout restart|apply_blueprint' "${OIDC_TEST_CALLS}"; then
  printf 'deployed LibreChat must not have its Secrets or sessions changed\n' >&2
  exit 1
fi

rm -f -- "${OIDC_TEST_CALLS}" "${OIDC_TEST_AUTH_ENV}" "${OIDC_TEST_LIBRECHAT_ENV}"
if OIDC_TEST_EXISTING_SECRET=true OIDC_TEST_SECOND_APPLY_FAIL=true \
   PATH="${test_root}/bin:${PATH}" "${app_root}/rotate-librechat-oidc-secrets.sh" >/dev/null 2>&1; then
  printf 'partial Secret update must fail visibly\n' >&2
  exit 1
fi
if rg -q 'rollout restart' "${OIDC_TEST_CALLS}"; then
  printf 'partial update must not restart the worker\n' >&2
  exit 1
fi

rm -f -- "${OIDC_TEST_CALLS}" "${OIDC_TEST_AUTH_ENV}" "${OIDC_TEST_LIBRECHAT_ENV}"
if OIDC_TEST_EXISTING_SECRET=true OIDC_TEST_BLUEPRINT_FAIL=true \
   PATH="${test_root}/bin:${PATH}" "${app_root}/rotate-librechat-oidc-secrets.sh" \
   >"${test_dir}/failure-output" 2>&1; then
  printf 'invalid blueprint must fail rotation visibly\n' >&2
  exit 1
fi
if rg -q 'FAKE_LEAKED_CLIENT_SECRET' "${test_dir}/failure-output"; then
  printf 'secret-bearing blueprint errors must not reach script output\n' >&2
  exit 1
fi
if rg -q 'ak apply_blueprint /blueprints/' "${OIDC_TEST_CALLS}"; then
  printf 'invalid blueprint must not be applied\n' >&2
  exit 1
fi

printf 'OIDC rotation updates matching Secrets, validates and applies the blueprint, and suppresses secret-bearing output\n'
