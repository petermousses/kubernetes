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
DEFAULT_IMAGE_MODEL = "qwen-image-2.1"
SUPPORTED_IMAGE_MODELS = (DEFAULT_IMAGE_MODEL, "qwen-image-2.1-uncensored")
SUPPORTED_TEXT_MODELS = (
    "qwen3.8-27b",
    "gemma-4-e4b-it",
    "gemma-4-12b-it",
    "gemma-4-26b-a4b-it",
    "qwen3.6-35b-a3b",
    "glm-5.3-flash-abliterated",
)
SUPPORTED_DECISION_MODELS = (
    "jevk5-4b-v0.3",
    "clef-flash-bf16",
    "clef-flash-q8",
    "clef-flash-q4",
    "clef-q4",
)
CLEF_IMAGE_COMPAT_MODEL = "clef-flash-bf16"


class ValidationResult(NamedTuple):
    choice: str
    confidence: float
    clef_image_choice: str
    generated: Path
    edited: Path
    text_models: tuple[str, ...]


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


def _validate_decisions_response(response: dict) -> None:
    if response.get("model") != "jevk5-4b-v0.3":
        raise RuntimeError("JevK5 Decisions response reported the wrong model")
    answers = response.get("answers")
    if not isinstance(answers, list) or len(answers) != 1:
        raise RuntimeError("JevK5 Decisions response must contain one answer")
    answer = answers[0]
    if (
        not isinstance(answer, dict)
        or answer.get("type") != "choice"
        or answer.get("name") != "route"
    ):
        raise RuntimeError("JevK5 Decisions response is missing the route choice")
    expected = {"delivered", "delayed", "misdelivered"}
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, list) or len(probabilities) != len(expected):
        raise RuntimeError("JevK5 Decisions response has invalid probabilities")
    observed: dict[str, float] = {}
    for probability in probabilities:
        if not isinstance(probability, dict):
            raise RuntimeError("JevK5 Decisions response has invalid probabilities")
        label = probability.get("value")
        value = probability.get("probability")
        if (
            not isinstance(label, str)
            or label not in expected
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise RuntimeError("JevK5 Decisions response has invalid probabilities")
        observed[label] = float(value)
    if set(observed) != expected or not math.isclose(
        sum(observed.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6
    ):
        raise RuntimeError("JevK5 Decisions response has invalid probabilities")
    choice = answer.get("choice")
    if choice != "misdelivered":
        raise RuntimeError(
            f"JevK5 Decisions selected {choice!r}; expected 'misdelivered'"
        )
    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise RuntimeError("JevK5 Decisions response has invalid confidence")
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(
        isinstance(usage.get(field), bool)
        or not isinstance(usage.get(field), int)
        or usage[field] < 0
        for field in ("input_tokens", "output_tokens")
    ):
        raise RuntimeError("JevK5 Decisions response has invalid token usage")


def _validate_clef_image_response(response: dict) -> str:
    if response.get("model") != CLEF_IMAGE_COMPAT_MODEL:
        raise RuntimeError("Clef image response reported the wrong model")
    answers = response.get("answers")
    answer = answers.get("visual_check") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise RuntimeError("Clef image response is missing its visual choice")
    expected = {"red_cube", "other"}
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != expected:
        raise RuntimeError("Clef image response has the wrong probability keys")
    values = list(probabilities.values())
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in values
    ) or not math.isclose(sum(values), 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise RuntimeError("Clef image response has invalid probabilities")
    choice = answer.get("choice")
    if choice != "red_cube":
        raise RuntimeError(f"Clef classified the generated image as {choice!r}")
    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise RuntimeError("Clef image response has invalid confidence")
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(
        isinstance(usage.get(field), bool)
        or not isinstance(usage.get(field), int)
        or usage[field] < 0
        for field in ("input_tokens", "output_tokens")
    ):
        raise RuntimeError("Clef image response has invalid token usage")
    return choice


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


def _validate_text_response(response: dict, model: str) -> None:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise RuntimeError(f"text model {model} did not return exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError(f"text model {model} returned a malformed choice")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"text model {model} returned no assistant message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"text model {model} returned empty text content")


def _multipart_edit(
    image: bytes, image_model: str = DEFAULT_IMAGE_MODEL
) -> tuple[bytes, str]:
    boundary = f"litellm-validation-{secrets.token_hex(16)}"
    parts: list[bytes] = []
    # LiteLLM treats this as a GPT Image-compatible request. Those models
    # always return base64 and do not accept the DALL-E-only response_format.
    fields = {
        "model": image_model,
        "prompt": "change the red cube to a blue cube; preserve composition",
        "size": "512x512",
        "quality": "low",
        "background": "opaque",
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
    image_model: str = DEFAULT_IMAGE_MODEL,
    text_models: tuple[str, ...] = (),
    opener: urllib.request.OpenerDirector | None = None,
) -> ValidationResult:
    base_url = _validate_base_url(base_url)
    api_key = _validate_api_key(api_key)
    if image_model not in SUPPORTED_IMAGE_MODELS:
        raise ValueError(
            f"unsupported image model; supported image model ids: "
            f"{', '.join(SUPPORTED_IMAGE_MODELS)}"
        )
    if any(model not in SUPPORTED_TEXT_MODELS for model in text_models):
        raise ValueError(
            f"unsupported text model; supported text model ids: "
            f"{', '.join(SUPPORTED_TEXT_MODELS)}"
        )
    if len(set(text_models)) != len(text_models):
        raise ValueError("text model ids must not contain duplicates")
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 1_800:
        raise ValueError("timeout must be greater than zero and at most 1800 seconds")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_name = "qwen-image" if image_model == DEFAULT_IMAGE_MODEL else image_model
    generated_path = output_dir / f"{output_name}-generated.png"
    edited_path = output_dir / f"{output_name}-edited.png"
    if generated_path.exists() or edited_path.exists():
        raise FileExistsError("validation output files already exist")

    opener = opener or urllib.request.build_opener(NoRedirectHandler())
    models_url = f"{base_url}/v1/models"
    systemone_url = f"{base_url}/v1/systemone"
    decisions_url = f"{base_url}/v1/decisions"
    typesafe_url = f"{base_url}/typesafe/v1/systemone"
    typesafe_models_url = f"{base_url}/typesafe/v1/models"
    chat_url = f"{base_url}/v1/chat/completions"
    generation_url = f"{base_url}/v1/images/generations"
    edit_url = f"{base_url}/v1/images/edits"
    jev_payload = {
        "model": "jevk5-4b-v0.3",
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
    decisions_payload = {
        "model": "jevk5-4b-v0.3",
        "input": (
            "The carrier marked the parcel delivered, but it was delivered "
            "to the wrong address."
        ),
        "questions": [
            {
                "type": "choice",
                "name": "route",
                "instructions": "Classify the parcel outcome.",
                "choices": [
                    {
                        "value": "delivered",
                        "description": "Delivered to the intended recipient",
                    },
                    {"value": "delayed", "description": "Not yet delivered"},
                    {
                        "value": "misdelivered",
                        "description": "Delivered to the wrong address or recipient",
                    },
                ],
            }
        ],
    }
    generation_payload = {
        "model": image_model,
        "prompt": "a centered red cube on a plain white background, studio lighting",
        "size": "512x512",
        "quality": "low",
        "background": "opaque",
        "n": 1,
    }
    jev_body = json.dumps(jev_payload, separators=(",", ":")).encode()
    decisions_body = json.dumps(
        decisions_payload, separators=(",", ":")
    ).encode()
    text_payload = {
        "messages": [
            {"role": "user", "content": "Reply with one short greeting."}
        ],
        "max_tokens": 32,
        "temperature": 0,
        "stream": False,
    }
    image_body = json.dumps(generation_payload, separators=(",", ":")).encode()

    _expect_status(
        opener,
        url=systemone_url,
        method="GET",
        expected=404,
        api_key=None,
        body=None,
        content_type=None,
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=decisions_url,
        method="GET",
        expected=404,
        api_key=None,
        body=None,
        content_type=None,
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=typesafe_url,
        method="GET",
        expected=404,
        api_key=None,
        body=None,
        content_type=None,
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=typesafe_models_url,
        method="GET",
        expected=401,
        api_key=None,
        body=None,
        content_type=None,
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=systemone_url,
        method="POST",
        expected=401,
        api_key=None,
        body=jev_body,
        content_type="application/json",
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=decisions_url,
        method="POST",
        expected=401,
        api_key=None,
        body=decisions_body,
        content_type="application/json",
        timeout=timeout,
    )
    _expect_status(
        opener,
        url=typesafe_url,
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

    if text_models:
        _expect_status(
            opener,
            url=chat_url,
            method="POST",
            expected=401,
            api_key=INVALID_API_KEY,
            body=json.dumps(
                {**text_payload, "model": text_models[0]}, separators=(",", ":")
            ).encode(),
            content_type="application/json",
            timeout=timeout,
        )

    for text_model in text_models:
        text_response = _post_json(
            opener,
            url=chat_url,
            api_key=api_key,
            payload={**text_payload, "model": text_model},
            timeout=timeout,
        )
        _validate_text_response(text_response, text_model)

    for url, required_models in (
        (models_url, set(SUPPORTED_DECISION_MODELS)),
        (typesafe_models_url, {CLEF_IMAGE_COMPAT_MODEL}),
    ):
        status, response_body, response_type = _request(
            opener,
            url=url,
            method="GET",
            api_key=api_key,
            body=None,
            content_type=None,
            timeout=timeout,
        )
        if status != 200:
            excerpt = response_body[:2_048].decode("utf-8", errors="replace")
            excerpt = excerpt.replace(api_key, "[redacted]")
            raise RuntimeError(f"GET {url} returned HTTP {status}: {excerpt}")
        if response_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RuntimeError(
                f"GET {url} returned non-JSON content type {response_type!r}"
            )
        try:
            model_list = json.loads(response_body)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"GET {url} returned invalid JSON") from error
        data = model_list.get("data") if isinstance(model_list, dict) else None
        model_ids = (
            {
                item.get("id")
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if isinstance(data, list)
            else set()
        )
        missing_models = required_models - model_ids
        if missing_models:
            raise RuntimeError(
                f"GET {url} is missing model IDs: {', '.join(sorted(missing_models))}"
            )

    systemone_response = _post_json(
        opener,
        url=systemone_url,
        api_key=api_key,
        payload=jev_payload,
        timeout=timeout,
    )
    choice, confidence = _validate_jev_response(systemone_response)

    decisions_response = _post_json(
        opener,
        url=decisions_url,
        api_key=api_key,
        payload=decisions_payload,
        timeout=timeout,
    )
    _validate_decisions_response(decisions_response)

    generation_response = _post_json(
        opener,
        url=generation_url,
        api_key=api_key,
        payload=generation_payload,
        timeout=timeout,
    )
    generated = _decode_image_response(generation_response)
    _write_new(generated_path, generated)

    clef_image_payload = {
        "model": CLEF_IMAGE_COMPAT_MODEL,
        "state": "Classify the uploaded picture.",
        "questions": {
            "visual_check": {
                "type": "choice",
                "instructions": (
                    "Does the image show one red cube on a plain white background?"
                ),
                "criteria": {
                    "red_cube": "One centered red cube on a plain white background",
                    "other": "Anything else",
                },
            }
        },
        "images": [
            "data:image/png;base64," + base64.b64encode(generated).decode("ascii")
        ],
    }
    clef_image_response = _post_json(
        opener,
        url=typesafe_url,
        api_key=api_key,
        payload=clef_image_payload,
        timeout=timeout,
    )
    clef_image_choice = _validate_clef_image_response(clef_image_response)

    edit_body, edit_type = _multipart_edit(generated, image_model)
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

    return ValidationResult(
        choice,
        confidence,
        clef_image_choice,
        generated_path,
        edited_path,
        tuple(text_models),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate native decisions, Clef image compatibility, and selected "
            "Qwen Image models through the public LiteLLM gateway."
        )
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument(
        "--image-model",
        choices=SUPPORTED_IMAGE_MODELS,
        action="append",
        help=(
            "image model to test; repeat to test both (default: "
            f"{DEFAULT_IMAGE_MODEL})"
        ),
    )
    parser.add_argument(
        "--text-model",
        choices=SUPPORTED_TEXT_MODELS,
        action="append",
        help="text model to smoke-test; repeat to test model switching",
    )
    args = parser.parse_args()

    api_key = os.environ.get("LITELLM_API_KEY")
    if api_key is None:
        api_key = getpass.getpass("LiteLLM API key: ")
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="litellm-validation-"))
    image_models = args.image_model or [DEFAULT_IMAGE_MODEL]
    if len(set(image_models)) != len(image_models):
        parser.error("--image-model cannot be repeated with duplicate values")
    text_models = tuple(args.text_model or ())
    if len(set(text_models)) != len(text_models):
        parser.error("--text-model cannot be repeated with duplicate values")
    for index, image_model in enumerate(image_models):
        model_output_dir = (
            output_dir / image_model if len(image_models) > 1 else output_dir
        )
        result = validate(
            base_url=args.base_url,
            api_key=api_key,
            output_dir=model_output_dir,
            timeout=args.timeout,
            image_model=image_model,
            text_models=text_models if index == 0 else (),
        )
        text_lines = [f"Text model {model} OK" for model in result.text_models]
        print(
            "\n".join(
                [
                    *text_lines,
                    f"JevK5 OK: choice={result.choice}, confidence={result.confidence:.6f}",
                    f"Clef image compatibility OK: choice={result.clef_image_choice}",
                ]
            )
            + "\n"
            f"Qwen Image ({image_model}) generation OK: {result.generated}\n"
            f"Qwen Image ({image_model}) edit OK: {result.edited}"
        )


if __name__ == "__main__":
    main()
