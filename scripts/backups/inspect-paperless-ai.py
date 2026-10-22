#!/usr/bin/env python3
"""Preserve AI's container-local API schema and verify its pinned generator source."""

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile


NAMESPACE = "paperless-ngx"
POD = "paperless-ai-6d468d8cbf-lj75p"
POD_UID = "4950eace-1e75-4e58-a352-af55299bc84e"
IMAGE_DIGEST = "sha256:2b65888163fd59716f1c8285b31c5bd0b30c9c3c192c42b516688e3887d4ba60"
IMAGE_REVISION = "0eee0c69d5da3b2a4d2fb4e0fdcd71242567e3ba"
ROOT = Path("/root/k3s-backup-recovery/paperless-ngx")
MAX_FILE = 1024 * 1024
# Hash-verified amd64 image manifest 19baad5a... contains these generator inputs
# in COPY layer sha256:81e2dddace2d8792c5c2b048f8be7abcc795f393788f9d08e05ebea08357e396.
# Its only later nonempty layer changes the mode of start-services.sh.
SOURCE_HASHES = {
    "routes/auth.js": "2aede0fd25b69427774390a4b3b94043ddfa4b36f456487af580d95e863b12dc",
    "routes/rag.js": "e9f1d7d0d5f9bdf88531c7a2ffbfadafbdaa62c35bfe386804406c14e5ebd50a",
    "routes/setup.js": "bc2f427bd7e6603b6c58419278f1a3fca518a34216a476ea7783ee1eabdbcf2f",
    "schemas.js": "7f6b4ff9214f5d6dee66a30105785c03047ac99e82372cef7bafee9e6b4afa55",
    "server.js": "b1f23749fc2f5fff45b13ef615b4dd04a73a76221060dbae5a90610c399a0455",
    "swagger.js": "f66d24618ed274c6607494b4a6169c9ff6adad83a5d201bf39cdc21202b8e025",
}
BUNDLED_SCHEMA_SHA256 = "998f44aaf47643ac9ef72b86636c59c9fd3b43b2c813e8ffb148173cb979d6e6"
NODE_ENV_REMOVALS = ("NODE_OPTIONS", "NODE_PATH", "NODE_V8_COVERAGE", "NODE_COMPILE_CACHE", "NODE_REDIRECT_WARNINGS")


NODE_CODE = r'''
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = process.argv[1];
class InspectionFailure extends Error {}
function check(value, code) { if (!value) throw new InspectionFailure(code); }
function snapshot(filename) {
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const before = fs.fstatSync(fd, {bigint: true});
    check(before.isFile() && before.nlink === 1n, 'expected_regular_file');
    check(before.size > 0n && before.size <= 1048576n, 'file_size_outside_limit');
    const buffer = Buffer.alloc(Number(before.size) + 1);
    let size = 0;
    while (size < buffer.length) {
      const count = fs.readSync(fd, buffer, size, buffer.length - size, size);
      if (count === 0) break;
      size += count;
    }
    const after = fs.fstatSync(fd, {bigint: true});
    check(size === Number(before.size) && before.size === after.size && before.mtimeNs === after.mtimeNs &&
          before.ctimeNs === after.ctimeNs && before.ino === after.ino && before.dev === after.dev, 'file_changed');
    const data = buffer.subarray(0, size);
    return {data, bytes: size, sha256: crypto.createHash('sha256').update(data).digest('hex'),
            uid: Number(before.uid), gid: Number(before.gid), mode: Number(before.mode) & 0o7777};
  } finally { fs.closeSync(fd); }
}
try {
  check(fs.lstatSync(root).isDirectory(), 'expected_app_directory');
  const folder = path.join(root, 'OPENAPI');
  check(fs.lstatSync(folder).isDirectory(), 'expected_openapi_directory');
  const entries = fs.readdirSync(folder);
  check(entries.length === 1 && entries[0] === 'openapi.json', 'unexpected_openapi_entries');
  const openapi = snapshot(path.join(folder, 'openapi.json'));
  let routesDirectory = false;
  try { routesDirectory = fs.lstatSync(path.join(root, 'routes')).isDirectory(); } catch (_) {}
  const sources = {};
  for (const name of ['routes/auth.js', 'routes/rag.js', 'routes/setup.js', 'schemas.js', 'server.js', 'swagger.js']) {
    try { sources[name] = name.startsWith('routes/') && !routesDirectory ? null : snapshot(path.join(root, name)).sha256; }
    catch (_) { sources[name] = null; }
  }
  let routeSetMatches = false;
  try {
    const routes = path.join(root, 'routes');
    if (routesDirectory) {
      routeSetMatches = JSON.stringify(fs.readdirSync(routes).filter(name => name.endsWith('.js')).sort()) ===
                        JSON.stringify(['auth.js', 'rag.js', 'setup.js']);
    }
  } catch (_) {}
  console.log(JSON.stringify({openapi: {bytes: openapi.bytes, sha256: openapi.sha256, uid: openapi.uid,
    gid: openapi.gid, mode: openapi.mode, base64: openapi.data.toString('base64')},
    source_hashes: sources, route_set_matches: routeSetMatches}));
} catch (error) {
  console.log(JSON.stringify({error: error instanceof InspectionFailure ? error.message : 'file_read_failed'}));
}
'''


class InspectionError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise InspectionError(message)


def kube(*arguments):
    try:
        result = subprocess.run(["k3s", "kubectl", "--request-timeout=15s", *arguments],
                                text=True, capture_output=True, timeout=40)
    except subprocess.TimeoutExpired as error:
        raise InspectionError("read-only Kubernetes request timed out") from error
    require(result.returncode == 0, "read-only Kubernetes request failed; private output withheld")
    require(len(result.stdout) <= 2 * 1024**2, "inspection output exceeded its limit; output withheld")
    return result.stdout


def checked_pod():
    pod = json.loads(kube("get", "pod", POD, "-n", NAMESPACE, "-o", "json"))
    containers = [item for item in pod["spec"]["containers"] if item.get("name") == "paperless-ai"]
    states = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "paperless-ai"]
    checks = {
        "pod_uid_matches_inventory": pod["metadata"].get("uid") == POD_UID,
        "pod_not_terminating": not pod["metadata"].get("deletionTimestamp"),
        "node_is_omv": pod["spec"].get("nodeName") == "openmediavault",
        "pod_is_running": pod.get("status", {}).get("phase") == "Running",
        "one_ai_container": len(containers) == len(states) == 1,
        "image_reference_matches": len(containers) == 1 and containers[0].get("image", "").endswith(IMAGE_DIGEST),
        "image_id_matches": len(states) == 1 and states[0].get("imageID", "").endswith(IMAGE_DIGEST),
        "container_id_present": len(states) == 1 and bool(states[0].get("containerID")),
        "container_is_ready": len(states) == 1 and states[0].get("ready") is True,
        "container_is_running": len(states) == 1 and isinstance(states[0].get("state", {}).get("running"), dict),
        "restart_count_is_valid": len(states) == 1 and type(states[0].get("restartCount")) is int and
                                  states[0]["restartCount"] >= 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    require(not failed, "AI Pod checks failed: " + ", ".join(failed))
    return pod, (pod["metadata"]["uid"], states[0]["containerID"], states[0]["imageID"], states[0]["restartCount"])


def inspect_files():
    arguments = ["exec", "-n", NAMESPACE, POD, "-c", "paperless-ai", "--", "env"]
    for name in NODE_ENV_REMOVALS:
        arguments += ["-u", name]
    arguments += ["NODE_DISABLE_COMPILE_CACHE=1", "node", "--no-warnings", "-e", NODE_CODE, "/app"]
    value = json.loads(kube(*arguments))
    if "error" in value:
        reasons = {"expected_regular_file", "file_size_outside_limit", "file_changed", "expected_openapi_directory", "expected_app_directory",
                   "unexpected_openapi_entries", "file_read_failed"}
        reason = value["error"] if value["error"] in reasons else "file_read_failed"
        raise InspectionError("AI file inspection stopped: " + reason)
    require(set(value) == {"openapi", "source_hashes", "route_set_matches"} and
            type(value["route_set_matches"]) is bool and set(value["source_hashes"]) == set(SOURCE_HASHES),
            "unexpected AI inspection metadata; output withheld")
    require(all(digest is None or isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                for digest in value["source_hashes"].values()), "invalid AI source checksums")
    record = value["openapi"]
    require(set(record) == {"bytes", "sha256", "uid", "gid", "mode", "base64"} and
            all(type(record[key]) is int and record[key] >= 0 for key in ("bytes", "uid", "gid", "mode")) and
            0 < record["bytes"] <= MAX_FILE and record["mode"] <= 0o7777 and
            isinstance(record["base64"], str) and len(record["base64"]) <= (MAX_FILE + 2) // 3 * 4,
            "invalid captured AI file metadata")
    try:
        data = base64.b64decode(record["base64"], validate=True)
    except (binascii.Error, ValueError, TypeError) as error:
        raise InspectionError("captured AI file encoding is invalid") from error
    require(len(data) == record["bytes"] and hashlib.sha256(data).hexdigest() == record["sha256"],
            "captured AI file checksum does not match")
    return value, data


def save_file(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture_directory():
    attrs = ROOT.lstat()
    require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == os.geteuid() and stat.S_IMODE(attrs.st_mode) == 0o700,
            "expected private Paperless recovery directory")
    parent = ROOT / "ai-openapi"
    if not os.path.lexists(parent):
        parent.mkdir(mode=0o700)
        sync_directory(ROOT)
    attrs = parent.lstat()
    require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == os.geteuid() and stat.S_IMODE(attrs.st_mode) == 0o700,
            "AI schema recovery directory is not private")
    directory = Path(tempfile.mkdtemp(prefix="capture-", dir=parent))
    os.chmod(directory, 0o700)
    sync_directory(parent)
    return directory


def schema_summary(data):
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {"expected_openapi_document": False}
    expected = (isinstance(value, dict) and value.get("openapi") == "3.0.0" and
                isinstance(value.get("info"), dict) and value["info"].get("title") == "Paperless-AI API Documentation" and
                isinstance(value.get("paths"), dict))
    return {"expected_openapi_document": expected, "paths": len(value["paths"]) if expected else None}


def inspect(progress):
    progress["phase"] = "checking AI Pod identity"
    pod, before = checked_pod()
    terminated = next((item.get("lastState", {}).get("terminated", {})
                       for item in pod.get("status", {}).get("containerStatuses", [])
                       if item.get("name") == "paperless-ai"), {})
    reason = terminated.get("reason")
    previous_termination = None if not terminated else {
        "reason": reason if reason in {"OOMKilled", "Error", "Completed", "ContainerCannotRun", "StartError", "DeadlineExceeded"}
                  else "other; details withheld",
        "exit_code": terminated.get("exitCode") if type(terminated.get("exitCode")) is int else None,
    }
    progress.update(restart_count=before[3], previous_termination=previous_termination)
    progress["phase"] = "reading bounded AI schema and source files"
    metadata, data = inspect_files()
    progress["phase"] = "preserving private schema copy"
    directory = capture_directory()
    progress["recovery_directory"] = str(directory)
    save_file(directory / "openapi.json", data)
    require(hashlib.sha256((directory / "openapi.json").read_bytes()).hexdigest() == metadata["openapi"]["sha256"],
            "private schema copy failed verification")
    progress["phase"] = "rechecking source and Pod identity"
    repeated, repeated_data = inspect_files()
    require(repeated == metadata and repeated_data == data and checked_pod()[1] == before,
            "AI schema, generator, or container changed during inspection; private copy retained")
    generator_verified = metadata["source_hashes"] == SOURCE_HASHES and metadata["route_set_matches"]
    summary = schema_summary(data)
    classified = generator_verified and summary["expected_openapi_document"]
    record = dict(progress, result="passed" if classified else "action_needed", phase="complete", namespace=NAMESPACE,
                  component="paperless-ai", completed_at=datetime.now(timezone.utc).isoformat(),
                  pod_uid=before[0], image_id=before[2], image_version="3.0.9", image_revision=IMAGE_REVISION,
                  classification="generated_api_documentation" if classified else "unclassified; private copy retained",
                  pinned_generator_verified=generator_verified, generator_inputs_checked=len(SOURCE_HASHES),
                  schema=summary, file_sha256=metadata["openapi"]["sha256"], bytes=len(data),
                  matches_bundled_schema=metadata["openapi"]["sha256"] == BUNDLED_SCHEMA_SHA256,
                  original_ownership={"uid": metadata["openapi"]["uid"], "gid": metadata["openapi"]["gid"],
                                      "mode": oct(metadata["openapi"]["mode"])},
                  source_and_pod_unchanged=True, credentials_or_file_contents_printed=False,
                  kubernetes_resources_changed=False)
    save_file(directory / "receipt.json", (json.dumps(record, indent=2) + "\n").encode())
    sync_directory(directory)
    progress.update(record)
    return progress


def main():
    progress = {"result": "stopped", "phase": "starting"}
    try:
        require(os.geteuid() == 0 and platform.system() == "Linux" and platform.node().split(".")[0] == "openmediavault",
                "run with sudo python3 on OMV")
        inspect(progress)
    except InspectionError as error:
        progress.update(result="stopped", reason=str(error))
    except KeyboardInterrupt:
        progress.update(result="stopped", reason="interrupted; retain any private capture")
    except Exception:
        progress.update(result="stopped", reason="filesystem, API, runtime, or JSON check failed; private details withheld")
    print(json.dumps(progress, indent=2), flush=True)
    return 0 if progress["result"] in ("passed", "action_needed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
