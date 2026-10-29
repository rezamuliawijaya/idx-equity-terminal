"""Investment Decisions: the full recommendation board.

Shows every analysed company with its decision, factor contributions, risk
flags and invalidation conditions, and lets you archive a recommendation
snapshot into the database.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui import theme

from src.analytics.engine import analyze_ticker
from src.analytics.scoring import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_HOLD,
    DECISION_INSUFFICIENT,
    DECISION_SELL,
)
from src.ui.components import (
    PLOT_TEMPLATE,
    current_settings,
    empty_state,
    factors_table,
    flags_table,
    get_repos,
    recommendation_card,
    recommendation_facts,
    source_block,
)
from src.utils.formatting import format_date, format_idr, format_multiple, format_percent

DECISION_ORDER = [DECISION_BUY, DECISION_HOLD, DECISION_SELL, DECISION_AVOID, DECISION_INSUFFICIENT]


def _board_rows(repos, settings) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for ticker in repos.financials.all_tickers_with_data():
        try:
            analysis = analyze_ticker(repos, ticker, settings)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not break the board
            rows.append({"Ticker": ticker, "Decision": "INSUFFICIENT DATA", "Error": f"{type(exc).__name__}: {exc}"})
            continue
        if analysis is None:
            continue
        rec = analysis.recommendation
        rows.append(
            {
                "Ticker": ticker,
                "Company": analysis.snapshot.company_name,
                "Sector": analysis.snapshot.sector,
                "Decision": rec.decision,
                "Score": rec.score,
                "Confidence": rec.confidence,
                "Completeness": rec.data_completeness,
                "Price": rec.price,
                "Fair value": rec.fair_value,
                "MoS": rec.margin_of_safety,
                "Expected return": rec.expected_return,
                "Quality": rec.factors["quality"].score,
                "Growth": rec.factors["growth"].score,
                "Valuation": rec.factors["valuation"].score,
                "Health": rec.factors["health"].score,
                "Momentum": rec.factors["momentum"].score,
                "High flags": len([f for f in analysis.flags if f.severity in {"critical", "high"}]),
                "Period": rec.period_label,
                "Synthetic": analysis.snapshot.is_synthetic,
                "_analysis": analysis,
            }
        )
    return rows


def _factor_heatmap(rows: List[Dict[str, Any]]) -> None:
    factors = ["Quality", "Growth", "Valuation", "Health", "Momentum"]
    frame = pd.DataFrame(
        [
            [row.get(factor) if row.get(factor) is not None else float("nan") for factor in factors]
            for row in rows
        ],
        index=[row["Ticker"] for row in rows],
        columns=factors,
    )
    if frame.empty:
        return
    figure = go.Figure(
        data=go.Heatmap(
            z=frame.values,
            x=factors,
            y=frame.index.tolist(),
            colorscale="Tealgrn",
            zmin=0,
            zmax=100,
            hovertemplate="%{y} · %{x}: %{z:.0f}<extra></extra>",
        )
    )
    figure.update_layout(
        template=PLOT_TEMPLATE,
        height=max(220, 60 + 32 * len(frame)),
        margin=dict(l=10, r=10, t=20, b=10),
        xaxis_title="",
        yaxis_title="",
    )
    st.plotly_chart(figure, width="stretch")
    st.caption("Blank cells mean the factor had no usable metric for that company; it is excluded, not scored zero.")


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("DEC", "Investment Decisions")
    st.caption(
        "The decision engine maps factor scores and risk vetoes onto BUY / HOLD / SELL-REDUCE / AVOID / "
        "INSUFFICIENT DATA. Thresholds are configurable research parameters, not universal rules."
    )

    rows = _board_rows(repos, settings)
    if not rows:
        empty_state("No companies have stored statements. Import data from Data Management.")
        return

    scored = [row for row in rows if row.get("Score") is not None]
    columns = st.columns(5)
    counts = {decision: sum(1 for row in rows if row.get("Decision") == decision) for decision in DECISION_ORDER}
    columns[0].metric("BUY", counts[DECISION_BUY])
    columns[1].metric("HOLD", counts[DECISION_HOLD])
    columns[2].metric("SELL / REDUCE", counts[DECISION_SELL])
    columns[3].metric("AVOID", counts[DECISION_AVOID])
    columns[4].metric("INSUFFICIENT DATA", counts[DECISION_INSUFFICIENT])

    table = pd.DataFrame(
        [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in rows
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=420,
        column_config={
            "Score": st.column_config.NumberColumn(format="%.1f"),
            "Confidence": st.column_config.NumberColumn(format="%.0f%%"),
            "Completeness": st.column_config.NumberColumn(format="%.0f%%"),
            "Price": st.column_config.NumberColumn(format="Rp %.0f"),
            "Fair value": st.column_config.NumberColumn(format="Rp %.0f"),
            "MoS": st.column_config.NumberColumn(format="%.1f%%"),
            "Expected return": st.column_config.NumberColumn(format="%.1f%%"),
            "Quality": st.column_config.NumberColumn(format="%.0f"),
            "Growth": st.column_config.NumberColumn(format="%.0f"),
            "Valuation": st.column_config.NumberColumn(format="%.0f"),
            "Health": st.column_config.NumberColumn(format="%.0f"),
            "Momentum": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.download_button(
        "Download decision board (CSV)",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name="investment_decisions.csv",
        mime="text/csv",
    )

    st.markdown("#### Factor score matrix")
    _factor_heatmap(rows)

    st.markdown("#### Decision detail")
    available = [row["Ticker"] for row in rows]
    ticker = st.selectbox("Ticker", available, key="decision_ticker")
    analysis = next((row.get("_analysis") for row in rows if row["Ticker"] == ticker), None)
    if analysis is None:
        st.warning("This ticker could not be analysed. Check its stored data.")
        return

    recommendation_card(analysis)
    recommendation_facts(analysis)

    left, right = st.columns([3, 2])
    with left:
        st.markdown("##### Factor scores")
        factors_table(analysis)
    with right:
        st.markdown("##### Decision rules applied")
        policy = settings.policy
        st.write(
            f"""
- BUY requires score ≥ **{policy.buy_score:.0f}**, margin of safety ≥ **{policy.buy_min_margin_of_safety:.0%}**, confidence ≥ **{policy.buy_min_confidence:.0%}**, and no critical flag.
- HOLD applies from **{policy.hold_score:.0f}** upward when the buy conditions are not all met.
- SELL / REDUCE applies below **{policy.hold_score:.0f}**.
- AVOID is triggered by any unresolved critical flag (other than missing data).
- INSUFFICIENT DATA applies when factor coverage < **{policy.min_factor_coverage:.0%}**, confidence < **{policy.min_confidence:.0%}**, or essentials are stale/missing.
"""
        )
        st.caption("Change these on the Data Management page; saved settings drive the whole terminal.")

    st.markdown("##### Reasons")
    for reason in analysis.recommendation.reasons:
        st.markdown(f"- {reason}")
    if analysis.recommendation.vetoes:
        st.error("Vetoes: " + " | ".join(analysis.recommendation.vetoes))

    st.markdown("##### Risk flags")
    flags_table(analysis.flags)

    st.markdown("##### Thesis invalidation conditions")
    for condition in analysis.recommendation.invalidation_conditions:
        st.markdown(f"- {condition}")

    source_block(analysis)

    st.markdown("#### Archive this recommendation")
    st.caption(
        "Archiving stores the full recommendation payload locally. It does not place any order; this terminal "
        "never connects to a broker."
    )
    if st.button("Save recommendation snapshot", type="primary"):
        repos.recommendations.save(analysis.recommendation.as_dict())
        repos.conn.commit()
        st.success(f"Snapshot saved for {ticker}.")

    history = repos.recommendations.history(ticker, limit=10)
    if history:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Saved": item.get("as_of", "")[:16],
                        "Decision": item.get("decision"),
                        "Score": item.get("score"),
                        "Confidence": item.get("confidence"),
                        "Fair value": item.get("fair_value"),
                        "MoS": item.get("margin_of_safety"),
                    }
                    for item in history
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Score": st.column_config.NumberColumn(format="%.1f"),
                "Confidence": st.column_config.NumberColumn(format="%.0f%%"),
                "Fair value": st.column_config.NumberColumn(format="Rp %.0f"),
                "MoS": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )

    with st.expander("Compare all companies side by side"):
        compare = st.multiselect(
            "Select companies",
            [row["Ticker"] for row in rows],
            default=[row["Ticker"] for row in rows][:4],
            key="compare_tickers",
        )
        if compare:
            comparison = pd.DataFrame(
                [
                    {
                        "Metric": metric,
                        **{
                            row["Ticker"]: _metric_display(_metric_value(row, metric), metric)
                            for row in rows
                            if row["Ticker"] in compare
                        },
                    }
                    for metric in (
                        "Decision", "Score", "P/E", "P/B", "ROE", "ROIC", "Net margin",
                        "Revenue growth", "D/E", "ND/EBITDA", "MoS", "Fair value",
                    )
                ]
            )
            st.dataframe(comparison, hide_index=True, width="stretch")


def _metric_value(row: Dict[str, Any], metric: str) -> Any:
    analysis = row.get("_analysis")
    if analysis is None:
        return None
    mapping = {
        "Decision": lambda: analysis.decision,
        "Score": lambda: analysis.score,
        "P/E": lambda: analysis.value("pe"),
        "P/B": lambda: analysis.value("pb"),
        "ROE": lambda: analysis.value("roe"),
        "ROIC": lambda: analysis.value("roic"),
        "Net margin": lambda: analysis.value("net_margin"),
        "Revenue growth": lambda: analysis.value("revenue_growth"),
        "D/E": lambda: analysis.value("debt_to_equity"),
        "ND/EBITDA": lambda: analysis.value("net_debt_ebitda"),
        "MoS": lambda: analysis.value("margin_of_safety"),
        "Fair value": lambda: analysis.fair_value,
    }
    getter = mapping.get(metric)
    return getter() if getter else None


def _metric_display(value: Any, metric: str) -> str:
    """Format one comparison cell as text.

    A column per ticker mixes decisions with ratios, so leaving the raw values
    in one column gives Arrow an object column of strings and floats. Rendering
    them as display text keeps the table serialisable and readable.
    """
    if value is None:
        return "n/a"
    if metric == "Decision":
        return str(value)
    if metric == "Score":
        return f"{float(value):.1f}"
    if metric in {"P/E", "P/B", "D/E", "ND/EBITDA"}:
        return format_multiple(value)
    if metric in {"ROE", "ROIC", "Net margin", "Revenue growth", "MoS"}:
        return format_percent(value)
    if metric == "Fair value":
        return format_idr(value)
    return str(value)
