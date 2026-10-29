"""Financial health, liquidity and risk flags.

``RiskFlag`` objects are first-class: they drive the decision engine's vetoes and
appear verbatim in recommendations and reports.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from config.settings import DecisionPolicy
from src.analytics.fundamentals import (
    CALCULATED,
    FundamentalSnapshot,
    Metric,
    calculated,
    compute_net_debt,
    not_meaningful,
    safe_ratio,
    unavailable,
)
from src.data.financial_parser import select_series

SEVERITIES = ("critical", "high", "medium", "info")


@dataclass(frozen=True)
class RiskFlag:
    code: str
    severity: str
    message: str
    metric_key: str = ""
    value: Optional[float] = None
    threshold: Optional[float] = None
    thesis_impact: str = ""

    @property
    def critical(self) -> bool:
        return self.severity == "critical"

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "metric_key": self.metric_key,
            "value": self.value,
            "threshold": self.threshold,
            "thesis_impact": self.thesis_impact,
        }


def add_health(snapshot: FundamentalSnapshot, policy: Optional[DecisionPolicy] = None) -> Dict[str, Metric]:
    """Compute balance-sheet health metrics."""
    policy = policy or DecisionPolicy()
    v = snapshot.values
    metrics: Dict[str, Metric] = {}

    current_assets = v.get("current_assets")
    current_liabilities = v.get("current_liabilities")
    if current_assets is None or current_liabilities is None:
        metrics["current_ratio"] = unavailable(
            "current_ratio", "Current Ratio", "Current assets or current liabilities missing.",
            "Current assets / current liabilities", "multiple", "health",
        )
    elif current_liabilities <= 0:
        metrics["current_ratio"] = not_meaningful(
            "current_ratio", "Current Ratio", "Current liabilities are zero or negative.",
            "Current assets / current liabilities", "multiple", "health",
        )
    else:
        metrics["current_ratio"] = calculated(
            "current_ratio", "Current Ratio", current_assets / current_liabilities,
            unit="multiple", formula="Current assets / current liabilities",
            inputs={"current_assets": current_assets, "current_liabilities": current_liabilities},
            group="health",
        )

    inventory = v.get("inventory")
    if current_assets is None or current_liabilities is None:
        metrics["quick_ratio"] = unavailable(
            "quick_ratio", "Quick Ratio", "Current assets or current liabilities missing.",
            "(Current assets - inventory) / current liabilities", "multiple", "health",
        )
    elif inventory is None:
        metrics["quick_ratio"] = unavailable(
            "quick_ratio", "Quick Ratio", "Inventory not reported; a quick ratio cannot be computed without assuming inventory is zero.",
            "(Current assets - inventory) / current liabilities", "multiple", "health",
        )
    elif current_liabilities <= 0:
        metrics["quick_ratio"] = not_meaningful(
            "quick_ratio", "Quick Ratio", "Current liabilities are zero or negative.",
            "(Current assets - inventory) / current liabilities", "multiple", "health",
        )
    else:
        metrics["quick_ratio"] = calculated(
            "quick_ratio", "Quick Ratio", (current_assets - inventory) / current_liabilities,
            unit="multiple", formula="(Current assets - inventory) / current liabilities",
            inputs={"current_assets": current_assets, "inventory": inventory, "current_liabilities": current_liabilities},
            group="health",
        )

    equity = v.get("equity")
    debt = v.get("total_debt")
    if debt is None and (v.get("short_term_debt") is not None or v.get("long_term_debt") is not None):
        debt = (v.get("short_term_debt") or 0.0) + (v.get("long_term_debt") or 0.0)
    if debt is None or equity is None:
        metrics["debt_to_equity"] = unavailable(
            "debt_to_equity", "Debt-to-Equity", "Total debt or equity missing.",
            "Total interest-bearing debt / total equity", "multiple", "health",
        )
    elif equity <= 0:
        metrics["debt_to_equity"] = not_meaningful(
            "debt_to_equity", "Debt-to-Equity", "Equity is zero or negative; leverage is effectively unbounded.",
            "Total interest-bearing debt / total equity", "multiple", "health",
        )
    else:
        metrics["debt_to_equity"] = calculated(
            "debt_to_equity", "Debt-to-Equity", debt / equity,
            unit="multiple", formula="Total interest-bearing debt / total equity",
            inputs={"total_debt": debt, "equity": equity}, group="health",
        )

    assets = v.get("total_assets")
    if equity is None or assets is None or assets <= 0:
        metrics["equity_to_assets"] = unavailable(
            "equity_to_assets", "Equity / Assets", "Equity or total assets missing.",
            "Total equity / total assets", "percent", "health",
        )
    else:
        metrics["equity_to_assets"] = calculated(
            "equity_to_assets", "Equity / Assets", equity / assets,
            unit="percent", formula="Total equity / total assets",
            inputs={"equity": equity, "total_assets": assets}, group="health",
        )

    net_debt = compute_net_debt(v)
    if net_debt is None:
        metrics["net_debt"] = unavailable(
            "net_debt", "Net Debt", "Debt or cash not reported.",
            "Total debt - cash - short-term investments", "idr", "health",
        )
    else:
        metrics["net_debt"] = calculated(
            "net_debt", "Net Debt", net_debt, unit="idr",
            formula="Total debt - cash - short-term investments",
            inputs={"total_debt": debt, "cash": v.get("cash"), "short_term_investments": v.get("short_term_investments")},
            note="A negative value indicates net cash.", group="health",
        )

    ebitda = v.get("ebitda")
    if net_debt is None or ebitda is None:
        metrics["net_debt_ebitda"] = unavailable(
            "net_debt_ebitda", "Net Debt / EBITDA", "Net debt or EBITDA missing.",
            "Net debt / EBITDA", "multiple", "health",
        )
    elif ebitda <= 0:
        metrics["net_debt_ebitda"] = not_meaningful(
            "net_debt_ebitda", "Net Debt / EBITDA",
            "EBITDA is zero or negative; the multiple is not interpretable.",
            "Net debt / EBITDA", "multiple", "health",
        )
    else:
        metrics["net_debt_ebitda"] = calculated(
            "net_debt_ebitda", "Net Debt / EBITDA", net_debt / ebitda,
            unit="multiple", formula="Net debt / EBITDA",
            inputs={"net_debt": net_debt, "ebitda": ebitda}, group="health",
        )

    ebit = v.get("operating_income")
    interest = v.get("interest_expense")
    if ebit is None or interest is None:
        metrics["interest_coverage"] = unavailable(
            "interest_coverage", "Interest Coverage", "EBIT or interest expense missing.",
            "EBIT / interest expense", "multiple", "health",
        )
    elif interest <= 0:
        metrics["interest_coverage"] = not_meaningful(
            "interest_coverage", "Interest Coverage", "No interest expense reported.",
            "EBIT / interest expense", "multiple", "health",
        )
    else:
        metrics["interest_coverage"] = calculated(
            "interest_coverage", "Interest Coverage", ebit / interest,
            unit="multiple", formula="EBIT / interest expense",
            inputs={"ebit": ebit, "interest_expense": interest}, group="health",
        )

    if snapshot.profile == "bank":
        # Bank balance sheets are structurally leveraged and their current
        # assets/liabilities are not a liquidity measure. Score them on capital
        # adequacy and asset quality instead (see `_BANK_HEALTH`).
        metrics["current_ratio"] = not_meaningful(
            "current_ratio", "Current Ratio",
            "Not meaningful for a bank: current assets and liabilities are not a liquidity measure.",
            "Current assets / current liabilities", "multiple", "health",
        )
        metrics["quick_ratio"] = not_meaningful(
            "quick_ratio", "Quick Ratio",
            "Not meaningful for a bank.",
            "(Current assets - inventory) / current liabilities", "multiple", "health",
        )
        metrics["debt_to_equity"] = not_meaningful(
            "debt_to_equity", "Debt-to-Equity",
            "Bank leverage is structural; use the capital adequacy ratio and equity/assets instead.",
            "Total interest-bearing debt / total equity", "multiple", "health",
        )
        metrics["net_debt_ebitda"] = not_meaningful(
            "net_debt_ebitda", "Net Debt / EBITDA",
            "Not meaningful for a bank: deposits and borrowings fund the loan book, they are operating inputs.",
            "Net debt / EBITDA", "multiple", "health",
        )
        metrics["interest_coverage"] = not_meaningful(
            "interest_coverage", "Interest Coverage",
            "Not meaningful for a bank: interest expense is cost of funds, not financing of the business.",
            "EBIT / interest expense", "multiple", "health",
        )

    snapshot.metrics.update(metrics)
    return metrics


# --------------------------------------------------------------------------- #
# Risk flags
# --------------------------------------------------------------------------- #


def _trend(values: Sequence[Optional[float]]) -> Optional[float]:
    """Simple slope of the last N values, normalized by the mean."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    mean = sum(clean) / len(clean)
    if mean == 0:
        return None
    return (clean[-1] - clean[0]) / abs(mean)


def detect_risk_flags(
    snapshot: FundamentalSnapshot,
    policy: Optional[DecisionPolicy] = None,
    today: Optional[_dt.date] = None,
    average_daily_value: Optional[float] = None,
    min_daily_value: float = 1_000_000_000.0,
) -> List[RiskFlag]:
    policy = policy or DecisionPolicy()
    today = today or _dt.date.today()
    flags: List[RiskFlag] = []
    v = snapshot.values

    def add(code: str, severity: str, message: str, **kwargs) -> None:
        flags.append(RiskFlag(code=code, severity=severity, message=message, **kwargs))

    # ---------------------------- data quality ---------------------------- #
    if snapshot.as_of is None:
        add("no_fundamentals", "critical", "No financial statements are stored for this company.",
            thesis_impact="No thesis can be formed.")
    else:
        age = (today - snapshot.as_of).days
        if age > policy.max_fundamental_age_days:
            add(
                "stale_fundamentals",
                "critical",
                f"Latest financial period is {age} days old (limit {policy.max_fundamental_age_days}).",
                metric_key="period_age_days",
                value=age,
                threshold=policy.max_fundamental_age_days,
                thesis_impact="Valuation and scoring would rely on stale data.",
            )
        elif age > policy.max_fundamental_age_days * 0.75:
            add(
                "aging_fundamentals",
                "medium",
                f"Latest financial period is {age} days old; check whether a newer filing exists.",
                value=age,
                thesis_impact="Figures may lag the current business.",
            )

    if snapshot.price is None:
        add("no_price", "high", "No share price is stored, so valuation and margin of safety cannot be assessed.",
            thesis_impact="Recommendation is limited to business quality.")
    elif snapshot.price_date and (today - snapshot.price_date).days > policy.max_price_age_days:
        age = (today - snapshot.price_date).days
        add("stale_price", "medium", f"Share price is {age} days old (limit {policy.max_price_age_days}).",
            value=age, threshold=policy.max_price_age_days,
            thesis_impact="Margin of safety may no longer reflect the market.")

    essential_missing = [
        item for item in ("revenue", "net_income", "total_assets", "equity", "shares_outstanding")
        if v.get(item) is None
    ]
    if essential_missing:
        add(
            "missing_essential_data",
            "critical",
            f"Essential inputs missing: {', '.join(essential_missing)}.",
            thesis_impact="Scoring and valuation would be unreliable.",
        )

    # ------------------------------ solvency ------------------------------ #
    equity = v.get("equity")
    if equity is not None and equity <= 0:
        add("negative_equity", "critical", "Total equity is zero or negative.", value=equity,
            thesis_impact="Balance-sheet insolvency; equity holders bear the first loss.")

    net_debt_ebitda = snapshot.metric_value("net_debt_ebitda")
    if snapshot.profile != "bank" and net_debt_ebitda is not None:
        if net_debt_ebitda > 5:
            add("excessive_leverage", "critical", f"Net debt / EBITDA is {net_debt_ebitda:.1f}x.",
                metric_key="net_debt_ebitda", value=net_debt_ebitda, threshold=5.0,
                thesis_impact="Refinancing risk dominates the equity story.")
        elif net_debt_ebitda > 3.5:
            add("high_leverage", "high", f"Net debt / EBITDA is {net_debt_ebitda:.1f}x.",
                metric_key="net_debt_ebitda", value=net_debt_ebitda, threshold=3.5,
                thesis_impact="Balance sheet limits resilience in a downturn.")

    coverage = snapshot.metric_value("interest_coverage")
    if coverage is not None and snapshot.profile != "bank":
        if coverage < 1.0:
            add("interest_not_covered", "critical", f"EBIT covers interest {coverage:.2f}x - operating profit does not cover interest.",
                metric_key="interest_coverage", value=coverage, threshold=1.0,
                thesis_impact="Loss-making at the operating level after financing costs.")
        elif coverage < 2.0:
            add("weak_interest_coverage", "high", f"Interest coverage is only {coverage:.2f}x.",
                metric_key="interest_coverage", value=coverage, threshold=2.0,
                thesis_impact="Little headroom if rates rise or earnings fall.")

    current_ratio = snapshot.metric_value("current_ratio")
    if current_ratio is not None and snapshot.profile != "bank" and current_ratio < 1.0:
        add("liquidity_gap", "high" if current_ratio < 0.8 else "medium",
            f"Current ratio is {current_ratio:.2f}x; short-term liabilities exceed current assets.",
            metric_key="current_ratio", value=current_ratio, threshold=1.0,
            thesis_impact="Working-capital stress or reliance on refinancing.")

    # ------------------------------ earnings ------------------------------ #
    net_income = v.get("net_income_parent") or v.get("net_income")
    if net_income is not None and net_income < 0:
        add("loss_making", "high", "The company reported a net loss for the latest period.",
            metric_key="net_income", value=net_income,
            thesis_impact="Multiples on earnings are not meaningful; valuation must rest on assets or normalised earnings.")

    annual = select_series(snapshot.periods, "annual", consolidated=snapshot.latest_period.is_consolidated if snapshot.latest_period else True)
    if len(annual) >= 3:
        revenues = [p.get("revenue") for p in annual[:3]][::-1]
        trend = _trend(revenues)
        if trend is not None and trend < -0.15:
            add("revenue_contraction", "high", "Revenue has contracted materially over the last three annual periods.",
                metric_key="revenue_trend", value=trend, threshold=-0.15,
                thesis_impact="Structural demand or market-share loss must be explained before underwriting growth.")
        nets = [p.get("net_income") for p in annual[:3]][::-1]
        etrend = _trend(nets)
        if etrend is not None and etrend < -0.30:
            add("earnings_deterioration", "high", "Net income has deteriorated materially over three annual periods.",
                metric_key="net_income_trend", value=etrend, threshold=-0.30,
                thesis_impact="Margin compression or cost creep is eroding the thesis.")

    fcf_series = [p.get("fcf") for p in annual[:3]]
    if fcf_series and all(value is not None and value < 0 for value in fcf_series):
        add("persistent_negative_fcf", "high", "Free cash flow has been negative for at least three consecutive annual periods.",
            thesis_impact="Growth is being funded externally; dilution or leverage risk is elevated.")

    payout = snapshot.metric_value("dividend_payout")
    if payout is not None and payout > 1.0:
        add("payout_above_earnings", "medium", f"Dividends paid exceeded net income ({payout:.0%} payout).",
            metric_key="dividend_payout", value=payout, threshold=1.0,
            thesis_impact="Dividend may be funded from reserves or debt.")

    cfo = v.get("cfo")
    if cfo is not None and net_income is not None and net_income > 0 and cfo < net_income * 0.5:
        add("weak_cash_conversion", "medium", "Operating cash flow is less than half of net income.",
            metric_key="cfo_to_net_income", value=(cfo / net_income), threshold=0.5,
            thesis_impact="Accounting earnings are not converting into cash.")

    # ------------------------- shares and dilution ------------------------ #
    share_history = [(p.period_end, p.get("shares_outstanding")) for p in snapshot.periods if p.get("shares_outstanding")]
    share_history.sort()
    if len(share_history) >= 2:
        first, last = share_history[0][1], share_history[-1][1]
        if first and last and abs(last / first - 1.0) > 0.05:
            change = last / first - 1.0
            add(
                "share_count_change",
                "medium" if change > 0.20 else "info",
                f"Share count changed {change:+.1%} between {share_history[0][0]} and {share_history[-1][0]} "
                "(split, rights issue, or buyback). Check per-share history before comparing EPS.",
                value=change,
                thesis_impact="Per-share metrics are not comparable across the full history without adjustment.",
            )

    # ------------------------------- liquidity ---------------------------- #
    if average_daily_value is not None and average_daily_value < min_daily_value:
        add(
            "illiquid",
            "high",
            f"Average daily traded value is about Rp {average_daily_value:,.0f}, below the Rp {min_daily_value:,.0f} threshold.",
            metric_key="average_daily_value",
            value=average_daily_value,
            threshold=min_daily_value,
            thesis_impact="Position sizing and exit are constrained; expect slippage.",
        )

    if snapshot.is_synthetic:
        add("synthetic_data", "info", "This company's data is a synthetic demonstration fixture, not a real issuer.",
            thesis_impact="Never treat any output for this ticker as investment research.")

    flags.sort(key=lambda flag: SEVERITIES.index(flag.severity) if flag.severity in SEVERITIES else 9)
    return flags


def has_critical(flags: Sequence[RiskFlag]) -> bool:
    return any(flag.critical for flag in flags)


def flags_frame(flags: Sequence[RiskFlag]):
    import pandas as pd

    return pd.DataFrame([flag.as_dict() for flag in flags], columns=["severity", "code", "message", "value", "threshold", "thesis_impact"])
