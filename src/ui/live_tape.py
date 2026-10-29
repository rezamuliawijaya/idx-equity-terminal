"""Scrolling quote tape shown above every page.

The tape is an isolated Streamlit fragment, so its auto-refresh never re-runs
the page underneath. It reads the configured symbols from SQLite, fetches one
batched quote request, persists the last good quotes and renders a marquee.
When the vendor is unreachable it falls back to those stored quotes and labels
them stale rather than pretending they are current.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List

import streamlit as st

from src.data import live
from src.ui import theme
from src.ui.components import get_repos
from src.utils.formatting import format_number, format_percent

REFRESH_SECONDS = 30


def _tick_html(ticker: str, quote: Dict[str, Any], stale: bool = False) -> str:
    price = quote.get("price")
    percent = quote.get("change_percent")
    if price is None:
        return (
            f'<span class="bb-tick"><b>{ticker}</b>'
            f'<span class="chg bb-flat">NO DATA</span></span>'
        )
    decimals = 2 if ticker == "IHSG" else 0
    arrow = theme.change_arrow(percent)
    cls = theme.change_class(percent)
    late = '<span class="late">*</span>' if stale else ""
    return (
        f'<span class="bb-tick"><b>{ticker}</b>'
        f'<span class="px">{format_number(price, decimals)}</span>'
        f'<span class="chg {cls}">{arrow} {format_percent(percent, 2, signed=True)}</span>'
        f"{late}</span>"
    )


def _rows(repos, quotes: Dict[str, live.LiveQuote]) -> List[Dict[str, Any]]:
    rows = []
    for entry in repos.live.symbols():
        ticker = entry["ticker"]
        quote = quotes.get(ticker)
        if quote is None:
            rows.append({"ticker": ticker, "price": None, "change_percent": None, "stale": True})
            continue
        rows.append(
            {
                "ticker": ticker,
                "price": quote.price,
                "change_percent": quote.change_percent,
                "stale": not quote.ok,
            }
        )
    return rows


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def _tape_fragment() -> None:
    try:
        repos = get_repos()
        entries = repos.live.symbols()
    except Exception as exc:  # noqa: BLE001 - the tape must never break navigation
        st.markdown(
            f'<div class="bb-tape"><span class="bb-tape-label">LIVE</span>'
            f'<span class="bb-tape-empty">feed unavailable: {type(exc).__name__}</span></div>',
            unsafe_allow_html=True,
        )
        return

    if not entries:
        st.markdown(
            '<div class="bb-tape"><span class="bb-tape-label">LIVE</span>'
            '<span class="bb-tape-empty">NO LIVE SYMBOLS &mdash; ENABLE THE FEED IN DATA MANAGEMENT &rsaquo; LIVE FEED</span></div>',
            unsafe_allow_html=True,
        )
        return

    quotes: Dict[str, live.LiveQuote] = {}
    stale = False
    try:
        quotes = live.provider().quotes([entry["symbol"] for entry in entries])
        saved = repos.live.save_quotes(list(quotes.values()))
        if saved:
            repos.conn.commit()
    except live.LiveDataUnavailable:
        stale = True

    if stale:
        stored = repos.live.latest()
        rows = []
        for entry in entries:
            stored_quote = stored.get(entry["ticker"]) or {}
            rows.append(
                {
                    "ticker": entry["ticker"],
                    "price": stored_quote.get("price"),
                    "change_percent": stored_quote.get("change_percent"),
                    "stale": True,
                }
            )
    else:
        rows = _rows(repos, quotes)

    ticker_html = "".join(_tick_html(row["ticker"], row, row["stale"]) for row in rows)
    # Duplicate the strip so the CSS loop is seamless.
    track = ticker_html + ticker_html
    duration = max(28, min(90, len(rows) * 3))
    note = "STALE" if stale else "DELAYED"
    st.markdown(
        f"""
<div class="bb-tape">
  <span class="bb-tape-label">LIVE</span>
  <div class="bb-tape-viewport">
    <div class="bb-tape-track" style="animation-duration:{duration}s;">{track}</div>
  </div>
</div>
<div style="font-size:9px;color:#8b8f96;letter-spacing:.08em;margin:-4px 0 8px 0;">
  {note} &middot; IDX QUOTES VIA YAHOO FINANCE &middot; {live.DELAY_NOTE}
</div>
""",
        unsafe_allow_html=True,
    )


def render() -> None:
    _tape_fragment()
