#!/usr/bin/env python3
"""Serialize native PostgreSQL dumps, VolSync uploads, and repository checks."""

import base64
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time


module_spec = importlib.util.spec_from_file_location(
    "board_games_backup", Path(__file__).with_name("backup-board-games.py"),
)
native = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(native)
setup = native.setup
NS = setup.NAMESPACE
HISTORY = setup.BASE / "backup-staging-history/data-postgres-0"
ACTIVE = setup.RECOVERY / "daily-active.json"
LATEST = setup.RECOVERY / "latest-daily-backup.json"
PROOF = setup.RECOVERY / "retention-proof.json"


def completed(source, tag):
    if not source:
        return False
    status = source.get("status", {})
    return (source.get("spec", {}).get("trigger") == {"manual": tag} and
            not source["spec"].get("paused") and
            status.get("lastManualSync") == tag and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and
            status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(condition.get("type") == "Synchronizing" and condition.get("status") == "False"
                and condition.get("reason") == "WaitingForManual"
                for condition in status.get("conditions", [])))


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path, value):
    setup.private_write(path, json.dumps(value, indent=2) + "\n")
    sync_directory(setup.RECOVERY)


def saved(path):
    return json.loads(setup.private_read(path))


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"(?:initial|daily)-[0-9a-f]{32}", value)


def wait_for_dependencies():
    deadline = time.monotonic() + 180
    announced = False
    while time.monotonic() < deadline:
        try:
            probe = setup.kube(
                "get", "pod/postgres-0", "deployment/restic-server", "-n", NS,
                "--ignore-not-found=true", "--request-timeout=5s", "-o", "json",
                capture=True, sensitive=True, check=False, timeout=10,
            )
        except subprocess.TimeoutExpired:
            probe = None
        if probe and not probe.returncode and probe.stdout.strip():
            objects = json.loads(probe.stdout)
            objects = objects.get("items", [objects])
            postgres = next((item for item in objects if item["kind"] == "Pod"), {})
            server = next((item for item in objects if item["kind"] == "Deployment"), {})
            if (any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in postgres.get("status", {}).get("conditions", [])) and
                    server.get("status", {}).get("availableReplicas", 0) >= 1):
                return
        if not announced:
            print("waiting up to 180 seconds for the Kubernetes API, PostgreSQL, and backup endpoint", flush=True)
            announced = True
        time.sleep(5)
    raise setup.SetupError("the API, PostgreSQL, or backup endpoint did not become ready")


def preflight():
    initial = saved(setup.RECOVERY / "initial-backup.json")["backup_id"]
    if not isinstance(initial, str) or not re.fullmatch(r"initial-[0-9a-f]{32}", initial):
        raise setup.SetupError("the saved initial backup generation is invalid")
    restored = saved(setup.RECOVERY / "initial-database-restore-result.json")
    if (restored.get("result") != "passed" or restored.get("backup_id") != initial or
            restored.get("database") != "board_games" or restored.get("role") != "board_games"):
        raise setup.SetupError("the initial database restore has not been verified")
    nodes = json.loads(setup.kube("get", "nodes", "-o", "json", capture=True).stdout)
    if [node["metadata"]["name"] for node in nodes["items"]] != ["openmediavault"]:
        raise setup.SetupError("additional nodes require the later approval gate")
    for claim_name, volume_name, path in (
            ("data-postgres-0", setup.LIVE_PV, None),
            ("postgres-backup-staging", "board-games-postgres-backup-staging", native.STAGING)):
        claim = setup.get_object("pvc", claim_name, NS)
        volume = setup.get_object("pv", volume_name)
        if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != volume_name or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
                (path and volume["spec"].get("local", {}).get("path") != str(path))):
            raise setup.SetupError("database or staging PVC binding changed: " + claim_name)
    for path in (native.STAGING, setup.BASE / "backups/data-postgres-0",
                 setup.BASE / "backup-cache/data-postgres-0/source"):
        setup.checked_directory(path)
        if not path.is_dir():
            raise setup.SetupError("approved array directory is missing: " + str(path))
    if shutil.disk_usage(native.STAGING).free < 20 * 1024**3:
        raise setup.SetupError("less than 20 GiB remains on the backup array")
    source = setup.get_object("replicationsource", native.SOURCE, NS)
    if not source:
        raise setup.SetupError("the verified VolSync source is missing")
    native.managed(source)
    expected = native.read_template(native.MANIFESTS / "replication-source.yaml")["spec"]
    expected.pop("paused")
    expected.pop("trigger")
    if not native.contains_fields(source["spec"], expected):
        raise setup.SetupError("VolSync placement, repository, or seven-day retention policy changed")
    repository = saved(setup.RECOVERY / (native.CLIENT_SECRET + ".secret.json"))
    current_secret = setup.get_object("secret", native.CLIENT_SECRET, NS, sensitive=True)
    if not current_secret or current_secret.get("data") != repository.get("data"):
        raise setup.SetupError("repository credentials changed; update independent recovery keys before proceeding")
    ca = setup.get_object("secret", "restic-root-ca", NS, sensitive=True)
    if (not ca or base64.b64decode(ca["data"]["tls.crt"], validate=True).decode() !=
            setup.private_read(setup.RECOVERY / "ca.crt")):
        raise setup.SetupError("repository trust root changed; update independent recovery keys before proceeding")
    checks = json.loads(setup.kube(
        "get", "pods", "-n", NS, "-l", "app.kubernetes.io/name=postgres-backup-check",
        "-o", "json", capture=True,
    ).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") or
           pod["metadata"].get("deletionTimestamp") for pod in checks):
        raise setup.SetupError("an earlier checksum check is still active; retry after its Pod stops")
    return initial, source


def run_state(initial, source):
    if ACTIVE.exists() or ACTIVE.is_symlink():
        state = saved(ACTIVE)
        if (not re.fullmatch(r"daily-[0-9a-f]{32}", state.get("backup_id", "")) or
                not valid_id(state.get("previous_id")) or state["previous_id"] == state["backup_id"] or
                state.get("job_name") != "postgres-backup-" + state["backup_id"]):
            raise setup.SetupError("saved daily run state is invalid; preserving it")
        return state
    if LATEST.exists() or LATEST.is_symlink():
        latest = saved(LATEST)
        if latest.get("result") != "passed":
            raise setup.SetupError("the latest saved daily backup has no successful verification")
        previous = latest["backup_id"]
    else:
        previous = initial
    if not valid_id(previous) or not completed(source, previous):
        raise setup.SetupError("the previous verified upload is incomplete or its trigger changed")
    if native.inspect_artifacts(previous) is None:
        raise setup.SetupError("the previous staged dump is missing")
    identifier = "daily-" + secrets.token_hex(16)
    state = {"backup_id": identifier, "previous_id": previous,
             "job_name": "postgres-backup-" + identifier,
             "started_at": datetime.now(timezone.utc).isoformat()}
    save(ACTIVE, state)
    return state


def reserve_source(state):
    for attempt in range(5):
        source = setup.get_object("replicationsource", native.SOURCE, NS)
        if not source:
            raise setup.SetupError("the VolSync source disappeared")
        tag = source["spec"].get("trigger", {}).get("manual")
        if tag != state["previous_id"]:
            break
        if not completed(source, tag):
            raise setup.SetupError("the previous upload is still active or paused; preserving staging")
        result = setup.kube("patch", "replicationsource", native.SOURCE, "-n", NS, "--type=merge", "-p", json.dumps({
            "metadata": {"resourceVersion": source["metadata"]["resourceVersion"]},
            "spec": {"paused": True, "trigger": {"manual": state["backup_id"]}},
        }), capture=True, check=False)
        if not result.returncode:
            print(result.stdout, end="", flush=True)
            source = setup.get_object("replicationsource", native.SOURCE, NS)
            break
        if "(Conflict)" not in result.stderr:
            raise setup.SetupError("could not pause the VolSync source; staging is preserved")
        time.sleep(0.2)
    else:
        raise setup.SetupError("VolSync source kept changing; staging is preserved")
    if not source:
        raise setup.SetupError("the VolSync source disappeared")
    if source["spec"].get("trigger") != {"manual": state["backup_id"]}:
        raise setup.SetupError("the VolSync trigger changed outside this daily run")
    # A resumed upload may already be running. Its published dump is immutable.
    if not source["spec"].get("paused"):
        if native.inspect_artifacts(state["backup_id"]) is None:
            raise setup.SetupError("an upload is unpaused without this run's completed dump")
        return
    movers = json.loads(setup.kube(
        "get", "pods", "-n", NS, "-l", "batch.kubernetes.io/job-name=volsync-src-" + native.SOURCE,
        "-o", "json", capture=True,
    ).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Failed", "Succeeded") for pod in movers):
        raise setup.SetupError("a VolSync mover remains active; preserving staging")


def archive_previous(state):
    setup.checked_directory(HISTORY)
    if HISTORY.exists():
        attributes = HISTORY.stat()
        if attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("staging history must be root-owned with mode 0700")
    else:
        HISTORY.mkdir(parents=True, mode=0o700)
        os.chmod(HISTORY, 0o700)
    current = native.STAGING / "current"
    previous = HISTORY / state["previous_id"]
    if current.exists():
        identifier = (current / "backup-id").read_text().strip()
        if identifier == state["backup_id"]:
            native.inspect_artifacts(identifier)
            return
        if identifier != state["previous_id"] or previous.exists():
            raise setup.SetupError("staging or its retained history differs from this run")
        source = setup.get_object("replicationsource", native.SOURCE, NS)
        if not source["spec"].get("paused") or source["spec"].get("trigger") != {"manual": state["backup_id"]}:
            raise setup.SetupError("refusing to change staging while VolSync is unpaused")
        native.inspect_artifacts(identifier)
        current.rename(previous)
        sync_directory(native.STAGING)
        sync_directory(HISTORY)
        print("previous verified dump retained outside the upload PVC: " + identifier, flush=True)
    if native.inspect_artifacts(state["previous_id"], previous) is None:
        raise setup.SetupError("the previous staged dump and its retained history are missing")
    job = setup.get_object("job", state["job_name"], NS)
    if not job and any(native.STAGING.iterdir()):
        raise setup.SetupError("unowned partial staging remains; preserving it for inspection")


def preserve_application_credentials():
    secret = setup.get_object("secret", "board-games-secrets", NS, sensitive=True)
    if not secret or not secret.get("data", {}).get("POSTGRES_PASSWORD"):
        raise setup.SetupError("application database credentials are missing")
    # Keep the initial restore's credentials immutable; save current ones separately.
    portable = {"apiVersion": "v1", "kind": "Secret", "type": secret.get("type", "Opaque"),
                "metadata": {"name": "board-games-secrets", "namespace": NS}, "data": secret["data"]}
    save(setup.RECOVERY / "latest-board-games-secrets.secret.json", portable)


def expire_staging_history(current):
    captured = datetime.fromisoformat(json.loads((current / "metadata.json").read_text())["captured_at"])
    if captured.tzinfo is None:
        raise setup.SetupError("dump metadata has no capture timezone")
    cutoff = captured - timedelta(days=7)
    for directory in list(HISTORY.iterdir()):
        if directory.name.startswith(".expired-") and valid_id(directory.name.removeprefix(".expired-")):
            remove_expired_history(directory)
            continue
        if not valid_id(directory.name):
            continue
        native.inspect_artifacts(directory.name, directory)
        timestamp = datetime.fromisoformat(json.loads((directory / "metadata.json").read_text())["captured_at"])
        if timestamp.tzinfo is None:
            raise setup.SetupError("staging history has no capture timezone")
        if timestamp < cutoff:
            # Mark a validated generation atomically before removal. A power
            # loss partway through deletion leaves a recognizable cleanup task.
            expired = HISTORY / (".expired-" + directory.name)
            if expired.exists() or expired.is_symlink():
                raise setup.SetupError("expired staging history name is already occupied")
            directory.rename(expired)
            sync_directory(HISTORY)
            remove_expired_history(expired)
            print("expired validated staging history: " + directory.name, flush=True)
    sync_directory(HISTORY)


def remove_expired_history(directory):
    setup.checked_directory(directory)
    attributes = directory.stat()
    if attributes.st_uid != 999 or stat.S_IMODE(attributes.st_mode) != 0o700:
        raise setup.SetupError("expired staging history has unexpected ownership or mode")
    artifacts = list(directory.iterdir())
    for artifact in artifacts:
        attributes = artifact.lstat()
        if (artifact.name not in native.ARTIFACTS or not stat.S_ISREG(attributes.st_mode) or
                attributes.st_uid != 999 or stat.S_IMODE(attributes.st_mode) != 0o600):
            raise setup.SetupError("unexpected file in expired staging history; preserving it")
    # Only the remaining recognized regular files; never recursively remove data.
    for artifact in artifacts:
        artifact.unlink()
    sync_directory(directory)
    directory.rmdir()
    sync_directory(HISTORY)


def backup():
    wait_for_dependencies()
    initial, source = preflight()
    state = run_state(initial, source)
    reserve_source(state)
    archive_previous(state)
    preserve_application_credentials()
    checksums = native.native_dump(state["backup_id"], job_name=state["job_name"])
    if {path.name for path in native.STAGING.iterdir()} != {"current"}:
        raise setup.SetupError("unexpected files accompany the completed dump; preserving staging")
    native.upload(state["backup_id"], result_path=setup.RECOVERY / "latest-daily-volsync-result.json")
    prove_retention = not (PROOF.exists() or PROOF.is_symlink())
    if not prove_retention and saved(PROOF).get("result") != "passed":
        raise setup.SetupError("the saved retention proof is invalid; preserving it")
    check_job = native.run_check(native.MANIFESTS / "verify-job.yaml", {
        "EXPECTED_BACKUP_ID": state["backup_id"], "EXPECTED_SHA256SUMS": checksums,
        "VERIFY_RETENTION_PROOF": "true" if prove_retention else "false",
    }, "daily native backup and retention verification")
    logs = setup.kube("logs", "-n", NS, "job/" + check_job, "--pod-running-timeout=5s", capture=True).stdout
    snapshots = re.findall(r"^native backup snapshot verified: ([0-9a-f]{64})$", logs, re.MULTILINE)
    if len(snapshots) != 1 or not completed(setup.get_object("replicationsource", native.SOURCE, NS), state["backup_id"]):
        raise setup.SetupError("verified snapshot evidence or VolSync completion changed")
    finished = datetime.now(timezone.utc).isoformat()
    if prove_retention:
        line = next((line for line in logs.splitlines() if line.startswith("scratch retention proof passed:")), None)
        if not line:
            raise setup.SetupError("the scratch retention proof did not complete")
        save(PROOF, {"result": "passed", "verified_at": finished, "job": check_job, "evidence": line})
    expire_staging_history(native.STAGING / "current")
    dump = setup.get_object("job", state["job_name"], NS)
    save(setup.RECOVERY / "latest-daily-dump.job.json", dump)
    setup.kube("patch", "job", state["job_name"], "-n", NS, "--type=json", "-p", json.dumps([
        {"op": "test", "path": "/metadata/uid", "value": dump["metadata"]["uid"]},
        {"op": "add", "path": "/spec/ttlSecondsAfterFinished", "value": 604800},
    ]))
    result = {**state, "result": "passed", "completed_at": finished,
              "snapshot": snapshots[0], "check_job": check_job, "retention": "within 7d"}
    save(setup.RECOVERY / (state["backup_id"] + ".result.json"), result)
    save(LATEST, result)
    ACTIVE.unlink()
    sync_directory(setup.RECOVERY)
    print("daily backup verified: " + state["backup_id"], flush=True)
    print("native dump, VolSync upload/retention, and full artifact verification completed in sequence", flush=True)


def main():
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    descriptor = os.open(setup.RECOVERY / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise setup.SetupError("another board-games backup/setup command is running") from error
        try:
            backup()
        except (setup.SetupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            native.diagnostics()
            raise


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"daily backup stopped: {error}", file=sys.stderr)
        sys.exit(1)
