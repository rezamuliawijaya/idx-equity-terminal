"""Live IDX quotes from Yahoo Finance, with explicit provenance.

Scope and limits:

* The vendor is Yahoo Finance's public chart/spark endpoints. For the Jakarta
  exchange they serve **delayed** data (typically 10-20 minutes), so every
  quote carries ``delayed=True`` and the exchange timestamp of the last trade.
  Nothing here is presented as real-time if it is not.
* No figure is invented. A failed request raises :class:`LiveDataUnavailable`;
  a symbol the vendor does not know comes back with ``price=None`` and an
  ``error`` string. Prices are never substituted with zero or with an old value
  masquerading as current.
* The provider is stateless apart from a short in-process TTL cache, so an
  auto-refreshing UI does not hammer the vendor.

The module has no Streamlit dependency and is testable with a fake transport.
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote as _urlquote

import pandas as pd
import requests

WIB = _dt.timezone(_dt.timedelta(hours=7), "WIB")

SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

VENDOR_NAME = "Yahoo Finance"
DELAY_NOTE = "Delayed IDX quote, unofficial vendor feed. Not a real-time execution price."

#: Default IDX watch list for the live tape: (display ticker, vendor symbol).
DEFAULT_FEED: Tuple[Tuple[str, str], ...] = (
    ("IHSG", "^JKSE"),
    ("BBCA", "BBCA.JK"),
    ("BBRI", "BBRI.JK"),
    ("BMRI", "BMRI.JK"),
    ("BBNI", "BBNI.JK"),
    ("TLKM", "TLKM.JK"),
    ("ASII", "ASII.JK"),
    ("UNVR", "UNVR.JK"),
    ("ICBP", "ICBP.JK"),
    ("ADRO", "ADRO.JK"),
    ("ANTM", "ANTM.JK"),
    ("GOTO", "GOTO.JK"),
)

_SYMBOL_RE = re.compile(r"^[\^A-Z0-9][A-Z0-9.\-]{0,15}$")

#: Display names that are not the vendor's own symbol.
INDEX_ALIASES = {"IHSG": "^JKSE", "JKSE": "^JKSE"}


class LiveDataUnavailable(RuntimeError):
    """The live vendor could not be reached or returned an unusable response."""


def yahoo_symbol(ticker: str) -> str:
    """Map ``BBCA`` to ``BBCA.JK``; leave explicit or index symbols alone."""
    text = (ticker or "").strip().upper()
    if not text:
        raise ValueError("A ticker is required.")
    if text in INDEX_ALIASES:
        return INDEX_ALIASES[text]
    if text.startswith("^") or text.endswith(".JK"):
        symbol = text
    else:
        symbol = f"{text}.JK"
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"{ticker!r} is not a valid IDX ticker or Yahoo symbol.")
    return symbol


def ticker_for(symbol: str) -> str:
    """Inverse of :func:`yahoo_symbol` for display purposes."""
    text = (symbol or "").strip().upper()
    if text == "^JKSE":
        return "IHSG"
    return text[:-3] if text.endswith(".JK") else text


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _stamp(epoch: Optional[float]) -> Optional[_dt.datetime]:
    if not epoch:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(epoch), WIB)
    except (OverflowError, OSError, ValueError):
        return None


def market_state(meta: Dict[str, Any], now: Optional[_dt.datetime] = None) -> str:
    """OPEN / PRE-OPEN / CLOSED from the session the vendor reported."""
    now = now or _dt.datetime.now(WIB)
    regular = ((meta or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    start = _stamp(regular.get("start"))
    end = _stamp(regular.get("end"))
    if start is None or end is None:
        return "UNKNOWN"
    if start.date() != now.date():
        return "CLOSED"
    if now < start:
        return "PRE-OPEN"
    if now <= end:
        return "OPEN"
    return "CLOSED"


@dataclass
class LiveQuote:
    symbol: str
    ticker: str
    name: str = ""
    currency: str = "IDR"
    exchange: str = ""
    price: Optional[float] = None
    previous_close: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    volume: Optional[float] = None
    quote_time: Optional[_dt.datetime] = None
    retrieved_at: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(WIB))
    market_state: str = "UNKNOWN"
    source_url: str = ""
    delayed: bool = True
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.price is not None

    @property
    def age_seconds(self) -> Optional[float]:
        if self.quote_time is None:
            return None
        return max(0.0, (self.retrieved_at - self.quote_time).total_seconds())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ticker": self.ticker,
            "name": self.name,
            "currency": self.currency,
            "exchange": self.exchange,
            "price": self.price,
            "previous_close": self.previous_close,
            "change": self.change,
            "change_percent": self.change_percent,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "week52_high": self.week52_high,
            "week52_low": self.week52_low,
            "volume": self.volume,
            "quote_time": self.quote_time.isoformat() if self.quote_time else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "market_state": self.market_state,
            "source_url": self.source_url,
            "delayed": int(self.delayed),
            "error": self.error,
        }


def _quote_from_meta(symbol: str, meta: Dict[str, Any], source_url: str) -> LiveQuote:
    price = _number(meta.get("regularMarketPrice"))
    previous = _number(meta.get("previousClose"))
    if previous is None:
        previous = _number(meta.get("chartPreviousClose"))

    change = percent = None
    if price is not None and previous not in (None, 0):
        change = price - previous
        percent = change / previous

    quote = LiveQuote(
        symbol=symbol,
        ticker=ticker_for(symbol),
        name=str(meta.get("longName") or meta.get("shortName") or "").strip(),
        currency=str(meta.get("currency") or "IDR").upper(),
        exchange=str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
        price=price,
        previous_close=previous,
        change=change,
        change_percent=percent,
        day_high=_number(meta.get("regularMarketDayHigh")),
        day_low=_number(meta.get("regularMarketDayLow")),
        week52_high=_number(meta.get("fiftyTwoWeekHigh")),
        week52_low=_number(meta.get("fiftyTwoWeekLow")),
        volume=_number(meta.get("regularMarketVolume")),
        quote_time=_stamp(meta.get("regularMarketTime")),
        market_state=market_state(meta),
        source_url=source_url,
    )
    if price is None:
        quote.error = "Vendor returned no traded price for this symbol."
    return quote


def _error_quote(symbol: str, message: str, source_url: str = "") -> LiveQuote:
    return LiveQuote(
        symbol=symbol,
        ticker=ticker_for(symbol),
        source_url=source_url,
        error=message,
    )


# --------------------------------------------------------------------------- #
# Transport, throttling and cache
# --------------------------------------------------------------------------- #

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_call = 0.0
MIN_INTERVAL_SECONDS = 0.25

_cache: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _http_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 8.0) -> Dict[str, Any]:
    """GET JSON from the vendor. Raises :class:`LiveDataUnavailable` on failure."""
    global _last_call
    with _throttle_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()

    with _session_lock:
        global _session
        if _session is None:
            _session = requests.Session()
            _session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        session = _session

    try:
        response = session.get(url, params=params, timeout=timeout)
    except requests.Timeout as exc:
        raise LiveDataUnavailable(f"{VENDOR_NAME} timed out after {timeout:.0f}s.") from exc
    except requests.RequestException as exc:
        raise LiveDataUnavailable(f"Network error contacting {VENDOR_NAME}: {type(exc).__name__}.") from exc

    if response.status_code == 429:
        raise LiveDataUnavailable(f"{VENDOR_NAME} rate-limited the request (HTTP 429).")
    if response.status_code >= 400:
        raise LiveDataUnavailable(f"{VENDOR_NAME} returned HTTP {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveDataUnavailable(f"{VENDOR_NAME} returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise LiveDataUnavailable(f"{VENDOR_NAME} returned an unexpected payload.")
    return payload


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key: Tuple[Any, ...], ttl: float, producer):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now + ttl, value)
    return value


class YahooIDXProvider:
    """Delayed IDX quotes, intraday series and daily history."""

    def __init__(self, timeout: float = 8.0, quote_ttl: float = 15.0, history_ttl: float = 300.0):
        self.timeout = timeout
        self.quote_ttl = quote_ttl
        self.history_ttl = history_ttl

    # -- quotes ------------------------------------------------------------ #

    def quotes(self, tickers: Iterable[str], use_cache: bool = True) -> Dict[str, LiveQuote]:
        """Return quotes keyed by display ticker (``BBCA``, ``IHSG``).

        Raises :class:`LiveDataUnavailable` only when the whole request fails;
        unknown symbols come back with ``error`` set.
        """
        symbols: List[str] = []
        for ticker in tickers:
            try:
                symbol = yahoo_symbol(ticker)
            except ValueError:
                continue
            if symbol not in symbols:
                symbols.append(symbol)
        if not symbols:
            return {}

        key = ("quotes", tuple(sorted(symbols)))
        if use_cache:
            return _cached(key, self.quote_ttl, lambda: self._fetch_quotes(symbols))
        return self._fetch_quotes(symbols)

    def quote(self, ticker: str) -> LiveQuote:
        symbol = yahoo_symbol(ticker)
        return self.quotes([symbol]).get(ticker_for(symbol), _error_quote(symbol, "No quote returned."))

    def _fetch_quotes(self, symbols: Sequence[str]) -> Dict[str, LiveQuote]:
        url = SPARK_URL
        params = {"symbols": ",".join(symbols), "range": "1d", "interval": "5m"}
        payload = _http_json(url, params=params, timeout=self.timeout)
        results = ((payload.get("spark") or {}).get("result")) or []
        if not isinstance(results, list):
            raise LiveDataUnavailable(f"{VENDOR_NAME} returned an unexpected spark payload.")

        source_url = f"{url}?symbols={','.join(symbols)}&range=1d&interval=5m"
        output: Dict[str, LiveQuote] = {}
        for item in results:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol:
                continue
            responses = item.get("response") or []
            meta = (responses[0] or {}).get("meta") if responses else None
            if not isinstance(meta, dict) or not meta:
                output[ticker_for(symbol)] = _error_quote(
                    symbol, "Vendor returned no quote detail for this symbol.", source_url
                )
                continue
            output[ticker_for(symbol)] = _quote_from_meta(symbol, meta, source_url)

        # The batch endpoint occasionally drops index symbols and thin names.
        # Fill those in from the single-symbol chart endpoint before giving up.
        for symbol in symbols:
            if ticker_for(symbol) in output:
                continue
            output[ticker_for(symbol)] = self._quote_from_chart(symbol)

        return output

    def _quote_from_chart(self, symbol: str) -> LiveQuote:
        source_url = (
            f"{CHART_URL.format(symbol=_urlquote(symbol, safe=''))}?range=1d&interval=5m"
        )
        try:
            payload = _http_json(
                CHART_URL.format(symbol=_urlquote(symbol, safe="")),
                params={"range": "1d", "interval": "5m"},
                timeout=self.timeout,
            )
        except LiveDataUnavailable as exc:
            return _error_quote(symbol, str(exc), source_url)

        results = ((payload.get("chart") or {}).get("result")) or []
        meta = (results[0] or {}).get("meta") if results else None
        if not isinstance(meta, dict) or not meta:
            return _error_quote(
                symbol, "Vendor did not return this symbol (unknown or delisted).", source_url
            )
        return _quote_from_meta(symbol, meta, source_url)

    # -- series ------------------------------------------------------------ #

    def intraday(self, ticker: str, range_: str = "1d", interval: str = "5m") -> pd.DataFrame:
        """Intraday close series in WIB, columns: ``time``, ``close``."""
        symbol = yahoo_symbol(ticker)
        key = ("intraday", symbol, range_, interval)

        def produce() -> pd.DataFrame:
            payload = _http_json(
                CHART_URL.format(symbol=_urlquote(symbol, safe="")),
                params={"range": range_, "interval": interval, "includePrePost": "false"},
                timeout=self.timeout,
            )
            return _chart_frame(payload)

        frame = _cached(key, self.quote_ttl, produce)
        return frame.copy()

    def daily(self, ticker: str, range_: str = "1y") -> pd.DataFrame:
        """Daily history, columns: ``date``, ``close``, ``volume``."""
        symbol = yahoo_symbol(ticker)
        key = ("daily", symbol, range_)

        def produce() -> pd.DataFrame:
            payload = _http_json(
                CHART_URL.format(symbol=_urlquote(symbol, safe="")),
                params={"range": range_, "interval": "1d"},
                timeout=self.timeout,
            )
            return _chart_frame(payload, daily=True)

        frame = _cached(key, self.history_ttl, produce)
        return frame.copy()

    def meta(self, ticker: str) -> Dict[str, Any]:
        """Raw metadata for one symbol (used by the symbol tester)."""
        symbol = yahoo_symbol(ticker)
        payload = _http_json(
            CHART_URL.format(symbol=_urlquote(symbol, safe="")),
            params={"range": "1d", "interval": "5m"},
            timeout=self.timeout,
        )
        results = ((payload.get("chart") or {}).get("result")) or []
        return (results[0] or {}).get("meta") or {}


def _chart_frame(payload: Dict[str, Any], daily: bool = False) -> pd.DataFrame:
    results = ((payload.get("chart") or {}).get("result")) or []
    if not results:
        error = ((payload.get("chart") or {}).get("error")) or {}
        raise LiveDataUnavailable(
            f"{VENDOR_NAME} returned no chart data"
            + (f": {error.get('description')}" if error.get("description") else ".")
        )
    result = results[0] or {}
    timestamps = result.get("timestamp") or []
    quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []

    rows: List[Dict[str, Any]] = []
    for position, epoch in enumerate(timestamps):
        close = _number(closes[position]) if position < len(closes) else None
        if close is None:
            continue
        moment = _stamp(epoch)
        if moment is None:
            continue
        row: Dict[str, Any] = {
            "date" if daily else "time": moment.date() if daily else moment,
            "close": close,
        }
        if position < len(volumes):
            row["volume"] = _number(volumes[position])
        rows.append(row)

    frame = pd.DataFrame(rows, columns=["date", "close", "volume"] if daily else ["time", "close", "volume"])
    if daily and not frame.empty:
        frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    return frame


_provider: Optional[YahooIDXProvider] = None
_provider_lock = threading.Lock()


def provider() -> YahooIDXProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = YahooIDXProvider()
        return _provider


def fetch_quotes(tickers: Iterable[str]) -> Dict[str, LiveQuote]:
    """Convenience wrapper used by the UI."""
    return provider().quotes(tickers)
