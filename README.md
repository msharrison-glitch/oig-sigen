# oig-sigen

Charge a Sigenergy SigenStor from the grid during Octopus **Intelligent Go**
bonus slots — the extra half-hours Octopus releases at short notice when it
schedules your car, billed at the off-peak rate for the whole property.

Your SigenStor has no idea those slots exist. This tells it.

No Home Assistant. No dependencies. About 3,800 lines of standard-library
Python, plus 1,700 lines of offline tests, running on anything that stays
awake — a NAS, a Pi, a mini-PC, a desktop, or a laptop configured not to
sleep.

---

## Is this for you?

You need all four:

- A **SigenStor** with Modbus TCP enabled on your LAN
- **Octopus Intelligent Go** as your import tariff
- An **EV that actually gets charged** — no charging, no bonus slots, nothing
  for this to do
- Somewhere to run a small Python process **that will not go to sleep** — see
  [What to run it on](#what-to-run-it-on)

**It does not replace your inverter's optimiser.** If you run Sigen AI or
Time-based Control and you're happy with it, keep it. This only touches the
bonus slots your plant cannot see, and hands control straight back. If you
want something that takes over battery management entirely and you already
run Home Assistant, use [Predbat](https://github.com/springfall2008/batpred) —
it's excellent, free, and supports SigenStor.

---

## Read this before installing

**What you save varies enormously, and winter is when it matters.** Two
things have to line up: Octopus must be charging your car — no charging, no
bonus slots — and your battery must have room to put the cheap energy. In a
UK summer, solar often fills the battery by evening and there is little
headroom left; in winter it does not, and the same slots become worth several
times more. Expect most of the benefit between roughly October and March.

This is not a flat saving, and testing it in July will not tell you what it
is worth in January.

**You may not need this for 23:30–05:30.** That window is guaranteed cheap,
and most setups already use it — Sigen AI on a profit-focused preference, or
TOU configured for IOG, will grid-charge through it. If yours does, run with
`--bonus-only` so the agent leaves it alone and only adds the bonus slots.

If your plant never grid-charges and still has room in the battery overnight,
omit `--bonus-only` and let the agent use the window too. Watch what your
battery actually does for a night or two before deciding — it depends on your
mode, your solar and your demand, not on the mode name alone.

**A planned dispatch is a forecast about your car, not a price guarantee.**
Octopus bills the off-peak rate for dispatches that actually *ran*. A slot in
`plannedDispatches` that never completes bills at **peak**. Charging on the
plan alone can buy 29.757p electricity while reporting a saving — so always
run with one of:

- **`--require-zappi`** — asks a myenergi Zappi directly. No lag, catches
  every slot including the first one after you plug in. myenergi only.
- **`--require-ev`** — works with any charger, using Octopus's record of
  *completed* dispatches. Those lag, so it **cannot confirm the first slot of
  a charging session** — the short one that appears seconds after you plug
  in. It picks up from the second slot onward.

No other charger is supported today, and won't be until someone with one can
test it.

**Choose your actuation path deliberately.** See below; one of them changes
your inverter's operating mode as a side effect.

---

## What to run it on

**The one hard requirement is that it does not sleep.** A suspended host is
indistinguishable from a dead one: the plant has no Modbus watchdog, so a
latched mode stays latched, and on the cloud path the plant simply carries on
grid-charging with your normal mode suspended. The agent measures its sleep
against the wall clock and logs when it has been suspended, so you will see it
after the fact — but seeing it afterwards is not the same as it not happening.

**A laptop is fine, but only once you have configured it.** Out of the box a
closed lid suspends it, which is how this was learned. To use one:

| | |
|---|---|
| macOS | `sudo pmset -a disablesleep 1` (lid-closed operation), plus `powernap 0`. **Keep it on AC** — `caffeinate -s` silently stops holding on battery |
| Linux | `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`, then restart `systemd-logind` |
| Windows | Power Options → *Choose what closing the lid does* → **Do nothing**, on "Plugged in" |

Battery wear is the trade: a laptop held permanently on AC ages its cell
faster than one that cycles. If the machine is otherwise idle anyway, that is
usually an acceptable price.

### Running cost

It runs continuously, so its idle draw is what you pay for, not its peak.
Figures below are a headless machine doing nothing but this, costed at a
blended **23p/kWh** — roughly what a 24/7 load works out at on IOG, with a
quarter of each day inside the guaranteed cheap window.

| Host | Typical idle | kWh/year | Approx £/year |
|---|---|---|---|
| Something already running (NAS, desktop, server) | **no extra** | 0 | **£0** |
| Raspberry Pi Zero 2 W | 0.5 W | 4 | £1 |
| Raspberry Pi 4 | 3 W | 26 | £6 |
| Raspberry Pi 5 | 3.5 W | 31 | £7 |
| Mac mini / laptop, Apple silicon | 5 W | 44 | £10 |
| Mini-PC (Intel N100 class) | 7 W | 61 | £14 |
| Older Intel mini-PC or laptop | 10–15 W | 88–131 | £21–31 |
| NAS bought specifically for this | 30 W+ | 263+ | £62+ |

Two things follow. **The cheapest host is one you are already running** — the
marginal cost of adding a Python process that sleeps most of the time is
indistinguishable from zero, which is why a NAS or an always-on desktop beats
buying anything. And if you are buying, **the running cost can overtake the
purchase price**: a £62 Pi 5 costs about £35 of electricity over five years,
while a 12 W mini-PC costs about £120 over the same period.

These are estimates, not measurements. Real draw depends on what else the
machine is doing, and on your own tariff split.

---

## The two ways it can charge

Both work. Which suits you depends mostly on **what operating mode you
normally run**, and on how you weigh a third-party dependency against a
side effect. Pick deliberately.

### Local Modbus (default)

Takes a Remote EMS lease and commands mode 3 (command charging, grid first).

- Official, documented protocol; no third party in the loop
- Works on your LAN alone — no internet needed
- Power set per command (`--kw`)
- **Releasing Remote EMS always returns the plant to Maximum Self-Powered**,
  whatever you had selected — including a custom profile. Sigenergy firmware
  behaviour, and it cannot be undone over Modbus because the operating mode
  has no register there. If `SIGEN_CLOUD_*` is set the agent puts your mode
  back automatically after every release, retrying until it succeeds and
  refusing to take another slot while it still owes you one. Without those
  credentials it cannot, and says so at startup

### Cloud (`--via-cloud`)

Switches your plant to a charging profile you create once in the mySigen app,
then switches it back.

- Nothing is latched at the plant; **your operating mode is preserved**
- No LAN presence needed — it can run anywhere
- Rate is fixed in the profile; change it in the app
- Uses an **unofficial, undocumented** Sigenergy cloud API. It has broken
  once before, its reference implementation has since been removed from
  GitHub, and it needs your full mySigen password in `.env`

### The thing that usually decides it

| You normally run | Modbus revert costs you | |
|---|---|---|
| **Self-Consumption** | nothing — you're returned where you were | Modbus is the simpler choice |
| **Sigen AI / TOU / Feed-in** | that setting, on every slot | either use the cloud path, or accept resetting it |

If you take the Modbus path on a non-Self-Consumption plant, the agent logs
`MODE REVERTED` on every release and the optional watchdog flags the site, so
it is at least visible rather than silent.

---

## Install

```sh
git clone https://github.com/msharrison-glitch/oig-sigen.git
cd oig-sigen
cp .env.example .env      # then fill it in
```

Nothing to install. Python 3.9+ and, for the cloud path, `openssl` (used to
match Sigenergy's password encoding, since Python ships no AES).

**3.9 is a floor, not a preference**, and worth checking rather than assuming:
`octopus.py` imports `zoneinfo`, which arrived in 3.9. A Synology NAS is the
trap here — DSM 7.4's own `python3` is 3.8, so the agent dies on that import,
and under a `Restart=always` unit that becomes a crash loop that still reports
as running. Install Python 3 from Package Center and point the unit at it;
`deploy/synology/install-service.sh` finds it for you.

### Configure

| Variable | Needed for | Notes |
|---|---|---|
| `OCTOPUS_API_KEY` | always | octopus.energy → Personal Details → API access |
| `OCTOPUS_ACCOUNT_NUMBER` | always | `A-XXXXXXXX` |
| `SIGEN_HOST` | always | your plant's LAN address |
| `IOG_OFF_PEAK_P` / `IOG_PEAK_P` | cost summary | rates vary by DNO region |
| `SIGEN_CLOUD_USERNAME` / `_PASSWORD` / `_REGION` | `--via-cloud` | your mySigen app login |
| `MYENERGI_SERIAL` / `_API_KEY` | `--require-zappi` | hub serial, not the Zappi's |
| `IOG_POLL_CHARGING_SECONDS` | optional | default 30 — how fast a withdrawn slot is caught |
| `IOG_POLL_IDLE_SECONDS` | optional | default 300 — how fast a new slot is noticed |
| `IOG_RESTORE_MODE` | **set this** | the mode to put your plant back on after a slot. Without it the agent infers it from the plant, which is correct by luck rather than design — and the fallback is Sigen AI, so an owner on Maximum Self-Powered or TOU is silently moved. `sigencloud.py --list` shows yours |
| `IOG_RESTORE_PROFILE` | if the above is a custom profile | its id, from `--list` |
| `IOG_RESUME_BAND_PCT` | optional | default 10 — SOC must fall this far below target before charging resumes, so the release does not chatter |
| `MYENERGI_USER_AGENT` | optional | only if Cloudflare starts rejecting the default |

### Check it works before commanding anything

```sh
python3 probe.py                       # read-only; confirms the register map
python3 octopus.py                     # your cheap periods
python3 reconcile.py --dry-run --once -v   # decides, writes nothing
```

---

## Run it

Local Modbus:

```sh
python3 reconcile.py --bonus-only --require-ev --kw 5
```

Or via the cloud:

```sh
python3 reconcile.py --bonus-only --require-ev \
                     --via-cloud --charge-profile "OIG Charge"
```

For the cloud path, first create a profile in the mySigen app
(**Operational Mode → Add**) that charges **from the grid**, covering as wide
a time range as the app allows. The agent uses it as an on/off switch — the
scheduling lives here, not in the profile.

**Choosing the rate takes a moment's thought, because the profile is a fixed
setting and the agent cannot override it.** Changing your mind later means
editing the profile in the app.

The constraint people miss: **a bonus slot exists because Octopus is charging
your car, so the battery and the car draw at the same time, by definition.**
Work out what is left:

```
headroom = supply capacity − EV charger − household baseline
rate     = the LOWER of that and your inverter's rated charge power
```

On a typical UK single-phase supply, 100 A is about 23 kW. Take off a 7.4 kW
charger and a kilowatt or so of background load and you have roughly 14 kW
left — so for most single-phase plants the **inverter's own charge rating is
the binding limit**, not the fuse. That changes if you have an 80 A or 60 A
supply, a 22 kW charger, or both, in which case the fuse binds first and you
should size to it. Three-phase installations have far more room and are
usually inverter-limited.

Check your inverter's rating rather than assuming: `python3 probe.py` reports
it. Note that on a plant with more battery modules than inverter capacity, it
is the **inverter** that decides, not the battery.

If you are unsure, start low and raise it once you have watched a slot. Too
low only costs you some of the benefit — a half-hour slot at 3 kW puts in
1.5 kWh where 10 kW would have put in 5. Too high risks tripping something
while you are asleep.

Running it on a laptop? Configure it not to sleep first — see
[What to run it on](#what-to-run-it-on). On macOS in particular,
`caffeinate -s` only holds on AC power and fails silently, so on battery it
will sleep through slots. `deploy/README.md` has the detail.

`deploy/` has a systemd unit, a cron deadman and a runbook. `Dockerfile` and
`docker-compose.yml` build a multi-arch image for a NAS or Pi — see
`deploy/DOCKER.md`, which covers the four things that specifically bite
(timezone data, the state volume, release-on-restart, and not co-locating the
watchdog).

---

## How it decides

Polls Octopus at least every five minutes when idle, and at `:25` and `:55`
— five minutes before each half-hour boundary — to re-confirm a slot still
exists before committing to it. It also wakes on every slot boundary.

**Once a slot is live it polls every 30 seconds.** Octopus withdraws slots at
short notice, and every second between a withdrawal and our noticing is
imported at the peak rate. Worst case is about 45 seconds of that: 30 to
notice, 5–25 to release. Both cadences are configurable in `.env`, floored at
15 seconds, and jittered a few seconds so many installations on the same
tariff don't poll in lockstep.

The five-minute floor matters for one case in particular: the first dispatch
after you plug in starts within a few minutes of the plug going in and runs
only to the next half-hour boundary. Waiting for the aligned poll would miss
most of it.

That churn handling is not theoretical: slots move. One was withdrawn two
minutes after charging began, and the agent released before the price
changed.

`--bonus-only` subtracts the guaranteed 23:30–05:30 window from the dispatch
schedule, so a dispatch straddling the boundary is trimmed rather than
duplicating what your plant already does. Without it, the agent treats the
guaranteed window as chargeable too — which is what you want if your
operating mode never grid-charges.

---

## Safety

There is **no Modbus watchdog** on a SigenStor. A latched command outlives
the process that set it, so:

- The intent is written to a state file **before** any register is touched
- Release is wired to exit, exceptions, SIGINT and SIGTERM
- Leases are short and rolled forward, so a dead agent is caught in minutes
- `control.py --deadman` and `sigencloud.py --deadman` are idempotent and
  cron-safe
- `cloud/server.py` is an optional off-box watchdog: it observes and alarms,
  and deliberately has **no** control path

Power limits written during a lease are restored on release. They are
enforced even with Remote EMS disabled, so a limit left behind silently
throttles the plant. A 3 kW limit left behind by a test capped this plant's
export at 3 kW through an entire evening peak — against a 14.4 kW rating —
with Remote EMS disabled the whole time.

---

## What it will not do

- Author or edit a mySigen energy profile (the API exposes no profile CRUD)
- See your solar, if your PV is on a separate inverter
- Help if your car isn't charging
- Work without an internet connection, on the cloud path

---

## Status

One SigenStor, firmware matching Modbus protocol V2.7. Be clear which half
of this is proven, because the two paths are not equally tested.

**Read the numbers in this repo as belonging to one plant.** It is single
phase, 12.6 kW charge, 24.18 kWh. A second installation checked on 2026-09-06
is three phase, 108.48 kWh of modules behind a 25 kW inverter -- so every cost
figure here is roughly half what it should be on that one. The register map
read cleanly on both, which is the part that generalises. The arithmetic is
not.

Note which rating matters: the ESS modules there are rated 52.8 kW, but the
inverter limits to 25 kW, and it is the inverter that decides what a stranded
charge actually costs you.

That second plant also reports real PV power on register 30035, where this
one reads zero because its solar is on a separate inverter. Same model, same
firmware, different wiring. Assume nothing about a plant you have not read.

**The cloud path (`--via-cloud`) is proven on hardware.** Multiple real bonus
slots across several nights, unattended: acquiring on a confirmed dispatch,
releasing on a withdrawal within ~30 s, releasing at a slot boundary,
stopping at the SOC ceiling, restoring the owner's operational mode, and
recovering from a transport fault mid-slot.

**The Modbus path has never charged a battery.** Standby (mode 1) and
discharge (mode 6) were commanded and released on hardware on 2026-08-30, and
the power-limit restoration was proven by removal the same evening. Grid
charging -- mode 3, the default when you omit `--via-cloud` -- has not been
run once. It is the path that *latches* the plant, so it is also the one
that needs the lease, the deadman and a host that will still be alive to
release it. If you use it, you are the first.

Everything else -- the lease, the deadman, the two-controller guard, the
schedule arithmetic -- is covered offline and has not been exercised against
a real held lease.

Eight offline test suites, no hardware or credentials required:

```sh
for t in test_*.py; do python3 "$t" || break; done
```

Written for one plant and shared in case it's useful. Your firmware, wiring
and tariff region may differ — probe before you command, and read the
comments in `registers.py` and `control.py`, which record what was actually
measured on hardware rather than what the protocol document claims.

## Licence

MIT.
