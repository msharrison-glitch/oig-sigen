# Deploying the agent

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
