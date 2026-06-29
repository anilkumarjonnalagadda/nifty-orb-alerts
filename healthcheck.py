#!/usr/bin/env python3
"""Watchdog (V6.2): during market hours on a weekday, alert via Telegram if
orb_monitor.py is NOT running. Catches silent deaths (crash, OOM, forgot to
start, or a token failure that exited gracefully) so a dead bot is never
discovered only by manual checking — which is how the 2026-06-29 crash went
unnoticed until end of day.

Run from cron every ~10 min during the session (VM is on IST):
    */10 9-15 * * 1-5  cd ~/nifty-orb-alerts && /usr/bin/python3 healthcheck.py >> orb.log 2>&1

The script self-gates by IST clock too, so loose cron timing is harmless.
Caveat: this alerts over Telegram, which is itself flaky on this VM — treat a
*missing* heartbeat as informative, not the only safety net.
"""
import subprocess
from datetime import datetime, time as dtime

try:
    import pytz
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
except Exception:
    now = datetime.now()

# Only Mon-Fri, 09:20-15:30 IST (bot should be up by 09:20; square-off is 15:15).
if now.weekday() < 5 and dtime(9, 20) <= now.time() <= dtime(15, 30):
    running = subprocess.run(
        ["pgrep", "-f", "orb_monitor.py"], capture_output=True
    ).returncode == 0
    if not running:
        try:
            from telegram_alert import send_alert
            send_alert(
                "⚠️ WATCHDOG: orb_monitor is NOT running during market hours. "
                "If the token died, re-run `python3 auth.py` (after ~7:45 AM) and restart; "
                "otherwise check the VM."
            )
        except Exception:
            pass
        print(f"{now:%Y-%m-%d %H:%M:%S} WATCHDOG: orb_monitor not running")
