---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-push-gate-queuing-a-4-second-selection-gets-routed-around
rule: a shared-slot admission gate prices work by a MEASURED cost. Run small, fully-weighed work directly with one typed decision line; send everything unmeasured to the queue (fail toward the queue, never toward the bypass); bound the direct path by a timeout and an overrun tripwire that hands the rest of the batch to the queue. Never infer "small" from a guess.
mechanism: the pre-push fast-suites gate took the machine-wide heavy slot for every selection, with a flat 15-minute estimate per selector. A two-class selection that ran in 4.11 s sat fourth in the queue behind other projects, so producers on three cards stopped the gate and ran the selection by hand. The fix reads weights at the merge base and goes direct at 120 s or less, with a 4x overrun tripwire (10 s floor). First live run: three measured classes plus one unmeasured whole-file selector took 85 s and was correctly queued. Limits: the 120 s threshold is a hub judgement, weights are hosted-CI seconds, and the new gate's direct branch is proven in scratch repos only, never run live.
applies: mlv-app
check: run the guard (36 of 36 rows green; 2 of 19 on the original gate) and read the typed decision line of a real run.
supersedes: none
evidence: measured
- **Guard:** Test-FastSuitesSmallSelection.ps1 in the hub tools folder (untracked, local to mlv-app; the full path is refused by the Law 4 screen)
