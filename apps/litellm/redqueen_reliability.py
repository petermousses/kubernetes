"""Pinned LiteLLM policy: one attempt, unlimited inference time, bounded bytes.

The CLI entrypoint installs the ASGI boundary before LiteLLM starts. Hooks use
a request-local mutable record because LiteLLM runs provider calls in child
tasks; ContextVar assignments inside those tasks do not flow back to ASGI.
"""

import asyncio
import contextvars
import contextlib
import json
import logging
from email.utils import parsedate_to_datetime
import re
import uuid

import httpx
import litellm
from openai import AsyncOpenAI, APIConnectionError
from pydantic import ValidationError
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

INFERENCE_TIMEOUT = httpx.Timeout(None, connect=10, write=30, pool=10)
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_SSE_FRAME_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 44 * 1024 * 1024
MAX_IN_FLIGHT = 4
MAX_JSON_DEPTH = 64
MAX_JSON_MARKERS = 250_000
UPLOAD_TIMEOUT = 60
REQUEST_STATE = contextvars.ContextVar("redqueen_request_state", default=None)
MODELS = frozenset({
    "qwen3.8-27b", "gemma-4-e4b-it", "gemma-4-12b-it",
    "gemma-4-26b-a4b-it", "qwen3.6-35b-a3b", "glm-5.3-flash-abliterated",
    "embeddinggemma-2", "qwen-image-2.1", "qwen-image-2.1-uncensored",
    "jevk5-4b-v0.3", "clm-v0.1-8b", "clef-flash-bf16", "clef-flash-q8",
    "clef-flash-q4", "clef-flash", "clef-q4",
})
POLICY_KEYS = frozenset({
    "timeout", "request_timeout", "stream_timeout", "force_timeout",
    "num_retries", "max_retries", "max_fallbacks", "fallbacks",
    "context_window_fallbacks", "content_policy_fallbacks", "disable_fallbacks",
    "retry_policy", "model_group_retry_policy", "keepalive_seconds",
    "allow_client_keepalive_override",
})
ERROR_DEFAULTS = {
    400: ("invalid_request", "invalid request", "invalid_request_error"),
    401: ("authentication_error", "invalid credentials", "authentication_error"),
    403: ("permission_denied", "access denied", "permission_error"),
    404: ("not_found", "unknown model or route", "invalid_request_error"),
    405: ("method_not_allowed", "unsupported method", "invalid_request_error"),
    408: ("request_body_timeout", "request upload timed out", "invalid_request_error"),
    413: ("request_too_large", "request exceeds size limit", "invalid_request_error"),
    429: ("model_busy", "model or request limit reached", "rate_limit_error"),
    500: ("internal_error", "internal server error", "server_error"),
    502: ("invalid_upstream_response", "invalid upstream response", "server_error"),
    503: ("backend_unavailable", "inference backend unavailable", "server_error"),
    504: ("upstream_transport_timeout", "upstream connection or upload timed out", "server_error"),
}
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
JSON_TOKENS = re.compile(rb'\\.|["{}\[\],:]|\\$')
LOGGER = logging.getLogger("redqueen.gateway")


def safe_retry_after(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or any(not 32 <= ord(char) <= 126 for char in value):
        return None
    if value.strip().isdecimal():
        return value
    try:
        date = parsedate_to_datetime(value)
        return value if date.tzinfo is not None and date.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


class JSONStructure:
    """Reject hostile object counts/depth before any JSON/Unicode expansion."""
    def __init__(self):
        self.depth = self.markers = 0
        self.in_string = self.escape_next = False

    def feed(self, chunk):
        offset = 1 if self.escape_next and chunk else 0
        if chunk:
            self.escape_next = False
        for match in JSON_TOKENS.finditer(chunk, offset):
            token = match.group()
            if token.startswith(b"\\"):
                if self.in_string and len(token) == 1:
                    self.escape_next = True
                continue
            if token == b'"':
                self.in_string = not self.in_string
                if not self.in_string:
                    continue
            elif self.in_string:
                continue
            elif token in (b"{", b"["):
                self.depth += 1
            elif token in (b"}", b"]"):
                self.depth -= 1
            self.markers += 1
            if self.depth > MAX_JSON_DEPTH or self.markers > MAX_JSON_MARKERS:
                raise ValueError("JSON structure exceeds limit")


def is_redqueen(data):
    state = REQUEST_STATE.get()
    if state is not None and state.get("path", "").startswith("/typesafe/"):
        return True
    model = data.get("model", "")
    return isinstance(model, str) and model.removeprefix("openai/") in MODELS


def enforce_policy(data):
    for name in ("extra_headers", "extra_body"):
        value = data.get(name)
        if value is not None and not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="invalid request options")
    extra_headers = data.get("extra_headers") or {}
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in extra_headers.items()):
        raise HTTPException(status_code=400, detail="invalid request headers")
    for key in POLICY_KEYS:
        data.pop(key, None)
    extra = data.get("extra_body")
    if isinstance(extra, dict):
        data["extra_body"] = {k: v for k, v in extra.items() if k not in POLICY_KEYS}
    data.update(timeout=INFERENCE_TIMEOUT, num_retries=0, max_retries=0,
                max_fallbacks=0, disable_fallbacks=True)
    # An empty list still takes the SDK's completion-with-fallbacks path. The
    # router has empty fallback lists; use None at the provider boundary.
    data.update(fallbacks=None, context_window_fallbacks=None, content_policy_fallbacks=None)
    state = REQUEST_STATE.get()
    if state is not None:
        extra_headers = dict(extra_headers)
        extra_headers["x-request-id"] = state["request_id"]
        data["extra_headers"] = extra_headers
    return data


def exception_chain(exc):
    pending, seen = [exc], set()
    while pending and len(seen) < 16:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        pending.extend(getattr(item, key, None) for key in
                       ("__cause__", "__context__", "original_exception"))


def safe_failure(exc, request_id):
    status = getattr(exc, "status_code", 500)
    if not isinstance(status, int) or status < 400 or status > 599:
        status = 500
    response, payload, transport_status = None, None, None
    for item in exception_chain(exc):
        candidate = getattr(item, "response", None)
        if isinstance(candidate, httpx.Response):
            response = candidate
        candidate = getattr(item, "body", None)
        if isinstance(candidate, dict):
            payload = candidate.get("error", candidate)
        if isinstance(item, httpx.DecodingError):
            transport_status = 502
        elif isinstance(item, httpx.TimeoutException):
            transport_status = 504
        elif status >= 500 and isinstance(item, (json.JSONDecodeError, ValidationError)):
            transport_status = 502
        elif status == 500 and isinstance(item, (httpx.NetworkError, APIConnectionError)):
            transport_status = transport_status or 503
    if response is not None:
        status = response.status_code if response.status_code >= 400 else status
        try:
            if len(response.content) <= MAX_ERROR_BYTES:
                candidate = response.json()
                if isinstance(candidate, dict):
                    payload = candidate.get("error", candidate)
        except (ValueError, httpx.ResponseNotRead):
            pass
    if transport_status is not None:
        status = transport_status
    code, message, kind = ERROR_DEFAULTS.get(status, ERROR_DEFAULTS[500])
    if isinstance(payload, dict):
        upstream_code = payload.get("code")
        if isinstance(upstream_code, str) and not upstream_code.isdecimal() and SAFE_TOKEN.fullmatch(upstream_code):
            code = upstream_code
        # Backend messages can contain prompts, keys or runtime paths. Preserve
        # their stable code, but never reflect their raw message at the gateway.
    headers = {"x-request-id": request_id}
    if response is not None:
        upstream_id = response.headers.get("x-request-id", "")
        if SAFE_TOKEN.fullmatch(upstream_id):
            headers["x-request-id"] = upstream_id
        retry = safe_retry_after(response.headers.get("retry-after", ""))
        if status in (429, 503) and retry is not None:
            headers["retry-after"] = retry
    return status, {"message": message, "type": kind, "param": None, "code": code}, headers


class RedqueenReliability(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not is_redqueen(data):
            return data
        await install_http_clients()
        if call_type == "pass_through_endpoint":
            # Legacy TypeSafe directly forwards this dictionary as JSON; policy
            # objects must never be inserted into its wire body. Its cached
            # HTTP handler is configured separately below.
            for key in POLICY_KEYS:
                data.pop(key, None)
            return data
        enforce_policy(data)
        # The generic router (Decisions, Responses, image edits, token counts)
        # validates a numeric planning timeout before deployment hooks. None of
        # its router methods wraps inference in wait_for. Install the actual
        # transport timeout after selection, at the provider boundary below.
        data.pop("timeout", None)
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        if not is_redqueen(kwargs):
            return kwargs
        await install_http_clients()
        enforce_policy(kwargs)
        # 1.104.2's image provider drops max_retries then defaults to 2; its
        # embedding provider treats 0 as absent. A supplied SDK client keeps
        # those provider defaults from re-enabling retries (including network
        # failures). The actual HTTPX pool is shared, not recreated per call.
        kwargs["client"] = NoRetryOpenAI(api_key=kwargs.get("api_key") or "unused",
                                         base_url=kwargs.get("api_base"),
                                         http_client=litellm.aclient_session,
                                         timeout=INFERENCE_TIMEOUT)
        return kwargs

    async def async_post_call_failure_hook(self, request_data, original_exception,
                                          user_api_key_dict, traceback_str=None):
        from litellm.proxy.route_llm_request import ProxyModelNotFoundError
        state = REQUEST_STATE.get()
        request_id = state["request_id"] if state else str(uuid.uuid4())
        if isinstance(original_exception, ProxyModelNotFoundError):
            code, text, kind = ERROR_DEFAULTS[404]
            status, error, headers = 404, {"message": text, "type": kind, "param": None, "code": code}, {"x-request-id": request_id}
        elif is_redqueen(request_data):
            status, error, headers = safe_failure(original_exception, request_id)
        else:
            return None
        if state is not None:
            state.update(status=status, error=error, error_headers=headers)
        return HTTPException(status_code=status, detail={"error": error}, headers=headers)

    async def async_post_call_response_headers_hook(self, data, user_api_key_dict,
                                                    response, request_headers=None,
                                                    litellm_call_info=None):
        state = REQUEST_STATE.get()
        if state is None:
            return None
        headers = dict(state.get("error_headers", {}))
        headers.setdefault("x-request-id", state.get("upstream_request_id", state["request_id"]))
        return headers


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit, sse=False):
        self.stream, self.limit, self.sse = stream, limit, sse

    async def __aiter__(self):
        size = 0
        tail = b""
        structure = JSONStructure()
        try:
            async for chunk in self.stream:
                if self.sse:
                    # Count each SSE frame, including a delimiter split across
                    # transport chunks. Never buffer an entire inference stream.
                    pieces = re.split(b"\r\n\r\n|\n\n|\r\r", tail + chunk)
                    for piece in pieces[:-1]:
                        if size + len(piece) > self.limit:
                            raise httpx.DecodingError("upstream SSE frame exceeds size limit")
                        size = 0
                    size += len(pieces[-1])
                    tail = pieces[-1][-3:]
                    size -= len(tail)
                    if size + len(tail) > self.limit:
                        raise httpx.DecodingError("upstream SSE frame exceeds size limit")
                else:
                    size += len(chunk)
                    if size > self.limit:
                        raise httpx.DecodingError("upstream response exceeds size limit")
                    try:
                        structure.feed(chunk)
                    except ValueError as error:
                        raise httpx.DecodingError("upstream JSON structure exceeds limit") from error
                yield chunk
        finally:
            await self.stream.aclose()

    async def aclose(self):
        await self.stream.aclose()


async def limit_response(response):
    state = REQUEST_STATE.get()
    upstream_id = response.headers.get("x-request-id", "")
    if state is not None and SAFE_TOKEN.fullmatch(upstream_id):
        state["upstream_request_id"] = upstream_id
    if response.headers.get("content-encoding", "identity") != "identity":
        await response.aclose()
        raise httpx.DecodingError("compressed upstream responses are not supported")
    sse = response.headers.get("content-type", "").split(";", 1)[0] == "text/event-stream"
    limit = MAX_SSE_FRAME_BYTES if sse else MAX_ERROR_BYTES if response.is_error else MAX_JSON_BYTES
    response.stream = LimitedStream(response.stream, limit, sse=sse)


async def install_http_clients():
    """Use supported HTTPX clients for SDK calls and the Decisions handler.

    Decisions bypasses OpenAI's SDK. Its cached AsyncHTTPHandler exposes a
    public client setter, so it needs the same byte limits independently.
    """
    from litellm.llms.custom_httpx.http_handler import get_async_httpx_client
    client = litellm.aclient_session
    if client is None or not getattr(client, "_redqueen_policy", False) or client.is_closed:
        client = httpx.AsyncClient(timeout=INFERENCE_TIMEOUT, follow_redirects=False,
                                  headers={"accept-encoding": "identity"},
                                  event_hooks={"response": [limit_response]},
                                  limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        client._redqueen_policy = True
        litellm.aclient_session = client
    handler = get_async_httpx_client("openai")
    if handler.client is not client:
        previous = handler.client
        handler.client = client
        await previous.aclose()
    state = REQUEST_STATE.get()
    if state is not None and state.get("path", "").startswith("/typesafe/"):
        from litellm.passthrough.timeout_utils import resolve_pass_through_request_timeout
        handler = get_async_httpx_client("pass_through_endpoint", params={"timeout": resolve_pass_through_request_timeout()})
        if handler.client is not client:
            previous = handler.client
            handler.client = client
            await previous.aclose()


class RedqueenErrors:
    """Normalize errors at ASGI; never buffer successful JSON or SSE streams."""
    def __init__(self, app):
        self.app = app
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(("/v1/", "/typesafe/")):
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers", []))
        request_id = incoming.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        if not SAFE_TOKEN.fullmatch(request_id):
            request_id = str(uuid.uuid4())
        async def reject(status, code=None):
            default_code, text, kind = ERROR_DEFAULTS[status]
            body = json.dumps({"error": {"message": text, "type": kind, "param": None, "code": code or default_code}}).encode()
            headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"x-request-id", request_id.encode())]
            if status == 429:
                headers.append((b"retry-after", b"1"))
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        # One event loop / one worker: increment before any await or body read.
        if self.active >= MAX_IN_FLIGHT:
            return await reject(429, "gateway_busy")
        self.active += 1
        scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-request-id"] + [(b"x-request-id", request_id.encode())]}
        state = {"request_id": request_id, "path": scope["path"]}
        token = REQUEST_STATE.set(state)
        start, error_bytes, sse = None, bytearray(), False

        async def relay(message):
            nonlocal start, sse
            if message["type"] == "http.response.start":
                start = message
                headers = dict(message.get("headers", []))
                sse = b"text/event-stream" in headers.get(b"content-type", b"")
                if message["status"] < 400 or sse:
                    headers.setdefault(b"x-request-id", state["request_id"].encode())
                    start = None
                    state["response_started"] = True
                    await send({**message, "headers": list(headers.items())})
                return
            if message["type"] != "http.response.body":
                return await send(message)
            if start is not None:
                body = message.get("body", b"")
                if len(error_bytes) + len(body) <= MAX_ERROR_BYTES:
                    error_bytes.extend(body)
                if message.get("more_body", False):
                    return
                status = state.get("status", 400 if start["status"] == 422 else start["status"])
                code, text, kind = ERROR_DEFAULTS.get(status, ERROR_DEFAULTS[500])
                error = dict(state.get("error", {"message": text, "type": kind, "param": None, "code": code}))
                # Pinned legacy passthrough passes a synthetic exception to its
                # hook, without the upstream body/headers. Recover only the
                # stable code from its bounded wire body, never its message.
                try:
                    payload = json.loads(error_bytes)
                    upstream = payload.get("error", {}) if isinstance(payload, dict) else {}
                    upstream_code = upstream.get("code") if isinstance(upstream, dict) else None
                    if isinstance(upstream_code, str) and not upstream_code.isdecimal() and SAFE_TOKEN.fullmatch(upstream_code):
                        error["code"] = upstream_code
                except ValueError:
                    pass
                headers = dict(start.get("headers", []))
                retry = safe_retry_after(headers.get(b"retry-after", b""))
                for name in (b"content-length", b"content-type", b"retry-after"):
                    headers.pop(name, None)
                if status in (429, 503) and retry is not None:
                    headers[b"retry-after"] = retry.encode()
                headers.update({k.encode(): v.encode() for k, v in state.get("error_headers", {}).items()})
                headers.setdefault(b"x-request-id", state.get("upstream_request_id", request_id).encode())
                output = json.dumps({"error": error}, separators=(",", ":")).encode()
                headers.update({b"content-type": b"application/json", b"content-length": str(len(output)).encode()})
                await send({**start, "status": status, "headers": list(headers.items())})
                state["response_started"] = True
                await send({"type": "http.response.body", "body": output})
                state["response_complete"] = True
                start = None
                return
            if sse and state.get("error"):
                body = message.get("body", b"")
                # LiteLLM emits complete SSE frames. Only replace its error frame;
                # comments and successful tokens are forwarded byte for byte.
                if len(body) <= MAX_ERROR_BYTES and body.startswith(b"data: "):
                    try:
                        payload = json.loads(body[6:].strip())
                        if isinstance(payload, dict) and "error" in payload:
                            message = {**message, "body": b"data: " + json.dumps({"error": state["error"]}).encode() + b"\n\n"}
                    except ValueError:
                        pass
            await send(message)
            if not message.get("more_body", False):
                state["response_complete"] = True

        # Native chat monitors disconnects. The older image endpoints do not.
        # One receive pump delivers uploads unchanged and independently observes
        # disconnects, so all providers cancel promptly without stealing body
        # chunks from the application's own consumers.
        received = asyncio.Queue(maxsize=1)
        disconnected = asyncio.Event()
        invalid_upload = asyncio.Event()
        upload_status = 400

        async def pump():
            nonlocal upload_status
            size, body = 0, bytearray()
            structure = JSONStructure()
            parse_json = scope["method"] in ("POST", "PUT", "PATCH") and not incoming.get(b"content-type", b"").lower().startswith(b"multipart/")
            try:
                async with asyncio.timeout(UPLOAD_TIMEOUT):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            disconnected.set()
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > MAX_REQUEST_BYTES:
                            upload_status = 413
                            invalid_upload.set()
                            return
                        if parse_json:
                            try:
                                structure.feed(chunk)
                            except ValueError:
                                upload_status = 413
                                invalid_upload.set()
                                return
                            body.extend(chunk)
                        if not message.get("more_body", False):
                            if parse_json:
                                try:
                                    value = json.loads(body)
                                    if not isinstance(value, dict):
                                        raise ValueError("object body required")
                                    del value
                                except (ValueError, RecursionError):
                                    invalid_upload.set()
                                    return
                            body.clear()
                            await received.put(message)
                            break
                        await received.put(message)
            except TimeoutError:
                upload_status = 408
                invalid_upload.set()
                return
            # The upload deadline ends at EOF; inference remains unbounded.
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return
                await received.put(message)

        async def next_message():
            if disconnected.is_set() and received.empty():
                return {"type": "http.disconnect"}
            item = asyncio.create_task(received.get())
            gone = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait((item, gone), return_when=asyncio.FIRST_COMPLETED)
                return item.result() if item.done() else {"type": "http.disconnect"}
            finally:
                for task in (item, gone):
                    task.cancel()
                await asyncio.gather(item, gone, return_exceptions=True)

        pump_task = asyncio.create_task(pump())
        app_task = asyncio.create_task(self.app(scope, next_message, relay))
        gone_task = asyncio.create_task(disconnected.wait())
        invalid_task = asyncio.create_task(invalid_upload.wait())
        try:
            await asyncio.wait((app_task, gone_task, invalid_task), return_when=asyncio.FIRST_COMPLETED)
            if invalid_task.done() and not app_task.done():
                app_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await app_task
                return await reject(upload_status)
            if gone_task.done() and not app_task.done() and not state.get("response_complete"):
                app_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app_task
        except Exception as error:
            LOGGER.error("gateway failure request_id=%s exception_type=%s", request_id, type(error).__name__)
            if not state.get("response_started"):
                await reject(500)
            elif sse and not state.get("response_complete"):
                code, text, kind = ERROR_DEFAULTS[500]
                body = b"data: " + json.dumps({"error": {"message": text, "type": kind, "param": None, "code": code}}).encode() + b"\n\ndata: [DONE]\n\n"
                await send({"type": "http.response.body", "body": body})
            elif not state.get("response_complete"):
                # HTTP success already began; abort so a truncated JSON body
                # cannot be mistaken for a successful completed response.
                raise
        finally:
            for task in (pump_task, gone_task, invalid_task, app_task):
                task.cancel()
            await asyncio.gather(pump_task, gone_task, invalid_task, app_task, return_exceptions=True)
            REQUEST_STATE.reset(token)
            self.active -= 1


class NoRetryOpenAI(AsyncOpenAI):
    @property
    def max_retries(self):
        return 0

    @max_retries.setter
    def max_retries(self, value):
        # LiteLLM updates this public SDK setting even on supplied clients.
        # Retry policy belongs to this gateway, never a provider or caller.
        pass


reliability = RedqueenReliability()
