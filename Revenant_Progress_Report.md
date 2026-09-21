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

## Final state

(Filled at the end of the build: test count, backtest verdict, review findings, commit list.)
