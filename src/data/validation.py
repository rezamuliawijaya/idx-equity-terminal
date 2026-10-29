"""Validation for imported financial data.

Two layers:

1. Row level (Pydantic) - types, ticker shape, unit vocabulary, currency.
2. Dataset level - duplicates, accounting identities, internal consistency and
   completeness. These checks produce *issues* with a severity rather than
   raising, so the UI can show exactly what will be blocked and what is merely
   suspicious.

Nothing in this module silently repairs data. Derived values are produced later
by ``financial_parser`` and are always marked as derived.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.data.normalization import (
    ESSENTIAL_ITEMS,
    ITEM_STATEMENT,
    STATEMENTS,
    normalize_currency,
    normalize_period_basis,
    normalize_period_type,
    normalize_ticker,
    normalize_unit,
    parse_period_end,
    parse_number,
)

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


class RawFinancialRow(BaseModel):
    """One validated financial line item for one period."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    ticker: str
    period_end: _dt.date
    period_type: str = "annual"
    period_basis: str = "cumulative"
    statement: str
    line_item: str
    value: Optional[float] = None
    unit: str = "rupiah"
    currency: str = "IDR"
    is_consolidated: bool = True
    source_url: Optional[str] = None
    source_file: Optional[str] = None
    publication_date: Optional[_dt.date] = None
    retrieved_at: Optional[_dt.datetime] = None
    notes: Optional[str] = None
    company_name: Optional[str] = None
    sector: Optional[str] = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _ticker(cls, value: object) -> str:
        return normalize_ticker(value)

    @field_validator("period_end", mode="before")
    @classmethod
    def _period(cls, value: object) -> _dt.date:
        return parse_period_end(value)

    @field_validator("period_type", mode="before")
    @classmethod
    def _period_type(cls, value: object) -> str:
        return normalize_period_type(value)

    @field_validator("period_basis", mode="before")
    @classmethod
    def _period_basis(cls, value: object) -> str:
        return normalize_period_basis(value)

    @field_validator("statement", mode="before")
    @classmethod
    def _statement(cls, value: object) -> str:
        text = str(value or "").strip().lower()
        aliases = {
            "income statement": "income",
            "profit and loss": "income",
            "p&l": "income",
            "pnl": "income",
            "laba rugi": "income",
            "balance sheet": "balance",
            "statement of financial position": "balance",
            "neraca": "balance",
            "cash flow": "cashflow",
            "cash flow statement": "cashflow",
            "cashflow statement": "cashflow",
            "arus kas": "cashflow",
        }
        text = aliases.get(text, text)
        if text not in STATEMENTS:
            raise ValueError(f"Unrecognized statement: {value!r}")
        return text

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> Optional[float]:
        return parse_number(value)

    @field_validator("unit", mode="before")
    @classmethod
    def _unit(cls, value: object) -> str:
        return normalize_unit(value)[0]

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: object) -> str:
        return normalize_currency(value)

    @field_validator("publication_date", "retrieved_at", mode="before")
    @classmethod
    def _optional_date(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, _dt.datetime):
            return value
        if isinstance(value, _dt.date):
            return value
        try:
            return parse_period_end(value)
        except ValueError:
            return None

    @model_validator(mode="after")
    def _check_item(self) -> "RawFinancialRow":
        if not self.line_item:
            raise ValueError("line_item is required")
        expected = ITEM_STATEMENT.get(self.line_item)
        if expected and expected != self.statement:
            raise ValueError(
                f"line_item {self.line_item!r} belongs to {expected!r}, not {self.statement!r}"
            )
        if self.currency != "IDR":
            raise ValueError(
                f"Unsupported currency {self.currency!r}. Import IDR statements only; "
                "foreign-currency statements require an explicit FX conversion step."
            )
        return self


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    context: Dict[str, object] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def as_dict(self) -> Dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            **{k: v for k, v in self.context.items()},
        }


@dataclass
class ValidationReport:
    issues: List[ValidationIssue] = field(default_factory=list)
    rows_checked: int = 0
    periods_checked: int = 0

    def add(self, severity: str, code: str, message: str, **context: object) -> None:
        self.issues.append(ValidationIssue(severity=severity, code=code, message=message, context=dict(context)))

    @property
    def errors(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def infos(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "info"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_severity(self) -> List[ValidationIssue]:
        return sorted(self.issues, key=lambda item: SEVERITY_ORDER.get(item.severity, 9))

    def summary(self) -> Dict[str, int]:
        return {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "rows": self.rows_checked,
            "periods": self.periods_checked,
        }

    def to_frame(self):
        import pandas as pd

        if not self.issues:
            return pd.DataFrame(columns=["severity", "code", "message"])
        return pd.DataFrame(
            [
                {"severity": i.severity, "code": i.code, "message": i.message, **i.context}
                for i in self.by_severity()
            ]
        )


_REL_TOLERANCE = 0.005  # 0.5% of the reference figure
_ABS_TOLERANCE = 1.0  # one rupiah


def _close(a: float, b: float, reference: float) -> bool:
    return abs(a - b) <= max(_ABS_TOLERANCE, abs(reference) * _REL_TOLERANCE)


def _period_key(row: RawFinancialRow) -> Tuple[str, _dt.date, bool, str]:
    return (row.ticker, row.period_end, row.is_consolidated, row.period_type)


def validate_dataset(rows: Sequence[RawFinancialRow]) -> ValidationReport:
    """Run dataset-level validation over parsed rows."""
    report = ValidationReport()
    report.rows_checked = len(rows)
    if not rows:
        report.add("error", "empty_dataset", "No financial rows were found in the import.")
        return report

    seen: Dict[Tuple[str, _dt.date, str, str, bool, str], int] = {}
    for row in rows:
        key = (row.ticker, row.period_end, row.statement, row.line_item, row.is_consolidated, row.period_type)
        if key in seen:
            report.add(
                "error",
                "duplicate_item",
                f"Duplicate {row.line_item} for {row.ticker} {row.period_end} ({row.statement}).",
                ticker=row.ticker,
                period_end=str(row.period_end),
                line_item=row.line_item,
            )
        seen[key] = seen.get(key, 0) + 1

    grouped: Dict[Tuple[str, _dt.date, bool, str], Dict[str, Optional[float]]] = {}
    sources: Dict[Tuple[str, _dt.date, bool, str], set] = {}
    for row in rows:
        key = _period_key(row)
        bucket = grouped.setdefault(key, {})
        if row.line_item in bucket and bucket[row.line_item] is not None:
            continue
        bucket[row.line_item] = row.value
        sources.setdefault(key, set())
        if row.source_url:
            sources[key].add(row.source_url)

    report.periods_checked = len(grouped)

    for (ticker, period_end, consolidated, period_type), values in grouped.items():
        label = f"{ticker} {period_end.isoformat()}"
        basis_label = "consolidated" if consolidated else "parent-only"

        present = {k: v for k, v in values.items() if v is not None}
        if not present:
            report.add("error", "empty_period", f"{label}: every line item is blank.", ticker=ticker, period_end=str(period_end))
            continue

        missing = [item for item in ESSENTIAL_ITEMS if values.get(item) is None]
        if missing:
            report.add(
                "warning",
                "missing_essential_items",
                f"{label} ({basis_label}) is missing essential items: {', '.join(missing)}.",
                ticker=ticker,
                period_end=str(period_end),
                missing=missing,
            )

        revenue = values.get("revenue")
        if revenue is not None and revenue < 0:
            report.add("warning", "negative_revenue", f"{label}: reported revenue is negative.", ticker=ticker, period_end=str(period_end))

        assets = values.get("total_assets")
        liabilities = values.get("total_liabilities")
        equity = values.get("equity")
        if assets is not None and liabilities is not None and equity is not None:
            if not _close(liabilities + equity, assets, assets):
                report.add(
                    "warning",
                    "balance_sheet_imbalance",
                    f"{label}: total liabilities + equity ({liabilities + equity:,.0f}) "
                    f"does not reconcile to total assets ({assets:,.0f}).",
                    ticker=ticker,
                    period_end=str(period_end),
                    difference=float(liabilities + equity - assets),
                )

        current_assets = values.get("current_assets")
        if current_assets is not None and assets is not None and current_assets > assets * 1.001:
            report.add(
                "error",
                "current_assets_exceed_assets",
                f"{label}: current assets ({current_assets:,.0f}) exceed total assets ({assets:,.0f}).",
                ticker=ticker,
                period_end=str(period_end),
            )

        current_liabilities = values.get("current_liabilities")
        if current_liabilities is not None and liabilities is not None and current_liabilities > liabilities * 1.001:
            report.add(
                "error",
                "current_liabilities_exceed_liabilities",
                f"{label}: current liabilities exceed total liabilities.",
                ticker=ticker,
                period_end=str(period_end),
            )

        gross = values.get("gross_profit")
        cogs = values.get("cost_of_revenue")
        if gross is not None and revenue is not None and cogs is not None:
            if not _close(revenue - cogs, gross, max(abs(revenue), 1.0)):
                report.add(
                    "warning",
                    "gross_profit_mismatch",
                    f"{label}: gross profit ({gross:,.0f}) differs from revenue - cost of revenue "
                    f"({revenue - cogs:,.0f}). The reported value is retained.",
                    ticker=ticker,
                    period_end=str(period_end),
                )
        if gross is None and revenue is not None and cogs is not None:
            report.add(
                "info",
                "gross_profit_derived",
                f"{label}: gross profit will be derived as revenue - cost of revenue.",
                ticker=ticker,
                period_end=str(period_end),
            )

        pretax = values.get("pretax_income")
        net = values.get("net_income")
        tax = values.get("income_tax")
        if pretax is not None and net is not None and tax is not None:
            if not _close(pretax - tax, net, max(abs(pretax), 1.0)):
                report.add(
                    "warning",
                    "tax_reconciliation",
                    f"{label}: pre-tax income - income tax does not equal net income.",
                    ticker=ticker,
                    period_end=str(period_end),
                )

        if period_type == "quarter" and period_end.month not in (3, 6, 9, 12):
            report.add(
                "warning",
                "odd_quarter_end",
                f"{label}: quarterly periods usually end in March, June, September or December.",
                ticker=ticker,
                period_end=str(period_end),
            )

        for row in rows:
            if (
                row.ticker == ticker
                and row.period_end == period_end
                and row.period_type == period_type
                and row.is_consolidated == consolidated
                and row.publication_date
                and row.publication_date < row.period_end
            ):
                report.add(
                    "warning",
                    "publication_before_period",
                    f"{label}: publication date {row.publication_date} precedes the period end.",
                    ticker=ticker,
                    period_end=str(period_end),
                )
                break

        if len(sources.get((ticker, period_end, consolidated, period_type), set())) == 0:
            report.add(
                "info",
                "missing_source_url",
                f"{label}: no source URL recorded. Provenance will be shown as 'not provided'.",
                ticker=ticker,
                period_end=str(period_end),
            )

    tickers = {row.ticker for row in rows}
    for ticker in tickers:
        ticker_rows = [r for r in rows if r.ticker == ticker]
        if not any(r.is_consolidated for r in ticker_rows):
            report.add(
                "warning",
                "no_consolidated_statements",
                f"{ticker}: only parent-only statements were imported. Sector comparisons may be distorted.",
                ticker=ticker,
            )
        years = {r.period_end.year for r in ticker_rows if r.period_type == "annual"}
        if 0 < len(years) < 2:
            report.add(
                "info",
                "single_year_history",
                f"{ticker}: only one annual period available; growth metrics will be unavailable.",
                ticker=ticker,
            )

    return report


def check_staleness(
    latest_period_end: Optional[_dt.date],
    today: Optional[_dt.date] = None,
    max_age_days: int = 550,
) -> Tuple[bool, Optional[int]]:
    """Return ``(is_stale, age_days)`` for the newest fundamental period."""
    if latest_period_end is None:
        return True, None
    reference = today or _dt.date.today()
    age = (reference - latest_period_end).days
    return age > max_age_days, age


def completeness(values: Dict[str, Optional[float]], required: Iterable[str] = ESSENTIAL_ITEMS) -> float:
    """Share of required line items that are present and non-null."""
    required = list(required)
    if not required:
        return 1.0
    present = sum(1 for item in required if values.get(item) is not None)
    return present / len(required)
