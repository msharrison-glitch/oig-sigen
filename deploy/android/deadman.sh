#!/data/data/com.termux/files/usr/bin/sh
# Put the owner's operational mode back if a charge selection has outlived
# its slot.
#
# Run by Android's JobScheduler, deliberately NOT from inside a Termux
# session. The whole point is that it survives Termux being killed -- which
# on MIUI is routine, and is precisely the moment the agent cannot clean up
# after itself. A deadman sharing a process tree with the thing it guards is
# not a deadman.
#
# Idempotent and a no-op when nothing is owed. Always exits 0, so a transient
# failure does not get the job deregistered.
cd /data/data/com.termux/files/home/oig-sigen || exit 0
echo "$(date '+%Y-%m-%d %H:%M:%S') deadman tick" >> deadman.log
python3 sigencloud.py --deadman >> deadman.log 2>&1
exit 0
