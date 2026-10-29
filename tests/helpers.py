"""Test helpers: build periods from explicit line items.

Nothing here invents market data. The values below are deliberately simple
arithmetic fixtures used to exercise the analytics, not a model of a real issuer.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, Iterable, List, Mapping, Optional

from src.data.financial_parser import PeriodData, build_periods
from src.data.normalization import ITEM_STATEMENT
from src.data.validation import RawFinancialRow


def make_period(
    ticker: str = "TEST",
    period_end: _dt.date = _dt.date(2023, 12, 31),
    values: Optional[Mapping[str, Optional[float]]] = None,
    period_type: str = "annual",
    basis: str = "cumulative",
    consolidated: bool = True,
    publication_date: Optional[_dt.date] = None,
    sector: str = "Industrials",
    company_name: str = "Test Manufacturing Tbk",
    source_url: str = "https://example.test/filing",
) -> PeriodData:
    payload = dict(values or {})
    rows = [
        RawFinancialRow(
            ticker=ticker,
            period_end=period_end,
            period_type=period_type,
            period_basis=basis,
            statement=ITEM_STATEMENT.get(item, "income"),
            line_item=item,
            value=value,
            unit="rupiah",
            is_consolidated=consolidated,
            source_url=source_url,
            source_file="tests/helpers.py",
            publication_date=publication_date,
            sector=sector,
            company_name=company_name,
        )
        for item, value in payload.items()
        if value is not None
    ]
    periods = build_periods(rows)
    if len(periods) != 1:
        raise AssertionError(f"expected exactly one period, built {len(periods)}")
    return periods[0]


def manufacturing_values(revenue: float) -> Dict[str, Optional[float]]:
    """A consistent, profitable manufacturing year at a given revenue level."""
    gross = revenue * 0.38
    cost = revenue - gross
    opex = revenue * 0.18
    ebit = gross - opex
    da = revenue * 0.05
    ebitda = ebit + da
    interest = revenue * 0.02
    pretax = ebit - interest
    tax = pretax * 0.22
    net_income = pretax - tax
    shares = 1_000_000_000.0
    return {
        "revenue": revenue,
        "cost_of_revenue": cost,
        "gross_profit": gross,
        "operating_expenses": opex,
        "operating_income": ebit,
        "depreciation_amortization": da,
        "ebitda": ebitda,
        "interest_expense": interest,
        "pretax_income": pretax,
        "income_tax": tax,
        "net_income": net_income,
        "eps": net_income / shares,
        "shares_outstanding": shares,
        "cash": revenue * 0.20,
        "receivables": revenue * 0.15,
        "inventory": revenue * 0.12,
        "current_assets": revenue * 0.50,
        "ppe": revenue * 0.75,
        "total_assets": revenue * 1.40,
        "current_liabilities": revenue * 0.28,
        "short_term_debt": revenue * 0.08,
        "long_term_debt": revenue * 0.18,
        "total_liabilities": revenue * 0.55,
        "equity": revenue * 0.85,
        "cfo": revenue * 0.19,
        "capex": revenue * 0.07,
        "cfi": -revenue * 0.06,
        "cff": -revenue * 0.04,
        "dividends_paid": -revenue * 0.04,
    }


def manufacturing_history(ticker: str = "TEST") -> List[PeriodData]:
    """Three annual periods, newest first, growing 11% a year."""
    return [
        make_period(ticker, _dt.date(2023, 12, 31), manufacturing_values(1_000_000_000_000.0)),
        make_period(ticker, _dt.date(2022, 12, 31), manufacturing_values(900_000_000_000.0)),
        make_period(ticker, _dt.date(2021, 12, 31), manufacturing_values(810_000_000_000.0)),
    ]


def bank_period(ticker: str = "BANK") -> PeriodData:
    values = {
        "revenue": 80_000_000_000_000.0,
        "net_income": 30_000_000_000_000.0,
        "total_assets": 1_300_000_000_000_000.0,
        "total_liabilities": 1_100_000_000_000_000.0,
        "equity": 200_000_000_000_000.0,
        "current_assets": 400_000_000_000_000.0,
        "current_liabilities": 900_000_000_000_000.0,
        "cfo": 20_000_000_000_000.0,
        "shares_outstanding": 150_000_000_000.0,
        "eps": 200.0,
        "interest_expense": 5_000_000_000_000.0,
    }
    return make_period(
        ticker,
        _dt.date(2023, 12, 31),
        values,
        sector="Financials",
        company_name="Test Bank Tbk",
    )
