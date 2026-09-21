"""The cycle: the one place every decision, order, and side effect is assembled.

The engine is used unchanged by the backtest and the live scheduler. It never
reads the clock or network itself: the caller passes "now" and a Broker. Public surface:

  Engine.rebalance(bars, now, headlines) -> CycleResult
  Engine.tick(prices, now) -> TickResult
  Engine.reconcile() -> ReconcileResult

Dataclasses carry what happened so the CLI can print plain sentences from them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from . import features as feat
from . import notify as ntf
from . import risk as rk
from . import signal as sig
from . import sizing as sz
from .broker import Broker, BrokerError, DuplicateOrderError, FakeBroker, OrderRejected
from .config import Config
from .ledger import Ledger
from .models import (
    Candidate, Decision, Fill, FilterResult, MarketContext, OrderRequest, Plan,
    PortfolioState, Position, RiskDecision, utcnow, TERMINAL_STATUSES,
)
from .scheduler import last_scheduled_weekly

log = logging.getLogger("fulcrum.engine")


@dataclass
class CycleResult:
    """What happened during a rebalance cycle."""

    cycle_ts: datetime
    skipped_reason: str | None = None  # If non-None, nothing was traded
    decisions: list[Decision] = field(default_factory=list)
    submitted: list[OrderRequest] = field(default_factory=list)
    rejected: list[tuple[OrderRequest, str]] = field(default_factory=list)
    vetoed: list[str] = field(default_factory=list)
    filter_result: FilterResult | None = None
    alerts: list[str] = field(default_factory=list)
    stops_fired: list[str] = field(default_factory=list)
    kill_events: list[str] = field(default_factory=list)


@dataclass
class TickResult:
    """What happened during a tick (stop check, kill check, equity update)."""

    ts: datetime
    equity: float
    drawdown: float
    kill_tripped_now: bool = False
    kill_reset_now: bool = False
    stops_fired: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


@dataclass
class ReconcileResult:
    """Reconciliation of ledger vs broker on startup."""

    updated: list[str] = field(default_factory=list)
    unknown_broker_orders: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)


class GhostBook:
    """The counterfactual portfolio: what would have happened if vetoes had been applied (or not).

    In shadow mode: ghost executes real trades minus vetoed buys.
    In active mode: ghost executes real trades plus vetoed buys.
    In off mode: ghost mirrors real trades exactly.

    A FakeBroker seeded on the first rebalance, then persisted and rehydrated from the ledger.
    The ghost book curve answers the edge question over time: did the filter's vetoes help or hurt?
    """

    def __init__(self, cfg: Config, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger
        self.broker: FakeBroker | None = None
        self.stops: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        """Rehydrate from the newest ghost book row (if any)."""
        rows = self.ledger.ghost_book("counterfactual")
        if not rows:
            return
        last = rows[-1]
        state = json.loads(last["positions_json"])
        self.broker = self._new_broker(float(last["cash"]))
        for sym, p in state.get("positions", {}).items():
            self.broker._positions[sym] = Position(sym, float(p["qty"]), float(p["avg_entry"]), 0.0)
        self.stops = {k: float(v) for k, v in state.get("stops", {}).items()}

    def _new_broker(self, cash: float) -> FakeBroker:
        """Create a new FakeBroker with the asset class fee and slippage model."""
        a = self.cfg.asset_class
        return FakeBroker(
            starting_cash=cash,
            fee_bps=a.taker_fee_bps,
            slippage_bps=a.slippage_bps,
            min_notional_usd=a.min_notional_usd,
        )

    def ensure_seeded(
        self, real_equity: float, positions: list[Position], prices: dict[str, float]
    ) -> None:
        """Seed the ghost book on the first rebalance with the real book's cash and positions."""
        if self.broker is not None:
            return
        real_cash = real_equity - sum(p.market_value for p in positions)
        self.broker = self._new_broker(real_cash)
        for p in positions:
            self.broker._positions[p.symbol] = Position(p.symbol, p.qty, p.avg_entry, p.market_value)
        self.mark(prices)

    def mark(self, prices: dict[str, float], ts: datetime | None = None) -> None:
        """Mark the ghost book to market with new prices."""
        if self.broker is None:
            return
        self.broker.set_prices({s: p for s, p in prices.items()}, ts)

    def equity(self) -> float:
        """Return the ghost book's current equity."""
        return self.broker.account().equity if self.broker else 0.0

    def submit(self, req: OrderRequest, ts: datetime | None = None) -> None:
        """Submit an order to the ghost book (if it exists). Errors are recorded as alerts."""
        if self.broker is None:
            return
        try:
            self.broker.submit(req)
        except Exception as e:
            # Record the failure so the ghost book measurement is not silently wrong
            if ts is not None:
                self.ledger.record_alert(ts, "ghost_order_failed",
                                       {"symbol": req.symbol, "reason": f"{type(e).__name__}: {str(e)[:200]}"})
            log.debug("ghost book order failed: %s", e)

    def execute_stop(self, symbol: str, price: float, qty: float, now: datetime) -> bool:
        """Execute a stop in the ghost book. Returns True if the stop fired."""
        if self.broker is None or symbol not in self.stops:
            return False
        stop = self.stops[symbol]
        if price <= stop:
            try:
                req = OrderRequest(
                    client_order_id=f"ghost-stop-{symbol}-{now.timestamp()}",
                    decision_id="",
                    symbol=symbol,
                    side="sell",
                    purpose="stop",
                    qty=qty,
                )
                self.broker.submit(req)
                del self.stops[symbol]
                return True
            except Exception as e:
                log.debug("ghost stop failed: %s", e)
        return False

    def persist(self, ts: datetime) -> None:
        """Persist the ghost book state to the ledger."""
        if self.broker is None:
            return
        state = {
            "positions": {
                p.symbol: {"qty": p.qty, "avg_entry": p.avg_entry}
                for p in self.broker.positions()
            },
            "stops": self.stops,
        }
        self.ledger.record_ghost(
            ts, "counterfactual", self.broker.account().equity, self.broker.cash, state
        )


class Engine:
    """The cycle orchestrator. Assembles features, signal, sizing, LLM filter, risk checks,
    execution, and ledger recording into a single deterministic unit.

    Public methods:
      rebalance(bars, now, headlines) : main cycle
      tick(prices, now) : intra-cycle updates
      reconcile() : startup reconciliation
    """

    def __init__(
        self,
        cfg: Config,
        broker: Broker,
        ledger: Ledger,
        filt,
        notifier,
        run_id: str,
        scope: str,
    ) -> None:
        """Initialize the engine with a broker, ledger, filter, and notifier.

        Parameters
        ----------
        cfg : Config
            Configuration (all strategy params, limits, asset class).
        broker : Broker
            Broker implementation (real, paper, or fake for backtests).
        ledger : Ledger
            SQLite ledger for audit trail and state recovery.
        filt
            LLM filter (or FakeFilter for tests).
        notifier
            Notification transport (or FakeNotifier for tests).
        run_id : str
            Unique run identifier.
        scope : str
            Scope for deterministic order ID generation.
        """
        self.cfg = cfg
        self.broker = broker
        self.ledger = ledger
        self.filter = filt
        self.notifier = notifier
        self.run_id = run_id
        self.scope = scope

        # Kill switch: reads from ledger, persists state there
        self.kill = rk.KillSwitch(ledger, cfg.risk, cfg.expanded(cfg.run.kill_reset_file))

        # Ghost book: counterfactual portfolio for filter impact measurement
        self.ghost = GhostBook(cfg, ledger)

        # Stored stops and entry decision IDs from positions snapshot
        self.stops: dict[str, float] = {}
        self.entry_ids: dict[str, str] = {}
        for row in ledger.positions_snapshot():
            if row["stop_price"] is not None:
                self.stops[row["symbol"]] = float(row["stop_price"])
            if row["entry_decision_id"]:
                self.entry_ids[row["symbol"]] = row["entry_decision_id"]

        # Flag for fee budget alarm: halves rebalance frequency until reviewed
        self.rebalance_frequency_halved = False

    # ---- Helpers ----

    def _decision_id(self, cycle_ts: datetime, symbol: str) -> str:
        """Generate a deterministic decision ID for idempotency.

        Keys the ID to the scheduled weekly rebalance slot, not wall clock time, so that
        the same weekly decision produces the same order IDs whether executed by a hand-run
        cycle followed by the loop, or by a rebalance that failed after sending orders."""
        slot = last_scheduled_weekly(cycle_ts, self.cfg.schedule.rebalance_weekday, self.cfg.schedule.rebalance_hour_utc)
        return hashlib.sha1(
            f"{self.scope}|{slot.isoformat()}|{symbol}".encode()
        ).hexdigest()[:16]

    def _state(self, now: datetime, prices: dict[str, float]) -> PortfolioState:
        """Snapshot the current portfolio state."""
        acct = self.broker.account()
        positions = {p.symbol: p for p in self.broker.positions()}
        return PortfolioState(
            as_of=now,
            equity=acct.equity,
            cash=acct.cash,
            positions=positions,
            trades_last_24h=self.ledger.risk_increasing_orders_since(now - timedelta(hours=24)),
            high_water_mark=float(self.ledger.kill_state()["hwm"]),
            prices=prices,
        )

    def _alert(self, now: datetime, kind: str, detail: str, payload: dict | None = None) -> None:
        """Record an alert and notify. Failures are logged, never raise."""
        self.ledger.record_alert(now, kind, {"detail": detail, **(payload or {})})
        try:
            self.notifier.send(ntf.msg_alert(now, kind, detail))
        except Exception as e:
            log.warning("notify failed: %s", e)

    def _execute(self, req: OrderRequest, decision_price: float, now: datetime) -> tuple[bool, str]:
        """Submit an order: ledger intent first, then broker, then fill recording.

        Returns (submitted, note). If submitted is False, the order was not sent
        (already acted on, rejected by broker, etc).
        """
        # Intent first: if this returns False, the order was already acted on (idempotency)
        if not self.ledger.record_order_intent(req, decided_at=now):
            existing = self.ledger.order(req.client_order_id)
            return False, f"already acted on (status {existing['status']})"
        return self._send(req, decision_price, now)

    def _resubmit_same_id(self, req: OrderRequest, decision_price: float, now: datetime) -> bool:
        """Resend an order whose ledger row exists in status unknown and whose venue lookup
        returned nothing. Same client id on purpose: the venue can refuse a duplicate, and
        a fresh id could not be refused. Only the stop path uses this, after reconciling."""
        row = self.ledger.order(req.client_order_id)
        if row is None or row["status"] not in ("unknown", "new"):
            return False
        self.ledger.update_order(req.client_order_id, "new", note="resend after venue reported no record")
        submitted, _note = self._send(req, decision_price, now)
        return submitted

    def _send(self, req: OrderRequest, decision_price: float, now: datetime) -> tuple[bool, str]:
        try:
            ack = self.broker.submit(req)
        except DuplicateOrderError:
            self.ledger.update_order(req.client_order_id, "unknown", note="duplicate at venue; reconcile")
            return False, "duplicate at venue"
        except OrderRejected as e:
            self.ledger.update_order(req.client_order_id, "rejected", note=str(e)[:200])
            return False, f"rejected: {e}"
        except BrokerError as e:
            self.ledger.update_order(
                req.client_order_id, "unknown", note=f"broker error: {str(e)[:200]}"
            )
            self._alert(
                now,
                "order_unknown",
                f"{req.symbol} {req.side} order state unknown after broker error; will reconcile",
            )
            return False, f"unknown: {e}"

        # Update ledger with ack
        self.ledger.update_order(
            req.client_order_id, ack.status, broker_order_id=ack.broker_order_id,
            submitted_at=ack.submitted_at
        )

        # Record any fills
        for f in self.broker.fills_for(req.client_order_id):
            slip = abs(f.price / decision_price - 1) * 1e4 if decision_price > 0 else None
            self.ledger.record_fill(
                Fill(f.client_order_id, f.symbol, f.side, f.qty, f.price, f.fee_usd, f.filled_at, slip)
            )

        return True, ack.status

    # ---- REBALANCE: The main cycle (12 steps) ----

    def rebalance(
        self, bars: dict[str, pd.DataFrame], now: datetime, headlines: tuple[str, ...] = ()
    ) -> CycleResult:
        """Main rebalance cycle: compute targets, filter candidates, execute orders, update stops.

        Parameters
        ----------
        bars : dict[str, pd.DataFrame]
            Symbol -> OHLCV DataFrame. Signals use data through bar close; fills at t+1 open.
        now : datetime
            Current time (UTC). Used for stale data guard and decision IDs.
        headlines : tuple[str, ...]
            Recent headlines for market context (data, never instructions).

        Returns
        -------
        CycleResult
            Summary of decisions, submissions, rejections, vetoes, alerts, and stops fired.
        """
        res = CycleResult(cycle_ts=now)

        # Step 1: Universe. Keep only symbols with bars, capped at max.
        universe = [s for s in self.cfg.universe.core + self.cfg.universe.candidates if s in bars][
            : self.cfg.universe.max_symbols
        ]
        if not universe:
            res.skipped_reason = "no bars for any universe symbol"
            self._alert(now, "no_data", res.skipped_reason)
            return res

        # Step 2: Stale data guard. Check if the newest bar is too old.
        # Guard against empty bars dict (all symbols have no data)
        bar_timestamps = [df.index[-1] for df in bars.values() if len(df)]
        if not bar_timestamps:
            res.skipped_reason = "no bars with data for any symbol"
            self._alert(now, "no_data", res.skipped_reason)
            return res
        newest = max(bar_timestamps)
        if rk.bars_are_stale(newest.to_pydatetime(), now, self.cfg.risk):
            res.skipped_reason = f"stale data: newest bar opened {newest.isoformat()}"
            self._alert(now, "stale_data", res.skipped_reason)
            return res

        # Step 3: Features and signal for each symbol with enough history.
        # Per-symbol error handling: one symbol's bad data does not stop the cycle (item g).
        last: dict[str, dict] = {}
        for s in universe:
            df = bars[s]
            if len(df) < self.cfg.signal.min_history_days:
                continue
            try:
                f = feat.compute_features(df, self.cfg.signal, self.cfg.asset_class.trading_days_per_year)
                row = f.iloc[-1].to_dict()
                row["conviction"] = sig.conviction_from_row(row, self.cfg.signal)
                row["thesis"] = sig.explain(row, self.cfg.signal)
                last[s] = row
            except Exception as e:
                self._alert(now, "feature_error", f"{s}: {str(e)[:200]}")
                log.warning("feature computation failed for %s: %s", s, e)
                continue

        prices = {s: float(r["close"]) for s, r in last.items()}

        # Step 4: Portfolio state. Equity must be > 0.
        state = self._state(now, prices)
        if state.equity <= 0:
            res.skipped_reason = "equity is zero or negative"
            self._alert(now, "no_equity", res.skipped_reason)
            return res

        # Step 5: Kill switch. Check for trip. If tripped, no new entries after this cycle.
        tripped_now = self.kill.update(state.equity, now)
        if tripped_now:
            res.kill_events.append(
                f"Kill switch tripped: equity {state.equity:.2f}, HWM {state.high_water_mark:.2f}"
            )
            try:
                self.notifier.send(
                    ntf.msg_kill(now, state.equity, state.high_water_mark, 1.0 - state.equity / state.high_water_mark)
                )
            except Exception as e:
                log.warning("notify failed: %s", e)
        kill_tripped = self.kill.tripped()

        # Step 6: Target weights (vol scaling + caps).
        targets = sz.target_weights(
            {s: r["conviction"] for s, r in last.items()},
            {s: float(r["realized_vol"]) for s, r in last.items()},
            self.cfg.sizing,
            len(universe),
        )

        # Step 7: For each symbol, compute delta and decide: buy, sell, or hold.
        buys: list[tuple[OrderRequest, Candidate, Decision]] = []
        sells: list[tuple[OrderRequest, Decision]] = []

        for s in universe:
            # Symbol with insufficient history: hold
            if s not in last:
                res.decisions.append(
                    Decision(
                        self._decision_id(now, s),
                        now,
                        s,
                        0.0,
                        0.0,
                        state.weight(s),
                        "hold",
                        "insufficient history",
                        None,
                        "",
                        "no_history",
                    )
                )
                continue

            r = last[s]
            t = targets[s]
            cur_w = state.weight(s)
            delta = sz.trade_notional(t.weight, cur_w, state.equity)
            did = self._decision_id(now, s)
            pos = state.positions.get(s)
            price = prices[s]

            # Compute initial stop for new entries
            atr = float(r.get("atr", price * 0.02))  # fallback to 2% if ATR missing
            stop_candidate = sz.initial_stop(price, atr, self.cfg.risk.stop_atr_multiple)
            stop_price = sz.ratchet_stop(self.stops.get(s), stop_candidate)

            # Check if the delta is inside the no-trade band
            if sz.inside_no_trade_band(delta, state.equity, self.cfg.sizing, self.cfg.asset_class.min_notional_usd):
                res.decisions.append(
                    Decision(
                        did,
                        now,
                        s,
                        r["conviction"],
                        t.weight,
                        cur_w,
                        "hold",
                        r["thesis"],
                        None,
                        "",
                        "no_trade_band",
                    )
                )
                continue

            # Delta > 0: buy (entry if no position, increase if position exists)
            if delta > 0:
                purpose = "entry" if (pos is None or pos.qty <= 0) else "increase"
                cand = Candidate(
                    symbol=s,
                    conviction=r["conviction"],
                    price=price,
                    realized_vol=float(r.get("realized_vol", 0.0)),
                    atr=atr,
                    target_weight=t.weight,
                    current_weight=cur_w,
                    existing_plan=Plan(r["thesis"], self.stops.get(s, price), "") if purpose == "increase" else None,
                )
                invalidation = f"conviction drops below 0.3 or close falls below SMA"
                # Idempotency: purpose is state-dependent (entry vs increase), so exclude from order ID.
                # decision_id + symbol + side + seq is already unique per order.
                req = OrderRequest(
                    client_order_id=rk.make_client_order_id(self.scope, did, s, "buy", seq=0),
                    decision_id=did,
                    symbol=s,
                    side="buy",
                    purpose=purpose,
                    notional_usd=delta,
                )
                dec = Decision(did, now, s, r["conviction"], t.weight, cur_w, "buy", r["thesis"],
                              stop_price, invalidation, t.binding_constraint)
                buys.append((req, cand, dec))

            # Delta < 0 and target == 0: sell all (exit)
            elif delta < 0 and t.weight == 0 and pos and pos.qty > 0:
                # Idempotency: purpose is state-dependent, so exclude from order ID.
                req = OrderRequest(
                    client_order_id=rk.make_client_order_id(self.scope, did, s, "sell", seq=0),
                    decision_id=did,
                    symbol=s,
                    side="sell",
                    purpose="exit",
                    qty=pos.qty,
                )
                dec = Decision(did, now, s, r["conviction"], t.weight, cur_w, "sell", r["thesis"],
                              None, "", t.binding_constraint)
                sells.append((req, dec))

            # Delta < 0 and target > 0: partial sell (exit)
            elif delta < 0 and t.weight > 0 and pos and pos.qty > 0:
                qty = abs(delta) / price
                # Idempotency: purpose is state-dependent, so exclude from order ID.
                req = OrderRequest(
                    client_order_id=rk.make_client_order_id(self.scope, did, s, "sell", seq=0),
                    decision_id=did,
                    symbol=s,
                    side="sell",
                    purpose="exit",
                    qty=qty,
                )
                dec = Decision(did, now, s, r["conviction"], t.weight, cur_w, "sell", r["thesis"],
                              None, "", t.binding_constraint)
                sells.append((req, dec))

            # No trade: hold
            else:
                res.decisions.append(
                    Decision(did, now, s, r["conviction"], t.weight, cur_w, "hold", r["thesis"],
                            None, "", t.binding_constraint)
                )

        # Step 8: Filter (buys only). Build candidates, call filter, record result.
        # Fail open: if the filter throws, continue with no vetoes (allow all).
        vetoed_symbols: frozenset[str] = frozenset()
        if self.cfg.llm_filter.mode != "off" and buys:
            cand_list = [c for _, c, _ in buys]
            summary = f"{len([r for r in last.values() if r['conviction'] > 0])} of {len(last)} symbols have positive conviction"
            context = MarketContext(as_of=now, summary=summary, headlines=headlines)
            try:
                filter_result = self.filter.evaluate(cand_list, context)
                res.filter_result = filter_result
                self.ledger.record_filter_result(
                    now,
                    self.cfg.llm_filter.mode,
                    filter_result,
                    applied=(self.cfg.llm_filter.mode == "active"),
                    decision_ids={c.symbol: self._decision_id(now, c.symbol) for c in cand_list},
                    candidates=tuple(c.symbol for c in cand_list),
                )
                vetoed_symbols = filter_result.vetoed()
            except Exception as e:  # noqa: BLE001  -- fail open is the requirement
                log.exception("filter.evaluate failed: %s", e)
                self.ledger.record_alert(now, "filter_error", {"detail": f"filter failed: {type(e).__name__}: {str(e)[:200]}"})
                # Continue with empty vetoes (allow all buys)

        # Step 9: Orders. Sells first, then buys. Risk-check each. Execute or reject.
        for req, dec in sells:
            # Record decision
            res.decisions.append(dec)

            # Risk check (exits always approved)
            risk_decision = rk.check_order(
                req, state, self.cfg.risk, self.cfg.sizing, self.cfg.asset_class, kill_tripped
            )
            if risk_decision.kind == "reject":
                res.rejected.append((req, risk_decision.reason))
                continue

            # Execute
            for order_req in risk_decision.orders:
                submitted, note = self._execute(order_req, prices[req.symbol], now)
                if submitted:
                    res.submitted.append(order_req)
                    # Refresh state for next checks
                    state = self._state(now, prices)
                else:
                    res.rejected.append((order_req, note))

        # Buys (filtered)
        for req, cand, dec in buys:
            # Record decision
            res.decisions.append(dec)

            # Apply filter veto (if in active or shadow mode)
            if cand.symbol in vetoed_symbols:
                if self.cfg.llm_filter.mode == "active":
                    res.vetoed.append(cand.symbol)
                    continue  # Do not submit in active mode
                # In shadow mode, continue to submit but ghost book will skip
            # In off mode, no filter was called

            # Risk check
            risk_decision = rk.check_order(
                req, state, self.cfg.risk, self.cfg.sizing, self.cfg.asset_class, kill_tripped
            )
            if risk_decision.kind == "reject":
                res.rejected.append((req, risk_decision.reason))
                continue

            # Execute
            for order_req in risk_decision.orders:
                submitted, note = self._execute(order_req, prices[req.symbol], now)
                if submitted:
                    res.submitted.append(order_req)
                    # Refresh state for next checks
                    state = self._state(now, prices)
                else:
                    res.rejected.append((order_req, note))

        # Step 10: Stops. Store ratcheted stop prices.
        positions = state.positions
        for s in universe:
            if s in last:
                r = last[s]
                price = prices[s]
                atr = float(r.get("atr", price * 0.02))
                stop_candidate = sz.initial_stop(price, atr, self.cfg.risk.stop_atr_multiple)
                stop_price = sz.ratchet_stop(self.stops.get(s), stop_candidate)
                if s in positions:
                    self.stops[s] = stop_price
                elif s in self.stops:
                    del self.stops[s]
            elif s in self.stops:
                del self.stops[s]

        # Persist stops to ledger
        self.ledger.snapshot_positions(
            list(positions.values()), self.stops, self.entry_ids, now
        )

        # Step 11: Ghost book. Seed if needed, execute trades, persist.
        # Ghost book logic (spec section 'THE GHOST BOOK'):
        # - Shadow mode: real book buys everything; ghost applies vetoes (skips vetoed buys)
        # - Active mode: real book skips vetoed; ghost adds them back (executes all including vetoed)
        # - Off mode: ghost mirrors real exactly
        self.ghost.ensure_seeded(state.equity, list(positions.values()), prices)
        # Sync stops to ghost book: the counterfactual runs with same stops as the real book
        self.ghost.stops = dict(self.stops)
        for req, _ in sells:
            self.ghost.submit(req, ts=now)
        for req, cand, _ in buys:
            # Ghost skips a vetoed buy only in shadow mode (the counterfactual applies the veto)
            if cand.symbol in vetoed_symbols and self.cfg.llm_filter.mode == "shadow":
                continue
            self.ghost.submit(req, ts=now)
        self.ghost.mark(prices, now)
        self.ghost.persist(now)

        # Record all decisions to ledger
        for dec in res.decisions:
            self.ledger.record_decision(dec, self.run_id)

        # Step 12: Notify and record equity.
        actions = []
        for req, _, _ in buys:
            if req not in [r[0] for r in res.rejected] and any(r.client_order_id == req.client_order_id for r in res.submitted):
                qty = req.qty if req.qty else req.notional_usd / prices[req.symbol]
                actions.append(f"Bought {qty:.6f} of {req.symbol}")
        for req, _ in sells:
            if req not in [r[0] for r in res.rejected] and any(r.client_order_id == req.client_order_id for r in res.submitted):
                actions.append(f"Sold {req.qty:.6f} of {req.symbol}")

        try:
            self.notifier.send(
                ntf.msg_rebalance(now, state.equity, actions, list(res.vetoed), self.cfg.run.mode)
            )
        except Exception as e:
            log.warning("notify failed: %s", e)

        # Record equity curve
        self.ledger.record_equity(now, state.equity, state.cash, state.gross_exposure,
                                 state.high_water_mark, 1.0 - state.equity / state.high_water_mark if state.high_water_mark > 0 else 0.0)

        return res

    # ---- TICK: Intra-cycle updates ----

    def tick(self, prices: dict[str, float], now: datetime) -> TickResult:
        """Intra-cycle tick: update kill switch, fire stops, update equity.

        Parameters
        ----------
        prices : dict[str, float]
            Symbol -> last traded price.
        now : datetime
            Current time (UTC).

        Returns
        -------
        TickResult
            Equity, drawdown, kill switch events, stops fired, and alarms.
        """
        res = TickResult(ts=now, equity=0.0, drawdown=0.0)

        # Step 1: Mark equity. Check kill switch and reset file.
        state = self._state(now, prices)
        res.equity = state.equity
        res.drawdown = 1.0 - state.equity / state.high_water_mark if state.high_water_mark > 0 else 0.0

        tripped_now = self.kill.update(state.equity, now)
        if tripped_now:
            res.kill_tripped_now = True
            try:
                self.notifier.send(
                    ntf.msg_kill(now, state.equity, state.high_water_mark, res.drawdown)
                )
            except Exception as e:
                log.warning("notify failed: %s", e)

        reset_now = self.kill.consume_reset_file(state.equity, now)
        if reset_now:
            res.kill_reset_now = True

        # Step 2: Synthetic stops. Fire any stops whose price has breached.
        for symbol, stop_price in list(self.stops.items()):
            if symbol not in prices:
                continue
            if prices[symbol] <= stop_price:
                pos = state.positions.get(symbol)
                if pos and pos.qty > 0:
                    # Use entry_decision_id if known, else 'held' (for seeds and edge cases)
                    entry_id = self.entry_ids.get(symbol, "held")

                    # Idempotent stop order IDs: keyed to (scope, entry_id, symbol, "sell", seq)
                    # Count prior stop orders for this symbol to determine seq
                    all_orders = self.ledger.orders()
                    prior_stops = [
                        o for o in all_orders
                        if o["symbol"] == symbol and o["side"] == "sell" and o["purpose"] == "stop"
                    ]
                    prior_stops.sort(key=lambda o: o["created_at"])

                    # Reconcile any non-terminal stop order (may be retrying after network failure)
                    should_submit = False
                    for prior_stop in prior_stops:
                        if prior_stop["status"] in ("unknown", "new", "accepted", "partially_filled"):
                            # Check broker for actual status
                            broker_order = self.broker.order_by_client_id(prior_stop["client_order_id"])
                            if broker_order is None:
                                # The venue has no record of it. Resend under the SAME client id: if
                                # the venue really never got it, this is the first copy it sees; if it
                                # did get it and the lookup lied, the venue refuses the duplicate and
                                # the row goes back to unknown for the next tick. Either way the venue
                                # can never hold two live stops for one position.
                                retry = OrderRequest(
                                    client_order_id=prior_stop["client_order_id"], decision_id=entry_id,
                                    symbol=symbol, side="sell", purpose="stop", qty=pos.qty)
                                if self._resubmit_same_id(retry, prices[symbol], now):
                                    res.stops_fired.append(symbol)
                                    del self.stops[symbol]
                                    try:
                                        self.notifier.send(ntf.msg_stop(now, symbol, prices[symbol], stop_price, pos.qty))
                                    except Exception as e:  # noqa: BLE001
                                        log.warning("notify failed: %s", e)
                                break  # one attempt per tick
                            elif broker_order.filled_qty > 0 or broker_order.status == "filled":
                                # Already filled, record and stop here
                                if broker_order.filled_qty > 0:
                                    self.ledger.record_fill(Fill(
                                        client_order_id=broker_order.client_order_id,
                                        symbol=symbol,
                                        side="sell",
                                        qty=broker_order.filled_qty,
                                        price=broker_order.filled_avg_price or prices[symbol],
                                        fee_usd=0.0,
                                        slippage_bps_est=None,
                                        filled_at=broker_order.filled_at or now,
                                    ))
                                    self.ledger.update_order(prior_stop["client_order_id"], "filled")
                                res.stops_fired.append(symbol)
                                del self.stops[symbol]
                                break  # Exit the loop, stop completed
                            else:
                                # Still pending at venue, wait until next tick
                                log.debug("stop order %s for %s still pending at venue", prior_stop["client_order_id"], symbol)
                                break  # Exit the loop, wait for next tick
                    else:
                        # No non-terminal orders found (all were terminal or list was empty)
                        should_submit = True

                    # Submit a stop if we should (no non-terminal orders, or one was rejected)
                    if should_submit and symbol in self.stops:
                        # seq = number of prior attempts. A fresh attempt only happens after the venue
                        # confirmed the earlier one never arrived (or none exists), so the venue sees
                        # exactly one live stop per position. Reusing seq 0 would collide with the
                        # ledger row of the failed attempt and skip the stop silently.
                        seq = len(prior_stops)
                        client_id = rk.make_client_order_id(
                            self.scope, entry_id, symbol, "sell", seq=seq
                        )
                        req = OrderRequest(
                            client_order_id=client_id,
                            decision_id=entry_id,
                            symbol=symbol,
                            side="sell",
                            purpose="stop",
                            qty=pos.qty,
                        )
                        risk_decision = rk.check_order(
                            req, state, self.cfg.risk, self.cfg.sizing, self.cfg.asset_class, False
                        )
                        for order_req in risk_decision.orders:
                            submitted, note = self._execute(order_req, prices[symbol], now)
                            if submitted:
                                res.stops_fired.append(symbol)
                                del self.stops[symbol]
                                try:
                                    self.notifier.send(ntf.msg_stop(now, symbol, prices[symbol], stop_price, pos.qty))
                                except Exception as e:
                                    log.warning("notify failed: %s", e)
                            else:
                                if "already acted on" not in note:
                                    self.ledger.record_alert(now, "stop_id_collision",
                                                           {"symbol": symbol, "reason": note, "seq": seq})
                            # Refresh state
                            state = self._state(now, prices)

        # Step 3: Ghost book. Mark to market and execute its stops.
        self.ghost.mark(prices, now)
        for symbol, stop_price in list(self.ghost.stops.items()):
            if symbol in prices:
                pos = next((p for p in self.ghost.broker.positions() if p.symbol == symbol), None) if self.ghost.broker else None
                if pos and pos.qty > 0:
                    self.ghost.execute_stop(symbol, prices[symbol], pos.qty, now)

        # Step 4: Record equity, check alarms.
        self.ledger.record_equity(now, state.equity, state.cash, state.gross_exposure,
                                 state.high_water_mark, res.drawdown)

        # Fee budget alarm
        trailing_fees = self.ledger.fees_since(now - timedelta(days=30))
        if rk.fee_budget_exceeded(trailing_fees, 30, state.equity, self.cfg.risk):
            res.alerts.append("fee_budget")
            self._alert(now, "fee_budget", f"Trailing 30d fees exceed budget. Halving rebalance frequency.")
            self.rebalance_frequency_halved = True

        # Slippage alarm
        recent_fills = self.ledger.fills(since=now - timedelta(days=1))
        if len(recent_fills) >= 20:
            slippages = [f["slippage_bps_est"] for f in recent_fills if f["slippage_bps_est"] is not None]
            if slippages:
                mean_slip = sum(slippages) / len(slippages)
                if mean_slip > self.cfg.risk.slippage_alarm_multiple * self.cfg.asset_class.slippage_bps:
                    res.alerts.append("slippage")
                    self._alert(now, "slippage", f"Mean slippage {mean_slip:.1f} bps exceeds model {self.cfg.asset_class.slippage_bps:.1f} bps")

        return res

    # ---- RECONCILE: Startup reconciliation ----

    def reconcile(self) -> ReconcileResult:
        """Reconcile the ledger against the broker on every start.

        Parameters
        ----------
        None

        Returns
        -------
        ReconcileResult
            Orders updated, unknown broker orders found, still-open orders.
        """
        res = ReconcileResult()

        # For each open ledger order, ask the broker for it by client_order_id
        for row in self.ledger.open_orders():
            cid = row["client_order_id"]
            broker_order = self.broker.order_by_client_id(cid)
            if broker_order is None:
                res.unknown_broker_orders.append(cid)
                res.still_open.append(cid)
                continue
            # Update ledger with broker status
            if broker_order.status != row["status"]:
                self.ledger.update_order(cid, broker_order.status)
                res.updated.append(cid)
            # If the order filled but we don't have a fill row, add it
            if broker_order.status == "filled":
                existing_fills = self.ledger.fills_for_order(cid)
                if not existing_fills and broker_order.filled_qty > 0 and broker_order.filled_avg_price:
                    # Reconstruct fill from order
                    f = Fill(
                        client_order_id=cid,
                        symbol=broker_order.symbol,
                        side=broker_order.side,
                        qty=broker_order.filled_qty,
                        price=broker_order.filled_avg_price,
                        fee_usd=broker_order.filled_qty * broker_order.filled_avg_price * self.cfg.asset_class.taker_fee_bps / 1e4,
                        filled_at=broker_order.filled_at or utcnow(),
                        slippage_bps_est=None,
                    )
                    self.ledger.record_fill(f)
            else:
                res.still_open.append(cid)

        # For each broker order not in the ledger: alert (never adopt silently)
        now = utcnow()
        for broker_order in self.broker.open_orders():
            if not self.ledger.order(broker_order.client_order_id):
                res.unknown_broker_orders.append(broker_order.client_order_id)
                self._alert(now, "unknown_broker_order",
                           f"Found unknown broker order {broker_order.client_order_id}: "
                           f"{broker_order.symbol} {broker_order.side} (not in ledger)")

        return res
