---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/per-turn-hook-tree-walks-grow-the-kernel-pool
rule: A hook or heartbeat that runs every turn or every few minutes must never walk the whole board tree. Prune wholly exempt directories BEFORE descending, bound receipt scans by a recency window (mtime comes free with the directory listing), and gate commits on RAM plus pagefile maximum, not RAM alone.
mechanism: Host up 6.8 days: paged pool 13.2 GB, nonpaged 5.0 GB (normal 1-2 GB) with 21.7 GB RAM free, while a `RAM - 4` commit gate refused lanes. A 3-minute xperf FILE_IO trace ranked the board heartbeat (4.47M file events), the Stop-hook doc-size check (6.1M) and the checkpoint hook (5.2M) first; each walked the whole board incl. its git-ignored state tree. Counted in-process 2026-10-22: doc-size touched 837,710 paths per run (614k in the run-output tree, which holds full repo copies), checkpoint 113,786 (about 4,700 receipt opens per turn). After pruning and a 72 h window: 155,487 and 25,882.
check: py -3 -m unittest tools.repo_hygiene.test_check_doc_size_walk tools.repo_hygiene.test_session_checkpoint_receipt_window (MLV-App). Elsewhere: read `\Memory\Pool Paged Bytes`; over about 4 GB, run the elevated FILE_IO trace and count `File*` events per process.
supersedes: none
evidence: measured
