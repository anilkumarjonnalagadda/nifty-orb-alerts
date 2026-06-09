#!/usr/bin/env bash
#
# Stop the Nifty ORB monitor if it's running.
#
#   ./stop.sh
#
set -uo pipefail
cd "$(dirname "$0")"

SCRIPT=orb_monitor.py

if pgrep -f "$SCRIPT" >/dev/null; then
    pkill -f "$SCRIPT"
    sleep 2
    if pgrep -f "$SCRIPT" >/dev/null; then
        pkill -9 -f "$SCRIPT"
        sleep 1
    fi
    echo "Stopped $SCRIPT."
else
    echo "$SCRIPT is not running."
fi
