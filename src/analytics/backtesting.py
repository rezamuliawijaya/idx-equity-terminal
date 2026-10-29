"""Point-in-time backtesting of the decision engine.

Bias controls implemented here:

* **Look-ahead**: a fundamental period is only visible on or after its
  ``publication_date``. When no publication date was imported, a conservative
  filing lag is applied and the limitation is recorded.
* **Survivorship**: every ticker present in the price panel is considered,
  including those whose series ends early. A ticker that stops trading is
  liquidated at its last observed close and the event is recorded.
* **Costs**: commission, tax and slippage are charged on every trade.
* **Liquidity**: a ticker with no price on a rebalance date cannot be bought.

The engine never fabricates prices or fundamentals. If only synthetic fixtures
are available, the result is flagged ``synthetic=True``.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config.settings import DecisionPolicy, TerminalSettings
from src.analytics.fundamentals import build_snapshot
from src.analytics.scoring import DECISION_BUY, DECISION_HOLD
from src.data.financial_parser import PeriodData, select_series


@dataclass
class BacktestConfig:
    start: _dt.date
    end: _dt.date
    rebalance: str = "quarterly"  # monthly | quarterly
    top_n: int = 5
    initial_capital: float = 1_000_000_000.0
    buy_cost: float = 0.0015
    sell_cost: float = 0.0025
    slippage: float = 0.001
    min_score: float = 55.0
    min_confidence: float = 0.55
    include_hold: bool = False
    risk_free_rate: float = 0.06
    benchmark_label: str = "IHSG"
    filing_lag_days: int = 120
    max_stale_days: int = 550

    def validate(self) -> None:
        if self.end <= self.start:
            raise ValueError("Backtest end date must be after the start date.")
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1.")
        if self.rebalance not in {"monthly", "quarterly"}:
            raise ValueError("rebalance must be 'monthly' or 'quarterly'.")


@dataclass
class Trade:
    date: _dt.date
    ticker: str
    side: str
    price: float
    shares: float
    cost: float
    reason: str = ""


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    benchmark_curve: pd.DataFrame
    trades: List[Trade]
    metrics: Dict[str, Any]
    limitations: List[str] = field(default_factory=list)
    config: Optional[BacktestConfig] = None
    synthetic: bool = False
    rebalances: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        payload = dict(self.metrics)
        payload["trades"] = len(self.trades)
        payload["synthetic"] = self.synthetic
        return payload


# --------------------------------------------------------------------------- #
# Point-in-time fundamentals
# --------------------------------------------------------------------------- #


class PointInTimeFundamentals:
    """Serves only the statements that were publicly available on a date."""

    def __init__(self, periods_by_ticker: Dict[str, List[PeriodData]], filing_lag_days: int = 120):
        self.periods_by_ticker = periods_by_ticker
        self.filing_lag_days = filing_lag_days
        self.missing_publication_dates: set = set()

    def available_periods(self, ticker: str, as_of: _dt.date) -> List[PeriodData]:
        periods = self.periods_by_ticker.get(ticker, [])
        visible: List[PeriodData] = []
        for period in periods:
            if period.publication_date is not None:
                published = period.publication_date
            else:
                published = period.period_end + _dt.timedelta(days=self.filing_lag_days)
                self.missing_publication_dates.add(ticker)
            if published <= as_of:
                visible.append(period)
        return visible

    def snapshot(self, ticker: str, as_of: _dt.date, price: Optional[float], company: Optional[Dict[str, Any]] = None):
        visible = self.available_periods(ticker, as_of)
        if not visible:
            return None
        company = company or {}
        try:
            return build_snapshot(
                ticker=ticker,
                periods=visible,
                price=price,
                price_date=as_of,
                company_name=company.get("name"),
                sector=company.get("sector"),
                shares_outstanding=company.get("shares_outstanding"),
                is_synthetic=bool(company.get("is_synthetic")),
            )
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Price helpers
# --------------------------------------------------------------------------- #


def _pivot_prices(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    pivot = frame.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    return pivot.sort_index()


def _rebalance_dates(index: pd.DatetimeIndex, frequency: str) -> List[pd.Timestamp]:
    if index.empty:
        return []
    dates: List[pd.Timestamp] = []
    seen: set = set()
    for date in index:
        key = (date.year, date.month) if frequency == "monthly" else (date.year, (date.month - 1) // 3)
        if key not in seen:
            seen.add(key)
            dates.append(date)
    return dates


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def performance_metrics(
    equity: pd.Series,
    risk_free_rate: float = 0.06,
    periods_per_year: int = 252,
) -> Dict[str, Any]:
    if equity is None or len(equity) < 2:
        return {
            "total_return": None,
            "cagr": None,
            "annualized_volatility": None,
            "sharpe": None,
            "max_drawdown": None,
            "observations": 0,
        }
    values = equity.dropna().astype(float)
    start_value, end_value = float(values.iloc[0]), float(values.iloc[-1])
    days = max((values.index[-1] - values.index[0]).days, 1)
    years = days / 365.25
    total_return = end_value / start_value - 1.0 if start_value > 0 else None
    cagr = (end_value / start_value) ** (1 / years) - 1.0 if start_value > 0 and years > 0 else None
    returns = values.pct_change().dropna()
    volatility = float(returns.std(ddof=1) * math.sqrt(periods_per_year)) if len(returns) > 1 else None
    excess = returns - risk_free_rate / periods_per_year
    sharpe = float(excess.mean() / returns.std(ddof=1) * math.sqrt(periods_per_year)) if len(returns) > 1 and returns.std(ddof=1) > 0 else None
    running_max = values.cummax()
    drawdown = values / running_max - 1.0
    max_drawdown = float(drawdown.min()) if len(drawdown) else None
    return {
        "total_return": total_return,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "observations": int(len(values)),
        "years": years,
        "start_value": start_value,
        "end_value": end_value,
    }


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


Analyzer = Callable[[str, pd.Timestamp, float], Optional[Dict[str, Any]]]


def run_backtest(
    config: BacktestConfig,
    prices: pd.DataFrame,
    fundamentals: Dict[str, List[PeriodData]],
    companies: Optional[Dict[str, Dict[str, Any]]] = None,
    benchmark: Optional[pd.DataFrame] = None,
    settings: Optional[TerminalSettings] = None,
    analyzer: Optional[Analyzer] = None,
    synthetic: bool = False,
) -> BacktestResult:
    """Run a periodic-rebalance backtest.

    ``analyzer`` may be supplied to decouple this module from the full engine
    (used by tests). It must return a dict with at least ``score``,
    ``decision`` and ``confidence``.
    """
    config.validate()
    settings = settings or TerminalSettings()
    companies = companies or {}
    limitations: List[str] = []

    panel = _pivot_prices(prices)
    if panel.empty:
        raise ValueError("No price data supplied for the backtest period.")
    panel = panel.loc[(panel.index >= pd.Timestamp(config.start)) & (panel.index <= pd.Timestamp(config.end))]
    if panel.empty:
        raise ValueError("No price observations fall inside the requested window.")

    pit = PointInTimeFundamentals(fundamentals, config.filing_lag_days)
    dates = list(panel.index)
    rebalance_dates = set(_rebalance_dates(panel.index, config.rebalance))

    if analyzer is None:
        analyzer = _default_analyzer(pit, companies, settings)

    cash = float(config.initial_capital)
    holdings: Dict[str, float] = {}
    equity_points: List[Tuple[pd.Timestamp, float]] = []
    trades: List[Trade] = []
    rebalances: List[Dict[str, Any]] = []
    turnover_total = 0.0
    delisted: List[str] = []

    last_prices: Dict[str, float] = {}

    for date in dates:
        row = panel.loc[date]
        for ticker in panel.columns:
            price = row.get(ticker)
            if pd.notna(price):
                last_prices[ticker] = float(price)

        if date in rebalance_dates:
            tradable = {
                ticker: float(row[ticker]) for ticker in panel.columns if pd.notna(row.get(ticker))
            }
            # Liquidate positions that can no longer be priced.
            for ticker in list(holdings):
                if ticker not in tradable:
                    price = last_prices.get(ticker)
                    if price:
                        proceeds = holdings[ticker] * price * (1 - config.sell_cost - config.slippage)
                        cash += proceeds
                        trades.append(Trade(date.date(), ticker, "sell", price, holdings[ticker], holdings[ticker] * price * (config.sell_cost + config.slippage), "delisted or suspended"))
                        turnover_total += holdings[ticker] * price
                        if ticker not in delisted:
                            delisted.append(ticker)
                        del holdings[ticker]

            candidates: List[Dict[str, Any]] = []
            for ticker, price in tradable.items():
                try:
                    result = analyzer(ticker, date, price)
                except Exception:  # noqa: BLE001 - one bad ticker must not stop the run
                    continue
                if not result:
                    continue
                decision = result.get("decision")
                score = result.get("score")
                if score is None:
                    continue
                if score < config.min_score:
                    continue
                if decision != DECISION_BUY and not (config.include_hold and decision == DECISION_HOLD):
                    continue
                if result.get("confidence", 0) < config.min_confidence:
                    continue
                candidates.append(result)

            candidates.sort(key=lambda item: item.get("score") or 0, reverse=True)
            selected = candidates[: config.top_n]
            target_tickers = {item["ticker"] for item in selected}

            equity_now = cash + sum(
                holdings.get(ticker, 0.0) * tradable.get(ticker, last_prices.get(ticker, 0.0))
                for ticker in holdings
            )
            target_value = equity_now / len(selected) if selected else 0.0

            # Sell anything no longer selected.
            for ticker in list(holdings):
                if ticker in target_tickers:
                    continue
                price = tradable.get(ticker)
                if not price:
                    continue
                shares = holdings[ticker]
                gross = shares * price
                cost = gross * (config.sell_cost + config.slippage)
                cash += gross - cost
                trades.append(Trade(date.date(), ticker, "sell", price, shares, cost, "rebalance out"))
                turnover_total += gross
                holdings.pop(ticker, None)

            # Adjust selected positions toward equal weight.
            for item in selected:
                ticker = item["ticker"]
                price = tradable[ticker]
                current_shares = holdings.get(ticker, 0.0)
                current_value = current_shares * price
                delta = target_value - current_value
                if delta > 0:
                    affordable = cash / (price * (1 + config.buy_cost + config.slippage))
                    shares = min(delta / price, affordable)
                    if shares <= 0:
                        continue
                    gross = shares * price
                    cost = gross * (config.buy_cost + config.slippage)
                    cash -= gross + cost
                    holdings[ticker] = current_shares + shares
                    trades.append(Trade(date.date(), ticker, "buy", price, shares, cost, f"score {item['score']:.0f}"))
                    turnover_total += gross
                elif delta < 0 and current_shares > 0:
                    shares = min(-delta / price, current_shares)
                    gross = shares * price
                    cost = gross * (config.sell_cost + config.slippage)
                    cash += gross - cost
                    holdings[ticker] = current_shares - shares
                    trades.append(Trade(date.date(), ticker, "sell", price, shares, cost, "trim to weight"))
                    turnover_total += gross

            rebalances.append(
                {
                    "date": date.date(),
                    "holdings": {k: round(v, 2) for k, v in holdings.items() if v > 1e-9},
                    "cash": cash,
                    "selected": [item["ticker"] for item in selected],
                }
            )

        market_value = sum(
            holdings.get(ticker, 0.0) * last_prices.get(ticker, 0.0) for ticker in holdings
        )
        equity_points.append((date, cash + market_value))

    curve = pd.DataFrame(equity_points, columns=["date", "equity"]).set_index("date")
    metrics = performance_metrics(curve["equity"], config.risk_free_rate)

    benchmark_curve = pd.DataFrame()
    relative = None
    if benchmark is not None and not benchmark.empty:
        bench = benchmark.copy()
        bench["date"] = pd.to_datetime(bench["date"]).dt.normalize()
        bench = bench.set_index("date").sort_index()["close"].astype(float)
        bench = bench.loc[(bench.index >= curve.index[0]) & (bench.index <= curve.index[-1])]
        if not bench.empty:
            normalized = bench / bench.iloc[0] * config.initial_capital
            benchmark_curve = pd.DataFrame({"date": normalized.index, "benchmark": normalized.values}).set_index("date")
            bench_metrics = performance_metrics(normalized, config.risk_free_rate)
            if metrics.get("cagr") is not None and bench_metrics.get("cagr") is not None:
                relative = metrics["cagr"] - bench_metrics["cagr"]
            metrics["benchmark_cagr"] = bench_metrics.get("cagr")
            metrics["benchmark_total_return"] = bench_metrics.get("total_return")
            metrics["benchmark_max_drawdown"] = bench_metrics.get("max_drawdown")
            metrics["excess_cagr"] = relative

    years = metrics.get("years") or 1.0
    metrics["turnover_annual"] = (
        turnover_total / (2.0 * config.initial_capital * max(years, 0.01)) if config.initial_capital else None
    )
    metrics["trade_count"] = len(trades)
    metrics["period"] = f"{curve.index[0].date()} to {curve.index[-1].date()}"
    metrics["rebalances"] = len(rebalances)
    metrics["distinct_tickers"] = int(len(panel.columns))

    if pit.missing_publication_dates:
        limitations.append(
            "Some statements had no publication date; a conservative "
            f"{config.filing_lag_days}-day filing lag was applied for: "
            + ", ".join(sorted(pit.missing_publication_dates)[:8])
            + ("..." if len(pit.missing_publication_dates) > 8 else "")
        )
    if delisted:
        limitations.append(
            "Positions stopped trading and were liquidated at the last observed close: "
            + ", ".join(delisted)
            + ". Survivorship bias cannot be fully removed without complete delisting records."
        )
    if benchmark is None or benchmark.empty:
        limitations.append("No benchmark series supplied; relative performance is not reported.")
    limitations.append(
        "Fundamentals are point-in-time by publication date, but corporate actions are not adjusted "
        "unless the imported price series is already adjusted."
    )
    if synthetic:
        limitations.append("All inputs are explicitly labelled synthetic demonstration data. Results are not evidence of real-world performance.")

    return BacktestResult(
        equity_curve=curve.reset_index(),
        benchmark_curve=benchmark_curve.reset_index(),
        trades=trades,
        metrics=metrics,
        limitations=limitations,
        config=config,
        synthetic=synthetic,
        rebalances=rebalances,
    )


def _default_analyzer(
    pit: PointInTimeFundamentals,
    companies: Dict[str, Dict[str, Any]],
    settings: TerminalSettings,
) -> Analyzer:
    def analyze(ticker: str, date: pd.Timestamp, price: float) -> Optional[Dict[str, Any]]:
        snapshot = pit.snapshot(ticker, date.date(), price, companies.get(ticker))
        if snapshot is None:
            return None
        from src.analytics.profitability import add_profitability
        from src.analytics.risk import add_health, detect_risk_flags
        from src.analytics.scoring import score_snapshot
        from src.analytics.valuation import valuation_metrics

        add_profitability(snapshot)
        valuation_metrics(snapshot)
        add_health(snapshot, settings.policy)
        flags = detect_risk_flags(snapshot, settings.policy, today=date.date())
        score, coverage, _ = score_snapshot(snapshot, settings.weights)
        if score is None:
            return None
        confidence = min(0.95, coverage)
        critical = any(flag.critical for flag in flags)
        decision = "INSUFFICIENT DATA" if critical or coverage < settings.policy.min_factor_coverage else (
            DECISION_BUY if score >= settings.policy.buy_score else (DECISION_HOLD if score >= settings.policy.hold_score else "SELL / REDUCE")
        )
        return {
            "ticker": ticker,
            "score": score,
            "confidence": confidence,
            "decision": decision,
            "snapshot": snapshot,
            "flags": flags,
        }

    return analyze


# --------------------------------------------------------------------------- #
# Synthetic demonstration data
# --------------------------------------------------------------------------- #


def synthetic_market(
    tickers: Sequence[str] = ("DEMO-A", "DEMO-B", "DEMO-C", "DEMO-D", "DEMO-E"),
    start: _dt.date = _dt.date(2021, 1, 4),
    end: _dt.date = _dt.date(2024, 12, 31),
    seed: int = 7,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Generate labelled synthetic prices and an IHSG-like benchmark.

    This exists only so the backtest framework can be demonstrated without
    licensed data. Output is flagged synthetic everywhere it is displayed.
    """
    rng = np.random.default_rng(seed)
    business_days = pd.bdate_range(start, end)
    prices = []
    benchmark_rows = []

    market_returns = rng.normal(0.00035, 0.011, len(business_days))
    benchmark = 6000 * np.cumprod(1 + market_returns)
    for date, level in zip(business_days, benchmark):
        benchmark_rows.append({"date": date, "close": float(level), "label": "IHSG (synthetic)"})

    for index, ticker in enumerate(tickers):
        beta = 0.7 + 0.25 * index
        alpha = 0.0001 * (index - 2)
        idiosyncratic = rng.normal(0, 0.012 + 0.003 * index, len(business_days))
        returns = alpha + beta * market_returns + idiosyncratic
        level = (800 + 400 * index) * np.cumprod(1 + returns)
        for date, price in zip(business_days, level):
            prices.append(
                {
                    "ticker": ticker,
                    "date": date,
                    "close": float(max(price, 50.0)),
                    "volume": float(rng.integers(1_000_000, 50_000_000)),
                }
            )
    return pd.DataFrame(prices), pd.DataFrame(benchmark_rows)
