"""Market data provider abstraction.

The analytics engine only ever consumes normalized rows, so a provider can be
swapped without touching valuation or scoring code.

Provider status in this build:

* ``CSVProvider``       - implemented, primary path.
* ``ManualEntryProvider`` - implemented (rows constructed in the UI).
* ``LocalDirectoryProvider`` - implemented, batch CSV folder import.
* ``IDXProvider``       - deliberately unimplemented. IDX does not publish a
  stable, documented, unauthenticated JSON API for financial statements. This
  provider raises ``ProviderUnavailable`` with a reason instead of scraping
  fragile pages or fabricating data.

No provider in this file bypasses authentication, CAPTCHAs, or terms of service.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Sequence

from src.data.csv_importer import (
    ImportResult,
    PriceImportResult,
    import_financials,
    import_prices,
)
from src.data.validation import RawFinancialRow


class ProviderError(Exception):
    """Base class for provider failures."""


class ProviderUnavailable(ProviderError):
    """Raised when a provider cannot serve data in this environment."""


@dataclass
class ProviderCapabilities:
    fundamentals: bool = False
    prices: bool = False
    corporate_actions: bool = False
    documents: bool = False
    realtime: bool = False
    automated: bool = False


@dataclass
class ProviderInfo:
    key: str
    name: str
    description: str
    homepage: str
    capabilities: ProviderCapabilities
    notes: str = ""
    requires_credentials: bool = False


class DataProvider(Protocol):
    info: ProviderInfo

    def fetch_fundamentals(self, ticker: str) -> ImportResult:  # pragma: no cover - protocol
        ...

    def fetch_prices(self, ticker: str, start: _dt.date, end: _dt.date) -> PriceImportResult:  # pragma: no cover
        ...


# --------------------------------------------------------------------------- #
# CSV provider (primary)
# --------------------------------------------------------------------------- #


class CSVProvider:
    """Imports financial statements and prices from CSV files."""

    info = ProviderInfo(
        key="csv",
        name="CSV Import",
        description="Import statements and prices from documented CSV templates.",
        homepage="local",
        capabilities=ProviderCapabilities(fundamentals=True, prices=True, documents=True),
        notes=(
            "Statements sourced manually from IDX (https://www.idx.co.id/), company annual "
            "reports, or OJK filings remain traceable through the source_url column."
        ),
    )

    def fetch_fundamentals(self, ticker: str) -> ImportResult:
        raise ProviderUnavailable("CSVProvider requires an explicit file; use import_financials().")

    def fetch_prices(self, ticker: str, start: _dt.date, end: _dt.date) -> PriceImportResult:
        raise ProviderUnavailable("CSVProvider requires an explicit file; use import_prices().")

    def load_financials(self, path: Path) -> ImportResult:
        return import_financials(path)

    def load_prices(self, path: Path) -> PriceImportResult:
        return import_prices(path)


class LocalDirectoryProvider:
    """Batch import every ``*.csv`` statement file in a directory."""

    info = ProviderInfo(
        key="directory",
        name="Local CSV Directory",
        description="Batch-import every CSV statement file in a folder.",
        homepage="local",
        capabilities=ProviderCapabilities(fundamentals=True, prices=True, documents=True),
        notes="Files are parsed independently; one bad file does not abort the batch.",
    )

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def iter_results(self) -> Iterable[tuple[Path, ImportResult]]:
        if not self.directory.exists():
            raise ProviderUnavailable(f"Directory not found: {self.directory}")
        for path in sorted(self.directory.glob("*.csv")):
            try:
                yield path, import_financials(path)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI per file
                yield path, ImportResult(source=str(path), issues=[]), exc  # type: ignore[misc]


class ManualEntryProvider:
    """Provider for figures typed directly into the UI."""

    info = ProviderInfo(
        key="manual",
        name="Manual Entry",
        description="Enter statement figures by hand, with an explicit source note.",
        homepage="local",
        capabilities=ProviderCapabilities(fundamentals=True),
        notes="Every manual figure should carry a source_url or filing reference.",
    )

    def __init__(self) -> None:
        self._rows: List[RawFinancialRow] = []

    def add_rows(self, rows: Sequence[RawFinancialRow]) -> None:
        self._rows.extend(rows)

    @property
    def rows(self) -> List[RawFinancialRow]:
        return list(self._rows)


# --------------------------------------------------------------------------- #
# IDX provider placeholder
# --------------------------------------------------------------------------- #


class IDXProvider:
    """Placeholder for an official IDX integration.

    IDX publishes statements and annual reports as PDF/XBRL downloads behind a
    website front end. There is no documented, stable, unauthenticated JSON
    endpoint for statement data, and scraping it would be brittle and likely
    contrary to the site's terms. This provider therefore refuses to pretend.
    """

    info = ProviderInfo(
        key="idx",
        name="IDX (Indonesia Stock Exchange)",
        description="Official exchange data. Not automated in this build.",
        homepage="https://www.idx.co.id/",
        capabilities=ProviderCapabilities(fundamentals=False, prices=False, documents=True),
        notes=(
            "Manual workflow: download the statement PDF/XBRL from "
            "https://www.idx.co.id/en/listed-companies/financial-statements-and-annual-report, "
            "then import the figures through the CSV template or manual entry. "
            "Configure an authorized endpoint in .env if you have licensed access."
        ),
        requires_credentials=False,
    )

    def __init__(self, endpoint: Optional[str] = None):
        self.endpoint = endpoint

    def _unavailable(self) -> ProviderUnavailable:
        if self.endpoint:
            return ProviderUnavailable(
                "An IDX endpoint is configured but no verified client is bundled. "
                "Implement the mapping in src/data/providers.py before enabling it."
            )
        return ProviderUnavailable(
            "IDX does not publish a stable public API for financial statements. "
            "Use CSV import or manual entry with a source URL."
        )

    def fetch_fundamentals(self, ticker: str) -> ImportResult:
        raise self._unavailable()

    def fetch_prices(self, ticker: str, start: _dt.date, end: _dt.date) -> PriceImportResult:
        raise self._unavailable()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRY: Dict[str, object] = {
    "csv": CSVProvider(),
    "manual": ManualEntryProvider(),
    "idx": IDXProvider(),
}


def get_provider(key: str):
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise ProviderUnavailable(f"Unknown provider {key!r}") from exc


def provider_infos() -> List[ProviderInfo]:
    infos = []
    for provider in _REGISTRY.values():
        info = getattr(provider, "info", None)
        if info is not None:
            infos.append(info)
    return infos


def provider_status_table() -> List[Dict[str, object]]:
    return [
        {
            "key": info.key,
            "name": info.name,
            "fundamentals": info.capabilities.fundamentals,
            "prices": info.capabilities.prices,
            "automated": info.capabilities.automated,
            "notes": info.notes or info.description,
        }
        for info in provider_infos()
    ]
