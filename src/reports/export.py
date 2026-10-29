"""Export research reports to PDF, Excel, Markdown, JSON and CSV.

All exporters take the dictionary produced by ``research_report.build_report`` so
they cannot drift from the analysis.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config.settings import EXPORT_DIR

try:  # pragma: no cover - optional at import time but declared in requirements
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    REPORTLAB_AVAILABLE = True
except Exception:  # pragma: no cover
    REPORTLAB_AVAILABLE = False


def safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return cleaned or "report"


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _target(directory: Optional[Path], ticker: str, extension: str) -> Path:
    directory = Path(directory) if directory else EXPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe_filename(ticker)}-research-{_stamp()}.{extension}"


# --------------------------------------------------------------------------- #
# Markdown / JSON / CSV
# --------------------------------------------------------------------------- #


def export_markdown(markdown: str, ticker: str, directory: Optional[Path] = None) -> Path:
    from src.reports.research_report import render_markdown  # noqa: F401  (documented dependency)

    path = _target(directory, ticker, "md")
    path.write_text(markdown, encoding="utf-8")
    return path


def export_json(report: Dict[str, Any], ticker: str, directory: Optional[Path] = None) -> Path:
    path = _target(directory, ticker, "json")
    payload = {key: value for key, value in report.items() if key not in {"analysis"}}
    payload["recommendation"] = report["recommendation"]
    payload["metrics"] = report["metrics"]
    payload["valuation"] = {
        key: value for key, value in report["valuation"].items() if key != "dcf"
    }
    if report["valuation"].get("dcf"):
        dcf = dict(report["valuation"]["dcf"])
        dcf.pop("assumptions", None)
        payload["valuation"]["dcf"] = dcf
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def export_metrics_csv(report: Dict[str, Any], ticker: str, directory: Optional[Path] = None) -> Path:
    rows = [metric for group in report["metrics"].values() for metric in group]
    path = _target(directory, ticker, "csv")
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #


def _metrics_frame(report: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for group, metrics in report["metrics"].items():
        for metric in metrics:
            rows.append(
                {
                    "group": group,
                    "metric": metric["label"],
                    "key": metric["key"],
                    "value": metric.get("value"),
                    "unit": metric.get("unit"),
                    "status": metric.get("status"),
                    "formula": metric.get("formula"),
                    "note": metric.get("note"),
                }
            )
    return pd.DataFrame(rows)


def _statements_frame(report: Dict[str, Any]) -> pd.DataFrame:
    records = []
    for statement in report["statements"]:
        for item, value in (statement.get("values") or {}).items():
            records.append(
                {
                    "period_end": statement["period_end"],
                    "period_type": statement["period_type"],
                    "basis": statement["basis"],
                    "line_item": item,
                    "value_idr": value,
                    "publication_date": statement.get("publication_date"),
                }
            )
    return pd.DataFrame(records)


def export_excel(report: Dict[str, Any], ticker: str, directory: Optional[Path] = None) -> Path:
    path = _target(directory, ticker, "xlsx")
    rec = report["recommendation"]
    summary = pd.DataFrame(
        [
            ("Ticker", rec["ticker"]),
            ("Company", rec["company_name"]),
            ("Recommendation", rec["decision"]),
            ("Score", rec["score"]),
            ("Confidence", rec["confidence"]),
            ("Data completeness", rec["data_completeness"]),
            ("Price", rec["price"]),
            ("Price date", rec["price_date"]),
            ("Fair value", rec["fair_value"]),
            ("Valuation method", rec["valuation_method"]),
            ("Margin of safety", rec["margin_of_safety"]),
            ("Expected annual return (scenario)", rec["expected_return"]),
            ("Fundamentals period", rec["period_label"]),
            ("Analysis timestamp", rec["as_of"]),
            ("Sector", report["company"].get("sector") if report["company"] else None),
            ("Synthetic data", report["synthetic"]),
        ],
        columns=["item", "value"],
    )
    factors = pd.DataFrame(
        [
            {
                "factor": factor["label"],
                "weight": factor["weight"],
                "score": factor["score"],
                "contribution": factor["contribution"],
            }
            for factor in rec["factors"].values()
        ]
    )
    risks = pd.DataFrame(rec["flags"], columns=["severity", "code", "message", "thesis_impact"])
    peers = pd.DataFrame(report["peers"])
    dcf_frame = pd.DataFrame()
    if report["valuation"].get("dcf"):
        dcf_frame = pd.DataFrame(report["valuation"]["dcf"]["schedule"])
    relative = pd.DataFrame(
        [
            {
                "method": item["method"],
                "peer_median": item["median_multiple"],
                "implied_per_share": item["implied_per_share"],
                "sample": item["sample_size"],
                "note": item["reason"],
            }
            for item in report["valuation"]["relative"]
        ]
    )
    notes = pd.DataFrame(report["notes"])

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        factors.to_excel(writer, sheet_name="Factors", index=False)
        _metrics_frame(report).to_excel(writer, sheet_name="Metrics", index=False)
        risks.to_excel(writer, sheet_name="Risks", index=False)
        if not dcf_frame.empty:
            dcf_frame.to_excel(writer, sheet_name="DCF", index=False)
        relative.to_excel(writer, sheet_name="Relative", index=False)
        _statements_frame(report).to_excel(writer, sheet_name="Statements", index=False)
        if not peers.empty:
            peers.to_excel(writer, sheet_name="Peers", index=False)
        if not notes.empty:
            notes.to_excel(writer, sheet_name="Notes", index=False)
    return path


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def _safe(text: Any) -> str:
    """Restrict text to characters Helvetica can render."""
    value = str(text)
    return value.encode("latin-1", "replace").decode("latin-1")


def _idr(value: Optional[float], compact: bool = True) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    if compact:
        if abs(number) >= 1e12:
            return f"Rp {number / 1e12:,.2f} T"
        if abs(number) >= 1e9:
            return f"Rp {number / 1e9:,.2f} bn"
        if abs(number) >= 1e6:
            return f"Rp {number / 1e6:,.1f} mn"
    return f"Rp {number:,.0f}"


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _table(data: List[List[str]], widths: Optional[List[float]] = None) -> Table:
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9ca3af")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def export_pdf(report: Dict[str, Any], ticker: str, directory: Optional[Path] = None) -> Path:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab is not installed; run pip install -r requirements.txt")
    path = _target(directory, ticker, "pdf")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="Tiny", parent=styles["BodyText"], fontSize=7, leading=9, textColor=colors.HexColor("#4b5563")))

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=_safe(f"{ticker} research report"),
        author="Indonesian Equity Research Terminal",
    )

    rec = report["recommendation"]
    snapshot = report["analysis"].snapshot
    story: List[Any] = []

    story.append(Paragraph(_safe(f"{ticker} - {snapshot.company_name}"), styles["Title"]))
    story.append(Paragraph(_safe(f"Sector: {snapshot.sector} | Profile: {snapshot.profile} | Generated {report['generated_at'][:16]}"), styles["Small"]))
    if report["synthetic"]:
        story.append(Spacer(1, 4))
        story.append(
            Paragraph(
                _safe("SYNTHETIC DEMONSTRATION DATA. This issuer does not exist and nothing here is investment research."),
                styles["Small"],
            )
        )
    story.append(Spacer(1, 8))

    headline = [
        ["Recommendation", rec["decision"]],
        ["Composite score", f"{rec['score']:.1f}/100" if rec["score"] is not None else "n/a"],
        ["Confidence / data completeness", f"{rec['confidence']:.0%} / {rec['data_completeness']:.0%}"],
        ["Current price", _idr(rec["price"], compact=False) if rec["price"] else "n/a"],
        ["Estimated fair value", _idr(rec["fair_value"], compact=False) if rec["fair_value"] else "n/a"],
        ["Valuation method", rec["valuation_method"]],
        ["Margin of safety", _pct(rec["margin_of_safety"])],
        ["Expected annual return (scenario)", _pct(rec["expected_return"])],
        ["Fundamentals period", rec["period_label"]],
    ]
    story.append(_table(headline, [65 * mm, 110 * mm]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Why", styles["Heading2"]))
    for reason in rec["reasons"]:
        story.append(Paragraph(_safe(f"- {reason}"), styles["Small"]))
    if rec["vetoes"]:
        story.append(Paragraph("Vetoes", styles["Heading3"]))
        for veto in rec["vetoes"]:
            story.append(Paragraph(_safe(f"- {veto}"), styles["Small"]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Factor scores", styles["Heading2"]))
    rows = [["Factor", "Weight", "Score", "Contribution"]]
    for factor in rec["factors"].values():
        rows.append(
            [
                factor["label"],
                f"{factor['weight']:.0%}",
                f"{factor['score']:.0f}" if factor["score"] is not None else "n/a",
                f"{factor['contribution']:.1f}" if factor["contribution"] is not None else "n/a",
            ]
        )
    story.append(_table(rows, [80 * mm, 25 * mm, 25 * mm, 35 * mm]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Key metrics", styles["Heading2"]))
    metric_rows = [["Metric", "Value", "Status"]]
    for group, metrics in report["metrics"].items():
        for metric in metrics:
            value = metric.get("value")
            unit = metric.get("unit")
            if value is None:
                text = "--"
            elif unit == "percent":
                text = f"{value:.1%}"
            elif unit == "multiple":
                text = f"{value:.1f}x"
            elif unit == "idr":
                text = _idr(value)
            elif unit == "days":
                text = f"{value:,.0f} days"
            else:
                text = f"{value:,.2f}"
            metric_rows.append([metric["label"], text, metric.get("status", "")])
    story.append(_table(metric_rows, [85 * mm, 45 * mm, 35 * mm]))

    story.append(PageBreak())
    story.append(Paragraph("Valuation", styles["Heading2"]))
    dcf = report["valuation"].get("dcf")
    if dcf:
        assumptions = dcf["assumptions"]
        lines = [
            f"Enterprise value {_idr(dcf['enterprise_value'])}; equity value {_idr(dcf['equity_value'])}; "
            f"per share {_idr(dcf['per_share'], compact=False)}.",
            f"WACC {assumptions['wacc']:.2%}, terminal growth {assumptions['terminal_growth']:.2%}, "
            f"horizon {assumptions['years']} years, operating margin {assumptions['operating_margin']:.2%}, "
            f"tax {assumptions['tax_rate']:.2%}.",
            f"Terminal value is {dcf['terminal_share']:.0%} of enterprise value.",
        ]
        for line in lines:
            story.append(Paragraph(_safe(line), styles["Small"])) 
        for warning in dcf.get("warnings", []):
            story.append(Paragraph(_safe(f"Warning: {warning}"), styles["Tiny"]))
    else:
        story.append(Paragraph(_safe("DCF not applicable: " + "; ".join(report["valuation"]["notes"])), styles["Small"]))

    if report["valuation"]["relative"]:
        story.append(Spacer(1, 4))
        rows = [["Method", "Peer median", "Implied / share", "Note"]]
        for item in report["valuation"]["relative"]:
            rows.append(
                [
                    item["method"],
                    f"{item['median_multiple']:.1f}x" if item["median_multiple"] else "n/a",
                    _idr(item["implied_per_share"], compact=False) if item["implied_per_share"] else "n/a",
                    item["reason"] or "applied",
                ]
            )
        story.append(_table(rows, [40 * mm, 28 * mm, 35 * mm, 60 * mm]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Risks and flags", styles["Heading2"]))
    risk_rows = [["Severity", "Flag"]]
    for flag in rec["flags"]:
        risk_rows.append([flag["severity"].upper(), flag["message"]])
    if len(risk_rows) == 1:
        risk_rows.append(["info", "No flags triggered by the imported data."])
    story.append(_table(risk_rows, [25 * mm, 140 * mm]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Thesis invalidation", styles["Heading2"]))
    for condition in rec["invalidation_conditions"]:
        story.append(Paragraph(_safe(f"- {condition}"), styles["Small"]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Data provenance", styles["Heading2"]))
    provenance = [["Period", "Type", "Published", "Source"]]
    for period in report["statements"]:
        provenance.append(
            [
                period["period_end"],
                f"{period['period_type']} / {period['basis']}",
                period.get("publication_date") or "not recorded",
                (", ".join(period.get("source_urls") or []) or "no URL recorded")[:70],
            ]
        )
    story.append(_table(provenance, [25 * mm, 35 * mm, 30 * mm, 75 * mm]))

    story.append(Spacer(1, 8))
    story.append(
        Paragraph(
            _safe(
                "Method: configurable rule-based scoring; fair value blends DCF and sector multiples. "
                "All figures derive from imported statements and stored prices. Estimates are scenarios, not guarantees. "
                "Research support only; no orders are placed."
            ),
            styles["Tiny"],
        )
    )

    doc.build(story)
    return path


def export_all(report: Dict[str, Any], ticker: str, markdown: str, directory: Optional[Path] = None) -> Dict[str, Path]:
    paths: Dict[str, Path] = {
        "markdown": export_markdown(markdown, ticker, directory),
        "excel": export_excel(report, ticker, directory),
        "json": export_json(report, ticker, directory),
        "csv": export_metrics_csv(report, ticker, directory),
    }
    try:
        paths["pdf"] = export_pdf(report, ticker, directory)
    except Exception as exc:  # noqa: BLE001 - PDF must not block the other formats
        paths["pdf_error"] = str(exc)  # type: ignore[assignment]
    return paths
