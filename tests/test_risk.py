"""Tests for risk.py.

These tests verify the risk gate, KillSwitch behavior, and the invariants
that keep the strategy safe. Property tests verify cap compliance and
exit approval over thousands of random iterations.
"""

from __future__ import annotations

import random
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from fulcrum.models import (
    OrderRequest, PortfolioState, Position, RISK_REDUCING_PURPOSES,
)
from fulcrum.risk import (
    make_client_order_id, check_order, KillSwitch, fee_budget_exceeded,
    slippage_alarm, bars_are_stale,
)
from tests.conftest import utc


def test_make_client_order_id_deterministic():
    """Same inputs produce same ID."""
    id1 = make_client_order_id("run1", "dec1", "BTC", "buy", "entry", 0)
    id2 = make_client_order_id("run1", "dec1", "BTC", "buy", "entry", 0)

    assert id1 == id2


def test_make_client_order_id_different_on_seq():
    """Different seq produces different ID."""
    id1 = make_client_order_id("run1", "dec1", "BTC", "buy", "entry", 0)
    id2 = make_client_order_id("run1", "dec1", "BTC", "buy", "entry", 1)

    assert id1 != id2


def test_make_client_order_id_truncation():
    """Truncation to max_len works (default 48 is well under SHA1's 40 chars)."""
    id_default = make_client_order_id("run1", "dec1", "BTC", "buy", "entry", 0)
    id_short = make_client_order_id("run1", "dec1", "BTC", "buy", "entry", 0, max_len=20)

    assert len(id_short) == 20
    assert id_default.startswith(id_short)


def test_check_order_exit_always_approved():
    """Exit orders are always approved, regardless of kill switch or caps."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="sell",
        purpose="exit", qty=1.0,
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=10000.0, cash=5000.0,
        positions={"BTC": Position("BTC", 1.0, 50000.0, 60000.0)},
        trades_last_24h=100,  # Well over the cap
        high_water_mark=15000.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=True)

    assert decision.kind == "approve"
    assert decision.orders == (req,)


def test_check_order_stop_always_approved():
    """Stop orders are always approved, regardless of kill switch or caps."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="sell",
        purpose="stop", qty=1.0,
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=10000.0, cash=5000.0,
        positions={"BTC": Position("BTC", 1.0, 50000.0, 60000.0)},
        trades_last_24h=100,
        high_water_mark=15000.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=True)

    assert decision.kind == "approve"


def test_check_order_kill_switch_reject():
    """Entry rejected when kill switch is tripped."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="buy",
        purpose="entry", notional_usd=5000.0,
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=100000.0, cash=50000.0,
        positions={},
        trades_last_24h=0,
        high_water_mark=100000.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=True)

    assert decision.kind == "reject"
    assert "kill switch" in decision.reason


def test_check_order_24h_cap_reject():
    """Entry rejected when 24h trade cap is exceeded."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="buy",
        purpose="entry", notional_usd=5000.0,
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=100000.0, cash=50000.0,
        positions={},
        trades_last_24h=10,  # Equal to cap
        high_water_mark=100000.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=False)

    assert decision.kind == "reject"
    assert "24h" in decision.reason


def test_check_order_clamp_order_pct():
    """Order is clamped by max_order_pct_equity."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="buy",
        purpose="entry", notional_usd=10000.0,  # 10% of equity
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=100000.0, cash=60000.0,
        positions={},
        trades_last_24h=0,
        high_water_mark=100000.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=False)

    assert decision.kind == "clamp"
    # Clamped to 5% = 5000
    assert decision.orders[0].notional_usd == pytest.approx(5000.0)
    assert "order clamp" in decision.reason


def test_check_order_clamp_min_notional_reject():
    """Order clamped below min notional is rejected."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="buy",
        purpose="entry", notional_usd=100.0,
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=100.0, cash=60.0,  # Very small account
        positions={},
        trades_last_24h=0,
        high_water_mark=100.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=False)

    assert decision.kind == "reject"


def test_check_order_approve_within_limits():
    """Order is approved when within all limits."""
    req = OrderRequest(
        client_order_id="id1", decision_id="dec1", symbol="BTC", side="buy",
        purpose="entry", notional_usd=3000.0,  # 3% of equity
    )

    state = PortfolioState(
        as_of=utc(2025, 1, 1), equity=100000.0, cash=60000.0,
        positions={},
        trades_last_24h=0,
        high_water_mark=100000.0,
        prices={"BTC": 60000.0},
    )

    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig
    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=False)

    assert decision.kind == "approve"
    # Original request object should be returned unchanged.
    assert decision.orders[0] is req


def test_check_order_property_loop():
    """Property test: every approve/clamp respects caps, every exit is approved."""
    from fulcrum.config import RiskConfig, SizingConfig, AssetClassConfig

    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )
    sizing_cfg = SizingConfig(
        portfolio_vol_target=0.20, max_weight_per_asset=0.15,
        max_gross_exposure=0.60, no_trade_band_pct_equity=0.02,
    )
    asset_cfg = AssetClassConfig(
        name="crypto", trading_days_per_year=365, calendar="24x7",
        taker_fee_bps=25.0, maker_fee_bps=15.0, slippage_bps=10.0,
        min_notional_usd=10.0, quote_currency="USD",
    )

    rng = random.Random(1234)

    for i in range(5000):
        # Generate random state.
        equity = 10000.0 + rng.random() * 100000.0
        cash = 0.1 * equity + rng.random() * 0.4 * equity

        positions_dict = {}
        if rng.random() > 0.5:
            positions_dict["BTC"] = Position("BTC", 1.0, 50000.0, rng.random() * 0.3 * equity)
        if rng.random() > 0.6:
            positions_dict["ETH"] = Position("ETH", 2.0, 3000.0, rng.random() * 0.2 * equity)

        trades_24h = rng.randint(0, 20)

        prices = {"BTC": 60000.0, "ETH": 3000.0}

        state = PortfolioState(
            as_of=utc(2025, 1, 1), equity=equity, cash=cash,
            positions=positions_dict, trades_last_24h=trades_24h,
            high_water_mark=equity + rng.random() * 10000.0, prices=prices,
        )

        # Test exit orders.
        if positions_dict:
            for symbol in positions_dict:
                req = OrderRequest(
                    client_order_id=f"id{i}_{symbol}", decision_id=f"dec{i}",
                    symbol=symbol, side="sell", purpose="exit", qty=0.5,
                )

                decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=True)

                # Exit must be approved regardless of kill state.
                assert decision.kind == "approve", f"iteration {i}: exit not approved"

        # Test entry.
        req = OrderRequest(
            client_order_id=f"id{i}_entry", decision_id=f"dec{i}",
            symbol="BTC", side="buy", purpose="entry",
            notional_usd=rng.random() * 0.2 * equity,
        )

        decision = check_order(req, state, risk_cfg, sizing_cfg, asset_cfg, kill_tripped=False)

        # Check invariants.
        if decision.kind in ("approve", "clamp"):
            for order in decision.orders:
                # Compute post-trade exposure.
                if order.notional_usd:
                    notional = order.notional_usd
                else:
                    notional = order.qty * prices[order.symbol]

                post_gross = (state.gross_exposure * state.equity + notional) / state.equity
                post_weight = (state.weight(order.symbol) * state.equity + notional) / state.equity

                assert post_weight <= sizing_cfg.max_weight_per_asset + 1e-9, \
                    f"iteration {i}: post-trade weight {post_weight} exceeds cap"
                assert post_gross <= sizing_cfg.max_gross_exposure + 1e-9, \
                    f"iteration {i}: post-trade gross {post_gross} exceeds cap"


def test_kill_switch_init_and_trip():
    """KillSwitch initializes hwm on first update, trips on threshold."""
    from fulcrum.ledger import Ledger

    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Ledger(Path(tmpdir) / "test.sqlite")

        from fulcrum.config import RiskConfig
        risk_cfg = RiskConfig(
            max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
            kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
            slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
        )

        ks = KillSwitch(ledger, risk_cfg, Path(tmpdir) / "reset")

        # First update: initialize hwm.
        ts1 = utc(2025, 1, 1)
        tripped1 = ks.update(100.0, ts1)
        assert not tripped1
        assert not ks.tripped()

        # Drawdown < 15%: no trip.
        ts2 = utc(2025, 1, 2)
        tripped2 = ks.update(87.0, ts2)  # 13% drawdown
        assert not tripped2
        assert not ks.tripped()

        # Drawdown >= 15%: trip.
        ts3 = utc(2025, 1, 3)
        tripped3 = ks.update(85.0, ts3)  # 15% drawdown
        assert tripped3
        assert ks.tripped()

        # Already tripped: no more trips.
        ts4 = utc(2025, 1, 4)
        tripped4 = ks.update(70.0, ts4)  # 30% drawdown
        assert not tripped4
        assert ks.tripped()

        ledger.close()


def test_kill_switch_ratchet_hwm():
    """HWM ratchets up but never down."""
    from fulcrum.ledger import Ledger

    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Ledger(Path(tmpdir) / "test.sqlite")

        from fulcrum.config import RiskConfig
        risk_cfg = RiskConfig(
            max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
            kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
            slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
        )

        ks = KillSwitch(ledger, risk_cfg, Path(tmpdir) / "reset")

        ts1 = utc(2025, 1, 1)
        ks.update(100.0, ts1)

        state1 = ledger.kill_state()
        assert state1["hwm"] == 100.0

        # Equity up: HWM ratchets.
        ts2 = utc(2025, 1, 2)
        ks.update(110.0, ts2)

        state2 = ledger.kill_state()
        assert state2["hwm"] == 110.0

        # Equity down: HWM stays the same.
        ts3 = utc(2025, 1, 3)
        ks.update(100.0, ts3)

        state3 = ledger.kill_state()
        assert state3["hwm"] == 110.0

        ledger.close()


def test_kill_switch_consume_reset_file():
    """Reset file is consumed only once per mtime."""
    from fulcrum.ledger import Ledger

    tmpdir = tempfile.mkdtemp()
    try:
        tmppath = Path(tmpdir)
        reset_file = tmppath / "reset"
        reset_file.touch()

        ledger = Ledger(tmppath / "test.sqlite")

        from fulcrum.config import RiskConfig
        risk_cfg = RiskConfig(
            max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
            kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
            slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
        )

        ks = KillSwitch(ledger, risk_cfg, reset_file)

        # Initialize and trip.
        ts1 = utc(2025, 1, 1)
        ks.update(100.0, ts1)

        ts2 = utc(2025, 1, 2)
        ks.update(84.0, ts2)  # 16% drawdown, trips

        assert ks.tripped()

        # Consume reset file.
        ts3 = utc(2025, 1, 3)
        consumed1 = ks.consume_reset_file(90.0, ts3)
        assert consumed1
        assert not ks.tripped()

        # Try to consume the same file again: should fail (file already consumed with same mtime).
        # But we can trip again by dropping enough from the new HWM (90.0).
        # 15% drawdown from 90 = 76.5. Drop below that.
        ts4 = utc(2025, 1, 4)
        ks.update(76.0, ts4)  # 15.6% drawdown from new HWM of 90
        assert ks.tripped()

        # Try to consume the same file again: should fail because it's already been consumed.
        ts5 = utc(2025, 1, 5)
        consumed2 = ks.consume_reset_file(76.0, ts5)
        assert not consumed2
        assert ks.tripped()

        ledger.close()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_kill_switch_no_file_operations():
    """KillSwitch never creates, writes, or deletes files. Source scan test."""
    import inspect

    source = inspect.getsource(KillSwitch)

    forbidden_substrings = [
        "open(",
        "write_text",
        "touch(",
        "unlink",
        "remove(",
        "rename(",
    ]

    for substring in forbidden_substrings:
        assert substring not in source, \
            f"KillSwitch source contains forbidden operation: {substring}"


def test_fee_budget_exceeded():
    """Annualized fee check."""
    from fulcrum.config import RiskConfig

    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )

    # 100 USD fees over 10 days, equity 10000.
    # Annualized = 100 * 365 / 10 = 3650 = 36.5% of equity.
    # Budget = 3% of 10000 = 300.
    exceeded = fee_budget_exceeded(100.0, 10, 10000.0, risk_cfg)
    assert exceeded is True

    # 10 USD fees over 100 days, equity 10000.
    # Annualized = 10 * 365 / 100 = 36.5 = 0.365% of equity.
    # Budget = 3% of 10000 = 300.
    exceeded2 = fee_budget_exceeded(10.0, 100, 10000.0, risk_cfg)
    assert exceeded2 is False


def test_slippage_alarm():
    """Slippage threshold check."""
    from fulcrum.config import RiskConfig

    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )

    # Realized 25 bps, modeled 10 bps.
    # 25 > 2.0 * 10 = 20? Yes.
    alarm = slippage_alarm(25.0, risk_cfg, 10.0)
    assert alarm is True

    # Realized 15 bps, modeled 10 bps.
    # 15 > 2.0 * 10 = 20? No.
    alarm2 = slippage_alarm(15.0, risk_cfg, 10.0)
    assert alarm2 is False


def test_bars_are_stale():
    """Stale bar detection."""
    from fulcrum.config import RiskConfig

    risk_cfg = RiskConfig(
        max_order_pct_equity=0.05, max_trades_per_24h=10, stop_atr_multiple=2.5,
        kill_switch_drawdown=0.15, fee_budget_annual_pct_equity=0.03,
        slippage_alarm_multiple=2.0, stale_bar_max_age_minutes=180,
    )

    bar_open = utc(2025, 1, 1, 0, 0)  # 1440 minute bar closes at end of day
    now = utc(2025, 1, 1, 12, 0)  # 12 hours later

    # Bar close would be at 2025-01-02 00:00. Age = 12 hours = 720 minutes < 180? No, wait.
    # Bar open at 2025-01-01 00:00, bar close at 2025-01-02 00:00.
    # now = 2025-01-01 12:00.
    # age = (2025-01-01 12:00 - 2025-01-02 00:00).total_seconds() / 60
    # This is negative! The bar hasn't closed yet.

    # Better test: bar opened at 2025-01-01 00:00, closed at 2025-01-02 00:00.
    # now is 2025-01-02 05:00 (5 hours after close).
    now_later = utc(2025, 1, 2, 5, 0)
    stale = bars_are_stale(bar_open, now_later, risk_cfg, bar_minutes=1440)
    # Age = 300 minutes > 180? Yes.
    assert stale is True

    # now is 2025-01-02 00:30 (30 minutes after close).
    now_early = utc(2025, 1, 2, 0, 30)
    stale2 = bars_are_stale(bar_open, now_early, risk_cfg, bar_minutes=1440)
    # Age = 30 minutes < 180? Yes.
    assert stale2 is False
