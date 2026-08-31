# oig-sigen

Charge a Sigenergy SigenStor from the grid during Octopus **Intelligent Go**
bonus slots — the extra half-hours Octopus releases at short notice when it
schedules your car, billed at the off-peak rate for the whole property.

Your SigenStor has no idea those slots exist. This tells it.

No Home Assistant. No dependencies. About 3,800 lines of standard-library
Python, plus 1,700 lines of offline tests, running on anything — a NAS, a Pi,
a mini-PC, a spare laptop.

---

## Is this for you?

You need all four:

- A **SigenStor** with Modbus TCP enabled on your LAN
- **Octopus Intelligent Go** as your import tariff
- An **EV that actually gets charged** — no charging, no bonus slots, nothing
  for this to do
- Somewhere to run a small Python process

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
git clone https://github.com/<you>/oig-sigen.git
cd oig-sigen
cp .env.example .env      # then fill it in
```

Nothing to install. Python 3.9+ and, for the cloud path, `openssl` (used to
match Sigenergy's password encoding, since Python ships no AES).

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
(**Operational Mode → Add**) that charges **from the grid** at a sensible
rate, covering as wide a time range as the app allows. The agent uses it as
an on/off switch — the scheduling lives here, not in the profile.

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

Verified on real hardware (single SigenStor, firmware matching Modbus
protocol V2.7): grid charging via both Modbus mode 3 and cloud profile
switching, clean release on a withdrawn slot, power-limit restoration, and
overnight unattended running.

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
