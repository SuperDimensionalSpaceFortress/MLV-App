---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — a provider "monthly spend limit" 429 is an account-side wall: the lane receipt said "wait, no rotation implied" while every Claude-engine lane refused and the Codex lanes kept working

**Symptom (measured):** two Claude-engine lanes were refused in the same minute (11:42Z). A read-only review lane ran 381 s (USD 6.23); an editing implementer lane ran 4255.8 s, 113 turns (USD 4.33). Both ended `exit=1` with `api_error_status=429 You've hit your monthly spend limit`, and the message named a weekly reset three days away. A Codex read-only lane dispatched at 12:26Z completed normally (`exit=0`, 250.5 s). Both lane receipts classified the refusal `providerRefusal kind=provider-rate-limit` and printed the remedy "wait and re-dispatch the same prompt; no rotation implied". The ledger entry at 12:27Z named the owner's CLI sign-in as the expected unblock; it did not record the act itself. The account-drift receipts show a repair window opened at 12:40Z on a `CLI_BEHIND_DESKTOP` verdict and `ALIGNED` at 12:46:37Z, and a Claude implementer lane started at 12:46:28Z completed (`exit=0`, 4651.9 s), days before the reset the message promised. The drift check had printed `ALIGNED` at 11:40Z and 11:45Z, around the refusals: it does not see a depleted account.

**Cause:** the runner maps every `provider-rate-limit` kind to one remedy. A transient rate limit (retry later) and a spend wall on the signed-in account (nothing works until the account changes) share a kind but need opposite responses. The message text is the only thing that tells them apart, and the remedy line did not read it.

**Rule:** treat a 429 whose text says "spend limit", "usage limit" or "weekly limit" as an account-side wall. Before re-dispatching the same prompt, check that the CLI's signed-in account is the intended one and can still spend (a small probe call, not the identity check alone: an aligned account can still be depleted). Keep other engines' lanes advancing and queue Claude-only work instead of retrying in a loop. Do not trust a "no rotation implied" line for this text.

**Falsifier:** grep the lane receipts for `monthly spend limit` or `usage limit`. Any that also carry the "no rotation implied" remedy show the mapping gap. Then run a one-line Claude probe after the account is fixed: a success days before the stated reset confirms it was the account, not the clock.

Relates to `TRAPS.md` > the dng-auto-processor 2026-09-14 entry beginning "A spend-limit refusal wrote `available: false` for a whole family", which measured the same refusal read as an absent model; and the Conjugal 2026-09-21 entry beginning "A PROVIDER-LIMIT CLASSIFIER FAILS CLOSED ON THE ONE PHRASE IT WAS NEVER TAUGHT", which measured a different classifier missing the exact phrase `monthly spend limit`.
