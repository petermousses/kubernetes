#!/usr/bin/env python3
"""Validate a privately prompted Paperless token and restart only the GPT writer."""

import base64
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import getpass
import hmac
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import warnings


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "paperless_gpt_diagnostic", Path(__file__).with_name("diagnose-paperless-gpt.py"))
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)

NAMESPACE = diagnostic.NAMESPACE
ROOT = Path("/root/k3s-backup-recovery/paperless-ngx")
TOKEN_KEY = "PAPERLESS_API_TOKEN"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
CLAIMS = {"/app/prompts": "paperless-gpt", "/app/config": "paperless-gpt-config", "/app/db": "paperless-gpt-db"}
STABILITY_SECONDS = 60


class RepairError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RepairError(message)


def kube(*arguments, stdin=None):
    try:
        result = subprocess.run(["k3s", "kubectl", "--request-timeout=15s", *arguments],
                                input=stdin, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        raise RepairError("Kubernetes request timed out; inspect the reported attempt flags before retrying") from error
    require(result.returncode == 0, "Kubernetes request failed; command output withheld")
    return result.stdout


def get(kind, name=None, *, namespace=NAMESPACE):
    arguments = ["get", kind, *([name] if name else []), "-o", "json"]
    if namespace:
        arguments += ["-n", namespace]
    return json.loads(kube(*arguments))


def private_directory(path):
    require(path == Path("/root") or Path("/root") in path.parents, "recovery path must remain below /root")
    if path == Path("/root"):
        attrs = path.lstat()
        require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == os.geteuid(), "unsafe root home directory")
        return
    private_directory(path.parent)
    if not os.path.lexists(path):
        path.mkdir(mode=0o700)
    attrs = path.lstat()
    require(stat.S_ISDIR(attrs.st_mode) and attrs.st_uid == os.geteuid() and
            stat.S_IMODE(attrs.st_mode) == 0o700, "recovery directory must be private and root-owned")


@contextmanager
def operation_lock():
    private_directory(ROOT)
    descriptor = os.open(ROOT / ".setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        attrs = os.fstat(descriptor)
        require(stat.S_ISREG(attrs.st_mode) and attrs.st_nlink == 1 and attrs.st_uid == os.geteuid() and
                stat.S_IMODE(attrs.st_mode) == 0o600, "unsafe namespace operation lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RepairError("another Paperless setup or migration is active; retry later") from error
        journal_path = ROOT / "gpt-storage-migration/state.json"
        descriptor_state = os.open(journal_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor_state) as stream:
            attrs = os.fstat(stream.fileno())
            require(stat.S_ISREG(attrs.st_mode) and attrs.st_uid == os.geteuid() and
                    stat.S_IMODE(attrs.st_mode) == 0o600 and attrs.st_size < 4 * 1024**2,
                    "unsafe GPT migration journal")
            state = json.load(stream)
        require(state.get("phase") == "complete" and state.get("authoritative") == "target",
                "GPT storage migration is unfinished; keep its current writer intact")
        yield
    finally:
        os.close(descriptor)


def private_save(directory, name, value):
    descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_before(secret, deployment):
    parent = ROOT / "gpt-token-repair"
    private_directory(parent)
    directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    os.chmod(directory, 0o700)
    private_save(directory, "before.json", {
        "namespace": NAMESPACE, "secret_uid": secret["metadata"]["uid"],
        "previous_token_base64": secret["data"][TOKEN_KEY], "deployment": deployment,
    })
    return directory


def prompt_token():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("Paperless My Profile API token (hidden; paste the token alone): ")
        token = value.strip().encode("ascii")
    except (getpass.GetPassWarning, EOFError, UnicodeEncodeError) as error:
        raise RepairError("a hidden terminal prompt and an ASCII API token are required") from error
    require(diagnostic.header_safe_token(token), "token is empty or contains spaces/control bytes; paste the token alone")
    require(not diagnostic.token_format(token)["recognized_placeholder"], "a placeholder cannot replace the API token")
    return token


def secret_patch(secret, token):
    require(TOKEN_KEY in secret.get("data", {}), "expected Paperless token Secret field is missing")
    return [
        {"op": "test", "path": "/metadata/uid", "value": secret["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": secret["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/data/" + TOKEN_KEY, "value": secret["data"][TOKEN_KEY]},
        {"op": "replace", "path": "/data/" + TOKEN_KEY, "value": base64.b64encode(token).decode("ascii")},
    ]


def restart_patch(deployment):
    template = copy.deepcopy(deployment["spec"]["template"])
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    annotations[RESTART_ANNOTATION] = datetime.now(timezone.utc).isoformat()
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": deployment["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
        {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations},
    ]
    return operations, template


def writer_guard(deployment):
    spec = deployment.get("spec", {})
    require(not deployment["metadata"].get("ownerReferences") and not spec.get("paused") and
            spec.get("replicas") == 1 and spec.get("strategy", {}).get("type") == "Recreate",
            "expected one unmanaged GPT writer with Recreate strategy")
    containers = [item for item in spec["template"]["spec"]["containers"] if item.get("name") == "paperless-gpt"]
    require(len(containers) == 1 and containers[0].get("image", "").endswith(diagnostic.IMAGE_DIGEST),
            "GPT deployment image differs from the inventoried version")
    require(any(item.get("secretRef", {}).get("name") == "paperless-env" for item in containers[0].get("envFrom", [])) and
            not any(item.get("name") == TOKEN_KEY for item in containers[0].get("env", [])),
            "GPT token source has an explicit override or differs from paperless-env")
    for autoscaler in get("hpa")["items"]:
        target = autoscaler.get("spec", {}).get("scaleTargetRef", {})
        require(not (target.get("kind") == "Deployment" and target.get("name") == "paperless-gpt"),
                "GPT has an autoscaler; writer restart requires a separate review")
    for item in get("deployments")["items"]:
        for metadata in (item.get("metadata", {}), item.get("spec", {}).get("template", {}).get("metadata", {})):
            require(not any("reloader.stakater.com" in key and str(value).lower() not in ("", "false")
                            for key, value in metadata.get("annotations", {}).items()),
                    "Secret reload automation is present; preserve the existing AI Pod before changing credentials")


def bindings(deployment):
    pod_spec = deployment["spec"]["template"]["spec"]
    volumes = {item["name"]: item.get("persistentVolumeClaim", {}).get("claimName") for item in pod_spec["volumes"]}
    container = next(item for item in pod_spec["containers"] if item["name"] == "paperless-gpt")
    for mount_path, claim in CLAIMS.items():
        require(any(item.get("mountPath") == mount_path and volumes.get(item.get("name")) == claim and
                    not item.get("readOnly") and not item.get("subPath") and not item.get("subPathExpr")
                    for item in container["volumeMounts"]), "GPT retained state mount differs from the completed cutover")
    result = {}
    for claim_name in CLAIMS.values():
        claim = get("pvc", claim_name)
        volume = get("pv", claim["spec"]["volumeName"], namespace=None)
        reference = volume["spec"].get("claimRef", {})
        require(claim.get("status", {}).get("phase") == "Bound" and
                reference.get("uid") == claim["metadata"]["uid"] and reference.get("namespace") == NAMESPACE and
                volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "GPT claim binding or retention differs")
        result[claim_name] = {"pvc_uid": claim["metadata"]["uid"], "pv_uid": volume["metadata"]["uid"],
                              "volume_name": volume["metadata"]["name"]}
    return result


def ai_identity():
    pods = json.loads(diagnostic.kube("get", "pods", "-n", NAMESPACE, "-l",
                                    "app.kubernetes.io/name=paperless-ngx,app.kubernetes.io/component=ai", "-o", "json"))
    return {item["metadata"]["uid"]: {state["name"]: state.get("restartCount", 0)
                                     for state in item.get("status", {}).get("containerStatuses", [])}
            for item in pods["items"]}


def owned_pod(deployment):
    pod, container = diagnostic.ready_pod()
    owners = [item for item in pod["metadata"].get("ownerReferences", []) if item.get("controller")]
    require(len(owners) == 1 and owners[0].get("kind") == "ReplicaSet", "ready GPT Pod ownership differs")
    replica_set = get("replicaset", owners[0]["name"])
    require(replica_set["metadata"]["uid"] == owners[0]["uid"] and
            any(item.get("controller") and item.get("kind") == "Deployment" and
                item.get("uid") == deployment["metadata"]["uid"]
                for item in replica_set["metadata"].get("ownerReferences", [])), "ready GPT Pod belongs to another controller")
    require(pod["spec"].get("nodeName") == "openmediavault", "GPT writer is not on OMV")
    return pod, container


def wait_writer(deployment, template, old_uid):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        current = get("deployment", "paperless-gpt")
        require(current["metadata"]["uid"] == deployment["metadata"]["uid"] and current["spec"].get("replicas") == 1 and
                current["spec"]["template"] == template, "GPT deployment changed while its credential was loading")
        status = current.get("status", {})
        if (status.get("observedGeneration", 0) >= current["metadata"].get("generation", 1) and
                all(status.get(key, 0) == 1 for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
            try:
                pod, container = owned_pod(current)
            except diagnostic.DiagnosticError:
                pass
            else:
                if pod["metadata"]["uid"] != old_uid:
                    require(container.get("restartCount", 0) == 0, "replacement GPT container restarted")
                    return pod, container
        time.sleep(2)
    raise RepairError("GPT did not finish its rollout; the validated Secret is retained for retry")


def repair(token, progress):
    progress["phase"] = "preflight"
    require(diagnostic.header_safe_token(token), "replacement token has invalid header bytes")
    deployment = get("deployment", "paperless-gpt")
    secret = get("secret", "paperless-env")
    require(TOKEN_KEY in secret.get("data", {}), "expected Paperless token Secret field is missing")
    require(not secret.get("immutable") and not secret["metadata"].get("ownerReferences"),
            "Paperless Secret is immutable or controller-owned; update its credential source instead")
    writer_guard(deployment)
    original_bindings = bindings(deployment)
    original_ai = ai_identity()
    require(len(original_ai) == 1, "expected one existing AI Pod to preserve")
    old_pod, _ = owned_pod(deployment)
    progress["phase"] = "validating replacement token"
    tag = diagnostic.manual_tag()
    authenticated = diagnostic.port_forward("service/paperless", 8000,
                                           lambda port: diagnostic.paperless_api_probe(port, token, tag))
    progress["replacement_token_accepted"] = authenticated.get("token_accepted") is True
    require(progress["replacement_token_accepted"], "replacement token did not pass Paperless tag/document API checks")
    directory = save_before(secret, deployment)
    progress["recovery_directory"] = str(directory)
    expected_data = dict(secret["data"], **{TOKEN_KEY: base64.b64encode(token).decode("ascii")})
    progress["phase"] = "updating one Secret field"
    if secret["data"] != expected_data:
        progress["secret_update_attempted"] = True
        kube("-n", NAMESPACE, "patch", "secret", "paperless-env", "--type=json", "--patch-file=/dev/stdin",
             stdin=json.dumps(secret_patch(secret, token)))
    updated = get("secret", "paperless-env")
    require(updated["metadata"]["uid"] == secret["metadata"]["uid"] and updated["data"] == expected_data,
            "Secret identity or unrelated fields changed; retain the private recovery copy")
    progress["secret_update_verified"] = True
    loaded = diagnostic.process_token(old_pod)
    template = deployment["spec"]["template"]
    old_uid = None
    if loaded is None or not hmac.compare_digest(loaded, token):
        progress["phase"] = "requesting GPT restart"
        current = get("deployment", "paperless-gpt")
        require(current["metadata"]["uid"] == deployment["metadata"]["uid"] and current["spec"] == deployment["spec"],
                "GPT workload changed; validated Secret retained but restart was withheld")
        operations, template = restart_patch(current)
        progress["gpt_restart_attempted"] = True
        kube("-n", NAMESPACE, "patch", "deployment", "paperless-gpt", "--type=json", "--patch-file=/dev/stdin",
             stdin=json.dumps(operations))
        old_uid = old_pod["metadata"]["uid"]
    progress["phase"] = "waiting for GPT readiness"
    print("validated token saved; waiting for GPT readiness", flush=True)
    pod, container = wait_writer(deployment, template, old_uid)
    loaded = diagnostic.process_token(pod)
    require(loaded is not None and hmac.compare_digest(loaded, token), "ready GPT process did not load the validated token")
    progress["phase"] = "checking GPT documents API"
    api = diagnostic.port_forward("pod/" + pod["metadata"]["name"], diagnostic.PORT, diagnostic.http_probe)
    progress["documents_api"] = api
    require(api.get("http_status") == 200 and api.get("json_type") == "array", "GPT documents API still fails after token repair")
    print("GPT documents API passed; observing readiness and restarts for one minute", flush=True)
    progress["phase"] = "observing GPT stability"
    deadline = time.monotonic() + STABILITY_SECONDS
    while time.monotonic() < deadline:
        current = get("deployment", "paperless-gpt")
        require(current["metadata"]["uid"] == deployment["metadata"]["uid"] and current["spec"]["template"] == template and
                current["spec"].get("replicas") == 1, "GPT workload changed during observation")
        live_pod, live_container = owned_pod(current)
        require(live_pod["metadata"]["uid"] == pod["metadata"]["uid"] and live_container.get("restartCount", 0) == 0,
                "GPT Pod changed or restarted during observation")
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    progress["phase"] = "final verification"
    current = get("deployment", "paperless-gpt")
    require(current["metadata"]["uid"] == deployment["metadata"]["uid"] and current["spec"]["template"] == template and
            current["spec"].get("replicas") == 1, "GPT workload changed before final verification")
    require(bindings(current) == original_bindings, "GPT storage bindings changed")
    require(ai_identity() == original_ai, "AI Pod identity or restart count changed during repair")
    final_secret = get("secret", "paperless-env")
    require(final_secret["metadata"]["uid"] == secret["metadata"]["uid"] and final_secret["data"] == expected_data,
            "Paperless Secret changed during observation")
    api = diagnostic.port_forward("pod/" + pod["metadata"]["name"], diagnostic.PORT, diagnostic.http_probe)
    progress["documents_api"] = api
    require(api.get("http_status") == 200 and api.get("json_type") == "array", "GPT documents API failed after observation")
    result = dict(progress, result="passed", phase="complete", namespace=NAMESPACE, component="paperless-gpt",
                  completed_at=datetime.now(timezone.utc).isoformat(), pod_uid=pod["metadata"]["uid"],
                  image_id=container["imageID"], restarts=0, claims=list(CLAIMS.values()),
                  stability_seconds=STABILITY_SECONDS, ai_pod_unchanged=True, storage_bindings_unchanged=True)
    private_save(directory, "result.json", result)
    progress.update(result)
    return progress


def main():
    progress = {"result": "stopped", "phase": "starting", "secret_update_attempted": False,
                "secret_update_verified": False, "gpt_restart_attempted": False, "credentials_printed": False}
    try:
        require(os.geteuid() == 0, "run this repair with sudo on OMV")
        progress["phase"] = "hidden API token prompt"
        token = prompt_token()
        progress["phase"] = "checking namespace lock and completed migration"
        with operation_lock():
            repair(token, progress)
    except (RepairError, diagnostic.DiagnosticError) as error:
        progress.update(result="stopped", reason=str(error))
    except KeyboardInterrupt:
        progress.update(result="stopped", reason="interrupted; any validated Secret update is retained for retry")
    except Exception:
        progress.update(result="stopped", reason="filesystem, API, runtime, or JSON check failed; private details withheld")
    print(json.dumps(progress, indent=2), flush=True)
    return 0 if progress["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
