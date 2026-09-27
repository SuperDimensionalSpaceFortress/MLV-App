# Automatic error remediation and hub adjudication

Standing user instruction, September 7, 2026: "Errors need to be automatically
remediated and adjudicated with wisdom and hub lanes and swarm of Luna agents.
Update durable memory and workflows". This extends the August 31 hub-adjudication
ruling in `tools/coordination/Invoke-Workstream.ps1`.

September 8 correction: the owner directed, "Dont ask me questions or pull me
into the loop. Adjudicate next steps using hub lanes and swarms of adversarial
low inference agents like Luna". Resolve ordinary technical decisions through
bounded adversarial Luna reviews and the hub, record the decision and evidence,
and continue execution. Do not turn an adjudicable repair into another owner
question. Preserve objective validation failures and genuine access blockers.
The specific synthetic GUI reference decision that prompted this direction is
recorded in `agents/release-and-regression.md`; it does not activate the general
autonomous golden-authority infrastructure.

## Recovery workflow

A recoverable build, test, import, tooling, or environment failure is work to
resolve within the authorized task. A phase reporting BLOCKED stops advancement
through that gate; it does not by itself end diagnosis and repair. Preserve the
original failed receipt and output, repair the cause, and rerun the same gate.
Do not ask the owner to choose an ordinary technical remedy.

1. The active parent is the hub and owns shared-workspace mutations. Record the
   failed command, exit status, phase/receipt, current commit and file ownership.
   Classify the failure as code, environment, transient external state, or an
   authority boundary. Capture relevant non-secret environment facts and module
   origins; distinguish observed facts from hypotheses. Never dump credentials.
2. Correct trivial command syntax locally. For a substantive failure, dispatch a
   bounded swarm of two or three read-only Luna agents with independent shards:
   root-cause/reproduction, regression/validation, and optionally workflow or
   authority risks. Each gets exact paths, evidence and a finite scope. Do not
   give Luna editing authority or let lanes compete over the same files.
3. Use the existing Fable review/planning lane for wisdom adjudication; escalate
   conflicting evidence or consequential uncertainty to Opus. The hub records
   which findings stand, the chosen remedy and its reasons, residual uncertainty,
   and the next validating command. A lane's venue limitation is evidence for the
   hub to collect elsewhere within existing authority, not a reason to ask the
   owner a technical question. A provider refusal is not a completed review.
4. The hub applies the smallest supported fix and a deterministic regression
   where possible. Prove the regression can fail before accepting its green
   result. Reproduce under the failing environment as well as any isolated
   fixture. Rerun the exact failed command and the unchanged phase gate; update
   only owned code, tests and instructions, then check in the validated block.
5. Allow at most three distinct repair hypotheses per incident before renewed
   wisdom adjudication. Do not repeat identical failed commands without evidence
   of a relevant state change. If no supported authorized next action exists,
   preserve the work and record owner, reason, attempted remedies and the next
   checkpoint. A finite retry budget does not manufacture owner-only authority.

Lanes can run through `tools/coordination/Invoke-Lane.ps1` with `-Lane luna`,
`-Lane fable`, or `-Lane opus`, a compact `-PromptFile`, an incident-specific
`-RunDir`, and a bounded `-TimeoutSec`. Keep `-AllowEdits` off. Native read-only
Luna subagents may supply the same independent shards when available; retain
their findings and identity in the hub's incident note. Use the repository's
normal Sol lane for the eventual PR review; wisdom advice does not replace it.

Store incident notes and original outputs under `.claude-state/` and link the
original phase receipts, lane verdicts/receipts, reproduction, chosen remedy,
validation and commit. Append subsequent decisions or create a new round; never
rewrite a failed receipt into success. Promote reusable prevention into tracked
instructions, tests, or workflow documentation in the same work block.

## Authority and evidence remain gates

Remediation must satisfy existing gates. It never authorizes skipping tests,
weakening checks, fabricating review evidence, overriding protected branches,
rewriting evidence, seizing a trusted bridge pairing, or touching excluded paths.
Respect later task-specific holds. Ask the owner only for a concrete action that
actually needs their authority, credentials, physical access, or reserved UI
interaction; explain the source of that boundary.

For the September 7 bridge runtime landing, automatic recovery supersedes the
earlier instruction to end the task at an ordinary error. The landing script
remains unchanged: no manual checkout/switch/reset in the main tree; no operations
against the separate agent-bridge checkout; Sol review bound to the current PR
head; all five required checks green and GitHub CLEAN before Merge; Settle's
lossless-master and spawn-census gates. Stop before Runtime to obtain the owner's
approval for changing Claude Desktop configuration. The owner restarts Desktop;
then rerun Preflight to verify the runtime-worktree processes.
