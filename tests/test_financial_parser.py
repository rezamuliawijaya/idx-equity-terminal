"""Period construction, cumulative-quarter differencing and TTM."""

from __future__ import annotations

import datetime as _dt

import pytest

from src.data.financial_parser import (
    discrete_quarters,
    select_series,
    trailing_twelve_months,
)
from tests.helpers import make_period, manufacturing_history


def _quarter(ticker, end, revenue, net_income, assets=1_000.0, basis="cumulative"):
    return make_period(
        ticker,
        end,
        {
            "revenue": revenue,
            "net_income": net_income,
            "total_assets": assets,
        },
        period_type="quarter",
        basis=basis,
    )


def cumulative_year(ticker="TEST"):
    return [
        _quarter(ticker, _dt.date(2023, 3, 31), 100.0, 10.0),
        _quarter(ticker, _dt.date(2023, 6, 30), 220.0, 22.0),
        _quarter(ticker, _dt.date(2023, 9, 30), 330.0, 33.0),
        _quarter(ticker, _dt.date(2023, 12, 31), 460.0, 46.0),
    ]


def test_cumulative_quarters_become_discrete():
    quarters = discrete_quarters(cumulative_year())
    by_end = {quarter.period_end: quarter for quarter in quarters}
    assert by_end[_dt.date(2023, 3, 31)].get("revenue") == pytest.approx(100.0)
    assert by_end[_dt.date(2023, 6, 30)].get("revenue") == pytest.approx(120.0)
    assert by_end[_dt.date(2023, 9, 30)].get("revenue") == pytest.approx(110.0)
    assert by_end[_dt.date(2023, 12, 31)].get("revenue") == pytest.approx(130.0)
    assert all(quarter.period_basis == "discrete" for quarter in quarters)


def test_discrete_conversion_does_not_difference_stock_items():
    quarters = discrete_quarters(cumulative_year())
    for quarter in quarters:
        # Balance-sheet items are point-in-time and must not be differenced.
        assert quarter.get("total_assets") == pytest.approx(1_000.0)


def test_discrete_inputs_are_passed_through():
    quarters = [
        _quarter("TEST", _dt.date(2023, 3, 31), 100.0, 10.0, basis="discrete"),
        _quarter("TEST", _dt.date(2023, 6, 30), 120.0, 12.0, basis="discrete"),
    ]
    result = discrete_quarters(quarters)
    assert {quarter.period_end: quarter.get("revenue") for quarter in result}[_dt.date(2023, 6, 30)] == 120.0


def test_ttm_sums_four_discrete_quarters():
    ttm, explanation = trailing_twelve_months(cumulative_year())
    assert ttm is not None
    assert ttm.get("revenue") == pytest.approx(460.0)
    assert ttm.get("net_income") == pytest.approx(46.0)
    assert ttm.period_type == "ttm"
    assert ttm.period_end == _dt.date(2023, 12, 31)
    assert "four discrete quarters" in explanation


def test_ttm_falls_back_to_annual_and_says_so():
    annual = manufacturing_history()
    ttm, explanation = trailing_twelve_months(annual)
    assert ttm is not None
    assert ttm.period_type == "annual"
    assert "annual" in explanation.lower()
    assert ttm.period_end == _dt.date(2023, 12, 31)


def test_ttm_without_annual_or_four_quarters_returns_none():
    partial = cumulative_year()[:2]
    ttm, explanation = trailing_twelve_months(partial, allow_annual_fallback=False)
    assert ttm is None
    assert "no usable period" in explanation.lower()


def test_select_series_filters_type_and_basis():
    mixed = manufacturing_history() + cumulative_year()
    annual = select_series(mixed, "annual")
    assert len(annual) == 3
    assert all(period.period_type == "annual" for period in annual)
    # Newest first.
    assert annual[0].period_end > annual[-1].period_end


def test_parent_only_series_is_separate():
    consolidated = manufacturing_history("TEST")
    parent = [
        make_period(
            "TEST",
            period.period_end,
            dict(period.values),
            consolidated=False,
        )
        for period in consolidated
    ]
    combined = consolidated + parent
    series = select_series(combined, "annual", consolidated=False)
    assert series
    assert all(period.is_consolidated is False for period in series)
