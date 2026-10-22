#!/usr/bin/env python3
"""Restore seven Paperless backups in a separate namespace; keep production running."""

import argparse
import base64
from contextlib import ExitStack
from collections import Counter
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location("paperless_backup", Path(__file__).with_name("backup-paperless.py"))
backup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backup)
setup = backup.setup
NS = "paperless-restore-validation"
MANIFESTS = setup.MANIFESTS / "restore"
STATE = setup.RECOVERY / "initial-restore.json"
COMPONENTS = tuple(setup.PROTECTED)
VALIDATORS = Path(__file__).with_name("validators")
ROOT = Path(__file__).resolve().parents[2]
INSTALL = Path("/usr/local/lib/k3s-backups/paperless-restore")
UNIT_DIRECTORY = Path("/etc/systemd/system")
SERVICE = "k3s-paperless-restore-initial.service"
PHASE = "starting"
FILES = (
    "scripts/backups/prepare-paperless.py", "scripts/backups/backup-paperless.py", "scripts/backups/restore-paperless.py",
    "scripts/backups/validators/paperless-restore.py", "scripts/backups/systemd/k3s-paperless-restore-initial.service",
    "apps/paperless-ngx/backups/restore/kustomization.yaml", "apps/paperless-ngx/backups/restore/storage.yaml",
    "apps/paperless-ngx/backups/restore/networkpolicy.yaml", "apps/paperless-ngx/backups/restore/server-networkpolicy.yaml",
    "apps/paperless-ngx/backups/restore/replication-destinations.yaml", "apps/paperless-ngx/backups/restore/validate-files-job.yaml",
    "apps/paperless-ngx/backups/restore/validate-postgres-job.yaml", "apps/paperless-ngx/backups/restore/validate-redis-job.yaml",
)


def base(component):
    return setup.BASE / "restore-validation" / setup.PROTECTED[component]["claim"] / "initial"


def destination_name(component):
    return component + "-restore-initial"


def claim_pods(claim):
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
            any(v.get("persistentVolumeClaim", {}).get("claimName") == claim for v in pod["spec"].get("volumes", []))]


def guarded_patch(kind, name, resource, operations):
    setup.require(resource["metadata"].get("namespace") == NS, "unexpected restore resource namespace")
    tests = [{"op":"test", "path":"/metadata/uid", "value":resource["metadata"]["uid"]},
             {"op":"test", "path":"/metadata/resourceVersion", "value":resource["metadata"]["resourceVersion"]}]
    setup.kube("patch", kind, name, "-n", NS, "--type=json", "-p", json.dumps(tests + operations), capture=True, sensitive=True)


def ensure_secret(name, fields):
    data = {key: base64.b64encode(value.encode()).decode() for key,value in fields.items()}
    existing = setup.get_object("secret", name, NS, sensitive=True)
    if existing:
        managed(existing)
        setup.require(existing.get("data") == data and existing.get("immutable") is True,
                      "isolated restore Secret differs; value omitted: " + name)
    else:
        resource = {"apiVersion":"v1", "kind":"Secret", "metadata":{"name":name,"namespace":NS,
                    "labels":{"app.kubernetes.io/managed-by":setup.MANAGER}}, "immutable":True, "type":"Opaque", "data":data}
        setup.kube("create", "-f", "-", stdin=json.dumps(resource), sensitive=True)
        print("private restore Secret created: " + name, flush=True)


def prepare_namespace():
    current = setup.get_object("namespace", NS)
    if current:
        managed(current)
        setup.require(not current["metadata"].get("deletionTimestamp"), "restore namespace is terminating")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps({"apiVersion":"v1","kind":"Namespace",
                   "metadata":{"name":NS,"labels":{"app.kubernetes.io/managed-by":setup.MANAGER}}}))
    policy = backup.read_template(MANIFESTS / "server-networkpolicy.yaml")
    setup.require(policy["metadata"]["namespace"] == setup.NAMESPACE and
                  policy["metadata"]["name"] == "allow-paperless-restore-repository", "unexpected repository ingress policy")
    existing = setup.get_object("networkpolicy", policy["metadata"]["name"], setup.NAMESPACE)
    if existing:
        managed(existing)
        setup.require(existing["spec"] == policy["spec"], "existing restore ingress policy differs")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps(policy))


def wait_for_job(name, timeout=1260):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = setup.get_object("job", name, NS)
        setup.require(job is not None, "isolated validation Job disappeared: " + name)
        conditions = job.get("status", {}).get("conditions", [])
        if any(item.get("type") == "Complete" and item.get("status") == "True" for item in conditions):
            print("job/" + name + " complete", flush=True)
            return
        failure = [item for item in conditions if item.get("type") in ("FailureTarget", "Failed") and item.get("status") == "True"]
        setup.require(not failure, "isolated validation failed: " + name + "; private logs and scratch data retained")
        time.sleep(2)
    raise setup.SetupError("isolated validation timed out; private logs and scratch data retained: " + name)



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
        raise setup.SetupError("the seven initial backups and writer recovery must pass first")
    existing_state = backup.saved(STATE) if STATE.exists() or STATE.is_symlink() else None
    bootstrap = existing_state.get("bootstrap_user") if existing_state else "restore_check_" + secrets.token_hex(16)
    setup.require(isinstance(bootstrap, str) and re.fullmatch(r"restore_check_[0-9a-f]{32}", bootstrap), "invalid saved bootstrap role")
    state = {"backup_id": identifier, "bootstrap_user": bootstrap, "components": {}}
    for component in COMPONENTS:
        result = backup.saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json"))
        saved_source = backup.saved(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"))
        source = setup.get_object("replicationsource", component + "-backup", setup.NAMESPACE)
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
            "PersistentVolume": 23, "PersistentVolumeClaim": 16,
            "ReplicationDestination": 7, "NetworkPolicy": 2}:
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
                if not expected.get(direction):
                    if current["spec"].get(direction):
                        raise setup.SetupError("restore policy permits unexpected traffic")
                    expected.pop(direction, None)
        if not backup.contains_fields(current["spec"], expected):
            raise setup.SetupError("existing restore resource differs: " + name)
    return resources, existing


def prepare_storage(resources, existing):
    storage_class = setup.get_object("storageclass", "paperless-backup-local")
    if (not storage_class or storage_class.get("provisioner") != "kubernetes.io/no-provisioner" or
            storage_class.get("volumeBindingMode") != "Immediate"):
        raise setup.SetupError("the approved static backup storage class changed")
    if shutil.disk_usage(setup.BASE).free < 100 * 1024**3:
        raise setup.SetupError("less than 100 GiB remains on the array")
    allowed = {base(component) / purpose for component in COMPONENTS for purpose in purposes(component, cache=True)}
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
        for purpose in purposes(component):
            name = component + "-restore-" + purpose
            setup.kube("wait", "-n", NS, "pvc/" + name, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s")
            check_binding(name, "paperless-" + name, base(component) / purpose)


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
    identity = {"claim_uid": claim["metadata"]["uid"], "volume_uid": volume["metadata"]["uid"], "path": str(path), "device": path.stat().st_dev, "inode": path.stat().st_ino}
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
        guarded_patch("replicationdestination", name, destination, [
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
    check_binding("volsync-dst-" + name + "-cache", "paperless-" + component + "-restore-cache",
                  base(component) / "cache")
    backup.save(setup.RECOVERY / ("initial-" + component + "-volsync-restore-result.json"), destination)
    print("VolSync restored and verified all " + str(len(backup.ARTIFACTS)) +
          " artifacts: paperless-" + component, flush=True)
    return current


def managed(resource):
    if resource["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER:
        raise setup.SetupError("scratch resource belongs to another operation")


def idle():
    deadline = time.monotonic() + 120
    while True:
        pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
        active = [p for p in pods if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        if not active:
            return
        setup.require(time.monotonic() < deadline, "a prior isolated restore worker remains active; preserving it")
        time.sleep(2)


def purposes(component, *, cache=False):
    return ("artifacts", "data") + (("cache",) if cache else ()) + (("native",) if component in ("postgres", "redis") else ())



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
    code = {name: (VALIDATORS / name).read_text() for name in ("paperless-restore.py",)}
    digest = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()
    resource = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "paperless-restore-validator-" + digest[:12], "namespace": NS,
        "labels": {"app.kubernetes.io/managed-by": setup.MANAGER, "app.kubernetes.io/part-of": "paperless-ngx"}},
        "immutable": True, "data": code}
    existing = setup.get_object("configmap", resource["metadata"]["name"], NS, sensitive=True)
    if existing:
        managed(existing)
        if existing.get("data") != code or existing.get("immutable") is not True or existing.get("binaryData"):
            raise setup.SetupError("existing validator code differs")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps(resource))
    return resource["metadata"]["name"], digest


def check_labels(actual, expected, uid, name):
    permitted = {**expected, "controller-uid": uid, "batch.kubernetes.io/controller-uid": uid,
                 "job-name": name, "batch.kubernetes.io/job-name": name}
    if any(actual.get(key) != value for key, value in expected.items()) or any(
            key not in permitted or value != permitted[key] for key, value in actual.items()):
        raise setup.SetupError("validator labels differ from the approved isolated Job")
    check_isolation(actual)


def normalized_pod_spec(spec):
    spec = json.loads(json.dumps(spec))
    for group in ("containers", "initContainers"):
        for container in spec.get(group, []):
            for variable in container.get("env", []):
                # EnvVar.Value defaults to "" and is omitted by API JSON.
                # A valueFrom reference is a distinct, unapproved input.
                if set(variable) == {"name"}:
                    variable["value"] = ""
    return spec


def check_pod_spec(actual, expected):
    actual, expected = normalized_pod_spec(actual), normalized_pod_spec(expected)
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
        for current, original in zip(actual.get(group, []), expected.get(group, [])):
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


def run_validator(job, state, code_sha, empty_roots):
    name = job["metadata"]["name"]
    job["metadata"]["annotations"] = {"backups.mousses.xyz/generation": state["backup_id"],
        "backups.mousses.xyz/code-sha256": code_sha,
        "backups.mousses.xyz/snapshots-sha256": backup.fingerprint(state["components"])}
    spec = job["spec"]["template"]["spec"]
    labels = job["spec"]["template"]["metadata"]["labels"]
    check_isolation(labels)
    existing = setup.get_object("job", name, NS)
    if existing:
        managed(existing)
        setup.require(existing["metadata"].get("annotations") == job["metadata"]["annotations"] and
                      existing["spec"].get("backoffLimit") == 0 and
                      existing["spec"].get("activeDeadlineSeconds") == job["spec"]["activeDeadlineSeconds"],
                      "existing validator identity differs; scratch data retained")
    else:
        idle()
        setup.require(not any(any(path.iterdir()) for path in empty_roots),
                      "scratch data has no matching validator Job; preserving it")
        for volume in spec["volumes"]:
            claim = volume.get("persistentVolumeClaim", {}).get("claimName")
            setup.require(not claim or not claim_pods(claim), "a scratch claim still has an active holder")
        backup.worker_budget()
        setup.kube("create", "-f", "-", stdin=json.dumps(job))
        existing = setup.get_object("job", name, NS)
    uid = existing["metadata"]["uid"]
    check_labels(existing["spec"]["template"]["metadata"].get("labels", {}), labels, uid, name)
    check_pod_spec(existing["spec"]["template"]["spec"], spec)
    receipt = setup.RECOVERY / (name + "-job.json")
    if receipt.exists() or receipt.is_symlink():
        setup.require(backup.saved(receipt)["metadata"]["uid"] == uid, "recorded validator Job identity changed")
    backup.save(receipt, existing)
    try:
        wait_for_job(name)
    finally:
        logs = setup.kube("logs", "-n", NS, "job/" + name, "-c", "validate", capture=True, sensitive=True, check=False, timeout=20)
        setup.private_write(setup.RECOVERY / (name + ".log"), logs.stdout + logs.stderr)
    idle()
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "batch.kubernetes.io/job-name=" + name, "-o", "json", capture=True).stdout)["items"]
    setup.require(len(pods) == 1 and pods[0].get("status", {}).get("phase") == "Succeeded", "expected one completed validator Pod")
    pod = pods[0]
    setup.require(any(owner.get("kind") == "Job" and owner.get("controller") is True and owner.get("uid") == uid
                      for owner in pod["metadata"].get("ownerReferences", [])), "validator Pod owner changed")
    check_labels(pod["metadata"].get("labels", {}), labels, uid, name)
    check_pod_spec(pod["spec"], spec)
    statuses = pod.get("status", {}).get("containerStatuses", [])
    setup.require(len(statuses) == 1 and statuses[0].get("restartCount") == 0 and
                  statuses[0].get("imageID", "").endswith("@sha256:" + spec["containers"][0]["image"].rsplit("@sha256:", 1)[1]) and
                  statuses[0].get("state", {}).get("terminated", {}).get("exitCode") == 0, "validator native image or exit status differs")
    return {"job": name, "job_uid": uid, "pod_uid": pod["metadata"]["uid"]}


def scratch_json(path):
    setup.checked_directory(path.parent)
    attrs = path.lstat()
    setup.require(stat.S_ISREG(attrs.st_mode) and attrs.st_nlink == 1 and
                  (attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) == (999, 999, 0o600) and attrs.st_size <= 16 * 1024**2,
                  "expected a private regular validator receipt")
    backup.digest_file(path)
    return json.loads(path.read_text())


def validate_files(state, recovered):
    config_name, code_sha = validator_config()
    job = backup.read_template(MANIFESTS / "validate-files-job.yaml")
    spec = job["spec"]["template"]["spec"]
    spec["volumes"][0]["configMap"]["name"] = config_name
    inputs = {component: {"artifacts": "/artifacts-" + component + "/" + recovered[component].relative_to(base(component) / "artifacts").as_posix(),
                         "data": "/restore-" + component, "checksums": state["components"][component]["checksums"]} for component in COMPONENTS}
    spec["containers"][0]["env"] = [{"name":"BACKUP_ID", "value":state["backup_id"]}, {"name":"RESTORES_JSON", "value":json.dumps(inputs)}]
    receipt = run_validator(job, state, code_sha, [base(component) / "data" for component in COMPONENTS])
    results, configurations = {}, set()
    for component in COMPONENTS:
        result = scratch_json(base(component) / "data/validated/result.json")
        initial = json.loads((recovered[component] / "validation.json").read_text())
        config = json.loads((recovered[component] / "configuration.json").read_text())
        contents = json.loads((recovered[component] / "contents.json").read_text())
        expected_sha = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        setup.require(result.get("result") == "passed" and result.get("backup_id") == state["backup_id"] and
                      result.get("component") == component and result.get("claim") == setup.PROTECTED[component]["claim"] and
                      result.get("archive_sha256") == initial["archive_sha256"] and result.get("sqlite_tables") == initial["sqlite_tables"] and
                      result.get("configuration_sha256") == expected_sha and
                      result.get("files") == sum(entry["type"] == "file" for entry in contents) and
                      result.get("directories") == sum(entry["type"] == "directory" for entry in contents) and
                      result.get("archive_metadata_verified") is True and result.get("file_checksums_verified") is True,
                      "restored file/SQLite/configuration evidence differs: " + component)
        configurations.add(expected_sha)
        backup.save(setup.RECOVERY / ("initial-" + component + "-restore-files.json"), {**result, **receipt})
        results[component] = {"claim":result["claim"], "snapshot":state["components"][component]["snapshot"],
            "files":result["files"], "directories":result["directories"], "sqlite_databases":len(result["sqlite_tables"]),
            "sqlite_tables":sum(len(tables) for tables in result["sqlite_tables"].values()),
            "archive_metadata_verified":True, "file_checksums_verified":True}
    setup.require(len(configurations) == 1, "recovered source configuration differs between archives")
    print("all seven restored file trees and captured SQLite row counts verified", flush=True)
    return results, code_sha


def native_files_unchanged(component, recovered):
    entries = json.loads((recovered[component] / "contents.json").read_text())
    root = base(component) / "data/validated/tree"
    setup.checked_directory(root)
    for entry in entries:
        if entry["type"] == "file":
            setup.require(backup.digest_file(root / entry["path"]) == entry["sha256"], "extracted native input changed: " + component)


def validate_native(state, recovered, code_sha):
    config = json.loads((recovered["postgres"] / "configuration.json").read_text())
    secrets_found = [entry["resource"] for entry in config["dependencies"] if entry["kind"] == "secret" and entry["name"] == "paperless-env"]
    setup.require(len(secrets_found) == 1, "captured PostgreSQL credentials are missing")
    fields = secrets_found[0]["data"]
    values = {key:base64.b64decode(fields[source], validate=True).decode() for key,source in
              {"APP_USER":"POSTGRES_USER", "APP_DATABASE":"POSTGRES_DB", "PGPASSWORD":"POSTGRES_PASSWORD"}.items()}
    setup.require(all(values.values()), "captured PostgreSQL credentials are empty")
    ensure_secret("postgres-restore-validation-auth", values)
    for component in ("postgres", "redis"):
        for purpose in ("data", "native"):
            check_binding(component + "-restore-" + purpose, "paperless-" + component + "-restore-" + purpose, base(component) / purpose)
        native_files_unchanged(component, recovered)
        job = backup.read_template(MANIFESTS / ("validate-" + component + "-job.yaml"))
        container = job["spec"]["template"]["spec"]["containers"][0]
        container.setdefault("env", []).append({"name":"BACKUP_ID", "value":state["backup_id"]})
        if component == "postgres":
            container["env"].append({"name":"BOOTSTRAP_USER", "value":state["bootstrap_user"]})
        receipt = run_validator(job, state, code_sha, [base(component) / "native"])
        result = scratch_json(base(component) / "native/validation-result.json")
        setup.require(result.get("result") == "passed" and result.get("backup_id") == state["backup_id"], "native restore generation differs")
        if component == "postgres":
            setup.require(result.get("database") == values["APP_DATABASE"] and result.get("role") == values["APP_USER"] and
                          int(result.get("server_version_num", 0)) // 10000 == 18 and
                          backup.digest_file(base(component) / "native/validation-logs/row-counts.csv") ==
                          backup.digest_file(base(component) / "data/validated/tree/row-counts.csv"),
                          "restored PostgreSQL credentials, version, or snapshot row counts differ")
        else:
            setup.require(result.get("native_rdb_loaded") is True and result.get("tcp_listener") is False, "Redis native restore proof differs")
        native_files_unchanged(component, recovered)
        backup.save(setup.RECOVERY / ("initial-" + component + "-restore-native.json"), {**result, **receipt})
        print("isolated native restore verified: " + component, flush=True)


def restore():
    global PHASE
    PHASE = "checking initial snapshots and production"
    production = setup.live_preflight(check_resources=False)
    state = restore_state()
    PHASE = "preparing isolated namespace and repository access"
    prepare_namespace()
    credentials = backup.saved(setup.RECOVERY / "credentials.json")["repositories"]
    for component in COMPONENTS:
        claim = setup.PROTECTED[component]["claim"]
        ensure_secret("restic-restore-" + claim, setup.credential_fields(claim, credentials[claim]))
    saved_ca = backup.saved(setup.RECOVERY / "restic-root-ca.secret.json")
    ensure_secret("restic-restore-ca", {"ca.crt":base64.b64decode(saved_ca["data"]["tls.crt"], validate=True).decode()})
    resources, existing = resources_for(state)
    prepare_storage(resources, existing)
    recovered = {}
    for component in COMPONENTS:
        PHASE = "restoring snapshot artifacts for " + component
        recovered[component] = restore_artifacts(component, state)
        for purpose in purposes(component):
            check_binding(component + "-restore-" + purpose, "paperless-" + component + "-restore-" + purpose, base(component) / purpose)
    PHASE = "validating extracted files, metadata, configuration, and SQLite"
    results, code_sha = validate_files(state, recovered)
    PHASE = "restoring PostgreSQL and Redis on private Unix sockets"
    validate_native(state, recovered, code_sha)
    PHASE = "checking production remained unchanged"
    setup.require(setup.live_preflight(check_resources=False) == production, "production Pod, image, or storage changed during isolated restore")
    proof = {"result":"passed", "backup_id":state["backup_id"], "namespace":NS, "completed_at":backup.now(),
             "components":results, "postgres_native_restore":True, "postgres_credentials_verified":True,
             "postgres_row_counts_verified":True, "redis_native_restore":True,
             "validator_network_access":False, "production_unchanged":True, "production":production}
    backup.save(setup.RECOVERY / "initial-restore-verification.json", proof)
    print("all seven isolated restores verified; production unchanged; daily retention/scheduling and off-NAS recovery remain pending", flush=True)



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
        expected = "/usr/bin/python3 -u " + str(INSTALL / "current/scripts/backups/restore-paperless.py") + " --run"
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
            descriptor, temporary = tempfile.mkstemp(prefix=".paperless-restore-unit-", dir=UNIT_DIRECTORY)
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
        print("running isolated restores and native PostgreSQL/Redis validation under systemd; production Paperless keeps running", flush=True)
        try:
            setup.run(["systemctl", "start", SERVICE], timeout=33000)
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
            print("Paperless restore worker serialized with the existing namespace backup workers", flush=True)
            restore()


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print(json.dumps({"result":"stopped", "phase":PHASE, "reason":str(error)}), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(json.dumps({"result":"stopped", "phase":PHASE, "error_type":type(error).__name__,
                          "reason":"filesystem, API, archive, or native validation failed; private details omitted"}), file=sys.stderr)
        sys.exit(1)
