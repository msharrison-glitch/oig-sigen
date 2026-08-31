#!/usr/bin/env python3
"""
Offline tests for the dispatch schedule logic.

No network, no credentials. Checks the parts that are easy to get subtly
wrong: BST/GMT handling on the fixed off-peak window, merging a bonus slot
that butts onto it, and the horizon filter.

    python3 test_octopus.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import octopus as O

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<56} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def local(text: str) -> datetime:
    """Parse a local wall-clock string into UTC."""
    naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=O.LOCAL_TZ).astimezone(timezone.utc)


def main() -> int:
    print("\nFixed off-peak window, British Summer Time")
    # 30 Aug is BST (UTC+1), so 23:30 local is 22:30 UTC.
    now = local("2026-08-30 15:00")
    windows = O.off_peak_windows(now, 24)
    tonight = [w for w in windows if w.start > now][0]
    check("23:30 BST starts at 22:30 UTC",
          tonight.start.strftime("%H:%M"), "22:30")
    check("05:30 BST ends at 04:30 UTC",
          tonight.end.strftime("%H:%M"), "04:30")
    check("window is 6 hours", tonight.minutes, 360.0)

    print("\nFixed off-peak window, Greenwich Mean Time")
    # 15 Dec is GMT (UTC+0), so the UTC times shift by an hour.
    winter = local("2026-12-15 15:00")
    w_windows = O.off_peak_windows(winter, 24)
    w_tonight = [w for w in w_windows if w.start > winter][0]
    check("23:30 GMT starts at 23:30 UTC",
          w_tonight.start.strftime("%H:%M"), "23:30")
    check("still 6 hours", w_tonight.minutes, 360.0)

    print("\nClock-change night")
    # BST ends 25 Oct 2026: the 23:30-05:30 window spans the transition and
    # is 7 hours of wall clock, not 6.
    autumn = local("2026-10-24 15:00")
    a_windows = O.off_peak_windows(autumn, 24)
    a_tonight = [w for w in a_windows if w.start > autumn][0]
    check("spring-back night is 7 real hours", a_tonight.minutes, 420.0)

    print("\nDispatch parsing")
    payload = {
        "plannedDispatches": [
            {"startDt": "2026-08-30T13:00:00Z",
             "endDt": "2026-08-30T13:30:00Z",
             "deltaKwh": "-3.5", "meta": {"source": None}},
            {"startDt": "2026-08-30T22:00:00+00:00",
             "endDt": "2026-08-30T22:30:00+00:00",
             "deltaKwh": None, "meta": {"source": None}},
        ]
    }
    slots = O.parse_dispatches(payload)
    check("two dispatches parsed", len(slots), 2)
    check("negative deltaKwh becomes positive magnitude",
          slots[0].kwh, 3.5)
    check("Z suffix parsed as UTC",
          slots[0].start.isoformat(), "2026-08-30T13:00:00+00:00")
    check("null deltaKwh tolerated", slots[1].kwh, None)

    print("\nMerging")
    # The 22:00-22:30 UTC dispatch butts directly onto the 22:30 UTC start
    # of tonight's off-peak window -- they must become one period.
    combined = O.merge([tonight] + slots)
    spanning = [s for s in combined if s.start.strftime("%H:%M") == "22:00"]
    check("adjacent dispatch and off-peak merged into one", len(spanning), 1)
    check("merged period is 6.5 hours", spanning[0].minutes, 390.0)
    check("merged period is labelled as both",
          spanning[0].source, "off-peak+dispatch")
    check("the 13:00 dispatch stays separate",
          len([s for s in combined
               if s.start.strftime("%H:%M") == "13:00"]), 1)

    print("\nNon-adjacent slots are not merged")
    far = O.Slot(local("2026-08-30 09:00"), local("2026-08-30 09:30"),
                 "dispatch")
    near = O.Slot(local("2026-08-30 11:00"), local("2026-08-30 11:30"),
                  "dispatch")
    check("gap preserved", len(O.merge([far, near])), 2)

    print("\nOverlapping slots collapse")
    a = O.Slot(local("2026-08-30 09:00"), local("2026-08-30 10:00"),
               "dispatch")
    b = O.Slot(local("2026-08-30 09:30"), local("2026-08-30 11:00"),
               "dispatch")
    merged = O.merge([a, b])
    check("two overlapping become one", len(merged), 1)
    check("union spans both", merged[0].minutes, 120.0)

    print("\nBonus isolation: what is left once Sigen AI has the window")
    # The only part worth commanding on a plant whose own EMS already covers
    # 23:30-05:30 is the time Octopus adds outside it.
    from zoneinfo import ZoneInfo as _Z
    _tz = _Z("Europe/London")
    _base = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    _guaranteed = O.off_peak_windows(_base, 24)

    def _d(day, h, m, dur):
        st = datetime(2026, 8, day, h, m, tzinfo=_tz).astimezone(timezone.utc)
        return O.Slot(st, st + timedelta(minutes=dur), "dispatch", 5.0)

    def _frag(slot):
        return [(f.local()[0].strftime("%H:%M"), f.local()[1].strftime("%H:%M"))
                for f in O.merge(O.subtract([slot], _guaranteed))]

    check("a slot outside the window survives whole",
          _frag(_d(30, 14, 0, 30)), [("14:00", "14:30")])
    check("a slot abutting the start is trimmed at 23:30",
          _frag(_d(30, 23, 0, 60)), [("23:00", "23:30")])
    check("a slot inside the window is dropped entirely",
          _frag(_d(31, 1, 0, 30)), [])
    check("a slot straddling the end keeps only the tail",
          _frag(_d(31, 5, 0, 60)), [("05:30", "06:00")])
    check("a slot spanning the window splits in two",
          _frag(_d(30, 22, 0, 9 * 60)),
          [("22:00", "23:30"), ("05:30", "07:00")])
    check("subtracting nothing changes nothing",
          len(O.subtract([_d(30, 14, 0, 30)], [])), 1)

    print("\nA completed dispatch is evidence the car actually charged")
    from datetime import timezone as _tz
    base = datetime(2026, 8, 31, 22, 0, tzinfo=_tz.utc)
    client = O.OctopusClient("k", "A-1")

    def completed(*offsets):
        client._last_payload = {"completedDispatches": [
            {"startDt": (base + timedelta(minutes=a)).isoformat(),
             "endDt": (base + timedelta(minutes=b)).isoformat()}
            for a, b in offsets]}

    completed()
    check("nothing completed -> None",
          client.recent_completion(base), None)
    completed((-60, -30))
    check("finished 30 min ago -> counts",
          client.recent_completion(base) is not None, True)
    completed((-120, -90))
    check("finished 90 min ago -> too old",
          client.recent_completion(base), None)
    completed((-60, -30), (-30, 0))
    got = client.recent_completion(base)
    check("picks the most recent",
          got.end, base)

    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
