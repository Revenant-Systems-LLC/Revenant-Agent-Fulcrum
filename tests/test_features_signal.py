"""Tests for features.py and signal.py.

These tests use hand-computed values for tiny synthetic series to verify correctness.
The lookahead guard ensures that features at time t depend only on data at times <= t.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from fulcrum.features import compute_features, min_history_days
from fulcrum.signal import conviction_from_row, conviction_series, explain
from tests.conftest import utc


@pytest.fixture
def simple_bars():
    """Tiny 10-bar series for hand-computed testing."""
    dates = pd.date_range("2025-01-01", periods=10, freq="D", tz="UTC")
    # Prices: 100, 102, 101, 105, 103, 107, 109, 108, 110, 112
    data = {
        "open": [100.0, 102.0, 101.0, 105.0, 103.0, 107.0, 109.0, 108.0, 110.0, 112.0],
        "high": [101.0, 103.0, 102.0, 106.0, 104.0, 108.0, 110.0, 109.0, 111.0, 113.0],
        "low": [99.0, 101.0, 100.0, 104.0, 102.0, 106.0, 108.0, 107.0, 109.0, 111.0],
        "close": [100.0, 102.0, 101.0, 105.0, 103.0, 107.0, 109.0, 108.0, 110.0, 112.0],
        "volume": [1000.0] * 10,
    }
    return pd.DataFrame(data, index=dates)


def test_features_input_validation_naive_index():
    """Reject naive (tz-unaware) index."""
    dates = pd.date_range("2025-01-01", periods=5, freq="D")  # naive
    data = {
        "open": [100.0] * 5,
        "high": [101.0] * 5,
        "low": [99.0] * 5,
        "close": [100.0] * 5,
        "volume": [1000.0] * 5,
    }
    df = pd.DataFrame(data, index=dates)

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    with pytest.raises(ValueError, match="timezone-aware"):
        compute_features(df, cfg, 365)


def test_features_input_validation_unsorted():
    """Reject unsorted index."""
    dates = pd.DatetimeIndex(
        ["2025-01-03", "2025-01-01", "2025-01-02"],
        tz="UTC",
    )
    data = {
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.0, 101.0, 102.0],
        "volume": [1000.0] * 3,
    }
    df = pd.DataFrame(data, index=dates)

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    with pytest.raises(ValueError, match="sorted"):
        compute_features(df, cfg, 365)


def test_features_input_validation_missing_columns():
    """Reject missing required columns."""
    dates = pd.date_range("2025-01-01", periods=5, freq="D", tz="UTC")
    data = {"close": [100.0] * 5, "volume": [1000.0] * 5}  # missing open, high, low
    df = pd.DataFrame(data, index=dates)

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    with pytest.raises(ValueError, match="columns"):
        compute_features(df, cfg, 365)


def test_features_close_passthrough(simple_bars):
    """Close column is passed through unchanged."""
    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    features = compute_features(simple_bars, cfg, 365)

    pd.testing.assert_series_equal(features["close"], simple_bars["close"])


def test_features_trailing_returns(simple_bars):
    """Trailing returns are computed correctly.

    ret_7 at index 9 = close[9] / close[2] - 1 = 112 / 101 - 1 = 0.1089...
    Rows 0-6 have NaN (not enough history).
    """
    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    features = compute_features(simple_bars, cfg, 365)

    # Rows 0-6: NaN.
    assert features["ret_7"].isna().sum() == 7

    # Row 7: ret_7 = 108 / 100 - 1 = 0.08
    expected_7 = 108.0 / 100.0 - 1
    np.testing.assert_almost_equal(features["ret_7"].iloc[7], expected_7)

    # Row 9: ret_7 = 112 / 101 - 1 = 0.108910...
    expected_9 = 112.0 / 101.0 - 1
    np.testing.assert_almost_equal(features["ret_7"].iloc[9], expected_9)


def test_features_sma(simple_bars):
    """SMA is computed correctly with NaN warm-up.

    SMA-50 at index 49 = mean of closes[0:50]. Before that: NaN.
    """
    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    features = compute_features(simple_bars, cfg, 365)

    # Only 10 bars, so SMA-50 should be all NaN.
    assert features["sma_50"].isna().all()


def test_features_realized_vol_annualized():
    """Realized vol is computed and annualized correctly.

    For a simple series, hand-compute std of daily returns and annualize.
    """
    dates = pd.date_range("2025-01-01", periods=35, freq="D", tz="UTC")
    closes = np.linspace(100.0, 110.0, 35)  # gentle uptrend

    data = {
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": [1000.0] * 35,
    }
    df = pd.DataFrame(data, index=dates)

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    features = compute_features(df, cfg, 365)

    # At index 30 (0-indexed), we have 30 daily returns to compute std from.
    # Daily returns: close[1:31] / close[0:30] - 1
    daily_rets = closes[1:31] / closes[0:30] - 1
    expected_vol = np.std(daily_rets, ddof=1) * math.sqrt(365)

    np.testing.assert_almost_equal(features["realized_vol"].iloc[30], expected_vol)


def test_features_atr_with_skipna():
    """ATR uses skipna=False to preserve NaN in warm-up rows.

    True range at row 0 includes abs(high[0] - close[-1]) which is NaN,
    so TR[0] must be NaN. ATR warm-up must be respected.
    """
    dates = pd.date_range("2025-01-01", periods=20, freq="D", tz="UTC")
    data = {
        "open": [100.0] * 20,
        "high": [101.0] * 20,
        "low": [99.0] * 20,
        "close": [100.0] * 20,
        "volume": [1000.0] * 20,
    }
    df = pd.DataFrame(data, index=dates)

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    features = compute_features(df, cfg, 365)

    # ATR-14: first valid value at index 13.
    assert features["atr"].isna().sum() == 14
    assert features["atr"].iloc[14] > 0  # Row 14 should have a value


def test_features_lookahead_guard(simple_bars):
    """Mutating a future bar does not affect features at earlier times."""
    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7,), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    features_before = compute_features(simple_bars.copy(), cfg, 365)

    # Mutate a future bar (e.g., index 9).
    bars_mutated = simple_bars.copy()
    bars_mutated.loc[bars_mutated.index[9], "close"] = 200.0  # drastic change

    features_after = compute_features(bars_mutated, cfg, 365)

    # Features at indices 0-8 must be unchanged.
    pd.testing.assert_series_equal(features_before.iloc[:-1]["close"],
                                   features_after.iloc[:-1]["close"])


def test_min_history_days(config):
    """min_history_days returns cfg.min_history_days."""
    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    result = min_history_days(cfg)
    expected = cfg.min_history_days

    assert result == expected


def test_conviction_from_row_below_sma():
    """Conviction is 0 if close < SMA (trend gate)."""
    row = {
        "close": 100.0,
        "sma_50": 105.0,
        "ret_7": 0.05,
        "ret_14": 0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    conviction = conviction_from_row(row, cfg)
    assert conviction == 0.0


def test_conviction_from_row_above_sma_all_positive():
    """Conviction is 1.0 if close >= SMA and all lookbacks positive."""
    row = {
        "close": 110.0,
        "sma_50": 105.0,
        "ret_7": 0.05,
        "ret_14": 0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    conviction = conviction_from_row(row, cfg)
    assert conviction == 1.0


def test_conviction_from_row_mixed_returns():
    """Conviction is the mean of the votes."""
    row = {
        "close": 110.0,
        "sma_50": 105.0,
        "ret_7": 0.05,      # positive: vote 1.0
        "ret_14": -0.02,    # negative: vote 0.0
        "ret_21": 0.02,     # positive: vote 1.0
        "ret_28": 0.0,      # zero: vote 0.0
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    conviction = conviction_from_row(row, cfg)
    expected = (1.0 + 0.0 + 1.0 + 0.0) / 4
    np.testing.assert_almost_equal(conviction, expected)


def test_conviction_from_row_nan_returns():
    """Conviction is 0.0 if any return is NaN."""
    row = {
        "close": 110.0,
        "sma_50": 105.0,
        "ret_7": float("nan"),
        "ret_14": 0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    conviction = conviction_from_row(row, cfg)
    assert conviction == 0.0


def test_conviction_from_row_nan_close():
    """Conviction is 0.0 if close is NaN."""
    row = {
        "close": float("nan"),
        "sma_50": 105.0,
        "ret_7": 0.05,
        "ret_14": 0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    conviction = conviction_from_row(row, cfg)
    assert conviction == 0.0


def test_conviction_from_row_sma_equality():
    """Conviction is non-zero if close == SMA (boundary test)."""
    row = {
        "close": 105.0,
        "sma_50": 105.0,  # equal to close
        "ret_7": 0.05,
        "ret_14": 0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    conviction = conviction_from_row(row, cfg)
    assert conviction == 1.0  # All positive


def test_conviction_series():
    """Conviction series applies the row function to each row."""
    dates = pd.date_range("2025-01-01", periods=3, freq="D", tz="UTC")
    data = {
        "close": [105.0, 110.0, 100.0],
        "sma_50": [105.0, 105.0, 105.0],
        "ret_7": [0.05, 0.05, 0.05],
        "ret_14": [0.03, 0.03, 0.03],
        "ret_21": [0.02, 0.02, 0.02],
        "ret_28": [0.01, 0.01, 0.01],
    }
    features = pd.DataFrame(data, index=dates)

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    convictions = conviction_series(features, cfg)

    # Row 0: close == sma, all positive -> 1.0
    assert convictions.iloc[0] == 1.0
    # Row 1: close > sma, all positive -> 1.0
    assert convictions.iloc[1] == 1.0
    # Row 2: close < sma -> 0.0
    assert convictions.iloc[2] == 0.0


def test_explain_below_sma():
    """Explain text reflects below-SMA state."""
    row = {
        "close": 100.0,
        "sma_50": 105.0,
        "ret_7": 0.05,
        "ret_14": 0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    text = explain(row, cfg)
    assert "below" in text


def test_explain_above_sma():
    """Explain text reflects above-SMA state."""
    row = {
        "close": 110.0,
        "sma_50": 105.0,
        "ret_7": 0.05,
        "ret_14": -0.03,
        "ret_21": 0.02,
        "ret_28": 0.01,
    }

    from fulcrum.config import SignalConfig
    cfg = SignalConfig(lookbacks_days=(7, 14, 21, 28), trend_filter_ma_days=50,
                       vol_window_days=30, atr_window_days=14)

    text = explain(row, cfg)
    assert "above" in text
    assert "50d SMA" in text
    assert "of 4 lookbacks" in text  # 3 positive out of 4
