import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "inspect_gpt_cutover", Path(__file__).with_name("inspect-paperless-gpt-cutover.py"))
inspection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspection)


class CutoverInspectionTests(unittest.TestCase):
    def test_command_allowlist_rejects_mutations_and_secret_reads(self):
        prefix = ["k3s", "ctr", "--namespace", "k8s.io", "tasks"]
        container = "a" * 64
        for arguments in (prefix + ["pause", "--help"], prefix + ["resume", "--help"], prefix + ["list"],
                          ["k3s", "kubectl", "--request-timeout=15s", "get", "pod", "writer", "-o", "json"],
                          ["k3s", "crictl", "inspect", "-o", "json", container]):
            self.assertTrue(inspection.is_read_only_command(arguments))
        for arguments in (prefix + ["pause", container], prefix + ["resume", container],
                          ["k3s", "kubectl", "--request-timeout=15s", "patch", "deployment", "writer"],
                          ["k3s", "kubectl", "--request-timeout=15s", "get", "secrets"],
                          ["k3s", "crictl", "stop", container]):
            self.assertFalse(inspection.is_read_only_command(arguments))

    def test_probe_summary_omits_headers_and_commands(self):
        for handler in ({"httpGet": {"path": "/private-query", "httpHeaders": [{"name": "Authorization", "value": "private-token"}]}},
                        {"exec": {"command": ["sh", "-c", "private-command"]}}):
            result = inspection.probe_summary({**handler, "periodSeconds": 30, "failureThreshold": 5})
            self.assertEqual(result["period_seconds"], 30)
            self.assertEqual(result["failure_threshold"], 5)
            self.assertNotIn("private", json.dumps(result))
        self.assertIsNone(inspection.probe_summary(None))

    def test_ownership_summary_omits_private_names_and_digests(self):
        manifest = {"files": {
            "config/private-key-file": {"type": "file", "uid": 0, "gid": 0, "mode": 0o600, "sha256": "private-digest"},
            "config/private-second-file": {"type": "file", "uid": 0, "gid": 0, "mode": 0o600},
            "db/private-database": {"type": "file", "uid": 1000, "gid": 1000, "mode": 0o640},
        }}
        result = inspection.captured_ownership(manifest)
        self.assertEqual(result, [
            {"component": "config", "type": "file", "uid": 0, "gid": 0, "mode": "0o600", "entries": 2},
            {"component": "db", "type": "file", "uid": 1000, "gid": 1000, "mode": "0o640", "entries": 1},
        ])
        self.assertNotIn("private", json.dumps(result))

    def test_unified_cgroup_requires_exact_container_leaf(self):
        container = "a" * 64
        self.assertEqual(inspection.unified_cgroup("0::/kubepods/pod/cri-" + container + ".scope\n", container),
                         ("kubepods", "pod", "cri-" + container + ".scope"))
        for path in ("/", "/kubepods/../" + container, "/kubepods//" + container,
                     "/kubepods/" + container + "/other-container", "/kubepods/unrelated"):
            with self.subTest(path=path), self.assertRaises(inspection.InspectionError):
                inspection.unified_cgroup("0::" + path, container)
        self.assertIsNone(inspection.unified_cgroup("1:memory:/legacy", container))


if __name__ == "__main__":
    unittest.main()
