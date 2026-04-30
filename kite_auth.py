import json
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from kiteconnect import KiteConnect

import config

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "access_token.json")


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _load_cached_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        data = json.load(f)
    if data.get("date") != _today_str():
        return None
    return data.get("access_token")


def _save_token(access_token):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": _today_str()}, f)


def get_kite():
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    cached = _load_cached_token()
    if cached:
        kite.set_access_token(cached)
        try:
            kite.profile()
            return kite
        except Exception:
            pass
    raise RuntimeError(
        "No valid access token for today. Run `python auth.py` first."
    )


def interactive_login():
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    print("\n1. Open this URL in your browser and log in:\n")
    print("   " + kite.login_url())
    print("\n2. After login you'll be redirected to a URL containing `request_token=...`")
    print("   Paste either the full redirect URL or just the request_token below.\n")
    raw = input("> ").strip()
    if raw.startswith("http"):
        qs = parse_qs(urlparse(raw).query)
        request_token = qs.get("request_token", [None])[0]
        if not request_token:
            raise SystemExit("Could not find request_token in the URL.")
    else:
        request_token = raw
    data = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    _save_token(data["access_token"])
    print("\nAccess token saved for today.")
