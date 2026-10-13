---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-12 — R16 adopted: every lane resolves its family's newest model at high effort, the codex resolver now refuses a stale or non-listed catalogue, and a pinned codex runs only models its promotion proved

**Before:** the lane table already named an alias per Claude lane and a tier per Codex lane (LANE-MODEL-CURRENCY-1), but four R16 requirements were open. The tier resolver took the highest version in the models cache with no age limit, no visibility check and no high-effort check. The receipt did not say how old the list was. The private codex pin had been promoted on one model (the luna tier) while the sol tier had moved to `gpt-6.1-sol`. Nothing read the receipts to say a lane had fallen behind.

**Built:** (1) The resolver selects only models whose visibility is `list` and whose reasoning levels include `high`, refuses a cache fetched more than 7 days ago with the typed error `cache-stale`, and refuses one with no parseable fetch time; it stays fail-closed, where R16.2 allows a fallback to the last good id, so a lane does not start rather than start on a guess. The receipt records `codexModelsFetchedAt` and the cache writer's `codexModelsClientVersion`. (2) The pin promoter exec-probes every lane tier's current model (sol, luna, astra) and writes `probedModels`; a pinned read-only lane whose model is not listed there runs on the global CLI with the state `PIN_MODEL_UNPROBED` and one log line. (3) `check-lane-model-currency.py` reads the lane receipts and reports STALE when a lane's two latest runs both used an id older than its family's newest, or any effort was not `high`. (4) Review keys: sol is key A; key B is opus, except fable when the card's implementing lane was opus (`Select-ReviewKeyB.ps1`). Duty posture: sonnet implements by default (83 percent DONE at USD 1.37 a round against opus at 48 percent and USD 7.97); opus and sol share design and planning; fable plans and is the fallback key; astra arbitrates contested designs; luna does breadth recon.

**Receipts today:** opus resolved to `claude-opus-5-5`, sonnet to `claude-sonnet-5-5`, fable to `claude-fable-5-1`, sol to `gpt-6.1-sol`, luna to `gpt-6-luna`, astra to `gpt-6-astra`. The currency check over the last 72 hours of receipts reads CURRENT for every lane that ran. Older receipts hold effort values other than `high` (low, xhigh), which the check flags when the window includes them.

**Falsifier:** run the check with a family's newest id supplied that no lane has used yet; the lane must read STALE. Point the resolver at a cache whose fetch time is 8 days old, or whose newest sol entry is hidden: it must name the stale cache, or resolve the older listed model.
