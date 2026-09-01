#!/usr/bin/env python3
"""
Read-only probe of a SigenStor over Modbus TCP.

Writes NOTHING. Safe to run at any time. The point is to confirm, before we
command anything, that:

  1. The plant answers on port 502 as unit 247.
  2. The V2.7 register addresses match your firmware.
  3. Remote EMS is currently disabled and the plant is in a normal mode.

The host may be omitted, in which case SIGEN_HOST from .env is used.

Usage:
    python3 probe.py 192.168.1.100
    python3 probe.py                                 (host from .env)
    python3 probe.py 192.168.1.100 --scan 40020 40060
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

import registers as R
from config import ConfigError, resolve_host
from sigen import PLANT_UNIT_ID, ModbusError, SigenClient


def check_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """Plain TCP connect, so a network problem is distinguishable from a
    Modbus problem."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        print(f"  cannot open TCP {host}:{port} -- {exc}")
        return False


def format_value(reg: R.Register, value) -> str:
    if value is None:
        return "unset (0xFFFFFFFF -- never configured)"
    if reg.address == R.REMOTE_EMS_MODE.address:
        name = R.EMS_MODE_NAMES.get(int(value), "UNKNOWN")
        return f"{int(value)} ({name})"
    if reg.address == R.REMOTE_EMS_ENABLE.address:
        return f"{int(value)} ({'ENABLED' if value else 'disabled'})"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f} {reg.unit}".strip()
    return f"{int(value)} {reg.unit}".strip()


def probe(host: str, port: int, unit: int) -> int:
    print(f"\nSigenStor probe -- {host}:{port} unit {unit}")
    print("=" * 62)

    print("\n[1] TCP reachability")
    if not check_reachable(host, port):
        print("\n  FAILED. Check the IP, and that Modbus TCP is enabled on")
        print("  the SigenStor. Nothing else can work until this passes.")
        return 1
    print(f"  OK -- {host}:{port} accepted a connection")

    print("\n[2] Register reads")
    print(f"  (1s minimum between requests, so this takes ~"
          f"{len(R.PROBE_SET)}s)\n")

    failures = 0
    with SigenClient(host, port=port, unit_id=unit) as client:
        for reg in R.PROBE_SET:
            kind = "holding" if reg.holding else "input  "
            label = f"  {reg.address}  {kind}  {reg.name:<38}"
            try:
                value = R.read(client, reg)
            except ModbusError as exc:
                print(f"{label} !! {exc}")
                failures += 1
            except OSError as exc:
                print(f"{label} !! transport error: {exc}")
                failures += 1
            else:
                print(f"{label} {format_value(reg, value)}")

    print("\n" + "=" * 62)
    if failures:
        print(f"{failures} of {len(R.PROBE_SET)} registers failed to read.")
        print("If these are all 'Illegal data address', the firmware uses a")
        print("different map than V2.7 and we need to re-derive it.")
        return 1

    print("All registers read cleanly. The V2.7 map matches this firmware.")
    return 0


def scan(host: str, port: int, unit: int, start: int, end: int) -> int:
    """Dump a range of holding registers one at a time, to identify
    addresses the document doesn't cover or that differ on this firmware."""
    print(f"\nScanning holding registers {start}..{end} "
          f"(~{end - start + 1}s)\n")
    with SigenClient(host, port=port, unit_id=unit) as client:
        for address in range(start, end + 1):
            try:
                value = client.read_u16(address, holding=True)
            except ModbusError as exc:
                print(f"  {address}  --  {exc}")
            except OSError as exc:
                print(f"  {address}  --  transport error: {exc}")
            else:
                print(f"  {address}  =  {value}  (0x{value:04X})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?",
                        help="SigenStor LAN address "
                             "(default: SIGEN_HOST from .env)")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=PLANT_UNIT_ID)
    parser.add_argument(
        "--scan", nargs=2, type=int, metavar=("START", "END"),
        help="dump a range of holding registers instead of the probe set",
    )
    args = parser.parse_args()

    started = time.time()
    try:
        host = resolve_host(args.host)
        if args.scan:
            rc = scan(host, args.port, args.unit, *args.scan)
        else:
            rc = probe(host, args.port, args.unit)
    except ConfigError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    print(f"\nfinished in {time.time() - started:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
