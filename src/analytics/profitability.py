"""Profitability and capital-efficiency metrics.

Every function takes a ``FundamentalSnapshot`` and returns ``Metric`` objects.
Nothing here mutates the snapshot's raw values, and nothing invents a missing
input: absent data becomes ``UNAVAILABLE`` with the reason attached.

Balance-sheet ratios use the average of the two most recent period-end
balances when a prior period exists, and say so in the note.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

from src.analytics.fundamentals import (
    CALCULATED,
    NOT_MEANINGFUL,
    REPORTED,
    Metric,
    FundamentalSnapshot,
    calculated,
    not_meaningful,
    unavailable,
)

DEFAULT_TAX_RATE = 0.22


def _average_balance(snapshot: FundamentalSnapshot, item: str) -> Tuple[Optional[float], str]:
    current = snapshot.values.get(item)
    if current is None:
        return None, "missing"
    candidates = [
        p for p in snapshot.periods
        if p.period_end < (snapshot.as_of or _dt.date.today()) and p.get(item) is not None
    ]
    if not candidates:
        return float(current), "ending balance (no prior period)"
    prior = max(candidates, key=lambda p: p.period_end)
    average = (float(current) + float(prior.get(item))) / 2.0  # type: ignore[arg-type]
    return average, f"average of {prior.period_end.isoformat()} and {snapshot.as_of.isoformat() if snapshot.as_of else 'latest'}"


def _margin(
    key: str,
    label: str,
    numerator: Optional[float],
    revenue: Optional[float],
    formula: str,
    note: str = "",
) -> Metric:
    if numerator is None or revenue is None:
        return unavailable(key, label, "Numerator or revenue is missing.", formula, "percent", "profitability")
    if revenue <= 0:
        return not_meaningful(key, label, "Revenue is zero or negative.", formula, "percent", "profitability")
    return calculated(
        key,
        label,
        numerator / revenue,
        unit="percent",
        formula=formula,
        inputs={"numerator": numerator, "revenue": revenue},
        note=note,
        group="profitability",
    )


def add_profitability(snapshot: FundamentalSnapshot) -> Dict[str, Metric]:
    v = snapshot.values
    revenue = v.get("revenue")
    metrics: Dict[str, Metric] = {}

    metrics["gross_margin"] = _margin(
        "gross_margin", "Gross Margin", v.get("gross_profit"), revenue, "Gross profit / revenue"
    ) if v.get("gross_profit") is not None else (
        not_meaningful(
            "gross_margin",
            "Gross Margin",
            "No cost-of-revenue / gross-profit line. Common for banks, where the concept does not apply.",
            "Gross profit / revenue",
            "percent",
            "profitability",
        )
        if snapshot.profile == "bank"
        else unavailable("gross_margin", "Gross Margin", "Gross profit and cost of revenue not reported.", "Gross profit / revenue", "percent", "profitability")
    )

    metrics["operating_margin"] = _margin(
        "operating_margin", "Operating Margin", v.get("operating_income"), revenue, "Operating income (EBIT) / revenue"
    )
    metrics["ebitda_margin"] = _margin(
        "ebitda_margin", "EBITDA Margin", v.get("ebitda"), revenue, "EBITDA / revenue"
    )
    metrics["net_margin"] = _margin(
        "net_margin", "Net Profit Margin", v.get("net_income_parent") or v.get("net_income"), revenue, "Net income / revenue"
    )

    net_income = v.get("net_income_parent") or v.get("net_income")
    assets_avg, assets_basis = _average_balance(snapshot, "total_assets")
    if net_income is None or assets_avg is None:
        metrics["roa"] = unavailable("roa", "Return on Assets", "Net income or total assets missing.", "Net income / average total assets", "percent", "profitability")
    else:
        metrics["roa"] = calculated(
            "roa",
            "Return on Assets",
            net_income / assets_avg,
            unit="percent",
            formula="Net income / average total assets",
            inputs={"net_income": net_income, "average_total_assets": assets_avg},
            note=f"Denominator: {assets_basis}.",
            group="profitability",
        )

    equity_item = "equity_parent" if v.get("equity_parent") is not None else "equity"
    equity_avg, equity_basis = _average_balance(snapshot, equity_item)
    if net_income is None or equity_avg is None:
        metrics["roe"] = unavailable("roe", "Return on Equity", "Net income or equity missing.", "Net income / average equity", "percent", "profitability")
    elif equity_avg <= 0:
        metrics["roe"] = not_meaningful(
            "roe",
            "Return on Equity",
            "Average equity is zero or negative; ROE has no useful interpretation.",
            "Net income / average equity",
            "percent",
            "profitability",
        )
    else:
        metrics["roe"] = calculated(
            "roe",
            "Return on Equity",
            net_income / equity_avg,
            unit="percent",
            formula="Net income / average equity",
            inputs={"net_income": net_income, "average_equity": equity_avg},
            note=f"Denominator: {equity_basis}.",
            group="profitability",
        )

    # ------------------------------- ROIC --------------------------------- #
    ebit = v.get("operating_income")
    tax_rate, tax_note = _effective_tax_rate(v)
    debt = v.get("total_debt")
    if debt is None and (v.get("short_term_debt") is not None or v.get("long_term_debt") is not None):
        debt = (v.get("short_term_debt") or 0.0) + (v.get("long_term_debt") or 0.0)
    equity = v.get("equity")
    cash = v.get("cash")
    if ebit is None or equity is None or cash is None or debt is None:
        metrics["roic"] = unavailable(
            "roic",
            "Return on Invested Capital",
            "Requires EBIT, total debt, equity and cash.",
            "NOPAT / average invested capital",
            "percent",
            "profitability",
        )
    else:
        invested = debt + equity - cash - (v.get("short_term_investments") or 0.0)
        invested_avg, invested_basis = _average_balance(snapshot, "total_assets")
        # Reconstruct average invested capital from the two latest balance sheets.
        prior = _prior_period(snapshot)
        if prior is not None and prior.get("equity") is not None and prior.get("cash") is not None:
            prior_debt = prior.get("total_debt")
            if prior_debt is None:
                prior_debt = (prior.get("short_term_debt") or 0.0) + (prior.get("long_term_debt") or 0.0)
            prior_invested = prior_debt + (prior.get("equity") or 0.0) - (prior.get("cash") or 0.0) - (prior.get("short_term_investments") or 0.0)
            invested_avg = (invested + prior_invested) / 2.0
            invested_basis = f"average of {prior.period_end.isoformat()} and latest"
        else:
            invested_avg = invested
            invested_basis = "ending balance (no prior period)"
        if invested_avg <= 0:
            metrics["roic"] = not_meaningful(
                "roic",
                "Return on Invested Capital",
                "Invested capital is zero or negative.",
                "NOPAT / average invested capital",
                "percent",
                "profitability",
            )
        else:
            nopat = ebit * (1.0 - tax_rate)
            metrics["roic"] = calculated(
                "roic",
                "Return on Invested Capital",
                nopat / invested_avg,
                unit="percent",
                formula="EBIT x (1 - tax rate) / average invested capital",
                inputs={
                    "ebit": ebit,
                    "tax_rate": tax_rate,
                    "average_invested_capital": invested_avg,
                },
                note=f"Invested capital = debt + equity - cash - short-term investments. {invested_basis}. {tax_note}",
                group="profitability",
            )

    # --------------------------- working capital -------------------------- #
    receivables_avg, _ = _average_balance(snapshot, "receivables")
    if receivables_avg is not None and revenue and revenue > 0:
        metrics["receivable_days"] = calculated(
            "receivable_days",
            "Receivable Days",
            receivables_avg / revenue * 365.0,
            unit="days",
            formula="Average receivables / revenue x 365",
            inputs={"average_receivables": receivables_avg, "revenue": revenue},
            group="profitability",
        )
    else:
        metrics["receivable_days"] = unavailable("receivable_days", "Receivable Days", "Receivables or revenue missing.", "Average receivables / revenue x 365", "days", "profitability")

    inventory_avg, _ = _average_balance(snapshot, "inventory")
    cogs = v.get("cost_of_revenue")
    if inventory_avg is not None and cogs and cogs > 0:
        metrics["inventory_days"] = calculated(
            "inventory_days",
            "Inventory Days",
            inventory_avg / cogs * 365.0,
            unit="days",
            formula="Average inventory / cost of revenue x 365",
            inputs={"average_inventory": inventory_avg, "cost_of_revenue": cogs},
            group="profitability",
        )
    else:
        metrics["inventory_days"] = unavailable("inventory_days", "Inventory Days", "Inventory or cost of revenue missing.", "Average inventory / cost of revenue x 365", "days", "profitability")

    # ------------------------------- banks -------------------------------- #
    if snapshot.profile == "bank":
        metrics.update(_bank_metrics(snapshot))

    snapshot.metrics.update(metrics)
    return metrics


def _prior_period(snapshot: FundamentalSnapshot):
    candidates = [p for p in snapshot.periods if p.period_end < (snapshot.as_of or _dt.date.today())]
    return max(candidates, key=lambda p: p.period_end) if candidates else None


def _effective_tax_rate(values: Dict[str, Optional[float]]) -> Tuple[float, str]:
    pretax = values.get("pretax_income")
    tax = values.get("income_tax")
    if pretax is not None and tax is not None and pretax > 0:
        rate = tax / pretax
        if 0.0 <= rate <= 0.6:
            return rate, f"Effective tax rate {rate:.1%} from reported figures."
    return DEFAULT_TAX_RATE, f"Effective tax rate unavailable; the statutory placeholder {DEFAULT_TAX_RATE:.0%} was used (estimate)."


def _bank_metrics(snapshot: FundamentalSnapshot) -> Dict[str, Metric]:
    v = snapshot.values
    metrics: Dict[str, Metric] = {}

    nii = v.get("net_interest_income")
    loans_avg, loans_basis = _average_balance(snapshot, "loans")
    if nii is not None and loans_avg and loans_avg > 0:
        metrics["net_interest_margin"] = calculated(
            "net_interest_margin",
            "Net Interest Margin",
            nii / loans_avg,
            unit="percent",
            formula="Net interest income / average gross loans",
            inputs={"net_interest_income": nii, "average_loans": loans_avg},
            note=f"Approximation using loans as earning assets ({loans_basis}).",
            group="profitability",
        )
    else:
        metrics["net_interest_margin"] = unavailable(
            "net_interest_margin",
            "Net Interest Margin",
            "Net interest income or gross loans not imported.",
            "Net interest income / average earning assets",
            "percent",
            "profitability",
        )

    opex = v.get("operating_expenses")
    income_base = None
    if nii is not None:
        income_base = nii + (v.get("other_operating_income") or 0.0)
    elif v.get("revenue") is not None:
        income_base = v.get("revenue")
    if opex is not None and income_base and income_base > 0:
        metrics["cost_to_income"] = calculated(
            "cost_to_income",
            "Cost-to-Income Ratio",
            opex / income_base,
            unit="percent",
            formula="Operating expenses / (net interest income + other operating income)",
            inputs={"operating_expenses": opex, "operating_income_base": income_base},
            group="profitability",
        )
    else:
        metrics["cost_to_income"] = unavailable(
            "cost_to_income",
            "Cost-to-Income Ratio",
            "Operating expenses or the income base is missing.",
            "Operating expenses / operating income",
            "percent",
            "profitability",
        )

    reported_npl = v.get("npl_ratio")
    npl_amount = v.get("npl")
    if reported_npl is not None:
        metrics["npl_ratio"] = Metric(
            key="npl_ratio",
            label="NPL Ratio",
            value=reported_npl,
            unit="percent",
            status=REPORTED,
            formula="Reported by the company",
            group="profitability",
        )
    elif npl_amount is not None and loans_avg and loans_avg > 0:
        metrics["npl_ratio"] = calculated(
            "npl_ratio",
            "NPL Ratio",
            npl_amount / loans_avg,
            unit="percent",
            formula="Non-performing loans / average gross loans",
            inputs={"npl": npl_amount, "average_loans": loans_avg},
            group="profitability",
        )
    else:
        metrics["npl_ratio"] = unavailable(
            "npl_ratio",
            "NPL Ratio",
            "Neither a reported NPL ratio nor NPL and loan balances were imported.",
            "Non-performing loans / gross loans",
            "percent",
            "profitability",
        )

    reported_car = v.get("car_ratio")
    if reported_car is not None:
        metrics["car_ratio"] = Metric(
            key="car_ratio",
            label="Capital Adequacy Ratio",
            value=reported_car,
            unit="percent",
            status=REPORTED,
            formula="Reported by the company",
            group="profitability",
        )
    else:
        metrics["car_ratio"] = unavailable(
            "car_ratio",
            "Capital Adequacy Ratio",
            "Not imported. CAR requires regulatory capital detail not present in summary statements.",
            "Regulatory capital / risk-weighted assets",
            "percent",
            "profitability",
        )

    return metrics
