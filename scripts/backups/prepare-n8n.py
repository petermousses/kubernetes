#!/usr/bin/env python3
"""Prepare n8n's two isolated array-backed Restic repositories after data separation."""

import base64
from contextlib import ExitStack
from decimal import Decimal
import fcntl
import hashlib
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
import tempfile
import time

NAMESPACE = "n8n"
MANAGER = "nas-backup-migration"
BASE = Path("/filesystem/k3s/data/n8n")
RECOVERY = Path("/root/k3s-backup-recovery/n8n")
COORDINATION_LOCK = Path("/root/k3s-backup-recovery/board-games/data-postgres-0/.setup.lock")
MANIFESTS = Path(__file__).resolve().parents[2] / "apps/n8n/backups"
IMAGE = "docker.n8n.io/n8nio/n8n@sha256:3989d9b8ebb77b4ee8f604519eb73e44f4384bfaa689526e0104eed79a237d30"
ORIGINAL_REQUESTS = {"cpu": "250m", "memory": "512Mi"}
TARGET_REQUESTS = {"cpu": "225m", "memory": "480Mi"}
LIMITS = {"cpu": "2", "memory": "1Gi"}
PROTECTED = {
    "data": {"claim": "n8n-data-live", "volume": "n8n-data-live", "size": "10Gi",
             "path": BASE / "live-data", "mount": "/home/node/.n8n"},
    "images": {"claim": "n8n-images", "volume": "n8n-images", "size": "10Gi",
               "path": BASE.parent / "n8n-images", "mount": "/home/node/.n8n-files"},
}
CLAIMS = tuple(entry["claim"] for entry in PROTECTED.values())
DIRECTORIES = [(BASE / "backups", 65534, 65534, 0o2770)]
for protected_claim in CLAIMS:
    DIRECTORIES.extend((
        (BASE / "backups" / protected_claim, 65534, 65534, 0o700),
        (BASE / "backup-staging" / protected_claim, 999, 999, 0o2770),
        (BASE / "backup-cache" / protected_claim / "source", 999, 999, 0o2770),
    ))

class SetupError(Exception):
    pass



def run(arguments, *, stdin=None, capture=False, sensitive=False, check=True, timeout=240):
    try:
        result = subprocess.run(arguments, input=stdin, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SetupError("command timed out: " + arguments[0])
    if result.returncode:
        private_write(RECOVERY / "last-command-error.json", json.dumps({
            "command": arguments[0], "returncode": result.returncode,
            "stdout": result.stdout[-1024**2:], "stderr": result.stderr[-1024**2:],
        }, indent=2) + "\n")
    if not result.returncode and not capture and not sensitive and result.stdout:
        print(result.stdout, end="", flush=True)
    if check and result.returncode:
        raise SetupError("command failed: " + arguments[0] + "; diagnostics saved privately")
    return result


def kube(*arguments, **options):
    return run(["k3s", "kubectl", "--request-timeout=20s", *arguments], **options)


def get_object(resource, name, namespace=None, *, sensitive=False):
    arguments = ["get", resource, name, "--ignore-not-found=true", "-o", "json"]
    if namespace:
        arguments.extend(["-n", namespace])
    output = kube(*arguments, capture=True, sensitive=sensitive).stdout
    return json.loads(output) if output.strip() else None


def mounted_on_array(path):
    existing = path
    while not existing.exists():
        existing = existing.parent
    source = run(
        ["findmnt", "-n", "-o", "SOURCE", "-T", str(existing)], capture=True,
    ).stdout.strip()
    filesystem = run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", str(existing)], capture=True,
    ).stdout.strip()
    if source != "/dev/md0" or filesystem != "btrfs":
        raise SetupError(f"{path} is on {source}/{filesystem}, expected /dev/md0/btrfs")


def checked_directory(path):
    """Allow the established base mapping, but no new symlinks beneath it."""
    if not path.is_relative_to(BASE):
        raise SetupError("directory is outside the approved namespace path")
    base_real = BASE.resolve(strict=True)
    if not path.resolve().is_relative_to(base_real):
        raise SetupError(f"directory escapes the approved namespace path: {path}")
    current = path
    while current != BASE:
        if current.is_symlink():
            raise SetupError(f"unexpected symlink: {current}")
        if current.exists() and not current.is_dir():
            raise SetupError(f"expected a directory: {current}")
        current = current.parent
    mounted_on_array(path)


def recovery_directory():
    current = Path("/root")
    for component in RECOVERY.relative_to(current).parts:
        current /= component
        if current.is_symlink():
            raise SetupError(f"unexpected recovery-directory symlink: {current}")
        if current.exists():
            attributes = current.stat()
            if not current.is_dir() or attributes.st_uid != 0:
                raise SetupError(f"recovery directory must be owned by root: {current}")
            if stat.S_IMODE(attributes.st_mode) != 0o700:
                raise SetupError(f"recovery directory must have mode 0700: {current}")
        else:
            current.mkdir(mode=0o700)


def private_read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as stream:
        attributes = os.fstat(stream.fileno())
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0 or attributes.st_nlink != 1 or
                stat.S_IMODE(attributes.st_mode) != 0o600 or attributes.st_size > 16 * 1024**2):
            raise SetupError(f"expected a private regular root-owned recovery file: {path}")
        return stream.read()


def private_write(path, content):
    if path.exists() or path.is_symlink():
        private_read(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=RECOVERY)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(RECOVERY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_secret(name, fields):
    data = {key: base64.b64encode(value.encode()).decode() for key, value in fields.items()}
    existing = get_object("secret", name, NAMESPACE, sensitive=True)
    if existing:
        if existing.get("data") != data:
            raise SetupError(f"existing Secret differs from saved recovery credentials: {name}")
        print(f"secret/{name} matches saved credentials", flush=True)
        return
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {
            "name": name, "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/managed-by": MANAGER},
        },
        "type": "Opaque", "data": data,
    }
    kube("create", "-f", "-", stdin=json.dumps(secret), sensitive=True)
    print(f"secret/{name} created", flush=True)


def portable(resource):
    return {"apiVersion": resource["apiVersion"], "kind": resource["kind"],
            "metadata": {"name": resource["metadata"]["name"],
                         **({"labels": resource["metadata"]["labels"]} if resource["metadata"].get("labels") else {}),
                         **({"namespace": resource["metadata"]["namespace"]} if resource["metadata"].get("namespace") else {})},
            **{key: resource[key] for key in ("spec", "data", "binaryData", "type") if key in resource}}


def save_secret(name, *, refresh=False, resource=None):
    secret = resource if resource is not None else get_object("secret", name, NAMESPACE, sensitive=True)
    if not secret:
        raise SetupError("required Secret is missing: " + name)
    if secret.get("kind") != "Secret" or secret["metadata"].get("name") != name or secret["metadata"].get("namespace") != NAMESPACE:
        raise SetupError("unexpected Secret identity: " + name)
    output = portable(secret)
    path = RECOVERY / (name + ".secret.json")
    if (path.exists() or path.is_symlink()) and not refresh:
        if json.loads(private_read(path)) != output:
            raise SetupError("Secret differs from the saved recovery copy: " + name)
    private_write(path, json.dumps(output, indent=2) + "\n")
    return secret


def ready(pod):
    return (not pod["metadata"].get("deletionTimestamp") and
            any(condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])))


def quantity(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(n|u|m|Ki|Mi|Gi|Ti|Pi|Ei|k|K|M|G|T|P|E)?", value)
    if not match:
        raise SetupError("unrecognized Kubernetes resource quantity")
    suffix = match[2] or ""
    factors = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
               **{unit + "i": 1024**power for power, unit in enumerate("KMGTPE", 1)},
               **{unit: 1000**power for power, unit in enumerate("KMGTPE", 1)}, "k": 1000}
    return Decimal(match[1]) * factors[suffix]


def resource_budget(node, pods):
    description = kube("describe", "node", "openmediavault", capture=True).stdout
    allocation = description.partition("Allocated resources:")[2].split("Events:", 1)[0]
    allocated = dict(re.findall(r"^\s+(cpu|memory)\s+(\S+)", allocation, re.MULTILINE))
    if set(allocated) != {"cpu", "memory"}:
        raise SetupError("could not determine the node's resource reservations")
    all_servers = [pod for pod in pods
                   if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "restic-server"
                   and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    servers = [pod for pod in all_servers if pod["spec"].get("nodeName") == "openmediavault"]
    if len(all_servers) > 1:
        raise SetupError("more than one n8n Restic server Pod exists")
    if all_servers and all_servers[0]["spec"]["containers"][0].get("resources", {}).get("requests") != {
            "cpu": "25m", "memory": "32Mi"}:
        raise SetupError("existing Restic server reservations differ from this setup")
    # Reserve the server plus one 50m/128Mi worker; check Jobs only use 64Mi.
    needed = {"cpu": Decimal("0.05") + (0 if servers else Decimal("0.025")),
              "memory": (128 + (0 if servers else 32)) * 1024**2}
    available = {key: quantity(node["status"]["allocatable"][key]) - quantity(allocated[key])
                 for key in needed}
    print("available reservations: " + str(available["cpu"] * 1000) + "m CPU, " +
          str(round(available["memory"] / 1024**2, 1)) + "Mi memory", flush=True)
    if any(available[key] < needed[key] for key in needed):
        raise SetupError("insufficient reservations for the backup endpoint plus one worker")
    cluster_pods = json.loads(kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    assigned = [pod for pod in cluster_pods if pod["spec"].get("nodeName") == "openmediavault" and
                pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    if len(assigned) + 1 + (0 if servers else 1) > int(node["status"]["allocatable"]["pods"]):
        raise SetupError("insufficient OMV Pod slots for the endpoint plus one worker")
    for pod in cluster_pods:
        if pod.get("status", {}).get("phase") != "Pending" or pod["spec"].get("nodeName"):
            continue
        if pod["metadata"]["namespace"] == NAMESPACE and any(
                pod["metadata"]["uid"] == server["metadata"]["uid"] for server in all_servers):
            continue
        if any(quantity(str(container.get("resources", {}).get("requests", {}).get(key, "0"))) > 0
               for group in ("containers", "initContainers") for container in pod["spec"].get(group, [])
               for key in ("cpu", "memory")):
            raise SetupError("pending Pod may compete for backup reservations: " +
                             pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"])


def checked_configuration():
    """Read the persisted key without returning it to a log or subprocess argument."""
    path = BASE / "live-data/config"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as stream:
        attributes = os.fstat(stream.fileno())
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 1000 or attributes.st_nlink != 1 or
                stat.S_IMODE(attributes.st_mode) != 0o600 or not 0 < attributes.st_size <= 65536):
            raise SetupError("persisted encryption configuration is not a private regular application file")
        text = stream.read(65537)
        after = os.fstat(stream.fileno())
        if (attributes.st_ino, attributes.st_size, attributes.st_mtime_ns, attributes.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SetupError("persisted encryption configuration changed while reading")
    config = json.loads(text)
    if not isinstance(config.get("encryptionKey"), str) or not config["encryptionKey"]:
        raise SetupError("persisted encryption key is missing")
    original = json.loads(private_read(RECOVERY / "storage-migration/copy-validation.json"))
    if hashlib.sha256(text.encode()).hexdigest() != original.get("encryption_config_sha256"):
        raise SetupError("persisted encryption configuration differs from the verified migration")
    private_write(RECOVERY / "n8n-encryption-config.json", text)


def checked_sources():
    state = json.loads(private_read(RECOVERY / "storage-migration/state.json"))
    result = json.loads(private_read(RECOVERY / "storage-migration/result.json"))
    original = json.loads(private_read(RECOVERY / "storage-migration/bindings.before.json"))
    if (state.get("version") != 1 or state.get("phase") != "complete" or state.get("authoritative") != "target" or
            result.get("result") != "passed" or result.get("new_claim") != "n8n-data-live" or
            result.get("namespace") != NAMESPACE):
        raise SetupError("the guarded live-data migration has not completed")
    real_base = BASE.resolve(strict=True)
    expected_base = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4/k3s/data/n8n")
    if real_base != expected_base:
        raise SetupError("the approved array namespace mapping changed")
    identities, evidence = {}, []
    for component, entry in PROTECTED.items():
        path = entry["path"]
        real = path.resolve(strict=True)
        expected = expected_base / "live-data" if component == "data" else expected_base.parent / "n8n-images"
        if path.is_symlink() or not path.is_dir() or real != expected:
            raise SetupError("a live data path changed or became a symlink: " + component)
        if component == "data" and (real.stat().st_dev, real.stat().st_ino) != (state["target_device"], state["target_inode"]):
            raise SetupError("the published live-data directory identity changed")
        mounted_on_array(path)
        attributes = path.stat()
        if (attributes.st_uid, attributes.st_gid) != (1000, 1000):
            raise SetupError("live directory ownership changed: " + component)
        claim = get_object("pvc", entry["claim"], NAMESPACE)
        volume = get_object("pv", entry["volume"])
        claim_uid = state["target_claim_uid"] if component == "data" else original["n8n-images"]["claim"]["metadata"]["uid"]
        volume_uid = state["target_volume_uid"] if component == "data" else original["n8n-images"]["volume"]["metadata"]["uid"]
        affinity = {"required": {"nodeSelectorTerms": [{"matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}}
        if (not claim or not volume or claim["metadata"]["uid"] != claim_uid or volume["metadata"]["uid"] != volume_uid or
                claim.get("status", {}).get("phase") != "Bound" or volume.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != entry["volume"] or volume["spec"].get("claimRef", {}).get("uid") != claim_uid or
                volume["spec"].get("local", {}).get("path") != str(path) or volume["spec"].get("nodeAffinity") != affinity or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
                volume["spec"].get("capacity", {}).get("storage") != entry["size"] or
                any(item["spec"].get("accessModes") != ["ReadWriteOnce"] or
                    item["spec"].get("storageClassName") != "n8n-local" for item in (claim, volume))):
            raise SetupError("a protected binding differs from the verified migration: " + component)
        identities[component] = {"claim_uid": claim_uid, "volume_uid": volume_uid}
        evidence.extend(((entry["claim"] + ".pvc.json", claim), (entry["volume"] + ".pv.json", volume)))
    checked_configuration()
    return state, identities, evidence


def live_preflight(*, check_resources=True, record=True):
    nodes = json.loads(kube("get", "nodes", "-o", "json", capture=True).stdout)["items"]
    if [node["metadata"]["name"] for node in nodes] != ["openmediavault"]:
        raise SetupError("additional nodes require their later approval gate")
    node = nodes[0]
    if (node["spec"].get("unschedulable") or any(item.get("effect") in ("NoSchedule", "NoExecute") for item in node["spec"].get("taints", [])) or
            not any(item.get("type") == "Ready" and item.get("status") == "True" for item in node["status"].get("conditions", [])) or
            any(item.get("status") == "True" for item in node["status"].get("conditions", [])
                if item.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure"))):
        raise SetupError("OMV is not ready/schedulable or reports node pressure")
    available = next((int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemAvailable:")), 0)
    if available < 2 * 1024**3:
        raise SetupError("less than 2 GiB of physical memory is available for burstable backup work")
    state, identities, evidence = checked_sources()
    deployment = get_object("deployment", "n8n", NAMESPACE)
    if (not deployment or deployment["metadata"]["uid"] != state["deployment_uid"] or
            deployment["metadata"].get("ownerReferences") or deployment["spec"].get("replicas") != 1 or
            deployment["spec"].get("strategy") != {"type": "Recreate"}):
        raise SetupError("the original single-replica n8n Deployment changed")
    spec = deployment["spec"]["template"]["spec"]
    container = spec.get("containers", [{}])[0]
    if (len(spec.get("containers", [])) != 1 or spec.get("initContainers") or spec.get("ephemeralContainers") or
            spec.get("nodeSelector") != {"kubernetes.io/hostname": "openmediavault"} or
            container.get("name") != "n8n" or container.get("image") != IMAGE or
            container.get("resources", {}).get("requests") not in (ORIGINAL_REQUESTS, TARGET_REQUESTS) or
            container.get("resources", {}).get("limits") != LIMITS or container.get("env") or
            container.get("command") or container.get("args") or
            container.get("envFrom") != [{"configMapRef": {"name": "n8n-config"}}] or
            spec.get("volumes") != [{"name": "n8n-data", "persistentVolumeClaim": {"claimName": "n8n-data-live"}},
                                    {"name": "n8n-images", "persistentVolumeClaim": {"claimName": "n8n-images"}}] or
            container.get("volumeMounts") != [{"name": "n8n-data", "mountPath": "/home/node/.n8n"},
                                               {"name": "n8n-images", "mountPath": "/home/node/.n8n-files"}]):
        raise SetupError("n8n image, storage mounts, backend, or resource settings changed")
    config = get_object("configmap", "n8n-config", NAMESPACE, sensitive=True)
    if not config or any(name in config.get("data", {}) for name in (
            "DB_TYPE", "DB_TYPE_FILE", "DB_SQLITE_DATABASE", "DB_SQLITE_DATABASE_FILE", "EXECUTIONS_MODE",
            "N8N_USER_FOLDER", "N8N_USER_FOLDER_FILE", "N8N_CONFIG_FILES", "N8N_ENCRYPTION_KEY", "N8N_ENCRYPTION_KEY_FILE",
            "N8N_BINARY_DATA_STORAGE_PATH", "N8N_BINARY_DATA_STORAGE_PATH_FILE", "N8N_DEFAULT_BINARY_DATA_MODE")):
        raise SetupError("n8n configuration now overrides the verified database/key/storage setup")
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]
    users = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
             any(volume.get("persistentVolumeClaim", {}).get("claimName") in (*CLAIMS, "n8n-data")
                 for volume in pod["spec"].get("volumes", []))]
    if len(users) != 1 or not ready(users[0]) or users[0]["spec"].get("nodeName") != "openmediavault":
        raise SetupError("expected exactly one ready Pod using both authoritative claims")
    pod = users[0]
    live_container = next((item for item in pod["spec"].get("containers", []) if item["name"] == "n8n"), {})
    live_volumes = {item["name"]: item for item in pod["spec"].get("volumes", [])}
    status = next((item for item in pod.get("status", {}).get("containerStatuses", []) if item["name"] == "n8n"), {})
    if status.get("imageID") != IMAGE or not status.get("ready") or live_container.get("resources") != container.get("resources"):
        raise SetupError("n8n rollout/image is not stable on the expected resource settings")
    for entry in PROTECTED.values():
        if not any(mount.get("mountPath") == entry["mount"] and not mount.get("readOnly") and not mount.get("subPath") and
                   not mount.get("subPathExpr") and live_volumes[mount["name"]].get("persistentVolumeClaim", {}).get("claimName") == entry["claim"] and
                   not live_volumes[mount["name"]]["persistentVolumeClaim"].get("readOnly") for mount in live_container.get("volumeMounts", [])):
            raise SetupError("a production writable mount differs from its authoritative claim")
    if json.loads(kube("get", "hpa", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]:
        raise SetupError("n8n autoscaling requires inspection before adjusting reservations")
    if check_resources:
        resource_budget(node, pods)
    identities["application"] = {"deployment_uid": deployment["metadata"]["uid"], "pod_uid": pod["metadata"]["uid"],
                                 "image_id": status["imageID"], "restarts": status.get("restartCount"),
                                 "template": hashlib.sha256(json.dumps(deployment["spec"]["template"], sort_keys=True).encode()).hexdigest()}
    if record:
        for name, value in (*evidence, ("n8n.deployment.json", deployment)):
            target = RECOVERY / name
            if (target.exists() or target.is_symlink()) and json.loads(private_read(target))["metadata"]["uid"] != value["metadata"]["uid"]:
                raise SetupError("saved resource identity changed: " + name)
            private_write(target, json.dumps(value, indent=2) + "\n")
        capture_dependencies([pod])
    return identities


def adjust_resources():
    before = get_object("deployment", "n8n", NAMESPACE)
    resources = before["spec"]["template"]["spec"]["containers"][0]["resources"]
    path = RECOVERY / "resource-adjustment.before.json"
    if path.exists() or path.is_symlink():
        original = json.loads(private_read(path))
        if original["metadata"]["uid"] != before["metadata"]["uid"]:
            raise SetupError("saved resource-adjustment Deployment identity changed")
        permitted = json.loads(json.dumps(original["spec"]))
        permitted["template"]["spec"]["containers"][0]["resources"]["requests"] = TARGET_REQUESTS
        if before["spec"] not in (original["spec"], permitted):
            raise SetupError("n8n settings drifted beyond the approved reservation adjustment")
    else:
        private_write(path, json.dumps(before, indent=2) + "\n")
    if resources["requests"] == TARGET_REQUESTS:
        print("n8n reservations already match 225m/480Mi; existing limits retained", flush=True)
        verify_adjustment(before, before["spec"], None)
        return
    if resources != {"requests": ORIGINAL_REQUESTS, "limits": LIMITS}:
        raise SetupError("live requests or limits differ before adjustment")
    identities = live_preflight(check_resources=False)
    pod_uid = identities["application"]["pod_uid"]
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]
    pod_name = next(pod["metadata"]["name"] for pod in pods if pod["metadata"]["uid"] == pod_uid)
    measurements = []
    for attempt in range(3):
        usage = json.loads(kube("get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/n8n/pods/" + pod_name,
                                capture=True, sensitive=True).stdout)
        entries = [item for item in usage.get("containers", []) if item["name"] == "n8n"]
        if len(entries) != 1:
            raise SetupError("current n8n container memory metrics are unavailable")
        measurements.append(quantity(entries[0]["usage"]["memory"]))
        if attempt < 2:
            time.sleep(5)
    peak = max(measurements)
    if peak >= quantity(TARGET_REQUESTS["memory"]):
        raise SetupError("observed n8n memory use already reaches the proposed reservation; do not lower it")
    latest = get_object("deployment", "n8n", NAMESPACE)
    if latest["metadata"]["uid"] != before["metadata"]["uid"] or latest["spec"] != before["spec"]:
        raise SetupError("n8n changed while sampling its memory use")
    before = latest
    original_template = before["spec"]["template"]
    expected_template = json.loads(json.dumps(original_template))
    expected_template["spec"]["containers"][0]["resources"]["requests"] = TARGET_REQUESTS
    patch = [{"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": before["metadata"]["resourceVersion"]},
             {"op": "test", "path": "/spec/template", "value": original_template},
             {"op": "test", "path": "/spec/template/spec/containers/0/resources", "value": resources},
             {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests", "value": TARGET_REQUESTS}]
    print("adjusting n8n requests to 225m/480Mi; 2 CPU/1Gi limits and authoritative storage retained", flush=True)
    kube("patch", "deployment", "n8n", "-n", NAMESPACE, "--type=json", "-p", json.dumps(patch), sensitive=True)
    expected_spec = json.loads(json.dumps(before["spec"]))
    expected_spec["template"] = expected_template
    verify_adjustment(before, expected_spec, peak)


def verify_adjustment(before, expected_spec, peak):
    kube("rollout", "status", "-n", NAMESPACE, "deployment/n8n", "--timeout=240s", timeout=270)
    current = get_object("deployment", "n8n", NAMESPACE)
    if current["metadata"]["uid"] != before["metadata"]["uid"] or current["spec"] != expected_spec:
        raise SetupError("n8n changed beyond the approved request patch")
    ready_identity = live_preflight(check_resources=False, record=False)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if live_preflight(check_resources=False, record=False) != ready_identity:
            raise SetupError("n8n restarted or changed during reservation-adjustment verification")
        time.sleep(5)
    private_write(RECOVERY / "resource-adjustment.json", json.dumps({"result": "passed", "requests": TARGET_REQUESTS,
        "limits": LIMITS, "peak_sampled_memory_bytes": int(peak) if peak is not None else None,
        "production": ready_identity}, indent=2) + "\n")
    print("n8n request adjustment verified; original Deployment saved privately", flush=True)


def dependency_references(pods):
    references = {"secret": {}, "configmap": {}}

    def add(kind, source, name_key, field, *, data_only=False):
        name = source.get(name_key)
        if not name:
            return
        optional = source.get("optional") is True
        entry = references[kind].setdefault(name, {"optional": True, "references": []})
        # A required reference wins even when another Pod permits absence.
        entry["optional"] = entry["optional"] and optional
        keys = [source["key"]] if "key" in source else [item["key"] for item in (source.get("items") or [])]
        entry["references"].append({"pod": pod["metadata"]["name"], "field": field,
                                    "optional": optional, "keys": keys, "data_only": data_only})

    for pod in pods:
        for volume in pod["spec"].get("volumes", []):
            for kind, field, name_key in (("secret", "secret", "secretName"), ("configmap", "configMap", "name")):
                add(kind, volume.get(field, {}), name_key, "volumes/" + volume["name"] + "/" + field)
            for index, projected in enumerate(volume.get("projected", {}).get("sources", [])):
                for kind, field in (("secret", "secret"), ("configmap", "configMap")):
                    add(kind, projected.get(field, {}), "name",
                        "volumes/" + volume["name"] + "/projected/" + str(index) + "/" + field)
        for group in ("containers", "initContainers"):
            for container in pod["spec"].get(group, []):
                for index, item in enumerate(container.get("envFrom", [])):
                    for kind, field in (("secret", "secretRef"), ("configmap", "configMapRef")):
                        add(kind, item.get(field, {}), "name",
                            group + "/" + container["name"] + "/envFrom/" + str(index) + "/" + field, data_only=True)
                for item in container.get("env", []):
                    for kind, field in (("secret", "secretKeyRef"), ("configmap", "configMapKeyRef")):
                        add(kind, item.get("valueFrom", {}).get(field, {}), "name",
                            group + "/" + container["name"] + "/env/" + item["name"] + "/" + field, data_only=True)
    return references


def capture_dependencies(pods):
    """Save present dependencies; record absent optional ones in the recovery index."""
    references = dependency_references(pods)
    for kind, entries in references.items():
        for name, entry in sorted(entries.items()):
            resource = get_object(kind, name, NAMESPACE, sensitive=True)
            entry.update(present=resource is not None, saved_file=None)
            if resource is None:
                if not entry["optional"]:
                    locations = ", ".join(item["pod"] + ":" + item["field"] for item in entry["references"] if not item["optional"])
                    raise SetupError("required " + ("Secret" if kind == "secret" else "ConfigMap") +
                                     " is missing: " + name + "; referenced by " + locations)
                print("optional " + kind + " absent; recorded for recovery: " + name, flush=True)
                continue
            data_keys = set(resource.get("data", {}))
            volume_keys = data_keys | set(resource.get("binaryData", {}))
            for reference in entry["references"]:
                available = data_keys if reference["data_only"] else volume_keys
                if not reference["optional"] and not set(reference["keys"]).issubset(available):
                    raise SetupError("required keys are missing from " + kind + "/" + name)
            filename = name + "." + kind + ".json"
            if kind == "secret":
                save_secret(name, resource=resource)
            else:
                private_write(RECOVERY / filename, json.dumps(portable(resource), indent=2) + "\n")
            entry.update(saved_file=filename, uid=resource["metadata"]["uid"],
                         resource_version=resource["metadata"].get("resourceVersion"))
    index = {"version": 1, "namespace": NAMESPACE, "resources": references}
    private_write(RECOVERY / "dependency-inventory.json", json.dumps(index, indent=2) + "\n")
    return index


def render_resources():
    rendered = kube("create", "--dry-run=client", "-k", str(MANIFESTS), "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if not resources:
        raise SetupError("backup manifest bundle is empty")
    for resource in resources:
        metadata = resource["metadata"]
        group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        kind = resource["kind"].lower() + ("." + group if group else "")
        existing = get_object(kind, metadata["name"], metadata.get("namespace"))
        if not existing:
            continue
        if existing["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != MANAGER:
            raise SetupError("resource name is already used outside this migration: " + metadata["name"])
        if resource["kind"] in ("PersistentVolume", "PersistentVolumeClaim", "StorageClass"):
            actual = existing.get("spec", existing)
            expected = resource.get("spec", resource)
            for key in ("local", "accessModes", "volumeMode", "storageClassName", "volumeName", "capacity",
                        "persistentVolumeReclaimPolicy", "nodeAffinity", "provisioner", "reclaimPolicy", "volumeBindingMode", "resources"):
                if key in expected and actual.get(key) != expected[key]:
                    raise SetupError("existing backup storage differs: " + metadata["name"] + ": " + key)
            if resource["kind"] == "PersistentVolume":
                reference = actual.get("claimRef", {})
                if any(reference.get(key) != value for key, value in expected["claimRef"].items()):
                    raise SetupError("backup PV is reserved for a different claim: " + metadata["name"])
                if existing.get("status", {}).get("phase") in ("Released", "Failed"):
                    raise SetupError("backup PV requires manual binding inspection: " + metadata["name"])
    return resources


def credential_fields(claim, entry):
    repository = "rest:https://restic.n8n.svc:8000/" + claim + "/"
    fields = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD", "htpasswd")
    if (not isinstance(entry, dict) or set(entry) != set(fields) or
            entry.get("RESTIC_REPOSITORY") != repository or entry.get("RESTIC_REST_USERNAME") != claim or
            not all(isinstance(entry[key], str) and entry[key] for key in fields)):
        raise SetupError("saved repository credentials are invalid: " + claim)
    return {key: value for key, value in entry.items() if key != "htpasswd"}


def prepare_credentials():
    path = RECOVERY / "credentials.json"
    have_credentials = path.exists() or path.is_symlink()
    credentials = json.loads(private_read(path)) if have_credentials else None
    for directory, uid, gid, mode in DIRECTORIES:
        checked_directory(directory)
        if directory.exists() and any(directory.iterdir()):
            if not have_credentials:
                raise SetupError("nonempty backup directory has no saved credentials: " + str(directory))
            attributes = directory.stat()
            if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (uid, gid, mode):
                raise SetupError("existing backup directory ownership or mode differs: " + str(directory))
    if not have_credentials:
        for name in ("restic-root-ca", "restic-server-tls", "restic-server-auth", *("restic-" + claim for claim in CLAIMS)):
            if get_object("secret", name, NAMESPACE, sensitive=True):
                raise SetupError("existing Secret has no saved recovery credentials: " + name)
        credentials = {"namespace": NAMESPACE, "repositories": {}}
        for claim in CLAIMS:
            password = secrets.token_hex(32)
            htpasswd = run(["htpasswd", "-n", "-i", "-B", "-C", "12", claim],
                           stdin=password + "\n", capture=True, sensitive=True).stdout.strip() + "\n"
            credentials["repositories"][claim] = {
                "RESTIC_REPOSITORY": "rest:https://restic.n8n.svc:8000/" + claim + "/",
                "RESTIC_PASSWORD": secrets.token_hex(32), "RESTIC_REST_USERNAME": claim,
                "RESTIC_REST_PASSWORD": password, "htpasswd": htpasswd,
            }
        private_write(path, json.dumps(credentials, indent=2) + "\n")
    if (credentials.get("namespace") != NAMESPACE or
            set(credentials.get("repositories", {})) != set(CLAIMS)):
        raise SetupError("saved credentials do not describe both approved repositories")
    entries = credentials["repositories"]
    for claim in CLAIMS:
        credential_fields(claim, entries[claim])
    # Check every existing Secret before creating any of them.
    secret_fields = {"restic-server-auth": {".htpasswd": "".join(entries[claim]["htpasswd"] for claim in CLAIMS)},
                     **{"restic-" + claim: credential_fields(claim, entries[claim]) for claim in CLAIMS}}
    for name, fields in secret_fields.items():
        existing = get_object("secret", name, NAMESPACE, sensitive=True)
        expected = {key: base64.b64encode(value.encode()).decode() for key, value in fields.items()}
        if existing and existing.get("data") != expected:
            raise SetupError("existing Secret differs from the saved credentials: " + name)
    return secret_fields


def wait_for_job(name, *, timeout=330):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        job = get_object("job", name, NAMESPACE)
        if not job:
            raise SetupError("repository check Job disappeared: " + name)
        conditions = job.get("status", {}).get("conditions", [])
        if any(item.get("type") == "Complete" and item.get("status") == "True" for item in conditions):
            print("job/" + name + " complete", flush=True)
            return
        for item in conditions:
            if item.get("type") in ("Failed", "FailureTarget") and item.get("status") == "True":
                raise SetupError("repository check failed: " + name + ": " + item.get("reason", "unknown"))
        if time.monotonic() - started >= 60:
            pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", "batch.kubernetes.io/job-name=" + name,
                                   "-o", "json", capture=True).stdout)["items"]
            for pod in pods:
                for item in pod.get("status", {}).get("conditions", []):
                    if item.get("type") == "PodScheduled" and item.get("status") == "False":
                        raise SetupError("repository check cannot schedule: " + item.get("message", "unknown reason"))
        time.sleep(2)
    raise SetupError("repository check timed out: " + name)


def print_check_logs(name):
    result = kube("logs", "-n", NAMESPACE, "job/" + name, "--pod-running-timeout=5s",
                  capture=True, sensitive=True, check=False, timeout=15)
    if result.returncode:
        print("repository check logs are not available yet: " + name, flush=True)
        return
    output = result.stdout
    entries = json.loads(private_read(RECOVERY / "credentials.json"))["repositories"]
    for entry in entries.values():
        for field in ("RESTIC_PASSWORD", "RESTIC_REST_PASSWORD", "htpasswd"):
            output = output.replace(entry[field], "[redacted]")
        basic_auth = base64.b64encode((entry["RESTIC_REST_USERNAME"] + ":" + entry["RESTIC_REST_PASSWORD"]).encode()).decode()
        output = output.replace(basic_auth, "[redacted]")
    print(output, end="", flush=True)


def repository_checks():
    active = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", "backups.mousses.xyz/restic-client=true",
                             "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in active):
        raise SetupError("another repository client is still active; wait for it to stop before rerunning setup")
    service = get_object("service", "restic", NAMESPACE)
    ips = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    expected = ",".join(str(ipaddress.ip_address(value)) for value in ips)
    template = json.loads(kube("create", "--dry-run=client", "-f", str(MANIFESTS / "repository-check.yaml"),
                               "-o", "json", capture=True).stdout)
    for claim in CLAIMS:
        live_preflight(check_resources=True)
        job = json.loads(json.dumps(template))
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["envFrom"] = [{"secretRef": {"name": "restic-" + claim}}]
        container["env"] = [{"name": "EXPECTED_RESTIC_IPS", "value": expected},
                            {"name": "OTHER_REPOSITORIES", "value": ",".join(name for name in CLAIMS if name != claim)}]
        created = json.loads(kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
        name = created["metadata"]["name"]
        private_write(RECOVERY / ("repository-check-" + claim + ".json"), json.dumps(created, indent=2) + "\n")
        print("job/" + name + " created (" + claim + " DNS/TLS/authentication/isolation check)", flush=True)
        try:
            wait_for_job(name)
        finally:
            print_check_logs(name)
        deadline = time.monotonic() + 45
        while True:
            clients = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", "backups.mousses.xyz/restic-client=true",
                                      "-o", "json", capture=True).stdout)["items"]
            if not any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in clients):
                break
            if time.monotonic() >= deadline:
                raise SetupError("the completed repository check still has a live Pod; do not start another worker")
            time.sleep(2)


def operation_lock(path, *, create):
    current = Path("/root")
    for part in path.parent.relative_to(current).parts:
        current /= part
        attributes = current.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise SetupError("operation-lock directory is not private and root-owned")
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0), 0o600)
    stream = os.fdopen(descriptor, "r+")
    attributes = os.fstat(stream.fileno())
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
            attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o600):
        stream.close()
        raise SetupError("operation lock is not a private regular root-owned file")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        stream.close()
        raise SetupError("another operation holds the backup lock: " + str(path)) from error
    return stream


def prepare():
    live_preflight(check_resources=False)
    adjust_resources()
    # The expected app rollout is complete. Only now establish the baseline
    # used to prove endpoint setup did not restart or rebind production.
    original_identities = live_preflight()
    render_resources()
    fields = prepare_credentials()
    for directory, uid, gid, mode in DIRECTORIES:
        if directory.exists():
            attributes = directory.stat()
            if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (uid, gid, mode):
                raise SetupError("existing backup directory ownership or mode differs: " + str(directory))
        else:
            directory.mkdir(parents=True)
            os.chown(directory, uid, gid)
            os.chmod(directory, mode)
        mounted_on_array(directory)
        print("array directory prepared: " + str(directory), flush=True)
    for name, values in fields.items():
        ensure_secret(name, values)
    kube("apply", "-k", str(MANIFESTS))
    kube("wait", "-n", NAMESPACE, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s",
         "pvc/restic-repositories", "pvc/data-backup-staging", "pvc/images-backup-staging")
    for name in ("restic-repositories", "data-backup-staging", "images-backup-staging"):
        claim = get_object("pvc", name, NAMESPACE)
        volume = get_object("pv", claim["spec"]["volumeName"])
        if volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"]:
            raise SetupError("new backup PV claim UID does not match: " + name)
    kube("wait", "-n", NAMESPACE, "--for=condition=Ready", "--timeout=180s",
         "certificate/restic-root-ca", "certificate/restic-server-tls")
    kube("rollout", "status", "-n", NAMESPACE, "deployment/restic-server", "--timeout=180s")
    for name in fields:
        save_secret(name)
    save_secret("restic-server-tls", refresh=True)
    ca = save_secret("restic-root-ca")
    private_write(RECOVERY / "ca.crt", base64.b64decode(ca["data"]["tls.crt"], validate=True).decode())
    print("recovery credentials saved privately under " + str(RECOVERY) + "; do not paste them", flush=True)
    repository_checks()
    final_identities = live_preflight(check_resources=False)
    if final_identities != original_identities:
        raise SetupError("production Pod, image, or storage identity changed during endpoint setup")
    private_write(RECOVERY / "endpoint-verification.json", json.dumps({"result": "passed", "namespace": NAMESPACE,
        "repositories": list(CLAIMS), "production": final_identities,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2) + "\n")
    kube("get", "pods", "-n", NAMESPACE, "-o", "wide")
    kube("get", "pvc", "-n", NAMESPACE)
    print("both n8n repositories and array staging verified; data backups and isolated restores remain pending", flush=True)


def main():
    if os.geteuid() != 0:
        raise SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    recovery_directory()
    for command in ("k3s", "findmnt", "htpasswd"):
        if not shutil.which(command):
            raise SetupError("required command is missing: " + command)
    if not BASE.is_dir() or not MANIFESTS.is_dir():
        raise SetupError("approved namespace path or downloaded backup manifests are missing")
    mounted_on_array(BASE)
    if shutil.disk_usage(BASE).free < 100 * 1024**3:
        raise SetupError("less than 100 GiB remains on the approved array")
    with ExitStack() as locks:
        locks.enter_context(operation_lock(RECOVERY / ".setup.lock", create=True))
        locks.enter_context(operation_lock(COORDINATION_LOCK, create=False))
        print("n8n endpoint setup serialized with the existing namespace backup workers", flush=True)
        prepare()


if __name__ == "__main__":
    try:
        main()
    except SetupError as error:
        print("n8n backup setup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, subprocess.TimeoutExpired):
        print("n8n backup setup stopped: filesystem, API, or configuration validation failed; private values omitted", file=sys.stderr)
        sys.exit(1)
