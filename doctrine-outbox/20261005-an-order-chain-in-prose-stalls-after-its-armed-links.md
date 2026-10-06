---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — an ORDER chain that lives in prose stalls after its armed links: a four-card playback order and a colour fix each sat idle (about 15 and 20 hours) because nothing watched the owner's goal

**Trap:** the hub ruled "ORDER = A -> B -> C -> D" for the playback goal and armed only the first two links as scheduled actions. When link two ended, links three and four existed only as ruling prose, so venue playback stopped for about 15 hours. A colour-cast fix hit the same shape: its measure-only change merged, an evidence leg showed a room-wide cast the search could not see, the follow-up card was queued and never dispatched, and the chain stalled about 20 hours until the owner asked. Both owner goals were unmet and nothing in the system noticed.

**Rule:** a queue that exists only as prose is not a queue. (1) Treat every "ORDER = A -> B -> C" line as a queue: the first link with no declared run, whose predecessor has merged or ended, is due now. (2) When dispatching link N, arm link N+1 in the same act if its prompt can be written; otherwise the next tick takes it. (3) Keep a standing goal watch per owner goal, restated verbatim: if no lane serving it has started in 3 hours and the newest evidence is not at target, the board is stalled and the tick acts (dispatch the next link, or rule a new order). (4) Report "goal watch: ok or stalled -> action" in every tick summary, until a merged PR plus venue evidence closes the goal.

**Also observed:** both chains later ended in a refused launch (a free-space gate), so the ledger named lanes that never started. The watch treated a declared lane with no receipt as stalled and re-dispatched.

**Mechanism:** an armed link fires on its predecessor's end; an unarmed link fires on nobody. Prose is read by sessions, and sessions end.

**Falsifier:** pick any ORDER ruling in the ledger. Every link must be declared with a run folder or receipt, or armed as a pending action, or have a live predecessor. A link with none of these whose predecessor ended over 3 hours ago is stalled, and the next tick summary must say so and name its action. Each owner goal needs a goal-watch line in the newest tick summaries; a goal missing from 3 consecutive ticks means the watch is missing.
