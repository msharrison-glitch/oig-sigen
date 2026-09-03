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
enough not to matter on any model. It differs enough from the systemd route
to have its own document — requirements, the supervisor, the Task Scheduler
alternative, and how to stop it again — in
[`synology/README.md`](synology/README.md).

The usual blocker is Python: `octopus.py` needs `zoneinfo`, so 3.9 or newer,
and DSM's own package on 6.x is 3.8. Check `uname -m` and `python3 --version`
before anything else.

## Operating the agent

Host-independent, once it is running.

**Reading the log.** The lines that carry information:

| Line | Meaning |
|---|---|
| `SCHEDULE + added` | Octopus published a slot |
| `SCHEDULE - WITHDRAWN` | a live slot was pulled — the expensive case |
| `SCHEDULE   ended` | a slot ran to its end; routine |
| `STARTED charging` | the plant has been commanded |
| `holding` | charging, and re-checking every 30 s |
| `RELEASED` with `cloud: restored mode N` | your normal mode is back |
| `MODE REVERTED` | a Remote EMS release dropped you to Self-Consumption |
| `RESTORE FAILED` | act on this: the plant may still be charging |

**The state files are the ground truth**, and both live beside the scripts:

- `.lease.json` — a Remote EMS lease is held. Its absence is what makes
  `control.py --deadman` a no-op.
- `.cloud-mode.json` — a cloud mode switch is outstanding, and it records how
  to undo it. Present with no agent running means something owes the plant a
  restore; that is what `sigencloud.py --deadman` is for.

**Stopping it.** Plain `kill` — SIGTERM — never `kill -9`. The release path is
wired to SIGTERM, SIGINT, normal exit and unhandled exceptions, so an ordinary
kill hands the plant back on the way out. `-9` skips all of it and leaves the
plant latched, or on the charging profile, until a deadman notices.

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
