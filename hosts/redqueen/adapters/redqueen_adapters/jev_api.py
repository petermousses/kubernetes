from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import aiohttp
from aiohttp import web

from .common import auth_middleware, error_response, safe_errors
from .jevk5_gguf import JevK5GGUF


MODEL = "jevk5-4b-v0.3"
MODEL_ALIASES = {MODEL, "jev-latest"}
MAX_QUESTIONS = 32
MAX_OPTIONS = 256
MAX_INSTRUCTIONS = 4_000


@dataclass(frozen=True)
class JevSettings:
    api_key: str
    llama_url: str = "http://127.0.0.1:8082"
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


def normalize_request(payload: object) -> tuple[object, dict[str, dict]]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    unknown = sorted(set(payload) - {"model", "state", "questions"})
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    if payload.get("model", MODEL) not in MODEL_ALIASES:
        raise ValueError(f"model must be one of {', '.join(sorted(MODEL_ALIASES))}")
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
    if not isinstance(questions, dict) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"questions must contain 1-{MAX_QUESTIONS} entries")
    normalized: dict[str, dict] = {}
    for question_id, question in questions.items():
        if (
            not isinstance(question_id, str)
            or not question_id
            or len(question_id) > 128
        ):
            raise ValueError("question ids must be 1-128 character strings")
        normalized[question_id] = normalize_question(question)
    return state, normalized


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
    settings: JevSettings, backend: JevBackend | None = None
) -> web.Application:
    actual_backend = backend or LocalJevBackend(settings)
    app = web.Application(
        client_max_size=1024 * 1024,
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
            {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
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

    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/systemone", systemone)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="10.9.20.242")
    parser.add_argument("--port", type=int, default=8191)
    args = parser.parse_args()
    api_key = os.environ.get("REDQUEEN_JEV_API_KEY", "")
    if not api_key:
        raise SystemExit("REDQUEEN_JEV_API_KEY is required")
    web.run_app(
        create_jev_app(JevSettings(api_key=api_key)), host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
