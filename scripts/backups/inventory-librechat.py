#!/usr/bin/env python3
"""Read LibreChat storage, runtime metadata, native backup tools, and capacity."""

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


NAMESPACE = "librechat"
BASE = Path("/filesystem/k3s/data") / NAMESPACE
MEDIA = Path("/filesystem/Media") / NAMESPACE
LOCAL_STORAGE = Path("/var/lib/rancher/k3s/storage")
KNOWN_ENTRIES = ("WiredTiger", "WiredTiger.wt", "journal", "storage.bson", "data.ms", "VERSION",
                 "dumps", "snapshots", ".env.temp", "backups", "backup-staging", "backup-cache",
                 "backup-staging-history", "restore-validation")


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
                **({"mongodb_cli_flags": {flag: any(value == flag or value.startswith(flag + "=")
                    for value in container.get("command", []) + container.get("args", []))
                    for flag in ("--auth", "--noauth", "--replSet", "--config")}}
                    if container["name"] == "mongodb" else {}),
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
    # Chat, upload, image, and database collection filenames are not safe inventory output.
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


# Read-only metadata commands: https://www.mongodb.com/docs/manual/reference/command/dbStats/
# and https://www.mongodb.com/docs/manual/reference/command/hello/ . No document query is run.
MONGO_CODE = r'''
const admin = db.getSiblingDB('admin');
const version = admin.runCommand({buildInfo: 1, maxTimeMS: 5000});
const hello = admin.runCommand({hello: 1, maxTimeMS: 5000});
const databases = admin.runCommand({listDatabases: 1, nameOnly: true, maxTimeMS: 5000});
const stats = db.runCommand({dbStats: 1, scale: 1, freeStorage: 0, maxTimeMS: 5000});
if (![version, hello, databases, stats].every(value => value.ok === 1)) quit(2);
const output = {
  version: version.version,
  replica_set: typeof hello.setName === 'string',
  sharded_router: hello.msg === 'isdbgrid',
  writable_primary: hello.isWritablePrimary === true,
  expected_database_exists: databases.databases.some(item => item.name === 'LibreChat'),
  other_application_databases: databases.databases.filter(item => !['admin','config','local','LibreChat'].includes(item.name)).length,
};
for (const field of ['collections','views','objects','dataSize','storageSize','indexes','indexSize']) {
  output[field] = Number(stats[field]);
}
print('MONGO_INVENTORY_JSON=' + JSON.stringify(output));
'''

RUNTIME_ENVIRONMENT = (
    "CREDS_KEY", "CREDS_IV", "JWT_SECRET", "JWT_REFRESH_SECRET", "MEILI_MASTER_KEY",
    "LITELLM_API_KEY", "OPENID_CLIENT_ID", "OPENID_CLIENT_SECRET", "MONGO_URI", "MEILI_HOST",
    "CONFIG_PATH", "LIBRECHAT_TEMP_CREDENTIALS_PATH", "FILE_STRATEGY", "S3_BUCKET", "AZURE_STORAGE_CONTAINER_NAME",
)
RUNTIME_PATHS = ("/app/data", "/app/uploads", "/app/client/public/images", "/app/data/.env.temp", "/app/librechat.yaml")
NODE_ENVIRONMENT = ["env", *(argument for name in (
    "NODE_OPTIONS", "NODE_PATH", "NODE_V8_COVERAGE", "NODE_COMPILE_CACHE", "NODE_REDIRECT_WARNINGS",
    "NODE_USE_ENV_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
) for argument in ("-u", name)), "NODE_DISABLE_COMPILE_CACHE=1"]

# Only GET /version, /stats, and filtered /tasks are called. Neither dumps nor snapshots are requested.
# https://www.meilisearch.com/docs/reference/api/stats
# https://www.meilisearch.com/docs/reference/api/tasks
APPLICATION_CODE = r'''
const fs = require('node:fs');
const environment = ['CREDS_KEY','CREDS_IV','JWT_SECRET','JWT_REFRESH_SECRET','MEILI_MASTER_KEY',
  'LITELLM_API_KEY','OPENID_CLIENT_ID','OPENID_CLIENT_SECRET','MONGO_URI','MEILI_HOST',
  'CONFIG_PATH','LIBRECHAT_TEMP_CREDENTIALS_PATH','FILE_STRATEGY','S3_BUCKET','AZURE_STORAGE_CONTAINER_NAME'];
const output = {
  node_version: process.versions.node,
  environment_present: Object.fromEntries(environment.map(name => [name, Boolean(process.env[name])])),
  creds_key_hex_32_bytes: /^[a-fA-F0-9]{64}$/.test(process.env.CREDS_KEY || ''),
  creds_iv_hex_16_bytes: /^[a-fA-F0-9]{32}$/.test(process.env.CREDS_IV || ''),
  configuration_path_matches_repository: process.env.CONFIG_PATH === '/app/librechat.yaml',
  temporary_credentials_path_matches_repository: process.env.LIBRECHAT_TEMP_CREDENTIALS_PATH === '/app/data/.env.temp',
  mongo_uri_matches_repository: process.env.MONGO_URI === 'mongodb://mongodb:27017/LibreChat',
  search_setting: ['true','false'].includes(process.env.SEARCH) ? process.env.SEARCH : process.env.SEARCH ? 'custom' : 'unset',
  paths: [],
  meilisearch: {available: false, phase: 'configuration'},
};
for (const path of ['/app/data','/app/uploads','/app/client/public/images','/app/data/.env.temp','/app/librechat.yaml']) {
  try {
    const attrs = fs.lstatSync(path);
    output.paths.push({path, kind: attrs.isSymbolicLink() ? 'symlink' : attrs.isDirectory() ? 'directory' : attrs.isFile() ? 'file' : 'other',
      uid: attrs.uid, gid: attrs.gid, mode: (attrs.mode & 0o7777).toString(8)});
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('runtime path metadata unavailable');
    output.paths.push({path, kind: 'missing'});
  }
}
async function searchMetadata() {
  const base = new URL(process.env.MEILI_HOST);
  const hosts = ['meilisearch','meilisearch.librechat','meilisearch.librechat.svc','meilisearch.librechat.svc.cluster.local'];
  const key = process.env.MEILI_MASTER_KEY || '';
  if (base.protocol !== 'http:' || base.port !== '7700' || !hosts.includes(base.hostname) ||
      base.username || base.password || base.pathname !== '/' || base.search || base.hash ||
      !/^[\x21-\x7e]{1,4096}$/.test(key)) return;
  async function get(path) {
    const response = await fetch(base.origin + path, {method: 'GET', redirect: 'error',
      headers: {Authorization: 'Bearer ' + key}, signal: AbortSignal.timeout(5000)});
    if (!response.ok) {
      output.meilisearch.http_status = response.status;
      throw new Error('metadata HTTP request failed');
    }
    const chunks = [];
    let size = 0;
    const reader = response.body.getReader();
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 1024 * 1024) throw new Error('metadata response too large');
        chunks.push(value);
      }
    } finally {
      await reader.cancel();
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }
  output.meilisearch.phase = 'version';
  const version = await get('/version');
  output.meilisearch.phase = 'stats';
  const stats = await get('/stats');
  output.meilisearch.phase = 'tasks';
  const tasks = await get('/tasks?statuses=enqueued,processing&limit=1');
  const indexes = Object.values(stats.indexes);
  if (!indexes.every(item => Number.isSafeInteger(item.numberOfDocuments) && item.numberOfDocuments >= 0 && typeof item.isIndexing === 'boolean')) {
    throw new Error('index statistics differ');
  }
  output.meilisearch = {available: true, version: version.pkgVersion, database_bytes: stats.databaseSize,
    indexes: indexes.length, documents: indexes.reduce((sum, item) => sum + item.numberOfDocuments, 0),
    indexing_indexes: indexes.filter(item => item.isIndexing).length, unfinished_tasks: tasks.total};
}
searchMetadata().catch(() => {}).then(() => console.log('LIBRECHAT_INVENTORY_JSON=' + JSON.stringify(output))).catch(() => process.exit(1));
'''


def probe_document(output, marker):
    lines = [line[len(marker):] for line in output.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise InventoryError("expected one runtime metadata result; output omitted")
    value = json.loads(lines[0])
    if not isinstance(value, dict):
        raise InventoryError("unexpected runtime metadata object; output omitted")
    return value


def integer_fields(value, fields):
    result = {}
    for field in fields:
        number = value.get(field)
        if type(number) is not int or not 0 <= number <= 2**53 - 1:
            raise InventoryError("unexpected numeric metadata; output omitted")
        result[field] = number
    return result


def boolean_fields(value, fields):
    result = {}
    for field in fields:
        if type(value.get(field)) is not bool:
            raise InventoryError("unexpected boolean metadata; output omitted")
        result[field] = value[field]
    return result


def version_field(value, field):
    version = value.get(field)
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise InventoryError("unexpected version metadata; output omitted")
    return version


def mongodb_summary(pods):
    pod = ready_container(pods, "mongodb")
    if not pod:
        return {"available": False, "reason": "expected one ready MongoDB Pod"}
    name = pod["metadata"]["name"]
    binaries = kube("exec", "-n", NAMESPACE, name, "-c", "mongodb", "--", "sh", "-ec",
        'for tool in mongosh mongodump mongorestore; do if command -v "$tool" >/dev/null 2>&1; '
        'then printf "%s=yes\\n" "$tool"; else printf "%s=no\\n" "$tool"; fi; done', optional=True)
    result = {"available": False, "pod": name, "tools": {}}
    if binaries is not None:
        lines = dict(line.split("=", 1) for line in binaries.splitlines() if re.fullmatch(r"(?:mongosh|mongodump|mongorestore)=(?:yes|no)", line))
        result["tools"] = {tool: lines[tool] == "yes" for tool in ("mongosh", "mongodump", "mongorestore") if tool in lines}
    # The repository uses a standalone, unauthenticated local MongoDB. Do not guess credentials if live settings differ.
    output = kube("exec", "-n", NAMESPACE, name, "-c", "mongodb", "--", *NODE_ENVIRONMENT,
        "mongosh", "--norc", "--quiet", "--eval", MONGO_CODE,
        "mongodb://127.0.0.1:27017/LibreChat?directConnection=true&serverSelectionTimeoutMS=5000", optional=True)
    if output is None:
        return {**result, "reason": "read-only local MongoDB metadata unavailable; authentication or tooling may differ"}
    value = probe_document(output, "MONGO_INVENTORY_JSON=")
    return {**result, "available": True, "version": version_field(value, "version"),
        **boolean_fields(value, ("replica_set", "sharded_router", "writable_primary", "expected_database_exists")),
        **integer_fields(value, ("other_application_databases", "collections", "views", "objects", "dataSize", "storageSize", "indexes", "indexSize"))}


def application_summary(pods):
    pod = ready_container(pods, "librechat")
    if not pod:
        return {"available": False, "reason": "expected one ready LibreChat Pod"}
    output = kube("exec", "-n", NAMESPACE, pod["metadata"]["name"], "-c", "librechat", "--", *NODE_ENVIRONMENT, "node", "--no-warnings",
        "--input-type=commonjs", "-e", APPLICATION_CODE, optional=True)
    if output is None:
        return {"available": False, "reason": "read-only LibreChat runtime inspection unavailable"}
    value = probe_document(output, "LIBRECHAT_INVENTORY_JSON=")
    result = {"available": True, "pod": pod["metadata"]["name"], "node_version": version_field(value, "node_version"),
        "environment_present": boolean_fields(value["environment_present"], RUNTIME_ENVIRONMENT),
        **boolean_fields(value, ("creds_key_hex_32_bytes", "creds_iv_hex_16_bytes", "configuration_path_matches_repository",
                                "temporary_credentials_path_matches_repository", "mongo_uri_matches_repository"))}
    if value.get("search_setting") not in ("true", "false", "unset", "custom"):
        raise InventoryError("unexpected search flag; value omitted")
    result["search_setting"] = value["search_setting"]
    paths = value.get("paths")
    if not isinstance(paths, list) or len(paths) != len(RUNTIME_PATHS) or {item.get("path") for item in paths} != set(RUNTIME_PATHS):
        raise InventoryError("unexpected runtime path metadata; output omitted")
    result["paths"] = []
    for entry in paths:
        if entry.get("kind") not in ("file", "directory", "symlink", "other", "missing"):
            raise InventoryError("unexpected runtime path kind; output omitted")
        clean = {"path": entry["path"], "kind": entry["kind"]}
        if entry["kind"] != "missing":
            clean.update(integer_fields(entry, ("uid", "gid")))
            mode = entry.get("mode")
            if not isinstance(mode, str) or not re.fullmatch(r"[0-7]{1,4}", mode):
                raise InventoryError("unexpected runtime path permissions; output omitted")
            clean["mode"] = "0o" + mode
        result["paths"].append(clean)
    meili = value.get("meilisearch", {})
    result["meilisearch"] = {"available": False, "reason": "read-only Meilisearch GET metadata unavailable"}
    if meili.get("available") is True:
        result["meilisearch"] = {"available": True, "version": version_field(meili, "version"),
            **integer_fields(meili, ("database_bytes", "indexes", "documents", "indexing_indexes", "unfinished_tasks"))}
    else:
        if meili.get("phase") in ("configuration", "version", "stats", "tasks"):
            result["meilisearch"]["phase"] = meili["phase"]
        if type(meili.get("http_status")) is int and 100 <= meili["http_status"] <= 599:
            result["meilisearch"]["http_status"] = meili["http_status"]
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
    emit("measured_librechat_usage", kube("top", "pods", "-n", NAMESPACE, "--containers", optional=True) or "unavailable")
    emit("workloads", [workload_summary(item) for item in objects("deployments,statefulsets,daemonsets,cronjobs,hpa", NAMESPACE)])
    emit("services", [{"name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
        "type": item["spec"].get("type"), "cluster_ips": item["spec"].get("clusterIPs"),
        "selector": item["spec"].get("selector"), "ports": item["spec"].get("ports", [])}
        for item in objects("services", NAMESPACE)])
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
    emit("mongodb", mongodb_summary(pods))
    emit("librechat_runtime", application_summary(pods))
    emit("config_maps", [{"name": item["metadata"]["name"], "keys": sorted(item.get("data", {})),
        "binary_keys": sorted(item.get("binaryData", {}))} for item in objects("configmaps", NAMESPACE)])
    for resource in ("networkpolicies", "limitranges", "resourcequotas", "poddisruptionbudgets"):
        emit(resource, [{"name": item["metadata"]["name"], "spec": item.get("spec", {})} for item in objects(resource, NAMESPACE)])
    emit("host_memory", [line for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemAvailable:", "SwapFree:"))])
    print("read-only librechat inventory complete; no writers stopped or Kubernetes resources changed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InventoryError as error:
        print("inventory stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, RuntimeError):
        print("inventory stopped: filesystem, API, or runtime metadata could not be read", file=sys.stderr)
        sys.exit(1)
