"""Data Management.

Everything that changes stored state lives here: CSV imports with a validation
preview, manual entry, company metadata, synthetic demo loading, saved settings,
database backups and the import log. Imports never overwrite unrelated data
silently; the preview shows exactly what will be written.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st

from src.ui import theme

from config.settings import (
    BACKUP_DIR,
    DCFDefaults,
    EXPORT_DIR,
    IMPORT_DIR,
    SAMPLE_DIR,
    ScoringWeights,
    TerminalSettings,
    TradingCosts,
    ensure_directories,
)
from src.data import live, live_sync
from src.data.csv_importer import (
    ImportResult,
    PriceImportResult,
    import_financials,
    import_prices,
    read_csv,
    write_all_templates,
)
from src.data.financial_parser import build_periods
from src.data.normalization import ITEM_STATEMENT, normalize_ticker
from src.data.providers import provider_status_table
from src.data.sample_data import DEMO_COMPANIES, seed_database
from src.data.validation import RawFinancialRow
from src.database.db import backup_database, database_info, restore_database
from src.ui.components import (
    current_settings,
    empty_state,
    freshness_table,
    get_repos,
    save_settings,
    score_all,
)

MANUAL_ITEMS: List[Tuple[str, str]] = [
    ("revenue", "Revenue"),
    ("cost_of_revenue", "Cost of revenue"),
    ("operating_expenses", "Operating expenses"),
    ("depreciation_amortization", "Depreciation & amortisation"),
    ("interest_expense", "Interest expense"),
    ("income_tax", "Income tax"),
    ("net_income", "Net income"),
    ("eps", "EPS (Rp per share)"),
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
    ("minority_interest", "Minority interest"),
    ("cfo", "Cash flow from operations"),
    ("capex", "Capital expenditure"),
    ("cfi", "Cash flow from investing"),
    ("cff", "Cash flow from financing"),
    ("dividends_paid", "Dividends paid"),
]

UNITS = ["rupiah", "thousand", "million", "billion"]


# --------------------------------------------------------------------------- #
# Cached parsing
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False, max_entries=8)
def _parse_statements(payload: bytes, name: str) -> ImportResult:
    return import_financials(io.BytesIO(payload), source_name=name)


@st.cache_data(show_spinner=False, max_entries=8)
def _parse_prices(payload: bytes, name: str) -> PriceImportResult:
    return import_prices(io.BytesIO(payload), source_name=name)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _issues_frame(rows: Sequence[Dict[str, Any]]) -> Optional[pd.DataFrame]:
    if not rows:
        return None
    return pd.DataFrame(rows)


def _invalidate_scores() -> None:
    score_all.clear()


def _refresh_after_write() -> None:
    _invalidate_scores()
    st.cache_data.clear()


# --------------------------------------------------------------------------- #
# Statements
# --------------------------------------------------------------------------- #


def _statements_tab(repos) -> None:
    st.markdown("#### Import financial statements")
    st.caption(
        "Long format is one row per line item; wide format is one row per period with a column per line item. "
        "Units are converted to absolute rupiah. Duplicate periods are replaced by the incoming file, so check "
        "the preview before committing."
    )

    uploads = st.file_uploader(
        "Statement CSV files",
        type=["csv"],
        accept_multiple_files=True,
        key="statement_uploader",
        help="Up to 32 MB per file. UTF-8, cp1252 and latin-1 are all accepted.",
    )
    if not uploads:
        st.info("Choose one or more CSV files to validate. Nothing is written until you press Import.")
        _template_downloads()
        return

    parsed: List[Tuple[str, ImportResult, str]] = []
    problems: List[Dict[str, Any]] = []
    for upload in uploads:
        payload = upload.getvalue()
        try:
            result = _parse_statements(payload, upload.name)
        except Exception as exc:  # noqa: BLE001 - surface the parser message
            problems.append({"file": upload.name, "message": f"{type(exc).__name__}: {exc}"})
            continue
        parsed.append((upload.name, result, _digest(payload)))

    if problems:
        for problem in problems:
            st.error(f"**{problem['file']}** could not be read: {problem['message']}")
    if not parsed:
        return

    total_rows = sum(len(result.rows) for _, result, _ in parsed)
    total_rows_checked = sum(result.report.rows_checked for _, result, _ in parsed)
    tickers = sorted({ticker for _, result, _ in parsed for ticker in result.tickers})
    errors = sum(len(result.report.errors) + sum(1 for i in result.issues if i.severity == "error") for _, result, _ in parsed)
    warnings = sum(len(result.report.warnings) + sum(1 for i in result.issues if i.severity == "warning") for _, result, _ in parsed)

    columns = st.columns(4)
    columns[0].metric("Files", len(parsed))
    columns[1].metric("Rows parsed", f"{total_rows:,}")
    columns[2].metric("Tickers", len(tickers))
    columns[3].metric("Errors / warnings", f"{errors} / {warnings}")

    summary_rows = []
    issue_rows: List[Dict[str, Any]] = []
    for name, result, _ in parsed:
        low, high = result.period_range
        summary_rows.append(
            {
                "File": name,
                "Layout": result.layout,
                "Rows": len(result.rows),
                "Tickers": ", ".join(result.tickers) or "(none)",
                "Period range": f"{low} to {high}" if low and high else "n/a",
                "Errors": len(result.report.errors) + sum(1 for i in result.issues if i.severity == "error"),
                "Warnings": len(result.report.warnings) + sum(1 for i in result.issues if i.severity == "warning"),
            }
        )
        for issue in result.report.by_severity():
            issue_rows.append(
                {
                    "File": name,
                    "Severity": issue.severity,
                    "Code": issue.code,
                    "Message": issue.message,
                }
            )
        for issue in result.issues:
            issue_rows.append(
                {
                    "File": name,
                    "Severity": issue.severity,
                    "Code": f"row {issue.row}" if issue.row else "file",
                    "Message": issue.message,
                }
            )

    st.dataframe(pd.DataFrame(summary_rows), hide_index=True, width="stretch")
    issue_frame = _issues_frame(issue_rows)
    if issue_frame is not None:
        with st.expander(f"Validation issues ({len(issue_rows)})", expanded=errors > 0):
            st.dataframe(issue_frame, hide_index=True, width="stretch")

    # Row sample so the mapping can be sanity-checked before writing.
    sample_rows = []
    for name, result, _ in parsed:
        for row in result.rows[:5]:
            sample_rows.append(
                {
                    "File": name,
                    "Ticker": row.ticker,
                    "Period": row.period_end.isoformat(),
                    "Type": row.period_type,
                    "Statement": row.statement,
                    "Line item": row.line_item,
                    "Value": row.value,
                    "Unit": row.unit,
                    "Consolidated": row.is_consolidated,
                }
            )
    if sample_rows:
        with st.expander("Parsed row sample"):
            st.dataframe(pd.DataFrame(sample_rows), hide_index=True, width="stretch")

    if tickers:
        existing = set(repos.companies.tickers())
        overlap = sorted(existing.intersection(tickers))
        if overlap:
            st.warning(
                "These tickers already exist and their matching periods will be replaced: "
                + ", ".join(overlap)
                + ". Other periods for the same ticker are kept."
            )

    blocked = errors > 0 or total_rows == 0
    if blocked:
        st.error(
            "Import is blocked because the validation found errors. Fix the rows listed above and upload again."
        )
        _template_downloads()
        return

    if st.button("Import validated rows", type="primary", key="commit_statements"):
        all_rows = [row for _, result, _ in parsed for row in result.rows]
        try:
            saved = repos.financials.save_periods(build_periods(all_rows))
        except Exception as exc:  # noqa: BLE001
            repos.conn.rollback()
            st.error(f"Import failed and was rolled back: {type(exc).__name__}: {exc}")
            return
        for name, result, digest in parsed:
            repos.imports.record(
                filename=name,
                tickers=result.tickers,
                rows=len(result.rows),
                errors=len(result.report.errors),
                warnings=len(result.report.warnings),
                ok=True,
                detail=f"layout={result.layout}; periods={saved}; sha256={digest[:16]}",
                sha256=digest,
            )
        repos.conn.commit()
        _refresh_after_write()
        st.success(
            f"Imported {len(all_rows):,} rows into {saved} periods for {', '.join(tickers)}. "
            "Scores will refresh on the next page load."
        )

    _template_downloads()


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def _prices_tab(repos) -> None:
    st.markdown("#### Import daily prices")
    st.caption(
        "Required columns: ticker, date, close. Optional: volume, value (turnover). Prices are stored as provided — "
        "adjust for splits and rights issues before importing, or keep a single adjusted series per ticker."
    )
    upload = st.file_uploader("Price CSV", type=["csv"], key="price_uploader")
    if upload is None:
        st.info("Choose a CSV file. Nothing is written until you press Import.")
        return

    payload = upload.getvalue()
    try:
        result = _parse_prices(payload, upload.name)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read {upload.name}: {type(exc).__name__}: {exc}")
        return

    frame = result.frame
    if frame.empty:
        st.error("No usable price rows were found.")
        if result.issues:
            st.dataframe(_issues_frame([issue.as_dict() for issue in result.issues]), hide_index=True, width="stretch")
        return

    columns = st.columns(4)
    columns[0].metric("Rows", f"{len(frame):,}")
    columns[1].metric("Tickers", frame["ticker"].nunique())
    columns[2].metric("First date", frame["date"].min().date().isoformat())
    columns[3].metric("Last date", frame["date"].max().date().isoformat())

    st.dataframe(frame.head(20), hide_index=True, width="stretch")
    if len(frame) > 20:
        st.caption(f"Previewing the first 20 of {len(frame):,} rows.")

    error_issues = [issue for issue in result.issues if issue.severity == "error"]
    if error_issues:
        with st.expander(f"Skipped rows ({len(error_issues)})", expanded=True):
            st.dataframe(
                _issues_frame([issue.as_dict() for issue in result.issues]),
                hide_index=True,
                width="stretch",
            )

    unknown = sorted(set(frame["ticker"]) - set(repos.companies.tickers()))
    if unknown:
        st.warning(
            "No company record exists yet for: "
            + ", ".join(unknown)
            + ". Prices will still be stored, but add the company metadata so it can be analysed."
        )

    if st.button("Import prices", type="primary", key="commit_prices"):
        written = repos.prices.upsert_many(frame)
        repos.imports.record(
            filename=upload.name,
            tickers=sorted(frame["ticker"].unique().tolist()),
            rows=written,
            errors=len(error_issues),
            warnings=0,
            ok=True,
            detail=f"price import; sha256={_digest(payload)[:16]}",
            sha256=_digest(payload),
        )
        repos.conn.commit()
        _refresh_after_write()
        st.success(f"Stored {written:,} price observations.")

    st.caption(
        "The latest close is used for every valuation. If the newest price is older than the configured maximum "
        "price age, the recommendation engine marks the data stale."
    )


def _benchmark_tab(repos) -> None:
    st.markdown("#### Import a benchmark series")
    st.caption("Columns: date, close and an optional label (default IHSG). Used only for backtest comparison.")
    label = st.text_input("Benchmark label", value="IHSG", key="benchmark_label")
    upload = st.file_uploader("Benchmark CSV", type=["csv"], key="benchmark_uploader")
    if upload is None:
        st.info("Choose a CSV file to preview.")
        return
    try:
        frame = read_csv(io.BytesIO(upload.getvalue()))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read the file: {type(exc).__name__}: {exc}")
        return

    date_column = next((c for c in frame.columns if c in {"date", "period_end", "tanggal"}), None)
    close_column = next((c for c in frame.columns if c in {"close", "close_price", "price", "harga", "level"}), None)
    if not date_column or not close_column:
        st.error("The file needs date and close columns.")
        st.dataframe(frame.head(), hide_index=True, width="stretch")
        return

    rows: List[Tuple[_dt.date, float]] = []
    problems: List[str] = []
    for index, record in frame.iterrows():
        try:
            parsed_date = pd.Timestamp(str(record[date_column]).strip()).date()
            close = float(str(record[close_column]).replace(",", ""))
        except (TypeError, ValueError):
            problems.append(f"row {index + 2}: unreadable date or close")
            continue
        if close <= 0:
            problems.append(f"row {index + 2}: non-positive close")
            continue
        rows.append((parsed_date, close))

    st.write(f"Parsed **{len(rows):,}** observations with label **{label or 'IHSG'}**.")
    if problems:
        with st.expander(f"Problems ({len(problems)})"):
            for problem in problems[:200]:
                st.write(f"- {problem}")

    if rows and st.button("Import benchmark", type="primary", key="commit_benchmark"):
        for date, close in rows:
            repos.benchmarks.upsert(label or "IHSG", date, close)
        repos.conn.commit()
        _refresh_after_write()
        st.success(f"Stored {len(rows):,} benchmark observations for {label or 'IHSG'}.")

    if repos.benchmarks.labels():
        st.caption("Stored benchmark series: " + ", ".join(repos.benchmarks.labels()))


# --------------------------------------------------------------------------- #
# Manual entry
# --------------------------------------------------------------------------- #


def _manual_tab(repos) -> None:
    st.markdown("#### Manual entry for one period")
    st.caption(
        "Use this for a single quarter or year that is easier to type than to restructure into a CSV. "
        "Values are entered in the unit you select and converted to absolute rupiah. Blank fields are not stored "
        "— they are never treated as zero."
    )

    with st.form("manual_entry"):
        columns = st.columns(4)
        ticker = columns[0].text_input("Ticker", value="", placeholder="e.g. BBCA")
        company_name = columns[1].text_input("Company name", value="")
        sector = columns[2].selectbox(
            "Sector",
            [
                "Financials",
                "Basic Materials",
                "Energy",
                "Industrials",
                "Consumer Non-Cyclicals",
                "Consumer Cyclicals",
                "Healthcare",
                "Infrastructure",
                "Properties & Real Estate",
                "Technology",
                "Transportation & Logistics",
                "Unclassified",
            ],
            index=8,
        )
        period_end = columns[3].date_input("Period end", value=_dt.date.today())

        columns = st.columns(4)
        period_type = columns[0].selectbox("Period type", ["annual", "quarter"])
        period_basis = columns[1].selectbox(
            "Period basis",
            ["cumulative", "discrete"],
            help="IDX quarterly reports are cumulative year-to-date. Choose discrete only if the figures are the "
            "quarter on its own.",
        )
        unit = columns[2].selectbox("Unit of the values below", UNITS, index=2)
        consolidated = columns[3].checkbox("Consolidated statements", value=True)

        columns = st.columns(4)
        source_url = columns[0].text_input("Source URL", value="")
        publication_date = columns[1].date_input("Publication date", value=None)
        retrieved_at = columns[2].date_input("Retrieved date", value=_dt.date.today())
        shares = columns[3].number_input(
            "Shares outstanding (absolute count)",
            min_value=0.0,
            value=0.0,
            step=1_000_000.0,
            format="%.0f",
            help="Enter the full share count, not millions. Stored with a unit multiplier of 1.",
        )

        st.markdown("##### Line items")
        values: Dict[str, Optional[float]] = {}
        grid = st.columns(3)
        for index, (key, label) in enumerate(MANUAL_ITEMS):
            with grid[index % 3]:
                values[key] = st.number_input(
                    label,
                    value=None,
                    step=None,
                    format="%.2f",
                    key=f"manual_{key}",
                    placeholder="not reported",
                )

        submitted = st.form_submit_button("Save period", type="primary")

    if not submitted:
        return

    try:
        clean_ticker = normalize_ticker(ticker)
    except ValueError as exc:
        st.error(str(exc))
        return
    if not ticker.strip():
        st.error("A ticker is required.")
        return

    provided = {key: value for key, value in values.items() if value is not None}
    if not provided and not shares:
        st.error("Enter at least one line item or a share count.")
        return

    rows: List[RawFinancialRow] = []
    for key, value in provided.items():
        rows.append(
            RawFinancialRow(
                ticker=clean_ticker,
                period_end=period_end,
                period_type=period_type,
                period_basis=period_basis,
                statement=ITEM_STATEMENT.get(key, "income"),
                line_item=key,
                value=value,
                unit=unit,
                currency="IDR",
                is_consolidated=consolidated,
                source_url=source_url or None,
                source_file="manual entry",
                publication_date=publication_date,
                retrieved_at=_dt.datetime.combine(retrieved_at, _dt.time()) if retrieved_at else None,
                company_name=company_name or None,
                sector=sector,
            )
        )
    if shares:
        rows.append(
            RawFinancialRow(
                ticker=clean_ticker,
                period_end=period_end,
                period_type=period_type,
                period_basis=period_basis,
                statement="balance",
                line_item="shares_outstanding",
                value=shares,
                unit="rupiah",
                currency="IDR",
                is_consolidated=consolidated,
                source_url=source_url or None,
                source_file="manual entry",
                publication_date=publication_date,
                retrieved_at=_dt.datetime.combine(retrieved_at, _dt.time()) if retrieved_at else None,
                company_name=company_name or None,
                sector=sector,
            )
        )

    try:
        periods = build_periods(rows)
        saved = repos.financials.save_periods(periods)
    except Exception as exc:  # noqa: BLE001
        repos.conn.rollback()
        st.error(f"Could not save the period: {type(exc).__name__}: {exc}")
        return

    if company_name or sector:
        repos.companies.upsert(clean_ticker, name=company_name or None, sector=sector or None)
    repos.imports.record(
        filename=f"manual:{clean_ticker}:{period_end.isoformat()}",
        tickers=[clean_ticker],
        rows=len(rows),
        errors=0,
        warnings=0,
        ok=True,
        detail=f"{period_type} {period_basis} manual entry; unit={unit}",
    )
    repos.conn.commit()
    _refresh_after_write()
    st.success(
        f"Saved {len(rows)} line items for {clean_ticker} ({period_type}, {period_end.isoformat()}) "
        f"across {saved} period record(s)."
    )
    st.info(
        "A single quarter on its own is not enough for TTM. Import the other quarters of the same fiscal year, "
        "or an annual period, before relying on the valuation."
    )


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def _template_downloads() -> None:
    st.markdown("##### Templates")
    st.caption("The templates contain example rows with placeholder figures. Replace them with filed data.")
    if st.button("Generate templates", key="generate_templates"):
        paths = write_all_templates(IMPORT_DIR)
        st.session_state["template_paths"] = {path.name: str(path) for path in paths}
    stored = st.session_state.get("template_paths") or {}
    if not stored:
        return
    columns = st.columns(len(stored))
    for column, (name, path) in zip(columns, stored.items()):
        with column:
            try:
                payload = Path(path).read_bytes()
            except OSError:
                st.caption(f"{name} is missing.")
                continue
            st.download_button(
                name,
                data=payload,
                file_name=name,
                mime="text/csv",
                key=f"template_download_{name}",
                width="stretch",
            )
    st.caption(f"Templates are also on disk in `{IMPORT_DIR}`.")


# --------------------------------------------------------------------------- #
# Companies and demo data
# --------------------------------------------------------------------------- #


def _companies_tab(repos) -> None:
    st.markdown("#### Stored companies")
    companies = repos.companies.list()
    if not companies:
        empty_state("No companies stored yet.")
    else:
        rows = []
        for company in companies:
            ticker = company["ticker"]
            periods = repos.financials.periods(ticker)
            coverage = repos.prices.coverage(ticker)
            rows.append(
                {
                    "Ticker": ticker,
                    "Name": company.get("name"),
                    "Sector": company.get("sector"),
                    "Synthetic": bool(company.get("is_synthetic")),
                    "Shares": company.get("shares_outstanding"),
                    "Periods": len(periods),
                    "Latest period": max((p.period_end for p in periods), default=None),
                    "Price observations": coverage["observations"],
                    "Last price date": coverage["end"],
                    "Source": company.get("source_url"),
                }
            )
        st.dataframe(
            pd.DataFrame(rows),
            hide_index=True,
            width="stretch",
            column_config={
                "Shares": st.column_config.NumberColumn(format="%.0f"),
                "Latest period": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "Last price date": st.column_config.DateColumn(format="YYYY-MM-DD"),
            },
        )

    if not companies:
        _demo_tab(repos)
        return

    st.markdown("##### Edit company metadata")
    ticker = st.selectbox("Ticker", [company["ticker"] for company in companies], key="edit_company_ticker")
    current = repos.companies.get(ticker) or {}
    with st.form(f"company_form_{ticker}"):
        columns = st.columns(2)
        name = columns[0].text_input("Name", value=current.get("name") or "")
        sector = columns[1].text_input("Sector", value=current.get("sector") or "")
        columns = st.columns(2)
        subsector = columns[0].text_input("Subsector", value=current.get("subsector") or "")
        listing = columns[1].date_input(
            "Listing date",
            value=_parse_date(current.get("listing_date")),
        )
        source_url = st.text_input("Source URL", value=current.get("source_url") or "")
        notes = st.text_area("Notes", value=current.get("notes") or "", height=100)
        shares = st.number_input(
            "Shares outstanding",
            min_value=0.0,
            value=float(current.get("shares_outstanding") or 0.0),
            step=1_000_000.0,
            format="%.0f",
        )
        saved = st.form_submit_button("Save metadata", type="primary")
    if saved:
        if current.get("is_synthetic"):
            st.warning(
                "This ticker is a synthetic demo fixture. Adding real data under the same ticker is not "
                "recommended; use a real ticker instead."
            )
        repos.companies.upsert(
            ticker,
            name=name or None,
            sector=sector or None,
            subsector=subsector or None,
            shares_outstanding=shares or None,
            source_url=source_url or None,
            notes=notes or None,
            listing_date=listing,
        )
        repos.conn.commit()
        _refresh_after_write()
        st.success(f"Metadata saved for {ticker}.")
        st.rerun()

    st.markdown("##### Delete a company")
    st.caption(
        "Deletes the company record, all of its stored statements and all of its prices. Research notes and saved "
        "valuations are retained as history, since they record what you concluded at a point in time."
    )
    confirm = st.checkbox(
        f"I understand this permanently deletes {ticker} statements and prices",
        key=f"delete_confirm_{ticker}",
    )
    if st.button("Delete permanently", disabled=not confirm, key=f"delete_{ticker}"):
        repos.financials.delete_ticker(ticker)
        repos.prices.delete_ticker(ticker)
        repos.watchlist.remove(ticker)
        repos.conn.commit()
        _refresh_after_write()
        st.success(f"Deleted {ticker}.")
        st.rerun()

    _demo_tab(repos)


def _parse_date(value: Any) -> Optional[_dt.date]:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _demo_tab(repos) -> None:
    st.markdown("#### Synthetic demonstration data")
    st.error(
        "**The demo companies do not exist.** Tickers are prefixed `DEMO-`, every figure is generated "
        "arithmetically, and prices are random walks. The data exists only to exercise the workflow end to end. "
        "It must never be treated as research on a real issuer."
    )
    st.caption(
        f"The generator writes its CSV files to `{SAMPLE_DIR}` and creates {len(DEMO_COMPANIES)} companies: "
        + ", ".join(company.ticker for company in DEMO_COMPANIES)
        + "."
    )
    if st.button("Load synthetic demo data", key="seed_demo"):
        with st.spinner("Generating and importing the synthetic dataset..."):
            try:
                result = seed_database(repos)
            except Exception as exc:  # noqa: BLE001
                repos.conn.rollback()
                st.error(f"Demo seeding failed: {type(exc).__name__}: {exc}")
                return
        repos.imports.record(
            filename="synthetic-demo",
            tickers=result["companies"],
            rows=result["rows_imported"],
            errors=0,
            warnings=0,
            ok=True,
            detail=f"synthetic demo seed; periods={result['periods_saved']}; prices={result['price_rows']}",
        )
        repos.conn.commit()
        _refresh_after_write()
        st.success(
            f"Seeded {result['periods_saved']} periods ({result['rows_imported']:,} rows) and "
            f"{result['price_rows']:,} price observations."
        )
        st.rerun()

    if st.button("Write demo CSV files without importing", key="write_demo_files"):
        from src.data.sample_data import write_sample_files

        paths = write_sample_files(SAMPLE_DIR)
        st.success("Written: " + ", ".join(path.name for path in paths.values()))


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _settings_tab(repos, settings: TerminalSettings) -> None:
    st.markdown("#### Scoring weights, decision policy and costs")
    st.caption(
        "These are research parameters, not validated optimums. They persist in the local database and drive the "
        "screener, the decision engine, the paper portfolio and every report."
    )

    with st.form("settings_form"):
        st.markdown("##### Factor weights")
        weights = settings.weights
        columns = st.columns(5)
        quality = columns[0].number_input("Quality", 0.0, 1.0, float(weights.quality), 0.01, format="%.2f")
        growth = columns[1].number_input("Growth", 0.0, 1.0, float(weights.growth), 0.01, format="%.2f")
        valuation = columns[2].number_input("Valuation", 0.0, 1.0, float(weights.valuation), 0.01, format="%.2f")
        health = columns[3].number_input("Health", 0.0, 1.0, float(weights.health), 0.01, format="%.2f")
        momentum = columns[4].number_input("Momentum", 0.0, 1.0, float(weights.momentum), 0.01, format="%.2f")

        columns = st.columns(2)
        dcf_weight = columns[0].number_input("DCF weight in blended fair value", 0.0, 1.0, float(settings.dcf_weight), 0.05, format="%.2f")
        relative_weight = columns[1].number_input("Relative value weight", 0.0, 1.0, float(settings.relative_weight), 0.05, format="%.2f")

        st.markdown("##### Decision policy")
        policy = settings.policy
        columns = st.columns(4)
        buy_score = columns[0].number_input("BUY minimum score", 0.0, 100.0, float(policy.buy_score), 1.0)
        buy_mos = columns[1].number_input("BUY minimum margin of safety", 0.0, 1.0, float(policy.buy_min_margin_of_safety), 0.01, format="%.2f")
        buy_conf = columns[2].number_input("BUY minimum confidence", 0.0, 1.0, float(policy.buy_min_confidence), 0.01, format="%.2f")
        hold_score = columns[3].number_input("HOLD minimum score", 0.0, 100.0, float(policy.hold_score), 1.0)
        columns = st.columns(4)
        min_conf = columns[0].number_input("Minimum confidence for any action", 0.0, 1.0, float(policy.min_confidence), 0.01, format="%.2f")
        min_coverage = columns[1].number_input("Minimum factor coverage", 0.0, 1.0, float(policy.min_factor_coverage), 0.01, format="%.2f")
        max_fund_age = columns[2].number_input("Maximum statement age (days)", 30, 2000, int(policy.max_fundamental_age_days), 10)
        max_price_age = columns[3].number_input("Maximum price age (days)", 1, 365, int(policy.max_price_age_days), 1)
        attractive_mos = st.number_input(
            "Attractive margin of safety for the expected-return label",
            0.0,
            1.0,
            float(policy.attractive_margin_of_safety),
            0.01,
            format="%.2f",
        )

        st.markdown("##### Trading costs (paper portfolio and backtest)")
        costs = settings.costs
        columns = st.columns(4)
        lot_size = columns[0].number_input("Lot size (shares)", 1, 10_000, int(costs.lot_size), 100)
        buy_fee = columns[1].number_input("Buy fee rate", 0.0, 0.05, float(costs.buy_fee_rate), 0.0005, format="%.4f")
        sell_fee = columns[2].number_input("Sell fee rate (incl. tax)", 0.0, 0.05, float(costs.sell_fee_rate), 0.0005, format="%.4f")
        min_fee = columns[3].number_input("Minimum fee (Rp)", 0.0, 100_000.0, float(costs.min_fee), 100.0, format="%.0f")
        columns = st.columns(3)
        slippage = columns[0].number_input("Slippage rate", 0.0, 0.05, float(costs.slippage_rate), 0.0005, format="%.4f")
        max_position = columns[1].number_input("Maximum single position", 0.0, 1.0, float(costs.max_position_pct), 0.01, format="%.2f")
        max_sector = columns[2].number_input("Maximum sector weight", 0.0, 1.0, float(costs.max_sector_pct), 0.01, format="%.2f")

        st.markdown("##### DCF defaults (used when a company has no better basis)")
        dcf = settings.dcf
        columns = st.columns(4)
        years = columns[0].number_input("Projection years", 1, 20, int(dcf.projection_years), 1)
        wacc = columns[1].number_input("WACC", 0.001, 0.5, float(dcf.wacc), 0.005, format="%.4f")
        terminal_growth = columns[2].number_input("Terminal growth", -0.05, 0.15, float(dcf.terminal_growth), 0.005, format="%.4f")
        tax_rate = columns[3].number_input("Tax rate", 0.0, 0.6, float(dcf.tax_rate), 0.01, format="%.4f")
        columns = st.columns(3)
        revenue_growth = columns[0].number_input("Default revenue growth", -0.5, 1.0, float(dcf.revenue_growth), 0.005, format="%.4f")
        nwc = columns[1].number_input("ΔNWC (% of revenue change)", -0.5, 1.0, float(dcf.nwc_pct_revenue_change), 0.01, format="%.3f")
        fade = columns[2].checkbox("Fade growth to terminal", value=bool(dcf.fade_growth_to_terminal))

        submitted = st.form_submit_button("Save settings", type="primary")

    if submitted:
        weight_sum = quality + growth + valuation + health + momentum
        if weight_sum <= 0:
            st.error("At least one factor weight must be positive.")
        elif terminal_growth >= wacc:
            st.error("Terminal growth must be below WACC; the terminal value is otherwise not meaningful.")
        else:
            new_settings = replace(
                settings,
                weights=ScoringWeights(quality=quality, growth=growth, valuation=valuation, health=health, momentum=momentum).normalized(),
                policy=replace(
                    settings.policy,
                    buy_score=buy_score,
                    buy_min_margin_of_safety=buy_mos,
                    buy_min_confidence=buy_conf,
                    hold_score=hold_score,
                    min_confidence=min_conf,
                    min_factor_coverage=min_coverage,
                    max_fundamental_age_days=int(max_fund_age),
                    max_price_age_days=int(max_price_age),
                    attractive_margin_of_safety=attractive_mos,
                ),
                costs=TradingCosts(
                    lot_size=int(lot_size),
                    buy_fee_rate=buy_fee,
                    sell_fee_rate=sell_fee,
                    min_fee=min_fee,
                    slippage_rate=slippage,
                    max_position_pct=max_position,
                    max_sector_pct=max_sector,
                ),
                dcf=DCFDefaults(
                    projection_years=int(years),
                    terminal_growth=terminal_growth,
                    wacc=wacc,
                    tax_rate=tax_rate,
                    revenue_growth=revenue_growth,
                    nwc_pct_revenue_change=nwc,
                    fade_growth_to_terminal=fade,
                ),
                dcf_weight=dcf_weight,
                relative_weight=relative_weight,
            )
            save_settings(repos, new_settings)
            _refresh_after_write()
            st.success("Settings saved. Scores and reports will use them from now on.")
            st.rerun()

    if st.button("Reset settings to defaults", key="reset_settings"):
        repos.settings.reset()
        repos.conn.commit()
        _refresh_after_write()
        st.session_state.pop("settings", None)
        st.success("Settings reset to the documented defaults.")
        st.rerun()

    st.markdown("##### Settings in effect")
    st.json(settings.as_dict(), expanded=False)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def _live_feed_tab(repos) -> None:
    """Symbol manager, provider status, EOD sync and quote provenance."""
    entries = repos.live.symbols(enabled_only=False)
    stored = repos.live.latest()
    enabled = [entry for entry in entries if entry.get("enabled")]

    st.markdown("#### Live IDX feed")
    st.caption(
        "Delayed quotes from Yahoo Finance's public chart/spark endpoints. This is an unofficial vendor feed: "
        "prices lag the exchange by roughly 10-20 minutes and are never treated as execution prices. Every stored "
        "quote keeps the vendor URL, the last-trade time and the retrieval time."
    )

    columns = st.columns(4)
    columns[0].metric("Symbols", f"{len(enabled)} / {len(entries)}", help="Enabled / total")
    columns[1].metric("Stored quotes", f"{len(stored)}")
    last_retrieved = repos.live.last_retrieved()
    columns[2].metric(
        "Last retrieval",
        last_retrieved.replace("T", " ")[11:19] if last_retrieved else "never",
    )
    columns[3].metric("Vendor", "Yahoo Finance", help=live.DELAY_NOTE)

    if not entries:
        st.info(
            "No live symbols configured. The default feed subscribes to the IDX Composite and eleven liquid "
            "blue chips. Nothing is fetched until you enable it."
        )
        if st.button("Enable default IDX feed", type="primary", key="live_enable_default"):
            inserted = repos.live.seed_defaults()
            repos.conn.commit()
            st.success(f"Added {inserted} symbols. Press **Refresh quotes** to store the first batch.")
            st.rerun()
        return

    actions = st.columns([1.1, 1.1, 2.2])
    if actions[0].button("Refresh quotes", type="primary", key="live_refresh_quotes"):
        with st.spinner("Requesting quotes…"):
            try:
                fresh = live.provider().quotes([entry["symbol"] for entry in enabled], use_cache=False)
            except live.LiveDataUnavailable as exc:
                st.error(f"Vendor unavailable: {exc}. Stored quotes were left untouched.")
            else:
                saved = repos.live.save_quotes(list(fresh.values()))
                repos.conn.commit()
                failed = [quote.ticker for quote in fresh.values() if not quote.ok]
                message = f"Stored {saved} of {len(fresh)} quotes."
                if failed:
                    message += f" No usable price for: {', '.join(failed)}."
                st.success(message)
                st.rerun()

    if actions[1].button("Sync EOD to prices", key="live_sync_eod"):
        with st.spinner("Fetching daily history…"):
            price_rows, benchmark_rows, errors = live_sync.sync_eod(repos, enabled, range_="1y")
        _refresh_after_write()
        message = (
            f"Wrote {price_rows} company closes to prices and {benchmark_rows} IHSG bars to the benchmark series."
        )
        if errors:
            message += " Failed: " + "; ".join(errors[:3])
        (st.warning if errors and not (price_rows or benchmark_rows) else st.success)(message)
        st.caption(
            "Only tickers that already exist as companies receive price rows; a live symbol cannot create a "
            "fundamental record."
        )

    with actions[2]:
        with st.form("live_add_symbol", clear_on_submit=True, border=False):
            left, right = st.columns([3, 1])
            new_ticker = left.text_input(
                "Add ticker",
                placeholder="e.g. INDF, IHSG",
                label_visibility="collapsed",
                key="live_new_ticker",
            )
            submitted = right.form_submit_button("Add")
        if submitted and new_ticker.strip():
            clean = new_ticker.strip().upper()
            if clean in {entry["ticker"] for entry in entries}:
                st.warning(f"{clean} is already in the feed.")
            else:
                try:
                    quote = live.provider().quote(clean)
                except live.LiveDataUnavailable as exc:
                    st.error(f"Cannot verify {clean} right now: {exc}")
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    if not quote.ok:
                        st.error(f"{clean}: {quote.error}")
                    else:
                        repos.live.upsert_symbol(clean, quote.symbol, quote.name)
                        repos.live.save_quotes([quote])
                        repos.conn.commit()
                        st.success(f"{clean} added as {quote.symbol}.")
                        st.rerun()

    table = pd.DataFrame(
        [
            {
                "TICKER": entry["ticker"],
                "VENDOR SYMBOL": entry["symbol"],
                "ENABLED": "YES" if entry.get("enabled") else "NO",
                "NAME": (entry.get("name") or "")[:38],
                "LAST SYNC": (entry.get("last_sync_at") or "never").replace("T", " ")[:19],
            }
            for entry in entries
        ]
    )
    st.dataframe(table, hide_index=True, width="stretch")

    manage = st.columns([2, 2, 1.2])
    target = manage[0].selectbox(
        "Toggle symbol",
        [entry["ticker"] for entry in entries],
        key="live_toggle_target",
    )
    current = next(entry for entry in entries if entry["ticker"] == target)
    if manage[1].button(
        "Disable" if current.get("enabled") else "Enable",
        key="live_toggle_button",
    ):
        repos.live.set_enabled(target, not current.get("enabled"))
        repos.conn.commit()
        st.rerun()
    with manage[2]:
        if st.button("Remove", key="live_remove_button"):
            repos.live.remove(target)
            repos.conn.commit()
            st.warning(f"{target} removed from the feed and its stored quote deleted.")
            st.rerun()

    with st.expander("Quote provenance"):
        if not stored:
            st.caption("No quotes stored yet.")
        else:
            provenance = pd.DataFrame(
                [
                    {
                        "TICKER": quote.get("ticker"),
                        "PRICE": quote.get("price"),
                        "QUOTE TIME": (quote.get("quote_time") or "").replace("T", " ")[:19],
                        "RETRIEVED": (quote.get("retrieved_at") or "").replace("T", " ")[:19],
                        "DELAYED": "YES" if quote.get("delayed") else "NO",
                        "SOURCE": quote.get("source_url"),
                    }
                    for quote in sorted(stored.values(), key=lambda row: row.get("ticker") or "")
                ]
            )
            st.dataframe(provenance, hide_index=True, width="stretch")
            st.caption(
                "Stored quotes are the last good values. A failed request never overwrites them, so an old "
                "timestamp means the vendor was unreachable — not that the price is current."
            )

    st.caption(
        "The feed is context for the tape. Fundamental scoring and valuation use the closes imported into "
        "**prices**; run *Sync EOD to prices* to bring the analysis price basis up to date."
    )


def _providers_tab() -> None:
    st.markdown("#### Data providers")
    st.caption(
        "CSV import is the primary, supported path. No provider in this build scrapes pages, bypasses a login, or "
        "fabricates missing values."
    )
    frame = pd.DataFrame(provider_status_table())
    if not frame.empty:
        display = frame.rename(
            columns={
                "key": "Key",
                "name": "Provider",
                "fundamentals": "Fundamentals",
                "prices": "Prices",
                "automated": "Automated",
                "notes": "Notes",
            }
        )
        st.dataframe(display, hide_index=True, width="stretch")

    st.info(
        "IDX does not publish a stable, documented, unauthenticated API for financial statements. Rather than "
        "scrape fragile pages or invent values, this terminal ships CSV import and manual entry, and records the "
        "source URL and publication date for every reported figure."
    )
    with st.expander("Where to get the source documents"):
        st.markdown(
            """
1. IDX issuer reports and financial statements: <https://www.idx.co.id/en/listed-companies/company-profiles>
2. IDX monthly statistics (sector aggregates and index levels): <https://www.idx.co.id/en/market-data/statistics>
3. The issuer's own investor-relations site — the primary document, and usually the fastest to update.

Record the exact URL and publication date on each imported row. The terminal shows both in every report.
"""
        )


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


def _database_tab(repos) -> None:
    info = database_info(repos.conn)
    columns = st.columns(4)
    columns[0].metric("Companies", info.get("companies") or 0)
    columns[1].metric("Periods", info.get("periods") or 0)
    columns[2].metric("Financial items", f"{info.get('financial_items') or 0:,}")
    columns[3].metric("Database size", f"{(info.get('size_bytes') or 0) / 1024 / 1024:.1f} MB")
    st.caption(f"Database: `{info['path']}` | schema version {info['schema_version']}")

    st.markdown("##### Backup")
    st.caption(
        "Uses SQLite's online backup API, so it is safe while the app is running. Backups are written to the "
        "local backups folder."
    )
    label = st.text_input("Backup label", value="manual", key="backup_label")
    if st.button("Create backup", key="create_backup"):
        try:
            path = backup_database(repos.conn, label=label or "manual")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Backup failed: {type(exc).__name__}: {exc}")
        else:
            st.success(f"Backup written to `{path}`")

    backups = sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    if backups:
        st.markdown("##### Restore")
        st.warning(
            "Restoring replaces the working database with the selected backup. A pre-restore safety copy is made "
            "first. The app must reload its connection afterwards."
        )
        choice = st.selectbox(
            "Backup file",
            [path.name for path in backups],
            key="restore_choice",
            format_func=lambda name: f"{name} ({ _size_label(next(p for p in backups if p.name == name)) })",
        )
        confirm = st.checkbox("I understand the working database will be replaced", key="restore_confirm")
        if st.button("Restore selected backup", disabled=not confirm, key="restore_button"):
            target = next(path for path in backups if path.name == choice)
            try:
                restore_database(target)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Restore failed: {type(exc).__name__}: {exc}")
            else:
                st.cache_resource.clear()
                st.cache_data.clear()
                st.session_state.pop("settings", None)
                st.success("Backup restored. Reloading connections...")
                st.rerun()
    else:
        st.caption("No backups exist yet.")

    st.markdown("##### Import and export log")
    log = repos.imports.recent(50)
    if log:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "When": item["imported_at"][:16].replace("T", " "),
                        "File": item["filename"],
                        "Tickers": item["tickers"],
                        "Rows": item["rows"],
                        "Errors": item["errors"],
                        "Warnings": item["warnings"],
                        "OK": bool(item["ok"]),
                        "Detail": item["detail"],
                    }
                    for item in log
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No imports have been logged yet.")

    st.markdown("##### Data freshness")
    frame = freshness_table(repos)
    if frame.empty:
        st.caption("No companies stored.")
    else:
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            column_config={
                "Latest period": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "Price date": st.column_config.DateColumn(format="YYYY-MM-DD"),
            },
        )
        policy = current_settings(repos).policy
        stale = frame[
            (frame["Period age (days)"].isna())
            | (frame["Period age (days)"] > policy.max_fundamental_age_days)
            | (frame["Price age (days)"].isna())
            | (frame["Price age (days)"] > policy.max_price_age_days)
        ]
        if not stale.empty:
            st.warning(
                "Stale or missing data for: "
                + ", ".join(stale["Ticker"].tolist())
                + ". Recommendations for these tickers are downgraded to INSUFFICIENT DATA when the policy "
                "limits are exceeded."
            )

    st.markdown("##### Exports")
    st.caption(f"Research report files are written to `{EXPORT_DIR}`.")


def _size_label(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "unknown size"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.0f} KB"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def render() -> None:
    ensure_directories()
    repos = get_repos()
    settings = current_settings(repos)
    theme.page_header("DATA", "Data Management")
    st.caption(
        "Fundamental data is imported from files or entered by hand. The Live feed tab subscribes to a delayed "
        "vendor quote feed for context only, and never fabricates a missing value."
    )

    tabs = st.tabs(
        [
            "Statements",
            "Prices",
            "Benchmark",
            "Live feed",
            "Manual entry",
            "Companies & demo",
            "Settings",
            "Providers",
            "Database",
        ]
    )
    with tabs[0]:
        _statements_tab(repos)
    with tabs[1]:
        _prices_tab(repos)
    with tabs[2]:
        _benchmark_tab(repos)
    with tabs[3]:
        _live_feed_tab(repos)
    with tabs[4]:
        _manual_tab(repos)
    with tabs[5]:
        _companies_tab(repos)
    with tabs[6]:
        _settings_tab(repos, settings)
    with tabs[7]:
        _providers_tab()
    with tabs[8]:
        _database_tab(repos)
