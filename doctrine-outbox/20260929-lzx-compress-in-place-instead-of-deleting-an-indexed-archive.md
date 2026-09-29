---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — to reclaim disk from another tool's archive whose index stores file paths, compress in place (NTFS LZX) instead of deleting or moving: 12.35 GiB back, every path still valid

**Symptom (measured):** a build machine sat below its 50 GiB free-space floor for new worktrees, and the largest reclaimable pile was another tool's archive of old session transcripts (`rollout-*.jsonl`, 4,930 files older than 2026-09-01, 23.86 GiB logical). That tool keeps a sqlite state database whose `threads` table stores a `rollout_path` for each archived thread (6,146 rows, per the swarm's read of the schema); deleting or moving the files would have left those rows pointing at nothing, and the tool's behaviour on a dangling path is not ours to test on its live state.

**What worked:** transparent NTFS compression in place, `compact.exe /c /exe:lzx /q /i`, in batches of 40 files. No file was moved, renamed or deleted, so every indexed path stayed valid and the content bytes read back unchanged. The run's own receipt: at start 38.43 GiB free; at the end 50.79 GiB free; `deltaGiB=12.35` over about 21 minutes (13:23Z to 13:44Z), which put the volume back above the floor. Undo is one command (`compact /u /exe /i <files>`). The script skipped files modified in the last day and any file `compact` could not open, so an active transcript was never touched. Earlier in the week the same lever on the board's own run-receipt and quarantine areas freed 2.89 GiB.

**Cause of the trap:** "delete the old archive" is the reflex, and it is only safe when nothing indexes the files. The index is another tool's private state and is invisible from the directory listing.

**Rule:** before deleting or moving files that another tool owns, look for an index that references their paths (a database column, a manifest). If one exists, prefer a lever that keeps paths and bytes intact, such as in-place LZX compression, restrict it to files past an age cutoff and outside the last day, and record free space before and after in a receipt. Read the free-space delta as approximate: the volume was also being written by other work (the mid-run reading was 51.43 GiB, above the end value).

**Falsifier:** after the run, read one compressed file (content hash equal to before) and confirm the tool's index still resolves that path; if it does not, the lever was not path-safe.
