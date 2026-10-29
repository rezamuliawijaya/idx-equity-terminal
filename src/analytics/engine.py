"""Analysis facade.

One entry point that turns stored data into a complete, explainable assessment:
metrics, risk flags, valuation, score and recommendation. The UI and the report
builder both call this module so they can never disagree.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from config.settings import (
    DCFDefaults,
    DecisionPolicy,
    TerminalSettings,
    resolve_profile,
)
from src.analytics.fundamentals import (
    FundamentalSnapshot,
    Metric,
    build_snapshot,
    calculated,
    unavailable,
)
from src.analytics.profitability import add_profitability
from src.analytics.risk import RiskFlag, add_health, detect_risk_flags
from src.analytics.scoring import (
    Recommendation,
    build_recommendation,
    score_snapshot,
)
from src.analytics.valuation import (
    DCFResult,
    RelativeResult,
    apply_margin_of_safety,
    blend_fair_value,
    build_dcf_assumptions,
    dcf_applicable,
    expected_return_scenario,
    justified_pb_value,
    relative_valuation,
    run_dcf,
    valuation_metrics,
)
from src.data.financial_parser import PeriodData

SCREENER_METRICS = (
    "revenue_growth",
    "revenue_cagr_3y",
    "roe",
    "roic",
    "net_margin",
    "operating_margin",
    "debt_to_equity",
    "net_debt_ebitda",
    "pe",
    "pb",
    "ev_ebitda",
    "fcf_yield",
    "dividend_yield",
    "margin_of_safety",
    "current_ratio",
    "market_cap",
    "average_daily_value",
)


@dataclass
class CompanyAnalysis:
    snapshot: FundamentalSnapshot
    flags: List[RiskFlag]
    dcf: Optional[DCFResult]
    dcf_assumptions: Optional[Any]
    relative: List[RelativeResult]
    fair_value: Optional[float]
    valuation_method: str
    valuation_notes: List[str]
    recommendation: Recommendation
    peers_used: int = 0
    horizon_years: float = 3.0

    # convenience ---------------------------------------------------------- #
    @property
    def score(self) -> Optional[float]:
        return self.recommendation.score

    @property
    def decision(self) -> str:
        return self.recommendation.decision

    def metric(self, key: str) -> Optional[Metric]:
        return self.snapshot.metrics.get(key)

    def value(self, key: str) -> Optional[float]:
        metric = self.snapshot.metrics.get(key)
        return metric.numeric if metric else None

    def factor(self, key: str):
        return self.recommendation.factors.get(key)

    def screener_row(self, settings: TerminalSettings) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "ticker": self.snapshot.ticker,
            "name": self.snapshot.company_name,
            "sector": self.snapshot.sector,
            "profile": self.snapshot.profile,
            "as_of": self.snapshot.as_of.isoformat() if self.snapshot.as_of else None,
            "price": self.snapshot.price,
            "price_date": self.snapshot.price_date.isoformat() if self.snapshot.price_date else None,
            "decision": self.recommendation.decision,
            "score": self.recommendation.score,
            "confidence": self.recommendation.confidence,
            "data_completeness": self.recommendation.data_completeness,
            "fair_value": self.fair_value,
            "flags": len([f for f in self.flags if f.severity in {"critical", "high"}]),
            "is_synthetic": self.snapshot.is_synthetic,
        }
        for key in SCREENER_METRICS:
            row[key] = self.value(key)
        row["status"] = {
            key: (self.metric(key).status if self.metric(key) else "unavailable") for key in SCREENER_METRICS
        }
        return row

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.snapshot.ticker,
            "company_name": self.snapshot.company_name,
            "sector": self.snapshot.sector,
            "profile": self.snapshot.profile,
            "period": self.snapshot.period_label,
            "metrics": {key: metric.as_dict() for key, metric in self.snapshot.metrics.items()},
            "flags": [flag.as_dict() for flag in self.flags],
            "dcf": self.dcf.as_dict() if self.dcf else None,
            "relative": [item.as_dict() for item in self.relative],
            "fair_value": self.fair_value,
            "valuation_method": self.valuation_method,
            "recommendation": self.recommendation.as_dict(),
        }


# --------------------------------------------------------------------------- #
# Market context metrics
# --------------------------------------------------------------------------- #


def add_market_context(
    snapshot: FundamentalSnapshot,
    price_history: Optional[pd.DataFrame] = None,
    average_daily_value: Optional[float] = None,
) -> None:
    """Momentum and liquidity, both derived from stored price history only."""
    if price_history is not None and not price_history.empty and "close" in price_history:
        series = price_history.dropna(subset=["close"]).sort_values("date")
        if len(series) >= 60:
            latest = float(series["close"].iloc[-1])
            window = series[series["date"] <= series["date"].iloc[-1] - pd.Timedelta(days=182)]
            base = float(window["close"].iloc[-1]) if not window.empty else float(series["close"].iloc[0])
            if base > 0:
                momentum = latest / base - 1.0
                snapshot.metrics["price_momentum_6m"] = calculated(
                    "price_momentum_6m",
                    "6-Month Price Momentum",
                    momentum,
                    unit="percent",
                    formula="Latest close / close ~6 months earlier - 1",
                    inputs={"latest": latest, "six_months_ago": base},
                    group="momentum",
                )
        if "volume" in series and "close" in series:
            recent = series.tail(30)
            values = []
            if "value" in recent and recent["value"].notna().any():
                values = [float(v) for v in recent["value"].dropna()]
            else:
                values = [
                    float(row["volume"]) * float(row["close"])
                    for _, row in recent.iterrows()
                    if pd.notna(row.get("volume")) and pd.notna(row.get("close"))
                ]
            if values and average_daily_value is None:
                average_daily_value = sum(values) / len(values)

    if average_daily_value is not None and average_daily_value > 0:
        snapshot.metrics["average_daily_value"] = calculated(
            "average_daily_value",
            "Average Daily Traded Value",
            average_daily_value,
            unit="idr",
            formula="Mean daily traded value over the last 30 observations",
            group="momentum",
        )
        snapshot.metrics["liquidity_score"] = calculated(
            "liquidity_score",
            "Liquidity Score (log10 rupiah)",
            math.log10(max(average_daily_value, 1.0)),
            unit="score",
            formula="log10(average daily traded value in IDR)",
            inputs={"average_daily_value": average_daily_value},
            note="Used only inside the momentum factor; not a valuation input.",
            group="momentum",
        )
    else:
        snapshot.metrics.setdefault(
            "price_momentum_6m",
            unavailable(
                "price_momentum_6m",
                "6-Month Price Momentum",
                "Not enough stored price history.",
                "Latest close / close ~6 months earlier - 1",
                "percent",
                "momentum",
            ),
        )
        snapshot.metrics.setdefault(
            "liquidity_score",
            unavailable(
                "liquidity_score",
                "Liquidity Score",
                "No volume or traded-value history stored.",
                "log10(average daily traded value in IDR)",
                "score",
                "momentum",
            ),
        )


# --------------------------------------------------------------------------- #
# Core analysis
# --------------------------------------------------------------------------- #


def analyze_company(
    ticker: str,
    periods: Sequence[PeriodData],
    price: Optional[float] = None,
    price_date: Optional[_dt.date] = None,
    price_history: Optional[pd.DataFrame] = None,
    company: Optional[Dict[str, Any]] = None,
    settings: Optional[TerminalSettings] = None,
    peer_multiples: Optional[Dict[str, Optional[float]]] = None,
    peer_sample_sizes: Optional[Dict[str, int]] = None,
    peer_count: int = 0,
    horizon_years: float = 3.0,
    dcf_overrides: Optional[Dict[str, Any]] = None,
    today: Optional[_dt.date] = None,
) -> CompanyAnalysis:
    settings = settings or TerminalSettings()
    today = today or _dt.date.today()
    company = company or {}

    snapshot = build_snapshot(
        ticker=ticker,
        periods=periods,
        price=price,
        price_date=price_date,
        company_name=company.get("name"),
        sector=company.get("sector"),
        shares_outstanding=company.get("shares_outstanding"),
        is_synthetic=bool(company.get("is_synthetic")),
    )

    add_profitability(snapshot)
    valuation_metrics(snapshot)
    add_health(snapshot, settings.policy)
    average_daily_value = None
    add_market_context(snapshot, price_history, average_daily_value)
    daily_value = snapshot.metric_value("average_daily_value")

    flags = detect_risk_flags(
        snapshot,
        policy=settings.policy,
        today=today,
        average_daily_value=daily_value,
    )

    # ----------------------------- valuation ------------------------------ #
    notes: List[str] = []
    applicable, reason = dcf_applicable(snapshot.profile)
    dcf_result: Optional[DCFResult] = None
    assumptions = None
    if applicable:
        assumptions, build_notes = build_dcf_assumptions(snapshot, settings.dcf, dcf_overrides)
        notes.extend(build_notes)
        if assumptions is not None:
            try:
                dcf_result = run_dcf(assumptions)
                notes.extend(dcf_result.warnings)
            except ValueError as exc:
                notes.append(f"DCF rejected: {exc}")
                dcf_result = None
        else:
            notes.append("DCF unavailable: " + "; ".join(build_notes))
    else:
        notes.append(reason)

    relative: List[RelativeResult] = []
    if peer_multiples:
        relative = relative_valuation(
            snapshot, peer_multiples, peer_sample_sizes or {}, source=f"sector peers (n={peer_count})"
        )
    else:
        relative = relative_valuation(snapshot, {}, {}, source="no peers")

    if snapshot.profile == "bank":
        justified, justified_note, implied_pb = justified_pb_value(
            snapshot,
            cost_of_equity=settings.dcf.wacc + 0.02,
            terminal_growth=settings.dcf.terminal_growth,
        )
        notes.append(justified_note)
        if justified is not None:
            relative.append(
                RelativeResult(
                    method="Justified P/B",
                    multiple_name="pb_justified",
                    median_multiple=implied_pb,
                    implied_per_share=justified,
                    sample_size=1,
                    applicable=True,
                    reason=justified_note,
                    source="model",
                )
            )

    relative_values = [item.implied_per_share for item in relative if item.implied_per_share]
    dcf_value = dcf_result.per_share if dcf_result else None
    fair_value, blend_weights = blend_fair_value(
        dcf_value, relative_values, settings.dcf_weight, settings.relative_weight
    )

    if fair_value is not None:
        components = []
        if "dcf" in blend_weights:
            components.append(f"DCF {blend_weights['dcf']:.0%}")
        if "relative" in blend_weights:
            components.append(f"relative multiples {blend_weights['relative']:.0%}")
        method = f"Blended fair value ({' + '.join(components)})"
    else:
        method = "unavailable"

    if snapshot.price is None:
        notes.append("No share price stored, so margin of safety and expected return are unavailable.")
    if not relative_values and not dcf_result:
        notes.append("No valuation method produced a usable value; margin of safety is unavailable.")

    apply_margin_of_safety(snapshot, fair_value)

    recommendation = build_recommendation(
        snapshot=snapshot,
        flags=flags,
        fair_value=fair_value,
        valuation_method=method,
        valuation_notes=notes,
        settings=settings,
        horizon_years=horizon_years,
        today=today,
    )

    return CompanyAnalysis(
        snapshot=snapshot,
        flags=flags,
        dcf=dcf_result,
        dcf_assumptions=assumptions,
        relative=relative,
        fair_value=fair_value,
        valuation_method=method,
        valuation_notes=notes,
        recommendation=recommendation,
        peers_used=peer_count,
        horizon_years=horizon_years,
    )


# --------------------------------------------------------------------------- #
# Repository-bound helpers
# --------------------------------------------------------------------------- #


def analyze_ticker(repos, ticker: str, settings: Optional[TerminalSettings] = None, **kwargs) -> Optional[CompanyAnalysis]:
    """Load everything needed for one ticker and analyse it."""
    settings = settings or TerminalSettings()
    periods = repos.financials.periods(ticker)
    if not periods:
        return None
    company = repos.companies.get(ticker) or {}
    latest_price = repos.prices.latest(ticker)
    price_history = repos.prices.series(ticker)

    peer_multiples, sample_sizes, peer_count = sector_peer_multiples(repos, ticker, company.get("sector"), settings)
    return analyze_company(
        ticker=ticker,
        periods=periods,
        price=latest_price["close"] if latest_price else None,
        price_date=latest_price["date"] if latest_price else None,
        price_history=price_history,
        company=company,
        settings=settings,
        peer_multiples=peer_multiples,
        peer_sample_sizes=sample_sizes,
        peer_count=peer_count,
        **kwargs,
    )


def _multiples_for(repos, ticker: str) -> Dict[str, Optional[float]]:
    periods = repos.financials.periods(ticker)
    if not periods:
        return {}
    company = repos.companies.get(ticker) or {}
    latest_price = repos.prices.latest(ticker)
    try:
        snapshot = build_snapshot(
            ticker=ticker,
            periods=periods,
            price=latest_price["close"] if latest_price else None,
            price_date=latest_price["date"] if latest_price else None,
            company_name=company.get("name"),
            sector=company.get("sector"),
            shares_outstanding=company.get("shares_outstanding"),
            is_synthetic=bool(company.get("is_synthetic")),
        )
    except ValueError:
        return {}
    add_profitability(snapshot)
    metrics = valuation_metrics(snapshot)
    return {key: metrics[key].numeric if key in metrics else None for key in ("pe", "pb", "ev_ebitda", "ev_sales")}


def sector_peer_multiples(
    repos, ticker: str, sector: Optional[str], settings: TerminalSettings
) -> Tuple[Dict[str, Optional[float]], Dict[str, int], int]:
    """Median multiples of same-sector peers (excluding the ticker itself)."""
    import statistics

    if not sector:
        return {}, {}, 0
    tickers = [row["ticker"] for row in repos.companies.by_sector(sector) if row["ticker"] != ticker]
    tickers = [t for t in dict.fromkeys(tickers) if t != ticker]
    peers: List[Dict[str, Optional[float]]] = []
    for peer in tickers[:25]:
        multiples = _multiples_for(repos, peer)
        if multiples:
            peers.append(multiples)
    if not peers:
        return {}, {}, 0

    result: Dict[str, Optional[float]] = {}
    sizes: Dict[str, int] = {}
    for key in ("pe", "pb", "ev_ebitda", "ev_sales"):
        values = [p.get(key) for p in peers if p.get(key) is not None and 0 < float(p[key]) < 200]  # type: ignore[arg-type]
        if values:
            result[key] = float(statistics.median(values))  # type: ignore[arg-type]
            sizes[key] = len(values)
        else:
            result[key] = None
            sizes[key] = 0
    return result, sizes, len(peers)


def quick_scores(
    repos, settings: Optional[TerminalSettings] = None, tickers: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Two-pass batch scoring used by the screener.

    Pass 1 collects multiples for every ticker so sector medians can be built,
    pass 2 scores each ticker against those medians. This avoids O(n^2) queries.
    """
    settings = settings or TerminalSettings()
    tickers = list(tickers or repos.financials.all_tickers_with_data())
    rows: List[Dict[str, Any]] = []
    for ticker in tickers:
        try:
            analysis = analyze_ticker(repos, ticker, settings)
        except Exception as exc:  # noqa: BLE001 - a single bad ticker must not break the screen
            rows.append(
                {
                    "ticker": ticker,
                    "name": ticker,
                    "sector": None,
                    "decision": "INSUFFICIENT DATA",
                    "score": None,
                    "confidence": 0.0,
                    "data_completeness": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if analysis is None:
            continue
        row = analysis.screener_row(settings)
        row["analysis"] = analysis
        rows.append(row)
    return rows
