"""Adversarial pass number two: ways an order could bypass or double fire END TO END.

`test_adversarial_core.py` already attacked `risk.check_order` on its own and it held up.
This file attacks the whole path instead: engine, scheduler, ledger, ghost book, kill
switch and filter working together, because that is where an order can go out twice, or
go out without the gate, or be stopped by something that is not allowed to stop it.

Everything here runs offline: FakeBroker (and two honest variations on it), FakeFilter,
FakeNotifier, a real Ledger on tmp_path, and clocks handed in by the test. Nothing
touches the network, a credential, Alpaca, Telegram or Anthropic.

Reading the failures: every test that fails against the code as it stands carries a
comment block above it saying so, what the defect is, and the one line fix. Those tests
are the finding. Do not weaken or delete one to get the suite green; fix the module.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from fulcrum import engine as engine_mod
from fulcrum import risk as rk
from fulcrum.broker import BrokerError, DuplicateOrderError, FakeBroker
from fulcrum.engine import Engine
from fulcrum.ledger import Ledger
from fulcrum.llm_filter import FakeFilter
from fulcrum.models import FilterResult, Order, OrderAck, OrderRequest, utcnow
from fulcrum.notify import FakeNotifier
from fulcrum.scheduler import Scheduler

BTC = "BTC/USD"
ETH = "ETH/USD"
SOL = "SOL/USD"


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    """Every timestamp in this repo has to be aware UTC, so build them all one way."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# bars
# ---------------------------------------------------------------------------


def make_bars(symbol: str, start: datetime, days: int, first_close: float,
              trend: float = 1.002) -> pd.DataFrame:
    """A daily series that trends the way the test asks, with repeatable wobble.

    The wobble matters: with perfectly smooth returns the realized volatility is zero,
    sizing refuses to size anything, and the test would prove nothing. The seed comes
    from the symbol and the start date, so two runs of the suite see identical prices.
    """
    import hashlib

    import numpy as np

    seed = int(hashlib.md5(f"{symbol}:{start.isoformat()}".encode()).hexdigest()[:8], 16) % (2**31)
    rng = np.random.RandomState(seed)
    rows = []
    close = first_close
    for i in range(days):
        open_ = close
        step = (trend - 1.0) + rng.normal(0, 0.005)
        close = open_ * (1.0 + step)
        rows.append({
            "timestamp": start + timedelta(days=i),
            "open": open_,
            "high": max(open_, close) * 1.01,
            "low": min(open_, close) * 0.99,
            "close": close,
            "volume": 1000.0,
        })
    frame = pd.DataFrame(rows)
    frame.set_index("timestamp", inplace=True)
    return frame


def rising_book(now: datetime, days: int = 61) -> dict[str, pd.DataFrame]:
    """Three coins all trending up, so every one of them is a buy candidate."""
    start = now - timedelta(days=days - 1)
    return {
        BTC: make_bars(BTC, start, days, 45000.0, trend=1.002),
        ETH: make_bars(ETH, start, days, 2500.0, trend=1.002),
        SOL: make_bars(SOL, start, days, 100.0, trend=1.002),
    }


def last_closes(bars: dict[str, pd.DataFrame]) -> dict[str, float]:
    return {s: float(df["close"].iloc[-1]) for s, df in bars.items()}


# ---------------------------------------------------------------------------
# brokers
# ---------------------------------------------------------------------------


class SpyBroker(FakeBroker):
    """A FakeBroker that keeps the order requests it was handed, failures included.

    The list is written before the fill is attempted, so an order that blew up on the
    way to the venue is still counted as "this went out". That is the only honest way
    to count double submits.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submitted: list[OrderRequest] = []

    def submit(self, req: OrderRequest) -> OrderAck:
        self.submitted.append(req)
        return super().submit(req)

    def buys_for(self, symbol: str) -> list[OrderRequest]:
        return [r for r in self.submitted if r.symbol == symbol and r.side == "buy"]

    def sells_for(self, symbol: str) -> list[OrderRequest]:
        return [r for r in self.submitted if r.symbol == symbol and r.side == "sell"]


class PendingFakeBroker(FakeBroker):
    """A venue that accepts an order and fills it later, which is what Alpaca actually does.

    This is not a rigged fake. `AlpacaBroker.submit` returns whatever status the venue
    sends back, and a market crypto order commonly comes back "accepted" first;
    `AlpacaBroker.fills_for` returns an empty list while `filled_qty` is still zero, and
    `GET /v2/positions` does not show the position until the fill happens. So between
    submit and fill the bot sees exactly what this class shows it: an open order, no
    position, and unchanged cash. Duplicate ids are refused the way the venue refuses
    them (HTTP 422, D-009).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submitted: list[OrderRequest] = []

    def submit(self, req: OrderRequest) -> OrderAck:
        self.submitted.append(req)
        if req.client_order_id in self._orders:
            raise DuplicateOrderError(f"client_order_id {req.client_order_id} already exists")
        self.latest_price(req.symbol)  # a symbol with no price is still refused
        self._seq += 1
        now = self._clock()
        broker_id = f"pending-{self._seq:08d}"
        self._orders[req.client_order_id] = Order(
            client_order_id=req.client_order_id,
            broker_order_id=broker_id,
            symbol=req.symbol,
            side=req.side,
            status="accepted",
            submitted_at=now,
            notional_usd=req.notional_usd,
            qty=req.qty,
            filled_qty=0.0,
            filled_avg_price=None,
            filled_at=None,
        )
        return OrderAck(client_order_id=req.client_order_id, broker_order_id=broker_id,
                        status="accepted", submitted_at=now)

    def fills_for(self, client_order_id: str) -> list:
        return []

    def buys_for(self, symbol: str) -> list[OrderRequest]:
        return [r for r in self.submitted if r.symbol == symbol and r.side == "buy"]


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


class ExplodingFilter:
    """A filter whose evaluate raises instead of returning a failed FilterResult.

    Any filter can end up here: a bug in a future implementation, a serialization error,
    a library that raises something the wrapper does not catch. The fail open rule in
    BUILD-PROMPT.md `<llm_filter>` says the rules must proceed anyway, so the engine has
    to be the thing that guarantees it, not the goodwill of each filter class.
    """

    def __init__(self, mode: str = "shadow") -> None:
        self.mode = mode
        self.calls = 0

    def evaluate(self, candidates, context) -> FilterResult:
        self.calls += 1
        raise RuntimeError("the filter fell over")


class VetoEverythingFilter:
    """Vetoes every candidate it is shown, and records what it was shown."""

    def __init__(self, mode: str = "active") -> None:
        self.mode = mode
        self.seen: list[str] = []

    def evaluate(self, candidates, context) -> FilterResult:
        from fulcrum.models import Verdict

        self.seen.extend(c.symbol for c in candidates)
        return FilterResult(
            verdicts=tuple(
                Verdict(c.symbol, "veto", "vetoed by the test", 0.9, "never") for c in candidates
            ),
            error=None,
            latency_ms=1,
        )


# ---------------------------------------------------------------------------
# wiring helpers
# ---------------------------------------------------------------------------


def open_ledger(tmp_path, name: str = "adv.sqlite") -> Ledger:
    ledger = Ledger(str(tmp_path / name))
    ledger.start_run("adv_run", "dry_run", "adv_hash", None, utc(2025, 1, 1))
    return ledger


def build_engine(cfg, broker, ledger, filt=None, notifier=None, scope: str = "adv_scope") -> Engine:
    return Engine(
        cfg=cfg,
        broker=broker,
        ledger=ledger,
        filt=filt if filt is not None else FakeFilter(mode=cfg.llm_filter.mode),
        notifier=notifier if notifier is not None else FakeNotifier(),
        run_id="adv_run",
        scope=scope,
    )


def with_filter_mode(cfg, mode: str):
    """The example config ships with the filter off for tests; turn it on for one test."""
    return replace(cfg, llm_filter=replace(cfg.llm_filter, mode=mode))


def providers(bars: dict[str, pd.DataFrame], prices: dict[str, float]):
    """Bars and prices handed to the scheduler, so it never reaches for the network."""

    def bars_provider(symbols, now):
        return {s: df for s, df in bars.items() if s in set(symbols)}

    def price_provider(symbols):
        return {s: p for s, p in prices.items() if s in set(symbols)}

    return bars_provider, price_provider


def stop_after(passes: int, state: dict):
    """A sleep that advances the test clock and stops the loop after N passes."""

    def sleep(_seconds: float) -> None:
        state["i"] += 1
        if state["i"] >= passes:
            raise KeyboardInterrupt
    return sleep


# ===========================================================================
# 1. Double fire: the same weekly decision sent twice
# ===========================================================================


# FAILS against the code as it stands. Defect: `Engine._decision_id` hashes `now`, the
# wall clock instant the cycle happened, so the same weekly decision made at two
# different instants gets two different decision ids and therefore two different
# client_order_ids. `Scheduler.run_once` deliberately does not use up the weekly slot
# (see test_scheduler.py::test_a_cycle_run_by_hand_never_uses_up_the_weekly_rebalance,
# which is correct and must stay), so a cycle run by hand followed by the loop rebalances
# twice in one week. While the first orders are still only accepted at the venue the
# position has not moved, so the second cycle computes the same buy and sends it again.
# Fix: key the decision id to the scheduled weekly slot, not to `now`. One line in
# Engine.rebalance: build the id from scheduler.last_scheduled_weekly(now, weekday, hour)
# instead of from now. Then the second cycle rebuilds the same client_order_id,
# ledger.record_order_intent returns False, and the duplicate never leaves the machine.
def test_a_cycle_run_by_hand_then_the_loop_must_not_send_the_same_buy_twice(config, tmp_path):
    now = utc(2025, 1, 15, 10, 0)  # a Wednesday; the weekly slot is Monday 01:00 UTC
    bars = rising_book(now)
    prices = last_closes(bars)
    broker = PendingFakeBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                               min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        bars_provider, price_provider = providers(bars, prices)
        clock = {"t": now}
        sched = Scheduler(config, engine, bars_provider, price_provider,
                          sleep=stop_after(1, {"i": 0}), clock=lambda: clock["t"])

        sched.run_once(dry_run=False)
        first_round = len(broker.submitted)
        assert first_round > 0, "the setup is wrong if the first cycle sent nothing"

        clock["t"] = now + timedelta(minutes=20)
        sched.run_forever()

        for symbol in (BTC, ETH, SOL):
            ids = {r.client_order_id for r in broker.buys_for(symbol)}
            assert len(ids) <= 1, (
                f"{symbol} was bought under {len(ids)} different order ids in one week. "
                "The venue cannot spot the duplicate, so the bot bought twice."
            )
    finally:
        ledger.close()


# FAILS against the code as it stands. Same defect as above, reached a different way.
# `Scheduler._maybe_rebalance` writes its "this week is done" bookkeeping row AFTER
# `engine.rebalance` returns, and orders are sent in step 9, well before that. So any
# failure in steps 10 to 12 (here: the ghost book write) leaves the orders sent and the
# week marked undone, and the next wake up fifteen minutes later sends them all again
# under fresh ids. Fix: the same one, key the decision id to the scheduled slot. Marking
# the week done before the cycle is NOT the fix; that would silently skip a real week.
def test_a_rebalance_that_dies_after_sending_orders_must_not_resend_them(config, tmp_path):
    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    prices = last_closes(bars)
    broker = PendingFakeBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                               min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)

        def die(_ts):
            raise RuntimeError("the ledger write after the orders went out failed")

        engine.ghost.persist = die  # step 11, after the orders of step 9

        bars_provider, price_provider = providers(bars, prices)
        times = [now, now + timedelta(minutes=15), now + timedelta(minutes=30)]
        state = {"i": 0}
        sched = Scheduler(config, engine, bars_provider, price_provider,
                          sleep=stop_after(2, state),
                          clock=lambda: times[min(state["i"], len(times) - 1)])
        sched.run_forever()

        for symbol in (BTC, ETH, SOL):
            ids = {r.client_order_id for r in broker.buys_for(symbol)}
            assert len(ids) <= 1, (
                f"a failed cycle resent the {symbol} buy under a second order id, so the "
                "same weekly decision reached the venue twice."
            )
    finally:
        ledger.close()


# ===========================================================================
# 2. The kill switch
# ===========================================================================


# FAILS against the code as it stands. Defect: `KillSwitch.consume_reset_file` returns
# early when the switch is not tripped, WITHOUT recording the file it just saw. So a
# reset file that was created at any earlier time and left on disk is still unconsumed
# when a trip finally happens, and `Engine.tick` calls update() then
# consume_reset_file() in the same pass: the brake trips, Dave gets the "kill switch
# tripped" message, and the same tick quietly lets the brake off again. Dave is alarmed
# about a brake that is already off, and the next rebalance buys. Fix: in
# consume_reset_file, when the switch is not tripped and the file exists, mark that
# mtime consumed and return False. A reset then only ever applies to a file Dave made
# after the trip he means to clear.
def test_a_reset_file_left_lying_around_must_not_cancel_the_next_trip(config, tmp_path):
    prices_up = {BTC: 1000.0}
    broker = FakeBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                        min_notional_usd=10.0)
    broker.set_price(BTC, 1000.0)
    broker.submit(OrderRequest(client_order_id="seed", decision_id="seed", symbol=BTC,
                               side="buy", purpose="entry", notional_usd=60_000.0))
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        reset_file = config.expanded(config.run.kill_reset_file)
        reset_file.parent.mkdir(parents=True, exist_ok=True)

        t0 = utc(2025, 3, 1, 0, 0)
        engine.tick(prices_up, t0)  # sets the high water mark
        assert not engine.kill.tripped()

        # Dave makes a reset file at a moment when nothing is tripped. Perhaps he cleared
        # an earlier brake and the file stayed, perhaps he made it by mistake.
        reset_file.write_text("reset", encoding="utf-8")
        engine.tick(prices_up, t0 + timedelta(minutes=15))
        assert not engine.kill.tripped()

        # Now the account falls far enough to trip the brake.
        broker.set_price(BTC, 500.0)
        result = engine.tick({BTC: 500.0}, t0 + timedelta(minutes=30))

        assert result.kill_tripped_now, "the setup is wrong if the brake did not trip"
        assert not result.kill_reset_now, (
            "a reset file made before the trip cleared the brake in the same tick that "
            "tripped it."
        )
        assert engine.kill.tripped(), (
            "the brake is off again without Dave doing anything after the trip, so the "
            "next rebalance is free to buy."
        )
    finally:
        ledger.close()


def test_a_tripped_brake_blocks_every_buy_and_still_lets_a_sale_out(config, tmp_path):
    """The invariant the brake exists for, checked end to end and not on check_order alone."""
    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    bars[SOL] = make_bars(SOL, now - timedelta(days=60), 61, 200.0, trend=0.99)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    broker.submit(OrderRequest(client_order_id="seed-sol", decision_id="seed", symbol=SOL,
                               side="buy", purpose="entry", notional_usd=8_000.0))
    broker.submitted.clear()
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        ledger.set_kill_state(tripped=True, hwm=200_000.0, tripped_at=now,
                              drawdown_at_trip=0.5)
        assert engine.kill.tripped()

        engine.rebalance(bars, now)

        assert not [r for r in broker.submitted if r.side == "buy"], (
            "a buy reached the venue while the brake was on."
        )
        assert broker.sells_for(SOL), (
            "the falling coin was not sold. A tripped brake must never block an exit."
        )
    finally:
        ledger.close()


# ===========================================================================
# 3. The filter must never be able to stop the rules
# ===========================================================================


# FAILS against the code as it stands. Defect: `Engine.rebalance` step 8 calls
# `self.filter.evaluate(...)` and `self.ledger.record_filter_result(...)` with nothing
# around them, and step 9 sends the SELLS first. So anything thrown by the filter takes
# the whole cycle down before a single exit is submitted. That breaks three separate
# rules at once: fail open (BUILD-PROMPT `<llm_filter>`: the filter must never be able
# to take the bot down), "nothing may ever block an exit" (`<risk_layer>` item 3), and
# "it never sees or affects exits" (`<llm_filter>` line 1). ClaudeFilter happens to
# catch everything today, so this is a contract the engine leaves unenforced rather than
# a live hole, but the engine is the only place it can be enforced. Fix: wrap step 8 in
# try/except Exception, and on failure log it, record an alert, and carry on with an
# empty veto set.
def test_a_filter_that_throws_must_not_stop_the_rules_or_block_an_exit(config, tmp_path):
    cfg = with_filter_mode(config, "shadow")
    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    bars[SOL] = make_bars(SOL, now - timedelta(days=60), 61, 200.0, trend=0.99)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    broker.submit(OrderRequest(client_order_id="seed-sol", decision_id="seed", symbol=SOL,
                               side="buy", purpose="entry", notional_usd=8_000.0))
    broker.submitted.clear()
    ledger = open_ledger(tmp_path)
    try:
        filt = ExplodingFilter(mode="shadow")
        engine = build_engine(cfg, broker, ledger, filt=filt)

        try:
            engine.rebalance(bars, now)
        except Exception as e:  # noqa: BLE001  this IS the finding
            pytest.fail(
                f"a broken filter took the whole cycle down with {type(e).__name__}. "
                "Fail open means the rules carry on."
            )

        assert filt.calls == 1, "the setup is wrong if the filter was never asked"
        assert broker.sells_for(SOL), (
            "the exit for the falling coin never went out because the filter fell over. "
            "The filter is not allowed to touch an exit at all."
        )
        assert [r for r in broker.submitted if r.side == "buy"], (
            "no buy went out either. A filter failure means allow, not stop everything."
        )
    finally:
        ledger.close()


# The first draft of this test compared the WHOLE stops dictionary between the two runs
# and failed. That draft was wrong, not the code: a veto stops a buy, the buy never
# becomes a position, and a position that does not exist has no stop to carry. Losing
# that entry is the veto doing its one permitted job, not the filter reaching a stop.
# The test now seeds a coin the rules will hold either way (SOL is exited, BTC sits in
# the no trade band) and checks the stop on the coin that is held in both runs, which is
# the thing a veto genuinely must not be able to move.
def test_a_veto_cannot_change_an_exit_a_stop_or_a_size(config, tmp_path):
    """Run the same cycle twice, once with the filter off and once vetoing everything.

    The exits, and the stop on a coin that is held in both runs, have to come out
    identical. Only the buys may differ, and only by not happening.
    """
    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    bars[SOL] = make_bars(SOL, now - timedelta(days=60), 61, 200.0, trend=0.99)
    prices = last_closes(bars)

    def run(cfg, filt, name):
        broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                           min_notional_usd=10.0)
        broker.set_prices(prices, ts=now)
        # SOL is held and the rules will exit it. BTC is held at roughly its target, so it
        # sits in the no trade band, is never a candidate, and is held in both runs.
        broker.submit(OrderRequest(client_order_id="seed-sol", decision_id="seed", symbol=SOL,
                                   side="buy", purpose="entry", notional_usd=8_000.0))
        broker.submit(OrderRequest(client_order_id="seed-btc", decision_id="seed", symbol=BTC,
                                   side="buy", purpose="entry", notional_usd=15_000.0))
        broker.submitted.clear()
        ledger = open_ledger(tmp_path, name)
        try:
            engine = build_engine(cfg, broker, ledger, filt=filt, scope="scope-" + name)
            engine.rebalance(bars, now)
            sells = [(r.symbol, round(r.qty or 0.0, 9)) for r in broker.submitted
                     if r.side == "sell"]
            return sells, dict(engine.stops)
        finally:
            ledger.close()

    off_sells, off_stops = run(config, FakeFilter(mode="off"), "off.sqlite")
    veto = VetoEverythingFilter(mode="active")
    veto_sells, veto_stops = run(with_filter_mode(config, "active"), veto, "veto.sqlite")

    assert veto.seen, "the setup is wrong if the filter was never shown a candidate"
    assert SOL not in veto.seen, "the filter was shown the coin the rules wanted to exit"
    assert BTC not in veto.seen, "the setup is wrong if the held coin was a candidate"
    assert veto_sells == off_sells, (
        "vetoing every buy changed which coins were sold or how much of them."
    )
    assert off_stops.get(BTC) is not None, "the setup is wrong if the held coin has no stop"
    assert veto_stops.get(BTC) == off_stops.get(BTC), (
        "vetoing every buy moved the stop on a coin the bot was already holding."
    )


# ===========================================================================
# 4. Stops
# ===========================================================================


# FAILS against the code as it stands. Defect: the stop order in `Engine.tick` builds
# its client_order_id from `f"stop-{symbol}-{now.timestamp()}"`, so it is not
# deterministic per stop. D-005 says order ids are deterministic so a crash and restart
# can never double submit, and a stop is an order. Two ways it bites, one cause:
#   forwards, when the venue answers a stop with a transport error the bot marks the
#   state unknown and leaves the stop armed, and the next tick sends a SECOND sale under
#   a different id, so the venue has no way to spot the duplicate;
#   backwards, if two ticks ever land on the same instant the id repeats,
#   record_order_intent returns False, and the stop is silently skipped while
#   self.stops still shows the position as protected.
# Fix: this one needs a small design decision, not a one line change, and a plain
# deterministic id on its own makes it worse. With a fixed id the retry hits
# record_order_intent, gets False, returns "already acted on", and the stop never fires
# again while self.stops still lists the position as protected: a double sale traded for
# silent loss of protection. Deriving the id from the stored stop level does not work
# either, because that level ratchets. What it needs: give the stop attempt a stable key
# (scope, entry_decision_id, symbol, "sell", seq) and, when the previous attempt is in
# status unknown, reconcile that client_order_id against the venue FIRST and only build
# attempt seq + 1 once the venue confirms the earlier attempt did not fill.
def test_a_stop_that_came_back_unknown_is_resent_under_the_same_order_id(config, tmp_path):
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_price(BTC, 1000.0)
    broker.submit(OrderRequest(client_order_id="seed", decision_id="seed", symbol=BTC,
                               side="buy", purpose="entry", notional_usd=20_000.0))
    broker.submitted.clear()
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        t0 = utc(2025, 4, 1, 12, 0)
        engine.tick({BTC: 1000.0}, t0)  # set the high water mark first
        engine.stops[BTC] = 950.0

        broker.set_price(BTC, 900.0)
        broker.fail_next = "network"  # the venue answers, or does not, and we cannot tell
        first = engine.tick({BTC: 900.0}, t0 + timedelta(minutes=15))
        assert not first.stops_fired, "the setup is wrong if the failed stop was recorded as done"

        second = engine.tick({BTC: 900.0}, t0 + timedelta(minutes=30))
        assert second.stops_fired == [BTC], "the stop should be retried on the next tick"

        ids = {r.client_order_id for r in broker.sells_for(BTC)}
        assert len(ids) == 1, (
            f"the same stop went out under {len(ids)} different order ids, so the venue "
            "cannot refuse the second one and the position can be sold twice."
        )
    finally:
        ledger.close()


# ===========================================================================
# 5. The rolling 24 hour trade cap
# ===========================================================================


# FAILS against the code as it stands. Defect: `Engine._state` asks the ledger for
# `risk_increasing_orders_since(now - 24h)` where `now` is the clock the engine was
# handed, but `ledger.risk_increasing_orders_since` filters on `orders.created_at`,
# which is `utcnow()` at the moment the row was written. The two clocks are the same
# only when the engine is running live. With any other clock, which is exactly what the
# backtest and every replay use, the comparison is meaningless: a clock ahead of the
# wall clock counts zero and the cap never binds at all, and a clock behind it counts
# every order ever written and the cap binds far too early. Either way the risk layer in
# a backtest is not the risk layer that runs live, so the backtest cannot tell Dave what
# the limits would have done. Fix: store the engine's own timestamp on the order row (a
# `decided_at` column, written from OrderRequest time in record_order_intent) and count
# the cap against that, not against created_at.
def test_the_trade_cap_counts_by_the_bots_clock_not_the_wall_clock(config, tmp_path):
    now = utc(2099, 1, 15, 10, 0)  # any clock that is not the wall clock
    bars = rising_book(now)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    ledger = open_ledger(tmp_path)
    try:
        # More risk increasing orders in the last hour of the bot's own time than the cap
        # allows in a day.
        for i in range(config.risk.max_trades_per_24h + 2):
            ledger.record_order_intent(OrderRequest(
                client_order_id=f"earlier-{i}", decision_id=f"earlier-{i}", symbol=BTC,
                side="buy", purpose="entry", notional_usd=100.0), decided_at=now)

        engine = build_engine(config, broker, ledger)
        engine.rebalance(bars, now)

        assert not [r for r in broker.submitted if r.side == "buy"], (
            "the 24 hour trade cap let a buy through with "
            f"{config.risk.max_trades_per_24h + 2} risk increasing orders already on the "
            "books, because it compares the bot's clock against wall clock timestamps."
        )
    finally:
        ledger.close()


# FAILS against the code as it stands. The same defect as above, seen from the direction
# that actually bites, because `fulcrum/backtest.py` builds a real Engine (line 285) and
# calls rebalance (line 353) and tick (line 374) with historical timestamps. Every order
# a backtest writes carries a created_at of the moment the backtest ran, so from the
# bot's point of view every order it has ever placed is always inside the last 24 hours.
# The cap therefore binds permanently after the tenth buy and stays bound for the rest
# of a multi year run. The risk layer measured by the backtest is not the risk layer that
# will run live, which is the whole point of running the backtest through the same engine.
def test_orders_from_a_fortnight_ago_in_the_bots_own_time_must_not_fill_todays_budget(
        config, tmp_path):
    first = utc(2024, 6, 3, 1, 0)
    bars = rising_book(first)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=first)
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        engine.rebalance(bars, first)
        assert [r for r in broker.submitted if r.side == "buy"], (
            "the setup is wrong if the first cycle bought nothing"
        )

        a_fortnight_later = first + timedelta(days=14)
        recent = ledger.risk_increasing_orders_since(a_fortnight_later - timedelta(hours=24))
        assert recent == 0, (
            f"{recent} buys from a fortnight earlier in the bot's own time are still "
            "counted as today's, so in a backtest the 24 hour cap binds forever after "
            "the tenth buy."
        )
    finally:
        ledger.close()


def test_an_order_row_stuck_on_new_still_counts_towards_the_trade_cap(config, tmp_path):
    """A crash between writing the intent and hearing back from the venue leaves the row
    on "new". The cap has to keep counting it, because the order may well have gone out.

    This one passes. It is here because the brief asked whether a crash could leave the
    table the cap reads in a state that lets an extra order through, and the answer is
    that it errs the safe way: an unknown order counts, and only a venue rejection stops
    counting. Recorded so a future change cannot quietly flip the direction.
    """
    ledger = open_ledger(tmp_path)
    try:
        now = utcnow()
        ledger.record_order_intent(OrderRequest(
            client_order_id="crashed", decision_id="d1", symbol=BTC, side="buy",
            purpose="entry", notional_usd=500.0), decided_at=now)
        assert ledger.order("crashed")["status"] == "new"
        assert ledger.risk_increasing_orders_since(now - timedelta(hours=24)) == 1

        ledger.record_order_intent(OrderRequest(
            client_order_id="refused", decision_id="d2", symbol=ETH, side="buy",
            purpose="entry", notional_usd=500.0), decided_at=now)
        ledger.update_order("refused", "rejected", note="venue said no")
        assert ledger.risk_increasing_orders_since(now - timedelta(hours=24)) == 1, (
            "an order the venue refused must not eat the day's budget"
        )
    finally:
        ledger.close()


# ===========================================================================
# 6. The gate itself: nothing reaches the venue around it
# ===========================================================================


def test_every_order_that_reached_the_venue_came_out_of_the_risk_gate(config, tmp_path, monkeypatch):
    """Watch check_order and the venue at the same time for a whole cycle and a stop.

    Any order at the venue whose id never came out of a RiskDecision means some path
    built an order and sent it without the gate.
    """
    approved: set[str] = set()
    real_check = rk.check_order

    def watched(*args, **kwargs):
        decision = real_check(*args, **kwargs)
        approved.update(o.client_order_id for o in decision.orders)
        return decision

    monkeypatch.setattr(engine_mod.rk, "check_order", watched)

    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    bars[SOL] = make_bars(SOL, now - timedelta(days=60), 61, 200.0, trend=0.99)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    broker.submit(OrderRequest(client_order_id="seed-sol", decision_id="seed", symbol=SOL,
                               side="buy", purpose="entry", notional_usd=8_000.0))
    broker.submitted.clear()
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        engine.rebalance(bars, now)
        engine.stops[BTC] = prices[BTC] * 1.01  # force a stop on the next tick
        engine.tick(prices, now + timedelta(minutes=15))

        sent = {r.client_order_id for r in broker.submitted}
        assert sent, "the setup is wrong if nothing was sent"
        assert sent <= approved, (
            f"orders reached the venue without passing the risk gate: {sorted(sent - approved)}"
        )
    finally:
        ledger.close()


def test_the_intent_is_written_to_the_ledger_before_the_venue_is_called(config, tmp_path):
    """Idempotency rests on the ledger row existing first. Check it at the venue's door."""
    seen_without_a_row: list[str] = []

    class CheckingBroker(SpyBroker):
        def submit(self, req: OrderRequest) -> OrderAck:
            row = self.ledger.order(req.client_order_id)
            if row is None:
                seen_without_a_row.append(req.client_order_id)
            return super().submit(req)

    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    prices = last_closes(bars)
    broker = CheckingBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                            min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    ledger = open_ledger(tmp_path)
    broker.ledger = ledger
    try:
        engine = build_engine(config, broker, ledger)
        engine.rebalance(bars, now)
        assert broker.submitted, "the setup is wrong if nothing was sent"
        assert not seen_without_a_row, (
            f"orders reached the venue with no ledger row to recover them by: "
            f"{seen_without_a_row}"
        )
    finally:
        ledger.close()


def test_a_rate_limited_order_that_actually_landed_cannot_become_a_second_order(
        config, tmp_path):
    """The one retry inside AlpacaBroker, driven through the engine, still offline.

    alpaca.py's docstring asserts that a 429 means the request was refused before it was
    processed, so retrying it cannot duplicate. That is an assumption, so this is the
    worst case where the assumption is wrong: the first POST really did reach the venue
    and the 429 came back anyway. The retry has to carry the same client_order_id, the
    venue then refuses it with the documented 422 (D-009), and the engine has to treat
    that as one order in an unknown state for reconcile, never as a second order.

    This one passes. It is the deterministic client_order_id earning its keep on the one
    path in the code that repeats a POST.
    """
    from fulcrum.alpaca import AlpacaBroker, FakeTransport
    from fulcrum.broker import ALPACA_PAPER_URL
    from fulcrum.creds import Credential

    transport = FakeTransport()
    transport.script_rate_limited("POST", "/v2/orders", retry_after="1")
    transport.script_duplicate()
    broker = AlpacaBroker(
        base_url=ALPACA_PAPER_URL,
        credential=Credential(user="unused", secret="unused"),
        transport=transport,
        asset_class=config.asset_class,
        sleep=lambda _s: None,
    )
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        now = utc(2025, 1, 15, 10, 0)
        req = OrderRequest(
            client_order_id=rk.make_client_order_id("adv_scope", "d1", BTC, "buy", seq=0),
            decision_id="d1", symbol=BTC, side="buy", purpose="entry", notional_usd=5_000.0)

        submitted, note = engine._execute(req, 45_000.0, now)

        assert submitted is False, "a duplicate at the venue must not count as a new order"
        assert "duplicate" in note
        posts = transport.calls_to("POST", "/v2/orders")
        assert len(posts) == 2, "the setup is wrong if the retry did not happen"
        assert posts[0].json_body["client_order_id"] == posts[1].json_body["client_order_id"], (
            "the retry carried a different client_order_id, so the venue could not refuse "
            "it and the rate limited order would have become two orders."
        )
        assert ledger.order(req.client_order_id)["status"] == "unknown", (
            "the order has to be left in an unknown state for reconcile, not written off"
        )
        assert len(ledger.orders()) == 1, "one decision produced more than one order row"
    finally:
        ledger.close()


# ===========================================================================
# 7. The ghost book
# ===========================================================================


def test_the_ghost_book_can_never_reach_the_real_venue(config, tmp_path):
    """In active mode the ghost buys what the filter vetoed. That must stay make believe."""
    cfg = with_filter_mode(config, "active")
    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(cfg, broker, ledger, filt=VetoEverythingFilter(mode="active"))
        result = engine.rebalance(bars, now)

        assert result.vetoed, "the setup is wrong if nothing was vetoed"
        assert not [r for r in broker.submitted if r.side == "buy"], (
            "a vetoed buy reached the real venue."
        )
        assert engine.ghost.broker is not broker, "the ghost book is holding the real venue"
        assert isinstance(engine.ghost.broker, FakeBroker)
        ghost_positions = {p.symbol for p in engine.ghost.broker.positions()}
        assert ghost_positions, (
            "the ghost book bought nothing, so the counterfactual measures nothing"
        )
    finally:
        ledger.close()


# FAILS against the code as it stands. Defect: `GhostBook.submit` swallows every
# exception into `log.debug`, so a counterfactual trade that did not happen leaves no
# trace anywhere a person looks. The ghost book is the ONLY thing that ever answers
# whether the filter earns its place, and the README promises that answer over months.
# A measurement system that loses rows quietly and says nothing will be read as an
# answer when it is not one. Fix: in GhostBook.submit, on failure record a ledger alert
# of kind "ghost_order_failed" carrying the symbol and the reason, so `fulcrum report`
# can say how much of the counterfactual is missing.
def test_a_ghost_trade_that_fails_is_written_down_and_not_lost(config, tmp_path):
    now = utc(2025, 1, 15, 10, 0)
    bars = rising_book(now)
    prices = last_closes(bars)
    broker = SpyBroker(starting_cash=100_000.0, fee_bps=25.0, slippage_bps=10.0,
                       min_notional_usd=10.0)
    broker.set_prices(prices, ts=now)
    ledger = open_ledger(tmp_path)
    try:
        engine = build_engine(config, broker, ledger)
        engine.ghost.ensure_seeded(100_000.0, [], prices)
        engine.ghost.broker.fail_next = "reject"

        engine.rebalance(bars, now)

        alerts = ledger.alerts()
        assert any("ghost" in row["kind"] for row in alerts), (
            "a ghost book trade failed and nothing anywhere records it, so the only "
            "measurement of whether the filter helps is quietly wrong."
        )
    finally:
        ledger.close()
