"""CSV import for financial statements, prices and company metadata.

Two statement layouts are supported.

Long format (recommended - one row per line item)::

    ticker,period_end,period_type,statement,line_item,value,unit,currency,...
    BBCA,2023-12-31,annual,income,revenue,102300000000000,rupiah,IDR,...

Wide format (one row per period, one column per line item)::

    ticker,period_end,period_type,unit,revenue,cost_of_revenue,net_income,...
    BBCA,2023-12-31,annual,million,102300000,...
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from pydantic import ValidationError

from src.data.normalization import (
    ITEM_STATEMENT,
    STATEMENTS,
    canonicalize_line_item,
    detect_unit_from_header,
    normalize_period_basis,
    normalize_period_type,
    normalize_ticker,
    parse_number,
    parse_period_end,
)
from src.data.validation import RawFinancialRow, ValidationReport, validate_dataset

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ROWS = 200_000

COLUMN_ALIASES = {
    "ticker": {"ticker", "symbol", "code", "kode", "kode_saham", "emiten", "stock"},
    "company_name": {"company", "company_name", "name", "nama", "nama_perusahaan", "issuer"},
    "sector": {"sector", "sektor", "industry", "industri", "sub_sector"},
    "period_end": {"period_end", "period", "date", "reporting_date", "tanggal", "periode", "period_ending"},
    "period_type": {"period_type", "frequency", "type", "periode_type", "jenis_periode"},
    "period_basis": {"period_basis", "basis", "cumulative", "ytd"},
    "statement": {"statement", "statement_type", "laporan", "jenis_laporan"},
    "line_item": {"line_item", "item", "account", "akun", "pos", "metric", "label"},
    "value": {"value", "amount", "nilai", "jumlah", "figure"},
    "unit": {"unit", "satuan", "scale", "units"},
    "currency": {"currency", "mata_uang", "ccy"},
    "is_consolidated": {"is_consolidated", "consolidated", "konsolidasi", "basis_laporan", "scope"},
    "source_url": {"source_url", "url", "source", "sumber", "link"},
    "publication_date": {"publication_date", "published", "filing_date", "tanggal_publikasi", "tanggal_rilis"},
    "retrieved_at": {"retrieved_at", "retrieved", "downloaded_at"},
    "notes": {"notes", "note", "comment", "catatan", "keterangan"},
}

REQUIRED_LONG = ("ticker", "period_end", "line_item", "value")
META_COLUMNS = set().union(*COLUMN_ALIASES.values()) | {"row"}


class ImportError_(Exception):
    """Raised for unreadable files; per-row problems are collected instead."""


@dataclass
class ImportIssue:
    row: Optional[int]
    message: str
    severity: str = "error"

    def as_dict(self) -> Dict[str, object]:
        return {"row": self.row, "severity": self.severity, "message": self.message}


@dataclass
class ImportResult:
    rows: List[RawFinancialRow] = field(default_factory=list)
    report: ValidationReport = field(default_factory=ValidationReport)
    issues: List[ImportIssue] = field(default_factory=list)
    source: str = ""
    layout: str = ""
    metadata: Dict[str, Optional[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and not any(i.severity == "error" for i in self.issues) and self.report.ok

    @property
    def tickers(self) -> List[str]:
        return sorted({row.ticker for row in self.rows})

    @property
    def period_range(self) -> Tuple[Optional[_dt.date], Optional[_dt.date]]:
        if not self.rows:
            return None, None
        dates = [row.period_end for row in self.rows]
        return min(dates), max(dates)

    def to_frame(self):
        return pd.DataFrame(
            [
                {
                    "row": i.row,
                    "severity": i.severity,
                    "message": i.message,
                }
                for i in self.issues
            ]
        )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _read_bytes(source) -> bytes:
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data
    path = Path(source)
    if not path.exists():
        raise ImportError_(f"File not found: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ImportError_(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB: {path.name}")
    return path.read_bytes()


def read_csv(source) -> pd.DataFrame:
    """Read a CSV with encoding fallbacks and CSV-injection-safe parsing.

    Values are always parsed as strings; nothing is evaluated.
    """
    data = _read_bytes(source)
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, na_filter=False)
            break
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            last_error = exc
            continue
    else:  # pragma: no cover - latin-1 decodes almost anything
        raise ImportError_(f"Could not parse CSV: {last_error}")
    if len(frame) > MAX_ROWS:
        raise ImportError_(f"CSV has {len(frame):,} rows; the limit is {MAX_ROWS:,}.")
    frame.columns = [_clean_header(c) for c in frame.columns]
    return frame


def _clean_header(header: str) -> str:
    text = str(header).replace("\ufeff", "").strip().lower()
    text = text.replace(" ", "_").replace("-", "_").replace(".", "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _resolve_columns(frame: pd.DataFrame) -> Dict[str, str]:
    """Map actual column names onto canonical roles."""
    lookup: Dict[str, str] = {}
    for column in frame.columns:
        for role, aliases in COLUMN_ALIASES.items():
            if column in aliases:
                lookup[role] = column
                break
    return lookup


def detect_layout(frame: pd.DataFrame) -> str:
    columns = _resolve_columns(frame)
    if "line_item" in columns and "value" in columns:
        return "long"
    if "ticker" in columns and "period_end" in columns:
        return "wide"
    return "unknown"


# --------------------------------------------------------------------------- #
# Row construction
# --------------------------------------------------------------------------- #


def _optional_date(value: object) -> Optional[_dt.date]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_period_end(text)
    except ValueError:
        return None


def _optional_datetime(value: object) -> Optional[_dt.datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        date = _optional_date(text)
        return _dt.datetime.combine(date, _dt.time()) if date else None


def _truthy(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "n", "parent", "parent-only", "parent_only", "separate", "individual"}


def _build_row(payload: Dict[str, object]) -> RawFinancialRow:
    return RawFinancialRow(**payload)


def _long_rows(
    frame: pd.DataFrame, columns: Dict[str, str], source_name: str
) -> Tuple[List[RawFinancialRow], List[ImportIssue]]:
    rows: List[RawFinancialRow] = []
    issues: List[ImportIssue] = []
    missing = [role for role in REQUIRED_LONG if role not in columns]
    if missing:
        raise ImportError_(f"Long-format CSV is missing required columns: {', '.join(missing)}")

    for index, record in frame.iterrows():
        row_number = index + 2  # header offset
        raw_item = record.get(columns["line_item"], "")
        item, recognized = canonicalize_line_item(raw_item)
        if not item:
            issues.append(ImportIssue(row_number, "Empty line item skipped.", "warning"))
            continue
        statement = str(record.get(columns.get("statement", ""), "")).strip().lower() or ITEM_STATEMENT.get(item, "")
        if not recognized:
            issues.append(
                ImportIssue(
                    row_number,
                    f"Unrecognized line item {raw_item!r}; skipped to avoid contaminating analytics.",
                    "warning",
                )
            )
            continue
        if statement not in STATEMENTS:
            issues.append(ImportIssue(row_number, f"Unrecognized statement {statement!r}; skipped.", "warning"))
            continue

        value = parse_number(record.get(columns["value"]))
        payload = {
            "ticker": record.get(columns["ticker"]),
            "period_end": record.get(columns["period_end"]),
            "period_type": record.get(columns.get("period_type", ""), "annual") or "annual",
            "period_basis": record.get(columns.get("period_basis", ""), "cumulative") or "cumulative",
            "statement": statement,
            "line_item": item,
            "value": value,
            "unit": record.get(columns.get("unit", ""), "rupiah") or "rupiah",
            "currency": record.get(columns.get("currency", ""), "IDR") or "IDR",
            "is_consolidated": _truthy(record.get(columns.get("is_consolidated", ""))),
            "source_url": (record.get(columns.get("source_url", "")) or "").strip() or None,
            "source_file": source_name,
            "publication_date": _optional_date(record.get(columns.get("publication_date", ""))),
            "retrieved_at": _optional_datetime(record.get(columns.get("retrieved_at", ""))),
            "notes": (record.get(columns.get("notes", "")) or "").strip() or None,
            "company_name": (record.get(columns.get("company_name", "")) or "").strip() or None,
            "sector": (record.get(columns.get("sector", "")) or "").strip() or None,
        }
        try:
            rows.append(_build_row(payload))
        except (ValidationError, ValueError) as exc:
            issues.append(ImportIssue(row_number, _format_validation_error(exc), "error"))
    return rows, issues


def _wide_rows(
    frame: pd.DataFrame, columns: Dict[str, str], source_name: str
) -> Tuple[List[RawFinancialRow], List[ImportIssue]]:
    rows: List[RawFinancialRow] = []
    issues: List[ImportIssue] = []
    if "period_end" not in columns:
        raise ImportError_("Wide-format CSV is missing the period_end column.")

    item_columns: List[Tuple[str, str, Optional[str]]] = []
    for column in frame.columns:
        if column in META_COLUMNS:
            continue
        item, recognized = canonicalize_line_item(column)
        if not recognized:
            issues.append(
                ImportIssue(
                    None,
                    f"Column {column!r} does not match a known line item and was ignored.",
                    "warning",
                )
            )
            continue
        item_columns.append((column, item, detect_unit_from_header(column)))

    if not item_columns:
        raise ImportError_("Wide-format CSV has no recognised financial line-item columns.")

    for index, record in frame.iterrows():
        row_number = index + 2
        default_unit = str(record.get(columns.get("unit", ""), "") or "rupiah")
        period_supplied_unit = default_unit if default_unit.strip() else "rupiah"
        for column, item, header_unit in item_columns:
            raw_value = record.get(column)
            value = parse_number(raw_value)
            if value is None:
                continue
            payload = {
                "ticker": record.get(columns.get("ticker", ""), ""),
                "period_end": record.get(columns.get("period_end", ""), ""),
                "period_type": record.get(columns.get("period_type", ""), "annual") or "annual",
                "period_basis": record.get(columns.get("period_basis", ""), "cumulative") or "cumulative",
                "statement": ITEM_STATEMENT.get(item, "income"),
                "line_item": item,
                "value": value,
                "unit": header_unit or period_supplied_unit,
                "currency": record.get(columns.get("currency", ""), "IDR") or "IDR",
                "is_consolidated": _truthy(record.get(columns.get("is_consolidated", ""))),
                "source_url": (record.get(columns.get("source_url", "")) or "").strip() or None,
                "source_file": source_name,
                "publication_date": _optional_date(record.get(columns.get("publication_date", ""))),
                "retrieved_at": _optional_datetime(record.get(columns.get("retrieved_at", ""))),
                "notes": (record.get(columns.get("notes", "")) or "").strip() or None,
                "company_name": (record.get(columns.get("company_name", "")) or "").strip() or None,
                "sector": (record.get(columns.get("sector", "")) or "").strip() or None,
            }
            try:
                rows.append(_build_row(payload))
            except (ValidationError, ValueError) as exc:
                issues.append(ImportIssue(row_number, f"{column}: {_format_validation_error(exc)}", "error"))
    return rows, issues


def _format_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            parts.append(f"{location}: {error.get('msg')}")
        return "; ".join(parts)
    return str(exc)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def import_financials(source, source_name: Optional[str] = None, validate: bool = True) -> ImportResult:
    """Parse a CSV into validated rows plus a validation report."""
    name = source_name or getattr(source, "name", None) or str(source)
    frame = read_csv(source)
    if frame.empty:
        raise ImportError_(f"{name} contains no data rows.")
    columns = _resolve_columns(frame)
    layout = detect_layout(frame)
    if layout == "unknown":
        raise ImportError_(
            "Unrecognized CSV layout. Need either long format "
            "(ticker, period_end, line_item, value) or wide format (ticker, period_end, <line items>)."
        )

    if layout == "long":
        rows, issues = _long_rows(frame, columns, str(name))
    else:
        rows, issues = _wide_rows(frame, columns, str(name))

    report = validate_dataset(rows) if validate else ValidationReport()
    metadata = {}
    for row in rows:
        metadata.setdefault("company_name", row.company_name)
        metadata.setdefault("sector", row.sector)

    return ImportResult(
        rows=rows,
        report=report,
        issues=issues,
        source=str(name),
        layout=layout,
        metadata={k: v for k, v in metadata.items() if v},
    )


@dataclass
class PriceImportResult:
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    issues: List[ImportIssue] = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.frame.empty and not any(i.severity == "error" for i in self.issues)


def import_prices(source, source_name: Optional[str] = None) -> PriceImportResult:
    """Parse a price CSV: ``ticker, date, close`` with optional volume/value."""
    name = source_name or getattr(source, "name", None) or str(source)
    frame = read_csv(source)
    columns = _resolve_columns(frame)
    issues: List[ImportIssue] = []
    date_col = columns.get("period_end") or next((c for c in frame.columns if c in {"date", "tanggal"}), None)
    ticker_col = columns.get("ticker")
    close_col = next((c for c in frame.columns if c in {"close", "close_price", "price", "harga", "last"}), None)
    if not ticker_col or not date_col or not close_col:
        raise ImportError_("Price CSV needs ticker, date and close columns.")

    volume_col = next((c for c in frame.columns if c in {"volume", "vol", "volume_saham"}), None)
    value_col = next((c for c in frame.columns if c in {"value", "turnover", "nilai_transaksi"}), None)
    rows = []
    for index, record in frame.iterrows():
        row_number = index + 2
        try:
            ticker = normalize_ticker(record.get(ticker_col))
            date = parse_period_end(record.get(date_col))
            close = parse_number(record.get(close_col))
        except ValueError as exc:
            issues.append(ImportIssue(row_number, str(exc), "error"))
            continue
        if close is None or close <= 0:
            issues.append(ImportIssue(row_number, "Close price is missing or non-positive; row skipped.", "error"))
            continue
        rows.append(
            {
                "ticker": ticker,
                "date": pd.Timestamp(date),
                "close": float(close),
                "volume": parse_number(record.get(volume_col)) if volume_col else None,
                "value": parse_number(record.get(value_col)) if value_col else None,
                "source_file": str(name),
            }
        )
    if not rows:
        return PriceImportResult(frame=pd.DataFrame(), issues=issues, source=str(name))
    result = pd.DataFrame(rows).sort_values(["ticker", "date"]).drop_duplicates(["ticker", "date"], keep="last")
    return PriceImportResult(frame=result.reset_index(drop=True), issues=issues, source=str(name))


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

LONG_TEMPLATE_ROWS = [
    ("BBRI", "2023-12-31", "annual", "income", "revenue", 187000000000000, "rupiah", "IDR", True,
     "https://www.idx.co.id/", "2024-03-31", "Values are illustrative placeholders - replace with audited figures."),
    ("BBRI", "2023-12-31", "annual", "income", "cost_of_revenue", 0, "rupiah", "IDR", True,
     "https://www.idx.co.id/", "2024-03-31", "Banks often do not report a cost of revenue."),
    ("BBRI", "2023-12-31", "annual", "balance", "total_assets", 1990000000000000, "rupiah", "IDR", True,
     "https://www.idx.co.id/", "2024-03-31", ""),
    ("BBRI", "2024-03-31", "quarter", "income", "revenue", 51000000000000, "million", "IDR", True,
     "https://www.idx.co.id/", "2024-04-30", "Cumulative Q1 by default"),
]

PRICE_TEMPLATE_ROWS = [
    ("BBRI", "2024-01-02", 5700, 100_000_000, 570_000_000_000),
    ("BBRI", "2024-01-03", 5750, 90_000_000, 517_500_000_000),
]


def write_template(directory: Path, name: str = "financial_template.csv") -> Path:
    """Write the long-format template with documentation rows."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    header = [
        "ticker", "period_end", "period_type", "statement", "line_item", "value",
        "unit", "currency", "is_consolidated", "source_url", "publication_date", "notes",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(LONG_TEMPLATE_ROWS)
    return path


def write_wide_template(directory: Path, name: str = "financial_template_wide.csv") -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    header = [
        "ticker", "company_name", "sector", "period_end", "period_type", "unit", "currency",
        "is_consolidated", "source_url", "publication_date",
        "revenue", "cost_of_revenue", "operating_expenses", "depreciation_amortization",
        "interest_expense", "income_tax", "net_income", "net_income_parent",
        "eps", "shares_outstanding", "cash", "receivables", "inventory",
        "current_assets", "total_assets", "current_liabilities", "short_term_debt",
        "long_term_debt", "total_liabilities", "equity", "minority_interest",
        "cfo", "capex", "cfi", "cff", "dividends_paid",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerow(
            [
                "BBRI", "Bank Rakyat Indonesia (Persero) Tbk", "Financials", "2023-12-31", "annual",
                "million", "IDR", True, "https://www.idx.co.id/", "2024-03-31",
                187000000, 0, 40000000, 8000000, 22000000, 12000000, 60000000, 60000000,
                400.0, 151000000000, 250000000, 1200000000, 0, 2100000000, 1990000000000,
                1500000000, 300000000, 350000000, 1750000000, 240000000000, 0,
                48000000, 6000000, -20000000, -18000000, 20000000,
            ]
        )
    return path


def write_price_template(directory: Path, name: str = "price_template.csv") -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "date", "close", "volume", "value"])
        writer.writerows(PRICE_TEMPLATE_ROWS)
    return path


def write_benchmark_template(directory: Path, name: str = "benchmark_template.csv") -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "close", "label"])
        writer.writerow(["2024-01-02", 7300.0, "IHSG"])
        writer.writerow(["2024-01-03", 7320.0, "IHSG"])
    return path


def write_all_templates(directory: Path) -> List[Path]:
    return [
        write_template(directory),
        write_wide_template(directory),
        write_price_template(directory),
        write_benchmark_template(directory),
    ]
