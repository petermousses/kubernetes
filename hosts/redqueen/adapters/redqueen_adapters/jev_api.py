from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

import aiohttp
from aiohttp import web
from PIL import Image

from .common import auth_middleware, error_response, safe_errors
from .jevk5_gguf import JevK5GGUF


MODEL = "jevk5-4b-v0.3"
MODEL_ALIASES = {MODEL, "jev-latest"}
MAX_QUESTIONS = 32
MAX_CLEF_QUESTIONS = 64
MAX_OPTIONS = 256
MAX_INSTRUCTIONS = 4_000
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
CLEF_MODEL_FAMILIES = {
    "clef-flash-bf16": "clef-flash",
    "clef-flash-q8": "clef-flash",
    "clef-flash-q4": "clef-flash",
    "clef-q4": "clef",
}
IMAGE_DATA_URL = re.compile(
    r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})$"
)
CLEF_QUESTION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


@dataclass(frozen=True)
class JevSettings:
    api_key: str
    llama_url: str = "http://127.0.0.1:8082"
    router_api_key: str = ""
    router_url: str = "http://10.9.20.242:8081"
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("api key must not be empty")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")


class JevBackend(Protocol):
    async def decide(self, state: object, question: dict) -> dict: ...


class LocalJevBackend:
    def __init__(self, settings: JevSettings) -> None:
        self._model = JevK5GGUF(
            url=settings.llama_url, temperature=1.22, knockout_temperature=0.93
        )
        self._slots = asyncio.Semaphore(settings.max_concurrency)

    async def decide(self, state: object, question: dict) -> dict:
        async with self._slots:
            return await asyncio.to_thread(self._model.decide, state, question)

    async def ready(self) -> bool:
        def check() -> bool:
            try:
                with urllib.request.urlopen(
                    f"{self._model.url}/health", timeout=5
                ) as response:
                    return response.status == 200
            except Exception:
                return False

        return await asyncio.to_thread(check)


class ClefBackend(Protocol):
    async def evaluate(self, model: str, payload: dict) -> dict: ...


class RouterClefBackend:
    def __init__(self, settings: JevSettings) -> None:
        if not settings.router_api_key:
            raise ValueError("router api key must not be empty")
        self.url = settings.router_url.rstrip("/")
        self.api_key = settings.router_api_key
        self.timeout = aiohttp.ClientTimeout(total=900, connect=10)
        self._slot = asyncio.Semaphore(1)

    async def evaluate(self, model: str, payload: dict) -> dict:
        async with self._slot:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.url}/v1/systemone",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as response:
                    body = await response.content.read(8 * 1024 * 1024 + 1)
                    if len(body) > 8 * 1024 * 1024:
                        raise ValueError("Clef upstream response exceeded 8 MiB")
                    if response.status != 200:
                        raise RuntimeError(
                            f"Clef upstream returned HTTP {response.status}"
                        )
                    try:
                        result = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ValueError("Clef upstream returned invalid JSON") from error
        return validate_clef_response(model, payload["questions"], result)


def _instructions(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("question instructions must be a non-empty string")
    if len(value) > MAX_INSTRUCTIONS:
        raise ValueError(f"question instructions exceed {MAX_INSTRUCTIONS} characters")
    return value


def normalize_question(question: object) -> dict:
    if not isinstance(question, dict):
        raise ValueError("each question must be an object")
    unknown = sorted(set(question) - {"type", "instructions", "criteria"})
    if unknown:
        raise ValueError(f"unsupported question fields: {', '.join(unknown)}")
    kind = question.get("type")
    if kind not in {"noul", "choice", "score"}:
        raise ValueError(f"unknown question type {kind!r}")
    normalized = {
        "type": kind,
        "instructions": _instructions(question.get("instructions")),
    }
    criteria = question.get("criteria")
    if kind == "noul":
        if criteria is not None:
            if not isinstance(criteria, dict) or set(criteria) - {"true", "false"}:
                raise ValueError("noul criteria may only define true and false")
            if any(
                not isinstance(value, str) or not value for value in criteria.values()
            ):
                raise ValueError("noul criteria values must be non-empty strings")
            normalized["criteria"] = criteria
    elif kind == "choice":
        if isinstance(criteria, list):
            if any(not isinstance(value, str) or not value for value in criteria):
                raise ValueError("choice criteria list must contain non-empty strings")
            criteria = dict.fromkeys(criteria)
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_OPTIONS:
            raise ValueError(f"choice criteria must name 2-{MAX_OPTIONS} options")
        if any(
            not isinstance(key, str)
            or not key
            or value is not None
            and (not isinstance(value, str) or not value)
            for key, value in criteria.items()
        ):
            raise ValueError(
                "choice option ids and descriptions must be non-empty strings"
            )
        normalized["criteria"] = criteria
    else:
        if (
            not isinstance(criteria, list)
            or not 2 <= len(criteria) <= MAX_OPTIONS
            or any(not isinstance(value, str) or not value for value in criteria)
        ):
            raise ValueError(
                f"score criteria must list 2-{MAX_OPTIONS} non-empty levels"
            )
        normalized["criteria"] = criteria
    return normalized


def normalize_request(
    payload: object,
    *,
    model_aliases: set[str] = MODEL_ALIASES,
    max_questions: int = MAX_QUESTIONS,
    max_question_id_length: int = 128,
) -> tuple[object, dict[str, dict]]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    unknown = sorted(set(payload) - {"model", "state", "questions"})
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    if payload.get("model", MODEL) not in model_aliases:
        raise ValueError(f"model must be one of {', '.join(sorted(model_aliases))}")
    if "state" not in payload:
        raise ValueError("state is required")
    state = payload["state"]
    try:
        encoded_state = json.dumps(state, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("state must contain finite JSON values") from error
    if len(encoded_state.encode()) > 512 * 1024:
        raise ValueError("state exceeds 512 KiB")
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= max_questions:
        raise ValueError(f"questions must contain 1-{max_questions} entries")
    normalized: dict[str, dict] = {}
    for question_id, question in questions.items():
        if (
            not isinstance(question_id, str)
            or not question_id
            or len(question_id) > max_question_id_length
        ):
            raise ValueError(
                f"question ids must be 1-{max_question_id_length} character strings"
            )
        normalized[question_id] = normalize_question(question)
    return state, normalized


def normalize_clef_request(payload: object, model: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    unknown = sorted(set(payload) - {"model", "state", "questions", "images"})
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    if "state" not in payload:
        raise ValueError("state is required")
    family = CLEF_MODEL_FAMILIES[model]
    if payload.get("model", model) not in {model, family}:
        raise ValueError(f"model must be {model!r} or {family!r} for this route")
    state, questions = normalize_request(
        {
            "model": model,
            "state": payload.get("state"),
            "questions": payload.get("questions"),
        },
        model_aliases={model},
        max_questions=MAX_CLEF_QUESTIONS,
        max_question_id_length=100,
    )
    if any(not CLEF_QUESTION_ID.fullmatch(question_id) for question_id in questions):
        raise ValueError("question ids may contain only letters, digits, '_', '.' and '-'")
    normalized = {"model": model, "state": state, "questions": questions}
    if "images" in payload and payload["images"] is not None:
        normalized["images"] = normalize_images(payload["images"])
    return normalized


def normalize_images(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("images must contain at most four embedded images")
    images: list[str] = []
    total_bytes = 0
    for image_data_url in value:
        if not isinstance(image_data_url, str):
            raise ValueError("each image must be a base64 data URL")
        match = IMAGE_DATA_URL.fullmatch(image_data_url)
        if match is None:
            raise ValueError("images must be PNG, JPEG, or WebP base64 data URLs")
        encoded = match.group(2)
        if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("each image must be at most 4 MiB decoded")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("each image must contain valid base64 data") from error
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("each image must be non-empty and at most 4 MiB")
        total_bytes += len(image_bytes)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("decoded images must total at most 8 MiB")
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                actual_format = image.format
                width, height = image.size
                image.verify()
        except Exception as error:
            raise ValueError("image data is corrupt or unsupported") from error
        expected_format = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}[
            match.group(1)
        ]
        if actual_format != expected_format:
            raise ValueError("image media type does not match its encoded format")
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("each image must be at most 16 megapixels")
        images.append(image_data_url)
    return images


def validate_clef_response(
    model: str, questions: dict[str, dict], body: object
) -> dict:
    if not isinstance(body, dict) or body.get("model") != model:
        raise ValueError("Clef upstream returned the wrong model")
    answers = body.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("Clef upstream returned the wrong answer ids")
    for question_id, question in questions.items():
        answer = answers[question_id]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise ValueError("Clef upstream returned an invalid answer")
        if question["type"] == "noul":
            noul = answer.get("noul")
            if (
                isinstance(noul, bool)
                or not isinstance(noul, (int, float))
                or not math.isfinite(noul)
                or not 0 <= noul <= 1
            ):
                raise ValueError("Clef upstream returned an invalid noul probability")
        else:
            validate_answer(question, {**answer, "input_tokens": 0})
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("Clef upstream response is missing usage")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens", 0)
    for token_count in (input_tokens, output_tokens):
        if (
            isinstance(token_count, bool)
            or not isinstance(token_count, int)
            or token_count < 0
        ):
            raise ValueError("Clef upstream returned invalid token usage")
    return body


def validate_answer(question: dict, answer: object) -> dict:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        raise ValueError("backend returned the wrong answer type")
    result = dict(answer)
    tokens = result.get("input_tokens")
    confidence = result.get("confidence")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError("backend returned invalid token usage")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("backend returned invalid confidence")
    if question["type"] == "noul":
        noul = result.get("noul")
        if (
            isinstance(noul, bool)
            or not isinstance(noul, (int, float))
            or not math.isfinite(noul)
            or not 0 <= noul <= 1
        ):
            raise ValueError("backend returned an invalid noul probability")
        return result
    expected = (
        set(question["criteria"])
        if question["type"] == "choice"
        else {str(index) for index in range(len(question["criteria"]))}
    )
    probabilities = result.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != expected:
        raise ValueError("backend returned the wrong probability keys")
    values = list(probabilities.values())
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in values
    ) or not math.isclose(sum(values), 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("backend returned invalid probabilities")
    if question["type"] == "choice" and result.get("choice") not in expected:
        raise ValueError("backend returned an invalid choice")
    score = result.get("score")
    if question["type"] == "score" and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= len(expected) - 1
    ):
        raise ValueError("backend returned an invalid score")
    return result


def create_jev_app(
    settings: JevSettings,
    backend: JevBackend | None = None,
    clef_backend: ClefBackend | None = None,
) -> web.Application:
    actual_backend = backend or LocalJevBackend(settings)
    actual_clef_backend = clef_backend or (
        RouterClefBackend(settings) if settings.router_api_key else None
    )
    app = web.Application(
        client_max_size=13 * 1024 * 1024,
        middlewares=[safe_errors, auth_middleware(settings.api_key)],
    )

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "model": MODEL})

    async def ready(_: web.Request) -> web.Response:
        check = getattr(actual_backend, "ready", None)
        if check is not None and not await check():
            return web.json_response({"ok": False}, status=503)
        return web.json_response({"ok": True})

    async def models(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {"id": model, "object": "model"}
                    for model in (MODEL, *CLEF_MODEL_FAMILIES)
                ],
            }
        )

    async def systemone(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            raise ValueError("request body must be valid JSON")
        state, questions = normalize_request(payload)
        started = time.perf_counter()
        try:
            raw_results = await asyncio.gather(
                *(
                    actual_backend.decide(state, question)
                    for question in questions.values()
                )
            )
            results = [
                validate_answer(question, answer)
                for question, answer in zip(
                    questions.values(), raw_results, strict=True
                )
            ]
            answers = dict(zip(questions, results, strict=True))
            tokens = sum(answer.pop("input_tokens") for answer in answers.values())
        except asyncio.CancelledError:
            raise
        except Exception:
            return error_response(502, "JevK5 backend failed", "upstream_error")
        return web.json_response(
            {
                "model": MODEL,
                "answers": answers,
                "usage": {"input_tokens": tokens, "output_tokens": 0},
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            headers={"Cache-Control": "no-store"},
        )

    def clef_handler(model: str):
        async def handle(request: web.Request) -> web.Response:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                raise ValueError("request body must be valid JSON")
            normalized = normalize_clef_request(payload, model)
            if actual_clef_backend is None:
                return error_response(
                    503, "Clef router is not configured", "server_error"
                )
            try:
                response = await actual_clef_backend.evaluate(model, normalized)
            except asyncio.CancelledError:
                raise
            except Exception:
                return error_response(502, "Clef backend failed", "upstream_error")
            return web.json_response(response, headers={"Cache-Control": "no-store"})

        return handle

    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/systemone", systemone)
    for model in CLEF_MODEL_FAMILIES:
        app.router.add_post(f"/{model}/v1/systemone", clef_handler(model))
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="10.9.20.242")
    parser.add_argument("--port", type=int, default=8191)
    args = parser.parse_args()
    api_key = os.environ.get("REDQUEEN_JEV_API_KEY", "")
    if not api_key:
        raise SystemExit("REDQUEEN_JEV_API_KEY is required")
    router_api_key = os.environ.get("QWEN_API_KEY", "")
    if not router_api_key:
        raise SystemExit("QWEN_API_KEY is required for Clef routes")
    web.run_app(
        create_jev_app(
            JevSettings(api_key=api_key, router_api_key=router_api_key)
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
