#!/usr/bin/env python3
"""
Keep the SigenStor charging exactly when Octopus import is cheap.

Every tick this recomputes the desired schedule from octopus.cheap_slots(),
compares it to what the plant is actually doing, and writes only when the two
disagree. It is a reconciliation loop, not a scheduler: there is no queue of
pending actions to get out of step with reality, so a plant that changes
underneath us -- someone poking the app, a restart, a withdrawn dispatch
slot -- is corrected on the next pass rather than silently ignored.

Three constraints shape the whole design:

  * **No watchdog.** A latched charge command outlives the process that set
    it, straight into the peak rate. So this holds a control.Lease, and the
    lease is *short and rolled forward* -- LEASE_TTL_MINUTES, renewed every
    tick -- rather than taken out for the length of the slot. A loop that
    dies stops renewing, and the cron deadman releases it within the TTL.
    This is also how a six-hour off-peak window is covered without ever
    exceeding control.MAX_LEASE_MINUTES.

  * **Actuation takes 18-31 s.** Commands go out COMMAND_LEAD seconds before
    a slot opens and the release goes out RELEASE_LEAD seconds before it
    shuts, so the battery is at setpoint for the whole cheap period and is
    never still importing when the price steps back up.

  * **One second minimum between Modbus requests.** A tick costs ~6 s of
    wall clock in reads alone (six registers). The loop sleeps until the next moment a
    decision could actually change, so it is idle almost all the time.

If Octopus is unreachable the loop falls back to the *guaranteed* 23:30-05:30
window, which needs no API. That is deliberately conservative: a bonus
dispatch slot we cannot confirm is a slot we do not charge on.

Usage:
    python3 reconcile.py --dry-run          # log decisions, write nothing
    python3 reconcile.py                    # run the loop
    python3 reconcile.py --once             # one pass, then exit
    python3 reconcile.py --kw 5 --log-file /var/log/oig.log

Send SIGHUP to re-poll immediately -- useful the moment you plug the car in,
because that first dispatch starts between the scheduled polls:

    kill -HUP $(pgrep -f 'reconcile.py')
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import control
import registers as R
from config import ConfigError, load_env, poll_seconds, resolve_host
from octopus import (LOCAL_TZ, OctopusClient, OctopusError, Slot,
                     merge, off_peak_windows)
from sigen import ModbusError, SigenClient
from zappi import ZappiClient, ZappiError

log = logging.getLogger("reconcile")

# Set by SIGHUP. A flag rather than an exception on purpose: an exception
# raised from a signal handler could land in the middle of a register write,
# and nothing is allowed to interrupt the lease sequence.
_refresh = False

# Granularity of the interruptible sleep. Small enough that SIGHUP and
# SIGTERM feel immediate, large enough to stay idle.
SLEEP_CHUNK = 5.0

# Longest a tick may sleep. The schedule is also consulted for nearer events,
# so this is an upper bound, not a cadence.
POLL_INTERVAL = 300.0

# --- poll cadences -------------------------------------------------------
#
# Both are overridable from .env, because the trade -- how much peak-rate
# exposure you tolerate against how heavy a guest you are on Octopus's API --
# is a judgement, not a fact.
#
# IOG_POLL_CHARGING_SECONDS: used whenever a slot is LIVE, both while we are
# charging and while we are waiting for the car to start drawing. This is the
# one that matters. Octopus withdraws slots at short notice -- observed two
# minutes after charging began -- and every second between the withdrawal and
# our noticing is imported at the PEAK rate. At the 30 s default, worst-case
# exposure is about 45 s (30 s to notice, 5-25 s to release), which is short
# enough to say plainly: if a slot is withdrawn, charging stops within a
# minute. Going much below this buys little, because at that point most of
# the delay is the plant's own actuation latency rather than detection.
DISPATCH_POLL_INTERVAL = poll_seconds("IOG_POLL_CHARGING_SECONDS", 30.0)

# Two different things need catching, and they need different cadences.
#
# Slot BOUNDARIES are half-hourly, so a confirmation poll at :25 and :55 --
# five minutes before each -- catches them precisely and cheaply.
POLL_MINUTES = (25, 55)

# IOG_POLL_IDLE_SECONDS: used when nothing is happening. It governs how
# quickly a NEW slot is noticed. That matters because the first dispatch
# after plugging in starts within a few minutes of the plug going in and runs
# only to the next half-hour boundary -- plug in at 18:07 for an 18:07-18:30
# slot and a half-hourly poll leaves four usable minutes of it. No money is
# at risk here, only opportunity, which is why the default is looser than the
# charging one. 5 minutes is 288 calls a day: modest for a personal tool.
BASE_POLL_INTERVAL = poll_seconds("IOG_POLL_IDLE_SECONDS", 300.0)

# Everyone running this is on the same tariff, so their slots begin at the
# same moments and synchronised polling from many installations is exactly
# the pattern that gets an API rate-limited. A few seconds of jitter costs
# nothing and spreads the load.
POLL_JITTER_SECONDS = 5.0

# A live slot whose dispatch is not yet confirmed uses the same cadence as
# charging: it is the active case, we simply have not committed yet. Observed
# 2026-08-30: the 23:00-23:30 dispatch was declined at 22:59 because the car
# was Paused, it began drawing shortly after the slot opened, and the
# dispatch completed -- so it was genuinely 4.49p and we sat out nearly all
# of it. Reacting fast here is how that slot gets caught.
CONFIRM_POLL_INTERVAL = DISPATCH_POLL_INTERVAL

# Actuation is 18-31 s, so lead the opening boundary comfortably.
COMMAND_LEAD = 60.0

# Wake this long before a slot opens purely to re-confirm it still exists.
# The schedule churns, and a slot withdrawn between the confirmation and the
# command is one we never start actuating for -- 20-30 s of pointless writes
# and, worse, a charge command for a period that is no longer cheap.
CONFIRM_LEAD = 300.0
# Release is 5-15 s. Give the price boundary a wider berth than that: the
# cost of stopping 30 s early is ~0.04 kWh, the cost of stopping late is
# peak-rate import.
RELEASE_LEAD = 30.0

# Rolling lease. Must stay well under control.MAX_LEASE_MINUTES, and
# comfortably above the renewal cadence so a slow tick never lets it lapse.
LEASE_TTL_MINUTES = 15

# Below this there is no point actuating at all -- the command would barely
# have taken effect before it was withdrawn.
MIN_SLOT_SECONDS = 120.0

AGENT_VERSION = "1.0"

# Heartbeats are fire-and-forget. Short timeout, every failure swallowed: the
# watchdog exists to observe the plant, and an observer that can stall or
# crash the controller is worse than no observer at all.
HEARTBEAT_TIMEOUT = 5.0

# How long past a slot's end the cloud deadman waits before undoing a
# charge selection. Long enough that a late tick is not treated as a failure,
# short enough that a real failure is caught within a few minutes.
CLOUD_GRACE = 300.0

DEFAULT_CHARGE_KW = 5.0
SCHEDULE_HORIZON_HOURS = 24

# Charge limit is a U32 with gain 1000, so compare in kW with a little slack.
LIMIT_TOLERANCE_KW = 0.005


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stop_requested() -> bool:
    """Signals raise KeyboardInterrupt directly, so nothing to poll here;
    kept as a seam so the chunked sleep reads the same either way."""
    return False


def request_refresh(signum=None, _frame=None) -> None:
    """SIGHUP handler. Sets a flag; the sleep loop picks it up."""
    global _refresh
    _refresh = True


# --------------------------------------------------------------------------
# schedule -> intent   (pure functions; these are what the tests pin down)
# --------------------------------------------------------------------------

def effective_window(slot: Slot) -> tuple[datetime, datetime]:
    """The period we actually command, once lead times are applied."""
    return (slot.start - timedelta(seconds=COMMAND_LEAD),
            slot.end - timedelta(seconds=RELEASE_LEAD))


def is_worth_commanding(slot: Slot) -> bool:
    begin, finish = effective_window(slot)
    return (finish - begin).total_seconds() >= MIN_SLOT_SECONDS


def desired_slot(slots: list[Slot], now: datetime) -> Slot | None:
    """The slot we should be charging for right now, or None.

    Lead times are folded in here rather than at the call site so that the
    loop's notion of 'now is a charging moment' and its notion of 'when does
    that next change' cannot drift apart.
    """
    for slot in slots:
        if not is_worth_commanding(slot):
            continue
        begin, finish = effective_window(slot)
        if begin <= now < finish:
            return slot
    return None


def next_poll(now: datetime,
              minutes: tuple[int, ...] = POLL_MINUTES) -> datetime:
    """The next scheduled poll on the half-hour grid."""
    candidates = []
    for base in (now, now + timedelta(hours=1)):
        for minute in minutes:
            stamp = base.replace(minute=minute, second=0, microsecond=0)
            if stamp > now:
                candidates.append(stamp)
    return min(candidates)


def upcoming(slots: list[Slot], now: datetime) -> list[Slot]:
    """Slots whose commanded window opens within CONFIRM_LEAD.

    These are the ones worth re-confirming: close enough to matter, far
    enough out that a withdrawal still costs us nothing.
    """
    out = []
    for slot in slots:
        if not is_worth_commanding(slot):
            continue
        begin, _ = effective_window(slot)
        if now < begin <= now + timedelta(seconds=CONFIRM_LEAD):
            out.append(slot)
    return out


def next_event(slots: list[Slot], now: datetime) -> datetime | None:
    """The next instant at which desired_slot() could return something else.

    Sleeping to exactly this point is what keeps the loop both punctual and
    almost entirely idle.
    """
    edges = []
    for slot in slots:
        if not is_worth_commanding(slot):
            continue
        begin, finish = effective_window(slot)
        # The confirmation wake is a first-class event: we want to be awake
        # and re-polling before a slot opens, not merely close to it because
        # the poll cap happened to land there.
        for edge in (begin - timedelta(seconds=CONFIRM_LEAD - COMMAND_LEAD),
                     begin, finish):
            if edge > now:
                edges.append(edge)
    return min(edges) if edges else None


def fallback_slots(now: datetime, hours: int) -> list[Slot]:
    """Guaranteed off-peak windows only. Computable with no network."""
    horizon_end = now + timedelta(hours=hours)
    return [s for s in merge(off_peak_windows(now, hours))
            if s.end > now and s.start < horizon_end]


# --------------------------------------------------------------------------
# plant
# --------------------------------------------------------------------------

@dataclass
class PlantState:
    enable: int
    mode: int
    soc: float | None
    charge_limit_kw: float | None
    grid_kw: float | None = None      # positive = importing
    ess_kw: float | None = None       # positive = charging

    def is_charging_at(self, kw: float) -> bool:
        return (self.enable == 1
                and self.mode == R.EMS_COMMAND_CHARGE_GRID_FIRST
                and self.charge_limit_kw is not None
                and abs(self.charge_limit_kw - kw) <= LIMIT_TOLERANCE_KW)


def _kw(value: float | None) -> str:
    """Signed kW for the status line, or '?' when the read gave nothing.

    The sign is the whole point: grid positive means importing, ESS positive
    means charging, so the two together say where the energy is going.
    """
    return f"{value:+.2f} kW" if value is not None else "?"


def with_retry(fn, *args, attempts: int = 2, **kwargs):
    """One retry on a transport blip.

    A connection idle for five minutes may have been dropped by the plant,
    and SigenClient only finds out on the next call -- at which point it has
    already reconnected for us. Retrying once turns that into a non-event.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (ModbusError, OSError) as exc:
            last = exc
            if attempt + 1 < attempts:
                log.warning("modbus call failed (%s); retrying", exc)
                time.sleep(1.0)      # also honours the inter-request floor
    assert last is not None
    raise last


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

class Reconciler:
    def __init__(self, client: SigenClient, octopus: OctopusClient | None,
                 charge_kw: float, target_soc: float,
                 dry_run: bool = False, heartbeat_url: str | None = None,
                 site_token: str | None = None,
                 bonus_only: bool = False,
                 zappi: "ZappiClient | None" = None,
                 cloud=None, charge_profile_id: int | None = None) -> None:
        self.heartbeat_url = heartbeat_url
        self.site_token = site_token
        self.bonus_only = bonus_only
        self.zappi = zappi
        # Cloud actuation: switch the plant's own operational mode to a
        # pre-made charging profile instead of taking a Remote EMS lease.
        # Nothing latches, and releasing does not revert the work mode --
        # which is the whole reason this path exists.
        self.cloud = cloud
        self.charge_profile_id = charge_profile_id
        self.cloud_held = False
        # With Modbus actuation, the cloud client is used only to put the
        # operational mode back after a release -- the firmware always drops
        # it to Self-Consumption, and there is no Modbus register for it.
        self.restore_mode_via_cloud = False
        self._mode_before_lease: tuple[int, int] | None = None
        # Confirmation uses the Zappi API when configured (precise), else the
        # plant's own grid meter (works with any charger).
        self.confirm_dispatch = False
        # Slots whose dispatch we have SEEN activate, by start time. Once the
        # car has drawn during a slot, Octopus has activated it and the whole
        # window bills off-peak -- so we keep charging for the rest of it even
        # if the car pauses. Without that, a car that cycles would have us
        # acquiring and releasing every few minutes at 20-30 s a time.
        self._confirmed: set[str] = set()
        # True when a slot is live but the car is not yet drawing: keep
        # watching, because it may start at any moment.
        self._awaiting_confirmation = False
        # When we last handed control back, and therefore when the plant was
        # last dropped to Self-Consumption by the firmware. Reported in the
        # heartbeat so an owner running Sigen AI is TOLD, rather than finding
        # out the way this one did -- by the battery emptying to the grid.
        self._reverted_at: str | None = None
        self._slots: list[Slot] = []
        self._known: set[tuple[str, str, str]] | None = None
        self.client = client
        self.octopus = octopus
        self.charge_kw = charge_kw
        self.target_soc = target_soc
        self.dry_run = dry_run
        self.lease = control.Lease(client, log=log.info)
        self._last_source = "none"
        self._holding_dispatch = False

    # -- inputs ----------------------------------------------------------

    def _note_changes(self, slots: list[Slot]) -> None:
        """Log what moved since the last poll.

        Octopus revises the dispatch schedule through the night. A slot that
        quietly vanishes mid-charge is the expensive case, so say so out loud
        rather than just behaving differently.
        """
        current = {(s.start.isoformat(), s.end.isoformat(), s.source)
                   for s in slots}
        if self._known is None:            # first poll: nothing to compare
            self._known = current
            return
        for start, end, source in sorted(current - self._known):
            log.info("SCHEDULE + added   %s -> %s [%s]",
                     datetime.fromisoformat(start).astimezone(LOCAL_TZ)
                     .strftime("%H:%M"),
                     datetime.fromisoformat(end).astimezone(LOCAL_TZ)
                     .strftime("%H:%M"), source)
        for start, end, source in sorted(self._known - current):
            began = datetime.fromisoformat(start)
            # How far into the slot the withdrawal landed. Negative means it
            # was pulled before it ever started. Logged because the useful
            # question -- is a fixed charging delay worth its cost? -- turns
            # entirely on whether withdrawals cluster early, and one
            # observation is not evidence.
            offset = (utcnow() - began).total_seconds() / 60
            when = (f"{offset:+.1f} min into it" if offset >= 0
                    else f"{-offset:.1f} min before it started")
            log.warning("SCHEDULE - WITHDRAWN %s -> %s [%s] -- %s",
                        began.astimezone(LOCAL_TZ).strftime("%H:%M"),
                        datetime.fromisoformat(end).astimezone(LOCAL_TZ)
                        .strftime("%H:%M"), source, when)
        self._known = current

    def fetch_slots(self, now: datetime) -> list[Slot]:
        if self.octopus is None:
            if self.bonus_only:
                # Bonus slots exist only in the API. With no client there is
                # nothing to act on, and the guaranteed window is not ours.
                self._last_source = "no Octopus client, bonus-only"
                return []
            self._last_source = "off-peak only (no Octopus client)"
            return fallback_slots(now, SCHEDULE_HORIZON_HOURS)
        try:
            slots = self.octopus.cheap_slots(SCHEDULE_HORIZON_HOURS, now,
                                             self.bonus_only)
            self._last_source = "octopus"
            self._note_changes(slots)
            return slots
        except (OctopusError, OSError) as exc:
            # Conservative on purpose: we keep the window we can prove and
            # drop every bonus slot we cannot.
            if self.bonus_only:
                # There is no offline fallback for bonus slots: they are
                # knowable only from the API. Command nothing rather than
                # guess, and let the plant's own EMS carry on.
                log.warning("Octopus unreachable (%s) -- no bonus slots can "
                            "be confirmed, so commanding nothing", exc)
                self._last_source = "fallback (bonus-only: none)"
                return []
            log.warning("Octopus unreachable (%s) -- falling back to the "
                        "guaranteed off-peak window only", exc)
            self._last_source = "fallback"
            return fallback_slots(now, SCHEDULE_HORIZON_HOURS)

    def read_plant(self) -> PlantState:
        enable = with_retry(self.client.read_u16, R.REMOTE_EMS_ENABLE.address)
        mode = with_retry(self.client.read_u16, R.REMOTE_EMS_MODE.address)
        soc = with_retry(R.read, self.client, R.ESS_SOC)
        limit = with_retry(R.read, self.client, R.ESS_MAX_CHARGE_LIMIT)
        grid = self._telemetry(R.GRID_ACTIVE_POWER)
        ess = self._telemetry(R.ESS_POWER)
        return PlantState(enable, mode,
                          soc if isinstance(soc, (int, float)) else None,
                          limit if isinstance(limit, (int, float)) else None,
                          grid, ess)

    def _telemetry(self, reg):
        """Read a register we only log, never decide on.

        Deliberately swallows a failure. read_plant runs at the top of every
        tick, so letting a cosmetic read raise would mean a firmware that does
        not serve this address crashes the loop on every pass -- with a lease
        possibly held and nothing left running to release it. A missing log
        field is not worth that.
        """
        try:
            # attempts=1 deliberately. The failure this guards against is an
            # illegal-data-address, which is deterministic -- a retry cannot
            # succeed, it just costs two more throttled seconds per tick and
            # emits with_retry's WARNING, which is indistinguishable from the
            # real transport faults the fail-safe path depends on us noticing.
            value = with_retry(R.read, self.client, reg, attempts=1)
        except (ModbusError, OSError) as exc:
            log.debug("telemetry read of %d failed (%s)", reg.address, exc)
            return None
        return value if isinstance(value, (int, float)) else None

    # -- outputs ---------------------------------------------------------

    def _settle_pending_restore(self) -> bool:
        """Put back any mode we still owe. True when nothing is outstanding.

        Retried every tick rather than attempted once, because a restore that
        quietly failed leaves the plant on a mode the owner did not choose.
        The same record is what lets cron recover it if we die first.
        """
        import sigencloud
        owed = sigencloud.pending_restore()
        if owed is None:
            return True
        mode, profile = owed
        try:
            current = self.cloud_restore_client.current_mode()
            if current.get("currentMode") == mode and \
                    current.get("currentProfileId", -1) == profile:
                sigencloud.clear_cloud_state()   # already back; nothing owed
                self._reverted_at = None
                return True
            self.cloud_restore_client.set_mode(mode, profile)
            log.info("restored operational mode %s (profile %s)",
                     mode, profile)
            sigencloud.clear_cloud_state()
            self._reverted_at = None
            return True
        except Exception as exc:                  # noqa: BLE001
            log.error("still cannot restore operational mode %s (%s) -- the "
                      "plant is on a mode you did not choose. Retrying; or "
                      "run: python3 sigencloud.py --deadman", mode, exc)
            return False

    def _cloud_start(self, slot: Slot) -> str:
        """Select the charging profile, recording how to undo it first."""
        if self.cloud_held:
            self._cloud_record(slot)          # roll the deadline forward
            return "holding"
        if self.dry_run:
            return "WOULD START charging (cloud)"
        before = self.cloud.current_mode()
        restore_mode = before.get("currentMode")
        restore_profile = before.get("currentProfileId", -1)
        if restore_mode is None:
            log.error("cannot read the current mode; refusing to switch "
                      "without knowing how to switch back")
            return "idle (mode unreadable)"
        # State BEFORE the switch, exactly as the Modbus lease does: if we
        # die between here and the restore, the deadman still knows what to
        # put back.
        self._cloud_record(slot, restore_mode, restore_profile)
        self.cloud.set_mode(9, self.charge_profile_id)
        self.cloud_held = True
        log.info("cloud: selected charging profile %s (was mode %s)",
                 self.charge_profile_id, restore_mode)
        return "STARTED charging"

    def _cloud_record(self, slot: Slot, mode=None, profile=-1) -> None:
        import sigencloud
        existing = sigencloud.read_cloud_state() or {}
        # Grace past the slot so a tick that runs late does not trip the
        # deadman while we are still legitimately charging.
        deadline = slot.end + timedelta(seconds=CLOUD_GRACE)
        sigencloud.write_cloud_state({
            "restore_mode": existing.get("restore_mode", mode),
            "restore_profile": existing.get("restore_profile", profile),
            "expires_at": deadline.isoformat(),
            "slot_end": slot.end.isoformat(),
            "pid": os.getpid(),
        })

    def _cloud_stop(self) -> str:
        import sigencloud
        if not self.cloud_held:
            return "idle"
        if self.dry_run:
            return "WOULD RELEASE (cloud)"
        state = sigencloud.read_cloud_state() or {}
        restore = state.get("restore_mode", 1)
        profile = state.get("restore_profile", -1)
        try:
            self.cloud.set_mode(int(restore), int(profile))
            log.info("cloud: restored mode %s", restore)
        except Exception as exc:                  # noqa: BLE001
            # Leave the state file: the deadman must still be able to undo
            # this, and clearing it would hide the problem.
            log.error("cloud restore FAILED (%s) -- the plant may still be "
                      "charging. Run: python3 sigencloud.py --deadman", exc)
            return "RESTORE FAILED"
        sigencloud.clear_cloud_state()
        self.cloud_held = False
        return "RELEASED"

    def ensure_charging(self, slot: Slot, state: PlantState) -> str:
        if self.cloud is not None:
            return self._cloud_start(slot)
        if state.is_charging_at(self.charge_kw) and self.lease.held:
            self.lease.renew(LEASE_TTL_MINUTES)
            if self.restore_mode_via_cloud and self._mode_before_lease:
                # Roll the restore deadline forward too: alive and renewing,
                # the deadman stays out of it; dead, it expires and fires.
                import sigencloud
                sigencloud.record_pending_restore(
                    int(self._mode_before_lease[0]),
                    int(self._mode_before_lease[1]),
                    expires_in_seconds=LEASE_TTL_MINUTES * 60 + CLOUD_GRACE)
            return "holding"
        if self.dry_run:
            return "WOULD START charging"
        if self.restore_mode_via_cloud and self._mode_before_lease is None:
            # Capture what to put back BEFORE taking the lease. If we cannot
            # read it, take the lease anyway and warn: charging cheaply and
            # losing the mode beats not charging, but the owner should know.
            try:
                import sigencloud
                before = self.cloud_restore_client.current_mode()
                self._mode_before_lease = (before.get("currentMode"),
                                           before.get("currentProfileId", -1))
                # Persist it NOW, not after the release. Held only in memory,
                # this knowledge dies with the process -- and a dead agent is
                # exactly when the owner is least likely to notice their plant
                # sitting on Self-Consumption. Written here, the deadman can
                # put it back on our behalf.
                sigencloud.record_pending_restore(
                    int(self._mode_before_lease[0]),
                    int(self._mode_before_lease[1]),
                    expires_in_seconds=LEASE_TTL_MINUTES * 60 + CLOUD_GRACE)
                log.info("will restore operational mode %s after release",
                         self._mode_before_lease[0])
            except Exception as exc:              # noqa: BLE001
                log.warning("cannot read the operational mode (%s) -- the "
                            "release will revert it and NOT restore", exc)

        # Covers both a cold start and a plant that drifted underneath us
        # (someone used the app, or it restarted). acquire() writes the state
        # file before any register, and orders limits -> mode -> enable.
        self.lease.acquire(R.EMS_COMMAND_CHARGE_GRID_FIRST,
                           self.charge_kw, LEASE_TTL_MINUTES)
        self.lease.renew(LEASE_TTL_MINUTES)
        return "STARTED charging"

    def ensure_released(self, state: PlantState) -> str:
        if self.cloud is not None:
            return self._cloud_stop()
        if self.lease.held:
            if self.dry_run:
                return "WOULD RELEASE"
            self.lease.release()
            self.lease = control.Lease(self.client, log=log.info)
            if self.restore_mode_via_cloud and self._mode_before_lease:
                # The record already exists from when the lease was taken;
                # make it due NOW so it is a debt rather than a live note.
                import sigencloud
                mode, profile = self._mode_before_lease
                sigencloud.record_pending_restore(int(mode), int(profile))
                self._mode_before_lease = None
                if self._settle_pending_restore():
                    return "RELEASED"
            self._reverted_at = utcnow().isoformat()
            log.warning(
                "MODE REVERTED: releasing Remote EMS has returned this plant "
                "to SELF-CONSUMPTION. If you run Sigen AI, TOU or Feed-in, "
                "reset it in the mySigen app -- it will not come back on its "
                "own.")
            return "RELEASED"
        if state.enable == 1:
            # Not our lease, but Remote EMS is on and we believe it should
            # not be. Say so; do not fight whatever set it.
            log.warning("Remote EMS is enabled but no lease of ours is held "
                        "-- another controller may be active. Leaving it.")
            return "foreign lease, untouched"
        if not self.dry_run:
            # Nothing is held and nothing is enabled, so any lease file left
            # here is stale. Dry run stays hands-off even about that.
            control.clear_state()
        return "idle"

    # -- one pass --------------------------------------------------------

    def tick(self, now: datetime | None = None) -> str:
        # An explicit time is a test pinning the clock; leave it alone.
        fixed_clock = now is not None
        now = now or utcnow()
        if self.restore_mode_via_cloud and not self._settle_pending_restore():
            # We owe the owner a mode we have not managed to set. Taking
            # another lease would compound that, so do nothing but keep
            # trying -- there is no cheap slot worth leaving someone on a
            # mode they did not choose.
            return "restore outstanding"
        slots = self.fetch_slots(now)
        self._slots = slots            # reused by sleep_seconds; do NOT refetch
        state = self.read_plant()

        # Decide on the time it is NOW, not when the tick began. Fetching the
        # schedule and reading six registers at the 1 s Modbus floor costs
        # 7-8 s, and deciding on the stale timestamp meant we once held a
        # lease two seconds after the deadline we had just printed -- the
        # release then fell to the next poll and ran 40 s late, past the slot
        # boundary and into peak rate. RELEASE_LEAD exists precisely to stop
        # that, so it must be measured against the moment of the decision.
        if not fixed_clock:
            now = utcnow()
        target = desired_slot(slots, now)

        reason = ""
        if target is not None and state.soc is not None \
                and state.soc >= self.target_soc:
            log.info("inside a cheap slot but SOC is %.1f%% (target %.1f%%) "
                     "-- nothing to gain", state.soc, self.target_soc)
            target = None
            reason = " (battery at target)"

        # A planned dispatch is a forecast about the CAR, not a price
        # guarantee. If the car never draws, the dispatch never completes and
        # the period bills at PEAK -- so importing on the plan alone can buy
        # expensive electricity. Requiring the Zappi to be drawing turns the
        # forecast into an observation.
        self._awaiting_confirmation = False
        if target is not None and self.confirm_dispatch:
            key = target.start.isoformat()
            if key not in self._confirmed:
                drawing = (self._zappi_drawing() if self.zappi is not None
                           else self._ev_drawing(state))
                if drawing:
                    self._confirmed.add(key)
                    log.info("DISPATCH ACTIVE: the car is drawing, so this "
                             "slot is really off-peak -- proceeding")
                else:
                    log.info("waiting: slot %s is planned but the car is not "
                             "drawing, so it may never complete and would "
                             "bill at peak",
                             target.local()[0].strftime("%H:%M"))
                    target = None
                    reason = " (dispatch unconfirmed)"
                    # Keep watching: a car that starts five minutes in still
                    # leaves most of the slot worth having.
                    self._awaiting_confirmation = True

        self._holding_dispatch = bool(
            target is not None and "dispatch" in target.source)

        for soon in upcoming(slots, now):
            begin, _ = effective_window(soon)
            log.info("CONFIRMED %s [%s] still scheduled; commanding in %.0fs",
                     soon.local()[0].strftime("%H:%M"), soon.source,
                     (begin - now).total_seconds())

        if target is not None:
            begin, finish = effective_window(target)
            log.info("cheap now [%s]: %s until %s local",
                     target.source, self._last_source,
                     finish.astimezone(LOCAL_TZ).strftime("%H:%M:%S"))
            action = self.ensure_charging(target, state)
        else:
            action = self.ensure_released(state)

        log.info("SOC %s  grid %s  ESS %s  enable=%d mode=%d limit=%s -> %s%s",
                 f"{state.soc:.1f}%" if state.soc is not None else "?",
                 _kw(state.grid_kw), _kw(state.ess_kw),
                 state.enable, state.mode,
                 f"{state.charge_limit_kw:.2f} kW"
                 if state.charge_limit_kw is not None else "unset",
                 action, reason)
        self.send_heartbeat(state, action)
        return action

    def _ev_drawing(self, state: PlantState) -> bool:
        """Charger-agnostic confirmation, from Octopus's own records.

        The plant's grid meter is NOT usable for this: EV chargers are
        commonly wired outside the plant's CT precisely so the battery does
        not chase the car's load, and an agent that watched for a load it can
        never see would silently never charge.

        A dispatch reaches completedDispatches only if the car actually drew
        during it, so a completion in the last half hour is evidence the car
        is charging now. It lags -- hence evidence, not proof -- which is why
        the poll rate matters and why a slot stays confirmed once seen.
        """
        if self.octopus is None:
            return False
        try:
            done = self.octopus.recent_completion()
        except (OctopusError, OSError) as exc:
            log.warning("cannot check completed dispatches (%s) -- "
                        "unconfirmed", exc)
            return False
        if done is None:
            log.info("no dispatch has completed recently -- the car does not "
                     "appear to be charging")
            return False
        log.info("dispatch %s-%s completed: the car is taking charge",
                 done.local()[0].strftime("%H:%M"),
                 done.local()[1].strftime("%H:%M"))
        return True

    def _zappi_drawing(self) -> bool:
        """Is the car actually taking charge right now?

        Fails closed: an unreachable Zappi means we do NOT know the dispatch
        activated, and guessing wrong costs peak rate.
        """
        if self.zappi is None:
            return False
        try:
            state = self.zappi.status()
        except (ZappiError, OSError) as exc:
            log.warning("Zappi unreachable (%s) -- treating the dispatch as "
                        "unconfirmed", exc)
            return False
        if state is None:
            log.warning("no Zappi on the account -- dispatch unconfirmed")
            return False
        # state["charging"] now covers Boosting, which is what an Octopus
        # dispatch actually looks like. The power check stays as insurance
        # against status codes we have not seen yet.
        drawing = bool(state["charging"]) or (state["power_kw"] or 0) > 0.2
        log.info("Zappi: %s, %s, %.2f kW", state["status"], state["plug"],
                 state["power_kw"] or 0.0)
        return drawing

    def send_heartbeat(self, state: PlantState, action: str) -> bool:
        """Tell the off-box watchdog what we just saw. Never raises.

        The watchdog cannot command anything, so this is a one-way report.
        Everything is caught, including the unexpected: a controller that dies
        because a monitoring endpoint returned malformed JSON would be a
        spectacular own goal.
        """
        if not self.heartbeat_url or not self.site_token:
            return False
        lease = control.read_state()
        payload = {
            "lease_held": bool(self.lease.held),
            "lease_expires": (lease or {}).get("expires_at"),
            "soc": state.soc,
            "mode": state.mode,
            "enable": state.enable,
            "action": action,
            "slots_known": len(self._slots),
            "reverted_at": self._reverted_at,
            "agent_version": AGENT_VERSION,
        }
        request = urllib.request.Request(
            self.heartbeat_url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.site_token}"},
            method="POST")
        try:
            with urllib.request.urlopen(
                    request, timeout=HEARTBEAT_TIMEOUT) as response:
                return 200 <= response.status < 300
        except Exception as exc:                  # noqa: BLE001 - deliberate
            log.warning("heartbeat failed (%s) -- the plant is unaffected",
                        exc)
            return False

    def _sleep(self, seconds: float) -> None:
        """Sleep in chunks so SIGHUP can cut it short.

        The first dispatch after plugging in starts a few minutes after the
        plug goes in and runs to the next half-hour boundary, so it lands
        between scheduled polls. SIGHUP is how you say "I have just plugged
        in, look now" without waiting for :25 or :55.
        """
        global _refresh
        # Against the WALL CLOCK, not a countdown. If the host suspends -- a
        # closed laptop lid does exactly this -- the process freezes mid-sleep
        # and a countdown resumes as though no time passed, leaving the agent
        # hours behind the schedule it is meant to be reconciling. Comparing
        # to the clock means a suspend is noticed the instant we wake.
        deadline = utcnow() + timedelta(seconds=seconds)
        while not _stop_requested():
            now = utcnow()
            if now >= deadline:
                return
            if _refresh:
                _refresh = False
                log.info("SIGHUP -- re-polling now")
                return
            time.sleep(min(SLEEP_CHUNK,
                           (deadline - now).total_seconds()))
        # A suspend long enough to overshoot the deadline lands here with the
        # next tick due immediately, which is what we want.

    def sleep_seconds(self, now: datetime, slots: list[Slot]) -> float:
        # Base cadence is the half-hour grid. The exception is actively
        # holding a bonus slot: a withdrawal mid-charge costs real money, so
        # that case keeps its own faster check.
        wake = min(next_poll(now),
                   now + timedelta(seconds=BASE_POLL_INTERVAL))
        if self._holding_dispatch:
            wake = min(wake, now + timedelta(seconds=DISPATCH_POLL_INTERVAL))
        if self._awaiting_confirmation:
            wake = min(wake, now + timedelta(seconds=CONFIRM_POLL_INTERVAL))
        event = next_event(slots, now)
        if event is not None:
            wake = min(wake, event)
        # A second of slack, so we wake just after the boundary rather than
        # a hair before it and have to go round again. Plus a little jitter,
        # so many installations on the same tariff do not poll in lockstep.
        delay = (wake - now).total_seconds() + 1.0
        return max(1.0, delay + random.uniform(0, POLL_JITTER_SECONDS))

    def run(self) -> int:
        # Only announce what is actually in force. On the cloud path the rate
        # comes from the app profile and there is no lease, so printing
        # "charge 5.00 kW, lease TTL 15 min" would describe machinery that is
        # not running. The SOC ceiling is announced either way because it is
        # applied in tick(), above the split, so it genuinely governs both.
        dry = ", DRY RUN" if self.dry_run else ""
        if self.cloud is not None:
            log.info("reconciler started (charging via cloud profile %s at "
                     "whatever rate it is configured for, target SOC %.1f%%, "
                     "no lease%s)",
                     self.charge_profile_id, self.target_soc, dry)
        else:
            log.info("reconciler started (charge %.2f kW, target SOC %.1f%%, "
                     "lease TTL %d min%s)", self.charge_kw, self.target_soc,
                     LEASE_TTL_MINUTES, dry)
        if not self.dry_run and self.cloud is None:
            # Say this every start, because it is the one thing that makes
            # this unsuitable for some plants and it cannot be detected from
            # here -- the operational mode has no Modbus register.
            #
            # Not on the cloud path: it never enables Remote EMS at all
            # (40029 stays 0), so there is no release to revert the mode.
            log.warning(
                "NOTE: releasing Remote EMS always returns this plant to "
                "SELF-CONSUMPTION, not to whatever mode was selected before. "
                "If your plant runs Self-Consumption anyway, that is a no-op. "
                "If it runs Sigen AI, every slot will cost you that setting "
                "until you restore it in the app.")
        while True:
            now = utcnow()
            try:
                # No argument: tick reads the clock itself, twice -- once to
                # fetch, once to decide -- so the decision is not made on a
                # timestamp that is already six seconds old.
                self.tick()
                slots = self._slots      # tick() already polled; one call only
            except (ModbusError, OSError) as exc:
                # Fail safe: we would rather give the plant back and lose a
                # cheap slot than hold a charge command we cannot see.
                log.error("tick failed (%s) -- releasing and retrying", exc)
                self.safe_release()
                slots = []
            # The plant drops an idle TCP connection long before our next
            # 30-minute poll, so holding one open just means every tick opens
            # with a failed call and a retry. Observed overnight: 12 ticks,
            # 12 identical warnings. Close it deliberately and reconnect on
            # the next call, which SigenClient does for us.
            try:
                self.client.close()
            except OSError:
                pass
            delay = self.sleep_seconds(utcnow(), slots)
            log.debug("sleeping %.0fs", delay)
            before = utcnow()
            self._sleep(delay)
            overshoot = (utcnow() - before).total_seconds() - delay
            if overshoot > 60:
                log.warning("woke %.0fs later than intended -- the host was "
                            "probably suspended. Anything held would NOT have "
                            "been renewed or released while it slept.",
                            overshoot)

    def safe_release(self) -> None:
        """Release, swallowing failures. Called from the error path and from
        atexit, where raising would obscure the original problem."""
        try:
            if self.lease.held:
                self.lease.release()
        except (ModbusError, OSError) as exc:
            log.error("RELEASE FAILED (%s). The plant may still be under "
                      "Remote EMS control. Run: python3 control.py release",
                      exc)


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------

def another_controller_running() -> int | None:
    """A live pid in the lease file that is not us means two controllers."""
    state = control.read_state()
    if not state:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None          # stale pid, the process is gone
    except PermissionError:
        return pid           # alive, just not ours to signal
    except OSError:
        return None
    return pid


def setup_logging(path: str | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if path:
        handlers.append(logging.FileHandler(path))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?",
                        help="SigenStor address (default: SIGEN_HOST in .env)")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--kw", type=float, default=DEFAULT_CHARGE_KW,
                        help=f"charge power limit (default {DEFAULT_CHARGE_KW})")
    parser.add_argument("--target-soc", type=float,
                        default=control.CHARGE_SOC_CEILING,
                        help="stop charging at this SOC")
    parser.add_argument("--once", action="store_true",
                        help="one reconcile pass, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="log decisions but write no registers")
    parser.add_argument("--no-octopus", action="store_true",
                        help="ignore bonus slots; guaranteed window only")
    parser.add_argument("--via-cloud", action="store_true",
                        help="charge by selecting a cloud energy profile "
                             "instead of taking a Remote EMS lease. Avoids "
                             "the mode revert entirely; needs "
                             "--charge-profile and SIGEN_CLOUD_* in .env")
    parser.add_argument("--no-restore-mode", action="store_true",
                        help="do NOT put the operational mode back after a "
                             "Modbus release. Restoring happens by default "
                             "whenever SIGEN_CLOUD_* is configured, because "
                             "releasing Remote EMS always leaves the plant on "
                             "Maximum Self-Powered -- whatever you had "
                             "selected")
    parser.add_argument("--charge-profile", metavar="NAME",
                        help="name of the app profile that grid-charges, "
                             "e.g. 'OIG Charge'")
    parser.add_argument("--require-ev", action="store_true",
                        help="only charge once the car is actually drawing, "
                             "proving Octopus activated the dispatch. Without "
                             "it the agent acts on a forecast that may never "
                             "complete and would bill at peak. Works with any "
                             "charger, using Octopus's completed dispatches "
                             "-- but those lag, so it cannot confirm the "
                             "FIRST slot after you plug in. Use "
                             "--require-zappi instead if you have one")
    parser.add_argument("--require-zappi", action="store_true",
                        help="as --require-ev, but ask a myenergi Zappi "
                             "directly rather than inferring from house "
                             "load. More precise; myenergi only")
    parser.add_argument("--bonus-only", action="store_true",
                        help="command ONLY the extra slots outside "
                             "23:30-05:30. Recommended on a plant running "
                             "Sigen AI, which already handles the "
                             "guaranteed window")
    parser.add_argument("--heartbeat-url",
                        help="off-box watchdog, e.g. "
                             "https://host/v1/heartbeat")
    parser.add_argument("--site-token", help="bearer token for the watchdog")
    parser.add_argument("--log-file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.log_file, args.verbose)

    if args.kw <= 0:
        log.error("--kw must be positive")
        return 2
    if LEASE_TTL_MINUTES > control.MAX_LEASE_MINUTES:
        log.error("LEASE_TTL_MINUTES exceeds the %d min ceiling",
                  control.MAX_LEASE_MINUTES)
        return 2

    other = another_controller_running()
    if other is not None:
        log.error("pid %d already holds %s -- refusing to start a second "
                  "controller", other, control.STATE_FILE.name)
        return 2

    try:
        host = resolve_host(args.host)
        cloud_client = None
        restore_client = None
        profile_id = None
        if not args.via_cloud and not args.no_restore_mode:
            # On by default: a release always leaves the plant on a mode the
            # owner did not choose, so restoring is the norm and declining is
            # the deliberate act.
            import sigencloud
            try:
                restore_client = sigencloud.client_from_env()
            except ConfigError:
                log.warning(
                    "NO MODE RESTORE: releasing Remote EMS always leaves this "
                    "plant on Maximum Self-Powered, whatever you had "
                    "selected, and there is no Modbus register to undo it. "
                    "Set SIGEN_CLOUD_* in .env to have it put back "
                    "automatically, or pass --no-restore-mode to silence "
                    "this.")
        if args.via_cloud:
            import sigencloud
            if not args.charge_profile:
                log.error("--via-cloud needs --charge-profile NAME")
                return 2
            cloud_client = sigencloud.client_from_env()
            wanted = args.charge_profile.strip().lower()
            for m in (cloud_client.modes().get("energyProfileItems") or []):
                if str(m.get("name", "")).strip().lower() == wanted:
                    profile_id = int(m["profileId"])
                    break
            if profile_id is None:
                log.error("no cloud profile called %r -- run "
                          "'python3 sigencloud.py --list'", args.charge_profile)
                return 2
            log.info("cloud actuation: profile %r is id %s",
                     args.charge_profile, profile_id)
        zappi_client = None
        if args.require_zappi:
            import zappi as _z
            zappi_client = _z.client_from_env()
        octopus = None
        if not args.no_octopus:
            env = load_env()
            octopus = OctopusClient(env["OCTOPUS_API_KEY"],
                                    env["OCTOPUS_ACCOUNT_NUMBER"])
    except (ConfigError, KeyError) as exc:
        log.error("configuration: %s", exc)
        return 1

    try:
        with SigenClient(host, port=args.port) as client:
            rec = Reconciler(client, octopus, args.kw, args.target_soc,
                             dry_run=args.dry_run,
                             heartbeat_url=args.heartbeat_url,
                             site_token=args.site_token,
                             bonus_only=args.bonus_only,
                             zappi=zappi_client, cloud=cloud_client,
                             charge_profile_id=profile_id)
            rec.confirm_dispatch = bool(args.require_ev or args.require_zappi)
            if restore_client is not None:
                rec.cloud_restore_client = restore_client
                rec.restore_mode_via_cloud = True
                log.info("operational mode will be restored after each "
                         "release")

            def on_signal(signum, _frame):
                log.info("caught signal %d", signum)
                raise KeyboardInterrupt

            signal.signal(signal.SIGINT, on_signal)
            signal.signal(signal.SIGTERM, on_signal)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, request_refresh)
            atexit.register(rec.safe_release)

            try:
                if args.once:
                    rec.tick()
                    return 0
                return rec.run()
            except KeyboardInterrupt:
                log.info("interrupted -- releasing")
                return 0
            finally:
                rec.safe_release()
    except (ModbusError, OSError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
