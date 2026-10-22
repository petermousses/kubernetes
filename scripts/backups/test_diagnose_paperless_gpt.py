"""Synthetic credential and loopback HTTP checks; no live cluster access."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "diagnose_paperless_gpt", Path(__file__).with_name("diagnose-paperless-gpt.py"))
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


@contextmanager
def http_server(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_arguments):
        pass


class TokenChecks(unittest.TestCase):
    def test_format_and_candidate_handle_invalid_header_bytes(self):
        cases = [
            (b"synthetic-credential", True, None, []),
            (b"synthetic-credential\r\n", False, b"synthetic-credential", ["trim_outer_whitespace"]),
            (b" Token synthetic-credential\n", False, b"synthetic-credential",
             ["trim_outer_whitespace", "remove_authorization_scheme_prefix"]),
            (b"Bearer synthetic-credential", False, b"synthetic-credential",
             ["remove_authorization_scheme_prefix"]),
            (b"synthetic internal space\n", False, None, ["trim_outer_whitespace"]),
            (b"synthetic\x00credential", False, None, []),
            (b"synthetic\xffcredential\n", False, None, ["trim_outer_whitespace"]),
            (b"\r\n", False, None, ["trim_outer_whitespace"]),
            (b"", False, None, []),
            (b"changeme\n", False, None, ["trim_outer_whitespace"]),
        ]
        for value, safe, candidate, changes in cases:
            with self.subTest(value=value):
                metadata = diagnostic.token_format(value)
                self.assertEqual(metadata["header_safe"], safe)
                self.assertEqual(diagnostic.normalized_token_candidate(value), (candidate, changes))
                self.assertTrue(all(type(item) is bool for item in metadata.values()))

    def test_missing_empty_and_invalid_encoding_are_distinct(self):
        cases = [({}, False, None),
                 ({"PAPERLESS_API_TOKEN": ""}, True, b""),
                 ({"PAPERLESS_API_TOKEN": "not base64!"}, True, None)]
        for data, present, expected in cases:
            with self.subTest(data=data), patch.object(diagnostic, "kube", return_value=json.dumps({"data": data})), \
                    patch.object(diagnostic, "process_token", return_value=b""):
                metadata, value = diagnostic.token_evidence({})
                self.assertEqual(metadata["secret_key_present"], present)
                self.assertEqual(value, expected)
                if expected == b"":
                    self.assertTrue(metadata["secret_token_format"]["empty"])
                elif present:
                    self.assertEqual(metadata["secret_encoding"], "invalid_base64")

    def test_runtime_failures_preserve_redacted_secret_format(self):
        secret = b"synthetic-credential\n"
        response = json.dumps({"data": {"PAPERLESS_API_TOKEN": base64.b64encode(secret).decode()}})
        errors = [diagnostic.DiagnosticError("running container process identity is unavailable"),
                  json.JSONDecodeError("SYNTHETIC_PRIVATE_ERROR", "SYNTHETIC_PRIVATE_DOC", 0)]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(diagnostic, "kube", return_value=response), \
                    patch.object(diagnostic, "process_token", side_effect=error):
                metadata, value = diagnostic.token_evidence({})
                self.assertEqual(value, secret)
                self.assertTrue(metadata["secret_token_format"]["outer_whitespace"])
                self.assertEqual(metadata["running_environment_check"]["result"], "unavailable")
                self.assertIsNone(metadata["running_value_matches_secret"])
                self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(metadata))
                self.assertNotIn("synthetic-credential", json.dumps(metadata))

    def test_invalid_header_never_reaches_http(self):
        with patch.object(diagnostic, "build_opener") as opener:
            for value in (None, b"", b"synthetic\ncredential", b"synthetic credential", b"\xff"):
                with self.subTest(value=value), self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.paperless_request(1, "/api/tags/", value, "tags")
            opener.assert_not_called()


class HttpChecks(unittest.TestCase):
    def test_authentication_is_not_forwarded_on_redirect(self):
        redirected = []

        class Destination(QuietHandler):
            def do_GET(self):
                redirected.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        with http_server(Destination) as destination_port:
            class Redirect(QuietHandler):
                def do_GET(self):
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{destination_port}/api/tags/")
                    self.end_headers()

            with http_server(Redirect) as port:
                result = diagnostic.paperless_request(port, "/api/tags/", b"synthetic-credential", "tags")
        self.assertEqual(result["http_status"], 302)
        self.assertEqual(redirected, [])

    def test_normalized_candidate_is_checked_without_mutation_or_output_leaks(self):
        original = b"synthetic-credential\r\n"
        calls = []

        class Paperless(QuietHandler):
            def do_GET(self):
                auth = self.headers.get("Authorization")
                calls.append((self.path, auth))
                if self.path == "/api/documents":
                    status, body = 500, {"error": "SYNTHETIC_PRIVATE_ERROR"}
                elif auth != "Token synthetic-credential":
                    status, body = 401, {"detail": "SYNTHETIC_PRIVATE_AUTH_ERROR"}
                elif self.path.startswith("/api/tags/?"):
                    status, body = 200, {"results": [{"name": "SYNTHETIC_PRIVATE_TAG", "document_count": 0}]}
                else:
                    status, body = 200, {"results": []}
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        def kube(*args, **_kwargs):
            if args[:2] == ("get", "secret"):
                return json.dumps({"data": {"PAPERLESS_API_TOKEN": base64.b64encode(original).decode()}})
            if args[:2] == ("get", "configmap"):
                return json.dumps({"data": {"MANUAL_TAG": "SYNTHETIC_PRIVATE_TAG"}})
            if args[0] == "logs":
                return "error fetching tags: 401 SYNTHETIC_PRIVATE_LOG"
            raise AssertionError("unexpected Kubernetes request")

        pod = {"metadata": {"name": "synthetic-pod", "uid": "synthetic-uid"}}
        with http_server(Paperless) as port, \
                patch.object(diagnostic, "ready_pod", return_value=(pod, {"imageID": "synthetic-image"})), \
                patch.object(diagnostic, "process_token", return_value=original), \
                patch.object(diagnostic, "kube", side_effect=kube), \
                patch.object(diagnostic, "port_forward", side_effect=lambda resource, remote, callback: callback(port)):
            result = diagnostic.probe()
        self.assertEqual(result["result"], "action_needed")
        self.assertTrue(result["token_evidence"]["running_value_matches_secret"])
        self.assertTrue(result["token_evidence"]["secret_token_format"]["outer_whitespace"])
        self.assertIsNone(result["paperless_manual_tag_api"])
        candidate = result["normalized_token_candidate"]
        self.assertEqual(candidate["changes"], ["trim_outer_whitespace"])
        self.assertTrue(candidate["paperless_manual_tag_api"]["token_accepted"])
        self.assertEqual(len(calls), 3)
        self.assertFalse(result["kubernetes_resources_changed"])
        for marker in ("synthetic-credential", "SYNTHETIC_PRIVATE"):
            self.assertNotIn(marker, json.dumps(result))

    def test_http_200_without_paginated_results_is_not_accepted(self):
        with patch.object(diagnostic, "paperless_request", return_value={"http_status": 200, "json_type": "dict"}):
            self.assertFalse(diagnostic.paperless_api_probe(1, b"synthetic", "synthetic")["token_accepted"])


if __name__ == "__main__":
    unittest.main()
