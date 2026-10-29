"""Write vendor end-of-day history into the local price tables.

This is the only path by which live data reaches the fundamental side of the
terminal. It copies **actual daily closes** the vendor returned; it never
derives a price, never back-fills a gap, and skips any symbol whose history is
unavailable (reporting the failure to the caller instead).

* A company ticker (``BBCA``) is written to ``prices`` only when that ticker
  already exists in ``companies``. A live symbol that has no fundamental record
  is left alone, so adding a quote symbol cannot invent a company.
* The composite index (``IHSG``) is written to ``benchmark_prices`` instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data import live


def sync_eod(
    repos,
    entries: Optional[Sequence[Dict[str, Any]]] = None,
    range_: str = "1y",
) -> Tuple[int, int, List[str]]:
    """Fetch daily bars and upsert them. Returns ``(price_rows, benchmark_rows, errors)``."""
    entries = list(entries if entries is not None else repos.live.symbols())
    price_rows = benchmark_rows = 0
    errors: List[str] = []
    provider = live.provider()

    for entry in entries:
        ticker, symbol = entry["ticker"], entry["symbol"]
        try:
            frame = provider.daily(symbol, range_=range_)
        except live.LiveDataUnavailable as exc:
            errors.append(f"{ticker}: {exc}")
            continue
        if frame.empty:
            errors.append(f"{ticker}: vendor returned no daily bars.")
            continue

        if ticker == "IHSG":
            for record in frame.to_dict("records"):
                repos.benchmarks.upsert("IHSG", record["date"], float(record["close"]))
            benchmark_rows += len(frame)
            continue

        if not repos.companies.get(ticker):
            continue

        payload = frame[["date", "close", "volume"]].copy()
        payload["ticker"] = ticker
        payload["value"] = None
        payload["source_file"] = f"yahoo:{symbol}"
        price_rows += repos.prices.upsert_many(
            payload[["ticker", "date", "close", "volume", "value", "source_file"]]
        )

    repos.conn.commit()
    return price_rows, benchmark_rows, errors
