"""Stock screener."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from src.ui import theme

from src.ui.components import current_settings, empty_state, get_repos, request_page, screener_rows
from src.utils.formatting import format_idr

PRESET_MAP = {
    "Quality companies": {"min_roe": 0.15, "min_roic": 0.10, "min_operating_margin": 0.10},
    "Undervalued companies": {"min_margin_of_safety": 0.15, "max_pe": 15.0},
    "Growing companies": {"min_revenue_growth": 0.10, "min_revenue_cagr_3y": 0.08},
    "Low-debt companies": {"max_net_debt_ebitda": 1.5, "max_debt_to_equity": 0.75},
    "Industrial and manufacturing": {"sectors": ["Industrials", "Basic Materials", "Energy"]},
}

FILTER_FIELDS = [
    ("min_market_cap", "Market cap at least (Rp)", None),
    ("min_revenue_growth", "Revenue growth at least", 0.01),
    ("min_revenue_cagr_3y", "3y revenue CAGR at least", 0.01),
    ("min_roe", "ROE at least", 0.01),
    ("min_roic", "ROIC at least", 0.01),
    ("min_net_margin", "Net margin at least", 0.01),
    ("max_debt_to_equity", "Debt / equity at most", 0.1),
    ("max_net_debt_ebitda", "Net debt / EBITDA at most", 0.5),
    ("max_pe", "P/E at most", 1.0),
    ("max_pb", "P/B at most", 0.5),
    ("min_fcf_yield", "FCF yield at least", 0.01),
    ("min_margin_of_safety", "Margin of safety at least", 0.01),
    ("min_daily_value", "Average daily traded value at least (Rp)", 1e9),
    ("min_data_completeness", "Data completeness at least", 0.05),
]

DEFAULT_SECTORS: List[str] = []


def _filters_sidebar(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    sectors = sorted({row.get("sector") for row in rows if row.get("sector")})
    decisions = ["BUY", "HOLD", "SELL / REDUCE", "AVOID", "INSUFFICIENT DATA"]

    with st.sidebar:
        st.markdown("### Screen filters")
        preset_name = st.selectbox("Preset", ["Custom"] + list(PRESET_MAP.keys()) + ["Saved presets"])
        if preset_name in PRESET_MAP:
            for key, value in PRESET_MAP[preset_name].items():
                st.session_state[f"flt_{key}"] = value
        selected_sectors = st.multiselect("Sector", sectors, default=st.session_state.get("flt_sectors", DEFAULT_SECTORS))
        st.session_state["flt_sectors"] = selected_sectors
        selected_decisions = st.multiselect("Recommendation", decisions, default=decisions)
        values: Dict[str, Any] = {"sectors": selected_sectors, "decisions": selected_decisions}
        with st.expander("Fundamental filters", expanded=True):
            for key, label, step in FILTER_FIELDS:
                values[key] = st.number_input(
                    label,
                    value=st.session_state.get(f"flt_{key}"),
                    step=step,
                    format="%.4f" if step and step < 1 else None,
                    key=f"input_{key}",
                )
        exclude_synthetic = st.checkbox("Exclude synthetic demo tickers", value=False)
        values["exclude_synthetic"] = exclude_synthetic
    return values


FILTER_ROW_KEYS = {
    "min_market_cap": "market_cap",
    "min_revenue_growth": "revenue_growth",
    "min_revenue_cagr_3y": "revenue_cagr_3y",
    "min_roe": "roe",
    "min_roic": "roic",
    "min_net_margin": "net_margin",
    "max_debt_to_equity": "debt_to_equity",
    "max_net_debt_ebitda": "net_debt_ebitda",
    "max_pe": "pe",
    "max_pb": "pb",
    "min_fcf_yield": "fcf_yield",
    "min_margin_of_safety": "margin_of_safety",
    "min_daily_value": "average_daily_value",
    "min_data_completeness": "data_completeness",
}


def _apply(rows: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    output = []
    for row in rows:
        if filters.get("sectors") and row.get("sector") not in filters["sectors"]:
            continue
        if filters.get("decisions") and row.get("decision") not in filters["decisions"]:
            continue
        if filters.get("exclude_synthetic") and row.get("is_synthetic"):
            continue
        ok = True
        for key, row_key in FILTER_ROW_KEYS.items():
            threshold = filters.get(key)
            if threshold is None:
                continue
            value = row.get(row_key)
            if value is None:
                ok = False
                break
            if key.startswith("min_") and value < threshold:
                ok = False
                break
            if key.startswith("max_") and value > threshold:
                ok = False
                break
        if ok:
            output.append(row)
    return output


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("SCRN", "Stock Screener")
    st.caption(
        "Metrics are calculated from stored statements and prices. Status per metric is available in the "
        "expanded view: reported, calculated, estimated or unavailable."
    )
    rows = screener_rows(repos, settings)
    if not rows:
        empty_state("No companies have stored statements yet. Import a CSV from Data Management.")
        return

    filters = _filters_sidebar(rows)
    filtered = _apply(rows, filters)

    columns = st.columns(4)
    columns[0].metric("Universe", len(rows))
    columns[1].metric("After filters", len(filtered))
    buy_count = sum(1 for row in filtered if row.get("decision") == "BUY")
    columns[2].metric("BUY candidates", buy_count)
    columns[3].metric("Median score", f"{pd.Series([r['score'] for r in filtered if r.get('score') is not None]).median():.1f}" if any(r.get("score") is not None for r in filtered) else "n/a")

    sort_key = st.selectbox(
        "Sort by",
        [
            "score", "margin_of_safety", "roe", "roic", "revenue_cagr_3y", "revenue_growth",
            "net_margin", "pe", "pb", "fcf_yield", "market_cap", "data_completeness",
        ],
        index=0,
    )
    ascending = st.checkbox("Ascending", value=False)
    filtered.sort(key=lambda row: row.get(sort_key) if row.get(sort_key) is not None else float("-inf"), reverse=not ascending)

    table = pd.DataFrame(
        [
            {
                "Ticker": row["ticker"],
                "Name": row.get("name"),
                "Sector": row.get("sector"),
                "Decision": row.get("decision"),
                "Score": row.get("score"),
                "MoS": row.get("margin_of_safety"),
                "Price": row.get("price"),
                "Fair value": row.get("fair_value"),
                "Mkt cap": row.get("market_cap"),
                "Rev growth": row.get("revenue_growth"),
                "Rev CAGR 3y": row.get("revenue_cagr_3y"),
                "ROE": row.get("roe"),
                "ROIC": row.get("roic"),
                "Net margin": row.get("net_margin"),
                "D/E": row.get("debt_to_equity"),
                "ND/EBITDA": row.get("net_debt_ebitda"),
                "P/E": row.get("pe"),
                "P/B": row.get("pb"),
                "FCF yield": row.get("fcf_yield"),
                "Completeness": row.get("data_completeness"),
                "Flags": row.get("flags"),
            }
            for row in filtered
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=460,
        column_config={
            "Score": st.column_config.NumberColumn(format="%.1f"),
            "MoS": st.column_config.NumberColumn(format="%.1f%%"),
            "Price": st.column_config.NumberColumn(format="Rp %.0f"),
            "Fair value": st.column_config.NumberColumn(format="Rp %.0f"),
            "Mkt cap": st.column_config.NumberColumn(format="Rp %.0f"),
            "Rev growth": st.column_config.NumberColumn(format="%.1f%%"),
            "Rev CAGR 3y": st.column_config.NumberColumn(format="%.1f%%"),
            "ROE": st.column_config.NumberColumn(format="%.1f%%"),
            "ROIC": st.column_config.NumberColumn(format="%.1f%%"),
            "Net margin": st.column_config.NumberColumn(format="%.1f%%"),
            "D/E": st.column_config.NumberColumn(format="%.2fx"),
            "ND/EBITDA": st.column_config.NumberColumn(format="%.2fx"),
            "P/E": st.column_config.NumberColumn(format="%.1fx"),
            "P/B": st.column_config.NumberColumn(format="%.2fx"),
            "FCF yield": st.column_config.NumberColumn(format="%.1f%%"),
            "Completeness": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )

    left, right = st.columns([3, 1])
    with right:
        st.download_button(
            "Download screen (CSV)",
            data=table.to_csv(index=False).encode("utf-8"),
            file_name="idx_screen.csv",
            mime="text/csv",
        )
    with left:
        if filtered:
            choice = st.selectbox("Inspect a company", [row["ticker"] for row in filtered])
            if st.button("Open company research", type="primary"):
                st.session_state["selected_ticker"] = choice
                request_page("Company Research")
                st.rerun()

    with st.expander("Metric status detail (reported / calculated / estimated / unavailable)"):
        status_rows = []
        for row in filtered:
            status = row.get("status") or {}
            for key, value in status.items():
                status_rows.append({"Ticker": row["ticker"], "Metric": key, "Status": value})
        if status_rows:
            st.dataframe(pd.DataFrame(status_rows), hide_index=True, width="stretch")

    st.caption(
        "Screening is a filter, not a recommendation. Thresholds are research parameters and are not "
        "claims of predictive power."
    )
