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
