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

    def open(self, request: object, timeout: float) -> FakeResponse:
        del timeout
        method = request.get_method()
        path = urlparse(request.full_url).path
        authorization = request.get_header("Authorization") or ""
        self.requests.append((method, path, authorization))
        body = request.data or b""
        if method == "GET":
            return FakeResponse(404, {"detail": "Not Found"})
        if authorization != f"Bearer {self.api_key}":
            return FakeResponse(401, {"error": {"message": "invalid key"}})
        if path == "/typesafe/v1/systemone":
            payload = json.loads(body)
            if payload.get("model") != "jev-latest":
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
        if path == "/v1/images/generations":
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
            self.assertEqual(result.generated.read_bytes(), opener.image)
            self.assertEqual(result.edited.read_bytes(), opener.image)

        self.assertEqual(
            [(method, path) for method, path, _ in opener.requests],
            [
                ("GET", "/typesafe/v1/systemone"),
                ("POST", "/typesafe/v1/systemone"),
                ("POST", "/v1/images/generations"),
                ("POST", "/typesafe/v1/systemone"),
                ("POST", "/v1/images/generations"),
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


if __name__ == "__main__":
    unittest.main()
