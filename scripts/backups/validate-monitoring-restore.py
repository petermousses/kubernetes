#!/usr/bin/env python3
"""Rootless, offline extraction and loopback-only checks inside restore Jobs."""

import configparser
from datetime import datetime, timezone
from decimal import Decimal
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
import sqlite3
import stat
import sys
import tarfile
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import yaml


ARTIFACTS = {"volume.tar", "contents.json", "configuration.json", "metadata.json", "backup-id", "SHA256SUMS"}
HTTP = build_opener(ProxyHandler({}))


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def digest(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "expected a regular independent file")
        checksum = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file changed while hashing")
        return checksum.hexdigest()


def write(path, value):
    with path.open("x", encoding="utf-8") as output:
        path.chmod(0o600)
        output.write(json.dumps(value, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())


def directory(path):
    path.mkdir(mode=0o700)
    path.chmod(0o700)  # Clear the setgid bit inherited from the PVC root.


def inventory_entries(inventory):
    require(isinstance(inventory, list), "invalid volume inventory")
    entries = {}
    for entry in inventory:
        name = entry.get("path")
        require(isinstance(name, str) and "\x00" not in name and
                (name == "." or (not PurePosixPath(name).is_absolute() and
                                 str(PurePosixPath(name)) == name and ".." not in PurePosixPath(name).parts)) and
                name not in entries and entry.get("type") in ("file", "directory"),
                "unsafe or duplicate inventory path")
        require(all(type(entry.get(key)) is int for key in ("uid", "gid", "mode", "mtime_ns")) and
                0 <= entry["uid"] < 2**32 - 1 and 0 <= entry["gid"] < 2**32 - 1 and
                0 <= entry["mode"] <= 0o7777, "invalid archive metadata")
        if entry["type"] == "file":
            require(type(entry.get("bytes")) is int and entry["bytes"] >= 0 and
                    re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")), "invalid file inventory")
        entries[name] = entry
    require(entries.get(".", {}).get("type") == "directory", "volume archive has no root directory")
    for name in entries:
        require(name == "." or entries.get(str(PurePosixPath(name).parent), {}).get("type") == "directory",
                "volume archive has an undeclared parent")
    return entries


def extract_archive(source, tree, entries):
    directory(tree)
    with tarfile.open(source, "r:") as archive:
        members = archive.getmembers()
        require(len(members) == len(entries) and {item.name for item in members} == set(entries),
                "archive paths differ from the saved inventory")
        for member in members:
            entry = entries[member.name]
            require(member.isdir() == (entry["type"] == "directory") and
                    (member.isdir() or member.isfile()) and not member.sparse and
                    (member.uid, member.gid, stat.S_IMODE(member.mode)) ==
                    (entry["uid"], entry["gid"], entry["mode"]) and
                    Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 10**9 == entry["mtime_ns"] and
                    (not member.isfile() or member.size == entry["bytes"]),
                    "archive type, ownership, mode, size, or timestamp differs")
        for name, entry in sorted(entries.items(), key=lambda pair: len(PurePosixPath(pair[0]).parts)):
            if name != "." and entry["type"] == "directory":
                directory(tree / name)
        copied, last_message = 0, time.monotonic()
        for member in members:
            if not member.isfile():
                continue
            checksum = hashlib.sha256()
            target = tree / member.name
            with archive.extractfile(member) as stream, target.open("xb") as output:
                target.chmod(0o600)
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
                    checksum.update(chunk)
                    copied += len(chunk)
                    if time.monotonic() - last_message >= 30:
                        print("restored file bytes: " + str(copied), flush=True)
                        last_message = time.monotonic()
                output.flush()
                os.fsync(output.fileno())
            entry = entries[member.name]
            require(target.stat().st_size == entry["bytes"] and checksum.hexdigest() == entry["sha256"],
                    "extracted volume file differs")
        for name, entry in sorted(entries.items(), key=lambda pair: len(PurePosixPath(pair[0]).parts), reverse=True):
            os.utime(tree / name, ns=(entry["mtime_ns"], entry["mtime_ns"]))


def configuration(config, component, expected):
    require(config.get("version") == 1 and config.get("component") == component and
            config.get("namespace") == "monitoring", "recovered configuration identity differs")
    workload = config["workload"]
    require(workload["metadata"].get("name") == expected["workload_name"] and
            workload["metadata"].get("namespace") == "monitoring" and
            workload["kind"].lower() == expected["workload_kind"], "recovered controller identity differs")
    require(set(config["dependencies"]) == set(config["dependency_inventory"]) == {"secret", "configmap"},
            "invalid dependency inventory")
    for kind, records in config["dependency_inventory"].items():
        require(set(config["dependencies"][kind]) == {name for name, record in records.items() if record["present"]},
                "dependency presence differs from inventory")
        for name, record in records.items():
            if not record["present"]:
                require(record["optional"] is True and record.get("saved_file") is None,
                        "missing required recovery dependency")
                continue
            resource = config["dependencies"][kind][name]
            require(resource.get("apiVersion") == "v1" and resource["kind"].lower() == kind and
                    resource["metadata"].get("name") == name and
                    resource["metadata"].get("namespace") == "monitoring", "recovered dependency identity differs")
    require(set(config["helmcharts"]) == {"kube-prometheus-stack", "loki", "alloy"},
            "recovered Helm configuration is incomplete")
    for name, chart in config["helmcharts"].items():
        require(chart["kind"] == "HelmChart" and chart["metadata"].get("name") == name and
                chart["metadata"].get("namespace") == "kube-system", "recovered Helm identity differs")
    return workload


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        require(key not in result, "duplicate configuration key")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def loki_config(config):
    original = yaml.load(config["dependencies"]["configmap"]["loki"]["data"]["config.yaml"], Loader=UniqueLoader)
    schema = original["schema_config"]
    require(len(schema.get("configs", [])) == 1, "unsupported Loki schema history")
    schema["configs"][0]["from"] = str(schema["configs"][0]["from"])
    require(schema == {"configs": [{"from": "2024-04-01", "index": {"period": "24h", "prefix": "index_"},
                                   "object_store": "filesystem", "schema": "v13", "store": "tsdb"}]} and
            original.get("auth_enabled") is False and original["common"]["path_prefix"] == "/var/loki" and
            original["common"]["storage"] == {"filesystem": {
                "chunks_directory": "/var/loki/chunks", "rules_directory": "/var/loki/rules"}},
            "Loki storage differs from the approved filesystem schema")
    storage = original.get("storage_config", {})
    require(not storage.get("use_thanos_objstore") and
            set(storage).issubset({"bloom_shipper", "boltdb_shipper", "hedging", "tsdb_shipper", "use_thanos_objstore"}),
            "unsupported Loki storage override")
    shipper = storage.get("tsdb_shipper", {})
    require(set(shipper).issubset({"active_index_directory", "cache_location", "index_gateway_client"}) and
            shipper.get("active_index_directory", "/var/loki/tsdb-shipper-active") == "/var/loki/tsdb-shipper-active" and
            shipper.get("cache_location", "/var/loki/tsdb-shipper-cache") == "/var/loki/tsdb-shipper-cache" and
            shipper.get("index_gateway_client", {}) in ({}, {"server_address": ""}), "unsupported Loki index path")
    ingester = original.get("ingester", {})
    require(set(ingester).issubset({"wal"}) and
            set(ingester.get("wal", {})).issubset({"dir", "enabled", "flush_on_shutdown"}) and
            ingester.get("wal", {}).get("dir", "/var/loki/wal") == "/var/loki/wal" and
            ingester.get("wal", {}).get("enabled", True) is True, "unsupported Loki WAL configuration")
    return {
        "auth_enabled": False, "analytics": {"reporting_enabled": False},
        "server": {"http_listen_address": "127.0.0.1", "http_listen_port": 3100,
                   "grpc_listen_address": "127.0.0.1", "grpc_listen_port": 9095},
        "memberlist": {"bind_addr": ["127.0.0.1"], "join_members": []},
        "common": {"path_prefix": "/var/loki", "replication_factor": 1,
                   "ring": {"instance_addr": "127.0.0.1", "kvstore": {"store": "inmemory"}},
                   "storage": {"filesystem": {"chunks_directory": "/var/loki/chunks",
                                               "rules_directory": "/validation/empty-rules"}}},
        "schema_config": schema,
        "storage_config": {"tsdb_shipper": {"active_index_directory": "/var/loki/tsdb-shipper-active",
                                             "cache_location": "/var/loki/tsdb-shipper-cache"}},
        "ingester": {"wal": {"enabled": True, "dir": "/var/loki/wal", "replay_memory_ceiling": "256MB"}},
        "compactor": {"working_directory": "/validation/compactor", "retention_enabled": False},
        "ruler": {"storage": {"type": "local", "local": {"directory": "/validation/empty-rules"}},
                  "rule_path": "/validation/rules", "enable_api": False},
        "limits_config": {"retention_period": "0s", "max_query_lookback": "0s", "max_query_length": "168h1m",
                          "split_queries_by_interval": "0s", "query_timeout": "120s"},
    }


def sqlite_check(tree, config, workload):
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(config["dependencies"]["configmap"]["kube-prometheus-stack-grafana"]["data"]["grafana.ini"])
    container = next(item for item in workload["spec"]["template"]["spec"]["containers"] if item["name"] == "grafana")
    require(ini.get("database", "type", fallback="sqlite3") == "sqlite3" and not ini.get("database", "url", fallback="") and
            not container.get("envFrom") and not any(item["name"].startswith("GF_DATABASE_") for item in container.get("env", [])) and
            not any("database." in item for item in container.get("args", [])), "Grafana backend differs from local SQLite")
    database = tree / "grafana.db"
    require(database.is_file() and not database.is_symlink(), "restored Grafana database is missing")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "Grafana SQLite integrity check failed")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        require({"dashboard", "user", "data_source"}.issubset(tables), "Grafana database lacks expected tables")
        counts = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in tables}
    return {"sqlite_integrity": "ok", "sqlite_tables": len(counts), "table_counts": counts}


def extract(source, root, identifier, component, expected, checksums):
    require(re.fullmatch(r"initial-[0-9a-f]{32}", identifier), "invalid restore generation")
    require(not any(root.iterdir()), "scratch data already exists; preserving it")
    require({item.name for item in source.iterdir()} == ARTIFACTS and
            (source / "SHA256SUMS").read_text() == checksums, "recovered artifact set or checksum manifest differs")
    names = set()
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        require(match and match[2] in ARTIFACTS - {"SHA256SUMS"} and match[2] not in names, "invalid artifact checksum manifest")
        names.add(match[2])
        require(digest(source / match[2]) == match[1], "recovered artifact checksum failed")
    require(names == ARTIFACTS - {"SHA256SUMS"} and (source / "backup-id").read_text().strip() == identifier,
            "recovered generation differs")
    metadata = json.loads((source / "metadata.json").read_text())
    require(all(metadata.get(key) == value for key, value in {
        "backup_id": identifier, "component": component, "claim": expected["claim"], "image_id": expected["image_id"],
        "mount": expected["mount"], "sub_path": expected["sub_path"],
    }.items()), "recovered volume metadata differs")
    entries = inventory_entries(json.loads((source / "contents.json").read_text()))
    require(sum(item["type"] == "file" for item in entries.values()) == metadata["files"] and
            sum(item.get("bytes", 0) for item in entries.values()) == metadata["file_bytes"], "recovered file accounting differs")
    config = json.loads((source / "configuration.json").read_text())
    workload = configuration(config, component, expected)
    native = None
    if component == "loki":
        native = loki_config(config)
    elif component == "prometheus":
        require(entries.get("prometheus-db/wal", {}).get("type") == "directory", "Prometheus WAL was not captured")
        native = {"global": {"scrape_interval": "1m"}, "scrape_configs": [], "rule_files": []}
    elif component == "alertmanager":
        require(entries.get("alertmanager-db", {}).get("type") == "directory", "Alertmanager state directory was not captured")
        native = {"route": {"receiver": "discard"}, "receivers": [{"name": "discard"}]}
    pending = root / (".pending-" + identifier)
    directory(pending)
    extract_archive(source / "volume.tar", pending / "volume", entries)
    directory(pending / "work")
    directory(pending / "work/empty-rules")
    if native is not None:
        write(pending / "work/native.yaml", native)  # JSON is also valid YAML.
    result = {"result": "passed", "backup_id": identifier, "claim": expected["claim"], "component": component,
              "files": metadata["files"], "file_bytes": metadata["file_bytes"],
              "archive_metadata_verified": True, "file_checksums_verified": True, "application_config_verified": True,
              "scratch_ownership": "validator; original numeric ownership retained in archive"}
    if component == "grafana":
        result.update(sqlite_check(pending / "volume", config, workload))
    write(pending / "extracted.json", result)
    for child in ("volume", "work", "extracted.json"):
        (pending / child).rename(root / child)
    pending.rmdir()
    print("archive metadata, file checksums, and recovery configuration verified: " + component, flush=True)
    return result


def isolation():
    # These application endpoints are confirmed healthy by the host before the Job.
    # Validators have no restic-client label; the namespace deny policy must block them.
    # Do not probe the local-node API endpoint: NetworkPolicy has a node-traffic exception.
    targets = json.loads(os.environ["DENIED_ENDPOINTS"])
    require(isinstance(targets, list) and len(targets) >= 3, "missing application isolation targets")
    for address, port in targets:
        try:
            connection = socket.create_connection((address, port), timeout=3)
        except TimeoutError:
            continue
        except OSError as error:
            if error.errno not in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM):
                raise ValidationError("network isolation probe was inconclusive") from None
        else:
            connection.close()
            raise ValidationError("validator unexpectedly reached a denied cluster endpoint")
    print("validator egress isolation verified", flush=True)


def request(port, path, *, raw=False):
    with HTTP.open("http://127.0.0.1:" + str(port) + path, timeout=130) as response:
        data = response.read(8 * 1024**2 + 1)
    require(len(data) <= 8 * 1024**2, "local validation response is unexpectedly large")
    return data.decode() if raw else json.loads(data)


def ready(port):
    path = "/ready" if port == 3100 else "/-/ready"
    deadline, last_message = time.monotonic() + 1200, 0
    while time.monotonic() < deadline:
        try:
            request(port, path, raw=True)
            return
        except (OSError, HTTPError, URLError):
            if time.monotonic() - last_message >= 30:
                print("waiting for isolated native recovery", flush=True)
                last_message = time.monotonic()
            time.sleep(2)
    raise ValueError("isolated native recovery did not become ready")


def metric_samples(metrics, name):
    samples, seen = [], set()
    decoder = json.JSONDecoder()
    for line in metrics.splitlines():
        if not line.startswith((name + "{", name + " ", name + "\t")):
            continue
        match = re.fullmatch(re.escape(name) + r"(?:\{([^{}]*)\})?[ \t]+(\S+)(?:[ \t]+[0-9]+)?", line)
        require(match is not None, "invalid recovery metric sample")
        labels, rest = {}, (match[1] or "").strip()
        while rest:
            key = re.match(r"([a-zA-Z_][a-zA-Z_0-9]*)=", rest)
            require(key is not None and key[1] not in labels, "invalid or duplicate recovery metric label")
            value, end = decoder.raw_decode(rest[key.end():])
            require(isinstance(value, str), "recovery metric label is not a string")
            labels[key[1]] = value
            rest = rest[key.end() + end:].strip()
            require(not rest or rest.startswith(","), "invalid recovery metric label separator")
            if rest:
                rest = rest[1:].strip()
                require(bool(rest), "trailing recovery metric label separator")
        identity = tuple(sorted(labels.items()))
        require(identity not in seen, "duplicate recovery metric sample")
        seen.add(identity)
        value = float(match[2])
        require(math.isfinite(value) and value >= 0, "invalid recovery counter value")
        samples.append({"labels": labels, "value": value})
    return samples


def recovery_metrics(port, required, optional=()):
    metrics = request(port, "/metrics", raw=True)
    for name in (*required, *optional):
        values = metric_samples(metrics, name)
        require((values or name in optional) and all(sample["value"] == 0 for sample in values),
                "native recovery reported corruption or discarded data")


def loki_recovery_metrics(root=None):
    metrics = request(3100, "/metrics", raw=True)
    corruptions = metric_samples(metrics, "loki_ingester_wal_corruptions_total")
    samples = metric_samples(metrics, "loki_ingester_wal_discarded_samples_total")
    byte_samples = metric_samples(metrics, "loki_ingester_wal_discarded_bytes_total")
    duplicates = metric_samples(metrics, "loki_ingester_wal_duplicate_entries_total")
    require(len(duplicates) == 1 and not duplicates[0]["labels"], "missing Loki duplicate replay accounting")
    for rows, label in ((corruptions, "type"), (samples, "reason"), (byte_samples, "reason")):
        require(all(set(row["labels"]) == {label} and
                    re.fullmatch(r"[a-z][a-z_0-9]{0,63}", row["labels"][label]) and row["value"].is_integer()
                    for row in rows), "unsupported Loki recovery counter labels or values")
    by_reason = {row["labels"]["reason"]: int(row["value"]) for row in samples}
    bytes_by_reason = {row["labels"]["reason"]: int(row["value"]) for row in byte_samples}
    report = {"wal_corruptions": sum(int(row["value"]) for row in corruptions),
              "wal_replay_duplicate_samples": by_reason.get("duplicate", 0),
              "wal_replay_duplicate_bytes": bytes_by_reason.get("duplicate", 0),
              "wal_replay_discarded_samples_by_reason": by_reason,
              "wal_replay_discarded_bytes_by_reason": bytes_by_reason,
              "wal_duplicate_entries": duplicates[0]["value"]}
    if root is not None:
        write(root / "work/loki-recovery-metrics.json", report)
    print("LOKI_RECOVERY_COUNTERS_JSON=" + json.dumps(report, sort_keys=True), flush=True)
    # Loki 3.7.8 skips WAL entries already present in the restored checkpoint.
    # stream.Push increments reason=duplicate; ingesterRecoverer.Push independently
    # increments duplicate_entries_total for the same entries (revision 09e6ce2f).
    require(report["wal_corruptions"] == 0 and
            all(value == 0 for reason, value in by_reason.items() if reason != "duplicate") and
            all(value == 0 for reason, value in bytes_by_reason.items() if reason != "duplicate") and
            report["wal_replay_duplicate_samples"] == report["wal_duplicate_entries"] and
            (report["wal_replay_duplicate_samples"] > 0 or report["wal_replay_duplicate_bytes"] == 0),
            "Loki recovery counters show corruption, nonduplicate loss, or inconsistent duplicate accounting")
    return {"recovery_error_counters": 0,
            "wal_replay_duplicate_samples": report["wal_replay_duplicate_samples"],
            "wal_replay_duplicate_bytes": report["wal_replay_duplicate_bytes"]}


def native_check(root, source, component):
    result = json.loads((root / "extracted.json").read_text())
    if component == "prometheus":
        ready(9090)
        recovery_metrics(9090, ("prometheus_tsdb_wal_corruptions_total", "prometheus_tsdb_mmap_chunk_corruptions_total",
                                "prometheus_tsdb_snapshot_replay_error_total"))
        stats = request(9090, "/api/v1/status/tsdb?limit=1")
        require(stats.get("status") == "success", "Prometheus TSDB statistics failed")
        candidates = []
        head_max = stats["data"]["headStats"]["maxTime"]
        if type(head_max) is int and head_max > 0:
            candidates.append(head_max / 1000)
        for path in (root / "volume/prometheus-db").glob("*/meta.json"):
            block = json.loads(path.read_text())
            if type(block.get("maxTime")) is int:
                candidates.append((block["maxTime"] - 1) / 1000)
        found = None
        for timestamp in sorted(set(candidates), reverse=True)[:20]:
            response = request(9090, "/api/v1/query?" + urlencode({"query": "count(up)", "time": str(timestamp)}))
            require(response.get("status") == "success", "restored Prometheus historical query failed")
            series = response["data"]["result"]
            if series and float(series[0]["value"][1]) > 0:
                found = {"historical_up_series": int(float(series[0]["value"][1])), "query_time": timestamp}
                break
        require(found is not None, "restored Prometheus returned no historical up metrics")
        result.update(found)
        result.update({"native_recovery_verified": True, "recovery_error_counters": 0})
    elif component == "alertmanager":
        ready(9093)
        silences = request(9093, "/api/v2/silences")
        require(isinstance(silences, list), "restored Alertmanager silence query failed")
        result.update({"native_recovery_verified": True, "restored_silences": len(silences)})
    elif component == "loki":
        ready(3100)
        counters = loki_recovery_metrics(root)
        metadata = json.loads((source / "metadata.json").read_text())
        end = int(datetime.fromisoformat(metadata["completed_at"].replace("Z", "+00:00")).timestamp() * 10**9)
        response = request(3100, "/loki/api/v1/query_range?" + urlencode({
            "query": '{cluster="openmediavault"}', "start": str(end - 7 * 86400 * 10**9),
            "end": str(end), "limit": "1", "direction": "backward",
        }))
        require(response.get("status") == "success" and response["data"].get("resultType") == "streams",
                "restored Loki historical query failed")
        values = [value for stream in response["data"]["result"] for value in stream.get("values", [])]
        require(values and all(end - 7 * 86400 * 10**9 <= int(value[0]) <= end for value in values),
                "restored Loki returned no historical cluster logs")
        result.update({"native_recovery_verified": True, "historical_log_entries": len(values), **counters})
    elif component != "grafana":
        raise ValueError("unapproved validation component")
    result["validated_at"] = datetime.now(timezone.utc).isoformat()
    write(root / "result.json", result)
    print("RESTORE_VALIDATED_JSON=" + json.dumps(result, sort_keys=True), flush=True)
    return result


def main():
    os.umask(0o077)
    component = os.environ["COMPONENT"]
    root, source = Path("/restore"), Path(os.environ["ARTIFACT_DIRECTORY"])
    if sys.argv[1] in ("extract", "grafana"):
        isolation()
        extract(source, root, os.environ["BACKUP_ID"], component,
                json.loads(os.environ["EXPECTED_VOLUME"]), os.environ["EXPECTED_SHA256SUMS"])
    if sys.argv[1] in ("native", "grafana"):
        native_check(root, source, component)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Configuration and database exceptions can contain private values.
        try:
            with Path("/restore/failure.txt").open("x") as output:
                output.write(traceback.format_exc())
        except OSError:
            pass
        print("restore validation failed: " + (str(error) if isinstance(error, ValidationError) else type(error).__name__) +
              "; scratch data and private diagnostics are retained", file=sys.stderr)
        sys.exit(1)
