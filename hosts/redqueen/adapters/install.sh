#!/usr/bin/env bash
set -euo pipefail
umask 0027

readonly source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly host_root="$(cd -- "${source_root}/.." && pwd)"
readonly repo_root="$(cd -- "${host_root}/../.." && pwd)"
readonly target_root="/srv/ai/adapters"
readonly package_target="${target_root}/source/redqueen_adapters"
readonly workflow_target="${target_root}/workflows"
readonly venv="${target_root}/venv"
readonly user_unit_dir="${HOME}/.config/systemd/user"

if [[ "$(id -u)" -eq 0 ]]; then
  printf 'refusing to install the adapters as root\n' >&2
  exit 1
fi
for command in python3 systemctl; do
  if ! command -v "${command}" >/dev/null; then
    printf 'required command not found: %s\n' "${command}" >&2
    exit 1
  fi
done

install -d -m 0750 -- "${package_target}" "${workflow_target}"
install -m 0640 -- "${repo_root}/LICENSE" "${target_root}/LICENSE"
install -m 0640 -- "${source_root}/NOTICE" "${target_root}/NOTICE"
install -m 0640 -- "${source_root}/requirements.lock" "${target_root}/requirements.lock"
for source in "${source_root}"/redqueen_adapters/*.py; do
  install -m 0640 -- "${source}" "${package_target}/$(basename -- "${source}")"
done
for name in \
  qwen-image-2.1-t2i-smoke-api.json \
  qwen-image-2.1-transparency-smoke-api.json \
  qwen-image-2.1-edit-smoke-api.json \
  qwen-image-2.1-multiref-smoke-api.json; do
  install -m 0640 -- "${host_root}/comfyui/${name}" "${workflow_target}/${name}"
done

if [[ ! -x "${venv}/bin/python" ]]; then
  python3 -m venv "${venv}"
fi
"${venv}/bin/pip" install \
  --disable-pip-version-check \
  --no-input \
  --only-binary=:all: \
  --upgrade \
  --requirement "${source_root}/requirements.lock"
PYTHONPATH="${target_root}/source" "${venv}/bin/python" -m unittest discover \
  -s "${source_root}/tests" -v

install -d -m 0755 -- "${user_unit_dir}"
for unit in qwen38.service qwen-image-adapter.service jevk5-adapter.service; do
  install -m 0644 -- "${host_root}/systemd/${unit}" "${user_unit_dir}/${unit}"
done
systemctl --user daemon-reload

printf 'adapter files and user units installed; credentials are intentionally not created\n'
