#!/bin/sh
# READ-ONLY. Reports whether a manual e2fsck is safe to attempt. Changes
# nothing, mounts nothing, stops nothing.
#
# Run as root from Task Scheduler (or `sudo sh fsck-preflight.sh`). It writes
# to /var/log/fsck-preflight.log, which lives on the SYSTEM partition (md0)
# rather than the volume being examined -- the same reason the real script
# logs there.
#
# The question it answers: e2fsck on a multi-terabyte ext4 filesystem needs
# RAM for its block and inode bitmaps, and an e2fsck that gets OOM-killed
# PART WAY THROUGH A REPAIR can leave the filesystem worse than it found it.
# That is the one outcome worse than doing nothing, so measure first.

VOLUME=${VOLUME:-/volume1}
LOG=/var/log/fsck-preflight.log

{
echo "================================================================"
echo "$(date): pre-flight for $VOLUME"
echo

if [ "$(id -u)" != "0" ]; then
    echo "NOT ROOT -- most of this will be blank. Run as root."
    echo
fi

DEV=$(awk -v m="$VOLUME" '$2 == m {print $1}' /proc/mounts | head -1)
echo "device:        ${DEV:-<not mounted>}"
echo "mounted:       $(awk -v m="$VOLUME" '$2 == m {print "yes, " $4}' /proc/mounts | head -1)"
echo

echo "--- array health (must be clean, no resync, before touching anything) ---"
grep -E "^md|blocks|recovery|resync" /proc/mdstat 2>/dev/null
echo

echo "--- filesystem ---"
# NOT dumpe2fs: it is not installed on DSM 7.4.1, which is why the first run
# of this script printed nothing here. tune2fs -l reports the same superblock
# fields and IS present.
if [ -n "$DEV" ]; then
    tune2fs -l "$DEV" 2>/dev/null | grep -iE \
        "Filesystem state|Errors behavior|Inode count|Block count|Block size|Free blocks|Free inodes|Last checked|Mount count|Filesystem features"
fi
echo

echo "--- memory, and whether e2fsck can fit ---"
free -m 2>/dev/null
echo
if [ -n "$DEV" ]; then
    # df works even with no ext2 tools at all, so it is the reliable source.
    INODES=$(df -i "$VOLUME" 2>/dev/null | awk 'NR==2 {print $2}')
    BLOCKS=$(df -B4096 "$VOLUME" 2>/dev/null | awk 'NR==2 {print $2}')
    AVAIL=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    if [ -n "$INODES" ] && [ -n "$BLOCKS" ]; then
        # e2fsck's fixed cost is bitmaps: roughly six over inodes (1 bit
        # each) and two or three over blocks (1 bit each), plus per-used-inode
        # structures. Order of magnitude, not a promise.
        EST=$(( (INODES / 8 * 6 + BLOCKS / 8 * 3) / 1048576 ))
        echo "inodes:        $INODES"
        echo "blocks:        $BLOCKS"
        echo "rough e2fsck working set: ~${EST} MB   (order of magnitude, not a guarantee)"
        echo "MemAvailable:  ${AVAIL} MB"
        if [ "$EST" -gt "$AVAIL" ]; then
            echo "VERDICT:       TIGHT OR INSUFFICIENT -- scratch_files is essential"
        else
            echo "VERDICT:       looks like it fits, but set scratch_files anyway"
        fi
    fi
fi
echo

echo "--- scratch_files (spills bitmaps to disk instead of being OOM-killed) ---"
if [ -f /etc/e2fsck.conf ]; then
    cat /etc/e2fsck.conf
else
    echo "/etc/e2fsck.conf does NOT exist -- the real script will create it"
fi
echo

echo "--- what is holding $VOLUME open right now ---"
if command -v fuser >/dev/null 2>&1; then
    fuser -vm "$VOLUME" 2>&1 | head -30
else
    echo "(no fuser; listing processes with a cwd or exe under $VOLUME)"
    for p in /proc/[0-9]*; do
        t=$(readlink "$p/cwd" 2>/dev/null)
        e=$(readlink "$p/exe" 2>/dev/null)
        case "$t$e" in
            *"$VOLUME"*) echo "  ${p#/proc/} $(cat "$p/comm" 2>/dev/null)";;
        esac
    done | head -30
fi
echo

echo "--- packages that will need stopping ---"
/usr/syno/bin/synopkg list --name 2>/dev/null | head -40
echo

echo "--- last e2fsck attempt, and why it stopped ---"
tail -3 /var/log/fsck/*.log 2>/dev/null
echo
echo "$(date): pre-flight done. NOTHING WAS CHANGED."
echo "================================================================"
} >> "$LOG" 2>&1

cat "$LOG"
