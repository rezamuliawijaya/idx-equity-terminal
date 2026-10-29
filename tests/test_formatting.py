"""Display formatting: Indonesian separators, statuses and freshness labels."""

from __future__ import annotations

import datetime as _dt

import pytest

from src.analytics.fundamentals import Metric
from src.utils.formatting import (
    decision_emoji,
    format_date,
    format_idr,
    format_metric_value,
    format_multiple,
    format_number,
    format_percent,
    freshness_label,
    severity_rank,
)


def test_missing_values_are_never_shown_as_zero():
    assert format_idr(None) == "n/a"
    assert format_number(None) == "n/a"
    assert format_percent(None) == "n/a"
    assert format_multiple(None) == "n/a"
    assert format_date(None) == "n/a"


def test_idr_uses_indonesian_separators():
    assert format_idr(1_500_000) == "Rp 1.500.000"
    assert format_idr(1_234.5, decimals=1) == "Rp 1.234,5"
    assert format_idr(-2_000) == "Rp -2.000"


def test_compact_idr_never_mixes_up_magnitudes():
    assert format_idr(1_500_000_000_000, compact=True) == "Rp 1.50 T"
    assert format_idr(2_500_000_000, compact=True) == "Rp 2.50 bn"
    assert format_idr(3_400_000, compact=True) == "Rp 3.4 mn"
    assert format_idr(9_000, compact=True) == "Rp 9.0 k"


def test_percent_and_multiple_formatting():
    assert format_percent(0.1234) == "12,3%"
    assert format_percent(0.1234, decimals=2, signed=True) == "+12,34%"
    assert format_percent(-0.05, signed=True) == "-5,0%"
    assert format_multiple(12.345) == "12,3x"
    assert format_multiple(1234.5) == "1.234,5x"


def test_metric_value_dispatches_on_unit():
    assert format_metric_value(Metric("m", "Margin", 0.2, "percent")) == "20,0%"
    assert format_metric_value(Metric("m", "P/E", 12.3, "multiple")) == "12,3x"
    assert format_metric_value(Metric("m", "Revenue", 5_000_000_000, "idr")) == "Rp 5.00 bn"
    assert format_metric_value(Metric("m", "EPS", 125.4, "idr_per_share")) == "Rp 125"
    assert format_metric_value(Metric("m", "Cash conversion", 90, "days")) == "90 days"


def test_not_meaningful_metrics_render_as_blank_not_zero():
    metric = Metric("current_ratio", "Current Ratio", None, "multiple", status="not_meaningful", note="Not meaningful for a bank.")
    assert format_metric_value(metric) == "--"


def test_format_date_accepts_dates_and_iso_strings():
    assert format_date(_dt.date(2023, 12, 31)) == "31 Dec 2023"
    assert format_date("2024-03-31T10:00:00") == "31 Mar 2024"
    assert format_date("garbage") == "garbage"


@pytest.mark.parametrize(
    "days,expected",
    [
        (10, "10 days old (current)"),
        (120, "120 days old (current)"),
        (121, "121 days old (acceptable)"),
        (270, "270 days old (acceptable)"),
        (400, "400 days old (aging)"),
        (551, "551 days old (STALE)"),
    ],
)
def test_freshness_labels(days, expected):
    today = _dt.date(2024, 12, 31)
    assert freshness_label(today - _dt.timedelta(days=days), today=today) == expected


def test_freshness_without_a_date():
    assert freshness_label(None) == "no data"


def test_decision_and_severity_labels():
    assert decision_emoji("BUY") == "BUY"
    assert decision_emoji("SELL / REDUCE") == "SELL"
    assert decision_emoji("INSUFFICIENT DATA") == "NO DATA"
    assert decision_emoji("SOMETHING ELSE") == "SOMETHING ELSE"
    assert severity_rank("critical") < severity_rank("high") < severity_rank("medium") < severity_rank("info")
    assert severity_rank("unknown") > severity_rank("info")
