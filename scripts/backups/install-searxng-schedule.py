#!/usr/bin/env python3
"""Install the approved searxng daily backup only after a successful run."""

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location(
    "searxng_daily", Path(__file__).with_name("daily-searxng.py"),
)
daily = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(daily)
setup = daily.setup
ROOT = Path(__file__).resolve().parents[2]
INSTALL = Path("/usr/local/lib/k3s-backups/searxng")
UNIT_DIRECTORY = Path("/etc/systemd/system")
SERVICE = "k3s-searxng-backup.service"
TIMER = "k3s-searxng-backup.timer"
RECOVERY_SERVICE = "k3s-searxng-backup-recovery.service"
FILES = (
    "scripts/backups/prepare-searxng.py",
    "scripts/backups/backup-searxng.py",
    "scripts/backups/daily-searxng.py",
    "apps/searxng/backups/data/kustomization.yaml",
    "apps/searxng/backups/data/networkpolicy.yaml",
    "apps/searxng/backups/data/valkey-dump-job.yaml",
    "apps/searxng/backups/data/verify-job.yaml",
    "apps/searxng/backups/data/replication-sources.yaml",
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
            stat.S_IMODE(attributes.st_mode) & 0o022):
        raise setup.SetupError("installation file must be regular, root-owned, and not writable by others: " + str(path))


def check_units():
    root_directory(UNIT_DIRECTORY)
    for name in (SERVICE, TIMER, RECOVERY_SERVICE):
        target = UNIT_DIRECTORY / name
        if target.exists() or target.is_symlink():
            regular_root_file(target)
            if not target.read_text().startswith(MARKER):
                raise setup.SetupError("refusing to replace an unmanaged systemd unit: " + name)
        dropins = []
        properties = setup.run(
            ["systemctl", "show", name, "--property=FragmentPath,DropInPaths"],
            capture=True, check=False,
        ).stdout.splitlines()
        for line in properties:
            key, _, value = line.partition("=")
            if key == "FragmentPath" and value and value != str(target):
                raise setup.SetupError("an unmanaged systemd unit already uses this name: " + name)
            if key == "DropInPaths" and value:
                dropins.append(value)
        if dropins or (UNIT_DIRECTORY / (name + ".d")).exists():
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
                daily.sync_directory(path)
            os.chmod(temporary, 0o755)
            daily.sync_directory(temporary)
            temporary.rename(destination)
            daily.sync_directory(INSTALL)
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
        daily.sync_directory(INSTALL)
    finally:
        if os.path.lexists(temporary_name):
            os.unlink(temporary_name)


def install_units():
    for name in (SERVICE, TIMER, RECOVERY_SERVICE):
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
    daily.sync_directory(UNIT_DIRECTORY)
    setup.run(["systemd-analyze", "verify", *(str(UNIT_DIRECTORY / name) for name in (SERVICE, TIMER, RECOVERY_SERVICE))])
    setup.run(["systemd-analyze", "calendar", "*-*-* 10:00:00 UTC"])
    setup.run(["systemctl", "daemon-reload"])


def service_idle():
    state = setup.run(["systemctl", "show", SERVICE, "--property=ActiveState", "--value"],
                      capture=True, check=False).stdout.strip()
    if state not in ("", "inactive", "failed"):
        raise setup.SetupError("the daily backup service is already active: " + state)


def install(revision):
    check_units()
    service_idle()
    descriptor = os.open(setup.RECOVERY / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise setup.SetupError("another searxng backup/setup command is running") from error
        daily.recover_writer()
        coordination = daily.coordination_descriptor()
        os.close(coordination)
        daily.preflight()
        # Pause only this managed timer while installing or retrying its runtime.
        if (UNIT_DIRECTORY / TIMER).exists():
            setup.run(["systemctl", "disable", "--now", TIMER])
        service_idle()
        install_runtime(revision)
        install_units()
    # The service needs the same setup lock, so never hold it during this run.
    setup.run(["systemctl", "enable", "--now", RECOVERY_SERVICE])
    setup.run(["systemctl", "is-active", RECOVERY_SERVICE])
    print("running the daily sequence and scratch retention proof before enabling its timer", flush=True)
    try:
        setup.run(["systemctl", "start", SERVICE], timeout=7260)
    finally:
        setup.run(["journalctl", "-u", SERVICE, "-n", "160", "--no-pager", "-o", "cat"], check=False)
    latest = daily.saved(daily.LATEST)
    proof = daily.saved(daily.PROOF)
    if (latest.get("result") != "passed" or proof.get("result") != "passed" or
            proof.get("retention") != "within 7d" or daily.ACTIVE.exists() or
            set(latest.get("components", {})) != set(daily.COMPONENTS) or
            not all(latest["components"][component].get("result") == "passed" and
                    latest["components"][component].get("backup_id") == latest["backup_id"] and
                    daily.native.upload_complete(
                        setup.get_object("replicationsource", daily.native.source_name(component), setup.NAMESPACE),
                        latest["backup_id"]) for component in daily.COMPONENTS)):
        raise setup.SetupError("daily backup or retention evidence is incomplete; the timer remains disabled")
    setup.run(["systemctl", "enable", "--now", TIMER])
    if setup.run(["systemctl", "is-enabled", TIMER], capture=True).stdout.strip() != "enabled":
        raise setup.SetupError("the daily timer is not enabled")
    setup.run(["systemctl", "is-active", TIMER])
    daily.save(setup.RECOVERY / "daily-schedule.json", {
        "result": "enabled", "revision": revision, "unit": TIMER,
        "schedule_utc": "*-*-* 10:00:00 UTC", "schedule_arizona": "03:00 daily",
        "persistent": True, "verified_at": datetime.now(timezone.utc).isoformat(),
        "recovery_unit": RECOVERY_SERVICE, "recovery_at_boot": True,
        "first_verified_daily_generation": latest["backup_id"],
        "snapshots": {component: latest["components"][component]["snapshot"] for component in daily.COMPONENTS},
    })
    setup.run(["systemctl", "list-timers", TIMER, "--all", "--no-pager"])
    print("daily backup enabled: 03:00 Arizona / 10:00 UTC; missed-run catch-up enabled", flush=True)
    print("daily sequence and scratch retention verified; independent credential storage remains pending", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", arguments.revision):
        raise setup.SetupError("supply the full immutable Git commit used for this download")
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for command in ("k3s", "findmnt", "systemctl", "systemd-analyze", "journalctl"):
        if not shutil.which(command):
            raise setup.SetupError("required command is missing: " + command)
    setup.recovery_directory()
    descriptor = os.open(setup.RECOVERY / ".schedule-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise setup.SetupError("another schedule installation is running") from error
        install(arguments.revision)


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"daily schedule installation stopped: {error}", file=sys.stderr)
        print("daily scheduling is not confirmed; preserve the recovery bundle and relay this output", file=sys.stderr)
        sys.exit(1)
