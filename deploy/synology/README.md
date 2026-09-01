# Running the agent on a Synology NAS

Proven on a DS213j (Marvell Armada 370, ARMv7) under DSM 7.0.1. An eight-year
-old two-bay NAS is ample: the agent is idle almost all the time, and the work
it does is a handful of Modbus reads.

A NAS earns its place here for one reason — it does not sleep. A laptop lid
closing suspends the agent mid-slot, and on the Modbus path that leaves the
plant latched with nothing running to release it.

## Why there is a supervisor script at all

DSM has systemd, but an `admin` SSH session cannot `sudo` without a TTY
password prompt, so you cannot install or restart a unit from a script. The
way round it is DSM's own **Task Scheduler**, which runs commands as root.

Create a scheduled task:

- Control Panel -> Task Scheduler -> Create -> Scheduled Task -> User-defined script
- **User: root**
- Schedule: daily, repeat **every 5 minutes**
- Command: `/var/services/homes/admin/oig-sigen/install-service.sh`

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

- **Python is not there by default.** Install Python 3.9 from Package Center
  (community). It lands at `/usr/local/bin/python3.9`, which is why the unit
  names that path explicitly rather than `python3`.
- **`scp` fails** with `subsystem request failed on channel 0` — DSM serves no
  sftp subsystem. Use `scp -O`, or pipe: `cat f | ssh nas 'cat > path/f'`.
- **`ps` and `pgrep -f` are BusyBox** and will not find processes you know are
  running. Trust `systemctl is-active`, not a pgrep.

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
- **Old DSM needs old crypto** to accept a modern OpenSSH client:
  `ssh -o Ciphers=+aes256-cbc -o HostKeyAlgorithms=+ssh-rsa admin@<nas>`

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
