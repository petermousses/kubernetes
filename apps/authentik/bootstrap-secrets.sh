#!/usr/bin/env bash
set -euo pipefail

if ! command -v kubectl >/dev/null || ! command -v openssl >/dev/null; then
  printf 'kubectl and openssl are required\n' >&2
  exit 1
fi

kubectl create namespace authentik --dry-run=client -o yaml | kubectl apply -f -

if kubectl -n authentik get secret authentik-env >/dev/null 2>&1; then
  printf 'authentik-env already exists; refusing to rotate credentials\n' >&2
  exit 1
fi

read -r -s -p 'new Authentik akadmin password (20+ characters): ' bootstrap_password
printf '\n'
read -r -s -p 'confirm akadmin password: ' confirmation
printf '\n'
if (( ${#bootstrap_password} < 20 )) || [[ "${bootstrap_password}" != "${confirmation}" ]]; then
  printf 'passwords differ or contain fewer than 20 characters\n' >&2
  exit 1
fi
unset confirmation

secret_key="$(openssl rand -hex 60)"
postgres_password="$(openssl rand -hex 32)"

{
  printf 'AUTHENTIK_SECRET_KEY=%s\n' "${secret_key}"
  printf 'AUTHENTIK_POSTGRESQL__PASSWORD=%s\n' "${postgres_password}"
  printf 'POSTGRES_PASSWORD=%s\n' "${postgres_password}"
  printf 'AUTHENTIK_BOOTSTRAP_PASSWORD=%s\n' "${bootstrap_password}"
} | kubectl -n authentik create secret generic authentik-env --from-env-file=/dev/stdin

unset bootstrap_password secret_key postgres_password
printf 'created authentik-env; values were not printed\n'
