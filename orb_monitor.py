import time
from datetime import datetime, timedelta, time as dtime

import pytz

import config
from kite_auth import get_kite
from telegram_alert import send_alert

IST = pytz.timezone("Asia/Kolkata")


def now_ist():
    return datetime.now(IST)


def at(h, m):
    today = now_ist().date()
    return IST.localize(datetime.combine(today, dtime(h, m)))


def wait_until(target):
    while now_ist() < target:
        remaining = (target - now_ist()).total_seconds()
        time.sleep(min(remaining, 30))


def get_nifty_fut(kite):
    today = now_ist().date()
    nfo = kite.instruments("NFO")
    futs = [
        i for i in nfo
        if i["name"] == "NIFTY"
        and i["instrument_type"] == "FUT"
        and i["expiry"] >= today
    ]
    futs.sort(key=lambda x: x["expiry"])
    if not futs:
        raise RuntimeError("No active Nifty futures found.")
    return futs[0], nfo


def atm_option_symbol(spot, direction, instruments_nfo):
    today = now_ist().date()
    strike = round(spot / 50) * 50
    opt_type = "CE" if direction == "UP" else "PE"
    opts = [
        i for i in instruments_nfo
        if i["name"] == "NIFTY"
        and i["instrument_type"] == opt_type
        and i["strike"] == strike
        and i["expiry"] >= today
    ]
    opts.sort(key=lambda x: x["expiry"])
    return opts[0]["tradingsymbol"] if opts else f"NIFTY {strike} {opt_type} (lookup failed)"


def compute_vwap(candles_1m):
    cum_pv = 0.0
    cum_v = 0
    for c in candles_1m:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += tp * c["volume"]
        cum_v += c["volume"]
    return cum_pv / cum_v if cum_v > 0 else None


def next_3min_boundary():
    n = now_ist()
    minutes_into_hour = n.minute
    next_min = ((minutes_into_hour // 3) + 1) * 3
    if next_min >= 60:
        base = n.replace(minute=0, second=20, microsecond=0) + timedelta(hours=1)
    else:
        base = n.replace(minute=next_min, second=20, microsecond=0)
    return base


def main():
    kite = get_kite()
    fut, instruments_nfo = get_nifty_fut(kite)
    fut_token = fut["instrument_token"]
    fut_symbol = fut["tradingsymbol"]

    market_open = at(9, 15)
    orb_end = at(9, 30)
    market_close = at(15, 30)

    if now_ist() > market_close:
        send_alert("Started after market close. Exiting.")
        return

    send_alert(
        f"ORB monitor started\n"
        f"Symbol: {fut_symbol}\n"
        f"Waiting for ORB candle to close at 9:30 IST..."
    )

    wait_until(orb_end + timedelta(seconds=30))

    orb_candles = kite.historical_data(fut_token, market_open, orb_end, "15minute")
    if not orb_candles:
        send_alert("ERROR: ORB candle unavailable. Exiting.")
        return

    orb = orb_candles[0]
    orb_high = orb["high"]
    orb_low = orb["low"]

    send_alert(
        f"ORB formed for {fut_symbol}\n"
        f"High: {orb_high}\n"
        f"Low: {orb_low}\n"
        f"Watching 3-min closes for breakout/breakdown..."
    )

    fired = {"UP": False, "DOWN": False}
    next_check = next_3min_boundary()

    while now_ist() < market_close:
        wait_until(next_check)
        next_check += timedelta(minutes=3)

        if fired["UP"] and fired["DOWN"]:
            time.sleep(5)
            continue

        try:
            now = now_ist()
            candles_3m = kite.historical_data(fut_token, market_open, now, "3minute")
            candles_1m = kite.historical_data(fut_token, market_open, now, "minute")
        except Exception as e:
            print(f"Data fetch failed: {e}")
            continue

        if not candles_3m or len(candles_3m) < 2:
            continue

        last = candles_3m[-1] if candles_3m[-1]["date"].minute % 3 == 0 else candles_3m[-2]
        prior = [c for c in candles_3m if c["date"] < last["date"]]

        recent = prior[-config.LOOKBACK_CANDLES:]
        avg_vol = sum(c["volume"] for c in recent) / len(recent) if recent else 0
        vol_ok = last["volume"] > config.VOLUME_MULTIPLIER * avg_vol if avg_vol > 0 else True

        vwap = compute_vwap(candles_1m) if candles_1m else None
        close = last["close"]

        vwap_str = f"{vwap:.2f}" if vwap else "n/a"
        vol_ratio = (last["volume"] / avg_vol) if avg_vol > 0 else 0

        if not fired["UP"] and close > orb_high and vol_ok and (vwap is None or close > vwap):
            try:
                spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
            except Exception:
                spot = close
            opt = atm_option_symbol(spot, "UP", instruments_nfo)
            send_alert(
                f"BREAKOUT (UP)\n"
                f"{fut_symbol} 3m close: {close} > ORB High {orb_high}\n"
                f"Vol: {last['volume']} (avg {avg_vol:.0f}, x{vol_ratio:.2f})\n"
                f"VWAP: {vwap_str}\n"
                f"Spot Nifty: {spot}\n"
                f"BUY CE: {opt}"
            )
            fired["UP"] = True

        if not fired["DOWN"] and close < orb_low and vol_ok and (vwap is None or close < vwap):
            try:
                spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
            except Exception:
                spot = close
            opt = atm_option_symbol(spot, "DOWN", instruments_nfo)
            send_alert(
                f"BREAKDOWN (DOWN)\n"
                f"{fut_symbol} 3m close: {close} < ORB Low {orb_low}\n"
                f"Vol: {last['volume']} (avg {avg_vol:.0f}, x{vol_ratio:.2f})\n"
                f"VWAP: {vwap_str}\n"
                f"Spot Nifty: {spot}\n"
                f"BUY PE: {opt}"
            )
            fired["DOWN"] = True

    send_alert("Market closed. ORB monitor stopping.")


if __name__ == "__main__":
    main()
