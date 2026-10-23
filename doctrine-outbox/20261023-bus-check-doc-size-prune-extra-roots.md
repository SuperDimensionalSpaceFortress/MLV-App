---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/check-doc-size-prune-before-descent-and-extra-roots
rule: A per-turn doc-size gate must not walk what it will never govern. In a worktree scan, prune a directory before descending when an exempt glob of the form <dir>/** or <dir>/* covers its whole subtree, and let a config name extra_roots so only those directories plus the top-level files are walked.
mechanism: bus tools/factory/check-doc-size.py (c82bddb) enters every directory and filters by exempt afterwards: on a large board 837,710 paths per run, 155,544 pruned, 47,215 with roots. Pruning is verdict-neutral (fnmatch star crosses slash); other glob shapes still walk. extra_roots is a non-empty list, scan worktree only (else exit 2); a bad root is a typed note, never a crash.
applies: mlv-app, conjugal, cloudvore, magic-lantern
check: git apply the sibling patch to bus c82bddb, run tools/factory/check-doc-size.tests.py: old tool red 5 of 28, patched green 28 of 28; the 3 fixture configs print byte-identical output (sha256 pinned in the tests).
supersedes: none
evidence: measured
- **Adopt:** bus steward applies sibling doctrine-outbox/20261023-bus-check-doc-size-predescent-prune-extra-roots.patch (LF sha256 7dc38f1d8e0a75debdd552b0e807ed6299b441f7f58aa2447b5aa5cfc6d73196) to bus c82bddbac001bfeb488493dc8910402753904ed8: check-doc-size.py 3635d4ac to c1295da3, its tests 3733d86b to 55391f7c.
