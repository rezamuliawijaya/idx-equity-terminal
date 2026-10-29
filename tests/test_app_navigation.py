"""Navigation smoke tests through Streamlit's own AppTest harness.

The sidebar radio owns the ``page`` session key, and Streamlit refuses writes to
a widget-backed key once that widget exists. Buttons that jump between pages are
rendered after the radio, so a direct write only fails at runtime. These tests
run the real ``app.py`` against a temporary database to catch that.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st
from streamlit.testing.v1 import AppTest

from src.data.sample_data import seed_database
from src.database import db
from src.database.repositories import Repositories


@pytest.fixture()
def app(tmp_path, monkeypatch):
    db_path = tmp_path / "terminal.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "BACKUP_DIR", tmp_path / "backups")
    st.cache_resource.clear()
    st.cache_data.clear()
    yield AppTest.from_file(str(ROOT / "app.py"), default_timeout=120), tmp_path
    st.cache_resource.clear()
    st.cache_data.clear()


def _seed(path: pathlib.Path, ticker: str = "DEMO-MFG") -> None:
    conn = db.connect(path)
    db.init_db(conn)
    repos = Repositories(conn)
    repos.presets.seed_defaults()
    seed_database(repos, directory=path.parent / "sample")
    repos.watchlist.add(ticker)
    conn.commit()
    conn.close()


def _labels(elements):
    return [element.label for element in elements]


def _render_errors(harness):
    """Errors raised by the page guard, as opposed to informational banners."""
    return [element.value for element in harness.error if "failed to render" in (element.value or "")]


def test_empty_database_boots_and_offers_the_import_page(app):
    harness, _ = app
    harness.run()

    assert not harness.exception
    assert harness.session_state["page"] == "Market Overview"
    assert "Open Data Management" in _labels(harness.button)

    next(button for button in harness.button if button.label == "Open Data Management").click().run()

    assert not harness.exception
    assert harness.session_state["page"] == "Data Management"


def test_watchlist_jump_opens_company_research(app):
    harness, tmp_path = app
    _seed(tmp_path / "terminal.db")

    harness.run()
    assert not harness.exception

    harness.radio[0].set_value("Watchlist").run()
    assert harness.session_state["page"] == "Watchlist"

    jump = [button for button in harness.button if button.label == "Open research for DEMO-MFG"]
    assert jump, _labels(harness.button)

    jump[0].click().run()

    assert not harness.exception
    assert not _render_errors(harness)
    assert harness.session_state["page"] == "Company Research"
    assert harness.session_state["selected_ticker"] == "DEMO-MFG"


def test_screener_jump_opens_the_selected_company(app):
    harness, tmp_path = app
    _seed(tmp_path / "terminal.db")

    harness.run()
    harness.radio[0].set_value("Stock Screener").run()
    assert harness.session_state["page"] == "Stock Screener"

    picker = [box for box in harness.selectbox if box.label == "Inspect a company"]
    assert picker, _labels(harness.selectbox)
    picker[0].set_value("DEMO-MFG").run()

    jump = [button for button in harness.button if button.label == "Open company research"]
    assert jump

    jump[0].click().run()

    assert not harness.exception
    assert not _render_errors(harness)
    assert harness.session_state["page"] == "Company Research"
    assert harness.session_state["selected_ticker"] == "DEMO-MFG"


def test_live_market_without_a_feed_offers_to_enable_one(app):
    harness, tmp_path = app
    _seed(tmp_path / "terminal.db")

    harness.run()
    harness.radio[0].set_value("Live Market").run()

    assert not harness.exception
    assert not _render_errors(harness)
    assert "Enable default IDX feed" in _labels(harness.button)
    assert "FEED NOT CONFIGURED" in " ".join(element.value or "" for element in harness.markdown)


def _add_live_company(path: pathlib.Path) -> None:
    """Clone the demo statements onto BBCA and store one delayed quote for it."""
    import datetime as _dt

    from src.data import live

    conn = db.connect(path)
    repos = Repositories(conn)
    repos.companies.upsert(
        "BBCA",
        name="Bank Central Asia",
        sector="Financials",
        shares_outstanding=123_000_000_000,
    )
    for row in conn.execute("SELECT * FROM periods WHERE ticker = 'DEMO-MFG'").fetchall():
        cursor = conn.execute(
            "INSERT INTO periods (ticker, period_end, period_type, period_basis, is_consolidated, "
            "fiscal_year, publication_date, retrieved_at, source_files, source_urls, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "BBCA",
                row["period_end"],
                row["period_type"],
                row["period_basis"],
                row["is_consolidated"],
                row["fiscal_year"],
                row["publication_date"],
                row["retrieved_at"],
                row["source_files"],
                row["source_urls"],
                row["notes"],
                row["created_at"],
            ),
        )
        for item in conn.execute(
            "SELECT * FROM financial_items WHERE period_id = ?", (row["id"],)
        ).fetchall():
            conn.execute(
                "INSERT INTO financial_items (period_id, line_item, value, value_idr, unit, currency, "
                "is_derived, source_url, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cursor.lastrowid,
                    item["line_item"],
                    item["value"],
                    item["value_idr"],
                    item["unit"],
                    item["currency"],
                    item["is_derived"],
                    item["source_url"],
                    item["notes"],
                ),
            )
    conn.execute(
        "INSERT INTO prices (ticker, date, close, volume, value, source_file) "
        "VALUES ('BBCA', '2026-10-28', 4900.0, 1000, NULL, 'test')"
    )
    repos.live.upsert_symbol("BBCA", "BBCA.JK", "Bank Central Asia")
    stamp = _dt.datetime(2026, 10, 29, 15, 16, tzinfo=live.WIB)
    repos.live.save_quotes(
        [
            live.LiveQuote(
                symbol="BBCA.JK",
                ticker="BBCA",
                name="Bank Central Asia",
                price=4960.0,
                previous_close=5275.0,
                change=-315.0,
                change_percent=-315.0 / 5275.0,
                day_low=4960.0,
                day_high=5325.0,
                quote_time=stamp,
                retrieved_at=stamp,
                market_state="OPEN",
                source_url="https://query1.finance.yahoo.com/v8/finance/chart/BBCA.JK",
            )
        ]
    )
    conn.commit()
    conn.close()


def test_company_research_renders_the_delayed_quote_strip(app, monkeypatch):
    harness, tmp_path = app
    path = tmp_path / "terminal.db"
    _seed(path)
    _add_live_company(path)

    # The tape would otherwise call the vendor for the symbol this test just
    # added. Fail the transport so the test never touches the network and the
    # stored quote is what the strip renders.
    from src.data import live

    def offline(*args, **kwargs):
        raise live.LiveDataUnavailable("offline in tests")

    live.clear_cache()
    monkeypatch.setattr(live, "_http_json", offline)

    harness.run()
    harness.radio[0].set_value("Company Research").run()

    picker = [box for box in harness.selectbox if box.label == "Company"]
    assert picker, _labels(harness.selectbox)
    picker[0].set_value("BBCA").run()

    assert not harness.exception
    assert not _render_errors(harness)
    html = "\n".join(str(element.value or "") for element in harness.markdown)
    captions = "\n".join(str(element.value or "") for element in harness.caption)
    assert "DELAYED QUOTE" in html
    assert "NOT AN EXECUTION PRICE" in html
    assert "4.960" in html
    assert "analysis price basis" in captions
    assert "delayed" in captions.lower()
