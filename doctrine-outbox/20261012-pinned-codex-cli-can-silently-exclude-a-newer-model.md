---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/pinned-codex-cli-can-silently-exclude-a-newer-model
rule: a pinned Codex CLI is proven only for the models its promotion exec-probed; a lane whose resolved model is not on that list runs on the unpinned CLI, with the receipt naming `PIN_MODEL_UNPROBED`, never on the pin by default.
mechanism: the models cache is one file shared by every Codex CLI on the machine, and each CLI rewrites it with the catalogue its own version was offered. Measured 2026-10-12: its `client_version` read 0.160.1 (the pin) at one read and 0.162.0 (the global CLI) at the next, minutes apart. A tier resolved from it can name a model the pinned CLI was never offered or never ran, and nothing errors at resolution. The promotion that wrote the pin had probed one model (the luna tier) while the sol tier had moved to `gpt-6.1-sol`; 103 sol lanes ran it on the pin with no probe behind them.
applies: mlv-app
check: set the pin's `probedModels` to `["gpt-6-luna"]` and launch a read-only sol lane: the receipt's `codexExe.pinState` must read `PIN_MODEL_UNPROBED`, its path must be the global CLI, and one `[codex-pin]` line must say why. Re-promote with every lane tier and the same lane must read `PINNED`.
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_lane_containment.py `test_codex_pin_runs_the_global_cli_when_the_resolved_model_was_never_probed` and `test_codex_pin_promoter_refuses_the_whole_pin_when_one_tier_model_cannot_exec`
