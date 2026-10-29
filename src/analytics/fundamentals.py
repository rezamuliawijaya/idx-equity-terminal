"""Core fundamental analytics: metric containers, growth, TTM and cash flow.

Design rules enforced here and everywhere downstream:

* A missing input yields ``UNAVAILABLE``. It is never coerced to zero.
* A mathematically undefined ratio (zero or negative denominator where the
  ratio has no economic meaning, e.g. P/E on losses) yields
  ``NOT_MEANINGFUL`` with an explanation.
* Reported, calculated, estimated and unavailable values are distinguishable.
* Every metric carries its formula and its inputs.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config.settings import resolve_profile
from src.data.financial_parser import (
    PeriodData,
    discrete_quarters,
    select_series,
    trailing_twelve_months,
)

# --------------------------------------------------------------------------- #
# Metric container
# --------------------------------------------------------------------------- #

REPORTED = "reported"
CALCULATED = "calculated"
ESTIMATED = "estimated"
UNAVAILABLE = "unavailable"
NOT_MEANINGFUL = "not_meaningful"

STATUS_ORDER = {REPORTED: 0, CALCULATED: 1, ESTIMATED: 2, NOT_MEANINGFUL: 3, UNAVAILABLE: 4}


@dataclass
class Metric:
    key: str
    label: str
    value: Optional[float] = None
    unit: str = "multiple"
    status: str = CALCULATED
    formula: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    as_of: Optional[_dt.date] = None
    group: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None and self.status in {REPORTED, CALCULATED, ESTIMATED}

    @property
    def numeric(self) -> Optional[float]:
        return float(self.value) if self.available else None  # type: ignore[arg-type]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "formula": self.formula,
            "inputs": self.inputs,
            "note": self.note,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "group": self.group,
        }


def unavailable(key: str, label: str, reason: str, formula: str = "", unit: str = "multiple", group: str = "") -> Metric:
    return Metric(key=key, label=label, value=None, status=UNAVAILABLE, note=reason, formula=formula, unit=unit, group=group)


def not_meaningful(key: str, label: str, reason: str, formula: str = "", unit: str = "multiple", group: str = "") -> Metric:
    return Metric(key=key, label=label, value=None, status=NOT_MEANINGFUL, note=reason, formula=formula, unit=unit, group=group)


def calculated(
    key: str,
    label: str,
    value: float,
    unit: str = "multiple",
    formula: str = "",
    inputs: Optional[Dict[str, Any]] = None,
    note: str = "",
    status: str = CALCULATED,
    group: str = "",
) -> Metric:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return unavailable(key, label, "Result is not a finite number.", formula, unit, group)
    return Metric(
        key=key,
        label=label,
        value=float(value),
        unit=unit,
        status=status,
        formula=formula,
        inputs=inputs or {},
        note=note,
        group=group,
    )


# --------------------------------------------------------------------------- #
# Arithmetic helpers
# --------------------------------------------------------------------------- #


def series_values(periods: Sequence[PeriodData], item: str) -> List[Tuple[_dt.date, Optional[float]]]:
    return [(p.period_end, p.get(item)) for p in periods]


def yoy_change(current: Optional[float], prior: Optional[float]) -> Tuple[Optional[float], str]:
    """Year-on-year relative change with economic-meaning guard rails."""
    if current is None or prior is None:
        return None, "One of the two periods is missing the required figure."
    if prior == 0:
        return None, "Prior-period figure is zero; relative growth is undefined."
    if prior < 0:
        return None, "Growth off a negative base is not economically meaningful."
    return (current - prior) / abs(prior), ""


def cagr(start: Optional[float], end: Optional[float], years: float) -> Tuple[Optional[float], str]:
    if start is None or end is None:
        return None, "One of the endpoint values is missing."
    if years <= 0:
        return None, "Invalid compounding horizon."
    if start <= 0:
        return None, "CAGR is undefined when the starting value is zero or negative."
    if end <= 0:
        return None, "CAGR is undefined when the ending value is zero or negative."
    return (end / start) ** (1.0 / years) - 1.0, ""


def calculate_margin_of_safety(fair_value: float, price: float) -> Optional[float]:
    """(Fair value - price) / fair value, per the project definition.

    Returns ``None`` when the fair value is zero or negative, because the
    percentage would be meaningless.
    """
    if fair_value is None or price is None:
        return None
    if fair_value <= 0:
        return None
    return (fair_value - price) / fair_value


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Tuple[Optional[float], str]:
    if numerator is None or denominator is None:
        return None, "Required input missing."
    if denominator == 0:
        return None, "Denominator is zero."
    return numerator / denominator, ""


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


@dataclass
class FundamentalSnapshot:
    """Everything the analytics stack needs for one company at one point in time."""

    ticker: str
    company_name: str
    sector: str
    profile: str
    as_of: Optional[_dt.date]
    period_label: str
    period_type: str
    values: Dict[str, Optional[float]]
    derived: set
    periods: List[PeriodData]
    annual: List[PeriodData]
    latest_period: Optional[PeriodData]
    shares_outstanding: Optional[float]
    price: Optional[float] = None
    price_date: Optional[_dt.date] = None
    metrics: Dict[str, Metric] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    ttm_explanation: str = ""
    is_synthetic: bool = False

    # ----------------------------- convenience ---------------------------- #
    def get(self, item: str) -> Optional[float]:
        return self.values.get(item)

    def metric(self, key: str) -> Optional[Metric]:
        return self.metrics.get(key)

    def metric_value(self, key: str) -> Optional[float]:
        metric = self.metrics.get(key)
        return metric.numeric if metric else None

    def add(self, metric: Metric) -> Metric:
        self.metrics[metric.key] = metric
        return metric

    def extend(self, metrics: Iterable[Metric]) -> None:
        for metric in metrics:
            self.metrics[metric.key] = metric

    def metrics_frame(self, group: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = []
        for metric in self.metrics.values():
            if group and metric.group != group:
                continue
            rows.append(metric.as_dict())
        return rows

    @property
    def data_sources(self) -> List[str]:
        seen: List[str] = []
        for period in self.periods:
            for url in period.source_urls:
                if url not in seen:
                    seen.append(url)
        return seen

    @property
    def publication_date(self) -> Optional[_dt.date]:
        dates = [p.publication_date for p in self.periods if p.publication_date]
        return max(dates) if dates else None

    def coverage(self, keys: Iterable[str]) -> float:
        keys = list(keys)
        if not keys:
            return 1.0
        return sum(1 for key in keys if self.metric_value(key) is not None) / len(keys)


def _shares(periods: Sequence[PeriodData], fallback: Optional[float]) -> Optional[float]:
    for period in periods:
        value = period.get("shares_outstanding")
        if value and value > 0:
            return float(value)
    return float(fallback) if fallback else None


def _net_income(snapshot_values: Dict[str, Optional[float]]) -> Optional[float]:
    return snapshot_values.get("net_income_parent") or snapshot_values.get("net_income")


def compute_net_debt(values: Dict[str, Optional[float]]) -> Optional[float]:
    """Net debt = total debt - cash - short-term investments.

    Returns ``None`` when debt is unavailable; a company with debt but no cash
    line is not assumed to have zero cash.
    """
    debt = values.get("total_debt")
    if debt is None:
        st = values.get("short_term_debt")
        lt = values.get("long_term_debt")
        if st is None and lt is None:
            return None
        debt = (st or 0.0) + (lt or 0.0)
    cash = values.get("cash")
    if cash is None:
        return None
    return debt - cash - (values.get("short_term_investments") or 0.0)


def _growth_series(
    snapshot: FundamentalSnapshot,
) -> Tuple[List[PeriodData], str]:
    """Choose TTM (when 8 quarters exist) or annual data for growth metrics."""
    quarters = discrete_quarters(snapshot.periods, consolidated=snapshot.latest_period.is_consolidated if snapshot.latest_period else True)
    if len(quarters) >= 8:
        ttm_now, _ = trailing_twelve_months(snapshot.periods)
        ttm_prior, _ = trailing_twelve_months(quarters[4:])
        if ttm_now is not None and ttm_prior is not None:
            return [ttm_now, ttm_prior], "TTM"
    if len(snapshot.annual) >= 2:
        return list(snapshot.annual[:3]), "annual"
    return [], "unavailable"


def build_snapshot(
    ticker: str,
    periods: Sequence[PeriodData],
    price: Optional[float] = None,
    price_date: Optional[_dt.date] = None,
    company_name: Optional[str] = None,
    sector: Optional[str] = None,
    shares_outstanding: Optional[float] = None,
    is_synthetic: bool = False,
) -> FundamentalSnapshot:
    """Build a TTM-first snapshot plus growth and cash-flow metrics."""
    if not periods:
        raise ValueError(f"No financial periods available for {ticker}")

    consolidated = periods[0].is_consolidated
    annual_all = select_series(periods, "annual", consolidated=consolidated)
    annual = list(annual_all)

    ttm, explanation = trailing_twelve_months(periods, consolidated=consolidated)
    if ttm is not None:
        base = ttm
        period_type = "ttm"
        period_label = f"TTM to {ttm.period_end.isoformat()}"
    else:
        base = annual[0]
        period_type = "annual"
        period_label = f"FY{annual[0].period_end.year} (annual)"

    values = dict(base.values)
    shares = _shares(periods, shares_outstanding)
    if shares and values.get("shares_outstanding") is None:
        values["shares_outstanding"] = shares

    snapshot = FundamentalSnapshot(
        ticker=ticker,
        company_name=company_name or base.company_name or ticker,
        sector=sector or base.sector or "Unclassified",
        profile=resolve_profile(sector or base.sector),
        as_of=base.period_end,
        period_label=period_label,
        period_type=period_type,
        values=values,
        derived=set(base.derived),
        periods=list(periods),
        annual=annual,
        latest_period=base,
        shares_outstanding=shares,
        price=float(price) if price is not None else None,
        price_date=price_date,
        ttm_explanation=explanation,
        is_synthetic=is_synthetic,
    )

    if not annual:
        snapshot.warnings.append("No annual statements: multi-year growth and CAGR are unavailable.")

    # ------------------------------ growth -------------------------------- #
    growth_periods, growth_basis = _growth_series(snapshot)
    if len(growth_periods) >= 2:
        current, prior = growth_periods[0], growth_periods[1]
        specs = [
            ("revenue", "revenue_growth", "Revenue Growth"),
            ("operating_income", "operating_income_growth", "Operating Income Growth"),
            ("net_income", "net_income_growth", "Net Income Growth"),
            ("eps", "eps_growth", "EPS Growth"),
            ("fcf", "fcf_growth", "Free Cash Flow Growth"),
        ]
        for item, key, label in specs:
            value, reason = yoy_change(current.get(item), prior.get(item))
            if value is None:
                snapshot.add(
                    not_meaningful(key, label, reason, f"({item}_t - {item}_t-1) / |{item}_t-1|", unit="percent", group="growth")
                    if current.get(item) is not None and prior.get(item) is not None
                    else unavailable(key, label, reason, unit="percent", group="growth")
                )
            else:
                snapshot.add(
                    calculated(
                        key,
                        label,
                        value,
                        unit="percent",
                        formula=f"({item}_t - {item}_t-1) / |{item}_t-1|",
                        inputs={f"{item}_current": current.get(item), f"{item}_prior": prior.get(item)},
                        note=f"Based on {growth_basis} figures ({prior.period_end.isoformat()} -> {current.period_end.isoformat()}).",
                        group="growth",
                    )
                )
    else:
        reason = "Need at least two comparable periods (annual, or eight discrete quarters)."
        for key, label in [
            ("revenue_growth", "Revenue Growth"),
            ("operating_income_growth", "Operating Income Growth"),
            ("net_income_growth", "Net Income Growth"),
            ("eps_growth", "EPS Growth"),
            ("fcf_growth", "Free Cash Flow Growth"),
        ]:
            snapshot.add(unavailable(key, label, reason, unit="percent", group="growth"))

    for years in (3, 5):
        key = f"revenue_cagr_{years}y"
        label = f"Revenue CAGR ({years}y)"
        if len(annual) >= years + 1:
            start = annual[years].get("revenue")
            end = annual[0].get("revenue")
            span = (annual[0].period_end - annual[years].period_end).days / 365.25
            value, reason = cagr(start, end, span)
            if value is None:
                snapshot.add(not_meaningful(key, label, reason, unit="percent", group="growth"))
            else:
                snapshot.add(
                    calculated(
                        key,
                        label,
                        value,
                        unit="percent",
                        formula=f"(revenue_t / revenue_t-{years})^(1/{span:.2f}) - 1",
                        inputs={"start": start, "end": end, "years": span},
                        group="growth",
                    )
                )
        else:
            snapshot.add(
                unavailable(
                    key,
                    label,
                    f"Requires {years + 1} annual periods; {len(annual)} available.",
                    unit="percent",
                    group="growth",
                )
            )

    # ---------------------------- cash flow ------------------------------- #
    cfo = values.get("cfo")
    capex = values.get("capex")
    net_income = _net_income(values)
    revenue = values.get("revenue")
    dividends = values.get("dividends_paid")

    if values.get("fcf") is not None:
        snapshot.add(
            Metric(
                key="fcf",
                label="Free Cash Flow",
                value=values["fcf"],
                unit="idr",
                status=REPORTED if "fcf" not in base.derived else CALCULATED,
                formula="CFO - Capex",
                inputs={"cfo": cfo, "capex": capex},
                group="cashflow",
            )
        )
    else:
        snapshot.add(unavailable("fcf", "Free Cash Flow", "CFO or capex missing.", "CFO - Capex", "idr", "cashflow"))

    ratio_specs = [
        ("cfo_to_net_income", "CFO / Net Income", cfo, net_income, "Cash conversion on reported earnings.", "multiple"),
        ("fcf_margin", "FCF Margin", values.get("fcf"), revenue, "Free cash flow as a share of revenue.", "percent"),
        (
            "dividend_payout",
            "Dividend Payout Ratio",
            abs(dividends) if dividends is not None else None,
            net_income,
            "Dividends paid / net income.",
            "percent",
        ),
    ]
    for key, label, numerator, denominator, formula, unit in ratio_specs:
        if numerator is None or denominator is None:
            snapshot.add(
                unavailable(key, label, "Required input missing.", formula, unit, "cashflow")
            )
            continue
        if denominator <= 0:
            snapshot.add(
                not_meaningful(
                    key,
                    label,
                    "Denominator is zero or negative; the ratio has no useful interpretation.",
                    formula,
                    unit,
                    "cashflow",
                )
            )
            continue
        value = numerator / denominator
        snapshot.add(
            calculated(
                key,
                label,
                value,
                unit=unit,
                formula=formula,
                inputs={"numerator": numerator, "denominator": denominator},
                group="cashflow",
            )
        )

    if dividends is None:
        snapshot.add(unavailable("dividends_paid", "Dividends Paid", "Not disclosed in imported data.", "", "idr", "cashflow"))
    else:
        snapshot.add(
            Metric(
                key="dividends_paid",
                label="Dividends Paid",
                value=abs(dividends),
                unit="idr",
                status=REPORTED,
                formula="Cash flow statement, financing section",
                group="cashflow",
            )
        )

    # Reported income-statement lines worth surfacing as metrics.
    for item, label in (("revenue", "Revenue"), ("net_income", "Net Income"), ("ebitda", "EBITDA")):
        if values.get(item) is not None:
            snapshot.add(
                Metric(
                    key=item,
                    label=label,
                    value=values[item],
                    unit="idr",
                    status=CALCULATED if item in base.derived else REPORTED,
                    formula="Reported in the financial statements" if item not in base.derived else "Derived during parsing",
                    group="statement",
                )
            )

    return snapshot


def history_frame(periods: Sequence[PeriodData], items: Sequence[str]) -> Any:
    """Long-format history table for charts and tables."""
    import pandas as pd

    rows = []
    for period in sorted(periods, key=lambda p: p.period_end):
        row = {
            "period_end": period.period_end,
            "period_type": period.period_type,
            "basis": "consolidated" if period.is_consolidated else "parent",
        }
        for item in items:
            row[item] = period.get(item)
        rows.append(row)
    return pd.DataFrame(rows)
