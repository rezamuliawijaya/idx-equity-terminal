"""Point-in-time visibility, performance metrics and backtest guard rails."""

from __future__ import annotations

import datetime as _dt

import pandas as pd
import pytest

from src.analytics.backtesting import (
    BacktestConfig,
    PointInTimeFundamentals,
    performance_metrics,
)
from tests.helpers import make_period, manufacturing_values


def test_period_is_invisible_before_publication_date():
    period = make_period(
        "TEST",
        _dt.date(2023, 12, 31),
        manufacturing_values(1_000.0),
        publication_date=_dt.date(2024, 3, 31),
    )
    pit = PointInTimeFundamentals({"TEST": [period]})
    assert pit.available_periods("TEST", _dt.date(2024, 3, 30)) == []
    assert pit.available_periods("TEST", _dt.date(2024, 3, 31)) == [period]
    assert pit.available_periods("TEST", _dt.date(2024, 1, 1)) == []


def test_missing_publication_date_uses_the_filing_lag():
    period = make_period("TEST", _dt.date(2023, 12, 31), manufacturing_values(1_000.0))
    pit = PointInTimeFundamentals({"TEST": [period]}, filing_lag_days=120)
    # 2023-12-31 + 120 days = 2024-04-29.
    assert pit.available_periods("TEST", _dt.date(2024, 4, 28)) == []
    assert pit.available_periods("TEST", _dt.date(2024, 4, 29)) == [period]
    assert "TEST" in pit.missing_publication_dates


def test_snapshot_is_none_before_any_period_is_visible():
    period = make_period(
        "TEST",
        _dt.date(2023, 12, 31),
        manufacturing_values(1_000.0),
        publication_date=_dt.date(2024, 3, 31),
    )
    pit = PointInTimeFundamentals({"TEST": [period]})
    assert pit.snapshot("TEST", _dt.date(2024, 1, 1), price=100.0) is None


def test_snapshot_uses_only_visible_periods():
    early = make_period(
        "TEST",
        _dt.date(2022, 12, 31),
        manufacturing_values(900.0),
        publication_date=_dt.date(2023, 3, 31),
    )
    late = make_period(
        "TEST",
        _dt.date(2023, 12, 31),
        manufacturing_values(1_000.0),
        publication_date=_dt.date(2024, 3, 31),
    )
    pit = PointInTimeFundamentals({"TEST": [late, early]})
    snapshot = pit.snapshot("TEST", _dt.date(2023, 6, 30), price=100.0)
    assert snapshot is not None
    assert snapshot.periods == [early]


def test_performance_metrics_arithmetic():
    index = pd.to_datetime(["2024-01-02", "2024-12-31"])
    equity = pd.Series([100.0, 110.0], index=index)
    metrics = performance_metrics(equity, risk_free_rate=0.0)
    assert metrics["total_return"] == pytest.approx(0.10)
    assert metrics["max_drawdown"] == pytest.approx(0.0)
    assert metrics["start_value"] == pytest.approx(100.0)
    assert metrics["end_value"] == pytest.approx(110.0)


def test_performance_metrics_handles_short_series():
    metrics = performance_metrics(pd.Series([], dtype=float))
    assert metrics["total_return"] is None
    assert metrics["observations"] == 0


def test_max_drawdown_is_negative():
    index = pd.to_datetime(["2024-01-02", "2024-06-03", "2024-12-31"])
    equity = pd.Series([100.0, 80.0, 120.0], index=index)
    metrics = performance_metrics(equity, risk_free_rate=0.06)
    assert metrics["max_drawdown"] == pytest.approx(-0.20)


def test_config_validation_rejects_bad_windows():
    with pytest.raises(ValueError):
        BacktestConfig(start=_dt.date(2024, 1, 2), end=_dt.date(2024, 1, 1)).validate()
    with pytest.raises(ValueError):
        BacktestConfig(start=_dt.date(2024, 1, 1), end=_dt.date(2024, 2, 1), top_n=0).validate()
    with pytest.raises(ValueError):
        BacktestConfig(start=_dt.date(2024, 1, 1), end=_dt.date(2024, 2, 1), rebalance="weekly").validate()
