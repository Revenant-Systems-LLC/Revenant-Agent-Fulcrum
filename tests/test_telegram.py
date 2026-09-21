"""Tests for the Telegram notifier.

Nothing here touches the network: a fake transport records what would have been sent.
The test that matters most is the one that proves the bot token never reaches a log line,
because the token lives in the address Telegram makes you call.
"""

from __future__ import annotations

import logging

import pytest

from fulcrum.creds import Credential
from fulcrum.telegram import (
    API_BASE,
    MAX_MESSAGE_CHARS,
    TRUNCATION_NOTE,
    TelegramNotifier,
    shorten,
)

EM_DASH = chr(0x2014)  # written this way so the character itself is not in this file
CHAT_ID = "987654321"
TOKEN = "111222:fulcrum-test-token-not-real"


class FakeTransport:
    """Records every call and answers with whatever the test asked for."""

    def __init__(self, status: int = 200, body: str = '{"ok":true}', raises: BaseException | None = None):
        self.status = status
        self.body = body
        self.raises = raises
        self.calls: list[tuple[str, dict, float]] = []

    def __call__(self, url: str, json_body: dict, timeout: float) -> tuple[int, str]:
        self.calls.append((url, json_body, timeout))
        if self.raises is not None:
            raise self.raises
        return self.status, self.body


def make_notifier(transport, **kw) -> TelegramNotifier:
    return TelegramNotifier(credential=Credential(user=CHAT_ID, secret=TOKEN), transport=transport, **kw)


# ---- address and body -------------------------------------------------------


def test_sends_to_the_right_address_with_the_right_body():
    t = FakeTransport()
    ok = make_notifier(t).send("Fulcrum rebalance done. Equity $100.00")

    assert ok is True
    assert len(t.calls) == 1
    url, body, timeout = t.calls[0]
    assert url == f"{API_BASE}/bot{TOKEN}/sendMessage"
    assert body == {"chat_id": CHAT_ID, "text": "Fulcrum rebalance done. Equity $100.00"}
    assert "parse_mode" not in body  # plain text only: no formatting to get wrong
    assert timeout == pytest.approx(10.0)


def test_chat_id_and_token_come_from_the_credential():
    t = FakeTransport()
    n = TelegramNotifier(credential=Credential(user="42", secret="abc:def"), transport=t)
    n.send("hello")
    url, body, _ = t.calls[0]
    assert body["chat_id"] == "42"
    assert url.endswith("/botabc:def/sendMessage")


@pytest.mark.parametrize("user,secret", [("", TOKEN), (CHAT_ID, "")])
def test_refuses_to_build_without_both_halves_of_the_credential(user, secret):
    with pytest.raises(ValueError):
        TelegramNotifier(credential=Credential(user=user, secret=secret), transport=FakeTransport())


# ---- failure behaviour ------------------------------------------------------


def test_status_500_returns_false_and_does_not_raise():
    t = FakeTransport(status=500, body="server error")
    assert make_notifier(t).send("anything") is False


def test_transport_blowing_up_returns_false():
    t = FakeTransport(raises=OSError("connection reset"))
    assert make_notifier(t).send("anything") is False


def test_house_style_violation_is_never_sent():
    t = FakeTransport()
    # An em dash is banned by notify.check_text, so the message must not go out at all.
    assert make_notifier(t).send(f"Fulcrum {EM_DASH} rebalance done") is False
    assert t.calls == []


# ---- the token must never be visible ---------------------------------------


def _all_log_text(caplog) -> str:
    parts = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.append(str(r.args))
        parts.append(str(getattr(r, "msg", "")))
    return "\n".join(parts)


@pytest.mark.parametrize(
    "make_transport",
    [
        lambda: FakeTransport(status=200),
        lambda: FakeTransport(status=500, body=f"bad token {TOKEN}"),
        lambda: FakeTransport(raises=RuntimeError(f"boom while calling with {TOKEN}")),
    ],
)
def test_token_never_appears_in_any_log_record(make_transport, caplog):
    caplog.set_level(logging.DEBUG, logger="fulcrum.telegram")
    make_notifier(make_transport()).send("Fulcrum status line")
    text = _all_log_text(caplog)
    assert TOKEN not in text          # not from us, and not echoed back from Telegram
    assert "api.telegram.org" not in text


def test_token_never_appears_in_repr_or_str_or_on_the_object():
    n = make_notifier(FakeTransport())
    assert TOKEN not in repr(n)
    assert TOKEN not in str(n)
    assert "redacted" in repr(n)
    # The address holding the token is built for one call and never kept on the object,
    # so nothing can print it later by accident.
    assert not any(TOKEN in str(v) for k, v in vars(n).items() if k != "_token")
    assert not any("api.telegram.org" in str(v) for v in vars(n).values())


def test_a_bad_message_logs_without_the_token(caplog):
    caplog.set_level(logging.DEBUG, logger="fulcrum.telegram")
    make_notifier(FakeTransport()).send(f"Fulcrum {EM_DASH} bad")
    assert TOKEN not in _all_log_text(caplog)


# ---- long messages ----------------------------------------------------------


def test_long_message_is_cut_short_with_a_note():
    t = FakeTransport()
    ok = make_notifier(t).send("a" * 5000)

    assert ok is True
    sent = t.calls[0][1]["text"]
    assert len(sent) == MAX_MESSAGE_CHARS
    assert sent.startswith("aaaa")
    assert sent.endswith(TRUNCATION_NOTE)


def test_message_at_the_limit_is_left_alone():
    t = FakeTransport()
    text = "b" * MAX_MESSAGE_CHARS
    make_notifier(t).send(text)
    assert t.calls[0][1]["text"] == text


def test_shorten_is_a_plain_function():
    assert shorten("short", limit=100) == "short"
    # A limit smaller than the note itself still comes back inside the limit.
    cut = shorten("x" * 50, limit=20)
    assert len(cut) == 20
    assert shorten("anything", limit=0) == ""
    # The normal case: text, then the note, never longer than the limit.
    normal = shorten("y" * 500, limit=200)
    assert len(normal) == 200
    assert normal.endswith(TRUNCATION_NOTE)


def test_custom_limit_is_honoured():
    t = FakeTransport()
    make_notifier(t, max_chars=200).send("c" * 1000)
    assert len(t.calls[0][1]["text"]) == 200
