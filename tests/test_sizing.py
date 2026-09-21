"""Tests for sizing.py.

These tests verify correct weight computation, cap application, and no-trade-band logic.
Property tests with random.Random(1234) verify invariants over thousands of iterations.
"""

from __future__ import annotations

import math
import random

import pytest

from fulcrum.sizing import (
    Target, target_weights, trade_notional, inside_no_trade_band,
    initial_stop, ratchet_stop,
)


def test_target_weights_all_flat():
    """All symbols get zero weight with 'flat' constraint when conviction is 0."""
    from fulcrum.config import SizingConfig

    cfg = SizingConfig(
        portfolio_vol_target=0.20,
        max_weight_per_asset=0.15,
        max_gross_exposure=0.60,
        no_trade_band_pct_equity=0.02,
    )

    convictions = {"BTC": 0.0, "ETH": 0.0}
    realized_vols = {"BTC": 0.30, "ETH": 0.25}

    targets = target_weights(convictions, realized_vols, cfg, n_universe=2)

    assert targets["BTC"].weight == 0.0
    assert targets["BTC"].binding_constraint == "flat"
    assert targets["ETH"].weight == 0.0
    assert targets["ETH"].binding_constraint == "flat"


def test_target_weights_nan_vol():
    """NaN vol results in 'flat' weight."""
    from fulcrum.config import SizingConfig

    cfg = SizingConfig(
        portfolio_vol_target=0.20,
        max_weight_per_asset=0.15,
        max_gross_exposure=0.60,
        no_trade_band_pct_equity=0.02,
    )

    convictions = {"BTC": 0.5, "ETH": 0.5}
    realized_vols = {"BTC": float("nan"), "ETH": 0.25}

    targets = target_weights(convictions, realized_vols, cfg, n_universe=2)

    assert targets["BTC"].weight == 0.0
    assert targets["BTC"].binding_constraint == "flat"
    assert targets["ETH"].weight == 0.15
    assert targets["ETH"].binding_constraint == "per_asset_cap"


def test_target_weights_hand_computed():
    """Hand-computed weights for a simple 2-symbol example.

    portfolio_vol_target = 0.20, n_universe = 2
    per_asset_vol_budget = 0.20 / 2 = 0.10

    BTC: conviction = 0.5, realized_vol = 0.30
      raw = 0.5 * (0.10 / 0.30) = 0.5 * 0.333... = 0.1667
      clamp to max_weight_per_asset (0.15) -> 0.15

    ETH: conviction = 0.75, realized_vol = 0.25
      raw = 0.75 * (0.10 / 0.25) = 0.75 * 0.40 = 0.30
      clamp to max_weight_per_asset (0.15) -> 0.15

    total = 0.15 + 0.15 = 0.30, exceeds max_gross_exposure (0.60)? No, it's within.
    So both stay at 0.15.
    """
    from fulcrum.config import SizingConfig

    cfg = SizingConfig(
        portfolio_vol_target=0.20,
        max_weight_per_asset=0.15,
        max_gross_exposure=0.60,
        no_trade_band_pct_equity=0.02,
    )

    convictions = {"BTC": 0.5, "ETH": 0.75}
    realized_vols = {"BTC": 0.30, "ETH": 0.25}

    targets = target_weights(convictions, realized_vols, cfg, n_universe=2)

    # Both should hit the per-asset cap.
    assert targets["BTC"].weight == 0.15
    assert targets["BTC"].binding_constraint == "per_asset_cap"
    assert targets["ETH"].weight == 0.15
    assert targets["ETH"].binding_constraint == "per_asset_cap"


def test_target_weights_gross_cap_scales():
    """When gross exposure exceeds cap, scale down proportionally."""
    from fulcrum.config import SizingConfig

    cfg = SizingConfig(
        portfolio_vol_target=0.20,
        max_weight_per_asset=0.50,  # loose per-asset cap
        max_gross_exposure=0.30,    # tight gross cap
        no_trade_band_pct_equity=0.02,
    )

    convictions = {"BTC": 1.0, "ETH": 1.0}
    realized_vols = {"BTC": 0.20, "ETH": 0.20}

    targets = target_weights(convictions, realized_vols, cfg, n_universe=2)

    # per_asset_vol_budget = 0.20 / 2 = 0.10
    # raw = 1.0 * (0.10 / 0.20) = 0.50 for each
    # total_raw = 0.50 + 0.50 = 1.0, exceeds max_gross_exposure (0.30)
    # scale_factor = 0.30 / 1.0 = 0.30
    # final = 0.50 * 0.30 = 0.15 each

    assert targets["BTC"].weight == pytest.approx(0.15, abs=1e-9)
    assert targets["BTC"].binding_constraint == "gross_cap"
    assert targets["ETH"].weight == pytest.approx(0.15, abs=1e-9)
    assert targets["ETH"].binding_constraint == "gross_cap"

    total = targets["BTC"].weight + targets["ETH"].weight
    assert total <= cfg.max_gross_exposure + 1e-9


def test_target_weights_n_universe_zero():
    """Reject n_universe <= 0."""
    from fulcrum.config import SizingConfig

    cfg = SizingConfig(
        portfolio_vol_target=0.20,
        max_weight_per_asset=0.15,
        max_gross_exposure=0.60,
        no_trade_band_pct_equity=0.02,
    )

    convictions = {"BTC": 0.5}
    realized_vols = {"BTC": 0.30}

    with pytest.raises(ValueError, match="n_universe"):
        target_weights(convictions, realized_vols, cfg, n_universe=0)


def test_target_weights_property_loop():
    """Property test: all weights in [0, max_weight_per_asset], sum <= gross_cap."""
    from fulcrum.config import SizingConfig

    cfg = SizingConfig(
        portfolio_vol_target=0.20,
        max_weight_per_asset=0.15,
        max_gross_exposure=0.60,
        no_trade_band_pct_equity=0.02,
    )

    rng = random.Random(1234)

    for i in range(2000):
        n = rng.randint(2, 8)
        symbols = [f"SYM{j}" for j in range(n)]

        convictions = {s: rng.random() for s in symbols}
        realized_vols = {s: 0.1 + rng.random() * 0.4 for s in symbols}

        targets = target_weights(convictions, realized_vols, cfg, n_universe=n)

        # Check each weight is in [0, max].
        for target in targets.values():
            assert 0.0 <= target.weight <= cfg.max_weight_per_asset + 1e-9, \
                f"iteration {i}: weight {target.weight} out of range"

        # Check total is within gross cap.
        total = sum(t.weight for t in targets.values())
        assert total <= cfg.max_gross_exposure + 1e-9, \
            f"iteration {i}: total {total} exceeds gross cap {cfg.max_gross_exposure}"


def test_trade_notional():
    """Compute signed notional change."""
    delta = trade_notional(target_weight=0.10, current_weight=0.05, equity=100000.0)

    # change = (0.10 - 0.05) * 100000 = 5000
    assert delta == pytest.approx(5000.0)


def test_trade_notional_sell():
    """Negative notional when target < current."""
    delta = trade_notional(target_weight=0.05, current_weight=0.10, equity=100000.0)

    # change = (0.05 - 0.10) * 100000 = -5000
    assert delta == pytest.approx(-5000.0)


def test_inside_no_trade_band_below_band():
    """Inside band: abs(delta) < band size."""
    inside = inside_no_trade_band(
        delta_notional=100.0,
        equity=100000.0,
        cfg=type("cfg", (), {"no_trade_band_pct_equity": 0.02})(),
        min_notional_usd=10.0,
    )

    # band = 0.02 * 100000 = 2000
    # abs(100) < 2000: True
    assert inside is True


def test_inside_no_trade_band_outside_band():
    """Outside band: abs(delta) >= band size and >= min notional."""
    inside = inside_no_trade_band(
        delta_notional=3000.0,
        equity=100000.0,
        cfg=type("cfg", (), {"no_trade_band_pct_equity": 0.02})(),
        min_notional_usd=10.0,
    )

    # band = 0.02 * 100000 = 2000
    # abs(3000) >= 2000: outside
    assert inside is False


def test_inside_no_trade_band_min_notional():
    """Inside band if below min notional regardless of band."""
    inside = inside_no_trade_band(
        delta_notional=5.0,
        equity=100000.0,
        cfg=type("cfg", (), {"no_trade_band_pct_equity": 0.02})(),
        min_notional_usd=10.0,
    )

    # abs(5.0) < 10.0: inside
    assert inside is True


def test_initial_stop():
    """Stop price is entry_price - multiple * atr, floored at 1e-8."""
    stop = initial_stop(entry_price=100.0, atr=5.0, multiple=2.5)

    expected = 100.0 - 2.5 * 5.0  # 100 - 12.5 = 87.5
    assert stop == pytest.approx(expected)


def test_initial_stop_negative_clamped():
    """Negative stop is clamped to 1e-8."""
    stop = initial_stop(entry_price=1.0, atr=10.0, multiple=2.5)

    # 1.0 - 2.5 * 10.0 = 1.0 - 25.0 = -24.0, clamped to 1e-8
    assert stop == 1e-8


def test_ratchet_stop_first_time():
    """First time: return candidate."""
    stop = ratchet_stop(previous_stop=None, candidate_stop=50.0)

    assert stop == 50.0


def test_ratchet_stop_move_up():
    """New candidate is higher: ratchet up."""
    stop = ratchet_stop(previous_stop=45.0, candidate_stop=50.0)

    assert stop == 50.0


def test_ratchet_stop_move_down():
    """New candidate is lower: keep previous (never move down)."""
    stop = ratchet_stop(previous_stop=50.0, candidate_stop=45.0)

    assert stop == 50.0
