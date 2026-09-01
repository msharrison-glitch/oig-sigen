#!/bin/sh
# Ask the running agent to re-poll the Octopus schedule NOW.
#
# Worth running the moment you plug the car in. The first dispatch of a
# charging session starts within a few minutes of the plug going in and runs
# only to the next half-hour boundary, so it can be most of the way over
# before the agent next looks of its own accord.
#
# Why not the obvious routes, all of which fail on DSM:
#   pgrep -f 'reconcile.py'  - BusyBox pgrep does not match the full command
#                              line. It finds nothing and says so silently,
#                              which is worse than an error.
#   systemctl show -p MainPID - returns empty on this DSM build.
#   systemctl kill -s HUP     - "Access denied" unless you are root.
#
# /proc always works, and the unit runs as the admin user, so the owner of
# the session can signal it directly with no sudo.
for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    if tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q 'reconcile\.py'; then
        pid=${p##*/}
        if kill -HUP "$pid" 2>/dev/null; then
            echo "sent SIGHUP to pid $pid -- re-polling the schedule now"
            exit 0
        fi
        echo "found agent at pid $pid but could not signal it" >&2
        exit 1
    fi
done
echo "agent is not running -- nothing to signal" >&2
exit 1
