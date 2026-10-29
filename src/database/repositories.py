"""Repositories: every SQL statement in the application lives here.

All queries use parameter binding. No user input is ever interpolated into SQL.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from config.settings import TerminalSettings
from src.data.financial_parser import PeriodData, ReportedValue
from src.data.validation import RawFinancialRow


def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _date(value: Optional[str]) -> Optional[_dt.date]:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _loads(value: Optional[str], default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Companies + statements
# --------------------------------------------------------------------------- #


class CompanyRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(
        self,
        ticker: str,
        name: Optional[str] = None,
        sector: Optional[str] = None,
        subsector: Optional[str] = None,
        shares_outstanding: Optional[float] = None,
        source_url: Optional[str] = None,
        is_synthetic: bool = False,
        notes: Optional[str] = None,
        listing_date: Optional[_dt.date] = None,
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO companies (ticker, name, sector, subsector, listing_date,
                                   shares_outstanding, currency, is_synthetic, source_url,
                                   notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'IDR', ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name = COALESCE(excluded.name, companies.name),
                sector = COALESCE(excluded.sector, companies.sector),
                subsector = COALESCE(excluded.subsector, companies.subsector),
                listing_date = COALESCE(excluded.listing_date, companies.listing_date),
                shares_outstanding = COALESCE(excluded.shares_outstanding, companies.shares_outstanding),
                is_synthetic = MAX(companies.is_synthetic, excluded.is_synthetic),
                source_url = COALESCE(excluded.source_url, companies.source_url),
                notes = COALESCE(excluded.notes, companies.notes),
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                name,
                sector,
                subsector,
                listing_date.isoformat() if listing_date else None,
                shares_outstanding,
                int(is_synthetic),
                source_url,
                notes,
                now,
                now,
            ),
        )

    def get(self, ticker: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM companies WHERE ticker = ?", (ticker,)).fetchone()
        return dict(row) if row else None

    def list(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM companies ORDER BY ticker").fetchall()
        return [dict(row) for row in rows]

    def tickers(self) -> List[str]:
        return [row["ticker"] for row in self.conn.execute("SELECT ticker FROM companies ORDER BY ticker")]

    def by_sector(self, sector: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM companies WHERE sector = ? ORDER BY ticker", (sector,)).fetchall()
        return [dict(row) for row in rows]

    def sectors(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT sector FROM companies WHERE sector IS NOT NULL AND sector != '' ORDER BY sector"
        ).fetchall()
        return [row["sector"] for row in rows]

    def delete(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM companies WHERE ticker = ?", (ticker,))

    def set_shares(self, ticker: str, shares: float) -> None:
        self.conn.execute(
            "UPDATE companies SET shares_outstanding = ?, updated_at = ? WHERE ticker = ?",
            (shares, _now(), ticker),
        )


class FinancialRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.companies = CompanyRepository(conn)

    # ------------------------------- writes ------------------------------- #
    def save_periods(self, periods: Sequence[PeriodData]) -> int:
        saved = 0
        now = _now()
        for period in periods:
            self.companies.upsert(
                ticker=period.ticker,
                name=period.company_name,
                sector=period.sector,
            )
            self.conn.execute(
                """
                INSERT INTO periods (ticker, period_end, period_type, period_basis,
                                     is_consolidated, fiscal_year, publication_date,
                                     retrieved_at, source_files, source_urls, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, period_end, period_type, is_consolidated) DO UPDATE SET
                    period_basis = excluded.period_basis,
                    fiscal_year = excluded.fiscal_year,
                    publication_date = COALESCE(excluded.publication_date, periods.publication_date),
                    retrieved_at = COALESCE(excluded.retrieved_at, periods.retrieved_at),
                    source_files = excluded.source_files,
                    source_urls = excluded.source_urls,
                    notes = COALESCE(excluded.notes, periods.notes)
                """,
                (
                    period.ticker,
                    period.period_end.isoformat(),
                    period.period_type,
                    period.period_basis,
                    int(period.is_consolidated),
                    period.fiscal_year,
                    period.publication_date.isoformat() if period.publication_date else None,
                    period.retrieved_at.isoformat() if period.retrieved_at else None,
                    json.dumps(period.source_files),
                    json.dumps(period.source_urls),
                    period.notes,
                    now,
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM periods WHERE ticker = ? AND period_end = ? "
                "AND period_type = ? AND is_consolidated = ?",
                (period.ticker, period.period_end.isoformat(), period.period_type, int(period.is_consolidated)),
            ).fetchone()
            period_id = row["id"]
            self.conn.execute("DELETE FROM financial_items WHERE period_id = ?", (period_id,))
            payload = []
            for item, value in period.values.items():
                reported = period.reported.get(item)
                payload.append(
                    (
                        period_id,
                        item,
                        reported.value if reported and reported.value is not None else value,
                        value,
                        reported.unit if reported else "rupiah",
                        reported.currency if reported else "IDR",
                        int(item in period.derived),
                        reported.source_url if reported else None,
                        reported.notes if reported else None,
                    )
                )
            self.conn.executemany(
                """
                INSERT INTO financial_items (period_id, line_item, value, value_idr, unit,
                                             currency, is_derived, source_url, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            saved += 1
        return saved

    def save_rows(self, rows: Sequence[RawFinancialRow]) -> int:
        from src.data.financial_parser import build_periods

        return self.save_periods(build_periods(rows))

    def delete_period(self, ticker: str, period_end: _dt.date, period_type: str, consolidated: bool) -> None:
        self.conn.execute(
            "DELETE FROM periods WHERE ticker = ? AND period_end = ? AND period_type = ? AND is_consolidated = ?",
            (ticker, period_end.isoformat(), period_type, int(consolidated)),
        )

    def delete_ticker(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM periods WHERE ticker = ?", (ticker,))
        self.conn.execute("DELETE FROM companies WHERE ticker = ?", (ticker,))

    # ------------------------------- reads -------------------------------- #
    def periods(self, ticker: str) -> List[PeriodData]:
        rows = self.conn.execute(
            """
            SELECT p.*, c.name AS company_name, c.sector AS sector
            FROM periods p LEFT JOIN companies c ON c.ticker = p.ticker
            WHERE p.ticker = ?
            ORDER BY p.period_end DESC
            """,
            (ticker,),
        ).fetchall()
        return [self._hydrate(row) for row in rows]

    def periods_for_tickers(self, tickers: Sequence[str]) -> Dict[str, List[PeriodData]]:
        return {ticker: self.periods(ticker) for ticker in tickers}

    def latest_period(self, ticker: str, period_type: Optional[str] = None) -> Optional[PeriodData]:
        periods = self.periods(ticker)
        if period_type:
            periods = [p for p in periods if p.period_type == period_type]
        return periods[0] if periods else None

    def all_tickers_with_data(self) -> List[str]:
        rows = self.conn.execute("SELECT DISTINCT ticker FROM periods ORDER BY ticker").fetchall()
        return [row["ticker"] for row in rows]

    def item_history(self, ticker: str, line_item: str, consolidated: bool = True) -> pd.DataFrame:
        rows = self.conn.execute(
            """
            SELECT p.period_end, p.period_type, p.publication_date, fi.value_idr AS value
            FROM financial_items fi
            JOIN periods p ON p.id = fi.period_id
            WHERE p.ticker = ? AND fi.line_item = ? AND p.is_consolidated = ?
            ORDER BY p.period_end
            """,
            (ticker, line_item, int(consolidated)),
        ).fetchall()
        return pd.DataFrame([dict(row) for row in rows], columns=["period_end", "period_type", "publication_date", "value"])

    def _hydrate(self, row: sqlite3.Row) -> PeriodData:
        period = PeriodData(
            ticker=row["ticker"],
            period_end=_date(row["period_end"]) or _dt.date(1900, 1, 1),
            period_type=row["period_type"],
            period_basis=row["period_basis"],
            is_consolidated=bool(row["is_consolidated"]),
            fiscal_year=row["fiscal_year"],
            company_name=row["company_name"],
            sector=row["sector"],
            publication_date=_date(row["publication_date"]),
            retrieved_at=None,
            source_files=_loads(row["source_files"], []),
            source_urls=_loads(row["source_urls"], []),
            notes=row["notes"],
        )
        items = self.conn.execute(
            "SELECT * FROM financial_items WHERE period_id = ?", (row["id"],)
        ).fetchall()
        for item in items:
            period.values[item["line_item"]] = item["value_idr"]
            period.reported[item["line_item"]] = ReportedValue(
                line_item=item["line_item"],
                value=item["value"],
                unit=item["unit"],
                currency=item["currency"],
                value_idr=item["value_idr"],
                source_url=item["source_url"],
                notes=item["notes"],
            )
            if item["is_derived"]:
                period.derived.add(item["line_item"])
        return period


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


class PriceRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert_many(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        payload = []
        for record in frame.to_dict("records"):
            payload.append(
                (
                    str(record["ticker"]).upper(),
                    pd.Timestamp(record["date"]).date().isoformat(),
                    float(record["close"]),
                    float(record["volume"]) if pd.notna(record.get("volume")) else None,
                    float(record["value"]) if pd.notna(record.get("value")) else None,
                    record.get("source_file"),
                )
            )
        self.conn.executemany(
            """
            INSERT INTO prices (ticker, date, close, volume, value, source_file)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                close = excluded.close,
                volume = excluded.volume,
                value = excluded.value,
                source_file = excluded.source_file
            """,
            payload,
        )
        return len(payload)

    def series(self, ticker: str) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT date, close, volume, value FROM prices WHERE ticker = ? ORDER BY date", (ticker,)
        ).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=["date", "close", "volume", "value"])
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def latest(self, ticker: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT date, close, volume, value FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return None
        return {
            "date": _date(row["date"]),
            "close": row["close"],
            "volume": row["volume"],
            "value": row["value"],
        }

    def price_on_or_before(self, ticker: str, when: _dt.date) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT date, close FROM prices WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (ticker, when.isoformat()),
        ).fetchone()
        if not row:
            return None
        return {"date": _date(row["date"]), "close": row["close"]}

    def average_daily_value(self, ticker: str, days: int = 30) -> Optional[float]:
        rows = self.conn.execute(
            "SELECT date, close, volume, value FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT ?",
            (ticker, days),
        ).fetchall()
        if not rows:
            return None
        values = []
        for row in rows:
            if row["value"] is not None:
                values.append(float(row["value"]))
            elif row["volume"] is not None and row["close"] is not None:
                values.append(float(row["volume"]) * float(row["close"]))
        if not values:
            return None
        return sum(values) / len(values)

    def coverage(self, ticker: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT MIN(date) AS start, MAX(date) AS end, COUNT(*) AS n FROM prices WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return {"start": _date(row["start"]), "end": _date(row["end"]), "observations": row["n"]}

    def all_series(self) -> pd.DataFrame:
        rows = self.conn.execute("SELECT ticker, date, close FROM prices ORDER BY ticker, date").fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=["ticker", "date", "close"])
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def delete_ticker(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM prices WHERE ticker = ?", (ticker,))


class BenchmarkRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, label: str, date: _dt.date, close: float) -> None:
        self.conn.execute(
            "INSERT INTO benchmark_prices (label, date, close) VALUES (?, ?, ?) "
            "ON CONFLICT(label, date) DO UPDATE SET close = excluded.close",
            (label, date.isoformat(), float(close)),
        )

    def series(self, label: str = "IHSG") -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT date, close FROM benchmark_prices WHERE label = ? ORDER BY date", (label,)
        ).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=["date", "close"])
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def labels(self) -> List[str]:
        return [row["label"] for row in self.conn.execute("SELECT DISTINCT label FROM benchmark_prices ORDER BY label")]


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


class WatchlistRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, ticker: str, notes: str = "", target_margin_of_safety: Optional[float] = None) -> None:
        self.conn.execute(
            "INSERT INTO watchlist (ticker, added_at, target_margin_of_safety, notes) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
            "notes = COALESCE(NULLIF(excluded.notes, ''), watchlist.notes), "
            "target_margin_of_safety = COALESCE(excluded.target_margin_of_safety, watchlist.target_margin_of_safety)",
            (ticker.upper(), _now(), target_margin_of_safety, notes),
        )

    def remove(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))

    def set_alerts(
        self, ticker: str, price_above: Optional[float], price_below: Optional[float]
    ) -> None:
        self.conn.execute(
            "UPDATE watchlist SET alert_price_above = ?, alert_price_below = ? WHERE ticker = ?",
            (price_above, price_below, ticker.upper()),
        )

    def list(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT w.*, c.name, c.sector FROM watchlist w
            LEFT JOIN companies c ON c.ticker = w.ticker
            ORDER BY w.added_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def contains(self, ticker: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM watchlist WHERE ticker = ?", (ticker.upper(),)).fetchone()
        return row is not None


# --------------------------------------------------------------------------- #
# Paper portfolio
# --------------------------------------------------------------------------- #


class PortfolioRepository:
    DEFAULT_NAME = "Main paper portfolio"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ensure(self, initial_cash: float = 100_000_000.0) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM portfolios WHERE name = ?", (self.DEFAULT_NAME,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO portfolios (name, initial_cash, cash, created_at) VALUES (?, ?, ?, ?)",
                (self.DEFAULT_NAME, initial_cash, initial_cash, _now()),
            )
            row = self.conn.execute("SELECT * FROM portfolios WHERE name = ?", (self.DEFAULT_NAME,)).fetchone()
        return dict(row)

    def get(self, portfolio_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
        return dict(row) if row else {}

    def update_cash(self, portfolio_id: int, cash: float) -> None:
        self.conn.execute("UPDATE portfolios SET cash = ? WHERE id = ?", (cash, portfolio_id))

    def reset(self, portfolio_id: int, initial_cash: float) -> None:
        self.conn.execute("DELETE FROM transactions WHERE portfolio_id = ?", (portfolio_id,))
        self.conn.execute("DELETE FROM equity_snapshots WHERE portfolio_id = ?", (portfolio_id,))
        self.conn.execute(
            "UPDATE portfolios SET cash = ?, initial_cash = ? WHERE id = ?",
            (initial_cash, initial_cash, portfolio_id),
        )

    def add_transaction(self, portfolio_id: int, **payload) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO transactions (portfolio_id, ticker, side, trade_date, price, lots,
                                      shares, gross, fees, net_cash, realized_pnl, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                portfolio_id,
                payload["ticker"],
                payload["side"],
                payload["trade_date"],
                payload["price"],
                payload["lots"],
                payload["shares"],
                payload["gross"],
                payload["fees"],
                payload["net_cash"],
                payload.get("realized_pnl"),
                payload.get("note"),
                _now(),
            ),
        )
        return int(cursor.lastrowid)

    def transactions(self, portfolio_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE portfolio_id = ? ORDER BY trade_date, id",
            (portfolio_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_transaction(self, portfolio_id: int, transaction_id: int) -> None:
        self.conn.execute(
            "DELETE FROM transactions WHERE id = ? AND portfolio_id = ?", (transaction_id, portfolio_id)
        )

    def save_snapshot(self, portfolio_id: int, date: _dt.date, cash: float, market_value: float) -> None:
        self.conn.execute(
            "INSERT INTO equity_snapshots (portfolio_id, snapshot_date, cash, market_value, total_value) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(portfolio_id, snapshot_date) DO UPDATE SET "
            "cash = excluded.cash, market_value = excluded.market_value, total_value = excluded.total_value",
            (portfolio_id, date.isoformat(), cash, market_value, cash + market_value),
        )

    def snapshots(self, portfolio_id: int) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT snapshot_date, cash, market_value, total_value FROM equity_snapshots "
            "WHERE portfolio_id = ? ORDER BY snapshot_date",
            (portfolio_id,),
        ).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=["snapshot_date", "cash", "market_value", "total_value"])
        if not frame.empty:
            frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"])
        return frame


# --------------------------------------------------------------------------- #
# Notes, recommendations, valuations, presets, settings, import log
# --------------------------------------------------------------------------- #


class NotesRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, ticker: str, title: str, body: str, tags: str = "") -> int:
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO research_notes (ticker, title, body, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker.upper(), title, body, tags, now, now),
        )
        return int(cursor.lastrowid)

    def update(self, note_id: int, title: str, body: str, tags: str = "") -> None:
        self.conn.execute(
            "UPDATE research_notes SET title = ?, body = ?, tags = ?, updated_at = ? WHERE id = ?",
            (title, body, tags, _now(), note_id),
        )

    def delete(self, note_id: int) -> None:
        self.conn.execute("DELETE FROM research_notes WHERE id = ?", (note_id,))

    def for_ticker(self, ticker: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM research_notes WHERE ticker = ? ORDER BY updated_at DESC", (ticker.upper(),)
        ).fetchall()
        return [dict(row) for row in rows]

    def all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM research_notes ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]


class RecommendationRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, payload: Dict[str, Any]) -> int:
        cursor = self.conn.execute(
            "INSERT INTO recommendations (ticker, created_at, decision, score, confidence, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload.get("ticker"),
                payload.get("as_of", _now()),
                payload.get("decision"),
                payload.get("score"),
                payload.get("confidence"),
                json.dumps(payload, default=str),
            ),
        )
        return int(cursor.lastrowid)

    def latest(self, ticker: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT payload FROM recommendations WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        return _loads(row["payload"], None) if row else None

    def history(self, ticker: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM recommendations WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return [payload for payload in (_loads(row["payload"], None) for row in rows) if payload]


class ValuationRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, ticker: str, method: str, fair_value: Optional[float], assumptions: Dict, result: Dict) -> int:
        cursor = self.conn.execute(
            "INSERT INTO valuation_runs (ticker, created_at, method, fair_value, assumptions, result) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker.upper(), _now(), method, fair_value, json.dumps(assumptions, default=str), json.dumps(result, default=str)),
        )
        return int(cursor.lastrowid)

    def history(self, ticker: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, created_at, method, fair_value, assumptions, result FROM valuation_runs "
            "WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return [dict(row) for row in rows]


class PresetRepository:
    DEFAULTS: Dict[str, Dict[str, Any]] = {
        "Quality companies": {"min_roe": 0.15, "min_roic": 0.10, "min_operating_margin": 0.10},
        "Undervalued companies": {"min_margin_of_safety": 0.15, "max_pe": 15},
        "Growing companies": {"min_revenue_growth": 0.10, "min_revenue_cagr_3y": 0.08},
        "Low-debt companies": {"max_net_debt_ebitda": 1.5, "max_de": 0.75},
        "Industrial and manufacturing": {"sectors": ["Industrials", "Basic Materials"], "min_roic": 0.08},
    }

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def seed_defaults(self) -> None:
        for name, payload in self.DEFAULTS.items():
            self.conn.execute(
                "INSERT INTO screener_presets (name, payload, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, json.dumps(payload), _now()),
            )

    def save(self, name: str, payload: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO screener_presets (name, payload, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload",
            (name, json.dumps(payload), _now()),
        )

    def list(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT name, payload FROM screener_presets ORDER BY name").fetchall()
        return [{"name": row["name"], "payload": _loads(row["payload"], {})} for row in rows]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT payload FROM screener_presets WHERE name = ?", (name,)).fetchone()
        return _loads(row["payload"], {}) if row else None

    def delete(self, name: str) -> None:
        self.conn.execute("DELETE FROM screener_presets WHERE name = ?", (name,))


class SettingsRepository:
    KEY = "terminal_settings"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> TerminalSettings:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (self.KEY,)).fetchone()
        return TerminalSettings.from_dict(_loads(row["value"], {}) if row else {})

    def save(self, settings: TerminalSettings) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (self.KEY, json.dumps(asdict(settings)), _now()),
        )

    def reset(self) -> TerminalSettings:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (self.KEY,))
        return TerminalSettings()


class ImportLogRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(
        self,
        filename: str,
        tickers: Sequence[str],
        rows: int,
        errors: int,
        warnings: int,
        ok: bool,
        detail: str = "",
        sha256: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO import_log (filename, sha256, tickers, rows, errors, warnings, ok, detail, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, sha256, ",".join(tickers), rows, errors, warnings, int(ok), detail, _now()),
        )

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM import_log ORDER BY imported_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


class LiveRepository:
    """Symbol map and last-known-good quotes for the delayed IDX feed."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def seed_defaults(self, entries: Optional[Sequence[tuple]] = None) -> int:
        if entries is None:
            from src.data.live import DEFAULT_FEED

            entries = DEFAULT_FEED
        now = _now()
        inserted = 0
        for order, (ticker, symbol) in enumerate(entries):
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO live_symbols "
                "(ticker, symbol, vendor, enabled, sort_order, added_at) VALUES (?, ?, 'yahoo', 1, ?, ?)",
                (str(ticker).upper(), str(symbol).upper(), order, now),
            )
            inserted += max(cursor.rowcount, 0)
        return inserted

    def symbols(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        clause = "WHERE enabled = 1" if enabled_only else ""
        rows = self.conn.execute(
            f"SELECT * FROM live_symbols {clause} ORDER BY sort_order, ticker"
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_symbol(
        self, ticker: str, symbol: Optional[str] = None, name: Optional[str] = None, enabled: bool = True
    ) -> None:
        from src.data.live import yahoo_symbol

        clean = str(ticker).strip().upper()
        vendor_symbol = (symbol or yahoo_symbol(clean)).strip().upper()
        self.conn.execute(
            "INSERT INTO live_symbols (ticker, symbol, name, vendor, enabled, sort_order, added_at) "
            "VALUES (?, ?, ?, 'yahoo', ?, 500, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "symbol = excluded.symbol, "
            "name = COALESCE(excluded.name, live_symbols.name), "
            "enabled = excluded.enabled",
            (clean, vendor_symbol, name, int(enabled), _now()),
        )

    def set_enabled(self, ticker: str, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE live_symbols SET enabled = ? WHERE ticker = ?", (int(enabled), ticker.upper())
        )

    def remove(self, ticker: str) -> None:
        clean = ticker.upper()
        self.conn.execute("DELETE FROM live_quotes WHERE ticker = ?", (clean,))
        self.conn.execute("DELETE FROM live_symbols WHERE ticker = ?", (clean,))

    def save_quotes(self, quotes: Sequence[Any]) -> int:
        """Persist last-known-good quotes. Failed quotes never overwrite a price."""
        saved = 0
        for quote in quotes:
            if not getattr(quote, "ok", False):
                continue
            payload = quote.as_dict()
            self.conn.execute(
                """
                INSERT INTO live_quotes (
                    symbol, ticker, name, currency, exchange, price, previous_close, change,
                    change_percent, day_high, day_low, week52_high, week52_low, volume,
                    market_state, quote_time, retrieved_at, source_url, delayed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    ticker = excluded.ticker,
                    name = excluded.name,
                    currency = excluded.currency,
                    exchange = excluded.exchange,
                    price = excluded.price,
                    previous_close = excluded.previous_close,
                    change = excluded.change,
                    change_percent = excluded.change_percent,
                    day_high = excluded.day_high,
                    day_low = excluded.day_low,
                    week52_high = excluded.week52_high,
                    week52_low = excluded.week52_low,
                    volume = excluded.volume,
                    market_state = excluded.market_state,
                    quote_time = excluded.quote_time,
                    retrieved_at = excluded.retrieved_at,
                    source_url = excluded.source_url,
                    delayed = excluded.delayed
                """,
                (
                    payload["symbol"], payload["ticker"], payload["name"], payload["currency"],
                    payload["exchange"], payload["price"], payload["previous_close"], payload["change"],
                    payload["change_percent"], payload["day_high"], payload["day_low"],
                    payload["week52_high"], payload["week52_low"], payload["volume"],
                    payload["market_state"], payload["quote_time"], payload["retrieved_at"],
                    payload["source_url"], payload["delayed"],
                ),
            )
            self.conn.execute(
                "UPDATE live_symbols SET last_sync_at = ?, name = COALESCE(NULLIF(name, ''), ?) "
                "WHERE ticker = ?",
                (payload["retrieved_at"], payload["name"], payload["ticker"]),
            )
            saved += 1
        return saved

    def latest(self) -> Dict[str, Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM live_quotes").fetchall()
        return {row["ticker"]: dict(row) for row in rows}

    def latest_for(self, ticker: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM live_quotes WHERE ticker = ?", (ticker.upper(),)
        ).fetchone()
        return dict(row) if row else None

    def last_retrieved(self) -> Optional[str]:
        row = self.conn.execute("SELECT MAX(retrieved_at) AS ts FROM live_quotes").fetchone()
        return row["ts"] if row and row["ts"] else None


class Repositories:
    """Convenience bundle bound to one connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.companies = CompanyRepository(conn)
        self.financials = FinancialRepository(conn)
        self.prices = PriceRepository(conn)
        self.benchmarks = BenchmarkRepository(conn)
        self.watchlist = WatchlistRepository(conn)
        self.portfolio = PortfolioRepository(conn)
        self.notes = NotesRepository(conn)
        self.recommendations = RecommendationRepository(conn)
        self.valuations = ValuationRepository(conn)
        self.presets = PresetRepository(conn)
        self.settings = SettingsRepository(conn)
        self.imports = ImportLogRepository(conn)
        self.live = LiveRepository(conn)
