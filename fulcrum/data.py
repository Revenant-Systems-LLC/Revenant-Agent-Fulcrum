"""Historical data layer: backtest bars and the universe liquidity screen.

Everything that talks to the outside world goes through one seam, ``HttpGet``.
``RequestsGet`` is the only implementation that opens a socket, and nothing in the
test suite constructs it. Tests inject ``FakeGet``, which serves scripted bytes.

Sources (see docs/DECISIONS.md D-011):
* ``CoinbaseCandles``  : primary. True USD quotes, 300 candles per request, 10 rps.
* ``BinanceVisionKlines``: fallback and cross-check. USDT quotes, monthly zip files.

Two rules run through the whole module:

1. A GAP IS NEVER FILLED. Missing calendar days are detected, returned, and
   reported. No forward fill, no interpolation, no synthetic bar. Coinbase's own
   docs warn that historical rate data may be incomplete, so a silently repaired
   series would be a lie the backtest would then price.
2. A SYMBOL IS NEVER A RAW PATH SEGMENT. Every symbol passes ``parse_symbol``
   before it becomes part of a URL or a filename, and ``BarCache`` additionally
   proves the resolved path's parent is the cache directory.

Bar frames handed to ``fulcrum.features`` have index = tz-aware UTC DatetimeIndex
and columns open, high, low, close, volume. That shape is the contract; see
``to_frame``.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import statistics
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

import pandas as pd

from .models import Bar

# --------------------------------------------------------------------------------------
# Transport seam
# --------------------------------------------------------------------------------------

HttpResponse = tuple[int, bytes, dict]

DEFAULT_TIMEOUT = 20.0
USER_AGENT = "revenant-agent-fulcrum/1.0 (research backtest; contact: local)"


class DataError(RuntimeError):
    """A source could not be read. The caller decides whether to fall back."""


class HttpGet(Protocol):
    """The only way this module reaches the network."""

    def get(self, url: str, *, params: dict | None, timeout: float) -> HttpResponse: ...


class RequestsGet:
    """Real transport. Constructs no session and imports nothing heavy until first use.

    Never instantiated by a test. If you find this class in a test, the test touches
    the network and the test is wrong.
    """

    def __init__(self, *, session=None, user_agent: str = USER_AGENT) -> None:
        self._session = session
        self._user_agent = user_agent

    def get(self, url: str, *, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> HttpResponse:
        import requests  # local import: importing this module must cost nothing

        if self._session is None:
            self._session = requests.Session()
        resp = self._session.get(
            url, params=params, timeout=timeout, headers={"User-Agent": self._user_agent}
        )
        return resp.status_code, resp.content, dict(resp.headers)


ResponderValue = HttpResponse | Callable[[str, dict | None], HttpResponse]


class FakeGet:
    """Scripted transport for tests.

    ``routes`` maps a URL PREFIX to either a fixed ``(status, body, headers)`` triple or
    a callable ``(url, params) -> triple``. Longest matching prefix wins. Anything
    unmatched returns 404 with an empty body, which is exactly what a not-yet-listed
    Binance month looks like.

    Every call is recorded in ``self.calls`` as ``(url, params, timeout)``.
    """

    def __init__(self, routes: dict[str, ResponderValue] | None = None) -> None:
        self.routes: dict[str, ResponderValue] = dict(routes or {})
        self.calls: list[tuple[str, dict | None, float]] = []

    def route(self, prefix: str, value: ResponderValue) -> "FakeGet":
        self.routes[prefix] = value
        return self

    def get(self, url: str, *, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> HttpResponse:
        self.calls.append((url, dict(params) if params else None, timeout))
        matches = [p for p in self.routes if url.startswith(p)]
        if not matches:
            return 404, b"", {}
        value = self.routes[max(matches, key=len)]
        if callable(value):
            return value(url, dict(params) if params else None)
        return value

    # convenience for assertions
    @property
    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def params(self) -> list[dict | None]:
        return [c[1] for c in self.calls]


# --------------------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}/[A-Z0-9]{1,10}$")
_SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Coinbase/Binance spell a few majors differently from Alpaca. Extend deliberately.
_BINANCE_BASE_ALIASES: dict[str, str] = {}
_COINBASE_BASE_ALIASES: dict[str, str] = {}


class SymbolError(ValueError):
    """The symbol is not of the form BASE/QUOTE with safe characters."""


def parse_symbol(symbol: str) -> tuple[str, str]:
    """Split ``BTC/USD`` into ``("BTC", "USD")`` or raise.

    This is the single gate. A symbol that has not been through here never becomes a
    URL path segment or a filename. It rejects path traversal (``BTC/../../USD``),
    separators (``BTC\\USD``), drive letters (``C:\\evil``) and empty parts.
    """
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol):
        raise SymbolError(f"unsafe or malformed symbol {symbol!r}; expected BASE/QUOTE like 'BTC/USD'")
    base, quote = symbol.split("/")
    return base, quote


def to_coinbase(symbol: str) -> str:
    """``BTC/USD`` -> ``BTC-USD`` (Coinbase Exchange product id)."""
    base, quote = parse_symbol(symbol)
    return f"{_COINBASE_BASE_ALIASES.get(base, base)}-{quote}"


def to_binance(symbol: str) -> str:
    """``BTC/USD`` -> ``BTCUSDT``.

    Binance Vision has no USD spot pairs; USDT is the stand-in. That substitution is a
    real difference in the underlying instrument, not a formatting detail, which is why
    ``BinanceVisionKlines.quote_currency`` exists and why the report must state the
    caveat.
    """
    base, quote = parse_symbol(symbol)
    mapped = _BINANCE_BASE_ALIASES.get(base, base)
    return f"{mapped}{BinanceVisionKlines.quote_currency if quote == 'USD' else quote}"


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _iso_z(d: date) -> str:
    """Midnight UTC of ``d`` as ISO 8601 with a Z suffix."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _ts_from_epoch(value: int | float | str) -> datetime:
    """Parse an epoch of unknown unit into an aware UTC datetime.

    Binance Vision switched its kline timestamp unit once already. Rather than trust a
    format this build cannot verify offline, decide by magnitude: 10 digits seconds,
    13 milliseconds, 16 microseconds, 19 nanoseconds. Defensive, not verified.
    """
    n = int(float(value))
    a = abs(n)
    if a >= 10**17:
        return datetime.fromtimestamp(n / 1_000_000_000, tz=timezone.utc)
    if a >= 10**14:
        return datetime.fromtimestamp(n / 1_000_000, tz=timezone.utc)
    if a >= 10**11:
        return datetime.fromtimestamp(n / 1_000, tz=timezone.utc)
    return datetime.fromtimestamp(n, tz=timezone.utc)


def _dedup_sort(bars: Iterable[Bar]) -> list[Bar]:
    """Sort ascending by timestamp, last writer wins on a duplicate timestamp.

    Later means later in the input sequence: a refetch is treated as a correction.
    """
    by_ts: dict[datetime, Bar] = {}
    for b in bars:
        by_ts[b.ts] = b
    return [by_ts[k] for k in sorted(by_ts)]


def _clip(bars: Iterable[Bar], start: date, end: date) -> list[Bar]:
    """Keep bars whose open date is within [start, end] inclusive.

    Applied client-side after every fetch so that whatever a venue means by an
    inclusive or exclusive window endpoint cannot change what this module returns.
    """
    return [b for b in bars if start <= b.ts.date() <= end]


def _missing_days(bars: Sequence[Bar], start: date, end: date) -> list[date]:
    """Calendar days in [start, end] with no bar. Never filled, only reported."""
    present = {b.ts.date() for b in bars}
    out: list[date] = []
    day = start
    while day <= end:
        if day not in present:
            out.append(day)
        day += timedelta(days=1)
    return out


def _check_range(start: date, end: date) -> None:
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError("start and end must be datetime.date")
    if isinstance(start, datetime) or isinstance(end, datetime):
        raise TypeError("pass datetime.date, not datetime.datetime")
    if end < start:
        raise ValueError(f"end {end} is before start {start}")


# --------------------------------------------------------------------------------------
# Source protocol
# --------------------------------------------------------------------------------------


class DailySource(Protocol):
    """A source of daily bars. Both implementations are interchangeable."""

    source_name: str

    def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]: ...

    def daily_bars_with_gaps(
        self, symbol: str, start: date, end: date
    ) -> tuple[list[Bar], list[date]]: ...


# --------------------------------------------------------------------------------------
# Coinbase Exchange
# --------------------------------------------------------------------------------------


class CoinbaseCandles:
    """Daily candles from Coinbase Exchange's public candles endpoint.

    ``GET /products/{product}/candles?granularity=86400&start=...&end=...``

    Per D-011 the endpoint serves at most 300 candles per request and allows 10
    requests per second per IP, so a long range is walked forward in windows of at
    most ``MAX_CANDLES`` days and each request after the first is preceded by a sleep
    of ``1 / requests_per_second``. The sleep is injected so tests can assert pacing
    without waiting.

    Rows are ``[time, low, high, open, close, volume]`` with ``time`` in unix seconds.
    Note the order: low and high come BEFORE open and close, which is the reverse of
    almost every other venue. Getting this wrong transposes the bar, which is why
    ``Bar.__post_init__`` re-checks OHLC consistency on every row.
    """

    source_name = "coinbase"
    quote_currency = "USD"
    BASE_URL = "https://api.exchange.coinbase.com"
    GRANULARITY = 86400
    MAX_CANDLES = 300  # D-011: hard venue limit per request

    def __init__(
        self,
        http: HttpGet,
        *,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
        requests_per_second: float = 10.0,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._min_interval = 1.0 / float(requests_per_second)
        self._timeout = timeout
        self._requests_made = 0

    # ---- public ----------------------------------------------------------------
    def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return self.daily_bars_with_gaps(symbol, start, end)[0]

    def daily_bars_with_gaps(
        self, symbol: str, start: date, end: date
    ) -> tuple[list[Bar], list[date]]:
        """Bars ascending and deduplicated, plus the calendar days that are absent.

        The gap list is information, not a defect to repair. Nothing here fills it.
        """
        _check_range(start, end)
        product = to_coinbase(symbol)
        collected: list[Bar] = []
        for win_start, win_end in self.windows(start, end):
            collected.extend(self._fetch_window(symbol, product, win_start, win_end))
        bars = _clip(_dedup_sort(collected), start, end)
        return bars, _missing_days(bars, start, end)

    @classmethod
    def windows(cls, start: date, end: date) -> list[tuple[date, date]]:
        """Split [start, end] into inclusive windows of at most ``MAX_CANDLES`` days.

        A daily window of N calendar days is N candles, so the invariant every caller
        and every test should assert is ``(win_end - win_start).days + 1 <= 300``.
        """
        _check_range(start, end)
        out: list[tuple[date, date]] = []
        cur = start
        span = timedelta(days=cls.MAX_CANDLES - 1)
        while cur <= end:
            win_end = min(end, cur + span)
            out.append((cur, win_end))
            cur = win_end + timedelta(days=1)
        return out

    # ---- internals -------------------------------------------------------------
    def _pace(self) -> None:
        if self._requests_made:
            self._sleep(self._min_interval)
        self._requests_made += 1

    def _fetch_window(self, symbol: str, product: str, win_start: date, win_end: date) -> list[Bar]:
        self._pace()
        url = f"{self._base_url}/products/{product}/candles"
        params = {
            "granularity": self.GRANULARITY,
            "start": _iso_z(win_start),
            "end": _iso_z(win_end),
        }
        status, body, _headers = self._http.get(url, params=params, timeout=self._timeout)
        if status != 200:
            raise DataError(f"coinbase {product} {win_start}..{win_end}: HTTP {status}")
        return self._parse(symbol, body)

    @staticmethod
    def _parse(symbol: str, body: bytes) -> list[Bar]:
        import json

        try:
            rows = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except Exception as e:  # malformed JSON is a source failure, not a crash
            raise DataError(f"coinbase {symbol}: unreadable response: {type(e).__name__}") from None
        if not isinstance(rows, list):
            raise DataError(f"coinbase {symbol}: expected a list of candles, got {type(rows).__name__}")
        bars: list[Bar] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise DataError(f"coinbase {symbol}: malformed candle row {row!r}")
            ts_raw, low, high, open_, close, volume = row[:6]
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=_ts_from_epoch(ts_raw),
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume),
                )
            )
        return bars


# --------------------------------------------------------------------------------------
# Binance Vision
# --------------------------------------------------------------------------------------


class BinanceVisionKlines:
    """Daily klines from data.binance.vision monthly zip archives.

    ``/data/spot/monthly/klines/{SYM}/1d/{SYM}-1d-YYYY-MM.zip``

    CSV columns inside the zip:
    ``open time, open, high, low, close, volume, close time, quote volume, trades,
    taker buy base, taker buy quote, ignore``. Note this is open/high/low/close, the
    OPPOSITE of Coinbase's low/high/open/close. Some months carry a header row; it is
    detected by trying to read the first field as a number and skipped.

    A 404 for a month means the symbol was not listed yet (or the month is not
    published). That is recorded in ``missing_months`` and is not an error: the bars
    for the months that do exist are still returned, and the absent days come back in
    the gap list.

    The quote currency is USDT, not USD. Any comparison against a USD source carries
    the stablecoin basis; ``HistoricalSource.compare`` is the sanity check and the
    report has to say so.
    """

    source_name = "binance_vision"
    quote_currency = "USDT"
    BASE_URL = "https://data.binance.vision"
    INTERVAL = "1d"

    def __init__(
        self,
        http: HttpGet,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        requests_per_second: float = 10.0,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._sleep = sleep
        self._min_interval = 1.0 / float(requests_per_second)
        self._requests_made = 0
        self.missing_months: list[str] = []

    # ---- public ----------------------------------------------------------------
    def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return self.daily_bars_with_gaps(symbol, start, end)[0]

    def daily_bars_with_gaps(
        self, symbol: str, start: date, end: date
    ) -> tuple[list[Bar], list[date]]:
        _check_range(start, end)
        sym = to_binance(symbol)
        self.missing_months = []
        collected: list[Bar] = []
        for year, month in self.months(start, end):
            body = self._fetch_month(sym, year, month)
            if body is None:
                self.missing_months.append(f"{year:04d}-{month:02d}")
                continue
            collected.extend(self._parse_zip(symbol, body))
        bars = _clip(_dedup_sort(collected), start, end)
        return bars, _missing_days(bars, start, end)

    @staticmethod
    def months(start: date, end: date) -> list[tuple[int, int]]:
        _check_range(start, end)
        out: list[tuple[int, int]] = []
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            out.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    def month_url(self, sym: str, year: int, month: int) -> str:
        return (
            f"{self._base_url}/data/spot/monthly/klines/{sym}/{self.INTERVAL}/"
            f"{sym}-{self.INTERVAL}-{year:04d}-{month:02d}.zip"
        )

    # ---- internals -------------------------------------------------------------
    def _pace(self) -> None:
        if self._requests_made:
            self._sleep(self._min_interval)
        self._requests_made += 1

    def _fetch_month(self, sym: str, year: int, month: int) -> bytes | None:
        self._pace()
        url = self.month_url(sym, year, month)
        status, body, _headers = self._http.get(url, params=None, timeout=self._timeout)
        if status == 404:
            return None
        if status != 200:
            raise DataError(f"binance_vision {sym} {year:04d}-{month:02d}: HTTP {status}")
        return body

    @staticmethod
    def _parse_zip(symbol: str, body: bytes) -> list[Bar]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile:
            raise DataError(f"binance_vision {symbol}: not a zip archive") from None
        with zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise DataError(f"binance_vision {symbol}: zip contains no csv")
            bars: list[Bar] = []
            for name in sorted(names):
                text = zf.read(name).decode("utf-8")
                for row in csv.reader(io.StringIO(text)):
                    if not row or len(row) < 6:
                        continue
                    try:
                        ts = _ts_from_epoch(row[0])
                    except (TypeError, ValueError):
                        continue  # header row, or a stray line
                    bars.append(
                        Bar(
                            symbol=symbol,
                            ts=ts,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                        )
                    )
            return bars


# --------------------------------------------------------------------------------------
# On-disk cache
# --------------------------------------------------------------------------------------

CACHE_COLUMNS = ("ts_iso", "open", "high", "low", "close", "volume")


class BarCache:
    """One CSV per (source, symbol) under ``cfg.data.cache_dir``.

    Columns: ts_iso, open, high, low, close, volume. Floats are written with ``repr``
    so a round trip is exact, not approximate.

    Writes are atomic: a temp file beside the target inside the cache directory, then
    ``os.replace``. If the replace fails, the temp file is removed in a ``finally`` and
    the previous file is left exactly as it was. A half-written cache file would be
    indistinguishable from a real gap, so this is a correctness property, not tidiness.

    Nothing is ever written outside ``cache_dir``: the symbol passes ``parse_symbol``
    and the final path is resolved and checked against the resolved cache directory.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)

    # ---- paths -----------------------------------------------------------------
    def path_for(self, source: str, symbol: str) -> Path:
        if not isinstance(source, str) or not _SOURCE_RE.match(source):
            raise SymbolError(f"unsafe source name {source!r}")
        base, quote = parse_symbol(symbol)  # raises on anything path-shaped
        name = f"{source}__{base}-{quote}.csv"
        root = self.cache_dir.resolve()
        p = (root / name).resolve()
        if p.parent != root:
            raise SymbolError(f"refusing to write outside the cache directory: {p}")
        return p

    # ---- io --------------------------------------------------------------------
    def load(self, source: str, symbol: str) -> list[Bar]:
        p = self.path_for(source, symbol)
        if not p.exists():
            return []
        bars: list[Bar] = []
        with open(p, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            missing = set(CACHE_COLUMNS) - set(reader.fieldnames or [])
            if missing:
                raise DataError(f"cache file {p} missing columns {sorted(missing)}")
            for row in reader:
                ts = datetime.fromisoformat(row["ts_iso"])
                if ts.tzinfo is None:
                    raise DataError(f"cache file {p} has a naive timestamp {row['ts_iso']!r}")
                bars.append(
                    Bar(
                        symbol=symbol,
                        ts=ts.astimezone(timezone.utc),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )
        return _dedup_sort(bars)

    def store(self, source: str, symbol: str, bars: Sequence[Bar]) -> list[Bar]:
        """Merge ``bars`` into the cached series by timestamp and rewrite atomically.

        New bars win on a duplicate timestamp: a refetch is a correction. Returns the
        merged series so the caller does not have to read it back.
        """
        p = self.path_for(source, symbol)
        merged = _dedup_sort(list(self.load(source, symbol)) + list(bars))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(CACHE_COLUMNS)
                for b in merged:
                    w.writerow(
                        [
                            b.ts.astimezone(timezone.utc).isoformat(),
                            repr(b.open),
                            repr(b.high),
                            repr(b.low),
                            repr(b.close),
                            repr(b.volume),
                        ]
                    )
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                tmp.unlink()
        return merged


# --------------------------------------------------------------------------------------
# Composed source
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyHistory:
    bars: list[Bar]
    missing_dates: list[date]
    source_used: str
    from_cache: bool

    @property
    def empty(self) -> bool:
        return not self.bars


def _covers(bars: Sequence[Bar], start: date, end: date) -> bool:
    """True when the cached series spans the whole request.

    Endpoint definition: ``min(ts.date()) <= start and max(ts.date()) >= end``.
    Interior gaps do NOT disqualify a cache hit, because interior gaps are real: the
    venue does not have those days and refetching will not conjure them.
    """
    if not bars:
        return False
    # min/max rather than bars[0]/bars[-1]: every current call site feeds sorted bars,
    # but a future one that does not must not be handed a false cache hit.
    return min(b.ts for b in bars).date() <= start and max(b.ts for b in bars).date() >= end


def compare(primary_bars: Sequence[Bar], fallback_bars: Sequence[Bar]) -> dict:
    """USD vs USDT sanity check on the overlapping days.

    Returns median and max absolute close difference in basis points relative to the
    primary close, plus the overlap count. Zero overlap is not an error: it returns
    ``n_overlap = 0`` and ``median_abs_bps = None`` so the report can say "no overlap"
    instead of showing a fabricated zero.
    """
    p_by_day = {b.ts.date(): b.close for b in primary_bars}
    f_by_day = {b.ts.date(): b.close for b in fallback_bars}
    days = sorted(set(p_by_day) & set(f_by_day))
    diffs_bps: list[float] = []
    worst_day: date | None = None
    worst = 0.0
    for d in days:
        p, f = p_by_day[d], f_by_day[d]
        if p <= 0:
            continue
        bps = abs(p - f) / p * 1e4
        diffs_bps.append(bps)
        if bps >= worst:
            worst, worst_day = bps, d
    if not diffs_bps:
        return {
            "n_overlap": 0,
            "median_abs_bps": None,
            "max_abs_bps": None,
            "worst_day": None,
            "first_day": None,
            "last_day": None,
        }
    return {
        "n_overlap": len(diffs_bps),
        "median_abs_bps": float(statistics.median(diffs_bps)),
        "max_abs_bps": float(max(diffs_bps)),
        "worst_day": worst_day,
        "first_day": days[0],
        "last_day": days[-1],
    }


class HistoricalSource:
    """Cache, then primary, then fallback. Records which one answered.

    Order of attempts for ``get_daily``:

    1. The primary's cache, then the fallback's cache. A hit needs the cached series to
       span the request (see ``_covers``); gaps are recomputed from the cached bars so
       a cache hit reports the same missing days a fresh fetch would.
    2. The primary source. Anything it raises, and an empty result, both count as "no
       answer for this symbol".
    3. The fallback source. ``source_used`` records which one actually answered, so a
       backtest can never silently mix a USD series with a USDT one and call it USD.

    Whatever is fetched is stored under the source that produced it.
    """

    compare = staticmethod(compare)

    def __init__(self, primary: DailySource, fallback: DailySource | None, cache: BarCache) -> None:
        self.primary = primary
        self.fallback = fallback
        self.cache = cache
        # Why the last get_daily fell through to the fallback, or returned nothing.
        # Diagnostic only; never control flow.
        self.last_errors: list[str] = []

    def get_daily(self, symbol: str, start: date, end: date) -> DailyHistory:
        _check_range(start, end)
        self.last_errors = []
        sources = [s for s in (self.primary, self.fallback) if s is not None]

        for src in sources:
            cached = self.cache.load(src.source_name, symbol)
            if _covers(cached, start, end):
                bars = _clip(cached, start, end)
                return DailyHistory(
                    bars=bars,
                    missing_dates=_missing_days(bars, start, end),
                    source_used=src.source_name,
                    from_cache=True,
                )

        errors = self.last_errors  # recorded as they happen, so a successful fallback
        for src in sources:       # still leaves the reason the primary was skipped
            try:
                bars, missing = src.daily_bars_with_gaps(symbol, start, end)
            except Exception as e:  # a dead source must not take the run down
                errors.append(f"{src.source_name}: {type(e).__name__}: {e}")
                continue
            if not bars:
                errors.append(f"{src.source_name}: no bars for {symbol} in {start}..{end}")
                continue
            self.cache.store(src.source_name, symbol, bars)
            return DailyHistory(
                bars=bars, missing_dates=missing, source_used=src.source_name, from_cache=False
            )

        return DailyHistory(
            bars=[], missing_dates=_missing_days([], start, end), source_used="none", from_cache=False
        )


# --------------------------------------------------------------------------------------
# Frames, screens, freshness
# --------------------------------------------------------------------------------------


def to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    """Bars to the exact frame ``fulcrum.features.compute_features`` expects.

    Index: tz-aware UTC DatetimeIndex, sorted ascending, no duplicates.
    Columns: open, high, low, close, volume.

    Duplicate timestamps RAISE rather than being collapsed. Deduplication is a data
    layer decision that belongs upstream in ``_dedup_sort``; by the time a frame is
    built, two bars for one day means something is wrong and silently keeping one of
    them would hide it.
    """
    cols = ("open", "high", "low", "close", "volume")
    if not bars:
        idx = pd.DatetimeIndex([], tz="UTC", name="ts")
        return pd.DataFrame({c: pd.Series([], dtype="float64") for c in cols}, index=idx)

    symbols = {b.symbol for b in bars}
    if len(symbols) > 1:
        raise ValueError(f"to_frame takes bars for one symbol, got {sorted(symbols)}")

    ordered = sorted(bars, key=lambda b: b.ts)
    ts = [b.ts.astimezone(timezone.utc) for b in ordered]
    if len(set(ts)) != len(ts):
        dupes = sorted({t.isoformat() for t in ts if ts.count(t) > 1})
        raise ValueError(f"duplicate bar timestamps: {dupes}")

    idx = pd.DatetimeIndex(ts, name="ts")
    if idx.tz is None:  # defensive: every Bar.ts is already aware
        idx = idx.tz_localize("UTC")
    frame = pd.DataFrame(
        {
            "open": [b.open for b in ordered],
            "high": [b.high for b in ordered],
            "low": [b.low for b in ordered],
            "close": [b.close for b in ordered],
            "volume": [b.volume for b in ordered],
        },
        index=idx,
    )
    return frame


def liquidity_screen(
    candidates: Sequence[str],
    histories: dict[str, DailyHistory],
    min_median_daily_volume_usd: float,
    lookback_days: int = 30,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Median daily dollar volume screen. Pure: no I/O, no clock.

    A symbol passes when the median of ``close * volume`` over its last
    ``lookback_days`` bars is STRICTLY greater than the threshold. Passing symbols come
    back in the order given so the universe is reproducible. Rejections come back as
    ``(symbol, reason)`` so the README can state why a coin is not in the book.

    A symbol with fewer than ``lookback_days`` bars is rejected, not screened on a
    short window: an unverifiable liquidity claim is the thing this screen exists to
    keep out.
    """
    if lookback_days < 1:
        raise ValueError("lookback_days must be >= 1")
    passing: list[str] = []
    rejected: list[tuple[str, str]] = []
    for sym in candidates:
        hist = histories.get(sym)
        if hist is None or not hist.bars:
            rejected.append((sym, "no history"))
            continue
        bars = hist.bars[-lookback_days:]
        if len(bars) < lookback_days:
            rejected.append((sym, f"only {len(bars)} bars, need {lookback_days}"))
            continue
        dollar_volume = [b.close * b.volume for b in bars]
        med = float(statistics.median(dollar_volume))
        if med > min_median_daily_volume_usd:
            passing.append(sym)
        else:
            rejected.append(
                (sym, f"median daily volume {med:,.0f} USD not above {min_median_daily_volume_usd:,.0f}")
            )
    return passing, rejected


def newest_bar_age_minutes(bars: Sequence[Bar], now: datetime) -> float:
    """Minutes since the newest bar CLOSED.

    A daily bar stamped at its open time closes 24 hours later, so the age is measured
    from ``newest.ts + 1 day``. A still-forming bar therefore has a negative age, which
    is correct and is deliberately not clamped: clamping would make "the bar has not
    closed yet" and "the bar closed this instant" indistinguishable to the stale-data
    guard in ``risk``.

    With no bars at all the age is infinite, which every threshold treats as stale.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware UTC")
    if not bars:
        return math.inf
    newest = max(b.ts for b in bars)
    closed_at = newest.astimezone(timezone.utc) + timedelta(days=1)
    return (now.astimezone(timezone.utc) - closed_at).total_seconds() / 60.0
