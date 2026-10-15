#!/usr/bin/env python3
"""Install the approved monitoring daily backup only after a successful run."""

import argparse
from datetime import datetime, timezone
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
    "monitoring_daily", Path(__file__).with_name("daily-monitoring.py"),
)
daily = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(daily)
setup = daily.setup
ROOT = Path(__file__).resolve().parents[2]
INSTALL = Path("/usr/local/lib/k3s-backups/monitoring")
UNIT_DIRECTORY = Path("/etc/systemd/system")
SERVICE = "k3s-monitoring-backup.service"
TIMER = "k3s-monitoring-backup.timer"
RECOVERY_SERVICE = "k3s-monitoring-backup-recovery.service"
FILES = (
    "scripts/backups/prepare-monitoring.py",
    "scripts/backups/backup-monitoring.py",
    "scripts/backups/daily-monitoring.py",
    "apps/monitoring/backups/data/kustomization.yaml",
    "apps/monitoring/backups/data/verify-job.yaml",
    "apps/monitoring/backups/data/replication-sources.yaml",
)
MARKER = "# managed by nas-backup-migration\n"


runtime_spec = importlib.util.spec_from_file_location(
    "monitoring_runtime_installer", Path(__file__).with_name("install-monitoring-backup.py"),
)
runtime = importlib.util.module_from_spec(runtime_spec)
runtime_spec.loader.exec_module(runtime)
root_directory = runtime.root_directory
regular_root_file = runtime.regular_root_file


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
    setup.run(["systemd-analyze", "calendar", "*-*-* 10:45:00 UTC"])
    setup.run(["systemctl", "daemon-reload"])


def service_idle():
    for name in (SERVICE, runtime.SERVICE):
        state = setup.run(["systemctl", "show", name, "--property=ActiveState", "--value"],
                          capture=True, check=False).stdout.strip()
        if state not in ("", "inactive", "failed"):
            raise setup.SetupError("a monitoring backup service is already active: " + name)


def verified_daily():
    latest, proof = daily.saved(daily.LATEST), daily.saved(daily.PROOF)
    if (latest.get("result") != "passed" or proof.get("result") != "passed" or
            proof.get("retention") != "within 7d" or daily.ACTIVE.exists() or daily.ACTIVE.is_symlink() or
            not re.fullmatch(r"daily-[0-9a-f]{32}", latest.get("backup_id", "")) or
            set(latest.get("components", {})) != set(daily.COMPONENTS)):
        raise setup.SetupError("daily backup or retention evidence is incomplete; timer remains disabled")
    for component in daily.COMPONENTS:
        result = latest["components"][component]
        journal = daily.saved(daily.native.journal_path(component))
        daily.native.check_journal(journal, component)
        if (result.get("result") != "passed" or result.get("backup_id") != latest["backup_id"] or
                result.get("claim") != setup.PROTECTED[component]["claim"] or
                not re.fullmatch(r"[0-9a-f]{64}", result.get("snapshot", "")) or
                result.get("checksums") != daily.native.inspect_artifacts(component, latest["backup_id"]) or
                journal["phase"] != "resumed" or journal["backup_id"] != latest["backup_id"] or
                not daily.native.upload_complete(setup.get_object("replicationsource", component + "-backup", setup.NAMESPACE), latest["backup_id"])):
            raise setup.SetupError("daily component evidence is incomplete; timer remains disabled: " + component)
        daily.native.service_health(component)
    return latest


def timer_loaded():
    properties = setup.run(["systemctl", "show", TIMER,
                            "--property=TimersCalendar,Persistent,Unit,FragmentPath,DropInPaths"], capture=True).stdout
    values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
    calendars = re.findall(r"\bOnCalendar=([^;{}]+)", values.get("TimersCalendar", ""))
    if (len(calendars) != 1 or calendars[0].strip() != "*-*-* 10:45:00 UTC" or
            values.get("Persistent") != "yes" or values.get("Unit") != SERVICE or
            values.get("FragmentPath") != str(UNIT_DIRECTORY / TIMER) or values.get("DropInPaths")):
        raise setup.SetupError("loaded timer calendar, persistence, service, or source differs")
    return values


def install(revision):
    check_units()
    service_idle()
    with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
        if (UNIT_DIRECTORY / TIMER).exists():
            setup.run(["systemctl", "disable", "--now", TIMER])
        daily.recover_writers()
        coordination = daily.coordination_descriptor()
        os.close(coordination)
        daily.preflight()
        service_idle()
        runtime.install_runtime(revision, files=FILES)
        install_units()
        timer_loaded()
    setup.run(["systemctl", "enable", "--now", RECOVERY_SERVICE], timeout=1560)
    properties = setup.run(["systemctl", "show", RECOVERY_SERVICE, "--property=Result,ExecMainStatus"], capture=True).stdout
    result = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
    if result.get("Result") != "success" or result.get("ExecMainStatus") != "0":
        raise setup.SetupError("interrupted-writer recovery did not complete successfully")
    print("running all four daily captures, recoveries, uploads, full verification, and scratch retention proof before enabling the timer", flush=True)
    try:
        setup.run(["systemctl", "start", SERVICE], timeout=45000)
    finally:
        setup.run(["journalctl", "-u", SERVICE, "-n", "300", "--no-pager", "-o", "cat"], check=False)
    # Snapshot the verified generation under the namespace lock. Release it
    # before activation so a persistent timer's catch-up can acquire that lock.
    with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
        latest = verified_daily()
    try:
        setup.run(["systemctl", "enable", "--now", TIMER])
        if setup.run(["systemctl", "is-enabled", TIMER], capture=True).stdout.strip() != "enabled":
            raise setup.SetupError("the daily timer is not enabled")
        setup.run(["systemctl", "is-active", TIMER])
        loaded = timer_loaded()
        daily.save(setup.RECOVERY / "daily-schedule.json", {
            "result": "enabled", "revision": revision, "unit": TIMER,
            "schedule_utc": "*-*-* 10:45:00 UTC", "schedule_arizona": "03:45 daily",
            "persistent": True, "verified_at": datetime.now(timezone.utc).isoformat(),
            "recovery_unit": RECOVERY_SERVICE, "recovery_at_boot": True,
            "first_verified_daily_generation": latest["backup_id"],
            "snapshots": {component: latest["components"][component]["snapshot"] for component in daily.COMPONENTS},
            "loaded_calendar": loaded["TimersCalendar"],
        })
    except (setup.SetupError, OSError, ValueError, subprocess.TimeoutExpired):
        setup.run(["systemctl", "disable", "--now", TIMER], check=False)
        raise
    setup.run(["systemctl", "list-timers", TIMER, "--all", "--no-pager"])
    print("daily backup enabled: 03:45 Arizona / 10:45 UTC; missed-run catch-up enabled", flush=True)
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
    with setup.operation_lock(setup.RECOVERY / ".install.lock", create=True):
        install(arguments.revision)



if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, runtime.setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"daily schedule installation stopped: {error}", file=sys.stderr)
        print("daily scheduling is not confirmed; preserve the recovery bundle and relay this output", file=sys.stderr)
        sys.exit(1)
