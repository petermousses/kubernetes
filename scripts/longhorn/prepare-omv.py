#!/usr/bin/python3
"""Prepare the current OMV node and its managed NFS backup export for Longhorn."""

from datetime import datetime, timezone
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid


NODE = "openmediavault"
NODE_IP = "10.9.20.14"
NAS_MOUNT = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4")
BACKUP_PARENT = Path("/filesystem/k3s/backups")
SHARE_NAME = "longhorn-backups"
BACKUP_USER = "longhorn-backup"
DATA_PATH = Path("/var/lib/longhorn")
OMV_CONFIG = Path("/etc/openmediavault/config.xml")
CONFIG_BACKUPS = Path("/root/longhorn-migration")
TARGET_URL = f"nfs://{NODE_IP}:/{SHARE_NAME}?nfsOptions=vers=4.1"


def run(*args, capture=False, timeout=None, env=None):
    result = subprocess.run(
        args, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout, env=env,
    )
    return result.stdout.strip() if capture else None


def read_config(model):
    return json.loads(run("omv-confdbadm", "read", model, capture=True))


def rpc(service, method, params):
    return json.loads(run(
        "omv-rpc", "-u", "admin", service, method, json.dumps(params), capture=True,
    ))


def unique(records, predicate, description):
    matches = [record for record in records if predicate(record)]
    if len(matches) > 1:
        raise RuntimeError(f"multiple {description}; refusing to guess")
    return matches[0] if matches else None


def mount_record(records):
    match = unique(
        records,
        lambda r: Path(r["dir"]).resolve() == NAS_MOUNT and r["type"] == "btrfs",
        "OMV mount records for the RAID filesystem",
    )
    if match is None:
        raise RuntimeError("the expected RAID filesystem is not registered in OMV")
    return match


def shared_folder(records, mount_uuid, relative_path):
    match = unique(records, lambda r: r["name"] == SHARE_NAME, "Longhorn shared folders")
    for record in records:
        same_path = (
            record["mntentref"] == mount_uuid
            and record["reldirpath"].strip("/") == relative_path.strip("/")
        )
        if same_path and record["name"] != SHARE_NAME:
            raise RuntimeError("another OMV shared folder already uses the backup path")
    if match is not None and (
        match["mntentref"] != mount_uuid
        or match["reldirpath"].strip("/") != relative_path.strip("/")
    ):
        raise RuntimeError("an existing longhorn-backups shared folder uses another path")
    return match


def nfs_share(records, share_uuid):
    matches = [r for r in records if r["sharedfolderref"] == share_uuid]
    if any(r["client"] != NODE_IP for r in matches):
        raise RuntimeError("the backup folder already has an export to another client")
    return unique(matches, lambda r: r["client"] == NODE_IP, "Longhorn NFS exports")


def export_params(share_uuid, new_uuid, uid, gid, existing=None):
    return {
        "uuid": existing["uuid"] if existing else new_uuid,
        "sharedfolderref": share_uuid,
        "mntentref": existing["mntentref"] if existing else new_uuid,
        "client": NODE_IP,
        "options": "rw",
        "extraoptions": (
            f"sync,no_subtree_check,root_squash,all_squash,anonuid={uid},anongid={gid},insecure"
        ),
        "comment": "Longhorn backups; current migration node only",
    }


def check_bind(records, export_path, backup_path, existing=None):
    bind = unique(records, lambda r: Path(r["dir"]) == export_path, "NFS backup bind mounts")
    if bind is not None and (
        bind["type"] != "none" or Path(bind["fsname"]).resolve() != backup_path
    ):
        raise RuntimeError("the NFS export path already has a bind to another directory")
    if existing is not None and (bind is None or bind["uuid"] != existing["mntentref"]):
        raise RuntimeError("the existing NFS export references an unexpected mount")


def filesystem(path):
    result = json.loads(run(
        "findmnt", "--json", "--target", str(path),
        "--output", "TARGET,SOURCE,FSTYPE", capture=True,
    ))
    return result["filesystems"][0]


def backup_account():
    try:
        account = pwd.getpwnam(BACKUP_USER)
    except KeyError:
        try:
            grp.getgrnam(BACKUP_USER)
        except KeyError:
            pass
        else:
            raise RuntimeError("a longhorn-backup group exists without its service account")
        run(
            "useradd", "--system", "--user-group", "--no-create-home",
            "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", BACKUP_USER,
        )
        account = pwd.getpwnam(BACKUP_USER)
    if (
        account.pw_dir != "/nonexistent" or account.pw_shell != "/usr/sbin/nologin"
        or account.pw_gid != grp.getgrnam(BACKUP_USER).gr_gid
        or account.pw_uid == 0 or account.pw_gid == 0
    ):
        raise RuntimeError("the existing longhorn-backup account has unexpected settings")
    return account


def check_backup_directory(path, account):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"backup path must be a real directory: {path}")
    info = path.stat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
        account.pw_uid, account.pw_gid, 0o700,
    ):
        raise RuntimeError(f"preserving existing backup directory with unexpected ownership/mode: {path}")


def verify_nfs(backup_path, uid):
    probe = Path(tempfile.mkdtemp(prefix="longhorn-nfs-probe-", dir="/run"))
    filename = ".longhorn-write-probe-" + uuid.uuid4().hex
    token = uuid.uuid4().hex.encode()
    probe_file = probe / filename
    created = False
    try:
        run(
            "mount", "-t", "nfs", "-o",
            "vers=4.1,soft,timeo=50,retrans=2,nosuid,nodev,noexec",
            f"{NODE_IP}:/{SHARE_NAME}", str(probe), timeout=60,
        )
        with probe_file.open("xb") as stream:
            created = True
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        server_file = backup_path / filename
        if probe_file.read_bytes() != token or server_file.read_bytes() != token:
            raise RuntimeError("NFS write/read verification failed")
        if server_file.stat().st_uid != uid:
            raise RuntimeError("NFS did not map the root write to the backup service account")
        probe_file.unlink()
        created = False
        if server_file.exists():
            raise RuntimeError("NFS delete verification failed")
        print("PASS: NFSv4.1 write, fsync, read, delete, and identity squashing", flush=True)
    finally:
        try:
            if created and os.path.ismount(probe):
                probe_file.unlink(missing_ok=True)
        finally:
            if os.path.ismount(probe):
                run("umount", str(probe), timeout=60)
            # Never recursively remove a directory that could still be mounted.
            probe.rmdir()


def main():
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    os.umask(0o077)
    if os.geteuid() != 0 or socket.gethostname().split(".")[0] != NODE:
        raise RuntimeError("run this script with sudo on openmediavault")
    import openmediavault  # Installed by OMV; use /usr/bin/python3.

    version = run("dpkg-query", "-W", "-f=${Version}", "openmediavault", capture=True)
    if version.split(".")[0] not in {"7", "8"}:
        raise RuntimeError(f"unreviewed OMV version: {version}")
    schema = json.loads(Path("/usr/share/openmediavault/datamodels/rpc.nfs.json").read_text())
    model = next(m for m in schema if m["id"] == "rpc.nfs.setshare")
    required = {k for k, v in model["params"]["properties"].items() if v.get("required")}
    if not required <= export_params("", "", 1, 1).keys():
        raise RuntimeError("the installed OMV NFS API requires unreviewed fields")
    new_uuid = openmediavault.getenv("OMV_CONFIGOBJECT_NEW_UUID")
    uuid.UUID(new_uuid)
    export_root = Path(openmediavault.getenv("OMV_NFSD_EXPORT_DIR", "/export"))

    nodes = json.loads(run("k3s", "kubectl", "get", "nodes", "-o", "json", capture=True))["items"]
    if len(nodes) != 1 or nodes[0]["metadata"]["name"] != NODE:
        raise RuntimeError("the approved migration posture requires only the OMV cluster node")
    if NODE_IP not in [a["address"] for a in nodes[0]["status"]["addresses"] if a["type"] == "InternalIP"]:
        raise RuntimeError("the cluster node has an unexpected internal IP")
    if (
        DATA_PATH.is_symlink() or (DATA_PATH.exists() and not DATA_PATH.is_dir())
        or filesystem(DATA_PATH if DATA_PATH.exists() else DATA_PATH.parent)["fstype"] != "ext4"
    ):
        raise RuntimeError("the Longhorn data path must be on the current ext4 disk")
    if DATA_PATH.exists() and (DATA_PATH.stat().st_uid != 0 or DATA_PATH.stat().st_mode & 0o022):
        raise RuntimeError("the existing Longhorn data directory has unsafe ownership/permissions")
    backup_parent = BACKUP_PARENT.resolve(strict=True)
    nas_fs = filesystem(backup_parent)
    if nas_fs["fstype"] != "btrfs" or Path(nas_fs["target"]).resolve() != NAS_MOUNT:
        raise RuntimeError("the NAS backup parent is not on the expected mounted RAID filesystem")
    backup_path = backup_parent / "longhorn"
    relative_path = backup_path.relative_to(NAS_MOUNT).as_posix() + "/"
    mounts = read_config("conf.system.filesystem.mountpoint")
    mount = mount_record(mounts)
    shared = shared_folder(read_config("conf.system.sharedfolder"), mount["uuid"], relative_path)
    existing = nfs_share(read_config("conf.service.nfs.share"), shared["uuid"]) if shared else None
    check_bind(mounts, export_root / SHARE_NAME, backup_path, existing)
    settings = read_config("conf.service.nfs")
    if not settings["enable"] or "4.1" not in settings["versions"].split(","):
        raise RuntimeError("OMV must already have NFS and NFSv4.1 enabled")
    if str(openmediavault.getenv("OMV_NFSD_V4_PSEUDO_ROOT_ENABLED", "yes")).lower() not in {"yes", "true", "1"}:
        raise RuntimeError("OMV's NFSv4 pseudo root is disabled")

    print(f"preflight passed: OMV {version}; ext4 Longhorn data; RAID backups", flush=True)
    CONFIG_BACKUPS.mkdir(mode=0o700, exist_ok=True)
    if (
        CONFIG_BACKUPS.is_symlink() or CONFIG_BACKUPS.stat().st_uid != 0
        or stat.S_IMODE(CONFIG_BACKUPS.stat().st_mode) != 0o700
    ):
        raise RuntimeError("the configuration backup directory must be private and not a symlink")
    saved_config = CONFIG_BACKUPS / (datetime.now(timezone.utc).strftime("config-%Y%m%dT%H%M%S.%fZ.xml"))
    with OMV_CONFIG.open("rb") as source, saved_config.open("xb") as destination:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    print(f"OMV configuration backup: {saved_config} (contains secrets; do not paste)", flush=True)

    print("installing iSCSI prerequisites", flush=True)
    environment = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")
    run("apt-get", "update", env=environment)
    run("apt-get", "install", "--yes", "--no-install-recommends", "open-iscsi", "nfs-common", env=environment)
    run("modprobe", "iscsi_tcp")
    module_file = Path("/etc/modules-load.d/longhorn-iscsi.conf")
    if module_file.exists():
        if module_file.is_symlink() or module_file.read_text() != "iscsi_tcp\n":
            raise RuntimeError("preserving an unexpected existing Longhorn module configuration")
    else:
        with module_file.open("x") as stream:
            stream.write("iscsi_tcp\n")
        module_file.chmod(0o644)
    run("systemctl", "enable", "--now", "iscsid")
    run("systemctl", "is-active", "--quiet", "iscsid")
    DATA_PATH.mkdir(mode=0o700, exist_ok=True)

    account = backup_account()
    if backup_path.exists() or backup_path.is_symlink():
        check_backup_directory(backup_path, account)
    else:
        backup_path.mkdir(mode=0o700)
        os.chown(backup_path, account.pw_uid, account.pw_gid)
    if shared is None:
        # Register the folder without ShareMgmt.set's changes to ancestor ACLs.
        record = {
            "uuid": new_uuid, "name": SHARE_NAME, "comment": "Longhorn volume backups",
            "mntentref": mount["uuid"], "reldirpath": relative_path,
            "privileges": {"privilege": []},
        }
        run("omv-confdbadm", "update", "conf.system.sharedfolder", json.dumps(record), capture=True)
        shared = shared_folder(read_config("conf.system.sharedfolder"), mount["uuid"], relative_path)
        if shared is None:
            raise RuntimeError("OMV did not save the shared folder")
    existing = nfs_share(read_config("conf.service.nfs.share"), shared["uuid"])
    rpc("NFS", "setShare", export_params(shared["uuid"], new_uuid, account.pw_uid, account.pw_gid, existing))
    print("applying OMV's fstab and NFS configuration", flush=True)
    run("omv-salt", "deploy", "run", "fstab")
    run("omv-salt", "deploy", "run", "nfs")
    if not os.path.samefile(export_root / SHARE_NAME, backup_path):
        raise RuntimeError("OMV's NFS bind mount does not point to the backup directory")
    verify_nfs(backup_path, account.pw_uid)
    run("exportfs", "-v")
    run("df", "-hT", str(DATA_PATH), str(backup_path))
    print(f"backup target: {TARGET_URL}", flush=True)
    print("host preparation complete; next step is Longhorn installation", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"STOP: {type(error).__name__}: {error}\npaste this output; do not continue to the database cutover", file=sys.stderr)
        sys.exit(1)
