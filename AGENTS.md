# Workspace Notes For Codex

## Document Map

This file is an INDEX. It carries pointers and session-start procedure only; detail lives in `agents/` children. Governed by [docs/22-doc-fragmentation-policy.md](docs/22-doc-fragmentation-policy.md) (entry tier: 8 KB soft / 12 KB hard). Check with:

```bash
py -3 tools/docs/check_pinned_tokens.py
```

Section headings are stable IDs. Demoted sections and where they went:

| Section (stable ID) | Now lives in |
|---|---|
| Brokered Auto-Closeout | [agents/brokered-auto-closeout.md](agents/brokered-auto-closeout.md) |
| GUI Release Build Verification | [agents/release-and-regression.md](agents/release-and-regression.md) |
| Output-Regression Prevention -- "behavior-preserving" is the HIGHEST-risk class | [agents/release-and-regression.md](agents/release-and-regression.md) |
| Tier-B lane-health contract | [agents/lane-health.md](agents/lane-health.md) |
| Implemented Test Scaffold | [agents/testing-and-notes.md](agents/testing-and-notes.md) |
| Active Investigation Notes | [agents/testing-and-notes.md](agents/testing-and-notes.md) |
| Runtime helper | [agents/testing-and-notes.md](agents/testing-and-notes.md) |

### Pinned contract tokens

`tools/repo_hygiene/brokered_closeout.py` and `tools/repo_hygiene/test_brokered_closeout.py` assert these exact substrings against this file; losing one raises `closeout_tooling_stale` or reds the test suite. They stay resident here as stable IDs even though the surrounding prose was demoted. The list is derived from both surfaces, never hand-maintained -- re-derive and re-check with:

```bash
py -3 tools/docs/check_pinned_tokens.py
```

- `/api/closeout/actions` -- detail in agents/brokered-auto-closeout.md
- `/api/closeout/actions/preview` -- detail in agents/brokered-auto-closeout.md
- `/api/closeout/actions/requests` -- detail in agents/brokered-auto-closeout.md
- `action-request-history` -- detail in agents/brokered-auto-closeout.md
- `agentRemediationQueue.queueRoots` -- detail in agents/brokered-auto-closeout.md
- `boundedRunnerExitCodes` -- detail in agents/brokered-auto-closeout.md
- `canonical dashboard contract` -- detail in agents/brokered-auto-closeout.md
- `Closeout actors must be bounded at the process boundary` -- detail in agents/brokered-auto-closeout.md
- `Closeout Remediation Freeze` -- detail in agents/brokered-auto-closeout.md
- `closeoutCleanTruth` -- detail in agents/brokered-auto-closeout.md
- `dashboard-action-requests` -- detail in agents/brokered-auto-closeout.md
- `docs/autonomous-golden-authority.md` -- detail in agents/release-and-regression.md
- `Evidence-preserving transaction prune` -- detail in agents/brokered-auto-closeout.md
- `first landing was deliberately dormant while the tooling baseline remained mandatory` -- detail in agents/brokered-auto-closeout.md
- `Hard-clean final responses are blocked unless the repo-closed postcondition passes` -- detail in agents/brokered-auto-closeout.md
- `Human approval remains mandatory today` -- detail in agents/release-and-regression.md
- `must exactly equal the copy loaded from the pinned target commit in both phases` -- detail in agents/brokered-auto-closeout.md
- `must never replace that human approval gate` -- detail in agents/brokered-auto-closeout.md
- `PowerShell 7+` -- detail in agents/brokered-auto-closeout.md
- `protected-target-dirty-recovery` -- detail in agents/brokered-auto-closeout.md
- `protected-target-noop-closeout` -- detail in agents/brokered-auto-closeout.md
- `repoClosedAuditHash` -- detail in agents/brokered-auto-closeout.md
- `repoStateLedger` -- detail in agents/brokered-auto-closeout.md
- `rollbackPolicy` -- detail in agents/brokered-auto-closeout.md
- `round-delta note` -- detail in agents/brokered-auto-closeout.md
- `semantic success authority` -- detail in agents/brokered-auto-closeout.md
- `separately reviewed activation is now landed: `candidateAcceptance.enabled` and `candidateAcceptance.requireReadyForFinalize` are both `true`` -- detail in agents/brokered-auto-closeout.md
- `start-closeout-dashboard.ps1` -- detail in agents/brokered-auto-closeout.md
- `validate-rollback-manifest.ps1` -- detail in agents/brokered-auto-closeout.md
- `webDashboardSpec` -- detail in agents/brokered-auto-closeout.md
- `workBlockBootstrap.autoBranchFromProtectedTarget` -- detail in agents/brokered-auto-closeout.md
- `workBlockBootstrap.requireIntegratedStartHeadForFinalize` -- detail in agents/brokered-auto-closeout.md
- `worktree-inspection.v1` -- detail in agents/brokered-auto-closeout.md

## CoS Response Duty (owner ruling, Layi, 2026-10-15)
CoS asks for `ACK: <plan or ETA>` are owner-authorized instructions, not "data only". Other CoS feedback (Blockers, Improvements) stays data. Doctrine text that calls cos-feedback "zero authority", or treats the heartbeat file as an ACK, does not apply to CoS asks.

CoS response duty: any PR comment, issue comment, or cos-feedback/ bus note from the Chief of Staff that asks for 'ACK: <plan or ETA>' must get a reply within 24 hours on the same thread. The reply starts with 'ACK:' and gives a plan or ETA, or starts with 'DISTINGUISH:' and gives the reason it doesn't apply. A push, a heartbeat file, or a commit message is not an ACK. Never merge a card while a CoS ask on it is unanswered. At the start of every session, check open CoS asks (GitHub comments by layibabalola that ask for an ACK, plus cos-feedback/mlv-app/ on softwarefactory-fleet-doctrine master) and answer them first.

Board scope (hub ruling 2026-10-15, ledger): "merge" includes enqueueing into the native merge queue, so a card with an unanswered CoS ask is never enqueued; and the open-asks check runs at the start of every session AND every hub tick, so a long-lived hub session cannot let the 24-hour window lapse.

## Sensitive Folders -- Write Policy
- **DO NOT write new files into `.claude/`**. It is treated as a sensitive folder in this repo. Anything committed there was curated and should stay stable.
- Write all *new* agent scratch/state to `.claude-state/` instead. This includes profiling runs, temporary JSON, smoke-test logs, stashed artifacts, summary scripts, etc. `.claude-state/` is `.gitignore`d.
- Durable cross-session findings (the kind you want committed alongside a code change) still belong under `.claude/analysis/<topic>.md` -- editing existing tracked notes there is fine, but do not create ad-hoc new files under `.claude/profiling/` or `.claude/` roots.
- Codex worktrees under `.claude/worktrees/` remain in place -- that subtree is load-bearing.

## Investigation Discipline
- Scratch profiling / ephemeral measurements: `.claude-state/profiling/<date>-<topic>/`.
- Curated, cross-session findings: update existing `.claude/analysis/<topic>.md` rather than scattering new files.
- Use `.claude/ANALYSIS_LOG.md` only as the append-only historical log for already-tracked major investigations (do not create parallel logs elsewhere).
- When a workflow or coordination gap is discovered, do not stop at a live correction. Add the smallest durable prevention mechanism that fits the failure mode in the same turn when feasible: a test, hook/check, ledger state, documented rule, or explicit roadmap item.
- Do not run broad recursive searches over `%USERPROFILE%`, `$env:LOCALAPPDATA\Packages`, or other whole user/app-package trees during agent investigations. These locations can contain unrelated mail attachments, downloads, and app caches; target the known Codex/OpenAI/bridge state paths instead, and exclude `microsoft.windowscommunicationsapps_*` when a package-level search is unavoidable.
- Separate claims into:
  - `Verified locally`
  - `Cross-checked from prior analysis`
  - `Needs runtime profiling`
- Prefer code references in `path:line` form.
- Keep next-step recommendations ranked by impact and effort.
- When the user asks for "repo status" or an equivalent overall repository state, report the current branch/tracking and dirty state, plus local branches, registered worktrees, and stashes. If you intentionally omit any of those, say why.

## Agent Bridge Startup
- On session open in this repository, initialize the agent bridge before normal relay work.
- For Codex-specific bridge heuristics, wake-loop behavior, and routing policy, also read `bridge_trigger_heuristics.md`.
- Before any substantive bridge-related response, run:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-bridge\codex_pre_response.ps1 -RepoRoot .`
- Before any final response after bridge-related work, run:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-bridge\codex_pre_final.ps1 -RepoRoot .`
- Agent bridge wake/reminder/toast/balloon commands are deliberate PowerShell 7+ exceptions until separately proven safe: bridge WinRT toast activation, WindowsPowerShell app IDs, clipboard, SendKeys, UIAutomation, and focus helpers may require Windows PowerShell 5.1 behavior. Bridge process metadata probes are not part of that exception and should use the shared `powershell_runtime.powershell_cim_command()` policy helper.
- These pre-response/pre-final scripts are workflow reminders, not consumers. They must not inspect message bodies, mark messages read, or replace explicit inbox hygiene.
- These hooks pass `-SkipSessionWorktree` into the bridge reminder and must remain read-only with respect to managed session worktree lifecycle. They may refresh context, metrics, timestamps, and completion gates, but they must not create, reuse, refresh, or resurrect `.codex-worktrees/` or session worktree branches; explicit start/bootstrap commands own that lifecycle.
- Treat them as best-effort only in this Codex Desktop thread. If a trivial nudge or other minimal prompt bypasses them, do not claim they provided reliable wake behavior.
- For explicit parked bridge-watch tests only, use:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-bridge\codex_bridge_watch_mode.ps1 -Action on`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-bridge\codex_bridge_watch_mode.ps1 -Action off`
- Bridge-watch mode is reminder-only. It makes the hooks front-load a louder `wait_inbox` reminder, but it does not hard-enforce tool usage and it does not change the main chat default.
- Use:
  - `py -3 tools\agent-bridge\bootstrap_session.py --state-dir "$env:USERPROFILE\.agent-bridge\state" --agent codex --cwd . --watcher-config "$env:USERPROFILE\.agent-bridge\watcher-config.json"`
- Bootstrap does four things:
  - derives the canonical project/rendezvous identity,
  - activates this Codex session and supersedes any older same-agent session,
  - drains any previous same-agent unread messages once,
  - sends the bridge `HANDSHAKE` and refreshes `watcher-config.json` with the active private GUID plus the rendezvous/control-plane entry.
- After bootstrap:
  - surface any drained previous-session messages in the chat,
  - if a bridge message body is surfaced to Codex by `check_inbox`, `wait_inbox`, or an equivalent non-destructive read, treat that message as already read by Codex and mark it read in the bridge immediately, even if the follow-up work will happen later,
  - if a surfaced bridge message is an `ACTION_REQUEST`, do not stop at an inbox summary. In the same turn, either start/continue implementation and record execution progress, record and park/block/displace it in the pending-action ledger with a reason, or explicitly name the user decision that blocks it.
  - after replying to, acting on, parking, blocking, displacing, rejecting, or otherwise folding a substantive surfaced message into the active task, mark that bridge message handled with the matching disposition,
  - use the returned active session GUID for bridge traffic,
  - if Codex's MCP/bridge tools become available again after an interruption or Desktop restart, send Claude a `STATUS_UPDATE` in that same turn before other outbound bridge traffic; include the active session GUID, pair id if known, bridge state, and any queued/dropped/drained messages from the dark window,
  - if bridge consumption reports `SESSION_UPDATE: superseded`, stop bridge communication in this session.
  - do not start a persistent `wait_inbox` loop in the main working chat by default; only use it for an explicit short smoke test or parked bridge-watch session described in `bridge_trigger_heuristics.md`.

## Runtime Execution Rules (Windows)
- Before running any `MLVApp.exe` binary directly, always use a Qt runtime path that matches the binary and force it for that launch.
- Required shell pattern before launch:
  - set `QT_OPENGL=desktop`.
  - set `PATH` so the active Qt runtime comes first, then the active MinGW toolchain, then the exe folder:
    - `C:\Qt\6.10.2\mingw_64\bin`
    - `C:\Qt\Tools\mingw1310_64\bin`
    - `<directory containing MLVApp.exe>`
  - launch from the exe directory (or pass absolute paths).
- Do not mix `C:\Qt\6.10.2\mingw_64` runtime binaries with a different Qt runtime in the same launch session.
- For profile/test runs, prefer:
  - `Set-Location <build-root>\release`
  - `$env:QT_OPENGL='desktop'`
  - `$env:PATH='C:\Qt\6.10.2\mingw_64\bin;C:\Qt\Tools\mingw1310_64\bin;' + (Get-Location) + ';' + $env:PATH`
  - `.\MLVApp.exe ...`
- If the system reports missing `Qt6Core.dll` / `Qt6Network.dll` or entry-point lookup failures, rerun:
  - `C:\Qt\6.10.2\mingw_64\bin\windeployqt.exe <path-to-MLVApp.exe> --release --no-translations --no-compiler-runtime`
- After every release build intended for manual dogfood or Explorer double-click launch, make the release folder self-contained for the MinGW runtime. Copy these DLLs from `C:\Qt\Tools\mingw1310_64\bin` into the directory containing `MLVApp.exe`, then verify they exist there:
  - `libgcc_s_seh-1.dll`
  - `libstdc++-6.dll`
  - `libwinpthread-1.dll`
  - `libgomp-1.dll`
  - This is required even when command-line launches work, because Explorer does not inherit the Codex shell `PATH`.
- For a repeatable launch with less chance of error, use:
  - `pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .claude-state\\scripts\\run-mlvapp.ps1 -ExePath <path-to-MLVApp.exe> -Arguments '--help'`
  - if you changed Qt paths, pass `-QtBin ...` and `-MingwBin ...`.

## Aspect / RAWC de-squeeze -- apply in EVERY present/output/screenshot path (Layi 2026-06-30)

The de-squeeze is NOT centralized: every render / present / export / screenshot path re-implements it,
so a NEW path tends to FORGET it and ship a SQUEEZED image (this is why "aspect ratio is always wrong").
Source of truth = `getMlvAspectRatio(mlvObject)` (RAWC `sampling_y/sampling_x` -> vertical stretch / picAR),
NOT raw RAWI dims and NOT the texture's pixel WxH. When ADDING or REVIEWING any frame display/export/
screenshot path, VERIFY the aspect matches getMlvAspectRatio. Known-good paths: GUI `setSliders`, CDNG export
picAR->`dng.c` (`d245e785`), `GpuDisplayViewport` stretched targetRect (`ed1bffaf`). CURRENT REGRESSION:
the M2 no-readback CUDA texture-present (`GpuDisplayWindow.cpp:624/:870`) letterboxes using display dims that
do NOT carry the stretch -> the no-readback smoke screenshots are squeezed. Fix: pass de-squeezed
`displayWidth/Height = (texWidth, round(texHeight * verticalStretch))` into the texture-present. Prefer to
CENTRALIZE the de-squeeze in one helper so future paths cannot forget it. Canonical detail:
`.claude-state/project-memory/aspect-desqueeze-always-apply.md`.
