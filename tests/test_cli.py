"""Tests for the command line.

Every command function is called directly with an injected ``Deps`` factory, so nothing
real is ever built: no Alpaca call, no Telegram send, no Anthropic call, no PowerShell,
no credential file read, no long running process. The only things that touch disk are a
SQLite ledger and a settings file, both under pytest's tmp_path.

Two house rules are asserted throughout:

* a person never sees a traceback, so no recorded line may contain "Traceback";
* no line may carry a credential value, so the fake secrets below are distinctive
  strings and every screen is searched for them.
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from fulcrum import cli
from fulcrum.broker import FakeBroker
from fulcrum.config import Config
from fulcrum.creds import Credential, CredentialError
from fulcrum.ledger import Ledger
from fulcrum.llm_filter import FakeFilter
from fulcrum.models import Fill, Position, utcnow
from fulcrum.notify import FakeNotifier

# Distinctive fake secrets. If one of these ever shows up on a screen, a real one would too.
FAKE_SECOND_FIELD = "SENTINEL-B-MUST-NEVER-BE-PRINTED"
FAKE_FIRST_FIELD = "SENTINEL-A-MUST-NEVER-BE-PRINTED"

NOW = datetime(2026, 3, 2, 14, 15, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class Recorder:
    """Stands in for printing. Collects every line a command would show."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def last(self) -> str:
        return self.lines[-1] if self.lines else ""


def assert_plain(rec: Recorder) -> None:
    """No traceback, no raw exception label, no secret, anywhere on the screen."""
    assert "Traceback" not in rec.text
    assert "Error:" not in rec.text
    assert FAKE_SECOND_FIELD not in rec.text
    assert FAKE_FIRST_FIELD not in rec.text
    # A long dash would break the house style the whole project is written to. It is
    # spelled by code point here so the character itself never appears in this repo.
    assert chr(0x2014) not in rec.text
    assert chr(0x2013) not in rec.text


def synthetic_frames(symbols, now: datetime, days: int = 70) -> dict:
    """Bars that trend gently upward with a repeatable wobble, so vol is not zero.

    The wobble is a fixed arithmetic pattern rather than a random draw, so the same
    symbols always produce the same frames and a failure is always reproducible.
    """
    frames = {}
    for index, symbol in enumerate(sorted(symbols)):
        start = now - timedelta(days=days - 1)
        price = 100.0 * (index + 1)
        rows = []
        for day in range(days):
            wobble = 0.004 * (1 if day % 3 else -1) * (1 + (day % 5) / 5.0)
            close = price * (1.0 + 0.002 + wobble)
            rows.append({
                "ts": start + timedelta(days=day),
                "open": price,
                "high": max(price, close) * 1.01,
                "low": min(price, close) * 0.99,
                "close": close,
                "volume": 1000.0,
            })
            price = close
        frame = pd.DataFrame(rows).set_index("ts")
        frames[symbol] = frame
    return frames


@dataclass
class FakeStackParts:
    """What the fake factory built, kept so a test can look at it afterwards."""

    broker: FakeBroker | None = None
    filt: FakeFilter | None = None
    notifier: FakeNotifier | None = None
    dry_run_seen: bool | None = None
    bars_calls: list = field(default_factory=list)


def make_deps(cfg: Config, *, now: datetime = NOW, rec: Recorder | None = None,
              missing: tuple[str, ...] = (), unreadable: tuple[str, ...] = (),
              sleeps_before_stop: int = 1, parts: FakeStackParts | None = None,
              ledger_holder: list | None = None) -> tuple[cli.Deps, Recorder, FakeStackParts]:
    """Build a Deps whose every callable is a fake. Nothing here can reach the network."""
    rec = rec or Recorder()
    parts = parts or FakeStackParts()
    creds_dir = Path(cfg.run.creds_dir).expanduser()
    creds_dir.mkdir(parents=True, exist_ok=True)
    for service, filename in {"alpaca": "alpaca_agent.cred", "anthropic": "anthropic_agent.cred",
                              "telegram": "telegram_agent.cred"}.items():
        if service in missing:
            continue
        (creds_dir / filename).write_text("pretend encrypted blob", encoding="utf-8")

    def load_credential(config: Config, service: str) -> Credential:
        if service in missing:
            raise CredentialError(f"credential file missing for {service}")
        if service in unreadable:
            raise CredentialError(f"DPAPI decrypt failed for {service}")
        return Credential(user=FAKE_FIRST_FIELD, secret=FAKE_SECOND_FIELD)

    def open_ledger(config: Config) -> Ledger:
        ledger = Ledger(cli.resolve_path(config, config.run.ledger_path))
        if ledger_holder is not None:
            ledger_holder.append(ledger)
        return ledger

    def build_stack(config: Config, *, dry_run: bool, deps: cli.Deps) -> cli.Stack:
        parts.dry_run_seen = dry_run
        broker = FakeBroker(
            starting_cash=cli.DRY_RUN_PRETEND_DOLLARS,
            fee_bps=config.asset_class.taker_fee_bps,
            slippage_bps=config.asset_class.slippage_bps,
            min_notional_usd=config.asset_class.min_notional_usd,
        )
        parts.broker = broker
        parts.filt = FakeFilter(mode=config.llm_filter.mode)
        parts.notifier = FakeNotifier()

        def bars_provider(symbols, when):
            parts.bars_calls.append(tuple(symbols))
            frames = synthetic_frames(symbols, when)
            broker.set_prices(
                {s: float(f["close"].iloc[-1]) for s, f in frames.items()}, when
            )
            return frames

        def price_provider(symbols):
            out = {}
            for symbol in symbols:
                try:
                    out[symbol] = float(broker.latest_price(symbol).price)
                except Exception:  # noqa: BLE001  a missing price is not a test failure
                    pass
            return out

        return cli.Stack(
            broker=broker, filt=parts.filt, notifier=parts.notifier,
            bars_provider=bars_provider, price_provider=price_provider,
            notes=["Practice mode. Nothing leaves this machine."] if dry_run
            else ["Paper mode. Orders go to your Alpaca paper account."],
        )

    ticks = {"n": 0}

    def clock() -> datetime:
        return now + timedelta(minutes=ticks["n"])

    def sleep(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > sleeps_before_stop:
            raise KeyboardInterrupt

    deps = cli.Deps(
        open_ledger=open_ledger, load_credential=load_credential, build_stack=build_stack,
        clock=clock, sleep=sleep, out=rec,
    )
    return deps, rec, parts


@pytest.fixture
def cfg(raw_config, tmp_path) -> Config:
    raw_config["run"]["ledger_path"] = str(tmp_path / "ledger.sqlite")
    raw_config["run"]["creds_dir"] = str(tmp_path / "creds")
    raw_config["run"]["kill_reset_file"] = str(tmp_path / "creds" / "agent_kill_reset")
    raw_config["data"]["cache_dir"] = str(tmp_path / "bars")
    return Config.from_dict(raw_config, source_path="<test>")


@pytest.fixture
def shadow_cfg(raw_config, tmp_path) -> Config:
    raw_config["run"]["ledger_path"] = str(tmp_path / "ledger.sqlite")
    raw_config["run"]["creds_dir"] = str(tmp_path / "creds")
    raw_config["run"]["kill_reset_file"] = str(tmp_path / "creds" / "agent_kill_reset")
    raw_config["data"]["cache_dir"] = str(tmp_path / "bars")
    raw_config["llm_filter"]["mode"] = "shadow"
    return Config.from_dict(raw_config, source_path="<test>")


@pytest.fixture
def paper_cfg(raw_config, tmp_path) -> Config:
    raw_config["run"]["mode"] = "paper"
    raw_config["run"]["ledger_path"] = str(tmp_path / "ledger.sqlite")
    raw_config["run"]["creds_dir"] = str(tmp_path / "creds")
    raw_config["run"]["kill_reset_file"] = str(tmp_path / "creds" / "agent_kill_reset")
    raw_config["data"]["cache_dir"] = str(tmp_path / "bars")
    return Config.from_dict(raw_config, source_path="<test>")


@pytest.fixture(autouse=True)
def quiet_logging():
    """Keep setup_logging from creating a real log file inside the repo during tests.

    A handler that is already there stops setup_logging adding its own, so the rotating
    file under data/ is never opened and no test writes outside tmp_path.
    """
    details = logging.getLogger(cli.DETAILS_LOGGER)
    placeholder = logging.NullHandler()
    details.addHandler(placeholder)
    package = logging.getLogger("fulcrum")
    before = list(package.handlers)
    yield
    details.removeHandler(placeholder)
    for handler in list(package.handlers):
        if handler not in before:
            package.removeHandler(handler)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return repr(value)


def write_config(raw: dict, path: Path) -> Path:
    """Write a settings dict back out as TOML, for the tests that go through main()."""
    lines = []
    for section, table in raw.items():
        lines.append(f"[{section}]")
        for key, value in table.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def populate(cfg: Config) -> None:
    """Put a small but real history into the ledger: one buy, one sell, some equity points."""
    ledger = Ledger(cli.resolve_path(cfg, cfg.run.ledger_path))
    symbol = cfg.universe.core[0]
    start = NOW - timedelta(days=20)
    ledger.start_run("history", cfg.run.mode, cfg.config_hash, None, start)
    ledger.record_fill(Fill("cli-buy", symbol, "buy", 1.0, 1000.0, 2.5, start, 10.0))
    ledger.record_fill(Fill("cli-sell", symbol, "sell", 1.0, 1100.0, 2.75, start + timedelta(days=5), 10.0))
    for day in range(0, 21, 2):
        when = start + timedelta(days=day)
        equity = 100_000.0 + 40.0 * day
        ledger.record_equity(when, equity, 20_000.0, 0.4, 100_800.0,
                             max(0.0, 1.0 - equity / 100_800.0))
    ledger.snapshot_positions(
        [Position(symbol, 1.0, 1000.0, 1100.0)], {symbol: 950.0}, {symbol: "d1"}, NOW
    )
    ledger.close()


# ---------------------------------------------------------------------------
# ids, scope and paths
# ---------------------------------------------------------------------------


def test_run_id_is_the_start_time_plus_a_short_tail():
    run_id = cli.make_run_id(NOW)
    stamp, _, tail = run_id.partition("-")
    assert stamp == "20260302T141500Z"
    assert len(tail) == 6 and all(c in "0123456789abcdef" for c in tail)
    # The tail keeps two runs started in the same second apart, so it has to vary. Drawn
    # from a 24 bit space, so ten draws colliding into one value is not a thing that happens.
    draws = {cli.make_run_id(NOW) for _ in range(10)}
    assert len(draws) > 1, "the tail must vary between runs"
    assert cli.make_run_id(NOW, suffix="abc123") == "20260302T141500Z-abc123"


def test_scope_is_stable_across_restarts_and_differs_by_mode(cfg, paper_cfg):
    """The scope namespaces order ids, so a restart must reproduce it exactly."""
    assert cli.make_scope(cfg) == cli.make_scope(cfg)
    assert cli.make_scope(cfg).startswith("dry_run-")
    assert cli.make_scope(paper_cfg).startswith("paper-")
    assert cli.make_scope(cfg) != cli.make_scope(paper_cfg), "practice and paper ids must not collide"


def test_scope_changes_with_the_ledger_file(raw_config, tmp_path):
    raw_config["run"]["ledger_path"] = str(tmp_path / "one.sqlite")
    first = Config.from_dict(dict(raw_config), source_path="<test>")
    raw_config["run"]["ledger_path"] = str(tmp_path / "two.sqlite")
    second = Config.from_dict(dict(raw_config), source_path="<test>")
    assert cli.make_scope(first) != cli.make_scope(second)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------


def test_selftest_passes_with_every_credential_present(cfg):
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_selftest(cfg, deps)
    assert code == 0
    assert rec.last == cli.PASS_LINE
    assert_plain(rec)


def test_selftest_every_check_line_ends_in_ok_when_all_is_well(cfg):
    deps, rec, _ = make_deps(cfg)
    cli.cmd_selftest(cfg, deps)
    check_lines = [line for line in rec.lines if line.endswith(": ok")]
    # config, ledger, three credentials, fake broker, risk gate, filter parser,
    # paper lock, message templates: ten checks in all.
    assert len(check_lines) == 11, rec.text  # 11 since the secret-scan hook check was added (security review)


def test_selftest_fails_and_names_the_missing_credential_file(cfg):
    deps, rec, _ = make_deps(cfg, missing=("telegram",))
    code = cli.cmd_selftest(cfg, deps)
    assert code == 1
    assert rec.last == cli.FAIL_LINE
    assert "These checks did not pass:" in rec.text
    assert "telegram_agent.cred" in rec.text
    assert "Telegram credential file" in rec.text
    # The two credentials that are fine must still report ok.
    assert "Alpaca credential file: ok" in rec.text
    assert "Anthropic credential file: ok" in rec.text
    assert_plain(rec)


def test_selftest_fails_plainly_when_a_credential_cannot_be_unlocked(cfg):
    deps, rec, _ = make_deps(cfg, unreadable=("anthropic",))
    code = cli.cmd_selftest(cfg, deps)
    assert code == 1
    assert rec.last == cli.FAIL_LINE
    assert "could not be unlocked" in rec.text
    assert_plain(rec)


def test_selftest_never_shows_a_credential_value_or_its_length(cfg):
    deps, rec, _ = make_deps(cfg)
    cli.cmd_selftest(cfg, deps)
    assert FAKE_SECOND_FIELD not in rec.text
    assert FAKE_FIRST_FIELD not in rec.text
    assert str(len(FAKE_SECOND_FIELD)) not in rec.text


def test_selftest_shows_the_filter_request_shortened_to_forty_lines(cfg):
    deps, rec, _ = make_deps(cfg)
    cli.cmd_selftest(cfg, deps)
    header = "This is what would be sent to the AI filter if it were asked. It was not sent."
    assert header in rec.text
    start = rec.lines.index(header) + 1
    preview = []
    for line in rec.lines[start:]:
        if line == "":
            break
        preview.append(line)
    assert preview, "the preview must not be empty"
    body = [line for line in preview if not line.startswith("(Shown to the first")]
    assert len(body) <= cli.PREVIEW_MAX_LINES
    assert any("system" in line for line in body), "the preview should show what is sent"


def test_selftest_writes_nothing_to_the_trading_tables(cfg):
    """The risk gate check is worked out in memory. Opening the ledger must not add rows."""
    deps, rec, _ = make_deps(cfg)
    cli.cmd_selftest(cfg, deps)
    ledger = Ledger(cli.resolve_path(cfg, cfg.run.ledger_path))
    counts = ledger.table_counts()
    ledger.close()
    for table in ("orders", "order_events", "fills", "decisions", "equity_curve",
                  "kill_switch_events", "alerts"):
        assert counts[table] == 0, f"{table} should still be empty after a selftest"


def test_selftest_reports_a_broken_ledger_without_a_traceback(cfg):
    deps, rec, _ = make_deps(cfg)

    def refuse(config):
        raise RuntimeError("ledger schema version 99 != code 1; migrate first")

    deps.open_ledger = refuse
    code = cli.cmd_selftest(cfg, deps)
    assert code == 1
    assert rec.last == cli.FAIL_LINE
    assert "it could not be opened" in rec.text
    assert_plain(rec)


def test_selftest_sends_nothing_anywhere(cfg):
    """Both possible last lines end in the same promise, and the fakes saw no traffic."""
    deps, rec, parts = make_deps(cfg)
    cli.cmd_selftest(cfg, deps)
    assert rec.last.endswith("Nothing was sent anywhere.")
    assert parts.broker is None, "selftest must not build the run stack at all"
    assert parts.notifier is None


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_once_dry_run_ends_with_the_practice_sentence(cfg):
    deps, rec, parts = make_deps(cfg)
    code = cli.cmd_run(cfg, deps, once=True, dry_run_flag=False)
    assert code == 0
    assert rec.last == cli.DRY_RUN_DONE
    assert parts.dry_run_seen is True
    assert parts.bars_calls, "the bars provider should have been asked for data"
    assert_plain(rec)


def test_run_once_prints_the_scheduler_summary_lines(cfg):
    deps, rec, _ = make_deps(cfg)
    cli.cmd_run(cfg, deps, once=True, dry_run_flag=False)
    assert any("Practice cycle. No schedule was changed by this run." in line
               for line in rec.lines)
    assert any("Checked the broker for orders left over from last time" in line
               for line in rec.lines)
    assert any("Account value $" in line for line in rec.lines)


def test_run_once_records_the_round_in_the_ledger(cfg):
    deps, rec, _ = make_deps(cfg)
    cli.cmd_run(cfg, deps, once=True, dry_run_flag=False)
    ledger = Ledger(cli.resolve_path(cfg, cfg.run.ledger_path))
    counts = ledger.table_counts()
    ledger.close()
    assert counts["runs"] == 1
    assert counts["decisions"] > 0
    assert counts["equity_curve"] > 0


def test_run_once_in_paper_mode_ends_with_the_paper_sentence(paper_cfg):
    deps, rec, parts = make_deps(paper_cfg)
    code = cli.cmd_run(paper_cfg, deps, once=True, dry_run_flag=False)
    assert code == 0
    assert rec.last == cli.PAPER_DONE
    assert parts.dry_run_seen is False
    assert_plain(rec)


def test_dry_run_flag_overrides_paper_mode(paper_cfg):
    """The flag may only ever make things safer, never the other way round."""
    deps, rec, parts = make_deps(paper_cfg)
    code = cli.cmd_run(paper_cfg, deps, once=True, dry_run_flag=True)
    assert code == 0
    assert parts.dry_run_seen is True
    assert rec.last == cli.DRY_RUN_DONE


def test_run_once_in_shadow_mode_asks_the_fake_filter(shadow_cfg):
    deps, rec, parts = make_deps(shadow_cfg)
    code = cli.cmd_run(shadow_cfg, deps, once=True, dry_run_flag=False)
    assert code == 0
    assert parts.filt is not None and parts.filt.calls, "shadow mode should ask the filter"
    assert rec.last == cli.DRY_RUN_DONE
    assert_plain(rec)


def test_loop_prints_a_check_line_and_stops_cleanly(cfg):
    deps, rec, _ = make_deps(cfg, sleeps_before_stop=0)
    code = cli.cmd_run(cfg, deps, once=False, dry_run_flag=False)
    assert code == 0
    assert rec.last == "Stopped by you. Nothing is running now."
    check_lines = [line for line in rec.lines if line.startswith("Checked at ")]
    assert check_lines, rec.text
    # Word for word the shape docs/HANDOFF.md step 7 promises.
    assert check_lines[0].startswith("Checked at 14:15 UTC. Holding ")
    assert "No stops hit." in check_lines[0]
    assert check_lines[0].endswith("Safety brake off.")
    assert_plain(rec)


def test_loop_says_when_the_weekly_round_happened(cfg):
    deps, rec, _ = make_deps(cfg, sleeps_before_stop=0)
    cli.cmd_run(cfg, deps, once=False, dry_run_flag=False)
    assert any(line.startswith("Weekly decision round done at ") for line in rec.lines)


def test_check_line_counts_one_coin_in_the_singular(cfg):
    deps, _, _ = make_deps(cfg)
    broker = FakeBroker(starting_cash=1000.0, fee_bps=25.0, slippage_bps=10.0)
    broker._positions["BTC/USD"] = Position("BTC/USD", 1.0, 100.0, 100.0)

    class Stub:
        pass

    engine = Stub()
    engine.broker = broker
    engine.kill = Stub()
    engine.kill.tripped = lambda: False
    tick = Stub()
    tick.stops_fired = []
    line = cli.describe_check(NOW, tick, engine)
    assert line == "Checked at 14:15 UTC. Holding 1 coin. No stops hit. Safety brake off."


def test_check_line_reports_a_stop_and_a_tripped_brake(cfg):
    class Stub:
        pass

    engine = Stub()
    engine.broker = Stub()
    engine.broker.positions = lambda: []
    engine.kill = Stub()
    engine.kill.tripped = lambda: True
    tick = Stub()
    tick.stops_fired = ["BTC/USD"]
    line = cli.describe_check(NOW, tick, engine)
    assert line == ("Checked at 14:15 UTC. Holding no coins. Stops hit: BTC/USD. "
                    "Safety brake ON.")


# ---------------------------------------------------------------------------
# status and report
# ---------------------------------------------------------------------------


def test_status_on_an_empty_ledger_says_nothing_has_happened(cfg):
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_status(cfg, deps)
    assert code == 0
    assert rec.text.strip() == "Nothing has happened yet."
    assert_plain(rec)


def test_status_on_a_populated_ledger_describes_the_holdings(cfg):
    populate(cfg)
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_status(cfg, deps)
    assert code == 0
    assert "Holding 1 coin worth" in rec.text
    assert "Safety brake is off." in rec.text
    assert "No real money is involved." in rec.text
    assert_plain(rec)


def test_report_on_an_empty_ledger_says_there_is_no_history(cfg):
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_report(cfg, deps)
    assert code == 0
    assert "Nothing has happened yet. There is no account history to report on." in rec.text
    assert "There is no history yet, so there is nothing to conclude." in rec.text
    assert_plain(rec)


def test_report_on_a_populated_ledger_covers_the_whole_history(cfg):
    populate(cfg)
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_report(cfg, deps)
    assert code == 0
    # The report is one block of text, so the headings are lines of that block and not
    # separate calls to the printer. Splitting is what the screen does anyway.
    printed = rec.text.splitlines()
    for heading in ("Account", "Risk", "Costs", "Trades", "AI filter", "Ghost book",
                    "What this means"):
        assert heading in printed, f"the {heading} section should be there"
    assert "Orders filled: 2." in rec.text
    assert "Nothing here involved real money." in rec.text
    assert_plain(rec)


def test_report_after_a_run_reads_the_same_ledger(cfg):
    deps, rec, _ = make_deps(cfg)
    cli.cmd_run(cfg, deps, once=True, dry_run_flag=False)
    rec.lines.clear()
    code = cli.cmd_report(cfg, deps)
    assert code == 0
    assert "Account" in rec.text.splitlines()
    assert_plain(rec)


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def fake_metrics(**overrides):
    """A report.Metrics with every field filled, so the wording layer has real numbers."""
    from fulcrum.report import Metrics

    base = dict(
        start=NOW - timedelta(days=200), end=NOW, days=200.0, total_return=0.18, cagr=0.34,
        annualized_vol=0.42, sharpe=0.80, sharpe_note="Sharpe assumes a 0% cash rate.",
        max_drawdown=0.11, max_drawdown_start=None, max_drawdown_end=None,
        average_exposure=0.44, turnover=2.1, fee_drag_pct_equity=0.012, fills=24,
        round_trips=9, win_rate=0.55, average_win=310.0, average_loss=-180.0,
        win_loss_magnitude_ratio=1.72, unmatched_sell_qty={}, worst_week=-0.08,
        best_week=0.09, alert_counts={}, kill_switch_trips=0, trading_days_per_year=365,
        cash_rate_annual=0.0, daily_return_count=199,
    )
    base.update(overrides)
    return Metrics(**base)


def fake_backtest_module(monkeypatch, results, seen: dict | None = None):
    """Stand in for fulcrum.backtest with the same call shape the real module has."""
    from fulcrum.backtest import Fold

    module = types.ModuleType("fulcrum.backtest")

    def run_backtest(config, source, start, end, ledger_path, benchmarks=True, filt=None):
        if seen is not None:
            seen.update({"start": start, "end": end, "ledger_path": ledger_path,
                         "source": source})
        return results

    def run_folds(config, source, folds, ledger_path, filt=None):
        if seen is not None:
            seen.update({"folds": folds, "ledger_path": ledger_path})
        return results

    module.run_backtest = run_backtest
    module.run_folds = run_folds
    module.Fold = Fold
    monkeypatch.setitem(sys.modules, "fulcrum.backtest", module)
    return module


class FakeResult:
    def __init__(self, metrics, missing_dates=None):
        self.metrics = metrics
        self.missing_dates = missing_dates or {}


def test_backtest_prints_the_verdict_and_where_the_write_up_is(cfg, monkeypatch):
    from datetime import date

    seen: dict = {}
    results = {
        "strategy": FakeResult(fake_metrics(sharpe=0.80)),
        "benchmark_btc": FakeResult(fake_metrics(sharpe=1.30, total_return=0.40)),
        "benchmark_cash": FakeResult(fake_metrics(sharpe=None, total_return=0.0)),
    }
    fake_backtest_module(monkeypatch, results, seen)

    deps, rec, _ = make_deps(cfg)
    deps.make_history_source = lambda config: "pretend price archive"
    code = cli.cmd_backtest(cfg, deps, start="2023-01-01", end="2023-12-31", folds=False)
    assert code == 0
    assert seen["start"] == date(2023, 1, 1)
    assert seen["end"] == date(2023, 12, 31)
    assert "How the rules did" in rec.text
    assert "just holding Bitcoin" in rec.text
    # Holding Bitcoin scored better, so the verdict has to say so rather than dress it up.
    assert "Doing nothing was better." in rec.text
    assert "BACKTEST.md" in rec.last
    assert_plain(rec)


def test_backtest_calls_a_weak_result_weak(cfg, monkeypatch):
    results = {"strategy": FakeResult(fake_metrics(sharpe=0.20))}
    fake_backtest_module(monkeypatch, results)
    deps, rec, _ = make_deps(cfg)
    deps.make_history_source = lambda config: "pretend price archive"
    assert cli.cmd_backtest(cfg, deps, start=None, end=None, folds=False) == 0
    assert "has not earned anything" in rec.text
    assert "The engine still works" in rec.text


def test_backtest_says_when_price_history_was_absent(cfg, monkeypatch):
    from datetime import date

    results = {"strategy": FakeResult(fake_metrics(), {"SOL/USD": [date(2023, 5, 1)]})}
    fake_backtest_module(monkeypatch, results)
    deps, rec, _ = make_deps(cfg)
    deps.make_history_source = lambda config: "pretend price archive"
    cli.cmd_backtest(cfg, deps, start=None, end=None, folds=False)
    assert "Some days of price history were absent for SOL/USD" in rec.text
    assert "never invented" in rec.text


def test_backtest_folds_runs_the_three_named_periods(cfg, monkeypatch):
    seen: dict = {}
    results = {
        "stress": FakeResult(fake_metrics(sharpe=-0.40, total_return=-0.22)),
        "trending": FakeResult(fake_metrics(sharpe=1.60, total_return=0.55)),
        "choppy": FakeResult(fake_metrics(sharpe=0.05, total_return=-0.03)),
    }
    fake_backtest_module(monkeypatch, results, seen)
    deps, rec, _ = make_deps(cfg)
    deps.make_history_source = lambda config: "pretend price archive"
    code = cli.cmd_backtest(cfg, deps, start=None, end=None, folds=True)
    assert code == 0
    assert [f.name for f in seen["folds"]] == ["stress", "trending", "choppy"]
    assert str(seen["folds"][0].start) == "2026-02-01"
    assert "Stress: the February 2026 crash" in rec.text
    assert "Nothing was tuned between them" in rec.text
    assert_plain(rec)


def test_backtest_refuses_a_date_that_is_not_a_date(cfg, monkeypatch):
    fake_backtest_module(monkeypatch, {"strategy": FakeResult(fake_metrics())})
    deps, rec, _ = make_deps(cfg)
    deps.make_history_source = lambda config: "pretend price archive"
    code = cli.cmd_backtest(cfg, deps, start="last Tuesday", end=None, folds=False)
    assert code == 1
    assert "has to be written as a date like 2024-01-31" in rec.text
    assert_plain(rec)


def test_backtest_refuses_a_range_that_runs_backwards(cfg, monkeypatch):
    fake_backtest_module(monkeypatch, {"strategy": FakeResult(fake_metrics())})
    deps, rec, _ = make_deps(cfg)
    deps.make_history_source = lambda config: "pretend price archive"
    code = cli.cmd_backtest(cfg, deps, start="2024-06-01", end="2024-01-01", folds=False)
    assert code == 1
    assert "has to come after the first day" in rec.text


def test_backtest_refuses_a_price_source_it_cannot_read(cfg, monkeypatch):
    """The example settings use a csv source, which the history test cannot read from."""
    fake_backtest_module(monkeypatch, {"strategy": FakeResult(fake_metrics())})
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_backtest(cfg, deps, start=None, end=None, folds=False)
    assert code == 1
    assert "The history test could not start." in rec.text
    assert "coinbase or binance_vision" in rec.text
    assert_plain(rec)


def test_backtest_says_so_plainly_when_the_module_is_not_there(cfg, monkeypatch):
    monkeypatch.setattr(cli, "BACKTEST_MODULE", "fulcrum._no_such_backtest_module")
    deps, rec, _ = make_deps(cfg)
    code = cli.cmd_backtest(cfg, deps, start=None, end=None, folds=False)
    assert code == 1
    assert rec.text.strip() == cli.BACKTEST_MISSING
    assert_plain(rec)


def test_backtest_says_so_plainly_when_the_entry_point_is_missing(cfg, monkeypatch):
    module = types.ModuleType("fulcrum.backtest")
    monkeypatch.setitem(sys.modules, "fulcrum.backtest", module)
    deps, rec, _ = make_deps(cfg)
    assert cli.cmd_backtest(cfg, deps, start=None, end=None, folds=False) == 1
    assert cli.BACKTEST_MISSING in rec.text


# ---------------------------------------------------------------------------
# the top level guard, argument parsing and config resolution
# ---------------------------------------------------------------------------


def test_guard_prints_one_sentence_and_no_traceback():
    rec = Recorder()
    try:
        raise ValueError("something deep in the machinery went wrong")
    except ValueError:
        code = cli.report_unexpected(rec, "the status command")
    assert code == 1
    assert len(rec.lines) == 2
    assert cli.LOG_SENTENCE in rec.lines[0]
    assert "Traceback" not in rec.text
    assert "ValueError" not in rec.text


def test_main_turns_an_unexpected_failure_into_a_sentence(cfg, raw_config, tmp_path):
    path = write_config(raw_config, tmp_path / "config.toml")
    deps, rec, _ = make_deps(cfg)

    def explode(config):
        raise ValueError("the ledger file is a picture of a cat")

    deps.open_ledger = explode
    code = cli.main(["--config", str(path), "status"], deps=deps)
    assert code == 1
    assert cli.LOG_SENTENCE in rec.text
    assert "Traceback" not in rec.text
    assert "ValueError" not in rec.text
    assert "picture of a cat" not in rec.text


def test_main_runs_status_end_to_end(cfg, raw_config, tmp_path):
    path = write_config(raw_config, tmp_path / "config.toml")
    deps, rec, _ = make_deps(cfg)
    assert cli.main(["--config", str(path), "status"], deps=deps) == 0
    assert "Nothing has happened yet." in rec.text
    assert_plain(rec)


def test_main_refuses_a_settings_file_that_is_not_there(cfg, tmp_path):
    deps, rec, _ = make_deps(cfg)
    code = cli.main(["--config", str(tmp_path / "absent.toml"), "status"], deps=deps)
    assert code == 1
    assert "The settings file could not be used." in rec.text
    assert "Nothing was sent anywhere." in rec.text
    assert_plain(rec)


def test_main_reports_a_bad_settings_value_plainly(cfg, raw_config, tmp_path):
    raw_config["risk"]["kill_switch_drawdown"] = 0.99
    path = write_config(raw_config, tmp_path / "config.toml")
    deps, rec, _ = make_deps(cfg)
    code = cli.main(["--config", str(path), "status"], deps=deps)
    assert code == 1
    assert "The settings file could not be used." in rec.text
    assert_plain(rec)


def test_main_stops_quietly_on_ctrl_c(cfg, raw_config, tmp_path):
    path = write_config(raw_config, tmp_path / "config.toml")
    deps, rec, _ = make_deps(cfg)

    def interrupt(config):
        raise KeyboardInterrupt

    deps.open_ledger = interrupt
    code = cli.main(["--config", str(path), "status"], deps=deps)
    assert code == 0
    assert rec.last == "Stopped by you. Nothing is running now."


def test_config_falls_back_to_the_example_with_a_note(monkeypatch, tmp_path):
    """No config.toml in the repo means the example is used, and the person is told."""
    empty_root = tmp_path / "root"
    empty_root.mkdir()
    monkeypatch.setattr(cli, "repo_root", lambda: empty_root)
    path, note = cli.resolve_config(None)
    assert path.name == "config.example.toml"
    assert note is not None
    assert "No config.toml was found" in note
    assert "Copy that file to config.toml" in note


def test_config_uses_the_repo_file_when_it_is_there(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "repo_root", lambda: root)
    path, note = cli.resolve_config(None)
    assert path == root / "config.toml"
    assert note is None


def test_parser_knows_every_command_the_launcher_uses():
    """Start-Fulcrum.ps1 runs exactly these. A rename here would break the menu."""
    parser = cli.build_parser()
    assert parser.parse_args(["selftest"]).command == "selftest"
    once = parser.parse_args(["run", "--once", "--dry-run"])
    assert once.command == "run" and once.once is True and once.dry_run is True
    loop = parser.parse_args(["run"])
    assert loop.once is False and loop.dry_run is False
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["report"]).command == "report"
    back = parser.parse_args(["backtest", "--start", "2023-01-01", "--folds"])
    assert back.command == "backtest" and back.start == "2023-01-01" and back.folds is True
    assert parser.parse_args(["backtest"]).folds is False
    assert parser.parse_args(["--config", "x.toml", "status"].copy()).config == "x.toml"


def test_module_entry_point_calls_main():
    """python -m fulcrum must reach the same main() these tests exercise."""
    import fulcrum.__main__ as entry

    assert entry.main is cli.main


def test_paper_lock_check_never_names_the_live_address(cfg):
    """Decision D-009: the live trading address appears nowhere in the codebase."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "api.alpaca.markets" not in source.replace("paper-api.alpaca.markets", "")
    check = cli._check_paper_lock(cfg)
    assert check.ok, check.detail
