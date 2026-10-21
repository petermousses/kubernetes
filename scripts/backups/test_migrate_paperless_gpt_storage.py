import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("migrate_gpt_storage", Path(__file__).with_name("migrate-paperless-gpt-storage.py"))
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def source_template():
    return {"metadata": {"labels": {"app": "paperless-gpt"}}, "spec": {
        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
        "terminationGracePeriodSeconds": 60,
        "containers": [{"name": "paperless-gpt", "image": migration.capture.IMAGE,
                        "env": [{"name": "PRIVATE_SETTING", "value": "must-be-preserved"}],
                        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False},
                        "readinessProbe": {"tcpSocket": {"port": 8080}},
                        "volumeMounts": [{"name": "prompts", "mountPath": "/app/prompts"}]}],
        "volumes": [{"name": "prompts", "persistentVolumeClaim": {"claimName": "paperless-gpt"}}]}}


class CutoverStateTests(unittest.TestCase):
    def test_patch_adds_only_state_claims_and_omv_pin(self):
        original = source_template()
        snapshot = copy.deepcopy(original)
        target = migration.target_template(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(target["spec"]["securityContext"], original["spec"]["securityContext"])
        self.assertEqual(target["spec"]["nodeSelector"], {"kubernetes.io/hostname": "openmediavault"})
        updated = copy.deepcopy(target)
        updated["spec"].pop("nodeSelector")
        updated["spec"]["volumes"] = updated["spec"]["volumes"][:-2]
        updated["spec"]["containers"][0]["volumeMounts"] = updated["spec"]["containers"][0]["volumeMounts"][:-2]
        self.assertEqual(updated, original)
        self.assertEqual([volume["persistentVolumeClaim"]["claimName"] for volume in target["spec"]["volumes"]],
                         ["paperless-gpt", "paperless-gpt-config", "paperless-gpt-db"])

    def test_existing_mount_or_sidecar_is_refused(self):
        for path in ("/app", "/app/config", "/app/db", "/app/config/nested"):
            original = source_template()
            original["spec"]["containers"][0]["volumeMounts"].append({"name": "other", "mountPath": path})
            with self.subTest(path=path), self.assertRaises(migration.capture.CaptureError):
                migration.target_template(original)
        original = source_template()
        original["spec"]["containers"].append({"name": "unknown-sidecar"})
        with self.assertRaises(migration.capture.CaptureError):
            migration.target_template(original)

    def watchdog(self, state):
        events = []
        def save(name, value):
            events.append(("saved", value["phase"]))
        def thaw(value):
            events.append(("thawed", value["phase"]))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "state.json").touch()
            with (patch.object(migration, "RECOVERY", Path(directory)),
                  patch.object(migration, "journal", contextlib.nullcontext),
                  patch.object(migration, "load", return_value=state),
                  patch.object(migration, "save", side_effect=save),
                  patch.object(migration, "thaw_source", side_effect=thaw),
                  patch.object(migration, "boot_id", return_value="this-boot"),
                  patch.object(migration.time, "monotonic", return_value=100)):
                stopped = migration.watchdog_tick()
        return stopped, events

    def test_expired_pause_cancels_before_thaw(self):
        stopped, events = self.watchdog({"phase": "frozen", "authoritative": "source",
                                         "anchor": {"boot_id": "this-boot"}, "freeze_deadline": 99})
        self.assertTrue(stopped)
        self.assertEqual(events, [("saved", "aborted"), ("thawed", "aborted")])

    def test_committed_target_is_never_thawed_by_watchdog(self):
        stopped, events = self.watchdog({"phase": "target-committed", "authoritative": "target"})
        self.assertTrue(stopped)
        self.assertEqual(events, [])
        with self.assertRaises(migration.capture.CaptureError):
            migration.thaw_source({"authoritative": "target"})

    def test_unexpired_pause_is_untouched(self):
        stopped, events = self.watchdog({"phase": "frozen", "authoritative": "source",
                                         "anchor": {"boot_id": "this-boot"}, "freeze_deadline": 101})
        self.assertFalse(stopped)
        self.assertEqual(events, [])

    def test_watchdog_retries_thaw_after_its_own_interruption(self):
        stopped, events = self.watchdog({"phase": "aborted", "authoritative": "source"})
        self.assertTrue(stopped)
        self.assertEqual(events, [("thawed", "aborted")])

    def test_deadline_rejects_aborted_or_rebooted_capture(self):
        state = {"phase": "frozen", "authoritative": "source", "anchor": {"boot_id": "this-boot"}, "freeze_deadline": 101}
        with patch.object(migration, "boot_id", return_value="this-boot"), patch.object(migration.time, "monotonic", return_value=100):
            self.assertTrue(migration.before_deadline(state))
            for change in ({"phase": "aborted"}, {"authoritative": "target"}, {"freeze_deadline": 99},
                           {"anchor": {"boot_id": "earlier-boot"}}):
                self.assertFalse(migration.before_deadline({**state, **change}))

    def test_target_start_fences_and_validates_before_allowing_writes(self):
        state = {"phase": "target-committed", "target_template": {"spec": "new"}}
        events = []
        def proof(value, before_start=False):
            events.append("prestart-proof" if before_start else "retained-proof")
            return {"relation_counts": {"settings": 2}}
        def persist(name, value):
            events.append("saved-" + value["phase"])
        with (patch.object(migration, "get", return_value={"spec": {"template": state["target_template"]}}),
              patch.object(migration, "target_proof", side_effect=proof),
              patch.object(migration, "zero_controllers", side_effect=lambda value: events.append("zeroed")),
              patch.object(migration, "fence_original", side_effect=lambda value: events.append("fenced")),
              patch.object(migration, "save", side_effect=persist),
              patch.object(migration, "patch_writer", side_effect=lambda value, replicas: events.append("started")),
              patch.object(migration, "ready_writer", return_value={"pod_uid": "new"})):
            self.assertEqual(migration.start_target(state)[0], {"pod_uid": "new"})
        self.assertEqual(events, ["retained-proof", "zeroed", "fenced", "prestart-proof", "saved-target-starting", "started"])

    def test_failure_to_fence_does_not_start_target(self):
        state = {"phase": "target-committed", "target_template": {"spec": "new"}}
        with (patch.object(migration, "get", return_value={"spec": {"template": state["target_template"]}}),
              patch.object(migration, "target_proof", return_value={}),
              patch.object(migration, "zero_controllers"),
              patch.object(migration, "fence_original", side_effect=migration.capture.CaptureError("not fenced")),
              patch.object(migration, "patch_writer", side_effect=AssertionError("second writer started")),
              self.assertRaises(migration.capture.CaptureError)):
            migration.start_target(state)

    def test_target_recovery_does_not_overwrite_live_database(self):
        state = {"phase": "target-starting", "target_template": {"spec": "new"}, "anchor": {}}
        events = []
        with (patch.object(migration, "get", return_value={"spec": {"template": state["target_template"]}}),
              patch.object(migration, "target_proof", side_effect=lambda value, **kwargs: events.append(kwargs) or {}),
              patch.object(migration, "identity_matches", return_value=False),
              patch.object(migration, "owned_pods", return_value=[]),
              patch.object(migration, "zero_controllers", side_effect=AssertionError("healthy target stopped")),
              patch.object(migration, "save", side_effect=AssertionError("authority changed")),
              patch.object(migration, "patch_writer"),
              patch.object(migration, "ready_writer", return_value={"pod_uid": "new"})):
            migration.start_target(state)
        self.assertEqual(events, [{}])  # No prestart byte equality once writes may have begun.

    def test_completed_boot_recovery_does_not_require_zero_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "state.json").touch()
            with (patch.object(migration, "RECOVERY", Path(directory)),
                  patch.object(migration, "journal", contextlib.nullcontext),
                  patch.object(migration, "load", return_value={"phase": "complete", "authoritative": "target"}),
                  patch.object(migration, "target_proof"),
                  patch.object(migration, "ready_writer", side_effect=AssertionError("boot readiness checked")),
                  patch.object(migration, "start_target", side_effect=AssertionError("completed migration changed"))):
                migration.recover()

    def test_completed_recovery_does_not_contend_with_daily_backup_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "state.json").touch()
            with (patch.object(sys, "argv", [str(migration.__file__), "--recover"]),
                  patch.object(migration.platform, "system", return_value="Linux"),
                  patch.object(migration.platform, "node", return_value="openmediavault"),
                  patch.object(migration.os, "geteuid", return_value=0),
                  patch.object(migration.os, "umask"), patch.dict(os.environ),
                  patch.object(migration, "RECOVERY", Path(directory)),
                  patch.object(migration.capture, "managed_directory"),
                  patch.object(migration, "journal", contextlib.nullcontext),
                  patch.object(migration, "load", return_value={"phase": "complete"}),
                  patch.object(migration.capture, "lock", side_effect=AssertionError("backup lock touched"))):
                migration.main()

    def test_pod_deletion_carries_original_uid_and_grace_period(self):
        observed = []
        def request(*arguments):
            observed.append((arguments, json.loads(Path(arguments[-1]).read_text())))
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(migration, "RECOVERY", Path(directory)),
                  patch.object(migration, "get", return_value={"metadata": {"uid": migration.capture.POD_UID}}),
                  patch.object(migration, "kube", side_effect=request),
                  patch.object(migration, "identity_matches", return_value=False),
                  patch.object(migration, "owned_pods", return_value=[])):
                migration.fence_original({"anchor": {}})
        self.assertEqual(observed[0][0][0:2], ("delete", "--raw"))
        self.assertEqual(observed[0][1], {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 60,
                                        "preconditions": {"uid": migration.capture.POD_UID}})

    def test_reused_original_pod_name_is_not_deleted(self):
        with (patch.object(migration, "get", return_value={"metadata": {"uid": "different-pod"}}),
              patch.object(migration, "kube", side_effect=AssertionError("replacement Pod deleted")),
              self.assertRaises(migration.capture.CaptureError)):
            migration.fence_original({"anchor": {}})


LINUX_INTEGRATION = (platform.system() == "Linux" and os.geteuid() == 0 and
                     os.environ.get("K3S_GPT_SANDBOX_INTEGRATION") == "1")


@unittest.skipUnless(LINUX_INTEGRATION, "requires disposable Linux guest with a writable cgroup v2 hierarchy")
class FrozenCutoverTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.base = Path(directory)
        self.source = self.base / "source"
        (self.source / "app/config").mkdir(parents=True)
        (self.source / "app/db").mkdir()
        (self.source / "app/config/settings.json").write_text('{"private-setting":"fixture-only"}')
        for path in (self.source / "app/config", self.source / "app/db"):
            path.chmod(0o755)
        (self.source / "app/config/settings.json").chmod(0o644)
        writer = r'''
import os, sqlite3, sys, time
sqlite3.connect(':memory:').close()
os.chroot(sys.argv[1]); os.chdir('/')
c = sqlite3.connect('/app/db/state.sqlite')
c.execute('PRAGMA journal_mode=' + sys.argv[2])
c.execute('PRAGMA cache_size=4')
c.execute('CREATE TABLE committed(value INTEGER)')
c.execute('CREATE TABLE pending(value BLOB)')
c.execute('INSERT INTO committed VALUES(42)'); c.commit()
c.execute('BEGIN IMMEDIATE')
for i in range(100): c.execute('INSERT INTO pending VALUES(?)', (b'uncommitted'*500,))
print('ready', flush=True)
i = 0
while True:
    with open('/app/db/heartbeat', 'w') as f: f.write(str(i))
    i += 1; time.sleep(.002)
'''
        self.child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-u", "-c", writer, str(self.source),
                                       getattr(self, "journal_mode", "WAL")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_child)
        import select
        self.assertTrue(select.select([self.child.stdout], [], [], 5)[0], "fixture writer did not start")
        self.assertEqual(self.child.stdout.readline().strip(), "ready")
        cid = "c" * 64
        self.group = migration.CGROUP_ROOT / ("gpt-test-" + uuid.uuid4().hex + "-" + cid)
        self.group.mkdir()
        self.addCleanup(self.group.rmdir)
        (self.group / "cgroup.procs").write_text(str(self.child.pid))
        attrs = self.group.stat()
        self.anchor = {"pid": self.child.pid, "process_start": migration.capture.process_start(self.child.pid),
                       "container_id": cid, "boot_id": migration.boot_id(), "cgroup_parts": [self.group.name],
                       "cgroup_identity": [attrs.st_dev, attrs.st_ino]}
        self.recovery = self.base / "recovery"
        self.captures = self.base / "captures"
        self.live = self.base / "live"
        for path in (self.recovery, self.captures, self.live, self.live / "config", self.live / "db"):
            path.mkdir(mode=0o700)
        self.stack.enter_context(patch.object(migration, "RECOVERY", self.recovery))
        self.stack.enter_context(patch.object(migration, "LIVE", self.live))
        self.stack.enter_context(patch.object(migration.capture, "CAPTURES", self.captures))
        self.state = {"phase": "prepared", "authoritative": "source", "anchor": self.anchor, "migration_id": uuid.uuid4().hex}
        migration.save("state.json", self.state)

    def stop_child(self):
        if self.child.poll() is None:
            self.child.kill()  # SIGKILL must work even in a frozen cgroup.
        self.child.wait(timeout=5)
        self.child.stdout.close()
        self.child.stderr.close()

    def tearDown(self):
        # unittest cleanups are LIFO; empty the cgroup before rmdir.
        self.stop_child()

    def test_frozen_wal_copy_recovers_committed_data_and_preserves_raw_evidence(self):
        # A process started by a container exec/preStop must also be fenced.
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        def stop_helper():
            if helper.poll() is None:
                helper.kill()
            helper.wait(timeout=5)
        self.addCleanup(stop_helper)
        (self.group / "cgroup.procs").write_text(str(helper.pid))
        result = migration.capture_frozen(self.state)
        self.assertEqual(result["authoritative"], "target")
        with migration.cgroup(self.anchor) as descriptor:
            self.assertTrue(migration.frozen(descriptor))
        proof = migration.load("copy-validation.json")
        self.assertEqual(proof["relation_counts"], {"committed": 1, "pending": 0})
        snapshot = Path(result["capture_directory"])
        self.assertEqual(migration.capture.snapshot_files(snapshot / "raw"), proof["raw_files"])
        self.assertEqual(migration.capture.snapshot_files(self.live), proof["files"])
        self.assertEqual((self.live / "config/settings.json").read_bytes(), (self.source / "app/config/settings.json").read_bytes())
        with sqlite3.connect((self.live / "db/state.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True) as database:
            self.assertEqual(database.execute("SELECT value FROM committed").fetchall(), [(42,)])
            self.assertEqual(database.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
        original = self.source / "app/db/heartbeat"
        first = original.read_bytes()
        time.sleep(.025)
        self.assertEqual(original.read_bytes(), first)
        with self.assertRaises(migration.capture.CaptureError):
            migration.thaw_source(result)
        # The real pidfd signal must terminate a frozen process without giving
        # it an opportunity to accept another write after the final capture.
        with patch.object(migration, "get", return_value=None), patch.object(migration, "owned_pods", return_value=[]):
            migration.fence_original(result)
        self.assertEqual(self.child.wait(timeout=5), -signal.SIGKILL)
        self.assertEqual(helper.wait(timeout=5), -signal.SIGKILL)

    def test_pause_timeout_thaws_source_and_rejects_stale_target(self):
        actual_normalize = migration.normalize_database
        def expire(*args):
            proof = actual_normalize(*args)
            state = migration.load("state.json")
            state["freeze_deadline"] = time.monotonic() - 1
            migration.save("state.json", state)
            self.assertTrue(migration.watchdog_tick())
            return proof
        with patch.object(migration, "normalize_database", side_effect=expire), self.assertRaises(migration.capture.CaptureError):
            migration.capture_frozen(self.state)
        self.assertEqual(migration.load("state.json")["phase"], "aborted")
        self.assertEqual(migration.load("state.json")["authoritative"], "source")
        with migration.cgroup(self.anchor) as descriptor:
            self.assertFalse(migration.frozen(descriptor))
        self.assertIsNone(self.child.poll())

    def test_cgroup_identity_mismatch_is_refused_without_freezing(self):
        anchor = {**self.anchor, "cgroup_identity": [0, 1]}
        with migration.cgroup(self.anchor) as descriptor:
            self.assertFalse(migration.frozen(descriptor))
        with self.assertRaises(migration.capture.CaptureError):
            with migration.cgroup(anchor):
                self.fail("replaced cgroup accepted")
        with migration.cgroup(self.anchor) as descriptor:
            self.assertFalse(migration.frozen(descriptor))

    def test_independent_watchdog_releases_pause_while_copier_waits(self):
        state = {**self.state, "phase": "frozen", "freeze_deadline": time.monotonic() + .1}
        migration.save("state.json", state)
        with migration.cgroup(self.anchor) as descriptor:
            migration.set_freeze(descriptor, True)
        watcher = os.fork()
        if watcher == 0:
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if migration.watchdog_tick():
                        os._exit(0)
                    time.sleep(.005)
            finally:
                os._exit(1)
        try:
            # This process is the blocked copier; only the independent watcher
            # can thaw the source and durably cancel acceptance of its capture.
            _, status = os.waitpid(watcher, 0)
            watcher = None
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertEqual(migration.load("state.json")["phase"], "aborted")
            with migration.cgroup(self.anchor) as descriptor:
                self.assertFalse(migration.frozen(descriptor))
        finally:
            if watcher is not None:
                os.kill(watcher, signal.SIGKILL)
                os.waitpid(watcher, 0)


@unittest.skipUnless(LINUX_INTEGRATION, "requires disposable Linux guest with a writable cgroup v2 hierarchy")
class FrozenRollbackJournalTests(FrozenCutoverTests):
    journal_mode = "DELETE"


if __name__ == "__main__":
    unittest.main()
