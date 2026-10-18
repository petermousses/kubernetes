#!/usr/bin/env python3
"""Capture both stopped n8n volumes together, recover the writer, then upload and verify."""

import argparse
from contextlib import ExitStack
import ctypes
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
module_spec = importlib.util.spec_from_file_location("n8n_setup", Path(__file__).with_name("prepare-n8n.py"))
setup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(setup)
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "data"
STATE = setup.RECOVERY / "initial-backup.json"
QUIESCENCE = setup.RECOVERY / "writer-quiescence.json"
COMPONENTS = tuple(setup.PROTECTED)
ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "validation.json", "metadata.json", "backup-id", "SHA256SUMS"}
PLACEHOLDER = "controlled-by-backup-runner"
SERVICE = "k3s-n8n-backup-initial.service"
RECOVERY_SERVICE = "k3s-n8n-backup-recovery.service"
INSTALL = Path("/usr/local/lib/k3s-backups/n8n")
UNIT_DIRECTORY = Path("/etc/systemd/system")

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
    expected_names = {component + "-backup" for component in COMPONENTS}
    if (len(resources) != len(expected_names) or
            any(not isinstance(resource, dict) or resource.get("kind") != "ReplicationSource" or
                resource.get("apiVersion") != "volsync.backube/v1alpha1" or
                not isinstance(resource.get("metadata"), dict) or
                resource["metadata"].get("namespace") != NS for resource in resources) or
            {resource["metadata"].get("name") for resource in resources} != expected_names):
        raise setup.SetupError("expected exactly the two approved n8n ReplicationSources")
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

def archive_volume(component, state, destination):
    source = setup.PROTECTED[component]["path"]
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
                         "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns}
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


def checked_source_path(component):
    path = setup.PROTECTED[component]["path"]
    real_base = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4/k3s/data/n8n")
    expected = real_base / "live-data" if component == "data" else real_base.parent / "n8n-images"
    attributes = path.lstat()
    if (not stat.S_ISDIR(attributes.st_mode) or path.resolve(strict=True) != expected or
            (attributes.st_uid, attributes.st_gid) != (1000, 1000)):
        raise setup.SetupError("authoritative source path or ownership changed: " + component)
    setup.mounted_on_array(path)
    return path


def workload():
    resource = setup.get_object("deployment", "n8n", NS, sensitive=True)
    if not resource:
        raise setup.SetupError("n8n Deployment disappeared")
    return resource


def check_journal(state):
    if (state.get("version") != 1 or not valid_generation(state.get("backup_id")) or
            state.get("phase") not in ("quiescing", "captured", "resumed") or
            type(state.get("capture_complete")) is not bool or
            state.get("original_replicas") != 1 or not state.get("workload_uid") or
            not re.fullmatch(r"[0-9a-f]{64}", state.get("controller_hash", "")) or
            not isinstance(state.get("pod_uids"), list) or len(state["pod_uids"]) != 1 or
            set(state.get("bindings", {})) != set(COMPONENTS)):
        raise setup.SetupError("invalid durable n8n writer journal")
    for component in COMPONENTS:
        if set(state["bindings"][component]) != {"claim_uid", "volume_uid", "device", "inode"}:
            raise setup.SetupError("invalid saved source identities")


def checked_controller(state):
    check_journal(state)
    for component in COMPONENTS:
        entry, identity = setup.PROTECTED[component], state["bindings"][component]
        path = checked_source_path(component)
        attributes = path.stat()
        claim = setup.get_object("pvc", entry["claim"], NS)
        volume = setup.get_object("pv", entry["volume"])
        if (not claim or not volume or claim["metadata"]["uid"] != identity["claim_uid"] or
                volume["metadata"]["uid"] != identity["volume_uid"] or
                claim.get("status", {}).get("phase") != "Bound" or volume.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != entry["volume"] or
                volume["spec"].get("claimRef", {}).get("uid") != identity["claim_uid"] or
                volume["spec"].get("local", {}).get("path") != str(path) or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
                (attributes.st_dev, attributes.st_ino) != (identity["device"], identity["inode"])):
            raise setup.SetupError("production binding or directory identity changed: " + component)
    resource = workload()
    if (resource["metadata"]["uid"] != state["workload_uid"] or
            controller_hash(resource) != state["controller_hash"] or resource["spec"].get("replicas") not in (0, 1)):
        raise setup.SetupError("n8n controller identity or specification changed")
    return resource


def writer_pods():
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
            any(volume.get("persistentVolumeClaim", {}).get("claimName") in (*setup.CLAIMS, "n8n-data")
                for volume in pod["spec"].get("volumes", []))]


def assert_stopped(component, state):
    if checked_controller(state)["spec"].get("replicas") != 0 or writer_pods():
        raise setup.SetupError("n8n or another holder of its production claims is still running")
    for container in runtime_containers():
        labels = container.get("labels", {})
        if labels.get("io.kubernetes.pod.uid") in state["pod_uids"] or (
                labels.get("io.kubernetes.pod.namespace") == NS and container.get("metadata", {}).get("name") == "n8n"):
            raise setup.SetupError("an n8n runtime container is still running")


def set_replicas(state, target):
    for attempt in range(5):
        resource = checked_controller(state)
        if resource["spec"]["replicas"] == target:
            return
        try:
            guarded_patch("deployment", "n8n", resource, [{"op": "replace", "path": "/spec/replicas", "value": target}])
            return
        except setup.SetupError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def wait_stopped(state):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        checked_controller(state)
        if not writer_pods():
            assert_stopped("data", state)
            return
        time.sleep(2)
    raise setup.SetupError("n8n writer did not stop; durable recovery remains enabled")


def resume_writer(state):
    resource = checked_controller(state)
    if resource["spec"]["replicas"] == 0:
        wait_stopped(state)
        set_replicas(state, 1)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        checked_controller(state)
        pods = writer_pods()
        if len(pods) == 1 and setup.ready(pods[0]):
            identities = setup.live_preflight(check_resources=False)
            if any(identities[component][key] != state["bindings"][component][key]
                   for component in COMPONENTS for key in ("claim_uid", "volume_uid")):
                raise setup.SetupError("restarted n8n storage identity changed")
            save(QUIESCENCE, {**state, "phase": "resumed", "resumed_at": now()})
            print("n8n restarted and ready on both original authoritative claims; capture downtime ended", flush=True)
            return
        time.sleep(2)
    raise setup.SetupError("n8n recovery timed out; durable writer journal retained")


def recover_writers():
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        state = saved(QUIESCENCE)
        check_journal(state)
        if state["phase"] != "resumed":
            resume_writer(state)


def check_units(*, loaded=True):
    program = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/backup-n8n.py")
    for name in (SERVICE, RECOVERY_SERVICE):
        path = UNIT_DIRECTORY / name
        attributes = path.lstat()
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0 or attributes.st_nlink != 1 or
                stat.S_IMODE(attributes.st_mode) & 0o022 or
                path.read_bytes() != (Path(__file__).resolve().parents[2] / "scripts/backups/systemd" / name).read_bytes()):
            raise setup.SetupError("installed n8n backup unit differs from its reviewed source")
        output = setup.run(["systemctl", "show", name,
            "--property=FragmentPath,DropInPaths,ExecStart,ExecStopPost,OnFailure,Restart"], capture=True).stdout
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        commands = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStart", ""))
        stops = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStopPost", ""))
        expected = program + (" --recover-writers-only" if name == RECOVERY_SERVICE else "")
        if (values.get("FragmentPath") != str(path) or values.get("DropInPaths") or
                os.path.lexists(UNIT_DIRECTORY / (name + ".d")) or
                [value.strip() for value in commands] != [expected] or
                [value.strip() for value in stops] != ([program + " --recover-writers-only"] if name == SERVICE else []) or
                values.get("OnFailure", "").split() != ([RECOVERY_SERVICE] if name == SERVICE else []) or
                values.get("Restart") != ("no" if name == SERVICE else "on-failure")):
            raise setup.SetupError("loaded n8n backup/recovery settings differ from the reviewed units")


def competing_writers():
    sources = [checked_source_path(component).resolve(strict=True) for component in COMPONENTS]
    def overlaps(path):
        real = Path(path).resolve()
        return any(real.is_relative_to(source) or source.is_relative_to(real) for source in sources)
    volumes = json.loads(setup.kube("get", "pv", "-o", "json", capture=True).stdout)["items"]
    aliases = set()
    for volume in volumes:
        path = (volume["spec"].get("local") or volume["spec"].get("hostPath") or {}).get("path")
        if path and overlaps(path):
            reference = volume["spec"].get("claimRef", {})
            aliases.add((reference.get("namespace"), reference.get("name")))
            if volume["metadata"]["name"] not in (*setup.CLAIMS, "n8n-data"):
                raise setup.SetupError("another persistent volume aliases n8n live storage")
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    own = {pod["metadata"]["uid"] for pod in writer_pods()}
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
            if ((host and overlaps(host)) or (not claim.get("readOnly") and
                    (pod["metadata"]["namespace"], claim.get("claimName")) in aliases)):
                raise setup.SetupError("another Pod can write to n8n live storage")
    mounts = json.loads(setup.run(["findmnt", "--json", "--list", "--output", "TARGET"], capture=True).stdout)["filesystems"]
    if any(Path(item["target"]).is_relative_to(source) and Path(item["target"]) != source
           for item in mounts for source in sources):
        raise setup.SetupError("a live source contains a nested mount")


def scheduling_backlog():
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") == "Pending" and not pod["spec"].get("nodeName") for pod in pods):
        raise setup.SetupError("pending Pods may take n8n's released reservations; resolve them before capture")


def inspect_source_layout():
    # Reject unsupported metadata before releasing any writer reservations.
    # The archive checks these conditions again while the writer is stopped.
    for component in COMPONENTS:
        source = checked_source_path(component)
        device, pending = source.stat().st_dev, [source]
        while pending:
            path = pending.pop()
            attributes = path.lstat()
            if (attributes.st_dev != device or os.listxattr(path, follow_symlinks=False) or
                    not (stat.S_ISDIR(attributes.st_mode) or stat.S_ISREG(attributes.st_mode)) or
                    (stat.S_ISREG(attributes.st_mode) and attributes.st_nlink != 1)):
                raise setup.SetupError("source links, extended attributes, special files, or nested filesystems need support before capture; n8n remains running")
            if stat.S_ISDIR(attributes.st_mode):
                pending.extend(path.iterdir())


def capture_pair(identifier):
    existing = {component: inspect_artifacts(component, identifier) for component in COMPONENTS}
    journal = saved(QUIESCENCE) if QUIESCENCE.exists() else None
    if journal:
        check_journal(journal)
        if journal["backup_id"] != identifier and journal["phase"] != "resumed":
            raise setup.SetupError("recover the previous n8n writer before another capture")
    if any(existing.values()) or (journal and journal["backup_id"] == identifier and journal.get("capture_complete")):
        if (not journal or journal["backup_id"] != identifier or not journal["capture_complete"] or
                journal["phase"] != "resumed"):
            raise setup.SetupError("existing captures lack a completed, recovered shared capture journal")
        locations = {component: staging(component) / ("current" if existing[component] else ".pending-" + identifier)
                     for component in COMPONENTS}
        validate_pair(identifier, locations)
        for component in COMPONENTS:
            if not existing[component]:
                publish(component, identifier, staging(component) / (".pending-" + identifier))
        print("verified both captures from the same stopped-writer interval", flush=True)
        return
    for component in COMPONENTS:
        ensure_source_idle(component, identifier)
        if any(staging(component).iterdir()):
            raise setup.SetupError("incomplete capture preserved; n8n is running; relay the output before retrying")
    scheduling_backlog()
    competing_writers()
    inspect_source_layout()
    check_units()
    identities = setup.live_preflight()
    original = workload()
    config = setup.get_object("configmap", "n8n-config", NS, sensitive=True)
    state = {"version": 1, "backup_id": identifier, "phase": "quiescing", "capture_complete": False,
             "original_replicas": 1, "workload_uid": original["metadata"]["uid"],
             "controller_hash": controller_hash(original), "pod_uids": [identities["application"]["pod_uid"]],
             "configuration_uid": config["metadata"]["uid"], "configuration_hash": fingerprint(config.get("data", {})),
             "started_at": now(), "bindings": {component: {**identities[component],
                "device": checked_source_path(component).stat().st_dev,
                "inode": checked_source_path(component).stat().st_ino} for component in COMPONENTS}}
    runtime_containers()
    pending = {}
    for component in COMPONENTS:
        path = staging(component) / (".pending-" + identifier)
        path.mkdir(mode=0o700)
        os.chown(path, 999, 999)
        os.chmod(path, 0o700)
        sync_directory(path.parent)
        pending[component] = path
    save(QUIESCENCE, state)  # Durable before the first replica-count mutation.
    try:
        print("briefly stopping n8n to capture SQLite, its encryption configuration, and images together", flush=True)
        set_replicas(state, 0)
        wait_stopped(state)
        for component in COMPONENTS:
            contents = archive_volume(component, state, pending[component] / "volume.tar")
            current_config = setup.get_object("configmap", "n8n-config", NS, sensitive=True)
            if (not current_config or current_config["metadata"]["uid"] != state["configuration_uid"] or
                    fingerprint(current_config.get("data", {})) != state["configuration_hash"]):
                raise setup.SetupError("n8n configuration changed during capture")
            artifact_write(pending[component] / "contents.json", json.dumps(contents, indent=2) + "\n")
            artifact_write(pending[component] / "configuration.json", json.dumps({
                "deployment": setup.portable(original), "configmap": setup.portable(config),
                "pair_id": identifier, "bindings": state["bindings"], "image_id": setup.IMAGE,
            }, indent=2) + "\n")
            artifact_write(pending[component] / "metadata.json", json.dumps({
                "backup_id": identifier, "pair_id": identifier, "component": component,
                "claim": setup.PROTECTED[component]["claim"], "image_id": setup.IMAGE,
                "mount": setup.PROTECTED[component]["mount"], "captured_at": state["started_at"],
                "completed_at": now(), "method": "both n8n volumes captured with their common writer stopped",
                "files": sum(item["type"] == "file" for item in contents),
                "file_bytes": sum(item.get("bytes", 0) for item in contents),
            }, indent=2) + "\n")
            artifact_write(pending[component] / "backup-id", identifier + "\n")
            sync_directory(pending[component])
        assert_stopped("images", state)
        state = {**state, "phase": "captured", "capture_complete": True, "captured_at": now()}
        save(QUIESCENCE, state)
    finally:
        handlers = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            resume_writer(state)
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
    # Validate only immutable archives after service recovery, then publish both.
    validate_pair(identifier, pending)
    for component in COMPONENTS:
        publish(component, identifier, pending[component])


def validate_pair(identifier, pending):
    data = pending["data"]
    proof_path = data / "validation.json"
    if not proof_path.exists():
        scratch_root = setup.BASE / "backup-validation"
        setup.checked_directory(scratch_root)
        if not scratch_root.exists():
            scratch_root.mkdir(mode=0o700)
            os.chmod(scratch_root, 0o700)  # Clear SGID inherited from the namespace directory.
        attributes = scratch_root.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("SQLite validation directory must be private and root-owned")
        with tempfile.TemporaryDirectory(prefix=identifier + "-", dir=scratch_root) as folder:
            scratch = Path(folder)
            with tarfile.open(data / "volume.tar", mode="r:") as archive:
                members = {entry.name: entry for entry in archive.getmembers()}
                for name in ("database.sqlite", "database.sqlite-wal", "database.sqlite-shm"):
                    member = members.get(name)
                    if member:
                        if not member.isfile():
                            raise setup.SetupError("captured SQLite artifact is not a regular file")
                        with archive.extractfile(member) as source, (scratch / name).open("xb") as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
                if "config" not in members or not members["config"].isfile() or members["config"].size > 65536:
                    raise setup.SetupError("captured encryption configuration is missing or invalid")
                config_text = archive.extractfile(members["config"]).read()
            config = json.loads(config_text)
            if not isinstance(config.get("encryptionKey"), str) or not config["encryptionKey"]:
                raise setup.SetupError("captured encryption key is missing")
            expected = saved(setup.RECOVERY / "storage-migration/copy-validation.json")["encryption_config_sha256"]
            if hashlib.sha256(config_text).hexdigest() != expected or not (scratch / "database.sqlite").is_file():
                raise setup.SetupError("captured database or encryption-key continuity check failed")
            connection = sqlite3.connect(scratch / "database.sqlite")
            try:
                connection.execute("PRAGMA trusted_schema=OFF")
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchmany(1):
                    raise setup.SetupError("copied SQLite integrity or foreign key check failed")
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                if not {"workflow_entity", "credentials_entity"}.issubset(tables):
                    raise setup.SetupError("captured n8n application tables are missing")
                counts = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
            finally:
                connection.close()
            artifact_write(proof_path, json.dumps({"result": "passed", "backup_id": identifier,
                "sqlite_integrity": "passed", "foreign_keys": "passed", "relation_counts": counts,
                "encryption_config_sha256": expected, "encryption_key_preserved": True}, indent=2) + "\n")
            print("scratch SQLite validation passed; tables=" + str(len(counts)) +
                  " workflows=" + str(counts["workflow_entity"]) + " credentials=" + str(counts["credentials_entity"]), flush=True)
    for component in COMPONENTS:
        directory = pending[component]
        if component == "images" and not (directory / "validation.json").exists():
            artifact_write(directory / "validation.json", json.dumps({"result": "passed", "backup_id": identifier,
                "pair_id": identifier, "method": "same stopped-writer interval as the data archive"}, indent=2) + "\n")
        names = {path.name for path in directory.iterdir()}
        if names not in (ARTIFACTS, ARTIFACTS - {"SHA256SUMS"}):
            raise setup.SetupError("captured artifact set is incomplete: " + component)
        proof = json.loads((directory / "validation.json").read_text())
        metadata = json.loads((directory / "metadata.json").read_text())
        if (proof.get("result") != "passed" or proof.get("backup_id") != identifier or
                metadata.get("backup_id") != identifier or metadata.get("pair_id") != identifier or
                metadata.get("claim") != setup.PROTECTED[component]["claim"] or
                (directory / "backup-id").read_text().strip() != identifier):
            raise setup.SetupError("captured pair, validation, or claim identity differs")
        expected = json.loads((directory / "contents.json").read_text())
        with tarfile.open(directory / "volume.tar", mode="r:") as archive:
            entries = archive.getmembers()
            if [entry.name for entry in entries] != [item["path"] for item in expected]:
                raise setup.SetupError("captured archive paths differ from their source manifest")
            for entry, item in zip(entries, expected):
                if (entry.uid, entry.gid, entry.mode) != (item["uid"], item["gid"], item["mode"]):
                    raise setup.SetupError("captured archive ownership or mode differs from its source manifest")
                exact_mtime = f"{item['mtime_ns'] // 1000000000}.{item['mtime_ns'] % 1000000000:09d}"
                if entry.pax_headers.get("mtime") != exact_mtime:
                    raise setup.SetupError("captured archive timestamp differs from its source manifest")
                if item["type"] == "directory":
                    if not entry.isdir():
                        raise setup.SetupError("captured directory type changed")
                elif item["type"] == "file":
                    if not entry.isfile() or entry.size != item["bytes"]:
                        raise setup.SetupError("captured file type or size changed")
                    checksum = hashlib.sha256()
                    with archive.extractfile(entry) as stream:
                        while chunk := stream.read(1024 * 1024):
                            checksum.update(chunk)
                    if checksum.hexdigest() != item["sha256"]:
                        raise setup.SetupError("captured archive contents differ from their source manifest")
                else:
                    raise setup.SetupError("unsupported captured source type")
        sync_directory(directory)


def preflight():
    proof = saved(setup.RECOVERY / "endpoint-verification.json")
    if proof.get("result") != "passed" or proof.get("repositories") != list(setup.CLAIMS):
        raise setup.SetupError("both backup repositories must pass endpoint setup first")
    identities = setup.live_preflight()
    server = setup.get_object("deployment", "restic-server", NS)
    if not server or server.get("status", {}).get("availableReplicas") != 1:
        raise setup.SetupError("backup endpoint is unavailable")
    clients = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "backups.mousses.xyz/restic-client=true",
                                   "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in clients):
        raise setup.SetupError("another n8n repository worker is still active")
    credentials = saved(setup.RECOVERY / "credentials.json")
    for component in COMPONENTS:
        name = setup.PROTECTED[component]["claim"]
        setup.ensure_secret("restic-" + name, setup.credential_fields(name, credentials["repositories"][name]))
        setup.save_secret("restic-" + name)
        claim = setup.get_object("pvc", component + "-backup-staging", NS)
        volume = setup.get_object("pv", "n8n-" + component + "-backup-staging")
        if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != volume["metadata"]["name"] or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("local", {}).get("path") != str(staging(component)) or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
            raise setup.SetupError("backup staging binding changed: " + component)
        setup.checked_directory(staging(component))
        attributes = staging(component).stat()
        if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o2770):
            raise setup.SetupError("backup staging ownership or mode changed")
        if shutil.disk_usage(staging(component)).free < 100 * 1024**3:
            raise setup.SetupError("less than 100 GiB remains on the array")
    setup.save_secret("restic-root-ca")
    return identities


def backup():
    check_units()
    recover_writers()
    before = preflight()
    if not STATE.exists() and not STATE.is_symlink():
        save(STATE, {"backup_id": "initial-" + secrets.token_hex(16), "started_at": now()})
    identifier = saved(STATE).get("backup_id")
    if not valid_id(identifier):
        raise setup.SetupError("invalid saved initial backup generation")
    apply_sources(identifier)
    capture_pair(identifier)
    after = setup.live_preflight(check_resources=False)
    if any(before[component] != after[component] for component in COMPONENTS) or (
            before["application"]["deployment_uid"] != after["application"]["deployment_uid"] or
            before["application"]["template"] != after["application"]["template"]):
        raise setup.SetupError("production storage, image, or controller changed during backup")
    for component in COMPONENTS:
        upload(component, identifier)
        verify_backup(component, identifier)
    setup.live_preflight(check_resources=False)
    save(setup.RECOVERY / "initial-backup-verification.json", {
        "result": "passed", "backup_id": identifier, "completed_at": now(),
        "components": {component: saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json"))
                       for component in COMPONENTS},
    })
    print("both n8n initial backups verified; isolated restores, daily scheduling, and independent credential storage remain pending", flush=True)


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--recover-writers-only", action="store_true")
    args = arguments.parse_args()
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    with ExitStack() as locks:
        locks.enter_context(setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True))
        if args.recover_writers_only:
            recover_writers()
        else:
            locks.enter_context(setup.operation_lock(setup.COORDINATION_LOCK, create=False))
            print("n8n backup worker serialized with the existing namespace backup workers", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print("n8n backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, sqlite3.Error, tarfile.TarError, subprocess.TimeoutExpired):
        print("n8n backup stopped: filesystem, API, or archive validation failed; private values omitted", file=sys.stderr)
        sys.exit(1)
