# Deploying the agent

## Is your plant suitable? Read this first.

Releasing Remote EMS **always** returns a SigenStor to **Self-Consumption**,
never to the mode that was selected before. This is firmware behaviour, it
cannot be changed over Modbus — the operational mode has no register at plant
or device level — and it happens after every commanded slot.

| Your normal mode | Effect |
|---|---|
| **Self-Consumption** | none. The revert returns you exactly where you were. **Run it.** |
| **Sigen AI / TOU / Feed-in** | every slot silently drops you to Self-Consumption until you reset it in the app |

So: if you run Self-Consumption, this is safe to run unattended today. If you
run Sigen AI, the agent now **tells** you every time — it logs `MODE
REVERTED` on release and reports it in the heartbeat, and the watchdog shows
that site as `ACTION` rather than a green `OK` until it next reports clean.
That turns a silent degradation into a notification, which is the difference
between usable-with-care and not usable. Expect to reset the mode afterwards — or wait until a restore path exists. The only known one is the
Sigen cloud API, which is unofficial, needs your mySigen password, and whose
reference implementation has been removed from GitHub.

One site, one agent. The agent is `reconcile.py`; everything here is about
making sure it can never leave the plant latched.

## Install

```sh
sudo useradd --system --home /opt/oig-sigen oig
sudo install -d -o oig -g oig /opt/oig-sigen
sudo cp *.py /opt/oig-sigen/
sudo cp .env /opt/oig-sigen/.env          # NOT .env.example
sudo chown -R oig:oig /opt/oig-sigen
sudo chmod 600 /opt/oig-sigen/.env

sudo cp deploy/oig-sigen.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo crontab -u oig deploy/oig-sigen.cron
```

Prove it before enabling the timer-driven path:

```sh
sudo -u oig sh -c 'cd /opt/oig-sigen && python3 reconcile.py --dry-run --once -v'
```

Create `/etc/default/oig-sigen`, owned by `oig`, mode 600:

```sh
WATCHDOG_URL=https://your-watchdog/v1/heartbeat
SITE_TOKEN=sig_...
```

Then `sudo systemctl enable --now oig-sigen`.

The unit will not start without those two set. That is deliberate: an agent
that does not report leaves the watchdog announcing "nothing held" for a
plant that may be latched in mode 3, which is worse than no watchdog because
it reads as reassurance.

## On a Synology NAS

A good host: it does not sleep, it is already on, and the agent is small
enough not to matter on any model. Two things differ from the systemd route.

**Check the Python version first — this is the usual blocker.** `octopus.py`
needs `zoneinfo`, so **Python 3.9 or newer**. Synology's own Python 3 package
on DSM 6.x is 3.8, which is too old. SynoCommunity's Python 3.11 covers DSM
6.x and 7.x across the ARM and x86 architectures, including older ones like
`armada370` (DS213j, DS216se).

```sh
uname -m                # armv7l, x86_64, aarch64 ...
python3 --version       # must be 3.9+
openssl version         # only needed for --via-cloud or the mode restore
```

If `uname -m` reports `armv5tel` (Marvell Kirkwood, pre-2013 models), stop —
there is no usable Python there.

**No Docker on ARM models.** DSM's Docker/Container Manager package is x86
only, so ignore the Dockerfile. Copy the `.py` files and `.env` to a share
and run them directly — being dependency-free, there is nothing else to
install.

**Use Task Scheduler instead of systemd**, in Control Panel:

| Task | Type | Runs |
|---|---|---|
| agent | Triggered Task → Boot-up | `cd /volume1/oig-sigen && nohup python3 reconcile.py --bonus-only --require-ev >/dev/null 2>&1 &` |
| deadman | Scheduled Task, every 5 min | `cd /volume1/oig-sigen && python3 control.py --deadman` |
| cloud deadman | Scheduled Task, every 5 min | `cd /volume1/oig-sigen && python3 sigencloud.py --deadman` |

Run them as a user that owns the directory: state lives beside the scripts,
and a deadman that cannot read `.lease.json` silently protects nothing.

**Networking:** the agent needs to reach the plant on your LAN and
`api.octopus.energy` outbound. It needs no inbound access at all, so a NAS
with no port forwarding is a sound place for it — but do not firewall it off
from the internet entirely, or the schedule poll will simply time out.

**Older DSM is end-of-life** (6.2 stopped getting updates some years ago).
The agent puts your Octopus API key, and optionally your mySigen password, in
a file on that machine. With no inbound exposure the risk is small, but it is
a new class of secret on an unpatched box — worth deciding rather than
defaulting into. The Modbus-only path needs no Sigen credentials at all.

## Testing on a Mac laptop? Read this first

A sleeping host is the same as a dead one — it stops polling, stops
renewing, stops releasing. On a laptop that happens routinely, and macOS
makes it easy to think you have prevented it when you have not.

`caffeinate -is` is the usual advice, but **`-s` only holds on AC power**, and
it fails *silently* — no error, the assertion simply is not taken. Measured on
battery:

```
$ pmset -g assertions
PreventSystemSleep          0     <- -s silently absent
PreventUserIdleSystemSleep  1     <- -i is held
```

So on battery you get idle-sleep prevention only, and closing the lid still
suspends the process. Observed: an agent stopped ticking at 22:59 and was
still frozen at 23:35, mid-schedule.

If you are testing on a Mac:

```sh
nohup caffeinate -is python3 reconcile.py ... &
pmset -g assertions | grep PreventSystemSleep   # confirm it says 1
```

and **keep it plugged in**. The agent detects a suspend after the fact — it
sleeps against the wall clock, not a countdown, and logs `woke Ns later than
intended` — but that is forensics, not prevention.

Properly: use hardware that does not sleep. This whole section is a laptop
problem, and a NAS or Pi does not have it.

## The three layers of release, and what each one covers

| Layer | Covers | Does not cover |
|---|---|---|
| In-process: SIGTERM/SIGINT/atexit | `systemctl stop`, crash, exception | `SIGKILL`, power loss |
| `ExecStopPost=control.py release` | the agent dying however it dies | systemd itself not running |
| Cron deadman, every 5 min | agent killed hard, host still up | **the host being down** |

**Nothing here covers the host dying.** There is no Modbus watchdog, so if
the VM stops, whatever mode was latched stays latched — potentially importing
at peak until someone notices. Cron and systemd die with the host; they
cannot help.

The only real fix is a watcher that is not on the same host. That is the
strongest argument for the cloud component: not scheduling, which the agent
can do alone, but being the off-box observer that notices a site has gone
quiet mid-lease and raises an alarm. Until that exists, keep `LEASE_TTL_MINUTES`
short — the risk window is the TTL, not the length of the cheap slot.

## Before enabling on a new site

1. `python3 probe.py` — read-only, confirms the V2.7 map matches the firmware.
2. `python3 reconcile.py --dry-run --once -v` — decisions only, no writes.
3. One supervised `control.py charge --kw 5 --minutes 10` inside a cheap slot.
4. Only then enable the service.
