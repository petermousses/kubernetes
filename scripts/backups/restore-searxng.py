#!/usr/bin/env python3
"""Restore both initial SearXNG backups into isolated array-backed scratch PVCs."""

import base64
from collections import Counter
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time


module_spec = importlib.util.spec_from_file_location(
    "searxng_backup", Path(__file__).with_name("backup-searxng.py"),
)
backup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backup)
setup = backup.setup
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "restore"
STATE = setup.RECOVERY / "initial-restore.json"
COMPONENTS = ("valkey", "cache")


def base(component):
    return setup.BASE / "restore-validation" / ("searxng-" + component) / "initial"


def destination_name(component):
    return component + "-restore-initial"


def job_name(component):
    return component + "-restore-validation-initial"


def restore_state():
    backup.preflight()
    identifier = backup.saved(backup.STATE).get("backup_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise setup.SetupError("invalid saved initial generation")
    state = {"backup_id": identifier, "components": {}}
    for component in COMPONENTS:
        result = backup.saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json"))
        saved_source = backup.saved(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"))
        source = setup.get_object("replicationsource", backup.source_name(component), NS)
        checksums = backup.inspect_artifacts(component, identifier)
        if (not source or not checksums or not backup.upload_complete(saved_source, identifier) or
                not backup.upload_complete(source, identifier) or
                source["spec"].get("trigger") != {"manual": identifier} or
                result.get("result") != "passed" or result.get("backup_id") != identifier or
                result.get("claim") != "searxng-" + component or result.get("checksums") != checksums or
                not re.fullmatch(r"[0-9a-f]{64}", result.get("snapshot", ""))):
            raise setup.SetupError("verified initial upload evidence differs: " + component)
        cutoff = saved_source["status"]["lastSyncTime"]
        if datetime.fromisoformat(cutoff.replace("Z", "+00:00")).tzinfo is None:
            raise setup.SetupError("saved upload timestamp has no timezone")
        state["components"][component] = {
            "restore_as_of": cutoff, "snapshot": result["snapshot"], "checksums": checksums,
        }
    if STATE.exists() or STATE.is_symlink():
        if backup.saved(STATE) != state:
            raise setup.SetupError("saved restore generation differs; preserving scratch data")
    else:
        backup.save(STATE, state)
    return state


def resources_for(state):
    rendered = setup.kube("create", "--dry-run=client", "-k", str(MANIFESTS),
                          "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if Counter(resource["kind"] for resource in resources) != {
            "PersistentVolume": 6, "PersistentVolumeClaim": 4,
            "ReplicationDestination": 2, "NetworkPolicy": 1}:
        raise setup.SetupError("unexpected restore resource bundle")
    existing = {}
    for resource in resources:
        kind = resource["kind"]
        name = resource["metadata"]["name"]
        if kind == "ReplicationDestination":
            component = name.removesuffix("-restore-initial")
            resource["spec"]["restic"]["restoreAsOf"] = state["components"][component]["restore_as_of"]
        group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        current = setup.get_object(kind.lower() + ("." + group if group else ""), name,
                                   resource["metadata"].get("namespace"))
        existing[name] = current
        if not current:
            continue
        backup.managed(current)
        expected = json.loads(json.dumps(resource["spec"]))
        if kind == "ReplicationDestination":
            expected.pop("paused")
            expected.pop("trigger")
            for flag in ("cleanupCachePVC", "enableFileDeletion"):
                if current["spec"]["restic"].get(flag, False):
                    raise setup.SetupError("restore destination unexpectedly enables deletion")
                expected["restic"].pop(flag, None)  # The API can omit default-false fields.
            trigger = current["spec"].get("trigger", {})
            if (set(trigger) != {"manual"} or trigger["manual"] not in (
                    "controlled-by-restore-runner", state["backup_id"]) or
                    (trigger["manual"] == "controlled-by-restore-runner" and
                     not current["spec"].get("paused"))):
                raise setup.SetupError("existing restore destination trigger differs")
        if kind == "NetworkPolicy":
            for direction in ("ingress", "egress"):
                if current["spec"].get(direction):
                    raise setup.SetupError("validation policy permits unexpected traffic")
                expected.pop(direction, None)
        if not backup.contains_fields(current["spec"], expected):
            raise setup.SetupError("existing restore resource differs: " + name)
    return resources, existing


def prepare_storage(resources, existing):
    storage_class = setup.get_object("storageclass", "searxng-backup-local")
    if (not storage_class or storage_class.get("provisioner") != "kubernetes.io/no-provisioner" or
            storage_class.get("volumeBindingMode") != "Immediate"):
        raise setup.SetupError("the approved static backup storage class changed")
    if shutil.disk_usage(setup.BASE).free < 20 * 1024**3:
        raise setup.SetupError("less than 20 GiB remains on the array")
    allowed = {base(component) / purpose for component in COMPONENTS for purpose in ("artifacts", "cache", "data")}
    for resource in resources:
        if resource["kind"] != "PersistentVolume":
            continue
        path = Path(resource["spec"]["local"]["path"])
        if path not in allowed:
            raise setup.SetupError("unexpected scratch storage path")
        setup.checked_directory(path)
        if path.exists():
            if any(path.iterdir()) and not existing[resource["metadata"]["name"]]:
                raise setup.SetupError("nonempty scratch data has no managed PV; preserving it")
            attributes = path.stat()
            if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o2770):
                raise setup.SetupError("existing scratch directory ownership or mode differs: " + str(path))
        else:
            path.mkdir(parents=True, mode=0o2770)
            os.chown(path, 999, 999)
            os.chmod(path, 0o2770)
        print("array restore directory verified: " + str(path), flush=True)
    for component in COMPONENTS:
        if not existing[destination_name(component)] and any((base(component) / "artifacts").iterdir()):
            raise setup.SetupError("recovered artifacts have no managed destination; preserving them")
    order = {"PersistentVolume": 0, "PersistentVolumeClaim": 1, "NetworkPolicy": 2, "ReplicationDestination": 3}
    for resource in sorted(resources, key=lambda item: order[item["kind"]]):
        if resource["kind"] == "ReplicationDestination" and existing[resource["metadata"]["name"]]:
            continue
        setup.kube("apply", "-f", "-", stdin=json.dumps(resource))
    for component in COMPONENTS:
        for purpose in ("artifacts", "data"):
            name = component + "-restore-" + purpose
            setup.kube("wait", "-n", NS, "pvc/" + name, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s")
            check_binding(name, "searxng-" + name, base(component) / purpose)


def check_binding(claim_name, volume_name, path):
    claim = setup.get_object("pvc", claim_name, NS)
    volume = setup.get_object("pv", volume_name)
    if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != volume_name or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("local", {}).get("path") != str(path) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("scratch PVC binding differs: " + claim_name)


def restore_artifacts(component, state):
    identifier = state["backup_id"]
    name = destination_name(component)
    artifacts = base(component) / "artifacts"
    destination = setup.get_object("replicationdestination", name, NS)
    if destination["spec"].get("paused"):
        if any(artifacts.iterdir()):
            raise setup.SetupError("paused destination already has data; preserving it")
        backup.worker_budget()
        setup.kube("patch", "replicationdestination", name, "-n", NS, "--type=merge", "-p", json.dumps({
            "metadata": {"resourceVersion": destination["metadata"]["resourceVersion"]},
            "spec": {"paused": False, "trigger": {"manual": identifier}},
        }))
    deadline = time.monotonic() + 630
    while True:
        destination = setup.get_object("replicationdestination", name, NS)
        if (not destination or destination["spec"].get("trigger") != {"manual": identifier} or
                destination["spec"].get("paused")):
            raise setup.SetupError("restore destination changed while restoring")
        if backup.upload_complete(destination, identifier):
            break
        if time.monotonic() >= deadline:
            public = {key: destination.get("status", {}).get(key) for key in (
                "conditions", "lastManualSync", "lastSyncTime", "lastSyncStartTime", "latestMoverStatus")}
            print(json.dumps(public, indent=2), flush=True)
            raise setup.SetupError("VolSync restore is still pending; scratch data preserved")
        time.sleep(2)
    names = {path.name for path in artifacts.iterdir()}
    if names == {"current"}:
        current = artifacts / "current"
    elif names == {"data"}:
        setup.checked_directory(artifacts / "data")
        if {path.name for path in (artifacts / "data").iterdir()} != {"current"}:
            raise setup.SetupError("unexpected recovered artifact layout")
        current = artifacts / "data/current"
    else:
        raise setup.SetupError("unexpected recovered artifact layout")
    checksums = backup.inspect_artifacts(component, identifier, current=current)
    if checksums != state["components"][component]["checksums"]:
        raise setup.SetupError("recovered artifacts differ from the verified initial snapshot")
    check_binding("volsync-dst-" + name + "-cache", "searxng-" + component + "-restore-cache",
                  base(component) / "cache")
    backup.save(setup.RECOVERY / ("initial-" + component + "-volsync-restore-result.json"), destination)
    print("VolSync restored and verified all " + str(len(backup.ARTIFACTS[component])) +
          " artifacts: searxng-" + component, flush=True)
    return current


def validation(component, state, current):
    identifier = state["backup_id"]
    job = backup.read_template(MANIFESTS / (component + "-validate-job.yaml"))
    job["metadata"]["annotations"] = {
        "backups.mousses.xyz/generation": identifier,
        "backups.mousses.xyz/snapshot": state["components"][component]["snapshot"],
    }
    environment = {
        "BACKUP_ID": identifier,
        "ARTIFACT_DIRECTORY": "/artifacts/" + current.relative_to(base(component) / "artifacts").as_posix(),
    }
    if component == "cache":
        environment["EXPECTED_SHA256SUMS"] = state["components"][component]["checksums"]
    else:
        environment["DUMP_SHA256"] = backup.digest_file(current / "dump.rdb")
    job["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": key, "value": value} for key, value in environment.items()]
    name = job_name(component)
    existing = setup.get_object("job", name, NS)
    if existing:
        backup.managed(existing)
        if (existing["metadata"].get("annotations", {}) != job["metadata"]["annotations"] or
                existing["spec"].get("backoffLimit") != 0 or
                existing["spec"].get("activeDeadlineSeconds") != 600 or
                not backup.contains_fields(existing["spec"]["template"]["spec"], job["spec"]["template"]["spec"])):
            raise setup.SetupError("existing validation Job differs; preserving it")
    else:
        if any((base(component) / "data").iterdir()):
            raise setup.SetupError("scratch validation data has no matching Job; preserving it")
        if any(backup.active_claim_pods(component + "-restore-" + purpose) for purpose in ("artifacts", "data")):
            raise setup.SetupError("scratch PVCs are still in use; wait before validating")
        backup.worker_budget()
        setup.kube("create", "-f", "-", stdin=json.dumps(job))
    try:
        setup.wait_for_job(name, timeout=630)
    finally:
        setup.print_check_logs(name)
    logs = setup.kube("logs", "-n", NS, "job/" + name, capture=True, sensitive=True).stdout
    results = [json.loads(line.removeprefix("RESTORE_VALIDATED_JSON=")) for line in logs.splitlines()
               if line.startswith("RESTORE_VALIDATED_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or
            results[0].get("claim") != "searxng-" + component or results[0].get("backup_id") != identifier):
        raise setup.SetupError("completed Job did not report the expected restored generation")
    result = results[0]
    evidence_path = base(component) / "data/validated/result.json"
    setup.checked_directory(evidence_path.parent)
    attributes = evidence_path.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
            (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600) or
            json.loads(evidence_path.read_text()) != result):
        raise setup.SetupError("scratch validation evidence differs from the completed Job")
    if component == "valkey":
        counts = [result.get(key) for key in ("keys_read", "keys_loaded", "keys_expired")]
        if (not all(type(value) is int and value >= 0 for value in counts) or
                counts[1] + counts[2] != counts[0] or result.get("private_socket_check") is not True or
                backup.digest_file(base(component) / "data/validated/dump.rdb") != environment["DUMP_SHA256"]):
            raise setup.SetupError("restored Valkey key accounting or dump checksum differs")
    elif (result.get("archive_metadata_verified") is not True or result.get("application_config_verified") is not True or
          not all(type(result.get(key)) is int and result[key] >= 0 for key in ("files", "directories", "sqlite_databases"))):
        raise setup.SetupError("cache validation evidence is incomplete")
    backup.save(setup.RECOVERY / ("initial-" + component + "-restore-result.json"), {
        **result, "snapshot": state["components"][component]["snapshot"],
        "completed_at": backup.now(), "job": name,
    })
    print("isolated restore verified: searxng-" + component, flush=True)


def restore():
    state = restore_state()
    credentials = backup.saved(setup.RECOVERY / "credentials.json")["repositories"]
    for component in COMPONENTS:
        claim = "searxng-" + component
        setup.ensure_secret("restic-restore-" + claim, setup.credential_fields(claim, credentials[claim]))
    saved_ca = backup.saved(setup.RECOVERY / "restic-root-ca.secret.json")
    ca = base64.b64decode(saved_ca["data"]["tls.crt"], validate=True).decode()
    setup.ensure_secret("restic-restore-ca", {"ca.crt": ca})
    resources, existing = resources_for(state)
    prepare_storage(resources, existing)
    for component in COMPONENTS:
        current = restore_artifacts(component, state)
        validation(component, state, current)
    setup.live_preflight(check_resources=False)
    print("both initial backups and isolated restores verified; daily scheduling and independent credential storage remain pending", flush=True)


def main():
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, backup.interrupted)
    descriptor = os.open(setup.RECOVERY / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise setup.SetupError("another searxng operation holds the namespace lock") from error
        restore()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("restore validation stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
