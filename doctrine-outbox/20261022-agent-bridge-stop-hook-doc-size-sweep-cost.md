---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/agent-bridge-stop-hook-whole-tree-doc-size-sweep-25s
rule: agent-bridge owns this; MLV-App does not edit it. Its per-turn Stop hook must not run the full doc-size sweep. Check only documents changed this turn, or cache verdicts by (mtime, size), or run the full sweep at pre-push or on a schedule. Keep any walk pruned of exempt trees before descending (precedent mlv-app/per-turn-hook-tree-walks-grow-the-kernel-pool), and compare size before reading a 1 MiB prefix per file.
mechanism: Measured 2026-10-22, read-only, agent-bridge 3760d5de. `.claude/settings.local.json:27` registers `hook_doc_size.py --stop` as a Stop hook; `tools/hook_doc_size.py:117` runs `check_doc_size.py --sweep --quiet-ok`. One sweep: 23.6 s in-process, 25.4 to 26.3 s over three timed runs; 600,174 IO-Other ops (about 25,400/s), 501 MB read, 0 written; 7,276 files governed, 6,539 from an unpruned `rglob` of the git-ignored state tree (`tools/check_doc_size.py:424`). It exits 1, so `--quiet-ok` prints a 39 KB report each time. By code reading the cost is `is_wal_copy` (:238-263, opens every file, reads up to 1 MiB) and `nested_checkout_root` (:292). A host census ranked it first for IO Other at 23,676 ops/s.
check: run agent-bridge `check_doc_size.py --sweep --quiet-ok` in-process under `py -3 -I -B`, read `psutil.Process().io_counters()` and wall time. Fixed means under about 5 s and 100,000 other-ops per Stop event.
applies: agent-bridge, mlv-app
supersedes: none
evidence: measured
