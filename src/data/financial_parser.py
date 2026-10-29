"""Turn validated rows into period-level financial statements.

The parser:

* groups rows into periods (annual/quarter, consolidated/parent-only),
* converts reported values to absolute rupiah while retaining the reported
  value and unit,
* derives a small number of unambiguous line items (gross profit, EBITDA,
  total debt, free cash flow) and records that they were derived,
* converts cumulative quarterly figures to discrete quarters when needed.

Derived values are never treated as reported. Every period keeps provenance:
source files/URLs, units seen, publication dates and retrieval timestamps.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.data.normalization import (
    ITEM_STATEMENT,
    LINE_ITEMS,
    quarter_of,
    unit_multiplier,
)
from src.data.validation import RawFinancialRow


@dataclass
class ReportedValue:
    """A single figure as it appeared in the source, plus its rupiah value."""

    line_item: str
    value: Optional[float]
    unit: str = "rupiah"
    currency: str = "IDR"
    value_idr: Optional[float] = None
    source_url: Optional[str] = None
    source_file: Optional[str] = None
    notes: Optional[str] = None

    @property
    def is_present(self) -> bool:
        return self.value_idr is not None


@dataclass
class PeriodData:
    ticker: str
    period_end: _dt.date
    period_type: str = "annual"
    period_basis: str = "cumulative"
    is_consolidated: bool = True
    fiscal_year: int = 0
    company_name: Optional[str] = None
    sector: Optional[str] = None
    values: Dict[str, Optional[float]] = field(default_factory=dict)
    reported: Dict[str, ReportedValue] = field(default_factory=dict)
    derived: set = field(default_factory=set)
    source_urls: List[str] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    publication_date: Optional[_dt.date] = None
    retrieved_at: Optional[_dt.datetime] = None
    notes: Optional[str] = None

    # ------------------------------- accessors ---------------------------- #
    def get(self, item: str) -> Optional[float]:
        return self.values.get(item)

    def has(self, *items: str) -> bool:
        return all(self.values.get(item) is not None for item in items)

    @property
    def label(self) -> str:
        suffix = "consolidated" if self.is_consolidated else "parent-only"
        return f"{self.period_end.isoformat()} ({self.period_type}, {suffix})"

    def display_unit(self) -> str:
        """Best single unit for this period, used only for presentation."""
        units = {rv.unit for rv in self.reported.values() if rv.is_present}
        if len(units) == 1:
            return units.pop()
        return "rupiah"

    def as_dict(self) -> Dict[str, Optional[float]]:
        return dict(self.values)


def _derive(values: Dict[str, Optional[float]], derived: set) -> None:
    """Fill in line items that are unambiguous arithmetic identities."""

    def derive(key: str, expression, inputs: Iterable[str]) -> None:
        if values.get(key) is not None:
            return
        if all(values.get(item) is not None for item in inputs):
            try:
                values[key] = float(expression())
            except (TypeError, ZeroDivisionError):
                return
            derived.add(key)

    derive(
        "gross_profit",
        lambda: values["revenue"] - values["cost_of_revenue"],
        ("revenue", "cost_of_revenue"),
    )
    derive(
        "operating_income",
        lambda: (values["gross_profit"] or 0) - (values["operating_expenses"] or 0),
        ("gross_profit", "operating_expenses"),
    )
    derive(
        "ebitda",
        lambda: values["operating_income"] + values["depreciation_amortization"],
        ("operating_income", "depreciation_amortization"),
    )
    derive(
        "pretax_income",
        lambda: values["operating_income"] + (values.get("interest_income") or 0)
        - (values.get("interest_expense") or 0)
        + (values.get("other_income_expense") or 0),
        ("operating_income",),
    )
    derive(
        "net_income",
        lambda: (values["pretax_income"] or 0) - (values["income_tax"] or 0),
        ("pretax_income", "income_tax"),
    )
    derive(
        "net_income_parent",
        lambda: values["net_income"],
        ("net_income",),
    )
    derive(
        "total_liabilities",
        lambda: values["total_assets"] - values["equity"],
        ("total_assets", "equity"),
    )
    derive(
        "equity",
        lambda: values["total_assets"] - values["total_liabilities"],
        ("total_assets", "total_liabilities"),
    )
    derive(
        "current_assets",
        lambda: (values.get("cash") or 0)
        + (values.get("short_term_investments") or 0)
        + (values.get("receivables") or 0)
        + (values.get("inventory") or 0)
        + (values.get("other_current_assets") or 0),
        ("cash",),
    )
    derive(
        "total_debt",
        lambda: (values.get("short_term_debt") or 0) + (values.get("long_term_debt") or 0),
        ("short_term_debt",),
    )
    derive(
        "fcf",
        lambda: values["cfo"] - values["capex"],
        ("cfo", "capex"),
    )
    derive(
        "eps",
        lambda: values["net_income_parent"] / values["shares_outstanding"],
        ("net_income_parent", "shares_outstanding"),
    )


def build_periods(rows: Sequence[RawFinancialRow]) -> List[PeriodData]:
    """Group rows into ``PeriodData`` objects sorted newest first."""
    buckets: Dict[Tuple[str, _dt.date, str, bool], PeriodData] = {}

    for row in rows:
        key = (row.ticker, row.period_end, row.period_type, row.is_consolidated)
        period = buckets.get(key)
        if period is None:
            period = PeriodData(
                ticker=row.ticker,
                period_end=row.period_end,
                period_type=row.period_type,
                period_basis=row.period_basis,
                is_consolidated=row.is_consolidated,
                fiscal_year=row.period_end.year,
                company_name=row.company_name,
                sector=row.sector,
                publication_date=row.publication_date,
                retrieved_at=row.retrieved_at,
                notes=row.notes,
            )
            buckets[key] = period

        multiplier = unit_multiplier(row.unit)
        value_idr = None if row.value is None else row.value * multiplier
        period.reported[row.line_item] = ReportedValue(
            line_item=row.line_item,
            value=row.value,
            unit=row.unit,
            currency=row.currency,
            value_idr=value_idr,
            source_url=row.source_url,
            source_file=row.source_file,
            notes=row.notes,
        )
        if row.line_item not in period.values or period.values[row.line_item] is None:
            period.values[row.line_item] = value_idr
        if row.source_url and row.source_url not in period.source_urls:
            period.source_urls.append(row.source_url)
        if row.source_file and row.source_file not in period.source_files:
            period.source_files.append(row.source_file)
        if period.company_name is None and row.company_name:
            period.company_name = row.company_name
        if period.sector is None and row.sector:
            period.sector = row.sector
        if period.publication_date is None and row.publication_date:
            period.publication_date = row.publication_date
        if period.retrieved_at is None and row.retrieved_at:
            period.retrieved_at = row.retrieved_at

    periods = list(buckets.values())
    for period in periods:
        _derive(period.values, period.derived)
    periods.sort(key=lambda p: (p.period_end, p.is_consolidated), reverse=True)
    return periods


def select_series(
    periods: Sequence[PeriodData],
    period_type: str = "annual",
    consolidated: bool = True,
) -> List[PeriodData]:
    """Filter to one statement family, newest first."""
    return sorted(
        [p for p in periods if p.period_type == period_type and p.is_consolidated == consolidated],
        key=lambda p: p.period_end,
        reverse=True,
    )


def discrete_quarters(periods: Sequence[PeriodData], consolidated: bool = True) -> List[PeriodData]:
    """Convert cumulative year-to-date quarters into discrete quarters.

    IDX interim statements are normally cumulative (Q1, H1, 9M, FY). Mixing
    those with discrete quarters double counts revenue, so every quarterly
    aggregation in this application goes through this function.
    """
    quarters = sorted(
        [p for p in periods if p.period_type == "quarter" and p.is_consolidated == consolidated],
        key=lambda p: p.period_end,
    )
    output: List[PeriodData] = []
    previous_by_year: Dict[int, PeriodData] = {}

    for period in quarters:
        if period.period_basis == "discrete":
            output.append(period)
            previous_by_year[period.period_end.year] = period
            continue

        prior = previous_by_year.get(period.period_end.year)
        if quarter_of(period.period_end) == 1 or prior is None:
            discrete_values = dict(period.values)
            derived = set(period.derived)
        else:
            discrete_values = {}
            derived = set(period.derived)
            for item, value in period.values.items():
                prev_value = prior.values.get(item)
                if value is None or prev_value is None:
                    discrete_values[item] = None
                elif item in _FLOW_ITEMS:
                    discrete_values[item] = value - prev_value
                    derived.add(item)
                else:
                    discrete_values[item] = value
            for item in _FLOW_ITEMS:
                if item not in period.values and prior.values.get(item) is not None:
                    discrete_values[item] = None
        clone = PeriodData(
            ticker=period.ticker,
            period_end=period.period_end,
            period_type="quarter",
            period_basis="discrete",
            is_consolidated=period.is_consolidated,
            fiscal_year=period.fiscal_year,
            company_name=period.company_name,
            sector=period.sector,
            values=discrete_values,
            reported=period.reported,
            derived=derived,
            source_urls=list(period.source_urls),
            source_files=list(period.source_files),
            publication_date=period.publication_date,
            retrieved_at=period.retrieved_at,
            notes=period.notes,
        )
        output.append(clone)
        previous_by_year[period.period_end.year] = period

    output.sort(key=lambda p: p.period_end, reverse=True)
    return output


# Items that accumulate over a fiscal year (income statement and cash flow).
_FLOW_ITEMS = frozenset(
    key for key, meta in LINE_ITEMS.items() if meta["statement"] in {"income", "cashflow"}
)


def trailing_twelve_months(
    periods: Sequence[PeriodData],
    consolidated: bool = True,
    allow_annual_fallback: bool = True,
) -> Tuple[Optional[PeriodData], str]:
    """Build a TTM period from four discrete quarters when available.

    Returns ``(period, explanation)``. ``period`` is ``None`` when no usable
    window exists. Annual data is used only as a documented fallback, never
    blended with quarterly data.
    """
    quarters = discrete_quarters(periods, consolidated=consolidated)
    if len(quarters) >= 4:
        last_four = quarters[:4]
        values: Dict[str, Optional[float]] = {}
        for item in _FLOW_ITEMS:
            components = [q.values.get(item) for q in last_four]
            if all(component is not None for component in components):
                values[item] = float(sum(components))  # type: ignore[arg-type]
            else:
                values[item] = None
        for item in LINE_ITEMS:
            if item in _FLOW_ITEMS:
                continue
            values[item] = last_four[0].values.get(item)

        ttm = PeriodData(
            ticker=last_four[0].ticker,
            period_end=last_four[0].period_end,
            period_type="ttm",
            period_basis="discrete",
            is_consolidated=last_four[0].is_consolidated,
            fiscal_year=last_four[0].fiscal_year,
            company_name=last_four[0].company_name,
            sector=last_four[0].sector,
            values=values,
            reported={},
            derived=set(_FLOW_ITEMS) | {"ttm"},
            source_urls=sorted({url for q in last_four for url in q.source_urls}),
            source_files=sorted({f for q in last_four for f in q.source_files}),
            publication_date=max(
                (q.publication_date for q in last_four if q.publication_date), default=None
            ),
            retrieved_at=last_four[0].retrieved_at,
            notes="Trailing twelve months: sum of four discrete quarters.",
        )
        _derive(ttm.values, ttm.derived)
        return ttm, (
            f"TTM built from four discrete quarters "
            f"({last_four[-1].period_end.isoformat()} to {last_four[0].period_end.isoformat()})."
        )

    if allow_annual_fallback:
        annual = select_series(periods, "annual", consolidated=consolidated)
        if annual:
            return annual[0], (
                "TTM unavailable (fewer than four discrete quarters); latest annual period used instead."
            )
    return None, "No usable period: need four quarters, or one annual statement, for the same basis."


def merge_metadata(periods: Sequence[PeriodData]) -> Dict[str, Optional[str]]:
    """Collect company metadata carried by imported rows."""
    meta: Dict[str, Optional[str]] = {"company_name": None, "sector": None}
    for period in periods:
        if meta["company_name"] is None and period.company_name:
            meta["company_name"] = period.company_name
        if meta["sector"] is None and period.sector:
            meta["sector"] = period.sector
    return meta
