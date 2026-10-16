#!/usr/bin/env python3
"""Repair the two identified Prometheus restore locks without discarding backups."""

from contextlib import ExitStack
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time


sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location(
    "monitoring_daily", Path(__file__).with_name("daily-monitoring.py"),
)
daily = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(daily)
native, setup = daily.native, daily.setup
SOURCE_UID = "a2daafd0-c314-4fea-af15-3534eebf37e7"
GENERATION = "daily-7a82a14ce1b9511bdd5c8c89e82d8811"
INITIAL = "initial-6f62df260568e7a5b67cf6e19177a5f1"
RESTORE_NS = "monitoring-restore-validation"
DESTINATION = "prometheus-restore-initial"
JOB_PREFIX = "prometheus-restic-lock-repair-"
LOCKS = {
    "76d80c23": {"hostname": "volsync-dst-prometheus-restore-initial-4vrpd", "pid": 58,
                 "time": "2026-10-15T15:47:54+00:00", "uid": 999, "gid": 999},
    "b475b721": {"hostname": "volsync-dst-prometheus-restore-initial-x29bf", "pid": 55,
                 "time": "2026-10-15T15:45:51+00:00", "uid": 999, "gid": 999},
}

REPAIR_CODE = r'''
import re

restic = ["restic", "--cacert", "/ca/ca.crt", "--no-cache"]
policy = json.loads(os.environ["APPROVED_RESTORE_LOCKS"])

def command(*arguments, timeout=120):
    return subprocess.run(restic + list(arguments), text=True, capture_output=True,
                          check=True, timeout=timeout).stdout

def identifiers(kind):
    # Restic list emits one full ID per line, including when --json is supplied.
    values = command("list", kind).splitlines()
    if (len(values) > 100000 or len(values) != len(set(values)) or
            any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values)):
        raise SystemExit("unexpected repository identifier inventory")
    return sorted(values)

before = identifiers("snapshots")
if os.environ["EXPECTED_INITIAL_SNAPSHOT"] not in before:
    raise SystemExit("the verified initial snapshot is missing; no locks changed")
lock_ids = identifiers("locks")
inventory = [{"id": value, "lock": json.loads(command("cat", "lock", value))} for value in lock_ids]
print("RESTIC_LOCK_INVENTORY_JSON=" + json.dumps(inventory, sort_keys=True), flush=True)
seen = set()
for item in inventory:
    matches = [prefix for prefix in policy if item["id"].startswith(prefix)]
    if len(matches) != 1 or matches[0] in seen:
        raise SystemExit("unknown repository lock; no locks changed")
    prefix = matches[0]
    expected, lock = policy[prefix], item["lock"]
    created = datetime.fromisoformat(lock["time"])
    expected_time = datetime.fromisoformat(expected["time"])
    if (created.tzinfo is None or lock.get("hostname") != expected["hostname"] or
            lock.get("pid") != expected["pid"] or lock.get("uid") != expected["uid"] or lock.get("gid") != expected["gid"] or
            lock.get("exclusive") is not False or abs((created - expected_time).total_seconds()) >= 1 or
            (datetime.now(timezone.utc) - created).total_seconds() < 3600):
        raise SystemExit("restore lock identity or age differs; no locks changed")
    seen.add(prefix)

# Re-read immediately before the default, stale-only unlock. An empty or partial
# approved inventory also permits recovery after an interrupted earlier repair.
if identifiers("locks") != lock_ids:
    raise SystemExit("lock inventory changed; no locks changed")
if lock_ids:
    print(command("unlock"), end="", flush=True)
if identifiers("locks"):
    raise SystemExit("repository still has locks; stopped without forcing removal")
print(command("check", "--read-data", timeout=7200), end="", flush=True)
after = identifiers("snapshots")
if after != before or identifiers("locks"):
    raise SystemExit("snapshot inventory changed or repository check left a lock")
print("RESTIC_LOCK_REPAIR_JSON=" + json.dumps({
    "result": "passed", "backup_id": os.environ["EXPECTED_BACKUP_ID"],
    "removed_locks": lock_ids, "lock_inventory": inventory,
    "snapshots": len(after), "snapshot_ids_sha256": hashlib.sha256(json.dumps(after).encode()).hexdigest(),
    "snapshots_unchanged": True, "repository_read_data_check": True,
}, sort_keys=True), flush=True)
'''


def checked_source():
    source = setup.get_object("replicationsource", "prometheus-backup", setup.NAMESPACE)
    if (not source or source["metadata"].get("uid") != SOURCE_UID or
            source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
            source["spec"].get("trigger") != {"manual": GENERATION}):
        raise setup.SetupError("the observed Prometheus source identity or generation changed")
    return source


def checked_destination():
    original = native.saved(setup.RECOVERY / "initial-prometheus-volsync-restore-result.json")
    current = setup.get_object("replicationdestination", DESTINATION, RESTORE_NS)
    if (not current or current["metadata"].get("uid") != original["metadata"]["uid"] or
            current["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
            {key: value for key, value in current["spec"].items() if key != "paused"} !=
            {key: value for key, value in original["spec"].items() if key != "paused"} or
            current["spec"].get("trigger") != {"manual": INITIAL}):
        raise setup.SetupError("the original completed Prometheus restore destination changed")
    unpaused = {**current, "spec": {**current["spec"], "paused": False}}
    if not native.upload_complete(unpaused, INITIAL):
        raise setup.SetupError("the original restore is not complete and idle")
    return current


def pause(kind, name, namespace, check):
    for attempt in range(5):
        current = check()
        if current["spec"].get("paused") is True:
            return
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
            {"op": "add", "path": "/spec/paused", "value": True},
        ]
        result = setup.kube("patch", kind, name, "-n", namespace, "--type=json", "-p", json.dumps(operations),
                            capture=True, sensitive=True, check=False)
        if not result.returncode:
            return
        if attempt == 4:
            raise setup.SetupError("could not pause the exact repository user: " + name)
        time.sleep(0.2)


def quiescent():
    if checked_source()["spec"].get("paused") is not True or checked_destination()["spec"].get("paused") is not True:
        raise setup.SetupError("repository source and restore destination must stay paused")
    daily.workers_idle()
    for namespace, name, owner in (
            (setup.NAMESPACE, "volsync-src-prometheus-backup", SOURCE_UID),
            (RESTORE_NS, "volsync-dst-" + DESTINATION, checked_destination()["metadata"]["uid"])):
        job = setup.get_object("job", name, namespace)
        if job and (job["spec"].get("parallelism") != 0 or not any(
                item.get("uid") == owner and item.get("controller") is True
                for item in job["metadata"].get("ownerReferences", []))):
            raise setup.SetupError("a repository mover Job is not paused under its expected controller")
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True, sensitive=True).stdout)["items"]
    for pod in pods:
        if pod["metadata"].get("namespace") not in (setup.NAMESPACE, RESTORE_NS):
            continue
        running = any(status.get("state", {}).get("running") for key in ("containerStatuses", "initContainerStatuses")
                      for status in pod.get("status", {}).get(key, []))
        if pod["metadata"]["name"].startswith("volsync-") and (
                pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") or running):
            raise setup.SetupError("a VolSync worker is still active")
    # This also catches container Restic processes and manual host clients.
    processes = setup.run(["ps", "-eo", "pid=,comm="], capture=True, sensitive=True).stdout
    if any(line.split()[-1] == "restic" for line in processes.splitlines() if line.split()):
        raise setup.SetupError("a Restic process is still running; no locks changed")


def job_template(initial_snapshot):
    job = native.read_template(native.MANIFESTS / "verify-job.yaml")
    job["metadata"]["generateName"] = JOB_PREFIX
    for metadata in (job["metadata"], job["spec"]["template"]["metadata"]):
        metadata["labels"]["app.kubernetes.io/name"] = "monitoring-restic-lock-repair"
        metadata["labels"]["app.kubernetes.io/managed-by"] = setup.MANAGER
    container = job["spec"]["template"]["spec"]["containers"][0]
    prologue, separator, _ = container["args"][0].partition('\nrestic = ["restic",')
    if not separator:
        raise setup.SetupError("the verified DNS/TLS/authentication prologue changed")
    container["args"] = [prologue + "\n" + REPAIR_CODE]
    container["envFrom"] = [{"secretRef": {"name": "restic-" + setup.PROTECTED["prometheus"]["claim"]}}]
    service = setup.get_object("service", "restic", setup.NAMESPACE)
    addresses = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    container["env"] = [{"name": key, "value": value} for key, value in {
        "EXPECTED_RESTIC_IPS": ",".join(str(ipaddress.ip_address(value)) for value in addresses),
        "EXPECTED_BACKUP_ID": GENERATION, "EXPECTED_INITIAL_SNAPSHOT": initial_snapshot,
        "APPROVED_RESTORE_LOCKS": json.dumps(LOCKS),
    }.items()]
    job["spec"]["activeDeadlineSeconds"] = 7500
    job["spec"].pop("ttlSecondsAfterFinished", None)
    return job


def recorded_job(expected):
    record_path = setup.RECOVERY / "prometheus-restic-lock-repair-job.json"
    if not record_path.exists() and not record_path.is_symlink():
        return None
    record = native.saved(record_path)
    if not record["name"].startswith(JOB_PREFIX):
        raise setup.SetupError("the recorded repair Job name changed")
    current = setup.get_object("job", record["name"], setup.NAMESPACE)
    if current and (current["metadata"]["uid"] != record["uid"] or
                    not native.contains_fields(current["metadata"].get("labels", {}), expected["metadata"]["labels"]) or
                    not native.contains_fields(current["spec"], expected["spec"])):
        raise setup.SetupError("a previous repair Job changed; preserving it")
    return current


def terminal(job, types=("Complete", "Failed", "FailureTarget")):
    return any(item.get("status") == "True" and item.get("type") in types
               for item in job.get("status", {}).get("conditions", []))


def run_job(expected, *, reattach_only=False):
    record_path = setup.RECOVERY / "prometheus-restic-lock-repair-job.json"
    current = recorded_job(expected)
    if current and terminal(current, ("Failed", "FailureTarget")) and not reattach_only:
        print("previous failed repair Job retained: " + current["metadata"]["name"], flush=True)
        current = None
    if not current:
        if reattach_only:
            raise setup.SetupError("the running repair Job disappeared; no replacement started")
        native.worker_budget()
        current = json.loads(setup.kube("create", "-f", "-", "-o", "json", stdin=json.dumps(expected),
                                        capture=True, sensitive=True).stdout)
        native.save(record_path, {"name": current["metadata"]["name"], "uid": current["metadata"]["uid"]})
    name = current["metadata"]["name"]
    try:
        setup.wait_for_job(name, timeout=7560)
    finally:
        output = setup.kube("logs", "-n", setup.NAMESPACE, "job/" + name, "--pod-running-timeout=5s",
                            capture=True, sensitive=True, check=False, timeout=15)
        native.save(setup.RECOVERY / (name + ".logs.json"), {"job": name, "logs": output.stdout})
        setup.print_check_logs(name)
    results = [json.loads(line.removeprefix("RESTIC_LOCK_REPAIR_JSON=")) for line in output.stdout.splitlines()
               if line.startswith("RESTIC_LOCK_REPAIR_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or results[0].get("backup_id") != GENERATION or
            results[0].get("snapshots_unchanged") is not True or results[0].get("repository_read_data_check") is not True):
        raise setup.SetupError("repository repair evidence is incomplete")
    return {**results[0], "job": name}


def repair():
    for service in ("k3s-monitoring-backup.service", "k3s-monitoring-backup-initial.service"):
        state = setup.run(["systemctl", "show", service, "--property=ActiveState", "--value"], capture=True).stdout.strip()
        if state not in ("inactive", "failed"):
            raise setup.SetupError("a monitoring backup service is active")
    timer = setup.run(["systemctl", "show", "k3s-monitoring-backup.timer",
                       "--property=ActiveState,UnitFileState"], capture=True).stdout
    if set(timer.splitlines()) != {"ActiveState=inactive", "UnitFileState=disabled"}:
        raise setup.SetupError("the monitoring timer must remain disabled")
    state = native.saved(daily.ACTIVE)
    if (state.get("backup_id") != GENERATION or state.get("initial_backup_id") != INITIAL or
            state.get("previous_id") != INITIAL):
        raise setup.SetupError("the preserved daily generation changed")
    native.recover_writers()
    source, destination = checked_source(), checked_destination()
    path = setup.RECOVERY / "prometheus-restic-lock-repair-before.json"
    if not path.exists() and not path.is_symlink():
        native.save(path, {"backup_id": GENERATION, "source": source, "destination": destination, "recorded_at": native.now()})
    pause("replicationsource", "prometheus-backup", setup.NAMESPACE, checked_source)
    pause("replicationdestination", DESTINATION, RESTORE_NS, checked_destination)
    preserved = [daily.ACTIVE, native.STATE, setup.RECOVERY / "initial-backup-verification.json",
                 setup.RECOVERY / "initial-restore-verification.json",
                 setup.RECOVERY / "initial-prometheus-volsync-restore-result.json"]
    hashes = {str(path): hashlib.sha256(setup.private_read(path).encode()).hexdigest() for path in preserved}
    checksums = native.inspect_artifacts("prometheus", GENERATION)
    previous = daily.history("prometheus") / INITIAL
    if not checksums or native.inspect_artifacts("prometheus", INITIAL, current=previous) != state["previous_checksums"]["prometheus"]:
        raise setup.SetupError("current or previous Prometheus capture changed")
    initial_snapshot = native.saved(setup.RECOVERY / "initial-backup-verification.json")["components"]["prometheus"]["snapshot"]
    expected = job_template(initial_snapshot)
    previous_job = recorded_job(expected)
    reattach = previous_job is not None and not terminal(previous_job)
    if not reattach:
        deadline = time.monotonic() + 180
        while True:
            try:
                quiescent()
                break
            except setup.SetupError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        daily.preflight()
    else:
        print("reattaching to the verified running repair Job: " + previous_job["metadata"]["name"], flush=True)
    result = run_job(expected, reattach_only=reattach)
    quiescent()
    daily.preflight()
    if (hashes != {str(path): hashlib.sha256(setup.private_read(path).encode()).hexdigest() for path in preserved} or
            native.inspect_artifacts("prometheus", GENERATION) != checksums):
        raise setup.SetupError("preserved generation, capture, or initial evidence changed")
    native.save(setup.RECOVERY / "prometheus-restic-lock-repair.json", {**result, "verified_at": native.now(),
                "source_uid": SOURCE_UID, "checksums": checksums, "preserved_evidence": hashes})
    print("stale restore locks cleared; full repository check passed; all snapshots and saved capture preserved", flush=True)
    print("Prometheus source remains paused; retry the same daily generation with the updated installer", flush=True)


def main():
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise setup.SetupError("run this script with sudo python3 and no arguments")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    with ExitStack() as locks:
        locks.enter_context(setup.operation_lock(setup.RECOVERY / ".install.lock", create=True))
        locks.enter_context(setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True))
        with os.fdopen(daily.coordination_descriptor(), "r+") as coordination:
            daily.fcntl.flock(coordination, daily.fcntl.LOCK_EX | daily.fcntl.LOCK_NB)
            repair()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("Prometheus repository repair stopped: " + str(error), file=sys.stderr)
        print("keep the uploader paused and relay this output; no forced unlock is permitted", file=sys.stderr)
        sys.exit(1)
