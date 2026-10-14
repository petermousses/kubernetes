#!/usr/bin/env python3
"""Repair Alloy's Loki endpoint and require fresh, successfully delivered logs."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode


NAMESPACE = "monitoring"
ROOT = Path("/root/k3s-backup-recovery/monitoring")
RECOVERY = ROOT / "alloy-repair"
CHART = "https://github.com/grafana/helm-charts/releases/download/alloy-1.12.1/alloy-1.12.1.tgz"
HOST = "loki-gateway.monitoring.svc.cluster.local"
OLD_URL = "http://" + HOST + "/loki/api/v1/push"
NEW_URL = "http://" + HOST + "./loki/api/v1/push"
CONFIG_HASHES = {"before": "a7e51567a33ecb6e9ec058fa4af31396c880b9baeafce5639fe9578985aba129",
                 "after": "556aa89307c9150a376430e0a112810c15789f8165770d766743f84c2268393b"}
ALLOY_IMAGE = "docker.io/grafana/alloy@sha256:b8ec653c44235fbe910879145dac3597d66b0aaecf60bcbbe82580767771a839"
LOKI_IMAGE = "docker.io/grafana/loki@sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193"
STABILITY_SECONDS = 60
METRICS_PATH = "/api/v1/namespaces/monitoring/services/http:alloy:http-metrics/proxy/metrics"


class RepairError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RepairError(message)


def kube(*arguments):
    result = subprocess.run(["k3s", "kubectl", "--request-timeout=20s", *arguments],
                            capture_output=True, text=True, timeout=40)
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


def saved(name):
    descriptor = os.open(RECOVERY / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as stream:
        attributes = os.fstat(stream.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o600 and 0 < attributes.st_size < 4 * 1024**2,
                "saved repair evidence is not a private regular file")
        value = json.load(stream)
    require(isinstance(value, dict), "saved repair evidence is invalid")
    return value


def save(name, value):
    path = RECOVERY / name
    if os.path.lexists(path):
        saved(name)
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
            value.get("metadata", {}).get("name") == "alloy" and value["metadata"].get("namespace") == "kube-system" and
            value.get("spec", {}).get("chart") == CHART and value["spec"].get("targetNamespace") == NAMESPACE and
            isinstance(value["spec"].get("valuesContent"), str), "downloaded manifest has an unexpected identity")
    return value


def config_version(config):
    require(set(config.get("data", {})) == {"config.alloy"}, "Alloy ConfigMap keys differ from the pinned chart")
    digest = hashlib.sha256(config["data"]["config.alloy"].encode()).hexdigest()
    version = next((key for key, value in CONFIG_HASHES.items() if digest == value), None)
    require(version, "live Alloy configuration differs from both reviewed versions")
    return version


def binding():
    claim = get("pvc", "storage-loki-0")
    volume = get("pv", "monitoring-loki", namespace=None)
    require(claim.get("status", {}).get("phase") == "Bound" and volume.get("status", {}).get("phase") == "Bound" and
            claim["spec"].get("volumeName") == "monitoring-loki" and
            volume["spec"].get("claimRef", {}).get("uid") == claim["metadata"]["uid"] and
            volume["spec"].get("local", {}).get("path") == "/filesystem/k3s/data/monitoring/loki" and
            volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "the Loki storage binding differs")
    return claim, volume


def pod_state(pod, container, image):
    statuses = pod.get("status", {}).get("containerStatuses", [])
    main = next((item for item in statuses if item["name"] == container), None)
    require(not pod["metadata"].get("deletionTimestamp") and main and main.get("imageID") == image and
            len(statuses) == len(pod["spec"]["containers"]) and all(item.get("ready") for item in statuses) and
            any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", [])),
            container + " is not ready on its inventoried image")
    return {"pod_uid": pod["metadata"]["uid"], "image_id": main["imageID"],
            "restarts": {item["name"]: item["restartCount"] for item in statuses}}


def alloy_pod():
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l",
                           "app.kubernetes.io/name=alloy,app.kubernetes.io/instance=alloy", "-o", "json"))["items"]
    require(len(pods) == 1, "expected exactly one Alloy Pod; inspect any rollout before repair")
    require({item["name"] for item in pods[0]["spec"]["containers"]} == {"alloy", "config-reloader"},
            "Alloy's config reloader or containers differ")
    return pods[0]


def loki_state():
    pod = get("pod", "loki-0")
    claims = {item["name"]: item.get("persistentVolumeClaim", {}) for item in pod["spec"].get("volumes", [])}
    container = next((item for item in pod["spec"]["containers"] if item["name"] == "loki"), {})
    require(any(item.get("mountPath") == "/var/loki" and not item.get("readOnly") and
                claims.get(item["name"], {}).get("claimName") == "storage-loki-0" and not claims[item["name"]].get("readOnly")
                for item in container.get("volumeMounts", [])), "Loki is not using its original writable claim")
    return pod_state(pod, "loki", LOKI_IMAGE)


def parse_metrics(output, host):
    result = {"sent_entries": 0, "dropped_entries": 0, "retries": 0, "successful_requests": 0}
    names = {"loki_write_sent_entries_total": "sent_entries", "loki_write_dropped_entries_total": "dropped_entries",
             "loki_write_batch_retries_total": "retries", "loki_write_request_duration_seconds_count": "successful_requests"}
    for line in output.splitlines():
        match = re.fullmatch(r"(loki_write_\w+)\{([^}]*)\}\s+([0-9.eE+-]+)(?:\s+\d+)?", line)
        if not match or match[1] not in names:
            continue
        labels = dict(re.findall(r'(\w+)="([^"\\]*)"', match[2]))
        if labels.get("host") != host or labels.get("component_id") != "loki.write.local" or labels.get("tenant") != "":
            continue
        value = float(match[3])
        require(math.isfinite(value) and value >= 0, "delivery counters are invalid")
        if match[1] == "loki_write_request_duration_seconds_count" and labels.get("status_code") != "204":
            continue
        result[names[match[1]]] += value
    return result


def transport_summary(logs):
    lines = [line for line in logs.splitlines() if "component_id=loki.write.local" in line and
             ("level=error" in line or "level=warn" in line)]
    failures = "\n".join(lines)
    return {"write_warning_or_error_lines": len(lines),
            "dial_addresses": sorted(set(re.findall(r"dial (?:tcp|udp) (\d+(?:\.\d+){3}:\d+)", failures))),
            "failure_categories": [name for name in ("connection refused", "context deadline exceeded", "i/o timeout", "no such host")
                                   if name in failures]}


def verify_delivery(expected_alloy, expected_loki, deployment):
    deadline = time.monotonic() + 300
    next_notice = time.monotonic()
    first = None
    since_ns = time.time_ns()
    while True:
        require(time.monotonic() < deadline, "new endpoint did not pass delivery and stability checks within five minutes")
        require(pod_state(alloy_pod(), "alloy", ALLOY_IMAGE) == expected_alloy, "Alloy restarted or changed during verification")
        require(loki_state() == expected_loki, "Loki restarted or changed during verification")
        current = get("deployment", "alloy")
        require(current["metadata"]["uid"] == deployment["metadata"]["uid"] and current["spec"] == deployment["spec"],
                "Alloy Deployment changed during the configuration-only repair")
        counters = parse_metrics(kube("get", "--raw", METRICS_PATH), HOST + ".")
        if first is None and counters["sent_entries"] > 0 and counters["successful_requests"] > 0:
            first = (time.monotonic(), counters)
            since_ns = time.time_ns()
            print("absolute endpoint accepted logs; observing delivery for one minute", flush=True)
        if first is not None:
            require(all(counters[key] >= first[1][key] for key in counters), "delivery counters reset during verification")
            require(counters["dropped_entries"] == first[1]["dropped_entries"] and counters["retries"] == first[1]["retries"],
                    "new endpoint retried or dropped logs during verification")
            if time.monotonic() - first[0] >= STABILITY_SECONDS and counters["sent_entries"] > first[1]["sent_entries"]:
                return {"initial_counters": first[1], "final_counters": counters,
                        "sent_entries_increase": counters["sent_entries"] - first[1]["sent_entries"], "query_start_ns": since_ns}
        if time.monotonic() >= next_notice:
            print("waiting for verified Alloy delivery: " + json.dumps(counters), flush=True)
            next_notice = time.monotonic() + 45
        time.sleep(5)


def repair(baseline_path, manifest_path):
    before, after = manifest(baseline_path), manifest(manifest_path)
    require({key: value for key, value in before["spec"].items() if key != "valuesContent"} ==
            {key: value for key, value in after["spec"].items() if key != "valuesContent"} and
            before["spec"]["valuesContent"].count(OLD_URL) == 1 and
            after["spec"]["valuesContent"] == before["spec"]["valuesContent"].replace(OLD_URL, NEW_URL),
            "the repair must change only the single Loki endpoint's trailing DNS dot")
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    require([item["metadata"]["name"] for item in nodes] == ["openmediavault"], "expected only OMV during this migration")
    current = get("helmchart", "alloy", "kube-system")
    require(all(current["spec"].get(key) == value for key, value in before["spec"].items() if key != "valuesContent") and
            current["spec"].get("valuesContent") in (before["spec"]["valuesContent"], after["spec"]["valuesContent"]),
            "live Alloy HelmChart differs from the reviewed versions")
    require(not current["spec"].get("set") and not current["spec"].get("valuesSecrets") and
            get("helmchartconfig", "alloy", "kube-system", optional=True) is None, "additional Helm overrides require inspection")
    config = get("configmap", "alloy")
    version = config_version(config)
    deployment, pod = get("deployment", "alloy"), alloy_pod()
    require(deployment["spec"].get("replicas") == 1 and deployment.get("status", {}).get("readyReplicas") == 1,
            "Alloy's one-replica deployment is not ready")
    expected_alloy = pod_state(pod, "alloy", ALLOY_IMAGE)
    expected_loki = loki_state()
    claim, volume = binding()
    original = "helmchart-alloy.before.json"
    if os.path.lexists(RECOVERY / original):
        previous = saved(original)
        require(previous["metadata"]["uid"] == current["metadata"]["uid"] and
                previous["spec"]["valuesContent"] == before["spec"]["valuesContent"], "saved Alloy rollback evidence differs")
    else:
        require(current["spec"]["valuesContent"] == before["spec"]["valuesContent"] and version == "before",
                "modified Alloy configuration has no saved pre-change evidence")
        save(original, current)
    for name, resource in {"configmap-alloy": config, "deployment-alloy": deployment, "pod-alloy": pod,
                           "pvc-loki": claim, "pv-loki": volume}.items():
        filename = name + ".before.json"
        if not os.path.lexists(RECOVERY / filename):
            require(name != "configmap-alloy" or version == "before", "original Alloy ConfigMap evidence is missing")
            save(filename, resource)
        elif name != "pod-alloy":
            previous = saved(filename)
            require(previous["metadata"]["uid"] == resource["metadata"]["uid"], "original resource identity changed: " + name)
            if name == "configmap-alloy":
                require(config_version(previous) == "before", "saved original Alloy configuration differs")
    logs = kube("logs", pod["metadata"]["name"], "-n", NAMESPACE, "-c", "alloy", "--since=30m", "--tail=5000", "--limit-bytes=1048576")
    save("attempt-before.json", {"alloy": expected_alloy, "loki": expected_loki,
                                  "old_endpoint_counters": parse_metrics(kube("get", "--raw", METRICS_PATH), HOST),
                                  "alloy_logs": logs})
    print("rollback metadata and diagnostic logs saved privately at " + str(RECOVERY), flush=True)
    print("pre-change transport summary: " + json.dumps(transport_summary(logs)), flush=True)
    if current["spec"]["valuesContent"] != after["spec"]["valuesContent"]:
        patch = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                 {"op": "test", "path": "/spec/valuesContent", "value": before["spec"]["valuesContent"]},
                 {"op": "replace", "path": "/spec/valuesContent", "value": after["spec"]["valuesContent"]}]
        kube("patch", "helmchart", "alloy", "-n", "kube-system", "--type=json", "-p", json.dumps(patch))
        print("helmchart/alloy endpoint made absolute", flush=True)
    print("waiting for Helm and the existing Alloy config reloader", flush=True)
    deadline = time.monotonic() + 900
    next_notice = time.monotonic() + 45
    while config_version(get("configmap", "alloy")) != "after":
        require(time.monotonic() < deadline, "Helm did not apply the expected Alloy configuration within 15 minutes")
        if time.monotonic() >= next_notice:
            print("waiting for the Helm controller's Alloy configuration update", flush=True)
            next_notice = time.monotonic() + 45
        time.sleep(3)
    delivery = verify_delivery(expected_alloy, expected_loki, deployment)
    prefix = "/api/v1/namespaces/monitoring/services/http:loki-gateway:http/proxy/loki/api/v1/"
    info = json.loads(kube("get", "--raw", prefix + "status/buildinfo"))
    require(info.get("version") == "3.7.8", "gateway Loki version changed")
    query = urlencode({"query": '{cluster="openmediavault"}', "start": str(delivery.pop("query_start_ns")),
                       "end": str(time.time_ns()), "limit": "1", "direction": "backward"})
    recent = json.loads(kube("get", "--raw", prefix + "query_range?" + query))
    entries = sum(len(item.get("values", [])) for item in recent.get("data", {}).get("result", []))
    require(recent.get("status") == "success" and recent.get("data", {}).get("resultType") == "streams" and entries > 0,
            "Loki did not return fresh cluster log entries after the endpoint repair")
    latest_claim, latest_volume = binding()
    require(latest_claim["metadata"]["uid"] == claim["metadata"]["uid"] and latest_volume["metadata"]["uid"] == volume["metadata"]["uid"],
            "Loki storage identity changed")
    require(get("helmchart", "alloy", "kube-system")["spec"]["valuesContent"] == after["spec"]["valuesContent"] and
            config_version(get("configmap", "alloy")) == "after", "the repaired configuration changed during verification")
    result = {"result": "passed", "verified_at": datetime.now(timezone.utc).isoformat(), "alloy": expected_alloy,
              "loki": expected_loki, "endpoint": NEW_URL, "stability_seconds": STABILITY_SECONDS, **delivery,
              "recent_log_entries": entries, "original_pvc_uid": claim["metadata"]["uid"], "original_pv_uid": volume["metadata"]["uid"]}
    save("result.json", result)
    print(json.dumps(result, indent=2), flush=True)
    print("Alloy endpoint repair and fresh Loki ingestion verified; monitoring backup setup remains pending", flush=True)


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
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o600, "namespace lock is not private and root-owned")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RepairError("another monitoring operation is running") from error
        repair(arguments.baseline, arguments.manifest)


if __name__ == "__main__":
    try:
        main()
    except RepairError as error:
        print("Alloy repair stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        print("Alloy repair stopped: files, API metadata, or command execution could not be verified", file=sys.stderr)
        sys.exit(1)
