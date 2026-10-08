from __future__ import annotations

import base64
import importlib.util
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import urlparse


SCRIPT = Path(__file__).resolve().parents[1] / "validate_inference.py"


def png(width: int = 512, height: int = 512) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    scanline = b"\x00" + (b"\xff\x00\x00" * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline * height))
        + chunk(b"IEND", b"")
    )


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.body = json.dumps(payload).encode()
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.body)),
        }

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeOpener:
    api_key = "sk-test-validation-key"
    image = png()

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.image_models: list[str] = []
        self.text_models: list[str] = []

    def open(self, request: object, timeout: float) -> FakeResponse:
        del timeout
        method = request.get_method()
        path = urlparse(request.full_url).path
        authorization = request.get_header("Authorization") or ""
        self.requests.append((method, path, authorization))
        body = request.data or b""
        if method == "GET":
            if path in {"/v1/models", "/typesafe/v1/models"}:
                if authorization != f"Bearer {self.api_key}":
                    return FakeResponse(401, {"error": {"message": "invalid key"}})
                model_ids = (
                    [
                        "jevk5-4b-v0.3",
                        "clef-flash-bf16",
                        "clef-flash-q8",
                        "clef-flash-q4",
                        "clef-q4",
                    ]
                    if path == "/v1/models"
                    else [
                        "jevk5-4b-v0.3",
                        "jev-latest",
                        "jevk5",
                        "clef-flash-bf16",
                        "clef-flash-q8",
                        "clef-flash-q4",
                        "clef-q4",
                        "clef-flash",
                        "clef",
                    ]
                )
                return FakeResponse(
                    200,
                    {"object": "list", "data": [{"id": model} for model in model_ids]},
                )
            return FakeResponse(404, {"detail": "Not Found"})
        if authorization != f"Bearer {self.api_key}":
            return FakeResponse(401, {"error": {"message": "invalid key"}})
        if path == "/v1/systemone":
            payload = json.loads(body)
            if payload.get("model") != "jevk5-4b-v0.3":
                return FakeResponse(400, {"error": {"message": "wrong model"}})
            return FakeResponse(
                200,
                {
                    "model": "jevk5-4b-v0.3",
                    "answers": {
                        "route": {
                            "type": "choice",
                            "choice": "misdelivered",
                            "probabilities": {
                                "delivered": 0.01,
                                "delayed": 0.02,
                                "misdelivered": 0.97,
                            },
                            "confidence": 0.97,
                        }
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 0},
                },
            )
        if path == "/v1/decisions":
            payload = json.loads(body)
            if payload.get("model") != "jevk5-4b-v0.3":
                return FakeResponse(400, {"error": {"message": "wrong model"}})
            return FakeResponse(
                200,
                {
                    "model": "jevk5-4b-v0.3",
                    "answers": [
                        {
                            "type": "choice",
                            "name": "route",
                            "choice": "misdelivered",
                            "probabilities": [
                                {"value": "delivered", "probability": 0.01},
                                {"value": "delayed", "probability": 0.02},
                                {"value": "misdelivered", "probability": 0.97},
                            ],
                            "confidence": 0.97,
                        }
                    ],
                    "usage": {"input_tokens": 42, "output_tokens": 0},
                },
            )
        if path == "/typesafe/v1/systemone":
            payload = json.loads(body)
            expected_image = (
                "data:image/png;base64," + base64.b64encode(self.image).decode()
            )
            if (
                payload.get("model") != "clef-flash-bf16"
                or payload.get("images") != [expected_image]
            ):
                return FakeResponse(400, {"error": {"message": "wrong Clef image"}})
            return FakeResponse(
                200,
                {
                    "model": "clef-flash-bf16",
                    "answers": {
                        "visual_check": {
                            "type": "choice",
                            "choice": "red_cube",
                            "probabilities": {"red_cube": 0.97, "other": 0.03},
                            "confidence": 0.97,
                        }
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 0},
                },
            )
        if path == "/v1/chat/completions":
            payload = json.loads(body)
            model = payload.get("model")
            if model not in {
                "qwen3.8-27b",
                "gemma-4-e4b-it",
                "gemma-4-12b-it",
                "gemma-4-26b-a4b-it",
                "qwen3.6-35b-a3b",
                "glm-5.3-flash-abliterated",
            }:
                return FakeResponse(400, {"error": {"message": "wrong model"}})
            self.text_models.append(model)
            return FakeResponse(
                200,
                {
                    "model": model,
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                },
            )
        if path == "/v1/images/generations":
            payload = json.loads(body)
            self.image_models.append(payload.get("model", ""))
            if payload.get("model") not in {
                "qwen-image-2.1",
                "qwen-image-2.1-uncensored",
            }:
                return FakeResponse(400, {"error": {"message": "wrong model"}})
            if "response_format" in payload:
                return FakeResponse(
                    400,
                    {"error": {"message": "response_format is not valid for GPT Image"}},
                )
            return FakeResponse(
                200,
                {"data": [{"b64_json": base64.b64encode(self.image).decode()}]},
            )
        if path == "/v1/images/edits":
            content_type = request.get_header("Content-type") or ""
            if not content_type.startswith("multipart/form-data;"):
                return FakeResponse(400, {"error": {"message": "not multipart"}})
            if b"filename=\"generated.png\"" not in body or self.image not in body:
                return FakeResponse(400, {"error": {"message": "missing image"}})
            matched_models = [
                model
                for model in (b"qwen-image-2.1", b"qwen-image-2.1-uncensored")
                if b'name="model"\r\n\r\n' + model + b"\r\n" in body
            ]
            if len(matched_models) != 1:
                return FakeResponse(400, {"error": {"message": "wrong model"}})
            self.image_models.append(matched_models[0].decode())
            if b'name="response_format"' in body:
                return FakeResponse(
                    400,
                    {"error": {"message": "response_format is not valid for GPT Image"}},
                )
            return FakeResponse(
                200,
                {"data": [{"b64_json": base64.b64encode(self.image).decode()}]},
            )
        return FakeResponse(404, {"detail": "Not Found"})


class ValidationScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("validate_inference", SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load {SCRIPT}")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_full_public_contract(self) -> None:
        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.module.validate(
                base_url="https://api.example.test",
                api_key=opener.api_key,
                output_dir=Path(temporary_directory),
                timeout=5,
                opener=opener,
            )
            self.assertEqual(result.choice, "misdelivered")
            self.assertEqual(result.clef_image_choice, "red_cube")
            self.assertEqual(result.generated.read_bytes(), opener.image)
            self.assertEqual(result.edited.read_bytes(), opener.image)

        self.assertEqual(
            [(method, path) for method, path, _ in opener.requests],
            [
                ("GET", "/v1/systemone"),
                ("GET", "/v1/decisions"),
                ("GET", "/typesafe/v1/systemone"),
                ("GET", "/typesafe/v1/models"),
                ("POST", "/v1/systemone"),
                ("POST", "/v1/decisions"),
                ("POST", "/typesafe/v1/systemone"),
                ("POST", "/v1/images/generations"),
                ("GET", "/v1/models"),
                ("GET", "/typesafe/v1/models"),
                ("POST", "/v1/systemone"),
                ("POST", "/v1/decisions"),
                ("POST", "/v1/images/generations"),
                ("POST", "/typesafe/v1/systemone"),
                ("POST", "/v1/images/edits"),
            ],
        )

    def test_rejects_invalid_key_before_network_access(self) -> None:
        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "LiteLLM API key"):
                self.module.validate(
                    base_url="https://api.example.test",
                    api_key="contains a space",
                    output_dir=Path(temporary_directory),
                    timeout=5,
                    opener=opener,
                )
        self.assertEqual(opener.requests, [])

    def test_uncensored_alias_is_forwarded_for_generation_and_edit(self) -> None:
        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.module.validate(
                base_url="https://api.example.test",
                api_key=opener.api_key,
                output_dir=Path(temporary_directory),
                timeout=5,
                opener=opener,
                image_model="qwen-image-2.1-uncensored",
            )
            self.assertEqual(
                result.generated.name, "qwen-image-2.1-uncensored-generated.png"
            )
            self.assertEqual(
                result.edited.name, "qwen-image-2.1-uncensored-edited.png"
            )
        self.assertEqual(
            opener.image_models,
            ["qwen-image-2.1-uncensored", "qwen-image-2.1-uncensored"],
        )

    def test_rejects_unknown_image_model_before_network_access(self) -> None:
        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "supported image model"):
                self.module.validate(
                    base_url="https://api.example.test",
                    api_key=opener.api_key,
                    output_dir=Path(temporary_directory),
                    timeout=5,
                    opener=opener,
                    image_model="arbitrary",
                )
        self.assertEqual(opener.requests, [])

    def test_text_model_alias_is_forwarded_and_response_is_checked(self) -> None:
        opener = FakeOpener()
        models = (
            "gemma-4-e4b-it",
            "qwen3.6-35b-a3b",
            "glm-5.3-flash-abliterated",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.module.validate(
                base_url="https://api.example.test",
                api_key=opener.api_key,
                output_dir=Path(temporary_directory),
                timeout=5,
                opener=opener,
                text_models=models,
            )
        self.assertEqual(result.text_models, models)
        self.assertEqual(opener.text_models, list(models))

    def test_rejects_unknown_text_model_before_network_access(self) -> None:
        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "supported text model"):
                self.module.validate(
                    base_url="https://api.example.test",
                    api_key=opener.api_key,
                    output_dir=Path(temporary_directory),
                    timeout=5,
                    opener=opener,
                    text_models=("unregistered-model",),
                )
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
