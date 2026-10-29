"""Live Market monitor: delayed IDX quotes, intraday chart and EOD sync."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.data import live, live_sync
from src.ui import theme
from src.ui.components import get_repos
from src.utils.formatting import format_number, format_percent

PLOT_TEMPLATE = "plotly_dark"
AUTO_REFRESH_SECONDS = 60


def _flash(kind: str, message: str) -> None:
    st.session_state["live_flash"] = (kind, message)


def _show_flash() -> None:
    flash = st.session_state.pop("live_flash", None)
    if not flash:
        return
    kind, message = flash
    {"ok": st.success, "warn": st.warning, "error": st.error}.get(kind, st.info)(message)


def _market_state(quotes: Dict[str, Dict[str, Any]]) -> str:
    states = [q.get("market_state") for q in quotes.values() if q.get("market_state")]
    if not states:
        return "UNKNOWN"
    for candidate in ("OPEN", "PRE-OPEN", "CLOSED"):
        if candidate in states:
            return candidate
    return states[0]


def _status_panel(entries: List[Dict[str, Any]], quotes: Dict[str, Dict[str, Any]]) -> None:
    latest = max((q.get("quote_time") or "" for q in quotes.values()), default="")
    retrieved = max((q.get("retrieved_at") or "" for q in quotes.values()), default="")
    state = _market_state(quotes)
    dot = "bb-dot" if state == "OPEN" else "bb-dot closed"
    cells = [
        ("FEED", "YAHOO FINANCE", "muted"),
        ("MARKET", state, "up" if state == "OPEN" else "muted"),
        ("SYMBOLS", str(len(entries)), ""),
        ("LAST TRADE", latest.replace("T", " ")[:19] if latest else "n/a", "muted"),
        ("RETRIEVED", retrieved.replace("T", " ")[:19] if retrieved else "n/a", "muted"),
        ("DELAY", "~10-20 MIN", "muted"),
    ]
    inner = "".join(
        f'<div class="bb-cell"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'
        for k, v, cls in cells
    )
    st.markdown(
        f"""
<div class="bb-panel" style="padding:0;">
  <div style="padding:6px 10px;border-bottom:1px solid #26282c;">
    <span class="{dot}"></span>
    <span style="font-size:10px;letter-spacing:.14em;color:#8b8f96;">LIVE FEED STATUS &mdash; DELAYED, UNOFFICIAL VENDOR DATA</span>
  </div>
  <div class="bb-grid">{inner}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def _quote_frame(entries: List[Dict[str, Any]], quotes: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for entry in entries:
        quote = quotes.get(entry["ticker"]) or {}
        price = quote.get("price")
        percent = quote.get("change_percent")
        quote_time = (quote.get("quote_time") or "")[11:19]
        rows.append(
            {
                "TICKER": entry["ticker"],
                "NAME": (quote.get("name") or entry.get("name") or "")[:34],
                "LAST": price,
                "CHG": quote.get("change"),
                "CHG%": percent * 100 if percent is not None else None,
                "LOW": quote.get("day_low"),
                "HIGH": quote.get("day_high"),
                "52W LOW": quote.get("week52_low"),
                "52W HIGH": quote.get("week52_high"),
                "VOLUME": quote.get("volume"),
                "TIME": quote_time or "n/a",
                "STATE": quote.get("market_state") or "n/a",
            }
        )
    return pd.DataFrame(rows)


def _movers_panel(quotes: Dict[str, Dict[str, Any]]) -> None:
    ranked = [q for q in quotes.values() if q.get("change_percent") is not None]
    if not ranked:
        return
    ranked.sort(key=lambda q: q["change_percent"], reverse=True)
    leaders = ranked[:3]
    laggards = list(reversed(ranked[-3:]))
    columns = st.columns(2)
    for column, title, items in (
        (columns[0], "TOP GAINERS", leaders),
        (columns[1], "TOP LOSERS", laggards),
    ):
        with column:
            cells = "".join(
                f'<div class="bb-cell"><div class="k">{q["ticker"]}</div>'
                f'<div class="v {"up" if q["change_percent"] >= 0 else "down"}">'
                f'{format_percent(q["change_percent"], 2, signed=True)}</div></div>'
                for q in items
            )
            st.markdown(
                f'<div class="bb-panel" style="padding:0;"><div class="bb-panel-title" '
                f'style="padding:6px 10px 4px 10px;margin:0;">{title}</div>'
                f'<div class="bb-grid">{cells}</div></div>',
                unsafe_allow_html=True,
            )


def _chart(ticker: str, stored: Dict[str, Any]) -> None:
    try:
        frame = live.provider().intraday(ticker, range_="1d", interval="5m")
        error = None
    except live.LiveDataUnavailable as exc:
        frame = pd.DataFrame()
        error = str(exc)

    if frame.empty:
        st.warning(
            f"No intraday series available for {ticker}"
            + (f": {error}" if error else " (market may be closed and the vendor returned no points).")
        )
        return

    previous = stored.get("previous_close")
    closes = frame["close"].astype(float)
    low, high = float(closes.min()), float(closes.max())
    pad = (high - low) * 0.12 or max(high * 0.002, 1.0)
    lower = min(low, previous) if previous else low
    upper = max(high, previous) if previous else high

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["time"],
            y=frame["close"],
            mode="lines",
            name=ticker,
            line={"color": theme.AMBER, "width": 1.6},
        )
    )
    if previous:
        figure.add_hline(
            y=previous,
            line={"color": "#5a5e66", "width": 1, "dash": "dot"},
            annotation_text=f"PREV {format_number(previous, 0)}",
            annotation_position="top left",
            annotation_font={"size": 9, "color": "#8b8f96"},
        )
    figure.update_layout(
        template=PLOT_TEMPLATE,
        height=280,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="#000000",
        plot_bgcolor="#000000",
        showlegend=False,
        xaxis={"gridcolor": "#16181c", "tickfont": {"size": 9, "color": "#8b8f96"}},
        yaxis={
            "gridcolor": "#16181c",
            "tickfont": {"size": 9, "color": "#8b8f96"},
            "side": "right",
            "range": [lower - pad, upper + pad],
            "tickformat": ",.2f" if ticker == "IHSG" else ",.0f",
        },
        hovermode="x unified",
    )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


def _sync_history(repos, entries: List[Dict[str, Any]]) -> Tuple[int, int, List[str]]:
    """Write real EOD closes into ``prices`` (companies) and ``benchmark_prices`` (IHSG)."""
    return live_sync.sync_eod(repos, entries, range_="1y")


@st.fragment(run_every=f"{AUTO_REFRESH_SECONDS}s")
def _monitor(repos, entries: List[Dict[str, Any]]) -> None:
    auto = st.session_state.get("live_auto", True)
    force = st.session_state.pop("live_force", False)

    error: Optional[str] = None
    if auto or force:
        try:
            fresh = live.provider().quotes([entry["symbol"] for entry in entries])
            repos.live.save_quotes(list(fresh.values()))
            repos.conn.commit()
        except live.LiveDataUnavailable as exc:
            error = str(exc)

    quotes = repos.live.latest()
    if error:
        st.error(f"Live vendor unavailable: {error}. Showing the last stored quotes, timestamped below.")

    _status_panel(entries, quotes)

    if not any(q.get("price") is not None for q in quotes.values()):
        st.info("No quotes stored yet. Press **Refresh now** once the vendor is reachable.")
        return

    frame = _quote_frame(entries, quotes)
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        height=min(560, 40 + 30 * len(frame)),
        column_config={
            "NAME": st.column_config.TextColumn(width="medium"),
            "LAST": st.column_config.NumberColumn(format="%.0f"),
            "CHG": st.column_config.NumberColumn(format="%.0f"),
            "CHG%": st.column_config.NumberColumn(format="%.2f%%"),
            "LOW": st.column_config.NumberColumn(format="%.0f"),
            "HIGH": st.column_config.NumberColumn(format="%.0f"),
            "52W LOW": st.column_config.NumberColumn(format="%.0f"),
            "52W HIGH": st.column_config.NumberColumn(format="%.0f"),
            "VOLUME": st.column_config.NumberColumn(format="%.0f"),
            "TIME": st.column_config.TextColumn(width="small"),
            "STATE": st.column_config.TextColumn(width="small"),
        },
    )

    _movers_panel(quotes)

    left, right = st.columns([1, 2])
    with left:
        tickers = [entry["ticker"] for entry in entries]
        selected = st.selectbox("Intraday chart", tickers, key="live_chart_ticker")
    with right:
        stored = quotes.get(selected) or {}
        if stored:
            st.caption(
                f"Last {format_number(stored.get('price'), 0)} &middot; "
                f"{format_percent(stored.get('change_percent'), 2, signed=True)} &middot; "
                f"day {format_number(stored.get('day_low'), 0)}-{format_number(stored.get('day_high'), 0)} &middot; "
                f"quote time {stored.get('quote_time', 'n/a')}"
            )
    _chart(selected, stored)


def render() -> None:
    repos = get_repos()
    theme.page_header(
        "LIVE",
        "Live Market Monitor",
        "Delayed IDX quotes from Yahoo Finance's public endpoints. The vendor is not an exchange feed: "
        "prices lag the tape and must not be used as execution prices.",
    )

    entries = repos.live.symbols()
    if not entries:
        st.markdown(
            '<div class="bb-panel"><div class="bb-panel-title">FEED NOT CONFIGURED</div>'
            '<div style="font-size:12px;color:#c9ccd1;line-height:1.6;">'
            "The live tape needs a symbol list. Enabling the default feed subscribes to the IDX composite "
            "and eleven blue-chip tickers. Quotes are delayed and stored locally with their vendor URL "
            "and retrieval timestamp; no price is invented or back-filled.</div></div>",
            unsafe_allow_html=True,
        )
        if st.button("Enable default IDX feed", type="primary"):
            inserted = repos.live.seed_defaults()
            repos.conn.commit()
            _flash("ok", f"Live feed enabled with {inserted} symbols. Fetching first quotes…")
            st.rerun()
        _show_flash()
        return

    _show_flash()

    controls = st.columns([1.1, 1.2, 2.4, 1.4])
    if controls[0].button("Refresh now", type="primary"):
        st.session_state["live_force"] = True
    st.session_state.setdefault("live_auto", True)
    controls[1].toggle("Auto-refresh 60s", key="live_auto")
    with controls[2]:
        with st.form("live_add", clear_on_submit=True, border=False):
            new_ticker = st.text_input(
                "Add IDX ticker",
                placeholder="BBCA, ANTM, IHSG",
                label_visibility="collapsed",
            )
            added = st.form_submit_button("Add symbol")
        if added and new_ticker.strip():
            clean = new_ticker.strip().upper()
            try:
                quote = live.provider().quote(clean)
                if not quote.ok:
                    _flash("error", f"{clean}: {quote.error}")
                else:
                    repos.live.upsert_symbol(clean, quote.symbol, quote.name)
                    repos.live.save_quotes([quote])
                    repos.conn.commit()
                    _flash("ok", f"{clean} added to the live feed ({quote.symbol}).")
            except live.LiveDataUnavailable as exc:
                _flash("error", f"Could not verify {clean}: {exc}")
            except (ValueError, sqlite3.IntegrityError) as exc:
                _flash("error", f"{clean} could not be added: {exc}")
            st.rerun()
    with controls[3]:
        if st.button("Sync EOD to prices"):
            with st.spinner("Fetching daily history…"):
                price_rows, benchmark_rows, errors = _sync_history(repos, entries)
            st.cache_data.clear()
            message = (
                f"Imported {price_rows} daily closes into prices and {benchmark_rows} IHSG bars into the "
                "benchmark series. Analysis pages use these as their price basis."
            )
            if errors:
                message += " Some symbols failed: " + "; ".join(errors[:3])
            _flash("ok" if price_rows or benchmark_rows else "warn", message)
            st.rerun()

    st.caption(
        "Source: query1.finance.yahoo.com (chart/spark). Quotes are delayed, unofficial and not an "
        "exchange feed. Stored values keep the vendor URL and retrieval time; see Data Management → Live feed."
    )

    _monitor(repos, entries)

    with st.expander("Manage feed symbols"):
        managed = st.multiselect(
            "Remove from feed",
            [entry["ticker"] for entry in entries],
            key="live_remove_list",
        )
        if st.button("Remove selected") and managed:
            for ticker in managed:
                repos.live.remove(ticker)
            repos.conn.commit()
            _flash("ok", f"Removed {', '.join(managed)} from the feed.")
            st.rerun()

        st.caption(
            "Mappings: a company ticker such as BBCA resolves to the vendor symbol BBCA.JK; the composite "
            "index IHSG resolves to ^JKSE. Daily history is written to prices only for tickers that exist "
            "as companies, so a live symbol never invents a fundamental record."
        )
