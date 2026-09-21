# Fulcrum v1 build checklist

Plan: `docs/PLAN.md`. Spec: `BUILD-PROMPT.md`. Judgment calls: `docs/DECISIONS.md`.

## 1. Skeleton
- [x] Copy report to docs/research (hash verified)
- [x] docs/PLAN.md approved by Dave 2026-09-21
- [x] venv at ./venv, direct deps installed
- [x] .gitignore (Nomos baseline + runtime)
- [x] requirements.in
- [x] requirements.txt pinned from freeze
- [x] config.example.toml (VERIFY values confirmed by D-009; fee 15/25 bps tier 1)
- [x] detect-secrets baseline + pre-commit hook (repo-local, no global installs; blocked one false positive, fixed the cause)
- [x] git init + first commit 78da09d; second e298b39

## 2. Foundations (sequential, main thread)
- [x] fulcrum/models.py
- [x] fulcrum/config.py + tests
- [x] fulcrum/ledger.py + tests
- [x] fulcrum/broker.py: Broker protocol + FakeBroker + tests
- [x] fulcrum/llm_filter.py: Filter protocol + FakeFilter
- [x] fulcrum/notify.py: Notifier protocol + FakeNotifier
- [x] fulcrum/creds.py (DPAPI loader, never logs values)

## 3. Live-doc verification (workflow wf_fb6fc303-25c, done, 8 agents, all claims confirmed by re-read)
- [x] Alpaca orders/fees/idempotency facts into DECISIONS.md (D-009)
- [x] Alpaca market data facts into DECISIONS.md (D-010)
- [x] Backtest data source chosen and terms recorded (D-011: Coinbase primary, Binance Vision fallback)
- [x] Stress window + regimes recorded (D-012: report's "March 2026" is misdated; crash was Feb 2026)

## 4. Parallel modules (against section-2 interfaces)
- [x] features.py + signal.py + sizing.py + risk.py with property tests (53 tests)
- [ ] data.py HistoricalSource (agent running)
- [ ] backtest.py + report.py + lookahead guard test
- [ ] AlpacaBroker (paper-only lock) + AlpacaData with recorded-HTTP tests (agent running)
- [ ] engine.py (agent rewriting after an interrupted write) then scheduler.py + telegram.py
- [ ] adversarial tests over the risk layer (agent running)

## 5. Filter and measurement
- [x] Load claude-api skill; ClaudeFilter with strict JSON, fail-open, injection tags (model claude-opus-5)
- [ ] Ghost book accounting in both modes (in engine)
- [ ] Shadow-mode daily hypothetical run (in scheduler)

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
