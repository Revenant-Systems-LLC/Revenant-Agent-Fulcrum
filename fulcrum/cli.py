"""The command line: the only way a person starts Fulcrum.

    python -m fulcrum selftest        check every part, send nothing anywhere
    python -m fulcrum run --once      one decision round
    python -m fulcrum run             the loop, until Ctrl and C
    python -m fulcrum status          what it holds right now
    python -m fulcrum report          how it has done so far
    python -m fulcrum backtest        run the strategy over past years

Every command takes ``--config PATH``. Every screen is plain English (decision D-013):
short sentences, no jargon without a gloss, no tables.

Three habits this file exists to keep:

1. Nothing real is built inside a command. Every outside thing (the ledger, the broker,
   the credentials, the filter, the notifier, the clock, the sleeper, where text goes)
   arrives through the ``Deps`` factory, so tests hand the same commands fakes and no
   test can reach Alpaca, Telegram or Anthropic.
2. A person never sees a traceback. Anything unexpected becomes one plain sentence plus
   a pointer to the log file. The traceback goes to the rotating log under ``data/``,
   which is git ignored, on a logger of its own that does not print to the screen.
3. No credential value is read, printed, logged or embedded anywhere. ``selftest``
   reports only whether each credential file is there and whether it could be unlocked.
   Not its value, not its length, not any part of it.

Exit codes: 0 when the command did what it said, 1 when something needs the person to act.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import logging
import logging.handlers
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from . import claude_filter as cf
from . import notify as ntf
from . import report as rpt
from . import risk as rk
from .broker import ALPACA_PAPER_URL, FakeBroker
from .config import Config, ConfigError, example_path, load_config
from .creds import CRED_FILES, CredentialError, Credential, cred_path, load_credential
from .engine import Engine
from .ledger import Ledger
from .llm_filter import FilterValidationError, OffFilter, parse_verdicts
from .models import Candidate, MarketContext, OrderRequest, PortfolioState, Position, utcnow
from .notify import NullNotifier
from .scheduler import STOPPED_MESSAGE, Scheduler

# The sentence the guard prints. The wording is fixed; only where the file lands is settable.
LOG_SENTENCE = "Details were written to data/fulcrum.log"
LOG_RELATIVE_PATH = "data/fulcrum.log"

# The log format carries a time, a level, a module name and our own message text.
# It never carries a request header, a URL or a credential value, because nothing in
# this program ever puts one of those in a log message.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DETAILS_LOGGER = "fulcrum.details"

PASS_LINE = "All checks passed. Nothing was sent anywhere."
FAIL_LINE = "Some checks failed. Nothing was sent anywhere."
DRY_RUN_DONE = "Practice cycle finished. No orders left this machine."
PAPER_DONE = "Paper cycle finished. Orders went to your Alpaca paper account."

PREVIEW_MAX_LINES = 40
DRY_RUN_PRETEND_DOLLARS = 100_000.0
BARS_HISTORY_BUFFER_DAYS = 45

log = logging.getLogger("fulcrum.cli")


# ---------------------------------------------------------------------------
# paths, ids, logging
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """The folder that holds the fulcrum package, config.example.toml and docs."""
    return Path(__file__).resolve().parent.parent


def resolve_path(cfg_or_none: Config | None, raw: str) -> Path:
    """Expand a config path. Relative paths hang off the repo root, not the shell's folder."""
    p = Path(raw).expanduser() if cfg_or_none is None else cfg_or_none.expanded(raw)
    return p if p.is_absolute() else (repo_root() / p)


def make_run_id(now: datetime, suffix: str | None = None) -> str:
    """The start time in compact ISO plus a short random tail, for example 20260921T120304Z-4f2a9c.

    The tail keeps two runs started in the same second apart. It is never used for an
    order id, so it may be random; see ``make_scope`` for the part that must not be.
    """
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    tail = suffix if suffix is not None else secrets.token_hex(3)
    return f"{stamp}-{tail}"


def make_scope(cfg: Config) -> str:
    """The namespace for client order ids: the run mode plus a hash of the ledger path.

    This must be the same on every start with the same settings, or a crash and restart
    would build different order ids for the same decision and could double submit. So it
    holds nothing that changes run to run: no time, no random tail, no run id. Two modes
    or two ledger files can never produce the same id, so a practice order id and a paper
    order id can never collide.
    """
    ledger_key = str(resolve_path(cfg, cfg.run.ledger_path)).lower()
    return f"{cfg.run.mode}-{hashlib.sha1(ledger_key.encode()).hexdigest()[:12]}"


def setup_logging(level: str, log_path: Path) -> None:
    """Send ordinary logging to stderr at the configured level, and tracebacks to a file.

    The file handler sits on its own logger with ``propagate`` off, so a traceback written
    for the record never reaches the screen. A person gets sentences; the file gets detail.
    """
    package = logging.getLogger("fulcrum")
    package.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not any(getattr(h, "_fulcrum_stderr", False) for h in package.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler._fulcrum_stderr = True  # type: ignore[attr-defined]
        package.addHandler(handler)

    details = logging.getLogger(DETAILS_LOGGER)
    details.setLevel(logging.ERROR)
    details.propagate = False
    if not details.handlers:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            details.addHandler(file_handler)
        except OSError:
            # A log file we cannot open must not stop the program. The sentence the guard
            # prints still tells the person where to look; there will be nothing there.
            details.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# the pieces a command needs, and how the real ones are built
# ---------------------------------------------------------------------------


@dataclass
class Stack:
    """The working parts of one run, already built."""

    broker: Any
    filt: Any
    notifier: Any
    bars_provider: Callable[[Sequence[str], datetime], dict]
    price_provider: Callable[[Sequence[str]], dict]
    notes: list[str] = field(default_factory=list)


def print_line(text: str) -> None:
    print(text)


def default_open_ledger(cfg: Config) -> Ledger:
    return Ledger(resolve_path(cfg, cfg.run.ledger_path))


def default_load_credential(cfg: Config, service: str) -> Credential:
    return load_credential(cfg.run.creds_dir, service)


def _bars_provider(data: Any, cfg: Config, seed_broker: Any | None):
    """Turn the market data reader into what the scheduler wants: symbols in, frames out.

    In practice mode the pretend broker has no prices of its own, so the newest close of
    each symbol is pushed into it here. That is the one place real prices meet pretend fills.
    """
    from . import data as data_mod

    lookback_days = cfg.signal.min_history_days + BARS_HISTORY_BUFFER_DAYS

    def provider(symbols: Sequence[str], now: datetime) -> dict:
        wanted = list(symbols)
        if not wanted:
            return {}
        raw = data.bars(wanted, start=now - timedelta(days=lookback_days))
        frames = {}
        for symbol in wanted:
            rows = raw.get(symbol) or []
            if rows:
                frames[symbol] = data_mod.to_frame(rows)
        if seed_broker is not None:
            latest = {s: float(f["close"].iloc[-1]) for s, f in frames.items() if len(f)}
            if latest:
                seed_broker.set_prices(latest, now)
        return frames

    return provider


def _price_provider(broker: Any, data: Any | None = None):
    """Last price per symbol for the 15-minute stop check. A symbol the venue cannot price is
    left out, not guessed.

    In practice mode the pretend broker has no live prices of its own, so the latest printed
    trades are fetched from the market data reader (one call for the whole book) and pushed
    into it, so stops fire on current prices rather than on yesterday's close.
    """

    def provider(symbols: Sequence[str]) -> dict:
        out: dict[str, float] = {}
        wanted = list(symbols)
        if data is not None:
            try:
                quotes = data.latest_prices(wanted)
            except Exception as e:  # noqa: BLE001  a failed price check skips this tick, never crashes it
                log.warning("Could not fetch current prices (%s); stops are checked next tick.", type(e).__name__)
                return out
            for symbol, q in quotes.items():
                out[symbol] = float(q.price)
            if out and hasattr(broker, "set_prices"):
                broker.set_prices(out)
            return out
        for symbol in wanted:
            try:
                out[symbol] = float(broker.latest_price(symbol).price)
            except Exception as e:  # noqa: BLE001  one missing price must not end the check
                log.debug("No price for %s (%s).", symbol, type(e).__name__)
        return out

    return provider


def _build_filter(cfg: Config, deps: "Deps", notes: list[str]) -> Any:
    if cfg.llm_filter.mode == "off":
        return OffFilter()
    credential = deps.load_credential(cfg, "anthropic")
    return cf.ClaudeFilter(
        mode=cfg.llm_filter.mode,
        model=cfg.llm_filter.model,
        client=cf.make_client(credential.secret),
        timeout_seconds=cfg.llm_filter.timeout_seconds,
        max_tokens=cfg.llm_filter.max_tokens,
        effort=cfg.llm_filter.effort,
    )


def _build_notifier(cfg: Config, deps: "Deps", notes: list[str]) -> Any:
    from . import telegram as tg

    if not cfg.notify.enabled:
        notes.append("Messages to your phone are switched off in the settings file.")
        return NullNotifier()
    try:
        credential = deps.load_credential(cfg, "telegram")
    except CredentialError:
        notes.append(
            "No Telegram credential was found, so nothing will be sent to your phone. "
            "Everything is still written to the ledger. See docs/HANDOFF.md step 2."
        )
        return NullNotifier()
    return tg.from_credential(credential, timeout_seconds=30.0)


def default_build_stack(cfg: Config, *, dry_run: bool, deps: "Deps") -> Stack:
    """Build the real working parts. Never called by a test.

    Practice mode: a pretend broker seeded with pretend dollars, real prices read from
    Alpaca's market data (a read only GET), the filter the settings ask for, and Telegram
    if its credential is there.

    Paper mode: the Alpaca paper broker, which refuses any address but the paper one.
    """
    from .alpaca import AlpacaBroker, AlpacaData, RequestsTransport

    notes: list[str] = []
    credential = deps.load_credential(cfg, "alpaca")
    data = AlpacaData(credential=credential, transport=RequestsTransport())

    if dry_run:
        broker: Any = FakeBroker(
            starting_cash=DRY_RUN_PRETEND_DOLLARS,
            fee_bps=cfg.asset_class.taker_fee_bps,
            slippage_bps=cfg.asset_class.slippage_bps,
            min_notional_usd=cfg.asset_class.min_notional_usd,
        )
        notes.append(
            f"Practice mode. The pretend account starts with "
            f"{ntf.fmt_money(DRY_RUN_PRETEND_DOLLARS)} and no order leaves this machine."
        )
    else:
        broker = AlpacaBroker(
            base_url=ALPACA_PAPER_URL,
            credential=credential,
            transport=RequestsTransport(),
            asset_class=cfg.asset_class,
        )
        notes.append("Paper mode. Orders go to your Alpaca paper account.")

    filt = _build_filter(cfg, deps, notes)
    notifier = _build_notifier(cfg, deps, notes)
    return Stack(
        broker=broker,
        filt=filt,
        notifier=notifier,
        bars_provider=_bars_provider(data, cfg, broker if dry_run else None),
        price_provider=_price_provider(broker, data if dry_run else None),
        notes=notes,
    )


@dataclass
class Deps:
    """The factory every command builds through. Tests pass their own callables.

    Nothing in here is looked up at call time from a module global, so overriding one
    field is enough to keep a test offline.
    """

    open_ledger: Callable[[Config], Any] = default_open_ledger
    load_credential: Callable[[Config, str], Credential] = default_load_credential
    build_stack: Callable[..., Stack] = default_build_stack
    make_history_source: Callable[[Config], Any] = None  # type: ignore[assignment]
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = print_line

    def __post_init__(self) -> None:
        # default_history_source is defined below the backtest section, so it is wired
        # here rather than as a field default.
        if self.make_history_source is None:
            self.make_history_source = default_history_source


# ---------------------------------------------------------------------------
# the loop, with a line after every check
# ---------------------------------------------------------------------------


def describe_check(now: datetime, tick: Any, engine: Any) -> str:
    """The line the loop prints every fifteen minutes.

    Matches docs/HANDOFF.md step 7 word for word:
    "Checked at 14:15 UTC. Holding 3 coins. No stops hit. Safety brake off."
    """
    try:
        held = len([p for p in engine.broker.positions() if p.qty > 0])
    except Exception as e:  # noqa: BLE001  a broker hiccup must not silence the line
        log.debug("Could not count holdings (%s).", type(e).__name__)
        held = 0
    if held == 0:
        holding = "Holding no coins."
    elif held == 1:
        holding = "Holding 1 coin."
    else:
        holding = f"Holding {held} coins."

    fired = list(getattr(tick, "stops_fired", ()) or ())
    stops = "No stops hit." if not fired else "Stops hit: " + ", ".join(fired) + "."

    try:
        brake = "Safety brake ON." if engine.kill.tripped() else "Safety brake off."
    except Exception as e:  # noqa: BLE001
        log.debug("Could not read the safety brake (%s).", type(e).__name__)
        brake = "Safety brake state unknown."

    return f"Checked at {now.strftime('%H:%M')} UTC. {holding} {stops} {brake}"


class PrintingScheduler(Scheduler):
    """The scheduler, plus one plain line per check so the window is never silent.

    It adds printing and nothing else. Every scheduling decision still belongs to
    ``scheduler.py``; each override here calls the original first.
    """

    def __init__(self, *args: Any, out: Callable[[str], None] = print_line, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.out = out

    def _do_tick(self, now: datetime, result: Any) -> None:
        prices = self.price_provider(self.held_symbols())
        tick = self.engine.tick(prices, now)
        result.ticks += 1
        self.out(describe_check(now, tick, self.engine))

    def _maybe_rebalance(self, now: datetime, result: Any, halved: bool) -> None:
        before = result.rebalances
        super()._maybe_rebalance(now, result, halved)
        if result.rebalances > before:
            self.out(f"Weekly decision round done at {now.strftime('%H:%M')} UTC.")

    def _maybe_shadow_daily(self, now: datetime, result: Any) -> None:
        before = result.shadow_runs
        super()._maybe_shadow_daily(now, result)
        if result.shadow_runs > before:
            self.out(
                f"Asked the AI filter today's practice question at {now.strftime('%H:%M')} UTC. "
                "Nothing was traded."
            )


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"{self.label}: ok" if self.ok else f"{self.label}: {self.detail}"


def _check_credentials(cfg: Config, deps: Deps) -> list[Check]:
    """Is each credential file there, and can this Windows login unlock it?

    Presence and readability only. No value is read into a message, no length is
    measured, nothing is written to the log. A failure names the file, never its contents.
    """
    out: list[Check] = []
    names = {"alpaca": "Alpaca", "anthropic": "Anthropic", "telegram": "Telegram"}
    for service, pretty in names.items():
        label = f"{pretty} credential file"
        try:
            path = cred_path(cfg.run.creds_dir, service)
        except CredentialError as e:
            out.append(Check(label, False, f"{e}."))
            continue
        if not path.exists():
            out.append(Check(
                label,
                False,
                f"the file {CRED_FILES[service]} was not found in {path.parent}. "
                "Follow docs/HANDOFF.md step 2 to create it.",
            ))
            continue
        try:
            deps.load_credential(cfg, service)
        except CredentialError:
            out.append(Check(
                label,
                False,
                f"the file {CRED_FILES[service]} is there but could not be unlocked. "
                "It has to be created by the same Windows login that runs the bot. "
                "Follow docs/HANDOFF.md step 2 to make it again.",
            ))
            continue
        except Exception as e:  # noqa: BLE001  never let a credential error carry a value
            out.append(Check(label, False, f"reading it failed ({type(e).__name__})."))
            continue
        out.append(Check(label, True))
    return out


def _check_fake_broker(cfg: Config) -> Check:
    label = "Pretend broker buys and sells"
    try:
        broker = FakeBroker(
            starting_cash=1000.0,
            fee_bps=cfg.asset_class.taker_fee_bps,
            slippage_bps=cfg.asset_class.slippage_bps,
            min_notional_usd=0.0,
        )
        symbol = cfg.universe.core[0]
        broker.set_price(symbol, 100.0)
        broker.submit(OrderRequest(
            client_order_id="selftest-buy", decision_id="selftest", symbol=symbol,
            side="buy", purpose="entry", notional_usd=500.0,
        ))
        held = {p.symbol: p.qty for p in broker.positions()}
        if symbol not in held or held[symbol] <= 0:
            return Check(label, False, "the pretend buy did not create a position.")
        broker.submit(OrderRequest(
            client_order_id="selftest-sell", decision_id="selftest", symbol=symbol,
            side="sell", purpose="exit", qty=held[symbol],
        ))
        if broker.positions():
            return Check(label, False, "the pretend sale did not close the position.")
        if broker.total_fees <= 0:
            return Check(label, False, "no fee was charged on the pretend trades.")
    except Exception as e:  # noqa: BLE001
        return Check(label, False, f"the round trip failed ({type(e).__name__}).")
    return Check(label, True)


def _check_risk_gate(cfg: Config, now: datetime) -> Check:
    """The safety brake blocks a buy and never blocks a sale. Worked out in memory only.

    Nothing here opens or writes to the ledger: ``check_order`` is a pure function and the
    account picture below is made up on the spot.
    """
    label = "Safety brake blocks buys and never blocks sales"
    try:
        symbol = cfg.universe.core[0]
        state = PortfolioState(
            as_of=now,
            equity=100_000.0,
            cash=100_000.0,
            positions={symbol: Position(symbol, 1.0, 1000.0, 1000.0)},
            trades_last_24h=0,
            high_water_mark=100_000.0,
            prices={symbol: 1000.0},
        )
        entry = OrderRequest(
            client_order_id="selftest-entry", decision_id="selftest", symbol=symbol,
            side="buy", purpose="entry", notional_usd=1000.0,
        )
        leave = OrderRequest(
            client_order_id="selftest-exit", decision_id="selftest", symbol=symbol,
            side="sell", purpose="exit", qty=1.0,
        )
        blocked = rk.check_order(entry, state, cfg.risk, cfg.sizing, cfg.asset_class, True)
        allowed = rk.check_order(leave, state, cfg.risk, cfg.sizing, cfg.asset_class, True)
        if blocked.kind != "reject":
            return Check(label, False, "a buy got through while the brake was on.")
        if allowed.kind != "approve":
            return Check(label, False, "a sale was blocked while the brake was on.")
    except Exception as e:  # noqa: BLE001
        return Check(label, False, f"the check failed ({type(e).__name__}).")
    return Check(label, True)


def _check_filter_contract(cfg: Config) -> Check:
    label = "AI filter answer checker rejects a bad answer"
    symbol = cfg.universe.core[0]
    try:
        try:
            parse_verdicts("this is not json", {symbol})
        except FilterValidationError:
            pass
        else:
            return Check(label, False, "a broken answer was accepted when it should not be.")
        good = (
            '{"verdicts": [{"symbol": "%s", "verdict": "allow", "reason": "no reason to stop",'
            ' "confidence": 0.5, "invalidation": "n/a"}]}' % symbol
        )
        verdicts = parse_verdicts(good, {symbol})
        if len(verdicts) != 1 or verdicts[0].verdict != "allow":
            return Check(label, False, "a good answer was not read back correctly.")
    except Exception as e:  # noqa: BLE001
        return Check(label, False, f"the check failed ({type(e).__name__}).")
    return Check(label, True)


def _check_paper_lock(cfg: Config) -> Check:
    """The broker must refuse any address that is not Alpaca's practice one.

    The address used here is a made up one that goes nowhere. The real trading address is
    deliberately absent from this file (decision D-009); the lock is what is being tested,
    not the string.
    """
    label = "Broker refuses any address but the practice one"
    from .alpaca import AlpacaBroker, PaperOnlyViolation

    try:
        AlpacaBroker(
            base_url="https://example.invalid",
            credential=Credential(user="unused", secret="unused"),
            transport=None,
            asset_class=cfg.asset_class,
        )
    except PaperOnlyViolation:
        return Check(label, True)
    except Exception as e:  # noqa: BLE001
        return Check(label, False, f"it refused for the wrong reason ({type(e).__name__}).")
    return Check(label, False, "it accepted an address it should have refused.")


def _check_message_templates(now: datetime, cfg: Config) -> Check:
    label = "Phone message wording passes the house style check"
    symbol = cfg.universe.core[0]
    try:
        ntf.msg_rebalance(now, 100_000.0, [f"Bought 0.010000 of {symbol}"], [], cfg.run.mode)
        ntf.msg_stop(now, symbol, 900.0, 950.0, 0.5)
        ntf.msg_kill(now, 85_000.0, 100_000.0, 0.15)
        ntf.msg_alert(now, "fee_budget", "trading fees are above the yearly budget")
    except Exception as e:  # noqa: BLE001
        return Check(label, False, f"a message failed the check ({type(e).__name__}).")
    return Check(label, True)


def _check_secret_scan_hook(repo_root: Path | None = None) -> Check:
    """The pre-commit secret scan is a plain git hook that a fresh clone does not carry.
    Nothing else would notice it missing, so the setup check does (security review)."""
    label = "Secret scan runs before every commit"
    root = repo_root or Path(__file__).resolve().parent.parent
    hook = root / ".git" / "hooks" / "pre-commit"
    if not (root / ".git").exists():
        return Check(label, True, "this folder is not a git checkout, so there is nothing to commit.")
    if not hook.exists():
        return Check(label, False, r"the hook is not installed. Run scripts\Install-Hooks.ps1 once, then check again.")
    try:
        text = hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Check(label, False, "the hook file could not be read.")
    if "detect-secrets" not in text:
        return Check(label, False, r"the installed hook is not the secret scan. Run scripts\Install-Hooks.ps1.")
    return Check(label, True)


def _filter_preview_lines(cfg: Config, now: datetime) -> list[str]:
    """What would be sent to the AI filter, shortened so the screen stays readable."""
    symbol = cfg.universe.core[0]
    candidate = Candidate(
        symbol=symbol, conviction=0.75, price=1000.0, realized_vol=0.55, atr=25.0,
        target_weight=0.10, current_weight=0.0,
    )
    context = MarketContext(
        as_of=now,
        summary="1 of 3 coins look like buys. This is a check only. Nothing is being traded.",
    )
    preview = cf.request_preview([candidate], context)
    lines = preview.splitlines()
    out = lines[:PREVIEW_MAX_LINES]
    if len(lines) > PREVIEW_MAX_LINES:
        out.append(
            f"(Shown to the first {PREVIEW_MAX_LINES} lines of {len(lines)}. "
            "The rest is more of the same.)"
        )
    return out


def cmd_selftest(cfg: Config, deps: Deps, config_note: str | None = None) -> int:
    """Run every check that can be run without talking to anybody. Nothing is sent."""
    out = deps.out
    now = deps.clock()
    checks: list[Check] = []

    out("Checking Fulcrum. Nothing in this check is sent to Alpaca, Telegram or the AI.")
    out("")
    if config_note:
        out(config_note)
        out("")

    checks.append(Check("Settings file reads and every value is sensible", True))

    label = "Ledger file opens and its layout version matches this program"
    ledger = None
    try:
        # The ledger refuses to open on a version it does not know, so opening it is the
        # version check. A count of its tables proves the layout is really there.
        ledger = deps.open_ledger(cfg)
        counts = ledger.table_counts()
        if "schema_version" not in counts:
            checks.append(Check(label, False, "the ledger opened but its layout is incomplete."))
        else:
            checks.append(Check(label, True))
    except Exception as e:  # noqa: BLE001
        checks.append(Check(
            label,
            False,
            f"it could not be opened ({type(e).__name__}). If this file came from an older "
            "version of Fulcrum, tell me before running anything else.",
        ))
    finally:
        if ledger is not None:
            try:
                ledger.close()
            except Exception as e:  # noqa: BLE001
                log.debug("Could not close the ledger (%s).", type(e).__name__)

    checks.extend(_check_credentials(cfg, deps))
    checks.append(_check_fake_broker(cfg))
    checks.append(_check_risk_gate(cfg, now))
    checks.append(_check_filter_contract(cfg))
    checks.append(_check_paper_lock(cfg))
    checks.append(_check_message_templates(now, cfg))
    checks.append(_check_secret_scan_hook())

    for check in checks:
        out(check.line())

    out("")
    out("This is what would be sent to the AI filter if it were asked. It was not sent.")
    for line in _filter_preview_lines(cfg, now):
        out(line)
    out("")

    failures = [c for c in checks if not c.ok]
    if failures:
        out("These checks did not pass:")
        for check in failures:
            out(f"  {check.label}: {check.detail}")
        out(FAIL_LINE)
        return 1
    out(PASS_LINE)
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def build_engine(cfg: Config, deps: Deps, stack: Stack, ledger: Any, now: datetime) -> Engine:
    run_id = make_run_id(now)
    scope = make_scope(cfg)
    ledger.start_run(run_id, cfg.run.mode, cfg.config_hash, None, now)
    return Engine(
        cfg=cfg, broker=stack.broker, ledger=ledger, filt=stack.filt,
        notifier=stack.notifier, run_id=run_id, scope=scope,
    )


def cmd_run(cfg: Config, deps: Deps, *, once: bool, dry_run_flag: bool,
            config_note: str | None = None) -> int:
    """One decision round, or the loop that keeps making them."""
    out = deps.out
    dry_run = bool(dry_run_flag) or cfg.run.mode == "dry_run"
    now = deps.clock()

    if config_note:
        out(config_note)
    stack = deps.build_stack(cfg, dry_run=dry_run, deps=deps)
    for note in stack.notes:
        out(note)

    ledger = deps.open_ledger(cfg)
    try:
        engine = build_engine(cfg, deps, stack, ledger, now)
        scheduler = PrintingScheduler(
            cfg, engine, stack.bars_provider, stack.price_provider,
            sleep=deps.sleep, clock=deps.clock, out=out,
        )
        if once:
            result = scheduler.run_once(dry_run=dry_run)
            for line in result.lines:
                out(line)
            out(DRY_RUN_DONE if dry_run else PAPER_DONE)
            return 0

        out(
            f"Fulcrum is running. It checks every {cfg.schedule.tick_minutes} minutes and "
            "makes its decision round once a week. Press Ctrl and C to stop it."
        )
        try:
            loop = scheduler.run_forever()
            message = loop.message
        except KeyboardInterrupt:
            message = STOPPED_MESSAGE
        out(message)
        return 0
    finally:
        try:
            ledger.close()
        except Exception as e:  # noqa: BLE001
            log.debug("Could not close the ledger (%s).", type(e).__name__)


# ---------------------------------------------------------------------------
# status and report
# ---------------------------------------------------------------------------


def cmd_status(cfg: Config, deps: Deps, config_note: str | None = None) -> int:
    ledger = deps.open_ledger(cfg)
    try:
        if config_note:
            deps.out(config_note)
        deps.out(rpt.render_status(ledger, cfg).rstrip("\n"))
        return 0
    finally:
        try:
            ledger.close()
        except Exception as e:  # noqa: BLE001
            log.debug("Could not close the ledger (%s).", type(e).__name__)


def cmd_report(cfg: Config, deps: Deps, config_note: str | None = None) -> int:
    ledger = deps.open_ledger(cfg)
    try:
        if config_note:
            deps.out(config_note)
        measured = rpt.metrics(ledger, cfg.asset_class.trading_days_per_year)
        text = rpt.render_plain(
            measured, rpt.filter_summary(ledger), rpt.ghost_summary(ledger), cfg.llm_filter.mode
        )
        deps.out(text.rstrip("\n"))
        return 0
    finally:
        try:
            ledger.close()
        except Exception as e:  # noqa: BLE001
            log.debug("Could not close the ledger (%s).", type(e).__name__)


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

BACKTEST_MODULE = "fulcrum.backtest"
BACKTEST_MISSING = (
    "The history test is not built into this copy of Fulcrum yet, so there is nothing to "
    "run. Every other command still works."
)
BACKTEST_LEDGER = "data/backtest.sqlite"

# The three periods decision D-012 named, so "run the folds" always means the same thing.
FOLD_PERIODS = (
    ("stress", "2026-02-01", "2026-03-31", "the February 2026 crash and the March pullback"),
    ("trending", "2025-04-07", "2025-10-06", "the long climb to the record high"),
    ("choppy", "2023-08-01", "2023-10-15", "a flat, directionless stretch"),
)
DEFAULT_BACKTEST_START = "2023-01-01"

BENCHMARK_WORDS = {
    "benchmark_btc": "just holding Bitcoin",
    "benchmark_equal": "holding an equal slice of every coin",
    "benchmark_cash": "leaving the money in cash",
}

BACKTEST_CAVEATS = (
    "Read this with three things in mind. The coin list is today's Alpaca listing, so "
    "coins that failed and were removed are not in it and the result flatters itself. "
    "The sample is short. Trend following in crypto has a history of sudden sharp "
    "losses that a calm stretch will not show you.",
)


def _parse_day(value: str, label: str):
    from datetime import date as date_type

    try:
        return date_type.fromisoformat(value)
    except ValueError:
        raise ConfigError(
            f"{label} has to be written as a date like 2024-01-31, and {value!r} is not"
        ) from None


def default_history_source(cfg: Config) -> Any:
    """The public price archive the history test reads. A key free, read only GET."""
    from .data import BinanceVisionKlines, CoinbaseCandles, RequestsGet

    http = RequestsGet()
    if cfg.data.historical_source == "coinbase":
        return CoinbaseCandles(http)
    if cfg.data.historical_source == "binance_vision":
        return BinanceVisionKlines(http)
    raise ConfigError(
        f"[data].historical_source is {cfg.data.historical_source!r}, which the history "
        "test cannot read from. Set it to coinbase or binance_vision"
    )


def _score_line(name: str, measured: Any) -> str:
    """One plain sentence about one run of the history test."""
    bits = []
    if measured.total_return is not None:
        bits.append(f"changed the account by {ntf.fmt_pct(measured.total_return)}")
    bits.append(f"with a worst fall of {ntf.fmt_pct(measured.max_drawdown)}")
    if measured.sharpe is not None:
        bits.append(f"and a return per unit of risk of {measured.sharpe:.2f}")
    else:
        bits.append("and too little daily history to score the risk")
    return f"{name} {', '.join(bits)}."


def render_backtest_verdict(results: dict) -> list[str]:
    """Turn the history test's numbers into the paragraph a non programmer reads."""
    lines: list[str] = []
    strategy = results.get("strategy")
    if strategy is None:
        return ["The history test ran but returned no result for the strategy itself."]

    measured = strategy.metrics
    lines.append("How the rules did")
    lines.append(_score_line("Over this period the rules", measured))
    if measured.fee_drag_pct_equity is not None:
        lines.append(
            f"Trading fees cost {ntf.fmt_pct(measured.fee_drag_pct_equity)} of the account "
            f"a year at this pace. The alarm level is 3%."
        )
    lines.append(
        f"Orders filled: {measured.fills}. Completed round trips: {measured.round_trips}."
    )

    benchmarks = [(k, v) for k, v in results.items() if k in BENCHMARK_WORDS]
    if benchmarks:
        lines.append("")
        lines.append("Compared with doing something simpler")
        for key, result in benchmarks:
            lines.append(_score_line(f"Over the same period, {BENCHMARK_WORDS[key]}", result.metrics))

    lines.append("")
    lines.append("What this means")
    btc = results.get("benchmark_btc")
    if measured.sharpe is None:
        lines.append(
            "There is not enough daily history here to say whether the returns were worth "
            "the risk. That is a fact about the sample, not a verdict on the rules."
        )
    elif measured.sharpe < 0.5:
        lines.append(
            f"Return per unit of risk is {measured.sharpe:.2f}, below the 0.5 this project "
            "set as the line between a strategy worth keeping and one that is not. On this "
            "evidence the strategy has not earned anything. The engine still works; the "
            "strategy is the part that did not."
        )
    elif btc is not None and btc.metrics.sharpe is not None and measured.sharpe < btc.metrics.sharpe:
        lines.append(
            f"Return per unit of risk is {measured.sharpe:.2f}, which is below the "
            f"{btc.metrics.sharpe:.2f} you would have got by simply holding Bitcoin and "
            "doing nothing. Doing nothing was better."
        )
    else:
        lines.append(
            f"Return per unit of risk is {measured.sharpe:.2f}. That is one number over one "
            "stretch of the past. It is not a promise about the future."
        )
    missing = getattr(strategy, "missing_dates", None) or {}
    if any(missing.values()):
        names = ", ".join(sorted(k for k, v in missing.items() if v))
        lines.append(
            f"Some days of price history were absent for {names}. They were left absent, "
            "never invented, so the result covers slightly less time than it looks like."
        )
    lines.extend(BACKTEST_CAVEATS)
    return lines


def render_fold_verdict(results: dict) -> list[str]:
    """The walk forward version: one paragraph per named period, then the caveats."""
    lines = ["How the rules did in each kind of market"]
    descriptions = {name: text for name, _, _, text in FOLD_PERIODS}
    for name, result in results.items():
        lines.append("")
        lines.append(f"{name.capitalize()}: {descriptions.get(name, 'a period of the past')}.")
        lines.append(_score_line("The rules", result.metrics))
    lines.append("")
    lines.append("What this means")
    lines.append(
        "The same settings were used in every period. Nothing was tuned between them, "
        "which is the only way a result like this means anything at all."
    )
    lines.extend(BACKTEST_CAVEATS)
    return lines


def cmd_backtest(cfg: Config, deps: Deps, *, start: str | None, end: str | None,
                 folds: bool = False, config_note: str | None = None) -> int:
    """Run the strategy over past years and say plainly how it did.

    The history test lives in its own module and is imported at the moment it is asked
    for, so every other command works whether or not that module is present.
    """
    out = deps.out
    if config_note:
        out(config_note)
    try:
        module = importlib.import_module(BACKTEST_MODULE)
        run_backtest = getattr(module, "run_backtest")
        run_folds = getattr(module, "run_folds")
        fold_type = getattr(module, "Fold")
    except (ImportError, AttributeError):
        out(BACKTEST_MISSING)
        return 1

    try:
        source = deps.make_history_source(cfg)
        if folds:
            periods = [
                fold_type(name, _parse_day(first, name), _parse_day(last, name))
                for name, first, last, _ in FOLD_PERIODS
            ]
        else:
            first = _parse_day(start or DEFAULT_BACKTEST_START, "--start")
            last = _parse_day(end, "--end") if end else deps.clock().date()
            if last <= first:
                out("The last day has to come after the first day. Nothing was run.")
                return 1
    except ConfigError as e:
        out(f"The history test could not start. {e}.")
        return 1

    ledger_path = resolve_path(cfg, BACKTEST_LEDGER)
    out("Reading years of past prices. This takes a few minutes and sends no orders.")

    if folds:
        results = run_folds(cfg, source, periods, ledger_path)
        lines = render_fold_verdict(results)
    else:
        results = run_backtest(cfg, source, first, last, ledger_path)
        lines = render_backtest_verdict(results)

    out("")
    for line in lines:
        out(line)
    out("")
    out(f"The full write up is in {repo_root() / 'docs' / 'BACKTEST.md'}.")
    return 0


# ---------------------------------------------------------------------------
# argument parsing and the top level guard
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fulcrum",
        description="Fulcrum: rules decide what to buy and sell, the AI can only say no. "
                    "Paper trading only.",
    )
    parser.add_argument("--config", default=None,
                        help="settings file to use (default ./config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="check every part, send nothing anywhere")

    run = sub.add_parser("run", help="run one decision round, or the loop")
    run.add_argument("--once", action="store_true", help="one round, then stop")
    run.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="use pretend fills even if the settings say paper")

    sub.add_parser("status", help="what it holds right now")
    sub.add_parser("report", help="how it has done so far")

    back = sub.add_parser("backtest", help="run the strategy over past years")
    back.add_argument("--start", default=None,
                      help=f"first day, as YYYY-MM-DD (default {DEFAULT_BACKTEST_START})")
    back.add_argument("--end", default=None, help="last day, as YYYY-MM-DD (default today)")
    back.add_argument("--folds", action="store_true",
                      help="run the three named market periods instead of one date range")

    return parser


def resolve_config(explicit: str | None) -> tuple[Path, str | None]:
    """Pick the settings file. Returns the path and a note to show the person, if any.

    An explicitly named file that is not there is a mistake worth stopping for. A missing
    ``config.toml`` is not: the example is used and the person is told so.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise ConfigError(f"the settings file {path} was not found")
        return path, None

    local = repo_root() / "config.toml"
    if local.exists():
        return local, None
    example = example_path()
    note = (
        f"No config.toml was found, so the example settings in {example.name} are being "
        "used. Copy that file to config.toml when you want to change anything."
    )
    return example, note


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    """Parse the arguments, run the command, and turn any surprise into one sentence."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    deps = deps or Deps()
    out = deps.out

    try:
        config_path, note = resolve_config(args.config)
        cfg = load_config(config_path)
    except ConfigError as e:
        out(f"The settings file could not be used. {e}.")
        out("Fix that file and run this again. Nothing was sent anywhere.")
        return 1

    setup_logging(cfg.run.log_level, resolve_path(cfg, LOG_RELATIVE_PATH))

    try:
        if args.command == "selftest":
            return cmd_selftest(cfg, deps, config_note=note)
        if args.command == "run":
            return cmd_run(cfg, deps, once=args.once, dry_run_flag=args.dry_run, config_note=note)
        if args.command == "status":
            return cmd_status(cfg, deps, config_note=note)
        if args.command == "report":
            return cmd_report(cfg, deps, config_note=note)
        if args.command == "backtest":
            return cmd_backtest(cfg, deps, start=args.start, end=args.end, folds=args.folds,
                                config_note=note)
        out(f"There is no command called {args.command}.")
        return 1
    except KeyboardInterrupt:
        out(STOPPED_MESSAGE)
        return 0
    except Exception:  # noqa: BLE001  a person gets a sentence; the file gets the detail
        return report_unexpected(out, f"the {args.command} command")


def report_unexpected(out: Callable[[str], None], what: str) -> int:
    """One plain sentence on the screen, the whole traceback in the log file."""
    logging.getLogger(DETAILS_LOGGER).exception("Unexpected failure during %s.", what)
    out(f"Something unexpected went wrong during {what}, so Fulcrum stopped. {LOG_SENTENCE}.")
    out("Nothing was left running. Tell me what you were doing and I will look at the log.")
    return 1
