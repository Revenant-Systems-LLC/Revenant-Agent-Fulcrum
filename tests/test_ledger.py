from datetime import timedelta

import pytest

from fulcrum.ledger import Ledger
from fulcrum.models import Decision, Fill, FilterResult, OrderRequest, Position, Verdict, utcnow
from tests.conftest import utc


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(tmp_path / "l.sqlite")
    yield lg
    lg.close()


def _req(cid="c1", purpose="entry", side="buy", notional=100.0):
    return OrderRequest(client_order_id=cid, decision_id="d1", symbol="BTC/USD", side=side, purpose=purpose,
                        notional_usd=notional)


def test_schema_created_and_reopenable(tmp_path):
    p = tmp_path / "l.sqlite"
    Ledger(p).close()
    lg = Ledger(p)
    counts = lg.table_counts()
    assert {"runs", "decisions", "orders", "order_events", "fills", "equity_curve", "ghost_book",
            "kill_switch_state", "kill_switch_events", "alerts", "shadow_daily", "filter_verdicts",
            "positions_snapshot", "reset_consumed"} <= set(counts)
    assert counts["kill_switch_state"] == 1
    assert lg.kill_state()["tripped"] == 0
    lg.close()


def test_order_intent_is_idempotent(ledger):
    now = utcnow()
    assert ledger.record_order_intent(_req(), decided_at=now) is True
    assert ledger.record_order_intent(_req(), decided_at=now) is False  # same client id: already acted on
    assert len(ledger.orders()) == 1
    assert ledger.order("c1")["status"] == "new"


def test_order_update_appends_events(ledger):
    now = utcnow()
    ledger.record_order_intent(_req(), decided_at=now)
    ledger.update_order("c1", "accepted", broker_order_id="b1", submitted_at=utc(2026, 1, 1))
    ledger.update_order("c1", "filled", note="fill")
    row = ledger.order("c1")
    assert row["status"] == "filled" and row["broker_order_id"] == "b1"
    events = ledger._conn.execute("SELECT status FROM order_events WHERE client_order_id='c1' ORDER BY event_id").fetchall()
    assert [e["status"] for e in events] == ["new", "accepted", "filled"]
    assert ledger.open_orders() == []


def test_update_unknown_order_raises(ledger):
    with pytest.raises(KeyError):
        ledger.update_order("nope", "filled")


def test_24h_cap_counts_only_risk_increasing_non_rejected(ledger):
    now = utc(2026, 1, 2, 12)
    ledger.record_order_intent(_req("e1", "entry"), decided_at=now)
    ledger.record_order_intent(_req("e2", "increase"), decided_at=now)
    ledger.record_order_intent(_req("x1", "exit", "sell"), decided_at=now)
    ledger.record_order_intent(_req("s1", "stop", "sell"), decided_at=now)
    ledger.record_order_intent(_req("e3", "entry"), decided_at=now)
    ledger.update_order("e3", "rejected")
    assert ledger.risk_increasing_orders_since(now - timedelta(days=365)) == 2


def test_fills_and_fees(ledger):
    ledger.record_fill(Fill("c1", "BTC/USD", "buy", 0.001, 50000.0, 0.125, utc(2026, 1, 1, 1)))
    ledger.record_fill(Fill("c2", "ETH/USD", "buy", 0.01, 3000.0, 0.075, utc(2026, 1, 3, 1)))
    assert len(ledger.fills()) == 2
    assert len(ledger.fills(symbol="BTC/USD")) == 1
    assert ledger.fees_since(utc(2026, 1, 2)) == pytest.approx(0.075)
    assert ledger.fees_since(utc(2025, 1, 1)) == pytest.approx(0.2)


def test_filter_results_fail_open_rows(ledger):
    ok = FilterResult(verdicts=(Verdict("BTC/USD", "veto", "r", 0.9, "inv"),), error=None, latency_ms=5)
    ledger.record_filter_result(utc(2026, 1, 1), "shadow", ok, applied=False, candidates=("BTC/USD",))
    bad = FilterResult(verdicts=(), error="timeout", latency_ms=30000)
    ledger.record_filter_result(utc(2026, 1, 8), "active", bad, applied=True, candidates=("BTC/USD", "ETH/USD"))
    rows = ledger.filter_verdicts()
    assert len(rows) == 3
    assert rows[0]["verdict"] == "veto" and rows[0]["applied"] == 0
    assert all(r["error"] == "timeout" and r["verdict"] == "allow" for r in rows[1:])


def test_kill_state_and_events(ledger):
    ledger.set_kill_state(tripped=True, hwm=1000.0, tripped_at=utc(2026, 2, 1), drawdown_at_trip=0.16)
    ledger.record_kill_event(utc(2026, 2, 1), "trip", 1000.0, 0.16, "dd breached")
    st = ledger.kill_state()
    assert st["tripped"] == 1 and st["hwm"] == 1000.0
    assert ledger.kill_events()[0]["kind"] == "trip"
    assert not ledger.reset_already_consumed(123)
    ledger.mark_reset_consumed(123)
    assert ledger.reset_already_consumed(123)


def test_decisions_equity_ghost_alerts_snapshot(ledger):
    d = Decision("d1", utc(2026, 1, 5), "BTC/USD", 0.75, 0.12, 0.0, "buy", "momentum up", 45000.0, "close < 45000",
                 "per_asset_cap")
    ledger.start_run("r1", "dry_run", "abcd", None, utc(2026, 1, 5))
    ledger.record_decision(d, "r1")
    assert ledger.latest_decision("BTC/USD")["stop_price"] == 45000.0
    ledger.record_equity(utc(2026, 1, 5), 1000.0, 880.0, 0.12, 1000.0, 0.0)
    ledger.record_ghost(utc(2026, 1, 5), "real", 1000.0, 880.0, {"BTC/USD": 0.002})
    ledger.record_ghost(utc(2026, 1, 5), "counterfactual", 1000.0, 1000.0, {})
    with pytest.raises(ValueError):
        ledger.record_ghost(utc(2026, 1, 5), "imaginary", 1.0, 1.0, {})
    ledger.record_alert(utc(2026, 1, 5), "stale_data", {"age_min": 400})
    ledger.snapshot_positions([Position("BTC/USD", 0.002, 50000.0, 100.0)], {"BTC/USD": 45000.0}, {"BTC/USD": "d1"},
                              utc(2026, 1, 5))
    assert len(ledger.equity_curve()) == 1
    assert len(ledger.ghost_book("real")) == 1 and len(ledger.ghost_book()) == 2
    assert ledger.alerts(kind="stale_data")[0]["kind"] == "stale_data"
    assert ledger.positions_snapshot()[0]["stop_price"] == 45000.0


def test_naive_datetime_rejected(ledger):
    from datetime import datetime
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.record_equity(datetime(2026, 1, 1), 1.0, 1.0, 0.0, 1.0, 0.0)
