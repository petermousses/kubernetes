#!/usr/bin/env python3
"""Export private recovery files on OMV; verify the encrypted copy off OMV."""

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


NAMESPACE = "board-games"
PVC = "data-postgres-0"
RECOVERY = Path("/root/k3s-backup-recovery") / NAMESPACE / PVC
REPOSITORY = "rest:https://restic.board-games.svc.cluster.local:8000/data-postgres-0/"
MOVER_REPOSITORY = "rest:https://restic.board-games.svc:8000/data-postgres-0/"
FILES = (
    "credentials.json", "ca.crt",
    "restic-data-postgres-0.secret.json", "restic-data-postgres-0-mover.secret.json",
    "restic-server-auth.secret.json", "restic-server-tls.secret.json", "restic-root-ca.secret.json",
    "board-games-secrets.secret.json", "latest-board-games-secrets.secret.json",
    "initial-backup.json", "initial-volsync-result.json", "initial-restore.json",
    "initial-volsync-restore-result.json", "initial-database-restore-result.json",
    "latest-daily-backup.json", "latest-daily-volsync-result.json",
    "daily-schedule.json", "retention-proof.json",
)
MANIFEST = "recovery-manifest.json"
MAX_FILE = 1024 * 1024
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_TAR = 32 * 1024 * 1024


class RecoveryError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def private_bytes(path, owner):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise RecoveryError("could not open the private recovery file: " + path.name) from error
    with os.fdopen(descriptor, "rb") as content:
        attributes = os.fstat(content.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_uid == owner and
                stat.S_IMODE(attributes.st_mode) == 0o600,
                "expected a private regular file: " + path.name)
        value = content.read(MAX_FILE + 1)
        require(0 < len(value) <= MAX_FILE, "recovery file is empty or too large: " + path.name)
        return value


def document(files, name):
    try:
        value = json.loads(files[name])
    except (ValueError, UnicodeError) as error:
        raise RecoveryError("invalid recovery JSON: " + name) from error
    require(isinstance(value, dict), "expected a JSON object: " + name)
    return value


def completed(source, identifier):
    status = source.get("status", {})
    return (source.get("spec", {}).get("trigger") == {"manual": identifier} and
            not source["spec"].get("paused") and
            status.get("lastManualSync") == identifier and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and
            status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(condition.get("type") == "Synchronizing" and condition.get("status") == "False"
                and condition.get("reason") == "WaitingForManual"
                for condition in status.get("conditions", [])))


def validate_files(files):
    require(set(files) == set(FILES), "the recovery file set is incomplete or unexpected")
    credentials = document(files, "credentials.json")
    fields = {key: value for key, value in credentials.items() if key != "htpasswd"}
    require(set(credentials) == {"RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME",
                                 "RESTIC_REST_PASSWORD", "htpasswd"} and
            all(isinstance(value, str) and value for value in credentials.values()) and
            credentials["RESTIC_REPOSITORY"] == REPOSITORY and credentials["RESTIC_REST_USERNAME"] == PVC,
            "repository recovery credentials are incomplete or target another repository")

    def secret(name, filename=None):
        saved = document(files, filename or name + ".secret.json")
        require(saved.get("kind") == "Secret" and saved.get("apiVersion") == "v1" and
                saved.get("metadata", {}).get("name") == name and
                saved["metadata"].get("namespace") == NAMESPACE,
                "saved Secret identity differs: " + name)
        data = saved.get("data", {})
        require(isinstance(data, dict) and data, "saved Secret data is missing: " + name)
        return {key: base64.b64decode(value, validate=True) for key, value in data.items()}

    require(secret("restic-data-postgres-0") == {key: value.encode() for key, value in fields.items()},
            "saved repository credentials do not match their Secret")
    require(secret("restic-data-postgres-0-mover") == {
        key: value.encode() for key, value in {**fields, "RESTIC_REPOSITORY": MOVER_REPOSITORY}.items()
    }, "saved mover credentials do not match the repository credentials")
    require(secret("restic-server-auth").get(".htpasswd") == credentials["htpasswd"].encode(),
            "saved server authentication differs from the recovery credentials")
    root_ca = secret("restic-root-ca")
    require(root_ca.get("tls.crt") == files["ca.crt"] and root_ca.get("tls.key") and
            files["ca.crt"].startswith(b"-----BEGIN CERTIFICATE-----"),
            "saved repository CA or its recovery key is missing or differs")
    server = secret("restic-server-tls")
    require(server.get("tls.crt") and server.get("tls.key"), "saved server TLS credentials are missing")
    for filename in ("board-games-secrets.secret.json", "latest-board-games-secrets.secret.json"):
        require(secret("board-games-secrets", filename).get("POSTGRES_PASSWORD"),
                "saved application database credentials are missing")

    initial = document(files, "initial-backup.json")["backup_id"]
    require(isinstance(initial, str) and re.fullmatch(r"initial-[0-9a-f]{32}", initial),
            "initial backup generation is invalid")
    restore = document(files, "initial-database-restore-result.json")
    require(restore.get("result") == "passed" and restore.get("backup_id") == initial and
            restore.get("database") == "board_games" and restore.get("role") == "board_games" and
            len(restore.get("relation_counts", [])) == 4,
            "the saved initial database restore evidence is incomplete")
    initial_upload = document(files, "initial-volsync-result.json")
    initial_restore = document(files, "initial-restore.json")
    destination = document(files, "initial-volsync-restore-result.json")
    require(completed(initial_upload, initial) and completed(destination, initial) and
            initial_restore.get("backup_id") == initial and
            initial_restore.get("restore_as_of") == initial_upload["status"]["lastSyncTime"] and
            destination["spec"].get("restic", {}).get("restoreAsOf") == initial_restore["restore_as_of"],
            "the saved initial upload and artifact restore evidence differ")
    daily = document(files, "latest-daily-backup.json")
    require(daily.get("result") == "passed" and isinstance(daily.get("backup_id"), str) and
            re.fullmatch(r"daily-[0-9a-f]{32}", daily["backup_id"]) and
            isinstance(daily.get("snapshot"), str) and re.fullmatch(r"[0-9a-f]{64}", daily["snapshot"]) and
            completed(document(files, "latest-daily-volsync-result.json"), daily["backup_id"]),
            "the latest daily backup evidence is incomplete")
    schedule = document(files, "daily-schedule.json")
    require(schedule.get("result") == "enabled" and schedule.get("unit") == "k3s-board-games-backup.timer" and
            schedule.get("schedule_utc") == "*-*-* 09:15:00 UTC" and schedule.get("persistent") is True,
            "the saved daily schedule evidence is incomplete")
    require(document(files, "retention-proof.json").get("result") == "passed",
            "the saved retention proof is incomplete")
    return {"initial_backup_id": initial, "daily_backup_id": daily["backup_id"],
            "daily_snapshot": daily["snapshot"]}


def make_archive(files, recipient):
    evidence = validate_files(files)
    manifest = {
        "version": 1, "namespace": NAMESPACE, "pvc": PVC, "recipient": recipient,
        "exported_at": datetime.now(timezone.utc).isoformat(), **evidence,
        "files": {name: {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                  for name, value in files.items()},
    }
    payload = {**files, MANIFEST: (json.dumps(manifest, indent=2) + "\n").encode()}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, value in payload.items():
            info = tarfile.TarInfo(PVC + "/" + name)
            info.size = len(value)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(value))
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
                stat.S_IMODE(attributes.st_mode) == 0o700,
                "recovery directory must be private and root-owned")
    descriptor = os.open(RECOVERY / ".setup.lock", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryError("a board-games setup or backup is running; retry after it finishes") from error
        require(not os.path.lexists(RECOVERY / "daily-active.json"),
                "an unfinished daily run remains; preserve it and finish that run before exporting")
        files = {name: private_bytes(RECOVERY / name, 0) for name in FILES}
        archive = make_archive(files, recipient)
    sys.stdout.buffer.write(archive)
    sys.stdout.buffer.flush()
    print("private recovery archive prepared: " + str(len(FILES)) + " files", file=sys.stderr)


def verify_archive(compressed, recipient):
    with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
        expanded = archive.read(MAX_TAR + 1)
    require(len(expanded) <= MAX_TAR, "the expanded recovery archive is too large")
    files = {}
    expected = set(FILES) | {MANIFEST}
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            name = member.name.removeprefix(PVC + "/")
            require(member.name == PVC + "/" + name and name in expected and name not in files and
                    member.isfile() and member.mode == 0o600 and member.uid == 0 and
                    0 < member.size <= MAX_FILE,
                    "the recovery archive contains an unexpected or duplicate entry")
            with archive.extractfile(member) as content:
                files[name] = content.read(MAX_FILE + 1)
            require(len(files[name]) == member.size, "a recovery archive member is truncated")
    require(set(files) == expected, "the recovery archive is incomplete")
    manifest = document(files, MANIFEST)
    require(manifest.get("version") == 1 and manifest.get("namespace") == NAMESPACE and
            manifest.get("pvc") == PVC and manifest.get("recipient") == recipient and
            set(manifest.get("files", {})) == set(FILES), "the recovery manifest identity differs")
    for name in FILES:
        require(manifest["files"][name] == {
            "sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name]),
        }, "a recovery archive checksum differs: " + name)
    evidence = validate_files({name: files[name] for name in FILES})
    require(all(manifest.get(key) == value for key, value in evidence.items()),
            "the recovery manifest evidence differs from the saved results")
    return {"namespace": NAMESPACE, "pvc": PVC, "files_verified": len(FILES),
            "recipient": recipient, "exported_at": manifest["exported_at"], **evidence}


def verify(arguments):
    require(shutil.which("age") and shutil.which("age-keygen"), "install age on this computer first")
    private_bytes(arguments.identity, os.geteuid())
    attributes = arguments.archive.lstat()
    require(stat.S_ISREG(attributes.st_mode) and attributes.st_uid == os.geteuid() and
            stat.S_IMODE(attributes.st_mode) == 0o600 and 0 < attributes.st_size <= MAX_ARCHIVE,
            "the encrypted archive must be a private regular file owned by this user")
    digest = hashlib.sha256(arguments.archive.read_bytes()).hexdigest()
    require(digest == arguments.expected_sha256, "the encrypted archive checksum differs from the NAS export")
    recipient = subprocess.run(["age-keygen", "-y", str(arguments.identity)],
                               capture_output=True, text=True, timeout=30)
    require(recipient.returncode == 0, "age could not read the private recovery key")
    decrypted = subprocess.run(["age", "--decrypt", "--identity", str(arguments.identity), str(arguments.archive)],
                               capture_output=True, timeout=120)
    require(decrypted.returncode == 0, "age decryption failed; the copy has not been verified")
    receipt = {"result": "passed", "archive_sha256": digest,
               "verified_at": datetime.now(timezone.utc).isoformat(),
               **verify_archive(decrypted.stdout, recipient.stdout.strip())}
    # All decrypted bytes remain in memory. The receipt contains only public
    # identifiers, timestamps, and the encrypted archive's checksum.
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
        require(re.fullmatch(r"age1[a-z0-9]{58}", arguments.recipient), "supply the native age public key from your Mac")
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, tarfile.TarError,
            zlib.error, subprocess.TimeoutExpired):
        # Parsing errors and tool diagnostics must not echo credential contents.
        print("recovery stopped: files, archive format, or tool execution could not be verified", file=sys.stderr)
        sys.exit(1)
