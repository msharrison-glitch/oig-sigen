#!/bin/sh
# Record one reading from every Shelly channel. Run as the OWNER (not root)
# from DSM Task Scheduler, every 5 minutes.
#
# WHY: a Shelly plug keeps a lifetime kWh counter and nothing else -- no
# yesterday, no last week. Per-period consumption can only be reconstructed
# by DIFFERENCING that counter, which means somebody has to write the
# readings down, and nothing else will. An hour not recorded is an hour that
# cannot be recovered later.
#
# The Pro 3EM is included even though it keeps two months of its own minute
# data: reading its lifetime counter is one fast call, where walking that
# history costs ~46 calls per channel per day and took 150 s for a single
# day. The device's own archive stays there for anything needing the detail.
#
# Cheap and idempotent: one line per channel, appended, no rewrite.
set -e

DIR=/var/services/homes/admin/oig-sigen
PY=/usr/local/bin/python3.9
SHELLYS="192.168.2.16 192.168.2.43 192.168.2.149 192.168.2.158 192.168.2.191"

cd "$DIR" || exit 1
args=""
for host in $SHELLYS; do args="$args --shelly $host"; done

# A device asleep or briefly unreachable is skipped and picked up next run,
# so this must not fail the task.
$PY dashboard.py --record $args 2>&1 || true
