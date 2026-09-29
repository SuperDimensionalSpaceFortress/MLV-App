---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — to reclaim disk from another tool's archive whose index stores file paths, compress in place (NTFS LZX) instead of deleting or moving: about 13.4 GiB saved, every path still valid

**Symptom (measured):** a build machine sat below its 50 GiB free-space floor, and the largest reclaimable pile was another tool's archive of old session transcripts (`rollout-*.jsonl`, 4,930 files older than 2026-09-01, 23.86 GiB logical). That tool's sqlite state database stores a `rollout_path` per archived thread (6,146 rows, from a read-only query of its index during the swarm, not re-queried by the producer of this note). Deleting or moving the files would leave those rows dangling, and the tool's behaviour on a dangling path is not ours to test on its live state.

**What worked:** NTFS compression in place, `compact.exe /c /exe:lzx /q /i`, in batches of 40 files. Nothing was moved, renamed or deleted, so every indexed path stayed valid. Files modified in the last day, or that `compact` could not open, were skipped. Undo is `compact /u /exe /i <files>`.

**Direct measurement (2026-09-29 about 15:55Z, `compact /q` on the archive directory's `*.jsonl`):**

> 4930 are compressed and 1216 are not compressed. 28,637,436,777 total bytes of data are stored in 14,272,831,740 bytes. The compression ratio is 2.0 to 1.

The 4,930 compressed files held about 25.6 GB logical; the 1,216 uncompressed are about 3.0 GB. So about 25.6 GB now occupies about 11.2 GB: a saving of about 14.4 GB (about 13.4 GiB), reversible, every indexed path intact.

**Do not read free-space change as the saving.** The run's receipt shows free space 38.43 to 50.79 GiB (`deltaGiB=12.35`), but two other reclaim jobs (worktree archive batches 3 and 4) freed space over the same window, so that delta does not measure what LZX saved.

**Rule:** before deleting or moving files another tool owns, look for an index that references their paths (a database column, a manifest). If one exists, prefer a lever that keeps paths and bytes intact, such as in-place LZX, limited to files past an age cutoff, and measure the saving with `compact /q`, not a before/after free-space reading.

**Falsifier:** `compact /q` on the directory reports the stored and total bytes (above: 28,637,436,777 stored in 14,272,831,740). If the tool's index no longer resolves a compressed file's path, the lever was not path-safe.
