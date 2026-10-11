import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location(
    "prepare_omv", Path(__file__).with_name("prepare-omv.py")
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.mount_uuid = "019bfa55-c53c-4a1c-b063-46fb90b5d2ac"
        self.share_uuid = "9535a292-11e2-4528-8ae2-e1be17cf1fde"
        self.relative_path = "k3s/backups/longhorn/"
        self.shared = {
            "uuid": self.share_uuid, "name": bootstrap.SHARE_NAME,
            "mntentref": self.mount_uuid, "reldirpath": self.relative_path,
        }

    def test_mount_reference_comes_from_omv_not_the_filesystem_uuid(self):
        records = [
            {"uuid": "another-mount", "dir": "/export/Media", "type": "none"},
            {"uuid": self.mount_uuid, "dir": str(bootstrap.NAS_MOUNT), "type": "btrfs"},
        ]
        self.assertEqual(bootstrap.mount_record(records)["uuid"], self.mount_uuid)
        self.assertIsNone(bootstrap.shared_folder([], self.mount_uuid, self.relative_path))
        with self.assertRaises(RuntimeError):
            bootstrap.mount_record(records + [records[1]])

    def test_matching_shared_folder_is_reused(self):
        self.assertEqual(
            bootstrap.shared_folder([self.shared], self.mount_uuid, self.relative_path),
            self.shared,
        )

    def test_shared_folder_name_or_path_collision_stops(self):
        for changed in (
            dict(self.shared, mntentref="another-mount"),
            dict(self.shared, reldirpath="someone-elses-backups/"),
            dict(self.shared, name="another-share"),
        ):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                bootstrap.shared_folder([changed], self.mount_uuid, self.relative_path)

    def test_unrelated_media_export_is_ignored(self):
        media = {"uuid": "media-export", "sharedfolderref": "media-share", "client": "10.9.20.101"}
        self.assertIsNone(bootstrap.nfs_share([media], self.share_uuid))
        self.assertEqual(media["client"], "10.9.20.101")

    def test_another_backup_client_or_duplicate_export_stops(self):
        record = {"uuid": "export-uuid", "sharedfolderref": self.share_uuid, "client": "10.9.20.101"}
        with self.assertRaises(RuntimeError):
            bootstrap.nfs_share([record], self.share_uuid)
        record["client"] = bootstrap.NODE_IP
        with self.assertRaises(RuntimeError):
            bootstrap.nfs_share([record, record], self.share_uuid)

    def test_export_maps_all_writes_to_a_service_identity_on_one_client(self):
        record = bootstrap.export_params(self.share_uuid, "new-object", 993, 994)
        self.assertEqual(record["client"], "10.9.20.14")
        self.assertEqual(record["options"], "rw")
        options = set(record["extraoptions"].split(","))
        self.assertTrue({"sync", "root_squash", "all_squash", "anonuid=993", "anongid=994"} <= options)
        self.assertNotIn("no_root_squash", options)
        existing = {"uuid": "saved-export", "mntentref": "saved-bind"}
        updated = bootstrap.export_params(self.share_uuid, "new-object", 993, 994, existing)
        self.assertEqual(updated["uuid"], "saved-export")
        self.assertEqual(updated["mntentref"], "saved-bind")

    def test_conflicting_or_misreferenced_bind_mount_stops(self):
        export_path = Path("/export/longhorn-backups")
        backup_path = bootstrap.NAS_MOUNT / self.relative_path
        record = {
            "uuid": "saved-bind", "dir": str(export_path), "type": "none",
            "fsname": str(backup_path),
        }
        bootstrap.check_bind([record], export_path, backup_path)
        for changed, existing in (
            (dict(record, fsname="/other-data"), None),
            (dict(record, type="ext4"), None),
            (record, {"mntentref": "wrong-bind"}),
        ):
            with self.subTest(changed=changed, existing=existing), self.assertRaises(RuntimeError):
                bootstrap.check_bind([changed], export_path, backup_path, existing)

    def test_existing_directory_requires_expected_owner_and_private_permissions(self):
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "backups"
            directory.mkdir(mode=0o700)
            bootstrap.check_backup_directory(directory, account)
            directory.chmod(0o755)
            with self.assertRaises(RuntimeError):
                bootstrap.check_backup_directory(directory, account)
            directory.chmod(0o700)
            with self.assertRaises(RuntimeError):
                bootstrap.check_backup_directory(directory, SimpleNamespace(pw_uid=account.pw_uid + 1, pw_gid=account.pw_gid))
            link = Path(temporary) / "linked-backups"
            link.symlink_to(directory)
            with self.assertRaises(RuntimeError):
                bootstrap.check_backup_directory(link, account)


if __name__ == "__main__":
    unittest.main()
