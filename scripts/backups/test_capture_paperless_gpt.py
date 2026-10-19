import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("capture-paperless-gpt.py")
spec = importlib.util.spec_from_file_location("capture_paperless_gpt", SCRIPT)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class WorkerDiagnosticTests(unittest.TestCase):
    def test_os_error_reports_errno_without_filename_or_message(self):
        with patch.object(capture, "WORKER_PHASE", "probe-open-sqlite"):
            record = capture.worker_error_record(
                PermissionError(errno.EACCES, "private exception text", "/private/source.db"))
        self.assertEqual(record["phase"], "probe-open-sqlite")
        self.assertEqual(record["error_type"], "PermissionError")
        self.assertEqual(record["errno"], errno.EACCES)
        serialized = json.dumps(record)
        self.assertNotIn("private exception text", serialized)
        self.assertNotIn("/private/source.db", serialized)

    def test_sqlite_error_reports_code_without_sql_or_table_names(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as database:
            try:
                database.execute("SELECT private_column FROM private_table")
            except sqlite3.Error as error:
                record = capture.worker_error_record(error)
        self.assertEqual(record["sqlite_errorcode"], sqlite3.SQLITE_ERROR)
        self.assertEqual(record["sqlite_errorname"], "SQLITE_ERROR")
        self.assertNotIn("private_column", json.dumps(record))
        self.assertNotIn("private_table", json.dumps(record))

    def test_traceback_omits_capture_error_text_and_source_paths(self):
        try:
            capture.require(False, "sensitive validation detail")
        except capture.CaptureError as error:
            record = capture.worker_error_record(error)
        self.assertTrue(any(frame["function"] == "require" for frame in record["frames"]))
        self.assertTrue(all(set(frame) == {"function", "line"} for frame in record["frames"]))
        self.assertNotIn("sensitive validation detail", json.dumps(record))
        self.assertNotIn(str(SCRIPT), json.dumps(record))

    def test_worker_cli_emits_actionable_safe_error(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [str(SCRIPT), "--sqlite-probe",
                "/run/k3s-paperless-gpt-capture/fixture/probe.db", "1", "2", "3"]
        real_open = os.open

        def deny_synthetic_source(path, *args, **kwargs):
            if str(path) == argv[2]:
                raise PermissionError(errno.EACCES, "never print this", "/private/real.db")
            return real_open(path, *args, **kwargs)

        with (
            patch.object(sys, "argv", argv),
            patch("platform.system", return_value="Linux"),
            patch("platform.node", return_value="openmediavault"),
            patch("os.geteuid", return_value=0),
            patch("os.umask"),
            patch.dict(os.environ),
            patch("os.open", side_effect=deny_synthetic_source),
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as stopped,
        ):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(stopped.exception.code, 1)
        records = [line.removeprefix("SQLITE_WORKER_ERROR_JSON=")
                   for line in stderr.getvalue().splitlines()
                   if line.startswith("SQLITE_WORKER_ERROR_JSON=")]
        self.assertEqual(len(records), 1, "failed worker must report its operation and error code")
        record = json.loads(records[0])
        self.assertEqual(record["phase"], "probe-pin-source")
        self.assertEqual(record["error_type"], "PermissionError")
        self.assertEqual(record["errno"], errno.EACCES)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("never print this", stderr.getvalue())
        self.assertNotIn("/private/real.db", stderr.getvalue())


class SyntheticOnlyTests(unittest.TestCase):
    def run_check_mode(self, synthetic_failure=None):
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", [str(SCRIPT), "--check-sandbox"]),
            patch.object(capture.platform, "system", return_value="Linux"),
            patch.object(capture.platform, "node", return_value="openmediavault"),
            patch.object(capture.os, "geteuid", return_value=0),
            patch.object(capture.os, "umask"),
            patch.dict(os.environ),
            patch.object(capture.shutil, "which", return_value="/host/tool"),
            patch.object(capture, "managed_directory"),
            patch.object(capture, "lock", side_effect=lambda path: contextlib.nullcontext()),
            patch.object(capture, "private_mount_namespace"),
            patch.object(capture, "synthetic_sqlite_sandbox_check", side_effect=synthetic_failure),
            patch.object(capture, "capture", side_effect=AssertionError("live capture invoked")),
            patch.object(capture, "run", side_effect=AssertionError("host command invoked")),
            patch.object(capture, "placement", side_effect=AssertionError("NAS placement invoked")),
            patch.object(capture, "live_source", side_effect=AssertionError("live GPT inspected")),
            contextlib.redirect_stdout(stdout),
        ):
            capture.main()
        return stdout.getvalue()

    def test_check_mode_finishes_without_inspecting_production(self):
        self.assertEqual(self.run_check_mode(),
                         "synthetic sandbox check passed; no live GPT files were opened\n")

    def test_failed_check_never_falls_through_to_production(self):
        with self.assertRaisesRegex(capture.CaptureError, "fixture stopped"):
            self.run_check_mode(capture.CaptureError("fixture stopped"))

    def test_probe_failure_is_printed_and_fixture_is_cleaned(self):
        diagnostic = 'SQLITE_WORKER_ERROR_JSON={"phase":"probe-open-sqlite","errno":13}\n'
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            recovery = Path(temporary) / "recovery"
            runtime.mkdir()
            recovery.mkdir()
            stderr = io.StringIO()
            with (
                patch.object(capture, "RUNTIME", runtime),
                patch.object(capture, "RECOVERY", recovery),
                patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["synthetic-worker"], 1, stdout="", stderr=diagnostic)),
                contextlib.redirect_stderr(stderr),
                self.assertRaisesRegex(capture.CaptureError, "synthetic WAL/write-denial probe failed"),
            ):
                capture.synthetic_sqlite_sandbox_check()
            self.assertIn(diagnostic.strip(), stderr.getvalue())
            record = json.loads((recovery / "sqlite-probe-error.json").read_text())
            self.assertEqual(record["stderr"], diagnostic)
            self.assertEqual(list(runtime.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
