"""End-to-end pipeline: synthetic import -> analysis -> report -> export.

These tests use a temporary SQLite database, so they never touch the working
``data/terminal.db``. The dataset is the synthetic demo fixture: four issuers
that do not exist, generated arithmetically.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from config.settings import TerminalSettings
from src.analytics.engine import analyze_ticker, quick_scores
from src.data.sample_data import DEMO_COMPANIES, seed_database
from src.database.db import connect, init_db
from src.database.repositories import Repositories
from src.reports.export import (
    REPORTLAB_AVAILABLE,
    export_excel,
    export_json,
    export_markdown,
    export_metrics_csv,
    export_pdf,
)
from src.reports.research_report import build_report, render_markdown

DECISIONS = {"BUY", "HOLD", "SELL / REDUCE", "AVOID", "INSUFFICIENT DATA"}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    directory = tmp_path_factory.mktemp("terminal")
    conn = connect(directory / "terminal.db")
    init_db(conn)
    repos = Repositories(conn)
    repos.presets.seed_defaults()
    result = seed_database(repos, directory=directory / "sample")
    conn.commit()
    yield repos, result, directory
    conn.close()


def test_seed_imports_every_demo_company(seeded):
    repos, result, _ = seeded
    tickers = {company["ticker"] for company in repos.companies.list()}
    assert tickers == {company.ticker for company in DEMO_COMPANIES}
    assert result["periods_saved"] > 0
    assert result["price_rows"] > 0
    assert not result["validation"].get("error")


def test_demo_rows_are_labelled_synthetic(seeded):
    repos, _, _ = seeded
    for company in repos.companies.list():
        assert company["is_synthetic"] == 1
        assert "synthetic" in (company["name"] or "").lower()


def test_every_company_produces_a_decision_with_factor_detail(seeded):
    repos, _, _ = seeded
    settings = TerminalSettings()
    for company in DEMO_COMPANIES:
        analysis = analyze_ticker(repos, company.ticker, settings)
        assert analysis is not None, company.ticker

        rec = analysis.recommendation
        assert rec.decision in DECISIONS
        assert set(rec.factors) == {"quality", "growth", "valuation", "health", "momentum"}
        assert 0.0 <= rec.confidence <= 1.0
        assert 0.0 <= rec.data_completeness <= 1.0
        assert rec.reasons
        assert rec.invalidation_conditions

        # Every essential input came from a period that records provenance.
        assert analysis.snapshot.data_sources
        assert analysis.snapshot.price is not None
        assert analysis.snapshot.price_date is not None


def test_bank_valuation_does_not_use_dcf_or_ev_ebitda(seeded):
    repos, _, _ = seeded
    analysis = analyze_ticker(repos, "DEMO-BNK", TerminalSettings())
    assert analysis is not None
    assert analysis.snapshot.profile == "bank"
    assert analysis.dcf is None
    assert analysis.snapshot.metric_value("ev_ebitda") is None
    assert analysis.snapshot.metric_value("pb") is not None
    methods = {item.method for item in analysis.relative}
    assert any("p/b" in method.lower() or "justified" in method.lower() for method in methods)
    assert not any("dcf" in method.lower() for method in methods)


def test_screener_rows_cover_all_tickers(seeded):
    repos, _, _ = seeded
    rows = quick_scores(repos, TerminalSettings())
    assert len(rows) == len(DEMO_COMPANIES)
    for row in rows:
        assert row["ticker"]
        assert row["decision"] in DECISIONS
        assert "error" not in row
        assert row["analysis"] is not None


def test_report_bundle_and_markdown(seeded):
    repos, _, _ = seeded
    report = build_report(repos, "DEMO-MFG", TerminalSettings())
    assert report is not None
    assert report["ticker"] == "DEMO-MFG"
    assert report["synthetic"] is True
    assert report["recommendation"]["decision"] in DECISIONS
    assert report["metrics"]["profitability"]

    markdown = render_markdown(report)
    assert "# DEMO-MFG" in markdown
    assert "SYNTHETIC DEMONSTRATION DATA" in markdown
    assert "Recommendation" in markdown
    # The report must not present a synthetic company as a real one.
    assert "does not exist" in markdown


def test_report_for_unknown_ticker_is_none(seeded):
    repos, _, _ = seeded
    assert build_report(repos, "NO-SUCH-TICKER", TerminalSettings()) is None


def test_exports_write_real_files(seeded, tmp_path: Path):
    repos, _, _ = seeded
    report = build_report(repos, "DEMO-CNS", TerminalSettings(), include_statements=True)
    assert report is not None
    markdown = render_markdown(report)

    directory = tmp_path / "exports"
    written = {
        "markdown": export_markdown(markdown, "DEMO-CNS", directory),
        "json": export_json(report, "DEMO-CNS", directory),
        "csv": export_metrics_csv(report, "DEMO-CNS", directory),
        "excel": export_excel(report, "DEMO-CNS", directory),
    }
    if REPORTLAB_AVAILABLE:
        written["pdf"] = export_pdf(report, "DEMO-CNS", directory)

    for kind, path in written.items():
        assert path.exists(), kind
        assert path.stat().st_size > 0, kind


def test_settings_round_trip_through_the_database(seeded):
    repos, _, _ = seeded
    defaults = TerminalSettings()
    repos.settings.save(defaults)
    repos.conn.commit()
    loaded = repos.settings.load()
    assert loaded.weights.as_dict() == defaults.weights.as_dict()
    assert loaded.policy.buy_score == defaults.policy.buy_score
    assert loaded.dcf.wacc == defaults.dcf.wacc

    # Writing a non-default value actually changes the analysis inputs.
    from dataclasses import replace

    from config.settings import ScoringWeights

    changed = replace(defaults, weights=ScoringWeights(quality=0.5, growth=0.1, valuation=0.2, health=0.1, momentum=0.1))
    repos.settings.save(changed)
    repos.conn.commit()
    reloaded = repos.settings.load()
    assert reloaded.weights.as_dict()["quality"] == pytest.approx(0.5)

    repos.settings.reset()
    repos.conn.commit()


def test_price_history_is_dated_and_positive(seeded):
    repos, _, _ = seeded
    frame = repos.prices.series("DEMO-MIN")
    assert not frame.empty
    assert frame["close"].min() > 0
    assert frame["date"].max().date() <= _dt.date.today()
    assert frame["date"].is_monotonic_increasing
