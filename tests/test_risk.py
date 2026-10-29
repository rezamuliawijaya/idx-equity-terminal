"""Health ratios and risk flags, including bank-specific suppression."""

from __future__ import annotations

import datetime as _dt

from src.analytics.fundamentals import build_snapshot
from src.analytics.risk import add_health, detect_risk_flags
from tests.helpers import bank_period, make_period, manufacturing_history, manufacturing_values


def manufacturing_snapshot(today_price=1_000.0):
    periods = manufacturing_history()
    return build_snapshot(
        ticker="TEST",
        periods=periods,
        price=today_price,
        price_date=_dt.date(2024, 1, 2),
        sector="Industrials",
        shares_outstanding=1_000_000_000.0,
    )


def bank_snapshot(price=3_000.0):
    period = bank_period()
    return build_snapshot(
        ticker="BANK",
        periods=[period],
        price=price,
        price_date=_dt.date(2024, 1, 2),
        sector="Financials",
        shares_outstanding=150_000_000_000.0,
    )


def test_manufacturing_health_ratios_are_calculated():
    snapshot = manufacturing_snapshot()
    metrics = add_health(snapshot)
    assert metrics["current_ratio"].status == "calculated"
    assert metrics["current_ratio"].value > 1.0
    assert metrics["debt_to_equity"].status == "calculated"
    assert metrics["interest_coverage"].status == "calculated"
    assert "debt_to_equity" in metrics


def test_bank_ratios_are_not_meaningful_not_zero():
    snapshot = bank_snapshot()
    metrics = add_health(snapshot)
    for key in ("current_ratio", "quick_ratio", "debt_to_equity", "net_debt_ebitda", "interest_coverage"):
        assert metrics[key].status == "not_meaningful", key
        assert metrics[key].value is None
        assert metrics[key].note


def test_bank_flags_do_not_fire_on_structural_leverage():
    snapshot = bank_snapshot()
    snapshot.metrics.update(add_health(snapshot))
    codes = {
        flag.code
        for flag in detect_risk_flags(snapshot, today=_dt.date(2024, 2, 1))
    }
    assert "excessive_leverage" not in codes
    assert "liquidity_gap" not in codes


def test_manufacturing_leverage_flag_fires_above_threshold():
    values = manufacturing_values(1_000_000_000_000.0)
    # Push debt up so net debt / EBITDA is extreme.
    values["short_term_debt"] = 3_000_000_000_000.0
    values["long_term_debt"] = 3_000_000_000_000.0
    values["total_liabilities"] = 6_400_000_000_000.0
    values["equity"] = values["total_assets"] - values["total_liabilities"]
    period = make_period("TEST", _dt.date(2023, 12, 31), values)
    snapshot = build_snapshot(
        "TEST",
        [period],
        price=1_000.0,
        price_date=_dt.date(2024, 1, 2),
        sector="Industrials",
        shares_outstanding=1_000_000_000.0,
    )
    snapshot.metrics.update(add_health(snapshot))
    flags = detect_risk_flags(snapshot, today=_dt.date(2024, 2, 1))
    assert any(flag.code in {"excessive_leverage", "high_leverage"} for flag in flags)


def test_loss_making_flag_on_negative_earnings():
    values = manufacturing_values(1_000_000_000_000.0)
    values["net_income"] = -50_000_000_000.0
    period = make_period("TEST", _dt.date(2023, 12, 31), values)
    snapshot = build_snapshot(
        "TEST",
        [period],
        price=1_000.0,
        price_date=_dt.date(2024, 1, 2),
        sector="Industrials",
        shares_outstanding=1_000_000_000.0,
    )
    snapshot.metrics.update(add_health(snapshot))
    flags = detect_risk_flags(snapshot, today=_dt.date(2024, 2, 1))
    assert any(flag.code == "loss_making" for flag in flags)


def test_stale_and_missing_price_flags():
    snapshot = manufacturing_snapshot()
    snapshot.price = None
    stale = detect_risk_flags(snapshot, today=_dt.date(2026, 1, 1))
    codes = {flag.code for flag in stale}
    assert "no_price" in codes
    assert "stale_fundamentals" in codes
    stale_flag = next(flag for flag in stale if flag.code == "stale_fundamentals")
    assert stale_flag.severity == "critical"
    assert stale_flag.critical
