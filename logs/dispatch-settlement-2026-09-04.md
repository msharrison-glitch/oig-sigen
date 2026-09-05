# Does a dispatch settle at off-peak after the car finishes?

Recorded live 2026-09-04 22:35:02 BST. The question: IOG bills
COMPLETED dispatches at off-peak. The car finished mid-dispatch and
Octopus did NOT withdraw the slot. We kept charging the house battery on
it. Tomorrow's completedDispatches says whether that period settled at
4.49p or 29.757p.

## What Octopus said, at 22:28 BST
```
plannedDispatches[0]    21:01Z -> 02:00Z   = 22:01 -> 03:00 BST   deltaKwh -28
completedDispatches[0]  20:00Z -> 20:30Z   = 21:00 -> 21:30 BST   deltaKwh -1
```
Note the plan still says -28 kWh to 03:00, an hour after the car was
reported Complete. Their plan had not caught up with the car.

## What the car did
- 21:16:51  Zappi Boosting, 7.17 kW  (Octopus released it ~90 s into the slot)
- 22:2x     Zappi Complete, 0.0 kW, 7.54 kWh added this session
- car was at 93% when plugged in, so it took very little

## What we did
- agent kept charging the HOUSE battery on the still-planned dispatch
- contested period: car finished ~22:25 -> slot end 23:29:30
- battery self-limits at the 95% ceiling around 23:19 regardless

## Live trace
```
2026-09-04 21:15:26 INFO    SCHEDULE + added   21:15 -> 23:30 [dispatch]
2026-09-04 21:15:33 INFO    Zappi: Paused, EV connected, not charging, 0.00 kW
2026-09-04 21:15:33 INFO    SOC 7.5%  grid -0.00 kW  ESS -0.42 kW  enable=0 mode=0 limit=unset work=1(Sigen AI) -> idle (dispatch unconfirmed)
2026-09-04 21:16:12 INFO    Zappi: Paused, EV connected, not charging, 0.00 kW
2026-09-04 21:16:51 INFO    Zappi: Boosting, charging, 7.17 kW
2026-09-04 21:16:56 INFO    SOC 7.4%  grid +0.00 kW  ESS -0.41 kW  enable=0 mode=0 limit=unset work=1(Sigen AI) -> STARTED charging
2026-09-04 21:20:13 INFO    SOC 9.7%  grid +11.31 kW  ESS +10.25 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 21:20:55 INFO    SOC 10.3%  grid +11.31 kW  ESS +10.25 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 21:25:01 INFO    SOC 13.3%  grid +11.29 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 21:25:39 INFO    SOC 13.8%  grid +11.29 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 21:30:25 INFO    SOC 17.3%  grid +11.28 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 21:35:09 INFO    SOC 20.8%  grid +11.27 kW  ESS +10.23 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 21:35:49 INFO    SOC 21.2%  grid +11.27 kW  ESS +10.23 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:00:26 INFO    SOC 39.1%  grid +11.52 kW  ESS +10.23 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:02:57 INFO    SCHEDULE + added   22:01 -> 23:30 [dispatch]
2026-09-04 22:02:57 WARNING SCHEDULE - WITHDRAWN 21:15 -> 23:30 [dispatch] -- +47.9 min into it
2026-09-04 22:03:04 INFO    Zappi: Boosting, charging, 6.45 kW
2026-09-04 22:05:04 INFO    SOC 42.4%  grid +11.62 kW  ESS +10.23 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:05:42 INFO    SOC 42.8%  grid +11.34 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:10:25 INFO    SOC 46.2%  grid +11.40 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:15:05 INFO    SOC 49.6%  grid +11.89 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:15:43 INFO    SOC 50.0%  grid +11.90 kW  ESS +10.25 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:20:28 INFO    SOC 53.4%  grid +11.34 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:25:11 INFO    SOC 56.8%  grid +11.94 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:25:50 INFO    SOC 57.2%  grid +11.96 kW  ESS +10.24 kW  enable=0 mode=0 limit=unset work=9 -> holding
2026-09-04 22:30:34 INFO    SOC 60.6%  grid +11.18 kW  ESS +10.25 kW  enable=0 mode=0 limit=unset work=9 -> holding
```

## RESULT

Octopus DID withdraw the dispatch when the car finished -- but not
immediately. Car complete ~22:25, withdrawal seen 22:43:55, agent
released 22:44:07 and restored mode 1 (confirmed locally, 30003 = 1).

So the exposure from a car finishing mid-slot is the WITHDRAWAL LATENCY,
about 18 minutes here, not the remainder of the slot. We charged roughly
3.2 kWh in that window; everything before it had the car drawing.

This means the 'once confirmed, stays confirmed' rule does NOT need a
car-finished exception. Octopus's withdrawal is the signal, the agent
already watches for it every 30 s, and it arrived. Adding a Zappi
'Complete' check would buy ~18 minutes of exposure at the cost of a
second, independent reason to release -- and a car that merely pauses
must not trigger it. Not worth it on this evidence.

Banked: SOC 7.4% -> 70.0% = ~15.1 kWh, at 4.49p if the dispatch settles
as expected.

STILL TO CHECK, 2026-09-05: does completedDispatches show the
full 22:01 -> 22:44, or is it truncated to when the car actually drew
(~22:25)? That decides whether the last 19 minutes billed at 4.49p or
29.757p -- about GBP 0.81 of difference.
