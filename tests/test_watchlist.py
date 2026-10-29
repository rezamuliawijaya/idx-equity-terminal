"""Watchlist storage and alert evaluation."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pandas as pd
import pytest

from config.settings import TerminalSettings
from src.database.db import connect, init_db
from src.database.repositories import Repositories
from src.portfolio.watchlist import WatchlistService
from tests.helpers import manufacturing_history


@pytest.fixture()
def repos(tmp_path: Path):
    conn = connect(tmp_path / "terminal.db")
    init_db(conn)
    bundle = Repositories(conn)
    bundle.companies.upsert("TEST", name="Test Manufacturing Tbk", sector="Industrials")
    bundle.financials.save_periods(manufacturing_history())
    bundle.prices.upsert_many(
        pd.DataFrame(
            [
                {"ticker": "TEST", "date": _dt.date.today() - _dt.timedelta(days=2), "close": 990.0},
                {"ticker": "TEST", "date": _dt.date.today() - _dt.timedelta(days=1), "close": 1_000.0},
            ]
        )
    )
    conn.commit()
    yield bundle
    conn.close()


def test_add_update_and_remove(repos):
    service = WatchlistService(repos, TerminalSettings())
    service.add("test", notes="First note", target_margin_of_safety=0.20)
    assert repos.watchlist.contains("TEST")
    assert service.entries()[0]["notes"] == "First note"

    # An empty note must not wipe the existing one; a new target replaces the old.
    service.add("TEST", notes="", target_margin_of_safety=0.30)
    entry = service.entries()[0]
    assert entry["notes"] == "First note"
    assert entry["target_margin_of_safety"] == pytest.approx(0.30)

    service.remove("TEST")
    assert not repos.watchlist.contains("TEST")
    assert service.entries() == []


def test_entries_include_analysis_and_price(repos):
    service = WatchlistService(repos, TerminalSettings())
    service.add("TEST")
    entry = service.entries()[0]
    assert entry["ticker"] == "TEST"
    assert entry["price"] == pytest.approx(1_000.0)
    assert entry["price_date"] == _dt.date.today() - _dt.timedelta(days=1)
    assert entry["decision"]
    assert "alerts" in entry


def test_price_alerts_fire_on_the_configured_side(repos):
    service = WatchlistService(repos, TerminalSettings())
    service.add("TEST")
    service.set_alerts("TEST", above=900.0, below=1_100.0)
    kinds = {alert["kind"] for alert in service.alerts()}
    assert {"price_above", "price_below"}.issubset(kinds)

    service.set_alerts("TEST", above=2_000.0, below=500.0)
    kinds = {alert["kind"] for alert in service.alerts()}
    assert "price_above" not in kinds
    assert "price_below" not in kinds


def test_margin_of_safety_target_alert(repos):
    service = WatchlistService(repos, TerminalSettings())
    service.add("TEST")
    margin = service.entries()[0]["margin_of_safety"]
    if margin is None:
        pytest.skip("No fair value available for this fixture")

    service.add("TEST", target_margin_of_safety=margin - 0.01)
    kinds = {alert["kind"] for alert in service.alerts()}
    assert "margin_of_safety" in kinds

    service.add("TEST", target_margin_of_safety=margin + 0.01)
    kinds = {alert["kind"] for alert in service.alerts()}
    assert "margin_of_safety" not in kinds
