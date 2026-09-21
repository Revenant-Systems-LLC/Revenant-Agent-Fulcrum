# Fulcrum v1 build checklist

Plan: `docs/PLAN.md`. Spec: `BUILD-PROMPT.md`. Judgment calls: `docs/DECISIONS.md`.

## 1. Skeleton
- [x] Copy report to docs/research (hash verified)
- [x] docs/PLAN.md approved by Dave 2026-09-21
- [x] venv at ./venv, direct deps installed
- [x] .gitignore (Nomos baseline + runtime)
- [x] requirements.in
- [ ] requirements.txt pinned from freeze
- [ ] config.example.toml (draft written; fill VERIFY values after doc check)
- [ ] detect-secrets baseline + pre-commit hook (repo-local, no global installs)
- [ ] git init + first commit

## 2. Foundations (sequential, main thread)
- [ ] fulcrum/models.py
- [ ] fulcrum/config.py + tests
- [ ] fulcrum/ledger.py + tests
- [ ] fulcrum/broker.py: Broker protocol + FakeBroker + tests
- [ ] fulcrum/llm_filter.py: Filter protocol + FakeFilter
- [ ] fulcrum/notify.py: Notifier protocol + FakeNotifier
- [ ] fulcrum/creds.py (DPAPI loader, never logs values)

## 3. Live-doc verification (workflow wf_fb6fc303-25c, background)
- [ ] Alpaca orders/fees/idempotency facts into DECISIONS.md
- [ ] Alpaca market data facts into DECISIONS.md
- [ ] Backtest data source chosen and terms recorded
- [ ] March 2026 stress window + regimes recorded

## 4. Parallel modules (against section-2 interfaces)
- [ ] features.py + signal.py + sizing.py + risk.py with property tests
- [ ] data.py HistoricalSource + backtest.py + lookahead guard test
- [ ] AlpacaBroker (paper-only lock) + AlpacaData with recorded-HTTP tests
- [ ] scheduler.py (reconcile, stop tick, kill tick) + TelegramNotifier

## 5. Filter and measurement
- [ ] Load claude-api skill; ClaudeFilter with strict JSON, fail-open, injection tags
- [ ] Ghost book accounting in both modes
- [ ] Shadow-mode daily hypothetical run

## 6. CLI and docs (layman-usable, D-013)
- [ ] cli: backtest | run | status | report | selftest, every screen in plain sentences
- [ ] Start-Fulcrum.ps1 numbered menu (check setup, practice cycle, paper trading, status, report, history test)
- [ ] README opens with plain-English "what it does / never does" + glossary
- [ ] HANDOFF.md as a click-by-click checklist
- [ ] selftest and one --dry-run cycle demonstrated
- [ ] docs/BACKTEST.md (honest)
- [ ] README.md, CLAUDE.md, Revenant_Progress_Report.md, docs/HANDOFF.md, Start-Fulcrum.ps1

## 7. Adversarial review (three required passes)
- [ ] Lookahead and accounting in backtest
- [ ] Risk-layer bypass or double-fire
- [ ] Credential handling and secret hygiene
- [ ] Findings fixed or declined with reasons; all tests rerun

## Review
(filled at the end)
