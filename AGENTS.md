# AGENTS.md: Revenant Agent Fulcrum

Read first, every session: `tasks/lessons.md`, then `tasks/todo.md`, then `docs/DECISIONS.md`. The spec is `BUILD-PROMPT.md`; the approved plan is `docs/PLAN.md`. Machine law is Nomos Logos (injected at session start) and the ASK FIRST rule; both bind every subagent.

## What this repo is

A standalone Python 3.11 paper-trading bot for spot crypto on Alpaca. One package `fulcrum/`, one process, one SQLite ledger. Data flow:

```
data -> features -> signal -> targets -> candidates -> llm_filter (veto only) -> risk -> broker -> ledger
```

**The contract: rules decide, size, and execute; the LLM may only veto.** If a change lets the LLM pick a size, price, symbol, time, or exit, it is wrong regardless of what it improves.

## Hard lines in the code (do not weaken)

- `fulcrum/alpaca.py` `AlpacaBroker` accepts only `fulcrum.broker.ALPACA_PAPER_URL`. No live mode exists; `config.run.mode` is `dry_run` or `paper`.
- `fulcrum/risk.py::check_order` is the single gate for every order. Exits and stops (`OrderRequest.purpose`) are never blocked. There is no bypass parameter.
- `KillSwitch` has no reset method that works without Dave's hand-made file; the code path that would create that file must not exist (a test scans the source for it).
- The filter sees entries and increases only; any failure is fail-open and logged.
- Credentials: DPAPI `.cred` files under `~/.fulcrum` named `*_agent.cred`, loaded by `fulcrum/creds.py`. Never read, print, log, or copy a value. Never touch the old bot's `alpaca.cred`, `~/.openclaw`, or `fulcrum_review_baseline.json`.
- Every user-facing string (CLI, Telegram, README, HANDOFF) is plain English for a non-programmer (D-013). No em dashes, no emojis, no tables in Telegram.

## Working in this repo

- Venv: `.\venv\Scripts\python.exe`. Tests: `.\venv\Scripts\python.exe -m pytest`. All tests are offline; none may touch the network or a credential.
- Pre-commit secret scan is a plain git hook (`scripts/hooks/pre-commit`); reinstall with `scripts\Install-Hooks.ps1` after a fresh clone. If it blocks, fix the cause; never bypass.
- Facts about Alpaca, data sources, and regimes were verified against official docs on 2026-09-21 and live in `docs/DECISIONS.md` D-009 to D-012 with URLs. Re-verify before relying on them months later; these numbers move.
- Judgment calls go in `docs/DECISIONS.md` (append, never rewrite). Corrections from Dave go in `tasks/lessons.md`.
- Commit at green milestones with a clear message. No remote, no push, no PR unless Dave says so.
- Never start the bot as a long-running process, create a scheduled task, or make a real Alpaca, Telegram, or Anthropic call yourself. Dave runs those from `Start-Fulcrum.ps1` per `docs/HANDOFF.md`.

## Module map

| File | Role |
|---|---|
| `models.py` | frozen dataclasses shared everywhere |
| `config.py` | typed fail-fast config from `config.toml` |
| `ledger.py` | SQLite audit trail, ghost book, kill-switch state |
| `features.py`, `signal.py`, `sizing.py`, `risk.py` | pure strategy and risk core (most tested) |
| `broker.py` | `Broker` protocol, `FakeBroker`, the paper URL constant |
| `alpaca.py` | `AlpacaBroker` (paper only) and `AlpacaData` |
| `data.py` | historical bars (Coinbase primary, Binance Vision fallback), cache, liquidity screen |
| `llm_filter.py`, `claude_filter.py` | veto-only filter contract, fakes, and the Codex implementation |
| `engine.py` | the cycle: rebalance, tick, reconcile, ghost book |
| `backtest.py`, `report.py` | walk-forward history test and metrics |
| `scheduler.py`, `telegram.py`, `notify.py` | the loop and plain-text notifications |
| `cli.py` | `python -m fulcrum <backtest|run|status|report|selftest>` |
