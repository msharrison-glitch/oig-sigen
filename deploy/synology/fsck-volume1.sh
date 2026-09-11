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
MARKER=.fsck-volume1-running.sh

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }

# Where can we copy ourselves to? Two requirements, and the first attempt at
# this got the second one wrong: the directory must NOT be on the volume we
# are about to unmount, and it must permit execution. /tmp satisfies the
# first and fails the second -- it is mounted noexec on DSM, so setsid
# reported "Permission denied" and the run died before doing anything.
pick_workdir() {
    for d in /var/tmp /root /usr/local/bin /dev/shm /tmp; do
        [ -d "$d" ] || continue
        case "$(df "$d" 2>/dev/null | awk 'NR==2 {print $6}')" in
            "$VOLUME"|"$VOLUME"/*) continue ;;   # would vanish at unmount
        esac
        t="$d/.fsck-exectest.$$"
        printf '#!/bin/sh\nexit 0\n' > "$t" 2>/dev/null || continue
        chmod 700 "$t" 2>/dev/null
        if "$t" 2>/dev/null; then rm -f "$t"; echo "$d"; return 0; fi
        rm -f "$t"
    done
    return 1
}

# ---------------------------------------------------------------- relaunch
case "$0" in
    *"$MARKER") ;;                     # already detached, carry on
    *)
        [ "$(id -u)" = "0" ] || { echo "must run as root" >&2; exit 1; }
        WORKDIR=$(pick_workdir) || {
            log "ABORT: no directory is both off $VOLUME and exec-capable"
            echo "no usable working directory; see $LOG" >&2
            exit 1
        }
        SELF_TMP="$WORKDIR/$MARKER"
        cp "$0" "$SELF_TMP" || exit 1
        chmod 700 "$SELF_TMP"
        if [ ! -x "$SELF_TMP" ]; then
            log "ABORT: $SELF_TMP is not executable after chmod"
            exit 1
        fi
        log "relaunching detached from $SELF_TMP (workdir $WORKDIR)"
        setsid "$SELF_TMP" < /dev/null >> "$LOG" 2>&1 &
        sleep 2
        # Only the tail: the log accumulates across attempts, and grepping
        # the whole file would match a PREVIOUS run's start line and report
        # success for a launch that never happened.
        if ! tail -5 "$LOG" 2>/dev/null | grep -q "manual e2fsck of $VOLUME starting"; then
            log "WARNING: the detached copy has not reported starting."
            log "         Check above for an exec error; nothing was changed."
        fi
        echo "started; watch $LOG"
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
tune2fs -l "$DEV" 2>/dev/null | grep -iE "Filesystem state|Inode count|Block count|Last checked" >> "$LOG"

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
# fuser and lsof are BOTH absent on DSM 7.4.1, so holders have to be found
# by walking /proc: cwd, exe, root and every open fd. Without this the unmount
# fails, the script aborts, and the NAS reboots having achieved nothing.
kill_holders() {
    sig=$1
    for pp in /proc/[0-9]*; do
        pid=${pp#/proc/}
        [ "$pid" = "$$" ] && continue
        [ "$pid" = "1" ] && continue
        hit=no
        for l in "$pp/cwd" "$pp/exe" "$pp/root"; do
            t=$(readlink "$l" 2>/dev/null)
            case "$t" in "$VOLUME"/*) hit=yes;; esac
        done
        if [ "$hit" = "no" ]; then
            for fd in "$pp"/fd/*; do
                t=$(readlink "$fd" 2>/dev/null) || continue
                case "$t" in "$VOLUME"/*) hit=yes; break;; esac
            done
        fi
        if [ "$hit" = "yes" ]; then
            kill -"$sig" "$pid" 2>/dev/null && \
                log "  kill -$sig $pid ($(cat "$pp/comm" 2>/dev/null))"
        fi
    done
}

# Killing a supervised process achieves nothing: DSM restarts it faster than
# the next umount attempt. The first real run lost this race six times --
# postgres and synologand came back with new PIDs each round. Stop the UNIT
# and systemd leaves it alone.
#
# Discovered from /proc/PID/cgroup rather than hardcoded, because the set
# differs by what is installed. sshd is excluded deliberately: stopping it
# would cut the only way to watch this, and a human shell holding the volume
# is dealt with by kill_holders instead.
stop_holder_units() {
    units=""
    for pp in /proc/[0-9]*; do
        hit=no
        for l in "$pp/cwd" "$pp/exe"; do
            t=$(readlink "$l" 2>/dev/null)
            case "$t" in "$VOLUME"/*) hit=yes;; esac
        done
        if [ "$hit" = "no" ]; then
            for fd in "$pp"/fd/*; do
                t=$(readlink "$fd" 2>/dev/null) || continue
                case "$t" in "$VOLUME"/*) hit=yes; break;; esac
            done
        fi
        [ "$hit" = "yes" ] || continue
        u=$(grep -oE "[a-zA-Z0-9_.@-]+\.service" "$pp/cgroup" 2>/dev/null | head -1)
        case "$u" in "" | sshd.service) continue ;; esac
        case " $units " in *" $u "*) ;; *) units="$units $u" ;; esac
    done
    for u in $units; do
        systemctl stop "$u" >/dev/null 2>&1 && log "  systemctl stop $u"
    done
    [ -n "$units" ]
}

log "--- stopping services that hold $VOLUME ---"
# The usual suspects first, by name, then whatever else is actually holding it.
for u in pgsql.service synologand.service synoindexd.service; do
    systemctl stop "$u" >/dev/null 2>&1 && log "  systemctl stop $u"
done
systemctl stop "pkg-*.service" >/dev/null 2>&1 && log "  stopped pkg-* units"
stop_holder_units

log "--- unmounting $VOLUME ---"
UNMOUNTED=no
i=1
while [ $i -le 8 ]; do
    if umount "$VOLUME" 2>>"$LOG"; then UNMOUNTED=yes; break; fi
    log "  attempt $i failed"
    # Units first every time -- something new may have started -- and only
    # then signal whatever is left that systemd does not own.
    stop_holder_units
    if [ $i -le 3 ]; then kill_holders TERM; else kill_holders KILL; fi
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
E2FSCK=$(command -v e2fsck || echo /sbin/e2fsck)
log "using $E2FSCK"
"$E2FSCK" -fy "$DEV" >> "$LOG" 2>&1
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
tune2fs -l "$DEV" 2>/dev/null | grep -iE "Filesystem state|Last checked" >> "$LOG"

rm -f "$LOCK"
rm -f "$0"        # the detached copy
log "rebooting in 10s to remount cleanly and restart packages"
log "================================================================"
sync
sleep 10
reboot
