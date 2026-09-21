"""Target sizing: conviction + vol -> position weight.

Pure functions. No I/O, no clock reads. Deterministic.

Per-asset vol budget is portfolio_vol_target / n_universe. This equal split of the
portfolio vol budget across the configured universe is the chosen default: it ensures
equal risk contribution given unequal conviction. If conviction and realized vol vary
wildly across assets, this choice will rarely bind in practice; the per-asset and
gross caps usually bind first. See docs/BACKTEST.md for which constraint bound each week.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Target:
    """Target position for one symbol."""

    symbol: str
    weight: float  # portfolio weight, in [0, max_weight_per_asset]
    binding_constraint: str  # "flat" | "vol_target" | "per_asset_cap" | "gross_cap"


def target_weights(
    convictions: dict[str, float],
    realized_vols: dict[str, float],
    cfg,
    n_universe: int,
) -> dict[str, Target]:
    """Derive target weights from conviction and realized volatility.

    Parameters
    ----------
    convictions : dict[str, float]
        Symbol -> conviction in [0, 1].
    realized_vols : dict[str, float]
        Symbol -> realized vol (annualized). May contain NaN or <= 0.
    cfg : SizingConfig
        Configuration with portfolio_vol_target, max_weight_per_asset, max_gross_exposure.
    n_universe : int
        Number of symbols in the universe (for per-asset vol budget).

    Returns
    -------
    dict[str, Target]
        One Target per symbol in convictions. All weights are in [0, max_weight_per_asset].
        Sum of weights <= max_gross_exposure (+ 1e-12 for float rounding).
        Binding constraint is recorded for each weight.

    Raises
    ------
    ValueError
        If n_universe <= 0.
    """
    if n_universe <= 0:
        raise ValueError("n_universe must be > 0")

    # Per-asset vol budget: equal split.
    per_asset_vol_budget = cfg.portfolio_vol_target / n_universe

    # Compute raw weights.
    raw_weights: dict[str, float] = {}
    for symbol, conviction in convictions.items():
        vol = realized_vols.get(symbol, float("nan"))

        # If vol is NaN, <= 0, or conviction is 0, weight is 0 with constraint "flat".
        if conviction == 0.0 or math.isnan(vol) or vol <= 0.0:
            raw_weights[symbol] = 0.0
        else:
            # raw = conviction * (budget / vol)
            raw_weights[symbol] = conviction * (per_asset_vol_budget / vol)

    # Clamp each to max_weight_per_asset.
    clamped_weights: dict[str, float] = {}
    per_asset_cap_hit: set[str] = set()
    for symbol, w in raw_weights.items():
        if w > cfg.max_weight_per_asset:
            clamped_weights[symbol] = cfg.max_weight_per_asset
            per_asset_cap_hit.add(symbol)
        else:
            clamped_weights[symbol] = w

    # Check if gross exposure exceeds cap.
    total_weight = sum(clamped_weights.values())

    # If gross exceeds cap, scale down non-zero weights proportionally.
    final_weights: dict[str, float] = {}
    gross_cap_hit: set[str] = set()

    if total_weight > cfg.max_gross_exposure + 1e-12:
        # Scale all non-zero weights down proportionally.
        scale_factor = cfg.max_gross_exposure / total_weight if total_weight > 0 else 0.0
        for symbol, w in clamped_weights.items():
            final_weights[symbol] = w * scale_factor if w > 0.0 else 0.0
            if w > 0.0:
                gross_cap_hit.add(symbol)
    else:
        final_weights = clamped_weights.copy()

    # Assign binding constraints.
    targets: dict[str, Target] = {}
    for symbol, conviction in convictions.items():
        w = final_weights[symbol]
        vol = realized_vols.get(symbol, float("nan"))

        # Determine binding constraint.
        if conviction == 0.0 or math.isnan(vol) or vol <= 0.0:
            constraint = "flat"
        elif symbol in gross_cap_hit:
            constraint = "gross_cap"
        elif symbol in per_asset_cap_hit:
            constraint = "per_asset_cap"
        else:
            constraint = "vol_target"

        targets[symbol] = Target(symbol=symbol, weight=w, binding_constraint=constraint)

    return targets


def trade_notional(target_weight: float, current_weight: float, equity: float) -> float:
    """Compute signed notional change to reach target.

    Parameters
    ----------
    target_weight : float
        Target portfolio weight [0, 1].
    current_weight : float
        Current portfolio weight [0, 1].
    equity : float
        Current portfolio equity in USD.

    Returns
    -------
    float
        Signed USD change. Positive = buy, negative = sell.
    """
    return (target_weight - current_weight) * equity


def inside_no_trade_band(
    delta_notional: float, equity: float, cfg, min_notional_usd: float
) -> bool:
    """Check if delta is small enough to skip trading.

    Parameters
    ----------
    delta_notional : float
        Signed notional change (USD).
    equity : float
        Current portfolio equity (USD).
    cfg : SizingConfig
        Configuration with no_trade_band_pct_equity.
    min_notional_usd : float
        Broker's minimum notional (USD).

    Returns
    -------
    bool
        True if abs(delta_notional) is below both the band and the minimum.
    """
    band_size = cfg.no_trade_band_pct_equity * equity
    return abs(delta_notional) < band_size or abs(delta_notional) < min_notional_usd


def initial_stop(entry_price: float, atr: float, multiple: float) -> float:
    """Compute the initial stop price for a long entry.

    Parameters
    ----------
    entry_price : float
        Entry price (USD).
    atr : float
        ATR at entry time (USD).
    multiple : float
        Stop distance in ATR multiples.

    Returns
    -------
    float
        Stop price, floored at a small positive number (never <= 0).
    """
    stop = entry_price - multiple * atr
    # Floor at a small positive number to avoid division by zero later.
    return max(stop, 1e-8)


def ratchet_stop(previous_stop: float | None, candidate_stop: float) -> float:
    """Ratchet the stop upward, never downward.

    Parameters
    ----------
    previous_stop : float | None
        Previous stop price, or None if this is the first update.
    candidate_stop : float
        New candidate stop price.

    Returns
    -------
    float
        The higher of the two (or the candidate if previous is None).
    """
    if previous_stop is None:
        return candidate_stop
    return max(previous_stop, candidate_stop)
