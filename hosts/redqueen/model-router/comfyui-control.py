#!/usr/bin/env python3
"""Narrow local control endpoint for pausing ComfyUI during GLM inference."""

from __future__ import annotations

import hmac
import http.server
import os
import stat
import subprocess
import sys

HOST = "127.0.0.1"
PORT = 18981
UNIT = "comfyui.service"
SYSTEMCTL = "/usr/bin/systemctl"
STATE_PATH = "/srv/ai/model-router/run/comfyui-paused-by-glm"
GLM_MARKERS = (
    b"/srv/ai/model-router/bin/glm-5.3-flash-abliterated-launcher.sh",
    b"/srv/ai/models/huihui-glm-5.3-flash-abliterated-gguf/UD-IQ1_S/GLM-5.3-Flash-UD-IQ1_S-00001-of-00003.gguf",
)
TOKEN = os.environ.get("GLM_COMFY_CONTROL_KEY", "")
paused_by_helper = False


def active_state(unit: str) -> str:
    result = subprocess.run(
        [SYSTEMCTL, "--user", "show", unit, "--property=ActiveState", "--value"],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("user service manager query failed")
    state = result.stdout.strip()
    if state not in {"active", "inactive", "failed", "activating", "deactivating"}:
        raise RuntimeError("unexpected user service state")
    return state


def change_state(action: str, unit: str = UNIT) -> None:
    result = subprocess.run(
        [SYSTEMCTL, "--user", action, unit],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"could not {action} {unit}")


def write_pause_state() -> None:
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("ComfyUI pause-state path is not a regular file")
        os.ftruncate(fd, 0)
        os.write(fd, b"paused\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def clear_pause_state() -> None:
    global paused_by_helper
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("ComfyUI pause-state path is not a regular file")
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    paused_by_helper = False


def read_pause_state() -> bool:
    fd = os.open(STATE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("ComfyUI pause-state path is not a regular file")
        state = os.read(fd, 32)
    finally:
        os.close(fd)
    if state not in {b"", b"paused\n"}:
        raise RuntimeError("ComfyUI pause-state file has an invalid value")
    return state == b"paused\n"


def glm_process_running() -> bool:
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        try:
            with open(f"/proc/{entry.name}/cmdline", "rb") as source:
                command = source.read(131072)
        except OSError:
            continue
        if any(marker in command for marker in GLM_MARKERS):
            return True
    return False


def restore_comfyui() -> None:
    global paused_by_helper
    if not paused_by_helper:
        return
    if active_state(UNIT) != "active":
        change_state("start")
        if active_state(UNIT) != "active":
            raise RuntimeError("ComfyUI remained inactive after recovery start")
    clear_pause_state()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _reply(self, status: int, body: str) -> None:
        encoded = body.encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=us-ascii")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        return bool(TOKEN) and hmac.compare_digest(supplied, expected)

    def _restore_after_failed_pause(self) -> None:
        try:
            restore_comfyui()
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            print(f"ComfyUI recovery after failed pause failed: {exc}", file=sys.stderr, flush=True)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        global paused_by_helper
        if self.path not in {"/pause", "/resume"}:
            self._reply(404, "not found\n")
            return
        if not self._authorized():
            self._reply(403, "forbidden\n")
            return
        if self.headers.get("Content-Length", "0") != "0":
            self._reply(400, "request body not accepted\n")
            return

        try:
            state = active_state(UNIT)
            if self.path == "/pause":
                if state == "active":
                    try:
                        write_pause_state()
                        paused_by_helper = True
                        change_state("stop")
                        if active_state(UNIT) not in {"inactive", "failed"}:
                            raise RuntimeError("ComfyUI did not stop")
                    except (OSError, subprocess.SubprocessError, RuntimeError):
                        self._restore_after_failed_pause()
                        raise
                    else:
                        try:
                            self._reply(200, "restore\n")
                        except OSError:
                            self._restore_after_failed_pause()
                            raise
                        return
                if state in {"inactive", "failed"}:
                    self._reply(200, "restore\n" if paused_by_helper else "leave\n")
                    return
                self._reply(409, "ComfyUI is transitioning\n")
                return

            if not paused_by_helper:
                self._reply(200, "leave\n")
                return
            if state in {"inactive", "failed"}:
                change_state("start")
                if active_state(UNIT) != "active":
                    self._reply(409, "ComfyUI did not start\n")
                    return
            elif state != "active":
                self._reply(409, "ComfyUI is transitioning\n")
                return
            clear_pause_state()
            self._reply(200, "resumed\n")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            print(f"ComfyUI control error: {exc}", file=sys.stderr, flush=True)
            self._reply(503, "service control failed\n")

    def log_message(self, fmt: str, *args: object) -> None:
        # Do not log request headers or credentials.
        print(f"comfyui-control {self.client_address[0]} {fmt % args}", flush=True)


if not TOKEN or len(TOKEN) != 64 or any(char not in "0123456789abcdef" for char in TOKEN):
    raise SystemExit("GLM_COMFY_CONTROL_KEY must be a 64-character lowercase hex token")

class LocalHTTPServer(http.server.HTTPServer):
    request_queue_size = 8
    timeout = 2

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address

    def service_actions(self):
        if paused_by_helper and not glm_process_running():
            try:
                restore_comfyui()
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                print(f"ComfyUI recovery after GLM exit failed: {exc}", file=sys.stderr, flush=True)


try:
    paused_by_helper = read_pause_state()
    if paused_by_helper and (
        active_state("llama-swap.service") != "active" or not glm_process_running()
    ):
        restore_comfyui()
except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
    raise SystemExit(f"could not recover previous ComfyUI pause state: {exc}") from exc

server = LocalHTTPServer((HOST, PORT), Handler)
server.serve_forever()
