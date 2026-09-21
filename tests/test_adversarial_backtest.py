"""Adversarial review of the backtest: lookahead and accounting attacks.

Written by the adversarial reviewer pass required by BUILD-PROMPT.md <workflow> item 4.
Nothing in this file edits a module under review. Every test asserts the behaviour the
spec requires, so a test that fails here names a defect in the code, not in the test.

The attacks, in the order the spec lists them in <backtest_protocol>:

  A1  run_backtest cannot return at all (NameError), so no backtest has ever run.
  A2  fills use a price from BEFORE the decision, not the t+1 open.
  A3  the same order is submitted twice and the ledger then calls a filled order rejected.
  A4  warmup bars are fetched and then thrown away, so every fold is signal dead.
  A5  one absent bar on the rebalance weekday stops all rebalancing for the rest of the run.
  A6  the ghost book never holds a stop, so the counterfactual is not the real strategy.
  A7  the three required benchmarks do not exist.
  A8  a stop derived from day t's close is tested against day t's own low.
  A9  max drawdown is measured on a different equity series than Sharpe and CAGR.
  A10 fee and slippage arithmetic, hand checked. This one passes and is here as the control.

Every test runs offline against hand built bars. Nothing here touches the network,
a credential, Alpaca, Telegram or Anthropic.

Mechanics worth knowing before reading the tests:

* ``run_backtest`` raises before it returns (A1), so the behavioural tests call
  ``_run_strategy_backtest`` directly with the histories ``run_backtest`` would have
  fetched. That is the same code path minus the broken metadata block.
* ``_run_strategy_backtest`` closes its ledger in a ``finally``, so the ledger file is
  reopened after the call to read orders, fills, decisions and the equity curve.
* ``SignalConfig.min_history_days`` is 50 for the shipped config, so a hand series needs
  at least 50 bars inside the window before the signal produces anything.
* 2024-01-01 is a Monday and Monday is the configured rebalance weekday.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fulcrum import report
from fulcrum.backtest import _run_strategy_backtest, run_backtest
from fulcrum.broker import FakeBroker
from fulcrum.data import DailyHistory, to_frame
from fulcrum.engine import Engine
from fulcrum.ledger import Ledger
from fulcrum.llm_filter import OffFilter
from fulcrum.models import Bar, OrderRequest
from fulcrum.notify import NullNotifier

FIRST_MONDAY = date(2024, 1, 1)
SYMBOL = "BTC/USD"


# ---------------------------------------------------------------------------
# hand built bars and the harness the backtest would have used
# ---------------------------------------------------------------------------


def _ts(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def uptrend(
    n: int,
    first_day: date = FIRST_MONDAY,
    symbol: str = SYMBOL,
    start_price: float = 100.0,
    overnight_gap: float = 0.002,
    intraday_body: float = 0.003,
    wick: float = 0.001,
) -> list[Bar]:
    """A rising series in which the previous close, this open and this close all differ.

    That separation is what makes the fill timing attack readable: each of the three
    candidate fill prices is a different number, so the recorded fill names the bar the
    backtest actually used. The small sine wobble keeps realized volatility strictly
    positive, which the sizing code needs before it will take any position at all.
    """
    bars: list[Bar] = []
    prev_close = float(start_price)
    for i in range(n):
        open_ = prev_close * (1.0 + overnight_gap)
        close = open_ * (1.0 + intraday_body + 0.0008 * math.sin(i))
        high = max(open_, close) * (1.0 + wick)
        low = min(open_, close) * (1.0 - wick)
        bars.append(
            Bar(symbol, _ts(first_day + timedelta(days=i)), open_, high, low, close, 1_000_000.0)
        )
        prev_close = close
    return bars


def by_day(bars: list[Bar]) -> dict[date, Bar]:
    return {b.ts.date(): b for b in bars}


def history(bars: list[Bar], start: date, end: date) -> DailyHistory:
    present = {b.ts.date() for b in bars}
    missing: list[date] = []
    day = start
    while day <= end:
        if day not in present:
            missing.append(day)
        day += timedelta(days=1)
    return DailyHistory(bars=list(bars), missing_dates=missing, source_used="hand", from_cache=False)


def drive(cfg, bars_by_symbol: dict[str, list[Bar]], start: date, end: date, ledger_path: Path):
    """Run the backtest loop exactly as ``run_backtest`` would, warmup fetch included.

    ``run_backtest`` fetches from ``start - (min_history_days + 5)`` so the engine has
    history before the window opens. That fetch is reproduced here so attack A4 tests the
    loop's handling of the warmup rather than a shortage of data.
    """
    universe = (cfg.universe.core + cfg.universe.candidates)[: cfg.universe.max_symbols]
    fetch_start = start - timedelta(days=cfg.signal.min_history_days + 5)
    histories: dict[str, DailyHistory] = {}
    for sym in universe:
        bars = bars_by_symbol.get(sym, [])
        histories[sym] = history(bars, fetch_start, end)
    return _run_strategy_backtest(
        cfg, universe, histories, start, end, Path(ledger_path), OffFilter(), NullNotifier()
    )


def reopen(ledger_path: Path) -> Ledger:
    """The loop closes its ledger in a finally, so read the file back to inspect it."""
    return Ledger(str(ledger_path))


def slipped_buy(price: float, cfg) -> float:
    return price * (1.0 + cfg.asset_class.slippage_bps / 1e4)


# ---------------------------------------------------------------------------
# A1  the backtest cannot complete
# ---------------------------------------------------------------------------


def test_run_backtest_returns_a_result(config, tmp_path):
    """run_backtest must return a BacktestResult for a period that has bars.

    fulcrum/backtest.py line 143 reads a name 'ledger' that is local to
    _run_strategy_backtest, so every backtest over a non empty period dies here. No
    backtest has ever produced a number, which is why docs/BACKTEST.md contains none.
    """
    bars = uptrend(90)

    class Source:
        def get_daily(self, symbol, start, end):
            return history(bars if symbol == SYMBOL else [], start, end)

    result = run_backtest(
        config, Source(), FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89), tmp_path / "a1.sqlite"
    )
    assert result.metrics is not None
    assert result.trades >= 0


# ---------------------------------------------------------------------------
# A2  fills must happen at the t+1 open
# ---------------------------------------------------------------------------


def test_fill_price_is_next_days_open_not_a_price_from_before_the_decision(config, tmp_path):
    """A buy decided on bar close t must fill at the open of t+1.

    BUILD-PROMPT <backtest_protocol>: "signals use data through bar close t; fills happen
    at t+1 open." Engine._execute submits to the broker synchronously inside rebalance,
    and on a rebalance day the backtest's pending order block has not run, so the broker
    still holds the price set at the end of the PREVIOUS day. The fill therefore uses day
    t minus 1's close while the decision used day t's close: a full bar of hindsight in
    the strategy's favour.
    """
    bars = uptrend(90)
    lookup = by_day(bars)
    start, end = FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89)
    path = tmp_path / "a2.sqlite"
    drive(config, {SYMBOL: bars}, start, end, path)

    led = reopen(path)
    try:
        fills = [f for f in led.fills() if f["side"] == "buy"]
        assert fills, "the hand series must produce at least one buy for this attack to mean anything"
        first = fills[0]
        order = led.order(first["client_order_id"])
        decision = [d for d in led.decisions() if d["decision_id"] == order["decision_id"]][0]
        decision_day = datetime.fromisoformat(decision["cycle_ts"]).date()
    finally:
        led.close()

    next_open = lookup[decision_day + timedelta(days=1)].open
    prev_close = lookup[decision_day - timedelta(days=1)].close
    same_close = lookup[decision_day].close
    actual = float(first["price"])

    assert actual != pytest.approx(slipped_buy(prev_close, config)), (
        "the fill used the close of the day BEFORE the decision, which the decision could "
        "not have known it would get"
    )
    assert actual != pytest.approx(slipped_buy(same_close, config)), (
        "the fill used the same close the signal read"
    )
    assert actual == pytest.approx(slipped_buy(next_open, config))


# ---------------------------------------------------------------------------
# A3  one decision, one submission, one truthful status
# ---------------------------------------------------------------------------


def test_a_filled_order_is_never_recorded_as_rejected(config, tmp_path):
    """No order may carry a fill row and a terminal status of rejected.

    The loop keeps cycle_result.submitted as pending_orders, but Engine._execute has
    already sent those orders to the broker. The next day the loop submits them again,
    FakeBroker raises DuplicateOrderError, the bare except catches it and writes status
    rejected over an order that filled. The ledger then disagrees with its own fills
    table, and every reconciliation and report built on order status is wrong.
    """
    bars = uptrend(90)
    path = tmp_path / "a3.sqlite"
    drive(config, {SYMBOL: bars}, FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89), path)

    led = reopen(path)
    try:
        filled_ids = {f["client_order_id"] for f in led.fills()}
        assert filled_ids, "the hand series must produce at least one fill"
        liars = [
            o["client_order_id"]
            for o in led.orders()
            if o["client_order_id"] in filled_ids and o["status"] == "rejected"
        ]
    finally:
        led.close()

    assert liars == [], f"orders with a fill row but status rejected: {liars}"


# ---------------------------------------------------------------------------
# A4  the warmup must reach the engine
# ---------------------------------------------------------------------------


def test_warmup_history_reaches_the_signal(config, tmp_path):
    """A fold must trade from its first rebalance, not 50 days in.

    run_backtest fetches start minus (min_history_days + 5) days of warmup, then
    _run_strategy_backtest clips every symbol to [start, end] and builds the feature
    frames from the clipped list. The warmup is fetched and discarded, so the first 50
    days of every fold see len(df) < min_history_days and are skipped. The 59 day stress
    fold in DEFAULT_FOLDS is almost entirely signal dead, which silently invalidates the
    walk forward: the folds measure warmup, not strategy.
    """
    warmup_days = config.signal.min_history_days + 5
    start = FIRST_MONDAY + timedelta(days=warmup_days + 30)
    end = start + timedelta(days=27)
    bars = uptrend(warmup_days + 30 + 28 + 2)
    path = tmp_path / "a4.sqlite"
    drive(config, {SYMBOL: bars}, start, end, path)

    led = reopen(path)
    try:
        in_window = [
            d for d in led.decisions()
            if start <= datetime.fromisoformat(d["cycle_ts"]).date() <= end
        ]
        informed = [d for d in in_window if d["thesis"] != "insufficient history"]
    finally:
        led.close()

    assert in_window, "the window must contain at least one rebalance"
    assert informed, (
        "every decision in the window says 'insufficient history' although "
        f"{warmup_days} days of warmup bars were supplied to the backtest"
    )


# ---------------------------------------------------------------------------
# A5  a gap in the data must not stop the schedule
# ---------------------------------------------------------------------------


def test_one_missing_rebalance_day_does_not_stop_every_later_rebalance(config, tmp_path):
    """Rebalancing must survive an absent bar on the rebalance weekday.

    _next_rebalance_date is only advanced inside 'if is_rebalance', and is_rebalance is
    'day == next_rebalance_date'. If that calendar day has no bar the loop walks straight
    past it and the equality is never true again, so the run stops rebalancing forever.
    D-011 records that Coinbase history may be incomplete and the data layer deliberately
    never fills a gap, so this fires on real data, not just on a hand series.
    """
    bars = uptrend(90)
    dropped = FIRST_MONDAY + timedelta(days=28)  # a Monday, well inside the run
    assert dropped.weekday() == config.schedule.rebalance_weekday
    holed = [b for b in bars if b.ts.date() != dropped]

    path = tmp_path / "a5.sqlite"
    drive(config, {SYMBOL: holed}, FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89), path)

    led = reopen(path)
    try:
        days = sorted({datetime.fromisoformat(d["cycle_ts"]).date() for d in led.decisions()})
    finally:
        led.close()

    after = [d for d in days if d > dropped]
    assert after, (
        f"no rebalance happened after the missing bar on {dropped}; the schedule died with "
        f"the gap. Rebalance days seen: {days}"
    )


# ---------------------------------------------------------------------------
# A6  the ghost book must be the same strategy minus the vetoes
# ---------------------------------------------------------------------------


def test_ghost_book_carries_the_same_stops_as_the_real_book(config, tmp_path):
    """The counterfactual book must run the same stops as the real one.

    GhostBook.stops is only ever written by _load from a persisted row, and persist
    writes back whatever _load put there. Nothing in Engine.rebalance or Engine.tick ever
    copies self.stops into self.ghost.stops, so the ghost book runs a no stop version of
    the strategy. The ghost minus real difference then mixes the filter's contribution
    with the absence of stop losses, and the measurement the spec calls "how the filter's
    contribution is measured continuously for the life of the bot" is not measuring the
    filter.
    """
    bars = uptrend(90)
    frame = to_frame(bars)
    now = _ts(bars[-1].ts.date()) + timedelta(hours=23)

    led = Ledger(str(tmp_path / "a6.sqlite"))
    try:
        led.start_run("adversarial", "dry_run", "hash", None, now)
        broker = FakeBroker(
            starting_cash=100_000.0,
            fee_bps=config.asset_class.taker_fee_bps,
            slippage_bps=config.asset_class.slippage_bps,
            min_notional_usd=config.asset_class.min_notional_usd,
        )
        broker.set_price(SYMBOL, bars[-2].close, now)
        engine = Engine(
            cfg=config, broker=broker, ledger=led, filt=OffFilter(),
            notifier=NullNotifier(), run_id="adversarial", scope="adversarial",
        )
        engine.rebalance({SYMBOL: frame}, now, headlines=())
        real_stops = dict(engine.stops)
        ghost_stops = dict(engine.ghost.stops)
        ghost_positions = [p.symbol for p in engine.ghost.broker.positions()] if engine.ghost.broker else []
    finally:
        led.close()

    assert real_stops, "the rebalance must open a position with a stop for this attack to mean anything"
    assert ghost_positions, "the ghost book must hold the position the real book opened"
    assert set(ghost_stops) == set(real_stops), (
        f"real book stops {sorted(real_stops)} but ghost book stops {sorted(ghost_stops)}; "
        "the counterfactual portfolio has no stop losses at all"
    )


# ---------------------------------------------------------------------------
# A7  the three benchmarks
# ---------------------------------------------------------------------------


def test_the_three_required_benchmarks_are_computed(config, tmp_path):
    """BUILD-PROMPT <backtest_protocol>: BTC buy and hold, equal weight buy and hold, cash.

    Both return paths of _run_strategy_backtest set benchmarks to an empty dict, so there
    is nothing to compare the strategy against and the protocol's "worse than BTC buy and
    hold on a risk adjusted basis" test cannot be applied. docs/BACKTEST.md states these
    are implemented and that all three pay the same fee and slippage as the strategy;
    neither statement is backed by code.
    """
    bars = uptrend(90)
    result = drive(config, {SYMBOL: bars}, FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89),
                   tmp_path / "a7.sqlite")

    assert set(result.benchmarks) >= {"btc_buy_hold", "equal_weight_buy_hold", "cash"}, (
        f"benchmarks present: {sorted(result.benchmarks)}"
    )


# ---------------------------------------------------------------------------
# A8  a stop may not be tested against a bar that closed before it existed
# ---------------------------------------------------------------------------


def test_stop_is_not_tested_against_the_low_of_its_own_entry_bar(config, tmp_path):
    """The stop set from day t's close may not fire on day t's own low.

    Step 10 of Engine.rebalance computes the stop from day t's close and ATR. Step 3 of
    the backtest loop then calls engine.tick with day t's LOW, which is a price that had
    already happened when the stop was created. The inversion happens on every rebalance
    day, for every held position, and does not depend on how fills are timed: the stop
    that fires is always newer than the low that fires it. This test uses the entry day
    only because that is the clean case where no earlier stop exists to muddy the result.
    The comment on that line says it sets "min(open, stop) for gap-day conservatism"; the
    code passes the low, which for a sell is the worst price of the day, not the
    conservative one.
    """
    bars = uptrend(60)
    # The spike is placed on the FIRST rebalance that can trade, which is the 50th bar of
    # the window (min_history_days) and is itself a Monday. No stop exists before that
    # day's close, so a correctly timed backtest cannot stop out on this bar: the only
    # stop in existence was created from the very close that follows this low. The bar
    # recovers fully by its close and the next two days trade well above the stop.
    spike_day = FIRST_MONDAY + timedelta(days=config.signal.min_history_days - 1)
    assert spike_day.weekday() == config.schedule.rebalance_weekday
    patched: list[Bar] = []
    for b in bars:
        if b.ts.date() == spike_day:
            patched.append(Bar(b.symbol, b.ts, b.open, b.high, b.close * 0.5, b.close, b.volume))
        else:
            patched.append(b)

    path = tmp_path / "a8.sqlite"
    drive(config, {SYMBOL: patched}, FIRST_MONDAY, spike_day + timedelta(days=2), path)

    led = reopen(path)
    try:
        entries = [o for o in led.orders() if o["purpose"] in ("entry", "increase")]
        stops = [
            (o["client_order_id"], o["symbol"]) for o in led.orders() if o["purpose"] == "stop"
        ]
    finally:
        led.close()

    assert entries, "the spike day must be the day the first position is opened"
    assert stops == [], (
        f"a stop order fired on the strength of a low that preceded the stop that fired it: {stops}"
    )


# ---------------------------------------------------------------------------
# A9  one equity series, not two
# ---------------------------------------------------------------------------


def test_drawdown_and_sharpe_are_measured_on_the_same_equity_series(config, tmp_path):
    """Max drawdown and Sharpe must come from the same marks.

    The loop writes an equity row from engine.tick while every position is marked at the
    day's LOW (FakeBroker.set_price rewrites market_value), then writes a second row at
    the day's close, and on a rebalance day a third at the previous day's marks. report's
    _drawdown walks every raw row, so max drawdown is measured on intraday low marks,
    while daily_returns uses daily_last and so measures Sharpe, volatility and CAGR on
    close marks. The two headline risk numbers in the report describe different series.
    """
    bars = uptrend(90)
    path = tmp_path / "a9.sqlite"
    drive(config, {SYMBOL: bars}, FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89), path)

    led = reopen(path)
    try:
        m = report.metrics(led, config.asset_class.trading_days_per_year)
        daily = report.daily_last(report.equity_series(led))
    finally:
        led.close()

    peak = float("-inf")
    close_drawdown = 0.0
    for value in daily.to_numpy():
        peak = max(peak, float(value))
        if peak > 0:
            close_drawdown = max(close_drawdown, 1.0 - float(value) / peak)

    assert m.max_drawdown == pytest.approx(close_drawdown, abs=1e-9), (
        f"max drawdown from all equity rows {m.max_drawdown:.6f} but from the daily close "
        f"marks that Sharpe uses {close_drawdown:.6f}"
    )


# ---------------------------------------------------------------------------
# A11  simulated time, not wall clock time
# ---------------------------------------------------------------------------


def test_backtest_orders_and_fills_are_stamped_with_simulated_time(config, tmp_path):
    """Every row a backtest writes must be dated inside the backtest window.

    FakeBroker stamps Fill.filled_at and OrderAck.submitted_at from its clock, which
    defaults to models.utcnow, and Ledger.record_order_intent stamps created_at from
    utcnow as well. The backtest never injects a simulated clock, so a run over January
    2024 writes orders and fills dated today. The equity curve is dated correctly, from
    the now_ts the loop passes in, so the ledger holds two incompatible time bases.

    This is not cosmetic. Every windowed query in the engine is driven by the SIMULATED
    now and reads rows stamped with the REAL now, so each window silently becomes "the
    whole run": the 24h trade cap (Ledger.risk_increasing_orders_since), the fee budget
    alarm (fees_since, 30 days) and the slippage alarm (fills since 1 day). See A12.
    """
    start, end = FIRST_MONDAY, FIRST_MONDAY + timedelta(days=89)
    bars = uptrend(90)
    path = tmp_path / "a11.sqlite"
    drive(config, {SYMBOL: bars}, start, end, path)

    window_end = _ts(end) + timedelta(days=1)
    led = reopen(path)
    try:
        bad_fills = [
            f["filled_at"] for f in led.fills()
            if not (_ts(start) <= datetime.fromisoformat(f["filled_at"]) <= window_end)
        ]
        bad_orders = [
            o["created_at"] for o in led.orders()
            if not (_ts(start) <= datetime.fromisoformat(o["created_at"]) <= window_end)
        ]
    finally:
        led.close()

    assert bad_fills == [], f"fills dated outside the backtest window: {bad_fills[:3]}"
    assert bad_orders == [], f"orders dated outside the backtest window: {bad_orders[:3]}"


# ---------------------------------------------------------------------------
# A12  the 24h trade cap in a backtest
# ---------------------------------------------------------------------------


def test_twenty_four_hour_trade_cap_counts_only_the_last_day(config, tmp_path):
    """The rolling 24h cap must count orders from the last 24 hours of simulated time.

    Direct consequence of A11, shown as a number. Ten entry orders are written by the
    same code path the engine uses, then the window the engine asks for during a 2024
    backtest is queried. The rows carry a wall clock created_at, so every one of them
    falls inside a window that starts in 2024 and the count comes back 10 instead of 0.

    This is a landmine rather than a live failure today. risk_increasing_orders_since
    filters out rows with status 'rejected', and A3's duplicate re-submit marks every
    filled order rejected on the following day, so rows currently drop out of the count
    one day after they are written and the cap does not bind. Two defects are partly
    cancelling. Fix A3 on its own, without also giving the backtest a simulated clock,
    and the 24h cap turns into a lifetime cap of ten buys for the whole run.

    fees_since has no such masking: fills are never restatused, so the fee budget alarm
    sums every fill in the run and annualizes it by 365/30.
    """
    path = tmp_path / "a12.sqlite"
    led = Ledger(str(path))
    try:
        # D-014: record_order_intent requires decided_at parameter for engine clock filtering
        for i in range(10):
            led.record_order_intent(
                OrderRequest(f"cid-{i}", f"dec-{i}", SYMBOL, "buy", "entry", notional_usd=100.0),
                decided_at=_ts(FIRST_MONDAY + timedelta(days=i))
            )
        simulated_now = _ts(FIRST_MONDAY + timedelta(days=60))
        counted = led.risk_increasing_orders_since(simulated_now - timedelta(hours=24))
    finally:
        led.close()

    assert counted == 0, (
        f"{counted} orders counted in the 24 hours before {simulated_now.isoformat()}, "
        "although none of them were placed in that window"
    )


# ---------------------------------------------------------------------------
# A10  the control: fee and slippage arithmetic by hand
# ---------------------------------------------------------------------------


def test_fee_and_slippage_are_charged_once_each_by_hand(config):
    """Hand computed round trip. This test passes: the FakeBroker arithmetic is correct.

    Buy 10,000 USD of a 100.00 asset with 10 bps slippage and 25 bps taker fee.
      fill price   = 100 * 1.0010 = 100.10
      quantity     = 10,000 / 100.10 = 99.9000999... units
      fee          = 10,000 * 0.0025 = 25.00
      cash after   = 100,000 - 10,000 - 25 = 89,975.00
    Then sell the whole position at the same 100.00 mark.
      fill price   = 100 * 0.9990 = 99.90
      proceeds     = 99.9000999... * 99.90 = 9,980.019980...
      fee          = proceeds * 0.0025 = 24.950049...
    Fee is charged once per fill and slippage is applied once in the price, on both
    sides. Neither is double counted and neither is skipped.
    """
    broker = FakeBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0, min_notional_usd=10.0)
    now = _ts(FIRST_MONDAY)
    broker.set_price(SYMBOL, 100.0, now)

    broker.submit(OrderRequest("buy-1", "d1", SYMBOL, "buy", "entry", notional_usd=10_000.0))
    buy = broker.fills_for("buy-1")[0]
    assert buy.price == pytest.approx(100.10)
    assert buy.qty == pytest.approx(10_000.0 / 100.10)
    assert buy.fee_usd == pytest.approx(25.00)
    assert broker.cash == pytest.approx(89_975.00)

    held = broker.positions()[0].qty
    broker.submit(OrderRequest("sell-1", "d1", SYMBOL, "sell", "exit", qty=held))
    sell = broker.fills_for("sell-1")[0]
    proceeds = held * 99.90
    assert sell.price == pytest.approx(99.90)
    assert sell.fee_usd == pytest.approx(proceeds * 0.0025)
    assert broker.cash == pytest.approx(89_975.00 + proceeds - proceeds * 0.0025)
    assert broker.total_fees == pytest.approx(25.00 + proceeds * 0.0025)
