"""Research report assembly.

The report is a plain dictionary so it can be rendered to Markdown, PDF, Excel
or JSON without duplicating logic. Every number in it comes from the analysis
object; nothing is retyped by hand.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from config.settings import TerminalSettings
from src.analytics.engine import CompanyAnalysis, analyze_ticker
from src.utils.formatting import (
    format_date,
    format_idr,
    format_metric_value,
    format_multiple,
    format_percent,
)


def build_report(
    repos,
    ticker: str,
    settings: Optional[TerminalSettings] = None,
    horizon_years: float = 3.0,
    include_statements: bool = True,
) -> Optional[Dict[str, Any]]:
    settings = settings or TerminalSettings()
    analysis = analyze_ticker(repos, ticker, settings, horizon_years=horizon_years)
    if analysis is None:
        return None

    company = repos.companies.get(ticker) or {}
    snapshot = analysis.snapshot
    rec = analysis.recommendation
    notes = repos.notes.for_ticker(ticker)

    metrics = {
        group: [
            metric.as_dict()
            for metric in snapshot.metrics.values()
            if metric.group == group
        ]
        for group in ("profitability", "growth", "health", "cashflow", "valuation", "momentum", "statement")
    }

    statements: List[Dict[str, Any]] = []
    if include_statements:
        for period in sorted(snapshot.periods, key=lambda p: p.period_end, reverse=True)[:8]:
            statements.append(
                {
                    "period_end": period.period_end.isoformat(),
                    "period_type": period.period_type,
                    "basis": "consolidated" if period.is_consolidated else "parent-only",
                    "values": dict(period.values),
                    "source_urls": list(period.source_urls),
                    "publication_date": period.publication_date.isoformat() if period.publication_date else None,
                }
            )

    peer_rows = []
    if company.get("sector"):
        for peer in repos.companies.by_sector(company["sector"]):
            if peer["ticker"] == ticker:
                continue
            try:
                peer_analysis = analyze_ticker(repos, peer["ticker"], settings)
            except Exception:
                continue
            if peer_analysis is None:
                continue
            peer_rows.append(
                {
                    "ticker": peer["ticker"],
                    "name": peer.get("name"),
                    "pe": peer_analysis.value("pe"),
                    "pb": peer_analysis.value("pb"),
                    "roe": peer_analysis.value("roe"),
                    "net_margin": peer_analysis.value("net_margin"),
                    "score": peer_analysis.score,
                    "decision": peer_analysis.decision,
                }
            )

    return {
        "generated_at": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "ticker": ticker,
        "company": company,
        "analysis": analysis,
        "recommendation": rec.as_dict(),
        "metrics": metrics,
        "statements": statements,
        "peers": peer_rows,
        "notes": notes,
        "valuation": {
            "fair_value": analysis.fair_value,
            "method": analysis.valuation_method,
            "dcf": analysis.dcf.as_dict() if analysis.dcf else None,
            "relative": [item.as_dict() for item in analysis.relative],
            "notes": analysis.valuation_notes,
        },
        "synthetic": snapshot.is_synthetic,
        "settings": settings.as_dict(),
    }


def _metric_line(metric: Dict[str, Any]) -> str:
    from src.analytics.fundamentals import Metric

    m = Metric(
        key=metric["key"],
        label=metric["label"],
        value=metric.get("value"),
        unit=metric.get("unit", "multiple"),
        status=metric.get("status", "calculated"),
    )
    value = format_metric_value(m)
    status = metric.get("status", "")
    note = metric.get("note") or ""
    suffix = f" ({status})" if status not in {"calculated", ""} else ""
    return f"| {metric['label']} | {value}{suffix} | {note} |"


def render_markdown(report: Dict[str, Any]) -> str:
    analysis: CompanyAnalysis = report["analysis"]
    snapshot = analysis.snapshot
    rec = analysis.recommendation
    company = report["company"] or {}

    lines: List[str] = []
    title = f"# {report['ticker']} - {snapshot.company_name}"
    lines.append(title)
    lines.append("")
    if report.get("synthetic"):
        lines.append("> **SYNTHETIC DEMONSTRATION DATA.** This issuer does not exist. Nothing in this document is investment research.")
        lines.append("")
    lines.append(f"Generated {format_date(report['generated_at'][:10])} | Sector: {snapshot.sector} | Profile: {snapshot.profile}")
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"**{rec.decision}** | Score {rec.score if rec.score is not None else 'n/a'}/100 | "
                 f"Confidence {rec.confidence:.0%} | Data completeness {rec.data_completeness:.0%}")
    lines.append("")
    lines.append(f"- Current price: {format_idr(rec.price) if rec.price else 'n/a'}"
                 + (f" (as of {format_date(rec.price_date)})" if rec.price_date else ""))
    lines.append(f"- Estimated fair value: {format_idr(rec.fair_value) if rec.fair_value else 'n/a'} ({rec.valuation_method})")
    lines.append(f"- Margin of safety: {format_percent(rec.margin_of_safety) if rec.margin_of_safety is not None else 'n/a'}")
    lines.append(f"- Expected annual return (scenario, {rec.horizon_years:g}y): "
                 f"{format_percent(rec.expected_return) if rec.expected_return is not None else 'n/a'}")
    lines.append(f"- Fundamentals period: {snapshot.period_label} ({format_date(snapshot.as_of)})")
    lines.append("")

    lines.append("### Why")
    for reason in rec.reasons:
        lines.append(f"- {reason}")
    if rec.vetoes:
        lines.append("")
        lines.append("### Vetoes")
        for veto in rec.vetoes:
            lines.append(f"- {veto}")
    lines.append("")

    lines.append("## Factor scores")
    lines.append("")
    lines.append("| Factor | Weight | Score | Contribution |")
    lines.append("|---|---|---|---|")
    for factor in rec.factors.values():
        lines.append(
            f"| {factor.label} | {factor.weight:.0%} | "
            f"{factor.score:.0f} | {factor.contribution:.1f} |" if factor.score is not None
            else f"| {factor.label} | {factor.weight:.0%} | n/a | n/a |"
        )
    lines.append("")

    lines.append("## Key metrics")
    for group, metrics in report["metrics"].items():
        if not metrics:
            continue
        lines.append("")
        lines.append(f"### {group.title()}")
        lines.append("")
        lines.append("| Metric | Value | Note |")
        lines.append("|---|---|---|")
        for metric in metrics:
            lines.append(_metric_line(metric))
    lines.append("")

    lines.append("## Risks")
    for flag in analysis.flags:
        lines.append(f"- **{flag.severity.upper()}** {flag.message}")
    if not analysis.flags:
        lines.append("- No flags triggered by the imported data.")
    lines.append("")

    lines.append("## Thesis invalidation")
    for condition in rec.invalidation_conditions:
        lines.append(f"- {condition}")
    lines.append("")

    lines.append("## Valuation detail")
    lines.append("")
    if analysis.dcf is not None:
        dcf = analysis.dcf
        a = dcf.assumptions
        lines.append("### DCF assumptions")
        lines.append("")
        lines.append(f"- Base revenue: {format_idr(a.base_revenue, compact=True)}")
        lines.append(f"- Operating margin: {format_percent(a.operating_margin)} (trailing {format_percent(a.base_ebit_margin)})")
        lines.append(f"- Revenue growth year 1: {format_percent(a.revenue_growth)} fading to {format_percent(a.terminal_growth)}")
        lines.append(f"- Tax rate: {format_percent(a.tax_rate)} | D&A {format_percent(a.da_pct_revenue)} of revenue | Capex {format_percent(a.capex_pct_revenue)} of revenue")
        lines.append(f"- WACC: {format_percent(a.wacc)} | Terminal growth: {format_percent(a.terminal_growth)} | Horizon: {a.years} years")
        lines.append(f"- Net debt: {format_idr(a.net_debt, compact=True)} | Shares: {a.shares:,.0f}")
        lines.append("")
        lines.append(f"Enterprise value {format_idr(dcf.enterprise_value, compact=True)}; "
                     f"equity value {format_idr(dcf.equity_value, compact=True)}; "
                     f"per share {format_idr(dcf.per_share)}.")
        lines.append("")
        lines.append(f"Terminal value is {dcf.terminal_share:.0%} of enterprise value.")
        for warning in dcf.warnings:
            lines.append(f"- Warning: {warning}")
    else:
        lines.append("DCF not available for this company: " + "; ".join(report["valuation"]["notes"]))

    if analysis.relative:
        lines.append("")
        lines.append("### Relative valuation")
        lines.append("")
        lines.append("| Method | Peer median | Implied value/share | Sample | Note |")
        lines.append("|---|---|---|---|---|")
        for item in analysis.relative:
            lines.append(
                f"| {item.method} | {format_multiple(item.median_multiple)} | "
                f"{format_idr(item.implied_per_share) if item.implied_per_share else 'n/a'} | "
                f"{item.sample_size} | {item.reason or 'applied'} |"
            )
    lines.append("")

    lines.append("## Data provenance")
    lines.append("")
    lines.append(f"- Periods used: {len(snapshot.periods)} stored periods; analysis basis {snapshot.period_label}")
    for period in sorted(snapshot.periods, key=lambda p: p.period_end, reverse=True)[:6]:
        published = format_date(period.publication_date) if period.publication_date else "not recorded"
        sources = ", ".join(period.source_urls) if period.source_urls else "no source URL recorded"
        lines.append(f"- {period.period_end.isoformat()} ({period.period_type}, "
                     f"{'consolidated' if period.is_consolidated else 'parent-only'}): published {published}; {sources}")
    if not snapshot.periods:
        lines.append("- No periods available.")
    lines.append("")

    if report["peers"]:
        lines.append("## Peer comparison")
        lines.append("")
        lines.append("| Ticker | P/E | P/B | ROE | Net margin | Score | Decision |")
        lines.append("|---|---|---|---|---|---|---|")
        for peer in report["peers"]:
            lines.append(
                f"| {peer['ticker']} | {format_multiple(peer['pe'])} | {format_multiple(peer['pb'])} | "
                f"{format_percent(peer['roe'])} | {format_percent(peer['net_margin'])} | "
                f"{peer['score']:.0f} | {peer['decision']} |" if peer['score'] is not None
                else f"| {peer['ticker']} | {format_multiple(peer['pe'])} | {format_multiple(peer['pb'])} | "
                     f"{format_percent(peer['roe'])} | {format_percent(peer['net_margin'])} | n/a | {peer['decision']} |"
            )
        lines.append("")

    if report["notes"]:
        lines.append("## Research notes")
        for note in report["notes"]:
            lines.append(f"### {note['title']} ({note['updated_at'][:10]})")
            lines.append(note["body"])
            lines.append("")

    lines.append("---")
    lines.append(
        "Method: rule-based scoring with configurable thresholds; fair value blends DCF and sector multiples. "
        "Estimates are scenarios, not guarantees. This document is research support, not investment advice, "
        "and no order is placed by this application."
    )
    return "\n".join(lines)
