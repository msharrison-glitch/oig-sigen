#!/usr/bin/env python3
"""
Read-only overnight observer.

Writes NOTHING to the plant. Not a single register. This exists to answer the
question the whole project rests on: left entirely alone, does Sigen AI
grid-charge the battery during the IOG cheap window at 4.49p? If it does, the
controller adds little on nights like this. If it does not, every kWh it
leaves on the table is ours to take.

Samples straight to CSV, flushed every row, so a crash or a closed laptop
costs you the rest of the night rather than the whole of it. Every read is
wrapped: a transport blip records a blank row and the run continues, because
an observer that dies at 02:00 tells you nothing at 07:00.

    python3 watch.py --until 07:00 --out overnight.csv
    python3 watch.py --summary overnight.csv

The summary is the point. It reports how much energy moved, when, and what it
cost -- split by whether the half hour was cheap.
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import registers as R
from config import ConfigError, load_env, resolve_host
from octopus import LOCAL_TZ, OctopusClient, OctopusError, Slot, off_peak_windows, merge
from sigen import ModbusError, SigenClient

# IOG rates, pence per kWh. From CLAUDE.md; adjust if your tariff moves.
OFF_PEAK_P = 4.49
PEAK_P = 29.757

FIELDS = ["utc", "local", "cheap", "soc_pct", "ess_kw", "grid_kw", "pv_kw",
          "ems_enable", "ems_mode", "charge_limit_kw", "discharge_limit_kw",
          "error"]

# Eight reads at the 1 s floor is ~8 s of the sample interval. Keep the
# interval comfortably above that.
MIN_INTERVAL = 15.0

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    print(f"\n  caught signal {signum}; finishing the current sample")


def fetch_slots(hours: int) -> list[Slot]:
    """Cheap periods for the run. Falls back to the guaranteed window, which
    needs no network -- the observer must never depend on Octopus being up."""
    now = datetime.now(timezone.utc)
    try:
        env = load_env()
        client = OctopusClient(env["OCTOPUS_API_KEY"],
                               env["OCTOPUS_ACCOUNT_NUMBER"])
        slots = client.cheap_slots(hours, now)
        print(f"  schedule from Octopus: {len(slots)} cheap period(s)")
        return slots
    except (OctopusError, ConfigError, KeyError, OSError) as exc:
        print(f"  ! Octopus unavailable ({exc}); using the guaranteed "
              f"23:30-05:30 window only")
        return [s for s in merge(off_peak_windows(now, hours))
                if s.end > now]


def read_sample(client: SigenClient, slots: list[Slot]) -> dict:
    now = datetime.now(timezone.utc)
    row = {f: "" for f in FIELDS}
    row["utc"] = now.isoformat(timespec="seconds")
    row["local"] = now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    row["cheap"] = int(any(s.contains(now) for s in slots))
    try:
        row["soc_pct"] = R.read(client, R.ESS_SOC)
        row["ess_kw"] = R.read(client, R.ESS_POWER)
        row["grid_kw"] = R.read(client, R.GRID_ACTIVE_POWER)
        row["pv_kw"] = R.read(client, R.PV_POWER)
        row["ems_enable"] = client.read_u16(R.REMOTE_EMS_ENABLE.address)
        row["ems_mode"] = client.read_u16(R.REMOTE_EMS_MODE.address)
        charge = R.read(client, R.ESS_MAX_CHARGE_LIMIT)
        discharge = R.read(client, R.ESS_MAX_DISCHARGE_LIMIT)
        row["charge_limit_kw"] = "" if charge is None else charge
        row["discharge_limit_kw"] = "" if discharge is None else discharge
    except (ModbusError, OSError) as exc:
        # Never fatal. A gap in the record beats no record.
        row["error"] = str(exc)[:120]
    return row


def run(client: SigenClient, out: Path, interval: float,
        finish: datetime, slots: list[Slot]) -> int:
    new = not out.exists()
    with out.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        samples = 0
        while not _stop and datetime.now(timezone.utc) < finish:
            row = read_sample(client, slots)
            writer.writerow(row)
            handle.flush()          # survive a kill, keep what we have
            samples += 1
            if samples == 1 or samples % 10 == 0:
                mark = " CHEAP" if row["cheap"] else ""
                print(f"  {row['local']}{mark}  SOC {row['soc_pct']}%  "
                      f"ESS {row['ess_kw']} kW  grid {row['grid_kw']} kW"
                      + (f"  !! {row['error']}" if row["error"] else ""))
            remaining = (finish - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0 or _stop:
                break
            time.sleep(min(interval, remaining))
    print(f"\n  {samples} samples written to {out}")
    return 0


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def analyse(rows: list[dict]) -> dict | None:
    """Energy totals from the sample rows. Split out from the printing so
    the arithmetic can be tested without a CSV or a plant."""
    usable = [r for r in rows if _num(r["soc_pct"]) is not None]
    if len(usable) < 2:
        return None

    charged = {0: 0.0, 1: 0.0}     # kWh into the battery, by cheap flag
    discharged = {0: 0.0, 1: 0.0}
    imported = {0: 0.0, 1: 0.0}    # kWh off the grid, by cheap flag
    exported = {0: 0.0, 1: 0.0}

    for a, b in zip(usable, usable[1:]):
        hours = ((datetime.fromisoformat(b["utc"])
                  - datetime.fromisoformat(a["utc"])).total_seconds() / 3600)
        if hours <= 0 or hours > 1:        # ignore gaps longer than an hour
            continue
        cheap = int(a["cheap"] or 0)
        for key_a, key_b, into, out_of in (
                ("ess_kw", "ess_kw", charged, discharged),
                ("grid_kw", "grid_kw", imported, exported)):
            pa, pb = _num(a[key_a]), _num(b[key_b])
            if pa is None or pb is None:
                continue
            mean = (pa + pb) / 2           # trapezoid over the interval
            if mean > 0:
                into[cheap] += mean * hours
            else:
                out_of[cheap] += -mean * hours

    first, last = usable[0], usable[-1]
    return {
        "samples": len(usable),
        "first": first, "last": last,
        "span_h": (datetime.fromisoformat(last["utc"])
                   - datetime.fromisoformat(first["utc"])).total_seconds()
                  / 3600,
        "cheap_samples": sum(1 for r in usable if str(r["cheap"]) == "1"),
        "ems_on": sum(1 for r in usable if str(r["ems_enable"]) == "1"),
        "charged": charged, "discharged": discharged,
        "imported": imported, "exported": exported,
    }


def summarise(path: Path) -> int:
    a = analyse(list(csv.DictReader(path.open())))
    if a is None:
        print(f"{path}: not enough usable samples")
        return 1
    first, last = a["first"], a["last"]
    charged, discharged = a["charged"], a["discharged"]
    imported, exported = a["imported"], a["exported"]
    cheap_samples, ems_on = a["cheap_samples"], a["ems_on"]

    print(f"\n  {path}  --  {a['samples']} samples over "
          f"{a['span_h']:.1f} h")
    print(f"  {first['local']}  ->  {last['local']}")
    print("=" * 68)
    print(f"  SOC {_num(first['soc_pct']):.1f}% -> "
          f"{_num(last['soc_pct']):.1f}%")
    print(f"  cheap-rate samples: {cheap_samples} of {a['samples']}")
    print(f"  remote EMS enabled during: {ems_on} sample(s)"
          + ("  <- something held a lease" if ems_on else ""))
    print()
    print(f"  battery charged    {charged[1]:6.2f} kWh cheap   "
          f"{charged[0]:6.2f} kWh other")
    print(f"  battery discharged {discharged[1]:6.2f} kWh cheap   "
          f"{discharged[0]:6.2f} kWh other")
    print(f"  grid imported      {imported[1]:6.2f} kWh cheap   "
          f"{imported[0]:6.2f} kWh other")
    print(f"  grid exported      {exported[1]:6.2f} kWh cheap   "
          f"{exported[0]:6.2f} kWh other")
    cost = (imported[1] * OFF_PEAK_P + imported[0] * PEAK_P) / 100
    print(f"  import cost        £{cost:.2f} "
          f"(at {OFF_PEAK_P}p / {PEAK_P}p)")
    print("=" * 68)

    # The question this whole run exists to answer.
    if charged[1] >= 1.0:
        print(f"  VERDICT: the battery took {charged[1]:.2f} kWh during cheap"
              f" periods.")
        if ems_on:
            print("  But remote EMS was enabled for part of the run, so this"
                  "\n  is not a clean observation of Sigen AI acting alone.")
        else:
            print("  Nothing of ours commanded it, so Sigen AI grid-charges"
                  "\n  unaided. The controller's value on nights like this is"
                  "\n  correspondingly smaller -- it is the bonus slots, not"
                  "\n  the guaranteed window, where it would earn its keep.")
    else:
        missed = max(0.0, discharged[1] + discharged[0])
        print(f"  VERDICT: the battery took only {charged[1]:.2f} kWh during"
              f" cheap periods.")
        print("  Sigen AI did NOT meaningfully grid-charge on its own. That is"
              "\n  the gap this project exists to close: every kWh it declined"
              "\n  to buy at 4.49p is one bought later at up to 29.757p.")
        if missed:
            print(f"  For scale, {missed:.1f} kWh left the battery over the"
                  f" run.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", nargs="?")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--out", type=Path, default=Path("overnight.csv"))
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--minutes", type=float,
                    help="run for this long (default 600)")
    ap.add_argument("--until", metavar="HH:MM",
                    help="run until this local time, e.g. 07:00")
    ap.add_argument("--summary", type=Path,
                    help="summarise an existing CSV and exit")
    args = ap.parse_args()

    if args.summary:
        if not args.summary.exists():
            print(f"no such file: {args.summary}", file=sys.stderr)
            return 1
        return summarise(args.summary)

    if args.interval < MIN_INTERVAL:
        print(f"--interval must be at least {MIN_INTERVAL:.0f}s: eight reads "
              f"at the 1 s Modbus floor take ~8 s.", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    if args.until:
        hh, _, mm = args.until.partition(":")
        local = now.astimezone(LOCAL_TZ)
        finish_local = local.replace(hour=int(hh), minute=int(mm or 0),
                                     second=0, microsecond=0)
        if finish_local <= local:
            finish_local += timedelta(days=1)
        finish = finish_local.astimezone(timezone.utc)
    else:
        finish = now + timedelta(minutes=args.minutes or 600)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    hours = int((finish - now).total_seconds() / 3600) + 2
    print(f"\n  Observing until {finish.astimezone(LOCAL_TZ):%a %d %b %H:%M} "
          f"local, every {args.interval:.0f}s")
    print("  READ ONLY -- this writes nothing to the plant.\n")
    slots = fetch_slots(hours)

    try:
        host = resolve_host(args.host)
        with SigenClient(host, port=args.port) as client:
            return run(client, args.out, args.interval, finish, slots)
    except (ConfigError, ModbusError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
