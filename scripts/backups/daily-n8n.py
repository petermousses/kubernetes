#!/usr/bin/env python3
"""Serialize paired n8n captures, writer recovery, uploads, and retention checks."""

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
module_spec = importlib.util.spec_from_file_location("n8n_backup", Path(__file__).with_name("backup-n8n.py"))
native = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(native)
setup = native.setup
NS = setup.NAMESPACE
COMPONENTS = native.COMPONENTS
ACTIVE = setup.RECOVERY / "daily-active.json"
LATEST = setup.RECOVERY / "latest-daily-backup.json"
PROOF = setup.RECOVERY / "retention-proof.json"
COORDINATION_LOCK = setup.COORDINATION_LOCK
INSTALL = Path("/usr/local/lib/k3s-backups/n8n-daily")
SERVICE = "k3s-n8n-backup.service"
UNIT_DIRECTORY = Path("/etc/systemd/system")
saved, save, sync_directory = native.saved, native.save, native.sync_directory
valid_id = native.valid_generation


def history(component):
    return setup.BASE / "backup-staging-history" / setup.PROTECTED[component]["claim"]


def check_service():
    native.check_units()  # The existing boot/stop recovery runtime stays authoritative.
    path = UNIT_DIRECTORY / SERVICE
    attributes = path.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0 or attributes.st_nlink != 1 or
            stat.S_IMODE(attributes.st_mode) & 0o022 or path.read_bytes() != (
                Path(__file__).resolve().parents[2] / "scripts/backups/systemd" / SERVICE).read_bytes()):
        raise setup.SetupError("installed daily n8n unit differs from its reviewed source")
    output = setup.run(["systemctl", "show", SERVICE,
        "--property=FragmentPath,DropInPaths,ExecStart,ExecStopPost,OnFailure,Restart"], capture=True).stdout
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    commands = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStart", ""))
    stops = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStopPost", ""))
    recovery = "/usr/bin/python3 -u " + str(native.INSTALL / "current/scripts/backups/backup-n8n.py") + " --recover-writers-only"
    if (values.get("FragmentPath") != str(path) or values.get("DropInPaths") or
            os.path.lexists(UNIT_DIRECTORY / (SERVICE + ".d")) or
            [value.strip() for value in commands] != ["/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/daily-n8n.py")] or
            [value.strip() for value in stops] != [recovery] or
            values.get("OnFailure", "").split() != [native.RECOVERY_SERVICE] or values.get("Restart") != "no"):
        raise setup.SetupError("loaded daily backup or writer recovery settings differ")


def workers_idle():
    pods = json.loads(setup.kube("get", "pods", "-A", "-l", "backups.mousses.xyz/restic-client=true",
                                "-o", "json", capture=True, sensitive=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in pods):
        raise setup.SetupError("a repository worker survived its process lock; preserving staging")


def wait_for_dependencies():
    deadline, announced = time.monotonic() + 180, False
    while time.monotonic() < deadline:
        try:
            resources = json.loads(setup.kube("get", "deployment/n8n", "deployment/restic-server", "-n", NS,
                "--request-timeout=5s", "-o", "json", capture=True, sensitive=True, timeout=10).stdout)
            items = resources.get("items", [resources])
            if len(items) == 2 and all(item.get("status", {}).get("availableReplicas") == 1 for item in items):
                return
        except (setup.SetupError, subprocess.TimeoutExpired):
            pass
        if not announced:
            print("waiting up to 180 seconds for the API, n8n, and backup endpoint", flush=True)
            announced = True
        time.sleep(5)
    raise setup.SetupError("n8n backup dependencies did not become ready")


def identity_matches(current, anchor):
    return (all(current[component] == anchor[component] for component in COMPONENTS) and
            all(current["application"][key] == anchor["application"][key] for key in ("deployment_uid", "template")))


def preflight():
    workers_idle()
    initial = saved(native.STATE).get("backup_id")
    verified = saved(setup.RECOVERY / "initial-backup-verification.json")
    restored = saved(setup.RECOVERY / "initial-restore-verification.json")
    if (not native.valid_id(initial) or verified.get("result") != "passed" or verified.get("backup_id") != initial or
            set(verified.get("components", {})) != set(COMPONENTS) or restored.get("result") != "passed" or
            restored.get("backup_id") != initial or restored.get("workflows_started") is not False or
            any(restored.get(key) is not True for key in ("archive_metadata_verified", "file_checksums_verified",
                                                       "sqlite_integrity", "encryption_key_preserved"))):
        raise setup.SetupError("both initial backups and the isolated native credential restore must pass first")
    for component in COMPONENTS:
        receipt = verified["components"][component]
        if (receipt.get("result") != "passed" or receipt.get("backup_id") != initial or
                receipt.get("claim") != setup.PROTECTED[component]["claim"] or
                not re.fullmatch(r"[0-9a-f]{64}", receipt.get("snapshot", "")) or
                restored.get("snapshots", {}).get(component) != receipt["snapshot"]):
            raise setup.SetupError("initial restore snapshot identity differs: " + component)
    current = native.preflight()
    if not identity_matches(current, restored["production"]):
        raise setup.SetupError("production storage, controller, or image changed since its isolated restore")
    # The live source spec must still be the approved manifest, excluding run triggers.
    output = setup.kube("create", "--dry-run=client", "-k", str(native.MANIFESTS), "-o", "json", capture=True).stdout.lstrip()
    decoder, resources = json.JSONDecoder(), []
    while output:
        item, end = decoder.raw_decode(output)
        resources.extend(item.get("items", [item])); output = output[end:].lstrip()
    if (len(resources) != len(COMPONENTS) or any(item.get("kind") != "ReplicationSource" or
            item["metadata"].get("namespace") != NS for item in resources)):
        raise setup.SetupError("unexpected n8n daily source bundle")
    templates = {item["metadata"]["name"]: item for item in resources}
    if set(templates) != {component + "-backup" for component in COMPONENTS}:
        raise setup.SetupError("unexpected n8n daily source identities")
    sources = {}
    for component in COMPONENTS:
        source = setup.get_object("replicationsource", component + "-backup", NS)
        expected = {key: value for key, value in templates[component + "-backup"]["spec"].items() if key not in ("paused", "trigger")}
        actual = {key: value for key, value in source.get("spec", {}).items() if key not in ("paused", "trigger")} if source else {}
        if (not source or source["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER or
                set(actual) != set(expected) or not native.contains_fields(actual, expected) or
                actual.get("restic", {}).get("retain") != {"within": "7d"} or
                actual.get("restic", {}).get("pruneIntervalDays") != 1 or
                set(source["spec"].get("trigger", {})) != {"manual"} or not valid_id(source["spec"]["trigger"]["manual"])):
            raise setup.SetupError("VolSync source, retention, resources, placement, or trigger changed: " + component)
        sources[component] = source
    if PROOF.exists() or PROOF.is_symlink():
        proof = saved(PROOF)
        if proof.get("result") != "passed" or proof.get("retention") != "within 7d":
            raise setup.SetupError("saved scratch retention proof is invalid")
    return initial, sources, current


def run_state(initial, sources):
    if ACTIVE.exists() or ACTIVE.is_symlink():
        state = saved(ACTIVE)
        if (not re.fullmatch(r"daily-[0-9a-f]{32}", state.get("backup_id", "")) or
                state.get("initial_backup_id") != initial or not valid_id(state.get("previous_id")) or
                state["previous_id"] == state["backup_id"] or set(state.get("previous_checksums", {})) != set(COMPONENTS) or
                not all(isinstance(value, str) and value for value in state["previous_checksums"].values()) or
                state.get("source_uids") != {component: source["metadata"]["uid"] for component, source in sources.items()}):
            raise setup.SetupError("saved daily generation or uploader identity differs; preserving it")
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
    checksums = {}
    for component in COMPONENTS:
        receipt = receipts[component]
        current = native.inspect_artifacts(component, previous)
        if (not valid_id(previous) or not native.upload_complete(sources[component], previous) or not current or
                receipt.get("result") != "passed" or receipt.get("backup_id") != previous or
                receipt.get("claim") != setup.PROTECTED[component]["claim"] or receipt.get("checksums") != current):
            raise setup.SetupError("previous verified upload or staged generation differs: " + component)
        checksums[component] = current
    state = {"backup_id": "daily-" + secrets.token_hex(16), "initial_backup_id": initial,
             "previous_id": previous, "previous_checksums": checksums, "started_at": native.now(),
             "source_uids": {component: source["metadata"]["uid"] for component, source in sources.items()}}
    save(ACTIVE, state)
    return state


def reserve_source(component, state):
    for attempt in range(5):
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if not source or source["metadata"]["uid"] != state["source_uids"][component]:
            raise setup.SetupError("a VolSync source disappeared or was replaced")
        trigger = source["spec"].get("trigger")
        if trigger == {"manual": state["backup_id"]}:
            break
        if trigger != {"manual": state["previous_id"]} or not native.upload_complete(source, state["previous_id"]):
            raise setup.SetupError("the previous upload is not idle at its verified generation")
        try:
            native.guarded_patch("replicationsource", component + "-backup", source, [
                {"op": "add", "path": "/spec/paused", "value": True},
                {"op": "replace", "path": "/spec/trigger", "value": {"manual": state["backup_id"]}}])
        except setup.SetupError:
            if attempt == 4:
                raise
            time.sleep(0.2)
            continue
        source = setup.get_object("replicationsource", component + "-backup", NS)
        break
    else:
        raise setup.SetupError("could not reserve the uploader")
    if (not source or source["metadata"]["uid"] != state["source_uids"][component] or
            source["spec"].get("trigger") != {"manual": state["backup_id"]}):
        raise setup.SetupError("VolSync source changed outside this daily run")
    if source["spec"].get("paused") is not True:
        if native.inspect_artifacts(component, state["backup_id"]) is None:
            raise setup.SetupError("uploader is unpaused without its immutable generation")
    elif native.claim_pods(component + "-backup-staging"):
        raise setup.SetupError("a Pod still holds staging; wait before rotating captures")


def archive_writer_journal(state):
    journal = saved(native.QUIESCENCE)
    native.check_journal(journal)
    if journal["backup_id"] == state["backup_id"]:
        return
    if journal["backup_id"] != state["previous_id"] or journal["phase"] != "resumed" or not journal["capture_complete"]:
        raise setup.SetupError("previous paired capture or writer recovery is incomplete")
    path = setup.RECOVERY / (state["previous_id"] + "-writer-quiescence.json")
    if path.exists() or path.is_symlink():
        if saved(path) != journal:
            raise setup.SetupError("saved previous writer journal differs; preserving it")
    else:
        save(path, journal)


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


def backup():
    check_service()
    wait_for_dependencies()
    initial, sources, before = preflight()
    state = run_state(initial, sources)
    archive_writer_journal(state)
    for component in COMPONENTS:
        reserve_source(component, state)
        archive_previous(component, state)
    native.capture_pair(state["backup_id"])
    if not identity_matches(setup.live_preflight(check_resources=False), before):
        raise setup.SetupError("production storage or controller changed during the paired daily capture")
    prove_retention = not (PROOF.exists() or PROOF.is_symlink())
    receipts = {}
    for component in COMPONENTS:
        workers_idle()
        native.upload(component, state["backup_id"], evidence_prefix=state["backup_id"])
        receipts[component] = native.verify_backup(component, state["backup_id"], evidence_prefix=state["backup_id"],
                                                   retention_proof=prove_retention and component == "images")
        source = setup.get_object("replicationsource", component + "-backup", NS)
        if not source or source["metadata"]["uid"] != state["source_uids"][component] or not native.upload_complete(source, state["backup_id"]):
            raise setup.SetupError("VolSync completion or uploader identity changed after verification")
    if prove_retention:
        proof = receipts["images"]
        save(PROOF, {"result": "passed", "retention": "within 7d", "verified_at": native.now(),
                     "job": proof["verification_job"], "evidence": proof["retention_proof"]})
    for component in COMPONENTS:
        expire_history(component, state)
    journal = saved(native.QUIESCENCE)
    native.check_journal(journal)
    if journal["backup_id"] != state["backup_id"] or journal["phase"] != "resumed" or not journal["capture_complete"]:
        raise setup.SetupError("daily paired capture or writer recovery evidence is incomplete")
    if not identity_matches(setup.live_preflight(check_resources=False), before):
        raise setup.SetupError("production identity changed before daily completion")
    result = {**state, "result": "passed", "completed_at": native.now(), "components": receipts,
              "retention": "within 7d", "production": before}
    save(setup.RECOVERY / (state["backup_id"] + ".result.json"), result)
    save(LATEST, result)
    ACTIVE.unlink()
    sync_directory(setup.RECOVERY)
    print("daily n8n backup verified: " + state["backup_id"], flush=True)
    print("paired capture, writer recovery, both uploads, seven-day retention, and full artifact checks completed", flush=True)


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
        native.recover_writers()  # Recover n8n before waiting on another namespace.
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
            print("n8n daily worker serialized with board-games, SearXNG, and monitoring", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print("n8n daily backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration,
            native.sqlite3.Error, native.tarfile.TarError, subprocess.TimeoutExpired):
        print("n8n daily backup stopped: filesystem, API, or configuration validation failed; private values omitted", file=sys.stderr)
        sys.exit(1)
