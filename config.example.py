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
