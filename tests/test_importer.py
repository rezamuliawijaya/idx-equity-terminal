"""CSV import: layouts, aliases, units and validation gating."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.data.csv_importer import (
    ImportError_,
    import_financials,
    import_prices,
    write_all_templates,
)

LONG_CSV = """ticker,period_end,period_type,statement,line_item,value,unit,currency,is_consolidated,source_url,publication_date
TEST,2023-12-31,annual,income,Revenue,"1,000,000",million,IDR,true,https://example.test/filing,2024-03-31
TEST,2023-12-31,annual,income,Net income,100000,million,IDR,true,https://example.test/filing,2024-03-31
TEST,2023-12-31,annual,balance,Total assets,2000000,million,IDR,true,https://example.test/filing,2024-03-31
TEST,2023-12-31,annual,balance,Total liabilities,1200000,million,IDR,true,https://example.test/filing,2024-03-31
TEST,2023-12-31,annual,balance,Equity,800000,million,IDR,true,https://example.test/filing,2024-03-31
TEST,2023-12-31,annual,cashflow,Cash flow from operations,150000,million,IDR,true,https://example.test/filing,2024-03-31
TEST,2023-12-31,annual,balance,Shares outstanding,1000000000,rupiah,IDR,true,https://example.test/filing,2024-03-31
"""

WIDE_CSV = """ticker,company_name,sector,period_end,period_type,unit,currency,is_consolidated,revenue,net_income,total_assets
TEST,Test Co,Industrials,2023-12-31,annual,million,IDR,true,1000000,100000,2000000
"""

PRICE_CSV = """ticker,date,close,volume,value
TEST,2024-01-02,1000,1000000,1000000000
TEST,2024-01-03,1010,900000,909000000
"""


def test_long_format_is_parsed_and_units_converted():
    result = import_financials(io.StringIO(LONG_CSV), source_name="long.csv")
    assert result.layout == "long"
    assert result.ok, result.to_frame()
    assert result.tickers == ["TEST"]
    revenue = next(row for row in result.rows if row.line_item == "revenue")
    assert revenue.value == pytest.approx(1_000_000.0)
    assert revenue.unit == "million"


def test_unknown_line_item_is_a_warning_not_a_silent_row():
    csv_text = LONG_CSV + "TEST,2023-12-31,annual,income,Vibes,42,rupiah,IDR,true,,\n"
    result = import_financials(io.StringIO(csv_text), source_name="long.csv")
    assert result.ok
    messages = [issue.message for issue in result.issues]
    assert any("Vibes" in message for message in messages)
    assert all(row.line_item != "vibes" for row in result.rows)


def test_duplicates_block_the_import():
    csv_text = LONG_CSV + "TEST,2023-12-31,annual,income,Revenue,1,million,IDR,true,,\n"
    result = import_financials(io.StringIO(csv_text), source_name="long.csv")
    assert not result.ok
    assert any(issue.code == "duplicate_item" for issue in result.report.errors)


def test_wide_format_is_parsed():
    result = import_financials(io.StringIO(WIDE_CSV), source_name="wide.csv")
    assert result.layout == "wide"
    assert result.ok, result.to_frame()
    items = {row.line_item for row in result.rows}
    assert {"revenue", "net_income", "total_assets"}.issubset(items)


def test_unknown_layout_is_rejected():
    with pytest.raises(ImportError_):
        import_financials(io.StringIO("a,b,c\n1,2,3\n"), source_name="nope.csv")


def test_parent_only_flag_is_read():
    csv_text = LONG_CSV.replace("true,https", "false,https")
    result = import_financials(io.StringIO(csv_text), source_name="parent.csv")
    assert result.ok
    assert all(row.is_consolidated is False for row in result.rows)


def test_price_import_drops_bad_rows():
    csv_text = PRICE_CSV + "TEST,2024-01-04,0,0,0\n"
    result = import_prices(io.StringIO(csv_text), source_name="prices.csv")
    assert not result.frame.empty
    assert len(result.frame) == 2
    assert result.issues
    assert result.frame["close"].min() > 0


def test_price_import_requires_columns():
    with pytest.raises(ImportError_):
        import_prices(io.StringIO("ticker,close\nTEST,1000\n"), source_name="bad.csv")


def test_templates_are_written(tmp_path: Path):
    paths = write_all_templates(tmp_path)
    assert len(paths) == 4
    for path in paths:
        assert path.exists()
        assert path.stat().st_size > 0
    assert (tmp_path / "financial_template.csv").exists()
