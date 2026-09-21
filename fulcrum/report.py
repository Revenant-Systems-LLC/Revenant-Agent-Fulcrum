"""Performance reporting: plain data out of the ledger, plain English out of the data.

This module reads the ledger and nothing else. No network, no broker, no clock reads
that change a number (only the rendering of "now" free text, and there is none of that).
Every function is pure with respect to the ledger contents.

Two layers, deliberately separated:

* the measurement layer (``equity_series``, ``daily_returns``, ``metrics``,
  ``filter_summary``, ``ghost_summary``) returns numbers and plain dataclasses;
* the wording layer (``render_plain``, ``render_status``) turns those into sentences a
  non programmer can read (decision D-013). No tables, no emoji, no em dashes.

Two annualization factors are used, and they are not interchangeable:

* ``365.0 / days`` where ``days`` is the calendar days between the first and last
  equity point. Used by CAGR, turnover and fee drag, because those are all
  "per elapsed year" quantities.
* ``sqrt(trading_days_per_year)`` from the asset class config. Used by volatility
  and Sharpe, because those scale with the square root of the number of return
  periods in a year.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .notify import check_text

CALENDAR_DAYS_PER_YEAR = 365.0
MIN_DAILY_RETURNS_FOR_SHARPE = 10


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _parse(ts: str) -> datetime:
    """Parse an ISO timestamp written by the ledger back into an aware UTC datetime."""
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _empty_series() -> pd.Series:
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"), name="equity")


def _money(x: float) -> str:
    """Money for a reader, not for a spreadsheet. Negative values keep the minus sign."""
    if x < 0:
        return f"-${abs(x):,.2f}"
    return f"${x:,.2f}"


def _pct(x: float, places: int = 2) -> str:
    return f"{x * 100:.{places}f}%"


def _day(ts: datetime) -> str:
    """A date a non programmer reads without effort, for example '3 January 2025'."""
    return f"{ts.day} {ts.strftime('%B %Y')}"


def _stamp(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _plural(n: float, one: str, many: str) -> str:
    """Pick the singular or plural wording. A report a person reads should not say
    '1 times'."""
    return one if n == 1 else many


def _join_symbols(symbols: list[str]) -> str:
    """'BTC/USD and ETH/USD', 'BTC/USD, ETH/USD and SOL/USD', or ''."""
    if not symbols:
        return ""
    if len(symbols) == 1:
        return symbols[0]
    return ", ".join(symbols[:-1]) + " and " + symbols[-1]


# ---------------------------------------------------------------------------
# equity
# ---------------------------------------------------------------------------


def equity_series(ledger) -> pd.Series:
    """Equity from ``ledger.equity_curve()``, one point per row, in row order.

    The index is a timezone aware UTC ``DatetimeIndex``. Rows are not resampled and
    duplicate timestamps are kept as they are, so the caller can resample if it wants
    to. An empty ledger gives an empty float Series with an empty UTC index.
    """
    rows = ledger.equity_curve()
    if not rows:
        return _empty_series()
    idx = pd.DatetimeIndex([_parse(r["ts"]) for r in rows], tz="UTC")
    return pd.Series([float(r["equity"]) for r in rows], index=idx, name="equity")


def daily_last(series: pd.Series) -> pd.Series:
    """The last equity value of each UTC day, indexed by that day at midnight UTC."""
    if series is None or len(series) == 0:
        return _empty_series()
    s = series.sort_index()
    out = s.groupby(s.index.normalize()).last()
    out.name = "equity"
    return out


def daily_returns(series: pd.Series) -> pd.Series:
    """Simple returns of the last equity value of each UTC day.

    Formula: take the final equity point inside each UTC calendar day, then
    ``r_i = e_i / e_(i-1) - 1`` over consecutive daily points. The index is each day
    at midnight UTC, and the first day has no return so it is not in the result.
    N days of equity give N minus 1 returns. Days with no equity row simply do not
    appear, so "consecutive" means consecutive observed days, not consecutive dates.
    """
    daily = daily_last(series)
    if len(daily) < 2:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"), name="daily_return")
    out = (daily / daily.shift(1) - 1.0).iloc[1:]
    out.name = "daily_return"
    return out


# ---------------------------------------------------------------------------
# FIFO round trips
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """One sell fill matched against the oldest open buy lots of the same symbol.

    ``realized_pl_usd`` is ``sum(matched_qty * (sell_price - lot_price))`` over every lot
    the sell consumed. It is a gross number: fees are NOT deducted, exactly as in the
    previous Fulcrum's trade journal. Fees are reported on their own in
    ``Metrics.fee_drag_pct_equity`` so the same dollar is never counted twice.
    ``cost_basis_usd`` is ``sum(matched_qty * lot_price)`` and ``realized_pl_pct`` is
    ``realized_pl_usd / cost_basis_usd``.
    """

    symbol: str
    closed_at: datetime
    qty_matched: float
    realized_pl_usd: float
    cost_basis_usd: float
    realized_pl_pct: float

    @property
    def won(self) -> bool:
        return self.realized_pl_usd > 0.0


@dataclass(frozen=True, slots=True)
class FifoResult:
    round_trips: tuple[RoundTrip, ...]
    unmatched_sell_qty: dict[str, float]


def fifo_round_trips(fills: list) -> FifoResult:
    """FIFO lot matching per symbol across fills, oldest lot first.

    Ported from ``M:\\Projects\\Revenant-Fulcrum-Finder\\tools\\trade_journal.py``
    (read only) and re verified. What the port keeps:

    * each buy fill opens a lot of ``(qty, price)``;
    * each sell fill closes against the oldest open lots of that symbol until its
      quantity is used up;
    * one round trip per sell fill that matched at least some quantity, so a sell that
      spans two lots is one round trip whose profit and loss nets the two;
    * a sell that matches nothing produces no round trip.

    What the port fixes, because the original was silent about it:

    * the original dropped unmatched sell quantity without a word, which hides a
      broken book. Here it is returned in ``unmatched_sell_qty`` per symbol so the
      report can say so out loud;
    * the original compared floats with a bare ``1e-9`` tolerance in one place and
      not the other. Here one constant ``_EPS`` is used for both.

    Fills must already be in time order; ``Ledger.fills()`` returns them that way.
    """
    _EPS = 1e-9
    open_lots: dict[str, list[list[float]]] = {}
    trips: list[RoundTrip] = []
    unmatched: dict[str, float] = {}

    for f in fills:
        symbol = f["symbol"]
        side = f["side"]
        qty = float(f["qty"])
        price = float(f["price"])
        lots = open_lots.setdefault(symbol, [])

        if side == "buy":
            lots.append([qty, price])
            continue

        remaining = qty
        realized = 0.0
        basis = 0.0
        matched = 0.0
        while remaining > _EPS and lots:
            lot = lots[0]
            take = min(lot[0], remaining)
            realized += take * (price - lot[1])
            basis += take * lot[1]
            matched += take
            lot[0] -= take
            remaining -= take
            if lot[0] <= _EPS:
                lots.pop(0)
        if remaining > _EPS:
            unmatched[symbol] = unmatched.get(symbol, 0.0) + remaining
        if basis > 0.0:
            trips.append(RoundTrip(
                symbol=symbol,
                closed_at=_parse(f["filled_at"]),
                qty_matched=matched,
                realized_pl_usd=realized,
                cost_basis_usd=basis,
                realized_pl_pct=realized / basis,
            ))

    return FifoResult(round_trips=tuple(trips), unmatched_sell_qty=unmatched)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metrics:
    """Every performance number the report needs, with its exact formula.

    Let ``E`` be the equity curve in row order, ``E0`` its first value and ``EN`` its
    last. Let ``R`` be ``daily_returns(equity_series(ledger))``, of length ``n``. Let
    ``A = mean(E)`` be average equity, ``T`` the configured ``trading_days_per_year``
    and ``c`` the annual cash rate. Let ``days = (end - start)`` in calendar days as a
    float, and ``y = 365.0 / days`` the elapsed year fraction inverse.

    * ``start``, ``end``: timestamp of the first and last equity row. ``None`` if the
      equity curve is empty.
    * ``days``: ``(end - start).total_seconds() / 86400``. Calendar days, a float, so
      half a day is 0.5. Zero when there is one row or none.
    * ``total_return``: ``EN / E0 - 1``. ``None`` if the curve is empty or ``E0 <= 0``.
    * ``cagr``: ``(EN / E0) ** (365.0 / days) - 1``. ``None`` if ``days <= 0`` or
      ``E0 <= 0``. A short sample makes this a projection, not a forecast.
    * ``annualized_vol``: ``stdev(R, ddof=1) * sqrt(T)``. ``None`` if ``n < 2``.
    * ``sharpe``: ``mean(R - c / T) / stdev(R - c / T, ddof=1) * sqrt(T)``. ``None``
      if ``n < 10`` (see ``MIN_DAILY_RETURNS_FOR_SHARPE``) or if the standard
      deviation is zero. ``sharpe_note`` always states the assumptions, and states
      the reason when the value is ``None``.
    * ``max_drawdown``: ``max over i of (1 - E_i / max(E_0..E_i))``, a positive
      fraction. 0.0 for an empty or never falling curve.
    * ``max_drawdown_end``: timestamp of the trough ``i`` that produced that maximum.
    * ``max_drawdown_start``: timestamp of the LAST point at or before that trough
      whose equity equals the running maximum at the trough. That is the peak the
      fall actually started from, which is not always the first time the curve
      reached that level.
    * ``average_exposure``: mean of the ``gross_exposure`` column of the equity
      curve. ``None`` if the curve is empty.
    * ``turnover``: ``sum(|qty * price|) over all fills / A * y``. Annualized.
      ``None`` if ``A <= 0`` or ``days <= 0``.
    * ``fee_drag_pct_equity``: ``sum(fee_usd) over all fills / A * y``. Annualized.
      ``None`` if ``A <= 0`` or ``days <= 0``.
    * ``fills``: number of fill rows.
    * ``round_trips``: number of sell fills that matched at least some open lot
      quantity (see ``fifo_round_trips``).
    * ``win_rate``: round trips with ``realized_pl_usd > 0`` divided by
      ``round_trips``. ``None`` if there are no round trips.
    * ``average_win``: mean ``realized_pl_usd`` over winning round trips, a positive
      number. ``None`` if there are none.
    * ``average_loss``: mean ``realized_pl_usd`` over losing round trips, a NEGATIVE
      number. ``None`` if there are none. A round trip of exactly zero is neither.
    * ``win_loss_magnitude_ratio``: ``average_win / abs(average_loss)``. ``None`` if
      either side is missing or ``average_loss`` is zero.
    * ``unmatched_sell_qty``: per symbol quantity sold with no open lot to match. Any
      non zero entry means the fill history is incomplete; it is surfaced, never
      swallowed.
    * ``worst_week`` / ``best_week``: the lowest and highest value of
      ``d_i / d_(i-7) - 1`` over the daily series ``d`` from ``daily_returns``'s
      source, that is seven daily observations apart. With one observation per day
      that is seven calendar days. ``None`` if there are fewer than eight daily
      observations.
    * ``alert_counts``: number of rows in ``alerts`` per ``kind``.
    * ``kill_switch_trips``: number of ``kill_switch_events`` rows with
      ``kind = 'trip'``.
    """

    start: datetime | None
    end: datetime | None
    days: float
    total_return: float | None
    cagr: float | None
    annualized_vol: float | None
    sharpe: float | None
    sharpe_note: str
    max_drawdown: float
    max_drawdown_start: datetime | None
    max_drawdown_end: datetime | None
    average_exposure: float | None
    turnover: float | None
    fee_drag_pct_equity: float | None
    fills: int
    round_trips: int
    win_rate: float | None
    average_win: float | None
    average_loss: float | None
    win_loss_magnitude_ratio: float | None
    unmatched_sell_qty: dict[str, float]
    worst_week: float | None
    best_week: float | None
    alert_counts: dict[str, int]
    kill_switch_trips: int
    trading_days_per_year: int
    cash_rate_annual: float
    daily_return_count: int


def _weekly_extremes(daily: pd.Series) -> tuple[float | None, float | None]:
    """Lowest and highest return over seven daily observations. See Metrics docstring."""
    if len(daily) < 8:
        return None, None
    vals = [float(v) for v in daily.to_numpy()]
    rets = [vals[i] / vals[i - 7] - 1.0 for i in range(7, len(vals)) if vals[i - 7] > 0]
    if not rets:
        return None, None
    return min(rets), max(rets)


def _drawdown(rows: list) -> tuple[float, datetime | None, datetime | None]:
    """Max drawdown, its peak timestamp and its trough timestamp. See Metrics docstring."""
    if not rows:
        return 0.0, None, None
    equities = [float(r["equity"]) for r in rows]
    run_max = equities[0]
    worst = 0.0
    trough_i = 0
    peak_level = equities[0]
    for i, e in enumerate(equities):
        if e > run_max:
            run_max = e
        dd = 1.0 - (e / run_max) if run_max > 0 else 0.0
        if dd > worst:
            worst = dd
            trough_i = i
            peak_level = run_max
    if worst <= 0.0:
        return 0.0, None, None
    # The peak the fall started from: the last point at or before the trough that was
    # at the running maximum. With a repeated peak level this is the later one, which
    # is where the decline actually began.
    peak_i = 0
    for i in range(trough_i + 1):
        if equities[i] >= peak_level:
            peak_i = i
    return worst, _parse(rows[peak_i]["ts"]), _parse(rows[trough_i]["ts"])


def metrics(ledger, trading_days_per_year: int, cash_rate_annual: float = 0.0) -> Metrics:
    """Compute every field of ``Metrics`` from the ledger. See the ``Metrics`` docstring
    for the exact formula behind each field."""
    rows = ledger.equity_curve()
    series = equity_series(ledger)
    daily_src = daily_last(series)
    rets = daily_returns(series)
    n = len(rets)

    start = _parse(rows[0]["ts"]) if rows else None
    end = _parse(rows[-1]["ts"]) if rows else None
    days = (end - start).total_seconds() / 86400.0 if (start and end) else 0.0

    first_eq = float(rows[0]["equity"]) if rows else 0.0
    last_eq = float(rows[-1]["equity"]) if rows else 0.0
    total_return = (last_eq / first_eq - 1.0) if (rows and first_eq > 0) else None
    cagr = ((last_eq / first_eq) ** (CALENDAR_DAYS_PER_YEAR / days) - 1.0) if (
        rows and first_eq > 0 and last_eq > 0 and days > 0) else None

    ann = math.sqrt(float(trading_days_per_year))
    vol = float(rets.std(ddof=1)) * ann if n >= 2 else None

    per_period_cash = cash_rate_annual / float(trading_days_per_year)
    sharpe: float | None = None
    if n < MIN_DAILY_RETURNS_FOR_SHARPE:
        sharpe_note = (
            f"Sharpe not reported: it needs at least {MIN_DAILY_RETURNS_FOR_SHARPE} daily returns "
            f"to mean anything and there are only {n}."
        )
    else:
        excess = rets - per_period_cash
        sd = float(excess.std(ddof=1))
        # A curve that grows by the same factor every day has returns that are equal to
        # floating point precision but not bit identical, so the standard deviation comes
        # out around 1e-17 instead of 0 and the ratio explodes to something like 1e15.
        # Printing that to a reader would be worse than printing nothing, so anything
        # smaller than float precision times the scale of the returns counts as no spread.
        scale = max(abs(float(excess.mean())), 1.0)
        if sd <= scale * 1e-12:
            sharpe_note = (
                "Sharpe not reported: the daily returns never varied, so risk per unit of return "
                "cannot be measured."
            )
        else:
            sharpe = float(excess.mean()) / sd * ann
            sharpe_note = (
                f"Sharpe assumes daily returns ({n} of them), annualized by the square root of "
                f"{trading_days_per_year}, with cash earning {_pct(cash_rate_annual)} a year."
            )

    max_dd, dd_start, dd_end = _drawdown(rows)
    avg_exposure = (sum(float(r["gross_exposure"]) for r in rows) / len(rows)) if rows else None
    avg_equity = (sum(float(r["equity"]) for r in rows) / len(rows)) if rows else 0.0

    fills = ledger.fills()
    gross_notional = sum(abs(float(f["qty"]) * float(f["price"])) for f in fills)
    total_fees = sum(float(f["fee_usd"]) for f in fills)
    annualizer = (CALENDAR_DAYS_PER_YEAR / days) if days > 0 else None
    turnover = (gross_notional / avg_equity * annualizer) if (annualizer and avg_equity > 0) else None
    fee_drag = (total_fees / avg_equity * annualizer) if (annualizer and avg_equity > 0) else None

    fifo = fifo_round_trips(fills)
    trips = fifo.round_trips
    wins = [t.realized_pl_usd for t in trips if t.realized_pl_usd > 0]
    losses = [t.realized_pl_usd for t in trips if t.realized_pl_usd < 0]
    win_rate = (len(wins) / len(trips)) if trips else None
    average_win = (sum(wins) / len(wins)) if wins else None
    average_loss = (sum(losses) / len(losses)) if losses else None
    ratio = (average_win / abs(average_loss)) if (average_win is not None and average_loss) else None

    worst_week, best_week = _weekly_extremes(daily_src)

    alert_counts: dict[str, int] = {}
    for a in ledger.alerts():
        alert_counts[a["kind"]] = alert_counts.get(a["kind"], 0) + 1
    trips_count = sum(1 for e in ledger.kill_events() if e["kind"] == "trip")

    return Metrics(
        start=start, end=end, days=days, total_return=total_return, cagr=cagr,
        annualized_vol=vol, sharpe=sharpe, sharpe_note=sharpe_note,
        max_drawdown=max_dd, max_drawdown_start=dd_start, max_drawdown_end=dd_end,
        average_exposure=avg_exposure, turnover=turnover, fee_drag_pct_equity=fee_drag,
        fills=len(fills), round_trips=len(trips), win_rate=win_rate,
        average_win=average_win, average_loss=average_loss, win_loss_magnitude_ratio=ratio,
        unmatched_sell_qty=dict(fifo.unmatched_sell_qty),
        worst_week=worst_week, best_week=best_week,
        alert_counts=dict(sorted(alert_counts.items())), kill_switch_trips=trips_count,
        trading_days_per_year=trading_days_per_year, cash_rate_annual=cash_rate_annual,
        daily_return_count=n,
    )


# ---------------------------------------------------------------------------
# filter summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Veto:
    symbol: str
    cycle_ts: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class FilterSummary:
    """What the AI filter did, straight from ``filter_verdicts`` and ``shadow_daily``.

    A "call" is one distinct ``cycle_ts`` in ``filter_verdicts``: the ledger writes one
    row per symbol per call, and every row of a call shares that call's ``cycle_ts``,
    ``latency_ms`` and ``error``.

    * ``calls``: number of distinct ``cycle_ts`` values.
    * ``failures_by_error_class``: for calls whose rows carry an ``error``, the count of
      calls per error class. The class is the text before the first colon, trimmed, so
      "timeout: 30s elapsed" and "timeout: 45s elapsed" are one class.
    * ``median_latency_ms`` / ``p95_latency_ms``: over one latency per call.
      ``numpy.median`` and ``numpy.percentile(..., 95)``, both with linear
      interpolation. ``None`` when there are no calls.
    * ``verdicts_by_kind``: row counts per verdict, counting only rows with no error.
      A failed call writes fail open "allow" rows; those are failures, not verdicts,
      so they are excluded here and counted in ``failures_by_error_class``.
    * ``applied_rows`` / ``shadow_rows``: rows with ``applied = 1`` and ``applied = 0``.
      Applied means the verdict was allowed to block a real buy.
    * ``vetoes``: every veto row, with its symbol, cycle timestamp and stated reason.
    * ``shadow_daily_counts``: rows of the ``shadow_daily`` table per verdict.
    * ``shadow_daily_errors``: rows of ``shadow_daily`` that carry an error.
    """

    calls: int
    failures_by_error_class: dict[str, int]
    median_latency_ms: float | None
    p95_latency_ms: float | None
    verdicts_by_kind: dict[str, int]
    applied_rows: int
    shadow_rows: int
    vetoes: tuple[Veto, ...]
    shadow_daily_counts: dict[str, int]
    shadow_daily_errors: int

    @property
    def failures(self) -> int:
        return sum(self.failures_by_error_class.values())


def _error_class(err: str) -> str:
    head = err.split(":", 1)[0].strip()
    return head if head else err.strip()


def filter_summary(ledger) -> FilterSummary:
    """Summarize every filter call the ledger holds. See ``FilterSummary`` for the rules."""
    rows = ledger.filter_verdicts()

    calls: dict[str, dict] = {}
    for r in rows:
        call = calls.setdefault(r["cycle_ts"], {"latency": int(r["latency_ms"]), "error": r["error"]})
        if r["error"] and not call["error"]:
            call["error"] = r["error"]

    failures: dict[str, int] = {}
    for call in calls.values():
        if call["error"]:
            cls = _error_class(str(call["error"]))
            failures[cls] = failures.get(cls, 0) + 1

    latencies = [c["latency"] for c in calls.values()]
    median_latency = float(np.median(latencies)) if latencies else None
    p95_latency = float(np.percentile(latencies, 95)) if latencies else None

    verdicts: dict[str, int] = {}
    applied_rows = 0
    shadow_rows = 0
    vetoes: list[Veto] = []
    for r in rows:
        if int(r["applied"]):
            applied_rows += 1
        else:
            shadow_rows += 1
        if r["error"]:
            continue
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
        if r["verdict"] == "veto":
            vetoes.append(Veto(symbol=r["symbol"], cycle_ts=_parse(r["cycle_ts"]), reason=r["reason"]))

    shadow_counts: dict[str, int] = {}
    shadow_errors = 0
    for r in ledger.shadow_daily():
        shadow_counts[r["verdict"]] = shadow_counts.get(r["verdict"], 0) + 1
        if r["error"]:
            shadow_errors += 1

    return FilterSummary(
        calls=len(calls),
        failures_by_error_class=dict(sorted(failures.items())),
        median_latency_ms=median_latency,
        p95_latency_ms=p95_latency,
        verdicts_by_kind=dict(sorted(verdicts.items())),
        applied_rows=applied_rows,
        shadow_rows=shadow_rows,
        vetoes=tuple(vetoes),
        shadow_daily_counts=dict(sorted(shadow_counts.items())),
        shadow_daily_errors=shadow_errors,
    )


# ---------------------------------------------------------------------------
# ghost book
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GhostSummary:
    """The real book next to the counterfactual one.

    * ``real_equity``: the latest ``ghost_book`` row with ``book = 'real'`` if the
      ledger holds one, otherwise the latest ``equity_curve`` equity. ``None`` if
      neither exists.
    * ``counterfactual_equity``: the latest ``ghost_book`` row with
      ``book = 'counterfactual'``. ``None`` if there is none.
    * ``difference_usd``: ``counterfactual_equity - real_equity``.
    * ``difference_pct``: ``difference_usd / real_equity`` as a fraction. ``None`` if
      real equity is zero or missing.
    * ``reading``: one plain sentence. It is worded for shadow mode, where the
      counterfactual book is the one that applied the vetoes. In active mode the
      counterfactual is the book that ignored them, and ``render_plain`` adds a line
      saying so, because ``ghost_summary`` is not told the mode.
    """

    real_equity: float | None
    counterfactual_equity: float | None
    difference_usd: float | None
    difference_pct: float | None
    reading: str


NOT_ENOUGH_HISTORY = "Not enough history yet to say."


def ghost_summary(ledger) -> GhostSummary:
    """Compare the real book with the counterfactual one. See ``GhostSummary``."""
    ghost_rows = ledger.ghost_book("counterfactual")
    real_rows = ledger.ghost_book("real")
    equity_rows = ledger.equity_curve()

    counterfactual = float(ghost_rows[-1]["equity"]) if ghost_rows else None
    if real_rows:
        real = float(real_rows[-1]["equity"])
    elif equity_rows:
        real = float(equity_rows[-1]["equity"])
    else:
        real = None

    if counterfactual is None or real is None:
        return GhostSummary(real, counterfactual, None, None, NOT_ENOUGH_HISTORY)

    diff = counterfactual - real
    pct = (diff / real) if real else None

    if abs(diff) < 0.005:
        reading = "If the filter's vetoes had been applied, the account would be worth the same."
    else:
        direction = "higher" if diff > 0 else "lower"
        pct_text = f" ({_pct(abs(pct), 1)})" if pct is not None else ""
        reading = (
            f"If the filter's vetoes had been applied, the account would be "
            f"{_money(abs(diff))}{pct_text} {direction}."
        )
    return GhostSummary(real, counterfactual, diff, pct, reading)


# ---------------------------------------------------------------------------
# wording
# ---------------------------------------------------------------------------

MODE_WORDS = {
    "off": "the AI filter is switched off",
    "shadow": "the AI filter is in shadow mode (recording only, it blocks nothing)",
    "active": "the AI filter is in active mode (its vetoes really do block buys)",
}


def render_plain(metrics: Metrics, filter_summary: FilterSummary, ghost_summary: GhostSummary,
                 mode: str) -> str:
    """The text the ``report`` command prints. Plain sentences, no tables, no emoji.

    ``mode`` is the filter mode from the config: off, shadow or active. Every number
    that needs one gets a short gloss in brackets.
    """
    m, fs, gs = metrics, filter_summary, ghost_summary
    L: list[str] = []

    # ---- Account
    L.append("Account")
    if m.start is None or m.end is None:
        L.append("Nothing has happened yet. There is no account history to report on.")
    else:
        L.append(f"History runs from {_day(m.start)} to {_day(m.end)}, which is {m.days:.1f} days.")
        if m.total_return is not None:
            L.append(f"Change in account value over that time: {_pct(m.total_return)} "
                     f"(the whole gain or loss, fees included).")
        if m.cagr is not None:
            L.append(f"Same pace stretched to a full year: {_pct(m.cagr)} "
                     f"(a projection from a short sample, not a forecast).")
    L.append("")

    # ---- Risk
    L.append("Risk")
    L.append(f"Worst fall from a peak: {_pct(m.max_drawdown)} "
             f"(the biggest drop from a best value to a later low).")
    if m.max_drawdown_start and m.max_drawdown_end:
        L.append(f"That fall began on {_day(m.max_drawdown_start)} and reached its low on "
                 f"{_day(m.max_drawdown_end)}.")
    if m.annualized_vol is not None:
        L.append(f"Year scale bumpiness: {_pct(m.annualized_vol)} "
                 f"(how much the account value swings about over a year; bigger is a rougher ride).")
    if m.sharpe is not None:
        L.append(f"Return per unit of risk (Sharpe): {m.sharpe:.2f} "
                 f"(1.0 or above is the bar this project set before anyone discusses real money).")
    L.append(m.sharpe_note)
    if m.worst_week is not None and m.best_week is not None:
        L.append(f"Worst seven day stretch: {_pct(m.worst_week)}. "
                 f"Best seven day stretch: {_pct(m.best_week)}.")
    if m.average_exposure is not None:
        L.append(f"Average share of the account held in coins: {_pct(m.average_exposure)} "
                 f"(the rest sat in cash).")
    L.append(f"Times the safety brake tripped: {m.kill_switch_trips}.")
    if m.alert_counts:
        parts = [f"{k} {v}" for k, v in m.alert_counts.items()]
        L.append("Warnings recorded: " + ", ".join(parts) + ".")
    else:
        L.append("Warnings recorded: none.")
    L.append("")

    # ---- Costs
    L.append("Costs")
    if m.fee_drag_pct_equity is not None:
        L.append(f"Trading fees cost {_pct(m.fee_drag_pct_equity)} of the account per year at this "
                 f"pace (this is the fee drag; the alarm level is 3%).")
    else:
        L.append("Trading fees: not enough history to work out a yearly cost yet.")
    if m.turnover is not None:
        L.append(f"Turnover: {_pct(m.turnover)} a year (how much of the account is bought or sold "
                 f"in a year; 100% means the whole account changes hands once).")
    else:
        L.append("Turnover: not enough history to work out a yearly rate yet.")
    L.append("")

    # ---- Trades
    L.append("Trades")
    L.append(f"Orders filled: {m.fills}. Completed round trips: {m.round_trips} "
             f"(a round trip is a buy and the sell that closed it).")
    if m.win_rate is not None:
        L.append(f"Share of round trips that made money: {_pct(m.win_rate, 1)}.")
    if m.average_win is not None and m.average_loss is not None:
        L.append(f"Average winning trip made {_money(m.average_win)} and average losing trip lost "
                 f"{_money(abs(m.average_loss))}.")
    if m.win_loss_magnitude_ratio is not None:
        L.append(f"Winners are {m.win_loss_magnitude_ratio:.2f} times the size of losers "
                 f"(above 1.0 means the wins are bigger than the losses).")
    if m.round_trips < 10:
        L.append("Too few round trips to read anything into the win rate yet; below about ten it "
                 "is noise, not a pattern.")
    if m.unmatched_sell_qty:
        parts = [f"{k} {v:.8f}" for k, v in sorted(m.unmatched_sell_qty.items())]
        L.append("Warning: some sales had no matching purchase on record, so the trade history is "
                 "incomplete: " + ", ".join(parts) + ".")
    L.append("")

    # ---- AI filter
    L.append("AI filter")
    L.append(f"Right now {MODE_WORDS.get(mode, 'the AI filter mode is unknown')}.")
    L.append(f"The filter was asked {fs.calls} {_plural(fs.calls, 'time', 'times')} and failed to "
             f"answer {fs.failures} {_plural(fs.failures, 'time', 'times')} (a failure always means "
             f"the buy went ahead; the filter can only say no, never yes).")
    if fs.failures_by_error_class:
        parts = [f"{k} {v}" for k, v in fs.failures_by_error_class.items()]
        L.append("Reasons it failed: " + ", ".join(parts) + ".")
    if fs.median_latency_ms is not None and fs.p95_latency_ms is not None:
        L.append(f"Typical time to answer: {fs.median_latency_ms:.0f} milliseconds. Slower end: "
                 f"{fs.p95_latency_ms:.0f} milliseconds (only one answer in twenty takes longer).")
    allow_n = fs.verdicts_by_kind.get("allow", 0)
    veto_n = fs.verdicts_by_kind.get("veto", 0)
    L.append(f"Answers given: {allow_n} allow and {veto_n} veto.")
    L.append(f"{fs.applied_rows} of those answers {_plural(fs.applied_rows, 'was', 'were')} allowed "
             f"to block a real buy; {fs.shadow_rows} {_plural(fs.shadow_rows, 'was', 'were')} "
             f"recorded only.")
    if fs.vetoes:
        L.append("Every veto, with the reason the filter gave:")
        for v in fs.vetoes:
            L.append(f"  {v.symbol} on {_stamp(v.cycle_ts)}: {v.reason}")
    else:
        L.append("It has not vetoed anything yet.")
    if fs.shadow_daily_counts:
        parts = [f"{k} {v}" for k, v in fs.shadow_daily_counts.items()]
        L.append("Daily practice runs (logged, never traded): " + ", ".join(parts) +
                 f", with {fs.shadow_daily_errors} "
                 f"{_plural(fs.shadow_daily_errors, 'failure', 'failures')}.")
    else:
        L.append("Daily practice runs: none recorded yet.")
    L.append("")

    # ---- Ghost book
    L.append("Ghost book")
    if gs.counterfactual_equity is None or gs.real_equity is None:
        L.append(NOT_ENOUGH_HISTORY)
    else:
        L.append(f"Real account value {_money(gs.real_equity)}. Shadow account value "
                 f"{_money(gs.counterfactual_equity)}.")
        L.append(gs.reading)
        if mode == "active":
            L.append("Note: in active mode the vetoes are already being applied to the real "
                     "account, so the shadow account is the one that ignored them. Read the "
                     "sentence above the other way round.")
    L.append("")

    # ---- What this means
    L.append("What this means")
    L.extend(_verdict_lines(m, fs, gs, mode))

    return check_text("\n".join(L).rstrip() + "\n")


def _verdict_lines(m: Metrics, fs: FilterSummary, gs: GhostSummary, mode: str) -> list[str]:
    """The closing paragraph: what a reader should take away, and what they should not."""
    out: list[str] = []
    if m.start is None:
        out.append("There is no history yet, so there is nothing to conclude. Run the bot and come "
                   "back to this page later.")
        return out
    if m.days < 90:
        out.append(f"This covers {m.days:.0f} days. The project's own bar is at least 90 days before "
                   f"anyone reads a result as evidence, so treat everything above as a check that "
                   f"the plumbing works, not as proof of skill.")
    else:
        out.append(f"This covers {m.days:.0f} days, which is past the 90 day mark the project set "
                   f"for reading results seriously.")
    if m.sharpe is None:
        out.append("There is not enough daily history to say whether the returns are worth the risk.")
    elif m.sharpe >= 1.0:
        out.append(f"Return per unit of risk is {m.sharpe:.2f}, at or above the 1.0 the project set. "
                   f"That is one of three conditions, not permission to do anything.")
    else:
        out.append(f"Return per unit of risk is {m.sharpe:.2f}, below the 1.0 the project set. On "
                   f"this evidence the strategy has not earned more trust.")
    if m.fee_drag_pct_equity is not None and m.fee_drag_pct_equity > 0.03:
        out.append(f"Fees are eating {_pct(m.fee_drag_pct_equity)} of the account a year, above the 3% "
                   f"alarm level. That is worth looking at before anything else.")
    if m.kill_switch_trips:
        out.append("The safety brake has tripped. Read the warnings above before resetting it.")
    if mode == "shadow":
        out.append("The AI filter is still only watching. Read its vetoes above and decide whether "
                   "the reasons read like a careful analyst or like guesses.")
    out.append("Nothing here involved real money.")
    return out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

NOTHING_YET = "Nothing has happened yet."


def render_status(ledger, cfg) -> str:
    """The text the ``status`` command prints: where the account stands right now.

    Reads the latest ``positions_snapshot``, the latest ``equity_curve`` row,
    ``kill_switch_state``, the newest ``alerts`` and the newest round of ``decisions``.
    A ledger with no equity history, no positions and no decisions renders exactly
    ``Nothing has happened yet.``
    """
    positions = ledger.positions_snapshot()
    equity_rows = ledger.equity_curve()
    decisions = ledger.decisions()

    if not equity_rows and not positions and not decisions:
        return check_text(NOTHING_YET + "\n")

    L: list[str] = []

    if equity_rows:
        last = equity_rows[-1]
        cash = float(last["cash"])
        hwm = float(last["hwm"])
        drawdown = float(last["drawdown"])
    else:
        cash, hwm, drawdown = 0.0, 0.0, 0.0

    held = [p for p in positions if float(p["qty"]) > 0]
    coins_value = sum(float(p["market_value"]) for p in held)
    if held:
        word = "coin" if len(held) == 1 else "coins"
        L.append(f"Holding {len(held)} {word} worth {_money(coins_value)} and {_money(cash)} in cash.")
        names = _join_symbols([p["symbol"] for p in held])
        L.append(f"The coins are {names}.")
    else:
        L.append(f"Holding no coins and {_money(cash)} in cash.")

    if hwm > 0:
        if drawdown <= 0.00005:
            L.append(f"Best value so far {_money(hwm)}; the account is at its best value right now.")
        else:
            L.append(f"Best value so far {_money(hwm)}; today {_pct(drawdown, 1)} below it.")

    kill = ledger.kill_state()
    if bool(kill["tripped"]):
        L.append("Safety brake is ON. No new buys. Stops still work. Only you can reset it, by "
                 "creating the reset file by hand.")
    else:
        L.append("Safety brake is off.")

    mode = cfg.llm_filter.mode
    if mode == "off":
        L.append("AI filter is switched off.")
    elif mode == "shadow":
        L.append("AI filter is in shadow mode (recording only).")
    else:
        L.append("AI filter is in active mode (its vetoes block buys).")

    if decisions:
        last_ts = decisions[-1]["cycle_ts"]
        round_rows = [d for d in decisions if d["cycle_ts"] == last_ts]
        when = _parse(last_ts)
        bought = sorted({d["symbol"] for d in round_rows if d["action"] == "buy"})
        sold = sorted({d["symbol"] for d in round_rows if d["action"] == "sell"})
        what: list[str] = []
        if bought:
            what.append("bought " + _join_symbols(bought))
        if sold:
            what.append("sold " + _join_symbols(sold))
        tail = ", ".join(what) if what else "no trades were needed"
        L.append(f"Last decision round: {when.strftime('%A %H:%M UTC')}, {tail}.")
    else:
        L.append("No decision round has run yet.")

    recent = ledger.alerts()
    if recent:
        newest = recent[-1]
        L.append(f"Most recent warning: {newest['kind']} at {_stamp(_parse(newest['ts']))}.")

    L.append(f"Running in {cfg.run.mode} mode. No real money is involved.")

    return check_text("\n".join(L) + "\n")
