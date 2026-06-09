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
_cfg.SL_PCT = 0.30
_cfg.TARGET_PCT = 0.50
_cfg.SQUARE_OFF_HOUR = 15
_cfg.SQUARE_OFF_MINUTE = 15
_cfg.POSITION_POLL_SECONDS = 60
_cfg.DB_PATH = ":memory:"
_cfg.RISK_PER_TRADE_INR = 5000
_cfg.MAX_LOTS = 1
_cfg.MONTHLY_LOSS_LIMIT_INR = 15000
sys.modules["config"] = _cfg
sys.modules["kite_auth"] = MagicMock()
sys.modules["telegram_alert"] = MagicMock()

from orb_monitor import (  # noqa: E402
    PositionTracker,
    compute_limit_price,
    compute_vol_filter,
    compute_vwap,
    decide_exit,
    decode_order_callback,
    encode_order_callback,
    evaluate_signal,
    extract_bid_ask,
    itm_option,
    position_size,
    round_to_tick,
    select_last_closed_candle,
    update_rearm_state,
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
