#!/usr/bin/env python3
"""Read searxng's live storage, resource budget, and Valkey persistence metadata."""

import itertools
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


NAMESPACE = "searxng"
BASE = Path("/filesystem/k3s/data/searxng")
LOCAL_STORAGE = Path("/var/lib/rancher/k3s/storage")
CONFIG_FIELDS = ("dir", "dbfilename", "save", "appendonly", "appendfilename", "appenddirname",
                 "maxmemory", "maxmemory-policy")


class InventoryError(Exception):
    pass


def read(arguments, *, optional=False, timeout=30):
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if optional:
            return None
        raise InventoryError("read-only command timed out: " + " ".join(arguments)) from error
    if result.returncode:
        if optional:
            return None
        # Never echo API object bodies or unexpected container diagnostics.
        raise InventoryError("read-only command failed: " + " ".join(arguments))
    return result.stdout


def kube(*arguments, **options):
    return read(["k3s", "kubectl", "--request-timeout=15s", *arguments], **options)


def objects(resource, namespace=None):
    arguments = ["get", resource, "-o", "json"]
    if namespace:
        arguments.extend(["-n", namespace])
    return json.loads(kube(*arguments))["items"]


def emit(section, value):
    print(json.dumps({"section": section, "value": value}, indent=2), flush=True)


def pod_summary(pod):
    spec = pod["spec"]
    volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
    statuses = {container["name"]: container for container in pod.get("status", {}).get("containerStatuses", [])}
    containers = []
    for container in spec["containers"]:
        mounts = []
        for mount in container.get("volumeMounts", []):
            volume = volumes[mount["name"]]
            if "persistentVolumeClaim" in volume:
                claim = volume["persistentVolumeClaim"]
                mounts.append({"claim": claim["claimName"], "path": mount["mountPath"],
                               "read_only": mount.get("readOnly", False) or claim.get("readOnly", False),
                               "sub_path": mount.get("subPath")})
        # Secret and ConfigMap names are useful recovery dependencies; values
        # and literal container arguments/environment are deliberately omitted.
        secret_refs = {entry["valueFrom"]["secretKeyRef"]["name"]
                       for entry in container.get("env", [])
                       if "secretKeyRef" in entry.get("valueFrom", {})}
        secret_refs.update(entry["secretRef"]["name"] for entry in container.get("envFrom", [])
                           if "secretRef" in entry)
        containers.append({"name": container["name"], "image": container["image"],
                           "image_id": statuses.get(container["name"], {}).get("imageID"),
                           "resources": container.get("resources", {}), "pvc_mounts": mounts,
                           "secret_refs": sorted(secret_refs)})
    security = spec.get("securityContext", {})
    return {"name": pod["metadata"]["name"], "node": spec.get("nodeName"),
            "phase": pod.get("status", {}).get("phase"),
            "ready": any(condition.get("type") == "Ready" and condition.get("status") == "True"
                         for condition in pod.get("status", {}).get("conditions", [])),
            "terminating": bool(pod["metadata"].get("deletionTimestamp")),
            "security": {key: security.get(key) for key in ("runAsUser", "runAsGroup", "fsGroup")},
            "config_maps": sorted(volume["configMap"]["name"] for volume in volumes.values() if "configMap" in volume),
            "containers": containers}


def path_summary(value):
    path = Path(value)
    if not path.is_absolute():
        raise InventoryError("volume path is not absolute")
    resolved = path.resolve()
    existing = resolved
    while not existing.exists():
        existing = existing.parent
    mount = json.loads(read(["findmnt", "--json", "--target", str(existing),
                            "--output", "TARGET,SOURCE,FSTYPE"]))["filesystems"]
    usage = shutil.disk_usage(existing)
    result = {"path": str(path), "resolved": str(resolved), "exists": path.exists(),
              "filesystem": mount, "filesystem_free_bytes": usage.free}
    if not path.is_dir():
        return result
    namespace_root = BASE.resolve()
    local_storage_root = LOCAL_STORAGE.resolve()
    approved = ((namespace_root.name == NAMESPACE and resolved.is_relative_to(namespace_root)) or
                (resolved != local_storage_root and resolved.is_relative_to(local_storage_root)))
    if not approved:
        result["directory_inspection"] = "skipped: outside the namespace or a specific k3s volume directory"
        return result
    attributes = resolved.stat()
    result["owner"] = {"uid": attributes.st_uid, "gid": attributes.st_gid,
                       "mode": oct(stat.S_IMODE(attributes.st_mode))}
    measured = read(["du", "-sx", "--block-size=1", "--", str(resolved)], optional=True, timeout=30)
    result["allocated_bytes"] = int(measured.split()[0]) if measured else None
    entries = list(itertools.islice(resolved.iterdir(), 31))
    result["entries_truncated"] = len(entries) > 30
    result["top_level_entries"] = []
    for entry in sorted(entries[:30]):
        try:
            attributes = entry.lstat()
        except FileNotFoundError:
            continue  # This is a live inventory, so cache files can disappear.
        result["top_level_entries"].append({
            "name": entry.name, "bytes": attributes.st_size,
            "type": "file" if stat.S_ISREG(attributes.st_mode) else
                    "directory" if stat.S_ISDIR(attributes.st_mode) else "other",
            "uid": attributes.st_uid, "gid": attributes.st_gid,
            "mode": oct(stat.S_IMODE(attributes.st_mode)),
        })
    return result


def valkey_summary(pods):
    candidates = [pod for pod in pods if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "valkey"
                  and pod_summary(pod)["ready"] and not pod["metadata"].get("deletionTimestamp")
                  and any(container["name"] == "valkey" for container in pod["spec"]["containers"])]
    if len(candidates) != 1:
        return {"available": False, "reason": "expected exactly one ready Valkey Pod"}
    pod = candidates[0]["metadata"]["name"]

    def cli(*arguments):
        return kube("exec", "-n", NAMESPACE, pod, "-c", "valkey", "--", "valkey-cli", "--raw",
                    *arguments, optional=True)

    result = {"pod": pod}
    for section, fields in (
            ("server", ("valkey_version", "redis_version", "arch_bits")),
            ("persistence", ("loading", "rdb_bgsave_in_progress", "rdb_last_bgsave_status", "rdb_last_save_time",
                             "rdb_changes_since_last_save", "aof_enabled", "aof_rewrite_in_progress")),
            ("memory", ("used_memory", "used_memory_rss", "maxmemory", "maxmemory_policy")),
            ("replication", ("role", "connected_slaves", "connected_replicas"))):
        output = cli("INFO", section)
        parsed = dict(line.split(":", 1) for line in (output or "").splitlines()
                      if not line.startswith("#") and ":" in line)
        result[section] = {key: parsed[key] for key in fields if key in parsed}
    output = cli("INFO", "keyspace")
    result["keyspace"] = [line for line in (output or "").splitlines()
                          if line.startswith("db") and ":keys=" in line]
    output = cli("CONFIG", "GET", *CONFIG_FIELDS)
    lines = (output or "").splitlines()
    if lines and len(lines) % 2 == 0 and all(key in CONFIG_FIELDS for key in lines[::2]):
        result["persistence_config"] = dict(zip(lines[::2], lines[1::2]))
    else:
        result["persistence_config"] = "unavailable; CONFIG GET may require authentication or permission"
    return result


def main():
    if os.geteuid() != 0:
        raise InventoryError("run with sudo python3 on OMV")
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for command in ("k3s", "findmnt", "du"):
        if not shutil.which(command):
            raise InventoryError("required read-only tool is missing: " + command)
    nodes = objects("nodes")
    emit("nodes", [{"name": node["metadata"]["name"], "allocatable": node["status"].get("allocatable"),
                    "version": node["status"].get("nodeInfo", {}).get("kubeletVersion")} for node in nodes])
    if [node["metadata"]["name"] for node in nodes] != ["openmediavault"]:
        raise InventoryError("expected only the OMV node during these migration stages")
    description = kube("describe", "node", "openmediavault")
    allocation = description.partition("Allocated resources:")[2].split("Events:", 1)[0].strip()
    emit("allocated_resources", allocation or "unavailable")
    emit("measured_node_usage", kube("top", "node", "openmediavault", optional=True) or "unavailable")
    emit("measured_searxng_usage", kube("top", "pods", "-n", NAMESPACE, optional=True) or "unavailable")
    workloads = objects("deployments,statefulsets,cronjobs,hpa", NAMESPACE)
    emit("workloads", [{"kind": item["kind"], "name": item["metadata"]["name"],
                        "replicas": item["spec"].get("replicas"),
                        "ready_replicas": item.get("status", {}).get("readyReplicas"),
                        "schedule": item["spec"].get("schedule"), "suspend": item["spec"].get("suspend")}
                       for item in workloads])
    pods = objects("pods", NAMESPACE)
    emit("pods", [pod_summary(pod) for pod in pods])
    claims = objects("pvc", NAMESPACE)
    emit("claims", [{"name": claim["metadata"]["name"], "phase": claim.get("status", {}).get("phase"),
                     "volume": claim["spec"].get("volumeName"), "access_modes": claim["spec"].get("accessModes"),
                     "storage_class": claim["spec"].get("storageClassName"),
                     "capacity": claim.get("status", {}).get("capacity")} for claim in claims])
    for claim in claims:
        name = claim["spec"].get("volumeName")
        if not name:
            continue
        volume = json.loads(kube("get", "pv", name, "-o", "json"))
        spec = volume["spec"]
        backing = spec.get("local") or spec.get("hostPath") or {}
        emit("bound_volume", {"name": name, "claim": claim["metadata"]["name"],
                              "matches_claim_uid": spec.get("claimRef", {}).get("uid") == claim["metadata"]["uid"],
                              "access_modes": spec.get("accessModes"), "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
                              "node_affinity": spec.get("nodeAffinity"), "local_path": backing.get("path"),
                              "csi_driver": spec.get("csi", {}).get("driver")})
        if backing.get("path"):
            emit("volume_path", path_summary(backing["path"]))
    emit("backup_namespace_parent", path_summary(str(BASE)))
    emit("valkey", valkey_summary(pods))
    print("read-only inventory complete; no backup or workload changes have been applied", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (InventoryError, OSError, ValueError, KeyError, IndexError) as error:
        print("inventory stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
