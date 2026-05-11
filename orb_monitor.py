import logging
import sys
import threading
import time
from datetime import datetime, timedelta, time as dtime

import pytz

import config
import trades_db
from kite_auth import get_kite
from telegram_alert import (
    answer_callback,
    build_order_button,
    get_updates,
    send_alert,
)

IST = pytz.timezone("Asia/Kolkata")

logger = logging.getLogger("orb")


def _setup_logging():
    """Configure stderr logging with IST timestamps. Idempotent."""
    logging.Formatter.converter = lambda *_: datetime.now(IST).timetuple()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s IST %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


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
# FIX #7 — require the firing candle to OPEN inside the ORB ceiling (UP) or floor (DOWN).
# Without this, a continuation candle that was already outside ORB but only got volume
# confirmation later would still fire — entering well into a move that already happened.
def evaluate_signal(open_price, close, orb_high, orb_low, vol_ok, vwap, up_armed, down_armed):
    """Return 'UP', 'DOWN', or None for the current candle close."""
    if (up_armed
            and open_price <= orb_high
            and close > orb_high + config.BREAKOUT_BUFFER
            and vol_ok
            and (vwap is None or close > vwap)):
        return "UP"
    elif (down_armed
            and open_price >= orb_low
            and close < orb_low - config.BREAKOUT_BUFFER
            and vol_ok
            and (vwap is None or close < vwap)):
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


def extract_bid_ask(quote):
    """Pull (best_bid, best_ask) from a Kite quote dict. Either may be None."""
    if not quote:
        return None, None
    depth = quote.get("depth") or {}
    sells = depth.get("sell") or []
    buys = depth.get("buy") or []
    bid = buys[0]["price"] if buys and buys[0].get("price") else None
    ask = sells[0]["price"] if sells and sells[0].get("price") else None
    return bid, ask


# ── Exit decision (V3) ───────────────────────────────────────────────────────

def decide_exit(entry_price, current_price, now, square_off_time, sl_pct, target_pct):
    """Return 'SL', 'TARGET', 'TIMEOUT', or None.

    SL/TARGET use current_price as a fraction of entry_price. TIMEOUT fires
    when wall-clock now has reached the configured square-off time, regardless
    of P&L. SL takes precedence over TARGET when both somehow fire on the same
    tick (defensive — shouldn't happen with normal levels).
    """
    if now >= square_off_time:
        return "TIMEOUT"
    if current_price <= entry_price * (1 - sl_pct):
        return "SL"
    if current_price >= entry_price * (1 + target_pct):
        return "TARGET"
    return None


# ── Order placement (V2) ─────────────────────────────────────────────────────


def place_buy_limit(kite, tradingsymbol, qty, price):
    """Place a BUY LIMIT order on NFO. Returns (order_id, error).

    PAPER_TRADE wins over DRY_RUN — both skip the Kite call, but PAPER mode
    is wired up by the caller to also write to the trade journal and start
    SL/target monitoring.
    """
    if config.PAPER_TRADE:
        return f"paper-{int(time.time())}", None
    if config.DRY_RUN:
        logger.info("DRY_RUN would BUY %s qty=%d @ LIMIT %.2f", tradingsymbol, qty, price)
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


def place_sell_limit(kite, tradingsymbol, qty, price):
    """Place a SELL LIMIT order on NFO. Mirrors place_buy_limit for exits."""
    if config.PAPER_TRADE:
        return f"paper-sell-{int(time.time())}", None
    if config.DRY_RUN:
        logger.info("DRY_RUN would SELL %s qty=%d @ LIMIT %.2f", tradingsymbol, qty, price)
        return f"dry-sell-{int(time.time())}", None
    try:
        order_id = kite.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol=tradingsymbol,
            transaction_type="SELL",
            quantity=qty,
            product=config.ORDER_PRODUCT,
            order_type="LIMIT",
            price=price,
            validity="DAY",
        )
        return order_id, None
    except Exception as e:
        return None, str(e)


# ── Position tracking (V3) ───────────────────────────────────────────────────

class PositionTracker:
    """Thread-safe at-most-one open position. Set by callback thread on BUY,
    cleared by main thread on SELL/exit."""

    def __init__(self):
        self._lock = threading.Lock()
        self._position = None

    def is_open(self):
        with self._lock:
            return self._position is not None

    def current(self):
        with self._lock:
            return dict(self._position) if self._position else None

    def open(self, position):
        with self._lock:
            if self._position is not None:
                return False
            self._position = dict(position)
            return True

    def close(self):
        with self._lock:
            self._position = None


def monitor_position_tick(kite, position, tracker, conn, db_lock, square_off_time):
    """One tick of position monitoring. Closes the position in DB + tracker
    and sends a Telegram alert if SL/TARGET/TIMEOUT fires. Returns the exit
    reason or None.
    """
    symbol = position["symbol"]
    try:
        q = kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]
    except Exception as e:
        logger.error("Position quote fetch failed for %s: %s", symbol, e)
        return None

    ltp = q.get("last_price")
    if ltp is None or ltp <= 0:
        return None

    bid, _ = extract_bid_ask(q)

    reason = decide_exit(
        position["entry_price"], ltp, now_ist(), square_off_time,
        config.SL_PCT, config.TARGET_PCT,
    )
    pnl_pct = (ltp - position["entry_price"]) / position["entry_price"] * 100
    logger.info(
        "position_tick %s entry=%.2f ltp=%.2f bid=%s pnl=%+.2f%% decision=%s",
        symbol, position["entry_price"], ltp, bid, pnl_pct, reason or "HOLD",
    )
    if reason is None:
        return None

    # Realistic exit reference — best_bid if available, else LTP.
    raw_exit = bid if bid and bid > 0 else ltp
    # Live mode: SELL LIMIT slightly below best_bid to ensure fill on exit.
    sell_price = round_to_tick(max(0.05, raw_exit - config.LIMIT_BUFFER))

    _, err = place_sell_limit(kite, symbol, position["qty"], sell_price)
    if err:
        send_alert(f"SELL ORDER FAILED for {symbol}: {err}\nWill retry next tick.")
        return None

    # PAPER records the realistic best_bid sim; LIVE records the LIMIT we sent.
    recorded_exit = raw_exit if config.PAPER_TRADE else sell_price

    with db_lock:
        pnl = trades_db.record_sell(conn, position["id"], recorded_exit, reason)
    tracker.close()

    logger.info(
        "EXIT reason=%s symbol=%s entry=%.2f exit=%.2f qty=%d pnl=%+.2f mode=%s",
        reason, symbol, position["entry_price"], recorded_exit, position["qty"],
        pnl, "PAPER" if config.PAPER_TRADE else "LIVE",
    )

    mode_tag = "[PAPER] " if config.PAPER_TRADE else ""
    send_alert(
        f"{mode_tag}EXIT [{reason}] {symbol}\n"
        f"Entry: ₹{position['entry_price']:.2f} → Exit: ₹{recorded_exit:.2f}\n"
        f"P&L: ₹{pnl:+.2f} ({position['qty']} qty)"
    )
    return reason


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


def handle_callback(kite, callback_query, conn, db_lock, tracker):
    cb_id = callback_query["id"]
    data = callback_query.get("data", "")
    decoded = decode_order_callback(data)
    if decoded is None:
        answer_callback(cb_id, "Invalid order payload")
        return
    symbol, price, qty = decoded  # `price` is the LIMIT from alert time

    if tracker.is_open():
        answer_callback(cb_id, "Position already open. Skipping.")
        send_alert(f"Skipping {symbol}: another position is already open.")
        return

    answer_callback(cb_id, "Placing order...")

    # All modes journal at the alert-time LIMIT price. LIVE sends this price
    # to Kite; PAPER matches so the journal measures signal quality rather
    # than the user's tap-reaction delay (which won't exist once an automated
    # broker leg places the order on tap).
    fill_price = price

    _, err = place_buy_limit(kite, symbol, qty, price)
    if err:
        logger.error("BUY order failed: symbol=%s qty=%d price=%.2f err=%s", symbol, qty, price, err)
        send_alert(f"ORDER FAILED for {symbol}: {err}")
        return

    # DRY_RUN-only path (PAPER off, DRY on): preserve original alert-only behavior.
    if not config.PAPER_TRADE and config.DRY_RUN:
        send_alert(f"[DRY_RUN] Order placed: {symbol} BUY {qty} @ LIMIT ₹{price}")
        return

    # PAPER or LIVE: journal the buy and arm position monitoring.
    signal_type = "UP" if symbol.endswith("CE") else "DOWN"
    mode = "PAPER" if config.PAPER_TRADE else "LIVE"
    with db_lock:
        entry_id = trades_db.record_buy(conn, symbol, qty, fill_price, signal_type, mode)
    tracker.open({
        "id": entry_id, "symbol": symbol, "qty": qty,
        "entry_price": fill_price, "signal_type": signal_type, "mode": mode,
    })
    logger.info(
        "BUY filled mode=%s symbol=%s qty=%d entry=%.2f signal=%s sl=%.2f target=%.2f",
        mode, symbol, qty, fill_price, signal_type,
        fill_price * (1 - config.SL_PCT), fill_price * (1 + config.TARGET_PCT),
    )

    mode_tag = "[PAPER] " if config.PAPER_TRADE else ""
    send_alert(
        f"{mode_tag}BUY filled: {symbol}\n"
        f"Entry: ₹{fill_price:.2f} ({qty} qty)\n"
        f"SL: -{int(config.SL_PCT*100)}% @ ₹{fill_price * (1 - config.SL_PCT):.2f}\n"
        f"Target: +{int(config.TARGET_PCT*100)}% @ ₹{fill_price * (1 + config.TARGET_PCT):.2f}\n"
        f"Square-off: {config.SQUARE_OFF_HOUR:02d}:{config.SQUARE_OFF_MINUTE:02d} IST"
    )


def callback_listener(kite, stop_event, conn, db_lock, tracker):
    """Background thread: long-poll Telegram for button taps and place orders."""
    offset = None
    while not stop_event.is_set():
        updates = get_updates(offset=offset, timeout=25)
        for u in updates:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                try:
                    handle_callback(kite, cq, conn, db_lock, tracker)
                except Exception as e:
                    logger.exception("Callback handler error: %s", e)
        if not updates:
            time.sleep(1)


# ── Main loop ────────────────────────────────────────────────────────────────


def main():
    _setup_logging()
    kite = get_kite()
    fut, instruments_nfo = get_nifty_fut(kite)
    fut_token = fut["instrument_token"]
    fut_symbol = fut["tradingsymbol"]

    market_open = at(9, 15)
    orb_end = at(9, 30)
    market_close = at(15, 30)
    square_off_time = at(config.SQUARE_OFF_HOUR, config.SQUARE_OFF_MINUTE)

    if now_ist() > market_close:
        send_alert("Started after market close. Exiting.")
        return

    # DB + tracker are used in PAPER and LIVE modes. In DRY_RUN-only mode the
    # tracker stays empty and the monitoring branch never engages (callback
    # path returns early, see handle_callback).
    conn = trades_db.init_db()
    db_lock = threading.Lock()
    tracker = PositionTracker()

    # Mid-day restart recovery: if a row is open in the DB, resume monitoring.
    recovered = trades_db.get_open_position(conn)
    if recovered is not None:
        tracker.open(recovered)
        send_alert(
            f"Recovered open position: {recovered['symbol']} "
            f"(qty {recovered['qty']} @ ₹{recovered['entry_price']:.2f}, mode={recovered['mode']})\n"
            f"Resuming SL/target monitoring."
        )

    stop_event = threading.Event()
    listener = threading.Thread(
        target=callback_listener,
        args=(kite, stop_event, conn, db_lock, tracker),
        daemon=True,
    )
    listener.start()

    if config.PAPER_TRADE:
        mode = "PAPER"
    elif config.DRY_RUN:
        mode = "DRY_RUN"
    else:
        mode = "LIVE"
    logger.info(
        "STARTUP mode=%s symbol=%s lot=%d product=%s sl=%.2f target=%.2f square_off=%02d:%02d",
        mode, fut_symbol, config.LOT_SIZE, config.ORDER_PRODUCT,
        config.SL_PCT, config.TARGET_PCT,
        config.SQUARE_OFF_HOUR, config.SQUARE_OFF_MINUTE,
    )
    send_alert(
        f"ORB monitor started ({mode})\n"
        f"Symbol: {fut_symbol}\n"
        f"Tracking: 5-min closes after 9:30\n"
        f"Lot: {config.LOT_SIZE}, Product: {config.ORDER_PRODUCT}\n"
        f"SL {int(config.SL_PCT*100)}% / Target {int(config.TARGET_PCT*100)}% / "
        f"Square-off {config.SQUARE_OFF_HOUR:02d}:{config.SQUARE_OFF_MINUTE:02d}\n"
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

    logger.info(
        "ORB_FORMED symbol=%s high=%.2f low=%.2f range=%.2f buffer=%d rearm=%d",
        fut_symbol, orb_high, orb_low, orb_high - orb_low,
        config.BREAKOUT_BUFFER, config.REARM_BUFFER,
    )
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
    next_signal_check = next_5min_boundary()

    while now_ist() < market_close:
        # Adaptive cadence — wake at the sooner of (next 5-min signal check)
        # or (next position poll, only when a position is open).
        if tracker.is_open():
            next_wake = min(
                next_signal_check,
                now_ist() + timedelta(seconds=config.POSITION_POLL_SECONDS),
                market_close,
            )
        else:
            next_wake = min(next_signal_check, market_close)
        wait_until(next_wake)
        now = now_ist()

        # 1. Position monitoring — runs on every wake while holding.
        if tracker.is_open():
            position = tracker.current()
            try:
                monitor_position_tick(kite, position, tracker, conn, db_lock, square_off_time)
            except Exception as e:
                logger.exception("Position monitor error: %s", e)

        # 2. Signal evaluation — only on a 5-min boundary.
        if now < next_signal_check:
            continue
        next_signal_check += timedelta(minutes=5)

        # One-position-at-a-time: skip evaluation while holding. After square-off
        # there is no point in firing new signals — they would auto-exit immediately.
        if tracker.is_open() or now >= square_off_time:
            continue

        if state["UP"]["fires"] >= max_fires and state["DOWN"]["fires"] >= max_fires:
            continue

        try:
            candles_5m = kite.historical_data(fut_token, market_open, now, "5minute")
            candles_1m = kite.historical_data(fut_token, market_open, now, "minute")
        except Exception as e:
            logger.error("Historical data fetch failed: %s", e)
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

        # FIX #2 + FIX #6 + FIX #7 — buffer + elif + open-inside-ORB requirement.
        signal = evaluate_signal(
            last["open"], close, orb_high, orb_low, vol_ok, vwap, up_armed, down_armed,
        )

        logger.info(
            "tick candle=%s open=%.2f close=%.2f orb_hi=%.2f orb_lo=%.2f "
            "vol=%d avg=%.0f x%.2f vol_ok=%s vwap=%s up_armed=%s down_armed=%s "
            "fires=U%d/D%d signal=%s",
            last["date"].strftime("%H:%M"), last["open"], close, orb_high, orb_low,
            last["volume"], avg_vol, vol_ratio, vol_ok, vwap_str,
            up_armed, down_armed,
            state["UP"]["fires"], state["DOWN"]["fires"], signal or "NONE",
        )

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

        bid, ask = extract_bid_ask(quote)
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

        logger.info(
            "SIGNAL_FIRED dir=%s fire=%d/%d close=%.2f ref=%.2f spot=%.2f "
            "opt=%s bid=%s ask=%s limit=%s reject=%s",
            signal, fires_now, max_fires, close, ref_level, spot,
            opt_symbol, bid, ask, limit_price, reject_reason or "none",
        )

        if limit_price is not None:
            payload = encode_order_callback(opt_symbol, limit_price, config.LOT_SIZE)
            if config.PAPER_TRADE:
                mode_tag = " [PAPER]"
            elif config.DRY_RUN:
                mode_tag = " [DRY_RUN]"
            else:
                mode_tag = ""
            label = f"BUY {config.LOT_SIZE} {buy_side} @ ₹{limit_price}{mode_tag}"
            markup = build_order_button(label, payload)
            send_alert(body + f"\nLIMIT: ₹{limit_price}", reply_markup=markup)
        else:
            send_alert(body + f"\nAuto-order skipped: {reject_reason}\nPlace manually.")

    stop_event.set()
    logger.info(
        "SHUTDOWN fires_up=%d/%d fires_down=%d/%d",
        state["UP"]["fires"], max_fires, state["DOWN"]["fires"], max_fires,
    )
    send_alert(
        f"Market closed. ORB monitor stopping.\n"
        f"Fires today — UP: {state['UP']['fires']}/{max_fires}, "
        f"DOWN: {state['DOWN']['fires']}/{max_fires}"
    )
    conn.close()


if __name__ == "__main__":
    main()
