"""Local MCP server exposing the ORB bot's trade journal as queryable tools.

This runs on the **Mac** (where Claude Code lives), not on the VM. Each tool
call opens an SSH connection to the Lightsail VM (`orb-vm` in ~/.ssh/config)
and runs the query against `trades.db` in **read-only** mode. Nothing is ever
written to the live trade journal.

Two independent safety layers protect the real-money data:
  1. `_assert_read_only` rejects anything that isn't a single SELECT/WITH.
  2. `sqlite3 -readonly` opens the file read-only, so even a query that slips
     past layer 1 physically cannot modify the database.

Register with Claude Code (one time):
    claude mcp add trades --scope local -- \
        /ABS/PATH/.venv-mcp/bin/python /ABS/PATH/mcp_trades_server.py
"""

import json
import re
import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

# --- Where the data lives -----------------------------------------------------
SSH_HOST = "orb-vm"  # alias defined in ~/.ssh/config
DB_PATH = "/home/ubuntu/nifty-orb-alerts/trades.db"
SSH_TIMEOUT = 30  # seconds

# Column legend, kept in sync with trades_db.py on the VM.
SCHEMA_NOTES = """\
Table: trades  (one row per position; exit_* is NULL while a position is open)
  id           INTEGER  primary key
  symbol       TEXT     option tradingsymbol, e.g. NIFTY26MAY23750CE
  qty          INTEGER  contracts (lot size * lots)
  signal_type  TEXT     'UP' (CE breakout) or 'DOWN' (PE breakout)
  mode         TEXT     'PAPER' or 'LIVE'
  entry_price  REAL     option premium at entry
  entry_ts     TEXT     ISO8601 IST timestamp of entry
  exit_price   REAL     premium at exit (NULL if still open)
  exit_ts      TEXT     ISO8601 IST timestamp of exit (NULL if open)
  exit_reason  TEXT     e.g. TARGET / SL / EOD (NULL if open)
  pnl          REAL     (exit_price - entry_price) * qty (NULL if open)
Note: filter a single day with  date(entry_ts) = 'YYYY-MM-DD'."""

mcp = FastMCP("trades")

# Single-statement, read-only guard. The hard guarantee is `sqlite3 -readonly`;
# this is the friendly first line of defense with clear error messages.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|truncate|grant|revoke|begin|commit|rollback|"
    r"savepoint)\b",
    re.IGNORECASE,
)


def _assert_read_only(sql: str) -> str:
    """Return the cleaned SQL if it is a single read-only statement, else raise."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise ValueError("Empty query.")
    if ";" in s:
        raise ValueError("Only one statement is allowed (remove the ';').")
    first = s.split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise ValueError("Only read-only SELECT/WITH queries are allowed.")
    hit = _FORBIDDEN.search(s)
    if hit:
        raise ValueError(
            f"Disallowed keyword '{hit.group(0)}' — this server is read-only."
        )
    return s


def _run_remote_sql(sql: str) -> list[dict]:
    """SSH to the VM, run a read-only query, return rows as a list of dicts."""
    safe = _assert_read_only(sql)
    remote = f"sqlite3 -readonly -json {shlex.quote(DB_PATH)} {shlex.quote(safe)}"
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             SSH_HOST, remote],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Timed out after {SSH_TIMEOUT}s connecting to {SSH_HOST}. "
            "Is the VM up and reachable (try `ssh orb-vm`)?"
        )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        raise RuntimeError(f"Query failed on VM: {err}")
    out = proc.stdout.strip()
    return json.loads(out) if out else []


@mcp.tool()
def run_query(sql: str) -> str:
    """Run a read-only SQL query against the trade journal (trades.db) on the VM.

    Only a single SELECT/WITH statement is allowed; the database is opened
    read-only. Returns rows as a JSON array of objects (column name -> value).
    Call get_schema() first if you need the column names.
    """
    rows = _run_remote_sql(sql)
    if not rows:
        return "No rows."
    return json.dumps(rows, indent=2, default=str)


@mcp.tool()
def get_schema() -> str:
    """Return the trades table columns and notes for writing queries."""
    return SCHEMA_NOTES


@mcp.tool()
def recent_trades(limit: int = 10, mode: str = "PAPER") -> str:
    """List the most recent trades (default: 10 most recent PAPER trades).

    mode must be 'PAPER', 'LIVE', or 'ALL'. Returns a JSON array, newest first.
    """
    mode = mode.upper()
    if mode not in ("PAPER", "LIVE", "ALL"):
        raise ValueError("mode must be 'PAPER', 'LIVE', or 'ALL'.")
    limit = max(1, min(int(limit), 500))  # clamp to a sane range
    where = "" if mode == "ALL" else f" WHERE mode = '{mode}'"
    sql = (
        "SELECT id, date(entry_ts) AS day, symbol, signal_type, mode, "
        "entry_price, exit_price, exit_reason, pnl "
        f"FROM trades{where} ORDER BY id DESC LIMIT {limit}"
    )
    rows = _run_remote_sql(sql)
    if not rows:
        return "No rows."
    return json.dumps(rows, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
