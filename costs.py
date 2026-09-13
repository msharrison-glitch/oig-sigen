#!/usr/bin/env python3
"""
What the electricity actually cost. READ-ONLY.

Everything else in this project counts kilowatt-hours. This counts pounds,
which is the only unit that answers "was any of this worth doing".

    python3 costs.py                       # yesterday
    python3 costs.py --days 7
    python3 costs.py --from 2026-09-01 --to 2026-09-13

WHERE THE NUMBERS COME FROM, and why each source rather than an easier one:

  ENERGY -- Octopus's own half-hourly meter readings, per MPAN. Not the
  plant's figures. The SigenStor's grid CT and the settlement meter are
  different instruments and will not agree exactly; only one of them is what
  you are billed on.

  EXPORT PRICE -- the Agile Outgoing rate table, half-hourly, exact.

  IMPORT PRICE -- derived, and this is the interesting part. Octopus does NOT
  publish IOG rates through the rates API: it returns zero periods, because
  which half-hours are cheap depends on YOUR dispatches. So a half hour is
  priced off-peak if it falls in the guaranteed 23:30-05:30 window, or if a
  dispatch covered it.

  **The Sigen cloud cannot be used for this, and the reason is the whole
  point of the project.** Its BUY_TARIFF series knows only the static IOG
  schedule. Checked 2026-09-13: the agent charged 20:12-22:00 during a
  confirmed bonus slot at 4.49p, and Sigen reported 29.76p for every one of
  those half hours, dropping to 4.49p only at 23:35. Costing from Sigen's
  data would price that 19 kWh at peak and show this project achieving
  exactly nothing.

  DISPATCHES -- `completedDispatches` is authoritative but Octopus keeps only
  a short window (3 entries, all today, when checked). The durable record is
  `observe.log`: the agent writes "SCHEDULE + added HH:MM -> HH:MM [dispatch]"
  every time one appears, 65 of them since 31 August. Octopus re-plans
  constantly, so these overlap heavily and must be merged.

CAVEAT ON RECENCY: meter readings lag roughly 22 hours. "Today" will be
missing or partial, and that is the meter's doing, not a bug here. Ask for
yesterday.

Standing charges are NOT included -- this is unit cost only, so it will not
reconcile with a bill on its own.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import re
import sys
import urllib.parse
import urllib.request

from config import ConfigError, load_env

API = "https://api.octopus.energy/v1"
TIMEOUT = 30.0

# Same defaults and overrides as watch.py, so one .env drives both.
DEFAULT_OFF_PEAK_P = 4.49
DEFAULT_PEAK_P = 29.757

# The window IOG guarantees regardless of dispatches, local time.
GUARANTEED = (dt.time(23, 30), dt.time(5, 30))

# "2026-09-13 21:00:31 INFO    SCHEDULE + added   21:00 -> 23:30 [dispatch]"
SCHEDULE_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\s+\w+\s+"
    r"SCHEDULE \+ added\s+(\d{2}):(\d{2})\s*->\s*(\d{2}):(\d{2})")


def _iso(text: str) -> dt.datetime:
    """Octopus mixes "...Z" and "...+01:00"; 3.9's fromisoformat rejects Z.

    The floor for this project is Python 3.9 (see CLAUDE.md), so this cannot
    just wait for 3.11 to handle it.
    """
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class CostError(RuntimeError):
    """Anything Octopus would not answer."""


def rates_from_env() -> tuple:
    try:
        env = load_env()
    except ConfigError:
        return DEFAULT_OFF_PEAK_P, DEFAULT_PEAK_P

    def rate(key, default):
        try:
            return float(env.get(key, "") or default)
        except ValueError:
            return default
    return (rate("IOG_OFF_PEAK_P", DEFAULT_OFF_PEAK_P),
            rate("IOG_PEAK_P", DEFAULT_PEAK_P))


def _get(path: str, key: str) -> dict:
    auth = base64.b64encode(f"{key}:".encode()).decode()
    request = urllib.request.Request(
        path if path.startswith("http") else f"{API}{path}",
        headers={"Authorization": f"Basic {auth}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except Exception as exc:                          # noqa: BLE001
        raise CostError(f"{type(exc).__name__}: {exc}") from exc


def meter_points(key: str, account: str) -> dict:
    """The live import and export meter points, with their tariff codes.

    Takes the agreement that is CURRENT rather than the last in the list.
    The list is not ordered by recency and contains zero-length artefacts --
    this account has an OUTGOING-VAR row valid from 2026-06-02 to 2026-06-02
    sitting after the live Agile Outgoing agreement, and reading the last
    entry would have reported a flat 12p export tariff for a house actually
    on a rate that ranges 15-28p.
    """
    data = _get(f"/accounts/{account}/", key)
    today = dt.date.today().isoformat()
    out = {}
    for prop in data.get("properties") or []:
        for point in prop.get("electricity_meter_points") or []:
            kind = "export" if point.get("is_export") else "import"
            live = None
            for agreement in point.get("agreements") or []:
                start = (agreement.get("valid_from") or "")[:10]
                end = (agreement.get("valid_to") or "9999-12-31")[:10]
                if start <= today < end:
                    live = agreement
            if not live:
                continue
            serials = [m.get("serial_number")
                       for m in (point.get("meters") or [])]
            out[kind] = {"mpan": point["mpan"],
                         "serials": serials,
                         "tariff": live.get("tariff_code")}
    return out


def consumption(mpan, serials, start, end, key) -> dict:
    """{half-hour start (aware) -> kWh}. Tries each serial; meters get swapped.

    A property accumulates meter serials over the years and only the current
    one answers. Trying them in turn is cheaper than working out which.
    """
    query = urllib.parse.urlencode({
        "period_from": start.isoformat(),
        "period_to": end.isoformat(),
        "page_size": 25000,
        "order_by": "period",
    })
    for serial in serials:
        try:
            data = _get(f"/electricity-meter-points/{mpan}/meters/"
                        f"{serial}/consumption/?{query}", key)
        except CostError:
            continue
        rows = data.get("results") or []
        if rows:
            return {_iso(r["interval_start"]):
                    r["consumption"] for r in rows}
    return {}


def agile_rates(tariff: str, start, end, key) -> dict:
    """{half-hour start (aware) -> pence inc VAT} for a half-hourly tariff."""
    # E-1R-AGILE-OUTGOING-19-05-13-M  ->  AGILE-OUTGOING-19-05-13
    parts = tariff.split("-")
    product = "-".join(parts[2:-1])
    query = urllib.parse.urlencode({
        "period_from": start.isoformat(),
        "period_to": end.isoformat(),
        "page_size": 1500,
    })
    out = {}
    url = (f"/products/{product}/electricity-tariffs/{tariff}"
           f"/standard-unit-rates/?{query}")
    while url:
        data = _get(url, key)
        for row in data.get("results") or []:
            out[_iso(row["valid_from"])] = \
                row["value_inc_vat"]
        url = data.get("next")
    return out


def dispatch_windows(lines, tz=None) -> list:
    """Bonus-slot windows, merged, from the agent's own log.

    Octopus re-plans constantly -- the same slot is re-added every few
    minutes with a later start -- so these overlap heavily and are merged.
    The agent runs --bonus-only, so the guaranteed window is already
    subtracted and is added back separately.
    """
    spans = []
    for raw in lines:
        found = SCHEDULE_LINE.match(raw)
        if not found:
            continue
        day, sh, sm, eh, em = found.groups()
        date = dt.date.fromisoformat(day)
        start = dt.datetime.combine(date, dt.time(int(sh), int(sm)))
        end = dt.datetime.combine(date, dt.time(int(eh), int(em)))
        if end <= start:                       # crosses midnight
            end += dt.timedelta(days=1)
        spans.append((start, end))
    spans.sort()
    merged = []
    for span in spans:
        if merged and span[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(list(span) if False else (span[0], span[1]))
            merged[-1] = [span[0], span[1]]
    return [(a, b) for a, b in merged]


def in_guaranteed(moment) -> bool:
    """The 23:30-05:30 window, which wraps midnight."""
    clock = moment.time()
    return clock >= GUARANTEED[0] or clock < GUARANTEED[1]


def is_cheap(moment, windows) -> bool:
    if in_guaranteed(moment):
        return True
    naive = moment.replace(tzinfo=None)
    return any(a <= naive < b for a, b in windows)


def summarise(imported, exported, rates, windows, off_p, peak_p) -> dict:
    """Money. Everything above this line is measurement."""
    cheap_kwh = peak_kwh = 0.0
    cost_p = 0.0
    for when, kwh in imported.items():
        local = when.astimezone()
        if is_cheap(local, windows):
            cheap_kwh += kwh
            cost_p += kwh * off_p
        else:
            peak_kwh += kwh
            cost_p += kwh * peak_p

    income_p = 0.0
    priced = unpriced = 0.0
    for when, kwh in exported.items():
        rate = rates.get(when)
        if rate is None:
            unpriced += kwh
            continue
        priced += kwh
        income_p += kwh * rate

    return {
        "import_kwh": cheap_kwh + peak_kwh,
        "cheap_kwh": cheap_kwh,
        "peak_kwh": peak_kwh,
        "import_cost": cost_p / 100.0,
        "export_kwh": priced + unpriced,
        "export_unpriced_kwh": unpriced,
        "export_income": income_p / 100.0,
        "net": cost_p / 100.0 - income_p / 100.0,
        # A COUNTERFACTUAL, not a measurement: what this much import would
        # have cost extra at the peak rate. It is an upper bound and should
        # be labelled as one -- without a battery the house would not have
        # imported the same kilowatt-hours at all, it would have imported
        # less and at different times. Useful as an order of magnitude for
        # "what is the cheap window worth", not as a savings claim.
        "vs_all_peak": cheap_kwh * (peak_p - off_p) / 100.0,
        "half_hours": len(imported),
    }


def report(start_date, end_date, log_path="observe.log") -> dict:
    env = load_env()
    key = env["OCTOPUS_API_KEY"]
    account = env["OCTOPUS_ACCOUNT_NUMBER"]
    off_p, peak_p = rates_from_env()

    local = dt.datetime.now().astimezone().tzinfo
    start = dt.datetime.combine(start_date, dt.time(0, 0), tzinfo=local)
    end = dt.datetime.combine(end_date + dt.timedelta(days=1),
                              dt.time(0, 0), tzinfo=local)

    points = meter_points(key, account)
    if "import" not in points:
        raise CostError("no live import agreement found on this account")

    imported = consumption(points["import"]["mpan"],
                           points["import"]["serials"], start, end, key)
    exported = {}
    rates = {}
    if "export" in points:
        exported = consumption(points["export"]["mpan"],
                               points["export"]["serials"], start, end, key)
        rates = agile_rates(points["export"]["tariff"], start, end, key)

    try:
        lines = io.open(log_path, encoding="utf-8", errors="replace").readlines()
    except OSError:
        lines = []
    windows = dispatch_windows(lines)

    out = summarise(imported, exported, rates, windows, off_p, peak_p)
    out.update({"start": start_date, "end": end_date,
                "import_tariff": points["import"]["tariff"],
                "export_tariff": points.get("export", {}).get("tariff"),
                "off_peak_p": off_p, "peak_p": peak_p,
                "dispatch_windows": len(windows)})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="What the electricity cost. Read-only.")
    parser.add_argument("--days", type=int, default=1,
                        help="ending yesterday; meter data lags ~22h")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--log", default="observe.log")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.start:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end) if args.end else start
    else:
        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=args.days - 1)

    try:
        data = report(start, end, args.log)
    except (CostError, ConfigError, KeyError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, default=str, indent=1))
        return 0

    print(f"\n{data['start']} to {data['end']}   "
          f"({data['half_hours']} half-hours of meter data)")
    print("-" * 62)
    print(f"  imported        {data['import_kwh']:8.2f} kWh   "
          f"GBP {data['import_cost']:7.2f}")
    print(f"     at off-peak  {data['cheap_kwh']:8.2f} kWh   "
          f"@ {data['off_peak_p']:.2f}p")
    print(f"     at peak      {data['peak_kwh']:8.2f} kWh   "
          f"@ {data['peak_p']:.3f}p")
    print(f"  exported        {data['export_kwh']:8.2f} kWh   "
          f"GBP {data['export_income']:7.2f}")
    if data["export_unpriced_kwh"]:
        print(f"     unpriced     {data['export_unpriced_kwh']:8.2f} kWh   "
              f"(no rate published for those half-hours)")
    print("-" * 62)
    print(f"  NET                          GBP {data['net']:7.2f}")
    print(f"  off-peak import vs peak rate GBP {data['vs_all_peak']:7.2f}"
          f"   (counterfactual, upper bound)")
    print(f"\n  import {data['import_tariff']}")
    print(f"  export {data['export_tariff']}")
    print(f"  {data['dispatch_windows']} dispatch window(s) from the agent log")
    print("  standing charges are NOT included\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
