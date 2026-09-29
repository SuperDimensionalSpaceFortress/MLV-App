---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — to reclaim disk from another tool's archive whose index stores file paths, compress in place (NTFS LZX) instead of deleting or moving: about 13.4 GiB saved, every path still valid

**Symptom (measured):** a build machine sat below its 50 GiB free-space floor, and the largest reclaimable pile was another tool's archive of old session transcripts (`rollout-*.jsonl`, 4,930 files older than 2026-09-01, 23.86 GiB logical). That tool's sqlite state database stores a `rollout_path` per archived thread (6,146 rows, from a read-only query of its index during the swarm, not re-queried by the producer of this note). Deleting or moving the files would leave those rows dangling, and the tool's behaviour on a dangling path is not ours to test on its live state.

**What worked:** NTFS compression in place, `compact.exe /c /exe:lzx /q /i`, in batches of 40 files. Nothing was moved, renamed or deleted, so every indexed path stayed valid. Files modified in the last day, or that `compact` could not open, were skipped. Undo is `compact /u /exe /i <files>`.

**Direct measurement (`compact /q` on the archive directory's `*.jsonl`, saved as a receipt file at 2026-09-29T16:47:53Z; an earlier identical query at about 15:55Z printed the same file counts):**

> 4930 are compressed and 1216 are not compressed. 28,637,436,777 total bytes of data are stored in 14,272,831,740 bytes. The compression ratio is 2.0 to 1.

`compact` puts the logical size first: 28,637,436,777 bytes is the **total** (what the files hold) and 14,272,831,740 bytes is what is **stored** on disk. The saving is the difference, about 14.36 GB (about 13.4 GiB), reversible, every indexed path intact. Cross-check: the 4,930 compressed files held about 25.6 GB logical, now about 11.2 GB stored.

**Do not read free-space change as the saving.** The run's receipt shows free space 38.43 to 50.79 GiB (`deltaGiB=12.35`), but two other reclaim jobs (worktree archive batches 3 and 4) freed space over the same window, so that delta does not measure what LZX saved.

**Rule:** before deleting or moving files another tool owns, look for an index that references their paths (a database column, a manifest). If one exists, prefer a lever that keeps paths and bytes intact, such as in-place LZX, limited to files past an age cutoff, and measure the saving with `compact /q`, not a before/after free-space reading.

**Falsifier:** `compact /q` on the directory prints "<total> total bytes of data are stored in <stored> bytes". A real saving has stored below total (above: total 28,637,436,777, stored 14,272,831,740); stored at or above total means nothing was saved. If the tool's index no longer resolves a compressed file's path, the lever was not path-safe.
