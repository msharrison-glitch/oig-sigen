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

# A Shelly on wifi power-save can take a second or two to wake. Short enough
# that one asleep device cannot stall the page, long enough not to miss it.
SHELLY_TIMEOUT = 4.0

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
        with urllib.request.urlopen(url, timeout=SHELLY_TIMEOUT) as response:
            return json.loads(response.read())

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


def snapshot(shelly_hosts, log_path) -> dict:
    """Everything, gathered in parallel so the slowest source sets the pace."""
    result = {"at": datetime.now()}
    with ThreadPoolExecutor(max_workers=8) as pool:
        sigen = pool.submit(read_sigen)
        agent = pool.submit(read_agent, log_path)
        shellys = [pool.submit(read_shelly, h) for h in shelly_hosts]
        result["sigen"] = sigen.result()
        result["agent"] = agent.result()
        result["shellys"] = [f.result() for f in shellys]
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


def shelly_rows(shellys) -> str:
    out = []
    for device in shellys:
        title = device.get("name") or device.get("model") or device["host"]
        if device.get("error"):
            out.append(f'<tr><td class="label">{escape(str(title))}</td>'
                       f'<td class="value">&mdash;</td>'
                       f'<td class="note">unreachable '
                       f'({escape(device["error"])})</td></tr>')
            continue
        for channel in device["channels"]:
            name = title
            if len(device["channels"]) > 1:
                name = f"{title} &middot; {escape(channel['id'])}"
            else:
                name = escape(str(name))
            note = ""
            if channel["kind"] == "switch":
                note = "on" if channel.get("on") else "off"
            out.append(f'<tr><td class="label">{name}</td>'
                       f'<td class="value">{watts(channel.get("watts"))}</td>'
                       f'<td class="note">{note}</td></tr>')
    return "".join(out) or '<tr><td colspan="3" class="muted">none configured</td></tr>'


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
<table>{shelly_rows(snap['shellys'])}</table>
</div>
<div>
<h2>Agent &amp; cheap slots</h2>
{agent_block(snap['agent'])}
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


def make_handler(shelly_hosts, log_path, cache):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass                                   # quiet by default

        def do_GET(self):                          # noqa: N802
            path = self.path.split("?")[0]
            if path == "/healthz":
                body = b"ok"
                ctype = "text/plain"
            elif path in ("/", "/index.html"):
                snap = cache.get(lambda: snapshot(shelly_hosts, log_path))
                body = render(snap)
                ctype = "text/html; charset=utf-8"
            elif path == "/snapshot.json":
                snap = cache.get(lambda: snapshot(shelly_hosts, log_path))
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
    args = parser.parse_args()

    hosts = shelly_hosts_from(args)
    if args.once:
        snap = snapshot(hosts, args.log)
        print(json.dumps(snap, default=str, indent=1))
        return 0

    if not args.serve:
        parser.print_help()
        return 2

    cache = _Cache(CACHE_SECONDS)
    handler = make_handler(hosts, args.log, cache)
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"dashboard on http://{args.bind}:{args.port}  "
          f"({len(hosts)} Shelly host(s), log {args.log})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
