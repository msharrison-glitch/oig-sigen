#!/bin/sh
# Append one heat pump observation. Safe to call as often as you like.
#
# Self-throttling on purpose. The Onecta API allows 200 requests per DAY per
# application, and there is no way to buy more: a task accidentally set to
# every 5 minutes would spend 288 and be refused for the rest of the day, with
# requests made while limited EXTENDING the block. So this refuses to poll
# more often than MIN_GAP regardless of how often it is invoked, which means
# the schedule can be wrong without being expensive.
#
# Run as the account that owns the project directory, NOT as root: the token
# is 0600 and the history files must stay writable by the same user that the
# agent runs as.
set -e

MIN_GAP=${MIN_GAP:-1700}          # seconds; 1700 = just under 29 minutes

HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/daikin.py" ]; then
    SRC="$HERE"
elif [ -f "$HERE/../../daikin.py" ]; then
    SRC=$(cd "$HERE/../.." && pwd)
else
    echo "daikin-snapshot: cannot find daikin.py from $HERE" >&2
    exit 1
fi

LOG="$SRC/daikin-snapshot.log"
HISTORY="$SRC/.daikin-history.jsonl"

# Nothing configured? Say so once and stay quiet -- this is optional, and a
# NAS without a heat pump should not accumulate errors forever.
if ! grep -q "^DAIKIN_CLIENT_ID=" "$SRC/.env" 2>/dev/null; then
    exit 0
fi

if [ -f "$HISTORY" ]; then
    now=$(date +%s)
    then_=$(date -r "$HISTORY" +%s 2>/dev/null || stat -c %Y "$HISTORY")
    gap=$((now - then_))
    if [ "$gap" -lt "$MIN_GAP" ]; then
        # Record the skip. A silent success is indistinguishable from a task
        # that never fired, and "did the scheduler actually run this?" is the
        # first question asked when no data appears.
        echo "$(date): skipped, last poll ${gap}s ago (min ${MIN_GAP}s)" \
            >> "$LOG"
        exit 0
    fi
fi

find_python() {
    for c in /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
             /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
             /usr/local/bin/python3.9 /usr/local/bin/python3 /usr/bin/python3; do
        [ -x "$c" ] || continue
        # daikin.py needs 3.9+ for the annotations it uses; it does NOT need
        # zoneinfo, unlike the rest of the project.
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            echo "$c"; return 0
        fi
    done
    return 1
}
PY=$(find_python) || {
    echo "$(date): no Python 3.9+ found" >> "$LOG"
    exit 0
}

cd "$SRC" || exit 0
if "$PY" daikin.py --snapshot >> "$LOG" 2>&1; then
    :
else
    echo "$(date): snapshot failed (see above)" >> "$LOG"
fi

# Keep the log from growing without bound; it is diagnostic, not a record.
# The actual data lives in .daikin-history.jsonl and .daikin-consumption.json.
if [ -f "$LOG" ]; then
    lines=$(wc -l < "$LOG")
    if [ "$lines" -gt 2000 ]; then
        tail -500 "$LOG" > "$LOG.trim" && mv "$LOG.trim" "$LOG"
    fi
fi
exit 0
