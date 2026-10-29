"""DCF arithmetic, the WACC guard, blending and margin of safety."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.analytics.fundamentals import calculate_margin_of_safety
from src.analytics.valuation import (
    DCFAssumptions,
    blend_fair_value,
    expected_return_scenario,
    run_dcf,
    sensitivity_table,
)


def base_assumptions(**overrides) -> DCFAssumptions:
    payload = {
        "base_revenue": 1_000.0,
        "base_ebit_margin": 0.20,
        "operating_margin": 0.20,
        "tax_rate": 0.25,
        "revenue_growth": 0.0,
        "growth_fade": False,
        "da_pct_revenue": 0.05,
        "capex_pct_revenue": 0.04,
        "nwc_pct_revenue": 0.0,
        "wacc": 0.10,
        "terminal_growth": 0.02,
        "years": 1,
        "net_debt": 100.0,
        "minority_interest": 10.0,
        "non_operating_assets": 20.0,
        "shares": 10.0,
    }
    payload.update(overrides)
    return DCFAssumptions(**payload)


def test_dcf_single_year_arithmetic():
    # FCFF = 200 * (1 - 0.25) + 50 - 40 - 0 = 160
    # PV(FCFF) = 160 / 1.10 = 145.4545...
    # TV = 160 * 1.02 / (0.10 - 0.02) = 2040; PV = 1854.5454...
    # EV = 2000; equity = 2000 - 100 + 20 - 10 = 1910; per share = 191
    result = run_dcf(base_assumptions())
    assert result.enterprise_value == pytest.approx(2000.0, rel=1e-9)
    assert result.equity_value == pytest.approx(1910.0, rel=1e-9)
    assert result.per_share == pytest.approx(191.0, rel=1e-9)
    assert result.pv_fcff == pytest.approx(145.454545, rel=1e-6)
    assert result.pv_terminal == pytest.approx(1854.545454, rel=1e-6)
    assert result.terminal_share == pytest.approx(1854.545454 / 2000.0, rel=1e-6)


def test_dcf_schedule_reports_the_fcff_build():
    result = run_dcf(base_assumptions())
    schedule = result.frame()
    assert len(schedule) == 1
    row = schedule.iloc[0]
    assert row["ebit"] == pytest.approx(200.0)
    assert row["nopat"] == pytest.approx(150.0)
    assert row["da"] == pytest.approx(50.0)
    assert row["capex"] == pytest.approx(40.0)
    assert row["fcff"] == pytest.approx(160.0)


def test_terminal_growth_at_or_above_wacc_is_rejected():
    with pytest.raises(ValidationError):
        base_assumptions(terminal_growth=0.10)
    with pytest.raises(ValidationError):
        base_assumptions(terminal_growth=0.12)


def test_negative_terminal_growth_is_allowed():
    result = run_dcf(base_assumptions(terminal_growth=-0.01))
    assert result.enterprise_value > 0


def test_net_debt_must_be_provided():
    with pytest.raises(ValidationError):
        base_assumptions(net_debt=None)


def test_net_cash_increases_equity_value():
    with_debt = run_dcf(base_assumptions(net_debt=100.0))
    with_cash = run_dcf(base_assumptions(net_debt=-100.0))
    assert with_cash.equity_value == pytest.approx(with_debt.equity_value + 200.0)


def test_minority_interest_reduces_equity_value():
    without = run_dcf(base_assumptions(minority_interest=0.0))
    with_minority = run_dcf(base_assumptions(minority_interest=50.0))
    assert with_minority.equity_value == pytest.approx(without.equity_value - 50.0)


def test_growth_fade_reduces_near_term_growth():
    faded = run_dcf(
        base_assumptions(revenue_growth=0.20, growth_fade=True, terminal_growth=0.02, years=5, wacc=0.12)
    )
    schedule = faded.frame()
    assert schedule["growth"].iloc[0] > schedule["growth"].iloc[-1]
    assert schedule["growth"].iloc[-1] == pytest.approx(0.02, abs=1e-9)


def test_sensitivity_table_marks_invalid_cells_blank():
    # WACC 3% shifted down to 2% equals terminal growth, so the model is invalid there.
    tight = base_assumptions(wacc=0.03, terminal_growth=0.02)
    table = sensitivity_table(tight, wacc_deltas=(-0.01,), growth_deltas=(0.0,))
    assert table.shape == (1, 2)
    assert table.iloc[0, 1] is None


def test_blend_renormalises_across_available_methods():
    value, weights = blend_fair_value(100.0, [50.0, 60.0], dcf_weight=0.6, relative_weight=0.4)
    assert value == pytest.approx(100 * 0.6 + 55 * 0.4)
    assert weights == {"dcf": 0.6, "relative": 0.4}

    value, weights = blend_fair_value(None, [50.0, 60.0])
    assert value == pytest.approx(55.0)
    assert weights == {"relative": 1.0}

    value, weights = blend_fair_value(100.0, [])
    assert value == pytest.approx(100.0)
    assert weights == {"dcf": 1.0}


def test_blend_ignores_non_positive_methods():
    value, weights = blend_fair_value(-5.0, [0.0, None])
    assert value is None
    assert weights == {}


def test_margin_of_safety_definition():
    assert calculate_margin_of_safety(100.0, 80.0) == pytest.approx(0.20)
    assert calculate_margin_of_safety(100.0, 120.0) == pytest.approx(-0.20)
    assert calculate_margin_of_safety(0.0, 10.0) is None
    assert calculate_margin_of_safety(-10.0, 10.0) is None
    assert calculate_margin_of_safety(None, 10.0) is None


def test_expected_return_is_labelled_a_scenario():
    metric = expected_return_scenario(133.1, 100.0, horizon_years=3.0)
    assert metric.status == "estimated"
    assert metric.value == pytest.approx(0.10, rel=1e-6)
    assert "not a forecast" in metric.note

    missing = expected_return_scenario(None, 100.0)
    assert missing.value is None
    assert missing.status == "unavailable"

    non_positive = expected_return_scenario(-1.0, 100.0)
    assert non_positive.status == "not_meaningful"
