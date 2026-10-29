"""Normalization helpers for imported financial data.

Responsibilities:

* Map free-text line items (English and Indonesian) onto canonical keys.
* Normalize units ("million", "juta", "thousand", ...) to absolute rupiah.
* Parse periods, numbers, and booleans coming from CSV exports.

The canonical line-item registry in ``LINE_ITEMS`` is the single source of truth
used by the importer, the statement engine, the screener, and the UI.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Canonical line items
# --------------------------------------------------------------------------- #

# ``statement`` is one of income / balance / cashflow.
# ``sign`` documents the expected sign convention (1 = normally positive).
LINE_ITEMS: Dict[str, Dict[str, object]] = {
    # ----------------------------- income statement ------------------------ #
    "revenue": {
        "statement": "income",
        "label": "Revenue",
        "aliases": [
            "revenue", "total revenue", "net revenue", "sales", "net sales",
            "total sales", "operating revenue", "turnover", "pendapatan",
            "penjualan", "pendapatan usaha", "penjualan bersih", "pendapatan bersih",
        ],
    },
    "cost_of_revenue": {
        "statement": "income",
        "label": "Cost of Revenue",
        "aliases": [
            "cost of revenue", "cost of goods sold", "cogs", "cost of sales",
            "cost of goods and services", "beban pokok penjualan", "hpp",
            "beban pokok pendapatan",
        ],
    },
    "gross_profit": {
        "statement": "income",
        "label": "Gross Profit",
        "aliases": ["gross profit", "laba kotor", "gross income"],
    },
    "operating_expenses": {
        "statement": "income",
        "label": "Operating Expenses",
        "aliases": [
            "operating expenses", "total operating expenses", "opex",
            "beban usaha", "beban operasional", "total beban usaha",
        ],
    },
    "selling_expenses": {
        "statement": "income",
        "label": "Selling Expenses",
        "aliases": ["selling expenses", "selling and marketing", "beban penjualan", "beban pemasaran"],
    },
    "general_admin_expenses": {
        "statement": "income",
        "label": "G&A Expenses",
        "aliases": [
            "general and administrative", "general and administrative expenses",
            "g&a", "beban umum dan administrasi", "beban umum", "beban administrasi",
        ],
    },
    "operating_income": {
        "statement": "income",
        "label": "Operating Income (EBIT)",
        "aliases": [
            "operating income", "operating profit", "ebit", "income from operations",
            "laba usaha", "laba operasi", "laba dari operasi",
        ],
    },
    "depreciation_amortization": {
        "statement": "income",
        "label": "Depreciation & Amortization",
        "aliases": [
            "depreciation and amortization", "depreciation & amortization", "d&a", "dna",
            "depreciation", "penyusutan dan amortisasi", "penyusutan", "amortisasi",
        ],
    },
    "ebitda": {
        "statement": "income",
        "label": "EBITDA",
        "aliases": ["ebitda"],
    },
    "interest_expense": {
        "statement": "income",
        "label": "Interest Expense",
        "aliases": [
            "interest expense", "finance costs", "financial charges", "interest and finance charges",
            "beban bunga", "beban keuangan", "biaya bunga",
        ],
    },
    "interest_income": {
        "statement": "income",
        "label": "Interest Income",
        "aliases": ["interest income", "finance income", "pendapatan bunga"],
    },
    "other_income_expense": {
        "statement": "income",
        "label": "Other Income / (Expense)",
        "aliases": [
            "other income expense", "other income", "other expenses", "other income (expense)",
            "pendapatan lain lain", "beban lain lain", "penghasilan lain lain",
        ],
    },
    "pretax_income": {
        "statement": "income",
        "label": "Pre-Tax Income",
        "aliases": [
            "pre-tax income", "pretax income", "income before tax", "profit before tax",
            "laba sebelum pajak", "laba sebelum beban pajak",
        ],
    },
    "income_tax": {
        "statement": "income",
        "label": "Income Tax",
        "aliases": ["income tax", "tax expense", "provision for income tax", "beban pajak", "pajak penghasilan"],
    },
    "net_income": {
        "statement": "income",
        "label": "Net Income",
        "aliases": [
            "net income", "net profit", "profit for the period", "profit for the year",
            "laba bersih", "laba tahun berjalan", "laba periode berjalan",
        ],
    },
    "net_income_parent": {
        "statement": "income",
        "label": "Net Income Attributable to Parent",
        "aliases": [
            "net income attributable to parent", "net income attributable to owners of the parent",
            "net profit attributable to parent", "profit attributable to owners of the parent",
            "laba bersih yang dapat diatribusikan kepada pemilik entitas induk",
            "laba yang dapat diatribusikan kepada pemilik entitas induk",
        ],
    },
    "net_interest_income": {
        "statement": "income",
        "label": "Net Interest Income",
        "aliases": [
            "net interest income", "nii", "pendapatan bunga bersih",
            "pendapatan bunga bersih setelah beban bunga",
        ],
    },
    "other_operating_income": {
        "statement": "income",
        "label": "Other Operating Income",
        "aliases": ["other operating income", "fee income", "pendapatan operasional lainnya"],
    },
    "provision_expense": {
        "statement": "income",
        "label": "Provision Expense",
        "aliases": [
            "provision expense", "provisions", "impairment losses", "allowance for credit losses",
            "beban penyisihan", "cadangan kerugian penurunan nilai", "ckpn",
        ],
    },
    "eps": {
        "statement": "income",
        "label": "Earnings Per Share",
        "aliases": [
            "eps", "earnings per share", "basic earnings per share", "basic eps",
            "laba per saham", "laba bersih per saham dasar",
        ],
    },
    # -------------------------------- balance sheet ------------------------ #
    "cash": {
        "statement": "balance",
        "label": "Cash & Cash Equivalents",
        "aliases": [
            "cash", "cash and cash equivalents", "cash equivalents",
            "kas dan setara kas", "kas dan bank",
        ],
    },
    "short_term_investments": {
        "statement": "balance",
        "label": "Short-Term Investments",
        "aliases": ["short-term investments", "current investments", "investasi jangka pendek"],
    },
    "long_term_investments": {
        "statement": "balance",
        "label": "Long-Term Investments",
        "aliases": ["long-term investments", "investasi jangka panjang", "investasi pada entitas asosiasi"],
    },
    "receivables": {
        "statement": "balance",
        "label": "Trade Receivables",
        "aliases": ["receivables", "accounts receivable", "trade receivables", "piutang usaha", "piutang"],
    },
    "inventory": {
        "statement": "balance",
        "label": "Inventory",
        "aliases": ["inventory", "inventories", "persediaan"],
    },
    "other_current_assets": {
        "statement": "balance",
        "label": "Other Current Assets",
        "aliases": ["other current assets", "aset lancar lainnya", "prepaid expenses", "beban dibayar dimuka"],
    },
    "current_assets": {
        "statement": "balance",
        "label": "Total Current Assets",
        "aliases": ["current assets", "total current assets", "jumlah aset lancar", "aset lancar"],
    },
    "ppe": {
        "statement": "balance",
        "label": "Property, Plant & Equipment",
        "aliases": [
            "property plant and equipment", "property, plant and equipment", "ppe", "fixed assets",
            "aset tetap", "aset tetap bersih",
        ],
    },
    "goodwill": {
        "statement": "balance",
        "label": "Goodwill",
        "aliases": ["goodwill", "goodwill and intangible assets"],
    },
    "intangibles": {
        "statement": "balance",
        "label": "Intangible Assets",
        "aliases": ["intangible assets", "aset takberwujud", "aset tidak berwujud"],
    },
    "total_assets": {
        "statement": "balance",
        "label": "Total Assets",
        "aliases": ["total assets", "assets", "jumlah aset", "total aset"],
    },
    "current_liabilities": {
        "statement": "balance",
        "label": "Total Current Liabilities",
        "aliases": ["current liabilities", "total current liabilities", "liabilitas jangka pendek", "kewajiban jangka pendek"],
    },
    "short_term_debt": {
        "statement": "balance",
        "label": "Short-Term Debt",
        "aliases": [
            "short-term debt", "short term debt", "short-term borrowings", "current borrowings",
            "bank loans short term", "utang bank jangka pendek", "pinjaman jangka pendek",
        ],
    },
    "long_term_debt": {
        "statement": "balance",
        "label": "Long-Term Debt",
        "aliases": [
            "long-term debt", "long term debt", "long-term borrowings", "non-current borrowings",
            "utang bank jangka panjang", "pinjaman jangka panjang", "obligasi",
        ],
    },
    "total_debt": {
        "statement": "balance",
        "label": "Total Debt",
        "aliases": ["total debt", "total borrowings", "interest-bearing debt", "total utang berbunga"],
    },
    "total_liabilities": {
        "statement": "balance",
        "label": "Total Liabilities",
        "aliases": ["total liabilities", "liabilities", "jumlah liabilitas", "total kewajiban"],
    },
    "equity": {
        "statement": "balance",
        "label": "Total Equity",
        "aliases": [
            "equity", "total equity", "shareholders equity", "shareholders' equity", "stockholders equity",
            "ekuitas", "total ekuitas", "ekuitas pemilik",
        ],
    },
    "equity_parent": {
        "statement": "balance",
        "label": "Equity Attributable to Parent",
        "aliases": [
            "equity attributable to parent", "equity attributable to owners of the parent",
            "total equity attributable to owners of the parent",
            "ekuitas yang dapat diatribusikan kepada pemilik entitas induk",
        ],
    },
    "minority_interest": {
        "statement": "balance",
        "label": "Non-Controlling Interest",
        "aliases": [
            "minority interest", "non-controlling interest", "non-controlling interests", "nci",
            "kepentingan non-pengendali", "kepentingan nonpengendali",
        ],
    },
    "shares_outstanding": {
        "statement": "balance",
        "label": "Shares Outstanding",
        "aliases": [
            "shares outstanding", "shares", "number of shares", "shares issued",
            "jumlah saham", "saham beredar", "jumlah saham beredar", "modal ditempatkan dan disetor",
        ],
    },
    "retained_earnings": {
        "statement": "balance",
        "label": "Retained Earnings",
        "aliases": ["retained earnings", "saldo laba", "laba ditahan"],
    },
    "loans": {
        "statement": "balance",
        "label": "Gross Loans",
        "aliases": ["loans", "gross loans", "loan book", "kredit", "kredit yang diberikan", "pinjaman yang diberikan"],
    },
    "customer_deposits": {
        "statement": "balance",
        "label": "Customer Deposits",
        "aliases": ["customer deposits", "deposits", "third party funds", "dana pihak ketiga", "simpanan nasabah"],
    },
    "npl": {
        "statement": "balance",
        "label": "Non-Performing Loans",
        "aliases": ["npl", "non-performing loans", "kredit bermasalah", "non performing loan"],
    },
    "npl_ratio": {
        "statement": "balance",
        "label": "NPL Ratio (reported)",
        "aliases": ["npl ratio", "non-performing loan ratio", "rasio npl", "rasio kredit bermasalah"],
    },
    "car_ratio": {
        "statement": "balance",
        "label": "Capital Adequacy Ratio (reported)",
        "aliases": ["car", "capital adequacy ratio", "rasio kecukupan modal", "kewajiban penyediaan modal minimum", "kpmm"],
    },
    # -------------------------------- cash flow ---------------------------- #
    "cfo": {
        "statement": "cashflow",
        "label": "Cash Flow from Operations",
        "aliases": [
            "cash flow from operations", "operating cash flow", "cash from operations", "cfo",
            "net cash from operating activities", "net cash provided by operating activities",
            "arus kas dari kegiatan operasi", "arus kas bersih dari kegiatan operasi",
            "kas bersih dari aktivitas operasi",
        ],
    },
    "capex": {
        "statement": "cashflow",
        "label": "Capital Expenditure",
        "aliases": [
            "capex", "capital expenditure", "capital expenditures", "purchase of property plant and equipment",
            "acquisition of fixed assets", "pengeluaran modal", "perolehan aset tetap",
        ],
    },
    "cfi": {
        "statement": "cashflow",
        "label": "Cash Flow from Investing",
        "aliases": [
            "cash flow from investing", "investing cash flow", "cfi",
            "net cash used in investing activities", "net cash from investing activities",
            "arus kas dari kegiatan investasi", "arus kas bersih dari kegiatan investasi",
            "kas bersih dari aktivitas investasi",
        ],
    },
    "cff": {
        "statement": "cashflow",
        "label": "Cash Flow from Financing",
        "aliases": [
            "cash flow from financing", "financing cash flow", "cff",
            "net cash from financing activities", "net cash used in financing activities",
            "arus kas dari kegiatan pendanaan", "arus kas bersih dari kegiatan pendanaan",
            "kas bersih dari aktivitas pendanaan",
        ],
    },
    "fcf": {
        "statement": "cashflow",
        "label": "Free Cash Flow",
        "aliases": ["free cash flow", "fcf", "arus kas bebas"],
    },
    "dividends_paid": {
        "statement": "cashflow",
        "label": "Dividends Paid",
        "aliases": ["dividends paid", "dividend paid", "dividend", "dividen", "pembayaran dividen"],
    },
    "debt_issued": {
        "statement": "cashflow",
        "label": "Debt Issued",
        "aliases": ["debt issued", "proceeds from borrowings", "new borrowings", "penerimaan pinjaman"],
    },
    "debt_repaid": {
        "statement": "cashflow",
        "label": "Debt Repaid",
        "aliases": ["debt repaid", "repayment of borrowings", "pembayaran pinjaman"],
    },
    "change_in_working_capital": {
        "statement": "cashflow",
        "label": "Change in Working Capital",
        "aliases": ["change in working capital", "changes in working capital", "perubahan modal kerja"],
    },
    "net_change_in_cash": {
        "statement": "cashflow",
        "label": "Net Change in Cash",
        "aliases": ["net change in cash", "net increase in cash", "kenaikan bersih kas"],
    },
}

ITEM_LABEL: Dict[str, str] = {key: str(meta["label"]) for key, meta in LINE_ITEMS.items()}
ITEM_STATEMENT: Dict[str, str] = {key: str(meta["statement"]) for key, meta in LINE_ITEMS.items()}

# Items required before the analytics engine will produce a full assessment.
ESSENTIAL_ITEMS: Tuple[str, ...] = (
    "revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "equity",
    "cfo",
    "shares_outstanding",
)

STATEMENTS = ("income", "balance", "cashflow")


def _build_alias_index() -> Dict[str, str]:
    index: Dict[str, str] = {}

    def _norm(text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9& ]+", " ", text.lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    for key, meta in LINE_ITEMS.items():
        index[_norm(key)] = key
        index[_norm(str(meta["label"]))] = key
        for alias in meta["aliases"]:  # type: ignore[union-attr]
            index.setdefault(_norm(alias), key)
    return index


_ALIAS_INDEX = _build_alias_index()


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower())
    return cleaned.strip("_")


def humanize(key: str) -> str:
    return key.replace("_", " ").title()


def canonicalize_line_item(name: str) -> Tuple[str, bool]:
    """Return ``(canonical_key, recognized)`` for a free-text line item."""
    if not name:
        return "", False
    text = str(name)
    if text in LINE_ITEMS:
        return text, True
    normalized = re.sub(r"[^a-z0-9& ]+", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if normalized in _ALIAS_INDEX:
        return _ALIAS_INDEX[normalized], True
    # Retry without a trailing unit annotation such as "revenue (rp million)".
    stripped = re.sub(r"\((?:rp|idr)?[^)]*\)\s*$", "", normalized).strip()
    if stripped and stripped in _ALIAS_INDEX:
        return _ALIAS_INDEX[stripped], True
    return slugify(text), False


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

UNIT_MULTIPLIERS: Dict[str, float] = {
    "rupiah": 1.0,
    "idr": 1.0,
    "rp": 1.0,
    "unit": 1.0,
    "one": 1.0,
    "thousand": 1e3,
    "ribu": 1e3,
    "thousands": 1e3,
    "rb": 1e3,
    "k": 1e3,
    "million": 1e6,
    "millions": 1e6,
    "juta": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "billion": 1e9,
    "billions": 1e9,
    "miliar": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "trillion": 1e12,
    "triliun": 1e12,
    "tn": 1e12,
    "t": 1e12,
    "percent": 0.01,
    "pct": 0.01,
    "%": 0.01,
    "ratio": 1.0,
}

UNIT_ALIASES = {
    "rp juta": "million",
    "rp. juta": "million",
    "rp ribu": "thousand",
    "rp miliar": "billion",
    "in millions": "million",
    "in thousands": "thousand",
    "in billions": "billion",
    "juta rupiah": "million",
    "ribu rupiah": "thousand",
    "miliar rupiah": "billion",
}


def normalize_unit(unit: Optional[str]) -> Tuple[str, float]:
    """Return ``(canonical_unit_name, multiplier_to_absolute_rupiah)``."""
    if unit is None or str(unit).strip() == "":
        return "rupiah", 1.0
    text = str(unit).strip().lower().replace(".", "")
    text = re.sub(r"\s+", " ", text)
    if text in UNIT_ALIASES:
        text = UNIT_ALIASES[text]
    if text in UNIT_MULTIPLIERS:
        return _canonical_unit_label(text), UNIT_MULTIPLIERS[text]
    raise ValueError(f"Unrecognized unit: {unit!r}")


def _canonical_unit_label(text: str) -> str:
    if text in {"thousand", "ribu", "thousands", "rb", "k"}:
        return "thousand"
    if text in {"million", "millions", "juta", "mn", "m"}:
        return "million"
    if text in {"billion", "billions", "miliar", "bn", "b"}:
        return "billion"
    if text in {"trillion", "triliun", "tn", "t"}:
        return "trillion"
    if text in {"percent", "pct", "%", "ratio"}:
        return "ratio"
    return "rupiah"


def unit_multiplier(unit: Optional[str]) -> float:
    return normalize_unit(unit)[1]


def detect_unit_from_header(header: str) -> Optional[str]:
    """Extract a scale from a column header like ``Revenue (Rp million)``.

    A parenthesised currency on its own (``(Rp)``) says nothing about magnitude,
    so it returns ``None`` rather than guessing rupiah.
    """
    match = re.search(r"\(([^)]*)\)", header or "")
    if not match:
        return None
    inner = re.sub(r"\s+", " ", match.group(1).strip().lower().replace(".", "")).strip()
    inner = re.sub(r"^(rp|idr|us\$|usd|sgd)\s+", "", inner).strip()
    if not inner or inner in {"rp", "idr", "rupiah", "%", "ratio", "one", "unit"}:
        return None
    try:
        canonical, _ = normalize_unit(inner)
    except ValueError:
        return None
    return None if canonical == "rupiah" else canonical


CURRENCY_ALIASES = {"rp": "IDR", "idr": "IDR", "rupiah": "IDR", "usd": "USD", "us$": "USD", "sgd": "SGD"}


def normalize_currency(currency: Optional[str]) -> str:
    if not currency:
        return "IDR"
    text = str(currency).strip().upper()
    return CURRENCY_ALIASES.get(text.lower(), text)


# --------------------------------------------------------------------------- #
# Numbers and periods
# --------------------------------------------------------------------------- #

_NULL_TOKENS = {"", "-", "--", "—", "–", "n/a", "na", "n.a.", "null", "none", "nil", "nan", "tidak ada"}


def parse_number(value: object) -> Optional[float]:
    """Parse a financial number from raw CSV text.

    Handles thousands separators, decimal commas, accounting negatives in
    parentheses, currency prefixes, and magnitude suffixes. Returns ``None`` for
    blank/censored cells instead of substituting zero.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            if value != value:  # NaN
                return None
        except TypeError:  # pragma: no cover - exotic numeric types
            pass
        return float(value)
    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if text.startswith("-") or text.endswith("-"):
        negative = True
        text = text.strip("-").strip()
    if text.startswith("+"):
        text = text[1:].strip()

    text = text.replace("\u00a0", " ").replace(" ", "")
    text = re.sub(r"^(rp|idr|usd|us\$|\$)", "", text, flags=re.IGNORECASE)
    text = text.replace("%", "")

    suffix_multiplier = 1.0
    suffix_match = re.search(r"(?<=\d)(k|m|bn|b|tn|t)$", text, flags=re.IGNORECASE)
    if suffix_match:
        suffix = suffix_match.group(1).lower()
        suffix_multiplier = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9, "t": 1e12, "tn": 1e12}[suffix]
        text = text[: suffix_match.start()]

    if text == "":
        return None

    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        # "1,234" is thousands; "1,5" is a decimal comma.
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    elif has_dot:
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", text):
            text = text.replace(".", "")

    try:
        number = float(text)
    except ValueError:
        return None
    number *= suffix_multiplier
    return -number if negative else number


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y",
    "%Y-%m", "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y", "%d-%b-%Y", "%d-%B-%Y",
)


def parse_period_end(value: object) -> _dt.date:
    """Parse a reporting period end date.

    Accepts ISO dates, Indonesian-style ``dd/mm/yyyy``, ``YYYY-MM`` (mapped to
    month end), and quarter labels such as ``2023Q2`` or ``Q2 2023``.
    """
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if value is None:
        raise ValueError("period_end is required")
    text = str(value).strip()
    if not text:
        raise ValueError("period_end is required")

    quarter_match = re.fullmatch(r"(?:(\d{4})[-\s]?q([1-4])|q([1-4])[-\s]?(\d{4}))", text, flags=re.IGNORECASE)
    if quarter_match:
        year = int(quarter_match.group(1) or quarter_match.group(4))
        quarter = int(quarter_match.group(2) or quarter_match.group(3))
        month = quarter * 3
        return _dt.date(year, month, _days_in_month(year, month))

    fiscal_match = re.fullmatch(r"fy\s?(\d{4})", text, flags=re.IGNORECASE)
    if fiscal_match:
        return _dt.date(int(fiscal_match.group(1)), 12, 31)

    cleaned = text.replace("Sept", "Sep")
    for fmt in _DATE_FORMATS:
        try:
            parsed = _dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m":
            return _dt.date(parsed.year, parsed.month, _days_in_month(parsed.year, parsed.month))
        return parsed.date()

    # Last resort: ISO datetime prefix.
    try:
        return _dt.date.fromisoformat(cleaned[:10])
    except ValueError as exc:
        raise ValueError(f"Unrecognized date format: {value!r}") from exc


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (_dt.date(year, month + 1, 1) - _dt.timedelta(days=1)).day


def normalize_period_type(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"annual", "fy", "year", "yearly", "12m", "full year", "audited", "tahunan"}:
        return "annual"
    if text in {"quarter", "quarterly", "q", "interim", "3m", "6m", "9m", "tw", "kuartal", "triwulan"}:
        return "quarter"
    if re.fullmatch(r"q[1-4]", text):
        return "quarter"
    if text in {"", "none", "nan"}:
        return "annual"
    raise ValueError(f"Unrecognized period_type: {value!r}")


def normalize_period_basis(value: object) -> str:
    """Quarterly reporting basis: ``cumulative`` (IDX default) or ``discrete``."""
    text = str(value or "cumulative").strip().lower()
    if text in {"cumulative", "ytd", "year to date", "kumulatif"}:
        return "cumulative"
    if text in {"discrete", "quarter only", "single quarter", "standalone", "kuartal"}:
        return "discrete"
    return "cumulative"


def parse_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"", "nan", "none", "null"}:
        return default
    return text in {"1", "true", "yes", "y", "t", "consolidated", "consolidation", "konsolidasi", "konsolidasian"}


def quarter_of(period_end: _dt.date) -> int:
    return (period_end.month - 1) // 3 + 1


def months_in_period(period_end: _dt.date, basis: str = "cumulative") -> int:
    if basis == "discrete":
        return 3
    return quarter_of(period_end) * 3


def normalize_ticker(value: object) -> str:
    text = str(value or "").strip().upper()
    text = text.replace(".JK", "").replace("JK:", "")
    if not text:
        raise ValueError("ticker is required")
    if not re.fullmatch(r"[A-Z0-9\-]{1,12}", text):
        raise ValueError(f"Invalid ticker: {value!r}")
    return text


def to_idr(value: Optional[float], unit: Optional[str], currency: Optional[str]) -> Optional[float]:
    """Convert a reported figure to absolute rupiah."""
    if value is None:
        return None
    code = normalize_currency(currency)
    if code != "IDR":
        raise ValueError(f"Unsupported currency {code!r}; import rates separately before mixing currencies")
    return float(value) * unit_multiplier(unit)


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output
