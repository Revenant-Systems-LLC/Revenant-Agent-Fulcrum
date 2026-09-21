"""Telegram messages: the only way Fulcrum talks to Dave while it is running.

Plain English, plain text, no formatting codes. The text is written by ``notify.py``
templates, never by a model, and it is checked by ``notify.check_text`` before it leaves
the machine.

Three rules this file exists to keep:

1. The bot token is a secret. It sits in the URL Telegram requires, so the URL is built
   inside ``send``, used once, and never logged, stored on the object, or put in an
   exception message. Nothing here prints the token, and ``repr`` of the notifier hides it.
2. ``send`` never raises. A notifier that crashes the trading loop would be worse than a
   missed message. Anything that goes wrong returns False and writes one plain log line.
3. Telegram refuses very long messages, so a long message is cut short with a note saying
   so instead of failing.

The network call itself is a small injected callable, so tests run offline:

    transport(url, json_body, timeout_seconds) -> (status_code, body_text)
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .creds import Credential
from .notify import check_text

log = logging.getLogger("fulcrum.telegram")

# Telegram's own limit is 4096 characters. 4000 leaves room for the note below.
MAX_MESSAGE_CHARS = 4000
TRUNCATION_NOTE = "\n(Message cut short here because it was too long to send.)"
API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0

# (url, json_body, timeout_seconds) -> (status_code, body_text)
Transport = Callable[[str, dict, float], "tuple[int, str]"]


def requests_transport(url: str, json_body: dict, timeout: float) -> tuple[int, str]:
    """The real network call. Imported lazily so tests never need the requests package.

    The URL holds the bot token, so nothing in here logs the URL, and the body Telegram
    sends back is returned to the caller but never written to a log by this module.
    """
    import requests

    response = requests.post(url, json=json_body, timeout=timeout)
    return int(response.status_code), str(response.text)[:500]


def shorten(text: str, limit: int = MAX_MESSAGE_CHARS, note: str = TRUNCATION_NOTE) -> str:
    """Return text no longer than ``limit`` characters, ending with a note if it was cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(note))
    return text[:keep] + note[: limit - keep]


class TelegramNotifier:
    """Sends one plain text message to one Telegram chat.

    ``credential`` carries the chat id as ``user`` and the bot token as ``secret``, loaded
    from the DPAPI file by ``creds.load_credential``. ``transport`` is the network call and
    is injectable so tests can watch what would have been sent without sending anything.
    """

    def __init__(
        self,
        *,
        credential: Credential,
        transport: Transport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_chars: int = MAX_MESSAGE_CHARS,
    ) -> None:
        chat_id = str(credential.user or "").strip()
        token = str(credential.secret or "").strip()
        if not chat_id:
            raise ValueError("Telegram credential is missing the chat id")
        if not token:
            raise ValueError("Telegram credential is missing the bot token")
        self._chat_id = chat_id
        self._token = token
        self._transport: Transport = transport or requests_transport
        self.timeout_seconds = float(timeout_seconds)
        self.max_chars = int(max_chars)

    def __repr__(self) -> str:  # never leak the token in a traceback or a log line
        return "TelegramNotifier(chat_id=<set>, token=<redacted>)"

    __str__ = __repr__

    def send(self, text: str) -> bool:
        """Send one message. Returns True if Telegram accepted it, False for anything else.

        This never raises. If it returns False, the message did not go out and one line in
        the log says why, without the token and without the address it was sent to.
        """
        try:
            clean = check_text(text)
        except Exception as e:  # noqa: BLE001  a bad message is our bug, not a reason to crash
            log.warning("Message not sent because it failed the house style check: %s", e)
            return False

        body: dict[str, Any] = {"chat_id": self._chat_id, "text": shorten(clean, self.max_chars)}
        url = f"{API_BASE}/bot{self._token}/sendMessage"

        try:
            status, _ = self._transport(url, body, self.timeout_seconds)
        except Exception as e:  # noqa: BLE001  any transport error: say so and carry on
            log.warning("Telegram message not sent: the call failed (%s).", type(e).__name__)
            return False

        if int(status) == 200:
            log.debug("Telegram message sent.")
            return True
        log.warning("Telegram did not accept the message. It answered with status %s.", status)
        return False


def from_credential(credential: Credential, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> TelegramNotifier:
    """Build a notifier that really talks to Telegram. Used by the CLI, never by tests."""
    return TelegramNotifier(credential=credential, transport=requests_transport, timeout_seconds=timeout_seconds)
