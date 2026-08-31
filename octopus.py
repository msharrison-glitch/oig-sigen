#!/usr/bin/env python3
"""
Octopus Intelligent Go dispatch schedule.

Two things make a half-hour slot cheap on IOG:

  1. The guaranteed off-peak window, 23:30-05:30 *local* time, every night.
  2. Bonus dispatch slots that Octopus schedules for the car, which are
     billed at the off-peak rate for the whole property.

This module fetches (2) from the Kraken GraphQL API, unions it with (1),
and hands back a merged list of cheap periods.

Stdlib only -- urllib, json, zoneinfo. No requests, no pip install.

Credentials come from a .env file beside this script:

    OCTOPUS_API_KEY=sk_live_...
    OCTOPUS_ACCOUNT_NUMBER=A-1234ABCD

Usage:
    python3 octopus.py            # print the next 24h of cheap slots
    python3 octopus.py --raw      # dump the raw dispatch payload
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import ConfigError, load_env, state_path

# Trailing slash matters. Octopus runs Django with APPEND_SLASH, so the
# slashless form 301s -- and urllib follows redirects by turning POST into
# GET and dropping the body, which the server reports as the misleading
# "Must provide query string".
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"
LOCAL_TZ = ZoneInfo("Europe/London")

# The guaranteed IOG off-peak window, in local time.
OFF_PEAK_START = time(23, 30)
OFF_PEAK_END = time(5, 30)

TOKEN_CACHE = state_path(".octopus-token.json")

# Kraken tokens last about an hour; refresh well before that.
TOKEN_TTL = timedelta(minutes=50)

# Octopus renamed this from the older `apiKeyAuthentication`. The input type
# also accepts email/password, but that route now requires hCaptcha and
# OAuth2/PKCE (hardened June 2026) -- API key is the path that still works
# headlessly.
AUTH_MUTATION = """
mutation ObtainKrakenToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    refreshToken
    refreshExpiresIn
  }
}
"""

# completedDispatches is fetched for --raw only: handy for checking after
# the fact which bonus slots actually ran. parse_dispatches() ignores it.
DISPATCH_QUERY = """
query getData($accountNumber: String!) {
  plannedDispatches(accountNumber: $accountNumber) {
    startDt
    endDt
    deltaKwh
    meta { source location }
  }
  completedDispatches(accountNumber: $accountNumber) {
    startDt
    endDt
    deltaKwh
    meta { source location }
  }
}
"""


class OctopusError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects rather than silently downgrading POST to GET.

    urllib's default handler turns a 301/302/303 into a GET and discards the
    request body. For a GraphQL POST that produces a confusing server-side
    error instead of an obvious client-side one, so we fail loudly here.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OctopusError(
            f"Octopus redirected {req.get_full_url()} -> {newurl} "
            f"(HTTP {code}). The request body would have been lost. "
            f"Update GRAPHQL_URL to the redirect target."
        )


_OPENER = urllib.request.build_opener(NoRedirect)


# --------------------------------------------------------------------------
# slots
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """A period during which import is at the off-peak rate."""
    start: datetime          # tz-aware, UTC
    end: datetime            # tz-aware, UTC
    source: str              # "off-peak" | "dispatch"
    kwh: float | None = None

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def local(self) -> tuple[datetime, datetime]:
        return self.start.astimezone(LOCAL_TZ), self.end.astimezone(LOCAL_TZ)

    def __str__(self) -> str:
        a, b = self.local()
        day = a.strftime("%a %d %b")
        kwh = f"  {self.kwh:.2f} kWh" if self.kwh else ""
        return (f"{day}  {a:%H:%M}-{b:%H:%M}  "
                f"{self.minutes:>5.0f} min  {self.source}{kwh}")


def off_peak_windows(start: datetime, horizon_hours: int) -> list[Slot]:
    """The guaranteed 23:30-05:30 local windows overlapping the horizon.

    Built in local time then converted, so BST/GMT transitions are handled
    by zoneinfo rather than by arithmetic.
    """
    local_start = start.astimezone(LOCAL_TZ)
    finish = start + timedelta(hours=horizon_hours)
    slots: list[Slot] = []

    # Look back a day so a window we are already inside is included.
    day = (local_start - timedelta(days=1)).date()
    for _ in range(horizon_hours // 24 + 3):
        begin = datetime.combine(day, OFF_PEAK_START, tzinfo=LOCAL_TZ)
        end = datetime.combine(day + timedelta(days=1), OFF_PEAK_END,
                               tzinfo=LOCAL_TZ)
        slot = Slot(begin.astimezone(timezone.utc),
                    end.astimezone(timezone.utc), "off-peak")
        if slot.end > start and slot.start < finish:
            slots.append(slot)
        day += timedelta(days=1)
    return slots


def merge(slots: list[Slot]) -> list[Slot]:
    """Union overlapping or touching periods.

    A dispatch slot butting onto the off-peak window becomes one period, so
    the controller issues one command rather than two adjacent ones.
    """
    if not slots:
        return []
    ordered = sorted(slots, key=lambda s: s.start)
    merged = [ordered[0]]
    for slot in ordered[1:]:
        last = merged[-1]
        if slot.start <= last.end:
            source = last.source
            if slot.source != last.source:
                source = "off-peak+dispatch"
            merged[-1] = Slot(
                last.start,
                max(last.end, slot.end),
                source,
                (last.kwh or 0) + (slot.kwh or 0) or None,
            )
        else:
            merged.append(slot)
    return merged


def subtract(slots: list[Slot], minus: list[Slot]) -> list[Slot]:
    """Remove the `minus` periods from `slots`, keeping the fragments.

    Used to isolate the *bonus* time: a dispatch that runs 23:00-00:30 is
    only worth commanding for its first half hour, because the guaranteed
    window takes over at 23:30 and the plant's own EMS already handles that.
    A slot can be split in two if a minus period sits inside it.
    """
    out: list[Slot] = []
    for slot in slots:
        fragments = [(slot.start, slot.end)]
        for gap in minus:
            nxt = []
            for start, end in fragments:
                if gap.end <= start or gap.start >= end:
                    nxt.append((start, end))       # no overlap
                    continue
                if gap.start > start:
                    nxt.append((start, gap.start))  # keep the head
                if gap.end < end:
                    nxt.append((gap.end, end))      # keep the tail
            fragments = nxt
        for start, end in fragments:
            if end > start:
                out.append(Slot(start, end, slot.source, slot.kwh))
    return out


def parse_dispatches(payload: dict) -> list[Slot]:
    """Turn the GraphQL response into Slots.

    Only planned dispatches matter for scheduling; completed ones are
    history and are ignored here.
    """
    slots = []
    for entry in payload.get("plannedDispatches") or []:
        try:
            start = _parse_dt(entry["startDt"])
            end = _parse_dt(entry["endDt"])
        except (KeyError, ValueError) as exc:
            raise OctopusError(f"Unparseable dispatch {entry!r}: {exc}")
        kwh = entry.get("deltaKwh")
        slots.append(Slot(start, end, "dispatch",
                          abs(float(kwh)) if kwh else None))
    return slots


def _parse_dt(value: str) -> datetime:
    """Kraken returns ISO8601; sometimes with 'Z', sometimes an offset."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class OctopusClient:
    def __init__(self, api_key: str, account_number: str,
                 timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.account_number = account_number
        self.timeout = timeout
        self._last_payload: dict | None = None

    def _post(self, query: str, variables: dict,
              token: str | None = None) -> dict:
        body = json.dumps({"query": query,
                           "variables": variables}).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        request = urllib.request.Request(
            GRAPHQL_URL, data=body, headers=headers, method="POST")
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise OctopusError(
                f"HTTP {exc.code} from Octopus: {exc.read()[:400]!r}")
        except urllib.error.URLError as exc:
            raise OctopusError(f"Cannot reach Octopus: {exc.reason}")
        if payload.get("errors"):
            raise OctopusError(f"GraphQL error: {payload['errors']}")
        return payload["data"]

    def token(self, force: bool = False) -> str:
        """Cached Kraken token. The API key is exchanged, not sent per call."""
        if not force and TOKEN_CACHE.exists():
            try:
                cached = json.loads(TOKEN_CACHE.read_text())
                obtained = datetime.fromisoformat(cached["obtained_at"])
                if datetime.now(timezone.utc) - obtained < TOKEN_TTL:
                    return cached["token"]
            except (OSError, ValueError, KeyError):
                pass

        data = self._post(AUTH_MUTATION,
                          {"input": {"APIKey": self.api_key}})
        token = data["obtainKrakenToken"]["token"]
        try:
            TOKEN_CACHE.write_text(json.dumps({
                "token": token,
                "obtained_at": datetime.now(timezone.utc).isoformat(),
            }))
            TOKEN_CACHE.chmod(0o600)
        except OSError:
            pass  # caching is an optimisation, not a requirement
        return token

    def dispatches(self) -> dict:
        try:
            return self._post(DISPATCH_QUERY,
                              {"accountNumber": self.account_number},
                              token=self.token())
        except OctopusError as exc:
            if "401" in str(exc) or "authentic" in str(exc).lower():
                # Token may have expired early; one retry with a fresh one.
                return self._post(DISPATCH_QUERY,
                                  {"accountNumber": self.account_number},
                                  token=self.token(force=True))
            raise

    def recent_completion(self, now: datetime | None = None,
                          within_minutes: float = 40.0) -> Slot | None:
        """The most recent dispatch that ACTUALLY RAN, if it was recent.

        A dispatch reaches completedDispatches only if the car drew during
        it, so this is evidence the car is charging -- from Octopus's own
        data, with no charger API and no assumption about how the charger is
        wired. It lags by up to a half hour, which is why it is evidence
        rather than proof.
        """
        now = now or datetime.now(timezone.utc)
        best = None
        for entry in (self._last_payload or {}).get("completedDispatches") or []:
            try:
                end = _parse_dt(entry["endDt"])
            except (KeyError, ValueError):
                continue
            age = (now - end).total_seconds() / 60
            if -5 <= age <= within_minutes:
                slot = Slot(_parse_dt(entry["startDt"]), end, "completed")
                if best is None or slot.end > best.end:
                    best = slot
        return best

    def cheap_slots(self, horizon_hours: int = 24,
                    now: datetime | None = None,
                    bonus_only: bool = False) -> list[Slot]:
        """Cheap periods over the horizon.

        By default: the guaranteed windows merged with the bonus dispatch
        slots. With bonus_only, the guaranteed windows are *subtracted*
        instead, leaving just the extra time Octopus has released — which is
        the only part a plant already running Sigen AI needs help with.
        """
        now = now or datetime.now(timezone.utc)
        guaranteed = off_peak_windows(now, horizon_hours)
        payload = self.dispatches()
        self._last_payload = payload
        dispatches = parse_dispatches(payload)
        if bonus_only:
            slots = merge(subtract(dispatches, guaranteed))
        else:
            slots = merge(guaranteed + dispatches)
        horizon_end = now + timedelta(hours=horizon_hours)
        return [s for s in slots if s.end > now and s.start < horizon_end]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--raw", action="store_true",
                        help="dump the raw GraphQL payload")
    parser.add_argument("--bonus-only", action="store_true",
                        help="only the extra slots outside 23:30-05:30")
    args = parser.parse_args()

    try:
        env = load_env()
        client = OctopusClient(env["OCTOPUS_API_KEY"],
                               env["OCTOPUS_ACCOUNT_NUMBER"])
        if args.raw:
            print(json.dumps(client.dispatches(), indent=2))
            return 0

        now = datetime.now(timezone.utc)
        slots = client.cheap_slots(args.hours, now, args.bonus_only)
    except (OctopusError, ConfigError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"\nERROR: missing {exc} in .env", file=sys.stderr)
        return 1

    print(f"\nCheap import periods, next {args.hours}h "
          f"(local time, now {now.astimezone(LOCAL_TZ):%a %d %b %H:%M})")
    print("=" * 68)
    if not slots:
        print("  none")
    for slot in slots:
        marker = " <- NOW" if slot.contains(now) else ""
        print(f"  {slot}{marker}")

    total = sum(s.minutes for s in slots)
    bonus = sum(s.minutes for s in slots if "dispatch" in s.source)
    print("=" * 68)
    print(f"  {total:.0f} min cheap, of which {bonus:.0f} min "
          f"involves bonus dispatch slots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
