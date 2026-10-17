#!/usr/bin/env python3
"""Export monitoring recovery credentials on OMV; verify an encrypted copy off OMV."""

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


NAMESPACE = "monitoring"
COMPONENT_CLAIMS = {
    "prometheus": "prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0",
    "alertmanager": "alertmanager-kube-prometheus-stack-alertmanager-db-alertmanager-kube-prometheus-stack-alertmanager-0",
    "grafana": "kube-prometheus-stack-grafana",
    "loki": "storage-loki-0",
}
COMPONENT_VOLUMES = {component: "monitoring-" + component for component in COMPONENT_CLAIMS}
COMPONENTS = tuple(COMPONENT_CLAIMS)
CLAIMS = tuple(COMPONENT_CLAIMS.values())
# USTAR basenames are limited to 100 bytes; the two longest claim names exceed it.
FILE_ALIASES = {
    name: alias for component, claim in COMPONENT_CLAIMS.items()
    for name, alias in (("restic-" + claim + ".secret.json", "restic-" + component + ".secret.json"),
                        (claim + ".pvc.json", component + ".pvc.json"))
}
ORIGINAL_FILES = {alias: name for name, alias in FILE_ALIASES.items()}
RECOVERY = Path("/root/k3s-backup-recovery") / NAMESPACE
FILES = (
    "credentials.json", "ca.crt", "endpoint-verification.json",
    *("restic-" + claim + ".secret.json" for claim in CLAIMS),
    "restic-server-auth.secret.json", "restic-server-tls.secret.json", "restic-root-ca.secret.json",
    *(name for component, claim in COMPONENT_CLAIMS.items()
      for name in (claim + ".pvc.json", COMPONENT_VOLUMES[component] + ".pv.json", component + "-quiescence.json")),
    "initial-backup.json", "initial-restore.json",
    "initial-backup-verification.json", "initial-restore-verification.json",
    *("initial-" + component + "-" + suffix + ".json"
      for component in COMPONENTS
      for suffix in ("volsync-result", "backup-result", "volsync-restore-result", "restore-result")),
    "latest-daily-backup.json", "daily-schedule.json", "retention-proof.json",
)
ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "metadata.json", "backup-id"}
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
    require(isinstance(value, str) and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None,
            "saved evidence timestamp is invalid")


def daily_files(generation):
    identifier(generation, "daily")
    return tuple(generation + "-" + component + "-" + suffix + ".json"
                 for component in COMPONENTS for suffix in ("volsync-result", "backup-result"))


def expected_files(files):
    require(set(FILES) <= set(files), "the recovery file set is incomplete")
    generation = document(files, "latest-daily-backup.json").get("backup_id")
    return set(FILES) | set(daily_files(generation))


def archive_name(name):
    return FILE_ALIASES.get(name, name)


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
                     namespace="monitoring-restore-validation" if restore else NAMESPACE)
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
            restic.get("repository") == ("restic-restore-" + component if restore else "restic-" + claim),
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


def validate_files(files):
    require(set(files) == expected_files(files), "the recovery file set is incomplete or unexpected")
    require(all(isinstance(value, bytes) and 0 < len(value) <= MAX_FILE for value in files.values()) and
            sum(map(len, files.values())) <= MAX_CONTENT, "recovery contents exceed the size limits")
    credentials = document(files, "credentials.json")
    entries = credentials.get("repositories", {})
    require(set(credentials) == {"namespace", "repositories"} and credentials["namespace"] == NAMESPACE and
            isinstance(entries, dict) and set(entries) == set(CLAIMS), "saved repository identities differ")
    authentication = b""
    for claim in CLAIMS:
        entry = entries[claim]
        require(isinstance(entry, dict) and set(entry) == {
            "RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD", "htpasswd"
        } and all(isinstance(value, str) and value for value in entry.values()) and
            entry["RESTIC_REPOSITORY"] == "rest:https://restic.monitoring.svc:8000/" + claim + "/" and
            entry["RESTIC_REST_USERNAME"] == claim and entry["htpasswd"].startswith(claim + ":") and
            entry["htpasswd"].endswith("\n") and entry["htpasswd"].count("\n") == 1,
            "saved repository recovery credentials are incomplete or differ")
        fields = {key: value.encode() for key, value in entry.items() if key != "htpasswd"}
        require(secret(files, "restic-" + claim) == fields, "saved repository Secret differs from its credentials")
        authentication += entry["htpasswd"].encode()
    require(secret(files, "restic-server-auth") == {".htpasswd": authentication},
            "saved server authentication differs from the repository credentials")
    root_ca = secret(files, "restic-root-ca")
    require(root_ca.get("tls.crt") == files["ca.crt"] and root_ca.get("tls.key") and
            files["ca.crt"].startswith(b"-----BEGIN CERTIFICATE-----"), "saved CA credentials are missing or differ")
    tls = secret(files, "restic-server-tls")
    require(tls.get("tls.crt") and tls.get("tls.key"), "saved server TLS credentials are missing")
    endpoint = document(files, "endpoint-verification.json")
    require(endpoint.get("result") == "passed" and endpoint.get("namespace") == NAMESPACE and
            endpoint.get("repositories") == list(CLAIMS), "saved endpoint verification is incomplete")

    initial = identifier(document(files, "initial-backup.json").get("backup_id"), "initial")
    restore = document(files, "initial-restore.json")
    verified = document(files, "initial-backup-verification.json")
    restored = document(files, "initial-restore-verification.json")
    require(restore.get("backup_id") == initial and restore.get("namespace") == "monitoring-restore-validation" and
            set(restore.get("components", {})) == set(COMPONENTS), "saved initial restore generation differs")
    require(all(item.get("result") == "passed" and item.get("backup_id") == initial and
                set(item.get("components", {})) == set(COMPONENTS) for item in (verified, restored)) and
            restored.get("namespace") == "monitoring-restore-validation", "saved initial verification is incomplete")
    initial_snapshots = {}
    for component in COMPONENTS:
        prefix = "initial-" + component
        source = completed(files, prefix + "-volsync-result.json", component, initial)
        destination = completed(files, prefix + "-volsync-restore-result.json", component, initial, restore=True)
        uploaded, _ = backup_result(files, prefix + "-backup-result.json", component, initial)
        expected = {"restore_as_of": source["status"]["lastSyncTime"], "snapshot": uploaded["snapshot"],
                    "checksums": uploaded["checksums"]}
        require(restore["components"][component] == expected and
                destination["spec"]["restic"].get("restoreAsOf") == expected["restore_as_of"] and
                verified["components"][component] == uploaded, "saved initial artifacts differ")
        result = document(files, prefix + "-restore-result.json")
        require(result == restored["components"][component] and result.get("result") == "passed" and
                result.get("component") == component and result.get("claim") == COMPONENT_CLAIMS[component] and
                result.get("backup_id") == initial and result.get("snapshot") == uploaded["snapshot"] and
                all(result.get(key) is True for key in
                    ("archive_metadata_verified", "file_checksums_verified", "application_config_verified")),
                "saved isolated restore verification differs")
        if component == "grafana":
            require(result.get("sqlite_integrity") == "ok" and isinstance(result.get("table_counts"), dict) and
                    bool(result["table_counts"]), "saved Grafana database verification is incomplete")
        else:
            require(result.get("native_recovery_verified") is True, "saved native recovery verification is incomplete")
        if component in ("prometheus", "loki"):
            require(type(result.get("recovery_error_counters")) is int and result["recovery_error_counters"] == 0,
                    "saved native recovery reported errors")
        if component == "prometheus":
            require(type(result.get("historical_up_series")) is int and result["historical_up_series"] > 0,
                    "saved Prometheus historical metric verification is incomplete")
        if component == "loki":
            require(result.get("loopback_routes_verified") is True and
                    type(result.get("historical_log_entries")) is int and result["historical_log_entries"] > 0,
                    "saved Loki historical log verification is incomplete")
        initial_snapshots[component] = uploaded["snapshot"]

    daily = document(files, "latest-daily-backup.json")
    generation = identifier(daily.get("backup_id"), "daily")
    require(daily.get("result") == "passed" and daily.get("initial_backup_id") == initial and
            daily.get("retention") == "within 7d" and set(daily.get("components", {})) == set(COMPONENTS),
            "saved daily backup verification is incomplete")
    timestamp(daily.get("completed_at"))
    daily_snapshots = {}
    for component, claim in COMPONENT_CLAIMS.items():
        prefix = generation + "-" + component
        completed(files, prefix + "-volsync-result.json", component, generation)
        result, _ = backup_result(files, prefix + "-backup-result.json", component, generation)
        require(daily["components"][component] == result, "daily backup receipts disagree")
        quiescence = document(files, component + "-quiescence.json")
        pvc = resource(files, claim + ".pvc.json", "PersistentVolumeClaim", claim)
        pv = resource(files, COMPONENT_VOLUMES[component] + ".pv.json", "PersistentVolume",
                      COMPONENT_VOLUMES[component], namespace=None)
        identities = {"claim_uid": pvc["metadata"]["uid"], "volume_uid": pv["metadata"]["uid"]}
        require(all(isinstance(value, str) and value for value in identities.values()) and
                pvc["spec"].get("volumeName") == COMPONENT_VOLUMES[component] and
                pv["spec"].get("claimRef", {}).get("uid") == identities["claim_uid"] and
                pv["spec"]["claimRef"].get("namespace") == NAMESPACE and
                pv["spec"]["claimRef"].get("name") == claim, "saved production volume binding differs")
        require(quiescence.get("backup_id") == generation and quiescence.get("component") == component and
                quiescence.get("phase") == "resumed" and quiescence.get("original_replicas") == 1 and
                all(quiescence.get(key) == value for key, value in identities.items()),
                "a latest monitoring writer has not resumed or its storage changed")
        anchor = daily.get("identities", {}).get(component, {})
        require(all(anchor.get(key) == value for key, value in identities.items()) and
                anchor.get("workload_uid") == quiescence.get("workload_uid") and
                isinstance(anchor.get("workload_uid"), str) and bool(anchor["workload_uid"]),
                "daily production identities disagree with recovery metadata")
        daily_snapshots[component] = result["snapshot"]
    schedule = document(files, "daily-schedule.json")
    require(schedule.get("result") == "enabled" and schedule.get("unit") == "k3s-monitoring-backup.timer" and
            schedule.get("schedule_utc") == "*-*-* 10:45:00 UTC" and schedule.get("persistent") is True and
            schedule.get("recovery_unit") == "k3s-monitoring-backup-recovery.service" and
            schedule.get("recovery_at_boot") is True and
            isinstance(schedule.get("revision"), str) and re.fullmatch(r"[0-9a-f]{40}", schedule["revision"]),
            "saved daily schedule or writer recovery verification is incomplete")
    proof = document(files, "retention-proof.json")
    require(proof.get("result") == "passed" and proof.get("retention") == "within 7d" and
            isinstance(proof.get("evidence"), str) and re.fullmatch(
                r"scratch retention proof passed: 9-day snapshot expired; 6-day and current retained; [1-9][0-9]* bytes reclaimed",
                proof["evidence"]), "saved retention and pruning proof is incomplete")
    return {"initial_backup_id": initial, "initial_snapshots": initial_snapshots,
            "daily_backup_id": generation, "daily_snapshots": daily_snapshots}


def make_archive(files, recipient):
    evidence = validate_files(files)
    manifest = {
        "version": 1, "namespace": NAMESPACE, "claims": list(CLAIMS), "recipient": recipient,
        "exported_at": datetime.now(timezone.utc).isoformat(), **evidence,
        "files": {name: {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                  for name, value in files.items()},
        "archive_paths": {name: NAMESPACE + "/" + archive_name(name) for name in files},
    }
    payload = {**files, MANIFEST: (json.dumps(manifest, indent=2) + "\n").encode()}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, value in payload.items():
            member = tarfile.TarInfo(NAMESPACE + "/" + archive_name(name))
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
            raise RecoveryError("a monitoring operation is running; retry after it finishes") from error
        require(not os.path.lexists(RECOVERY / "daily-active.json"),
                "an unfinished daily run remains; finish it before exporting")
        files = {name: private_bytes(RECOVERY / name, 0) for name in FILES}
        generation = document(files, "latest-daily-backup.json").get("backup_id")
        files.update({name: private_bytes(RECOVERY / name, 0) for name in daily_files(generation)})
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
            basename = member.name.removeprefix(NAMESPACE + "/")
            name = ORIGINAL_FILES.get(basename, basename)
            allowed = name in FILES or name == MANIFEST or re.fullmatch(
                r"daily-[0-9a-f]{32}-(?:prometheus|alertmanager|grafana|loki)-(?:volsync-result|backup-result)\.json", name)
            require(member.name == NAMESPACE + "/" + archive_name(name) and allowed and name not in files and
                    len(files) < len(FILES) + 2 * len(COMPONENTS) + 1 and member.isfile() and not member.pax_headers and
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
            manifest.get("archive_paths") == {name: NAMESPACE + "/" + archive_name(name) for name in files},
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
