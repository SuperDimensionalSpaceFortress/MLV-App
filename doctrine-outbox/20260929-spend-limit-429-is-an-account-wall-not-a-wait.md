---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — a provider "monthly spend limit" 429 is an account-side wall: the lane receipt said "wait, no rotation implied" while every Claude-engine lane refused and the Codex lanes kept working

**Symptom (measured):** two Claude-engine lanes were refused in the same minute (11:42Z). A read-only review lane had run 381 s (USD 6.23 spent); an editing implementer lane had run 4255.8 s, about 71 minutes and 113 turns (USD 4.33 spent). Both ended `exit=1` with `api_error_status=429 You've hit your monthly spend limit`, and the message named a weekly reset three days away. A Codex read-only lane dispatched at 12:26Z on the same machine completed normally (`exit=0`, 250.5 s). Both lane receipts classified the refusal `providerRefusal kind=provider-rate-limit` and printed the remedy "wait and re-dispatch the same prompt; no rotation implied". Following that advice would have meant waiting days. The ledger instead recorded the unblock as the owner's CLI sign-in, and a Claude implementer lane started at 13:47Z completed (`exit=0`), days before the reset the message promised.

**Cause:** the runner maps every `provider-rate-limit` kind to one remedy. A transient rate limit (retry later) and a spend or usage wall on the signed-in account (nothing works until the account changes or the limit is raised) share a kind but need opposite responses. The message text is the only thing that tells them apart, and the remedy line did not read it. The mid-run timing makes it worse: the wall hit lanes that had already spent real money, so the refusal arrived as a failed run with cost attached rather than as a clean pre-flight no.

**Rule:** treat a 429 whose text says "spend limit", "usage limit" or "weekly limit" as an account-side wall. Before re-dispatching the same prompt, check that the CLI's signed-in account is the intended one and can still spend (a small probe call, not the identity check alone: an aligned account can still be depleted). Keep other engines' lanes advancing meanwhile, and route Claude-only work to a queue instead of retrying it in a loop. Do not trust a "no rotation implied" line for this text.

**Falsifier:** grep the lane receipts for `monthly spend limit` or `usage limit`. Any that also carry the "no rotation implied" remedy show the mapping gap. Then run a one-line Claude probe after the account is fixed: a success days before the stated reset confirms it was the account, not the clock.

Relates to `TRAPS.md` > the dng-auto-processor 2026-09-14 entry beginning "A spend-limit refusal wrote `available: false` for a whole family", which measured the same refusal read as an absent model.
