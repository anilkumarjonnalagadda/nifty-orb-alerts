# Nifty ORB Alert System — Handbook

Your daily operating manual for the breakout alert bot running on AWS Lightsail.

---

## What this system does

Every trading day, this bot watches the **Nifty current-month futures contract** on Zerodha and tells you (via Telegram) when price makes a clean breakout from the **first 15-minute range** of the day.

- **ORB window:** 9:15–9:30 IST. The high and low of that single 15-min candle become your levels.
- **Watch period:** 9:30 AM – 3:30 PM IST. Every 3 minutes, the latest candle close is checked.
- **Trigger:** A 3-min close above ORB high → buy ATM weekly **CE**. A close below ORB low → buy ATM weekly **PE**.
- **Filters (to cut noise):**
  - Volume on the breakout candle must be > **1.2×** the average of the last 20 three-minute candles.
  - Close must be on the right side of intraday VWAP (above for UP, below for DOWN).
- **Re-fires:** Up to **2 alerts per direction per day**. After fire #1, the system waits for price to come back inside the ORB range (a "reset") before re-arming. Catches clean re-entries without spam.
- **You execute the trades manually.** The bot only alerts. It will never place an order in V1.

---

## Daily morning ritual

Every trading day, you'll get **two Telegram messages from the system before market open**:

### 8:30 AM IST — Token refresh nudge

A message like:

```
Good morning. Wed, 30 Apr 2026

Refresh Kite access token before 9:15 IST.

1. Tap to log in:
https://kite.zerodha.com/connect/login?api_key=XXX&v=3

2. SSH to Lightsail and run:
   cd ~/nifty-orb-alerts && source venv/bin/activate
   python auth.py
   nohup python orb_monitor.py > orb.log 2>&1 &
```

### Steps to follow (takes ~60 seconds)

1. **Tap the Kite login link** in the Telegram message → log in with your Zerodha credentials + 2FA.
2. After login, you land on a page (might look broken — that's fine). **Copy the full URL** from the browser address bar — it contains `request_token=...`.
3. **SSH to Lightsail** from your laptop or AWS browser-based SSH:
   ```bash
   ssh ubuntu@<lightsail-ip>
   cd ~/nifty-orb-alerts
   source venv/bin/activate
   python auth.py
   ```
4. **Paste the URL** when prompted. You'll see `Access token saved for today.`
5. **Start the monitor:**
   ```bash
   nohup python orb_monitor.py > orb.log 2>&1 &
   ```
6. Within a minute, you should get a Telegram alert: `ORB monitor started`. If yes — you're done. Walk away.

### If you don't get the "ORB monitor started" message

Something failed. SSH back in and check:
```bash
tail -50 orb.log
```
Most common causes are listed in **Troubleshooting** below.

---

## What the alerts mean

You'll get one of these messages over the day. Treat each as a **signal to evaluate**, not a blind buy.

### "ORB monitor started"
Confirmation the bot is alive and watching. No action.

### "ORB formed" (around 9:30:30 AM)
```
ORB formed for NIFTY24MAYFUT
High: 22687.40
Low: 22641.25
Watching 3-min closes for breakout/breakdown...
```
Now you know the day's range. **Note these levels mentally.** Wider ranges (>80 points) often mean choppier days.

### "BREAKOUT (UP) — fire 1/2" (the real signal)
```
BREAKOUT (UP) — fire 1/2
NIFTY24MAYFUT 3m close: 22693.10 > ORB High 22687.40
Vol: 142500 (avg 89200, x1.60)
VWAP: 22669.85
Spot Nifty: 22691.55
BUY CE: NIFTY2450822700CE
```
- **fire 1/2** = first breakout of the day in this direction
- **x1.60** = breakout candle had 1.6× the average volume — strong
- **VWAP** = if price is well above VWAP, trend is supported
- **BUY CE: ...** = the ATM weekly call option to buy

### "BREAKDOWN (DOWN) — fire 1/2"
Same shape, but opposite — buy the suggested PE.

### "fire 2/2"
A re-entry breakout after price came back inside ORB range and broke out again. Often **cleaner than fire 1** because the noise has been shaken out. Take it seriously.

### "Market closed. ORB monitor stopping."
End of day at 3:30 PM. Shows total fires. No action.

---

## What to do when an alert fires (your trading checklist)

The bot tells you *what looks like a setup*. The decision to trade is yours. Run this 30-second checklist:

1. **Check Nifty chart on Kite** — is price genuinely outside the ORB on multiple timeframes, or is this one suspicious candle?
2. **Check the option's bid-ask spread** — if it's wider than 2-3 rupees on a near-ATM strike, liquidity is poor; skip or use a limit order.
3. **Define your stop loss before entry.** Standard rule: stop on the option = 30-40% of premium paid. On the futures level = re-entry into ORB range.
4. **Position size.** Risk per trade ≤ 1-2% of trading capital. Calculate quantity = (risk amount) / (premium × stop %).
5. **Place the trade.** Limit order at LTP or slightly worse. Avoid market orders on options.
6. **Set a target.** Common: 1.5× to 2× the risk (i.e., target premium = entry × 1.5).
7. **Don't average down.** If it goes against you to your stop, take the loss. Move on.

---

## Tuning the filters

All filter thresholds live in `config.py` on the Lightsail box. Edit, save, restart the monitor.

| Setting | Default | What it does | When to change |
|---|---|---|---|
| `VOLUME_MULTIPLIER` | `1.2` | Breakout vol must be ≥ this × the last-20-candle avg | Increase to `1.5` if you're getting weak signals; decrease to `1.0` if you're missing valid moves |
| `LOOKBACK_CANDLES` | `20` | Number of recent 3-min candles for the volume average | Rarely needs changing |
| `MAX_FIRES_PER_DIRECTION` | `2` | Max alerts per direction per day | Set to `1` for ultra-conservative; `3` if you want every clean re-break |

After editing `config.py`, you must restart the monitor:
```bash
pkill -f orb_monitor.py
nohup python orb_monitor.py > orb.log 2>&1 &
```

---

## Troubleshooting

### "ORB monitor started" never arrives
```bash
tail -100 orb.log
```
Look for the error. Common ones:

| Error contains | Fix |
|---|---|
| `No valid access token for today` | You forgot to run `python auth.py`. Run it. |
| `TokenException` | Token expired or wrong API key/secret. Re-run `auth.py`. |
| `connection`, `timeout`, `network` | Lightsail momentarily lost network. Restart: `nohup python orb_monitor.py > orb.log 2>&1 &` |
| `No active Nifty futures found` | Holiday or expiry edge case. Check NSE calendar. |

### Telegram alerts not arriving
```bash
python -c "from telegram_alert import send_alert; send_alert('test')"
```
- Nothing on phone → bot token wrong, or you blocked the bot, or chat ID wrong.
- Get `Telegram send failed: ...` → the error tells you what's wrong.

### Monitor seems hung / no breakout alerts even though Nifty clearly broke ORB
- Most likely the **filters rejected it** — check `orb.log` for that candle's volume + VWAP at the time.
- This is by design. If it happens often, lower `VOLUME_MULTIPLIER` to `1.0` or `1.1`.

### Process is still running from yesterday
```bash
ps aux | grep orb_monitor
pkill -f orb_monitor.py
```
Always kill before re-running. Otherwise you'll have two copies firing duplicate alerts.

---

## Things you should NOT do

- **Don't run `python auth.py` after market open.** It works, but you'll have missed the ORB and the script may already be exiting.
- **Don't push `config.py` to GitHub.** It's in `.gitignore` for a reason. Your secrets stay on Lightsail.
- **Don't paste your Kite API secret, access token, or TOTP into chats, screenshots, or anywhere outside `config.py`.** If you do — rotate immediately at https://developers.kite.trade/apps.
- **Don't trade options without a stop loss.** Options decay; "let me just hold and see" is how accounts die.
- **Don't let one losing day double your size the next day** to "recover."

---

## Risk discipline (read once a week)

- Options are leveraged. A 50% drop in the underlying option premium is **normal** and frequent.
- The bot's win rate will not be 100%. Expect 40-55% winners. Profitability comes from average winner > average loser.
- **Daily loss cap:** stop trading for the day after 2 consecutive losses or after losing 3% of capital, whichever comes first.
- **Weekly review:** Sunday evening, review all alerts that fired vs. trades you took vs. P&L. Look for patterns (e.g., wide ORBs always lose, tight ORBs always win, etc.).

---

## Roadmap

| Version | Status | What it adds |
|---|---|---|
| **V1** | Live | Telegram alerts only (this) |
| **V1.5** | Done | Re-arm logic — up to 2 fires per direction |
| **V2** | Planned | Auto-place CE/PE order with predefined SL & target. Manual confirmation toggle. |
| **V3** | Maybe | Trailing SL, multi-symbol (BankNifty, FinNifty), holiday calendar, web dashboard |

---

## Reference

- **Repo:** https://github.com/anilkumarjonnalagadda/nifty-orb-alerts (private)
- **Kite developer console:** https://developers.kite.trade/apps
- **NSE holiday calendar:** https://www.nseindia.com/resources/exchange-communication-holidays
- **Bot logs on Lightsail:** `~/nifty-orb-alerts/orb.log`
- **Cron logs (token reminder):** `~/nifty-orb-alerts/cron.log`
