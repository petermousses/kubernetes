#!/usr/bin/env python3
"""Serialize daily monitoring captures, writer recovery, uploads, and verification."""

import argparse
from datetime import datetime, timedelta
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time


sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location(
    "monitoring_backup", Path(__file__).with_name("backup-monitoring.py"),
)
native = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(native)
setup = native.setup
NS = setup.NAMESPACE
COMPONENTS = native.COMPONENTS
ACTIVE = setup.RECOVERY / "daily-active.json"
LATEST = setup.RECOVERY / "latest-daily-backup.json"
PROOF = setup.RECOVERY / "retention-proof.json"
COORDINATION_LOCK = setup.COORDINATION_LOCK
saved = native.saved
save = native.save
sync_directory = native.sync_directory


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"(?:initial|daily)-[0-9a-f]{32}", value)


def history(component):
    return setup.BASE / "backup-staging-history" / setup.PROTECTED[component]["claim"]


def recover_writers():
    native.recover_writers()


def workers_idle():
    pods = json.loads(setup.kube("get", "pods", "-A", "-l", "backups.mousses.xyz/restic-client=true",
                                 "-o", "json", capture=True, sensitive=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in pods):
        raise setup.SetupError("a repository worker survived its process lock; wait for it to finish before another capture")


def wait_for_dependencies():
    deadline, announced = time.monotonic() + 180, False
    while time.monotonic() < deadline:
        try:
            response = setup.kube("get", "pods", "-n", NS, "--request-timeout=5s", "-o", "json",
                                  capture=True, sensitive=True, check=False, timeout=10)
            pods = json.loads(response.stdout)["items"] if not response.returncode else []
            server = setup.get_object("deployment", "restic-server", NS)
            ready = server and server.get("status", {}).get("availableReplicas") == 1
            for component in COMPONENTS:
                holders = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and any(
                    volume.get("persistentVolumeClaim", {}).get("claimName") == setup.PROTECTED[component]["claim"]
                    for volume in pod["spec"].get("volumes", []))]
                ready = ready and len(holders) == 1 and setup.ready(holders[0])
            if ready:
                return
        except (setup.SetupError, subprocess.TimeoutExpired):
            pass
        if not announced:
            print("waiting up to 180 seconds for the API, four monitoring writers, and backup endpoint", flush=True)
            announced = True
        time.sleep(5)
    raise setup.SetupError("monitoring backup dependencies did not become ready")


def preflight():
    workers_idle()
    initial = saved(native.STATE).get("backup_id")
    verified = saved(setup.RECOVERY / "initial-backup-verification.json")
    restored = saved(setup.RECOVERY / "initial-restore-verification.json")
    if (not native.valid_id(initial) or any(item.get("result") != "passed" or item.get("backup_id") != initial or
            set(item.get("components", {})) != set(COMPONENTS) for item in (verified, restored)) or
            restored.get("namespace") != "monitoring-restore-validation"):
        raise setup.SetupError("all four initial backups and isolated restores must be verified")
    anchors = {}
    for component in COMPONENTS:
        receipt, restore = verified["components"][component], restored["components"][component]
        claim = setup.PROTECTED[component]["claim"]
        if (any(item.get("result") != "passed" or item.get("backup_id") != initial or item.get("claim") != claim
                for item in (receipt, restore)) or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("snapshot", "")) or
                restore.get("snapshot") != receipt["snapshot"] or
                not all(restore.get(key) is True for key in ("archive_metadata_verified", "file_checksums_verified", "application_config_verified")) or
                (component != "grafana" and restore.get("native_recovery_verified") is not True) or
                (component in ("prometheus", "loki") and restore.get("recovery_error_counters") != 0) or
                (component == "grafana" and (restore.get("sqlite_integrity") != "ok" or not restore.get("table_counts"))) or
                (component == "loki" and (restore.get("loopback_routes_verified") is not True or
                                          type(restore.get("historical_log_entries")) is not int or restore["historical_log_entries"] < 1))):
            raise setup.SetupError("initial isolated restore evidence is incomplete: " + component)
        journal = saved(native.journal_path(component))
        native.check_journal(journal, component)
        if journal["phase"] != "resumed":
            raise setup.SetupError("recover the monitoring writer before daily preflight: " + component)
        anchors[component] = {key: journal[key] for key in ("claim_uid", "volume_uid", "workload_uid")}
    before = native.preflight()
    if any(before[component][key] != value for component, identity in anchors.items() for key, value in identity.items()):
        raise setup.SetupError("production storage or controller identity changed since its verified capture")
    rendered = setup.kube("create", "--dry-run=client", "-k", str(native.MANIFESTS), "-o", "json", capture=True).stdout.lstrip()
    decoder, resources = json.JSONDecoder(), []
    while rendered:
        item, end = decoder.raw_decode(rendered)
        resources.extend(item.get("items", [item]))
        rendered = rendered[end:].lstrip()
    if (len(resources) != len(COMPONENTS) or any(item.get("kind") != "ReplicationSource" or
            item["metadata"].get("namespace") != NS for item in resources)):
        raise setup.SetupError("unexpected daily source manifest bundle")
    sources = {item["metadata"]["name"]: item for item in resources}
    if set(sources) != {component + "-backup" for component in COMPONENTS}:
        raise setup.SetupError("unexpected daily source identities")
    current = {}
    for component in COMPONENTS:
        name = component + "-backup"
        source = setup.get_object("replicationsource", name, NS)
        expected = {key: value for key, value in sources[name]["spec"].items() if key not in ("paused", "trigger")}
        actual = {key: value for key, value in source.get("spec", {}).items() if key not in ("paused", "trigger")} if source else {}
        if (not source or source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                set(actual) != set(expected) or not native.contains_fields(actual, expected) or
                actual.get("restic", {}).get("retain") != {"within": "7d"} or
                actual.get("restic", {}).get("pruneIntervalDays") != 1 or
                set(source["spec"].get("trigger", {})) != {"manual"} or
                not valid_id(source["spec"]["trigger"]["manual"])):
            raise setup.SetupError("VolSync repository, retention, resources, placement, or trigger changed: " + component)
        current[component] = source
    if PROOF.exists() or PROOF.is_symlink():
        proof = saved(PROOF)
        if proof.get("result") != "passed" or proof.get("retention") != "within 7d":
            raise setup.SetupError("saved scratch retention proof is invalid")
    return initial, current, anchors


def run_state(initial, sources):
    if ACTIVE.exists() or ACTIVE.is_symlink():
        state = saved(ACTIVE)
        if (not re.fullmatch(r"daily-[0-9a-f]{32}", state.get("backup_id", "")) or
                state.get("initial_backup_id") != initial or not valid_id(state.get("previous_id")) or
                state["previous_id"] == state["backup_id"] or
                set(state.get("previous_checksums", {})) != set(COMPONENTS) or
                not all(isinstance(value, str) and value for value in state["previous_checksums"].values())):
            raise setup.SetupError("saved daily run state is invalid; preserving it")
        return state
    if LATEST.exists() or LATEST.is_symlink():
        latest = saved(LATEST)
        if (latest.get("result") != "passed" or latest.get("initial_backup_id") != initial or
                set(latest.get("components", {})) != set(COMPONENTS)):
            raise setup.SetupError("the latest daily backup is not fully verified")
        previous, receipts = latest["backup_id"], latest["components"]
    else:
        previous = initial
        receipts = {component: saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json")) for component in COMPONENTS}
    previous_checksums = {}
    for component in COMPONENTS:
        receipt = receipts[component]
        checksums = native.inspect_artifacts(component, previous)
        if (not valid_id(previous) or not native.upload_complete(sources[component], previous) or
                receipt.get("result") != "passed" or receipt.get("backup_id") != previous or
                receipt.get("claim") != setup.PROTECTED[component]["claim"] or not checksums or
                receipt.get("checksums") != checksums):
            raise setup.SetupError("the previous verified upload or staged generation differs: " + component)
        previous_checksums[component] = checksums
    state = {"backup_id": "daily-" + secrets.token_hex(16), "initial_backup_id": initial,
             "previous_id": previous, "previous_checksums": previous_checksums, "started_at": native.now()}
    save(ACTIVE, state)
    return state


def reserve_source(component, state):
    for attempt in range(5):
        source = setup.get_object("replicationsource", (component + "-backup"), NS)
        if not source:
            raise setup.SetupError("a VolSync source disappeared")
        if source["spec"].get("trigger") != {"manual": state["previous_id"]}:
            break
        if not native.upload_complete(source, state["previous_id"]):
            raise setup.SetupError("the previous upload is not idle; preserving staging")
        result = setup.kube("patch", "replicationsource", (component + "-backup"), "-n", NS,
                            "--type=merge", "-p", json.dumps({
                                "metadata": {"resourceVersion": source["metadata"]["resourceVersion"]},
                                "spec": {"paused": True, "trigger": {"manual": state["backup_id"]}},
                            }), capture=True, check=False)
        if not result.returncode:
            print(result.stdout, end="", flush=True)
            source = setup.get_object("replicationsource", (component + "-backup"), NS)
            break
        if "(Conflict)" not in result.stderr:
            raise setup.SetupError("could not reserve the VolSync source")
        time.sleep(0.2)
    else:
        raise setup.SetupError("VolSync source kept changing; preserving staging")
    if not source or source["spec"].get("trigger") != {"manual": state["backup_id"]}:
        raise setup.SetupError("VolSync trigger changed outside this daily run")
    if not source["spec"].get("paused"):
        if native.inspect_artifacts(component, state["backup_id"]) is None:
            raise setup.SetupError("an upload is unpaused without its immutable published generation")
    elif native.claim_pods(component + "-backup-staging"):
        raise setup.SetupError("a Pod still holds staging; wait before moving its data")


def archive_previous(component, state):
    directory = history(component)
    setup.checked_directory(directory)
    if directory.exists():
        attributes = directory.stat()
        if attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("staging history must be root-owned with mode 0700")
    else:
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
    current = native.staging(component) / "current"
    previous = directory / state["previous_id"]
    if current.exists() or current.is_symlink():
        setup.checked_directory(current)
        identifier = (current / "backup-id").read_text().strip()
        if identifier == state["backup_id"]:
            native.inspect_artifacts(component, identifier)
        else:
            if identifier != state["previous_id"] or previous.exists() or previous.is_symlink():
                raise setup.SetupError("staging history differs from this daily run")
            native.ensure_source_idle(component, state["backup_id"])
            if native.inspect_artifacts(component, identifier) != state["previous_checksums"][component]:
                raise setup.SetupError("previous staged artifacts differ from their verification receipt")
            current.rename(previous)
            sync_directory(native.staging(component))
            sync_directory(directory)
            print("previous verified capture retained outside staging: " + setup.PROTECTED[component]["claim"] + "/" + identifier, flush=True)
    if native.inspect_artifacts(component, state["previous_id"], current=previous) != state["previous_checksums"][component]:
        raise setup.SetupError("the previous capture is missing or changed in staging history")


def expiry_receipt(component, identifier):
    return setup.RECOVERY / ("history-expiry-monitoring-" + component + "-" + identifier + ".json")


def remove_expired_history(component, directory):
    identifier = directory.name.removeprefix(".expired-")
    if directory.parent != history(component) or not directory.name.startswith(".expired-") or not valid_id(identifier):
        raise setup.SetupError("invalid expired generation name")
    receipt = saved(expiry_receipt(component, identifier))
    if receipt.get("backup_id") != identifier or receipt.get("component") != component:
        raise setup.SetupError("expired history has no matching cleanup approval")
    checksums = {}
    for line in receipt["checksums"].splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        if not match or match[2] in checksums:
            raise setup.SetupError("expired history cleanup manifest is invalid")
        checksums[match[2]] = match[1]
    if set(checksums) != native.ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("expired history cleanup manifest is incomplete")
    setup.checked_directory(directory)
    attributes = directory.stat()
    if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o700):
        raise setup.SetupError("expired history ownership or mode differs")
    artifacts = list(directory.iterdir())
    for artifact in artifacts:
        attributes = artifact.lstat()
        if (artifact.name not in native.ARTIFACTS or not stat.S_ISREG(attributes.st_mode) or
                attributes.st_nlink != 1 or
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600)):
            raise setup.SetupError("unexpected expired history file; preserving it")
        if artifact.name == "SHA256SUMS":
            if artifact.read_text() != receipt["checksums"]:
                raise setup.SetupError("expired history checksum manifest differs")
        elif native.digest_file(artifact) != checksums[artifact.name]:
            raise setup.SetupError("expired history file differs from its cleanup approval")
    for artifact in artifacts:
        artifact.unlink()
    sync_directory(directory)
    directory.rmdir()
    sync_directory(history(component))


def expire_history(component, state):
    current = native.staging(component) / "current"
    captured = datetime.fromisoformat(json.loads((current / "metadata.json").read_text())["captured_at"])
    if captured.tzinfo is None:
        raise setup.SetupError("capture metadata has no timezone")
    cutoff = captured - timedelta(days=7)
    for directory in list(history(component).iterdir()):
        if directory.name.startswith(".expired-"):
            remove_expired_history(component, directory)
            continue
        if not valid_id(directory.name):
            raise setup.SetupError("unrecognized staging history; preserving it")
        checksums = native.inspect_artifacts(component, directory.name, current=directory)
        timestamp = datetime.fromisoformat(json.loads((directory / "metadata.json").read_text())["captured_at"])
        if timestamp.tzinfo is None:
            raise setup.SetupError("staging history has no capture timezone")
        if timestamp < cutoff and directory.name != state["previous_id"]:
            expired = directory.with_name(".expired-" + directory.name)
            if expired.exists() or expired.is_symlink():
                raise setup.SetupError("expired history name is already occupied")
            save(expiry_receipt(component, directory.name), {
                "backup_id": directory.name, "component": component,
                "checksums": checksums, "approved_at": native.now(),
            })
            directory.rename(expired)
            sync_directory(history(component))
            remove_expired_history(component, expired)
            print("expired validated staging history: " + setup.PROTECTED[component]["claim"] + "/" + directory.name, flush=True)


def backup():
    wait_for_dependencies()
    initial, sources, before = preflight()
    state = run_state(initial, sources)
    prove_retention = not (PROOF.exists() or PROOF.is_symlink())
    receipts = {}
    for component in COMPONENTS:
        workers_idle()
        reserve_source(component, state)
        archive_previous(component, state)
        native.capture(component, state["backup_id"])
        native.service_health(component)
        after = setup.live_preflight(check_resources=False)
        if any(after[name][key] != value for name, identity in before.items() for key, value in identity.items()):
            raise setup.SetupError("production binding or controller was replaced during the daily capture")
        native.upload(component, state["backup_id"], evidence_prefix=state["backup_id"])
        receipts[component] = native.verify_backup(component, state["backup_id"], evidence_prefix=state["backup_id"],
                                                   retention_proof=prove_retention and component == COMPONENTS[-1])
        if not native.upload_complete(setup.get_object("replicationsource", component + "-backup", NS), state["backup_id"]):
            raise setup.SetupError("VolSync completion changed after snapshot verification")
    if prove_retention:
        proof = receipts[COMPONENTS[-1]]
        save(PROOF, {"result": "passed", "retention": "within 7d", "verified_at": native.now(),
                     "job": proof["verification_job"], "evidence": proof["retention_proof"]})
    for component in COMPONENTS:
        expire_history(component, state)
    setup.live_preflight(check_resources=False)
    for component in COMPONENTS:
        journal = saved(native.journal_path(component))
        native.check_journal(journal, component)
        if journal["backup_id"] != state["backup_id"] or journal["phase"] != "resumed":
            raise setup.SetupError("daily writer recovery evidence is incomplete: " + component)
        native.service_health(component)
    result = {**state, "result": "passed", "completed_at": native.now(), "components": receipts,
              "retention": "within 7d", "identities": before}
    save(setup.RECOVERY / (state["backup_id"] + ".result.json"), result)
    save(LATEST, result)
    ACTIVE.unlink()
    sync_directory(setup.RECOVERY)
    print("daily backup verified: " + state["backup_id"], flush=True)
    print("four stopped-writer captures, recoveries, uploads, retention, and full artifact checks completed in sequence", flush=True)


def coordination_descriptor():
    current = Path("/root")
    for part in COORDINATION_LOCK.parent.relative_to(current).parts:
        current /= part
        attributes = current.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("board-games operation-lock directory is not private and root-owned")
    attributes = COORDINATION_LOCK.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
            attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o600):
        raise setup.SetupError("board-games operation lock is missing or changed")
    descriptor = os.open(COORDINATION_LOCK, os.O_RDWR | os.O_NOFOLLOW)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (attributes.st_dev, attributes.st_ino):
        os.close(descriptor)
        raise setup.SetupError("board-games operation lock changed while opening it")
    return descriptor


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, native.interrupted)
    with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
        recover_writers()  # Recover interrupted monitoring before waiting for another namespace.
        with os.fdopen(coordination_descriptor(), "r+") as coordination:
            deadline, announced = time.monotonic() + 2700, False
            while True:
                try:
                    fcntl.flock(coordination, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise setup.SetupError("shared backup worker lock remained busy for 45 minutes")
                    if not announced:
                        print("waiting for shared backup worker capacity", flush=True)
                        announced = True
                    time.sleep(5)
            print("monitoring daily worker serialized with board-games and SearXNG", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("monitoring daily backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
