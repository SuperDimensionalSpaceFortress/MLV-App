---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — a second Win32_Process.Create in one PowerShell process returns rc=8 with no pid, and a comma-versus-plus precedence slip silently mangled a generated launcher

**Trap 1: the second detached launch in a process fails.** The hub starts detached review lanes through `Win32_Process.Create`. The first call in a PowerShell process succeeds. A second call in the same process returned rc=8 with no process id, three times in one evening. Three retries in that process, with 5 s pauses, all failed with rc=8; a fresh PowerShell process then succeeded first time. A first create can also be lost: one review lane's folder held only an interrupted log and no receipt, so no verdict existed and it had to be relaunched. A launcher that assumes the call worked leaves a lane that never started and a ledger line saying it did.

**Rule:** one detached launch per process (or a fresh CimSession per launch), and verify the launch's own proof file (process id plus receipt) before writing any "dispatched" line. Check rc, never retry inside the same process, and fail loudly. A tick must not assume a launch happened.

**Trap 2: comma binds tighter than plus.** In PowerShell, `@(a + b, c)` parses as `@(a + (b, c))`, not `@((a + b), c)`. A script that generated a launcher file from such an array wrote one mangled line instead of separate lines, and a review-dispatch chain ran on that version before it was caught. The chain was stopped before dispatching anything, after it had already re-run a failed CI job. The fix built each line separately and parse-checked the generated file (0 errors). Parse-checking, then executing the generated launcher once with a no-op payload, is the cheap catch; reading the script is not.

**Mechanism:** the first failure is per-process state in the WMI client, so a new process works and in-process retries cannot. The second is operator precedence: the comma outranks the arithmetic operators.

**Falsifier:** in one PowerShell process call `Win32_Process.Create` twice back to back with a harmless command: expect the second return value 8 and an empty pid, again on an in-process retry, and 0 with a pid in a fresh process. Separately, `@('a' + 'b', 'c').Count` is 1 and the element is the single string `ab c`, not two elements. If the second create succeeds on a given host, the trap does not reproduce there; record its OS build.
