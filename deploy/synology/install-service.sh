#!/bin/sh
# Supervisor + deadman. Safe to run once, at boot, or every few minutes.
#
#   - works out this NAS's interpreter, user and directory rather than
#     assuming the ones the author happened to have
#   - renders and installs/updates the unit, restarting ONLY when it changed
#   - starts the agent if it is not running
#   - runs the deadman, which releases the plant if a lease has outlived its
#     TTL. That is the one thing that must happen even if the agent is gone,
#     so it runs unconditionally and last.
set -e

# Two different directories, and conflating them breaks the deadman silently.
# HERE is where this script and its template live. SRC is the project root,
# identified by reconcile.py being in it -- that is the directory the deadman
# must run in, because control.py reads .lease.json beside itself. On the NAS
# this script is copied to the project root and the two coincide; in a full
# checkout it sits in deploy/synology, two levels down. Handle both.
HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/reconcile.py" ]; then
    SRC="$HERE"
elif [ -f "$HERE/../../reconcile.py" ]; then
    SRC=$(cd "$HERE/../.." && pwd)
else
    echo "install-service.sh: cannot find reconcile.py from $HERE" >&2
    exit 1
fi
OUT="$SRC/service-status.txt"

# Run as the account that owns the tree, not a name baked in here. The deadman
# reads .lease.json beside control.py, so a mismatch here is a silent failure
# of the one mechanism protecting against a latched charge.
RUN_USER=$(stat -c %U "$SRC" 2>/dev/null || echo admin)

# The agent needs zoneinfo, which is 3.9+. DSM's own /usr/bin/python3 is 3.8
# on 7.4 and will crash-loop on the import every RestartSec forever, so test
# the real requirement rather than parsing a version string. Highest first.
find_python() {
    for c in /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
             /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
             /usr/local/bin/python3.9 /usr/local/bin/python3 /usr/bin/python3; do
        [ -x "$c" ] || continue
        if "$c" -c 'import zoneinfo, sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            echo "$c"; return 0
        fi
    done
    return 1
}

ARGS='--bonus-only --require-zappi --via-cloud --charge-profile "OIG Charge" -v --log-file observe.log'

# Optional per-NAS overrides: PY, RUN_USER, ARGS. Gitignored, so an owner's
# local choices survive an update of the tree.
[ -f "$SRC/agent.conf" ] && . "$SRC/agent.conf"

if [ -z "$PY" ]; then
    PY=$(find_python) || {
        echo "$(date): no Python 3.9+ with zoneinfo found. Install Python 3 from" \
             "Package Center, or set PY= in $SRC/agent.conf" > "$OUT"
        chown "$RUN_USER" "$OUT" 2>/dev/null || true
        exit 1
    }
fi

CHANGED=0

# A restart can be requested without root: drop a .restart-requested file in
# the project directory and the next run of this task picks it up. Needed
# because updating reconcile.py does not change the unit, so nothing would
# otherwise notice that the running process is executing stale code.
if [ -f "$SRC/.restart-requested" ]; then
    CHANGED=1
    rm -f "$SRC/.restart-requested"
fi

# The template must travel with this script. If it does not, sed fails, set -e
# exits, and the two deadmen at the bottom never run -- every five minutes,
# forever, while looking healthy. Fail loudly into the status file instead.
if [ ! -f "$HERE/oig-sigen.service.in" ]; then
    echo "$(date): oig-sigen.service.in missing beside $0 -- copy BOTH files" > "$OUT"
    chown "$RUN_USER" "$OUT" 2>/dev/null || true
    exit 1
fi

RENDERED="$SRC/.oig-sigen.service.rendered"
sed -e "s|@PY@|$PY|g" -e "s|@USER@|$RUN_USER|g" \
    -e "s|@DIR@|$SRC|g" -e "s|@ARGS@|$ARGS|g" \
    "$HERE/oig-sigen.service.in" > "$RENDERED"

if ! cmp -s "$RENDERED" /etc/systemd/system/oig-sigen.service 2>/dev/null; then
    install -m 644 "$RENDERED" /etc/systemd/system/oig-sigen.service
    systemctl daemon-reload
    CHANGED=1
fi
systemctl enable oig-sigen >/dev/null 2>&1 || true

if [ "$CHANGED" = "1" ]; then
    echo "$(date): unit changed, restarting (python $PY, user $RUN_USER)" > "$OUT"
    systemctl restart oig-sigen
    sleep 3
elif systemctl is-active --quiet oig-sigen; then
    echo "$(date): healthy, left alone" > "$OUT"
else
    echo "$(date): was not running, starting" > "$OUT"
    systemctl start oig-sigen
    sleep 3
fi
systemctl status oig-sigen --no-pager >> "$OUT" 2>&1 || true

# Two deadmen, and both matter. control.py hands the plant back if a lease
# outlived its TTL; sigencloud.py puts the owner's operational mode back,
# because releasing Remote EMS always drops the plant to Maximum Self-Powered
# whatever they had selected. Without the second one, a crash silently costs
# them their mode -- which is precisely when they are least likely to notice.
cd "$SRC" || exit 0
"$PY" control.py --deadman >> "$OUT" 2>&1 || true
"$PY" sigencloud.py --deadman >> "$OUT" 2>&1 || true
chown "$RUN_USER" "$OUT" 2>/dev/null || true
