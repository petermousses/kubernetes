#!/usr/bin/env python3
"""Read unmounted integration-state metadata and sample converter resource use."""

from decimal import Decimal
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time


NAMESPACE = "paperless-ngx"
TARGETS = {
    "paperless": ("paperless-5dfc665d56-2tgl6", "a93a81da-fe28-4ab1-98cb-640451249568",
        "ghcr.io/paperless-ngx/paperless-ngx@sha256:a28330462994b937938e02aaa5632a1effcc7f9acfba96c1bef9c50e1167a027"),
    "paperless-ai": ("paperless-ai-6d468d8cbf-lj75p", "4950eace-1e75-4e58-a352-af55299bc84e",
        "clusterzx/paperless-ai@sha256:2b65888163fd59716f1c8285b31c5bd0b30c9c3c192c42b516688e3887d4ba60"),
    "paperless-gpt": ("paperless-gpt-7b768f749d-kz5c4", "5e4f5b13-f1fb-4bfa-8569-84380f137452",
        "icereed/paperless-gpt@sha256:15bad5d455b98f21bb7b5d6615f56871ff67a8bb379dc0dd7ba411f4633071a6"),
    "gotenberg": ("gotenberg-56d68c7b69-v4bw7", "f817236a-6ca0-4ecd-a08d-564335b21cbf",
        "gotenberg/gotenberg@sha256:f9104080d9a7ecab253fb5ebe75100329cf5699c33ec0448f2ea02d885dfde4b"),
}
PATHS = {"paperless-ai": ("/app/data", "/app/OPENAPI"),
         "paperless-gpt": ("/app/prompts", "/app/config", "/app/db")}
ENV_FLAGS = ("PAPERLESS_DBHOST", "PAPERLESS_DBNAME", "PAPERLESS_DBUSER", "PAPERLESS_DBPASS",
             "PAPERLESS_DBPASS_FILE", "PAPERLESS_SECRET_KEY", "PAPERLESS_SECRET_KEY_FILE",
             "PAPERLESS_REDIS", "PAPERLESS_REDIS_FILE", "PAPERLESS_CONFIGURATION_PATH",
             "PAPERLESS_DATA_DIR_FILE", "PAPERLESS_MEDIA_ROOT_FILE", "PAPERLESS_CONSUMPTION_DIR_FILE")
ENV_PATHS = {"PAPERLESS_DATA_DIR": "/usr/src/paperless/data",
             "PAPERLESS_MEDIA_ROOT": "/usr/src/paperless/media",
             "PAPERLESS_CONSUMPTION_DIR": "/usr/src/paperless/consume"}


class InspectionError(Exception):
    pass


def kube(*arguments, optional=False):
    try:
        result = subprocess.run(["k3s", "kubectl", "--request-timeout=15s", *arguments],
                                capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired as error:
        if optional:
            return None
        raise InspectionError("read-only Kubernetes command timed out") from error
    if result.returncode:
        if optional:
            return None
        raise InspectionError("read-only Kubernetes command failed; diagnostics omitted")
    if len(result.stdout) > 1024 * 1024:
        raise InspectionError("read-only command returned excessive output; output omitted")
    return result.stdout


def checked_pod(container):
    name, uid, image = TARGETS[container]
    pod = json.loads(kube("get", "pod", name, "-n", NAMESPACE, "-o", "json"))
    spec, status = pod["spec"], pod.get("status", {})
    definitions = [item for item in spec.get("containers", []) if item["name"] == container]
    states = [item for item in status.get("containerStatuses", []) if item["name"] == container]
    if (pod["metadata"].get("uid") != uid or pod["metadata"].get("deletionTimestamp") or
            spec.get("nodeName") != "openmediavault" or status.get("phase") != "Running" or
            len(definitions) != 1 or definitions[0].get("image") != image or len(states) != 1 or
            states[0].get("ready") is not True or not states[0].get("state", {}).get("running") or
            not states[0].get("containerID") or not states[0].get("imageID")):
        raise InspectionError("writer identity or readiness changed; rerun the live inventory")
    state = states[0]
    return pod, (uid, state["containerID"], state["imageID"], state["restartCount"])


DIRECTORY_CODE = r'''
index=0
for directory do
  index=$((index + 1))
  if [ -L "$directory" ]; then kind=symlink
  elif [ -d "$directory" ]; then kind=directory
  elif [ -f "$directory" ]; then kind=file
  elif [ -e "$directory" ]; then kind=other
  else kind=missing; fi
  printf 'kind\t%s\t%s\n' "$index" "$kind"
  [ "$kind" = directory ] || continue
  metadata=$(stat -c '%u %g %a' "$directory")
  printf 'owner\t%s\t%s\n' "$index" "$metadata"
  entries=0; files=0; directories=0; links=0; other=0; truncated=0
  for entry in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    if [ "$entries" -ge 1000 ]; then truncated=1; break; fi
    entries=$((entries + 1))
    if [ -L "$entry" ]; then links=$((links + 1))
    elif [ -f "$entry" ]; then files=$((files + 1))
    elif [ -d "$entry" ]; then directories=$((directories + 1))
    else other=$((other + 1)); fi
  done
  printf 'counts\t%s\t%s %s %s %s %s %s\n' "$index" "$entries" "$files" "$directories" "$links" "$other" "$truncated"
done
'''


def directory_summary(container, pod):
    paths = PATHS[container]
    output = kube("exec", "-n", NAMESPACE, pod["metadata"]["name"], "-c", container, "--",
                  "sh", "-ec", DIRECTORY_CODE, "inspect-paperless", *paths, optional=True)
    if output is None:
        return {"available": False, "reason": "metadata-only shell inspection unavailable"}
    rows = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] not in {"kind", "owner", "counts"} or not re.fullmatch(r"[1-9][0-9]*", fields[1]):
            raise InspectionError("unexpected directory metadata; output omitted")
        index = int(fields[1])
        key = fields[0], index
        if not 1 <= index <= len(paths) or key in rows:
            raise InspectionError("unexpected or duplicate directory metadata; output omitted")
        rows[key] = fields[2]
    result = []
    definition = next(item for item in pod["spec"]["containers"] if item["name"] == container)
    volumes = {item["name"]: item for item in pod["spec"].get("volumes", [])}
    for index, path in enumerate(paths, 1):
        kind = rows.get(("kind", index))
        if kind not in {"directory", "file", "symlink", "other", "missing"}:
            raise InspectionError("directory metadata is incomplete; output omitted")
        # A parent mount can cover these paths even when there is no exact mount.
        covering = [item for item in definition.get("volumeMounts", []) if
                    path == item["mountPath"] or path.startswith(item["mountPath"].rstrip("/") + "/")]
        mount = max(covering, key=lambda item: len(item["mountPath"]), default=None)
        volume = volumes.get(mount["name"], {}) if mount else {}
        item = {"path": path, "kind": kind,
                "mount_type": "pvc" if "persistentVolumeClaim" in volume else "emptyDir" if "emptyDir" in volume else
                              "other" if mount else "container filesystem",
                "claim": volume.get("persistentVolumeClaim", {}).get("claimName")}
        if kind == "directory":
            owner, counts = rows.get(("owner", index), ""), rows.get(("counts", index), "")
            if not re.fullmatch(r"[0-9]+ [0-9]+ [0-7]{1,4}", owner) or not re.fullmatch(r"[0-9]+(?: [0-9]+){4} [01]", counts):
                raise InspectionError("directory ownership or counts are incomplete; output omitted")
            uid, gid, mode = owner.split()
            entries, files, directories, links, other, truncated = map(int, counts.split())
            if entries > 1000 or entries != files + directories + links + other or (truncated and entries != 1000):
                raise InspectionError("directory counts disagree; output omitted")
            item.update(uid=int(uid), gid=int(gid), mode="0o" + mode,
                        scanned_entries=entries, files=files, directories=directories,
                        symlinks=links, other_entries=other, entries_truncated=bool(truncated))
        elif ("owner", index) in rows or ("counts", index) in rows:
            raise InspectionError("unexpected non-directory metadata; output omitted")
        result.append(item)
    return {"available": True, "directories": result}


PYTHON_CODE = '''
import json, os, sys
flags = ''' + repr(ENV_FLAGS) + '''
paths = ''' + repr(ENV_PATHS) + '''
print(json.dumps({
  "uid": os.geteuid(), "gid": os.getegid(), "python_version": list(sys.version_info[:3]),
  "environment_present": {name: bool(os.environ.get(name)) for name in flags},
  "path_overrides": {name: "unset" if not os.environ.get(name) else "expected mount" if os.environ[name] == path else "custom; value omitted" for name, path in paths.items()},
  "database_engine": os.environ.get("PAPERLESS_DBENGINE", "unset") if os.environ.get("PAPERLESS_DBENGINE", "unset") in ("postgresql", "mariadb", "sqlite", "unset") else "custom; value omitted",
}))
'''


def runtime_summary(pod):
    output = kube("exec", "-n", NAMESPACE, pod["metadata"]["name"], "-c", "paperless", "--",
                  "python3", "-I", "-S", "-B", "-c", PYTHON_CODE, optional=True)
    if output is None:
        return {"available": False, "reason": "isolated Python environment-flag inspection unavailable"}
    value = json.loads(output)
    if (not isinstance(value, dict) or set(value) != {"uid", "gid", "python_version", "environment_present", "path_overrides", "database_engine"} or
            any(type(value.get(key)) is not int or value[key] < 0 for key in ("uid", "gid")) or
            not isinstance(value.get("python_version"), list) or len(value["python_version"]) != 3 or
            any(type(number) is not int or number < 0 for number in value["python_version"]) or
            not isinstance(value.get("environment_present"), dict) or set(value["environment_present"]) != set(ENV_FLAGS) or
            any(type(flag) is not bool for flag in value["environment_present"].values()) or
            not isinstance(value.get("path_overrides"), dict) or set(value["path_overrides"]) != set(ENV_PATHS) or
            any(flag not in {"unset", "expected mount", "custom; value omitted"} for flag in value["path_overrides"].values()) or
            value.get("database_engine") not in {"postgresql", "mariadb", "sqlite", "unset", "custom; value omitted"}):
        raise InspectionError("unexpected runtime flags; output omitted")
    return {"available": True, **value}


def metrics_sample():
    name = TARGETS["gotenberg"][0]
    output = kube("get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/" + NAMESPACE + "/pods/" + name, optional=True)
    if output is None:
        return {"available": False}
    value = json.loads(output)
    containers = [item for item in value.get("containers", []) if item.get("name") == "gotenberg"]
    if (value.get("metadata", {}).get("name") != name or
            value.get("metadata", {}).get("namespace") != NAMESPACE or len(containers) != 1):
        raise InspectionError("converter metrics identity differs")
    usage = containers[0].get("usage", {})
    cpu, memory = usage.get("cpu", ""), usage.get("memory", "")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:n|u|m)?", cpu) or not re.fullmatch(r"[0-9]+(?:Ki|Mi|Gi|Ti|Pi|Ei)?", memory):
        raise InspectionError("converter resource units could not be verified")
    suffix = cpu[-1] if cpu[-1] in "num" else ""
    millicpu = Decimal(cpu[:-1] if suffix else cpu) * {"n": Decimal("0.000001"), "u": Decimal("0.001"), "m": Decimal(1), "": Decimal(1000)}[suffix]
    unit = memory[-2:] if memory[-2:] in {"Ki", "Mi", "Gi", "Ti", "Pi", "Ei"} else ""
    byte_count = int(memory[:-2] if unit else memory) * (1024 ** ({"Ki": 1, "Mi": 2, "Gi": 3, "Ti": 4, "Pi": 5, "Ei": 6}.get(unit, 0)))
    return {"available": True, "cpu_millicores": str(millicpu), "memory_bytes": byte_count}


def main():
    if platform.system() != "Linux" or platform.node().split(".")[0] != "openmediavault" or os.geteuid() != 0:
        raise InspectionError("run with sudo python3 on OMV")
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if not shutil.which("k3s"):
        raise InspectionError("k3s is unavailable")
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    if [node["metadata"]["name"] for node in nodes] != ["openmediavault"]:
        raise InspectionError("expected only the OMV node during migration")
    before = {container: checked_pod(container) for container in TARGETS}
    result = {"paperless_runtime": runtime_summary(before["paperless"][0]),
              "integration_paths": {container: directory_summary(container, before[container][0]) for container in PATHS}}
    samples = []
    for index in range(5):
        samples.append(metrics_sample())
        if index != 4:
            time.sleep(5)
    result["gotenberg_samples"] = samples
    for container, (_, anchor) in before.items():
        if checked_pod(container)[1] != anchor:
            raise InspectionError("a container changed during inspection; rerun the live inventory")
    print(json.dumps(result, indent=2))
    print("read-only runtime inspection complete; no writers stopped, restarted, or reconfigured")


if __name__ == "__main__":
    try:
        main()
    except InspectionError as error:
        print("inspection stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        print("inspection stopped: runtime metadata could not be verified; diagnostics omitted", file=sys.stderr)
        sys.exit(1)
