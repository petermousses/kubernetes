#!/usr/bin/env python3
"""Extract seven recovered Paperless archives; validate scratch files and SQLite only."""

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
CLAIMS = {"data": "paperless-data", "ai": "paperless-ai", "gpt": "paperless-gpt",
          "gpt-config": "paperless-gpt-config", "gpt-db": "paperless-gpt-db",
          "postgres": "paperless-postgres", "redis": "paperless-redis"}
PHASE = "starting"

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
    if (metadata.get("backup_id") != identifier or metadata.get("component") != component or
            metadata.get("claim") != CLAIMS[component] or config.get("backup_id") != identifier or
            {key: value.get("claim") for key,value in config.get("sources", {}).items()} != CLAIMS or
            proof.get("result") != "passed" or proof.get("backup_id") != identifier or
            proof.get("component") != component or proof.get("archive_sha256") != checksums["volume.tar"] or
            metadata.get("archive_sha256") != checksums["volume.tar"]):
        raise ValueError("restored claim or common generation differs")
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


def native_structure(component, tree, entries, initial):
    counts = {}
    for name, entry in entries.items():
        if entry["type"] != "file":
            continue
        path = tree / name
        with path.open("rb") as stream:
            header = stream.read(16)
        if header == b"SQLite format 3\x00":
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("restored SQLite integrity failed")
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                counts[name] = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
    if counts != initial.get("sqlite_tables") or component in ("ai", "gpt-db") and not counts:
        raise ValueError("restored SQLite tables or row counts differ from the captured snapshot")
    if component == "gpt-config":
        files = [name for name,entry in entries.items() if entry["type"] == "file"]
        if len(files) != 1 or not isinstance(json.loads((tree / files[0]).read_text()), dict):
            raise ValueError("restored GPT configuration is invalid")
    native = initial.get("native", {})
    if component == "redis" and (native.get("native_integrity") != "passed" or digest(tree / "dump.rdb") != native.get("rdb_sha256")):
        raise ValueError("restored RDB differs from the native integrity proof")
    if component == "postgres":
        with (tree / "database.dump").open("rb") as stream:
            if stream.read(5) != b"PGDMP" or native.get("archive_list_checked") is not True:
                raise ValueError("restored native PostgreSQL archive differs")
    return counts


def validate():
    global PHASE
    identifier = os.environ["BACKUP_ID"]
    if not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise ValueError("invalid restore generation")
    configuration = None
    restores = json.loads(os.environ["RESTORES_JSON"])
    if set(restores) != set(CLAIMS):
        raise ValueError("restore component set differs")
    for component, claim in CLAIMS.items():
        PHASE = "checking recovered artifacts for " + component
        source = Path(restores[component]["artifacts"])
        root = Path(restores[component]["data"])
        if any(root.iterdir()):
            raise ValueError("scratch data exists; preserving it")
        expected_checksums = restores[component]["checksums"]
        entries, config, initial = artifacts(component, source, identifier, expected_checksums)
        if configuration is not None and configuration != config:
            raise ValueError("restored configuration differs between captures")
        configuration = config
        pending = root / (".pending-" + identifier)
        pending.mkdir(mode=0o700)
        pending.chmod(0o700)
        tree = pending / "tree"
        PHASE = "extracting recovered archive for " + component
        extract(source, tree, entries)
        PHASE = "checking restored SQLite/native structure for " + component
        counts = native_structure(component, tree, entries, initial)
        result = {"result": "passed", "backup_id": identifier, "component": component, "claim": claim,
                  "files": sum(entry["type"] == "file" for entry in entries.values()),
                  "directories": sum(entry["type"] == "directory" for entry in entries.values()),
                  "archive_sha256": digest(source / "volume.tar"), "sqlite_tables": counts,
                  "archive_metadata_verified": True, "file_checksums_verified": True,
                  "configuration_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                  "scratch_ownership": "validator; original numeric ownership retained in archive"}
        output = pending / "result.json"
        output.write_text(json.dumps(result, sort_keys=True) + "\n")
        output.chmod(0o600)
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        pending.rename(root / "validated")
    print("all seven restored archives extracted; file checksums, numeric archive metadata, and SQLite row counts verified", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        validate()
    except Exception as error:
        raise SystemExit(json.dumps({"result":"stopped", "phase":PHASE, "error_type":type(error).__name__,
                                     "sqlite_errorname":getattr(error, "sqlite_errorname", None),
                                     "reason":"isolated validation failed; private scratch evidence preserved"}))
