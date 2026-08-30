#!/bin/sh
# Release before we start, then run the loop.
#
# A container that restarts after a crash may be inheriting a lease its dead
# predecessor took out -- the plant has no watchdog, so that command is still
# latched. Running the deadman first makes a restart self-healing instead of
# a way to lose track of the plant.
set -e

echo "entrypoint: checking for a lease left by a previous run"
python3 /app/control.py --deadman || \
    echo "entrypoint: deadman failed; continuing (the loop reconciles anyway)"

echo "entrypoint: starting agent"
exec python3 /app/reconcile.py "$@"
