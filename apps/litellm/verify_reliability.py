#!/usr/bin/env python3
"""Operator-run gateway checks; never prints credentials, prompts or responses.

kubectl -n litellm exec -i deployment/litellm -c litellm -- python - \
  < apps/litellm/verify_reliability.py
"""
import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

import yaml

sys.path.insert(0, "/etc/litellm")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="optionally run one real chat generation; may load/evict idle models")
    parser.add_argument("--idle-only", action="store_true", help="check the existing gateway before a maintenance rollout")
    args = parser.parse_args()
    if args.idle_only:
        headers = {"authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]}
        req = urllib.request.Request("http://127.0.0.1:4000/health/backlog", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read(65537)
            assert len(raw) <= 65536, "backlog response exceeds verification limit"
            backlog = json.loads(raw)
        in_flight = backlog.get("in_flight_requests")
        print("gateway backlog:", {key: backlog.get(key) for key in ("in_flight_requests", "admitted_requests", "queued_requests")})
        assert isinstance(in_flight, int) and in_flight <= 1, "active gateway requests remain; leave them running and retry after they finish"
        return
    from redqueen_reliability import INFERENCE_TIMEOUT, NoRetryOpenAI
    versions = {name: importlib.metadata.version(name) for name in ("litellm", "openai", "httpx")}
    print("versions:", versions)
    assert versions == {"litellm": "1.104.2", "openai": "2.33.0", "httpx": "0.28.1"}
    command = Path("/proc/1/cmdline").read_bytes()
    assert b"redqueen_gateway.py" in command, "gateway policy entrypoint is not running"
    config = yaml.safe_load(Path("/etc/litellm/config.yaml").read_text())
    assert "redqueen_callbacks.reliability" in config["litellm_settings"]["callbacks"]
    assert config["router_settings"]["num_retries"] == 0
    assert config["router_settings"]["disable_cooldowns"] is True
    assert "request_timeout" not in config["litellm_settings"]
    assert all("timeout" not in model["litellm_params"] for model in config["model_list"])
    assert INFERENCE_TIMEOUT.read is None
    sdk = NoRetryOpenAI(api_key="verification-only")
    sdk.max_retries = 9
    assert sdk.max_retries == 0
    asyncio.run(sdk.close())
    print("policy:", {"inference_read_timeout": None, "retries": 0, "heartbeat_seconds": config["litellm_settings"].get("sse_keepalive_ping_interval_seconds")})
    token = os.environ["LITELLM_MASTER_KEY"]

    def request(path, payload=None, authorized=True, expected=None):
        request_id = str(uuid.uuid4())
        headers = {"content-type": "application/json", "x-request-id": request_id}
        if authorized:
            headers["authorization"] = "Bearer " + token
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request("http://127.0.0.1:4000" + path, data=body, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=None if args.model else 15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(65537)
            assert len(raw) <= 65536, "verification response exceeded byte limit"
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            error = data.get("error", {}) if isinstance(data, dict) else {}
            summary = {"route": path, "status": response.status,
                       "request_id": response.headers.get("x-request-id"),
                       "retry_after": response.headers.get("retry-after"),
                       "error_code": error.get("code") if isinstance(error, dict) else None}
            print(json.dumps(summary, sort_keys=True))
            if expected is not None:
                assert response.status == expected, "unexpected verification HTTP status"
            if response.status >= 400:
                assert isinstance(error, dict) and isinstance(error.get("code"), str)
            return response.status

    request("/health/readiness", expected=200)
    request("/v1/models", authorized=False, expected=401)
    request("/v1/chat/completions", {"model": "not-a-configured-model", "messages": [{"role": "user", "content": "hello"}]}, expected=404)
    if args.model:
        status = request("/v1/chat/completions", {"model": args.model, "messages": [{"role": "user", "content": "say hello in one sentence"}], "max_tokens": 32})
        assert status in (200, 429, 503), "chat smoke failed"
        if status != 200:
            print("real generation remains unproven while admission is busy")


if __name__ == "__main__":
    main()
