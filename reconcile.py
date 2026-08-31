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

  * **One second minimum between Modbus requests.** A tick costs ~4 s of
    wall clock in reads alone. The loop sleeps until the next moment a
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
import logging
import os
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import control
import registers as R
from config import ConfigError, load_env, resolve_host
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

# While charging on a bonus slot we poll harder: Octopus can withdraw one at
# short notice, and every minute we are slow to notice is imported at peak.
DISPATCH_POLL_INTERVAL = 120.0

# Dispatch data is half-hourly, so poll on that grid rather than on a free-
# running timer: at :25 and :55, five minutes before each boundary. That is a
# confirmation poll before anything can start, at 48 calls a day instead of
# 720 -- which matters for a tool other people will run against Octopus too.
# Event wakes still fire independently, so punctuality does not depend on it.
POLL_MINUTES = (25, 55)

# While a slot is live but the car has not yet started drawing, watch closely
# rather than waiting for the next grid poll. Observed 2026-08-30: the
# 23:00-23:30 dispatch was declined at 22:59 because the Zappi was Paused,
# the car began drawing shortly after the slot opened, and the dispatch
# completed -- so it was genuinely 4.49p and we sat out nearly all of it.
CONFIRM_POLL_INTERVAL = 120.0

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

    def is_charging_at(self, kw: float) -> bool:
        return (self.enable == 1
                and self.mode == R.EMS_COMMAND_CHARGE_GRID_FIRST
                and self.charge_limit_kw is not None
                and abs(self.charge_limit_kw - kw) <= LIMIT_TOLERANCE_KW)


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
                 zappi: "ZappiClient | None" = None) -> None:
        self.heartbeat_url = heartbeat_url
        self.site_token = site_token
        self.bonus_only = bonus_only
        self.zappi = zappi
        # Slots whose dispatch we have SEEN activate, by start time. Once the
        # car has drawn during a slot, Octopus has activated it and the whole
        # window bills off-peak -- so we keep charging for the rest of it even
        # if the car pauses. Without that, a car that cycles would have us
        # acquiring and releasing every few minutes at 20-30 s a time.
        self._confirmed: set[str] = set()
        # True when a slot is live but the car is not yet drawing: keep
        # watching, because it may start at any moment.
        self._awaiting_confirmation = False
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
            log.warning("SCHEDULE - WITHDRAWN %s -> %s [%s]",
                        datetime.fromisoformat(start).astimezone(LOCAL_TZ)
                        .strftime("%H:%M"),
                        datetime.fromisoformat(end).astimezone(LOCAL_TZ)
                        .strftime("%H:%M"), source)
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
        return PlantState(enable, mode,
                          soc if isinstance(soc, (int, float)) else None,
                          limit if isinstance(limit, (int, float)) else None)

    # -- outputs ---------------------------------------------------------

    def ensure_charging(self, slot: Slot, state: PlantState) -> str:
        if state.is_charging_at(self.charge_kw) and self.lease.held:
            self.lease.renew(LEASE_TTL_MINUTES)
            return "holding"
        if self.dry_run:
            return "WOULD START charging"
        # Covers both a cold start and a plant that drifted underneath us
        # (someone used the app, or it restarted). acquire() writes the state
        # file before any register, and orders limits -> mode -> enable.
        self.lease.acquire(R.EMS_COMMAND_CHARGE_GRID_FIRST,
                           self.charge_kw, LEASE_TTL_MINUTES)
        self.lease.renew(LEASE_TTL_MINUTES)
        return "STARTED charging"

    def ensure_released(self, state: PlantState) -> str:
        if self.lease.held:
            if self.dry_run:
                return "WOULD RELEASE"
            self.lease.release()
            self.lease = control.Lease(self.client, log=log.info)
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
        now = now or utcnow()
        slots = self.fetch_slots(now)
        self._slots = slots            # reused by sleep_seconds; do NOT refetch
        target = desired_slot(slots, now)
        state = self.read_plant()

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
        if target is not None and self.zappi is not None:
            key = target.start.isoformat()
            if key not in self._confirmed:
                if self._zappi_drawing():
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

        log.info("SOC %s  enable=%d mode=%d limit=%s -> %s%s",
                 f"{state.soc:.1f}%" if state.soc is not None else "?",
                 state.enable, state.mode,
                 f"{state.charge_limit_kw:.2f} kW"
                 if state.charge_limit_kw is not None else "unset",
                 action, reason)
        self.send_heartbeat(state, action)
        return action

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
        wake = next_poll(now)
        if self._holding_dispatch:
            wake = min(wake, now + timedelta(seconds=DISPATCH_POLL_INTERVAL))
        if self._awaiting_confirmation:
            wake = min(wake, now + timedelta(seconds=CONFIRM_POLL_INTERVAL))
        event = next_event(slots, now)
        if event is not None:
            wake = min(wake, event)
        # A second of slack, so we wake just after the boundary rather than
        # a hair before it and have to go round again.
        return max(1.0, (wake - now).total_seconds() + 1.0)

    def run(self) -> int:
        log.info("reconciler started (charge %.2f kW, target SOC %.1f%%, "
                 "lease TTL %d min%s)", self.charge_kw, self.target_soc,
                 LEASE_TTL_MINUTES, ", DRY RUN" if self.dry_run else "")
        if not self.dry_run:
            # Say this every start, because it is the one thing that makes
            # this unsuitable for some plants and it cannot be detected from
            # here -- the operational mode has no Modbus register.
            log.warning(
                "NOTE: releasing Remote EMS always returns this plant to "
                "SELF-CONSUMPTION, not to whatever mode was selected before. "
                "If your plant runs Self-Consumption anyway, that is a no-op. "
                "If it runs Sigen AI, every slot will cost you that setting "
                "until you restore it in the app.")
        while True:
            now = utcnow()
            try:
                self.tick(now)
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
    parser.add_argument("--require-zappi", action="store_true",
                        help="only charge once the Zappi is actually drawing, "
                             "proving Octopus activated the dispatch. Without "
                             "this the agent acts on a forecast that may "
                             "never complete, and would bill at peak")
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
                             zappi=zappi_client)

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
