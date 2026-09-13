#!/usr/bin/env python3
"""
Offline test of the energy dashboard. No network, no devices, no plant.

Three things are worth pinning, and only one of them is about pixels:

  - it must have NO WRITE PATH. It reads five Shelly devices, any of which is
    one HTTP call away from switching a socket, and the owner's standing
    instruction is that nothing gets switched without consent. Asserted
    against the source so it cannot appear by accident later.

  - it must never open a Modbus connection. Two readers on a link with a
    1s minimum request spacing means the agent that commands the battery
    gets starved by a browser tab someone left open. Also asserted against
    the source, because "we agreed not to" is not a mechanism.

  - every source must degrade rather than raise. A dead Shelly, an expired
    cloud token or a missing log should render as "unavailable", because a
    dashboard that 500s is a blank screen at exactly the moment you wanted
    to look at something.

    python3 test_dashboard.py
"""

from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path

import dashboard

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


SOURCE = io.open(Path(__file__).with_name("dashboard.py"),
                 encoding="utf-8").read()


def main() -> int:
    print("\nIt cannot control anything, and that is structural")
    # The Shelly RPC verbs that switch a load, and the plant's write verb.
    for forbidden in ("Switch.Set", "Switch.Toggle", "/relay/",
                      "write_register", "set_mode", "EM1.SetConfig",
                      "Sys.SetConfig", "Shelly.Reboot"):
        check(f"no {forbidden!r} anywhere in the source",
              forbidden in SOURCE, False)
    # Only GET handlers exist.
    check("serves no POST/PUT/DELETE handler",
          bool(re.search(r"def do_(POST|PUT|DELETE|PATCH)", SOURCE)), False)

    print("\nIt never talks Modbus -- the agent owns that link")
    for forbidden in ("import sigen\n", "SigenClient", "read_registers",
                      "from sigen import"):
        check(f"no {forbidden.strip()!r} in the source",
              forbidden in SOURCE, False)

    print("\nSolar is the sum of both inverters, whichever a plant has")
    flow = {"solar": 0.11 + 0.0, "solar_third": 0.11, "solar_mppt": 0.0,
            "load": 0.39, "battery": -9.1, "grid": 8.82, "soc": 30.1,
            "ev": 0.0, "heat_pump": 0.0, "solar_today": 9.89}
    html = dashboard.render({"at": dt.datetime(2026, 9, 13, 18, 5),
                             "sigen": flow, "shellys": [],
                             "agent": {"log": "x", "error": "FileNotFoundError"}}
                            ).decode()
    check("solar is rendered", "0.11 kW" in html, True)
    check("house load is shown as measured", "0.39 kW" in html, True)
    check("today's solar total appears", "9.89 kWh" in html, True)

    print("\nSigns are interpreted, not just printed")
    check("negative battery reads as discharging",
          "discharging" in html, True)
    check("positive grid reads as exporting", "exporting" in html, True)
    flipped = dict(flow, battery=4.0, grid=-2.0)
    html2 = dashboard.render({"at": dt.datetime(2026, 9, 13, 2, 0),
                              "sigen": flipped, "shellys": [],
                              "agent": {"log": "x", "error": "x"}}).decode()
    check("positive battery reads as charging", "charging" in html2, True)
    check("negative grid reads as importing", "importing" in html2, True)
    check("and the magnitude is shown unsigned", "2.00 kW" in html2, True)

    print("\nZero-power devices are not dressed up as active")
    check("an idle EV is omitted rather than shown as 0",
          "EV" in html and "0.00 kW" in html, False)

    print("\nEvery source can fail without taking the page with it")
    broken = dashboard.render({
        "at": dt.datetime(2026, 9, 13, 18, 5),
        "sigen": {"error": "TimeoutError: cloud"},
        "shellys": [{"host": "192.168.2.43", "error": "URLError",
                     "channels": []}],
        "agent": {"log": "observe.log", "error": "FileNotFoundError"},
    }).decode()
    check("a dead cloud says so", "Sigen cloud unavailable" in broken, True)
    check("a dead Shelly says so", "unreachable" in broken, True)
    check("a missing log says so", "No agent log" in broken, True)
    check("and the page still renders", broken.startswith("<!doctype"), True)

    print("\nA stale agent is called out, because silence looks like calm")
    fresh = {"log": "observe.log", "slots": [],
             "last_seen": dt.datetime.now(), "action": "idle"}
    stale = {"log": "observe.log", "slots": [],
             "last_seen": dt.datetime.now() - dt.timedelta(hours=3),
             "action": "idle"}
    check("a recent tick is not flagged",
          "STALE" in dashboard.agent_block(fresh), False)
    check("a three-hour-old tick is flagged",
          "STALE" in dashboard.agent_block(stale), True)

    print("\nAn open slot is visible, because that is money in motion")
    slot_open = {"log": "x", "slots": [
        {"start": dt.datetime(2026, 9, 13, 21, 0), "end": None,
         "soc_start": 12.0, "soc_end": None}], "holding": True}
    check("an unclosed slot renders as still open",
          "still open" in dashboard.agent_block(slot_open), True)
    banner = dashboard.render({"at": dt.datetime.now(), "sigen": flow,
                               "shellys": [], "agent": slot_open}).decode()
    check("and the page banners the cheap rate",
          "off-peak rate" in banner, True)

    print("\nShelly parsing, both generations, without a device")
    gen2 = {"host": "h", "name": "Freezer", "gen": 2, "model": "SNPL",
            "channels": [{"id": "switch:0", "kind": "switch", "on": True,
                          "watts": 162.2, "kwh": 214.99}]}
    row = dashboard.shelly_rows([gen2])
    check("gen2 switch shows watts", "162 W" in row, True)
    check("and its state", ">on<" in row, True)
    gen1 = {"host": "h", "name": "Kitchen right", "gen": 1, "model": "SHSW-25",
            "channels": [{"id": "relay:0", "kind": "switch", "on": False,
                          "watts": 0.0, "kwh": 0.34},
                         {"id": "relay:1", "kind": "switch", "on": False,
                          "watts": 0.0, "kwh": 0.88}]}
    row1 = dashboard.shelly_rows([gen1])
    check("a two-relay gen1 device renders both channels",
          row1.count("<tr>"), 2)
    check("and disambiguates them", "relay:0" in row1 and "relay:1" in row1, True)
    check("no devices at all is not a crash",
          "none configured" in dashboard.shelly_rows([]), True)

    print("\nFormatting helpers")
    check("None power is a dash, not 0", dashboard.kw(None), "&mdash;")
    check("None watts is a dash", dashboard.watts(None), "&mdash;")
    check("kW is 2dp", dashboard.kw(1.2345), "1.23 kW")
    check("watts are whole numbers", dashboard.watts(162.24), "162 W")

    print("\nThe page is self-contained -- it has to work over an SSH tunnel")
    check("no external scripts", "<script" in html, False)
    check("no CDN references", "http://" in html.replace("http-equiv", ""), False)

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. It observes, and it cannot command anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
