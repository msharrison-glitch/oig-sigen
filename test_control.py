#!/usr/bin/env python3
"""
Offline test of the lease logic against the mock plant.

The thing that must never fail is release. This checks the write ORDER on
acquire, that release disables Remote EMS, that release still happens when
the body raises, and that the deadman and release paths are idempotent.

    python3 test_control.py
"""

from __future__ import annotations

import control
import registers as R
from sigen import SigenClient
from test_mock import HOLDING, MockPlant

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<52} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def fresh_client(plant: MockPlant) -> SigenClient:
    client = SigenClient("127.0.0.1", port=plant.port)
    client._throttle = lambda: None  # type: ignore[method-assign]
    client.connect()
    return client


def main() -> int:
    plant = MockPlant()
    plant.start()

    # Mirror the real plant's observed starting state.
    HOLDING[40029] = 0
    HOLDING[40031] = 0
    control.STATE_FILE = control.Path("/tmp/.lease-test.json")
    control.clear_state()

    print(f"\nMock plant on 127.0.0.1:{plant.port}\n")

    print("Acquire")
    client = fresh_client(plant)
    plant.writes.clear()
    lease = control.Lease(client)
    lease.acquire(R.EMS_COMMAND_CHARGE_GRID_FIRST, 5.0, minutes=10)

    order = [addr for addr, _ in plant.writes]
    check("write order is limit -> mode -> enable",
          order, [40032, 40031, 40029])
    check("charge limit written as 5.0 kW raw 5000",
          HOLDING[40032] << 16 | HOLDING[40033], 5000)
    check("mode latched to 3", HOLDING[40031], 3)
    check("remote EMS enabled", HOLDING[40029], 1)
    check("state file written", control.read_state() is not None, True)
    check("original mode captured as 0", lease.original_mode, 0)

    print("\nRelease")
    plant.writes.clear()
    lease.release()
    check("remote EMS disabled", HOLDING[40029], 0)
    check("original mode restored", HOLDING[40031], 0)
    check("state file cleared", control.read_state(), None)

    print("\nRelease is idempotent")
    plant.writes.clear()
    lease.release()
    check("second release writes nothing", plant.writes, [])

    print("\nDischarge uses the discharge limit register, not the charge one")
    clientD = fresh_client(plant)
    plant.writes.clear()
    leaseD = control.Lease(clientD)
    leaseD.acquire(R.EMS_COMMAND_DISCHARGE_ESS_FIRST, 4.0, minutes=5)
    check("wrote 40034 not 40032",
          [addr for addr, _ in plant.writes], [40034, 40031, 40029])
    check("discharge limit raw 4000",
          HOLDING[40034] << 16 | HOLDING[40035], 4000)
    check("mode latched to 6", HOLDING[40031], 6)
    leaseD.release()
    check("released after discharge", HOLDING[40029], 0)

    print("\nRelease still runs when the body raises")
    client2 = fresh_client(plant)
    lease2 = control.Lease(client2)
    try:
        lease2.acquire(R.EMS_STANDBY, None, minutes=5)
        check("standby latched mid-lease", HOLDING[40031], 1)
        raise RuntimeError("simulated crash")
    except RuntimeError:
        pass
    finally:
        lease2.release()
    check("remote EMS disabled after exception", HOLDING[40029], 0)

    print("\nDeadman")
    client3 = fresh_client(plant)
    lease3 = control.Lease(client3)
    lease3.acquire(R.EMS_COMMAND_CHARGE_GRID_FIRST, 3.0, minutes=10)
    lease3._released = True          # simulate the holder being killed
    check("lease still held on plant", HOLDING[40029], 1)

    control.cmd_deadman(client3)
    check("deadman leaves an unexpired lease alone", HOLDING[40029], 1)

    state = control.read_state()
    state["expires_at"] = "2020-01-01T00:00:00+00:00"
    control.write_state(state)
    control.cmd_deadman(client3)
    check("deadman releases an expired lease", HOLDING[40029], 0)
    check("deadman cleared state file", control.read_state(), None)

    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. The release path holds under crash and kill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
