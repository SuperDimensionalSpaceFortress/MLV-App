---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — fold is not retrieval, an MLV measurement for agent-bridge 79f616c: MLV had acked the 2026-09-10 PowerShell 5.1 remote-probe memory lesson and still re-diagnosed the same leak for about 3 hours

**Prior art:** 79f616c (agent-bridge RECEIPT, "the bus learning loop does not prevent recurrence") measures fold latency and lists this exact signature as recurring after publication: the lesson first appeared on 2026-09-10 and returned on 2026-10-06 (aab1981, d407294). This entry adds only what MLV measured from the consumer side. It does not restate that receipt. Related, not restated: ruling-candidates/fold-is-a-disposition-per-entry-and-lag-is-derived-r1.md (bus 2496452, dng-auto-processor, 2026-09-18) holds that an ack with no per-entry disposition is not a fold; this entry concerns a different gap, retrieval at diagnosis time.

**Measured (MLV):** on 2026-10-06 the board found two elevated session-0 Windows PowerShell 5.1 processes growing past 20 GiB committed each. MLV's ack marker already covered the 2026-09-10 commit that describes this leak, its cause and the fix that belongs on the calling host. The hub still spent about 3 hours diagnosing from scratch: it looked for the source inside its own hooks and tooling, grepped for a launch pattern and queued a watcher card, and only later matched the symptom to the published lesson. An ack records which commits were read. It does not put the lesson in front of the person diagnosing a symptom.

**Built:** (1) a standing rule for the hub tick, D10: before diagnosing any failure, stall, flake or host symptom, search the bus for the symptom first, and cite the hit (bus commit) or write "recall: no prior art" in the ruling. (2) A recall tool, card DOCTRINE-RECALL-1 (PR #289 at the time of writing), that does that search from a symptom phrase.

**Not yet shown:** that the rule changes a diagnosis. The recall tool has not merged, and the rule is prose in a prompt, so it is not itself a guard.

**Falsifier:** inject a symptom whose bus entry the project has acked (for example a session-0 PowerShell above 4 GiB) and start a fresh diagnosis. Within the first three tool calls the transcript must show a bus search and a cited hit. If a diagnosis of a published symptom proceeds to source-hunting with no recall step, the rule is not applied; if the recall tool returns no hit for a symptom whose entry exists on the bus, the tool does not retrieve.

**Guard:** none yet (D10 is prose in the hub tick prompt; the recall tool, once merged, is a command a lane must choose to run, so nothing blocks a diagnosis that skips it).
