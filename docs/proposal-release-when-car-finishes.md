# Proposal: release when the car finishes, not when Octopus notices

Status: **proposed**, not built. Written 2026-09-05 after one night's
settlement data. Wants a second night before it goes near the plant.

## The problem, measured

2026-09-04. Car plugged in at 93 %, so it took very little and finished early,
in the middle of a 2¼-hour dispatch.

    21:16:51  Zappi Boosting 7.17 kW      agent confirms, starts charging
    ~22:25    Zappi Complete, 0.0 kW      car done, 7.54 kWh added
    22:43:55  SCHEDULE - WITHDRAWN        Octopus pulls the slot, 18 min later
    22:44:07  agent releases              12 s after seeing the withdrawal

Next morning, `completedDispatches`, converted to local time:

    21:00 -> 21:30   deltaKwh -1
    21:30 -> 22:00   deltaKwh -3
    22:00 -> 22:30   deltaKwh -2      <- last one
    plannedDispatches: 0

**Completions stop at 22:30.** We charged until 22:44. That 14 minutes, about
2.6 kWh at 11.3 kW, sits outside every completed dispatch -- so on the rule
this project already relies on, it billed at peak. Roughly 78p where it should
have been 12p.

## Why the current design does not catch it

`reconcile.py` confirms a slot once and then stops asking:

    if key not in self._confirmed:
        ...
        self._confirmed.add(key)

That is deliberate, and still right for what it was written for. A car that
cycles must not make us acquire and release repeatedly at 20-30 s of actuation
each way. But it means a car that has **finished** is indistinguishable from
one that is merely **paused** -- we stopped looking.

Octopus does eventually withdraw, and the agent does catch that within 30 s.
The gap is not the withdrawal, it is the 18 minutes before it.

## The rule

Keep consulting the charger after confirmation, but act only on a *terminal*
state, and only at a half-hour boundary.

1. While holding a dispatch, if the charger reports the car **finished**
   -- Zappi status 5, `Complete`, already decoded in `zappi.py` and distinct
   from 1 `Paused` -- record the time.
2. Release at the **next half-hour boundary** after that, less `RELEASE_LEAD`.
3. `Paused`, `Waiting`, or simply 0 kW must NOT trigger it. Only completion.
4. If the car resumes before the boundary, cancel the pending release.

## Why the boundary, and not immediately

The completed records prove it, and they were in the data from the start.
Every one is aligned to the half-hour:

    20:00:00Z -> 20:30:00Z   deltaKwh -1
    20:30:00Z -> 21:00:00Z   deltaKwh -3
    21:00:00Z -> 21:30:00Z   deltaKwh -2   <- car stopped ~21:25Z, inside this

The last runs to 21:30:00Z -- the full settlement period -- although the car
stopped about five minutes before it ended. Octopus did not truncate it to the
moment charging stopped. The settlement period is the unit, so the argument is
just "Octopus bills in half-hours" and needs nothing about charger-status
semantics.


Completions are half-hourly, and the half-hour containing the car's last
charging *does* complete -- 22:00-22:30 settled even though the car stopped at
22:25. Releasing the moment the car finishes would forfeit up to 30 minutes of
charging you are entitled to at 4.49p. Releasing at the boundary captures
exactly what is covered and nothing that is not.

That distinction is the whole value of having waited for the settlement data.
Without it the obvious design -- "stop when the car stops" -- would have cost
more than it saved about half the time.

## Why the car finishes early here

**Corrected 2026-09-05.** An earlier version of this section blamed Octopus for
planning blind -- `deltaKwh -28` against a car that took 7.54 kWh -- and called
the gap structural. That was wrong, and it was my inference rather than the
owner's account. Octopus honours a 6-hour cheap-charging allowance, and this
owner deliberately requests the full six hours regardless of the car's state of
charge, because that is what generates the bonus slots this project exists to
catch. Octopus was doing exactly as asked.

What follows still holds, with a narrower claim. When you request more charging
than the car needs, the car finishes early and the planned dispatch outlives it.
On this install that is the *deliberate normal case*, not an occasional one, so
the gap opens on most sessions here.

Whether it generalises is unproven. Any owner whose car reaches its target
before the planned window ends meets the same thing -- which on a tariff built
around "set a target and let it plan" ought to be common -- but that is
reasoning, not evidence, and this project has been bitten by that difference
repeatedly.

Nothing in the chain knows the car's state of charge: Octopus integrates with
the charger, AC charging over Type 2 does not expose SOC to the EVSE, and there
is no SOC field in the myenergi payload. So neither the agent nor Octopus can
see the early finish coming. Only observe it.

Bluelink is the only thing that could supply the missing number -- there is no
vehicle registered with Octopus to ask -- and it is not available to us.

The European login flow is a browser-based OAuth2 journey through
`idpconnect-eu` that **requires solving a Google reCAPTCHA**, followed by a
device registration carrying an FCM `pushRegId` plus `ccsp-service-id`,
`ccsp-application-id` and a derived `Stamp` header. The maintained library has
open issues on that flow breaking. This is categorically harder than Sigen,
myenergi or Meross, all of which are plain signed HTTP and were reimplemented
here in a few hundred stdlib lines. It is also rate-limited to roughly 50 forced
refreshes a day, a forced refresh wakes the car and drains its 12 V battery, and
accounts get flagged for polling.

Which is academic anyway: the boundary rule solves the same problem by
observation, at no cost, with nothing new to hold.

## What it is worth

66p on the observed occasion. At 11.3 kW each minute past the boundary is
~5.6p at peak against ~0.8p off-peak, so the cost scales with how long Octopus
takes to notice -- 18 minutes here, and nothing bounds it. Multiply by "most
sessions" rather than "occasionally".

## Risks, and what would make this wrong

**One night of data.** The claim that completions truncate at the boundary
rests on a single observation. If instead Octopus sometimes back-fills the
remainder, the whole proposal dissolves. Confirm on a second occasion first.

**It cannot help `--require-ev` owners.** That path infers the car from
`completedDispatches`, which lag by up to half an hour -- so a non-Zappi owner
has no timely completion signal at all. This rule is specific to hosts that can
ask the charger directly, which today means `--require-zappi`. Worth stating
plainly rather than implying the fix is universal.

**A car that completes then resumes.** Preconditioning or a top-up could do
this. Mitigated by the boundary delay plus cancel-on-resume, but it is the
failure mode to test hardest.

**Charger semantics.** Status 5 is Zappi's. Another charger's "complete" may
mean something looser. Do not generalise the numeric status.

## Alternatives considered

**Do nothing.** The withdrawal always arrives; exposure is bounded by Octopus's
latency. Costs ~66p a time and recurs. Cheap to fix, so this loses -- but it is
the honest baseline and the change must beat it by more than its own risk.

**Release immediately on completion.** Simpler, no boundary arithmetic. Forfeits
up to 30 minutes of covered charging, so it can cost more than it saves.

**Re-poll `completedDispatches` and release when the current half-hour is not
covered.** Charger-agnostic and uses the authoritative signal -- but it lags up
to 30 minutes, which is the very window we are trying to close.

## Sketch

- `zappi.py` already returns `status`; expose completion as a first-class flag
  rather than a string comparison at the call site.
- `reconcile.py`: `self._car_finished_at`. Set while `_holding_dispatch` and
  the charger reports completion; cleared if it resumes. Compute the release
  point as the next half-hour boundary after it, less `RELEASE_LEAD`.
- Tests: completes mid-slot -> releases at the boundary, not before; pauses ->
  does not release; completes then resumes -> pending release cancelled;
  completes at 22:29 -> releases at 22:30, not 23:00.

## Before building

Wait for a second night where the car finishes mid-dispatch, and check
`completedDispatches` the next morning. If completions truncate again, build
it. If they do not, delete this file and keep the current behaviour.
