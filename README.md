# oig-sigen

Charge a Sigenergy SigenStor from the grid during Octopus **Intelligent Go**
bonus slots — the extra half-hours Octopus releases at short notice when it
schedules your car, billed at the off-peak rate for the whole property.

Your SigenStor has no idea those slots exist. This tells it.

No Home Assistant. No dependencies. About 2,000 lines of standard-library
Python that runs on anything — a NAS, a Pi, a mini-PC, a spare laptop.

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

**The savings track your EV usage, not the calendar.** Bonus slots exist
because Octopus is charging your car. On a night when the car is nearly full,
there may be 90 minutes of bonus time; on a night it doesn't charge at all,
there is none. This is not a flat saving.

**You do not need this for 23:30–05:30.** That window is guaranteed and your
plant's own optimiser almost certainly already uses it. Run with
`--bonus-only` (the default in the supplied unit file) so the agent leaves it
alone.

**A planned dispatch is a forecast about your car, not a price guarantee.**
Octopus bills the off-peak rate for dispatches that actually *ran*. A slot in
`plannedDispatches` that never completes bills at **peak**. Charging on the
plan alone can buy 29.757p electricity while reporting a saving — so always
run with `--require-ev` or `--require-zappi`.

**Choose your actuation path deliberately.** See below; one of them changes
your inverter's operating mode as a side effect.

---

## The two ways it can charge

### Cloud (`--via-cloud`) — recommended

Switches your plant to a charging profile you create once in the mySigen app,
then switches it back.

- Nothing is latched at the plant; **your operating mode is preserved**
- No LAN presence needed — it can run anywhere
- Rate is fixed in the profile; change it in the app
- Uses an **unofficial, undocumented** Sigenergy cloud API that has broken
  once before and needs your full mySigen password in `.env`

### Local Modbus (default)

Takes a Remote EMS lease and commands mode 3 (command charging, grid first).

- Official, documented protocol; no third party involved
- Power set per command (`--kw`)
- **Releasing Remote EMS always returns the plant to Self-Consumption**, not
  to the mode you had selected. This is Sigenergy firmware behaviour and
  cannot be undone over Modbus. If you run Self-Consumption anyway it costs
  you nothing; if you run Sigen AI, **every slot silently drops you out of
  it** until you reset it in the app or let the cloud restore do it

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

### Check it works before commanding anything

```sh
python3 probe.py                       # read-only; confirms the register map
python3 octopus.py                     # your cheap periods
python3 reconcile.py --dry-run --once -v   # decides, writes nothing
```

---

## Run it

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

Polls Octopus at `:25` and `:55` — dispatch data is half-hourly, so there's
nothing to gain from a faster free-running timer. It also wakes on every slot
boundary, five minutes before a slot opens to re-confirm it still exists, and
every two minutes while a slot is live.

That churn handling is not theoretical: slots move. One was withdrawn two
minutes after charging began, and the agent released before the price
changed.

`--bonus-only` subtracts the guaranteed 23:30–05:30 window from the dispatch
schedule, so a dispatch straddling the boundary is trimmed rather than
duplicating what your plant already does.

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
throttles the plant — that bug cost the author a day of capped export before
it was found.

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

Written for one plant and shared in case it's useful. If your setup differs,
read `CLAUDE.md` — it records what was measured, what was assumed, and
several things that turned out to be wrong.

## Licence

MIT.
