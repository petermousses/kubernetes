"""Exercise token repair against synthetic Kubernetes state, without cluster access."""

import base64
from contextlib import ExitStack, nullcontext, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "repair_paperless_gpt_token", Path(__file__).with_name("repair-paperless-gpt-token.py"))
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)
TOKEN = b"synthetic-valid-api-token"
OLD_TOKEN = b"synthetic invalid token"


def encoded(value):
    return base64.b64encode(value).decode()


class Cluster:
    def __init__(self):
        self.secret = {"metadata": {"uid": "secret-uid", "resourceVersion": "10"}, "data": {
            repair.TOKEN_KEY: encoded(OLD_TOKEN), "POSTGRES_PASSWORD": encoded(b"synthetic-db-password"),
            "PAPERLESS_SECRET_KEY": encoded(b"synthetic-signing-key")}}
        self.deployment = {"metadata": {"uid": "deployment-uid", "resourceVersion": "20", "generation": 1}, "spec": {
            "replicas": 1, "strategy": {"type": "Recreate"}, "template": {
                "metadata": {"annotations": {"existing-annotation": "preserve"}}, "spec": {
                    "containers": [{"name": "paperless-gpt", "image": "synthetic@" + repair.diagnostic.IMAGE_DIGEST,
                                    "envFrom": [{"secretRef": {"name": "paperless-env"}}],
                                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
                                    "securityContext": {"runAsUser": 0},
                                    "volumeMounts": [{"name": claim, "mountPath": path}
                                                     for path, claim in repair.CLAIMS.items()]}],
                    "volumes": [{"name": claim, "persistentVolumeClaim": {"claimName": claim}}
                                for claim in repair.CLAIMS.values()]}}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1,
                       "readyReplicas": 1, "availableReplicas": 1}}
        self.pod_uid = "old-pod"
        self.loaded_token = OLD_TOKEN
        self.events = []
        self.conflict = False
        self.unrelated_secret_mutation = False
        self.deployment_drift = False
        self.api_accepts = True
        self.proofs = []
        self.before = None

    def get(self, kind, name=None, **_kwargs):
        if kind == "deployment":
            return copy.deepcopy(self.deployment)
        if kind == "secret":
            return copy.deepcopy(self.secret)
        if kind == "hpa":
            return {"items": []}
        if kind == "deployments":
            return {"items": [copy.deepcopy(self.deployment)]}
        if kind == "pvc":
            return {"metadata": {"uid": "pvc-" + name}, "spec": {"volumeName": name}, "status": {"phase": "Bound"}}
        if kind == "pv":
            return {"metadata": {"uid": "pv-" + name, "name": name}, "spec": {
                "persistentVolumeReclaimPolicy": "Retain", "claimRef": {"uid": "pvc-" + name, "namespace": repair.NAMESPACE}}}
        raise AssertionError("unexpected synthetic Kubernetes read")

    def kube(self, *args, stdin=None):
        if args[:3] != ("-n", repair.NAMESPACE, "patch"):
            raise AssertionError("unexpected synthetic Kubernetes write")
        kind, name = args[3:5]
        if (kind, name) not in (("secret", "paperless-env"), ("deployment", "paperless-gpt")):
            raise AssertionError("write targeted another resource")
        if "--patch-file=/dev/stdin" not in args:
            raise AssertionError("credential patch was not passed on stdin")
        target = self.secret if kind == "secret" else self.deployment
        if kind == "secret" and self.conflict:
            target["metadata"]["resourceVersion"] = "11"
        updated = copy.deepcopy(target)
        operations = json.loads(stdin)
        for operation in operations:
            fields = operation["path"].strip("/").split("/")
            parent = updated
            for field in fields[:-1]:
                parent = parent[field]
            key = fields[-1]
            if operation["op"] == "test":
                if parent.get(key) != operation["value"]:
                    raise repair.RepairError("synthetic resource-version conflict")
            else:
                parent[key] = operation["value"]
        self.events.append(kind)
        if kind == "secret":
            if self.unrelated_secret_mutation:
                updated["data"]["POSTGRES_PASSWORD"] = encoded(b"synthetic-concurrent-db-change")
            self.secret = updated
            if self.deployment_drift:
                self.deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsUser"] = 123
        else:
            self.deployment = updated
            self.pod_uid = "new-pod"
            self.loaded_token = base64.b64decode(self.secret["data"][repair.TOKEN_KEY])
        return "patched"

    def owned_pod(self, _deployment):
        return ({"metadata": {"uid": self.pod_uid, "name": self.pod_uid}},
                {"imageID": "synthetic@" + repair.diagnostic.IMAGE_DIGEST, "restartCount": 0})

    def authenticate(self, _port, token, _tag):
        self.events.append("authenticate")
        return {"token_accepted": self.api_accepts and token == TOKEN}

    def save_before(self, secret, deployment):
        self.before = copy.deepcopy((secret, deployment))
        return Path("/synthetic-private-recovery")

    def progress(self):
        return {"result": "stopped", "secret_update_attempted": False, "secret_update_verified": False,
                "gpt_restart_attempted": False, "credentials_printed": False}

    def patches(self):
        stack = ExitStack()
        for name, value in (("get", self.get), ("kube", self.kube), ("owned_pod", self.owned_pod),
                            ("save_before", self.save_before), ("STABILITY_SECONDS", 0),
                            ("ai_identity", lambda: {"existing-ai-uid": {"paperless-ai": 0}}),
                            ("private_save", lambda _directory, _name, value: self.proofs.append(copy.deepcopy(value)))):
            stack.enter_context(patch.object(repair, name, value))
        for name, value in (("manual_tag", lambda: "synthetic-private-tag"), ("paperless_api_probe", self.authenticate),
                            ("process_token", lambda _pod: self.loaded_token),
                            ("port_forward", lambda _resource, _port, callback: callback(1)),
                            ("http_probe", lambda _port: {"http_status": 200, "json_type": "array", "item_count": 0})):
            stack.enter_context(patch.object(repair.diagnostic, name, value))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack


class RepairChecks(unittest.TestCase):
    def test_validates_before_replacing_one_field_and_restarting_gpt(self):
        cluster = Cluster()
        original = copy.deepcopy(cluster.secret)
        original_template = copy.deepcopy(cluster.deployment["spec"]["template"])
        with cluster.patches():
            result = repair.repair(TOKEN, cluster.progress())
        self.assertEqual(cluster.events, ["authenticate", "secret", "deployment"])
        expected = copy.deepcopy(original["data"])
        expected[repair.TOKEN_KEY] = encoded(TOKEN)
        self.assertEqual(cluster.secret["data"], expected)
        restored_template = copy.deepcopy(cluster.deployment["spec"]["template"])
        del restored_template["metadata"]["annotations"][repair.RESTART_ANNOTATION]
        self.assertEqual(restored_template, original_template)
        self.assertEqual(cluster.before[0], original)
        self.assertEqual(result["result"], "passed")
        self.assertTrue(result["storage_bindings_unchanged"])
        self.assertTrue(result["ai_pod_unchanged"])
        self.assertNotIn(TOKEN.decode(), json.dumps(result))
        self.assertNotIn(OLD_TOKEN.decode(), json.dumps(result))
        self.assertNotIn("synthetic-db-password", json.dumps(result))

    def test_failed_authentication_prevents_all_writes(self):
        cluster = Cluster()
        cluster.api_accepts = False
        with cluster.patches(), self.assertRaises(repair.RepairError):
            repair.repair(TOKEN, cluster.progress())
        self.assertEqual(cluster.events, ["authenticate"])
        self.assertIsNone(cluster.before)

    def test_resource_version_conflict_prevents_restart(self):
        cluster = Cluster()
        cluster.conflict = True
        progress = cluster.progress()
        with cluster.patches(), self.assertRaises(repair.RepairError):
            repair.repair(TOKEN, progress)
        self.assertEqual(cluster.events, ["authenticate"])
        self.assertEqual(cluster.secret["data"][repair.TOKEN_KEY], encoded(OLD_TOKEN))
        self.assertTrue(progress["secret_update_attempted"])
        self.assertFalse(progress["secret_update_verified"])
        self.assertFalse(progress["gpt_restart_attempted"])

    def test_unrelated_secret_mutation_prevents_restart(self):
        cluster = Cluster()
        cluster.unrelated_secret_mutation = True
        with cluster.patches(), self.assertRaises(repair.RepairError):
            repair.repair(TOKEN, cluster.progress())
        self.assertEqual(cluster.events, ["authenticate", "secret"])

    def test_concurrent_workload_change_is_not_overwritten(self):
        cluster = Cluster()
        cluster.deployment_drift = True
        progress = cluster.progress()
        with cluster.patches(), self.assertRaises(repair.RepairError):
            repair.repair(TOKEN, progress)
        self.assertEqual(cluster.events, ["authenticate", "secret"])
        self.assertTrue(progress["secret_update_verified"])
        self.assertFalse(progress["gpt_restart_attempted"])
        self.assertEqual(cluster.deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsUser"], 123)

    def test_retry_after_secret_update_finishes_restart(self):
        cluster = Cluster()
        cluster.secret["data"][repair.TOKEN_KEY] = encoded(TOKEN)
        with cluster.patches():
            result = repair.repair(TOKEN, cluster.progress())
        self.assertEqual(cluster.events, ["authenticate", "deployment"])
        self.assertEqual(result["result"], "passed")
        self.assertFalse(result["secret_update_attempted"])

    def test_already_loaded_token_does_not_restart_again(self):
        cluster = Cluster()
        cluster.secret["data"][repair.TOKEN_KEY] = encoded(TOKEN)
        cluster.loaded_token = TOKEN
        with cluster.patches():
            result = repair.repair(TOKEN, cluster.progress())
        self.assertEqual(cluster.events, ["authenticate"])
        self.assertEqual(result["result"], "passed")
        self.assertFalse(result["gpt_restart_attempted"])

    def test_receipt_failure_cannot_report_passed(self):
        cluster = Cluster()
        progress = cluster.progress()
        with cluster.patches(), patch.object(repair, "private_save", side_effect=OSError("synthetic failure")), \
                self.assertRaises(OSError):
            repair.repair(TOKEN, progress)
        self.assertEqual(progress["result"], "stopped")

    def test_invalid_header_is_rejected_before_cluster_access(self):
        with patch.object(repair, "get") as get:
            for token in (b"synthetic invalid token", b"synthetic\n", b"", b"\xff"):
                with self.subTest(token=token), self.assertRaises(repair.RepairError):
                    repair.repair(token, {})
            get.assert_not_called()

    def test_wrong_persistent_mount_prevents_all_writes(self):
        cluster = Cluster()
        mounts = cluster.deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        mounts[1]["name"] = "unknown-config-volume"
        with cluster.patches(), self.assertRaises(repair.RepairError):
            repair.repair(TOKEN, cluster.progress())
        self.assertEqual(cluster.events, [])

    def test_prompt_refuses_echo_fallback_and_internal_spaces(self):
        for value in ("synthetic invalid token", "Token synthetic-token", "", "synthetic-\u2603"):
            with self.subTest(value=value), patch.object(repair.getpass, "getpass", return_value=value), \
                    self.assertRaises(repair.RepairError):
                repair.prompt_token()
        with patch.object(repair.getpass, "getpass", side_effect=repair.getpass.GetPassWarning("synthetic echo failure")), \
                self.assertRaises(repair.RepairError):
            repair.prompt_token()

    def test_kubernetes_patch_keeps_credentials_off_argv_and_error_output(self):
        payload = json.dumps({"synthetic": TOKEN.decode()})
        response = SimpleNamespace(returncode=1, stdout=TOKEN.decode(), stderr=TOKEN.decode())
        with patch.object(repair.subprocess, "run", return_value=response) as run, \
                self.assertRaises(repair.RepairError) as error:
            repair.kube("patch", "secret", "paperless-env", "--patch-file=/dev/stdin", stdin=payload)
        self.assertEqual(run.call_args.kwargs["input"], payload)
        self.assertNotIn(TOKEN.decode(), str(run.call_args.args[0]))
        self.assertNotIn(TOKEN.decode(), str(error.exception))

    def test_private_before_file_contains_old_token_only(self):
        cluster = Cluster()
        with tempfile.TemporaryDirectory() as temporary, patch.object(repair, "ROOT", Path(temporary)), \
                patch.object(repair, "private_directory", side_effect=lambda path: path.mkdir(mode=0o700, exist_ok=True)):
            directory = repair.save_before(cluster.secret, cluster.deployment)
            saved = directory / "before.json"
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            data = json.loads(saved.read_text())
            self.assertEqual(data["previous_token_base64"], encoded(OLD_TOKEN))
            self.assertNotIn(encoded(TOKEN), saved.read_text())

    def test_unexpected_exception_never_prints_credentials(self):
        output = io.StringIO()
        with patch.object(repair.os, "geteuid", return_value=0), patch.object(repair, "prompt_token", return_value=TOKEN), \
                patch.object(repair, "operation_lock", return_value=nullcontext()), \
                patch.object(repair, "repair", side_effect=RuntimeError(TOKEN.decode())), redirect_stdout(output):
            self.assertEqual(repair.main(), 1)
        self.assertNotIn(TOKEN.decode(), output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["result"], "stopped")


if __name__ == "__main__":
    unittest.main()
