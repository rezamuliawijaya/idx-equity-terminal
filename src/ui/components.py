"""Shared Streamlit components.

The UI never computes financial results itself: every number shown here comes
from ``src.analytics.engine`` or the repositories.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config.settings import TerminalSettings
from src.analytics.engine import CompanyAnalysis
from src.analytics.fundamentals import Metric
from src.analytics.risk import RiskFlag
from src.database.db import connect, init_db
from src.database.repositories import Repositories
from src.utils.formatting import (
    format_date,
    format_idr,
    format_metric_value,
    format_multiple,
    format_percent,
)

PLOT_TEMPLATE = "plotly_dark"


@st.cache_resource(show_spinner=False)
def get_repos() -> Repositories:
    """One shared connection for the local single-user app."""
    conn = connect(check_same_thread=False)
    init_db(conn)
    repos = Repositories(conn)
    repos.presets.seed_defaults()
    conn.commit()
    return repos


def current_settings(repos: Optional[Repositories] = None) -> TerminalSettings:
    if "settings" not in st.session_state:
        repos = repos or get_repos()
        st.session_state["settings"] = repos.settings.load()
    return st.session_state["settings"]


def save_settings(repos: Repositories, settings: TerminalSettings) -> None:
    repos.settings.save(settings)
    repos.conn.commit()
    st.session_state["settings"] = settings


def commit(repos: Repositories) -> None:
    repos.conn.commit()


def tickers(repos: Repositories) -> List[str]:
    return repos.companies.tickers()


def ticker_selector(repos: Repositories, key: str = "ticker_select", label: str = "Company") -> Optional[str]:
    available = tickers(repos)
    if not available:
        empty_state("No companies yet. Import a CSV or load the synthetic demo data from **Data Management**.")
        return None
    default = st.session_state.get("selected_ticker")
    index = available.index(default) if default in available else 0
    choice = st.selectbox(label, available, index=index, key=key)
    st.session_state["selected_ticker"] = choice
    return choice


def empty_state(message: str) -> None:
    st.info(message)


def request_page(page: str) -> None:
    """Queue navigation to another page.

    Every page-level button is drawn after the sidebar radio that owns the
    ``page`` key, and Streamlit refuses writes to a widget-backed key once that
    widget exists. The entry point applies the queued target before it creates
    the radio, so callers set this and then call ``st.rerun()``.
    """
    st.session_state["_nav_target"] = page


def kpi(label: str, value: str, help_text: str = "", delta: Optional[str] = None) -> None:
    st.metric(label, value, delta=delta, help=help_text or None)


def recommendation_card(analysis: CompanyAnalysis) -> None:
    rec = analysis.recommendation
    colour = {
        "BUY": "#16a34a",
        "HOLD": "#d97706",
        "SELL / REDUCE": "#dc2626",
        "AVOID": "#7f1d1d",
        "INSUFFICIENT DATA": "#475569",
    }.get(rec.decision, "#475569")
    st.markdown(
        f"""
<div style="border-left:6px solid {colour}; padding:12px 16px; background:rgba(148,163,184,0.10); border-radius:6px;">
  <div style="font-size:0.8rem; letter-spacing:0.08em; text-transform:uppercase; opacity:0.75;">Rule-based recommendation</div>
  <div style="font-size:1.6rem; font-weight:700; color:{colour};">{rec.decision}</div>
  <div style="font-size:0.9rem;">
    Score <b>{f"{rec.score:.1f}" if rec.score is not None else "n/a"}</b>/100 &nbsp;|&nbsp;
    Confidence <b>{rec.confidence:.0%}</b> &nbsp;|&nbsp;
    Data completeness <b>{rec.data_completeness:.0%}</b> &nbsp;|&nbsp;
    Profile <b>{rec.profile}</b>
  </div>
  <div style="font-size:0.85rem; opacity:0.85; margin-top:4px;">{rec.company_name} &middot; {rec.ticker} &middot; analysed {rec.as_of[:16]}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def recommendation_facts(analysis: CompanyAnalysis) -> None:
    rec = analysis.recommendation
    columns = st.columns(4)
    columns[0].metric("Current price", format_idr(rec.price) if rec.price else "n/a", help="From stored price data only.")
    columns[1].metric("Estimated fair value", format_idr(rec.fair_value) if rec.fair_value else "n/a", help=rec.valuation_method)
    columns[2].metric("Margin of safety", format_percent(rec.margin_of_safety) if rec.margin_of_safety is not None else "n/a")
    columns[3].metric(
        "Expected return (scenario)",
        format_percent(rec.expected_return) if rec.expected_return is not None else "n/a",
        help=f"Annualised convergence to fair value over {rec.horizon_years:g} years. A scenario, not a forecast.",
    )
    st.caption(
        f"Fundamentals: {rec.period_label} | Price date: {format_date(rec.price_date)} | "
        f"Valuation method: {rec.valuation_method}"
    )


def factors_table(analysis: CompanyAnalysis) -> None:
    rec = analysis.recommendation
    rows = []
    for factor in rec.factors.values():
        rows.append(
            {
                "Factor": factor.label,
                "Weight": factor.weight,
                "Score": factor.score,
                "Contribution": factor.contribution,
            }
        )
    frame = pd.DataFrame(rows)
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={
            "Weight": st.column_config.NumberColumn(format="%.0f%%"),
            "Score": st.column_config.ProgressColumn(format="%.0f", min_value=0, max_value=100),
            "Contribution": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    with st.expander("Component detail (every breakpoint is documented)"):
        for factor in rec.factors.values():
            st.markdown(f"**{factor.label}** - weight {factor.weight:.0%}")
            st.dataframe(
                pd.DataFrame(factor.components)[
                    ["label", "value", "score", "weight", "note"]
                ] if factor.components else pd.DataFrame(),
                hide_index=True,
                width="stretch",
            )


def metrics_table(metrics: Iterable[Metric], compact_idr: bool = True) -> None:
    rows = []
    for metric in metrics:
        rows.append(
            {
                "Metric": metric.label,
                "Value": format_metric_value(metric, compact_idr=compact_idr),
                "Status": metric.status,
                "Formula": metric.formula,
                "Note": metric.note,
            }
        )
    if not rows:
        empty_state("No metrics available for this section.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def flags_table(flags: Sequence[RiskFlag]) -> None:
    if not flags:
        st.success("No risk flags were triggered by the imported data.")
        return
    frame = pd.DataFrame(
        [
            {
                "Severity": flag.severity.upper(),
                "Code": flag.code,
                "Message": flag.message,
                "Thesis impact": flag.thesis_impact,
            }
            for flag in flags
        ]
    )
    st.dataframe(frame, hide_index=True, width="stretch")


def source_block(analysis: CompanyAnalysis) -> None:
    snapshot = analysis.snapshot
    with st.expander("Data provenance and freshness", expanded=False):
        st.write(f"Analysis basis: **{snapshot.period_label}** ending {format_date(snapshot.as_of)}")
        st.write(f"Stored periods: {len(snapshot.periods)} | TTM: {snapshot.ttm_explanation}")
        if snapshot.is_synthetic:
            st.warning("This ticker is a synthetic demonstration fixture and does not represent a real issuer.")
        rows = []
        for period in snapshot.periods[:10]:
            rows.append(
                {
                    "Period end": period.period_end,
                    "Type": period.period_type,
                    "Basis": "consolidated" if period.is_consolidated else "parent-only",
                    "Published": period.publication_date,
                    "Sources": ", ".join(period.source_urls) or "none recorded",
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No periods stored.")


def price_chart(prices: pd.DataFrame, title: str = "Share price", ticker: str = "") -> None:
    if prices is None or prices.empty:
        empty_state("No price history stored for this ticker. Import prices from Data Management.")
        return
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["close"],
            mode="lines",
            name=ticker or "close",
            line=dict(color="#38bdf8", width=1.6),
        )
    )
    figure.update_layout(
        title=title,
        template=PLOT_TEMPLATE,
        height=320,
        margin=dict(l=10, r=10, t=40, b=10),
        yaxis_title="Rp",
        xaxis_title="",
        showlegend=False,
    )
    st.plotly_chart(figure, width="stretch")
    st.caption(
        f"{len(frame):,} observations from {format_date(frame['date'].min())} to {format_date(frame['date'].max())}. "
        "Prices are shown as stored; corporate actions are not adjusted unless the source series was adjusted."
    )


def history_chart(periods: Sequence[Any], items: Sequence[str], title: str, unit_label: str = "Rp bn") -> None:
    rows = []
    for period in sorted(periods, key=lambda p: p.period_end):
        row = {"period": period.period_end, "type": period.period_type}
        for item in items:
            value = period.get(item)
            row[item] = None if value is None else value / 1e9
        rows.append(row)
    frame = pd.DataFrame(rows)
    if frame.empty or not any(frame[item].notna().any() for item in items if item in frame):
        empty_state("Not enough stored periods to chart this series.")
        return
    long = frame.melt(id_vars=["period", "type"], value_vars=[i for i in items if i in frame], var_name="Item", value_name="Value")
    figure = px.bar(
        long,
        x="period",
        y="Value",
        color="Item",
        barmode="group",
        title=title,
        template=PLOT_TEMPLATE,
        labels={"Value": unit_label},
    )
    figure.update_layout(height=340, margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h"))
    st.plotly_chart(figure, width="stretch")


def statement_table(periods: Sequence[Any], items: Sequence[tuple[str, str]], limit: int = 6) -> None:
    columns = {}
    for period in sorted(periods, key=lambda p: p.period_end, reverse=True)[:limit]:
        columns[period.period_end.isoformat()] = {key: period.get(key) for key, _ in items}
    if not columns:
        empty_state("No statements stored.")
        return
    frame = pd.DataFrame(
        [
            {
                "Line item": label,
                **{
                    period_label: (values.get(key) / 1e9 if values.get(key) is not None else None)
                    for period_label, values in columns.items()
                },
            }
            for key, label in items
        ]
    )
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={column: st.column_config.NumberColumn(format="%.1f") for column in columns},
    )
    st.caption("Values in Rp billion, converted from the reported units. Blank cells were not reported (not zero).")


def peer_table(analysis: CompanyAnalysis, peers: Sequence[Dict[str, Any]]) -> None:
    rows = []
    for peer in peers:
        rows.append(
            {
                "Ticker": peer.get("ticker"),
                "Name": peer.get("name"),
                "Score": peer.get("score"),
                "Decision": peer.get("decision"),
                "P/E": peer.get("pe"),
                "P/B": peer.get("pb"),
                "ROE": peer.get("roe"),
                "Net margin": peer.get("net_margin"),
            }
        )
    if not rows:
        empty_state("No peers with stored data in the same sector.")
        return
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={
            "Score": st.column_config.NumberColumn(format="%.0f"),
            "P/E": st.column_config.NumberColumn(format="%.1fx"),
            "P/B": st.column_config.NumberColumn(format="%.1fx"),
            "ROE": st.column_config.NumberColumn(format="%.1f%%"),
            "Net margin": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )
    st.caption("Peer sample used for relative valuation: " + str(analysis.peers_used) + " companies in the same sector.")


def sector_chart(rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        empty_state("No scored companies yet.")
        return
    frame = pd.DataFrame(rows)
    frame = frame.dropna(subset=["score"])
    if frame.empty:
        empty_state("No company has enough data to score.")
        return
    grouped = frame.groupby("sector", dropna=False)["score"].mean().reset_index()
    figure = px.bar(
        grouped.sort_values("score"),
        x="score",
        y="sector",
        orientation="h",
        template=PLOT_TEMPLATE,
        title="Average composite score by sector",
        labels={"score": "Composite score", "sector": ""},
        color="score",
        color_continuous_scale="Tealgrn",
    )
    figure.update_layout(height=340, margin=dict(l=10, r=10, t=40, b=10), coloraxis_showscale=False)
    st.plotly_chart(figure, width="stretch")


def with_status(metric: Optional[Metric]) -> str:
    if metric is None:
        return "n/a"
    return format_metric_value(metric)


@st.cache_data(ttl=300, show_spinner=False)
def score_all(tickers_key: tuple, settings_json: str) -> List[Dict[str, Any]]:
    """Screener rows for every ticker, cached against the settings payload."""
    from src.analytics.engine import quick_scores

    repos = get_repos()
    settings = TerminalSettings.from_dict(json.loads(settings_json))
    return quick_scores(repos, settings, list(tickers_key))


def screener_rows(repos: Repositories, settings: TerminalSettings) -> List[Dict[str, Any]]:
    available = tuple(repos.financials.all_tickers_with_data())
    return score_all(available, json.dumps(settings.as_dict(), sort_keys=True, default=str))


def freshness_table(repos: Repositories) -> pd.DataFrame:
    rows = []
    today = _dt.date.today()
    for company in repos.companies.list():
        ticker = company["ticker"]
        latest_period = repos.financials.latest_period(ticker)
        latest_price = repos.prices.latest(ticker)
        age = (today - latest_period.period_end).days if latest_period else None
        price_age = (today - latest_price["date"]).days if latest_price else None
        rows.append(
            {
                "Ticker": ticker,
                "Name": company.get("name"),
                "Sector": company.get("sector"),
                "Latest period": latest_period.period_end if latest_period else None,
                "Period age (days)": age,
                "Price date": latest_price["date"] if latest_price else None,
                "Price age (days)": price_age,
                "Synthetic": bool(company.get("is_synthetic")),
            }
        )
    return pd.DataFrame(rows)

