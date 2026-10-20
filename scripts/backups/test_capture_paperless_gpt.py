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


class SQLiteTransportTests(unittest.TestCase):
    def test_wal_backup_can_be_deserialized_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "source.db"
            with contextlib.closing(sqlite3.connect(source_path, isolation_level=None)) as source:
                source.execute("PRAGMA journal_mode=WAL")
                source.execute("CREATE TABLE probe(value TEXT NOT NULL)")
                source.execute("INSERT INTO probe VALUES ('committed in WAL')")
                with contextlib.closing(sqlite3.connect(":memory:")) as memory:
                    source.backup(memory)
                    before = {path.name: path.read_bytes() for path in Path(temporary).iterdir()}
                    self.assertEqual(memory.serialize()[18:20], b"\x02\x02")
                    payload = capture.serialize_snapshot(memory)
                self.assertEqual(payload[18:20], b"\x01\x01")
                with contextlib.closing(sqlite3.connect(":memory:")) as checked:
                    checked.deserialize(payload)
                    self.assertEqual(checked.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    self.assertEqual(checked.execute("SELECT value FROM probe").fetchall(), [("committed in WAL",)])
                self.assertEqual({path.name: path.read_bytes() for path in Path(temporary).iterdir()}, before)


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0 and
                     os.environ.get("K3S_GPT_SANDBOX_INTEGRATION") == "1",
                     "requires an isolated root Linux guest with Landlock and mount namespaces")
class LinuxSandboxTests(unittest.TestCase):
    """Opt-in kernel checks; run in an isolated guest named openmediavault."""

    def setUp(self):
        capture.RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        capture.RECOVERY.mkdir(mode=0o700, parents=True, exist_ok=True)

    def test_synthetic_wal_probe_uses_real_kernel_restrictions(self):
        capture.synthetic_sqlite_sandbox_check()

    def test_missing_wal_shared_memory_cannot_be_created(self):
        with tempfile.TemporaryDirectory(dir=capture.RUNTIME) as temporary:
            directory = Path(temporary)
            with contextlib.closing(sqlite3.connect(directory / "writer.db", isolation_level=None)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE probe(value TEXT NOT NULL)")
                writer.execute("INSERT INTO probe VALUES ('committed-in-wal')")
                (directory / "probe.db").write_bytes((directory / "writer.db").read_bytes())
                (directory / "probe.db-wal").write_bytes((directory / "writer.db-wal").read_bytes())
                before = {path.name: path.read_bytes() for path in directory.iterdir()}
                attrs = (directory / "probe.db").stat()
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-B", str(SCRIPT), "--sqlite-probe",
                     str(directory / "probe.db"), str(os.getpid()), str(attrs.st_dev), str(attrs.st_ino)],
                    capture_output=True, text=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('"phase": "probe-read-wal"', result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
                self.assertFalse((directory / "probe.db-shm").exists())

    def test_snapshot_from_foreign_mount_namespace_preserves_source(self):
        writer_code = r'''
import ctypes, json, os, sqlite3, sys
from pathlib import Path
root, journal = sys.argv[1:]
libc = ctypes.CDLL(None, use_errno=True)
def check(result):
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
check(libc.unshare(0x00020000))
check(libc.mount(None, b"/", None, (1 << 14) | (1 << 18), None))
check(libc.mount(b"tmpfs", os.fsencode(root), b"tmpfs", 0, b"size=4m"))
os.chroot(root)
os.chdir("/")
Path("/app/db").mkdir(parents=True)
database = sqlite3.connect("/app/db/state.db", isolation_level=None)
database.execute("PRAGMA journal_mode=" + journal).fetchone()
database.execute("PRAGMA wal_autocheckpoint=0")
database.execute("CREATE TABLE probe(value TEXT NOT NULL)")
database.execute("INSERT INTO probe VALUES ('committed synthetic row')")
print(json.dumps({"ready": True}), flush=True)
sys.stdin.read(1)
database.close()
'''
        for journal in ("WAL", "DELETE"):
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as temporary:
                writer = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-B", "-c", writer_code, temporary, journal],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(json.loads(writer.stdout.readline()), {"ready": True})
                    source = Path("/proc") / str(writer.pid) / "root/app/db"
                    self.assertEqual(list(Path(temporary).iterdir()), [],
                                     "writer tmpfs must be invisible in the caller's mount namespace")

                    def source_state():
                        result = {}
                        for path in sorted(source.iterdir()):
                            attrs = path.stat()
                            result[path.name] = (attrs.st_dev, attrs.st_ino, attrs.st_size,
                                                 attrs.st_mtime_ns, attrs.st_ctime_ns,
                                                 capture.hashlib.sha256(path.read_bytes()).hexdigest())
                        return result

                    before = source_state()
                    attrs = (source / "state.db").stat()
                    anchor = {"pid": writer.pid, "process_start": capture.process_start(writer.pid)}
                    with self.assertRaisesRegex(capture.CaptureError, "isolated SQLite snapshot failed"):
                        capture.online_snapshot_in_worker(
                            "state.db", {"device": attrs.st_dev, "inode": attrs.st_ino + 1}, anchor)
                    self.assertEqual(source_state(), before)
                    memory, relations, original_journal = capture.online_snapshot_in_worker(
                        "state.db", {"device": attrs.st_dev, "inode": attrs.st_ino}, anchor)
                    with contextlib.closing(memory):
                        self.assertEqual(original_journal, journal.lower())
                        self.assertEqual(relations, {"probe": 1})
                        self.assertEqual(memory.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                        self.assertEqual(memory.execute("SELECT value FROM probe").fetchall(),
                                         [("committed synthetic row",)])
                    self.assertEqual(source_state(), before)
                    self.assertIsNone(writer.poll())
                finally:
                    if writer.poll() is None:
                        writer.communicate("\n", timeout=5)
                    else:
                        writer.communicate(timeout=5)
                    self.assertEqual(writer.returncode, 0)


if __name__ == "__main__":
    unittest.main()
