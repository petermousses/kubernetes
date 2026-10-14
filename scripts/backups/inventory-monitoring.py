#!/usr/bin/env python3
"""Read monitoring's live bindings, writer metadata, and backup resource budget."""

import itertools
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


NAMESPACE = "monitoring"
BASE = Path("/filesystem/k3s/data") / NAMESPACE
LOCAL_STORAGE = Path("/var/lib/rancher/k3s/storage")
FLAGS = {
    "storage.tsdb.path", "storage.tsdb.retention.time", "storage.tsdb.retention.size",
    "storage.tsdb.wal-compression", "web.enable-admin-api", "web.enable-lifecycle", "enable-feature",
    "config.file", "storage.path", "data-path", "web.listen-address", "cluster.listen-address",
    "target", "config.expand-env", "common.path-prefix", "ingester.wal-enabled", "ingester.wal-dir",
}


class InventoryError(Exception):
    pass


def read(arguments, *, optional=False, timeout=30):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if optional:
            return None
        raise InventoryError("read-only command timed out: " + " ".join(arguments)) from error
    if result.returncode:
        if optional:
            return None
        raise InventoryError("read-only command failed: " + " ".join(arguments))
    return result.stdout


def kube(*arguments, **options):
    return read(["k3s", "kubectl", "--request-timeout=15s", *arguments], **options)


def objects(resource, namespace=None, *, optional=False):
    arguments = ["get", resource, "-o", "json"]
    if namespace:
        arguments.extend(["-n", namespace])
    output = kube(*arguments, optional=optional)
    return json.loads(output)["items"] if output is not None else None


def emit(section, value):
    print(json.dumps({"section": section, "value": value}, indent=2), flush=True)


def flags(arguments):
    """Keep only known storage/management flags; omit other literal arguments."""
    result = {}
    for index, argument in enumerate(arguments):
        if not argument.startswith("-"):
            continue
        name, separator, value = argument.lstrip("-").partition("=")
        if name not in FLAGS:
            continue
        if not separator:
            value = arguments[index + 1] if index + 1 < len(arguments) and not arguments[index + 1].startswith("-") else True
        result[name] = value
    return result


def volume_references(volumes):
    secrets, configs = set(), set()
    for volume in volumes:
        if "secret" in volume:
            secrets.add(volume["secret"]["secretName"])
        if "configMap" in volume:
            configs.add(volume["configMap"]["name"])
        for source in volume.get("projected", {}).get("sources", []):
            if "secret" in source:
                secrets.add(source["secret"]["name"])
            if "configMap" in source:
                configs.add(source["configMap"]["name"])
    return secrets, configs


def pod_summary(pod):
    spec, status = pod["spec"], pod.get("status", {})
    volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
    secrets, configs = volume_references(volumes.values())
    containers = []
    for group, status_group in (("containers", "containerStatuses"), ("initContainers", "initContainerStatuses")):
        statuses = {item["name"]: item for item in status.get(status_group, [])}
        for container in spec.get(group, []):
            mounts = []
            for mount in container.get("volumeMounts", []):
                volume = volumes.get(mount["name"], {})
                if "persistentVolumeClaim" in volume:
                    claim = volume["persistentVolumeClaim"]
                    mounts.append({"claim": claim["claimName"], "path": mount["mountPath"],
                                   "read_only": mount.get("readOnly", False) or claim.get("readOnly", False),
                                   "sub_path": mount.get("subPath"), "sub_path_expr": mount.get("subPathExpr")})
            for entry in container.get("env", []):
                source = entry.get("valueFrom", {})
                if "secretKeyRef" in source:
                    secrets.add(source["secretKeyRef"]["name"])
                if "configMapKeyRef" in source:
                    configs.add(source["configMapKeyRef"]["name"])
            for entry in container.get("envFrom", []):
                if "secretRef" in entry:
                    secrets.add(entry["secretRef"]["name"])
                if "configMapRef" in entry:
                    configs.add(entry["configMapRef"]["name"])
            state = statuses.get(container["name"], {})
            security = container.get("securityContext", {})
            literal_database_type = next((entry.get("value") for entry in container.get("env", [])
                                          if entry["name"] == "GF_DATABASE_TYPE"), None)
            containers.append({
                "name": container["name"], "group": group, "image": container["image"],
                "image_id": state.get("imageID"), "ready": state.get("ready"),
                "restarts": state.get("restartCount"), "waiting_reason": state.get("state", {}).get("waiting", {}).get("reason"),
                "resources": container.get("resources", {}), "pvc_mounts": mounts,
                "storage_flags": flags(container.get("args", [])),
                "environment_names": sorted(entry["name"] for entry in container.get("env", [])),
                "grafana_database_type": literal_database_type if literal_database_type in ("sqlite3", "mysql", "postgres") else None,
                "security": {key: security.get(key) for key in ("runAsUser", "runAsGroup", "readOnlyRootFilesystem")},
            })
    security = spec.get("securityContext", {})
    scheduled = next((item for item in status.get("conditions", []) if item.get("type") == "PodScheduled"), {})
    return {
        "name": pod["metadata"]["name"], "node": spec.get("nodeName"), "phase": status.get("phase"),
        "ready": any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions", [])),
        "terminating": bool(pod["metadata"].get("deletionTimestamp")),
        "owners": [{"kind": item["kind"], "name": item["name"]} for item in pod["metadata"].get("ownerReferences", [])],
        "scheduling": {key: scheduled.get(key) for key in ("status", "reason", "message")},
        "security": {key: security.get(key) for key in ("runAsUser", "runAsGroup", "fsGroup", "fsGroupChangePolicy")},
        "secret_refs": sorted(secrets), "config_map_refs": sorted(configs), "containers": containers,
    }


def workload_summary(item):
    spec, status = item.get("spec", {}), item.get("status", {})
    template = spec.get("template", {}).get("spec", {})
    return {
        "kind": item["kind"], "name": item["metadata"]["name"],
        "owners": [{"kind": owner["kind"], "name": owner["name"]} for owner in item["metadata"].get("ownerReferences", [])],
        "replicas": spec.get("replicas"), "ready_replicas": status.get("readyReplicas"),
        "updated_replicas": status.get("updatedReplicas"), "strategy": spec.get("strategy") or spec.get("updateStrategy"),
        "node_selector": template.get("nodeSelector"), "affinity": template.get("affinity"),
        "scale_target": spec.get("scaleTargetRef"), "min_replicas": spec.get("minReplicas"), "max_replicas": spec.get("maxReplicas"),
    }


def path_summary(value, *, inspect=True):
    path = Path(value)
    if not path.is_absolute():
        raise InventoryError("PV path is not absolute")
    resolved = path.resolve()
    existing = resolved
    while not existing.exists():
        existing = existing.parent
    mount = json.loads(read(["findmnt", "--json", "--target", str(existing),
                            "--output", "TARGET,SOURCE,FSTYPE"]))["filesystems"]
    result = {"path": str(path), "resolved": str(resolved), "exists": path.exists(), "filesystem": mount,
              "filesystem_free_bytes": shutil.disk_usage(existing).free}
    if not path.is_dir():
        return result
    namespace_root, local_root = BASE.resolve(), LOCAL_STORAGE.resolve()
    approved = ((namespace_root.name == NAMESPACE and resolved.is_relative_to(namespace_root)) or
                (resolved != local_root and resolved.is_relative_to(local_root)))
    if not approved:
        result["directory_inspection"] = "skipped: outside the namespace or a specific k3s volume directory"
        return result
    attributes = resolved.stat()
    result["owner"] = {"uid": attributes.st_uid, "gid": attributes.st_gid, "mode": oct(stat.S_IMODE(attributes.st_mode))}
    if not inspect:
        return result
    arguments = ["du", "-sx", "--block-size=1", "--", str(resolved)]
    if shutil.which("ionice"):
        arguments = ["ionice", "-c", "3", *arguments]
    if shutil.which("nice"):
        arguments = ["nice", "-n", "19", *arguments]
    measured = read(arguments, optional=True, timeout=30)
    result["allocated_bytes"] = int(measured.split()[0]) if measured else None
    entries = list(itertools.islice(resolved.iterdir(), 31))
    result["entries_truncated"] = len(entries) > 30
    result["top_level_entries"] = []
    for entry in sorted(entries[:30]):
        try:
            attributes = entry.lstat()
        except FileNotFoundError:
            continue
        result["top_level_entries"].append({
            "name": entry.name, "bytes": attributes.st_size,
            "type": "file" if stat.S_ISREG(attributes.st_mode) else "directory" if stat.S_ISDIR(attributes.st_mode) else "other",
            "uid": attributes.st_uid, "gid": attributes.st_gid, "mode": oct(stat.S_IMODE(attributes.st_mode)),
        })
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
    emit("allocated_resources", description.partition("Allocated resources:")[2].split("Events:", 1)[0].strip() or "unavailable")
    emit("measured_node_usage", kube("top", "node", "openmediavault", optional=True) or "unavailable")
    emit("measured_monitoring_usage", kube("top", "pods", "-n", NAMESPACE, "--containers", optional=True) or "unavailable")
    emit("workloads", [workload_summary(item) for item in objects("deployments,statefulsets,daemonsets,hpa", NAMESPACE)])
    for resource in ("prometheuses.monitoring.coreos.com", "alertmanagers.monitoring.coreos.com"):
        resources = objects(resource, NAMESPACE, optional=True)
        emit(resource, [{"kind": item["kind"], "name": item["metadata"]["name"],
                         "settings": {key: item.get("spec", {}).get(key) for key in
                                      ("replicas", "paused", "version", "image", "retention", "retentionSize", "enableAdminAPI", "storage", "nodeSelector")}}
                        for item in resources] if resources is not None else "unavailable")
    pods = objects("pods", NAMESPACE)
    emit("pods", [pod_summary(pod) for pod in pods])
    pending = json.loads(kube("get", "pods", "-A", "--field-selector=status.phase=Pending", "-o", "json"))["items"]
    emit("pending_pods", [{"namespace": pod["metadata"]["namespace"], "name": pod["metadata"]["name"],
                           "node": pod["spec"].get("nodeName"),
                           "resources": [{"name": item["name"], "resources": item.get("resources", {})}
                                         for group in ("containers", "initContainers") for item in pod["spec"].get(group, [])],
                           "scheduling": [{key: item.get(key) for key in ("status", "reason", "message")}
                                          for item in pod.get("status", {}).get("conditions", []) if item.get("type") == "PodScheduled"]}
                          for pod in pending])
    claims = objects("pvc", NAMESPACE)
    emit("claims", [{"name": claim["metadata"]["name"], "phase": claim.get("status", {}).get("phase"),
                     "volume": claim["spec"].get("volumeName"), "access_modes": claim["spec"].get("accessModes"),
                     "storage_class": claim["spec"].get("storageClassName"), "capacity": claim.get("status", {}).get("capacity")}
                    for claim in claims])
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
    emit("backup_namespace_parent", path_summary(str(BASE), inspect=False))
    for resource in ("networkpolicies", "limitranges", "resourcequotas", "poddisruptionbudgets"):
        resources = objects(resource, NAMESPACE)
        emit(resource, [{"name": item["metadata"]["name"], "spec": item.get("spec", {})} for item in resources])
    services = objects("services", NAMESPACE)
    emit("services", [{"name": item["metadata"]["name"], "selector": item["spec"].get("selector"),
                       "cluster_ip": item["spec"].get("clusterIP"), "ports": item["spec"].get("ports", [])} for item in services])
    print("read-only monitoring inventory complete", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InventoryError as error:
        print("inventory stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        print("inventory stopped: filesystem or API metadata could not be read", file=sys.stderr)
        sys.exit(1)
