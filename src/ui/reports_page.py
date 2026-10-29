"""Research Reports page.

Builds the report bundle from the same analysis object the rest of the terminal
uses, previews the Markdown, and offers the generated files for download. A
single-ticker report and a batch export are supported; batch export writes to
``data/exports`` and lists what it produced.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from src.ui import theme

from config.settings import EXPORT_DIR
from src.reports.export import (
    export_all,
    export_excel,
    export_json,
    export_markdown,
    export_metrics_csv,
    export_pdf,
    safe_filename,
)
from src.reports.research_report import build_report, render_markdown
from src.ui.components import current_settings, empty_state, get_repos, ticker_selector
from src.utils.formatting import format_date, format_idr, format_percent

CACHE_KEY = "report_bundle"


def _bundle_key(ticker: str, generated_at: str) -> str:
    return f"{CACHE_KEY}:{ticker}:{generated_at}"


def _build(repos, settings, ticker: str, horizon_years: float, include_statements: bool) -> Optional[Dict[str, Any]]:
    report = build_report(
        repos,
        ticker,
        settings,
        horizon_years=horizon_years,
        include_statements=include_statements,
    )
    if report is None:
        return None
    markdown = render_markdown(report)
    payloads: Dict[str, bytes] = {
        "markdown": markdown.encode("utf-8"),
        "json": json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"analysis"}
            },
            indent=2,
            default=str,
        ).encode("utf-8"),
        "csv": pd.DataFrame(
            [metric for group in report["metrics"].values() for metric in group]
        ).to_csv(index=False).encode("utf-8"),
    }
    paths: Dict[str, str] = {}
    try:
        paths["markdown"] = str(export_markdown(markdown, ticker))
        paths["excel"] = str(export_excel(report, ticker))
        paths["json"] = str(export_json(report, ticker))
        paths["csv"] = str(export_metrics_csv(report, ticker))
    except Exception as exc:  # noqa: BLE001 - keep the preview usable
        paths["error"] = f"{type(exc).__name__}: {exc}"
    try:
        pdf_path = export_pdf(report, ticker)
        paths["pdf"] = str(pdf_path)
        payloads["pdf"] = pdf_path.read_bytes()
    except Exception as exc:  # noqa: BLE001 - PDF is optional
        paths["pdf_error"] = f"{type(exc).__name__}: {exc}"

    excel_path = paths.get("excel")
    if excel_path:
        try:
            payloads["excel"] = Path(excel_path).read_bytes()
        except OSError:
            pass

    return {
        "report": report,
        "markdown": markdown,
        "payloads": payloads,
        "paths": paths,
        "generated_at": report["generated_at"],
    }


def _store(ticker: str, generated_at: str, bundle: Dict[str, Any]) -> None:
    st.session_state[_bundle_key(ticker, generated_at)] = bundle
    st.session_state[f"{CACHE_KEY}:current:{ticker}"] = generated_at


def _load(ticker: str) -> Optional[Dict[str, Any]]:
    generated_at = st.session_state.get(f"{CACHE_KEY}:current:{ticker}")
    if not generated_at:
        return None
    return st.session_state.get(_bundle_key(ticker, generated_at))


def _download_buttons(bundle: Dict[str, Any], ticker: str) -> None:
    payloads = bundle["payloads"]
    columns = st.columns(4)
    specifications = [
        ("markdown", "Markdown (.md)", "text/markdown"),
        ("pdf", "PDF", "application/pdf"),
        ("excel", "Excel (.xlsx)", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("json", "JSON", "application/json"),
    ]
    for column, (kind, label, mime) in zip(columns, specifications):
        with column:
            data = payloads.get(kind)
            if data is None:
                st.button(label, disabled=True, key=f"disabled_{kind}_{ticker}")
                st.caption("Not available for this run.")
                continue
            extension = {"markdown": "md", "pdf": "pdf", "excel": "xlsx", "json": "json"}[kind]
            st.download_button(
                label,
                data=data,
                file_name=f"{safe_filename(ticker)}-research.{extension}",
                mime=mime,
                key=f"download_{kind}_{ticker}",
                width="stretch",
            )
    csv_data = payloads.get("csv")
    if csv_data is not None:
        st.download_button(
            "Metrics (.csv)",
            data=csv_data,
            file_name=f"{safe_filename(ticker)}-metrics.csv",
            mime="text/csv",
            key=f"download_csv_{ticker}",
        )


def _preview(bundle: Dict[str, Any]) -> None:
    report = bundle["report"]
    rec = report["recommendation"]
    columns = st.columns(5)
    columns[0].metric("Decision", rec["decision"])
    columns[1].metric("Score", f"{rec['score']:.1f}" if rec["score"] is not None else "n/a")
    columns[2].metric("Fair value", format_idr(rec["fair_value"]) if rec["fair_value"] else "n/a")
    columns[3].metric("Margin of safety", format_percent(rec["margin_of_safety"]))
    columns[4].metric("Confidence", f"{rec['confidence']:.0%}")

    generated = report["generated_at"]
    st.caption(f"Report generated {generated[:16].replace('T', ' ')} | basis {rec['period_label']}")
    if report.get("synthetic"):
        st.warning(
            "This report covers a synthetic demonstration company. It is a worked example of the "
            "workflow, not research on a real issuer."
        )

    with st.expander("Rendered report", expanded=True):
        st.markdown(bundle["markdown"])

    with st.expander("Files written to disk"):
        for label, path in bundle["paths"].items():
            if label.endswith("_error"):
                st.warning(f"{label}: {path}")
            else:
                st.write(f"- **{label}**: `{path}`")


def _single_report(repos, settings) -> None:
    st.markdown("#### Single-company report")
    ticker = ticker_selector(repos, key="report_ticker")
    if ticker is None:
        return

    columns = st.columns([1, 1, 2])
    horizon = columns[0].number_input(
        "Expected-return horizon (years)",
        min_value=1.0,
        max_value=10.0,
        value=3.0,
        step=0.5,
        help="Used for the annualised expected-return scenario in the report.",
    )
    include_statements = columns[1].checkbox(
        "Include statement detail",
        value=True,
        help="Adds up to eight recent periods of line items to the provenance section and Excel workbook.",
    )
    columns[2].caption(
        "The report is generated from the stored data and the current saved settings. Re-generate after "
        "changing assumptions or importing new periods."
    )

    if st.button("Build report", type="primary", key="build_single_report"):
        with st.spinner("Building report bundle (PDF, Excel, Markdown, JSON, CSV)..."):
            bundle = _build(repos, settings, ticker, float(horizon), include_statements)
        if bundle is None:
            st.error(f"No analysis is available for {ticker}. It may have no stored statements.")
            return
        _store(ticker, bundle["generated_at"], bundle)
        repos.imports.record(
            filename=f"report:{ticker}",
            tickers=[ticker],
            rows=1,
            errors=0,
            warnings=0,
            ok=True,
            detail="Research report generated",
        )
        repos.conn.commit()
        st.success("Report bundle generated and written to the exports folder.")

    bundle = _load(ticker)
    if bundle is None:
        st.info("Press **Build report** to generate the bundle for this company.")
        return

    _preview(bundle)
    st.markdown("#### Download")
    _download_buttons(bundle, ticker)


def _batch_report(repos, settings) -> None:
    st.markdown("#### Batch export")
    st.caption(
        "Selects from the companies with stored statements and writes one bundle per ticker. Files are written "
        "to the exports folder; nothing is emailed or uploaded."
    )
    available = repos.financials.all_tickers_with_data()
    if not available:
        empty_state("No companies have stored statements.")
        return
    selected = st.multiselect("Companies", available, default=available, key="report_batch_tickers")
    include_statements = st.checkbox("Include statement detail", value=False, key="report_batch_statements")
    if st.button("Export selected", key="build_batch_report"):
        if not selected:
            st.warning("Select at least one company.")
            return
        produced: List[Dict[str, Any]] = []
        progress = st.progress(0.0)
        for index, ticker in enumerate(selected, start=1):
            try:
                bundle = _build(repos, settings, ticker, 3.0, include_statements)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
                produced.append({"Ticker": ticker, "Status": f"failed: {type(exc).__name__}: {exc}", "Files": ""})
                progress.progress(index / len(selected))
                continue
            if bundle is None:
                produced.append({"Ticker": ticker, "Status": "no analysis", "Files": ""})
            else:
                files = ", ".join(label for label in bundle["paths"] if not label.endswith("_error"))
                produced.append({"Ticker": ticker, "Status": "ok", "Files": files})
            progress.progress(index / len(selected))
        progress.empty()
        st.dataframe(pd.DataFrame(produced), hide_index=True, width="stretch")
        st.success(f"Batch export finished for {len(selected)} ticker(s). Files are in `{EXPORT_DIR}`.")
        repos.imports.record(
            filename="report:batch",
            tickers=list(selected),
            rows=len(produced),
            errors=sum(1 for row in produced if row["Status"] != "ok"),
            warnings=0,
            ok=all(row["Status"] == "ok" for row in produced),
            detail="Batch research report export",
        )
        repos.conn.commit()


def _archive() -> None:
    st.markdown("#### Export folder")
    st.caption(f"`{EXPORT_DIR}`")
    if not EXPORT_DIR.exists():
        empty_state("No export folder exists yet.")
        return
    files = sorted(EXPORT_DIR.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        empty_state("No files have been exported yet.")
        return
    rows = [
        {
            "File": path.name,
            "Format": path.suffix.lstrip(".").upper() or "?",
            "Size (KB)": round(path.stat().st_size / 1024, 1),
            "Modified": _dt.datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0).isoformat(sep=" "),
        }
        for path in files[:200]
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Showing the 200 most recent files. Deleting files is done in the operating system, not here.")


def render() -> None:
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("RPT", "Research Reports")
    st.caption(
        "Reports are assembled from the stored statements, prices and computed metrics. They document what the "
        "model currently says and why; they are not investment advice and place no orders."
    )

    if not repos.companies.list():
        empty_state("No companies stored. Import data or load the synthetic demo set from **Data Management**.")
        return

    tabs = st.tabs(["Single company", "Batch export", "Export folder"])
    with tabs[0]:
        _single_report(repos, settings)
    with tabs[1]:
        _batch_report(repos, settings)
    with tabs[2]:
        _archive()
