# Proposal: shift the heat pump into bonus slots

Status: **read-only step DONE 2026-09-10; nothing built, nothing written to
the unit.** Originally written 2026-09-07 from the Onecta API, the Home
Assistant integration's source and the Homey app's Flow cards. It has since
been checked against the real plant with `daikin.py --raw`, and the measured
findings are in "What the unit actually exposes" below. Two of the guesses
were wrong, and both are corrected in place rather than quietly amended.

The heat pump is not in use for heating as of 2026-09-07. Heating season is
weeks away, which is the whole reason to answer this now rather than in
January.

## The idea

Same thesis as the rest of this project, different load.

An IOG bonus slot prices the **whole property** at 4.49p, not just the car.
The SigenStor cannot see those slots, which is why `reconcile.py` exists. A
Daikin Altherma cannot see them either, and it is the largest deferrable load
in the house after the EV. The guaranteed 23:30-05:30 window needs no software
at all -- the Daikin's own schedule handles that. Bonus slots are the only
part that needs a dynamic response, because they are unpredictable and
short-notice.

So: when a bonus slot opens, make the heat pump work harder; when it closes,
put it back. The agent already knows when those moments are, and already acts
on them for the battery.

## The economics, corrected

An earlier version of this reasoning -- stated in conversation on 2026-09-07,
before it was worked through -- claimed ASHP shifting was a *better* per-kWh
case than the battery arbitrage, "closer to the export spread outright". That
was wrong, and it was wrong in the direction that flatters the proposal.

The plant exports to Agile Outgoing at 13-24p. Holding heat delivered
constant:

    heat later, from the battery    battery exports 1 kWh less
                                    -> forgo 13-24p

    heat now, from a bonus slot     pay 4.49p, battery exports that kWh
                                    -> +13-24p revenue, -4.49p cost

    net gain                        8.5 - 19.5p per kWh of ELECTRICITY

That is the same order as the battery's ~8.4p round-trip margin, not better.

And it is per kWh of *electricity*. The figure per kWh of **heat** must be
read off the COP table in the next section, not obtained by dividing this one
by three: it is the difference between the two compressor rows -- 4.3-7.9p to
make that heat later from the battery, against 1.5p to make it during a bonus
slot -- so **2.8-6.4p per kWh of heat delivered**. Any estimate that quotes
the electricity spread against heat is inflated roughly threefold.

Realistic magnitude: near nil in September; **£10-25/month in deep winter**.
Note what that requires: at 2.8-6.4p per kWh of heat, £10-25/month means
**5-12 kWh of shifted heat every day** landing inside bonus slots. That is a
lot of bonus-slot minutes in a cold month, and it is the assumption most
likely to be optimistic. Worth a weekend of code. Not worth £400 of hardware.

## The decisive unknown: which lever keeps the compressor running

This is the load-bearing question, and it inverts the obvious answer.

Electricity and heat are not fungible. If DHW **Powerful mode** engages the
backup immersion heater -- as it does on many Altherma configurations -- it
delivers heat at COP ~1 instead of ~3. Marginal cost of 1 kWh of heat, with
COP figures as **assumptions to verify, not measurements**:

    Powerful mode in a bonus slot (COP 1)
        1.00 kWh @ 4.49p                              = 4.49p

    compressor later, from the battery (COP 3)
        0.33 kWh, forgoing export at 13-24p           = 4.3 - 7.9p

    compressor during a bonus slot (COP 3)
        0.33 kWh @ 4.49p                              = 1.5p

So Powerful mode is roughly a **wash** against simply letting the tank reheat
off the battery, and worse when export prices are low. The prize is the third
row, and the route to it is to **raise the DHW setpoint** during a slot --
which triggers a normal compressor reheat -- rather than to enable
`powerfulMode`.

The same logic ranks the space-heating lever first: `leavingWaterOffset` nudges
the weather-compensation curve and keeps the compressor running, so the full
spread applies. Shifting a setpoint hard makes the unit cycle; shifting the
curve makes it run harder at similar efficiency.

Ranking, **revised 2026-09-10 against the real unit**:

1. **`leavingWaterOffset` on `climateControl`** -- settable, -10..+10, step 1,
   currently +5. Shifts the weather curve, compressor keeps running, full
   spread applies. Confirmed available. Earns only in the heating season.
2. **`onOffMode` on `domesticHotWaterTank`** -- settable. Not in the original
   ranking at all, and it may be the better half of the opportunity: hot
   water is ~1,200 kWh/year against space heating's ~1,700, and unlike
   heating it runs ALL YEAR. Suppress the tank through expensive periods,
   release it during a bonus slot, and the reheat lands at 4.49p. Crude
   compared with a setpoint nudge, but it is what this unit exposes.
3. ~~`domesticHotWaterTemperature` setpoint~~ -- **not available.** Ranked
   second in the original on the assumption it was settable. It is
   `read-only` on this unit (value=50, min=30, max=75). The compressor-driven
   tank reheat this proposal wanted cannot be commanded directly.
4. `powerfulMode` -- settable, but still suspected of running the immersion at
   COP ~1, which the table above shows is barely better than doing nothing.
   Unmeasured. A clue that the tank can draw heavily: September 2025 shows
   380 kWh against 46-153 kWh in the months either side.

## The API

Official, documented, versioned, with scoped credentials -- a far better
foundation than `sigencloud.py`, which is unofficial and whose reference
implementation was deleted.

    GET   /v1/gateway-devices                     whole device tree

    PATCH /v1/gateway-devices/{deviceId}
            /management-points/{embeddedId}
            /characteristics/{dataPoint}

    body: {"value": <value>, "path": "<dataPointPath>"}

Bearer token, `Content-Type: application/json`. The `path` member is omitted
when the datapoint takes a bare value.

Space heating, management point `climateControl`:

    onOffMode                      "on" / "off"
    temperatureControl             path /operationModes/heating/setpoints/
                                        leavingWaterOffset
                                        (or roomTemperature, or
                                         leavingWaterTemperature, depending on
                                         how the installer configured it)

Hot water, management point `domesticHotWaterTank`:

    temperatureControl             path /operationModes/heating/setpoints/
                                        domesticHotWaterTemperature   (integer)
    powerfulMode                   "on" / "off"
    onOffMode                      "on" / "off"

Two constraints that shape the design:

- **200 requests/day per application.** Every response carries
  `X-RateLimit-Remaining-day`. This is the interesting one, and it favours us
  -- see below.
- **A GET immediately after a PATCH returns stale data.** The reference
  implementation waits 10 s. Same read-back-to-verify discipline as
  `set_mode_verified`.

## What the unit actually exposes

Measured 2026-09-10 with `daikin.py --raw`, one call out of 200. This replaces
the guesswork above; where the two disagree, this section is right.

    device      Altherma, type heating-wlan, online
    indoor      EDLA04E2V3        gateway BRP069A78 fw 4.1.0

    climateControl / climateControlMainZone
        controlMode          roomTemperature      read-only
        setpointMode         weatherDependent     read-only
        onOffMode            off                  SETTABLE
        leavingWaterOffset   5   (-10..10, step 1)  SETTABLE   <-- the lever
        roomTemperature      21  (12..30, step 0.5) SETTABLE
        sensors: leavingWater 17, outdoor 16, room 22.4

    domesticHotWaterTank
        setpointMode                 fixed            read-only
        heatupMode                   reheatSchedule   read-only
        domesticHotWaterTemperature  50 (30..75)      READ-ONLY  <-- not a lever
        powerfulMode                 off              SETTABLE
        onOffMode                    on               SETTABLE   <-- the lever
        sensors: tankTemperature 46

`weatherDependent` with a settable `leavingWaterOffset` is the best case for
space heating: the unit keeps running its own curve and we nudge it, which is
the same relationship this project has with Sigen AI.

### Consumption history comes free in the same payload

`consumptionData.electrical` carries `unit: "kWh"` and 24 monthly buckets --
**two calendar years, not a rolling window**. Index 0-11 is the previous year,
12-23 the current one; the proof is that the trailing entries are `null`
because those months have not happened. Getting this wrong makes heating look
like it peaks in September, which is how the mistake was caught.

                       2025      2026 (Jan-Sep)
    space heating      1684 kWh       1346 kWh
    hot water          1317 kWh        914 kWh

Heating is Nov-Apr only, peaking at 429 (Dec 2025) and 602 (Jan 2026). Hot
water runs every month of the year. 2026 is running well above 2025 -- 602 vs
378 in January -- so the shiftable quantity is larger than the 2025 column
suggests.

Two consequences:

- **The business case can be measured rather than estimated.** A representative
  winter month is ~430 kWh of electricity for heating; at the 8.5-19.5p spread,
  shifting 20-30% of it is roughly **GBP 12-18/month**, which lands in the
  lower half of the range this proposal originally guessed. It also
  cross-checks: 86 kWh/month is ~2.9 kWh/day of electricity, or ~8.7 kWh/day of
  heat at COP 3, inside the 5-12 kWh/day the estimate requires.
- **Hot water is not the sideshow it was treated as.** 1,200 kWh/year, spread
  across all twelve months, against heating's 1,700 concentrated in four or
  five. A DHW lever earns in July; a heating lever does not.

### Why 2025 is not the baseline: a solar diverter was in play

Context from the owner, 2026-09-10, and unrecoverable from the data alone: a
SolarEdge diverter used to send surplus solar to the hot water tank. It was
stopped shortly after the Sigen was installed, in favour of exporting instead.

Summer is the clean test, because a diverter only acts when there is surplus:

    DHW, Jun-Aug   2025:  52, 46, 54   (mean 51)
                   2026:  70, 67, 67   (mean 68)      +34%

So through summer 2025 roughly a third of the tank's load was not on the heat
pump at all, and the Daikin's own figures understate it. **Plan from the 2026
column.** The true current tank load is ~67 kWh/month in summer and 150-175 in
winter -- call it 1,200-1,400 kWh/year, all of it now through the compressor
and all of it deferrable.

The owner's decision was right, and for this project's own reasoning: a
diverter converts 1 kWh of electricity into 1 kWh of heat (COP 1), where
exporting it earns 13-24p and the heat pump can make the same heat later from
0.33 kWh at COP 3. That is the same argument that makes `powerfulMode`
suspect.

Not explained by the diverter: **January-February roughly doubled too**
(92, 90 -> 174, 147), and there is no surplus solar to divert in January. Some
other change coincided -- setpoint, schedule, or usage. Unresolved, and not
worth chasing.

Anomaly, unresolved: **September 2025 shows 380 kWh for hot water** against
46-153 either side, with space heating at zero that month. That is 12.7
kWh/day -- about four times a normal compressor-driven tank, and very close to
what a 3 kW resistive element running 4-5 hours a day would draw. The Altherma
EDLA's own backup heater is included in these figures, so a booster left
enabled when the diverter was decommissioned would fit; so would several other
things. The owner does not recall, the daily and weekly arrays only reach back
two days and two weeks, and the Onecta app reads the same window -- **the
evidence needed to diagnose it no longer exists.** Recorded anyway, because it
proves something in that tank circuit can draw 12-13 kWh/day, which is a
resistive element and not a compressor.

### The observer MUST archive these arrays

The window is two calendar years, not rolling. On 1 January 2027 the buckets
shift and **2025 is gone permanently** -- as 2024 already is; it was in this
same array a year ago and no parameter asks for it back.

So the observer appends the consumption arrays to a local file on every poll.
This costs zero extra API calls, because they arrive in the same payload as
everything else. After a couple of years that yields a continuous record
rather than a two-year window, and -- more to the point -- the before/after
evidence for whether any of this actually saves money, which no amount of
arithmetic can supply.

## Why our architecture fits the rate limit and Homey's does not

200 requests/day is about one every 7 minutes. Homey and Home Assistant poll
continuously and spend that budget merely watching.

`reconcile.py` is event-driven at slot boundaries. A bonus slot opening and
closing is **two calls**. Even a busy day is ~20 against a budget of 200. We
would fit inside the limit using the design we already have, with room for
read-back verification on every write.

That is not a small point. It is the main technical argument for building
rather than buying.

## What Homey would give, and why not to buy it

The Homey Daikin ONECTA app's published Flow cards:

    reads    power meter, target temperature, room temperature,
             leaving water temperature, outdoor temperature,
             on/off state, thermostat mode

    writes   Set the temperature
             Turn on / Turn off / Toggle
             Set the thermostat mode to ...
             Set the offset for operation-mode to ...

That last card is the good lever -- the weather-compensation offset. So Homey
is *not* inadequate for the best strategy, and an earlier claim in
conversation that it was has been withdrawn.

But there is no published DHW tank or Powerful-mode card, so the second lever
is missing; it costs ~£350-400; and it would sit alongside the agent rather
than inside it, with no shared view of slot state. Roughly 200 lines of ours
gets the same primary lever plus the DHW setpoint, on hardware already owned,
wired into slot transitions that are already debugged.

Separately and for the record: the Homey **Sigenergy** app exposes no action
for EMS work mode, forced grid charging or Remote EMS -- its only actions are
EV-charger ones. It cannot do what this project does. If that ever changes it
is worth revisiting, because it works over local Modbus with no cloud
dependency, which is a better foundation than our unofficial cloud path.

## Shape of the build

- **`daikin.py`**, ~200 lines, stdlib `urllib` + `json`, same shape as
  `zappi.py`. Read-only functions first; writes behind an explicit flag.
- **`daikin.py --auth`**, run once on a workstation. OIDC redirect; the
  callback rejects `localhost`, so it needs a resolvable hostname and a
  self-signed certificate. Produces a refresh token stored beside
  `.octopus-token.json`, mode 0600.
- **Two hooks in `reconcile.py`**, at slot open and slot close -- the same
  transitions that already switch the Sigen profile.
- **`daikin.py --deadman`**, which undoes the offset if the agent dies.
  Non-negotiable. An offset left at +5 C is exactly the latched-state class of
  bug that pinned the battery at -3.00 kW across an evening peak for six
  hours. Record how to undo the change *before* making it, as
  `sigencloud.py` already does.
- **Fail closed.** An unreachable Daikin cloud means no nudge, never a guess.

## First step, and it is free

`GET /v1/gateway-devices` against the real unit, read-only, one call out of
200. It answers:

- which management points and datapoints this firmware actually exposes;
- whether the installer configured `roomTemperature`, `leavingWaterTemperature`
  or `leavingWaterOffset` -- this determines the whole strategy;
- the tank's current setpoint and range.

Then one measurement, watching plant import while Powerful mode runs, settles
whether it uses the compressor or the immersion -- and therefore whether lever
3 is worth anything at all.

Needs a Daikin developer portal account and one OAuth round trip. No writes,
no risk to the heating.

## Open questions

1. Compressor or immersion under `powerfulMode`? Still open, and now less
   important, because `onOffMode` gives a DHW lever that does not depend on
   the answer.
2. ~~Which setpoint mode is configured?~~ **Answered:** `weatherDependent`
   with `controlMode = roomTemperature`, and `leavingWaterOffset` settable.
   The best case.
6. Does releasing `onOffMode` on the tank actually trigger a reheat inside a
   30-minute slot, or does `heatupMode = reheatSchedule` defer it to the
   unit's own schedule? This decides whether lever 2 works at all.
7. How much of the DHW load is genuinely deferrable without running out of
   hot water? Suppressing the tank has a comfort failure mode that shifting
   a heating curve does not.
3. How much load actually lands inside bonus slots in winter? The £10-25/month
   estimate is arithmetic, not observation, and it needs 5-12 kWh of shifted
   heat per day to hold. Bonus slots are driven by the car's schedule, so the
   binding constraint may be how many slot-minutes exist at all, not how much
   heat the house can absorb. Measure before believing.
4. Does bumping the curve mid-slot and dropping it 30 minutes later cause
   short-cycling? A 30-minute slot is short for a heat pump.
5. **Main fuse headroom.** During a bonus slot the agent already draws ~10 kW
   charging the battery. An ASHP at 3-5 kW plus a Zappi at 7 kW on top needs
   headroom that should be confirmed, not discovered.
