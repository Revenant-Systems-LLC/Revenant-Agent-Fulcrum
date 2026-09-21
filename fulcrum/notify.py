"""Notifications: plain text, templated, never LLM-written. No em dashes, no emojis, no tables.

``TelegramNotifier`` is in ``telegram.py``. Everything here is transport-free.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

FORBIDDEN_CHARS = ("—", "–")  # em dash, en dash


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


def check_text(text: str) -> str:
    for ch in FORBIDDEN_CHARS:
        if ch in text:
            raise ValueError("notification text contains a dash character that is not allowed")
    if any(ord(c) > 0xFFFF for c in text):
        raise ValueError("notification text contains an emoji or other astral character")
    if "|" in text and "\n|" in text:
        raise ValueError("notification text looks like a table")
    return text


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def msg_rebalance(ts: datetime, equity: float, actions: list[str], vetoed: list[str], mode: str) -> str:
    lines = [f"Fulcrum rebalance {ts.strftime('%Y-%m-%d %H:%M UTC')} ({mode})", f"Equity {fmt_money(equity)}"]
    lines += actions if actions else ["No trades: all targets inside the no-trade band."]
    if vetoed:
        lines.append("Filter vetoed: " + ", ".join(vetoed))
    return check_text("\n".join(lines))


def msg_stop(ts: datetime, symbol: str, price: float, stop: float, qty: float) -> str:
    return check_text(f"Fulcrum STOP {symbol} at {fmt_money(price)} (stop {fmt_money(stop)}), selling {qty:.6f} units. "
                      f"{ts.strftime('%Y-%m-%d %H:%M UTC')}")


def msg_kill(ts: datetime, equity: float, hwm: float, drawdown: float) -> str:
    return check_text(
        f"Fulcrum KILL SWITCH TRIPPED {ts.strftime('%Y-%m-%d %H:%M UTC')}. Equity {fmt_money(equity)}, high-water "
        f"mark {fmt_money(hwm)}, drawdown {fmt_pct(drawdown)}. No new entries. Stops keep working. Only you can "
        f"reset this by creating the reset file by hand.")


def msg_alert(ts: datetime, kind: str, detail: str) -> str:
    return check_text(f"Fulcrum ALERT {kind} {ts.strftime('%Y-%m-%d %H:%M UTC')}: {detail}")


class FakeNotifier:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> bool:
        check_text(text)
        if self.fail:
            return False
        self.sent.append(text)
        return True


class NullNotifier:
    def send(self, text: str) -> bool:
        check_text(text)
        return True
