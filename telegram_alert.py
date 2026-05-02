import json

import requests

import config


def send_alert(message, reply_markup=None):
    """Send a Telegram message. If reply_markup is given, attach inline buttons."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"Telegram send failed: {e}")


def build_order_button(label, callback_payload):
    """Build a one-button inline keyboard. callback_payload must be ≤ 64 bytes (Telegram limit)."""
    return {
        "inline_keyboard": [[
            {"text": label, "callback_data": callback_payload}
        ]]
    }


def answer_callback(callback_query_id, text=None):
    """Acknowledge a button tap so Telegram stops the loading spinner on the user's phone."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram callback ack failed: {e}")


def get_updates(offset=None, timeout=25):
    """Long-poll Telegram for new updates. Returns list of update dicts."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": timeout, "allowed_updates": json.dumps(["callback_query"])}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"Telegram getUpdates failed: {e}")
        return []
