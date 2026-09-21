import pytest

from fulcrum.broker import BrokerError, DuplicateOrderError, FakeBroker, OrderRejected
from fulcrum.models import OrderRequest
from tests.conftest import utc


def make(cash=10000.0, fee=25.0, slip=10.0, min_notional=10.0):
    b = FakeBroker(starting_cash=cash, fee_bps=fee, slippage_bps=slip, min_notional_usd=min_notional,
                   clock=lambda: utc(2026, 1, 1))
    b.set_price("BTC/USD", 50000.0)
    return b


def buy(cid="b1", notional=1000.0, purpose="entry"):
    return OrderRequest(client_order_id=cid, decision_id="d", symbol="BTC/USD", side="buy", purpose=purpose,
                        notional_usd=notional)


def sell(cid="s1", qty=None, notional=None, purpose="exit"):
    return OrderRequest(client_order_id=cid, decision_id="d", symbol="BTC/USD", side="sell", purpose=purpose,
                        qty=qty, notional_usd=notional)


def test_buy_fill_math_hand_computed():
    b = make()
    ack = b.submit(buy(notional=1000.0))
    assert ack.status == "filled"
    f = b.fills_for("b1")[0]
    # slippage 10 bps against the buyer: 50000 * 1.001 = 50050
    assert f.price == pytest.approx(50050.0)
    assert f.qty == pytest.approx(1000.0 / 50050.0)
    # fee 25 bps of gross notional 1000 = 2.50
    assert f.fee_usd == pytest.approx(2.50)
    assert b.cash == pytest.approx(10000.0 - 1000.0 - 2.50)
    pos = b.positions()[0]
    assert pos.qty == pytest.approx(f.qty)
    assert pos.avg_entry == pytest.approx(50050.0)
    # equity marks at last price (50000), so the round trip already shows slippage + fee as a loss
    assert b.account().equity == pytest.approx(10000.0 - 2.50 - 1000.0 * (1 - 50000.0 / 50050.0))


def test_sell_fill_math_and_flat_after_full_exit():
    b = make()
    b.submit(buy(notional=1000.0))
    qty = b.positions()[0].qty
    b.set_price("BTC/USD", 55000.0)
    ack = b.submit(sell(qty=qty))
    f = b.fills_for("s1")[0]
    assert f.price == pytest.approx(55000.0 * 0.999)
    assert f.fee_usd == pytest.approx(qty * 55000.0 * 0.999 * 0.0025)
    assert b.positions() == []
    assert ack.status == "filled"
    assert b.total_fees == pytest.approx(2.50 + f.fee_usd)


def test_cannot_go_short():
    b = make()
    b.submit(buy(notional=1000.0))
    qty = b.positions()[0].qty
    with pytest.raises(OrderRejected, match="long or flat"):
        b.submit(sell(qty=qty * 1.5))
    with pytest.raises(OrderRejected, match="no BTC/USD position"):
        b2 = make(); b2.submit(sell(qty=0.01))


def test_float_overshoot_on_exit_is_clamped_to_position():
    b = make()
    b.submit(buy(notional=1000.0))
    qty = b.positions()[0].qty
    b.submit(sell(qty=qty * (1 + 1e-9)))
    assert b.positions() == []


def test_duplicate_client_order_id_rejected():
    b = make()
    b.submit(buy())
    with pytest.raises(DuplicateOrderError):
        b.submit(buy())
    assert len(b.all_fills) == 1


def test_min_notional_and_insufficient_cash():
    b = make(cash=100.0)
    with pytest.raises(OrderRejected, match="below minimum"):
        b.submit(buy(notional=5.0))
    with pytest.raises(OrderRejected, match="insufficient cash"):
        b.submit(buy(notional=100.0))  # fee pushes it over


def test_network_failure_leaves_no_fill_and_unknown_state():
    b = make()
    b.fail_next = "network"
    with pytest.raises(BrokerError):
        b.submit(buy())
    assert b.order_by_client_id("b1") is None
    b.submit(buy())  # retry with same id succeeds because nothing was recorded
    assert b.order_by_client_id("b1").status == "filled"


def test_average_entry_on_increase():
    b = make()
    b.submit(buy("b1", 1000.0))
    b.set_price("BTC/USD", 60000.0)
    b.submit(buy("b2", 1000.0, purpose="increase"))
    pos = b.positions()[0]
    q1, q2 = 1000.0 / 50050.0, 1000.0 / 60060.0
    assert pos.qty == pytest.approx(q1 + q2)
    assert pos.avg_entry == pytest.approx((q1 * 50050.0 + q2 * 60060.0) / (q1 + q2))


def test_no_price_raises():
    b = FakeBroker(starting_cash=1.0, fee_bps=0, slippage_bps=0)
    with pytest.raises(BrokerError, match="no price"):
        b.submit(buy(notional=1.0))


def test_order_request_invariants():
    with pytest.raises(ValueError):
        OrderRequest("c", "d", "BTC/USD", "buy", "entry")  # neither notional nor qty
    with pytest.raises(ValueError):
        OrderRequest("c", "d", "BTC/USD", "buy", "entry", notional_usd=1.0, qty=1.0)
    with pytest.raises(ValueError):
        OrderRequest("c", "d", "BTC/USD", "buy", "exit", qty=1.0)  # exits are sells
    with pytest.raises(ValueError):
        OrderRequest("c", "d", "BTC/USD", "sell", "entry", qty=1.0)  # entries are buys
