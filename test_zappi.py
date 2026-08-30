#!/usr/bin/env python3
"""
Offline test of the Zappi client.

No network. Digest auth against a real TLS server is not worth mocking; what
is worth pinning is the part that will silently mislead if it is wrong -- the
server chosen from the hub serial, and the decoding of status codes into
"is the car actually drawing power". A wrong answer there feeds straight into
how much headroom we think we have before the main fuse.

    python3 test_zappi.py
"""

from __future__ import annotations

import zappi

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<54} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def stub(client, payload):
    client.raw = lambda: payload          # type: ignore[method-assign]
    return client


def main() -> int:
    print("\nServer comes from the LAST DIGIT of the hub serial")
    check("serial ...8 -> s8", zappi.ZappiClient("12345678", "k").host,
          "s8.myenergi.net")
    check("serial ...1 -> s1", zappi.ZappiClient("20000001", "k").host,
          "s1.myenergi.net")
    check("whitespace is stripped",
          zappi.ZappiClient(" 12345670 ", "k").host, "s0.myenergi.net")
    try:
        zappi.ZappiClient("", "k")
        raised = False
    except zappi.ZappiError:
        raised = True
    check("an empty serial is refused, not guessed", raised, True)

    print("\nDecoding what the car is actually doing")
    c = stub(zappi.ZappiClient("12345678", "k"),
             {"zappi": [{"sno": 10088888, "sta": 3, "zmo": 1, "pst": "C2",
                         "div": 7040, "che": 12.5}]})
    s = c.status()
    check("charging is charging", s["charging"], True)
    check("status decoded", s["status"], "Charging")
    check("mode decoded", s["mode"], "Fast")
    check("plug state decoded", s["plug"], "charging")
    check("watts become kW", s["power_kw"], 7.04)
    check("energy added carried through", s["added_kwh"], 12.5)

    c = stub(zappi.ZappiClient("12345678", "k"),
             {"zappi": [{"sno": 1, "sta": 1, "zmo": 4, "pst": "A"}]})
    s = c.status()
    check("paused is not charging", s["charging"], False)
    check("stopped mode decoded", s["mode"], "Stopped")
    check("unplugged decoded", s["plug"], "EV disconnected")
    check("absent div reads as zero, not None", s["power_kw"], 0.0)

    print("\nUnknowns are surfaced, never silently swallowed")
    c = stub(zappi.ZappiClient("12345678", "k"),
             {"zappi": [{"sno": 1, "sta": 99, "zmo": 42, "pst": "ZZ"}]})
    s = c.status()
    check("unknown status says so", s["status"], "? (99)")
    check("unknown mode says so", s["mode"], "? (42)")
    check("unknown plug code passes through", s["plug"], "ZZ")

    print("\nNo Zappi on the account")
    check("empty list -> None",
          stub(zappi.ZappiClient("12345678", "k"), {"zappi": []}).status(),
          None)
    check("missing key -> None",
          stub(zappi.ZappiClient("12345678", "k"), {}).status(), None)

    print("\n" + "=" * 68)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
