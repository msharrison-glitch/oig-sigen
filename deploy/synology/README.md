# Running the agent on a Synology NAS

Verified here on a DS213j (Marvell Armada 370, ARMv7) under DSM 7.1.1. A
low-end two-bay NAS from over a decade ago is ample: the agent idles almost
all the time, and the work it does is a handful of Modbus reads.

A NAS earns its place for one reason — it does not sleep. A laptop lid closing
suspends the agent mid-slot, and on the Modbus path that leaves the plant
latched with nothing running to release it.

> **Not here for the agent?** This directory also carries three scripts for
> repairing a DSM volume that Storage Manager's own File System Check cannot
> fix, and for undoing the package damage that doing so can cause. They are
> unrelated to charging a battery — see
> [Not the agent: repairing the NAS itself](#not-the-agent-repairing-the-nas-itself).

## What you need

No model is recommended over another; only one has been tested. Check a
candidate against these instead — the first two are the usual blockers.

| Requirement | Why |
|---|---|
| **Python 3.9 or newer** | `octopus.py` imports `zoneinfo` to get BST/GMT right. DSM's own Python 3 on DSM 6.x is 3.8 — too old. SynoCommunity's Python 3.11 covers DSM 6.x and 7.x, ARM and x86 |
| **A CPU with a usable Python** | `uname -m` giving `armv7l`, `aarch64` or `x86_64` is fine. `armv5tel` (Marvell Kirkwood, pre-2013) has none — stop there |
| **DSM 7, for the supervisor** | `install-service.sh` drives `systemctl` throughout. DSM 6 has no systemd; use the Task Scheduler boot-up task in `../README.md` instead |
| **`openssl` on PATH** | only for `--via-cloud` and the mode restore: `sigencloud.py` shells out to it because Python ships no AES. The Modbus-only path needs neither |
| **LAN reach to the plant, and outbound to `api.octopus.energy`** | no inbound access is required at all, so a NAS behind NAT with no port forwarding is a sound home |
| **Never sleeps** | the entire reason a NAS beats a laptop |

Check the first three in one go:

```sh
uname -m                # armv7l, aarch64, x86_64 ...
python3 --version       # DSM 7.4's own is 3.8 -- see below, this is NOT enough
openssl version         # only if you want --via-cloud
systemctl --version     # absent on DSM 6
```

Not needed: RAM, disk or CPU worth measuring. The agent runs in tens of MB and
sleeps between ticks.

**The unit is generated, not shipped.** `install-service.sh` renders
`oig-sigen.service.in` into the real unit each run, substituting what it finds
on *this* NAS: the interpreter, the account that owns the project directory,
and the directory itself. So no account name or Python path is baked in, and
running as something other than `admin` — which DSM 7 encourages — needs no
edit. State lives beside the scripts, so a deadman running as the wrong user
reads no `.lease.json` and silently protects nothing; deriving the user from
the directory's owner is what stops that happening.

Override any of it in `agent.conf` beside the scripts (gitignored, so it
survives an update): set `PY=`, `RUN_USER=` or `ARGS=`.

An earlier version of this file said both the unit and a `SRC=` line hardcoded
`/var/services/homes/admin/oig-sigen`. That was true, and it is why the first
install on a second NAS — a different user, and Python 3.13 rather than 3.9 —
would have produced a unit pointing at an interpreter that was not there.

## Why there is a supervisor script at all

DSM has systemd, and you *can* drive it over SSH — `ssh -t <host> 'sudo
systemctl restart oig-sigen'` works, the `-t` being what gives sudo a terminal
to prompt on. What you cannot do is that **unattended**, which is exactly what
a supervisor has to be: no terminal, nobody to type a password. DSM's own
**Task Scheduler** is the way round it, because it runs commands as root with
no prompt at all.

Create a scheduled task:

- Control Panel -> Task Scheduler -> Create -> Scheduled Task -> User-defined script
- **User: root**
- Schedule: daily, repeat **every 5 minutes**
- Command: `<project directory>/install-service.sh` — e.g.
  `/var/services/homes/admin/oig-sigen/install-service.sh`. Whatever path you
  use, `oig-sigen.service.in` must sit beside it and `reconcile.py` must be in
  that directory or two levels up; the script derives everything else.

`install-service.sh` is idempotent and does four things: installs the unit if
it changed, restarts only when it actually changed, starts the agent if it is
not running, and — unconditionally, last — runs both deadmen.

Running it every five minutes is not about the unit. It is about the deadmen.
They are the only thing that hands the plant back if the agent dies holding a
lease, so they must run on a schedule that does not depend on the agent.

## Updating code without root

Editing `reconcile.py` does not change the unit, so nothing would notice the
running process is executing stale code. Drop a marker instead:

    touch ~/oig-sigen/.restart-requested

The next run of the task picks it up and restarts within five minutes.

## Gotchas

- **DSM's own `python3` is too old, and it fails in the worst way.** On DSM
  7.4.1 `/usr/bin/python3` is 3.8, which has no `zoneinfo` — 3.9+ only — so
  `octopus.py` dies on the import. Under `Restart=always` that is a crash
  loop every `RestartSec`, forever, looking like a service that is running.
  Install **Python 3** from Package Center; it lands in `/usr/local/bin/` as
  `python3.9`, `python3.13` or similar depending on the version offered.
  `install-service.sh` picks the newest one that can actually `import
  zoneinfo`, testing the requirement rather than parsing a version string, so
  you do not have to tell it which. Measured 2026-09-07 on a DS920+ (DSM
  7.4.1): system 3.8.15 fails four of the eight suites on the import;
  `/usr/local/bin/python3.13` passes all eight.
- **`scp` fails** with `subsystem request failed on channel 0` — DSM serves no
  sftp subsystem. Use `scp -O`, or pipe: `cat f | ssh nas 'cat > path/f'`.
- **`ps` and `pgrep -f` are BusyBox** and will not find processes you know are
  running. Trust `systemctl is-active`, not a pgrep.
- **A `/proc` walk run inline over SSH can match itself.** `ssh nas '...for p
  in /proc/*; do ... reconcile.py ...'` puts the whole script into the remote
  shell's own `cmdline`, so the search finds that shell and a `kill` aimed at
  the agent terminates your session instead. `repoll.sh` is safe because it is
  a *file* — its cmdline is `sh repoll.sh` — but an inline one-liner is not.
  Hit four times now, most recently on 2026-09-18 restarting the dashboard,
  which is why `restart-dashboard.sh` exists and why it also checks that the
  process it is about to kill is really a Python one.
  For anything that signals the agent, take the pid from systemd rather than
  from string matching:
  ```sh
  systemctl show oig-sigen -p MainPID | cut -d= -f2
  ```
  Note DSM's systemd is old enough to reject `--value`, hence the `cut`.
- **`systemctl restart` needs root, but SIGTERM does not.** The unit runs as
  `admin` with `Restart=always`, so the owner can pick up new code with
  `kill -TERM <MainPID>` and systemd brings it back within `RestartSec`
  (30s). The agent handles signal 15 properly — it logs `caught signal 15`,
  runs its release path, and exits — so this is a clean restart, not a kill.
  Check nothing is held first.
- **Old DSM needs old crypto** to accept a modern OpenSSH client:
  `ssh -o Ciphers=+aes256-cbc -o HostKeyAlgorithms=+ssh-rsa <user>@<nas>`
  Those options belong in a `~/.ssh/config` block, which pins them to this
  host rather than weakening your client everywhere.
- **`sudo` over SSH needs `ssh -t`.** Without a terminal it fails with
  `sudo: a terminal is required to read the password`, which reads like an
  authentication failure and is not one.

## Re-polling the moment you plug in

The first dispatch of a session starts within a few minutes of the plug going
in and runs only to the next half-hour boundary, so it can be most of the way
over before the agent next looks. SIGHUP cuts the wait short:

    ~/oig-sigen/repoll.sh

The main README gives `kill -HUP $(pgrep -f 'reconcile.py')` for this, which
does **not** work here -- nor does `systemctl show -p MainPID` (empty on this
DSM build) or `systemctl kill -s HUP` (needs root). `repoll.sh` finds the pid
in /proc instead, and signals it as the admin user that owns it.

Verified: SIGHUP at 18:38:50, a fresh schedule poll and plant read completed
at 18:38:55.

Since 2026-09-05 the agent also accepts `--repoll`, which drops a file beside
the other state and then waits to watch the agent eat it -- so it tells you
whether anything was listening, where a signal into the void looks identical
to one that worked:

```sh
cd ~/oig-sigen && /usr/local/bin/python3.13 reconcile.py --repoll   # your path may differ
```

Run it from the same directory and as the same user as the agent, or the two
disagree about where the file lives -- the same hazard as the deadman's
`WorkingDirectory`. `repoll.sh` remains shorter to type and works fine.

## Without the supervisor: Task Scheduler alone

If you would rather not install a systemd unit, DSM's Task Scheduler can run
the whole thing. Three entries, all as a user that owns the directory:

| Task | Type | Runs |
|---|---|---|
| agent | Triggered Task → Boot-up | `cd /volume1/oig-sigen && nohup $PY reconcile.py --bonus-only --require-ev >/dev/null 2>&1 &` |
| deadman | Scheduled Task, every 5 min | `cd /volume1/oig-sigen && $PY control.py --deadman` |
| cloud deadman | Scheduled Task, every 5 min | `cd /volume1/oig-sigen && $PY sigencloud.py --deadman` |

**Write the real interpreter into each of those three, not `python3`.** These
tasks bypass `install-service.sh`, so nothing detects it for you, and bare
`python3` is DSM's 3.8. Find the path once:

```sh
for c in /usr/local/bin/python3.1? /usr/local/bin/python3.9; do
    [ -x "$c" ] && "$c" -c 'import zoneinfo' 2>/dev/null && echo "$c"
done
```

`--require-ev` is the charger-agnostic gate: it takes Octopus's own
`completedDispatches` as evidence the car is drawing, so it works with any
IOG-compatible charger. It lags by up to half an hour. myenergi owners can use
`--require-zappi` instead, which asks the charger directly and does not lag —
that is what the shipped unit uses, and it is the one thing in that file you
should expect to change.

State lives beside the scripts, so run all three as the same user: a deadman
that cannot read `.lease.json` silently protects nothing.

**No Docker on ARM models.** DSM's Docker/Container Manager package is x86
only, so ignore the Dockerfile. Copy the `.py` files and `.env` to a share and
run them directly — being dependency-free, there is nothing else to install.

**If you use the supervisor, copy `install-service.sh` and
`oig-sigen.service.in` together.** The script renders the unit from the
template beside it, so the template is not optional documentation — it is an
input. Copying only the script is the likelier mistake because that is all the
older flow needed. It now fails loudly into `service-status.txt` rather than
half-running: without the guard, `sed` would fail, `set -e` would exit, and
*both deadmen at the end of the script would never run* — every five minutes,
indefinitely, while the task itself looked fine.

## Rotating the log

`observe.log` grows forever: 600-1200 lines a day, and it is read whole by
`costs.py` and `heatreport.py` every time they run. `rotate-log.sh` copies it
to `observe-YYYY-MM-DD.log` and empties the original.

Task Scheduler, **user-defined script as the owner**, daily — the script
itself does nothing on any day but the 1st, which is more reliable than
trusting a monthly trigger to fire:

```sh
sh ~/oig-sigen/rotate-log.sh          # FORCE=1 to rotate now
```

**It copies and truncates rather than renaming, and that is the whole
point.** The agent holds the file open through a `logging.FileHandler`.
Renaming leaves it writing into a file nobody reads again until it restarts,
and restarting it mid-slot is what this project avoids everywhere else.
Truncating in place is safe because that handle is append-mode: the next
write lands at byte 0 with no sparse hole. Verified 2026-09-21 against a real
`FileHandler`.

It refuses to truncate if the copy came out short, and it will not rotate a
log under 100 lines or rotate twice in a day.

**Nothing is ever deleted.** A year is a few tens of megabytes, and this log
is the only durable record of which half hours were bonus slots: Octopus
keeps `completedDispatches` for hours, and Sigen never knew they existed.
`config.log_files()` globs the archives back in, oldest first, so the
dashboard, `costs.py` and `heatreport.py` keep their history across a
rotation.

## Optional: the energy dashboard

`dashboard-start.sh` runs `dashboard.py --serve` on port 8099 and publishes it
to the tailnet with `tailscale serve`. Run it from Task Scheduler as **root**,
triggered *Boot-up*: the serve step needs root, and Task Scheduler already is,
which sidesteps `sudo` having no terminal over SSH. It starts the server as
the owner, so the state files it reads stay owned by the owner. Idempotent —
running it again when the server is already up only re-checks the tailnet.

`restart-dashboard.sh` picks up new code. Run it as the **owner**, no root:

```sh
sh ~/oig-sigen/restart-dashboard.sh
```

It stops whatever is serving and starts it again from the current files. The
agent is untouched — it does not import `dashboard.py` — so this is safe
mid-slot, where restarting the agent is not.

**Both are files rather than inline commands, and that is the point.** Each
has to find the running server by walking `/proc`, because BusyBox `pgrep -f`
matches nothing here. An inline `ssh nas '...dashboard.py...'` puts that
pattern into the remote shell's own cmdline, so the search finds the shell and
kills the session instead of the server. That happened on 2026-09-18: the
restart killed its own shell, the old server kept serving, and the deploy
looked done while the new code was not running. `restart-dashboard.sh` also
requires the process's `exe` to be Python, which a shell never is.

## Optional: observing the heat pump

`daikin-snapshot.sh` appends one Daikin Onecta observation to
`.daikin-history.jsonl`, and folds that poll's monthly consumption into
`.daikin-consumption.json`. Read-only -- `daikin.py` has no write path at all,
and its test asserts that against the source.

A **separate** Task Scheduler entry, not part of the supervisor:

- Control Panel -> Task Scheduler -> Create -> Scheduled Task -> User-defined
- **User: the account that owns the project directory** (NOT root). The token
  is 0600 and the history files must stay writable by the same user the agent
  runs as; a root-owned history file locks the owner out of appending to it.
- Schedule: daily, repeat **every 30 minutes**, last run time **23:55**
- Command: `<project directory>/daikin-snapshot.sh`

**Why it is separate from `install-service.sh`.** That script is the deadman,
and it runs every five minutes. Polling Daikin 288 times a day would exceed
their 200/day limit before lunch, and mixing an optional feature into the one
script that hands the plant back is not a trade worth making.

**The budget is the reason for the throttle.** The Onecta API allows 200
requests per day per application, cannot be raised, and requests made while
rate-limited *extend* the block. So the script refuses to poll more often than
`MIN_GAP` (1700s, just under 29 minutes) no matter how often it is invoked --
the schedule can be wrong without being expensive. Thirty-minute polling costs
48 calls a day.

It exits 0 and does nothing if `DAIKIN_CLIENT_ID` is absent from `.env`, so a
NAS without a heat pump is unaffected.

## Stopping it again is not just `kill`

With the supervisor installed the agent is a systemd unit with
`Restart=always` and `RestartSec=30`, *and* a Task Scheduler entry re-runs
`install-service.sh`, which re-enables and restarts it. Killing the process
buys about thirty seconds.

`systemctl mask` does not help either. It fails with `File exists`, because
the installer puts a **real file** at `/etc/systemd/system/oig-sigen.service`
and mask needs to create a symlink there.

What works, in this order:

```sh
mv ~/oig-sigen/install-service.sh ~/oig-sigen/install-service.sh.disabled
sudo systemctl stop oig-sigen        # needs ssh -t
systemctl is-active oig-sigen        # want: inactive
```

The rename needs no root, since the agent's own user owns that script.
`Restart=always` does not fire after an explicit `systemctl stop`, so with the
periodic supervisor out of the way the stop sticks. Rename it back to undo.

**But that is not enough, and an earlier version of this section said it was.**
There is a SECOND Task Scheduler entry -- the **Boot-up** task, the one in the
table above that launches the agent. It does not go through
`install-service.sh`, so renaming that script does nothing to it, and it fires
on every power-on.

Observed 2026-09-03: a smart plug power-cycled, taking the router and the NAS
with it. The NAS rebooted, the boot-up task started the agent two minutes
later, and it ran for the next 25 hours -- defeating both the rename and a
`systemctl stop` that had been confirmed `inactive` an hour earlier. Nobody
noticed, because everything that reports on the agent is the agent.

It then fought a second controller on another host all night and reset the
plant to the charging profile twice, three seconds after the other agent had
correctly restored it.

So to genuinely stop it, **disable the boot-up task in Control Panel → Task
Scheduler as well** -- untick it, do not delete it -- and remember to re-enable
it afterwards. A stop you cannot see is worse than no stop, because you plan
around it.

This matters most when moving the agent to another host — two must never run
at once, and `reconcile.py`'s own guard is a local pid check that cannot see
across machines.

## Security note on older DSM

DSM 6.2 stopped getting updates some years ago. The agent puts your Octopus
API key, and optionally your mySigen password, in a file on that machine. With
no inbound exposure the risk is small, but it is a new class of secret on an
unpatched box — worth deciding rather than defaulting into. The Modbus-only
path needs no Sigen credentials at all.

## Choosing the actuation path

The unit here is on `--via-cloud`, which selects a pre-built energy profile in
the mySigen app instead of taking a Remote EMS lease. **You must create that
profile yourself** and pass its name; `sigencloud.py --list` shows what your
account has. Nothing latches at the plant on this path, so a host that dies
mid-slot stops charging rather than stranding the plant — but the charge rate
is whatever the profile says, not `--kw`.

For the Modbus lease path instead, drop `--via-cloud --charge-profile` and add
`--kw <rate>`. That gives per-command control of the rate, at the cost of the
mode revert documented in the main README.

## Day-to-day

Reading the log, what `.lease.json` and `.cloud-mode.json` mean, and why you
must never `kill -9` are host-independent — see "Operating the agent" in
[`../README.md`](../README.md).

---

## Not the agent: repairing the NAS itself

Three scripts here have nothing to do with charging a battery. They are kept
because **DSM cannot do this job itself**, and because a NAS that will not
mount its volume is not going to run the agent either.

### Why DSM's own File System Check cannot fix a corrupt volume

DSM runs `e2fsck` in **preen** mode (`-p`), which by design refuses anything
needing a decision. A corrupted orphan inode list is exactly that. So a volume
reporting

```
UNEXPECTED INCONSISTENCY; RUN fsck MANUALLY (i.e., without -a or -p options)
```

can never be repaired from Storage Manager, however many times you press the
button — it runs for about a minute and reports "Unable to complete ...
because errors occurred". On the NAS this was written for, `/volume1` had been
corrupt since **February 2021** and every check since had failed that way. A
manual `e2fsck -fy` fixed it in one pass: 519 repairs, `lost+found` empty.

### The three scripts, in the order you use them

| Script | Root? | What it does |
|---|---|---|
| `fsck-preflight.sh` | yes | **READ-ONLY.** Reports whether a manual `e2fsck` is safe to attempt. Mounts nothing, stops nothing, changes nothing. **Run this first.** |
| `fsck-volume1.sh` | yes | The real repair. Stops the services holding the volume, unmounts it, runs `e2fsck -fy`, remounts. Not a casual thing to run. |
| `restore-packages.sh` | yes | Re-enables packages afterwards. Safe to run repeatedly. |

All three log to `/var/log/`, which is on the system partition (`md0`) rather
than the volume being worked on — a log written to the volume you are about to
unmount is a log you cannot read when it matters.

Run them from **Task Scheduler as root**, not over SSH: the command is just
the script's path.

### `synopkg stop` disables a package, it does not merely stop it

This is the trap, and it cost two days here. `synopkg stop` **clears the
package's `enabled` marker**, and that marker is what DSM reads at boot to
decide what to start. So a loop like

```sh
for pkg in $(synopkg list --name); do synopkg stop "$pkg"; done
```

does not stop 25 packages — it *disables* them, permanently. The NAS came back
from an unrelated power cut two days later running almost nothing, and the
owner's offsite backup had silently not run in that time. Nobody noticed,
because a package that was never scheduled to start does not log an error.

**To free a volume, stop the systemd units instead:**

```sh
systemctl stop pkg-*.service pgsql.service synologand.service
```

Same effect on the mount, markers untouched. `fsck-volume1.sh` does it this
way now; `restore-packages.sh` exists to repair databases damaged by the
earlier version.

One more reason this stayed invisible: **`synopkg start` as a non-root user
returns `{"success":true}`** at the "prepare" stage and changes nothing at
all. It looks like it worked.

### DSM facts these scripts encode, which will cost you hours otherwise

- **`/tmp` is mounted `noexec`.** Anything you write there and try to run
  fails, including `setsid` wrappers. Pick a working directory that permits
  execution and is not on the volume you are unmounting.
- **`dumpe2fs`, `fuser` and `lsof` are not installed.** `tune2fs` and `df`
  are, and you can walk `/proc/*/fd` for open files.
- **Killing a supervised process achieves nothing** — DSM respawns it. Stop
  the unit.
- **Kernel `sataN` names re-enumerate between boots and do NOT match DSM's
  Drive numbers.** The failing disk here was DSM "Drive 2" but kernel `sata3`.
  Following the kernel name would have pulled a **healthy** drive out of a
  degraded array. Identify the disk by serial number, in Storage Manager.
- **SMART self-tests log internally, not to `dmesg`.** A clean `dmesg` after a
  test is not evidence of a clean test; read the SMART log itself. Believing
  otherwise here meant declaring a disk healthy that shed 14 more bad sectors
  during the next fsck.
