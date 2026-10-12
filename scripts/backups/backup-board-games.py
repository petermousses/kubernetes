#!/usr/bin/env python3
"""Create and verify the first board-games native PostgreSQL/VolSync backup."""

import fcntl
import hashlib
import importlib.util
import ipaddress
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
    "board_games_setup", Path(__file__).with_name("prepare-board-games.py"),
)
setup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(setup)

NS = setup.NAMESPACE
MANAGER = setup.MANAGER
MANIFESTS = setup.MANIFESTS / "postgres"
STAGING = setup.BASE / "backup-staging/data-postgres-0"
SOURCE = "postgres-backup"
DUMP_JOB = "postgres-native-backup-initial"
CLIENT_SECRET = "restic-data-postgres-0-mover"
# The standard namespace search list expands this before the LAN wildcard.
# This SAN already exists in the server certificate. VolSync 0.16.0 cannot
# override Pod dnsConfig, so do not rely on the standalone check's ndots:1.
MOVER_REPOSITORY = "rest:https://restic.board-games.svc:8000/data-postgres-0/"
ARTIFACTS = {
    "database.dump", "globals.sql", "row-counts.csv", "metadata.json",
    "archive-list.txt", "backup-id", "SHA256SUMS",
}


def read_template(filename):
    return json.loads(setup.kube(
        "create", "--dry-run=client", "-f", str(filename), "-o", "json", capture=True,
    ).stdout)


def service_ips():
    service = setup.get_object("service", "restic", NS)
    if not service:
        raise setup.SetupError("the verified Restic Service is missing")
    values = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    return ",".join(str(ipaddress.ip_address(value)) for value in values)


def run_check(template, fields, description):
    job = read_template(template)
    pod_spec = job["spec"]["template"]["spec"]
    # Both checks must exercise the default DNS policy used by the mover.
    pod_spec.pop("dnsConfig", None)
    pod_spec["dnsPolicy"] = "ClusterFirst"
    container = pod_spec["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": CLIENT_SECRET}}]
    container.setdefault("env", []).extend(
        {"name": key, "value": value}
        for key, value in {"EXPECTED_RESTIC_IPS": service_ips(), **fields}.items()
    )
    created = json.loads(setup.kube(
        "create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True,
    ).stdout)
    name = created["metadata"]["name"]
    print(f"job/{name} created ({description})", flush=True)
    try:
        setup.wait_for_job(name, timeout=630, description=description)
    finally:
        setup.kube("logs", "-n", NS, "job/" + name, check=False)


def managed(resource):
    if resource["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != MANAGER:
        raise setup.SetupError("existing resource is not owned by this migration: " +
                               resource["metadata"]["name"])


def contains_fields(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains_fields(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains_fields(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def apply_source_bundle(backup_id):
    rendered = setup.kube(
        "create", "--dry-run=client", "-k", str(MANIFESTS), "-o", "json", capture=True,
    ).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if not resources:
        raise setup.SetupError("native backup bundle is empty")
    for resource in resources:
        group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        kind = resource["kind"].lower() + ("." + group if group else "")
        existing = setup.get_object(kind, resource["metadata"]["name"], NS)
        if existing:
            managed(existing)
            if resource["kind"] == "ReplicationSource":
                expected = resource["spec"].copy()
                expected.pop("paused")
                expected.pop("trigger")
                if (not contains_fields(existing["spec"], expected) or
                        existing["spec"].get("trigger", {}).get("manual") not in (
                            "controlled-by-backup-runner", backup_id) or
                        existing["spec"].get("trigger", {}).get("schedule")):
                    raise setup.SetupError("existing VolSync source differs from the initial backup policy")
                if (existing["spec"]["trigger"]["manual"] == "controlled-by-backup-runner" and
                        not existing["spec"].get("paused")):
                    raise setup.SetupError("initial VolSync source was unexpectedly unpaused")
                # Preserve an upload already started by a previous invocation.
                continue
        setup.kube("apply", "-f", "-", stdin=json.dumps(resource))


def inspect_artifacts(backup_id, current=None):
    current = STAGING / "current" if current is None else current
    if not current.exists():
        return None
    setup.checked_directory(current)
    attributes = current.stat()
    if attributes.st_uid != 999 or stat.S_IMODE(attributes.st_mode) != 0o700:
        raise setup.SetupError("the native backup directory has unexpected ownership or permissions")
    if {path.name for path in current.iterdir()} != ARTIFACTS:
        raise setup.SetupError("the native backup is incomplete; preserving it for inspection")
    for name in ARTIFACTS:
        attributes = (current / name).lstat()
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 999 or
                stat.S_IMODE(attributes.st_mode) != 0o600 or not attributes.st_size):
            raise setup.SetupError("unexpected native backup artifact: " + name)
    if (current / "backup-id").read_text().strip() != backup_id:
        raise setup.SetupError("the staged dump belongs to a different backup generation")
    metadata = json.loads((current / "metadata.json").read_text())
    if (metadata.get("database") != "board_games" or
            not 170000 <= int(metadata.get("server_version_num", 0)) < 180000):
        raise setup.SetupError("the staged dump has an unexpected database or PostgreSQL version")
    checksums = (current / "SHA256SUMS").read_text()
    seen = set()
    for line in checksums.splitlines():
        digest, name = line.split("  ", 1)
        if (name not in ARTIFACTS - {"SHA256SUMS"} or name in seen or
                not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise setup.SetupError("unexpected native backup checksum manifest")
        seen.add(name)
        checksum = hashlib.sha256()
        with (current / name).open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                checksum.update(chunk)
        if checksum.hexdigest() != digest:
            raise setup.SetupError("native backup checksum failed: " + name)
    if seen != ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("native backup checksum manifest is incomplete")
    return checksums


def retry_failed_dump(job, template):
    # An immutable failed Job needs replacement to pick up the connection retry.
    # Keep its evidence, and refuse replacement if any writer or artifact remains.
    expected = json.loads(json.dumps(template["spec"]["template"]["spec"]))
    expected["containers"][0].pop("args")
    if not contains_fields(job["spec"]["template"]["spec"], expected):
        raise setup.SetupError("failed native dump Job differs from the approved backup generation")
    source = setup.get_object("replicationsource", SOURCE, NS)
    if (not source or not source["spec"].get("paused") or
            source["spec"].get("trigger", {}).get("manual") != "controlled-by-backup-runner"):
        raise setup.SetupError("failed dump retry requires the original paused VolSync source")
    if any(STAGING.iterdir()):
        raise setup.SetupError("staging contains artifacts; preserving the failed dump Job and data")
    uid = job["metadata"]["uid"]
    if not re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", uid):
        raise setup.SetupError("unexpected failed dump Job UID")
    pods = json.loads(setup.kube(
        "get", "pods", "-n", NS, "-l", "batch.kubernetes.io/job-name=" + DUMP_JOB,
        "-o", "json", capture=True,
    ).stdout)
    for pod in pods["items"]:
        if (pod.get("status", {}).get("phase") not in ("Failed", "Succeeded") or
                not any(owner.get("uid") == uid
                        for owner in pod["metadata"].get("ownerReferences", []))):
            raise setup.SetupError("a dump Pod is still active or has unexpected ownership; preserving it")
    prefix = setup.RECOVERY / ("initial-dump-failed-" + uid)
    setup.private_write(Path(str(prefix) + ".job.json"), json.dumps(job, indent=2) + "\n")
    logs = []
    for pod in pods["items"]:
        name = pod["metadata"]["name"]
        result = setup.kube("logs", "-n", NS, "pod/" + name, "-c", "dump",
                            capture=True, sensitive=True, check=False, timeout=30)
        logs.append(name + ":\n" + result.stdout + result.stderr)
    setup.private_write(Path(str(prefix) + ".log"), "\n".join(logs))
    # UID and resourceVersion preconditions prevent deleting a replacement Job.
    setup.kube(
        "delete", "--raw", f"/apis/batch/v1/namespaces/{NS}/jobs/{DUMP_JOB}",
        "-f", "-", "--request-timeout=30s", capture=True,
        stdin=json.dumps({
            "apiVersion": "v1", "kind": "DeleteOptions",
            "preconditions": {"uid": uid, "resourceVersion": job["metadata"]["resourceVersion"]},
            "propagationPolicy": "Foreground",
        }),
    )
    deadline = time.monotonic() + 60
    while setup.get_object("job", DUMP_JOB, NS):
        if time.monotonic() >= deadline:
            raise setup.SetupError("failed dump Job deletion is still pending; no replacement was started")
        time.sleep(2)
    print("failed dump evidence saved privately; empty staging and terminated dump Pods verified before retry", flush=True)


def native_dump(backup_id):
    template = read_template(MANIFESTS / "dump-job.yaml")
    container = template["spec"]["template"]["spec"]["containers"][0]
    container["env"].append({"name": "BACKUP_ID", "value": backup_id})
    production = setup.get_object("pod", "postgres-0", NS)
    if (not production or production["spec"].get("nodeName") != "openmediavault" or
            not any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in production.get("status", {}).get("conditions", [])) or
            production["spec"]["containers"][0]["image"] != container["image"] or
            not any(volume.get("name") == "data" and
                    volume.get("persistentVolumeClaim", {}).get("claimName") == "data-postgres-0"
                    for volume in production["spec"].get("volumes", []))):
        raise setup.SetupError("production PostgreSQL health, node, or pinned version differs from inventory")
    existing = setup.get_object("job", DUMP_JOB, NS)
    if existing:
        managed(existing)
        if any(condition.get("type") == "Failed" and condition.get("status") == "True"
               for condition in existing.get("status", {}).get("conditions", [])):
            retry_failed_dump(existing, template)
            existing = None
    if existing:
        if not contains_fields(existing["spec"]["template"]["spec"], template["spec"]["template"]["spec"]):
            raise setup.SetupError("existing native dump Job differs from this backup generation")
    else:
        if inspect_artifacts(backup_id) is not None:
            raise setup.SetupError("staged artifacts exist without their native dump Job; preserving them")
        source = setup.get_object("replicationsource", SOURCE, NS)
        if not source["spec"].get("paused"):
            raise setup.SetupError("refusing to write staging while the VolSync source is unpaused")
        setup.kube("create", "-f", "-", stdin=json.dumps(template))
    try:
        setup.wait_for_job(DUMP_JOB, timeout=630, description="native PostgreSQL dump")
    finally:
        setup.kube("logs", "-n", NS, "job/" + DUMP_JOB, check=False)
    checksums = inspect_artifacts(backup_id)
    if checksums is None:
        raise setup.SetupError("native dump Job completed without publishing its artifacts")
    print("native dump files and checksums verified on the array", flush=True)
    return checksums


def upload(backup_id):
    for attempt in range(5):
        source = setup.get_object("replicationsource", SOURCE, NS)
        if not source:
            raise setup.SetupError("the VolSync source disappeared before upload")
        if source["spec"].get("trigger", {}).get("manual") not in (
                "controlled-by-backup-runner", backup_id):
            raise setup.SetupError("the VolSync trigger changed before upload")
        if not source["spec"].get("paused") and source["spec"]["trigger"]["manual"] == backup_id:
            break
        # Compare resourceVersion to avoid changing a concurrently edited source.
        result = setup.kube("patch", "replicationsource", SOURCE, "-n", NS, "--type=merge", "-p", json.dumps({
            "metadata": {"resourceVersion": source["metadata"]["resourceVersion"]},
            "spec": {"paused": False, "trigger": {"manual": backup_id}},
        }), capture=True, check=False)
        if not result.returncode:
            print(result.stdout, end="", flush=True)
            break
        if "(Conflict)" not in result.stderr:
            raise setup.SetupError("could not trigger the VolSync upload")
        time.sleep(0.2)
    else:
        raise setup.SetupError("VolSync source kept changing; staged dump is preserved")
    deadline = time.monotonic() + 630
    while time.monotonic() < deadline:
        source = setup.get_object("replicationsource", SOURCE, NS)
        if not source or source["spec"].get("trigger", {}).get("manual") != backup_id:
            raise setup.SetupError("VolSync source disappeared or its trigger changed")
        status = source.get("status", {})
        if (status.get("lastManualSync") == backup_id and status.get("lastSyncTime") and
                not status.get("lastSyncStartTime") and
                status.get("latestMoverStatus", {}).get("result") == "Successful" and
                any(condition.get("type") == "Synchronizing" and condition.get("status") == "False"
                    and condition.get("reason") == "WaitingForManual"
                    for condition in status.get("conditions", []))):
            setup.private_write(setup.RECOVERY / "initial-volsync-result.json", json.dumps(source, indent=2) + "\n")
            print("VolSync upload, retention, and cleanup completed", flush=True)
            return
        time.sleep(2)
    raise setup.SetupError("VolSync upload did not finish within 630 seconds; staged files are preserved")


def diagnostics():
    setup.kube("get", "replicationsource", SOURCE, "-n", NS, "-o", "yaml", check=False)
    setup.kube("get", "pvc", "-n", NS, "-o", "wide", check=False)
    setup.kube("get", "pods", "-n", NS, "-o", "wide", check=False)
    setup.kube("get", "endpointslices", "-n", NS, "-l", "kubernetes.io/service-name=postgres",
               "-o", "wide", check=False)
    setup.kube("get", "events", "-n", NS, "--sort-by=.metadata.creationTimestamp", check=False)
    # Secret values and native SQL/dump contents are never printed.
    mover_pods = setup.kube("get", "pods", "-n", NS, "-l",
                           "batch.kubernetes.io/job-name=volsync-src-postgres-backup",
                           "-o", "name", capture=True, check=False).stdout.splitlines()
    for pod in mover_pods:
        setup.kube("logs", "-n", NS, pod, "--tail=60", "--pod-running-timeout=5s", check=False)


def backup():
    inventory = json.loads(setup.kube("get", "nodes", "-o", "json", capture=True).stdout)
    if [node["metadata"]["name"] for node in inventory["items"]] != ["openmediavault"]:
        raise setup.SetupError("other nodes require the later approval gate")
    claim = setup.get_object("pvc", "data-postgres-0", NS)
    volume = setup.get_object("pv", setup.LIVE_PV)
    if (not claim or claim["spec"].get("volumeName") != setup.LIVE_PV or
            claim.get("status", {}).get("phase") != "Bound" or not volume or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("production database binding or Retain safeguard has changed")
    for path in (STAGING, setup.BASE / "backups/data-postgres-0",
                 setup.BASE / "backup-cache/data-postgres-0/source"):
        setup.checked_directory(path)
        if not path.is_dir():
            raise setup.SetupError("approved array directory is missing: " + str(path))
    staging_claim = setup.get_object("pvc", "postgres-backup-staging", NS)
    staging_volume = setup.get_object("pv", "board-games-postgres-backup-staging")
    if (not staging_claim or not staging_volume or
            staging_claim.get("status", {}).get("phase") != "Bound" or
            staging_claim["spec"].get("volumeName") != staging_volume["metadata"]["name"] or
            staging_volume["spec"].get("local", {}).get("path") != str(STAGING) or
            staging_volume["spec"].get("claimRef", {}).get("uid") != staging_claim["metadata"]["uid"]):
        raise setup.SetupError("staging PVC no longer binds the approved array directory")
    if shutil.disk_usage(STAGING).free < 20 * 1024**3:
        raise setup.SetupError("less than 20 GiB remains on the backup array")
    credentials = json.loads(setup.private_read(setup.RECOVERY / "credentials.json"))
    required_keys = {
        "RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME",
        "RESTIC_REST_PASSWORD", "htpasswd",
    }
    if (set(credentials) != required_keys or
            any(not isinstance(value, str) or not value for value in credentials.values()) or
            credentials["RESTIC_REPOSITORY"] != setup.REPOSITORY or
            credentials["RESTIC_REST_USERNAME"] != "data-postgres-0"):
        raise setup.SetupError("saved repository credentials differ from the verified setup")
    fields = {key: value for key, value in credentials.items() if key != "htpasswd"}
    setup.ensure_secret("restic-data-postgres-0", fields)
    fields["RESTIC_REPOSITORY"] = MOVER_REPOSITORY
    setup.ensure_secret(CLIENT_SECRET, fields)
    setup.save_secret(CLIENT_SECRET)
    setup.save_secret("board-games-secrets")
    print("database and mover credentials saved in the private recovery bundle; do not paste them", flush=True)

    state_path = setup.RECOVERY / "initial-backup.json"
    if not state_path.exists():
        setup.private_write(state_path, json.dumps({"backup_id": "initial-" + secrets.token_hex(16)}) + "\n")
    backup_id = json.loads(setup.private_read(state_path))["backup_id"]
    if not re.fullmatch(r"initial-[0-9a-f]{32}", backup_id):
        raise setup.SetupError("invalid saved initial-backup identifier")

    run_check(setup.MANIFESTS / "repository-check.yaml", {}, "mover DNS/TLS/authentication check")
    apply_source_bundle(backup_id)
    checksums = native_dump(backup_id)
    upload(backup_id)
    run_check(MANIFESTS / "verify-job.yaml", {
        "EXPECTED_BACKUP_ID": backup_id, "EXPECTED_SHA256SUMS": checksums,
    }, "native backup snapshot verification")
    print("first board-games database backup verified; isolated restore and daily scheduling are still pending", flush=True)


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
        except (setup.SetupError, OSError, ValueError, subprocess.TimeoutExpired):
            diagnostics()
            raise


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"backup stopped: {error}", file=sys.stderr)
        sys.exit(1)
