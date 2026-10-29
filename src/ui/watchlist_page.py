"""Watchlist and alerts page.

Alerts are evaluated against stored prices and statements only. Nothing here
sends a notification or places an order.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from src.ui import theme

from src.portfolio.watchlist import WatchlistService
from src.ui.components import current_settings, empty_state, get_repos, request_page
from src.utils.formatting import format_date, format_idr, format_percent


def _service(repos, settings) -> WatchlistService:
    return WatchlistService(repos, settings)


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    service = _service(repos, settings)
    theme.page_header("MON", "Watchlist")
    st.caption(
        "A watchlist is a research shortlist. Alerts compare stored prices and margin-of-safety targets; they are "
        "informational only and never trigger an order."
    )

    available = repos.companies.tickers()
    if not available:
        empty_state("No companies available. Import data from Data Management first.")
        return

    with st.form("watchlist_add"):
        columns = st.columns([2, 1, 2, 1])
        ticker = columns[0].selectbox("Company", available)
        target = columns[1].number_input(
            "Target margin of safety",
            min_value=0.0,
            max_value=1.0,
            value=0.15,
            step=0.01,
            format="%.2f",
        )
        notes = columns[2].text_input("Note", placeholder="Why is this on the list?")
        submitted = columns[3].form_submit_button("Add", type="primary")
    if submitted:
        if repos.watchlist.contains(ticker):
            st.info(f"{ticker} is already on the watchlist; the note and target were updated.")
        service.add(ticker, notes=notes, target_margin_of_safety=target)
        repos.conn.commit()
        st.rerun()

    entries = service.entries()
    if not entries:
        st.info("The watchlist is empty. Add a company above.")
        return

    live_quotes = repos.live.latest()
    table = pd.DataFrame(
        [
            {
                "Ticker": entry["ticker"],
                "Name": entry.get("name"),
                "Sector": entry.get("sector"),
                "Price": entry.get("price"),
                "Price date": format_date(entry.get("price_date")),
                "Live (delayed)": (live_quotes.get(entry["ticker"]) or {}).get("price"),
                "Live chg": (
                    (live_quotes.get(entry["ticker"]) or {}).get("change_percent") * 100
                    if (live_quotes.get(entry["ticker"]) or {}).get("change_percent") is not None
                    else None
                ),
                "Quote time": str(
                    (live_quotes.get(entry["ticker"]) or {}).get("quote_time") or ""
                ).replace("T", " ")[11:19]
                or "n/a",
                "Fair value": entry.get("fair_value"),
                "MoS": entry.get("margin_of_safety"),
                "Target MoS": entry.get("target_margin_of_safety"),
                "Decision": entry.get("decision"),
                "Score": entry.get("score"),
                "Confidence": entry.get("confidence"),
                "Alerts": len(entry.get("alerts") or []),
                "Added": str(entry.get("added_at", ""))[:10],
            }
            for entry in entries
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "Price": st.column_config.NumberColumn(format="Rp %.0f"),
            "Live (delayed)": st.column_config.NumberColumn(format="Rp %.0f"),
            "Live chg": st.column_config.NumberColumn(format="%.2f%%"),
            "Quote time": st.column_config.TextColumn(width="small"),
            "Fair value": st.column_config.NumberColumn(format="Rp %.0f"),
            "MoS": st.column_config.NumberColumn(format="%.1f%%"),
            "Target MoS": st.column_config.NumberColumn(format="%.0f%%"),
            "Score": st.column_config.NumberColumn(format="%.1f"),
            "Confidence": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )
    st.caption(
        "**Price** is the close the analysis uses. **Live (delayed)** is tape context from Yahoo Finance and is "
        "never substituted into the valuation; subscribe a ticker on Live Market to populate it."
    )

    triggered = [alert for entry in entries for alert in (entry.get("alerts") or [])]
    st.markdown("#### Triggered alerts")
    if triggered:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Ticker": alert["ticker"],
                        "Kind": alert["kind"],
                        "Severity": alert["severity"],
                        "Message": alert["message"],
                    }
                    for alert in triggered
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.success("No alert conditions are currently met.")

    st.markdown("#### Manage an entry")
    selected = st.selectbox("Ticker", [entry["ticker"] for entry in entries], key="watch_manage")
    entry = next(item for item in entries if item["ticker"] == selected)

    with st.form(f"watch_edit_{selected}"):
        columns = st.columns(4)
        above = columns[0].number_input(
            "Alert if price rises above (Rp)",
            min_value=0.0,
            value=float(entry.get("alert_price_above") or 0.0),
            step=50.0,
            format="%.0f",
            help="Set to 0 to disable.",
        )
        below = columns[1].number_input(
            "Alert if price falls below (Rp)",
            min_value=0.0,
            value=float(entry.get("alert_price_below") or 0.0),
            step=50.0,
            format="%.0f",
            help="Set to 0 to disable.",
        )
        note = columns[2].text_input("Note", value=entry.get("notes") or "")
        saved = columns[3].form_submit_button("Update")
    if saved:
        service.set_alerts(selected, above or None, below or None)
        repos.watchlist.add(selected, notes=note, target_margin_of_safety=entry.get("target_margin_of_safety"))
        repos.conn.commit()
        st.success("Watchlist entry updated.")
        st.rerun()

    columns = st.columns(2)
    if columns[0].button(f"Open research for {selected}"):
        st.session_state["selected_ticker"] = selected
        request_page("Company Research")
        st.rerun()
    if columns[1].button(f"Remove {selected} from watchlist"):
        service.remove(selected)
        repos.conn.commit()
        st.success(f"Removed {selected}.")
        st.rerun()
