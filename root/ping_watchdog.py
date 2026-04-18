#!/usr/bin/env python3
#
# Copyright (c) 2026 Jason Godsey <jason@godsey.net>
# Licensed under the MIT License. See LICENSE.txt for details.

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

DEFAULTS_FILE = "/etc/default/ping_watchdog"

DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_AUTO_START = False
DEFAULT_AUTO_START_COUNT = 3
DEFAULT_VERBOSE = False
DEFAULT_DRY_RUN = False
DEFAULT_VMIDS: list[str] = []
DEFAULT_STATE_DIR = "/run/qm-agent-watchdog"
DEFAULT_LOGGER_TAG = "ping_watchdog"
DEFAULT_QM_PATH = "/usr/sbin/qm"
DEFAULT_STARTUP_ORDER = 999999

FAIL_THRESHOLD = DEFAULT_FAIL_THRESHOLD
AUTO_START = DEFAULT_AUTO_START
AUTO_START_THRESHOLD = DEFAULT_AUTO_START_COUNT
VERBOSE = DEFAULT_VERBOSE
LOGGER_TAG = DEFAULT_LOGGER_TAG
STATE_DIR = Path(DEFAULT_STATE_DIR)
QM_PATH = DEFAULT_QM_PATH


@dataclass
class VMInfo:
    vmid: str
    name: str
    status: str


@dataclass
class VMState:
    seen_good: bool = False
    fail_count: int = 0
    auto_start_count: int = 0

    @classmethod
    def from_json_text(cls, text: str) -> "VMState":
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return cls()
            return cls(
                seen_good=bool(data.get("seen_good", False)),
                fail_count=int(data.get("fail_count", 0)),
                auto_start_count=int(data.get("auto_start_count", 0)),
            )
        except Exception:
            return cls()

    def to_json_text(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass
class VMConfig:
    agent_enabled: bool = False
    lock_value: Optional[str] = None
    onboot: bool = False
    startup_order: int = DEFAULT_STARTUP_ORDER

    @classmethod
    def from_lines(cls, lines: list[str]) -> "VMConfig":
        agent_enabled = False
        lock_value: Optional[str] = None
        onboot = False
        startup_order = DEFAULT_STARTUP_ORDER

        for line in lines:
            if line.startswith("lock:"):
                lock_value = line.split(":", 1)[1].strip() or None
                continue

            if line.startswith("onboot:"):
                value = line.split(":", 1)[1].strip()
                onboot = value == "1"
                continue

            if line.startswith("agent:"):
                value = line.split(":", 1)[1].strip()
                if value == "1":
                    agent_enabled = True
                elif value == "0":
                    agent_enabled = False
                elif re.search(r"(^|,)enabled=1($|,)", value):
                    agent_enabled = True
                elif re.search(r"(^|,)enabled=0($|,)", value):
                    agent_enabled = False
                continue

            if line.startswith("startup:"):
                value = line.split(":", 1)[1].strip()
                for part in value.split(","):
                    part = part.strip()
                    if part.startswith("order="):
                        try:
                            startup_order = int(part.split("=", 1)[1].strip())
                        except ValueError:
                            startup_order = DEFAULT_STARTUP_ORDER
                        break

        return cls(
            agent_enabled=agent_enabled,
            lock_value=lock_value,
            onboot=onboot,
            startup_order=startup_order,
        )


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        return result

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"')) or
            (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]

        result[key] = value

    return result


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def log(msg: str, also_logger: bool = True, verbose_only: bool = False) -> None:
    if verbose_only and not VERBOSE:
        return

    print(msg)

    if also_logger:
        subprocess.run(
            ["logger", "-t", LOGGER_TAG, msg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def state_path(vmid: str) -> Path:
    return STATE_DIR / f"{vmid}.json"


def load_state(vmid: str) -> VMState:
    path = state_path(vmid)
    if not path.exists():
        return VMState()
    return VMState.from_json_text(path.read_text())


def save_state(vmid: str, state: VMState, dry_run: bool) -> None:
    if dry_run:
        return

    path = state_path(vmid)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(state.to_json_text())
    os.replace(tmp_path, path)


def clear_state(vmid: str, dry_run: bool) -> None:
    path = state_path(vmid)

    if dry_run:
        if path.exists():
            log(f"[DRY] would clear state for VM {vmid}")
        return

    try:
        path.unlink()
    except FileNotFoundError:
        pass


def parse_qm_list() -> list[VMInfo]:
    result = run_cmd([QM_PATH, "list"])
    if result.returncode != 0:
        raise RuntimeError(f"{QM_PATH} list failed: {result.stderr.strip()}")

    lines = result.stdout.strip().splitlines()
    if not lines:
        return []

    vms: list[VMInfo] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue

        vms.append(
            VMInfo(
                vmid=parts[0],
                name=parts[1],
                status=parts[2],
            )
        )

    return vms


def get_vm_config_lines(vmid: str) -> list[str]:
    result = run_cmd([QM_PATH, "config", vmid, "--current"])
    if result.returncode != 0:
        raise RuntimeError(f"failed to read config: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines()]


def get_vm_config(vmid: str) -> VMConfig:
    try:
        return VMConfig.from_lines(get_vm_config_lines(vmid))
    except RuntimeError as exc:
        log(f"VM {vmid}: {exc}")
        return VMConfig()


def agent_ping(vmid: str) -> bool:
    result = run_cmd([QM_PATH, "agent", vmid, "ping"])
    return result.returncode == 0


def vm_status_running(vmid: str) -> bool:
    result = run_cmd([QM_PATH, "status", vmid])
    if result.returncode != 0:
        return False
    return "status: running" in result.stdout


def reset_vm(vm: VMInfo, dry_run: bool) -> None:
    if dry_run:
        log(f"[DRY] would reset VM {vm.vmid} ({vm.name})")
        return

    if not vm_status_running(vm.vmid):
        log(f"VM {vm.vmid} ({vm.name}): no longer running, skipping reset")
        return

    result = run_cmd([QM_PATH, "reset", vm.vmid])
    if result.returncode == 0:
        log(f"VM {vm.vmid} ({vm.name}): reset after {FAIL_THRESHOLD} failed agent pings")
    else:
        log(f"VM {vm.vmid} ({vm.name}): {QM_PATH} reset failed: {result.stderr.strip()}")


def start_vm(vm: VMInfo, dry_run: bool) -> None:
    if dry_run:
        log(f"[DRY] would start VM {vm.vmid} ({vm.name})")
        return

    result = run_cmd([QM_PATH, "start", vm.vmid])
    if result.returncode == 0:
        log(
            f"VM {vm.vmid} ({vm.name}): started after "
            f"{AUTO_START_THRESHOLD} consecutive stopped checks"
        )
    else:
        log(f"VM {vm.vmid} ({vm.name}): {QM_PATH} start failed: {result.stderr.strip()}")


def cleanup_stale_state(active_vmids: set[str], dry_run: bool) -> None:
    if not STATE_DIR.exists():
        return

    for path in STATE_DIR.glob("*.json"):
        vmid = path.stem
        if vmid not in active_vmids:
            if dry_run:
                log(f"[DRY] would clear stale state for unknown VM {vmid}")
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def get_lowest_autostart_order(vms: list[VMInfo], vm_configs: dict[str, VMConfig]) -> Optional[int]:
    eligible_orders: list[int] = []

    for vm in vms:
        config = vm_configs[vm.vmid]

        if config.lock_value:
            continue
        if vm.status not in {"stopped", "shutdown"}:
            continue
        if not config.onboot:
            continue

        eligible_orders.append(config.startup_order)

    if not eligible_orders:
        return None

    return min(eligible_orders)


def process_running_vm(vm: VMInfo, config: VMConfig, state: VMState, dry_run: bool) -> None:
    state.auto_start_count = 0

    if not config.agent_enabled:
        log(
            f"VM {vm.vmid} ({vm.name}): skipped because guest agent is not enabled",
            verbose_only=True,
        )
        state.seen_good = False
        state.fail_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    if agent_ping(vm.vmid):
        if not state.seen_good or state.fail_count != 0:
            log(f"VM {vm.vmid} ({vm.name}): agent ping OK, watchdog armed")
        else:
            log(f"VM {vm.vmid} ({vm.name}): agent ping OK", verbose_only=True)

        state.seen_good = True
        state.fail_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    if not state.seen_good:
        log(f"VM {vm.vmid} ({vm.name}): agent ping failed, watchdog not armed yet")
        save_state(vm.vmid, state, dry_run)
        return

    next_fail = state.fail_count + 1
    log(f"VM {vm.vmid} ({vm.name}): agent ping failed ({next_fail}/{FAIL_THRESHOLD})")

    if next_fail >= FAIL_THRESHOLD:
        if dry_run:
            log(f"[DRY] VM {vm.vmid} ({vm.name}): would clear seen_good/fail_count and reset VM")
            return

        state.seen_good = False
        state.fail_count = 0
        save_state(vm.vmid, state, dry_run=False)
        reset_vm(vm, dry_run=False)
        return

    state.fail_count = next_fail
    save_state(vm.vmid, state, dry_run)


def process_stopped_vm(
    vm: VMInfo,
    config: VMConfig,
    state: VMState,
    dry_run: bool,
    lowest_autostart_order: Optional[int],
) -> None:
    state.seen_good = False
    state.fail_count = 0

    if not AUTO_START:
        log(f"VM {vm.vmid} ({vm.name}): skipped because status={vm.status}", verbose_only=True)
        state.auto_start_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    if not config.onboot:
        log(
            f"VM {vm.vmid} ({vm.name}): stopped but onboot is not enabled",
            verbose_only=True,
        )
        state.auto_start_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    if lowest_autostart_order is None:
        state.auto_start_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    if config.startup_order != lowest_autostart_order:
        log(
            f"VM {vm.vmid} ({vm.name}): waiting for lower startup order "
            f"{lowest_autostart_order} before starting order {config.startup_order}",
            verbose_only=True,
        )
        state.auto_start_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    next_auto = state.auto_start_count + 1
    log(
        f"VM {vm.vmid} ({vm.name}): stopped/shutdown with onboot enabled "
        f"and startup order {config.startup_order} "
        f"({next_auto}/{AUTO_START_THRESHOLD})",
        verbose_only=True,
    )

    if next_auto >= AUTO_START_THRESHOLD:
        if dry_run:
            log(f"[DRY] VM {vm.vmid} ({vm.name}): would clear auto_start_count and start VM")
            return

        state.auto_start_count = 0
        save_state(vm.vmid, state, dry_run=False)
        start_vm(vm, dry_run=False)
        return

    state.auto_start_count = next_auto
    save_state(vm.vmid, state, dry_run)


def process_vm(
    vm: VMInfo,
    config: VMConfig,
    dry_run: bool,
    lowest_autostart_order: Optional[int],
) -> None:
    state = load_state(vm.vmid)

    if config.lock_value:
        log(
            f"VM {vm.vmid} ({vm.name}): skipped because lock={config.lock_value}",
            verbose_only=True,
        )
        state.seen_good = False
        state.fail_count = 0
        state.auto_start_count = 0
        save_state(vm.vmid, state, dry_run)
        return

    if vm.status == "running":
        process_running_vm(vm, config, state, dry_run)
        return

    if vm.status in {"stopped", "shutdown"}:
        process_stopped_vm(vm, config, state, dry_run, lowest_autostart_order)
        return

    log(f"VM {vm.vmid} ({vm.name}): skipped because status={vm.status}", verbose_only=True)
    state.seen_good = False
    state.fail_count = 0
    state.auto_start_count = 0
    save_state(vm.vmid, state, dry_run)


def build_parser(env_defaults: dict[str, str]) -> argparse.ArgumentParser:
    env_fail_count = int(env_defaults.get("FAIL_COUNT", DEFAULT_FAIL_THRESHOLD))
    env_auto_start = parse_bool(env_defaults.get("AUTO_START", str(DEFAULT_AUTO_START)))
    env_auto_start_count = int(
        env_defaults.get("AUTO_START_COUNT", DEFAULT_AUTO_START_COUNT)
    )
    env_verbose = parse_bool(env_defaults.get("VERBOSE", str(DEFAULT_VERBOSE)))
    env_dry_run = parse_bool(env_defaults.get("DRY_RUN", str(DEFAULT_DRY_RUN)))
    env_vmids_raw = env_defaults.get("VMIDS", "")
    env_vmids = [v for v in env_vmids_raw.split() if v]

    parser = argparse.ArgumentParser(
        description="Watchdog Proxmox VMs with qemu-guest-agent enabled."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_dry_run,
        help=f"Do not modify state and do not start/reset VMs "
             f"(default from {DEFAULTS_FILE}: {env_dry_run})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=env_verbose,
        help=f"Log skips and successful checks too "
             f"(default from {DEFAULTS_FILE}: {env_verbose})",
    )
    parser.add_argument(
        "--fail-count",
        type=int,
        default=env_fail_count,
        help=f"Failed ping threshold before reset "
             f"(default from {DEFAULTS_FILE}: {env_fail_count})",
    )
    parser.add_argument(
        "--auto-start",
        action="store_true",
        default=env_auto_start,
        help=f"Auto-start stopped/shutdown VMs that have onboot: 1 configured "
             f"(default from {DEFAULTS_FILE}: {env_auto_start})",
    )
    parser.add_argument(
        "--auto-start-count",
        type=int,
        default=env_auto_start_count,
        help=f"Consecutive stopped/shutdown checks before auto-start "
             f"(default from {DEFAULTS_FILE}: {env_auto_start_count})",
    )
    parser.add_argument(
        "--vmid",
        action="append",
        default=env_vmids,
        help="Specific VMID to check. May be given multiple times. Defaults to all VMs.",
    )
    return parser


def main() -> int:
    global VERBOSE, FAIL_THRESHOLD, AUTO_START, AUTO_START_THRESHOLD, LOGGER_TAG, STATE_DIR, QM_PATH

    env_defaults = parse_env_file(DEFAULTS_FILE)

    LOGGER_TAG = env_defaults.get("LOGGER_TAG", DEFAULT_LOGGER_TAG)
    STATE_DIR = Path(env_defaults.get("STATE_DIR", DEFAULT_STATE_DIR))
    QM_PATH = env_defaults.get("QM_PATH", DEFAULT_QM_PATH)

    if not Path(QM_PATH).exists():
        log(f"watchdog error: QM_PATH does not exist: {QM_PATH}")
        return 1

    if not os.access(QM_PATH, os.X_OK):
        log(f"watchdog error: QM_PATH is not executable: {QM_PATH}")
        return 1

    parser = build_parser(env_defaults)
    args = parser.parse_args()

    dry_run = bool(args.dry_run)
    VERBOSE = bool(args.verbose)
    FAIL_THRESHOLD = max(1, int(args.fail_count))
    AUTO_START = bool(args.auto_start)
    AUTO_START_THRESHOLD = max(1, int(args.auto_start_count))
    requested_vmids = [str(v) for v in args.vmid]

    ensure_state_dir()

    try:
        all_vms = parse_qm_list()
    except Exception as exc:
        log(f"watchdog error: {exc}")
        return 1

    vm_map = {vm.vmid: vm for vm in all_vms}

    if requested_vmids:
        selected_vms: list[VMInfo] = []
        for vmid in requested_vmids:
            vm = vm_map.get(vmid)
            if vm is None:
                log(f"VM {vmid}: not found in qm list")
                continue
            selected_vms.append(vm)
    else:
        selected_vms = all_vms

    active_vmids = {vm.vmid for vm in all_vms}
    cleanup_stale_state(active_vmids, dry_run)

    vm_configs = {vm.vmid: get_vm_config(vm.vmid) for vm in selected_vms}
    lowest_autostart_order = get_lowest_autostart_order(selected_vms, vm_configs)

    for vm in selected_vms:
        process_vm(
            vm=vm,
            config=vm_configs[vm.vmid],
            dry_run=dry_run,
            lowest_autostart_order=lowest_autostart_order,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

