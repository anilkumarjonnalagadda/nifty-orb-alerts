from datetime import datetime

import pytz
from kiteconnect import KiteConnect

import config
from telegram_alert import send_alert

IST = pytz.timezone("Asia/Kolkata")


def main():
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    login_url = kite.login_url()
    today = datetime.now(IST).strftime("%a, %d %b %Y")

    msg = (
        f"Good morning. {today}\n\n"
        f"Refresh Kite access token before 9:15 IST.\n\n"
        f"1. Tap to log in:\n{login_url}\n\n"
        f"2. SSH to Lightsail and run:\n"
        f"   cd ~/nifty-orb-alerts && source venv/bin/activate\n"
        f"   python auth.py\n"
        f"   nohup python orb_monitor.py > orb.log 2>&1 &\n\n"
        f"You should get a 'ORB monitor started' alert once it's live."
    )
    send_alert(msg)


if __name__ == "__main__":
    main()
