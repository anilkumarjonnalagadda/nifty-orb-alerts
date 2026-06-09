#!/usr/bin/env bash
#
# Restart the Nifty ORB monitor: stop any running instance, then start a fresh
# one with python3 under nohup. Safe to run every morning.
#
#   ./start.sh
#
# Logs append to orb.log with a dated banner per run. If today's Kite token
# isn't cached yet, the script tells you to run `python3 auth.py` and stops
# (instead of letting the bot crash silently with no Telegram alert).
#
set -uo pipefail
cd "$(dirname "$0")"

PY=python3
SCRIPT=orb_monitor.py
LOG=orb.log

echo "== ORB monitor restart =="

# 1. Stop any running instance.
if pgrep -f "$SCRIPT" >/dev/null; then
    echo "Stopping existing $SCRIPT ..."
    pkill -f "$SCRIPT"
    sleep 2
    if pgrep -f "$SCRIPT" >/dev/null; then
        echo "Still alive, forcing kill ..."
        pkill -9 -f "$SCRIPT"
        sleep 1
    fi
else
    echo "No existing process."
fi

# 2. Pre-flight: is today's Kite token cached? (date check only, no network)
TOKEN_DATE=$($PY -c "import json; print(json.load(open('access_token.json')).get('date',''))" 2>/dev/null || true)
TODAY=$(date +%F)
if [ "$TOKEN_DATE" != "$TODAY" ]; then
    echo
    echo "No valid Kite token for today (token date: '${TOKEN_DATE:-none}', today: $TODAY)."
    echo "   Run:  $PY auth.py    then re-run ./start.sh"
    exit 1
fi

# 3. Start fresh (append to log with a dated banner).
echo "Starting $SCRIPT ..."
{
    echo ""
    echo "===== start $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
} >> "$LOG"
nohup $PY "$SCRIPT" >> "$LOG" 2>&1 &
sleep 3

# 4. Report.
if pgrep -f "$SCRIPT" >/dev/null; then
    echo "Running (PID $(pgrep -f "$SCRIPT" | head -1)). Watch alerts on Telegram."
    echo "--- last log lines ---"
    tail -n 6 "$LOG"
else
    echo "Not running after start. Last log lines:"
    echo "--- last log lines ---"
    tail -n 15 "$LOG"
    echo "(If it says 'market closed', that's normal after 15:30 IST.)"
fi
