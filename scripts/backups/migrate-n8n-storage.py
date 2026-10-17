#!/usr/bin/env python3
"""Copy stopped n8n data into its own retained claim, with durable recovery."""

import argparse
from contextlib import ExitStack
import copy
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
NAMESPACE = "n8n"
BASE = Path("/filesystem/k3s/data/n8n")
SOURCE = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4/k3s/data/n8n")
IMAGES = SOURCE.parent / "n8n-images"
TARGET = SOURCE / "live-data"
CLAIM = "n8n-data-live"
IMAGE = "docker.n8n.io/n8nio/n8n@sha256:3989d9b8ebb77b4ee8f604519eb73e44f4384bfaa689526e0104eed79a237d30"
IDENTITIES = {
    "n8n-data": ("4ed03cb1-cab1-4b1c-a480-55eb60fad35b", "69077cdb-a8eb-428d-8b9e-a9b321b97d8f", BASE),
    "n8n-images": ("b2c9d7d3-e36e-42a1-b13d-496047359fdc", "4ebde73d-a642-4129-a469-501b483cea29", BASE.parent / "n8n-images"),
}
RECOVERY = Path("/root/k3s-backup-recovery/n8n/storage-migration")
GLOBAL_LOCK = Path("/root/k3s-backup-recovery/board-games/data-postgres-0/.setup.lock")
INSTALL = Path("/usr/local/lib/k3s-backups/n8n-storage")
SERVICE = "k3s-n8n-storage-migration.service"
RECOVERY_SERVICE = "k3s-n8n-storage-recovery.service"
UNIT_DIRECTORY = Path("/etc/systemd/system")
ROOT = Path(__file__).resolve().parents[2]
FILES = ("scripts/backups/migrate-n8n-storage.py", "scripts/backups/inventory-n8n.py",
         "scripts/backups/systemd/" + SERVICE, "scripts/backups/systemd/" + RECOVERY_SERVICE,
         "apps/n8n/persistent-volume-data-live.yaml",
         "apps/n8n/persistent-volume-claim-data-live.yaml")


class MigrationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def run(arguments, *, stdin=None, timeout=40, check=True):
    result = subprocess.run(arguments, input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        save("last-command-error.json", {"command": arguments[0], "returncode": result.returncode,
                                         "stdout": result.stdout[-1024**2:], "stderr": result.stderr[-1024**2:]})
    require(not check or result.returncode == 0, "command failed: " + arguments[0])
    return result


def kube(*arguments, stdin=None, timeout=40):
    return run(["k3s", "kubectl", "--request-timeout=20s", *arguments], stdin=stdin, timeout=timeout).stdout


def get(kind, name=None, namespace=NAMESPACE):
    arguments = ["get", kind]
    if name is not None:
        arguments.extend([name, "--ignore-not-found=true"])
    if namespace is not None:
        arguments.extend(["-n", namespace])
    output = kube(*arguments, "-o", "json")
    return json.loads(output) if output.strip() else None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rename_no_replace(source, destination):
    # Linux renameat2 atomically refuses even an empty foreign destination.
    library = ctypes.CDLL(None, use_errno=True)
    method = getattr(library, "renameat2", None)
    require(method is not None, "Linux renameat2 is required for publication without replacement")
    method.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    method.restype = ctypes.c_int
    if method(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise MigrationError("atomic publication failed; existing destination and original data preserved")


def root_directory(path, mode):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            current.mkdir(mode=mode)
            os.chmod(current, mode)
            sync_directory(current.parent)
        attributes = current.lstat()
        require(stat.S_ISDIR(attributes.st_mode) and attributes.st_uid == 0 and
                not stat.S_IMODE(attributes.st_mode) & 0o022,
                "managed directory is not a root-owned directory protected from writes")
        if current.is_relative_to(Path("/root/k3s-backup-recovery")):
            require(stat.S_IMODE(attributes.st_mode) == 0o700, "recovery directory must have mode 0700")


def load(name):
    descriptor = os.open(RECOVERY / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as stream:
        attributes = os.fstat(stream.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_uid == 0 and attributes.st_nlink == 1 and
                stat.S_IMODE(attributes.st_mode) == 0o600 and attributes.st_size <= 32 * 1024**2,
                "saved migration state is not a private regular root-owned file")
        return json.load(stream)


def save(name, value):
    if os.path.lexists(RECOVERY / name):
        load(name)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=RECOVERY)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RECOVERY / name)
        sync_directory(RECOVERY)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    stream = os.fdopen(descriptor, "r+")
    attributes = os.fstat(stream.fileno())
    require(stat.S_ISREG(attributes.st_mode) and attributes.st_uid == 0 and attributes.st_nlink == 1 and
            stat.S_IMODE(attributes.st_mode) == 0o600, "migration lock is not private and root-owned")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise MigrationError("another operation holds a migration or backup worker lock")
    return stream


def bindings():
    result = {}
    for name, (claim_uid, volume_uid, path) in IDENTITIES.items():
        claim, volume = get("pvc", name), get("pv", name, namespace=None)
        require(claim and volume and claim["metadata"]["uid"] == claim_uid and volume["metadata"]["uid"] == volume_uid and
                claim.get("status", {}).get("phase") == "Bound" and claim["spec"].get("volumeName") == name and
                volume["spec"].get("claimRef", {}).get("uid") == claim_uid and
                volume["spec"].get("local", {}).get("path") == str(path) and
                volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain",
                "an original n8n storage binding changed; stopped without replacing it")
        result[name] = {"claim": claim, "volume": volume}
    if os.path.lexists(RECOVERY / "bindings.before.json"):
        original = load("bindings.before.json")
        require(all(result[name][kind]["spec"] == original[name][kind]["spec"]
                    for name in IDENTITIES for kind in ("claim", "volume")), "an original storage specification changed")
    return result


def placement():
    require(BASE.resolve(strict=True) == SOURCE and (BASE.parent / "n8n-images").resolve(strict=True) == IMAGES,
            "the established namespace or images mapping changed")
    for path in (SOURCE, IMAGES):
        attributes = path.lstat()
        require(stat.S_ISDIR(attributes.st_mode) and attributes.st_uid == 1000 and attributes.st_gid == 1000,
                "source directory type or ownership changed")
        mount = json.loads(run(["findmnt", "--json", "--target", str(path),
                              "--output", "SOURCE,FSTYPE"]).stdout)["filesystems"]
        require(len(mount) == 1 and mount[0]["source"] == "/dev/md0" and mount[0]["fstype"] == "btrfs",
                "n8n storage is not on the approved Btrfs RAID array")
    mounts = json.loads(run(["findmnt", "--json", "--list", "--output", "TARGET"]).stdout)["filesystems"]
    require(not any(Path(item["target"]) != source and Path(item["target"]).is_relative_to(source)
                    for item in mounts for source in (SOURCE, IMAGES)), "a source contains a nested mount")


def references(pod, names):
    return any(volume.get("persistentVolumeClaim", {}).get("claimName") in names
               for volume in pod["spec"].get("volumes", []))


def active_pods():
    return [pod for pod in get("pods")["items"] if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]


def assert_stopped(state):
    require(not any(references(pod, set(IDENTITIES) | {CLAIM}) for pod in active_pods()), "a storage-using n8n Pod still exists")
    containers = json.loads(run(["k3s", "crictl", "ps", "-o", "json"]).stdout)["containers"]
    require(not any(container.get("labels", {}).get("io.kubernetes.pod.uid") in state["old_pod_uids"] or
                    (container.get("labels", {}).get("io.kubernetes.pod.namespace") == NAMESPACE and
                     container.get("metadata", {}).get("name") == "n8n") for container in containers),
            "an original n8n runtime container is still running")


def wait_stopped(state):
    deadline = time.monotonic() + 240
    while True:
        try:
            assert_stopped(state)
            return
        except MigrationError:
            require(time.monotonic() < deadline, "n8n writers did not stop; no data copied")
            time.sleep(2)


def checked_deployment(state):
    deployment = get("deployment", "n8n")
    require(deployment and deployment["metadata"]["uid"] == state["deployment_uid"], "n8n Deployment identity changed")
    original = load("deployment.before.json")
    actual = {key: value for key, value in deployment["spec"].items() if key not in ("replicas", "template")}
    expected = {key: value for key, value in original["spec"].items() if key not in ("replicas", "template")}
    require(actual == expected and deployment["spec"].get("replicas") in (0, 1) and
            deployment["spec"]["template"] in (state["original_template"], state["target_template"]),
            "n8n controller settings drifted; no automatic override is safe")
    return deployment


def patch_deployment(state, *, replicas, target):
    deployment = checked_deployment(state)
    template = state["target_template"] if target else state["original_template"]
    require(not (state["authoritative"] == "target" and not target), "refusing to resume stale original data")
    operations = [{"op": "test", "path": "/metadata/uid", "value": state["deployment_uid"]},
                  {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
                  {"op": "test", "path": "/spec/template", "value": deployment["spec"]["template"]}]
    if deployment["spec"]["template"] != template:
        require(deployment["spec"]["replicas"] == 0 and replicas == 0, "claim changes require a stopped Deployment")
        operations.append({"op": "replace", "path": "/spec/template", "value": template})
    operations.append({"op": "replace", "path": "/spec/replicas", "value": replicas})
    kube("patch", "deployment", "n8n", "-n", NAMESPACE, "--type=json", "-p", json.dumps(operations))


def stable_attributes(attributes):
    return (attributes.st_ino, attributes.st_dev, attributes.st_size, attributes.st_mtime_ns, attributes.st_ctime_ns)


def digest_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "expected a regular file")
        checksum = hashlib.sha256()
        for block in iter(lambda: stream.read(1024**2), b""):
            checksum.update(block)
        require(stable_attributes(before) == stable_attributes(os.fstat(stream.fileno())) == stable_attributes(path.lstat()),
                "a supposedly stopped source file changed")
    return checksum.hexdigest()


def tree_manifest(root, *, ignore_live=False, synchronize=False):
    entries = []
    device = root.lstat().st_dev

    def visit(path, relative):
        before = path.lstat()
        require(before.st_dev == device, "copy contains a nested filesystem")
        entry = {"path": relative, "uid": before.st_uid, "gid": before.st_gid,
                 "mode": stat.S_IMODE(before.st_mode),
                 "xattrs": {name: os.getxattr(path, name, follow_symlinks=False).hex()
                            for name in sorted(os.listxattr(path, follow_symlinks=False))}}
        if relative != ".":
            entry["mtime_ns"] = before.st_mtime_ns
        if stat.S_ISDIR(before.st_mode):
            names = sorted(item.name for item in path.iterdir() if not (ignore_live and relative == "." and item.name == "live-data"))
            entry["type"] = "directory"
            entries.append(entry)
            for name in names:
                visit(path / name, name if relative == "." else relative + "/" + name)
            require(stable_attributes(before) == stable_attributes(path.lstat()), "a stopped source directory changed")
            if synchronize:
                sync_directory(path)
        elif stat.S_ISREG(before.st_mode):
            require(before.st_nlink == 1, "hard-linked files require inspection before migration")
            entry.update({"type": "file", "bytes": before.st_size, "sha256": digest_file(path)})
            entries.append(entry)
            if synchronize:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        elif stat.S_ISLNK(before.st_mode):
            entry.update({"type": "symlink", "target": os.readlink(path)})
            entries.append(entry)
        else:
            raise MigrationError("special files require inspection before migration")
        require(stable_attributes(before) == stable_attributes(path.lstat()), "source metadata changed during capture")

    visit(root, ".")
    return entries


def validate_sqlite(copy_root, scratch):
    scratch.mkdir(mode=0o700)
    for name in ("database.sqlite", "database.sqlite-wal", "database.sqlite-shm"):
        path = copy_root / name
        if os.path.lexists(path):
            require(stat.S_ISREG(path.lstat().st_mode), "SQLite files must be regular files")
            run(["cp", "--reflink=auto", "--preserve=all", "--", str(path), str(scratch / name)])
    require((scratch / "database.sqlite").is_file(), "the copied SQLite database is missing")
    config = copy_root / "config"
    require(stat.S_ISREG(config.lstat().st_mode) and config.stat().st_size <= 65536 and
            stat.S_IMODE(config.stat().st_mode) == 0o600, "n8n encryption configuration must be a private regular file")
    with config.open("r") as stream:
        value = json.load(stream)
    require(isinstance(value.get("encryptionKey"), str) and bool(value["encryptionKey"]), "the persisted encryption key is missing")
    connection = sqlite3.connect(scratch / "database.sqlite")
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "copied SQLite integrity check failed")
        require(not connection.execute("PRAGMA foreign_key_check").fetchmany(1), "copied SQLite foreign key check failed")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        require({"workflow_entity", "credentials_entity"}.issubset(tables), "n8n application tables are missing")
        counts = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
    finally:
        connection.close()
    return {"integrity_check": "passed", "foreign_key_check": "passed", "relation_counts": counts,
            "encryption_config_sha256": digest_file(config)}


def target_resources():
    resources = []
    for filename in FILES[-2:]:
        resources.append(json.loads(kube("create", "--dry-run=client", "-f", str(ROOT / filename), "-o", "json")))
    pv, pvc = resources
    require(pv.get("kind") == "PersistentVolume" and pv["metadata"]["name"] == CLAIM and
            pv["spec"].get("local") == {"path": str(BASE / "live-data")} and
            pv["spec"].get("persistentVolumeReclaimPolicy") == "Retain" and
            pv["spec"].get("accessModes") == ["ReadWriteOnce"] and
            pv["spec"].get("storageClassName") == "n8n-local" and
            pv["spec"].get("capacity") == {"storage": "10Gi"} and pv["spec"].get("volumeMode") == "Filesystem" and
            pv["spec"].get("nodeAffinity") == {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}} and
            pv["spec"].get("claimRef") == {"name": CLAIM, "namespace": NAMESPACE} and
            pvc.get("kind") == "PersistentVolumeClaim" and pvc["metadata"]["name"] == CLAIM and
            pvc["metadata"].get("namespace") == NAMESPACE and pvc["spec"].get("volumeName") == CLAIM and
            pvc["spec"].get("storageClassName") == "n8n-local" and pvc["spec"].get("accessModes") == ["ReadWriteOnce"] and
            pvc["spec"].get("resources") == {"requests": {"storage": "10Gi"}},
            "downloaded replacement storage manifests differ from the approved placement")
    return resources


def check_target_binding(state):
    claim, volume = get("pvc", CLAIM), get("pv", CLAIM, namespace=None)
    require(claim and volume and claim["metadata"]["uid"] == state["target_claim_uid"] and
            volume["metadata"]["uid"] == state["target_volume_uid"] and
            claim.get("status", {}).get("phase") == "Bound" and claim["spec"].get("volumeName") == CLAIM and
            volume["spec"].get("claimRef", {}).get("uid") == claim["metadata"]["uid"] and
            volume["spec"].get("local") == {"path": str(BASE / "live-data")} and
            volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "replacement storage binding changed")
    expected_pv, expected_pvc = state["resources"]
    require(all(volume["spec"].get(key) == value for key, value in expected_pv["spec"].items() if key != "claimRef") and
            all(claim["spec"].get(key) == value for key, value in expected_pvc["spec"].items()),
            "replacement access modes, placement, size, or class changed")


def preflight():
    placement()
    nodes = get("nodes", namespace=None)["items"]
    require([node["metadata"]["name"] for node in nodes] == ["openmediavault"] and not nodes[0]["spec"].get("unschedulable"),
            "expected a single schedulable OMV node")
    require(not get("hpa")["items"], "n8n autoscaling must be resolved before quiescence")
    require(not get("statefulsets,daemonsets,cronjobs")["items"], "additional n8n controllers require inspection")
    cluster_pods = json.loads(kube("get", "pods", "-A", "-o", "json"))["items"]
    require(not any(pod.get("status", {}).get("phase") == "Pending" and not pod["metadata"].get("deletionTimestamp")
                    for pod in cluster_pods), "pending Pods may take n8n's released reservations")
    require(not os.path.lexists(TARGET) and not get("pvc", CLAIM) and not get("pv", CLAIM, namespace=None),
            "replacement directory or storage already exists; preserve it for inspection")
    original_bindings = bindings()
    for volume in get("pv", namespace=None)["items"]:
        path = (volume["spec"].get("local") or volume["spec"].get("hostPath") or {}).get("path")
        if path and volume["metadata"]["name"] not in IDENTITIES:
            resolved = Path(path).resolve()
            require(not any(resolved.is_relative_to(source) or source.is_relative_to(resolved) for source in (SOURCE, IMAGES)),
                    "another PV aliases n8n's source storage")
    for pod in cluster_pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for volume in pod["spec"].get("volumes", []):
            path = volume.get("hostPath", {}).get("path")
            if path and any(mount.get("name") == volume["name"] and not mount.get("readOnly", False)
                            for group in ("containers", "initContainers", "ephemeralContainers")
                            for container in pod["spec"].get(group, []) for mount in container.get("volumeMounts", [])):
                resolved = Path(path).resolve()
                require(not any(resolved.is_relative_to(source) or source.is_relative_to(resolved) for source in (SOURCE, IMAGES)),
                        "another workload has writable host access to n8n data")
    deployment = get("deployment", "n8n")
    require(deployment and not deployment["metadata"].get("ownerReferences") and
            deployment["spec"].get("replicas") == 1 and deployment["spec"].get("strategy") == {"type": "Recreate"},
            "expected the original one-replica Recreate Deployment")
    spec = deployment["spec"]["template"]["spec"]
    require(len(spec.get("containers", [])) == 1 and not spec.get("initContainers") and not spec.get("ephemeralContainers"),
            "additional application containers require inspection")
    container = spec["containers"][0]
    require(container["name"] == "n8n" and container["image"] == IMAGE and not container.get("command") and
            not container.get("args") and not container.get("env") and
            container.get("envFrom") == [{"configMapRef": {"name": "n8n-config"}}] and
            spec.get("securityContext", {}).get("runAsUser") == 1000 and spec["securityContext"].get("fsGroup") == 1000 and
            spec.get("volumes") == [{"name": "n8n-data", "persistentVolumeClaim": {"claimName": "n8n-data"}},
                                    {"name": "n8n-images", "persistentVolumeClaim": {"claimName": "n8n-images"}}] and
            container.get("volumeMounts") == [{"name": "n8n-data", "mountPath": "/home/node/.n8n"},
                                              {"name": "n8n-images", "mountPath": "/home/node/.n8n-files"}],
            "the live n8n template differs from the inventoried storage/backend setup")
    require(spec.get("nodeSelector") in (None, {"kubernetes.io/hostname": "openmediavault"}) and
            not spec.get("affinity") and not spec.get("schedulingGates"), "unexpected n8n scheduling overrides")
    pods = active_pods()
    writers = [pod for pod in pods if references(pod, set(IDENTITIES))]
    require(len(writers) == 1 and writers[0]["spec"].get("nodeName") == "openmediavault" and
            any(condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in writers[0].get("status", {}).get("conditions", [])), "expected one ready original writer")
    configuration = get("configmap", "n8n-config")
    require(configuration is not None, "n8n configuration is missing")
    inspector_spec = importlib.util.spec_from_file_location("n8n_inventory", ROOT / "scripts/backups/inventory-n8n.py")
    inspector = importlib.util.module_from_spec(inspector_spec)
    inspector_spec.loader.exec_module(inspector)
    runtime = inspector.runtime_summary(pods)
    require(runtime.get("n8n_version") == "2.33.7" and runtime.get("database_type_environment") == "unset" and
            runtime.get("execution_mode_environment") == "unset" and runtime.get("binary_mode_environment") == "unset" and
            runtime.get("database_type_config") == "unset" and runtime.get("encryption_key_in_config") is True and
            runtime.get("data_directory_matches_expected_mount") is True and
            not runtime.get("encryption_key_environment_present") and not runtime.get("encryption_key_file_environment_present") and
            not any(runtime.get("overrides_present", {"unknown": True}).values()), "database or encryption-key source changed")
    for name in ("backups", "backup-staging", "backup-cache", "backup-staging-history", "restore-validation"):
        require(not os.path.lexists(SOURCE / name), "backup-layout directories already exist inside the live source")
    used = int(run(["du", "-sx", "--block-size=1", "--", str(SOURCE)], timeout=90).stdout.split()[0])
    require(shutil.disk_usage(SOURCE).free >= max(used * 3, 1024**3), "insufficient array space for migration and validation")
    resources = target_resources()
    target_template = copy.deepcopy(deployment["spec"]["template"])
    target_template["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = CLAIM
    target_template["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "openmediavault"}
    save("deployment.before.json", deployment)
    save("bindings.before.json", original_bindings)
    save("configuration.before.json", configuration)
    state = {"version": 1, "phase": "prepared", "authoritative": "source", "created_at": now(),
             "deployment_uid": deployment["metadata"]["uid"], "old_pod_uids": [pod["metadata"]["uid"] for pod in writers],
             "original_template": deployment["spec"]["template"], "target_template": target_template,
             "configuration_hash": fingerprint(configuration.get("data", {})), "resources": resources,
             "temporary": str(SOURCE.parent / (".n8n-storage-migration-" + uuid.uuid4().hex))}
    save("state.json", state)
    return state


def check_configuration(state):
    configuration = get("configmap", "n8n-config")
    require(configuration and configuration["metadata"]["uid"] == load("configuration.before.json")["metadata"]["uid"] and
            fingerprint(configuration.get("data", {})) == state["configuration_hash"], "n8n configuration changed during migration")


def copy_and_publish(state):
    assert_stopped(state)
    placement()
    temporary = Path(state["temporary"])
    require(temporary.parent == SOURCE.parent and re.fullmatch(r"\.n8n-storage-migration-[0-9a-f]{32}", temporary.name) and
            not os.path.lexists(temporary) and not os.path.lexists(TARGET), "migration destination is already present")
    temporary.mkdir(mode=0o700)
    os.chmod(temporary, 0o700)
    sync_directory(temporary.parent)
    state["phase"] = "copying"
    save("state.json", state)
    original = tree_manifest(SOURCE)
    save("source-files.json", original)
    run(["cp", "-a", "--reflink=auto", "--one-file-system", "--", str(SOURCE), str(temporary / "data")], timeout=300)
    copied = temporary / "data"
    require(tree_manifest(copied, synchronize=True) == original == tree_manifest(SOURCE), "copied file data or metadata differs")
    for entry in original:
        if entry["type"] == "file":
            before, after = (SOURCE / entry["path"]).stat(), (copied / entry["path"]).stat()
            require((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino), "copied file is hard-linked to original data")
    proof = validate_sqlite(copied, temporary / "sqlite-validation")
    save("copy-validation.json", proof)
    require(tree_manifest(copied) == original == tree_manifest(SOURCE), "data changed during SQLite validation")
    assert_stopped(state)
    # The namespace is quiesced and locked; never replace an existing live directory.
    require(not os.path.lexists(TARGET), "the live destination appeared during copying")
    rename_no_replace(copied, TARGET)
    sync_directory(SOURCE)
    sync_directory(temporary)
    state["phase"] = "published"
    state["target_device"] = TARGET.stat().st_dev
    state["target_inode"] = TARGET.stat().st_ino
    save("state.json", state)
    print("stopped source copied; all file checksums/metadata, SQLite integrity, and encryption configuration verified", flush=True)


def create_binding(state):
    for resource in state["resources"]:
        kube("create", "-f", "-", stdin=json.dumps(resource))
    deadline = time.monotonic() + 90
    while True:
        claim, volume = get("pvc", CLAIM), get("pv", CLAIM, namespace=None)
        require(claim and volume, "replacement binding creation was incomplete")
        if claim.get("status", {}).get("phase") == "Bound":
            state.update({"target_claim_uid": claim["metadata"]["uid"], "target_volume_uid": volume["metadata"]["uid"]})
            save("state.json", state)
            check_target_binding(state)
            return
        require(time.monotonic() < deadline, "replacement claim did not bind")
        time.sleep(2)


def ready_writer(state, *, target):
    name = CLAIM if target else "n8n-data"
    deployment = checked_deployment(state)
    require(deployment["spec"]["replicas"] == 1 and deployment["spec"]["template"] ==
            (state["target_template"] if target else state["original_template"]), "n8n controller is not on the authoritative data")
    pods = [pod for pod in active_pods() if references(pod, set(IDENTITIES) | {CLAIM})]
    require(len(pods) == 1 and references(pods[0], {name}) and
            pods[0]["spec"].get("nodeName") == "openmediavault" and not pods[0]["metadata"].get("deletionTimestamp"),
            "expected one authoritative n8n writer")
    pod = pods[0]
    require(any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", [])),
            "the authoritative n8n writer is not ready")
    main = next((item for item in pod.get("status", {}).get("containerStatuses", []) if item["name"] == "n8n"), {})
    require(main.get("ready") and main.get("imageID") == IMAGE, "n8n image or container readiness differs")
    return {"pod_uid": pod["metadata"]["uid"], "image_id": main["imageID"], "restarts": main.get("restartCount")}


def wait_ready(state, *, target, timeout=420):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return ready_writer(state, target=target)
        except MigrationError:
            require(time.monotonic() < deadline, "authoritative n8n writer did not become ready; preserve its data")
            time.sleep(3)


def verify_target(state):
    placement()
    require(TARGET.is_dir() and not TARGET.is_symlink() and
            (TARGET.stat().st_dev, TARGET.stat().st_ino) == (state["target_device"], state["target_inode"]),
            "published live directory identity changed")
    check_target_binding(state)
    check_configuration(state)
    require(digest_file(TARGET / "config") == load("copy-validation.json")["encryption_config_sha256"],
            "published encryption configuration changed")


def recover():
    if not os.path.lexists(RECOVERY / "state.json"):
        run(["systemctl", "disable", RECOVERY_SERVICE])
        return
    state = load("state.json")
    require(state.get("version") == 1 and state.get("authoritative") in ("source", "target"), "migration journal is invalid")
    if state["phase"] in ("complete", "source-recovered"):
        run(["systemctl", "disable", RECOVERY_SERVICE])
        return
    bindings()
    check_configuration(state)
    if state["authoritative"] == "source":
        deployment = checked_deployment(state)
        if deployment["spec"]["replicas"] == 0:
            wait_stopped(state)
        else:
            require(not any(references(pod, {CLAIM}) for pod in active_pods()), "a target writer appeared before authority transfer")
        if deployment["spec"]["template"] != state["original_template"]:
            require(deployment["spec"]["replicas"] == 0, "unexpected writer appeared before authority transfer")
            assert_stopped(state)
            patch_deployment(state, replicas=0, target=False)
        patch_deployment(state, replicas=1, target=False)
        wait_ready(state, target=False, timeout=300)
        state["phase"] = "source-recovered"
        save("state.json", state)
        print("original n8n writer recovered; partial copies retained; cutover is incomplete", flush=True)
    else:
        verify_target(state)
        deployment = checked_deployment(state)
        require(deployment["spec"]["template"] == state["target_template"], "authoritative target template changed")
        if deployment["spec"]["replicas"] == 0:
            wait_stopped(state)
        patch_deployment(state, replicas=1, target=True)
        wait_ready(state, target=True, timeout=300)
        print("new n8n data remains authoritative and its writer is ready; migration verification must be resumed", flush=True)
    run(["systemctl", "disable", RECOVERY_SERVICE])


def unit_properties(name, properties):
    output = run(["systemctl", "show", name, "--property=" + ",".join(properties)], check=False).stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def regular_root_file(path):
    attributes = path.lstat()
    require(stat.S_ISREG(attributes.st_mode) and attributes.st_uid == 0 and attributes.st_nlink == 1 and
            not stat.S_IMODE(attributes.st_mode) & 0o022, "installed runtime or unit is not a protected root-owned file")


def check_units(*, loaded=False):
    root_directory(UNIT_DIRECTORY, 0o755)
    program = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/migrate-n8n-storage.py")
    for name in (SERVICE, RECOVERY_SERVICE):
        unit = UNIT_DIRECTORY / name
        if os.path.lexists(unit):
            regular_root_file(unit)
            require(unit.read_text().startswith("# managed by nas-backup-migration\n"), "refusing to replace an unmanaged unit")
        values = unit_properties(name, ("FragmentPath", "DropInPaths", "ExecStart", "ExecStopPost", "OnFailure", "Restart"))
        require(values.get("FragmentPath", "") in ("", str(unit)) and not values.get("DropInPaths") and
                not os.path.lexists(UNIT_DIRECTORY / (name + ".d")), "an unmanaged unit or drop-in overrides n8n migration")
        if loaded:
            expected_start = program + (" --run" if name == SERVICE else " --recover")
            commands = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStart", ""))
            stops = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStopPost", ""))
            require(values.get("FragmentPath") == str(unit) and [value.strip() for value in commands] == [expected_start] and
                    [value.strip() for value in stops] == ([program + " --recover"] if name == SERVICE else []) and
                    values.get("OnFailure", "").split() == ([RECOVERY_SERVICE] if name == SERVICE else []) and
                    values.get("Restart") == ("no" if name == SERVICE else "on-failure"),
                    "loaded n8n migration/recovery commands differ from the reviewed units")


def migrate():
    if os.path.lexists(RECOVERY / "state.json"):
        state = load("state.json")
        if state.get("phase") == "complete":
            verify_target(state)
            ready_writer(state, target=True)
            print(json.dumps(load("result.json"), indent=2), flush=True)
            return
        recover()
        raise MigrationError("prior migration recovered; relay its journal output before another attempt")
    state = preflight()
    state["phase"] = "stopping"
    save("state.json", state)
    print("briefly stopping n8n for a consistent SQLite/data copy", flush=True)
    patch_deployment(state, replicas=0, target=False)
    wait_stopped(state)
    copy_and_publish(state)
    create_binding(state)
    bindings()
    check_configuration(state)
    assert_stopped(state)
    patch_deployment(state, replicas=0, target=True)
    state["phase"] = "target-starting"
    state["authoritative"] = "target"
    # Persist BEFORE any Pod can start. A timeout or crash must never revive the stale database.
    save("state.json", state)
    verify_target(state)
    patch_deployment(state, replicas=1, target=True)
    expected = wait_ready(state, target=True)
    print("n8n is ready on the separated claim; observing it for one minute", flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        require(ready_writer(state, target=True) == expected, "n8n restarted or changed during verification")
        time.sleep(3)
    verify_target(state)
    require(tree_manifest(SOURCE, ignore_live=True) == load("source-files.json"), "retained original data changed")
    bindings()
    proof = load("copy-validation.json")
    result = {"result": "passed", "completed_at": now(), "namespace": NAMESPACE,
              "new_claim": CLAIM, "new_path": str(BASE / "live-data"), "original_claim_retained": "n8n-data",
              "images_claim_unchanged": "n8n-images", "sqlite_integrity": "passed", "encryption_key_preserved": True,
              "copied_files": sum(entry["type"] == "file" for entry in load("source-files.json")),
              "sqlite_tables": len(proof["relation_counts"]), "workflows": proof["relation_counts"]["workflow_entity"],
              "credentials": proof["relation_counts"]["credentials_entity"], **expected}
    save("result.json", result)
    state["phase"] = "complete"
    save("state.json", state)
    print(json.dumps(result, indent=2), flush=True)
    print("n8n live storage separated; application sign-in/workflow checks and backup onboarding remain pending", flush=True)


def install(revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "revision must be the immutable downloaded commit")
    check_units()
    for name in (SERVICE, RECOVERY_SERVICE):
        status = run(["systemctl", "show", name, "--property=ActiveState", "--value"], check=False).stdout.strip()
        require(status in ("", "inactive", "failed"), "a n8n migration/recovery service is already active")
    root_directory(INSTALL, 0o755)
    destination = INSTALL / revision
    if destination.exists():
        root_directory(destination, 0o755)
        for name in FILES:
            root_directory((destination / name).parent, 0o755)
            regular_root_file(destination / name)
            require((destination / name).read_bytes() == (ROOT / name).read_bytes(), "installed immutable runtime differs")
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=INSTALL))
        try:
            for name in FILES:
                source = ROOT / name
                require(source.is_file() and not source.is_symlink(), "downloaded runtime file is missing or symlinked")
                output = temporary / name
                output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                output.write_bytes(source.read_bytes())
                os.chmod(output, 0o644)
            for path in sorted(temporary.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_dir():
                    os.chmod(path, 0o755)
                    sync_directory(path)
                else:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            os.chmod(temporary, 0o755)
            sync_directory(temporary)
            rename_no_replace(temporary, destination)
            sync_directory(INSTALL)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    pointer = INSTALL / "current"
    if os.path.lexists(pointer):
        require(pointer.is_symlink() and pointer.lstat().st_uid == 0 and re.fullmatch(r"[0-9a-f]{40}", os.readlink(pointer)),
                "refusing to replace an unmanaged runtime pointer")
    pending = INSTALL / (".current-" + uuid.uuid4().hex)
    os.symlink(revision, pending)
    os.replace(pending, pointer)
    sync_directory(INSTALL)
    for name in (SERVICE, RECOVERY_SERVICE):
        unit = UNIT_DIRECTORY / name
        descriptor, pending = tempfile.mkstemp(prefix=".n8n-migration-", dir=unit.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((ROOT / ("scripts/backups/systemd/" + name)).read_bytes())
            os.fchmod(stream.fileno(), 0o644)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, unit)
    sync_directory(UNIT_DIRECTORY)
    run(["systemd-analyze", "verify", *(str(UNIT_DIRECTORY / name) for name in (SERVICE, RECOVERY_SERVICE))])
    run(["systemctl", "daemon-reload"])
    check_units(loaded=True)
    run(["systemctl", "enable", RECOVERY_SERVICE])
    print("migration runs under systemd and can recover after an SSH disconnect or reboot", flush=True)
    result = run(["systemctl", "start", SERVICE], timeout=2200, check=False)
    logs = run(["journalctl", "-u", SERVICE, "-u", RECOVERY_SERVICE, "-n", "150", "--no-pager", "-o", "cat"]).stdout
    print(logs, end="", flush=True)
    require(result.returncode == 0, "n8n migration did not complete; relay the output and retain all original/target data")
    state, receipt = load("state.json"), load("result.json")
    require(state.get("phase") == "complete" and state.get("authoritative") == "target" and
            receipt.get("result") == "passed" and receipt.get("new_claim") == CLAIM and receipt.get("namespace") == NAMESPACE,
            "systemd returned without a complete n8n migration receipt")
    verify_target(state)
    ready_writer(state, target=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--recover", action="store_true")
    parser.add_argument("--revision")
    arguments = parser.parse_args()
    require(os.geteuid() == 0, "run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    root_directory(RECOVERY, 0o700)
    with ExitStack() as stack:
        stack.enter_context(lock(RECOVERY / (".install.lock" if arguments.install else ".operation.lock")))
        if arguments.install:
            install(arguments.revision or "")
        else:
            if not arguments.recover:
                root_directory(GLOBAL_LOCK.parent, 0o700)
                stack.enter_context(lock(GLOBAL_LOCK))
            stack.enter_context(lock(RECOVERY.parent / ".setup.lock"))
            if arguments.recover:
                recover()
            else:
                migrate()


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.TimeoutExpired):
        # Configuration, filenames, SQLite errors, and kubectl stderr can contain private data.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, MigrationError) else "filesystem, API, runtime, or SQLite check failed; private data omitted"
        print("n8n migration stopped: " + message, file=sys.stderr, flush=True)
        sys.exit(1)
