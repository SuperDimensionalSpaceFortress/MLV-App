---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — a guard verdict asserted is not a guard verdict: a lane wrote "the hook would refuse this edit" without attempting it, the hub relayed it as owner-only, and a CI fix sat about 20 hours

**Trap:** a lane patching the test-sharding step of a CI workflow wrote that the edit-time hook "would refuse" a change to a workflow run body. It never attempted the edit. The hub copied the sentence into its ledger as owner-only and repeated it for about 20 hours, while a 240 s kill on one test shard kept dropping pull requests out of the merge queue (two in a day).

**What the guard actually did:** an adjudication that ran the real hook against the base ref's copy got ALLOW for an Edit whose old text was the single pattern line, ALLOW for a whole-file Write, and DENY only for an Edit spanning the step header. The same one-line shape had already landed three times. The fix then merged normally: 442 tests re-split from 75 to 77 shards, every test in exactly one shard, at most 12 per shard, the 240 s bound untouched, hosted CI 26 of 26, every hook allowing the edit.

**Rule:** never write a guard verdict in a prompt, summary or ledger line unless it is the output of executing the guard: attempt the act with the native tool, or dry-run the hook's decision function against the base ref's hook, and quote the result with the hook's hash. A real block goes to a three-agent adversarial adjudication with two outcomes, a true block (recorded) or a false positive (a test-pinned hook-refinement card). Neither is escalated to the owner. Owner-only is reserved for credentials, authentication, MFA, payment and physical presence.

**Mechanism:** a guard's behaviour is a function of the exact bytes of the proposed edit. A prediction from its name or intent is wrong at the margins, and a minimal edit lives at the margin. Relaying the prediction launders it into a fact.

**Falsifier:** grep the hub's ledger for "<guard> refuses <act>" or "owner-only (<guard>)" lines. Each must sit in an entry that quotes an execution result (attempt transcript or dry-run output, plus hook hash); a line with neither means the trap is live. To re-test here, feed the base ref's hook an Edit whose old text is one run-body line and another whose old text includes the step header: the first must allow, the second deny. If both deny, or a later execution overturns a recorded "refuses" line, re-measure.
