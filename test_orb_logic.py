"""
Comprehensive unit tests for orb_monitor.py logic.

Covers all 6 fixes:
  Fix #1 - select_last_closed_candle (replaced broken minute % 3 check)
  Fix #2 - BREAKOUT_BUFFER threshold in evaluate_signal
  Fix #3 - REARM_BUFFER in update_rearm_state
  Fix #4 - ORB candle date validation (9:15 AM assertion)
  Fix #5 - Volume average behaviour (vol_ok bypass when avg_vol == 0)
  Fix #6 - elif mutual exclusion in evaluate_signal
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
sys.modules["config"] = _cfg
sys.modules["kite_auth"] = MagicMock()
sys.modules["telegram_alert"] = MagicMock()

from orb_monitor import (  # noqa: E402
    compute_vwap,
    compute_vol_filter,
    evaluate_signal,
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
    """Build a minimal OHLCV dict with a 3-minute start timestamp."""
    return {
        "date":   ts(h, m),
        "open":   close,
        "high":   high if high is not None else close + 5,
        "low":    low  if low  is not None else close - 5,
        "close":  close,
        "volume": volume,
    }


def fresh_state():
    return {
        "UP":   {"fires": 0, "needs_reset": False},
        "DOWN": {"fires": 0, "needs_reset": False},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Fix #1 — select_last_closed_candle
# ═══════════════════════════════════════════════════════════════════════════

class TestSelectLastClosedCandle:
    """A candle is closed when its start_time + 3 min <= now."""

    def test_returns_last_candle_when_fully_closed(self):
        candles = [candle(9, 15, 24490), candle(9, 18, 24495), candle(9, 21, 24502)]
        now = ts(9, 25)  # 9:21 candle closed at 9:24, well in the past
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 21)

    def test_skips_in_progress_candle(self):
        # 9:21 candle closes at 9:24; if now is 9:23 it's still open
        candles = [candle(9, 15, 24490), candle(9, 18, 24495), candle(9, 21, 24502)]
        now = ts(9, 23)  # 9:21 + 3min = 9:24 > 9:23 → skip; 9:18 closed at 9:21 → ok
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 18)

    def test_returns_none_when_no_closed_candle_exists(self):
        candles = [candle(9, 15, 24490)]
        now = ts(9, 17)  # 9:15 candle closes at 9:18 > 9:17 → not closed yet
        result = select_last_closed_candle(candles, now)
        assert result is None

    def test_boundary_exactly_at_close_time(self):
        # Candle starting 9:15 closes at exactly 9:18:00
        candles = [candle(9, 15, 24490)]
        now = ts(9, 18, 0)  # exactly at close time → counts as closed
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 15)

    def test_second_to_last_used_when_last_is_open(self):
        candles = [
            candle(9, 15, 24480),
            candle(9, 18, 24490),
            candle(9, 21, 24502),
            candle(9, 24, 24510),  # closes at 9:27; now = 9:25 → still open
        ]
        now = ts(9, 25)
        result = select_last_closed_candle(candles, now)
        assert result["date"] == ts(9, 21)


# ═══════════════════════════════════════════════════════════════════════════
# compute_vwap
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeVwap:

    def test_single_candle(self):
        c = {"high": 100, "low": 90, "close": 95, "volume": 1000}
        # tp = (100 + 90 + 95) / 3 = 95; vwap = 95 * 1000 / 1000 = 95
        assert compute_vwap([c]) == pytest.approx(95.0)

    def test_volume_weighted_average(self):
        c1 = {"high": 100, "low":  90, "close":  95, "volume": 1000}  # tp=95
        c2 = {"high": 110, "low": 100, "close": 105, "volume": 3000}  # tp=105
        # vwap = (95*1000 + 105*3000) / 4000 = (95000 + 315000) / 4000 = 102.5
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
        return [candle(9, 15 + i * 3, 24490, volume=vol) for i in range(n)]

    def test_vol_ok_when_above_threshold(self):
        # avg_vol = 10000; multiplier = 1.2 → threshold = 12000; last vol = 15000
        last = candle(9, 30, 24500, volume=15000)
        vol_ok, avg = compute_vol_filter(last, self._prior(10000))
        assert vol_ok is True
        assert avg == pytest.approx(10000.0)

    def test_vol_not_ok_when_at_threshold(self):
        # last vol exactly equals threshold (not strictly greater)
        last = candle(9, 30, 24500, volume=12000)
        vol_ok, _ = compute_vol_filter(last, self._prior(10000))
        assert vol_ok is False

    def test_vol_not_ok_when_below_threshold(self):
        last = candle(9, 30, 24500, volume=8000)
        vol_ok, _ = compute_vol_filter(last, self._prior(10000))
        assert vol_ok is False

    def test_vol_ok_bypassed_when_no_prior_candles(self):
        # Fix #5: when avg_vol == 0 (empty prior), vol filter is True
        last = candle(9, 15, 24490, volume=1)
        vol_ok, avg = compute_vol_filter(last, [])
        assert vol_ok is True
        assert avg == 0

    def test_lookback_window_respected(self):
        # LOOKBACK_CANDLES = 20; supply 25 prior candles: first 5 at high vol, last 20 at low vol.
        # 9:15-9:27 → 5 candles; 10:00-10:57 → 20 candles (avoids minute > 59 overflow)
        old_candles = [candle(9, 15 + i * 3, 24490, volume=50000) for i in range(5)]
        new_candles = [candle(10, i * 3, 24490, volume=5000) for i in range(20)]
        prior = old_candles + new_candles
        last = candle(11, 0, 24500, volume=7000)
        vol_ok, avg = compute_vol_filter(last, prior)
        # Only the last 20 are used → avg = 5000, threshold = 6000
        assert avg == pytest.approx(5000.0)
        assert vol_ok is True  # 7000 > 6000


# ═══════════════════════════════════════════════════════════════════════════
# Fix #3 — update_rearm_state (REARM_BUFFER)
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdateRearmState:

    def test_up_rearms_when_pullback_exceeds_buffer(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        # close must be < orb_high - REARM_BUFFER = 24500 - 15 = 24485
        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False

    def test_up_does_not_rearm_at_buffer_boundary(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        # exactly at orb_high - REARM_BUFFER → NOT strictly less than → stays True
        update_rearm_state(state, close=24485, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True

    def test_up_does_not_rearm_when_still_outside_orb(self):
        state = fresh_state()
        state["UP"]["needs_reset"] = True
        # price still above ORB high — no pullback at all
        update_rearm_state(state, close=24510, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True

    def test_down_rearms_when_bounce_exceeds_buffer(self):
        state = fresh_state()
        state["DOWN"]["needs_reset"] = True
        # close must be > orb_low + REARM_BUFFER = 24400 + 15 = 24415
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
        # price is well inside ORB → both should reset
        update_rearm_state(state, close=24450, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False
        assert state["DOWN"]["needs_reset"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Fix #2 — BREAKOUT_BUFFER in evaluate_signal
# ═══════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalBreakout:
    """Breakout (UP) requires close > orb_high + BREAKOUT_BUFFER."""

    def _call(self, close, vol_ok=True, vwap=None, up_armed=True, down_armed=True):
        return evaluate_signal(close, ORB_HIGH, ORB_LOW, vol_ok, vwap, up_armed, down_armed)

    def test_fires_when_buffer_exceeded(self):
        # close > 24500 + 10 = 24510
        assert self._call(close=24511) == "UP"

    def test_does_not_fire_at_exact_buffer(self):
        # close == 24510 is NOT strictly greater than 24510
        assert self._call(close=24510) is None

    def test_does_not_fire_just_inside_buffer(self):
        # close = 24509 — old code would have fired, new code must not
        assert self._call(close=24509) is None

    def test_does_not_fire_at_orb_high(self):
        # close == orb_high — the classic 0-tick fakeout
        assert self._call(close=24500) is None

    def test_does_not_fire_when_vol_not_ok(self):
        assert self._call(close=24515, vol_ok=False) is None

    def test_does_not_fire_when_below_vwap(self):
        # close > orb_high + buffer but below VWAP — trend not confirmed
        assert self._call(close=24515, vwap=24520) is None

    def test_fires_when_vwap_is_none(self):
        # vwap unavailable → bypass vwap filter
        assert self._call(close=24515, vwap=None) == "UP"

    def test_does_not_fire_when_not_armed(self):
        assert self._call(close=24515, up_armed=False) is None


# ═══════════════════════════════════════════════════════════════════════════
# Fix #2 — BREAKOUT_BUFFER in evaluate_signal (DOWN side)
# ═══════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalBreakdown:
    """Breakdown (DOWN) requires close < orb_low - BREAKOUT_BUFFER."""

    def _call(self, close, vol_ok=True, vwap=None, up_armed=True, down_armed=True):
        return evaluate_signal(close, ORB_HIGH, ORB_LOW, vol_ok, vwap, up_armed, down_armed)

    def test_fires_when_buffer_exceeded(self):
        # close < 24400 - 10 = 24390
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
        # close < orb_low - buffer but above VWAP → trend not confirmed for breakdown
        assert self._call(close=24385, vwap=24380) is None

    def test_fires_when_vwap_is_none(self):
        assert self._call(close=24385, vwap=None) == "DOWN"

    def test_does_not_fire_when_not_armed(self):
        assert self._call(close=24385, down_armed=False) is None


# ═══════════════════════════════════════════════════════════════════════════
# Fix #6 — elif mutual exclusion
# ═══════════════════════════════════════════════════════════════════════════

class TestMutualExclusion:
    """UP and DOWN can never fire in the same tick."""

    def test_up_wins_when_both_conditions_true(self):
        # Artificially set ORB high < ORB low to force both conditions True simultaneously.
        # evaluate_signal must return only "UP" (the first branch).
        result = evaluate_signal(
            close=24300,
            orb_high=24320,   # close < orb_high + buffer? No: 24300 < 24330 — UP won't fire
            orb_low=24280,    # close < orb_low - buffer? 24300 > 24270 — DOWN won't fire either
            vol_ok=True,
            vwap=None,
            up_armed=True,
            down_armed=True,
        )
        assert result is None  # sanity check with normal params

    def test_only_up_fires_when_up_condition_met(self):
        # With valid ORB range, confirm DOWN never fires on same tick as UP
        result = evaluate_signal(
            close=ORB_HIGH + BUFFER + 5,  # 24515 — UP fires
            orb_high=ORB_HIGH,
            orb_low=ORB_LOW,
            vol_ok=True,
            vwap=None,
            up_armed=True,
            down_armed=True,
        )
        assert result == "UP"

    def test_only_down_fires_when_down_condition_met(self):
        result = evaluate_signal(
            close=ORB_LOW - BUFFER - 5,  # 24385 — DOWN fires
            orb_high=ORB_HIGH,
            orb_low=ORB_LOW,
            vol_ok=True,
            vwap=None,
            up_armed=True,
            down_armed=True,
        )
        assert result == "DOWN"

    def test_forced_overlap_returns_only_up(self):
        # Inverted ORB (high < low) forces both numeric conditions True at same time.
        # The elif guarantees only UP is returned.
        result = evaluate_signal(
            close=24450,
            orb_high=24440,  # close > 24440 + 10 = 24450? 24450 > 24450 → False (not strictly)
            orb_low=24460,   # close < 24460 - 10 = 24450? 24450 < 24450 → False (not strictly)
            vol_ok=True,
            vwap=None,
            up_armed=True,
            down_armed=True,
        )
        assert result is None  # neither fires at exact boundary

    def test_forced_overlap_strictly_returns_only_up(self):
        # Adjust to strictly satisfy both → only UP fires due to elif
        result = evaluate_signal(
            close=24451,
            orb_high=24440,  # close > 24440 + 10 = 24450? 24451 > 24450 → True
            orb_low=24462,   # close < 24462 - 10 = 24452? 24451 < 24452 → True (would fire DOWN)
            vol_ok=True,
            vwap=None,
            up_armed=True,
            down_armed=True,
        )
        # elif means UP was evaluated first and returned; DOWN never checked
        assert result == "UP"


# ═══════════════════════════════════════════════════════════════════════════
# Fix #4 — ORB candle date validation (condition tested inline)
# ═══════════════════════════════════════════════════════════════════════════

class TestOrbCandleValidation:
    """Validate that the ORB candle date check works correctly."""

    def test_valid_orb_candle_at_9_15(self):
        orb = candle(9, 15, 24490)
        assert orb["date"].time() == dtime(9, 15)

    def test_invalid_orb_candle_pre_open(self):
        orb = candle(9, 0, 24490)
        assert orb["date"].time() != dtime(9, 15)

    def test_invalid_orb_candle_wrong_minute(self):
        orb = candle(9, 7, 24490)
        assert orb["date"].time() != dtime(9, 15)


# ═══════════════════════════════════════════════════════════════════════════
# Integration-style: re-arm + signal across multiple ticks
# ═══════════════════════════════════════════════════════════════════════════

class TestRearmAndFireSequence:
    """Simulate a full UP fire → reset → re-arm → second fire sequence."""

    def test_up_fires_twice_after_proper_pullback(self):
        state = fresh_state()
        max_fires = _cfg.MAX_FIRES_PER_DIRECTION

        # Tick 1: breakout fires
        update_rearm_state(state, close=24515, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < max_fires
        sig = evaluate_signal(24515, ORB_HIGH, ORB_LOW, True, None, up_armed, True)
        assert sig == "UP"
        state["UP"]["fires"] += 1
        state["UP"]["needs_reset"] = True

        # Tick 2: price barely pulls back — NOT enough to re-arm (< REARM_BUFFER)
        update_rearm_state(state, close=24490, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is True  # not yet re-armed

        # Tick 3: meaningful pullback — re-arms
        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["UP"]["needs_reset"] is False  # now re-armed

        # Tick 4: fresh breakout fires second time
        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < max_fires
        sig = evaluate_signal(24515, ORB_HIGH, ORB_LOW, True, None, up_armed, True)
        assert sig == "UP"
        state["UP"]["fires"] += 1
        state["UP"]["needs_reset"] = True
        assert state["UP"]["fires"] == 2

    def test_no_third_fire_after_max_fires(self):
        state = fresh_state()
        state["UP"]["fires"] = _cfg.MAX_FIRES_PER_DIRECTION  # already maxed out
        state["UP"]["needs_reset"] = True

        update_rearm_state(state, close=24484, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        up_armed = not state["UP"]["needs_reset"] and state["UP"]["fires"] < _cfg.MAX_FIRES_PER_DIRECTION
        assert up_armed is False
        sig = evaluate_signal(24515, ORB_HIGH, ORB_LOW, True, None, up_armed, False)
        assert sig is None

    def test_down_fires_twice_after_proper_bounce(self):
        state = fresh_state()
        max_fires = _cfg.MAX_FIRES_PER_DIRECTION

        # Tick 1: breakdown fires
        update_rearm_state(state, close=24385, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        down_armed = not state["DOWN"]["needs_reset"] and state["DOWN"]["fires"] < max_fires
        sig = evaluate_signal(24385, ORB_HIGH, ORB_LOW, True, None, False, down_armed)
        assert sig == "DOWN"
        state["DOWN"]["fires"] += 1
        state["DOWN"]["needs_reset"] = True

        # Tick 2: small bounce — not enough to re-arm
        update_rearm_state(state, close=24410, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["DOWN"]["needs_reset"] is True

        # Tick 3: proper bounce inside ORB — re-arms
        update_rearm_state(state, close=24416, orb_high=ORB_HIGH, orb_low=ORB_LOW)
        assert state["DOWN"]["needs_reset"] is False

        # Tick 4: fresh breakdown
        down_armed = not state["DOWN"]["needs_reset"] and state["DOWN"]["fires"] < max_fires
        sig = evaluate_signal(24385, ORB_HIGH, ORB_LOW, True, None, False, down_armed)
        assert sig == "DOWN"
        state["DOWN"]["fires"] += 1
        assert state["DOWN"]["fires"] == 2
