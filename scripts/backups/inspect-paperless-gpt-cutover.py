#!/usr/bin/env python3
"""Read GPT cutover prerequisites without pausing or restarting its writer."""

from collections import Counter
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
CAPTURE_ID = "gpt-safety-8b2a2b3960d64d2a91721a0a5fdd7230"
MANIFEST_SHA256 = "53dbf68364b6b9828f8b8c276155fe4953842d64d89066a6158764605ac8b62f"
CGROUP_ROOT = Path("/sys/fs/cgroup")
DATA_ROOT = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4/k3s/data/paperless-ngx")
LIMIT = 4 * 1024**2


class InspectionError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise InspectionError(message)


def is_read_only_command(arguments):
    if arguments[:4] == ["k3s", "kubectl", "--request-timeout=15s", "get"]:
        return len(arguments) > 4 and arguments[4] in ("nodes", "deployment", "pod", "hpa", "pvc", "pv")
    if arguments[:5] == ["k3s", "crictl", "inspect", "-o", "json"]:
        return len(arguments) == 6 and bool(re.fullmatch(r"[0-9a-f]{64}", arguments[5]))
    if arguments[:5] == ["k3s", "ctr", "--namespace", "k8s.io", "tasks"]:
        return arguments[5:] in (["pause", "--help"], ["resume", "--help"], ["list"])
    return arguments in (
        ["findmnt", "--json", "--target", str(CGROUP_ROOT), "--output", "FSTYPE,OPTIONS"],
        ["findmnt", "--json", "--target", str(DATA_ROOT), "--output", "SOURCE,FSTYPE,OPTIONS"],
    )


def read_command(arguments, *, check=True, timeout=45):
    require(is_read_only_command(arguments), "inspection refused a command outside its read-only allowlist")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    require(len(result.stdout) <= LIMIT and len(result.stderr) <= LIMIT,
            "read-only command returned excessive output; output omitted")
    require(not check or result.returncode == 0, "read-only host command failed; output omitted")
    return result


def read_file(path, *, directory=None, limit=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "inspection target is not a regular file")
        data = stream.read(limit + 1)
    require(len(data) <= limit, "inspection file exceeds its read bound")
    return data.decode("utf-8")


def probe_summary(probe):
    if not probe:
        return None
    return {"handler": next((kind for kind in ("tcpSocket", "httpGet", "exec", "grpc") if kind in probe), "unknown"),
            "initial_delay_seconds": probe.get("initialDelaySeconds", 0),
            "period_seconds": probe.get("periodSeconds", 10),
            "timeout_seconds": probe.get("timeoutSeconds", 1),
            "failure_threshold": probe.get("failureThreshold", 3),
            "termination_grace_period_seconds": probe.get("terminationGracePeriodSeconds")}


def captured_ownership(manifest):
    groups = Counter()
    for name, entry in manifest["files"].items():
        component = name.split("/", 1)[0]
        require(component in ("config", "db"), "capture has an unexpected component")
        groups[(component, entry["type"], entry["uid"], entry["gid"], entry["mode"])] += 1
    return [{"component": component, "type": kind, "uid": uid, "gid": gid,
             "mode": oct(mode), "entries": count}
            for (component, kind, uid, gid, mode), count in sorted(groups.items())]


def unified_cgroup(membership, container_id):
    require(re.fullmatch(r"[0-9a-f]{64}", container_id), "container identity is invalid")
    matches = [line[3:] for line in membership.splitlines() if line.startswith("0::")]
    if not matches:
        return None
    require(len(matches) == 1, "process has ambiguous unified cgroup membership")
    path = PurePosixPath(matches[0])
    parts = path.parts
    require(path.is_absolute() and len(parts) > 1 and
            all(part not in ("", ".", "..") and "\x00" not in part for part in parts[1:]) and
            matches[0] == "/" + "/".join(parts[1:]) and container_id in parts[-1],
            "cgroup does not identify the expected container leaf")
    return parts[1:]


def cgroup_summary(pid, container_id):
    parts = unified_cgroup(read_file(Path("/proc") / str(pid) / "cgroup"), container_id)
    if parts is None:
        return {"unified": False}
    filesystem = json.loads(read_command(
        ["findmnt", "--json", "--target", str(CGROUP_ROOT), "--output", "FSTYPE,OPTIONS"]).stdout)["filesystems"]
    require(len(filesystem) == 1 and filesystem[0]["fstype"] == "cgroup2",
            "unified cgroup mount could not be verified")
    with ExitStack() as stack:
        descriptor = os.open(CGROUP_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, descriptor)
        for part in parts:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        processes = set()
        descendants = 0

        def walk(directory, depth=0):
            nonlocal descendants
            require(depth <= 4 and descendants <= 128, "container cgroup tree exceeds inspection bounds")
            values = read_file("cgroup.procs", directory=directory).split()
            require(all(value.isdigit() and int(value) > 1 for value in values),
                    "container cgroup has invalid process metadata")
            processes.update(map(int, values))
            require(len(processes) <= 128, "container has too many processes for inspection")
            names = os.listdir(directory)
            require(len(names) <= 512, "container cgroup has too many entries for inspection")
            for name in names:
                attrs = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(attrs.st_mode):
                    continue
                descendants += 1
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
                try:
                    walk(child, depth + 1)
                finally:
                    os.close(child)

        walk(descriptor)
        require(pid in processes, "verified GPT process is absent from its cgroup")
        events = dict(line.split() for line in read_file("cgroup.events", directory=descriptor).splitlines())
        freeze_available = "cgroup.freeze" in os.listdir(descriptor)
        freeze_value = read_file("cgroup.freeze", directory=descriptor).strip() if freeze_available else None
        freeze_attrs = os.stat("cgroup.freeze", dir_fd=descriptor, follow_symlinks=False) if freeze_available else None
        threads = len(os.listdir(Path("/proc") / str(pid) / "task"))
        return {"unified": True, "freezer_available": freeze_available, "freeze_value": freeze_value,
                "frozen": events.get("frozen"), "populated": events.get("populated"),
                "processes": len(processes), "main_process_threads": threads,
                "descendant_cgroups": descendants,
                "mount_read_only": "ro" in filesystem[0].get("options", "").split(","),
                "freezer_owner": {"uid": freeze_attrs.st_uid, "gid": freeze_attrs.st_gid,
                                  "mode": oct(stat.S_IMODE(freeze_attrs.st_mode))} if freeze_attrs else None}


def runtime_summary(anchor):
    result = {"pause_help_available": False, "resume_help_available": False, "task_status": None}
    for name in ("pause", "resume"):
        command = read_command(["k3s", "ctr", "--namespace", "k8s.io", "tasks", name, "--help"], check=False)
        result[name + "_help_available"] = (command.returncode == 0 and name in command.stdout.lower() and
                                            "no help topic" not in command.stdout.lower())
    command = read_command(["k3s", "ctr", "--namespace", "k8s.io", "tasks", "list"], check=False)
    if command.returncode == 0:
        rows = [line.split() for line in command.stdout.splitlines()]
        rows = [row for row in rows if row and row[0] == anchor["container_id"]]
        if len(rows) == 1 and len(rows[0]) == 3 and rows[0][1] == str(anchor["pid"]):
            require(rows[0][2] in ("RUNNING", "PAUSED", "PAUSING", "STOPPED", "CREATED", "UNKNOWN"),
                    "runtime task returned an unexpected status")
            result["task_status"] = rows[0][2]
    return result


def inspect(capture):
    receipt = capture.private_json(capture.RECOVERY / "result.json")
    require(receipt.get("capture_id") == CAPTURE_ID and receipt.get("manifest_sha256") == MANIFEST_SHA256,
            "saved recovery receipt differs from the verified relayed result")
    manifest = capture.verify_capture(receipt)
    require(capture.BASE.resolve(strict=True) == capture.PHYSICAL == DATA_ROOT,
            "namespace array path mapping changed")
    filesystems = json.loads(read_command(
        ["findmnt", "--json", "--target", str(DATA_ROOT), "--output", "SOURCE,FSTYPE,OPTIONS"]).stdout)["filesystems"]
    require(len(filesystems) == 1 and filesystems[0]["source"] == "/dev/md0" and
            filesystems[0]["fstype"] == "btrfs", "namespace storage is not the expected NAS array")
    space = os.statvfs(DATA_ROOT)
    anchor, deployment, pod = capture.live_source()
    require(anchor == manifest["source"], "GPT identity or deployment changed after the verified capture")
    definition = next(item for item in pod["spec"]["containers"] if item["name"] == "paperless-gpt")
    status = read_file(Path("/proc") / str(anchor["pid"]) / "status")
    credentials = {key: list(map(int, value.split())) for line in status.splitlines()
                   for key, separator, value in [line.partition(":")]
                   if separator and key in ("Uid", "Gid", "Groups")}
    pod_security = pod["spec"].get("securityContext", {})
    container_security = definition.get("securityContext", {})
    result = {
        "safety_capture": {"result": "passed", "capture_id": CAPTURE_ID, "manifest_sha256": MANIFEST_SHA256,
                           "files_verified": receipt["files_verified"], "sqlite_tables": receipt["sqlite_tables"]},
        "captured_ownership": captured_ownership(manifest),
        "array": {"source": "/dev/md0", "fstype": "btrfs", "free_bytes": space.f_bavail * space.f_frsize,
                  "read_only": "ro" in filesystems[0].get("options", "").split(",")},
        "writer": {"pod_uid": anchor["pod_uid"], "restarts": anchor["restarts"],
                   "strategy": deployment["spec"]["strategy"]["type"],
                   "termination_grace_period_seconds": pod["spec"].get("terminationGracePeriodSeconds", 30),
                   "probes": {key: probe_summary(definition.get(key))
                              for key in ("startupProbe", "readinessProbe", "livenessProbe")},
                   "process_credentials": credentials,
                   "pod_security": {key: pod_security.get(key) for key in
                                    ("runAsUser", "runAsGroup", "runAsNonRoot", "fsGroup", "fsGroupChangePolicy")},
                   "container_security": {key: container_security.get(key) for key in
                                          ("runAsUser", "runAsGroup", "runAsNonRoot", "allowPrivilegeEscalation", "readOnlyRootFilesystem")},
                   "resources": definition.get("resources", {})},
        "runtime": runtime_summary(anchor),
        "cgroup": cgroup_summary(anchor["pid"], anchor["container_id"]),
    }
    require(capture.live_source()[0] == anchor, "GPT identity changed during inspection")
    print(json.dumps(result, indent=2))
    print("read-only GPT cutover inspection complete; no writers paused and no storage changed")


def main():
    require(platform.system() == "Linux" and platform.node().split(".")[0] == "openmediavault" and
            os.geteuid() == 0, "run with sudo python3 on OMV")
    source = Path(__file__).with_name("capture-paperless-gpt.py")
    spec = importlib.util.spec_from_file_location("gpt_capture", source)
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    # The capture module's ordinary command wrapper saves failure diagnostics.
    # Replace only that wrapper so this inspection never writes recovery files.
    capture.run = read_command
    try:
        inspect(capture)
    except capture.CaptureError as error:
        raise InspectionError(str(error)) from error


if __name__ == "__main__":
    try:
        main()
    except (InspectionError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError,
            RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, InspectionError) else "read-only prerequisite check failed; private data omitted"
        print("GPT cutover inspection stopped: " + message, file=sys.stderr)
        sys.exit(1)
