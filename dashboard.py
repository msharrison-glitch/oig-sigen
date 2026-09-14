#!/usr/bin/env python3
"""
Local energy dashboard. READ-ONLY, and structurally incapable of control.

One page showing what the house is actually doing -- solar, battery, grid,
load, EV, heat pump, and whatever Shelly devices you point it at -- with the
IOG tariff overlaid. That last part is the reason it exists: neither the
mySigen app nor the Shelly app knows Intelligent Go exists, so neither can
tell you whether the power you are watching is costing 4.49p or 29.757p.

    python3 dashboard.py --serve --port 8099
    python3 dashboard.py --once            # print a snapshot and exit

THE RULE THAT SHAPES THIS FILE: it never opens a Modbus connection to the
plant. The protocol mandates >= 1000 ms between requests, so a second reader
competes directly with the agent that commands your battery -- and a browser
tab left open overnight would starve it. Plant state therefore comes from
`observe.log`, which the agent writes on every tick anyway. Zero contention,
zero extra load, and the dashboard shows exactly what the agent saw.

Where each number comes from:

  solar / load / battery / grid / EV / heat pump
        -> Sigen cloud, device/sigen/station/energyflow/async
           `thirdPvPower` is the AC-coupled SolarEdge; `pvPower` is the
           SigenStor's own MPPTs and reads 0 on a plant like this one.
           `loadPower` is MEASURED here, which matters: from Modbus alone
           the balance PV + grid - ESS = load has two unknowns and cannot
           be solved.
  agent state, slot history
        -> observe.log, parsed with heatreport's existing helpers
  per-circuit power
        -> Shelly devices over local HTTP, no cloud, no rate limit

Every source is optional. A dead Shelly, an expired cloud token or a missing
log degrades to "unavailable" on the page rather than a stack trace, because
a dashboard that fails closed is just a blank screen at the moment you wanted
to look at something.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import heatreport
from config import load_env, ConfigError, state_path

# A Shelly with eco_mode on sleeps its wifi hard and takes a full 2 s to
# answer the first request -- measured on two Plus Plugs from the NAS. Five
# of those probed in parallel from a slow host pushed past a 4 s budget and
# reported perfectly healthy plugs as unreachable, so this is deliberately
# generous. One asleep device still cannot stall the page, because the probes
# run concurrently and the result is cached.
SHELLY_TIMEOUT = 8.0

# Eco mode means the FIRST request wakes the radio and may still time out.
# A single retry costs nothing when the device is awake and is the difference
# between "unreachable" and a reading when it is not.
SHELLY_RETRIES = 2

# The cloud call is the only one that leaves the network. A refresh-happy
# browser must not turn into a poll loop against someone else's API.
CACHE_SECONDS = 15.0

# The page refreshes itself; no JS, so this is the only way.
PAGE_REFRESH_SECONDS = 30

# The report is expensive (a Zappi call per day) and changes slowly.
REPORT_CACHE_SECONDS = 600.0

DEFAULT_LOG = "observe.log"

# Lifetime counters, written down so they can be differenced later. Nothing
# else records the plugs; see record_shellys().
SHELLY_HISTORY_FILE = ".shelly-history.jsonl"


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def read_sigen() -> dict:
    """Live flow from the Sigen cloud. Never raises."""
    try:
        import sigencloud
        client = sigencloud.client_from_env()
        client.ensure()
        data = client._call(
            "GET",
            f"device/sigen/station/energyflow/async"
            f"?id={client.station_id}&refreshFlag=false",
        ).get("data") or {}
    except Exception as exc:                      # noqa: BLE001 - deliberate
        return {"error": f"{type(exc).__name__}: {exc}"}

    def num(key):
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    return {
        # thirdPvPower is the SolarEdge; pvPower is the SigenStor's MPPTs.
        # Sum them so this works on either wiring without a config flag.
        "solar": (num("thirdPvPower") or 0.0) + (num("pvPower") or 0.0),
        "solar_mppt": num("pvPower"),
        "solar_third": num("thirdPvPower"),
        "load": num("loadPower"),
        "battery": num("batteryPower"),       # negative = discharging
        "grid": num("buySellPower"),          # positive = exporting
        "soc": num("batterySoc"),
        "ev": num("evPower"),
        "heat_pump": num("heatPumpPower"),
        "solar_today": num("pvDayNrg"),
        "on_grid": data.get("onGrid"),
    }


def read_shelly(host: str) -> dict:
    """One Shelly, whatever generation. Never raises."""
    out = {"host": host, "name": None, "gen": None, "channels": []}

    def get(path):
        url = f"http://{host}{path}"
        last = None
        for attempt in range(SHELLY_RETRIES):
            try:
                with urllib.request.urlopen(
                        url, timeout=SHELLY_TIMEOUT) as response:
                    return json.loads(response.read())
            except Exception as exc:              # noqa: BLE001
                last = exc
                # The first request is what wakes an eco-mode radio; by the
                # second it is usually listening. No backoff: the delay is
                # the device waking, not congestion.
        raise last

    try:
        info = get("/shelly")
    except Exception as exc:                      # noqa: BLE001
        out["error"] = f"{type(exc).__name__}"
        return out

    out["gen"] = info.get("gen", 1)
    out["model"] = info.get("model") or info.get("type")
    out["name"] = info.get("name")

    try:
        if out["gen"] >= 2:
            status = get("/rpc/Shelly.GetStatus")
            if not out["name"]:
                config = get("/rpc/Shelly.GetConfig")
                out["name"] = ((config.get("sys") or {})
                               .get("device") or {}).get("name")
            for key in sorted(status):
                node = status[key]
                if not isinstance(node, dict):
                    continue
                if key.startswith("switch:"):
                    out["channels"].append({
                        "id": key, "kind": "switch",
                        "on": node.get("output"),
                        "watts": node.get("apower"),
                        # aenergy.total is WATT-hours on Gen2, so this is a
                        # division not a rename. Stored raw it made a fridge
                        # look like it drew 3 kW.
                        "kwh": ((node.get("aenergy") or {}).get("total") or 0)
                               / 1000.0,
                    })
                elif key.startswith("em1:"):
                    # The matching em1data:N node carries the LIFETIME
                    # counter, in the same response. That matters: walking
                    # the device's minute history costs ~46 calls per channel
                    # per day at 31 records a call, which took 150 s for a
                    # single day and was hopeless for a month. Differencing
                    # the counter, exactly as the plugs must be handled,
                    # turns the whole report into one fast call.
                    meta = status.get("em1data:" + key.split(":", 1)[1]) or {}
                    out["channels"].append({
                        "id": key, "kind": "meter",
                        "watts": node.get("act_power"),
                        "volts": node.get("voltage"),
                        "kwh": (meta.get("total_act_energy") or 0) / 1000.0,
                    })
        else:
            status = get("/status")
            settings = get("/settings")
            out["name"] = settings.get("name")
            relays = status.get("relays") or []
            meters = status.get("meters") or []
            for i, relay in enumerate(relays):
                meter = meters[i] if i < len(meters) else {}
                out["channels"].append({
                    "id": f"relay:{i}", "kind": "switch",
                    "on": relay.get("ison"),
                    "watts": meter.get("power"),
                    # Gen1 counts watt-minutes.
                    "kwh": (meter.get("total") or 0) / 60000.0,
                })
    except Exception as exc:                      # noqa: BLE001
        out["error"] = f"{type(exc).__name__}"
    return out


AGENT_STATE_FILE = ".agent-state.json"


def read_agent_state(path=None) -> dict:
    """This tick's plant reading, published by the agent itself.

    Fresher and structured, where the log gives a formatted line up to five
    minutes old. Still not a Modbus connection: the agent writes what it
    already read, so this costs no extra traffic on a link that mandates
    >=1s between requests and is shared with the thing commanding the
    battery.
    """
    try:
        raw = json.loads(io.open(path or str(state_path(AGENT_STATE_FILE)),
                                 encoding="utf-8").read())
    except (OSError, ValueError):
        return {}
    try:
        raw["_t"] = datetime.fromisoformat(raw["local"])
    except (KeyError, ValueError):
        return {}
    return raw


def read_agent(log_path: str, state_file=None) -> dict:
    """What the agent last saw and did. Parses, never polls.

    Slot history comes from the log because only the log has history. The
    CURRENT reading prefers the state file and falls back to the log line,
    so a dashboard pointed at an agent too old to publish one still works.
    """
    out = {"log": log_path}
    try:
        lines = io.open(log_path, encoding="utf-8", errors="replace").readlines()
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}"
        return out

    out["slots"] = heatreport.parse_slots(lines)[-6:]
    for raw in reversed(lines[-400:]):
        if "SOC " in raw and "grid " in raw:
            out["last_tick"] = raw.strip()
            try:
                out["last_seen"] = heatreport.parse_time(raw[:19])
            except Exception:                     # noqa: BLE001
                pass
            out["action"] = raw.rstrip().split("-> ")[-1] if "-> " in raw else None
            break
    holding = [s for s in out.get("slots", []) if not s.get("end")]
    out["holding"] = bool(holding)

    published = read_agent_state(state_file)
    if published:
        out["state"] = published
        out["last_seen"] = published["_t"]
        out["action"] = published.get("action") or out.get("action")
        # The agent knows what it is holding; the log only implies it from an
        # unclosed slot, which is why parse_slots truncating on schedule churn
        # once made the two disagree on the same page.
        out["holding"] = bool(published.get("cloud_held")
                              or published.get("lease_held")) or out["holding"]
        out["source"] = "state file"
    else:
        out["source"] = "log"
    return out


def record_shellys(shelly_hosts, path=None) -> tuple:
    """Append one reading per channel, so the plugs get a history at all.

    THE REASON THIS EXISTS: a Shelly plug keeps a lifetime kWh counter and
    nothing else. There is no yesterday on the device, no last week, no last
    month -- ask it what it used on Tuesday and it cannot tell you, and never
    could. The Pro 3EM is the exception; it stores two months at one-minute
    resolution, which is why it needs none of this.

    So consumption per period has to be reconstructed by DIFFERENCING this
    counter between two readings, which means somebody has to write the
    readings down. Nothing else will. Every hour this does not run is an hour
    that cannot be recovered later, which is the same argument that put the
    Daikin monthly figures in a file.

    Cheap by construction: one line per channel per run, appended, no
    rewrite. Run it from cron or DSM Task Scheduler every few minutes.
    """
    path = path or str(state_path(SHELLY_HISTORY_FILE))
    now = datetime.now()
    written = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        devices = list(pool.map(read_shelly, shelly_hosts))
    with io.open(path, "a", encoding="utf-8") as handle:
        for device in devices:
            if device.get("error"):
                continue
            for channel in device["channels"]:
                # kwh is the lifetime counter; watts is only useful as a
                # sanity check when reading the file back.
                if channel.get("kwh") is None:
                    continue
                handle.write(json.dumps({
                    "at": now.isoformat(timespec="seconds"),
                    "host": device["host"],
                    "channel": channel["id"],
                    "kwh": channel["kwh"],
                    "watts": channel.get("watts"),
                }, sort_keys=True) + "\n")
                written += 1
    return path, written, len(devices)


def load_shelly_history(path=None) -> list:
    path = path or str(state_path(SHELLY_HISTORY_FILE))
    out = []
    try:
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                row["_t"] = datetime.fromisoformat(row["at"])
                out.append(row)
            except (ValueError, KeyError):
                continue
    except OSError:
        return []
    out.sort(key=lambda r: r["_t"])
    return out


def shelly_usage(history, start, end) -> dict:
    """kWh per channel between two instants, by differencing the counter.

    Returns {(host, channel): kwh}. A counter that goes BACKWARDS means the
    device was reset or replaced, and the correct answer then is "we do not
    know", not a negative number or a huge positive one -- so that channel is
    dropped rather than guessed at.
    """
    per = {}
    for row in history:
        if start <= row["_t"] <= end:
            per.setdefault((row["host"], row["channel"]), []).append(row["kwh"])
    out = {}
    for key, values in per.items():
        if len(values) < 2:
            continue                       # nothing to difference yet
        delta = values[-1] - values[0]
        if delta < 0:
            continue                       # counter reset; unknowable
        out[key] = delta
    return out


def load_labels(path: str) -> dict:
    """Friendly names for devices and channels.

    Needed because the Shelly APP stores the name you set in Shelly Cloud,
    not on the device -- `sys.device.name` reads null over the LAN. It is also
    the only way to name a Pro 3EM's three clamps, which sit on three
    unrelated circuits behind one device name.
    """
    try:
        return json.loads(io.open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return {}


def label_for(device: dict, channel_id: str, labels: dict) -> str:
    """Config wins, then the device's own name, then the model, then the IP."""
    entry = labels.get(device["host"])
    if isinstance(entry, dict):
        named = entry.get(channel_id)
        if named:
            return named
    elif isinstance(entry, str) and entry:
        return entry
    return (device.get("name") or device.get("model") or device["host"])


def read_tariff_soc(day: str | None = None) -> dict:
    """Today's price and SOC, five-minute resolution, from the Sigen cloud.

    This is the series the whole project is about: BUY_TARIFF shows the IOG
    bonus slots as the plant itself sees them, so plotting SOC against it
    answers "did we actually charge when it was cheap" from one source.
    """
    day = day or datetime.now().strftime("%Y%m%d")
    try:
        import sigencloud
        client = sigencloud.client_from_env()
        client.ensure()
        data = client._call(
            "GET",
            f"data-process/sigen/station/statistics/tariff-soc/day"
            f"?stationId={client.station_id}&dt={day}&needPrediction=false",
        ).get("data") or {}
    except Exception as exc:                      # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}

    out = {}
    for series in data.get("dataSeries") or []:
        name = series.get("id")
        if name == "VIRTUAL_BATTERY_SOC":
            continue                               # constant 50 here; noise
        points = [(p.get("time"), p.get("value"))
                  for p in (series.get("points") or [])
                  if p.get("value") is not None]
        if points:
            out[name] = points
    return out


def snapshot(shelly_hosts, log_path, labels=None, history=False) -> dict:
    """Everything, gathered in parallel so the slowest source sets the pace."""
    result = {"at": datetime.now(), "labels": labels or {}}
    with ThreadPoolExecutor(max_workers=10) as pool:
        sigen = pool.submit(read_sigen)
        agent = pool.submit(read_agent, log_path)
        tariff = pool.submit(read_tariff_soc) if history else None
        shellys = [pool.submit(read_shelly, h) for h in shelly_hosts]
        result["sigen"] = sigen.result()
        result["agent"] = agent.result()
        result["shellys"] = [f.result() for f in shellys]
        result["tariff_soc"] = tariff.result() if tariff else {}
    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def kw(value, dp=2):
    if value is None:
        return "&mdash;"
    return f"{value:.{dp}f} kW"


# A CT clamp reads a little either side of zero when nothing is drawing.
# Showing "-1 W" makes an idle circuit look like a fault.
WATT_NOISE_FLOOR = 3.0


def watts(value):
    if value is None:
        return "&mdash;"
    if abs(value) < WATT_NOISE_FLOOR:
        return "0 W"
    return f"{value:.0f} W"


def bar(value, peak, tone) -> str:
    """A proportional bar. Pure CSS, because a chart library is a CDN away
    and this page has to render over an SSH tunnel with nothing installed."""
    if not value or not peak or peak <= 0:
        return '<div class="bar"></div>'
    width = max(1.5, min(100.0, abs(value) / peak * 100.0))
    return (f'<div class="bar"><i style="width:{width:.1f}%" '
            f'class="t-{tone}"></i></div>')


def flow_rows(sigen: dict) -> str:
    """The four quantities that make up the house's energy balance.

    Shown as proportional bars against the largest of them, because the point
    is the RELATIONSHIP -- 11 kW into the battery while the house uses 0.5 --
    and four numbers in a column do not show that.
    """
    if sigen.get("error"):
        return (f'<p class="empty">Sigen cloud unavailable &mdash; '
                f'{escape(sigen["error"])}</p>')
    battery, grid = sigen.get("battery"), sigen.get("grid")
    rows = [
        ("Solar", sigen.get("solar"), "solar",
         "generating" if (sigen.get("solar") or 0) > 0.01 else "dark"),
        ("House", sigen.get("load"), "house", ""),
        ("Battery", abs(battery) if battery is not None else None, "batt",
         "charging" if (battery or 0) > 0.01 else
         ("discharging" if (battery or 0) < -0.01 else "idle")),
        ("Grid", abs(grid) if grid is not None else None, "grid",
         "exporting" if (grid or 0) > 0.01 else
         ("importing" if (grid or 0) < -0.01 else "idle")),
    ]
    if (sigen.get("ev") or 0) > 0.01:
        rows.append(("Car", sigen["ev"], "ev", "charging"))
    if (sigen.get("heat_pump") or 0) > 0.01:
        rows.append(("Heat pump", sigen["heat_pump"], "heat", "running"))

    peak = max([abs(v) for _n, v, _t, _s in rows if v] or [1.0])
    out = []
    for name, value, tone, state in rows:
        out.append(
            f'<div class="flow">'
            f'<div class="fname">{escape(name)}</div>'
            f'<div class="fbar">{bar(value, peak, tone)}</div>'
            f'<div class="fval">{kw(value)}</div>'
            f'<div class="fstate s-{tone}">{escape(state)}</div>'
            f'</div>')
    return "".join(out)


def shelly_rows(shellys, labels=None) -> str:
    labels = labels or {}
    live = []
    for device in shellys:
        if device.get("error"):
            live.append((label_for(device, "", labels), None,
                         f'unreachable ({device["error"]})', None))
            continue
        for channel in device["channels"]:
            name = label_for(device, channel["id"], labels)
            if len(device["channels"]) > 1 and name == label_for(device, "", labels):
                name = f'{name} · {channel["id"]}'
            state = ""
            if channel["kind"] == "switch":
                state = "on" if channel.get("on") else "off"
            live.append((name, channel.get("watts"), state, channel["kind"]))

    if not live:
        return '<p class="empty">none configured</p>'
    peak = max([abs(w) for _n, w, _s, _k in live if w] or [1.0])
    out = []
    for name, value, state, _kind in live:
        cls = "on" if state == "on" else ""
        out.append(
            f'<div class="circuit">'
            f'<div class="cname">{escape(str(name))}</div>'
            f'<div class="cbar">{bar(value, peak, "circuit")}</div>'
            f'<div class="cval">{watts(value)}</div>'
            f'<div class="cstate {cls}">{escape(state)}</div>'
            f'</div>')
    return "".join(out)


def value_now(points):
    """The value for the CURRENT half hour, not the last point in the series.

    BUY_TARIFF and SELL_TARIFF carry all 288 five-minute points for the whole
    day, INCLUDING THE FUTURE. Taking points[-1] therefore always read 23:55,
    which is inside the guaranteed cheap window -- so the price card showed
    "4.49p off-peak" at two in the afternoon. SOC hid the bug because its
    series stops at the present, making its last point genuinely current.

    Returns the latest point at or before now, falling back to the first
    point if the series has not started yet.
    """
    if not points:
        return None
    now = datetime.now()
    best = None
    for label, value in points:
        try:
            when = datetime.strptime(label, "%Y%m%d %H:%M")
        except (ValueError, TypeError):
            continue
        if when <= now:
            best = value
        else:
            break
    return best if best is not None else points[0][1]


def sparkline(points, width=320, height=48, fill=False,
              zero_base=True, fmt=None) -> str:
    """An inline SVG line. No library, no CDN, no build step.

    Pass `fmt` to make it hoverable: each half hour gets a
    transparent rect with an SVG <title>, which browsers render
    as a tooltip natively.
    """
    values = [v for _, v in points if v is not None]
    if len(values) < 2:
        return '<span class="empty">not enough data</span>'
    low = min(min(values), 0.0) if zero_base else min(values)
    high = max(values)
    span = (high - low) or 1.0
    step = width / (len(points) - 1)
    coords = []
    for i, (_, value) in enumerate(points):
        if value is None:
            continue
        x = i * step
        y = height - ((value - low) / span) * height
        coords.append(f"{x:.1f},{y:.1f}")
    path = " ".join(coords)
    area = ""
    if fill and coords:
        area = (f'<polygon points="0,{height} {path} {width},{height}" '
                f'fill="currentColor" opacity="0.14"/>')
    # Hoverable bands, one per half hour, each carrying an SVG <title> --
    # a NATIVE browser tooltip. No JavaScript, no library, nothing to load,
    # so the page still works over an SSH tunnel with nothing installed.
    #
    # Half hours rather than the underlying five-minute points because that
    # is the settlement period, the granularity the tariff actually changes
    # at, and 48 bands instead of 288.
    bands = ""
    if fmt:
        per_band = max(1, len(points) // 48)
        for begin in range(0, len(points), per_band):
            chunk = [pt for pt in points[begin:begin + per_band]
                     if pt[1] is not None]
            if not chunk:
                continue
            label = chunk[0][0]
            clock = label[-5:] if isinstance(label, str) else ""
            bands += (
                f'<rect x="{begin * step:.1f}" y="0" '
                f'width="{max(step * per_band, 1.0):.1f}" height="{height}" '
                f'fill="transparent"><title>{escape(clock)}  '
                f'{escape(fmt(chunk[-1][1]))}</title></rect>')

    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img">'
            f'{area}<polyline points="{path}" fill="none" '
            f'stroke="currentColor" stroke-width="1.7" '
            f'vector-effect="non-scaling-stroke"/>{bands}</svg>')


def tariff_block(tariff: dict) -> str:
    """Price and SOC for today, which is the question this project asks."""
    if not tariff:
        return ""
    if tariff.get("error"):
        return (f'<p class="empty">Tariff history unavailable &mdash; '
                f'{escape(tariff["error"])}</p>')
    rows = []
    def pence(v):
        return f"{v * 100:.2f}p"

    def percent(v):
        return f"{v:.1f}%"

    for key, title, tone, fmt in (
            ("BUY_TARIFF", "Import price", "peak", pence),
            ("SELL_TARIFF", "Export price", "cheap", pence),
            ("SOC", "Battery SOC", "batt", percent)):
        points = tariff.get(key)
        if not points:
            continue
        values = [v for _, v in points]
        current = value_now(points)
        if current is None:
            current = values[-1]
        if key.endswith("TARIFF"):
            lo, hi = f"{min(values) * 100:.2f}p", f"{max(values) * 100:.2f}p"
            now = f"{current * 100:.2f}p"
        else:
            lo, hi = f"{min(values):.0f}%", f"{max(values):.0f}%"
            now = f"{current:.1f}%"
        rows.append(
            f'<div class="sparkrow">'
            f'<div class="sname">{escape(title)}'
            f'<span class="srange">{lo} &ndash; {hi}</span></div>'
            f'<div class="sline g-{tone}">{sparkline(points, fill=True, fmt=fmt)}</div>'
            f'<div class="snow">{now}</div>'
            f'</div>')
    if not rows:
        return '<p class="empty">No tariff series for today yet.</p>'
    return "".join(rows)


def agent_block(agent: dict) -> str:
    if agent.get("error"):
        return (f'<p class="empty">No agent log at '
                f'{escape(agent["log"])} &mdash; {escape(agent["error"])}</p>')
    bits = []
    seen = agent.get("last_seen")
    if seen:
        age = (datetime.now() - seen).total_seconds()
        stale = age > 900
        if stale:
            bits.append(f'<p class="alarm">Last tick '
                        f'{escape(seen.strftime("%H:%M:%S"))} '
                        f'({age / 60:.0f} min ago) &mdash; STALE, is the agent '
                        f'running?</p>')
        else:
            bits.append(f'<p class="meta">Last tick '
                        f'{escape(seen.strftime("%H:%M:%S"))} '
                        f'&middot; {age / 60:.0f} min ago &middot; '
                        f'<strong>{escape(agent.get("action") or "?")}</strong>'
                        f'</p>')
    slots = agent.get("slots") or []
    if slots:
        rows = []
        for slot in reversed(slots):
            end = slot.get("end")
            when = slot["start"].strftime("%d %b %H:%M")
            if end:
                mins = (end - slot["start"]).total_seconds() / 60
                span = (f'{when} &rarr; {end.strftime("%H:%M")}'
                        f'<span class="mins">{mins:.0f} min</span>')
                live = ""
            else:
                span = f'{when} &rarr; <strong>still open</strong>'
                live = " live"
            gain = ""
            if slot.get("soc_start") is not None and slot.get("soc_end") is not None:
                gain = (f'{slot["soc_start"]:.0f}% &rarr; {slot["soc_end"]:.0f}%'
                        f'<span class="delta">'
                        f'+{slot["soc_end"] - slot["soc_start"]:.0f}</span>')
            rows.append(f'<div class="slot{live}"><div>{span}</div>'
                        f'<div class="soc">{gain}</div></div>')
        bits.append("".join(rows))
    else:
        bits.append('<p class="empty">No commanded slots in the log yet.</p>')
    return "".join(bits)


def hero(snap: dict) -> str:
    """The things worth seeing from across the room.

    Import and export price sit together deliberately: the SPREAD between
    them is the decision. Buying at 4.49p to sell at 16.94p is worth doing
    and buying at 29.76p to sell at 16.94p is not, and neither number answers
    that on its own.
    """
    sigen = snap["sigen"]
    tariff = snap.get("tariff_soc") or {}
    cards = []

    buy = (tariff.get("BUY_TARIFF") or [])
    if buy and not tariff.get("error"):
        current = value_now(buy)
        price = (current if current is not None else buy[0][1]) * 100
        cheap = price < 10
        cards.append((f"{price:.2f}p", "import now",
                      "cheap" if cheap else "peak",
                      "off-peak" if cheap else "peak rate"))

    sell = (tariff.get("SELL_TARIFF") or [])
    if sell and not tariff.get("error"):
        current = value_now(sell)
        out_p = (current if current is not None else sell[0][1]) * 100
        # The spread, when both are known: what a kWh through the battery is
        # worth right now, before round-trip losses.
        sub = "export rate"
        if buy and not tariff.get("error"):
            spread = out_p - price
            sub = (f"spread {spread:+.2f}p" if abs(spread) >= 0.005
                   else "spread flat")
        cards.append((f"{out_p:.2f}p", "export now", "cheap", sub))

    soc = sigen.get("soc")
    if soc is not None:
        # Capacity comes from the agent, which reads register 30083 once.
        # Hard-coding this plant's 24.18 would hand a second adopter a
        # confidently wrong kWh figure with nothing to indicate why.
        capacity = ((snap.get("agent") or {}).get("state") or {}).get(
            "capacity_kwh")
        sub = (f"{soc / 100 * capacity:.1f} kWh of {capacity:.1f}"
               if capacity else "state of charge")
        cards.append((f"{soc:.0f}%", "battery", "batt", sub))

    grid = sigen.get("grid")
    if grid is not None:
        cards.append((f"{abs(grid):.1f}", "kW " + ("export" if grid > 0
                                                   else "import"),
                      "grid", "exporting" if grid > 0 else "importing"))

    today = sigen.get("solar_today")
    if today is not None:
        cards.append((f"{today:.2f} kWh", "solar today", "solar",
                      "generated"))

    return "".join(
        f'<div class="card c-{tone}">'
        f'<div class="big">{escape(value)}</div>'
        f'<div class="cap">{escape(caption)}</div>'
        f'<div class="sub">{escape(sub)}</div></div>'
        for value, caption, tone, sub in cards)


def render(snap: dict) -> bytes:
    sigen = snap["sigen"]
    holding = snap["agent"].get("holding")
    banner = ""
    if holding:
        banner = ('<div class="banner">Commanding a cheap slot now &mdash; '
                  'this import is at the off-peak rate</div>')
    tariff = snap.get("tariff_soc") or {}
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SECONDS}">
<meta name="color-scheme" content="dark light">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Energy &middot; {snap['at']:%H:%M}</title>
<style>
:root {{
  --bg:#0e1113; --panel:#161a1d; --line:#252b30; --text:#e7ecef;
  --muted:#8b979e; --solar:#e8b64c; --grid:#5aa8e8; --batt:#a98be0;
  --house:#7f8c94; --cheap:#3fb98c; --peak:#e0705e; --ev:#5fc9c0;
  --heat:#e08a5e; --circuit:#5f8fa8;
}}
@media (prefers-color-scheme: light) {{
  :root {{ --bg:#f6f7f8; --panel:#fff; --line:#e2e6e9; --text:#151a1d;
           --muted:#68757c; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:1.1rem; background:var(--bg); color:var(--text);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui,
  sans-serif; max-width:1080px; }}
header {{ display:flex; justify-content:space-between; align-items:baseline;
  flex-wrap:wrap; gap:.4rem; margin-bottom:1rem; }}
h1 {{ font-size:1.05rem; margin:0; font-weight:650; letter-spacing:-.01em; }}
.when {{ color:var(--muted); font-size:.85rem;
  font-variant-numeric:tabular-nums; }}
.when a {{ color:var(--muted); padding:.35rem 0; display:inline-block; }}
h2 {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.1em;
  color:var(--muted); font-weight:650; margin:0 0 .7rem; }}

.nav-shared {{}}
nav {{ display:flex; flex-wrap:wrap; gap:.4rem; margin:0 0 1rem; }}
nav a {{ padding:.34rem .75rem; border:1px solid var(--line);
  border-radius:7px; text-decoration:none; color:var(--muted);
  font-size:.85rem; background:var(--panel); }}
nav a.on {{ background:var(--cheap); color:#04231a; border-color:var(--cheap);
  font-weight:650; }}
.cards {{ display:grid; gap:.7rem; margin-bottom:1rem;
  grid-template-columns:repeat(auto-fit, minmax(148px, 1fr)); }}
.card {{ background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:.85rem .95rem; position:relative;
  overflow:hidden; }}
.card::before {{ content:""; position:absolute; inset:0 auto 0 0; width:3px;
  background:currentColor; }}
.big {{ font-size:1.85rem; font-weight:680; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; line-height:1.05; color:var(--text); }}
.cap {{ font-size:.78rem; color:var(--muted); margin-top:.15rem; }}
.sub {{ font-size:.72rem; color:currentColor; margin-top:.3rem;
  font-weight:600; opacity:.95; }}
.c-cheap {{ color:var(--cheap); }} .c-peak {{ color:var(--peak); }}
.c-batt {{ color:var(--batt); }} .c-grid {{ color:var(--grid); }}
.c-solar {{ color:var(--solar); }}

.banner {{ background:var(--cheap); color:#04231a; padding:.55rem .85rem;
  border-radius:10px; font-weight:650; font-size:.9rem; margin-bottom:1rem; }}

.panel {{ background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:.95rem 1rem 1.05rem; margin-bottom:.9rem; }}
.cols {{ display:grid; gap:.9rem; grid-template-columns:1fr; }}
@media (min-width:860px) {{ .cols {{ grid-template-columns:1.05fr .95fr; }} }}

.flow, .circuit {{ display:grid; align-items:center; gap:.2rem .6rem;
  grid-template-columns:minmax(9.5rem, auto) 1fr 4.6rem 4.4rem;
  grid-template-areas:"name bar val state"; padding:.32rem 0;
  border-bottom:1px solid var(--line); }}
.fname, .cname {{ grid-area:name; }}
.fbar, .cbar {{ grid-area:bar; min-width:0; }}
.fval, .cval {{ grid-area:val; }}
.fstate, .cstate {{ grid-area:state; }}
.flow:last-child, .circuit:last-child {{ border-bottom:0; }}
.fname, .cname {{ font-size:.9rem; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }}
.fval, .cval {{ text-align:right; font-variant-numeric:tabular-nums;
  font-weight:640; font-size:.9rem; }}
.fstate, .cstate {{ font-size:.72rem; color:var(--muted); text-align:right; }}
.cstate.on {{ color:var(--cheap); font-weight:600; }}
.bar {{ height:7px; border-radius:4px; background:var(--line);
  overflow:hidden; }}
.bar i {{ display:block; height:100%; border-radius:4px; }}
.t-solar {{ background:var(--solar); }} .t-grid {{ background:var(--grid); }}
.t-batt {{ background:var(--batt); }} .t-house {{ background:var(--house); }}
.t-ev {{ background:var(--ev); }} .t-heat {{ background:var(--heat); }}
.t-circuit {{ background:var(--circuit); }}
.s-solar {{ color:var(--solar); }} .s-grid {{ color:var(--grid); }}
.s-batt {{ color:var(--batt); }} .s-ev {{ color:var(--ev); }}
.s-heat {{ color:var(--heat); }}

@media (max-width:560px) {{
  body {{ padding:.8rem; }}
  .flow, .circuit {{ grid-template-columns:1fr 4.4rem 3.6rem;
    grid-template-areas:"name val state" "bar bar bar"; gap:.15rem .5rem;
    padding:.45rem 0; }}
  .fname, .cname {{ white-space:normal; }}
  .big {{ font-size:1.55rem; }}
  .card {{ padding:.7rem .75rem; }}
  .sparkrow {{ grid-template-columns:1fr 3.4rem;
    grid-template-areas:"name now" "line line"; }}
  .sname {{ grid-area:name; }} .sline {{ grid-area:line; }}
  .snow {{ grid-area:now; }}
  .slot {{ flex-direction:column; gap:.1rem; }}
  .soc {{ font-size:.8rem; }}
  header {{ gap:.15rem; }}
  .when {{ font-size:.8rem; }}
}}
.sparkrow {{ display:grid; grid-template-columns:8.5rem 1fr 3.6rem;
  gap:.6rem; align-items:center; padding:.4rem 0;
  border-bottom:1px solid var(--line); }}
.sparkrow:last-child {{ border-bottom:0; }}
.sname {{ font-size:.85rem; }}
.srange {{ display:block; color:var(--muted); font-size:.72rem;
  font-variant-numeric:tabular-nums; }}
.snow {{ text-align:right; font-weight:640; font-size:.88rem;
  font-variant-numeric:tabular-nums; }}
.spark {{ width:100%; height:40px; display:block; }}
.spark rect {{ cursor:crosshair; }}
.g-peak {{ color:var(--peak); }} .g-cheap {{ color:var(--cheap); }}
.g-batt {{ color:var(--batt); }}

.slot {{ display:flex; justify-content:space-between; gap:.6rem;
  padding:.36rem 0; border-bottom:1px solid var(--line); font-size:.85rem;
  font-variant-numeric:tabular-nums; }}
.slot:last-child {{ border-bottom:0; }}
.slot.live {{ color:var(--cheap); font-weight:600; }}
.mins {{ color:var(--muted); margin-left:.45rem; font-weight:400; }}
.soc {{ color:var(--muted); white-space:nowrap; }}
.delta {{ color:var(--cheap); margin-left:.4rem; font-weight:640; }}
.meta {{ color:var(--muted); font-size:.82rem; margin:0 0 .6rem; }}
.alarm {{ color:var(--peak); font-weight:640; font-size:.85rem;
  margin:0 0 .6rem; }}
.empty {{ color:var(--muted); font-size:.85rem; margin:.2rem 0; }}
footer {{ color:var(--muted); font-size:.75rem; line-height:1.5;
  margin-top:.4rem; }}
footer code {{ font-size:.95em; }}
</style></head><body>
<header>
  <h1>8 Sycamore Ave</h1>
  <div class="when">{snap['at']:%a %d %b %H:%M:%S} &middot; read-only</div>
</header>
<nav>
  <a class="on" href="/">Live</a>
  <a href="/report?period=today">Today</a>
  <a href="/report?period=yesterday">Yesterday</a>
  <a href="/report?period=last7">Last 7 days</a>
  <a href="/report?period=month">This month</a>
  <a href="/report?period=lastmonth">Last month</a>
</nav>

<div class="cards">{hero(snap)}</div>
{banner}

<div class="cols">
  <div>
    <div class="panel">
      <h2>Now</h2>
      {flow_rows(sigen)}
    </div>
    <div class="panel">
      <h2>Circuits</h2>
      {shelly_rows(snap['shellys'], snap.get('labels'))}
    </div>
  </div>
  <div>
    {('<div class="panel"><h2>Today</h2>' + tariff_block(tariff) + '</div>')
     if tariff else ''}
    <div class="panel">
      <h2>Agent &amp; cheap slots</h2>
      {agent_block(snap['agent'])}
    </div>
  </div>
</div>

<footer>
Solar is <code>thirdPvPower</code> (the SolarEdge, AC-coupled via the gateway)
plus <code>pvPower</code> (the SigenStor's own MPPTs, 0 on this plant). House
load is measured, not derived. Plant state comes from the agent's log, never
from a second Modbus connection &mdash; two readers on a link with a
1&thinsp;s minimum would starve the thing that commands the battery.
Refreshes every {PAGE_REFRESH_SECONDS}s.
</footer>
</body></html>""".encode("utf-8")


# --------------------------------------------------------------------------
# serving
# --------------------------------------------------------------------------

class _Cache:
    def __init__(self, seconds):
        self.seconds = seconds
        self.at = 0.0
        self.value = None
        self.lock = threading.Lock()

    def get(self, produce):
        with self.lock:
            if self.value is None or (time.time() - self.at) > self.seconds:
                self.value = produce()
                self.at = time.time()
            return self.value


class _KeyedCache:
    """One cached value per period. Longer TTL than the live page: a month
    total does not change minute to minute, and each Zappi day is a call."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.entries = {}
        self.lock = threading.Lock()

    def get(self, key, produce):
        with self.lock:
            at, value = self.entries.get(key, (0.0, None))
            if value is None or (time.time() - at) > self.seconds:
                value = produce()
                self.entries[key] = (time.time(), value)
            return value


def make_handler(shelly_hosts, log_path, cache, labels=None, history=True,
                 em_host=None, report_cache=None):
    report_cache = report_cache or _KeyedCache(REPORT_CACHE_SECONDS)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass                                   # quiet by default

        def do_GET(self):                          # noqa: N802
            path = self.path.split("?")[0]
            if path == "/healthz":
                body = b"ok"
                ctype = "text/plain"
            elif path in ("/", "/index.html"):
                snap = cache.get(lambda: snapshot(shelly_hosts, log_path, labels, history))
                body = render(snap)
                ctype = "text/html; charset=utf-8"
            elif path == "/report":
                import urllib.parse as _up
                query = _up.parse_qs(self.path.partition("?")[2])
                period = (query.get("period") or ["today"])[0]
                if period not in dict((p[0], p) for p in PERIODS):
                    period = "today"
                # Its own cache: a month total does not change minute to
                # minute, and the Zappi is a call per day.
                snap = report_cache.get(
                    period,
                    lambda: report_snapshot(period, shelly_hosts, labels,
                                            em_host))
                body = render_report(snap)
                ctype = "text/html; charset=utf-8"
            elif path == "/snapshot.json":
                snap = cache.get(lambda: snapshot(shelly_hosts, log_path, labels, history))
                body = json.dumps(snap, default=str, indent=1).encode()
                ctype = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def shelly_hosts_from(args) -> list:
    if args.shelly:
        return args.shelly
    try:
        raw = (load_env().get("IOG_SHELLY_HOSTS") or "").strip()
    except ConfigError:
        raw = ""
    raw = raw or os.environ.get("IOG_SHELLY_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local energy dashboard. Read-only.")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--once", action="store_true",
                        help="print one snapshot and exit")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="default is loopback; it has no auth, so think "
                             "before binding it to the LAN")
    parser.add_argument("--shelly", action="append",
                        help="Shelly host or IP; repeatable. Falls back to "
                             "IOG_SHELLY_HOSTS in .env")
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--labels", default=".shelly-labels.json",
                        help="JSON of friendly names; the Shelly app keeps "
                             "names in Shelly Cloud, not on the device, so a "
                             "LAN-only dashboard cannot read them")
    parser.add_argument("--record", action="store_true",
                        help="append one reading per channel and exit. Put "
                             "this on a timer: a Shelly plug keeps only a "
                             "lifetime counter, so per-period consumption "
                             "has to be differenced, and an hour not "
                             "recorded cannot be recovered")
    parser.add_argument("--no-history", action="store_true",
                        help="skip the tariff/SOC series (one extra cloud "
                             "call per refresh)")
    args = parser.parse_args()

    hosts = shelly_hosts_from(args)
    labels = load_labels(args.labels)
    if args.record:
        path, written, seen = record_shellys(hosts)
        print(f"recorded {written} channel reading(s) from {seen} device(s) "
              f"-> {path}")
        return 0
    history = not args.no_history
    if args.once:
        snap = snapshot(hosts, args.log, labels, history)
        print(json.dumps(snap, default=str, indent=1))
        return 0

    if not args.serve:
        parser.print_help()
        return 2

    cache = _Cache(CACHE_SECONDS)
    em_host = next((h for h, e in (labels or {}).items()
                    if isinstance(e, dict)
                    and any(k.startswith("em1:") for k in e)), None)
    handler = make_handler(hosts, args.log, cache, labels, history, em_host)
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"dashboard on http://{args.bind}:{args.port}  "
          f"({len(hosts)} Shelly host(s), {len(labels)} label(s), "
          f"log {args.log})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0




# --------------------------------------------------------------------------
# reporting: consumption per period
# --------------------------------------------------------------------------

# Which periods the page offers, in the order they appear. "kind" decides how
# the Sigen totals are fetched: dateFlag=1 is a single DAY and returns zeros
# for a range, dateFlag=2 is a whole MONTH in one call. Asking for a month
# day-by-day would be 30 calls for a number the API already aggregates.
PERIODS = (
    ("today", "Today", "day"),
    ("yesterday", "Yesterday", "day"),
    ("last7", "Last 7 days", "days"),
    ("month", "This month", "month"),
    ("lastmonth", "Last month", "month"),
)


def period_range(name: str, today=None) -> tuple:
    """(start date, end date inclusive, label, kind). Defaults to today."""
    today = today or datetime.now().date()
    label = dict((p[0], p[1]) for p in PERIODS).get(name, "Today")
    kind = dict((p[0], p[2]) for p in PERIODS).get(name, "day")
    if name == "yesterday":
        day = today - timedelta(days=1)
        return day, day, label, kind
    if name == "last7":
        return today - timedelta(days=6), today, label, kind
    if name == "month":
        return today.replace(day=1), today, label, kind
    if name == "lastmonth":
        last_day = today.replace(day=1) - timedelta(days=1)
        return last_day.replace(day=1), last_day, label, kind
    return today, today, label, kind


def report_sigen(start, end, kind) -> dict:
    """Energy totals for the period. One call for a day or a month."""
    try:
        import sigencloud
        client = sigencloud.client_from_env()
        client.ensure()

        def fetch(s, e, flag):
            data = client._call(
                "GET",
                f"data-process/sigen/station/statistics/v1/energy/custom"
                f"?stationId={client.station_id}&startDate={s:%Y%m%d}"
                f"&endDate={e:%Y%m%d}&dateFlag={flag}"
                f"&resourceIds=energy_card").get("data") or {}
            return {c.get("cardKey"): c.get("value")
                    for c in (data.get("stationCards") or [])}

        if kind == "month":
            return fetch(start, end, 2)
        if kind == "day":
            return fetch(start, end, 1)
        # A span of days: the API returns zeros for a ranged dateFlag=1, so
        # sum the days. Seven calls, cached; still far cheaper than walking a
        # month when dateFlag=2 exists.
        total = {}
        day = start
        while day <= end:
            for key, value in fetch(day, day, 1).items():
                if isinstance(value, (int, float)):
                    total[key] = total.get(key, 0.0) + value
            day += timedelta(days=1)
        return total
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}


def report_zappi(start, end) -> dict:
    """Car charging for the period, in kWh.

    myenergi reports JOULES PER MINUTE in h1b/h1d/h2b/... where the suffix is
    b for boost (grid) and d for diverted (surplus solar). Sum only fields
    that match that shape: `hr` and `min` are the timestamp and summing by
    prefix silently counted the hour as energy.
    """
    try:
        import zappi as zappi_mod
        client = zappi_mod.client_from_env()

        days = []
        day = start
        while day <= end:
            days.append(day)
            day += timedelta(days=1)

        def one(d):
            """One day's joules. Never raises: a month must not fail on a
            single bad day, and myenergi returns nothing for a date before
            the charger was commissioned."""
            try:
                raw = client._get(f"/cgi-jday-Z{client.serial}-"
                                  f"{d.year}-{d.month}-{d.day}")
                rows = raw[next(iter(raw))] if raw else []
            except Exception:                          # noqa: BLE001
                return 0.0, 0.0, False
            grid = solar = 0.0
            for row in rows:
                for key, value in row.items():
                    # h<phase><b|d>: b is boost (grid), d is diverted solar.
                    # Matching on the prefix alone counted `hr`, the HOUR.
                    if len(key) == 3 and key[0] == "h" and key[1].isdigit():
                        if key[2] == "b":
                            grid += value
                        elif key[2] == "d":
                            solar += value
            return grid, solar, True

        # A day at a time is how this API works, so a month is 30 round
        # trips. Sequentially that took long enough to time the page out;
        # concurrently it is one round trip's worth of waiting.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, days))

        grid = sum(r[0] for r in results)
        solar = sum(r[1] for r in results)
        return {"grid": grid / 3600000.0, "solar": solar / 3600000.0,
                "total": (grid + solar) / 3600000.0,
                "days": sum(1 for r in results if r[2]),
                "days_asked": len(days)}
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}


def report_costs(start, end, log_path) -> dict:
    """Money, from costs.py. Never raises.

    Meter readings lag roughly 22 hours, so "today" will be empty or partial
    and that is the meter's doing. Reported as a count of half-hours so the
    page can say how complete it is rather than presenting a third of a day
    as if it were the whole thing.
    """
    try:
        import costs
        return costs.report(start, end, log_path)
    except Exception as exc:                          # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:120]}


def report_daikin(start, end, kind="day") -> dict:
    """Heat pump kWh, from whichever archive daikin.py keeps for that span.

    A WHOLE MONTH must come from the monthly archive, not by summing days.
    The daily archive only began on 2026-09-13 and reaches back a fortnight,
    so asking it for August returned 31 August alone and reported one day as
    a month -- 2 kWh where the real figure was 67. The monthly file has run
    since January 2025 and is authoritative for any complete month.
    """
    out = {"climateControl": 0.0, "domesticHotWaterTank": 0.0, "days": 0}

    if kind == "month" and start.day == 1:
        try:
            monthly = json.loads(io.open(
                state_path(".daikin-consumption.json"), encoding="utf-8").read())
        except (OSError, ValueError):
            monthly = {}
        entry = monthly.get(f"{start.year}-{start.month:02d}")
        if entry:
            for field in ("climateControl", "domesticHotWaterTank"):
                value = entry.get(field)
                if isinstance(value, (int, float)):
                    out[field] = float(value)
            out["days"] = 1
            out["source"] = "monthly archive"
            return out
        # No monthly figure yet: fall through and sum what days we have,
        # which is right for a month still in progress.

    try:
        store = json.loads(io.open(
            state_path(".daikin-daily.json"), encoding="utf-8").read())
    except (OSError, ValueError):
        return {"error": "no daily archive yet"}
    day = start
    while day <= end:
        entry = store.get(day.isoformat())
        if entry:
            out["days"] += 1
            for kind in ("climateControl", "domesticHotWaterTank"):
                value = entry.get(kind)
                if isinstance(value, (int, float)):
                    out[kind] += value
        day += timedelta(days=1)
    return out


def report_snapshot(period, shelly_hosts, labels, em_host=None) -> dict:
    start, end, label, kind = period_range(period)
    out = {"period": period, "label": label, "start": start, "end": end,
           "kind": kind, "labels": labels or {}}
    with ThreadPoolExecutor(max_workers=6) as pool:
        sigen = pool.submit(report_sigen, start, end, kind)
        zap = pool.submit(report_zappi, start, end)
        daikin_f = pool.submit(report_daikin, start, end, kind)
        money = pool.submit(report_costs, start, end, "observe.log")

        out["sigen"] = sigen.result()
        out["zappi"] = zap.result()
        out["daikin"] = daikin_f.result()
        out["costs"] = money.result()
        out["em"] = {}
    history = load_shelly_history()
    begin = datetime.combine(start, datetime.min.time())
    finish = datetime.combine(end + timedelta(days=1), datetime.min.time())
    out["plugs"] = shelly_usage(history, begin, finish)
    out["recording_since"] = history[0]["_t"] if history else None
    return out


def kwh(value, dp=2):
    if value is None:
        return "&mdash;"
    return f"{value:.{dp}f}"


def money_block(c: dict, expected_half_hours: int) -> str:
    """Pounds. The only unit that answers "was any of this worth doing"."""
    if c.get("error"):
        return (f'<p class="empty">Costs unavailable &mdash; '
                f'{escape(c["error"])}</p>')
    got = c.get("half_hours") or 0
    pct_settled = (100.0 * got / expected_half_hours
                   if expected_half_hours else 0.0)

    # Below this, the figures cover so little of the period that showing them
    # as a headline invites a comparison they cannot survive -- the owner
    # reasonably read "+GBP 0.68" against 40 kWh of plant-measured export and
    # concluded revenue was low, when in fact a tenth of the day had settled.
    if pct_settled < 25:
        rows = "".join(
            f'<div class="mrow"><div class="label">{name}</div>'
            f'<div class="value">{kwh}</div>'
            f'<div class="value">{amount}</div></div>'
            for name, kwh, amount in (
                ("Imported so far", f"{c['import_kwh']:.2f} kWh",
                 f"&minus;&pound;{c['import_cost']:.2f}"),
                ("Exported so far", f"{c['export_kwh']:.2f} kWh",
                 f"+&pound;{c['export_income']:.2f}")))
        return (f'<div class="money"><div class="mbig settling">Settling'
                f'</div><div class="cap">only {pct_settled:.0f}% of this '
                f'period has reached the meter yet</div></div>{rows}'
                f'<p class="empty">Octopus publishes meter readings about a '
                f'day late. Totals here will keep rising; compare the Plant '
                f'figures below for what actually happened.</p>')

    note = ""
    settled = ""
    if got < expected_half_hours * 0.9:
        # Not an error: Octopus publishes meter readings about 22 hours late.
        pct = 100.0 * got / expected_half_hours if expected_half_hours else 0
        settled = (f'<span class="settled">{pct:.0f}% settled</span>')
        note = ('<p class="empty">Octopus publishes meter readings about a '
                'day late, so recent periods fill in afterwards.</p>')

    net = c.get("net", 0.0)
    # Negative net means income exceeded cost.
    days = max(1, expected_half_hours // 48)
    per_day = abs(net) / days
    compare = (f'{"+" if net < 0 else "&minus;"}&pound;{per_day:.2f} a day'
               if days > 1 else "")
    headline = (f'<div class="money {"good" if net < 0 else "bad"}">'
                f'<div class="mbig">{"+" if net < 0 else "&minus;"}'
                f'&pound;{abs(net):.2f}{settled}</div>'
                f'<div class="cap">{"net income" if net < 0 else "net cost"}'
                f'{" &middot; " + compare if compare else ""}</div></div>')

    rows = [
        ("Imported", f"{c['import_kwh']:.2f} kWh",
         f"&minus;&pound;{c['import_cost']:.2f}", "bad"),
        ("&nbsp;&nbsp;at off-peak", f"{c['cheap_kwh']:.2f} kWh",
         f"@ {c['off_peak_p']:.2f}p", ""),
        ("&nbsp;&nbsp;at peak", f"{c['peak_kwh']:.2f} kWh",
         f"@ {c['peak_p']:.3f}p", ""),
        ("Exported", f"{c['export_kwh']:.2f} kWh",
         f"+&pound;{c['export_income']:.2f}", "good"),
    ]
    if c.get("export_unpriced_kwh"):
        rows.append(("&nbsp;&nbsp;unpriced",
                     f"{c['export_unpriced_kwh']:.2f} kWh",
                     "no rate published", ""))
    table = "".join(
        f'<div class="mrow"><div class="label">{name}</div>'
        f'<div class="value">{kwh}</div>'
        f'<div class="value {tone}">{amount}</div></div>'
        for name, kwh, amount, tone in rows)

    extra = ""
    if c.get("vs_all_peak"):
        extra = (f'<p class="empty">Off-peak import would have cost '
                 f'&pound;{c["vs_all_peak"]:.2f} more at the peak rate '
                 f'(upper bound &mdash; without a battery the house would '
                 f'not have imported the same kWh).</p>')
    return headline + table + extra + (
        '<p class="empty">Unit rates only; standing charges are not '
        'included, so this will not reconcile with a bill on its own.</p>'
        + note)


def render_report(rep: dict) -> bytes:
    tabs = '<a href="/">Live</a> ' + " ".join(
        f'<a class="{"on" if key == rep["period"] else ""}" '
        f'href="/report?period={key}">{escape(name)}</a>'
        for key, name, _kind in PERIODS)

    s = rep["sigen"]
    if s.get("error"):
        sigen_rows = (f'<tr><td colspan="2" class="muted">unavailable '
                      f'&mdash; {escape(s["error"])}</td></tr>')
    else:
        sigen_rows = "".join(
            f'<tr><td class="label">{escape(title)}</td>'
            f'<td class="value">{kwh(s.get(key))} kWh</td></tr>'
            for key, title in (("FROM_SOLAR", "Solar generated"),
                               ("TO_LOAD", "House consumed"),
                               ("FROM_GRID", "Imported (plant CT)"),
                               ("TO_GRID", "Exported (plant CT)"),
                               ("TO_BATTERY", "Into battery"),
                               ("FROM_BATTERY", "Out of battery"))
            if s.get(key) is not None)

    z = rep["zappi"]
    if z.get("error"):
        zap = (f'<tr><td class="label">Car (Zappi)</td>'
               f'<td class="value muted">{escape(z["error"])}</td></tr>')
    else:
        zap = (f'<tr><td class="label">Car (Zappi)<br>'
               f'<span class="note">{kwh(z.get("grid"))} from grid, '
               f'{kwh(z.get("solar"))} diverted solar</span></td>'
               f'<td class="value">{kwh(z.get("total"))} kWh</td></tr>')

    d = rep["daikin"]
    if d.get("error"):
        heat = (f'<tr><td class="label">Heat pump</td>'
                f'<td class="value muted">{escape(d["error"])}</td></tr>')
    else:
        heat = (f'<tr><td class="label">Heat pump &mdash; space heating</td>'
                f'<td class="value">{kwh(d.get("climateControl"))} kWh</td></tr>'
                f'<tr><td class="label">Heat pump &mdash; hot water</td>'
                f'<td class="value">{kwh(d.get("domesticHotWaterTank"))} kWh'
                f'</td></tr>')
        if d.get("source") != "monthly archive" and rep["kind"] == "month":
            heat += (f'<tr><td colspan="2" class="muted">summed from '
                     f'{d.get("days", 0)} archived day(s) &mdash; the month is '
                     f'not complete in the monthly archive yet</td></tr>')
        if d.get("days", 0) == 0:
            heat += ('<tr><td colspan="2" class="muted">no daily figures '
                     'archived for this period yet</td></tr>')

    circuits = []
    for channel, value in sorted(rep.get("em", {}).items()):
        name = channel
        for host, entry in (rep["labels"] or {}).items():
            if isinstance(entry, dict) and channel in entry:
                name = entry[channel]
        circuits.append(f'<tr><td class="label">{escape(name)}</td>'
                        f'<td class="value">{kwh(value)} kWh</td></tr>')
    for (host, channel), value in sorted(rep.get("plugs", {}).items()):
        entry = (rep["labels"] or {}).get(host)
        name = entry if isinstance(entry, str) else (
            entry.get(channel, f"{host} {channel}")
            if isinstance(entry, dict) else f"{host} {channel}")
        circuits.append(f'<tr><td class="label">{escape(str(name))}</td>'
                        f'<td class="value">{kwh(value)} kWh</td></tr>')
    since = rep.get("recording_since")
    if not rep.get("plugs"):
        note = ("plug history starts when the recorder does"
                if not since else
                f"recording since {since:%d %b %H:%M}; not enough yet "
                f"for this period")
        circuits.append(f'<tr><td colspan="2" class="muted">{escape(note)}'
                        f'</td></tr>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Energy &middot; {escape(rep['label'])}</title>
<style>
:root {{ color-scheme: light dark; --line:#8883; --muted:#8886; }}
body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; margin:0;
        padding:1.2rem; max-width:780px; }}
h1 {{ font-size:1.1rem; margin:0 0 .1rem; font-weight:600; }}
h2 {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.09em;
      margin:1.6rem 0 .4rem; color:var(--muted); font-weight:600; }}
nav {{ margin:.9rem 0 .2rem; display:flex; flex-wrap:wrap; gap:.4rem; }}
nav a {{ padding:.28rem .7rem; border:1px solid var(--line); border-radius:5px;
         text-decoration:none; color:inherit; font-size:.9rem; }}
nav a.on {{ background:#4a9; color:#fff; border-color:#4a9; font-weight:600; }}
table {{ border-collapse:collapse; width:100%; }}
td {{ padding:.34rem 0; border-bottom:1px solid var(--line); }}
.label {{ width:62%; }}
.money {{ margin:.2rem 0 .9rem; }}
.mbig.settling {{ color:var(--muted); font-size:1.5rem; }}
.settled {{ font-size:.72rem; font-weight:600; color:var(--muted);
  margin-left:.55rem; vertical-align:middle; }}
.mbig {{ font-size:2rem; font-weight:680; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; line-height:1.05; }}
.money.good .mbig {{ color:#1e6b34; }}
.money.bad .mbig {{ color:#b3261e; }}
.mrow {{ display:grid; grid-template-columns:1fr 6.5rem 6.5rem; gap:.5rem;
  padding:.3rem 0; border-bottom:1px solid var(--line); }}
.mrow .good {{ color:#1e6b34; }} .mrow .bad {{ color:#b3261e; }}
@media (max-width:560px) {{
  .mrow {{ grid-template-columns:1fr 5.4rem 5.4rem; font-size:.9rem; }}
}}
.value {{ text-align:right; font-variant-numeric:tabular-nums;
          font-weight:600; }}
.note, .muted {{ color:var(--muted); font-size:.88rem; font-weight:400; }}
footer {{ margin-top:2rem; color:var(--muted); font-size:.82rem; }}
</style></head><body>
<h1>Energy report &mdash; {escape(rep['label'])}</h1>
<p class="muted">{rep['start']:%a %d %b} &ndash; {rep['end']:%a %d %b} &middot;
<a href="/" style="color:inherit">live view</a></p>
<nav>{tabs}</nav>

<h2>Money</h2>
{money_block(rep.get("costs") or {}, max(1, (rep["end"] - rep["start"]).days * 48 + 48))}

<h2>Plant</h2>
<p class="muted">Measured by the SigenStor's own CT, over the whole period.
Money above uses Octopus's settlement meter and only settled half-hours, so
the two import and export figures are different instruments answering
different questions and will not match.</p>
<table>{sigen_rows}</table>

<h2>Devices</h2>
<table>{zap}{heat}</table>

<h2>Circuits</h2>
<table>{"".join(circuits)}</table>

<footer>
Plant totals come from the Sigen cloud &mdash; one call for a day or a whole
month. Circuits metered by the Pro 3EM come from the device's own two months
of minute data. The plugs keep only a lifetime counter, so their figures are
differenced from recorded readings and only cover the period since recording
began. Heat pump figures come from the daily buckets Daikin keeps for a
rolling fortnight, archived locally so they outlive it.
</footer>
</body></html>""".encode("utf-8")

if __name__ == "__main__":
    raise SystemExit(main())
