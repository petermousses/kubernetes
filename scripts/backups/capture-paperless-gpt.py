#!/usr/bin/env python3
"""Secure GPT's volatile config/SQLite state without restarting its existing Pod."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import select
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.dont_write_bytecode = True
NAMESPACE = "paperless-ngx"
POD = "paperless-gpt-7b768f749d-kz5c4"
POD_UID = "5e4f5b13-f1fb-4bfa-8569-84380f137452"
DEPLOYMENT_UID = "ab83093f-9c69-4784-b04a-566906d17232"
IMAGE = "icereed/paperless-gpt@sha256:15bad5d455b98f21bb7b5d6615f56871ff67a8bb379dc0dd7ba411f4633071a6"
CLAIM_UID = "bebd72c8-7cf3-4150-8c20-c6a2685c6d1b"
PV_UID = "3f100c21-687e-469a-ad10-0949e01d26e2"
BASE = Path("/filesystem/k3s/data/paperless-ngx")
PHYSICAL = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4/k3s/data/paperless-ngx")
CAPTURES = PHYSICAL / "gpt-state-captures"
RECOVERY = Path("/root/k3s-backup-recovery/paperless-ngx/gpt-state-capture")
GLOBAL_LOCK = Path("/root/k3s-backup-recovery/board-games/data-postgres-0/.setup.lock")
INSTALL = Path("/usr/local/lib/k3s-backups/paperless-gpt-capture")
RUNTIME = Path("/run/k3s-paperless-gpt-capture")
UNITS = Path("/etc/systemd/system")
SERVICE = "k3s-paperless-gpt-capture.service"
ROOT = Path(__file__).resolve().parents[2]
FILES = ("scripts/backups/capture-paperless-gpt.py", "scripts/backups/systemd/" + SERVICE)
MAX_DATABASE = 64 * 1024**2
MAX_CONTENT = 8 * 1024**2
MAX_ENTRIES = 128
MAX_JSON = 4 * 1024**2
MARKER = "# managed by nas-backup-migration\n"
WORKER_PHASE = "startup"

class CaptureError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CaptureError(message)


def worker_phase(name):
    global WORKER_PHASE
    WORKER_PHASE = name


def worker_error_record(error):
    """Report the failed operation without paths, SQL, values, or exception text."""
    frames = []
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename == __file__:
            frames.append({"function": traceback.tb_frame.f_code.co_name,
                           "line": traceback.tb_lineno})
        traceback = traceback.tb_next
    return {"phase": WORKER_PHASE, "error_type": type(error).__name__,
            "errno": error.errno if isinstance(error, OSError) else None,
            "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
            "sqlite_errorname": getattr(error, "sqlite_errorname", None),
            "frames": frames[-12:]}


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def managed_directory(path, mode=0o700):
    """For root-controlled paths only; never chown namespace roots or live data."""
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            current.mkdir(mode=mode)
            os.chmod(current, mode)
            fsync_directory(current.parent)
        attrs = current.lstat()
        require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == 0 and
                not stat.S_IMODE(attrs.st_mode) & 0o022, "managed path is not a protected root-owned directory")
        if current.is_relative_to(Path("/root/k3s-backup-recovery")):
            require(stat.S_IMODE(attrs.st_mode) == 0o700, "recovery directories must be private")


def private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        attrs = os.fstat(stream.fileno())
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                stat.S_IMODE(attrs.st_mode) == 0o600 and 0 < attrs.st_size <= MAX_JSON,
                "expected a private regular recovery record")
        data = stream.read(MAX_JSON + 1)
        after = os.fstat(stream.fileno())
        require((attrs.st_size, attrs.st_mtime_ns, attrs.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) and len(data) == attrs.st_size,
                "recovery record changed while reading")
    return json.loads(data)


def save(path, value):
    if os.path.lexists(path):
        private_json(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        attrs = os.fstat(stream.fileno())
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                stat.S_IMODE(attrs.st_mode) == 0o600, "backup lock is not private and root-owned")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CaptureError("another backup operation is running; retry when it finishes") from error
        yield


def run(arguments, *, check=True, timeout=45):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if result.returncode and check:
        save(RECOVERY / "last-command-error.json", {"command": arguments[0], "returncode": result.returncode,
             "stdout": result.stdout[-MAX_JSON:], "stderr": result.stderr[-MAX_JSON:]})
        raise CaptureError("command failed; diagnostics saved privately")
    return result


def get(kind, name=None, *, namespace=NAMESPACE):
    args = ["k3s", "kubectl", "--request-timeout=15s", "get", kind]
    if name:
        args.append(name)
    if namespace:
        args.extend(["-n", namespace])
    return json.loads(run([*args, "-o", "json"]).stdout)


def live_source():
    require([item["metadata"]["name"] for item in get("nodes", namespace=None)["items"]] == ["openmediavault"],
            "expected only OMV during migration")
    deployment, pod = get("deployment", "paperless-gpt"), get("pod", POD)
    require(deployment["metadata"]["uid"] == DEPLOYMENT_UID and deployment["spec"].get("replicas") == 1 and
            deployment["spec"].get("strategy", {}).get("type") == "Recreate" and not deployment["metadata"].get("ownerReferences"),
            "GPT controller identity or strategy changed")
    states = [item for item in pod.get("status", {}).get("containerStatuses", []) if item["name"] == "paperless-gpt"]
    definitions = [item for item in pod["spec"].get("containers", []) if item["name"] == "paperless-gpt"]
    require(pod["metadata"].get("uid") == POD_UID and not pod["metadata"].get("deletionTimestamp") and
            pod["spec"].get("nodeName") == "openmediavault" and pod.get("status", {}).get("phase") == "Running" and
            len(states) == len(definitions) == 1 and definitions[0].get("image") == IMAGE and
            states[0].get("imageID", "").removeprefix("docker.io/") == IMAGE and states[0].get("restartCount") == 0 and
            states[0].get("ready") is True and bool(states[0].get("state", {}).get("running")),
            "inventoried GPT writer is no longer ready; rerun inventory")
    for target in ("/app/config", "/app/db"):
        require(not any(target == mount["mountPath"] or target.startswith(mount["mountPath"].rstrip("/") + "/")
                    for mount in definitions[0].get("volumeMounts", [])), "GPT state already has a covering mount; rerun inventory")
    for item in get("hpa")["items"]:
        require(item.get("spec", {}).get("scaleTargetRef", {}).get("name") != "paperless-gpt", "GPT has an autoscaler")
    claim, volume = get("pvc", "paperless-gpt"), get("pv", "paperless-gpt", namespace=None)
    require(claim["metadata"]["uid"] == CLAIM_UID and volume["metadata"]["uid"] == PV_UID and
            claim.get("status", {}).get("phase") == "Bound" and claim["spec"].get("volumeName") == "paperless-gpt" and
            volume["spec"].get("claimRef", {}).get("uid") == CLAIM_UID and
            volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "GPT prompts binding changed")
    cid = states[0].get("containerID", "").removeprefix("containerd://")
    require(re.fullmatch(r"[0-9a-f]{64}", cid), "GPT container identity is invalid")
    runtime = json.loads(run(["k3s", "crictl", "inspect", "-o", "json", cid]).stdout)
    status, info = runtime.get("status", {}), runtime.get("info", {})
    labels = status.get("labels", {})
    pid = info.get("pid")
    require(status.get("id") == cid and status.get("state") == "CONTAINER_RUNNING" and
            status.get("metadata", {}).get("name") == "paperless-gpt" and
            labels.get("io.kubernetes.pod.uid") == POD_UID and labels.get("io.kubernetes.pod.namespace") == NAMESPACE and
            type(pid) is int and pid > 1, "CRI process identity could not be verified")
    start = process_start(pid)
    anchor = {"pod_uid": POD_UID, "container_id": cid, "image_id": states[0]["imageID"],
              "restarts": states[0]["restartCount"], "pid": pid, "process_start": start,
              "deployment_template": fingerprint(deployment["spec"]["template"])}
    return anchor, deployment, pod


def process_start(pid):
    text = Path("/proc/" + str(pid) + "/stat").read_text()
    fields = text.rpartition(") ")[2].split()
    require(len(fields) > 19 and fields[19].isdigit(), "source process start time is unavailable")
    return fields[19]


def placement():
    require(BASE.resolve(strict=True) == PHYSICAL, "established namespace array mapping changed")
    found = json.loads(run(["findmnt", "--json", "--target", str(PHYSICAL), "--output", "SOURCE,FSTYPE"]).stdout)["filesystems"]
    require(len(found) == 1 and found[0]["source"] == "/dev/md0" and found[0]["fstype"] == "btrfs", "capture destination is not the NAS array")
    require(shutil.disk_usage(PHYSICAL).free >= 1024**3, "less than 1 GiB is available on the array")
    if not os.path.lexists(CAPTURES):
        CAPTURES.mkdir(mode=0o700)
        # The existing namespace parent has setgid; do not retain its 2700 bit.
        os.chmod(CAPTURES, 0o700)
        fsync_directory(CAPTURES)
        fsync_directory(PHYSICAL)
    attrs = CAPTURES.lstat()
    require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == 0 and stat.S_IMODE(attrs.st_mode) == 0o700,
            "capture directory is not private and root-owned")


def private_mount_namespace():
    require(os.stat("/proc/self/ns/mnt").st_ino != os.stat("/proc/1/ns/mnt").st_ino,
            "capture requires the installed service's private mount namespace")
    attrs = RUNTIME.lstat()
    require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == 0 and stat.S_IMODE(attrs.st_mode) == 0o700,
            "service runtime directory is not private")
    propagation = run(["findmnt", "-n", "-o", "PROPAGATION", "-T", str(RUNTIME)]).stdout.strip()
    # PrivateMounts first detaches the host peer group via MS_SLAVE. systemd's
    # default MS_SHARED step can then produce a separate shared,slave group.
    # https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#PrivateMounts=
    flags = propagation.split(",")
    require(len(flags) == len(set(flags)) and set(flags) in ({"private"}, {"slave"}, {"shared", "slave"}),
            "mount propagation is not isolated from the host")


def find_database(root_descriptor):
    """Find one SQLite file through no-follow directory descriptors."""
    root_attrs = os.fstat(root_descriptor)
    require(stat.S_ISDIR(root_attrs.st_mode) and
            (fcntl.fcntl(root_descriptor, fcntl.F_GETFL) & os.O_ACCMODE) == os.O_RDONLY,
            "database root descriptor is not read-only")
    candidates = []
    entry_count = 0

    def walk(descriptor, relative, depth=0):
        nonlocal entry_count
        require(depth <= 8, "database tree exceeds the bounded capture depth")
        attrs = os.fstat(descriptor)
        require(stat.S_ISDIR(attrs.st_mode) and attrs.st_dev == root_attrs.st_dev and not os.listxattr(descriptor),
                "database tree has a nested filesystem or unsupported attributes")
        for name in sorted(os.listdir(descriptor)):
            entry_count += 1
            require(entry_count <= MAX_ENTRIES, "database tree exceeds the bounded capture size")
            require("/" not in name and name not in (".", ".."), "database entry name is unsafe")
            child_relative = relative + "/" + name if relative else name
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), "database directory changed during inspection")
                    walk(child, child_relative, depth + 1)
                finally:
                    os.close(child)
                continue
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= MAX_DATABASE,
                    "database tree contains a link, special file, or oversized file")
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and
                        (opened.st_dev, opened.st_ino, opened.st_size) == (before.st_dev, before.st_ino, before.st_size) and
                        opened.st_dev == root_attrs.st_dev and not os.listxattr(child),
                        "database file changed or uses unsupported attributes")
                if opened.st_size >= 16 and os.read(child, 16) == b"SQLite format 3\x00":
                    candidates.append((child_relative, os.dup(descriptor), name, metadata(opened)))
            finally:
                os.close(child)

    try:
        walk(root_descriptor, "")
        require(len(candidates) == 1, "expected exactly one native SQLite database")
        return candidates[0]
    except BaseException:
        for _, parent, _, _ in candidates:
            os.close(parent)
        raise


def metadata(attrs):
    return {"uid": attrs.st_uid, "gid": attrs.st_gid, "mode": stat.S_IMODE(attrs.st_mode),
            "mtime_ns": attrs.st_mtime_ns, "ctime_ns": attrs.st_ctime_ns, "inode": attrs.st_ino, "device": attrs.st_dev}


def collect(roots, database=None):
    entries, contents = {}, {}
    size = 0
    owned_roots = []
    descriptors = {}

    def add_entry(name, entry):
        require(name not in entries and len(entries) < MAX_ENTRIES,
                "source tree exceeds the bounded capture size")
        entries[name] = entry

    def walk(descriptor, relative, device, depth=0):
        nonlocal size
        require(depth <= 8 and len(entries) < MAX_ENTRIES, "source tree exceeds the bounded capture size")
        attrs = os.fstat(descriptor)
        require(stat.S_ISDIR(attrs.st_mode) and attrs.st_dev == device and not os.listxattr(descriptor),
                "source has a nested filesystem or unsupported attributes")
        entry = metadata(attrs)
        if stat.S_ISDIR(attrs.st_mode):
            entry["type"] = "directory"
            add_entry(relative, entry)
            for name in sorted(os.listdir(descriptor)):
                require("/" not in name and name not in (".", ".."), "source entry name is unsafe")
                child_relative = relative + "/" + name
                if database and child_relative in {database + suffix for suffix in ("-wal", "-shm", "-journal")}:
                    continue
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
                    try:
                        opened = os.fstat(child)
                        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), "source directory changed during inspection")
                        walk(child, child_relative, device, depth + 1)
                    finally:
                        os.close(child)
                    continue
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                        "source has a link or unsupported file type")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and
                            (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino) and opened.st_dev == device and
                            not os.listxattr(child), "source file changed or uses unsupported attributes")
                    entry = metadata(opened)
                    entry["type"] = "file"
                    if child_relative == database:
                        require(0 < opened.st_size <= MAX_DATABASE and os.read(child, 16) == b"SQLite format 3\x00",
                                "source database is not a bounded SQLite database")
                        entry["native_sqlite"] = True
                    else:
                        require(metadata(before) == metadata(opened), "configuration changed before reading")
                        require(opened.st_size <= MAX_CONTENT - size, "configuration exceeds the bounded capture size")
                        chunks, remaining = [], MAX_CONTENT - size + 1
                        while remaining:
                            block = os.read(child, min(1024 * 1024, remaining))
                            if not block:
                                break
                            chunks.append(block)
                            remaining -= len(block)
                        data = b"".join(chunks)
                        after = os.fstat(child)
                        require(len(data) == opened.st_size and metadata(opened) == metadata(after),
                                "configuration changed while reading")
                        size += len(data)
                        contents[child_relative] = data
                        entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                finally:
                    os.close(child)
                add_entry(child_relative, entry)
        else:
            raise CaptureError("capture root is not a directory")
    try:
        for name, root in roots.items():
            if isinstance(root, int):
                descriptors[name] = root
            else:
                descriptors[name] = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                owned_roots.append(descriptors[name])
        for name, descriptor in descriptors.items():
            attrs = os.fstat(descriptor)
            require(stat.S_ISDIR(attrs.st_mode) and
                    (fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE) == os.O_RDONLY,
                    "source root descriptor is not read-only")
            walk(descriptor, name, attrs.st_dev)
        return entries, contents
    finally:
        for descriptor in owned_roots:
            os.close(descriptor)


def stable_configuration(entries):
    result = {}
    for name, entry in entries.items():
        result[name] = {key: value for key, value in entry.items() if key not in (
            {"mtime_ns", "ctime_ns"} if entry["type"] == "directory" or entry.get("native_sqlite") else set())}
    return result


def counts(connection):
    names = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")]
    require(len(names) <= 256, "database has too many tables for this capture")
    return {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}


def landlock_database_read_ban_writes(readable_paths, write_probe_path):
    worker_phase("landlock-query-abi")
    require(platform.machine() in ("x86_64", "aarch64"), "unsupported CPU architecture for filesystem sandbox")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.argtypes = [ctypes.c_long]
    libc.syscall.restype = ctypes.c_long

    def syscall(number, *arguments):
        result = libc.syscall(number, *arguments)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    # 444-446 are the generic Linux Landlock syscalls on x86_64 and aarch64.
    version = syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    require(version >= 3, "Landlock ABI cannot deny file truncation")
    # Handle reads too: allow /proc and /dev traversal, the pinned SQLite tree,
    # and only /dev/urandom file contents. All other files stay unreadable.
    handled = sum(1 << bit for bit in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12))
    if version >= 2:
        handled |= 1 << 13  # LANDLOCK_ACCESS_FS_REFER
    handled |= 1 << 14  # LANDLOCK_ACCESS_FS_TRUNCATE
    if version >= 5:
        handled |= 1 << 15  # LANDLOCK_ACCESS_FS_IOCTL_DEV

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64),
                    ("handled_access_net", ctypes.c_uint64),
                    ("scoped", ctypes.c_uint64),
                    ("quiet_access_fs", ctypes.c_uint64),
                    ("quiet_access_net", ctypes.c_uint64),
                    ("quiet_scoped", ctypes.c_uint64)]

    class PathBeneathAttr(ctypes.Structure):
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    require(ctypes.sizeof(PathBeneathAttr) == 16,
            "unexpected Landlock path-rule structure layout")

    ruleset = RulesetAttr(handled, 0, 0, 0, 0, 0)
    ruleset_size = 48 if version >= 10 else 24 if version >= 6 else 16 if version >= 4 else 8
    worker_phase("landlock-create-ruleset")
    ruleset_fd = syscall(444, ctypes.byref(ruleset), ctypes.c_size_t(ruleset_size), ctypes.c_uint(0))
    try:
        for index, (descriptor, allowed_access) in enumerate(readable_paths):
            worker_phase("landlock-add-rule-" + str(index))
            path_rule = PathBeneathAttr(allowed_access, descriptor)
            syscall(445, ctypes.c_int(ruleset_fd), ctypes.c_int(1),
                    ctypes.byref(path_rule), ctypes.c_uint(0))  # LANDLOCK_RULE_PATH_BENEATH
        # sandbox_worker set no_new_privs before installing seccomp.
        require(threading.active_count() == 1, "unexpected helper threads before filesystem sandbox")
        worker_phase("landlock-restrict-self")
        syscall(446, ctypes.c_int(ruleset_fd), ctypes.c_uint(0))  # LANDLOCK_RESTRICT_SELF
    finally:
        os.close(ruleset_fd)

    worker_phase("database-write-denial")
    try:
        descriptor = os.open(write_probe_path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        require(error.errno in (1, 13), "Landlock write-denial probe failed unexpectedly")
    else:
        os.close(descriptor)
        raise CaptureError("Landlock allowed a writable SQLite descriptor")
    worker_phase("outside-read-denial")
    try:
        descriptor = os.open("/etc/passwd", os.O_RDONLY | os.O_CLOEXEC)
    except OSError as error:
        require(error.errno in (1, 13), "Landlock read-confinement probe failed unexpectedly")
    else:
        os.close(descriptor)
        raise CaptureError("Landlock allowed a read outside the SQLite directory")
    return version


def open_landlock_read_paths(database_directory):
    require(hasattr(os, "O_PATH"), "Linux O_PATH support is required for the filesystem sandbox")
    flags = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
    descriptors = []
    try:
        proc_fd = os.open("/proc", flags)
        descriptors.append(proc_fd)
        database_fd = os.open(database_directory, flags | os.O_NOFOLLOW)
        descriptors.append(database_fd)
        dev_fd = os.open("/dev", flags)
        descriptors.append(dev_fd)
        random_fd = os.open("/dev/urandom", os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        descriptors.append(random_fd)
        require(stat.S_ISDIR(os.fstat(proc_fd).st_mode) and
                stat.S_ISDIR(os.fstat(database_fd).st_mode) and
                stat.S_ISDIR(os.fstat(dev_fd).st_mode) and
                stat.S_ISCHR(os.fstat(random_fd).st_mode),
                "Landlock read allowlist paths have unexpected types")
        return [(proc_fd, 1 << 3), (database_fd, (1 << 2) | (1 << 3)),
                (dev_fd, 1 << 3), (random_fd, 1 << 2)], descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def sandbox_worker(readable_paths, write_probe_path):
    """Drop capabilities, filter dangerous syscalls, and confine Landlock reads/writes."""
    worker_phase("drop-capabilities")
    require(platform.machine() in ("x86_64", "aarch64"), "unsupported CPU architecture for filesystem sandbox")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int

    def prctl(option, argument):
        if libc.prctl(option, argument, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    require(threading.active_count() == 1, "unexpected helper threads before filesystem sandbox")
    prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    prctl(28, 3)  # PR_SET_SECUREBITS: SECBIT_NOROOT | SECBIT_NOROOT_LOCKED
    cap_last = int(Path("/proc/sys/kernel/cap_last_cap").read_text().strip())
    for capability in range(cap_last + 1):
        prctl(24, capability)  # PR_CAPBSET_DROP

    class CapabilityHeader(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class CapabilityData(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                    ("inheritable", ctypes.c_uint32)]

    header = CapabilityHeader(0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3
    data = (CapabilityData * 2)()
    syscall_number = {"x86_64": 126, "aarch64": 91}[platform.machine()]
    libc.syscall.argtypes = [ctypes.c_long]
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(syscall_number, ctypes.byref(header), data) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "could not clear worker capabilities")
    status = Path("/proc/self/status").read_text()
    capability_values = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                         for line in status.splitlines() if line.startswith(("CapEff:", "CapPrm:", "CapInh:", "CapAmb:"))}
    require(set(capability_values) == {"CapEff", "CapPrm", "CapInh", "CapAmb"} and
            all(int(value, 16) == 0 for value in capability_values.values()),
            "worker still has Linux capabilities")

    worker_phase("load-seccomp")
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as error:
        raise CaptureError("libseccomp is required for the SQLite worker sandbox") from error
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.restype = None
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    require(bool(context), "could not initialize the worker syscall filter")
    denied = set()
    syscall_names = (
        "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat",
        "utime", "utimes", "futimesat", "utimensat", "setxattr", "lsetxattr", "fsetxattr",
        "removexattr", "lremovexattr", "fremovexattr", "mount", "umount2", "pivot_root",
        "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr",
        "unshare", "setns", "open_by_handle_at", "name_to_handle_at", "ptrace", "process_vm_readv",
        "process_vm_writev", "pidfd_getfd", "prctl", "capset", "setuid", "setgid", "setresuid",
        "setresgid", "setfsuid", "setfsgid", "setgroups",
        "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto",
        "sendmsg", "sendmmsg", "execve", "execveat", "fork", "vfork", "clone", "clone3",
        "io_uring_setup", "io_uring_enter", "io_uring_register", "userfaultfd", "bpf", "ioctl",
        "kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo", "pidfd_send_signal",
    )
    try:
        for name in syscall_names:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                continue
            result = seccomp.seccomp_rule_add(context, 0x00050000 | 1, number, 0)  # SCMP_ACT_ERRNO(EPERM)
            require(result == 0, "could not deny a metadata or mount syscall")
            denied.add(name)
        require({"fchmodat", "fchownat", "utimensat", "setxattr", "mount", "umount2", "socket",
                 "connect", "execve", "clone", "process_vm_readv", "prctl", "capset", "ioctl"} <= denied,
                "libseccomp could not resolve required worker restrictions")
        require(seccomp.seccomp_load(context) == 0, "could not load the worker syscall filter")
    finally:
        seccomp.seccomp_release(context)

    return landlock_database_read_ban_writes(readable_paths, write_probe_path)


def sqlite_sandbox_probe(database_path, parent_pid, expected_identity):
    worker_phase("probe-pin-source")
    database_path = Path(database_path)
    require(database_path.is_absolute() and database_path.is_relative_to(RUNTIME) and
            database_path.name == "probe.db" and not any(part in (".", "..") for part in database_path.parts),
            "synthetic SQLite probe path is outside the private service runtime")
    descriptor = os.open(database_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        attrs = os.fstat(descriptor)
        require(stat.S_ISREG(attrs.st_mode) and (attrs.st_dev, attrs.st_ino) ==
                (expected_identity["device"], expected_identity["inode"]),
                "synthetic WAL database identity changed")
    finally:
        os.close(descriptor)
    proc_root = Path("/proc") / str(parent_pid) / "root"
    database_path_in_parent = proc_root / database_path.relative_to("/")
    readable_paths, rule_descriptors = open_landlock_read_paths(database_path_in_parent.parent)
    try:
        expected_parent = database_path.parent.stat()
        pinned_parent = os.fstat(rule_descriptors[1])
        require((expected_parent.st_dev, expected_parent.st_ino) == (pinned_parent.st_dev, pinned_parent.st_ino),
                "synthetic SQLite directory identity changed")
        version = sandbox_worker(readable_paths, database_path_in_parent)
    finally:
        for descriptor in reversed(rule_descriptors):
            os.close(descriptor)
    worker_phase("probe-open-sqlite")
    database_uri = database_path_in_parent.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True, timeout=1, isolation_level=None)) as source:
        worker_phase("probe-read-wal")
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA trusted_schema=OFF")
        source.execute("PRAGMA temp_store=MEMORY")
        require(source.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and
                source.execute("SELECT value FROM probe").fetchone() == ("committed-in-wal",),
                "read-only WAL probe did not recover the committed synthetic row")
        memory = sqlite3.connect(":memory:", isolation_level=None)
        try:
            worker_phase("probe-backup-sqlite")
            source.backup(memory)
            worker_phase("probe-validate-sqlite")
            require(memory.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and
                    memory.execute("SELECT value FROM probe").fetchone() == ("committed-in-wal",),
                    "synthetic WAL online backup failed validation")
        finally:
            memory.close()
    print("synthetic read-only WAL snapshot passed with allowlisted reads and filesystem writes denied", flush=True)


def synthetic_sqlite_sandbox_check():
    directory = Path(tempfile.mkdtemp(prefix="landlock-probe-", dir=RUNTIME))
    writer = None
    try:
        database_path = directory / "probe.db"
        writer = sqlite3.connect(database_path, isolation_level=None)
        require(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal",
                "could not prepare a synthetic WAL database")
        writer.execute("CREATE TABLE probe(value TEXT NOT NULL)")
        writer.execute("INSERT INTO probe VALUES ('committed-in-wal')")
        require(all((directory / ("probe.db" + suffix)).is_file() for suffix in ("-wal", "-shm")),
                "synthetic WAL sidecars were not created")
        source_paths = [database_path, directory / "probe.db-wal", directory / "probe.db-shm"]
        before = []
        for path in source_paths:
            attrs = path.stat()
            before.append((attrs.st_dev, attrs.st_ino, attrs.st_size, attrs.st_mtime_ns, attrs.st_ctime_ns))
        attrs = database_path.stat()
        command = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                   "--sqlite-probe", str(database_path), str(os.getpid()),
                   str(attrs.st_dev), str(attrs.st_ino)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                         "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"})
        except subprocess.TimeoutExpired as error:
            save(RECOVERY / "sqlite-probe-error.json", {"reason": "synthetic WAL probe timed out"})
            raise CaptureError("synthetic WAL/write-denial probe timed out; private diagnostics saved") from error
        if result.returncode:
            save(RECOVERY / "sqlite-probe-error.json", {
                "returncode": result.returncode, "stdout": result.stdout[-4096:],
                "stderr": result.stderr[-8192:]})
            # This helper sees only the synthetic fixture. Emit its bounded,
            # structured diagnostics now so a failed probe needs no second SSH run.
            print(result.stderr[-8192:].strip(), file=sys.stderr, flush=True)
            raise CaptureError("synthetic WAL/write-denial probe failed; private diagnostics saved")
        require(result.stdout.strip() ==
                "synthetic read-only WAL snapshot passed with allowlisted reads and filesystem writes denied",
                "synthetic WAL/write-denial probe returned unexpected output")
        after = [(path.stat().st_dev, path.stat().st_ino, path.stat().st_size,
                  path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in source_paths]
        require(before == after,
                "synthetic read-only WAL snapshot changed source-file metadata")
        print("synthetic read-only WAL snapshot, outside-read denial, and database write-denial checks passed before live GPT inspection", flush=True)
    finally:
        if writer is not None:
            writer.close()
        shutil.rmtree(directory)


def regular_descriptors():
    result = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            descriptor = int(name)
            attrs = os.fstat(descriptor)
        except OSError:
            continue
        if stat.S_ISREG(attrs.st_mode):
            result[descriptor] = (attrs.st_dev, attrs.st_ino)
    return result


def sqlite_snapshot_worker(pid, process_started, database_relative, expected_identity):
    worker_phase("snapshot-pin-source")
    parts = PurePosixPath(database_relative).parts
    require(parts and str(PurePosixPath(database_relative)) == database_relative and
            all(part not in ("", ".", "..") and "/" not in part for part in parts),
            "database path is unsafe")
    descriptors = []
    pidfd = os.pidfd_open(pid)
    try:
        require(process_start(pid) == process_started and not select.select([pidfd], [], [], 0)[0],
                "source process changed before SQLite snapshot")
        rootfd = os.open("/proc/" + str(pid) + "/root", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(rootfd)
        appfd = os.open("app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd)
        descriptors.append(appfd)
        dbfd = os.open("db", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=appfd)
        descriptors.append(dbfd)
        parentfd = dbfd
        for part in parts[:-1]:
            parentfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parentfd)
            descriptors.append(parentfd)
        sourcefd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parentfd)
        descriptors.append(sourcefd)
        attrs = os.fstat(sourcefd)
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_nlink == 1 and attrs.st_size <= MAX_DATABASE and
                (attrs.st_dev, attrs.st_ino) == (expected_identity["device"], expected_identity["inode"]) and
                os.pread(sourcefd, 16, 0) == b"SQLite format 3\x00",
                "container database identity differs from the pinned source")

        uri_path = Path("/proc") / str(pid) / "root" / "app" / "db" / Path(*parts)
        readable_paths, rule_descriptors = open_landlock_read_paths(uri_path.parent)
        try:
            expected_parent = os.fstat(parentfd)
            pinned_parent = os.fstat(rule_descriptors[1])
            require((expected_parent.st_dev, expected_parent.st_ino) ==
                    (pinned_parent.st_dev, pinned_parent.st_ino),
                    "container database directory changed before sandboxing")
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            descriptors.clear()
            version = sandbox_worker(readable_paths, uri_path)
        finally:
            for descriptor in reversed(rule_descriptors):
                os.close(descriptor)
        worker_phase("snapshot-list-descriptors")
        before = regular_descriptors()
        uri = uri_path.as_uri() + "?mode=ro"
        deadline = time.monotonic() + 20
        memory = sqlite3.connect(":memory:", isolation_level=None)
        try:
            def progress(status, remaining, total):
                if time.monotonic() >= deadline or total * page_size > MAX_DATABASE:
                    raise RuntimeError("native SQLite snapshot exceeded its time or size budget")

            memory.execute("PRAGMA temp_store=MEMORY")
            worker_phase("snapshot-open-sqlite")
            with closing(sqlite3.connect(uri, uri=True, timeout=1, isolation_level=None)) as source:
                worker_phase("snapshot-read-sqlite")
                source.execute("PRAGMA query_only=ON")
                source.execute("PRAGMA trusted_schema=OFF")
                source.execute("PRAGMA temp_store=MEMORY")
                source.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                source.execute("BEGIN")
                page_size = source.execute("PRAGMA page_size").fetchone()[0]
                page_count = source.execute("PRAGMA page_count").fetchone()[0]
                require(page_size in {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536} and
                        page_size * page_count <= MAX_DATABASE,
                        "source SQLite page geometry exceeds the capture budget")
                identities = regular_descriptors()
                opened_database = [identity for descriptor, identity in identities.items()
                                   if before.get(descriptor) != identity and
                                   identity == (expected_identity["device"], expected_identity["inode"])]
                require(opened_database, "SQLite did not open the pinned source database")
                journal = source.execute("PRAGMA journal_mode").fetchone()[0]
                memory.execute("PRAGMA page_size=" + str(page_size))
                worker_phase("snapshot-backup-sqlite")
                source.backup(memory, pages=128, progress=progress, sleep=0.01)
                expected_counts = counts(source)
                source.rollback()
            worker_phase("snapshot-validate-sqlite")
            require(memory.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and
                    counts(memory) == expected_counts,
                    "native SQLite memory snapshot failed integrity or row-count verification")
            require(hasattr(memory, "serialize"), "host SQLite cannot serialize the bounded snapshot")
            payload = memory.serialize()
            require(0 < len(payload) <= MAX_DATABASE, "serialized SQLite snapshot exceeds the capture budget")
        finally:
            memory.close()
        require(not select.select([pidfd], [], [], 0)[0],
                "source process exited during SQLite snapshot")
        header = json.dumps({"version": 1, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                             "journal_mode": journal, "relation_counts": expected_counts,
                             "device": expected_identity["device"], "inode": expected_identity["inode"],
                             "landlock_abi": version}, sort_keys=True, separators=(",", ":")).encode()
        require(len(header) <= MAX_JSON, "snapshot metadata exceeds its bound")
        return struct.pack("!I", len(header)) + header + payload
    finally:
        os.close(pidfd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def online_snapshot_in_worker(database_relative, expected_identity, anchor):
    command = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
               "--sqlite-snapshot", str(anchor["pid"]), anchor["process_start"], database_relative,
               str(expected_identity["device"]), str(expected_identity["inode"])]
    try:
        result = subprocess.run(command, capture_output=True, text=False, timeout=45,
                                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                     "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"})
    except subprocess.TimeoutExpired as error:
        save(RECOVERY / "sqlite-worker-error.json", {"reason": "isolated snapshot timed out"})
        raise CaptureError("isolated SQLite snapshot timed out; private diagnostics saved") from error
    if result.returncode:
        save(RECOVERY / "sqlite-worker-error.json", {
            "returncode": result.returncode,
            "stdout_bytes": len(result.stdout),
            "stderr": result.stderr[-8192:].decode("utf-8", "replace")})
        raise CaptureError("isolated SQLite snapshot failed; private diagnostics saved")
    require(4 <= len(result.stdout) <= MAX_DATABASE + MAX_JSON + 4,
            "isolated SQLite snapshot exceeds its transport bound")
    header_size = struct.unpack("!I", result.stdout[:4])[0]
    require(0 < header_size <= MAX_JSON and 4 + header_size <= len(result.stdout),
            "isolated SQLite snapshot header is invalid")
    header = json.loads(result.stdout[4:4 + header_size])
    payload = result.stdout[4 + header_size:]
    expected_fields = {"version", "bytes", "sha256", "journal_mode", "relation_counts", "device", "inode", "landlock_abi"}
    require(isinstance(header, dict) and set(header) == expected_fields and header.get("version") == 1 and
            type(header.get("bytes")) is int and header["bytes"] == len(payload) and
            0 < len(payload) <= MAX_DATABASE and header.get("device") == expected_identity["device"] and
            header.get("inode") == expected_identity["inode"] and type(header.get("landlock_abi")) is int and
            header["landlock_abi"] >= 3 and header.get("journal_mode") in {"delete", "truncate", "persist", "memory", "wal", "off"} and
            isinstance(header.get("relation_counts"), dict) and len(header["relation_counts"]) <= 256 and
            all(isinstance(name, str) and type(value) is int and value >= 0
                for name, value in header["relation_counts"].items()) and
            hashlib.sha256(payload).hexdigest() == header.get("sha256"),
            "isolated SQLite snapshot metadata or digest is invalid")
    memory = sqlite3.connect(":memory:", isolation_level=None)
    try:
        require(hasattr(memory, "deserialize"), "host SQLite cannot validate the transported snapshot")
        memory.deserialize(payload)
        require(memory.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and
                counts(memory) == header["relation_counts"],
                "transported SQLite snapshot failed integrity or row-count verification")
        return memory, header["relation_counts"], header["journal_mode"]
    except BaseException:
        memory.close()
        raise


def write_file(path, data, entry):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fchown(stream.fileno(), entry["uid"], entry["gid"])
        os.fchmod(stream.fileno(), entry["mode"])
        os.fsync(stream.fileno())
    os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]), follow_symlinks=False)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def snapshot_files(directory):
    result = {}
    for index, path in enumerate(sorted(directory.rglob("*")), 1):
        require(index <= MAX_ENTRIES + 1, "capture contains too many entries")
        relative = path.relative_to(directory).as_posix()
        if relative == "capture-manifest.json":
            continue
        attrs = path.lstat()
        require(not path.is_symlink() and not os.listxattr(path, follow_symlinks=False), "capture contains a link or unsupported attributes")
        entry = {"uid": attrs.st_uid, "gid": attrs.st_gid, "mode": stat.S_IMODE(attrs.st_mode), "mtime_ns": attrs.st_mtime_ns}
        if stat.S_ISDIR(attrs.st_mode):
            entry["type"] = "directory"
        else:
            require(stat.S_ISREG(attrs.st_mode) and attrs.st_nlink == 1 and attrs.st_size <= MAX_DATABASE,
                    "capture contains an unsupported file")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    digest.update(chunk)
            entry.update(type="file", bytes=attrs.st_size, sha256=digest.hexdigest())
        result[relative] = entry
    return result


def verify_capture(result):
    require(result.get("result") == "passed" and re.fullmatch(r"gpt-safety-[0-9a-f]{32}", result.get("capture_id", "")), "capture receipt is incomplete")
    directory = CAPTURES / result["capture_id"]
    attrs = directory.lstat()
    require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == 0 and stat.S_IMODE(attrs.st_mode) == 0o700, "capture directory is not private")
    manifest = private_json(directory / "capture-manifest.json")
    anchor = manifest.get("source", {})
    require(fingerprint(manifest) == result.get("manifest_sha256") and manifest.get("capture_id") == result["capture_id"] and
            manifest.get("method") == "SQLite online backup" and manifest.get("source_unchanged") is True and
            manifest.get("configuration_stable") is True and anchor.get("pod_uid") == POD_UID and
            isinstance(anchor.get("image_id"), str) and anchor["image_id"].removeprefix("docker.io/") == IMAGE and
            snapshot_files(directory) == manifest.get("files"), "capture manifest or saved file hashes differ")
    name = manifest.get("database")
    require(isinstance(name, str) and len(PurePosixPath(name).parts) >= 2 and PurePosixPath(name).parts[0] == "db" and
            not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts and name in manifest["files"], "captured database path is invalid")
    with closing(sqlite3.connect((directory / name).as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
        database.execute("PRAGMA query_only=ON")
        database.execute("PRAGMA trusted_schema=OFF")
        require(database.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and counts(database) == manifest.get("relation_counts"), "saved SQLite snapshot failed isolated verification")
    require(type(result.get("files_verified")) is int and type(result.get("sqlite_tables")) is int and
            result.get("files_verified") == sum(item["type"] == "file" for item in manifest["files"].values()) and
            result.get("sqlite_tables") == len(manifest["relation_counts"]) and result.get("namespace") == NAMESPACE and
            result.get("component") == "paperless-gpt" and result.get("sqlite_integrity") == "passed" and
            result.get("source_unchanged") is True and
            result.get("capture_directory") == str(BASE / "gpt-state-captures" / result["capture_id"]),
            "public capture evidence differs from private evidence")
    return manifest


def verify_source_paths(anchor, pidfd, rootfd, appfd, source_fds):
    require(not select.select([pidfd], [], [], 0)[0] and process_start(anchor["pid"]) == anchor["process_start"],
            "source process changed")
    for current, descriptor in ((os.stat("/proc/" + str(anchor["pid"]) + "/root"), rootfd),
                                (os.stat("app", dir_fd=rootfd, follow_symlinks=False), appfd)):
        pinned = os.fstat(descriptor)
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino),
                "source root or application directory was replaced")
    for name, descriptor in source_fds.items():
        current, pinned = os.stat(name, dir_fd=appfd, follow_symlinks=False), os.fstat(descriptor)
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino),
                "source state directory was replaced")


def capture():
    if os.path.lexists(RECOVERY / "result.json"):
        placement()
        result = private_json(RECOVERY / "result.json")
        verify_capture(result)
        print(json.dumps(result, indent=2))
        print("existing recovery snapshot reverified; final PVC cutover still requires a fresh quiesced capture")
        return
    private_mount_namespace()
    synthetic_sqlite_sandbox_check()
    placement()
    anchor, deployment, pod = live_source()
    save(RECOVERY / "deployment.before.json", deployment)
    save(RECOVERY / "pod.before.json", pod)
    capture_id = "gpt-safety-" + uuid.uuid4().hex
    state = {"capture_id": capture_id, "phase": "reading", "started_at": now(), "source": anchor}
    if os.path.lexists(RECOVERY / "state.json"):
        previous = private_json(RECOVERY / "state.json")
        old_id = previous.get("capture_id", "")
        require(re.fullmatch(r"gpt-safety-[0-9a-f]{32}", old_id), "prior capture record is invalid")
        save(RECOVERY / (old_id + ".attempt.json"), previous)
    save(RECOVERY / "state.json", state)
    with ExitStack() as stack:
        pidfd = os.pidfd_open(anchor["pid"])
        stack.callback(os.close, pidfd)
        require(not select.select([pidfd], [], [], 0)[0] and process_start(anchor["pid"]) == anchor["process_start"], "source process changed")
        rootfd = os.open("/proc/" + str(anchor["pid"]) + "/root", os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, rootfd)
        appfd = os.open("app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        stack.callback(os.close, appfd)
        source_fds = {}
        for name in ("config", "db"):
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=appfd)
            stack.callback(os.close, descriptor)
            require(os.fstat(descriptor).st_dev == os.fstat(rootfd).st_dev, "source directory is on an unexpected mount")
            source_fds[name] = descriptor
        database_relative, database_parent_fd, database_basename, database_identity = find_database(source_fds["db"])
        stack.callback(os.close, database_parent_fd)
        database_name = "db/" + database_relative
        before, contents = collect(source_fds, database_name)
        source_database = before.get(database_name)
        require(source_database is not None and
                (source_database["device"], source_database["inode"]) ==
                (database_identity["device"], database_identity["inode"]),
                "SQLite file changed between discovery and capture")
        require(any(name.startswith("config/") for name in contents), "no configuration files were found")
        print("pinned read-only source descriptors verified; capturing SQLite in an isolated worker with dropped capabilities, syscall restrictions, and a kernel-enforced filesystem write ban", flush=True)
        memory, relations, journal = online_snapshot_in_worker(
            database_relative, {"device": source_database["device"], "inode": source_database["inode"]}, anchor)
        stack.callback(memory.close)
        after, _ = collect(source_fds, database_name)
        require(stable_configuration(before) == stable_configuration(after), "configuration changed during the snapshot; no capture accepted")
        verify_source_paths(anchor, pidfd, rootfd, appfd, source_fds)
        require(live_source()[0] == anchor, "production writer changed during the snapshot")
        state["phase"] = "persisting"
        save(RECOVERY / "state.json", state)
        directory = CAPTURES / capture_id
        directory.mkdir(mode=0o700)
        fsync_directory(CAPTURES)
        for name, entry in sorted(before.items(), key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
            path = directory / name
            if entry["type"] == "directory":
                path.mkdir(mode=0o700)
            elif name != database_name:
                write_file(path, contents[name], entry)
        target = directory / database_name
        require(not os.path.lexists(target), "capture database target already exists")
        with closing(sqlite3.connect(target)) as output:
            memory.backup(output)
            output.execute("PRAGMA journal_mode=DELETE")
            require(output.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and counts(output) == relations,
                    "array SQLite snapshot failed isolated verification")
        attrs = before[database_name]
        os.chown(target, attrs["uid"], attrs["gid"])
        os.chmod(target, attrs["mode"])
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        for name, entry in sorted(before.items(), key=lambda item: len(PurePosixPath(item[0]).parts), reverse=True):
            path = directory / name
            if entry["type"] == "directory":
                os.chown(path, entry["uid"], entry["gid"])
                os.chmod(path, entry["mode"])
                os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]), follow_symlinks=False)
                fsync_directory(path)
        verify_source_paths(anchor, pidfd, rootfd, appfd, source_fds)
        require(live_source()[0] == anchor, "production writer changed while the snapshot was saved")
        manifest = {"version": 1, "capture_id": capture_id, "method": "SQLite online backup", "captured_at": now(),
                    "source": anchor, "source_unchanged": True, "configuration_stable": True,
                    "database": database_name, "source_journal_mode": journal, "relation_counts": relations,
                    "files": snapshot_files(directory)}
        save(directory / "capture-manifest.json", manifest)
        result = {"result": "passed", "namespace": NAMESPACE, "component": "paperless-gpt", "capture_id": capture_id,
                  "completed_at": now(), "manifest_sha256": fingerprint(manifest),
                  "files_verified": sum(item["type"] == "file" for item in manifest["files"].values()),
                  "sqlite_tables": len(relations), "sqlite_integrity": "passed", "source_unchanged": True,
                  "capture_directory": str(BASE / "gpt-state-captures" / capture_id)}
        verify_capture(result)
        save(RECOVERY / "result.json", result)
        state["phase"] = "complete"
        save(RECOVERY / "state.json", state)
    print(json.dumps(result, indent=2), flush=True)
    print("GPT recovery snapshot verified on the array; the original Pod was not paused, restarted, or reconfigured")
    print("persistent storage cutover, namespace backups/restores, and scheduling remain pending")


def unit_properties():
    output = run(["systemctl", "show", SERVICE, "--property=FragmentPath,DropInPaths,ExecStart,ActiveState,PrivateMounts,RuntimeDirectory"], check=False).stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def rename_no_replace(source, destination):
    library = ctypes.CDLL(None, use_errno=True)
    method = getattr(library, "renameat2", None)
    require(method is not None, "Linux renameat2 is required for immutable runtime publication")
    method.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    method.restype = ctypes.c_int
    require(method(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0,
            "runtime publication stopped; existing files retained")


def install_runtime(revision, contents):
    destination = INSTALL / revision
    if os.path.lexists(destination):
        managed_directory(destination, 0o755)
        for name, content in contents.items():
            target = destination / name
            managed_directory(target.parent, 0o755)
            attrs = target.lstat()
            require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                    stat.S_IMODE(attrs.st_mode) == 0o644 and target.read_bytes() == content, "installed immutable runtime differs")
        return
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=INSTALL))
    try:
        for name, content in contents.items():
            target = temporary / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
        for path in sorted((path for path in temporary.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            os.chmod(path, 0o755)
            fsync_directory(path)
        os.chmod(temporary, 0o755)
        fsync_directory(temporary)
        rename_no_replace(temporary, destination)
        fsync_directory(INSTALL)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def install(revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "supply the immutable downloaded revision")
    managed_directory(INSTALL, 0o755)
    props = unit_properties()
    require(props.get("ActiveState", "") in ("", "inactive", "failed") and not props.get("DropInPaths") and
            props.get("FragmentPath", "") in ("", str(UNITS / SERVICE)) and not os.path.lexists(UNITS / (SERVICE + ".d")),
            "an active or unmanaged unit overrides capture")
    contents = {}
    for name in FILES:
        source = ROOT / name
        require(source.is_file() and not source.is_symlink() and source.stat().st_nlink == 1, "downloaded runtime file is unsafe")
        contents[name] = source.read_bytes()
    pointer = INSTALL / "current"
    if os.path.lexists(pointer):
        require(pointer.is_symlink() and pointer.lstat().st_uid == 0 and re.fullmatch(r"[0-9a-f]{40}", os.readlink(pointer)), "unmanaged runtime pointer exists")
    unit = UNITS / SERVICE
    content = contents[FILES[1]]
    require(content.startswith(MARKER.encode()), "downloaded unit lacks its management marker")
    if os.path.lexists(unit):
        attrs = unit.lstat()
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                not stat.S_IMODE(attrs.st_mode) & 0o022 and unit.read_bytes().startswith(MARKER.encode()), "refusing to replace unmanaged unit")
    install_runtime(revision, contents)
    run(["systemd-analyze", "verify", str(INSTALL / revision / FILES[1])])
    pending = INSTALL / (".current-" + uuid.uuid4().hex)
    try:
        os.symlink(revision, pending)
        os.replace(pending, pointer)
        fsync_directory(INSTALL)
    finally:
        if os.path.lexists(pending):
            os.unlink(pending)
    descriptor, temporary = tempfile.mkstemp(prefix=".paperless-capture-", dir=UNITS)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        os.fchmod(stream.fileno(), 0o644)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, unit)
    fsync_directory(UNITS)
    run(["systemd-analyze", "verify", str(unit)])
    run(["systemctl", "daemon-reload"])
    props = unit_properties()
    expected = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/capture-paperless-gpt.py") + " --run"
    commands = re.findall(r"\bargv\[\]=([^;{}]+)", props.get("ExecStart", ""))
    require([value.strip() for value in commands] == [expected] and props.get("PrivateMounts") == "yes" and
            props.get("RuntimeDirectory") == "k3s-paperless-gpt-capture" and not props.get("DropInPaths"), "loaded capture service differs from the reviewed unit")
    print("running a native online GPT recovery capture under systemd; the existing Pod keeps running", flush=True)
    started = run(["systemctl", "start", SERVICE], check=False, timeout=360)
    print(run(["journalctl", "-u", SERVICE, "-n", "100", "--no-pager", "-o", "cat"]).stdout, end="", flush=True)
    require(started.returncode == 0, "capture service did not complete; original GPT state is untouched")
    verify_capture(private_json(RECOVERY / "result.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--check-sandbox", action="store_true",
                       help="check only synthetic SQLite state; never inspect the live GPT Pod")
    modes.add_argument("--sqlite-snapshot", nargs=5,
                       metavar=("PID", "PROCESS_START", "DATABASE", "DEVICE", "INODE"),
                       help=argparse.SUPPRESS)
    modes.add_argument("--sqlite-probe", nargs=4,
                       metavar=("DATABASE", "PARENT_PID", "DEVICE", "INODE"),
                       help=argparse.SUPPRESS)
    parser.add_argument("--revision")
    args = parser.parse_args()
    require(platform.system() == "Linux" and platform.node().split(".")[0] == "openmediavault" and os.geteuid() == 0,
            "run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if args.sqlite_snapshot:
        pid, process_started, database_relative, device, inode = args.sqlite_snapshot
        payload = sqlite_snapshot_worker(int(pid), process_started, database_relative,
                                         {"device": int(device), "inode": int(inode)})
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return
    if args.sqlite_probe:
        database_path, parent_pid, device, inode = args.sqlite_probe
        sqlite_sandbox_probe(database_path, int(parent_pid),
                             {"device": int(device), "inode": int(inode)})
        return
    for tool in ("k3s", "findmnt", "systemctl", "systemd-analyze"):
        require(shutil.which(tool), "required host tool is missing")
    managed_directory(RECOVERY)
    with ExitStack() as stack:
        stack.enter_context(lock(RECOVERY / (".install.lock" if args.install else ".capture.lock")))
        if args.install:
            install(args.revision or "")
        else:
            stack.enter_context(lock(GLOBAL_LOCK))
            stack.enter_context(lock(RECOVERY.parent / ".setup.lock"))
            if args.check_sandbox:
                private_mount_namespace()
                synthetic_sqlite_sandbox_check()
                print("synthetic sandbox check passed; no live GPT files were opened", flush=True)
            else:
                capture()


if __name__ == "__main__":
    try:
        main()
    except (CaptureError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError,
            RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, KeyboardInterrupt):
        error = sys.exc_info()[1]
        if "--sqlite-probe" in sys.argv[1:] or "--sqlite-snapshot" in sys.argv[1:]:
            print("SQLITE_WORKER_ERROR_JSON=" + json.dumps(worker_error_record(error)),
                  file=sys.stderr, flush=True)
        message = str(error) if isinstance(error, CaptureError) else "filesystem, API, runtime, or SQLite check failed; private data omitted"
        print("GPT capture stopped: " + message, file=sys.stderr, flush=True)
        sys.exit(1)
