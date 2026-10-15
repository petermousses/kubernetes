#!/usr/bin/env python3
"""Restore monitoring's verified initial backups into isolated array-backed scratch PVCs."""

import argparse
import base64
from contextlib import ExitStack
from datetime import datetime
import hashlib
import importlib.util
import ipaddress
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


sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location("monitoring_backup", Path(__file__).with_name("backup-monitoring.py"))
backup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backup)
setup = backup.setup
NS = "monitoring-restore-validation"
STATE = setup.RECOVERY / "initial-restore.json"
COMPONENTS = backup.COMPONENTS
TOOLS_IMAGE = "docker.io/searxng/searxng@sha256:7914267d4a3b91132aa888b889dbe0657bdb9e1af5a13eb6fbab99a94990c235"
PYTHON = "/usr/local/searxng/.venv/bin/python"
LABELS = {"app.kubernetes.io/managed-by": setup.MANAGER, "app.kubernetes.io/part-of": "monitoring-restore",
          "app.kubernetes.io/name": "monitoring-restore-validation"}
PLACEHOLDER = "controlled-by-restore-runner"
LEGACY_VALIDATOR_SHA256 = "bcd65b2cb28eeee5ca094d1ec82e26d52c4ba6a50d6fb1bee59362a8105d6c45"
LOKI_RETRY_STATE = setup.RECOVERY / "initial-loki-duplicates-retry.json"
SECURITY = {"runAsNonRoot": True, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999,
            "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}}
CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                      "capabilities": {"drop": ["ALL"]}}


def base(component):
    return setup.BASE / "restore-validation" / setup.PROTECTED[component]["claim"] / "initial"


def destination_name(component):
    return component + "-restore-initial"


def job_name(component):
    return component + "-restore-validation-initial"


def validation_target(component, *, retry=False):
    if retry and component != "loki":
        raise setup.SetupError("only the reviewed Loki retry has an alternate scratch target")
    suffix = "-duplicates-v1" if retry else ""
    return {"job": job_name(component) + suffix,
            "claim": component + "-restore" + suffix + "-data",
            "volume": "monitoring-" + component + "-restore" + suffix + "-data",
            "path": str(base(component) / ("retry-duplicates-v1/data" if retry else "data"))}


def checked_target(component, target):
    if target != validation_target(component) and (component != "loki" or target != validation_target(component, retry=True)):
        raise setup.SetupError("unexpected validation storage target")
    return target


def resource(kind, name, spec=None, *, namespace=NS, api="v1", **fields):
    result = {"apiVersion": api, "kind": kind, "metadata": {"name": name, "labels": dict(LABELS)}, **fields}
    if namespace:
        result["metadata"]["namespace"] = namespace
    if spec is not None:
        result["spec"] = spec
    return result


def managed(current):
    if any(current["metadata"].get("labels", {}).get(key) != value for key, value in LABELS.items()):
        raise setup.SetupError("scratch resource belongs to another operation: " + current["metadata"]["name"])


def object_for(expected):
    group = expected["apiVersion"].split("/")[0] if "/" in expected["apiVersion"] else ""
    return setup.get_object(expected["kind"].lower() + ("." + group if group else ""),
                            expected["metadata"]["name"], expected["metadata"].get("namespace"), sensitive=True)


def policies():
    return [
        resource("NetworkPolicy", "deny-all", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                                                "ingress": [], "egress": []}, api="networking.k8s.io/v1"),
        resource("NetworkPolicy", "restic-mover-egress", {
            "podSelector": {"matchLabels": {"backups.mousses.xyz/restic-client": "true"}},
            "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
                         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "restic-server"}}}],
                 "ports": [{"protocol": "TCP", "port": 8000}]},
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                         "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                 "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
            ]}, api="networking.k8s.io/v1"),
        resource("NetworkPolicy", "restic-restore-ingress", {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "restic-server"}},
            "policyTypes": ["Ingress"], "ingress": [
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}},
                           "podSelector": {"matchLabels": {"backups.mousses.xyz/restic-client": "true"}}}],
                 "ports": [{"protocol": "TCP", "port": 8000}]},
            ]}, namespace="monitoring", api="networking.k8s.io/v1"),
    ]


def check_policy(current, expected):
    actual, desired = json.loads(json.dumps(current["spec"])), json.loads(json.dumps(expected["spec"]))
    for value in (actual, desired):
        for direction in ("ingress", "egress"):
            if not value.get(direction):
                value.pop(direction, None)
    if actual != desired:
        raise setup.SetupError("scratch isolation policy changed: " + expected["metadata"]["name"])


def prepare_namespace():
    expected = resource("Namespace", NS, namespace=None)
    current = object_for(expected)
    if current:
        managed(current)
        if current.get("status", {}).get("phase") != "Active" or current["metadata"].get("deletionTimestamp"):
            raise setup.SetupError("scratch namespace is not active")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps(expected))
    unrelated = json.loads(setup.kube("get", "deployment,statefulset,daemonset,cronjob,service,resourcequota,limitrange",
                                      "-n", NS, "-o", "json", capture=True).stdout)["items"]
    if unrelated:
        raise setup.SetupError("scratch namespace contains unexpected controllers, services, or resource defaults")
    existing_policies = json.loads(setup.kube("get", "networkpolicy", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    if any(item["metadata"]["name"] not in ("deny-all", "restic-mover-egress") for item in existing_policies):
        raise setup.SetupError("additional scratch policies could bypass isolation")
    expected_policies = policies()
    for policy in expected_policies:
        current = object_for(policy)
        if current:
            managed(current)
            check_policy(current, policy)
    for policy in expected_policies:
        if not object_for(policy):
            setup.kube("create", "-f", "-", stdin=json.dumps(policy))
    print("dedicated scratch namespace and restrictive network policies verified", flush=True)


def check_namespace():
    current = setup.get_object("namespace", NS)
    managed(current)
    existing = json.loads(setup.kube("get", "networkpolicy", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    if {item["metadata"]["name"] for item in existing} != {"deny-all", "restic-mover-egress"}:
        raise setup.SetupError("scratch namespace isolation policy set changed")
    for expected in policies():
        current = object_for(expected)
        if not current:
            raise setup.SetupError("required isolation policy disappeared")
        managed(current)
        check_policy(current, expected)


def restore_state():
    before = backup.preflight()
    initial = backup.saved(setup.RECOVERY / "initial-backup-verification.json")
    identifier = backup.saved(backup.STATE).get("backup_id")
    if not backup.valid_id(identifier) or initial.get("result") != "passed" or initial.get("backup_id") != identifier:
        raise setup.SetupError("all four verified initial backups are required")
    state = {"backup_id": identifier, "namespace": NS, "components": {}}
    for component in COMPONENTS:
        result = backup.saved(setup.RECOVERY / ("initial-" + component + "-backup-result.json"))
        source = backup.saved(setup.RECOVERY / ("initial-" + component + "-volsync-result.json"))
        current_source = setup.get_object("replicationsource", component + "-backup", "monitoring")
        checksums = backup.inspect_artifacts(component, identifier)
        if (not checksums or result != initial["components"].get(component) or result.get("result") != "passed" or
                result.get("backup_id") != identifier or result.get("claim") != setup.PROTECTED[component]["claim"] or
                result.get("checksums") != checksums or not re.fullmatch(r"[0-9a-f]{64}", result.get("snapshot", "")) or
                not backup.upload_complete(source, identifier) or not backup.upload_complete(current_source, identifier)):
            raise setup.SetupError("initial snapshot evidence differs: " + component)
        cutoff = source["status"]["lastSyncTime"]
        if datetime.fromisoformat(cutoff.replace("Z", "+00:00")).tzinfo is None:
            raise setup.SetupError("upload timestamp has no timezone")
        state["components"][component] = {"snapshot": result["snapshot"], "checksums": checksums, "restore_as_of": cutoff}
    if STATE.exists() or STATE.is_symlink():
        if backup.saved(STATE) != state:
            raise setup.SetupError("restore generation changed; preserving existing scratch data")
    else:
        backup.save(STATE, state)
    return state, before


def storage_resources(state):
    result = []
    for component in COMPONENTS:
        for purpose in ("artifacts", "cache", "data"):
            claim = ("volsync-dst-" + destination_name(component) + "-cache" if purpose == "cache" else
                     component + "-restore-" + purpose)
            name = "monitoring-" + component + "-restore-" + purpose
            size = "1Gi" if purpose == "cache" else setup.PROTECTED[component]["size"]
            result.append(resource("PersistentVolume", name, {
                "capacity": {"storage": size}, "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "monitoring-backup-local",
                "local": {"path": str(base(component) / purpose)}, "claimRef": {"namespace": NS, "name": claim},
                "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                    {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}},
            }, namespace=None))
            if purpose != "cache":
                result.append(resource("PersistentVolumeClaim", claim, {
                    "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "storageClassName": "monitoring-backup-local",
                    "volumeName": name, "resources": {"requests": {"storage": size}},
                }))
        result.append(resource("ReplicationDestination", destination_name(component), {
            "paused": True, "trigger": {"manual": PLACEHOLDER}, "restic": {
                "destinationPVC": component + "-restore-artifacts", "copyMethod": "Direct",
                "repository": "restic-restore-" + component, "customCA": {"secretName": "restic-restore-ca", "key": "ca.crt"},
                "restoreAsOf": state["components"][component]["restore_as_of"],
                "cacheCapacity": "1Gi", "cacheAccessModes": ["ReadWriteOnce"], "cacheStorageClassName": "monitoring-backup-local",
                "cleanupCachePVC": False, "enableFileDeletion": False,
                "moverPodLabels": {"backups.mousses.xyz/restic-client": "true", "app.kubernetes.io/part-of": "monitoring-restore",
                                   "app.kubernetes.io/name": "monitoring-restore-mover"},
                "moverResources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                "moverSecurityContext": SECURITY,
                "moverAffinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [{"matchExpressions": [
                        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}}},
            }}, api="volsync.backube/v1alpha1"))
    return result


def check_existing(expected, current, state):
    managed(current)
    kind = expected["kind"]
    spec = json.loads(json.dumps(expected["spec"]))
    if kind == "ReplicationDestination":
        spec.pop("paused")
        spec.pop("trigger")
        trigger = current["spec"].get("trigger")
        if (trigger not in ({"manual": PLACEHOLDER}, {"manual": state["backup_id"]}) or
                (trigger == {"manual": PLACEHOLDER} and not current["spec"].get("paused"))):
            raise setup.SetupError("existing restore trigger differs")
        for flag in ("cleanupCachePVC", "enableFileDeletion"):
            if current["spec"]["restic"].get(flag, False):
                raise setup.SetupError("restore destination unexpectedly enables deletion")
            spec["restic"].pop(flag)
    if not backup.contains_fields(current["spec"], spec) or current["metadata"].get("deletionTimestamp"):
        raise setup.SetupError("existing scratch storage differs: " + expected["metadata"]["name"])
    if kind == "PersistentVolume" and current.get("status", {}).get("phase") in ("Released", "Failed"):
        raise setup.SetupError("scratch PV requires manual binding inspection")


def prepare_storage(state):
    storage_class = setup.get_object("storageclass", "monitoring-backup-local")
    if (not storage_class or storage_class.get("provisioner") != "kubernetes.io/no-provisioner" or
            storage_class.get("volumeBindingMode") != "Immediate"):
        raise setup.SetupError("approved static scratch storage class changed")
    if shutil.disk_usage(setup.BASE).free < 100 * 1024**3:
        raise setup.SetupError("less than 100 GiB remains on the array")
    resources = storage_resources(state)
    existing = {item["metadata"]["name"]: object_for(item) for item in resources}
    for item in resources:
        current = existing[item["metadata"]["name"]]
        if current:
            check_existing(item, current, state)
    for item in resources:
        if item["kind"] != "PersistentVolume":
            continue
        path = Path(item["spec"]["local"]["path"])
        setup.checked_directory(path)
        if path.exists():
            attributes = path.stat()
            if ((any(path.iterdir()) and not existing[item["metadata"]["name"]]) or
                    (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o2770)):
                raise setup.SetupError("scratch directory has unexpected data, ownership, or permissions; preserving it")
        else:
            path.mkdir(parents=True, mode=0o2770)
            os.chown(path, 999, 999)
            os.chmod(path, 0o2770)
        print("array scratch directory verified: " + str(path), flush=True)
    order = {"PersistentVolume": 0, "PersistentVolumeClaim": 1, "ReplicationDestination": 2}
    for item in sorted(resources, key=lambda value: order[value["kind"]]):
        if not existing[item["metadata"]["name"]]:
            if item["kind"] == "ReplicationDestination" and any((base(item["metadata"]["name"].removesuffix("-restore-initial")) /
                                                                   "artifacts").iterdir()):
                raise setup.SetupError("recovered artifacts have no managed destination; preserving them")
            setup.kube("create", "-f", "-", stdin=json.dumps(item))
    for component in COMPONENTS:
        for purpose in ("artifacts", "data"):
            claim = component + "-restore-" + purpose
            setup.kube("wait", "-n", NS, "pvc/" + claim, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s")
            check_binding(component, purpose)


def check_binding(component, purpose):
    name = ("volsync-dst-" + destination_name(component) + "-cache" if purpose == "cache" else component + "-restore-" + purpose)
    claim = setup.get_object("pvc", name, NS)
    volume = setup.get_object("pv", "monitoring-" + component + "-restore-" + purpose)
    if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
            volume.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != volume["metadata"]["name"] or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("local", {}).get("path") != str(base(component) / purpose) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("scratch binding differs: " + name)
    setup.checked_directory(base(component) / purpose)


def ensure_secret(name, fields):
    desired = resource("Secret", name, type="Opaque", data={
        key: base64.b64encode(value.encode()).decode() for key, value in fields.items()})
    current = object_for(desired)
    if current:
        managed(current)
        if current.get("data") != desired["data"] or current.get("type") != "Opaque":
            raise setup.SetupError("scratch Secret differs from private recovery credentials")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps(desired), sensitive=True)
    print("secret/" + name + " matches private recovery credentials", flush=True)


def active_pods():
    return [pod for pod in json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
            if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") or
            any(status.get("state", {}).get("running") for key in ("containerStatuses", "initContainerStatuses")
                for status in pod.get("status", {}).get(key, []))]


def idle():
    deadline = time.monotonic() + 180
    while active_pods():
        if time.monotonic() >= deadline:
            raise setup.SetupError("a previous scratch worker is still active; preserving it")
        time.sleep(2)


def restore_artifacts(component, state):
    check_namespace()
    check_binding(component, "artifacts")
    destination = setup.get_object("replicationdestination", destination_name(component), NS)
    if destination["spec"].get("paused"):
        idle()
        if any((base(component) / "artifacts").iterdir()):
            raise setup.SetupError("paused restore destination already has data; preserving it")
        backup.worker_budget()
        setup.kube("patch", "replicationdestination", destination_name(component), "-n", NS, "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/uid", "value": destination["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": destination["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/spec/paused", "value": False},
            {"op": "replace", "path": "/spec/trigger", "value": {"manual": state["backup_id"]}},
        ]))
    deadline, last_message = time.monotonic() + 7200, 0
    while True:
        destination = setup.get_object("replicationdestination", destination_name(component), NS)
        if not destination or destination["spec"].get("trigger") != {"manual": state["backup_id"]}:
            raise setup.SetupError("restore generation changed during download")
        if backup.upload_complete(destination, state["backup_id"]):
            break
        if time.monotonic() >= deadline:
            raise setup.SetupError("VolSync restore exceeded two hours; scratch data retained")
        if time.monotonic() - last_message >= 60:
            print("waiting for " + component + " VolSync restore and cleanup", flush=True)
            last_message = time.monotonic()
        time.sleep(5)
    idle()
    check_binding(component, "cache")
    artifacts = base(component) / "artifacts"
    if {path.name for path in artifacts.iterdir()} == {"data"}:
        setup.checked_directory(artifacts / "data")
        artifacts /= "data"
    current = artifacts / "current"
    checksums = backup.inspect_artifacts(component, state["backup_id"], current=current)
    if checksums != state["components"][component]["checksums"]:
        raise setup.SetupError("restored artifacts differ from the verified initial snapshot")
    backup.save(setup.RECOVERY / ("initial-" + component + "-volsync-restore-result.json"), destination)
    print("all six artifacts restored and checksummed: " + component, flush=True)
    return current


def validator_config():
    code = Path(__file__).with_name("validate-monitoring-restore.py").read_text()
    name = "restore-validator-" + hashlib.sha256(code.encode()).hexdigest()[:16]
    desired = resource("ConfigMap", name, immutable=True, data={"validate.py": code})
    current = object_for(desired)
    if current:
        managed(current)
        if current.get("data") != desired["data"] or current.get("immutable") is not True or current.get("binaryData"):
            raise setup.SetupError("validator ConfigMap differs from its immutable revision")
    else:
        setup.kube("create", "-f", "-", stdin=json.dumps(desired))
    return name


def existing_validator(job, *, allow_legacy=True):
    volumes = [volume for volume in job["spec"]["template"]["spec"].get("volumes", []) if volume["name"] == "code"]
    if len(volumes) != 1:
        raise setup.SetupError("existing validation Job has no unique code volume")
    name = volumes[0].get("configMap", {}).get("name")
    config = setup.get_object("configmap", name, NS, sensitive=True) if name else None
    if not config:
        raise setup.SetupError("existing validation code disappeared")
    managed(config)
    code = config.get("data", {}).get("validate.py")
    if not isinstance(code, str):
        raise setup.SetupError("existing validation code is invalid")
    checksum = hashlib.sha256(code.encode()).hexdigest()
    approved = {hashlib.sha256(Path(__file__).with_name("validate-monitoring-restore.py").read_bytes()).hexdigest()}
    if allow_legacy:
        approved.add(LEGACY_VALIDATOR_SHA256)
    if (config.get("immutable") is not True or set(config.get("data", {})) != {"validate.py"} or config.get("binaryData") or
            checksum not in approved or name != "restore-validator-" + checksum[:16]):
        raise setup.SetupError("existing validation code is not an approved immutable revision")
    return name


def target_storage(component, target):
    checked_target(component, target)
    size = setup.PROTECTED[component]["size"]
    volume = resource("PersistentVolume", target["volume"], {
        "capacity": {"storage": size}, "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
        "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "monitoring-backup-local",
        "local": {"path": target["path"]}, "claimRef": {"namespace": NS, "name": target["claim"]},
        "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": ["openmediavault"]}]}]}},
    }, namespace=None)
    claim = resource("PersistentVolumeClaim", target["claim"], {
        "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "storageClassName": "monitoring-backup-local",
        "volumeName": target["volume"], "resources": {"requests": {"storage": size}},
    })
    return volume, claim


def check_target_binding(component, target):
    expected_volume, expected_claim = target_storage(component, target)
    volume, claim = object_for(expected_volume), object_for(expected_claim)
    if not volume or not claim:
        raise setup.SetupError("validation scratch storage disappeared")
    check_existing(expected_volume, volume, {})
    check_existing(expected_claim, claim, {})
    if (claim.get("status", {}).get("phase") != "Bound" or volume.get("status", {}).get("phase") != "Bound" or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"]):
        raise setup.SetupError("validation scratch PVC binding differs")
    setup.checked_directory(Path(target["path"]))


def validation_job(component, state, current, code_name, *, target=None):
    target = checked_target(component, target or validation_target(component))
    denied = []
    for name, port in (("restic", 8000), ("kube-prometheus-stack-prometheus", 9090),
                       ("kube-prometheus-stack-alertmanager", 9093), ("kube-prometheus-stack-grafana", 80), ("loki", 3100)):
        service = setup.get_object("service", name, "monitoring")
        ips = service["spec"].get("clusterIPs") or [service["spec"]["clusterIP"]]
        denied.extend((str(ipaddress.ip_address(address)), port) for address in ips)
    environment = {"COMPONENT": component, "BACKUP_ID": state["backup_id"],
                   "EXPECTED_VOLUME": json.dumps(setup.PROTECTED[component]),
                   "EXPECTED_SHA256SUMS": state["components"][component]["checksums"],
                   "ARTIFACT_DIRECTORY": "/artifacts/" + current.relative_to(base(component) / "artifacts").as_posix(),
                   "DENIED_ENDPOINTS": json.dumps(denied)}
    mounts = [{"name": "artifacts", "mountPath": "/artifacts", "readOnly": True},
              {"name": "data", "mountPath": "/restore"}, {"name": "code", "mountPath": "/code", "readOnly": True},
              {"name": "temp", "mountPath": "/tmp"}]
    tools = {"name": "validate", "image": TOOLS_IMAGE, "workingDir": "/tmp",
             "command": [PYTHON, "-u", "/code/validate.py"], "args": ["native"],
             "env": [{"name": key, "value": value} for key, value in environment.items()],
             "securityContext": CONTAINER_SECURITY, "volumeMounts": mounts,
             "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}}}
    spec = {"restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
            "nodeSelector": {"kubernetes.io/hostname": "openmediavault"}, "securityContext": SECURITY,
            "terminationGracePeriodSeconds": 90, "containers": [tools], "volumes": [
                {"name": "artifacts", "persistentVolumeClaim": {"claimName": component + "-restore-artifacts", "readOnly": True}},
                {"name": "data", "persistentVolumeClaim": {"claimName": target["claim"]}},
                {"name": "code", "configMap": {"name": code_name, "defaultMode": 0o444}},
                {"name": "temp", "emptyDir": {"sizeLimit": "64Mi"}},
            ]}
    if component == "grafana":
        tools["args"] = ["grafana"]
        tools["resources"]["requests"] = {"cpu": "50m", "memory": "128Mi"}
    else:
        extract = json.loads(json.dumps(tools))
        extract.update({"name": "extract", "args": ["extract"]})
        extract["resources"]["requests"] = {"cpu": "50m", "memory": "128Mi"}
        native_args = {
            "prometheus": ["--config.file=/validation/native.yaml", "--storage.tsdb.path=/prometheus/prometheus-db",
                           "--web.listen-address=127.0.0.1:9090", "--storage.tsdb.retention.time=10000d"],
            "alertmanager": ["--config.file=/validation/native.yaml", "--storage.path=/alertmanager/alertmanager-db",
                             "--web.listen-address=127.0.0.1:9093", "--cluster.listen-address="],
            "loki": ["-config.file=/validation/native.yaml", "-target=all"],
        }
        native = {"name": "server", "restartPolicy": "Always", "image": setup.PROTECTED[component]["image_id"],
                  "args": native_args[component], "workingDir": "/tmp", "securityContext": CONTAINER_SECURITY,
                  "resources": {"requests": {"cpu": "40m", "memory": "96Mi"}, "limits": {"cpu": "2", "memory": "3Gi"}},
                  "volumeMounts": [{"name": "data", "mountPath": setup.PROTECTED[component]["mount"], "subPath": "volume"},
                                   {"name": "data", "mountPath": "/validation", "subPath": "work"},
                                   {"name": "temp", "mountPath": "/tmp"}]}
        spec["initContainers"] = [extract, native]
    return resource("Job", target["job"], {"backoffLimit": 0, "activeDeadlineSeconds": 7200,
                    "template": {"metadata": {"labels": dict(LABELS)}, "spec": spec}}, api="batch/v1")


def same_template(actual, expected):
    """Compare the full security-sensitive template, allowing only known API defaults."""
    actual = json.loads(json.dumps(actual))
    for key in ("dnsPolicy", "schedulerName", "serviceAccount", "serviceAccountName"):
        value = actual.pop(key, None)
        if value not in (None, {"dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
                              "serviceAccount": "default", "serviceAccountName": "default"}[key]):
            return False
    for group in ("containers", "initContainers"):
        for container in actual.get(group, []):
            for key, default in (("imagePullPolicy", "IfNotPresent"), ("terminationMessagePath", "/dev/termination-log"),
                                 ("terminationMessagePolicy", "File")):
                if container.pop(key, default) != default:
                    return False
    return actual == expected


def diagnostics(component, *, target=None, report=False):
    target = checked_target(component, target or validation_target(component))
    name = target["job"]
    prefix = "initial-" + component + ("-duplicates-v1" if target != validation_target(component) else "")
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "job-name=" + name, "-o", "json", capture=True).stdout)["items"]
    backup.save(setup.RECOVERY / (prefix + "-restore-pods.json"), pods)
    for pod in pods:
        for group in ("initContainers", "containers"):
            for container in pod["spec"].get(group, []):
                logs = setup.kube("logs", "-n", NS, pod["metadata"]["name"], "-c", container["name"],
                                  capture=True, sensitive=True, check=False).stdout
                setup.private_write(setup.RECOVERY / (prefix + "-" + container["name"] + "-restore.log"), logs)
                if report and container["name"] == "validate":
                    for line in logs.splitlines():
                        if line.startswith(("LOKI_RECOVERY_COUNTERS_JSON=", "restore validation failed: ")):
                            print(name + ": " + line, flush=True)
    return pods


def log_timestamp(line):
    match = re.search(r'\b(?:time|ts)="?([^\s"]+)', line)
    if match:
        try:
            value = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
            return value if value.tzinfo is not None else None
        except ValueError:
            pass
    return None


def recovery_log_errors(logs, validated_at, *, component=None):
    cutoff = datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise setup.SetupError("validation completion timestamp has no timezone")
    lines = logs.splitlines()
    starts = [log_timestamp(line) for line in lines if 'msg="Starting Loki"' in line]
    started = [log_timestamp(line) for line in lines if 'msg="Loki started"' in line]
    bootstrap = (component == "loki" and len(starts) == len(started) == 1 and starts[0] is not None and
                 started[0] is not None and 0 <= (started[0] - starts[0]).total_seconds() <= 30 and started[0] <= cutoff)
    for line in lines:
        if not re.search(r'level="?(?:error|warn[^\n]*(?:load|corrupt|checksum|recover|silence|nflog))', line, re.IGNORECASE):
            continue
        observed = log_timestamp(line)
        if (bootstrap and observed is not None and starts[0] <= observed <= started[0] and
                re.search(r"\bcaller=ratestore.go:[0-9]+(?:\s|$)", line) and
                'msg="error getting ingester clients"' in line and re.search(r'\berr="empty ring"(?:\s|$)', line) and
                not re.search(r"corrupt|checksum|\bwal\b|recover|snapshot", line, re.IGNORECASE)):
            continue  # The rate store can run before the standalone ingester first joins.
        canceled = re.search(
            r'\b(?:err|error)="(?:context canceled|rpc error: code = Canceled desc = context canceled)"(?:\s|$)', line)
        if (observed is not None and observed > cutoff and canceled and
                not re.search(r"corrupt|checksum|\bwal\b|recover|snapshot", line, re.IGNORECASE)):
            continue  # Ignore only recognized teardown cancellations, retaining other late errors.
        return True
    return False


def validation(component, state, current, code_name, *, target=None):
    target = checked_target(component, target or validation_target(component))
    data = Path(target["path"])
    prefix = "initial-" + component + ("-duplicates-v1" if target != validation_target(component) else "")
    check_namespace()
    check_target_binding(component, target)
    existing = setup.get_object("job", target["job"], NS)
    if existing:
        managed(existing)
        code_name = existing_validator(existing, allow_legacy=target == validation_target(component))
    job = validation_job(component, state, current, code_name, target=target)
    job["metadata"]["annotations"] = {"backups.mousses.xyz/generation": state["backup_id"],
                                         "backups.mousses.xyz/snapshot": state["components"][component]["snapshot"]}
    if existing:
        if (existing["metadata"].get("annotations") != job["metadata"]["annotations"] or
                existing["spec"].get("backoffLimit") != 0 or existing["spec"].get("activeDeadlineSeconds") != 7200 or
                not same_template(existing["spec"]["template"]["spec"], job["spec"]["template"]["spec"])):
            raise setup.SetupError("existing restore Job differs; preserving it")
    else:
        idle()
        if any(data.iterdir()):
            raise setup.SetupError("scratch data has no matching validation Job; preserving it")
        available = re.search(r"^MemAvailable:\s+(\d+) kB$", Path("/proc/meminfo").read_text(), re.MULTILINE)
        if not available or int(available[1]) < 4 * 1024**2:
            raise setup.SetupError("native restore needs at least 4 GiB physical MemAvailable")
        backup.worker_budget()
        setup.kube("create", "-f", "-", stdin=json.dumps(job))
    deadline, last_message = time.monotonic() + 7260, 0
    try:
        while True:
            existing = object_for(job)
            conditions = existing.get("status", {}).get("conditions", [])
            if any(item["type"] in ("Failed", "FailureTarget") and item["status"] == "True" for item in conditions):
                raise setup.SetupError("restore Job failed; private logs and scratch data retained: " + component)
            if any(item["type"] == "Complete" and item["status"] == "True" for item in conditions):
                break
            if time.monotonic() >= deadline:
                raise setup.SetupError("restore validation exceeded two hours; scratch data retained")
            if time.monotonic() - last_message >= 60:
                print("waiting for isolated " + component + " extraction and validation", flush=True)
                last_message = time.monotonic()
            time.sleep(5)
    finally:
        pods = diagnostics(component, target=target, report=True)
    idle()
    if len(pods) != 1 or pods[0].get("status", {}).get("phase") != "Succeeded":
        raise setup.SetupError("expected one successful isolated restore Pod")
    statuses = {item["name"]: item for key in ("containerStatuses", "initContainerStatuses") for item in pods[0]["status"].get(key, [])}
    for group in ("containers", "initContainers"):
        for container in job["spec"]["template"]["spec"].get(group, []):
            status = statuses.get(container["name"], {})
            if (status.get("restartCount") != 0 or not status.get("state", {}).get("terminated") or
                    not status.get("imageID", "").endswith(container["image"].split("@", 1)[1])):
                raise setup.SetupError("restore container restarted or its image identity differs")
    logs = setup.private_read(setup.RECOVERY / (prefix + "-validate-restore.log"))
    results = [json.loads(line.removeprefix("RESTORE_VALIDATED_JSON=")) for line in logs.splitlines()
               if line.startswith("RESTORE_VALIDATED_JSON=")]
    if len(results) != 1:
        raise setup.SetupError("restore Job did not report exactly one validation result")
    result = results[0]
    if component != "grafana":
        logs = setup.private_read(setup.RECOVERY / (prefix + "-server-restore.log"))
        if recovery_log_errors(logs, result.get("validated_at", ""), component=component):
            raise setup.SetupError("native recovery reported an error; inspect the private server log: " + component)
    metadata = json.loads((current / "metadata.json").read_text())
    if (result.get("result") != "passed" or result.get("backup_id") != state["backup_id"] or
            result.get("claim") != setup.PROTECTED[component]["claim"] or result.get("component") != component or
            not all(result.get(key) is True for key in ("archive_metadata_verified", "file_checksums_verified", "application_config_verified")) or
            (component != "grafana" and result.get("native_recovery_verified") is not True) or
            (component in ("prometheus", "loki") and result.get("recovery_error_counters") != 0) or
            (component == "loki" and any(type(result.get(key, 0)) is not int or result.get(key, 0) < 0
                                        for key in ("wal_replay_duplicate_samples", "wal_replay_duplicate_bytes"))) or
            any(result.get(key) != metadata.get(key) for key in ("files", "file_bytes")) or
            (component == "grafana" and (result.get("sqlite_integrity") != "ok" or not result.get("table_counts")))):
        raise setup.SetupError("restore validation evidence is incomplete")
    evidence = data / "result.json"
    attributes = evidence.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
            (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600) or
            json.loads(evidence.read_text()) != result):
        raise setup.SetupError("scratch evidence differs from the completed restore Job")
    backup.save(setup.RECOVERY / ("initial-" + component + "-restore-result.json"), {
        **result, "snapshot": state["components"][component]["snapshot"], "completed_at": backup.now(),
        "job": target["job"], "pod_uid": pods[0]["metadata"]["uid"],
    })
    print("isolated restore verified: " + component, flush=True)
    print(json.dumps({key: value for key, value in result.items() if key not in ("table_counts",)}, sort_keys=True), flush=True)


def other_restores_verified(state):
    for component in COMPONENTS:
        if component == "loki":
            continue
        receipt = backup.saved(setup.RECOVERY / ("initial-" + component + "-restore-result.json"))
        job = setup.get_object("job", job_name(component), NS)
        if (receipt.get("result") != "passed" or receipt.get("backup_id") != state["backup_id"] or
                receipt.get("claim") != setup.PROTECTED[component]["claim"] or
                receipt.get("snapshot") != state["components"][component]["snapshot"] or
                receipt.get("job") != job_name(component) or not job or not any(
                    condition.get("type") == "Complete" and condition.get("status") == "True"
                    for condition in job.get("status", {}).get("conditions", []))):
            raise setup.SetupError("Loki-only retry requires the other three verified restores")


def prepare_loki_retry(state, current):
    component = "loki"
    idle()
    check_target_binding(component, validation_target(component))
    old = setup.get_object("job", job_name(component), NS)
    if not old:
        raise setup.SetupError("the original failed Loki validation Job must be retained")
    managed(old)
    old_code = existing_validator(old)
    if old_code != "restore-validator-" + LEGACY_VALIDATOR_SHA256[:16]:
        raise setup.SetupError("the failed Loki Job does not use the reviewed initial validator")
    expected = validation_job(component, state, current, old_code)
    annotations = {"backups.mousses.xyz/generation": state["backup_id"],
                   "backups.mousses.xyz/snapshot": state["components"][component]["snapshot"]}
    status = old.get("status", {})
    if (old["metadata"].get("annotations") != annotations or old["spec"].get("backoffLimit") != 0 or
            old["spec"].get("activeDeadlineSeconds") != 7200 or not old["metadata"].get("uid") or
            any(status.get(key, 0) for key in ("active", "ready", "terminating", "succeeded")) or not any(
                condition.get("type") == "Failed" and condition.get("status") == "True" and
                condition.get("reason") == "BackoffLimitExceeded" for condition in status.get("conditions", [])) or
            not same_template(old["spec"]["template"]["spec"], expected["spec"]["template"]["spec"])):
        raise setup.SetupError("original Loki failure identity, generation, or Job specification differs")
    pods = diagnostics(component)
    if len(pods) != 1:
        raise setup.SetupError("expected one retained failed Loki Pod")
    pod = pods[0]
    statuses = {item["name"]: item for group in ("initContainerStatuses", "containerStatuses")
                for item in pod.get("status", {}).get(group, [])}
    expected_exits = {"extract": 0, "server": 0, "validate": 1}
    if (pod.get("status", {}).get("phase") != "Failed" or pod["spec"].get("ephemeralContainers") or
            set(statuses) != set(expected_exits) or any(
                statuses[name].get("restartCount") != 0 or
                statuses[name].get("state", {}).get("terminated", {}).get("exitCode") != exit_code
                for name, exit_code in expected_exits.items()) or not any(
                    owner.get("uid") == old["metadata"]["uid"] and owner.get("controller") is True and owner.get("kind") == "Job"
                    for owner in pod["metadata"].get("ownerReferences", []))):
        raise setup.SetupError("original Loki validation containers are not stopped with the expected failure")
    for group in ("initContainers", "containers"):
        for container in expected["spec"]["template"]["spec"].get(group, []):
            if not statuses[container["name"]].get("imageID", "").endswith(container["image"].split("@", 1)[1]):
                raise setup.SetupError("original Loki validation image identity differs")
    checker_log = setup.private_read(setup.RECOVERY / "initial-loki-validate-restore.log")
    server_log = setup.private_read(setup.RECOVERY / "initial-loki-server-restore.log")
    if ("restore validation failed: native recovery reported corruption or discarded data;" not in checker_log or
            not re.search(r'msg="recovered WAL checkpoint recovery finished"[^\n]*errors=false', server_log) or
            not re.search(r'msg="WAL segment recovery finished"[^\n]*errors=false', server_log)):
        raise setup.SetupError("original Loki failure differs from the reviewed replay-counter check")
    old_data = Path(validation_target(component)["path"])
    extracted = old_data / "extracted.json"
    backup.digest_file(extracted)
    result = json.loads(extracted.read_text())
    metadata = json.loads((current / "metadata.json").read_text())
    if (result.get("result") != "passed" or result.get("backup_id") != state["backup_id"] or
            result.get("component") != component or result.get("claim") != setup.PROTECTED[component]["claim"] or
            any(result.get(key) != metadata.get(key) for key in ("files", "file_bytes")) or
            not all(result.get(key) is True for key in ("archive_metadata_verified", "file_checksums_verified", "application_config_verified"))):
        raise setup.SetupError("original Loki extraction evidence differs")
    target = validation_target(component, retry=True)
    retry = {"backup_id": state["backup_id"], "failed_job_uid": old["metadata"]["uid"],
             "failed_pod_uid": pod["metadata"]["uid"], "target": target,
             "validator_sha256": hashlib.sha256(Path(__file__).with_name("validate-monitoring-restore.py").read_bytes()).hexdigest()}
    if LOKI_RETRY_STATE.exists() or LOKI_RETRY_STATE.is_symlink():
        if backup.saved(LOKI_RETRY_STATE) != retry:
            raise setup.SetupError("saved Loki retry identity changed; preserving scratch data")
    else:
        backup.save(LOKI_RETRY_STATE, retry)
    backup.save(setup.RECOVERY / "initial-loki-counter-failure-job.json", old)
    resources = target_storage(component, target)
    existing = [object_for(item) for item in resources]
    for item, actual in zip(resources, existing):
        if actual:
            check_existing(item, actual, state)
    path = Path(target["path"])
    setup.checked_directory(path)
    if path.exists():
        attributes = path.stat()
        if ((any(path.iterdir()) and not all(existing)) or
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o2770)):
            raise setup.SetupError("new Loki retry directory has unexpected data or permissions; preserving it")
    else:
        path.mkdir(parents=True, mode=0o2770)
        os.chown(path, 999, 999)
        os.chmod(path, 0o2770)
    for item, actual in zip(resources, existing):
        if not actual:
            setup.kube("create", "-f", "-", stdin=json.dumps(item))
    setup.kube("wait", "-n", NS, "pvc/" + target["claim"], "--for=jsonpath={.status.phase}=Bound", "--timeout=60s")
    check_target_binding(component, target)
    print("original failed Loki Job/data retained; fresh retry PVC verified on the array: " + target["path"], flush=True)
    return target


def restore(*, retry_loki=False):
    state, before = restore_state()
    retry_loki = retry_loki or LOKI_RETRY_STATE.exists()
    if retry_loki:
        other_restores_verified(state)
    prepare_namespace()
    credentials = backup.saved(setup.RECOVERY / "credentials.json")["repositories"]
    for component in COMPONENTS:
        claim = setup.PROTECTED[component]["claim"]
        ensure_secret("restic-restore-" + component, setup.credential_fields(claim, credentials[claim]))
    ca = backup.saved(setup.RECOVERY / "restic-root-ca.secret.json")
    ensure_secret("restic-restore-ca", {"ca.crt": base64.b64decode(ca["data"]["tls.crt"], validate=True).decode()})
    prepare_storage(state)
    code_name = validator_config()
    for component in COMPONENTS:
        current = restore_artifacts(component, state)
        target = prepare_loki_retry(state, current) if component == "loki" and retry_loki else None
        validation(component, state, current, code_name, target=target)
    check_namespace()
    after = setup.live_preflight(check_resources=False)
    for component in COMPONENTS:
        if before[component] != after[component]:
            raise setup.SetupError("production identity or restart count changed during isolated restores")
        backup.service_health(component)
    backup.save(setup.RECOVERY / "initial-restore-verification.json", {
        "result": "passed", "backup_id": state["backup_id"], "namespace": NS, "completed_at": backup.now(),
        "components": {component: backup.saved(setup.RECOVERY / ("initial-" + component + "-restore-result.json"))
                       for component in COMPONENTS},
    })
    print("all four isolated restores verified; daily scheduling and independent credential storage remain pending", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-loki-duplicates", action="store_true", help="retain the failed initial Loki Job and use a fresh scratch PVC")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, backup.interrupted)
    with ExitStack() as locks:
        locks.enter_context(setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True))
        locks.enter_context(setup.operation_lock(setup.COORDINATION_LOCK, create=False))
        print("monitoring restore worker serialized with board-games and SearXNG", flush=True)
        restore(retry_loki=arguments.retry_loki_duplicates)


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("monitoring restore stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
