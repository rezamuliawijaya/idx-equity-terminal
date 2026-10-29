"""Valuation Lab: editable DCF assumptions with reproducible output.

The lab never edits stored data. It builds a fresh ``DCFAssumptions`` model,
validates it, runs the same ``run_dcf`` used by the recommendation engine, and
lets you save the run to the local database for later comparison.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Dict, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui import theme

from src.analytics.engine import analyze_ticker
from src.analytics.valuation import (
    DCFAssumptions,
    build_dcf_assumptions,
    run_dcf,
    sensitivity_table,
)
from src.ui.components import (
    PLOT_TEMPLATE,
    current_settings,
    empty_state,
    get_repos,
    ticker_selector,
)
from src.utils.formatting import format_date, format_idr, format_multiple, format_percent


def _seed_payload(assumptions: DCFAssumptions) -> Dict[str, Any]:
    return assumptions.model_dump()


def _form(payload: Dict[str, Any], key_prefix: str) -> Optional[Dict[str, Any]]:
    st.markdown("#### Projection assumptions")
    left, right = st.columns(2)
    with left:
        base_revenue = st.number_input(
            "Base revenue (Rp)",
            value=float(payload["base_revenue"]),
            min_value=1.0,
            step=max(1.0, float(payload["base_revenue"]) * 0.01),
            format="%.0f",
            key=f"{key_prefix}_rev",
            help="Trailing revenue in absolute rupiah. Change only if you have a reason.",
        )
        revenue_growth = st.number_input(
            "Year-1 revenue growth",
            value=float(payload["revenue_growth"]),
            min_value=-0.5,
            max_value=1.0,
            step=0.005,
            format="%.3f",
            key=f"{key_prefix}_growth",
        )
        operating_margin = st.number_input(
            "Operating margin",
            value=float(payload["operating_margin"]),
            min_value=-1.0,
            max_value=1.0,
            step=0.005,
            format="%.3f",
            key=f"{key_prefix}_margin",
        )
        tax_rate = st.number_input(
            "Tax rate",
            value=float(payload["tax_rate"]),
            min_value=0.0,
            max_value=0.6,
            step=0.01,
            format="%.3f",
            key=f"{key_prefix}_tax",
        )
        da_pct = st.number_input(
            "D&A (% of revenue)",
            value=float(payload["da_pct_revenue"]),
            min_value=0.0,
            max_value=0.5,
            step=0.0025,
            format="%.4f",
            key=f"{key_prefix}_da",
        )
    with right:
        capex_pct = st.number_input(
            "Capex (% of revenue)",
            value=float(payload["capex_pct_revenue"]),
            min_value=0.0,
            max_value=1.0,
            step=0.0025,
            format="%.4f",
            key=f"{key_prefix}_capex",
        )
        nwc_pct = st.number_input(
            "ΔNWC (% of revenue change)",
            value=float(payload["nwc_pct_revenue"]),
            min_value=-0.5,
            max_value=1.0,
            step=0.01,
            format="%.3f",
            key=f"{key_prefix}_nwc",
        )
        wacc = st.number_input(
            "WACC",
            value=float(payload["wacc"]),
            min_value=0.001,
            max_value=0.5,
            step=0.0025,
            format="%.4f",
            key=f"{key_prefix}_wacc",
        )
        terminal_growth = st.number_input(
            "Terminal growth",
            value=float(payload["terminal_growth"]),
            min_value=-0.05,
            max_value=0.15,
            step=0.0025,
            format="%.4f",
            key=f"{key_prefix}_tg",
        )
        years = st.number_input(
            "Projection years",
            value=int(payload["years"]),
            min_value=1,
            max_value=20,
            step=1,
            key=f"{key_prefix}_years",
        )
        growth_fade = st.checkbox(
            "Fade growth linearly to terminal growth",
            value=bool(payload["growth_fade"]),
            key=f"{key_prefix}_fade",
        )

    with st.expander("Enterprise-to-equity bridge"):
        net_debt = st.number_input(
            "Net debt (Rp, negative = net cash)",
            value=float(payload["net_debt"] or 0.0),
            step=1e9,
            format="%.0f",
            key=f"{key_prefix}_nd",
        )
        minority = st.number_input(
            "Minority interest (Rp)",
            value=float(payload["minority_interest"]),
            min_value=0.0,
            step=1e9,
            format="%.0f",
            key=f"{key_prefix}_mi",
        )
        non_op = st.number_input(
            "Non-operating assets (Rp)",
            value=float(payload["non_operating_assets"]),
            min_value=0.0,
            step=1e9,
            format="%.0f",
            key=f"{key_prefix}_noa",
        )
        shares = st.number_input(
            "Shares outstanding",
            value=float(payload["shares"]),
            min_value=1.0,
            step=1e6,
            format="%.0f",
            key=f"{key_prefix}_shares",
        )

    if terminal_growth >= wacc:
        st.error(
            f"Terminal growth ({terminal_growth:.2%}) must be below WACC ({wacc:.2%}). "
            "The terminal value is infinite or negative otherwise, so the model will not run."
        )
        return None

    return {
        "base_revenue": base_revenue,
        "base_ebit_margin": float(payload["base_ebit_margin"]),
        "operating_margin": operating_margin,
        "tax_rate": tax_rate,
        "revenue_growth": revenue_growth,
        "growth_fade": growth_fade,
        "da_pct_revenue": da_pct,
        "capex_pct_revenue": capex_pct,
        "nwc_pct_revenue": nwc_pct,
        "wacc": wacc,
        "terminal_growth": terminal_growth,
        "years": int(years),
        "net_debt": net_debt,
        "minority_interest": minority,
        "non_operating_assets": non_op,
        "shares": shares,
    }


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("VAL", "Valuation Lab")
    st.caption(
        "A discounted cash flow is a scenario generator, not an oracle. Every output below is reproducible "
        "from the assumptions shown, and nothing is written to the database unless you press Save."
    )
    ticker = ticker_selector(repos, key="valuation_ticker")
    if ticker is None:
        return

    try:
        analysis = analyze_ticker(repos, ticker, settings)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Analysis failed: {type(exc).__name__}: {exc}")
        return
    if analysis is None:
        empty_state(f"No statements stored for {ticker}.")
        return

    snapshot = analysis.snapshot
    st.write(
        f"**{snapshot.company_name}** | sector {snapshot.sector} | profile `{snapshot.profile}` | "
        f"basis {snapshot.period_label}"
    )

    if analysis.dcf_assumptions is None:
        st.warning(
            "A DCF cannot be built for this company from the imported data: "
            + (" ".join(analysis.valuation_notes[:4]) or "operating income, revenue or net debt is missing.")
        )
        st.info(
            "Banks are deliberately excluded from unlevered FCFF: interest is an operating item, so enterprise "
            "value is not a meaningful construct. Use the P/B and justified P/B figures on the company page."
        )
        _show_reference_valuation(analysis)
        return

    payload = _seed_payload(analysis.dcf_assumptions)
    st.caption(
        "Defaults come from the latest reported period. Operating margin, D&A and capex default to trailing "
        "values; D&A or capex is substituted from a documented rule when it was not reported."
    )

    assumptions_payload = _form(payload, key_prefix=f"dcf_{ticker}")
    if assumptions_payload is None:
        return

    try:
        assumptions = DCFAssumptions(**assumptions_payload)
        result = run_dcf(assumptions)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Invalid assumptions: {exc}")
        return

    price = snapshot.price
    mos = (result.per_share - price) / result.per_share if (price and result.per_share and result.per_share > 0) else None
    required_mos = st.slider(
        "Required margin of safety",
        min_value=0.0,
        max_value=0.50,
        value=float(settings.policy.buy_min_margin_of_safety),
        step=0.01,
        help="Used only to show the entry price this assumption set would require. It does not change the DCF.",
    )

    st.markdown("#### Result")
    columns = st.columns(4)
    columns[0].metric("Enterprise value", format_idr(result.enterprise_value, compact=True))
    columns[1].metric("Equity value", format_idr(result.equity_value, compact=True))
    columns[2].metric("Value per share", format_idr(result.per_share))
    columns[3].metric("Margin of safety", format_percent(mos))

    extra = st.columns(3)
    extra[0].metric("Terminal share of EV", format_percent(result.terminal_share))
    extra[1].metric("Current price", format_idr(price) if price else "n/a")
    if result.per_share and result.per_share > 0:
        entry = result.per_share * (1.0 - required_mos)
        extra[2].metric(
            f"Entry price at {required_mos:.0%} MoS",
            format_idr(entry),
            delta=f"{'above' if price and price > entry else 'below'} current" if price else None,
        )

    for warning in result.warnings:
        st.warning(warning)
    if result.per_share is None or result.per_share <= 0:
        st.error(
            "The implied equity value per share is zero or negative. Treat this assumption set as not meaningful "
            "rather than as a valuation."
        )

    tab_schedule, tab_sensitivity, tab_bridge, tab_save = st.tabs(
        ["Projection", "Sensitivity", "Bridge", "Save and compare"]
    )

    with tab_schedule:
        st.dataframe(
            result.frame(),
            hide_index=True,
            width="stretch",
            column_config={
                "growth": st.column_config.NumberColumn(format="%.2f%%"),
                "revenue": st.column_config.NumberColumn(format="%.0f"),
                "ebit": st.column_config.NumberColumn(format="%.0f"),
                "nopat": st.column_config.NumberColumn(format="%.0f"),
                "da": st.column_config.NumberColumn(format="%.0f"),
                "capex": st.column_config.NumberColumn(format="%.0f"),
                "delta_nwc": st.column_config.NumberColumn(format="%.0f"),
                "fcff": st.column_config.NumberColumn(format="%.0f"),
                "discount_factor": st.column_config.NumberColumn(format="%.4f"),
                "pv_fcff": st.column_config.NumberColumn(format="%.0f"),
            },
        )
        figure = go.Figure()
        figure.add_trace(go.Bar(x=result.frame()["year"], y=result.frame()["fcff"], name="FCFF", marker_color="#38bdf8"))
        figure.add_trace(go.Bar(x=result.frame()["year"], y=result.frame()["pv_fcff"], name="PV of FCFF", marker_color="#f59e0b"))
        figure.update_layout(
            template=PLOT_TEMPLATE,
            barmode="group",
            height=300,
            margin=dict(l=10, r=10, t=20, b=10),
            xaxis_title="Projection year",
            yaxis_title="Rp",
            legend=dict(orientation="h"),
        )
        st.plotly_chart(figure, width="stretch")
        st.caption(
            "Formula: FCFF = EBIT x (1 - tax) + D&A - capex - ΔNWC. Enterprise value = PV(FCFF) + PV(terminal value). "
            "Equity value = EV - net debt + non-operating assets - minority interest."
        )

    with tab_sensitivity:
        try:
            table = sensitivity_table(assumptions)
            st.dataframe(table, hide_index=True, width="stretch")
            st.caption(
                "Rows vary WACC, columns vary terminal growth. Blank cells indicate invalid combinations "
                "(terminal growth at or above WACC)."
            )
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Sensitivity table unavailable: {exc}")

    with tab_bridge:
        st.write(
            f"""
| Step | Amount |
|---|---|
| Present value of explicit FCFF | {format_idr(result.pv_fcff, compact=True)} |
| Present value of terminal value | {format_idr(result.pv_terminal, compact=True)} |
| **Enterprise value** | **{format_idr(result.enterprise_value, compact=True)}** |
| Less: net debt | {format_idr(-float(assumptions.net_debt or 0.0), compact=True)} |
| Plus: non-operating assets | {format_idr(assumptions.non_operating_assets, compact=True)} |
| Less: minority interest | {format_idr(-assumptions.minority_interest, compact=True)} |
| **Equity value** | **{format_idr(result.equity_value, compact=True)}** |
| Shares outstanding | {assumptions.shares:,.0f} |
| **Value per share** | **{format_idr(result.per_share)}** |
"""
        )
        st.caption("Net debt here is the figure from the latest reported balance sheet unless you changed it above.")

    with tab_save:
        st.caption("Saving stores the assumptions and the result as JSON in the local SQLite database.")
        note = st.text_input("Label for this run", value=f"{ticker} base case {format_date(_dt.date.today())}")
        if st.button("Save valuation run", type="primary"):
            repos.valuations.save(
                ticker,
                "DCF",
                result.per_share,
                assumptions.model_dump(),
                {
                    "enterprise_value": result.enterprise_value,
                    "equity_value": result.equity_value,
                    "per_share": result.per_share,
                    "terminal_share": result.terminal_share,
                    "warnings": result.warnings,
                    "label": note,
                },
            )
            repos.conn.commit()
            st.success("Valuation run saved.")

        history = repos.valuations.history(ticker, limit=15)
        if history:
            rows = []
            for item in history:
                stored_result = json.loads(item["result"])
                rows.append(
                    {
                        "Saved": item["created_at"][:16],
                        "Method": item["method"],
                        "Value / share": item["fair_value"],
                        "Label": stored_result.get("label", ""),
                        "Terminal share": stored_result.get("terminal_share"),
                    }
                )
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                width="stretch",
                column_config={
                    "Value / share": st.column_config.NumberColumn(format="Rp %.0f"),
                    "Terminal share": st.column_config.NumberColumn(format="%.0f%%"),
                },
            )
            st.caption(
                "Saved runs are historical snapshots of your assumptions. They are not re-based to new data, "
                "so compare them with the reporting period they were made against."
            )
        else:
            st.caption("No saved valuation runs for this ticker yet.")

    _show_reference_valuation(analysis)


def _show_reference_valuation(analysis) -> None:
    st.markdown("#### Reference valuation from the recommendation engine")
    st.write(
        f"Blended fair value **{format_idr(analysis.fair_value) if analysis.fair_value else 'n/a'}** — "
        f"{analysis.valuation_method}"
    )
    if analysis.relative:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Method": item.method,
                        "Median multiple": item.median_multiple,
                        "Implied value": item.implied_per_share,
                        "Sample": item.sample_size,
                        "Note": item.reason,
                    }
                    for item in analysis.relative
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Median multiple": st.column_config.NumberColumn(format="%.2fx"),
                "Implied value": st.column_config.NumberColumn(format="Rp %.0f"),
            },
        )
    for note in analysis.valuation_notes:
        st.caption(f"- {note}")
