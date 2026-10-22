#!/usr/bin/env python3
"""Read Paperless-GPT's document-list response without printing response data or logs."""

import base64
import binascii
import hmac
import json
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


NAMESPACE = "paperless-ngx"
LABELS = "app.kubernetes.io/name=paperless-ngx,app.kubernetes.io/component=gpt"
IMAGE_DIGEST = "sha256:15bad5d455b98f21bb7b5d6615f56871ff67a8bb379dc0dd7ba411f4633071a6"
PORT = 8080
MAX_RESPONSE = 4 * 1024 * 1024
HTTP_STATUS = re.compile(r"(?:status(?:_code|-code)?\s*[=:]\s*|error fetching tags:\s*)(\d{3})\b")
CURRENT_PHASE = "starting"


class DiagnosticError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def kube(*arguments, timeout=45):
    return command(["k3s", "kubectl", "--request-timeout=15s", *arguments], timeout=timeout)


def command(arguments, timeout=45):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
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


def port_forward(resource, remote_port, callback):
    process = subprocess.Popen(
        ["k3s", "kubectl", "--request-timeout=15s", "-n", NAMESPACE, "port-forward",
         "--address=127.0.0.1", resource, f":{remote_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        deadline = time.monotonic() + 10
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise DiagnosticError("loopback port-forward did not start; command output omitted")
                events = selector.select(timeout=max(0, deadline - time.monotonic()))
                if not events:
                    break
                line = process.stdout.readline()
                match = re.match(r"^Forwarding from 127\.0\.0\.1:(\d+) -> \d+\s*$", line)
                if match:
                    return callback(int(match.group(1)))
        raise DiagnosticError("loopback port-forward did not become ready")
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()


def http_probe(port):
    result = {"http_status": None, "content_type": None, "json_type": None, "item_count": None,
              "body_printed": False}
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/documents", timeout=8) as response:
            result["http_status"] = response.status
            result["content_type"] = response.headers.get_content_type()
            body = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        with error:
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


def paperless_request(port, path, token, kind):
    if not header_safe_token(token):
        raise DiagnosticError("unsafe Paperless API token was withheld from the HTTP request")
    request = Request(f"http://127.0.0.1:{port}{path}",
                      headers={"Authorization": "Token " + token.decode("ascii"),
                               "Accept": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    result = {"http_status": None, "content_type": None, "json_type": None, "result_count": None,
              "tag_document_count": None, "token_value_printed": False}
    try:
        with opener.open(request, timeout=8) as response:
            result["http_status"] = response.status
            result["content_type"] = response.headers.get_content_type()
            body = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        with error:
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
    result["json_type"] = "dict" if isinstance(decoded, dict) else type(decoded).__name__
    if isinstance(decoded, dict) and isinstance(decoded.get("results"), list):
        results = decoded["results"]
        result["result_count"] = len(results)
        if kind == "tags" and len(results) == 1 and isinstance(results[0], dict):
            count = results[0].get("document_count")
            result["tag_document_count"] = count if type(count) is int else None
    return result


def paperless_api_probe(port, token, manual_tag):
    global CURRENT_PHASE
    tag_query = urlencode({"name__iexact": manual_tag})
    document_query = urlencode({"tags__name__iexact": manual_tag, "page_size": 1})
    CURRENT_PHASE = "looking up the Paperless-GPT manual tag"
    tags = paperless_request(port, f"/api/tags/?{tag_query}", token, "tags")
    CURRENT_PHASE = "checking Paperless-GPT manual-tag documents"
    documents = paperless_request(port, f"/api/documents/?{document_query}", token, "documents")
    return {"token_accepted": tags.get("http_status") == 200 and tags.get("result_count") is not None and
            documents.get("http_status") == 200 and documents.get("result_count") is not None,
            "manual_tag_lookup": tags, "manual_tag_documents": documents,
            "tag_name_or_document_data_printed": False}


def process_start(pid, container_id):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
        membership = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError as error:
        raise DiagnosticError("running container process identity could not be verified") from error
    if len(fields) <= 19 or not fields[19].isdigit() or container_id not in membership:
        raise DiagnosticError("process does not belong to the expected running container")
    return fields[19]


def header_safe_token(value):
    return bool(value) and all(0x21 <= byte <= 0x7E for byte in value)


def token_format(value):
    stripped = value.strip()
    placeholders = {b"changeme", b"change-me", b"replace-me", b"your-token", b"your-token-here",
                    b"your-paperless-api-token", b"your_paperless_api_token", b"<your-token>",
                    b"<paperless-api-token>", b"replace_with_your_api_token"}
    return {
        "empty": not value,
        "header_safe": header_safe_token(value),
        "contains_whitespace": any(byte in b" \t\r\n\v\f" for byte in value),
        "outer_whitespace": stripped != value,
        "contains_control_bytes": any(byte < 0x20 or byte == 0x7F for byte in value),
        "contains_non_ascii": any(byte > 0x7F for byte in value),
        "authorization_scheme_prefix": re.match(br"(?i)^(?:token|bearer)[ \t]+", stripped) is not None,
        "recognized_placeholder": stripped.lower() in placeholders,
    }


def normalized_token_candidate(value):
    candidate = value.strip()
    changes = []
    if candidate != value:
        changes.append("trim_outer_whitespace")
    prefix = re.match(br"(?i)^(?:token|bearer)[ \t]+", candidate)
    if prefix:
        candidate = candidate[prefix.end():].strip()
        changes.append("remove_authorization_scheme_prefix")
    if changes and header_safe_token(candidate) and not token_format(candidate)["recognized_placeholder"]:
        return candidate, changes
    return None, changes


def process_token(pod):
    global CURRENT_PHASE
    CURRENT_PHASE = "verifying GPT Pod Secret wiring"
    pod_spec = pod.get("spec", {})
    containers = [item for item in pod_spec.get("containers", []) if item.get("name") == "paperless-gpt"]
    env_from = containers[0].get("envFrom", []) if len(containers) == 1 else []
    wired = any(item.get("secretRef", {}).get("name") == "paperless-env" for item in env_from)
    states = [item for item in pod.get("status", {}).get("containerStatuses", [])
              if item.get("name") == "paperless-gpt"]
    if not wired or len(states) != 1:
        raise DiagnosticError("running Pod does not match the expected Paperless Secret wiring")
    container_id = states[0].get("containerID", "").removeprefix("containerd://")
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise DiagnosticError("running container identity is invalid")
    CURRENT_PHASE = "checking CRI container identity"
    runtime = json.loads(command(["k3s", "crictl", "inspect", "-o", "json", container_id]))
    status, info = runtime.get("status", {}), runtime.get("info", {})
    if (status.get("id") != container_id or status.get("state") != "CONTAINER_RUNNING" or
            status.get("metadata", {}).get("name") != "paperless-gpt" or
            status.get("labels", {}).get("io.kubernetes.pod.uid") != pod["metadata"]["uid"] or
            status.get("labels", {}).get("io.kubernetes.pod.namespace") != NAMESPACE):
        raise DiagnosticError("container runtime identity changed during token inspection")
    pid = info.get("pid")
    if type(pid) is not int or pid <= 1:
        raise DiagnosticError("running container process identity is unavailable")
    CURRENT_PHASE = "checking container process identity"
    start = process_start(pid, container_id)
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError as error:
        raise DiagnosticError("running container environment could not be inspected") from error
    if process_start(pid, container_id) != start:
        raise DiagnosticError("running container process changed during token inspection")
    CURRENT_PHASE = "comparing in-memory token metadata"
    values = [entry.partition(b"=")[2] for entry in entries if entry.startswith(b"PAPERLESS_API_TOKEN=")]
    if len(values) > 1:
        raise DiagnosticError("running container has ambiguous Paperless API token variables")
    return values[0] if values else None


def token_evidence(pod):
    global CURRENT_PHASE
    CURRENT_PHASE = "reading Paperless token Secret metadata"
    secret = json.loads(kube("get", "secret", "paperless-env", "-n", NAMESPACE, "-o", "json"))
    encoded = secret.get("data", {}).get("PAPERLESS_API_TOKEN")
    evidence = {"secret_key_present": encoded is not None, "running_process_key_present": None,
                "running_value_matches_secret": None}
    if encoded is None:
        return evidence, None
    try:
        secret_token = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError):
        evidence["secret_encoding"] = "invalid_base64"
        return evidence, None
    evidence["secret_encoding"] = "valid_base64"
    evidence["secret_token_format"] = token_format(secret_token)
    try:
        current = process_token(pod)
    except DiagnosticError as error:
        evidence["running_environment_check"] = {"result": "unavailable", "reason": str(error)}
    except json.JSONDecodeError:
        evidence["running_environment_check"] = {"result": "unavailable", "reason": "invalid CRI JSON metadata"}
    else:
        evidence["running_environment_check"] = {"result": "passed"}
        evidence["running_process_key_present"] = current is not None
        evidence["running_value_matches_secret"] = current is not None and hmac.compare_digest(current, secret_token)
    return evidence, secret_token


def manual_tag():
    global CURRENT_PHASE
    CURRENT_PHASE = "reading Paperless-GPT manual-tag metadata"
    config = json.loads(kube("get", "configmap", "paperless-config", "-n", NAMESPACE, "-o", "json"))
    tag = config.get("data", {}).get("MANUAL_TAG", "")
    if not tag:
        raise DiagnosticError("Paperless-GPT manual tag is not configured")
    return tag


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
    global CURRENT_PHASE
    CURRENT_PHASE = "selecting the ready Paperless-GPT Pod"
    pod, container = ready_pod()
    CURRENT_PHASE = "checking the Paperless-GPT documents endpoint"
    api = port_forward("pod/" + pod["metadata"]["name"], PORT, http_probe)
    token, token_value = token_evidence(pod)
    paperless = None
    candidate = None
    candidate_evidence = {"available": False, "changes": [], "paperless_manual_tag_api": None}
    if token_value is not None:
        candidate, changes = normalized_token_candidate(token_value)
        candidate_evidence.update(available=candidate is not None, changes=changes)
    if header_safe_token(token_value) or candidate is not None:
        tag = manual_tag()
        CURRENT_PHASE = "checking Paperless API authentication and tag lookup"

        def check_tokens(port):
            original = paperless_api_probe(port, token_value, tag) if header_safe_token(token_value) else None
            normalized = paperless_api_probe(port, candidate, tag) if candidate is not None else None
            return original, normalized

        paperless, candidate_evidence["paperless_manual_tag_api"] = port_forward("service/paperless", 8000, check_tokens)
    passed = (api.get("http_status") == 200 and api.get("json_type") == "array" and
              token.get("running_value_matches_secret") is True and
              paperless is not None and paperless.get("token_accepted") is True)
    CURRENT_PHASE = "summarizing sanitized Paperless-GPT logs"
    result = {
        "result": "passed" if passed else "action_needed",
        "namespace": NAMESPACE,
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "image_id": container.get("imageID"),
        "documents_api": api,
        "token_evidence": token,
        "paperless_manual_tag_api": paperless,
        "normalized_token_candidate": candidate_evidence,
        "manual_tag_value_printed": False,
        "backend_log_summary": log_summary(pod["metadata"]["name"]),
        "document_titles_or_contents_printed": False,
        "credentials_or_raw_logs_printed": False,
        "kubernetes_resources_changed": False,
    }
    CURRENT_PHASE = "complete"
    return result


def main():
    try:
        print(json.dumps(probe(), indent=2), flush=True)
    except DiagnosticError as error:
        print(json.dumps({
            "result": "stopped",
            "phase": CURRENT_PHASE,
            "reason": str(error),
            "command_output_printed": False,
            "application_data_printed": False,
        }), file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print(json.dumps({
            "result": "stopped",
            "phase": CURRENT_PHASE,
            "reason": "invalid Kubernetes JSON metadata",
            "command_output_printed": False,
            "application_data_printed": False,
        }), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({
            "result": "stopped",
            "phase": CURRENT_PHASE,
            "reason": "unexpected diagnostic failure; details withheld",
            "command_output_printed": False,
            "application_data_printed": False,
        }), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
