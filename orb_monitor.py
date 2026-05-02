import threading
import time
from datetime import datetime, timedelta, time as dtime

import pytz

import config
from kite_auth import get_kite
from telegram_alert import (
    answer_callback,
    build_order_button,
    get_updates,
    send_alert,
)

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


# FIX #1 — replaces the broken "minute % 3 == 0" check (always True for Nifty,
# so the candles fallback was dead code and partial candles were used).
def select_last_closed_candle(candles, now, interval_minutes=5):
    """Return the most recent candle whose full interval window has elapsed."""
    return next(
        (c for c in reversed(candles) if c["date"] + timedelta(minutes=interval_minutes) <= now),
        None,
    )


# FIX #3 — extracted so the re-arm conditions are unit-testable in isolation.
def update_rearm_state(state, close, orb_high, orb_low):
    """Reset arm flags once price pulls back meaningfully inside the ORB."""
    if state["UP"]["needs_reset"] and close < orb_high - config.REARM_BUFFER:
        state["UP"]["needs_reset"] = False
    if state["DOWN"]["needs_reset"] and close > orb_low + config.REARM_BUFFER:
        state["DOWN"]["needs_reset"] = False


# FIX #2 + FIX #6 — buffer guards against fakeouts; elif enforces mutual exclusion.
def evaluate_signal(close, orb_high, orb_low, vol_ok, vwap, up_armed, down_armed):
    """Return 'UP', 'DOWN', or None for the current candle close."""
    if up_armed and close > orb_high + config.BREAKOUT_BUFFER and vol_ok and (vwap is None or close > vwap):
        return "UP"
    elif down_armed and close < orb_low - config.BREAKOUT_BUFFER and vol_ok and (vwap is None or close < vwap):
        return "DOWN"
    return None


def compute_vol_filter(last_candle, prior_candles):
    """Return (vol_ok, avg_vol) based on lookback window."""
    recent = prior_candles[-config.LOOKBACK_CANDLES:]
    avg_vol = sum(c["volume"] for c in recent) / len(recent) if recent else 0
    vol_ok = last_candle["volume"] > config.VOLUME_MULTIPLIER * avg_vol if avg_vol > 0 else True
    return vol_ok, avg_vol


def compute_vwap(candles_1m):
    cum_pv = 0.0
    cum_v = 0
    for c in candles_1m:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += tp * c["volume"]
        cum_v += c["volume"]
    return cum_pv / cum_v if cum_v > 0 else None


def next_5min_boundary():
    n = now_ist()
    minutes_into_hour = n.minute
    next_min = ((minutes_into_hour // 5) + 1) * 5
    if next_min >= 60:
        base = n.replace(minute=0, second=20, microsecond=0) + timedelta(hours=1)
    else:
        base = n.replace(minute=next_min, second=20, microsecond=0)
    return base


# ── Option selection (V2) ────────────────────────────────────────────────────


def itm_option(spot, direction, instruments_nfo):
    """Return the ITM-1 option (one strike in the money) for the given direction.

    UP  → BUY CE; ITM means strike *below* spot.
    DOWN → BUY PE; ITM means strike *above* spot.
    Returns the full instrument dict (has tradingsymbol, instrument_token, etc.).
    """
    today = now_ist().date()
    atm_strike = round(spot / 50) * 50
    if direction == "UP":
        target_strike = atm_strike - 50
        opt_type = "CE"
    else:
        target_strike = atm_strike + 50
        opt_type = "PE"

    opts = [
        i for i in instruments_nfo
        if i["name"] == "NIFTY"
        and i["instrument_type"] == opt_type
        and i["strike"] == target_strike
        and i["expiry"] >= today
    ]
    opts.sort(key=lambda x: x["expiry"])
    return opts[0] if opts else None


# ── Limit price calc (V2) ────────────────────────────────────────────────────


def round_to_tick(price, tick=0.05):
    """Round a price to the NSE option tick size (0.05). Avoids float drift."""
    return round(round(price / tick) * tick, 2)


def compute_limit_price(quote, buffer, max_spread_pct, max_premium):
    """Decide the LIMIT price for a BUY order. Return (price, reason_or_none).

    reason is None on success, or a human-readable string explaining why we
    refuse to auto-place (caller should alert + skip the order button).
    """
    depth = quote.get("depth") or {}
    sells = depth.get("sell") or []
    buys = depth.get("buy") or []
    best_ask = sells[0]["price"] if sells and sells[0].get("price") else None
    best_bid = buys[0]["price"] if buys and buys[0].get("price") else None

    if not best_ask or best_ask <= 0:
        ltp = quote.get("last_price")
        if not ltp:
            return None, "no quote available"
        # Fallback: 2× buffer above LTP, flagged in caller.
        return round_to_tick(ltp + 2 * buffer), "no ask depth, using LTP + 2× buffer"

    if best_ask > max_premium:
        return None, f"premium ₹{best_ask:.2f} > MAX_PREMIUM ₹{max_premium}"

    if best_bid and best_ask > 0:
        mid = (best_ask + best_bid) / 2
        spread_pct = (best_ask - best_bid) / mid if mid > 0 else 1
        if spread_pct > max_spread_pct:
            return None, f"spread {spread_pct*100:.1f}% > {max_spread_pct*100:.0f}%"

    return round_to_tick(best_ask + buffer), None


# ── Order placement (V2) ─────────────────────────────────────────────────────


def place_buy_limit(kite, tradingsymbol, qty, price):
    """Place a BUY LIMIT order on NFO. Honors DRY_RUN. Returns (order_id, error)."""
    if config.DRY_RUN:
        msg = f"DRY_RUN: would place BUY {qty} {tradingsymbol} @ LIMIT ₹{price}"
        print(msg)
        return f"dry-{int(time.time())}", None
    try:
        order_id = kite.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol=tradingsymbol,
            transaction_type="BUY",
            quantity=qty,
            product=config.ORDER_PRODUCT,
            order_type="LIMIT",
            price=price,
            validity="DAY",
        )
        return order_id, None
    except Exception as e:
        return None, str(e)


# ── Telegram callback handling (V2) ──────────────────────────────────────────

# Callback payload format: "o|<tradingsymbol>|<price>|<qty>"
# Telegram caps callback_data at 64 bytes; the longest realistic NIFTY option
# symbol is ~17 chars, so total payload stays well under the cap.

def encode_order_callback(tradingsymbol, price, qty):
    return f"o|{tradingsymbol}|{price}|{qty}"


def decode_order_callback(payload):
    """Return (tradingsymbol, price, qty) or None if payload is malformed."""
    parts = payload.split("|")
    if len(parts) != 4 or parts[0] != "o":
        return None
    try:
        return parts[1], float(parts[2]), int(parts[3])
    except ValueError:
        return None


def handle_callback(kite, callback_query):
    cb_id = callback_query["id"]
    data = callback_query.get("data", "")
    decoded = decode_order_callback(data)
    if decoded is None:
        answer_callback(cb_id, "Invalid order payload")
        return
    symbol, price, qty = decoded
    answer_callback(cb_id, "Placing order...")
    order_id, err = place_buy_limit(kite, symbol, qty, price)
    if err:
        send_alert(f"ORDER FAILED for {symbol}: {err}")
    else:
        prefix = "[DRY_RUN] " if config.DRY_RUN else ""
        send_alert(f"{prefix}Order placed: {symbol} BUY {qty} @ LIMIT ₹{price}\nOrder ID: {order_id}")


def callback_listener(kite, stop_event):
    """Background thread: long-poll Telegram for button taps and place orders."""
    offset = None
    while not stop_event.is_set():
        updates = get_updates(offset=offset, timeout=25)
        for u in updates:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                try:
                    handle_callback(kite, cq)
                except Exception as e:
                    print(f"Callback handler error: {e}")
        if not updates:
            time.sleep(1)


# ── Main loop ────────────────────────────────────────────────────────────────


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

    stop_event = threading.Event()
    listener = threading.Thread(target=callback_listener, args=(kite, stop_event), daemon=True)
    listener.start()

    mode = "DRY_RUN" if config.DRY_RUN else "LIVE"
    send_alert(
        f"ORB monitor started ({mode})\n"
        f"Symbol: {fut_symbol}\n"
        f"Tracking: 5-min closes after 9:30\n"
        f"Lot: {config.LOT_SIZE}, Product: {config.ORDER_PRODUCT}\n"
        f"Waiting for ORB candle to close at 9:30 IST..."
    )

    wait_until(orb_end + timedelta(seconds=30))

    orb_candles = kite.historical_data(fut_token, market_open, orb_end, "15minute")
    if not orb_candles:
        send_alert("ERROR: ORB candle unavailable. Exiting.")
        return

    # FIX #4 — validate that the candle Kite returned actually starts at 9:15 AM.
    orb = orb_candles[0]
    if orb["date"].time() != dtime(9, 15):
        send_alert(
            f"ERROR: ORB candle starts at {orb['date'].time()}, expected 09:15. "
            f"Data may be from pre-open session. Exiting."
        )
        return

    orb_high = orb["high"]
    orb_low = orb["low"]

    send_alert(
        f"ORB formed for {fut_symbol}\n"
        f"High: {orb_high}\n"
        f"Low: {orb_low}\n"
        f"Buffer: {config.BREAKOUT_BUFFER} pts | Re-arm: {config.REARM_BUFFER} pts\n"
        f"Watching 5-min closes for breakout/breakdown..."
    )

    max_fires = config.MAX_FIRES_PER_DIRECTION
    state = {
        "UP": {"fires": 0, "needs_reset": False},
        "DOWN": {"fires": 0, "needs_reset": False},
    }
    next_check = next_5min_boundary()

    while now_ist() < market_close:
        wait_until(next_check)
        next_check += timedelta(minutes=5)

        if state["UP"]["fires"] >= max_fires and state["DOWN"]["fires"] >= max_fires:
            time.sleep(5)
            continue

        try:
            now = now_ist()
            candles_5m = kite.historical_data(fut_token, market_open, now, "5minute")
            candles_1m = kite.historical_data(fut_token, market_open, now, "minute")
        except Exception as e:
            print(f"Data fetch failed: {e}")
            continue

        if not candles_5m or len(candles_5m) < 2:
            continue

        # FIX #1 — use the time-based helper instead of the broken % 3 check.
        last = select_last_closed_candle(candles_5m, now, interval_minutes=5)
        if last is None:
            continue

        prior = [c for c in candles_5m if c["date"] < last["date"]]
        vol_ok, avg_vol = compute_vol_filter(last, prior)

        vwap = compute_vwap(candles_1m) if candles_1m else None
        close = last["close"]

        vwap_str = f"{vwap:.2f}" if vwap else "n/a"
        vol_ratio = (last["volume"] / avg_vol) if avg_vol > 0 else 0

        # FIX #3 — re-arm requires a REARM_BUFFER pullback, not just touching the level.
        update_rearm_state(state, close, orb_high, orb_low)

        up_armed = (not state["UP"]["needs_reset"]) and state["UP"]["fires"] < max_fires
        down_armed = (not state["DOWN"]["needs_reset"]) and state["DOWN"]["fires"] < max_fires

        # FIX #2 + FIX #6 — BREAKOUT_BUFFER threshold; elif prevents simultaneous fires.
        signal = evaluate_signal(close, orb_high, orb_low, vol_ok, vwap, up_armed, down_armed)

        if signal is None:
            continue

        try:
            spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
        except Exception:
            spot = close

        opt = itm_option(spot, signal, instruments_nfo)
        if opt is None:
            send_alert(
                f"{signal} signal but no ITM option found near spot {spot}. "
                f"Place trade manually."
            )
            continue
        opt_symbol = opt["tradingsymbol"]

        try:
            quote_resp = kite.quote([f"NFO:{opt_symbol}"])
            quote = quote_resp[f"NFO:{opt_symbol}"]
        except Exception as e:
            quote = None
            quote_err = str(e)

        if quote:
            limit_price, reject_reason = compute_limit_price(
                quote, config.LIMIT_BUFFER, config.MAX_SPREAD_PCT, config.MAX_PREMIUM,
            )
        else:
            limit_price, reject_reason = None, f"quote fetch failed: {quote_err}"

        bid = (quote.get("depth", {}).get("buy") or [{}])[0].get("price") if quote else None
        ask = (quote.get("depth", {}).get("sell") or [{}])[0].get("price") if quote else None
        bid_ask = f"{bid}/{ask}" if bid and ask else "n/a"

        if signal == "UP":
            state["UP"]["fires"] += 1
            state["UP"]["needs_reset"] = True
            label_dir = "BREAKOUT (UP)"
            buy_side = "CE"
        else:
            state["DOWN"]["fires"] += 1
            state["DOWN"]["needs_reset"] = True
            label_dir = "BREAKDOWN (DOWN)"
            buy_side = "PE"

        fires_now = state[signal]["fires"]
        comparator = ">" if signal == "UP" else "<"
        ref_level = orb_high if signal == "UP" else orb_low
        sign = "+" if signal == "UP" else "-"

        body = (
            f"{label_dir} — fire {fires_now}/{max_fires}\n"
            f"{fut_symbol} 5m close: {close} {comparator} ORB {'High' if signal=='UP' else 'Low'} "
            f"{ref_level} ({sign}{config.BREAKOUT_BUFFER} buf)\n"
            f"Vol: {last['volume']} (avg {avg_vol:.0f}, x{vol_ratio:.2f})\n"
            f"VWAP: {vwap_str}\n"
            f"Spot Nifty: {spot}\n"
            f"ITM-1 {buy_side}: {opt_symbol}\n"
            f"Bid/Ask: {bid_ask}"
        )

        if limit_price is not None:
            payload = encode_order_callback(opt_symbol, limit_price, config.LOT_SIZE)
            mode_tag = " [DRY_RUN]" if config.DRY_RUN else ""
            label = f"BUY {config.LOT_SIZE} {buy_side} @ ₹{limit_price}{mode_tag}"
            markup = build_order_button(label, payload)
            send_alert(body + f"\nLIMIT: ₹{limit_price}", reply_markup=markup)
        else:
            send_alert(body + f"\nAuto-order skipped: {reject_reason}\nPlace manually.")

    stop_event.set()
    send_alert(
        f"Market closed. ORB monitor stopping.\n"
        f"Fires today — UP: {state['UP']['fires']}/{max_fires}, "
        f"DOWN: {state['DOWN']['fires']}/{max_fires}"
    )


if __name__ == "__main__":
    main()
