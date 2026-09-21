"""Signal generation: conviction from features.

Pure functions. No I/O, no clock reads. Deterministic.

Conviction is an equal-weight ensemble of momentum indicators across lookbacks,
gated by price above a trend-filter MA. Conviction is in [0, 1], where 0 is flat
and 1 is all lookbacks positive.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .config import SignalConfig


def conviction_from_row(row: Mapping[str, float], cfg: SignalConfig) -> float:
    """Compute conviction from a single feature row.

    Parameters
    ----------
    row : Mapping[str, float]
        Feature values keyed by name: close, sma_{n}, ret_{k} for each k.
    cfg : SignalConfig
        Configuration with lookbacks_days and trend_filter_ma_days.

    Returns
    -------
    float in [0, 1]
        Mean of 1.0 (if ret_k > 0) else 0.0, over all lookbacks.
        Returns 0.0 if close < sma (trend gate) or if any required value is NaN.
        Long or flat only; never negative.
    """
    close = row.get("close")
    sma_key = f"sma_{cfg.trend_filter_ma_days}"
    sma = row.get(sma_key)

    # Check for NaN or missing values.
    if close is None or sma is None or (isinstance(close, float) and pd.isna(close)) or (isinstance(sma, float) and pd.isna(sma)):
        return 0.0

    # Trend gate: close must be above SMA (strict).
    if close < sma:
        return 0.0

    # Collect votes for each lookback.
    votes = []
    for k in cfg.lookbacks_days:
        ret_key = f"ret_{k}"
        ret_k = row.get(ret_key)

        # If any ret_k is NaN or missing, return 0.
        if ret_k is None or (isinstance(ret_k, float) and pd.isna(ret_k)):
            return 0.0

        # Vote: 1.0 if strictly positive, else 0.0.
        votes.append(1.0 if ret_k > 0.0 else 0.0)

    # Return mean of votes (ensemble).
    return sum(votes) / len(votes) if votes else 0.0


def conviction_series(features: pd.DataFrame, cfg: SignalConfig) -> pd.Series:
    """Compute conviction for every row in features.

    Parameters
    ----------
    features : pd.DataFrame
        Feature frame with close, ret_{k}, sma_{n}, etc.
    cfg : SignalConfig
        Configuration.

    Returns
    -------
    pd.Series
        Index aligned to features. Values in [0, 1]. No NaN; out-of-band rows are 0.
    """
    return features.apply(lambda row: conviction_from_row(row, cfg), axis=1)


def explain(row: Mapping[str, float], cfg: SignalConfig) -> str:
    """Generate a short deterministic thesis string for a decision.

    Parameters
    ----------
    row : Mapping[str, float]
        Feature values.
    cfg : SignalConfig
        Configuration.

    Returns
    -------
    str
        Short text describing the decision logic, e.g.
        "3 of 4 lookbacks positive; close above 50d SMA".
    """
    close = row.get("close")
    sma_key = f"sma_{cfg.trend_filter_ma_days}"
    sma = row.get(sma_key)

    # Check for NaN or missing values.
    if close is None or sma is None or (isinstance(close, float) and pd.isna(close)) or (isinstance(sma, float) and pd.isna(sma)):
        return "insufficient data"

    # Trend gate check.
    above_sma = "above" if close >= sma else "below"

    # Count positive lookbacks.
    positive_count = 0
    lookback_count = 0
    for k in cfg.lookbacks_days:
        ret_key = f"ret_{k}"
        ret_k = row.get(ret_key)
        if ret_k is not None and not (isinstance(ret_k, float) and pd.isna(ret_k)):
            lookback_count += 1
            if ret_k > 0.0:
                positive_count += 1

    if lookback_count == 0:
        return "insufficient data"

    return f"{positive_count} of {lookback_count} lookbacks positive; close {above_sma} {cfg.trend_filter_ma_days}d SMA"
