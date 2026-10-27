#!/usr/bin/env python3
"""Capture and verify LibreChat's five initial backups with a recoverable writer pause."""

import argparse
import base64
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
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location(
    "librechat_setup", Path(__file__).with_name("prepare-librechat.py"),
)
setup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(setup)

NS = setup.NAMESPACE
COMPONENTS = tuple(setup.PROTECTED)
FILE_COMPONENTS = ("data", "uploads", "images")
MANIFESTS = setup.MANIFESTS / "data"
STATE = setup.RECOVERY / "initial-backup.json"
QUIESCENCE = setup.RECOVERY / "writer-quiescence.json"
COMPLETE = setup.RECOVERY / "initial-backup-verification.json"
PLACEHOLDER = "controlled-by-backup-runner"
ARTIFACTS = {
    "data": {"volume.tar", "contents.json", "configuration.json", "metadata.json", "validation.json", "backup-id", "SHA256SUMS"},
    "uploads": {"volume.tar", "contents.json", "configuration.json", "metadata.json", "validation.json", "backup-id", "SHA256SUMS"},
    "images": {"volume.tar", "contents.json", "configuration.json", "metadata.json", "validation.json", "backup-id", "SHA256SUMS"},
    "mongodb": {"mongodb.archive.gz", "configuration.json", "database.json", "metadata.json", "validation.json", "backup-id", "SHA256SUMS"},
    "meilisearch": {"meilisearch.dump", "configuration.json", "search.json", "metadata.json", "validation.json", "backup-id", "SHA256SUMS"},
}
SERVICE = "k3s-librechat-backup-initial.service"
RECOVERY_SERVICE = "k3s-librechat-backup-recovery.service"
INSTALL = Path("/usr/local/lib/k3s-backups/librechat")
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


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"initial-[0-9a-f]{32}", value)


def staging(component):
    return setup.BASE / "backup-staging" / setup.PROTECTED[component]["claim"]


def pending(component, identifier):
    return staging(component) / (".pending-" + identifier)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rename_no_replace(source, destination):
    library = ctypes.CDLL(None, use_errno=True)
    method = getattr(library, "renameat2", None)
    if method is None:
        raise setup.SetupError("Linux renameat2 is required to publish without replacement")
    method.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    method.restype = ctypes.c_int
    if method(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise setup.SetupError("capture publication stopped; existing and pending data were retained")


def artifact_write(path, content):
    encoded = content.encode() if isinstance(content, str) else content
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as existing:
            attrs = os.fstat(existing.fileno())
            if (not stat.S_ISREG(attrs.st_mode) or attrs.st_nlink != 1 or
                    (attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) != (999, 999, 0o600) or
                    existing.read() != encoded):
                raise setup.SetupError("existing artifact differs; preserving it: " + path.name)
        return
    with os.fdopen(descriptor, "wb") as output:
        output.write(encoded)
        output.flush()
        os.fchown(output.fileno(), 999, 999)
        os.fsync(output.fileno())


def digest_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise setup.SetupError("backup artifact is not an independent regular file")
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise setup.SetupError("backup artifact changed while hashing")
        return digest.hexdigest()


def controller_hash(resource):
    spec = {key: value for key, value in resource["spec"].items() if key != "replicas"}
    return fingerprint(spec)


def guarded_patch(kind, name, resource, operations):
    tests = [{"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": resource["metadata"]["resourceVersion"]}]
    setup.kube("patch", kind, name, "-n", NS, "--type=json", "-p", json.dumps(tests + operations),
               capture=True, sensitive=True)


def parse_stream(output):
    decoder, resources = json.JSONDecoder(), []
    while output.strip():
        resource, end = decoder.raw_decode(output.lstrip())
        resources.extend(resource.get("items", [resource]))
        output = output.lstrip()[end:]
    return resources


def apply_sources(identifier):
    rendered = setup.kube("kustomize", str(MANIFESTS), capture=True).stdout
    output = setup.kube("create", "--dry-run=client", "-f", "-", "-o", "json",
                        stdin=rendered, capture=True).stdout
    resources = parse_stream(output)
    expected_names = {component + "-backup" for component in COMPONENTS}
    if (len(resources) != len(expected_names) or
            any(item.get("kind") != "ReplicationSource" or item.get("apiVersion") != "volsync.backube/v1alpha1" or
                item.get("metadata", {}).get("namespace") != NS for item in resources) or
            {item["metadata"].get("name") for item in resources} != expected_names):
        raise setup.SetupError("the source bundle does not contain exactly the five approved LibreChat repositories")
    for resource in resources:
        name = resource["metadata"]["name"]
        current = setup.get_object("replicationsource", name, NS)
        if current:
            expected = {key: value for key, value in resource["spec"].items() if key not in ("paused", "trigger")}
            actual = {key: value for key, value in current["spec"].items() if key not in ("paused", "trigger")}
            if (current["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                    actual != expected or current["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier) or
                    (current["spec"].get("trigger", {}).get("manual") == PLACEHOLDER and not current["spec"].get("paused"))):
                raise setup.SetupError("existing VolSync source differs from the reviewed bundle: " + name)
        else:
            setup.kube("create", "-f", "-", stdin=json.dumps(resource), capture=True)


def controller():
    result = setup.get_object("deployment", "librechat", NS, sensitive=True)
    if not result:
        raise setup.SetupError("LibreChat Deployment disappeared")
    return result


def check_journal(state):
    if (state.get("version") != 1 or not valid_id(state.get("backup_id")) or
            state.get("phase") not in ("quiescing", "captured", "resumed") or
            type(state.get("capture_complete")) is not bool or state.get("replicas") != 1 or
            state.get("controller_uid") != setup.WORKLOADS["librechat"][1] or
            not re.fullmatch(r"[0-9a-f]{64}", state.get("controller_sha256", "")) or
            not isinstance(state.get("pod_uids"), list) or len(state["pod_uids"]) != 1 or
            set(state.get("bindings", {})) != set(COMPONENTS)):
        raise setup.SetupError("invalid durable LibreChat writer journal")
    for component, binding in state["bindings"].items():
        if set(binding) != {"claim_uid", "volume_uid", "device", "inode"}:
            raise setup.SetupError("invalid saved source binding: " + component)
    mongo_lock = state.get("mongodb_fsync_lock")
    if mongo_lock is not None:
        if (not isinstance(mongo_lock, dict) or
                mongo_lock.get("phase") not in ("acquiring", "locked", "releasing", "released", "conflict") or
                not isinstance(mongo_lock.get("pod_identity"), dict) or
                set(mongo_lock["pod_identity"]) != {"pod_uid", "container_id", "restarts"} or
                not isinstance(mongo_lock["pod_identity"].get("pod_uid"), str) or
                not isinstance(mongo_lock["pod_identity"].get("container_id"), str) or
                type(mongo_lock["pod_identity"].get("restarts")) is not int or
                mongo_lock.get("dump_phase") not in ("not_started", "running", "finished")):
            raise setup.SetupError("invalid durable MongoDB fsync-lock journal")


def checked_sources(state):
    for component, entry in setup.PROTECTED.items():
        path = entry["path"]
        attrs = path.lstat()
        expected = setup.PHYSICAL / path.relative_to("/filesystem")
        identity = state["bindings"][component]
        claim = setup.get_object("pvc", entry["claim"], NS)
        volume = setup.get_object("pv", entry["claim"])
        if (path.resolve(strict=True) != expected or not stat.S_ISDIR(attrs.st_mode) or
                (attrs.st_dev, attrs.st_ino) != (identity["device"], identity["inode"]) or
                not claim or not volume or claim["metadata"]["uid"] != identity["claim_uid"] or
                volume["metadata"]["uid"] != identity["volume_uid"] or claim.get("status", {}).get("phase") != "Bound" or
                volume.get("status", {}).get("phase") != "Bound" or claim["spec"].get("volumeName") != entry["volume"] or
                volume["spec"].get("claimRef", {}).get("uid") != identity["claim_uid"] or
                volume["spec"].get("local", {}).get("path") != str(path) or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
            raise setup.SetupError("production source binding or directory identity changed: " + component)
        setup.mounted_on_array(path)
    current = controller()
    if (current["metadata"]["uid"] != state["controller_uid"] or
            controller_hash(current) != state["controller_sha256"] or current["spec"].get("replicas") not in (0, 1)):
        raise setup.SetupError("LibreChat Deployment identity or template changed")
    return current


def application_pods():
    items = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    claims = {setup.PROTECTED[name]["claim"] for name in FILE_COMPONENTS}
    return [pod for pod in items if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
            (any(container.get("name") == "librechat" for container in pod["spec"].get("containers", [])) or
             any(volume.get("persistentVolumeClaim", {}).get("claimName") in claims
                 for volume in pod["spec"].get("volumes", [])))]


def runtime_containers():
    return json.loads(setup.run(["k3s", "crictl", "ps", "-o", "json"], capture=True,
                                sensitive=True, timeout=30).stdout)["containers"]


def assert_stopped(state):
    current = checked_sources(state)
    if current["spec"].get("replicas") != 0 or application_pods():
        raise setup.SetupError("LibreChat or another holder of its file claims is still running")
    old_uids = set(state["pod_uids"])
    for item in runtime_containers():
        labels = item.get("labels", {})
        if (labels.get("io.kubernetes.pod.uid") in old_uids or
                labels.get("io.kubernetes.pod.namespace") == NS and item.get("metadata", {}).get("name") == "librechat"):
            raise setup.SetupError("a LibreChat runtime container is still running")


def set_replicas(state, target):
    for attempt in range(5):
        current = checked_sources(state)
        if current["spec"].get("replicas") == target:
            return
        try:
            guarded_patch("deployment", "librechat", current,
                          [{"op": "replace", "path": "/spec/replicas", "value": target}])
            return
        except setup.SetupError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def wait_stopped(state):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        checked_sources(state)
        if not application_pods():
            assert_stopped(state)
            return
        time.sleep(2)
    raise setup.SetupError("LibreChat did not stop; durable recovery remains enabled")


def resume_writer(state):
    release_mongodb_fsync_lock(state)
    current = checked_sources(state)
    if current["spec"].get("replicas") == 0:
        wait_stopped(state)
        set_replicas(state, 1)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        current = checked_sources(state)
        pods = application_pods()
        status = current.get("status", {})
        if (len(pods) == 1 and setup.ready(pods[0]) and pods[0].get("status", {}).get("phase") == "Running" and
                status.get("observedGeneration", 0) >= current["metadata"]["generation"] and
                all(status.get(key, 0) == 1 for key in ("replicas", "readyReplicas", "updatedReplicas"))):
            identities = setup.live_preflight(check_resources=False)
            if any(identities["bindings"][name].get("claim_uid") != value["claim_uid"] or
                   identities["bindings"][name].get("volume_uid") != value["volume_uid"] or
                   identities["bindings"][name].get("directory_device") != value["device"] or
                   identities["bindings"][name].get("directory_inode") != value["inode"]
                   for name, value in state["bindings"].items()):
                raise setup.SetupError("LibreChat recovered on different storage bindings")
            journal = saved(QUIESCENCE)
            if journal.get("backup_id") != state.get("backup_id"):
                raise setup.SetupError("LibreChat writer journal changed during recovery")
            save(QUIESCENCE, {**journal, "phase": "resumed", "resumed_at": now()})
            print("LibreChat recovered on its original claims; capture downtime ended", flush=True)
            return
        time.sleep(3)
    raise setup.SetupError("LibreChat did not become ready; durable recovery remains enabled")


def recover_writer():
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        state = saved(QUIESCENCE)
        check_journal(state)
        if state["phase"] != "resumed":
            resume_writer(state)


def safe_source_path(component):
    path = setup.PROTECTED[component]["path"]
    expected = setup.PHYSICAL / path.relative_to("/filesystem")
    attrs = path.lstat()
    if not stat.S_ISDIR(attrs.st_mode) or path.resolve(strict=True) != expected:
        raise setup.SetupError("protected source path changed: " + component)
    setup.mounted_on_array(path)
    return path


def inspect_sources():
    """Reject metadata the archive format cannot preserve before stopping LibreChat."""
    for component in FILE_COMPONENTS:
        root = safe_source_path(component)
        device, pending_paths, entries = root.stat().st_dev, [root], 0
        while pending_paths:
            path = pending_paths.pop()
            attrs = path.lstat()
            entries += 1
            if (entries > 20000 or attrs.st_dev != device or os.listxattr(path, follow_symlinks=False) or
                    not (stat.S_ISDIR(attrs.st_mode) or stat.S_ISREG(attrs.st_mode)) or
                    (stat.S_ISREG(attrs.st_mode) and attrs.st_nlink != 1)):
                raise setup.SetupError("file source has unsupported links, metadata, special files, or nested mounts: " + component)
            if stat.S_ISDIR(attrs.st_mode):
                pending_paths.extend(path.iterdir())
    setup.inspect_file_layout()


class HashingReader:
    def __init__(self, stream, progress):
        self.stream, self.progress = stream, progress
        self.checksum = hashlib.sha256()

    def read(self, count):
        chunk = self.stream.read(count)
        self.checksum.update(chunk)
        self.progress(len(chunk))
        return chunk


def file_attrs(attrs):
    return (attrs.st_dev, attrs.st_ino, attrs.st_mode, attrs.st_uid, attrs.st_gid,
            attrs.st_nlink, attrs.st_size, attrs.st_mtime_ns, attrs.st_ctime_ns)


def archive_volume(component, state, destination):
    source = safe_source_path(component)
    device, contents = source.stat().st_dev, []
    copied, last_check = 0, time.monotonic()

    def progress(count):
        nonlocal copied, last_check
        copied += count
        if time.monotonic() - last_check >= 30:
            assert_stopped(state)
            print(component + " captured bytes: " + str(copied), flush=True)
            last_check = time.monotonic()

    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            def visit(path, relative):
                assert_stopped(state)
                before = path.lstat()
                if (before.st_dev != device or os.listxattr(path, follow_symlinks=False) or
                        not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)) or
                        (stat.S_ISREG(before.st_mode) and before.st_nlink != 1)):
                    raise setup.SetupError("source changed or contains unsupported file metadata")
                entry = {"path": relative, "uid": before.st_uid, "gid": before.st_gid,
                         "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns,
                         "source_inode": before.st_ino, "source_ctime_ns": before.st_ctime_ns}
                if stat.S_ISDIR(before.st_mode):
                    names = sorted(child.name for child in path.iterdir())
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uname = info.gname = ""
                    info.pax_headers["mtime"] = f"{before.st_mtime_ns // 1000000000}.{before.st_mtime_ns % 1000000000:09d}"
                    archive.addfile(info)
                    contents.append({**entry, "type": "directory"})
                    for name in names:
                        visit(path / name, name if relative == "." else relative + "/" + name)
                    if names != sorted(child.name for child in path.iterdir()):
                        raise setup.SetupError("source directory changed while LibreChat was stopped")
                else:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        if file_attrs(os.fstat(stream.fileno())) != file_attrs(before):
                            raise setup.SetupError("source file changed before capture")
                        info = archive.gettarinfo(fileobj=stream, arcname=relative)
                        info.uname = info.gname = ""
                        info.pax_headers["mtime"] = f"{before.st_mtime_ns // 1000000000}.{before.st_mtime_ns % 1000000000:09d}"
                        reader = HashingReader(stream, progress)
                        archive.addfile(info, reader)
                        if file_attrs(os.fstat(stream.fileno())) != file_attrs(before):
                            raise setup.SetupError("source file changed during capture")
                    contents.append({**entry, "type": "file", "bytes": before.st_size,
                                     "sha256": reader.checksum.hexdigest()})
                if file_attrs(path.lstat()) != file_attrs(before) or os.listxattr(path, follow_symlinks=False):
                    raise setup.SetupError("source metadata changed during capture")
            visit(source, ".")
        output.flush()
        os.fchown(output.fileno(), 999, 999)
        os.fsync(output.fileno())
    assert_stopped(state)
    return contents


def mongo_inventory():
    script = r'''const d=db.getSiblingDB("LibreChat");
const infos=d.getCollectionInfos().sort((a,b)=>a.name.localeCompare(b.name));
const collections=infos.filter(x=>x.type==="collection").map(x=>{const indexes=d.getCollection(x.name).getIndexes();return {name:x.name,count:d.getCollection(x.name).countDocuments(),indexes:indexes.map(i=>i.name).sort(),ttl_indexes:indexes.filter(i=>Object.prototype.hasOwnProperty.call(i,"expireAfterSeconds")).map(i=>i.name).sort()}});
const appdbs=db.getSiblingDB("admin").adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name).filter(x=>!["admin","config","local"].includes(x)).sort();
print(JSON.stringify({database:"LibreChat",database_names:appdbs,collections,views:infos.filter(x=>x.type==="view").map(x=>x.name).sort()}));'''
    result = setup.kube("exec", "-n", NS, "mongodb-0", "-c", "mongodb", "--", "mongosh", "--quiet", "--eval", script,
                        capture=True, sensitive=True, timeout=120)
    candidates = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if len(candidates) != 1:
        raise setup.SetupError("MongoDB inventory query returned an unexpected result")
    value = json.loads(candidates[0])
    if (value.get("database_names") != ["LibreChat"] or value.get("database") != "LibreChat" or
            value.get("views") or not isinstance(value.get("collections"), list)):
        raise setup.SetupError("MongoDB database topology or collection types changed")
    if any(not isinstance(item.get("count"), int) or item["count"] < 0 or not item.get("indexes")
           for item in value["collections"]):
        raise setup.SetupError("MongoDB collection counts or indexes could not be verified")
    return value


def mongo_admin_json(script, description):
    result = setup.kube("exec", "-n", NS, "mongodb-0", "-c", "mongodb", "--", "mongosh", "--quiet",
                        "--eval", script, capture=True, sensitive=True, timeout=120)
    candidates = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if len(candidates) != 1:
        raise setup.SetupError("MongoDB " + description + " returned unexpected output")
    try:
        value = json.loads(candidates[0])
    except json.JSONDecodeError:
        raise setup.SetupError("MongoDB " + description + " returned malformed metadata")
    if not isinstance(value, dict):
        raise setup.SetupError("MongoDB " + description + " returned malformed metadata")
    return value


def mongodb_process_identity():
    pod = setup.get_object("pod", "mongodb-0", NS)
    if (not pod or pod["metadata"].get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running" or
            not setup.ready(pod)):
        raise setup.SetupError("MongoDB is not ready for its protected backup operation")
    containers = [item for item in pod.get("status", {}).get("containerStatuses", [])
                 if item.get("name") == "mongodb"]
    if (len(containers) != 1 or not containers[0].get("ready") or not containers[0].get("containerID") or
            not isinstance(containers[0].get("restartCount"), int)):
        raise setup.SetupError("MongoDB container identity is unavailable")
    return {"pod_uid": pod["metadata"]["uid"], "container_id": containers[0]["containerID"],
            "restarts": containers[0]["restartCount"]}


def mongodb_fsync_locked():
    value = mongo_admin_json(
        'const r=db.getSiblingDB("admin").adminCommand({currentOp:1});'
        'print(JSON.stringify({ok:r.ok===1,locked:r.fsyncLock===true}));', "write-lock status check")
    if value.get("ok") is not True or type(value.get("locked")) is not bool:
        raise setup.SetupError("MongoDB write-lock status could not be verified")
    return value["locked"]


def save_mongodb_fsync_lock(state, lock):
    journal = saved(QUIESCENCE)
    check_journal(journal)
    if journal.get("backup_id") != state.get("backup_id"):
        raise setup.SetupError("MongoDB write-lock journal belongs to another backup")
    save(QUIESCENCE, {**journal, "mongodb_fsync_lock": lock})


def release_mongodb_fsync_lock(state):
    journal = saved(QUIESCENCE) if QUIESCENCE.exists() or QUIESCENCE.is_symlink() else None
    if not journal:
        raise setup.SetupError("LibreChat writer journal is missing; MongoDB lock state cannot be proven")
    check_journal(journal)
    if journal.get("backup_id") != state.get("backup_id"):
        raise setup.SetupError("MongoDB write-lock journal belongs to another backup")
    lock = journal.get("mongodb_fsync_lock")
    locked = mongodb_fsync_locked()
    if lock and lock.get("dump_phase") == "running":
        if lock.get("phase") != "conflict":
            save_mongodb_fsync_lock(state, {**lock, "phase": "conflict", "dump_exit_unconfirmed_at": now(),
                                            "observed_locked": locked})
        raise setup.SetupError("MongoDB dump exit is unconfirmed; preserving the lock state and keeping LibreChat stopped")
    if not locked:
        if lock:
            save_mongodb_fsync_lock(state, {**lock, "phase": "released", "released_at": now(),
                                            "recovered_unlocked": True})
        return
    if not lock:
        raise setup.SetupError("MongoDB has an fsync lock with no backup ownership record; LibreChat remains stopped")
    phase = lock["phase"]
    if phase == "released":
        save_mongodb_fsync_lock(state, {**lock, "phase": "conflict", "unexpected_lock_at": now()})
        raise setup.SetupError("MongoDB is locked despite a released journal; preserving the lock and LibreChat")
    if phase == "conflict":
        raise setup.SetupError("MongoDB fsync-lock state needs manual inspection; LibreChat remains stopped")
    if phase in ("acquiring", "releasing"):
        save_mongodb_fsync_lock(state, {**lock, "phase": "conflict", "ambiguous_command_at": now()})
        raise setup.SetupError("MongoDB fsync-lock command outcome is ambiguous; preserving the lock and LibreChat")
    if phase != "locked":
        raise setup.SetupError("MongoDB fsync-lock journal is not in a releasable state; LibreChat remains stopped")
    identity = mongodb_process_identity()
    if identity != lock["pod_identity"]:
        save_mongodb_fsync_lock(state, {**lock, "phase": "conflict", "process_changed_at": now()})
        raise setup.SetupError("MongoDB process changed while locked; preserving the lock and LibreChat")
    save_mongodb_fsync_lock(state, {**lock, "phase": "releasing", "release_started_at": now()})
    response = mongo_admin_json(
        'const r=db.getSiblingDB("admin").adminCommand({fsyncUnlock:1});'
        'print(JSON.stringify({ok:r.ok===1,lock_count:Number(r.lockCount)}));', "write-lock release")
    locked_after = mongodb_fsync_locked()
    if (response.get("ok") is not True or type(response.get("lock_count")) is not int or
            response["lock_count"] != 0 or locked_after):
        save_mongodb_fsync_lock(state, {**lock, "phase": "conflict", "observed_lock_count": response.get("lock_count"),
                                        "release_failed_at": now()})
        raise setup.SetupError("MongoDB remains locked after the backup lock release; LibreChat remains stopped")
    save_mongodb_fsync_lock(state, {**lock, "phase": "released", "released_at": now()})


def acquire_mongodb_fsync_lock(state):
    identity = mongodb_process_identity()
    if mongodb_fsync_locked():
        raise setup.SetupError("MongoDB already has an fsync lock; preserving it and LibreChat")
    record = {"phase": "acquiring", "pod_identity": identity, "dump_phase": "not_started", "started_at": now()}
    save_mongodb_fsync_lock(state, record)
    response = mongo_admin_json(
        'const r=db.getSiblingDB("admin").adminCommand({fsync:1,lock:true});'
        'print(JSON.stringify({ok:r.ok===1,lock_count:Number(r.lockCount)}));', "write-lock acquisition")
    if response.get("ok") is not True or type(response.get("lock_count")) is not int or response["lock_count"] < 1:
        raise setup.SetupError("MongoDB fsync lock acquisition was not confirmed; preserving ambiguous state")
    record = {**record, "phase": "locked", "lock_count": response["lock_count"], "locked_at": now()}
    save_mongodb_fsync_lock(state, record)
    if response["lock_count"] != 1:
        raise setup.SetupError("MongoDB fsync lock count is unexpected; preserving the external lock")
    if mongodb_process_identity() != identity or not mongodb_fsync_locked():
        raise setup.SetupError("MongoDB fsync lock ownership or status could not be verified")
    return record


def capture_mongodb(state, destination):
    lock = None
    log_path = setup.RECOVERY / "mongodb-initial-dump.stderr.log"
    try:
        lock = acquire_mongodb_fsync_lock(state)
        before = mongo_inventory()
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as errors:
            command = ["k3s", "kubectl", "--request-timeout=20s", "exec", "-n", NS,
                       "mongodb-0", "-c", "mongodb", "--", "mongodump", "--quiet",
                       "--db=LibreChat", "--archive", "--gzip"]
            with destination.open("xb") as output:
                save_mongodb_fsync_lock(state, {**lock, "dump_phase": "running", "dump_started_at": now()})
                try:
                    process = subprocess.Popen(command, stdout=output, stderr=errors)
                except OSError:
                    save_mongodb_fsync_lock(state, {**lock, "dump_phase": "not_started"})
                    raise
                try:
                    status = process.wait(timeout=600)
                except BaseException:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    raise
                if status:
                    raise setup.SetupError("MongoDB native dump failed; its remote exit is unconfirmed and the database remains locked")
                save_mongodb_fsync_lock(state, {**lock, "dump_phase": "finished", "dump_finished_at": now(),
                                               "dump_exit_code": 0})
                output.flush()
                os.fsync(output.fileno())
                os.fchown(output.fileno(), 999, 999)
        after = mongo_inventory()
        if before != after:
            raise setup.SetupError("MongoDB contents changed during the fsync-locked dump")
        attrs = destination.stat()
        with destination.open("rb") as archive:
            header = archive.read(2)
        if attrs.st_size < 18 or header != b"\x1f\x8b":
            raise setup.SetupError("MongoDB archive is empty or is not gzip-compressed")
        assert_stopped(state)
    finally:
        if lock is not None or QUIESCENCE.exists():
            release_mongodb_fsync_lock(state)
        if log_path.exists():
            os.chmod(log_path, 0o600)
    return {"before": before, "after": after, "archive_bytes": attrs.st_size,
            "sha256": digest_file(destination), "writer_quiesced": True,
            "fsync_lock_acquired": lock is not None, "fsync_lock_released": True,
            "fsync_dump_completed": True, "fsync_lock_pod_uid": lock["pod_identity"]["pod_uid"]}


def meili_key():
    resource = setup.get_object("secret", "librechat-env", NS, sensitive=True)
    saved_secret = json.loads(setup.private_read(setup.RECOVERY / "librechat-env.secret.json"))
    if not resource or setup.portable(resource) != saved_secret:
        raise setup.SetupError("Meilisearch credential Secret changed since its private recovery capture")
    try:
        key = base64.b64decode(resource["data"]["MEILI_MASTER_KEY"], validate=True).decode()
    except (KeyError, ValueError, UnicodeDecodeError):
        raise setup.SetupError("Meilisearch credential is missing or malformed")
    if not key or len(key) > 16384:
        raise setup.SetupError("Meilisearch credential is empty or too large")
    return key


def meili_request(port, key, route, *, method="GET", body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request("http://127.0.0.1:" + str(port) + route, data=data, method=method,
                                     headers={"Authorization": "Bearer " + key,
                                              **({"Content-Type": "application/json"} if data is not None else {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise setup.SetupError("Meilisearch returned oversized metadata")
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        raise setup.SetupError("Meilisearch API returned HTTP " + str(error.code))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise setup.SetupError("Meilisearch API request failed: " + type(error).__name__)


def meili_snapshot(port, key):
    status, version = meili_request(port, key, "/version")
    if status != 200 or version.get("pkgVersion") != "1.35.1":
        raise setup.SetupError("Meilisearch version differs from its inventoried image")
    status, task_list = meili_request(port, key, "/tasks?statuses=enqueued%2Cprocessing&limit=1000")
    if status != 200 or task_list.get("results") != [] or task_list.get("total") != 0:
        raise setup.SetupError("Meilisearch has active tasks; preserving its existing state")
    status, stats = meili_request(port, key, "/stats")
    indexes = stats.get("indexes") if status == 200 else None
    if not isinstance(indexes, dict) or any(not isinstance(item, dict) or
            type(item.get("numberOfDocuments")) is not int or item["numberOfDocuments"] < 0
            for item in indexes.values()):
        raise setup.SetupError("Meilisearch index statistics could not be verified")
    return {"version": version["pkgVersion"], "indexes": indexes,
            "index_count": len(indexes),
            "document_count": sum(item["numberOfDocuments"] for item in indexes.values())}


def create_meili_dump(state, port, key):
    before = meili_snapshot(port, key)
    status, created = meili_request(port, key, "/dumps", method="POST", timeout=30)
    task_uid = created.get("taskUid") if status == 202 else None
    if type(task_uid) is not int or task_uid < 0 or created.get("type") != "dumpCreation":
        raise setup.SetupError("Meilisearch did not accept a native dump task")
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        assert_stopped(state)
        status, task = meili_request(port, key, "/tasks/" + str(task_uid), timeout=20)
        if status != 200 or task.get("uid") != task_uid or task.get("type") != "dumpCreation":
            raise setup.SetupError("Meilisearch dump task identity changed")
        if task.get("status") == "succeeded":
            dump_uid = task.get("details", {}).get("dumpUid")
            if not isinstance(dump_uid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", dump_uid):
                raise setup.SetupError("Meilisearch completed without a valid dump identity")
            after = meili_snapshot(port, key)
            if before != after:
                raise setup.SetupError("Meilisearch indexes changed during the stopped-writer dump")
            return {"task_uid": task_uid, "dump_uid": dump_uid, "before": before,
                    "after": after, "completed_at": task.get("finishedAt")}
        if task.get("status") == "failed":
            save(setup.RECOVERY / "meilisearch-dump-task-failure.json", {
                "task_uid": task_uid, "status": task.get("status"), "error": task.get("error"),
                "finished_at": task.get("finishedAt"),
            })
            raise setup.SetupError("Meilisearch dump task failed; private diagnostics retained")
        time.sleep(3)
    raise setup.SetupError("Meilisearch dump task timed out; source writer recovery remains enabled")


def copy_meili_dump(component_path, state, dump):
    worker_budget()
    if component_path != pending("meilisearch", state["backup_id"]):
        raise setup.SetupError("Meilisearch dump destination differs from its durable pending path")
    directory = component_path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or
            (directory.st_uid, directory.st_gid, stat.S_IMODE(directory.st_mode)) != (999, 999, 0o700)):
        raise setup.SetupError("Meilisearch dump destination directory identity changed")
    # Meilisearch owns its dump as uid 1000; omit fsGroup on the read-only source
    # mount and use uid 1000 for the copy. The root runner returns staging to 999.
    os.chown(component_path, 1000, 1000, follow_symlinks=False)
    os.chmod(component_path, 0o700, follow_symlinks=False)
    sync_directory(component_path.parent)
    template = json.loads(setup.kube("create", "--dry-run=client", "-f",
        str(MANIFESTS / "meilisearch-dump-copy-job.yaml"), "-o", "json", capture=True).stdout)
    pod = template["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if (template.get("metadata", {}).get("namespace") != NS or
            pod.get("nodeSelector") != {"kubernetes.io/hostname": "openmediavault"} or
            pod.get("securityContext", {}).get("runAsUser") != 1000 or
            pod.get("securityContext", {}).get("runAsGroup") != 1000 or
            pod.get("securityContext", {}).get("fsGroup") is not None or
            999 not in pod.get("securityContext", {}).get("supplementalGroups", []) or
            container.get("image") != "quay.io/backube/volsync@sha256:0d03a6aad57569eba2c0eaa0848cf4a908d9744b372ff2224bb291a320f36d76" or
            {volume["name"]: volume.get("persistentVolumeClaim", {}) for volume in pod.get("volumes", [])} != {
                "source": {"claimName": "librechat-meilisearch", "readOnly": True},
                "staging": {"claimName": "meilisearch-backup-staging"}} or
            {mount["name"]: mount.get("readOnly", False) for mount in container.get("volumeMounts", [])} != {
                "source": True, "staging": False}):
        raise setup.SetupError("Meilisearch dump copy Job identity, source mount, or permissions differ")
    env = container["env"]
    replacements = set()
    for item in env:
        if item["name"] == "DUMP_UID":
            item["value"] = dump["dump_uid"]
            replacements.add("DUMP_UID")
        elif item["name"] == "PENDING_DIRECTORY":
            item["value"] = component_path.name
            replacements.add("PENDING_DIRECTORY")
    if replacements != {"DUMP_UID", "PENDING_DIRECTORY"}:
        raise setup.SetupError("Meilisearch dump copy Job template is incomplete")
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json",
                                     stdin=json.dumps(template), capture=True).stdout)
    name = created["metadata"]["name"]
    print("job/" + name + " created (read-only copy of the completed Meilisearch dump)", flush=True)
    try:
        wait_job(name)
    except setup.SetupError:
        output = setup.kube("logs", "-n", NS, "job/" + name, "-c", "copy", capture=True,
                            sensitive=True, check=False, timeout=20)
        if not output.returncode:
            save(setup.RECOVERY / "meilisearch-dump-copy-failure.json", {
                "job": name, "log_sha256": hashlib.sha256(output.stdout.encode()).hexdigest(),
            })
        raise
    logs = setup.kube("logs", "-n", NS, "job/" + name, "-c", "copy", capture=True,
                      sensitive=True, timeout=20).stdout
    rows = [line.removeprefix("MEILISEARCH_DUMP_COPY_JSON=") for line in logs.splitlines()
            if line.startswith("MEILISEARCH_DUMP_COPY_JSON=")]
    if len(rows) != 1:
        raise setup.SetupError("Meilisearch dump copy did not return one result")
    try:
        result = json.loads(rows[0])
    except json.JSONDecodeError:
        raise setup.SetupError("Meilisearch dump copy returned malformed verification metadata")
    destination = component_path / "meilisearch.dump"
    if (result.get("result") != "passed" or result.get("dump_uid") != dump["dump_uid"] or
            result.get("bytes") != destination.stat().st_size or result.get("sha256") != digest_file(destination)):
        raise setup.SetupError("copied Meilisearch dump identity or checksum differs")
    os.chown(destination, 999, 999, follow_symlinks=False)
    os.chmod(destination, 0o600, follow_symlinks=False)
    os.chown(component_path, 999, 999, follow_symlinks=False)
    os.chmod(component_path, 0o700, follow_symlinks=False)
    sync_directory(component_path)
    sync_directory(component_path.parent)
    save(setup.RECOVERY / "meilisearch-dump-copy-job.json", created)
    return result


def configuration_snapshot(baseline):
    resources = []
    for kind, name in (("deployment", "librechat"), ("configmap", "librechat-config"),
                       ("configmap", "librechat-custom-config")):
        resource = setup.get_object(kind, name, NS, sensitive=True)
        saved_path = setup.RECOVERY / (name + ".configmap.json") if kind == "configmap" else None
        if not resource or kind == "configmap" and (
                not saved_path.exists() or setup.portable(resource) != json.loads(setup.private_read(saved_path))):
            raise setup.SetupError("application configuration changed since endpoint setup: " + name)
        resources.append(setup.portable(resource))
    credential_proof = saved(setup.RECOVERY / "application-credentials-verification.json")
    if credential_proof.get("result") != "passed" or credential_proof.get("fields_verified") != 8:
        raise setup.SetupError("private application credential recovery evidence is incomplete")
    return {"namespace": NS, "workload": resources[0], "config_maps": resources[1:],
            "secret_identities": credential_proof["sources"], "source_bindings": baseline["bindings"],
            "captured_at": now()}


def write_artifacts(identifier, baseline, source_state, mongo, search, copied):
    config = source_state["configuration"]
    for component in COMPONENTS:
        current = staging(component) / "current"
        if current.exists() or current.is_symlink():
            inspect_artifacts(component, identifier)
            continue
        folder = pending(component, identifier)
        if not folder.is_dir() or folder.is_symlink():
            raise setup.SetupError("complete capture journal has no pending artifacts: " + component)
        common = {"backup_id": identifier, "component": component,
                  "claim": setup.PROTECTED[component]["claim"], "captured_at": source_state["started_at"],
                  "completed_at": source_state["artifacts_completed_at"],
                  "source_binding": source_state["bindings"][component],
                  "production_workloads": {name: {key: baseline["workloads"][name][key]
                                                   for key in ("controller_uid", "spec_sha256", "image_id")}
                                           for name in baseline["workloads"]}}
        artifact_write(folder / "configuration.json", json.dumps(config, indent=2) + "\n")
        if component in FILE_COMPONENTS:
            entries = source_state["file_contents"][component]
            artifact_write(folder / "contents.json", json.dumps(entries, indent=2) + "\n")
            details = {"files": sum(item["type"] == "file" for item in entries),
                       "directories": sum(item["type"] == "directory" for item in entries),
                       "file_bytes": sum(item.get("bytes", 0) for item in entries),
                       "method": "tar archive captured while the LibreChat Deployment was stopped"}
            proof = {"result": "passed", "backup_id": identifier, "component": component,
                     "source_file_metadata_verified": True, "writer_quiesced": True}
        elif component == "mongodb":
            artifact_write(folder / "database.json", json.dumps(mongo, indent=2) + "\n")
            details = {"database": "LibreChat", "collections": len(mongo["before"]["collections"]),
                       "documents": sum(item["count"] for item in mongo["before"]["collections"]),
                       "archive_bytes": mongo["archive_bytes"], "archive_sha256": mongo["sha256"],
                       "method": "mongodump archive under MongoDB fsync write lock while the application writer was stopped",
                       "ttl_indexes_present": any(item.get("ttl_indexes") for item in mongo["before"]["collections"])}
            proof = {"result": "passed", "backup_id": identifier, "component": component,
                     "collection_counts_stable": mongo["before"] == mongo["after"],
                     "archive_gzip_verified": True, "writer_quiesced": True,
                     "fsync_lock_acquired": mongo["fsync_lock_acquired"],
                     "fsync_lock_released": mongo["fsync_lock_released"],
                     "fsync_dump_completed": mongo["fsync_dump_completed"]}
        else:
            artifact_write(folder / "search.json", json.dumps({"task_uid": search["task_uid"],
                "dump_uid": search["dump_uid"], "before": search["before"], "after": search["after"],
                "copy": copied}, indent=2) + "\n")
            details = {"indexes": search["before"]["index_count"],
                       "documents": search["before"]["document_count"], "dump_uid": search["dump_uid"],
                       "dump_bytes": copied["bytes"], "dump_sha256": copied["sha256"],
                       "method": "Meilisearch native asynchronous dump task"}
            proof = {"result": "passed", "backup_id": identifier, "component": component,
                     "dump_task_succeeded": True, "index_statistics_stable": search["before"] == search["after"],
                     "dump_copy_sha256_verified": True, "writer_quiesced": True}
        artifact_write(folder / "metadata.json", json.dumps({**common, **details}, indent=2) + "\n")
        artifact_write(folder / "validation.json", json.dumps(proof, indent=2) + "\n")
        artifact_write(folder / "backup-id", identifier + "\n")
        sync_directory(folder)


def safe_relative(path):
    return bool(path) and not path.startswith("/") and ".." not in Path(path).parts


def validate_volume_archive(folder):
    contents = json.loads((folder / "contents.json").read_text())
    if not isinstance(contents, list) or not contents:
        raise setup.SetupError("file archive manifest is empty or malformed")
    with tarfile.open(folder / "volume.tar", "r:") as archive:
        members = archive.getmembers()
        if len(members) != len(contents):
            raise setup.SetupError("file archive and content manifest entry counts differ")
        for member, item in zip(members, contents):
            if (member.name != item.get("path") or not safe_relative(member.name) or
                    (member.uid, member.gid, member.mode) != (item.get("uid"), item.get("gid"), item.get("mode"))):
                raise setup.SetupError("file archive member metadata differs from its manifest")
            exact_mtime = f"{item['mtime_ns'] // 1000000000}.{item['mtime_ns'] % 1000000000:09d}"
            if member.pax_headers.get("mtime") != exact_mtime:
                raise setup.SetupError("file archive timestamp differs from its source manifest")
            if item.get("type") == "directory":
                if not member.isdir():
                    raise setup.SetupError("file archive directory type changed")
            elif item.get("type") == "file":
                if not member.isfile() or member.size != item.get("bytes"):
                    raise setup.SetupError("file archive type or size differs from its manifest")
                digest = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != item.get("sha256"):
                    raise setup.SetupError("file archive content checksum failed")
            else:
                raise setup.SetupError("file archive contains an unsupported entry")


def validate_mongo_archive(folder):
    archive = folder / "mongodb.archive.gz"
    import gzip
    expanded = 0
    with gzip.open(archive, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            expanded += len(chunk)
            if expanded > 10 * 1024**3:
                raise setup.SetupError("MongoDB archive expands beyond its approved staging capacity")
    database = json.loads((folder / "database.json").read_text())
    if (database.get("before") != database.get("after") or not database.get("writer_quiesced") or
            database.get("fsync_lock_acquired") is not True or database.get("fsync_lock_released") is not True or
            database.get("fsync_dump_completed") is not True):
        raise setup.SetupError("MongoDB backup consistency evidence differs")


def validate_meili_archive(folder):
    search = json.loads((folder / "search.json").read_text())
    if search.get("before") != search.get("after") or not search.get("dump_uid"):
        raise setup.SetupError("Meilisearch dump metadata is incomplete")
    found_metadata, names = False, set()
    entries = 0
    with tarfile.open(folder / "meilisearch.dump", "r:*") as archive:
        for member in archive:
            entries += 1
            name = member.name.removeprefix("./")
            if entries > 100000 or not safe_relative(name) or name in names:
                raise setup.SetupError("Meilisearch dump has unsafe or excessive archive paths")
            names.add(name)
            if not (member.isfile() or member.isdir()):
                raise setup.SetupError("Meilisearch dump contains an unsupported entry")
            if name == "metadata.json":
                if not member.isfile() or member.size > 1024 * 1024:
                    raise setup.SetupError("Meilisearch dump metadata file is invalid")
                with archive.extractfile(member) as stream:
                    contents = stream.read(1024 * 1024 + 1)
                if len(contents) > 1024 * 1024 or not isinstance(json.loads(contents), dict):
                    raise setup.SetupError("Meilisearch dump metadata cannot be parsed")
                found_metadata = True
    if not found_metadata or entries < 2:
        raise setup.SetupError("Meilisearch native dump is incomplete")


def publish(component, identifier):
    folder = pending(component, identifier)
    setup.checked_directory(folder.parent)
    if not folder.is_dir() or folder.is_symlink():
        raise setup.SetupError("pending capture directory is missing or changed: " + component)
    expected_names = ARTIFACTS[component] - {"SHA256SUMS"}
    if {path.name for path in folder.iterdir()} not in (expected_names, ARTIFACTS[component]):
        raise setup.SetupError("partial capture preserved for inspection: " + component)
    metadata = json.loads((folder / "metadata.json").read_text())
    proof = json.loads((folder / "validation.json").read_text())
    if ((folder / "backup-id").read_text().strip() != identifier or metadata.get("backup_id") != identifier or
            metadata.get("claim") != setup.PROTECTED[component]["claim"] or proof.get("backup_id") != identifier or
            proof.get("result") != "passed"):
        raise setup.SetupError("capture generation, claim, or validation evidence differs: " + component)
    if component in FILE_COMPONENTS:
        validate_volume_archive(folder)
    elif component == "mongodb":
        validate_mongo_archive(folder)
    else:
        validate_meili_archive(folder)
    for path in folder.iterdir():
        attrs = path.lstat()
        if not stat.S_ISREG(attrs.st_mode) or attrs.st_nlink != 1:
            raise setup.SetupError("backup artifact is not an independent regular file")
        os.chown(path, 999, 999, follow_symlinks=False)
        os.chmod(path, 0o600)
    checksums = "".join(digest_file(folder / name) + "  " + name + "\n"
                        for name in sorted(expected_names))
    sums = folder / "SHA256SUMS"
    if sums.exists():
        if sums.read_text() != checksums:
            raise setup.SetupError("saved checksum manifest differs; preserving capture")
    else:
        artifact_write(sums, checksums)
    os.chown(folder, 999, 999, follow_symlinks=False)
    os.chmod(folder, 0o700)
    sync_directory(folder)
    current = staging(component) / "current"
    if current.exists() or current.is_symlink():
        raise setup.SetupError("refusing to replace an existing published capture: " + component)
    rename_no_replace(folder, current)
    sync_directory(staging(component))
    inspect_artifacts(component, identifier)
    print("complete capture published: " + component, flush=True)


def inspect_artifacts(component, identifier):
    current = staging(component) / "current"
    if not current.exists():
        return None
    attrs = current.lstat()
    if (not stat.S_ISDIR(attrs.st_mode) or (attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) != (999, 999, 0o700) or
            {path.name for path in current.iterdir()} != ARTIFACTS[component]):
        raise setup.SetupError("published artifact directory or contents differ: " + component)
    checksums, names = (current / "SHA256SUMS").read_text(), set()
    for path in current.iterdir():
        attrs = path.lstat()
        if (not stat.S_ISREG(attrs.st_mode) or attrs.st_nlink != 1 or
                (attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) != (999, 999, 0o600)):
            raise setup.SetupError("published artifact permissions or type changed: " + component)
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9.-]+)", line)
        if not match or match[2] not in ARTIFACTS[component] - {"SHA256SUMS"} or match[2] in names:
            raise setup.SetupError("published checksum manifest is invalid: " + component)
        names.add(match[2])
        if digest_file(current / match[2]) != match[1]:
            raise setup.SetupError("published artifact checksum failed: " + component)
    if names != ARTIFACTS[component] - {"SHA256SUMS"} or (current / "backup-id").read_text().strip() != identifier:
        raise setup.SetupError("published capture is incomplete or from a different generation")
    return checksums


def claim_pods(claim):
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
            any(volume.get("persistentVolumeClaim", {}).get("claimName") == claim
                for volume in pod["spec"].get("volumes", []))]


def pause_completed_source(component, identifier, source_uid):
    name = component + "-backup"
    for attempt in range(5):
        source = setup.get_object("replicationsource", name, NS)
        if (not source or source["metadata"].get("uid") != source_uid or
                source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                source["spec"].get("trigger") != {"manual": identifier}):
            raise setup.SetupError("completed upload source identity changed before pausing: " + component)
        if source["spec"].get("paused") is True:
            return source
        if (not upload_complete(source, identifier) or claim_pods(component + "-backup-staging") or
                active_workers()):
            raise setup.SetupError("completed upload is not idle enough to pause: " + component)
        try:
            guarded_patch("replicationsource", name, source,
                          [{"op": "replace", "path": "/spec/paused", "value": True}])
            source = setup.get_object("replicationsource", name, NS)
            if (source and source["metadata"].get("uid") == source_uid and
                    source["spec"].get("trigger") == {"manual": identifier} and
                    source["spec"].get("paused") is True):
                return source
        except setup.SetupError:
            if attempt == 4:
                raise setup.SetupError("completed upload could not be safely paused: " + component)
        time.sleep(0.2)
    raise setup.SetupError("completed upload pause did not converge: " + component)


def ensure_source_idle(component, identifier):
    source = setup.get_object("replicationsource", component + "-backup", NS)
    if (source and source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == setup.MANAGER and
            source["spec"].get("trigger") == {"manual": identifier} and not source["spec"].get("paused") and
            upload_complete(source, identifier) and not claim_pods(component + "-backup-staging") and
            not active_workers()):
        source = pause_completed_source(component, identifier, source["metadata"]["uid"])
    if (not source or source["spec"].get("paused") is not True or
            source["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier) or
            claim_pods(component + "-backup-staging")):
        raise setup.SetupError("backup source or staging PVC is not idle and paused: " + component)


def assert_tree_unchanged(component, contents):
    root = safe_source_path(component)
    actual, pending_paths = {}, [(root, ".")]
    while pending_paths:
        path, relative = pending_paths.pop()
        attrs = path.lstat()
        actual[relative] = attrs
        if stat.S_ISDIR(attrs.st_mode):
            pending_paths.extend((child, child.name if relative == "." else relative + "/" + child.name)
                                 for child in path.iterdir())
    if set(actual) != {item["path"] for item in contents}:
        raise setup.SetupError("file source tree changed during its stopped-writer capture: " + component)
    for item in contents:
        attrs = actual[item["path"]]
        if ((attrs.st_ino, attrs.st_ctime_ns, attrs.st_mtime_ns, attrs.st_uid, attrs.st_gid,
             stat.S_IMODE(attrs.st_mode)) !=
                (item["source_inode"], item["source_ctime_ns"], item["mtime_ns"], item["uid"],
                 item["gid"], item["mode"]) or
                (item["type"] == "file" and attrs.st_size != item["bytes"])):
            raise setup.SetupError("file source changed during its stopped-writer capture: " + component)


def dependencies_unchanged():
    index = saved(setup.RECOVERY / "dependency-inventory.json")
    for kind, entries in index.get("resources", {}).items():
        for name, entry in entries.items():
            current = setup.get_object(kind, name, NS, sensitive=kind == "secret")
            if not entry.get("present"):
                if current is not None:
                    raise setup.SetupError("an optional application dependency appeared during capture: " + name)
                continue
            filename = entry.get("saved_file")
            if not filename or not current or current["metadata"].get("uid") != entry.get("uid"):
                raise setup.SetupError("application dependency identity changed during capture: " + name)
            saved_resource = json.loads(setup.private_read(setup.RECOVERY / filename))
            if setup.portable(current) != saved_resource:
                raise setup.SetupError("application dependency content changed during capture: " + name)


def dependent_workloads_unchanged(baseline):
    for name in ("mongodb", "meilisearch"):
        expected = baseline["workloads"][name]
        current = setup.get_object("statefulset", name, NS)
        if (not current or current["metadata"].get("uid") != expected["controller_uid"] or
                fingerprint(current["spec"]) != expected["spec_sha256"] or
                current["spec"].get("replicas") != 1):
            raise setup.SetupError("a LibreChat database/search controller changed during capture: " + name)
        pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
        matches = [pod for pod in pods if pod["metadata"].get("uid") == expected["pod_uid"]]
        if len(matches) != 1 or not setup.ready(matches[0]) or matches[0].get("status", {}).get("phase") != "Running":
            raise setup.SetupError("a LibreChat database/search pod is not stable during capture: " + name)
        statuses = [item for item in matches[0].get("status", {}).get("containerStatuses", [])
                    if item.get("name") == name]
        if (len(statuses) != 1 or statuses[0].get("containerID") != expected["container_id"] or
                statuses[0].get("imageID") != expected["image_id"] or
                statuses[0].get("restartCount") != expected["restarts"]):
            raise setup.SetupError("a LibreChat database/search container restarted during capture: " + name)


def competing_writers():
    sources = [safe_source_path(component).resolve(strict=True) for component in COMPONENTS]

    def overlaps(path):
        real = Path(path).resolve()
        return any(real.is_relative_to(source) or source.is_relative_to(real) for source in sources)

    volumes = json.loads(setup.kube("get", "pv", "-o", "json", capture=True).stdout)["items"]
    aliases, allowed_volumes = set(), {entry["claim"] for entry in setup.PROTECTED.values()}
    for volume in volumes:
        path = (volume["spec"].get("local") or volume["spec"].get("hostPath") or {}).get("path")
        if path and overlaps(path):
            if volume["metadata"]["name"] not in allowed_volumes:
                raise setup.SetupError("another PersistentVolume aliases protected LibreChat storage")
            reference = volume["spec"].get("claimRef", {})
            aliases.add((reference.get("namespace"), reference.get("name")))
    baseline = setup.live_preflight(check_resources=False, record=False)
    own = {value["pod_uid"] for value in baseline["workloads"].values()}
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    for pod in pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed") or pod["metadata"]["uid"] in own:
            continue
        for volume in pod["spec"].get("volumes", []):
            writable_mounts = [mount for group in ("containers", "initContainers", "ephemeralContainers")
                               for container in pod["spec"].get(group, [])
                               for mount in container.get("volumeMounts", [])
                               if mount["name"] == volume["name"] and not mount.get("readOnly")]
            if not writable_mounts:
                continue
            host = volume.get("hostPath", {}).get("path")
            claim = volume.get("persistentVolumeClaim", {})
            if ((host and overlaps(host)) or
                    (not claim.get("readOnly") and
                     (pod["metadata"]["namespace"], claim.get("claimName")) in aliases)):
                raise setup.SetupError("another pod can write protected LibreChat storage")
    mounts = json.loads(setup.run(["findmnt", "--json", "--list", "--output", "TARGET"], capture=True).stdout)["filesystems"]
    if any(Path(item["target"]).is_relative_to(source) and Path(item["target"]) != source
           for item in mounts for source in sources):
        raise setup.SetupError("a protected LibreChat source contains a nested mount")


def scheduling_backlog():
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") == "Pending" and not pod["spec"].get("nodeName") for pod in pods):
        raise setup.SetupError("pending pods may take LibreChat's released reservations; resolve them before capture")


def active_workers():
    pods = json.loads(setup.kube("get", "pods", "-A", "-l", "backups.mousses.xyz/restic-client=true",
                                "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]


def worker_budget():
    node = setup.get_object("node", "openmediavault")
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    setup.resource_budget(node, pods)


def preflight():
    endpoint = saved(setup.RECOVERY / "endpoint-verification.json")
    if (endpoint.get("result") != "passed" or not isinstance(endpoint.get("repositories"), list) or
            set(endpoint["repositories"]) != set(setup.CLAIMS) or
            len(endpoint["repositories"]) != len(COMPONENTS)):
        raise setup.SetupError("all five LibreChat repositories must pass endpoint setup first")
    baseline = setup.live_preflight()
    if endpoint.get("production", {}).get("bindings") != baseline["bindings"]:
        raise setup.SetupError("protected storage changed since repository endpoint setup")
    for name in ("librechat", "mongodb", "meilisearch"):
        before = endpoint["production"].get("workloads", {}).get(name, {})
        current = baseline["workloads"].get(name, {})
        if any(before.get(key) != current.get(key) for key in ("controller_uid", "spec_sha256", "image_id")):
            raise setup.SetupError("production workload changed since repository endpoint setup: " + name)
    if endpoint["production"].get("dependencies") != baseline.get("dependencies"):
        raise setup.SetupError("application credentials or configuration dependencies changed since endpoint setup")
    server = setup.get_object("deployment", "restic-server", NS)
    if not server or server.get("status", {}).get("availableReplicas") != 1:
        raise setup.SetupError("LibreChat Restic endpoint is unavailable")
    service = setup.get_object("service", "restic", NS)
    if not service or not (service["spec"].get("clusterIPs") or service["spec"].get("clusterIP")):
        raise setup.SetupError("LibreChat Restic service has no stable cluster address")
    if active_workers():
        raise setup.SetupError("another Restic worker is active; wait for it before starting LibreChat backup")
    credentials = saved(setup.RECOVERY / "credentials.json")
    for component in COMPONENTS:
        entry = setup.PROTECTED[component]
        claim_name = component + "-backup-staging"
        expected_pv = "librechat-" + claim_name
        claim = setup.get_object("pvc", claim_name, NS)
        volume = setup.get_object("pv", expected_pv)
        repository_claim = entry["claim"]
        setup.save_secret("restic-" + repository_claim)
        fields = setup.credential_fields(repository_claim, credentials["repositories"][repository_claim])
        secret = setup.get_object("secret", "restic-" + repository_claim, NS, sensitive=True)
        expected_data = {key: base64.b64encode(value.encode()).decode() for key, value in fields.items()}
        if not secret or secret.get("data") != expected_data:
            raise setup.SetupError("Restic repository credentials differ from the private recovery copy: " + repository_claim)
        expected_path = str(setup.BASE / "backup-staging" / repository_claim)
        if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
                volume.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != expected_pv or
                claim["spec"].get("storageClassName") != "librechat-backup-local" or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("claimRef", {}).get("namespace") != NS or
                volume["spec"].get("claimRef", {}).get("name") != claim_name or
                volume["spec"].get("local", {}).get("path") != expected_path or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
            raise setup.SetupError("backup staging PVC/PV binding changed: " + component)
        path = staging(component)
        setup.checked_directory(path)
        attrs = path.lstat()
        if ((attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) != (999, 999, 0o2770) or
                shutil.disk_usage(path).free < 100 * 1024**3):
            raise setup.SetupError("backup staging ownership or array free space differs: " + component)
    setup.save_secret("restic-root-ca")
    if shutil.disk_usage(setup.BASE).free < 100 * 1024**3:
        raise setup.SetupError("less than 100 GiB remains on the approved array")
    setup.resource_budget(setup.get_object("node", "openmediavault"),
                          json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"])
    inspect_sources()
    mongo_inventory()  # Validate topology before the writer is stopped.
    if mongodb_fsync_locked():
        raise setup.SetupError("MongoDB already has an fsync lock; preserving the live application")
    key = meili_key()
    process, stream, port = start_port_forward()
    try:
        wait_for_meili(process, port, key)
    finally:
        stop_port_forward(process, stream)
    configuration = configuration_snapshot(baseline)
    return baseline, configuration


def start_port_forward():
    path = setup.RECOVERY / "meilisearch-port-forward.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(descriptor, "wb")
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    process = subprocess.Popen(["k3s", "kubectl", "--request-timeout=20s", "port-forward", "-n", NS,
                                "service/meilisearch", f"127.0.0.1:{port}:7700"],
                               stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
    return process, stream, port


def stop_port_forward(process, stream):
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        stream.flush()
        stream.close()
        os.chmod(setup.RECOVERY / "meilisearch-port-forward.log", 0o600)


def wait_for_meili(process, port, key):
    deadline = time.monotonic() + 60
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise setup.SetupError("Meilisearch port-forward exited; private diagnostics retained")
        try:
            return meili_snapshot(port, key)
        except setup.SetupError as error:
            last_error = str(error)
            if "active tasks" in last_error or "version differs" in last_error or "statistics could not" in last_error:
                raise
            time.sleep(2)
    raise setup.SetupError("Meilisearch API did not become ready; last check: " + (last_error or "no response"))


def upload_complete(source, identifier):
    if not source:
        return False
    status = source.get("status", {})
    return (not source["spec"].get("paused") and source["spec"].get("trigger") == {"manual": identifier} and
            status.get("lastManualSync") == identifier and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(item.get("type") == "Synchronizing" and item.get("status") == "False" and
                item.get("reason") == "WaitingForManual" for item in status.get("conditions", [])))


def stop_failed_upload(component, identifier, source_uid, reason):
    path = setup.RECOVERY / ("initial-" + component + "-upload-failure.json")
    evidence = {"backup_id": identifier, "component": component, "failed_at": now(),
                "source_uid": source_uid, "reason": reason}
    save(path, evidence)
    for attempt in range(5):
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if (not source or source["metadata"].get("uid") != source_uid or
                source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                source["spec"].get("trigger") != {"manual": identifier}):
            raise setup.SetupError(reason + "; source identity changed, private evidence retained")
        save(path, {**evidence, "source": source})
        if source["spec"].get("paused") is True:
            break
        try:
            guarded_patch("replicationsource", component + "-backup", source,
                          [{"op": "add", "path": "/spec/paused", "value": True}])
            break
        except setup.SetupError:
            if attempt == 4:
                raise setup.SetupError(reason + "; failed to pause uploader, private evidence retained")
            time.sleep(0.2)
    raise setup.SetupError(reason + "; uploader paused, capture retained, no lock was removed")


def upload(component, identifier):
    checksums = inspect_artifacts(component, identifier)
    if not checksums:
        raise setup.SetupError("cannot upload without complete published artifacts: " + component)
    worker_budget()
    if active_workers():
        raise setup.SetupError("another Restic worker is active; uploader was not triggered")
    source = setup.get_object("replicationsource", component + "-backup", NS)
    if (not source or source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
            source["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier)):
        raise setup.SetupError("LibreChat upload source generation changed: " + component)
    source_uid, started_at = source["metadata"]["uid"], now()
    if source["spec"].get("paused"):
        ensure_source_idle(component, identifier)
        for attempt in range(5):
            source = setup.get_object("replicationsource", component + "-backup", NS)
            if (not source or source["metadata"].get("uid") != source_uid or
                    source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                    source["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier)):
                raise setup.SetupError("paused VolSync source changed before its trigger: " + component)
            if source["spec"].get("paused") is not True:
                break
            try:
                guarded_patch("replicationsource", component + "-backup", source,
                              [{"op": "replace", "path": "/spec/paused", "value": False},
                               {"op": "replace", "path": "/spec/trigger", "value": {"manual": identifier}}])
                break
            except setup.SetupError:
                if attempt == 4:
                    raise setup.SetupError("VolSync trigger could not be safely applied: " + component)
                time.sleep(0.2)
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if (not source or source["metadata"].get("uid") != source_uid or
                source["spec"].get("paused") is True or source["spec"].get("trigger") != {"manual": identifier}):
            raise setup.SetupError("VolSync trigger did not become active: " + component)
    deadline, last_message = time.monotonic() + 1800, 0
    while time.monotonic() < deadline:
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if (not source or source["metadata"].get("uid") != source_uid or
                source["spec"].get("trigger") != {"manual": identifier}):
            raise setup.SetupError("LibreChat upload generation changed during synchronization: " + component)
        if upload_complete(source, identifier):
            deadline_quiet = time.monotonic() + 60
            while claim_pods(component + "-backup-staging") or active_workers():
                if time.monotonic() >= deadline_quiet:
                    raise setup.SetupError("completed upload still has a live staging worker: " + component)
                time.sleep(2)
            source = pause_completed_source(component, identifier, source_uid)
            ensure_source_idle(component, identifier)
            save(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"), source)
            print(component + " VolSync upload, retention, and cleanup completed", flush=True)
            return
        status = source.get("status", {})
        if (source["spec"].get("paused") or status.get("latestMoverStatus", {}).get("result") == "Failed" or
                any(item.get("type") in ("Failed", "FailureTarget") and item.get("status") == "True"
                    for item in status.get("conditions", []))):
            stop_failed_upload(component, identifier, source_uid, "VolSync upload failed or was paused")
        if time.monotonic() - last_message >= 60:
            print("waiting for " + component + " VolSync upload and repository cleanup", flush=True)
            last_message = time.monotonic()
        time.sleep(5)
    stop_failed_upload(component, identifier, source_uid,
                       "VolSync upload exceeded thirty minutes; check retained logs before retrying")


def verify_backup(component, identifier):
    checksums = inspect_artifacts(component, identifier)
    if not checksums:
        raise setup.SetupError("cannot verify a repository without published artifacts")
    if active_workers():
        raise setup.SetupError("another Restic worker is active; verifier was not started")
    worker_budget()
    service = setup.get_object("service", "restic", NS)
    if not service:
        raise setup.SetupError("LibreChat Restic service disappeared")
    ips = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    job = json.loads(setup.kube("create", "--dry-run=client", "-f",
                                str(MANIFESTS / "verify-job.yaml"), "-o", "json", capture=True).stdout)
    pod = job["spec"]["template"]["spec"]
    if (job.get("metadata", {}).get("namespace") != NS or
            pod["containers"][0].get("image") != "quay.io/backube/volsync@sha256:0d03a6aad57569eba2c0eaa0848cf4a908d9744b372ff2224bb291a320f36d76" or
            pod.get("nodeSelector") != {"kubernetes.io/hostname": "openmediavault"}):
        raise setup.SetupError("LibreChat backup verification Job template differs")
    container = pod["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": "restic-" + setup.PROTECTED[component]["claim"]}}]
    container["env"] = [{"name": key, "value": value} for key, value in {
        "EXPECTED_RESTIC_IPS": ",".join(str(ipaddress.ip_address(value)) for value in ips),
        "EXPECTED_BACKUP_ID": identifier,
        "EXPECTED_SHA256SUMS": checksums,
        "EXPECTED_ARTIFACTS": json.dumps(sorted(ARTIFACTS[component])),
        "PROTECTED_CLAIM": setup.PROTECTED[component]["claim"],
    }.items()]
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json",
                                     stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    save(setup.RECOVERY / ("initial-" + component + "-verify-job.json"), created)
    print("job/" + name + " created (" + component + " full Restic snapshot verification)", flush=True)
    try:
        wait_job(name)
    except setup.SetupError:
        output = setup.kube("logs", "-n", NS, "job/" + name, "-c", "check", capture=True,
                            sensitive=True, check=False, timeout=20)
        if not output.returncode:
            setup.private_write(setup.RECOVERY / ("initial-" + component + "-verify.log"), output.stdout)
        raise
    logs = setup.kube("logs", "-n", NS, "job/" + name, "-c", "check", capture=True,
                      sensitive=True, timeout=20).stdout
    results = [json.loads(line.removeprefix("BACKUP_VERIFIED_JSON=")) for line in logs.splitlines()
               if line.startswith("BACKUP_VERIFIED_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or
            results[0].get("backup_id") != identifier or
            results[0].get("claim") != setup.PROTECTED[component]["claim"] or
            not re.fullmatch(r"[0-9a-f]{64}", results[0].get("snapshot", ""))):
        raise setup.SetupError("verification Job did not prove this component's uploaded snapshot")
    result = {**results[0], "completed_at": now(), "checksums": checksums, "verification_job": name}
    save(setup.RECOVERY / ("initial-" + component + "-backup-result.json"), result)
    print(component + " snapshot and all staged artifact checksums verified", flush=True)
    return result


def wait_job(name, timeout=1860):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = setup.get_object("job", name, NS)
        if not job:
            raise setup.SetupError("backup Job disappeared: " + name)
        conditions = job.get("status", {}).get("conditions", [])
        if any(item.get("type") == "Complete" and item.get("status") == "True" for item in conditions):
            return
        if any(item.get("type") in ("Failed", "FailureTarget") and item.get("status") == "True" for item in conditions):
            raise setup.SetupError("backup Job failed: " + name)
        if time.monotonic() > deadline - timeout + 60:
            pods = json.loads(setup.kube("get", "pods", "-n", NS,
                                         "-l", "batch.kubernetes.io/job-name=" + name,
                                         "-o", "json", capture=True).stdout)["items"]
            if any(any(item.get("type") == "PodScheduled" and item.get("status") == "False"
                       for item in pod.get("status", {}).get("conditions", [])) for pod in pods):
                raise setup.SetupError("backup Job cannot schedule; current resources and jobs were retained")
        time.sleep(2)
    raise setup.SetupError("backup Job timed out: " + name)


def capture_all(identifier, baseline, configuration):
    existing = {component: inspect_artifacts(component, identifier) for component in COMPONENTS}
    journal = saved(QUIESCENCE) if QUIESCENCE.exists() or QUIESCENCE.is_symlink() else None
    if journal:
        check_journal(journal)
        if journal["backup_id"] != identifier:
            raise setup.SetupError("a different LibreChat writer-capture generation needs manual inspection")
    if all(existing.values()):
        if not journal or not journal["capture_complete"] or journal["phase"] != "resumed":
            raise setup.SetupError("published artifacts lack a complete, recovered shared capture journal")
        return
    if journal and journal.get("capture_complete"):
        if journal["phase"] != "resumed":
            raise setup.SetupError("writer recovery must finish before publishing captured artifacts")
        source_state = journal
        write_artifacts(identifier, source_state["baseline"], source_state,
                        source_state["mongo"], source_state["search"], source_state["copied"])
        for component in COMPONENTS:
            if not inspect_artifacts(component, identifier):
                publish(component, identifier)
        return
    if any(existing.values()):
        raise setup.SetupError("partial published capture is retained; writer state and repositories need inspection")
    if journal and journal["phase"] == "resumed" and not journal.get("capture_complete"):
        raise setup.SetupError("incomplete stopped-writer capture is preserved; inspect retained staging before retrying")
    for component in COMPONENTS:
        ensure_source_idle(component, identifier)
        if any(staging(component).iterdir()):
            raise setup.SetupError("incomplete staging data is preserved; LibreChat remains running: " + component)
    if active_workers():
        raise setup.SetupError("another Restic worker is active; LibreChat remains running")
    scheduling_backlog()
    competing_writers()
    check_units()
    original = controller()
    bindings = {name: {"claim_uid": value["claim_uid"], "volume_uid": value["volume_uid"],
                       "device": value["directory_device"], "inode": value["directory_inode"]}
                for name, value in baseline["bindings"].items()}
    state = {"version": 1, "backup_id": identifier, "phase": "quiescing", "capture_complete": False,
             "replicas": 1, "controller_uid": original["metadata"]["uid"],
             "controller_sha256": controller_hash(original),
             "pod_uids": [baseline["workloads"]["librechat"]["pod_uid"]],
             "bindings": bindings, "baseline": baseline, "configuration": configuration, "started_at": now()}
    pending_dirs = {}
    for component in COMPONENTS:
        directory = pending(component, identifier)
        directory.mkdir(mode=0o700)
        os.chown(directory, 999, 999)
        os.chmod(directory, 0o700)
        sync_directory(directory.parent)
        pending_dirs[component] = directory
    runtime_containers()
    save(QUIESCENCE, state)  # Durable before the first scale operation.
    try:
        print("briefly stopping LibreChat to capture its three file volumes, MongoDB, and Meilisearch together", flush=True)
        set_replicas(state, 0)
        wait_stopped(state)
        dependent_workloads_unchanged(baseline)
        files, mongo, search, copied = {}, None, None, None
        for component in FILE_COMPONENTS:
            files[component] = archive_volume(component, state, pending_dirs[component] / "volume.tar")
            assert_tree_unchanged(component, files[component])
        mongo = capture_mongodb(state, pending_dirs["mongodb"] / "mongodb.archive.gz")
        key = meili_key()
        process, stream, port = start_port_forward()
        try:
            wait_for_meili(process, port, key)
            search = create_meili_dump(state, port, key)
            copied = copy_meili_dump(pending_dirs["meilisearch"], state, search)
        finally:
            stop_port_forward(process, stream)
        dependent_workloads_unchanged(baseline)
        dependencies_unchanged()
        assert_stopped(state)
        state = {**state, "phase": "captured", "capture_complete": True, "captured_at": now(),
                 "artifacts_completed_at": now(), "file_contents": files,
                 "mongo": mongo, "search": search, "copied": copied}
        save(QUIESCENCE, state)
    finally:
        handlers = {number: signal.signal(number, signal.SIG_IGN)
                    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            resume_writer(state)
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
    source_state = saved(QUIESCENCE)
    write_artifacts(identifier, baseline, source_state, mongo, search, copied)
    for component in COMPONENTS:
        publish(component, identifier)


def same_production(before, after):
    if before.get("bindings") != after.get("bindings") or before.get("dependencies") != after.get("dependencies"):
        return False
    for name, original in before["workloads"].items():
        current = after["workloads"].get(name, {})
        stable = ("controller_uid", "spec_sha256", "image_id")
        if any(original.get(key) != current.get(key) for key in stable):
            return False
        if name == "librechat":
            if current.get("restarts") != 0:
                return False
        elif original != current:
            return False
    return True


def backup():
    global PHASE
    PHASE = "checking installed units and recovering an interrupted writer"
    check_units()
    recover_writer()
    PHASE = "checking repository endpoint, application, and protected storage"
    baseline, configuration = preflight()
    if COMPLETE.exists() or COMPLETE.is_symlink():
        proof = saved(COMPLETE)
        if proof.get("result") == "passed" and set(proof.get("components", {})) == set(COMPONENTS):
            print("LibreChat initial backups were already verified: " + proof["backup_id"], flush=True)
            return
        raise setup.SetupError("existing initial backup completion evidence is invalid; preserving it")
    if not STATE.exists() and not STATE.is_symlink():
        save(STATE, {"version": 1, "backup_id": "initial-" + secrets.token_hex(16), "started_at": now()})
    identifier = saved(STATE).get("backup_id")
    if not valid_id(identifier):
        raise setup.SetupError("invalid durable initial LibreChat backup identity")
    PHASE = "checking five paused upload sources"
    apply_sources(identifier)
    for component in COMPONENTS:
        ensure_source_idle(component, identifier)
    PHASE = "capturing protected storage and recovering LibreChat"
    capture_all(identifier, baseline, configuration)
    recovered = setup.live_preflight(check_resources=False)
    if not same_production(baseline, recovered):
        raise setup.SetupError("LibreChat storage, database/search workloads, or configuration changed during capture")
    print("LibreChat is back on its original claims; observing its post-capture health for one minute", flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if setup.live_preflight(check_resources=False, record=False) != recovered:
            raise setup.SetupError("production identities or restart counts changed during the stability observation")
        time.sleep(5)
    results = {}
    for component in COMPONENTS:
        PHASE = "uploading " + component + " to its encrypted Restic repository"
        upload(component, identifier)
        PHASE = "checking all uploaded files and generation for " + component
        results[component] = verify_backup(component, identifier)
    final = setup.live_preflight(check_resources=False)
    if final != recovered:
        raise setup.SetupError("production changed during repository uploads; keep the verified captures")
    proof = {"result": "passed", "namespace": NS, "backup_id": identifier,
             "completed_at": now(), "components": results, "production": final}
    save(COMPLETE, proof)
    print(json.dumps({"result": "passed", "namespace": NS, "backup_id": identifier,
                      "snapshots": {name: value["snapshot"] for name, value in results.items()},
                      "completed_at": proof["completed_at"]}, indent=2), flush=True)
    print("five LibreChat initial backups uploaded and read back; isolated restore tests, daily scheduling, and off-NAS recovery remain pending", flush=True)


def interrupted(number, frame):
    raise setup.SetupError("backup interrupted by signal " + str(number) + "; durable writer recovery remains enabled")


def check_units():
    program = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/backup-librechat.py")
    root = Path(__file__).resolve().parents[2]
    for name in (SERVICE, RECOVERY_SERVICE):
        path = UNIT_DIRECTORY / name
        if not path.exists() or path.is_symlink():
            raise setup.SetupError("installed backup/recovery unit is missing or is a symlink: " + name)
        attrs = path.lstat()
        expected_file = root / "scripts/backups/systemd" / name
        if (not stat.S_ISREG(attrs.st_mode) or attrs.st_uid != 0 or attrs.st_nlink != 1 or
                stat.S_IMODE(attrs.st_mode) & 0o022 or path.read_bytes() != expected_file.read_bytes()):
            raise setup.SetupError("installed backup/recovery unit differs from the reviewed source: " + name)
        output = setup.run(["systemctl", "show", name,
                            "--property=FragmentPath,DropInPaths,ExecStart,ExecStopPost,OnFailure,Restart"],
                           capture=True).stdout
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        starts = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStart", ""))
        stops = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStopPost", ""))
        expected_start = program + (" --recover-writers-only" if name == RECOVERY_SERVICE else "")
        expected_stop = [program + " --recover-writers-only"] if name == SERVICE else []
        if (values.get("FragmentPath") != str(path) or values.get("DropInPaths") or
                os.path.lexists(UNIT_DIRECTORY / (name + ".d")) or
                [value.strip() for value in starts] != [expected_start] or
                [value.strip() for value in stops] != expected_stop or
                values.get("OnFailure", "").split() != ([RECOVERY_SERVICE] if name == SERVICE else []) or
                values.get("Restart") != ("no" if name == SERVICE else "on-failure")):
            raise setup.SetupError("loaded LibreChat backup/recovery unit settings differ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-writers-only", action="store_true")
    arguments = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "linux" or setup.platform.node().split(".")[0] != "openmediavault":
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for command in ("k3s", "findmnt", "systemctl"):
        if not shutil.which(command):
            raise setup.SetupError("required command is missing: " + command)
    if not setup.BASE.is_dir() or not MANIFESTS.is_dir():
        raise setup.SetupError("approved LibreChat paths or backup manifests are missing")
    setup.mounted_on_array(setup.BASE)
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    with ExitStack() as locks:
        locks.enter_context(setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True))
        if arguments.recover_writers_only:
            recover_writer()
        else:
            locks.enter_context(setup.operation_lock(setup.COORDINATION_LOCK, create=False))
            print("LibreChat backup worker serialized with existing namespace backup workers", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print(json.dumps({"result": "stopped", "phase": PHASE, "reason": str(error)}), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(json.dumps({"result": "stopped", "phase": PHASE,
                          "reason": "interrupted; durable recovery and private capture evidence retained"}),
              file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(json.dumps({"result": "stopped", "phase": PHASE, "error_type": type(error).__name__,
                          "reason": "filesystem, Kubernetes, database, or archive validation failed; private details omitted"}),
              file=sys.stderr)
        sys.exit(1)
