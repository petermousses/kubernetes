#!/usr/bin/env python3
"""Export Paperless recovery credentials on OMV; verify an encrypted copy off OMV."""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zlib

NAMESPACE = "paperless-ngx"
RESTORE_NAMESPACE = "paperless-restore-validation"
BASE = Path("/filesystem/k3s/data/paperless-ngx")
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
COMPONENT_CLAIMS = {component:entry["claim"] for component,entry in PROTECTED.items()}
COMPONENTS = tuple(COMPONENT_CLAIMS)
CLAIMS = tuple(COMPONENT_CLAIMS.values())
WRITERS = ("paperless-ai", "paperless-gpt", "paperless")
RECOVERY = Path("/root/k3s-backup-recovery/paperless-ngx")
AI_CAPTURE = "ai-openapi/capture-cnpoixp8/"
AI_SCHEMA_SHA256 = "998f44aaf47643ac9ef72b86636c59c9fd3b43b2c813e8ffb148173cb979d6e6"
VALIDATOR_CODE_SHA256 = "c9df3394f6327878d65f1c41efbefdd80c6044961911ba3cf5c0a788f1d72a17"
TOOLS_DIGEST = "0d03a6aad57569eba2c0eaa0848cf4a908d9744b372ff2224bb291a320f36d76"
JOBS = {"files":"paperless-files-restore-initial", "postgres":"paperless-postgres-restore-initial", "redis":"paperless-redis-restore-initial"}
FILES = (
    "credentials.json", "ca.crt", "endpoint-verification.json",
    *("restic-" + claim + ".secret.json" for claim in CLAIMS),
    "restic-server-auth.secret.json", "restic-server-tls.secret.json", "restic-root-ca.secret.json",
    "dependency-inventory.json", "source-bindings.json",
    *(name + "." + kind + ".json" for name,(kind,_,_) in WORKLOADS.items()),
    *(name for claim in CLAIMS for name in (claim + ".pvc.json", claim + ".pv.json")),
    "gpt-storage-migration/state.json", "gpt-storage-migration/copy-validation.json", "gpt-storage-migration/result.json",
    AI_CAPTURE + "receipt.json", AI_CAPTURE + "openapi.json",
    "initial-backup.json", "initial-restore.json", "initial-backup-verification.json", "initial-restore-verification.json",
    *("initial-" + component + "-" + suffix + ".json" for component in COMPONENTS
      for suffix in ("volsync-result", "backup-result", "volsync-restore-result", "restore-files")),
    *(name + "-job.json" for name in JOBS.values()),
    "initial-postgres-restore-native.json", "initial-redis-restore-native.json", "writer-quiescence.json",
    "latest-daily-backup.json", "daily-schedule.json", "retention-proof.json",
)
ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "validation.json", "metadata.json", "backup-id"}
MANIFEST = "recovery-manifest.json"
MAX_FILE = 8 * 1024 * 1024
MAX_CONTENT = 64 * 1024 * 1024
MAX_ARCHIVE = 80 * 1024 * 1024
MAX_TAR = 80 * 1024 * 1024
MAX_IDENTITY = 4096

class RecoveryError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryError(message)

def private_bytes(path, owner, *, maximum=MAX_FILE):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise RecoveryError("could not open a private recovery file: " + path.name) from error
    with os.fdopen(descriptor, "rb") as content:
        attributes = os.fstat(content.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
                attributes.st_uid == owner and stat.S_IMODE(attributes.st_mode) == 0o600,
                "expected a private regular file: " + path.name)
        require(0 < attributes.st_size <= maximum, "private file is empty or too large: " + path.name)
        value = content.read(maximum + 1)
        after = os.fstat(content.fileno())
        require((attributes.st_size, attributes.st_mtime_ns, attributes.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) and len(value) == attributes.st_size,
                "private file changed while reading: " + path.name)
        return value

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "recovery JSON contains duplicate keys")
        result[key] = value
    return result

def document(files, name):
    value = json.loads(files[name], object_pairs_hook=unique_object)
    require(isinstance(value, dict), "expected a recovery JSON object")
    return value

def identifier(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix + r"-[0-9a-f]{32}", value),
            "saved backup generation is invalid")
    return value

def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value),
        "saved evidence timestamp is invalid")
    # Go's RFC3339Nano timestamps can have nine fractional digits. Python 3.9
    # accepts microseconds; validate the timezone without changing saved evidence.
    parsed = re.sub(r"\.(\d+)", lambda match: "." + (match[1] + "000000")[:6], value)
    require(datetime.fromisoformat(parsed.replace("Z", "+00:00")).tzinfo is not None,
            "saved evidence timestamp is invalid")

def daily_files(generation):
    identifier(generation, "daily")
    return tuple(generation + "-" + component + "-" + suffix + ".json"
                 for component in COMPONENTS for suffix in ("volsync-result", "backup-result"))

def resource(files, filename, kind, name, api="v1", *, namespace=NAMESPACE):
    value = document(files, filename)
    require(value.get("apiVersion") == api and value.get("kind") == kind and
            value.get("metadata", {}).get("namespace") == namespace and
            value["metadata"].get("name") == name, "saved resource identity differs")
    return value

def secret(files, name, filename=None):
    data = resource(files, filename or name + ".secret.json", "Secret", name).get("data")
    require(isinstance(data, dict) and data and
            all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()),
            "saved Secret data is missing or invalid")
    return {key: base64.b64decode(value, validate=True) for key, value in data.items()}

def completed(files, filename, component, generation, *, restore=False):
    kind = "ReplicationDestination" if restore else "ReplicationSource"
    name = component + ("-restore-initial" if restore else "-backup")
    value = resource(files, filename, kind, name, "volsync.backube/v1alpha1",
                     namespace=RESTORE_NAMESPACE if restore else NAMESPACE)
    spec = value.get("spec", {})
    status = value.get("status", {})
    restic = spec.get("restic", {})
    require(spec.get("trigger") == {"manual": generation} and not spec.get("paused") and
            status.get("lastManualSync") == generation and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and
            status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(item.get("type") == "Synchronizing" and item.get("status") == "False" and
                item.get("reason") == "WaitingForManual" for item in status.get("conditions", [])),
            "saved VolSync completion is incomplete")
    timestamp(status["lastSyncTime"])
    claim = COMPONENT_CLAIMS[component]
    ca = restic.get("customCA", {})
    require(restic.get("copyMethod") == "Direct" and
            restic.get("repository") == ("restic-restore-" if restore else "restic-") + claim,
            "saved VolSync repository differs")
    if restore:
        require(restic.get("destinationPVC") == component + "-restore-artifacts" and
                ca.get("secretName") == "restic-restore-ca" and ca.get("key") == "ca.crt",
                "saved artifact restore target or CA differs")
    else:
        require(spec.get("sourcePVC") == component + "-backup-staging" and
                restic.get("retain", {}).get("within") == "7d" and
                ca.get("secretName") == "restic-root-ca" and ca.get("key") == "tls.crt",
                "saved upload source, retention, or CA differs")
    return value

def backup_result(files, filename, component, generation):
    value = document(files, filename)
    require(value.get("result") == "passed" and value.get("backup_id") == generation and
            value.get("claim") == COMPONENT_CLAIMS[component] and isinstance(value.get("snapshot"), str) and
            re.fullmatch(r"[0-9a-f]{64}", value["snapshot"]) and
            isinstance(value.get("verification_job"), str) and value["verification_job"],
            "saved backup verification is incomplete")
    timestamp(value.get("completed_at"))
    checksums = value.get("checksums")
    require(isinstance(checksums, str), "saved artifact checksums are missing")
    artifacts = {}
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        require(match and match[2] not in artifacts, "saved artifact checksums are invalid")
        artifacts[match[2]] = match[1]
    require(set(artifacts) == ARTIFACTS, "saved artifact checksum set differs")
    return value, artifacts

def initial_files(generation):
    identifier(generation, "initial")
    return (generation + "-writer-quiescence.json",)

def recovery_bytes(name):
    parts = Path(name).parts
    require(bool(parts) and not Path(name).is_absolute() and ".." not in parts,
            "invalid recovery file path")
    current = RECOVERY
    for part in parts[:-1]:
        current /= part
        attributes = current.lstat()
        require(stat.S_ISDIR(attributes.st_mode) and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o700,
                "nested recovery directory must be private and root-owned")
    return private_bytes(current / parts[-1], 0)

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def dependency_files(files):
    inventory = document(files, "dependency-inventory.json")
    require(inventory.get("version") == 1 and inventory.get("namespace") == NAMESPACE and
            set(inventory.get("resources", {})) == {"secret", "configmap"}, "saved dependency inventory differs")
    result = set()
    for kind, entries in inventory["resources"].items():
        require(isinstance(entries, dict), "invalid saved dependency list")
        for name, entry in entries.items():
            require(isinstance(name, str) and len(name) <= 253 and re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?",name) and
                    type(entry.get("present")) is bool, "invalid dependency resource name or presence")
            if entry["present"]:
                filename = name + "." + kind + ".json"
                require(entry.get("saved_file") == filename, "dependency recovery filename differs")
                result.add(filename)
            else:
                require(entry.get("optional") is True and entry.get("saved_file") is None, "required dependency is absent")
    return result


def expected_files(files):
    require(set(FILES) <= set(files), "the recovery file set is incomplete")
    initial = document(files, "initial-backup.json").get("backup_id")
    daily = document(files, "latest-daily-backup.json").get("backup_id")
    return set(FILES) | dependency_files(files) | set(daily_files(daily)) | set(initial_files(initial))


def writer_journal(files, filename, generation, identities, controllers):
    journal = document(files, filename)
    require(journal.get("version") == 1 and journal.get("backup_id") == generation and
            journal.get("phase") == "resumed" and journal.get("capture_complete") is True and
            set(journal.get("workloads", {})) == set(WRITERS) and set(journal.get("bindings", {})) == set(COMPONENTS),
            "saved writer recovery is incomplete")
    for name in WRITERS:
        value = journal["workloads"][name]
        controller = controllers[name]
        require(value.get("uid") == controller["metadata"]["uid"] and value.get("replicas") == 1 and
                value.get("hash") == fingerprint({key:value for key,value in controller["spec"].items() if key != "replicas"}) and
                isinstance(value.get("pod_uids"), list) and len(value["pod_uids"]) == 1,
                "saved writer controller identity differs")
    for component in COMPONENTS:
        binding = journal["bindings"][component]
        require(all(binding.get(key) == value for key,value in identities[component].items()) and
                all(type(binding.get(key)) is int and binding[key] >= 0 for key in ("directory_device", "directory_inode")),
                "writer recovery storage identity differs")
    return journal


def production_anchor(value, identities, controllers):
    require(set(value.get("bindings", {})) == set(COMPONENTS) and set(value.get("workloads", {})) == set(WORKLOADS),
            "saved production identity set differs")
    for component, binding in identities.items():
        require(all(value["bindings"][component].get(key) == data for key,data in binding.items()), "saved production storage identity differs")
    for name,(_,uid,digest) in WORKLOADS.items():
        anchor=value["workloads"][name]
        require(anchor.get("controller_uid") == uid and anchor.get("spec_sha256") == fingerprint(controllers[name]["spec"]) and
                isinstance(anchor.get("image_id"),str) and anchor["image_id"].endswith("@sha256:"+digest),
                "saved production controller or image differs")


def validate_files(files):
    require(set(files) == expected_files(files), "the recovery file set is incomplete or unexpected")
    require(all(isinstance(value,bytes) and 0 < len(value) <= MAX_FILE for value in files.values()) and
            sum(map(len,files.values())) <= MAX_CONTENT, "recovery contents exceed size limits")
    credentials = document(files, "credentials.json")
    entries = credentials.get("repositories", {})
    require(set(credentials) == {"namespace","repositories"} and credentials["namespace"] == NAMESPACE and
            isinstance(entries,dict) and set(entries) == set(CLAIMS), "saved repository identities differ")
    authentication = b""
    for claim in CLAIMS:
        entry=entries[claim]
        require(isinstance(entry,dict) and set(entry) == {"RESTIC_REPOSITORY","RESTIC_PASSWORD","RESTIC_REST_USERNAME","RESTIC_REST_PASSWORD","htpasswd"} and
                all(isinstance(value,str) and value for value in entry.values()) and
                entry["RESTIC_REPOSITORY"] == "rest:https://restic.paperless-ngx.svc:8000/"+claim+"/" and
                entry["RESTIC_REST_USERNAME"] == claim and entry["htpasswd"].startswith(claim+":") and
                entry["htpasswd"].endswith("\n") and entry["htpasswd"].count("\n") == 1,
                "repository recovery credentials are incomplete or differ")
        require(secret(files,"restic-"+claim) == {key:value.encode() for key,value in entry.items() if key != "htpasswd"},
                "saved repository Secret differs")
        authentication += entry["htpasswd"].encode()
    require(secret(files,"restic-server-auth") == {".htpasswd":authentication}, "saved server authentication differs")
    root_ca=secret(files,"restic-root-ca")
    require(root_ca.get("tls.crt") == files["ca.crt"] and root_ca.get("tls.key") and files["ca.crt"].startswith(b"-----BEGIN CERTIFICATE-----"),
            "saved CA credentials differ")
    tls=secret(files,"restic-server-tls")
    require(tls.get("tls.crt") and tls.get("tls.key"), "saved server TLS credentials are incomplete")
    endpoint=document(files,"endpoint-verification.json")
    require(endpoint.get("result") == "passed" and endpoint.get("namespace") == NAMESPACE and endpoint.get("repositories") == list(CLAIMS),
            "saved endpoint verification is incomplete")
    dependencies=document(files,"dependency-inventory.json")["resources"]
    for kind,values in dependencies.items():
        for name,entry in values.items():
            if not entry["present"]:
                continue
            filename=entry["saved_file"]
            item=resource(files,filename,"Secret" if kind=="secret" else "ConfigMap",name)
            keys=set(item.get("data",{})) | set(item.get("binaryData",{}))
            if kind=="secret":
                secret(files,name,filename)
            for ref in entry.get("references",[]):
                require(ref.get("optional") is True or set(ref.get("keys",[])).issubset(keys), "required dependency keys are missing")
    application=secret(files,"paperless-env")
    require(all(application.get(key) for key in ("POSTGRES_DB","POSTGRES_USER","POSTGRES_PASSWORD","PAPERLESS_API_TOKEN")) and
            "paperless-litellm-env.secret.json" in files and "paperless-config.configmap.json" in files,
            "saved application or integration credentials/configuration are incomplete")
    controllers={}
    for name,(kind,uid,digest) in WORKLOADS.items():
        controller=resource(files,name+"."+kind+".json","StatefulSet" if kind=="statefulset" else "Deployment",name,"apps/v1")
        containers=controller.get("spec",{}).get("template",{}).get("spec",{}).get("containers",[])
        require(controller["metadata"].get("uid") == uid and controller.get("spec",{}).get("replicas") == 1 and len(containers) == 1 and
                containers[0].get("name") == name and containers[0].get("image","").endswith("@sha256:"+digest),
                "saved application controller identity or image differs")
        controllers[name]=controller
    identities={}
    for component,claim in COMPONENT_CLAIMS.items():
        pvc=resource(files,claim+".pvc.json","PersistentVolumeClaim",claim)
        pv=resource(files,claim+".pv.json","PersistentVolume",claim,namespace=None)
        binding={"claim_uid":pvc["metadata"].get("uid"),"volume_uid":pv["metadata"].get("uid")}
        expected=PROTECTED[component]
        require(binding == {key:expected[key] for key in ("claim_uid","volume_uid")} and
                pvc["spec"].get("volumeName") == claim and pv["spec"].get("claimRef",{}).get("uid") == binding["claim_uid"] and
                pv["spec"]["claimRef"].get("namespace") == NAMESPACE and pv["spec"]["claimRef"].get("name") == claim and
                pv["spec"].get("local",{}).get("path") == str(expected["path"]) and
                pv["spec"].get("persistentVolumeReclaimPolicy") == "Retain" and
                all(item["spec"].get("accessModes") == ["ReadWriteOnce"] for item in (pv,pvc)), "saved live binding or path differs")
        identities[component]=binding
    migration=document(files,"gpt-storage-migration/state.json")
    migrated=document(files,"gpt-storage-migration/result.json")
    copied=document(files,"gpt-storage-migration/copy-validation.json")
    require(migration.get("version") == 1 and migration.get("phase") == "complete" and migration.get("authoritative") == "target" and
            migration.get("copy_sha256") == fingerprint(copied) and migrated.get("result") == "passed" and
            migrated.get("namespace") == NAMESPACE and migrated.get("component") == "paperless-gpt" and
            migrated.get("claims") == ["paperless-gpt-config","paperless-gpt-db"] and migrated.get("sqlite_integrity") == "passed" and
            migrated.get("live_template_fields_preserved") is True, "saved GPT migration verification differs")
    for component in ("config","db"):
        require(migration.get("bindings",{}).get(component) == {"pv":identities["gpt-"+component]["volume_uid"],"pvc":identities["gpt-"+component]["claim_uid"]},
                "saved GPT migration binding differs")
    ai=document(files,AI_CAPTURE+"receipt.json")
    require(ai.get("result") == "passed" and ai.get("classification") == "generated_api_documentation" and
            ai.get("pinned_generator_verified") is True and ai.get("source_and_pod_unchanged") is True and
            ai.get("file_sha256") == AI_SCHEMA_SHA256 and hashlib.sha256(files[AI_CAPTURE+"openapi.json"]).hexdigest() == AI_SCHEMA_SHA256,
            "saved AI generated-schema capture differs")
    initial=identifier(document(files,"initial-backup.json").get("backup_id"),"initial")
    restore=document(files,"initial-restore.json")
    verified=document(files,"initial-backup-verification.json")
    restored=document(files,"initial-restore-verification.json")
    require(verified.get("result") == "passed" and verified.get("backup_id") == initial and set(verified.get("components",{})) == set(COMPONENTS) and
            restore.get("backup_id") == initial and set(restore.get("components",{})) == set(COMPONENTS) and
            restored.get("result") == "passed" and restored.get("backup_id") == initial and restored.get("namespace") == RESTORE_NAMESPACE and
            set(restored.get("components",{})) == set(COMPONENTS) and restored.get("validator_network_access") is False and
            all(restored.get(key) is True for key in ("postgres_native_restore","postgres_credentials_verified","postgres_row_counts_verified","redis_native_restore","production_unchanged")),
            "saved initial backups or isolated native restores are incomplete")
    timestamp(restored.get("completed_at"))
    initial_snapshots,source_uids={},{}
    for component in COMPONENTS:
        prefix="initial-"+component
        source=completed(files,prefix+"-volsync-result.json",component,initial)
        destination=completed(files,prefix+"-volsync-restore-result.json",component,initial,restore=True)
        uploaded,_=backup_result(files,prefix+"-backup-result.json",component,initial)
        expected={"restore_as_of":source["status"]["lastSyncTime"],"snapshot":uploaded["snapshot"],"checksums":uploaded["checksums"]}
        artifact=restored["components"][component]
        checked=document(files,prefix+"-restore-files.json")
        require(restore["components"][component] == expected and verified["components"][component] == uploaded and
                artifact.get("snapshot") == uploaded["snapshot"] and artifact.get("claim") == COMPONENT_CLAIMS[component] and
                all(artifact.get(key) is True for key in ("archive_metadata_verified","file_checksums_verified")) and
                destination["spec"]["restic"].get("restoreAsOf") == expected["restore_as_of"] and
                checked.get("result") == "passed" and checked.get("backup_id") == initial and checked.get("component") == component and
                checked.get("claim") == COMPONENT_CLAIMS[component] and checked.get("files") == artifact.get("files") and
                checked.get("directories") == artifact.get("directories") and
                len(checked.get("sqlite_tables",{})) == artifact.get("sqlite_databases") and
                sum(len(tables) for tables in checked.get("sqlite_tables",{}).values()) == artifact.get("sqlite_tables"),
                "saved file/SQLite restore differs from the verified initial snapshot")
        initial_snapshots[component]=uploaded["snapshot"]
        source_uids[component]=source["metadata"]["uid"]
    jobs={}
    for component,name in JOBS.items():
        job=resource(files,name+"-job.json","Job",name,"batch/v1",namespace=RESTORE_NAMESPACE)
        annotations=job["metadata"].get("annotations",{})
        spec=job["spec"]["template"]["spec"]
        digest=TOOLS_DIGEST if component=="files" else WORKLOADS[component][2]
        require(annotations.get("backups.mousses.xyz/generation") == initial and
                annotations.get("backups.mousses.xyz/code-sha256") == VALIDATOR_CODE_SHA256 and
                annotations.get("backups.mousses.xyz/snapshots-sha256") == fingerprint(restore["components"]) and
                spec.get("automountServiceAccountToken") is False and len(spec.get("containers",[])) == 1 and
                spec["containers"][0].get("image","").endswith("@sha256:"+digest), "saved isolated native restore Job differs")
        jobs[component]=job
    for component in COMPONENTS:
        proof=document(files,"initial-"+component+"-restore-files.json")
        require(proof.get("job") == JOBS["files"] and proof.get("job_uid") == jobs["files"]["metadata"]["uid"], "file validator receipt identity differs")
    pg=document(files,"initial-postgres-restore-native.json")
    redis=document(files,"initial-redis-restore-native.json")
    require(pg.get("result") == "passed" and pg.get("backup_id") == initial and pg.get("job_uid") == jobs["postgres"]["metadata"]["uid"] and
            pg.get("database") == application["POSTGRES_DB"].decode() and pg.get("role") == application["POSTGRES_USER"].decode() and
            int(pg.get("server_version_num",0)) // 10000 == 18 and redis.get("result") == "passed" and redis.get("backup_id") == initial and
            redis.get("job_uid") == jobs["redis"]["metadata"]["uid"] and redis.get("native_rdb_loaded") is True and redis.get("tcp_listener") is False,
            "saved PostgreSQL credential or Redis native restore differs")
    writer_journal(files,initial_files(initial)[0],initial,identities,controllers)
    daily=document(files,"latest-daily-backup.json")
    generation=identifier(daily.get("backup_id"),"daily")
    require(daily.get("result") == "passed" and daily.get("initial_backup_id") == initial and daily.get("retention") == "within 7d" and
            set(daily.get("components",{})) == set(COMPONENTS) and daily.get("source_uids") == source_uids, "saved daily backup verification is incomplete")
    timestamp(daily.get("completed_at"))
    writer_journal(files,"writer-quiescence.json",generation,identities,controllers)
    production_anchor(restored.get("production",{}),identities,controllers)
    production_anchor(daily.get("production",{}),identities,controllers)
    production_anchor(document(files,"source-bindings.json"),identities,controllers)
    daily_snapshots={}
    for component in COMPONENTS:
        prefix=generation+"-"+component
        source=completed(files,prefix+"-volsync-result.json",component,generation)
        checked,_=backup_result(files,prefix+"-backup-result.json",component,generation)
        require(daily["components"][component] == checked and source["metadata"]["uid"] == source_uids[component], "daily backup receipt or source identity differs")
        daily_snapshots[component]=checked["snapshot"]
    schedule=document(files,"daily-schedule.json")
    require(schedule.get("result") == "enabled" and schedule.get("unit") == "k3s-paperless-backup.timer" and
            schedule.get("schedule_utc") == "*-*-* 12:15:00 UTC" and schedule.get("persistent") is True and
            schedule.get("recovery_unit") == "k3s-paperless-backup-recovery.service" and schedule.get("recovery_at_boot") is True and
            isinstance(schedule.get("revision"),str) and re.fullmatch(r"[0-9a-f]{40}",schedule["revision"]), "saved daily schedule or recovery differs")
    identifier(schedule.get("first_verified_daily_generation"),"daily")
    require(set(schedule.get("snapshots",{})) == set(COMPONENTS) and all(isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value) for value in schedule["snapshots"].values()) and
            (schedule["first_verified_daily_generation"] != generation or schedule["snapshots"] == daily_snapshots), "saved first daily snapshot identities differ")
    retention=document(files,"retention-proof.json")
    require(retention.get("result") == "passed" and retention.get("retention") == "within 7d" and
            isinstance(retention.get("evidence"),str) and re.fullmatch(r"scratch retention proof passed: 9-day snapshot expired; 6-day and current retained; [1-9][0-9]* bytes reclaimed",retention["evidence"]),
            "saved retention/pruning proof is incomplete")
    return {"initial_backup_id":initial,"initial_snapshots":initial_snapshots,"daily_backup_id":generation,"daily_snapshots":daily_snapshots,
            "repository_credentials_verified":True,"application_configuration_verified":True,"isolated_native_restores_verified":True}


def make_archive(files, recipient):
    evidence = validate_files(files)
    manifest = {
        "version": 1, "namespace": NAMESPACE, "claims": list(CLAIMS), "recipient": recipient,
        "exported_at": datetime.now(timezone.utc).isoformat(), **evidence,
        "files": {name: {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                  for name, value in files.items()},
        "archive_paths": {name: NAMESPACE + "/" + name for name in files},
    }
    payload = {**files, MANIFEST: (json.dumps(manifest, indent=2) + "\n").encode()}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, value in payload.items():
            member = tarfile.TarInfo(NAMESPACE + "/" + name)
            member.size = len(value)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(value))
    require(len(output.getvalue()) <= MAX_ARCHIVE, "the recovery archive is too large")
    return output.getvalue()

def export(recipient):
    require(os.geteuid() == 0, "export requires sudo python3")
    require(not sys.stdout.isatty(), "export must be piped directly into age encryption")
    current = Path("/root")
    for component in RECOVERY.relative_to(current).parts:
        current /= component
        attributes = current.lstat()
        require(stat.S_ISDIR(attributes.st_mode) and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o700, "recovery directory must be private and root-owned")
    descriptor = os.open(RECOVERY / ".setup.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as lock:
        attributes = os.fstat(lock.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o600, "namespace lock is not private and root-owned")
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryError("a Paperless operation is running; retry after it finishes") from error
        require(not os.path.lexists(RECOVERY / "daily-active.json"),
                "an unfinished daily run remains; finish it before exporting")
        files = {name: recovery_bytes(name) for name in FILES}
        files.update({name: recovery_bytes(name) for name in dependency_files(files)})
        generation = document(files, "latest-daily-backup.json").get("backup_id")
        files.update({name: recovery_bytes(name) for name in daily_files(generation)})
        initial = document(files, "initial-backup.json").get("backup_id")
        files.update({name: recovery_bytes(name) for name in initial_files(initial)})
        archive = make_archive(files, recipient)
    sys.stdout.buffer.write(archive)
    sys.stdout.buffer.flush()
    print("private recovery archive prepared: " + str(len(files)) + " files", file=sys.stderr)

def verify_archive(compressed, recipient):
    require(0 < len(compressed) <= MAX_ARCHIVE, "the decrypted archive is empty or too large")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
        expanded = archive.read(MAX_TAR + 1)
    require(len(expanded) <= MAX_TAR, "the expanded recovery archive is too large")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            name = member.name.removeprefix(NAMESPACE + "/")
            allowed = name in FILES or name == MANIFEST or re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\.(?:secret|configmap)\.json",name) or re.fullmatch(
                r"daily-[0-9a-f]{32}-(?:data|ai|gpt|gpt-config|gpt-db|postgres|redis)-(?:volsync-result|backup-result)\.json", name) or re.fullmatch(r"initial-[0-9a-f]{32}-writer-quiescence\.json", name)
            require(member.name == NAMESPACE + "/" + name and allowed and name not in files and
                    len(files) < 512 and member.isfile() and not member.pax_headers and
                    member.mode == 0o600 and member.uid == 0 and member.gid == 0 and 0 < member.size <= MAX_FILE,
                    "the recovery archive contains an unexpected or duplicate entry")
            with archive.extractfile(member) as content:
                files[name] = content.read(MAX_FILE + 1)
            require(len(files[name]) == member.size, "a recovery archive member is truncated")
    manifest = document(files, MANIFEST)
    del files[MANIFEST]
    evidence = validate_files(files)
    require(manifest.get("version") == 1 and manifest.get("namespace") == NAMESPACE and
            manifest.get("claims") == list(CLAIMS) and manifest.get("recipient") == recipient and
            set(manifest.get("files", {})) == set(files) and
            manifest.get("archive_paths") == {name: NAMESPACE + "/" + name for name in files},
            "the recovery manifest identity differs")
    timestamp(manifest.get("exported_at"))
    for name, value in files.items():
        require(manifest["files"][name] == {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)},
                "a recovery archive checksum differs")
    require(all(manifest.get(key) == value for key, value in evidence.items()),
            "the recovery manifest evidence differs from the saved results")
    return {"namespace": NAMESPACE, "claims": list(CLAIMS), "files_verified": len(files),
            "recipient": recipient, "exported_at": manifest["exported_at"], **evidence}

def verify(arguments):
    require(shutil.which("age") and shutil.which("age-keygen"), "install age on this computer first")
    identity = private_bytes(arguments.identity, os.geteuid(), maximum=MAX_IDENTITY)
    encrypted = private_bytes(arguments.archive, os.geteuid(), maximum=MAX_ARCHIVE)
    digest = hashlib.sha256(encrypted).hexdigest()
    require(digest == arguments.expected_sha256, "the encrypted archive checksum differs from the NAS export")
    recipient = subprocess.run(["age-keygen", "-y"], input=identity, capture_output=True, timeout=30)
    require(recipient.returncode == 0, "age could not read the private recovery key")
    # Both tools use the validated key bytes. Reopening its pathname would allow
    # replacement after the ownership/permissions check. The small native key
    # stays in memory; the child inherits only this pipe's read descriptor.
    identity_read, identity_write = os.pipe()
    try:
        try:
            require(os.write(identity_write, identity) == len(identity), "could not prepare the recovery key stream")
        finally:
            os.close(identity_write)
        decrypted = subprocess.run(["age", "--decrypt", "--identity", "/dev/fd/" + str(identity_read)],
                                   pass_fds=(identity_read,), input=encrypted, capture_output=True, timeout=120)
    finally:
        os.close(identity_read)
    require(decrypted.returncode == 0, "age decryption failed; the copy has not been verified")
    receipt = {"result": "passed", "archive_sha256": digest, "verified_at": datetime.now(timezone.utc).isoformat(),
               **verify_archive(decrypted.stdout, recipient.stdout.decode("ascii").strip())}
    # Decryption and archive inspection stay in memory. Only public evidence is written.
    if arguments.receipt:
        descriptor = os.open(arguments.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(receipt, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
    print(json.dumps(receipt, indent=2))
    print("off-NAS recovery copy verified; no decrypted files were written")

def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export")
    exporter.add_argument("--recipient", required=True)
    verifier = commands.add_parser("verify")
    verifier.add_argument("--archive", type=Path, required=True)
    verifier.add_argument("--identity", type=Path, required=True)
    verifier.add_argument("--expected-sha256", required=True)
    verifier.add_argument("--receipt", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "export":
        require(re.fullmatch(r"age1[a-z0-9]{58}", arguments.recipient), "supply your native age public key")
        export(arguments.recipient)
    else:
        require(re.fullmatch(r"[0-9a-f]{64}", arguments.expected_sha256), "supply the NAS export's full SHA-256 checksum")
        verify(arguments)

if __name__ == "__main__":
    try:
        main()
    except RecoveryError as error:
        print("recovery stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, RecursionError,
            tarfile.TarError, zlib.error, subprocess.TimeoutExpired):
        # Never echo parsing errors, tool stderr, or private credential contents.
        print("recovery stopped: private files, archive format, or tool execution could not be verified", file=sys.stderr)
        sys.exit(1)
