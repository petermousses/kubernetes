#!/usr/bin/env bash
set -euo pipefail

test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd -- "${test_root}/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -f -- "${test_dir}/calls" "${test_dir}/secret" "${test_dir}/output"; rmdir -- "${test_dir}"' EXIT

export LIBRECHAT_TEST_CALLS="${test_dir}/calls"
export LIBRECHAT_TEST_SECRET="${test_dir}/secret"

printf 'sk-test-only\n' | PATH="${test_root}/bin:${PATH}" \
  bash "${app_root}/bootstrap-secrets.sh" >"${test_dir}/output" 2>&1
rg -q '^LITELLM_API_KEY=sk-test-only$' "${LIBRECHAT_TEST_SECRET}"
for key in CREDS_KEY CREDS_IV JWT_SECRET JWT_REFRESH_SECRET MEILI_MASTER_KEY; do
  rg -q "^${key}=[0-9a-f]+$" "${LIBRECHAT_TEST_SECRET}"
done
if rg -q 'sk-test-only|CREDS_KEY=' "${test_dir}/output"; then
  printf 'bootstrap leaked a credential\n' >&2
  exit 1
fi

rm -f -- "${LIBRECHAT_TEST_CALLS}" "${LIBRECHAT_TEST_SECRET}"
if printf 'sk-test-only\n' | LIBRECHAT_TEST_EXISTING_SECRET=true \
   PATH="${test_root}/bin:${PATH}" bash "${app_root}/bootstrap-secrets.sh" \
   >"${test_dir}/output" 2>&1; then
  printf 'existing application Secret must stop bootstrap\n' >&2
  exit 1
fi
if rg -q 'create secret' "${LIBRECHAT_TEST_CALLS}"; then
  printf 'bootstrap must not overwrite an existing Secret\n' >&2
  exit 1
fi

rm -f -- "${LIBRECHAT_TEST_CALLS}" "${LIBRECHAT_TEST_SECRET}"
if printf 'sk-test-only\n' | LIBRECHAT_TEST_MISSING_PREREQUISITE=true \
   PATH="${test_root}/bin:${PATH}" bash "${app_root}/bootstrap-secrets.sh" \
   >"${test_dir}/output" 2>&1; then
  printf 'missing OIDC prerequisite must stop bootstrap\n' >&2
  exit 1
fi
if rg -q 'create secret' "${LIBRECHAT_TEST_CALLS}"; then
  printf 'bootstrap must not create a Secret when OIDC is missing\n' >&2
  exit 1
fi

rm -f -- "${LIBRECHAT_TEST_CALLS}" "${LIBRECHAT_TEST_SECRET}"
if printf '\n' | PATH="${test_root}/bin:${PATH}" \
   bash "${app_root}/bootstrap-secrets.sh" >"${test_dir}/output" 2>&1; then
  printf 'empty virtual key must stop bootstrap\n' >&2
  exit 1
fi
if rg -q 'create secret' "${LIBRECHAT_TEST_CALLS}"; then
  printf 'bootstrap must not create a Secret with an empty key\n' >&2
  exit 1
fi

printf 'LibreChat secret bootstrap contract passed\n'
