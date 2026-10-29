"""Market Overview page."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from src.analytics.risk import detect_risk_flags
from src.ui import theme
from src.ui.components import (
    PLOT_TEMPLATE,
    empty_state,
    freshness_table,
    get_repos,
    current_settings,
    request_page,
    screener_rows,
    sector_chart,
)
from src.utils.formatting import format_date, format_number, format_percent


def _live_panel(repos) -> None:
    quotes = repos.live.latest()
    if not quotes:
        return
    index = quotes.get("IHSG") or {}
    advancers = sum(1 for q in quotes.values() if (q.get("change_percent") or 0) > 0)
    decliners = sum(1 for q in quotes.values() if (q.get("change_percent") or 0) < 0)
    last_quote = max((q.get("quote_time") or "" for q in quotes.values()), default="")

    st.markdown("#### Live market (delayed)")
    columns = st.columns(4)
    columns[0].metric(
        "IHSG",
        format_number(index.get("price"), 2) if index else "n/a",
        format_percent(index.get("change_percent"), 2, signed=True) if index else None,
    )
    columns[1].metric("Advancers", f"{advancers}")
    columns[2].metric("Decliners", f"{decliners}")
    columns[3].metric("Last vendor trade", last_quote[11:19] if last_quote else "n/a")
    if st.button("Open live monitor"):
        request_page("Live Market")
        st.rerun()
    st.caption(
        "Delayed quotes from Yahoo Finance, stored with their vendor URL and retrieval time. "
        "They are context for the tape, not the price basis of the fundamental analysis — that remains "
        "the last close imported into prices."
    )


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("MKT", "Market Overview")
    st.caption(
        "Fundamental figures come from statements and prices stored locally. The quote tape and the Live "
        "Market page use a delayed vendor feed; neither is a broker connection."
    )

    companies = repos.companies.list()
    if not companies:
        empty_state(
            "The database is empty. Go to **Data Management** and either import a CSV or click "
            "*Load synthetic demo data* to explore the terminal."
        )
        return

    rows = screener_rows(repos, settings)
    scored = [row for row in rows if row.get("score") is not None]
    decisions: dict = {}
    for row in rows:
        decisions[row.get("decision", "INSUFFICIENT DATA")] = decisions.get(row.get("decision", "INSUFFICIENT DATA"), 0) + 1

    latest_periods = [repos.financials.latest_period(c["ticker"]) for c in companies]
    latest_periods = [p for p in latest_periods if p]
    latest_date = max((p.period_end for p in latest_periods), default=None)

    columns = st.columns(4)
    columns[0].metric("Companies tracked", f"{len(companies)}")
    columns[1].metric("Scored", f"{len(scored)}")
    columns[2].metric("Average score", f"{sum(r['score'] for r in scored) / len(scored):.1f}" if scored else "n/a")
    columns[3].metric("Latest fundamental period", format_date(latest_date))

    _live_panel(repos)

    st.markdown("#### Recommendations in force")
    if decisions:
        order = ["BUY", "HOLD", "SELL / REDUCE", "AVOID", "INSUFFICIENT DATA"]
        frame = pd.DataFrame(
            [{"Decision": key, "Count": decisions.get(key, 0)} for key in order if decisions.get(key, 0)]
        )
        left, right = st.columns([1, 2])
        with left:
            figure = px.bar(
                frame,
                x="Decision",
                y="Count",
                template=PLOT_TEMPLATE,
                color="Decision",
                color_discrete_map={
                    "BUY": "#16a34a",
                    "HOLD": "#d97706",
                    "SELL / REDUCE": "#dc2626",
                    "AVOID": "#7f1d1d",
                    "INSUFFICIENT DATA": "#475569",
                },
            )
            figure.update_layout(height=300, showlegend=False, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(figure, width="stretch")
        with right:
            sector_chart(rows)

    st.markdown("#### Company snapshot")
    table_rows = []
    for row in sorted(rows, key=lambda r: (r.get("score") is None, -(r.get("score") or 0))):
        table_rows.append(
            {
                "Ticker": row["ticker"],
                "Name": row.get("name"),
                "Sector": row.get("sector"),
                "Decision": row.get("decision"),
                "Score": row.get("score"),
                "Price": row.get("price"),
                "Fair value": row.get("fair_value"),
                "Margin of safety": row.get("margin_of_safety"),
                "ROE": row.get("roe"),
                "P/E": row.get("pe"),
                "Flags": row.get("flags"),
            }
        )
    st.dataframe(
        pd.DataFrame(table_rows),
        hide_index=True,
        width="stretch",
        column_config={
            "Score": st.column_config.NumberColumn(format="%.1f"),
            "Price": st.column_config.NumberColumn(format="Rp %.0f"),
            "Fair value": st.column_config.NumberColumn(format="Rp %.0f"),
            "Margin of safety": st.column_config.NumberColumn(format="%.1f%%"),
            "ROE": st.column_config.NumberColumn(format="%.1f%%"),
            "P/E": st.column_config.NumberColumn(format="%.1fx"),
        },
    )
    st.caption("Scores refresh when data or settings change. Cache lifetime is five minutes.")

    st.markdown("#### Data freshness")
    st.dataframe(freshness_table(repos), hide_index=True, width="stretch")

    watchlist = repos.watchlist.list()
    if watchlist:
        st.markdown("#### Watchlist")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Ticker": item["ticker"],
                        "Name": item.get("name"),
                        "Sector": item.get("sector"),
                        "Added": item.get("added_at", "")[:10],
                        "Target MoS": item.get("target_margin_of_safety"),
                        "Notes": item.get("notes"),
                    }
                    for item in watchlist
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    if any(company.get("is_synthetic") for company in companies):
        st.warning(
            "Some stored companies are **synthetic demonstration fixtures** (tickers starting `DEMO-`). "
            "They exist to exercise the workflow and must never be treated as investment research."
        )
