"""Compact delayed-quote widgets shared by the research pages.

The rule everywhere: the quote strip is *context*, and it is labelled as
delayed vendor data. It never replaces the price basis used by the fundamental
analysis, which stays the last close imported into ``prices``. When the two
differ the widget says so instead of quietly picking one.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st

from src.data import live
from src.ui import theme
from src.utils.formatting import format_number, format_percent


def _decimals(ticker: str) -> int:
    return 2 if ticker.upper() == "IHSG" else 0


def _quote_cells(quote: Dict[str, Any]) -> str:
    ticker = quote.get("ticker") or ""
    percent = quote.get("change_percent")
    cells = [
        ("LAST (DELAYED)", format_number(quote.get("price"), _decimals(ticker)), theme.change_class(percent)),
        (
            "CHANGE",
            f"{theme.change_arrow(percent)} {format_percent(percent, 2, signed=True)}",
            theme.change_class(percent),
        ),
        ("PREV CLOSE", format_number(quote.get("previous_close"), _decimals(ticker)), ""),
        (
            "DAY RANGE",
            f"{format_number(quote.get('day_low'), _decimals(ticker))} - "
            f"{format_number(quote.get('day_high'), _decimals(ticker))}",
            "muted",
        ),
        ("LAST TRADE", (quote.get("quote_time") or "n/a").replace("T", " ")[11:19] + " WIB", "muted"),
        ("MARKET", quote.get("market_state") or "n/a", "muted"),
    ]
    return "".join(
        f'<div class="bb-cell"><div class="k">{name}</div><div class="v {cls}">{value}</div></div>'
        for name, value, cls in cells
    )


def quote_strip(
    repos,
    ticker: str,
    basis_price: Optional[float] = None,
    basis_date: Optional[str] = None,
    key: str = "",
) -> None:
    """Render the stored delayed quote for ``ticker``, or offer to subscribe."""
    ticker = (ticker or "").upper()
    if not ticker:
        return

    quote = repos.live.latest_for(ticker)
    if not quote or quote.get("price") is None:
        company = repos.companies.get(ticker) or {}
        if company.get("is_synthetic"):
            st.caption(
                f"{ticker} is a synthetic fixture, so no vendor quote exists for it. The analysis below uses the "
                "generated price series."
            )
            return
        columns = st.columns([4, 1.2])
        columns[0].caption(
            f"No delayed quote stored for {ticker}. Subscribe it to the live feed to see tape context here; "
            "the analysis below is unaffected."
        )
        if columns[1].button("Subscribe", key=f"live_subscribe_{ticker}_{key}"):
            try:
                fetched = live.provider().quote(ticker)
            except live.LiveDataUnavailable as exc:
                st.error(f"Vendor unavailable: {exc}")
            else:
                if not fetched.ok:
                    st.error(f"{ticker}: {fetched.error}")
                else:
                    repos.live.upsert_symbol(ticker, fetched.symbol, fetched.name)
                    repos.live.save_quotes([fetched])
                    repos.conn.commit()
                    st.rerun()
        return

    st.markdown(
        f"""
<div class="bb-panel" style="padding:0;">
  <div style="padding:6px 10px;border-bottom:1px solid #26282c;">
    <span class="bb-dot{' closed' if quote.get('market_state') != 'OPEN' else ''}"></span>
    <span style="font-size:10px;letter-spacing:.14em;color:#8b8f96;">
      DELAYED QUOTE &mdash; YAHOO FINANCE &mdash; NOT AN EXECUTION PRICE
    </span>
  </div>
  <div class="bb-grid">{_quote_cells(quote)}</div>
</div>
""",
        unsafe_allow_html=True,
    )

    notes = []
    if quote.get("retrieved_at"):
        notes.append(f"retrieved {quote['retrieved_at'].replace('T', ' ')[:19]} WIB")
    if basis_price:
        difference = (quote["price"] - float(basis_price)) / float(basis_price) if basis_price else None
        basis_label = f"on {basis_date}" if basis_date else "in the analysis"
        notes.append(
            f"analysis price basis {format_number(basis_price, _decimals(ticker))} {basis_label}; "
            f"the delayed quote is {format_percent(difference, 2, signed=True)} away from it"
        )
    st.caption("Vendor: query1.finance.yahoo.com · " + " · ".join(notes) + f" · {live.DELAY_NOTE}")
