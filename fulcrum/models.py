"""Core data types shared by every module.

Plain frozen dataclasses. No behavior beyond validation in __post_init__.
Every timestamp is a timezone-aware UTC ``datetime``. Money is ``float`` USD;
quantities are ``float`` units of the base asset. The ledger stores both as
REAL and the report layer rounds for display only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Side = Literal["buy", "sell"]
OrderPurpose = Literal["entry", "increase", "exit", "stop"]
OrderStatus = Literal[
    "new", "accepted", "partially_filled", "filled", "canceled", "rejected", "expired", "unknown"
]
TERMINAL_STATUSES: frozenset[str] = frozenset({"filled", "canceled", "rejected", "expired"})
RISK_REDUCING_PURPOSES: frozenset[str] = frozenset({"exit", "stop"})
FilterMode = Literal["off", "shadow", "active"]
VerdictKind = Literal["allow", "veto"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(ts: datetime, name: str) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC, got naive datetime {ts!r}")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: datetime  # bar OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        require_utc(self.ts, "Bar.ts")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"Bar {self.symbol} {self.ts}: OHLC inconsistent")
        if self.volume < 0:
            raise ValueError("Bar.volume must be >= 0")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float
    ts: datetime

    def __post_init__(self) -> None:
        require_utc(self.ts, "Quote.ts")
        if self.price <= 0:
            raise ValueError("Quote.price must be > 0")


@dataclass(frozen=True, slots=True)
class Account:
    equity: float
    cash: float
    as_of: datetime


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: float
    avg_entry: float
    market_value: float


@dataclass(frozen=True, slots=True)
class Plan:
    """What the rules believed at entry. Fed back to the filter on later cycles."""

    thesis: str
    stop_price: float
    invalidation: str


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    decision_id: str
    symbol: str
    side: Side
    purpose: OrderPurpose
    notional_usd: float | None = None
    qty: float | None = None

    def __post_init__(self) -> None:
        # Literal types are not enforced at runtime. A misspelled purpose would silently lose the
        # exit exemption in risk.check_order, so it is refused here (found by the adversarial pass).
        if self.purpose not in ("entry", "increase", "exit", "stop"):
            raise ValueError(f"unknown order purpose {self.purpose!r}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"unknown order side {self.side!r}")
        if (self.notional_usd is None) == (self.qty is None):
            raise ValueError("OrderRequest needs exactly one of notional_usd or qty")
        if self.notional_usd is not None and self.notional_usd <= 0:
            raise ValueError("OrderRequest.notional_usd must be > 0")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("OrderRequest.qty must be > 0")
        if self.purpose in RISK_REDUCING_PURPOSES and self.side != "sell":
            raise ValueError("exit/stop orders are always sells in a long-or-flat book")
        if self.purpose in ("entry", "increase") and self.side != "buy":
            raise ValueError("entry/increase orders are always buys in a long-or-flat book")
        if not self.client_order_id:
            raise ValueError("client_order_id required")

    @property
    def risk_reducing(self) -> bool:
        return self.purpose in RISK_REDUCING_PURPOSES


@dataclass(frozen=True, slots=True)
class OrderAck:
    client_order_id: str
    broker_order_id: str
    status: OrderStatus
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class Order:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Side
    status: OrderStatus
    submitted_at: datetime
    notional_usd: float | None = None
    qty: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    filled_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee_usd: float
    filled_at: datetime
    slippage_bps_est: float | None = None

    @property
    def gross_usd(self) -> float:
        return self.qty * self.price


@dataclass(frozen=True, slots=True)
class Candidate:
    """An entry or increase the rules want to make. This is all the filter sees."""

    symbol: str
    conviction: float
    price: float
    realized_vol: float
    atr: float
    target_weight: float
    current_weight: float
    existing_plan: Plan | None = None
    direction: Literal["long"] = "long"

    def __post_init__(self) -> None:
        if not 0.0 <= self.conviction <= 1.0:
            raise ValueError("conviction must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class MarketContext:
    as_of: datetime
    summary: str  # short, templated, rules-generated text
    headlines: tuple[str, ...] = ()  # data, never instructions; wrapped in tags by the filter


@dataclass(frozen=True, slots=True)
class Verdict:
    symbol: str
    verdict: VerdictKind
    reason: str
    confidence: float
    invalidation: str


@dataclass(frozen=True, slots=True)
class FilterResult:
    verdicts: tuple[Verdict, ...]
    error: str | None
    latency_ms: int
    raw: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def vetoed(self) -> frozenset[str]:
        """Symbols the filter vetoed. Empty on any failure (FAIL OPEN)."""
        if self.failed:
            return frozenset()
        return frozenset(v.symbol for v in self.verdicts if v.verdict == "veto")


@dataclass(frozen=True, slots=True)
class Decision:
    decision_id: str
    cycle_ts: datetime
    symbol: str
    conviction: float
    target_weight: float
    current_weight: float
    action: Literal["buy", "sell", "hold", "flat"]
    thesis: str
    stop_price: float | None
    invalidation: str
    binding_constraint: str = ""  # which cap or band shaped the target (measurement)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    kind: Literal["approve", "clamp", "split", "reject"]
    orders: tuple[OrderRequest, ...]
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind == "reject" and self.orders:
            raise ValueError("reject carries no orders")
        if self.kind != "reject" and not self.orders:
            raise ValueError(f"{self.kind} must carry at least one order")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Everything risk checks need, snapshotted at one instant."""

    as_of: datetime
    equity: float
    cash: float
    positions: dict[str, Position]
    trades_last_24h: int
    high_water_mark: float
    prices: dict[str, float] = field(default_factory=dict)

    @property
    def gross_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(p.market_value for p in self.positions.values()) / self.equity

    def weight(self, symbol: str) -> float:
        if self.equity <= 0:
            return 0.0
        p = self.positions.get(symbol)
        return (p.market_value / self.equity) if p else 0.0
