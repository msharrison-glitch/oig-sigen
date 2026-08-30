#!/usr/bin/env python3
"""
Commanded control of a SigenStor over Modbus TCP, with a guaranteed release.

The SigenStor has NO Modbus watchdog: once Remote EMS is enabled and a mode
is latched, it stays latched until something changes it. If this process dies
holding a charge command, the battery keeps importing -- straight into the
peak rate. So every command here is a *lease*:

  * the intent and its deadline are written to a state file BEFORE any
    register is touched, so a separate process can always clean up;
  * release is wired to normal exit, exceptions, SIGINT and SIGTERM;
  * `release` and `--deadman` are idempotent and safe to run at any time.

Releasing means writing 40029 = 0, which disables Remote EMS and hands the
plant back to its own configured EMS work mode (Sigen AI, TOU, whatever you
have set in the app). We deliberately do NOT force mode 2, because that
would override your own configuration.

The host may be omitted, in which case SIGEN_HOST from .env is used.

Usage:
    python3 control.py 192.168.2.53 status
    python3 control.py status                        (host from .env)
    python3 control.py 192.168.2.53 standby --minutes 2
    python3 control.py 192.168.2.53 charge --kw 5 --minutes 10
    python3 control.py 192.168.2.53 release
    python3 control.py 192.168.2.53 --deadman        (for cron)
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import registers as R
from config import ConfigError, resolve_host
from sigen import PLANT_UNIT_ID, ModbusError, SigenClient

STATE_FILE = Path(__file__).with_name(".lease.json")

# Hard ceiling on any single lease, regardless of what's asked for.
MAX_LEASE_MINUTES = 120

# Above this SOC a charge command can't be distinguished from a full battery.
CHARGE_SOC_CEILING = 95.0

# Below this SOC a discharge command is both unhelpful and impolite to the
# battery. Same reasoning in the other direction.
DISCHARGE_SOC_FLOOR = 25.0

CHARGE_MODES = (R.EMS_COMMAND_CHARGE_GRID_FIRST,
                R.EMS_COMMAND_CHARGE_PV_FIRST)
DISCHARGE_MODES = (R.EMS_COMMAND_DISCHARGE_PV_FIRST,
                   R.EMS_COMMAND_DISCHARGE_ESS_FIRST)


def limit_register(mode: int):
    """Modes 3-6 each have a power limit; which register depends on
    direction. Modes 0-2 take no limit."""
    if mode in CHARGE_MODES:
        return R.ESS_MAX_CHARGE_LIMIT
    if mode in DISCHARGE_MODES:
        return R.ESS_MAX_DISCHARGE_LIMIT
    return None

SAMPLE_INTERVAL = 10.0


# --------------------------------------------------------------------------
# state file
# --------------------------------------------------------------------------

def write_state(payload: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, STATE_FILE)


def read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def clear_state() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# lease
# --------------------------------------------------------------------------

class Lease:
    """Holds a Remote EMS command and guarantees it is given back."""

    def __init__(self, client: SigenClient, log=print) -> None:
        self.client = client
        self.log = log
        self.original_mode: int | None = None
        self.original_limits: dict[int, int] = {}
        self.held = False
        self._released = False

    def snapshot(self) -> dict:
        # The limits are captured raw, because they MUST be given back. A
        # limit left behind is honoured even with Remote EMS disabled -- a
        # 3 kW discharge limit from a test capped this plant's export at
        # 3 kW for days. Verified 2026-08-30: 40029=0, 40034=3 kW,
        # ESS power pinned at -3.00 kW.
        return {
            "remote_ems_enable": self.client.read_u16(
                R.REMOTE_EMS_ENABLE.address),
            "remote_ems_mode": self.client.read_u16(
                R.REMOTE_EMS_MODE.address),
            "limits": {
                R.ESS_MAX_CHARGE_LIMIT.address: self.client.read_u32(
                    R.ESS_MAX_CHARGE_LIMIT.address),
                R.ESS_MAX_DISCHARGE_LIMIT.address: self.client.read_u32(
                    R.ESS_MAX_DISCHARGE_LIMIT.address),
            },
        }

    def acquire(self, mode: int, power_kw: float | None,
                minutes: int) -> None:
        before = self.snapshot()
        self.original_mode = before["remote_ems_mode"]
        self.original_limits = before["limits"]

        if before["remote_ems_enable"] == 1:
            self.log("  ! Remote EMS was ALREADY enabled before we started.")
            self.log("    Something else may be controlling this plant.")

        deadline = datetime.now(timezone.utc) + timedelta(minutes=minutes)

        # State first: if we die between here and the writes, the deadman
        # still knows to release.
        write_state({
            "host": self.client.host,
            "mode": mode,
            "mode_name": R.EMS_MODE_NAMES.get(mode, "?"),
            "power_kw": power_kw,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": deadline.isoformat(),
            "original_mode": self.original_mode,
            "original_limits": {str(k): v
                                for k, v in self.original_limits.items()},
            "pid": os.getpid(),
        })

        # Order matters: configure the mode and its limits BEFORE enabling,
        # so that enabling never briefly applies a stale mode. 40031 is
        # currently 0 (PCS remote control) on this plant, which we do not
        # want applied even for an instant.
        limit_reg = limit_register(mode)
        if power_kw is not None and limit_reg is not None:
            raw = int(round(power_kw * limit_reg.gain))
            self.client.write_u32(limit_reg.address, raw)
            self.log(f"  wrote {limit_reg.address} {limit_reg.name} "
                  f"= {power_kw:.2f} kW")

        self.client.write_u16(R.REMOTE_EMS_MODE.address, mode)
        self.log(f"  wrote 40031 mode = {mode} "
              f"({R.EMS_MODE_NAMES.get(mode, '?')})")

        self.client.write_u16(R.REMOTE_EMS_ENABLE.address, 1)
        self.held = True
        self.log("  wrote 40029 remote EMS enable = 1  -- LEASE HELD")

    def renew(self, minutes: int) -> None:
        """Push the lease deadline out without touching a single register.

        This is the heartbeat: reconcile.py calls it every tick, so a loop
        that dies simply stops renewing and the cron deadman releases within
        `minutes`. It is also how a six-hour off-peak window is covered
        without ever exceeding MAX_LEASE_MINUTES -- the lease stays short and
        is rolled forward, rather than being taken out long.
        """
        if not self.held or self._released:
            return
        state = read_state() or {}
        now = datetime.now(timezone.utc)
        state.update({
            "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
            "renewed_at": now.isoformat(),
            "pid": os.getpid(),
        })
        write_state(state)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if not self.held:
            clear_state()
            return
        self.log("\n  releasing...")
        try:
            self.client.write_u16(R.REMOTE_EMS_ENABLE.address, 0)
            self.log("  wrote 40029 remote EMS enable = 0 "
                  "-- plant back on its own EMS")
            if self.original_mode is not None:
                self.client.write_u16(
                    R.REMOTE_EMS_MODE.address, self.original_mode)
                self.log(f"  restored 40031 mode = {self.original_mode}")
            # Limits last: 40029 = 0 is the safety-critical write and has
            # already happened, so a failure here cannot leave the plant
            # under our control -- only with a limit still applied, which the
            # deadman and a later `release` will also try to undo.
            restore_limits(self.client, self.original_limits, self.log)
        except (ModbusError, OSError) as exc:
            self.log(f"\n  *** RELEASE FAILED: {exc}")
            self.log("  *** The plant may still be under Remote EMS control.")
            self.log(f"  *** Run:  python3 {Path(__file__).name} "
                  f"{self.client.host} release")
            raise
        else:
            clear_state()
            self.log("  released cleanly.")


def restore_limits(client: SigenClient, limits: dict, log=print) -> None:
    """Give back the ESS power limits.

    Not cosmetic. A limit written during a lease stays in force after
    release, with Remote EMS disabled, and silently caps the plant's own EMS.
    """
    for address, raw in (limits or {}).items():
        address = int(address)
        try:
            if client.read_u32(address) == int(raw):
                continue
            client.write_u32(address, int(raw))
            shown = "unset" if int(raw) == R.U32_UNSET else \
                f"{int(raw) / 1000:.2f} kW"
            log(f"  restored {address} limit = {shown}")
        except (ModbusError, OSError) as exc:
            log(f"  ! could not restore limit {address}: {exc}")


# --------------------------------------------------------------------------
# display
# --------------------------------------------------------------------------

def fmt(value, unit: str = "", places: int = 2) -> str:
    if value is None:
        return "unset"
    if isinstance(value, float):
        return f"{value:.{places}f}{unit}"
    return f"{value}{unit}"


def print_live(client: SigenClient, prefix: str = "  ") -> None:
    soc = R.read(client, R.ESS_SOC)
    ess = R.read(client, R.ESS_POWER)
    pv = R.read(client, R.PV_POWER)
    grid = R.read(client, R.GRID_ACTIVE_POWER)
    stamp = datetime.now().strftime("%H:%M:%S")
    direction = ""
    if isinstance(ess, float):
        direction = " (charging)" if ess > 0.05 else (
            " (discharging)" if ess < -0.05 else " (idle)")
    print(f"{prefix}{stamp}  SOC {fmt(soc, '%', 1):>7}   "
          f"battery {fmt(ess, ' kW'):>9}{direction:<14} "
          f"PV {fmt(pv, ' kW'):>8}   grid {fmt(grid, ' kW'):>9}")


def cmd_status(client: SigenClient) -> int:
    enable = client.read_u16(R.REMOTE_EMS_ENABLE.address)
    mode = client.read_u16(R.REMOTE_EMS_MODE.address)
    print(f"\n  Remote EMS enable (40029): {enable} "
          f"({'ENABLED' if enable else 'disabled'})")
    print(f"  Remote EMS mode   (40031): {mode} "
          f"({R.EMS_MODE_NAMES.get(mode, '?')})")
    limit = R.read(client, R.ESS_MAX_CHARGE_LIMIT)
    print(f"  Max charge limit  (40032): {fmt(limit, ' kW')}")
    print()
    print_live(client)

    state = read_state()
    if state:
        expires = datetime.fromisoformat(state["expires_at"])
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        print(f"\n  Lease file present: {state['mode_name']}, "
              f"{'EXPIRED' if remaining < 0 else f'{remaining:.0f}s left'}")
        if remaining < 0:
            print("  -> stale lease. Run 'release' to be sure.")
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_hold(client: SigenClient, mode: int, power_kw: float | None,
             minutes: int, assume_yes: bool) -> int:
    if minutes > MAX_LEASE_MINUTES:
        print(f"Refusing: {minutes} min exceeds the "
              f"{MAX_LEASE_MINUTES} min ceiling.")
        return 2

    soc = R.read(client, R.ESS_SOC)
    if not assume_yes and isinstance(soc, float):
        if mode in CHARGE_MODES and soc >= CHARGE_SOC_CEILING:
            print(f"\nSOC is {soc:.1f}% -- at or above the "
                  f"{CHARGE_SOC_CEILING}% ceiling.")
            print("A charge command here can't be told apart from a full")
            print("battery. Use --yes to override if you meant it.")
            return 2
        if mode in DISCHARGE_MODES and soc <= DISCHARGE_SOC_FLOOR:
            print(f"\nSOC is {soc:.1f}% -- at or below the "
                  f"{DISCHARGE_SOC_FLOOR}% floor.")
            print("Use --yes to override if you meant it.")
            return 2

    print(f"\n  Plan: {R.EMS_MODE_NAMES.get(mode, '?')}"
          + (f" at up to {power_kw} kW" if power_kw else "")
          + f", for {minutes} minute(s).")
    print(f"  Current SOC: {fmt(soc, '%', 1)}")
    print(f"  Release on exit: 40029 -> 0 (plant returns to its own EMS)")

    if not assume_yes:
        try:
            if input("\n  Proceed? [y/N] ").strip().lower() != "y":
                print("  aborted.")
                return 1
        except EOFError:
            print("  no tty and --yes not given; aborting.")
            return 1

    lease = Lease(client)
    lease_power = power_kw

    def on_signal(signum, _frame):
        print(f"\n  caught signal {signum}")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    atexit.register(lease.release)

    print("\n  Baseline before command:")
    print_live(client)

    print("\n  Acquiring lease:")
    try:
        lease.acquire(mode, lease_power, minutes)

        deadline = time.monotonic() + minutes * 60
        print(f"\n  Holding until "
              f"{(datetime.now() + timedelta(minutes=minutes)):%H:%M:%S}. "
              f"Ctrl-C releases early.\n")
        while time.monotonic() < deadline:
            print_live(client)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(SAMPLE_INTERVAL, remaining))
    except KeyboardInterrupt:
        print("\n  interrupted -- releasing early")
    finally:
        lease.release()

    print("\n  State after release:")
    print_live(client)
    return 0


def cmd_release(client: SigenClient) -> int:
    """Idempotent. Safe to run at any time, whether or not we hold a lease."""
    enable = client.read_u16(R.REMOTE_EMS_ENABLE.address)
    if enable == 0:
        print("  Remote EMS already disabled -- nothing to release.")
        clear_state()
        return 0
    print(f"  Remote EMS is ENABLED. Disabling.")
    client.write_u16(R.REMOTE_EMS_ENABLE.address, 0)
    state = read_state()
    if state and state.get("original_mode") is not None:
        client.write_u16(R.REMOTE_EMS_MODE.address, state["original_mode"])
        print(f"  restored 40031 mode = {state['original_mode']}")
    if state and state.get("original_limits"):
        restore_limits(client, state["original_limits"])
    clear_state()
    print("  released.")
    return 0


def cmd_clear_limits(client: SigenClient) -> int:
    """Return both ESS power limits to 'never configured'.

    Remediation for a limit left behind by an earlier lease. Safe at any
    time: it removes a cap, it never imposes one.
    """
    for reg in (R.ESS_MAX_CHARGE_LIMIT, R.ESS_MAX_DISCHARGE_LIMIT):
        current = R.read(client, reg)
        if current is None:
            print(f"  {reg.address} {reg.name}: already unset")
            continue
        print(f"  {reg.address} {reg.name}: {current:.2f} kW -> unset")
        client.write_u32(reg.address, R.U32_UNSET)
        after = R.read(client, reg)
        print(f"    readback: {'unset' if after is None else f'{after:.2f} kW'}")
    return 0


def cmd_deadman(client: SigenClient) -> int:
    """For cron. Releases only if a lease exists and has expired."""
    state = read_state()
    if not state:
        return 0
    expires = datetime.fromisoformat(state["expires_at"])
    if datetime.now(timezone.utc) <= expires:
        return 0
    print(f"DEADMAN: lease expired at {expires.isoformat()} -- releasing")
    return cmd_release(client)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?",
                        help="SigenStor LAN address "
                             "(default: SIGEN_HOST from .env)")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=PLANT_UNIT_ID)
    parser.add_argument("--deadman", action="store_true",
                        help="release only if a lease exists and has expired")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")
    sub.add_parser("release")
    sub.add_parser("clear-limits")

    p_standby = sub.add_parser("standby")
    p_standby.add_argument("--minutes", type=int, default=2)
    p_standby.add_argument("--yes", action="store_true")

    p_charge = sub.add_parser("charge")
    p_charge.add_argument("--kw", type=float, required=True)
    p_charge.add_argument("--minutes", type=int, default=10)
    p_charge.add_argument("--yes", action="store_true")

    p_discharge = sub.add_parser("discharge")
    p_discharge.add_argument("--kw", type=float, required=True)
    p_discharge.add_argument("--minutes", type=int, default=10)
    p_discharge.add_argument("--yes", action="store_true")

    args = parser.parse_args()

    try:
        host = resolve_host(args.host)
        with SigenClient(host, port=args.port, unit_id=args.unit) as c:
            if args.deadman:
                return cmd_deadman(c)
            if args.command == "status" or args.command is None:
                return cmd_status(c)
            if args.command == "release":
                return cmd_release(c)
            if args.command == "clear-limits":
                return cmd_clear_limits(c)
            if args.command == "standby":
                return cmd_hold(c, R.EMS_STANDBY, None,
                                args.minutes, args.yes)
            if args.command == "charge":
                return cmd_hold(c, R.EMS_COMMAND_CHARGE_GRID_FIRST,
                                args.kw, args.minutes, args.yes)
            if args.command == "discharge":
                return cmd_hold(c, R.EMS_COMMAND_DISCHARGE_ESS_FIRST,
                                args.kw, args.minutes, args.yes)
    except (ModbusError, OSError, ConfigError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
