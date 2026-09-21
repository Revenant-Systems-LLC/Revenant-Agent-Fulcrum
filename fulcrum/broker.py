"""Broker interface and the deterministic FakeBroker.

Two implementations exist from day one: ``FakeBroker`` (tests, backtests, dry-run) and
``AlpacaBroker`` (paper only, in ``alpaca.py``). Every order the engine produces has
already passed ``risk.check_order``; the broker is the last thing that touches it and
it does no strategy work of its own.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Callable, Protocol

from .models import Account, Fill, Order, OrderAck, OrderRequest, Position, Quote, utcnow

# The only base URL AlpacaBroker will ever accept. A constant, not a config value.
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    """Transport or venue error. The caller must assume the order state is UNKNOWN until reconciled."""


class DuplicateOrderError(BrokerError):
    """The venue already has an order with this client_order_id."""


class OrderRejected(BrokerError):
    """The venue refused the order (bad size, insufficient funds, unknown symbol...)."""


class Broker(Protocol):
    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def submit(self, req: OrderRequest) -> OrderAck: ...
    def order_by_client_id(self, client_order_id: str) -> Order | None: ...
    def open_orders(self) -> list[Order]: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def latest_price(self, symbol: str) -> Quote: ...
    def fills_for(self, client_order_id: str) -> list[Fill]: ...


class FakeBroker:
    """Deterministic broker for tests, backtests and dry-run.

    Fills every market order immediately at the last set price, moved against the trader
    by ``slippage_bps`` and charged ``fee_bps`` of gross notional. Long-or-flat only:
    a sell larger than the position is rejected (a tiny float overshoot is clamped).
    Duplicate client_order_ids are rejected like a real venue would.
    """

    def __init__(self, *, starting_cash: float, fee_bps: float, slippage_bps: float, min_notional_usd: float = 0.0,
                 clock: Callable[[], datetime] | None = None):
        if starting_cash < 0 or fee_bps < 0 or slippage_bps < 0 or min_notional_usd < 0:
            raise ValueError("FakeBroker parameters must be non-negative")
        self.cash = float(starting_cash)
        self.fee_bps = float(fee_bps)
        self.slippage_bps = float(slippage_bps)
        self.min_notional_usd = float(min_notional_usd)
        self._clock = clock or utcnow
        self._prices: dict[str, Quote] = {}
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = {}
        self.all_fills: list[Fill] = []
        self.total_fees: float = 0.0
        self.fail_next: str | None = None  # "network" | "reject" for tests
        self._seq = 0

    # ---- market state -----------------------------------------------------
    def set_price(self, symbol: str, price: float, ts: datetime | None = None) -> None:
        self._prices[symbol] = Quote(symbol=symbol, price=float(price), ts=ts or self._clock())
        pos = self._positions.get(symbol)
        if pos:
            self._positions[symbol] = Position(pos.symbol, pos.qty, pos.avg_entry, pos.qty * float(price))

    def set_prices(self, prices: dict[str, float], ts: datetime | None = None) -> None:
        for s, p in prices.items():
            self.set_price(s, p, ts)

    def latest_price(self, symbol: str) -> Quote:
        if symbol not in self._prices:
            raise BrokerError(f"no price for {symbol}")
        return self._prices[symbol]

    # ---- account ----------------------------------------------------------
    def account(self) -> Account:
        mv = sum(p.market_value for p in self._positions.values())
        return Account(equity=self.cash + mv, cash=self.cash, as_of=self._clock())

    def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.qty > 0]

    # ---- orders -----------------------------------------------------------
    def submit(self, req: OrderRequest) -> OrderAck:
        if self.fail_next == "network":
            self.fail_next = None
            raise BrokerError("simulated network failure after submit; order state unknown")
        if self.fail_next == "reject":
            self.fail_next = None
            raise OrderRejected("simulated venue rejection")
        if req.client_order_id in self._orders:
            raise DuplicateOrderError(f"client_order_id {req.client_order_id} already exists")
        q = self.latest_price(req.symbol)
        slip = self.slippage_bps / 1e4
        fill_price = q.price * (1 + slip) if req.side == "buy" else q.price * (1 - slip)

        if req.side == "buy":
            notional = req.notional_usd if req.notional_usd is not None else req.qty * fill_price
            if notional < self.min_notional_usd:
                raise OrderRejected(f"notional {notional:.2f} below minimum {self.min_notional_usd:.2f}")
            qty = notional / fill_price
            fee = notional * self.fee_bps / 1e4
            if notional + fee > self.cash + 1e-9:
                raise OrderRejected(f"insufficient cash: need {notional + fee:.2f}, have {self.cash:.2f}")
            self.cash -= notional + fee
            pos = self._positions.get(req.symbol)
            if pos and pos.qty > 0:
                new_qty = pos.qty + qty
                avg = (pos.avg_entry * pos.qty + fill_price * qty) / new_qty
            else:
                new_qty, avg = qty, fill_price
            self._positions[req.symbol] = Position(req.symbol, new_qty, avg, new_qty * q.price)
        else:
            pos = self._positions.get(req.symbol)
            held = pos.qty if pos else 0.0
            if held <= 0:
                raise OrderRejected(f"no {req.symbol} position to sell")
            qty = req.qty if req.qty is not None else req.notional_usd / fill_price
            if qty > held * (1 + 1e-6):
                raise OrderRejected(f"sell qty {qty} exceeds position {held}; this book is long or flat")
            qty = min(qty, held)
            notional = qty * fill_price
            if notional < self.min_notional_usd and not math.isclose(qty, held, rel_tol=1e-9):
                raise OrderRejected(f"notional {notional:.2f} below minimum {self.min_notional_usd:.2f}")
            fee = notional * self.fee_bps / 1e4
            self.cash += notional - fee
            remaining = held - qty
            if remaining <= 1e-12:
                self._positions.pop(req.symbol, None)
            else:
                self._positions[req.symbol] = Position(req.symbol, remaining, pos.avg_entry, remaining * q.price)

        self._seq += 1
        now = self._clock()
        broker_id = f"fake-{self._seq:08d}"
        fill = Fill(client_order_id=req.client_order_id, symbol=req.symbol, side=req.side, qty=qty, price=fill_price,
                    fee_usd=fee, filled_at=now, slippage_bps_est=self.slippage_bps)
        order = Order(client_order_id=req.client_order_id, broker_order_id=broker_id, symbol=req.symbol, side=req.side,
                      status="filled", submitted_at=now, notional_usd=req.notional_usd, qty=req.qty, filled_qty=qty,
                      filled_avg_price=fill_price, filled_at=now)
        self._orders[req.client_order_id] = order
        self._fills[req.client_order_id] = [fill]
        self.all_fills.append(fill)
        self.total_fees += fee
        return OrderAck(client_order_id=req.client_order_id, broker_order_id=broker_id, status="filled", submitted_at=now)

    def order_by_client_id(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if not o.terminal]

    def cancel(self, broker_order_id: str) -> None:
        for o in self._orders.values():
            if o.broker_order_id == broker_order_id and not o.terminal:
                self._orders[o.client_order_id] = replace(o, status="canceled")
                return
        raise BrokerError(f"no open order {broker_order_id}")

    def fills_for(self, client_order_id: str) -> list[Fill]:
        return list(self._fills.get(client_order_id, []))

    # ---- test/backtest helpers -------------------------------------------
    def inject_order(self, order: Order) -> None:
        """Plant an order the ledger does not know about (reconciliation tests)."""
        self._orders[order.client_order_id] = order
