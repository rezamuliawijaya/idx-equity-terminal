"""Paper portfolio: lot sizing, fees, cash, realised P&L and limits."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from config.settings import TradingCosts
from src.database.db import connect, init_db
from src.database.repositories import Repositories
from src.portfolio.paper_portfolio import PaperPortfolio, check_sector_limit


@pytest.fixture()
def repos():
    conn = connect(Path(":memory:"))
    init_db(conn)
    bundle = Repositories(conn)
    yield bundle
    conn.close()


def portfolio(repos, **costs):
    return PaperPortfolio(repos.portfolio, TradingCosts(**costs))


def test_buy_charges_fees_and_reduces_cash(repos):
    book = portfolio(repos)
    outcome = book.buy("TEST", 1_000.0, 1, trade_date=_dt.date(2024, 1, 2))
    assert outcome.ok
    # 100 shares at 1,000 = 100,000 gross + 150 fee.
    assert book.cash() == pytest.approx(100_000_000.0 - 100_150.0)
    positions = book.positions()
    assert len(positions) == 1
    assert positions[0].shares == 100
    assert positions[0].average_cost == pytest.approx(1_000.0)


def test_sell_realises_pnl_after_sell_fees(repos):
    book = portfolio(repos)
    book.buy("TEST", 1_000.0, 1, trade_date=_dt.date(2024, 1, 2))
    outcome = book.sell("TEST", 1_100.0, 1, trade_date=_dt.date(2024, 1, 3))
    assert outcome.ok
    # Proceeds 110,000 - 275 fee; realised (1,100 - 1,000) * 100 - 275.
    assert outcome.transaction["fees"] == pytest.approx(275.0)
    assert outcome.transaction["realized_pnl"] == pytest.approx(9_725.0)
    assert book.cash() == pytest.approx(100_000_000.0 - 100_150.0 + 109_725.0)
    assert book.positions() == []


def test_insufficient_cash_is_rejected(repos):
    book = portfolio(repos)
    outcome = book.buy("TEST", 2_000_000.0, 100)
    assert not outcome.ok
    assert "Insufficient cash" in outcome.message
    assert book.cash() == pytest.approx(100_000_000.0)


def test_selling_more_than_held_is_rejected(repos):
    book = portfolio(repos)
    book.buy("TEST", 1_000.0, 1)
    outcome = book.sell("TEST", 1_000.0, 2)
    assert not outcome.ok
    assert "position holds" in outcome.message


def test_zero_and_negative_price_rejected(repos):
    book = portfolio(repos)
    assert not book.buy("TEST", 0.0, 1).ok
    assert not book.buy("TEST", -5.0, 1).ok
    assert not book.buy("TEST", 1_000.0, 0).ok


def test_position_limit_blocks_oversized_order(repos):
    costs = dict(max_position_pct=0.001)
    book = portfolio(repos, **costs)
    prices = {"TEST": 1_000.0}
    outcome = book.buy("TEST", 1_000.0, 200, price_map=prices)
    assert not outcome.ok
    assert "Position limit" in outcome.message


def test_costs_are_configurable(repos):
    book = portfolio(repos, buy_fee_rate=0.0, sell_fee_rate=0.0, min_fee=0.0)
    book.buy("TEST", 1_000.0, 1)
    assert book.cash() == pytest.approx(100_000_000.0 - 100_000.0)


def test_minimum_fee_applies(repos):
    book = portfolio(repos, buy_fee_rate=0.0, min_fee=5_000.0)
    outcome = book.buy("TEST", 1_000.0, 1)
    assert outcome.ok
    assert outcome.transaction["fees"] == pytest.approx(5_000.0)


def test_summary_and_allocation(repos):
    book = portfolio(repos)
    book.buy("TEST", 1_000.0, 10, price_map={"TEST": 1_000.0})
    summary = book.summary({"TEST": 1_100.0}, {"TEST": "Industrials"})
    assert summary["market_value"] == pytest.approx(1_100_000.0)
    assert summary["total_value"] == pytest.approx(summary["cash"] + 1_100_000.0)
    assert summary["unrealized_pnl"] == pytest.approx(100_000.0)
    assert summary["allocation"][0]["ticker"] == "TEST"


def test_sector_limit_warning():
    costs = TradingCosts(max_sector_pct=0.45)
    allocation = [
        {"ticker": "A", "sector": "Mining", "value": 60.0},
        {"ticker": "B", "sector": "Consumer", "value": 40.0},
    ]
    warnings = check_sector_limit(allocation, costs)
    assert len(warnings) == 1
    assert "Mining" in warnings[0]


def test_reset_clears_trades_and_restores_cash(repos):
    book = portfolio(repos)
    book.buy("TEST", 1_000.0, 2, trade_date=_dt.date(2024, 1, 2))
    book.snapshot(_dt.date(2024, 1, 2), prices={})
    assert book.positions()
    assert book.transactions()

    repos.portfolio.reset(book.portfolio_id, 50_000_000.0)
    repos.conn.commit()

    assert book.cash() == pytest.approx(50_000_000.0)
    assert book.initial_cash() == pytest.approx(50_000_000.0)
    assert book.positions() == []
    assert book.transactions() == []
    assert repos.portfolio.snapshots(book.portfolio_id).empty


def test_snapshot_and_drawdown(repos):
    book = portfolio(repos)
    book.snapshot(_dt.date(2024, 1, 2), prices={})
    book.repo.save_snapshot(book.portfolio_id, _dt.date(2024, 1, 3), cash=80_000_000.0, market_value=10_000_000.0)
    drawdown = book.drawdown()
    assert drawdown is not None
    assert drawdown < 0
