"""Feature engineering: bars in, features out.

Pure functions. No I/O, no clock reads. Deterministic.

Input DataFrame must have index = tz-aware UTC DatetimeIndex, sorted ascending,
columns = [open, high, low, close, volume]. Output carries the same index.

Output columns: close, ret_{k} for each k in cfg.lookbacks_days, sma_{cfg.trend_filter_ma_days},
realized_vol, atr. Rows without enough history are NaN.

Warm-up rows use min_history_days(cfg). The value at row t depends only on rows <= t.
"""

from __future__ import annotations

import math

import pandas as pd

from .config import SignalConfig


def compute_features(
    bars: pd.DataFrame, cfg: SignalConfig, trading_days_per_year: int
) -> pd.DataFrame:
    """Compute features from daily bars.

    Parameters
    ----------
    bars : pd.DataFrame
        Index: tz-aware UTC DatetimeIndex, sorted ascending.
        Columns: open, high, low, close, volume.
    cfg : SignalConfig
        Configuration with lookbacks_days, trend_filter_ma_days, vol_window_days, atr_window_days.
    trading_days_per_year : int
        Annualization factor (365 for crypto, 252 for equities).

    Returns
    -------
    pd.DataFrame
        Same index as bars. Columns: close, ret_{k}, sma_{ma_days}, realized_vol, atr.
        Rows with insufficient history are NaN.

    Raises
    ------
    ValueError
        If bars is unsorted, has duplicates, index is naive, or columns are missing.
    """
    # Input validation.
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars.index must be DatetimeIndex")
    if bars.index.tz is None:
        raise ValueError("bars.index must be timezone-aware UTC")
    if not bars.index.is_monotonic_increasing:
        raise ValueError("bars.index must be sorted ascending")
    if bars.index.has_duplicates:
        raise ValueError("bars.index has duplicates")
    required_cols = {"open", "high", "low", "close", "volume"}
    if not required_cols.issubset(bars.columns):
        raise ValueError(f"bars must have columns {required_cols}, got {set(bars.columns)}")

    close = bars["close"]

    # Build output frame with close and trailing returns.
    out = pd.DataFrame({"close": close}, index=bars.index)

    for k in cfg.lookbacks_days:
        # ret_k = close_t / close_{t-k} - 1
        # Shift by k to get close at t-k, then divide current by that.
        out[f"ret_{k}"] = close / close.shift(k) - 1

    # SMA: simple moving average of close.
    out[f"sma_{cfg.trend_filter_ma_days}"] = close.rolling(cfg.trend_filter_ma_days).mean()

    # Realized vol: std of daily returns, annualized.
    # Daily returns: close_t / close_{t-1} - 1
    daily_returns = close / close.shift(1) - 1
    # Rolling std with ddof=1 (sample std).
    realized_vol_daily = daily_returns.rolling(cfg.vol_window_days).std(ddof=1)
    # Annualize.
    annualization = math.sqrt(trading_days_per_year)
    out["realized_vol"] = realized_vol_daily * annualization

    # ATR: simple moving average of true range.
    # True range = max(high - low, abs(high - prev_close), abs(low - prev_close))
    high = bars["high"]
    low = bars["low"]
    prev_close = close.shift(1)

    # Compute each term. NaN will propagate correctly with skipna=False.
    tr_1 = high - low
    tr_2 = (high - prev_close).abs()
    tr_3 = (low - prev_close).abs()

    # True range: max of the three, with skipna=False to preserve NaN.
    tr = pd.concat([tr_1, tr_2, tr_3], axis=1).max(axis=1, skipna=False)

    # Moving average of TR.
    out["atr"] = tr.rolling(cfg.atr_window_days).mean()

    return out


def min_history_days(cfg: SignalConfig) -> int:
    """Minimum bars needed before the first row has all features.

    Consistent with SignalConfig.min_history_days property.
    """
    return cfg.min_history_days
