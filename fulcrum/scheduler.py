"""The clock: the plain loop that decides WHEN the engine runs, and nothing else.

The engine decides what to do. This file only answers three questions:

  Is it time for the weekly rebalance?
  Is it time for the daily practice run of the filter (shadow mode only)?
  Is it time for the next check on stops and the kill switch?

Everything that touches the outside world is handed in, so the same code runs in tests
with a fake clock, fake sleeping, and made-up prices:

  bars_provider(symbols, now) -> {symbol: DataFrame of daily bars}
  price_provider(symbols)     -> {symbol: last price}
  sleep(seconds)              -> returns when that much time has passed
  clock()                     -> the time right now, in UTC

Two habits that keep the loop alive:

* It never spins. After each pass it sleeps until the next scheduled check.
* One bad pass does not end the run. Anything that goes wrong is logged, written to the
  ledger as a "loop_error" alert at most once every 15 minutes, and then the loop carries on.

Pressing Ctrl and C stops it: the step in progress finishes, and the loop returns the
message "Stopped by you. Nothing is running now."

Where the schedule is remembered: the time of the last rebalance and the last daily
practice run are written to the ledger alerts table, so stopping and starting the bot
does not make it rebalance twice or lose its place in the halved (every other week)
schedule the fee alarm can switch on.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

import pandas as pd

from . import features as feat
from . import signal as sig
from . import sizing as sz
from .config import Config, ScheduleConfig
from .models import Candidate, MarketContext, require_utc, utcnow

log = logging.getLogger("fulcrum.scheduler")

STOPPED_MESSAGE = "Stopped by you. Nothing is running now."

# Ledger alert kinds this file owns. They are bookkeeping, not problems, except loop_error.
REBALANCE_ALERT = "scheduler_rebalance"
SHADOW_ALERT = "scheduler_shadow_daily"
LOOP_ERROR_ALERT = "loop_error"

# How often a repeating failure is allowed to write an alert row.
LOOP_ERROR_QUIET_MINUTES = 15

BarsProvider = Callable[[Sequence[str], datetime], "dict[str, pd.DataFrame]"]
PriceProvider = Callable[[Sequence[str]], "dict[str, float]"]


# ---------------------------------------------------------------------------
# Pure scheduling helpers. No clock reads, no I/O: hand them the time, they answer.
# ---------------------------------------------------------------------------


def next_tick(now: datetime, tick_minutes: int) -> datetime:
    """The next time the bot should wake up, always later than ``now``.

    Wake-up times sit on a fixed grid measured from midnight UTC, so with a 15 minute
    setting they land on :00, :15, :30 and :45. If ``now`` is exactly on the grid the
    answer is the following slot, never ``now`` itself, so the loop can never spin.
    """
    require_utc(now, "now")
    if tick_minutes <= 0:
        raise ValueError("tick_minutes must be greater than zero")
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_elapsed = (now - midnight).total_seconds() / 60.0
    steps = math.floor(minutes_elapsed / tick_minutes) + 1
    return midnight + timedelta(minutes=steps * tick_minutes)


def last_scheduled_weekly(now: datetime, weekday: int, hour_utc: int) -> datetime:
    """The most recent weekly appointment at or before ``now`` (weekday 0 is Monday, UTC)."""
    require_utc(now, "now")
    moment = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    moment -= timedelta(days=(moment.weekday() - weekday) % 7)
    if moment > now:
        moment -= timedelta(days=7)
    return moment


def last_scheduled_daily(now: datetime, hour_utc: int) -> datetime:
    """The most recent daily appointment at or before ``now`` (UTC)."""
    require_utc(now, "now")
    moment = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if moment > now:
        moment -= timedelta(days=1)
    return moment


def is_rebalance_due(
    now: datetime, last_rebalance_ts: datetime | None, schedule: ScheduleConfig, halved: bool
) -> bool:
    """Has the weekly rebalance appointment passed without being kept?

    ``halved`` is the fee alarm's brake: when the bot has been paying too much in fees it
    rebalances every other week instead of every week, so one appointment is skipped.
    """
    scheduled = last_scheduled_weekly(now, schedule.rebalance_weekday, schedule.rebalance_hour_utc)
    if last_rebalance_ts is None:
        return True
    require_utc(last_rebalance_ts, "last_rebalance_ts")
    if halved:
        return last_rebalance_ts < scheduled - timedelta(days=7)
    return last_rebalance_ts < scheduled


def is_shadow_daily_due(now: datetime, last_shadow_ts: datetime | None, hour_utc: int) -> bool:
    """Has today's practice run of the filter not happened yet?"""
    scheduled = last_scheduled_daily(now, hour_utc)
    if last_shadow_ts is None:
        return True
    require_utc(last_shadow_ts, "last_shadow_ts")
    return last_shadow_ts < scheduled


# ---------------------------------------------------------------------------
# What the caller gets back
# ---------------------------------------------------------------------------


@dataclass
class OnceResult:
    """One practice cycle: the check of old orders, one rebalance, one stop check."""

    ts: datetime
    reconcile: Any = None
    cycle: Any = None
    tick: Any = None
    lines: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        return "\n".join(self.lines)

    def __str__(self) -> str:
        return self.message


@dataclass
class LoopResult:
    """What the long run did before it was stopped."""

    message: str = STOPPED_MESSAGE
    ticks: int = 0
    rebalances: int = 0
    shadow_runs: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Runs the engine on a schedule. Owns no trading logic of its own."""

    def __init__(
        self,
        cfg: Config,
        engine: Any,
        bars_provider: BarsProvider,
        price_provider: PriceProvider,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.bars_provider = bars_provider
        self.price_provider = price_provider
        self.sleep = sleep
        self.clock = clock
        self._last_loop_error_at: datetime | None = None

    # ---- small lookups ----------------------------------------------------

    def universe_symbols(self) -> list[str]:
        """The symbols the bot may hold, in the same order the engine reads them."""
        u = self.cfg.universe
        return list((u.core + u.candidates)[: u.max_symbols])

    def held_symbols(self) -> list[str]:
        """Symbols the bot is holding, plus any symbol with a stop still parked on it."""
        held: set[str] = set(self.engine.stops or {})
        try:
            held.update(p.symbol for p in self.engine.broker.positions() if p.qty > 0)
        except Exception as e:  # noqa: BLE001  a broker hiccup must not stop the stop check
            log.warning("Could not read positions from the broker (%s).", type(e).__name__)
        return sorted(held)

    def _last_alert_ts(self, kind: str) -> datetime | None:
        rows = self.engine.ledger.alerts(kind=kind)
        if not rows:
            return None
        return datetime.fromisoformat(rows[-1]["ts"])

    def _record_bookkeeping(self, kind: str, now: datetime, detail: str) -> None:
        self.engine.ledger.record_alert(now, kind, {"detail": detail})

    # ---- one practice cycle ----------------------------------------------

    def run_once(self, dry_run: bool = True) -> OnceResult:
        """One full pass by hand: check old orders, rebalance once, check stops once.

        This never changes the weekly schedule, in either mode. Only ``run_forever`` keeps
        the calendar, so running a cycle by hand can never cause the next real weekly
        rebalance to be skipped. ``dry_run`` only changes what the summary says; whether
        real orders can leave the machine is decided by the broker the engine was built
        with and by ``[run].mode`` in the config, never here.
        """
        now = self.clock()
        res = OnceResult(ts=now)
        res.lines.append(
            "Practice cycle. No schedule was changed by this run."
            if dry_run
            else "Single cycle, run by hand."
        )

        res.reconcile = self.engine.reconcile()
        res.lines.append(describe_reconcile(res.reconcile))

        symbols = self.universe_symbols()
        bars = self.bars_provider(symbols, now)
        res.cycle = self.engine.rebalance(bars, now)
        res.lines.extend(describe_cycle(res.cycle))

        prices = self.price_provider(self.held_symbols())
        res.tick = self.engine.tick(prices, now)
        res.lines.extend(describe_tick(res.tick))
        return res

    # ---- the long run -----------------------------------------------------

    def run_forever(self) -> LoopResult:
        """Run until you stop it with Ctrl and C.

        On start it checks the broker for orders left over from last time. Then, on every
        wake-up: rebalance if the weekly appointment has passed, run the daily practice
        check of the filter if it is shadow mode and today's has not happened, and always
        check stops and the kill switch. Then sleep until the next wake-up.
        """
        result = LoopResult()
        try:
            log.info("Checking the broker for anything left over from last time.")
            self.engine.reconcile()
        except KeyboardInterrupt:
            return result
        except Exception as e:  # noqa: BLE001
            result.errors += 1
            self._note_error(self.clock(), "startup check", e)

        while True:
            now = self.clock()
            try:
                self._one_pass(now, result)
            except KeyboardInterrupt:
                return result
            except Exception as e:  # noqa: BLE001  backstop: nothing takes the loop down
                result.errors += 1
                self._note_error(now, "scheduled pass", e)

            wake_at = next_tick(now, self.cfg.schedule.tick_minutes)
            seconds = max(0.0, (wake_at - self.clock()).total_seconds())
            try:
                self.sleep(seconds)
            except KeyboardInterrupt:
                return result

    def _one_pass(self, now: datetime, result: LoopResult) -> None:
        """One wake-up. Each step is guarded on its own so one failure cannot skip the rest."""
        halved = bool(getattr(self.engine, "rebalance_frequency_halved", False))
        self._safe(now, result, "weekly rebalance check", lambda: self._maybe_rebalance(now, result, halved))
        if self.cfg.llm_filter.mode == "shadow":
            self._safe(now, result, "daily filter practice run", lambda: self._maybe_shadow_daily(now, result))
        self._safe(now, result, "stop and kill switch check", lambda: self._do_tick(now, result))

    def _maybe_rebalance(self, now: datetime, result: LoopResult, halved: bool) -> None:
        last = self._last_alert_ts(REBALANCE_ALERT)
        if not is_rebalance_due(now, last, self.cfg.schedule, halved):
            return
        symbols = self.universe_symbols()
        log.info("Weekly rebalance is due. Fetching prices for %d symbols.", len(symbols))
        bars = self.bars_provider(symbols, now)
        self.engine.rebalance(bars, now)
        self._record_bookkeeping(
            REBALANCE_ALERT, now, "every other week" if halved else "weekly"
        )
        result.rebalances += 1

    def _maybe_shadow_daily(self, now: datetime, result: LoopResult) -> None:
        last = self._last_alert_ts(SHADOW_ALERT)
        if not is_shadow_daily_due(now, last, self.cfg.llm_filter.shadow_daily_hour_utc):
            return
        self.run_shadow_daily(now)
        result.shadow_runs += 1

    def _do_tick(self, now: datetime, result: LoopResult) -> None:
        prices = self.price_provider(self.held_symbols())
        self.engine.tick(prices, now)
        result.ticks += 1

    # ---- the daily practice run of the filter (shadow mode only) ----------

    def run_shadow_daily(self, now: datetime) -> int:
        """Ask the filter what it would say about today's buy list, and only write it down.

        Nothing is traded and nothing is sized here. A weekly rebalance gives the filter
        about one chance a week to speak, which is far too little to learn anything from in
        the first week, so in shadow mode it is asked daily and the answers are logged.
        """
        helper = getattr(self.engine, "shadow_daily", None)
        if callable(helper):
            helper(now)
            self._record_bookkeeping(SHADOW_ALERT, now, "filter practice run (engine helper)")
            return 0

        symbols = self.universe_symbols()
        bars = self.bars_provider(symbols, now)
        candidates = self.shadow_candidates(bars)
        if not candidates:
            self._record_bookkeeping(SHADOW_ALERT, now, "filter practice run: nothing looked like a buy")
            return 0

        summary = (
            f"{len(candidates)} of {len(symbols)} coins look like buys today. "
            "This is a practice question only. Nothing is being traded."
        )
        outcome = self.engine.filter.evaluate(candidates, MarketContext(as_of=now, summary=summary))
        answers = {v.symbol: v for v in outcome.verdicts}
        for c in candidates:
            v = answers.get(c.symbol)
            self.engine.ledger.record_shadow_daily(
                now,
                c.symbol,
                c.conviction,
                v.verdict if v is not None else "allow",
                v.reason if v is not None else "no answer from the filter, so the rules would have gone ahead",
                outcome.error,
            )
        self._record_bookkeeping(
            SHADOW_ALERT, now, f"filter practice run on {len(candidates)} candidates"
        )
        return len(candidates)

    def shadow_candidates(self, bars: dict[str, pd.DataFrame]) -> list[Candidate]:
        """Build today's hypothetical buy list the same way a rebalance would."""
        cfg = self.cfg
        universe = [s for s in self.universe_symbols() if s in bars][: cfg.universe.max_symbols]
        rows: dict[str, dict] = {}
        for s in universe:
            df = bars[s]
            if df is None or len(df) < cfg.signal.min_history_days:
                continue
            f = feat.compute_features(df, cfg.signal, cfg.asset_class.trading_days_per_year)
            row = f.iloc[-1].to_dict()
            row["conviction"] = sig.conviction_from_row(row, cfg.signal)
            rows[s] = row
        if not rows:
            return []

        targets = sz.target_weights(
            {s: r["conviction"] for s, r in rows.items()},
            {s: _number(r.get("realized_vol")) for s, r in rows.items()},
            cfg.sizing,
            len(universe),
        )

        equity, positions = self._book_for_shadow()
        out: list[Candidate] = []
        for s, r in rows.items():
            t = targets[s]
            current_weight = (positions[s] / equity) if (equity > 0 and s in positions) else 0.0
            if r["conviction"] <= 0 or t.weight <= current_weight:
                continue
            price = _number(r.get("close"))
            if price <= 0:
                continue
            out.append(
                Candidate(
                    symbol=s,
                    conviction=float(r["conviction"]),
                    price=price,
                    realized_vol=max(0.0, _number(r.get("realized_vol"))),
                    atr=_number(r.get("atr")) if _number(r.get("atr")) > 0 else price * 0.02,
                    target_weight=t.weight,
                    current_weight=current_weight,
                )
            )
        return out

    def _book_for_shadow(self) -> tuple[float, dict[str, float]]:
        try:
            equity = float(self.engine.broker.account().equity)
            positions = {p.symbol: float(p.market_value) for p in self.engine.broker.positions()}
            return equity, positions
        except Exception as e:  # noqa: BLE001  the practice run must not depend on the broker
            log.debug("Could not read the book for the practice run (%s).", type(e).__name__)
            return 0.0, {}

    # ---- error handling ---------------------------------------------------

    def _safe(self, now: datetime, result: LoopResult, label: str, fn: Callable[[], None]) -> bool:
        try:
            fn()
            return True
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            result.errors += 1
            log.exception("The %s step failed. The bot will keep running.", label)
            self._note_error(now, label, e)
            return False

    def _note_error(self, now: datetime, label: str, exc: BaseException) -> None:
        """Write one loop_error alert, at most once every 15 minutes, and tell Dave once."""
        if self._last_loop_error_at is not None:
            if now - self._last_loop_error_at < timedelta(minutes=LOOP_ERROR_QUIET_MINUTES):
                return
        self._last_loop_error_at = now
        detail = f"{label} failed: {type(exc).__name__}"
        try:
            self.engine.ledger.record_alert(now, LOOP_ERROR_ALERT, {"detail": detail, "step": label})
        except Exception as e:  # noqa: BLE001
            log.error("Could not write the problem to the ledger (%s).", type(e).__name__)
        try:
            self.engine.notifier.send(
                f"Fulcrum had a problem during the {label} and will keep running. "
                f"Problem type: {type(exc).__name__}. "
                f"Time {now.strftime('%Y-%m-%d %H:%M UTC')}."
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not send the problem message (%s).", type(e).__name__)


# ---------------------------------------------------------------------------
# Plain sentences for the command line
# ---------------------------------------------------------------------------


def describe_reconcile(rec: Any) -> str:
    if rec is None:
        return "Checked for orders left over from last time."
    updated = len(getattr(rec, "updated", ()) or ())
    unknown = len(getattr(rec, "unknown_broker_orders", ()) or ())
    still_open = len(getattr(rec, "still_open", ()) or ())
    if not (updated or unknown or still_open):
        return "Checked the broker for orders left over from last time. There were none."
    parts = ["Checked the broker for orders left over from last time."]
    if updated:
        parts.append(f"{updated} order record(s) brought up to date.")
    if still_open:
        parts.append(f"{still_open} order(s) are still waiting at the broker.")
    if unknown:
        parts.append(f"{unknown} order(s) at the broker are not in the bot's records and were flagged, not adopted.")
    return " ".join(parts)


def describe_cycle(cycle: Any) -> list[str]:
    if cycle is None:
        return ["No rebalance was run."]
    skipped = getattr(cycle, "skipped_reason", None)
    if skipped:
        return [f"The rebalance did nothing this time. Reason: {skipped}."]
    lines = [f"Rebalance done. {len(getattr(cycle, 'decisions', ()) or ())} coin(s) looked at."]
    submitted = getattr(cycle, "submitted", ()) or ()
    if submitted:
        lines.append(f"{len(submitted)} order(s) sent.")
    else:
        lines.append("No orders were needed. Everything was already close enough to target.")
    vetoed = getattr(cycle, "vetoed", ()) or ()
    if vetoed:
        lines.append("The safety filter blocked these buys: " + ", ".join(vetoed) + ".")
    rejected = getattr(cycle, "rejected", ()) or ()
    if rejected:
        lines.append(f"{len(rejected)} order(s) were held back by the risk limits.")
    return lines


def describe_tick(tick: Any) -> list[str]:
    if tick is None:
        return ["No stop check was run."]
    equity = getattr(tick, "equity", 0.0)
    drawdown = getattr(tick, "drawdown", 0.0)
    lines = [
        f"Account value ${equity:,.2f}. Down {drawdown * 100:.2f} percent from its best level so far."
    ]
    if getattr(tick, "stops_fired", None):
        lines.append("Sold on a stop: " + ", ".join(tick.stops_fired) + ".")
    if getattr(tick, "kill_tripped_now", False):
        lines.append("The kill switch has tripped. No new buys until you reset it by hand.")
    if getattr(tick, "kill_reset_now", False):
        lines.append("Your reset file was found. The kill switch is off again.")
    for a in getattr(tick, "alerts", ()) or ():
        lines.append(f"Warning raised: {a}.")
    return lines


def _number(value: Any) -> float:
    """Turn a possibly missing or not-a-number value into a plain float, 0.0 if unusable."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f
