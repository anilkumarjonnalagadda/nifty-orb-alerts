KITE_API_KEY = "your_api_key"
KITE_API_SECRET = "your_api_secret"

TELEGRAM_BOT_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID = "your_chat_id"

VOLUME_MULTIPLIER = 1.2
LOOKBACK_CANDLES = 20
MAX_FIRES_PER_DIRECTION = 2

# Points above ORB High (or below ORB Low) required for a confirmed breakout.
# Filters out fakeouts — tiny pokes through the level that immediately reverse.
BREAKOUT_BUFFER = 10

# Points inside the ORB range price must close before the system re-arms for
# a second fire. Prevents fire-1 and fire-2 triggering on the same move.
REARM_BUFFER = 15

# ── Order placement (V2) ─────────────────────────────────────────────────────

# Nifty F&O lot size. NSE last revised this to 65 in late 2025/early 2026.
# Verify on Kite if you see "lot size mismatch" errors when an order is placed.
LOT_SIZE = 65

# Marketable LIMIT buffer for option BUY orders, in rupees.
# Order is placed at best_ask + LIMIT_BUFFER (rounded to 0.05 tick).
# Small enough to not overpay, large enough to absorb micro-tick drift.
LIMIT_BUFFER = 0.50

# Skip the auto-order (and alert "place manually") when the bid-ask spread
# is wider than this fraction of the mid price. Wide spread = poor liquidity.
MAX_SPREAD_PCT = 0.05

# Skip the auto-order if the option's best ask exceeds this premium (₹).
# Catches strange data, deep-ITM accidents, etc.
MAX_PREMIUM = 500

# Order product. MIS = intraday (auto-squareoff before market close).
# Use "NRML" only if you intend to carry positions overnight.
ORDER_PRODUCT = "MIS"

# When True, button taps log "would place" instead of calling kite.place_order().
# Run for one full trading day to verify symbol/qty/price are sane,
# then flip to False on the VM to go live.
DRY_RUN = True

# ── Paper trading + exit management (V3) ─────────────────────────────────────

# When True, button taps simulate fills at the live best-ask (no Kite order).
# The bot then monitors the option's bid every minute and "sells" at best-bid
# when SL / TARGET / square-off time hits, recording everything in trades.db.
# Use this to validate signal + exit logic against real ticks without risking
# capital.
#
# PAPER_TRADE wins over DRY_RUN — the safety check inside place_buy_limit /
# place_sell_limit returns BEFORE the kite.place_order call whenever
# PAPER_TRADE is True, regardless of DRY_RUN. Mode truth table:
#
#   PAPER_TRADE  DRY_RUN  Mode      Kite order?  DB?  SL/target?
#   True         True     PAPER     No           Yes  Yes
#   True         False    PAPER     No           Yes  Yes
#   False        True     DRY_RUN   No           No   No   (alert-only)
#   False        False    LIVE      Yes          Yes  Yes
PAPER_TRADE = True

# TEMPORARY (Telegram banned in India through 2026-06-22 night): when True AND
# PAPER_TRADE is True, a fired signal auto-journals the paper trade at the
# alert-time LIMIT price WITHOUT waiting for the BUY-button tap. Needed because
# the phone can't reach Telegram to tap during the ban. Set back to False once
# Telegram is unblocked to restore the normal tap-to-enter workflow. Has NO
# effect when PAPER_TRADE is False (LIVE/DRY always require the tap).
PAPER_AUTO_ENTER = False

# Stop-loss as a fraction of the entry premium. 0.30 = exit if option mid drops
# 30% below entry (e.g. enter at ₹100, exit at ₹70).
SL_PCT = 0.30

# Target (profit-take) as a fraction of the entry premium. 0.50 = exit at +50%.
# IGNORED when USE_TRAILING_STOP is True.
TARGET_PCT = 0.50

# Exit style (V6 — "let winners run", validated on 7yr backtest).
#   True  = trailing stop, NO fixed target: the stop starts at -SL_PCT and
#           trails up to stay SL_PCT*entry below the running peak, locking in
#           gains as the option rises (never moves down). Captures big trend
#           days the fixed +50% target used to cut off. Backtest: lifted CAGR
#           ~8.5%->~10-12% and cut max drawdown ~37%->~28%.
#   False = original fixed SL_PCT stop / TARGET_PCT target behaviour.
# The trailing give-back distance equals SL_PCT (e.g. 30% of entry below peak).
USE_TRAILING_STOP = True

# Hard square-off time. Any open position is exited at this IST time even if
# SL/TARGET have not hit. Set well before market close so MIS auto-squareoff
# does not race us. Default 15:15 IST (15 min before close).
SQUARE_OFF_HOUR = 15
SQUARE_OFF_MINUTE = 15

# How often (seconds) to poll the option's quote when a position is open.
# 60 = check exit conditions once per minute. Lower = tighter SL but more
# Kite API calls. Note: the main signal-evaluation loop still runs at 5-min
# boundaries; this is a separate cadence that only kicks in when holding.
POSITION_POLL_SECONDS = 60

# SQLite trade journal. Lives next to orb_monitor.py on the VM. Survives
# process restart so today's open position can be recovered if you bounce
# the bot mid-day.
DB_PATH = "trades.db"

# ── Risk sizing + monthly loss limit (V4) ────────────────────────────────────

# Max rupees to risk on a single trade. Position size is chosen so the
# worst-case loss if the stop hits (entry * SL_PCT * qty) stays within this.
# NSE trades whole lots, so the smallest position is one LOT_SIZE lot. With
# LOT_SIZE=65 and SL_PCT=0.30 one lot risks entry*19.5, so a signal whose
# option premium exceeds RISK_PER_TRADE_INR / (SL_PCT * LOT_SIZE) is skipped
# entirely (no BUY button) because even one lot would breach the cap. At 5000
# the skip threshold is a premium of ~₹256.
RISK_PER_TRADE_INR = 5000

# Hard ceiling on lots per trade, on top of the risk cap. 1 = never size beyond
# a single lot (pure downside protection — the cap can only skip a trade, never
# scale it up). Raise only when you deliberately want multi-lot scaling.
MAX_LOTS = 1

# Month-to-date realized-loss limit (rupees). Once closed-trade P&L for the
# current calendar month in the active mode (PAPER or LIVE) reaches
# -MONTHLY_LOSS_LIMIT_INR, the bot stops offering NEW entries for the rest of
# the month. Open positions are still managed (SL/target/square-off). Resets
# automatically at the month boundary.
MONTHLY_LOSS_LIMIT_INR = 15000

# ── DTE-based expiry routing (V7 — weekly-track / monthly-act) ────────────────
# The bot picks the option expiry based on how many days are left until the
# MONTHLY expiry (the current Nifty monthly futures expiry):
#   monthly DTE <= DTE_MONTHLY_MAX  → recommend the MONTHLY ITM-1 (the validated
#                                     DTE-monthly<=15 strategy; ACTIONABLE).
#   monthly DTE >  DTE_MONTHLY_MAX  → recommend the WEEKLY ITM-1 (early cycle).
# Backtest support: the DTE-monthly<=15 CE variant led the OOS (2024-26) option
# family; weekly is kept early-cycle only to TRACK performance.
DTE_MONTHLY_MAX = 15

# When True, weekly (early-cycle) signals are journaled as PAPER for performance
# tracking and get NO live BUY button — you act ONLY on the monthly (late-cycle)
# recommendations. This holds even in LIVE mode: weekly stays paper-tracked,
# monthly goes live. Set False to make weekly actionable too (not recommended).
WEEKLY_TRACK_ONLY = True

# ── Fill confirmation (V5 — for LIVE) ────────────────────────────────────────
# After placing a LIVE order, poll its status for up to this many seconds to
# confirm it actually FILLED — placing an order is not the same as it filling.
# Prevents the bot from acting on a phantom position when a marketable LIMIT
# doesn't fill because price jumped past it before the order reached the book.
# Only used in LIVE mode (PAPER simulates a fill at the limit).
FILL_CONFIRM_TIMEOUT_SECONDS = 8
FILL_POLL_SECONDS = 1

# ── Volume-profile high-conviction tag (V5) ──────────────────────────────────
# Developing volume profile, built from the day's 1-min futures candles up to
# the signal, is used ONLY to LABEL a signal high-conviction — it never blocks
# a trade. UP is high-conviction when the breakout close clears the value-area
# high (VAH); DOWN when it clears the value-area low (VAL). VAH/VAL/POC and the
# conviction are journaled for later analysis.
VP_BIN_WIDTH = 5.0          # price-bin size (points) for the histogram
VP_VALUE_AREA_PCT = 0.70    # standard 70% value area
VP_WARMUP_MINUTES = 60      # skip the VA tag until this many 1-min candles exist
