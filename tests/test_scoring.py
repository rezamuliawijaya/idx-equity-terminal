"""Scoring primitives, sector profiles and the decision thresholds."""

from __future__ import annotations

import pytest

from config.settings import DecisionPolicy, ScoringWeights
from src.analytics.risk import RiskFlag
from src.analytics.scoring import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_HOLD,
    DECISION_INSUFFICIENT,
    DECISION_SELL,
    Component,
    decide,
    interpolate_score,
    profile_components,
    score_component,
)


def test_interpolate_score_is_piecewise_linear_and_clamped():
    points = ((0.0, 0.0), (0.10, 50.0), (0.20, 100.0))
    assert interpolate_score(-1.0, points) == 0.0
    assert interpolate_score(0.05, points) == pytest.approx(25.0)
    assert interpolate_score(0.15, points) == pytest.approx(75.0)
    assert interpolate_score(1.0, points) == 100.0


def test_component_direction_reflects_the_breakpoints():
    higher = Component("roe", "ROE", ((0.0, 0.0), (0.2, 100.0)))
    lower = Component("pe", "P/E", ((5.0, 100.0), (30.0, 0.0)))
    assert higher.direction == "higher_is_better"
    assert lower.direction == "lower_is_better"


def test_score_component_returns_none_for_missing_input():
    component = Component("roe", "ROE", ((0.0, 0.0), (0.2, 100.0)))
    assert score_component(component, None) is None


def test_score_component_clamps_explosive_growth():
    component = Component(
        "revenue_growth",
        "Revenue growth",
        ((-0.5, 0.0), (0.0, 40.0), (0.15, 80.0), (0.5, 100.0)),
    )
    assert score_component(component, 25.0) == 100.0
    assert score_component(component, -10.0) == 0.0


def test_default_weights_sum_to_one():
    weights = ScoringWeights().normalized().as_dict()
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["quality"] == pytest.approx(0.30)
    assert weights["valuation"] == pytest.approx(0.30)


def test_weights_are_renormalised_after_editing():
    weights = ScoringWeights(quality=2, growth=1, valuation=1, health=1, momentum=1).normalized().as_dict()
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["quality"] == pytest.approx(1 / 3)


def test_bank_profile_never_scores_ev_ebitda_or_current_ratio():
    bank = profile_components("bank")
    bank_metrics = {
        component.metric_key
        for factor in bank.values()
        for component in factor
    }
    assert "ev_ebitda" not in bank_metrics
    assert "current_ratio" not in bank_metrics
    assert "debt_to_equity" not in bank_metrics
    # Banks are scored on P/B, earnings multiples and capital/returns.
    assert {"pb", "pe", "roe"}.issubset(bank_metrics)

    manufacturing = profile_components("manufacturing")
    manufacturing_metrics = {
        component.metric_key
        for factor in manufacturing.values()
        for component in factor
    }
    assert "ev_ebitda" in manufacturing_metrics
    assert "current_ratio" in manufacturing_metrics


def test_mining_profile_differs_from_generic():
    mining = profile_components("mining")
    generic = profile_components("generic")
    mining_quality = {component.metric_key for component in mining["quality"]}
    generic_quality = {component.metric_key for component in generic["quality"]}
    assert mining_quality != generic_quality


# --------------------------------------------------------------------------- #
# Decision thresholds
# --------------------------------------------------------------------------- #


def run_decide(
    score=70.0,
    confidence=0.80,
    coverage=1.0,
    flags=(),
    margin_of_safety=0.20,
    expected_return=0.10,
    price_available=True,
    policy=None,
):
    return decide(
        score,
        confidence,
        coverage,
        policy or DecisionPolicy(),
        list(flags),
        margin_of_safety,
        expected_return,
        price_available,
    )


def test_buy_requires_score_margin_and_confidence():
    decision, reasons, vetoes = run_decide()
    assert decision == DECISION_BUY
    assert not vetoes
    assert reasons


def test_buy_is_blocked_when_margin_of_safety_is_too_thin():
    decision, _, _ = run_decide(margin_of_safety=0.05)
    assert decision == DECISION_HOLD


def test_buy_is_blocked_when_confidence_is_below_buy_threshold():
    decision, _, _ = run_decide(confidence=0.60)
    assert decision == DECISION_HOLD


def test_buy_is_blocked_without_a_price():
    decision, _, _ = run_decide(price_available=False, margin_of_safety=None)
    assert decision == DECISION_HOLD


def test_score_between_hold_and_buy_is_hold():
    decision, _, _ = run_decide(score=50.0, margin_of_safety=0.05)
    assert decision == DECISION_HOLD


def test_score_below_hold_is_sell():
    decision, _, _ = run_decide(score=40.0, margin_of_safety=-0.30)
    assert decision == DECISION_SELL


def test_low_confidence_is_insufficient_data():
    decision, _, vetoes = run_decide(confidence=0.30)
    assert decision == DECISION_INSUFFICIENT
    assert vetoes


def test_low_factor_coverage_is_insufficient_data():
    decision, _, _ = run_decide(coverage=0.20)
    assert decision == DECISION_INSUFFICIENT


def test_missing_score_is_insufficient_data():
    decision, _, _ = run_decide(score=None)
    assert decision == DECISION_INSUFFICIENT


def test_stale_fundamentals_block_any_action():
    flag = RiskFlag(code="stale_fundamentals", severity="high", message="Statements are 700 days old.")
    decision, _, vetoes = run_decide(flags=[flag])
    assert decision == DECISION_INSUFFICIENT
    assert "700 days" in " ".join(vetoes)


def test_critical_flag_forces_avoid():
    flag = RiskFlag(code="negative_equity", severity="critical", message="Equity is negative.")
    decision, _, vetoes = run_decide(flags=[flag])
    assert decision == DECISION_AVOID
    assert vetoes


def test_thresholds_are_configurable():
    policy = DecisionPolicy(buy_score=90.0)
    decision, _, _ = run_decide(score=80.0, policy=policy)
    assert decision == DECISION_HOLD
