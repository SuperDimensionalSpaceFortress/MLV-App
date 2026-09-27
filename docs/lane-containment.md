# Bounded model processes

`tools/coordination/Invoke-Lane.ps1` starts each Claude CLI beneath an inert
PowerShell host. The launcher assigns that host to a Windows job with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` before sending provider configuration.
Claude and its descendants inherit the job. Losing the launcher closes its
job handle and terminates those descendants. Assignment failure starts no
provider and terminates the inert host.

The launcher writes a `running`, incomplete receipt with process identities
before delivering the prompt. Normal completion replaces it atomically. If
the launcher is forcibly terminated, the receipt remains incomplete; it cannot
prove delivery or zero spend. Receipt I/O failures do not retain the job.
Setup, startup and provider execution share one monotonic timeout budget. The
UTC deadline projects that budget from the same initial start time; the stopwatch
enforces it. An exhausted setup budget prevents the host or provider from starting.

Every Claude lane receives
`--disallowedTools Agent,Task,Monitor,ScheduleWakeup,CronCreate,CronDelete,RemoteTrigger,Workflow,TaskCreate`
(LANE-NO-BACKGROUND-END-TURN-1): a headless lane has no later turn, so a
tool that promises one (background jobs, a scheduled wakeup, a cron entry, a remote
trigger) leaves the work stranded exactly like the built-in nested-agent tools this
deny list already blocked. The same list (`$DENIED_TOOLS` /
`$DENIED_TOOLS_DISPLAY` in `tools/coordination/Invoke-Lane.ps1`) is checked at
pre-reservation, so an editing allowlist containing ANY of the nine denied tools is
rejected before reservation, not only `Agent`/`Task`. Claude-CLI-only
(codex has no equivalent, and keeps its previous behaviour). This blocks
the CLI's own tool surface only; `Bash`'s `run_in_background` parameter
cannot be reached this way -- it is a parameter of the `Bash` tool, not a
separate tool name, and editing lanes need `Bash` itself. Round 2 (hub
ruling) removed a `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` child-environment
flag that stood here through round 1: `docs/never-authorized.json` NA-3
prohibits assigning ANY `CLAUDE_CODE_*` variable, and widening that rule is
an authority change the hub will not make. This PR adds no
`ANTHROPIC_`/`OPENAI_`/`CLAUDE_CODE_` assignment; the pre-existing
`CLAUDE_CODE_EFFORT_LEVEL` assignment (8ad69168, 2026-09-07, previously
reviewed) stays out of scope, tracked under NA3-CHILD-ENV-SCOPE-1.

**Blocking `run_in_background` (round 6, swarm ruling; matcher, fail-closed
protocol, launch self-test and hook-copy tamper detection added round 7;
registered in EXEC FORM since round 13; interpreter tamper detection added
round 14).** Every Claude-engine lane -- read-only and editing alike --
receives a per-run `--settings` JSON beside its receipt
(`<lane>-NNN.settings.json`) registering a `PreToolUse` hook on the
`Bash|PowerShell` matcher (round 7: widened from `Bash` alone -- `PowerShell`
carries the same parameter and is granted to every editing lane by
`docs/Start-EditingLane.ps1`). The hook script,
`tools/coordination/lane-no-background.py`, is copied into this run's
already-reserved, board-rooted slot (`<lane>-NNN.lane-no-background.py`,
beside the receipt under `.claude-state/fleet-runs` -- an NA-2 protected
tail no lane can write to) before argv is built, and registered by that
copy's path -- never a path inside the lane's own writable worktree (round
7, sol major 3; `docs/Start-EditingLane.ps1` runs `Invoke-Lane.ps1` FROM
`$WorkDir`, inside that worktree, for every editing lane). The copy is
hashed (`sha256`) immediately.

**Registered in EXEC FORM (round 13), which removes the shell entirely
rather than proving it.** Rounds 8-12 self-tested shell CANDIDATES, because
a shell-form `command` (a bare string, no `args`) hands the executable
choice to Claude Code's own, undocumented shell auto-detection, and each
closed hole (a WSL stub on PATH, PATH precedence, an inherited
`CLAUDE_CODE_GIT_BASH_PATH`) left the shape of one more behind it. Per the
vendor docs (code.claude.com/docs/en/hooks, "Exec form and shell form" /
"Command hook fields", fetched 2026-09-22), exec form (`args` present) has
Claude Code resolve `command` as an executable and spawn it directly with
`args` as the argument vector, no shell involved, `shell` ignored once
`args` is set. The hook is registered as `{"type": "command", "command":
"<resolved python.exe>", "args": ["<hook copy path>"]}` -- no command
STRING, no `shell` field -- so there is no auto-detection left to out-guess
and no candidate list to enumerate. Rounds 8-12's shell-selection machinery
(all-candidates resolver, Git Bash/PowerShell resolvers, per-shell-kind
self-test loop, `$hookCommand` string builders, the `MLV_GIT_BASH`/
`MLV_LANE_POWERSHELL_EXE` overrides and their fixture tests) is DELETED,
not disabled.

**The INTERPRETER (`command` in exec form) is still resolved at runtime.**
`$PYTHON_EXE` comes from `Resolve-LaneExecutable`: an authoritative override
(`MLV_LANE_PYTHON_EXE`, test/operator use only), then curated known install
locations, then `PATH` as a last resort -- the order round 9 established
after a dev-machine-only pin broke hosted CI. No interpreter resolving
refuses the launch before a hook is registered
(`background-gate-interpreter-not-found`) -- an unrunnable command denies
nothing.

**The self-test is exact, not a candidate proof.** With no shell in the
path, it invokes `& $PYTHON_EXE $hookCopyPath` through PowerShell's call
operator with an ARGUMENT ARRAY -- the identical spawn exec form itself
performs -- fed the synthetic deny payload on stdin. Round 11's finding
survives: exit 2 alone is not proof the hook denied THIS payload (Python
itself exits 2 for a missing script path), so the self-test also requires
the hook's own deny-reason text (`"headless lane"`) in the captured output,
plus a positive control (same call, non-background payload, required
ALLOWED) before trusting it. Failure refuses the launch
(`background-gate-selftest-failed`, naming the captured exit code and
output). The receipt's `authority` block records
`backgroundGateInterpreterPath`/`-Source` and `backgroundGateForm: 'exec'` --
replacing round 9-12's now-meaningless `backgroundGateShellKind`/`-Path`/
`-Source`/`-ValidatedCandidates`.

**Tamper detection covers BOTH the hook copy and the interpreter (round 14
added the interpreter half) -- the same check, run twice.** Each is hashed
once, when first proven to deny (the hook copy right after it is written,
before the self-test; the interpreter right before the self-test, off the
exact bytes that self-test runs), then re-hashed after the provider exits.
A mismatch, or either file going missing, overwrites
`authority.backgroundGate` to `'background-gate-tampered'` with
`backgroundGateTamperDetail` naming which fired (both append rather than
overwrite, like `workEvidence.reason` elsewhere). The interpreter's launch
hash is `backgroundGateInterpreterSha256`, parallel to the hook copy's
`backgroundHookSha256`. Round 14 reuses this SAME tampered state for the
interpreter rather than a softer third "unverified" state -- a mismatch and
a missing file both mean this run's launch-time proof no longer describes
what ran. **Exactly two points in time, launch and exit, never
continuous:** a mid-run rewrite RESTORED before the provider exits leaves
both hashes matching, recording no tamper despite the gate being provably
absent meanwhile. Neither check covers a rewrite of the WORKTREE'S SOURCE
copy of `lane-no-background.py`, of `Invoke-Lane.ps1` itself, or of the
interpreter's install location before it is ever resolved and hashed.
**Known gap, not yet closed:** NA-10 would be the natural place to guard
the two script sources as gate tails a lane may write only at the board
venue, but `mlv-never-authorized.py` is itself one of NA-10's guarded
tails, so a lane-worktree session cannot make that edit; filed as card
NA10-LANE-NO-BACKGROUND-TAIL-1, not done here.

**The deny-reason substring is a hand-synchronized literal.** `"headless
lane"` is load-bearing in `lane-no-background.py`'s `DENY_REASON` and
`Invoke-Lane.ps1`'s `$backgroundGateExpectedDenySubstring` -- the ONE other
site it must stay in sync with (round 12 also named this test file's own
`_bash_candidate_runs_the_hook` helper as a third site, deleted round 13
with the rest of the shell-candidate machinery it classified). `DENY_REASON`
carries a comment naming that one remaining site, corrected round 14 after
it drifted to still cite the deleted helper and a since-renamed test --
the same drift the comment exists to prevent.
`test_deny_reason_substring_matches_launcher_selftest` reads both literals,
runs the real hook against actual runtime stderr, and asserts the comment
names only the current, correct site.

The hook script reads the tool-call JSON on stdin and denies the call (exit 2,
one line on stderr -- the same fail-closed protocol
`tools/hooks/mlv-never-authorized.py` uses) whenever `tool_input.run_in_background`
is truthy, for ANY `tool_name` -- a tool-name-agnostic second gate beneath the
matcher, so a future tool carrying the same parameter is refused without a
second wiring change. Every other input allows silently (exit 0, no output).
**Fail-closed on the input itself:** missing/empty stdin, non-JSON stdin, a
non-object top-level JSON value, or a non-object `tool_input`, all DENY
(exit 2) -- a hook that cannot read its own input has no basis to conclude
the call is safe. Pure: no side effects, no network, no filesystem access
beyond stdin/stdout/stderr. The Read deny rules for the manifest-surface
paths stay conditional on `-AllowBulkReads`; the hook registration is
unconditional. Claude-CLI-only; Codex lanes get neither the settings file
nor the hook. The receipt's authority block records `backgroundGate =
'denied-by-settings-hook'`, `backgroundHookSha256` and
`backgroundGateInterpreterSha256` (each file's hash at launch,
proven-denying by the self-test) for every Claude lane.

Containment rests on three things, with a fourth filed and not yet landed:
the `--disallowedTools` deny list above; this settings-file hook, proven by
a launch-time self-test and checked for tamper after the run for both the
hook copy and the interpreter that runs it (the mechanism that actually
denies `run_in_background`); and the dirty-no-commit receipt below as an
after-the-fact backstop for anything reaching a background shell some other
route -- auto-backgrounding by the harness, if any, would not necessarily
carry the `run_in_background` flag this hook keys on. **Residual routes,
stated honestly:** a shell built-in that backgrounds work WITHOUT setting
`run_in_background` -- `Start-Job`, a trailing `&`, or `nohup ... &` inside
an otherwise-foreground call -- is invisible to this hook, which inspects
only the tool-call parameter, never the command text; such a job ends when
the command that started it returns control to the CLI turn, so it does
not survive past the lane's last turn the way `run_in_background: true`
does, but it is not covered by denial here. Arbitrary shell descendants
remain confined to the owning Windows job unless an independently
authorized external service starts them (see the job-object section
above); the job closes them, it does not prevent them from being started.

A receipt is also never marked `complete` (or state `ended-incomplete` is forced)
when an EDITING Claude lane's own worktree is still at the lane's starting sha and
carries tracked-file dirt the lane itself introduced; see
`workEvidence.reason: dirty-worktree-no-commit` in
`tools/coordination/Invoke-Lane.ps1`. **What this detects, precisely:** the check
snapshots a per-path content identity for every tracked (non-`??`) dirty path
before the child starts and compares it against the same snapshot after exit; a
path counts as lane-introduced if it is newly dirty or its content identity
changed, so pre-existing dirt never flips a receipt on its own, but a further
edit to an already-dirty tracked file does (a raw porcelain status line stays
textually identical across such a further edit; content identity per path
does not). That identity is `git hash-object -- <path>` of the working-tree
file itself (a `DELETED` marker when absent), not a hash of rendered `git
diff HEAD -- <path>` text -- diff rendering for a binary file collapses to
the fixed text `Binary files a/<path> and b/<path> differ` regardless of
which bytes are on disk, so hashing that text could not tell two different
binary edits apart; `hash-object` reports the identity of the bytes git
would actually commit. **What this does NOT detect:** background work that
changes no tracked file -- a build, a test run, a Bash job reading files or
writing only untracked scratch output -- leaves no trace here, regardless of
whether that work is still running when the lane's last turn ends. It
applies only to `-AllowEdits` Claude lanes -- a read-only lane can never
move HEAD by construction, so the check there would degenerate to "was the
surrounding checkout dirty," outside a review lane's control; Codex lanes
are excluded the same way. When the check fires it appends to any existing
`workEvidence.reason` rather than overwriting it, so a prior failure
classification (e.g. `subtype-error_max_turns`) is never masked.

**Fail-closed git capture (round 5).** Every `git` call this check makes
(`status --porcelain=v1 -z`, `hash-object` per dirty path) goes through
`Invoke-GitCaptureUtf8`, returning `{ok, stdout, exitCode, error}` instead of
a bare string -- a caller must check `.ok` before touching `.stdout`, so a
failed process start, output read, or non-zero git exit can never be
silently read back as `''`, which previously collided with git's own
genuine empty-and-successful output and made a git failure indistinguishable
from "nothing is dirty." If either the PRE-launch or POST-exit snapshot
cannot be taken, the receipt records `dirtyCheck: 'unavailable'` with
`dirtyCheckReason` naming which snapshot failed and why, and `workEvidence`
is left untouched -- never a false `ended-incomplete`, never a fallback
claim of `dirtyCheck: 'clean'`. `dirtyCheck` also records `'not-applicable'`
(gate did not apply, or HEAD moved) and `'clean'`/`'dirty'` for a check that
ran to completion. The pre-launch and post-exit `git rev-parse HEAD` calls
the gate's base/comparison shas come from go through the same
`Invoke-GitCaptureUtf8` and record `dirtyCheck: 'unavailable'` on failure
too, with `dirtyCheckReason` naming exactly which call failed and why,
rather than collapsing into the misleading `'not-applicable'`/`'HEAD moved'`
readings a falsy `$BaseSha` previously produced either way.

`-ReasoningEffort` explicitly overrides effort for one invocation. LANE-MODEL-CURRENCY-1
round 2 (owner ruling 2026-09-26: every lane runs at high effort): a REAL launch --
`$CLAUDE_EXE`/`$CODEX_EXE` unmodified from `Invoke-Lane.ps1`'s own definitions -- now
refuses any value other than `high` with a typed `lane-effort-must-be-high` failure (a
normal `failed` receipt, non-zero exit, before any provider process starts). The parameter
still parses `low`/`medium` syntactically; the disposable fixture below is the one place
that still passes them, because it replaces `$CLAUDE_EXE`/`$CODEX_EXE` with a fake shim and
so can never reach a real CLI regardless of the value -- it exists to prove the override
still reaches the child's effort configuration mechanically, not to license a real low-effort
launch. When `high` is what runs, Codex receives its normal reasoning configuration argument
and Claude receives `CLAUDE_CODE_EFFORT_LEVEL=high` in the child environment -- the same
pre-existing, out-of-scope-for-this-PR assignment tracked under
NA3-CHILD-ENV-SCOPE-1 above. The receipt records the effective effort; user settings
and the parent environment are unchanged. Codex continues through its existing direct
process path; the new Windows job containment applies to Claude.

Run the real Windows fixture suite with:

```powershell
py -3 -m pytest -q tests/coordination/test_lane_containment.py
```

Disposable fake providers exercise argv, stdin, JSON, effort, normal
completion, startup/execution deadlines, parent loss, assignment failure, and
receipt write failure. Cleanup targets fixture-owned process identities only.
These tests run in the existing Windows coordination check and invoke no
model or network service. Provider lifecycle completion is not card
acceptance: tests, commit, independent review, merge and target verification
remain separate gates.

Read-only Claude lanes receive a model-visible capability notice through the
supported `--append-system-prompt` argument, stating that only Read, Grep and
Glob are permitted, directing reviews to hub-exported evidence, and
prohibiting retrying unavailable shells. The receipt records the exact
notice. User prompt bytes remain unchanged; the notice informs the model,
while the existing permission allowlist and Agent/Task deny still enforce
tool permissions. The hub must export any required Git diff or hosted
evidence before starting such a review.

The production dispatcher must use the reviewed launcher revision and the
applicable updated execution-control receipt before resuming editing
dispatch. This change does not enable the workstream loop or rewrite
historical receipts.
