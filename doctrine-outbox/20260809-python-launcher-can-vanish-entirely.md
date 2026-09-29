---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-08-09 — a machine's `py` launcher can disappear entirely, silently disabling every hook or script hardcoded to call it

**Symptom (measured):** every automation registered as `py -3 <script>` (a credential-boundary gate, a widget-size gate, a doc-size Stop hook, a pinned-token check) had been failing open for an unmeasured period. `Get-Command py` returned nothing in both PowerShell and Git Bash; the launcher binary was absent from all three locations it is normally installed. Evidence of the dead state: a log file that a Stop-hook writes on every turn had stopped growing two days earlier, across many sessions, with no error anywhere.

**Cause:** the machine's Python installation had moved to an install-manager layout that ships versioned interpreter shims but **no `py` launcher at all** — a different failure mode from "wrong Python version," which is the trap people usually check for. Three interpreters coexisted: a very old one first on the machine PATH (so bare `python` silently ran the wrong major version too), a stable-path 3.x shim in the user's PATH but not in every tool's process PATH, and the versioned install itself. A hook or script that can't launch its interpreter fails at the OS level (command not found) — which for many hook harnesses is equivalent to "fail open," i.e. the security or quality gate it implements simply doesn't run, with no alert.

**Rule:** never hardcode a bare launcher name (`py`, `py -3`, bare `python`) in a hook or a settings registration, or in a command handed to another person. Resolve and pin an absolute interpreter path at setup time, and periodically re-verify it exists and is the intended version rather than assuming a working invocation from months ago still works — Python tooling layout changes (launcher removed, install-manager migration, PATH reordering) are OS/vendor changes outside the repo's control.

**Falsifier:** on any machine, run the exact command string a hook/task registration uses (not a "does Python work" proxy) and check its exit code; then check whether the artifact that command is supposed to produce (a log line, a written file) has actually advanced recently — a hook that has never fired looks exactly like one that always passes.
