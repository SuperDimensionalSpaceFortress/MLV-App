# Test scaffold, runtime helper and investigation notes (detail)

Demoted verbatim from [`CLAUDE.md`](../CLAUDE.md) under [docs/22-doc-fragmentation-policy.md](../docs/22-doc-fragmentation-policy.md). Headings are unchanged and remain the stable IDs referenced from the parent index.
## Implemented Test Scaffold
- Seed automated coverage now lives under `tests/`.
- CI entrypoint for that scaffold is `.github/workflows/tests.yml`.
- Keep the docs above synchronized with what is implemented now versus still planned next.
- On Windows, never launch `pipeline_tests.exe`, `console_tests.exe`, or other Qt-linked test executables from a bare shell, Explorer, or a direct `& .\...\*_tests.exe` command. They need the Qt and MinGW runtime DLLs on `PATH`; otherwise Windows shows modal missing-DLL popups such as `Qt6Core.dll`, `Qt6OpenGL.dll`, `Qt6Gui.dll`, `libgcc_s_seh-1.dll`, `libstdc++-6.dll`, or `libwinpthread-1.dll` before the test can print a useful error.
- Use the repo wrapper for all local Windows Qt-linked tests. It prepares PATH, deploys missing runtime DLLs beside the selected test exe, and sets the process error mode to suppress Windows loader popups:
  - `pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tools\testing\run-windows-test.ps1 -Suite console -TestArgs '--gtest_filter=PlaybackQualityAutoSampler.*'`
  - `pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tools\testing\run-windows-test.ps1 -Suite pipeline -TestArgs '--gtest_filter=DualIsoPipeline.Phase4Bv3_AggressivePreviewAllowsHqMean23PreReconX4'`
- The Windows `console_tests` and `pipeline_tests` builds deploy Qt runtime/plugins with `windeployqt`, then copy MinGW/OpenMP runtime DLLs beside the exe through `tools\testing\deploy-windows-test-runtime.ps1`. If a modal missing-DLL popup appears, treat it as a workflow regression: stop using the bare command, rerun through `tools\testing\run-windows-test.ps1`, and update this note if the wrapper itself fails.
- When running a nested `pwsh.exe -Command` from an already-running PowerShell shell, do not put `$env:PATH=...` inside outer double quotes; the outer shell expands `$env:PATH` too early and can corrupt the child PATH. Prefer the current-shell form above, or single-quote the child command string.


## Probe values and red-mutation proofs (bus trap 7cf7477; PR #705 B1)
- A test that pins a unit, scale, rounding or conversion picks a probe value that puts the bug reading and the fix reading on opposite sides of the assertion window, with the arithmetic in a comment (a 2.8 s probe that both the Int32 overload and the fix read as 3 passes under both); a red-mutation proof names the assertion that failed (file:line plus the message), not only "suite red".
- Check both with `py -3 tools/repo_hygiene/red_mutation_verdict.py verdict --output red.txt --expect <file:line>` (PASS only when that assertion failed; REFUSED for a green run, a collection or import error, or a different assertion) and `... probe --bug B --fix F --window LO HI`; guard `DG-RED-MUTATION-VERDICT` in `tools/repo_hygiene/test_doctrine_guards.py` pins the checker.
- The checker's promise is narrow on purpose: it reads text and PASSes only output it can classify with certainty. Output holding a chained-exception marker, a Captured block (pytest `--- Captured ... ---`, unittest `-b` `Stdout:`), a test identity that does not split into its own segments (a `.` `/` `\` `:` in a unittest method name, an empty segment such as `a..b`), a line number over 9 digits, a pytest short summary that is not made only of the shapes pytest prints under `-rA` (the `short-summary line` allowlist, r7: `FAILED`/`ERROR <ascii file>::<name> - <message>` on one line, `FAILED <node>` of at most 74 characters since pytest cuts the message to the terminal width but not on a `CI` runner, `PASSED <id>`, `SKIPPED [n] file:line: reason`, `XFAIL`/`XPASS <id>`, in that order, counts equal to the runner's own, and nothing after the final bar; so a run with a passing test needs `-rA`, since default reporting lists no passes to reconcile), a failing test whose exception class is not exactly a name on the fixed builtin list (it may subclass `ImportError`), or a probe number that is not an `int`, a `float` or ASCII numeric text a float can hold (`Decimal`, `Fraction`, `1e400`, `1e-400`, other scripts' digits) is answered `REFUSED COULD-NOT-CHECK (<reason>)` (exit 1). A COULD-NOT-CHECK is not a pass: re-run the mutation so the asserting test neither prints nor chains exceptions, or have a reviewer confirm the site by hand.

## Active Investigation Notes

- The content-review gate validates a CLAIMED identity from the ledger; it does NOT authenticate that actor or session. Its blocked `expectedEntryFormat` includes the parsed heading grammar as well as the canonical range and exact `Range:`/`Verdict:` lines.
- `.claude/analysis/mlv-playback-investigation.md`
- `.claude/analysis/testing-strategy.md`
- `.claude/analysis/testing-scaffold-implementation.md`


## Runtime helper
- Use `.claude-state\\scripts\\run-mlvapp.ps1` for deterministic launches:
  - prepends the correct Qt and toolchain bins,
  - sets `QT_OPENGL=desktop`,
  - optionally runs `windeployqt` in-place,
  - and launches `MLVApp.exe` with supplied arguments.
