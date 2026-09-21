#!/bin/sh
# Rotate the agent's log, monthly, WITHOUT restarting the agent.
#
# Run from DSM Task Scheduler as the owner (no root), daily is fine -- it
# does nothing on every day but the first of the month. Or monthly on the
# 1st, if the scheduler offers it.
#
# COPY AND TRUNCATE, NOT RENAME. reconcile.py holds the file open through a
# logging.FileHandler, which opens in append mode. Renaming the file would
# leave the agent writing happily into a file nobody reads again until it is
# restarted -- and restarting it mid-slot is exactly what this project avoids.
# Truncating in place is safe precisely because the handle is O_APPEND: the
# next write goes to the new end of file, byte 0, with no sparse hole.
#
# The archive name is the DATE OF ROTATION, not a claim about what is inside:
# the first rotation of a long-running log carries whatever had accumulated.
# config.log_files() globs these back in, oldest first, so costs.py,
# heatreport.py and the dashboard keep their history.
#
# Nothing is deleted, ever. A year of this log is a few tens of megabytes and
# it is the only durable record of which half hours were bonus slots --
# Octopus keeps completedDispatches for a few hours and Sigen never knew.
set -e

cd "$(dirname "$0")" || exit 1
LOG=${LOG:-observe.log}
TODAY=$(date +%Y-%m-%d)
DAY=$(date +%d)
ARCHIVE="${LOG%.log}-${TODAY}.log"

[ -f "$LOG" ] || { echo "no $LOG here ($(pwd))"; exit 0; }

# Only on the 1st, unless FORCE=1. Daily scheduling plus this test is more
# robust than trusting a monthly trigger to have fired.
if [ "$DAY" != "01" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "not the 1st ($TODAY) -- nothing to do"
    exit 0
fi

if [ -f "$ARCHIVE" ]; then
    echo "already rotated today -- $ARCHIVE exists"
    exit 0
fi

lines=$(wc -l < "$LOG")
if [ "$lines" -lt 100 ]; then
    echo "$LOG has only $lines lines -- too soon to rotate"
    exit 0
fi

cp "$LOG" "$ARCHIVE"
# Verify the copy before destroying the original. cp on a full volume fails
# silently often enough to be worth the two seconds.
copied=$(wc -l < "$ARCHIVE")
if [ "$copied" -lt "$lines" ]; then
    echo "copy is short ($copied of $lines lines) -- NOT truncating" >&2
    exit 1
fi

: > "$LOG"            # truncate in place; the agent keeps its handle
echo "rotated $lines lines into $ARCHIVE; $LOG is now empty"
