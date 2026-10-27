#!/usr/bin/env python3
"""Read-only: plant power registers that might carry solar and house load.

Reads INPUT registers only and writes nothing. Run it on the host that can
reach the plant (the NAS), ideally around midday when the panels are
producing, so a solar register is distinguishable from one that is simply
zero at night:

    ssh nas 'cd ~/oig-sigen && /usr/local/bin/python3.9 probe_power.py'

Candidates come from the community Home Assistant integration's definitions
(TypQxQ/Sigenergy-Local-Modbus); none has been verified on this plant beyond
a single night-time read. Each is a signed 32-bit value, gain 1000, in kW.
"""

import sys
import time

import config
import sigen

REGISTERS = (
    ("grid (30005)", 30005),
    ("ESS (30037)", 30037),
    ("third-party PV (30194)", 30194),
    ("general load (30282)", 30282),
    ("total load (30284)", 30284),
)


def read_kw(client, address):
    high, low = client.read_registers(address, 2, holding=False)
    value = (high << 16) | low
    if value & 0x80000000:
        value -= 1 << 32
    return value / 1000.0


def main() -> int:
    client = sigen.SigenClient(config.resolve_host(None))
    client.connect()
    try:
        print(time.strftime("%Y-%m-%d %H:%M:%S"))
        values = {}
        for name, address in REGISTERS:
            try:
                values[address] = read_kw(client, address)
                print(f"  {name:<26} {values[address]:+8.3f} kW")
            except Exception as exc:                  # noqa: BLE001
                print(f"  {name:<26} ERROR {exc!r}")
        grid, ess, load = (values.get(a) for a in (30005, 30037, 30284))
        if None not in (grid, ess, load):
            # Positive grid = importing, positive ESS = charging.
            print(f"  grid - ESS - load          {grid - ess - load:+8.3f} kW"
                  f"  (conversion losses, if 30284 is not derived)")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
