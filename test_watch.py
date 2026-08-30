#!/usr/bin/env python3
"""
Offline test of the overnight observer.

Two things must hold. It must never write a register -- it is pointed at a
live plant for nine hours unattended, and the whole point is that it cannot
disturb what it is measuring. And the energy arithmetic must be right, because
the verdict it prints in the morning is what decides whether the controller is
worth deploying at all.

    python3 test_watch.py
"""

from __future__ import annotations

import sigen
import watch
from octopus import Slot
from datetime import datetime, timedelta, timezone
from sigen import SigenClient
from test_mock import MockPlant

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<56} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def close(label: str, got, want, tol=0.005) -> None:
    ok = got is not None and abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<56} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected ~{want}, got {got!r}")


def rows(*specs, start=None, step_min=30):
    """Build sample rows: (cheap, ess_kw, grid_kw). SOC is filler."""
    start = start or datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    out = []
    for i, (cheap, ess, grid) in enumerate(specs):
        t = start + timedelta(minutes=step_min * i)
        out.append({"utc": t.isoformat(), "local": "", "cheap": str(cheap),
                    "soc_pct": "50", "ess_kw": str(ess), "grid_kw": str(grid),
                    "pv_kw": "0", "ems_enable": "0", "ems_mode": "0",
                    "charge_limit_kw": "", "discharge_limit_kw": "",
                    "error": ""})
    return out


def main() -> int:
    sigen.MIN_REQUEST_INTERVAL = 0.0
    plant = MockPlant(
        holding={40029: 0, 40031: 0, 40032: 0, 40033: 0,
                 40034: 0, 40035: 0},
        input_regs={30014: 512, 30035: 0, 30036: 0,
                    30037: 0xFFFF, 30038: 0xF448,     # s32 -3000 -> -3.0 kW
                    30005: 0xFFFF, 30006: 0xF448},
    )
    plant.start()
    client = SigenClient("127.0.0.1", port=plant.port)
    client._throttle = lambda: None            # type: ignore[method-assign]
    client.connect()

    print("\nIt reads, and only reads")
    plant.writes.clear()
    now = datetime.now(timezone.utc)
    slot = Slot(now - timedelta(minutes=5), now + timedelta(hours=1),
                "off-peak")
    row = watch.read_sample(client, [slot])
    check("no registers written", len(plant.writes), 0)
    check("SOC decoded", row["soc_pct"], 51.2)
    check("ESS power decoded", row["ess_kw"], -3.0)
    check("inside a cheap slot is flagged", row["cheap"], 1)
    check("no error recorded", row["error"], "")
    row_out = watch.read_sample(client, [])
    check("outside any slot is flagged", row_out["cheap"], 0)

    print("\nA transport fault is recorded, never raised")
    plant.faults = 9
    row_bad = watch.read_sample(client, [slot])
    check("the run survives", isinstance(row_bad, dict), True)
    check("the error is captured", row_bad["error"] != "", True)
    check("still wrote nothing", len(plant.writes), 0)
    plant.faults = 0

    print("\nEnergy arithmetic (trapezoid over each interval)")
    # 30 min at a steady 4 kW charge, all inside a cheap period.
    a = watch.analyse(rows((1, 4.0, 4.0), (1, 4.0, 4.0)))
    close("2 kWh charged in half an hour at 4 kW", a["charged"][1], 2.0)
    check("and none outside the cheap window", a["charged"][0], 0.0)
    close("grid import counted the same way", a["imported"][1], 2.0)

    # Ramp 0 -> 4 kW over 30 min averages 2 kW, so 1 kWh.
    a = watch.analyse(rows((1, 0.0, 0.0), (1, 4.0, 4.0)))
    close("a ramp is averaged, not squared off", a["charged"][1], 1.0)

    # Discharging and exporting are counted separately.
    a = watch.analyse(rows((0, -6.0, -6.0), (0, -6.0, -6.0)))
    close("3 kWh discharged in half an hour at 6 kW",
          a["discharged"][0], 3.0)
    close("and 3 kWh exported", a["exported"][0], 3.0)
    check("nothing charged", a["charged"][0], 0.0)

    print("\nCheap and peak are kept apart")
    a = watch.analyse(rows((1, 4.0, 4.0), (1, 4.0, 4.0), (0, 4.0, 4.0),
                           (0, 4.0, 4.0)))
    close("cheap half", a["imported"][1], 4.0)
    close("peak half", a["imported"][0], 2.0)

    print("\nGaps and junk do not corrupt the totals")
    far = rows((1, 4.0, 4.0), (1, 4.0, 4.0), step_min=180)
    a = watch.analyse(far)
    check("an interval longer than an hour is skipped",
          a["charged"][1], 0.0)
    check("too few usable samples -> no analysis",
          watch.analyse(rows((1, 4.0, 4.0))), None)

    print("\nIt notices if something held a lease during the run")
    leased = rows((1, 4.0, 4.0), (1, 4.0, 4.0))
    leased[0]["ems_enable"] = "1"
    a = watch.analyse(leased)
    check("lease samples counted, so the verdict can be honest",
          a["ems_on"], 1)

    print("\n" + "=" * 68)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. It observes without disturbing, and the "
          "arithmetic holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
