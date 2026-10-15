#!/usr/bin/env python3
"""Capture stopped monitoring writers, restart them, then upload stable artifacts."""

import argparse
from contextlib import ExitStack
import configparser
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
import stat
import subprocess
import sys
import tarfile
import time


sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location(
    "monitoring_setup", Path(__file__).with_name("prepare-monitoring.py"),
)
setup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(setup)
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "data"
STATE = setup.RECOVERY / "initial-backup.json"
COMPONENTS = tuple(setup.PROTECTED)
ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "metadata.json", "backup-id", "SHA256SUMS"}
PLACEHOLDER = "controlled-by-backup-runner"


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


def staging(component):
    return setup.BASE / "backup-staging" / setup.PROTECTED[component]["claim"]


def journal_path(component):
    return setup.RECOVERY / (component + "-quiescence.json")


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


def workload(component):
    settings = setup.PROTECTED[component]
    result = setup.get_object(settings["workload_kind"], settings["workload_name"], NS, sensitive=True)
    if not result:
        raise setup.SetupError("writer controller disappeared: " + component)
    return result


def controller_hash(resource):
    return fingerprint({key: value for key, value in resource["spec"].items() if key != "replicas"})


def operator(component):
    settings = setup.PROTECTED[component]
    if "operator_kind" not in settings:
        return None
    result = setup.get_object(settings["operator_kind"], settings["operator_name"], NS, sensitive=True)
    if not result:
        raise setup.SetupError("operator resource disappeared: " + component)
    return result


def operator_hash(resource):
    return fingerprint({key: value for key, value in resource["spec"].items() if key != "paused"})


def check_retention(resource):
    if resource["kind"] == "StatefulSet" and any(
            resource["spec"].get("persistentVolumeClaimRetentionPolicy", {}).get(key, "Retain") != "Retain"
            for key in ("whenScaled", "whenDeleted")):
        raise setup.SetupError("writer StatefulSet does not retain its PVCs")


def guarded_patch(kind, name, resource, operations):
    tests = [{"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": resource["metadata"]["resourceVersion"]}]
    setup.kube("patch", kind, name, "-n", NS, "--type=json", "-p", json.dumps(tests + operations),
               capture=True, sensitive=True)


def check_journal(state, component):
    if (state.get("component") != component or not valid_id(state.get("backup_id")) or
            state.get("original_replicas") != 1 or not state.get("workload_uid") or
            not state.get("claim_uid") or not state.get("volume_uid") or
            not re.fullmatch(r"[0-9a-f]{64}", state.get("controller_hash", "")) or
            not isinstance(state.get("pod_uids"), list) or len(state["pod_uids"]) != 1 or
            state.get("phase") not in ("quiescing", "captured", "resumed")):
        raise setup.SetupError("invalid durable writer journal: " + component)
    if "operator_kind" in setup.PROTECTED[component] and (
            not state.get("operator_uid") or not re.fullmatch(r"[0-9a-f]{64}", state.get("operator_hash", "")) or
            state.get("operator_original_paused") not in (None, False)):
        raise setup.SetupError("invalid durable operator journal: " + component)


def check_binding(component, state):
    settings = setup.PROTECTED[component]
    claim = setup.get_object("pvc", settings["claim"], NS)
    volume = setup.get_object("pv", settings["volume"])
    if (not claim or not volume or claim["metadata"]["uid"] != state["claim_uid"] or
            volume["metadata"]["uid"] != state["volume_uid"] or
            claim.get("status", {}).get("phase") != "Bound" or volume.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != settings["volume"] or
            volume["spec"].get("claimRef", {}).get("uid") != state["claim_uid"] or
            volume["spec"].get("local", {}).get("path") != str(setup.BASE / component) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("production PVC/PV identity or binding changed: " + component)
    setup.checked_directory(setup.BASE / component)


def checked_controller(component, state):
    check_binding(component, state)
    resource = workload(component)
    if (resource["metadata"]["uid"] != state["workload_uid"] or
            controller_hash(resource) != state["controller_hash"]):
        raise setup.SetupError("writer controller identity or specification changed: " + component)
    check_retention(resource)
    return resource


def checked_operator(component, state):
    resource = operator(component)
    if resource and (resource["metadata"]["uid"] != state["operator_uid"] or
                     operator_hash(resource) != state["operator_hash"]):
        raise setup.SetupError("operator resource identity or specification changed: " + component)
    return resource


def set_paused(component, state, paused):
    settings = setup.PROTECTED[component]
    for attempt in range(5):
        resource = checked_operator(component, state)
        target = True if paused else state["operator_original_paused"]
        if resource["spec"].get("paused") is target:
            return
        action = {"op": "add", "path": "/spec/paused", "value": target}
        if target is None:
            action = {"op": "remove", "path": "/spec/paused"}
        try:
            guarded_patch(settings["operator_kind"], settings["operator_name"], resource, [action])
            return
        except setup.SetupError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def set_replicas(component, state, target):
    settings = setup.PROTECTED[component]
    for attempt in range(5):
        resource = checked_controller(component, state)
        current = resource["spec"].get("replicas")
        if current == target:
            return
        if current not in (0, 1):
            raise setup.SetupError("writer replica count changed outside backup: " + component)
        try:
            guarded_patch(settings["workload_kind"], settings["workload_name"], resource,
                          [{"op": "replace", "path": "/spec/replicas", "value": target}])
            return
        except setup.SetupError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def wait_paused(component, state):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        resource = checked_operator(component, state)
        if resource["spec"].get("paused") is not True:
            raise setup.SetupError("operator pause was changed: " + component)
        if resource.get("status", {}).get("paused") is True:
            return
        time.sleep(2)
    raise setup.SetupError("operator did not acknowledge its pause: " + component)


def assert_stopped(component, state):
    resource = checked_controller(component, state)
    if resource["spec"].get("replicas") != 0:
        raise setup.SetupError("writer controller restarted during capture: " + component)
    resource = checked_operator(component, state)
    if resource and (resource["spec"].get("paused") is not True or resource.get("status", {}).get("paused") is not True):
        raise setup.SetupError("operator is no longer paused: " + component)
    if claim_pods(setup.PROTECTED[component]["claim"]):
        raise setup.SetupError("a Pod still holds the production PVC: " + component)
    for container in runtime_containers():
        labels = container.get("labels", {})
        if labels.get("io.kubernetes.pod.uid") in state["pod_uids"] or (
                labels.get("io.kubernetes.pod.namespace") == NS and container.get("metadata", {}).get("name") == component):
            raise setup.SetupError("a production writer is still running: " + component)


def stop_writer(component, state):
    if "operator_kind" in setup.PROTECTED[component]:
        set_paused(component, state, True)
        wait_paused(component, state)
    set_replicas(component, state, 0)
    deadline = time.monotonic() + 660
    while time.monotonic() < deadline:
        checked_controller(component, state)
        if not claim_pods(setup.PROTECTED[component]["claim"]):
            assert_stopped(component, state)
            print(component + " writer Pods and runtime containers stopped", flush=True)
            return
        time.sleep(2)
    raise setup.SetupError("production writer did not stop: " + component)


def resume_writer(component, state):
    check_journal(state, component)
    checked_controller(component, state)
    resource = checked_operator(component, state)
    if resource:
        # Its desired replica count stayed at one throughout the backup.
        set_paused(component, state, False)
    else:
        set_replicas(component, state, 1)
    settings = setup.PROTECTED[component]
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        resource = checked_controller(component, state)
        pods = claim_pods(settings["claim"])
        if resource["spec"].get("replicas") == 1 and len(pods) == 1 and setup.ready(pods[0]):
            current = next((item for item in pods[0].get("status", {}).get("containerStatuses", [])
                            if item["name"] == component), {})
            if current.get("imageID") != settings["image_id"] or pods[0]["spec"].get("nodeName") != "openmediavault":
                raise setup.SetupError("restarted writer image or placement changed: " + component)
            resource = checked_operator(component, state)
            if resource and (resource["spec"].get("paused", False) or resource.get("status", {}).get("paused", False)):
                time.sleep(2)
                continue
            save(journal_path(component), {**state, "phase": "resumed", "resumed_at": now()})
            print(component + " restarted and ready; capture downtime ended", flush=True)
            return
        time.sleep(2)
    raise setup.SetupError("writer recovery timed out; durable journal retained: " + component)


def recover_writers():
    for component in COMPONENTS:
        path = journal_path(component)
        if path.exists() or path.is_symlink():
            state = saved(path)
            check_journal(state, component)
            if state["phase"] != "resumed":
                resume_writer(component, state)


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
        raise setup.SetupError("expected exactly the four approved monitoring ReplicationSources")
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
    if {item.name for item in root.iterdir()} != {"current"}:
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
    if {path.name for path in pending.iterdir()} != ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("partial capture preserved for inspection: " + component)
    for path in pending.iterdir():
        attributes = path.lstat()
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
            raise setup.SetupError("pending artifact is not an independent regular file")
        os.chown(path, 999, 999, follow_symlinks=False)
        os.chmod(path, 0o600)
    checksums = "".join(digest_file(pending / name) + "  " + name + "\n"
                        for name in sorted(ARTIFACTS - {"SHA256SUMS"}))
    artifact_write(pending / "SHA256SUMS", checksums)
    os.chmod(pending, 0o700)
    sync_directory(pending)
    current = staging(component) / "current"
    if current.exists() or current.is_symlink():
        raise setup.SetupError("refusing to replace published artifacts: " + component)
    pending.rename(current)
    sync_directory(staging(component))
    inspect_artifacts(component, identifier)
    print("complete volume and recovery configuration published: " + component, flush=True)


def configuration(component, pod, original, original_operator):
    index = saved(setup.RECOVERY / "dependency-inventory.json")
    references = setup.dependency_references([pod])
    resources = {"secret": {}, "configmap": {}}
    selected = {"secret": {}, "configmap": {}}
    for kind, names in references.items():
        for name in names:
            record = index["resources"][kind][name]
            selected[kind][name] = record
            if not record["present"]:
                if not references[kind][name]["optional"] or record.get("saved_file") is not None:
                    raise setup.SetupError("invalid optional dependency record")
                continue
            filename = name + (".secret.json" if kind == "secret" else ".configmap.json")
            if record["saved_file"] != filename:
                raise setup.SetupError("unexpected saved dependency filename")
            resources[kind][name] = saved(setup.RECOVERY / filename)
    return {"version": 1, "namespace": NS, "component": component,
            "workload": setup.portable(original),
            "operator": setup.portable(original_operator) if original_operator else None,
            "dependencies": resources, "dependency_inventory": selected,
            "helmcharts": {name: saved(setup.RECOVERY / (name + ".helmchart.json"))
                           for name in ("kube-prometheus-stack", "loki", "alloy")}}


def unchanged_dependencies(config):
    for kind, records in config["dependency_inventory"].items():
        for name, record in records.items():
            resource = setup.get_object(kind, name, NS, sensitive=True)
            if record["present"]:
                if not resource or setup.portable(resource) != config["dependencies"][kind][name]:
                    raise setup.SetupError("configuration changed during capture; pending files preserved")
            elif resource:
                raise setup.SetupError("an optional dependency appeared during capture; pending files preserved")


def check_grafana_backend():
    resource = setup.get_object("configmap", "kube-prometheus-stack-grafana", NS, sensitive=True)
    if not resource:
        raise setup.SetupError("Grafana configuration disappeared")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(resource.get("data", {}).get("grafana.ini", ""))
    except configparser.Error:
        raise setup.SetupError("Grafana configuration could not be parsed; contents omitted") from None
    container = next(item for item in workload("grafana")["spec"]["template"]["spec"]["containers"]
                     if item["name"] == "grafana")
    if (parser.get("database", "type", fallback="sqlite3") != "sqlite3" or
            parser.get("database", "url", fallback="") or container.get("envFrom") or
            any(item["name"].startswith("GF_DATABASE_") for item in container.get("env", [])) or
            any("database." in argument for argument in container.get("args", []))):
        raise setup.SetupError("Grafana database backend differs from the verified local SQLite inventory")
    database = setup.BASE / "grafana/grafana.db"
    if database.is_symlink() or not database.is_file():
        raise setup.SetupError("Grafana SQLite database is missing or not a regular file")


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
    source = setup.BASE / component
    setup.checked_directory(source)
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


def capture(component, identifier):
    if inspect_artifacts(component, identifier):
        print("verified existing stopped-writer capture: " + component, flush=True)
        return
    ensure_source_idle(component, identifier)
    root = staging(component)
    if any(root.iterdir()):
        raise setup.SetupError("incomplete staging data preserved; writer is running: " + component)
    # Refuse to release reservations to any unknown scheduling backlog.
    pending_pods = json.loads(setup.kube("get", "pods", "-A", "--field-selector=status.phase=Pending",
                                          "-o", "json", capture=True).stdout)["items"]
    if any(not pod["spec"].get("nodeName") for pod in pending_pods):
        raise setup.SetupError("resolve unscheduled Pending Pods before stopping monitoring writers")
    hpas = json.loads(setup.kube("get", "hpa", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    if hpas:
        raise setup.SetupError("monitoring autoscalers require a reviewed capture procedure")
    original, original_operator = workload(component), operator(component)
    check_retention(original)
    if original["spec"].get("replicas") != 1 or (original_operator and original_operator["spec"].get("paused", False)):
        raise setup.SetupError("writer replica count or pause changed before capture")
    owners = [ref for ref in original["metadata"].get("ownerReferences", []) if ref.get("controller")]
    if original_operator:
        if len(owners) != 1 or owners[0].get("uid") != original_operator["metadata"]["uid"]:
            raise setup.SetupError("writer is not controlled by the expected operator resource")
    elif owners:
        raise setup.SetupError("unexpected owner controls the writer")
    pods = claim_pods(setup.PROTECTED[component]["claim"])
    if len(pods) != 1 or not setup.ready(pods[0]):
        raise setup.SetupError("expected one ready production PVC holder before capture")
    runtime_containers()
    if component == "grafana":
        check_grafana_backend()
    config = configuration(component, pods[0], original, original_operator)
    unchanged_dependencies(config)
    state = {"backup_id": identifier, "component": component, "phase": "quiescing",
             "original_replicas": 1, "workload_uid": original["metadata"]["uid"],
             "controller_hash": controller_hash(original), "pod_uids": [pods[0]["metadata"]["uid"]],
             "claim_uid": saved(setup.RECOVERY / (setup.PROTECTED[component]["claim"] + ".pvc.json"))["metadata"]["uid"],
             "volume_uid": saved(setup.RECOVERY / (setup.PROTECTED[component]["volume"] + ".pv.json"))["metadata"]["uid"],
             "started_at": now()}
    if original_operator:
        state.update({"operator_uid": original_operator["metadata"]["uid"],
                      "operator_hash": operator_hash(original_operator),
                      "operator_original_paused": original_operator["spec"].get("paused")})
    pending = root / (".pending-" + identifier)
    pending.mkdir(mode=0o700)
    os.chown(pending, 999, 999)
    os.chmod(pending, 0o700)  # Clear SGID inherited from the staging directory.
    sync_directory(root)
    save(journal_path(component), state)
    try:
        print("stopping " + component + " for a complete volume capture", flush=True)
        stop_writer(component, state)
        started = now()
        contents = archive_volume(component, state, pending / "volume.tar")
        unchanged_dependencies(config)
        artifact_write(pending / "contents.json", json.dumps(contents, indent=2) + "\n")
        artifact_write(pending / "configuration.json", json.dumps(config, indent=2) + "\n")
        settings = setup.PROTECTED[component]
        artifact_write(pending / "metadata.json", json.dumps({
            "backup_id": identifier, "component": component, "claim": settings["claim"],
            "image_id": settings["image_id"], "mount": settings["mount"], "sub_path": settings["sub_path"],
            "captured_at": started, "completed_at": now(), "method": "stopped writers; complete PVC including WAL/head",
            "files": sum(item["type"] == "file" for item in contents),
            "file_bytes": sum(item.get("bytes", 0) for item in contents),
        }, indent=2) + "\n")
        artifact_write(pending / "backup-id", identifier + "\n")
        sync_directory(pending)
        save(journal_path(component), {**state, "phase": "captured", "captured_at": now()})
    finally:
        handlers = {number: signal.signal(number, signal.SIG_IGN)
                    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            resume_writer(component, state)
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
    # Rehash and upload after service recovery, keeping downtime to the copy.
    publish(component, identifier, pending)


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


def upload(component, identifier):
    if not inspect_artifacts(component, identifier):
        raise setup.SetupError("cannot upload without complete published artifacts")
    worker_budget()
    source = setup.get_object("replicationsource", component + "-backup", NS)
    if not source or source["spec"].get("trigger", {}).get("manual") not in (PLACEHOLDER, identifier):
        raise setup.SetupError("upload source generation changed")
    if source["spec"].get("paused"):
        ensure_source_idle(component, identifier)
        guarded_patch("replicationsource", component + "-backup", source,
                      [{"op": "replace", "path": "/spec/paused", "value": False},
                       {"op": "replace", "path": "/spec/trigger", "value": {"manual": identifier}}])
    deadline, last_message = time.monotonic() + 7200, 0
    while time.monotonic() < deadline:
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if not source or source["spec"].get("trigger") != {"manual": identifier}:
            raise setup.SetupError("upload generation changed while synchronizing")
        if upload_complete(source, identifier):
            save(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"), source)
            print(component + " VolSync upload, retention, and cleanup completed", flush=True)
            return
        if time.monotonic() - last_message >= 60:
            print("waiting for " + component + " VolSync upload and cleanup", flush=True)
            last_message = time.monotonic()
        time.sleep(5)
    raise setup.SetupError("upload did not complete within two hours; published capture retained")


def verify_backup(component, identifier):
    checksums = inspect_artifacts(component, identifier)
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
    }.items()]
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    print("job/" + name + " created (" + component + " full snapshot verification)", flush=True)
    try:
        setup.wait_for_job(name, timeout=7260)
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
    save(setup.RECOVERY / ("initial-" + component + "-backup-result.json"), result)


def service_health(component):
    service, port, path = {
        "prometheus": ("kube-prometheus-stack-prometheus", "http-web", "/-/ready"),
        "alertmanager": ("kube-prometheus-stack-alertmanager", "http-web", "/-/ready"),
        "grafana": ("kube-prometheus-stack-grafana", "http-web", "/api/health"),
        "loki": ("loki", "http-metrics", "/ready"),
    }[component]
    response = setup.kube("get", "--raw", "/api/v1/namespaces/monitoring/services/http:" +
                          service + ":" + port + "/proxy" + path, capture=True, sensitive=True, timeout=40).stdout
    if component == "grafana" and json.loads(response).get("database") != "ok":
        raise setup.SetupError("Grafana database health check failed")
    print(component + " application health passed", flush=True)


def preflight():
    proof = saved(setup.RECOVERY / "endpoint-verification.json")
    if proof.get("result") != "passed" or proof.get("repositories") != list(setup.CLAIMS):
        raise setup.SetupError("the four backup repositories have not been verified")
    identities = setup.live_preflight()
    server = setup.get_object("deployment", "restic-server", NS)
    if not server or server.get("status", {}).get("availableReplicas") != 1:
        raise setup.SetupError("backup endpoint is not available")
    active = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "backups.mousses.xyz/restic-client=true",
                                   "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in active):
        raise setup.SetupError("a previous monitoring repository worker is still running")
    credentials = saved(setup.RECOVERY / "credentials.json")
    for component in COMPONENTS:
        settings = setup.PROTECTED[component]
        setup.ensure_secret("restic-" + settings["claim"],
                            setup.credential_fields(settings["claim"], credentials["repositories"][settings["claim"]]))
        setup.save_secret("restic-" + settings["claim"])
        claim = setup.get_object("pvc", component + "-backup-staging", NS)
        volume = setup.get_object("pv", "monitoring-" + component + "-backup-staging")
        if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != volume["metadata"]["name"] or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("local", {}).get("path") != str(staging(component)) or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
            raise setup.SetupError("backup staging binding changed: " + component)
        setup.checked_directory(staging(component))
        if shutil.disk_usage(staging(component)).free < 100 * 1024**3:
            raise setup.SetupError("less than 100 GiB remains on the backup array")
        check_retention(workload(component))
        service_health(component)
    check_grafana_backend()
    setup.save_secret("restic-root-ca")
    return identities


def backup():
    recover_writers()
    before = preflight()
    if not STATE.exists() and not STATE.is_symlink():
        save(STATE, {"backup_id": "initial-" + secrets.token_hex(16), "started_at": now()})
    identifier = saved(STATE).get("backup_id")
    if not valid_id(identifier):
        raise setup.SetupError("invalid saved initial backup generation")
    apply_sources(identifier)
    for component in COMPONENTS:
        capture(component, identifier)
        service_health(component)
        after = setup.live_preflight(check_resources=False)
        for name in COMPONENTS:
            for key in ("claim_uid", "volume_uid", "workload_uid"):
                if before[name][key] != after[name][key]:
                    raise setup.SetupError("production binding or controller was replaced during backup")
        upload(component, identifier)
        verify_backup(component, identifier)
    setup.live_preflight(check_resources=False)
    save(setup.RECOVERY / "initial-backup-verification.json", {
        "result": "passed", "backup_id": identifier, "completed_at": now(),
        "components": {component: saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json"))
                       for component in COMPONENTS},
    })
    print("all four initial monitoring backups verified; isolated restores, daily scheduling, and independent credential storage remain pending", flush=True)


def interrupted(number, frame):
    raise setup.SetupError("backup interrupted by signal " + str(number))


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
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                try:
                    setup.kube("get", "namespace", NS, "--request-timeout=5s", capture=True, sensitive=True, timeout=10)
                    break
                except (setup.SetupError, subprocess.TimeoutExpired):
                    time.sleep(5)
            else:
                raise setup.SetupError("API did not become available for writer recovery")
            recover_writers()
        else:
            locks.enter_context(setup.operation_lock(setup.COORDINATION_LOCK, create=False))
            print("monitoring backup worker serialized with board-games and SearXNG", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("monitoring backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
