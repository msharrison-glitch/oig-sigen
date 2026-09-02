# Running the agent on an Android phone (Termux)

Proven on a Redmi Note 10s (Android 13, MIUI) with Termux and Python 3.14.

**Why a phone.** Not because it is the best host -- it is not -- but because
almost everyone has an old one in a drawer. It lets someone find out whether
this project sees bonus slots on *their* Octopus account before buying any
hardware. A Pi Zero 2 W is a better permanent home.

## Pick your rung

| | Flag | Host needs to be |
|---|---|---|
| 1. Does it see my slots? | `--dry-run` | anything that boots |
| 2. Actually charge | `--via-cloud` | reasonably reliable |
| 3. Modbus lease | (default) | genuinely always-on, with a deadman |

**A phone is fine for 1 and 2, and a poor choice for 3.** Only the Modbus
path latches a mode at the plant, and only that path needs a host that can
guarantee it will still be alive to release it. Android cannot promise that
-- see the MIUI note below. On `--via-cloud` nothing latches, so a killed
process just stops charging.

## Install

Get **F-Droid**, then **Termux** and **Termux:Boot** from inside it. Not the
Play Store build: it is deprecated, frozen, and cannot install packages.

```sh
pkg update -y && pkg upgrade -y
pkg install -y python git openssl-tool openssh
pip install tzdata            # see below -- this one is not optional
termux-wake-lock
git clone https://github.com/msharrison-glitch/oig-sigen.git
cd oig-sigen
```

Then prove it runs, before any credentials or hardware:

```sh
for t in mock control octopus reconcile cloud watch zappi sigencloud; do
    python3 test_$t.py >/dev/null 2>&1 && echo "test_$t PASS" || echo "test_$t FAIL"
done
```

Eight passes means the phone can run this.

## `pip install tzdata` is required

Termux ships no timezone database, and `octopus.py` needs `Europe/London` to
get BST/GMT right. Without it you get:

    ZoneInfoNotFoundError: 'No time zone found with key Europe/London'

and five of the eight tests die at import. There is no `pkg install tzdata`
-- the package does not exist in Termux -- so pip is the only route.

This is the one exception to the project's stdlib-only rule, and it is a
narrow one: tzdata is *data*, not a library. It is the same IANA database
Debian and Alpine hand you through `apt` and `apk`. No code imports it; only
`zoneinfo` reads it, and `zoneinfo` is stdlib.

## MIUI will kill it otherwise

Xiaomi's skin is the most aggressive in Android about background processes.
Before anything else:

- Settings -> Apps -> Termux -> **Autostart: on**
- Settings -> Battery -> App battery saver -> Termux -> **No restrictions**
- Recents -> long-press Termux -> **padlock**

Even then, treat a kill as something that will happen, not something that
might. That is the whole argument for `--via-cloud` on a phone.

## Reaching the plant

Do not test with `ping` -- a SigenStor does not answer ICMP, so a failed ping
proves nothing. Test the thing you actually need, which is Modbus on TCP 502:

```sh
python3 probe.py 192.168.2.53
```

An explicit host on the command line beats `.env`, and Modbus has no
authentication, so this works before you have configured anything.

## Getting your .env across

Do not type an API key on a touchscreen. Termux runs an sshd on **port
8022**, key auth only:

```sh
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo '<your workstation public key>' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
sshd
whoami                       # the u0_aNNN username
```

Then from the machine that has the file:

```sh
cat .env | ssh -p 8022 u0_aNNN@<phone-ip> 'cat > ~/oig-sigen/.env'
ssh -p 8022 u0_aNNN@<phone-ip> 'chmod 600 ~/oig-sigen/.env'
```

sshd does not survive a reboot on its own; Termux:Boot can restart it.

## The deadman must outlive Termux

On `--via-cloud` nothing latches at the plant, so a killed agent simply stops
charging -- but only if it is killed *between* slots. Killed mid-slot, it
leaves the plant on the charging profile, importing at whatever rate that
profile allows until something puts the mode back. That something is the
deadman, and on MIUI it cannot live inside Termux: MIUI does not kill one
session, it kills the app, so an agent and a deadman sharing a process tree
die together. A deadman that dies with the thing it guards is not a deadman.

Android's own JobScheduler runs outside the app. `deadman.sh` here is the
script; register it once:

```sh
termux-job-scheduler --job-id 0 \
    --script ~/oig-sigen/deadman.sh \
    --period-ms 900000 \
    --persisted true \
    --battery-not-low false
```

- **900000 ms is the floor.** Since Android N the minimum period is 15
  minutes, so the worst-case exposure is longer than a cron-driven host's.
  Bounded, though: 15 minutes at 12.6 kW is about 3 kWh, or roughly 80p of
  peak-rate import over off-peak.
- **`--battery-not-low false` matters.** It defaults to *true*, which would
  stop the deadman running exactly when the phone is about to die -- the
  moment it is most needed.
- **`--persisted true`** survives a reboot. Nothing else on the phone does:
  `~/.termux/boot` is empty unless you populate it, so the agent itself does
  not come back after a restart.

Check it with `termux-job-scheduler --pending`.

## Do NOT auto-start the agent while another host runs one

It is tempting to add a second job that restarts `reconcile.py` if it is not
running -- the equivalent of the supervisor on a NAS. Do not do that until
this phone is the *only* host running an agent. A supervisor job would
happily start a second controller behind your back, and the guard in
`reconcile.py` is a local pid check that cannot see across machines.

## Two controllers must not run at once

If you already run this on another host, **stop that agent first**. The
guard in `reconcile.py` is a pid check against a local `.lease.json`, so it
cannot see an agent on a different machine -- and `--via-cloud` takes no
lease at all. Two of them would fight over the cloud operational mode.
