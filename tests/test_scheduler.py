"""Tests for the scheduler: the part that decides when things happen.

Nothing here waits in real time. The clock is a fake that only moves when the fake sleep
moves it, so a whole week of running is simulated in a fraction of a second.

Two kinds of test:
  * the pure timing helpers, checked against times worked out by hand;
  * the loop itself, driven against a stand-in engine that records what it was asked to do,
    because the scheduler's job is the calendar, not the trading.

There is also one pass through a real Engine and a real FakeBroker, to prove the wiring
holds end to end.
"""

from __future__ import annotations

import tomllib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from fulcrum.broker import FakeBroker
from fulcrum.config import Config, ScheduleConfig, example_path
from fulcrum.engine import Engine
from fulcrum.ledger import Ledger
from fulcrum.llm_filter import FakeFilter
from fulcrum.models import OrderRequest
from fulcrum.notify import FakeNotifier
from fulcrum.scheduler import (
    LoopResult,
    REBALANCE_ALERT,
    SHADOW_ALERT,
    STOPPED_MESSAGE,
    Scheduler,
    is_rebalance_due,
    is_shadow_daily_due,
    last_scheduled_daily,
    last_scheduled_weekly,
    next_tick,
)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


EM_DASH = chr(0x2014)  # written this way so the character itself is not in this file
MONDAY = 0
SCHEDULE = ScheduleConfig(rebalance_weekday=MONDAY, rebalance_hour_utc=1, tick_minutes=15)


# ===========================================================================
# Pure helpers
# ===========================================================================


@pytest.mark.parametrize(
    "now,minutes,expected",
    [
        (utc(2025, 1, 6, 0, 0), 15, utc(2025, 1, 6, 0, 15)),      # exactly on the grid: next slot
        (utc(2025, 1, 6, 0, 1), 15, utc(2025, 1, 6, 0, 15)),
        (utc(2025, 1, 6, 0, 14, 59), 15, utc(2025, 1, 6, 0, 15)),
        (utc(2025, 1, 6, 0, 15, 1), 15, utc(2025, 1, 6, 0, 30)),
        (utc(2025, 1, 6, 23, 50), 15, utc(2025, 1, 7, 0, 0)),      # rolls into the next day
        (utc(2025, 1, 6, 23, 59, 59), 15, utc(2025, 1, 7, 0, 0)),
        (utc(2025, 1, 6, 10, 3), 1, utc(2025, 1, 6, 10, 4)),
        (utc(2025, 1, 6, 10, 3), 60, utc(2025, 1, 6, 11, 0)),
        (utc(2025, 1, 6, 0, 0), 7, utc(2025, 1, 6, 0, 7)),         # a size that does not divide the hour
    ],
)
def test_next_tick_is_always_later_than_now(now, minutes, expected):
    assert next_tick(now, minutes) == expected
    assert next_tick(now, minutes) > now


def test_next_tick_rejects_nonsense():
    with pytest.raises(ValueError):
        next_tick(utc(2025, 1, 6), 0)
    with pytest.raises(ValueError):
        next_tick(datetime(2025, 1, 6), 15)  # naive time, no time zone


def test_last_scheduled_weekly_is_worked_out_by_hand():
    # Monday 6 January 2025 at 01:00 UTC is the appointment.
    assert last_scheduled_weekly(utc(2025, 1, 8, 12, 0), MONDAY, 1) == utc(2025, 1, 6, 1, 0)
    assert last_scheduled_weekly(utc(2025, 1, 6, 1, 0), MONDAY, 1) == utc(2025, 1, 6, 1, 0)
    # One minute before it, the most recent one is the week before.
    assert last_scheduled_weekly(utc(2025, 1, 6, 0, 59), MONDAY, 1) == utc(2024, 12, 30, 1, 0)
    # Sunday evening still belongs to the previous Monday.
    assert last_scheduled_weekly(utc(2025, 1, 12, 23, 59), MONDAY, 1) == utc(2025, 1, 6, 1, 0)


def test_last_scheduled_daily_is_worked_out_by_hand():
    assert last_scheduled_daily(utc(2025, 1, 6, 3, 0), 2) == utc(2025, 1, 6, 2, 0)
    assert last_scheduled_daily(utc(2025, 1, 6, 1, 59), 2) == utc(2025, 1, 5, 2, 0)


def test_rebalance_is_due_when_it_has_never_run():
    assert is_rebalance_due(utc(2025, 1, 8, 12, 0), None, SCHEDULE, halved=False) is True
    assert is_rebalance_due(utc(2025, 1, 8, 12, 0), None, SCHEDULE, halved=True) is True


def test_rebalance_due_on_the_weekly_appointment_and_not_before():
    last = utc(2024, 12, 30, 1, 0)  # kept last Monday
    assert is_rebalance_due(utc(2025, 1, 5, 23, 0), last, SCHEDULE, halved=False) is False  # Sunday
    assert is_rebalance_due(utc(2025, 1, 6, 0, 59), last, SCHEDULE, halved=False) is False  # one minute early
    assert is_rebalance_due(utc(2025, 1, 6, 1, 0), last, SCHEDULE, halved=False) is True    # on time
    assert is_rebalance_due(utc(2025, 1, 6, 9, 0), last, SCHEDULE, halved=False) is True    # late is still due


def test_rebalance_not_due_twice_in_the_same_week():
    last = utc(2025, 1, 6, 1, 0)
    assert is_rebalance_due(utc(2025, 1, 6, 1, 15), last, SCHEDULE, halved=False) is False
    assert is_rebalance_due(utc(2025, 1, 9, 12, 0), last, SCHEDULE, halved=False) is False
    assert is_rebalance_due(utc(2025, 1, 13, 1, 0), last, SCHEDULE, halved=False) is True


def test_halved_cadence_skips_every_other_appointment():
    last = utc(2024, 12, 30, 1, 0)
    # The next Monday is skipped when the fee alarm has halved the frequency.
    assert is_rebalance_due(utc(2025, 1, 6, 1, 0), last, SCHEDULE, halved=True) is False
    assert is_rebalance_due(utc(2025, 1, 10, 12, 0), last, SCHEDULE, halved=True) is False
    # The Monday after that is kept.
    assert is_rebalance_due(utc(2025, 1, 13, 1, 0), last, SCHEDULE, halved=True) is True


def test_halving_mid_week_does_not_cause_a_second_rebalance():
    last = utc(2025, 1, 6, 1, 0)  # already rebalanced this week
    assert is_rebalance_due(utc(2025, 1, 6, 4, 0), last, SCHEDULE, halved=True) is False
    assert is_rebalance_due(utc(2025, 1, 13, 1, 0), last, SCHEDULE, halved=True) is False
    assert is_rebalance_due(utc(2025, 1, 20, 1, 0), last, SCHEDULE, halved=True) is True


def test_shadow_daily_due_once_a_day():
    assert is_shadow_daily_due(utc(2025, 1, 6, 2, 0), None, 2) is True
    last = utc(2025, 1, 6, 2, 0)
    assert is_shadow_daily_due(utc(2025, 1, 6, 2, 15), last, 2) is False
    assert is_shadow_daily_due(utc(2025, 1, 6, 23, 0), last, 2) is False
    assert is_shadow_daily_due(utc(2025, 1, 7, 1, 59), last, 2) is False
    assert is_shadow_daily_due(utc(2025, 1, 7, 2, 0), last, 2) is True


# ===========================================================================
# Test doubles for the loop
# ===========================================================================


class FakeClock:
    """A clock that only moves when the fake sleep moves it."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class FakeSleep:
    """Records how long it was asked to wait, moves the clock, and can stop the loop."""

    def __init__(self, clock: FakeClock, stop_at: datetime | None = None, stop_after: int | None = None):
        self.clock = clock
        self.stop_at = stop_at
        self.stop_after = stop_after
        self.durations: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        self.clock.now = self.clock.now + timedelta(seconds=seconds)
        if self.stop_after is not None and len(self.durations) >= self.stop_after:
            raise KeyboardInterrupt
        if self.stop_at is not None and self.clock.now >= self.stop_at:
            raise KeyboardInterrupt


class SpyEngine:
    """Stands in for the Engine. Records what it was asked to do and when."""

    def __init__(self, ledger: Ledger, broker: FakeBroker, notifier: FakeNotifier, filt=None):
        self.ledger = ledger
        self.broker = broker
        self.notifier = notifier
        self.filter = filt
        self.stops: dict[str, float] = {}
        self.rebalance_frequency_halved = False
        self.order: list[str] = []
        self.reconcile_calls = 0
        self.rebalance_times: list[datetime] = []
        self.rebalance_bars: list[dict] = []
        self.tick_times: list[datetime] = []
        self.tick_prices: list[dict] = []
        self.tick_raises: BaseException | None = None

    def reconcile(self):
        self.order.append("reconcile")
        self.reconcile_calls += 1
        return SimpleNamespace(updated=[], unknown_broker_orders=[], still_open=[])

    def rebalance(self, bars, now, headlines=()):
        self.order.append("rebalance")
        self.rebalance_times.append(now)
        self.rebalance_bars.append(bars)
        return SimpleNamespace(skipped_reason=None, decisions=[], submitted=[], rejected=[], vetoed=[])

    def tick(self, prices, now):
        self.order.append("tick")
        self.tick_times.append(now)
        self.tick_prices.append(prices)
        if self.tick_raises is not None:
            raise self.tick_raises
        return SimpleNamespace(
            ts=now, equity=100000.0, drawdown=0.0, stops_fired=[], alerts=[],
            kill_tripped_now=False, kill_reset_now=False,
        )


def build_config(tmp_path, **overrides) -> Config:
    """The example config, pointed at a temporary folder, with small edits for a test."""
    with open(example_path(), "rb") as f:
        raw = tomllib.load(f)
    raw["llm_filter"]["mode"] = "off"
    raw["data"]["historical_source"] = "csv"
    raw["run"]["ledger_path"] = str(tmp_path / "ledger.sqlite")
    raw["run"]["creds_dir"] = str(tmp_path / "creds")
    raw["run"]["kill_reset_file"] = str(tmp_path / "creds" / "agent_kill_reset")
    raw["data"]["cache_dir"] = str(tmp_path / "bars")
    for section, values in overrides.items():
        raw[section].update(values)
    return Config.from_dict(raw, source_path="<test>")


def make_bars(start: datetime, days: int, close_start: float, step: float = 0.0) -> pd.DataFrame:
    """Daily bars. With step 0.0 the price never moves, which the rules read as no signal."""
    rows = []
    price = close_start
    for i in range(days):
        open_ = price
        close = price + step
        rows.append({
            "timestamp": start + timedelta(days=i),
            "open": open_,
            "high": max(open_, close) * 1.001,
            "low": min(open_, close) * 0.999,
            "close": close,
            "volume": 1000.0,
        })
        price = close
    df = pd.DataFrame(rows).set_index("timestamp")
    return df


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    led = Ledger(str(tmp_path / "sched.sqlite"))
    yield led
    led.close()


@pytest.fixture
def spy(ledger) -> SpyEngine:
    broker = FakeBroker(starting_cash=100000.0, fee_bps=25.0, slippage_bps=10.0, min_notional_usd=10.0)
    return SpyEngine(ledger, broker, FakeNotifier())


def bars_for(symbols, now, days: int = 60, step: float = 0.0):
    return {s: make_bars(now - timedelta(days=days - 1), days, 100.0, step) for s in symbols}


# ===========================================================================
# run_once
# ===========================================================================


def test_run_once_does_exactly_one_of_each_step(tmp_path, spy):
    cfg = build_config(tmp_path)
    now = utc(2025, 1, 6, 1, 0)
    seen_symbols: list = []
    seen_price_symbols: list = []

    def bars_provider(symbols, when):
        seen_symbols.append(list(symbols))
        assert when == now
        return bars_for(symbols, when)

    def price_provider(symbols):
        seen_price_symbols.append(list(symbols))
        return {s: 100.0 for s in symbols}

    s = Scheduler(cfg, spy, bars_provider, price_provider, sleep=lambda _: None, clock=FakeClock(now))
    result = s.run_once(dry_run=True)

    assert spy.order == ["reconcile", "rebalance", "tick"]
    assert seen_symbols == [s.universe_symbols()]
    assert seen_price_symbols == [[]]  # nothing held yet, so no prices are needed
    assert result.cycle is not None and result.tick is not None
    assert "Practice cycle" in result.message
    assert EM_DASH not in result.message  # plain English, no em dashes


def test_a_cycle_run_by_hand_never_uses_up_the_weekly_rebalance(tmp_path, spy):
    """Only the long run keeps the calendar, so a hand run cannot make Monday go missing."""
    cfg = build_config(tmp_path)
    now = utc(2025, 1, 6, 1, 0)
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=lambda _: None, clock=FakeClock(now))
    s.run_once(dry_run=True)
    s.run_once(dry_run=False)
    assert spy.ledger.alerts(kind=REBALANCE_ALERT) == []

    # The loop still treats the weekly rebalance as owed.
    assert is_rebalance_due(now, None, cfg.schedule, halved=False) is True


def test_run_once_asks_for_prices_of_what_is_held(tmp_path, spy):
    cfg = build_config(tmp_path)
    now = utc(2025, 1, 6, 1, 0)
    spy.broker.set_price("BTC/USD", 45000.0, ts=now)
    spy.broker.submit(OrderRequest(client_order_id="seed-1", decision_id="d1", symbol="BTC/USD",
                                   side="buy", purpose="entry", notional_usd=1000.0))
    asked: list = []
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when),
                  lambda syms: asked.append(list(syms)) or {s_: 45000.0 for s_ in syms},
                  sleep=lambda _: None, clock=FakeClock(now))
    s.run_once()
    assert asked == [["BTC/USD"]]


def test_run_once_against_a_real_engine(tmp_path, config):
    """One real pass: real Engine, real ledger, deterministic FakeBroker.

    The bars deliberately do not move, so the rules want nothing and no position is opened.
    That is not laziness: fulcrum/engine.py line 616 hands a dict to
    ledger.snapshot_positions(), which expects a list, so any cycle that ends holding
    something currently raises AttributeError. Reported, not worked around here.
    """
    led = Ledger(str(tmp_path / "real.sqlite"))
    led.start_run("run-1", "dry_run", config.config_hash, None, utc(2025, 1, 6, 1, 0))
    broker = FakeBroker(starting_cash=50000.0, fee_bps=25.0, slippage_bps=10.0, min_notional_usd=10.0)
    now = utc(2025, 1, 6, 1, 0)
    engine = Engine(cfg=config, broker=broker, ledger=led, filt=FakeFilter(mode="off"),
                    notifier=FakeNotifier(), run_id="run-1", scope="scope-1")

    def bars_provider(symbols, when):
        return {s: make_bars(when - timedelta(days=59), 60, 100.0, step=0.0) for s in symbols}

    broker.set_prices({s: 100.0 for s in config.universe.core}, ts=now)
    s = Scheduler(config, engine, bars_provider, lambda syms: {s_: 100.0 for s_ in syms},
                  sleep=lambda _: None, clock=FakeClock(now))
    result = s.run_once(dry_run=True)

    assert result.cycle.skipped_reason is None
    assert result.cycle.submitted == []
    assert led.decisions() != []           # the cycle really ran and wrote its reasoning down
    assert led.equity_curve() != []        # the stop check really ran and marked the account
    assert EM_DASH not in result.message
    led.close()


# ===========================================================================
# run_forever
# ===========================================================================


def test_one_simulated_week_rebalances_once_and_checks_every_fifteen_minutes(tmp_path, spy):
    cfg = build_config(tmp_path)
    start = utc(2025, 1, 5, 0, 0)          # Sunday
    stop_at = start + timedelta(days=7)
    # Pretend last Monday's rebalance was kept, so only the coming Monday is due.
    spy.ledger.record_alert(utc(2024, 12, 30, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})

    clock = FakeClock(start)
    sleeper = FakeSleep(clock, stop_at=stop_at)
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=sleeper, clock=clock)
    result = s.run_forever()

    assert spy.reconcile_calls == 1
    assert result.rebalances == 1
    assert spy.rebalance_times == [utc(2025, 1, 6, 1, 0)]     # Monday 01:00 UTC, on the dot
    assert result.ticks == 7 * 24 * 4                          # every 15 minutes for a week
    assert result.errors == 0
    assert set(sleeper.durations) == {900.0}                   # never spins, never oversleeps
    assert str(result) == STOPPED_MESSAGE


def test_halved_cadence_skips_a_week_across_a_restart(tmp_path, spy):
    cfg = build_config(tmp_path)
    spy.rebalance_frequency_halved = True
    spy.ledger.record_alert(utc(2024, 12, 30, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})

    start = utc(2025, 1, 5, 0, 0)
    clock = FakeClock(start)
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=FakeSleep(clock, stop_at=start + timedelta(days=7)), clock=clock)
    first_week = s.run_forever()
    assert first_week.rebalances == 0          # Monday 6 January is the skipped one

    # Stop and start again. The ledger remembers where the schedule got to.
    clock2 = FakeClock(utc(2025, 1, 12, 0, 0))
    s2 = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                   sleep=FakeSleep(clock2, stop_at=utc(2025, 1, 19, 0, 0)), clock=clock2)
    second_week = s2.run_forever()
    assert second_week.rebalances == 1
    assert spy.rebalance_times == [utc(2025, 1, 13, 1, 0)]


def test_a_broken_step_does_not_stop_the_loop_and_alerts_once(tmp_path, spy):
    cfg = build_config(tmp_path, schedule={"tick_minutes": 5})
    spy.ledger.record_alert(utc(2025, 1, 3, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})
    start = utc(2025, 1, 5, 0, 0)  # Sunday, so no rebalance is due during this test

    failures = {"left": 2}

    def price_provider(symbols):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise RuntimeError("the price feed fell over")
        return {}

    clock = FakeClock(start)
    sleeper = FakeSleep(clock, stop_after=3)
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), price_provider,
                  sleep=sleeper, clock=clock)
    result = s.run_forever()

    assert len(sleeper.durations) == 3          # it kept going after both failures
    assert result.errors == 2
    assert result.ticks == 1                    # the third pass worked
    # Two failures ten minutes apart, one alert row: the quiet period is fifteen minutes.
    assert len(spy.ledger.alerts(kind="loop_error")) == 1
    assert spy.notifier.sent  # Dave was told once
    assert str(result) == STOPPED_MESSAGE


def test_a_failed_rebalance_does_not_skip_the_stop_check(tmp_path, spy):
    cfg = build_config(tmp_path)
    start = utc(2025, 1, 6, 1, 0)   # Monday, rebalance due, and it will fail

    def bars_provider(symbols, when):
        raise RuntimeError("no bars today")

    clock = FakeClock(start)
    s = Scheduler(cfg, spy, bars_provider, lambda syms: {}, sleep=FakeSleep(clock, stop_after=1), clock=clock)
    result = s.run_forever()

    assert result.rebalances == 0
    assert result.errors == 1
    assert result.ticks == 1        # stops are still checked when the rebalance fails


def test_ctrl_c_during_sleep_stops_cleanly(tmp_path, spy):
    cfg = build_config(tmp_path)
    start = utc(2025, 1, 5, 0, 0)
    spy.ledger.record_alert(utc(2025, 1, 3, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})

    clock = FakeClock(start)
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=FakeSleep(clock, stop_after=1), clock=clock)
    result = s.run_forever()

    assert isinstance(result, LoopResult)
    assert result.message == "Stopped by you. Nothing is running now."
    assert result.ticks == 1        # the pass in progress was finished first
    assert spy.reconcile_calls == 1


def test_ctrl_c_during_a_step_stops_cleanly(tmp_path, spy):
    cfg = build_config(tmp_path)
    spy.ledger.record_alert(utc(2025, 1, 3, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})
    spy.tick_raises = KeyboardInterrupt()
    clock = FakeClock(utc(2025, 1, 5, 0, 0))
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=FakeSleep(clock, stop_after=5), clock=clock)
    result = s.run_forever()

    assert str(result) == STOPPED_MESSAGE
    assert result.errors == 0       # stopping on purpose is not an error
    assert len(spy.ledger.alerts(kind="loop_error")) == 0


# ===========================================================================
# Shadow mode: the daily practice run of the filter
# ===========================================================================


def rising_bars(start: datetime, days: int, first: float) -> pd.DataFrame:
    """Bars that rise unevenly, so the rules see a trend and a real volatility number."""
    rows = []
    price = first
    for i in range(days):
        open_ = price
        close = price * (1.004 if i % 3 else 1.010)
        rows.append({
            "timestamp": start + timedelta(days=i),
            "open": open_,
            "high": max(open_, close) * 1.01,
            "low": min(open_, close) * 0.99,
            "close": close,
            "volume": 1000.0,
        })
        price = close
    return pd.DataFrame(rows).set_index("timestamp")


def test_shadow_daily_asks_the_filter_and_writes_the_answers_down(tmp_path, ledger):
    cfg = build_config(tmp_path, llm_filter={"mode": "shadow", "shadow_daily_hour_utc": 2})
    broker = FakeBroker(starting_cash=100000.0, fee_bps=25.0, slippage_bps=10.0, min_notional_usd=10.0)
    filt = FakeFilter(mode="shadow", script={"BTC/USD": "veto"})
    spy = SpyEngine(ledger, broker, FakeNotifier(), filt=filt)
    now = utc(2025, 1, 5, 2, 0)

    def bars_provider(symbols, when):
        return {s: rising_bars(when - timedelta(days=59), 60, 100.0) for s in symbols}

    s = Scheduler(cfg, spy, bars_provider, lambda syms: {}, sleep=lambda _: None, clock=FakeClock(now))
    count = s.run_shadow_daily(now)

    assert count > 0
    assert filt.calls, "the filter should have been asked"
    asked_symbols = filt.calls[0][0]
    assert "BTC/USD" in asked_symbols
    rows = ledger._conn.execute("SELECT symbol, verdict FROM shadow_daily ORDER BY symbol").fetchall()
    assert len(rows) == count
    assert dict((r["symbol"], r["verdict"]) for r in rows)["BTC/USD"] == "veto"
    assert len(ledger.alerts(kind=SHADOW_ALERT)) == 1


def test_shadow_daily_runs_once_a_day_inside_the_loop(tmp_path, ledger):
    cfg = build_config(tmp_path, llm_filter={"mode": "shadow", "shadow_daily_hour_utc": 2})
    broker = FakeBroker(starting_cash=100000.0, fee_bps=25.0, slippage_bps=10.0, min_notional_usd=10.0)
    spy = SpyEngine(ledger, broker, FakeNotifier(), filt=FakeFilter(mode="shadow"))
    ledger.record_alert(utc(2025, 1, 3, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})
    # Yesterday's practice run already happened, so today's is the next one owed.
    ledger.record_alert(utc(2025, 1, 3, 2, 0), SHADOW_ALERT, {"detail": "filter practice run"})

    start = utc(2025, 1, 4, 0, 0)  # Saturday: no rebalance due in this window
    clock = FakeClock(start)

    def bars_provider(symbols, when):
        return {s: rising_bars(when - timedelta(days=59), 60, 100.0) for s in symbols}

    s = Scheduler(cfg, spy, bars_provider, lambda syms: {},
                  sleep=FakeSleep(clock, stop_at=start + timedelta(days=2)), clock=clock)
    result = s.run_forever()

    assert result.shadow_runs == 2          # Saturday 02:00 and Sunday 02:00, once each
    assert result.rebalances == 0
    assert len(ledger.alerts(kind=SHADOW_ALERT)) == 3  # the seeded one plus the two new runs


def test_shadow_daily_does_not_run_when_the_filter_is_off(tmp_path, spy):
    cfg = build_config(tmp_path)  # mode off
    ledger_alerts_before = len(spy.ledger.alerts(kind=SHADOW_ALERT))
    spy.ledger.record_alert(utc(2025, 1, 3, 1, 0), REBALANCE_ALERT, {"detail": "weekly"})
    clock = FakeClock(utc(2025, 1, 4, 3, 0))
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=FakeSleep(clock, stop_after=2), clock=clock)
    result = s.run_forever()

    assert result.shadow_runs == 0
    assert len(spy.ledger.alerts(kind=SHADOW_ALERT)) == ledger_alerts_before


def test_shadow_daily_uses_an_engine_helper_when_one_exists(tmp_path, spy):
    cfg = build_config(tmp_path, llm_filter={"mode": "shadow", "shadow_daily_hour_utc": 2})
    called: list[datetime] = []
    spy.shadow_daily = lambda when: called.append(when)
    now = utc(2025, 1, 5, 2, 30)
    s = Scheduler(cfg, spy, lambda syms, when: bars_for(syms, when), lambda syms: {},
                  sleep=lambda _: None, clock=FakeClock(now))
    s.run_shadow_daily(now)

    assert called == [now]
    assert len(spy.ledger.alerts(kind=SHADOW_ALERT)) == 1
