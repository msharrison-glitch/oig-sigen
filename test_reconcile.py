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
    print("\nA planned dispatch is a forecast; the car drawing is proof")

    class FakeZappi:
        def __init__(self, charging, power=7.0):
            self.charging, self.power, self.calls = charging, power, 0
        def status(self):
            self.calls += 1
            return {"charging": self.charging, "power_kw": self.power,
                    "status": "Charging" if self.charging else "Paused",
                    "plug": "x"}

    class DeadZappi:
        def status(self):
            raise reconcile.ZappiError("unreachable")

    live = slot_at(now, -5, 55, "dispatch")

    # Car idle: the slot may never complete, so importing would bill at PEAK.
    plant_z = make_plant(soc_pct=40.0)
    rec_z = reconcile.Reconciler(client_for(plant_z), FakeOctopus([live]),
                                 5.0, 95.0, bonus_only=True,
                                 zappi=FakeZappi(charging=False, power=0.0))
    rec_z.confirm_dispatch = True
    plant_z.writes.clear()
    check("car not drawing -> do not charge", rec_z.tick(now), "idle")
    check("and nothing is written", len(plant_z.writes), 0)

    # Car drawing: Octopus has activated the dispatch, so it is really cheap.
    plant_y = make_plant(soc_pct=40.0)
    zap = FakeZappi(charging=True)
    rec_y = reconcile.Reconciler(client_for(plant_y), FakeOctopus([live]),
                                 5.0, 95.0, bonus_only=True, zappi=zap)
    rec_y.confirm_dispatch = True
    check("car drawing -> charge", rec_y.tick(now), "STARTED charging")
    check("mode 3 latched", plant_y.holding[40031], 3)

    # Once seen, the slot stays confirmed: a car that cycles must not make us
    # acquire and release every few minutes.
    zap.charging = False
    check("a mid-slot pause does not drop the lease",
          rec_y.tick(now), "holding")
    check("and the Zappi is not re-queried once confirmed", zap.calls, 1)

    # A car that starts mid-slot must still be caught: check again soon
    # rather than waiting for the next :25/:55 poll.
    plant_w = make_plant(soc_pct=40.0)
    zap_w = FakeZappi(charging=False, power=0.0)
    rec_w = reconcile.Reconciler(client_for(plant_w), FakeOctopus([live]),
                                 5.0, 95.0, bonus_only=True, zappi=zap_w)
    rec_w.confirm_dispatch = True
    check("declined while the car is idle", rec_w.tick(now), "idle")
    check("but flagged as worth watching",
          rec_w._awaiting_confirmation, True)
    check("so the next look is soon, not at :25",
          rec_w.sleep_seconds(now, [live]) <= reconcile.CONFIRM_POLL_INTERVAL
          + reconcile.POLL_JITTER_SECONDS + 1, True)
    zap_w.charging = True
    check("car starts mid-slot -> we ride the remainder",
          rec_w.tick(now), "STARTED charging")
    check("and stop watching once committed",
          rec_w._awaiting_confirmation, False)

    # Unreachable Zappi must fail closed, never open.
    plant_d = make_plant(soc_pct=40.0)
    rec_d = reconcile.Reconciler(client_for(plant_d), FakeOctopus([live]),
                                 5.0, 95.0, bonus_only=True,
                                 zappi=DeadZappi())
    rec_d.confirm_dispatch = True
    plant_d.writes.clear()
    check("unreachable Zappi -> do not charge", rec_d.tick(now), "idle")
    check("fails closed, writes nothing", len(plant_d.writes), 0)

    # No Zappi configured at all: unchanged behaviour, acts on the plan.
    plant_n = make_plant(soc_pct=40.0)
    rec_n = reconcile.Reconciler(client_for(plant_n), FakeOctopus([live]),
                                 5.0, 95.0, bonus_only=True)
    check("no Zappi configured -> old behaviour",
          rec_n.tick(now), "STARTED charging")

    print("\nWithdrawals record HOW FAR into the slot they landed")
    import logging as _lg, io as _io
    stream = _io.StringIO()
    handler = _lg.StreamHandler(stream)
    reconcile.log.addHandler(handler)
    _lg.disable(_lg.NOTSET)
    reconcile.log.setLevel(_lg.INFO)

    rec_o = reconciler(make_plant(), FakeOctopus([]))
    started = now - timedelta(minutes=4)
    doomed = Slot(started, started + timedelta(minutes=30), "dispatch")
    rec_o._note_changes([doomed])          # first poll: nothing to compare
    rec_o._note_changes([])                # gone
    logged = stream.getvalue()
    reconcile.log.removeHandler(handler)
    _lg.disable(_lg.CRITICAL)
    check("withdrawal is logged", "WITHDRAWN" in logged, True)
    check("with how far into the slot it was",
          "min into it" in logged, True)

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

    print("\nCharger-agnostic confirmation, from completed dispatches")
    # The plant's grid meter cannot be used: chargers are commonly wired
    # outside its CT so the battery does not chase the car. Octopus's own
    # completion record works whatever the charger is.
    live_e = slot_at(now, -5, 55, "dispatch")

    class OctopusWithCompletion(FakeOctopus):
        def __init__(self, slots, completed):
            super().__init__(slots)
            self.completed = completed
        def recent_completion(self, now=None, within_minutes=40.0):
            return self.completed

    def rec_completion(completed):
        pl = make_plant(soc_pct=40.0)
        r = reconcile.Reconciler(
            client_for(pl), OctopusWithCompletion([live_e], completed),
            5.0, 95.0, bonus_only=True)
        r.confirm_dispatch = True
        return pl, r

    pl1, r1 = rec_completion(None)
    check("nothing has completed -> car not charging -> decline",
          r1.tick(now), "idle")
    check("and nothing written", len(pl1.writes), 0)

    done = slot_at(now, -35, -5, "completed")
    pl2, r2 = rec_completion(done)
    check("a recent completion -> the car IS charging -> proceed",
          r2.tick(now), "STARTED charging")

    class BrokenCompletion(FakeOctopus):
        def recent_completion(self, now=None, within_minutes=40.0):
            raise OctopusError("api down")
    pl3 = make_plant(soc_pct=40.0)
    r3 = reconcile.Reconciler(client_for(pl3), BrokenCompletion([live_e]),
                              5.0, 95.0, bonus_only=True)
    r3.confirm_dispatch = True
    check("API failure fails closed", r3.tick(now), "idle")
    check("writes nothing", len(pl3.writes), 0)

    print("\nRestoring the owner's mode is retried until it succeeds")
    import sigencloud
    from pathlib import Path as _P2
    sigencloud.CLOUD_STATE = _P2("/tmp/.cloud-mode-restore-test.json")
    sigencloud.clear_cloud_state()

    class RestoreCloud:
        def __init__(self, mode=1, profile=-1, fail=0):
            self.mode, self.profile = mode, profile
            self.sets, self.fail = [], fail
        def current_mode(self):
            return {"currentMode": self.mode, "currentProfileId": self.profile}
        def set_mode(self, m, p=-1):
            if self.fail > 0:
                self.fail -= 1
                raise RuntimeError("cloud down")
            self.sets.append((m, p)); self.mode, self.profile = m, p
            return {"code": 0}
        def firmware_revert(self):
            # What the plant actually does when Remote EMS is released:
            # drops to Maximum Self-Powered regardless of what was selected.
            self.mode, self.profile = 0, -1

    live_r = slot_at(now, -5, 55, "dispatch")

    def modbus_rec(cloud, slots=(live_r,)):
        pl = make_plant(soc_pct=40.0)
        r = reconcile.Reconciler(client_for(pl), FakeOctopus(list(slots)),
                                 5.0, 95.0, bonus_only=True)
        r.cloud_restore_client = cloud
        r.restore_mode_via_cloud = True
        return pl, r

    # Whatever mode was set is what comes back -- including a custom profile.
    pl, rec_r = modbus_rec(RestoreCloud(mode=9, profile=9664))
    check("captures mode AND profile before the lease",
          (rec_r.tick(now), rec_r._mode_before_lease),
          ("STARTED charging", (9, 9664)))
    rec_r.octopus.slots = []
    rec_r.cloud_restore_client.firmware_revert()
    check("releases", rec_r.tick(now), "RELEASED")
    check("restores the custom profile, not a default",
          rec_r.cloud_restore_client.sets, [(9, 9664)])
    check("nothing outstanding", sigencloud.pending_restore(), None)

    # The restore must survive the agent dying, so it is recorded when the
    # lease is TAKEN -- not after a release that may never happen.
    sigencloud.clear_cloud_state()
    pl_k, rec_k = modbus_rec(RestoreCloud(mode=9, profile=9664))
    rec_k.tick(now)
    st = sigencloud.read_cloud_state()
    check("mode recorded the moment the lease is taken",
          (st["restore_mode"], st["restore_profile"]), (9, 9664))
    check("but not yet a DEBT -- the agent is alive and holding",
          sigencloud.pending_restore(), None)
    check("because the record has a live deadline",
          datetime.fromisoformat(st["expires_at"]) > datetime.now(timezone.utc),
          True)
    check("and it becomes a debt once nobody renews it",
          sigencloud.pending_restore(
              datetime.now(timezone.utc) + timedelta(hours=1)), (9, 9664))
    first_deadline = st["expires_at"]
    rec_k.tick(now)                       # still holding
    check("and the deadline rolls forward while it holds",
          sigencloud.read_cloud_state()["expires_at"] >= first_deadline, True)
    rec_k.octopus.slots = []
    rec_k.cloud_restore_client.firmware_revert()
    rec_k.tick(now)
    check("cleared once the mode is actually back",
          sigencloud.pending_restore(), None)
    sigencloud.clear_cloud_state()

    # A failed restore is a debt, retried, not a logged shrug.
    sigencloud.clear_cloud_state()
    # fails twice: once during the release, once on the retry after it
    pl2, rec_f = modbus_rec(RestoreCloud(mode=1, fail=2))
    rec_f.tick(now)
    rec_f.octopus.slots = []
    rec_f.cloud_restore_client.firmware_revert()
    rec_f.tick(now)
    owed = sigencloud.pending_restore()
    check("a failed restore is recorded as owed", owed, (1, -1))
    plant_writes = len(pl2.writes)
    check("and blocks another lease while outstanding",
          rec_f.tick(now), "restore outstanding")
    check("no further plant writes while owing", len(pl2.writes), plant_writes)
    rec_f.octopus.slots = [live_r]
    check("next tick pays the debt and resumes",
          rec_f.tick(now), "STARTED charging")
    check("debt cleared", sigencloud.pending_restore(), None)

    # If the plant is already on the right mode, do not write pointlessly.
    sigencloud.clear_cloud_state()
    sigencloud.record_pending_restore(1, -1)
    rc = RestoreCloud(mode=1)     # plant already back on 1: nothing to do
    _, rec_ok = modbus_rec(rc, slots=())
    rec_ok.tick(now)
    check("already correct -> no pointless write", rc.sets, [])
    check("and the debt is cleared", sigencloud.pending_restore(), None)
    sigencloud.clear_cloud_state()

    print("\nCloud actuation: switch a profile, never take a lease")
    import sigencloud
    from pathlib import Path as _P
    sigencloud.CLOUD_STATE = _P("/tmp/.cloud-mode-reconcile-test.json")
    sigencloud.clear_cloud_state()

    class FakeCloud:
        def __init__(self, mode=1):
            self.mode, self.profile, self.sets = mode, -1, []
        def current_mode(self):
            return {"currentMode": self.mode, "currentProfileId": self.profile}
        def set_mode(self, m, p=-1):
            self.sets.append((m, p)); self.mode, self.profile = m, p
            return {"code": 0}

    live_c = slot_at(now, -5, 55, "dispatch")
    plant_c2 = make_plant(soc_pct=40.0)
    fc = FakeCloud(mode=1)
    rec_c2 = reconcile.Reconciler(client_for(plant_c2), FakeOctopus([live_c]),
                                  5.0, 95.0, bonus_only=True, cloud=fc,
                                  charge_profile_id=9664)
    plant_c2.writes.clear()
    check("starts by selecting the profile", rec_c2.tick(now),
          "STARTED charging")
    check("switched to mode 9 with the profile", fc.sets, [(9, 9664)])
    check("NO modbus write -- no lease, nothing latched",
          len(plant_c2.writes), 0)
    check("40029 untouched", plant_c2.holding[40029], 0)

    st = sigencloud.read_cloud_state()
    check("recorded how to undo it BEFORE switching",
          st["restore_mode"], 1)
    check("with a deadline past the slot",
          datetime.fromisoformat(st["expires_at"]) > live_c.end, True)

    fc.sets.clear()
    check("second tick just holds", rec_c2.tick(now), "holding")
    check("without switching again", fc.sets, [])

    oct_c2 = rec_c2.octopus
    oct_c2.slots = []
    check("slot gone -> restore the previous mode",
          rec_c2.tick(now), "RELEASED")
    check("restored to what was there before", fc.sets, [(1, -1)])
    check("state cleared", sigencloud.read_cloud_state(), None)

    print("\nCloud actuation refuses to switch blind")
    class BlindCloud(FakeCloud):
        def current_mode(self): return {}
    plant_b = make_plant(soc_pct=40.0)
    bc = BlindCloud()
    rec_b2 = reconcile.Reconciler(client_for(plant_b), FakeOctopus([live_c]),
                                  5.0, 95.0, bonus_only=True, cloud=bc,
                                  charge_profile_id=9664)
    check("cannot read the mode -> will not switch",
          rec_b2.tick(now), "idle (mode unreadable)")
    check("and wrote nothing", bc.sets, [])

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

    print("\nJitter so many installations do not poll in lockstep")
    rec_j = reconciler(make_plant(), FakeOctopus([]))
    waits = {round(rec_j.sleep_seconds(now, []), 3) for _ in range(20)}
    check("successive sleeps differ", len(waits) > 1, True)
    check("never below the interval", min(waits) >= reconcile.BASE_POLL_INTERVAL,
          True)
    check("never more than the jitter above it",
          max(waits) <= reconcile.BASE_POLL_INTERVAL
          + reconcile.POLL_JITTER_SECONDS + 1, True)

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
    check("idle never waits longer than the base poll interval",
          idle_wait <= reconcile.BASE_POLL_INTERVAL
          + reconcile.POLL_JITTER_SECONDS + 1, True)
    rec_c._holding_dispatch = True
    held_wait = rec_c.sleep_seconds(at(21, 26), [])
    check("but holding a bonus slot still checks sooner",
          held_wait <= reconcile.DISPATCH_POLL_INTERVAL
          + reconcile.POLL_JITTER_SECONDS + 1, True)

    print("\nSleeping only until the decision could change")
    rec = reconciler(make_plant(), FakeOctopus([]))
    check("no events -> capped by the base poll, not the 25 min to :25",
          rec.sleep_seconds(now, []) <= reconcile.BASE_POLL_INTERVAL
          + reconcile.POLL_JITTER_SECONDS + 1, True)
    check("a new slot appearing is noticed within 5 min",
          reconcile.BASE_POLL_INTERVAL <= 300.0, True)
    soon = slot_at(now, 3, 60)
    wait = rec.sleep_seconds(now, [soon])
    check("an imminent boundary shortens the sleep",
          120 <= wait <= 121 + reconcile.POLL_JITTER_SECONDS, True)
    rec._holding_dispatch = True
    check("holding a bonus slot polls harder",
          rec.sleep_seconds(now, []) <= reconcile.DISPATCH_POLL_INTERVAL
          + reconcile.POLL_JITTER_SECONDS + 1, True)

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
