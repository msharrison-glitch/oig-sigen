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
from config import load_env, ConfigError

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

DEFAULT_LOG = "observe.log"


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
                        "kwh": (node.get("aenergy") or {}).get("total"),
                    })
                elif key.startswith("em1:"):
                    out["channels"].append({
                        "id": key, "kind": "meter",
                        "watts": node.get("act_power"),
                        "volts": node.get("voltage"),
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


def read_agent(log_path: str) -> dict:
    """What the agent last saw and did. Parses, never polls."""
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


def watts(value):
    if value is None:
        return "&mdash;"
    return f"{value:.0f} W"


def flow_rows(sigen: dict) -> str:
    """The energy balance, as rows rather than a diagram.

    A Sankey would be prettier and would need a library, a CDN and a build
    step -- none of which this project has. Rows are legible at 2am.
    """
    if sigen.get("error"):
        return (f'<tr><td colspan="3" class="muted">Sigen cloud unavailable '
                f'&mdash; {escape(sigen["error"])}</td></tr>')
    battery = sigen.get("battery")
    grid = sigen.get("grid")
    rows = [
        ("Solar", kw(sigen.get("solar")), "in" if (sigen.get("solar") or 0) > 0.01 else ""),
        ("House", kw(sigen.get("load")), "out"),
        ("Battery",
         kw(abs(battery) if battery is not None else None),
         "charging" if (battery or 0) > 0.01 else
         ("discharging" if (battery or 0) < -0.01 else "idle")),
        ("Grid",
         kw(abs(grid) if grid is not None else None),
         "exporting" if (grid or 0) > 0.01 else
         ("importing" if (grid or 0) < -0.01 else "idle")),
    ]
    if (sigen.get("ev") or 0) > 0.01:
        rows.append(("EV", kw(sigen["ev"]), "charging"))
    if (sigen.get("heat_pump") or 0) > 0.01:
        rows.append(("Heat pump", kw(sigen["heat_pump"]), "running"))
    return "".join(
        f'<tr><td class="label">{escape(name)}</td>'
        f'<td class="value">{value}</td>'
        f'<td class="note">{escape(note)}</td></tr>'
        for name, value, note in rows
    )


def shelly_rows(shellys, labels=None) -> str:
    labels = labels or {}
    out = []
    for device in shellys:
        if device.get("error"):
            fallback = label_for(device, "", labels)
            out.append(f'<tr><td class="label">{escape(str(fallback))}</td>'
                       f'<td class="value">&mdash;</td>'
                       f'<td class="note">unreachable '
                       f'({escape(device["error"])})</td></tr>')
            continue
        for channel in device["channels"]:
            name = label_for(device, channel["id"], labels)
            # Only fall back to showing the raw channel id when a multi-channel
            # device has no per-channel label -- otherwise "Fridge" is enough
            # and "Fridge . switch:0" is just noise.
            if len(device["channels"]) > 1 and name == label_for(device, "", labels):
                name = f'{name} &middot; {escape(channel["id"])}'
            else:
                name = escape(str(name))
            note = ""
            if channel["kind"] == "switch":
                note = "on" if channel.get("on") else "off"
            out.append(f'<tr><td class="label">{name}</td>'
                       f'<td class="value">{watts(channel.get("watts"))}</td>'
                       f'<td class="note">{note}</td></tr>')
    return "".join(out) or '<tr><td colspan="3" class="muted">none configured</td></tr>'


def sparkline(points, width=300, height=44, fill=False, zero_base=True) -> str:
    """An inline SVG line. No library, no CDN, no build step.

    `points` is a list of (label, value). Labels are only used for the title;
    the x axis is index, which is right for evenly-sampled series and close
    enough for these.
    """
    values = [v for _, v in points if v is not None]
    if len(values) < 2:
        return '<span class="muted">not enough data</span>'
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
                f'fill="currentColor" opacity="0.13"/>')
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img">'
            f'{area}<polyline points="{path}" fill="none" '
            f'stroke="currentColor" stroke-width="1.6" '
            f'vector-effect="non-scaling-stroke"/></svg>')


def tariff_block(tariff: dict) -> str:
    """Price and SOC for today, which is the question this project asks."""
    if not tariff:
        return ""
    if tariff.get("error"):
        return (f'<p class="muted">Tariff history unavailable &mdash; '
                f'{escape(tariff["error"])}</p>')
    rows = []
    for key, title, note in (
        ("BUY_TARIFF", "Import price",
         "dips are the cheap window and any IOG bonus slots"),
        ("SELL_TARIFF", "Export price", ""),
        ("SOC", "Battery SOC", ""),
    ):
        points = tariff.get(key)
        if not points:
            continue
        values = [v for _, v in points]
        if key.endswith("TARIFF"):
            lo, hi = f"{min(values) * 100:.2f}p", f"{max(values) * 100:.2f}p"
            now = f"{values[-1] * 100:.2f}p"
        else:
            lo, hi = f"{min(values):.0f}%", f"{max(values):.0f}%"
            now = f"{values[-1]:.1f}%"
        rows.append(
            f'<tr><td class="label">{escape(title)}<br>'
            f'<span class="note">{escape(note)}</span></td>'
            f'<td class="sparkcell">{sparkline(points, fill=True)}</td>'
            f'<td class="note">{lo} &ndash; {hi}<br>now {now}</td></tr>')
    if not rows:
        return '<p class="muted">No tariff series for today yet.</p>'
    return f'<table class="sparks">{"".join(rows)}</table>'


def agent_block(agent: dict) -> str:
    if agent.get("error"):
        return (f'<p class="muted">No agent log at '
                f'{escape(agent["log"])} &mdash; {escape(agent["error"])}</p>')
    bits = []
    seen = agent.get("last_seen")
    if seen:
        age = (datetime.now() - seen).total_seconds()
        stale = age > 900
        bits.append(f'<p class="{"warn" if stale else ""}">Last tick '
                    f'{escape(seen.strftime("%H:%M:%S"))} '
                    f'({age / 60:.0f} min ago)'
                    f'{" &mdash; STALE, is the agent running?" if stale else ""}'
                    f'</p>')
    if agent.get("action"):
        bits.append(f'<p>Agent: <strong>{escape(agent["action"])}</strong></p>')
    slots = agent.get("slots") or []
    if slots:
        rows = []
        for slot in reversed(slots):
            end = slot.get("end")
            when = slot["start"].strftime("%d %b %H:%M")
            if end:
                mins = (end - slot["start"]).total_seconds() / 60
                span = f'{when} &rarr; {end.strftime("%H:%M")} ({mins:.0f} min)'
            else:
                span = f'{when} &rarr; <strong>still open</strong>'
            soc = ""
            if slot.get("soc_start") is not None and slot.get("soc_end") is not None:
                soc = f'SOC {slot["soc_start"]:.1f}% &rarr; {slot["soc_end"]:.1f}%'
            rows.append(f'<tr><td class="label">{span}</td>'
                        f'<td class="note" colspan="2">{soc}</td></tr>')
        bits.append('<table>' + "".join(rows) + '</table>')
    else:
        bits.append('<p class="muted">No commanded slots in the log yet.</p>')
    return "".join(bits)


def render(snap: dict) -> bytes:
    sigen = snap["sigen"]
    solar_today = sigen.get("solar_today")
    holding = snap["agent"].get("holding")
    banner = ""
    if holding:
        banner = ('<div class="banner">Agent is commanding a cheap slot right '
                  'now &mdash; this import is at the off-peak rate.</div>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SECONDS}">
<title>Energy &middot; {snap['at']:%H:%M}</title>
<style>
:root {{ color-scheme: light dark; --line:#8883; --muted:#8886; }}
body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; margin:0;
        padding:1.2rem; max-width:980px; }}
h1 {{ font-size:1.1rem; margin:0 0 .2rem; font-weight:600; }}
h2 {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.09em;
      margin:1.6rem 0 .4rem; color:var(--muted); font-weight:600; }}
table {{ border-collapse:collapse; width:100%; }}
td {{ padding:.34rem 0; border-bottom:1px solid var(--line);
      vertical-align:baseline; }}
.label {{ width:45%; }}
.value {{ width:25%; text-align:right; font-variant-numeric:tabular-nums;
          font-weight:600; }}
.note {{ padding-left:1rem; color:var(--muted); font-size:.88rem; }}
.muted {{ color:var(--muted); }}
.warn {{ color:#b3261e; font-weight:600; }}
.banner {{ background:#1e6b34; color:#fff; padding:.55rem .8rem;
           border-radius:6px; margin:.8rem 0; font-weight:600; }}
.spark {{ width:100%; height:44px; display:block; color:#4a9; }}
.sparks .label {{ width:34%; }}
.sparkcell {{ width:45%; padding:.4rem .8rem; }}
.grid {{ display:grid; gap:0 2.4rem; grid-template-columns:1fr; }}
@media (min-width:760px) {{ .grid {{ grid-template-columns:1fr 1fr; }} }}
footer {{ margin-top:2rem; color:var(--muted); font-size:.82rem; }}
</style></head><body>
<h1>8 Sycamore Ave &mdash; {snap['at']:%a %d %b %H:%M:%S}</h1>
<p class="muted">Read-only. This page cannot switch anything.</p>
{banner}
<div class="grid">
<div>
<h2>Now</h2>
<table>{flow_rows(sigen)}</table>
{'<p class="muted">Solar today: <strong>' + f"{solar_today:.2f}" + ' kWh</strong></p>' if solar_today is not None else ''}
{'<p class="muted">Battery SOC: <strong>' + f"{sigen['soc']:.1f}" + '%</strong></p>' if sigen.get('soc') is not None else ''}

<h2>Circuits</h2>
<table>{shelly_rows(snap['shellys'], snap.get('labels'))}</table>
</div>
<div>
<h2>Agent &amp; cheap slots</h2>
{agent_block(snap['agent'])}
{('<h2>Today</h2>' + tariff_block(snap.get('tariff_soc') or {})) if snap.get('tariff_soc') else ''}
</div>
</div>
<footer>
Solar is <code>thirdPvPower</code> (the SolarEdge, AC-coupled via the gateway)
plus <code>pvPower</code> (the SigenStor's own MPPTs, 0 on this plant).
House load is measured, not derived. Plant state comes from the agent's log,
never from a second Modbus connection &mdash; two readers on a link with a
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


def make_handler(shelly_hosts, log_path, cache, labels=None, history=True):
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
    parser.add_argument("--no-history", action="store_true",
                        help="skip the tariff/SOC series (one extra cloud "
                             "call per refresh)")
    args = parser.parse_args()

    hosts = shelly_hosts_from(args)
    labels = load_labels(args.labels)
    history = not args.no_history
    if args.once:
        snap = snapshot(hosts, args.log, labels, history)
        print(json.dumps(snap, default=str, indent=1))
        return 0

    if not args.serve:
        parser.print_help()
        return 2

    cache = _Cache(CACHE_SECONDS)
    handler = make_handler(hosts, args.log, cache, labels, history)
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"dashboard on http://{args.bind}:{args.port}  "
          f"({len(hosts)} Shelly host(s), {len(labels)} label(s), "
          f"log {args.log})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
