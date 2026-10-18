#!/usr/bin/env python3
"""Restore both initial n8n backups into isolated array-backed scratch PVCs."""

import argparse
import base64
from contextlib import ExitStack
import hashlib
from collections import Counter
from datetime import datetime
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
import tempfile
import time


sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location(
    "n8n_backup", Path(__file__).with_name("backup-n8n.py"),
)
backup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backup)
setup = backup.setup
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "restore"
STATE = setup.RECOVERY / "initial-restore.json"
COMPONENTS = ("data", "images")


def base(component):
    return setup.BASE / "restore-validation" / setup.PROTECTED[component]["claim"] / "initial"


def destination_name(component):
    return component + "-restore-initial"


JOB = "n8n-restore-validation-initial"
VALIDATORS = Path(__file__).with_name("validators")
CIPHER_SHA256 = "223e50d75f7dea662a371bf30dbf8daa06258b3a4b0b8fdcff2d209e62d29257"
TOOLS_IMAGE = "quay.io/backube/volsync@sha256:0d03a6aad57569eba2c0eaa0848cf4a908d9744b372ff2224bb291a320f36d76"
ROOT = Path(__file__).resolve().parents[2]
INSTALL = Path("/usr/local/lib/k3s-backups/n8n-restore")
UNIT_DIRECTORY = Path("/etc/systemd/system")
SERVICE = "k3s-n8n-restore-initial.service"
FILES = (
    "scripts/backups/prepare-n8n.py", "scripts/backups/backup-n8n.py", "scripts/backups/restore-n8n.py",
    "scripts/backups/validators/n8n-restore.py", "scripts/backups/validators/n8n-credentials.js",
    "scripts/backups/systemd/k3s-n8n-restore-initial.service",
    "apps/n8n/backups/restore/kustomization.yaml", "apps/n8n/backups/restore/storage.yaml",
    "apps/n8n/backups/restore/networkpolicy.yaml", "apps/n8n/backups/restore/replication-destinations.yaml",
    "apps/n8n/backups/restore/validate-job.yaml",
)


def restore_state():
    backup.preflight()
    identifier = backup.saved(backup.STATE).get("backup_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise setup.SetupError("invalid saved initial generation")
    initial = backup.saved(setup.RECOVERY / "initial-backup-verification.json")
    journal = backup.saved(backup.QUIESCENCE)
    backup.check_journal(journal)
    if (initial.get("result") != "passed" or initial.get("backup_id") != identifier or
            set(initial.get("components", {})) != set(COMPONENTS) or journal["backup_id"] != identifier or
            journal["phase"] != "resumed" or not journal["capture_complete"]):
        raise setup.SetupError("the paired initial backups and writer recovery must pass first")
    state = {"backup_id": identifier, "components": {}}
    for component in COMPONENTS:
        result = backup.saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json"))
        saved_source = backup.saved(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"))
        source = setup.get_object("replicationsource", component + "-backup", NS)
        checksums = backup.inspect_artifacts(component, identifier)
        if (not source or not checksums or not backup.upload_complete(saved_source, identifier) or
                not backup.upload_complete(source, identifier) or
                source["spec"].get("trigger") != {"manual": identifier} or
                result.get("result") != "passed" or result.get("backup_id") != identifier or
                result.get("claim") != setup.PROTECTED[component]["claim"] or result.get("checksums") != checksums or
                result != initial["components"][component] or source["metadata"]["uid"] != saved_source["metadata"]["uid"] or
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
        managed(current)
        if kind == "PersistentVolume" and current.get("status", {}).get("phase") in ("Released", "Failed"):
            raise setup.SetupError("existing scratch PV needs binding inspection; preserving it")
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
    storage_class = setup.get_object("storageclass", "n8n-backup-local")
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
            check_binding(name, "n8n-" + name, base(component) / purpose)


def check_binding(claim_name, volume_name, path):
    claim = setup.get_object("pvc", claim_name, NS)
    volume = setup.get_object("pv", volume_name)
    if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != volume_name or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("local", {}).get("path") != str(path) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("scratch PVC binding differs: " + claim_name)
    setup.checked_directory(path)
    identity = {"claim_uid": claim["metadata"]["uid"], "volume_uid": volume["metadata"]["uid"], "path": str(path)}
    record = setup.RECOVERY / "initial-restore-bindings.json"
    bindings = backup.saved(record) if record.exists() or record.is_symlink() else {}
    if claim_name in bindings and bindings[claim_name] != identity:
        raise setup.SetupError("recorded scratch binding identity changed")
    bindings[claim_name] = identity
    backup.save(record, bindings)


def restore_artifacts(component, state):
    identifier = state["backup_id"]
    name = destination_name(component)
    artifacts = base(component) / "artifacts"
    destination = setup.get_object("replicationdestination", name, NS)
    destination_uid = destination["metadata"]["uid"]
    if destination["spec"].get("paused"):
        if any(artifacts.iterdir()):
            raise setup.SetupError("paused destination already has data; preserving it")
        backup.worker_budget()
        backup.guarded_patch("replicationdestination", name, destination, [
            {"op": "replace", "path": "/spec/paused", "value": False},
            {"op": "replace", "path": "/spec/trigger", "value": {"manual": identifier}},
        ])
    deadline = time.monotonic() + 1800
    while True:
        destination = setup.get_object("replicationdestination", name, NS)
        if (not destination or destination["metadata"]["uid"] != destination_uid or
                destination["spec"].get("trigger") != {"manual": identifier} or
                destination["spec"].get("paused")):
            raise setup.SetupError("restore destination changed while restoring")
        if backup.upload_complete(destination, identifier):
            break
        if time.monotonic() >= deadline:
            backup.save(setup.RECOVERY / ("initial-" + component + "-volsync-restore-pending.json"), destination)
            raise setup.SetupError("VolSync restore is still pending; scratch data preserved")
        time.sleep(2)
    idle()
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
    check_binding("volsync-dst-" + name + "-cache", "n8n-" + component + "-restore-cache",
                  base(component) / "cache")
    backup.save(setup.RECOVERY / ("initial-" + component + "-volsync-restore-result.json"), destination)
    print("VolSync restored and verified all " + str(len(backup.ARTIFACTS)) +
          " artifacts: n8n-" + component, flush=True)
    return current



def restore():
    production = setup.live_preflight(check_resources=False)
    state = restore_state()
    credentials = backup.saved(setup.RECOVERY / "credentials.json")["repositories"]
    for component in COMPONENTS:
        claim = setup.PROTECTED[component]["claim"]
        setup.ensure_secret("restic-restore-" + claim, setup.credential_fields(claim, credentials[claim]))
    saved_ca = backup.saved(setup.RECOVERY / "restic-root-ca.secret.json")
    ca = base64.b64decode(saved_ca["data"]["tls.crt"], validate=True).decode()
    setup.ensure_secret("restic-restore-ca", {"ca.crt": ca})
    resources, existing = resources_for(state)
    prepare_storage(resources, existing)
    recovered = {}
    for component in COMPONENTS:
        current = restore_artifacts(component, state)
        recovered[component] = current
    proof = validate_pair(state, recovered)
    if setup.live_preflight(check_resources=False) != production:
        raise setup.SetupError("production Pod, image, or storage changed during isolated validation")
    backup.save(setup.RECOVERY / "initial-restore-verification.json", {**proof, "production": production})
    print("both initial backups and isolated restores verified; daily scheduling and independent credential storage remain pending", flush=True)


def managed(resource):
    if resource["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER:
        raise setup.SetupError("scratch resource belongs to another operation")


def idle():
    deadline = time.monotonic() + 120
    while True:
        pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
        active = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and (
            pod["metadata"].get("labels", {}).get("backups.mousses.xyz/restic-client") == "true" or
            pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "n8n-restore-validation")]
        if not active:
            return
        if time.monotonic() >= deadline:
            raise setup.SetupError("a previous restore worker remains active; preserving it")
        time.sleep(2)


def selector_matches(selector, labels):
    if set(selector) - {"matchLabels", "matchExpressions"}:
        raise setup.SetupError("unsupported network policy selector")
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for item in selector.get("matchExpressions", []):
        key, operation, values = item["key"], item["operator"], item.get("values", [])
        if operation == "In":
            match = key in labels and labels[key] in values
        elif operation == "NotIn":
            match = labels.get(key) not in values
        elif operation == "Exists":
            match = key in labels
        elif operation == "DoesNotExist":
            match = key not in labels
        else:
            raise setup.SetupError("unsupported network policy expression")
        if not match:
            return False
    return True


def check_isolation(labels):
    policies = json.loads(setup.kube("get", "networkpolicies", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    denied = set()
    for policy in policies:
        spec = policy["spec"]
        if not selector_matches(spec.get("podSelector", {}), labels):
            continue
        types = spec.get("policyTypes", ["Ingress"] + (["Egress"] if spec.get("egress") else []))
        for direction in ("Ingress", "Egress"):
            if direction in types:
                if spec.get(direction.lower()):
                    raise setup.SetupError("a network policy permits traffic for the isolated validator")
                denied.add(direction)
    if denied != {"Ingress", "Egress"}:
        raise setup.SetupError("validator must deny ingress and egress")


def validator_config():
    code = {name: (VALIDATORS / name).read_text() for name in ("n8n-restore.py", "n8n-credentials.js")}
    digest = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()
    resource = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "n8n-restore-validator-" + digest[:12], "namespace": NS,
        "labels": {"app.kubernetes.io/managed-by": setup.MANAGER, "app.kubernetes.io/part-of": "n8n"}},
        "immutable": True, "data": code}
    existing = setup.get_object("configmap", resource["metadata"]["name"], NS, sensitive=True)
    if existing:
        managed(existing)
        if existing.get("data") != code or existing.get("immutable") is not True or existing.get("binaryData"):
            raise setup.SetupError("existing validator code differs")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps(resource))
    return resource["metadata"]["name"], digest


def check_labels(actual, expected, uid):
    permitted = {**expected, "controller-uid": uid, "batch.kubernetes.io/controller-uid": uid,
                 "job-name": JOB, "batch.kubernetes.io/job-name": JOB}
    if any(actual.get(key) != value for key, value in expected.items()) or any(
            key not in permitted or value != permitted[key] for key, value in actual.items()):
        raise setup.SetupError("validator labels differ from the approved isolated Job")
    check_isolation(actual)


def check_pod_spec(actual, expected):
    if not backup.contains_fields(actual, expected):
        raise setup.SetupError("validator Pod specification differs")
    defaults = {"dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler", "serviceAccountName": "default",
                "serviceAccount": "default", "terminationGracePeriodSeconds": 30, "priority": 0,
                "preemptionPolicy": "PreemptLowerPriority", "nodeName": "openmediavault"}
    tolerated = [
        {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300},
        {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}]
    for key, value in actual.items():
        if key in expected:
            continue
        if key == "tolerations" and sorted(value, key=lambda item: item.get("key", "")) == tolerated:
            continue
        if key not in defaults or value != defaults[key]:
            raise setup.SetupError("unapproved field in validator Pod specification")
    security = dict(actual.get("securityContext", {}))
    if "supplementalGroupsPolicy" not in expected.get("securityContext", {}) and security.get("supplementalGroupsPolicy") == "Merge":
        security.pop("supplementalGroupsPolicy")
    if security != expected.get("securityContext"):
        raise setup.SetupError("validator Pod security context differs")
    container_defaults = {"imagePullPolicy": "IfNotPresent", "terminationMessagePath": "/dev/termination-log",
                          "terminationMessagePolicy": "File"}
    for group in ("containers", "initContainers"):
        for current, original in zip(actual[group], expected[group]):
            for key, value in current.items():
                if key in original:
                    continue
                if key not in container_defaults or value != container_defaults[key]:
                    raise setup.SetupError("unapproved field in validator container")
            if current.get("securityContext") != original.get("securityContext") or current.get("resources") != original.get("resources"):
                raise setup.SetupError("validator container privileges or reservations differ")
            for key in original:
                if key not in ("securityContext", "resources") and current[key] != original[key]:
                    raise setup.SetupError("validator container arguments, environment, or mounts differ")
    if actual["volumes"] != expected["volumes"] or actual.get("nodeSelector") != expected.get("nodeSelector"):
        raise setup.SetupError("validator volumes or placement differ")


def check_receipt(result, state, recovered):
    initial = json.loads((recovered["data"] / "validation.json").read_text())
    images = json.loads((recovered["images"] / "metadata.json").read_text())
    expected_keys = {"result", "backup_id", "credentials_verified", "workflows", "sqlite_tables", "image_files",
                     "cipher_sha256", "workflows_started", "archive_metadata_verified", "file_checksums_verified",
                     "sqlite_integrity", "encryption_key_preserved"}
    if (not isinstance(result, dict) or set(result) != expected_keys or
            any(type(result[key]) is not int for key in ("credentials_verified", "workflows", "sqlite_tables", "image_files")) or
            result.get("result") != "passed" or result.get("backup_id") != state["backup_id"] or
            result.get("credentials_verified") != initial["relation_counts"]["credentials_entity"] or
            result.get("workflows") != initial["relation_counts"]["workflow_entity"] or
            result.get("sqlite_tables") != len(initial["relation_counts"]) or result.get("image_files") != images["files"] or
            result.get("cipher_sha256") != CIPHER_SHA256 or result.get("workflows_started") is not False or
            any(result.get(key) is not True for key in ("archive_metadata_verified", "file_checksums_verified", "sqlite_integrity", "encryption_key_preserved"))):
        raise setup.SetupError("restored file, database, or native credential evidence differs")


def validate_pair(state, recovered):
    code_name, code_sha = validator_config()
    job = backup.read_template(MANIFESTS / "validate-job.yaml")
    job["metadata"]["annotations"] = {"backups.mousses.xyz/generation": state["backup_id"],
        "backups.mousses.xyz/code-sha256": code_sha, **{
        "backups.mousses.xyz/" + component + "-snapshot": state["components"][component]["snapshot"] for component in COMPONENTS}}
    spec = job["spec"]["template"]["spec"]
    spec["volumes"][0]["configMap"]["name"] = code_name
    environment = {"BACKUP_ID": state["backup_id"], "EXPECTED_IMAGE": setup.IMAGE}
    for component in COMPONENTS:
        environment[component.upper() + "_ARTIFACTS"] = "/artifacts-" + component + "/" + recovered[component].relative_to(base(component) / "artifacts").as_posix()
        environment[component.upper() + "_RESTORE"] = "/restore-" + component
        environment[component.upper() + "_CHECKSUMS"] = state["components"][component]["checksums"]
        for purpose in ("artifacts", "data"):
            check_binding(component + "-restore-" + purpose, "n8n-" + component + "-restore-" + purpose, base(component) / purpose)
    spec["initContainers"][0]["env"] = [{"name": key, "value": value} for key, value in environment.items()]
    spec["containers"][0]["env"].append({"name": "BACKUP_ID", "value": state["backup_id"]})
    labels = job["spec"]["template"]["metadata"]["labels"]
    check_isolation(labels)
    existing = setup.get_object("job", JOB, NS)
    if existing:
        managed(existing)
        if (existing["metadata"].get("annotations") != job["metadata"]["annotations"] or
                existing["spec"].get("backoffLimit") != 0 or existing["spec"].get("activeDeadlineSeconds") != 1200 or
                not backup.contains_fields(existing["spec"]["template"]["spec"], spec)):
            raise setup.SetupError("existing validation Job differs; preserving scratch evidence")
    else:
        idle()
        if any(any((base(component) / "data").iterdir()) for component in COMPONENTS):
            raise setup.SetupError("scratch validation data has no matching Job; preserving it")
        if any(backup.claim_pods(component + "-restore-" + purpose) for component in COMPONENTS for purpose in ("data", "artifacts")):
            raise setup.SetupError("a scratch claim still has a holder; preserving it")
        backup.worker_budget()
        setup.kube("create", "-f", "-", stdin=json.dumps(job))
        existing = setup.get_object("job", JOB, NS)
    job_uid = existing["metadata"]["uid"]
    check_labels(existing["spec"]["template"]["metadata"].get("labels", {}), labels, job_uid)
    check_pod_spec(existing["spec"]["template"]["spec"], spec)
    saved_job = setup.RECOVERY / "initial-restore-validation-job.json"
    if (saved_job.exists() or saved_job.is_symlink()) and backup.saved(saved_job)["metadata"]["uid"] != job_uid:
        raise setup.SetupError("recorded validation Job identity changed")
    backup.save(saved_job, existing)
    try:
        setup.wait_for_job(JOB, timeout=1260)
    finally:
        for container in ("extract", "credentials"):
            logs = setup.kube("logs", "-n", NS, "job/" + JOB, "-c", container,
                              capture=True, sensitive=True, check=False, timeout=20)
            if logs.returncode == 0:
                setup.private_write(setup.RECOVERY / ("initial-restore-" + container + ".log"), logs.stdout)
    idle()
    check_isolation(labels)
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "batch.kubernetes.io/job-name=" + JOB,
                                 "-o", "json", capture=True).stdout)["items"]
    if len(pods) != 1 or pods[0].get("status", {}).get("phase") != "Succeeded":
        raise setup.SetupError("expected one completed isolated validation Pod")
    pod = pods[0]
    if not any(owner.get("kind") == "Job" and owner.get("controller") is True and owner.get("uid") == job_uid
               for owner in pod["metadata"].get("ownerReferences", [])):
        raise setup.SetupError("validation Pod owner differs from the recorded Job")
    check_labels(pod["metadata"].get("labels", {}), labels, job_uid)
    check_pod_spec(pod["spec"], spec)
    for group, expected in (("initContainerStatuses", TOOLS_IMAGE), ("containerStatuses", setup.IMAGE)):
        statuses = pods[0]["status"].get(group, [])
        if len(statuses) != 1 or statuses[0].get("imageID") != expected or statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0:
            raise setup.SetupError("completed validator image or exit status differs")
    logs = setup.kube("logs", "-n", NS, "job/" + JOB, "-c", "credentials", capture=True, sensitive=True).stdout
    results = [json.loads(line.removeprefix("RESTORE_VALIDATED_JSON=")) for line in logs.splitlines() if line.startswith("RESTORE_VALIDATED_JSON=")]
    if len(results) != 1:
        raise setup.SetupError("validator did not report exactly one restored pair")
    result = results[0]
    check_receipt(result, state, recovered)
    proof = {**result,
        "completed_at": backup.now(), "validator_sha256": code_sha, "pod_uid": pods[0]["metadata"]["uid"],
        "snapshots": {component: state["components"][component]["snapshot"] for component in COMPONENTS}}
    print("isolated n8n restore verified: both volumes, every table count, and every saved credential", flush=True)
    return proof




def root_directory(path, *, create=False):
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise setup.SetupError("unexpected installation-directory symlink: " + str(current))
        if not current.exists() and create:
            current.mkdir(mode=0o755)
            os.chmod(current, 0o755)
        attributes = current.stat()
        if (not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or
                stat.S_IMODE(attributes.st_mode) & 0o022):
            raise setup.SetupError("installation directory must be root-owned and not writable by others: " + str(current))

def regular_root_file(path):
    attributes = path.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0 or
            stat.S_IMODE(attributes.st_mode) & 0o022 or attributes.st_nlink != 1):
        raise setup.SetupError("installation file must be regular, root-owned, and not writable by others: " + str(path))

def install_runtime(revision, *, files=FILES):
    root_directory(INSTALL, create=True)
    destination = INSTALL / revision
    if destination.exists() or destination.is_symlink():
        root_directory(destination)
        for filename in files:
            target = destination / filename
            root_directory(target.parent)
            regular_root_file(target)
            if target.read_bytes() != (ROOT / filename).read_bytes():
                raise setup.SetupError("an installed revision differs from its downloaded files: " + revision)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=INSTALL))
        try:
            for filename in files:
                target = temporary / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / filename).read_bytes())
                os.chmod(target, 0o644)
                with target.open("rb") as content:
                    os.fsync(content.fileno())
            for path in sorted((path for path in temporary.rglob("*") if path.is_dir()),
                               key=lambda path: len(path.parts), reverse=True):
                os.chmod(path, 0o755)
                backup.sync_directory(path)
            os.chmod(temporary, 0o755)
            backup.sync_directory(temporary)
            backup.rename_no_replace(temporary, destination)
            backup.sync_directory(INSTALL)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    current = INSTALL / "current"
    if current.exists() or current.is_symlink():
        if (not current.is_symlink() or current.lstat().st_uid != 0 or
                not re.fullmatch(r"[0-9a-f]{40}", os.readlink(current))):
            raise setup.SetupError("refusing to replace an unmanaged runtime pointer")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=INSTALL)
    os.close(descriptor)
    os.unlink(temporary_name)
    try:
        os.symlink(revision, temporary_name)
        os.replace(temporary_name, current)
        backup.sync_directory(INSTALL)
    finally:
        if os.path.lexists(temporary_name):
            os.unlink(temporary_name)


def service_idle():
    state = setup.run(["systemctl", "show", SERVICE, "--property=ActiveState", "--value"], capture=True, check=False).stdout.strip()
    if state not in ("", "inactive", "failed"):
        raise setup.SetupError("the isolated restore service is already active")


def check_unit(*, loaded=False):
    root_directory(UNIT_DIRECTORY)
    path = UNIT_DIRECTORY / SERVICE
    source = ROOT / "scripts/backups/systemd" / SERVICE
    if not source.read_text().startswith("# managed by nas-backup-migration\n"):
        raise setup.SetupError("downloaded restore unit lacks its management marker")
    if path.exists() or path.is_symlink():
        regular_root_file(path)
        if not path.read_text().startswith("# managed by nas-backup-migration\n"):
            raise setup.SetupError("refusing to replace an unmanaged restore unit")
    output = setup.run(["systemctl", "show", SERVICE,
        "--property=FragmentPath,DropInPaths,ExecStart,ExecStopPost,OnFailure,Restart"], capture=True, check=False).stdout
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if (values.get("FragmentPath", "") not in ("", str(path)) or values.get("DropInPaths") or
            os.path.lexists(UNIT_DIRECTORY / (SERVICE + ".d"))):
        raise setup.SetupError("an unmanaged restore unit or drop-in overrides this operation")
    if loaded:
        commands = re.findall(r"\bargv\[\]=([^;{}]+)", values.get("ExecStart", ""))
        expected = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/restore-n8n.py") + " --run"
        if (path.read_bytes() != source.read_bytes() or values.get("FragmentPath") != str(path) or
                [command.strip() for command in commands] != [expected] or values.get("ExecStopPost") or
                values.get("OnFailure") or values.get("Restart") != "no"):
            raise setup.SetupError("loaded restore service differs from its reviewed source")


def install(revision):
    with setup.operation_lock(setup.RECOVERY / ".restore-install.lock", create=True):
        service_idle()
        check_unit()
        with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
            service_idle()
            backup.preflight()
            install_runtime(revision)
            descriptor, temporary = tempfile.mkstemp(prefix=".n8n-restore-unit-", dir=UNIT_DIRECTORY)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write((ROOT / "scripts/backups/systemd" / SERVICE).read_bytes())
                    os.fchmod(output.fileno(), 0o644)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, UNIT_DIRECTORY / SERVICE)
                backup.sync_directory(UNIT_DIRECTORY)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            setup.run(["systemd-analyze", "verify", str(UNIT_DIRECTORY / SERVICE)])
            setup.run(["systemctl", "daemon-reload"])
            check_unit(loaded=True)
        print("running isolated restores and native credential validation under systemd; production n8n keeps running", flush=True)
        try:
            setup.run(["systemctl", "start", SERVICE], timeout=7500)
        finally:
            setup.run(["journalctl", "-u", SERVICE, "-n", "200", "--no-pager", "-o", "cat"], check=False)
        proof = backup.saved(setup.RECOVERY / "initial-restore-verification.json")
        if proof.get("result") != "passed" or proof.get("backup_id") != backup.saved(STATE)["backup_id"]:
            raise setup.SetupError("completed restore evidence is missing")
        print(json.dumps(proof, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--revision")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    if arguments.install:
        if not arguments.revision or not re.fullmatch(r"[0-9a-f]{40}", arguments.revision):
            raise setup.SetupError("installation requires the immutable downloaded revision")
        install(arguments.revision)
    else:
        check_unit(loaded=True)
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(number, backup.interrupted)
        with ExitStack() as locks:
            locks.enter_context(setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True))
            locks.enter_context(setup.operation_lock(setup.COORDINATION_LOCK, create=False))
            print("n8n restore worker serialized with the existing namespace backup workers", flush=True)
            restore()


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print("n8n restore stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.TimeoutExpired):
        print("n8n restore stopped: filesystem, API, or configuration validation failed; private values omitted", file=sys.stderr)
        sys.exit(1)
