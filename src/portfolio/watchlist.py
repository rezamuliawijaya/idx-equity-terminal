"""Watchlist and alert rules.

Alerts are evaluated against stored data only. They are informational and never
place orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config.settings import TerminalSettings
from src.analytics.engine import analyze_ticker


@dataclass
class Alert:
    ticker: str
    kind: str
    message: str
    severity: str = "info"

    def as_dict(self) -> Dict[str, Any]:
        return {"ticker": self.ticker, "kind": self.kind, "message": self.message, "severity": self.severity}


class WatchlistService:
    def __init__(self, repos, settings: Optional[TerminalSettings] = None):
        self.repos = repos
        self.settings = settings or TerminalSettings()

    def add(self, ticker: str, notes: str = "", target_margin_of_safety: Optional[float] = None) -> None:
        self.repos.watchlist.add(ticker.upper(), notes, target_margin_of_safety)

    def remove(self, ticker: str) -> None:
        self.repos.watchlist.remove(ticker.upper())

    def set_alerts(self, ticker: str, above: Optional[float], below: Optional[float]) -> None:
        self.repos.watchlist.set_alerts(ticker.upper(), above, below)

    def entries(self) -> List[Dict[str, Any]]:
        rows = self.repos.watchlist.list()
        output: List[Dict[str, Any]] = []
        for row in rows:
            ticker = row["ticker"]
            latest = self.repos.prices.latest(ticker)
            analysis = None
            try:
                analysis = analyze_ticker(self.repos, ticker, self.settings)
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {exc}"
            alerts: List[Alert] = []

            price = latest["close"] if latest else None
            if price is not None and row.get("alert_price_above") and price >= row["alert_price_above"]:
                alerts.append(Alert(ticker, "price_above", f"Price Rp {price:,.0f} is at or above the Rp {row['alert_price_above']:,.0f} alert.", "info"))
            if price is not None and row.get("alert_price_below") and price <= row["alert_price_below"]:
                alerts.append(Alert(ticker, "price_below", f"Price Rp {price:,.0f} is at or below the Rp {row['alert_price_below']:,.0f} alert.", "medium"))

            if analysis is not None:
                mos = analysis.value("margin_of_safety")
                target = row.get("target_margin_of_safety")
                if mos is not None and target is not None and mos >= target:
                    alerts.append(Alert(ticker, "margin_of_safety", f"Margin of safety {mos:.1%} clears the {target:.0%} target.", "medium"))
                for flag in analysis.flags:
                    if flag.severity == "critical":
                        alerts.append(Alert(ticker, "risk", flag.message, "critical"))

            output.append(
                {
                    **row,
                    "price": price,
                    "price_date": latest["date"] if latest else None,
                    "decision": analysis.recommendation.decision if analysis else "INSUFFICIENT DATA",
                    "score": analysis.recommendation.score if analysis else None,
                    "confidence": analysis.recommendation.confidence if analysis else None,
                    "margin_of_safety": analysis.value("margin_of_safety") if analysis else None,
                    "fair_value": analysis.fair_value if analysis else None,
                    "alerts": [alert.as_dict() for alert in alerts],
                }
            )
        return output

    def alerts(self) -> List[Dict[str, Any]]:
        return [alert for entry in self.entries() for alert in entry.get("alerts", [])]
