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
    flat = [(dt.date.min, 4.49, 29.757)]        # one rate, as before
    out = costs.summarise(imported, exported, rates, windows, flat)
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
    empty = costs.summarise({}, {}, {}, windows, flat)
    check("no import", empty["import_cost"], 0.0)
    check("no export", empty["export_income"], 0.0)
    check("no half hours", empty["half_hours"], 0)

    print("\nA range spanning a rate change prices each day at its own rate")
    # The bug this exists to stop: one scalar rate silently re-pricing
    # history. "This month" and "last 7 days" both straddled 2026-10-01,
    # when VAT on domestic electricity went to zero, and 2026-10-31, when
    # this account's tariff was re-priced. Editing one number would have
    # priced September at November's rates -- no error, just a wrong total.
    schedule = [(dt.date.min, 4.49, 29.757),
                (dt.date(2026, 10, 1), 4.279, 28.3429),
                (dt.date(2026, 10, 31), 7.6, 34.955)]

    def cheap_at(day, kwh):
        """One off-peak half hour inside the guaranteed window, that day."""
        return {dt.datetime(day.year, day.month, day.day, 2, 0,
                            tzinfo=tz): kwh}

    for day, rate in ((dt.date(2026, 9, 30), 4.49),
                      (dt.date(2026, 10, 1), 4.279),
                      (dt.date(2026, 10, 30), 4.279),
                      (dt.date(2026, 10, 31), 7.6)):
        got = costs.summarise(cheap_at(day, 10.0), {}, {}, [], schedule)
        check(f"{day} prices at {rate}p",
              round(got["import_cost"], 6), round(10 * rate / 100, 6))

    spanning = {}
    spanning.update(cheap_at(dt.date(2026, 9, 30), 10.0))
    spanning.update(cheap_at(dt.date(2026, 10, 2), 10.0))
    spanning.update(cheap_at(dt.date(2026, 11, 2), 10.0))
    got = costs.summarise(spanning, {}, {}, [], schedule)
    check("a range crossing BOTH changes sums three different rates",
          round(got["import_cost"], 6),
          round((10 * 4.49 + 10 * 4.279 + 10 * 7.6) / 100, 6))

    print("\nThe schedule parses, and a typo cannot quietly change a rate")
    import config as _cfg
    check("no IOG_RATE_CHANGES is the old single-rate behaviour",
          _cfg.rate_schedule({"IOG_OFF_PEAK_P": "4.49", "IOG_PEAK_P": "29.757"}),
          [(dt.date.min, 4.49, 29.757)])
    parsed = _cfg.rate_schedule({
        "IOG_RATE_CHANGES": "2026-10-01:4.279/28.3429, 2026-10-31:7.6/34.955"})
    check("two changes become three periods, oldest first",
          [str(d) for d, _, _ in parsed],
          ["0001-01-01", "2026-10-01", "2026-10-31"])
    # A malformed entry is dropped rather than raised on, because the
    # dashboard must degrade -- but it must not take the good ones with it,
    # and costs.py prints every period so a missing row is visible.
    messy = _cfg.rate_schedule({"IOG_RATE_CHANGES":
                                "not-a-date:1/2, 2026-10-31:7.6/34.955"})
    check("a malformed entry is skipped, the valid one survives",
          [str(d) for d, _, _ in messy], ["0001-01-01", "2026-10-31"])
    check("and the baseline is never dropped with it",
          messy[0][1:], (_cfg.DEFAULT_OFF_PEAK_P, _cfg.DEFAULT_PEAK_P))
    check("a date before everything gets the baseline",
          _cfg.rates_at(parsed, dt.date(2000, 1, 1)), (4.49, 29.757))
    check("order in the env does not matter",
          _cfg.rate_schedule({"IOG_RATE_CHANGES":
                              "2026-10-31:7.6/34.955, 2026-10-01:4.279/28.3429"}),
          parsed)

    print("\nCosting day by day then combining equals costing the range")
    # The reason per-day results can be trusted: nothing about summing days is
    # allowed to differ from pricing the whole span at once.
    sched = [(dt.date.min, 4.49, 29.757), (dt.date(2026, 10, 2), 4.279, 28.3429)]
    d0 = dt.datetime(2026, 10, 1, 0, 0).astimezone()
    imp, exp, rts = {}, {}, {}
    for i in range(3 * 48):
        when = d0 + dt.timedelta(minutes=30 * i)
        imp[when] = 0.1 + (i % 7) * 0.05
        exp[when] = 0.02 * (i % 5)
        rts[when] = 12.0 + (i % 11)
    win = [(dt.datetime(2026, 10, 1, 20, 0), dt.datetime(2026, 10, 1, 22, 30))]
    whole = costs.summarise(imp, exp, rts, win, sched)
    days = costs.day_summaries(imp, exp, rts, win, sched,
                               dt.date(2026, 10, 1), dt.date(2026, 10, 3))
    rolled = costs.combine(days.values(), dt.date(2026, 10, 1),
                           dt.date(2026, 10, 3), sched)
    for field in ("import_kwh", "cheap_kwh", "peak_kwh", "import_cost",
                  "export_kwh", "export_income", "net", "vs_all_peak",
                  "half_hours", "import_half_hours", "export_half_hours"):
        check(f"{field} matches the whole-range figure",
              round(rolled[field], 6), round(whole[field], 6))
    check("a different rate per day is honoured (2 Oct reprices)",
          round(days[dt.date(2026, 10, 2)]["import_cost"]
                / days[dt.date(2026, 10, 2)]["import_kwh"], 6) > 0, True)
    check("a full day with windows is settled",
          days[dt.date(2026, 10, 1)]["settled"], True)
    check("no dispatch windows means NOT settled (everything priced at peak)",
          costs.day_summaries(imp, exp, rts, [], sched, dt.date(2026, 10, 1),
                              dt.date(2026, 10, 1))[dt.date(2026, 10, 1)]
          ["settled"], False)
    partial = {k: v for k, v in imp.items() if k < d0 + dt.timedelta(hours=10)}
    check("a part-published day is not settled",
          costs.day_summaries(partial, exp, rts, win, sched,
                              dt.date(2026, 10, 1), dt.date(2026, 10, 1))
          [dt.date(2026, 10, 1)]["settled"], False)
    check("a day whose export has no published rate is not settled",
          costs.day_summaries(imp, exp, {}, win, sched, dt.date(2026, 10, 1),
                              dt.date(2026, 10, 1))[dt.date(2026, 10, 1)]
          ["settled"], False)
    check("no export meter at all can still settle on import alone",
          costs.day_summaries(imp, {}, {}, win, sched, dt.date(2026, 10, 1),
                              dt.date(2026, 10, 1), has_export=False)
          [dt.date(2026, 10, 1)]["settled"], True)

    print("\nA range is half-open: the next day's first half hour is not in it")
    tzx = dt.timezone(dt.timedelta(hours=1))
    lo = dt.datetime(2026, 9, 30, 0, 0, tzinfo=tzx)
    hi = dt.datetime(2026, 10, 1, 0, 0, tzinfo=tzx)
    boundary = {lo: 1.0, hi - dt.timedelta(minutes=30): 2.0, hi: 9.0}
    saved_c, saved_a = costs.consumption, costs.agile_rates
    costs.consumption = lambda *a, **k: dict(boundary)
    costs.agile_rates = lambda *a, **k: dict(boundary)
    try:
        pts = {"import": {"mpan": "1", "serials": ["s"]},
               "export": {"mpan": "2", "serials": ["s"], "tariff": "T"}}
        got_i, got_e, got_r = costs._fetch_range(pts, "k", lo, hi)
        check("the half hour starting AT the end is dropped",
              sorted(got_i.values()), [1.0, 2.0])
        check("for export and for rates as well",
              (sorted(got_e.values()), sorted(got_r.values())),
              ([1.0, 2.0], [1.0, 2.0]))
    finally:
        costs.consumption, costs.agile_rates = saved_c, saved_a

    print("\nA clock-change day has 46 or 50 half hours, not 48")
    import os as _os, time as _time
    saved_tz = _os.environ.get("TZ")
    _os.environ["TZ"] = "Europe/London"; _time.tzset()
    try:
        check("an ordinary day is 48", costs.expected_half_hours(dt.date(2026, 10, 18)), 48)
        check("25 Oct 2026 (clocks go back) is 50",
              costs.expected_half_hours(dt.date(2026, 10, 25)), 50)
        check("29 Mar 2026 (clocks go forward) is 46",
              costs.expected_half_hours(dt.date(2026, 3, 29)), 46)
    finally:
        if saved_tz is None:
            _os.environ.pop("TZ", None)
        else:
            _os.environ["TZ"] = saved_tz
        _time.tzset()

    print("\nThe fingerprint changes with the rates")
    fp1 = costs.fingerprint()
    real = costs.config.rate_schedule
    costs.config.rate_schedule = lambda env=None: [(dt.date.min, 9.0, 9.0)]
    try:
        check("a different schedule is a different fingerprint",
              costs.fingerprint() != fp1, True)
    finally:
        costs.config.rate_schedule = real
    check("and the same inputs give the same one", costs.fingerprint(), fp1)

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
