"""SQLite persistence layer.

A single local database file holds companies, statements, prices, watchlist,
paper portfolio, notes, saved valuations and settings. All access goes through
parameterized queries in ``repositories.py``; this module only owns connection
handling, schema creation and backups.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from config.settings import BACKUP_DIR, DB_PATH

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    ticker            TEXT PRIMARY KEY,
    name              TEXT,
    sector            TEXT,
    subsector         TEXT,
    listing_date      TEXT,
    shares_outstanding REAL,
    currency          TEXT DEFAULT 'IDR',
    is_synthetic      INTEGER DEFAULT 0,
    source_url        TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL REFERENCES companies(ticker) ON DELETE CASCADE,
    period_end        TEXT NOT NULL,
    period_type       TEXT NOT NULL CHECK (period_type IN ('annual', 'quarter')),
    period_basis      TEXT NOT NULL DEFAULT 'cumulative',
    is_consolidated   INTEGER NOT NULL DEFAULT 1,
    fiscal_year       INTEGER NOT NULL,
    publication_date  TEXT,
    retrieved_at      TEXT,
    source_files      TEXT NOT NULL DEFAULT '[]',
    source_urls       TEXT NOT NULL DEFAULT '[]',
    notes             TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (ticker, period_end, period_type, is_consolidated)
);

CREATE INDEX IF NOT EXISTS idx_periods_ticker_date ON periods (ticker, period_end DESC);

CREATE TABLE IF NOT EXISTS financial_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id   INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    line_item   TEXT NOT NULL,
    value       REAL,
    value_idr   REAL,
    unit        TEXT NOT NULL DEFAULT 'rupiah',
    currency    TEXT NOT NULL DEFAULT 'IDR',
    is_derived  INTEGER NOT NULL DEFAULT 0,
    source_url  TEXT,
    notes       TEXT,
    UNIQUE (period_id, line_item)
);

CREATE INDEX IF NOT EXISTS idx_items_period ON financial_items (period_id);

CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    close       REAL NOT NULL,
    volume      REAL,
    value       REAL,
    source_file TEXT,
    UNIQUE (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON prices (ticker, date DESC);

CREATE TABLE IF NOT EXISTS benchmark_prices (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    date  TEXT NOT NULL,
    close REAL NOT NULL,
    UNIQUE (label, date)
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker            TEXT PRIMARY KEY,
    added_at          TEXT NOT NULL,
    target_margin_of_safety REAL,
    alert_price_above REAL,
    alert_price_below REAL,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS portfolios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    initial_cash REAL NOT NULL,
    cash         REAL NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id   INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    ticker         TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    trade_date     TEXT NOT NULL,
    price          REAL NOT NULL,
    lots           INTEGER NOT NULL,
    shares         INTEGER NOT NULL,
    gross          REAL NOT NULL,
    fees           REAL NOT NULL,
    net_cash       REAL NOT NULL,
    realized_pnl   REAL,
    note           TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_portfolio ON transactions (portfolio_id, trade_date);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    snapshot_date TEXT NOT NULL,
    cash         REAL NOT NULL,
    market_value REAL NOT NULL,
    total_value  REAL NOT NULL,
    UNIQUE (portfolio_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS research_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_ticker ON research_notes (ticker, updated_at DESC);

CREATE TABLE IF NOT EXISTS recommendations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decision   TEXT NOT NULL,
    score      REAL,
    confidence REAL,
    payload    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recs_ticker ON recommendations (ticker, created_at DESC);

CREATE TABLE IF NOT EXISTS valuation_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    method     TEXT NOT NULL,
    fair_value REAL,
    assumptions TEXT NOT NULL,
    result     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screener_presets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    sha256      TEXT,
    tickers     TEXT,
    rows        INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    warnings    INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_symbols (
    ticker       TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL UNIQUE,
    name         TEXT,
    vendor       TEXT NOT NULL DEFAULT 'yahoo',
    enabled      INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 100,
    added_at     TEXT NOT NULL,
    last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS live_quotes (
    symbol         TEXT PRIMARY KEY,
    ticker         TEXT NOT NULL,
    name           TEXT,
    currency       TEXT,
    exchange       TEXT,
    price          REAL,
    previous_close REAL,
    change         REAL,
    change_percent REAL,
    day_high       REAL,
    day_low        REAL,
    week52_high    REAL,
    week52_low     REAL,
    volume         REAL,
    market_state   TEXT,
    quote_time     TEXT,
    retrieved_at   TEXT NOT NULL,
    source_url     TEXT,
    delayed        INTEGER NOT NULL DEFAULT 1
);
"""


def _utcnow() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def connect(path: Optional[Path] = None, readonly: bool = False, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with sane PRAGMAs and row access by name."""
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=30, check_same_thread=check_same_thread)
    else:
        conn = sqlite3.connect(str(target), timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            pass
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None, path: Optional[Path] = None) -> sqlite3.Connection:
    owns = conn is None
    connection = conn or connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('initialized_at', ?) ON CONFLICT(key) DO NOTHING",
        (_utcnow(),),
    )
    connection.commit()
    if owns:
        return connection
    return connection


@contextmanager
def session(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection context manager."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def backup_database(conn: sqlite3.Connection, label: str = "manual") -> Path:
    """Copy the live database to ``data/backups`` using SQLite's backup API."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_label = "".join(ch for ch in label if ch.isalnum() or ch in "-_") or "manual"
    target = BACKUP_DIR / f"terminal-{stamp}-{safe_label}.db"
    destination = sqlite3.connect(str(target))
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return target


def restore_database(backup_path: Path, target: Optional[Path] = None) -> Path:
    """Replace the working database with a backup, keeping a safety copy.

    The copy is written through SQLite's backup API rather than by replacing the
    file, so an open WAL connection cannot be corrupted by a stale ``-wal``
    sidecar and Windows file locks are avoided.
    """
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(backup_path)
    target_path = Path(target) if target else DB_PATH

    if target_path.exists():
        safety = connect(target_path)
        try:
            backup_database(safety, label="pre-restore")
        finally:
            safety.close()

    source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True, timeout=30)
    destination = connect(target_path)
    try:
        source.backup(destination)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        source.close()
        destination.close()
    return target_path


def database_info(conn: sqlite3.Connection) -> dict:
    info = {"path": str(DB_PATH), "exists": DB_PATH.exists()}
    for table in (
        "companies", "periods", "financial_items", "prices", "watchlist",
        "transactions", "research_notes", "recommendations",
    ):
        try:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            info[table] = row["n"]
        except sqlite3.DatabaseError:
            info[table] = None
    info["schema_version"] = SCHEMA_VERSION
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    info["size_bytes"] = size
    return info
