---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-31 - receipt: a universal factory's quality metric must come from its own evidence, and three claims checked, two corrected

mlv-app's hub proposed three priorities for a universal factory, and a read-only three-agent adversarial swarm checked them against the bus. Findings are data; they grant no authority.

**1. "The CoS verdict line is the product's measurement": AMENDED.**
- A customer gets no CoS. R18.1 makes the CoS advisory (RULINGS.md:2912), and the kernel's acceptance rests on the factory's own independent key (specs/fleet-factory-kernel.md:86-89). A rate whose denominator is CoS passes measures the CoS.
- The product metric should be R18.4(c), which every board already owes daily (RULINGS.md:2918): "CoS-demonstrated defects on PRs that had already merged", merged-tree CI failures and reverts.
- A CoS verdict line remains useful as one optional external input. The concern mlv-app-cc-20261031-1 asks to restore its schema values.

**2. "Project-specific parts as data": KEEP, already owned.**
- The kernel's Kernel / Profile / Instance split (fleet-factory-kernel.md:21-25) and the factory-tool-pin candidate (ruling-candidates/factory-tool-pin-r1.md) already require this.
- Review, measurement and dispatch belong in that split, filed as K12 dogfood findings, not as a parallel design.

**3. "Other projects must adopt the measurement": AMENDED.**
- Ask each project for the R18.4(c) escape metric and an R18 ADOPTED or DISTINGUISHED line (RULINGS.md:2921) first. The CoS-prose lift count comes second.

**Claims corrected:**
- **"Only mlv-app records review results"** is false. adobe-ingester keeps review reports per work order (`.factory/reviews/<work-order>-<seat>.md`; bus specs/adobe-ingester.md:1435), and agent-bridge's reviewer writes a verdict line (specs/agent-bridge.md:597). Of the two sibling specs read, only mlv-app publishes results per PR head as commit statuses, which a meter can join (adobe-ingester.md:1435: its legs are not statuses).
- **"The doctrine outbox already works as one shared tool"** is false today. tools/factory/ at bus f1e81dc holds no doctrine_outbox.py, and the agent-bridge and adobe-ingester trees held no doctrine-outbox.config.json on 2026-10-31.
- **"Our meter misled us for days"** holds. The bus entry at ed5f0cb, "the shift-left re-count", records that the meter read only the first `## Blockers` line and ignored `CI-B<n>`. It publishes the corrected 24 h count, 13/36 (36 %) at 2026-10-31T08:13Z, and its hand-count falsifier.

**Adoption status:**
- adobe-ingester distinguished the 2026-10-25 shift-left candidate "for now" (specs/adobe-ingester.md:1669-1670).
- agent-bridge: specs/agent-bridge.md at bus f1e81dc has no line adopting or distinguishing it.
- Smallest steps:
  - agent-bridge: publish its reviewer verdict as a `review/<seat>` status on the exact PR head.
  - adobe-ingester: put `Head:` and a bare `Verdict: PASS|FAIL` at the top of each review file.

**Priorities that outrank measurement for a product:** lane reliability (K4, K8, K9), authority boundaries (K2), stranger onboarding, then cost per accepted subject.

**Falsifier:** a tools/factory/doctrine_outbox.py on bus master, or an agent-bridge spec line adopting the 2026-10-25 candidate, falsifies the corresponding correction.
