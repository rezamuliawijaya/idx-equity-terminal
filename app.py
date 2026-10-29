"""Indonesian Equity Research & Decision Terminal - Streamlit entry point.

Run with::

    streamlit run app.py

The application is single-user and local. It reads and writes one SQLite file
and never contacts a broker, a market feed, or an AI provider unless one is
explicitly configured.
"""

from __future__ import annotations

import datetime as _dt
import traceback
from typing import Dict

import streamlit as st

from config.settings import APP_NAME, APP_VERSION, DB_PATH
from src.ui import (
    backtest_page,
    company_detail,
    dashboard,
    data_management,
    decisions,
    live_market,
    live_tape,
    portfolio_page,
    reports_page,
    screener,
    theme,
    valuation_lab,
    watchlist_page,
)

PAGES: Dict[str, object] = {
    "Market Overview": dashboard,
    "Live Market": live_market,
    "Stock Screener": screener,
    "Company Research": company_detail,
    "Valuation Lab": valuation_lab,
    "Investment Decisions": decisions,
    "Watchlist": watchlist_page,
    "Paper Portfolio": portfolio_page,
    "Backtesting": backtest_page,
    "Research Reports": reports_page,
    "Data Management": data_management,
}

PAGE_CODES = {
    "Market Overview": "MKT",
    "Live Market": "LIVE",
    "Stock Screener": "SCRN",
    "Company Research": "DES",
    "Valuation Lab": "VAL",
    "Investment Decisions": "DEC",
    "Watchlist": "MON",
    "Paper Portfolio": "PORT",
    "Backtesting": "BT",
    "Research Reports": "RPT",
    "Data Management": "DATA",
}


def _sidebar_status() -> None:
    """Small data fingerprint so the user always knows what is loaded."""
    from src.ui.components import get_repos, request_page

    try:
        repos = get_repos()
        companies = repos.companies.list()
    except Exception as exc:  # noqa: BLE001 - never block navigation on a status widget
        st.sidebar.caption(f"Database unavailable: {type(exc).__name__}")
        return

    st.sidebar.divider()
    if not companies:
        st.sidebar.warning("No data loaded yet.")
        if st.sidebar.button("Open Data Management", width="stretch"):
            request_page("Data Management")
            st.rerun()
        return

    synthetic = [company["ticker"] for company in companies if company.get("is_synthetic")]
    st.sidebar.markdown("**Database**")
    st.sidebar.caption(
        f"{len(companies)} companies | {len(repos.financials.all_tickers_with_data())} with statements"
    )
    if synthetic:
        st.sidebar.warning(
            f"Synthetic fixtures loaded: {', '.join(synthetic[:4])}"
            + (" ..." if len(synthetic) > 4 else "")
        )
    live_symbols = repos.live.symbols()
    st.sidebar.caption(f"Live feed: {len(live_symbols)} symbols | delayed")
    st.sidebar.caption(f"File: `{DB_PATH.name}` | {_dt.date.today().isoformat()}")


def _sidebar_footer() -> None:
    st.sidebar.divider()
    st.sidebar.markdown(
        f"""
<div class="terminal-foot">
LOCAL SINGLE-USER TERMINAL. NO BROKER, NO ORDERS.<br>
Scores and fair values are rule-based estimates.
Live quotes are delayed vendor data, not investment advice.
<br><br>
{APP_NAME} v{APP_VERSION}
</div>
""",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    theme.apply()

    # Apply a queued cross-page jump before the navigation radio exists: once
    # that widget owns the ``page`` key Streamlit rejects direct writes to it.
    requested = st.session_state.pop("_nav_target", None)
    if requested in PAGES:
        st.session_state["page"] = requested

    if "page" not in st.session_state or st.session_state["page"] not in PAGES:
        st.session_state["page"] = "Market Overview"

    with st.sidebar:
        st.markdown(
            '<div class="terminal-brand">IDX EQUITY<br>TERMINAL</div>'
            '<div class="terminal-sub">Research &amp; decision support</div>'
            '<div style="height:10px"></div>'
            '<div style="font-size:9px;letter-spacing:.2em;color:#5d6168;">FUNCTION MENU</div>',
            unsafe_allow_html=True,
        )
        st.write("")
        selection = st.radio(
            "Navigation",
            list(PAGES),
            key="page",
            format_func=lambda name: f"{PAGE_CODES.get(name, '---'):<5}{name.upper()}",
            label_visibility="collapsed",
        )
        _sidebar_status()
        _sidebar_footer()

    live_tape.render()

    module = PAGES[selection]
    try:
        module.render()
    except Exception as exc:  # noqa: BLE001 - one page must not take down the app
        st.error(
            f"**{selection} failed to render.** {type(exc).__name__}: {exc}\n\n"
            "The rest of the application is unaffected. Data already stored is unchanged."
        )
        with st.expander("Technical detail"):
            st.code(traceback.format_exc(), language="text")

    theme.status_bar(
        provider="YAHOO FINANCE · DELAYED",
        note="NO BROKER · NO ORDERS",
        db_name=DB_PATH.name,
        version=APP_VERSION,
    )


if __name__ == "__main__":
    main()
