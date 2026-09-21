# Lessons

Read at the start of every session in this repo. After any correction from Dave, add the pattern and the rule that prevents a repeat.

## 2026-09-21

- **Muninn's filename.** The machine index binds `MUNNIN.md`; the file on disk is `MUNINN.md`. Read the one that exists, surface the mismatch, do not fix a gated file uninvited.
- **A response can be stopped mid-turn by a safety classifier.** When that happens, tool calls in that response did not run. Re-check disk state before assuming anything was written, and do not reissue the same content. Keep research prompts plainly worded and about documentation.
- **Two Fulcrums are deliberately severed** (Muninn, 2026-07-17): the trading agent does not know the CFO prompt exists. Do not pull the CFO prompt into this repo unless Dave directs it.
- **Spec DEFAULTs vs the Expertise skill's "do not fill in" list.** For this repo the spec wins; record the defaults as prompt-author starting values in DECISIONS.md so both are honored.
- **"Tests pass" from a build agent is a claim, not a fact.** Run the suite yourself before committing anything an agent wrote. The core agent's tests were green; the adversarial agent then proved seven real defects with failing tests (a crash after kill-switch reset, NaN making every cap inert, a NaN stop that never fires). The hostile pass earned its cost; keep it in the process.
- **"Test-harness problem, not a code bug" is also a claim.** The engine agent labelled 9 failures that way. Triage each one against the spec before accepting the label.
- **Fake secrets in tests trip the secret scanner.** Name test constants after what they are structurally (first field, second field), not "secret" or "key". The block stands; fix the cause, never allowlist.
- **Dave's global gitignore excludes `CLAUDE.md` and `HANDOFF.md`.** They live on disk in the repo folder but are not tracked. His rule; do not force-add.
- **A classifier stop mid-write leaves a truncated file.** Check the tree after any interrupted response; a fragment that does not parse must be rewritten by someone else, not salvaged.
