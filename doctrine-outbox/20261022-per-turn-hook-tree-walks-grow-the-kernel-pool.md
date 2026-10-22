---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/per-turn-hook-tree-walks-grow-the-kernel-pool
rule: A hook or heartbeat that runs every turn or every few minutes must never walk the whole board tree. Govern git-ignored state through an explicit allowlist of roots (an unlisted tree is enumerated zero times) and prune wholly exempt directories BEFORE descending. Bound receipt scans by a recency window on the receipt FILE's own mtime, never its directory's: on NTFS an in-place rewrite does not move the parent directory's mtime. Gate commits on RAM plus pagefile maximum, not RAM alone.
mechanism: Host up 6.8 days: paged pool 13.2 GB, nonpaged 5.0 GB (normal 1-2 GB) with 21.7 GB RAM free, while a `RAM - 4` commit gate refused lanes. A 3-minute xperf FILE_IO trace ranked the board heartbeat (4.47M file events), the Stop-hook doc-size check (6.1M) and a checkpoint run (5.2M) first; each walked the whole board incl. its git-ignored state tree. Counted in-process 2026-10-22 (Python touches, not xperf events): doc-size 837,710 paths per run, 155,544 after pruning exempt trees, 47,215 after the allowlist; project tools/session-checkpoint.py 113,786 per Stop event (about 4,700 receipt opens), then 102,566 touches but 663 opens with a per-file 72 h filter (a directory-mtime filter reached 25,882 but lost a receipt rewritten in an older run dir). That project copy is not wired in any settings file today and who launched it in the trace is UNKNOWN; the live per-turn hook is user-scope ~/.claude/hooks/session-checkpoint.py, unchanged.
check: py -3 -m unittest tools.repo_hygiene.test_check_doc_size_walk tools.repo_hygiene.test_session_checkpoint_receipt_window (MLV-App). Elsewhere: read `\Memory\Pool Paged Bytes`; over about 4 GB, run the elevated FILE_IO trace and count `File*` events per process.
supersedes: none
evidence: measured
