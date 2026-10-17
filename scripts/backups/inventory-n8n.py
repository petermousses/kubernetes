#!/usr/bin/env python3
"""Read n8n bindings, storage metadata, backend/key-source flags, and capacity."""

import itertools
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

NAMESPACE = "n8n"
BASE = Path("/filesystem/k3s/data/n8n")
IMAGES = Path("/filesystem/k3s/data/n8n-images")
LOCAL_STORAGE = Path("/var/lib/rancher/k3s/storage")
KNOWN_ENTRIES = ("config", "database.sqlite", "database.sqlite-wal", "database.sqlite-shm",
                 "binaryData", "nodes", "git", "backups", "backup-staging", "backup-cache",
                 "backup-staging-history", "restore-validation", "live", "live-data")

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
                                          if entry["name"] == "DB_TYPE"), None)
            containers.append({
                "name": container["name"], "group": group, "image": container["image"],
                "image_id": state.get("imageID"), "ready": state.get("ready"),
                "restarts": state.get("restartCount"), "waiting_reason": state.get("state", {}).get("waiting", {}).get("reason"),
                "resources": container.get("resources", {}), "pvc_mounts": mounts,
                "custom_command_present": bool(container.get("command")),
                "custom_arguments_present": bool(container.get("args")),
                "environment_names": sorted(entry["name"] for entry in container.get("env", [])),
                "literal_database_type": literal_database_type if literal_database_type in ("sqlite", "postgresdb") else None,
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
    result = {"path": str(path), "resolved": str(resolved), "exists": path.exists(), "filesystem": mount,
              "filesystem_free_bytes": shutil.disk_usage(existing).free}
    approved = resolved in (BASE.resolve(), IMAGES.resolve()) or (
        resolved != LOCAL_STORAGE.resolve() and resolved.is_relative_to(LOCAL_STORAGE.resolve()))
    if not path.is_dir() or not approved:
        result["directory_inspection"] = "skipped: missing or outside approved source roots"
        return result
    attributes = resolved.stat()
    result["owner"] = {"uid": attributes.st_uid, "gid": attributes.st_gid,
                       "mode": oct(stat.S_IMODE(attributes.st_mode))}
    mounts = json.loads(read(["findmnt", "--json", "--list", "--output", "TARGET"]))["filesystems"]
    nested = [item["target"] for item in mounts if Path(item["target"]) != resolved and
              Path(item["target"]).is_relative_to(resolved)]
    result["nested_mounts"] = nested
    measured = None if nested else read(["du", "-sx", "--block-size=1", "--", str(resolved)],
                                        optional=True, timeout=30)
    result["allocated_bytes"] = int(measured.split()[0]) if measured else None
    entries = list(itertools.islice(resolved.iterdir(), 1001))
    result["top_level_entry_count"] = min(len(entries), 1000)
    result["entry_count_truncated"] = len(entries) > 1000
    result["known_entries"] = []
    # Report only fixed application/migration names, not workflow/image filenames.
    for name in KNOWN_ENTRIES:
        entry = resolved / name
        try:
            attributes = entry.lstat()
        except FileNotFoundError:
            continue
        result["known_entries"].append({"name": name, "bytes": attributes.st_size,
            "type": "file" if stat.S_ISREG(attributes.st_mode) else
                    "directory" if stat.S_ISDIR(attributes.st_mode) else "other",
            "uid": attributes.st_uid, "gid": attributes.st_gid, "mode": oct(stat.S_IMODE(attributes.st_mode))})
    return result


RUNTIME_CODE = r'''
const fs = require('fs');
const path = require('path');
const os = require('os');
function choice(value, choices) {
  return value === undefined ? 'unset' : choices.includes(value) ? value : 'set; unrecognized value omitted';
}
function boundedJSON(filename, maximum) {
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const before = fs.fstatSync(fd);
    if (!before.isFile() || before.size > maximum) throw new Error('invalid file');
    const buffer = Buffer.alloc(maximum + 1);
    const length = fs.readSync(fd, buffer, 0, buffer.length, null);
    const after = fs.fstatSync(fd);
    if (length !== before.size || before.size !== after.size || before.mtimeMs !== after.mtimeMs)
      throw new Error('changed file');
    return JSON.parse(buffer.subarray(0, length).toString('utf8'));
  } finally { fs.closeSync(fd); }
}
const result = {
  node_version: process.version,
  database_type_environment: choice(process.env.DB_TYPE, ['sqlite', 'postgresdb']),
  execution_mode_environment: choice(process.env.EXECUTIONS_MODE, ['regular', 'queue']),
  binary_mode_environment: choice(process.env.N8N_DEFAULT_BINARY_DATA_MODE, ['default', 'filesystem', 's3', 'database']),
  overrides_present: Object.fromEntries(['DB_TYPE_FILE', 'DB_SQLITE_DATABASE', 'DB_SQLITE_DATABASE_FILE',
    'N8N_USER_FOLDER', 'N8N_USER_FOLDER_FILE', 'N8N_CONFIG_FILES', 'N8N_BINARY_DATA_STORAGE_PATH',
    'N8N_BINARY_DATA_STORAGE_PATH_FILE'].map(key => [key, Object.hasOwn(process.env, key)])),
  encryption_key_environment_present: Boolean(process.env.N8N_ENCRYPTION_KEY),
  encryption_key_file_environment_present: Boolean(process.env.N8N_ENCRYPTION_KEY_FILE),
};
const folder = path.join(process.env.N8N_USER_FOLDER || os.homedir(), '.n8n');
result.data_directory_matches_expected_mount = folder === '/home/node/.n8n';
try {
  const config = boundedJSON(path.join(folder, 'config'), 65536);
  result.config_read_status = 'read';
  result.encryption_key_in_config = typeof config.encryptionKey === 'string' && config.encryptionKey.length > 0;
  result.database_type_config = choice(config.database && config.database.type, ['sqlite', 'postgresdb']);
} catch (_) { result.config_read_status = 'missing, changed, symlinked, oversized, or unreadable'; }
try {
  const pkg = boundedJSON('/usr/local/lib/node_modules/n8n/package.json', 262144);
  result.n8n_version = /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$/.test(pkg.version) ? pkg.version : 'unavailable';
} catch (_) { result.n8n_version = 'unavailable'; }
console.log(JSON.stringify(result));
'''


def runtime_summary(pods):
    candidates = [pod for pod in pods if pod.get("status", {}).get("phase") == "Running" and
                  not pod["metadata"].get("deletionTimestamp") and
                  any(item.get("type") == "Ready" and item.get("status") == "True"
                      for item in pod.get("status", {}).get("conditions", [])) and
                  any(container["name"] == "n8n" for container in pod["spec"].get("containers", [])) and
                  any(volume.get("persistentVolumeClaim", {}).get("claimName") == "n8n-data"
                      for volume in pod["spec"].get("volumes", []))]
    if len(candidates) != 1:
        return {"available": False, "reason": "expected one ready n8n-data writer Pod"}
    output = kube("exec", "-n", NAMESPACE, candidates[0]["metadata"]["name"], "-c", "n8n", "--",
                  "env", "-u", "NODE_OPTIONS", "-u", "NODE_PATH", "-u", "NODE_V8_COVERAGE",
                  "-u", "NODE_COMPILE_CACHE", "-u", "NODE_REDIRECT_WARNINGS", "NODE_DISABLE_COMPILE_CACHE=1",
                  "node", "-e", RUNTIME_CODE, optional=True)
    if output is None:
        return {"available": False, "reason": "Node read-only inspection unavailable"}
    result = json.loads(output)
    # Never forward opaque container stdout or environment/configuration values.
    expected = {"node_version", "database_type_environment", "execution_mode_environment", "binary_mode_environment",
                "overrides_present", "encryption_key_environment_present", "encryption_key_file_environment_present",
                "data_directory_matches_expected_mount", "config_read_status", "encryption_key_in_config",
                "database_type_config", "n8n_version"}
    if not isinstance(result, dict) or set(result) - expected:
        raise InventoryError("unexpected runtime inventory fields; output omitted")
    choices = {
        "database_type_environment": {"sqlite", "postgresdb"},
        "execution_mode_environment": {"regular", "queue"},
        "binary_mode_environment": {"default", "filesystem", "s3", "database"},
    }
    for field, allowed in choices.items():
        if result.get(field) not in allowed | {"unset", "set; unrecognized value omitted"}:
            raise InventoryError("unexpected runtime configuration value; output omitted")
    boolean_fields = ("encryption_key_environment_present", "encryption_key_file_environment_present",
                      "data_directory_matches_expected_mount")
    if (any(type(result.get(field)) is not bool for field in boolean_fields) or
            not isinstance(result.get("node_version"), str) or
            not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", result["node_version"]) or
            not isinstance(result.get("n8n_version"), str) or
            not re.fullmatch(r"(?:unavailable|[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)", result["n8n_version"])):
        raise InventoryError("unexpected runtime version or key-source flags; output omitted")
    override_names = {"DB_TYPE_FILE", "DB_SQLITE_DATABASE", "DB_SQLITE_DATABASE_FILE", "N8N_USER_FOLDER",
                      "N8N_USER_FOLDER_FILE", "N8N_CONFIG_FILES", "N8N_BINARY_DATA_STORAGE_PATH",
                      "N8N_BINARY_DATA_STORAGE_PATH_FILE"}
    overrides = result.get("overrides_present")
    if (not isinstance(overrides, dict) or set(overrides) != override_names or
            any(type(value) is not bool for value in overrides.values())):
        raise InventoryError("unexpected runtime override flags; output omitted")
    if result.get("config_read_status") == "read":
        if (type(result.get("encryption_key_in_config")) is not bool or
                result.get("database_type_config") not in {"sqlite", "postgresdb", "unset", "set; unrecognized value omitted"}):
            raise InventoryError("unexpected runtime config flags; output omitted")
    elif (result.get("config_read_status") != "missing, changed, symlinked, oversized, or unreadable" or
          "encryption_key_in_config" in result or "database_type_config" in result):
        raise InventoryError("unexpected runtime config status; output omitted")
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
        raise InventoryError("expected only the OMV node during migration")
    description = kube("describe", "node", "openmediavault")
    emit("allocated_resources", description.partition("Allocated resources:")[2].split("Events:", 1)[0].strip() or "unavailable")
    emit("measured_node_usage", kube("top", "node", "openmediavault", optional=True) or "unavailable")
    emit("measured_n8n_usage", kube("top", "pods", "-n", NAMESPACE, "--containers", optional=True) or "unavailable")
    emit("workloads", [workload_summary(item) for item in objects("deployments,statefulsets,daemonsets,cronjobs,hpa", NAMESPACE)])
    pods = objects("pods", NAMESPACE)
    emit("pods", [pod_summary(pod) for pod in pods])
    pending = json.loads(kube("get", "pods", "-A", "--field-selector=status.phase=Pending", "-o", "json"))["items"]
    emit("pending_pods", [{"namespace": pod["metadata"]["namespace"], "name": pod["metadata"]["name"],
                           "node": pod["spec"].get("nodeName"),
                           "requests": [item.get("resources", {}).get("requests", {})
                                        for group in ("containers", "initContainers") for item in pod["spec"].get(group, [])],
                           "scheduling": [{key: item.get(key) for key in ("status", "reason", "message")}
                                          for item in pod.get("status", {}).get("conditions", []) if item.get("type") == "PodScheduled"]}
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
    emit("n8n_runtime", runtime_summary(pods))
    for resource in ("networkpolicies", "limitranges", "resourcequotas", "poddisruptionbudgets"):
        emit(resource, [{"name": item["metadata"]["name"], "spec": item.get("spec", {})} for item in objects(resource, NAMESPACE)])
    print("read-only n8n inventory complete", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InventoryError as error:
        print("inventory stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        print("inventory stopped: filesystem, API, or runtime metadata could not be read", file=sys.stderr)
        sys.exit(1)
