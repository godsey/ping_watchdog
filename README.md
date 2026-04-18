# ping_watchdog

A small Python script that watches the QEMU guest-agent ping on a standalone
Proxmox VE host, resets VMs that stop responding, and optionally restarts VMs
that should be up but aren't.

Not tested against a Proxmox HA cluster. If you use HA, the cluster already
handles fencing and restarts, and this will fight with it. Run it only on
single-node Proxmox hosts.

## What it does

Once a minute, a systemd timer runs the script. It shells out to `qm list` and
walks each VM.

For running VMs that have the guest agent enabled, it calls
`qm agent <vmid> ping`. A VM has to reply successfully at least once before
the watchdog is "armed" for that VM. That arming step is deliberate: it keeps
the script from resetting a VM that's still booting, or one where the guest
agent was never installed. Once armed, `FAIL_COUNT` failed pings in a row
(default 3) trigger a `qm reset`.

If `AUTO_START` is on, the script also looks at VMs that are stopped or shut
down and have `onboot: 1` in their config. After `AUTO_START_COUNT`
consecutive checks in that state (default 5) it runs `qm start`. Startup
order is respected: only the lowest `order=` group is considered on each run,
so higher-order groups wait their turn.

VMs with a `lock:` set are skipped entirely. Per-VM state lives in
`/run/qm-agent-watchdog/` as one JSON file per VMID. `/run` is tmpfs, so a
reboot wipes the state and the arming step starts over. That's intentional.

## Requirements

- Proxmox VE, standalone (not HA)
- Python 3.9 or newer (what ships with Debian 12, which Proxmox 8 is based on)
- `qemu-guest-agent` installed and running inside each VM you want watched

## Install

```
sudo ./install.sh
```

The installer copies the script to `/root/ping_watchdog.py`, the env file to
`/etc/default/ping_watchdog`, and the two systemd units to
`/etc/systemd/system/`. It then reloads systemd and enables the timer.

If the host doesn't look like Proxmox (no `qm`, no `/etc/pve`, no
`pveversion`), the installer bails out. Pass `--force` to override that
check.

## Configure

All tunables live in `/etc/default/ping_watchdog`. The file is heavily
commented. Edit it, then:

```
systemctl restart ping_watchdog.timer
```

The knobs that tend to matter:

- `FAIL_COUNT` — failed pings in a row before a running VM is reset.
- `AUTO_START` and `AUTO_START_COUNT` — whether to start stopped VMs that
  have `onboot: 1`, and how long they have to stay down before starting.
- `VERBOSE` — print every check, not just the interesting ones. Useful
  while you're dialing things in.
- `DRY_RUN` — log what it would do, without actually resetting or starting
  anything. Leave this on for the first run.
- `VMIDS` — space-separated list of VMIDs if you only want to watch a
  subset. Blank means all VMs.

The same options exist as command-line flags (`--dry-run`, `--verbose`,
`--fail-count`, `--auto-start`, `--auto-start-count`, `--vmid`). The flags
override the env file, so running the script by hand for testing doesn't
require touching `/etc/default/ping_watchdog`.

## Watching the logs

```
systemctl status ping_watchdog.timer
journalctl -t qm-agent-watchdog -f
journalctl -u ping_watchdog.service -n 50
```

The tag for `logger` is `qm-agent-watchdog` by default and is configurable
via `LOGGER_TAG`. The service journal is always available under the unit
name.

## First run

During the first few cycles the watchdog isn't going to reset anything even
with `DRY_RUN=false`. Every VM has to pass one good ping before the arming
flag flips on. If a VM never passes (agent not running, missing virtio-serial,
networking issue, and so on) the watchdog stays disarmed and leaves that VM
alone.

I'd recommend flipping `VERBOSE=true` and `DRY_RUN=true` for the first day or
so, skimming `journalctl -t qm-agent-watchdog`, and then turning both off
once the output looks right.

## Uninstall

There's no uninstall script. It's four files:

```
systemctl disable --now ping_watchdog.timer
rm /etc/systemd/system/ping_watchdog.timer
rm /etc/systemd/system/ping_watchdog.service
rm /etc/default/ping_watchdog
rm /root/ping_watchdog.py
systemctl daemon-reload
```

The state directory under `/run/` clears itself on reboot and doesn't need
removing.

## License

MIT. See `LICENSE.txt`.
