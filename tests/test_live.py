"""Live feed parsing, caching and persistence.

No network call is made: the transport is replaced with canned vendor payloads,
which is also how the failure paths are exercised.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from src.data import live
from src.database.db import connect, init_db
from src.database.repositories import Repositories


def spark_payload(symbol: str, price=4970.0, previous=5275.0, name="Bank Central Asia Tbk") -> dict:
    """A minimal but shape-accurate spark response."""
    return {
        "spark": {
            "result": [
                {
                    "symbol": symbol,
                    "response": [
                        {
                            "meta": {
                                "currency": "IDR",
                                "symbol": symbol,
                                "exchangeName": "JKT",
                                "fullExchangeName": "Jakarta",
                                "regularMarketTime": 1793261281,
                                "previousClose": previous,
                                "regularMarketPrice": price,
                                "regularMarketDayHigh": 5325.0,
                                "regularMarketDayLow": 4960.0,
                                "fiftyTwoWeekHigh": 8750.0,
                                "fiftyTwoWeekLow": 4740.0,
                                "regularMarketVolume": 229359100,
                                "longName": name,
                                "currentTradingPeriod": {
                                    "regular": {"start": 1793239200, "end": 1793265300}
                                },
                            },
                            "timestamp": [1793239500, 1793239800],
                            "indicators": {"quote": [{"close": [4965.0, 4970.0]}]},
                        }
                    ],
                }
            ]
        }
    }


def chart_payload(symbol: str, closes=(4960.0, None, 4970.0)) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "regularMarketPrice": closes[-1]},
                    "timestamp": [1793239200 + i * 300 for i in range(len(closes))],
                    "indicators": {"quote": [{"close": list(closes), "volume": [10, 20, 30]}]},
                }
            ],
            "error": None,
        }
    }


@pytest.fixture(autouse=True)
def clean_cache():
    live.clear_cache()
    yield
    live.clear_cache()


def test_symbol_mapping():
    assert live.yahoo_symbol("bbca") == "BBCA.JK"
    assert live.yahoo_symbol("BBCA.JK") == "BBCA.JK"
    assert live.yahoo_symbol("^JKSE") == "^JKSE"
    assert live.ticker_for("BBCA.JK") == "BBCA"
    assert live.ticker_for("^JKSE") == "IHSG"
    with pytest.raises(ValueError):
        live.yahoo_symbol("  ")
    with pytest.raises(ValueError):
        live.yahoo_symbol("BAD SYMBOL!")


def test_quote_parses_price_change_and_wib_timestamp(monkeypatch):
    monkeypatch.setattr(live, "_http_json", lambda url, params=None, timeout=8.0: spark_payload("BBCA.JK"))
    quote = live.YahooIDXProvider().quote("BBCA")

    assert quote.ok
    assert quote.price == pytest.approx(4970.0)
    assert quote.previous_close == pytest.approx(5275.0)
    assert quote.change == pytest.approx(-305.0)
    assert quote.change_percent == pytest.approx(-305.0 / 5275.0)
    assert quote.name == "Bank Central Asia Tbk"
    assert quote.currency == "IDR"
    assert quote.quote_time is not None
    assert quote.quote_time.utcoffset() == _dt.timedelta(hours=7)
    assert quote.delayed is True
    assert quote.source_url.startswith("https://query1.finance.yahoo.com")


def test_missing_price_is_an_error_not_zero(monkeypatch):
    payload = spark_payload("BBCA.JK")
    del payload["spark"]["result"][0]["response"][0]["meta"]["regularMarketPrice"]
    monkeypatch.setattr(live, "_http_json", lambda url, params=None, timeout=8.0: payload)

    quote = live.YahooIDXProvider().quote("BBCA")
    assert quote.ok is False
    assert quote.price is None
    assert quote.change is None
    assert "no traded price" in (quote.error or "").lower()


def test_batch_falls_back_to_chart_for_missing_symbol(monkeypatch):
    calls = []

    def fake(url, params=None, timeout=8.0):
        calls.append((url, params))
        if "spark" in url:
            return spark_payload("BBCA.JK")
        return {"chart": {"result": [{"meta": {"symbol": "^JKSE", "regularMarketPrice": 5111.4, "previousClose": 5266.1, "regularMarketTime": 1793261281}}]}}

    monkeypatch.setattr(live, "_http_json", fake)
    quotes = live.YahooIDXProvider().quotes(["BBCA", "IHSG"])

    assert quotes["BBCA"].ok
    assert quotes["IHSG"].ok
    assert quotes["IHSG"].ticker == "IHSG"
    assert quotes["IHSG"].price == pytest.approx(5111.4)
    assert "finance/chart/%5EJKSE" in calls[-1][0]


def test_unknown_symbol_is_reported_not_invented(monkeypatch):
    def fake(url, params=None, timeout=8.0):
        if "spark" in url:
            return spark_payload("BBCA.JK")
        return {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}

    monkeypatch.setattr(live, "_http_json", fake)
    quotes = live.YahooIDXProvider().quotes(["BBCA", "NOSUCH"])
    assert quotes["NOSUCH"].ok is False
    assert quotes["NOSUCH"].price is None
    assert quotes["NOSUCH"].error


def test_transport_failure_surfaces_as_a_typed_error(monkeypatch):
    def boom(url, params=None, timeout=8.0):
        raise live.LiveDataUnavailable("Yahoo Finance rate-limited the request (HTTP 429).")

    monkeypatch.setattr(live, "_http_json", boom)
    with pytest.raises(live.LiveDataUnavailable, match="429"):
        live.YahooIDXProvider().quotes(["BBCA"])


def test_quotes_are_cached_for_the_ttl(monkeypatch):
    calls = {"n": 0}

    def fake(url, params=None, timeout=8.0):
        calls["n"] += 1
        return spark_payload("BBCA.JK")

    monkeypatch.setattr(live, "_http_json", fake)
    provider = live.YahooIDXProvider(quote_ttl=60)
    provider.quotes(["BBCA"])
    provider.quotes(["BBCA"])
    assert calls["n"] == 1

    live.clear_cache()
    provider.quotes(["BBCA"])
    assert calls["n"] == 2


def test_intraday_frame_skips_empty_points(monkeypatch):
    monkeypatch.setattr(
        live, "_http_json", lambda url, params=None, timeout=8.0: chart_payload("BBCA.JK")
    )
    frame = live.YahooIDXProvider().intraday("BBCA", range_="1d")
    assert list(frame.columns) == ["time", "close", "volume"]
    assert len(frame) == 2  # the None close is dropped, never forward-filled
    assert frame["close"].tolist() == [4960.0, 4970.0]


def test_daily_frame_is_deduplicated_and_sorted(monkeypatch):
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {},
                    "timestamp": [1793239200, 1793152800, 1793239200],
                    "indicators": {"quote": [{"close": [4970.0, 4900.0, 4975.0], "volume": [1, 2, 3]}]},
                }
            ]
        }
    }
    monkeypatch.setattr(live, "_http_json", lambda url, params=None, timeout=8.0: payload)
    frame = live.YahooIDXProvider().daily("BBCA", range_="5d")
    assert frame["close"].tolist() == [4900.0, 4975.0]
    assert frame["date"].is_monotonic_increasing


def test_repository_seeds_defaults_once(tmp_path: Path):
    conn = connect(tmp_path / "terminal.db")
    init_db(conn)
    repos = Repositories(conn)

    assert repos.live.seed_defaults() == len(live.DEFAULT_FEED)
    assert repos.live.seed_defaults() == 0
    symbols = repos.live.symbols()
    assert symbols[0]["ticker"] == "IHSG"
    assert symbols[0]["symbol"] == "^JKSE"
    conn.close()


def test_failed_quote_never_overwrites_the_last_good_price(tmp_path: Path, monkeypatch):
    conn = connect(tmp_path / "terminal.db")
    init_db(conn)
    repos = Repositories(conn)
    repos.live.seed_defaults([("BBCA", "BBCA.JK")])

    monkeypatch.setattr(live, "_http_json", lambda url, params=None, timeout=8.0: spark_payload("BBCA.JK"))
    good = live.YahooIDXProvider().quote("BBCA")
    assert repos.live.save_quotes([good]) == 1
    conn.commit()
    assert repos.live.latest_for("BBCA")["price"] == pytest.approx(4970.0)

    failed = live._error_quote("BBCA.JK", "Network error contacting Yahoo Finance: ConnectionError.")
    assert repos.live.save_quotes([failed]) == 0
    conn.commit()
    stored = repos.live.latest_for("BBCA")
    assert stored["price"] == pytest.approx(4970.0)
    conn.close()


def test_market_state_labels():
    meta = {"currentTradingPeriod": {"regular": {"start": 1793239200, "end": 1793265300}}}
    start = _dt.datetime.fromtimestamp(1793239200, live.WIB)
    assert live.market_state(meta, now=start + _dt.timedelta(hours=1)) == "OPEN"
    assert live.market_state(meta, now=start - _dt.timedelta(hours=1)) == "PRE-OPEN"
    assert live.market_state(meta, now=start + _dt.timedelta(days=10)) == "CLOSED"
    assert live.market_state({}, now=start) == "UNKNOWN"


def test_eod_sync_writes_companies_and_benchmark_only(tmp_path: Path, monkeypatch):
    conn = connect(tmp_path / "terminal.db")
    init_db(conn)
    repos = Repositories(conn)
    repos.companies.upsert("BBCA", name="Bank Central Asia", sector="Financials")
    repos.live.seed_defaults([("BBCA", "BBCA.JK"), ("IHSG", "^JKSE"), ("ZZZZ", "ZZZZ.JK")])
    conn.commit()

    calls = []

    def fake_daily(symbol, range_="1y"):
        import pandas as pd

        calls.append((symbol, range_))
        if symbol == "ZZZZ.JK":
            raise live.LiveDataUnavailable("Yahoo Finance returned HTTP 404.")
        return pd.DataFrame(
            {
                "date": [_dt.date(2026, 10, 28), _dt.date(2026, 10, 29)],
                "close": [4900.0, 4960.0],
                "volume": [1000, 2000],
            }
        )

    monkeypatch.setattr(live.YahooIDXProvider, "daily", lambda self, symbol, range_="1y": fake_daily(symbol, range_))
    from src.data import live_sync

    price_rows, benchmark_rows, errors = live_sync.sync_eod(repos)

    assert price_rows == 2  # BBCA only: ZZZZ has no company row
    assert benchmark_rows == 2  # IHSG goes to the benchmark series
    assert errors and "ZZZZ" in errors[0]
    assert repos.prices.latest("BBCA") is not None
    assert len(repos.benchmarks.series("IHSG")) == 2
    assert not repos.prices.latest("ZZZZ")
    conn.close()

