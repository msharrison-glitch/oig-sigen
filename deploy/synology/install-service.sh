#!/bin/sh
# Supervisor + deadman. Safe to run once, at boot, or every few minutes.
#
#   - installs/updates the unit, restarting ONLY when it actually changed
#   - starts the agent if it is not running
#   - runs the deadman, which releases the plant if a lease has outlived its
#     TTL. That is the one thing that must happen even if the agent is gone,
#     so it runs unconditionally and last.
set -e
SRC=/var/services/homes/admin/oig-sigen
PY=/usr/local/bin/python3.9
OUT="$SRC/service-status.txt"

CHANGED=0

# A restart can be requested without root: drop a .restart-requested file in
# the project directory and the next run of this task picks it up. Needed
# because updating reconcile.py does not change the unit, so nothing would
# otherwise notice that the running process is executing stale code.
if [ -f "$SRC/.restart-requested" ]; then
    CHANGED=1
    rm -f "$SRC/.restart-requested"
fi

if ! cmp -s "$SRC/oig-sigen.service" /etc/systemd/system/oig-sigen.service 2>/dev/null; then
    install -m 644 "$SRC/oig-sigen.service" /etc/systemd/system/oig-sigen.service
    systemctl daemon-reload
    CHANGED=1
fi
systemctl enable oig-sigen >/dev/null 2>&1 || true

if [ "$CHANGED" = "1" ]; then
    echo "$(date): unit changed, restarting" > "$OUT"
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
chown admin "$OUT" 2>/dev/null || true
