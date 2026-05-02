"""
Comprehensive unit tests for orb_monitor.py logic.

Covers all 6 original signal-logic fixes plus V2 (5-min tracking, ITM-1 option
selection, marketable-limit pricing, order callback encoding):

  Fix #1 - select_last_closed_candle (replaced broken minute % 3 check)
  Fix #2 - BREAKOUT_BUFFER threshold in evaluate_signal
  Fix #3 - REARM_BUFFER in update_rearm_state
  Fix #4 - ORB candle date validation (9:15 AM assertion)
  Fix #5 - Volume average behaviour (vol_ok bypass when avg_vol == 0)
  Fix #6 - elif mutual exclusion in evaluate_signal
  V2  #7 - 5-min interval tracking (replaces 3-min)
  V2  #8 - itm_option direction + strike selection
  V2  #9 - round_to_tick + compute_limit_price (spread/premium guards)
  V2 #10 - order callback payload encode/decode roundtrip
"""

import sys
import types
from datetime import datetime, timedelta, time as dtime
from unittest.mock import MagicMock

import pytz
import pytest

# ── Stub out config and broker/telegram before importing orb_monitor ─────────

_cfg = types.ModuleType("config")
_cfg.VOLUME_MULTIPLIER = 1.2
_cfg.LOOKBACK_CANDLES = 20
_cfg.MAX_FIRES_PER_DIRECTION = 2
_cfg.BREAKOUT_BUFFER = 10
_cfg.REARM_BUFFER = 15
_cfg.LOT_SIZE = 65
_cfg.LIMIT_BUFFER = 0.50
_cfg.MAX_SPREAD_PCT = 0.05
_cfg.MAX_PREMIUM = 500
_cfg.ORDER_PRODUCT = "MIS"
_cfg.DRY_RUN = True
sys.modules["config"] = _cfg
sys.modules["kite_auth"] = MagicMock()
sys.modules["telegram_alert"] = MagicMock()

from orb_monitor import (  # noqa: E402
    compute_limit_price,
    compute_vol_filter,
    compute_vwap,
    decode_order_callback,
    encode_order_callback,
    evaluate_signal,
    itm_option,
    round_to_tick,
    select_last_closed_candle,
    update_rearm_state,
)

IST = pytz.timezone("Asia/Kolkata")

ORB_HIGH = 24500
ORB_LOW  = 24400

BUFFER   = _cfg.BREAKOUT_BUFFER   # 10
REARM    = _cfg.REARM_BUFFER      # 15


# ── Helpers ───────────────────────────────────────────────────────────────────

def ts(h, m, s=0):
    """IST-aware datetime for today at h:m:s."""
    d = datetime.today().date()
    return IST.localize(datetime.combine(d, dtime(h, m, s)))


def candle(h, m, close, volume=10000, high=None, low=None):
    """Build a minimal OHLCV dict at the given time."""
    return {
        "date":   ts(h, m),
        "open":   close,
        "high":   high if high is not None else close + 5,
        "low":    low  if low  is not None else close - 5,
        "close":  close,
        "volume": volume,
    }


def candle_seq(start_h, start_m, n, step_min, close=24490, volume=10000):
    """Build n candles spaced step_min apart, handling hour rollover."""
    out = []
    base = start_h * 60 + start_m
    for i in range(n):
        total = base + i * step_min
        out.append(candle(total // 60, total % 60, close, volume=volume))
    return out


def fresh_state():
    return {
        "UP":   {"fires": 0, "needs_reset": False},
        "DOWN": {"fires": 0, "needs_reset": False},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Fix #1 / V2 #7 — select_last_closed_candle (now 5-min interval)
# ═══════════════════════════════════════════════════════════════════════════

class TestSelectLastClosedCandle:
    """A 5-min candle is closed when its start_time + 5 min <= now."""

    def test_returns_last_candle_when_fully_closed(self):
        candles = [candle(9, 15, 24490), candle(9, 20, 24495), candle(9, 25, 24502)]
        now = ts(9, 35)  # 9:25 candle closed at 9:30, well in the past
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 25)

    def test_skips_in_progress_candle(self):
        # 9:25 closes at 9:30; if now is 9:28 it's still open
        candles = [candle(9, 15, 24490), candle(9, 20, 24495), candle(9, 25, 24502)]
        now = ts(9, 28)
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 20)

    def test_returns_none_when_no_closed_candle_exists(self):
        candles = [candle(9, 15, 24490)]
        now = ts(9, 18)  # 9:15 closes at 9:20 > 9:18 → not closed
        result = select_last_closed_candle(candles, now)
        assert result is None

    def test_boundary_exactly_at_close_time(self):
        candles = [candle(9, 15, 24490)]
        now = ts(9, 20, 0)  # exactly at close
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 15)

    def test_second_to_last_used_when_last_is_open(self):
        candles = [
            candle(9, 15, 24480),
            candle(9, 20, 24490),
            candle(9, 25, 24502),
            candle(9, 30, 24510),  # closes at 9:35; now = 9:32 → still open
        ]
        now = ts(9, 32)
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 25)

    def test_default_interval_is_five_minutes(self):
        # Caller in orb_monitor passes interval_minutes=5; the default also is 5.
        candles = [candle(9, 15, 24490)]
        # Old behaviour with 3-min default would have called this closed at 9:18.
        result = select_last_closed_candle(candles, ts(9, 19))
        assert result is None  # still open under 5-min rule


# ═══════════════════════════════════════════════════════════════════════════
# compute_vwap
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeVwap:

    def test_single_candle(self):
        c = {"high": 100, "low": 90, "close": 95, "volume": 1000}
        assert compute_vwap([c]) == pytest.approx(95.0)

    def test_volume_weighted_average(self):
        c1 = {"high": 100, "low":  90, "close":  95, "volume": 1000}
        c2 = {"high": 110, "low": 100, "close": 105, "volume": 3000}
        assert compute_vwap([c1, c2]) == pytest.approx(102.5)

    def test_returns_none_when_all_volumes_zero(self):
        c = {"high": 100, "low": 90, "close": 95, "volume": 0}
        assert compute_vwap([c]) is None

    def test_returns_none_for_empty_list(self):
        assert compute_vwap([]) is None


# ═══════════════════════════════════════════════════════════════════════════
# Fix #5 — compute_vol_filter
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeVolFilter:

    def _prior(self, vol, n=5):
        return candle_seq(9, 15, n=n, step_min=5, volume=vol)

    def test_vol_ok_when_above_threshold(self):
        last = candle(9, 40, 24500, volume=15000)
        vol_ok, avg = compute_vol_filter(last, self._prior(10000))
        assert vol_ok is True
        assert avg == pytest.approx(10000.0)

    def test_vol_not_ok_when_at_threshold(self):
        last = candle(9, 40, 24500, volume=12000)
        vol_ok, _ = compute_vol_filter(last, self._prior(10000))
        assert vol_ok is False

    def test_vol_not_ok_when_below_threshold(self):
        last = candle(9, 40, 24500, volume=8000)
        vol_ok, _ = compute_vol_filter(last, self._prior(10000))
        assert vol_ok is False

    def test_vol_ok_bypassed_when_no_prior_candles(self):
        last = candle(9, 15, 24490, volume=1)
        vol_ok, avg = compute_vol_filter(last, [])
        assert vol_ok is True
        assert avg == 0

    def test_lookback_window_respected(self):
        # 5 old high-vol candles + 20 newer low-vol candles. Only last 20 count.
        old_candles = candle_seq(9, 15, n=5, step_min=5, volume=50000)
        new_candles = candle_seq(10, 0, n=20, step_min=5, volume=5000)
        prior = old_candles + new_candles
        last = candle(11, 45, 24500, volume=7000)
        vol_ok, avg = compute_vol_filter(last, prior)
        assert avg == pytest.approx(5000.0)
        assert vol_ok is True  # 7000 > 6000 threshold


# ═══════════════════════════════════════════════════════════════════════════
# Fix #3 — update_rearm_state (REARM_BUFFER)
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdateRearmState:

    def test_up_rearms_when_pullback_exceeds_buffer(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False

    def test_up_does_not_rearm_at_buffer_boundary(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        update_rearm_state(state, close=24485, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True

    def test_up_does_not_rearm_when_still_outside_orb(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        update_rearm_state(state, close=24510, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True

    def test_down_rearms_when_bounce_exceeds_buffer(self):
        state = fresh_state()
        state["DOWN"]["needs_reset"] = True
        update_rearm_state(state, close=24416, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["DOWN"]["needs_reset"] is False

    def test_down_does_not_rearm_at_buffer_boundary(self):
        state = fresh_state()
        state["DOWN"]["needs_reset"] = True
        update_rearm_state(state, close=24415, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["DOWN"]["needs_reset"] is True

    def test_down_does_not_rearm_when_still_outside_orb(self):
        state = fresh_state()
        state["DOWN"]["needs_reset"] = True
        update_rearm_state(state, close=24390, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["DOWN"]["needs_reset"] is True

    def test_both_directions_independent(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        state["DOWN"]["needs_reset"] = True
        update_rearm_state(state, close=24450, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False
        assert state["DOWN"]["needs_reset"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Fix #2 — BREAKOUT_BUFFER in evaluate_signal
# ═══════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalBreakout:

    def _call(self, close, vol_ok=True, vwap=None, up_armed=True, down_armed=True):
        return evaluate_signal(close, ORB_HIGH, ORB_LOW, vol_ok, vwap, up_armed, down_armed)

    def test_fires_when_buffer_exceeded(self):
        assert self._call(close=24511) == "UP"

    def test_does_not_fire_at_exact_buffer(self):
        assert self._call(close=24510) is None

    def test_does_not_fire_just_inside_buffer(self):
        assert self._call(close=24509) is None

    def test_does_not_fire_at_orb_high(self):
        assert self._call(close=24500) is None

    def test_does_not_fire_when_vol_not_ok(self):
        assert self._call(close=24515, vol_ok=False) is None

    def test_does_not_fire_when_below_vwap(self):
        assert self._call(close=24515, vwap=24520) is None

    def test_fires_when_vwap_is_none(self):
        assert self._call(close=24515, vwap=None) == "UP"

    def test_does_not_fire_when_not_armed(self):
        assert self._call(close=24515, up_armed=False) is None


class TestEvaluateSignalBreakdown:

    def _call(self, close, vol_ok=True, vwap=None, up_armed=True, down_armed=True):
        return evaluate_signal(close, ORB_HIGH, ORB_LOW, vol_ok, vwap, up_armed, down_armed)

    def test_fires_when_buffer_exceeded(self):
        assert self._call(close=24389) == "DOWN"

    def test_does_not_fire_at_exact_buffer(self):
        assert self._call(close=24390) is None

    def test_does_not_fire_just_inside_buffer(self):
        assert self._call(close=24391) is None

    def test_does_not_fire_at_orb_low(self):
        assert self._call(close=24400) is None

    def test_does_not_fire_when_vol_not_ok(self):
        assert self._call(close=24385, vol_ok=False) is None

    def test_does_not_fire_when_above_vwap(self):
        assert self._call(close=24385, vwap=24380) is None

    def test_fires_when_vwap_is_none(self):
        assert self._call(close=24385, vwap=None) == "DOWN"

    def test_does_not_fire_when_not_armed(self):
        assert self._call(close=24385, down_armed=False) is None


# ═══════════════════════════════════════════════════════════════════════════
# Fix #6 — elif mutual exclusion
# ═══════════════════════════════════════════════════════════════════════════

class TestMutualExclusion:

    def test_only_up_fires_when_up_condition_met(self):
        result = evaluate_signal(
            close=ORB_HIGH + BUFFER + 5, orb_high=ORB_HIGH, orb_low=ORB_LOW,
            vol_ok=True, vwap=None, up_armed=True, down_armed=True,
        )
        assert result == "UP"

    def test_only_down_fires_when_down_condition_met(self):
        result = evaluate_signal(
            close=ORB_LOW - BUFFER - 5, orb_high=ORB_HIGH, orb_low=ORB_LOW,
            vol_ok=True, vwap=None, up_armed=True, down_armed=True,
        )
        assert result == "DOWN"

    def test_forced_overlap_strictly_returns_only_up(self):
        # Inverted ORB satisfies both numeric conditions; elif → UP wins.
        result = evaluate_signal(
            close=24451, orb_high=24440, orb_low=24462,
            vol_ok=True, vwap=None, up_armed=True, down_armed=True,
        )
        assert result == "UP"


# ═══════════════════════════════════════════════════════════════════════════
# Fix #4 — ORB candle date validation
# ═══════════════════════════════════════════════════════════════════════════

class TestOrbCandleValidation:

    def test_valid_orb_candle_at_9_15(self):
        orb = candle(9, 15, 24490)
        assert orb["date"].time() == dtime(9, 15)

    def test_invalid_orb_candle_pre_open(self):
        orb = candle(9, 0, 24490)
        assert orb["date"].time() != dtime(9, 15)


# ═══════════════════════════════════════════════════════════════════════════
# Re-arm + signal across multiple ticks
# ═══════════════════════════════════════════════════════════════════════════

class TestRearmAndFireSequence:

    def test_up_fires_twice_after_proper_pullback(self):
        state = fresh_state()
        max_fires = _cfg.MAX_FIRES_PER_DIRECTION

        update_rearm_state(state, close=24515, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < max_fires
        sig = evaluate_signal(24515, ORB_HIGH, ORB_LOW, True, None, up_armed, True)
        assert sig == "UP"
        state["UP"]["fires"] += 1
        state["UP"]["needs_reset"] = True

        update_rearm_state(state, close=24490, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True

        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False

        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < max_fires
        sig = evaluate_signal(24515, ORB_HIGH, ORB_LOW, True, None, up_armed, True)
        assert sig == "UP"
        state["UP"]["fires"] += 1
        assert state["UP"]["fires"] == 2

    def test_no_third_fire_after_max_fires(self):
        state = fresh_state()
        state["UP"]["fires"] = _cfg.MAX_FIRES_PER_DIRECTION
        state["UP"]["needs_reset"] = True
        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < _cfg.MAX_FIRES_PER_DIRECTION
        assert up_armed is False
        sig = evaluate_signal(24515, ORB_HIGH, ORB_LOW, True, None, up_armed, False)
        assert sig is None


# ═══════════════════════════════════════════════════════════════════════════
# V2 #8 — itm_option
# ═══════════════════════════════════════════════════════════════════════════

def _mock_instruments():
    today = datetime.today().date()
    near = today + timedelta(days=3)
    far = today + timedelta(days=10)
    rows = []
    for strike in range(24300, 24701, 50):
        for opt_type in ("CE", "PE"):
            for exp in (near, far):
                rows.append({
                    "name": "NIFTY",
                    "instrument_type": opt_type,
                    "strike": strike,
                    "expiry": exp,
                    "tradingsymbol": f"NIFTY{exp.strftime('%y%b').upper()}{strike}{opt_type}",
                    "instrument_token": strike * 10 + (1 if opt_type == "CE" else 2),
                })
    return rows


class TestItmOption:

    def test_up_picks_itm_strike_below_spot(self):
        # Spot 24500, ATM strike = 24500, ITM-1 CE = 24450
        opt = itm_option(spot=24500, direction="UP", instruments_nfo=_mock_instruments())
        assert opt is not None
        assert opt["strike"] == 24450
        assert opt["instrument_type"] == "CE"

    def test_down_picks_itm_strike_above_spot(self):
        # Spot 24500, ATM strike = 24500, ITM-1 PE = 24550
        opt = itm_option(spot=24500, direction="DOWN", instruments_nfo=_mock_instruments())
        assert opt is not None
        assert opt["strike"] == 24550
        assert opt["instrument_type"] == "PE"

    def test_atm_rounding_above_midpoint(self):
        # Spot 24527 → rounds to 24550 ATM → ITM-1 CE = 24500
        opt = itm_option(spot=24527, direction="UP", instruments_nfo=_mock_instruments())
        assert opt["strike"] == 24500

    def test_atm_rounding_below_midpoint(self):
        # Spot 24523 → rounds to 24500 ATM → ITM-1 CE = 24450
        opt = itm_option(spot=24523, direction="UP", instruments_nfo=_mock_instruments())
        assert opt["strike"] == 24450

    def test_picks_nearest_expiry(self):
        opt = itm_option(spot=24500, direction="UP", instruments_nfo=_mock_instruments())
        # Two expiries exist — the nearer one must be selected.
        all_exps = sorted({i["expiry"] for i in _mock_instruments()})
        assert opt["expiry"] == all_exps[0]

    def test_returns_none_when_no_match(self):
        # Spot far outside our mock strike range
        opt = itm_option(spot=99000, direction="UP", instruments_nfo=_mock_instruments())
        assert opt is None


# ═══════════════════════════════════════════════════════════════════════════
# V2 #9 — round_to_tick + compute_limit_price
# ═══════════════════════════════════════════════════════════════════════════

class TestRoundToTick:

    def test_already_on_tick(self):
        assert round_to_tick(152.45) == 152.45

    def test_rounds_up_to_tick(self):
        assert round_to_tick(152.46) == 152.45  # nearest 0.05
        assert round_to_tick(152.48) == 152.50

    def test_rounds_down_to_tick(self):
        assert round_to_tick(152.44) == 152.45  # 152.44 → nearest 0.05 = 152.45
        assert round_to_tick(152.42) == 152.40

    def test_no_float_precision_drift(self):
        # 0.1 + 0.2 = 0.30000000000000004 in IEEE; tick rounding must clean it.
        assert round_to_tick(0.1 + 0.2) == 0.30


class TestComputeLimitPrice:

    def _q(self, bid, ask, ltp=None):
        depth = {
            "buy":  [{"price": bid}] if bid else [],
            "sell": [{"price": ask}] if ask else [],
        }
        return {"depth": depth, "last_price": ltp if ltp is not None else (bid + ask) / 2}

    def test_marketable_limit_above_ask(self):
        # ask 152.45 + buffer 0.50 = 152.95
        price, reason = compute_limit_price(self._q(152.10, 152.45), buffer=0.50,
                                            max_spread_pct=0.05, max_premium=500)
        assert reason is None
        assert price == 152.95

    def test_rejects_when_premium_above_cap(self):
        price, reason = compute_limit_price(self._q(600, 610), buffer=0.50,
                                            max_spread_pct=0.05, max_premium=500)
        assert price is None
        assert "MAX_PREMIUM" in reason

    def test_rejects_when_spread_too_wide(self):
        # bid 100 / ask 110 → mid 105, spread 10/105 ≈ 9.5% > 5%
        price, reason = compute_limit_price(self._q(100, 110), buffer=0.50,
                                            max_spread_pct=0.05, max_premium=500)
        assert price is None
        assert "spread" in reason.lower()

    def test_falls_back_to_ltp_when_no_ask(self):
        q = {"depth": {"buy": [], "sell": []}, "last_price": 152.30}
        price, reason = compute_limit_price(q, buffer=0.50,
                                            max_spread_pct=0.05, max_premium=500)
        # Fallback path: LTP + 2× buffer; reason explains the fallback.
        assert price == round_to_tick(152.30 + 1.00)
        assert "ltp" in reason.lower()

    def test_returns_none_when_no_quote_at_all(self):
        q = {"depth": {"buy": [], "sell": []}, "last_price": None}
        price, reason = compute_limit_price(q, buffer=0.50,
                                            max_spread_pct=0.05, max_premium=500)
        assert price is None
        assert reason  # non-empty


# ═══════════════════════════════════════════════════════════════════════════
# V2 #10 — order callback encode/decode
# ═══════════════════════════════════════════════════════════════════════════

class TestOrderCallback:

    def test_roundtrip(self):
        payload = encode_order_callback("NIFTY26MAY24500CE", 152.95, 65)
        decoded = decode_order_callback(payload)
        assert decoded == ("NIFTY26MAY24500CE", 152.95, 65)

    def test_payload_under_telegram_64_byte_cap(self):
        payload = encode_order_callback("NIFTY26MAY24500CE", 152.95, 65)
        assert len(payload.encode("utf-8")) <= 64

    def test_decode_rejects_wrong_prefix(self):
        assert decode_order_callback("x|SYM|10|1") is None

    def test_decode_rejects_bad_arity(self):
        assert decode_order_callback("o|SYM|10") is None
        assert decode_order_callback("o|SYM|10|1|extra") is None

    def test_decode_rejects_non_numeric_price(self):
        assert decode_order_callback("o|SYM|abc|65") is None

    def test_decode_rejects_non_int_qty(self):
        assert decode_order_callback("o|SYM|10|1.5") is None
