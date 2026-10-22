#!/usr/bin/env python3
"""Prepare seven Paperless repositories while preserving its live writers and settings."""

import base64
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import tempfile

NAMESPACE = "paperless-ngx"
PHASE = "starting"
MANAGER = "nas-backup-migration"
BASE = Path("/filesystem/k3s/data/paperless-ngx")
PHYSICAL = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4")
RECOVERY = Path("/root/k3s-backup-recovery/paperless-ngx")
COORDINATION_LOCK = Path("/root/k3s-backup-recovery/board-games/data-postgres-0/.setup.lock")
MANIFESTS = Path(__file__).resolve().parents[2] / "apps/paperless-ngx/backups"
ORIGINAL_REQUESTS = {"cpu": "250m", "memory": "512Mi"}
TARGET_REQUESTS = {"cpu": "225m", "memory": "480Mi"}
LIMITS = {"cpu": "2", "memory": "2Gi"}
AI_CAPTURE = RECOVERY / "ai-openapi/capture-cnpoixp8"
AI_SCHEMA_SHA256 = "998f44aaf47643ac9ef72b86636c59c9fd3b43b2c813e8ffb148173cb979d6e6"
PROTECTED = {
    "data": {"claim": "paperless-data", "size": "10Gi", "path": Path("/filesystem/Media/paperless-ngx/paperless"),
             "claim_uid": "6e8db495-34c0-4059-b26b-486baf8cc515", "volume_uid": "b2ffe013-235f-4a42-8eb7-a7fc059821a7"},
    "ai": {"claim": "paperless-ai", "size": "1Gi", "path": Path("/filesystem/Media/paperless-ngx/paperless-ai/data"),
           "claim_uid": "b479477d-5a23-45a9-b4eb-30e8d3162e53", "volume_uid": "d593cf06-07f5-4297-b98c-bb7ae8374cbc"},
    "gpt": {"claim": "paperless-gpt", "size": "1Gi", "path": Path("/filesystem/Media/paperless-ngx/paperless-gpt/prompts"),
            "claim_uid": "bebd72c8-7cf3-4150-8c20-c6a2685c6d1b", "volume_uid": "3f100c21-687e-469a-ad10-0949e01d26e2"},
    "gpt-config": {"claim": "paperless-gpt-config", "size": "1Gi", "path": BASE / "gpt-live/config",
                   "claim_uid": "579b3284-32f7-44f0-b1f7-71fd86fd1379", "volume_uid": "f97db49f-253e-426b-a2a1-30462dbc36c8"},
    "gpt-db": {"claim": "paperless-gpt-db", "size": "1Gi", "path": BASE / "gpt-live/db",
               "claim_uid": "f8e29cdf-f0d7-4121-80a4-0a36166e16de", "volume_uid": "2bcaf473-2807-472d-a9b9-cb39020bcd7c"},
    "postgres": {"claim": "paperless-postgres", "size": "10Gi", "path": Path("/filesystem/Media/paperless-ngx/postgres/data"),
                 "claim_uid": "f77e69f2-09bb-4bf5-90f3-5556ee43afd7", "volume_uid": "f9af95f0-78d9-46c2-9ff3-ba7363c9af57"},
    "redis": {"claim": "paperless-redis", "size": "1Gi", "path": Path("/filesystem/Media/paperless-ngx/redis/data"),
              "claim_uid": "3977d7e9-459f-4a26-883d-d8d69b44fb89", "volume_uid": "98462924-5864-4711-a67a-d22a1f9c1771"},
}
WORKLOADS = {
    "paperless": ("deployment", "f65e67cc-4d25-4723-9244-82eb2235b0ec", "a28330462994b937938e02aaa5632a1effcc7f9acfba96c1bef9c50e1167a027"),
    "paperless-ai": ("deployment", "a4989770-d4c0-407f-a11f-0c8e42873532", "2b65888163fd59716f1c8285b31c5bd0b30c9c3c192c42b516688e3887d4ba60"),
    "paperless-gpt": ("deployment", "ab83093f-9c69-4784-b04a-566906d17232", "15bad5d455b98f21bb7b5d6615f56871ff67a8bb379dc0dd7ba411f4633071a6"),
    "postgres": ("statefulset", "22709009-62fd-4284-a69d-5c98d1237749", "1090bc3a8ccfb0b55f78a494d76f8d603434f7e4553543d6e807bc7bd6bbd17f"),
    "redis": ("deployment", "788eb0c3-bb9a-4a21-a2af-c4e0723d59aa", "c22af04bb576503bf16b3e34a1fd2fd82de0f765afd866d2e380145e0af30d78"),
    "gotenberg": ("deployment", "86b8f7cb-c65e-4d83-9d3f-76a88d0ecfb2", "f9104080d9a7ecab253fb5ebe75100329cf5699c33ec0448f2ea02d885dfde4b"),
    "tika": ("deployment", "fee2f180-7adc-4de3-8f7b-02a71ec62c68", "c0154cb95587cde64be74f35ada1a2bd7892219f3f0ac3c9dc6cab34046b3573"),
}
MOUNTS = {
    "paperless": {"/usr/src/paperless/" + name: ("paperless-data", name) for name in ("data", "media", "export", "consume")},
    "paperless-ai": {"/app/data": ("paperless-ai", None)},
    "paperless-gpt": {"/app/prompts": ("paperless-gpt", None), "/app/config": ("paperless-gpt-config", None), "/app/db": ("paperless-gpt-db", None)},
    "postgres": {"/var/lib/postgresql": ("paperless-postgres", None)},
    "redis": {"/data": ("paperless-redis", None)},
    "gotenberg": {}, "tika": {},
}
CLAIMS = tuple(entry["claim"] for entry in PROTECTED.values())
DIRECTORIES = [(BASE / "backups", 65534, 65534, 0o2770)]
for protected_claim in CLAIMS:
    DIRECTORIES.extend(((BASE / "backups" / protected_claim, 65534, 65534, 0o700),
                        (BASE / "backup-staging" / protected_claim, 999, 999, 0o2770),
                        (BASE / "backup-cache" / protected_claim / "source", 999, 999, 0o2770)))


class SetupError(Exception):
    pass


class CapacityError(SetupError):
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
        raise SetupError("more than one paperless-ngx Restic server Pod exists")
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
    insufficient = any(available[key] < needed[key] for key in needed)
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
    if insufficient:
        raise CapacityError("insufficient reservations for the backup endpoint plus one worker")


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
    repository = "rest:https://restic.paperless-ngx.svc:8000/" + claim + "/"
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
                "RESTIC_REPOSITORY": "rest:https://restic.paperless-ngx.svc:8000/" + claim + "/",
                "RESTIC_PASSWORD": secrets.token_hex(32), "RESTIC_REST_USERNAME": claim,
                "RESTIC_REST_PASSWORD": password, "htpasswd": htpasswd,
            }
        private_write(path, json.dumps(credentials, indent=2) + "\n")
    if (credentials.get("namespace") != NAMESPACE or
            set(credentials.get("repositories", {})) != set(CLAIMS)):
        raise SetupError("saved credentials do not describe all seven approved repositories")
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
    global PHASE
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
        PHASE = "verifying repository " + claim
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


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise SetupError(message)


def checked_sources():
    require(BASE.resolve(strict=True) == PHYSICAL / "k3s/data/paperless-ngx", "approved namespace array mapping changed")
    state = json.loads(private_read(RECOVERY / "gpt-storage-migration/state.json"))
    require(state.get("phase") == "complete" and state.get("authoritative") == "target",
            "GPT storage migration is unfinished; retain its authoritative writer")
    receipt = json.loads(private_read(AI_CAPTURE / "receipt.json"))
    require(receipt.get("result") == "passed" and receipt.get("namespace") == NAMESPACE and
            receipt.get("component") == "paperless-ai" and receipt.get("pinned_generator_verified") is True and
            receipt.get("classification") == "generated_api_documentation" and
            receipt.get("file_sha256") == AI_SCHEMA_SHA256 and receipt.get("source_and_pod_unchanged") is True,
            "AI schema classification/private capture has not passed")
    require(hashlib.sha256(private_read(AI_CAPTURE / "openapi.json").encode()).hexdigest() == AI_SCHEMA_SHA256,
            "private AI schema copy changed")
    identities, evidence = {}, []
    for component, entry in PROTECTED.items():
        path = entry["path"]
        expected = PHYSICAL / path.relative_to("/filesystem")
        require(path.is_dir() and not path.is_symlink() and path.resolve(strict=True) == expected,
                "a protected source directory changed: " + component)
        mounted_on_array(path)
        for backup in (BASE / "backups", BASE / "backup-staging", BASE / "backup-cache"):
            target = backup.resolve()
            require(not target.is_relative_to(expected) and not expected.is_relative_to(target),
                    "backup storage overlaps a live source: " + component)
        claim = get_object("pvc", entry["claim"], NAMESPACE)
        volume = get_object("pv", entry["claim"])
        require(claim and volume and claim["metadata"]["uid"] == entry["claim_uid"] and
                volume["metadata"]["uid"] == entry["volume_uid"], "inventoried source binding identity changed: " + component)
        reference = volume["spec"].get("claimRef", {})
        affinity = {"required": {"nodeSelectorTerms": [{"matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}}
        require(claim.get("status", {}).get("phase") == "Bound" and volume.get("status", {}).get("phase") == "Bound" and
                claim["spec"].get("volumeName") == entry["claim"] and reference.get("uid") == entry["claim_uid"] and
                reference.get("name") == entry["claim"] and reference.get("namespace") == NAMESPACE and
                volume["spec"].get("local", {}).get("path") == str(path) and
                volume["spec"].get("nodeAffinity") == affinity and volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain" and
                volume["spec"].get("capacity", {}).get("storage") == entry["size"] and
                all(item["spec"].get("accessModes") == ["ReadWriteOnce"] and
                    item["spec"].get("storageClassName") == "paperless-local" for item in (claim, volume)),
                "protected source storage settings changed: " + component)
        attrs = path.stat()
        identities[component] = {"claim_uid": entry["claim_uid"], "volume_uid": entry["volume_uid"],
                                 "directory_device": attrs.st_dev, "directory_inode": attrs.st_ino}
        evidence.extend(((entry["claim"] + ".pvc.json", claim), (entry["claim"] + ".pv.json", volume)))
    return identities, evidence, receipt


def checked_workloads(pods, receipt):
    controllers, identities, production_pods = {}, {}, []
    active = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    used = set()
    for name, (kind, uid, digest) in WORKLOADS.items():
        controller = get_object(kind, name, NAMESPACE)
        require(controller and controller["metadata"]["uid"] == uid and not controller["metadata"].get("ownerReferences") and
                not controller["metadata"].get("deletionTimestamp") and controller["spec"].get("replicas") == 1,
                "inventoried single writer/controller changed: " + name)
        status = controller.get("status", {})
        require(status.get("observedGeneration", 0) >= controller["metadata"]["generation"] and
                all(status.get(key, 0) == 1 for key in ("replicas", "readyReplicas", "updatedReplicas")),
                "workload rollout is unfinished: " + name)
        spec = controller["spec"]["template"]["spec"]
        definitions = spec.get("containers", [])
        require(len(definitions) == 1 and definitions[0].get("name") == name and
                definitions[0].get("image", "").endswith("@sha256:" + digest) and
                spec.get("nodeSelector") == {"kubernetes.io/hostname": "openmediavault"},
                "workload image, container, or placement changed: " + name)
        if kind == "deployment":
            require(controller["spec"].get("strategy", {}).get("type") == "Recreate", "writer strategy changed: " + name)
        else:
            retention = controller["spec"].get("persistentVolumeClaimRetentionPolicy", {})
            require(retention.get("whenScaled", "Retain") == retention.get("whenDeleted", "Retain") == "Retain",
                    "PostgreSQL PVC retention changed")
        matches = []
        for pod in active:
            if not any(item.get("name") == name for item in pod["spec"].get("containers", [])):
                continue
            owners = [item for item in pod["metadata"].get("ownerReferences", []) if item.get("controller")]
            require(len(owners) == 1, "Pod controller ownership is ambiguous: " + name)
            owner = owners[0]
            if kind == "statefulset":
                belongs = owner.get("kind") == "StatefulSet" and owner.get("uid") == uid
            else:
                require(owner.get("kind") == "ReplicaSet", "Deployment Pod is not owned by a ReplicaSet: " + name)
                replica_set = get_object("replicaset", owner["name"], NAMESPACE)
                belongs = bool(replica_set and replica_set["metadata"]["uid"] == owner.get("uid") and
                               any(item.get("controller") and item.get("kind") == "Deployment" and item.get("uid") == uid
                                   for item in replica_set["metadata"].get("ownerReferences", [])))
            require(belongs, "Pod belongs to a different workload: " + name)
            matches.append(pod)
        require(len(matches) == 1 and ready(matches[0]) and matches[0]["spec"].get("nodeName") == "openmediavault" and
                matches[0].get("status", {}).get("phase") == "Running", "expected one ready OMV Pod: " + name)
        pod = matches[0]
        definition = next(item for item in pod["spec"]["containers"] if item["name"] == name)
        container = next((item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == name), {})
        require(container.get("ready") is True and container.get("state", {}).get("running") and
                container.get("containerID") and container.get("imageID", "").endswith("@sha256:" + digest) and
                type(container.get("restartCount")) is int and container["restartCount"] >= 0 and
                definition.get("resources") == definitions[0].get("resources"),
                "workload container is unstable or uses different resources: " + name)
        volumes = {item["name"]: item for item in pod["spec"].get("volumes", [])}
        mounts = {}
        for mount in definition.get("volumeMounts", []):
            claim_ref = volumes[mount["name"]].get("persistentVolumeClaim")
            if not claim_ref:
                continue
            require(not mount.get("readOnly") and not mount.get("subPathExpr") and not claim_ref.get("readOnly"),
                    "protected volume is not an ordinary writable claim: " + name)
            mounts[mount["mountPath"]] = (claim_ref["claimName"], mount.get("subPath"))
        require(mounts == MOUNTS[name], "protected writable mounts changed: " + name)
        if name == "paperless-ai":
            require(pod["metadata"]["uid"] == receipt["pod_uid"] and container["imageID"] == receipt["image_id"],
                    "AI Pod/image changed since its private schema capture")
        if name == "gotenberg":
            require(definition.get("resources", {}).get("requests") in (ORIGINAL_REQUESTS, TARGET_REQUESTS) and
                    definition.get("resources", {}).get("limits") == LIMITS, "Gotenberg requests or limits changed")
        used.add(pod["metadata"]["uid"])
        production_pods.append(pod)
        controllers[name] = controller
        identities[name] = {"controller_uid": uid, "spec_sha256": fingerprint(controller["spec"]),
                            "pod_uid": pod["metadata"]["uid"], "container_id": container["containerID"],
                            "image_id": container["imageID"], "restarts": container["restartCount"]}
    for pod in active:
        claims = {item.get("persistentVolumeClaim", {}).get("claimName") for item in pod["spec"].get("volumes", [])}
        require(not claims.intersection(CLAIMS) or pod["metadata"]["uid"] in used,
                "an additional live Pod mounts protected source storage")
    return controllers, identities, production_pods


def live_preflight(*, check_resources=True, record=True):
    nodes = json.loads(kube("get", "nodes", "-o", "json", capture=True).stdout)["items"]
    require([node["metadata"]["name"] for node in nodes] == ["openmediavault"], "additional nodes require their later approval gate")
    node = nodes[0]
    require(not node["spec"].get("unschedulable") and
            not any(item.get("effect") in ("NoSchedule", "NoExecute") for item in node["spec"].get("taints", [])) and
            any(item.get("type") == "Ready" and item.get("status") == "True" for item in node["status"].get("conditions", [])) and
            not any(item.get("status") == "True" for item in node["status"].get("conditions", [])
                    if item.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure")), "OMV is not ready or reports node pressure")
    available = next((int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemAvailable:")), 0)
    require(available >= 2 * 1024**3, "less than 2 GiB physical memory is available for backup work")
    bindings, evidence, receipt = checked_sources()
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]
    controllers, workloads, production_pods = checked_workloads(pods, receipt)
    require(not json.loads(kube("get", "hpa", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"],
            "namespace autoscaling requires inspection before setup")
    if check_resources:
        resource_budget(node, pods)
    identities = {"bindings": bindings, "workloads": workloads}
    if record:
        for name, value in (*evidence, *((name + "." + value["kind"].lower() + ".json", value) for name, value in controllers.items())):
            target = RECOVERY / name
            if target.exists() or target.is_symlink():
                require(json.loads(private_read(target))["metadata"]["uid"] == value["metadata"]["uid"], "saved source identity changed: " + name)
            private_write(target, json.dumps(value, indent=2) + "\n")
        capture_dependencies(production_pods)
        private_write(RECOVERY / "source-bindings.json", json.dumps(identities, indent=2) + "\n")
    return identities


def adjust_resources():
    before = get_object("deployment", "gotenberg", NAMESPACE)
    resources = before["spec"]["template"]["spec"]["containers"][0]["resources"]
    path = RECOVERY / "resource-adjustment.before.json"
    if path.exists() or path.is_symlink():
        original = json.loads(private_read(path))
        permitted = json.loads(json.dumps(original["spec"]))
        permitted["template"]["spec"]["containers"][0]["resources"]["requests"] = TARGET_REQUESTS
        require(original["metadata"]["uid"] == before["metadata"]["uid"] and before["spec"] in (original["spec"], permitted),
                "Gotenberg changed beyond the saved request adjustment")
    try:
        live_preflight(record=False)
    except CapacityError:
        require(resources["requests"] == ORIGINAL_REQUESTS, "capacity is still insufficient after the permitted Gotenberg adjustment")
    else:
        if path.exists() and resources["requests"] == TARGET_REQUESTS:
            verify_adjustment(before, before["spec"], None)
        else:
            print("current reservations fit the endpoint and worker; Gotenberg requests retained", flush=True)
        return
    identities = live_preflight(check_resources=False, record=False)
    baseline = identities["workloads"]["gotenberg"]
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]
    pod_name = next(pod["metadata"]["name"] for pod in pods if pod["metadata"]["uid"] == baseline["pod_uid"])
    samples = []
    for attempt in range(3):
        metrics = json.loads(kube("get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/" + NAMESPACE + "/pods/" + pod_name,
                                  capture=True, sensitive=True).stdout)
        entries = [item for item in metrics.get("containers", []) if item.get("name") == "gotenberg"]
        require(metrics.get("metadata", {}).get("name") == pod_name and len(entries) == 1,
                "Gotenberg resource metrics are unavailable")
        samples.append({key: quantity(entries[0]["usage"][key]) for key in ("cpu", "memory")})
        if attempt < 2:
            time.sleep(5)
    require(all(sample["memory"] < quantity(TARGET_REQUESTS["memory"]) / 2 and sample["cpu"] < quantity(TARGET_REQUESTS["cpu"])
                for sample in samples), "current converter usage does not support the proposed reservation")
    require(live_preflight(check_resources=False, record=False) == identities, "production changed while sampling converter usage")
    latest = get_object("deployment", "gotenberg", NAMESPACE)
    require(latest["metadata"]["uid"] == before["metadata"]["uid"] and latest["spec"] == before["spec"], "Gotenberg changed during sampling")
    before = latest
    if not path.exists():
        private_write(path, json.dumps(before, indent=2) + "\n")
    expected = json.loads(json.dumps(before["spec"]))
    expected["template"]["spec"]["containers"][0]["resources"]["requests"] = TARGET_REQUESTS
    patch = [{"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": before["metadata"]["resourceVersion"]},
             {"op": "test", "path": "/spec", "value": before["spec"]},
             {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests", "value": TARGET_REQUESTS}]
    print("adjusting only Gotenberg requests to 225m/480Mi; existing 2 CPU/2Gi limits retained", flush=True)
    kube("patch", "deployment", "gotenberg", "-n", NAMESPACE, "--type=json", "--patch-file=/dev/stdin",
         stdin=json.dumps(patch), sensitive=True)
    verify_adjustment(before, expected, max(int(sample["memory"]) for sample in samples), new_rollout=True)
    after = live_preflight(record=False)
    for name, identity in identities["workloads"].items():
        if name != "gotenberg":
            require(after["workloads"][name] == identity, "an unrelated workload changed during converter adjustment: " + name)
    require(after["bindings"] == identities["bindings"], "protected storage changed during converter adjustment")


def verify_adjustment(before, expected_spec, peak, *, new_rollout=False):
    kube("rollout", "status", "-n", NAMESPACE, "deployment/gotenberg", "--timeout=240s", timeout=270)
    current = get_object("deployment", "gotenberg", NAMESPACE)
    require(current["metadata"]["uid"] == before["metadata"]["uid"] and current["spec"] == expected_spec,
            "converter changed beyond its request patch")
    baseline = live_preflight(check_resources=False, record=False)
    if new_rollout:
        require(baseline["workloads"]["gotenberg"]["restarts"] == 0, "replacement converter container restarted")
    print("converter is ready; observing all production identities for one minute", flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        require(live_preflight(check_resources=False, record=False) == baseline, "production changed during converter stability observation")
        time.sleep(5)
    private_write(RECOVERY / "resource-adjustment.json", json.dumps({"result": "passed", "component": "gotenberg",
        "requests": TARGET_REQUESTS, "limits": LIMITS, "peak_sampled_memory_bytes": peak,
        "completed_at": datetime.now(timezone.utc).isoformat(), "production": baseline}, indent=2) + "\n")
    print("converter request adjustment verified; original deployment saved privately", flush=True)


def prepare():
    global PHASE
    PHASE = "checking existing repository workers"
    clients = json.loads(kube("get", "pods", "-A", "-l", "backups.mousses.xyz/restic-client=true", "-o", "json", capture=True).stdout)["items"]
    require(not any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in clients),
            "a repository worker survived its process lock; wait for it to finish before setup")
    PHASE = "checking source bindings, workloads, and recovery evidence"
    live_preflight(check_resources=False)
    # Reject storage/resource-name collisions before the optional converter rollout.
    PHASE = "checking backup resource names and storage"
    render_resources()
    PHASE = "preparing private repository credentials"
    fields = prepare_credentials()
    PHASE = "checking capacity and converter requests"
    adjust_resources()
    original = live_preflight()
    PHASE = "preparing array directories"
    for directory, uid, gid, mode in DIRECTORIES:
        if directory.exists():
            attrs = directory.stat()
            require((attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) == (uid, gid, mode),
                    "existing backup directory ownership or mode differs: " + str(directory))
        else:
            directory.mkdir(parents=True)
            os.chown(directory, uid, gid)
            os.chmod(directory, mode)
        mounted_on_array(directory)
        print("array directory prepared: " + str(directory), flush=True)
    PHASE = "creating repository credentials and endpoint resources"
    for name, values in fields.items():
        ensure_secret(name, values)
    kube("apply", "-k", str(MANIFESTS))
    claim_names = ["restic-repositories", *(component + "-backup-staging" for component in PROTECTED)]
    PHASE = "waiting for backup storage bindings"
    kube("wait", "-n", NAMESPACE, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s", *("pvc/" + name for name in claim_names))
    for name in claim_names:
        claim = get_object("pvc", name, NAMESPACE)
        volume = get_object("pv", claim["spec"]["volumeName"])
        require(volume["spec"].get("claimRef", {}).get("uid") == claim["metadata"]["uid"], "new backup binding UID differs: " + name)
    PHASE = "waiting for endpoint certificates and rollout"
    kube("wait", "-n", NAMESPACE, "--for=condition=Ready", "--timeout=180s", "certificate/restic-root-ca", "certificate/restic-server-tls")
    kube("rollout", "status", "-n", NAMESPACE, "deployment/restic-server", "--timeout=180s")
    for name in fields:
        save_secret(name)
    save_secret("restic-server-tls", refresh=True)
    ca = save_secret("restic-root-ca")
    private_write(RECOVERY / "ca.crt", base64.b64decode(ca["data"]["tls.crt"], validate=True).decode())
    print("repository credentials and source dependencies saved privately; do not paste them", flush=True)
    repository_checks()
    PHASE = "verifying final production identities"
    final = live_preflight(check_resources=False)
    require(final == original, "production identity/storage changed during endpoint setup")
    result = {"result": "passed", "namespace": NAMESPACE, "repositories": list(CLAIMS), "production": final,
              "completed_at": datetime.now(timezone.utc).isoformat(), "production_unchanged_during_endpoint_setup": True}
    private_write(RECOVERY / "endpoint-verification.json", json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "production"}, indent=2), flush=True)
    print("all seven repositories and array staging verified; initial data backups and isolated restores remain pending", flush=True)


def main():
    require(os.geteuid() == 0 and platform.system() == "Linux" and platform.node().split(".")[0] == "openmediavault",
            "run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    recovery_directory()
    for command in ("k3s", "findmnt", "htpasswd"):
        require(shutil.which(command), "required command is missing: " + command)
    require(BASE.is_dir() and MANIFESTS.is_dir(), "approved namespace path or downloaded backup manifests are missing")
    mounted_on_array(BASE)
    require(shutil.disk_usage(BASE).free >= 100 * 1024**3, "less than 100 GiB remains on the approved array")
    with ExitStack() as locks:
        locks.enter_context(operation_lock(RECOVERY / ".setup.lock", create=True))
        locks.enter_context(operation_lock(COORDINATION_LOCK, create=False))
        print("Paperless endpoint setup serialized with existing namespace backup workers", flush=True)
        prepare()


if __name__ == "__main__":
    try:
        main()
    except SetupError as error:
        print(json.dumps({"result": "stopped", "phase": PHASE, "reason": str(error)}), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(json.dumps({"result": "stopped", "phase": PHASE, "reason": "interrupted; private evidence and source claims retained"}), file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(json.dumps({"result": "stopped", "phase": PHASE, "error_type": type(error).__name__,
                          "reason": "filesystem, API, or configuration validation failed; private values omitted"}), file=sys.stderr)
        sys.exit(1)
