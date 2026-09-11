#!/usr/bin/env python3
"""
Offline test of the heat pump / bonus slot report.

No network, no files outside the fixtures below. What is worth pinning is the
parsing, because every number this report prints is derived from it and a
quiet error would flow straight into the business case:

  - the two sources use DIFFERENT timestamp formats. observe.log writes naive
    local; the heat pump history writes ISO-8601 with an offset. Comparing
    them wrongly would silently shift every overlap by an hour, which in a
    report about half-hour slots is the difference between "it overlaps" and
    "it does not".
  - a slot is a STARTED..RELEASED pair and nothing else. Planned dispatches
    the agent DECLINED -- car not drawing, battery already full -- are not
    periods of cheap import, and counting them would inflate the one number
    the report exists to measure.

    python3 test_heatreport.py
"""

from __future__ import annotations

import datetime as dt

import heatreport

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<56} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


LOG = """\
2026-09-06 21:12:00 INFO    cheap now [dispatch]: octopus until 23:29:30 local
2026-09-06 21:13:34 INFO    cloud: selected charging profile 9664 (was mode 1)
2026-09-06 21:13:34 INFO    SOC 8.2%  grid +0.00 kW  ESS -0.49 kW -> STARTED charging
2026-09-06 21:30:00 DEBUG   sleeping 35s
2026-09-06 21:59:46 INFO    cloud: restored mode 1
2026-09-06 21:59:46 INFO    SOC 41.4%  grid +11.37 kW -> RELEASED
2026-09-07 10:00:00 INFO    inside a cheap slot but SOC is 95.6% -- nothing to gain
2026-09-07 10:00:00 INFO    SOC 95.6%  grid +0.01 kW -> idle (battery at target)
2026-09-08 20:42:10 INFO    SOC 11.4% -> STARTED charging
2026-09-08 20:59:00 INFO    SOC 24.0% -> RELEASED
"""


def record(when: str, heating: str = "off", room=21.0):
    return {"fetched_at": when,
            "points": {"climateControl": {"onOffMode": heating,
                                          "sensors": {"roomTemperature": room}},
                       "domesticHotWaterTank": {"onOffMode": "on",
                                                "sensors": {"tankTemperature": 46}}}}


def main() -> int:
    print("\nTwo timestamp formats, one timeline")
    check("naive local, as observe.log writes it",
          heatreport.parse_time("2026-09-06 21:13:34"),
          dt.datetime(2026, 9, 6, 21, 13, 34))
    check("ISO with an offset, as the history writes it",
          heatreport.parse_time("2026-09-11T17:30:05+0100"),
          dt.datetime(2026, 9, 11, 17, 30, 5))
    check("ISO with a colon in the offset",
          heatreport.parse_time("2026-09-11T17:30:05+01:00"),
          dt.datetime(2026, 9, 11, 17, 30, 5))
    # The offset is DROPPED, not converted: both sources are local time on the
    # same host. Converting would move every heat pump reading by an hour.
    check("the offset is dropped, not applied",
          heatreport.parse_time("2026-09-11T17:30:05+0100").hour, 17)

    print("\nA slot is STARTED..RELEASED, and nothing else")
    slots = heatreport.parse_slots(LOG.splitlines())
    check("two slots found", len(slots), 2)
    check("first starts at the STARTED line",
          slots[0]["start"], dt.datetime(2026, 9, 6, 21, 13, 34))
    check("and ends at the RELEASED line",
          slots[0]["end"], dt.datetime(2026, 9, 6, 21, 59, 46))
    check("SOC is captured at both ends",
          (slots[0]["soc_start"], slots[0]["soc_end"]), (8.2, 41.4))
    # The declined dispatch on 09-07 must NOT appear: the agent sat it out,
    # so no cheap electricity was bought and it is not shiftable time.
    check("a DECLINED dispatch is not a slot",
          any(s["start"].day == 7 for s in slots), False)

    print("\nA slot still open at the end of the log")
    open_log = LOG.splitlines()[:3]
    still = heatreport.parse_slots(open_log)
    check("it is reported, not silently dropped", len(still), 1)
    check("with no end", still[0]["end"], None)
    check("and is excluded from totals",
          heatreport.overlap(still, [])["slot_minutes"], 0)

    print("\nThe join")
    hist = [record("2026-09-06T21:20:00+0100"),            # inside slot 1
            record("2026-09-06T21:50:00+0100", heating="on"),   # inside, ON
            record("2026-09-07T12:00:00+0100", heating="on"),   # outside
            record("2026-09-08T20:50:00+0100")]            # inside slot 2
    for r in hist:
        r["_t"] = heatreport.parse_time(r["fetched_at"])
    stats = heatreport.overlap(slots, hist)
    check("slot minutes add up", round(stats["slot_minutes"]), 63)
    check("only observations inside slots count",
          stats["observations_inside"], 3)
    check("heating-on outside a slot is not counted",
          stats["heating_on_inside"], 1)

    print("\nBoundaries are inclusive, so a reading exactly on the edge counts")
    edge = [record("2026-09-06T21:13:34+0100")]
    edge[0]["_t"] = heatreport.parse_time(edge[0]["fetched_at"])
    check("a reading at the start instant is inside",
          heatreport.overlap(slots, edge)["observations_inside"], 1)

    print("\nGaps in observation are reported, not smoothed over")
    sparse = [record("2026-09-10T22:15:00+0100"),
              record("2026-09-11T16:02:00+0100")]
    for r in sparse:
        r["_t"] = heatreport.parse_time(r["fetched_at"])
    found = heatreport.gaps(sparse)
    check("one gap over an hour", len(found), 1)
    check("of about 17.8 hours", round(found[0][2] / 60, 1), 17.8)
    check("a dense series has none",
          heatreport.gaps([r for r in hist[:2]]), [])

    print("\nMissing files are not a crash")
    check("no history file -> empty", heatreport.load_history("/nonexistent"), [])
    check("no slots in an empty log", heatreport.parse_slots([]), [])
    check("and the join says zero rather than dividing by it",
          heatreport.overlap([], [])["slot_minutes"], 0)

    print("\nheating_on reads the right field")
    check("off", heatreport.heating_on(record("2026-09-06T21:00:00+0100")), False)
    check("on", heatreport.heating_on(
        record("2026-09-06T21:00:00+0100", heating="on")), True)
    check("a malformed record is False, not an exception",
          heatreport.heating_on({}), False)

    print("\n" + "=" * 68)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. Parsing holds, and declined slots stay out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
