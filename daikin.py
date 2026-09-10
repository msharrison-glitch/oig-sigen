#!/usr/bin/env python3
"""
Daikin Onecta cloud API, READ-ONLY.

Why this exists: an IOG bonus slot prices the whole property at the off-peak
rate, and the heat pump is the largest deferrable load in the house after the
car. It cannot see those slots any more than the SigenStor can. Nudging it
during a slot puts heat into the fabric cheaply instead of drawing it from a
battery we would rather export.

This module deliberately contains NO WRITE PATH. Not behind a flag, not
commented out. Its whole job is to answer the questions that decide whether a
write path is worth building at all:

  - which management points and datapoints does THIS unit expose?
  - which setpoint mode did the installer configure -- roomTemperature,
    leavingWaterTemperature, or leavingWaterOffset? That single answer
    decides the entire strategy, because only the offset reliably keeps the
    compressor running rather than slamming a setpoint.
  - is powerfulMode present, and does it run the compressor or the immersion?
    (the API cannot tell us; only watching the power draw can)

See docs/proposal-ashp-load-shift.md for the economics, which are NOT as
good as first claimed: roughly 2.8-6.4p per kWh of heat shifted.

Unlike sigencloud.py, this is an OFFICIAL, documented API with scoped
credentials -- no owner password in plaintext.

    https://developer.cloud.daikineurope.com/     register, create an app

Two constraints shape everything here:

  - 200 requests per DAY, per application. Every response carries
    X-RateLimit-Remaining-day and this module prints it, because burning the
    budget on polling is the main way integrations get this wrong. An
    event-driven agent that acts on slot boundaries spends about two calls
    per slot; a poller spends the lot just watching.
  - A GET immediately after a PATCH returns STALE data. The reference
    implementations wait ~10 s. Nothing here writes, but any future write
    path must read back the way sigencloud.set_mode_verified does.

Setup, once:

    python3 daikin.py --auth-url        # open the URL it prints, sign in
    python3 daikin.py --auth "<url>"    # paste the URL you were bounced to

The redirect never has to work. Daikin rejects localhost as a registered
redirect URI, so every other implementation runs a local HTTPS server with a
self-signed certificate. We do not: the authorisation code is in the address
bar before the browser loads anything, so a URI that resolves nowhere is
fine as long as it matches what is registered.

Then:

    python3 daikin.py                   # what does this unit expose?
    python3 daikin.py --raw             # the whole payload, unabridged
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from config import ConfigError, load_env, state_path

API_BASE = "https://api.onecta.daikineurope.com"
AUTHORIZE_URL = "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
TOKEN_URL = "https://idp.onecta.daikineurope.com/v1/oidc/token"

# offline_access is what yields a refresh token. Without it the browser step
# would have to be repeated every hour, which is useless for an unattended
# agent.
SCOPE = "openid onecta:basic.integration offline_access"

TIMEOUT = 30.0
TOKEN_FILE = ".daikin-token.json"
# Append-only. See snapshot(): the API keeps two calendar years and
# silently drops the older one every January, so this file is the only
# thing that will still know what 2025 cost once 2027 begins.
HISTORY_FILE = ".daikin-history.jsonl"
# Consumption is kept SEPARATELY and keyed by month, not repeated into every
# observation. The arrays change once a month while sensors change constantly,
# so inlining them multiplied the history file by roughly four for no
# information at all.
CONSUMPTION_FILE = ".daikin-consumption.json"

# Refresh this long before expiry rather than waiting to be refused. The
# agent's ticks are minutes apart, so a token that expires between deciding
# and acting would cost a slot.
REFRESH_MARGIN = 300.0

USER_AGENT = "oig-sigen/1.0 (+https://github.com/msharrison-glitch/oig-sigen)"


class DaikinError(RuntimeError):
    """Anything the Onecta API would not do."""


class RateLimited(DaikinError):
    """The 200/day budget is spent. Retrying today will not help."""


# --------------------------------------------------------------------------
# credentials and tokens


def _need(env: dict, key: str) -> str:
    value = env.get(key)
    if not value:
        raise ConfigError(
            f"{key} is not set. Register an application at "
            "https://developer.cloud.daikineurope.com/ and put its "
            "credentials in .env -- see .env.example.")
    return value


def _redirect_uri(env: dict) -> str:
    # Must match the portal registration byte for byte. It does not need to
    # resolve or serve anything; see the module docstring.
    return env.get("DAIKIN_REDIRECT_URI") or "https://oig-sigen.invalid/callback"


def _read_tokens() -> dict:
    path = state_path(TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise DaikinError(f"{path} is unreadable: {exc}") from exc


def _write_tokens(payload: dict) -> None:
    """0600 before anything is written into it, not after.

    A refresh token is a durable credential for the account; a window where
    it sits world-readable is a window too many.
    """
    path = state_path(TOKEN_FILE)
    # os.open with the mode, rather than open() then chmod: the latter leaves
    # a window where a refresh token sits world-readable. fdopen takes
    # ownership of the descriptor, so it must not be closed separately.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(payload, out, indent=2)
    os.chmod(path, 0o600)   # a pre-existing file keeps its old mode otherwise


def _post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise DaikinError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DaikinError(f"cannot reach {url}: {exc.reason}") from exc


def authorize_url(env: dict) -> str:
    """Step one: the URL the owner opens in a browser."""
    # Check the credentials BEFORE touching the token file, or a user who
    # has not filled in .env yet gets a stray file created and then an error.
    client_id = _need(env, "DAIKIN_CLIENT_ID")
    _need(env, "DAIKIN_CLIENT_SECRET")

    state = secrets.token_urlsafe(24)
    stored = _read_tokens()
    stored["pending_state"] = state
    _write_tokens(stored)

    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri(env),
        "scope": SCOPE,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(env: dict, redirected: str) -> None:
    """Step two: turn the code in the redirected URL into tokens."""
    parsed = urllib.parse.urlparse(redirected.strip())
    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        raise DaikinError(
            f"Daikin refused authorisation: {params['error'][0]} "
            f"{params.get('error_description', [''])[0]}")

    code = (params.get("code") or [None])[0]
    if not code:
        raise DaikinError(
            "No ?code= in that URL. Paste the whole address bar contents "
            "from the page you were bounced to -- the browser will have "
            "shown an error, which is expected and fine.")

    stored = _read_tokens()
    expected = stored.get("pending_state")
    got = (params.get("state") or [None])[0]
    # No stored state means we cannot check it -- the token file was deleted
    # between the two commands, or this URL came from another machine. We
    # proceed rather than refuse: this is a local CLI where the owner pastes
    # a URL they just fetched themselves, so there is no third party to
    # forge one. The check exists to catch a stale paste, not an attacker.
    if expected and got != expected:
        raise DaikinError(
            "state mismatch -- this URL is not from the --auth-url we just "
            "issued. Run --auth-url again and use the fresh link.")

    payload = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(env),
        "client_id": _need(env, "DAIKIN_CLIENT_ID"),
        "client_secret": _need(env, "DAIKIN_CLIENT_SECRET"),
    })
    _store_grant(payload)


def _store_grant(payload: dict) -> None:
    if "access_token" not in payload:
        raise DaikinError(f"no access_token in the reply: {payload}")
    if "refresh_token" not in payload:
        # Without offline_access this happens, and the agent would then need
        # a browser every hour. Better to fail loudly now than at 3am.
        raise DaikinError(
            "no refresh_token in the reply -- the application may not have "
            "been granted offline_access. Re-run --auth-url.")
    _write_tokens({
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": time.time() + float(payload.get("expires_in", 3600)),
    })


def access_token(env: dict) -> str:
    """A usable access token, refreshed if it is close to expiry."""
    stored = _read_tokens()
    if "refresh_token" not in stored:
        raise DaikinError(
            "Not authorised yet. Run:  python3 daikin.py --auth-url")

    if stored.get("access_token") and \
            time.time() < float(stored.get("expires_at", 0)) - REFRESH_MARGIN:
        return stored["access_token"]

    payload = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": stored["refresh_token"],
        "client_id": _need(env, "DAIKIN_CLIENT_ID"),
        "client_secret": _need(env, "DAIKIN_CLIENT_SECRET"),
    })
    # Some servers return a fresh refresh_token, some do not. Keep the old
    # one if it is absent, or the next refresh has nothing to present.
    payload.setdefault("refresh_token", stored["refresh_token"])
    _store_grant(payload)
    return payload["access_token"]


# --------------------------------------------------------------------------
# reading


def _get(env: dict, path: str) -> tuple:
    """GET, returning (parsed body, rate-limit remaining or None)."""
    request = urllib.request.Request(API_BASE + path, method="GET")
    request.add_header("Authorization", "Bearer " + access_token(env))
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            remaining = response.headers.get("X-RateLimit-Remaining-day")
            body = json.loads(response.read().decode("utf-8"))
            return body, remaining
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(
                "429: the 200/day budget is spent. It resets on Daikin's "
                "clock, not at local midnight; do not retry in a loop, "
                "because requests made while limited extend the block.")
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise DaikinError(f"HTTP {exc.code} from {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DaikinError(f"cannot reach {path}: {exc.reason}") from exc


def gateway_devices(env: dict) -> tuple:
    """The whole device tree. One call out of 200."""
    return _get(env, "/v1/gateway-devices")


# --------------------------------------------------------------------------
# reading the payload
#
# Written against a real Altherma payload captured 2026-09-10, not against
# other people's source. Two things in the real shape are easy to get wrong
# and both are handled here:
#
#   - consumptionData.electrical carries a STRING "unit" key alongside the
#     operation-mode dicts, so anything that assumes every value is a dict
#     crashes on it.
#   - the 24 monthly buckets are two CALENDAR years, not a rolling window.
#     Index 0-11 is last year, 12-23 this year, and the trailing entries are
#     null because those months have not happened. Reading them as "the last
#     24 months" makes heating appear to peak in September.


# The setpoint the installer configured decides the strategy, so call it out
# rather than leaving it buried in the dump.
SETPOINT_NOTES = {
    "leavingWaterOffset":
        "BEST -- shifts the weather curve, compressor keeps running",
    "leavingWaterTemperature":
        "workable, but moving flow temperature directly has a big COP cost",
    "roomTemperature":
        "workable, but setpoint jumps make the unit cycle",
    "domesticHotWaterTemperature":
        "read-only on some units -- check before designing around it",
}

MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")


def management_points(payload: list):
    """(device, management point) pairs, so callers stop re-walking the tree."""
    for device in payload or []:
        for point in device.get("managementPoints", []) or []:
            yield device, point


def setpoints(point: dict) -> dict:
    """{(operation mode, setpoint name): spec} for one management point."""
    control = point.get("temperatureControl") or {}
    modes = (control.get("value") or {}).get("operationModes") or {}
    found = {}
    for mode_name, mode in modes.items():
        for name, spec in ((mode or {}).get("setpoints") or {}).items():
            if isinstance(spec, dict):
                found[(mode_name, name)] = spec
    return found


def sensors(point: dict) -> dict:
    """{name: value} from sensoryData, flattened."""
    data = (point.get("sensoryData") or {}).get("value") or {}
    return {k: v.get("value") for k, v in data.items()
            if isinstance(v, dict) and "value" in v}


def consumption(point: dict, this_year: int = None) -> dict:
    """Monthly electrical consumption, labelled with real calendar months.

    Returns {"unit": "kWh", "monthly": [(year, month_number, value), ...]}
    with the null future months dropped. See the note above about why these
    are calendar years rather than a rolling window.
    """
    node = (point.get("consumptionData") or {}).get("value") or {}
    electrical = node.get("electrical") or {}
    unit = electrical.get("unit") if isinstance(
        electrical.get("unit"), str) else None

    buckets = None
    for mode, series in electrical.items():
        if not isinstance(series, dict):
            continue                       # the "unit" string lives here too
        if isinstance(series.get("m"), list):
            buckets = series["m"]
            break
    if not buckets:
        return {}

    if this_year is None:
        import datetime
        this_year = datetime.date.today().year   # injectable, so the calendar
                                                 # mapping can be tested
    out = []
    for index, value in enumerate(buckets[:24]):
        if value is None:
            continue
        year = this_year - 1 + (index // 12)
        out.append((year, index % 12 + 1, value))
    return {"unit": unit, "monthly": out}


def snapshot(payload: list) -> dict:
    """One flat, append-able record of everything worth keeping.

    Deliberately includes the consumption arrays. The monthly window is two
    calendar years, so 2025 disappears on 1 January 2027 exactly as 2024
    already has -- archiving on every poll is the only way to end up with a
    continuous record, and it costs no extra API calls because it arrives in
    the same payload.
    """
    record = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "points": {}}
    for device, point in management_points(payload):
        kind = point.get("managementPointType")
        if kind not in ("climateControl", "domesticHotWaterTank"):
            continue
        record.setdefault("device", device.get("deviceModel"))
        entry = {
            "embeddedId": point.get("embeddedId"),
            "onOffMode": (point.get("onOffMode") or {}).get("value"),
            "operationMode": (point.get("operationMode") or {}).get("value"),
            "controlMode": (point.get("controlMode") or {}).get("value"),
            "setpointMode": (point.get("setpointMode") or {}).get("value"),
            "powerfulMode": (point.get("powerfulMode") or {}).get("value"),
            "sensors": sensors(point),
            "setpoints": {f"{mode}/{name}": spec.get("value")
                          for (mode, name), spec in setpoints(point).items()},
        }
        record["points"][kind] = entry
    return record


def describe(payload: list) -> None:
    for device, point in management_points(payload):
        kind = point.get("managementPointType")
        if kind == "gateway":
            print(f"\ndevice: {device.get('deviceModel')}  "
                  f"type={device.get('type')}  online="
                  f"{(device.get('isCloudConnectionUp') or {}).get('value')}")
            continue
        if kind not in ("climateControl", "domesticHotWaterTank"):
            continue

        print(f"\n[{kind}]  embeddedId={point.get('embeddedId')}")
        for name in ("onOffMode", "operationMode", "controlMode",
                     "setpointMode", "heatupMode", "powerfulMode"):
            node = point.get(name)
            if isinstance(node, dict) and "value" in node:
                mark = "SETTABLE" if node.get("settable") else "read-only"
                print(f"    {name:24s} {str(node['value']):18s} [{mark}]")

        for (mode, name), spec in sorted(setpoints(point).items()):
            mark = "SETTABLE" if spec.get("settable") else "READ-ONLY"
            rng = ""
            if "minValue" in spec:
                rng = (f"({spec.get('minValue')}..{spec.get('maxValue')}"
                       f", step {spec.get('stepValue')})")
            print(f"    setpoint [{mode}] {name}: {spec.get('value')} "
                  f"{rng} [{mark}]")
            note = SETPOINT_NOTES.get(name)
            if note:
                print(f"        ^ {note}")

        read = sensors(point)
        if read:
            print("    sensors: " + ", ".join(
                f"{k}={v}" for k, v in sorted(read.items())))

        used = consumption(point)
        if used.get("monthly"):
            unit = used.get("unit") or "?"
            total = {}
            for year, _month, value in used["monthly"]:
                total[year] = total.get(year, 0) + value
            summary = "  ".join(f"{y}: {t} {unit}"
                                for y, t in sorted(total.items()))
            print(f"    consumption: {summary}"
                  "   (2 calendar years; the older one is lost each January)")


def merge_consumption(payload: list) -> tuple:
    """Fold this poll's monthly figures into a permanent per-month record.

    Keyed by "YYYY-MM" and merged rather than appended, so polling every half
    hour costs nothing extra and the file stays the size of the history it
    covers. This is what outlives the API's two-calendar-year window: once
    2027 begins, 2025 exists here and nowhere else.
    """
    path = state_path(CONSUMPTION_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            store = json.load(handle)
    except (FileNotFoundError, ValueError):
        store = {}

    added = 0
    for _device, point in management_points(payload):
        kind = point.get("managementPointType")
        if kind not in ("climateControl", "domesticHotWaterTank"):
            continue
        used = consumption(point)
        unit = used.get("unit") or "kWh"
        for year, month, value in used.get("monthly", []):
            key = f"{year}-{month:02d}"
            entry = store.setdefault(key, {})
            if entry.get(kind) != value:
                added += 1
            entry[kind] = value
            entry["unit"] = unit

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2, sort_keys=True)
    return str(path), len(store), added


def append_history(record: dict) -> str:
    """One JSON object per line. Never rewrites, so a crash cannot eat it."""
    path = state_path(HISTORY_FILE)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daikin Onecta, read-only. Writes nothing, ever.")
    parser.add_argument("--auth-url", action="store_true",
                        help="print the URL to open in a browser")
    parser.add_argument("--auth", metavar="URL",
                        help="the URL you were redirected to, in quotes")
    parser.add_argument("--raw", action="store_true",
                        help="dump the whole payload as JSON")
    parser.add_argument("--snapshot", action="store_true",
                        help="append one observation to the history file "
                             "(one API call; safe to schedule)")
    args = parser.parse_args()

    try:
        env = load_env()

        if args.auth_url:
            print("\nOpen this in a browser and sign in with your Onecta "
                  "account:\n")
            print("  " + authorize_url(env))
            print("\nYou will be bounced to a page that fails to load. That "
                  "is expected.\nCopy the whole address bar and run:\n")
            print('  python3 daikin.py --auth "<paste it here>"\n')
            return 0

        if args.auth:
            exchange_code(env, args.auth)
            print("Authorised. Refresh token stored 0600 in "
                  f"{state_path(TOKEN_FILE)}")
            print("Now run:  python3 daikin.py")
            return 0

        payload, remaining = gateway_devices(env)
        if args.raw:
            print(json.dumps(payload, indent=2))
        elif args.snapshot:
            record = snapshot(payload)
            where = append_history(record)
            store, months, changed = merge_consumption(payload)
            climate = record.get("points", {}).get("climateControl", {})
            print(f"appended to {where}")
            print(f"  {record['fetched_at']}  "
                  f"heating={climate.get('onOffMode')}  "
                  f"sensors={climate.get('sensors')}")
            print(f"consumption: {months} months on record in {store}"
                  f"  ({changed} value(s) updated this poll)")
        else:
            describe(payload)
        if remaining is not None:
            print(f"\nrate limit: {remaining} of 200 requests left today")
        return 0

    except (DaikinError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
