#!/usr/bin/env python3
"""Prepare the approved OMV-only board-games backup endpoint and storage."""

import base64
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time


NAMESPACE = "board-games"
MANAGER = "nas-backup-migration"
BASE = Path("/filesystem/k3s/data/board-games")
RECOVERY = Path("/root/k3s-backup-recovery/board-games/data-postgres-0")
REPOSITORY = "rest:https://restic.board-games.svc.cluster.local:8000/data-postgres-0/"
LIVE_PV = "pvc-3632ee17-7a63-47fa-9314-06548af1266b"
MANIFESTS = Path(__file__).resolve().parents[2] / "apps/board-games/backups"
DIRECTORIES = (
    (BASE / "backups", 65534, 65534, 0o2770),
    (BASE / "backups/data-postgres-0", 65534, 65534, 0o700),
    (BASE / "backup-staging/data-postgres-0", 999, 999, 0o2770),
    (BASE / "backup-cache/data-postgres-0/source", 999, 999, 0o2770),
)


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
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_secret(name):
    secret = get_object("secret", name, NAMESPACE, sensitive=True)
    if not secret:
        raise SetupError(f"expected Secret is missing: {name}")
    portable = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "type": secret.get("type", "Opaque"), "data": secret["data"],
    }
    private_write(RECOVERY / f"{name}.secret.json", json.dumps(portable, indent=2) + "\n")
    return secret


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


def wait_for_job(name, timeout=330):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_object("job", name, NAMESPACE)
        if not job:
            raise SetupError(f"verification job disappeared: job/{name}")
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("status") != "True":
                continue
            if condition["type"] == "Complete":
                print(f"job/{name} complete", flush=True)
                return
            if condition["type"] == "Failed":
                reason = condition.get("reason", "unknown failure")
                raise SetupError(f"repository verification failed: job/{name}: {reason}")
        time.sleep(2)
    raise SetupError(f"repository verification timed out: job/{name}")


def endpoint_diagnostics():
    print("collecting Restic service diagnostics", flush=True)
    kube("get", "service", "restic", "-n", NAMESPACE, "-o", "yaml", check=False)
    kube("get", "endpointslices", "-n", NAMESPACE,
         "-l", "kubernetes.io/service-name=restic", "-o", "yaml", check=False)
    kube("get", "pods", "-n", NAMESPACE, "-l", "app.kubernetes.io/name=restic-server",
         "-o", "wide", check=False)
    kube("logs", "-n", NAMESPACE, "deployment/restic-server", "--tail=100",
         "--timestamps=true", check=False)


def main():
    if os.geteuid() != 0:
        raise SetupError("run with sudo python3")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for command in ("k3s", "findmnt", "htpasswd"):
        if not shutil.which(command):
            raise SetupError(f"required command is missing: {command}")
    if not BASE.is_dir() or not MANIFESTS.is_dir():
        raise SetupError("approved NAS data path or downloaded manifests are missing")
    mounted_on_array(BASE)
    if shutil.disk_usage(BASE).free < 20 * 1024**3:
        raise SetupError("less than 20 GiB free on the approved array")

    nodes = json.loads(kube("get", "nodes", "-o", "json", capture=True).stdout)
    if [node["metadata"]["name"] for node in nodes["items"]] != ["openmediavault"]:
        raise SetupError("expected only openmediavault; other nodes require the later approval gate")
    live_claim = get_object("pvc", "data-postgres-0", NAMESPACE)
    live_volume = get_object("pv", LIVE_PV)
    if (not live_claim or live_claim["spec"].get("volumeName") != LIVE_PV or
            live_claim.get("status", {}).get("phase") != "Bound" or
            not live_volume or live_volume.get("status", {}).get("phase") != "Bound" or
            live_volume["spec"].get("claimRef", {}).get("uid") != live_claim["metadata"]["uid"] or
            live_volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise SetupError("live database binding or Retain safeguard differs from the verified inventory")

    recovery_directory()
    lock_path = RECOVERY / ".setup.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("another board-games backup setup is already running") from error
        prepare()


def prepare():
    credential_path = RECOVERY / "credentials.json"
    have_credentials = credential_path.exists() or credential_path.is_symlink()
    credentials = json.loads(private_read(credential_path)) if have_credentials else None

    for path, uid, gid, mode in DIRECTORIES:
        checked_directory(path)
        if path.exists() and any(path.iterdir()):
            if not have_credentials:
                raise SetupError(f"non-empty directory has no saved backup credentials; preserving it: {path}")
            attributes = path.stat()
            if attributes.st_uid != uid or attributes.st_gid != gid:
                raise SetupError(f"existing backup directory ownership is unexpected: {path}")

    # Check names before applying resources, rather than adopting an unrelated object.
    rendered = kube(
        "create", "--dry-run=client", "-k", str(MANIFESTS), "-o", "json", capture=True,
    ).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    # kubectl create can print a stream of separate JSON objects for a bundle.
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if not resources:
        raise SetupError("downloaded backup configuration contains no resources")
    for resource in resources:
        metadata = resource["metadata"]
        api_group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        kind = resource["kind"].lower() + ("." + api_group if api_group else "")
        existing = get_object(kind, metadata["name"], metadata.get("namespace"))
        if existing and existing["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != MANAGER:
            raise SetupError(f"existing {kind}/{metadata['name']} is not managed by this migration")

    if not have_credentials:
        for name in ("restic-root-ca", "restic-server-tls", "restic-server-auth", "restic-data-postgres-0"):
            if get_object("secret", name, NAMESPACE, sensitive=True):
                raise SetupError(f"existing Secret has no saved recovery credentials; preserving it: {name}")
        password = secrets.token_hex(32)
        htpasswd = run(
            ["htpasswd", "-n", "-i", "-B", "-C", "12", "data-postgres-0"],
            stdin=password + "\n", capture=True, sensitive=True,
        ).stdout.strip() + "\n"
        credentials = {
            "RESTIC_REPOSITORY": REPOSITORY,
            "RESTIC_PASSWORD": secrets.token_hex(32),
            "RESTIC_REST_USERNAME": "data-postgres-0",
            "RESTIC_REST_PASSWORD": password,
            "htpasswd": htpasswd,
        }
        private_write(credential_path, json.dumps(credentials, indent=2) + "\n")

    if (credentials.get("RESTIC_REPOSITORY") != REPOSITORY or
            credentials.get("RESTIC_REST_USERNAME") != "data-postgres-0" or
            not all(credentials.get(key) for key in (
                "RESTIC_PASSWORD", "RESTIC_REST_PASSWORD", "htpasswd"))):
        raise SetupError("saved backup credentials are incomplete or target a different repository")

    for path, uid, gid, mode in DIRECTORIES:
        path.mkdir(parents=True, exist_ok=True)
        # Deliberately affect only these new backup roots; never recurse into live data.
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        mounted_on_array(path)
        print(f"array directory prepared: {path}", flush=True)

    ensure_secret("restic-server-auth", {".htpasswd": credentials["htpasswd"]})
    ensure_secret("restic-data-postgres-0", {
        key: value for key, value in credentials.items() if key != "htpasswd"
    })
    kube("apply", "-k", str(MANIFESTS))
    kube("wait", "-n", NAMESPACE, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s",
         "pvc/restic-repositories", "pvc/postgres-backup-staging")
    kube("wait", "-n", NAMESPACE, "--for=condition=Ready", "--timeout=180s",
         "certificate/restic-root-ca", "certificate/restic-server-tls")
    kube("rollout", "status", "-n", NAMESPACE, "deployment/restic-server", "--timeout=180s")

    for name in ("restic-server-auth", "restic-data-postgres-0", "restic-server-tls"):
        save_secret(name)
    ca = save_secret("restic-root-ca")
    private_write(RECOVERY / "ca.crt", base64.b64decode(ca["data"]["tls.crt"]).decode())
    print(f"recovery credentials and TLS secrets saved at {RECOVERY}; do not paste their contents", flush=True)

    job = json.loads(kube(
        "create", "-f", str(MANIFESTS / "repository-check.yaml"), "-o", "json", capture=True,
    ).stdout)
    job_name = job["metadata"]["name"]
    print(f"job/{job_name} created", flush=True)
    try:
        wait_for_job(job_name)
    except SetupError:
        kube("logs", "-n", NAMESPACE, "job/" + job_name, check=False)
        endpoint_diagnostics()
        raise
    kube("logs", "-n", NAMESPACE, "job/" + job_name, check=False)

    kube("get", "pods", "-n", NAMESPACE, "-l", "app.kubernetes.io/name=restic-server", "-o", "wide")
    kube("get", "pvc", "-n", NAMESPACE, "restic-repositories", "postgres-backup-staging")
    kube("get", "pv", "board-games-restic-repositories", "board-games-postgres-backup-staging",
         "board-games-postgres-backup-cache", "-o", "wide")
    print("board-games backup endpoint and array storage verified; database backup/restore is still pending", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"setup stopped: {error}", file=sys.stderr)
        sys.exit(1)
