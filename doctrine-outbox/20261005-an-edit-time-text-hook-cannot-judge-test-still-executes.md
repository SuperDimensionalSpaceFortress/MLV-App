---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — an edit-time text hook cannot judge "the test still executes": a guard PR that parsed workflow edits loosened the guard and over-blocked reads, so it was closed and rebuilt as a deny-only tripwire

**Trap:** a guard PR tried to make the workflow-edit hook judge whether a changed run body still executes the same tests, using token subsets and command heads. An executed differential (27 payloads through the real hook CLI, base ref against PR) showed two failures at once. It loosened the guard: wrapping a command in an uncalled function, and header-spanning edits that narrowed a filter with a split or a first-N select, were DENY on the base ref and ALLOW on the PR. It over-blocked: a read-only print of the workflow with stderr discarded was ALLOW on the base ref and DENY on the PR, because redirect parsing read a read as a write. An adjudication (all three agreed the hook cannot judge execution; two said close) closed the PR unmerged.

**Ruling candidate (not law until ratified):** the hook is a monotone, deny-only tripwire. (1) The base ref's predicate stays byte-identical and runs first. (2) Additions are refusals only: a whole-file step-multiset comparison for Edit and Write (run and uses lines, shorthand included, fail-closed reconstruction) and a skip-marker count increase. (3) A differential test asserts that nothing the base ref denies is allowed (the accepted version: 326 real-CLI inputs, zero loosenings). (4) Execution coverage ("every test that ran before still runs, with the same filter breadth") belongs in CI, checked by a verifier taken from the base ref; every review key for a workflow-touching PR must report that finding.

**Second lesson, same card:** a shell arm that judged shell intent by text (which destination is a write, which redirect sits in a comment, which data argument names a workflow) produced a new false positive in each review round, three rounds and three classes. It was dropped, not patched; shell writes to workflows moved to a CI-side check. When each review finds a new class, the approach is the defect.

**Mechanism:** a text hook sees one edit's bytes, not the program that runs. a fragment judgment errs permissive or blocking.

**Falsifier:** run the base ref's hook and a candidate's over payloads including an uncalled-function wrap of a test command, a header-spanning filter narrowing, and a read-only cat of the workflow with stderr discarded. The candidate must deny everything the base ref denies and must not deny the read. Any ALLOW where the base ref said DENY falsifies the monotone design; a hook claiming execution is preserved while CI has no base-ref verifier falsifies the ruling.
