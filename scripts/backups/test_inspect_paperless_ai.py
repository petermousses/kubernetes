"""Run the actual Node reader on synthetic files and check private capture behavior."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "inspect_paperless_ai", Path(__file__).with_name("inspect-paperless-ai.py"))
inspection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspection)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        (self.app / "OPENAPI").mkdir(parents=True)
        (self.app / "routes").mkdir()
        for name in inspection.SOURCE_HASHES:
            (self.app / name).write_text("// synthetic source: " + name)
        self.data = json.dumps({"openapi": "3.0.0", "info": {
            "title": "Paperless-AI API Documentation", "description": "SYNTHETIC_PRIVATE_CONTENT"},
            "paths": {"/synthetic": {}}}).encode()
        (self.app / "OPENAPI/openapi.json").write_bytes(self.data)

    def read_with_node(self):
        environment = dict(os.environ)
        for name in inspection.NODE_ENV_REMOVALS:
            environment.pop(name, None)
        environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
        result = subprocess.run(["node", "--no-warnings", "-e", inspection.NODE_CODE, str(self.app)],
                                text=True, capture_output=True, env=environment, timeout=10, check=True)
        return json.loads(result.stdout)


class NodeReaderChecks(Fixture):
    def test_reads_bounded_file_without_modifying_it(self):
        path = self.app / "OPENAPI/openapi.json"
        before = path.stat()
        result = self.read_with_node()
        after = path.stat()
        self.assertEqual(base64.b64decode(result["openapi"]["base64"]), self.data)
        self.assertEqual(result["openapi"]["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual((before.st_mtime_ns, before.st_ctime_ns, before.st_size),
                         (after.st_mtime_ns, after.st_ctime_ns, after.st_size))
        self.assertTrue(result["route_set_matches"])
        self.assertEqual(set(result["source_hashes"]), set(inspection.SOURCE_HASHES))

    def test_unknown_entries_are_reported_without_names_or_contents(self):
        (self.app / "OPENAPI/SYNTHETIC_PRIVATE_NAME").write_text("SYNTHETIC_PRIVATE_CONTENT")
        result = self.read_with_node()
        self.assertEqual(result, {"error": "unexpected_openapi_entries"})
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_schema_symlinks_and_hard_links_are_rejected(self):
        path = self.app / "OPENAPI/openapi.json"
        external = self.root / "private.json"
        external.write_bytes(self.data)
        path.unlink()
        path.symlink_to(external)
        self.assertEqual(self.read_with_node(), {"error": "file_read_failed"})
        path.unlink()
        os.link(external, path)
        self.assertEqual(self.read_with_node(), {"error": "expected_regular_file"})

    def test_fifo_is_rejected_without_blocking(self):
        path = self.app / "OPENAPI/openapi.json"
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(self.read_with_node(), {"error": "expected_regular_file"})

    def test_source_directory_symlink_is_not_followed(self):
        outside = self.root / "private-routes"
        shutil.move(str(self.app / "routes"), outside)
        (self.app / "routes").symlink_to(outside, target_is_directory=True)
        result = self.read_with_node()
        self.assertFalse(result["route_set_matches"])
        self.assertTrue(all(value is None for name, value in result["source_hashes"].items() if name.startswith("routes/")))

    def test_oversized_file_is_rejected(self):
        (self.app / "OPENAPI/openapi.json").write_bytes(b"x" * (inspection.MAX_FILE + 1))
        self.assertEqual(self.read_with_node(), {"error": "file_size_outside_limit"})


class CaptureChecks(Fixture):
    def capture(self, metadata, *, second=None, hashes=None):
        anchor = ("synthetic-pod-uid", "synthetic-container", "synthetic-image", 0)
        recovery = self.root / "recovery"
        recovery.mkdir(mode=0o700, exist_ok=True)
        inputs = [(metadata, self.data), (metadata if second is None else second, self.data)]
        progress = {"result": "stopped"}
        with patch.object(inspection, "ROOT", recovery), \
                patch.object(inspection, "SOURCE_HASHES", metadata["source_hashes"] if hashes is None else hashes), \
                patch.object(inspection, "checked_pod", return_value=({}, anchor)), \
                patch.object(inspection, "inspect_files", side_effect=inputs):
            result = inspection.inspect(progress)
        return result

    def test_private_copy_retains_bytes_and_output_reveals_only_metadata(self):
        metadata = self.read_with_node()
        result = self.capture(metadata)
        self.assertEqual(result["result"], "passed")
        self.assertTrue(result["pinned_generator_verified"])
        self.assertEqual(result["classification"], "generated_api_documentation")
        directory = Path(result["recovery_directory"])
        self.assertEqual((directory / "openapi.json").read_bytes(), self.data)
        self.assertEqual((directory / "openapi.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(result["kubernetes_resources_changed"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertNotIn(metadata["openapi"]["base64"], json.dumps(result))

    def test_changed_generator_is_preserved_but_not_classified(self):
        metadata = self.read_with_node()
        expected = dict(metadata["source_hashes"], **{"server.js": "0" * 64})
        result = self.capture(metadata, hashes=expected)
        self.assertEqual(result["result"], "action_needed")
        self.assertFalse(result["pinned_generator_verified"])
        self.assertTrue((Path(result["recovery_directory"]) / "openapi.json").is_file())

    def test_source_change_during_copy_prevents_success_receipt(self):
        metadata = self.read_with_node()
        second = copy.deepcopy(metadata)
        second["route_set_matches"] = False
        with self.assertRaises(inspection.InspectionError):
            self.capture(metadata, second=second)
        self.assertEqual(len(list((self.root / "recovery/ai-openapi").glob("capture-*/openapi.json"))), 1)
        self.assertEqual(list((self.root / "recovery/ai-openapi").glob("capture-*/receipt.json")), [])

    def test_transport_validates_checksums_and_disables_node_hooks(self):
        metadata = self.read_with_node()
        with patch.object(inspection, "kube", return_value=json.dumps(metadata)) as kube:
            actual, data = inspection.inspect_files()
        self.assertEqual(actual, metadata)
        self.assertEqual(data, self.data)
        arguments = kube.call_args.args
        for name in inspection.NODE_ENV_REMOVALS:
            index = arguments.index(name)
            self.assertEqual(arguments[index - 1], "-u")
        metadata["openapi"]["sha256"] = "0" * 64
        with patch.object(inspection, "kube", return_value=json.dumps(metadata)), self.assertRaises(inspection.InspectionError):
            inspection.inspect_files()

    def test_schema_values_and_invalid_json_are_not_reported(self):
        self.assertEqual(inspection.schema_summary(b"SYNTHETIC_PRIVATE_CONTENT"), {"expected_openapi_document": False})
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(inspection.schema_summary(self.data)))


if __name__ == "__main__":
    unittest.main()
