#!/usr/bin/env python3
"""
Offline test of the Daikin Onecta client.

No network. The OAuth exchange against a real IdP is not worth mocking; what
is worth pinning is everything that will silently mislead if it is wrong.

Three of these exist because the mistake was actually made:

  - the 24 monthly consumption buckets are two CALENDAR years, not a rolling
    window. Reading them as "the last 24 months" made space heating appear to
    peak in September and bottom out in January, which is backwards for the
    UK and was only caught because it looked absurd. A quieter error -- say,
    off by one month -- would have gone straight into the business case.
  - consumptionData.electrical carries a STRING "unit" key alongside the
    operation-mode dicts. Code that assumes every value there is a dict
    crashes on it, which is exactly what the first parser did.
  - this module must never gain a write path by accident. The whole premise
    of the read-only stage is that nothing can leave the heating in a strange
    state while we learn the API, so that is asserted against the source
    rather than trusted to memory.

    python3 test_daikin.py
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import time

# Point state at a temp directory BEFORE daikin is used, so a developer's real
# refresh token can never be read or overwritten by the test suite. Same
# discipline as test_control.py keeping its hands off a live lease.
_TMP = tempfile.mkdtemp(prefix="daikin-test-")
os.environ["IOG_STATE_DIR"] = _TMP

import daikin                                                    # noqa: E402
from config import ConfigError                                   # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<56} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def raises(label: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        print(f"  PASS  {label:<56} {type(exc).__name__}")
        return
    print(f"  FAIL  {label:<56} no exception")
    failures.append(f"{label}: expected an exception")


# A real Altherma payload, captured 2026-09-10, with the identifiers replaced.
# Structure is untouched -- that is the point of keeping it.
FIXTURE = [{
    "id": "00000000-0000-0000-0000-000000000000",
    "deviceModel": "Altherma",
    "type": "heating-wlan",
    "isCloudConnectionUp": {"settable": False, "value": True},
    "managementPoints": [
        {"embeddedId": "gateway", "managementPointType": "gateway",
         "firmwareVersion": {"settable": False, "value": "4.1.0"},
         "macAddress": {"settable": False, "value": "00:00:00:00:00:00"}},
        {"embeddedId": "climateControlMainZone",
         "managementPointType": "climateControl",
         "onOffMode": {"settable": True, "value": "off"},
         "operationMode": {"settable": False, "value": "heating"},
         "controlMode": {"settable": False, "value": "roomTemperature"},
         "setpointMode": {"settable": False, "value": "weatherDependent"},
         "sensoryData": {"settable": False, "value": {
             "leavingWaterTemperature": {"value": 17},
             "outdoorTemperature": {"value": 16},
             "roomTemperature": {"value": 22.4}}},
         "temperatureControl": {"settable": True, "value": {
             "operationModes": {
                 "cooling": {"setpoints": {}},
                 "heating": {"setpoints": {
                     "leavingWaterOffset": {
                         "value": 5, "minValue": -10, "maxValue": 10,
                         "stepValue": 1, "settable": True},
                     "roomTemperature": {
                         "value": 21, "minValue": 12, "maxValue": 30,
                         "stepValue": 0.5, "settable": True}}}}}},
         "consumptionData": {"settable": False, "value": {"electrical": {
             "unit": "kWh",
             "heating": {
                 "d": [0] * 24,
                 "w": [0] * 14,
                 "m": [378, 280, 104, 18, 0, 0, 0, 0, 0, 131, 344, 429,
                       602, 363, 234, 104, 43, 0, 0, 0, 0,
                       None, None, None]}}}}},
        {"embeddedId": "domesticHotWaterTank",
         "managementPointType": "domesticHotWaterTank",
         "onOffMode": {"settable": True, "value": "on"},
         "powerfulMode": {"settable": True, "value": "off"},
         "setpointMode": {"settable": False, "value": "fixed"},
         "sensoryData": {"settable": False,
                         "value": {"tankTemperature": {"value": 46}}},
         "temperatureControl": {"settable": True, "value": {
             "operationModes": {"heating": {"setpoints": {
                 "domesticHotWaterTemperature": {
                     "value": 50, "minValue": 30, "maxValue": 75,
                     "stepValue": 1, "settable": False}}}}}},
         "consumptionData": {"settable": False, "value": {"electrical": {
             "unit": "kWh",
             "heating": {"m": [92, 90, 50, 55, 68, 52, 46, 54, 380, 153,
                               149, 128, 174, 147, 160, 115, 93, 70, 67,
                               67, 21, None, None, None]}}}}}]}]


def point(kind: str) -> dict:
    for _device, p in daikin.management_points(FIXTURE):
        if p.get("managementPointType") == kind:
            return p
    raise AssertionError(kind)


def main() -> int:
    print("\nThe 24 monthly buckets are two CALENDAR years, not a rolling "
          "window")
    used = daikin.consumption(point("climateControl"), this_year=2026)
    check("unit is read from the payload, not assumed", used["unit"], "kWh")
    check("21 months carry data, three trailing nulls dropped",
          len(used["monthly"]), 21)
    check("index 0 is January of LAST year", used["monthly"][0], (2025, 1, 378))
    check("index 11 is December of last year", used["monthly"][11],
          (2025, 12, 429))
    check("index 12 is January of THIS year", used["monthly"][12],
          (2026, 1, 602))
    check("index 20 is September, the last with data",
          used["monthly"][-1], (2026, 9, 0))

    # The whole point: heating must peak in winter. If the mapping slips, this
    # is the assertion that notices.
    by_month = {(y, m): v for y, m, v in used["monthly"]}
    check_true("January beats September in both years",
               by_month[(2026, 1)] > by_month[(2026, 9)]
               and by_month[(2025, 1)] > by_month[(2025, 9)])
    check("2025 totals 1684 kWh",
          sum(v for y, _m, v in used["monthly"] if y == 2025), 1684)

    print("\nA string 'unit' key sitting among the operation-mode dicts")
    # This shape crashed the first parser. Keep it crashing here instead.
    check_true("hot water parses too",
               daikin.consumption(point("domesticHotWaterTank"),
                                  this_year=2026)["monthly"])
    check("no consumption data at all -> {}", daikin.consumption({}), {})
    check("consumptionData present but empty -> {}",
          daikin.consumption({"consumptionData": {"value": {}}}), {})

    print("\nSetpoints, and whether they can actually be written")
    sp = daikin.setpoints(point("climateControl"))
    check("leavingWaterOffset is settable",
          sp[("heating", "leavingWaterOffset")]["settable"], True)
    check("its range is -10..10", (sp[("heating", "leavingWaterOffset")]
                                   ["minValue"],
                                   sp[("heating", "leavingWaterOffset")]
                                   ["maxValue"]), (-10, 10))
    dhw = daikin.setpoints(point("domesticHotWaterTank"))
    # Ranked second in the proposal until the real unit said otherwise.
    check("the DHW setpoint is READ-ONLY on this unit",
          dhw[("heating", "domesticHotWaterTemperature")]["settable"], False)
    check("an empty operation mode contributes nothing",
          ("cooling", "leavingWaterOffset") in sp, False)

    print("\nSensors")
    check("climate sensors are flattened",
          daikin.sensors(point("climateControl")),
          {"leavingWaterTemperature": 17, "outdoorTemperature": 16,
           "roomTemperature": 22.4})
    check("no sensoryData -> {}", daikin.sensors({}), {})

    print("\nA snapshot keeps what the API will later throw away")
    snap = daikin.snapshot(FIXTURE)
    check("both primary points captured",
          sorted(snap["points"]), ["climateControl", "domesticHotWaterTank"])
    check("heating on/off recorded",
          snap["points"]["climateControl"]["onOffMode"], "off")
    check("the offset is recorded as a value",
          snap["points"]["climateControl"]["setpoints"]
          ["heating/leavingWaterOffset"], 5)
    check_true("consumption is archived, not just read",
               snap["points"]["climateControl"]["consumption"]["monthly"])
    check("the gateway is not archived", "gateway" in snap["points"], False)
    check_true("it round-trips through JSON", json.dumps(snap))

    print("\nHistory is append-only, so a crash cannot eat it")
    first = daikin.append_history({"fetched_at": "a"})
    daikin.append_history({"fetched_at": "b"})
    lines = io.open(first, encoding="utf-8").read().strip().splitlines()
    check("two appends, two lines", len(lines), 2)
    check("the first is still there", json.loads(lines[0])["fetched_at"], "a")

    print("\nThe authorisation URL")
    env = {"DAIKIN_CLIENT_ID": "cid", "DAIKIN_CLIENT_SECRET": "sec"}
    url = daikin.authorize_url(env)
    check_true("asks for offline_access, or there is no refresh token",
               "offline_access" in url)
    check_true("carries the default redirect",
               "oig-sigen.invalid" in url)
    check_true("carries a state", "state=" in url)
    check("a configured redirect wins",
          "https%3A%2F%2Fexample.test%2Fcb" in daikin.authorize_url(
              dict(env, DAIKIN_REDIRECT_URI="https://example.test/cb")), True)

    os.remove(daikin.state_path(daikin.TOKEN_FILE))
    raises("no client id -> ConfigError, before any file is written",
           daikin.authorize_url, {})
    check("and no token file was left behind",
          os.path.exists(daikin.state_path(daikin.TOKEN_FILE)), False)

    print("\nThe token file is not world-readable, ever")
    daikin._write_tokens({"refresh_token": "r"})
    mode = stat.S_IMODE(os.stat(daikin.state_path(daikin.TOKEN_FILE)).st_mode)
    check("mode 0600", oct(mode), oct(0o600))

    print("\nExchanging the redirected URL")
    daikin._write_tokens({"pending_state": "S"})
    raises("a URL with no ?code= is refused",
           daikin.exchange_code, env, "https://x.test/cb")
    raises("an ?error= is reported rather than ignored",
           daikin.exchange_code, env,
           "https://x.test/cb?error=access_denied")
    raises("a stale paste is caught by the state check",
           daikin.exchange_code, env,
           "https://x.test/cb?code=C&state=WRONG")

    posted = {}

    def fake_post(url, fields):
        posted.update(fields)
        return {"access_token": "AT", "refresh_token": "RT",
                "expires_in": 3600}

    real_post, daikin._post_form = daikin._post_form, fake_post
    try:
        daikin.exchange_code(env, "https://x.test/cb?code=C&state=S")
        check("the code is exchanged", posted.get("code"), "C")
        check("as an authorization_code grant",
              posted.get("grant_type"), "authorization_code")
        check("the redirect is sent again, as the spec requires",
              posted.get("redirect_uri"), "https://oig-sigen.invalid/callback")
        check("a live token is reused rather than refreshed",
              daikin.access_token(env), "AT")

        print("\nA token near expiry is refreshed before it is refused")
        daikin._write_tokens({"access_token": "OLD", "refresh_token": "RT",
                              "expires_at": time.time() + 60})
        posted.clear()
        check("refreshed", daikin.access_token(env), "AT")
        check("using the refresh grant",
              posted.get("grant_type"), "refresh_token")

        # A server that returns no new refresh_token must not leave us unable
        # to refresh again -- that would need another browser round trip.
        daikin._post_form = lambda u, f: {"access_token": "AT2",
                                          "expires_in": 3600}
        daikin._write_tokens({"access_token": "OLD", "refresh_token": "KEEP",
                              "expires_at": 0})
        daikin.access_token(env)
        check("the old refresh token is kept when none is returned",
              daikin._read_tokens()["refresh_token"], "KEEP")
    finally:
        daikin._post_form = real_post

    print("\nA grant with no refresh_token fails loudly, not at 3am")
    raises("missing refresh_token is rejected",
           daikin._store_grant, {"access_token": "AT"})

    print("\nThis module has NO write path, and must not grow one by accident")
    source = io.open(daikin.__file__, encoding="utf-8").read()
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    check("no PATCH request is constructed", 'method="PATCH"' in code, False)
    check("no PUT request is constructed", 'method="PUT"' in code, False)
    check("nothing posts to the device API",
          "gateway-devices" in code.split("def _post_form")[0], False)
    check("the only device call is a GET",
          code.count('method="GET"'), 1)

    print("\ndescribe() survives the real payload shape")
    try:
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as out:
            daikin.describe(FIXTURE)
        text = out.getvalue()
        check_true("it names the good lever", "leavingWaterOffset" in text)
        check_true("it flags the read-only one", "READ-ONLY" in text)
        check_true("it warns the older year is lost", "lost each January"
                   in text)
    except Exception as exc:                      # pragma: no cover
        failures.append(f"describe() raised {exc!r}")
        print(f"  FAIL  describe() raised {exc!r}")

    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. Read-only, and the calendar mapping holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
