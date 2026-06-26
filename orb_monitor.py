import logging
import sys
import threading
import time
from collections import defaultdict
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


def itm_option(spot, direction, instruments_nfo, target_expiry=None):
    """Return the ITM-1 option (one strike in the money) for the given direction.

    UP  → BUY CE; ITM means strike *below* spot.
    DOWN → BUY PE; ITM means strike *above* spot.
    Returns the full instrument dict (has tradingsymbol, instrument_token, etc.).

    target_expiry (date) selects WHICH expiry:
      None  → nearest expiry on/after today (the weekly). Default, unchanged.
      date  → that exact expiry if it exists (the monthly), else the nearest
              expiry on/after it, else the nearest overall.
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
    if not opts:
        return None
    if target_expiry is not None:
        exact = [o for o in opts if o["expiry"] == target_expiry]
        if exact:
            return exact[0]
        after = [o for o in opts if o["expiry"] >= target_expiry]
        if after:
            return after[0]
    return opts[0]


def days_to_expiry(expiry, today):
    """Whole days from `today` (a date) to `expiry` (a date or datetime)."""
    exp = expiry.date() if isinstance(expiry, datetime) else expiry
    return (exp - today).days


def select_trade_plan(monthly_dte, max_dte, weekly_track_only):
    """Route a signal to weekly (early cycle) or monthly (late cycle) expiry.

    monthly_dte       — days to the current monthly expiry.
    max_dte           — at/below this, trade the MONTHLY (config.DTE_MONTHLY_MAX).
    weekly_track_only — when in the weekly regime, journal as PAPER (track) with
                        no live button instead of being actionable.

    Returns a dict: {regime, use_monthly, track_only, actionable}.
    Pure — no I/O, no config reads — so it is unit-testable in isolation.
    """
    if monthly_dte <= max_dte:
        return {"regime": "monthly", "use_monthly": True,
                "track_only": False, "actionable": True}
    return {"regime": "weekly", "use_monthly": False,
            "track_only": bool(weekly_track_only),
            "actionable": not bool(weekly_track_only)}


# ── Signal-quality gates (V7): VAH + put-OI support (match the backtest) ──────

def support_pe_strikes(spot, step=50):
    """The two OTM put strikes just below spot used for the put-OI support read:
    (ATM-1step, ATM-2step), where ATM is the nearest `step` to spot. These are
    the spot-1 / spot-2 puts whose rising OI = put writers building support."""
    atm = round(spot / step) * step
    return atm - step, atm - 2 * step


def pe_symbol_at_strike(strike, instruments_nfo):
    """Nearest-weekly PE tradingsymbol at `strike`, or None if absent."""
    today = now_ist().date()
    opts = [
        i for i in instruments_nfo
        if i["name"] == "NIFTY" and i["instrument_type"] == "PE"
        and i["strike"] == strike and i["expiry"] >= today
    ]
    opts.sort(key=lambda x: x["expiry"])
    return opts[0]["tradingsymbol"] if opts else None


def put_oi_rising(current_oi, baseline_oi):
    """True when combined support-put OI has RISEN vs the 09:30 baseline (put
    writers building support beneath price → bullish CE confirm). False if either
    reading is missing or the baseline is non-positive. Must strictly rise."""
    return (current_oi is not None and baseline_oi is not None
            and baseline_oi > 0 and current_oi > baseline_oi)


def resistance_ce_strikes(spot, step=50):
    """The two OTM call strikes just above spot used for the call-OI resistance
    read: (ATM+1step, ATM+2step), where ATM is the nearest `step` to spot. These
    are the spot+1 / spot+2 calls whose rising OI = call writers building
    resistance overhead. Mirror of support_pe_strikes for the DOWN/PE side."""
    atm = round(spot / step) * step
    return atm + step, atm + 2 * step


def ce_symbol_at_strike(strike, instruments_nfo):
    """Nearest-weekly CE tradingsymbol at `strike`, or None if absent.
    Mirror of pe_symbol_at_strike."""
    today = now_ist().date()
    opts = [
        i for i in instruments_nfo
        if i["name"] == "NIFTY" and i["instrument_type"] == "CE"
        and i["strike"] == strike and i["expiry"] >= today
    ]
    opts.sort(key=lambda x: x["expiry"])
    return opts[0]["tradingsymbol"] if opts else None


def call_oi_rising(current_oi, baseline_oi):
    """True when combined resistance-call OI has RISEN vs the 09:30 baseline (call
    writers building resistance overhead → bearish DOWN confirm). Mirror of
    put_oi_rising; same strictly-rise / missing-or-zero-base semantics."""
    return (current_oi is not None and baseline_oi is not None
            and baseline_oi > 0 and current_oi > baseline_oi)


def vah_gate_ok(direction, close, vah, val):
    """Value-area gate: UP must close ABOVE VAH, DOWN must close BELOW VAL. False
    when the needed level is missing (= no trade), matching the backtest, which
    skips when the value area is unavailable."""
    if direction == "UP":
        return vah is not None and close > vah
    return val is not None and close < val


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


# ── Risk-based position sizing (V4) ──────────────────────────────────────────

def position_size(entry_price, sl_pct, lot_size, risk_per_trade_inr, max_lots):
    """Return the order qty (a whole multiple of lot_size) whose worst-case
    loss — entry_price * sl_pct * qty when the stop hits — stays within
    risk_per_trade_inr, capped at max_lots lots.

    Returns 0 when even a single lot would risk more than the cap; the caller
    must then skip the auto-order, since one lot is the smallest tradable size.
    """
    risk_per_lot = entry_price * sl_pct * lot_size
    if risk_per_lot <= 0:
        return 0
    lots = int(risk_per_trade_inr // risk_per_lot)
    lots = min(lots, max_lots)
    return lots * lot_size


# ── Volume profile / high-conviction tag (V5) ────────────────────────────────

def volume_profile(candles, bin_width, value_area_pct):
    """Developing volume-at-price profile from OHLCV candles.

    Each candle's volume is spread uniformly across its high-low range into
    `bin_width`-point bins (a standard proxy — OHLCV doesn't reveal the
    intra-candle distribution). Returns (poc, vah, val): the most-traded price
    and the high/low edges of the `value_area_pct` (e.g. 0.70) volume band
    around it. Returns None if there is no volume.
    """
    bins = defaultdict(float)
    for c in candles:
        hi, lo, vol = c["high"], c["low"], c["volume"]
        if vol <= 0:
            continue
        lo_b = int(lo // bin_width)
        hi_b = int(hi // bin_width)
        share = vol / (hi_b - lo_b + 1)
        for b in range(lo_b, hi_b + 1):
            bins[b] += share
    if not bins:
        return None
    total = sum(bins.values())
    poc_b = max(bins, key=bins.get)
    included = {poc_b}
    acc = bins[poc_b]
    lo_b = hi_b = poc_b
    sorted_b = sorted(bins)
    min_b, max_b = sorted_b[0], sorted_b[-1]
    while acc < value_area_pct * total and (lo_b > min_b or hi_b < max_b):
        up = bins.get(hi_b + 1, -1.0) if hi_b < max_b else -1.0
        dn = bins.get(lo_b - 1, -1.0) if lo_b > min_b else -1.0
        if up >= dn:
            hi_b += 1
            acc += bins.get(hi_b, 0.0)
            included.add(hi_b)
        else:
            lo_b -= 1
            acc += bins.get(lo_b, 0.0)
            included.add(lo_b)
    poc = (poc_b + 0.5) * bin_width
    vah = (max(included) + 1) * bin_width
    val = min(included) * bin_width
    return poc, vah, val


# ── Fill confirmation (V5 — for LIVE orders) ─────────────────────────────────

def classify_fill(filled_qty, want_qty):
    """Classify an order's fill: 'NONE' (nothing filled), 'PARTIAL' (some but
    not all), or 'COMPLETE' (fully filled)."""
    if filled_qty <= 0:
        return "NONE"
    if filled_qty < want_qty:
        return "PARTIAL"
    return "COMPLETE"


def confirm_order_fill(kite, order_id, want_qty, timeout_s, poll_s):
    """Poll Kite order history until the order is COMPLETE/terminal or timeout.

    Returns (filled_qty, avg_price, status) where status comes from
    classify_fill. I/O wrapper — not unit-tested (the offline suite covers
    classify_fill instead).
    """
    deadline = time.time() + timeout_s
    filled, avg = 0, 0.0
    while time.time() < deadline:
        try:
            hist = kite.order_history(order_id)
        except Exception as e:
            logger.error("order_history failed for %s: %s", order_id, e)
            time.sleep(poll_s)
            continue
        if hist:
            last = hist[-1]
            status = (last.get("status") or "").upper()
            filled = last.get("filled_quantity") or 0
            avg = last.get("average_price") or 0.0
            if status == "COMPLETE":
                return filled, avg, "COMPLETE"
            if status in ("REJECTED", "CANCELLED"):
                return filled, avg, classify_fill(filled, want_qty)
        time.sleep(poll_s)
    return filled, avg, classify_fill(filled, want_qty)


# ── Exit decision (V3) ───────────────────────────────────────────────────────

def decide_exit(entry_price, current_price, now, square_off_time, sl_pct, target_pct,
                peak_price=None, use_trailing=False, trail_step_pct=0.15):
    """Return an exit reason ('SL' / 'TARGET' / 'TRAIL' / 'TIMEOUT') or None.

    TIMEOUT always fires once wall-clock `now` reaches the square-off time.

    Fixed mode (use_trailing=False): SL at entry*(1-sl_pct), TARGET at
    entry*(1+target_pct). SL wins ties (defensive).

    Trailing mode (use_trailing=True, the V6 'let winners run' upgrade): no
    fixed target. STEPPED ("ladder") trail matching the validated Volrix
    trailSL 15%/15% leg: the stop starts at entry*(1-sl_pct) and ratchets UP by
    trail_step_pct*entry for every full trail_step_pct*entry the option's peak
    (`peak_price`) has risen above entry. Between steps the stop holds — it does
    NOT tighten on every uptick. (A continuous 'peak - sl_pct*entry' trail tightens
    each tick and backtested ~1/3 worse: 11.7%->7.5% net OOS.) The stop never moves
    down. Returns 'TRAIL' when hit (a win or a loss; the P&L tells which).
    """
    if now >= square_off_time:
        return "TIMEOUT"
    if use_trailing:
        if peak_price is None:
            peak_price = entry_price
        step_amt = trail_step_pct * entry_price
        if step_amt > 0 and peak_price > entry_price:
            steps = int((peak_price - entry_price) // step_amt)
        else:
            steps = 0
        stop = entry_price * (1 - sl_pct) + steps * step_amt
        if current_price <= stop:
            return "TRAIL"
        return None
    if current_price <= entry_price * (1 - sl_pct):
        return "SL"
    if current_price >= entry_price * (1 + target_pct):
        return "TARGET"
    return None


# ── Order placement (V2) ─────────────────────────────────────────────────────


def place_buy_limit(kite, tradingsymbol, qty, price, paper=None):
    """Place a BUY LIMIT order on NFO. Returns (order_id, error).

    PAPER_TRADE wins over DRY_RUN — both skip the Kite call, but PAPER mode
    is wired up by the caller to also write to the trade journal and start
    SL/target monitoring.

    `paper` overrides config.PAPER_TRADE for this one order (None = use config).
    Used to paper-track weekly (early-cycle) signals even while the bot is LIVE.
    """
    is_paper = config.PAPER_TRADE if paper is None else paper
    if is_paper:
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


def place_sell_limit(kite, tradingsymbol, qty, price, paper=None):
    """Place a SELL LIMIT order on NFO. Mirrors place_buy_limit for exits.

    `paper` overrides config.PAPER_TRADE (None = use config) so a paper-tracked
    position exits as paper even when the bot is otherwise LIVE.
    """
    is_paper = config.PAPER_TRADE if paper is None else paper
    if is_paper:
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


# ── Put-OI support read (V7) ─────────────────────────────────────────────────

def fetch_combined_oi(kite, symbols):
    """Sum the open interest of the given NFO option symbols in one quote call.
    Returns total OI (float) or None if unavailable. I/O wrapper — the gate
    decision lives in put_oi_rising()."""
    syms = [f"NFO:{s}" for s in symbols if s]
    if not syms:
        return None
    try:
        q = kite.quote(syms)
    except Exception as e:
        logger.error("put-OI quote fetch failed: %s", e)
        return None
    total = 0.0
    got = False
    for s in syms:
        d = q.get(s) or {}
        oi = d.get("oi")
        if oi is not None and oi > 0:
            total += float(oi)
            got = True
    return total if got else None


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

    def update_peak(self, price):
        """Raise the open position's high-water mark (for the trailing stop).
        Never lowers it. No-op if no position is open."""
        with self._lock:
            if self._position is not None:
                cur = self._position.get("peak", self._position["entry_price"])
                if price > cur:
                    self._position["peak"] = price

    def close(self):
        with self._lock:
            self._position = None


class SignalContext:
    """Thread-safe holder for the most recent signal's volume-profile context,
    keyed by option symbol. The main thread sets it when it sends an alert; the
    callback thread reads it when the BUY button is tapped, so the journal can
    record the VAH/VAL/conviction that the alert was based on."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_symbol = {}

    def set(self, symbol, ctx):
        with self._lock:
            self._by_symbol[symbol] = dict(ctx)

    def get(self, symbol):
        with self._lock:
            return dict(self._by_symbol.get(symbol, {}))


def monitor_position_tick(kite, position, tracker, conn, db_lock, square_off_time):
    """One tick of position monitoring. Closes the position in DB + tracker
    and sends a Telegram alert if SL/TARGET/TIMEOUT fires. Returns the exit
    reason or None.
    """
    symbol = position["symbol"]
    # Exit in the SAME mode the position was opened in (a weekly track is PAPER
    # even when the bot is LIVE), not the global config flag.
    is_paper = position.get("mode") == "PAPER"
    try:
        q = kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]
    except Exception as e:
        logger.error("Position quote fetch failed for %s: %s", symbol, e)
        return None

    ltp = q.get("last_price")
    if ltp is None or ltp <= 0:
        return None

    bid, _ = extract_bid_ask(q)

    # Trailing-stop high-water mark: peak = max(prior peak, current ltp).
    entry = position["entry_price"]
    prev_peak = position.get("peak", entry)
    peak = ltp if ltp > prev_peak else prev_peak
    tracker.update_peak(ltp)

    reason = decide_exit(
        entry, ltp, now_ist(), square_off_time,
        config.SL_PCT, config.TARGET_PCT,
        peak_price=peak, use_trailing=config.USE_TRAILING_STOP,
        trail_step_pct=getattr(config, "TRAIL_STEP_PCT", 0.15),
    )
    pnl_pct = (ltp - entry) / entry * 100
    logger.info(
        "position_tick %s entry=%.2f ltp=%.2f peak=%.2f bid=%s pnl=%+.2f%% decision=%s",
        symbol, entry, ltp, peak, bid, pnl_pct, reason or "HOLD",
    )
    if reason is None:
        return None

    # Realistic exit reference — best_bid if available, else LTP.
    raw_exit = bid if bid and bid > 0 else ltp
    # Live mode: SELL LIMIT slightly below best_bid to ensure fill on exit.
    sell_price = round_to_tick(max(0.05, raw_exit - config.LIMIT_BUFFER))

    order_id, err = place_sell_limit(kite, symbol, position["qty"], sell_price, paper=is_paper)
    if err:
        send_alert(f"SELL ORDER FAILED for {symbol}: {err}\nWill retry next tick.")
        return None

    if is_paper:
        # PAPER simulates a fill at the realistic best_bid reference.
        recorded_exit = raw_exit
    else:
        # LIVE: confirm the exit actually filled before closing the position.
        fill_qty, avg_price, status = confirm_order_fill(
            kite, order_id, position["qty"],
            config.FILL_CONFIRM_TIMEOUT_SECONDS, config.FILL_POLL_SECONDS,
        )
        if status == "NONE":
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except Exception as e:
                logger.error("cancel of unfilled SELL %s failed: %s", order_id, e)
            send_alert(
                f"SELL NOT FILLED for {symbol} [{reason}] @ ₹{sell_price:.2f}.\n"
                f"Position still OPEN — retrying next tick."
            )
            logger.info("SELL not filled symbol=%s reason=%s — position kept open", symbol, reason)
            return None
        if status == "PARTIAL":
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except Exception as e:
                logger.error("cancel of SELL remainder %s failed: %s", order_id, e)
            send_alert(
                f"⚠️ EXIT PARTIAL for {symbol}: sold {fill_qty}/{position['qty']} @ ₹{avg_price:.2f}. "
                f"{position['qty'] - fill_qty} left — broker auto-square-off will close it; VERIFY in Kite."
            )
        recorded_exit = avg_price

    with db_lock:
        pnl = trades_db.record_sell(conn, position["id"], recorded_exit, reason)
    tracker.close()

    logger.info(
        "EXIT reason=%s symbol=%s entry=%.2f exit=%.2f qty=%d pnl=%+.2f mode=%s",
        reason, symbol, position["entry_price"], recorded_exit, position["qty"],
        pnl, "PAPER" if is_paper else "LIVE",
    )

    mode_tag = "[PAPER] " if is_paper else ""
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


def handle_callback(kite, callback_query, conn, db_lock, tracker, signal_ctx):
    cb_id = callback_query["id"]
    data = callback_query.get("data", "")
    decoded = decode_order_callback(data)
    if decoded is None:
        answer_callback(cb_id, "Invalid order payload")
        return
    symbol, price, qty = decoded  # `price` is the LIMIT from alert time
    enter_position(kite, symbol, price, qty, conn, db_lock, tracker, signal_ctx, cb_id=cb_id)


def enter_position(kite, symbol, price, qty, conn, db_lock, tracker, signal_ctx, cb_id=None, paper=None):
    """Open one position, journal it with the signal's VP context, and arm exit
    monitoring. Shared by the Telegram BUY-button tap (cb_id set) and the PAPER
    auto-enter path (cb_id None — see PAPER_AUTO_ENTER). At-most-one open
    position is enforced here, so both entry paths stay mutually exclusive.

    `paper` overrides config.PAPER_TRADE for this entry (None = use config), so a
    weekly (early-cycle) track is journaled/managed as PAPER even when LIVE."""
    is_paper = config.PAPER_TRADE if paper is None else paper
    if tracker.is_open():
        if cb_id:
            answer_callback(cb_id, "Position already open. Skipping.")
            send_alert(f"Skipping {symbol}: another position is already open.")
        else:
            logger.info("AUTO_ENTER skipped: position already open (%s)", symbol)
        return

    # Monthly loss limit — authoritative gate. Blocks new entries (even from a
    # stale button) once month-to-date realized loss in the active mode reaches
    # the cap. Open positions are unaffected; they keep being managed.
    journal_mode = "PAPER" if is_paper else "LIVE"
    with db_lock:
        mtd_pnl = trades_db.month_to_date_pnl(
            conn, journal_mode, now_ist().strftime("%Y-%m")
        )
    if mtd_pnl <= -config.MONTHLY_LOSS_LIMIT_INR:
        if cb_id:
            answer_callback(cb_id, "Monthly loss limit reached. Order blocked.")
        send_alert(
            f"BLOCKED {symbol}: monthly loss limit hit "
            f"(MTD ₹{mtd_pnl:+.0f}, limit ₹{config.MONTHLY_LOSS_LIMIT_INR}).\n"
            f"No new trades until next month."
        )
        return

    if cb_id:
        answer_callback(cb_id, "Placing order...")

    order_id, err = place_buy_limit(kite, symbol, qty, price, paper=is_paper)
    if err:
        logger.error("BUY order failed: symbol=%s qty=%d price=%.2f err=%s", symbol, qty, price, err)
        send_alert(f"ORDER FAILED for {symbol}: {err}")
        return

    # DRY_RUN-only path (PAPER off, DRY on): preserve original alert-only behavior.
    if not is_paper and config.DRY_RUN:
        send_alert(f"[DRY_RUN] Order placed: {symbol} BUY {qty} @ LIMIT ₹{price}")
        return

    # Determine the actual fill. PAPER simulates a full fill at the alert-time
    # limit; LIVE confirms with the broker, because placing != filling — a
    # marketable limit can rest unfilled if price jumped before the tap.
    if is_paper:
        fill_qty, fill_price = qty, price
    else:
        fill_qty, avg_price, status = confirm_order_fill(
            kite, order_id, qty,
            config.FILL_CONFIRM_TIMEOUT_SECONDS, config.FILL_POLL_SECONDS,
        )
        if status == "NONE":
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except Exception as e:
                logger.error("cancel of unfilled BUY %s failed: %s", order_id, e)
            logger.info("BUY not filled order=%s symbol=%s qty=%d limit=%.2f", order_id, symbol, qty, price)
            send_alert(
                f"BUY NOT FILLED: {symbol} @ ₹{price} (price likely moved past the limit).\n"
                f"No position taken."
            )
            return
        if status == "PARTIAL":
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except Exception as e:
                logger.error("cancel of BUY remainder %s failed: %s", order_id, e)
            send_alert(
                f"⚠️ PARTIAL FILL: {symbol} {fill_qty}/{qty} @ ₹{avg_price:.2f}.\n"
                f"Holding {fill_qty} (odd lot — exit may need manual handling in Kite)."
            )
        fill_price = avg_price

    # Journal the buy (with the signal's volume-profile context) and arm monitoring.
    signal_type = "UP" if symbol.endswith("CE") else "DOWN"
    mode = "PAPER" if is_paper else "LIVE"
    ctx = signal_ctx.get(symbol)
    with db_lock:
        entry_id = trades_db.record_buy(
            conn, symbol, fill_qty, fill_price, signal_type, mode,
            vah=ctx.get("vah"), val=ctx.get("val"), poc=ctx.get("poc"),
            conviction=ctx.get("conviction"),
        )
    tracker.open({
        "id": entry_id, "symbol": symbol, "qty": fill_qty,
        "entry_price": fill_price, "signal_type": signal_type, "mode": mode,
        "peak": fill_price,
    })
    logger.info(
        "BUY filled mode=%s symbol=%s qty=%d entry=%.2f signal=%s conviction=%s sl=%.2f trailing=%s",
        mode, symbol, fill_qty, fill_price, signal_type, ctx.get("conviction"),
        fill_price * (1 - config.SL_PCT), config.USE_TRAILING_STOP,
    )

    mode_tag = "[PAPER] " if is_paper else ""
    conv = ctx.get("conviction")
    conv_line = f"\nConviction: {conv}" if conv and conv != "n/a" else ""
    if config.USE_TRAILING_STOP:
        exit_line = (
            f"Initial SL: -{int(config.SL_PCT*100)}% @ ₹{fill_price * (1 - config.SL_PCT):.2f}\n"
            f"Then trailing stop (locks gains as it rises; no fixed target)"
        )
    else:
        exit_line = (
            f"SL: -{int(config.SL_PCT*100)}% @ ₹{fill_price * (1 - config.SL_PCT):.2f}\n"
            f"Target: +{int(config.TARGET_PCT*100)}% @ ₹{fill_price * (1 + config.TARGET_PCT):.2f}"
        )
    send_alert(
        f"{mode_tag}BUY filled: {symbol}\n"
        f"Entry: ₹{fill_price:.2f} ({fill_qty} qty){conv_line}\n"
        f"{exit_line}\n"
        f"Square-off: {config.SQUARE_OFF_HOUR:02d}:{config.SQUARE_OFF_MINUTE:02d} IST"
    )


def callback_listener(kite, stop_event, conn, db_lock, tracker, signal_ctx):
    """Background thread: long-poll Telegram for button taps and place orders."""
    offset = None
    while not stop_event.is_set():
        updates = get_updates(offset=offset, timeout=25)
        for u in updates:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                try:
                    handle_callback(kite, cq, conn, db_lock, tracker, signal_ctx)
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
    signal_ctx = SignalContext()

    # Mid-day restart recovery: if a row is open in the DB, resume monitoring.
    recovered = trades_db.get_open_position(conn)
    if recovered is not None:
        # Peak isn't persisted in the DB; restart the trailing high-water mark
        # from entry (conservative — the trail re-arms from the entry stop).
        recovered["peak"] = recovered["entry_price"]
        tracker.open(recovered)
        send_alert(
            f"Recovered open position: {recovered['symbol']} "
            f"(qty {recovered['qty']} @ ₹{recovered['entry_price']:.2f}, mode={recovered['mode']})\n"
            f"Resuming exit monitoring."
        )

    stop_event = threading.Event()
    listener = threading.Thread(
        target=callback_listener,
        args=(kite, stop_event, conn, db_lock, tracker, signal_ctx),
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
        f"Risk/trade: ≤₹{config.RISK_PER_TRADE_INR} (max {config.MAX_LOTS} lot) | "
        f"Monthly stop: ₹{config.MONTHLY_LOSS_LIMIT_INR}\n"
        f"Exit: {'SL ' + str(int(config.SL_PCT*100)) + '% + trailing (let winners run)' if config.USE_TRAILING_STOP else 'SL ' + str(int(config.SL_PCT*100)) + '% / Target ' + str(int(config.TARGET_PCT*100)) + '%'}"
        f" / Square-off {config.SQUARE_OFF_HOUR:02d}:{config.SQUARE_OFF_MINUTE:02d}\n"
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

    # Put-OI support baseline (V7): snapshot the combined OI of the two OTM puts
    # just below spot ("support" strikes) right after the ORB forms (~09:30:30).
    # An UP breakout later requires this OI to have RISEN (put writers building
    # support = bullish). Strikes are FIXED here so the read doesn't drift.
    sup_oi_syms = []
    sup_oi_base = None
    try:
        snap_spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
        s1, s2 = support_pe_strikes(snap_spot)
        sup_oi_syms = [
            pe_symbol_at_strike(s1, instruments_nfo),
            pe_symbol_at_strike(s2, instruments_nfo),
        ]
        sup_oi_base = fetch_combined_oi(kite, sup_oi_syms)
    except Exception as e:
        logger.error("put-OI baseline snapshot failed: %s", e)
    logger.info("PUTOI_BASELINE syms=%s base=%s", sup_oi_syms, sup_oi_base)
    if sup_oi_base is None:
        logger.warning("put-OI baseline unavailable — UP put-OI gate will FAIL-OPEN today")

    # Call-OI resistance baseline (V8): mirror of the put-OI support read for the
    # DOWN/PE side. Combined OI of the two OTM calls just ABOVE spot, snapshotted
    # at ~09:30:30. A DOWN breakdown later requires this OI to have RISEN (call
    # writers building resistance overhead = bearish confirm). PE stays a
    # LOW-conviction, paper-only side — this gate just filters the noise.
    res_oi_syms = []
    res_oi_base = None
    try:
        snap_spot_c = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
        r1, r2 = resistance_ce_strikes(snap_spot_c)
        res_oi_syms = [
            ce_symbol_at_strike(r1, instruments_nfo),
            ce_symbol_at_strike(r2, instruments_nfo),
        ]
        res_oi_base = fetch_combined_oi(kite, res_oi_syms)
    except Exception as e:
        logger.error("call-OI baseline snapshot failed: %s", e)
    logger.info("CALLOI_BASELINE syms=%s base=%s", res_oi_syms, res_oi_base)
    if res_oi_base is None:
        logger.warning("call-OI baseline unavailable — DOWN call-OI gate will FAIL-OPEN today")

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

        # ── Signal-quality gates (V7) — evaluated BEFORE consuming a fire so a
        # suppressed signal can re-fire later, exactly like the backtest. The
        # value area is built from the day's 1-min candles (no warmup gate, to
        # match the backtest). UP gets VAH + put-OI support (the validated CE
        # edge); DOWN gets the mirror VAL + call-OI resistance gate but stays a
        # LOW-conviction, paper-only side (PE is not the validated edge).
        vp = (volume_profile(candles_1m, config.VP_BIN_WIDTH, config.VP_VALUE_AREA_PCT)
              if candles_1m else None)
        poc_v, vah_v, val_v = vp if vp else (None, None, None)

        if signal == "UP":
            # VAH gate: breakout must clear the value-area high.
            if not vah_gate_ok("UP", close, vah_v, val_v):
                logger.info("SIGNAL_SUPPRESSED dir=UP gate=VAH close=%.2f vah=%s", close, vah_v)
                continue
            # Put-OI gate: support-put OI must have risen since 09:30. Fail-OPEN
            # if the baseline couldn't be snapshotted (don't lose the whole day).
            if sup_oi_base is not None:
                cur_oi = fetch_combined_oi(kite, sup_oi_syms)
                if not put_oi_rising(cur_oi, sup_oi_base):
                    logger.info("SIGNAL_SUPPRESSED dir=UP gate=putOI cur=%s base=%s",
                                cur_oi, sup_oi_base)
                    continue
        else:  # signal == "DOWN"
            # VAL gate: breakdown must close below the value-area low (mirror of
            # the UP VAH gate).
            if not vah_gate_ok("DOWN", close, vah_v, val_v):
                logger.info("SIGNAL_SUPPRESSED dir=DOWN gate=VAL close=%.2f val=%s", close, val_v)
                continue
            # Call-OI gate: resistance-call OI must have risen since 09:30 (call
            # writers building a ceiling overhead = bearish confirm). Fail-OPEN if
            # the baseline couldn't be snapshotted.
            if res_oi_base is not None:
                cur_coi = fetch_combined_oi(kite, res_oi_syms)
                if not call_oi_rising(cur_coi, res_oi_base):
                    logger.info("SIGNAL_SUPPRESSED dir=DOWN gate=callOI cur=%s base=%s",
                                cur_coi, res_oi_base)
                    continue

        try:
            spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
        except Exception:
            spot = close

        # DTE routing: weekly (early cycle) vs monthly (<= DTE_MONTHLY_MAX, late
        # cycle). Weekly is paper-tracked only; monthly is the actionable trade.
        monthly_dte = days_to_expiry(fut["expiry"], now.date())
        plan = select_trade_plan(
            monthly_dte, config.DTE_MONTHLY_MAX,
            getattr(config, "WEEKLY_TRACK_ONLY", True),
        )
        target_expiry = fut["expiry"] if plan["use_monthly"] else None
        opt = itm_option(spot, signal, instruments_nfo, target_expiry=target_expiry)
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

        if plan["use_monthly"]:
            regime_line = f"Expiry: MONTHLY · {monthly_dte}d to expiry — ACTIONABLE"
        elif plan["track_only"]:
            regime_line = f"Expiry: WEEKLY · monthly {monthly_dte}d out — TRACKING ONLY (no order)"
        else:
            regime_line = f"Expiry: WEEKLY · monthly {monthly_dte}d out — ACTIONABLE"

        # Volume-profile conviction tag — reuses the value area computed for the
        # gate above. Both sides reached here already cleared their value-area
        # edge (gated): UP is HIGH by construction; DOWN/PE passed VAL + call-OI
        # but stays LOW conviction — PE is not the validated edge, paper only.
        if vp:
            if signal == "UP":
                conviction = "HIGH"
                conv_desc = f"★ HIGH CONVICTION — cleared VAH {vah_v:.0f}"
            else:
                conviction = "LOW"
                conv_desc = (f"⚠ LOW CONVICTION (PE — experimental, paper only) — "
                             f"below VAL {val_v:.0f}, call-OI resistance rising")
            va_line = f"VA: POC {poc_v:.0f} / VAH {vah_v:.0f} / VAL {val_v:.0f}\n{conv_desc}"
        else:
            conviction = "n/a"
            va_line = "VA: n/a"

        body = (
            f"{label_dir} — fire {fires_now}/{max_fires}\n"
            f"{fut_symbol} 5m close: {close} {comparator} ORB {'High' if signal=='UP' else 'Low'} "
            f"{ref_level} ({sign}{config.BREAKOUT_BUFFER} buf)\n"
            f"Vol: {last['volume']} (avg {avg_vol:.0f}, x{vol_ratio:.2f})\n"
            f"VWAP: {vwap_str}\n"
            f"Spot Nifty: {spot}\n"
            f"{va_line}\n"
            f"{regime_line}\n"
            f"ITM-1 {buy_side}: {opt_symbol}\n"
            f"Bid/Ask: {bid_ask}"
        )

        logger.info(
            "SIGNAL_FIRED dir=%s fire=%d/%d close=%.2f ref=%.2f spot=%.2f "
            "opt=%s bid=%s ask=%s limit=%s conviction=%s vah=%s val=%s reject=%s",
            signal, fires_now, max_fires, close, ref_level, spot,
            opt_symbol, bid, ask, limit_price, conviction, vah_v, val_v, reject_reason or "none",
        )

        if limit_price is not None:
            qty = position_size(
                limit_price, config.SL_PCT, config.LOT_SIZE,
                config.RISK_PER_TRADE_INR, config.MAX_LOTS,
            )
            # Weekly early-cycle signals are paper-tracked even in LIVE mode, so
            # the loss-limit gate is checked against the mode this trade will use.
            effective_paper = True if plan["track_only"] else config.PAPER_TRADE
            journal_mode = "PAPER" if effective_paper else "LIVE"
            with db_lock:
                mtd_pnl = trades_db.month_to_date_pnl(
                    conn, journal_mode, now.strftime("%Y-%m")
                )
            limit_breached = mtd_pnl <= -config.MONTHLY_LOSS_LIMIT_INR

            if limit_breached:
                send_alert(
                    body + f"\nAuto-order skipped: monthly loss limit hit "
                    f"(MTD ₹{mtd_pnl:+.0f}, limit ₹{config.MONTHLY_LOSS_LIMIT_INR}).\n"
                    f"Trading paused until next month."
                )
            elif qty == 0:
                one_lot_risk = limit_price * config.SL_PCT * config.LOT_SIZE
                send_alert(
                    body + f"\nAuto-order skipped: 1-lot risk ₹{one_lot_risk:.0f} "
                    f"> risk cap ₹{config.RISK_PER_TRADE_INR}.\nPlace manually if intended."
                )
            else:
                signal_ctx.set(opt_symbol, {
                    "vah": vah_v, "val": val_v, "poc": poc_v, "conviction": conviction,
                })
                if plan["track_only"]:
                    # Weekly (early cycle): record as PAPER for the performance
                    # track only — NO button, nothing to act on. You trade only
                    # the monthly (late-cycle) recommendations.
                    send_alert(
                        body + f"\nLIMIT: ₹{limit_price} | Qty: {qty}\n"
                        f"[TRACKING ONLY — weekly journaled as paper, not actionable]"
                    )
                    enter_position(
                        kite, opt_symbol, limit_price, qty,
                        conn, db_lock, tracker, signal_ctx, paper=True,
                    )
                elif config.PAPER_TRADE and getattr(config, "PAPER_AUTO_ENTER", False):
                    # TEMPORARY (Telegram India ban): no tap reachable, so enter
                    # the paper trade directly. The alert is still sent for the
                    # record (no button — enter_position confirms the fill).
                    send_alert(
                        body + f"\nLIMIT: ₹{limit_price} | Qty: {qty} "
                        f"(risk ≤ ₹{config.RISK_PER_TRADE_INR})\n"
                        f"[AUTO-ENTER: paper trade taken automatically — Telegram ban]"
                    )
                    enter_position(
                        kite, opt_symbol, limit_price, qty,
                        conn, db_lock, tracker, signal_ctx,
                    )
                else:
                    payload = encode_order_callback(opt_symbol, limit_price, qty)
                    if config.PAPER_TRADE:
                        mode_tag = " [PAPER]"
                    elif config.DRY_RUN:
                        mode_tag = " [DRY_RUN]"
                    else:
                        mode_tag = ""
                    conv_tag = "★ " if conviction == "HIGH" else ""
                    label = f"{conv_tag}BUY {qty} {buy_side} @ ₹{limit_price}{mode_tag}"
                    markup = build_order_button(label, payload)
                    send_alert(
                        body + f"\nLIMIT: ₹{limit_price} | Qty: {qty} "
                        f"(risk ≤ ₹{config.RISK_PER_TRADE_INR})",
                        reply_markup=markup,
                    )
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
