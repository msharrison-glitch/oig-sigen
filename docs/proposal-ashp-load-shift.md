# Proposal: shift the heat pump into bonus slots

Status: **researched, not built, and not yet measured on the plant.** Written
2026-09-07 from the Daikin Onecta API, the Home Assistant integration's
source, and the Homey app's published Flow cards. Nothing here has touched a
heat pump. The first step below is read-only and settles the one question that
decides whether the rest is worth building.

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

And it is per kWh of *electricity*. At COP 3 a kWh of *heat* needs a third of
that, so per kWh of heat delivered the figure is roughly **3-6.5p**. Any
estimate that quotes the spread against heat rather than electricity is
inflated threefold.

Realistic magnitude: near nil in September; **£10-25/month in deep winter**,
depending on how much load actually lands inside bonus slots. Worth a weekend
of code. Not worth £400 of hardware.

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

Ranking, subject to measurement:

1. `leavingWaterOffset` on `climateControl` -- compressor, full spread.
2. `domesticHotWaterTemperature` setpoint -- compressor, bounded, tank holds
   the heat well.
3. `powerfulMode` -- probably the immersion. Verify before using; may be
   worth nothing.

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

1. Compressor or immersion under `powerfulMode`? Decides the DHW lever.
2. Which setpoint mode is configured? Decides the space-heating lever.
3. How much load actually lands inside bonus slots in winter? The £10-25/month
   estimate is arithmetic, not observation.
4. Does bumping the curve mid-slot and dropping it 30 minutes later cause
   short-cycling? A 30-minute slot is short for a heat pump.
5. **Main fuse headroom.** During a bonus slot the agent already draws ~10 kW
   charging the battery. An ASHP at 3-5 kW plus a Zappi at 7 kW on top needs
   headroom that should be confirmed, not discovered.
