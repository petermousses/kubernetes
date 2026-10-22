#!/usr/bin/env python3
"""Capture Paperless files and native databases in one quiescence interval, recover, then upload."""

import argparse
from contextlib import ExitStack
import csv
import ctypes
from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("paperless_setup", Path(__file__).with_name("prepare-paperless.py"))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "data"
STATE = setup.RECOVERY / "initial-backup.json"
QUIESCENCE = setup.RECOVERY / "writer-quiescence.json"
COMPONENTS = tuple(setup.PROTECTED)
FILE_COMPONENTS = ("data", "ai", "gpt", "gpt-config", "gpt-db")
WRITERS = ("paperless-ai", "paperless-gpt", "paperless")
ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "validation.json", "metadata.json", "backup-id", "SHA256SUMS"}
PLACEHOLDER = "controlled-by-backup-runner"
SERVICE = "k3s-paperless-backup-initial.service"
RECOVERY_SERVICE = "k3s-paperless-backup-recovery.service"
INSTALL = Path("/usr/local/lib/k3s-backups/paperless-ngx")
UNIT_DIRECTORY = Path("/etc/systemd/system")
PHASE = "starting"

def now():
    return datetime.now(timezone.utc).isoformat()


def saved(path):
    return json.loads(setup.private_read(path))


def save(path, value):
    setup.private_write(path, json.dumps(value, indent=2) + "\n")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def valid_id(identifier):
    return isinstance(identifier, str) and re.fullmatch(r"initial-[0-9a-f]{32}", identifier)


def valid_generation(identifier):
    return isinstance(identifier, str) and re.fullmatch(r"(?:initial|daily)-[0-9a-f]{32}", identifier)


def evidence_name(identifier, prefix):
    if not valid_generation(identifier) or (prefix != "initial" and prefix != identifier):
        raise setup.SetupError("invalid backup evidence generation or prefix")
    if prefix == "initial" and not valid_id(identifier):
        raise setup.SetupError("daily backups must not replace initial verification evidence")
    return prefix


def staging(component):
    return setup.BASE / "backup-staging" / setup.PROTECTED[component]["claim"]


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def artifact_write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
        output.flush()
        os.fchown(output.fileno(), 999, 999)
        os.fsync(output.fileno())


def digest_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise setup.SetupError("artifact is not an independent regular file")
        checksum = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise setup.SetupError("artifact changed while hashing")
        return checksum.hexdigest()


def claim_pods(claim):
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
            any(volume.get("persistentVolumeClaim", {}).get("claimName") == claim
                for volume in pod["spec"].get("volumes", []))]


def runtime_containers():
    return json.loads(setup.run(["k3s", "crictl", "ps", "-o", "json"],
                                capture=True, sensitive=True, timeout=30).stdout)["containers"]


def controller_hash(resource):
    return fingerprint({key: value for key, value in resource["spec"].items() if key != "replicas"})


def guarded_patch(kind, name, resource, operations):
    tests = [{"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": resource["metadata"]["resourceVersion"]}]
    setup.kube("patch", kind, name, "-n", NS, "--type=json", "-p", json.dumps(tests + operations),
               capture=True, sensitive=True)


def read_template(filename):
    return json.loads(setup.kube("create", "--dry-run=client", "-f", str(filename), "-o", "json",
                                 capture=True).stdout)


def contains_fields(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains_fields(actual[key], value)
                                                for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains_fields(left, right) for left, right in zip(actual, expected))
    return actual == expected


def apply_sources(identifier):
    rendered = setup.kube("kustomize", str(MANIFESTS), capture=True).stdout
    output = setup.kube("create", "--dry-run=client", "-f", "-", "-o", "json",
                        stdin=rendered, capture=True).stdout.lstrip()
    # kubectl prints one JSON document per object for a multi-document input.
    # Parse the complete stream before creating any source; also accept a List.
    decoder, resources = json.JSONDecoder(), []
    while output:
        resource, end = decoder.raw_decode(output)
        if not isinstance(resource, dict):
            raise setup.SetupError("unexpected JSON value in the data backup bundle")
        resources.extend(resource.get("items", [resource]))
        output = output[end:].lstrip()
    policies = [item for item in resources if item.get("kind") == "NetworkPolicy"]
    if {item["metadata"]["name"] for item in policies} != {"allow-paperless-native-postgres", "paperless-native-client-egress"} or len(policies) != 2:
        raise setup.SetupError("native backup network policy bundle differs")
    for policy in policies:
        existing = setup.get_object("networkpolicy", policy["metadata"]["name"], NS)
        if existing and existing["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER:
            raise setup.SetupError("native backup policy name is already used outside this migration")
        setup.kube("apply", "-f", "-", stdin=json.dumps(policy))
    resources = [item for item in resources if item.get("kind") != "NetworkPolicy"]
    expected_names = {component + "-backup" for component in COMPONENTS}
    if (len(resources) != len(expected_names) or
            any(not isinstance(resource, dict) or resource.get("kind") != "ReplicationSource" or
                resource.get("apiVersion") != "volsync.backube/v1alpha1" or
                not isinstance(resource.get("metadata"), dict) or
                resource["metadata"].get("namespace") != NS for resource in resources) or
            {resource["metadata"].get("name") for resource in resources} != expected_names):
        raise setup.SetupError("expected exactly the seven approved Paperless ReplicationSources")
    for resource in resources:
        name = resource["metadata"]["name"]
        existing = setup.get_object("replicationsource", name, NS)
        if existing:
            expected = {key: value for key, value in resource["spec"].items() if key not in ("paused", "trigger")}
            actual = {key: value for key, value in existing["spec"].items() if key not in ("paused", "trigger")}
            if (existing["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                    not contains_fields(actual, expected) or set(actual) != set(expected) or
                    existing["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier)):
                raise setup.SetupError("existing VolSync source differs from this runner: " + name)
        else:
            setup.kube("create", "-f", "-", stdin=json.dumps(resource))


def ensure_source_idle(component, identifier):
    source = setup.get_object("replicationsource", component + "-backup", NS)
    if (not source or source["spec"].get("paused") is not True or
            source["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier) or
            claim_pods(component + "-backup-staging")):
        raise setup.SetupError("staging is not idle and paused: " + component)


def inspect_artifacts(component, identifier, *, current=None):
    root = current.parent if current is not None else staging(component)
    current = current if current is not None else root / "current"
    setup.checked_directory(root)
    if not current.exists() and not current.is_symlink():
        return None
    setup.checked_directory(current)
    historical = (current.name == identifier and valid_generation(identifier) and
                  root == setup.BASE / "backup-staging-history" / setup.PROTECTED[component]["claim"])
    if historical:
        attributes = root.stat()
        if attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("staging history must be private and root-owned")
    elif {item.name for item in root.iterdir()} != {"current"}:
        raise setup.SetupError("additional staging data is preserved: " + component)
    attributes = current.stat()
    if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o700):
        raise setup.SetupError("published directory ownership or permissions changed: " + component)
    if {item.name for item in current.iterdir()} != ARTIFACTS:
        raise setup.SetupError("published artifact set is incomplete: " + component)
    for path in current.iterdir():
        attributes = path.lstat()
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600)):
            raise setup.SetupError("published artifact type, ownership, or permissions changed: " + component)
    if (current / "backup-id").read_text().strip() != identifier:
        raise setup.SetupError("published backup generation differs: " + component)
    metadata = json.loads((current / "metadata.json").read_text())
    if metadata.get("backup_id") != identifier or metadata.get("claim") != setup.PROTECTED[component]["claim"]:
        raise setup.SetupError("published metadata identity differs: " + component)
    checksums, names = (current / "SHA256SUMS").read_text(), set()
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        if not match or match[2] not in ARTIFACTS - {"SHA256SUMS"} or match[2] in names:
            raise setup.SetupError("published checksum manifest is invalid: " + component)
        names.add(match[2])
        if digest_file(current / match[2]) != match[1]:
            raise setup.SetupError("published artifact checksum failed: " + component)
    if names != ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("published checksum manifest is incomplete: " + component)
    return checksums


def publish(component, identifier, pending):
    ensure_source_idle(component, identifier)
    setup.checked_directory(pending)
    if {path.name for path in pending.iterdir()} not in (ARTIFACTS, ARTIFACTS - {"SHA256SUMS"}):
        raise setup.SetupError("partial capture preserved for inspection: " + component)
    for path in pending.iterdir():
        attributes = path.lstat()
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
            raise setup.SetupError("pending artifact is not an independent regular file")
        os.chown(path, 999, 999, follow_symlinks=False)
        os.chmod(path, 0o600)
    checksums = "".join(digest_file(pending / name) + "  " + name + "\n"
                        for name in sorted(ARTIFACTS - {"SHA256SUMS"}))
    manifest = pending / "SHA256SUMS"
    if manifest.exists():
        if manifest.read_text() != checksums:
            raise setup.SetupError("pending checksum manifest differs; all captures preserved")
    else:
        artifact_write(manifest, checksums)
    os.chmod(pending, 0o700)
    sync_directory(pending)
    current = staging(component) / "current"
    if current.exists() or current.is_symlink():
        raise setup.SetupError("refusing to replace published artifacts: " + component)
    rename_no_replace(pending, current)
    sync_directory(staging(component))
    inspect_artifacts(component, identifier)
    print("complete volume and recovery configuration published: " + component, flush=True)


class HashingReader:
    def __init__(self, stream, progress):
        self.stream, self.progress = stream, progress
        self.checksum = hashlib.sha256()

    def read(self, count):
        data = self.stream.read(count)
        self.checksum.update(data)
        self.progress(len(data))
        return data


def attributes_key(attributes):
    return (attributes.st_dev, attributes.st_ino, attributes.st_mode, attributes.st_uid, attributes.st_gid,
            attributes.st_nlink, attributes.st_size, attributes.st_mtime_ns, attributes.st_ctime_ns)


def archive_volume(component, state, destination, *, native_source=None):
    source = native_source if native_source is not None else setup.PROTECTED[component]["path"]
    if native_source is not None:
        setup.checked_directory(native_source)
    checked_source_path(component)
    device, contents = source.stat().st_dev, []
    copied, last_check = 0, time.monotonic()

    def progress(count):
        nonlocal copied, last_check
        copied += count
        if time.monotonic() - last_check >= 30:
            assert_stopped(component, state)
            print(component + " captured bytes: " + str(copied), flush=True)
            last_check = time.monotonic()

    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            def visit(path, relative):
                before = path.lstat()
                if (before.st_dev != device or not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)) or
                        (stat.S_ISREG(before.st_mode) and before.st_nlink != 1) or
                        os.listxattr(path, follow_symlinks=False)):
                    raise setup.SetupError("source contains a link, special file, nested filesystem, or xattr; capture stopped")
                entry = {"path": relative, "uid": before.st_uid, "gid": before.st_gid,
                         "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns,
                         "source_inode": before.st_ino, "source_ctime_ns": before.st_ctime_ns}
                if stat.S_ISDIR(before.st_mode):
                    names = sorted(item.name for item in path.iterdir())
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uname = info.gname = ""
                    info.pax_headers["mtime"] = f"{before.st_mtime_ns // 1000000000}.{before.st_mtime_ns % 1000000000:09d}"
                    archive.addfile(info)
                    entry["type"] = "directory"
                    contents.append(entry)
                    for name in names:
                        visit(path / name, name if relative == "." else relative + "/" + name)
                    if names != sorted(item.name for item in path.iterdir()):
                        raise setup.SetupError("directory changed while writer was stopped")
                else:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as stream:
                        if attributes_key(os.fstat(stream.fileno())) != attributes_key(before):
                            raise setup.SetupError("source file changed while opening it")
                        info = archive.gettarinfo(fileobj=stream, arcname=relative)
                        info.uname = info.gname = ""
                        info.pax_headers["mtime"] = f"{before.st_mtime_ns // 1000000000}.{before.st_mtime_ns % 1000000000:09d}"
                        reader = HashingReader(stream, progress)
                        archive.addfile(info, reader)
                        if attributes_key(os.fstat(stream.fileno())) != attributes_key(before):
                            raise setup.SetupError("source file changed during stopped-writer capture")
                    entry.update({"type": "file", "bytes": before.st_size, "sha256": reader.checksum.hexdigest()})
                    contents.append(entry)
                if attributes_key(path.lstat()) != attributes_key(before) or os.listxattr(path, follow_symlinks=False):
                    raise setup.SetupError("source metadata changed during stopped-writer capture")
            visit(source, ".")
        output.flush()
        os.fchown(output.fileno(), 999, 999)
        os.fsync(output.fileno())
    assert_stopped(component, state)
    return contents


def worker_budget():
    node = setup.get_object("node", "openmediavault")
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    setup.resource_budget(node, pods)


def upload_complete(source, identifier):
    if not source:
        return False
    status = source.get("status", {})
    return (not source["spec"].get("paused") and source["spec"].get("trigger") == {"manual": identifier} and
            status.get("lastManualSync") == identifier and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(item.get("type") == "Synchronizing" and item.get("status") == "False" and
                item.get("reason") == "WaitingForManual" for item in status.get("conditions", [])))


def failed_upload_lock(component, source, not_before):
    name = "volsync-src-" + component + "-backup"
    job = setup.get_object("job", name, NS)
    if (not job or job.get("status", {}).get("succeeded", 0) > 0 or not any(
            item.get("kind") == "ReplicationSource" and item.get("controller") is True and
            item.get("uid") == source["metadata"]["uid"]
            for item in job["metadata"].get("ownerReferences", []))):
        return None
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "batch.kubernetes.io/job-name=" + name,
                                 "-o", "json", capture=True, sensitive=True).stdout)["items"]
    for pod in pods:
        if not any(owner.get("kind") == "Job" and owner.get("controller") is True and
                   owner.get("uid") == job["metadata"]["uid"]
                   for owner in pod["metadata"].get("ownerReferences", [])):
            continue
        failed = [item.get("state", {}).get("terminated", {})
                  for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "restic"]
        if not any(item.get("exitCode") == 11 and item.get("finishedAt") and
                   datetime.fromisoformat(item["finishedAt"].replace("Z", "+00:00")) >= datetime.fromisoformat(not_before.replace("Z", "+00:00")) for item in failed):
            continue
        logs = setup.kube("logs", "-n", NS, pod["metadata"]["name"], "-c", "restic", "--tail=100",
                          capture=True, sensitive=True, check=False, timeout=15)
        if not logs.returncode and "repository is already locked" in logs.stdout:
            return {"job": job, "pod": pod, "mover_logs": logs.stdout}
    return None


def stop_failed_upload(component, identifier, source_uid, evidence_prefix, reason, diagnostics=None):
    path = setup.RECOVERY / (evidence_prefix + "-" + component + "-upload-failure.json")
    evidence = {"backup_id": identifier, "component": component, "failed_at": now(), "reason": reason,
                "source_uid": source_uid, "diagnostics": diagnostics or {}}
    save(path, evidence)
    for attempt in range(5):
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if (not source or source["metadata"].get("uid") != source_uid or
                source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                source["spec"].get("trigger") != {"manual": identifier}):
            raise setup.SetupError(reason + "; source identity changed, pause not confirmed; private evidence: " + str(path))
        evidence["source"] = source
        save(path, evidence)
        if source["spec"].get("paused") is True:
            break
        try:
            guarded_patch("replicationsource", component + "-backup", source,
                          [{"op": "add", "path": "/spec/paused", "value": True}])
            break
        except setup.SetupError:
            if attempt == 4:
                raise setup.SetupError(reason + "; could not pause uploader; private evidence: " + str(path))
            time.sleep(0.2)
    raise setup.SetupError(reason + "; uploader paused, published capture retained; private evidence: " + str(path))


def upload(component, identifier, *, evidence_prefix="initial"):
    evidence_name(identifier, evidence_prefix)
    if not inspect_artifacts(component, identifier):
        raise setup.SetupError("cannot upload without complete published artifacts")
    worker_budget()
    source = setup.get_object("replicationsource", component + "-backup", NS)
    if not source or source["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier):
        raise setup.SetupError("upload source generation changed")
    source_uid = source["metadata"]["uid"]
    started_at = now()
    if source["spec"].get("paused"):
        ensure_source_idle(component, identifier)
        guarded_patch("replicationsource", component + "-backup", source,
                      [{"op": "replace", "path": "/spec/paused", "value": False},
                       {"op": "replace", "path": "/spec/trigger", "value": {"manual": identifier}}])
    deadline, last_message = time.monotonic() + 1800, 0
    while time.monotonic() < deadline:
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if (not source or source["metadata"].get("uid") != source_uid or
                source["spec"].get("trigger") != {"manual": identifier}):
            raise setup.SetupError("upload generation changed while synchronizing")
        if upload_complete(source, identifier):
            save(setup.RECOVERY / (evidence_prefix + "-" + component + "-volsync-result.json"), source)
            print(component + " VolSync upload, retention, and cleanup completed", flush=True)
            return
        failure = failed_upload_lock(component, source, started_at)
        if failure:
            stop_failed_upload(component, identifier, source_uid, evidence_prefix,
                               "repository lock blocked " + component + " upload/retention", failure)
        if source["spec"].get("paused"):
            stop_failed_upload(component, identifier, source_uid, evidence_prefix,
                               "uploader was paused before completion")
        if time.monotonic() - last_message >= 60:
            print("waiting for " + component + " VolSync upload and cleanup", flush=True)
            last_message = time.monotonic()
        time.sleep(5)
    stop_failed_upload(component, identifier, source_uid, evidence_prefix,
                       "upload did not complete within thirty minutes")


def verify_backup(component, identifier, *, evidence_prefix="initial", retention_proof=False):
    evidence_name(identifier, evidence_prefix)
    checksums = inspect_artifacts(component, identifier)
    if not checksums:
        raise setup.SetupError("cannot verify without published artifacts")
    worker_budget()
    service = setup.get_object("service", "restic", NS)
    ips = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    job = read_template(MANIFESTS / "verify-job.yaml")
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": "restic-" + setup.PROTECTED[component]["claim"]}}]
    container["env"] = [{"name": key, "value": value} for key, value in {
        "EXPECTED_RESTIC_IPS": ",".join(str(ipaddress.ip_address(value)) for value in ips),
        "EXPECTED_BACKUP_ID": identifier, "EXPECTED_SHA256SUMS": checksums,
        "EXPECTED_ARTIFACTS": json.dumps(sorted(ARTIFACTS)),
        "PROTECTED_CLAIM": setup.PROTECTED[component]["claim"],
        "VERIFY_RETENTION_PROOF": "true" if retention_proof else "false",
    }.items()]
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    print("job/" + name + " created (" + component + " full snapshot verification)", flush=True)
    try:
        setup.wait_for_job(name, timeout=1860)
    finally:
        setup.print_check_logs(name)
    logs = setup.kube("logs", "-n", NS, "job/" + name, capture=True, sensitive=True).stdout
    results = [json.loads(line.removeprefix("BACKUP_VERIFIED_JSON=")) for line in logs.splitlines()
               if line.startswith("BACKUP_VERIFIED_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or results[0].get("backup_id") != identifier or
            results[0].get("claim") != setup.PROTECTED[component]["claim"] or
            not re.fullmatch(r"[0-9a-f]{64}", results[0].get("snapshot", ""))):
        raise setup.SetupError("verification Job did not report this claim and generation")
    result = {**results[0], "completed_at": now(), "checksums": checksums, "verification_job": name}
    if retention_proof:
        proofs = [line for line in logs.splitlines() if line.startswith("scratch retention proof passed:")]
        if len(proofs) != 1:
            raise setup.SetupError("scratch retention proof did not complete exactly once")
        result["retention_proof"] = proofs[0]
    save(setup.RECOVERY / (evidence_prefix + "-" + component + "-backup-result.json"), result)
    return result


def interrupted(number, frame):
    raise setup.SetupError("backup interrupted by signal " + str(number))


def rename_no_replace(source, destination):
    library = ctypes.CDLL(None, use_errno=True)
    method = getattr(library, "renameat2", None)
    if method is None:
        raise setup.SetupError("Linux renameat2 is required for publication without replacement")
    method.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    method.restype = ctypes.c_int
    if method(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise setup.SetupError("publication stopped; existing and pending artifacts retained")


def scheduling_backlog():
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") == "Pending" and not pod["spec"].get("nodeName") for pod in pods):
        raise setup.SetupError("pending Pods may take Paperless's released reservations; resolve them before capture")


def checked_source_path(component):
    entry = setup.PROTECTED[component]
    path = entry["path"]
    attrs = path.lstat()
    setup.require(stat.S_ISDIR(attrs.st_mode) and path.resolve(strict=True) == setup.PHYSICAL / path.relative_to("/filesystem"),
                  "protected source path changed: " + component)
    setup.mounted_on_array(path)
    return path


def check_journal(state):
    setup.require(state.get("version") == 1 and valid_generation(state.get("backup_id")) and
                  state.get("phase") in ("quiescing", "captured", "resumed") and type(state.get("capture_complete")) is bool and
                  set(state.get("workloads", {})) == set(WRITERS) and set(state.get("bindings", {})) == set(COMPONENTS),
                  "invalid durable Paperless writer journal")
    for name, entry in state["workloads"].items():
        setup.require(entry.get("uid") == setup.WORKLOADS[name][1] and entry.get("replicas") == 1 and
                      re.fullmatch(r"[0-9a-f]{64}", entry.get("hash", "")) and
                      isinstance(entry.get("pod_uids"), list) and len(entry["pod_uids"]) == 1,
                      "invalid saved writer identity: " + name)
    for component, entry in state["bindings"].items():
        expected = setup.PROTECTED[component]
        setup.require(set(entry) == {"claim_uid", "volume_uid", "directory_device", "directory_inode"} and
                      entry["claim_uid"] == expected["claim_uid"] and entry["volume_uid"] == expected["volume_uid"],
                      "invalid saved source binding: " + component)


def checked_controllers(state):
    check_journal(state)
    bindings = setup.checked_sources()[0]
    setup.require(bindings == state["bindings"], "protected storage changed while writers were quiesced")
    controllers = {}
    for name, entry in state["workloads"].items():
        current = setup.get_object("deployment", name, NS, sensitive=True)
        setup.require(current and current["metadata"]["uid"] == entry["uid"] and controller_hash(current) == entry["hash"] and
                      current["spec"].get("replicas") in (0, 1), "writer controller changed: " + name)
        controllers[name] = current
    return controllers


def writer_pods():
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    file_claims = {setup.PROTECTED[name]["claim"] for name in FILE_COMPONENTS}
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
            (any(item.get("name") in WRITERS for item in pod["spec"].get("containers", [])) or
             any(item.get("persistentVolumeClaim", {}).get("claimName") in file_claims for item in pod["spec"].get("volumes", [])))]


def assert_stopped(component, state):
    controllers = checked_controllers(state)
    setup.require(all(item["spec"]["replicas"] == 0 for item in controllers.values()) and not writer_pods(),
                  "an application writer or source-claim holder is still running")
    old_uids = {uid for entry in state["workloads"].values() for uid in entry["pod_uids"]}
    for container in runtime_containers():
        labels = container.get("labels", {})
        setup.require(not (labels.get("io.kubernetes.pod.uid") in old_uids or
                          labels.get("io.kubernetes.pod.namespace") == NS and container.get("metadata", {}).get("name") in WRITERS),
                      "an application runtime container is still running")


def set_replicas(state, name, target):
    for attempt in range(5):
        current = checked_controllers(state)[name]
        if current["spec"]["replicas"] == target:
            return
        try:
            guarded_patch("deployment", name, current, [{"op": "replace", "path": "/spec/replicas", "value": target}])
            return
        except setup.SetupError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def wait_stopped(state):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        checked_controllers(state)
        if not writer_pods():
            assert_stopped("data", state)
            return
        time.sleep(2)
    raise setup.SetupError("writers did not stop; durable recovery remains enabled")


def resume_writers(state):
    controllers = checked_controllers(state)
    stopping = {name for name in WRITERS if controllers[name]["spec"]["replicas"] == 0}
    deadline = time.monotonic() + 600
    while stopping:
        controllers, pods, containers = checked_controllers(state), writer_pods(), runtime_containers()
        # Request the API first. Recover each safe writer without waiting for
        # the readiness of another, but never overlap its terminating process.
        for name in reversed(WRITERS):
            if name not in stopping:
                continue
            claims = {claim for claim, _ in setup.MOUNTS[name].values()}
            old_uids = set(state["workloads"][name]["pod_uids"])
            holders = [pod for pod in pods if
                       any(item.get("name") == name for item in pod["spec"].get("containers", [])) or
                       any(item.get("persistentVolumeClaim", {}).get("claimName") in claims
                           for item in pod["spec"].get("volumes", []))]
            running = [container for container in containers if
                       container.get("labels", {}).get("io.kubernetes.pod.uid") in old_uids or
                       container.get("labels", {}).get("io.kubernetes.pod.namespace") == NS and
                       container.get("metadata", {}).get("name") == name]
            if controllers[name]["spec"]["replicas"] == 1:
                stopping.remove(name)
            elif not holders and not running:
                set_replicas(state, name, 1)
                stopping.remove(name)
        if stopping:
            setup.require(time.monotonic() < deadline,
                          "old writer still holds storage: " + ", ".join(sorted(stopping)) + "; durable recovery remains enabled")
            time.sleep(2)
    deadline = time.monotonic() + 600
    waiting = list(WRITERS)
    while time.monotonic() < deadline:
        controllers, pods, waiting = checked_controllers(state), writer_pods(), []
        for name in reversed(WRITERS):
            current = controllers[name]
            status = current.get("status", {})
            matches = [pod for pod in pods if any(item.get("name") == name for item in pod["spec"].get("containers", []))]
            if not (len(matches) == 1 and setup.ready(matches[0]) and
                    status.get("observedGeneration", 0) >= current["metadata"]["generation"] and
                    all(status.get(key, 0) == 1 for key in ("replicas", "readyReplicas", "updatedReplicas"))):
                waiting.append(name)
        if not waiting:
            break
        time.sleep(2)
    else:
        raise setup.SetupError("writer recovery timed out: " + ", ".join(waiting) + "; boot/stop recovery remains enabled")
    identities = setup.live_preflight(check_resources=False)
    setup.require(identities["bindings"] == state["bindings"], "recovered writer storage differs")
    save(QUIESCENCE, {**state, "phase": "resumed", "resumed_at": now(), "recovered_production": identities})
    print("Paperless, AI, and GPT recovered on their original claims; capture downtime ended", flush=True)


def recover_writers():
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        state = saved(QUIESCENCE)
        check_journal(state)
        if state["phase"] != "resumed":
            resume_writers(state)


def check_units():
    program = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/backup-paperless.py")
    for name in (SERVICE, RECOVERY_SERVICE):
        path = UNIT_DIRECTORY / name
        attrs = path.lstat()
        setup.require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                      not stat.S_IMODE(attrs.st_mode) & 0o022 and
                      path.read_bytes() == (Path(__file__).resolve().parents[2] / "scripts/backups/systemd" / name).read_bytes(),
                      "installed backup/recovery unit differs from its reviewed source")
        output = setup.run(["systemctl", "show", name,
            "--property=FragmentPath,DropInPaths,ExecStart,ExecStopPost,OnFailure,Restart"], capture=True).stdout
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        starts = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStart", ""))
        stops = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStopPost", ""))
        expected = program + (" --recover-writers-only" if name == RECOVERY_SERVICE else "")
        setup.require(values.get("FragmentPath") == str(path) and not values.get("DropInPaths") and
                      not os.path.lexists(UNIT_DIRECTORY / (name + ".d")) and [value.strip() for value in starts] == [expected] and
                      [value.strip() for value in stops] == ([program + " --recover-writers-only"] if name == SERVICE else []) and
                      values.get("OnFailure", "").split() == ([RECOVERY_SERVICE] if name == SERVICE else []) and
                      values.get("Restart") == ("no" if name == SERVICE else "on-failure"),
                      "loaded backup/recovery settings differ from the reviewed units")


def competing_writers():
    sources = [checked_source_path(name).resolve(strict=True) for name in COMPONENTS]
    def overlaps(path):
        real = Path(path).resolve()
        return any(real.is_relative_to(source) or source.is_relative_to(real) for source in sources)
    volumes = json.loads(setup.kube("get", "pv", "-o", "json", capture=True).stdout)["items"]
    aliases = set()
    for volume in volumes:
        path = (volume["spec"].get("local") or volume["spec"].get("hostPath") or {}).get("path")
        if path and overlaps(path):
            setup.require(volume["metadata"]["name"] in setup.CLAIMS, "another persistent volume aliases protected live storage")
            reference = volume["spec"].get("claimRef", {})
            aliases.add((reference.get("namespace"), reference.get("name")))
    baseline = setup.live_preflight(check_resources=False, record=False)
    own = {entry["pod_uid"] for entry in baseline["workloads"].values()}
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    for pod in pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed") or pod["metadata"]["uid"] in own:
            continue
        for volume in pod["spec"].get("volumes", []):
            mounts = [mount for group in ("containers", "initContainers", "ephemeralContainers")
                      for container in pod["spec"].get(group, []) for mount in container.get("volumeMounts", [])
                      if mount["name"] == volume["name"] and not mount.get("readOnly")]
            if not mounts:
                continue
            host = volume.get("hostPath", {}).get("path")
            claim = volume.get("persistentVolumeClaim", {})
            setup.require(not ((host and overlaps(host)) or not claim.get("readOnly") and
                              (pod["metadata"]["namespace"], claim.get("claimName")) in aliases),
                          "another Pod can write protected live storage")
    mounts = json.loads(setup.run(["findmnt", "--json", "--list", "--output", "TARGET"], capture=True).stdout)["filesystems"]
    setup.require(not any(Path(item["target"]).is_relative_to(source) and Path(item["target"]) != source
                          for item in mounts for source in sources), "a protected source contains a nested mount")


def inspect_source_layout():
    for component in FILE_COMPONENTS:
        source = checked_source_path(component)
        device, pending = source.stat().st_dev, [source]
        while pending:
            path = pending.pop()
            attrs = path.lstat()
            setup.require(attrs.st_dev == device and not os.listxattr(path, follow_symlinks=False) and
                          (stat.S_ISDIR(attrs.st_mode) or stat.S_ISREG(attrs.st_mode)) and
                          (not stat.S_ISREG(attrs.st_mode) or attrs.st_nlink == 1),
                          "source metadata now needs additional handling; writers remain running: " + component)
            if stat.S_ISDIR(attrs.st_mode):
                pending.extend(path.iterdir())


def source_configuration():
    index = saved(setup.RECOVERY / "dependency-inventory.json")
    resources = []
    for kind, entries in index["resources"].items():
        for name, entry in entries.items():
            if entry["present"]:
                resource = setup.get_object(kind, name, NS, sensitive=True)
                setup.require(resource and resource["metadata"]["uid"] == entry["uid"], "source dependency identity changed: " + name)
                resources.append({"kind": kind, "name": name, "uid": entry["uid"], "resource": setup.portable(resource)})
    controllers = {name: setup.portable(setup.get_object(kind, name, NS, sensitive=True))
                   for name, (kind, _, _) in setup.WORKLOADS.items()}
    return {"dependencies": resources, "controllers": controllers}


def unchanged_dependencies(configuration):
    for entry in configuration["dependencies"]:
        current = setup.get_object(entry["kind"], entry["name"], NS, sensitive=True)
        setup.require(current and current["metadata"]["uid"] == entry["uid"] and setup.portable(current) == entry["resource"],
                      "source configuration changed during capture: " + entry["name"])


def redis_command(*arguments):
    return setup.kube("exec", "-n", NS, redis_pod(), "-c", "redis", "--",
                      "redis-cli", "--raw", *arguments, capture=True, sensitive=True, timeout=60).stdout


def redis_pod():
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l",
                               "app.kubernetes.io/name=paperless-ngx,app.kubernetes.io/component=cache", "-o", "json", capture=True).stdout)["items"]
    active = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    setup.require(len(active) == 1 and setup.ready(active[0]), "expected one ready Redis Pod")
    return active[0]["metadata"]["name"]


def redis_preflight():
    config = redis_command("CONFIG", "GET", "dir", "dbfilename").splitlines()
    setup.require(len(config) == 4 and dict(zip(config[::2], config[1::2])) == {"dir": "/data", "dbfilename": "dump.rdb"},
                  "Redis persistence paths changed; private values omitted")
    persistence = dict(line.split(":", 1) for line in redis_command("INFO", "persistence").splitlines() if ":" in line)
    setup.require(persistence.get("aof_enabled") == "0" and persistence.get("loading") == "0", "Redis persistence mode changed")
    return persistence


def native_postgres(identifier, pending):
    job = read_template(MANIFESTS / "postgres-capture-job.yaml")
    job["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "BACKUP_ID", "value": identifier})
    worker_budget()
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    save(setup.RECOVERY / (identifier + "-postgres-capture-job.json"), created)
    print("native PostgreSQL capture Job created; dump and row counts share one snapshot", flush=True)
    try:
        setup.wait_for_job(name, timeout=660)
    finally:
        logs = setup.kube("logs", "-n", NS, "job/" + name, capture=True, sensitive=True, check=False, timeout=15)
        setup.private_write(setup.RECOVERY / (identifier + "-postgres-capture.log"), logs.stdout + logs.stderr)
    deadline = time.monotonic() + 60
    while claim_pods("postgres-backup-staging"):
        setup.require(time.monotonic() < deadline, "native PostgreSQL Job still holds staging; retain its capture")
        time.sleep(2)
    folder = pending / "native-postgres"
    setup.require(folder.is_dir() and not folder.is_symlink() and
                  {item.name for item in folder.iterdir()} == {"database.dump", "globals.sql", "row-counts.csv", "metadata.json", "archive-list.txt", "backup-id"},
                  "native PostgreSQL artifact set is incomplete")
    setup.require((folder / "backup-id").read_text().strip() == identifier and (folder / "database.dump").stat().st_size > 5,
                  "native PostgreSQL capture generation differs")
    with (folder / "database.dump").open("rb") as stream:
        setup.require(stream.read(5) == b"PGDMP", "native PostgreSQL archive header differs")
    metadata = json.loads((folder / "metadata.json").read_text())
    setup.require(int(metadata["server_version_num"]) // 10000 == 18 and bool((folder / "archive-list.txt").read_text().strip()),
                  "PostgreSQL native metadata or archive list differs")
    with (folder / "row-counts.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    setup.require(rows and all(set(row) == {"relation", "rows"} and row["rows"].isdigit() for row in rows) and
                  len({row["relation"] for row in rows}) == len(rows), "native PostgreSQL row-count evidence is incomplete")
    return folder, {"method": "pg_dump and row counts from one exported MVCC snapshot", "native_job": name,
                    "tables": len(rows), "archive_list_checked": True, "server_major": 18}


def native_redis(state):
    deadline = time.monotonic() + 60
    while redis_preflight().get("rdb_bgsave_in_progress") != "0":
        setup.require(time.monotonic() < deadline, "Redis background persistence did not become idle")
        time.sleep(2)
    setup.require(redis_command("SAVE").strip() == "OK", "Redis native SAVE failed; private details omitted")
    path = checked_source_path("redis") / "dump.rdb"
    setup.require({entry.name for entry in path.parent.iterdir()} == {"dump.rdb"}, "Redis directory now contains additional persistence files")
    checksum = digest_file(path)
    setup.kube("exec", "-n", NS, redis_pod(), "-c", "redis", "--", "redis-check-rdb", "/data/dump.rdb",
               capture=True, sensitive=True, timeout=60)
    setup.require(digest_file(path) == checksum, "Redis RDB changed during native validation")
    assert_stopped("redis", state)
    return {"method": "native SAVE and redis-check-rdb", "rdb_sha256": checksum, "aof_enabled": False, "native_integrity": "passed"}


def assert_tree_unchanged(component, contents):
    root = checked_source_path(component)
    actual, pending = {}, [(root, ".")]
    while pending:
        path, relative = pending.pop()
        attrs = path.lstat()
        actual[relative] = attrs
        if stat.S_ISDIR(attrs.st_mode):
            pending.extend((entry, entry.name if relative == "." else relative + "/" + entry.name) for entry in path.iterdir())
    setup.require(set(actual) == {entry["path"] for entry in contents}, "source tree changed during capture: " + component)
    for entry in contents:
        attrs = actual[entry["path"]]
        setup.require((attrs.st_ino, attrs.st_ctime_ns, attrs.st_mtime_ns, attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) ==
                      (entry["source_inode"], entry["source_ctime_ns"], entry["mtime_ns"], entry["uid"], entry["gid"], entry["mode"]) and
                      (entry["type"] != "file" or attrs.st_size == entry["bytes"]), "source metadata changed during capture: " + component)


def validate_archive(component, identifier, current):
    contents = json.loads((current / "contents.json").read_text())
    metadata = json.loads((current / "metadata.json").read_text())
    setup.require(metadata.get("backup_id") == identifier and metadata.get("component") == component and
                  digest_file(current / "volume.tar") == metadata["archive_sha256"], "capture identity/checksum differs: " + component)
    scratch_root = setup.BASE / "backup-validation"
    setup.checked_directory(scratch_root)
    if not scratch_root.exists():
        scratch_root.mkdir(mode=0o700)
        os.chmod(scratch_root, 0o700)
    attrs = scratch_root.lstat()
    setup.require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == 0 and stat.S_IMODE(attrs.st_mode) == 0o700,
                  "archive validation directory must be private and root-owned")
    sqlite_counts = {}
    with tempfile.TemporaryDirectory(prefix=identifier + "-", dir=scratch_root) as folder:
        scratch = Path(folder)
        expected = {entry["path"]: entry for entry in contents}
        setup.require(len(expected) == len(contents), "duplicate captured file metadata")
        with tarfile.open(current / "volume.tar", "r:") as archive:
            seen = set()
            for member in archive:
                relative = Path(member.name)
                setup.require(not relative.is_absolute() and ".." not in relative.parts and member.name in expected and member.name not in seen,
                              "unsafe or duplicate captured archive path")
                seen.add(member.name)
                entry = expected[member.name]
                setup.require((member.uid, member.gid, member.mode, int(Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 10**9)) ==
                              (entry["uid"], entry["gid"], entry["mode"], entry["mtime_ns"]), "captured archive metadata differs")
                target = scratch / relative
                if entry["type"] == "directory":
                    setup.require(member.isdir(), "captured directory type differs")
                    target.mkdir(mode=0o700, exist_ok=member.name == ".")
                else:
                    setup.require(member.isfile() and member.size == entry["bytes"], "captured regular file type/size differs")
                    checksum = hashlib.sha256()
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            checksum.update(chunk)
                            output.write(chunk)
                    setup.require(checksum.hexdigest() == entry["sha256"], "captured file checksum differs")
            setup.require(seen == set(expected), "captured archive entry set differs")
        for entry in contents:
            if entry["type"] != "file":
                continue
            path = scratch / entry["path"]
            with path.open("rb") as stream:
                header = stream.read(16)
            if header == b"SQLite format 3\x00":
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    setup.require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "scratch SQLite integrity check failed")
                    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                    sqlite_counts[entry["path"]] = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                                                    for name in tables}
        if component in ("ai", "gpt-db"):
            setup.require(sqlite_counts, "expected integration SQLite database is missing: " + component)
        if component == "gpt-config":
            files = [entry for entry in contents if entry["type"] == "file"]
            setup.require(len(files) == 1 and isinstance(json.loads((scratch / files[0]["path"]).read_text()), dict), "GPT configuration is not one valid JSON object")
        native = metadata.get("native", {})
        if component == "redis":
            setup.require(native.get("native_integrity") == "passed" and
                          digest_file(scratch / "dump.rdb") == native["rdb_sha256"], "Redis native integrity proof differs")
        if component == "postgres":
            with (scratch / "database.dump").open("rb") as stream:
                setup.require(stream.read(5) == b"PGDMP" and native.get("archive_list_checked") is True,
                              "PostgreSQL native archive proof differs")
    proof = {"result": "passed", "backup_id": identifier, "component": component, "archive_sha256": metadata["archive_sha256"],
             "files": sum(entry["type"] == "file" for entry in contents), "sqlite_tables": sqlite_counts, "native": metadata.get("native", {})}
    path = current / "validation.json"
    if path.exists():
        setup.require(json.loads(path.read_text()) == proof, "saved archive validation proof differs")
    else:
        artifact_write(path, json.dumps(proof, indent=2) + "\n")
    print(component + " archive checksums/metadata and native structure verified", flush=True)
    return proof


def capture_all(identifier):
    existing = {component: inspect_artifacts(component, identifier) for component in COMPONENTS}
    journal = saved(QUIESCENCE) if QUIESCENCE.exists() else None
    if journal:
        check_journal(journal)
        setup.require(journal["backup_id"] == identifier or journal["phase"] == "resumed", "recover the previous writers before another generation")
    if any(existing.values()) or journal and journal["backup_id"] == identifier and journal["capture_complete"]:
        setup.require(journal and journal["backup_id"] == identifier and journal["capture_complete"] and journal["phase"] == "resumed",
                      "existing captures lack a completed and recovered shared journal")
        for component in COMPONENTS:
            path = staging(component) / ("current" if existing[component] else ".pending-" + identifier)
            validate_archive(component, identifier, path)
            if not existing[component]:
                publish(component, identifier, path)
        return
    for component in COMPONENTS:
        ensure_source_idle(component, identifier)
        setup.require(not any(staging(component).iterdir()), "incomplete capture retained; writers are running; inspect before retrying: " + component)
    scheduling_backlog()
    competing_writers()
    inspect_source_layout()
    redis_preflight()
    check_units()
    identities = setup.live_preflight()
    configuration = source_configuration()
    configuration.update(backup_id=identifier, sources={name: {"claim": setup.PROTECTED[name]["claim"],
                          "path": str(setup.PROTECTED[name]["path"]), "identity": identity}
                          for name, identity in identities["bindings"].items()})
    controllers = {name: setup.get_object("deployment", name, NS, sensitive=True) for name in WRITERS}
    state = {"version": 1, "backup_id": identifier, "phase": "quiescing", "capture_complete": False, "started_at": now(),
             "bindings": identities["bindings"], "workloads": {name: {"uid": value["metadata"]["uid"], "hash": controller_hash(value),
                "replicas": 1, "pod_uids": [identities["workloads"][name]["pod_uid"]]} for name, value in controllers.items()}}
    pending = {}
    for component in COMPONENTS:
        path = staging(component) / (".pending-" + identifier)
        path.mkdir(mode=0o700)
        os.chown(path, 999, 999)
        os.chmod(path, 0o700)
        sync_directory(path.parent)
        pending[component] = path
    runtime_containers()
    save(QUIESCENCE, state)
    try:
        print("stopping Paperless, AI, and GPT for one consistent file/database capture interval", flush=True)
        for name in WRITERS:
            set_replicas(state, name, 0)
        wait_stopped(state)
        native_folder, pg_proof = native_postgres(identifier, pending["postgres"])
        assert_stopped("postgres", state)
        redis_proof = native_redis(state)
        trees = {}
        for component in COMPONENTS:
            assert_stopped(component, state)
            contents = archive_volume(component, state, pending[component] / "volume.tar",
                                      native_source=native_folder if component == "postgres" else None)
            if component != "postgres":
                trees[component] = contents
            artifact_write(pending[component] / "contents.json", json.dumps(contents, indent=2) + "\n")
            artifact_write(pending[component] / "configuration.json", json.dumps(configuration, indent=2) + "\n")
            artifact_write(pending[component] / "metadata.json", json.dumps({"backup_id": identifier, "component": component,
                "claim": setup.PROTECTED[component]["claim"], "captured_at": state["started_at"], "completed_at": now(),
                "method": "application writers stopped; native PostgreSQL/Redis and complete file captures",
                "archive_sha256": digest_file(pending[component] / "volume.tar"),
                "native": pg_proof if component == "postgres" else redis_proof if component == "redis" else {}}, indent=2) + "\n")
            artifact_write(pending[component] / "backup-id", identifier + "\n")
            sync_directory(pending[component])
        for component, contents in trees.items():
            assert_tree_unchanged(component, contents)
        unchanged_dependencies(configuration)
        assert_stopped("data", state)
        retained = setup.BASE / "backup-native-captures" / identifier
        setup.checked_directory(retained)
        retained.mkdir(parents=True, mode=0o700)
        os.chmod(retained.parent, 0o700)
        os.chmod(retained, 0o700)
        rename_no_replace(native_folder, retained / "postgres")
        sync_directory(retained)
        sync_directory(retained.parent)
        sync_directory(setup.BASE)
        sync_directory(pending["postgres"])
        state = {**state, "phase": "captured", "capture_complete": True, "captured_at": now()}
        save(QUIESCENCE, state)
    finally:
        handlers = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            resume_writers(state)
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
    for component in COMPONENTS:
        validate_archive(component, identifier, pending[component])
        publish(component, identifier, pending[component])


def preflight():
    proof = saved(setup.RECOVERY / "endpoint-verification.json")
    setup.require(proof.get("result") == "passed" and proof.get("repositories") == list(setup.CLAIMS),
                  "all seven repositories must pass endpoint setup first")
    identities = setup.live_preflight()
    server = setup.get_object("deployment", "restic-server", NS)
    setup.require(server and server.get("status", {}).get("availableReplicas") == 1, "backup endpoint is unavailable")
    clients = json.loads(setup.kube("get", "pods", "-A", "-l", "backups.mousses.xyz/restic-client=true", "-o", "json", capture=True).stdout)["items"]
    setup.require(not any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in clients),
                  "another repository worker survived its process lock; wait for it to finish")
    credentials = saved(setup.RECOVERY / "credentials.json")
    for component in COMPONENTS:
        claim_name = setup.PROTECTED[component]["claim"]
        setup.ensure_secret("restic-" + claim_name, setup.credential_fields(claim_name, credentials["repositories"][claim_name]))
        setup.save_secret("restic-" + claim_name)
        claim = setup.get_object("pvc", component + "-backup-staging", NS)
        volume = setup.get_object("pv", "paperless-" + component + "-backup-staging")
        setup.require(claim and volume and claim.get("status", {}).get("phase") == "Bound" and
                      claim["spec"].get("volumeName") == volume["metadata"]["name"] and
                      volume["spec"].get("claimRef", {}).get("uid") == claim["metadata"]["uid"] and
                      volume["spec"].get("local", {}).get("path") == str(staging(component)) and
                      volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "backup staging binding changed: " + component)
        setup.checked_directory(staging(component))
        attrs = staging(component).stat()
        setup.require((attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) == (999, 999, 0o2770), "backup staging permissions changed")
    setup.require(shutil.disk_usage(setup.BASE).free >= 100 * 1024**3, "less than 100 GiB remains on the array")
    setup.save_secret("restic-root-ca")
    return identities


def same_production(before, after):
    if before["bindings"] != after["bindings"]:
        return False
    for name, entry in before["workloads"].items():
        keys = ("controller_uid", "spec_sha256", "image_id") if name in WRITERS else tuple(entry)
        if any(entry[key] != after["workloads"][name][key] for key in keys):
            return False
    return True


def backup():
    global PHASE
    PHASE = "checking units and recovering interrupted writers"
    check_units()
    recover_writers()
    PHASE = "checking endpoint, source identities, and staging"
    before = preflight()
    if not STATE.exists() and not STATE.is_symlink():
        save(STATE, {"backup_id": "initial-" + secrets.token_hex(16), "started_at": now()})
    identifier = saved(STATE).get("backup_id")
    setup.require(valid_id(identifier), "invalid saved initial backup generation")
    PHASE = "preparing paused uploaders and native capture network rules"
    apply_sources(identifier)
    PHASE = "capturing files and native databases with application writers stopped"
    capture_all(identifier)
    PHASE = "observing recovered application writers"
    recovered = setup.live_preflight(check_resources=False)
    setup.require(same_production(before, recovered), "source storage, image, or controller changed beyond the planned writer restarts")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        setup.require(setup.live_preflight(check_resources=False, record=False) == recovered,
                      "recovered production changed during stability observation")
        time.sleep(5)
    for component in COMPONENTS:
        PHASE = "uploading " + component
        upload(component, identifier)
        PHASE = "verifying complete repository data and artifacts for " + component
        verify_backup(component, identifier)
    final = setup.live_preflight(check_resources=False)
    setup.require(final == recovered, "production changed during uploads; retain all capture and repository evidence")
    save(setup.RECOVERY / "initial-backup-verification.json", {"result": "passed", "backup_id": identifier, "completed_at": now(),
         "components": {name: saved(setup.RECOVERY / ("initial-" + name + "-backup-result.json")) for name in COMPONENTS},
         "production": final})
    print("all seven initial backups verified; isolated restores, daily scheduling, and off-NAS recovery remain pending", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-writers-only", action="store_true")
    arguments = parser.parse_args()
    setup.require(os.geteuid() == 0 and sys.platform == "linux" and setup.platform.node().split(".")[0] == "openmediavault",
                  "run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    with ExitStack() as locks:
        locks.enter_context(setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True))
        if arguments.recover_writers_only:
            recover_writers()
        else:
            locks.enter_context(setup.operation_lock(setup.COORDINATION_LOCK, create=False))
            print("Paperless backup worker serialized with the existing namespace backup workers", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print(json.dumps({"result": "stopped", "phase": PHASE, "reason": str(error)}), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(json.dumps({"result": "stopped", "phase": PHASE, "error_type": type(error).__name__,
                          "reason": "filesystem, API, database, or archive validation failed; private details omitted"}), file=sys.stderr)
        sys.exit(1)
