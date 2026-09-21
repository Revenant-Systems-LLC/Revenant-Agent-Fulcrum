# Revenant Agent Fulcrum v1 — Build Plan

Plan version 1.0, written 2026-09-21 against `BUILD-PROMPT.md` v1.0. This is the one planned checkpoint. After Dave's go, work proceeds autonomously; routine judgment calls land in `docs/DECISIONS.md`.

Sources read in full before writing this: BUILD-PROMPT.md; `docs/research/Fulcrum_Report.md` (copied from Desktop, hash-verified); the old repo's `CLAUDE.md`, `tools/alpaca_trader.py`, `tools/trade_journal.py`, and the DPAPI loader in `mcp_server/fulcrum_mcp_server.py`; the v1 `SKILL.md` and `Skill2.md`; `B:\BORIS_CLAUDE_SOP.md`; the Expertise "Fulcrum Crypto Trader" operating boundary (pasted by Dave 2026-09-21); Armory skill `ECC\llm-trading-agent-security`.

---

## 0. The contract, restated once

**RULES DECIDE, SIZE, AND EXECUTE. THE LLM MAY ONLY VETO.** The LLM never sees or affects exits, stops, sizes, or the kill switch. If any code path lets the LLM choose a size, price, symbol, or time, it is a bug of the highest severity.

Paper only. `AlpacaBroker` refuses any base URL that is not Alpaca's paper endpoint. No config flag for live exists.

---

## 1. Module boundaries

One package `fulcrum/`, Python 3.11+, one process, one SQLite file. Data flow:

```
data -> features -> signal -> candidates -> llm_filter (veto only) -> risk (size, caps, stops, kill switch) -> broker -> ledger -> reports
```

| Module | Responsibility | I/O | Stateful? |
|---|---|---|---|
| `config.py` | Load and validate `config.toml` into frozen dataclasses. Fail fast on any bad value. Asset-class block (calendar, annualization, fee model, min notional) lives here so the engine stays class-agnostic. | file read at startup | no |
| `data.py` | Bars. `HistoricalSource` for backtests (key-free public source, cached under `data/`). `AlpacaData` for live bars. Stale-data guard lives here. | HTTP GET, disk cache | cache only |
| `features.py` | Pure: bars in, feature frame out (trailing returns, 50d MA, realized vol, ATR-14). No clock reads. | none | no |
| `signal.py` | Pure: features in, per-symbol conviction in [0,1] out. TSMOM ensemble gated by MA. | none | no |
| `sizing.py` | Pure: conviction + vol + config -> target weights. Vol targeting, per-asset cap, gross cap, no-trade band. | none | no |
| `risk.py` | Pure checks + one stateful `KillSwitch`. Order clamp, 24h trade cap, exposure caps, fee/slippage alarms, kill switch. Exits are exempt from clamps and caps and are never blocked. | ledger (kill switch state) | kill switch only |
| `broker.py` | `Broker` protocol. `FakeBroker` (deterministic fills, fee + slippage model). `AlpacaBroker` (paper only, hard-locked). Ports the old repo's symbol map after live verification. | HTTP (Alpaca only) | FakeBroker holds sim state |
| `llm_filter.py` | `Filter` protocol. `FakeFilter` (scripted verdicts). `ClaudeFilter` (strict JSON, schema-validated, fail open). Modes: off / shadow / active. | HTTP (Anthropic only) | no |
| `ledger.py` | SQLite. Append-only where possible. Audit trail and measurement system, including the ghost book. | disk | yes |
| `scheduler.py` | Plain loop. Weekly rebalance, 15-minute stop and kill-switch tick. Reconciles ledger against broker on every start before anything else. | clock | loop state |
| `notify.py` | Telegram, plain text, templated. `FakeNotifier` for tests. No em dashes, no emojis, no tables. | HTTP (Telegram only) | no |
| `backtest.py` | Walk-forward engine on `FakeBroker` + `HistoricalSource`. Signals at bar close t, fills at t+1 open. | disk | no |
| `report.py` | Metrics per `<backtest_protocol>` from the ledger. Also `fulcrum report` for live/paper. | ledger read | no |
| `cli.py` / `__main__.py` | `python -m fulcrum <backtest|run|status|report|selftest>`. `run` defaults to `--dry-run` unless `mode = "paper"`. | argparse | no |
| `creds.py` | Loads DPAPI `.cred` files via PowerShell `Import-Clixml`, same pattern as the old MCP server. Never logs values. | subprocess | no |

Dependencies expected: `pandas`, `numpy`, `requests`, `anthropic`, `pytest`, `detect-secrets`. `tomllib` is stdlib in 3.11. Nothing else unless it earns its place and is written into DECISIONS.md.

---

## 2. Interfaces (written first so parallel work has ground to stand on)

### 2.1 Broker

```python
class Broker(Protocol):
    def account(self) -> Account                       # equity, cash
    def positions(self) -> list[Position]              # symbol, qty, avg_entry, market_value
    def submit(self, order: OrderRequest) -> OrderAck   # OrderRequest carries client_order_id
    def order_by_client_id(self, client_order_id: str) -> Order | None
    def open_orders(self) -> list[Order]
    def cancel(self, order_id: str) -> None
    def latest_price(self, symbol: str) -> Quote
```

`OrderRequest`: `client_order_id`, `symbol`, `side`, `notional_usd` or `qty`, `kind` (always market in v1), `purpose` (entry | increase | exit | stop). `purpose` is what lets `risk.py` exempt exits from caps without a bypass flag.

`FakeBroker` fill model: fill at next available price, plus configured slippage (DEFAULT 10 bps against the trade), minus configured taker fee. Deterministic given the bar sequence. Records fees per fill so the ledger and the backtest share one accounting path.

`AlpacaBroker.__init__` asserts `base_url == PAPER_URL` (constant, not config). Any other value raises before a single request is made.

### 2.2 LLM filter

```python
class Filter(Protocol):
    mode: Literal["off", "shadow", "active"]
    def evaluate(self, candidates: list[Candidate], context: MarketContext) -> FilterResult
```

`Candidate`: symbol, direction (long), conviction, recent price and vol stats, stored plan for any existing position. `FilterResult`: list of `Verdict {symbol, verdict: allow|veto, reason, confidence, invalidation}` plus `error: str | None` and `latency_ms`. Any schema failure, timeout, API error, or refusal stop reason produces `error` and an empty verdict list; the caller treats that as allow-all and logs it (FAIL OPEN).

The filter receives **entries and increases only**. Exits, stops, and kill-switch actions never construct a `Candidate`.

### 2.3 Risk

```python
def check_order(req: OrderRequest, state: PortfolioState, cfg: RiskConfig, ks: KillSwitch) -> RiskDecision
```

Single entry point. Every order passes through it, including `FakeBroker` orders in backtests. `RiskDecision` is `approve(req')`, `clamp(req', reason)`, `split(reqs, reason)`, or `reject(reason)`. For `purpose in {exit, stop}`: caps and clamps are skipped; the only possible outcome is approve. There is no parameter that skips this function.

`KillSwitch`: reads high-water mark and state from the ledger. `tripped()` is checked every tick. Trip persists to the ledger and fires a notification. There is no `reset()` method in code. Reset is Dave creating `~/.fulcrum/agent_kill_reset` by hand; the bot checks for the file, logs the reset event, and clears state. The code path that would create that file does not exist.

### 2.4 Ledger (SQLite schema, v1)

All tables carry `created_at` (UTC ISO). Rows are inserted, never updated, except `positions_snapshot` and `kill_switch_state` which are current-state tables.

| Table | Key columns | Notes |
|---|---|---|
| `runs` | run_id, started_at, mode, config_hash, git_sha | one row per process start |
| `decisions` | decision_id, run_id, cycle_ts, symbol, conviction, target_weight, current_weight, action, thesis, stop_price, invalidation | one row per symbol per rebalance |
| `filter_verdicts` | verdict_id, decision_id, mode, verdict, reason, confidence, invalidation, latency_ms, error, raw_json | every filter call, including errors |
| `orders` | client_order_id (PK), decision_id, symbol, side, purpose, notional_usd, qty, broker_order_id, status, submitted_at | `client_order_id` is deterministic: `sha1(run_scope + decision_id + symbol + side + purpose)[:32]` |
| `fills` | fill_id, client_order_id, qty, price, fee_usd, slippage_bps_est, filled_at | from broker; FIFO lot matching computed in `report.py` |
| `positions_snapshot` | symbol, qty, avg_entry, market_value, stop_price, entry_decision_id, as_of | rewritten each tick |
| `equity_curve` | ts, equity, cash, gross_exposure, hwm, drawdown | every tick |
| `ghost_book` | ts, book (real | counterfactual), equity, positions_json | counterfactual applies or ignores vetoes depending on mode |
| `kill_switch_state` | id=1, tripped, tripped_at, hwm, drawdown_at_trip, reset_at | single row |
| `kill_switch_events` | event_id, ts, kind (trip | reset | check), hwm, drawdown, note | append-only |
| `alerts` | alert_id, ts, kind (fee_budget | slippage | stale_data | kill | error), payload | what notify sent |
| `shadow_daily` | ts, symbol, conviction, verdict, reason | shadow-mode daily hypothetical filter runs |

---

## 3. Strategy as it will be coded (DEFAULTS from spec, all in config)

- Universe: BTC, ETH, SOL plus up to 5 more Alpaca USD pairs passing a documented liquidity screen (DEFAULT screen: 30-day median daily USD volume above a config threshold on the chosen data source; no stablecoins; hard cap 8).
- Features at bar close t: trailing returns over 7/14/21/28 days, 50-day SMA, 30-day realized vol (daily returns, annualized with 365), ATR-14.
- Signal: conviction = mean of sign(ret_k > 0) over k in {7,14,21,28}, set to 0 if close < SMA50. Long or flat only.
- Sizing: raw_w = conviction * (per_asset_vol_budget / realized_vol); per_asset_vol_budget derived from 20% portfolio target across the universe; clamp to 15% per asset; scale down to 60% gross if exceeded; remainder cash.
- No-trade band: skip any adjustment below 2% of equity or below broker minimum notional.
- Rebalance: weekly, on a configured weekday and UTC hour.
- Exits: (a) flat signal at rebalance; (b) per-position stop at entry minus 2.5 x ATR-14, ratchets up only, checked every 15-minute tick; (c) kill switch at 15% drawdown from HWM.
- Order clamp 5% of equity per order; oversized changes split across ticks. Trade cap 10 per rolling 24h. Exits and stops exempt from both.
- Fee alarm: trailing annualized fees above 3% of equity halves rebalance frequency until reviewed. Slippage alarm: realized above 2x modeled.

---

## 4. Build order

Interfaces and fakes first, then the money-critical pure code, then the stateful edges, then the LLM, then polish. Commit at each green step.

1. **Skeleton.** `pyproject`-free layout, `requirements.txt` pinned, `.gitignore` (Nomos baseline + venv/, data/, *.sqlite, __pycache__/), `config.example.toml`, `.pre-commit-config.yaml` with `detect-secrets`, venv created, `git init`, baseline secrets scan, first commit.
2. **Config + ledger + interfaces.** Typed config with fail-fast validation; SQLite schema with migrations-by-version; `Broker` and `Filter` protocols; `FakeBroker`, `FakeFilter`, `FakeNotifier`. Tests: config rejects every bad value class; schema round-trip.
3. **Features + signal + sizing + risk (pure).** Property-style tests: exposure never exceeds caps; exit never blocked; tripped kill switch never approves an entry; deterministic `client_order_id`; no-trade band; split logic. Hand-computed fee and slippage examples.
4. **Historical data + backtest engine.** Choose and verify the key-free source (see section 6). Lookahead guard test: a feature at t that reads any bar after t fails the test. Walk-forward runner. Benchmarks: BTC buy-and-hold, equal-weight buy-and-hold, cash. Report per protocol, including the stated biases. Write `docs/BACKTEST.md` honestly, whatever it shows.
5. **AlpacaBroker (paper).** Port and verify the symbol map, order submission, order lookup by `client_order_id`, positions, account. Paper-only lock test. All tests against a recorded fake HTTP layer; no network in tests.
6. **Scheduler + reconciliation + notify.** Start-up reconcile: ledger orders vs broker orders by `client_order_id`; unknown broker orders are logged and alarmed, never silently adopted. Stale-data guard. Stop tick. Kill-switch tick. Telegram templates.
7. **LLM filter + ghost book.** Load the `claude-api` skill first. `ClaudeFilter` with strict JSON schema, timeout, fail-open, injection boundary tags. System prompt: role, veto-only contract, distilled scam and DYOR heuristics, "insufficient information, allow". Shadow-mode daily hypothetical runs. Ghost book accounting in both modes.
8. **CLI + docs + handoff.** `selftest`, `--dry-run` cycle demonstrated. `README.md`, `CLAUDE.md`, `Revenant_Progress_Report.md`, `docs/HANDOFF.md`, `Start-Fulcrum.ps1`, `tasks/todo.md`, `tasks/lessons.md`.
9. **Adversarial review (three required passes)** with independent subagents: lookahead and accounting in the backtest; order bypass or double-fire through the risk layer; credential handling and secret hygiene. Fix confirmed findings, rerun everything.

Parallelism: steps 3 and 4 can run in parallel against the step-2 interfaces; step 5 can run alongside step 4 once `FakeBroker` exists. Everything else is sequential.

---

## 5. Where I think the spec is wrong or under-specified, in one paragraph each

**Stops must be synthetic, not resting broker orders.** The spec says stops are "checked every scheduler tick", which is the right design, but it does not say why: Alpaca's crypto order-type support for native stop or stop-limit sells is exactly the kind of fact the report warns moves, and a resting stop that is silently unsupported would be a hole under the whole risk layer. I will implement stops as bot-side: the tick compares last price to the stored stop and submits a market exit with `purpose = stop`. Whether Alpaca also supports a native crypto stop is a fact to verify (section 6); if it does, it becomes a config-optional belt-and-braces, never the primary mechanism.

**The portfolio vol target will rarely bind.** With a long-or-flat book of three to eight highly correlated majors, the 15% per-asset cap and 60% gross cap will usually bind before the 20% portfolio vol target does. That is fine, the caps are the safety, but the plan should say so: the backtest will report which constraint bound each week so Dave can see whether the vol target is doing anything. No change to the decision; this is a measurement note.

**The Expertise skill's "Owner Questions" partly conflict with the spec's DEFAULTs.** The skill says the ATR multiplier and stop mechanics must not be filled in with guessed defaults. The spec sets them as prompt-author DEFAULTs and says do not re-litigate. For this repo the spec wins. I will record in `DECISIONS.md` that 2.5 x ATR-14, the 20% vol target, and the 15% kill threshold are starting defaults chosen by the prompt author, config-overridable, and not owner-validated edge parameters. The skill's third question (wallet-filter thresholds) does not apply to v1, which has no wallet signals.

**Shadow week honesty.** As the spec itself says: seven days at weekly cadence is one rebalance. The daily hypothetical run makes the week informative about plumbing and verdict sanity, not edge. This goes in the README verbatim.

---

## 6. Facts to verify against live documentation before the dependent code is written

Read-only GETs against official docs. Each gets a line in `DECISIONS.md` with source URL and as-of date.

**Alpaca trading (drives broker, risk, fee model):**
1. Crypto fee schedule, base tier maker and taker in bps; how the fee is charged; whether paper trading simulates crypto fees or the bot must model them.
2. Supported crypto order types and time-in-force values; whether native stop or stop-limit sells exist for crypto (see section 5).
3. Minimum order size, minimum notional, fractional support, qty and price increments per symbol (assets endpoint fields).
4. `client_order_id`: max length, uniqueness rules, response on duplicate submission, lookup endpoint. This decides the idempotency design.
5. Exact paper and live base URLs (the paper-only lock constant).
6. Confirmation that spot crypto cannot be shorted or margined.
7. Trading API rate limits.

**Alpaca market data (drives `AlpacaData`, stale-data guard):**
8. Crypto bars endpoint path and version, timeframes, max limit, pagination.
9. Whether crypto data endpoints need keys.
10. History depth and the daily-bar timestamp convention (UTC boundary).
11. Data rate limits on the free plan.
12. Current tradable USD crypto pairs; confirm BTC, ETH, SOL and the liquidity-screen candidates.

**Backtest data source (drives `HistoricalSource`):**
13. Choose one key-free public daily OHLCV source with history to at least 2020 for the majors; verify its terms permit programmatic retrieval and local caching for personal research; document pagination limits and a fallback source.
14. Identify the "March 2026 stress window" the report names: exact dates and what happened, from primary sources, so the backtest can include it.

**Anthropic API (drives `ClaudeFilter`):**
15. Current default model id, JSON-output method, timeout and stop-reason semantics, via the `claude-api` skill. No values from memory.

**Tooling:**
16. `detect-secrets` pre-commit wiring on Windows with a repo-local venv (hook command path).

---

## 7. What I will not do without asking

Everything on the spec's NOT GRANTED list: no deletes, moves, or renames; no writes outside this repo; no reading or logging any credential value; no calls to Alpaca, Telegram, or Anthropic with real keys; no long-running process; no scheduled task. If a step needs one of these, I finish everything else first and list it in the summary.

---

## 8. Definition of done for v1

- All tests green, run offline, output shown.
- `python -m fulcrum selftest` and one `--dry-run` cycle demonstrated.
- Honest backtest executed and written to `docs/BACKTEST.md`, result stated plainly at the top of the summary even if it is bad.
- Three adversarial review passes completed and findings resolved or explicitly declined with reasons.
- README, CLAUDE.md, HANDOFF.md, Progress Report, launcher, tasks files present.
- Local git history at each green milestone. No remote.
