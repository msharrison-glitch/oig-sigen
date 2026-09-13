#!/bin/sh
# Put DSM packages back after something stopped them all. Run as ROOT from
# Task Scheduler; `synopkg start` as a normal user only reaches the "prepare"
# stage and silently changes nothing.
#
# WHY THIS EXISTS, and the mistake not to repeat:
#
#   fsck-volume1.sh stopped every package to free the volume for unmounting:
#
#       for pkg in $(synopkg list --name); do synopkg stop "$pkg"; done
#
#   On DSM 7 `synopkg stop` ALSO CLEARS the package's `enabled` marker, and
#   that marker is what DSM reads at boot to decide what to start. So the
#   packages did not merely stop -- they were disabled, permanently, and the
#   NAS came back from a power cut two days later with almost nothing running.
#   The offsite backup was among them, so it had not run for two days.
#
#   The unmount never needed that loop. What actually worked was stopping the
#   systemd UNITS holding the volume (pgsql.service, synologand.service,
#   pkg-*.service). fsck-volume1.sh should stop only those; the blanket
#   synopkg loop is collateral damage with a long tail.
#
# Safe to run repeatedly: starting a running package is a no-op.
set -e

LOG=/var/log/restore-packages.log        # md0, survives a volume unmount

# Packages to leave alone. MediaServer was already disabled on this NAS before
# any of our work (checked 2026-09-07), so starting it would be us deciding
# something the owner had decided differently.
SKIP="MediaServer"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }

[ "$(id -u)" = "0" ] || { echo "must run as root" >&2; exit 1; }

{
echo "================================================================"
log "restoring packages"

SYNOPKG=/usr/syno/bin/synopkg
ALL=$("$SYNOPKG" list --name 2>/dev/null)

# Two passes. Packages depend on each other -- Photos needs
# SynologyApplicationService and a Node runtime -- and synopkg does not
# reorder them, so anything that failed because its dependency was not up
# yet gets a second chance.
pass() {
    n=$1
    log "--- pass $n ---"
    for pkg in $ALL; do
        case " $SKIP " in *" $pkg "*) continue ;; esac
        if [ -e "/var/packages/$pkg/enabled" ]; then
            continue                      # already restored
        fi
        "$SYNOPKG" start "$pkg" >/dev/null 2>&1 || true
        if [ -e "/var/packages/$pkg/enabled" ]; then
            log "  started  $pkg"
        else
            log "  FAILED   $pkg"
        fi
    done
}

pass 1
sleep 10
pass 2

log "--- final state ---"
for d in /var/packages/*/; do
    pkg=$(basename "$d")
    case " $SKIP " in *" $pkg "*) log "  skipped  $pkg"; continue ;; esac
    if [ -e "$d/enabled" ]; then
        log "  enabled  $pkg"
    else
        log "  STOPPED  $pkg"
    fi
done

log "done"
echo "================================================================"
} >> "$LOG" 2>&1

cat "$LOG" | tail -60
