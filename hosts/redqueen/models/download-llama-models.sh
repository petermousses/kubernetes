#!/usr/bin/env bash
set -euo pipefail
umask 0027

readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -eq 0 ]]; then
  printf 'refusing to download model files as root\n' >&2
  exit 1
fi

download() {
  local model_root="$1"
  local url="$2"
  local relative_path="$3"
  local expected_sha256="$4"
  local destination="${model_root}/${relative_path}"
  local partial="${destination}.part"
  local http_status

  mkdir -p -- "$(dirname -- "${destination}")"
  if [[ -f "${destination}" ]] && printf '%s  %s\n' "${expected_sha256}" "${destination}" | sha256sum --check --status; then
    printf 'verified existing %s\n' "${destination}"
    return
  fi

  if [[ -f "${partial}" ]] && printf '%s  %s\n' "${expected_sha256}" "${partial}" | sha256sum --check --status; then
    mv -- "${partial}" "${destination}"
    chmod 0440 "${destination}"
    printf 'promoted verified partial %s\n' "${destination}"
    return
  fi

  if ! http_status="$(curl \
    --fail \
    --location \
    --retry 10 \
    --retry-delay 5 \
    --connect-timeout 20 \
    --continue-at - \
    --output "${partial}" \
    --write-out '%{http_code}' \
    "${url}")" && [[ "${http_status}" != 416 ]]; then
    return 1
  fi

  if ! printf '%s  %s\n' "${expected_sha256}" "${partial}" | sha256sum --check --status; then
    printf 'discarding corrupt partial and retrying %s from byte zero\n' "${destination}" >&2
    rm -f -- "${partial}"
    curl \
      --fail \
      --location \
      --retry 10 \
      --retry-all-errors \
      --retry-delay 5 \
      --connect-timeout 20 \
      --output "${partial}" \
      "${url}"
    printf '%s  %s\n' "${expected_sha256}" "${partial}" | sha256sum --check --status
  fi
  mv -- "${partial}" "${destination}"
  chmod 0440 "${destination}"
  printf 'downloaded and verified %s\n' "${destination}"
}

download_qwen() {
  local model_root="${QWEN_MODEL_ROOT:-/srv/ai/models/qwen3.8-27b}"
  local gguf_revision="71bc7b627595dc8a91039addd9c791ae548d6747"
  local source_revision="1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
  local gguf_base="https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF/resolve/${gguf_revision}"
  local source_base="https://huggingface.co/Qwen/Qwen3.8-27B/resolve/${source_revision}"

  mkdir -p -- "${model_root}"
  exec 8>"${model_root}/.download.lock"
  flock -n 8 || {
    printf 'another Qwen3.8 download is already running\n' >&2
    return 1
  }

  download "${model_root}" "${gguf_base}/Qwen3.8-27B-Q4_K_M.gguf" \
    Qwen3.8-27B-Q4_K_M.gguf \
    c600de0300ae8a0eb3a6c0b8b5561b8b96f16bd2c863c2a66c42de29d391a747
  download "${model_root}" "${gguf_base}/mmproj-Qwen3.8-27B-Q8_0.gguf" \
    mmproj-Qwen3.8-27B-Q8_0.gguf \
    2e968a6af97ce35d8971890b257b9b7edabf20ad91450501fa53162a19ee33eb
  download "${model_root}" "${source_base}/LICENSE" \
    LICENSE \
    bbedc3fda3305820b977265f01b8619d87570a6739de3a5582c3464840f1e57a
  download "${model_root}" "${source_base}/README.md" \
    QWEN_MODEL_CARD.md \
    57e4bdb258ee1a7d2635c5174ebd4e56abe392505cdb5f8bbb356b0dc4293641
  download "${model_root}" "${gguf_base}/README.md" \
    GGUF_MODEL_CARD.md \
    4a1ddebc85fd59b14650bb04757dea42cfefa63b75173e15fd5bc919d2861adb

  install -m 0440 "${script_dir}/qwen3.8-27b.sha256" "${model_root}/SHA256SUMS"
  local source_revision_partial
  source_revision_partial="$(mktemp "${model_root}/.SOURCE_REVISION.XXXXXX")"
  printf '%s\n' \
    'gguf_repository=ggml-org/Qwen3.8-27B-GGUF' \
    "gguf_revision=${gguf_revision}" \
    'source_repository=Qwen/Qwen3.8-27B' \
    "source_revision=${source_revision}" \
    >"${source_revision_partial}"
  chmod 0440 "${source_revision_partial}"
  mv -- "${source_revision_partial}" "${model_root}/SOURCE_REVISION"
  (cd -- "${model_root}" && sha256sum --check SHA256SUMS)
}

download_jevk5() {
  local model_root="${JEVK5_MODEL_ROOT:-/srv/ai/models/jevk5-4b-v0.3}"
  local gguf_revision="ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a"
  local source_revision="f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a"
  local gguf_base="https://huggingface.co/alibiserikbay/JevK5-GGUF/resolve/${gguf_revision}"
  local source_base="https://raw.githubusercontent.com/allebee/jevk5/${source_revision}"

  mkdir -p -- "${model_root}"
  exec 7>"${model_root}/.download.lock"
  flock -n 7 || {
    printf 'another JevK5 download is already running\n' >&2
    return 1
  }

  download "${model_root}" "${gguf_base}/jevk5-4b-v0.3-Q8_0.gguf" \
    jevk5-4b-v0.3-Q8_0.gguf \
    aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30
  download "${model_root}" "${source_base}/LICENSE" \
    LICENSE \
    cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30
  download "${model_root}" "${gguf_base}/README.md" \
    MODEL_CARD.md \
    5c75f91401539afe2ce722e87f8e81f1bbd23cb3cea16758180be4a316389fbe
  download "${model_root}" "${gguf_base}/SHA256SUMS" \
    UPSTREAM_SHA256SUMS \
    ad21b6c0b7a21fb6cc6980495a91bb2a1c97078beacfc065d4f19e934d6d9422

  install -m 0440 "${script_dir}/jevk5-4b-v0.3.sha256" "${model_root}/SHA256SUMS"
  local source_revision_partial
  source_revision_partial="$(mktemp "${model_root}/.SOURCE_REVISION.XXXXXX")"
  printf '%s\n' \
    'gguf_repository=alibiserikbay/JevK5-GGUF' \
    "gguf_revision=${gguf_revision}" \
    'source_repository=allebee/jevk5' \
    "source_revision=${source_revision}" \
    >"${source_revision_partial}"
  chmod 0440 "${source_revision_partial}"
  mv -- "${source_revision_partial}" "${model_root}/SOURCE_REVISION"
  (cd -- "${model_root}" && sha256sum --check SHA256SUMS)
}

download_qwen
download_jevk5
