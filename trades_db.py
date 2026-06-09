"""SQLite-backed trade journal for paper + live trades.

One row per position. Entry columns are populated on BUY, exit columns on
SELL. An open position is any row whose exit_price IS NULL — there is at most
one such row at a time because the bot holds one position at a time.

Every row carries a `mode` column ("PAPER" or "LIVE") so paper-trade history
is preserved even after going live, and reports can filter by mode.
"""

import sqlite3
from datetime import datetime

import pytz

import config

IST = pytz.timezone("Asia/Kolkata")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    signal_type   TEXT NOT NULL,
    mode          TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    entry_ts      TEXT NOT NULL,
    exit_price    REAL,
    exit_ts       TEXT,
    exit_reason   TEXT,
    pnl           REAL
);
"""


def _connect(path=None):
    """Open a SQLite connection. Caller is responsible for closing it.

    check_same_thread=False because the callback listener thread writes BUYs
    while the main thread writes SELLs against the same connection. Caller
    must serialize writes with a lock.
    """
    target = path if path is not None else config.DB_PATH
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path=None):
    """Create the trades table if missing. Idempotent. Returns the connection."""
    conn = _connect(path)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def _now_iso():
    return datetime.now(IST).isoformat(timespec="seconds")


def record_buy(conn, symbol, qty, entry_price, signal_type, mode, ts=None):
    """Insert a new open position. Returns the row id (use to close later)."""
    cur = conn.execute(
        "INSERT INTO trades (symbol, qty, signal_type, mode, entry_price, entry_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, int(qty), signal_type, mode, float(entry_price), ts or _now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def record_sell(conn, entry_id, exit_price, exit_reason, ts=None):
    """Close the position identified by entry_id. Computes pnl from entry_price.

    pnl = (exit_price - entry_price) * qty for a long option position.
    """
    row = conn.execute(
        "SELECT entry_price, qty FROM trades WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No trade with id {entry_id}")
    pnl = (float(exit_price) - row["entry_price"]) * row["qty"]
    conn.execute(
        "UPDATE trades SET exit_price = ?, exit_ts = ?, exit_reason = ?, pnl = ? "
        "WHERE id = ?",
        (float(exit_price), ts or _now_iso(), exit_reason, pnl, entry_id),
    )
    conn.commit()
    return pnl


def month_to_date_pnl(conn, mode, year_month):
    """Sum realized P&L for closed trades in `mode` during `year_month`.

    `year_month` is "YYYY-MM", matched against the first 7 chars of exit_ts.
    Open trades (exit_ts NULL) are excluded — only realized P&L counts toward
    the monthly loss limit. Returns 0.0 when there are no matching trades.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) AS p FROM trades "
        "WHERE mode = ? AND exit_ts IS NOT NULL AND substr(exit_ts, 1, 7) = ?",
        (mode, year_month),
    ).fetchone()
    return row["p"]


def get_open_position(conn):
    """Return the most recent open position as a dict, or None.

    Used at startup to recover a position if the bot was restarted mid-day.
    """
    row = conn.execute(
        "SELECT * FROM trades WHERE exit_price IS NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
