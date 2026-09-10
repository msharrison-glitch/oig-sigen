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
    state = secrets.token_urlsafe(24)
    stored = _read_tokens()
    stored["pending_state"] = state
    _write_tokens(stored)

    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": _need(env, "DAIKIN_CLIENT_ID"),
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
# what the unit actually offers

# The setpoint the installer configured decides the strategy, so call it out
# rather than leaving it buried in the dump.
SETPOINT_NOTES = {
    "leavingWaterOffset":
        "BEST -- shifts the weather curve, compressor keeps running",
    "leavingWaterTemperature":
        "workable, but moving flow temperature directly has a big COP cost",
    "roomTemperature":
        "workable, but setpoint jumps make the unit cycle",
}


def describe(payload: list) -> None:
    for device in payload:
        print(f"\ndevice {device.get('id', '?')}  "
              f"({device.get('deviceModel', 'unknown model')})  "
              f"online={device.get('isCloudConnectionUp', {}).get('value')}")

        for point in device.get("managementPoints", []) or []:
            kind = point.get("managementPointType", "?")
            embedded = point.get("embeddedId", "?")
            print(f"  management point: {kind}   embeddedId={embedded}")

            for name, value in sorted(point.items()):
                if name in ("managementPointType", "embeddedId"):
                    continue
                if not isinstance(value, dict):
                    continue
                print(f"      {name}: {_brief(value)}")
                if name == "temperatureControl":
                    _describe_setpoints(value)


def _brief(node: dict) -> str:
    if "value" in node and not isinstance(node["value"], (dict, list)):
        settable = node.get("settable")
        suffix = "" if settable is None else ("  settable" if settable
                                              else "  read-only")
        return f"{node['value']}{suffix}"
    return "(nested)"


def _describe_setpoints(control: dict) -> None:
    """Pull the setpoint names out, because they decide the whole design."""
    modes = (control.get("value") or {}).get("operationModes") or {}
    for mode_name, mode in sorted(modes.items()):
        setpoints = (mode or {}).get("setpoints") or {}
        for setpoint, spec in sorted(setpoints.items()):
            note = SETPOINT_NOTES.get(setpoint, "")
            detail = ""
            if isinstance(spec, dict):
                bits = [f"value={spec.get('value')}"]
                if "minValue" in spec:
                    bits.append(f"min={spec['minValue']}")
                if "maxValue" in spec:
                    bits.append(f"max={spec['maxValue']}")
                if "stepValue" in spec:
                    bits.append(f"step={spec['stepValue']}")
                if spec.get("settable") is not None:
                    bits.append("settable" if spec["settable"]
                                else "read-only")
                detail = "  ".join(bits)
            print(f"        setpoint [{mode_name}] {setpoint}: {detail}")
            if note:
                print(f"            ^ {note}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daikin Onecta, read-only. Writes nothing, ever.")
    parser.add_argument("--auth-url", action="store_true",
                        help="print the URL to open in a browser")
    parser.add_argument("--auth", metavar="URL",
                        help="the URL you were redirected to, in quotes")
    parser.add_argument("--raw", action="store_true",
                        help="dump the whole payload as JSON")
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
