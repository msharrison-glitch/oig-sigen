#!/bin/sh
# Start the energy dashboard and publish it to the tailnet. Run as ROOT from
# DSM Task Scheduler, triggered "Boot-up", and again whenever you want to
# restart it by hand.
#
# WHY A SCRIPT AND NOT TWO COMMANDS IN THE TASK BOX:
#
#   `tailscale serve` requires root. Over SSH that means sudo, and sudo
#   without a terminal fails with "a terminal is required to read the
#   password" -- which reads like an authentication problem and is not one.
#   Task Scheduler already runs as root, so it sidesteps the whole thing.
#
# ON BINDING. Synology's Tailscale runs in USERSPACE mode -- there is no
# tailscale0 interface, so the tailnet address is not assignable and
# `--bind 100.x.y.z` fails with "Cannot assign requested address". Tailscale
# Serve is the bridge instead: it accepts tailnet traffic and proxies to
# 127.0.0.1. See the BIND note below for what this does and does not expose.
#
# Reachable two ways, deliberately:
#   https://diskstation.<tailnet>.ts.net   from anywhere, over Tailscale
#   http://192.168.2.18:8099               from the home LAN
# NOT reachable from the internet: Tailscale Funnel is off and nothing is
# port-forwarded.
#
# Idempotent: safe to run repeatedly.
set -e

DIR=/var/services/homes/admin/oig-sigen
USER_NAME=admin
PY=/usr/local/bin/python3.9
TS=/var/packages/Tailscale/target/bin/tailscale
PORT=8099

# 0.0.0.0, and it has to be. Tailscale Serve proxies to 127.0.0.1, so
# binding only to the LAN address (192.168.2.18) would leave Serve with
# nothing to talk to and break phone access. Binding only to loopback --
# which is where this started -- makes it unreachable from the LAN. The
# NAS has exactly two interfaces, lo and eth0, and userspace Tailscale adds
# none, so 0.0.0.0 here means "loopback and the LAN" and nothing else.
#
# NOTE what that means: anything on your LAN can read this page, with no
# authentication. Far smaller than internet exposure, but a live house-load
# feed is an OCCUPANCY SIGNAL -- sub-100 W for hours means nobody is home.
# Worth remembering before putting untrusted devices on the same network.
BIND=0.0.0.0
LOG=/var/log/oig-dashboard.log        # md0, survives a volume unmount

# The circuits to poll. Names come from .shelly-labels.json beside the code.
SHELLYS="192.168.2.16 192.168.2.43 192.168.2.149 192.168.2.158 192.168.2.191"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }

[ "$(id -u)" = "0" ] || { echo "must run as root" >&2; exit 1; }

{
log "starting dashboard"

# --- already running? -----------------------------------------------------
# `ps` and `pgrep -f` are BusyBox here and will not match a full command
# line, so walk /proc. Match on the SCRIPT NAME only: matching a longer
# string would also match this script's own cmdline and we would find
# ourselves. See deploy/synology/README.md.
running=""
for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    if tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q 'dashboard\.py'; then
        case "$(tr '\0' ' ' < "$p/cmdline")" in
            *dashboard-start*) continue ;;      # this script, not the server
        esac
        running="${p#/proc/}"
        break
    fi
done

if [ -n "$running" ]; then
    log "  already running at pid $running"
else
    args=""
    for host in $SHELLYS; do args="$args --shelly $host"; done
    # su to the owner: the dashboard reads .env, .shelly-labels.json and the
    # agent's observe.log, all of which belong to $USER_NAME at 0600. Running
    # it as root would work and then leave root-owned state behind.
    su - "$USER_NAME" -c \
        "cd $DIR && setsid $PY dashboard.py --serve --bind $BIND \
         --port $PORT $args < /dev/null >> $DIR/dashboard.log 2>&1 &"
    sleep 4
    log "  started on $BIND:$PORT"
fi

# --- publish to the tailnet ----------------------------------------------
if [ -x "$TS" ]; then
    if "$TS" serve status 2>/dev/null | grep -q "$PORT"; then
        log "  tailscale serve already configured"
    else
        if "$TS" serve --bg "$PORT" >> "$LOG" 2>&1; then
            log "  tailscale serve configured for port $PORT"
        else
            log "  tailscale serve FAILED -- is Serve enabled on the tailnet?"
        fi
    fi
    "$TS" serve status 2>&1 | sed 's/^/  /' >> "$LOG"
else
    log "  no tailscale binary at $TS -- skipping"
fi

log "done"
} >> "$LOG" 2>&1

tail -25 "$LOG"
