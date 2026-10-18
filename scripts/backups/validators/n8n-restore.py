#!/usr/bin/env python3
"""Extract and validate n8n's paired initial backup in scratch volumes only."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile

ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "validation.json", "metadata.json", "backup-id"}
CLAIMS = {"data": "n8n-data-live", "images": "n8n-images"}


def digest(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("expected an independent regular file")
        checksum = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("file changed while verifying")
    return checksum.hexdigest()


def inventory(value):
    if not isinstance(value, list):
        raise ValueError("invalid archive inventory")
    entries = {}
    for entry in value:
        name = entry.get("path")
        if (not isinstance(name, str) or "\x00" in name or name in entries or
                (name != "." and (PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name or
                                   ".." in PurePosixPath(name).parts)) or
                entry.get("type") not in ("directory", "file")):
            raise ValueError("unsafe, duplicate, or unsupported archive path")
        if any(type(entry.get(key)) is not int for key in ("uid", "gid", "mode", "mtime_ns")) or not (
                0 <= entry["uid"] < 2**32 - 1 and 0 <= entry["gid"] < 2**32 - 1 and 0 <= entry["mode"] <= 0o7777):
            raise ValueError("invalid saved numeric metadata")
        if entry["type"] == "file" and (type(entry.get("bytes")) is not int or entry["bytes"] < 0 or
                not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))):
            raise ValueError("invalid saved file contents")
        entries[name] = entry
    if entries.get(".", {}).get("type") != "directory":
        raise ValueError("archive root is missing")
    for name in entries:
        if name != "." and entries.get(str(PurePosixPath(name).parent), {}).get("type") != "directory":
            raise ValueError("archive parent is missing")
    return entries


def artifacts(component, source, identifier, expected_checksums):
    checksums = {}
    for line in expected_checksums.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        if not match or match[2] in checksums:
            raise ValueError("invalid artifact checksum manifest")
        checksums[match[2]] = match[1]
    if set(checksums) != ARTIFACTS or {p.name for p in source.iterdir()} != ARTIFACTS | {"SHA256SUMS"}:
        raise ValueError("restored artifact set differs")
    digest(source / "SHA256SUMS")
    if (source / "SHA256SUMS").read_text() != expected_checksums or any(digest(source / name) != value for name, value in checksums.items()):
        raise ValueError("restored artifact checksum differs")
    if (source / "backup-id").read_text().strip() != identifier:
        raise ValueError("restored generation differs")
    metadata = json.loads((source / "metadata.json").read_text())
    config = json.loads((source / "configuration.json").read_text())
    proof = json.loads((source / "validation.json").read_text())
    if (metadata.get("backup_id") != identifier or metadata.get("pair_id") != identifier or
            metadata.get("claim") != CLAIMS[component] or config.get("pair_id") != identifier or
            metadata.get("image_id") != os.environ["EXPECTED_IMAGE"] or config.get("image_id") != os.environ["EXPECTED_IMAGE"] or
            proof.get("result") != "passed" or proof.get("backup_id") != identifier):
        raise ValueError("restored claim, image, or common generation differs")
    return inventory(json.loads((source / "contents.json").read_text())), config, proof


def extract(source, tree, entries):
    tree.mkdir(mode=0o700)
    with tarfile.open(source / "volume.tar", "r:") as archive:
        members = archive.getmembers()
        if len(members) != len(entries) or {member.name for member in members} != set(entries):
            raise ValueError("archive members differ from saved inventory")
        for member in members:
            entry = entries[member.name]
            stamp = f"{entry['mtime_ns'] // 1000000000}.{entry['mtime_ns'] % 1000000000:09d}"
            if ((member.uid, member.gid, member.mode) != (entry["uid"], entry["gid"], entry["mode"]) or
                    member.pax_headers.get("mtime") != stamp or member.linkname or
                    not (member.isfile() if entry["type"] == "file" else member.isdir())):
                raise ValueError("archive type or numeric metadata differs")
            if entry["type"] == "file" and member.size != entry["bytes"]:
                raise ValueError("archive file size differs")
        for name, entry in sorted(entries.items(), key=lambda pair: (len(PurePosixPath(pair[0]).parts), pair[0])):
            target = tree if name == "." else tree / name
            if name != "." and entry["type"] == "directory":
                target.mkdir(mode=0o700)
            elif entry["type"] == "file":
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output, archive.extractfile(archive.getmember(name)) as content:
                    shutil.copyfileobj(content, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if digest(target) != entry["sha256"]:
                    raise ValueError("extracted file contents differ")
        for name, entry in sorted(entries.items(), key=lambda pair: len(PurePosixPath(pair[0]).parts), reverse=True):
            target = tree if name == "." else tree / name
            os.chmod(target, entry["mode"])
            os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
    for name, entry in entries.items():
        target = tree if name == "." else tree / name
        attributes = target.lstat()
        if (stat.S_IMODE(attributes.st_mode), attributes.st_mtime_ns) != (entry["mode"], entry["mtime_ns"]):
            raise ValueError("extracted mode or timestamp differs")
        if entry["type"] == "file" and digest(target) != entry["sha256"]:
            raise ValueError("extracted file checksum differs")


def sqlite_check(tree, config, initial):
    if config["configmap"].get("data", {}).get("N8N_ENV_FEAT_ENCRYPTION_KEY_ROTATION", "false").lower() == "true":
        raise ValueError("rotated credential keys require a separate restore check")
    key_path = tree / "config"
    if digest(key_path) != initial.get("encryption_config_sha256"):
        raise ValueError("restored encryption configuration differs")
    key = json.loads(key_path.read_text()).get("encryptionKey")
    if not isinstance(key, str) or not key:
        raise ValueError("restored encryption key is missing")
    database = tree / "database.sqlite"
    digest(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchmany(1):
            raise ValueError("restored SQLite integrity or foreign keys failed")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
        if not {"workflow_entity", "credentials_entity"}.issubset(counts) or counts != initial.get("relation_counts"):
            raise ValueError("restored SQLite relation counts differ from the backup")
        # Only the scratch database is normalized. The final container reads it
        # through a read-only mount without needing to create WAL/SHM files.
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0] != 0 or connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise ValueError("scratch SQLite checkpoint failed")
    finally:
        connection.close()
    return {"sqlite_integrity": True, "relation_counts": counts, "encryption_key_preserved": True,
            "encryption_config_sha256": digest(key_path), "database_sha256": digest(database),
            "scratch_database_normalized": True}


def validate():
    identifier = os.environ["BACKUP_ID"]
    if not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise ValueError("invalid restore generation")
    results, configs = {}, {}
    for component in CLAIMS:
        root = Path(os.environ[component.upper() + "_RESTORE"])
        source = Path(os.environ[component.upper() + "_ARTIFACTS"])
        if any(root.iterdir()):
            raise ValueError("scratch data exists; preserving it")
        entries, config, proof = artifacts(component, source, identifier, os.environ[component.upper() + "_CHECKSUMS"])
        pending = root / (".pending-" + identifier)
        pending.mkdir(mode=0o700)
        pending.chmod(0o700)
        tree = pending / "tree"
        extract(source, tree, entries)
        result = {"result": "passed", "backup_id": identifier, "claim": CLAIMS[component],
                  "files": sum(entry["type"] == "file" for entry in entries.values()),
                  "directories": sum(entry["type"] == "directory" for entry in entries.values()),
                  "archive_metadata_verified": True, "file_checksums_verified": True,
                  "scratch_ownership": "validator; original numeric ownership retained in archive"}
        if component == "data":
            result.update(sqlite_check(tree, config, proof))
        output = pending / "result.json"
        output.write_text(json.dumps(result, sort_keys=True) + "\n")
        output.chmod(0o600)
        with output.open("rb") as descriptor:
            os.fsync(descriptor.fileno())
        pending.rename(root / "validated")
        results[component], configs[component] = result, config
    if configs["data"] != configs["images"]:
        raise ValueError("recovered configuration differs between the two volume captures")
    print("both archives extracted; file checksums, numeric archive metadata, key continuity, and SQLite row counts verified", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        validate()
    except Exception:
        raise SystemExit("isolated file/SQLite validation failed; private scratch evidence preserved")
