#!/usr/bin/env python3
"""Validate the initial board-games backup using isolated array-backed PVCs."""

import base64
import csv
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time


module_spec = importlib.util.spec_from_file_location(
    "board_games_backup", Path(__file__).with_name("backup-board-games.py"),
)
backup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backup)
setup = backup.setup

NS = setup.NAMESPACE
MANIFESTS = backup.MANIFESTS / "restore"
BASE = setup.BASE / "restore-validation/data-postgres-0/initial"
ARTIFACTS = BASE / "artifacts"
DATABASE = BASE / "database"
DESTINATION = "postgres-restore-initial"
JOB = "postgres-restore-validation-initial"


def completed(resource, tag):
    status = resource.get("status", {})
    return (status.get("lastManualSync") == tag and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and
            status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(condition.get("type") == "Synchronizing" and condition.get("status") == "False"
                and condition.get("reason") == "WaitingForManual"
                for condition in status.get("conditions", [])))


def restore_state():
    nodes = json.loads(setup.kube("get", "nodes", "-o", "json", capture=True).stdout)
    if [node["metadata"]["name"] for node in nodes["items"]] != ["openmediavault"]:
        raise setup.SetupError("other nodes require the later approval gate")
    claim = setup.get_object("pvc", "data-postgres-0", NS)
    volume = setup.get_object("pv", setup.LIVE_PV)
    if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != setup.LIVE_PV or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"]):
        raise setup.SetupError("production database binding or Retain safeguard changed")
    backup_id = json.loads(setup.private_read(setup.RECOVERY / "initial-backup.json"))["backup_id"]
    if not re.fullmatch(r"initial-[0-9a-f]{32}", backup_id):
        raise setup.SetupError("invalid saved initial backup generation")
    saved = json.loads(setup.private_read(setup.RECOVERY / "initial-volsync-result.json"))
    source = setup.get_object("replicationsource", backup.SOURCE, NS)
    if (not source or not completed(saved, backup_id) or not completed(source, backup_id) or
            source["spec"].get("trigger", {}) != {"manual": backup_id}):
        raise setup.SetupError("the initial upload is incomplete or its source trigger changed")
    cutoff = saved["status"]["lastSyncTime"]
    if datetime.fromisoformat(cutoff.replace("Z", "+00:00")).tzinfo is None:
        raise setup.SetupError("the saved initial upload timestamp has no timezone")
    expected_checksums = backup.inspect_artifacts(backup_id)
    if expected_checksums is None:
        raise setup.SetupError("initial staged artifacts are missing; cannot compare the recovery copy")
    state = {
        "backup_id": backup_id, "restore_as_of": cutoff,
        "bootstrap_user": "restore_check_" + backup_id.removeprefix("initial-"),
    }
    state_path = setup.RECOVERY / "initial-restore.json"
    if state_path.exists() or state_path.is_symlink():
        if json.loads(setup.private_read(state_path)) != state:
            raise setup.SetupError("saved initial restore generation differs; preserving its data")
    else:
        setup.private_write(state_path, json.dumps(state, indent=2) + "\n")
    return state, expected_checksums


def resources_for(state):
    rendered = setup.kube("create", "--dry-run=client", "-k", str(MANIFESTS),
                          "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if not resources:
        raise setup.SetupError("restore manifest bundle is empty")
    existing = {}
    for resource in resources:
        kind = resource["kind"]
        name = resource["metadata"]["name"]
        if kind == "ReplicationDestination":
            resource["spec"]["restic"]["restoreAsOf"] = state["restore_as_of"]
        group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        current = setup.get_object(kind.lower() + ("." + group if group else ""), name,
                                   resource["metadata"].get("namespace"))
        existing[name] = current
        if current:
            backup.managed(current)
            expected = resource["spec"].copy()
            if kind == "ReplicationDestination":
                expected.pop("paused")
                expected.pop("trigger")
                expected["restic"] = expected["restic"].copy()
                for flag in ("cleanupCachePVC", "enableFileDeletion"):
                    if current["spec"]["restic"].get(flag, False):
                        raise setup.SetupError("existing restore destination enables deletion")
                    # The API omits these default-false fields when reading back.
                    expected["restic"].pop(flag, None)
                trigger = current["spec"].get("trigger", {})
                if (set(trigger) != {"manual"} or trigger["manual"] not in (
                        "controlled-by-restore-runner", state["backup_id"]) or
                        (trigger["manual"] == "controlled-by-restore-runner" and
                         not current["spec"].get("paused"))):
                    raise setup.SetupError("existing restore destination trigger differs")
            if kind == "NetworkPolicy":
                for direction in ("ingress", "egress"):
                    if current["spec"].get(direction):
                        raise setup.SetupError("existing validation policy permits unexpected traffic")
                    expected.pop(direction, None)
            if not backup.contains_fields(current["spec"], expected):
                raise setup.SetupError("existing restore resource differs: " + name)
    return resources, existing


def prepare_storage(resources, existing):
    storage_class = setup.get_object("storageclass", "board-games-backup-local")
    if (not storage_class or storage_class.get("provisioner") != "kubernetes.io/no-provisioner" or
            storage_class.get("volumeBindingMode") != "Immediate"):
        raise setup.SetupError("the approved static backup storage class is missing or changed")
    setup.checked_directory(BASE)
    if shutil.disk_usage(setup.BASE).free < 20 * 1024**3:
        raise setup.SetupError("less than 20 GiB remains on the approved array")
    for resource in resources:
        if resource["kind"] != "PersistentVolume":
            continue
        path = Path(resource["spec"]["local"]["path"])
        if path not in (ARTIFACTS, DATABASE, BASE / "cache"):
            raise setup.SetupError("unexpected restore storage path")
        setup.checked_directory(path)
        current = existing[resource["metadata"]["name"]]
        if path.exists():
            if any(path.iterdir()) and not current:
                raise setup.SetupError("nonempty restore directory has no managed PV: " + str(path))
            attributes = path.stat()
            if (attributes.st_uid != 999 or attributes.st_gid != 999 or
                    stat.S_IMODE(attributes.st_mode) != 0o2770):
                raise setup.SetupError("existing restore directory ownership or mode differs: " + str(path))
        else:
            path.mkdir(parents=True, mode=0o2770)
            os.chown(path, 999, 999)
            os.chmod(path, 0o2770)
        print("array restore directory verified: " + str(path), flush=True)
    if not existing[DESTINATION] and any(ARTIFACTS.iterdir()):
        raise setup.SetupError("restore artifacts exist without their destination; preserving them")
    order = {"PersistentVolume": 0, "PersistentVolumeClaim": 1,
             "NetworkPolicy": 2, "ReplicationDestination": 3}
    for resource in sorted(resources, key=lambda item: order[item["kind"]]):
        if resource["kind"] == "ReplicationDestination" and existing[DESTINATION]:
            continue
        setup.kube("apply", "-f", "-", stdin=json.dumps(resource))
    for name, volume_name, path in (
            ("postgres-restore-artifacts", "board-games-postgres-restore-artifacts", ARTIFACTS),
            ("postgres-restore-database", "board-games-postgres-restore-database", DATABASE)):
        setup.kube("wait", "-n", NS, "pvc/" + name, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s")
        claim = setup.get_object("pvc", name, NS)
        volume = setup.get_object("pv", volume_name)
        if (claim["spec"].get("volumeName") != volume_name or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("local", {}).get("path") != str(path)):
            raise setup.SetupError("restore PVC binding differs: " + name)


def restore_artifacts(state, expected_checksums):
    destination = setup.get_object("replicationdestination", DESTINATION, NS)
    if destination["spec"].get("paused"):
        if any(ARTIFACTS.iterdir()):
            raise setup.SetupError("paused restore destination already contains artifacts; preserving them")
        setup.kube("patch", "replicationdestination", DESTINATION, "-n", NS, "--type=merge", "-p", json.dumps({
            "metadata": {"resourceVersion": destination["metadata"]["resourceVersion"]},
            "spec": {"paused": False, "trigger": {"manual": state["backup_id"]}},
        }))
    deadline = time.monotonic() + 630
    while True:
        destination = setup.get_object("replicationdestination", DESTINATION, NS)
        if (not destination or destination["spec"].get("trigger", {}).get("manual") != state["backup_id"] or
                destination["spec"].get("paused")):
            raise setup.SetupError("the restore destination disappeared or its trigger changed")
        if completed(destination, state["backup_id"]):
            break
        if time.monotonic() >= deadline:
            raise setup.SetupError("VolSync restore is still pending; destination data is preserved")
        time.sleep(2)
    candidates = [path for path in (ARTIFACTS / "current", ARTIFACTS / "data/current") if path.exists()]
    if len(candidates) != 1:
        raise setup.SetupError("VolSync did not restore exactly one native backup directory")
    current = candidates[0]
    if backup.inspect_artifacts(state["backup_id"], current) != expected_checksums:
        raise setup.SetupError("restored artifacts differ from the initial backup checksum manifest")
    setup.private_write(setup.RECOVERY / "initial-volsync-restore-result.json",
                        json.dumps(destination, indent=2) + "\n")
    print("VolSync restored all 7 artifacts; all checksums match the initial backup", flush=True)
    return current


def validation_job(state, current):
    template = backup.read_template(MANIFESTS / "validate-job.yaml")
    template["spec"]["template"]["spec"]["containers"][0]["env"].extend([
        {"name": "BACKUP_ID", "value": state["backup_id"]},
        {"name": "BOOTSTRAP_USER", "value": state["bootstrap_user"]},
        {"name": "BACKUP_SUBDIR", "value": current.relative_to(ARTIFACTS).as_posix()},
    ])
    existing = setup.get_object("job", JOB, NS)
    if existing:
        backup.managed(existing)
        if not backup.contains_fields(existing["spec"]["template"]["spec"], template["spec"]["template"]["spec"]):
            raise setup.SetupError("existing validation Job differs; preserving its database")
    else:
        if any(DATABASE.iterdir()):
            raise setup.SetupError("validation database already contains data without its Job; preserving it")
        setup.kube("create", "-f", "-", stdin=json.dumps(template))
    try:
        setup.wait_for_job(JOB, timeout=630, description="isolated PostgreSQL restore validation")
    finally:
        setup.kube("logs", "-n", NS, "job/" + JOB, check=False)
    result_path = DATABASE / "validation-result.json"
    attributes = result_path.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 999 or
            stat.S_IMODE(attributes.st_mode) != 0o600):
        raise setup.SetupError("validation result has unexpected ownership or permissions")
    result = json.loads(result_path.read_text())
    if (result.get("result") != "passed" or result.get("backup_id") != state["backup_id"] or
            result.get("database") != "board_games" or result.get("role") != "board_games" or
            not 170000 <= int(result.get("server_version_num", 0)) < 180000 or
            (DATABASE / "validation-logs/row-counts.csv").read_bytes() != (current / "row-counts.csv").read_bytes()):
        raise setup.SetupError("the restored database result or row counts differ")
    with (current / "row-counts.csv").open(newline="") as counts:
        relations = list(csv.DictReader(counts))
    setup.private_write(setup.RECOVERY / "initial-database-restore-result.json",
                        json.dumps({**state, **result, "relation_counts": relations}, indent=2) + "\n")
    print(f"isolated PostgreSQL restore verified: {len(relations)} relation counts match", flush=True)


def restore():
    state, expected_checksums = restore_state()
    # Rebuild separate restore Secrets from the saved recovery files.
    # This exercises the saved keys instead of the original client Secrets.
    repository = json.loads(setup.private_read(
        setup.RECOVERY / (backup.CLIENT_SECRET + ".secret.json"),
    ))
    if (repository.get("kind") != "Secret" or repository["metadata"].get("name") != backup.CLIENT_SECRET or
            repository["metadata"].get("namespace") != NS):
        raise setup.SetupError("saved repository recovery Secret differs")
    fields = {key: base64.b64decode(value, validate=True).decode()
              for key, value in repository["data"].items()}
    if (set(fields) != {"RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD"} or
            any(not value for value in fields.values()) or
            fields["RESTIC_REPOSITORY"] != backup.MOVER_REPOSITORY or
            fields["RESTIC_REST_USERNAME"] != "data-postgres-0"):
        raise setup.SetupError("saved repository credentials differ from the initial backup")
    setup.ensure_secret("postgres-restore-restic-auth", fields)
    setup.ensure_secret("postgres-restore-ca", {"ca.crt": setup.private_read(setup.RECOVERY / "ca.crt")})
    application = json.loads(setup.private_read(setup.RECOVERY / "board-games-secrets.secret.json"))
    if (application.get("kind") != "Secret" or application["metadata"].get("name") != "board-games-secrets" or
            application["metadata"].get("namespace") != NS):
        raise setup.SetupError("saved application recovery Secret differs")
    password = base64.b64decode(application["data"]["POSTGRES_PASSWORD"], validate=True).decode()
    if not password:
        raise setup.SetupError("saved application database password is empty")
    setup.ensure_secret("postgres-restore-validation-auth", {"PGPASSWORD": password})
    resources, existing = resources_for(state)
    prepare_storage(resources, existing)
    current = restore_artifacts(state, expected_checksums)
    validation_job(state, current)
    setup.kube("wait", "-n", NS, "pod/postgres-0", "--for=condition=Ready", "--timeout=60s")
    for deployment in ("board-games-api", "board-games-web"):
        setup.kube("rollout", "status", "-n", NS, "deployment/" + deployment, "--timeout=60s")
    print("initial backup and isolated restore verified; daily scheduling and independent credential storage remain pending", flush=True)


def diagnostics():
    setup.kube("get", "replicationdestination", DESTINATION, "-n", NS, "-o", "yaml", check=False)
    setup.kube("get", "pvc", "-n", NS, "-o", "wide", check=False)
    setup.kube("get", "pods", "-n", NS, "-o", "wide", check=False)
    pods = setup.kube("get", "pods", "-n", NS, "-l",
                      "batch.kubernetes.io/job-name=volsync-dst-" + DESTINATION,
                      "-o", "name", capture=True, check=False).stdout.splitlines()
    for pod in pods:
        setup.kube("logs", "-n", NS, pod, "--tail=60", "--pod-running-timeout=5s", check=False)


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
            restore()
        except (setup.SetupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            diagnostics()
            raise


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"restore validation stopped: {error}", file=sys.stderr)
        sys.exit(1)
