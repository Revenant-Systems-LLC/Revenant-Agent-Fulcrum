"""AlpacaBroker (paper only) and AlpacaData, behind a tiny injectable HTTP seam.

Facts this module is built on are recorded in docs/DECISIONS.md D-009 and D-010, each with
a source URL and an as-of date. The ones that shape the code:

* Paper base URL is ``broker.ALPACA_PAPER_URL``. The live host string does not appear in
  this file at all. ``AlpacaBroker.__init__`` compares the caller's base_url to the paper
  constant and refuses anything else with ``PaperOnlyViolation`` before any request is made.
  There is no parameter, environment variable, subclass hook, or config key that relaxes it.
* Crypto orders: market type, gtc time in force, exactly one of notional or qty, per order
  maximum 200000 USD notional, per symbol min_order_size / min_trade_increment from
  GET /v2/assets.
* ``client_order_id`` is unique per active order; a duplicate returns HTTP 422 with
  "client_order_id must be unique". Lookup path has a literal colon in it.
* Trading API budget is 200 requests per minute per account, HTTP 429 on breach.
* Fees are posted end of day and are not attached to the fill, so every fee this module
  reports is MODELED from ``asset_class.taker_fee_bps`` and marked as such (D-009).

Retry policy, because this is where money is lost:
* 429 is retried exactly once, after sleeping the Retry-After hint capped at 5 seconds.
  A 429 means the request was refused before it was processed, so a retry cannot duplicate.
* A 5xx or a connection error is NEVER retried, on any method. For a POST the order state
  is genuinely unknown, and an automatic retry is how double submits happen. The caller
  treats ``BrokerError`` as unknown state and reconciles by the deterministic
  client_order_id (D-005, D-008).

Nothing here logs, prints, or stores a credential. The secret goes into one request header
and nowhere else, error messages are built from method, path, status and a truncated body,
and ``__repr__`` is explicit so a traceback cannot leak the object's contents.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import requests

from .broker import ALPACA_PAPER_URL, BrokerError, DuplicateOrderError, OrderRejected
from .config import AssetClassConfig
from .creds import Credential
from .models import (
    Account, Bar, Fill, Order, OrderAck, OrderRequest, OrderStatus, Position, Quote, utcnow,
)

REQUEST_TIMEOUT_SECONDS = 15.0
RATE_LIMIT_PER_MINUTE = 200
MAX_RETRY_AFTER_SECONDS = 5.0
MAX_ORDER_NOTIONAL_USD = 200_000.0
DEFAULT_DATA_URL = "https://data.alpaca.markets"
BARS_PAGE_LIMIT = 10_000
MAX_PAGES = 1_000  # a broken pager must fail loudly, not spin forever
_BODY_SNIPPET_CHARS = 300

# Alpaca statuses that map one to one onto the OrderStatus literal. Everything else
# (pending_new, done_for_day, replaced, pending_cancel, stopped, suspended, calculated,
# held, accepted_for_bidding) is deliberately "unknown": the engine reconciles rather
# than guessing what a status it has never seen means.
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": "new",
    "accepted": "accepted",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "rejected": "rejected",
    "expired": "expired",
}

_FRACTION_RE = re.compile(r"\.(\d+)")


class PaperOnlyViolation(ValueError):
    """AlpacaBroker was constructed against something other than Alpaca's paper endpoint.

    A ValueError and not a BrokerError: this is a programming or configuration fault caught
    before any network contact, not a venue condition to be reconciled.
    """


# ---------------------------------------------------------------------------
# transport seam
# ---------------------------------------------------------------------------
class HttpTransport(Protocol):
    """The whole of this module's contact with the network, so tests can replace it."""

    def request(self, method: str, url: str, *, headers: dict, params: dict | None,
                json_body: dict | None, timeout: float) -> tuple[int, dict | list | None, dict]:
        """Return (status_code, parsed json or None, response headers with lower case keys)."""
        ...


class RequestsTransport:
    """The real transport. Keeps header case normalization here so nothing downstream
    has to care that ``requests`` hands back a case insensitive mapping and a plain dict
    does not."""

    def __init__(self, session: Any | None = None) -> None:
        self._session = session if session is not None else requests.Session()

    def request(self, method: str, url: str, *, headers: dict, params: dict | None,
                json_body: dict | None, timeout: float) -> tuple[int, dict | list | None, dict]:
        resp = self._session.request(method, url, headers=headers, params=params, json=json_body,
                                     timeout=timeout)
        body: dict | list | None
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = None
        return resp.status_code, body, {str(k).lower(): v for k, v in resp.headers.items()}

    def __repr__(self) -> str:
        return "RequestsTransport()"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    method: str
    url: str
    headers: dict
    params: dict | None
    json_body: dict | None
    timeout: float

    @property
    def path(self) -> str:
        return urlsplit(self.url).path


class FakeTransport:
    """Scripted transport for tests. Also importable by other test modules.

    Responses are keyed by (METHOD, path) and popped in order, so a test can script a 429
    followed by a 200 for the same endpoint. A scripted entry may be an Exception instance,
    which is raised when it is popped; that is how a connection error is scripted. Running
    out of script for a key is an AssertionError naming the unscripted call, never a silent
    empty response.
    """

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self._script: dict[tuple[str, str], list[Any]] = {}

    # ---- scripting -------------------------------------------------------
    def script(self, method: str, path: str, status: int, body: dict | list | None = None,
               headers: dict | None = None) -> "FakeTransport":
        self._script.setdefault((method.upper(), path), []).append(
            (int(status), body, {str(k).lower(): v for k, v in (headers or {}).items()}))
        return self

    def script_error(self, method: str, path: str, exc: BaseException) -> "FakeTransport":
        self._script.setdefault((method.upper(), path), []).append(exc)
        return self

    def script_connection_error(self, method: str, path: str,
                                message: str = "connection reset") -> "FakeTransport":
        return self.script_error(method, path, requests.ConnectionError(message))

    def script_duplicate(self, method: str = "POST", path: str = "/v2/orders") -> "FakeTransport":
        """The real 422 shape for a repeated client_order_id (D-009)."""
        return self.script(method, path, 422,
                           {"code": 42210000, "message": "client_order_id must be unique"})

    def script_rate_limited(self, method: str, path: str,
                            retry_after: str | None = "1") -> "FakeTransport":
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return self.script(method, path, 429, {"message": "rate limit exceeded"}, headers)

    def script_server_error(self, method: str, path: str, status: int = 503) -> "FakeTransport":
        return self.script(method, path, status, {"message": "service unavailable"})

    # ---- transport -------------------------------------------------------
    def request(self, method: str, url: str, *, headers: dict, params: dict | None,
                json_body: dict | None, timeout: float) -> tuple[int, dict | list | None, dict]:
        path = urlsplit(url).path
        self.calls.append(RecordedCall(method.upper(), url, dict(headers), params, json_body, timeout))
        queue = self._script.get((method.upper(), path))
        if not queue:
            raise AssertionError(f"unscripted call {method.upper()} {path} (url {url}, params {params})")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # ---- assertions helpers ---------------------------------------------
    def calls_to(self, method: str, path: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.method == method.upper() and c.path == path]

    @property
    def remaining(self) -> int:
        return sum(len(v) for v in self._script.values())


# ---------------------------------------------------------------------------
# rate budget
# ---------------------------------------------------------------------------
class TokenBucket:
    """200 requests per minute, with an injectable clock and sleep so tests never wait.

    ``take`` computes the wait once, sleeps once, and then consumes a token unconditionally.
    There is no re-check loop: with a fake clock that does not advance, a loop would hang
    the test suite instead of failing it.
    """

    def __init__(self, *, capacity: int = RATE_LIMIT_PER_MINUTE, per_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if capacity < 1 or per_seconds <= 0:
            raise ValueError("TokenBucket needs capacity >= 1 and per_seconds > 0")
        self.capacity = float(capacity)
        self._rate = float(capacity) / float(per_seconds)
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self._rate)

    def take(self) -> float:
        """Consume one token. Returns the seconds waited (0.0 when a token was available)."""
        self._refill()
        waited = 0.0
        if self._tokens < 1.0:
            waited = (1.0 - self._tokens) / self._rate
            self._sleep(waited)
            self._refill()
            if self._tokens < 1.0:
                self._tokens = 1.0
        self._tokens -= 1.0
        return waited


# ---------------------------------------------------------------------------
# small parsing helpers
# ---------------------------------------------------------------------------
def parse_rfc3339(value: str | None, field: str) -> datetime | None:
    """RFC 3339 with a Z suffix to an aware UTC datetime.

    Alpaca sends nanosecond precision on some fields. datetime.fromisoformat on 3.11
    happens to truncate, but that is not something to depend on, so the fraction is cut to
    six digits here explicitly.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BrokerError(f"{field}: expected an RFC 3339 string, got {type(value).__name__}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    def _trim(m: re.Match[str]) -> str:
        return "." + m.group(1)[:6]

    text = _FRACTION_RE.sub(_trim, text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise BrokerError(f"{field}: cannot parse timestamp {value!r}: {e}") from None
    if parsed.tzinfo is None:
        raise BrokerError(f"{field}: timestamp {value!r} carries no timezone")
    return parsed.astimezone(timezone.utc)


def rfc3339(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_float(value: Any, field: str, *, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise BrokerError(f"{field}: expected a number, got {value!r}") from None
    if math.isnan(out) or math.isinf(out):
        raise BrokerError(f"{field}: non finite number {value!r}")
    return out


def _req_float(value: Any, field: str) -> float:
    out = _as_float(value, field)
    if out is None:
        raise BrokerError(f"{field}: missing")
    return out


def _decimal(value: float | str, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BrokerError(f"{field}: cannot read {value!r} as a decimal") from None


def _plain(d: Decimal) -> str:
    """Plain decimal string, never scientific notation. str(Decimal('0.0000001')) is '1E-7'."""
    return format(d, "f")


def _snippet(body: Any) -> str:
    """A short, credential free description of a response body, for an error message.

    Only the venue's own message text or a truncated repr of the parsed body. Request headers
    are never touched here, which is what keeps the secret out of exception strings.
    """
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, str):
            return msg[:_BODY_SNIPPET_CHARS]
    return repr(body)[:_BODY_SNIPPET_CHARS]


def _floor_to(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise BrokerError(f"invalid increment {increment}")
    steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return (steps * increment).quantize(increment, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class AssetInfo:
    symbol: str           # normalized, with the slash: BTC/USD
    tradable: bool
    min_order_size: float
    min_trade_increment: float
    price_increment: float


# ---------------------------------------------------------------------------
# shared HTTP client
# ---------------------------------------------------------------------------
class _ApiClient:
    """Credential headers, rate budget, timeout, and the retry policy, in one place.

    Returns (status, body, headers) for anything that is not a transport failure, a 5xx, or
    an exhausted 429, so callers can give 404 and 422 their own meaning. Raises BrokerError
    for the cases where the request did not produce an answer.
    """

    def __init__(self, *, credential: Credential, transport: HttpTransport,
                 timeout: float = REQUEST_TIMEOUT_SECONDS,
                 bucket: TokenBucket | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._credential = credential
        self._transport = transport
        self._timeout = float(timeout)
        self._sleep = sleep
        self.bucket = bucket if bucket is not None else TokenBucket(sleep=sleep)

    def _headers(self) -> dict:
        # The only place either credential field is ever used.
        return {
            "APCA-API-KEY-ID": self._credential.user,
            "APCA-API-SECRET-KEY": self._credential.secret,
            "Accept": "application/json",
        }

    @staticmethod
    def _retry_after(headers: dict) -> float:
        raw = headers.get("retry-after")
        if raw is None:
            return 0.0
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return 0.0  # a date form or anything malformed counts as no hint
        if seconds != seconds or seconds < 0:
            return 0.0
        return min(seconds, MAX_RETRY_AFTER_SECONDS)

    def request(self, method: str, url: str, *, params: dict | None = None,
                json_body: dict | None = None) -> tuple[int, Any, dict]:
        attempt = 0
        while True:
            self.bucket.take()
            try:
                status, body, headers = self._transport.request(
                    method, url, headers=self._headers(), params=params, json_body=json_body,
                    timeout=self._timeout)
            except Exception as e:  # noqa: BLE001  any transport failure means order state unknown (security review)
                # Never retried, on any method. See the module docstring.
                raise BrokerError(
                    f"{method} {urlsplit(url).path}: transport failure ({type(e).__name__}); "
                    "order state is unknown until reconciled") from None
            if status == 429:
                if attempt == 0:
                    attempt = 1
                    self._sleep(self._retry_after(headers))
                    continue
                raise BrokerError(f"{method} {urlsplit(url).path}: rate limited twice (429)")
            if status >= 500:
                raise BrokerError(
                    f"{method} {urlsplit(url).path}: server error {status}: {_snippet(body)}; "
                    "state unknown until reconciled")
            return status, body, headers

    def get_json(self, url: str, *, params: dict | None = None, expect: type | tuple[type, ...] = dict,
                 none_on_404: bool = False) -> Any:
        status, body, _ = self.request("GET", url, params=params)
        if status == 404 and none_on_404:
            return None
        if status >= 400:
            raise BrokerError(f"GET {urlsplit(url).path}: HTTP {status}: {_snippet(body)}")
        if not isinstance(body, expect):
            raise BrokerError(f"GET {urlsplit(url).path}: unexpected payload {type(body).__name__}")
        return body


# ---------------------------------------------------------------------------
# broker
# ---------------------------------------------------------------------------
class AlpacaBroker:
    """Alpaca paper trading, implementing the Broker protocol from broker.py."""

    def __init__(self, *, base_url: str, credential: Credential, transport: HttpTransport,
                 asset_class: AssetClassConfig, data_url: str = DEFAULT_DATA_URL,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 bucket: TokenBucket | None = None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        # THE PAPER ONLY LOCK. First statement in the constructor, before any attribute is
        # set and long before any request. isinstance keeps None and non strings from
        # raising AttributeError instead of the intended error.
        if not isinstance(base_url, str) or base_url.rstrip("/") != ALPACA_PAPER_URL:
            raise PaperOnlyViolation(
                f"AlpacaBroker accepts only Alpaca's paper endpoint {ALPACA_PAPER_URL}; "
                f"refusing base_url {base_url!r}. There is no flag, environment variable, or "
                "config key that relaxes this. Going live is a reviewed code change.")
        self.base_url = ALPACA_PAPER_URL
        self.data_url = str(data_url).rstrip("/")
        self.asset_class = asset_class
        self._clock = clock or utcnow
        self._api = _ApiClient(credential=credential, transport=transport, timeout=timeout,
                               bucket=bucket, sleep=sleep)
        # Instance level, not a module global: a global would leak between processes' worth
        # of tests and between two brokers. One broker lives for the process, so this is the
        # process lifetime cache the spec asks for.
        self._assets: dict[str, AssetInfo] | None = None

    def __repr__(self) -> str:  # explicit, so no default repr can ever surface a credential
        return f"AlpacaBroker(base_url={self.base_url!r}, asset_class={self.asset_class.name!r})"

    __str__ = __repr__

    # ---- account and positions -------------------------------------------
    def account(self) -> Account:
        body = self._api.get_json(f"{self.base_url}/v2/account")
        equity = _req_float(body.get("equity"), "account.equity")
        # Spot crypto cannot be bought on margin, so the figure that says what can actually
        # be spent is non_marginable_buying_power, not cash (D-009). Plain cash can include
        # margin buying power that crypto is not allowed to use, which would let the sizer
        # target a position the venue will refuse. Fall back to cash only if the field is
        # absent, so an API change degrades to the conservative-enough value rather than
        # crashing the scheduler.
        nmbp = _as_float(body.get("non_marginable_buying_power"), "account.non_marginable_buying_power")
        cash = nmbp if nmbp is not None else _req_float(body.get("cash"), "account.cash")
        return Account(equity=equity, cash=cash, as_of=self._clock())

    def positions(self) -> list[Position]:
        body = self._api.get_json(f"{self.base_url}/v2/positions", expect=list)
        out: list[Position] = []
        for raw in body:
            if not isinstance(raw, dict):
                raise BrokerError("positions: unexpected row type")
            symbol = self.normalize_symbol(str(raw.get("symbol", "")))
            qty = _req_float(raw.get("qty"), f"position[{symbol}].qty")
            avg = _req_float(raw.get("avg_entry_price"), f"position[{symbol}].avg_entry_price")
            mv = _as_float(raw.get("market_value"), f"position[{symbol}].market_value")
            out.append(Position(symbol=symbol, qty=qty, avg_entry=avg,
                                market_value=mv if mv is not None else qty * avg))
        return out

    def normalize_symbol(self, venue_symbol: str) -> str:
        """Alpaca returns crypto positions as BTCUSD; everything else in this repo uses BTC/USD.

        The asset table is the authority. If it cannot be loaded (the assets endpoint is down)
        the deterministic quote-currency suffix rule is used instead, because a reconciliation
        pass must not be blocked by a second endpoint being unavailable.
        """
        if "/" in venue_symbol or not venue_symbol:
            return venue_symbol
        try:
            table = self.assets()
        except BrokerError:
            table = {}
        for sym in table:
            if sym.replace("/", "") == venue_symbol:
                return sym
        quote = self.asset_class.quote_currency
        if venue_symbol.endswith(quote) and len(venue_symbol) > len(quote):
            return f"{venue_symbol[:-len(quote)]}/{quote}"
        return venue_symbol

    # ---- assets -----------------------------------------------------------
    def assets(self) -> dict[str, AssetInfo]:
        """Active crypto assets quoted in the configured currency, keyed by BTC/USD form.

        Cached after the first call: the table changes on the scale of listings, not ticks,
        and the sizer reads it on every cycle.
        """
        if self._assets is not None:
            return self._assets
        body = self._api.get_json(f"{self.base_url}/v2/assets", expect=list,
                                  params={"asset_class": "crypto", "status": "active"})
        quote = self.asset_class.quote_currency
        table: dict[str, AssetInfo] = {}
        for raw in body:
            if not isinstance(raw, dict):
                raise BrokerError("assets: unexpected row type")
            symbol = str(raw.get("symbol", ""))
            if "/" not in symbol or not symbol.endswith(f"/{quote}"):
                continue
            table[symbol] = AssetInfo(
                symbol=symbol,
                tradable=bool(raw.get("tradable", False)),
                min_order_size=_req_float(raw.get("min_order_size"), f"asset[{symbol}].min_order_size"),
                min_trade_increment=_req_float(raw.get("min_trade_increment"),
                                               f"asset[{symbol}].min_trade_increment"),
                price_increment=_req_float(raw.get("price_increment"), f"asset[{symbol}].price_increment"),
            )
        self._assets = table
        return table

    def asset(self, symbol: str) -> AssetInfo:
        info = self.assets().get(symbol)
        if info is None:
            raise OrderRejected(f"{symbol} is not an active {self.asset_class.quote_currency} crypto pair")
        return info

    def _round_qty_dec(self, symbol: str, qty: float) -> Decimal:
        info = self.asset(symbol)
        inc = _decimal(info.min_trade_increment, f"asset[{symbol}].min_trade_increment")
        rounded = _floor_to(_decimal(qty, "qty"), inc)
        if rounded < _decimal(info.min_order_size, f"asset[{symbol}].min_order_size"):
            raise OrderRejected(
                f"{symbol}: qty {_plain(rounded)} after flooring to increment {_plain(inc)} is below "
                f"the venue minimum order size {info.min_order_size}")
        return rounded

    def round_qty(self, symbol: str, qty: float) -> float:
        """Floor qty to the symbol's min_trade_increment. Raises OrderRejected below minimum."""
        return float(self._round_qty_dec(symbol, qty))

    # ---- orders -----------------------------------------------------------
    def build_order_body(self, req: OrderRequest) -> dict:
        """The exact JSON body for a market order, with every pre-send check applied.

        Checks run against the value that will actually be sent, after rounding, so a
        notional that floors below the minimum cannot slip through.
        """
        body: dict[str, Any] = {
            "symbol": req.symbol,
            "side": req.side,
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": req.client_order_id,
        }
        if req.notional_usd is not None:
            notional = _decimal(req.notional_usd, "notional_usd").quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if notional > _decimal(MAX_ORDER_NOTIONAL_USD, "max notional"):
                raise OrderRejected(
                    f"{req.symbol}: notional {_plain(notional)} exceeds the venue per order maximum "
                    f"{MAX_ORDER_NOTIONAL_USD:.2f}")
            if notional < _decimal(self.asset_class.min_notional_usd, "min_notional_usd"):
                raise OrderRejected(
                    f"{req.symbol}: notional {_plain(notional)} is below the configured minimum "
                    f"{self.asset_class.min_notional_usd}")
            body["notional"] = _plain(notional)
        else:
            body["qty"] = _plain(self._round_qty_dec(req.symbol, float(req.qty)))
        return body

    def submit(self, req: OrderRequest) -> OrderAck:
        body = self.build_order_body(req)
        url = f"{self.base_url}/v2/orders"
        status, payload, _ = self._api.request("POST", url, json_body=body)
        if status in (200, 201):
            data = payload if isinstance(payload, dict) else {}
            if not data:
                raise BrokerError("POST /v2/orders: accepted with an unreadable body")
            submitted = (parse_rfc3339(data.get("submitted_at"), "order.submitted_at")
                         or parse_rfc3339(data.get("created_at"), "order.created_at")
                         or self._clock())
            return OrderAck(
                client_order_id=str(data.get("client_order_id") or req.client_order_id),
                broker_order_id=str(data.get("id") or ""),
                status=_map_status(data.get("status")),
                submitted_at=submitted,
            )
        message = _snippet(payload)
        if status == 422 and "client_order_id must be unique" in str(message):
            raise DuplicateOrderError(
                f"{req.symbol}: client_order_id {req.client_order_id} already exists at the venue: {message}")
        if 400 <= status < 500:
            raise OrderRejected(f"POST /v2/orders rejected ({status}): {message}")
        raise BrokerError(f"POST /v2/orders: unexpected status {status}: {message}")

    def order_by_client_id(self, client_order_id: str) -> Order | None:
        url = f"{self.base_url}/v2/orders:by_client_order_id"
        body = self._api.get_json(url, params={"client_order_id": client_order_id}, none_on_404=True)
        if body is None:
            return None
        return self._order_from(body)

    def open_orders(self) -> list[Order]:
        body = self._api.get_json(f"{self.base_url}/v2/orders", expect=list,
                                  params={"status": "open", "limit": 500})
        return [self._order_from(raw) for raw in body]

    def cancel(self, broker_order_id: str) -> None:
        """204 means canceled, 404 means it is already gone. Both are the desired end state."""
        url = f"{self.base_url}/v2/orders/{broker_order_id}"
        status, payload, _ = self._api.request("DELETE", url)
        if status in (200, 204, 404):
            return
        if 400 <= status < 500:
            raise OrderRejected(f"DELETE /v2/orders/{broker_order_id}: HTTP {status}: "
                                f"{_snippet(payload)}")
        raise BrokerError(f"DELETE /v2/orders/{broker_order_id}: unexpected status {status}")

    def _order_from(self, raw: Any) -> Order:
        if not isinstance(raw, dict):
            raise BrokerError("order: unexpected payload type")
        symbol = self.normalize_symbol(str(raw.get("symbol", "")))
        side = str(raw.get("side", ""))
        if side not in ("buy", "sell"):
            raise BrokerError(f"order {raw.get('client_order_id')!r}: unreadable side {side!r}")
        filled_qty = _as_float(raw.get("filled_qty"), "order.filled_qty", default=0.0) or 0.0
        return Order(
            client_order_id=str(raw.get("client_order_id") or ""),
            broker_order_id=str(raw.get("id") or ""),
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            status=_map_status(raw.get("status")),
            submitted_at=(parse_rfc3339(raw.get("submitted_at"), "order.submitted_at")
                          or parse_rfc3339(raw.get("created_at"), "order.created_at")
                          or self._clock()),
            notional_usd=_as_float(raw.get("notional"), "order.notional"),
            qty=_as_float(raw.get("qty"), "order.qty"),
            filled_qty=filled_qty,
            filled_avg_price=_as_float(raw.get("filled_avg_price"), "order.filled_avg_price"),
            filled_at=parse_rfc3339(raw.get("filled_at"), "order.filled_at"),
        )

    # ---- prices and fills -------------------------------------------------
    def latest_price(self, symbol: str) -> Quote:
        url = f"{self.data_url}/v1beta3/crypto/us/latest/trades"
        body = self._api.get_json(url, params={"symbols": symbol})
        trades = body.get("trades")
        if not isinstance(trades, dict) or symbol not in trades or not isinstance(trades[symbol], dict):
            raise BrokerError(f"latest trade for {symbol} missing from the response")
        trade = trades[symbol]
        ts = parse_rfc3339(trade.get("t"), f"trade[{symbol}].t")
        if ts is None:
            raise BrokerError(f"latest trade for {symbol} has no timestamp")
        return Quote(symbol=symbol, price=_req_float(trade.get("p"), f"trade[{symbol}].p"), ts=ts)

    def fills_for(self, client_order_id: str) -> list[Fill]:
        """One aggregate fill derived from the order, since Alpaca reports filled_qty and
        filled_avg_price rather than a per execution list on this path.

        The fee is MODELED from asset_class.taker_fee_bps, not reported by the venue: Alpaca
        calculates and posts crypto fees end of day, so no fee figure exists at fill time
        (D-009). slippage_bps_est is None here because the modeled reference price belongs to
        the decision, not to the broker.
        """
        order = self.order_by_client_id(client_order_id)
        if order is None or order.filled_qty <= 0:
            return []
        if order.filled_avg_price is None:
            raise BrokerError(f"order {client_order_id} reports filled_qty without filled_avg_price")
        fee = order.filled_qty * order.filled_avg_price * self.asset_class.taker_fee_bps / 1e4
        return [Fill(
            client_order_id=order.client_order_id or client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.filled_qty,
            price=order.filled_avg_price,
            fee_usd=fee,
            filled_at=order.filled_at or order.submitted_at,
            slippage_bps_est=None,
        )]


def _map_status(raw: Any) -> OrderStatus:
    return _STATUS_MAP.get(str(raw or "").lower(), "unknown")


# ---------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------
class AlpacaData:
    """Crypto bars from the market data API (D-010).

    Historical crypto data is the documented exception to the auth requirement; the keys are
    sent anyway so the account's own budget and entitlements apply.
    """

    def __init__(self, *, credential: Credential, transport: HttpTransport,
                 data_url: str = DEFAULT_DATA_URL,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 bucket: TokenBucket | None = None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.data_url = str(data_url).rstrip("/")
        self._clock = clock or utcnow
        self._api = _ApiClient(credential=credential, transport=transport, timeout=timeout,
                               bucket=bucket, sleep=sleep)

    def __repr__(self) -> str:
        return f"AlpacaData(data_url={self.data_url!r})"

    __str__ = __repr__

    def latest_prices(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest printed trade per symbol in one call (D-010), for the 15-minute stop check.
        A symbol the venue does not price is left out, never guessed."""
        wanted = [s for s in symbols if s]
        if not wanted:
            return {}
        url = f"{self.data_url}/v1beta3/crypto/us/latest/trades"
        body = self._api.get_json(url, params={"symbols": ",".join(wanted)})
        trades = body.get("trades") if isinstance(body, dict) else None
        out: dict[str, Quote] = {}
        if not isinstance(trades, dict):
            return out
        for sym in wanted:
            t = trades.get(sym)
            if not isinstance(t, dict):
                continue
            ts = parse_rfc3339(t.get("t"), f"trade[{sym}].t")
            if ts is None or t.get("p") is None:
                continue
            out[sym] = Quote(symbol=sym, price=_req_float(t.get("p"), f"trade[{sym}].p"), ts=ts)
        return out

    def bars(self, symbols: list[str], start: datetime, end: datetime | None = None,
             timeframe: str = "1Day") -> dict[str, list[Bar]]:
        """Paginate with page_token until the response stops handing one back.

        Guards: a repeated token and a bounded page count both raise, because a pager that
        never terminates would hang the process instead of failing it.
        """
        if not symbols:
            return {}
        url = f"{self.data_url}/v1beta3/crypto/us/bars"
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": rfc3339(start),
            "limit": BARS_PAGE_LIMIT,
        }
        if end is not None:
            params["end"] = rfc3339(end)
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        seen_tokens: set[str] = set()
        token: str | None = None
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            body = self._api.get_json(url, params=page_params)
            page = body.get("bars") or {}
            if not isinstance(page, dict):
                raise BrokerError("bars: 'bars' is not an object")
            for symbol, rows in page.items():
                if not isinstance(rows, list):
                    raise BrokerError(f"bars[{symbol}]: not a list")
                out.setdefault(symbol, []).extend(_bar_from(symbol, r) for r in rows)
            nxt = body.get("next_page_token")
            if not nxt:
                return out
            token = str(nxt)
            if token in seen_tokens:
                raise BrokerError("bars: page_token repeated; refusing to loop")
            seen_tokens.add(token)
        raise BrokerError(f"bars: more than {MAX_PAGES} pages; refusing to continue")

    def probe_daily_boundary(self, symbol: str, lookback_days: int = 10) -> dict[str, Any]:
        """Observe where the 1Day bar boundary actually falls (D-010: it is undocumented).

        Returns what was seen, not an assumption: the timestamps of the last five daily bars,
        their hour and minute, and the spacing between them in hours. The caller records this
        to the ledger alerts table as a data_probe. Nothing in the engine assumes UTC midnight
        until this has been observed.
        """
        now = self._clock()
        start = now - timedelta(days=lookback_days)
        series = self.bars([symbol], start=start).get(symbol, [])
        last = series[-5:]
        stamps = [b.ts for b in last]
        spacing = [round((b - a).total_seconds() / 3600.0, 4) for a, b in zip(stamps, stamps[1:])]
        return {
            "symbol": symbol,
            "observed_at": now.isoformat(),
            "bar_count": len(last),
            "timestamps": [t.isoformat() for t in stamps],
            "hours": [t.hour for t in stamps],
            "minutes": [t.minute for t in stamps],
            "distinct_hours": sorted({t.hour for t in stamps}),
            "distinct_minutes": sorted({t.minute for t in stamps}),
            "spacing_hours": spacing,
            "uniform_spacing": len(set(spacing)) <= 1,
        }


def _bar_from(symbol: str, raw: Any) -> Bar:
    if not isinstance(raw, dict):
        raise BrokerError(f"bars[{symbol}]: unexpected row type")
    ts = parse_rfc3339(raw.get("t"), f"bar[{symbol}].t")
    if ts is None:
        raise BrokerError(f"bars[{symbol}]: bar without a timestamp")
    return Bar(
        symbol=symbol,
        ts=ts,
        open=_req_float(raw.get("o"), f"bar[{symbol}].o"),
        high=_req_float(raw.get("h"), f"bar[{symbol}].h"),
        low=_req_float(raw.get("l"), f"bar[{symbol}].l"),
        close=_req_float(raw.get("c"), f"bar[{symbol}].c"),
        volume=_req_float(raw.get("v"), f"bar[{symbol}].v"),
    )
