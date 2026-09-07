# Running the agent on a Synology NAS

Verified here on a DS213j (Marvell Armada 370, ARMv7) under DSM 7.1.1. A
low-end two-bay NAS from over a decade ago is ample: the agent idles almost
all the time, and the work it does is a handful of Modbus reads.

A NAS earns its place for one reason — it does not sleep. A laptop lid closing
suspends the agent mid-slot, and on the Modbus path that leaves the plant
latched with nothing running to release it.

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
