#!/usr/bin/env python3
"""Apply the reviewed Loki DNS/rollout repair and verify it on OMV."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode


NAMESPACE = "monitoring"
ROOT = Path("/root/k3s-backup-recovery/monitoring")
RECOVERY = ROOT / "loki-repair"
CHART = "https://github.com/grafana-community/helm-charts/releases/download/loki-18.13.4/loki-18.13.4.tgz"
CLAIM = "storage-loki-0"
VOLUME = "monitoring-loki"
STABILITY_SECONDS = 300


class RepairError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RepairError(message)


def kube(*arguments, timeout=40, request_timeout="20s"):
    result = subprocess.run(["k3s", "kubectl", "--request-timeout=" + request_timeout, *arguments],
                            capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0, "kubectl command failed: " + arguments[0])
    return result.stdout


def get(kind, name, namespace=NAMESPACE, *, optional=False):
    arguments = ["get", kind, name, "--ignore-not-found=true", "-o", "json"]
    if namespace:
        arguments.extend(["-n", namespace])
    output = kube(*arguments)
    require(optional or output.strip(), "required resource is missing: " + kind + "/" + name)
    return json.loads(output) if output.strip() else None


def private_directory():
    current = Path("/root")
    for part in RECOVERY.relative_to(current).parts:
        current /= part
        if not os.path.lexists(current):
            current.mkdir(mode=0o700)
        attributes = current.lstat()
        require(stat.S_ISDIR(attributes.st_mode) and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o700, "recovery directory must be private and root-owned")


def saved(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as stream:
        attributes = os.fstat(stream.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o600 and 0 < attributes.st_size < 4 * 1024**2,
                "saved repair evidence is not a private regular file")
        value = json.load(stream)
    require(isinstance(value, dict), "saved repair evidence is invalid")
    return value


def save(path, value):
    if os.path.lexists(path):
        saved(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=RECOVERY)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(RECOVERY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def manifest(path):
    value = json.loads(kube("create", "--dry-run=client", "-f", str(path), "-o", "json"))
    require(value.get("apiVersion") == "helm.cattle.io/v1" and value.get("kind") == "HelmChart" and
            value.get("metadata", {}).get("name") == "loki" and value["metadata"].get("namespace") == "kube-system" and
            value.get("spec", {}).get("chart") == CHART and value["spec"].get("targetNamespace") == NAMESPACE and
            isinstance(value["spec"].get("valuesContent"), str), "downloaded manifest has an unexpected identity")
    return value


def binding():
    claim = get("pvc", CLAIM)
    volume = get("pv", VOLUME, namespace=None)
    require(claim.get("status", {}).get("phase") == "Bound" and claim["spec"].get("volumeName") == VOLUME and
            volume["spec"].get("claimRef", {}).get("uid") == claim["metadata"]["uid"] and
            volume["spec"].get("local", {}).get("path") == "/filesystem/k3s/data/monitoring/loki" and
            volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "the Loki storage binding differs")
    return claim, volume


def ndots_one(spec):
    return any(item.get("name") == "ndots" and item.get("value") == "1"
               for item in spec.get("dnsConfig", {}).get("options", []))


def settings_applied():
    loki = get("statefulset", "loki")
    gateway = get("deployment", "loki-gateway")
    service = get("service", "loki-memberlist")
    return (loki["spec"].get("replicas") == 1 and gateway["spec"].get("replicas") == 1 and
            ndots_one(loki["spec"]["template"]["spec"]) and ndots_one(gateway["spec"]["template"]["spec"]) and
            gateway["spec"].get("strategy") == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}} and
            service["spec"].get("publishNotReadyAddresses") is True)


def ready_loki():
    pod = get("pod", "loki-0")
    main = next((item for item in pod.get("status", {}).get("containerStatuses", []) if item["name"] == "loki"), None)
    claims = {item["name"]: item.get("persistentVolumeClaim", {}) for item in pod["spec"].get("volumes", [])}
    container = next((item for item in pod["spec"]["containers"] if item["name"] == "loki"), {})
    require(not pod["metadata"].get("deletionTimestamp") and ndots_one(pod["spec"]) and main and main.get("ready") and
            any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", [])) and
            any(item.get("mountPath") == "/var/loki" and not item.get("readOnly") and
                claims.get(item["name"], {}).get("claimName") == CLAIM and not claims[item["name"]].get("readOnly")
                for item in container.get("volumeMounts", [])), "Loki is not ready on its original writable claim")
    return {"pod_uid": pod["metadata"]["uid"], "restart_count": main.get("restartCount"), "image_id": main.get("imageID")}


def stable():
    expected = ready_loki()
    require(expected["image_id"] == "docker.io/grafana/loki@sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193",
            "the repaired Loki image differs from the inventoried running image")
    deadline = time.monotonic() + STABILITY_SECONDS
    next_notice = time.monotonic()
    print("observing Loki readiness and restart count for five minutes", flush=True)
    while True:
        require(ready_loki() == expected, "Loki restarted or changed during the stability check")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return expected
        if time.monotonic() >= next_notice:
            print("Loki remains ready; stability seconds remaining: " + str(int(remaining)), flush=True)
            next_notice = time.monotonic() + 60
        time.sleep(min(5, remaining))


def repair(baseline_path, manifest_path):
    before, after = manifest(baseline_path), manifest(manifest_path)
    require({key: value for key, value in before["spec"].items() if key != "valuesContent"} ==
            {key: value for key, value in after["spec"].items() if key != "valuesContent"}, "the repair changes unrelated chart settings")
    require(before["spec"]["valuesContent"] != after["spec"]["valuesContent"], "the repair manifest has no values change")
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    require([item["metadata"]["name"] for item in nodes] == ["openmediavault"], "expected only OMV during this migration")
    current = get("helmchart", "loki", "kube-system")
    for key, value in before["spec"].items():
        if key != "valuesContent":
            require(current["spec"].get(key) == value, "live HelmChart differs from the reviewed baseline")
    require(not current["spec"].get("set") and not current["spec"].get("valuesSecrets") and
            get("helmchartconfig", "loki", "kube-system", optional=True) is None,
            "additional Helm values overrides require inspection before repair")
    require(current["spec"].get("valuesContent") in (before["spec"]["valuesContent"], after["spec"]["valuesContent"]),
            "live Helm values differ from both reviewed versions; preserve and inspect the drift")
    claim, volume = binding()
    source = RECOVERY / "helmchart-loki.before.json"
    if os.path.lexists(source):
        original = saved(source)
        require(original["metadata"]["uid"] == current["metadata"]["uid"] and
                original["spec"]["valuesContent"] == before["spec"]["valuesContent"], "saved HelmChart rollback evidence differs")
    else:
        require(current["spec"]["valuesContent"] == before["spec"]["valuesContent"], "patched chart has no saved pre-change evidence")
        save(source, current)
    resources = {"statefulset-loki": get("statefulset", "loki"), "deployment-loki-gateway": get("deployment", "loki-gateway"),
                 "service-loki-memberlist": get("service", "loki-memberlist"), "pod-loki-0": get("pod", "loki-0"),
                 "pvc-loki": claim, "pv-loki": volume}
    require(resources["statefulset-loki"]["spec"].get("replicas") == 1 and
            resources["deployment-loki-gateway"]["spec"].get("replicas") == 1, "Loki or gateway replica count changed")
    for name, resource in resources.items():
        path = RECOVERY / (name + ".before.json")
        if not os.path.lexists(path):
            save(path, resource)
        elif name in ("pvc-loki", "pv-loki"):
            require(saved(path)["metadata"]["uid"] == resource["metadata"]["uid"], "the original storage identity changed")
    print("original HelmChart, workloads, and storage metadata saved privately at " + str(RECOVERY), flush=True)
    if current["spec"]["valuesContent"] != after["spec"]["valuesContent"]:
        patch = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                 {"op": "test", "path": "/spec/valuesContent", "value": before["spec"]["valuesContent"]},
                 {"op": "replace", "path": "/spec/valuesContent", "value": after["spec"]["valuesContent"]}]
        kube("patch", "helmchart", "loki", "-n", "kube-system", "--type=json", "-p", json.dumps(patch))
        print("helmchart/loki DNS, memberlist bootstrap, and gateway rollout values patched", flush=True)
    print("waiting for the Helm controller to apply the expected settings", flush=True)
    deadline = time.monotonic() + 900
    while not settings_applied():
        require(time.monotonic() < deadline, "Helm did not apply the expected settings within 15 minutes")
        time.sleep(3)
    for kind, name in (("statefulset", "loki"), ("deployment", "loki-gateway")):
        print(kube("rollout", "status", kind + "/" + name, "-n", NAMESPACE, "--timeout=600s",
                   timeout=630, request_timeout="610s"), end="", flush=True)
    state = stable()
    require(settings_applied(), "the repaired workload settings changed during verification")
    latest_claim, latest_volume = binding()
    require(latest_claim["metadata"]["uid"] == claim["metadata"]["uid"] and
            latest_volume["metadata"]["uid"] == volume["metadata"]["uid"], "storage identity changed during repair")
    prefix = "/api/v1/namespaces/monitoring/services/http:loki-gateway:http/proxy/loki/api/v1/"
    info = json.loads(kube("get", "--raw", prefix + "status/buildinfo"))
    require(info.get("version") == "3.7.8", "gateway did not return the expected Loki build information")
    query = urlencode({"query": '{cluster="openmediavault"}', "start": str(time.time_ns() - 300 * 10**9),
                       "end": str(time.time_ns()), "limit": "1", "direction": "backward"})
    recent = json.loads(kube("get", "--raw", prefix + "query_range?" + query))
    require(recent.get("status") == "success" and recent.get("data", {}).get("resultType") == "streams",
            "gateway log query did not succeed")
    entries = sum(len(item.get("values", [])) for item in recent["data"].get("result", []))
    result = {"result": "passed", "verified_at": datetime.now(timezone.utc).isoformat(), **state,
              "stability_seconds": STABILITY_SECONDS, "original_pvc_uid": claim["metadata"]["uid"],
              "original_pv_uid": volume["metadata"]["uid"], "gateway_build_info_version": info["version"],
              "gateway_log_query": "passed", "recent_log_entries": entries,
              "recent_ingestion": "observed" if entries else "not yet observed; inspect Alloy before backup onboarding"}
    save(RECOVERY / "result.json", result)
    print(json.dumps(result, indent=2), flush=True)
    print("Loki DNS/rollout repair verified; monitoring backup setup remains pending", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args()
    require(os.geteuid() == 0, "run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    require(shutil.which("k3s"), "k3s is required on OMV")
    private_directory()
    descriptor = os.open(ROOT / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        attributes = os.fstat(lock.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
                attributes.st_uid == 0 and stat.S_IMODE(attributes.st_mode) == 0o600,
                "namespace lock is not private and root-owned")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RepairError("another monitoring operation is running") from error
        repair(arguments.baseline, arguments.manifest)


if __name__ == "__main__":
    try:
        main()
    except RepairError as error:
        print("Loki repair stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        print("Loki repair stopped: files, API metadata, or command execution could not be verified", file=sys.stderr)
        sys.exit(1)
