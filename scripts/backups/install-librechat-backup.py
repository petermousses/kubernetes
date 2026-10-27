#!/usr/bin/env python3
"""Install writer recovery and run LibreChat's five initial backups."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location("librechat_native", Path(__file__).with_name("backup-librechat.py"))
native = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(native)
setup = native.setup
ROOT = Path(__file__).resolve().parents[2]
INSTALL = Path("/usr/local/lib/k3s-backups/librechat")
UNIT_DIRECTORY = Path("/etc/systemd/system")
SERVICE = "k3s-librechat-backup-initial.service"
RECOVERY_SERVICE = "k3s-librechat-backup-recovery.service"
FILES = (
    "scripts/backups/prepare-librechat.py",
    "scripts/backups/backup-librechat.py",
    "scripts/backups/install-librechat-backup.py",
    "scripts/backups/systemd/k3s-librechat-backup-initial.service",
    "scripts/backups/systemd/k3s-librechat-backup-recovery.service",
    "apps/librechat/backups/data/kustomization.yaml",
    "apps/librechat/backups/data/replication-sources.yaml",
    "apps/librechat/backups/data/meilisearch-dump-copy-job.yaml",
    "apps/librechat/backups/data/verify-job.yaml",
)
MARKER = "# managed by nas-backup-migration\n"


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


def check_units():
    root_directory(UNIT_DIRECTORY)
    for name in (SERVICE, RECOVERY_SERVICE):
        target = UNIT_DIRECTORY / name
        if target.exists() or target.is_symlink():
            regular_root_file(target)
            if not target.read_text().startswith(MARKER):
                raise setup.SetupError("refusing to replace an unmanaged systemd unit: " + name)
        properties = setup.run(["systemctl", "show", name, "--property=FragmentPath,DropInPaths"],
                               capture=True, check=False).stdout.splitlines()
        for line in properties:
            key, _, value = line.partition("=")
            if key == "FragmentPath" and value and value != str(target):
                raise setup.SetupError("an unmanaged systemd unit already uses this name: " + name)
            if key == "DropInPaths" and value:
                raise setup.SetupError("unexpected systemd drop-ins; preserving them: " + name)
        if ((UNIT_DIRECTORY / (name + ".d")).exists() or
                (UNIT_DIRECTORY / (name + ".d")).is_symlink()):
            raise setup.SetupError("unexpected systemd drop-ins; preserving them: " + name)
        source = ROOT / "scripts/backups/systemd" / name
        if not source.read_text().startswith(MARKER):
            raise setup.SetupError("downloaded unit is missing its management marker: " + name)


def install_runtime(revision):
    root_directory(INSTALL, create=True)
    destination = INSTALL / revision
    if destination.exists() or destination.is_symlink():
        root_directory(destination)
        for filename in FILES:
            target = destination / filename
            root_directory(target.parent)
            regular_root_file(target)
            if target.read_bytes() != (ROOT / filename).read_bytes():
                raise setup.SetupError("an installed revision differs from its downloaded files: " + revision)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=INSTALL))
        try:
            for filename in FILES:
                target = temporary / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / filename).read_bytes())
                os.chmod(target, 0o644)
                with target.open("rb") as content:
                    os.fsync(content.fileno())
            for path in sorted((path for path in temporary.rglob("*") if path.is_dir()),
                               key=lambda path: len(path.parts), reverse=True):
                os.chmod(path, 0o755)
                native.sync_directory(path)
            os.chmod(temporary, 0o755)
            native.sync_directory(temporary)
            native.rename_no_replace(temporary, destination)
            native.sync_directory(INSTALL)
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
        native.sync_directory(INSTALL)
    finally:
        if os.path.lexists(temporary_name):
            os.unlink(temporary_name)


def service_idle():
    for name in (SERVICE, RECOVERY_SERVICE):
        state = setup.run(["systemctl", "show", name, "--property=ActiveState", "--value"],
                          capture=True, check=False).stdout.strip()
        if state not in ("", "inactive", "failed"):
            raise setup.SetupError("a backup or recovery service is active: " + name)


def install_units():
    for name in (SERVICE, RECOVERY_SERVICE):
        descriptor, temporary = tempfile.mkstemp(prefix="." + name + "-", dir=UNIT_DIRECTORY)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((ROOT / "scripts/backups/systemd" / name).read_bytes())
                os.fchmod(output.fileno(), 0o644)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, UNIT_DIRECTORY / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    native.sync_directory(UNIT_DIRECTORY)
    setup.run(["systemd-analyze", "verify", *(str(UNIT_DIRECTORY / name)
                                              for name in (SERVICE, RECOVERY_SERVICE))])
    setup.run(["systemctl", "daemon-reload"])
    native.check_units()


def install_locked(revision):
    check_units()
    service_idle()
    with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
        native.recover_writer()
        service_idle()
        install_runtime(revision)
        install_units()
    setup.run(["systemctl", "enable", "--now", RECOVERY_SERVICE], timeout=1560)
    properties = setup.run(["systemctl", "show", RECOVERY_SERVICE,
                            "--property=Result,ExecMainStatus"], capture=True).stdout
    result = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
    if result.get("Result") != "success" or result.get("ExecMainStatus") != "0":
        raise setup.SetupError("interrupted-writer recovery did not complete successfully")
    print("running initial captures, writer recovery, five VolSync uploads, and full verification; this can take several minutes", flush=True)
    try:
        setup.run(["systemctl", "start", SERVICE], timeout=10860)
    finally:
        setup.run(["journalctl", "-u", SERVICE, "-n", "250", "--no-pager", "-o", "cat"], check=False)
    state = native.saved(native.STATE)
    proof = native.saved(native.COMPLETE)
    native.check_journal(native.saved(native.QUIESCENCE))
    if (proof.get("result") != "passed" or proof.get("backup_id") != state.get("backup_id") or
            set(proof.get("components", {})) != set(native.COMPONENTS)):
        raise setup.SetupError("initial backup evidence is incomplete")
    journal = native.saved(native.QUIESCENCE)
    if (journal["phase"] != "resumed" or not journal["capture_complete"] or
            journal["backup_id"] != state["backup_id"]):
        raise setup.SetupError("common writer recovery or capture identity is incomplete")
    for component in native.COMPONENTS:
        result = proof["components"][component]
        if (result.get("result") != "passed" or result.get("backup_id") != state["backup_id"] or
                result.get("claim") != setup.PROTECTED[component]["claim"] or
                not re.fullmatch(r"[0-9a-f]{64}", result.get("snapshot", ""))):
            raise setup.SetupError("backup verification is incomplete: " + component)
        source = setup.get_object("replicationsource", component + "-backup", setup.NAMESPACE)
        if (not source or source["spec"].get("paused") is not True or
                source["spec"].get("trigger") != {"manual": state["backup_id"]} or
                source.get("status", {}).get("lastManualSync") != state["backup_id"] or
                source.get("status", {}).get("latestMoverStatus", {}).get("result") != "Successful"):
            raise setup.SetupError("verified upload source is not paused and successful: " + component)
    setup.live_preflight(check_resources=False)
    print(json.dumps({"result": "passed", "backup_id": state["backup_id"],
                      "snapshots": {name: result["snapshot"] for name, result in proof["components"].items()}}, indent=2))
    print("five LibreChat initial backups uploaded and read back; isolated restores, daily scheduling, and off-NAS recovery remain pending")


def install(revision):
    with setup.operation_lock(setup.RECOVERY / ".install.lock", create=True):
        install_locked(revision)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", arguments.revision):
        raise setup.SetupError("revision must be the immutable downloaded commit SHA")
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    install(arguments.revision)


if __name__ == "__main__":
    try:
        main()
    except setup.SetupError as error:
        print("LibreChat backup installation stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("LibreChat backup installation stopped: " + type(error).__name__ + "; private values omitted", file=sys.stderr)
        sys.exit(1)
