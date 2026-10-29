"""Display formatting helpers.

Rupiah formatting follows Indonesian conventions (dot thousands separator,
comma decimal separator) for narrative text, while table columns use plain
numeric values so sorting works.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, Optional

from src.analytics.fundamentals import (
    CALCULATED,
    ESTIMATED,
    NOT_MEANINGFUL,
    REPORTED,
    UNAVAILABLE,
    Metric,
)

STATUS_LABELS = {
    REPORTED: "reported",
    CALCULATED: "calculated",
    ESTIMATED: "estimated",
    NOT_MEANINGFUL: "not meaningful",
    UNAVAILABLE: "unavailable",
}


def format_idr(value: Optional[float], compact: bool = False, decimals: int = 0) -> str:
    """Format an absolute rupiah amount."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    number = float(value)
    if compact:
        magnitude = abs(number)
        if magnitude >= 1e12:
            return f"Rp {number / 1e12:,.2f} T"
        if magnitude >= 1e9:
            return f"Rp {number / 1e9:,.2f} bn"
        if magnitude >= 1e6:
            return f"Rp {number / 1e6:,.1f} mn"
        if magnitude >= 1e3:
            return f"Rp {number / 1e3:,.1f} k"
    return "Rp " + f"{number:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def format_number(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def format_percent(value: Optional[float], decimals: int = 1, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    text = f"{value:+.{decimals}%}" if signed else f"{value:.{decimals}%}"
    return text.replace(".", ",")


def format_multiple(value: Optional[float], decimals: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{decimals}f}x".replace(",", "_").replace(".", ",").replace("_", ".")


def format_days(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f} days"


def format_metric_value(metric: Optional[Metric], compact_idr: bool = True) -> str:
    if metric is None:
        return "n/a"
    if metric.value is None:
        return "--"
    unit = metric.unit
    if unit == "percent":
        return format_percent(metric.value)
    if unit == "multiple":
        return format_multiple(metric.value)
    if unit == "idr":
        return format_idr(metric.value, compact=compact_idr)
    if unit == "idr_per_share":
        return format_idr(metric.value, decimals=0)
    if unit == "days":
        return format_days(metric.value)
    if unit == "score":
        return f"{metric.value:,.2f}"
    return format_number(metric.value)


def metric_label_with_status(metric: Metric) -> str:
    label = metric.label
    status = STATUS_LABELS.get(metric.status, metric.status)
    return f"{label} ({status})"


def format_date(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%d %b %Y")
    try:
        return _dt.date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value)


def freshness_label(as_of: Optional[_dt.date], today: Optional[_dt.date] = None) -> str:
    if as_of is None:
        return "no data"
    today = today or _dt.date.today()
    days = (today - as_of).days
    if days <= 120:
        return f"{days} days old (current)"
    if days <= 270:
        return f"{days} days old (acceptable)"
    if days <= 550:
        return f"{days} days old (aging)"
    return f"{days} days old (STALE)"


def decision_emoji(decision: str) -> str:
    return {
        "BUY": "BUY",
        "HOLD": "HOLD",
        "SELL / REDUCE": "SELL",
        "AVOID": "AVOID",
        "INSUFFICIENT DATA": "NO DATA",
    }.get(decision, decision)


def severity_rank(severity: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "info": 3}.get(severity, 9)
