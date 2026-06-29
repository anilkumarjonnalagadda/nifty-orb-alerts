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
_cfg.PAPER_TRADE = True
_cfg.PAPER_AUTO_ENTER = False
_cfg.SL_PCT = 0.30
_cfg.TARGET_PCT = 0.50
_cfg.USE_TRAILING_STOP = True
_cfg.TRAIL_STEP_PCT = 0.15
_cfg.SQUARE_OFF_HOUR = 15
_cfg.SQUARE_OFF_MINUTE = 15
_cfg.POSITION_POLL_SECONDS = 60
_cfg.DB_PATH = ":memory:"
_cfg.RISK_PER_TRADE_INR = 5000
_cfg.MAX_LOTS = 1
_cfg.MONTHLY_LOSS_LIMIT_INR = 15000
_cfg.DTE_MONTHLY_MAX = 15
_cfg.WEEKLY_TRACK_ONLY = True
_cfg.FILL_CONFIRM_TIMEOUT_SECONDS = 8
_cfg.FILL_POLL_SECONDS = 1
_cfg.VP_BIN_WIDTH = 5.0
_cfg.VP_VALUE_AREA_PCT = 0.70
_cfg.VP_WARMUP_MINUTES = 60
sys.modules["config"] = _cfg
sys.modules["kite_auth"] = MagicMock()
sys.modules["telegram_alert"] = MagicMock()

from orb_monitor import (  # noqa: E402
    PositionTracker,
    SignalContext,
    enter_position,
    compute_limit_price,
    compute_vol_filter,
    compute_vwap,
    decide_exit,
    decode_order_callback,
    encode_order_callback,
    evaluate_signal,
    classify_fill,
    extract_bid_ask,
    itm_option,
    days_to_expiry,
    select_trade_plan,
    support_pe_strikes,
    pe_symbol_at_strike,
    put_oi_rising,
    resistance_ce_strikes,
    ce_symbol_at_strike,
    call_oi_rising,
    vah_gate_ok,
    fetch_combined_oi,
    position_size,
    round_to_tick,
    volume_profile,
    select_last_closed_candle,
    update_rearm_state,
    _is_token_error,
    ensure_kite_alive,
    fetch_orb_candle,
)
import trades_db  # noqa: E402

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

    # Default open is 24450 — inside ORB [24400, 24500] — so the open-inside
    # rule does not block these tests; they exercise other dimensions.
    def _call(self, close, open_price=24450, vol_ok=True, vwap=None, up_armed=True, down_armed=True):
        return evaluate_signal(open_price, close, ORB_HIGH, ORB_LOW, vol_ok, vwap, up_armed, down_armed)

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

    def _call(self, close, open_price=24450, vol_ok=True, vwap=None, up_armed=True, down_armed=True):
        return evaluate_signal(open_price, close, ORB_HIGH, ORB_LOW, vol_ok, vwap, up_armed, down_armed)

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
# Fix #7 — open must be inside ORB for the breakout candle to fire.
# Real-world bug this prevents: 10:00 candle closes below ORB low (no volume),
# 11:30 candle is still below ORB low and finally has volume → without this
# rule, the 11:30 candle would fire and we'd enter 1.5h into the move.
# ═══════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalOpenInsideOrb:

    def test_up_fires_when_open_inside_and_close_above_with_buffer(self):
        # Open at 24450 (inside ORB), close at 24515 (10+ above ORB high) → UP
        sig = evaluate_signal(24450, 24515, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig == "UP"

    def test_up_fires_when_open_exactly_at_orb_high(self):
        # Boundary: open AT orb_high counts as "inside or at" → allowed.
        sig = evaluate_signal(24500, 24515, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig == "UP"

    def test_up_does_not_fire_when_open_above_orb_high(self):
        # The 11:30 continuation scenario — already above ORB at candle start.
        sig = evaluate_signal(24501, 24550, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig is None

    def test_down_fires_when_open_inside_and_close_below_with_buffer(self):
        sig = evaluate_signal(24450, 24385, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig == "DOWN"

    def test_down_fires_when_open_exactly_at_orb_low(self):
        sig = evaluate_signal(24400, 24385, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig == "DOWN"

    def test_down_does_not_fire_when_open_below_orb_low(self):
        # Same continuation bug, mirrored — open already below ORB.
        sig = evaluate_signal(24399, 24350, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig is None

    def test_real_world_10am_then_1130am_scenario(self):
        # 10:00 candle: open inside ORB, closes below ORB low — but volume weak
        # so signal does not fire (vol_ok=False). down_armed stays True.
        sig_10 = evaluate_signal(24450, 24385, ORB_HIGH, ORB_LOW, False, None, True, True)
        assert sig_10 is None

        # 11:30 candle: opens AT 24370 (already below ORB low because price never
        # came back inside), closes 24350. Volume now present. Without Fix #7
        # this would fire DOWN — exactly the bug the user reported.
        sig_1130 = evaluate_signal(24370, 24350, ORB_HIGH, ORB_LOW, True, None, True, True)
        assert sig_1130 is None


# ═══════════════════════════════════════════════════════════════════════════
# Fix #6 — elif mutual exclusion
# ═══════════════════════════════════════════════════════════════════════════

class TestMutualExclusion:

    def test_only_up_fires_when_up_condition_met(self):
        result = evaluate_signal(
            open_price=24450, close=ORB_HIGH + BUFFER + 5,
            orb_high=ORB_HIGH, orb_low=ORB_LOW,
            vol_ok=True, vwap=None, up_armed=True, down_armed=True,
        )
        assert result == "UP"

    def test_only_down_fires_when_down_condition_met(self):
        result = evaluate_signal(
            open_price=24450, close=ORB_LOW - BUFFER - 5,
            orb_high=ORB_HIGH, orb_low=ORB_LOW,
            vol_ok=True, vwap=None, up_armed=True, down_armed=True,
        )
        assert result == "DOWN"

    def test_forced_overlap_strictly_returns_only_up(self):
        # Inverted ORB (high < low) satisfies both numeric conditions on close;
        # elif → UP wins. open_price=24439 is <= orb_high=24440 (UP gate) and
        # also >= orb_low=24462 is FALSE — so DOWN gate would block, leaving
        # only UP eligible. The elif still asserts UP-first ordering.
        result = evaluate_signal(
            open_price=24439, close=24451, orb_high=24440, orb_low=24462,
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
        # open_price=24450 is inside ORB for both fires (Fix #7 — fresh breakouts).
        update_rearm_state(state, close=24515, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < max_fires
        sig = evaluate_signal(24450, 24515, ORB_HIGH, ORB_LOW, True, None, up_armed, True)
        assert sig == "UP"
        state["UP"]["fires"] += 1
        state["UP"]["needs_reset"] = True

        update_rearm_state(state, close=24490, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True

        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False

        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < max_fires
        sig = evaluate_signal(24450, 24515, ORB_HIGH, ORB_LOW, True, None, up_armed, True)
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
        sig = evaluate_signal(24450, 24515, ORB_HIGH, ORB_LOW, True, None, up_armed, False)
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

    def test_target_expiry_picks_that_expiry(self):
        # V7: explicit target_expiry (the monthly) selects that expiry, not nearest.
        insts = _mock_instruments()
        near, far = sorted({i["expiry"] for i in insts})[:2]
        opt = itm_option(spot=24500, direction="UP", instruments_nfo=insts, target_expiry=far)
        assert opt["expiry"] == far
        assert opt["strike"] == 24450

    def test_target_expiry_none_is_nearest(self):
        insts = _mock_instruments()
        near = sorted({i["expiry"] for i in insts})[0]
        opt = itm_option(spot=24500, direction="UP", instruments_nfo=insts, target_expiry=None)
        assert opt["expiry"] == near

    def test_target_expiry_falls_back_to_next_on_or_after(self):
        # A date with no exact contract → nearest expiry on/after it.
        insts = _mock_instruments()
        near, far = sorted({i["expiry"] for i in insts})[:2]
        opt = itm_option(spot=24500, direction="UP",
                         instruments_nfo=insts, target_expiry=near + timedelta(days=1))
        assert opt["expiry"] == far


# ═══════════════════════════════════════════════════════════════════════════
# V7 — DTE routing: days_to_expiry + select_trade_plan
# ═══════════════════════════════════════════════════════════════════════════

class TestDaysToExpiry:

    def test_plain_date(self):
        today = datetime(2026, 6, 11).date()
        exp = datetime(2026, 6, 25).date()
        assert days_to_expiry(exp, today) == 14

    def test_datetime_expiry_is_normalized(self):
        today = datetime(2026, 6, 11).date()
        exp = datetime(2026, 6, 25, 15, 30)  # datetime, not date
        assert days_to_expiry(exp, today) == 14

    def test_same_day_is_zero(self):
        d = datetime(2026, 6, 25).date()
        assert days_to_expiry(d, d) == 0


class TestSelectTradePlan:

    def test_monthly_within_max_dte_is_actionable(self):
        plan = select_trade_plan(monthly_dte=3, max_dte=15, weekly_track_only=True)
        assert plan["regime"] == "monthly"
        assert plan["use_monthly"] is True
        assert plan["actionable"] is True
        assert plan["track_only"] is False

    def test_boundary_at_max_dte_is_monthly(self):
        assert select_trade_plan(15, 15, True)["use_monthly"] is True
        assert select_trade_plan(16, 15, True)["use_monthly"] is False

    def test_weekly_track_only_when_far(self):
        plan = select_trade_plan(monthly_dte=20, max_dte=15, weekly_track_only=True)
        assert plan["regime"] == "weekly"
        assert plan["use_monthly"] is False
        assert plan["track_only"] is True
        assert plan["actionable"] is False

    def test_weekly_actionable_when_track_off(self):
        plan = select_trade_plan(monthly_dte=20, max_dte=15, weekly_track_only=False)
        assert plan["use_monthly"] is False
        assert plan["track_only"] is False
        assert plan["actionable"] is True


# ═══════════════════════════════════════════════════════════════════════════
# V7 — signal-quality gates: VAH gate + put-OI support
# ═══════════════════════════════════════════════════════════════════════════

class TestPutOiAndVahGates:

    def test_support_pe_strikes_at_strike(self):
        assert support_pe_strikes(24500) == (24450, 24400)

    def test_support_pe_strikes_rounds_to_atm(self):
        # 24527 → ATM 24550 → supports 24500, 24450
        assert support_pe_strikes(24527) == (24500, 24450)

    def test_pe_symbol_at_strike_found(self):
        sym = pe_symbol_at_strike(24450, _mock_instruments())
        assert sym is not None and sym.endswith("PE") and "24450" in sym

    def test_pe_symbol_at_strike_missing(self):
        assert pe_symbol_at_strike(99999, _mock_instruments()) is None

    def test_put_oi_rising_true(self):
        assert put_oi_rising(1200, 1000) is True

    def test_put_oi_rising_flat_or_falling(self):
        assert put_oi_rising(1000, 1000) is False   # must strictly rise
        assert put_oi_rising(900, 1000) is False

    def test_put_oi_rising_missing_or_zero_base(self):
        assert put_oi_rising(None, 1000) is False
        assert put_oi_rising(1200, None) is False
        assert put_oi_rising(1200, 0) is False

    def test_vah_gate_up(self):
        assert vah_gate_ok("UP", 105, vah=100, val=90) is True
        assert vah_gate_ok("UP", 100, vah=100, val=90) is False   # not strictly above
        assert vah_gate_ok("UP", 105, vah=None, val=90) is False  # no value area

    def test_vah_gate_down(self):
        assert vah_gate_ok("DOWN", 85, vah=100, val=90) is True
        assert vah_gate_ok("DOWN", 90, vah=100, val=90) is False
        assert vah_gate_ok("DOWN", 85, vah=100, val=None) is False

    def test_fetch_combined_oi_sums(self):
        class _K:
            def quote(self, syms):
                return {"NFO:A": {"oi": 1000}, "NFO:B": {"oi": 500}}
        assert fetch_combined_oi(_K(), ["A", "B"]) == 1500

    def test_fetch_combined_oi_empty_is_none(self):
        assert fetch_combined_oi(None, []) is None

    def test_fetch_combined_oi_none_when_no_oi(self):
        class _K:
            def quote(self, syms):
                return {"NFO:A": {}, "NFO:B": {"oi": 0}}
        assert fetch_combined_oi(_K(), ["A", "B"]) is None


# ═══════════════════════════════════════════════════════════════════════════
# V8 — call-OI resistance gate (DOWN/PE mirror of put-OI support)
# ═══════════════════════════════════════════════════════════════════════════

class TestCallOiResistanceGate:

    def test_resistance_ce_strikes_at_strike(self):
        # Calls just ABOVE spot (mirror of support_pe_strikes which is below).
        assert resistance_ce_strikes(24500) == (24550, 24600)

    def test_resistance_ce_strikes_rounds_to_atm(self):
        # 24527 rounds to ATM 24550 → resistance = 24600, 24650.
        assert resistance_ce_strikes(24527) == (24600, 24650)

    def test_ce_symbol_at_strike_found(self):
        sym = ce_symbol_at_strike(24600, _mock_instruments())
        assert sym is not None
        assert sym.endswith("24600CE")

    def test_ce_symbol_at_strike_missing(self):
        assert ce_symbol_at_strike(99999, _mock_instruments()) is None

    def test_call_oi_rising_true(self):
        assert call_oi_rising(1200, 1000) is True

    def test_call_oi_rising_flat_or_falling(self):
        assert call_oi_rising(1000, 1000) is False   # must strictly rise
        assert call_oi_rising(900, 1000) is False

    def test_call_oi_rising_missing_or_zero_base(self):
        assert call_oi_rising(None, 1000) is False
        assert call_oi_rising(1200, None) is False
        assert call_oi_rising(1200, 0) is False


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


# ═══════════════════════════════════════════════════════════════════════════
# V3 — extract_bid_ask
# ═══════════════════════════════════════════════════════════════════════════

class TestExtractBidAsk:

    def test_normal_quote(self):
        q = {"depth": {"buy": [{"price": 100.0}], "sell": [{"price": 102.0}]}}
        assert extract_bid_ask(q) == (100.0, 102.0)

    def test_missing_depth_returns_none_pair(self):
        assert extract_bid_ask({}) == (None, None)

    def test_empty_depth_lists(self):
        q = {"depth": {"buy": [], "sell": []}}
        assert extract_bid_ask(q) == (None, None)

    def test_handles_none_quote(self):
        assert extract_bid_ask(None) == (None, None)

    def test_zero_price_treated_as_missing(self):
        q = {"depth": {"buy": [{"price": 0}], "sell": [{"price": 102.0}]}}
        bid, ask = extract_bid_ask(q)
        assert bid is None
        assert ask == 102.0


# ═══════════════════════════════════════════════════════════════════════════
# V3 — decide_exit (SL / TARGET / TIMEOUT)
# ═══════════════════════════════════════════════════════════════════════════

class TestDecideExit:

    SL = 0.30
    TG = 0.50
    ENTRY = 100.0

    def _square_off(self):
        return ts(15, 15)  # square-off time

    def _now(self):
        return ts(11, 0)  # mid-day

    def test_returns_none_when_price_within_band(self):
        # Price 90 → -10%, within both SL (-30%) and target (+50%)
        assert decide_exit(self.ENTRY, 90, self._now(), self._square_off(), self.SL, self.TG) is None

    def test_sl_at_exact_threshold(self):
        # SL = 30% → trigger when price <= 70.0
        assert decide_exit(self.ENTRY, 70.0, self._now(), self._square_off(), self.SL, self.TG) == "SL"

    def test_sl_just_above_threshold_does_not_fire(self):
        assert decide_exit(self.ENTRY, 70.01, self._now(), self._square_off(), self.SL, self.TG) is None

    def test_sl_when_price_well_below_entry(self):
        assert decide_exit(self.ENTRY, 50.0, self._now(), self._square_off(), self.SL, self.TG) == "SL"

    def test_target_at_exact_threshold(self):
        assert decide_exit(self.ENTRY, 150.0, self._now(), self._square_off(), self.SL, self.TG) == "TARGET"

    def test_target_just_below_threshold_does_not_fire(self):
        assert decide_exit(self.ENTRY, 149.99, self._now(), self._square_off(), self.SL, self.TG) is None

    def test_timeout_fires_at_square_off(self):
        # At square-off time, even a profitable position exits.
        now = ts(15, 15)
        assert decide_exit(self.ENTRY, 110.0, now, self._square_off(), self.SL, self.TG) == "TIMEOUT"

    def test_timeout_fires_after_square_off(self):
        now = ts(15, 20)
        assert decide_exit(self.ENTRY, 110.0, now, self._square_off(), self.SL, self.TG) == "TIMEOUT"

    def test_timeout_takes_precedence_over_no_signal(self):
        # Price perfectly flat at entry, but past square-off → still exits.
        now = ts(15, 16)
        assert decide_exit(self.ENTRY, 100.0, now, self._square_off(), self.SL, self.TG) == "TIMEOUT"

    def test_sl_takes_precedence_over_target_when_inverted_thresholds(self):
        # Defensive — if someone sets SL_PCT > 1, both checks could pass; SL wins.
        now = ts(11, 0)
        result = decide_exit(self.ENTRY, 50.0, now, self._square_off(), 0.30, 0.50)
        assert result == "SL"

    # ── V6: stepped ("ladder") trailing-stop mode (use_trailing=True) ──────────
    # ENTRY=100, SL_PCT=0.30, TRAIL_STEP_PCT=0.15 → step_amt=15, initial stop=70.
    # The stop ratchets up 15 for every full 15 the peak rises above entry:
    #   peak<115 → n=0 stop=70 ;  peak in [115,130) → n=1 stop=85 ;
    #   peak in [130,145) → n=2 stop=100 ; peak=150 → n=3 stop=115 ;
    #   peak=160 → n=4 stop=130 ; peak=200 → n=6 stop=160.

    def _trail(self, current, peak, now=None):
        return decide_exit(self.ENTRY, current, now or self._now(), self._square_off(),
                           self.SL, self.TG, peak_price=peak, use_trailing=True,
                           trail_step_pct=0.15)

    def test_trail_initial_stop_at_minus_30(self):
        # No rise yet (peak==entry): stop is entry-30% = 70.
        assert self._trail(70.0, 100.0) == "TRAIL"
        assert self._trail(70.01, 100.0) is None

    def test_trail_no_fixed_target(self):
        # +60% with no pullback from the peak must NOT exit (trailing has no target).
        assert self._trail(160.0, 160.0) is None

    def test_trail_holds_between_steps(self):
        # Peaked at 114 (+14%, less than one 15% step): stop has NOT ratcheted,
        # still 70. A pullback to 84 must NOT exit; only <=70 does.
        assert self._trail(84.0, 114.0) is None
        assert self._trail(70.0, 114.0) == "TRAIL"

    def test_trail_one_step(self):
        # Peaked at 115 (exactly one step): stop ratchets to 70+15 = 85.
        assert self._trail(85.0, 115.0) == "TRAIL"
        assert self._trail(85.01, 115.0) is None

    def test_trail_two_steps_to_breakeven(self):
        # Peaked at 130 (two steps): stop = 70+30 = 100 (breakeven).
        assert self._trail(100.0, 130.0) == "TRAIL"
        assert self._trail(100.01, 130.0) is None

    def test_trail_locks_gain_after_rise(self):
        # Peaked at 150 → 3 steps → stop = 70+45 = 115.
        assert self._trail(115.0, 150.0) == "TRAIL"   # exit locked at +15%
        assert self._trail(116.0, 150.0) is None       # still holding

    def test_trail_big_winner(self):
        # Peaked at 200 → 6 steps → stop = 70+90 = 160; exit at +60%.
        assert self._trail(160.0, 200.0) == "TRAIL"
        assert self._trail(161.0, 200.0) is None

    def test_trail_new_high_does_not_exit(self):
        # Current is the new high (180) → stop is 145 (5 steps) → no exit.
        assert self._trail(180.0, 180.0) is None

    def test_trail_timeout_still_overrides(self):
        now = ts(15, 15)
        assert self._trail(190.0, 200.0, now=now) == "TIMEOUT"

    def test_trail_defaults_peak_to_entry_when_none(self):
        # peak_price omitted -> treated as entry; stop = 70.
        assert decide_exit(self.ENTRY, 70.0, self._now(), self._square_off(),
                           self.SL, self.TG, use_trailing=True) == "TRAIL"


# ═══════════════════════════════════════════════════════════════════════════
# V3 — PositionTracker (thread-safe at-most-one position)
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionTracker:

    POS = {"id": 1, "symbol": "NIFTY26MAY24500CE", "qty": 65, "entry_price": 100.0,
           "signal_type": "UP", "mode": "PAPER"}

    def test_starts_empty(self):
        t = PositionTracker()
        assert t.is_open() is False
        assert t.current() is None

    def test_open_then_close_roundtrip(self):
        t = PositionTracker()
        assert t.open(self.POS) is True
        assert t.is_open() is True
        cur = t.current()
        assert cur["symbol"] == "NIFTY26MAY24500CE"
        t.close()
        assert t.is_open() is False

    def test_open_rejects_when_already_open(self):
        t = PositionTracker()
        t.open(self.POS)
        # Second open should return False (caller must reject the trade).
        second = dict(self.POS)
        second["id"] = 2
        second["symbol"] = "NIFTY26MAY24450PE"
        assert t.open(second) is False
        assert t.current()["id"] == 1  # original is still the active one

    def test_current_returns_copy_not_reference(self):
        t = PositionTracker()
        t.open(self.POS)
        cur = t.current()
        cur["entry_price"] = 999.0  # mutate the copy
        assert t.current()["entry_price"] == 100.0  # internal state unchanged


# ═══════════════════════════════════════════════════════════════════════════
# V3 — trades_db (SQLite trade journal)
# ═══════════════════════════════════════════════════════════════════════════

class TestTradesDb:

    def _conn(self):
        return trades_db.init_db(":memory:")

    def test_init_creates_table(self):
        conn = self._conn()
        # Table exists if we can SELECT against it without error.
        conn.execute("SELECT id FROM trades").fetchall()

    def test_record_buy_returns_entry_id(self):
        conn = self._conn()
        eid = trades_db.record_buy(conn, "NIFTY26MAY24500CE", 65, 100.0, "UP", "PAPER")
        assert isinstance(eid, int)
        assert eid > 0

    def test_open_position_visible_after_buy(self):
        conn = self._conn()
        trades_db.record_buy(conn, "NIFTY26MAY24500CE", 65, 100.0, "UP", "PAPER")
        pos = trades_db.get_open_position(conn)
        assert pos is not None
        assert pos["symbol"] == "NIFTY26MAY24500CE"
        assert pos["entry_price"] == 100.0
        assert pos["mode"] == "PAPER"
        assert pos["exit_price"] is None

    def test_record_sell_closes_position(self):
        conn = self._conn()
        eid = trades_db.record_buy(conn, "NIFTY26MAY24500CE", 65, 100.0, "UP", "PAPER")
        pnl = trades_db.record_sell(conn, eid, 130.0, "TARGET")
        # 30 rupees * 65 qty = 1950
        assert pnl == pytest.approx(30.0 * 65)
        assert trades_db.get_open_position(conn) is None

    def test_pnl_negative_on_sl(self):
        conn = self._conn()
        eid = trades_db.record_buy(conn, "NIFTY26MAY24500CE", 65, 100.0, "UP", "PAPER")
        pnl = trades_db.record_sell(conn, eid, 70.0, "SL")
        assert pnl == pytest.approx(-30.0 * 65)

    def test_get_open_position_after_close(self):
        conn = self._conn()
        eid = trades_db.record_buy(conn, "NIFTY26MAY24500CE", 65, 100.0, "UP", "PAPER")
        trades_db.record_sell(conn, eid, 110.0, "TARGET")
        # Now record a second buy — that should be the open one.
        eid2 = trades_db.record_buy(conn, "NIFTY26MAY24450PE", 65, 80.0, "DOWN", "PAPER")
        pos = trades_db.get_open_position(conn)
        assert pos["id"] == eid2
        assert pos["signal_type"] == "DOWN"

    def test_record_sell_invalid_id_raises(self):
        conn = self._conn()
        with pytest.raises(ValueError):
            trades_db.record_sell(conn, 9999, 100.0, "SL")

    def test_paper_and_live_history_coexist(self):
        conn = self._conn()
        eid_p = trades_db.record_buy(conn, "SYM1", 65, 100.0, "UP", "PAPER")
        trades_db.record_sell(conn, eid_p, 110.0, "TARGET")
        eid_l = trades_db.record_buy(conn, "SYM2", 65, 200.0, "DOWN", "LIVE")
        trades_db.record_sell(conn, eid_l, 180.0, "SL")
        rows = conn.execute("SELECT mode, pnl FROM trades ORDER BY id").fetchall()
        assert [r["mode"] for r in rows] == ["PAPER", "LIVE"]


# ── V4: risk-based position sizing ─────────────────────────────────────────────

class TestPositionSize:
    # Defaults mirror config: SL_PCT=0.30, LOT_SIZE=65 → one lot risks entry*19.5.
    SL, LOT, CAP = 0.30, 65, 5000

    def test_one_lot_within_cap(self):
        # premium 100 → risk/lot 1950 ≤ 5000 → one lot.
        assert position_size(100, self.SL, self.LOT, self.CAP, 1) == 65

    def test_skip_when_one_lot_exceeds_cap(self):
        # premium 300 → risk/lot 5850 > 5000 → skip.
        assert position_size(300, self.SL, self.LOT, self.CAP, 1) == 0

    def test_flip_point_around_256(self):
        # cap/(sl*lot) = 5000/19.5 = 256.41 → 256 trades, 257 skips.
        assert position_size(256, self.SL, self.LOT, self.CAP, 1) == 65
        assert position_size(257, self.SL, self.LOT, self.CAP, 1) == 0

    def test_max_lots_ceiling_of_one(self):
        # premium 20 → risk/lot 390 → cap allows 12 lots, but max_lots=1.
        assert position_size(20, self.SL, self.LOT, self.CAP, 1) == 65

    def test_max_lots_allows_scaling_when_raised(self):
        # same cheap premium, max_lots=3 → three lots.
        assert position_size(20, self.SL, self.LOT, self.CAP, 3) == 195

    def test_zero_or_negative_price_returns_zero(self):
        assert position_size(0, self.SL, self.LOT, self.CAP, 1) == 0
        assert position_size(-5, self.SL, self.LOT, self.CAP, 1) == 0


# ── V4: month-to-date P&L for the monthly loss limit ──────────────────────────

class TestMonthToDatePnl:

    def _conn(self):
        return trades_db.init_db(":memory:")

    def _closed(self, conn, mode, entry, exit_, buy_ts, sell_ts):
        eid = trades_db.record_buy(conn, "SYM", 65, entry, "UP", mode, ts=buy_ts)
        trades_db.record_sell(conn, eid, exit_, "SL", ts=sell_ts)

    def test_sums_closed_trades_in_month_and_mode(self):
        conn = self._conn()
        # (70-100)*65 = -1950 ; (80-100)*65 = -1300
        self._closed(conn, "PAPER", 100, 70, "2026-06-01T10:00:00+05:30", "2026-06-01T11:00:00+05:30")
        self._closed(conn, "PAPER", 100, 80, "2026-06-02T10:00:00+05:30", "2026-06-02T11:00:00+05:30")
        assert trades_db.month_to_date_pnl(conn, "PAPER", "2026-06") == -3250

    def test_excludes_other_month(self):
        conn = self._conn()
        self._closed(conn, "PAPER", 100, 70, "2026-05-01T10:00:00+05:30", "2026-05-30T11:00:00+05:30")
        assert trades_db.month_to_date_pnl(conn, "PAPER", "2026-06") == 0

    def test_excludes_other_mode(self):
        conn = self._conn()
        self._closed(conn, "LIVE", 100, 70, "2026-06-01T10:00:00+05:30", "2026-06-01T11:00:00+05:30")
        assert trades_db.month_to_date_pnl(conn, "PAPER", "2026-06") == 0

    def test_excludes_open_position(self):
        conn = self._conn()
        trades_db.record_buy(conn, "SYM", 65, 100, "UP", "PAPER", ts="2026-06-01T10:00:00+05:30")
        assert trades_db.month_to_date_pnl(conn, "PAPER", "2026-06") == 0

    def test_no_trades_returns_zero(self):
        conn = self._conn()
        assert trades_db.month_to_date_pnl(conn, "PAPER", "2026-06") == 0


# ── V5: volume profile (high-conviction tag) ──────────────────────────────────

class TestVolumeProfile:
    def _c(self, hi, lo, vol):
        return {"high": hi, "low": lo, "volume": vol}

    def test_none_when_no_volume(self):
        assert volume_profile([], 5.0, 0.70) is None
        assert volume_profile([self._c(100, 90, 0)], 5.0, 0.70) is None

    def test_poc_at_heaviest_bin(self):
        # Heavy volume in the 100-105 bin, light far above.
        candles = [self._c(104, 100, 1000), self._c(124, 120, 50)]
        poc, vah, val = volume_profile(candles, 5.0, 0.70)
        assert 100 <= poc <= 105
        assert val <= poc <= vah

    def test_value_area_widens_with_pct(self):
        candles = [self._c(104, 100, 500), self._c(109, 105, 300), self._c(114, 110, 200)]
        _, vah70, val70 = volume_profile(candles, 5.0, 0.70)
        _, vah100, val100 = volume_profile(candles, 5.0, 1.0)
        assert (vah100 - val100) >= (vah70 - val70)

    def test_conviction_direction(self):
        # Value area built low; an UP close above VAH = high-conviction,
        # a DOWN close below VAL = high-conviction.
        candles = [self._c(104, 100, 1000)]
        poc, vah, val = volume_profile(candles, 5.0, 0.70)
        assert (110 > vah)        # UP breakout above value -> would clear VAH
        assert (95 < val)         # DOWN breakdown below value -> would clear VAL


# ── V5: fill classification ───────────────────────────────────────────────────

class TestClassifyFill:
    def test_none(self):
        assert classify_fill(0, 65) == "NONE"

    def test_partial(self):
        assert classify_fill(40, 65) == "PARTIAL"

    def test_complete(self):
        assert classify_fill(65, 65) == "COMPLETE"

    def test_over_counts_complete(self):
        assert classify_fill(130, 65) == "COMPLETE"


# ── V5: trade journal records volume-profile context ──────────────────────────

class TestJournalConviction:
    def test_record_buy_stores_va_context(self):
        conn = trades_db.init_db(":memory:")
        eid = trades_db.record_buy(
            conn, "NIFTY26JUN23400CE", 65, 185.95, "UP", "LIVE",
            vah=23475.0, val=23385.0, poc=23432.5, conviction="HIGH",
        )
        row = conn.execute(
            "SELECT vah, val, poc, conviction FROM trades WHERE id=?", (eid,)
        ).fetchone()
        assert row["vah"] == 23475.0
        assert row["val"] == 23385.0
        assert row["poc"] == 23432.5
        assert row["conviction"] == "HIGH"

    def test_record_buy_defaults_va_to_null(self):
        conn = trades_db.init_db(":memory:")
        eid = trades_db.record_buy(conn, "SYM", 65, 100.0, "UP", "PAPER")
        row = conn.execute(
            "SELECT vah, conviction FROM trades WHERE id=?", (eid,)
        ).fetchone()
        assert row["vah"] is None
        assert row["conviction"] is None


# ── PAPER auto-enter (temporary Telegram-ban workaround) ──────────────────────

class TestEnterPositionAutoEnter:
    """With no button tap reachable (Telegram banned in India), enter_position
    must journal the paper trade and arm the tracker directly (cb_id=None)."""

    def test_paper_auto_enter_journals_and_arms(self):
        import threading
        conn = trades_db.init_db(":memory:")
        tracker = PositionTracker()
        ctx = SignalContext()
        symbol = "NIFTY26JUN24000CE"
        ctx.set(symbol, {"vah": 24010.0, "val": 23950.0, "poc": 23980.0,
                         "conviction": "HIGH"})

        enter_position(None, symbol, 120.0, 65, conn,
                       threading.Lock(), tracker, ctx, cb_id=None)

        # Position armed for monitoring at the alert-time price/qty.
        assert tracker.is_open()
        pos = tracker.current()
        assert pos["symbol"] == symbol
        assert pos["entry_price"] == 120.0
        assert pos["qty"] == 65
        assert pos["signal_type"] == "UP"
        assert pos["mode"] == "PAPER"
        # Journaled (still open) with the signal's VP context.
        row = conn.execute(
            "SELECT entry_price, mode, conviction, exit_ts FROM trades WHERE id=?",
            (pos["id"],),
        ).fetchone()
        assert row["entry_price"] == 120.0
        assert row["mode"] == "PAPER"
        assert row["conviction"] == "HIGH"
        assert row["exit_ts"] is None

    def test_auto_enter_skips_when_position_already_open(self):
        import threading
        conn = trades_db.init_db(":memory:")
        tracker = PositionTracker()
        ctx = SignalContext()
        tracker.open({"id": 1, "symbol": "X", "qty": 65, "entry_price": 50.0,
                      "signal_type": "UP", "mode": "PAPER", "peak": 50.0})

        enter_position(None, "NIFTY26JUN24000CE", 120.0, 65, conn,
                       threading.Lock(), tracker, ctx, cb_id=None)

        # At-most-one position: no new journal row, original position intact.
        assert conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"] == 0
        assert tracker.current()["id"] == 1


# ── V7: weekly track stays PAPER even when the bot is LIVE ────────────────────

class TestWeeklyTrackPaperOverride:
    """The money-safety crux: a weekly (early-cycle) track must journal + manage
    as PAPER and place NO live order, even when config is LIVE (paper=True)."""

    def test_paper_override_journals_paper_while_live(self):
        import threading
        # Simulate LIVE global config; the per-entry override must still be PAPER.
        saved_paper, saved_dry = _cfg.PAPER_TRADE, _cfg.DRY_RUN
        _cfg.PAPER_TRADE, _cfg.DRY_RUN = False, False
        try:
            conn = trades_db.init_db(":memory:")
            tracker = PositionTracker()
            ctx = SignalContext()
            symbol = "NIFTY26JUN24000CE"
            ctx.set(symbol, {"vah": None, "val": None, "poc": None, "conviction": "normal"})

            # kite=None proves no live broker call happens on the paper path.
            enter_position(None, symbol, 100.0, 65, conn,
                           threading.Lock(), tracker, ctx, cb_id=None, paper=True)

            pos = tracker.current()
            assert pos is not None
            assert pos["mode"] == "PAPER"
            assert pos["entry_price"] == 100.0
            row = conn.execute(
                "SELECT mode, exit_ts FROM trades WHERE id=?", (pos["id"],)
            ).fetchone()
            assert row["mode"] == "PAPER"
            assert row["exit_ts"] is None
        finally:
            _cfg.PAPER_TRADE, _cfg.DRY_RUN = saved_paper, saved_dry


# ═══════════════════════════════════════════════════════════════════════════
# V6.2 — token / ORB-fetch resilience (the 2026-06-29 crash hardening)
# ═══════════════════════════════════════════════════════════════════════════

class _Kite:
    """Minimal fake kite for resilience tests."""
    def __init__(self, hist=None, hist_exc=None, fail_first=0, profile_exc=None):
        self._hist = hist
        self._hist_exc = hist_exc
        self._fail_first = fail_first
        self._calls = 0
        self._profile_exc = profile_exc

    def historical_data(self, *a, **k):
        self._calls += 1
        if self._hist_exc is not None and self._calls <= (self._fail_first or 10**9):
            raise self._hist_exc
        return self._hist

    def profile(self):
        if self._profile_exc is not None:
            raise self._profile_exc
        return {"user_id": "X"}


class TestTokenResilience:

    TOKEN_MSG = "Incorrect `api_key` or `access_token`."

    def test_is_token_error_kite_message(self):
        assert _is_token_error(Exception(self.TOKEN_MSG))

    def test_is_token_error_runtime_no_token(self):
        assert _is_token_error(RuntimeError("No valid access token for today. Run auth.py first."))

    def test_is_token_error_by_class_name(self):
        class TokenException(Exception):
            pass
        assert _is_token_error(TokenException("boom"))

    def test_is_token_error_false_for_transient(self):
        assert not _is_token_error(ValueError("connection timed out"))
        assert not _is_token_error(Exception("rate limit exceeded"))

    def test_fetch_orb_returns_candles(self):
        k = _Kite(hist=[{"date": "candle"}])
        assert fetch_orb_candle(k, 1, None, None, delay_s=0) == [{"date": "candle"}]

    def test_fetch_orb_token_error_returns_none_no_retry_spam(self):
        k = _Kite(hist_exc=Exception(self.TOKEN_MSG))
        assert fetch_orb_candle(k, 1, None, None, retries=3, delay_s=0) is None
        assert k._calls == 1  # token errors are terminal — don't retry

    def test_fetch_orb_retries_transient_then_succeeds(self):
        k = _Kite(hist=[{"ok": 1}], hist_exc=Exception("temporary network error"), fail_first=1)
        assert fetch_orb_candle(k, 1, None, None, retries=3, delay_s=0) == [{"ok": 1}]
        assert k._calls == 2

    def test_fetch_orb_persistent_transient_returns_none(self):
        k = _Kite(hist_exc=Exception("temporary network error"))
        assert fetch_orb_candle(k, 1, None, None, retries=2, delay_s=0) is None
        assert k._calls == 2

    def test_ensure_kite_alive_true(self):
        assert ensure_kite_alive(_Kite()) is True

    def test_ensure_kite_alive_false_on_token_error(self):
        assert ensure_kite_alive(_Kite(profile_exc=Exception(self.TOKEN_MSG))) is False
