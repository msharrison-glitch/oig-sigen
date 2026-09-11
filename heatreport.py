#!/usr/bin/env python3
"""
Put the heat pump next to the bonus slots. READ-ONLY.

Two records exist and have never been compared. `observe.log` knows when the
agent commanded a bonus slot and what the plant did; `.daikin-history.jsonl`
knows what the heat pump was doing. The business case for shifting heat --
docs/proposal-ashp-load-shift.md -- rests entirely on a number neither file
answers alone:

    how many bonus-slot minutes actually coincide with heating demand?

Everything else in that proposal is arithmetic. This is the measurement. It
exists now, before the heating season, so that the question can be answered
from the first cold week rather than a fortnight after it.

Writes nothing, commands nothing, and needs no credentials or network.

    python3 heatreport.py
    python3 heatreport.py --log observe.log --history .daikin-history.jsonl

A note on time. `observe.log` writes naive local timestamps; the heat pump
history writes ISO-8601 with an offset. Both are local time on the same host,
so the offset is dropped and everything is compared naive. That is correct
here and would NOT be correct if the two ever came from different machines.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys

from config import state_path

HISTORY_FILE = ".daikin-history.jsonl"
CONSUMPTION_FILE = ".daikin-consumption.json"
DEFAULT_LOG = "observe.log"

# "2026-09-10 22:35:26 INFO    cloud: selected charging profile 9664 ..."
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\w+\s+(.*)$")
SOC = re.compile(r"SOC\s+([\d.]+)%")


def parse_time(text: str):
    """Naive local datetime from either file's format."""
    text = text.strip()
    try:
        return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    # ISO with an offset, e.g. 2026-09-11T17:30:05+0100. Drop the offset
    # rather than convert: both sources are local time on the same host.
    cleaned = text.replace("T", " ")
    cleaned = re.sub(r"[+-]\d{2}:?\d{2}$", "", cleaned).strip()
    return dt.datetime.strptime(cleaned[:19], "%Y-%m-%d %H:%M:%S")


def parse_slots(lines) -> list:
    """Charging periods the agent actually commanded.

    A slot is a STARTED..RELEASED pair. Deliberately NOT every line mentioning
    a dispatch: a planned dispatch the agent declined (because the car never
    drew, or the battery was already full) is not a period during which we
    were buying cheap electricity, and counting it would inflate the very
    number this report exists to measure.
    """
    slots, open_slot = [], None
    for raw in lines:
        m = LOG_LINE.match(raw)
        if not m:
            continue
        when, rest = parse_time(m.group(1)), m.group(2)
        soc = SOC.search(rest)
        soc = float(soc.group(1)) if soc else None

        if "STARTED charging" in rest:
            open_slot = {"start": when, "soc_start": soc}
        elif ("RELEASED" in rest or "WITHDRAWN" in rest) and open_slot:
            open_slot["end"] = when
            open_slot["soc_end"] = soc
            slots.append(open_slot)
            open_slot = None
    if open_slot:                       # still charging at the end of the log
        open_slot["end"] = None
        slots.append(open_slot)
    return slots


def load_history(path) -> list:
    out = []
    try:
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            when = rec.get("fetched_at")
            if not when:
                continue
            rec["_t"] = parse_time(when)
            out.append(rec)
    except FileNotFoundError:
        return []
    out.sort(key=lambda r: r["_t"])
    return out


def heating_on(rec: dict) -> bool:
    point = (rec.get("points") or {}).get("climateControl") or {}
    return point.get("onOffMode") == "on"


def sensor(rec: dict, point: str, name: str):
    node = (rec.get("points") or {}).get(point) or {}
    return (node.get("sensors") or {}).get(name)


def overlap(slots: list, history: list) -> dict:
    """How much of the commanded cheap time we have heat pump data for."""
    total_minutes = 0.0
    observed = 0
    heating = 0
    for slot in slots:
        if not slot.get("end"):
            continue
        total_minutes += (slot["end"] - slot["start"]).total_seconds() / 60.0
        inside = [r for r in history if slot["start"] <= r["_t"] <= slot["end"]]
        observed += len(inside)
        heating += sum(1 for r in inside if heating_on(r))
    return {"slot_minutes": total_minutes,
            "observations_inside": observed,
            "heating_on_inside": heating}


def gaps(history: list, threshold_minutes: float = 60.0) -> list:
    out = []
    for a, b in zip(history, history[1:]):
        delta = (b["_t"] - a["_t"]).total_seconds() / 60.0
        if delta > threshold_minutes:
            out.append((a["_t"], b["_t"], delta))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Heat pump against bonus slots. Read-only.")
    parser.add_argument("--history", default=str(state_path(HISTORY_FILE)))
    parser.add_argument("--consumption",
                        default=str(state_path(CONSUMPTION_FILE)))
    parser.add_argument("--log", default=DEFAULT_LOG)
    args = parser.parse_args()

    history = load_history(args.history)
    try:
        lines = io.open(args.log, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"no agent log at {args.log}", file=sys.stderr)
        lines = []
    slots = parse_slots(lines)

    print("\nHeat pump observations")
    print("-" * 62)
    if not history:
        print(f"  none yet at {args.history}")
    else:
        first, last = history[0]["_t"], history[-1]["_t"]
        span = (last - first).total_seconds() / 3600.0
        on = sum(1 for r in history if heating_on(r))
        print(f"  {len(history)} records, {first} to {last} ({span:.1f} h)")
        print(f"  heating on in {on} of {len(history)} "
              f"({100.0 * on / len(history):.0f}%)")
        big = gaps(history)
        if big:
            worst = max(big, key=lambda g: g[2])
            print(f"  {len(big)} gap(s) over an hour; largest "
                  f"{worst[2] / 60:.1f} h from {worst[0]}")
        last_rec = history[-1]
        print(f"  latest: outdoor={sensor(last_rec,'climateControl','outdoorTemperature')}  "
              f"room={sensor(last_rec,'climateControl','roomTemperature')}  "
              f"leavingWater={sensor(last_rec,'climateControl','leavingWaterTemperature')}  "
              f"tank={sensor(last_rec,'domesticHotWaterTank','tankTemperature')}")

    print("\nBonus slots the agent actually commanded")
    print("-" * 62)
    if not slots:
        print("  none found in the log")
    else:
        for slot in slots[-12:]:
            end = slot.get("end")
            if end:
                mins = (end - slot["start"]).total_seconds() / 60.0
                span = f"{slot['start']:%Y-%m-%d %H:%M} - {end:%H:%M}  {mins:5.0f} min"
            else:
                span = f"{slot['start']:%Y-%m-%d %H:%M} - (still open)      "
            soc = ""
            if slot.get("soc_start") is not None and slot.get("soc_end") is not None:
                soc = f"   SOC {slot['soc_start']:.1f}% -> {slot['soc_end']:.1f}%"
            print(f"  {span}{soc}")
        closed = [s for s in slots if s.get("end")]
        total = sum((s["end"] - s["start"]).total_seconds() / 60.0
                    for s in closed)
        print(f"  {len(closed)} completed slots, {total:.0f} minutes total")

    print("\nThe join -- the number the business case rests on")
    print("-" * 62)
    stats = overlap(slots, history)
    print(f"  commanded cheap minutes:        {stats['slot_minutes']:.0f}")
    print(f"  heat pump observations inside:  {stats['observations_inside']}")
    print(f"  ...with heating ON:             {stats['heating_on_inside']}")
    if stats["slot_minutes"] and not stats["observations_inside"]:
        print("  -> the observer was not running during these slots, so this")
        print("     says nothing yet. It is not evidence of no overlap.")
    elif not stats["heating_on_inside"]:
        print("  -> no overlap observed. Expected before the heating season;")
        print("     revisit once the heating has actually been running.")

    try:
        store = json.loads(io.open(args.consumption, encoding="utf-8").read())
    except (FileNotFoundError, ValueError):
        store = {}
    if store:
        print("\nMonthly electricity (kWh), from the archive")
        print("-" * 62)
        years = {}
        for key, entry in store.items():
            year = key.split("-")[0]
            acc = years.setdefault(year, {"climateControl": 0,
                                          "domesticHotWaterTank": 0})
            for kind in acc:
                acc[kind] += entry.get(kind) or 0
        for year in sorted(years):
            acc = years[year]
            print(f"  {year}   space heating {acc['climateControl']:6d}   "
                  f"hot water {acc['domesticHotWaterTank']:6d}")
        print("  (the API keeps two calendar years; this archive outlives it)")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
