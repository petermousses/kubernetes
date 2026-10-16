"""Loopback contracts against the deployed LiteLLM/provider release, no cluster."""
import asyncio
import contextlib
import importlib.metadata
import json
import os
import re
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_LOG"] = "CRITICAL"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
import httpx
import litellm
import yaml
from fastapi import FastAPI, Request
from litellm.proxy import proxy_server
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.types_utils.utils import get_instance_fn
from redqueen_reliability import (
    INFERENCE_TIMEOUT, JSONStructure, LimitedStream, REQUEST_STATE, RedqueenErrors,
    enforce_policy, is_redqueen, reliability, safe_failure, safe_retry_after,
)


MODEL = "qwen3.8-27b"


class GatewayContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertEqual(importlib.metadata.version("litellm"), "1.104.2")
        self.assertEqual(importlib.metadata.version("openai"), "2.33.0")
        self.assertEqual(importlib.metadata.version("httpx"), "0.28.1")
        self.attempts = 0
        self.status = 200
        self.retry_after = "1"
        self.delay = 0
        self.release = None
        self.disconnected = asyncio.Event()
        self.requests = []
        self.received_bodies = []
        self.mode = "chat"

        async def backend(request):
            self.attempts += 1
            self.requests.append(request)
            self.received_bodies.append(await request.read())
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.release is not None:
                await self.release.wait()
                if request.transport is None or request.transport.is_closing():
                    self.disconnected.set()
                    return web.Response()
            if request.transport is None or request.transport.is_closing():
                self.disconnected.set()
                return web.Response()
            if self.mode == "drop":
                request.transport.close()
                return web.Response()
            if self.status != 200:
                return web.json_response({"error": {"message": "secret prompt and private key",
                    "type": "server_error", "param": None,
                    "code": "model_busy" if self.status == 429 else "capacity_unavailable"}},
                    status=self.status, headers={"Retry-After": self.retry_after, "x-request-id": "backend-contract-id"})
            if self.mode in ("stream", "stream-error"):
                response = web.StreamResponse(headers={"content-type": "text/event-stream"})
                await response.prepare(request)
                try:
                    for text in ("hello", " world"):
                        payload = {"id": "chatcmpl-contract", "object": "chat.completion.chunk", "created": 1,
                                   "model": MODEL, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                        # Deliberately fragment a valid event, including its UTF8
                        # and frame delimiters, through real network transport.
                        frame = ("data: " + json.dumps(payload) + "\n\n").encode()
                        await response.write(frame[:17]); await response.write(frame[17:])
                        await asyncio.sleep(0.06)
                        if self.mode == "stream-error":
                            await response.write(b'data: {"error":{"message":"secret prompt", "type":"server_error","code":"backend_unready"}}\n\n')
                            return response
                    await response.write(b"data: [DONE]\n\n")
                except (ConnectionResetError, asyncio.CancelledError):
                    self.disconnected.set()
                return response
            if self.mode == "image":
                payload = {"created": 1, "data": [{"b64_json": "cG5n"}]}
            elif self.mode == "embedding":
                payload = {"object": "list", "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
                           "model": "embeddinggemma-2", "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            elif self.mode == "decision":
                payload = {"id": "decision-contract", "object": "decision", "model": "clm-v0.1-8b",
                           "created_at": 1, "answers": [{"name": "yes", "type": "predicate", "probability": 0.9}],
                           "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            else:
                payload = {"id": "chatcmpl-contract", "object": "chat.completion", "created": 1,
                           "model": MODEL, "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            response = web.StreamResponse(headers={"content-type": "application/json", "x-request-id": "backend-contract-id"})
            await response.prepare(request)
            body = b"{invalid" if self.mode == "invalid-json" else b'{"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}' if self.mode == "structural" else '{"x":1}'.encode("utf-16") if self.mode == "utf16" else json.dumps(payload).encode()
            await response.write(body[:11]); await asyncio.sleep(0); await response.write(body[11:])
            await response.write_eof()
            return response

        backend_app = web.Application()
        backend_app.router.add_route("*", "/{rest:.*}", backend)
        self.runner = web.AppRunner(backend_app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"
        models = [MODEL, "embeddinggemma-2", "qwen-image-2.1", "clm-v0.1-8b"]
        proxy_server.llm_router = litellm.Router(model_list=[
            {"model_name": model, "litellm_params": {"model": f"openai/{model}", "api_key": "contract-only",
              "api_base": self.base, "max_retries": 0}} for model in models
        ], num_retries=0, max_fallbacks=0, disable_cooldowns=True)
        proxy_server.general_settings = {}
        proxy_server.user_request_timeout = None
        proxy_server.user_model = None
        loaded_callback = get_instance_fn("redqueen_callbacks.reliability", config_file_path=str(Path(__file__).resolve().parents[1] / "config.yaml"))
        self.assertIs(loaded_callback, reliability)
        litellm.callbacks = [loaded_callback]
        litellm.num_retries = 0
        litellm.sse_keepalive_ping_interval_seconds = 0.02
        self.keepalive_minimum = patch.object(proxy_server, "_KEEPALIVE_MIN_SECONDS", 0.01)
        self.keepalive_minimum.start()
        litellm.request_timeout = 0.025  # Deliberately hostile old/default cutoff.
        litellm.aclient_session = None

        async def auth():
            return UserAPIKeyAuth(api_key="contract-only", models=models)
        proxy_server.app.dependency_overrides[user_api_key_auth] = auth
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=RedqueenErrors(proxy_server.app)),
                                        base_url="http://contract", timeout=None)

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.release is not None:
            self.release.set()
        await self.runner.cleanup()
        if litellm.aclient_session is not None:
            await litellm.aclient_session.aclose()
        proxy_server.app.dependency_overrides.clear()
        self.keepalive_minimum.stop()
        litellm.callbacks = []
        # Cached SDK clients otherwise retain clients from a closed test loop.
        litellm.in_memory_llm_clients_cache.flush_cache()

    def body(self, **extra):
        return {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], **extra}

    async def test_single_attempt_status_code_headers_and_redaction(self):
        for status in (429, 503, 504):
            with self.subTest(status=status):
                self.status, self.attempts = status, 0
                response = await self.client.post("/v1/chat/completions", json=self.body(
                    num_retries=5, max_retries=5, max_fallbacks=5,
                    fallbacks=[{"qwen3.8-27b": ["qwen3.8-27b"]}],
                    timeout=0.001, request_timeout=0.001, stream_timeout=0.001,
                    retry_policy={"InternalServerErrorRetries": 4}))
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.attempts, 1)
                self.assertEqual(response.json()["error"]["code"], "model_busy" if status == 429 else "capacity_unavailable")
                self.assertEqual(response.headers["x-request-id"], "backend-contract-id")
                self.assertEqual(response.headers.get("retry-after"), "1" if status in (429, 503) else None)
                self.assertNotIn("secret", response.text)
                self.assertNotIn("private key", response.text)

    async def test_retry_after_http_date_preserved_exactly(self):
        self.status = 503
        self.retry_after = "Wed, 21 Oct 2037 07:28:00 GMT"
        response = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("retry-after"), self.retry_after)
        self.assertEqual(self.attempts, 1)

    async def test_nonstream_waits_beyond_former_deadline_and_fragmented_json(self):
        self.delay = 0.08
        response = await self.client.post("/v1/chat/completions", json=self.body(timeout=0.01))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "hello")
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(self.attempts, 1)
        self.assertEqual(response.headers["x-request-id"], "backend-contract-id")

    async def test_quiet_ttft_midstream_keepalives_and_fragmented_sse(self):
        self.delay, self.mode = 0.08, "stream"
        response = await self.client.post("/v1/chat/completions", json=self.body(stream=True, stream_timeout=0.001))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(": ping", response.text)
        self.assertGreaterEqual(response.text.count(": ping"), 3)
        self.assertIn("hello", response.text)
        self.assertIn("world", response.text)
        first_token, second_token = response.text.index("hello"), response.text.index("world")
        self.assertIn(": ping", response.text[first_token:second_token])
        self.assertIn("data: [DONE]", response.text)
        self.assertEqual(self.attempts, 1)

    async def test_late_stream_failure_is_sanitized_structured_error(self):
        self.delay, self.status = 0.08, 503
        response = await self.client.post("/v1/chat/completions", json=self.body(stream=True))
        self.assertEqual(response.status_code, 200)  # Headers already committed by heartbeat.
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        self.assertEqual(frames[-1]["error"]["code"], "capacity_unavailable")
        self.assertNotIn("secret", response.text)
        self.assertEqual(self.attempts, 1)

    async def test_image_and_embedding_wait_without_default_deadline(self):
        self.delay, self.mode = 0.08, "image"
        response = await self.client.post("/v1/images/generations", json={"model": "qwen-image-2.1", "prompt": "hello", "timeout": 0.001})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "cG5n")
        self.mode = "embedding"
        response = await self.client.post("/v1/embeddings", json={"model": "embeddinggemma-2", "input": "hello", "timeout": 0.001})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["embedding"], [0.1, 0.2])

    async def test_image_error_preserves_headers_despite_legacy_hook_ignoring_return(self):
        self.status = 503
        response = await self.client.post("/v1/images/generations", json={"model": "qwen-image-2.1", "prompt": "hello", "max_retries": 5})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "capacity_unavailable")
        self.assertEqual(response.headers.get("retry-after"), "1")
        self.assertEqual(self.attempts, 1)

    async def test_multipart_image_edit_waits_and_never_retries(self):
        self.mode, self.delay = "image", 0.08
        data = {"model": "qwen-image-2.1", "prompt": "hello", "timeout": "0.001", "max_retries": "5"}
        files = {"image": ("input.png", b"fixture-not-decoded-by-gateway", "image/png")}
        response = await self.client.post("/v1/images/edits", data=data, files=files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"][0]["b64_json"], "cG5n")
        self.status, self.attempts = 503, 0
        response = await self.client.post("/v1/images/edits", data=data, files=files)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "capacity_unavailable")
        self.assertEqual(response.headers.get("retry-after"), "1")
        self.assertEqual(self.attempts, 1)

    async def test_decision_waits_and_fragmented_response_and_capacity_error(self):
        self.delay, self.mode = 0.08, "decision"
        body = {"model": "clm-v0.1-8b", "input": "hello", "questions": [{"type": "predicate", "name": "yes", "instructions": "is this a greeting?"}], "timeout": 0.001}
        response = await self.client.post("/v1/decisions", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["answers"][0]["probability"], 0.9)
        self.status, self.attempts = 503, 0
        response = await self.client.post("/v1/decisions", json=body)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "capacity_unavailable")
        self.assertEqual(response.headers.get("retry-after"), "1")
        self.assertEqual(self.attempts, 1)

    async def test_embedding_provider_cannot_restore_retries(self):
        self.mode, self.status = "embedding", 503
        response = await self.client.post("/v1/embeddings", json={"model": "embeddinggemma-2", "input": "hello", "max_retries": 5})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.attempts, 1)

    async def test_responses_generic_route_waits_and_returns_original_error(self):
        self.delay, self.status = 0.08, 503
        response = await self.client.post("/v1/responses", json={"model": MODEL, "input": "hello", "timeout": 0.001, "num_retries": 4})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "capacity_unavailable")
        self.assertEqual(self.attempts, 1)

    async def test_sdk_connection_failure_is_not_retried(self):
        self.mode = "drop"
        response = await self.client.post("/v1/images/generations", json={"model": "qwen-image-2.1", "prompt": "hello"})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.attempts, 1)

    async def test_malformed_upstream_response_is_502(self):
        self.mode = "invalid-json"
        response = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_upstream_response")

    async def test_bounded_upstream_response(self):
        with patch("redqueen_reliability.MAX_JSON_BYTES", 32):
            response = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.attempts, 1)

    async def test_late_midstream_upstream_error_is_redacted(self):
        self.mode = "stream-error"
        response = await self.client.post("/v1/chat/completions", json=self.body(stream=True))
        self.assertEqual(response.status_code, 200)
        self.assertIn("hello", response.text)
        self.assertIn('"error"', response.text)
        self.assertNotIn("secret", response.text)
        self.assertEqual(self.attempts, 1)

    async def test_legacy_typesafe_waits_without_injecting_policy_into_wire_body(self):
        self.delay = 0.08
        proxy_server.general_settings["pass_through_request_timeout"] = 0.025
        with patch.dict(os.environ, {"TYPESAFE_API_BASE": self.base.removesuffix("/v1"), "TYPESAFE_API_KEY": "contract-only"}):
            body = {"model": "clm-v0.1-8b", "state": "hello", "questions": {"yes": {"type": "noul", "instructions": "greeting?"}}, "timeout": 0.001}
            response = await self.client.post("/typesafe/v1/systemone", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            sent = json.loads(self.received_bodies[-1])
            self.assertNotIn("timeout", sent)
            self.assertNotIn("max_retries", sent)
            self.status, self.attempts = 503, 0
            response = await self.client.post("/typesafe/v1/systemone", json=body)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["error"]["code"], "capacity_unavailable")
            self.assertEqual(response.headers.get("retry-after"), "1")
            self.assertEqual(self.attempts, 1)

    async def test_nonstream_image_disconnect_cancels_upstream(self):
        self.release = asyncio.Event()
        self.mode = "image"
        body = json.dumps({"model": "qwen-image-2.1", "prompt": "hello"}).encode()
        gone = asyncio.Event()
        supplied = False

        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        output = []
        async def send(message):
            output.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/v1/images/generations", "raw_path": b"/v1/images/generations",
                 "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1234), "server": ("contract", 80)}
        task = asyncio.create_task(RedqueenErrors(proxy_server.app)(scope, receive, send))
        for _ in range(100):
            if self.attempts:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.attempts, 1)
        gone.set()
        await asyncio.wait_for(task, 1)  # Test guard only, no production deadline.
        self.assertTrue(self.requests[-1].transport is None or self.requests[-1].transport.is_closing())
        self.assertFalse(output)
        self.release.set()

    async def test_framework_errors_use_safe_json_envelope(self):
        for path, body, expected in (("/v1/not-real", "{}", 404), ("/v1/chat/completions", "not-json", 400)):
            response = await self.client.post(path, content=body, headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, expected)
            self.assertIsInstance(response.json()["error"]["code"], str)
            self.assertIn("x-request-id", response.headers)

    async def test_native_authentication_failure_remains_401_and_is_redacted(self):
        proxy_server.app.dependency_overrides.pop(user_api_key_auth)
        previous = proxy_server.master_key
        proxy_server.master_key = "contract-only-master-key"
        try:
            # No DB exists in this fixture. Keep the real auth path while
            # bypassing only Prisma's generated database-error classifier.
            with patch("litellm.proxy.db.exception_handler.PrismaDBExceptionHandler.is_database_service_unavailable_error_in_chain", return_value=False), patch("litellm.proxy.db.exception_handler.PrismaDBExceptionHandler.is_database_service_unavailable_error", return_value=False):
                response = await self.client.get("/v1/models")
        finally:
            proxy_server.master_key = previous
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["code"], "authentication_error")
        self.assertIn("x-request-id", response.headers)
        self.assertEqual(self.attempts, 0)

    async def test_unknown_model_is_404(self):
        response = await self.client.post("/v1/chat/completions", json=self.body(model="not-a-configured-model"))
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertEqual(self.attempts, 0)

    async def test_malformed_root_body_fuzz_is_400_without_backend_attempt(self):
        for body in ("null", "[]", "true", '"hello"', "123", "{", "{\"model\":", "[[]]", "\ud800"):
            response = await self.client.post("/v1/images/generations", content=body.encode(errors="surrogatepass"))
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.attempts, 0)

    async def test_malformed_request_options_are_400_before_backend(self):
        for field, value in (
            ("extra_headers", "not-an-object"), ("extra_headers", {"x-unsafe": []}),
            ("extra_headers", {"Bad Name": "value"}), ("extra_headers", {"x-test": "café"}),
            ("extra_headers", {"x-test": "bad\r\nsecret: value"}),
            ("extra_headers", {"x-test": "bad\x00value"}),
            ("extra_headers", {"x-test": " leading"}), ("extra_body", []),
        ):
            response = await self.client.post("/v1/chat/completions", json=self.body(**{field: value}))
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.attempts, 0)

    async def test_caller_request_id_is_replaced_without_duplicate_headers(self):
        response = await self.client.post("/v1/chat/completions", headers={"x-request-id": "request-contract"}, json=self.body(extra_headers={"X-Request-ID": "caller-id", "x-request-id": "duplicate-id"}))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.requests[-1].headers.getall("x-request-id", []), ["request-contract"])

    async def test_request_json_encoding_is_strict_utf8_before_backend(self):
        invalid = [
            json.dumps(self.body()).encode("utf-16"),
            b'{"model":"qwen3.8-27b","messages":[],"value":"\\ud800"}',
            b'{"model":"qwen3.8-27b","messages":[],"value":NaN}',
            b'{"model":"qwen3.8-27b","messages":[],"value":"x\xff"}',
        ]
        for body in invalid:
            response = await self.client.post("/v1/chat/completions", content=body)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.attempts, 0)

    async def test_upstream_utf16_json_is_502(self):
        self.mode = "utf16"
        response = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_upstream_response")
        self.assertEqual(self.attempts, 1)

    async def test_upload_cap_is_413_without_backend_attempt(self):
        with patch("redqueen_reliability.MAX_REQUEST_BYTES", 32):
            response = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.attempts, 0)

    async def test_hostile_json_structure_rejected_before_decode_and_backend(self):
        body = b'{"model":"qwen3.8-27b","messages":[' + b'{},' * 125_000 + b'{}]}'
        response = await self.client.post("/v1/chat/completions", content=body)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.attempts, 0)

    async def test_upstream_hostile_json_structure_is_502(self):
        self.mode = "structural"
        response = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.attempts, 1)


class PolicyTests(unittest.TestCase):
    def test_retry_after_rejects_unbounded_unsafe_or_timezone_free_values(self):
        for value in ("1\r\nx-secret: value", "1.5", "-1", "x" * 81, "Wed, 21 Oct 2037 07:28:00", b"\xff"):
            self.assertIsNone(safe_retry_after(value))
        self.assertEqual(safe_retry_after("60"), "60")
    def test_config_no_inference_deadlines_and_router_retries(self):
        cm = yaml.safe_load((Path(__file__).resolve().parents[1] / "configmap.yaml").read_text())
        config = yaml.safe_load(cm["data"]["config.yaml"])
        self.assertNotIn("request_timeout", config["litellm_settings"])
        self.assertEqual(config["router_settings"]["num_retries"], 0)
        self.assertTrue(config["router_settings"]["disable_cooldowns"])
        for model in config["model_list"]:
            self.assertNotIn("timeout", model["litellm_params"])
            self.assertEqual(model["litellm_params"]["max_retries"], 0)

    def test_policy_removes_nested_caller_controls(self):
        result = enforce_policy({"model": MODEL, "extra_body": {"timeout": 0.1, "num_retries": 99, "thinking": True}, "force_timeout": 0.1})
        self.assertEqual(result["extra_body"], {"thinking": True})
        self.assertIsNone(result["timeout"].read)
        self.assertEqual(result["timeout"].connect, 10)
        self.assertEqual(result["num_retries"], 0)
        self.assertTrue(result["disable_fallbacks"])

    def test_ingress_admission_precedes_buffering_on_every_route(self):
        root = Path(__file__).resolve().parents[1]
        ingress = yaml.safe_load((root / "ingress.yaml").read_text())
        routes = ingress["spec"]["routes"]
        self.assertEqual(len(routes), 1, "the 16 request limit must share one Traefik router")
        names = [middleware["name"] for middleware in routes[0]["middlewares"]]
        self.assertLess(names.index("inference-admission"), names.index("request-limits"))
        expression = re.sub(r"(Host|Method|PathPrefix|Path)\(`([^`]*)`\)", r"\1('\2')", routes[0]["match"])
        expression = expression.replace("&&", " and ").replace("||", " or ")
        self.assertNotIn("PathPrefix(`/v1/responses`)", routes[0]["match"])

        def matches(host, method, path):
            namespace = {
                "Host": lambda expected: host == expected,
                "Method": lambda expected: method == expected,
                "Path": lambda expected: path == expected,
                "PathPrefix": lambda expected: path.startswith(expected),
            }
            return eval(expression, {"__builtins__": {}}, namespace)

        for method, path in (
            ("GET", "/v1/responses"), ("POST", "/v1/responses/resp/cancel"),
            ("POST", "/v1/chat/completions"), ("POST", "/v1/chat/completions/input_tokens"),
            ("POST", "/v1/images/edits"), ("GET", "/v1/models/qwen3.8-27b"),
            ("POST", "/typesafe/v1/systemone"), ("GET", "/typesafe/v1/models"),
        ):
            self.assertTrue(matches("api.ai.omv.mousses.xyz", method, path), (method, path))
        for host, method, path in (
            ("other.example", "POST", "/v1/chat/completions"),
            ("api.ai.omv.mousses.xyz", "GET", "/v1/chat/completions"),
            ("api.ai.omv.mousses.xyz", "POST", "/v1/models"),
            ("api.ai.omv.mousses.xyz", "PUT", "/v1/images/edits"),
            ("api.ai.omv.mousses.xyz", "GET", "/v1/responsesx"),
            ("api.ai.omv.mousses.xyz", "POST", "/typesafe/v1/models"),
        ):
            self.assertFalse(matches(host, method, path), (host, method, path))
        policies = list(yaml.safe_load_all((root / "request-limits.yaml").read_text()))
        self.assertEqual(policies[0]["spec"]["inFlightReq"]["amount"], 16)

    def test_structure_scanner_ignores_fragmented_escaped_string_contents(self):
        body = json.dumps({"text": '[{\\\"}]' * 100}).encode()
        scanner = JSONStructure()
        for byte in body:
            scanner.feed(bytes([byte]))
        self.assertEqual(scanner.depth, 0)
        self.assertFalse(scanner.in_string)
        self.assertLess(scanner.markers, 10)


class ByteLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_failure_inside_real_fastapi_middleware_is_json500(self):
        app = FastAPI()
        app.add_middleware(RedqueenErrors)
        @app.post("/v1/failure")
        async def failure():
            raise RuntimeError("secret private prompt")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            with self.assertLogs("redqueen.gateway", level="ERROR") as logs:
                response = await client.post("/v1/failure", json={})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("secret", response.text)
        self.assertNotIn("secret", "".join(logs.output))

    async def test_bodyless_response_cancel_is_admitted_and_keeps_redqueen_error_code(self):
        app = FastAPI()

        @app.post("/v1/responses/{response_id}/cancel")
        async def cancel(request: Request):
            state = REQUEST_STATE.get()
            self.assertTrue(state["redqueen_route"])
            self.assertTrue(is_redqueen({}))
            request_id = state["request_id"]
            upstream = httpx.Response(503, headers={"retry-after": "1"}, json={"error": {"code": "capacity_unavailable"}})
            exception = httpx.HTTPStatusError("capacity", request=httpx.Request("POST", "http://upstream/cancel"), response=upstream)
            status, error, headers = safe_failure(exception, request_id)
            return JSONResponse({"error": error}, status_code=status, headers=headers)

        from fastapi.responses import JSONResponse
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=RedqueenErrors(app)), base_url="http://fixture") as client:
            for content in (b"", b"{}"):
                response = await client.post("/v1/responses/resp_contract/cancel", content=content)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "capacity_unavailable")
                self.assertEqual(response.headers.get("retry-after"), "1")

    async def test_admission_rejects_before_reading_or_parsing_upload(self):
        entered, release = asyncio.Event(), asyncio.Event()
        reads = 0
        async def app(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        async def receive():
            nonlocal reads
            reads += 1
            await release.wait()
            return {"type": "http.disconnect"}
        sent = []
        async def send(message):
            sent.append(message)
        middleware = RedqueenErrors(app)
        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
        with patch("redqueen_reliability.MAX_IN_FLIGHT", 1):
            first = asyncio.create_task(middleware(scope, receive, send))
            await entered.wait()
            before = reads
            await middleware(scope, receive, send)
            self.assertEqual(reads, before)
            self.assertEqual(sent[0]["status"], 429)
            self.assertEqual(dict(sent[0]["headers"])[b"retry-after"], b"1")
            release.set()
            await first
        self.assertEqual(middleware.active, 0)

    async def test_upload_timeout_ends_at_eof_and_does_not_limit_inference(self):
        async def app(scope, receive, send):
            await receive()
            await asyncio.sleep(0.04)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        eof = False
        finished = asyncio.Event()
        async def receive():
            nonlocal eof
            if not eof:
                eof = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            await finished.wait()
            return {"type": "http.disconnect"}
        sent = []
        async def send(message):
            sent.append(message)
        with patch("redqueen_reliability.UPLOAD_TIMEOUT", 0.01):
            await RedqueenErrors(app)({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive, send)
        self.assertEqual(sent[0]["status"], 200)

    async def test_incomplete_upload_times_out_without_backend_work(self):
        called = False
        async def app(scope, receive, send):
            nonlocal called
            called = True
            await receive()
            await receive()
        supplied = False
        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": b"{", "more_body": True}
            await asyncio.Event().wait()
        sent = []
        async def send(message):
            sent.append(message)
        middleware = RedqueenErrors(app)
        with patch("redqueen_reliability.UPLOAD_TIMEOUT", 0.01):
            await middleware({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive, send)
        self.assertTrue(called)
        self.assertEqual(sent[0]["status"], 408)
        self.assertEqual(middleware.active, 0)

    async def test_sse_limit_resets_at_fragmented_frame_boundaries(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in (b"data: 1\n", b"\ndata: ", b"2\n\n", b"data: 3\n\n"):
                    yield chunk
            async def aclose(self):
                pass
        actual = b"".join([chunk async for chunk in LimitedStream(Stream(), 10, sse=True)])
        self.assertEqual(actual, b"data: 1\n\ndata: 2\n\ndata: 3\n\n")

    async def test_sse_oversized_fragmented_frame_closes_stream(self):
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                for _ in range(100):
                    yield b"abcdefgh"
            async def aclose(self):
                self.closed = True
        source = Stream()
        with self.assertRaises(httpx.DecodingError):
            async for chunk in LimitedStream(source, 32, sse=True):
                pass
        self.assertTrue(source.closed)


if __name__ == "__main__":
    unittest.main()
