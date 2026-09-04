#!/usr/bin/env python3
"""
Sigen cloud account client, for one job: putting the operational mode back.

Releasing Remote EMS always drops the plant to Self-Consumption, and the mode
has no Modbus register at plant or device level. This is the only known way
to restore it.

Read the health warning before relying on it. The API is unofficial and
undocumented; it broke once between app v2 and v3; its reference
implementation was removed from GitHub (2026-08-31); and it authenticates
with your full mySigen account password, not a scoped token. Fine on your own
plant, a poor foundation for anything holding other owners' credentials.

Stdlib only, with one exception: the password is AES-128-CBC encrypted before
transmission and Python ships no AES, so `openssl` is driven via subprocess
rather than taking a pip dependency. The key and IV are hardcoded in
Sigenergy's own web app ("sigensigensigenp" for both), so this is obfuscation
rather than security -- we reproduce it only because the endpoint demands it.

    python3 sigencloud.py                 # show the current operational mode
    python3 sigencloud.py --list          # list selectable modes
    python3 sigencloud.py --set "Sigen AI Mode"
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from datetime import datetime, timedelta, timezone

from config import ConfigError, load_env, state_path

REGION_BASE_URLS = {
    "eu": "https://api-eu.sigencloud.com/",
    "cn": "https://api-cn.sigencloud.com/",
    "apac": "https://api-apac.sigencloud.com/",
    "us": "https://api-us.sigencloud.com/",
}
CLIENT_SERVER_BY_REGION = {"eu": "eu", "cn": "cn", "apac": "aus", "us": "us"}

# Lifted from Sigenergy's own web app; the endpoint rejects anything else.
WEB_APP_VERSION = "3.5.2"
WEB_APP_BUILD = "1"
WEB_APP_PACKAGE = "sigen_app"
WEB_APP_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; "
                      "rv:152.0) Gecko/20100101 Firefox/152.0")

# Their own client hardcodes both. Not a secret, not protecting anything.
PW_KEY = b"sigensigensigenp"
PW_IV = b"sigensigensigenp"

TIMEOUT = 30.0


class SigenCloudError(RuntimeError):
    pass


def encrypt_password(password: str) -> str:
    """AES-128-CBC, PKCS7, base64 -- exactly what the web app sends.

    Via openssl because the stdlib has no AES and this project does not take
    pip dependencies. If openssl is missing the failure is explicit.
    """
    try:
        out = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-K", PW_KEY.hex(),
             "-iv", PW_IV.hex(), "-base64", "-A"],
            input=password.encode(), capture_output=True, check=True)
    except FileNotFoundError:
        raise SigenCloudError(
            "openssl not found. It encrypts the password before sending; "
            "install it (Alpine: apk add openssl) or this cannot log in.")
    except subprocess.CalledProcessError as exc:
        raise SigenCloudError(f"openssl failed: {exc.stderr[:200]!r}")
    return out.stdout.decode().strip()


class SigenCloud:
    def __init__(self, username: str, password: str,
                 region: str = "eu") -> None:
        if region not in REGION_BASE_URLS:
            raise SigenCloudError(
                f"unsupported region {region!r}; expected one of "
                f"{', '.join(REGION_BASE_URLS)}")
        self.username = username
        self.password = encrypt_password(password)
        self.region = region
        self.base = REGION_BASE_URLS[region]
        self.user_device_id = str(int(time.time() * 1000))
        self.session_id = str(uuid.uuid4())
        self.token: str | None = None
        self.station_id: int | None = None

    # -- plumbing ---------------------------------------------------------

    def _origin(self) -> str:
        return self.base.rstrip("/").replace("https://api-", "https://app-", 1)

    def _headers(self, content_type: str, path: str,
                 token: str | None = None) -> dict:
        origin = self._origin()
        # Their web app sends Date.now() * 1000. Reproduced, oddity included.
        stamp = str(int(time.time() * 1000) * 1000)
        headers = {
            "Accept": "*/*",
            "User-Agent": WEB_APP_USER_AGENT,
            "Content-Type": content_type,
            "Origin": origin,
            "Referer": f"{origin}/",
            "lang": "en_US",
            "client-server": CLIENT_SERVER_BY_REGION[self.region],
            "AUTH-CLIENT-ID": "sigen",
            "VERSION": "RELEASE",
            "sg-v": WEB_APP_VERSION,
            "sg-bui": WEB_APP_BUILD,
            "sg-env": "1",
            "sg-platform": "web",
            "sg-pkg": WEB_APP_PACKAGE,
            "sg-ts": stamp,
            "sg-log-id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                        f"{path}{stamp}")),
            "sg-session": self.session_id,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _call(self, method: str, path: str, *, body=None, form=None,
              basic: tuple[str, str] | None = None,
              _reauth: bool = True) -> dict:
        url = self.base + path
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers = self._headers("application/x-www-form-urlencoded", path)
        else:
            data = json.dumps(body).encode() if body is not None else None
            headers = self._headers("application/json", "", self.token)
        if basic:
            token = base64.b64encode(
                f"{basic[0]}:{basic[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300]
            if exc.code == 401:
                raise SigenCloudError(
                    "Sigen cloud rejected the credentials. Check "
                    "SIGEN_CLOUD_USERNAME and SIGEN_CLOUD_PASSWORD are your "
                    f"mySigen app login. ({detail!r})")
            # 424 is what this API returns for an EXPIRED token, not a bad
            # one -- the body reads "user credentials have expired". The
            # token lasts about a day, so any agent that stays up overnight
            # meets this. Treating it as fatal killed the agent mid-slot on
            # 2026-09-03, after it had already switched the plant, which is
            # the worst possible moment. Re-authenticate once and retry.
            #
            # `basic` marks the login call itself: retrying that would
            # recurse. `_reauth` bounds it to one attempt, so a genuinely
            # broken credential still fails instead of looping.
            if exc.code == 424 and _reauth and basic is None:
                print(f"sigencloud: token expired ({detail!r:.60}), "
                      "re-authenticating", file=sys.stderr, flush=True)
                self.token = None
                self.login()
                return self._call(method, path, body=body, form=form,
                                  basic=basic, _reauth=False)
            raise SigenCloudError(f"HTTP {exc.code}: {detail!r}")
        except urllib.error.URLError as exc:
            raise SigenCloudError(f"cannot reach {url}: {exc.reason}")
        except ValueError as exc:
            raise SigenCloudError(f"unparseable response: {exc}")
        if payload.get("code") not in (None, 0, 200):
            raise SigenCloudError(f"API error: {payload}")
        return payload

    # -- session ----------------------------------------------------------

    def login(self) -> None:
        payload = self._call("POST", "auth/oauth/token", _reauth=False, form={
            "scope": "server",
            "grant_type": "password",
            "userDeviceId": self.user_device_id,
            "username": self.username,
            "password": self.password,
        }, basic=("sigen", "sigen"))
        data = payload.get("data") or {}
        token = data.get("access_token")
        if not token:
            raise SigenCloudError(f"no access_token in response: {payload}")
        self.token = token

    def ensure(self) -> None:
        if self.token is None:
            self.login()
        if self.station_id is None:
            data = self._call("GET", "device/owner/station/home").get("data") or {}
            self.station_id = data.get("stationId")
            if self.station_id is None:
                raise SigenCloudError(f"no stationId in response: {data}")

    # -- modes ------------------------------------------------------------

    def modes(self) -> dict:
        self.ensure()
        return (self._call(
            "GET",
            f"device/energy-profile/mode/all/{self.station_id}"
        ).get("data") or {})

    def current_mode(self) -> dict:
        self.ensure()
        return (self._call(
            "GET",
            f"device/energy-profile/mode/current/{self.station_id}"
        ).get("data") or {})

    def set_mode(self, operation_mode: int, profile_id: int = -1) -> dict:
        """Write. Everything above this line is read-only."""
        self.ensure()
        return self._call("PUT", "device/energy-profile/mode", body={
            "stationId": self.station_id,
            "operationMode": operation_mode,
            "profileId": profile_id,
        })

    def set_mode_verified(self, operation_mode: int,
                          profile_id: int = -1) -> dict:
        """Set the mode, then read it back and prove it actually took.

        The PUT can return success and change nothing, and an unverified
        write is worse than a failed one, because the caller deletes the only
        record that would let anything else put it right. So prove it, and
        let the caller's existing failure path keep that record when we
        cannot.

        A CORRECTION, for whoever reads this next. This was written on
        2026-09-03 believing a restore to mode 1 had been accepted and
        silently ignored, leaving the plant on the charging profile for eight
        hours. That was a misdiagnosis. The write took. A SECOND CONTROLLER
        on another host -- restarted by a Synology boot-up task after a power
        cut, unnoticed -- set the plant back to the charging profile three
        seconds later.

        The verification is still right, and it would catch that case too.
        But be clear about what it does not do: it reads back after
        MODE_VERIFY_SETTLE, and a competing controller three seconds behind
        can overwrite us just after we have confirmed. Verification proves
        our own write landed. It is not, and cannot be, protection against
        somebody else writing. The only protection there is one agent per
        plant.
        """
        result = self.set_mode(operation_mode, profile_id)
        last = None
        for attempt in range(MODE_VERIFY_ATTEMPTS):
            if MODE_VERIFY_SETTLE:
                time.sleep(MODE_VERIFY_SETTLE)
            last = self.current_mode()
            if last.get("currentMode") != operation_mode:
                continue
            # profileId only matters when selecting a custom profile
            if profile_id != -1 and last.get("currentProfileId") != profile_id:
                continue
            return result
        raise SigenCloudError(
            f"mode did not take: asked for mode {operation_mode} "
            f"profile {profile_id}, plant still reports mode "
            f"{(last or {}).get('currentMode')} "
            f"profile {(last or {}).get('currentProfileId')}")


# --------------------------------------------------------------------------
# deadman
# --------------------------------------------------------------------------

# Written before we switch INTO a charging profile, cleared once we have
# switched back. Its presence means "a charge profile may be selected"; the
# deadman restores if it has outlived its slot.
CLOUD_STATE = state_path(".cloud-mode.json")

# How hard set_mode_verified looks before giving up. The plant takes 18-31 s
# to actuate, but the API's own view of the mode updates far sooner; this is
# about catching a write that never landed, not waiting for the inverter.
MODE_VERIFY_ATTEMPTS = 4
MODE_VERIFY_SETTLE = 2.5


def write_cloud_state(payload: dict) -> None:
    tmp = CLOUD_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, CLOUD_STATE)


def read_cloud_state() -> dict | None:
    if not CLOUD_STATE.exists():
        return None
    try:
        return json.loads(CLOUD_STATE.read_text())
    except (OSError, ValueError):
        return None


def clear_cloud_state() -> None:
    try:
        CLOUD_STATE.unlink()
    except FileNotFoundError:
        pass


def record_pending_restore(mode: int, profile: int = -1,
                           expires_in_seconds: float = 0.0) -> None:
    """Remember a mode that still has to be put back.

    Written the moment a release leaves the plant on the wrong mode, and only
    cleared once the restore actually succeeds. That way a transient failure,
    or the agent dying between release and restore, is recovered by the next
    tick or by cron -- rather than leaving the owner on a mode they did not
    choose, with one line in a log they may not be reading.
    """
    state = read_cloud_state() or {}
    state.update({
        "restore_mode": mode,
        "restore_profile": profile,
        "pending_restore": True,
        # With no lifetime given the record is already overdue, so the
        # deadman acts on its next run -- that is the "we have just released
        # and owe a restore" case. A lifetime is passed while a lease is
        # actually held: the agent rolls it forward each tick, so the deadman
        # only fires once the agent has stopped doing so.
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=expires_in_seconds)).isoformat(),
    })
    write_cloud_state(state)


def pending_restore(now: datetime | None = None) -> tuple[int, int] | None:
    """The mode still owed to the owner, if any.

    A record whose deadline has not passed is NOT a debt: it is the note an
    agent leaves while it legitimately holds a lease, rolling the deadline
    forward each tick. Only once nobody is renewing it -- the agent released,
    or died -- does it become something to act on.
    """
    state = read_cloud_state() or {}
    if not state.get("pending_restore"):
        return None
    mode = state.get("restore_mode")
    if mode is None:
        return None
    expires = state.get("expires_at")
    if expires and (now or datetime.now(timezone.utc)) < \
            datetime.fromisoformat(expires):
        return None
    return int(mode), int(state.get("restore_profile", -1))


def deadman(client: "SigenCloud | None" = None) -> int:
    """Restore the previous mode if a charge selection has outlived its slot.

    The cloud path has no lease and nothing latches at the plant -- but if we
    select a charging profile and then fail to switch back (API down, token
    expired, process killed), the plant charges at the profile's rate until
    someone notices. At 8 kW on peak that is about GBP 2.38 an hour. So the
    same discipline as the Modbus lease applies: record the intent before
    acting, and give a separate process enough to clean up.

    Idempotent. Safe to run from cron at any time.
    """
    state = read_cloud_state()
    if not state:
        return 0
    expires = state.get("expires_at")
    if expires and datetime.now(timezone.utc) <= \
            datetime.fromisoformat(expires):
        return 0
    restore_to = state.get("restore_mode")
    restore_profile = state.get("restore_profile", -1)
    if restore_to is None:
        clear_cloud_state()
        return 0
    client = client or client_from_env()
    current = client.current_mode()
    if current.get("currentMode") == restore_to:
        # Already back where it belongs; nothing to undo.
        clear_cloud_state()
        return 0
    print(f"DEADMAN: charge selection expired at {expires}; "
          f"restoring mode {restore_to}")
    try:
        client.set_mode_verified(int(restore_to), int(restore_profile))
    except SigenCloudError as exc:
        # Keep the state file. Something must still be able to put this
        # right, and the next tick of the deadman is that something.
        print(f"DEADMAN: restore did NOT take ({exc}); state kept for retry",
              file=sys.stderr)
        return 1
    clear_cloud_state()
    return 0


def client_from_env() -> SigenCloud:
    env = load_env()
    user = env.get("SIGEN_CLOUD_USERNAME", "")
    pw = env.get("SIGEN_CLOUD_PASSWORD", "")
    if not user or not pw:
        raise ConfigError(
            "SIGEN_CLOUD_USERNAME and SIGEN_CLOUD_PASSWORD are not set in "
            ".env. These are your mySigen app login.")
    return SigenCloud(user, pw, env.get("SIGEN_CLOUD_REGION", "eu") or "eu")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true",
                    help="list selectable operational modes")
    ap.add_argument("--set", metavar="LABEL",
                    help="select a mode by its label (WRITES)")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--deadman", action="store_true",
                    help="restore the previous mode if a charge selection "
                         "has outlived its slot (for cron)")
    args = ap.parse_args()
    try:
        if args.deadman:
            return deadman()
        c = client_from_env()
        if args.list or args.set:
            data = c.modes()
            if args.raw:
                print(json.dumps(data, indent=2))
            defaults = data.get("defaultWorkingModes") or []
            profiles = data.get("energyProfileItems") or []
            if args.list:
                print(f"\n  station {c.station_id}")
                for m in defaults:
                    print(f"    [{m.get('value')}] {m.get('label')}")
                for m in profiles:
                    print(f"    [9/profile {m.get('profileId')}] "
                          f"{m.get('name')}")
                return 0
            wanted = args.set.strip().lower()
            for m in defaults:
                if str(m.get("label", "")).strip().lower() == wanted:
                    print(c.set_mode(int(m["value"])))
                    return 0
            for m in profiles:
                if str(m.get("name", "")).strip().lower() == wanted:
                    print(c.set_mode(9, int(m["profileId"])))
                    return 0
            print(f"no mode called {args.set!r}; use --list",
                  file=sys.stderr)
            return 1
        data = c.current_mode()
        print(json.dumps(data, indent=2) if args.raw
              else f"\n  station {c.station_id}\n  current mode: {data}")
    except (SigenCloudError, ConfigError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
