"""Tests for the historical data layer.

Every payload here is hand built in memory. Nothing in this file opens a socket:
the only transport is ``FakeGet``, and ``RequestsGet`` is never constructed.

The things being defended:
* the Coinbase column order (low/high/open/close, not open/high/low/close),
* the 300-candle window limit, asserted as an invariant over every recorded call
  rather than as a call count, because a call count passes for an off-by-one too,
* gaps are reported and never filled,
* the cache cannot be made to write outside its directory and cannot leave a
  half-written file behind,
* the frame shape ``fulcrum.features`` consumes.
"""

from __future__ import annotations

import io
import json
import math
import os
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from fulcrum.data import (
    BarCache,
    BinanceVisionKlines,
    CoinbaseCandles,
    DailyHistory,
    DataError,
    FakeGet,
    HistoricalSource,
    SymbolError,
    compare,
    liquidity_screen,
    newest_bar_age_minutes,
    to_binance,
    to_coinbase,
    to_frame,
)
from fulcrum.models import Bar

UTC = timezone.utc
COINBASE_PREFIX = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
BINANCE_PREFIX = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def cb_row(d: date, *, low: float, high: float, open_: float, close: float, volume: float) -> list:
    """One Coinbase candle: [time, low, high, open, close, volume]."""
    return [epoch(d), low, high, open_, close, volume]


def cb_series(days: list[date], *, base: float = 100.0) -> list[list]:
    """Descending (newest first), which is how Coinbase serves candles."""
    rows = []
    for i, d in enumerate(days):
        p = base + i
        rows.append(cb_row(d, low=p - 2, high=p + 3, open_=p, close=p + 1, volume=1000.0 + i))
    return list(reversed(rows))


def json_body(rows: list) -> tuple[int, bytes, dict]:
    return 200, json.dumps(rows).encode("utf-8"), {"Content-Type": "application/json"}


def parse_window(params: dict) -> tuple[date, date]:
    s = datetime.fromisoformat(params["start"].replace("Z", "+00:00")).date()
    e = datetime.fromisoformat(params["end"].replace("Z", "+00:00")).date()
    return s, e


def bar(d: date, close: float, *, symbol: str = "BTC/USD", volume: float = 1.0) -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime(d.year, d.month, d.day, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
    )


def binance_zip(rows: list[list], *, header: bool = False, name: str = "BTCUSDT-1d-2024-01.csv") -> bytes:
    """Build a Binance Vision monthly zip in memory."""
    buf = io.StringIO()
    if header:
        buf.write(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n"
        )
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, buf.getvalue())
    return out.getvalue()


def bn_row(d: date, *, open_: float, high: float, low: float, close: float, volume: float) -> list:
    """One Binance kline: open time ms, o, h, l, c, v, close time, ... (12 columns)."""
    ms = epoch(d) * 1000
    return [ms, open_, high, low, close, volume, ms + 86_399_999, 0, 0, 0, 0, 0]


class ScriptedSource:
    """A ten line DailySource for HistoricalSource tests: no HTTP anywhere."""

    def __init__(self, source_name: str, bars: list[Bar] | None = None, *, raises: Exception | None = None):
        self.source_name = source_name
        self._bars = bars or []
        self._raises = raises
        self.calls = 0

    def daily_bars(self, symbol, start, end):
        return self.daily_bars_with_gaps(symbol, start, end)[0]

    def daily_bars_with_gaps(self, symbol, start, end):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        bars = [b for b in self._bars if start <= b.ts.date() <= end]
        present = {b.ts.date() for b in bars}
        missing, day = [], start
        while day <= end:
            if day not in present:
                missing.append(day)
            day += timedelta(days=1)
        return bars, missing


# --------------------------------------------------------------------------------------
# symbol mapping
# --------------------------------------------------------------------------------------


def test_symbol_mapping():
    assert to_coinbase("BTC/USD") == "BTC-USD"
    assert to_coinbase("ETH/USD") == "ETH-USD"
    assert to_binance("BTC/USD") == "BTCUSDT"
    assert to_binance("SOL/USD") == "SOLUSDT"


@pytest.mark.parametrize(
    "bad",
    ["../../evil", "BTC/../../USD", "BTC\\USD", "C:\\evil", "BTC", "", "btc/usd", "BTC/USD/EXTRA", "/USD", "BTC/"],
)
def test_symbol_mapping_rejects_path_shaped_input(bad):
    """A symbol becomes a URL path segment and a filename. One gate, applied everywhere."""
    with pytest.raises(SymbolError):
        to_coinbase(bad)
    with pytest.raises(SymbolError):
        to_binance(bad)


# --------------------------------------------------------------------------------------
# Coinbase
# --------------------------------------------------------------------------------------


def test_coinbase_column_mapping_against_a_hand_written_row():
    """Coinbase is [time, low, high, open, close, volume]. Four distinct values so a
    transposition cannot pass by coincidence."""
    row = [epoch(date(2024, 3, 1)), 10.0, 20.0, 12.0, 18.0, 555.5]
    http = FakeGet({COINBASE_PREFIX: json_body([row])})
    src = CoinbaseCandles(http, sleep=lambda _s: None)

    bars = src.daily_bars("BTC/USD", date(2024, 3, 1), date(2024, 3, 1))

    assert len(bars) == 1
    b = bars[0]
    assert b.ts == datetime(2024, 3, 1, tzinfo=UTC)
    assert b.low == 10.0
    assert b.high == 20.0
    assert b.open == 12.0
    assert b.close == 18.0
    assert b.volume == 555.5
    assert b.symbol == "BTC/USD"


def test_coinbase_transposed_row_is_rejected_not_absorbed():
    """If the mapping were ever flipped to o/h/l/c, Bar's own OHLC check catches it."""
    bad = [epoch(date(2024, 3, 1)), 12.0, 18.0, 10.0, 20.0, 1.0]  # close 20 > high 18
    http = FakeGet({COINBASE_PREFIX: json_body([bad])})
    src = CoinbaseCandles(http, sleep=lambda _s: None)
    with pytest.raises(ValueError, match="OHLC inconsistent"):
        src.daily_bars("BTC/USD", date(2024, 3, 1), date(2024, 3, 1))


def test_coinbase_700_day_range_makes_three_calls_with_correct_windows():
    start, end = date(2023, 1, 1), date(2024, 11, 30)  # 700 days inclusive
    assert (end - start).days + 1 == 700

    seen: list[tuple[date, date]] = []

    def responder(url, params):
        s, e = parse_window(params)
        seen.append((s, e))
        days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        return json_body(cb_series(days))

    http = FakeGet({COINBASE_PREFIX: responder})
    src = CoinbaseCandles(http, sleep=lambda _s: None)
    bars = src.daily_bars("BTC/USD", start, end)

    assert len(http.calls) == 3, http.urls

    # The invariant D-011 actually asks for. A call count of 3 passes for an
    # off-by-one that asks for 301 candles; this does not.
    for s, e in seen:
        assert (e - s).days + 1 <= CoinbaseCandles.MAX_CANDLES, f"window {s}..{e} exceeds 300 candles"

    # Contiguous, no overlap, exactly covering the request.
    assert seen[0][0] == start
    assert seen[-1][1] == end
    for (_s0, e0), (s1, _e1) in zip(seen, seen[1:]):
        assert s1 == e0 + timedelta(days=1)

    # granularity is always daily
    for _url, params, _timeout in http.calls:
        assert params["granularity"] == 86400

    assert len(bars) == 700
    assert bars[0].ts.date() == start
    assert bars[-1].ts.date() == end


def test_coinbase_output_is_ascending_and_deduplicated_across_windows():
    """Coinbase serves newest first, and windows can overlap after a retry. The
    assembled series must still be ascending with one bar per day."""
    start, end = date(2024, 1, 1), date(2024, 1, 5)
    days = [start + timedelta(days=i) for i in range(5)]
    rows = cb_series(days)  # descending
    rows = rows + [cb_row(date(2024, 1, 3), low=1.0, high=9.0, open_=2.0, close=8.0, volume=42.0)]

    http = FakeGet({COINBASE_PREFIX: json_body(rows)})
    src = CoinbaseCandles(http, sleep=lambda _s: None)
    bars = src.daily_bars("BTC/USD", start, end)

    assert [b.ts.date() for b in bars] == days
    # last writer wins on a duplicate timestamp: a refetch is a correction
    third = next(b for b in bars if b.ts.date() == date(2024, 1, 3))
    assert third.close == 8.0 and third.volume == 42.0


def test_coinbase_gap_detection_finds_a_removed_day_and_does_not_fill_it():
    start, end = date(2024, 1, 1), date(2024, 1, 5)
    days = [start + timedelta(days=i) for i in range(5)]
    rows = [r for r in cb_series(days) if r[0] != epoch(date(2024, 1, 3))]

    http = FakeGet({COINBASE_PREFIX: json_body(rows)})
    src = CoinbaseCandles(http, sleep=lambda _s: None)
    bars, missing = src.daily_bars_with_gaps("BTC/USD", start, end)

    assert missing == [date(2024, 1, 3)]
    assert len(bars) == 4
    assert date(2024, 1, 3) not in [b.ts.date() for b in bars]


def test_coinbase_clips_to_the_requested_range():
    """Whatever the venue means by an inclusive endpoint, the output is clipped here."""
    days = [date(2023, 12, 30), date(2023, 12, 31), date(2024, 1, 1), date(2024, 1, 2)]
    http = FakeGet({COINBASE_PREFIX: json_body(cb_series(days))})
    src = CoinbaseCandles(http, sleep=lambda _s: None)

    bars = src.daily_bars("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))

    assert [b.ts.date() for b in bars] == [date(2024, 1, 1), date(2024, 1, 2)]


def test_coinbase_paces_at_ten_requests_per_second():
    start, end = date(2023, 1, 1), date(2024, 11, 30)
    slept: list[float] = []

    def responder(url, params):
        s, e = parse_window(params)
        return json_body(cb_series([s + timedelta(days=i) for i in range((e - s).days + 1)]))

    http = FakeGet({COINBASE_PREFIX: responder})
    src = CoinbaseCandles(http, sleep=slept.append, requests_per_second=10.0)
    src.daily_bars("BTC/USD", start, end)

    assert len(http.calls) == 3
    # one sleep between consecutive requests, none before the first
    assert slept == [0.1, 0.1]
    assert all(s == pytest.approx(1 / 10.0) for s in slept)


def test_coinbase_single_window_does_not_sleep():
    http = FakeGet({COINBASE_PREFIX: json_body(cb_series([date(2024, 1, 1)]))})
    slept: list[float] = []
    src = CoinbaseCandles(http, sleep=slept.append)
    src.daily_bars("BTC/USD", date(2024, 1, 1), date(2024, 1, 1))
    assert slept == []


def test_coinbase_window_split_is_exactly_300_at_the_boundary():
    """299 days inclusive is one call; 301 is two. The edge the off-by-one lives on."""
    s = date(2024, 1, 1)
    assert CoinbaseCandles.windows(s, s + timedelta(days=299)) == [(s, s + timedelta(days=299))]
    two = CoinbaseCandles.windows(s, s + timedelta(days=300))
    assert len(two) == 2
    assert two[0] == (s, s + timedelta(days=299))
    assert two[1] == (s + timedelta(days=300), s + timedelta(days=300))
    for a, b in CoinbaseCandles.windows(date(2017, 1, 1), date(2026, 1, 1)):
        assert (b - a).days + 1 <= 300


def test_coinbase_http_error_raises_data_error():
    http = FakeGet({COINBASE_PREFIX: (429, b"slow down", {})})
    src = CoinbaseCandles(http, sleep=lambda _s: None)
    with pytest.raises(DataError, match="429"):
        src.daily_bars("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))


def test_coinbase_rejects_reversed_range():
    src = CoinbaseCandles(FakeGet(), sleep=lambda _s: None)
    with pytest.raises(ValueError, match="before start"):
        src.daily_bars("BTC/USD", date(2024, 2, 1), date(2024, 1, 1))


# --------------------------------------------------------------------------------------
# Binance Vision
# --------------------------------------------------------------------------------------


def test_binance_zip_parsing_from_an_in_memory_zip():
    """Binance is open/high/low/close, the reverse of Coinbase. Distinct values again."""
    rows = [bn_row(date(2024, 1, 1), open_=12.0, high=20.0, low=10.0, close=18.0, volume=777.0)]
    http = FakeGet({BINANCE_PREFIX: (200, binance_zip(rows), {})})
    src = BinanceVisionKlines(http, sleep=lambda _s: None)

    bars = src.daily_bars("BTC/USD", date(2024, 1, 1), date(2024, 1, 31))

    assert len(bars) == 1
    b = bars[0]
    assert b.ts == datetime(2024, 1, 1, tzinfo=UTC)
    assert (b.open, b.high, b.low, b.close, b.volume) == (12.0, 20.0, 10.0, 18.0, 777.0)
    assert b.symbol == "BTC/USD"  # canonical symbol, not BTCUSDT
    assert src.missing_months == []


def test_binance_header_row_is_skipped():
    rows = [
        bn_row(date(2024, 1, 1), open_=12.0, high=20.0, low=10.0, close=18.0, volume=1.0),
        bn_row(date(2024, 1, 2), open_=13.0, high=21.0, low=11.0, close=19.0, volume=2.0),
    ]
    http = FakeGet({BINANCE_PREFIX: (200, binance_zip(rows, header=True), {})})
    src = BinanceVisionKlines(http, sleep=lambda _s: None)

    bars = src.daily_bars("BTC/USD", date(2024, 1, 1), date(2024, 1, 31))

    assert [b.ts.date() for b in bars] == [date(2024, 1, 1), date(2024, 1, 2)]
    assert bars[1].close == 19.0


def test_binance_404_month_is_recorded_as_missing_and_does_not_raise():
    """A 404 means the symbol was not listed yet. Not an error: the later months still count."""
    rows = [bn_row(date(2024, 2, 1), open_=12.0, high=20.0, low=10.0, close=18.0, volume=1.0)]
    feb_url = BINANCE_PREFIX + "BTCUSDT-1d-2024-02.zip"
    http = FakeGet({feb_url: (200, binance_zip(rows, name="BTCUSDT-1d-2024-02.csv"), {})})
    src = BinanceVisionKlines(http, sleep=lambda _s: None)

    bars, missing = src.daily_bars_with_gaps("BTC/USD", date(2024, 1, 1), date(2024, 2, 2))

    assert src.missing_months == ["2024-01"]  # January 404s, unmapped in FakeGet
    assert [b.ts.date() for b in bars] == [date(2024, 2, 1)]
    # every absent calendar day is reported, and none of them is filled
    assert date(2024, 1, 15) in missing
    assert date(2024, 2, 2) in missing
    assert date(2024, 2, 1) not in missing
    assert len(missing) == 32  # 31 January days plus Feb 2; only Feb 1 was served


def test_binance_quote_currency_caveat_is_exposed_on_the_class():
    """The report has to be able to say 'this series is USDT, not USD'."""
    assert BinanceVisionKlines.quote_currency == "USDT"
    assert CoinbaseCandles.quote_currency == "USD"
    assert BinanceVisionKlines.source_name == "binance_vision"
    assert CoinbaseCandles.source_name == "coinbase"


def test_binance_month_urls_are_built_per_month():
    http = FakeGet()
    src = BinanceVisionKlines(http, sleep=lambda _s: None)
    src.daily_bars("BTC/USD", date(2023, 11, 15), date(2024, 1, 10))
    assert http.urls == [
        BINANCE_PREFIX + "BTCUSDT-1d-2023-11.zip",
        BINANCE_PREFIX + "BTCUSDT-1d-2023-12.zip",
        BINANCE_PREFIX + "BTCUSDT-1d-2024-01.zip",
    ]
    assert src.missing_months == ["2023-11", "2023-12", "2024-01"]


def test_binance_paces_between_month_downloads():
    """The Binance Vision rate limit is NOT documented. The pacing is a deliberate
    self-imposed courtesy limit, so the test pins the behavior that exists rather than
    a published number."""
    slept: list[float] = []
    http = FakeGet()
    src = BinanceVisionKlines(http, sleep=slept.append, requests_per_second=10.0)
    src.daily_bars("BTC/USD", date(2023, 11, 15), date(2024, 1, 10))

    assert len(http.calls) == 3
    assert slept == [0.1, 0.1]  # no sleep before the first request


def test_binance_non_404_error_raises():
    http = FakeGet({BINANCE_PREFIX: (500, b"", {})})
    src = BinanceVisionKlines(http, sleep=lambda _s: None)
    with pytest.raises(DataError, match="500"):
        src.daily_bars("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))


def test_binance_epoch_units_are_detected_by_magnitude():
    """The current Binance timestamp unit cannot be verified offline, so the parser is
    defensive: seconds, milliseconds and microseconds all land on the same day."""
    d = date(2024, 1, 1)
    secs = epoch(d)
    for scale, digits in ((1, "s"), (1000, "ms"), (1_000_000, "us")):
        row = [secs * scale, 12.0, 20.0, 10.0, 18.0, 1.0, 0, 0, 0, 0, 0, 0]
        http = FakeGet({BINANCE_PREFIX: (200, binance_zip([row]), {})})
        src = BinanceVisionKlines(http, sleep=lambda _s: None)
        bars = src.daily_bars("BTC/USD", d, d)
        assert [b.ts for b in bars] == [datetime(2024, 1, 1, tzinfo=UTC)], digits


# --------------------------------------------------------------------------------------
# BarCache
# --------------------------------------------------------------------------------------


def test_bar_cache_round_trip_is_exact(tmp_path):
    cache = BarCache(tmp_path / "bars")
    bars = [
        Bar("BTC/USD", datetime(2024, 1, 1, tzinfo=UTC), 0.1 + 0.2, 1.7976931348623157e50, 0.1, 1 / 3, 12345.678901),
        Bar("BTC/USD", datetime(2024, 1, 2, tzinfo=UTC), 100.0, 110.0, 90.0, 105.0, 1.0),
    ]
    cache.store("coinbase", "BTC/USD", bars)
    loaded = cache.load("coinbase", "BTC/USD")

    assert len(loaded) == 2
    for a, b in zip(bars, loaded):
        assert a.ts == b.ts
        assert a.open == b.open  # exact, not approx: repr round trips floats losslessly
        assert a.high == b.high
        assert a.low == b.low
        assert a.close == b.close
        assert a.volume == b.volume


def test_bar_cache_header_and_columns(tmp_path):
    cache = BarCache(tmp_path / "bars")
    cache.store("coinbase", "BTC/USD", [bar(date(2024, 1, 1), 100.0)])
    p = cache.path_for("coinbase", "BTC/USD")
    first_line = p.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "ts_iso,open,high,low,close,volume"


def test_bar_cache_merges_by_timestamp_new_wins(tmp_path):
    cache = BarCache(tmp_path / "bars")
    a = bar(date(2024, 1, 1), 100.0)
    b_old = bar(date(2024, 1, 2), 200.0)
    b_new = bar(date(2024, 1, 2), 222.0, volume=9.0)
    c = bar(date(2024, 1, 3), 300.0)

    cache.store("coinbase", "BTC/USD", [a, b_old])
    cache.store("coinbase", "BTC/USD", [b_new, c])
    loaded = cache.load("coinbase", "BTC/USD")

    assert [x.ts.date() for x in loaded] == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
    second = loaded[1]
    assert second.close == 222.0 and second.volume == 9.0  # the correction won


def test_bar_cache_rewrite_is_atomic_and_leaves_no_partial_file(tmp_path, monkeypatch):
    cache = BarCache(tmp_path / "bars")
    original = [bar(date(2024, 1, 1), 100.0)]
    cache.store("coinbase", "BTC/USD", original)
    p = cache.path_for("coinbase", "BTC/USD")
    before = p.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        cache.store("coinbase", "BTC/USD", [bar(date(2024, 1, 2), 999.0)])

    assert p.read_text(encoding="utf-8") == before  # old file untouched
    assert list(cache.cache_dir.glob("*.tmp")) == []  # temp cleaned up
    monkeypatch.undo()
    assert [b.close for b in cache.load("coinbase", "BTC/USD")] == [100.0]


@pytest.mark.parametrize("bad", ["../../evil", "BTC/../../USD", "BTC\\USD", "C:\\evil", "BTC", "btc/usd"])
def test_bar_cache_never_writes_outside_cache_dir(tmp_path, bad):
    cache = BarCache(tmp_path / "bars")
    with pytest.raises(SymbolError):
        cache.path_for("coinbase", bad)
    with pytest.raises(SymbolError):
        cache.store("coinbase", bad, [])
    # nothing escaped: the only thing under tmp_path is (at most) the cache dir itself
    escaped = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert escaped == []


def test_bar_cache_rejects_unsafe_source_name(tmp_path):
    cache = BarCache(tmp_path / "bars")
    for bad in ["../coinbase", "Coinbase/x", "co inbase", ""]:
        with pytest.raises(SymbolError):
            cache.path_for(bad, "BTC/USD")


def test_bar_cache_path_stays_inside_resolved_cache_dir(tmp_path):
    cache = BarCache(tmp_path / "bars")
    p = cache.path_for("coinbase", "BTC/USD")
    assert p.parent == (tmp_path / "bars").resolve()
    assert p.name == "coinbase__BTC-USD.csv"


def test_bar_cache_load_missing_file_is_empty(tmp_path):
    assert BarCache(tmp_path / "bars").load("coinbase", "BTC/USD") == []


# --------------------------------------------------------------------------------------
# HistoricalSource
# --------------------------------------------------------------------------------------


def test_historical_source_cache_hit_makes_no_http_call(tmp_path):
    cache = BarCache(tmp_path / "bars")
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(5)]
    cache.store("coinbase", "BTC/USD", [bar(d, 100.0 + i) for i, d in enumerate(days)])

    http = FakeGet()  # any call would 404 and be visible in http.calls
    primary = CoinbaseCandles(http, sleep=lambda _s: None)
    fallback = BinanceVisionKlines(http, sleep=lambda _s: None)
    hs = HistoricalSource(primary, fallback, cache)

    hist = hs.get_daily("BTC/USD", date(2024, 1, 2), date(2024, 1, 4))

    assert http.calls == []
    assert hist.from_cache is True
    assert hist.source_used == "coinbase"
    assert [b.ts.date() for b in hist.bars] == days[1:4]
    assert hist.missing_dates == []


def test_historical_source_cache_miss_when_range_not_covered(tmp_path):
    cache = BarCache(tmp_path / "bars")
    cache.store("coinbase", "BTC/USD", [bar(date(2024, 1, 2), 100.0)])
    primary = ScriptedSource("coinbase", [bar(d, 100.0) for d in [date(2024, 1, 1), date(2024, 1, 2)]])
    hs = HistoricalSource(primary, None, cache)

    hist = hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))

    assert hist.from_cache is False
    assert primary.calls == 1


def test_historical_source_cache_hit_still_reports_interior_gaps(tmp_path):
    """An interior gap is real. It must not force a refetch, and it must still be
    reported from the cache path exactly as a fresh fetch would report it."""
    cache = BarCache(tmp_path / "bars")
    cache.store(
        "coinbase",
        "BTC/USD",
        [bar(date(2024, 1, 1), 100.0), bar(date(2024, 1, 3), 102.0)],
    )
    primary = ScriptedSource("coinbase", [])
    hs = HistoricalSource(primary, None, cache)

    hist = hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 3))

    assert hist.from_cache is True
    assert primary.calls == 0
    assert hist.missing_dates == [date(2024, 1, 2)]


def test_historical_source_falls_back_when_primary_raises(tmp_path):
    cache = BarCache(tmp_path / "bars")
    primary = ScriptedSource("coinbase", raises=DataError("coinbase is down"))
    fallback = ScriptedSource("binance_vision", [bar(date(2024, 1, 1), 100.0), bar(date(2024, 1, 2), 101.0)])
    hs = HistoricalSource(primary, fallback, cache)

    hist = hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))

    assert hist.source_used == "binance_vision"
    assert hist.from_cache is False
    assert len(hist.bars) == 2
    assert any("coinbase is down" in e for e in hs.last_errors)
    # stored under the source that actually answered, never under the primary's name
    assert cache.load("binance_vision", "BTC/USD") != []
    assert cache.load("coinbase", "BTC/USD") == []


def test_historical_source_falls_back_when_primary_returns_nothing(tmp_path):
    cache = BarCache(tmp_path / "bars")
    primary = ScriptedSource("coinbase", [])
    fallback = ScriptedSource("binance_vision", [bar(date(2024, 1, 1), 100.0)])
    hs = HistoricalSource(primary, fallback, cache)

    hist = hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 1))

    assert hist.source_used == "binance_vision"
    assert primary.calls == 1 and fallback.calls == 1


def test_historical_source_both_sources_empty_returns_empty_not_an_exception(tmp_path):
    cache = BarCache(tmp_path / "bars")
    hs = HistoricalSource(ScriptedSource("coinbase", []), ScriptedSource("binance_vision", []), cache)
    hist = hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 3))
    assert hist.bars == []
    assert hist.source_used == "none"
    assert len(hist.missing_dates) == 3
    assert len(hs.last_errors) == 2


def test_historical_source_stores_what_it_fetched(tmp_path):
    cache = BarCache(tmp_path / "bars")
    primary = ScriptedSource("coinbase", [bar(date(2024, 1, 1), 100.0), bar(date(2024, 1, 2), 101.0)])
    hs = HistoricalSource(primary, None, cache)

    hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))
    again = hs.get_daily("BTC/USD", date(2024, 1, 1), date(2024, 1, 2))

    assert again.from_cache is True
    assert primary.calls == 1  # the second call was served from disk


def test_compare_bps_math_on_a_hand_example():
    """Odd count so median is a real element, not an average of two."""
    primary = [bar(date(2024, 1, 1), 100.0), bar(date(2024, 1, 2), 200.0), bar(date(2024, 1, 3), 50.0)]
    fallback = [
        bar(date(2024, 1, 1), 100.1),  # +0.1 / 100   =  10 bps
        bar(date(2024, 1, 2), 199.0),  # -1.0 / 200   =  50 bps
        bar(date(2024, 1, 3), 50.05),  # +0.05 / 50   =  10 bps
        bar(date(2024, 1, 4), 999.0),  # no overlap
    ]
    out = compare(primary, fallback)

    assert out["n_overlap"] == 3
    assert out["median_abs_bps"] == pytest.approx(10.0)
    assert out["max_abs_bps"] == pytest.approx(50.0)
    assert out["worst_day"] == date(2024, 1, 2)
    assert out["first_day"] == date(2024, 1, 1)
    assert out["last_day"] == date(2024, 1, 3)


def test_compare_even_count_averages_the_middle_two():
    primary = [bar(date(2024, 1, 1), 100.0), bar(date(2024, 1, 2), 100.0)]
    fallback = [bar(date(2024, 1, 1), 100.1), bar(date(2024, 1, 2), 100.3)]  # 10 and 30 bps
    out = compare(primary, fallback)
    assert out["n_overlap"] == 2
    assert out["median_abs_bps"] == pytest.approx(20.0)


def test_compare_with_no_overlap_reports_none_not_zero():
    out = compare([bar(date(2024, 1, 1), 100.0)], [bar(date(2024, 6, 1), 100.0)])
    assert out["n_overlap"] == 0
    assert out["median_abs_bps"] is None
    assert out["max_abs_bps"] is None


def test_compare_is_reachable_from_historical_source(tmp_path):
    hs = HistoricalSource(ScriptedSource("coinbase", []), None, BarCache(tmp_path / "bars"))
    assert hs.compare([bar(date(2024, 1, 1), 100.0)], [bar(date(2024, 1, 1), 100.0)])["n_overlap"] == 1


# --------------------------------------------------------------------------------------
# to_frame
# --------------------------------------------------------------------------------------


def test_to_frame_shape_matches_what_features_expects():
    bars = [
        Bar("BTC/USD", datetime(2024, 1, 2, tzinfo=UTC), 12.0, 20.0, 10.0, 18.0, 5.0),
        Bar("BTC/USD", datetime(2024, 1, 1, tzinfo=UTC), 11.0, 19.0, 9.0, 17.0, 4.0),
    ]
    df = to_frame(bars)

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing  # sorted even though input was not
    assert not df.index.has_duplicates
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert df.loc[pd.Timestamp("2024-01-02", tz="UTC"), "close"] == 18.0
    assert df.loc[pd.Timestamp("2024-01-02", tz="UTC"), "low"] == 10.0


def test_to_frame_feeds_compute_features_without_complaint():
    """The shape contract is only real if the consumer accepts it."""
    from fulcrum.config import SignalConfig
    from fulcrum.features import compute_features

    cfg = SignalConfig(lookbacks_days=(7, 14), trend_filter_ma_days=5, vol_window_days=5, atr_window_days=5)
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(40)]
    bars = [
        Bar("BTC/USD", datetime(d.year, d.month, d.day, tzinfo=UTC), 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0)
        for i, d in enumerate(days)
    ]
    out = compute_features(to_frame(bars), cfg, 365)
    assert len(out) == 40
    assert "ret_7" in out.columns and "sma_5" in out.columns


def test_to_frame_raises_on_duplicate_timestamps():
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [
        Bar("BTC/USD", ts, 12.0, 20.0, 10.0, 18.0, 5.0),
        Bar("BTC/USD", ts, 13.0, 21.0, 11.0, 19.0, 6.0),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        to_frame(bars)


def test_to_frame_raises_on_mixed_symbols():
    bars = [bar(date(2024, 1, 1), 100.0, symbol="BTC/USD"), bar(date(2024, 1, 2), 100.0, symbol="ETH/USD")]
    with pytest.raises(ValueError, match="one symbol"):
        to_frame(bars)


def test_to_frame_empty_is_an_empty_frame_with_the_right_shape():
    df = to_frame([])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 0
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"


# --------------------------------------------------------------------------------------
# liquidity screen
# --------------------------------------------------------------------------------------


def history(days: int, *, close: float, volume: float, symbol: str = "BTC/USD") -> DailyHistory:
    start = date(2024, 1, 1)
    bars = [bar(start + timedelta(days=i), close, symbol=symbol, volume=volume) for i in range(days)]
    return DailyHistory(bars=bars, missing_dates=[], source_used="coinbase", from_cache=False)


def test_liquidity_screen_passes_and_rejects_with_reasons():
    histories = {
        "BTC/USD": history(40, close=100.0, volume=10_000_000.0),  # 1e9 median
        "ETH/USD": history(40, close=10.0, volume=1_000.0, symbol="ETH/USD"),  # 1e4 median
        "SOL/USD": history(5, close=100.0, volume=10_000_000.0, symbol="SOL/USD"),  # too short
    }
    passing, rejected = liquidity_screen(
        ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"], histories, 50_000_000.0, lookback_days=30
    )

    assert passing == ["BTC/USD"]
    reasons = dict(rejected)
    assert set(reasons) == {"ETH/USD", "SOL/USD", "DOGE/USD"}
    assert "not above" in reasons["ETH/USD"]
    assert "only 5 bars, need 30" in reasons["SOL/USD"]
    assert reasons["DOGE/USD"] == "no history"


def test_liquidity_screen_preserves_input_order():
    histories = {s: history(30, close=100.0, volume=10_000_000.0, symbol=s) for s in ["C/USD", "A/USD", "B/USD"]}
    passing, rejected = liquidity_screen(["C/USD", "A/USD", "B/USD"], histories, 1.0)
    assert passing == ["C/USD", "A/USD", "B/USD"]
    assert rejected == []


def test_liquidity_screen_threshold_is_strictly_greater():
    histories = {"BTC/USD": history(30, close=100.0, volume=10.0)}  # median exactly 1000
    passing, rejected = liquidity_screen(["BTC/USD"], histories, 1000.0)
    assert passing == []
    assert len(rejected) == 1
    passing, _ = liquidity_screen(["BTC/USD"], histories, 999.99)
    assert passing == ["BTC/USD"]


def test_liquidity_screen_uses_only_the_lookback_window():
    """Old glory does not qualify a coin that has gone quiet."""
    start = date(2024, 1, 1)
    loud = [bar(start + timedelta(days=i), 100.0, volume=10_000_000.0) for i in range(30)]
    quiet = [bar(start + timedelta(days=30 + i), 100.0, volume=1.0) for i in range(30)]
    hist = DailyHistory(bars=loud + quiet, missing_dates=[], source_used="coinbase", from_cache=False)
    passing, rejected = liquidity_screen(["BTC/USD"], {"BTC/USD": hist}, 50_000_000.0, lookback_days=30)
    assert passing == []
    assert "not above" in dict(rejected)["BTC/USD"]


def test_liquidity_screen_is_pure():
    histories = {"BTC/USD": history(30, close=100.0, volume=10_000_000.0)}
    before = [(b.ts, b.close, b.volume) for b in histories["BTC/USD"].bars]
    liquidity_screen(["BTC/USD"], histories, 1.0)
    liquidity_screen(["BTC/USD"], histories, 1.0)
    after = [(b.ts, b.close, b.volume) for b in histories["BTC/USD"].bars]
    assert before == after


# --------------------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------------------


def test_newest_bar_age_uses_the_bar_close_not_its_open():
    bars = [bar(date(2024, 1, 1), 100.0), bar(date(2024, 1, 2), 101.0)]
    # the 2024-01-02 bar closes at 2024-01-03T00:00Z
    now = datetime(2024, 1, 3, 0, 30, tzinfo=UTC)
    assert newest_bar_age_minutes(bars, now) == pytest.approx(30.0)


def test_newest_bar_age_is_negative_while_the_bar_is_still_forming():
    """Not clamped: the stale-data guard must be able to tell 'not closed yet' from
    'closed this instant'."""
    bars = [bar(date(2024, 1, 2), 101.0)]
    now = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)
    assert newest_bar_age_minutes(bars, now) == pytest.approx(-12 * 60.0)


def test_newest_bar_age_ignores_ordering():
    bars = [bar(date(2024, 1, 2), 101.0), bar(date(2024, 1, 1), 100.0)]
    now = datetime(2024, 1, 3, 0, 0, tzinfo=UTC)
    assert newest_bar_age_minutes(bars, now) == pytest.approx(0.0)


def test_newest_bar_age_with_no_bars_is_infinite():
    assert newest_bar_age_minutes([], datetime(2024, 1, 3, tzinfo=UTC)) == math.inf


def test_newest_bar_age_requires_aware_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        newest_bar_age_minutes([bar(date(2024, 1, 1), 100.0)], datetime(2024, 1, 3))


def test_newest_bar_age_against_the_configured_stale_threshold(config):
    """Sanity: the number this returns is in the units risk.stale_bar_max_age_minutes uses."""
    bars = [bar(date(2024, 1, 2), 101.0)]
    fresh = datetime(2024, 1, 3, 1, 0, tzinfo=UTC)
    stale = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)
    limit = config.risk.stale_bar_max_age_minutes
    assert newest_bar_age_minutes(bars, fresh) <= limit
    assert newest_bar_age_minutes(bars, stale) > limit


# --------------------------------------------------------------------------------------
# no network
# --------------------------------------------------------------------------------------


def test_fake_get_is_the_only_transport_used_here():
    """Guard against a future edit quietly introducing the real transport."""
    import fulcrum.data as data_mod

    src = CoinbaseCandles(FakeGet(), sleep=lambda _s: None)
    assert isinstance(src._http, FakeGet)
    assert not isinstance(src._http, data_mod.RequestsGet)


def test_fake_get_records_calls_and_longest_prefix_wins():
    http = FakeGet({"https://x/": (200, b"generic", {}), "https://x/special": (200, b"special", {})})
    assert http.get("https://x/special/thing", params={"a": 1}, timeout=5.0)[1] == b"special"
    assert http.get("https://x/other", params=None, timeout=5.0)[1] == b"generic"
    assert http.get("https://y/", params=None, timeout=5.0)[0] == 404
    assert len(http.calls) == 3
    assert http.calls[0] == ("https://x/special/thing", {"a": 1}, 5.0)
