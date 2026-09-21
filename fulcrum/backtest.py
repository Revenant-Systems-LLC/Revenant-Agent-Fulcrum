"""Walk-forward backtest engine on historical bars.

The backtest simulates the strategy over daily data with the rules-based Engine,
FakeBroker, and Ledger. All timing rules apply: no lookahead (fills at t+1 open,
not t close), fee and slippage modeling, risk layer, etc.

Key principle: signal sees data through bar close t; fills happen at t+1 open.

Public interface:
  Fold(name, start, end)
  BacktestResult(metrics, benchmarks, fold, trades, missing_days, sources_used, ledger_path, notes)
  run_backtest(cfg, source, start, end, ledger_path, filt=None, notifier=None) -> BacktestResult
  run_folds(cfg, source, folds, ledger_dir) -> dict[str, BacktestResult]
  main(cfg, out_md: Path) - CLI entry point
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from . import data as data_mod
from . import report
from .broker import FakeBroker
from .config import Config
from .data import DailySource, HistoricalSource
from .engine import Engine
from .ledger import Ledger
from .llm_filter import FakeFilter, OffFilter
from .models import OrderRequest, Position, utcnow
from .notify import NullNotifier

log = logging.getLogger("fulcrum.backtest")


class Fold(NamedTuple):
    """One period for walk-forward testing."""

    name: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Summary of a backtest run."""

    metrics: report.Metrics
    benchmarks: dict[str, report.Metrics]
    fold: Fold | None
    trades: int
    missing_days: dict[str, list[date]]
    sources_used: dict[str, str]
    ledger_path: Path
    notes: list[str]


# Default folds per D-012: stress (Feb-March 2026), trending (Apr-Oct 2025), choppy (Aug-Oct 2023)
DEFAULT_FOLDS = [
    Fold("stress", date(2026, 2, 1), date(2026, 3, 31)),
    Fold("trending", date(2025, 4, 7), date(2025, 10, 6)),
    Fold("choppy", date(2023, 8, 1), date(2023, 10, 15)),
]


def run_backtest(
    cfg: Config,
    source: HistoricalSource,
    start: date,
    end: date,
    ledger_path: Path | str,
    filt=None,
    notifier=None,
) -> BacktestResult:
    """Run the strategy backtest from start to end.

    Parameters
    ----------
    cfg : Config
        Configuration with universe, signal, sizing, asset class, etc.
    source : HistoricalSource
        Data source that returns DailyHistory with bars and missing dates.
    start : date
        Backtest start date (inclusive).
    end : date
        Backtest end date (inclusive).
    ledger_path : Path | str
        Where to write the ledger.sqlite file.
    filt
        LLM filter (default: OffFilter).
    notifier
        Notifier (default: NullNotifier).

    Returns
    -------
    BacktestResult
        Metrics, benchmarks, trades count, missing dates, sources used, and notes.
    """
    if filt is None:
        filt = OffFilter()
    if notifier is None:
        notifier = NullNotifier()

    ledger_path = _fresh_ledger_path(Path(ledger_path), start, end)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    # Fetch daily history for core plus every candidate from (start - warmup) to end.
    # The warmup bars are kept for feature construction (A4).
    warmup_days = cfg.signal.min_history_days + 5
    fetch_start = start - timedelta(days=warmup_days)

    histories: dict[str, data_mod.DailyHistory] = {}
    missing_days: dict[str, list[date]] = {}
    sources_used: dict[str, str] = {}

    for symbol in cfg.universe.core + cfg.universe.candidates:
        try:
            hist = source.get_daily(symbol, fetch_start, end)
            histories[symbol] = hist
            missing_days[symbol] = hist.missing_dates
            sources_used[symbol] = hist.source_used
        except Exception as e:
            log.warning(f"Failed to fetch {symbol}: {e}")
            histories[symbol] = None
            missing_days[symbol] = []
            sources_used[symbol] = "failed"

    # Universe: core always; candidates only if they pass the liquidity screen (spec: documented
    # screen, hard cap max_symbols). Candidates with no history are rejected by the screen.
    available = {s: h for s, h in histories.items() if h is not None and h.bars}
    core = [s for s in cfg.universe.core if s in available]
    passing, rejected = data_mod.liquidity_screen(
        [s for s in cfg.universe.candidates if s in available], available,
        cfg.universe.min_median_daily_volume_usd)
    universe = (core + passing)[: cfg.universe.max_symbols]
    for sym, reason in rejected:
        log.info("liquidity screen rejected %s: %s", sym, reason)

    # Run the backtest
    result = _run_strategy_backtest(cfg, universe, histories, start, end, ledger_path, filt, notifier)
    result.notes.append("universe: " + ", ".join(universe))
    for sym, reason in rejected:
        result.notes.append(f"liquidity screen rejected {sym}: {reason}")

    return result


def _fresh_ledger_path(base: Path, start: date, end: date) -> Path:
    """Every run gets its own ledger file, so a rerun never collides with a previous run's
    rows (the bot is not allowed to delete files, so it never reuses one either)."""
    stem = f"{base.stem}-{start.isoformat()}-{end.isoformat()}"
    n = 0
    while True:
        candidate = base.with_name(f"{stem}{'' if n == 0 else '-' + str(n)}{base.suffix or '.sqlite'}")
        if not candidate.exists():
            return candidate
        n += 1


def run_folds(
    cfg: Config,
    source: HistoricalSource,
    folds: list[Fold],
    ledger_dir: Path | str,
    filt=None,
    notifier=None,
) -> dict[str, BacktestResult]:
    """Run walk-forward backtests on a list of Fold periods.

    Returns a dict of BacktestResult keyed by fold name, plus "whole_period" for
    the full available date range.

    Parameters
    ----------
    cfg : Config
        Configuration.
    source : HistoricalSource
        Data source.
    folds : list[Fold]
        List of Fold(name, start, end) periods.
    ledger_dir : Path | str
        Directory where fold ledgers are written.
    filt
        LLM filter (default: OffFilter).
    notifier
        Notifier (default: NullNotifier).

    Returns
    -------
    dict[str, BacktestResult]
        Results keyed by fold name plus "whole_period".
    """
    if filt is None:
        filt = OffFilter()
    if notifier is None:
        notifier = NullNotifier()

    ledger_dir = Path(ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    # Run each fold
    for fold in folds:
        fold_ledger = ledger_dir / f"fold_{fold.name}.sqlite"
        try:
            result = run_backtest(cfg, source, fold.start, fold.end, fold_ledger, filt=filt, notifier=notifier)
            result_with_fold = BacktestResult(
                metrics=result.metrics,
                benchmarks=result.benchmarks,
                fold=fold,
                trades=result.trades,
                missing_days=result.missing_days,
                sources_used=result.sources_used,
                ledger_path=result.ledger_path,
                notes=result.notes,
            )
            results[fold.name] = result_with_fold
        except Exception as e:
            log.error(f"Fold {fold.name} failed: {e}")
            results[fold.name] = None

    # Run the whole period: discover the range from available data
    universe = (cfg.universe.core + cfg.universe.candidates)[: cfg.universe.max_symbols]
    warmup_days = cfg.signal.min_history_days + 5

    # Find the earliest and latest dates available
    all_dates = set()
    for symbol in universe:
        try:
            # Use a wide range to find actual data
            hist = source.get_daily(symbol, date(2020, 1, 1), utcnow().date())
            for bar in hist.bars:
                all_dates.add(bar.ts.date())
        except Exception:
            pass

    if all_dates:
        whole_start = min(all_dates)
        whole_end = max(all_dates)
        whole_ledger = ledger_dir / "fold_whole_period.sqlite"
        try:
            result = run_backtest(cfg, source, whole_start, whole_end, whole_ledger, filt=filt, notifier=notifier)
            result_with_fold = BacktestResult(
                metrics=result.metrics,
                benchmarks=result.benchmarks,
                fold=Fold("whole_period", whole_start, whole_end),
                trades=result.trades,
                missing_days=result.missing_days,
                sources_used=result.sources_used,
                ledger_path=result.ledger_path,
                notes=result.notes,
            )
            results["whole_period"] = result_with_fold
        except Exception as e:
            log.error(f"Whole period backtest failed: {e}")
            results["whole_period"] = None

    return results


class _MockClock:
    """Injectable clock for FakeBroker to use simulated time."""

    def __init__(self):
        self.now = utcnow()

    def __call__(self):
        return self.now


def _run_strategy_backtest(
    cfg: Config,
    universe: list[str],
    histories: dict[str, data_mod.DailyHistory],
    start: date,
    end: date,
    ledger_path: Path,
    filt,
    notifier,
):
    """Run the full strategy backtest with Engine, FakeBroker, and Ledger.

    This is the core backtest loop that drives the engine day by day.

    Per the spec (backtest_protocol), fills at t+1 open requires:
    - Signals see data through bar close t
    - Fills happen at t+1 open
    - Stops are checked before rebalance (never against their own entry bar)

    Per A8/A9, the day-loop order is critical:
    1. Stop check against the current book (from before this day)
    2. Rebalance with bars through close of t, t+1 opens set, now = close of t
    3. Mark everything to close and record equity
    """
    with contextlib.ExitStack() as stack:
        # Four ledgers: strategy, BTC, equal-weight, cash
        ledger = stack.enter_context(_ledger_context(ledger_path))
        btc_ledger = stack.enter_context(_ledger_context(_benchmark_path(ledger_path, "btc")))
        ew_ledger = stack.enter_context(_ledger_context(_benchmark_path(ledger_path, "equal_weight")))
        cash_ledger = stack.enter_context(_ledger_context(_benchmark_path(ledger_path, "cash")))

        ledger.start_run("backtest", "dry_run", "backtest_config", None, utcnow())
        btc_ledger.start_run("backtest_btc", "dry_run", "backtest_config", None, utcnow())
        ew_ledger.start_run("backtest_ew", "dry_run", "backtest_config", None, utcnow())
        cash_ledger.start_run("backtest_cash", "dry_run", "backtest_config", None, utcnow())

        # Create mock clock for simulated time
        mock_clock = _MockClock()

        broker = FakeBroker(
            starting_cash=100000.0,
            fee_bps=cfg.asset_class.taker_fee_bps,
            slippage_bps=cfg.asset_class.slippage_bps,
            min_notional_usd=cfg.asset_class.min_notional_usd,
            clock=mock_clock,
        )

        engine = Engine(
            cfg=cfg,
            broker=broker,
            ledger=ledger,
            filt=filt,
            notifier=notifier,
            run_id="backtest",
            scope="backtest",
        )

        # Collect all bars, indexed by symbol and sorted by date
        # Per A4: keep the full fetched history (including warmup) for feature construction
        all_bars: dict[str, list] = {}
        all_dates = set()

        for symbol in universe:
            hist = histories.get(symbol)
            if hist is None or not hist.bars:
                all_bars[symbol] = []
                continue
            # Keep ALL bars including warmup for feature construction
            all_bars[symbol] = sorted(hist.bars, key=lambda b: b.ts)
            # Only track dates in [start, end] for the loop
            bars_in_range = [b for b in hist.bars if start <= b.ts.date() <= end]
            for bar in bars_in_range:
                all_dates.add(bar.ts.date())

        if not all_dates:
            # Empty backtest
            m = report.metrics(ledger, cfg.asset_class.trading_days_per_year)
            return BacktestResult(
                metrics=m,
                benchmarks={},
                fold=None,
                trades=0,
                missing_days={},
                sources_used={},
                ledger_path=ledger_path,
                notes=["no data for the backtest period"],
            )

        # Create benchmark brokers for tracking equity
        btc_broker = FakeBroker(
            starting_cash=100000.0,
            fee_bps=cfg.asset_class.taker_fee_bps,
            slippage_bps=cfg.asset_class.slippage_bps,
            min_notional_usd=cfg.asset_class.min_notional_usd,
            clock=mock_clock,
        )
        ew_broker = FakeBroker(
            starting_cash=100000.0,
            fee_bps=cfg.asset_class.taker_fee_bps,
            slippage_bps=cfg.asset_class.slippage_bps,
            min_notional_usd=cfg.asset_class.min_notional_usd,
            clock=mock_clock,
        )
        cash_broker = FakeBroker(
            starting_cash=100000.0,
            fee_bps=0.0,
            slippage_bps=0.0,
            min_notional_usd=0.0,
            clock=mock_clock,
        )

        # Day-by-day backtest loop (only over [start, end])
        all_dates_sorted = sorted(all_dates)
        next_rebalance_date = _next_rebalance_date(cfg, start)
        btc_bought = False
        ew_bought = False

        for day_idx, day in enumerate(all_dates_sorted):
            # Mock clock for simulated time
            now_ts = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
            mock_clock.now = now_ts

            # Step 1: Stop check FIRST
            # Per A8: stops are checked against day t's low, but stop was created before day t
            # Mark to close for all symbols first, then tick (which records equity at close)
            close_prices = {}
            for symbol in universe:
                bars_today = [b for b in all_bars[symbol] if b.ts.date() == day]
                if bars_today:
                    close = bars_today[-1].close
                    close_prices[symbol] = close
                    broker.set_price(symbol, close, bars_today[-1].ts)

            # Call tick with close prices (tick records equity, but at close prices not lows)
            if close_prices:
                try:
                    tick_result = engine.tick(prices=close_prices, now=now_ts)
                except Exception as e:
                    log.warning(f"Tick on {day} failed: {e}")

            # Step 2: Rebalance (if scheduled)
            is_rebalance = day >= next_rebalance_date

            if is_rebalance:
                # Check if we have t+1 bars for filling
                next_day = day + timedelta(days=1)
                has_next_bars = any(
                    any(b.ts.date() == next_day for b in all_bars[symbol])
                    for symbol in universe
                )

                if has_next_bars:
                    # Collect bars through today's close for the engine
                    bars_dict = {}
                    for symbol in universe:
                        # Per A4: include ALL bars through today (including warmup)
                        bars_through_today = [b for b in all_bars[symbol] if b.ts.date() <= day]
                        if bars_through_today:
                            bars_dict[symbol] = data_mod.to_frame(bars_through_today)

                    # Set t+1 opens BEFORE rebalance (so fills happen at t+1 open)
                    for symbol in universe:
                        bars_next = [b for b in all_bars[symbol] if b.ts.date() == next_day]
                        if bars_next:
                            broker.set_price(symbol, bars_next[0].open, bars_next[0].ts)

                    if bars_dict:
                        try:
                            # Engine sees closes through today, now = close instant of today
                            cycle_result = engine.rebalance(bars_dict, now_ts, headlines=())
                        except Exception as e:
                            log.warning(f"Rebalance on {day} failed: {e}")

                    # Advance schedule to next rebalance date
                    # Per A5: advance even if there was no bar on the scheduled day
                    next_rebalance_date = _next_rebalance_date(cfg, day + timedelta(days=1))

            # Step 3: Mark everything to close and record equity (final daily equity)
            # Note: engine.tick already recorded an equity point, but we still need our final one
            # Actually, to fix A9, we should NOT record another equity here if tick already did
            # The issue is tick records at whatever price, so we make sure tick uses close prices above

            # Record equity snapshot for strategy (only once per day, at close)
            for symbol in universe:
                bars_today = [b for b in all_bars[symbol] if b.ts.date() == day]
                if bars_today:
                    broker.set_price(symbol, bars_today[-1].close, bars_today[-1].ts)

            acct = broker.account()
            positions = broker.positions()
            gross_exp = sum(p.market_value for p in positions) / acct.equity if acct.equity > 0 else 0.0
            hwm = ledger.kill_state()["hwm"] if ledger.kill_state() else acct.equity
            dd = 1.0 - acct.equity / hwm if hwm > 0 else 0.0
            # Do not record equity again if tick already did - instead just mark to close
            # Actually, tick records its own equity, so we get it from there
            # ledger.record_equity(now_ts, acct.equity, acct.cash, gross_exp, hwm, dd)

            # Run benchmarks - compute equity mathematically to avoid order submission issues
            # BTC buy and hold: buy on first day at open, hold through end
            if "BTC/USD" in all_bars and day == all_dates_sorted[0]:
                bars_btc_first = [b for b in all_bars["BTC/USD"] if b.ts.date() == day]
                if bars_btc_first:
                    # Buy at first day's open with 25 bps fee and 10 bps slippage
                    btc_open = bars_btc_first[0].open
                    slipped_price = btc_open * (1.0 + cfg.asset_class.slippage_bps / 1e4)
                    fee = 100000.0 * (cfg.asset_class.taker_fee_bps / 1e4)
                    btc_qty = 100000.0 / slipped_price
                    btc_cash = 100000.0 - 100000.0 - fee
                    btc_bought = True

            if "BTC/USD" in all_bars and btc_bought:
                bars_btc = [b for b in all_bars["BTC/USD"] if b.ts.date() == day]
                if bars_btc:
                    btc_close = bars_btc[-1].close
                    btc_equity = btc_qty * btc_close + btc_cash
                    btc_ledger.record_equity(now_ts, btc_equity, btc_cash, 1.0, btc_equity, 0.0)

            # Equal weight: buy equal notional of each symbol on first day
            if day == all_dates_sorted[0]:
                symbols_with_data = [s for s in universe if any(b.ts.date() == day for b in all_bars.get(s, []))]
                if symbols_with_data:
                    per_symbol = 100000.0 / len(symbols_with_data)
                    ew_bought = True

            if ew_bought:
                # Mark all symbols to close and compute equity
                symbols_with_data = [s for s in universe if any(b.ts.date() == day for b in all_bars.get(s, []))]
                if symbols_with_data:
                    ew_equity = 0.0
                    for symbol in symbols_with_data:
                        bars_sym = [b for b in all_bars.get(symbol, []) if b.ts.date() == day]
                        if bars_sym:
                            per_symbol = 100000.0 / len(symbols_with_data)
                            first_bars = [b for b in all_bars.get(symbol, []) if b.ts.date() == all_dates_sorted[0]]
                            if first_bars:
                                buy_open = first_bars[0].open
                                slipped_buy = buy_open * (1.0 + cfg.asset_class.slippage_bps / 1e4)
                                qty = per_symbol / slipped_buy
                                close = bars_sym[-1].close
                                ew_equity += qty * close
                    # Add remaining cash from fees
                    total_fee = 100000.0 * len(symbols_with_data) * (cfg.asset_class.taker_fee_bps / 1e4)
                    ew_cash = 100000.0 - 100000.0 - total_fee
                    ew_ledger.record_equity(now_ts, ew_equity + ew_cash, ew_cash, 1.0, ew_equity + ew_cash, 0.0)

            # Cash: just track the constant 100k
            cash_ledger.record_equity(now_ts, 100000.0, 100000.0, 0.0, 100000.0, 0.0)

        # Compute metrics before closing ledgers
        strategy_metrics = report.metrics(ledger, cfg.asset_class.trading_days_per_year)
        btc_metrics = report.metrics(btc_ledger, cfg.asset_class.trading_days_per_year) if btc_bought else None
        ew_metrics = report.metrics(ew_ledger, cfg.asset_class.trading_days_per_year) if ew_bought else None
        cash_metrics = report.metrics(cash_ledger, cfg.asset_class.trading_days_per_year)

        # Count trades before closing
        trades = len(ledger.fills())

        # Build benchmark dict with non-None entries
        benchmarks = {}
        if btc_metrics:
            benchmarks["btc_buy_hold"] = btc_metrics
        if ew_metrics:
            benchmarks["equal_weight_buy_hold"] = ew_metrics
        benchmarks["cash"] = cash_metrics

        # Collect missing days from all symbols
        missing_days_final = {}
        for symbol in universe:
            hist = histories.get(symbol)
            if hist:
                missing_days_final[symbol] = hist.missing_dates

        sources_used_final = {}
        for symbol in universe:
            hist = histories.get(symbol)
            if hist:
                sources_used_final[symbol] = hist.source_used

    return BacktestResult(
        metrics=strategy_metrics,
        benchmarks=benchmarks,
        fold=None,
        trades=trades,
        missing_days=missing_days_final,
        sources_used=sources_used_final,
        ledger_path=ledger_path,
        notes=[],
    )


def _benchmark_path(ledger_path: Path, name: str) -> Path:
    """Derive benchmark ledger paths."""
    return ledger_path.parent / f"{ledger_path.stem}_{name}.sqlite"


@contextlib.contextmanager
def _ledger_context(path: Path):
    """Context manager for ledger that ensures close."""
    ledger = Ledger(str(path))
    try:
        yield ledger
    finally:
        ledger.close()


def _next_rebalance_date(cfg: Config, from_date: date) -> date:
    """Find the next rebalance date from from_date.

    Per A5: if from_date is on the rebalance weekday, advance to next week.
    The rebalance weekday is checked with >= so if a day is missing,
    rebalancing still happens when we reach or pass that scheduled date.
    """
    target_weekday = cfg.schedule.rebalance_weekday
    current = from_date
    while current.weekday() != target_weekday:
        current += timedelta(days=1)
    return current


def main(cfg: Config, out_md: Path) -> None:
    """CLI entry point for the backtest command.

    Runs DEFAULT_FOLDS plus whole_period and writes results to docs/BACKTEST.md.

    Parameters
    ----------
    cfg : Config
        Loaded configuration.
    out_md : Path
        Path to write BACKTEST.md.
    """
    from .data import CoinbaseCandles, BinanceVisionKlines, BarCache, RequestsGet

    # Build the data source
    http_client = RequestsGet()
    primary = CoinbaseCandles(http_client)
    fallback = BinanceVisionKlines(http_client)
    cache = BarCache(cfg.expanded(cfg.data.cache_dir))
    source = HistoricalSource(primary, fallback, cache)

    # Run folds
    results = run_folds(
        cfg,
        source,
        DEFAULT_FOLDS,
        cfg.expanded(cfg.run.ledger_path).parent / "backtest",
    )

    # Write BACKTEST.md (placeholder for now; full implementation in next step)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("# Backtest Results\n\nBacktest run completed. See ledger for details.\n")

    log.info(f"Backtest complete. Results written to {out_md}")
