"""Company research page.

Seven tabs: Overview, Statements, Ratios, Valuation, Peers, Risks, Notes.
Every figure is loaded from the repositories and computed by the analytics
engine; this module only arranges the output.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui import theme
from src.ui.live_widgets import quote_strip

from config.settings import EXPORT_DIR
from src.analytics.engine import analyze_ticker
from src.analytics.valuation import sensitivity_table
from src.reports.export import export_all
from src.reports.research_report import build_report, render_markdown
from src.ui.components import (
    PLOT_TEMPLATE,
    empty_state,
    factors_table,
    flags_table,
    get_repos,
    current_settings,
    history_chart,
    metrics_table,
    peer_table,
    price_chart,
    recommendation_card,
    recommendation_facts,
    score_all,
    source_block,
    statement_table,
    ticker_selector,
)
from src.utils.formatting import (
    format_date,
    format_idr,
    format_multiple,
    format_percent,
)

INCOME_ITEMS = [
    ("revenue", "Revenue"),
    ("cost_of_revenue", "Cost of revenue"),
    ("gross_profit", "Gross profit"),
    ("operating_expenses", "Operating expenses"),
    ("operating_income", "Operating income (EBIT)"),
    ("depreciation_amortization", "D&A"),
    ("ebitda", "EBITDA"),
    ("interest_expense", "Interest expense"),
    ("pretax_income", "Pre-tax income"),
    ("income_tax", "Income tax"),
    ("net_income", "Net income"),
    ("eps", "EPS (Rp)"),
]

BALANCE_ITEMS = [
    ("cash", "Cash & equivalents"),
    ("receivables", "Receivables"),
    ("inventory", "Inventory"),
    ("current_assets", "Current assets"),
    ("ppe", "Property, plant & equipment"),
    ("total_assets", "Total assets"),
    ("current_liabilities", "Current liabilities"),
    ("short_term_debt", "Short-term debt"),
    ("long_term_debt", "Long-term debt"),
    ("total_liabilities", "Total liabilities"),
    ("equity", "Total equity"),
    ("shares_outstanding", "Shares outstanding (mn)"),
]

CASHFLOW_ITEMS = [
    ("cfo", "Cash flow from operations"),
    ("capex", "Capital expenditure"),
    ("cfi", "Cash flow from investing"),
    ("cff", "Cash flow from financing"),
    ("fcf", "Free cash flow"),
    ("dividends_paid", "Dividends paid"),
]


def _line_chart(periods: List[Any], items: List[tuple[str, str]], title: str) -> None:
    rows = []
    for period in sorted(periods, key=lambda p: p.period_end):
        row: Dict[str, Any] = {"period": period.period_end.isoformat()}
        for key, label in items:
            value = period.get(key)
            row[label] = None if value is None else value / 1e9
        rows.append(row)
    frame = pd.DataFrame(rows)
    if frame.empty:
        empty_state("No periods stored.")
        return
    figure = go.Figure()
    colours = ["#38bdf8", "#f59e0b", "#34d399", "#a78bfa", "#f472b6"]
    for index, (_, label) in enumerate(items):
        if label not in frame:
            continue
        figure.add_trace(
            go.Scatter(
                x=frame["period"],
                y=frame[label],
                mode="lines+markers",
                name=label,
                line=dict(color=colours[index % len(colours)], width=2),
            )
        )
    figure.update_layout(
        title=title,
        template=PLOT_TEMPLATE,
        height=340,
        margin=dict(l=10, r=10, t=40, b=10),
        yaxis_title="Rp billion",
        xaxis_title="",
        legend=dict(orientation="h"),
    )
    st.plotly_chart(figure, width="stretch")


def _overview(repos, analysis) -> None:
    snapshot = analysis.snapshot
    company = repos.companies.get(snapshot.ticker) or {}
    columns = st.columns([2, 1])
    with columns[0]:
        st.markdown(f"#### {snapshot.company_name}")
        st.write(
            f"**Ticker** {snapshot.ticker} &nbsp;|&nbsp; **Sector** {snapshot.sector} "
            f"&nbsp;|&nbsp; **Scoring profile** `{snapshot.profile}` "
            f"&nbsp;|&nbsp; **Listing date** {format_date(company.get('listing_date'))}"
        )
        if snapshot.is_synthetic:
            st.error(
                "This is a **synthetic demonstration company**. The issuer does not exist and none of "
                "the figures reflect a real business."
            )
        if company.get("notes"):
            st.caption(company["notes"])
    with columns[1]:
        st.metric("Analysis basis", snapshot.period_label)
        st.caption(f"Period end {format_date(snapshot.as_of)} | {snapshot.ttm_explanation}")

    recommendation_card(analysis)
    recommendation_facts(analysis)

    st.markdown("#### Factor scores")
    factors_table(analysis)

    st.markdown("#### Revenue and earnings history")
    _line_chart(
        snapshot.annual,
        [("revenue", "Revenue"), ("operating_income", "EBIT"), ("net_income", "Net income")],
        "Annual revenue, operating income and net income",
    )

    st.markdown("#### Share price")
    price_chart(repos.prices.series(snapshot.ticker), title=f"{snapshot.ticker} close", ticker=snapshot.ticker)

    st.markdown("#### Recommendation reasoning")
    for reason in analysis.recommendation.reasons:
        st.markdown(f"- {reason}")
    if analysis.recommendation.vetoes:
        st.markdown("**Vetoes**")
        for veto in analysis.recommendation.vetoes:
            st.markdown(f"- {veto}")

    with st.expander("Investment thesis (bull / base / bear)"):
        _scenarios(analysis, snapshot.price)

    with st.expander("Corporate actions and share count"):
        _share_history(snapshot)

    source_block(analysis)


def _scenarios(analysis, price: Optional[float]) -> None:
    """Scenario values derived from the DCF rather than invented numbers."""
    dcf = analysis.dcf
    st.caption(
        "Scenarios are mechanical variations of the valuation assumptions, not forecasts. "
        "Each row states the assumption that changes."
    )
    rows = []
    base = analysis.fair_value
    if dcf is not None:
        try:
            table = sensitivity_table(dcf.assumptions)
            if not table.empty:
                middle = table.iloc[len(table) // 2]
                numeric = [value for key, value in middle.items() if key != "wacc" and value is not None]
                if numeric:
                    low, high = min(numeric), max(numeric)
                    rows = [
                        {
                            "Scenario": "Bear",
                            "Assumption": f"WACC {dcf.assumptions.wacc + 0.01:.1%}, terminal growth {dcf.assumptions.terminal_growth - 0.005:.1%}",
                            "Value per share": low,
                        },
                        {
                            "Scenario": "Base",
                            "Assumption": f"WACC {dcf.assumptions.wacc:.1%}, terminal growth {dcf.assumptions.terminal_growth:.1%}",
                            "Value per share": dcf.per_share,
                        },
                        {
                            "Scenario": "Bull",
                            "Assumption": f"WACC {dcf.assumptions.wacc - 0.01:.1%}, terminal growth {dcf.assumptions.terminal_growth + 0.005:.1%}",
                            "Value per share": high,
                        },
                    ]
        except Exception:  # noqa: BLE001 - scenario display must not break the page
            rows = []
    if not rows:
        st.info(
            "No DCF is available for this company (banks are valued on P/B and earnings multiples), "
            "so bull/base/bear values are not shown. The blended fair value is "
            + (format_idr(base) if base else "unavailable")
            + "."
        )
    else:
        frame = pd.DataFrame(rows)
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            column_config={
                "Value per share": st.column_config.NumberColumn(format="Rp %.0f"),
            },
        )
        if price:
            frame["Upside vs price"] = frame["Value per share"] / price - 1.0
            st.dataframe(
                frame[["Scenario", "Upside vs price"]],
                hide_index=True,
                width="stretch",
                column_config={"Upside vs price": st.column_config.NumberColumn(format="%.1f%%")},
            )
        st.caption("Blended fair value across available methods: " + (format_idr(base) if base else "n/a"))
    st.markdown("**Conditions that would invalidate the thesis**")
    for condition in analysis.recommendation.invalidation_conditions:
        st.markdown(f"- {condition}")


def _share_history(snapshot) -> None:
    rows = [
        (period.period_end, period.get("shares_outstanding"))
        for period in snapshot.periods
        if period.get("shares_outstanding")
    ]
    rows.sort()
    if len(rows) < 2:
        st.caption("Only one share-count observation is stored; no corporate-action inference is possible.")
        return
    frame = pd.DataFrame(rows, columns=["Period end", "Shares"])
    frame["Change"] = frame["Shares"].pct_change()
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={
            "Shares": st.column_config.NumberColumn(format="%.0f"),
            "Change": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    first, last = frame["Shares"].iloc[0], frame["Shares"].iloc[-1]
    if first and abs(last / first - 1.0) > 0.05:
        st.warning(
            "The share count changed materially over the stored history (splits, rights issues, buybacks or "
            "conversions). Price and EPS history must be adjusted before comparison unless the imported price "
            "series is already adjusted."
        )
    else:
        st.caption("Share count is stable within 5% across the stored history.")


def _statements(repos, analysis) -> None:
    snapshot = analysis.snapshot
    basis_label = "consolidated" if (snapshot.latest_period and snapshot.latest_period.is_consolidated) else "parent-only"
    st.caption(f"Showing {basis_label} statements. Values in Rp billion, converted from reported units.")

    tab_income, tab_balance, tab_cash = st.tabs(["Income statement", "Balance sheet", "Cash flow"])
    with tab_income:
        statement_table(snapshot.periods, INCOME_ITEMS)
        _line_chart(
            snapshot.annual,
            [("gross_profit", "Gross profit"), ("operating_income", "EBIT"), ("net_income", "Net income")],
            "Annual earnings build",
        )
    with tab_balance:
        statement_table(snapshot.periods, BALANCE_ITEMS)
        _line_chart(
            snapshot.annual,
            [("total_assets", "Total assets"), ("total_liabilities", "Total liabilities"), ("equity", "Equity")],
            "Balance sheet",
        )
    with tab_cash:
        statement_table(snapshot.periods, CASHFLOW_ITEMS)
        _line_chart(
            snapshot.annual,
            [("cfo", "Operating cash flow"), ("capex", "Capex"), ("fcf", "Free cash flow")],
            "Cash generation",
        )

    with st.expander("Balance sheet analysis"):
        metrics_table(
            [
                metric
                for key in ("current_ratio", "quick_ratio", "debt_to_equity", "net_debt", "net_debt_ebitda", "interest_coverage", "equity_to_assets")
                if (metric := snapshot.metrics.get(key)) is not None
            ]
        )
        st.caption(
            "Working-capital and leverage ratios use the ending balance, or the average of the two most recent "
            "period ends where a prior period exists. Banks suppress these ratios because they are not meaningful."
        )

    with st.expander("Cash flow analysis"):
        metrics_table(
            [
                metric
                for key in ("cfo_to_net_income", "fcf", "fcf_margin", "dividend_payout")
                if (metric := snapshot.metrics.get(key)) is not None
            ]
        )


def _ratios(analysis) -> None:
    snapshot = analysis.snapshot
    groups = [
        ("Profitability", "profitability"),
        ("Growth", "growth"),
        ("Financial health", "health"),
        ("Cash flow", "cashflow"),
        ("Valuation", "valuation"),
        ("Momentum and liquidity", "momentum"),
        ("Reported statement lines", "statement"),
    ]
    for label, group in groups:
        metrics = [metric for metric in snapshot.metrics.values() if metric.group == group]
        if not metrics:
            continue
        st.markdown(f"##### {label}")
        metrics_table(metrics)
        st.markdown("")


def _valuation(analysis, settings) -> None:
    snapshot = analysis.snapshot
    columns = st.columns(3)
    columns[0].metric("Price", format_idr(snapshot.price) if snapshot.price else "n/a")
    columns[1].metric("Blended fair value", format_idr(analysis.fair_value) if analysis.fair_value else "n/a")
    columns[2].metric("Margin of safety", format_percent(analysis.value("margin_of_safety")))

    st.caption(
        f"Method: {analysis.valuation_method}. The blended value renormalises the weights across whichever methods "
        "actually produced a value; a missing method is never replaced with zero."
    )

    metrics_table(
        [
            metric
            for key in ("market_cap", "pe", "pb", "ev_ebitda", "ev_sales", "fcf_yield", "dividend_yield", "margin_of_safety", "expected_return")
            if (metric := snapshot.metrics.get(key)) is not None
        ]
    )

    st.markdown("#### Relative valuation")
    if analysis.relative:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Method": item.method,
                        "Multiple used": item.median_multiple,
                        "Implied value / share": item.implied_per_share,
                        "Sample": item.sample_size,
                        "Source": item.source,
                        "Note": item.reason,
                    }
                    for item in analysis.relative
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Multiple used": st.column_config.NumberColumn(format="%.2fx"),
                "Implied value / share": st.column_config.NumberColumn(format="Rp %.0f"),
            },
        )
    else:
        empty_state("No peer multiples were available, so relative valuation was not produced.")

    st.markdown("#### Discounted cash flow")
    if analysis.dcf is None:
        st.info(
            "DCF is not available for this company. "
            + " ".join(analysis.valuation_notes[:3])
        )
        return

    dcf = analysis.dcf
    assumptions = dcf.assumptions
    left, right = st.columns(2)
    with left:
        st.write(
            f"""
- Base revenue: **{format_idr(assumptions.base_revenue, compact=True)}**
- Operating margin: **{format_percent(assumptions.operating_margin)}** (trailing {format_percent(assumptions.base_ebit_margin)})
- Year-1 growth: **{format_percent(assumptions.revenue_growth)}** fading to **{format_percent(assumptions.terminal_growth)}**
- Tax rate: **{format_percent(assumptions.tax_rate)}**
- D&A: **{format_percent(assumptions.da_pct_revenue)}** of revenue
- Capex: **{format_percent(assumptions.capex_pct_revenue)}** of revenue
- ΔNWC: **{format_percent(assumptions.nwc_pct_revenue)}** of revenue change
"""
        )
    with right:
        st.write(
            f"""
- WACC: **{format_percent(assumptions.wacc)}**
- Terminal growth: **{format_percent(assumptions.terminal_growth)}**
- Horizon: **{assumptions.years}** years
- Net debt: **{format_idr(assumptions.net_debt, compact=True)}**
- Minority interest: **{format_idr(assumptions.minority_interest, compact=True)}**
- Non-operating assets: **{format_idr(assumptions.non_operating_assets, compact=True)}**
- Shares: **{assumptions.shares:,.0f}**
"""
        )
    st.write(
        f"Enterprise value **{format_idr(dcf.enterprise_value, compact=True)}** | "
        f"equity value **{format_idr(dcf.equity_value, compact=True)}** | "
        f"per share **{format_idr(dcf.per_share)}** | "
        f"terminal value is **{dcf.terminal_share:.0%}** of enterprise value."
    )
    for warning in dcf.warnings:
        st.warning(warning)

    with st.expander("DCF projection schedule"):
        schedule = dcf.frame()
        st.dataframe(
            schedule,
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
                "discount_factor": st.column_config.NumberColumn(format="%.3f"),
                "pv_fcff": st.column_config.NumberColumn(format="%.0f"),
            },
        )

    with st.expander("Sensitivity to WACC and terminal growth (Rp per share)"):
        try:
            table = sensitivity_table(assumptions)
            st.dataframe(table, hide_index=True, width="stretch")
            st.caption(
                "Each cell re-runs the full DCF with the row WACC and column terminal growth. "
                "Cells where terminal growth is at or above WACC are blank because the model is invalid there."
            )
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Sensitivity table could not be built: {exc}")


def _peers(repos, analysis) -> None:
    snapshot = analysis.snapshot
    company = repos.companies.get(snapshot.ticker) or {}
    sector = company.get("sector")
    if not sector:
        empty_state("This company has no sector assigned, so sector peers cannot be identified.")
        return
    settings = current_settings(repos)
    peer_rows = []
    for peer in repos.companies.by_sector(sector):
        if peer["ticker"] == snapshot.ticker:
            continue
        peer_rows.append(
            {
                "ticker": peer["ticker"],
                "name": peer.get("name"),
                "pe": None,
                "pb": None,
                "roe": None,
                "net_margin": None,
                "score": None,
                "decision": "n/a",
            }
        )
    if not peer_rows:
        empty_state(f"No other company in sector **{sector}** has stored data.")
        return
    # Fill peer metrics from a single cached screen call.
    screen = {row["ticker"]: row for row in score_all(tuple(repos.financials.all_tickers_with_data()), _settings_json(settings))}
    for row in peer_rows:
        source = screen.get(row["ticker"])
        if source:
            row.update(
                {
                    "pe": source.get("pe"),
                    "pb": source.get("pb"),
                    "roe": source.get("roe"),
                    "net_margin": source.get("net_margin"),
                    "score": source.get("score"),
                    "decision": source.get("decision") or "n/a",
                }
            )
    st.caption(f"Sector **{sector}** has {len(peer_rows)} other company/companies with stored data.")
    peer_table(analysis, peer_rows)

    frame_rows = [{"Ticker": row["ticker"], "P/E": row["pe"], "P/B": row["pb"], "ROE": row["roe"]} for row in peer_rows]
    frame_rows.append({"Ticker": snapshot.ticker, "P/E": analysis.value("pe"), "P/B": analysis.value("pb"), "ROE": analysis.value("roe")})
    frame = pd.DataFrame(frame_rows)
    figure = go.Figure()
    for metric in ("P/E", "P/B"):
        figure.add_trace(
            go.Bar(name=metric, x=frame["Ticker"], y=frame[metric], marker_color="#38bdf8" if metric == "P/E" else "#f59e0b")
        )
    figure.update_layout(
        template=PLOT_TEMPLATE,
        barmode="group",
        height=300,
        margin=dict(l=10, r=10, t=20, b=10),
        yaxis_title="Multiple (x)",
        legend=dict(orientation="h"),
    )
    st.plotly_chart(figure, width="stretch")


def _settings_json(settings) -> str:
    import json

    return json.dumps(settings.as_dict(), sort_keys=True, default=str)


def _risks(analysis) -> None:
    st.markdown("#### Risk flags")
    st.caption(
        "Flags are generated by explicit rules over the imported data. They are not a judgement about "
        "governance, management quality or regulation, which this model cannot observe."
    )
    flags_table(analysis.flags)

    st.markdown("#### Principal risks in the recommendation")
    for risk in analysis.recommendation.principal_risks:
        st.markdown(f"- {risk}")

    st.markdown("#### Valuation caveats")
    for note in analysis.valuation_notes:
        st.markdown(f"- {note}")

    st.markdown("#### Data quality")
    rec = analysis.recommendation
    st.write(
        f"Factor coverage **{rec.data_completeness:.0%}**, confidence **{rec.confidence:.0%}**. "
        "Confidence measures evidence quality and model limitations, not the probability of profit."
    )
    if rec.confidence < 0.55:
        st.error("Confidence is below the policy minimum, so no actionable recommendation is issued.")
    elif rec.confidence < 0.70:
        st.warning("Confidence is below the BUY threshold; a high score alone cannot produce a BUY.")


def _notes(repos, analysis, settings) -> None:
    snapshot = analysis.snapshot
    ticker = snapshot.ticker
    st.markdown("#### Research notes")
    notes = repos.notes.for_ticker(ticker)
    if notes:
        for note in notes:
            with st.expander(f"{note['title']} — updated {note['updated_at'][:10]}"):
                st.write(note["body"])
                if note.get("tags"):
                    st.caption("Tags: " + note["tags"])
                if st.button("Delete note", key=f"del_note_{note['id']}"):
                    repos.notes.delete(note["id"])
                    repos.conn.commit()
                    st.rerun()
    else:
        st.caption("No notes saved for this ticker yet.")

    with st.form(f"note_form_{ticker}"):
        title = st.text_input("Title", value=f"{ticker} thesis notes")
        body = st.text_area("Note", height=160, placeholder="Facts, calculations, assumptions and opinions — label which is which.")
        tags = st.text_input("Tags (comma separated)", value="")
        submitted = st.form_submit_button("Save note", type="primary")
    if submitted:
        if not body.strip():
            st.error("A note body is required.")
        else:
            repos.notes.add(ticker, title.strip() or f"{ticker} note", body.strip(), tags.strip())
            repos.conn.commit()
            st.success("Note saved locally.")
            st.rerun()

    st.markdown("#### Download a research report")
    st.caption(
        "The report is built from the same analysis object you are reading. PDF and Excel are generated locally."
    )
    if st.button("Build report bundle (PDF, Excel, Markdown, JSON, CSV)"):
        report = build_report(repos, ticker, settings)
        if report is None:
            st.error("No analysis is available for this ticker.")
        else:
            markdown = render_markdown(report)
            paths = export_all(report, ticker, markdown)
            st.success("Report written to " + str(EXPORT_DIR))
            for label, path in paths.items():
                if label == "pdf_error":
                    st.warning(f"PDF export failed: {path}")
                    continue
                st.write(f"- **{label}**: `{path}`")
            st.download_button(
                "Download Markdown",
                data=markdown.encode("utf-8"),
                file_name=f"{ticker}-research.md",
                mime="text/markdown",
            )

    st.markdown("#### Source documents")
    for period in snapshot.periods[:8]:
        sources = ", ".join(period.source_urls) if period.source_urls else "no source URL recorded"
        st.markdown(
            f"- **{period.period_end.isoformat()}** ({period.period_type}, "
            f"{'consolidated' if period.is_consolidated else 'parent-only'}) — published "
            f"{format_date(period.publication_date)} — {sources}"
        )


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("DES", "Company Research")

    ticker = ticker_selector(repos, key="research_ticker")
    if ticker is None:
        return

    try:
        analysis = analyze_ticker(repos, ticker, settings)
    except Exception as exc:  # noqa: BLE001 - show the failure instead of a blank page
        st.error(f"Analysis failed for {ticker}: {type(exc).__name__}: {exc}")
        return
    if analysis is None:
        empty_state(f"No financial statements are stored for {ticker}. Import them from Data Management.")
        return

    quote_strip(
        repos,
        ticker,
        basis_price=analysis.snapshot.price,
        basis_date=analysis.snapshot.price_date.isoformat() if analysis.snapshot.price_date else None,
        key="research",
    )

    tabs = st.tabs(["Overview", "Statements", "Ratios", "Valuation", "Peers", "Risks", "Notes"])
    with tabs[0]:
        _overview(repos, analysis)
    with tabs[1]:
        _statements(repos, analysis)
    with tabs[2]:
        _ratios(analysis)
    with tabs[3]:
        _valuation(analysis, settings)
    with tabs[4]:
        _peers(repos, analysis)
    with tabs[5]:
        _risks(analysis)
    with tabs[6]:
        _notes(repos, analysis, settings)
