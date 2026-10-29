"""Unit, number and date normalisation."""

from __future__ import annotations

import datetime as _dt

import pytest

from src.data.normalization import (
    detect_unit_from_header,
    normalize_period_type,
    normalize_ticker,
    normalize_unit,
    parse_number,
    parse_period_end,
    to_idr,
    unit_multiplier,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("rupiah", "rupiah"),
        ("IDR", "rupiah"),
        ("Rp", "rupiah"),
        ("thousand", "thousand"),
        ("ribu", "thousand"),
        ("million", "million"),
        ("Rp juta", "million"),
        ("in millions", "million"),
        ("juta", "million"),
        ("billion", "billion"),
        ("miliar", "billion"),
        ("trillion", "trillion"),
    ],
)
def test_unit_aliases(raw, expected):
    unit, multiplier = normalize_unit(raw)
    assert unit == expected
    assert multiplier == unit_multiplier(raw)


def test_unit_multiplier_scales_to_absolute_rupiah():
    assert to_idr(1.5, "million", "IDR") == pytest.approx(1_500_000.0)
    assert to_idr(2, "billion", "IDR") == pytest.approx(2_000_000_000.0)
    assert to_idr(None, "billion", "IDR") is None


def test_unknown_unit_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError):
        normalize_unit("furlongs")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234", 1234.0),
        ("1.234", 1234.0),
        ("1,234.56", 1234.56),
        ("1.234,56", 1234.56),
        ("1,5", 1.5),
        ("(1,234)", -1234.0),
        ("-1,234", -1234.0),
        ("1,234-", -1234.0),
        ("Rp 1.500.000", 1_500_000.0),
        ("12.5m", 12_500_000.0),
        ("3bn", 3_000_000_000.0),
        ("12,5%", 12.5),
        ("n/a", None),
        ("", None),
        ("-", None),
        ("tidak ada", None),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_parse_number_never_substitutes_zero_for_missing():
    assert parse_number(None) is None
    assert parse_number("   ") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2023-12-31", _dt.date(2023, 12, 31)),
        ("31/12/2023", _dt.date(2023, 12, 31)),
        ("2023Q2", _dt.date(2023, 6, 30)),
        ("Q4 2023", _dt.date(2023, 12, 31)),
        ("FY2023", _dt.date(2023, 12, 31)),
        ("2023-02", _dt.date(2023, 2, 28)),
        ("31 Dec 2023", _dt.date(2023, 12, 31)),
    ],
)
def test_parse_period_end(raw, expected):
    assert parse_period_end(raw) == expected


def test_parse_period_end_rejects_garbage():
    with pytest.raises(ValueError):
        parse_period_end("not a date")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("annual", "annual"),
        ("FY", "annual"),
        ("tahunan", "annual"),
        ("quarter", "quarter"),
        ("quarterly", "quarter"),
        ("Q1", "quarter"),
        ("triwulan", "quarter"),
    ],
)
def test_period_type(raw, expected):
    assert normalize_period_type(raw) == expected


def test_ticker_normalisation():
    assert normalize_ticker(" bbca ") == "BBCA"
    assert normalize_ticker("bbca.jk") == "BBCA"
    assert normalize_ticker("DEMO-MFG") == "DEMO-MFG"
    with pytest.raises(ValueError):
        normalize_ticker("")


def test_unit_from_header():
    assert detect_unit_from_header("revenue (Rp million)") == "million"
    assert detect_unit_from_header("revenue") is None
