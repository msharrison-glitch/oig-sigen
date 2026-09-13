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

    print("\nLabels, because the Shelly app keeps names in the cloud")
    # sys.device.name reads null over the LAN when you name a device in the
    # app rather than its own web UI, so config has to be able to override.
    labels = {"192.168.2.149": "Fridge",
              "192.168.2.191": {"em1:0": "Hob & oven",
                                "em1:2": "Office AC"}}
    plug = {"host": "192.168.2.149", "name": None, "model": "SNPL-00112UK",
            "channels": [{"id": "switch:0", "kind": "switch", "on": True,
                          "watts": 39.9}]}
    check("config names an unnamed device",
          dashboard.label_for(plug, "switch:0", labels), "Fridge")
    named_device = dict(plug, host="10.0.0.9", name="Set on the device")
    check("a device's own name is used when config is silent",
          dashboard.label_for(named_device, "switch:0", labels),
          "Set on the device")
    bare = {"host": "10.0.0.8", "name": None, "model": None, "channels": []}
    check("and the IP is the last resort",
          dashboard.label_for(bare, "x", labels), "10.0.0.8")

    em = {"host": "192.168.2.191", "name": None, "model": "SPEM-003CEBEU",
          "channels": [{"id": "em1:0", "kind": "meter", "watts": -1.3},
                       {"id": "em1:1", "kind": "meter", "watts": 8.2},
                       {"id": "em1:2", "kind": "meter", "watts": 7.9}]}
    check("a 3EM clamp gets its own name",
          dashboard.label_for(em, "em1:0", labels), "Hob & oven")
    rows = dashboard.shelly_rows([em], labels)
    check("labelled clamps render by name", "Hob &amp; oven" in rows, True)
    check("an unlabelled clamp still shows its id",
          "em1:1" in rows, True)
    check("a labelled single-channel plug is not suffixed with its id",
          "switch:0" in dashboard.shelly_rows([plug], labels), False)
    check("a missing labels file is not fatal",
          dashboard.load_labels("/nonexistent/x.json"), {})

    print("\nSparklines are inline SVG -- no library, no CDN")
    series = [(f"t{i}", v) for i, v in enumerate([1, 3, 2, 5, 4, 6])]
    svg = dashboard.sparkline(series)
    check("produces an svg", svg.startswith("<svg"), True)
    check("with a polyline", "<polyline" in svg, True)
    check("and no external reference", "http" in svg, False)
    check("one point is not a line",
          "not enough data" in dashboard.sparkline([("t", 1)]), True)
    check("an empty series says so",
          "not enough data" in dashboard.sparkline([]), True)
    check("a flat series does not divide by zero",
          dashboard.sparkline([("a", 5), ("b", 5)]).startswith("<svg"), True)

    print("\nThe tariff block answers 'was it cheap when we charged?'")
    tariff = {"BUY_TARIFF": [("20260913 00:00", 0.0449),
                             ("20260913 12:00", 0.2976),
                             ("20260913 23:55", 0.0449)],
              "SOC": [("20260913 00:00", 1.0), ("20260913 23:55", 97.5)]}
    block = dashboard.tariff_block(tariff)
    check("prices are shown in pence", "4.49p" in block, True)
    check("and the peak too", "29.76p" in block, True)
    check("SOC is a percentage", "97.5%" in block, True)
    check("an unavailable series degrades",
          "unavailable" in dashboard.tariff_block({"error": "TimeoutError"}),
          True)
    check("no tariff data at all is empty, not broken",
          dashboard.tariff_block({}), "")

    print("\nPlug history is DIFFERENCED, because a plug only counts upwards")
    # A Shelly plug exposes a lifetime kWh counter and nothing else -- no
    # yesterday, no last week. Per-period consumption can only come from the
    # difference between two recorded readings, which is why record_shellys
    # has to run on a timer and why an hour unrecorded is unrecoverable.
    t0 = dt.datetime(2026, 9, 13, 0, 0)
    hist = [{"_t": t0 + dt.timedelta(hours=h), "host": "h1",
             "channel": "switch:0", "kwh": 100.0 + h * 0.5}
            for h in range(5)]
    use = dashboard.shelly_usage(hist, t0, t0 + dt.timedelta(hours=4))
    check("consumption is last minus first",
          round(use[("h1", "switch:0")], 2), 2.0)
    half = dashboard.shelly_usage(hist, t0, t0 + dt.timedelta(hours=2))
    check("a shorter window gives less",
          round(half[("h1", "switch:0")], 2), 1.0)
    check("a window outside the data yields nothing",
          dashboard.shelly_usage(hist, t0 - dt.timedelta(days=2),
                                 t0 - dt.timedelta(days=1)), {})
    check("a single reading cannot be differenced",
          dashboard.shelly_usage(hist[:1], t0, t0 + dt.timedelta(hours=4)), {})

    # A counter that goes backwards means the device was reset or swapped.
    # The honest answer is "unknown" -- not a negative, and definitely not a
    # huge positive from treating the wrap as real consumption.
    reset = [{"_t": t0, "host": "h1", "channel": "switch:0", "kwh": 900.0},
             {"_t": t0 + dt.timedelta(hours=1), "host": "h1",
              "channel": "switch:0", "kwh": 0.4}]
    check("a counter reset is dropped, not reported as negative",
          dashboard.shelly_usage(reset, t0, t0 + dt.timedelta(hours=2)), {})

    check("channels are kept apart",
          len(dashboard.shelly_usage(hist + [
              {"_t": t0, "host": "h1", "channel": "switch:1", "kwh": 5.0},
              {"_t": t0 + dt.timedelta(hours=4), "host": "h1",
               "channel": "switch:1", "kwh": 9.0}], t0,
              t0 + dt.timedelta(hours=4))), 2)

    print("\nA missing history file is empty, not an error")
    check("no file yet", dashboard.load_shelly_history("/nonexistent/x.jsonl"), [])

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
