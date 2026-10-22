#!/usr/bin/env python3
"""Enable paperless's daily timer only after a verified coordinated backup and retention proof."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
module_spec = importlib.util.spec_from_file_location("paperless_daily", Path(__file__).with_name("daily-paperless.py"))
daily = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(daily)
setup = daily.setup
runtime_spec = importlib.util.spec_from_file_location("paperless_runtime", Path(__file__).with_name("install-paperless-backup.py"))
runtime = importlib.util.module_from_spec(runtime_spec)
runtime_spec.loader.exec_module(runtime)
ROOT = Path(__file__).resolve().parents[2]
INSTALL = daily.INSTALL
UNIT_DIRECTORY = daily.UNIT_DIRECTORY
SERVICE = daily.SERVICE
TIMER = "k3s-paperless-backup.timer"
CALENDAR = "*-*-* 12:15:00 UTC"
FILES = (
    "scripts/backups/prepare-paperless.py", "scripts/backups/backup-paperless.py", "scripts/backups/daily-paperless.py",
    "scripts/backups/install-paperless-backup.py", "scripts/backups/install-paperless-schedule.py",
    "scripts/backups/systemd/k3s-paperless-backup-initial.service", "scripts/backups/systemd/k3s-paperless-backup-recovery.service",
    "scripts/backups/systemd/k3s-paperless-backup.service", "scripts/backups/systemd/k3s-paperless-backup.timer",
    "apps/paperless-ngx/backups/data/kustomization.yaml", "apps/paperless-ngx/backups/data/replication-sources.yaml",
    "apps/paperless-ngx/backups/data/verify-job.yaml",
    "apps/paperless-ngx/backups/data/networkpolicy.yaml", "apps/paperless-ngx/backups/data/postgres-capture-job.yaml",
)
MARKER = "# managed by nas-backup-migration\n"


def service_idle():
    for name in (SERVICE, daily.native.SERVICE, daily.native.RECOVERY_SERVICE, "k3s-paperless-restore-initial.service"):
        state = setup.run(["systemctl", "show", name, "--property=ActiveState", "--value"], capture=True, check=False).stdout.strip()
        if state not in ("", "inactive", "failed"):
            raise setup.SetupError("a Paperless backup, restore, or recovery service is active")


def check_units():
    runtime.root_directory(UNIT_DIRECTORY)
    daily.native.check_units()
    for name in (SERVICE, TIMER):
        target = UNIT_DIRECTORY / name
        if target.exists() or target.is_symlink():
            runtime.regular_root_file(target)
            if not target.read_text().startswith(MARKER):
                raise setup.SetupError("refusing to replace an unmanaged paperless schedule unit")
        output = setup.run(["systemctl", "show", name, "--property=FragmentPath,DropInPaths"], capture=True, check=False).stdout
        properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if (properties.get("FragmentPath", "") not in ("", str(target)) or properties.get("DropInPaths") or
                os.path.lexists(UNIT_DIRECTORY / (name + ".d"))):
            raise setup.SetupError("an unmanaged unit or drop-in overrides this paperless schedule")
        if not (ROOT / "scripts/backups/systemd" / name).read_text().startswith(MARKER):
            raise setup.SetupError("downloaded schedule unit lacks its management marker")


def install_units():
    for name in (SERVICE, TIMER):
        descriptor, temporary = tempfile.mkstemp(prefix="." + name + "-", dir=UNIT_DIRECTORY)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((ROOT / "scripts/backups/systemd" / name).read_bytes())
                os.fchmod(output.fileno(), 0o644)
                output.flush(); os.fsync(output.fileno())
            os.replace(temporary, UNIT_DIRECTORY / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    daily.sync_directory(UNIT_DIRECTORY)
    setup.run(["systemd-analyze", "verify", *(str(UNIT_DIRECTORY / name) for name in (SERVICE, TIMER))])
    setup.run(["systemd-analyze", "calendar", CALENDAR])
    setup.run(["systemctl", "daemon-reload"])
    daily.check_service()
    timer_loaded()


def timer_loaded():
    path = UNIT_DIRECTORY / TIMER
    runtime.regular_root_file(path)
    output = setup.run(["systemctl", "show", TIMER,
        "--property=TimersCalendar,Persistent,Unit,FragmentPath,DropInPaths"], capture=True).stdout
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    calendars = re.findall(r"\bOnCalendar=([^;{}]+)", values.get("TimersCalendar", ""))
    if (path.read_bytes() != (ROOT / "scripts/backups/systemd" / TIMER).read_bytes() or
            [item.strip() for item in calendars] != [CALENDAR] or values.get("Persistent") != "yes" or
            values.get("Unit") != SERVICE or values.get("FragmentPath") != str(path) or values.get("DropInPaths") or
            os.path.lexists(UNIT_DIRECTORY / (TIMER + ".d"))):
        raise setup.SetupError("loaded timer calendar, persistence, service, or source differs")
    return values


def verified_daily():
    latest, proof = daily.saved(daily.LATEST), daily.saved(daily.PROOF)
    if (latest.get("result") != "passed" or proof.get("result") != "passed" or proof.get("retention") != "within 7d" or
            daily.ACTIVE.exists() or daily.ACTIVE.is_symlink() or
            not re.fullmatch(r"daily-[0-9a-f]{32}", latest.get("backup_id", "")) or
            latest.get("initial_backup_id") != daily.saved(daily.native.STATE).get("backup_id") or
            set(latest.get("components", {})) != set(daily.COMPONENTS)):
        raise setup.SetupError("daily backup or retention evidence is incomplete; timer remains disabled")
    journal = daily.saved(daily.native.QUIESCENCE)
    daily.native.check_journal(journal)
    if journal["backup_id"] != latest["backup_id"] or journal["phase"] != "resumed" or not journal["capture_complete"]:
        raise setup.SetupError("daily writer recovery evidence is incomplete; timer remains disabled")
    for component in daily.COMPONENTS:
        result = latest["components"][component]
        source = setup.get_object("replicationsource", component + "-backup", setup.NAMESPACE)
        if (result.get("result") != "passed" or result.get("backup_id") != latest["backup_id"] or
                result.get("claim") != setup.PROTECTED[component]["claim"] or
                not re.fullmatch(r"[0-9a-f]{64}", result.get("snapshot", "")) or
                result.get("checksums") != daily.native.inspect_artifacts(component, latest["backup_id"]) or
                not source or source["metadata"]["uid"] != latest["source_uids"][component] or
                not daily.native.upload_complete(source, latest["backup_id"])):
            raise setup.SetupError("daily snapshot or upload evidence is incomplete; timer remains disabled")
    if not daily.identity_matches(setup.live_preflight(check_resources=False), latest["production"]):
        raise setup.SetupError("production identity changed after daily verification")
    return latest


def install(revision):
    with setup.operation_lock(setup.RECOVERY / ".schedule-install.lock", create=True):
        with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
            service_idle()
            check_units()
            if (UNIT_DIRECTORY / TIMER).exists():
                setup.run(["systemctl", "disable", "--now", TIMER])
            daily.native.recover_writers()
            descriptor = daily.coordination_descriptor(); os.close(descriptor)
            daily.preflight()
            # Keep initial capture/boot recovery and isolated restore runtimes intact.
            runtime.INSTALL = INSTALL
            runtime.install_runtime(revision, files=FILES)
            install_units()
        setup.run(["systemctl", "enable", "--now", daily.native.RECOVERY_SERVICE], timeout=2160)
        properties = setup.run(["systemctl", "show", daily.native.RECOVERY_SERVICE, "--property=Result,ExecMainStatus"], capture=True).stdout
        values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
        if values.get("Result") != "success" or values.get("ExecMainStatus") != "0":
            raise setup.SetupError("interrupted-writer recovery did not complete")
        print("running a coordinated daily capture, writer recovery, all seven uploads, full verification, and scratch retention proof before enabling the timer", flush=True)
        try:
            setup.run(["systemctl", "start", SERVICE], timeout=33000)
        finally:
            setup.run(["journalctl", "-u", SERVICE, "-n", "250", "--no-pager", "-o", "cat"], check=False)
        with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
            latest = verified_daily()
        try:
            setup.run(["systemctl", "enable", "--now", TIMER])
            if setup.run(["systemctl", "is-enabled", TIMER], capture=True).stdout.strip() != "enabled":
                raise setup.SetupError("daily timer is not enabled")
            setup.run(["systemctl", "is-active", TIMER])
            loaded = timer_loaded()
            daily.save(setup.RECOVERY / "daily-schedule.json", {
                "result": "enabled", "revision": revision, "unit": TIMER,
                "schedule_utc": CALENDAR, "schedule_arizona": "05:15 daily", "persistent": True,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "recovery_unit": daily.native.RECOVERY_SERVICE, "recovery_at_boot": True,
                "first_verified_daily_generation": latest["backup_id"],
                "snapshots": {component: latest["components"][component]["snapshot"] for component in daily.COMPONENTS},
                "loaded_calendar": loaded["TimersCalendar"], "automatic_execution_verified": False,
            })
        except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            setup.run(["systemctl", "disable", "--now", TIMER], check=False)
            raise
        setup.run(["systemctl", "list-timers", TIMER, "--all", "--no-pager"])
        print("daily backup enabled: 05:15 Arizona / 12:15 UTC; missed-run catch-up enabled", flush=True)
        print("manual daily sequence and scratch retention verified; automatic execution and independent credential storage remain pending", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", arguments.revision):
        raise setup.SetupError("supply the full immutable download revision")
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for command in ("k3s", "findmnt", "systemctl", "systemd-analyze", "journalctl"):
        if not shutil.which(command):
            raise setup.SetupError("required backup command is missing")
    setup.recovery_directory()
    install(arguments.revision)


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, runtime.setup.SetupError) as error:
        print("paperless daily installation stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.TimeoutExpired):
        print("paperless daily installation stopped: filesystem, API, or configuration validation failed; private values omitted", file=sys.stderr)
        sys.exit(1)
