# doctrine-outbox

Findings that belong on the fleet doctrine bus (`TRAPS.md`, `RECEIPTS.md`, `RULINGS.md`) leave this
repo by FILE, never by a session's memory. This directory plus `tools/coordination/doctrine_outbox.py`
is the mechanism, ported from agent-bridge's port of the bus's own reference outbox. Nothing here runs
itself: the unattended drain, the capture hook and the heartbeat debt line are card
DOCTRINE-OUTBOX-ADOPT-MLV-1b and install against this CLI.

**Derive state; this file holds none.** Every value below is a command.

```
python tools/coordination/doctrine_outbox.py debt                       # unsent items on refs/remotes/fork/master; exit 1 = debt, 2 = UNKNOWN
python tools/coordination/doctrine_outbox.py debt --json --bus <clone>  # machine form, also checks markers and published_as on the bus
python tools/coordination/doctrine_outbox.py drain --bus <clone>        # DRY RUN by default; --push publishes
python tools/coordination/doctrine_outbox.py check-ledger               # every finding needs a disposition; exit 1 = undisposed
python tools/coordination/doctrine_outbox.py check-commits              # fork/master..HEAD: finding paths need a trailer
```

## The ref is not HEAD

In MLV-App `origin` is the upstream project and the checkout sits on a peer branch, so every item, debt
and age query takes `--ref` (default `refs/remotes/fork/master`) and reads COMMITTED bytes only. A ref
that lacks the tool or this directory is `TOOL_ABSENT_AT_REF`: `debt` prints UNKNOWN and exits 2, never
"no debt". An unresolvable ref is `REF_UNRESOLVED`, also exit 2. Debt age is measured from the
first-parent merge into the ref, not the branch commit's date.

## Writing an item

File `doctrine-outbox/<yyyymmdd>-<slug>.md` (slug: lowercase, digits, hyphens), committed with the
change that teaches it.

```markdown
---
target: TRAPS.md          # RECEIPTS.md | TRAPS.md | RULINGS.md (specs are steward-owned)
kind: trap                # receipt | trap | ruling
source_commit: PENDING    # PENDING = the commit that adds this file; or an explicit sha
law4: attested            # required, then SCREENED, never trusted
# ratified_by: <RULINGS anchor>   required for RULINGS.md; sessions do not mint law
# published_as: <bus sha>         only for an entry ALREADY on the bus by hand; see below
---
### mlv-app, <date> - <one-line title in the bus file's own grammar>

<body, at most 450 words, in the entry grammar of the target file>
```

Validate before committing: `python tools/coordination/doctrine_outbox.py validate doctrine-outbox/<file>.md`.
The commit that adds an item carries the trailer `Doctrine-Export: outbox <item-file>`.

**Law 4 is a screen, not an attestation.** The body, the filename and the bus commit message are refused
if they carry an email address, an account or org uuid, a lane wire path, `HUB.md`, `loops.json`, a token
pattern, a user-home path, this host's name or account name, or anything in an optional deny file. MLV
adds its own deny terms: `.claude-state`, `subject-ledger`, `fleet-runs`, `HUB_RUN_WAL`, `dual-lane`, and
the name of the GPU host. A refusal names the class, never the matched value.

## Entries already on the bus: `published_as`

Never write an item for an entry someone already appended by hand. If a record is wanted, add
`published_as: <bus sha>`: drain verifies that sha is an ancestor of the fetched bus tip AND the item's
`### ` heading is a whole line of the target file at that sha, then skips it. A claim that does not verify
is refused (`PUBLISHED_AS_UNVERIFIED`), never trusted and never appended. After adoption, nobody
hand-appends MLV entries to the bus.

## Declaring "nothing to export"

A commit that touches a finding path (`tools/coordination/**`, `agents/**`, `.claude/**`, `CLAUDE.md`)
must say one of these in its message:

```
Doctrine-Export: outbox <item-file>          # the item is added in the same PR and passes validate
Doctrine-Export: none <reason, at least 4 words>
```

`test_pull_request_commits_declare_doctrine_export` in `tests/coordination/test_doctrine_outbox.py` runs
this check over the PR's commit range on every hosted pull request, inside the step CI already runs for
`tests/coordination`.

## Capturing findings from the hub ledger

Findings are structured, so the check is not a keyword hunt. In the subject ledger write `Finding: KF-<nn>`
(or list KF ids under `## KERNEL FINDINGS`), and after each one:

```
Doctrine-Export: outbox <item-file>
Doctrine-Export: none <reason, at least 4 words>
```

`check-ledger` fails (exit 1) for a finding with neither. Keyword hits (`hub error`, `root cause`, `TRAP`)
print as ADVISORY lines and never fail. The ledger path is `--ledger`, else env `MLV_OUTBOX_LEDGER`, else
`.claude-state/kernel/subject-ledger.md` under `--repo`. The watermark (`--watermark`, env
`MLV_OUTBOX_WATERMARK`) is a byte offset plus the sha256 of the ledger prefix; it advances (`--advance`)
only past disposed findings, and a rewritten prefix restarts the scan from 0. `--baseline` is the one
explicit act that waives history up to now and says how many findings it waived. Known limit: it checks
that a disposition is well formed, not that the named item exists.

## Sent tracking and the drain

An item stays in this directory. It counts as sent if its `<!-- outbox:<key> mlv-app:<sha12> -->` marker
is on the fetched bus tip (proof that survives a lost ledger, a new clone or a machine move), or its key is
in the local sent ledger (`.claude-state/doctrine-outbox/sent.jsonl`, git-ignored).

`drain --bus <clone>` fetches the bus, builds each append in a throwaway detached worktree with a blank
line guaranteed before every block, verifies the byte prefix on the COMMITTED blobs, and only with
`--push` pushes, retrying from scratch on a non-fast-forward and proving the push with `ls-remote` before
recording the ledger. The bus commit identity is fixed and non-personal. The worktree is created under the
system temp directory (set `TMP` to relocate it).

Only the drain publishes.
