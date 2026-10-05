#!/usr/bin/env bash
set -Eeuo pipefail

readonly server=/srv/ai/runtimes/llama.cpp-v0.6.0-d812350/bin/llama-server
readonly systemctl=/usr/bin/systemctl
readonly comfyui=comfyui.service

if [[ ! -x "${server}" ]]; then
  printf 'pinned GLM runtime is missing: %s\n' "${server}" >&2
  exit 1
fi
if [[ ! -x "${systemctl}" ]]; then
  printf 'systemctl is missing: %s\n' "${systemctl}" >&2
  exit 1
fi

comfyui_state="$("${systemctl}" --user show "${comfyui}" --property=ActiveState --value)"
restore_comfyui=0
case "${comfyui_state}" in
  active)
    restore_comfyui=1
    ;;
  inactive|failed)
    ;;
  *)
    printf 'refusing to start GLM while ComfyUI is in state %s\n' \
      "${comfyui_state}" >&2
    exit 1
    ;;
esac

server_pid=''
signal_status=0
cleanup() {
  status=$?
  trap - EXIT INT TERM HUP
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill -INT "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" || true
  fi
  if ((restore_comfyui)); then
    if ! "${systemctl}" --user start "${comfyui}"; then
      printf 'failed to restore %s after GLM stopped\n' "${comfyui}" >&2
      status=1
    fi
  fi
  exit "${status}"
}
forward_signal() {
  local signal="$1"
  case "${signal}" in
    INT) signal_status=130 ;;
    TERM) signal_status=143 ;;
    HUP) signal_status=129 ;;
  esac
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill -s "${signal}" "${server_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM
trap 'forward_signal HUP' HUP

if ((restore_comfyui)); then
  "${systemctl}" --user stop "${comfyui}"
  comfyui_state="$("${systemctl}" --user show "${comfyui}" --property=ActiveState --value)"
  if [[ "${comfyui_state}" != inactive && "${comfyui_state}" != failed ]]; then
    printf 'ComfyUI did not stop; refusing to load GLM alongside it\n' >&2
    exit 1
  fi
fi

"${server}" "$@" &
server_pid=$!
while true; do
  set +e
  wait "${server_pid}"
  status=$?
  set -e
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    break
  fi
done
server_pid=''
if ((signal_status)); then
  exit "${signal_status}"
fi
exit "${status}"
