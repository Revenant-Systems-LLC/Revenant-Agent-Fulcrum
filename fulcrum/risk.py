"""Risk limits: the gate every order passes through.

Pure functions plus the KillSwitch stateful class. This is the critical gatekeeper
for sizing, caps, and kill-switch enforcement. There is no bypass parameter anywhere.

The LLM filter never sees risk limits and cannot affect them. Exits and stops
(risk_reducing purposes) are always approved, regardless of kill switch, caps, or state.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .models import OrderRequest, PortfolioState, RiskDecision
from .config import RiskConfig, SizingConfig, AssetClassConfig


def _as_dt(value) -> datetime | None:
    """sqlite returns stored timestamps as ISO text; the ledger API wants aware datetimes."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def make_client_order_id(
    scope: str, decision_id: str, symbol: str, side: str, purpose: str, seq: int, max_len: int = 48
) -> str:
    """Deterministic, idempotent order ID.

    Parameters
    ----------
    scope : str
        Ledger scope (e.g., "run_id" or similar).
    decision_id : str
        Decision that triggered this order.
    symbol : str
        Asset symbol.
    side : str
        "buy" or "sell".
    purpose : str
        Order purpose: entry, increase, exit, stop.
    seq : int
        Sequence number for split orders (0 for the first order from a decision).
    max_len : int
        Maximum length to truncate to.

    Returns
    -------
    str
        SHA1 hex digest, truncated to max_len. Same inputs, same ID.
    """
    parts = [scope, decision_id, symbol, side, purpose, str(seq)]
    payload = "|".join(parts)
    digest = hashlib.sha1(payload.encode()).hexdigest()
    return digest[:max_len]


def check_order(
    req: OrderRequest,
    state: PortfolioState,
    risk: RiskConfig,
    sizing: SizingConfig,
    asset: AssetClassConfig,
    kill_tripped: bool,
) -> RiskDecision:
    """Single gate every order passes through.

    Rules, in this order:
    1. Risk-reducing orders (exit, stop) are always approved unchanged.
    2. Kill switch tripped: reject.
    3. 24h trade cap exceeded: reject.
    4. Compute and clamp notional to the smallest of four limits.
    5. If clamped < minimum notional: reject.
    6. If clamped < original: return clamp decision with new OrderRequest.

    Parameters
    ----------
    req : OrderRequest
        Order request, already validated.
    state : PortfolioState
        Current portfolio state.
    risk : RiskConfig
        Risk configuration.
    sizing : SizingConfig
        Sizing configuration.
    asset : AssetClassConfig
        Asset class configuration.
    kill_tripped : bool
        Whether the kill switch is tripped.

    Returns
    -------
    RiskDecision
        approve, clamp, or reject.
    """
    # Rule 1: Risk-reducing orders bypass everything.
    if req.risk_reducing:
        return RiskDecision(kind="approve", orders=(req,), reason="")

    # Rule 2: Kill switch tripped.
    if kill_tripped:
        return RiskDecision(kind="reject", orders=(), reason="kill switch tripped")

    # Rule 3: 24h trade cap.
    if state.trades_last_24h >= risk.max_trades_per_24h:
        return RiskDecision(kind="reject", orders=(), reason="24h trade cap")

    # Rule 3b: the account numbers must be usable. NaN or non-positive equity would make every
    # clamp below inert (NaN compares False), so a broken account snapshot means no new risk.
    # Exits were already approved above; this only ever blocks buys.
    gross = state.gross_exposure
    if (not math.isfinite(state.equity) or state.equity <= 0 or not math.isfinite(state.cash)
            or not math.isfinite(gross)):
        return RiskDecision(kind="reject", orders=(), reason="account numbers unusable")

    # Rule 4: Compute order notional and clamp to four limits.
    # Get the initial notional.
    if req.notional_usd is not None:
        orig_notional = req.notional_usd
    else:
        # notional = qty * price
        price = state.prices.get(req.symbol)
        if price is None:
            raise ValueError(f"no price for {req.symbol}")
        if not math.isfinite(price) or price <= 0:
            # A zero, negative or infinite price cannot size an order; block the buy, do not abort the cycle.
            return RiskDecision(kind="reject", orders=(), reason=f"unusable price for {req.symbol}")
        orig_notional = req.qty * price
    if not math.isfinite(orig_notional):
        return RiskDecision(kind="reject", orders=(), reason="order size unusable")

    # Clamp 1: order_pct_equity
    clamp_1 = risk.max_order_pct_equity * state.equity

    # Clamp 2: per-asset cap (room left under max_weight_per_asset).
    current_weight = state.weight(req.symbol)
    room_per_asset = (sizing.max_weight_per_asset - current_weight) * state.equity
    clamp_2 = room_per_asset

    # Clamp 3: gross cap (room left under max_gross_exposure).
    room_gross = (sizing.max_gross_exposure - state.gross_exposure) * state.equity
    clamp_3 = room_gross

    # Clamp 4: cash available (after fees).
    clamp_4 = state.cash / (1 + asset.taker_fee_bps / 1e4)

    # Compute the final clamped notional: minimum of the four.
    clamped_notional = min(clamp_1, clamp_2, clamp_3, clamp_4)

    # Rule 5: Check against minimum notional.
    if clamped_notional < asset.min_notional_usd:
        # Determine which limit bound.
        if clamped_notional == clamp_1:
            reason = "order clamp"
        elif clamped_notional == clamp_2:
            reason = "per-asset cap"
        elif clamped_notional == clamp_3:
            reason = "gross cap"
        else:
            reason = "cash"
        return RiskDecision(kind="reject", orders=(), reason=reason)

    # Rule 6: Compare clamped to original.
    if clamped_notional < orig_notional - 1e-9:
        # Determine which limit bound.
        if clamped_notional == clamp_1:
            reason = "order clamp"
        elif clamped_notional == clamp_2:
            reason = "per-asset cap"
        elif clamped_notional == clamp_3:
            reason = "gross cap"
        else:
            reason = "cash"

        # Return clamped order.
        clamped_req = replace(req, notional_usd=clamped_notional, qty=None)
        return RiskDecision(kind="clamp", orders=(clamped_req,), reason=reason)

    # Approve unchanged.
    return RiskDecision(kind="approve", orders=(req,), reason="")


class KillSwitch:
    """Drawdown-triggered kill switch with manual reset.

    The kill switch trips at cfg.kill_switch_drawdown from the high-water mark (hwm).
    It can only be reset by Dave creating a file by hand; the bot never creates, writes,
    deletes, renames, or touches the reset file.

    On the first update(), initialize hwm to the current equity if it is currently 0.
    """

    def __init__(self, ledger, cfg: RiskConfig, reset_file: Path):
        """Initialize from ledger state.

        Parameters
        ----------
        ledger : Ledger
            Ledger instance for persistence.
        cfg : RiskConfig
            Risk configuration.
        reset_file : Path
            Path to the reset file. The bot never creates it.
        """
        self.ledger = ledger
        self.cfg = cfg
        self.reset_file = Path(reset_file)

    def tripped(self) -> bool:
        """Check if the kill switch is tripped.

        Returns
        -------
        bool
            True if tripped.
        """
        state = self.ledger.kill_state()
        return bool(state["tripped"])

    def update(self, equity: float, ts: datetime) -> bool:
        """Update hwm and check for trip.

        Parameters
        ----------
        equity : float
            Current portfolio equity.
        ts : datetime
            Current timestamp (UTC).

        Returns
        -------
        bool
            True if the switch tripped on this call (first trip only).
        """
        state = self.ledger.kill_state()

        hwm = state["hwm"]
        tripped = bool(state["tripped"])

        # Initialize hwm on first call (hwm == 0).
        if hwm == 0.0:
            self.ledger.set_kill_state(
                tripped=False, hwm=equity, tripped_at=None, drawdown_at_trip=None
            )
            self.ledger.record_kill_event(ts, "init", equity, 0.0, "hwm initialized")
            return False

        # Update hwm if equity exceeds it. sqlite hands timestamps back as ISO text; the ledger
        # wants datetimes, so convert before passing them through (found by the adversarial pass).
        if equity > hwm:
            self.ledger.set_kill_state(
                tripped=tripped, hwm=equity, tripped_at=_as_dt(state["tripped_at"]) if tripped else None,
                drawdown_at_trip=state["drawdown_at_trip"], reset_at=_as_dt(state["reset_at"]),
            )
            hwm = equity

        # Already tripped: no more trips.
        if tripped:
            return False

        # Compute drawdown and check for trip.
        drawdown = 1.0 - equity / hwm

        if drawdown >= self.cfg.kill_switch_drawdown:
            # Trip.
            self.ledger.set_kill_state(
                tripped=True, hwm=hwm, tripped_at=ts, drawdown_at_trip=drawdown
            )
            self.ledger.record_kill_event(ts, "trip", hwm, drawdown, "drawdown threshold exceeded")
            return True

        return False

    def consume_reset_file(self, equity: float, ts: datetime) -> bool:
        """Consume a reset file if present and not already consumed.

        Parameters
        ----------
        equity : float
            Current portfolio equity (used as the new hwm after reset).
        ts : datetime
            Current timestamp (UTC).

        Returns
        -------
        bool
            True if reset was applied, False otherwise (not tripped, file missing, or already consumed).
        """
        # Only reset if currently tripped.
        if not self.tripped():
            return False

        # Check if reset file exists.
        if not self.reset_file.exists():
            return False

        # Get the file's mtime in nanoseconds.
        file_mtime_ns = self.reset_file.stat().st_mtime_ns

        # Check if this file has already been consumed.
        if self.ledger.reset_already_consumed(file_mtime_ns):
            return False

        # Apply reset: clear tripped, set hwm to current equity, record event.
        self.ledger.set_kill_state(
            tripped=False, hwm=equity, tripped_at=None, drawdown_at_trip=None, reset_at=ts
        )
        self.ledger.mark_reset_consumed(file_mtime_ns)
        self.ledger.record_kill_event(ts, "reset", equity, None, "reset file consumed")

        return True


def fee_budget_exceeded(
    fees_trailing_usd: float, window_days: int, equity: float, cfg: RiskConfig
) -> bool:
    """Check if trailing fees exceed the annual budget.

    Parameters
    ----------
    fees_trailing_usd : float
        Fees paid over the trailing window (USD).
    window_days : int
        Length of the window in days.
    equity : float
        Current portfolio equity (USD).
    cfg : RiskConfig
        Risk configuration.

    Returns
    -------
    bool
        True if annualized fees exceed budget.
    """
    if window_days <= 0 or equity <= 0:
        return False

    # Annualize fees.
    annualized_fees = fees_trailing_usd * 365.0 / window_days
    budget = cfg.fee_budget_annual_pct_equity * equity

    return annualized_fees > budget


def slippage_alarm(
    realized_bps: float, cfg: RiskConfig, modeled_bps: float
) -> bool:
    """Check if realized slippage exceeds the alarm threshold.

    Parameters
    ----------
    realized_bps : float
        Realized slippage in basis points.
    cfg : RiskConfig
        Risk configuration.
    modeled_bps : float
        Modeled slippage in basis points.

    Returns
    -------
    bool
        True if realized is more than the alarm multiple * modeled.
    """
    return realized_bps > cfg.slippage_alarm_multiple * modeled_bps


def bars_are_stale(
    newest_bar_open: datetime, now: datetime, cfg: RiskConfig, bar_minutes: int = 1440
) -> bool:
    """Check if the newest bar is older than allowed.

    Parameters
    ----------
    newest_bar_open : datetime
        Timestamp of the newest bar's open (UTC).
    now : datetime
        Current timestamp (UTC).
    cfg : RiskConfig
        Risk configuration.
    bar_minutes : int
        Duration of each bar in minutes (default 1440 = 1 day).

    Returns
    -------
    bool
        True if the bar's close is older than stale_bar_max_age_minutes.
    """
    from datetime import timedelta

    bar_close = newest_bar_open + timedelta(minutes=bar_minutes)
    age_minutes = (now - bar_close).total_seconds() / 60.0

    return age_minutes > cfg.stale_bar_max_age_minutes
