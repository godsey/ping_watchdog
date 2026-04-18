#!/usr/bin/env python3
#
# Copyright (c) 2026 Jason Godsey <jason@godsey.net>
# Licensed under the MIT License. See LICENSE.txt for details.

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# Configuration Defaults
DEFAULTS_FILE = "/etc/default/ping_watchdog"

DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_AUTO_START = False
DEFAULT_AUTO_START_COUNT = 5
DEFAULT_VERBOSE = False
DEFAULT_DRY_RUN = False
DEFAULT_VMIDS: list[str] = []
DEFAULT_STATE_DIR = "/run/qm-agent-watchdog"
DEFAULT_LOGGER_TAG = "qm-agent-watchdog"
DEFAULT_QM_PATH = "/usr/sbin/qm"
DEFAULT_STARTUP_ORDER = 999999

VERBOSE = DEFAULT_VERBOSE
LOGGER_TAG = DEFAULT_LOGGER_TAG

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
            line = line.strip()
            if line.startswith("lock:"):
                lock_value = line.split(":", 1)[1].strip() or None
            elif line.startswith("onboot:"):
                value = line.split(":", 1)[1].strip()
                onboot = value == "1"
            elif line.startswith("agent:"):
                value = line.split(":", 1)[1].strip()
                parts = [p.strip() for p in value.split(",")]
                if "1" in parts or "enabled=1" in parts:
                    agent_enabled = True
                elif "0" in parts or "enabled=0" in parts:
                    agent_enabled = False
            elif line.startswith("startup:"):
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
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}

def parse_env_int(env_defaults: dict[str, str], key: str, fallback: int) -> int:
    raw_value = env_defaults.get(key)
    if raw_value is None:
        return fallback
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw_value!r}") from exc

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
            check=False,
        )

class Watchdog:
    def __init__(self, config_dict: dict):
        self.dry_run = config_dict.get("dry_run", DEFAULT_DRY_RUN)
        self.verbose = config_dict.get("verbose", DEFAULT_VERBOSE)
        self.fail_threshold = config_dict.get("fail_threshold", DEFAULT_FAIL_THRESHOLD)
        self.auto_start = config_dict.get("auto_start", DEFAULT_AUTO_START)
        self.auto_start_threshold = config_dict.get("auto_start_threshold", DEFAULT_AUTO_START_COUNT)
        self.state_dir = Path(config_dict.get("state_dir", DEFAULT_STATE_DIR))
        self.qm_path = config_dict.get("qm_path", DEFAULT_QM_PATH)
        self.requested_vmids = config_dict.get("vmids", [])

    def run_cmd(self, cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )

    def get_state_path(self, vmid: str) -> Path:
        return self.state_dir / f"{vmid}.json"

    def load_state(self, vmid: str) -> VMState:
        path = self.get_state_path(vmid)
        if not path.exists():
            return VMState()
        try:
            return VMState.from_json_text(path.read_text())
        except Exception:
            return VMState()

    def save_state(self, vmid: str, state: VMState) -> None:
        if self.dry_run:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.get_state_path(vmid)
        tmp_path = path.with_suffix(".tmp")
        try:
            tmp_path.write_text(state.to_json_text())
            os.replace(tmp_path, path)
        except Exception as exc:
            log(f"Failed to save state for VM {vmid}: {exc}")

    def parse_qm_list(self) -> list[VMInfo]:
        result = self.run_cmd([self.qm_path, "list"])
        if result.returncode != 0:
            raise RuntimeError(f"{self.qm_path} list failed: {result.stderr.strip()}")

        lines = result.stdout.strip().splitlines()
        if not lines:
            return []

        vms: list[VMInfo] = []
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 3:
                continue
            vms.append(VMInfo(vmid=parts[0], name=parts[1], status=parts[2]))
        return vms

    def get_vm_config(self, vmid: str) -> VMConfig:
        result = self.run_cmd([self.qm_path, "config", vmid, "--current"])
        if result.returncode != 0:
            log(f"VM {vmid}: failed to read config: {result.stderr.strip()}")
            return VMConfig()
        lines = [line.strip() for line in result.stdout.splitlines()]
        return VMConfig.from_lines(lines)

    def agent_ping(self, vmid: str) -> bool:
        result = self.run_cmd([self.qm_path, "agent", vmid, "ping"])
        return result.returncode == 0

    def vm_status_running(self, vmid: str) -> bool:
        result = self.run_cmd([self.qm_path, "status", vmid])
        if result.returncode != 0:
            return False
        return "status: running" in result.stdout

    def reset_vm(self, vm: VMInfo) -> None:
        if self.dry_run:
            log(f"[DRY] would reset VM {vm.vmid} ({vm.name})")
            return

        if not self.vm_status_running(vm.vmid):
            log(f"VM {vm.vmid} ({vm.name}): no longer running, skipping reset")
            return

        result = self.run_cmd([self.qm_path, "reset", vm.vmid])
        if result.returncode == 0:
            log(f"VM {vm.vmid} ({vm.name}): reset after {self.fail_threshold} failed agent pings")
        else:
            log(f"VM {vm.vmid} ({vm.name}): {self.qm_path} reset failed: {result.stderr.strip()}")

    def start_vm(self, vm: VMInfo) -> None:
        if self.dry_run:
            log(f"[DRY] would start VM {vm.vmid} ({vm.name})")
            return

        result = self.run_cmd([self.qm_path, "start", vm.vmid])
        if result.returncode == 0:
            log(f"VM {vm.vmid} ({vm.name}): started after {self.auto_start_threshold} consecutive stopped checks")
        else:
            log(f"VM {vm.vmid} ({vm.name}): {self.qm_path} start failed: {result.stderr.strip()}")

    def cleanup_stale_state(self, active_vmids: set[str]) -> None:
        if not self.state_dir.exists():
            return
        for path in self.state_dir.glob("*.json"):
            vmid = path.stem
            if vmid not in active_vmids:
                if self.dry_run:
                    log(f"[DRY] would clear stale state for unknown VM {vmid}")
                else:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass

    def get_lowest_autostart_order(self, vms: list[VMInfo], vm_configs: dict[str, VMConfig]) -> Optional[int]:
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

        return min(eligible_orders) if eligible_orders else None

    def process_vm(self, vm: VMInfo, config: VMConfig, lowest_autostart_order: Optional[int]) -> None:
        state = self.load_state(vm.vmid)

        if config.lock_value:
            log(f"VM {vm.vmid} ({vm.name}): skipped because lock={config.lock_value}", verbose_only=True)
            state.seen_good = False
            state.fail_count = 0
            state.auto_start_count = 0
            self.save_state(vm.vmid, state)
            return

        if vm.status == "running":
            self.process_running_vm(vm, config, state)
        elif vm.status in {"stopped", "shutdown"}:
            self.process_stopped_vm(vm, config, state, lowest_autostart_order)
        else:
            log(f"VM {vm.vmid} ({vm.name}): skipped because status={vm.status}", verbose_only=True)
            state.seen_good = False
            state.fail_count = 0
            state.auto_start_count = 0
            self.save_state(vm.vmid, state)

    def process_running_vm(self, vm: VMInfo, config: VMConfig, state: VMState) -> None:
        state.auto_start_count = 0

        if not config.agent_enabled:
            log(f"VM {vm.vmid} ({vm.name}): skipped because guest agent is not enabled", verbose_only=True)
            state.seen_good = False
            state.fail_count = 0
            self.save_state(vm.vmid, state)
            return

        if self.agent_ping(vm.vmid):
            if not state.seen_good or state.fail_count != 0:
                log(f"VM {vm.vmid} ({vm.name}): agent ping OK, watchdog armed")
            else:
                log(f"VM {vm.vmid} ({vm.name}): agent ping OK", verbose_only=True)

            state.seen_good = True
            state.fail_count = 0
            self.save_state(vm.vmid, state)
            return

        if not state.seen_good:
            log(f"VM {vm.vmid} ({vm.name}): agent ping failed, watchdog not armed yet")
            self.save_state(vm.vmid, state)
            return

        next_fail = state.fail_count + 1
        log(f"VM {vm.vmid} ({vm.name}): agent ping failed ({next_fail}/{self.fail_threshold})")

        if next_fail >= self.fail_threshold:
            if self.dry_run:
                log(f"[DRY] VM {vm.vmid} ({vm.name}): would clear seen_good/fail_count and reset VM")
                return

            state.seen_good = False
            state.fail_count = 0
            self.save_state(vm.vmid, state)
            self.reset_vm(vm)
            return

        state.fail_count = next_fail
        self.save_state(vm.vmid, state)

    def process_stopped_vm(self, vm: VMInfo, config: VMConfig, state: VMState, lowest_autostart_order: Optional[int]) -> None:
        state.seen_good = False
        state.fail_count = 0

        if not self.auto_start:
            log(f"VM {vm.vmid} ({vm.name}): skipped because status={vm.status}", verbose_only=True)
            state.auto_start_count = 0
            self.save_state(vm.vmid, state)
            return

        if not config.onboot:
            log(f"VM {vm.vmid} ({vm.name}): stopped but onboot is not enabled", verbose_only=True)
            state.auto_start_count = 0
            self.save_state(vm.vmid, state)
            return

        if lowest_autostart_order is None or config.startup_order != lowest_autostart_order:
            if lowest_autostart_order is not None:
                 log(f"VM {vm.vmid} ({vm.name}): waiting for lower startup order {lowest_autostart_order} before starting order {config.startup_order}", verbose_only=True)
            state.auto_start_count = 0
            self.save_state(vm.vmid, state)
            return

        next_auto = state.auto_start_count + 1
        log(f"VM {vm.vmid} ({vm.name}): stopped/shutdown with onboot enabled and startup order {config.startup_order} ({next_auto}/{self.auto_start_threshold})", verbose_only=True)

        if next_auto >= self.auto_start_threshold:
            if self.dry_run:
                log(f"[DRY] VM {vm.vmid} ({vm.name}): would clear auto_start_count and start VM")
                return

            state.auto_start_count = 0
            self.save_state(vm.vmid, state)
            self.start_vm(vm)
            return

        state.auto_start_count = next_auto
        self.save_state(vm.vmid, state)

    def run(self) -> int:
        try:
            all_vms = self.parse_qm_list()
        except Exception as exc:
            log(f"Watchdog error: {exc}")
            return 1

        vm_map = {vm.vmid: vm for vm in all_vms}
        if self.requested_vmids:
            selected_vms = []
            for vmid in self.requested_vmids:
                vm = vm_map.get(vmid)
                if vm:
                    selected_vms.append(vm)
                else:
                    log(f"VM {vmid}: not found in qm list")
        else:
            selected_vms = all_vms

        self.cleanup_stale_state({vm.vmid for vm in all_vms})
        vm_configs = {vm.vmid: self.get_vm_config(vm.vmid) for vm in selected_vms}
        lowest_autostart_order = self.get_lowest_autostart_order(selected_vms, vm_configs)

        for vm in selected_vms:
            self.process_vm(vm, vm_configs[vm.vmid], lowest_autostart_order)
        
        return 0

def setup_logging(verbose: bool, tag: str) -> None:
    global VERBOSE, LOGGER_TAG
    VERBOSE = verbose
    LOGGER_TAG = tag

def main() -> int:
    env_defaults = parse_env_file(DEFAULTS_FILE)
    
    # Early logging setup with defaults
    setup_logging(
        parse_bool(env_defaults.get("VERBOSE", str(DEFAULT_VERBOSE))),
        env_defaults.get("LOGGER_TAG", DEFAULT_LOGGER_TAG)
    )

    qm_path = env_defaults.get("QM_PATH", DEFAULT_QM_PATH)

    try:
        env_fail_count = parse_env_int(env_defaults, "FAIL_COUNT", DEFAULT_FAIL_THRESHOLD)
        env_auto_start = parse_bool(env_defaults.get("AUTO_START", str(DEFAULT_AUTO_START)))
        env_auto_start_count = parse_env_int(env_defaults, "AUTO_START_COUNT", DEFAULT_AUTO_START_COUNT)
        env_verbose = parse_bool(env_defaults.get("VERBOSE", str(DEFAULT_VERBOSE)))
        env_dry_run = parse_bool(env_defaults.get("DRY_RUN", str(DEFAULT_DRY_RUN)))
        env_vmids = [v for v in env_defaults.get("VMIDS", "").split() if v]
    except ValueError as exc:
        log(f"Invalid config in {DEFAULTS_FILE}: {exc}")
        return 1

    parser = argparse.ArgumentParser(description="Watchdog Proxmox VMs with qemu-guest-agent enabled.")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=env_dry_run,
                        help=f"Do not modify state and do not start/reset VMs (default: {env_dry_run})")
    parser.add_argument("--verbose", action=argparse.BooleanOptionalAction, default=env_verbose,
                        help=f"Log skips and successful checks too (default: {env_verbose})")
    parser.add_argument("--fail-count", type=int, default=env_fail_count,
                        help=f"Failed ping threshold before reset (default: {env_fail_count})")
    parser.add_argument("--auto-start", action=argparse.BooleanOptionalAction, default=env_auto_start,
                        help=f"Auto-start stopped VMs with onboot: 1 (default: {env_auto_start})")
    parser.add_argument("--auto-start-count", type=int, default=env_auto_start_count,
                        help=f"Consecutive stopped checks before auto-start (default: {env_auto_start_count})")
    parser.add_argument("--vmid", action="append", default=None,
                        help="Specific VMID to check. May be given multiple times. "
                             "If provided, replaces VMIDS from the env file.")
    
    args = parser.parse_args()

    setup_logging(args.verbose, env_defaults.get("LOGGER_TAG", DEFAULT_LOGGER_TAG))

    if not Path(qm_path).exists():
        log(f"QM_PATH does not exist: {qm_path}")
        return 1
    if not os.access(qm_path, os.X_OK):
        log(f"QM_PATH is not executable: {qm_path}")
        return 1

    config = {
        "dry_run": args.dry_run,
        "verbose": args.verbose,
        "fail_threshold": max(1, args.fail_count),
        "auto_start": args.auto_start,
        "auto_start_threshold": max(1, args.auto_start_count),
        "state_dir": env_defaults.get("STATE_DIR", DEFAULT_STATE_DIR),
        "qm_path": qm_path,
        "vmids": [str(v) for v in (args.vmid if args.vmid is not None else env_vmids)]
    }

    watchdog = Watchdog(config)
    return watchdog.run()

if __name__ == "__main__":
    sys.exit(main())
