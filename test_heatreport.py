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
  - a slot runs from STARTED to RELEASED or STOOD DOWN, and nothing else
    opens one. Planned dispatches the agent DECLINED -- car not drawing,
    battery already full -- are not periods of cheap import, and counting
    them would inflate the one number the report exists to measure.
  - a slot whose end was never logged (restart, crash) ends at its last
    holding tick and is flagged `end_inferred`, rather than being erased by
    the next STARTED or left looking live forever.

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

    print("\nSchedule churn must NOT end a slot")
    # Octopus re-plans constantly: it withdraws a dispatch and adds a new one
    # covering the same time, in the same tick, while the agent charges
    # straight through. Matching the word WITHDRAWN anywhere in a line treated
    # that as the end of the slot. Observed 2026-09-13, and across a fortnight
    # of real log it hid 277 of 612 commanded minutes -- 45% of the only
    # number the ASHP proposal actually rests on.
    churn = """\
2026-09-13 20:12:51 INFO    SOC 11.1% -> STARTED charging
2026-09-13 20:30:23 INFO    SCHEDULE + added   20:30 -> 23:30 [dispatch]
2026-09-13 20:30:23 WARNING SCHEDULE - WITHDRAWN 20:09 -> 23:30 [dispatch] -- +21.4 min into it
2026-09-13 20:30:30 INFO    SOC 24.4% -> holding
2026-09-13 23:29:40 INFO    SOC 95.0% -> RELEASED
""".splitlines()
    churned = heatreport.parse_slots(churn)
    check("churn leaves exactly one slot", len(churned), 1)
    check("and it ends at the RELEASE, not the withdrawal",
          churned[0]["end"], dt.datetime(2026, 9, 13, 23, 29, 40))
    check("so its length is the real one",
          round((churned[0]["end"] - churned[0]["start"]).total_seconds() / 60),
          197)

    print("\nThe other ways a slot really can end")
    for action, label in (("RELEASED", "a normal release"),
                          ("STOOD DOWN (plant taken back)", "a stand-down")):
        log = [f"2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging",
               f"2026-09-13 20:30:00 INFO    SOC 30.0% -> {action}"]
        got = heatreport.parse_slots(log)
        check(f"{label} closes the slot",
              (len(got), got[0].get("end")),
              (1, dt.datetime(2026, 9, 13, 20, 30)))
    # "holding" is the steady state, not an ending.
    held = heatreport.parse_slots([
        "2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging",
        "2026-09-13 20:05:00 INFO    SOC 12.0% -> holding"])
    check("but 'holding' leaves it open", held[0].get("end"), None)

    print("\nA failed restore is not an ending")
    # _cloud_stop leaves cloud_held True when the restore does not verify, so
    # the plant is still on the charging profile and the next tick retries.
    # This test used to assert the opposite, pinning the bug in place.
    failed = heatreport.parse_slots("""\
2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging
2026-09-13 23:29:30 INFO    SOC 95.0% -> RESTORE FAILED
2026-09-13 23:30:05 INFO    SOC 95.0% -> RESTORE FAILED
2026-09-13 23:30:40 INFO    SOC 95.0% -> RELEASED
""".splitlines())
    check("one slot, not two", len(failed), 1)
    check("ending at the RELEASE that finally took",
          failed[0]["end"], dt.datetime(2026, 9, 13, 23, 30, 40))
    check("and not marked as inferred", failed[0].get("end_inferred"), None)

    print("\nA second STARTED must not erase the slot already open")
    # Cloud path, agent restarted mid-slot: nothing releases the profile, the
    # plant charges straight through, and the new process logs STARTED again.
    # The second STARTED used to overwrite the first, deleting 20 minutes.
    restart = heatreport.parse_slots("""\
2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging
2026-09-13 20:19:30 INFO    SOC 18.0% -> holding
2026-09-13 20:20:00 INFO    interrupted -- releasing
2026-09-13 20:22:00 INFO    reconciler started (charging via cloud profile 9664 at 8.00 kW)
2026-09-13 20:22:00 ERROR   plant is on a mode only we could have set (mode 9, profile 9664)
2026-09-13 20:22:05 INFO    SOC 19.0% -> STARTED charging
2026-09-13 20:59:30 INFO    SOC 40.0% -> RELEASED
""".splitlines())
    check("both segments survive", len(restart), 2)
    check("the first ends at its last holding tick",
          restart[0]["end"], dt.datetime(2026, 9, 13, 20, 19, 30))
    check("marked as inferred, because nothing logged its end",
          restart[0].get("end_inferred"), True)
    check("with the SOC at that tick", restart[0]["soc_end"], 18.0)
    check("and the minutes are no longer lost",
          round(heatreport.overlap(restart, [])["slot_minutes"], 1), 56.9)

    # An agent that died after its slot never logs RELEASED; the next STARTED
    # may be a day later. Closing at that STARTED would invent 23 hours of
    # charging, so the orphan ends where the evidence of holding ends.
    died = heatreport.parse_slots("""\
2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging
2026-09-13 20:29:30 INFO    SOC 30.0% -> holding
2026-09-14 09:00:00 INFO    SOC 60.0% -> idle
2026-09-14 19:00:00 INFO    SOC 12.0% -> STARTED charging
2026-09-14 19:29:30 INFO    SOC 30.0% -> RELEASED
""".splitlines())
    check("a death between slots keeps both", len(died), 2)
    check("the orphan ends at its last holding tick, not the next day",
          died[0]["end"], dt.datetime(2026, 9, 13, 20, 29, 30))
    check("and the next slot is intact",
          (died[1]["start"], died[1]["end"]),
          (dt.datetime(2026, 9, 14, 19, 0), dt.datetime(2026, 9, 14, 19, 29, 30)))

    print("\nAn orphan at the end of the log is not 'still open'")
    # The dashboard reads an open slot as "holding now", so an orphan left
    # open would claim a charge the agent has since logged it is not making.
    tail = heatreport.parse_slots("""\
2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging
2026-09-13 20:29:30 INFO    SOC 30.0% -> holding
2026-09-13 21:00:00 INFO    SOC 29.0% -> idle
""".splitlines())
    check("it is closed at its last holding tick",
          (tail[0]["end"], tail[0].get("end_inferred")),
          (dt.datetime(2026, 9, 13, 20, 29, 30), True))
    # Other lines carry "-> " too, and must not count as the agent's action.
    live = heatreport.parse_slots("""\
2026-09-13 20:00:00 INFO    SOC 10.0% -> STARTED charging
2026-09-13 20:29:30 INFO    SOC 30.0% -> holding
2026-09-13 20:30:00 INFO    SCHEDULE + added   20:30 -> 23:30 [dispatch]
2026-09-13 20:30:01 INFO    heartbeat -> http://192.168.2.18:8787/heartbeat
""".splitlines())
    check("but a slot still holding at the end stays open",
          live[0]["end"], None)

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
