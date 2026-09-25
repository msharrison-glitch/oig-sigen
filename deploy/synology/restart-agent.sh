#!/bin/sh
# Restart the reconciliation agent. Run from ~/oig-sigen as the owner.
#
# A FILE, not an inline ssh one-liner, for the reason repoll.sh and
# restart-dashboard.sh are files: BusyBox pgrep cannot match a full command
# line, so finding the agent means walking /proc -- and an inline command's own
# cmdline CONTAINS the pattern, so it matches itself and kills the shell doing
# the killing. That trap has been hit five times now.
#
# Belt and braces: match the cmdline AND require the process to actually be the
# python interpreter, which a shell never is.
#
# REFUSES TO RUN WHILE THE PLANT IS HELD. The agent releases on SIGTERM, but a
# restart mid-slot is exactly what this project avoids: .cloud-mode.json is the
# only record of how to put the owner's mode back, and killing the process that
# owns it is how a plant gets stranded on a grid-charging profile. If either
# state file is present, run `python3 sigencloud.py --deadman` first, or simply
# wait for the slot to end -- the agent is idle between them.
cd "$(dirname "$0")" || exit 1
PY=/usr/local/bin/python3.9
PATTERN='reconcile\.py'

for f in .lease.json .cloud-mode.json; do
    if [ -f "$f" ]; then
        echo "REFUSING: $f exists, so something is held. Release it first." >&2
        exit 1
    fi
done

for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q "$PATTERN" || continue
    case "$(readlink "$p/exe" 2>/dev/null)" in
        *python*) ;;
        *) continue ;;
    esac
    pid=${p##*/}
    echo "stopping agent at pid $pid"
    # SIGTERM, never SIGKILL: the release and mode restore are wired to it.
    kill "$pid" 2>/dev/null
done
sleep 5

# Exactly the command the agent has been running. "OIG Charge" is one
# argument and the quotes matter.
setsid $PY -u reconcile.py --bonus-only --require-zappi --via-cloud \
    --charge-profile "OIG Charge" -v --log-file observe.log \
    < /dev/null >> agent-nohup.log 2>&1 &
sleep 4

found=""
for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q "$PATTERN" || continue
    case "$(readlink "$p/exe" 2>/dev/null)" in
        *python*) echo "started at pid ${p##*/}"; found=yes ;;
    esac
done
[ -n "$found" ] || { echo "FAILED to start -- see agent-nohup.log" >&2; exit 1; }
