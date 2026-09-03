# Running the agent on an Android phone (Termux)

Verified here on a Redmi Note 10s (Android 13, MIUI) running Termux and
Python 3.14 -- see "What one night actually proved" at the end for what that
does and does not establish.

**Why a phone.** Not because it is the best host -- it is not -- but because
almost everyone has an old one in a drawer. It lets someone find out whether
this project sees bonus slots on *their* Octopus account before buying any
hardware. A Pi Zero 2 W is a better permanent home.

## Choosing a device

Nothing here is vendor-specific, and no model is recommended over another --
only one has been tested, so anything else would be written blind. Judge a
candidate in your drawer against these instead:

| Requirement | Why |
|---|---|
| **Android 7.0 or newer** | Termux needs it, and the persisted periodic jobs the deadman relies on arrived with N |
| **arm64 (`aarch64`)** | what Termux packages Python for most reliably -- check with `uname -m` |
| **Wi-Fi reaching the plant's LAN** | telemetry is Modbus on TCP 502, local only |
| **Can live permanently on a charger** | an agent asleep is an agent absent |
| **~200 MB free storage** | Termux, Python and the repo |

Explicitly **not** needed: a SIM, a working screen, Play Services, root, or
much RAM -- this runs in well under 100 MB.

The one thing that varies enormously between vendors is how aggressively the
skin kills background processes. That is a setup problem rather than a
disqualification, and it is dealt with below.

**One hazard worth naming.** An old phone held at 100 % on a charger for
months is a swelling risk, and a swollen cell in a cupboard is a fire in a
cupboard. Look at it occasionally, do not shut it inside anything sealed, and
retire it if the back starts to lift. A Pi Zero 2 W or any always-on Linux box
avoids the question entirely and is the better permanent home. The phone's
value is that it costs nothing to find out whether this project is worth
anything on your account.

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

## Your vendor's battery saver will kill it otherwise

Every Android skin kills background processes; they differ only in how
eagerly. Find your vendor's background-execution settings and exempt Termux
from all of them. The wording varies -- look for autostart, battery
optimisation or restriction, and a way to pin the app in the recents list.

The worked example is MIUI, because that is the one actually tested here:

- Settings -> Apps -> Termux -> **Autostart: on**
- Settings -> Battery -> App battery saver -> Termux -> **No restrictions**
- Recents -> long-press Termux -> **padlock**

Then hold a wake lock, which is what stops the CPU sleeping between ticks:

```sh
termux-wake-lock
```

Even with all of that, treat a kill as something that will happen, not
something that might. A quiet night proves nothing. The deadman below is what
makes a kill survivable, and it is the whole argument for `--via-cloud` on a
phone.

## Reaching the plant

Do not test with `ping` -- a SigenStor does not answer ICMP, so a failed ping
proves nothing. Test the thing you actually need, which is Modbus on TCP 502:

```sh
python3 probe.py 192.168.1.100      # your plant's address
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
lease at all. Two of them would fight over the cloud operational mode, and
worse than a clash: the second one reads the first's charging profile as the
mode to go back to, so it "restores" the plant into charging.

Stopping the other host may be less obvious than it sounds. On a Synology the
agent is a systemd unit with `Restart=always` behind a Task Scheduler
supervisor, so `kill` buys about thirty seconds -- see "Stopping it again is
not just `kill`" in `../README.md`.

## Running it

Register the deadman first, and make sure no other host is running an agent.
Then start with the rung that writes nothing at all:

```sh
cd ~/oig-sigen
python3 reconcile.py --dry-run --bonus-only --require-zappi -v
```

Leave that running across a plug-in. If it logs a `SCHEDULE + added` line for
a dispatch, the phone can see your bonus slots -- which is the entire question
rung 1 exists to answer, and you have answered it without touching the plant.

For rung 2, detach it so it outlives the terminal:

```sh
termux-wake-lock
cd ~/oig-sigen && nohup python3 reconcile.py \
    --bonus-only --require-zappi --via-cloud --charge-profile "OIG Charge" \
    -v --log-file phone.log < /dev/null > phone-stdout.log 2>&1 &
```

`--charge-profile` names a custom profile you have already created in the
mySigen app and configured to grid-charge. The agent selects it and puts your
normal mode back afterwards; it cannot author the profile for you, and it
refuses to start if the name does not resolve rather than charging blind.

**Launched over SSH, that command appears to hang.** Redirecting all three
streams is not enough -- ssh holds the channel open and never returns, which
looks exactly like a failed launch. The agent is running fine. Open a second
connection and look, rather than killing anything:

```sh
ssh -p 8022 u0_aNNN@<phone-ip> 'tail -5 ~/oig-sigen/phone.log'
```

A healthy first minute:

    cloud actuation: profile 'OIG Charge' is id 1234
    reconciler started (charging via cloud profile 1234 ..., no lease)
    SOC 6.5%  grid -0.01 kW  ESS -0.39 kW  enable=0 mode=0 limit=unset -> idle
    sleeping 303s

Three independent things are working in those four lines: the Sigen cloud
login, the Modbus read of the plant, and the schedule poll.

## Operating it

**Poke it the moment you plug the car in.** The first dispatch of a session
starts within a few minutes of the plug going in and runs only to the next
half-hour boundary, so it lands between the scheduled polls -- left alone you
catch its tail rather than the whole thing. That means SIGHUP:

```sh
kill -HUP <pid>
```

Within five seconds the log says `SIGHUP -- re-polling now`.

**Finding that pid is the awkward part.** `pgrep -f reconcile` does not work:
BusyBox does not match against full command lines, so it finds nothing and
reports nothing, which is indistinguishable from the agent being dead. Walk
`/proc` instead:

```sh
for p in /proc/[0-9]*; do
    case "$(tr '\0' ' ' < $p/cmdline 2>/dev/null)" in
        *python3*reconcile.py*) echo "${p#/proc/}" ;;
    esac
done
```

**That snippet matches itself.** Your own shell's command line contains the
string `reconcile.py`, so it turns up in its own results -- expect one extra
pid every time, and identify the real agent by its `python3` argument rather
than by counting. It is not a bug and it will catch you more than once.

**Reading the log**, what `.cloud-mode.json` means, and why you must never
`kill -9` are host-independent — see "Operating the agent" in
[`../README.md`](../README.md). The short version of the last one: the release
path is wired to SIGTERM, so a plain `kill` restores your mode on the way out
and `-9` leaves the plant on the charging profile until the deadman notices.

## What one night actually proved

Observed 2026-09-02 on the device named at the top, running
`--via-cloud --bonus-only --require-zappi`:

- two bonus dispatches caught and charged, the second with nobody watching;
- a real withdrawal -- provoked by unplugging the car mid-slot -- detected and
  the operational mode restored **eight seconds later**, the plant actually
  stopping within its 18-31 s actuation window;
- a scheduled release at a slot boundary, 30 s early as intended;
- ~10.5 hours continuous uptime with a wake lock held, and no kill by the skin;
- 95 deadman ticks, every one correctly a no-op;
- ~10.25 kW into the battery, the rate coming from the app profile.

What that establishes is that a phone is a viable host for **rung 2**. What it
does not: rung 3 remains a poor fit, because nothing here tested a latched
mode surviving a dead host. Nor does one night without a background kill mean
the kill will not come -- the battery-saver settings above are not made stale
by it.
