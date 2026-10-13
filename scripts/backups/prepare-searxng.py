#!/usr/bin/env python3
"""Prepare searxng's two approved array-backed Restic repositories and staging PVCs."""

import base64
from decimal import Decimal
import fcntl
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


NAMESPACE = "searxng"
MANAGER = "nas-backup-migration"
BASE = Path("/filesystem/k3s/data/searxng")
RECOVERY = Path("/root/k3s-backup-recovery/searxng")
MANIFESTS = Path(__file__).resolve().parents[2] / "apps/searxng/backups"
CLAIMS = ("searxng-cache", "searxng-valkey")
IMAGES = {
    "searxng": "docker.io/searxng/searxng@sha256:7914267d4a3b91132aa888b889dbe0657bdb9e1af5a13eb6fbab99a94990c235",
    "valkey": "docker.io/valkey/valkey@sha256:d1cc70645bbcef743615463a2fa4616e841407545e18f560aed0c49671a90147",
}
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
    result = subprocess.run(
        arguments, input=stdin, text=True, capture_output=True, timeout=timeout,
    )
    if not capture and not sensitive and result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr and not sensitive:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if check and result.returncode:
        # Arguments never contain passwords; secret manifests go through stdin.
        raise SetupError("command failed: " + " ".join(arguments))
    return result


def kube(*arguments, **options):
    return run(["k3s", "kubectl", *arguments], **options)


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
    attributes = path.lstat()
    if not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0:
        raise SetupError(f"expected a regular root-owned recovery file: {path}")
    if stat.S_IMODE(attributes.st_mode) != 0o600:
        raise SetupError(f"recovery file must have mode 0600: {path}")
    return path.read_text()


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
                         **({"namespace": NAMESPACE} if resource["metadata"].get("namespace") else {})},
            **{key: resource[key] for key in ("spec", "data", "binaryData", "type") if key in resource}}


def save_secret(name, *, refresh=False):
    secret = get_object("secret", name, NAMESPACE, sensitive=True)
    if not secret:
        raise SetupError("required Secret is missing: " + name)
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
    servers = [pod for pod in pods
               if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "restic-server"
               and pod["spec"].get("nodeName") == "openmediavault"
               and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    if len(servers) > 1:
        raise SetupError("more than one Restic server Pod is reserving node resources")
    if servers and servers[0]["spec"]["containers"][0].get("resources", {}).get("requests") != {
            "cpu": "50m", "memory": "64Mi"}:
        raise SetupError("existing Restic server reservations differ from this setup")
    # Reserve the server plus one 50m/128Mi worker; check Jobs only use 64Mi.
    needed = {"cpu": Decimal("0.05") + (0 if servers else Decimal("0.05")),
              "memory": (128 + (0 if servers else 64)) * 1024**2}
    available = {key: quantity(node["status"]["allocatable"][key]) - quantity(allocated[key])
                 for key in needed}
    print("available reservations: " + str(available["cpu"] * 1000) + "m CPU, " +
          str(round(available["memory"] / 1024**2, 1)) + "Mi memory", flush=True)
    if any(available[key] < needed[key] for key in needed):
        raise SetupError("insufficient reservations for the backup endpoint plus one worker; no resources applied")


def live_preflight():
    nodes = json.loads(kube("get", "nodes", "-o", "json", capture=True).stdout)["items"]
    if [node["metadata"]["name"] for node in nodes] != ["openmediavault"]:
        raise SetupError("additional nodes require the later approval gate")
    node = nodes[0]
    if node["spec"].get("unschedulable") or not any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in node.get("status", {}).get("conditions", [])):
        raise SetupError("OMV is not ready and schedulable")
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]
    saved = []
    for component, claim_name, uid, gid in (("searxng", "searxng-cache", 977, 977),
                                           ("valkey", "searxng-valkey", 999, 1000)):
        expected_path = BASE / ("cache" if component == "searxng" else "valkey")
        claim = get_object("pvc", claim_name, NAMESPACE)
        volume = get_object("pv", claim_name)
        if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
                volume.get("status", {}).get("phase") != "Bound" or
                claim["spec"].get("volumeName") != claim_name or
                volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
                volume["spec"].get("local", {}).get("path") != str(expected_path) or
                volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
                any(item["spec"].get("accessModes") != ["ReadWriteOncePod"] for item in (claim, volume))):
            raise SetupError("live binding differs from the verified inventory: " + claim_name)
        checked_directory(expected_path)
        if not expected_path.is_dir():
            raise SetupError("live volume directory is missing: " + str(expected_path))
        owner = expected_path.stat()
        if owner.st_uid != uid or owner.st_gid != gid:
            raise SetupError("live data directory ownership changed: " + claim_name)
        candidates = [pod for pod in pods
                      if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == component
                      and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        if len(candidates) != 1 or not ready(candidates[0]) or candidates[0]["spec"].get("nodeName") != "openmediavault":
            raise SetupError("expected exactly one ready production Pod on OMV: " + component)
        pod = candidates[0]
        containers = [item for item in pod["spec"]["containers"] if item["name"] == component]
        mounts = {item["name"]: item for item in pod["spec"].get("volumes", [])}
        expected_mount = "/var/cache/searxng" if component == "searxng" else "/data"
        if (len(containers) != 1 or containers[0]["image"] != IMAGES[component] or
                not any(mount.get("mountPath") == expected_mount and not mount.get("readOnly") and
                        not mount.get("subPath") and not mount.get("subPathExpr") and
                        mounts[mount["name"]].get("persistentVolumeClaim", {}).get("claimName") == claim_name and
                        not mounts[mount["name"]]["persistentVolumeClaim"].get("readOnly")
                        for mount in containers[0].get("volumeMounts", []))):
            raise SetupError("production image or writable claim mount changed: " + component)
        kind = "deployment" if component == "searxng" else "statefulset"
        workload = get_object(kind, component, NAMESPACE)
        if not workload or workload["spec"].get("replicas") != 1:
            raise SetupError("production replica count changed: " + component)
        saved.extend(((claim_name + ".pvc.json", claim), (claim_name + ".pv.json", volume),
                      (component + "." + kind + ".json", workload)))
    resource_budget(node, pods)
    for filename, resource in saved:
        path = RECOVERY / filename
        if not path.exists() and not path.is_symlink():
            private_write(path, json.dumps(portable(resource), indent=2) + "\n")
    save_secret("searxng-secret")
    config = get_object("configmap", "searxng-config", NAMESPACE, sensitive=True)
    if not config:
        raise SetupError("required SearXNG ConfigMap is missing")
    private_write(RECOVERY / "searxng-config.configmap.json", json.dumps(portable(config), indent=2) + "\n")


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
                        "persistentVolumeReclaimPolicy", "nodeAffinity", "provisioner", "reclaimPolicy", "volumeBindingMode"):
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
    repository = "rest:https://restic.searxng.svc:8000/" + claim + "/"
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
                "RESTIC_REPOSITORY": "rest:https://restic.searxng.svc:8000/" + claim + "/",
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


def wait_for_job(name):
    started = time.monotonic()
    while time.monotonic() - started < 330:
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
        job = json.loads(json.dumps(template))
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["envFrom"] = [{"secretRef": {"name": "restic-" + claim}}]
        container["env"] = [{"name": "EXPECTED_RESTIC_IPS", "value": expected},
                            {"name": "OTHER_REPOSITORY", "value": next(name for name in CLAIMS if name != claim)}]
        created = json.loads(kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
        name = created["metadata"]["name"]
        print("job/" + name + " created (" + claim + " DNS/TLS/authentication/isolation check)", flush=True)
        try:
            wait_for_job(name)
        finally:
            print_check_logs(name)


def prepare():
    live_preflight()
    render_resources()
    fields = prepare_credentials()
    for directory, uid, gid, mode in DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
        # Only new backup roots: no recursive ownership changes to live data.
        os.chown(directory, uid, gid)
        os.chmod(directory, mode)
        mounted_on_array(directory)
        print("array directory prepared: " + str(directory), flush=True)
    for name, values in fields.items():
        ensure_secret(name, values)
    kube("apply", "-k", str(MANIFESTS))
    kube("wait", "-n", NAMESPACE, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s",
         "pvc/restic-repositories", "pvc/cache-backup-staging", "pvc/valkey-backup-staging")
    for name in ("restic-repositories", "cache-backup-staging", "valkey-backup-staging"):
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
    result = {"result": "passed", "namespace": NAMESPACE, "repositories": list(CLAIMS),
              "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    private_write(RECOVERY / "endpoint-verification.json", json.dumps(result, indent=2) + "\n")
    kube("get", "pods", "-n", NAMESPACE, "-o", "wide")
    kube("get", "pvc", "-n", NAMESPACE)
    print("both searxng repositories and array-backed staging verified; initial backups and restores remain pending", flush=True)


def main():
    if os.geteuid() != 0:
        raise SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for command in ("k3s", "findmnt", "htpasswd"):
        if not shutil.which(command):
            raise SetupError("required command is missing: " + command)
    if not BASE.is_dir() or not MANIFESTS.is_dir():
        raise SetupError("approved NAS namespace path or downloaded manifests are missing")
    mounted_on_array(BASE)
    if shutil.disk_usage(BASE).free < 20 * 1024**3:
        raise SetupError("less than 20 GiB remains on the approved array")
    recovery_directory()
    descriptor = os.open(RECOVERY / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("another searxng backup operation holds the namespace lock") from error
        prepare()


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("setup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
