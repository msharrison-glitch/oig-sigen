#!/usr/bin/env python3
"""
myenergi Zappi status, read-only.

Two reasons this matters here. The Zappi is the largest other load in the
house, so knowing whether it is drawing 7 kW decides how much headroom is
left before the main fuse when we command a charge. And because Octopus
controls it, the car's behaviour is the other half of every dispatch slot --
if a slot moves shortly after we start importing, the Zappi log is what says
whether the car stopped, started, or did nothing.

Cloud API, Digest auth (MD5, qop=auth). Base URL is s<last digit of hub
serial>.myenergi.net; the server may redirect via the X_MYENERGI-asn header,
which is handled. Stdlib only.

Credentials. Two routes, and the digest exchange cannot tell them apart:

  1. API key: myaccount.myenergi.net -> Products -> the Gateway Device row at
     the top -> "Advanced" (key icon) -> Generate new API key. Only the
     account that REGISTERED the devices has that button; if an installer set
     the system up, it is on their account, not yours.
  2. App password: set one in the myenergi app and use that instead. Older
     method, still works, and avoids the account-ownership problem entirely.

    MYENERGI_SERIAL=12345678         # HUB serial, used as the username
    MYENERGI_API_KEY=...             # API key OR app password

    python3 zappi.py                 # current state
    python3 zappi.py --raw           # the whole payload
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from config import ConfigError, load_env

TIMEOUT = 20.0

# Field meanings differ between firmware versions and between community
# sources, so the parsed view below is best-effort and --raw is authoritative.
# Reported consistently enough to rely on:
CHARGE_STATUS = {1: "Paused", 3: "Charging", 5: "Complete"}
ZAPPI_MODE = {1: "Fast", 2: "Eco", 3: "Eco+", 4: "Stopped"}
PLUG_STATUS = {
    "A": "EV disconnected",
    "B1": "EV connected, not charging",
    "B2": "EV connected, waiting",
    "C1": "charging (no power)",
    "C2": "charging",
}


class ZappiError(RuntimeError):
    pass


class ZappiClient:
    def __init__(self, serial: str, api_key: str,
                 timeout: float = TIMEOUT) -> None:
        self.serial = str(serial).strip()
        self.api_key = api_key
        self.timeout = timeout
        if not self.serial:
            raise ZappiError("MYENERGI_SERIAL is empty")
        # The app picks the server from the LAST DIGIT of the hub serial.
        self.host = f"s{self.serial[-1]}.myenergi.net"
        self._opener = self._build_opener(self.host)

    def _build_opener(self, host: str):
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, f"https://{host}", self.serial,
                             self.api_key)
        return urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(manager))

    def _get(self, path: str, _redirected: bool = False) -> dict:
        url = f"https://{self.host}{path}"
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                # myenergi routes accounts to a particular server and says so
                # in a header rather than an HTTP redirect. Follow it once.
                asn = response.headers.get("X_MYENERGI-asn")
                body = response.read()
                if asn and asn != self.host and not _redirected:
                    self.host = asn
                    self._opener = self._build_opener(asn)
                    return self._get(path, _redirected=True)
                return json.loads(body or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ZappiError(
                    "myenergi rejected the credentials. Username is the HUB "
                    "serial, not the Zappi's. The password is either an API "
                    "key (myaccount -> Products -> Gateway row -> Advanced, "
                    "only on the account that registered the devices) or the "
                    "app password set in the myenergi app -- either works.")
            raise ZappiError(f"HTTP {exc.code} from myenergi: "
                             f"{exc.read()[:200]!r}")
        except urllib.error.URLError as exc:
            raise ZappiError(f"cannot reach {self.host}: {exc.reason}")
        except ValueError as exc:
            raise ZappiError(f"unparseable response: {exc}")

    def raw(self) -> dict:
        return self._get("/cgi-jstatus-Z")

    def status(self) -> dict | None:
        """Best-effort view of the first Zappi. None if there isn't one."""
        payload = self.raw()
        units = payload.get("zappi") or []
        if not units:
            return None
        z = units[0]
        watts = z.get("div") or 0
        return {
            "serial": z.get("sno"),
            "charging": z.get("sta") == 3,
            "status": CHARGE_STATUS.get(z.get("sta"), f"? ({z.get('sta')})"),
            "mode": ZAPPI_MODE.get(z.get("zmo"), f"? ({z.get('zmo')})"),
            "plug": PLUG_STATUS.get(z.get("pst"), z.get("pst")),
            "power_kw": round(watts / 1000, 3),
            "added_kwh": z.get("che"),
            "raw": z,
        }


def client_from_env() -> ZappiClient:
    env = load_env()
    serial = env.get("MYENERGI_SERIAL", "")
    key = env.get("MYENERGI_API_KEY", "")
    if not serial or not key:
        raise ConfigError(
            "MYENERGI_SERIAL and MYENERGI_API_KEY are not set. Get an API "
            "key from myaccount.myenergi.net -> Advanced -> API key, and use "
            "the HUB serial as the username.")
    return ZappiClient(serial, key)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    try:
        client = client_from_env()
        if args.raw:
            print(json.dumps(client.raw(), indent=2))
            return 0
        state = client.status()
    except (ZappiError, ConfigError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    if state is None:
        print("  no Zappi found on this account")
        return 1
    print(f"\n  Zappi {state['serial']}")
    print(f"    {state['status']}   mode {state['mode']}")
    print(f"    plug: {state['plug']}")
    print(f"    power: {state['power_kw']} kW"
          + (f"   added this session: {state['added_kwh']} kWh"
             if state.get("added_kwh") is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
