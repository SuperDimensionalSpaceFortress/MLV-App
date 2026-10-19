# doctrine-outbox

Findings that belong on the fleet doctrine bus (trap cards in `specs/mlv-app/cards.md`, `RECEIPTS.md`,
`RULINGS.md`) leave this repo by FILE, never by a session's memory. This directory plus `tools/coordination/doctrine_outbox.py`
is the mechanism, ported from agent-bridge's port of the bus's own reference outbox. Nothing here runs
itself: the unattended drain, the capture hook and the heartbeat debt line are card
DOCTRINE-OUTBOX-ADOPT-MLV-1b and install against this CLI.

**One file for the fleet (OUTBOX-SINGLE-SOURCE-1).** `tools/coordination/doctrine_outbox.py` is not edited
per project: every project value (ref, items directory, finding paths, state files, Law-4 deny terms) is
data in `doctrine-outbox.config.json` at the repo root. The bus steward is asked to land the same bytes as
`tools/factory/doctrine_outbox.py` (handoff card in this directory; adoption is pending; the bus deleted its
reference copy in `665b4566`). Sharing is by pin, not by copy (bus `tools/factory/README.md`: "A tool here is
never copied into a project: the project pins it"): a project records a reviewed bus SHA with
`python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py
--reviewer <who> --reason <why>` and runs `python factory-pins/pin-runner.py run doctrine-outbox -- --config
doctrine-outbox.config.json <command>`. It KEEPS its local copy until a pin-aware release (card
OUTBOX-PIN-AWARE-TOOL-AT-REF-1): the hook `install-hooks` writes and the debt and drain checks still read
`tool_rel`, so deleting the copy bricks the push guard. The digest of the candidate is recorded in that
config's `bus_candidate` and checked by `tests/coordination/test_doctrine_outbox_hooks.py`. Change behaviour by
changing the config; once the bus has the file, change the tool only there and re-pin. The same file also answers the hook
commands `check`, `pre-push`, `stop`, `session-start` and `install-hooks`. `stop` judges the commits not
yet on the remote (`default_range`, or `--ref R` for `R..HEAD`); `pre-push` hands git's stdin on to the
layers after it (`git lfs pre-push`); `install-hooks` leaves the hook executable and splices the guard right
after the shebang and any `set -e` lines, so a hook that ends in `exit 0` or `exec <next hook>` still runs it
(a guard already sitting behind a top-level `exit`/`exec` is moved ahead of it and reported as `repaired`, never
as present; `HOOK_GUARD_UNREACHABLE` stays as a defensive invariant that cannot fire today). A push that creates a branch judges only the commits not on the remote's default branch. `check`
takes a whole number of minutes; anything else is a usage error (exit 2). A config that is
present but invalid is `CONFIG_INVALID` (exit 1, naming the key), distinct from `CONFIG_ABSENT` (exit 2).

**Derive state; this file holds none.** Every value below is a command.

```
python tools/coordination/doctrine_outbox.py debt                       # unsent items on refs/remotes/fork/master; exit 1 = debt, 2 = UNKNOWN
python tools/coordination/doctrine_outbox.py debt --json --bus <clone>  # machine form, also checks markers and published_as on the bus
python tools/coordination/doctrine_outbox.py drain --bus <clone>        # DRY RUN by default; --push publishes
python tools/coordination/doctrine_outbox.py kernel-filing --bus <clone> # kernel filing to its review branch; DRY RUN unless --push
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
target: TRAPS.md          # RECEIPTS.md | TRAPS.md | RULINGS.md | specs/mlv-app/cards.md | specs/mlv-app/cos-concerns.md (other specs are steward-owned)
kind: trap                # receipt | trap | ruling | concern (concern goes only with cos-concerns.md)
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

## Card routes are checked before they ship (CARD-ROUTE-EXERCISE-2)

CoS work order FWO-DEFECT-PREVENT-1 item 3, in `validate --exercise`. It is fail-closed: it replaces the parked
CARD-ROUTE-EXERCISE-1 (PR 479), whose launcher support relied on two hand-written walks over one command line (a safety
matcher and a script discovery) that kept disagreeing about which script would run.

`python tools/coordination/doctrine_outbox.py validate --exercise --bus <bus clone> doctrine-outbox/<file>.md` (the bus
defaults to `$FACTORY_BUS_CLONE`) clones the bus's committed tip into a scratch directory with
`git clone --local --no-hardlinks`, drops its `origin`, checks every command the item names there, without a shell, then
removes that directory by name. The bus is only read. **"Refuse if `--bus` resolves to the live clone" means this (hub
ruling, r2):** no route runs with its working directory, a `-C`/`--git-dir`/`--work-tree` target, or any argument or
`--fill` value (also the value of `--opt=VALUE`) that resolves inside the live bus; that route is `ROUTE_UNSAFE`. A
`--scratch-root` inside the bus is exit 2. "Inside" is decided on identity, not spelling: real path (junctions, symlinks
and 8.3 names followed), `..` collapsed, case folded, trailing separators dropped. `<scratch>` in a route names the scratch
clone. A bus clone behind its own `origin` prints `ROUTE_BUS_STALE <behind>` on stderr (the exit is unchanged). A command is:
- a line of a fenced block whose info string starts `route` (`$ ` or `> ` prompt and `#` lines dropped); or
- a backticked span of two or more words inside a route section, which opens at a `Route:` or `Adopt:` label
  line (bullet and bold allowed) and runs to the next blank line, `#` heading or fence. Bullets right under it,
  such as `- **Pin:**`, are in the section. A one-word span (`debt`, `RULINGS.md`) is a name, not a command.

**Every route gets a safety screen and a static existence check, and neither runs anything.** The screen refuses a route
that is not offline, credential-free and confined to the clone (the `ROUTE_UNSAFE` list below). The existence check needs
the program to resolve on `PATH` or inside the clone, and every script the route names to be a file inside the clone,
symlinks and junctions followed: the script of `python`/`py`, `node` (and its `-r`/`--require`/`--import`/`--loader`
preloads), a shell (and its `--rcfile`/`--init-file`), `deno run`, `bun run`/`bun <file>` (and `--preload`), `pwsh -File`,
`awk -f`, `tclsh`, `perl`, `ruby`, `php`, ... A `bun run build` names a package script, not a file, and is not looked up.
The program is read by what it resolves to as well as by its name, so a link called `helper.exe` that is really `python`
has its script checked. A miss is `ROUTE_MISSING` with a typed reason first in its detail: `PROGRAM_NOT_FOUND`,
`PROGRAM_IS_SHIM` (the `git` found is a `.cmd`/`.bat` shim, which cannot start without `cmd /c`), `SCRIPT_NOT_FOUND` or
`SCRIPT_OUTSIDE_CLONE` (an absolute path, a `..` path or an outward link). This covers the #468 and #469 evidence: a nonexistent runner script, and a
`doctrine-sync debt` no bus carries.

**Only a route whose program is on the execution allowlist is run, and the allowlist is `git`.** It is decided on the
program as the route spells it, a bare name (`git`, `git.exe`; never a path, so `tools/git` and `/usr/bin/git` are not on
it), and on its resolved executable: the first `git` on a `PATH` stripped of every relative entry and every entry inside the
scratch clone, whose real path is not inside the clone and is itself called `git`. Those entries are walked by hand and the
current directory is never consulted (on Windows `shutil.which` searches it before `PATH`, so it would find a `git.exe` planted
in the clone). A `git` that resolves only inside the
clone, or to some other program, is `ROUTE_UNSAFE`; a `.cmd`/`.bat` shim is `ROUTE_MISSING PROGRAM_IS_SHIM`. Every other program is
**existence-checked but not executed**: it prints `ROUTE_NOT_EXECUTED`, which is a skip and not a failure (so a card's
`py -3 tools/x.py` still validates), and it is never launched, whatever its arguments. That includes every shell,
interpreter and wrapper, and the programs a name list would have missed: `pytest` is never run (it executes Python named in
its arguments, its conftest files, `-p` plugins and `PYTEST_ADDOPTS`), nor are `pyw`, `ipython`, `java`, `go`, `php-cgi`,
`jq`, a renamed copy or a link of any of them, or a spelling of a path (`POWERS~1.EXE`, `py.exe.`). An unfilled `<name>`
placeholder on a route that is not run is noted in its detail rather than failing it, and the script check skips an operand
that holds one. Widening the allowlist is the separate later card CARD-ROUTE-EXERCISE-LAUNCHER-ALLOWLIST-1: exact shapes, a
pinned executable and the secondary execution inputs (preload flags, environment) have to be closed first. Because nothing
launches on the strength of the static check's reading of a program's options, the PR 479 bypass class (a check that accepts
one script while the program runs another) cannot reach a launch. Each known repro has an acceptance test row that mocks the
process launch and asserts it is not called:

| Repro (none is on the allowlist) | Outcome |
|---|---|
| B1: `python <outside script that exists>` | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `pwsh -f <outside>` (and `-fi`, `-File:`, `/f`) | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `pwsh <outside.ps1> -File <in-clone>` (pwsh 7 runs the first bare argument) | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `pwsh -NoProfile <outside.ps1> -f` | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `node -r <in-clone> <outside.js>` | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `bash --rcfile <in-clone> <outside.sh>` | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `bash -o x <outside.sh>` | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |
| `python --check-hash-based-pycs default <outside.py>` | `ROUTE_MISSING SCRIPT_OUTSIDE_CLONE`, not launched |

A second row set forces the static check to pass everything and shows each repro is still `ROUTE_NOT_EXECUTED` and still not
launched.

A `git` route that is run gets the interpreter-injection environment stripped: `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`,
`NODE_OPTIONS`, `NODE_PATH`, `PERL5OPT`, `PERL5LIB`, `RUBYOPT`, `BASH_ENV` and `ENV`, plus credential-shaped variables, user
and system git config, every `GIT_*` variable (`GIT_EDITOR`, `GIT_SEQUENCE_EDITOR`, `GIT_PAGER`, `GIT_ASKPASS`, `GIT_SSH`,
`GIT_SSH_COMMAND`, `GIT_EXTERNAL_DIFF`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, ...) and the generic
helpers `EDITOR`, `VISUAL`, `PAGER` and `SSH_ASKPASS`. Git is started as `git -c core.pager=cat -c core.editor=false
-c diff.external= -c core.fsmonitor=false -c commit.gpgsign=false -c tag.gpgsign=false -c core.hooksPath=<an empty directory
beside the clone in the scratch> ...`, so no pager, editor, external diff, hook or signing program is reachable even through
a verb that wants one. A route may not pass `-c` itself. It must be exercisable offline and only in the scratch clone, so
these are `ROUTE_UNSAFE`, whether or not the program would be run:
- a shell operator (pipe, redirect, `&&`, `;`) or an environment-assignment prefix;
- git other than an allowlisted read-only or scratch-local verb (`log`, `status`, `show`, `diff`, `rev-parse`,
  `commit`, `checkout`, ... but never `push`, `pull`, `fetch`, `ls-remote`, `clone`, `remote`, `submodule` or an
  alias), `git -c`, `--config-env`, a `git config` write, and `-C`/`--git-dir`/`--work-tree` outside the scratch clone;
- a git option that starts a helper or writes outside the clone: `--exec`, `--upload-pack`, `--receive-pack` (before or after
  the verb), `--ext-diff`, `-O`/`--open-files-in-pager`, `--output`, `apply --unsafe-paths`, `-S`/`--gpg-sign` on `commit`,
  `tag`, `merge`, `cherry-pick` or `revert`, `tag -s`/`-u`/`--local-user`, `merge -s` and `--strategy` (git looks the strategy
  up on `PATH` as `git-merge-<name>`), and a `!` alias (`git !echo hi`);
- any `gh`, `claude`/`codex` `auth|login|logout`, a network client (`curl`, `ssh`, `npx`, ...), `find -exec`, a URL or
  `user@host:` argument, and a credential assignment (`GH_TOKEN=`, `ANTHROPIC_*=`, `*_TOKEN=`, `*_KEY=`, `*PASSWORD*=`)
  or `--token`.

Each route prints one `ROUTE_OK`, `ROUTE_NOT_EXECUTED`, `ROUTE_MISSING`, `ROUTE_FAILED` (non-zero exit, an unfilled `<name>`
placeholder on a route that is run, or it does not parse), `ROUTE_TIMEOUT` (`--timeout`, default 60 s; the whole process tree
is killed, via a Job Object on Windows and a process group on POSIX) or `ROUTE_UNSAFE` line, then the item name and the
command. Anything but the first two exits 1. Only the last 64 KiB of a route's output is kept. Route text is split like a
POSIX shell (on Windows a backslash path stays whole). Give `<name>` placeholders a value with `--fill name=VALUE`.
`--timeout` must be a finite number of seconds above zero; `nan`, `inf`, `0` and a negative value are refused before any
route runs. A credential's value is never printed: it shows as `NAME=<redacted>` in every diagnostic and route line, whatever
it contains, in the form it was filled and in the form `repr()` writes it. Put a command a card ships but the bus does not
yet have in prose, not in a route.

## Concerns to the CoS (COS-CONCERN-CHANNEL-1)

A project had no way to tell the CoS a concern: only the CoS writes `cos-feedback/`, and a project-owned
file is the one legal shape (bus Law 2). `target: specs/mlv-app/cos-concerns.md` with `kind: concern` is
that file. The body is a `### mlv-app-cc-<yyyymmdd>-<n>` heading and exactly seven `- field: value` lines,
each on one line: `id`, `to` (always `CoS`), `opened_utc`, `expires_utc` (after `opened_utc`, and no more
than 14 days after it: a longer window is refused as `CONCERN_WINDOW_TOO_LONG`), `kind` (`output-format` |
`process` | `shared-tool-defect` | `fleet-pattern`), `evidence` (bus SHAs, public PR comment ids, repo paths)
and `ask` (one sentence). The set is closed, so no field is an approval, a ruling or a permission; free text is
data. The CoS answers each id once, in its own `cos-feedback/mlv-app/concerns.md`:
`<id> ADOPTED|DISTINGUISHED|NEEDS-INFO <reason>`. Open = entries with no answer line and not expired, derived
and never stored.

The drain holds an entry, and the refusal names the reason:
- `CONCERN_CHANNEL_NOT_RATIFIED` until the fetched bus tip's `cos-feedback/README.md` carries the exact duty
  line `- The CoS reads `CoS-CONCERN:` PR comments and every `specs/*/cos-concerns.md` each pass.`: a line EQUAL to
  it (a leading `-` or `*` bullet followed by 1 to 4 spaces or a tab is optional, the line may be indented 0 to 3
  spaces and carry trailing whitespace, nothing else). A line that only starts like it, adds a clause, forbids or
  negates it, names only R1, sits at 4 or more spaces or a tab, has 5 or more spaces after the bullet (both are
  indented code blocks), or sits inside a ``` fence, a `~~~` fence or an HTML comment (one line or many) does not
  count, so nothing is written to a file nobody reads. Lines end only at `\n`, `\r\n` or `\r`, as in Markdown: a
  vertical tab, form feed, `\x85`, U+2028 or U+2029 inside the text does not start a new line. The scan does
  not parse inline code, so a `<!--` inside a code span before the line keeps the gate shut (it fails closed);
- `CONCERNS_PATH_IS_LINK` when `specs` or `specs/mlv-app` at the tip is a link (120000), a gitlink (160000) or
  any other entry that is not a directory (a regular file, say, worded "not a directory: mode 100644"), and
  `CONCERNS_PATH_NOT_REGULAR_FILE` when the tip's `specs/mlv-app/cos-concerns.md` is a link or any entry
  other than a regular 100644 blob: that concern alone is refused and the rest of the drain goes on. A card gets
  the same walk over its own path under `CARDS_PATH_IS_LINK` (the same three cases at `specs` or `specs/mlv-app`);
- `CONCERN_EXPIRED` when `expires_utc` is not after the clock at drain time;
- `CONCERN_DUPLICATE_ID` when the id heads more than one committed item (all of them are held), or is already
  in the tip's `specs/mlv-app/cos-concerns.md`; the refusal names the fields that differ from the published
  entry, or says none does. An id names exactly one concern.

A concern about one PR goes instead as a comment on that PR's thread starting `CoS-CONCERN:`. The CoS reads
those comments too: the duty line is live at the bus `cos-feedback/README.md:65`.

## Traps are cards (bus R14.1)

From the bus commit that landed `tools/validate-cards.mjs` (R14 packet 2), a new trap filing is a card in
`specs/mlv-app/cards.md`, not a `TRAPS.md` entry. The drain keys this on the fetched bus tip, never on a
date: when the tip carries the validator, an unsent `target: TRAPS.md` item is refused
(`R14_1_TRAP_FILING_IS_A_CARD`, exit 1) and nothing is appended. An entry already on the bus stays sent.
Receipts and rulings keep their targets.

```markdown
---
target: specs/mlv-app/cards.md
kind: trap                # cards take trap items only
source_commit: PENDING
law4: attested
---
## mlv-app/<id>
rule: <the rule>
mechanism: <why it happens>
applies: <comma-separated fleet members; omit for every board>
check: <a command, or a law-6 pointer to a pushed harness>
supersedes: none
evidence: measured
```

The bus validator's header is the format contract (bold-bullet `- **Rule:** ...` fields are accepted too).
A card is at most 15 lines and 2,000 bytes, and the drain adds one line to it, the marker
`- **Outbox:** outbox:<key> mlv-app:<sha12>`. The first card creates the file with a one-line title. Before
any push, the drain runs the tip's own `validate-cards.mjs` with node over the would-be file in its temp
worktree; a rejection refuses the publish (`CARD_INVALID`), and a missing validator or node refuses the card
(`CARD_VALIDATOR_ABSENT`, `CARD_VALIDATOR_NODE_ABSENT`). `validate` checks the heading only, not the fields.

## Kernel filings go to a review branch, never master

A factory-kernel filing (bus kernel K12 and section 4, `adjudications/factory-kernel/README.md`, PROMPT-K
sections 4-5) is not an item. Commit it as `doctrine-outbox/kernel-filing/<yyyymmdd>/`, holding exactly
`adjudications-factory-kernel-mlv-app.md` (the whole filing) and, optionally, `specs-mlv-app-block.md` (a
block for the project's own spec). `kernel-filing --bus <clone>` publishes them to bus branch
`review/mlv-app-kernel-<YYYY-MM-DD>`: it rewrites `adjudications/factory-kernel/mlv-app.md` wholesale and
appends the block, with an `outbox:<key>` marker, to `specs/mlv-app.md`. A revised block for the same
date replaces the earlier one: the spec is rebuilt from where the branch left bus master. It writes no
other path. The branch comes from the date directory, and no parameter can name a branch or a path. The
branch is created from bus master, or fast-forwarded when every commit on it is this outbox's (else
`KERNEL_BRANCH_NOT_OURS`). Any other file in the date directory is `KERNEL_FILING_EXTRA_PATH`; an empty or
whitespace-only filing or block is `KERNEL_FILING_EMPTY`. Every Law-4 class and deny term is screened,
with no word cap, because a filing is wholesale. Every success (a push, a lost-ack recovery, a no-op) is
proven by `ls-remote` equal to the certified commit (R7), else `PUSH_VERIFY_FAILED` with no ledger row,
and recorded in the sent ledger with `route: kernel-filing`. The key covers the bytes only, never the
source commit. A re-run is a no-op, by the ledger or, on a fresh clone, by the bytes already on the branch.

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
