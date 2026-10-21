#!/usr/bin/env python3
"""Move the inventoried GPT container's config/SQLite state to retained OMV claims."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import copy
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]

def module(filename, name):
    specification = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(result)
    return result

capture = module("capture-paperless-gpt.py", "gpt_capture")
inspection = module("inspect-paperless-gpt-cutover.py", "gpt_inspection")
require = capture.require
RECOVERY = capture.RECOVERY.parent / "gpt-storage-migration"
LIVE = capture.PHYSICAL / "gpt-live"
INSTALL = Path("/usr/local/lib/k3s-backups/paperless-gpt-storage")
SERVICE = "k3s-paperless-gpt-storage-migration.service"
RECOVERY_SERVICE = "k3s-paperless-gpt-storage-recovery.service"
WATCHDOG_SERVICE = "k3s-paperless-gpt-storage-watchdog.service"
UNITS = (SERVICE, RECOVERY_SERVICE, WATCHDOG_SERVICE)
CLAIMS = {name: "paperless-gpt-" + name for name in ("config", "db")}
FILES = ("scripts/backups/migrate-paperless-gpt-storage.py",
         "scripts/backups/capture-paperless-gpt.py", "scripts/backups/inspect-paperless-gpt-cutover.py",
         *("scripts/backups/systemd/" + name for name in UNITS),
         *("apps/paperless-ngx/" + kind + "-gpt-" + component + ".yaml"
           for component in CLAIMS for kind in ("persistent-volume", "persistent-volume-claim")))
FREEZE_SECONDS = 35
CGROUP_ROOT = Path("/sys/fs/cgroup")


def save(name, value):
    capture.save(RECOVERY / name, value)


def load(name):
    return capture.private_json(RECOVERY / name)


def run(arguments, *, stdin=None, timeout=30, check=True):
    result = subprocess.run(arguments, input=stdin, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        save("last-command-error.json", {"program": arguments[0], "returncode": result.returncode,
                                        "stdout": result.stdout[-65536:], "stderr": result.stderr[-65536:]})
        raise capture.CaptureError("command failed; diagnostics saved privately")
    return result


def kube(*arguments, stdin=None, timeout=30):
    return run(["k3s", "kubectl", "--request-timeout=10s", *arguments], stdin=stdin, timeout=timeout).stdout


def get(kind, name=None, namespace=capture.NAMESPACE):
    arguments = ["get", kind, *([name] if name else []), "--ignore-not-found", "-o", "json"]
    if namespace:
        arguments += ["-n", namespace]
    output = kube(*arguments)
    return json.loads(output) if output.strip() else None


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


@contextmanager
def journal():
    # Only short state transitions hold this lock. The watchdog must not wait
    # behind copying, SQLite, Kubernetes calls, or NAS file fsyncs.
    descriptor = os.open(RECOVERY / ".journal.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        attrs = os.fstat(descriptor)
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                stat.S_IMODE(attrs.st_mode) == 0o600, "unsafe migration journal lock")
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def private_child(parent, name):
    path = parent / name
    require(not os.path.lexists(path), "migration destination already exists; retain it for inspection")
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)  # Clear any inherited Btrfs setgid bit.
    capture.fsync_directory(parent)
    return path


def target_template(original):
    target = copy.deepcopy(original)
    spec = target["spec"]
    require(len(spec.get("containers", [])) == 1 and not spec.get("initContainers"),
            "unexpected GPT sidecar or init container")
    container = spec["containers"][0]
    require(container["name"] == "paperless-gpt" and container["image"] == capture.IMAGE,
            "GPT image or container changed")
    mounts = container.setdefault("volumeMounts", [])
    volumes = spec.setdefault("volumes", [])
    for component, claim in CLAIMS.items():
        path = "/app/" + component
        require(not any(mount["name"] == claim or path == mount["mountPath"] or
                        path.startswith(mount["mountPath"].rstrip("/") + "/") or
                        mount["mountPath"].startswith(path + "/") for mount in mounts),
                "GPT already has a conflicting state mount")
        require(not any(volume["name"] == claim for volume in volumes), "GPT has a conflicting volume name")
        mounts.append({"name": claim, "mountPath": path})
        volumes.append({"name": claim, "persistentVolumeClaim": {"claimName": claim}})
    selectors = spec.setdefault("nodeSelector", {})
    require(selectors.get("kubernetes.io/hostname", "openmediavault") == "openmediavault",
            "GPT node selector changed")
    selectors["kubernetes.io/hostname"] = "openmediavault"
    return target


def storage_resources():
    objects = []
    for component, claim in CLAIMS.items():
        expected_pv = {"accessModes": ["ReadWriteOnce"], "capacity": {"storage": "1Gi"},
                       "volumeMode": "Filesystem", "persistentVolumeReclaimPolicy": "Retain",
                       "storageClassName": "paperless-local", "local": {"path": str(capture.BASE / "gpt-live" / component)},
                       "claimRef": {"namespace": capture.NAMESPACE, "name": claim},
                       "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                           {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}}}
        expected_pvc = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}},
                        "volumeMode": "Filesystem", "storageClassName": "paperless-local", "volumeName": claim}
        for kind, prefix, expected in (("PersistentVolume", "persistent-volume", expected_pv),
                                       ("PersistentVolumeClaim", "persistent-volume-claim", expected_pvc)):
            filename = ROOT / ("apps/paperless-ngx/" + prefix + "-gpt-" + component + ".yaml")
            obj = json.loads(kube("create", "--dry-run=client", "-f", str(filename), "-o", "json"))
            require(obj.get("kind") == kind and obj["metadata"]["name"] == claim and obj["spec"] == expected and
                    (kind != "PersistentVolumeClaim" or obj["metadata"].get("namespace") == capture.NAMESPACE),
                    "new storage manifest differs from the approved NAS placement")
            objects.append(obj)
    return objects


def bindings(state):
    for component, claim in CLAIMS.items():
        pvc, pv = get("pvc", claim), get("pv", claim, namespace=None)
        expected = state["bindings"][component]
        require(pvc and pv and pvc["metadata"]["uid"] == expected["pvc"] and pv["metadata"]["uid"] == expected["pv"] and
                pvc.get("status", {}).get("phase") == "Bound" and pvc["spec"].get("volumeName") == claim and
                pvc["spec"].get("accessModes") == ["ReadWriteOnce"] and
                pv["spec"].get("claimRef", {}).get("uid") == expected["pvc"] and
                pv["spec"].get("persistentVolumeReclaimPolicy") == "Retain" and
                pv["spec"].get("local") == {"path": str(capture.BASE / "gpt-live" / component)} and
                pv["spec"].get("nodeAffinity") == state["storage_affinity"], "new GPT binding changed")
    prompt, volume = get("pvc", "paperless-gpt"), get("pv", "paperless-gpt", namespace=None)
    require(prompt["metadata"]["uid"] == capture.CLAIM_UID and volume["metadata"]["uid"] == capture.PV_UID and
            volume["spec"].get("claimRef", {}).get("uid") == capture.CLAIM_UID and
            volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "original prompt binding changed")


def identity_matches(anchor):
    if anchor["boot_id"] != boot_id():
        return False
    try:
        descriptor = os.pidfd_open(anchor["pid"])
        try:
            return (capture.process_start(anchor["pid"]) == anchor["process_start"] and
                    not select.select([descriptor], [], [], 0)[0])
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return False


@contextmanager
def cgroup(anchor):
    require(identity_matches(anchor), "original GPT process is no longer present")
    membership = inspection.unified_cgroup(Path("/proc", str(anchor["pid"]), "cgroup").read_text(), anchor["container_id"])
    require(membership and list(membership) == anchor["cgroup_parts"], "GPT cgroup membership changed")
    with ExitStack() as stack:
        descriptor = os.open(CGROUP_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, descriptor)
        for part in membership:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        attrs = os.fstat(descriptor)
        require([attrs.st_dev, attrs.st_ino] == anchor["cgroup_identity"], "GPT cgroup was replaced")
        processes = inspection.read_file("cgroup.procs", directory=descriptor).split()
        require(str(anchor["pid"]) in processes and len(processes) <= 128 and
                not any(stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)
                        for name in os.listdir(descriptor)), "GPT process is absent or its cgroup is no longer a bounded leaf")
        yield descriptor


def frozen(descriptor):
    events = dict(line.split() for line in inspection.read_file("cgroup.events", directory=descriptor).splitlines())
    return events.get("frozen") == "1" and inspection.read_file("cgroup.freeze", directory=descriptor).strip() == "1"


def set_freeze(descriptor, value):
    descriptor = os.open("cgroup.freeze", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        require(os.write(descriptor, b"1" if value else b"0") == 1, "freezer control write was incomplete")
    finally:
        os.close(descriptor)


def thaw_source(state):
    # Never thaw a source after publishing target authority, even on a timeout.
    require(state["authoritative"] == "source", "refusing to thaw the superseded GPT writer")
    anchor = state["anchor"]
    if not identity_matches(anchor):
        raise capture.CaptureError("original volatile GPT process vanished; keep all captures and relay diagnostics")
    with cgroup(anchor) as descriptor:
        set_freeze(descriptor, False)
    print("original GPT process thawed; persistent cutover was not committed", flush=True)


def before_deadline(state):
    return (state["anchor"]["boot_id"] == boot_id() and
            time.monotonic() < state["freeze_deadline"] and state["phase"] == "frozen" and
            state["authoritative"] == "source")


def watchdog_tick():
    with journal():
        if not (RECOVERY / "state.json").exists():
            return False
        state = load("state.json")
        if state["phase"] == "complete" or state["authoritative"] == "target":
            return True
        if state["phase"] == "aborted":
            # A watchdog may itself have died after recording cancellation but
            # before thawing. Repeating thaw is safe; treating the marker alone
            # as completion would strand the process in its frozen cgroup.
            thaw_source(state)
            return True
        if state["authoritative"] == "source" and state["phase"] in ("freezing", "frozen"):
            if state["anchor"]["boot_id"] != boot_id() or time.monotonic() >= state["freeze_deadline"]:
                # Record cancellation BEFORE releasing the process. The copier
                # must subsequently reject its now potentially stale output.
                state["phase"] = "aborted"
                save("state.json", state)
                thaw_source(state)
                return True
    return False


def patch_writer(state, replicas):
    deployment = get("deployment", "paperless-gpt")
    require(deployment["metadata"]["uid"] == capture.DEPLOYMENT_UID and
            deployment["spec"].get("strategy", {}).get("type") == "Recreate" and
            deployment["spec"]["template"] in (state["original_template"], state["target_template"]),
            "GPT controller/template changed; no overwrite permitted")
    patch = [{"op": "test", "path": "/metadata/uid", "value": capture.DEPLOYMENT_UID},
             {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
             {"op": "replace", "path": "/spec/template", "value": state["target_template"]},
             {"op": "replace", "path": "/spec/replicas", "value": replicas}]
    with tempfile.NamedTemporaryFile(mode="w", dir=RECOVERY, prefix=".patch-", suffix=".json") as stream:
        json.dump(patch, stream)
        stream.flush()
        kube("-n", capture.NAMESPACE, "patch", "deployment", "paperless-gpt", "--type=json", "--patch-file", stream.name)


def owned_pods():
    sets = {item["metadata"]["uid"] for item in get("replicasets")["items"]
            if any(owner.get("controller") is True and owner.get("uid") == capture.DEPLOYMENT_UID
                   for owner in item["metadata"].get("ownerReferences", []))}
    return [pod for pod in get("pods")["items"] if any(owner.get("controller") is True and owner.get("uid") in sets
                                                       for owner in pod["metadata"].get("ownerReferences", []))]


def zero_controllers(state):
    patch_writer(state, 0)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        deployment = get("deployment", "paperless-gpt")
        sets = [item for item in get("replicasets")["items"] if any(owner.get("uid") == capture.DEPLOYMENT_UID
                and owner.get("controller") for owner in item["metadata"].get("ownerReferences", []))]
        if (deployment.get("status", {}).get("observedGeneration", 0) >= deployment["metadata"]["generation"] and
                all(item["spec"].get("replicas", 1) == 0 for item in sets)):
            return
        time.sleep(0.5)
    raise capture.CaptureError("controller has not stopped its old ReplicaSets; GPT remains fenced for recovery")


def fence_original(state):
    # Mark the exact Pod deleting before SIGKILL. A terminating Pod is never
    # restarted by kubelet. All owned ReplicaSets have already been scaled to 0.
    pod = get("pod", capture.POD)
    if pod:
        require(pod["metadata"]["uid"] == capture.POD_UID, "original Pod name was reused")
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 60,
                   "preconditions": {"uid": capture.POD_UID}}
        with tempfile.NamedTemporaryFile(mode="w", dir=RECOVERY, prefix=".delete-", suffix=".json") as stream:
            json.dump(options, stream)
            stream.flush()
            kube("delete", "--raw", "/api/v1/namespaces/" + capture.NAMESPACE + "/pods/" + capture.POD,
                 "-f", stream.name)
    if identity_matches(state["anchor"]):
        with cgroup(state["anchor"]) as descriptor:
            require(frozen(descriptor), "original GPT writer was unexpectedly thawed after target authority")
            pidfd = os.pidfd_open(state["anchor"]["pid"])
            try:
                require(identity_matches(state["anchor"]), "original GPT PID changed before fencing")
                killer = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    require(os.write(killer, b"1") == 1, "cgroup kill request was incomplete")
                finally:
                    os.close(killer)
                require(select.select([pidfd], [], [], 15)[0], "original process did not exit after fencing")
                # The main process can exit before a killed exec/preStop child.
                # Wait for the whole leaf to empty, not just the main pidfd.
                deadline = time.monotonic() + 5
                while True:
                    try:
                        events = dict(line.split() for line in inspection.read_file("cgroup.events", directory=descriptor).splitlines())
                        if events.get("populated") == "0":
                            break
                    except FileNotFoundError:
                        # A cgroup can only be removed after its tasks exit.
                        require(not CGROUP_ROOT.joinpath(*state["anchor"]["cgroup_parts"]).exists(),
                                "old cgroup population evidence is unavailable")
                        break
                    require(time.monotonic() < deadline, "the old container still has live child processes")
                    time.sleep(0.01)
            finally:
                os.close(pidfd)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if not owned_pods() and not identity_matches(state["anchor"]):
            return
        time.sleep(1)
    raise capture.CaptureError("old GPT Pods are still terminating; target remains stopped")


def prepare():
    capture.placement()
    receipt = capture.private_json(capture.RECOVERY / "result.json")
    require(receipt.get("capture_id") == inspection.CAPTURE_ID and
            receipt.get("manifest_sha256") == inspection.MANIFEST_SHA256, "verified safety capture changed")
    proof = capture.verify_capture(receipt)
    anchor, deployment, pod = capture.live_source()
    require(anchor == proof["source"], "GPT changed since the verified safety capture")
    writers = owned_pods()
    selector = deployment["spec"]["selector"].get("matchLabels", {})
    matches = [item for item in get("pods")["items"] if all(item["metadata"].get("labels", {}).get(key) == value
                for key, value in selector.items())]
    require(selector and len(writers) == len(matches) == 1 and
            writers[0]["metadata"]["uid"] == matches[0]["metadata"]["uid"] == capture.POD_UID,
            "unexpected additional or orphaned GPT writer")
    require(not os.path.lexists(LIVE), "target GPT live directory exists; retain it and inspect the migration journal")
    targets = storage_resources()
    require(not any(get(kind, claim, namespace=namespace) for claim in CLAIMS.values()
                    for kind, namespace in (("pv", None), ("pvc", capture.NAMESPACE))), "new GPT storage names already exist")
    cgroups = inspection.cgroup_summary(anchor["pid"], anchor["container_id"])
    require(cgroups["unified"] and cgroups["freezer_available"] and cgroups["freeze_value"] == "0" and
            cgroups["frozen"] == "0" and cgroups["processes"] == 1 and cgroups["descendant_cgroups"] == 0 and
            not cgroups["mount_read_only"] and cgroups["freezer_owner"] == {"uid": 0, "gid": 0, "mode": "0o644"},
            "GPT freezer prerequisites changed")
    parts = inspection.unified_cgroup(Path("/proc", str(anchor["pid"]), "cgroup").read_text(), anchor["container_id"])
    attrs = CGROUP_ROOT.joinpath(*parts).stat()
    anchor.update(boot_id=boot_id(), cgroup_parts=list(parts), cgroup_identity=[attrs.st_dev, attrs.st_ino])
    with cgroup(anchor) as descriptor:
        attrs = os.stat("cgroup.kill", dir_fd=descriptor, follow_symlinks=False)
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and not stat.S_IMODE(attrs.st_mode) & 0o022,
                "kernel container fencing control is unavailable or unsafe")
    state = {"version": 1, "phase": "prepared", "authoritative": "source", "anchor": anchor,
             "migration_id": uuid.uuid4().hex, "original_template": deployment["spec"]["template"],
             "target_template": target_template(deployment["spec"]["template"]), "bindings": {},
             "storage_affinity": targets[0]["spec"]["nodeAffinity"], "started_at": capture.now()}
    save("deployment.before.json", deployment)
    save("pod.before.json", pod)
    save("state.json", state)
    private_child(capture.PHYSICAL, "gpt-live")
    for component in CLAIMS:
        private_child(LIVE, component)
    for obj in targets:
        kube("create", "-f", "-", stdin=json.dumps(obj))
        kind = "pv" if obj["kind"] == "PersistentVolume" else "pvc"
        name = obj["metadata"]["name"]
        actual = get(kind, name, namespace=None if kind == "pv" else capture.NAMESPACE)
        component = next(component for component, claim in CLAIMS.items() if claim == name)
        state["bindings"].setdefault(component, {})[kind] = actual["metadata"]["uid"]
        save("state.json", state)
    for claim in CLAIMS.values():
        kube("-n", capture.NAMESPACE, "wait", "pvc/" + claim, "--for=jsonpath={.status.phase}=Bound", "--timeout=45s", timeout=55)
    bindings(state)
    return state


def write_tree(directory, entries, contents):
    for name, entry in sorted(entries.items(), key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
        path = directory / name
        if entry["type"] == "directory":
            path.mkdir(mode=0o700)
        else:
            capture.write_file(path, contents[name], entry)
    for name, entry in sorted(entries.items(), key=lambda item: len(PurePosixPath(item[0]).parts), reverse=True):
        path = directory / name
        if entry["type"] == "directory":
            os.chown(path, entry["uid"], entry["gid"])
            os.chmod(path, entry["mode"])
            os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]), follow_symlinks=False)
            capture.fsync_directory(path)


def normalize_database(raw_directory, output_directory, database_name, metadata):
    # Only this fresh, private COPY is opened writable. Keeping WAL/journal files
    # alongside the DB lets SQLite recover an interrupted transaction on the copy.
    source = raw_directory / database_name
    target = output_directory / database_name
    require(not target.exists(), "standalone SQLite destination already exists")
    with closing(sqlite3.connect(source, timeout=1)) as database, closing(sqlite3.connect(target)) as output:
        database.execute("PRAGMA trusted_schema=OFF")
        require(database.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "copied GPT SQLite recovery failed")
        relations = capture.counts(database)
        database.backup(output)
        output.execute("PRAGMA journal_mode=DELETE")
        require(output.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and capture.counts(output) == relations,
                "standalone GPT SQLite copy failed validation")
    os.chown(target, metadata["uid"], metadata["gid"])
    os.chmod(target, metadata["mode"])
    with target.open("rb") as stream:
        os.fsync(stream.fileno())
    capture.fsync_directory(target.parent)
    return relations


def capture_frozen(state):
    anchor = state["anchor"]
    attempt = private_child(capture.CAPTURES, "gpt-cutover-" + state["migration_id"])
    raw, normalized = private_child(attempt, "raw"), private_child(attempt, "normalized")
    with ExitStack() as stack:
        pidfd = os.pidfd_open(anchor["pid"])
        stack.callback(os.close, pidfd)
        rootfd = os.open("/proc/" + str(anchor["pid"]) + "/root", os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, rootfd)
        appfd = os.open("app", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        stack.callback(os.close, appfd)
        roots = {}
        for component in CLAIMS:
            roots[component] = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=appfd)
            stack.callback(os.close, roots[component])
            require(os.fstat(roots[component]).st_dev == os.fstat(rootfd).st_dev, "unexpected GPT state mount")
        database_relative, database_parent, _, _ = capture.find_database(roots["db"])
        os.close(database_parent)
        database_name = "db/" + database_relative
        with cgroup(anchor) as descriptor:
            require(not frozen(descriptor), "original GPT is already frozen")
            with journal():
                state["phase"] = "freezing"
                state["freeze_deadline"] = time.monotonic() + FREEZE_SECONDS
                save("state.json", state)
                set_freeze(descriptor, True)
            deadline = time.monotonic() + 3
            while not frozen(descriptor) and time.monotonic() < deadline:
                time.sleep(0.01)
            require(frozen(descriptor), "GPT did not freeze within three seconds")
            with journal():
                state = load("state.json")
                require(state["phase"] == "freezing" and time.monotonic() < state["freeze_deadline"], "GPT freeze was cancelled")
                state["phase"] = "frozen"
                save("state.json", state)
            print("GPT paused; copying its config, database, and SQLite recovery sidecars", flush=True)
            before, contents = capture.collect(roots)
            require(database_name in contents and any(name.startswith("config/") for name in contents), "GPT state is incomplete")
            after, repeated = capture.collect(roots)
            capture.verify_source_paths(anchor, pidfd, rootfd, appfd, roots)
            require(before == after and contents == repeated and frozen(descriptor), "GPT source changed while frozen")
            save("frozen-source.json", before)
            write_tree(raw, before, contents)
            # Preserve the exact crash-consistent files separately from the copy
            # SQLite may recover. Never edit this retained raw evidence.
            recovery_copy = private_child(attempt, "sqlite-recovery")
            write_tree(recovery_copy, before, contents)
            sidecars = {database_name + suffix for suffix in ("-wal", "-shm", "-journal")}
            clean_entries = {name: value for name, value in before.items() if name not in sidecars}
            config_entries = {name: value for name, value in clean_entries.items() if name != database_name}
            write_tree(normalized, config_entries, contents)
            relations = normalize_database(recovery_copy, normalized, database_name, before[database_name])
            for name, entry in sorted(clean_entries.items(), key=lambda item: len(PurePosixPath(item[0]).parts), reverse=True):
                if entry["type"] == "directory":
                    os.utime(normalized / name, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                    capture.fsync_directory(normalized / name)
            manifest = {"migration_id": state["migration_id"], "database": database_name, "relation_counts": relations,
                        "files": capture.snapshot_files(normalized), "raw_files": capture.snapshot_files(raw)}
            save("copy-validation.json", manifest)
            # Populate empty retained targets only. Publish their complete proof
            # before the authority transition can authorize Pod replacement.
            for component in CLAIMS:
                require(not any((LIVE / component).iterdir()), "new live directory is no longer empty")
            for component in CLAIMS:
                source = normalized / component
                target = LIVE / component
                for child in source.iterdir():
                    destination = target / child.name
                    require(not os.path.lexists(destination), "target entry already exists")
                    if child.is_dir():
                        shutil.copytree(child, destination, copy_function=shutil.copy2)
                    else:
                        shutil.copy2(child, destination)
                attrs = source.stat()
                os.chown(target, attrs.st_uid, attrs.st_gid)
                os.chmod(target, stat.S_IMODE(attrs.st_mode))
                os.utime(target, ns=(attrs.st_mtime_ns, attrs.st_mtime_ns))
                for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                    require(not path.is_symlink(), "copied target contains a link")
                    metadata = manifest["files"][path.relative_to(LIVE).as_posix()]
                    os.chown(path, metadata["uid"], metadata["gid"])
                    os.chmod(path, metadata["mode"])
                    os.utime(path, ns=(metadata["mtime_ns"], metadata["mtime_ns"]))
                    if path.is_file():
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())
                    else:
                        capture.fsync_directory(path)
                capture.fsync_directory(target)
            capture.fsync_directory(LIVE)
            require(capture.snapshot_files(LIVE) == manifest["files"], "published config/database metadata or checksums differ")
            capture.verify_source_paths(anchor, pidfd, rootfd, appfd, roots)
            require(capture.collect(roots)[0] == before and frozen(descriptor), "GPT changed before target authority")
            with journal():
                latest = load("state.json")
                require(before_deadline(latest) and frozen(descriptor), "pause deadline expired; copied state cannot authorize cutover")
                latest.update(authoritative="target", phase="target-committed", capture_directory=str(attempt),
                              copy_sha256=capture.fingerprint(manifest))
                save("state.json", latest)
                return latest


def target_proof(state, *, before_start=False):
    manifest = load("copy-validation.json")
    require(capture.fingerprint(manifest) == state["copy_sha256"] and
            Path(state["capture_directory"]) == capture.CAPTURES / ("gpt-cutover-" + state["migration_id"]),
            "cutover recovery receipt changed")
    normalized = Path(state["capture_directory"]) / "normalized"
    require(capture.snapshot_files(normalized) == manifest["files"], "retained final capture changed")
    if before_start:
        require(capture.snapshot_files(LIVE) == manifest["files"], "target files changed before their first start")
    bindings(state)
    return manifest


def ready_writer(state):
    deployment = get("deployment", "paperless-gpt")
    require(deployment["metadata"]["uid"] == capture.DEPLOYMENT_UID and deployment["spec"].get("replicas") == 1 and
            deployment["spec"]["template"] == state["target_template"], "target GPT deployment changed")
    pods = [pod for pod in owned_pods() if not pod["metadata"].get("deletionTimestamp")]
    if len(pods) != 1:
        return None
    pod = pods[0]
    if pod["metadata"]["uid"] == capture.POD_UID:
        return None
    containers = pod["spec"]["containers"]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if not (pod["spec"].get("nodeName") == "openmediavault" and pod.get("status", {}).get("phase") == "Running" and
            len(containers) == len(statuses) == 1 and statuses[0].get("ready") is True):
        return None
    require(containers[0]["image"] == capture.IMAGE and statuses[0].get("restartCount") == 0 and
            statuses[0].get("imageID", "").removeprefix("docker.io/") == capture.IMAGE,
            "target GPT image changed or restarted")
    for component, claim in CLAIMS.items():
        require(any(volume == {"name": claim, "persistentVolumeClaim": {"claimName": claim}}
                    for volume in pod["spec"]["volumes"]) and
                any(mount.get("name") == claim and mount.get("mountPath") == "/app/" + component and
                    not mount.get("readOnly") and not mount.get("subPath") and not mount.get("subPathExpr")
                    for mount in containers[0]["volumeMounts"]), "target GPT state mount differs")
    return {"pod_uid": pod["metadata"]["uid"], "image_id": statuses[0]["imageID"], "restarts": 0}


def start_target(state):
    proof = target_proof(state)
    current = get("deployment", "paperless-gpt")
    if (state["phase"] == "target-committed" or current["spec"]["template"] != state["target_template"] or
            identity_matches(state["anchor"]) or any(pod["metadata"]["uid"] == capture.POD_UID for pod in owned_pods())):
        zero_controllers(state)
        fence_original(state)
    # Once starting has been journalled the target may legitimately change on
    # startup. Recovery must NEVER restore the prestart snapshot over that data.
    if state["phase"] == "target-committed":
        target_proof(state, before_start=True)
        state["phase"] = "target-starting"
        save("state.json", state)
    patch_writer(state, 1)
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        ready = ready_writer(state)
        if ready:
            return ready, proof
        time.sleep(2)
    raise capture.CaptureError("GPT target did not become ready; retain target data and relay diagnostics")


def recover():
    if not (RECOVERY / "state.json").exists():
        return
    with journal():
        state = load("state.json")
        if state["authoritative"] == "source":
            if state["phase"] in ("freezing", "frozen", "aborted"):
                state["phase"] = "aborted"
                save("state.json", state)
                thaw_source(state)
            print("source remains authoritative; no Pod replacement was authorized", flush=True)
            return
    if state["phase"] == "complete":
        # This unit repairs an interrupted migration, not normal app restarts.
        # After a later reboot the Pod may still be starting or have restarts.
        return
    start_target(state)
    print("authoritative persistent GPT writer recovered; relay output before resuming verification", flush=True)


def migrate():
    if (RECOVERY / "state.json").exists():
        state = load("state.json")
        if state["phase"] == "complete":
            target_proof(state)
            require(ready_writer(state), "persistent GPT target is not ready")
            print(json.dumps(load("result.json"), indent=2), flush=True)
            return
        recover()
        require(state["authoritative"] == "target", "prior attempt retained; relay diagnostics before retrying")
        state = load("state.json")
    else:
        state = prepare()
        require(capture.live_source()[0] == {key: value for key, value in state["anchor"].items()
                if key not in ("boot_id", "cgroup_parts", "cgroup_identity")}, "GPT changed during storage preparation")
        run(["systemctl", "start", WATCHDOG_SERVICE])
        require(run(["systemctl", "is-active", WATCHDOG_SERVICE]).stdout.strip() == "active", "pause watchdog is not running")
        state = capture_frozen(state)
    ready, proof = start_target(state)
    print("GPT ready on retained config/database claims; observing stability for one minute", flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        require(ready_writer(state) == ready, "persistent GPT writer changed during verification")
        time.sleep(3)
    target_proof(state)
    require(not identity_matches(state["anchor"]), "superseded GPT process is still present")
    result = {"result": "passed", "namespace": capture.NAMESPACE, "component": "paperless-gpt",
              "completed_at": capture.now(), "claims": list(CLAIMS.values()),
              "paths": [str(capture.BASE / "gpt-live" / component) for component in CLAIMS],
              "prompts_claim_unchanged": "paperless-gpt", "safety_capture_retained": inspection.CAPTURE_ID,
              "final_capture_retained": "gpt-cutover-" + state["migration_id"],
              "files_verified_before_start": sum(item["type"] == "file" for item in proof["files"].values()),
              "sqlite_integrity": "passed", "sqlite_tables": len(proof["relation_counts"]),
              "live_template_fields_preserved": True, **ready}
    save("result.json", result)
    with journal():
        state["phase"] = "complete"
        save("state.json", state)
    print(json.dumps(result, indent=2), flush=True)
    print("GPT state is persistent; browser checks and Paperless namespace backup onboarding remain pending", flush=True)


def unit_properties(name):
    output = run(["systemctl", "show", name, "--property=FragmentPath,DropInPaths,ExecStart,ExecStopPost,OnFailure,Restart"], check=False).stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def check_units(loaded=False):
    program = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/migrate-paperless-gpt-storage.py")
    for name, mode in zip(UNITS, ("--run", "--recover", "--watchdog")):
        unit = capture.UNITS / name
        if os.path.lexists(unit):
            attrs = unit.lstat()
            require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == 0 and attrs.st_nlink == 1 and
                    not stat.S_IMODE(attrs.st_mode) & 0o022 and unit.read_text().startswith(capture.MARKER), "refusing to replace an unmanaged unit")
        properties = unit_properties(name)
        require(properties.get("FragmentPath", "") in ("", str(unit)) and not properties.get("DropInPaths") and
                not os.path.lexists(capture.UNITS / (name + ".d")), "unmanaged systemd unit/drop-in")
        if loaded:
            starts = re.findall(r"\bargv\[\]=([^;{}]+)", properties.get("ExecStart", ""))
            stops = re.findall(r"\bargv\[\]=([^;{}]+)", properties.get("ExecStopPost", ""))
            require([value.strip() for value in starts] == [program + " " + mode] and
                    [value.strip() for value in stops] == ([program + " --recover"] if name == SERVICE else []), "loaded unit commands differ")


def install(revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "expected the immutable downloaded revision")
    check_units()
    for name in UNITS:
        require(run(["systemctl", "show", name, "--property=ActiveState", "--value"], check=False).stdout.strip()
                in ("", "inactive", "failed"), "a GPT migration/recovery unit is already active")
    capture.INSTALL = INSTALL
    capture.FILES = FILES
    capture.ROOT = ROOT
    contents = {}
    for name in FILES:
        source = ROOT / name
        require(source.is_file() and not source.is_symlink() and source.stat().st_nlink == 1,
                "downloaded migration runtime is unsafe")
        contents[name] = source.read_bytes()
    capture.managed_directory(INSTALL, 0o755)
    capture.install_runtime(revision, contents)
    pointer = INSTALL / "current"
    if os.path.lexists(pointer):
        require(pointer.is_symlink() and pointer.lstat().st_uid == 0 and
                re.fullmatch(r"[0-9a-f]{40}", os.readlink(pointer)), "unmanaged GPT runtime pointer")
    pending = INSTALL / (".current-" + uuid.uuid4().hex)
    os.symlink(revision, pending)
    os.replace(pending, pointer)
    capture.fsync_directory(INSTALL)
    for name in UNITS:
        destination = capture.UNITS / name
        with tempfile.NamedTemporaryFile(dir=capture.UNITS, prefix=".gpt-unit-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents["scripts/backups/systemd/" + name])
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    capture.fsync_directory(capture.UNITS)
    run(["systemd-analyze", "verify", *(str(capture.UNITS / name) for name in UNITS)])
    run(["systemctl", "daemon-reload"])
    check_units(loaded=True)
    run(["systemctl", "enable", RECOVERY_SERVICE])
    started = "@" + str(int(time.time()))
    print("cutover runs under systemd with an independent pause watchdog and persistent recovery journal", flush=True)
    result = run(["systemctl", "start", SERVICE], timeout=1500, check=False)
    print(run(["journalctl", "-u", SERVICE, "-u", RECOVERY_SERVICE, "-u", WATCHDOG_SERVICE,
               "--since", started, "--no-pager", "-o", "cat"], check=False).stdout, end="", flush=True)
    require(result.returncode == 0, "GPT cutover did not finish; retain all source/target captures and relay this output")
    require(load("state.json")["phase"] == "complete", "cutover receipt is incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("install", "run", "recover", "watchdog"):
        modes.add_argument("--" + mode, action="store_true")
    parser.add_argument("--revision")
    args = parser.parse_args()
    require(platform.system() == "Linux" and platform.node().split(".")[0] == "openmediavault" and os.geteuid() == 0,
            "run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    capture.managed_directory(RECOVERY)
    if args.recover:
        with journal():
            if (RECOVERY / "state.json").exists() and load("state.json")["phase"] == "complete":
                # Completed boot/ExecStopPost recovery must not contend with
                # the daily backups for a lock it no longer needs.
                return
    if args.watchdog:
        while not watchdog_tick():
            time.sleep(0.2)
        return
    if args.install:
        with capture.lock(RECOVERY / ".install.lock"):
            install(args.revision or "")
        return
    with capture.lock(capture.GLOBAL_LOCK), capture.lock(RECOVERY.parent / ".setup.lock"), capture.lock(RECOVERY / ".operation.lock"):
        if args.recover:
            recover()
        else:
            try:
                migrate()
            finally:
                # systemd's ExecStopPost also covers a killed Python process.
                recover()


if __name__ == "__main__":
    try:
        main()
    except (capture.CaptureError, inspection.InspectionError, OSError, ValueError, KeyError, IndexError, TypeError,
            AttributeError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, (capture.CaptureError, inspection.InspectionError)) else "filesystem, API, process, or SQLite check failed; private data omitted"
        print("GPT storage migration stopped: " + message, file=sys.stderr, flush=True)
        sys.exit(1)
