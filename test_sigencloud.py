#!/usr/bin/env python3
"""
Offline test of the Sigen cloud client.

No network. What matters here is the password encryption (wrong and login
fails opaquely), the region routing, and the mode lookup by label -- because
restoring the wrong mode after a release is worse than restoring none.

    python3 test_sigencloud.py
"""

from __future__ import annotations

import base64
import subprocess

import sigencloud

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<52} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def main() -> int:
    print("\nPassword encryption must match their web app exactly")
    enc = sigencloud.encrypt_password("hello-world")
    check("base64 output", isinstance(enc, str) and enc.endswith("=="), True)
    back = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc",
         "-K", sigencloud.PW_KEY.hex(), "-iv", sigencloud.PW_IV.hex(),
         "-base64", "-A"], input=enc.encode(), capture_output=True)
    check("round-trips", back.stdout.decode(), "hello-world")
    check("deterministic (fixed IV, as theirs is)",
          sigencloud.encrypt_password("x") == sigencloud.encrypt_password("x"),
          True)
    check("block-aligned input is still padded",
          len(base64.b64decode(sigencloud.encrypt_password("0123456789abcdef"))),
          32)

    print("\nRegion routing")
    check("eu", sigencloud.SigenCloud("u", "p", "eu").base,
          "https://api-eu.sigencloud.com/")
    check("origin swaps api- for app-",
          sigencloud.SigenCloud("u", "p", "eu")._origin(),
          "https://app-eu.sigencloud.com")
    try:
        sigencloud.SigenCloud("u", "p", "moon")
        raised = False
    except sigencloud.SigenCloudError:
        raised = True
    check("unknown region refused, not defaulted", raised, True)

    print("\nMode lookup: restoring the WRONG mode is worse than none")
    c = sigencloud.SigenCloud("u", "p", "eu")
    c.token, c.station_id = "t", 1
    modes = {"defaultWorkingModes": [
                 {"value": "1", "label": "Sigen AI Mode"},
                 {"value": "0", "label": "Maximum Self-Powered"},
                 {"value": "7", "label": "Remote EMS Mode"}],
             "energyProfileItems": [{"profileId": 9664, "name": "9th Aug 25"}]}
    c._call = lambda *a, **k: {"data": modes}   # type: ignore[method-assign]
    got = c.modes()
    check("AI mode is value 1",
          [m["value"] for m in got["defaultWorkingModes"]
           if m["label"] == "Sigen AI Mode"], ["1"])
    check("custom profiles are listed separately",
          got["energyProfileItems"][0]["profileId"], 9664)

    print("\nHeaders the endpoint insists on")
    h = c._headers("application/json", "", "tok")
    for key in ("sg-v", "sg-ts", "sg-session", "client-server",
                "AUTH-CLIENT-ID", "Origin"):
        check(f"sends {key}", key in h, True)
    check("bearer token attached", h["Authorization"], "Bearer tok")

    print("\n" + "=" * 62)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
