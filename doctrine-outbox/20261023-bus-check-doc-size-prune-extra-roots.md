---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/check-doc-size-prune-before-descent-and-extra-roots
rule: A per-turn doc-size gate must not walk what it will never govern. In a worktree scan, prune a directory before descending when an exempt glob of the form <dir>/** or <dir>/* covers its whole subtree (an extra root at or below such a dir included), and let a config name extra_roots so only those directories plus the top-level files are walked.
mechanism: bus tools/factory/check-doc-size.py (c82bddb) enters every directory and filters by exempt afterwards: in mlv-app's own doc-size walk 837,710 paths per run, 155,544 pruned, 47,215 with roots. Pruning is verdict-neutral (fnmatch star crosses slash); other glob shapes still walk. extra_roots is a non-empty list, scan worktree only (else exit 2); a bad root is a typed note, never a crash.
applies: mlv-app, conjugal, cloudvore
check: git apply the pinned patch (see Adopt) to bus c82bddb, run tools/factory/check-doc-size.tests.py: old tool red 8 of 31, patched green 31 of 31; the 3 fixture configs print byte-identical output (sha256 pinned in the tests).
supersedes: none
evidence: measured
- **Adopt:** bus steward applies the 20744-byte patch (LF sha256 bcf0b273d00d8bb8143f6555b891ec185c71abaa6f3f6ba430cd2d2626d2e1b5), blob doctrine-outbox/20261023-bus-check-doc-size-predescent-prune-extra-roots-r3.patch at mlv-app commit 223a06460de78c819fb93d27738a9d51502977bc (not in the outbox: an item is a .md card of at most 2000 bytes), to bus c82bddbac001bfeb488493dc8910402753904ed8: check-doc-size.py git blob 62485339 to 10ecdde7, its tests git blob 71f36aed to e4d8b571.
