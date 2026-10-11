---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/meter-fails-open
rule: any meter, waiter or scan reports exactly one of OK | DUE | UNKNOWN | GAVE-UP; an unknown reading is never read as zero, and an UNKNOWN that lasts past 2 beats is reported as DUE.
mechanism: the fold-debt meter treated a failed recall (exit 4, no result) as nothing owed, so the meter read clean at the moment it could not see. A reading of zero and a reading that could not be taken look identical to the next consumer unless the output type separates them.
check: a canary input that forces the probe to fail must make the meter print UNKNOWN and exit non-zero; a per-meter unknown-streak counter is printed beside the reading and the meter turns DUE when it passes 2.
supersedes: none
evidence: measured
