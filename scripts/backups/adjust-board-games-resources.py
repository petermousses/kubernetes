#!/usr/bin/env python3
"""Make room for the approved backup pilot without changing application versions."""

import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


NAMESPACE = "board-games"
MANIFESTS = Path(__file__).resolve().parents[2] / "apps/board-games"
RECOVERY = Path("/root/k3s-backup-recovery/board-games/resource-adjustment")
TARGETS = (
    ("board-games-api", "api", "api.yaml"),
    ("board-games-web", "web", "web.yaml"),
)


class AdjustmentError(Exception):
    pass


def kube(*arguments):
    result = subprocess.run(
        ["k3s", "kubectl", *arguments], text=True, capture_output=True, timeout=300,
    )
    if result.returncode:
        raise AdjustmentError(result.stderr.strip() or "kubectl command failed")
    return result.stdout


def json_stream(content):
    decoder = json.JSONDecoder()
    documents = []
    content = content.lstrip()
    while content:
        document, end = decoder.raw_decode(content)
        documents.extend(document.get("items", [document]))
        content = content[end:].lstrip()
    return documents


def repository(image):
    parts = image.split("@", 1)[0].rsplit("/", 1)
    parts[-1] = parts[-1].split(":", 1)[0]
    return "/".join(parts)


def image_pin(image_id, expected_repository):
    image_id = image_id.removeprefix("docker-pullable://").removeprefix("containerd://")
    if "@" in image_id:
        actual_repository, digest = image_id.rsplit("@", 1)
        if actual_repository != expected_repository:
            raise AdjustmentError("running image repository differs from the deployment")
    else:
        digest = image_id
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise AdjustmentError("could not determine the running image digest safely")
    return expected_repository + "@" + digest


def prepare_changes():
    changes = []
    for name, container_name, filename in TARGETS:
        rendered = json_stream(kube(
            "create", "--dry-run=client", "-n", NAMESPACE,
            "-f", str(MANIFESTS / filename), "-o", "json",
        ))
        desired = next(item for item in rendered if item["kind"] == "Deployment")
        live = json.loads(kube("get", "deployment", name, "-n", NAMESPACE, "-o", "json"))
        if desired["metadata"]["name"] != name or live["spec"].get("replicas") != 2:
            raise AdjustmentError(f"unexpected deployment name or replica count: {name}")
        status = live.get("status", {})
        if (status.get("observedGeneration") != live["metadata"]["generation"] or
                any(status.get(key) != 2 for key in (
                    "updatedReplicas", "readyReplicas", "availableReplicas"))):
            raise AdjustmentError(f"both replicas must be healthy before adjustment: {name}")
        desired_container = next(
            item for item in desired["spec"]["template"]["spec"]["containers"]
            if item["name"] == container_name
        )
        live_container = next(
            item for item in live["spec"]["template"]["spec"]["containers"]
            if item["name"] == container_name
        )
        pods = json.loads(kube(
            "get", "pods", "-n", NAMESPACE, "-l", "app.kubernetes.io/name=" + name,
            "-o", "json",
        ))["items"]
        active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        if len(active) != 2:
            raise AdjustmentError(f"expected exactly two active application pods: {name}")
        pins = set()
        for pod in active:
            if pod["spec"].get("nodeName") != "openmediavault":
                raise AdjustmentError(f"application pod is not on OMV: {name}")
            container_status = next(
                item for item in pod.get("status", {}).get("containerStatuses", [])
                if item["name"] == container_name
            )
            if not container_status.get("ready") or "running" not in container_status.get("state", {}):
                raise AdjustmentError(f"application container is not ready: {name}")
            pins.add(image_pin(container_status["imageID"], repository(live_container["image"])))
        if len(pins) != 1:
            raise AdjustmentError(f"replicas run different images; preserving them for inspection: {name}")
        requests = desired_container["resources"]["requests"]
        if live_container["resources"].get("limits") != desired_container["resources"].get("limits"):
            raise AdjustmentError(f"live resource limits differ from the reviewed configuration: {name}")
        changes.append((name, live, {
            "metadata": {"resourceVersion": live["metadata"]["resourceVersion"]},
            "spec": {
                "strategy": desired["spec"]["strategy"],
                "template": {"spec": {"containers": [{
                    "name": container_name, "image": pins.pop(),
                    "resources": {"requests": requests},
                }]}},
            },
        }))
    return changes


def recovery_directory():
    current = Path("/root")
    for component in RECOVERY.relative_to(current).parts:
        current /= component
        if current.is_symlink():
            raise AdjustmentError(f"unexpected recovery directory symlink: {current}")
        if not current.exists():
            current.mkdir(mode=0o700)
        attributes = current.stat()
        if (not current.is_dir() or attributes.st_uid != 0 or
                stat.S_IMODE(attributes.st_mode) != 0o700):
            raise AdjustmentError(f"recovery directory must be root-owned with mode 0700: {current}")


def adjust(changes):
    # Keep the original deployment records across repeated setup attempts.
    for name, original, patch in changes:
        path = RECOVERY / (name + ".before.json")
        if path.exists() or path.is_symlink():
            attributes = path.lstat()
            if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0 or
                    stat.S_IMODE(attributes.st_mode) != 0o600):
                raise AdjustmentError(f"unexpected recovery file: {path}")
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(json.dumps(original, indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
    for name, original, patch in changes:
        print(kube("patch", "deployment", name, "-n", NAMESPACE,
                   "--type=strategic", "--patch", json.dumps(patch)), end="", flush=True)
        print(kube("rollout", "status", "-n", NAMESPACE,
                   "deployment/" + name, "--timeout=240s"), end="", flush=True)
    print(f"both deployments ready; original settings saved under {RECOVERY}", flush=True)
    print(kube("get", "pods", "-n", NAMESPACE, "-o", "wide"), end="", flush=True)


def main():
    if os.geteuid() != 0:
        raise AdjustmentError("run with sudo python3")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    nodes = json.loads(kube("get", "nodes", "-o", "json"))
    if [node["metadata"]["name"] for node in nodes["items"]] != ["openmediavault"]:
        raise AdjustmentError("expected only openmediavault")
    recovery_directory()
    descriptor = os.open(RECOVERY / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        adjust(prepare_changes())


if __name__ == "__main__":
    try:
        main()
    except (AdjustmentError, OSError, ValueError, KeyError, StopIteration,
            subprocess.TimeoutExpired) as error:
        message = str(error) or "expected deployment/container details are missing"
        print(f"resource adjustment stopped: {message}", file=sys.stderr)
        sys.exit(1)
