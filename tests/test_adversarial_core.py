"""Attacks on the safety layer: features, signal, sizing, risk.

Everything in this file is written from the outside, as an attacker. Each test
tries to make the bot do the one thing the spec says it must never do:

  1. buy while the kill switch is tripped or the daily trade cap is used up,
  2. fail to sell when a sale would reduce risk,
  3. end up holding more of something than the caps allow,
  4. let a number from the future leak into a decision made in the past,
  5. survive odd numbers (zero, missing, negative, not a number) without
     quietly doing something dangerous,
  6. touch the kill switch reset file that only Dave is allowed to create.

A test that fails here is a real hole, not a style complaint. The docstring on
each one says, in plain words, what would go wrong with real money.

These tests never touch the network, a credential, or the modules they test.
"""

from __future__ import annotations

import inspect
import math
import random
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import fulcrum.risk as risk_module
from fulcrum.config import AssetClassConfig, RiskConfig, SignalConfig, SizingConfig
from fulcrum.features import compute_features
from fulcrum.ledger import Ledger
from fulcrum.models import OrderRequest, PortfolioState, Position
from fulcrum.risk import KillSwitch, check_order, make_client_order_id
from fulcrum.sizing import initial_stop, ratchet_stop, target_weights
from tests.conftest import utc

NAN = float("nan")
INF = float("inf")

RISK = RiskConfig(
    max_order_pct_equity=0.05,
    max_trades_per_24h=10,
    stop_atr_multiple=2.5,
    kill_switch_drawdown=0.15,
    fee_budget_annual_pct_equity=0.03,
    slippage_alarm_multiple=2.0,
    stale_bar_max_age_minutes=180,
)

SIZING = SizingConfig(
    portfolio_vol_target=0.20,
    max_weight_per_asset=0.15,
    max_gross_exposure=0.60,
    no_trade_band_pct_equity=0.02,
)

ASSET = AssetClassConfig(
    name="crypto",
    trading_days_per_year=365,
    calendar="24x7",
    taker_fee_bps=25.0,
    maker_fee_bps=15.0,
    slippage_bps=10.0,
    min_notional_usd=10.0,
    quote_currency="USD",
)

SYMBOL = "BTC/USD"


def plain_state(**overrides) -> PortfolioState:
    """A healthy portfolio, unless a test changes one number on purpose."""
    base = dict(
        as_of=utc(2025, 1, 1),
        equity=100000.0,
        cash=50000.0,
        positions={},
        trades_last_24h=0,
        high_water_mark=100000.0,
        prices={SYMBOL: 50000.0, "ETH/USD": 3000.0},
    )
    base.update(overrides)
    return PortfolioState(**base)


def buy(notional: float | None = None, qty: float | None = None, purpose: str = "entry") -> OrderRequest:
    return OrderRequest(
        client_order_id="attack-buy",
        decision_id="attack-decision",
        symbol=SYMBOL,
        side="buy",
        purpose=purpose,
        notional_usd=notional,
        qty=qty,
    )


def sell(notional: float | None = None, qty: float | None = None, purpose: str = "exit") -> OrderRequest:
    return OrderRequest(
        client_order_id="attack-sell",
        decision_id="attack-decision",
        symbol=SYMBOL,
        side="sell",
        purpose=purpose,
        notional_usd=notional,
        qty=qty,
    )


def gate(req: OrderRequest, state: PortfolioState, kill_tripped: bool):
    return check_order(req, state, RISK, SIZING, ASSET, kill_tripped=kill_tripped)


def resolved_notional(order: OrderRequest, state: PortfolioState) -> float:
    if order.notional_usd is not None:
        return order.notional_usd
    return order.qty * state.prices[order.symbol]


# ---------------------------------------------------------------------------
# Attack 1: can a buy get through while the bot is supposed to be stopped?
# ---------------------------------------------------------------------------


def test_no_buy_gets_through_a_tripped_kill_switch_or_a_used_up_trade_cap():
    """Every way of asking to buy, in every blocked state, must be turned down.

    If one of these slipped through, the bot would keep buying after the
    drawdown alarm had already fired, which is exactly the failure the kill
    switch exists to prevent.
    """
    cap = RISK.max_trades_per_24h
    blocked_trade_counts = (cap, cap + 1, cap + 500)

    for purpose in ("entry", "increase"):
        for shape in ("notional", "qty"):
            order = buy(notional=500.0, purpose=purpose) if shape == "notional" else buy(qty=0.01, purpose=purpose)

            # Blocked because the kill switch is tripped, at any trade count.
            for trades in (0, cap - 1) + blocked_trade_counts:
                decision = gate(order, plain_state(trades_last_24h=trades), kill_tripped=True)
                assert decision.kind == "reject", (
                    f"{purpose} order as {shape} got through a tripped kill switch"
                )
                assert decision.orders == ()

            # Blocked because the daily trade cap is used up, switch healthy.
            for trades in blocked_trade_counts:
                decision = gate(order, plain_state(trades_last_24h=trades), kill_tripped=False)
                assert decision.kind == "reject", (
                    f"{purpose} order as {shape} got through a used up trade cap"
                )
                assert decision.orders == ()

            # The same order is allowed when nothing is blocking it, so the
            # rejections above are caused by the block and not by the order.
            allowed = gate(order, plain_state(trades_last_24h=cap - 1), kill_tripped=False)
            assert allowed.kind in ("approve", "clamp")


def test_a_buy_is_never_treated_as_risk_reducing():
    """Only selling can reduce risk. A buy must never claim the exit exemption."""
    for purpose in ("entry", "increase"):
        order = buy(notional=500.0, purpose=purpose)
        assert order.risk_reducing is False
        assert order.side == "buy"


# ---------------------------------------------------------------------------
# Attack 2: can a sale that reduces risk be stopped, trimmed, or held up?
# ---------------------------------------------------------------------------


def test_a_sale_that_reduces_risk_is_never_stopped_or_trimmed():
    """Selling out must work in every broken state the account can be in.

    The spec is blunt about this: nothing may ever block an exit. So this walks
    through the worst states we can build, including states where the account
    has no money, no price for the coin, and has already traded far too often.
    """
    hostile_states = [
        ("nothing wrong", plain_state()),
        ("no equity left", plain_state(equity=0.0)),
        ("equity below zero", plain_state(equity=-5000.0)),
        ("cash below zero", plain_state(cash=-25000.0)),
        ("no cash at all", plain_state(cash=0.0)),
        ("way over the trade cap", plain_state(trades_last_24h=10000)),
        ("no price for the coin", plain_state(prices={})),
        ("price of zero", plain_state(prices={SYMBOL: 0.0})),
        ("equity is not a number", plain_state(equity=NAN)),
        ("holding far too much already", plain_state(
            positions={SYMBOL: Position(SYMBOL, 10.0, 50000.0, 500000.0)})),
    ]

    for purpose in ("exit", "stop"):
        for shape in ("notional", "qty"):
            order = sell(notional=250.0, purpose=purpose) if shape == "notional" else sell(qty=0.5, purpose=purpose)
            for label, state in hostile_states:
                for kill_tripped in (True, False):
                    decision = gate(order, state, kill_tripped=kill_tripped)
                    assert decision.kind == "approve", (
                        f"{purpose} order as {shape} was not approved when {label}"
                    )
                    assert decision.orders == (order,), (
                        f"{purpose} order as {shape} was changed when {label}"
                    )
                    assert decision.reason == ""


def test_an_exit_is_not_blocked_by_the_smallest_order_size():
    """A tiny sale is still a sale. The minimum order size must not stop it."""
    order = sell(notional=0.01)
    decision = gate(order, plain_state(), kill_tripped=True)
    assert decision.kind == "approve"
    assert decision.orders == (order,)


def test_an_order_cannot_be_written_with_a_purpose_the_bot_does_not_know():
    """A sale spelled "Exit" instead of "exit" must not be accepted at all.

    Nothing checks that the purpose word is one of the four the bot knows, so a
    near miss is accepted, is not counted as a sale that reduces risk, and is
    then refused by the kill switch. That is a blocked exit, which the spec
    forbids. The safe behaviour is to refuse the order when it is written,
    before it can ever reach the gate.
    """
    try:
        order = OrderRequest(
            client_order_id="typo-exit",
            decision_id="attack-decision",
            symbol=SYMBOL,
            side="sell",
            purpose="Exit",
            qty=1.0,
        )
    except ValueError:
        return  # Good: an unknown purpose word cannot exist.

    decision = gate(order, plain_state(), kill_tripped=True)
    pytest.fail(
        "an order was accepted with the unknown purpose 'Exit', and the gate then "
        f"answered '{decision.kind}' with the reason '{decision.reason}'. "
        "A sale that reduces risk was blocked."
    )


# ---------------------------------------------------------------------------
# Attack 3 and 5: odd numbers going into the gate
# ---------------------------------------------------------------------------


def test_an_equity_that_is_not_a_number_must_not_wave_a_buy_through():
    """If the account value arrives as "not a number", the caps must hold.

    Every cap is worked out by multiplying the account value, so if that value
    is not a number then all four caps are not numbers either, and the smallest
    of them is also not a number. Comparing against it is always false, so the
    order is waved through at full size. A broker hiccup would become an
    unlimited buy.
    """
    state = plain_state(equity=NAN)
    order = buy(notional=1_000_000.0)

    decision = gate(order, state, kill_tripped=False)

    assert decision.kind == "reject", (
        f"a 1,000,000 dollar buy was answered with '{decision.kind}' while the "
        "account value was not a number"
    )


def test_a_position_value_that_is_not_a_number_must_not_switch_off_the_gross_cap():
    """One bad holding must not quietly switch off the total exposure cap.

    Total exposure is added up across every holding. If one holding reports a
    value that is not a number, the total is not a number, the cap worked out
    from it is skipped when the smallest cap is chosen, and the bot buys with
    the total exposure cap no longer enforced.
    """
    state = plain_state(
        equity=100000.0,
        cash=100000.0,
        positions={
            "ETH/USD": Position("ETH/USD", 1.0, 3000.0, NAN),  # the bad holding
            SYMBOL: Position(SYMBOL, 1.0, 50000.0, 5000.0),  # small, so its own cap is not the issue
        },
    )
    assert math.isnan(state.gross_exposure)
    assert state.weight(SYMBOL) < SIZING.max_weight_per_asset

    decision = gate(buy(notional=5000.0), state, kill_tripped=False)

    assert decision.kind == "reject", (
        f"the gate answered '{decision.kind}' although it could not work out how "
        "much of the account is already invested"
    )


@pytest.mark.parametrize("price", [0.0, -50000.0, NAN])
def test_a_bad_price_must_not_wave_a_quantity_order_through(price):
    """A bad price turns a large order into an order that looks like nothing.

    When the order says "buy this many coins", the gate multiplies by the price
    it has on file to work out the size in dollars. A price of zero makes any
    order look like a zero dollar order, so no cap can bind and the order is
    approved at full quantity. The bot would then buy 5000 coins at the real
    price. A missing price already raises an error here; a broken price should
    be treated the same way.
    """
    # A price that is missing altogether is already refused, which is the
    # behaviour a broken price should copy.
    with pytest.raises(ValueError):
        gate(buy(qty=5000.0), plain_state(prices={}), kill_tripped=False)

    state = plain_state(prices={SYMBOL: price})
    order = buy(qty=5000.0)

    try:
        decision = gate(order, state, kill_tripped=False)
    except ValueError:
        return  # Good: a price that cannot be used is refused.

    assert decision.kind == "reject", (
        f"a 5000 coin buy was answered with '{decision.kind}' while the price on "
        f"file was {price}"
    )


def test_a_price_too_large_to_use_does_not_survive_as_a_quantity_order():
    """A price of infinity is already handled safely. This pins that behaviour."""
    state = plain_state(prices={SYMBOL: INF})
    decision = gate(buy(qty=5000.0), state, kill_tripped=False)

    assert decision.kind in ("reject", "clamp")
    for order in decision.orders:
        assert order.qty is None
        assert order.notional_usd is not None
        assert order.notional_usd <= RISK.max_order_pct_equity * state.equity + 1e-9


def test_zero_or_negative_equity_stops_every_buy():
    """An account with nothing in it must not be able to buy anything."""
    for equity in (0.0, -1.0, -100000.0):
        state = plain_state(equity=equity, cash=50000.0)
        decision = gate(buy(notional=100.0), state, kill_tripped=False)
        assert decision.kind == "reject", f"a buy was allowed with equity {equity}"


def test_negative_cash_stops_every_buy():
    """An account already overdrawn must not be able to buy more."""
    state = plain_state(cash=-1.0)
    decision = gate(buy(notional=100.0), state, kill_tripped=False)
    assert decision.kind == "reject"


def test_holding_more_than_the_cap_allows_stops_any_further_buying():
    """Once a holding is over its cap, adding to it must be refused."""
    state = plain_state(
        positions={SYMBOL: Position(SYMBOL, 1.0, 50000.0, 50000.0)},  # 50 percent of equity
    )
    assert state.weight(SYMBOL) > SIZING.max_weight_per_asset
    decision = gate(buy(notional=1000.0), state, kill_tripped=False)
    assert decision.kind == "reject"
    assert decision.reason == "per-asset cap"


def test_what_the_bot_ends_up_holding_never_goes_over_a_cap():
    """Random hostile accounts: after any allowed buy, both caps still hold.

    This is the money question. It runs the gate against accounts that are
    already close to, at, or past their caps, with both order shapes, and
    checks what the account would look like after the order filled.
    """
    rng = random.Random(20260921)

    for i in range(4000):
        equity = rng.choice([1.0, 137.5, 10000.0, 100000.0, 5_000_000.0])
        cash = rng.choice([0.0, 1.0, 0.03, 0.5, 1.0, 2.0]) * equity
        price = rng.choice([0.01, 1.0, 137.5, 50000.0, 1_000_000.0])

        positions = {}
        if rng.random() < 0.8:
            positions[SYMBOL] = Position(SYMBOL, 1.0, price, rng.uniform(0.0, 0.30) * equity)
        if rng.random() < 0.6:
            positions["ETH/USD"] = Position("ETH/USD", 1.0, 3000.0, rng.uniform(0.0, 0.60) * equity)

        state = PortfolioState(
            as_of=utc(2025, 1, 1),
            equity=equity,
            cash=cash,
            positions=positions,
            trades_last_24h=rng.randint(0, 9),
            high_water_mark=equity,
            prices={SYMBOL: price, "ETH/USD": 3000.0},
        )

        if rng.random() < 0.5:
            order = buy(notional=rng.uniform(1e-3, 2.0) * equity)
        else:
            order = buy(qty=rng.uniform(1e-6, 2.0) * equity / price)

        decision = gate(order, state, kill_tripped=False)
        if decision.kind == "reject":
            continue

        for filled in decision.orders:
            notional = resolved_notional(filled, state)
            tolerance = 1e-9 * max(1.0, equity)

            assert notional > 0.0, f"case {i}: an order of size {notional} was allowed"
            assert notional <= RISK.max_order_pct_equity * equity + tolerance, (
                f"case {i}: single order {notional} is over the order cap"
            )

            after_symbol = state.weight(SYMBOL) * equity + notional
            after_gross = state.gross_exposure * equity + notional

            assert after_symbol <= SIZING.max_weight_per_asset * equity + tolerance, (
                f"case {i}: holding in one coin would be over its cap"
            )
            assert after_gross <= SIZING.max_gross_exposure * equity + tolerance, (
                f"case {i}: total invested would be over the cap"
            )
            assert notional * (1.0 + ASSET.taker_fee_bps / 1e4) <= cash + tolerance, (
                f"case {i}: the order costs more cash than the account has"
            )


def test_a_clamped_order_keeps_its_identity_and_is_still_a_real_order():
    """Trimming an order must not change which decision it belongs to."""
    state = plain_state()
    order = buy(notional=90000.0)
    decision = gate(order, state, kill_tripped=False)

    assert decision.kind == "clamp"
    trimmed = decision.orders[0]
    assert trimmed.client_order_id == order.client_order_id
    assert trimmed.decision_id == order.decision_id
    assert trimmed.symbol == order.symbol
    assert trimmed.side == order.side
    assert trimmed.purpose == order.purpose
    assert trimmed.notional_usd is not None and trimmed.notional_usd > 0.0
    assert trimmed.notional_usd < order.notional_usd
    assert decision.reason != ""


# ---------------------------------------------------------------------------
# Attack 4: does any number from the future leak backwards?
# ---------------------------------------------------------------------------


def hostile_bars(n: int = 60, seed: int = 11) -> pd.DataFrame:
    """Daily bars with quiet stretches, jumps, and flat days."""
    rng = random.Random(seed)
    index = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    close = 100.0
    rows = []
    for i in range(n):
        if i % 17 == 0:
            move = rng.uniform(-0.25, 0.25)  # shock day
        elif i % 7 == 0:
            move = 0.0  # flat day, so the day to day move is exactly zero
        else:
            move = rng.uniform(-0.03, 0.03)
        close = max(1.0, close * (1.0 + move))
        open_ = close * (1.0 + rng.uniform(-0.01, 0.01))
        high = max(open_, close) * (1.0 + rng.uniform(0.0, 0.02))
        low = min(open_, close) * (1.0 - rng.uniform(0.0, 0.02))
        rows.append((open_, high, low, close, rng.uniform(0.0, 1000.0)))
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close", "volume"])


SIGNAL_CFG = SignalConfig(
    lookbacks_days=(7, 14),
    trend_filter_ma_days=10,
    vol_window_days=5,
    atr_window_days=4,
)


def assert_same_value(a, b, where: str) -> None:
    if pd.isna(a) and pd.isna(b):
        return
    assert not pd.isna(a) and not pd.isna(b), where
    assert abs(a - b) <= 1e-12 * max(1.0, abs(a)), f"{where}: {a} != {b}"


def test_no_feature_can_see_a_day_that_has_not_happened_yet():
    """Cutting the history short must not change any earlier answer.

    This is the strongest version of the no lookahead check: for every day in
    the history, work the features out again using only the days up to and
    including that day, and compare every single column. If any number needed
    a later day, the two answers would differ.
    """
    bars = hostile_bars()
    full = compute_features(bars, SIGNAL_CFG, 365)

    for t in range(len(bars)):
        truncated = compute_features(bars.iloc[: t + 1].copy(), SIGNAL_CFG, 365)
        assert list(truncated.columns) == list(full.columns)
        for column in full.columns:
            assert_same_value(
                full[column].iloc[t],
                truncated[column].iloc[-1],
                f"row {t}, column {column}",
            )


def test_changing_a_later_day_leaves_every_earlier_number_alone():
    """Rewriting the last day must not move any number before it."""
    bars = hostile_bars()
    before = compute_features(bars.copy(), SIGNAL_CFG, 365)

    for target in (len(bars) - 1, len(bars) - 2, len(bars) // 2):
        changed = bars.copy()
        row = changed.index[target]
        changed.loc[row, "close"] = 9999.0
        changed.loc[row, "high"] = 10500.0
        changed.loc[row, "low"] = 0.5
        changed.loc[row, "open"] = 5000.0

        after = compute_features(changed, SIGNAL_CFG, 365)

        for column in before.columns:
            for t in range(target):
                assert_same_value(
                    before[column].iloc[t],
                    after[column].iloc[t],
                    f"changed day {target}, row {t}, column {column}",
                )


def test_a_missing_price_in_the_middle_does_not_reach_back_in_time():
    """A gap in the data must only affect the days at and after the gap."""
    bars = hostile_bars()
    before = compute_features(bars.copy(), SIGNAL_CFG, 365)

    gap = 30
    holed = bars.copy()
    holed.loc[holed.index[gap], "close"] = NAN

    after = compute_features(holed, SIGNAL_CFG, 365)

    for column in before.columns:
        for t in range(gap):
            assert_same_value(
                before[column].iloc[t], after[column].iloc[t], f"row {t}, column {column}"
            )


def test_the_warm_up_period_is_blank_and_not_guessed():
    """Early days must say "not enough history" rather than invent a number."""
    bars = hostile_bars()
    features = compute_features(bars, SIGNAL_CFG, 365)

    assert features["sma_10"].iloc[:9].isna().all()
    assert features["sma_10"].iloc[9:].notna().all()
    assert features["ret_14"].iloc[:14].isna().all()
    assert features["realized_vol"].iloc[:5].isna().all()
    assert features["atr"].iloc[:3].isna().all()


# ---------------------------------------------------------------------------
# Attack 5: stops, sizes, and order ids under odd numbers
# ---------------------------------------------------------------------------


def test_a_stop_price_is_always_a_usable_number():
    """A stop worked out from a blank ATR is blank, and a blank stop never fires.

    The bot watches the price and sells when it drops to the stop. Comparing a
    price against a blank value is always false, so the sale never happens and
    the position has no stop at all while looking like it has one. Blank ATR is
    normal during the first days of history, before the ATR window has filled.
    """
    # Show that a blank ATR is a real thing the bot produces, not a made up
    # input: the first days of history have no ATR yet.
    warm_up_atr = compute_features(hostile_bars(), SIGNAL_CFG, 365)["atr"].iloc[0]
    assert pd.isna(warm_up_atr)

    # Resolution (2026-09-21): the fix the reviewer recommended, and the one applied, is to REFUSE
    # rather than return a made-up number. A fallback stop would be an invented parameter; refusing
    # forces the caller to skip the entry. The original assertion (a usable number) contradicted
    # the recommended fix, so it was changed to expect the refusal.
    with pytest.raises(ValueError, match="missing price or ATR"):
        initial_stop(entry_price=50000.0, atr=warm_up_atr, multiple=2.5)


def test_a_stop_price_survives_odd_inputs():
    """Odd but real inputs must still give a price above zero."""
    for entry, atr, multiple in [
        (50000.0, 0.0, 2.5),
        (0.0, 10.0, 2.5),
        (1e-8, 1.0, 2.5),
        (50000.0, 1e12, 2.5),
        (50000.0, 10.0, 0.0),
    ]:
        stop = initial_stop(entry, atr, multiple)
        assert stop > 0.0, f"entry {entry}, atr {atr}, multiple {multiple} gave stop {stop}"


def test_a_stop_that_went_blank_once_can_recover():
    """One blank stop must not disable the stop for the life of the position.

    The stop is only ever allowed to move up. If the stored value is blank, the
    comparison with the new value is false, so the blank value is kept, and it
    is kept again on every later check. The position is then unprotected for
    good.
    """
    recovered = ratchet_stop(previous_stop=NAN, candidate_stop=48000.0)
    assert not math.isnan(recovered), (
        "a blank stop stayed blank, so the position would never be protected again"
    )
    assert recovered == 48000.0


def test_the_stop_only_ever_moves_up():
    """The safe parts of the ratchet: first call, higher, lower, and blank input."""
    assert ratchet_stop(None, 100.0) == 100.0
    assert ratchet_stop(100.0, 120.0) == 120.0
    assert ratchet_stop(120.0, 100.0) == 120.0
    assert ratchet_stop(120.0, NAN) == 120.0  # a blank new value is ignored


@pytest.mark.parametrize("conviction", [NAN, -0.5])
def test_a_bad_conviction_never_becomes_a_position(conviction):
    """A conviction that is blank or below zero must mean "hold nothing".

    A blank conviction produces a blank target weight, and a conviction below
    zero produces a target below zero, which in this long only book would mean
    betting against the coin. Both slip past the checks that are meant to keep
    every weight between zero and the per coin cap.
    """
    targets = target_weights({SYMBOL: conviction}, {SYMBOL: 0.5}, SIZING, n_universe=3)
    weight = targets[SYMBOL].weight

    assert not math.isnan(weight), "the target size came back as 'not a number'"
    assert 0.0 <= weight <= SIZING.max_weight_per_asset, (
        f"conviction {conviction} produced a target size of {weight}"
    )


def test_sizes_stay_sane_when_volatility_is_odd():
    """Odd volatility numbers must give a size between zero and the cap."""
    odd_vols = {
        "a": NAN,
        "b": 0.0,
        "c": -0.5,
        "d": INF,
        "e": 1e-12,
        "f": 1e12,
    }
    convictions = {symbol: 1.0 for symbol in odd_vols}

    targets = target_weights(convictions, odd_vols, SIZING, n_universe=len(odd_vols))

    total = 0.0
    for symbol, target in targets.items():
        assert 0.0 <= target.weight <= SIZING.max_weight_per_asset + 1e-12, (
            f"{symbol} with vol {odd_vols[symbol]} got weight {target.weight}"
        )
        total += target.weight
    assert total <= SIZING.max_gross_exposure + 1e-12


def test_order_ids_are_repeatable_and_a_sensible_length():
    """Same decision, same id, so a crash and restart cannot buy twice.

    Test corrected: this file still called ``make_client_order_id`` with a ``purpose``
    argument after that parameter was removed from ``fulcrum.risk``. Passing "entry"
    positionally landed in ``seq`` and 0 landed in ``max_len``, so every id came back as
    the empty string and the length check failed. The key is now scope, decision_id,
    symbol, side and seq, because a buy's purpose flips between entry and increase
    depending on whether a position already exists, which would hand the same decision
    two different ids across a crash and let it buy twice. The old "increase" row is
    gone from the list below for that reason; the rest of the assertions are unchanged.
    """
    first = make_client_order_id("scope", "decision-1", SYMBOL, "buy", 0)
    again = make_client_order_id("scope", "decision-1", SYMBOL, "buy", 0)
    assert first == again
    assert 0 < len(first) <= 48
    assert first.isalnum()

    # A buy and an increase for one decision deliberately share an id: that is what
    # stops a restart from submitting the same purchase a second time.
    assert make_client_order_id("scope", "decision-1", SYMBOL, "buy", 0) == first

    different = [
        make_client_order_id("other", "decision-1", SYMBOL, "buy", 0),
        make_client_order_id("scope", "decision-2", SYMBOL, "buy", 0),
        make_client_order_id("scope", "decision-1", "ETH/USD", "buy", 0),
        make_client_order_id("scope", "decision-1", SYMBOL, "sell", 0),
        make_client_order_id("scope", "decision-1", SYMBOL, "buy", 1),
        make_client_order_id("scope", "decision-1", SYMBOL, "buy", -1),
    ]
    assert len(set(different)) == len(different)
    assert first not in different


# ---------------------------------------------------------------------------
# Attack 6: the kill switch reset file that only Dave may create
# ---------------------------------------------------------------------------


def test_the_risk_code_contains_nothing_that_can_write_a_file():
    """Scan the whole risk file, not just the kill switch class.

    Dave resets the kill switch by creating a file by hand. The spec says the
    code that could create, change, or delete that file must not exist. This
    reads the source and looks for anything that writes to disk.
    """
    source = inspect.getsource(risk_module)

    forbidden = [
        "open(",
        ".write(",
        ".write_text(",
        ".write_bytes(",
        ".touch(",
        ".unlink(",
        ".mkdir(",
        ".rmdir(",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "os.makedirs",
        "shutil",
        "tempfile",
    ]
    for token in forbidden:
        assert token not in source, f"fulcrum/risk.py contains a way to write to disk: {token}"


def test_the_kill_switch_never_creates_the_reset_file(tmp_path):
    """Run the kill switch for real and prove the folder stays empty."""
    watched = tmp_path / "watched"
    watched.mkdir()
    reset_file = watched / "agent_kill_reset"

    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        switch = KillSwitch(ledger, RISK, reset_file)

        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        switch.update(100000.0, now)          # first look, records the high point
        assert switch.tripped() is False
        switch.update(50000.0, now)           # big drop, should trip
        assert switch.tripped() is True
        assert switch.consume_reset_file(50000.0, now) is False  # no file to act on
        assert switch.tripped() is True

        assert list(watched.iterdir()) == [], "the bot created something in the reset folder"
        assert not reset_file.exists(), "the bot created the reset file"
    finally:
        ledger.close()


def test_a_tripped_kill_switch_stays_tripped_until_a_file_appears(tmp_path):
    """A partial recovery in the account value must not quietly clear the alarm."""
    reset_file = tmp_path / "agent_kill_reset"
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        switch = KillSwitch(ledger, RISK, reset_file)
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)

        switch.update(100000.0, now)
        switch.update(80000.0, now)
        assert switch.tripped() is True

        for equity in (80001.0, 85000.0, 99999.0, 100000.0):
            switch.update(equity, now)
            assert switch.tripped() is True, (
                f"the alarm cleared itself when the account value reached {equity}"
            )

        # And the gate still refuses to buy while the alarm is on.
        decision = gate(buy(notional=100.0), plain_state(), kill_tripped=switch.tripped())
        assert decision.kind == "reject"
    finally:
        ledger.close()


def test_a_new_account_high_after_the_alarm_fires_does_not_crash_the_bot(tmp_path):
    """The alarm is on and the account recovers past its old best. This crashes.

    When the account value beats its previous best, the kill switch writes the
    new best back to the ledger and passes along the time the alarm fired. That
    time comes back out of the database as text, and the ledger expects a real
    timestamp, so the write blows up. The scheduler calls this on every tick, so
    the bot would fall over instead of watching the stops.
    """
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        switch = KillSwitch(ledger, RISK, tmp_path / "agent_kill_reset")
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)

        switch.update(100000.0, now)
        switch.update(80000.0, now)
        assert switch.tripped() is True

        switch.update(120000.0, now)  # a new best while the alarm is still on

        assert switch.tripped() is True
    finally:
        ledger.close()


def test_the_bot_keeps_working_after_dave_resets_the_alarm_by_hand(tmp_path):
    """The reset works, then the next small gain crashes the bot. Same cause.

    This is the path that matters most: Dave creates the reset file, the bot
    starts again with the current account value as its new best, and then the
    account goes up by one dollar. Writing that new best down crashes, because
    the time of the reset comes back out of the database as text.
    """
    reset_file = tmp_path / "agent_kill_reset"
    ledger = Ledger(tmp_path / "ledger.sqlite")
    try:
        switch = KillSwitch(ledger, RISK, reset_file)
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)

        switch.update(100000.0, now)
        switch.update(80000.0, now)
        assert switch.tripped() is True

        reset_file.write_text("Dave resets this by hand")  # the test plays Dave, not the bot
        assert switch.consume_reset_file(80000.0, now) is True
        assert switch.tripped() is False

        switch.update(80001.0, now)  # one dollar of recovery

        assert switch.tripped() is False
    finally:
        ledger.close()
