"""Valuation: unlevered DCF, relative multiples, margin of safety, sensitivity.

The DCF is explicit and reproducible: every assumption used is returned with
the result, and invalid assumptions raise rather than silently producing a
number. Nothing here claims an intrinsic value is objectively correct.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.settings import DCFDefaults, EV_MULTIPLE_PROFILES
from src.analytics.fundamentals import (
    FundamentalSnapshot,
    Metric,
    calculate_margin_of_safety,
    compute_net_debt,
)
from src.data.normalization import humanize


# --------------------------------------------------------------------------- #
# DCF
# --------------------------------------------------------------------------- #


class DCFAssumptions(BaseModel):
    """Unlevered free-cash-flow assumptions, all in absolute rupiah."""

    model_config = ConfigDict(extra="forbid")

    base_revenue: float = Field(gt=0, description="Trailing revenue in IDR")
    base_ebit_margin: float = Field(ge=-1.0, le=1.0)
    operating_margin: float = Field(ge=-1.0, le=1.0)
    tax_rate: float = Field(ge=0.0, le=0.6)
    revenue_growth: float = Field(ge=-0.5, le=1.0)
    growth_fade: bool = True
    da_pct_revenue: float = Field(ge=0.0, le=0.5)
    capex_pct_revenue: float = Field(ge=0.0, le=1.0)
    nwc_pct_revenue: float = Field(ge=-0.5, le=1.0)
    wacc: float = Field(gt=0.0, le=0.5)
    terminal_growth: float = Field(ge=-0.05, le=0.15)
    years: int = Field(ge=1, le=20)
    net_debt: Optional[float] = None
    minority_interest: float = 0.0
    non_operating_assets: float = 0.0
    shares: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_coherence(self) -> "DCFAssumptions":
        if self.terminal_growth >= self.wacc:
            raise ValueError(
                f"Terminal growth ({self.terminal_growth:.2%}) must be below WACC ({self.wacc:.2%}); "
                "otherwise the terminal value is infinite or negative."
            )
        if self.net_debt is None:
            raise ValueError("Net debt is required to bridge enterprise value to equity value.")
        return self


@dataclass
class DCFResult:
    assumptions: DCFAssumptions
    schedule: List[Dict[str, float]]
    pv_fcff: float
    pv_terminal: float
    enterprise_value: float
    equity_value: float
    per_share: Optional[float]
    terminal_share: float
    warnings: List[str] = field(default_factory=list)
    method: str = "DCF (unlevered FCFF)"

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.schedule)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "assumptions": self.assumptions.model_dump(),
            "schedule": self.schedule,
            "pv_fcff": self.pv_fcff,
            "pv_terminal": self.pv_terminal,
            "enterprise_value": self.enterprise_value,
            "equity_value": self.equity_value,
            "per_share": self.per_share,
            "terminal_share": self.terminal_share,
            "warnings": self.warnings,
            "method": self.method,
        }


def run_dcf(assumptions: DCFAssumptions) -> DCFResult:
    """Project FCFF, discount it, and bridge to equity value per share."""
    # Guard here as well as in the model: ``model_copy`` and direct construction
    # in a sensitivity grid can bypass Pydantic validation, and a terminal growth
    # at or above WACC produces a meaningless (or infinite) terminal value.
    if assumptions.terminal_growth >= assumptions.wacc:
        raise ValueError(
            f"Terminal growth ({assumptions.terminal_growth:.2%}) must be below WACC "
            f"({assumptions.wacc:.2%}); otherwise the terminal value is infinite or negative."
        )
    margin = assumptions.operating_margin
    growth_start = assumptions.revenue_growth
    growth_end = assumptions.terminal_growth
    years = assumptions.years
    warnings: List[str] = []

    schedule: List[Dict[str, float]] = []
    revenue_prior = assumptions.base_revenue
    pv_fcff = 0.0

    for year in range(1, years + 1):
        if assumptions.growth_fade and years > 1:
            weight = (year - 1) / (years - 1)
            growth = growth_start + (growth_end - growth_start) * weight
        else:
            growth = growth_start
        revenue = revenue_prior * (1.0 + growth)
        ebit = revenue * margin
        nopat = ebit * (1.0 - assumptions.tax_rate)
        da = revenue * assumptions.da_pct_revenue
        capex = revenue * assumptions.capex_pct_revenue
        delta_nwc = (revenue - revenue_prior) * assumptions.nwc_pct_revenue
        fcff = nopat + da - capex - delta_nwc
        discount = 1.0 / ((1.0 + assumptions.wacc) ** year)
        present = fcff * discount
        pv_fcff += present
        schedule.append(
            {
                "year": year,
                "growth": growth,
                "revenue": revenue,
                "ebit": ebit,
                "nopat": nopat,
                "da": da,
                "capex": capex,
                "delta_nwc": delta_nwc,
                "fcff": fcff,
                "discount_factor": discount,
                "pv_fcff": present,
            }
        )
        revenue_prior = revenue

    final_fcff = schedule[-1]["fcff"]
    terminal_fcff = final_fcff * (1.0 + assumptions.terminal_growth)
    terminal_value = terminal_fcff / (assumptions.wacc - assumptions.terminal_growth)
    pv_terminal = terminal_value / ((1.0 + assumptions.wacc) ** years)

    enterprise_value = pv_fcff + pv_terminal
    if enterprise_value <= 0:
        warnings.append(
            "Enterprise value is zero or negative: the assumptions imply a business that destroys value. "
            "Treat the per-share result as not meaningful."
        )

    equity_value = (
        enterprise_value
        - float(assumptions.net_debt)
        + assumptions.non_operating_assets
        - assumptions.minority_interest
    )
    per_share = equity_value / assumptions.shares if assumptions.shares > 0 else None
    terminal_share = pv_terminal / enterprise_value if enterprise_value else 0.0

    if terminal_share > 0.80:
        warnings.append(
            f"{terminal_share:.0%} of enterprise value comes from the terminal value. "
            "The result is highly sensitive to the long-run growth assumption."
        )
    if assumptions.operating_margin != assumptions.base_ebit_margin:
        warnings.append(
            f"Margin assumption {assumptions.operating_margin:.1%} differs from the trailing margin "
            f"{assumptions.base_ebit_margin:.1%}."
        )
    if per_share is not None and per_share <= 0:
        warnings.append("Implied equity value per share is negative; equity holders would receive nothing in this scenario.")

    return DCFResult(
        assumptions=assumptions,
        schedule=schedule,
        pv_fcff=pv_fcff,
        pv_terminal=pv_terminal,
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        per_share=per_share,
        terminal_share=terminal_share,
        warnings=warnings,
    )


def dcf_applicable(profile: str) -> Tuple[bool, str]:
    if profile == "bank":
        return False, (
            "Unlevered FCFF is not appropriate for banks: interest income and interest expense are "
            "operating items, so enterprise value is not a meaningful construct. Use P/B, P/E and ROE."
        )
    return True, ""


def build_dcf_assumptions(
    snapshot: FundamentalSnapshot,
    defaults: Optional[DCFDefaults] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[DCFAssumptions], List[str]]:
    """Derive assumptions from imported data; return ``(model, blocking_notes)``."""
    defaults = defaults or DCFDefaults()
    notes: List[str] = []
    value = snapshot.values
    revenue = value.get("revenue")
    shares = snapshot.shares_outstanding

    if revenue is None or revenue <= 0:
        return None, ["Revenue is missing or non-positive; a DCF cannot be built."]
    if not shares or shares <= 0:
        return None, ["Shares outstanding are unavailable; per-share value cannot be computed."]

    ebit = value.get("operating_income")
    if ebit is None:
        return None, ["Operating income (EBIT) is missing; a DCF cannot be built."]
    base_margin = ebit / revenue

    da = value.get("depreciation_amortization")
    if da is not None and revenue > 0:
        da_pct = max(0.0, da / revenue)
    else:
        da_pct = 0.03
        notes.append("Depreciation and amortisation were not reported; 3.0% of revenue was assumed.")

    capex = value.get("capex")
    if capex is not None and revenue > 0:
        capex_pct = max(0.0, abs(capex) / revenue)
    else:
        capex_pct = da_pct * 1.2
        notes.append("Capital expenditure was not reported; it was estimated at 1.2x D&A.")

    net_debt = compute_net_debt(value)
    if net_debt is None:
        return None, ["Net debt cannot be computed (debt or cash missing); the enterprise-to-equity bridge is unsafe."]

    from src.analytics.profitability import _effective_tax_rate

    tax_rate, tax_note = _effective_tax_rate(value)
    if "placeholder" in tax_note:
        notes.append(tax_note)

    payload: Dict[str, Any] = {
        "base_revenue": float(revenue),
        "base_ebit_margin": float(base_margin),
        "operating_margin": float(base_margin),
        "tax_rate": float(tax_rate),
        "revenue_growth": float(defaults.revenue_growth),
        "growth_fade": bool(defaults.fade_growth_to_terminal),
        "da_pct_revenue": float(da_pct),
        "capex_pct_revenue": float(capex_pct),
        "nwc_pct_revenue": float(defaults.nwc_pct_revenue_change),
        "wacc": float(defaults.wacc),
        "terminal_growth": float(defaults.terminal_growth),
        "years": int(defaults.projection_years),
        "net_debt": float(net_debt),
        "minority_interest": float(value.get("minority_interest") or 0.0),
        "non_operating_assets": float(value.get("long_term_investments") or 0.0),
        "shares": float(shares),
    }
    if defaults.operating_margin is not None:
        payload["operating_margin"] = float(defaults.operating_margin)
    if defaults.capex_pct_revenue is not None:
        payload["capex_pct_revenue"] = float(defaults.capex_pct_revenue)
    if defaults.da_pct_revenue is not None:
        payload["da_pct_revenue"] = float(defaults.da_pct_revenue)
    if overrides:
        payload.update({k: v for k, v in overrides.items() if v is not None})

    try:
        return DCFAssumptions(**payload), notes
    except ValueError as exc:
        return None, [str(exc)]


def sensitivity_table(
    assumptions: DCFAssumptions,
    wacc_deltas: Sequence[float] = (-0.02, -0.01, 0.0, 0.01, 0.02),
    growth_deltas: Sequence[float] = (-0.01, -0.005, 0.0, 0.005, 0.01),
) -> pd.DataFrame:
    """Per-share value across WACC and terminal-growth variations."""
    rows = []
    for dw in wacc_deltas:
        row: Dict[str, Any] = {"wacc": assumptions.wacc + dw}
        for dg in growth_deltas:
            try:
                variant = assumptions.model_copy(update={"wacc": assumptions.wacc + dw, "terminal_growth": assumptions.terminal_growth + dg})
                result = run_dcf(variant)
                row[f"{assumptions.terminal_growth + dg:.1%}"] = result.per_share
            except ValueError:
                row[f"{assumptions.terminal_growth + dg:.1%}"] = None
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Relative valuation
# --------------------------------------------------------------------------- #


@dataclass
class RelativeResult:
    method: str
    multiple_name: str
    median_multiple: Optional[float]
    implied_per_share: Optional[float]
    sample_size: int = 0
    applicable: bool = True
    reason: str = ""
    source: str = "peers"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "multiple": self.multiple_name,
            "median_multiple": self.median_multiple,
            "implied_per_share": self.implied_per_share,
            "sample_size": self.sample_size,
            "applicable": self.applicable,
            "reason": self.reason,
            "source": self.source,
        }


def relative_valuation(
    snapshot: FundamentalSnapshot,
    multiples: Dict[str, Optional[float]],
    sample_sizes: Optional[Dict[str, int]] = None,
    source: str = "peers",
) -> List[RelativeResult]:
    """Apply peer/sector median multiples to the company's own fundamentals."""
    sample_sizes = sample_sizes or {}
    v = snapshot.values
    shares = snapshot.shares_outstanding
    results: List[RelativeResult] = []

    def finish(method: str, name: str, multiple: Optional[float], value: Optional[float], reason: str = "") -> None:
        results.append(
            RelativeResult(
                method=method,
                multiple_name=name,
                median_multiple=multiple,
                implied_per_share=value if value is not None and value > 0 else None,
                sample_size=sample_sizes.get(name, 0),
                applicable=value is not None and value > 0,
                reason=reason,
                source=source,
            )
        )

    if not shares or shares <= 0:
        for method, name in (
            ("P/E", "pe"), ("P/B", "pb"), ("EV/EBITDA", "ev_ebitda"), ("EV/Sales", "ev_sales")
        ):
            finish(method, name, multiples.get(name), None, "Shares outstanding unavailable.")
        return results

    # P/E
    eps = (v.get("net_income_parent") or v.get("net_income"))
    pe = multiples.get("pe")
    if eps is None or pe is None:
        finish("P/E", "pe", pe, None, "Net income or peer P/E unavailable.")
    elif eps <= 0:
        finish("P/E", "pe", pe, None, "Negative earnings: P/E is not meaningful.")
    else:
        finish("P/E", "pe", pe, pe * (eps / shares))

    # P/B
    equity = v.get("equity_parent") if v.get("equity_parent") is not None else v.get("equity")
    pb = multiples.get("pb")
    if equity is None or pb is None:
        finish("P/B", "pb", pb, None, "Equity or peer P/B unavailable.")
    elif equity <= 0:
        finish("P/B", "pb", pb, None, "Negative book value: P/B is not meaningful.")
    else:
        finish("P/B", "pb", pb, pb * (equity / shares))

    # EV/EBITDA (not for banks)
    ebitda = v.get("ebitda")
    ev_ebitda = multiples.get("ev_ebitda")
    net_debt = compute_net_debt(v)
    if snapshot.profile == "bank":
        finish("EV/EBITDA", "ev_ebitda", ev_ebitda, None, "Enterprise-value multiples do not apply to banks.")
    elif ebitda is None or ev_ebitda is None or net_debt is None:
        finish("EV/EBITDA", "ev_ebitda", ev_ebitda, None, "EBITDA, net debt or peer multiple unavailable.")
    elif ebitda <= 0:
        finish("EV/EBITDA", "ev_ebitda", ev_ebitda, None, "Negative EBITDA: the multiple is not meaningful.")
    else:
        equity_value = ev_ebitda * ebitda - net_debt
        finish("EV/EBITDA", "ev_ebitda", ev_ebitda, equity_value / shares)

    # EV/Sales (not for banks)
    revenue = v.get("revenue")
    ev_sales = multiples.get("ev_sales")
    if snapshot.profile == "bank":
        finish("EV/Sales", "ev_sales", ev_sales, None, "Enterprise-value multiples do not apply to banks.")
    elif revenue is None or ev_sales is None or net_debt is None:
        finish("EV/Sales", "ev_sales", ev_sales, None, "Revenue, net debt or peer multiple unavailable.")
    elif revenue <= 0:
        finish("EV/Sales", "ev_sales", ev_sales, None, "Revenue is zero or negative.")
    else:
        equity_value = ev_sales * revenue - net_debt
        finish("EV/Sales", "ev_sales", ev_sales, equity_value / shares)

    return results


def peer_medians(peer_metrics: Sequence[Dict[str, Optional[float]]]) -> Tuple[Dict[str, Optional[float]], Dict[str, int]]:
    """Median multiple across peers, ignoring unavailable and non-positive values."""
    import statistics

    fields = {"pe": "pe", "pb": "pb", "ev_ebitda": "ev_ebitda", "ev_sales": "ev_sales"}
    medians: Dict[str, Optional[float]] = {}
    sizes: Dict[str, int] = {}
    for name, key in fields.items():
        values = [m.get(key) for m in peer_metrics if m.get(key) is not None and m.get(key) > 0]  # type: ignore[operator]
        if values:
            medians[name] = float(statistics.median(values))
            sizes[name] = len(values)
        else:
            medians[name] = None
            sizes[name] = 0
    return medians, sizes


# --------------------------------------------------------------------------- #
# Blending and margin of safety
# --------------------------------------------------------------------------- #


def blend_fair_value(
    dcf_per_share: Optional[float],
    relative_values: Sequence[Optional[float]],
    dcf_weight: float = 0.6,
    relative_weight: float = 0.4,
) -> Tuple[Optional[float], Dict[str, float]]:
    """Weighted blend of the methods that actually produced a value."""
    relative_values = [value for value in relative_values if value is not None and value > 0]
    relative = sum(relative_values) / len(relative_values) if relative_values else None

    components: Dict[str, float] = {}
    if dcf_per_share is not None and dcf_per_share > 0:
        components["dcf"] = dcf_weight
    if relative is not None:
        components["relative"] = relative_weight
    total = sum(components.values())
    if total <= 0:
        return None, {}

    blended = 0.0
    if "dcf" in components:
        blended += dcf_per_share * components["dcf"] / total  # type: ignore[operator]
    if "relative" in components:
        blended += relative * components["relative"] / total  # type: ignore[operator]
    return blended, {key: value / total for key, value in components.items()}


def expected_return_scenario(
    fair_value: Optional[float],
    price: Optional[float],
    horizon_years: float = 3.0,
    dividend_yield: Optional[float] = None,
) -> Metric:
    """Annualised re-rating scenario. Explicitly a scenario, not a forecast."""
    if fair_value is None or price is None or price <= 0 or horizon_years <= 0:
        return Metric(
            key="expected_return",
            label="Expected Annual Return (scenario)",
            value=None,
            unit="percent",
            status="unavailable",
            formula="(fair value / price)^(1/horizon) - 1 + dividend yield",
            note="Fair value, price or horizon unavailable.",
            group="valuation",
        )
    if fair_value <= 0:
        return Metric(
            key="expected_return",
            label="Expected Annual Return (scenario)",
            value=None,
            unit="percent",
            status="not_meaningful",
            formula="(fair value / price)^(1/horizon) - 1 + dividend yield",
            note="Fair value is non-positive.",
            group="valuation",
        )
    capital = (fair_value / price) ** (1.0 / horizon_years) - 1.0
    total = capital + (dividend_yield or 0.0)
    return Metric(
        key="expected_return",
        label="Expected Annual Return (scenario)",
        value=total,
        unit="percent",
        status="estimated",
        formula="(fair value / price)^(1/horizon) - 1 + dividend yield",
        inputs={"fair_value": fair_value, "price": price, "horizon_years": horizon_years, "dividend_yield": dividend_yield},
        note=(
            "Scenario assuming the share price converges to the estimated fair value over the horizon. "
            "It is not a forecast or a guarantee."
        ),
        group="valuation",
    )


def justified_pb_value(
    snapshot: FundamentalSnapshot,
    cost_of_equity: float = 0.13,
    terminal_growth: float = 0.04,
) -> Tuple[Optional[float], str, Optional[float]]:
    """Gordon-growth justified P/B, the standard fallback for bank valuation.

    Returns ``(value_per_share, note, implied_pb)``.
    """
    roe = snapshot.metric_value("roe")
    equity = snapshot.values.get("equity_parent")
    if equity is None:
        equity = snapshot.values.get("equity")
    shares = snapshot.shares_outstanding
    if roe is None:
        return None, "ROE is unavailable, so a justified P/B cannot be derived.", None
    if not shares or not equity or equity <= 0:
        return None, "Book equity or share count is unavailable.", None
    if cost_of_equity <= terminal_growth:
        return None, "Cost of equity must exceed long-run growth.", None

    implied_pb = (roe - terminal_growth) / (cost_of_equity - terminal_growth)
    if implied_pb <= 0:
        return None, (
            f"ROE ({roe:.1%}) is below the assumed long-run growth rate ({terminal_growth:.1%}); "
            "the justified P/B is not positive."
        ), implied_pb
    book_per_share = equity / shares
    return implied_pb * book_per_share, (
        f"Justified P/B {implied_pb:.2f}x from ROE {roe:.1%}, cost of equity {cost_of_equity:.1%}, "
        f"long-run growth {terminal_growth:.1%}."
    ), implied_pb


def valuation_metrics(snapshot: FundamentalSnapshot) -> Dict[str, Metric]:
    """Market-based multiples for the company itself."""
    v = snapshot.values
    price = snapshot.price
    shares = snapshot.shares_outstanding
    metrics: Dict[str, Metric] = {}

    def add_market(key: str, label: str, value: Optional[float], formula: str, unit: str = "multiple", inputs: Optional[Dict] = None, na_reason: str = "") -> None:
        if value is None:
            metrics[key] = Metric(key=key, label=label, value=None, unit=unit, status="unavailable",
                                  formula=formula, note="Price or required fundamental input missing.", group="valuation")
        elif value <= 0 and unit == "multiple":
            metrics[key] = Metric(key=key, label=label, value=None, unit=unit, status="not_meaningful",
                                  formula=formula, note=na_reason or "Non-positive denominator.", group="valuation")
        else:
            metrics[key] = Metric(key=key, label=label, value=value, unit=unit, status="calculated",
                                  formula=formula, inputs=inputs or {}, group="valuation")

    if price is None or not shares:
        for key, label, formula in (
            ("market_cap", "Market Capitalisation", "Price x shares outstanding"),
            ("pe", "P/E", "Price / earnings per share"),
            ("pb", "P/B", "Price / book value per share"),
            ("ev_ebitda", "EV/EBITDA", "Enterprise value / EBITDA"),
            ("ev_sales", "EV/Sales", "Enterprise value / revenue"),
            ("fcf_yield", "FCF Yield", "Free cash flow per share / price"),
            ("dividend_yield", "Dividend Yield", "Dividends per share / price"),
        ):
            add_market(key, label, None, formula)
        return metrics

    market_cap = price * shares
    metrics["market_cap"] = Metric(
        key="market_cap", label="Market Capitalisation", value=market_cap, unit="idr", status="calculated",
        formula="Price x shares outstanding", inputs={"price": price, "shares": shares}, group="valuation",
    )

    net_income = v.get("net_income_parent") or v.get("net_income")
    add_market("pe", "P/E", market_cap / net_income if net_income else None,
               "Market capitalisation / net income (equivalently price / EPS)",
               inputs={"market_cap": market_cap, "net_income": net_income},
               na_reason="Negative or zero earnings: P/E is not meaningful.")

    equity = v.get("equity_parent") if v.get("equity_parent") is not None else v.get("equity")
    add_market("pb", "P/B", market_cap / equity if equity else None,
               "Market capitalisation / equity",
               inputs={"market_cap": market_cap, "equity": equity},
               na_reason="Negative or zero book value: P/B is not meaningful.")

    net_debt = compute_net_debt(v)
    ev = market_cap + net_debt if net_debt is not None else None
    ebitda = v.get("ebitda")
    if snapshot.profile == "bank":
        metrics["ev_ebitda"] = Metric(key="ev_ebitda", label="EV/EBITDA", value=None, status="not_meaningful",
                                      formula="Enterprise value / EBITDA",
                                      note="Not applicable to banks: debt is an operating input.", group="valuation")
    else:
        add_market("ev_ebitda", "EV/EBITDA", (ev / ebitda) if ev and ebitda else None,
                   "Enterprise value / EBITDA", inputs={"enterprise_value": ev, "ebitda": ebitda},
                   na_reason="Negative EBITDA: the multiple is not meaningful.")
    revenue = v.get("revenue")
    add_market("ev_sales", "EV/Sales", (ev / revenue) if ev and revenue else None,
               "Enterprise value / revenue", inputs={"enterprise_value": ev, "revenue": revenue})

    fcf = v.get("fcf")
    add_market("fcf_yield", "FCF Yield", (fcf / shares) / price if fcf is not None else None,
               "Free cash flow per share / price", unit="percent",
               inputs={"free_cash_flow": fcf, "shares": shares, "price": price})

    dividends = v.get("dividends_paid")
    add_market("dividend_yield", "Dividend Yield", (abs(dividends) / shares) / price if dividends is not None else None,
               "Dividends per share / price", unit="percent",
               inputs={"dividends_paid": dividends, "shares": shares, "price": price})

    snapshot.metrics.update(metrics)
    return metrics


def apply_margin_of_safety(snapshot: FundamentalSnapshot, fair_value: Optional[float]) -> Optional[Metric]:
    """Record margin of safety against the current price."""
    if fair_value is None or snapshot.price is None:
        metric = Metric(
            key="margin_of_safety",
            label="Margin of Safety",
            value=None,
            unit="percent",
            status="unavailable",
            formula="(Fair value - price) / Fair value",
            note="Fair value or price unavailable.",
            group="valuation",
        )
        snapshot.metrics[metric.key] = metric
        return metric
    value = calculate_margin_of_safety(fair_value, snapshot.price)
    metric = Metric(
        key="margin_of_safety",
        label="Margin of Safety",
        value=value,
        unit="percent",
        status="estimated",
        formula="(Fair value - price) / Fair value",
        inputs={"fair_value": fair_value, "price": snapshot.price},
        note="A negative value means the price is above the estimated fair value.",
        group="valuation",
    )
    snapshot.metrics[metric.key] = metric
    return metric
