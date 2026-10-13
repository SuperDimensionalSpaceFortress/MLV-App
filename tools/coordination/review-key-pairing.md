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

## Checking the posture

`python tools/coordination/check-lane-model-currency.py` reads the lane receipts and reports a lane
whose two latest runs both used an id older than its family's newest, or any effort but `high`
(exit 1). Missing evidence is never `CURRENT`: a lane whose newest run proved no model is `NO-DATA`,
one with a proven model but a run that recorded no effort is `INCOMPLETE` (`unrecordedEffort` in the
table), and either makes the overall verdict `INCOMPLETE` (exit 3; no lane in the window is `NO-DATA`,
also exit 3). `Invoke-Lane.ps1` receipts carry `codexModelsFetchedAt` (age of the model list) and, for a
pinned read-only codex lane, `codexExe.pinState` (`PIN_MODEL_UNPROBED` = the pin never proved the
resolved model, so the global CLI ran it).
