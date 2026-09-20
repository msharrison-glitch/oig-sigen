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
import json
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
    check("today's solar total appears, undegraded",
          "9.89" in html, True)

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
    # Counted by name rather than by tag: the markup is a design decision
    # and changing it must not look like a behaviour regression.
    check("a two-relay gen1 device renders both channels",
          row1.count("Kitchen right"), 2)
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

    print("\nThe dashboard prefers the agent's published state to the log")
    import json as _json, tempfile as _tf, pathlib as _pl
    _d = _pl.Path(_tf.mkdtemp())
    log = _d / "observe.log"
    log.write_text(
        "2026-09-13 20:12:51 INFO    SOC 11.1% -> STARTED charging\n"
        "2026-09-13 20:30:00 INFO    SOC 24.4%  grid +11.4 kW  ESS +10.2 kW "
        "enable=0 mode=0 limit=unset work=9 -> holding\n", encoding="utf-8")

    from_log = dashboard.read_agent(str(log), state_file=str(_d / "absent.json"))
    check("with no state file it still reads the log",
          from_log["source"], "log")
    check("and finds the open slot", from_log["holding"], True)

    fresh = _d / "agent-state.json"
    fresh.write_text(_json.dumps({
        "local": dt.datetime.now().isoformat(timespec="seconds"),
        "soc": 63.5, "grid_kw": 11.4, "ess_kw": 10.2, "action": "holding",
        "cloud_held": True, "lease_held": False, "work_mode": 9}),
        encoding="utf-8")
    from_state = dashboard.read_agent(str(log), state_file=str(fresh))
    check("with one, it is preferred", from_state["source"], "state file")
    check("the reading is the agent's, not the log's",
          from_state["state"]["soc"], 63.5)
    check("and 'holding' comes from the agent, not inferred from a slot",
          from_state["holding"], True)
    # The log said holding too, but the agent is authoritative -- parse_slots
    # truncating on schedule churn once made the two disagree on one page.
    quiet = _d / "idle-state.json"
    quiet.write_text(_json.dumps({
        "local": dt.datetime.now().isoformat(timespec="seconds"),
        "soc": 20.0, "action": "idle", "cloud_held": False,
        "lease_held": False}), encoding="utf-8")
    check("a stale state file is still used for the reading",
          dashboard.read_agent(str(log), state_file=str(quiet))["state"]["soc"],
          20.0)

    corrupt = _d / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    check("a half-written file falls back rather than crashing",
          dashboard.read_agent(str(log), state_file=str(corrupt))["source"],
          "log")

    print("\nBattery capacity comes from the plant, never hard-coded")
    # 24.18 kWh is THIS plant. Baking it into a shared file hands the next
    # adopter a confidently wrong kWh figure with nothing to signal why.
    # Assert on the CODE, not the prose: the comment explaining why this
    # matters legitimately names the figure.
    code = "\n".join(l for l in SOURCE.splitlines()
                     if not l.lstrip().startswith("#"))
    check("no capacity literal is used in arithmetic",
          bool(re.search(r"\*\s*24\.18|24\.18\s*\*", code)), False)
    with_cap = dashboard.hero({"sigen": {"soc": 50.0},
                               "agent": {"state": {"capacity_kwh": 24.18}},
                               "tariff_soc": {}})
    check("published capacity is used", "12.1 kWh of 24.2" in with_cap, True)
    without = dashboard.hero({"sigen": {"soc": 50.0}, "agent": {},
                              "tariff_soc": {}})
    check("and without it, no invented kWh",
          "state of charge" in without, True)
    check("the percentage still shows either way", "50%" in without, True)

    print("\nAn idle CT clamp is not a fault")
    check("a small negative reads as zero", dashboard.watts(-1.0), "0 W")
    check("so does a small positive", dashboard.watts(0.4), "0 W")
    check("a real reading is untouched", dashboard.watts(38.2), "38 W")
    check("and a real negative survives", dashboard.watts(-120.0), "-120 W")

    print("\nMoney: a net INCOME must not read as a cost")
    # costs.py returns net = cost - income, so a profitable day is NEGATIVE.
    # Rendering that raw would show "-14.36" for a day you made money.
    good = {"import_kwh": 52.65, "import_cost": 2.37, "cheap_kwh": 52.64,
            "peak_kwh": 0.01, "export_kwh": 90.45, "export_income": 16.73,
            "export_unpriced_kwh": 0.0, "net": -14.36, "vs_all_peak": 13.30,
            "off_peak_p": 4.49, "peak_p": 29.757, "half_hours": 96}
    block_html = dashboard.money_block(good, 96)
    check("a negative net renders as income", "net income" in block_html, True)
    check("and is shown positive with a plus",
          "+&pound;14.36" in block_html, True)
    check("not as a negative number", "&pound;-14.36" in block_html, False)

    bad = dict(good, net=5.20, export_income=1.0)
    check("a positive net renders as a cost",
          "net cost" in dashboard.money_block(bad, 96), True)

    print("\nSettlement has three regimes, because a number too early misleads")
    # The owner read "+GBP 0.68" against 40 kWh of plant-measured export and
    # concluded revenue was low. It was not low, it was 10% settled. Below a
    # quarter, the headline is suppressed rather than invited into a
    # comparison it cannot survive.
    barely = dict(good, half_hours=9)
    early = dashboard.money_block(barely, 96)
    check("under a quarter settled shows no headline figure",
          "Settling" in early, True)
    check("and says how little has arrived", "9%" in early, True)
    check("but still shows what HAS settled, labelled 'so far'",
          "Exported so far" in early, True)
    check("and points at the plant figures instead",
          "Plant figures below" in early, True)

    partial = dict(good, half_hours=60)
    mid = dashboard.money_block(partial, 96)
    check("most of the way, the figure shows with a badge",
          "settled" in mid and "Settling" not in mid, True)
    check("a complete period does not nag",
          "settled" in dashboard.money_block(good, 96), False)

    print("\nSettlement is counted across BOTH series, not import alone")
    # Today had export rows and no import rows, so counting import alone
    # printed "0% settled" beside a real export figure.
    import costs as _c
    out = _c.summarise({}, {dt.datetime.now(): 3.93}, {}, [], 4.49, 29.757)
    check("export-only data is not reported as zero coverage",
          out["half_hours"], 1)
    check("and the two series are countable separately",
          (out["import_half_hours"], out["export_half_hours"]), (0, 1))

    print("\nThe counterfactual is labelled as one")
    check("upper bound is stated", "upper bound" in block_html, True)
    check("standing charges are disclaimed",
          "standing charges" in block_html, True)
    check("a failure degrades",
          "Costs unavailable" in dashboard.money_block({"error": "CostError"}, 48),
          True)

    print("\nImport and export prices sit together, with the spread")
    _n = dt.datetime.now().replace(second=0, microsecond=0)
    _ago = lambda m: (_n - dt.timedelta(minutes=m)).strftime("%Y%m%d %H:%M")
    both = {"BUY_TARIFF": [(_ago(60), 0.29757), (_ago(5), 0.29757)],
            "SELL_TARIFF": [(_ago(60), 0.1694), (_ago(5), 0.1694)]}
    cards = dashboard.hero({"sigen": {"soc": 50.0}, "tariff_soc": both})
    check("the import price appears", "29.76p" in cards, True)
    check("the export price appears beside it", "16.94p" in cards, True)
    # Buying at 29.76 to sell at 16.94 loses money; the sign must say so.
    check("and the spread is signed, showing the loss",
          "-12.82p" in cards, True)

    cheap = {"BUY_TARIFF": [(_ago(5), 0.0449)],
             "SELL_TARIFF": [(_ago(5), 0.1694)]}
    check("a cheap import shows a positive spread",
          "+12.45p" in dashboard.hero({"sigen": {}, "tariff_soc": cheap}), True)
    check("export alone still renders when there is no import series",
          "16.94p" in dashboard.hero(
              {"sigen": {}, "tariff_soc": {"SELL_TARIFF": [(_ago(5), 0.1694)]}}),
          True)
    check("and no tariff data at all is not a crash",
          dashboard.hero({"sigen": {}, "tariff_soc": {}}), "")

    print("\nA bonus slot is priced at off-peak, not at Sigen's peak")
    # Sigen's BUY_TARIFF knows only the static schedule, so on 2026-09-15 the
    # card read 29.76p while the agent charged a confirmed dispatch at 4.49p,
    # under a banner on the same page saying the import was off-peak.
    peak = {"BUY_TARIFF": [(_ago(5), 0.29757)],
            "SELL_TARIFF": [(_ago(5), 0.1694)]}

    def card(bonus):
        return dashboard.hero({"sigen": {}, "tariff_soc": peak,
                               "agent": {"bonus": bonus}, "off_peak_p": 4.49})
    charging = card("charging")
    check("charging a slot shows off-peak", "4.49p" in charging, True)
    check("and says why", "bonus slot" in charging, True)
    check("and the spread follows the real price", "+12.45p" in charging, True)
    check("a confirmed slot at target is still off-peak",
          "4.49p" in card("confirmed"), True)
    unconfirmed = card("unconfirmed")
    # Planned but the car never drew: that bills at PEAK. Showing 4.49p here
    # would be the exact mistake the agent is built to avoid.
    check("an unconfirmed slot stays at peak", "29.76p" in unconfirmed, True)
    check("and says it is unconfirmed", "unconfirmed" in unconfirmed, True)
    check("no slot is peak rate", "peak rate" in card(None), True)
    check("the guaranteed window is untouched by the override",
          "4.49p" in dashboard.hero({"sigen": {}, "tariff_soc": cheap,
                                     "agent": {"bonus": None}}), True)

    print("\nHouse load is derived, so the metered circuits sit beside it")
    # The Sigen's load figure is solar - battery - grid, so the inverter's
    # conversion losses are inside it. Measured 2026-09-20: ~93% battery
    # round trip against 78-85% at the grid, so the gap is real money.
    circuits = [
        {"host": "3em", "channels": [
            {"id": "em1:0", "kind": "meter", "watts": -5.6},
            {"id": "em1:1", "kind": "meter", "watts": 14.8},
            {"id": "em1:2", "kind": "meter", "watts": 300.0}]},
        # A PLUG, which sits downstream of a circuit the 3EM already meters.
        # Counting it would count the same watts twice.
        {"host": "plug", "channels": [
            {"id": "switch:0", "kind": "switch", "watts": 1771.7}]},
        {"host": "dead", "error": "URLError", "channels": []},
    ]
    check("only whole-circuit meters are summed",
          round(dashboard.metered_circuits(circuits), 4), 0.3092)
    check("a dead device does not zero the total",
          dashboard.metered_circuits(circuits) > 0, True)
    check("no meters at all gives no figure, not a false zero",
          dashboard.metered_circuits([{"host": "p", "channels": [
              {"id": "switch:0", "kind": "switch", "watts": 40.0}]}]), None)
    check("and no devices at all is None too",
          dashboard.metered_circuits([]), None)

    house_row = dashboard.flow_rows(
        {"solar": 0.1, "load": 1.17, "battery": 10.2, "grid": -11.4},
        None, circuits)
    check("the derived figure is still the headline", "1.17 kW" in house_row,
          True)
    check("with the metered figure beside it",
          "(0.31 kW metered)" in house_row, True)
    check("and nothing is claimed when no meter answers",
          "metered)" in dashboard.flow_rows(
              {"solar": 0.1, "load": 1.17, "battery": 10.2, "grid": -11.4},
              None, []), False)

    print("\nHeat pump temperatures, from the snapshot and not from Daikin")
    # The API budget is 200 requests a day and the token lives on the polling
    # host. The dashboard reads the record daikin.py already wrote; a second
    # client would spend the same budget twice and fight over the token.
    for forbidden in ("onecta", "daikineurope", "import daikin"):
        check(f"no {forbidden!r} in the source", forbidden in SOURCE, False)

    def snapshot(when, room=22.1, outdoor=16):
        return json.dumps({
            "fetched_at": when,
            "points": {
                "climateControl": {
                    "onOffMode": "off", "setpointMode": "weatherDependent",
                    "sensors": {"roomTemperature": room,
                                "outdoorTemperature": outdoor,
                                "leavingWaterTemperature": 16}},
                "domesticHotWaterTank": {
                    "onOffMode": "on",
                    "sensors": {"tankTemperature": 46}}}}) + "\n"

    recent = (dt.datetime.now() - dt.timedelta(minutes=12)).strftime(
        "%Y-%m-%dT%H:%M:%S+0100")
    hp_file = _d / "hp.jsonl"
    # Two records: the last one wins, which is what "now" means here.
    hp_file.write_text(snapshot("2026-09-18T20:00:07+0100", room=19.0)
                       + snapshot(recent))
    hp = dashboard.read_heatpump(str(hp_file))
    check("the latest record is the one read",
          (hp["room"], hp["outdoor"]), (22.1, 16))
    check("hot water and leaving water too",
          (hp["tank"], hp["water"]), (46, 16))
    check("with the state around them",
          (hp["heating"], hp["water_on"]), ("off", "on"))
    check("a fresh snapshot is not stale", hp["stale"], False)

    row = dashboard.heatpump_row(hp)
    check("the room temperature is shown", "22.1&deg;C" in row, True)
    check("and the outdoor one", "16&deg;C" in row and "outdoor" in row, True)
    # Up to half an hour old by design, so the age is part of the reading.
    check("the age is stated", "12 min ago" in row, True)

    old = _d / "old.jsonl"
    old.write_text(snapshot((dt.datetime.now() - dt.timedelta(hours=3))
                            .strftime("%Y-%m-%dT%H:%M:%S+0100")))
    stale = dashboard.read_heatpump(str(old))
    check("an old snapshot is flagged", stale["stale"], True)
    check("and says so on the page",
          "STALE" in dashboard.heatpump_row(stale), True)

    # Outdoor temperature earns a headline card: it is what predicts the heat
    # pump's demand, and moving that demand into bonus slots is the case the
    # whole ASHP proposal rests on.
    hp_card = dashboard.hero({"sigen": {}, "heatpump": hp})
    check("outdoor gets a card", "16&deg;C" in hp_card or "16°C" in hp_card,
          True)
    check("with the inside temperature beside it",
          "22.1°C inside" in hp_card, True)
    # A stale snapshot must not headline: a quiet card reporting yesterday's
    # weather is worse than no card, because nothing on it looks wrong.
    check("a stale snapshot gets no card",
          "outdoor" in dashboard.hero({"sigen": {}, "heatpump": stale}), False)
    check("and the row's words match its colour",
          "STALE" in dashboard.heatpump_row(stale)
          and "stale" in dashboard.heatpump_row(stale), True)

    check("a missing file is not a crash",
          dashboard.read_heatpump(str(_d / "nope.jsonl")), {})
    check("and renders nothing at all", dashboard.heatpump_row({}), "")
    broken = _d / "broken.jsonl"
    broken.write_text("{not json\n")
    check("a corrupt line is not a crash",
          dashboard.read_heatpump(str(broken)), {})

    print("\nThe car, read from the charger and never commanded")
    # myenergi's API can start a charge, pause one, and set a boost. None of
    # those verbs may ever appear here: this page watches, like the rest of
    # the dashboard.
    # These are myenergi's own command endpoints -- the mode/boost setters.
    # Reading the word "boost" is fine: the hourly series has a boost field.
    for forbidden in ("cgi-zappi-mode", "cgi-boost-time", "cgi-set-"):
        check(f"no {forbidden!r} in the source", forbidden in SOURCE, False)

    live = {"power_kw": 7.29, "charging": True, "status": "Boosting",
            "mode": "Eco+", "plug": "EV connected", "added_kwh": 12.4}
    dashboard._zappi_cache["at"] = None
    got = dashboard.read_zappi(status_fn=lambda: live)
    check("the rate is read", got["power_kw"], 7.29)
    check("with the status that proves a dispatch is real",
          (got["status"], got["charging"]), ("Boosting", True))
    # A second call inside the TTL must not hit myenergi again: the page
    # refreshes every 30s and several people may have it open.
    calls = []

    def counted():
        calls.append(1)
        return live
    dashboard.read_zappi(status_fn=counted)
    check("a second read inside the TTL is served from cache", calls, [])
    dashboard._zappi_cache["at"] = None
    check("and expiring the cache reads again",
          dashboard.read_zappi(status_fn=counted)["power_kw"], 7.29)

    def boom():
        raise OSError("timed out")
    dashboard._zappi_cache["at"] = None
    check("an unreachable charger is an error, not a crash",
          dashboard.read_zappi(status_fn=boom), {"error": "OSError"})
    dashboard._zappi_cache["at"] = None
    check("no Zappi on the account says so",
          dashboard.read_zappi(status_fn=lambda: None)["error"],
          "no Zappi on the account")
    dashboard._zappi_cache["at"] = None

    flow = {"solar": 0.1, "load": 0.4, "battery": -1.0, "grid": 0.5}
    row = dashboard.flow_rows(flow, live)
    check("the car appears in the flow", "7.29 kW" in row, True)
    check("with its charger's own words", "Boosting, Eco+" in row, True)
    # Plugged in but paused is worth seeing: it is the state that decides
    # whether a planned dispatch will ever bill at 4.49p.
    paused = dict(live, power_kw=0.0, charging=False, status="Paused")
    check("a plugged-in car still shows at 0 kW",
          "Paused" in dashboard.flow_rows(flow, paused), True)
    check("an unreachable charger is visible on the page",
          "charger unreachable" in dashboard.flow_rows(flow,
                                                       {"error": "OSError"}),
          True)
    check("no charger configured adds no row",
          "Car" in dashboard.flow_rows(flow, {}), False)
    check("and Sigen's own evPower still shows if it ever reports one",
          "2.00 kW" in dashboard.flow_rows(dict(flow, ev=2.0), {}), True)

    card = dashboard.hero({"sigen": {}, "zappi": live})
    check("a drawing car gets a card", "7.29" in card and "kW car" in card,
          True)
    check("with what it has added", "12.4 kWh added" in card, True)
    check("a paused car does not",
          "kW car" in dashboard.hero({"sigen": {}, "zappi": paused}), False)

    print("\nThe Today chart re-prices confirmed bonus slots too")
    # Same blind spot as the card: Sigen's series draws every bonus slot at
    # peak, so the chart contradicted the "4.49p off-peak" card above it.
    _today = dt.datetime.now().strftime("%Y%m%d")
    _pt = lambda h, m: (f"{_today} {h:02d}:{m:02d}", 0.29757)
    series = {"BUY_TARIFF": [_pt(19, 0), _pt(20, 0), _pt(20, 30), _pt(21, 0)]}
    win = [(dt.datetime.now().replace(hour=20, minute=0, second=0,
                                      microsecond=0),
            dt.datetime.now().replace(hour=21, minute=0, second=0,
                                      microsecond=0))]
    block = dashboard.tariff_block(series, win, 4.49)
    check("the slot's low price reaches the range", "4.49p" in block, True)
    check("the correction is marked", "Import price *" in block, True)
    check("and explained", "knows only the fixed" in block, True)
    check("an uncorrected chart says nothing extra",
          "*" in dashboard.tariff_block(series, [], 4.49), False)

    fixed, corrected = dashboard.correct_buy_tariff(
        series["BUY_TARIFF"], win, 4.49)
    check("only points inside the window move",
          [round(v, 4) for _, v in fixed],
          [0.2976, 0.0449, 0.0449, 0.2976])
    check("and it reports that it changed something", corrected, True)
    # A price is only ever pulled DOWN: a wrong window cannot invent an
    # expensive half hour out of a cheap one.
    already = [(f"{_today} 23:45", 0.0449)]
    check("an already-cheap point is untouched",
          dashboard.correct_buy_tariff(already, win, 4.49), (already, False))
    check("no windows, no change",
          dashboard.correct_buy_tariff(series["BUY_TARIFF"], [], 4.49)[1],
          False)

    print("\nOnly dispatches the car actually drew in are re-priced")
    at = dt.datetime(2026, 9, 15, 21, 0, 0)
    confirmed_log = """
2026-09-15 18:00:05 INFO    SCHEDULE + added   18:00 -> 18:30 [dispatch]
2026-09-15 18:29:30 INFO    SOC 40.0% -> idle (dispatch unconfirmed)
2026-09-15 20:00:11 INFO    SCHEDULE + added   20:00 -> 23:30 [dispatch]
2026-09-15 20:09:20 INFO    DISPATCH ACTIVE: the car is drawing, so this slot is really off-peak -- proceeding
2026-09-15 20:09:26 INFO    SOC 12.3% -> STARTED charging
""".strip("\n").splitlines()
    spans = dashboard.confirmed_bonus_windows(confirmed_log, now=at)
    check("one window, the confirmed one", len(spans), 1)
    check("it starts at the dispatch, not at the confirmation",
          spans[0][0], dt.datetime(2026, 9, 15, 20, 0))
    # The slot runs to 23:30 but Octopus can still withdraw the rest, so
    # nothing past the clock is claimed as cheap.
    check("and stops at now, not at the slot's end", spans[0][1], at)
    check("a dispatch with no confirmation is not re-priced",
          any(s[0].hour == 18 for s in spans), False)

    print("\nA dispatch ends when it is WITHDRAWN, not at its published end")
    # Real shape, 2026-09-15: published to 23:30, withdrawn at 21:05:51 when
    # the car finished. Taking 23:30 would paint 2.4 hours of peak import as
    # off-peak -- on the chart, and in the money figures.
    withdrawn = """
2026-09-15 20:00:11 INFO    SCHEDULE + added   20:00 -> 23:30 [dispatch]
2026-09-15 20:09:20 INFO    DISPATCH ACTIVE: the car is drawing, so this slot is really off-peak -- proceeding
2026-09-15 20:30:17 INFO    SCHEDULE + added   20:30 -> 23:30 [dispatch]
2026-09-15 20:30:17 WARNING SCHEDULE - WITHDRAWN 20:00 -> 23:30 [dispatch] -- +30.3 min into it
2026-09-15 21:05:51 WARNING SCHEDULE - WITHDRAWN 20:30 -> 23:30 [dispatch] -- +35.9 min into it
2026-09-15 21:35:21 WARNING SCHEDULE - WITHDRAWN 22:00 -> 22:30 [dispatch] -- 24.6 min before it started
""".strip("\n").splitlines()
    later = dt.datetime(2026, 9, 15, 23, 59, 0)
    cover = dashboard.dispatch_coverage(withdrawn)
    check("the re-plan is one continuous period, not two", len(cover), 1)
    check("starting at the first dispatch",
          cover[0][0], dt.datetime(2026, 9, 15, 20, 0))
    check("and ending at the withdrawal",
          cover[0][1], dt.datetime(2026, 9, 15, 21, 5, 51))
    check("the confirmed window matches it",
          dashboard.confirmed_bonus_windows(withdrawn, now=later),
          [(dt.datetime(2026, 9, 15, 20, 0),
            dt.datetime(2026, 9, 15, 21, 5, 51))])
    # A slot withdrawn before it ever started covers nothing: it was added at
    # 22:00-22:30 and taken away at 21:35.
    check("a slot withdrawn before it started covers nothing",
          any(a.hour == 22 for a, _ in cover), False)
    # A dispatch still live has no withdrawal, so it runs to its published
    # end -- trimmed at now by confirmed_bonus_windows.
    live_log = withdrawn[:3]
    check("a live dispatch keeps its published end",
          dashboard.dispatch_coverage(live_log)[0][1],
          dt.datetime(2026, 9, 15, 23, 30))

    print("\nbonus_status reads the agent's verdict, and distrusts stale ones")
    at = dt.datetime(2026, 9, 15, 21, 0, 0)

    def lines(text):
        return text.strip("\n").splitlines()
    fresh_hold = {"_t": at - dt.timedelta(seconds=30), "cloud_held": True}
    check("a fresh hold is charging",
          dashboard.bonus_status([], fresh_hold, now=at), "charging")
    check("a stale hold is not trusted",
          dashboard.bonus_status([], {"_t": at - dt.timedelta(minutes=20),
                                      "cloud_held": True}, now=at), None)

    tonight = lines("""
2026-09-15 20:00:11 INFO    SCHEDULE + added   20:00 -> 23:30 [dispatch]
2026-09-15 20:00:18 INFO    Zappi: Paused, EV connected, not charging, 0.00 kW
2026-09-15 20:00:18 INFO    waiting: slot 20:00 is planned but the car is not drawing, so it may never complete and would bill at peak
2026-09-15 20:00:18 INFO    SOC 12.7%  grid -0.01 kW -> idle (dispatch unconfirmed)
2026-09-15 20:09:20 INFO    Zappi: Boosting, charging, 7.29 kW
2026-09-15 20:09:20 INFO    DISPATCH ACTIVE: the car is drawing, so this slot is really off-peak -- proceeding
2026-09-15 20:09:26 INFO    SOC 12.3%  grid +0.01 kW -> STARTED charging
2026-09-15 20:30:17 INFO    SCHEDULE + added   20:30 -> 23:30 [dispatch]
2026-09-15 20:30:17 WARNING SCHEDULE - WITHDRAWN 20:00 -> 23:30 [dispatch] -- +30.3 min into it
2026-09-15 20:58:00 INFO    SOC 95.0%  grid +11.40 kW -> RELEASED
2026-09-15 20:59:30 INFO    inside a cheap slot but SOC is 95.2% (target 95.0%, resume below 85.0%) -- nothing to gain
2026-09-15 20:59:30 INFO    SOC 95.2%  grid +0.40 kW -> idle (battery at target)
2026-09-15 21:00:00 INFO    cheap now [dispatch]: octopus until 23:29:30 local
""")
    # Released at target, the car still charging: the house is on 4.49p. The
    # agent stops asking the Zappi once confirmed, so the verdict has to come
    # from earlier in the dispatch -- across the 20:30 re-plan, too.
    check("confirmed and at target, across a re-plan",
          dashboard.bonus_status(tonight, {}, now=at), "confirmed")
    check("a line from a tick still in progress is ignored",
          dashboard.bonus_status(tonight[:-1], {}, now=at), "confirmed")

    waiting = lines("""
2026-09-15 18:00:05 INFO    SCHEDULE + added   18:00 -> 18:30 [dispatch]
2026-09-15 18:10:00 INFO    DISPATCH ACTIVE: the car is drawing, so this slot is really off-peak -- proceeding
2026-09-15 18:29:30 INFO    SOC 40.0%  grid +11.40 kW -> RELEASED
2026-09-15 20:55:02 INFO    SCHEDULE + added   20:30 -> 23:30 [dispatch]
2026-09-15 20:59:40 INFO    waiting: slot 20:30 is planned but the car is not drawing, so it may never complete and would bill at peak
2026-09-15 20:59:40 INFO    SOC 12.0%  grid +0.01 kW -> idle (dispatch unconfirmed)
""")
    check("a planned slot the car has not drawn in is unconfirmed",
          dashboard.bonus_status(waiting, {}, now=at), "unconfirmed")
    # The 18:10 confirmation belonged to a different dispatch. Carrying it
    # forward would price a slot that may never complete at off-peak.
    check("an earlier dispatch's confirmation does not carry over",
          dashboard.bonus_status(waiting, {}, now=at) != "confirmed", True)
    check("a stale tick is not evidence",
          dashboard.bonus_status(waiting, {},
                                 now=at + dt.timedelta(minutes=20)), None)
    idle = lines("""
2026-09-15 20:59:40 INFO    SOC 60.0%  grid +0.01 kW -> idle
""")
    check("no live slot is None", dashboard.bonus_status(idle, {}, now=at), None)
    check("read_agent never lets this raise",
          "bonus" in dashboard.read_agent(str(log),
                                          state_file=str(_d / "absent.json")),
          True)

    print("\nThe CURRENT price, not the last point in the series")
    # BUY_TARIFF carries all 288 points for the whole day including the
    # FUTURE, so points[-1] is always 23:55 -- inside the guaranteed cheap
    # window. The price card therefore read "4.49p off-peak" at any hour of
    # the day. SOC hid it, because its series stops at the present.
    day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    series = []
    for i in range(288):
        when = day + dt.timedelta(minutes=5 * i)
        cheap = when.time() >= dt.time(23, 30) or when.time() < dt.time(5, 30)
        series.append((when.strftime("%Y%m%d %H:%M"), 0.0449 if cheap else 0.29757))

    noon = [(w, v) for w, v in series
            if w <= day.replace(hour=12).strftime("%Y%m%d %H:%M")]
    check("midday reads the midday value, not 23:55",
          dashboard.value_now(noon), 0.29757)
    check("the last point of a full day is NOT what we want",
          series[-1][1], 0.0449)

    # A series entirely in the future (the day has not started) must not
    # return None and blank the card.
    future = [((day + dt.timedelta(days=1)).strftime("%Y%m%d %H:%M"), 0.1)]
    check("a not-yet-started series falls back to its first point",
          dashboard.value_now(future), 0.1)
    check("an empty series is None, not an exception",
          dashboard.value_now([]), None)
    check("an unparseable label does not crash it",
          dashboard.value_now([("not a time", 0.2)]), 0.2)

    print("\nSparklines are hoverable with CSS, not SVG titles or script")
    # Shipped once using SVG <title>, which SAFARI DOES NOT RENDER -- the
    # result was a crosshair cursor and no value, on the browser the owner
    # had moved to because Chrome could not reach the LAN. CSS :hover works
    # everywhere and still needs nothing loaded.
    _day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _series = []
    for i in range(288):
        w = _day + dt.timedelta(minutes=5 * i)
        cheap = w.time() >= dt.time(23, 30) or w.time() < dt.time(5, 30)
        _series.append((w.strftime("%Y%m%d %H:%M"), 0.0449 if cheap else 0.29757))

    bands = dashboard.hover_bands(_series, lambda v: f"{v * 100:.2f}p")
    labels = re.findall(r"<b[^>]*>([^<]*)</b>", bands)
    check("288 five-minute points become 48 half-hour bands",
          len(labels), 48)
    check("a band carries its clock time and value",
          labels[0].replace("&middot;", "·").strip(), "00:00 · 4.49p")
    check("midday reads the peak rate", "29.76p" in labels[24], True)
    check("23:30 picks up the guaranteed window", "4.49p" in labels[47], True)
    check("no SVG <title> is relied on", "<title>" in bands, False)
    check("and no script", "<script" in bands, False)

    # The last band must not run off the edge of the card.
    check("bands near the right edge anchor right",
          "right:0" in bands, True)
    check("and near the left edge anchor left", "left:0" in bands, True)
    check("no points means no bands, not a crash",
          dashboard.hover_bands([], lambda v: str(v)), "")
    check("no formatter means no bands",
          dashboard.hover_bands(_series, None), "")

    print("\nThe tariff block answers 'was it cheap when we charged?'")
    # Time-relative, not hard-coded: value_now compares against the clock, so
    # a fixture pinned to one date would assert the wrong thing tomorrow. The
    # last point is deliberately in the PAST so "now" is well defined.
    _d = dt.datetime.now().replace(second=0, microsecond=0)
    _t = lambda mins: (_d - dt.timedelta(minutes=mins)).strftime("%Y%m%d %H:%M")
    tariff = {"BUY_TARIFF": [(_t(600), 0.0449), (_t(300), 0.2976),
                             (_t(5), 0.0449)],
              "SOC": [(_t(600), 1.0), (_t(5), 97.5)]}
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
