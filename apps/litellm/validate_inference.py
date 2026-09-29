#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import math
import os
import re
import secrets
import struct
import tempfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse


DEFAULT_BASE_URL = "https://api.ai.omv.mousses.xyz"
EXPECTED_IMAGE_SIZE = (512, 512)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
INVALID_API_KEY = "sk-invalid-litellm-validation-key"


class ValidationResult(NamedTuple):
    choice: str
    confidence: float
    generated: Path
    edited: Path


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base URL must contain a hostname and no credentials")
    if parsed.query or parsed.fragment or parsed.params or parsed.path not in {"", "/"}:
        raise ValueError("base URL must not contain a path, query, or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("base URL must use HTTPS; HTTP is allowed only on loopback")
    return base_url.rstrip("/")


def _validate_api_key(api_key: str) -> str:
    if not API_KEY_PATTERN.fullmatch(api_key):
        raise ValueError("LiteLLM API key must be an sk- prefixed URL-safe token")
    return api_key


def _read_limited(response: object) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise RuntimeError("response exceeds the 64 MiB validation limit")
        except ValueError as error:
            raise RuntimeError("response has an invalid Content-Length") from error
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response exceeds the 64 MiB validation limit")
    return body


def _request(
    opener: urllib.request.OpenerDirector,
    *,
    url: str,
    method: str,
    api_key: str | None,
    body: bytes | None,
    content_type: str | None,
    timeout: float,
) -> tuple[int, bytes, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "redqueen-inference-validator/1",
    }
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        url, data=body, headers=headers, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return (
                response.status,
                _read_limited(response),
                response.headers.get("Content-Type", ""),
            )
    except urllib.error.HTTPError as error:
        with error:
            return (
                error.code,
                _read_limited(error),
                error.headers.get("Content-Type", ""),
            )


def _expect_status(
    opener: urllib.request.OpenerDirector,
    *,
    url: str,
    method: str,
    expected: int,
    api_key: str | None,
    body: bytes | None,
    content_type: str | None,
    timeout: float,
) -> None:
    status, response_body, _ = _request(
        opener,
        url=url,
        method=method,
        api_key=api_key,
        body=body,
        content_type=content_type,
        timeout=timeout,
    )
    if status != expected:
        excerpt = response_body[:2_048].decode("utf-8", errors="replace")
        if api_key:
            excerpt = excerpt.replace(api_key, "[redacted]")
        raise RuntimeError(
            f"{method} {url} returned HTTP {status}; expected {expected}: {excerpt}"
        )


def _post_json(
    opener: urllib.request.OpenerDirector,
    *,
    url: str,
    api_key: str,
    payload: dict,
    timeout: float,
) -> dict:
    status, response_body, response_type = _request(
        opener,
        url=url,
        method="POST",
        api_key=api_key,
        body=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
        content_type="application/json",
        timeout=timeout,
    )
    if status != 200:
        excerpt = response_body[:2_048].decode("utf-8", errors="replace")
        excerpt = excerpt.replace(api_key, "[redacted]")
        raise RuntimeError(f"POST {url} returned HTTP {status}: {excerpt}")
    if response_type.split(";", 1)[0].strip().lower() != "application/json":
        raise RuntimeError(
            f"POST {url} returned non-JSON content type {response_type!r}"
        )
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"POST {url} returned invalid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError(f"POST {url} returned a non-object JSON response")
    return result


def _validate_jev_response(response: dict) -> tuple[str, float]:
    if response.get("model") != "jevk5-4b-v0.3":
        raise RuntimeError("JevK5 response reported the wrong model")
    answer = response.get("answers", {}).get("route")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise RuntimeError("JevK5 response is missing the route choice")
    expected = {"delivered", "delayed", "misdelivered"}
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != expected:
        raise RuntimeError("JevK5 response has the wrong probability keys")
    values = list(probabilities.values())
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in values
    ) or not math.isclose(sum(values), 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise RuntimeError("JevK5 response has invalid probabilities")
    choice = answer.get("choice")
    if choice != "misdelivered":
        raise RuntimeError(f"JevK5 selected {choice!r}; expected 'misdelivered'")
    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise RuntimeError("JevK5 response has invalid confidence")
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(
        isinstance(usage.get(field), bool)
        or not isinstance(usage.get(field), int)
        or usage[field] < 0
        for field in ("input_tokens", "output_tokens")
    ):
        raise RuntimeError("JevK5 response has invalid token usage")
    return choice, float(confidence)


def _validate_png(image: bytes, expected_size: tuple[int, int]) -> None:
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("image response is not a PNG")
    offset = 8
    dimensions: tuple[int, int] | None = None
    saw_iend = False
    while offset < len(image):
        if offset + 12 > len(image):
            raise RuntimeError("PNG response is truncated")
        length = struct.unpack(">I", image[offset : offset + 4])[0]
        kind = image[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(image):
            raise RuntimeError("PNG response has a truncated chunk")
        expected_crc = struct.unpack(">I", image[data_end:crc_end])[0]
        actual_crc = zlib.crc32(kind + image[data_start:data_end]) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise RuntimeError("PNG response has an invalid checksum")
        if dimensions is None:
            if kind != b"IHDR" or length != 13:
                raise RuntimeError("PNG response is missing its initial IHDR chunk")
            dimensions = struct.unpack(">II", image[data_start : data_start + 8])
        if kind == b"IEND":
            if length != 0 or crc_end != len(image):
                raise RuntimeError("PNG response has an invalid IEND chunk")
            saw_iend = True
            break
        offset = crc_end
    if not saw_iend or dimensions != expected_size:
        raise RuntimeError(
            f"PNG response dimensions are {dimensions}; expected {expected_size}"
        )


def _decode_image_response(response: dict) -> bytes:
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise RuntimeError("image response must contain exactly one data item")
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str):
        raise RuntimeError("image response is missing b64_json")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RuntimeError("image response contains invalid base64") from error
    _validate_png(image, EXPECTED_IMAGE_SIZE)
    return image


def _multipart_edit(image: bytes) -> tuple[bytes, str]:
    boundary = f"litellm-validation-{secrets.token_hex(16)}"
    parts: list[bytes] = []
    fields = {
        "model": "qwen-image-2.1",
        "prompt": "change the red cube to a blue cube; preserve composition",
        "size": "512x512",
        "quality": "low",
        "background": "opaque",
        "response_format": "b64_json",
    }
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                b'Content-Disposition: form-data; name="image"; '
                b'filename="generated.png"\r\n'
            ),
            b"Content-Type: image/png\r\n\r\n",
            image,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as output:
        output.write(data)
    path.chmod(0o600)


def validate(
    *,
    base_url: str,
    api_key: str,
    output_dir: Path,
    timeout: float,
    opener: urllib.request.OpenerDirector | None = None,
) -> ValidationResult:
    base_url = _validate_base_url(base_url)
    api_key = _validate_api_key(api_key)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 1_800:
        raise ValueError("timeout must be greater than zero and at most 1800 seconds")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    generated_path = output_dir / "qwen-image-generated.png"
    edited_path = output_dir / "qwen-image-edited.png"
    if generated_path.exists() or edited_path.exists():
        raise FileExistsError("validation output files already exist")

    opener = opener or urllib.request.build_opener(NoRedirectHandler())
    jev_url = f"{base_url}/typesafe/v1/systemone"
    generation_url = f"{base_url}/v1/images/generations"
    edit_url = f"{base_url}/v1/images/edits"
    jev_payload = {
        "model": "jev-latest",
        "state": {
            "tracking_event": (
                "The carrier marked the parcel delivered, but it was delivered "
                "to the wrong address."
            )
        },
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Classify the parcel outcome.",
                "criteria": {
                    "delivered": "Delivered to the intended recipient",
                    "delayed": "Not yet delivered",
                    "misdelivered": "Delivered to the wrong address or recipient",
                },
            }
        },
    }
    generation_payload = {
        "model": "qwen-image-2.1",
        "prompt": "a centered red cube on a plain white background, studio lighting",
        "size": "512x512",
        "quality": "low",
        "background": "opaque",
        "n": 1,
        "response_format": "b64_json",
    }
    jev_body = json.dumps(jev_payload, separators=(",", ":")).encode()
    image_body = json.dumps(generation_payload, separators=(",", ":")).encode()

    _expect_status(
        opener,
        url=jev_url,
        method="GET",
        expected=404,
        api_key=None,
        body=None,
        content_type=None,
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=jev_url,
        method="POST",
        expected=401,
        api_key=None,
        body=jev_body,
        content_type="application/json",
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=generation_url,
        method="POST",
        expected=401,
        api_key=INVALID_API_KEY,
        body=image_body,
        content_type="application/json",
        timeout=timeout,
    )

    jev_response = _post_json(
        opener,
        url=jev_url,
        api_key=api_key,
        payload=jev_payload,
        timeout=timeout,
    )
    choice, confidence = _validate_jev_response(jev_response)

    generation_response = _post_json(
        opener,
        url=generation_url,
        api_key=api_key,
        payload=generation_payload,
        timeout=timeout,
    )
    generated = _decode_image_response(generation_response)
    _write_new(generated_path, generated)

    edit_body, edit_type = _multipart_edit(generated)
    status, response_body, response_type = _request(
        opener,
        url=edit_url,
        method="POST",
        api_key=api_key,
        body=edit_body,
        content_type=edit_type,
        timeout=timeout,
    )
    if status != 200:
        excerpt = response_body[:2_048].decode("utf-8", errors="replace")
        excerpt = excerpt.replace(api_key, "[redacted]")
        raise RuntimeError(f"POST {edit_url} returned HTTP {status}: {excerpt}")
    if response_type.split(";", 1)[0].strip().lower() != "application/json":
        raise RuntimeError(
            f"POST {edit_url} returned non-JSON content type {response_type!r}"
        )
    try:
        edit_response = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"POST {edit_url} returned invalid JSON") from error
    if not isinstance(edit_response, dict):
        raise RuntimeError(f"POST {edit_url} returned a non-object JSON response")
    edited = _decode_image_response(edit_response)
    _write_new(edited_path, edited)

    return ValidationResult(choice, confidence, generated_path, edited_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate JevK5 and Qwen Image through the public LiteLLM gateway."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    api_key = os.environ.get("LITELLM_API_KEY")
    if api_key is None:
        api_key = getpass.getpass("LiteLLM API key: ")
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="litellm-validation-"))
    result = validate(
        base_url=args.base_url,
        api_key=api_key,
        output_dir=output_dir,
        timeout=args.timeout,
    )
    print(
        f"JevK5 OK: choice={result.choice}, confidence={result.confidence:.6f}\n"
        f"Qwen Image generation OK: {result.generated}\n"
        f"Qwen Image edit OK: {result.edited}"
    )


if __name__ == "__main__":
    main()
