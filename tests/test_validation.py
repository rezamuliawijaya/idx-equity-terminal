"""Validation of imported rows and datasets."""

from __future__ import annotations

import datetime as _dt

import pytest
from pydantic import ValidationError

from src.data.validation import RawFinancialRow, validate_dataset


def row(**overrides):
    payload = {
        "ticker": "TEST",
        "period_end": "2023-12-31",
        "period_type": "annual",
        "statement": "income",
        "line_item": "revenue",
        "value": 1000.0,
        "unit": "million",
        "currency": "IDR",
        "is_consolidated": True,
    }
    payload.update(overrides)
    return RawFinancialRow(**payload)


def test_row_normalises_unit_and_currency():
    parsed = row(unit="Rp juta", currency="rp")
    assert parsed.unit == "million"
    assert parsed.currency == "IDR"


def test_row_rejects_unknown_unit():
    with pytest.raises(ValidationError):
        row(unit="furlongs")


def test_row_rejects_unknown_statement():
    with pytest.raises(ValidationError):
        row(statement="poetry")


def test_row_parses_text_numbers():
    parsed = row(value="(1,234)")
    assert parsed.value == -1234.0


def test_dataset_flags_duplicate_line_items():
    report = validate_dataset([row(), row(value=2000.0)])
    assert not report.ok
    assert "duplicate_item" in {issue.code for issue in report.errors}


def test_dataset_accepts_a_complete_period():
    rows = [
        row(line_item="revenue", statement="income", value=1000),
        row(line_item="net_income", statement="income", value=100),
        row(line_item="total_assets", statement="balance", value=2000),
        row(line_item="total_liabilities", statement="balance", value=1200),
        row(line_item="equity", statement="balance", value=800),
        row(line_item="cfo", statement="cashflow", value=150),
        row(line_item="shares_outstanding", statement="balance", value=100),
    ]
    report = validate_dataset(rows)
    assert report.ok, report.to_frame()


def test_dataset_detects_balance_sheet_that_does_not_balance():
    rows = [
        row(line_item="total_assets", statement="balance", value=2000),
        row(line_item="total_liabilities", statement="balance", value=1200),
        row(line_item="equity", statement="balance", value=1000),
    ]
    report = validate_dataset(rows)
    codes = {issue.code for issue in report.issues}
    assert "balance_sheet_imbalance" in codes
    # An imbalance is suspicious but does not block the import.
    assert report.ok
    assert report.warnings


def test_empty_dataset_is_an_error():
    report = validate_dataset([])
    assert not report.ok
    assert "empty_dataset" in {issue.code for issue in report.errors}
