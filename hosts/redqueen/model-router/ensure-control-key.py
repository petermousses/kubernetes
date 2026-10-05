#!/usr/bin/env python3
"""Create the dedicated local ComfyUI-control credential without overwriting it."""

from __future__ import annotations

import os
import secrets
import stat
import sys
import tempfile

PATH = "/srv/ai/secrets/glm-comfy-control.env"
NAME = "GLM_COMFY_CONTROL_KEY"


def read_token(path: str) -> str | None:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise SystemExit("ComfyUI control credential must be a regular file owned by the ai user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("ComfyUI control credential permissions must be 0600")
    values: list[str] = []
    with open(path, encoding="ascii") as source:
        for line in source:
            if line.startswith(f"{NAME}="):
                values.append(line.rstrip("\n").split("=", 1)[1])
    if len(values) != 1:
        raise SystemExit("ComfyUI control credential must contain exactly one token")
    token = values[0]
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise SystemExit("ComfyUI control credential has an invalid token format")
    return token


os.makedirs(os.path.dirname(PATH), mode=0o750, exist_ok=True)
try:
    read_token(PATH)
except FileNotFoundError:
    token = secrets.token_hex(32)
    fd, temporary = tempfile.mkstemp(prefix=".glm-comfy-control.", dir=os.path.dirname(PATH))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as destination:
            destination.write(f"{NAME}={token}\n")
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, PATH)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    read_token(PATH)

print("dedicated ComfyUI-control credential is present (value withheld)")
