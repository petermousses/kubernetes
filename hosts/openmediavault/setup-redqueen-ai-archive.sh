#!/usr/bin/env bash
set -euo pipefail
umask 0022

readonly script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly repo_root="$(cd -- "${script_root}/../.." && pwd)"
readonly release_file="${repo_root}/hosts/borg-release.env"
readonly backup_parent=/filesystem/k3s/backups
readonly data_anchor=/filesystem/k3s/data
readonly archive_root=/filesystem/k3s/backups/redqueen-ai
readonly repository_path="${archive_root}/repo"
readonly archive_user=redqueen-archive
readonly archive_home=/var/lib/redqueen-archive
readonly borg_target=/usr/local/bin/borg
if [[ "$(id -u)" -ne 0 ]]; then
  printf 'run this NAS setup as root, normally with sudo\n' >&2
  exit 1
fi
for command in awk cat chmod chown cmp curl find findmnt getent gpg grep install \
  mktemp mv rm sha256sum stat useradd usermod; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    printf 'required command not found: %s; install its package on the NAS, then retry\n' \
      "${command}" >&2
    exit 1
  fi
done
[[ -r "${release_file}" ]] || {
  printf 'missing pinned Borg release metadata: %s\n' "${release_file}" >&2
  exit 1
}
# The release metadata is a versioned shell fragment containing only constants.
# shellcheck disable=SC1090
source "${release_file}"

[[ -d "${backup_parent}" && -d "${data_anchor}" ]] || {
  printf 'expected NAS paths are missing: %s and/or %s\n' "${backup_parent}" "${data_anchor}" >&2
  exit 1
}
readonly backup_mount="$(findmnt -n -o TARGET -T "${backup_parent}")"
readonly data_mount="$(findmnt -n -o TARGET -T "${data_anchor}")"
[[ -n "${backup_mount}" && "${backup_mount}" == "${data_mount}" ]] || {
  printf 'archive path and existing k3s data path are not on the same mounted filesystem (%s vs %s)\n' \
    "${backup_mount:-unknown}" "${data_mount:-unknown}" >&2
  exit 1
}

readonly temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/redqueen-ai-archive-setup.XXXXXXXX")"
cleanup() {
  rm -rf -- "${temporary_root}"
}
trap cleanup EXIT
readonly gpg_home="${temporary_root}/gnupg"
install -d -m 0700 -- "${gpg_home}"
readonly asset_url="https://github.com/borgbackup/borg/releases/download/${BORG_RELEASE}/${BORG_ASSET}"
readonly signature_url="${asset_url}.asc"
readonly asset_path="${temporary_root}/${BORG_ASSET}"
readonly signature_path="${asset_path}.asc"

if [[ -e "${borg_target}" ]]; then
  if [[ ! -x "${borg_target}" ]] || ! "${borg_target}" --version | grep -Fq "${BORG_RELEASE}"; then
    printf 'existing %s differs from pinned Borg %s; preserving it for review\n' \
      "${borg_target}" "${BORG_RELEASE}" >&2
    exit 1
  fi
else
  curl --fail --location --retry 3 --connect-timeout 20 --output "${asset_path}" "${asset_url}"
  curl --fail --location --retry 3 --connect-timeout 20 --output "${signature_path}" "${signature_url}"
  printf '%s  %s\n' "${BORG_ASSET_SHA256}" "${asset_path}" | sha256sum --check
  gpg --batch --homedir "${gpg_home}" --keyserver hkps://keyserver.ubuntu.com \
    --recv-keys "${BORG_SIGNING_FINGERPRINT}"
  readonly actual_fingerprint="$(gpg --batch --homedir "${gpg_home}" \
    --with-colons --fingerprint "${BORG_SIGNING_FINGERPRINT}" \
    | awk -F: '$1 == "fpr" { print toupper($10); exit }')"
  [[ "${actual_fingerprint}" == "${BORG_SIGNING_FINGERPRINT}" ]] || {
    printf 'Borg signing-key fingerprint did not match the pinned primary key\n' >&2
    exit 1
  }
  gpg --batch --homedir "${gpg_home}" --verify "${signature_path}" "${asset_path}"
  chmod 0755 "${asset_path}"
  install -o root -g root -m 0755 -- "${asset_path}" "${borg_target}"
fi

if getent passwd "${archive_user}" >/dev/null; then
  IFS=: read -r _ _ _ _ _ existing_home existing_shell \
    <<<"$(getent passwd "${archive_user}")"
  [[ "${existing_home}" == "${archive_home}" && "${existing_shell}" == /bin/dash ]] || {
    printf 'existing %s account has an unexpected home or shell; preserving it for review\n' \
      "${archive_user}" >&2
    exit 1
  }
  readonly existing_shadow_entry="$(getent shadow "${archive_user}")"
  IFS=: read -r _ existing_password_field _ <<<"${existing_shadow_entry}"
  [[ "${existing_password_field}" == NP ]] || {
    printf 'existing %s account does not have the expected key-only password field; preserving it\n' \
      "${archive_user}" >&2
    exit 1
  }
else
  useradd --system --user-group --home-dir "${archive_home}" --create-home \
    --shell /bin/dash "${archive_user}"
  # Debian sshd rejects accounts whose shadow password begins with '!'
  # before public-key auth. NP is not a valid password hash, but permits keys.
  usermod --password NP "${archive_user}"
fi
install -d -o root -g root -m 0755 -- "${archive_home}/.ssh"
if [[ ! -e "${archive_home}/.ssh/authorized_keys" ]]; then
  install -o root -g root -m 0644 /dev/null "${archive_home}/.ssh/authorized_keys"
fi
[[ -f "${archive_home}/.ssh/authorized_keys" && ! -L "${archive_home}/.ssh/authorized_keys" ]] || {
  printf 'authorized_keys must be a regular file; preserving unexpected path\n' >&2
  exit 1
}
chown root:root "${archive_home}/.ssh/authorized_keys"
# The key is public, and sshd must be able to read it for this system account.
# Keep root ownership so the restricted user cannot alter its forced command.
chmod 0644 "${archive_home}/.ssh/authorized_keys"

install -d -o root -g root -m 0755 -- "${archive_root}"
if [[ -e "${repository_path}" ]]; then
  [[ -d "${repository_path}" && ! -L "${repository_path}" ]] || {
    printf 'repository path exists but is not a regular directory; preserving it\n' >&2
    exit 1
  }
  if [[ -f "${repository_path}/config" ]]; then
    [[ "$(stat -c %U "${repository_path}")" == "${archive_user}" ]] || {
      printf 'existing Borg repository is not owned by %s; preserving it\n' "${archive_user}" >&2
      exit 1
    }
  elif [[ -n "$(find "${repository_path}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'repository directory is non-empty but has no Borg config; preserving it\n' >&2
    exit 1
  else
    chown "${archive_user}:${archive_user}" "${repository_path}"
    chmod 0700 "${repository_path}"
  fi
else
  install -d -o "${archive_user}" -g "${archive_user}" -m 0700 -- "${repository_path}"
fi

printf 'NAS receiver prepared at %s\n' "${repository_path}"
printf 'Borg %s is installed at %s; account %s is locked for password login\n' \
  "${BORG_RELEASE}" "${borg_target}" "${archive_user}"
printf 'the authorized_keys file is %s; add the redqueen public key there with this exact prefix:\n' \
  "${archive_home}/.ssh/authorized_keys"
printf 'command="%s serve --append-only --restrict-to-repository %s",restrict\n' \
  "${borg_target}" "${repository_path}"
