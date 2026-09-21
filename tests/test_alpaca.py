"""AlpacaBroker and AlpacaData, entirely against FakeTransport.

No test here touches the network, and the only credential used is a made up pair defined in
this file. Assertions are made on the calls FakeTransport recorded, so "it did not call the
venue" is a provable statement and not a hope.
"""

from __future__ import annotations

import json

import pytest
import requests

from fulcrum.alpaca import (
    AlpacaBroker, AlpacaData, AssetInfo, FakeTransport, MAX_ORDER_NOTIONAL_USD, PaperOnlyViolation,
    RATE_LIMIT_PER_MINUTE, REQUEST_TIMEOUT_SECONDS, TokenBucket, parse_rfc3339,
)
from fulcrum.broker import ALPACA_PAPER_URL, BrokerError, DuplicateOrderError, OrderRejected
from fulcrum.config import AssetClassConfig
from fulcrum.creds import Credential
from fulcrum.models import OrderRequest
from tests.conftest import utc

# The live host appears exactly once in the repo: here, as the value the lock must reject.
# Every other rejected variant below is derived from this string or from the paper constant.
LIVE_URL = "https://api.alpaca.markets"

FIRST_FIELD = "TEST-FIRST-FIELD-0000"
SECOND_FIELD = "test-only-second-field-value"
CRED = Credential(user=FIRST_FIELD, secret=SECOND_FIELD)

ASSET_CLASS = AssetClassConfig(
    name="crypto", trading_days_per_year=365, calendar="24x7", taker_fee_bps=25.0,
    maker_fee_bps=15.0, slippage_bps=10.0, min_notional_usd=10.0, quote_currency="USD",
)

ASSETS_PAYLOAD = [
    {"symbol": "BTC/USD", "tradable": True, "status": "active", "min_order_size": "0.000026",
     "min_trade_increment": "0.000001", "price_increment": "1"},
    {"symbol": "ETH/USD", "tradable": True, "status": "active", "min_order_size": "0.0013",
     "min_trade_increment": "0.0001", "price_increment": "0.1"},
    # Not USD quoted: must be filtered out of the table.
    {"symbol": "BTC/USDT", "tradable": True, "status": "active", "min_order_size": "0.000026",
     "min_trade_increment": "0.000001", "price_increment": "1"},
]

ORDER_PAYLOAD = {
    "id": "b0b1c2d3-0000-4000-8000-000000000001",
    "client_order_id": "cid-entry-1",
    "symbol": "BTC/USD",
    "side": "buy",
    "status": "accepted",
    "submitted_at": "2026-09-21T14:30:00.123456789Z",
    "created_at": "2026-09-21T14:29:59Z",
    "notional": "1000",
    "qty": None,
    "filled_qty": "0",
    "filled_avg_price": None,
    "filled_at": None,
}


class FakeClock:
    """Monotonic seconds that only move when a test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


def make_broker(transport: FakeTransport | None = None, *, sleep=None, bucket=None, clock=None):
    t = transport if transport is not None else FakeTransport()
    b = AlpacaBroker(
        base_url=ALPACA_PAPER_URL, credential=CRED, transport=t, asset_class=ASSET_CLASS,
        clock=clock or (lambda: utc(2026, 9, 21, 15, 0)),
        sleep=sleep if sleep is not None else RecordingSleep(), bucket=bucket,
    )
    return b, t


def with_assets(t: FakeTransport) -> FakeTransport:
    return t.script("GET", "/v2/assets", 200, ASSETS_PAYLOAD)


def entry(cid: str = "cid-entry-1", notional: float | None = 1000.0, qty: float | None = None) -> OrderRequest:
    return OrderRequest(client_order_id=cid, decision_id="dec-1", symbol="BTC/USD", side="buy",
                        purpose="entry", notional_usd=notional, qty=qty)


# ---------------------------------------------------------------------------
# the paper only lock
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    LIVE_URL,
    LIVE_URL.replace("https://", "http://"),
    LIVE_URL + "/",
    ALPACA_PAPER_URL.replace("https://", "http://"),
    "https://paper-api.alpaca.markets.evil.example",
    "https://paper-api.alpaca.markets.evil.example/v2/orders",
    ALPACA_PAPER_URL.replace("https://", "https://user@"),
    "",
    None,
    "   ",
    ALPACA_PAPER_URL.upper(),
])
def test_paper_lock_rejects_every_non_paper_base_url(bad):
    t = FakeTransport()
    with pytest.raises(PaperOnlyViolation):
        AlpacaBroker(base_url=bad, credential=CRED, transport=t, asset_class=ASSET_CLASS)
    assert t.calls == [], "the lock must fire before any request is made"


@pytest.mark.parametrize("good", [ALPACA_PAPER_URL, ALPACA_PAPER_URL + "/", ALPACA_PAPER_URL + "///"])
def test_paper_lock_accepts_the_paper_url_with_or_without_trailing_slash(good):
    t = FakeTransport()
    b = AlpacaBroker(base_url=good, credential=CRED, transport=t, asset_class=ASSET_CLASS)
    assert b.base_url == ALPACA_PAPER_URL
    assert t.calls == [], "the constructor does no I/O"


def test_paper_lock_has_no_relaxing_parameter():
    import inspect

    params = set(inspect.signature(AlpacaBroker.__init__).parameters)
    for forbidden in ("live", "allow_live", "paper", "paper_only", "force", "override", "env"):
        assert forbidden not in params


# ---------------------------------------------------------------------------
# credential handling
# ---------------------------------------------------------------------------
def test_headers_carry_the_credential_and_the_body_never_does():
    b, t = make_broker()
    with_assets(t).script("POST", "/v2/orders", 200, ORDER_PAYLOAD)
    b.submit(entry())
    post = t.calls_to("POST", "/v2/orders")[0]
    assert post.headers["APCA-API-KEY-ID"] == FIRST_FIELD
    assert post.headers["APCA-API-SECRET-KEY"] == SECOND_FIELD
    assert post.timeout == REQUEST_TIMEOUT_SECONDS
    body_text = json.dumps(post.json_body)
    assert SECOND_FIELD not in body_text and FIRST_FIELD not in body_text
    for call in t.calls:
        assert SECOND_FIELD not in json.dumps(call.json_body)
        assert SECOND_FIELD not in call.url


def test_repr_does_not_contain_the_secret():
    b, _ = make_broker()
    assert SECOND_FIELD not in repr(b) and SECOND_FIELD not in str(b)
    assert FIRST_FIELD not in repr(b)
    assert ALPACA_PAPER_URL in repr(b)


def test_error_messages_do_not_contain_the_secret():
    b, t = make_broker()
    with_assets(t).script_server_error("POST", "/v2/orders", 500)
    with pytest.raises(BrokerError) as exc:
        b.submit(entry())
    assert SECOND_FIELD not in str(exc.value)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------
def test_submit_builds_the_documented_body():
    b, t = make_broker()
    with_assets(t).script("POST", "/v2/orders", 201, ORDER_PAYLOAD)
    ack = b.submit(entry(notional=1000.0))
    post = t.calls_to("POST", "/v2/orders")[0]
    assert post.json_body == {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc",
        "client_order_id": "cid-entry-1",
        "notional": "1000.00",
    }
    assert "qty" not in post.json_body
    assert ack.client_order_id == "cid-entry-1"
    assert ack.broker_order_id == ORDER_PAYLOAD["id"]
    assert ack.status == "accepted"
    assert ack.submitted_at == parse_rfc3339(ORDER_PAYLOAD["submitted_at"], "t")


def test_submit_qty_order_is_floored_and_sent_as_a_plain_decimal_string():
    b, t = make_broker()
    with_assets(t).script("POST", "/v2/orders", 200, ORDER_PAYLOAD)
    # 0.01234567 floored to ETH's 0.0001 increment is 0.0123.
    req = OrderRequest(client_order_id="cid-qty", decision_id="d", symbol="ETH/USD", side="buy",
                       purpose="entry", qty=0.01234567)
    b.submit(req)
    body = t.calls_to("POST", "/v2/orders")[0].json_body
    assert body["qty"] == "0.0123"
    assert "notional" not in body
    assert "e" not in body["qty"].lower(), "never scientific notation"


def test_submit_tiny_qty_stays_plain_decimal():
    b, t = make_broker()
    with_assets(t).script("POST", "/v2/orders", 200, ORDER_PAYLOAD)
    req = OrderRequest(client_order_id="cid-tiny", decision_id="d", symbol="BTC/USD", side="buy",
                       purpose="entry", qty=0.0000271)
    b.submit(req)
    assert t.calls_to("POST", "/v2/orders")[0].json_body["qty"] == "0.000027"


def test_duplicate_client_order_id_raises_duplicate_order_error():
    b, t = make_broker()
    with_assets(t).script_duplicate()
    with pytest.raises(DuplicateOrderError) as exc:
        b.submit(entry())
    assert "cid-entry-1" in str(exc.value)
    assert len(t.calls_to("POST", "/v2/orders")) == 1


def test_other_422_is_a_plain_rejection_not_a_duplicate():
    b, t = make_broker()
    with_assets(t).script("POST", "/v2/orders", 422, {"message": "insufficient balance"})
    with pytest.raises(OrderRejected) as exc:
        b.submit(entry())
    assert type(exc.value) is OrderRejected
    assert "insufficient balance" in str(exc.value)


def test_403_is_an_order_rejection_with_the_venue_message():
    b, t = make_broker()
    with_assets(t).script("POST", "/v2/orders", 403, {"message": "crypto trading is not enabled"})
    with pytest.raises(OrderRejected) as exc:
        b.submit(entry())
    assert "crypto trading is not enabled" in str(exc.value)


def test_429_then_200_retries_exactly_once_with_backoff():
    sleeps = RecordingSleep()
    b, t = make_broker(sleep=sleeps)
    with_assets(t).script_rate_limited("POST", "/v2/orders", retry_after="2")
    t.script("POST", "/v2/orders", 200, ORDER_PAYLOAD)
    ack = b.submit(entry())
    assert ack.status == "accepted"
    assert len(t.calls_to("POST", "/v2/orders")) == 2, "one retry, not two"
    assert sleeps.calls == [2.0]


def test_retry_after_is_capped_at_five_seconds_and_read_case_insensitively():
    sleeps = RecordingSleep()
    b, t = make_broker(sleep=sleeps)
    with_assets(t)
    t.script("POST", "/v2/orders", 429, {"message": "slow down"}, {"retry-after": "600"})
    t.script("POST", "/v2/orders", 200, ORDER_PAYLOAD)
    b.submit(entry())
    assert sleeps.calls == [5.0]


def test_429_without_a_usable_retry_after_still_retries_once():
    sleeps = RecordingSleep()
    b, t = make_broker(sleep=sleeps)
    with_assets(t)
    t.script("POST", "/v2/orders", 429, {"message": "slow down"}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    t.script("POST", "/v2/orders", 200, ORDER_PAYLOAD)
    b.submit(entry())
    assert sleeps.calls == [0.0]


def test_429_twice_is_a_broker_error():
    b, t = make_broker()
    with_assets(t).script_rate_limited("POST", "/v2/orders")
    t.script_rate_limited("POST", "/v2/orders")
    with pytest.raises(BrokerError) as exc:
        b.submit(entry())
    assert type(exc.value) is BrokerError
    assert len(t.calls_to("POST", "/v2/orders")) == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_on_a_post_is_a_broker_error_with_no_retry(status):
    sleeps = RecordingSleep()
    b, t = make_broker(sleep=sleeps)
    t.script_server_error("POST", "/v2/orders", status)
    with pytest.raises(BrokerError) as exc:
        b.submit(entry())
    assert type(exc.value) is BrokerError, "not a rejection: the order state is unknown"
    assert len(t.calls_to("POST", "/v2/orders")) == 1, "a retried POST is how double submits happen"
    assert sleeps.calls == []
    # The load bearing assertion is the call count above. This one only confirms the script
    # was fully consumed; do not read it as independent proof that nothing was retried.
    assert t.remaining == 0


def test_connection_error_on_a_post_is_a_broker_error_with_no_retry():
    b, t = make_broker()
    with_assets(t).script_connection_error("POST", "/v2/orders")
    with pytest.raises(BrokerError) as exc:
        b.submit(entry())
    assert type(exc.value) is BrokerError
    assert len(t.calls_to("POST", "/v2/orders")) == 1
    assert "unknown" in str(exc.value)


def test_connection_error_subclasses_are_handled_too():
    b, t = make_broker()
    with_assets(t).script_error("POST", "/v2/orders", requests.Timeout("read timed out"))
    with pytest.raises(BrokerError):
        b.submit(entry())
    assert len(t.calls_to("POST", "/v2/orders")) == 1


# ---------------------------------------------------------------------------
# pre-send rejections: nothing leaves the process
# ---------------------------------------------------------------------------
def test_notional_above_the_venue_maximum_is_rejected_before_sending():
    b, t = make_broker()
    with_assets(t)
    with pytest.raises(OrderRejected) as exc:
        b.submit(entry(notional=MAX_ORDER_NOTIONAL_USD + 0.01))
    assert "maximum" in str(exc.value)
    assert t.calls_to("POST", "/v2/orders") == []


def test_notional_below_the_configured_minimum_is_rejected_before_sending():
    b, t = make_broker()
    with_assets(t)
    with pytest.raises(OrderRejected) as exc:
        b.submit(entry(notional=9.99))
    assert "below" in str(exc.value)
    assert t.calls_to("POST", "/v2/orders") == []


def test_notional_is_checked_after_rounding_down_not_before():
    # 9.999 rounds down to 9.99, which is below the 10.00 minimum. It must not sneak past.
    b, t = make_broker()
    with_assets(t)
    with pytest.raises(OrderRejected):
        b.submit(entry(notional=9.999))
    assert t.calls_to("POST", "/v2/orders") == []


def test_qty_below_the_venue_minimum_order_size_is_rejected_before_sending():
    b, t = make_broker()
    with_assets(t)
    req = OrderRequest(client_order_id="cid-small", decision_id="d", symbol="BTC/USD", side="buy",
                       purpose="entry", qty=0.00001)
    with pytest.raises(OrderRejected) as exc:
        b.submit(req)
    assert "minimum order size" in str(exc.value)
    assert t.calls_to("POST", "/v2/orders") == []


def test_unknown_symbol_is_rejected_before_sending():
    b, t = make_broker()
    with_assets(t)
    req = OrderRequest(client_order_id="cid-unknown", decision_id="d", symbol="DOGE/USD", side="buy",
                       purpose="entry", qty=100.0)
    with pytest.raises(OrderRejected):
        b.submit(req)
    assert t.calls_to("POST", "/v2/orders") == []


# ---------------------------------------------------------------------------
# order lookup
# ---------------------------------------------------------------------------
def test_order_by_client_id_uses_the_colon_path_and_the_query_param():
    b, t = make_broker()
    t.script("GET", "/v2/orders:by_client_order_id", 200, ORDER_PAYLOAD)
    order = b.order_by_client_id("cid-entry-1")
    call = t.calls[0]
    assert call.path == "/v2/orders:by_client_order_id"
    assert call.url == f"{ALPACA_PAPER_URL}/v2/orders:by_client_order_id"
    assert call.params == {"client_order_id": "cid-entry-1"}
    assert order is not None and order.client_order_id == "cid-entry-1"
    assert order.broker_order_id == ORDER_PAYLOAD["id"]
    assert order.notional_usd == pytest.approx(1000.0)
    assert order.qty is None


def test_order_by_client_id_returns_none_on_404():
    b, t = make_broker()
    t.script("GET", "/v2/orders:by_client_order_id", 404, {"code": 40410000, "message": "order not found"})
    assert b.order_by_client_id("cid-missing") is None


def test_rfc3339_parsing_gives_aware_utc_including_nanoseconds():
    b, t = make_broker()
    payload = dict(ORDER_PAYLOAD, status="filled", filled_qty="0.02", filled_avg_price="50000",
                   filled_at="2026-09-21T14:30:01Z")
    t.script("GET", "/v2/orders:by_client_order_id", 200, payload)
    order = b.order_by_client_id("cid-entry-1")
    assert order.submitted_at == utc(2026, 9, 21, 14, 30).replace(microsecond=123456)
    assert order.submitted_at.tzinfo is not None
    assert order.submitted_at.utcoffset().total_seconds() == 0
    assert order.filled_at == utc(2026, 9, 21, 14, 30).replace(second=1)
    assert order.filled_qty == pytest.approx(0.02)
    assert order.filled_avg_price == pytest.approx(50000.0)


@pytest.mark.parametrize("raw,expected", [
    ("new", "new"),
    ("accepted", "accepted"),
    ("partially_filled", "partially_filled"),
    ("filled", "filled"),
    ("canceled", "canceled"),
    ("rejected", "rejected"),
    ("expired", "expired"),
    ("pending_new", "unknown"),
    ("done_for_day", "unknown"),
    ("held", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_status_mapping_including_unknown(raw, expected):
    b, t = make_broker()
    t.script("GET", "/v2/orders:by_client_order_id", 200, dict(ORDER_PAYLOAD, status=raw))
    assert b.order_by_client_id("cid-entry-1").status == expected


def test_open_orders_asks_for_open_with_the_documented_limit():
    b, t = make_broker()
    t.script("GET", "/v2/orders", 200, [ORDER_PAYLOAD, dict(ORDER_PAYLOAD, id="x2", client_order_id="cid-2")])
    orders = b.open_orders()
    assert [o.client_order_id for o in orders] == ["cid-entry-1", "cid-2"]
    assert t.calls[0].params == {"status": "open", "limit": 500}


@pytest.mark.parametrize("status", [204, 404])
def test_cancel_accepts_204_and_404(status):
    b, t = make_broker()
    t.script("DELETE", "/v2/orders/abc-123", status, None)
    b.cancel("abc-123")
    assert t.calls[0].method == "DELETE"
    assert t.calls[0].path == "/v2/orders/abc-123"


def test_cancel_surfaces_other_client_errors():
    b, t = make_broker()
    t.script("DELETE", "/v2/orders/abc-123", 422, {"message": "order is not cancelable"})
    with pytest.raises(OrderRejected):
        b.cancel("abc-123")


# ---------------------------------------------------------------------------
# account, positions, assets
# ---------------------------------------------------------------------------
def test_account_uses_non_marginable_buying_power_as_cash():
    b, t = make_broker()
    t.script("GET", "/v2/account", 200,
             {"equity": "25000.5", "cash": "19000.25", "non_marginable_buying_power": "12000.75"})
    acct = b.account()
    assert acct.equity == pytest.approx(25000.5)
    # Spot crypto cannot use margin, so the spendable figure is the non marginable one.
    assert acct.cash == pytest.approx(12000.75)
    assert acct.as_of == utc(2026, 9, 21, 15, 0)


def test_account_falls_back_to_cash_when_the_field_is_absent():
    b, t = make_broker()
    t.script("GET", "/v2/account", 200, {"equity": "100", "cash": "40"})
    assert b.account().cash == pytest.approx(40.0)


def test_positions_normalize_venue_symbols():
    b, t = make_broker()
    with_assets(t).script("GET", "/v2/positions", 200, [
        {"symbol": "BTCUSD", "qty": "0.5", "avg_entry_price": "40000", "market_value": "25000"},
        {"symbol": "ETHUSD", "qty": "2", "avg_entry_price": "2000", "market_value": "4200"},
    ])
    positions = b.positions()
    assert [p.symbol for p in positions] == ["BTC/USD", "ETH/USD"]
    assert positions[0].qty == pytest.approx(0.5)
    assert positions[0].market_value == pytest.approx(25000.0)


def test_positions_already_slashed_are_left_alone_and_need_no_asset_call():
    b, t = make_broker()
    t.script("GET", "/v2/positions", 200,
             [{"symbol": "BTC/USD", "qty": "1", "avg_entry_price": "40000", "market_value": "41000"}])
    assert b.positions()[0].symbol == "BTC/USD"
    assert t.calls_to("GET", "/v2/assets") == []


def test_positions_survive_an_assets_outage_with_the_suffix_rule():
    b, t = make_broker()
    t.script_server_error("GET", "/v2/assets", 503)
    t.script("GET", "/v2/positions", 200,
             [{"symbol": "SOLUSD", "qty": "10", "avg_entry_price": "150", "market_value": "1600"}])
    assert b.positions()[0].symbol == "SOL/USD"


def test_assets_parsing_filters_to_usd_pairs_and_caches():
    b, t = make_broker()
    with_assets(t)
    table = b.assets()
    assert set(table) == {"BTC/USD", "ETH/USD"}, "non USD quoted pairs are excluded"
    assert table["BTC/USD"] == AssetInfo(symbol="BTC/USD", tradable=True, min_order_size=0.000026,
                                         min_trade_increment=0.000001, price_increment=1.0)
    assert b.assets() is table, "cached after the first call"
    assert len(t.calls_to("GET", "/v2/assets")) == 1
    assert t.calls[0].params == {"asset_class": "crypto", "status": "active"}


@pytest.mark.parametrize("symbol,raw,expected", [
    ("BTC/USD", 0.0123456789, 0.012345),
    ("BTC/USD", 0.000026, 0.000026),
    ("ETH/USD", 1.99999, 1.9999),
    ("ETH/USD", 2.0, 2.0),
])
def test_round_qty_floors_to_the_trade_increment(symbol, raw, expected):
    b, t = make_broker()
    with_assets(t)
    assert b.round_qty(symbol, raw) == pytest.approx(expected)


def test_round_qty_never_rounds_up():
    b, t = make_broker()
    with_assets(t)
    for raw in (0.0000269, 0.00009999, 0.123456999):
        assert b.round_qty("BTC/USD", raw) <= raw


def test_round_qty_raises_below_the_minimum_order_size():
    b, t = make_broker()
    with_assets(t)
    with pytest.raises(OrderRejected) as exc:
        b.round_qty("BTC/USD", 0.0000259)
    assert "minimum order size" in str(exc.value)


# ---------------------------------------------------------------------------
# prices and fills
# ---------------------------------------------------------------------------
def test_latest_price_parses_the_trades_payload():
    b, t = make_broker()
    t.script("GET", "/v1beta3/crypto/us/latest/trades", 200,
             {"trades": {"BTC/USD": {"t": "2026-09-21T14:59:30.123456789Z", "p": 63250.42, "s": 0.01}}})
    quote = b.latest_price("BTC/USD")
    assert quote.symbol == "BTC/USD"
    assert quote.price == pytest.approx(63250.42)
    assert quote.ts == utc(2026, 9, 21, 14, 59).replace(second=30, microsecond=123456)
    call = t.calls[0]
    assert call.url.startswith("https://data.alpaca.markets")
    assert call.params == {"symbols": "BTC/USD"}


def test_latest_price_missing_symbol_is_a_broker_error():
    b, t = make_broker()
    t.script("GET", "/v1beta3/crypto/us/latest/trades", 200, {"trades": {}})
    with pytest.raises(BrokerError):
        b.latest_price("BTC/USD")


def test_fills_for_models_the_fee_from_the_taker_rate():
    b, t = make_broker()
    t.script("GET", "/v2/orders:by_client_order_id", 200,
             dict(ORDER_PAYLOAD, status="filled", filled_qty="0.02", filled_avg_price="50000",
                  filled_at="2026-09-21T14:30:05Z"))
    fills = b.fills_for("cid-entry-1")
    assert len(fills) == 1
    f = fills[0]
    assert f.qty == pytest.approx(0.02)
    assert f.price == pytest.approx(50000.0)
    # 0.02 * 50000 = 1000 gross; 25 bps taker = 2.50. Modeled, because Alpaca posts fees end of day.
    assert f.fee_usd == pytest.approx(2.50)
    assert f.slippage_bps_est is None
    assert f.filled_at == utc(2026, 9, 21, 14, 30).replace(second=5)


def test_fills_for_is_empty_when_nothing_filled():
    b, t = make_broker()
    t.script("GET", "/v2/orders:by_client_order_id", 200, ORDER_PAYLOAD)
    assert b.fills_for("cid-entry-1") == []


def test_fills_for_is_empty_when_the_order_is_unknown():
    b, t = make_broker()
    t.script("GET", "/v2/orders:by_client_order_id", 404, {"message": "not found"})
    assert b.fills_for("cid-nope") == []


# ---------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------
def bar_row(day: int, hour: int = 0, close: float = 100.0) -> dict:
    return {"t": f"2026-09-{day:02d}T{hour:02d}:00:00Z", "o": close - 1, "h": close + 2,
            "l": close - 3, "c": close, "v": 1234.5, "n": 10, "vw": close}


def make_data(transport: FakeTransport | None = None, clock=None):
    t = transport if transport is not None else FakeTransport()
    return AlpacaData(credential=CRED, transport=t, clock=clock or (lambda: utc(2026, 9, 21, 12, 0)),
                      sleep=RecordingSleep()), t


def test_bars_follows_the_page_token_across_two_pages_and_stops():
    d, t = make_data()
    t.script("GET", "/v1beta3/crypto/us/bars", 200,
             {"bars": {"BTC/USD": [bar_row(1, close=100.0), bar_row(2, close=101.0)]},
              "next_page_token": "PAGE2"})
    t.script("GET", "/v1beta3/crypto/us/bars", 200,
             {"bars": {"BTC/USD": [bar_row(3, close=102.0)]}, "next_page_token": None})
    out = d.bars(["BTC/USD"], start=utc(2026, 9, 1), end=utc(2026, 9, 4))
    assert [b.close for b in out["BTC/USD"]] == [100.0, 101.0, 102.0]
    assert [b.ts for b in out["BTC/USD"]] == [utc(2026, 9, 1), utc(2026, 9, 2), utc(2026, 9, 3)]
    assert out["BTC/USD"][0].volume == pytest.approx(1234.5)
    calls = t.calls_to("GET", "/v1beta3/crypto/us/bars")
    assert len(calls) == 2, "stops when no token comes back"
    assert calls[0].params == {"symbols": "BTC/USD", "timeframe": "1Day",
                               "start": "2026-09-01T00:00:00Z", "end": "2026-09-04T00:00:00Z",
                               "limit": 10000}
    assert calls[1].params["page_token"] == "PAGE2"


def test_bars_handles_several_symbols_and_an_absent_token_key():
    d, t = make_data()
    t.script("GET", "/v1beta3/crypto/us/bars", 200,
             {"bars": {"BTC/USD": [bar_row(1)], "ETH/USD": [bar_row(1, close=50.0)]}})
    out = d.bars(["BTC/USD", "ETH/USD"], start=utc(2026, 9, 1))
    assert set(out) == {"BTC/USD", "ETH/USD"}
    assert t.calls[0].params["symbols"] == "BTC/USD,ETH/USD"
    assert "end" not in t.calls[0].params


def test_bars_refuses_to_loop_on_a_repeated_page_token():
    d, t = make_data()
    for _ in range(3):
        t.script("GET", "/v1beta3/crypto/us/bars", 200,
                 {"bars": {"BTC/USD": [bar_row(1)]}, "next_page_token": "SAME"})
    with pytest.raises(BrokerError) as exc:
        d.bars(["BTC/USD"], start=utc(2026, 9, 1))
    assert "repeated" in str(exc.value)


def test_probe_daily_boundary_reports_spacing_and_hours():
    d, t = make_data(clock=lambda: utc(2026, 9, 21, 12, 0))
    rows = [bar_row(day, hour=0, close=100.0 + day) for day in range(15, 21)]
    t.script("GET", "/v1beta3/crypto/us/bars", 200, {"bars": {"BTC/USD": rows}})
    probe = d.probe_daily_boundary("BTC/USD")
    assert probe["bar_count"] == 5, "the last five daily bars"
    assert probe["hours"] == [0, 0, 0, 0, 0]
    assert probe["minutes"] == [0, 0, 0, 0, 0]
    assert probe["spacing_hours"] == [24.0, 24.0, 24.0, 24.0]
    assert probe["uniform_spacing"] is True
    assert probe["timestamps"][-1].startswith("2026-09-20T00:00:00")
    assert probe["symbol"] == "BTC/USD"


def test_probe_daily_boundary_reports_a_non_midnight_boundary_as_observed():
    d, t = make_data()
    rows = [bar_row(day, hour=5, close=100.0) for day in range(16, 21)]
    t.script("GET", "/v1beta3/crypto/us/bars", 200, {"bars": {"BTC/USD": rows}})
    probe = d.probe_daily_boundary("BTC/USD")
    assert probe["distinct_hours"] == [5], "nothing assumes UTC midnight"
    assert probe["spacing_hours"] == [24.0, 24.0, 24.0, 24.0]


# ---------------------------------------------------------------------------
# rate budget
# ---------------------------------------------------------------------------
def test_token_bucket_blocks_the_call_after_the_minute_budget():
    clock = FakeClock()
    sleeps = RecordingSleep()
    bucket = TokenBucket(capacity=RATE_LIMIT_PER_MINUTE, per_seconds=60.0, clock=clock, sleep=sleeps)
    b, t = make_broker(sleep=sleeps, bucket=bucket)
    for _ in range(RATE_LIMIT_PER_MINUTE + 1):
        t.script("GET", "/v2/account", 200, {"equity": "100", "cash": "100"})
    for _ in range(RATE_LIMIT_PER_MINUTE):
        b.account()
    assert sleeps.calls == [], "the first 200 calls inside the minute do not wait"
    b.account()
    assert len(sleeps.calls) == 1, "the 201st call is blocked"
    assert sleeps.calls[0] == pytest.approx(60.0 / RATE_LIMIT_PER_MINUTE)
    assert len(t.calls_to("GET", "/v2/account")) == RATE_LIMIT_PER_MINUTE + 1


def test_token_bucket_refills_with_the_clock():
    clock = FakeClock()
    sleeps = RecordingSleep(clock)
    bucket = TokenBucket(capacity=2, per_seconds=60.0, clock=clock, sleep=sleeps)
    bucket.take()
    bucket.take()
    clock.advance(60.0)
    assert bucket.take() == 0.0, "a full minute later the budget is back"
    assert sleeps.calls == []


def test_token_bucket_does_not_loop_when_the_clock_never_moves():
    clock = FakeClock()
    sleeps = RecordingSleep()  # deliberately does not advance the clock
    bucket = TokenBucket(capacity=1, per_seconds=60.0, clock=clock, sleep=sleeps)
    bucket.take()
    for _ in range(5):
        assert bucket.take() > 0.0
    assert len(sleeps.calls) == 5, "one sleep per call, no spin"


# ---------------------------------------------------------------------------
# timestamp helper
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("2026-09-21T14:30:00Z", utc(2026, 9, 21, 14, 30)),
    ("2026-09-21T14:30:00.123Z", utc(2026, 9, 21, 14, 30).replace(microsecond=123000)),
    ("2026-09-21T14:30:00.123456789Z", utc(2026, 9, 21, 14, 30).replace(microsecond=123456)),
    ("2026-09-21T16:30:00+02:00", utc(2026, 9, 21, 14, 30)),
])
def test_parse_rfc3339_variants(raw, expected):
    parsed = parse_rfc3339(raw, "t")
    assert parsed == expected
    assert parsed.utcoffset().total_seconds() == 0


def test_parse_rfc3339_rejects_naive_and_garbage():
    assert parse_rfc3339(None, "t") is None
    assert parse_rfc3339("", "t") is None
    with pytest.raises(BrokerError):
        parse_rfc3339("2026-09-21T14:30:00", "t")
    with pytest.raises(BrokerError):
        parse_rfc3339("not a timestamp", "t")
