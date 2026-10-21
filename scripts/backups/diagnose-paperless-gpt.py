#!/usr/bin/env python3
"""Read Paperless-GPT's document-list response without printing response data or logs."""

import json
import re
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


NAMESPACE = "paperless-ngx"
LABELS = "app.kubernetes.io/name=paperless-ngx,app.kubernetes.io/component=gpt"
IMAGE_DIGEST = "sha256:15bad5d455b98f21bb7b5d6615f56871ff67a8bb379dc0dd7ba411f4633071a6"
PORT = 8080
MAX_RESPONSE = 4 * 1024 * 1024
HTTP_STATUS = re.compile(r"(?:status(?:_code|-code)?\s*[=:]\s*|error fetching tags:\s*)(\d{3})\b")


class DiagnosticError(Exception):
    pass


def kube(*arguments, timeout=45):
    try:
        result = subprocess.run(["k3s", "kubectl", "--request-timeout=15s", *arguments],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise DiagnosticError("read-only Kubernetes request timed out") from error
    if result.returncode:
        raise DiagnosticError("read-only Kubernetes request failed; command output omitted")
    return result.stdout


def ready_pod():
    items = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", LABELS, "-o", "json")).get("items", [])
    candidates = []
    for pod in items:
        status = pod.get("status", {})
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in status.get("conditions", []))
        containers = [item for item in status.get("containerStatuses", []) if item.get("name") == "paperless-gpt"]
        if (status.get("phase") == "Running" and not pod["metadata"].get("deletionTimestamp") and
                ready and len(containers) == 1 and containers[0].get("ready") is True and
                containers[0].get("imageID", "").endswith(IMAGE_DIGEST)):
            candidates.append((pod, containers[0]))
    if len(candidates) != 1:
        raise DiagnosticError("expected exactly one ready Paperless-GPT Pod on the inventoried image")
    return candidates[0]


def local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http_probe(port):
    result = {"http_status": None, "content_type": None, "json_type": None, "item_count": None,
              "body_printed": False}
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/documents", timeout=8) as response:
            result["http_status"] = response.status
            result["content_type"] = response.headers.get_content_type()
            body = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        result["http_status"] = error.code
        result["content_type"] = error.headers.get_content_type()
        body = error.read(MAX_RESPONSE + 1)
    except (URLError, TimeoutError, OSError):
        result["transport"] = "connection_failed_or_timed_out"
        return result
    if len(body) > MAX_RESPONSE:
        result["body"] = "larger_than_limit"
        return result
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["body"] = "not_json"
        return result
    result["json_type"] = "array" if isinstance(decoded, list) else type(decoded).__name__
    if isinstance(decoded, list):
        result["item_count"] = len(decoded)
    result["empty_list_is_expected"] = result["http_status"] == 200 and decoded == []
    return result


def log_summary(pod_name):
    output = kube("logs", "-n", NAMESPACE, pod_name, "-c", "paperless-gpt", "--since=15m", "--tail=1000",
                  timeout=30)
    rules = {
        "connection_refused": ("connection refused", "connect: refused"),
        "dns_resolution": ("no such host", "temporary failure in name resolution"),
        "timeout": ("i/o timeout", "context deadline exceeded", "timed out"),
        "authentication": ("unauthorized", "invalid token", "status=401", "status_code=401", "401 "),
        "authorization": ("forbidden", "status=403", "status_code=403", "403 "),
        "not_found": ("status=404", "status_code=404", "404 not found"),
        "upstream_5xx": ("status=500", "status_code=500", "status-code=500", "status=502", "status_code=502",
                         "status-code=502", "status=503", "status_code=503", "status-code=503", "server error"),
        "tls": ("x509:", "certificate verify failed", "tls handshake", "ssl error"),
        "bad_json": ("failed to parse json", "invalid character", "unexpected end of json", "unexpected eof",
                      "cannot unmarshal"),
        "paperless_api_error": ("error fetching documents", "error checking document count", "error fetching tags",
                                "http request failed", "error response from server in getdocumentsbytag"),
    }
    matched = 0
    categories = set()
    for line in output.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in (
                "error fetching documents", "error checking document count", "error fetching tags",
                "http request failed", "error response from server in getdocumentsbytag",
                "failed to parse json response in getdocumentsbytag")):
            matched += 1
            for category, markers in rules.items():
                if any(marker in lowered for marker in markers):
                    categories.add(category)
            status = HTTP_STATUS.search(lowered)
            if status:
                code = int(status.group(1))
                if code == 401:
                    categories.add("authentication")
                elif code == 403:
                    categories.add("authorization")
                elif code == 404:
                    categories.add("not_found")
                elif 500 <= code <= 599:
                    categories.add("upstream_5xx")
    return {"matching_error_lines": matched, "categories": sorted(categories), "raw_logs_printed": False}


def probe():
    pod, container = ready_pod()
    port = local_port()
    process = subprocess.Popen(
        ["k3s", "kubectl", "--request-timeout=15s", "-n", NAMESPACE, "port-forward",
         "--address=127.0.0.1", "pod/" + pod["metadata"]["name"], f"{port}:{PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise DiagnosticError("loopback port-forward did not start; command output omitted")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise DiagnosticError("loopback port-forward did not become ready")
        api = http_probe(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    return {
        "result": "passed" if api.get("http_status") == 200 and api.get("json_type") == "array" else "action_needed",
        "namespace": NAMESPACE,
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "image_id": container.get("imageID"),
        "documents_api": api,
        "backend_log_summary": log_summary(pod["metadata"]["name"]),
        "document_titles_or_contents_printed": False,
        "credentials_or_raw_logs_printed": False,
        "kubernetes_resources_changed": False,
    }


def main():
    try:
        print(json.dumps(probe(), indent=2), flush=True)
    except (DiagnosticError, json.JSONDecodeError):
        print("Paperless-GPT diagnostics stopped; command output and application data were omitted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
