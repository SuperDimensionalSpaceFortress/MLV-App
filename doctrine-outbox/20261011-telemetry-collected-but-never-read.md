---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/telemetry-collected-but-never-read
rule: every quality field a run already records gets a standing reader that runs each tick, flags it against an owner-visible bar, and needs a live fix card for every flag kind; a session reads that view before it reports any benchmark, and treats a long-frame or hitch number as a finding, never as tool noise.
mechanism: a board reads only the metric each card was built to measure. Every playback receipt for a day carried present-gap and present-setup-stall fields (84 of 98 legs over the bar, periodic stalls of roughly 100-300 ms on two GPU hosts), and a session even reported p99 of 700-1300 ms as tool noise. The owner found the stutter on screen first.
check: a scan over the newest receipts prints one FLAG line per leg over the bar, and the tick summary names which live card covers each flag kind; a flag kind with no covering card must have one declared in that same tick
supersedes: none
evidence: measured
