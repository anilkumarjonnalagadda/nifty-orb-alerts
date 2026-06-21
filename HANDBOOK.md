# Nifty ORB Alert System — Handbook (V7)

Your daily operating manual for the breakout alert + 1-tap order bot running on AWS Lightsail.

---

## What this system does

Every trading day the bot watches the **Nifty current-month futures** on Zerodha and alerts you on Telegram when price breaks cleanly out of the **first 15-minute range** (the "ORB"). A qualifying breakout arrives with a **one-tap BUY button** for an ITM-1 option. You tap to enter; **the bot exits on its own** (a −30% stop that *trails up* to lock gains as the trade rises — "let winners run" — or the end-of-day square-off). It only trades real money once you set `PAPER_TRADE=False` and `DRY_RUN=False`.

**New in V7 — you only trade the last ~2 weeks of the monthly cycle.** The bot now picks the option *expiry* by how close the monthly expiry is: in the **last 15 days** before monthly expiry it offers an **actionable MONTHLY** call (the trades you take); earlier in the cycle it offers a **weekly** call tagged **TRACKING ONLY** — recorded as paper to measure performance, with no button and no real money. See "Which option you'll be offered" below.

**Who does what:**
- **You do:** the morning Kite login, and one button tap for each trade you choose to take.
- **The bot does:** watches the market, applies every filter, sizes the trade, places the order, and manages all exits automatically.
- **Built-in protection:** per-trade risk capped at ~**₹5,000** (1 lot), and a **₹15,000 monthly loss limit** that pauses new trades for the rest of the month if hit.

---

## How the system decides (the full pipeline)

A BUY recommendation appears **only when every check below is YES**. Each one is a noise filter — a single NO means no alert.

```flowchart
start|9:30 AM — ORB forms: record the day's High & Low
process|Every 5 min, take the candle that just closed
gate|Opened INSIDE the range? (don't chase a runaway move)
gate|Closed beyond ORB High +10 / ORB Low -10?
gate|Volume > 1.2x the 20-candle average?
gate|Right side of VWAP? (above for UP, below for DOWN)
gate|Still armed? (max 2 fires per direction per day)
process|SIGNAL — pick ITM-1 option (CE for UP, PE for DOWN)
gate|Spread <=5% and premium <=Rs.500?
gate|1-lot stop-loss risk <=Rs.5000? (premium <=~Rs.256)
gate|Month's loss still under Rs.15000?
process|Tag HIGH CONVICTION if it also cleared the value area (VAH/VAL)
end|Telegram alert with BUY button — nothing is bought until you tap
```

**The same filters in words:**
1. **ORB formed** — the 9:15–9:30 candle sets the day's High and Low.
2. **Open-inside-range** — the breakout candle must *open inside* the ORB (no chasing a move that already ran away).
3. **Clean break** — close must clear ORB High **+10 pts** (UP) or ORB Low **−10 pts** (DOWN).
4. **Volume** — breakout volume > **1.2×** the 20-candle average.
5. **VWAP** — close above VWAP for UP, below for DOWN.
6. **Armed / fire limit** — max **2 fires per direction** per day; after a fire, price must pull back ≥15 pts into the range to re-arm.
7. **Expiry + option** — pick ITM-1 on the **monthly** (≤15 days to monthly expiry → actionable) or the **weekly** (earlier in the cycle → tracked only); skip the button if spread >5% or premium >₹500.
8. **Risk sizing** — take 1 lot only if its worst-case stop-loss is ≤ **₹5,000** (premium ≲ ₹256); otherwise skip.
9. **Monthly loss limit** — if the month's realized loss has hit **₹15,000**, pause until next month.
10. **Conviction tag** — if the breakout also cleared the day's value area, the alert is marked **★ HIGH CONVICTION** (information only — still 1 lot).

### After you tap: the bot runs the trade by itself

```flowchart
start|You tap BUY -> order placed & fill-confirmed (1 lot)
process|Bot checks the live position every 60 seconds (automatic)
process|As premium rises, the stop trails UP behind it (locks gains)
gate|Premium fell to the trailing stop? -> EXIT: TRAIL (win or loss)
gate|Premium down -30% from entry (initial stop)? -> EXIT: STOP-LOSS
gate|Clock reached 15:15? -> EXIT: square-off
end|Auto-sell, fill-confirmed, P&L logged, EXIT alert sent
```

You do **not** tap to exit — exits are automatic. In live mode, both entry and exit are **fill-confirmed**: if a BUY doesn't fill because price jumped, you get "BUY NOT FILLED" and **no position is taken**.

---

## Which option you'll be offered: weekly (track) vs monthly (act)

The bot chooses the option **expiry** from how many days are left to the **monthly** expiry — and **only the monthly half is yours to trade**.

**Last ~2 weeks of the cycle → MONTHLY (you ACT).**
When the monthly contract is **15 days or fewer** from expiry, the bot recommends the **monthly** ITM-1 call, tagged **ACTIONABLE** with a BUY button. These are the trades you take. This is the validated DTE-monthly strategy.

**Earlier in the cycle → WEEKLY (TRACKING ONLY, you do nothing).**
When the monthly is **more than 15 days** out, the bot recommends a **weekly** ITM-1 call but tags it **TRACKING ONLY** — **no button**, and it's recorded as a *paper* trade purely to measure how the weekly half performs. **These stay paper even after you go live — never real money.** You ignore them.

Every alert tells you which half you're in:
- `Expiry: MONTHLY · 4d to expiry — ACTIONABLE` → a real recommendation (trade it if you choose)
- `Expiry: WEEKLY · monthly 22d out — TRACKING ONLY (no order)` → ignore; it's only being recorded

**In one line:** in the ~2 weeks before each monthly expiry you get real monthly recommendations to act on; the rest of the month the bot quietly paper-tracks weeklies and you sit out. The switch is automatic — the cutoff is `DTE_MONTHLY_MAX` (default **15**) in `config.py`, and `WEEKLY_TRACK_ONLY=True` keeps the weekly half paper-only.

---

## Every day: your 4 steps

This is the entire daily routine — about a minute.

1. **~8:30 AM** — you get a Telegram nudge with the Kite login link.
2. **Log in to Kite** (Zerodha + 2FA) and copy the redirect URL from the browser.
3. **Cache today's token** on the VM:
   ```bash
   ssh orb-vm
   cd ~/nifty-orb-alerts
   python3 auth.py        # paste the URL when prompted
   ```
4. **Start the bot:**
   ```bash
   ./start.sh
   ```
   You'll get an "ORB monitor started" message on Telegram. **Done — close SSH; the bot keeps running.**

**During the day:** when a breakout alert arrives, **tap BUY** if you want that trade, or ignore it. The bot handles the stop-loss, target, and 15:15 square-off automatically. To stop the bot early: `./stop.sh`.

---

## Pre-flight: the DRY_RUN flag

The bot ships with `DRY_RUN = True` in `config.py`. In this mode:

- All alerts and buttons appear normally on Telegram
- When you tap the button, the bot **logs** the order it would have placed and confirms back: `[DRY_RUN] Order placed: NIFTY26MAY24450CE BUY 65 @ LIMIT ₹152.95`
- **No real money moves**

**Run for one full trading day in DRY_RUN.** Verify each alert: is the symbol right (correct expiry, correct CE/PE), is the strike one ITM (50 pts in the money for Nifty), is the price near the visible Kite quote? Once you've seen 2-3 clean alerts pass that check, flip the flag:

```bash
ssh ubuntu@<lightsail-ip>
cd ~/nifty-orb-alerts
nano config.py        # change DRY_RUN = True  →  DRY_RUN = False
pkill -f orb_monitor.py
nohup python orb_monitor.py > orb.log 2>&1 &
```

The startup Telegram message will now say `ORB monitor started (LIVE)` instead of `(DRY_RUN)`.

---

## Paper trading mode (V3)

Before flipping `DRY_RUN = False` (which uses real money), run **`PAPER_TRADE = True`** for a couple of weeks. Paper mode is the realistic dress rehearsal:

- All alerts fire normally on Telegram with BUY buttons.
- When you tap the button, **no Kite order is placed** — but the bot:
  1. Journals the trade in `trades.db` at the **alert-time LIMIT price** (same value LIVE will use).
  2. Starts monitoring the position every minute (`POSITION_POLL_SECONDS = 60`).
  3. Auto-exits on the **trailing stop** (initial −30%, then trails up to lock gains; exit reason `TRAIL`) or **TIMEOUT** (15:15 IST square-off). *(With `USE_TRAILING_STOP=False` it reverts to the old fixed −30% SL / +50% TARGET.)*
  4. Sends a Telegram exit alert with realized P&L.

This builds a full P&L history without risking capital. Mode truth table:

| `PAPER_TRADE` | `DRY_RUN` | Mode | Real order? | DB row? | Auto-exit? |
|---|---|---|---|---|---|
| True | True | **PAPER** | No | Yes | Yes |
| True | False | **PAPER** | No | Yes | Yes |
| False | True | **DRY_RUN** | No | No | No (alert-only) |
| False | False | **LIVE** | Yes | Yes | Yes |

**Important:** `PAPER_TRADE` overrides `DRY_RUN`. To go live, set **both** to `False`.

### How the journal records prices

The journal records the price from the alert (e.g., `LIMIT: ₹152.95` shown on the BUY button), not the live ask at the moment you tap. This means:

- **In LIVE**, your Kite LIMIT order is placed at exactly that price — fills land at or near it.
- **In PAPER**, the same price is recorded — so the journal measures **signal quality**, not tap-reaction speed.

If you tap a button 5 minutes late and the option has moved ₹30, the journal still uses the original alert price. The position monitor uses real-time LTP for SL / target / timeout decisions, so the trade is still evaluated against current market — but the entry P&L is anchored to the price the bot recommended.

### Configurable exit levels (in `config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `SL_PCT` | `0.30` | Initial stop 30% below entry; also the trailing give-back distance |
| `USE_TRAILING_STOP` | `True` | Trailing stop, no fixed target ("let winners run"). False = old fixed SL/target |
| `TARGET_PCT` | `0.50` | Fixed target — **ignored when `USE_TRAILING_STOP=True`** |
| `SQUARE_OFF_HOUR` | `15` | Hard square-off hour (IST) |
| `SQUARE_OFF_MINUTE` | `15` | Hard square-off minute. 15:15 = 15 min before close |
| `POSITION_POLL_SECONDS` | `60` | How often to poll the option for exit conditions |
| `RISK_PER_TRADE_INR` | `5000` | Max rupees risked per trade; pricier options get skipped (see below) |
| `MAX_LOTS` | `1` | Hard ceiling on lots per trade — never sizes beyond one lot |
| `MONTHLY_LOSS_LIMIT_INR` | `15000` | Month's realized loss cap; pauses new trades when hit |

### Position sizing & the monthly loss limit (V4)

Two guards now bound how much you can lose. Both are automatic — you don't tap anything extra.

**1. Per-trade risk cap (`RISK_PER_TRADE_INR`, default ₹5,000).**
Before showing a BUY button, the bot works out the worst case if the stop-loss hits:

> one-lot risk = option premium × `SL_PCT` (0.30) × `LOT_SIZE` (65) = premium × 19.5

- If that worst-case loss is **within ₹5,000**, you get the button for **one lot** (65).
- If even one lot would risk **more than ₹5,000**, the trade is **skipped** — no button — and you'll see `1-lot risk ₹… > risk cap ₹5000`.
- At ₹5,000 the cutoff is a premium of about **₹256**: cheaper options trade, pricier ones are skipped.

Why one lot only? `MAX_LOTS = 1` is a hard ceiling so the cap can only ever *skip* a trade, never *scale you up* into a bigger position. This is deliberate downside protection. If you ever want the bot to take expensive trades too, raise `RISK_PER_TRADE_INR` — but understand that also raises how much you can lose on one trade.

**2. Monthly loss limit (`MONTHLY_LOSS_LIMIT_INR`, default ₹15,000).**
The bot adds up your **realized** (closed) P&L for the current calendar month. Once losses for the month reach ₹15,000, it **stops offering new trades for the rest of the month** — you'll see `monthly loss limit hit`. It resets on its own at the start of the next month.

- Any position already open is **still managed** normally (stop-loss, target, square-off all run). The limit only blocks *new* entries.
- During paper trading the limit counts paper P&L; once live, it counts live P&L.

These two are your hard guardrails. Treat a skip or a pause as the system doing its job — the whole point is to make a single bad trade, or a bad month, impossible to turn into a disaster.

---

## Daily morning ritual

You'll get **two Telegram messages from the system before market open**:

### 8:30 AM IST — Token refresh nudge

A message like:

```
Good morning. Wed, 30 Apr 2026

Refresh Kite access token before 9:15 IST.

1. Tap to log in:
https://kite.zerodha.com/connect/login?api_key=XXX&v=3

2. SSH to Lightsail and run:
   cd ~/nifty-orb-alerts
   python3 auth.py
   ./start.sh
```

### Steps to follow (~60 seconds)

1. **Tap the Kite login link** in the Telegram message → log in with Zerodha + 2FA.
2. After login, you land on a page that may look broken — that's fine. **Copy the full URL** from the browser address bar (it contains `request_token=...`).
3. **SSH to Lightsail** from your laptop or AWS browser-based SSH:
   ```bash
   ssh orb-vm                  # or: ssh ubuntu@<lightsail-ip>
   cd ~/nifty-orb-alerts
   python3 auth.py
   ```
4. **Paste the URL** when prompted. You'll see `Access token saved for today.`
5. **Start the monitor** (this stops any old process and starts a fresh one):
   ```bash
   ./start.sh
   ```
   `start.sh` first checks today's token is cached, then restarts the bot. If the
   token is missing it tells you to run `python3 auth.py` instead of failing
   silently. To stop the bot any time: `./stop.sh`.
6. Within a minute, you should get a Telegram alert: `ORB monitor started (PAPER)`. If yes — you're done. **Close the SSH window. The bot keeps running.**

### Why the bot keeps running after you close SSH

This is the part most people ask about, so here's the plain explanation:

- `nohup` (= "no hangup") tells Linux: "if my session ends, don't kill this process."
- The trailing `&` puts it in the background so the shell prompt comes back immediately.
- `> orb.log 2>&1` sends both normal output and errors into the file `orb.log` so the process doesn't try to write to a terminal that's gone.

**Net effect:** you can close the SSH window, close your laptop lid, fly across the country — the bot keeps running on the Lightsail VM until either:
- You explicitly kill it (see "Daily shutdown" below), or
- The VM itself reboots (rare; AWS usually warns you).

If your VM does reboot, the bot will **not** restart automatically — see the optional **systemd setup** at the end of this handbook to fix that.

---

## What the alerts mean

### "ORB monitor started"
Confirmation the bot is alive. Includes mode (DRY_RUN/LIVE), futures symbol, lot size. No action.

### "ORB formed" (around 9:30:30 AM)
```
ORB formed for NIFTY24MAYFUT
High: 22687.40
Low: 22641.25
Buffer: 10 pts | Re-arm: 15 pts
Watching 5-min closes for breakout/breakdown...
```
Your day's range is set. Wider ranges (>80 pts) often mean choppier days — be more selective.

### "BREAKOUT (UP) — fire 1/2" — the real signal with a button
```
BREAKOUT (UP) — fire 1/2
NIFTY24MAYFUT 5m close: 22693.10 > ORB High 22687.40 (+10 buf)
Vol: 142500 (avg 89200, x1.60)
VWAP: 22669.85
Spot Nifty: 22691.55
Expiry: MONTHLY · 4d to expiry — ACTIONABLE
ITM-1 CE: NIFTY24MAY22650CE
Bid/Ask: 152.10/152.45
LIMIT: ₹152.95 | Qty: 65 (risk ≤ ₹5000)

[ BUY 65 CE @ ₹152.95 ]   ← tap to place order
```

- **fire 1/2** = first breakout in this direction today
- **x1.60** = breakout candle volume was 1.6× the 20-candle avg — strong
- **Expiry: MONTHLY … ACTIONABLE** = you're in the last ~2 weeks of the monthly cycle, so this is a real recommendation with a button. (A `WEEKLY … TRACKING ONLY` line means ignore it — see below.)
- **ITM-1 CE** = 50 pts in the money (better delta than ATM for short holds)
- **Bid/Ask** = the live option quote at the moment the alert fired
- **LIMIT ₹152.95** = `best_ask + 0.50` rounded to 0.05 tick — almost guaranteed to fill at the visible ask

**Tap the button** → bot places a BUY LIMIT order via Kite → confirmation message comes back:
```
Order placed: NIFTY24MAY22650CE BUY 65 @ LIMIT ₹152.95
Order ID: 250502000123456
```

If the order fails (margin, rejected, network), you'll see:
```
ORDER FAILED for NIFTY24MAY22650CE: <error message from Kite>
```
…and you can place the trade manually in Kite.

### "BREAKDOWN (DOWN) — fire 1/2"
Same shape but opposite — buys an ITM-1 PE (50 pts above spot).

### "fire 2/2"
A re-entry breakout after price came back inside ORB by ≥15 pts. Often **cleaner than fire 1** because the noise has been shaken out.

### "Auto-order skipped" (any reason)
You'll see this if the safety guards rejected the auto-order:
- `spread 7.2% > 5%` — option spread too wide; place manually with a tight limit
- `premium ₹620 > MAX_PREMIUM ₹500` — option too expensive (often deep ITM after a big move)
- `quote fetch failed: ...` — Kite API hiccup; refresh and place manually
- `1-lot risk ₹5640 > risk cap ₹5000` — one lot of this option would risk more than your per-trade cap (its premium is above ~₹256). This is **by design** — don't override it lightly (see "Position sizing" below)
- `monthly loss limit hit (MTD ₹-15200, limit ₹15000)` — the month's realized loss reached the cap; **no new trades until next month**

The alert still fires — you just don't get the button.

### "★ HIGH CONVICTION" tag (V5)
Some signals carry a `★ HIGH CONVICTION` tag on the button and a `VA: POC.. / VAH.. / VAL..` line in the alert. This means the breakout cleared the day's **value area** (where most volume traded) — historically a higher-quality setup. A signal without the star is `normal — inside value`.

**Important:** this is *information only*. Both kinds still fire and both still place **one lot** if you tap. The star does **not** mean "trade bigger" — size is always 1 lot. We're collecting data to learn whether high-conviction signals actually win more.

### "TRACKING ONLY" (weekly, early in the cycle)
Earlier in the monthly cycle (monthly more than 15 days out) you'll see alerts like:
```
Expiry: WEEKLY · monthly 22d out — TRACKING ONLY (no order)
ITM-1 CE: NIFTY25JUN24650CE
LIMIT: ₹138.00 | Qty: 65
[TRACKING ONLY — weekly journaled as paper, not actionable]
```
There is **no button and nothing to do**. The bot records it as a *paper* trade so the weekly half's performance can be compared later. **These never use real money — not even in LIVE mode.** You act only on **ACTIONABLE** (monthly) alerts. This is normal for roughly the first two weeks after each monthly expiry.

### Live fill messages (V5 — only in LIVE mode)
In LIVE mode the bot confirms your order actually filled before acting. You may see:
- **"BUY NOT FILLED"** — price moved past your limit before the order filled, so **no position was taken**. Nothing to do; wait for the next signal. (This is a safety feature, not an error.)
- **"⚠️ PARTIAL FILL"** — only part of the lot filled (rare). The message tells you how many; check Kite.
- **"SELL NOT FILLED … retrying"** — an exit didn't fill on the first try; the bot keeps trying. The broker's auto-square-off (~3:20 PM) is the final backstop.

### "Market closed. ORB monitor stopping."
End of day at 3:30 PM. Shows total fires.

---

## Your trading checklist (still applies)

The button tap places the **entry**. Stops, exits, sizing — still your job:

1. **Define stop-loss before the position fills.** Standard rule: stop on the option = 30–40% of premium paid. On the futures level = re-entry into ORB range (use a Kite GTT for this).
2. **Position size sanity.** 1 lot of Nifty at premium ₹150 = ₹9,750 capital deployed. Stop at 30% = ₹2,925 risk. That should be ≤ 1–2% of trading capital. If it's more, **don't tap the button — the bot doesn't know your account size.**
3. **Set a target** before the trade goes anywhere. Common: 1.5×–2× the risk.
4. **Don't average down** if it goes against you.

---

## Daily shutdown (after market close)

The bot auto-sends `Market closed. ORB monitor stopping.` at 3:30 PM and the main loop exits. **But the Python process may still be alive** for a few seconds (the Telegram listener thread is a daemon and dies with the process — but only when the main thread exits cleanly).

Verify and clean up:

```bash
ssh ubuntu@<lightsail-ip>
ps aux | grep orb_monitor | grep -v grep
```

If you see a line, kill it:
```bash
pkill -f orb_monitor.py
```

Then verify it's gone:
```bash
ps aux | grep orb_monitor | grep -v grep
```
Empty output = clean.

**Why this matters:** if you start tomorrow's session without killing today's process, you'll have **two bots running**, doubled alerts, and possibly two BUY orders on the same button tap. Always kill before starting fresh.

---

## Daily P&L review (V3)

Every paper / live trade is journaled to `trades.db` (a SQLite file in `~/nifty-orb-alerts/`). One row per position with entry, exit, and P&L.

### Quick review with `report.py`

```bash
cd ~/nifty-orb-alerts && source venv/bin/activate
python3 report.py                              # today
python3 report.py 2026-05-08                   # specific date
python3 report.py 2026-05-08 --mode PAPER      # filter by mode
```

Output is a per-trade table plus daily totals (wins / losses / win rate / net P&L).

### Correcting a row (rare)

Back up first, then UPDATE. `pnl` is **stored**, not computed on read — always recompute it whenever you change `entry_price` or `exit_price`, or the row will be inconsistent:

```bash
cp trades.db trades.db.bak.$(date +%Y%m%d-%H%M)

sqlite3 trades.db "
BEGIN;
UPDATE trades
SET entry_price = <NEW_ENTRY>,
    pnl         = (exit_price - <NEW_ENTRY>) * qty
WHERE id = <ID>;
COMMIT;
"
```

### Reconstructing a trade from `orb.log`

V3 writes structured, timestamped logs for every signal evaluation, fire, fill, position poll, and exit. After a trading day you can fully reconstruct any trade:

```bash
grep SIGNAL_FIRED orb.log         # all signals that fired today
grep "EXIT reason" orb.log        # all exits with realized P&L
grep position_tick orb.log        # minute-by-minute monitoring while holding
grep "tick candle" orb.log        # every 5-min signal evaluation (including skips)
```

Useful when a trade behaved unexpectedly — you'll see the exact ORB levels, volume ratio, VWAP, and signal decision for each candle.

---

## Tuning the filters

All thresholds live in `config.py` on the Lightsail box. Edit, save, restart.

| Setting | Default | What it does | When to change |
|---|---|---|---|
| `VOLUME_MULTIPLIER` | `1.2` | Breakout vol must be ≥ this × the 20-candle avg | Up to `1.5` for stricter; down to `1.0` if missing valid moves |
| `LOOKBACK_CANDLES` | `20` | Number of recent 5-min candles for the volume average | Rarely needs changing |
| `MAX_FIRES_PER_DIRECTION` | `2` | Max alerts per direction per day | `1` for ultra-conservative; `3` if you want every clean re-break |
| `BREAKOUT_BUFFER` | `10` | Pts beyond ORB needed to count as a breakout | Higher = fewer fakeouts but later entries |
| `REARM_BUFFER` | `15` | Pts back inside ORB needed before re-arm | Higher = stricter re-entry filter |
| `LOT_SIZE` | `65` | Nifty lot size (NSE-defined) | Update if NSE changes the lot size |
| `LIMIT_BUFFER` | `0.50` | ₹ above best ask for the BUY limit | Higher = more fill certainty, slightly worse price |
| `MAX_SPREAD_PCT` | `0.05` | Skip auto-order if (ask−bid)/mid > this | Lower for stricter liquidity filter |
| `MAX_PREMIUM` | `500` | Skip auto-order if option premium > this | Catches expensive far-from-spot options |
| `ORDER_PRODUCT` | `"MIS"` | MIS = intraday auto-squareoff | Use `"NRML"` only if carrying overnight |
| `DRY_RUN` | `True` | Log instead of placing real orders | Flip to `False` after one clean DRY_RUN day |
| `PAPER_TRADE` | `True` | Simulate fills + auto-exits + journal to `trades.db` (no real orders) | Flip to `False` once 2 weeks of paper history look profitable |

**After editing `config.py`:**
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
Common errors:

| Error contains | Fix |
|---|---|
| `No valid access token for today` | You forgot `python auth.py`. Run it. |
| `TokenException` | Token expired or wrong API key/secret. Re-run `auth.py`. |
| `connection`, `timeout`, `network` | Lightsail momentarily lost network. Restart. |
| `No active Nifty futures found` | Holiday or expiry edge case. Check NSE calendar. |

### Telegram alerts not arriving
```bash
python -c "from telegram_alert import send_alert; send_alert('test')"
```
- Nothing on phone → bot token wrong, or you blocked the bot, or chat ID wrong.
- Get `Telegram send failed: ...` → the error tells you.

### Tapped the button but no order/confirmation came back
1. Check `orb.log` — look for `Callback handler error:` or `ORDER FAILED`.
2. Most likely cause: the access token expired mid-day (rare; tokens last ~24h). Re-run `auth.py`, restart the bot. The button you already tapped won't be retried — place that one manually.
3. Could also be that the bot process died. `ps aux | grep orb_monitor | grep -v grep` — if empty, restart.

### "Auto-order skipped" on every alert
Spread or premium guards are too tight. Loosen `MAX_SPREAD_PCT` (e.g., to `0.07`) or raise `MAX_PREMIUM` (e.g., to `800`). Restart.

### Process still running from yesterday
```bash
ps aux | grep orb_monitor | grep -v grep
pkill -f orb_monitor.py
```

### Got two identical alerts within seconds
Two bots running. `pkill -f orb_monitor.py` (kills both), then start fresh.

---

## Things you should NOT do

- **Don't flip `DRY_RUN = False` without one full day of dry-run validation.** Real money, irreversible orders.
- **Don't run `python auth.py` after market open.** The bot may already be exiting.
- **Don't push `config.py` to GitHub.** It's in `.gitignore`. Secrets stay on Lightsail.
- **Don't paste your Kite API secret, access token, or TOTP into chats, screenshots, or anywhere outside `config.py`.** If you do — rotate immediately at https://developers.kite.trade/apps.
- **Don't tap a button after the alert is more than ~30 seconds old** — the underlying option price has moved and the LIMIT may now be too far below market to fill.
- **Don't let one losing day double your size the next day** to "recover".

---

## Risk discipline (read once a week)

- Options decay daily. A 50% drop in the option premium is normal and frequent.
- Win rate will not be 100%. Expect 40–55% winners. Profitability comes from average winner > average loser.
- **Daily loss cap:** stop trading after 2 consecutive losses or 3% of capital, whichever comes first.
- **Weekly review:** Sunday evening, review every alert vs. trades you took vs. P&L. Look for patterns (wide ORBs lose, tight ORBs win, etc.).

---

## Optional: auto-restart with systemd

If you want the bot to survive a Lightsail VM reboot without you SSHing in to start it manually, create a systemd service. **You still need to refresh the Kite token every morning** — systemd just handles the process lifecycle.

One-time setup on the VM:

```bash
sudo tee /etc/systemd/system/orb-monitor.service > /dev/null <<'EOF'
[Unit]
Description=Nifty ORB Monitor
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/nifty-orb-alerts
ExecStart=/home/ubuntu/nifty-orb-alerts/venv/bin/python orb_monitor.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/ubuntu/nifty-orb-alerts/orb.log
StandardError=append:/home/ubuntu/nifty-orb-alerts/orb.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable orb-monitor
```

Daily morning then becomes:
```bash
python auth.py                          # refresh token (still manual)
sudo systemctl restart orb-monitor      # picks up the new token
```

Daily shutdown:
```bash
sudo systemctl stop orb-monitor
```

Status check:
```bash
sudo systemctl status orb-monitor
```

If you're not comfortable with systemd, **stick with `nohup`** — it works fine for daily manual operation.

---

## Roadmap

| Version | Status | What it adds |
|---|---|---|
| **V1** | Done | Telegram alerts only |
| **V1.5** | Done | Re-arm logic — up to 2 fires per direction |
| **V2** | Done | 5-min tracking; ITM-1 strike; 1-tap LIMIT order with safety guards; DRY_RUN flag |
| **V3** | **Live now** | PAPER_TRADE mode; auto SL/target/timeout exits; `trades.db` journal at alert-time price; structured `orb.log`; `report.py` daily summary |
| **V4** | Done | Risk-based position sizing; monthly loss limit |
| **V5** | Done | Live fill-confirmation; VAH/VAL high-conviction tagging |
| **V6** | Done | Trailing-stop exit ("let winners run"); `USE_TRAILING_STOP` flag |
| **V7** | **Live (paper)** | DTE expiry routing: monthly ITM-1 (≤15 DTE, actionable) vs weekly (early cycle, paper-tracked) — `DTE_MONTHLY_MAX`, `WEEKLY_TRACK_ONLY`. Paper-validating before live flip |
| **V7+** | Researching | Put-side support-OI confirmation filter — passed walk-forward backtest; paper-observe ~1 month before adoption |
| **Later** | Maybe | Multi-symbol (BankNifty, FinNifty); holiday calendar; web dashboard with live P&L |

---

## Reference

- **Repo:** https://github.com/anilkumarjonnalagadda/nifty-orb-alerts (private)
- **Kite developer console:** https://developers.kite.trade/apps
- **NSE holiday calendar:** https://www.nseindia.com/resources/exchange-communication-holidays
- **Bot logs on Lightsail:** `~/nifty-orb-alerts/orb.log`
- **Cron logs (token reminder):** `~/nifty-orb-alerts/cron.log`
