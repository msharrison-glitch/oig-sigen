#!/usr/bin/env python3
"""Does an inverter power cycle restore the operational mode after a
Remote EMS release? One experiment, run once, to answer one question.

    ANSWERED 2026-09-04: NO. Kept as the record of how, not as a thing to
    run again. A standby lease was taken and released, 30003 went 1 (Sigen
    AI) -> 0 (Maximum Self-Powered) as expected, the plant was fully powered
    off and back on, and 30003 still read 0 afterwards. The community's
    "the power switch resumes the previous mode" holds for using that switch
    INSTEAD of Remote EMS; it does not recover a mode Remote EMS has already
    reverted. The cloud restore stays.

    Do not re-run it to confirm that. It stops your plant to tell you
    something already in this docstring.

WHY THIS MATTERS
    Releasing Remote EMS always drops this plant to Self-Consumption, never
    back to Sigen AI. Today the only way to put the mode back is the
    unofficial Sigen cloud API, which needs the owner's mySigen password in
    plaintext. That is the single biggest obstacle to sharing this project,
    and it turns out the official developer/VPP API cannot help: its whole
    mode vocabulary is MSC / FFG / VPP / NBI, with no Sigen AI in it.

    The community reports that the inverter power switch -- register 40000 --
    "resumes back to the previously selected control mode", unlike a Remote
    EMS release. If that also holds for a mode that Remote EMS has ALREADY
    reverted, the cloud drops out of this project entirely.

    The prior is not good. That claim is about using the power switch
    INSTEAD of Remote EMS, which is a different situation. If the plant
    persists Self-Consumption at release, a power cycle restores exactly
    that. It only works if "Sigen AI" lives at the app level and Remote EMS
    merely masks it. Even odds, cheap to settle, and a clean negative is
    worth having.

WHAT IT DOES
    1. Pre-flight, read-only. Refuses to run if anything else holds the
       plant, if a cloud restore is outstanding, or if real power is moving.
    2. Baseline: 30003 (mode), 30578 (inverter running), SOC, grid, ESS.
    3. Takes a SHORT STANDBY lease and releases it, to trigger the revert.
       Standby, not charge or discharge: it is the least disruptive way to
       make the plant do the thing we are studying.
    4. Power cycle: 40000 = 0, wait for 30578 to fall, dwell, 40000 = 1,
       wait for 30578 to rise.
    5. Reads 30003 and reports which way it went.

WHAT IT WILL DO TO YOUR HOUSE
    Step 4 STOPS THE WHOLE PLANT for the dwell period. The battery stops,
    solar stops, and the house draws everything from the grid. Anything
    charging or exporting is interrupted. Do not run this during a cheap
    slot, during the evening peak, or unattended.

    40000 is WRITE-ONLY: we cannot read back what we commanded. The plant's
    running state is inferred from 30578 on inverter unit 1.

SAFETY
    The plant must never be left stopped. The start write is wired to every
    exit path -- normal, exception, SIGINT and SIGTERM -- the same discipline
    control.py uses for a lease, and for the same reason: the failure we
    cannot accept is walking away from a halted plant.

    Writes NOTHING without --commit. Default is a dry run that prints the
    plan and takes the readings.

    python3 experiments/mode_restore_power_cycle.py            # dry run
    python3 experiments/mode_restore_power_cycle.py --commit   # for real
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import control                                         # noqa: E402
import registers as R                                  # noqa: E402
import sigen                                           # noqa: E402

PLANT_START_STOP = 40000        # write-only, U16: 0 stop, 1 start

STANDBY_MINUTES = 2             # lease length, only long enough to revert
DWELL_SECONDS = 45              # how long the plant stays stopped

# 2026-09-04: the first version of this waited 90 s for the state to reach
# STANDBY, sat at SHUTDOWN the whole time -- which is the plant stopping
# correctly, not failing to -- gave up, fired a start into the middle of the
# shutdown where it was ignored, and left the plant halted. Stopping took
# about 100 s. Give it three minutes and treat SHUTDOWN as progress.
STATE_WAIT_SECONDS = 180
POWER_IDLE_KW = 1.5             # above this, something is happening: abort

_stopped = False                # the plant is halted and we owe it a start


def say(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def read_mode(client) -> int | None:
    try:
        return client.read_u16(R.EMS_WORK_MODE.address, holding=False)
    except Exception as exc:                            # noqa: BLE001
        say(f"  ! could not read 30003: {type(exc).__name__}")
        return None


def read_running(host: str) -> int | None:
    """Plant running state, 30051. The plant unit serves it; unit 1 is not
    needed, which the first version of this got wrong too."""
    try:
        with sigen.SigenClient(host) as c:
            return c.read_u16(R.PLANT_RUNNING_STATE.address, holding=False)
    except Exception as exc:                            # noqa: BLE001
        say(f"  ! could not read 30051: {type(exc).__name__}")
        return None


def wait_for_running(host: str, want: int) -> bool:
    """Wait for a settled state. SHUTDOWN is transitional -- it means the
    plant is doing as it was told, so it resets the patience rather than
    exhausting it."""
    deadline = time.time() + STATE_WAIT_SECONDS
    while time.time() < deadline:
        state = read_running(host)
        say(f"  30051 = {state} "
            f"({R.RUNNING_STATE_NAMES.get(state, '?')}), want "
            f"{R.RUNNING_STATE_NAMES.get(want, want)}")
        if state == want:
            return True
        if state == R.RUNNING_SHUTDOWN and want == R.RUNNING_STANDBY:
            deadline = max(deadline, time.time() + 60)
        time.sleep(5)
    return False


def start_plant(client, host: str, why: str) -> None:
    """Idempotent, and safe to call when we never stopped it."""
    global _stopped
    if not _stopped:
        return
    say(f"RESTARTING THE PLANT ({why})")
    # The write succeeding is NOT the plant starting. The first version set
    # _stopped = False as soon as write_u16 returned, so a start fired into
    # the middle of a shutdown -- and silently ignored -- disarmed the only
    # thing that would have tried again. Exactly the failure fixed in
    # sigencloud.py the day before, reproduced here in the script whose one
    # job was to never leave the plant halted. Clear it on the PLANT saying
    # it is running, and on nothing else.
    for attempt in range(1, 5):
        try:
            client.write_u16(PLANT_START_STOP, 1)
            say(f"  start command sent ({attempt})")
        except Exception as exc:                        # noqa: BLE001
            say(f"  ! start write {attempt} failed: {exc}")
            time.sleep(5)
            continue
        if wait_for_running(host, R.RUNNING_RUNNING):
            _stopped = False
            say("  plant is running again")
            return
        say("  did not come back yet -- sending the start again")
    say("  !!! THE PLANT IS STILL NOT RUNNING AFTER 4 ATTEMPTS.")
    say("  !!! START IT FROM THE mySigen APP NOW.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="actually write. Without this nothing is written.")
    ap.add_argument("--host", default=None)
    args = ap.parse_args()
    host = config.resolve_host(args.host)

    say(f"plant {host}   {'COMMIT -- WILL WRITE' if args.commit else 'dry run'}")

    # -- pre-flight, all read-only ---------------------------------------
    if control.STATE_FILE.exists():
        say(f"ABORT: {control.STATE_FILE.name} exists -- something holds a "
            "lease. Stop the agent first.")
        return 2
    try:
        import sigencloud
        if sigencloud.read_cloud_state() is not None:
            say("ABORT: .cloud-mode.json exists -- a restore is outstanding. "
                "Settle it before adding another variable.")
            return 2
    except ImportError:
        pass

    with sigen.SigenClient(host) as client:
        mode0 = read_mode(client)
        soc = R.read(client, R.ESS_SOC)
        grid = R.read(client, R.GRID_ACTIVE_POWER)
        ess = R.read(client, R.ESS_POWER)
        run0 = read_running(host)
        say(f"baseline: 30003={mode0} ({R.EMS_WORK_MODE_NAMES.get(mode0, '?')})"
            f"  30051={run0}  SOC={soc}%  grid={grid:+.2f} kW  ESS={ess:+.2f} kW")

        if mode0 != R.EMS_WORK_MODE_AI:
            say(f"ABORT: the plant is not on Sigen AI (30003={mode0}). The "
                "experiment asks whether AI survives; start from AI.")
            return 2
        if max(abs(grid or 0), abs(ess or 0)) > POWER_IDLE_KW:
            say(f"ABORT: {POWER_IDLE_KW} kW of headroom wanted, but real power "
                "is moving. Come back when the house is quiet.")
            return 2
        if run0 != R.RUNNING_RUNNING:
            say(f"ABORT: plant is not running (30051={run0}).")
            return 2

        if not args.commit:
            say("dry run: would take a 2 min STANDBY lease, release it, then")
            say(f"         write 40000=0, wait {DWELL_SECONDS}s, write 40000=1")
            say("         and re-read 30003. Nothing written. Re-run with "
                "--commit.")
            return 0

        signal.signal(signal.SIGINT,
                      lambda *_: (start_plant(client, host, "SIGINT"),
                                  sys.exit(130)))
        signal.signal(signal.SIGTERM,
                      lambda *_: (start_plant(client, host, "SIGTERM"),
                                  sys.exit(143)))
        try:
            # -- 1. provoke the revert ---------------------------------
            say("taking a STANDBY lease to provoke the revert")
            lease = control.Lease(client, log=lambda m: say(f"  {m}"))
            lease.acquire(R.EMS_STANDBY, None, STANDBY_MINUTES)
            time.sleep(20)                    # 18-31 s actuation
            lease.release()
            time.sleep(20)
            mode1 = read_mode(client)
            say(f"after release: 30003={mode1} "
                f"({R.EMS_WORK_MODE_NAMES.get(mode1, '?')})")
            if mode1 == R.EMS_WORK_MODE_AI:
                say("NOTE: the mode did NOT revert. Either 30003 does not "
                    "track, or the revert is not what we believe. Either way "
                    "the power cycle would prove nothing -- stopping here.")
                return 0

            # -- 2. the power cycle ------------------------------------
            global _stopped
            say("STOPPING THE PLANT (40000 = 0)")
            client.write_u16(PLANT_START_STOP, 0)
            _stopped = True
            if not wait_for_running(host, R.RUNNING_STANDBY):
                say("  30578 never reached 0 -- restarting and abandoning")
                return 1
            say(f"  stopped. dwelling {DWELL_SECONDS}s")
            time.sleep(DWELL_SECONDS)
            start_plant(client, host, "planned restart")

            # -- 3. the answer -----------------------------------------
            time.sleep(20)
            mode2 = read_mode(client)
            say(f"after power cycle: 30003={mode2} "
                f"({R.EMS_WORK_MODE_NAMES.get(mode2, '?')})")
            say("")
            if mode2 == R.EMS_WORK_MODE_AI:
                say("RESULT: YES -- the power cycle restored Sigen AI.")
                say("        The cloud API is no longer needed to restore "
                    "the mode.")
            else:
                say("RESULT: NO -- the mode did not come back.")
                say(f"        Still {mode2}. The cloud restore stays.")
                say("        Put the mode back in the app, or run:")
                say("          python3 sigencloud.py --set 'Sigen AI Mode'")
            return 0
        finally:
            start_plant(client, host, "cleanup")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
