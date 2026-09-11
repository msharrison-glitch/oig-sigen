#!/bin/sh
# Manual e2fsck of a DSM data volume, for the case DSM's own File System Check
# refuses: "UNEXPECTED INCONSISTENCY; RUN fsck MANUALLY (i.e., without -a or
# -p options)". DSM runs e2fsck in PREEN mode, which by design declines
# anything needing a decision -- a corrupted orphan inode list is exactly that
# -- so the GUI button can never fix it, however many times it is pressed.
#
# Run as ROOT from DSM Task Scheduler. The command is just this script's path;
# it re-execs itself from /tmp and detaches, for two reasons that both matter:
#
#   - it lives on the volume it is about to unmount, so it must move off it
#     first (/tmp is tmpfs here, so it survives);
#   - stopping services can take the Task Scheduler down with it, and a
#     half-finished e2fsck is the worst possible outcome. setsid puts it in
#     its own session so nothing can take it with it.
#
# THE NAS GOES OFFLINE FOR HOURS AND REBOOTS AT THE END. On a multi-terabyte
# volume expect a long wait with no visible progress.
#
# Watch it from another machine:  tail -f /var/log/fsck-volume1.log
#
#   -y answers yes to everything. That is the correct answer for a corrupted
#   orphan list, but unrecoverable directory entries end up in lost+found with
#   numeric names. Files are not deleted; some may become hard to identify.

VOLUME=${VOLUME:-/volume1}
LOG=/var/log/fsck-volume1.log          # md0, NOT the volume being checked
LOCK=/var/run/fsck-volume1.lock
SELF_TMP=/tmp/.fsck-volume1-running.sh

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }

# ---------------------------------------------------------------- relaunch
case "$0" in
    "$SELF_TMP") ;;                    # already detached, carry on
    *)
        [ "$(id -u)" = "0" ] || { echo "must run as root" >&2; exit 1; }
        cp "$0" "$SELF_TMP" || exit 1
        chmod 700 "$SELF_TMP"
        log "relaunching detached from $SELF_TMP"
        setsid "$SELF_TMP" < /dev/null >> "$LOG" 2>&1 &
        echo "started; watch /var/log/fsck-volume1.log"
        exit 0
        ;;
esac

# ---------------------------------------------------------------- guards
exec >> "$LOG" 2>&1
log "================================================================"
log "manual e2fsck of $VOLUME starting"

if [ "$(id -u)" != "0" ]; then log "ABORT: not root"; exit 1; fi

if [ -e "$LOCK" ]; then
    log "ABORT: $LOCK exists -- another run may be in progress"
    exit 1
fi
echo $$ > "$LOCK"

DEV=$(awk -v m="$VOLUME" '$2 == m {print $1}' /proc/mounts | head -1)
if [ -z "$DEV" ]; then
    log "ABORT: $VOLUME is not mounted; refusing to guess the device"
    rm -f "$LOCK"; exit 1
fi
log "device: $DEV"

# Never repair on top of a degraded or rebuilding array: a rebuild rewrites
# blocks underneath e2fsck, and a degraded array has no parity in reserve if
# a read fails during a several-hour scan.
if grep -qE "recovery|resync" /proc/mdstat 2>/dev/null; then
    log "ABORT: an array is rebuilding or resyncing. Wait for it to finish."
    rm -f "$LOCK"; exit 1
fi
if grep -qE "\[[U_]*_[U_]*\]" /proc/mdstat 2>/dev/null; then
    log "NOTE: an array is not fully populated:"
    grep -E "^md|\[" /proc/mdstat >> "$LOG"
    log "NOTE: that is normal on a 4-bay with 3 disks; continuing."
fi

# Spill bitmaps to disk rather than be OOM-killed mid-repair. On a large
# filesystem with modest RAM this is the difference between a slow repair and
# a corrupted one.
if [ ! -f /etc/e2fsck.conf ]; then
    log "creating /etc/e2fsck.conf with scratch_files enabled"
    cat > /etc/e2fsck.conf <<'CONF'
[scratch_files]
directory = /var/cache/e2fsck
size = 0
CONF
    mkdir -p /var/cache/e2fsck
else
    log "/etc/e2fsck.conf already exists; leaving it alone:"
    cat /etc/e2fsck.conf >> "$LOG"
fi

log "--- state before ---"
free -m >> "$LOG" 2>&1
dumpe2fs -h "$DEV" 2>/dev/null | grep -iE "Filesystem state|Inode count|Block count|Last checked" >> "$LOG"

# ---------------------------------------------------------------- quiesce
log "--- stopping packages ---"
for pkg in $(/usr/syno/bin/synopkg list --name 2>/dev/null); do
    /usr/syno/bin/synopkg stop "$pkg" >/dev/null 2>&1 && log "  stopped $pkg"
done

log "--- stopping indexing and thumbnail daemons ---"
for d in synoindexd synomkthumbd synomkflvd synomkflvd synoindexplugind; do
    killall "$d" >/dev/null 2>&1 && log "  killed $d"
done

# ---------------------------------------------------------------- unmount
log "--- unmounting $VOLUME ---"
UNMOUNTED=no
i=1
while [ $i -le 6 ]; do
    if umount "$VOLUME" 2>>"$LOG"; then UNMOUNTED=yes; break; fi
    log "  attempt $i failed; killing holders"
    if command -v fuser >/dev/null 2>&1; then
        fuser -km "$VOLUME" >/dev/null 2>&1
    fi
    sleep 5
    i=$((i + 1))
done

# THE critical check. e2fsck on a MOUNTED read-write filesystem destroys it.
# Verify against /proc/mounts by both mountpoint and device, and treat any
# doubt as fatal.
if grep -qE "^$DEV |[[:space:]]$VOLUME " /proc/mounts; then
    log "ABORT: $VOLUME or $DEV is STILL MOUNTED. Refusing to run e2fsck."
    log "       Running e2fsck on a mounted filesystem would destroy it."
    log "       Rebooting to restore normal service; nothing was changed."
    grep -E "$DEV|$VOLUME" /proc/mounts >> "$LOG"
    rm -f "$LOCK"
    sync; sleep 5; reboot
    exit 1
fi
if [ "$UNMOUNTED" != "yes" ]; then
    log "ABORT: umount never reported success. Rebooting; nothing changed."
    rm -f "$LOCK"; sync; sleep 5; reboot; exit 1
fi
log "unmount confirmed -- $VOLUME is not in /proc/mounts"

# ---------------------------------------------------------------- repair
log "--- running e2fsck -fy $DEV  (hours; no progress output) ---"
START=$(date +%s)
e2fsck -fy "$DEV" >> "$LOG" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START ))
log "e2fsck finished after ${ELAPSED}s with exit code $RC"

case $RC in
    0)  log "RESULT: no errors remained." ;;
    1)  log "RESULT: errors were found and CORRECTED." ;;
    2)  log "RESULT: errors corrected; a reboot is required (doing that)." ;;
    4)  log "RESULT: errors remain UNCORRECTED. Needs a human -- do not"
        log "        assume the filesystem is now clean." ;;
    8)  log "RESULT: operational error in e2fsck itself." ;;
    16) log "RESULT: usage error -- the command was wrong, nothing done." ;;
    32) log "RESULT: cancelled." ;;
    *)  log "RESULT: unexpected exit code $RC." ;;
esac

log "--- state after ---"
dumpe2fs -h "$DEV" 2>/dev/null | grep -iE "Filesystem state|Last checked" >> "$LOG"

rm -f "$LOCK"
rm -f "$SELF_TMP"
log "rebooting in 10s to remount cleanly and restart packages"
log "================================================================"
sync
sleep 10
reboot
