#!/usr/bin/env python3
"""Capture stable SearXNG/Valkey artifacts, upload with VolSync, and verify them."""

from datetime import datetime, timezone
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
import signal
import stat
import subprocess
import sys
import tarfile
import time


module_spec = importlib.util.spec_from_file_location(
    "searxng_setup", Path(__file__).with_name("prepare-searxng.py"),
)
setup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(setup)
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "data"
STATE = setup.RECOVERY / "initial-backup.json"
QUIESCENCE = setup.RECOVERY / "cache-quiescence.json"
NATIVE_JOB = "valkey-native-backup-initial"
ARTIFACTS = {
    "cache": {"cache.tar", "contents.json", "application-config.json", "application-secret.json",
              "metadata.json", "backup-id", "SHA256SUMS"},
    "valkey": {"dump.rdb", "rdb-check.txt", "transfer.log", "metadata.json", "backup-id", "SHA256SUMS"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    setup.private_write(path, json.dumps(value, indent=2) + "\n")


def saved(path):
    return json.loads(setup.private_read(path))


def template_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_name(component):
    return component + "-backup"


def staging(component):
    return setup.BASE / "backup-staging" / ("searxng-" + component)


def managed(resource):
    if resource["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER:
        raise setup.SetupError("resource is not owned by this backup setup: " + resource["metadata"]["name"])


def contains_fields(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains_fields(actual[key], value)
                                                for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains_fields(left, right) for left, right in zip(actual, expected))
    return actual == expected


def read_template(filename):
    return json.loads(setup.kube("create", "--dry-run=client", "-f", str(filename), "-o", "json",
                                 capture=True).stdout)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest_file(path):
    attributes = path.lstat()
    if not stat.S_ISREG(attributes.st_mode):
        raise setup.SetupError("backup artifact is not a regular file")
    checksum = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_ino, opened.st_dev) != (attributes.st_ino, attributes.st_dev):
            raise setup.SetupError("backup artifact changed while opening it")
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(source.fileno())
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise setup.SetupError("backup artifact changed while hashing it")
    return checksum.hexdigest()


def artifact_write(path, value):
    # Pending generation directories are private, validated, and never uploaded.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
        os.fchown(output.fileno(), 999, 999)


def inspect_artifacts(component, identifier):
    root = staging(component)
    setup.checked_directory(root)
    current = root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if {path.name for path in root.iterdir()} != {"current"}:
        raise setup.SetupError("unexpected staging data exists beside the published backup: " + component)
    setup.checked_directory(current)
    attributes = current.stat()
    if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o700):
        raise setup.SetupError("published backup directory ownership or mode changed: " + component)
    if {path.name for path in current.iterdir()} != ARTIFACTS[component]:
        raise setup.SetupError("published backup artifact set is incomplete: " + component)
    for path in current.iterdir():
        attributes = path.lstat()
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600)):
            raise setup.SetupError("published artifact ownership, mode, or file type changed: " + component)
    if (current / "backup-id").read_text().strip() != identifier:
        raise setup.SetupError("published artifacts belong to a different generation: " + component)
    metadata = json.loads((current / "metadata.json").read_text())
    if metadata.get("backup_id") != identifier or metadata.get("claim") != "searxng-" + component:
        raise setup.SetupError("published metadata does not match its claim and generation")
    checksums = (current / "SHA256SUMS").read_text()
    lines = checksums.splitlines()
    names = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        if not match or match[2] not in ARTIFACTS[component] - {"SHA256SUMS"}:
            raise setup.SetupError("published checksum manifest is invalid")
        names.append(match[2])
        if digest_file(current / match[2]) != match[1]:
            raise setup.SetupError("published backup artifact checksum failed: " + component)
    if len(names) != len(set(names)) or set(names) != ARTIFACTS[component] - {"SHA256SUMS"}:
        raise setup.SetupError("published checksum manifest is incomplete")
    return checksums


def active_claim_pods(claim):
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")
            and any(volume.get("persistentVolumeClaim", {}).get("claimName") == claim
                    for volume in pod["spec"].get("volumes", []))]


def ensure_source_idle(component, identifier):
    source = setup.get_object("replicationsource", source_name(component), NS)
    if (not source or not source["spec"].get("paused") or
            source["spec"].get("trigger", {}).get("manual") not in ("controlled-by-backup-runner", identifier)):
        raise setup.SetupError("staging may only change while its VolSync source is paused: " + component)
    if active_claim_pods(component + "-backup-staging"):
        raise setup.SetupError("a Pod is still using backup staging: " + component)


def worker_budget():
    node = setup.get_object("node", "openmediavault")
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    setup.resource_budget(node, pods)


def new_pending(component, identifier):
    ensure_source_idle(component, identifier)
    root = staging(component)
    setup.checked_directory(root)
    if any(root.iterdir()):
        raise setup.SetupError("unexpected or partial staging data is preserved: " + component)
    path = root / (".pending-" + identifier)
    path.mkdir(mode=0o700)
    os.chown(path, 999, 999)
    sync_directory(root)
    return path


def publish(component, identifier, pending):
    ensure_source_idle(component, identifier)
    if {path.name for path in pending.iterdir()} != ARTIFACTS[component] - {"SHA256SUMS"}:
        raise setup.SetupError("pending backup artifacts are incomplete: " + component)
    for path in pending.iterdir():
        attributes = path.lstat()
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
            raise setup.SetupError("pending backup artifact has an unexpected file type or hard link")
        os.chown(path, 999, 999, follow_symlinks=False)
        os.chmod(path, 0o600)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    checksums = "".join(digest_file(pending / name) + "  " + name + "\n"
                        for name in sorted(ARTIFACTS[component] - {"SHA256SUMS"}))
    artifact_write(pending / "SHA256SUMS", checksums)
    sync_directory(pending)
    current = staging(component) / "current"
    if current.exists() or current.is_symlink():
        raise setup.SetupError("refusing to replace a published backup generation")
    pending.rename(current)
    sync_directory(staging(component))
    inspect_artifacts(component, identifier)
    print("consistent artifacts published and checksummed: searxng-" + component, flush=True)


def apply_sources(identifier):
    rendered = setup.kube("create", "--dry-run=client", "-k", str(MANIFESTS), "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if len(resources) != 4:
        raise setup.SetupError("unexpected source/NetworkPolicy bundle")
    for resource in resources:
        kind = resource["kind"].lower() + "." + resource["apiVersion"].split("/")[0]
        existing = setup.get_object(kind, resource["metadata"]["name"], NS)
        if existing:
            managed(existing)
            if resource["kind"] == "ReplicationSource":
                expected = {key: value for key, value in resource["spec"].items() if key not in ("paused", "trigger")}
                if (not contains_fields(existing["spec"], expected) or
                        existing["spec"].get("trigger") not in ({"manual": "controlled-by-backup-runner"}, {"manual": identifier})):
                    raise setup.SetupError("existing VolSync policy or generation differs")
                if (not existing["spec"].get("paused") and
                        existing["spec"]["trigger"]["manual"] == "controlled-by-backup-runner"):
                    raise setup.SetupError("an initial source was unexpectedly unpaused")
                continue  # Preserve an upload already started by this generation.
        setup.kube("apply", "-f", "-", stdin=json.dumps(resource))


def valkey_service_ip():
    pod = setup.get_object("pod", "valkey-0", NS)
    service = setup.get_object("service", "valkey", NS)
    if not pod or not setup.ready(pod) or not service:
        raise setup.SetupError("production Valkey Pod or Service is unavailable")
    if (service["spec"].get("selector", {}).get("app.kubernetes.io/name") != "valkey" or
            not any(port.get("port") == 6379 for port in service["spec"].get("ports", []))):
        raise setup.SetupError("Valkey Service selector or port changed")
    slices = json.loads(setup.kube("get", "endpointslices", "-n", NS,
                                   "-l", "kubernetes.io/service-name=valkey", "-o", "json", capture=True).stdout)["items"]
    endpoints = [entry for item in slices for entry in item.get("endpoints", [])
                 if entry.get("conditions", {}).get("ready") is not False]
    if not endpoints or any(entry.get("targetRef", {}).get("uid") != pod["metadata"]["uid"] for entry in endpoints):
        raise setup.SetupError("Valkey Service does not point exclusively to the verified Pod")
    return str(ipaddress.ip_address(service["spec"]["clusterIP"]))


def capture_valkey(identifier):
    if inspect_artifacts("valkey", identifier):
        return
    job = read_template(MANIFESTS / "valkey-dump-job.yaml")
    job["metadata"].setdefault("annotations", {})["backups.mousses.xyz/generation"] = identifier
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [{"name": "BACKUP_ID", "value": identifier},
                        {"name": "VALKEY_HOST", "value": valkey_service_ip()}]
    existing = setup.get_object("job", NATIVE_JOB, NS)
    pending = staging("valkey") / (".pending-" + identifier)
    if existing:
        managed(existing)
        if (existing["metadata"].get("annotations", {}).get("backups.mousses.xyz/generation") != identifier or
                not contains_fields(existing["spec"]["template"]["spec"], job["spec"]["template"]["spec"])):
            raise setup.SetupError("existing Valkey dump Job differs from this generation")
        setup.checked_directory(pending)
        if not pending.is_dir():
            raise setup.SetupError("existing native dump Job has no matching pending directory")
    else:
        worker_budget()
        pending = new_pending("valkey", identifier)
        setup.kube("create", "-f", "-", stdin=json.dumps(job))
    try:
        setup.wait_for_job(NATIVE_JOB)
    except (setup.SetupError, subprocess.TimeoutExpired):
        logs = setup.kube("logs", "-n", NS, "job/" + NATIVE_JOB, "--pod-running-timeout=5s",
                          capture=True, sensitive=True, check=False, timeout=15)
        setup.private_write(setup.RECOVERY / "initial-valkey-dump.log", logs.stdout + logs.stderr)
        raise
    if {path.name for path in pending.iterdir()} != {"dump.rdb", "transfer.log", "rdb-check.txt"}:
        raise setup.SetupError("native dump has partial or unexpected files; preserving them")
    if (pending / "dump.rdb").stat().st_size == 0:
        raise setup.SetupError("native Valkey RDB is empty")
    with (pending / "dump.rdb").open("rb") as source:
        header = source.read(9)
    # Valkey and Redis use different magic strings; the image's own checker
    # validates the complete format and checksum before the Job succeeds.
    if not re.fullmatch(rb"(?:REDIS0|VALKEY)[0-9]{3}", header):
        raise setup.SetupError("native Valkey dump has no RDB header")
    artifact_write(pending / "metadata.json", json.dumps({
        "backup_id": identifier, "claim": "searxng-valkey", "captured_at": now(),
        "method": "valkey-cli --rdb; valkey-check-rdb", "image": setup.IMAGES["valkey"],
        "bytes": (pending / "dump.rdb").stat().st_size,
        "expiry": "absolute key expiration times are preserved; keys may expire before restore",
    }, indent=2) + "\n")
    artifact_write(pending / "backup-id", identifier + "\n")
    publish("valkey", identifier, pending)


def check_quiescence_state(state, identifier=None):
    if (not isinstance(state, dict) or not re.fullmatch(r"initial-[0-9a-f]{32}", state.get("backup_id", "")) or
            (identifier is not None and state["backup_id"] != identifier) or
            state.get("original_replicas") != 1 or not state.get("deployment_uid") or
            not re.fullmatch(r"[0-9a-f]{64}", state.get("template_hash", "")) or
            state.get("phase") not in ("quiescing", "captured", "resumed")):
        raise setup.SetupError("saved cache quiescence state is invalid")


def resume_searxng(state):
    check_quiescence_state(state)
    deployment = setup.get_object("deployment", "searxng", NS)
    if (not deployment or deployment["metadata"]["uid"] != state["deployment_uid"] or
            template_hash(deployment["spec"]["template"]) != state["template_hash"]):
        raise setup.SetupError("SearXNG was replaced or its Pod template changed; inspect the saved quiescence journal")
    replicas = deployment["spec"].get("replicas")
    if replicas == 0:
        setup.kube("scale", "deployment/searxng", "-n", NS, "--replicas=1", "--current-replicas=0",
                   "--resource-version=" + deployment["metadata"]["resourceVersion"])
    elif replicas != 1:
        raise setup.SetupError("SearXNG replica count changed outside the backup; preserving it")
    setup.kube("rollout", "status", "deployment/searxng", "-n", NS, "--timeout=180s")
    pods = active_claim_pods("searxng-cache")
    if len(pods) != 1 or not setup.ready(pods[0]):
        raise setup.SetupError("SearXNG did not become ready after cache capture")
    state = {**state, "phase": "resumed", "resumed_at": now()}
    save(QUIESCENCE, state)
    print("SearXNG restarted and ready; cache capture downtime ended", flush=True)


def running_containers():
    return json.loads(setup.run(["k3s", "crictl", "ps", "-o", "json"],
                                capture=True, sensitive=True, timeout=30).stdout)["containers"]


def stopped_cache_writers(old_uids):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pods = active_claim_pods("searxng-cache")
        if not pods:
            containers = running_containers()
            if not any(container.get("labels", {}).get("io.kubernetes.pod.uid") in old_uids or
                       (container.get("labels", {}).get("io.kubernetes.pod.namespace") == NS and
                        container.get("metadata", {}).get("name") == "searxng") for container in containers):
                return
        time.sleep(2)
    raise setup.SetupError("cache writers did not stop; no cache archive was created")


class HashingReader:
    def __init__(self, source):
        self.source = source
        self.checksum = hashlib.sha256()

    def read(self, count):
        data = self.source.read(count)
        self.checksum.update(data)
        return data


def cache_archive(source, destination):
    """Archive regular files and directories in one pass, without following links."""
    contents = []
    device = source.stat().st_dev
    with tarfile.open(destination, "x", format=tarfile.PAX_FORMAT, dereference=False) as archive:
        def visit(path, relative):
            before = path.lstat()
            if before.st_dev != device or (not stat.S_ISDIR(before.st_mode) and not stat.S_ISREG(before.st_mode)):
                raise setup.SetupError("cache contains a link, special file, or nested filesystem; preserving it")
            entry = {"path": relative, "uid": before.st_uid, "gid": before.st_gid,
                     "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns}
            if stat.S_ISDIR(before.st_mode):
                names = sorted(item.name for item in path.iterdir())
                information = archive.gettarinfo(str(path), arcname=relative)
                information.uname = information.gname = ""
                archive.addfile(information)
                entry["type"] = "directory"
                contents.append(entry)
                for name in names:
                    visit(path / name, name if relative == "." else relative + "/" + name)
                after = path.lstat()
                if (sorted(item.name for item in path.iterdir()) != names or
                        (after.st_ino, after.st_dev, after.st_mtime_ns, after.st_ctime_ns) != (
                            before.st_ino, before.st_dev, before.st_mtime_ns, before.st_ctime_ns)):
                    raise setup.SetupError("cache directory changed while quiesced")
            else:
                if before.st_nlink != 1:
                    raise setup.SetupError("cache contains a hard-linked file; preserving it")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    attributes = os.fstat(stream.fileno())
                    if (attributes.st_ino, attributes.st_dev) != (before.st_ino, before.st_dev):
                        raise setup.SetupError("cache file changed while opening it")
                    reader = HashingReader(stream)
                    information = archive.gettarinfo(fileobj=stream, arcname=relative)
                    information.uname = information.gname = ""
                    archive.addfile(information, reader)
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                            before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                        raise setup.SetupError("cache file changed while quiesced")
                after = path.lstat()
                if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                        before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                    raise setup.SetupError("cache file was replaced while quiesced")
                entry.update({"type": "file", "bytes": before.st_size, "sha256": reader.checksum.hexdigest()})
                contents.append(entry)
        visit(source, ".")
    return contents


def capture_cache(identifier):
    if inspect_artifacts("cache", identifier):
        return
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        previous = saved(QUIESCENCE)
        check_quiescence_state(previous, identifier)
        # A prior incomplete capture is preserved for inspection, never overwritten.
        if any(staging("cache").iterdir()):
            raise setup.SetupError("an interrupted cache capture is preserved; SearXNG is running")
    hpas = json.loads(setup.kube("get", "hpa", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    if any(item["spec"].get("scaleTargetRef", {}).get("name") == "searxng" for item in hpas):
        raise setup.SetupError("an autoscaler may restart cache writers; no quiescence attempted")
    # Do not release the app's reservations into an existing scheduling backlog.
    node = setup.get_object("node", "openmediavault")
    pending_pods = json.loads(setup.kube("get", "pods", "-A", "--field-selector=status.phase=Pending",
                                       "-o", "json", capture=True).stdout)["items"]
    candidates = [pod for pod in pending_pods if not pod["spec"].get("nodeName")
                  and not pod["metadata"].get("deletionTimestamp")
                  and all(node["metadata"].get("labels", {}).get(key) == value
                          for key, value in pod["spec"].get("nodeSelector", {}).items())]
    if candidates:
        names = [pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"] for pod in candidates]
        print("pending Pods may compete for SearXNG's reservations: " + ", ".join(names), flush=True)
        raise setup.SetupError("resolve the scheduling backlog before stopping SearXNG")
    running_containers()  # Verify runtime inspection works before stopping the app.
    pods = active_claim_pods("searxng-cache")
    if len(pods) != 1 or not setup.ready(pods[0]):
        raise setup.SetupError("expected one ready cache writer before quiescence")
    deployment = setup.get_object("deployment", "searxng", NS)
    if deployment["spec"].get("replicas") != 1:
        raise setup.SetupError("SearXNG replica count changed before quiescence")
    state = {"backup_id": identifier, "phase": "quiescing", "original_replicas": 1,
             "deployment_uid": deployment["metadata"]["uid"],
             "template_hash": template_hash(deployment["spec"]["template"]), "started_at": now()}
    pending = new_pending("cache", identifier)
    save(QUIESCENCE, state)
    try:
        print("briefly stopping SearXNG to capture its cache", flush=True)
        setup.kube("scale", "deployment/searxng", "-n", NS, "--replicas=0", "--current-replicas=1",
                   "--resource-version=" + deployment["metadata"]["resourceVersion"])
        stopped_cache_writers({pod["metadata"]["uid"] for pod in pods})
        setup.checked_directory(setup.BASE / "cache")
        started = now()
        contents = cache_archive(setup.BASE / "cache", pending / "cache.tar")
        artifact_write(pending / "contents.json", json.dumps(contents, indent=2) + "\n")
        secret = setup.save_secret("searxng-secret")
        config = setup.get_object("configmap", "searxng-config", NS, sensitive=True)
        if not config:
            raise setup.SetupError("SearXNG recovery ConfigMap disappeared")
        artifact_write(pending / "application-secret.json", json.dumps(setup.portable(secret), indent=2) + "\n")
        artifact_write(pending / "application-config.json", json.dumps(setup.portable(config), indent=2) + "\n")
        artifact_write(pending / "metadata.json", json.dumps({
            "backup_id": identifier, "claim": "searxng-cache", "captured_at": started,
            "completed_at": now(), "method": "stopped writer; archive and per-file SHA-256",
            "image": setup.IMAGES["searxng"],
            "file_count": sum(entry["type"] == "file" for entry in contents),
        }, indent=2) + "\n")
        artifact_write(pending / "backup-id", identifier + "\n")
        publish("cache", identifier, pending)
        save(QUIESCENCE, {**state, "phase": "captured", "captured_at": now()})
    finally:
        # Repeated disconnect/termination signals must not interrupt recovery.
        previous_handlers = {number: signal.signal(number, signal.SIG_IGN)
                             for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            resume_searxng(state)
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)


def upload_complete(source, identifier):
    status = source.get("status", {})
    return (not source["spec"].get("paused") and source["spec"].get("trigger") == {"manual": identifier} and
            status.get("lastManualSync") == identifier and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(item.get("type") == "Synchronizing" and item.get("status") == "False" and
                item.get("reason") == "WaitingForManual" for item in status.get("conditions", [])))


def upload(component, identifier):
    if not inspect_artifacts(component, identifier):
        raise setup.SetupError("cannot upload without published artifacts")
    for attempt in range(5):
        source = setup.get_object("replicationsource", source_name(component), NS)
        if not source or source["spec"].get("trigger") not in ({"manual": "controlled-by-backup-runner"}, {"manual": identifier}):
            raise setup.SetupError("VolSync source or generation changed before upload")
        if not source["spec"].get("paused"):
            break
        if active_claim_pods(component + "-backup-staging"):
            raise setup.SetupError("a producer still holds staging; no upload started")
        worker_budget()
        result = setup.kube("patch", "replicationsource", source_name(component), "-n", NS, "--type=merge", "-p",
                            json.dumps({"metadata": {"resourceVersion": source["metadata"]["resourceVersion"]},
                                        "spec": {"paused": False, "trigger": {"manual": identifier}}}),
                            capture=True, check=False)
        if not result.returncode:
            print(result.stdout, end="", flush=True)
            break
        if "(Conflict)" not in result.stderr:
            raise setup.SetupError("could not trigger the VolSync upload")
        time.sleep(0.2)
    else:
        raise setup.SetupError("VolSync source kept changing; staging is preserved")
    deadline = time.monotonic() + 630
    while time.monotonic() < deadline:
        source = setup.get_object("replicationsource", source_name(component), NS)
        if not source or source["spec"].get("trigger") != {"manual": identifier}:
            raise setup.SetupError("VolSync source or generation changed during upload")
        if upload_complete(source, identifier):
            save(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"), source)
            print(component + " VolSync upload, retention, and cleanup completed", flush=True)
            return
        time.sleep(2)
    public = {key: source.get("status", {}).get(key) for key in (
        "conditions", "lastManualSync", "lastSyncTime", "lastSyncStartTime", "latestMoverStatus")}
    print(json.dumps(public, indent=2), flush=True)
    raise setup.SetupError("VolSync did not complete within 630 seconds; published files are preserved")


def verify_backup(component, identifier):
    checksums = inspect_artifacts(component, identifier)
    if not checksums:
        raise setup.SetupError("cannot verify without published artifacts")
    worker_budget()
    service = setup.get_object("service", "restic", NS)
    ips = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    job = read_template(MANIFESTS / "verify-job.yaml")
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": "restic-searxng-" + component}}]
    container["env"] = [{"name": key, "value": value} for key, value in {
        "EXPECTED_RESTIC_IPS": ",".join(str(ipaddress.ip_address(value)) for value in ips),
        "EXPECTED_BACKUP_ID": identifier, "EXPECTED_SHA256SUMS": checksums,
        "EXPECTED_ARTIFACTS": json.dumps(sorted(ARTIFACTS[component])),
        "PROTECTED_CLAIM": "searxng-" + component,
    }.items()]
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    print("job/" + name + " created (" + component + " snapshot verification)", flush=True)
    try:
        setup.wait_for_job(name, timeout=630)
    finally:
        setup.print_check_logs(name)
    logs = setup.kube("logs", "-n", NS, "job/" + name, capture=True, sensitive=True).stdout
    results = [json.loads(line.removeprefix("BACKUP_VERIFIED_JSON=")) for line in logs.splitlines()
               if line.startswith("BACKUP_VERIFIED_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or
            results[0].get("backup_id") != identifier or results[0].get("claim") != "searxng-" + component or
            not re.fullmatch(r"[0-9a-f]{64}", results[0].get("snapshot", ""))):
        raise setup.SetupError("completed verification Job did not report the expected backup")
    result = {**results[0], "completed_at": now(), "checksums": checksums, "verification_job": name}
    save(setup.RECOVERY / ("initial-" + component + "-backup-result.json"), result)


def preflight():
    proof = saved(setup.RECOVERY / "endpoint-verification.json")
    if proof.get("result") != "passed" or proof.get("repositories") != list(setup.CLAIMS):
        raise setup.SetupError("the two backup endpoints have not been verified")
    setup.live_preflight(check_resources=False)
    server = setup.get_object("deployment", "restic-server", NS)
    if not server or server.get("status", {}).get("availableReplicas", 0) != 1:
        raise setup.SetupError("the backup endpoint is not available")
    credentials = saved(setup.RECOVERY / "credentials.json")
    for claim in setup.CLAIMS:
        setup.ensure_secret("restic-" + claim, setup.credential_fields(claim, credentials["repositories"][claim]))
        setup.save_secret("restic-" + claim)
    setup.save_secret("restic-root-ca")
    for component in ARTIFACTS:
        claim = setup.get_object("pvc", component + "-backup-staging", NS)
        volume = setup.get_object("pv", "searxng-" + component + "-backup-staging")
        if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != volume["metadata"]["name"] or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("local", {}).get("path") != str(staging(component)) or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
            raise setup.SetupError("backup staging binding changed: " + component)
        for path in (staging(component), setup.BASE / "backups" / ("searxng-" + component),
                     setup.BASE / "backup-cache" / ("searxng-" + component) / "source"):
            setup.checked_directory(path)
            if not path.is_dir():
                raise setup.SetupError("an approved backup directory is missing")
        if shutil.disk_usage(staging(component)).free < 20 * 1024**3:
            raise setup.SetupError("less than 20 GiB remains on the backup array")


def backup():
    # A durable journal permits restoring the writer after SIGKILL or host loss.
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        state = saved(QUIESCENCE)
        check_quiescence_state(state)
        if state["phase"] != "resumed":
            resume_searxng(state)
    preflight()
    if not STATE.exists() and not STATE.is_symlink():
        save(STATE, {"backup_id": "initial-" + secrets.token_hex(16), "started_at": now()})
    identifier = saved(STATE).get("backup_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise setup.SetupError("saved initial backup generation is invalid")
    apply_sources(identifier)
    capture_valkey(identifier)
    capture_cache(identifier)
    for component in ("valkey", "cache"):
        upload(component, identifier)
        verify_backup(component, identifier)
    setup.live_preflight(check_resources=False)
    print("both searxng initial backups verified; isolated restores, daily scheduling, and independent credential storage remain pending", flush=True)


def interrupted(number, frame):
    raise setup.SetupError("backup interrupted by signal " + str(number))


def main():
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    descriptor = os.open(setup.RECOVERY / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise setup.SetupError("another searxng backup operation holds the namespace lock") from error
        backup()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
