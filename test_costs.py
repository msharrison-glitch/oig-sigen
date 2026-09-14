#!/usr/bin/env python3
"""
Offline test of the cost calculation. No network, no account, no meter.

What matters here is that a half hour is priced correctly, because every
pound on the dashboard is that decision repeated 48 times a day:

  - the guaranteed window WRAPS MIDNIGHT. 23:30-05:30 is not a range you can
    compare with <= and expect the right answer.
  - a bonus slot makes a half hour cheap even though it is nowhere near the
    guaranteed window -- and Sigen's own tariff data does NOT know this, which
    is why the dispatch windows come from the agent's log instead.
  - Octopus re-plans constantly, so the log's dispatch windows overlap
    heavily and must be merged or the same half hour is counted repeatedly.

    python3 test_costs.py
"""

from __future__ import annotations

import datetime as dt

import costs

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


LOG = """\
2026-09-11 20:51:06 INFO    SCHEDULE + added   20:51 -> 22:30 [dispatch]
2026-09-11 21:00:31 INFO    SCHEDULE + added   21:00 -> 22:30 [dispatch]
2026-09-11 21:30:07 INFO    SCHEDULE + added   21:30 -> 23:00 [dispatch]
2026-09-12 08:00:00 INFO    SCHEDULE + added   08:00 -> 08:30 [dispatch]
2026-09-12 08:00:00 DEBUG   sleeping 303s
"""


def main() -> int:
    print("\nThe guaranteed window wraps midnight")
    for hhmm, want in (("23:29", False), ("23:30", True), ("00:00", True),
                       ("03:00", True), ("05:29", True), ("05:30", False),
                       ("12:00", False), ("20:00", False)):
        h, m = (int(x) for x in hhmm.split(":"))
        check(f"{hhmm} is off-peak",
              costs.in_guaranteed(dt.datetime(2026, 9, 12, h, m)), want)

    print("\nDispatch windows are merged, because Octopus re-plans constantly")
    windows = costs.dispatch_windows(LOG.splitlines())
    check("overlapping re-plans collapse", len(windows), 2)
    check("and the merged span runs to the latest end",
          windows[0][1], dt.datetime(2026, 9, 11, 23, 0))

    # The agent logs a slot TRIMMED TO NOW when it first sees it, so a
    # dispatch that really began on the half hour appears as "20:51 -> 22:30".
    # Octopus dispatches are half-hour aligned -- completedDispatches returns
    # exactly 19:00->19:30 -- so the start is snapped back. Without it, the
    # half hour containing the trimmed time is priced at PEAK despite the
    # dispatch covering most of it, which overstated peak import on this
    # account by roughly 8 kWh in a week.
    check("a start of 20:51 snaps back to 20:30",
          windows[0][0], dt.datetime(2026, 9, 11, 20, 30))
    later = costs.dispatch_windows(
        ["2026-09-11 21:40:00 INFO    SCHEDULE + added   21:47 -> 22:30 [dispatch]"])
    check("and 21:47 snaps to 21:30",
          later[0][0], dt.datetime(2026, 9, 11, 21, 30))
    exact = costs.dispatch_windows(
        ["2026-09-11 20:00:00 INFO    SCHEDULE + added   20:00 -> 22:30 [dispatch]"])
    check("an already-aligned start is untouched",
          exact[0][0], dt.datetime(2026, 9, 11, 20, 0))
    check("a separate day stays separate",
          windows[1][0], dt.datetime(2026, 9, 12, 8, 0))
    check("a log with no schedule lines yields none",
          costs.dispatch_windows(["2026-09-12 08:00:00 DEBUG sleeping"]), [])

    print("\nA bonus slot makes a half hour cheap far from the window")
    tz = dt.timezone(dt.timedelta(hours=1))
    inside = dt.datetime(2026, 9, 11, 21, 15, tzinfo=tz)
    outside = dt.datetime(2026, 9, 11, 23, 15, tzinfo=tz)
    check("21:15 during a dispatch is cheap",
          costs.is_cheap(inside, windows), True)
    check("23:15 with no dispatch is not",
          costs.is_cheap(outside, windows), False)
    check("but 23:45 is, on the guaranteed window alone",
          costs.is_cheap(dt.datetime(2026, 9, 11, 23, 45, tzinfo=tz), []), True)

    print("\nPricing")
    half = dt.timedelta(minutes=30)
    base = dt.datetime(2026, 9, 11, 21, 0, tzinfo=tz)
    imported = {base: 10.0,                       # in a dispatch -> cheap
                base + 4 * half: 2.0}             # 23:00, not cheap
    exported = {base: 5.0, base + 4 * half: 5.0}
    rates = {base: 20.0}                          # only one half hour priced
    out = costs.summarise(imported, exported, rates, windows, 4.49, 29.757)
    check("cheap kWh identified", out["cheap_kwh"], 10.0)
    check("peak kWh identified", out["peak_kwh"], 2.0)
    check("import cost mixes both rates",
          round(out["import_cost"], 4), round((10 * 4.49 + 2 * 29.757) / 100, 4))
    check("export income uses the published rate",
          round(out["export_income"], 4), 1.0)
    check("export with no published rate is flagged, not assumed free",
          out["export_unpriced_kwh"], 5.0)
    check("net is cost minus income",
          round(out["net"], 4),
          round(out["import_cost"] - out["export_income"], 4))
    check("the counterfactual uses the SPREAD, not the peak rate",
          round(out["vs_all_peak"], 4), round(10 * (29.757 - 4.49) / 100, 4))

    print("\nNo meter data is zero, not a crash")
    empty = costs.summarise({}, {}, {}, windows, 4.49, 29.757)
    check("no import", empty["import_cost"], 0.0)
    check("no export", empty["export_income"], 0.0)
    check("no half hours", empty["half_hours"], 0)

    print("\nOctopus mixes Z and +01:00 in the same API")
    check("a Z timestamp parses on Python 3.9",
          costs._iso("2026-09-12T22:30:00Z").utcoffset(), dt.timedelta(0))
    check("and an offset one still does",
          costs._iso("2026-09-13T00:30:00+01:00").utcoffset(),
          dt.timedelta(hours=1))

    print("\n" + "=" * 68)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. Half hours are priced the way Octopus bills them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
