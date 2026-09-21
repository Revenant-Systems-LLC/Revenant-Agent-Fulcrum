# Decisions and assumptions

Every judgment call made while building autonomously, with the reason. Facts verified against live docs carry a source URL and an as-of date. Entries are appended, never rewritten; a superseded entry gets a dated note.

## D-001 Strategy DEFAULTs are prompt-author starting values (2026-09-21)
The 7/14/21/28-day ensemble, 50-day MA gate, 20% vol target, 15%/60% caps, 2% band, 2.5 x ATR-14 stop, 15% kill threshold, 10 trades/24h, 5% order clamp, 3% fee alarm, and 2x slippage alarm come from BUILD-PROMPT.md as DEFAULTs "chosen by the prompt author, not by the report." They are config values. They are not owner-validated edge parameters and the Expertise "Fulcrum Crypto Trader" skill is right that nobody should mistake them for such. Both positions are honored by recording this here and by making every one of them a `config.toml` field.

## D-002 Stops are synthetic (2026-09-21)
The stop is enforced by the scheduler tick: compare last price to the stored stop level, and if breached submit a market order with `purpose = "stop"`. It does not depend on the broker accepting a resting stop order for crypto. If Alpaca's docs confirm a native crypto stop type, it may be added later as an optional secondary safety, never as the primary mechanism. Reason: a resting stop that turns out to be unsupported fails silently; a tick that runs every 15 minutes fails loudly.

## D-003 Order `purpose` replaces any bypass flag (2026-09-21)
`OrderRequest.purpose` is one of entry, increase, exit, stop. `risk.check_order` exempts exit and stop from the order clamp and the 24h cap by purpose. There is no `skip_checks` parameter anywhere. The LLM filter only ever sees entry and increase candidates, so it cannot construct or influence an exit.

## D-004 Kill switch reset has no code path (2026-09-21)
`KillSwitch` has `trip()`, `tripped()`, and `consume_reset_file()`. The last one reads `~/.fulcrum/agent_kill_reset`, logs a reset event, deletes nothing, and marks the file consumed by recording its mtime in the ledger so the same file is not honored twice. The bot never creates that file. Dave creates it by hand.

## D-005 Deterministic client_order_id (2026-09-21)
`client_order_id = sha1(f"{ledger_scope}|{decision_id}|{symbol}|{side}|{purpose}|{seq}").hexdigest()[:N]` where N is bounded by Alpaca's documented max length (verified in D-0xx) and `seq` is the split index for clamped orders. Same decision, same order, same id, so a crash between submit and ledger write cannot double-submit: reconcile finds the order by client id on restart.

## D-005a Correction: purpose is not part of the client_order_id (2026-09-21, engine triage)
Found while making the crash-and-rerun test pass: a buy's purpose is "entry" when no position exists and "increase" when one does. If the first order fills and the process dies before the ledger row is written, the rerun computes "increase" for the same decision, gets a different id, and buys twice. The id is now `sha1(scope|decision_id|symbol|side|seq)`. The decision id already pins the cycle and symbol, side separates a buy from a sell of the same decision, and purpose is carried on the order row for the risk layer, not in the key. `ledger.record_decision` is `INSERT OR IGNORE` so a rerun with identical inputs writes nothing and raises nothing.

## D-006 Pinning by freeze (2026-09-21)
`requirements.in` lists direct dependencies with reasons; `requirements.txt` is `pip freeze` from the repo venv, so every transitive version is pinned. Reason: "pinned" means reproducible, and a lockfile is the simplest reproducible thing.

## D-007 Pre-commit without the pre-commit framework (2026-09-21)
The `pre-commit` framework installs hook environments under the user profile on C:, which the grant forbids. Instead `detect-secrets` is installed in the repo venv and a plain `.git/hooks/pre-commit` shell script (tracked copy at `scripts/hooks/pre-commit`) runs `detect-secrets-hook` against staged files with `.secrets.baseline`. Same guarantee, zero writes outside the repo.

## D-008 Ledger is the source of truth for the bot's own state; the broker is the source of truth for fills (2026-09-21)
On start, reconcile walks every ledger order without a terminal status and asks the broker for it by client_order_id. Broker orders with no ledger row are logged as `alerts.kind = "unknown_order"` and never adopted silently.

## D-009 Alpaca trading facts, verified against official docs 2026-09-21 (research + independent adversarial re-read, all confirmed)
- Crypto fees, tier 1 (30-day volume under $100k): maker 0.15%, taker 0.25% (15 / 25 bps). https://docs.alpaca.markets/us/docs/crypto-fees
- Fee is charged on the credited asset ("what you receive"): BTC on a BTC/USD buy, USD on a sell. Fees are calculated and posted end of day, not on the fill. Same page.
- Whether PAPER simulates crypto fees is undocumented in either direction. Consequence: the ledger models fees itself from `taker_fee_bps` on every fill, in USD terms, and marks them modeled. https://docs.alpaca.markets/us/docs/paper-trading
- Crypto order types: market, limit, stop_limit only. time_in_force: gtc, ioc only. Plain stop and trailing_stop are not accepted for crypto, which confirms D-002 (synthetic stops). https://docs.alpaca.markets/us/reference/postorder
- Per-symbol `min_order_size`, `min_trade_increment`, `price_increment` come from GET /v2/assets (crypto-only fields). Per-order maximum $200k notional. Read at startup, never hardcode. https://docs.alpaca.markets/us/reference/get-v2-assets-1
- `client_order_id`: max 128 chars; duplicate against an active order returns HTTP 422 "client_order_id must be unique". Lookup: GET /v2/orders:by_client_order_id?client_order_id=... (colon in the path). https://docs.alpaca.markets/us/reference/getorderbyclientorderid
- Paper base URL https://paper-api.alpaca.markets; live https://api.alpaca.markets. The live string appears nowhere in this codebase except in this line and in the lock test that asserts it is rejected.
- Spot crypto cannot be shorted or bought on margin; sizing reads `non_marginable_buying_power`. https://docs.alpaca.markets/us/docs/crypto-trading
- Trading API limit 200 requests/minute/account, HTTP 429 on breach. https://alpaca.markets/support/usage-limit-api-calls

## D-010 Alpaca market data facts, verified 2026-09-21
- Bars: GET https://data.alpaca.markets/v1beta3/crypto/us/bars?symbols=BTC/USD,...&timeframe=1Day&start=...&limit=10000, paginate with `page_token`. https://docs.alpaca.markets/reference/cryptobars-1
- Stop-check price: GET https://data.alpaca.markets/v1beta3/crypto/us/latest/trades?symbols=... (one call for the whole book; a printed trade, not a quote). https://docs.alpaca.markets/reference/cryptolatesttrades-1
- Historical crypto data is the documented exception to the auth requirement; the bot sends its paper keys anyway. https://docs.alpaca.markets/docs/about-market-data-api
- The 1Day crypto bar boundary and time zone are UNDOCUMENTED, and so is history depth. The data layer records the observed bar timestamps and the observed first bar on first contact and writes them to the ledger alerts table as `data_probe`; nothing assumes UTC midnight until observed.
- Rate limit for crypto data is not documented; budget under 200/min and back off on 429.

## D-011 Backtest data source: Coinbase Exchange public candles primary, Binance Vision bulk files fallback (2026-09-21)
Coinbase Exchange `GET /products/{BTC-USD}/candles?granularity=86400&start&end`: key-free, 300 candles per request, 10 requests/second per IP, US-domiciled, and its Market Data Terms grant a licence for "personal or research purposes" with no redistribution and no AI/ML training. True USD quotes. https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles and https://www.coinbase.com/legal/market_data
Fallback and cross-check: data.binance.vision monthly 1d kline zips (key-free, US-reachable, USDT quotes, BTCUSDT from 2017-08, SOL/AVAX/UNI from 2020). Its terms PDF is labelled "[Draft]" and "Non-Commercial"; treat as the weaker source.
Rejected: Kraken (only the last 720 daily bars, cannot page back), CoinGecko free (2-year cap and terms forbid storing data), Yahoo (terms forbid automated collection), CryptoCompare/CoinDesk Data (unreadable, free tier reportedly retired), Stooq (unverifiable, CAPTCHA-gated key reported), Alpaca's own bars (history depth unknown and likely short).
Coinbase listed SOL, AVAX, DOT, UNI in 2021, which covers every regime fold in D-012. Docs warn "Historical rate data may be incomplete": the loader detects missing days, reports them, and never fills them silently.

## D-013 Layman usability is a requirement (Dave, 2026-09-21 11:10)
Dave: "this must be usable and understandable by the layman." Consequences, all in scope for v1: a numbered-menu launcher (`Start-Fulcrum.ps1`) so no command has to be memorized; `status` and `report` output written as plain sentences with a one-line "what this means" where a number needs it; Telegram messages likewise; the README opens with a plain-English explanation and a short glossary before any technical section; HANDOFF.md is a checklist of exactly what to type or click and what should appear. Technical detail is not removed, it is moved below the plain layer. Every user-facing string is checked for jargon before done.

## D-012 Regime folds for the walk-forward backtest (2026-09-21)
The report's "March 2026 stress window" does not match primary sources. Binance Research's monthly reports show March 2026 was a recovery month (market cap +1.8%, BTC +1.51%, ETH +6.27%) with a mild 17 to 28 March pullback (BTC about -12%, ETH about -16%) driven by the US-Iran oil shock and a hawkish Fed repricing. The severe event was February 2026 (market cap -22.6%, BTC -23.5%, ETH -30.8%, Fear and Greed bottomed at 5); Q1 2026 was BTC's worst first quarter since 2018. Folds used, non-overlapping:
- Stress: 2026-02-01 to 2026-03-31 (covers both the February crash and the March pullback).
- Trending: 2025-04-07 to 2025-10-06 (BTC $74,637 intraday low to the $126,198 all-time high, about +69%).
- Choppy: 2023-08-01 to 2023-10-15 (BTC in a $25k to $30k range for about 59 days), extendable back to 2023-04-01.
This is flagged to Dave in the build summary as a question about which window the report's author meant.

## D-014 Defect fixes to make test_adversarial_engine.py pass (2026-09-21)
Five defects found and fixed per the adversarial pass:

1. **Filter exception handling (fail open)**: Filter exceptions now caught in `Engine.rebalance` step 8 with try/except, logged, alert recorded as "filter_error", and exits are sent first to guarantee they are not blocked by filter failures.

2. **Decision ID keyed to scheduled slot, not wall clock**: `Engine._decision_id` now calls `last_scheduled_weekly(cycle_ts, weekday, hour)` to build the ID from the scheduled weekly slot instead of `cycle_ts.isoformat()`. Crash-and-restart cycles that rebalance twice in one week now produce identical order IDs and the duplicate is caught by `ledger.record_order_intent` returning False.

3. **Deterministic stop order IDs with reconciliation**: Stop order client_order_ids are now keyed to (scope, entry_decision_id or "held", symbol, "sell", seq). Before submitting, `Engine.tick` queries ledger for prior stops with that key, reconciles their status against the broker (filled, pending, or rejected), and only sends with seq+1 if the prior attempt was rejected or never reached the venue. Terminal statuses stop the loop.

4. **Trade cap filters on engine clock (decided_at), not wall clock**: Added `decided_at TEXT NOT NULL` column to orders table with schema migration (v1 to v2). `Ledger.record_order_intent` now requires `decided_at: datetime` parameter (the engine's decision time). `risk_increasing_orders_since` filters on `decided_at` instead of `created_at`, making the 24h cap work correctly in backtests with historical time. All callers of `record_order_intent` updated to pass `decided_at=now`.

5. **Ghost book alerts on failure**: `GhostBook.submit` now accepts `ts: datetime | None` parameter and records `ledger.record_alert(ts, "ghost_order_failed", {symbol, reason})` on exception instead of only logging. This makes failed counterfactual trades visible in reports so the filter's measurement is not silently incomplete.

## D-016 Paper account: Fulcrum2.0, the $1,000,000 one (Dave, 2026-09-21 14:38)
Dave created two new Alpaca paper accounts, Fulcrum1.0 ($100k) and Fulcrum2.0 ($1M), and chose 2.0. The old bot's account is not accessible to him and is not used. Consequence: nothing in the code depends on account size; the 5% per-order clamp is $50,000 at this size, under Alpaca's $200,000 per-order cap (D-009). Backtest comparisons are percent-based and unaffected. Only the 2.0 keys go into `alpaca_agent.cred`.
