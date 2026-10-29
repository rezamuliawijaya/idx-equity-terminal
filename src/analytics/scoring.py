"""Sector-aware scoring and the transparent rule-based decision engine.

Scoring is deliberately piecewise-linear and explicit: each component maps a
metric value onto 0-100 through documented breakpoints. Nothing is a black box,
and every factor shows its contribution.

The output is a ``Recommendation`` object that contains everything the UI and
reports need to explain themselves.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config.settings import DecisionPolicy, ScoringWeights, TerminalSettings
from src.analytics.fundamentals import FundamentalSnapshot, Metric
from src.analytics.risk import RiskFlag

DECISION_BUY = "BUY"
DECISION_HOLD = "HOLD"
DECISION_SELL = "SELL / REDUCE"
DECISION_AVOID = "AVOID"
DECISION_INSUFFICIENT = "INSUFFICIENT DATA"

FACTOR_LABELS = {
    "quality": "Business Quality & Profitability",
    "growth": "Growth",
    "valuation": "Valuation",
    "health": "Financial Health",
    "momentum": "Momentum & Liquidity",
}


# --------------------------------------------------------------------------- #
# Scoring primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Component:
    metric_key: str
    label: str
    points: Tuple[Tuple[float, float], ...]
    weight: float = 1.0
    note: str = ""

    @property
    def direction(self) -> str:
        first, last = self.points[0], self.points[-1]
        return "higher_is_better" if last[1] > first[1] else "lower_is_better"


def interpolate_score(value: float, points: Sequence[Tuple[float, float]]) -> float:
    """Piecewise-linear score, clamped to the first/last breakpoint."""
    ordered = sorted(points, key=lambda pair: pair[0])
    if value <= ordered[0][0]:
        return float(ordered[0][1])
    if value >= ordered[-1][0]:
        return float(ordered[-1][1])
    for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return float(y1)
            ratio = (value - x0) / (x1 - x0)
            return float(y0 + (y1 - y0) * ratio)
    return float(ordered[-1][1])


def score_component(component: Component, value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if component.metric_key in {"revenue_growth", "net_income_growth", "eps_growth", "revenue_cagr_3y", "revenue_cagr_5y"}:
        value = max(min(value, 1.0), -0.5)  # trim explosive growth from a tiny base
    return max(0.0, min(100.0, interpolate_score(value, component.points)))


# --------------------------------------------------------------------------- #
# Sector profiles
# --------------------------------------------------------------------------- #

_QUALITY_MANUFACTURING = (
    Component("roe", "Return on equity", ((0.0, 0), (0.08, 30), (0.12, 50), (0.18, 75), (0.25, 95)), 1.2,
              "Returns on book equity; distorted by leverage."),
    Component("roic", "Return on invested capital", ((0.0, 0), (0.06, 30), (0.10, 55), (0.15, 80), (0.22, 95)), 1.5,
              "Capital efficiency before financing."),
    Component("operating_margin", "Operating margin", ((0.0, 5), (0.05, 35), (0.10, 60), (0.18, 85), (0.25, 95)), 1.0),
    Component("gross_margin", "Gross margin", ((0.0, 10), (0.15, 40), (0.30, 65), (0.45, 85)), 0.8),
)

_GROWTH_STANDARD = (
    Component("revenue_cagr_3y", "3-year revenue CAGR", ((-0.05, 0), (0.0, 30), (0.08, 60), (0.15, 85), (0.25, 95)), 1.2),
    Component("revenue_growth", "Revenue growth (latest)", ((-0.10, 0), (0.0, 35), (0.08, 60), (0.15, 85), (0.25, 95)), 1.0),
    Component("net_income_growth", "Net income growth", ((-0.15, 0), (0.0, 35), (0.10, 60), (0.20, 85), (0.35, 95)), 1.0),
    Component("eps_growth", "EPS growth", ((-0.15, 0), (0.0, 35), (0.10, 60), (0.20, 85), (0.35, 95)), 0.8),
)

_VALUATION_STANDARD = (
    Component("margin_of_safety", "Margin of safety", ((-0.30, 5), (-0.10, 30), (0.0, 45), (0.10, 60), (0.20, 80), (0.35, 95)), 2.0,
              "The single most important valuation input."),
    Component("pe", "P/E", ((4, 95), (8, 80), (12, 65), (18, 45), (25, 25), (40, 8)), 1.0),
    Component("ev_ebitda", "EV/EBITDA", ((3, 95), (6, 80), (9, 60), (13, 40), (18, 20), (25, 5)), 1.0),
    Component("fcf_yield", "FCF yield", ((0.0, 20), (0.03, 50), (0.06, 75), (0.10, 90)), 0.8),
)

_HEALTH_STANDARD = (
    Component("net_debt_ebitda", "Net debt / EBITDA", ((-1, 95), (0, 90), (1, 75), (2, 60), (3, 40), (4, 20), (6, 0)), 1.2),
    Component("interest_coverage", "Interest coverage", ((0, 0), (1.5, 25), (3, 55), (6, 80), (10, 95)), 1.0),
    Component("current_ratio", "Current ratio", ((0.5, 10), (1.0, 40), (1.5, 70), (2.5, 90)), 0.8),
    Component("debt_to_equity", "Debt / equity", ((0, 95), (0.5, 80), (1.0, 60), (2.0, 30), (3.0, 10)), 0.8),
)

_MOMENTUM_STANDARD = (
    Component("price_momentum_6m", "6-month price momentum", ((-0.30, 5), (-0.10, 30), (0.0, 50), (0.15, 75), (0.30, 90)), 1.0,
              "Included as a research input, not a market-timing rule."),
    Component("liquidity_score", "Trading liquidity", ((8.0, 0), (9.0, 30), (9.5, 45), (10.0, 60), (10.5, 78), (11.0, 92)), 1.2,
              "Log10 of average daily traded value in rupiah."),
)

_BANK_QUALITY = (
    Component("roe", "Return on equity", ((0.0, 0), (0.06, 25), (0.10, 45), (0.15, 70), (0.20, 90)), 1.6,
              "Primary bank profitability measure."),
    Component("roa", "Return on assets", ((0.0, 0), (0.005, 25), (0.01, 45), (0.02, 75), (0.03, 95)), 1.2),
    Component("npl_ratio", "NPL ratio", ((0.005, 95), (0.01, 85), (0.02, 65), (0.03, 45), (0.05, 15), (0.08, 0)), 1.2,
              "Lower is better. Credit quality drives bank equity risk."),
    Component("cost_to_income", "Cost-to-income", ((0.30, 95), (0.40, 80), (0.50, 60), (0.60, 35), (0.75, 10)), 0.8),
)

_BANK_VALUATION = (
    Component("margin_of_safety", "Margin of safety", ((-0.30, 5), (-0.10, 30), (0.0, 45), (0.10, 60), (0.20, 80), (0.35, 95)), 2.0),
    Component("pb", "P/B", ((0.4, 95), (0.8, 85), (1.2, 65), (2.0, 40), (3.0, 20), (4.0, 8)), 1.2,
              "The primary bank valuation multiple."),
    Component("pe", "P/E", ((4, 95), (8, 80), (12, 65), (18, 45), (25, 25), (40, 8)), 0.8),
)

_BANK_HEALTH = (
    Component("car_ratio", "Capital adequacy ratio", ((0.10, 20), (0.14, 50), (0.18, 75), (0.22, 90)), 1.5,
              "Regulatory capital buffer."),
    Component("equity_to_assets", "Equity / assets", ((0.05, 20), (0.08, 50), (0.11, 70), (0.15, 90)), 1.0),
    Component("npl_ratio", "NPL ratio", ((0.005, 95), (0.01, 85), (0.02, 65), (0.03, 45), (0.05, 15)), 0.8),
)

_MINING_QUALITY = (
    Component("roic", "Return on invested capital", ((0.0, 0), (0.06, 30), (0.10, 55), (0.15, 80), (0.22, 95)), 1.5),
    Component("operating_margin", "Operating margin", ((-0.05, 0), (0.05, 30), (0.15, 60), (0.25, 80), (0.35, 95)), 1.2,
              "Highly sensitive to commodity prices."),
    Component("gross_margin", "Gross margin", ((0.0, 10), (0.15, 40), (0.30, 65), (0.45, 85)), 0.6),
)

_PROPERTY_QUALITY = (
    Component("roe", "Return on equity", ((0.0, 0), (0.06, 30), (0.10, 50), (0.15, 75), (0.20, 90)), 1.0),
    Component("operating_margin", "Operating margin", ((0.0, 10), (0.10, 40), (0.20, 65), (0.30, 85)), 1.0),
    Component("roic", "Return on invested capital", ((0.0, 0), (0.05, 30), (0.08, 55), (0.12, 80)), 1.2),
)


def profile_components(profile: str) -> Dict[str, Tuple[Component, ...]]:
    """Return the component set for a sector profile."""
    if profile == "bank":
        return {
            "quality": _BANK_QUALITY,
            "growth": _GROWTH_STANDARD,
            "valuation": _BANK_VALUATION,
            "health": _BANK_HEALTH,
            "momentum": _MOMENTUM_STANDARD,
        }
    if profile == "mining":
        return {
            "quality": _MINING_QUALITY,
            "growth": _GROWTH_STANDARD,
            "valuation": _VALUATION_STANDARD,
            "health": _HEALTH_STANDARD,
            "momentum": _MOMENTUM_STANDARD,
        }
    if profile == "property":
        return {
            "quality": _PROPERTY_QUALITY,
            "growth": _GROWTH_STANDARD,
            "valuation": _VALUATION_STANDARD,
            "health": _HEALTH_STANDARD,
            "momentum": _MOMENTUM_STANDARD,
        }
    if profile == "consumer":
        return {
            "quality": _QUALITY_MANUFACTURING,
            "growth": _GROWTH_STANDARD,
            "valuation": _VALUATION_STANDARD,
            "health": _HEALTH_STANDARD,
            "momentum": _MOMENTUM_STANDARD,
        }
    return {
        "quality": _QUALITY_MANUFACTURING,
        "growth": _GROWTH_STANDARD,
        "valuation": _VALUATION_STANDARD,
        "health": _HEALTH_STANDARD,
        "momentum": _MOMENTUM_STANDARD,
    }


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #


@dataclass
class FactorScore:
    key: str
    label: str
    weight: float
    score: Optional[float]
    contribution: Optional[float]
    components: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "score": self.score,
            "contribution": self.contribution,
            "components": self.components,
        }


@dataclass
class Recommendation:
    ticker: str
    company_name: str
    as_of: str
    decision: str
    score: Optional[float]
    confidence: float
    data_completeness: float
    factors: Dict[str, FactorScore]
    reasons: List[str] = field(default_factory=list)
    vetoes: List[str] = field(default_factory=list)
    flags: List[Dict[str, Any]] = field(default_factory=list)
    fair_value: Optional[float] = None
    valuation_method: str = ""
    margin_of_safety: Optional[float] = None
    expected_return: Optional[float] = None
    price: Optional[float] = None
    price_date: Optional[str] = None
    period_label: str = ""
    data_sources: List[str] = field(default_factory=list)
    principal_risks: List[str] = field(default_factory=list)
    invalidation_conditions: List[str] = field(default_factory=list)
    profile: str = "generic"
    horizon_years: float = 3.0
    assumptions_url: str = ""

    @property
    def factor_scores(self) -> Dict[str, Optional[float]]:
        return {key: factor.score for key, factor in self.factors.items()}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "as_of": self.as_of,
            "decision": self.decision,
            "score": self.score,
            "confidence": self.confidence,
            "data_completeness": self.data_completeness,
            "factors": {key: factor.as_dict() for key, factor in self.factors.items()},
            "reasons": self.reasons,
            "vetoes": self.vetoes,
            "flags": self.flags,
            "fair_value": self.fair_value,
            "valuation_method": self.valuation_method,
            "margin_of_safety": self.margin_of_safety,
            "expected_return": self.expected_return,
            "price": self.price,
            "price_date": self.price_date,
            "period_label": self.period_label,
            "data_sources": self.data_sources,
            "principal_risks": self.principal_risks,
            "invalidation_conditions": self.invalidation_conditions,
            "profile": self.profile,
            "horizon_years": self.horizon_years,
        }

    def factors_frame(self):
        import pandas as pd

        rows = []
        for factor in self.factors.values():
            rows.append(
                {
                    "factor": factor.label,
                    "weight": factor.weight,
                    "score": factor.score,
                    "contribution": factor.contribution,
                }
            )
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

#: Confidence is discounted when essential evidence is thin.
_DATA_QUALITY_PENALTIES = {
    "no_price": 0.75,
    "no_annual_history": 0.90,
    "single_period": 0.85,
    "no_source_url": 0.95,
    # Synthetic fixtures are flagged explicitly rather than being suppressed;
    # a token penalty keeps the score honest without making the demo unusable.
    "synthetic": 0.95,
}


def score_snapshot(
    snapshot: FundamentalSnapshot,
    weights: Optional[ScoringWeights] = None,
    profile: Optional[str] = None,
) -> Tuple[Optional[float], float, Dict[str, FactorScore]]:
    """Return ``(overall_score, coverage, factors)``."""
    weights = (weights or ScoringWeights()).normalized()
    components = profile_components(profile or snapshot.profile)
    weight_map = weights.as_dict()

    factors: Dict[str, FactorScore] = {}
    available_weight = 0.0
    weighted_sum = 0.0

    for factor_key in ("quality", "growth", "valuation", "health", "momentum"):
        factor_weight = weight_map.get(factor_key, 0.0)
        entries: List[Dict[str, Any]] = []
        scored: List[Tuple[float, float]] = []

        for component in components.get(factor_key, ()):
            raw = snapshot.metric_value(component.metric_key)
            component_score = score_component(component, raw)
            entries.append(
                {
                    "metric": component.metric_key,
                    "label": component.label,
                    "value": raw,
                    "score": component_score,
                    "weight": component.weight,
                    "note": component.note,
                    "direction": component.direction,
                    "breakpoints": list(component.points),
                }
            )
            if component_score is not None:
                scored.append((component_score, component.weight))

        if scored:
            total_component_weight = sum(weight for _, weight in scored)
            factor_score: Optional[float] = (
                sum(score * weight for score, weight in scored) / total_component_weight
            )
            contribution = factor_score * factor_weight
            available_weight += factor_weight
            weighted_sum += contribution
        else:
            factor_score = None
            contribution = None

        factors[factor_key] = FactorScore(
            key=factor_key,
            label=FACTOR_LABELS[factor_key],
            weight=factor_weight,
            score=factor_score,
            contribution=contribution,
            components=entries,
        )

    coverage = available_weight
    overall = weighted_sum / available_weight if available_weight > 0 else None
    return overall, coverage, factors


def compute_confidence(snapshot: FundamentalSnapshot, coverage: float, policy: DecisionPolicy) -> float:
    confidence = max(0.0, min(1.0, coverage))
    if snapshot.price is None:
        confidence *= _DATA_QUALITY_PENALTIES["no_price"]
    if snapshot.price_date and ( _dt.date.today() - snapshot.price_date).days > policy.max_price_age_days:
        confidence *= 0.95
    if not snapshot.annual:
        confidence *= _DATA_QUALITY_PENALTIES["no_annual_history"]
    elif len(snapshot.annual) < 2:
        confidence *= _DATA_QUALITY_PENALTIES["single_period"]
    if not snapshot.data_sources:
        confidence *= _DATA_QUALITY_PENALTIES["no_source_url"]
    if snapshot.is_synthetic:
        confidence *= _DATA_QUALITY_PENALTIES["synthetic"]
    return round(min(confidence, 0.95), 4)


# --------------------------------------------------------------------------- #
# Decision engine
# --------------------------------------------------------------------------- #


def decide(
    score: Optional[float],
    confidence: float,
    coverage: float,
    policy: DecisionPolicy,
    flags: Sequence[RiskFlag],
    margin_of_safety: Optional[float],
    expected_return: Optional[float],
    price_available: bool,
) -> Tuple[str, List[str], List[str]]:
    """Map scores and flags onto a recommendation. Returns decision, reasons, vetoes."""
    reasons: List[str] = []
    vetoes: List[str] = []

    data_blockers = {"no_fundamentals", "stale_fundamentals", "missing_essential_data"}
    data_flags = [flag for flag in flags if flag.code in data_blockers]
    hard_flags = [flag for flag in flags if flag.critical and flag.code not in data_blockers]

    if data_flags:
        vetoes.extend(flag.message for flag in data_flags)
        return DECISION_INSUFFICIENT, reasons, vetoes
    if score is None or coverage < policy.min_factor_coverage or confidence < policy.min_confidence:
        vetoes.append(
            f"Factor coverage {coverage:.0%} and confidence {confidence:.0%} are below the "
            f"minimum ({policy.min_factor_coverage:.0%} coverage, {policy.min_confidence:.0%} confidence)."
        )
        return DECISION_INSUFFICIENT, reasons, vetoes

    if hard_flags:
        vetoes.extend(f"{flag.severity.upper()}: {flag.message}" for flag in hard_flags)
        return DECISION_AVOID, reasons, vetoes

    reasons.append(f"Composite fundamental score is {score:.1f}/100 across available factors.")

    if price_available and margin_of_safety is not None:
        reasons.append(
            f"Estimated fair value implies a {margin_of_safety:+.1%} margin of safety at the current price."
            if margin_of_safety >= 0
            else f"The current price is {-margin_of_safety:.1%} above the estimated fair value."
        )

    buy_ready = (
        score >= policy.buy_score
        and price_available
        and margin_of_safety is not None
        and margin_of_safety >= policy.buy_min_margin_of_safety
        and confidence >= policy.buy_min_confidence
    )
    if buy_ready:
        reasons.append(
            f"Score clears the buy threshold ({policy.buy_score:.0f}) and the margin of safety clears "
            f"{policy.buy_min_margin_of_safety:.0%}."
        )
        if expected_return is not None:
            reasons.append(
                f"Scenario annual return to fair value is approximately {expected_return:.1%}; "
                "this is a scenario, not a promise."
            )
        return DECISION_BUY, reasons, vetoes

    if score >= policy.buy_score and not buy_ready:
        missing = []
        if not price_available:
            missing.append("no current price")
        elif margin_of_safety is not None and margin_of_safety < policy.buy_min_margin_of_safety:
            missing.append(f"margin of safety below {policy.buy_min_margin_of_safety:.0%}")
        if confidence < policy.buy_min_confidence:
            missing.append(f"confidence below {policy.buy_min_confidence:.0%}")
        reasons.append(
            "Business quality clears the buy threshold, but price attractiveness or evidence quality does not"
            + (f" ({'; '.join(missing)})" if missing else "")
            + "."
        )
        return DECISION_HOLD, reasons, vetoes

    if score >= policy.hold_score:
        reasons.append(
            f"Score is between the hold ({policy.hold_score:.0f}) and buy ({policy.buy_score:.0f}) thresholds: "
            "the thesis is acceptable but not compellingly priced."
        )
        return DECISION_HOLD, reasons, vetoes

    if score < policy.sell_score:
        reasons.append(
            f"Score of {score:.1f} is below the sell threshold ({policy.sell_score:.0f}); the fundamental case "
            "does not support holding at the current price."
        )
        return DECISION_SELL, reasons, vetoes

    reasons.append(
        f"Score of {score:.1f} is below the hold threshold ({policy.hold_score:.0f}); "
        "evidence does not support the current valuation."
    )
    return DECISION_SELL, reasons, vetoes


def build_recommendation(
    snapshot: FundamentalSnapshot,
    flags: Sequence[RiskFlag],
    fair_value: Optional[float],
    valuation_method: str,
    valuation_notes: Sequence[str] = (),
    settings: Optional[TerminalSettings] = None,
    horizon_years: float = 3.0,
    today: Optional[_dt.date] = None,
) -> Recommendation:
    settings = settings or TerminalSettings()
    policy = settings.policy
    today = today or _dt.date.today()

    if fair_value is None:
        method = "unavailable"
    else:
        method = valuation_method

    from src.analytics.valuation import apply_margin_of_safety, expected_return_scenario

    mos_metric = apply_margin_of_safety(snapshot, fair_value)
    margin = mos_metric.value if mos_metric else None
    dividend_yield = snapshot.metric_value("dividend_yield")
    expected = expected_return_scenario(fair_value, snapshot.price, horizon_years, dividend_yield)

    score, coverage, factors = score_snapshot(snapshot, settings.weights)
    confidence = compute_confidence(snapshot, coverage, policy)
    decision, reasons, vetoes = decide(
        score, confidence, coverage, policy, flags, margin, expected.value, snapshot.price is not None
    )

    principal_risks = [f.message for f in flags if f.severity in {"critical", "high"}][:6]
    if not principal_risks:
        principal_risks = [
            "No high-severity flag was triggered from the imported data; qualitative risks (governance, "
            "regulation, commodity exposure) are not captured by this model."
        ]

    invalidation = _invalidation_conditions(snapshot, flags, margin, method)
    factor_summary = ", ".join(
        f"{factor.label} {factor.score:.0f}" for factor in factors.values() if factor.score is not None
    )
    if factor_summary:
        reasons.append(f"Factor scores: {factor_summary}.")

    return Recommendation(
        ticker=snapshot.ticker,
        company_name=snapshot.company_name,
        as_of=_dt.datetime.now().replace(microsecond=0).isoformat(),
        decision=decision,
        score=round(score, 2) if score is not None else None,
        confidence=confidence,
        data_completeness=round(coverage, 4),
        factors=factors,
        reasons=reasons,
        vetoes=vetoes,
        flags=[flag.as_dict() for flag in flags],
        fair_value=fair_value,
        valuation_method=method,
        margin_of_safety=margin,
        expected_return=expected.value,
        price=snapshot.price,
        price_date=snapshot.price_date.isoformat() if snapshot.price_date else None,
        period_label=snapshot.period_label,
        data_sources=snapshot.data_sources,
        principal_risks=principal_risks,
        invalidation_conditions=invalidation,
        profile=snapshot.profile,
        horizon_years=horizon_years,
    )


def _invalidation_conditions(
    snapshot: FundamentalSnapshot,
    flags: Sequence[RiskFlag],
    margin: Optional[float],
    method: str,
) -> List[str]:
    conditions: List[str] = []
    roic = snapshot.metric_value("roic")
    if roic is not None:
        conditions.append(f"ROIC falling below {max(0.0, roic - 0.03):.1%} on a sustained basis would undermine the quality case.")
    growth = snapshot.metric_value("revenue_cagr_3y")
    if growth is not None:
        conditions.append(f"Revenue growth materially below the {growth:.1%} three-year trend would invalidate the growth assumption.")
    net_debt_ebitda = snapshot.metric_value("net_debt_ebitda")
    if net_debt_ebitda is not None:
        conditions.append(f"Net debt / EBITDA rising above {max(2.5, net_debt_ebitda + 1.0):.1f}x would breach the leverage case.")
    if margin is not None and margin > 0:
        conditions.append("A share-price increase that removes the margin of safety would end the entry case even if the business performs.")
    if method.startswith("DCF"):
        conditions.append("A sustained rise in the cost of capital or a cut to long-run growth assumptions would lower the DCF value.")
    for flag in flags:
        if flag.code in {"payout_above_earnings", "share_count_change"}:
            conditions.append(f"{flag.message} Revisit the thesis if this becomes structural.")
    return conditions[:6]
