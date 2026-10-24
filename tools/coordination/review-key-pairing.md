# Review-key pairing (MODEL-POSTURE-R16-1)

Hub ruling 2026-10-12 (ledger line `HUB RULING (hub chat, model posture)`), after a three-agent
adversarial swarm. A formal review round has two keys:

| Key | Lane | Why |
|---|---|---|
| A | `sol` | Found 51 blockers `fable` missed against 1 the other way, over 191 paired reviews. Also co-planner with `opus`. |
| B | `opus`, **unless the PR's implementing lane was `opus`, then `fable`** | No self-review. `fable` stays the planning/guidance lane and the fallback key B. |

`astra` is the tie-breaker on a contested design, never a key. `sonnet` is the default implementer
(83 % DONE at USD 1.37 a round against `opus` at 48 % and USD 7.97), so most cards get `opus` as key B.

## The tracked helper

`tools/coordination/Select-ReviewKeyB.ps1 -Card <CARD> [-Json]` reads the producer from the card's
**newest producer receipt**: a `*.receipt.json` under `.claude-state/fleet-runs/<run>/` whose `card`
equals the card and whose `allowEdits` is `true` (a review key never edits, so a key receipt can
never name the producer), newest by `startedUtc`. It prints the key-B lane name, or with `-Json`:
`card, keyA, keyB, producerLane, producerKnown, producerReceipt, reason, warnings`.

- No readable matching producer receipt (a hand-written PR): `keyB = fable`, `producerKnown = false`,
  and one warning. An unknown producer fails toward `fable`, because the producer may have been `opus`
  and key B must stay independent of it (hub ruling, HUB-TICK 20261013T0321Z, r3 blocker B2).
- Producer `sol`: `keyB = opus` plus a warning that key A would review its own work.
- Exit 2 on a usage error (unknown `-ProducerLane`, missing runs directory).
- **Call it as a child process**, as below. Never dot-source it: its `param` block (`-Card`,
  `-RunsDir`, ...) would overwrite the caller's own `$Card`.

## Exact patch for `hub-tools\new-review-keys.ps1` (board state, not tracked, not edited by this card)

First put the helper beside the other hub tools (it must exist on the board, because the main
checkout sits on a peer branch and may not have the file):

```powershell
git -C 'C:\!Layi Wkspc\MLV-App' show fork/master:tools/coordination/Select-ReviewKeyB.ps1 |
    Set-Content 'C:\!Layi Wkspc\MLV-App\.claude-state\fleet-runs\hub-tools\Select-ReviewKeyB.ps1' -Encoding ascii
```

Then, against the file as it stands today (line numbers are today's):

```diff
--- a/hub-tools/new-review-keys.ps1
+++ b/hub-tools/new-review-keys.ps1
@@ -1,1 +1,1 @@
-# new-review-keys.ps1 - build a formal review-key round (fable + sol) for one PR.
+# new-review-keys.ps1 - build a formal review-key round (key B + sol) for one PR. Key B = opus, or fable when the card's producer lane was opus (Select-ReviewKeyB.ps1).
@@ -41,2 +41,9 @@
 if ($gc.Required) { $judge = $judge.TrimEnd() + "`n" + $gc.Line }
+# MODEL-POSTURE-R16-1: key A = sol; key B = opus unless the card's producer lane was opus (then fable). Child process, never dot-sourced ($Card would be overwritten).
+$keySel = (& pwsh -NoProfile -File (Join-Path $PSScriptRoot 'Select-ReviewKeyB.ps1') -Card $Card -RunsDir $fr -Json) | ConvertFrom-Json
+if ($LASTEXITCODE -ne 0 -or -not $keySel -or $keySel.keyB -notin 'opus', 'fable') { throw "Select-ReviewKeyB failed for card $Card" }
+foreach ($w in @($keySel.warnings)) { Write-Warning $w }
+$keyB = [string]$keySel.keyB
 $ts =[DateTime]::UtcNow.ToString('yyyyMMddTHHmm') + 'Z'
@@ -49,1 +56,1 @@
-foreach ($lane in 'fable', 'sol') {
+foreach ($lane in $keyB, 'sol') {
@@ -57,1 +64,1 @@
-    if ($lane -eq 'fable') {
+    if ($lane -in 'fable', 'opus') {   # both are claude lanes: stdout is read only at exit, so they need the phase-1 provisional verdict
```

The `"reviewer":"$lane"` field in the key prompt, the `key-prompt-$lane.md` / `watch-ci-then-$lane.ps1`
file names and the `review-pr$Pr-$lane-r$Round-$ts` run directory all follow `$lane`, so they
become `opus` automatically. `dispatch-lane.ps1` already accepts `-Lane opus`
(`-MaxTurns` is passed for `sonnet`, `opus` and `fable`).

## Same-day follow-up the hub must wire together with the patch

The key-B lane name is hard-coded as `fable` in other board tools. A round whose key B is `opus`
writes `review-pr<N>-opus-r<R>-*` directories those tools do not recognise, so **apply these in
the same change as the patch above**, or `opus` keys are never read, retried or counted
(line numbers as of 2026-10-12, paths under `.claude-state\`):

| File | Line | Today | Needs |
|---|---|---|---|
| `hub-autopilot\hub-autopilot.ps1` | 254 | `foreach ($ln in 'fable', 'sol')` reads the verdicts | add `'opus'` |
| `hub-autopilot\mechanical-keys.ps1` | 261 | `^review-pr(\d+)-(keys\|sol\|fable)-r` | `(keys\|sol\|fable\|opus)` |
| `hub-autopilot\mechanical-keys.ps1` | 305 | `(?<lane>sol\|fable)` D5 failed-launch state | `(?<lane>sol\|fable\|opus)` |
| `hub-autopilot\mechanical-keys.ps1` | 491 | `ValidateSet('sol', 'fable')` | add `'opus'` |
| `hub-autopilot\mechanical-keys.ps1` | 507, 673 | `(?:sol\|fable)` in two regexes | add `\|opus` |
| `hub-autopilot\mechanical-keys.ps1` | 697, 701 | `foreach ($ln in 'sol', 'fable')` | iterate `sol` + the round's key B (the dir that exists) |
| `hub-autopilot\rerun-then-keys.ps1`, `pr270-rerun-then-keys.ps1` | 25, 12 | `foreach ($l in 'sol', 'fable')` | same |
| `fleet-runs\hub-tools\new-key-round.ps1` | 26, 45 | `foreach ($lane in 'sol', 'fable')`, `-eq 'fable'` | same as new-review-keys.ps1 |
| `hub-autopilot\Test-MechKeysD5FailedLaunch.ps1` | many | fixtures use `fable` | add an `opus` key-B fixture row |

`read-key-verdict.ps1` already classes `fable`, `opus` and `sonnet` alike as claude lanes.

## Defect dispositions block (FACTORY-ESCAPE-REGRESSION-DISCIPLINE-1, evidence-only)

Owner ask 2026-10-24 (dot notice on #660): every demonstrated defect (a key BLOCKER, a confirmed Dot/Grok
finding, a merge_group failure that is not a flake) leaves a row in the landing PR body: the regression test
that is RED on the old sha and GREEN on the new, the adversarial combination tests (or `none` with a reason),
and a disposition (`fixed`, `distinguished: <reason>`, `deferred: <CARD>`). The block sits between
`<!-- defect-dispositions:begin -->` and `<!-- defect-dispositions:end -->` and is built by
`tools/coordination/defect_ledger.py`; the record schema `defect-ledger.v1` is in its docstring.

**It is record-only, not a gate.** No hook deny, CI check or enqueue refusal reads it. A fix round writes a
JSON list of records, then `render --pr N --head <40-hex> --records <file>` and
`splice --body-file <body> --block-file <block> --out <new>` (idempotent; malformed markers are refused,
exit 2, nothing written), then `gh pr edit --body-file`. An absent field renders `MISSING`; a non-`fixed` row
states `n/a` explicitly. `check --body-file <f>` prints one line (`DEFECT_LEDGER ok n=<k>`, `WARN missing`,
`WARN unbound=<n> rows=<ids>`, `WARN incomplete rows=<ids>`, `WARN unreadable <why>`) and always exits 0.
"Incomplete" means a MISSING cell, a `fixed` row without distinct RED-old / GREEN-new shas or without a
combination test or reason, or an unknown disposition.

**Binding rule (Dot B1 on #667).** Every record carries `schema`, `pr` and `head_sha` (full 40-hex), and
`render` compares them with `--pr`, `--head` and `defect-ledger.v1`. A record that differs or lacks one is never
printed as if it belonged to this PR and head: its Disposition cell starts `UNBOUND: pr 612 != 667` (or
`head 1a2b3c4d != 95e9ff8f`, `head_sha MISSING`, `schema <x>`) and keeps the record's own disposition after it,
the row stays visible, and a `Binding check:` line counts bound against UNBOUND rows. A block whose records all
match reads exactly as before. Still no gate: `check` warns and exits 0.

The hub's daily roadmap line comes from `escapes --merged-log <jsonl> --records-dir <dir> --since-days 7
[--known-flakes <file>] [--reverts-from-git <repo>] [--now <utc>]`: `ESCAPES window=7d merged_defects=<a>
reverts=<c> nonflake_merge_group_failures=<b> repeat_failures=<r> undetermined=<u>`, then one `ESCAPE ...` line
per counted item. A repeat is one test id or normalised claim recorded on two or more distinct PRs inside the
window; `distinguished` rows and known flakes (events, and records whose regression test is one) never count.
A merged defect needs the record's `fixed_utc` after the PR's merge event (a defect fixed before landing is not
an escape); without a usable `fixed_utc` it is `undetermined`, not an escape. `--now` also drops merge events
dated after it, so a replay never counts a future merge. It also always exits 0 and reads only the files it is
handed.

## Checking the posture

`python tools/coordination/check-lane-model-currency.py` reads the lane receipts and reports a lane
whose two latest runs both used an id older than its family's newest, or any effort but `high`
(exit 1). Missing evidence is never `CURRENT`: a lane whose newest run proved no model is `NO-DATA`,
one with a proven model but a run that recorded no effort is `INCOMPLETE` (`unrecordedEffort` in the
table), and either makes the overall verdict `INCOMPLETE` (exit 3; no lane in the window is `NO-DATA`,
also exit 3). `Invoke-Lane.ps1` receipts carry `codexModelsFetchedAt` (age of the model list) and, for a
pinned read-only codex lane, `codexExe.pinState` (`PIN_MODEL_UNPROBED` = the pin never proved the
resolved model, so the global CLI ran it).

## Provider/model topology block (PR-PROVIDER-MODEL-TOPOLOGY-1, record-only)

Owner ask 2026-10-24 (dot notice on #660): a PR body shows the provider and exact model for each role.
`python tools/coordination/pr_topology.py render --pr N --head <40-hex> --card <CARD> --runs-root
<fleet-runs>` builds the `<!-- pr-topology:begin -->` ... `<!-- pr-topology:end -->` block from the run
dirs (`lane-<CARD>-r<k>-*`, `design-<CARD>-*`, `review-pr<N>-<seat>[-key3]-r<k>-*`): provider by receipt
`engine`, `resolvedModel` (else `claimed: <requestedModel>`), `effort`, and the SHA each row is bound to
(a key's `subject_sha`, a producer's `DONE <sha>`; else `unbound`). A row on another head reads
`stale (other head)`. A footer is only ever `claimed by footer, unverified`.

**It is not a gate.** `check --body-file <f> --head <sha>` prints one line (`PR_TOPOLOGY ok`, `WARN
missing`, `WARN stale head=.. section=..`, `WARN unreadable ..`) and always exits 0. The hub refreshes the
block with `render`, then `splice --body-file <body> --block-file <block> --out <new>` (idempotent; a body with
malformed markers is refused, exit 2, nothing written), then `gh pr edit --body-file`. Wiring `check` into
merge-enqueue's log and `render`+`splice` into the key watcher is hub tooling under `.claude-state`.
