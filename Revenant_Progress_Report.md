# Revenant Agent Fulcrum: progress report

**As of:** 2026-09-21 (build day one). **Author:** Claude Fable 5.1, lead engineer for this build, under BUILD-PROMPT.md v1.0 approved by Dave the same day.

## Where it stands, in plain words

The bot is built, tested, and committed locally. It has never traded, not even on paper, because that first step is Dave's by design (docs/HANDOFF.md). Every module runs offline against fakes; the only real thing that has run is `selftest`, `status`, and `report` on an empty ledger, all of which read as intended. The three credential files do not exist yet, and `selftest` says so by name.

## What was built (v1 scope, all delivered unless marked)

- Rules-based weekly momentum strategy on Alpaca paper crypto, volatility-sized, capped, with ratcheting stops and a 15% kill switch that only Dave can reset.
- The AI filter that can only veto, shipping in shadow mode, with the ghost book that measures it for as long as the bot lives.
- Alpaca paper adapter with a hard paper-only lock, Coinbase and Binance Vision history loaders with a local cache, SQLite ledger, Telegram notifications, a numbered launcher menu, and plain-English `selftest`, `status`, `report` screens.
- The honest backtest across three regime folds: see docs/BACKTEST.md (state recorded in the final section below).
- Documentation: README (plain English first), HANDOFF (click by click), PLAN, DECISIONS (every judgment call and verified fact with sources), CLAUDE.md, tasks/todo.md, tasks/lessons.md.

## Verification performed

- Test suite: see the count in the final section; all offline, none touch the network or a credential.
- Facts verified against official docs on 2026-09-21 by a research agent and an independent adversarial re-read: Alpaca fees, order types, minimums, client id rules, paper URL, data endpoints, backtest data terms, regime dates (DECISIONS D-009 to D-012).
- Adversarial passes: the risk layer (33 hostile tests; 7 real defects found and fixed before any commit carried them), the engine end to end, the backtest accounting, and credential hygiene (results in the final section).

## Open items only Dave can do

In order, per docs/HANDOFF.md: create the second Alpaca paper account; create the three DPAPI credential files; run `selftest`; run one practice cycle; flip `mode = "paper"`; run the first paper cycle and check Alpaca and Telegram; start the shadow week; on day 7, read the report and decide on the filter.

## Questions for Dave

1. The research report's "March 2026 stress window" does not match primary sources: March 2026 was a recovery month and the crash was February 2026 (D-012). The backtest uses February 1 to March 31 as the stress fold. Which did the report's author mean?
2. Your global gitignore excludes `CLAUDE.md` and `HANDOFF.md`, so both exist in the folder but are not in git. Intentional?
3. Muninn is bound in the machine index as `MUNNIN.md` but the file is `MUNINN.md`; Heimdall reports it dead for that reason. Your call, it is a gated file.

## Final state (2026-09-21 14:15)

- **Tests:** 579, all green, all offline. Includes four adversarial suites written by independent reviewers (risk layer, engine end to end, backtest accounting, security), every one of which found real defects that were fixed before this line was written.
- **Backtest, run for real on Coinbase history:** 2023-01-01 to 2026-09-21 the rules made 46.8% with a worst fall of 12.6% and a net Sharpe of 1.11; holding Bitcoin made 418.8% with a worst fall of 53.1% and a Sharpe of 1.18. On the pre-registered test (Sharpe) the rules came in just below holding Bitcoin; on drawdown they were far safer. In the stress fold (Feb to Mar 2026) the rules lost 3.4% against Bitcoin's 11.3%; in the trending fold they made 8.0% against 57.7%; over 2020 to 2026 (includes the 2022 bear) Sharpe 1.38 against 0.92. The account was only about 13% invested on average because the 20% volatility target binds every week. Nothing was tuned. Full write-up: docs/BACKTEST.md.
- **Demonstrated by hand:** `selftest` (all checks pass except the three credential files, which do not exist yet), `status` and `report` on an empty ledger, `backtest` and `backtest --folds` with real data. The practice cycle (`run --once --dry-run`) is exercised by tests against fakes; the first real one needs the Alpaca credential file and is Dave's step 4.
- **Commits (local, branch main):** 78da09d, e298b39, 8e0ecfd, d11b3cb, a7d4faf, 7662ff4, 2191d03 (Dave's own, "Pushing current build"), 3a93b00. Dave added the remote `Revenant-Systems-LLC/cryptobro-fulcrum`; the last commit has not been pushed by the agent (not granted).
- **Follow-ups (not built, on purpose):** the ghost book's mirrored orders were refused by its own pretend broker a few dozen times over a multi-year backtest (`ghost_order_failed` alerts), so its counterfactual drifts on long runs; look before judging the filter on it. The volatility-target split (20% across N coins) is the reason exposure is low; that is a config decision for Dave, to be made before more paper trading. `CLAUDE.md` and `docs/HANDOFF.md` are excluded from git by Dave's global ignore.
