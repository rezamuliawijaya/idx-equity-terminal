"""Backtesting page.

The framework is real; the data may not be. If the stored prices and statements
are synthetic the result is labelled as such and never presented as evidence.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui import theme

from src.analytics.backtesting import BacktestConfig, run_backtest
from src.ui.components import PLOT_TEMPLATE, current_settings, empty_state, get_repos
from src.utils.formatting import format_date, format_idr, format_percent


def _default_window(repos) -> tuple[_dt.date, _dt.date]:
    frame = repos.prices.all_series()
    if frame.empty:
        today = _dt.date.today()
        return today - _dt.timedelta(days=3 * 365), today
    dates = pd.to_datetime(frame["date"])
    return dates.min().date(), dates.max().date()


def _company_map(repos) -> Dict[str, Dict[str, Any]]:
    return {company["ticker"]: company for company in repos.companies.list()}


def _equity_figure(result) -> None:
    curve = result.equity_curve.copy()
    curve["date"] = pd.to_datetime(curve["date"])
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=curve["date"],
            y=curve["equity"],
            mode="lines",
            name="Strategy",
            line=dict(color="#38bdf8", width=2),
        )
    )
    if not result.benchmark_curve.empty:
        bench = result.benchmark_curve.copy()
        bench["date"] = pd.to_datetime(bench["date"])
        figure.add_trace(
            go.Scatter(
                x=bench["date"],
                y=bench["benchmark"],
                mode="lines",
                name="Benchmark",
                line=dict(color="#f59e0b", width=1.5, dash="dot"),
            )
        )
    figure.update_layout(
        template=PLOT_TEMPLATE,
        height=380,
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis_title="Portfolio value (Rp)",
        xaxis_title="",
        legend=dict(orientation="h"),
        title="Equity curve",
    )
    st.plotly_chart(figure, width="stretch")

    values = curve["equity"].astype(float)
    running_max = values.cummax()
    drawdown = values / running_max - 1.0
    draw = go.Figure(
        go.Scatter(
            x=curve["date"],
            y=drawdown,
            fill="tozeroy",
            line=dict(color="#dc2626", width=1),
            name="Drawdown",
        )
    )
    draw.update_layout(
        template=PLOT_TEMPLATE,
        height=200,
        margin=dict(l=10, r=10, t=20, b=10),
        yaxis_title="Drawdown",
        yaxis_tickformat=".0%",
        showlegend=False,
    )
    st.plotly_chart(draw, width="stretch")


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("BT", "Backtesting")
    st.caption(
        "Point-in-time fundamentals are served by publication date, with a configurable filing lag when the "
        "publication date is missing. Costs and slippage are charged on every trade."
    )

    prices = repos.prices.all_series()
    if prices.empty:
        empty_state("No price history is stored. Import prices from Data Management before backtesting.")
        return
    start_default, end_default = _default_window(repos)
    tickers_with_data = repos.financials.all_tickers_with_data()
    if not tickers_with_data:
        empty_state("No financial statements are stored, so the engine cannot score anything.")
        return

    with st.form("backtest_config"):
        columns = st.columns(4)
        start = columns[0].date_input("Start", value=start_default, max_value=end_default)
        end = columns[1].date_input("End", value=end_default, min_value=start)
        rebalance = columns[2].selectbox("Rebalance", ["quarterly", "monthly"])
        top_n = columns[3].number_input("Positions", min_value=1, max_value=20, value=min(5, len(tickers_with_data)))

        columns = st.columns(4)
        capital = columns[0].number_input(
            "Initial capital (Rp)", min_value=1_000_000.0, value=1_000_000_000.0, step=50_000_000.0, format="%.0f"
        )
        min_score = columns[1].number_input("Minimum score", min_value=0.0, max_value=100.0, value=55.0, step=1.0)
        min_conf = columns[2].number_input(
            "Minimum confidence", min_value=0.0, max_value=1.0, value=0.55, step=0.05, format="%.2f"
        )
        include_hold = columns[3].checkbox("Include HOLD names", value=False)

        with st.expander("Costs, slippage and bias controls"):
            columns = st.columns(4)
            buy_cost = columns[0].number_input("Buy cost", min_value=0.0, max_value=0.05, value=settings.costs.buy_fee_rate, step=0.0005, format="%.4f")
            sell_cost = columns[1].number_input("Sell cost", min_value=0.0, max_value=0.05, value=settings.costs.sell_fee_rate, step=0.0005, format="%.4f")
            slippage = columns[2].number_input("Slippage", min_value=0.0, max_value=0.05, value=settings.costs.slippage_rate, step=0.0005, format="%.4f")
            risk_free = columns[3].number_input("Risk-free rate", min_value=0.0, max_value=0.30, value=0.06, step=0.005, format="%.3f")
            columns = st.columns(2)
            filing_lag = columns[0].number_input("Filing lag when publication date is missing (days)", min_value=0, max_value=365, value=120, step=15)
            max_stale = columns[1].number_input("Maximum age of the latest statement (days)", min_value=90, max_value=1200, value=550, step=30)
        benchmark_label = st.selectbox("Benchmark", ["(none)"] + repos.benchmarks.labels())
        submitted = st.form_submit_button("Run backtest", type="primary")

    if not submitted:
        st.info(
            "Configure the window and press **Run backtest**. The first run may take a few seconds because every "
            "rebalance date re-scores every ticker against point-in-time statements."
        )
        _data_notes(repos)
        return

    try:
        config = BacktestConfig(
            start=start,
            end=end,
            rebalance=rebalance,
            top_n=int(top_n),
            initial_capital=float(capital),
            buy_cost=float(buy_cost),
            sell_cost=float(sell_cost),
            slippage=float(slippage),
            min_score=float(min_score),
            min_confidence=float(min_conf),
            include_hold=bool(include_hold),
            risk_free_rate=float(risk_free),
            benchmark_label=benchmark_label,
            filing_lag_days=int(filing_lag),
            max_stale_days=int(max_stale),
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Invalid configuration: {exc}")
        return

    fundamentals = {ticker: repos.financials.periods(ticker) for ticker in tickers_with_data}
    benchmark = None
    if benchmark_label != "(none)":
        benchmark = repos.benchmarks.series(benchmark_label)
    companies = _company_map(repos)
    synthetic = any(company.get("is_synthetic") for company in companies.values())

    with st.spinner("Running backtest over stored prices and point-in-time fundamentals..."):
        try:
            result = run_backtest(
                config,
                prices,
                fundamentals,
                companies=companies,
                benchmark=benchmark,
                settings=settings,
                synthetic=synthetic,
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Backtest failed: {type(exc).__name__}: {exc}")
            return

    metrics = result.metrics
    if synthetic:
        st.warning(
            "**Synthetic data.** At least one stored company is a demonstration fixture. The mechanics are real; "
            "the performance numbers are not evidence of anything."
        )

    columns = st.columns(5)
    columns[0].metric("Total return", format_percent(metrics.get("total_return"), signed=True))
    columns[1].metric("CAGR", format_percent(metrics.get("cagr"), signed=True))
    columns[2].metric("Volatility (ann.)", format_percent(metrics.get("annualized_volatility")))
    columns[3].metric("Sharpe", f"{metrics['sharpe']:.2f}" if metrics.get("sharpe") is not None else "n/a")
    columns[4].metric("Max drawdown", format_percent(metrics.get("max_drawdown")))

    columns = st.columns(5)
    columns[0].metric("Trades", metrics.get("trade_count", 0))
    columns[1].metric("Rebalances", metrics.get("rebalances", 0))
    columns[2].metric("Annual turnover", format_percent(metrics.get("turnover_annual")))
    columns[3].metric("Benchmark CAGR", format_percent(metrics.get("benchmark_cagr"), signed=True))
    columns[4].metric("Excess CAGR", format_percent(metrics.get("excess_cagr"), signed=True))

    st.caption(
        f"Period {metrics.get('period')} | {metrics.get('observations', 0)} observations | "
        f"{metrics.get('distinct_tickers', 0)} tickers in the price panel | capital "
        f"{format_idr(config.initial_capital, compact=True)}"
    )

    _equity_figure(result)

    st.markdown("#### Trades")
    if result.trades:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Date": trade.date,
                        "Ticker": trade.ticker,
                        "Side": trade.side.upper(),
                        "Price": trade.price,
                        "Shares": trade.shares,
                        "Cost": trade.cost,
                        "Reason": trade.reason,
                    }
                    for trade in result.trades
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Price": st.column_config.NumberColumn(format="Rp %.0f"),
                "Shares": st.column_config.NumberColumn(format="%.0f"),
                "Cost": st.column_config.NumberColumn(format="Rp %.0f"),
            },
        )
        st.download_button(
            "Download trades (CSV)",
            data=pd.DataFrame([trade.__dict__ for trade in result.trades]).to_csv(index=False).encode("utf-8"),
            file_name="backtest_trades.csv",
            mime="text/csv",
        )
    else:
        st.info(
            "No trades were executed. Either no ticker cleared the score, confidence and safety filters on any "
            "rebalance date, or there was no price available on those dates."
        )

    with st.expander("Rebalance log"):
        if result.rebalances:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Date": item["date"],
                            "Selected": ", ".join(item["selected"]) or "(none)",
                            "Holdings": ", ".join(f"{k}: {v:,.0f}" for k, v in item["holdings"].items()) or "(none)",
                            "Cash": item["cash"],
                        }
                        for item in result.rebalances
                    ]
                ),
                hide_index=True,
                width="stretch",
                column_config={"Cash": st.column_config.NumberColumn(format="Rp %.0f")},
            )
        else:
            st.caption("No rebalances were recorded.")

    st.markdown("#### Limitations of this run")
    for limitation in result.limitations:
        st.markdown(f"- {limitation}")

    st.caption(
        "A backtest is a historical simulation on the data you supplied. It cannot validate a strategy that has "
        "not been tested out of sample, and survivorship bias cannot be removed without complete delisting records."
    )

    _data_notes(repos)


def _data_notes(repos) -> None:
    with st.expander("Data coverage in this database"):
        rows = []
        for ticker in repos.financials.all_tickers_with_data():
            coverage = repos.prices.coverage(ticker)
            periods = repos.financials.periods(ticker)
            rows.append(
                {
                    "Ticker": ticker,
                    "Periods": len(periods),
                    "First period": min((p.period_end for p in periods), default=None),
                    "Latest period": max((p.period_end for p in periods), default=None),
                    "Price start": coverage["start"],
                    "Price end": coverage["end"],
                    "Price observations": coverage["observations"],
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No coverage recorded.")
