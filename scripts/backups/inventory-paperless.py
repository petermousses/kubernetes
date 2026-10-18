#!/usr/bin/env python3
"""Read paperless-ngx bindings, writer/dependency metadata, and backup capacity."""

import itertools
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys


NAMESPACE = "paperless-ngx"
BASE = Path("/filesystem/k3s/data") / NAMESPACE
MEDIA = Path("/filesystem/Media") / NAMESPACE
LOCAL_STORAGE = Path("/var/lib/rancher/k3s/storage")
KNOWN_ENTRIES = ("data", "media", "export", "consume", "prompts", "PG_VERSION", "pg_wal",
                 "dump.rdb", "appendonly.aof", "appendonlydir", "database.sqlite", "database.sqlite3",
                 "backups", "backup-staging", "backup-cache", "restore-validation")


class InventoryError(Exception):
    pass


def read(arguments, *, optional=False, timeout=30):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if optional:
            return None
        raise InventoryError("read-only command timed out: " + arguments[0]) from error
    if result.returncode:
        if optional:
            return None
        # Container diagnostics and resource bodies can contain credentials.
        raise InventoryError("read-only command failed: " + arguments[0])
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
            mounts, temporary, host_paths = [], [], []
            for mount in container.get("volumeMounts", []):
                volume = volumes.get(mount["name"], {})
                common = {"path": mount["mountPath"], "read_only": mount.get("readOnly", False),
                          "sub_path": mount.get("subPath"), "sub_path_expr": mount.get("subPathExpr")}
                if "persistentVolumeClaim" in volume:
                    claim = volume["persistentVolumeClaim"]
                    mounts.append({**common, "claim": claim["claimName"],
                                   "read_only": common["read_only"] or claim.get("readOnly", False)})
                elif "emptyDir" in volume:
                    temporary.append({**common, "medium": volume["emptyDir"].get("medium", ""),
                                      "size_limit": volume["emptyDir"].get("sizeLimit")})
                elif "hostPath" in volume:
                    host_paths.append({**common, "host_path": volume["hostPath"].get("path")})
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
            containers.append({"name": container["name"], "group": group, "image": container["image"],
                "image_id": state.get("imageID"), "ready": state.get("ready"),
                "restarts": state.get("restartCount"), "waiting_reason": state.get("state", {}).get("waiting", {}).get("reason"),
                "resources": container.get("resources", {}), "pvc_mounts": mounts,
                "empty_dir_mounts": temporary, "host_path_mounts": host_paths,
                "custom_command_present": bool(container.get("command")),
                "custom_arguments_present": bool(container.get("args")),
                "environment_names": sorted(entry["name"] for entry in container.get("env", [])),
                "security": {key: security.get(key) for key in ("runAsUser", "runAsGroup", "readOnlyRootFilesystem")}})
    security = spec.get("securityContext", {})
    scheduled = next((item for item in status.get("conditions", []) if item.get("type") == "PodScheduled"), {})
    return {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
        "node": spec.get("nodeName"), "phase": status.get("phase"),
        "ready": any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions", [])),
        "terminating": bool(pod["metadata"].get("deletionTimestamp")),
        "owners": [{"kind": item["kind"], "name": item["name"]} for item in pod["metadata"].get("ownerReferences", [])],
        "scheduling": {key: scheduled.get(key) for key in ("status", "reason", "message")},
        "security": {key: security.get(key) for key in ("runAsUser", "runAsGroup", "fsGroup", "fsGroupChangePolicy")},
        "secret_refs": sorted(secrets), "config_map_refs": sorted(configs), "containers": containers}


def workload_summary(item):
    spec, status = item.get("spec", {}), item.get("status", {})
    template = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}) if item["kind"] == "CronJob" else spec.get("template", {})
    pod = template.get("spec", {})
    return {"kind": item["kind"], "name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
        "owners": [{"kind": owner["kind"], "name": owner["name"]} for owner in item["metadata"].get("ownerReferences", [])],
        "replicas": spec.get("replicas"), "ready_replicas": status.get("readyReplicas"),
        "updated_replicas": status.get("updatedReplicas"), "strategy": spec.get("strategy") or spec.get("updateStrategy"),
        "pvc_retention": spec.get("persistentVolumeClaimRetentionPolicy"),
        "node_selector": pod.get("nodeSelector"), "affinity": pod.get("affinity"),
        "scale_target": spec.get("scaleTargetRef"), "min_replicas": spec.get("minReplicas"), "max_replicas": spec.get("maxReplicas"),
        "schedule": spec.get("schedule"), "suspend": spec.get("suspend")}


def path_summary(value):
    path = Path(value)
    if not path.is_absolute():
        raise InventoryError("PV path is not absolute")
    resolved = path.resolve()
    existing = resolved
    while not existing.exists():
        existing = existing.parent
    mount = json.loads(read(["findmnt", "--json", "--target", str(existing),
                            "--output", "TARGET,SOURCE,FSTYPE"]))["filesystems"]
    result = {"path": str(path), "resolved": str(resolved), "exists": path.exists(),
              "filesystem": mount, "filesystem_free_bytes": shutil.disk_usage(existing).free}
    roots = [root.resolve() for root in (BASE, MEDIA) if root.resolve().name == NAMESPACE]
    local = LOCAL_STORAGE.resolve()
    approved = any(resolved.is_relative_to(root) for root in roots) or (resolved != local and resolved.is_relative_to(local))
    if not path.is_dir() or not approved:
        result["directory_inspection"] = "skipped: missing or outside approved source roots"
        return result
    attributes = resolved.stat()
    result["owner"] = {"uid": attributes.st_uid, "gid": attributes.st_gid, "mode": oct(stat.S_IMODE(attributes.st_mode))}
    mounts = json.loads(read(["findmnt", "--json", "--list", "--output", "TARGET"]))["filesystems"]
    nested = [item["target"] for item in mounts if Path(item["target"]) != resolved and Path(item["target"]).is_relative_to(resolved)]
    result["nested_mounts"] = nested
    command = ["du", "-sx", "--block-size=1", "--", str(resolved)]
    if shutil.which("ionice"):
        command = ["ionice", "-c", "3", *command]
    if shutil.which("nice"):
        command = ["nice", "-n", "19", *command]
    measured = None if nested else read(command, optional=True, timeout=30)
    result["allocated_bytes"] = int(measured.split()[0]) if measured else None
    entries = list(itertools.islice(resolved.iterdir(), 1001))
    result["top_level_entry_count"] = min(len(entries), 1000)
    result["entry_count_truncated"] = len(entries) > 1000
    result["known_entries"] = []
    # Document, prompt, and integration filenames are not safe inventory output.
    for name in KNOWN_ENTRIES:
        try:
            attributes = (resolved / name).lstat()
        except FileNotFoundError:
            continue
        result["known_entries"].append({"name": name, "bytes": attributes.st_size,
            "type": "file" if stat.S_ISREG(attributes.st_mode) else "directory" if stat.S_ISDIR(attributes.st_mode) else "other",
            "uid": attributes.st_uid, "gid": attributes.st_gid, "mode": oct(stat.S_IMODE(attributes.st_mode))})
    return result


def ready_container(pods, name):
    candidates = [pod for pod in pods if pod.get("status", {}).get("phase") == "Running" and
        not pod["metadata"].get("deletionTimestamp") and
        any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", [])) and
        any(container["name"] == name for container in pod["spec"].get("containers", []))]
    return candidates[0] if len(candidates) == 1 else None


POSTGRES_CODE = r'''
export PGPASSWORD="${POSTGRES_PASSWORD:-}"
export PGCONNECT_TIMEOUT=5
export PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000'
exec psql --no-psqlrc --no-password -qAt -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c '
SELECT json_build_object(
  '\''server_version_num'\'', current_setting('\''server_version_num'\'')::integer,
  '\''in_recovery'\'', pg_is_in_recovery(),
  '\''database_bytes'\'', pg_database_size(current_database()),
  '\''public_tables'\'', (SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = '\''public'\'')
);'
'''


def postgres_summary(pods):
    pod = ready_container(pods, "postgres")
    if not pod:
        return {"available": False, "reason": "expected one ready PostgreSQL Pod"}
    output = kube("exec", "-n", NAMESPACE, pod["metadata"]["name"], "-c", "postgres", "--",
                  "sh", "-ec", POSTGRES_CODE, optional=True)
    if output is None:
        return {"available": False, "reason": "read-only authenticated PostgreSQL inspection unavailable"}
    result = json.loads(output)
    integer_fields = {"server_version_num", "database_bytes", "public_tables"}
    if (not isinstance(result, dict) or set(result) != integer_fields | {"in_recovery"} or
            any(type(result.get(key)) is not int or result[key] < 0 for key in integer_fields) or
            type(result.get("in_recovery")) is not bool):
        raise InventoryError("unexpected PostgreSQL inventory fields; output omitted")
    return {"available": True, "pod": pod["metadata"]["name"], **result}


def redis_summary(pods):
    pod = ready_container(pods, "redis")
    if not pod:
        return {"available": False, "reason": "expected one ready Redis Pod"}
    output = kube("exec", "-n", NAMESPACE, pod["metadata"]["name"], "-c", "redis", "--",
                  "redis-cli", "--raw", "INFO", optional=True)
    parsed = dict(line.split(":", 1) for line in (output or "").splitlines() if not line.startswith("#") and ":" in line)
    result = {"available": False, "pod": pod["metadata"]["name"]}
    version = parsed.get("redis_version", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        result["reason"] = "read-only Redis INFO unavailable; authentication may be required"
        return result
    result.update(available=True, redis_version=version)
    for key in ("loading", "rdb_bgsave_in_progress", "rdb_last_save_time", "rdb_changes_since_last_save",
                "aof_enabled", "aof_rewrite_in_progress", "used_memory", "used_memory_rss", "maxmemory"):
        if re.fullmatch(r"[0-9]+", parsed.get(key, "")):
            result[key] = int(parsed[key])
    for key, choices in (("rdb_last_bgsave_status", {"ok", "err"}), ("role", {"master", "slave", "replica"})):
        if parsed.get(key) in choices:
            result[key] = parsed[key]
    result["keyspace"] = []
    for line in (output or "").splitlines():
        match = re.fullmatch(r"db([0-9]+):keys=([0-9]+),expires=([0-9]+),avg_ttl=([0-9]+)(?:,subexpiry=[0-9]+)?", line)
        if match:
            result["keyspace"].append(dict(zip(("database", "keys", "expires", "average_ttl_ms"), map(int, match.groups()))))
    return result


def main():
    if platform.system() != "Linux" or platform.node().split(".")[0] != "openmediavault":
        raise InventoryError("run on the OMV host, not on your Mac or another node")
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
        raise InventoryError("expected only the OMV node during migration")
    description = kube("describe", "node", "openmediavault")
    emit("allocated_resources", description.partition("Allocated resources:")[2].split("Events:", 1)[0].strip() or "unavailable")
    emit("measured_node_usage", kube("top", "node", "openmediavault", optional=True) or "unavailable")
    emit("measured_paperless_usage", kube("top", "pods", "-n", NAMESPACE, "--containers", optional=True) or "unavailable")
    emit("workloads", [workload_summary(item) for item in objects("deployments,statefulsets,daemonsets,cronjobs,hpa", NAMESPACE)])
    pods = objects("pods", NAMESPACE)
    emit("pods", [pod_summary(pod) for pod in pods])
    pending = json.loads(kube("get", "pods", "-A", "--field-selector=status.phase=Pending", "-o", "json"))["items"]
    emit("pending_pods", [{"namespace": pod["metadata"]["namespace"], "name": pod["metadata"]["name"],
        "node": pod["spec"].get("nodeName"),
        "requests": [item.get("resources", {}).get("requests", {}) for group in ("containers", "initContainers") for item in pod["spec"].get(group, [])],
        "scheduling": [{key: item.get(key) for key in ("status", "reason", "message")} for item in pod.get("status", {}).get("conditions", []) if item.get("type") == "PodScheduled"]}
        for pod in pending])
    claims = objects("pvc", NAMESPACE)
    emit("claims", [{"name": claim["metadata"]["name"], "uid": claim["metadata"]["uid"],
        "phase": claim.get("status", {}).get("phase"), "volume": claim["spec"].get("volumeName"),
        "access_modes": claim["spec"].get("accessModes"), "storage_class": claim["spec"].get("storageClassName"),
        "capacity": claim.get("status", {}).get("capacity")} for claim in claims])
    for claim in claims:
        name = claim["spec"].get("volumeName")
        if not name:
            continue
        volume = json.loads(kube("get", "pv", name, "-o", "json"))
        spec = volume["spec"]
        backing = spec.get("local") or spec.get("hostPath") or {}
        emit("bound_volume", {"name": name, "uid": volume["metadata"]["uid"], "claim": claim["metadata"]["name"],
            "matches_claim_uid": spec.get("claimRef", {}).get("uid") == claim["metadata"]["uid"],
            "access_modes": spec.get("accessModes"), "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
            "node_affinity": spec.get("nodeAffinity"), "local_path": backing.get("path"),
            "csi_driver": spec.get("csi", {}).get("driver")})
        if backing.get("path"):
            emit("volume_path", path_summary(backing["path"]))
            emit("repository_overlap", {"claim": claim["metadata"]["name"],
                "proposed_backup_path_inside_live_volume": (BASE / "backups").resolve().is_relative_to(Path(backing["path"]).resolve())})
    emit("backup_namespace_parent", path_summary(str(BASE)))
    emit("postgres", postgres_summary(pods))
    emit("redis", redis_summary(pods))
    emit("config_maps", [{"name": item["metadata"]["name"], "keys": sorted(item.get("data", {})),
        "binary_keys": sorted(item.get("binaryData", {}))} for item in objects("configmaps", NAMESPACE)])
    for resource in ("networkpolicies", "limitranges", "resourcequotas", "poddisruptionbudgets"):
        emit(resource, [{"name": item["metadata"]["name"], "spec": item.get("spec", {})} for item in objects(resource, NAMESPACE)])
    emit("host_memory", [line for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemAvailable:", "SwapFree:"))])
    print("read-only paperless-ngx inventory complete; no writers stopped or Kubernetes resources changed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InventoryError as error:
        print("inventory stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError):
        print("inventory stopped: filesystem, API, or runtime metadata could not be read", file=sys.stderr)
        sys.exit(1)
