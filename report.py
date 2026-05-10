"""Daily paper/live trade summary from trades.db.

Usage:
    python3 report.py                # today (IST)
    python3 report.py 2026-05-08     # specific date
    python3 report.py --mode LIVE    # filter by mode (default: both)
"""

import argparse
import sys
from datetime import datetime

import pytz

import config
from trades_db import init_db

IST = pytz.timezone("Asia/Kolkata")


def _fmt_time(iso_ts):
    if not iso_ts:
        return "—"
    return iso_ts[11:16]


def _fmt_money(x):
    if x is None:
        return "—"
    return f"{x:>+8.2f}" if x < 0 else f" {x:>7.2f}"


def fetch_trades(conn, date_str, mode):
    sql = (
        "SELECT id, symbol, qty, signal_type, mode, entry_price, entry_ts, "
        "       exit_price, exit_ts, exit_reason, pnl "
        "FROM trades WHERE date(entry_ts) = ?"
    )
    params = [date_str]
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def print_report(date_str, trades):
    print()
    print(f"Trade report — {date_str}")
    print("=" * 60)

    if not trades:
        print("No trades on this date.")
        return

    print(
        f"{'In':>5} {'Out':>5} {'Symbol':<22} {'Sig':<4} "
        f"{'Entry':>7} {'Exit':>7} {'P&L':>9} {'Why':<8}"
    )
    print("-" * 70)

    closed = []
    open_pos = []
    for t in trades:
        line = (
            f"{_fmt_time(t['entry_ts']):>5} "
            f"{_fmt_time(t['exit_ts']):>5} "
            f"{t['symbol'][:22]:<22} "
            f"{t['signal_type']:<4} "
            f"{t['entry_price']:>7.2f} "
            f"{(t['exit_price'] or 0):>7.2f} "
            f"{_fmt_money(t['pnl'])} "
            f"{(t['exit_reason'] or 'OPEN'):<8}"
        )
        print(line)
        if t["exit_price"] is None:
            open_pos.append(t)
        else:
            closed.append(t)

    print("-" * 70)
    if closed:
        wins = sum(1 for t in closed if t["pnl"] > 0)
        losses = len(closed) - wins
        net = sum(t["pnl"] for t in closed)
        win_rate = wins / len(closed) * 100
        print(
            f"Closed: {len(closed)}   Wins: {wins}   Losses: {losses}   "
            f"Win%: {win_rate:.0f}%"
        )
        print(f"Net P&L: ₹{net:+.2f}")

    if open_pos:
        print(f"\nStill open: {len(open_pos)} position(s) — no P&L yet.")

    modes = {t["mode"] for t in trades}
    if len(modes) == 1:
        print(f"Mode: {modes.pop()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "date",
        nargs="?",
        default=datetime.now(IST).strftime("%Y-%m-%d"),
        help="Date in YYYY-MM-DD (default: today IST)",
    )
    ap.add_argument(
        "--mode",
        choices=["PAPER", "LIVE"],
        help="Filter to one mode (default: both)",
    )
    args = ap.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid date: {args.date}. Use YYYY-MM-DD.", file=sys.stderr)
        sys.exit(2)

    conn = init_db(config.DB_PATH)
    try:
        trades = fetch_trades(conn, args.date, args.mode)
        print_report(args.date, trades)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
