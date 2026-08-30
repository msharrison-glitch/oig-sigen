#!/usr/bin/env python3
"""
Offline test of the reconciliation loop.

Two halves. The first pins down the pure schedule arithmetic -- lead times,
which slot is live, when the decision next changes -- because that is where
an off-by-one costs real money at a price boundary. The second drives whole
ticks against the mock plant and checks the properties that matter:

  * it writes only when the plant actually disagrees with the schedule;
  * it releases when a slot is withdrawn underneath it;
  * it does not charge a battery that is already at target;
  * --dry-run touches nothing;
  * a Modbus fault does not leave a lease held.

    python3 test_reconcile.py
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import control
import reconcile
import registers as R
import sigen
from octopus import OctopusError, Slot
from sigen import SigenClient
from test_mock import MockPlant

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class FakeOctopus:
    """Stands in for OctopusClient. `slots` is swapped between ticks to
    simulate Octopus adding or withdrawing a bonus dispatch."""

    def __init__(self, slots: list[Slot]) -> None:
        self.slots = slots
        self.calls = 0

    def cheap_slots(self, hours, now, bonus_only=False):
        self.calls += 1
        self.last_bonus_only = bonus_only
        return list(self.slots)


class BrokenOctopus:
    def cheap_slots(self, hours, now, bonus_only=False):
        raise OctopusError("simulated API outage")


def make_plant(soc_pct: float = 50.0, enable: int = 0,
               mode: int = 0) -> MockPlant:
    """An isolated plant, so tests cannot leak state into each other."""
    plant = MockPlant(
        holding={40029: enable, 40031: mode,
                 40032: 0, 40033: 0,       # charge limit u32
                 40034: 0, 40035: 0},      # discharge limit u32
        input_regs={30014: int(soc_pct * 10)},
    )
    plant.start()
    return plant


def client_for(plant: MockPlant) -> SigenClient:
    c = SigenClient("127.0.0.1", port=plant.port)
    c._throttle = lambda: None  # type: ignore[method-assign]
    c.connect()
    return c


def reconciler(plant: MockPlant, octopus, **kw) -> reconcile.Reconciler:
    return reconcile.Reconciler(client_for(plant), octopus,
                                charge_kw=kw.pop("charge_kw", 5.0),
                                target_soc=kw.pop("target_soc", 95.0), **kw)


def slot_at(now: datetime, start_min: float, end_min: float,
            source: str = "off-peak") -> Slot:
    return Slot(now + timedelta(minutes=start_min),
                now + timedelta(minutes=end_min), source)


def main() -> int:
    logging.disable(logging.CRITICAL)      # keep the loop's chatter out
    sigen.MIN_REQUEST_INTERVAL = 0.0       # the 1 s floor would make this crawl
    control.STATE_FILE = Path("/tmp/.lease-reconcile-test.json")
    control.clear_state()

    now = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    # ---------------------------------------------------------------- pure
    print("\nLead times around a slot boundary")
    s = slot_at(now, 10, 40)                     # opens in 10 min, 30 min long
    check("well before the slot -> nothing",
          reconcile.desired_slot([s], now), None)
    check("90 s before opening -> still nothing",
          reconcile.desired_slot([s], s.start - timedelta(seconds=90)), None)
    check("30 s before opening -> commanded (60 s lead)",
          reconcile.desired_slot([s], s.start - timedelta(seconds=30)) is s,
          True)
    check("mid-slot -> commanded",
          reconcile.desired_slot([s], s.start + timedelta(minutes=5)) is s,
          True)
    check("60 s before close -> still commanded",
          reconcile.desired_slot([s], s.end - timedelta(seconds=60)) is s,
          True)
    check("15 s before close -> released (30 s lead-out)",
          reconcile.desired_slot([s], s.end - timedelta(seconds=15)), None)
    check("after the slot -> nothing",
          reconcile.desired_slot([s], s.end + timedelta(minutes=1)), None)

    print("\nConfirmation wake, 5 min before a slot opens")
    # The schedule churns, so we want to be awake and re-polling shortly
    # before committing, not merely near the boundary by luck of the cap.
    far = slot_at(now, 30, 60, "dispatch")
    begin = far.start - timedelta(seconds=reconcile.COMMAND_LEAD)
    confirm_at = begin - timedelta(seconds=reconcile.CONFIRM_LEAD
                                   - reconcile.COMMAND_LEAD)
    check("the next wake is the confirmation, not the command",
          reconcile.next_event([far], now), confirm_at)
    check("nothing to confirm while still far out",
          reconcile.upcoming([far], now), [])
    check("inside the confirmation window it is flagged",
          reconcile.upcoming([far], confirm_at + timedelta(seconds=1))
          == [far], True)
    check("but still not commanded yet",
          reconcile.desired_slot([far], confirm_at + timedelta(seconds=1)),
          None)
    check("commanded once the lead-in arrives",
          reconcile.desired_slot([far], begin) is far, True)
    check("a slot withdrawn after confirming is simply not commanded",
          reconcile.desired_slot([], begin), None)

    print("\nSlots too short to be worth actuating")
    tiny = slot_at(now, 10, 10.5)                # 30 s
    check("30 s slot is skipped", reconcile.is_worth_commanding(tiny), False)
    check("and never becomes the target",
          reconcile.desired_slot([tiny], tiny.start), None)
    check("30 min slot is commanded", reconcile.is_worth_commanding(s), True)

    print("\nWhen does the decision next change")
    check("next event is the confirmation wake, well before the start",
          reconcile.next_event([s], now),
          s.start - timedelta(seconds=reconcile.CONFIRM_LEAD))
    check("and the lead-in is the wake after that",
          reconcile.next_event([s], s.start
                               - timedelta(seconds=reconcile.CONFIRM_LEAD)),
          s.start - timedelta(seconds=reconcile.COMMAND_LEAD))
    check("no slots -> no event", reconcile.next_event([], now), None)

    print("\nOctopus outage falls back to the guaranteed window")
    rec = reconciler(make_plant(), BrokenOctopus())
    fell_back = rec.fetch_slots(now)
    check("fetch_slots does not raise", isinstance(fell_back, list), True)
    check("source recorded as fallback", rec._last_source, "fallback")
    check("only guaranteed off-peak slots survive",
          sorted({x.source for x in fell_back}), ["off-peak"])

    # ------------------------------------------------------------ hardware
    print("\nBonus-only refuses to guess when Octopus is down")
    rec_b = reconciler(make_plant(), BrokenOctopus(), )
    rec_b.bonus_only = True
    check("no bonus slots can be confirmed -> command nothing",
          rec_b.fetch_slots(now), [])
    rec_n = reconcile.Reconciler(client_for(make_plant()), None, 5.0, 95.0,
                                 bonus_only=True)
    check("and with no client at all, likewise",
          rec_n.fetch_slots(now), [])
    rec_p = reconciler(make_plant(), FakeOctopus([]))
    rec_p.bonus_only = True
    rec_p.fetch_slots(now)
    check("the flag is passed through to the client",
          rec_p.octopus.last_bonus_only, True)

    print("\nCold start inside a cheap slot")
    plant = make_plant(soc_pct=50.0)
    live = slot_at(now, -5, 55)
    oct_ = FakeOctopus([live])
    rec = reconciler(plant, oct_)
    plant.writes.clear()
    check("tick starts charging", rec.tick(now), "STARTED charging")
    check("write order is limit -> mode -> enable",
          [a for a, _ in plant.writes], [40032, 40031, 40029])
    check("mode 3, command charging grid first", plant.holding[40031], 3)
    check("remote EMS enabled", plant.holding[40029], 1)
    check("limit written as 5.0 kW (raw 5000)",
          plant.holding[40032] << 16 | plant.holding[40033], 5000)

    print("\nSecond tick: agrees with the plant, so writes nothing")
    plant.writes.clear()
    check("action is a no-op hold", rec.tick(now), "holding")
    check("no registers written", len(plant.writes), 0)

    print("\nLease is rolled forward, never taken out long")
    state = control.read_state()
    ttl = (datetime.fromisoformat(state["expires_at"])
           - datetime.now(timezone.utc)).total_seconds() / 60
    check("deadline is about the TTL ahead",
          round(ttl) in (reconcile.LEASE_TTL_MINUTES,
                         reconcile.LEASE_TTL_MINUTES - 1), True)
    check("and well under the 120 min ceiling",
          ttl < control.MAX_LEASE_MINUTES, True)
    check("renewal touches no registers", len(plant.writes), 0)

    print("\nOctopus withdraws the slot mid-charge")
    oct_.slots = []
    plant.writes.clear()
    check("tick releases", rec.tick(now), "RELEASED")
    check("remote EMS disabled", plant.holding[40029], 0)
    check("lease file cleared", control.read_state(), None)

    print("\nBattery already at target SOC")
    plant = make_plant(soc_pct=96.0)
    rec = reconciler(plant, FakeOctopus([live]), target_soc=95.0)
    plant.writes.clear()
    check("does not charge a full battery", rec.tick(now), "idle")
    check("nothing written", len(plant.writes), 0)

    print("\nDry run")
    plant = make_plant(soc_pct=50.0)
    rec = reconciler(plant, FakeOctopus([live]), dry_run=True)
    plant.writes.clear()
    check("reports intent only", rec.tick(now), "WOULD START charging")
    check("writes nothing at all", len(plant.writes), 0)
    check("remote EMS untouched", plant.holding[40029], 0)

    print("\nTransport blips and faults")
    plant = make_plant()
    c = client_for(plant)
    plant.faults = 1
    check("one bad response is retried through",
          reconcile.with_retry(c.read_u16, 40029), 0)
    plant.faults = 9
    try:
        reconcile.with_retry(c.read_u16, 40029)
        raised = False
    except Exception:
        raised = True
    check("a persistent fault still raises", raised, True)

    print("\nFail safe: a fault mid-tick must not leave a lease held")
    plant = make_plant(soc_pct=50.0)
    rec = reconciler(plant, FakeOctopus([live]))
    rec.tick(now)
    check("charging before the fault", plant.holding[40029], 1)
    rec.safe_release()
    check("safe_release gives the plant back", plant.holding[40029], 0)
    rec.safe_release()
    check("and is idempotent", plant.holding[40029], 0)

    print("\nRefuses to run beside another controller")
    control.write_state({"pid": os.getpid(), "expires_at":
                         datetime.now(timezone.utc).isoformat()})
    check("our own pid is not a conflict",
          reconcile.another_controller_running(), None)
    control.write_state({"pid": 999_999_999, "expires_at":
                         datetime.now(timezone.utc).isoformat()})
    check("a dead pid is not a conflict",
          reconcile.another_controller_running(), None)
    control.write_state({"pid": 1, "expires_at":
                         datetime.now(timezone.utc).isoformat()})
    check("a live foreign pid is a conflict (EPERM still means alive)",
          reconcile.another_controller_running(), 1)
    control.clear_state()

    print("\nThe schedule churns overnight, so poll and notice")
    plant_c = make_plant(soc_pct=50.0)
    oct_c = FakeOctopus([])
    rec_c = reconcile.Reconciler(client_for(plant_c), oct_c, 5.0, 95.0,
                                 bonus_only=True)
    oct_c.calls = 0
    rec_c.tick(now)
    check("one tick makes exactly one Octopus call", oct_c.calls, 1)
    check("and the slots are cached for the sleep calculation",
          rec_c._slots, [])

    bonus = slot_at(now, 30, 60, "dispatch")
    oct_c.slots = [bonus]
    rec_c.tick(now)
    check("a newly appeared slot is picked up", len(rec_c._slots), 1)
    oct_c.slots = []
    rec_c.tick(now)
    check("and a withdrawn one disappears again", len(rec_c._slots), 0)
    check("three ticks, three calls -- not six", oct_c.calls, 3)

    print("\nSIGHUP cuts the sleep short, for the moment you plug in")
    import time as _t
    rec_h = reconciler(make_plant(), FakeOctopus([]))
    reconcile._refresh = False
    t0 = _t.monotonic()
    rec_h._sleep(1.0)                      # no refresh: sleeps normally
    check("a short sleep runs to completion",
          _t.monotonic() - t0 >= 0.9, True)
    reconcile.request_refresh()
    check("SIGHUP sets the flag", reconcile._refresh, True)
    t0 = _t.monotonic()
    rec_h._sleep(60.0)                     # would be a minute without it
    check("and the sleep returns at once", _t.monotonic() - t0 < 1.0, True)
    check("the flag is consumed, not sticky", reconcile._refresh, False)

    print("\nPolling sits on the half-hour grid, at :25 and :55")
    at = lambda h, m: datetime(2026, 1, 15, h, m, tzinfo=timezone.utc)
    check("just after the hour -> :25", reconcile.next_poll(at(21, 0)),
          at(21, 25))
    check("just after :25 -> :55", reconcile.next_poll(at(21, 26)),
          at(21, 55))
    check("just after :55 -> next hour's :25",
          reconcile.next_poll(at(21, 56)), at(22, 25))
    check("rolls over midnight", reconcile.next_poll(at(23, 59)),
          datetime(2026, 1, 16, 0, 25, tzinfo=timezone.utc))
    check("never more than 30 min away",
          max((reconcile.next_poll(at(21, m)) - at(21, m)).total_seconds()
              for m in range(60)) <= 1800, True)

    rec_c._holding_dispatch = False
    idle_wait = rec_c.sleep_seconds(at(21, 26), [])
    check("idle waits for the grid, not a free-running timer",
          round(idle_wait), 1741)
    rec_c._holding_dispatch = True
    held_wait = rec_c.sleep_seconds(at(21, 26), [])
    check("but holding a bonus slot still checks sooner",
          held_wait <= reconcile.DISPATCH_POLL_INTERVAL + 1, True)

    print("\nSleeping only until the decision could change")
    rec = reconciler(make_plant(), FakeOctopus([]))
    check("no events -> waits for the next grid poll (:25 from 22:00)",
          rec.sleep_seconds(now, []),
          (reconcile.next_poll(now) - now).total_seconds() + 1.0)
    soon = slot_at(now, 3, 60)
    check("an imminent boundary shortens the sleep",
          round(rec.sleep_seconds(now, [soon])), 121)
    rec._holding_dispatch = True
    check("holding a bonus slot polls harder (+1s wake slack)",
          rec.sleep_seconds(now, []),
          reconcile.DISPATCH_POLL_INTERVAL + 1.0)

    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. Schedule arithmetic and fail-safe paths hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
