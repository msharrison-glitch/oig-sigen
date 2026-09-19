#!/bin/sh
# Restart the energy dashboard. Run from ~/oig-sigen as the owner.
#
# A FILE, not an inline ssh one-liner, for the reason repoll.sh is a file:
# BusyBox pgrep cannot match a full command line, so finding the server means
# walking /proc -- and an inline command's own cmdline CONTAINS the pattern,
# so it matches itself and kills the shell doing the killing. Observed doing
# exactly that on 2026-09-18.
#
# Belt and braces: match the cmdline AND require the process to actually be
# the python interpreter, which a shell never is.
cd "$(dirname "$0")" || exit 1
PY=/usr/local/bin/python3.9
SHELLYS="192.168.2.16 192.168.2.43 192.168.2.149 192.168.2.158 192.168.2.191"

for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q 'dashboard\.py --serve' \
        || continue
    case "$(readlink "$p/exe" 2>/dev/null)" in
        *python*) ;;
        *) continue ;;
    esac
    pid=${p##*/}
    echo "stopping dashboard at pid $pid"
    kill "$pid" 2>/dev/null
done
sleep 2

args=""
for host in $SHELLYS; do args="$args --shelly $host"; done
setsid $PY -u dashboard.py --serve --bind 0.0.0.0 --port 8099 $args \
    < /dev/null >> dashboard.log 2>&1 &
sleep 3
for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q 'dashboard\.py --serve' \
        || continue
    case "$(readlink "$p/exe" 2>/dev/null)" in
        *python*) echo "started at pid ${p##*/}" ;;
    esac
done
