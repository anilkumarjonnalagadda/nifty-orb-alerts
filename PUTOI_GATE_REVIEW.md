# Put-OI Gate — Code Review & Backtest Reconciliation (updated 2026-06-26)

## RESOLUTION (2026-06-26): +11.72% is REAL and reproduced exactly

The Volrix team supplied the full run UUIDs. OOS run `550e67eb-fb53-4218-8739-736d13d38010`
→ `run_details(include_code=True)` returned the original code (class `ORBCEMonthlyDTE`).
Re-running it verbatim (run 87383c09, OOS 2024-01-01→2026-06-18) reproduced the documented
numbers EXACTLY: **NET (1%/side) CAGR 11.72%, Sharpe 1.71, maxDD 18.16%, 98 trades, PF 1.4**;
GROSS 19.8%. The earlier "artifact / loses money" conclusion in this note was WRONG — it came
from a reconstruction error (trailing stop set to 1%/1% instead of the validated **15%/15%**;
also MAX_FIRES 1 vs 2, and value-area from 1-min vs cumulative 5-min bars). The put-OI gate
(weekly puts) was never the problem — live and original match on it.

The real open item is now: **does the LIVE bot faithfully implement the validated spec?**
See the live-vs-validated diff below.

---

## (Historical) original context

Investigating why the deployed ORB-UP strategy could not initially be reproduced at the
documented **+11.72% net OOS CAGR** ("DTE-monthly ≤15 CE +VAH +put-OI",
Backtest_CAGR_Summary.pdf, runs 2026-06-20). The PDF stored only 8-char run-ID prefixes;
the Volrix team later shared the full UUIDs, which resolved it (above).

## Backtest reconstruction (OOS 2024-01-01 → 2026-06-18, NIFTY, 1 lot, capital ₹1.5L)

| Reconstruction (faithful as possible) | Trades | GROSS CAGR | NET CAGR (1%/side) |
|---|---|---|---|
| Weekly-put gate (matches live code)   | 128 | +6.47% | −6.1% |
| OI divergence gate                    | 100 | +3.47% | −6.75% |
| Monthly-put gate (expiry hypothesis)  | 110 | +4.97% | −5.4% |
| **Documented (unsaved original)**     | 98  | **+19.80%** | **+11.72%** |

Every principled rebuild lands at ~3.5–6.5% gross / **−5 to −7% net**. The documented gross is
**3–4× higher than anything reproducible** — too large a gap for a gate detail (strike/expiry
changes move results ~1–2 pts). Most likely explanation: a **methodological artifact
(look-ahead bias)** in the unsaved original. Do NOT deploy real capital against 11.72% until
the original code is recovered and the gap is explained.

## Code-review findings — `orb_monitor.py` put-OI gate

1. **Expiry mismatch (weekly vs monthly).** `pe_symbol_at_strike()` (orb_monitor.py:217-226)
   returns the **nearest expiry = weekly** PE, but the traded leg is the **monthly** ITM-1 CE
   (`itm_option(..., target_expiry=fut["expiry"])`). So support-OI is read on weekly puts while
   the position is monthly. They coincide only in the final ~week of the cycle (nearest weekly ==
   monthly); the mismatch bites at **DTE 8–15**. Strong candidate for live-vs-original drift.
   NOTE: monthly-put rebuild was still −5.4% net — fixing this corrects logic, not edge.

2. **Gate is a near-no-op.** `put_oi_rising()` (orb_monitor.py:229-234) = `current_oi > baseline_oi`
   (any rise vs 09:30). Intraday OI almost always rises → passes nearly every breakout
   (empirically 128 gated ≈ ungated count). The "support confirmation" gate barely filters.
   Same for `call_oi_rising()` on the V8 DOWN side.

3. **Fail-open on snapshot failure.** Gate runs only `if sup_oi_base is not None` (orb_monitor.py:1138).
   If the 09:30 OI snapshot fails (quote error / illiquid strike), the gate is skipped entirely and
   every breakout passes that day — a live-vs-backtest divergence the backtest never sees.

4. **Minor: weekly-expiry-day noise.** On weekly expiry days the nearest-weekly puts are the
   expiring series; their OI distorts near settlement.

## Live (orb_monitor.py) vs validated ORBCEMonthlyDTE — diff (2026-06-26)

ENTRY: faithful. ORB 9:15-9:30, buffer 10, re-arm 15, vol 1.2x/20 intraday, VWAP gate,
weekly put-OI rising vs 09:30, DTE≤15 monthly, MAX_FIRES=2, monthly ITM-1 CE — all match.

MISMATCHES:
1. **EXIT trail (CRITICAL).** Validated = `trailSL 15%/15%` (stepped ratchet). Live `decide_exit`
   (orb_monitor.py:435-463) = continuous `stop = peak − 0.30*entry`. Same initial −30% SL and
   no fixed target, but different trail algorithm. Trail is the dominant P&L lever (a 1% vs 15%
   error swung gross 6.5%→19.8% in rebuilds) → MUST be quantified before live.
2. **VAH source (moderate).** Validated builds value area from cumulative 5-min bars; live builds
   from 1-min candles (orb_monitor.py:1127, volume_profile(candles_1m,...)). Different VAH → diff
   set of breakouts clear close>VAH.
3. **VWAP source (minor).** Live cumulates 1-min hlc3; validated cumulates 5-min hlc3.
4. **Open-inside (minor).** Live UP checks open≤ORB_high; validated checks ORB_low≤open≤ORB_high.
5. **Scope.** Live also runs V8 DOWN/PE + weekly track-only PAPER trades — separate from the
   validated monthly-CE edge; don't corrupt CE path.

## Recommended action

- Edge is REAL (OOS 11.72% net / Sharpe 1.71 / ~18% DD). Path to live = make the live bot match
  the validated spec, EXIT first.
- Decisive test to run: backtest the validated entry with the LIVE exit mechanism
  (peak−0.30*entry trail) to measure whether the live trail preserves the 11.72% or degrades it.
  If it degrades, change live `decide_exit` to the validated 15%/15% stepped trail.
- IS is weaker than OOS (IS CAGR ~4.9% / Sharpe 1.29) — regime-lean caveat; start small per
  algo_design_brief.
- Related memory: `gated-strategy-not-reproducible` (RESOLVED), `volrix-backtest-conclusion`.
