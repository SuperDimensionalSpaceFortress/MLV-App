---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — a read-only lane's sandbox is denied a UNC network share, so its recon returns UNKNOWN on files the orchestrator can read directly

**Symptom (measured):** a read-only Codex breadth-recon lane (sandbox `read-only`, 250.5 s, `exit=0`, receipt `state: complete`) was asked to explain two failed venue legs from result files that a remote test host had written to a network share. Its answer for both legs was `class: unknown`, with the sentence "Access to the path ... is denied" naming the share's outbox folder and a self-reported failure: it could not read either result file, so it could not quote failure lines or verify the exit codes. The orchestrator then read the same files directly, read-only, and extracted what the lane could not: one leg showed 928 drawn frames presenting nothing after the first 21, an application-side finding; the other showed a fail-closed timeout, later corrected by a live owner observation.

**Cause:** the lane runner launches a read-only Codex lane inside a sandbox whose file access is confined to local paths. A `\\host\share` path is outside it, and the denial arrives as an ordinary read error in the lane's own output. The lane did the honest thing and reported UNKNOWN, but the receipt still says `complete`, so a dashboard that reads only `state` counts the recon as done. The orchestrator's own session was not sandboxed that way, which is why the same read worked there.

**Rule:** evidence that lives on a network share is read by the orchestrator, or copied to a local path before the lane is dispatched, and the brief names the local copy. When a recon lane returns `unknown` with an access-denied sentence, treat it as a delivery failure of the brief, not as a finding about the system under test; do not re-dispatch the same brief expecting a different answer. When you write the brief, list every path the lane must read and check each one is local.

**Falsifier:** from inside the same lane configuration, read one file on the share. A denial confirms the sandbox boundary, and the same file read from an unsandboxed shell succeeds.

Relates to the `RECEIPTS.md` bullet "Worker-env credential blindness" (a worker's launch environment differing from the interactive one): the same class, filesystem reach instead of credentials. Also relates to `TRAPS.md` > the dng-auto-processor 2026-09-13 entry "A Codex `--approve-for-me` sandbox on Windows cannot reach an administrative share", which measured the same sandbox boundary on a different share type.
