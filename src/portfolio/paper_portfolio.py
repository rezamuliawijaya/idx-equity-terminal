"""Simulated (paper) portfolio with IDX lot and cost rules.

No broker connection exists anywhere in this module. Orders are recorded in the
local SQLite database and priced at whatever price the caller supplies, which
must come from stored market data or an explicit manual entry.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config.settings import TradingCosts
from src.database.repositories import PortfolioRepository


@dataclass
class TradeOutcome:
    ok: bool
    message: str
    transaction: Optional[Dict[str, Any]] = None


@dataclass
class Position:
    ticker: str
    shares: float = 0.0
    average_cost: float = 0.0
    last_price: Optional[float] = None
    sector: Optional[str] = None
    realized_pnl: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.shares * self.average_cost

    @property
    def market_value(self) -> Optional[float]:
        if self.last_price is None:
            return None
        return self.shares * self.last_price

    @property
    def unrealized_pnl(self) -> Optional[float]:
        if self.last_price is None:
            return None
        return (self.last_price - self.average_cost) * self.shares

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "shares": self.shares,
            "average_cost": self.average_cost,
            "last_price": self.last_price,
            "cost_basis": self.cost_basis,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pct": (self.unrealized_pnl / self.cost_basis) if self.unrealized_pnl is not None and self.cost_basis else None,
            "sector": self.sector,
        }


class PaperPortfolio:
    def __init__(
        self,
        repo: PortfolioRepository,
        costs: Optional[TradingCosts] = None,
        portfolio_id: Optional[int] = None,
    ):
        self.repo = repo
        self.costs = costs or TradingCosts()
        if portfolio_id is None:
            portfolio = repo.ensure()
            portfolio_id = int(portfolio["id"])
        self.portfolio_id = portfolio_id

    # ------------------------------ state -------------------------------- #
    def cash(self) -> float:
        return float(self.repo.get(self.portfolio_id).get("cash") or 0.0)

    def initial_cash(self) -> float:
        return float(self.repo.get(self.portfolio_id).get("initial_cash") or 0.0)

    def transactions(self) -> List[Dict[str, Any]]:
        return self.repo.transactions(self.portfolio_id)

    def positions(self) -> List[Position]:
        """Average-cost book built from the transaction ledger."""
        positions: Dict[str, Position] = {}
        for row in self.transactions():
            ticker = row["ticker"]
            position = positions.setdefault(ticker, Position(ticker=ticker))
            shares = float(row["shares"])
            price = float(row["price"])
            if row["side"] == "buy":
                total_cost = position.average_cost * position.shares + price * shares
                position.shares += shares
                position.average_cost = total_cost / position.shares if position.shares else 0.0
            else:
                position.shares -= shares
                if position.shares < 1e-6:
                    position.shares = 0.0
                position.realized_pnl += float(row.get("realized_pnl") or 0.0)
        return [position for position in positions.values() if position.shares > 1e-6]

    def realized_pnl(self) -> float:
        return sum(float(row.get("realized_pnl") or 0.0) for row in self.transactions())

    # ------------------------------ orders ------------------------------- #
    def preview_buy(self, ticker: str, price: float, lots: int) -> TradeOutcome:
        return self._validate_buy(ticker, price, lots)

    def _validate_buy(self, ticker: str, price: float, lots: int) -> TradeOutcome:
        if lots is None or int(lots) != lots or lots <= 0:
            return TradeOutcome(False, "Lots must be a positive whole number.")
        if price is None or price <= 0:
            return TradeOutcome(False, "Price must be positive.")
        shares = int(lots) * self.costs.lot_size
        gross = shares * float(price)
        fees = max(self.costs.min_fee, gross * self.costs.buy_fee_rate)
        total = gross + fees
        cash = self.cash()
        if total > cash + 1e-6:
            return TradeOutcome(False, f"Insufficient cash: need Rp {total:,.0f}, have Rp {cash:,.0f}.")
        return TradeOutcome(True, "ok", {"shares": shares, "gross": gross, "fees": fees, "total": total})

    def buy(
        self,
        ticker: str,
        price: float,
        lots: int,
        trade_date: Optional[_dt.date] = None,
        note: str = "",
        price_map: Optional[Dict[str, float]] = None,
    ) -> TradeOutcome:
        outcome = self._validate_buy(ticker, price, lots)
        if not outcome.ok or outcome.transaction is None:
            return outcome
        ticker = ticker.upper()
        trade_date = trade_date or _dt.date.today()

        if price_map:
            portfolio_value = self.portfolio_value(price_map)
            if portfolio_value > 0:
                existing = next((p for p in self.positions() if p.ticker == ticker), None)
                current_value = (existing.shares * price) if existing else 0.0
                projected = (current_value + outcome.transaction["shares"] * price) / portfolio_value
                if projected > self.costs.max_position_pct:
                    return TradeOutcome(
                        False,
                        f"Position limit: this order would put {projected:.0%} of the portfolio in {ticker} "
                        f"(limit {self.costs.max_position_pct:.0%}).",
                    )

        payload = {
            "ticker": ticker,
            "side": "buy",
            "trade_date": trade_date.isoformat(),
            "price": float(price),
            "lots": int(lots),
            "shares": outcome.transaction["shares"],
            "gross": outcome.transaction["gross"],
            "fees": outcome.transaction["fees"],
            "net_cash": -outcome.transaction["total"],
            "realized_pnl": None,
            "note": note,
        }
        self.repo.add_transaction(self.portfolio_id, **payload)
        self.repo.update_cash(self.portfolio_id, self.cash() - outcome.transaction["total"])
        payload["message"] = (
            f"Bought {lots:,} lots ({outcome.transaction['shares']:,} shares) of {ticker} at Rp {price:,.0f}; "
            f"fees Rp {outcome.transaction['fees']:,.0f}."
        )
        return TradeOutcome(True, payload["message"], payload)

    def sell(
        self,
        ticker: str,
        price: float,
        lots: int,
        trade_date: Optional[_dt.date] = None,
        note: str = "",
    ) -> TradeOutcome:
        ticker = ticker.upper()
        if lots <= 0:
            return TradeOutcome(False, "Lots must be positive.")
        if price is None or price <= 0:
            return TradeOutcome(False, "Price must be positive.")
        position = next((p for p in self.positions() if p.ticker == ticker), None)
        if position is None:
            return TradeOutcome(False, f"No open position in {ticker}.")
        shares = lots * self.costs.lot_size
        if shares > position.shares + 1e-6:
            return TradeOutcome(
                False,
                f"Cannot sell {shares:,} shares; the position holds {position.shares:,.0f}.",
            )
        gross = shares * float(price)
        fees = max(self.costs.min_fee, gross * self.costs.sell_fee_rate)
        proceeds = gross - fees
        realized = (float(price) - position.average_cost) * shares - fees
        trade_date = trade_date or _dt.date.today()
        payload = {
            "ticker": ticker,
            "side": "sell",
            "trade_date": trade_date.isoformat(),
            "price": float(price),
            "lots": int(lots),
            "shares": shares,
            "gross": gross,
            "fees": fees,
            "net_cash": proceeds,
            "realized_pnl": realized,
            "note": note,
        }
        self.repo.add_transaction(self.portfolio_id, **payload)
        self.repo.update_cash(self.portfolio_id, self.cash() + proceeds)
        payload["message"] = (
            f"Sold {lots:,} lots of {ticker} at Rp {price:,.0f}; realised P&L Rp {realized:,.0f} after fees."
        )
        return TradeOutcome(True, payload["message"], payload)

    # ---------------------------- valuation ------------------------------ #
    def price_map(self, repo) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for position in self.positions():
            latest = repo.prices.latest(position.ticker)
            if latest:
                prices[position.ticker] = latest["close"]
        return prices

    def portfolio_value(self, prices: Optional[Dict[str, float]] = None) -> float:
        prices = prices or {}
        total = self.cash()
        for position in self.positions():
            price = prices.get(position.ticker) or position.average_cost
            total += position.shares * price
        return total

    def summary(
        self,
        prices: Optional[Dict[str, float]] = None,
        sectors: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        prices = prices or {}
        sectors = sectors or {}
        positions = []
        for position in self.positions():
            position.last_price = prices.get(position.ticker)
            position.sector = sectors.get(position.ticker, "Unclassified")
            positions.append(position)
        total_value = self.cash() + sum(p.market_value or p.cost_basis for p in positions)
        initial = self.initial_cash() or 1.0
        return {
            "cash": self.cash(),
            "market_value": sum(p.market_value or p.cost_basis for p in positions),
            "total_value": total_value,
            "initial_cash": self.initial_cash(),
            "total_return": total_value / initial - 1.0 if initial else None,
            "unrealized_pnl": sum(p.unrealized_pnl or 0.0 for p in positions),
            "realized_pnl": self.realized_pnl(),
            "positions": [p.as_dict() for p in positions],
            "allocation": [
                {
                    "ticker": p.ticker,
                    "value": p.market_value or p.cost_basis,
                    "weight": (p.market_value or p.cost_basis) / total_value if total_value else 0.0,
                    "sector": p.sector,
                }
                for p in positions
            ],
            "cash_weight": self.cash() / total_value if total_value else 0.0,
        }

    def sector_concentration(self, prices: Optional[Dict[str, float]] = None, sectors: Optional[Dict[str, str]] = None) -> Dict[str, float]:
        summary = self.summary(prices, sectors)
        totals: Dict[str, float] = {}
        for item in summary["allocation"]:
            totals[item["sector"]] = totals.get(item["sector"], 0.0) + item["value"]
        total = summary["total_value"] or 1.0
        return {sector: value / total for sector, value in sorted(totals.items(), key=lambda kv: -kv[1])}

    def snapshot(self, date: Optional[_dt.date] = None, prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        summary = self.summary(prices)
        self.repo.save_snapshot(
            self.portfolio_id,
            date or _dt.date.today(),
            summary["cash"],
            summary["market_value"],
        )
        return summary

    def drawdown(self) -> Optional[float]:
        frame = self.repo.snapshots(self.portfolio_id)
        if frame.empty or len(frame) < 2:
            return None
        values = frame["total_value"].astype(float)
        peak = values.cummax()
        return float((values / peak - 1.0).min())


def check_sector_limit(allocation: Sequence[Dict[str, Any]], costs: TradingCosts) -> List[str]:
    """Return warnings for sector weights above the configured limit."""
    totals: Dict[str, float] = {}
    for item in allocation:
        totals[item.get("sector") or "Unclassified"] = totals.get(item.get("sector") or "Unclassified", 0.0) + float(item.get("value") or 0.0)
    total = sum(totals.values())
    if total <= 0:
        return []
    return [
        f"{sector} is {value / total:.0%} of the portfolio (limit {costs.max_sector_pct:.0%})."
        for sector, value in totals.items()
        if value / total > costs.max_sector_pct
    ]
