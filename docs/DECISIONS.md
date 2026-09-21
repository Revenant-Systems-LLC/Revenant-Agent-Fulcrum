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

## D-006 Pinning by freeze (2026-09-21)
`requirements.in` lists direct dependencies with reasons; `requirements.txt` is `pip freeze` from the repo venv, so every transitive version is pinned. Reason: "pinned" means reproducible, and a lockfile is the simplest reproducible thing.

## D-007 Pre-commit without the pre-commit framework (2026-09-21)
The `pre-commit` framework installs hook environments under the user profile on C:, which the grant forbids. Instead `detect-secrets` is installed in the repo venv and a plain `.git/hooks/pre-commit` shell script (tracked copy at `scripts/hooks/pre-commit`) runs `detect-secrets-hook` against staged files with `.secrets.baseline`. Same guarantee, zero writes outside the repo.

## D-008 Ledger is the source of truth for the bot's own state; the broker is the source of truth for fills (2026-09-21)
On start, reconcile walks every ledger order without a terminal status and asks the broker for it by client_order_id. Broker orders with no ledger row are logged as `alerts.kind = "unknown_order"` and never adopted silently.
