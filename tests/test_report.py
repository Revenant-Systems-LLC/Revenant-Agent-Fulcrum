"""Tests for fulcrum/report.py.

Every number is hand computed in the test with the arithmetic written out in plain
Python, then compared to the module's answer to 1e-9. Nothing here calls the module to
produce its own expected value, so a wrong formula in report.py cannot agree with a
wrong formula in the test.

Offline: a real Ledger on tmp_path, no network, no broker, no credential.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from fulcrum import report as rp
from fulcrum.config import Config
from fulcrum.ledger import Ledger
from fulcrum.models import Fill, FilterResult, Position, Verdict
from fulcrum.notify import check_text

TOL = 1e-9

# Written as code points on purpose: the banned characters must not appear literally in
# any file in this repo, including the test that bans them.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# The one equity path every metric test shares. Twelve daily points, 1 to 12 January
# 2025 at midnight UTC, so there are eleven daily returns and eleven calendar days.
EQUITY = [10000.0, 10100.0, 10000.0, 10200.0, 10100.0, 10300.0,
          10200.0, 10400.0, 10300.0, 10500.0, 10400.0, 10600.0]
# Gross exposure per row. Chosen to sum to 3.0 so the mean is exactly 0.25.
GROSS = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.50, 0.40, 0.30, 0.20, 0.10, 0.0]


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    led = Ledger(str(tmp_path / "report.sqlite"))
    yield led
    led.close()


def seed_equity(led: Ledger) -> None:
    hwm = 0.0
    for i, eq in enumerate(EQUITY):
        hwm = max(hwm, eq)
        led.record_equity(
            ts=utc(2025, 1, 1) + timedelta(days=i),
            equity=eq,
            cash=eq * (1.0 - GROSS[i]),
            gross_exposure=GROSS[i],
            hwm=hwm,
            drawdown=1.0 - eq / hwm,
        )


def seed_fills(led: Ledger) -> None:
    """Five fills. Two FIFO round trips: one winner on BTC, one loser on ETH."""
    rows = [
        ("o1", "BTC/USD", "buy", 0.1, 40000.0, 10.0, utc(2025, 1, 2, 12)),
        ("o2", "BTC/USD", "buy", 0.1, 50000.0, 12.5, utc(2025, 1, 5, 12)),
        ("o3", "BTC/USD", "sell", 0.15, 60000.0, 22.5, utc(2025, 1, 8, 12)),
        ("o4", "ETH/USD", "buy", 1.0, 3000.0, 7.5, utc(2025, 1, 9, 12)),
        ("o5", "ETH/USD", "sell", 1.0, 2800.0, 7.0, utc(2025, 1, 10, 12)),
    ]
    for cid, sym, side, qty, price, fee, ts in rows:
        led.record_fill(Fill(cid, sym, side, qty, price, fee, ts))


def seed_alerts_and_kill(led: Ledger) -> None:
    led.record_alert(utc(2025, 1, 3), "stale_data", {"detail": "bars too old"})
    led.record_alert(utc(2025, 1, 4), "stale_data", {"detail": "bars too old again"})
    led.record_alert(utc(2025, 1, 7), "fee_budget", {"detail": "fees above budget"})
    led.record_kill_event(utc(2025, 1, 6), "trip", 10300.0, 0.16, "drawdown")
    led.record_kill_event(utc(2025, 1, 7), "reset", 10300.0, None, "reset file consumed")


# ---------------------------------------------------------------------------
# equity_series and daily_returns
# ---------------------------------------------------------------------------


def test_equity_series_is_one_point_per_row_indexed_by_aware_utc(ledger):
    seed_equity(ledger)
    s = rp.equity_series(ledger)

    assert len(s) == 12
    assert list(s.to_numpy()) == EQUITY
    assert s.index.tz is not None
    assert str(s.index.tz) in ("UTC", "utc")
    assert s.index[0] == pd.Timestamp("2025-01-01T00:00:00Z")
    assert s.index[-1] == pd.Timestamp("2025-01-12T00:00:00Z")


def test_equity_series_empty_ledger(ledger):
    s = rp.equity_series(ledger)
    assert len(s) == 0
    assert s.index.tz is not None


def test_daily_returns_take_the_last_value_of_each_utc_day(ledger):
    # Two rows inside 1 January: the 23:00 one is the day's last value. Then one row on
    # 2 January. Expected: exactly one return, 10500 / 10200 - 1.
    ledger.record_equity(utc(2025, 1, 1, 9), 10000.0, 0.0, 0.0, 10000.0, 0.0)
    ledger.record_equity(utc(2025, 1, 1, 23), 10200.0, 0.0, 0.0, 10200.0, 0.0)
    ledger.record_equity(utc(2025, 1, 2, 5), 10500.0, 0.0, 0.0, 10500.0, 0.0)

    r = rp.daily_returns(rp.equity_series(ledger))

    assert len(r) == 1
    assert r.index[0] == pd.Timestamp("2025-01-02T00:00:00Z")
    assert abs(float(r.iloc[0]) - (10500.0 / 10200.0 - 1.0)) < TOL


def test_daily_returns_on_the_shared_path(ledger):
    seed_equity(ledger)
    r = rp.daily_returns(rp.equity_series(ledger))

    expected = [
        10100 / 10000 - 1, 10000 / 10100 - 1, 10200 / 10000 - 1, 10100 / 10200 - 1,
        10300 / 10100 - 1, 10200 / 10300 - 1, 10400 / 10200 - 1, 10300 / 10400 - 1,
        10500 / 10300 - 1, 10400 / 10500 - 1, 10600 / 10400 - 1,
    ]
    assert len(r) == len(expected) == 11
    for got, want in zip(r.to_numpy(), expected):
        assert abs(float(got) - want) < TOL


def test_daily_returns_of_one_day_is_empty(ledger):
    ledger.record_equity(utc(2025, 1, 1), 10000.0, 0.0, 0.0, 10000.0, 0.0)
    assert len(rp.daily_returns(rp.equity_series(ledger))) == 0


# ---------------------------------------------------------------------------
# FIFO round trips
# ---------------------------------------------------------------------------


def fill_rows(led: Ledger, rows: list[tuple]) -> list:
    for cid, sym, side, qty, price, fee, ts in rows:
        led.record_fill(Fill(cid, sym, side, qty, price, fee, ts))
    return led.fills()


def test_fifo_single_lot(ledger):
    rows = fill_rows(ledger, [
        ("a", "BTC/USD", "buy", 2.0, 100.0, 0.0, utc(2025, 1, 1)),
        ("b", "BTC/USD", "sell", 2.0, 110.0, 0.0, utc(2025, 1, 2)),
    ])
    res = rp.fifo_round_trips(rows)

    # 2 * (110 - 100) = 20 profit on a cost basis of 2 * 100 = 200, so 10 percent.
    assert len(res.round_trips) == 1
    t = res.round_trips[0]
    assert t.symbol == "BTC/USD"
    assert abs(t.qty_matched - 2.0) < TOL
    assert abs(t.realized_pl_usd - 20.0) < TOL
    assert abs(t.cost_basis_usd - 200.0) < TOL
    assert abs(t.realized_pl_pct - (20.0 / 200.0)) < TOL
    assert t.won is True
    assert t.closed_at == utc(2025, 1, 2)
    assert res.unmatched_sell_qty == {}


def test_fifo_partial_close_across_two_lots(ledger):
    rows = fill_rows(ledger, [
        ("a", "BTC/USD", "buy", 1.0, 100.0, 0.0, utc(2025, 1, 1)),
        ("b", "BTC/USD", "buy", 1.0, 200.0, 0.0, utc(2025, 1, 2)),
        ("c", "BTC/USD", "sell", 1.5, 150.0, 0.0, utc(2025, 1, 3)),
    ])
    res = rp.fifo_round_trips(rows)

    # Oldest lot first: 1.0 at 100 gives 1.0 * (150 - 100) = 50.
    # Then 0.5 of the 200 lot gives 0.5 * (150 - 200) = -25.
    # Net 25 on a cost basis of 1.0 * 100 + 0.5 * 200 = 200, so 12.5 percent.
    # One sell fill is one round trip even when it spans two lots; the two legs net.
    assert len(res.round_trips) == 1
    t = res.round_trips[0]
    assert abs(t.realized_pl_usd - 25.0) < TOL
    assert abs(t.cost_basis_usd - 200.0) < TOL
    assert abs(t.realized_pl_pct - 0.125) < TOL
    assert abs(t.qty_matched - 1.5) < TOL
    assert res.unmatched_sell_qty == {}


def test_fifo_two_symbols_interleaved(ledger):
    rows = fill_rows(ledger, [
        ("a", "BTC/USD", "buy", 1.0, 100.0, 0.0, utc(2025, 1, 1)),
        ("b", "ETH/USD", "buy", 4.0, 10.0, 0.0, utc(2025, 1, 2)),
        ("c", "BTC/USD", "buy", 1.0, 300.0, 0.0, utc(2025, 1, 3)),
        ("d", "ETH/USD", "sell", 4.0, 8.0, 0.0, utc(2025, 1, 4)),
        ("e", "BTC/USD", "sell", 1.0, 120.0, 0.0, utc(2025, 1, 5)),
    ])
    res = rp.fifo_round_trips(rows)

    # The ETH sell must not touch the BTC lots and the BTC sell must take the 100 lot,
    # not the 300 lot, even though the 300 lot was bought later than the ETH buy.
    assert len(res.round_trips) == 2
    eth, btc = res.round_trips[0], res.round_trips[1]
    assert eth.symbol == "ETH/USD"
    assert abs(eth.realized_pl_usd - (4.0 * (8.0 - 10.0))) < TOL   # -8.0
    assert abs(eth.cost_basis_usd - 40.0) < TOL
    assert eth.won is False
    assert btc.symbol == "BTC/USD"
    assert abs(btc.realized_pl_usd - (1.0 * (120.0 - 100.0))) < TOL  # 20.0
    assert abs(btc.cost_basis_usd - 100.0) < TOL
    assert btc.won is True
    assert res.unmatched_sell_qty == {}


def test_fifo_sell_with_no_lot(ledger):
    rows = fill_rows(ledger, [
        ("a", "ETH/USD", "sell", 3.0, 50.0, 0.0, utc(2025, 1, 1)),
    ])
    res = rp.fifo_round_trips(rows)

    # No open lot means nothing was closed, so there is no round trip to report. The
    # quantity is surfaced instead of being dropped in silence, which is the one
    # behaviour changed from the old trade journal.
    assert res.round_trips == ()
    assert set(res.unmatched_sell_qty) == {"ETH/USD"}
    assert abs(res.unmatched_sell_qty["ETH/USD"] - 3.0) < TOL


def test_fifo_sell_larger_than_open_lots_reports_the_remainder(ledger):
    rows = fill_rows(ledger, [
        ("a", "SOL/USD", "buy", 1.0, 20.0, 0.0, utc(2025, 1, 1)),
        ("b", "SOL/USD", "sell", 3.0, 25.0, 0.0, utc(2025, 1, 2)),
    ])
    res = rp.fifo_round_trips(rows)

    # 1.0 matched at 20, so 1.0 * (25 - 20) = 5 on a basis of 20. The other 2.0 had no
    # lot and is reported as unmatched.
    assert len(res.round_trips) == 1
    assert abs(res.round_trips[0].realized_pl_usd - 5.0) < TOL
    assert abs(res.round_trips[0].cost_basis_usd - 20.0) < TOL
    assert abs(res.unmatched_sell_qty["SOL/USD"] - 2.0) < TOL


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def hand_returns() -> list[float]:
    return [
        10100 / 10000 - 1, 10000 / 10100 - 1, 10200 / 10000 - 1, 10100 / 10200 - 1,
        10300 / 10100 - 1, 10200 / 10300 - 1, 10400 / 10200 - 1, 10300 / 10400 - 1,
        10500 / 10300 - 1, 10400 / 10500 - 1, 10600 / 10400 - 1,
    ]


def hand_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def hand_sd(xs: list[float]) -> float:
    m = hand_mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def test_metrics_on_the_shared_path(ledger):
    seed_equity(ledger)
    seed_fills(ledger)
    seed_alerts_and_kill(ledger)

    m = rp.metrics(ledger, trading_days_per_year=365, cash_rate_annual=0.0)

    # ---- window
    assert m.start == utc(2025, 1, 1)
    assert m.end == utc(2025, 1, 12)
    assert abs(m.days - 11.0) < TOL          # 1 Jan to 12 Jan is 11 calendar days
    assert m.daily_return_count == 11

    # ---- return
    assert abs(m.total_return - (10600.0 / 10000.0 - 1.0)) < TOL          # 0.06
    assert abs(m.cagr - ((10600.0 / 10000.0) ** (365.0 / 11.0) - 1.0)) < TOL

    # ---- risk
    rets = hand_returns()
    sd = hand_sd(rets)
    mean = hand_mean(rets)
    assert abs(m.annualized_vol - sd * math.sqrt(365.0)) < TOL
    assert abs(m.sharpe - (mean / sd) * math.sqrt(365.0)) < TOL

    # Biggest fall: the peak 10100 on 2 January down to 10000 on 3 January.
    # 1 - 10000 / 10100 = 0.009900990099..., larger than any later dip.
    assert abs(m.max_drawdown - (1.0 - 10000.0 / 10100.0)) < TOL
    assert m.max_drawdown_start == utc(2025, 1, 2)
    assert m.max_drawdown_end == utc(2025, 1, 3)

    # Mean gross exposure: 3.0 spread over 12 rows.
    assert abs(m.average_exposure - (3.0 / 12.0)) < TOL

    # ---- weeks: value now over the value seven daily points back
    weekly = [
        10400 / 10000 - 1, 10300 / 10100 - 1, 10500 / 10000 - 1,
        10400 / 10200 - 1, 10600 / 10100 - 1,
    ]
    assert abs(m.worst_week - min(weekly)) < TOL     # 10400 / 10200 - 1
    assert abs(m.best_week - max(weekly)) < TOL      # 10500 / 10000 - 1 = 0.05

    # ---- costs
    avg_equity = sum(EQUITY) / len(EQUITY)           # 123100 / 12
    assert abs(avg_equity - 123100.0 / 12.0) < TOL
    gross_notional = (0.1 * 40000.0 + 0.1 * 50000.0 + 0.15 * 60000.0
                      + 1.0 * 3000.0 + 1.0 * 2800.0)        # 23800
    fees = 10.0 + 12.5 + 22.5 + 7.5 + 7.0                   # 59.5
    annualizer = 365.0 / 11.0
    assert abs(m.turnover - gross_notional / avg_equity * annualizer) < TOL
    assert abs(m.fee_drag_pct_equity - fees / avg_equity * annualizer) < TOL

    # ---- trades
    assert m.fills == 5
    assert m.round_trips == 2
    # BTC: 0.1 * (60000 - 40000) + 0.05 * (60000 - 50000) = 2000 + 500 = 2500
    # ETH: 1.0 * (2800 - 3000) = -200
    assert abs(m.win_rate - 0.5) < TOL
    assert abs(m.average_win - 2500.0) < TOL
    assert abs(m.average_loss - (-200.0)) < TOL
    assert abs(m.win_loss_magnitude_ratio - (2500.0 / 200.0)) < TOL     # 12.5
    assert m.unmatched_sell_qty == {}

    # ---- ledger counters
    assert m.alert_counts == {"fee_budget": 1, "stale_data": 2}
    assert m.kill_switch_trips == 1


def test_metrics_cash_rate_shifts_sharpe_only_by_the_excess(ledger):
    seed_equity(ledger)
    m = rp.metrics(ledger, trading_days_per_year=365, cash_rate_annual=0.05)

    rets = hand_returns()
    excess = [r - 0.05 / 365.0 for r in rets]
    assert abs(m.sharpe - (hand_mean(excess) / hand_sd(excess)) * math.sqrt(365.0)) < TOL
    # Subtracting a constant cannot change the spread, so volatility is untouched.
    assert abs(m.annualized_vol - hand_sd(rets) * math.sqrt(365.0)) < TOL


def test_metrics_uses_the_configured_annualization_factor(ledger):
    seed_equity(ledger)
    m = rp.metrics(ledger, trading_days_per_year=252, cash_rate_annual=0.0)

    rets = hand_returns()
    assert abs(m.annualized_vol - hand_sd(rets) * math.sqrt(252.0)) < TOL
    assert abs(m.sharpe - (hand_mean(rets) / hand_sd(rets)) * math.sqrt(252.0)) < TOL
    # CAGR annualizes by elapsed calendar days, so it does not move with this setting.
    assert abs(m.cagr - ((10600.0 / 10000.0) ** (365.0 / 11.0) - 1.0)) < TOL


def test_sharpe_is_none_below_ten_daily_returns(ledger):
    # Ten daily rows give nine returns, one short of the threshold.
    for i in range(10):
        ledger.record_equity(utc(2025, 1, 1) + timedelta(days=i), 10000.0 + 10.0 * i,
                             0.0, 0.0, 10000.0 + 10.0 * i, 0.0)
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert m.daily_return_count == 9
    assert m.sharpe is None
    assert "at least 10 daily returns" in m.sharpe_note
    assert "only 9" in m.sharpe_note
    # Volatility needs only two returns, so it is still reported.
    assert m.annualized_vol is not None


def test_sharpe_is_reported_at_exactly_ten_daily_returns(ledger):
    # Eleven daily rows give ten returns, exactly the threshold. The returns must vary
    # or the standard deviation is zero and Sharpe is undefined for that reason instead.
    values = [10000.0, 10100.0, 10000.0, 10200.0, 10100.0, 10300.0,
              10200.0, 10400.0, 10300.0, 10500.0, 10400.0]
    for i, v in enumerate(values):
        ledger.record_equity(utc(2025, 1, 1) + timedelta(days=i), v, 0.0, 0.0, max(values[:i + 1]), 0.0)
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert m.daily_return_count == 10
    assert m.sharpe is not None
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    assert abs(m.sharpe - (hand_mean(rets) / hand_sd(rets)) * math.sqrt(365.0)) < TOL


def test_sharpe_is_none_when_the_account_never_moves(ledger):
    for i in range(12):
        ledger.record_equity(utc(2025, 1, 1) + timedelta(days=i), 10000.0, 0.0, 0.0, 10000.0, 0.0)
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert m.daily_return_count == 11
    assert m.sharpe is None
    assert "never varied" in m.sharpe_note


def test_sharpe_is_none_when_returns_are_equal_to_floating_point_precision(ledger):
    # Test corrected while writing it: 10000 * 1.01 ** i does NOT give bit identical
    # daily returns, so the standard deviation lands near 1e-17 rather than at 0 and an
    # exact "sd == 0" check let a Sharpe of about 1.2e15 through. The code now treats a
    # spread below float precision as no spread, and this test pins that down.
    for i in range(12):
        v = 10000.0 * (1.01 ** i)
        ledger.record_equity(utc(2025, 1, 1) + timedelta(days=i), v, 0.0, 0.0, v, 0.0)
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert m.daily_return_count == 11
    assert m.sharpe is None
    assert "never varied" in m.sharpe_note


def test_max_drawdown_start_is_the_peak_the_fall_began_from(ledger):
    # 120 is reached twice. The 0.5 drawdown to 60 began from the SECOND 120, not the
    # first. A naive "first index of the running maximum" would answer 2 January.
    values = [100.0, 120.0, 90.0, 120.0, 60.0, 130.0]
    hwm = 0.0
    for i, v in enumerate(values):
        hwm = max(hwm, v)
        ledger.record_equity(utc(2025, 1, 1) + timedelta(days=i), v, 0.0, 0.0, hwm, 1.0 - v / hwm)
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert abs(m.max_drawdown - (1.0 - 60.0 / 120.0)) < TOL      # 0.5
    assert m.max_drawdown_start == utc(2025, 1, 4)
    assert m.max_drawdown_end == utc(2025, 1, 5)


def test_metrics_on_an_empty_ledger(ledger):
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert m.start is None and m.end is None
    assert m.days == 0.0
    assert m.total_return is None and m.cagr is None
    assert m.annualized_vol is None and m.sharpe is None
    assert m.max_drawdown == 0.0
    assert m.average_exposure is None
    assert m.turnover is None and m.fee_drag_pct_equity is None
    assert m.fills == 0 and m.round_trips == 0
    assert m.win_rate is None and m.win_loss_magnitude_ratio is None
    assert m.worst_week is None and m.best_week is None
    assert m.alert_counts == {} and m.kill_switch_trips == 0


def test_average_loss_is_negative_and_ratio_uses_its_magnitude(ledger):
    fill_rows(ledger, [
        ("a", "BTC/USD", "buy", 1.0, 100.0, 0.0, utc(2025, 1, 1)),
        ("b", "BTC/USD", "sell", 1.0, 130.0, 0.0, utc(2025, 1, 2)),    # +30
        ("c", "BTC/USD", "buy", 1.0, 100.0, 0.0, utc(2025, 1, 3)),
        ("d", "BTC/USD", "sell", 1.0, 90.0, 0.0, utc(2025, 1, 4)),     # -10
        ("e", "BTC/USD", "buy", 1.0, 100.0, 0.0, utc(2025, 1, 5)),
        ("f", "BTC/USD", "sell", 1.0, 80.0, 0.0, utc(2025, 1, 6)),     # -20
    ])
    ledger.record_equity(utc(2025, 1, 1), 1000.0, 0.0, 0.0, 1000.0, 0.0)
    ledger.record_equity(utc(2025, 1, 6), 1000.0, 0.0, 0.0, 1000.0, 0.0)
    m = rp.metrics(ledger, trading_days_per_year=365)

    assert m.round_trips == 3
    assert abs(m.win_rate - 1.0 / 3.0) < TOL
    assert abs(m.average_win - 30.0) < TOL
    assert abs(m.average_loss - ((-10.0 + -20.0) / 2.0)) < TOL       # -15.0
    assert abs(m.win_loss_magnitude_ratio - (30.0 / 15.0)) < TOL     # 2.0


# ---------------------------------------------------------------------------
# filter summary
# ---------------------------------------------------------------------------


def seed_filter(led: Ledger) -> None:
    """Three calls: one good with a veto, one good all allow, one failure."""
    led.record_filter_result(
        utc(2025, 1, 6, 1), "shadow",
        FilterResult(
            verdicts=(
                Verdict("BTC/USD", "veto", "the chain halted twice this week", 0.8, "chain resumes"),
                Verdict("ETH/USD", "allow", "nothing concrete against it", 0.6, ""),
            ),
            error=None, latency_ms=100, raw=None),
        applied=False, candidates=("BTC/USD", "ETH/USD"))
    led.record_filter_result(
        utc(2025, 1, 13, 1), "active",
        FilterResult(
            verdicts=(Verdict("SOL/USD", "allow", "no concrete reason to stop", 0.5, ""),),
            error=None, latency_ms=200, raw=None),
        applied=True, candidates=("SOL/USD",))
    led.record_filter_result(
        utc(2025, 1, 20, 1), "shadow",
        FilterResult(verdicts=(), error="timeout: no answer in 30 seconds", latency_ms=30000, raw=None),
        applied=False, candidates=("BTC/USD",))
    led.record_shadow_daily(utc(2025, 1, 7, 2), "BTC/USD", 0.75, "allow", "nothing concrete", None)
    led.record_shadow_daily(utc(2025, 1, 8, 2), "BTC/USD", 0.75, "veto", "exchange outage", None)
    led.record_shadow_daily(utc(2025, 1, 9, 2), "ETH/USD", 0.50, "allow", "fail open", "timeout: slow")


def test_filter_summary(ledger):
    seed_filter(ledger)
    fs = rp.filter_summary(ledger)

    assert fs.calls == 3
    assert fs.failures_by_error_class == {"timeout": 1}
    assert fs.failures == 1

    # One latency per call: 100, 200, 30000. Median of three sorted values is the
    # middle one. The 95th percentile with linear interpolation sits at position
    # 0.95 * (3 - 1) = 1.9, that is 200 + 0.9 * (30000 - 200).
    assert abs(fs.median_latency_ms - 200.0) < TOL
    assert abs(fs.p95_latency_ms - (200.0 + 0.9 * (30000.0 - 200.0))) < TOL

    # The failed call wrote a fail open "allow" row; it counts as a failure, not a
    # verdict, so only the two real allows and the one veto are counted here.
    assert fs.verdicts_by_kind == {"allow": 2, "veto": 1}
    assert fs.applied_rows == 1          # the active call, one symbol
    assert fs.shadow_rows == 3           # two from the first call, one from the failure

    assert len(fs.vetoes) == 1
    assert fs.vetoes[0].symbol == "BTC/USD"
    assert fs.vetoes[0].cycle_ts == utc(2025, 1, 6, 1)
    assert fs.vetoes[0].reason == "the chain halted twice this week"

    assert fs.shadow_daily_counts == {"allow": 2, "veto": 1}
    assert fs.shadow_daily_errors == 1


def test_filter_summary_on_an_empty_ledger(ledger):
    fs = rp.filter_summary(ledger)
    assert fs.calls == 0
    assert fs.failures == 0
    assert fs.median_latency_ms is None and fs.p95_latency_ms is None
    assert fs.verdicts_by_kind == {}
    assert fs.vetoes == ()
    assert fs.shadow_daily_counts == {}


# ---------------------------------------------------------------------------
# ghost book
# ---------------------------------------------------------------------------


def test_ghost_summary_counterfactual_ahead(ledger):
    ledger.record_equity(utc(2025, 1, 12), 10000.00, 0.0, 0.0, 10000.0, 0.0)
    ledger.record_ghost(utc(2025, 1, 12), "counterfactual", 10012.40, 0.0, {})
    gs = rp.ghost_summary(ledger)

    assert abs(gs.real_equity - 10000.00) < TOL
    assert abs(gs.counterfactual_equity - 10012.40) < TOL
    assert abs(gs.difference_usd - 12.40) < TOL
    assert abs(gs.difference_pct - (12.40 / 10000.00)) < TOL
    assert gs.reading == (
        "If the filter's vetoes had been applied, the account would be $12.40 (0.1%) higher.")


def test_ghost_summary_counterfactual_behind(ledger):
    ledger.record_equity(utc(2025, 1, 12), 10000.00, 0.0, 0.0, 10000.0, 0.0)
    ledger.record_ghost(utc(2025, 1, 12), "counterfactual", 9950.00, 0.0, {})
    gs = rp.ghost_summary(ledger)

    assert abs(gs.difference_usd - (-50.0)) < TOL
    assert gs.reading == (
        "If the filter's vetoes had been applied, the account would be $50.00 (0.5%) lower.")


def test_ghost_summary_prefers_a_recorded_real_book(ledger):
    ledger.record_equity(utc(2025, 1, 12), 1.0, 0.0, 0.0, 1.0, 0.0)
    ledger.record_ghost(utc(2025, 1, 12), "real", 10000.0, 0.0, {})
    ledger.record_ghost(utc(2025, 1, 12), "counterfactual", 10100.0, 0.0, {})
    gs = rp.ghost_summary(ledger)

    assert abs(gs.real_equity - 10000.0) < TOL
    assert abs(gs.difference_usd - 100.0) < TOL


def test_ghost_summary_with_no_counterfactual(ledger):
    ledger.record_equity(utc(2025, 1, 12), 10000.0, 0.0, 0.0, 10000.0, 0.0)
    gs = rp.ghost_summary(ledger)

    assert gs.counterfactual_equity is None
    assert gs.difference_usd is None
    assert gs.reading == "Not enough history yet to say."


def test_ghost_summary_on_an_empty_ledger(ledger):
    gs = rp.ghost_summary(ledger)
    assert gs.real_equity is None and gs.counterfactual_equity is None
    assert gs.reading == "Not enough history yet to say."


# ---------------------------------------------------------------------------
# plain English
# ---------------------------------------------------------------------------


def assert_plain_english(text: str) -> None:
    """The house rules for every user facing string: no em or en dash, nothing above
    the basic plane (which is where emoji live), and no table."""
    assert EM_DASH not in text, "em dash found"
    assert EN_DASH not in text, "en dash found"
    assert all(ord(c) <= 0xFFFF for c in text), "character above U+FFFF found"
    check_text(text)


def test_render_plain_reads_as_sentences(ledger):
    seed_equity(ledger)
    seed_fills(ledger)
    seed_alerts_and_kill(ledger)
    seed_filter(ledger)
    ledger.record_ghost(utc(2025, 1, 12), "counterfactual", 10612.40, 0.0, {})

    m = rp.metrics(ledger, trading_days_per_year=365)
    fs = rp.filter_summary(ledger)
    gs = rp.ghost_summary(ledger)
    text = rp.render_plain(m, fs, gs, mode="shadow")

    assert_plain_english(text)

    for heading in ("Account", "Risk", "Costs", "Trades", "AI filter", "Ghost book",
                    "What this means"):
        assert f"\n{heading}\n" in "\n" + text, f"missing section heading {heading}"

    assert "History runs from 1 January 2025 to 12 January 2025, which is 11.0 days." in text
    assert "Change in account value over that time: 6.00%" in text
    assert "Worst fall from a peak: 0.99%" in text
    assert "That fall began on 2 January 2025 and reached its low on 3 January 2025." in text
    assert "Average share of the account held in coins: 25.00%" in text
    assert "Times the safety brake tripped: 1." in text
    assert "Warnings recorded: fee_budget 1, stale_data 2." in text
    assert "Orders filled: 5. Completed round trips: 2" in text
    assert "Share of round trips that made money: 50.0%." in text
    assert "Winners are 12.50 times the size of losers" in text
    assert "the AI filter is in shadow mode (recording only, it blocks nothing)" in text
    assert "The filter was asked 3 times and failed to answer 1 time (" in text
    assert "1 of those answers was allowed to block a real buy; 3 were recorded only." in text
    assert "Daily practice runs (logged, never traded): allow 2, veto 1, with 1 failure." in text
    assert "Answers given: 2 allow and 1 veto." in text
    assert "BTC/USD on 2025-01-06 01:00 UTC: the chain halted twice this week" in text
    assert "If the filter's vetoes had been applied, the account would be $12.40 (0.1%) higher." in text
    assert "Nothing here involved real money." in text


def test_render_plain_says_when_there_is_too_little_history(ledger):
    m = rp.metrics(ledger, trading_days_per_year=365)
    fs = rp.filter_summary(ledger)
    gs = rp.ghost_summary(ledger)
    text = rp.render_plain(m, fs, gs, mode="off")

    assert_plain_english(text)
    assert "Nothing has happened yet." in text
    assert "Not enough history yet to say." in text
    assert "Sharpe not reported" in text
    assert "there is nothing to conclude" in text


def test_render_plain_active_mode_explains_the_ghost_book_the_other_way_round(ledger):
    seed_equity(ledger)
    ledger.record_ghost(utc(2025, 1, 12), "counterfactual", 10700.0, 0.0, {})
    text = rp.render_plain(rp.metrics(ledger, 365), rp.filter_summary(ledger),
                           rp.ghost_summary(ledger), mode="active")

    assert_plain_english(text)
    assert "in active mode the vetoes are already being applied" in text


def test_render_plain_warns_when_fees_are_above_the_alarm_level(ledger):
    seed_equity(ledger)
    seed_fills(ledger)
    m = rp.metrics(ledger, trading_days_per_year=365)
    text = rp.render_plain(m, rp.filter_summary(ledger), rp.ghost_summary(ledger), mode="shadow")

    # 59.5 of fees on an average account of 123100 / 12 over 11 days annualizes to far
    # more than the 3 percent alarm level, so the closing paragraph must say so.
    assert m.fee_drag_pct_equity > 0.03
    assert "above the 3% alarm level" in text


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.fixture
def shadow_config(raw_config, tmp_path) -> Config:
    raw_config["llm_filter"]["mode"] = "shadow"
    raw_config["run"]["ledger_path"] = str(tmp_path / "ledger.sqlite")
    raw_config["run"]["creds_dir"] = str(tmp_path / "creds")
    raw_config["run"]["kill_reset_file"] = str(tmp_path / "creds" / "agent_kill_reset")
    raw_config["data"]["cache_dir"] = str(tmp_path / "bars")
    return Config.from_dict(raw_config, source_path="<test>")


def test_render_status_on_an_empty_ledger(ledger, config):
    assert rp.render_status(ledger, config) == "Nothing has happened yet.\n"


def test_render_status_reads_as_sentences(ledger, shadow_config):
    # 612.40 of coins plus 9387.60 of cash is 10000.00 of equity, against a best value
    # of 10060.00. 1 - 10000 / 10060 = 0.005964..., which prints as 0.6%.
    ledger.snapshot_positions(
        [Position("BTC/USD", 0.01, 30000.0, 300.00),
         Position("ETH/USD", 0.08, 2500.0, 200.00),
         Position("SOL/USD", 1.00, 112.00, 112.40)],
        stops={"BTC/USD": 28000.0}, entry_ids={}, as_of=utc(2025, 1, 6, 1))
    ledger.record_equity(utc(2025, 1, 6, 1), 10000.00, 9387.60, 0.06124,
                         10060.00, 1.0 - 10000.0 / 10060.0)
    seed_decisions_one_round(ledger)

    text = rp.render_status(ledger, shadow_config)
    assert_plain_english(text)

    assert "Holding 3 coins worth $612.40 and $9,387.60 in cash." in text
    assert "The coins are BTC/USD, ETH/USD and SOL/USD." in text
    assert "Best value so far $10,060.00; today 0.6% below it." in text
    assert "Safety brake is off." in text
    assert "AI filter is in shadow mode (recording only)." in text
    assert "Last decision round: Monday 01:00 UTC, bought BTC/USD and ETH/USD." in text


def seed_decisions_one_round(led: Ledger) -> None:
    """One rebalance round on Monday 6 January 2025 at 01:00 UTC."""
    from fulcrum.models import Decision
    led.start_run("r1", "dry_run", "hash", None, utc(2025, 1, 6, 1))
    cycle = utc(2025, 1, 6, 1)
    led.record_decision(Decision("d1", cycle, "BTC/USD", 1.0, 0.15, 0.0, "buy",
                                 "trend is up", 28000.0, "trend breaks"), "r1")
    led.record_decision(Decision("d2", cycle, "ETH/USD", 1.0, 0.15, 0.0, "buy",
                                 "trend is up", 2300.0, "trend breaks"), "r1")
    led.record_decision(Decision("d3", cycle, "SOL/USD", 0.0, 0.0, 0.0, "hold",
                                 "below its average", None, ""), "r1")


def test_render_status_with_no_positions_and_the_brake_on(ledger, shadow_config):
    ledger.record_equity(utc(2025, 1, 6, 1), 8500.00, 8500.00, 0.0, 10000.00, 0.15)
    ledger.set_kill_state(tripped=True, hwm=10000.0, tripped_at=utc(2025, 1, 6, 1),
                          drawdown_at_trip=0.15)
    ledger.record_alert(utc(2025, 1, 6, 1), "kill", {"detail": "drawdown"})

    text = rp.render_status(ledger, shadow_config)
    assert_plain_english(text)

    assert "Holding no coins and $8,500.00 in cash." in text
    assert "Best value so far $10,000.00; today 15.0% below it." in text
    assert "Safety brake is ON." in text
    assert "No decision round has run yet." in text
    assert "Most recent warning: kill at 2025-01-06 01:00 UTC." in text
    assert "Running in dry_run mode. No real money is involved." in text


def test_render_status_at_the_high_water_mark(ledger, config):
    ledger.record_equity(utc(2025, 1, 6, 1), 10000.00, 10000.00, 0.0, 10000.00, 0.0)
    text = rp.render_status(ledger, config)

    assert_plain_english(text)
    assert "the account is at its best value right now" in text
    assert "AI filter is switched off." in text


def test_render_status_with_one_coin_uses_the_singular(ledger, config):
    ledger.snapshot_positions([Position("BTC/USD", 0.01, 30000.0, 300.00)],
                              stops={}, entry_ids={}, as_of=utc(2025, 1, 6, 1))
    ledger.record_equity(utc(2025, 1, 6, 1), 1000.00, 700.00, 0.3, 1000.00, 0.0)
    text = rp.render_status(ledger, config)

    assert_plain_english(text)
    assert "Holding 1 coin worth $300.00 and $700.00 in cash." in text


def test_render_status_reports_a_round_with_no_trades(ledger, config):
    from fulcrum.models import Decision
    ledger.start_run("r1", "dry_run", "hash", None, utc(2025, 1, 6, 1))
    ledger.record_decision(Decision("d1", utc(2025, 1, 6, 1), "BTC/USD", 0.5, 0.1, 0.1,
                                    "hold", "inside the band", None, ""), "r1")
    ledger.record_equity(utc(2025, 1, 6, 1), 1000.00, 1000.00, 0.0, 1000.00, 0.0)
    text = rp.render_status(ledger, config)

    assert_plain_english(text)
    assert "Last decision round: Monday 01:00 UTC, no trades were needed." in text


# ---------------------------------------------------------------------------
# house style over the whole module
# ---------------------------------------------------------------------------


def test_report_source_has_no_em_dash_and_no_astral_character():
    """D-013 applies to the source too: the dashes usually slip in through comments."""
    from pathlib import Path
    src = Path(rp.__file__).read_text(encoding="utf-8")
    assert EM_DASH not in src, "em dash in fulcrum/report.py"
    assert EN_DASH not in src, "en dash in fulcrum/report.py"
    assert all(ord(c) <= 0xFFFF for c in src)
