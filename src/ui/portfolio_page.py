"""Paper portfolio page.

Simulated only: orders are written to the local SQLite ledger. No broker
connection exists, and no order is ever transmitted anywhere.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui import theme

from src.portfolio.paper_portfolio import PaperPortfolio, check_sector_limit
from src.ui.components import PLOT_TEMPLATE, current_settings, empty_state, get_repos, request_page
from src.utils.formatting import format_date, format_idr, format_percent


def _portfolio(repos, settings) -> PaperPortfolio:
    return PaperPortfolio(repos.portfolio, settings.costs)


def _price_map(repos, portfolio: PaperPortfolio) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for position in portfolio.positions():
        latest = repos.prices.latest(position.ticker)
        if latest:
            prices[position.ticker] = latest["close"]
    return prices


def _sectors(repos, portfolio: PaperPortfolio) -> Dict[str, str]:
    output: Dict[str, str] = {}
    for position in portfolio.positions():
        company = repos.companies.get(position.ticker)
        output[position.ticker] = (company or {}).get("sector") or "Unclassified"
    return output


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    costs = settings.costs
    portfolio = _portfolio(repos, settings)
    theme.page_header("PORT", "Paper Portfolio")
    st.caption(
        "Simulated transactions only. Lot size, fees, taxes and position limits are configurable on the "
        "Data Management page. No broker connection exists in this application."
    )

    prices = _price_map(repos, portfolio)
    sectors = _sectors(repos, portfolio)
    summary = portfolio.summary(prices, sectors)

    columns = st.columns(5)
    columns[0].metric("Total value", format_idr(summary["total_value"], compact=True))
    columns[1].metric("Cash", format_idr(summary["cash"], compact=True))
    columns[2].metric("Market value", format_idr(summary["market_value"], compact=True))
    columns[3].metric("Total return", format_percent(summary["total_return"], signed=True))
    columns[4].metric("Unrealized P&L", format_idr(summary["unrealized_pnl"], compact=True))

    st.caption(
        f"Initial cash {format_idr(summary['initial_cash'], compact=True)} | realised P&L "
        f"{format_idr(summary['realized_pnl'], compact=True)} | lot size {costs.lot_size} shares | "
        f"buy fee {costs.buy_fee_rate:.2%} | sell fee {costs.sell_fee_rate:.2%} (includes sales tax as configured) | "
        f"slippage {costs.slippage_rate:.2%}"
    )

    tab_positions, tab_trade, tab_ledger, tab_settings = st.tabs(
        ["Positions", "Place a simulated order", "Transaction ledger", "Portfolio settings"]
    )

    with tab_positions:
        if not summary["positions"]:
            st.info("No open positions. Use the order tab to simulate a purchase.")
            if st.button("Open Company Research to look for candidates"):
                request_page("Company Research")
                st.rerun()
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Ticker": position["ticker"],
                            "Sector": position["sector"],
                            "Shares": position["shares"],
                            "Average cost": position["average_cost"],
                            "Last price": position["last_price"],
                            "Cost basis": position["cost_basis"],
                            "Market value": position["market_value"],
                            "Unrealized P&L": position["unrealized_pnl"],
                            "Unrealized %": position["unrealized_pct"],
                        }
                        for position in summary["positions"]
                    ]
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Shares": st.column_config.NumberColumn(format="%.0f"),
                    "Average cost": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Last price": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Cost basis": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Market value": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Unrealized P&L": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Unrealized %": st.column_config.NumberColumn(format="%.2f%%"),
                },
            )

            allocation = pd.DataFrame(summary["allocation"])
            figure = go.Figure(
                data=[
                    go.Pie(
                        labels=allocation["ticker"],
                        values=allocation["value"],
                        hole=0.45,
                        textinfo="label+percent",
                    )
                ]
            )
            if summary["cash"] > 0:
                figure.add_trace(
                    go.Pie(
                        labels=["Cash"],
                        values=[summary["cash"]],
                        hole=0.45,
                        textinfo="label+percent",
                        marker=dict(colors=["#334155"]),
                    )
                )
            figure.update_layout(
                template=PLOT_TEMPLATE,
                height=320,
                margin=dict(l=10, r=10, t=30, b=10),
                title="Allocation",
                showlegend=True,
            )
            st.plotly_chart(figure, width="stretch")

            concentration = portfolio.sector_concentration(prices, sectors)
            st.markdown("##### Sector concentration")
            st.dataframe(
                pd.DataFrame(
                    [{"Sector": sector, "Weight": weight} for sector, weight in concentration.items()]
                ),
                hide_index=True,
                width="stretch",
                column_config={"Weight": st.column_config.NumberColumn(format="%.1f%%")},
            )
            warnings = check_sector_limit(summary["allocation"], costs)
            for warning in warnings:
                st.warning(warning)

            max_weight = max((item["weight"] for item in summary["allocation"]), default=0.0)
            if max_weight > costs.max_position_pct:
                st.warning(
                    f"Largest position is {max_weight:.0%} of the portfolio, above the "
                    f"{costs.max_position_pct:.0%} single-position limit."
                )
            drawdown = portfolio.drawdown()
            if drawdown is not None:
                st.metric("Maximum drawdown (saved snapshots)", format_percent(drawdown))
            if st.button("Save equity snapshot for today"):
                portfolio.snapshot(prices=prices)
                repos.conn.commit()
                st.success("Snapshot saved. Drawdown tracking uses these snapshots.")

    with tab_trade:
        available = repos.companies.tickers()
        if not available:
            empty_state("No companies stored. Import data first.")
        else:
            side = st.radio("Side", ["Buy", "Sell"], horizontal=True)
            columns = st.columns(3)
            ticker = columns[0].selectbox("Ticker", available, key="trade_ticker")
            latest = repos.prices.latest(ticker)
            default_price = float(latest["close"]) if latest else 0.0
            price = columns[1].number_input(
                "Price (Rp)",
                min_value=0.0,
                value=default_price,
                step=10.0,
                format="%.0f",
                key=f"trade_price_{ticker}",
            )
            lots = columns[2].number_input("Lots", min_value=1, value=1, step=1, key="trade_lots")

            st.caption(
                f"One lot is {costs.lot_size} shares at the configured lot size. "
                f"Price defaults to the latest stored close"
                + (f" ({format_date(latest['date'])})" if latest else " — none stored.")
            )
            if latest and _dt.date.today() - latest["date"] > _dt.timedelta(days=settings.policy.max_price_age_days):
                st.warning(
                    f"The latest stored price is {format_date(latest['date'])}. Simulating a trade at a stale "
                    "price will distort portfolio performance."
                )

            shares = int(lots) * costs.lot_size
            gross = shares * price
            if side == "Buy":
                fees = max(costs.min_fee, gross * costs.buy_fee_rate)
                st.write(
                    f"Estimated gross {format_idr(gross, compact=True)} + fees "
                    f"{format_idr(fees, compact=True)} = **{format_idr(gross + fees, compact=True)}**"
                )
            else:
                fees = max(costs.min_fee, gross * costs.sell_fee_rate)
                st.write(
                    f"Estimated gross {format_idr(gross, compact=True)} - fees "
                    f"{format_idr(fees, compact=True)} = **{format_idr(gross - fees, compact=True)}** proceeds"
                )

            note = st.text_input("Note", value="", key="trade_note")
            if st.button("Record simulated transaction", type="primary"):
                if price <= 0:
                    st.error("A positive price is required.")
                elif side == "Buy":
                    outcome = portfolio.buy(
                        ticker,
                        price,
                        int(lots),
                        trade_date=_dt.date.today(),
                        note=note,
                        price_map=prices,
                    )
                    if outcome.ok:
                        repos.conn.commit()
                        st.success(outcome.message)
                    else:
                        st.error(outcome.message)
                else:
                    outcome = portfolio.sell(ticker, price, int(lots), trade_date=_dt.date.today(), note=note)
                    if outcome.ok:
                        repos.conn.commit()
                        st.success(outcome.message)
                    else:
                        st.error(outcome.message)
                st.rerun()

    with tab_ledger:
        transactions = portfolio.transactions()
        if not transactions:
            st.info("No transactions recorded.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Date": item["trade_date"],
                            "Ticker": item["ticker"],
                            "Side": item["side"].upper(),
                            "Lots": item["lots"],
                            "Shares": item["shares"],
                            "Price": item["price"],
                            "Gross": item["gross"],
                            "Fees": item["fees"],
                            "Cash impact": item["net_cash"],
                            "Realized P&L": item["realized_pnl"],
                            "Note": item.get("note"),
                        }
                        for item in transactions
                    ]
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Shares": st.column_config.NumberColumn(format="%.0f"),
                    "Price": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Gross": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Fees": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Cash impact": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Realized P&L": st.column_config.NumberColumn(format="Rp %.0f"),
                },
            )
            st.download_button(
                "Download ledger (CSV)",
                data=pd.DataFrame(transactions).to_csv(index=False).encode("utf-8"),
                file_name="paper_portfolio_transactions.csv",
                mime="text/csv",
            )
            snapshots = repos.portfolio.snapshots(portfolio.portfolio_id)
            if not snapshots.empty:
                figure = go.Figure(
                    go.Scatter(
                        x=snapshots["snapshot_date"],
                        y=snapshots["total_value"],
                        mode="lines+markers",
                        line=dict(color="#38bdf8"),
                    )
                )
                figure.update_layout(
                    template=PLOT_TEMPLATE,
                    height=280,
                    margin=dict(l=10, r=10, t=20, b=10),
                    yaxis_title="Rp",
                    title="Equity curve from saved snapshots",
                )
                st.plotly_chart(figure, width="stretch")

    with tab_settings:
        st.markdown("##### Starting capital")
        st.caption(
            "Resetting deletes every transaction and snapshot for this paper portfolio. It does not touch "
            "financial statements, prices or research notes."
        )
        initial = st.number_input(
            "Initial virtual cash (Rp)",
            min_value=1_000_000.0,
            value=float(summary["initial_cash"] or 100_000_000.0),
            step=10_000_000.0,
            format="%.0f",
        )
        confirm = st.checkbox("I understand this deletes the paper trading history")
        if st.button("Reset paper portfolio", disabled=not confirm):
            repos.portfolio.reset(portfolio.portfolio_id, initial)
            repos.conn.commit()
            st.success("Paper portfolio reset.")
            st.rerun()
