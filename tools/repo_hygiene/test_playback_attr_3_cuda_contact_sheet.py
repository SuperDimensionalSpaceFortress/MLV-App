"""CUDA-PLAYBACK-CONTACT-SHEET-1: behaviour tests for the attribution job generator's
opt-in -ContactSheet switch (playback-attr-3-cuda-job.ps1).

Kept as its own small file, separate from the ~4000-line
test_playback_attr_3_cuda_behaviour.py, on purpose: UM-CUDA-BENCH-VENUE-1 is concurrently
adding a venue parameter to the same generator, and a small self-contained test file is far
less likely to collide with that work than an insertion into the shared suite.

WHAT IS EXERCISED. This generates real jobs with the real generator against the same
throwaway fixture repo `_make_fixture_repo` builds for the existing suite (imported from
there rather than duplicated) and asserts on the EMITTED JOB TEXT: the switch is off by
default, an off run's smoke invocation is byte-identical to before this card (the "switch
off -> unchanged job text" requirement), an on run's invocation carries
--contact-sheet-dir/--contact-sheet-frames, and both emitted jobs parse as valid
PowerShell. It does not run either emitted job (no CUDA build, no Bachelor here --
round-1 scope).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import (  # noqa: E402
    ATTRIBUTION_GENERATOR,
    MODULE,
    OWNER_FOOTAGE_MODULE,
    PWSH,
    _git_run,
    _long_path,
    _make_fixture_repo,
    _run_pwsh_file,
    normalize_pwsh_message_text,
    requires_git,
    requires_pwsh,
    tempfile,
)
# The composer's own test helper needs numpy + Pillow, which the hosted CI images do not install (they install
# hash-pinned requirements that pin neither yet; see the tools/profiling --ignore note in factory-bridge.yml).
# Only the one real-interpreter compose test needs it (already skipped via _HAS_REAL_PYTHON_DEPS), so it is imported
# lazily inside that test instead of failing the whole module at import time.

_HAS_REAL_PYTHON_DEPS = (
    importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None
)


def _make_fixture_repo_pre_contact_sheet_card(path: Path) -> list[str]:
    """Like _make_fixture_repo, but tools/profiling/make-contact-sheet.py does not exist at
    the FIRST commit -- a stand-in for a real pre-CUDA-PLAYBACK-CONTACT-SHEET-1 commit (e.g.
    c2f9d377). B4 (r1c BLOCKER fix): a default-off (-ContactSheet not passed) generation
    against such a commit must succeed -- the composer blob must never be resolved when the
    switch is off, regardless of whether it would even resolve."""
    path.mkdir(parents=True, exist_ok=True)
    _git_run(["init", "-q", "-b", "main"], path)
    _git_run(["config", "commit.gpgsign", "false"], path)
    _git_run(["config", "user.email", "lane@example.invalid"], path)
    _git_run(["config", "user.name", "attr3 behaviour fixture"], path)
    (path / "src" / "mlv" / "llrawproc").mkdir(parents=True)
    (path / "tools" / "gpu" / "backend").mkdir(parents=True)
    (path / "tools" / "profiling").mkdir(parents=True)
    (path / "tools" / "profiling" / "run-release-gui-smoke.ps1").write_text(
        "# fixture stand-in for run-release-gui-smoke.ps1\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
        "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
        ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n",
        encoding="utf-8",
    )
    (path / "tools" / "profiling" / "gui-smoke-screenshot-provenance.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "provenance-stamp.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-process-boundary.psm1").write_text(
        "# fixture stand-in sibling (imported directly by the runner).\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-color-artifact-scan.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-gpu-texture-route-validation.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    # Deliberately OMITTED from the first commit: tools/profiling/make-contact-sheet.py.
    shas = []
    for index, text in enumerate(("first", "second")):
        (path / "src" / "mlv" / "llrawproc" / "llrawproc.c").write_text(
            f"/* fixture revision {text} */\n", encoding="utf-8"
        )
        if index == 1:
            (path / "tools" / "profiling" / "make-contact-sheet.py").write_text(
                "# fixture stand-in for make-contact-sheet.py, added at the second commit\n",
                encoding="utf-8",
            )
        _git_run(["add", "-A"], path)
        _git_run(["commit", "-q", "-m", f"fixture {index}"], path)
        shas.append(_git_run(["rev-parse", "HEAD"], path))
    return shas

# The base smoke invocation's own text, exactly as playback-attr-3-cuda-job.ps1 emits it when
# -ContactSheet is not passed. A change to this literal is a real change to what every
# non-contact-sheet leg runs, so this is pinned rather than derived.
_BASE_CMD_TAIL = (
    "-RequireLookAssist:`$false -Scope none -FrameTelemetry -PreserveExperimentalEnvironment "
    "-ExtraEnvironment @($envList)\"\n"
)


@requires_pwsh
@requires_git
@unittest.skipIf(os.name != "nt", "the ATTR-3 host jobs are Windows-only (drive-letter path parameters); Windows CI runs this")
class ContactSheetSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        if PWSH is None:  # pragma: no cover - guarded by requires_pwsh too
            self.skipTest("pwsh is not on PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-contact-sheet-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)

    def _generate(self, out_name: str, *extra_args: str, sha: str | None = None) -> Path:
        out_file = self.tmp / out_name
        script = self.tmp / f"generate-{out_name}.ps1"
        args = " ".join(extra_args)
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{sha or self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' "
            f"-RepoRoot '{self.repo}' {args}\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"generator failed: {proc.stdout}\n{proc.stderr}")
        self.assertTrue(out_file.is_file(), "generator reported success but wrote no job file")
        return out_file

    def _assert_valid_powershell(self, job_file: Path) -> None:
        probe = self.tmp / f"probe-{job_file.name}.ps1"
        probe.write_text(
            "$errors = $null\n"
            f"[void][System.Management.Automation.PSParser]::Tokenize(\n"
            f"    (Get-Content -Raw -LiteralPath '{job_file}'), [ref]$errors)\n"
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.Message } }\n"
            "else { Write-Output 'NO_PARSE_ERRORS' }\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(probe)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("NO_PARSE_ERRORS", proc.stdout, proc.stdout)

    def test_switch_off_by_default_bakes_false_and_leaves_smoke_invocation_unchanged(self) -> None:
        job_file = self._generate("off.job.ps1")
        text = job_file.read_text(encoding="utf-8")

        self.assertIn("$ContactSheetEnabled = $false", text)
        self.assertIn("$ContactSheetFrameCount = 6", text)  # generator's own default

        # The base $cmd assignment's own tail must be byte-identical to what every
        # non-contact-sheet leg ran before this card: no AdditionalArgs baked into it.
        self.assertIn(_BASE_CMD_TAIL, text)
        base_cmd_line = next(line for line in text.splitlines() if line.startswith('$cmd = "& '))
        self.assertNotIn("AdditionalArgs", base_cmd_line)

        self._assert_valid_powershell(job_file)

    def test_switch_on_bakes_true_frame_count_and_additional_args(self) -> None:
        job_file = self._generate("on.job.ps1", "-ContactSheet", "-ContactSheetFrames", "8")
        text = job_file.read_text(encoding="utf-8")

        self.assertIn("$ContactSheetEnabled = $true", text)
        self.assertIn("$ContactSheetFrameCount = 8", text)

        # The base $cmd line itself is STILL unchanged (appended to at runtime, not baked) --
        # only the runtime conditional block differs between an off and on generation.
        self.assertIn(_BASE_CMD_TAIL, text)

        # The runtime conditional block that appends -AdditionalArgs must reference both app
        # flags and the frame-count variable, and must run before $cmd is used to launch smoke.
        conditional_match = re.search(
            r"if \(\$ContactSheetEnabled\) \{.*?\$cmd = \"\$cmd -AdditionalArgs \$contactSheetAdditionalArgs\"\n\}",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(conditional_match, "expected the contact-sheet $cmd append block")
        block = conditional_match.group(0)
        self.assertIn("--contact-sheet-dir", block)
        self.assertIn("--contact-sheet-frames", block)
        self.assertIn("$ContactSheetFrameCount", block)

        self._assert_valid_powershell(job_file)

    def test_publish_block_runs_before_the_artifact_index_is_built(self) -> None:
        job_file = self._generate("on-publish-order.job.ps1", "-ContactSheet")
        text = job_file.read_text(encoding="utf-8")

        # CUDA-PLAYBACK-CONTACT-SHEET-2: the raw-copy logic moved into
        # Publish-AttrCudaContactSheetRawCaptures (also called from both early-refusal exits --
        # see the NOTE-fix tests below), so the main-flow publish step now reads as a call to it.
        # Round 2: the function itself now lives in AttrCudaArtifacts.psm1 and is spliced into
        # the emitted job at the __EMBEDDED_FUNCTIONS__ token (test_every_called_attrcuda_
        # command_is_defined_in_the_real_embedded_text requires every -AttrCuda-named call in
        # the template to come from that splice), so its declaration in the emitted text is the
        # module's own param()-block style, not the old inline-parameter one.
        function_def_pos = text.index("function Publish-AttrCudaContactSheetRawCaptures {")
        publish_call_pos = text.index(
            "$contactSheetPubDir = Publish-AttrCudaContactSheetRawCaptures "
            "-Enabled $ContactSheetEnabled -SourceDir $contactSheetDir -PubRoot $Pub"
        )
        index_pos = text.index(
            "$files = Get-ChildItem -LiteralPath $Pub -Recurse -File"
        )
        self.assertLess(
            function_def_pos,
            publish_call_pos,
            "the helper must be defined before its first call site (no forward references "
            "in this generated script)",
        )
        self.assertLess(
            publish_call_pos,
            index_pos,
            "contact-sheet frames must be published before the artifact index is built, "
            "or they will not be indexed",
        )

    def test_compose_step_passes_host_gpu_scale_to_the_composer(self) -> None:
        # BLOCKER (sol pre-review #2): a job-composed sheet with no --host/--gpu/--scale
        # renders host=unknown gpu=unknown scale=unknown, defeating the owner's side-by-side
        # host/build/look comparison. Static check on the emitted job's own compose-step
        # text -- the runtime leg is proven separately against a real composer run.
        job_file = self._generate("host-gpu-scale.job.ps1", "-ContactSheet")
        text = job_file.read_text(encoding="utf-8")
        snippet = self._extract_compose_step_snippet(text)
        self.assertIn("'--host', $contactSheetHostLabel", snippet)
        self.assertIn("'--gpu', $contactSheetGpuLabel", snippet)
        self.assertIn("'--scale', $contactSheetScaleLabel", snippet)
        self.assertIn("$env:COMPUTERNAME", snippet)
        self.assertIn("$verdict.cudaBackendDescription", snippet)
        self.assertIn("$verdict.scale", snippet)
        # BLOCKER fix (r1d): every composeArgs element must be quoted before Start-Process --
        # a bare GPU description containing spaces would otherwise silently split across argv
        # (the same class of bug as the r1c -c-quoting blocker).
        self.assertIn("ConvertTo-AttrCudaQuotedProcessArgument", snippet)

    def _extract_cmd_construction_snippet(self, text: str) -> str:
        """The real emitted-job lines from the ConvertTo-PsSingleQuoted helper through the end
        of the contact-sheet $cmd-append block -- executed verbatim below with stub inputs, so
        this proves RUNTIME behaviour (what $cmd actually becomes), not just static text shape.
        """
        start = text.index("function ConvertTo-PsSingleQuoted")
        end_marker = '$cmd = "$cmd -AdditionalArgs $contactSheetAdditionalArgs"\n}'
        end = text.index(end_marker) + len(end_marker)
        return text[start:end]

    def _eval_cmd_for(self, text: str, enabled: bool, frame_count: int) -> str:
        snippet = self._extract_cmd_construction_snippet(text)
        work_dir = self.tmp / f"work-eval-{enabled}-{frame_count}"
        probe = self.tmp / f"probe-cmd-{enabled}-{frame_count}.ps1"
        probe.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$Work = '{work_dir}'\n"
            "$smoke = 'smoke.ps1'\n"
            "$exePath = 'exe.exe'\n"
            "$clipPath = 'clip.mlv'\n"
            "$resultPath = 'result.json'\n"
            "$envList = \"'A=1','B=2'\"\n"
            f"$ContactSheetEnabled = ${'true' if enabled else 'false'}\n"
            f"$ContactSheetFrameCount = {frame_count}\n"
            + snippet
            + "\nWrite-Output \"CMD=$cmd\"\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(probe)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        line = next(l for l in proc.stdout.splitlines() if l.startswith("CMD="))
        return line[len("CMD="):]

    def test_runtime_cmd_is_unchanged_when_disabled_and_carries_args_when_enabled(self) -> None:
        # One generation covers both: the runtime block is present in the emitted text
        # regardless of the switch (only its EXECUTION is conditional -- see the -ContactSheet
        # param's own comment), so evaluating it twice with different stub inputs exercises the
        # real branch each caller actually takes at job-run time on Bachelor.
        job_file = self._generate("runtime-eval.job.ps1", "-ContactSheet", "-ContactSheetFrames", "8")
        text = job_file.read_text(encoding="utf-8")

        cmd_off = self._eval_cmd_for(text, enabled=False, frame_count=6)
        cmd_on = self._eval_cmd_for(text, enabled=True, frame_count=8)

        self.assertNotIn("AdditionalArgs", cmd_off)
        self.assertNotIn("contact-sheet", cmd_off)
        self.assertIn("-AdditionalArgs", cmd_on)
        self.assertIn("--contact-sheet-dir", cmd_on)
        self.assertIn("--contact-sheet-frames", cmd_on)
        self.assertIn("'8'", cmd_on)
        # The "off" prefix of the "on" command must still match the off command exactly --
        # AdditionalArgs is strictly appended, never interleaved or substituted in.
        self.assertTrue(cmd_on.startswith(cmd_off))

    def _extract_presentmon_helper_functions(self, text: str) -> str:
        """Start/Wait/Stop-PresentMonCapture -- the compose step's HARDENING fix (r1d) reuses
        Stop-PresentMonCapture's own already-tested Kill()+bounded-WaitForExit()+
        confirmedExited pattern, so a standalone probe of the compose step needs it in scope
        too, exactly as it is in the real job (defined earlier in the same script)."""
        start = text.index("function Start-PresentMonCapture(")
        end = text.index("\nfunction Get-FrameRows(", start)
        return text[start:end]

    def _extract_full_compose_block_snippet(self, text: str) -> str:
        """Like _extract_compose_step_snippet, but through the compose-warnings.txt publish
        added in r1d -- the sibling to compose-status.txt that records a deps-probe or
        composer child that was still alive after a timed-out Kill() attempt."""
        start_marker = (
            "# CUDA-PLAYBACK-CONTACT-SHEET-1 r1b: compose the raw captures "
            "into one labelled sheet +"
        )
        end_marker = (
            "[void](Publish-AttrCudaText -Path (Join-Path $Pub "
            "'contact-sheet\\compose-warnings.txt') -Value "
            "($contactSheetOrphanNotes -join \"`n\"))\n    }"
        )
        start = text.index(start_marker)
        end = text.index(end_marker, start) + len(end_marker)
        return text[start:end]

    def test_deps_probe_records_a_warning_when_the_child_survives_kill(self) -> None:
        # BLOCKER-adjacent HARDENING (sol pre-review #2): a timed-out deps-probe child that
        # survives Kill() must be recorded, never silently left running while the job moves on
        # regardless. Both python.exe and py.exe candidates are forced to "time out and never
        # actually exit" here, via a fake process object -- no real 20s wait, no real orphan
        # process -- so this proves the WIRING (Stop-PresentMonCapture is called, its
        # not-confirmed-exited outcome reaches compose-warnings.txt), not the underlying
        # Kill()/WaitForExit() primitives, which WaitPresentMonCaptureTimeoutWaitsAfterKillTests
        # already proves against a real process.
        job_file = self._generate("deps-probe-orphan.job.ps1", "-ContactSheet")
        text = job_file.read_text(encoding="utf-8")
        snippet = self._extract_full_compose_block_snippet(text)
        presentmon_functions = self._extract_presentmon_helper_functions(text)

        work_dir = self.tmp / "orphan-work"
        pub_dir = self.tmp / "orphan-pub"
        raw_dir = pub_dir / "contact-sheet" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "frame-00.png").write_bytes(b"not a real png, never read by this leg")

        probe = self.tmp / "probe-deps-orphan.ps1"
        probe.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            + presentmon_functions + "\n"
            f"$Work = '{work_dir}'\n"
            f"$Pub = '{pub_dir}'\n"
            f"$contactSheetPubDir = '{raw_dir}'\n"
            "$ClipId = 'tiny_dual_iso'\n"
            "$SourceCommit = '" + self.shas[1] + "'\n"
            "$FixtureRehearsal = $true\n"
            "New-Item -ItemType Directory -Path $Work -Force | Out-Null\n"
            # A fake process: WaitForExit always reports "not yet exited" and Kill() is a
            # no-op that never actually terminates it -- deterministic, no real 20s wait.
            "function Start-Process {\n"
            "    param([string]$FilePath, [string[]]$ArgumentList, [switch]$PassThru, [string]$WindowStyle)\n"
            "    $fake = [pscustomobject]@{ ExitCode = 1; HasExited = $false }\n"
            "    $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return $false }\n"
            "    $fake | Add-Member -MemberType ScriptMethod -Name Kill -Value { }\n"
            "    return $fake\n"
            "}\n"
            + snippet
            + "\nWrite-Output ('MARKER=' + $contactSheetComposeMarker)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(probe)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        marker_line = next(l for l in proc.stdout.splitlines() if l.startswith("MARKER="))
        self.assertIn(
            "no Python 3 interpreter with Pillow+numpy was found on this venue", marker_line
        )

        warnings_file = pub_dir / "contact-sheet" / "compose-warnings.txt"
        self.assertTrue(warnings_file.is_file(), "expected a compose-warnings.txt with the orphan note")
        warnings_text = warnings_file.read_text(encoding="utf-8")
        self.assertIn("python.exe", warnings_text)
        self.assertIn("py.exe", warnings_text)
        self.assertIn("did not exit after Kill()", warnings_text)

    def _extract_compose_step_snippet(self, text: str) -> str:
        """The real emitted job's contact-sheet compose step, run verbatim below (with the
        module's real functions imported, real embedded composer bytes, but a controlled
        $env:PATH) so this proves RUNTIME behaviour, not just that the text is present."""
        start_marker = (
            "# CUDA-PLAYBACK-CONTACT-SHEET-1 r1b: compose the raw captures "
            "into one labelled sheet +"
        )
        end_marker = (
            "[void](Publish-AttrCudaText -Path (Join-Path $Pub "
            "'contact-sheet\\compose-status.txt') -Value $contactSheetComposeMarker)\n"
            "    }"
        )
        start = text.index(start_marker)
        end = text.index(end_marker) + len(end_marker)
        return text[start:end]

    def test_compose_step_records_a_typed_marker_when_no_python_interpreter_is_on_path(self) -> None:
        # Forces the "unavailable" leg the hub asked for: with no python.exe/py.exe reachable,
        # both probe attempts in the real emitted compose step must fail closed and the step
        # must degrade to a non-fatal, typed compose-status.txt marker -- never throw, never
        # silently produce nothing.
        job_file = self._generate("compose-unavailable.job.ps1", "-ContactSheet")
        text = job_file.read_text(encoding="utf-8")
        snippet = self._extract_compose_step_snippet(text)

        work_dir = self.tmp / "compose-work"
        pub_dir = self.tmp / "compose-pub"
        raw_dir = pub_dir / "contact-sheet" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "frame-00.png").write_bytes(b"not a real png, never read by this leg")

        probe = self.tmp / "probe-compose-unavailable.ps1"
        probe.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"$Work = '{work_dir}'\n"
            f"$Pub = '{pub_dir}'\n"
            f"$contactSheetPubDir = '{raw_dir}'\n"
            "$ClipId = 'tiny_dual_iso'\n"
            "$SourceCommit = '" + self.shas[1] + "'\n"
            "$FixtureRehearsal = $true\n"
            "New-Item -ItemType Directory -Path $Work -Force | Out-Null\n"
            # CUDA-PLAYBACK-CONTACT-SHEET-1 r1c: a scrubbed $env:PATH alone does not reliably
            # hide python.exe/py.exe from Start-Process on every host -- Start-Process's own
            # executable resolution does not strictly follow the current process's $env:PATH
            # (confirmed on this host: python.exe still launches successfully even after this
            # override), which is exactly the class of Start-Process resolution surprise this
            # round's blocker fix (the -c quoting bug) was also about. Shadow the cmdlet
            # itself instead, deterministically simulating a venue where BOTH candidates fail
            # to launch, host-independent.
            "function Start-Process {\n"
            "    param([string]$FilePath, [string[]]$ArgumentList, [switch]$PassThru, [string]$WindowStyle)\n"
            "    throw [System.Management.Automation.CommandNotFoundException]::new(\"$FilePath not found (test stub)\")\n"
            "}\n"
            + snippet
            + "\nWrite-Output ('MARKER=' + $contactSheetComposeMarker)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(probe)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        marker_line = next(l for l in proc.stdout.splitlines() if l.startswith("MARKER="))
        self.assertIn("CONTACT_SHEET_COMPOSE_UNAVAILABLE", marker_line)
        self.assertIn(
            "no Python 3 interpreter with Pillow+numpy was found on this venue", marker_line
        )
        status_file = pub_dir / "contact-sheet" / "compose-status.txt"
        self.assertTrue(status_file.is_file(), "expected a compose-status.txt marker file")
        self.assertIn("CONTACT_SHEET_COMPOSE_UNAVAILABLE", status_file.read_text(encoding="utf-8"))
        # Never attempted a compose with no interpreter: no sheet/stats output.
        self.assertFalse((pub_dir / "contact-sheet" / "sheet.png").exists())
        self.assertFalse((pub_dir / "contact-sheet" / "stats.json").exists())

    @unittest.skipUnless(
        _HAS_REAL_PYTHON_DEPS, "this host has no Pillow/numpy to prove the SUCCESS leg against"
    )
    def test_compose_step_actually_composes_a_real_sheet_and_stats_with_a_real_interpreter(
        self,
    ) -> None:
        # B3 (r1c): the disclosed-open remedy from r1b -- a real tiny PNG+JSON fixture pair,
        # composed by the REAL emitted snippet against a REAL Python 3 + Pillow + numpy on
        # this host, proving the deps probe's positive leg (never exercised by the
        # PATH-scrubbed "unavailable" test alone) actually finds a capable interpreter and
        # the composer actually runs end to end.
        # _make_fixture_repo's own make-contact-sheet.py is a one-line stand-in comment (kept
        # that way so `git archive` stays instant for every OTHER test in this suite) -- swap
        # in the REAL composer script's bytes for this one commit, so the compose step this
        # test proves actually runs make-contact-sheet.py's real logic, not a no-op stub.
        real_composer_bytes = (
            ROOT / "tools" / "profiling" / "make-contact-sheet.py"
        ).read_bytes()
        (self.repo / "tools" / "profiling" / "make-contact-sheet.py").write_bytes(
            real_composer_bytes
        )
        _git_run(["add", "-A"], self.repo)
        _git_run(["commit", "-q", "-m", "real composer for the r1c positive test"], self.repo)
        real_composer_sha = _git_run(["rev-parse", "HEAD"], self.repo)

        job_file = self._generate("compose-available.job.ps1", "-ContactSheet", sha=real_composer_sha)
        text = job_file.read_text(encoding="utf-8")
        snippet = self._extract_compose_step_snippet(text)
        # The snippet references $ContactSheetComposerPyBase64/$ContactSheetComposerSha256 --
        # baked into the real job by the generator, exactly as -AdditionalArgs etc. are; pull
        # the REAL emitted values out of the job text rather than recomputing them, so this
        # test proves the real generator's own bytes compose correctly, not a stand-in.
        base64_line = next(
            l for l in text.splitlines() if l.startswith("$ContactSheetComposerPyBase64 =")
        )
        sha256_line = next(
            l for l in text.splitlines() if l.startswith("$ContactSheetComposerSha256 =")
        )

        work_dir = self.tmp / "compose-work-real"
        pub_dir = self.tmp / "compose-pub-real"
        raw_dir = pub_dir / "contact-sheet" / "raw"
        raw_dir.mkdir(parents=True)
        for i in range(4):
            level = 40 + i * 30
            from tools.profiling.test_make_contact_sheet import _write_frame  # noqa: E402 -- lazy: needs numpy/Pillow
            _write_frame(str(raw_dir), i, (level, level, level))

        probe = self.tmp / "probe-compose-available.ps1"
        probe.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"$Work = '{work_dir}'\n"
            f"$Pub = '{pub_dir}'\n"
            f"$contactSheetPubDir = '{raw_dir}'\n"
            "$ClipId = 'tiny_dual_iso'\n"
            "$SourceCommit = '" + real_composer_sha + "'\n"
            "$FixtureRehearsal = $true\n"
            f"{base64_line}\n"
            f"{sha256_line}\n"
            # CUDA-PLAYBACK-CONTACT-SHEET-1 r1d: the compose step reads GPU/scale off
            # $verdict (the eligibility-line parse the job does earlier at runtime) --
            # stubbed here with known values so this test can assert they reach stats.json.
            "$verdict = [pscustomobject]@{ "
            "cudaBackendDescription = 'CUDA / NVIDIA GeForce RTX 4090'; scale = '4' }\n"
            "New-Item -ItemType Directory -Path $Work -Force | Out-Null\n"
            # Deliberately does NOT scrub $env:PATH: this leg proves the probe finds a real,
            # capable interpreter when one is genuinely present.
            + snippet
            + "\nWrite-Output ('MARKER=' + $contactSheetComposeMarker)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(probe)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("CONTACT_SHEET_COMPOSE_UNAVAILABLE", proc.stdout)

        sheet_out = pub_dir / "contact-sheet" / "sheet.png"
        stats_out = pub_dir / "contact-sheet" / "stats.json"
        self.assertTrue(sheet_out.is_file(), "expected a real composed sheet.png")
        self.assertTrue(stats_out.is_file(), "expected a real composed stats.json")
        stats = json.loads(stats_out.read_text(encoding="utf-8"))
        self.assertEqual(stats["schema"], "contact-sheet-stats.v1")
        self.assertEqual(stats["tile_count"], 4)
        self.assertEqual(len(stats["tiles"]), 4)
        # H3: the composed sidecar carries no absolute local path.
        self.assertNotIn(str(self.tmp), stats["sheet_path"])
        self.assertNotIn(str(self.tmp), stats["frames_dir"])
        # BLOCKER fix (r1d): host/GPU/scale must reach stats.json, not render as "unknown".
        self.assertEqual(stats["host"], os.environ["COMPUTERNAME"])
        self.assertEqual(stats["gpu"], "CUDA / NVIDIA GeForce RTX 4090")
        self.assertEqual(stats["scale"], "4")
        status_file = pub_dir / "contact-sheet" / "compose-status.txt"
        self.assertFalse(
            status_file.exists(), "success leg must not also write the unavailable marker"
        )

    def test_frame_count_out_of_range_is_refused_at_parameter_binding(self) -> None:
        out_file = self.tmp / "refused.job.ps1"
        script = self.tmp / "generate-refused.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' "
            f"-RepoRoot '{self.repo}' -ContactSheet -ContactSheetFrames 0\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(out_file.exists())


@requires_pwsh
class MeasuredSmokeSessionIdTests(unittest.TestCase):
    """HARDENING (sol pre-review #2, r1d): Get-MeasuredSmokeSessionId must bind to the app's
    explicit playback_smoke.measured_session marker when the log carries one, rather than
    positionally assuming the first playback_smoke.summary line is the measured session --
    and must still fall back to that old heuristic against a log from a build that predates
    the marker."""

    def _function_text(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("function Get-MeasuredSmokeSessionId(")
        end = text.index("\nfunction Get-FrameRows(", start)
        return text[start:end]

    def _run(self, raw_log: str) -> subprocess.CompletedProcess:
        tmp = tempfile.TemporaryDirectory(prefix="measured-session-id-")
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "probe.ps1"
        log_literal = "'" + raw_log.replace("'", "''") + "'"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            + self._function_text() + "\n"
            f"Write-Output ('ID=' + (Get-MeasuredSmokeSessionId {log_literal}))\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_binds_to_the_explicit_marker_even_when_it_disagrees_with_the_first_summary(self) -> None:
        # A synthetic case an alternate GUI-smoke mode could produce: an earlier (warmup)
        # session's summary precedes the real measured one. The marker, not position, wins.
        log = (
            "playback_smoke.summary session=1 reason=play-restart elapsed_ms=10\n"
            "playback_smoke.measured_session id=2\n"
            "playback_smoke.summary session=2 reason=play-stop elapsed_ms=1000\n"
        )
        proc = self._run(log)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ID=2", proc.stdout)

    def test_falls_back_to_the_first_summary_line_when_no_marker_is_present(self) -> None:
        # A log from a build that predates the marker (or any other reason it is absent) must
        # still resolve, via the old first-summary heuristic -- no regression for such a build.
        log = (
            "playback_smoke.summary session=7 reason=play-stop elapsed_ms=1000\n"
            "playback_smoke.summary session=8 reason=play-restart elapsed_ms=10\n"
        )
        proc = self._run(log)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ID=7", proc.stdout)

    def test_throws_when_neither_marker_nor_summary_is_present(self) -> None:
        proc = self._run("nothing relevant here\n")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            "no playback_smoke.summary line found",
            normalize_pwsh_message_text(proc.stdout + proc.stderr),
        )


@requires_pwsh
@requires_git
@unittest.skipIf(os.name != "nt", "the ATTR-3 host jobs are Windows-only (drive-letter path parameters); Windows CI runs this")
class ContactSheetDefaultOffPreCardCommitTests(unittest.TestCase):
    """B4 (r1c BLOCKER fix): a default-off generation must succeed against a commit that
    predates tools/profiling/make-contact-sheet.py entirely -- the composer blob must never
    be resolved unless -ContactSheet is actually passed."""

    def setUp(self) -> None:
        if PWSH is None:  # pragma: no cover - guarded by requires_pwsh too
            self.skipTest("pwsh is not on PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-contact-sheet-precard-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo_pre_contact_sheet_card(self.repo)

    def test_default_off_generation_succeeds_against_a_commit_with_no_composer_file(self) -> None:
        # self.shas[0] is the PRE-CARD commit: tools/profiling/make-contact-sheet.py does not
        # exist there at all. -ContactSheet is not passed, matching a real pre-card leg.
        out_file = self.tmp / "off-pre-card.job.ps1"
        script = self.tmp / "generate-off-pre-card.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[0]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' "
            f"-RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"generator failed: {proc.stdout}\n{proc.stderr}")
        self.assertTrue(out_file.is_file(), "generator reported success but wrote no job file")
        text = out_file.read_text(encoding="utf-8")
        self.assertIn("$ContactSheetEnabled = $false", text)
        self.assertIn("$ContactSheetComposerPyBase64 = ''", text)

    def test_on_generation_against_the_pre_card_commit_still_refuses(self) -> None:
        # The converse: -ContactSheet against the SAME pre-card commit must still fail
        # (there is genuinely nothing to embed), proving the off-run's success above is not
        # simply because blob resolution failures are being swallowed somewhere.
        out_file = self.tmp / "on-pre-card.job.ps1"
        script = self.tmp / "generate-on-pre-card.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[0]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' "
            f"-RepoRoot '{self.repo}' -ContactSheet\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(out_file.exists())


@requires_pwsh
class ContactSheetRawCapturesOnRefusalTests(unittest.TestCase):
    """NOTE fix (fable, CUDA-PLAYBACK-CONTACT-SHEET-2): raw --contact-sheet-dir captures must
    still be published under $Pub/contact-sheet/raw even when the leg refuses at the
    eligibility gate (BACKEND_NOT_AVAILABLE) or the GPU-frame gate (GPU_RECON_FRAMES_ZERO),
    both of which exit before the main publish step used to run this copy at all."""

    def test_copies_every_file_and_returns_the_raw_dir_when_enabled_and_populated(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="contact-sheet-raw-publish-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source_dir = root / "capture"
        source_dir.mkdir()
        (source_dir / "frame-00.png").write_bytes(b"\x89PNG-fixture")
        (source_dir / "frame-00.json").write_text('{"index": 0}', encoding="utf-8")
        pub_root = root / "pub"
        pub_root.mkdir()

        script = root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$result = Publish-AttrCudaContactSheetRawCaptures -Enabled $true "
            f"-SourceDir '{source_dir}' -PubRoot '{pub_root}'\n"
            "Write-Output \"RESULT=$result\"\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        raw_dir = pub_root / "contact-sheet" / "raw"
        self.assertTrue((raw_dir / "frame-00.png").is_file())
        self.assertTrue((raw_dir / "frame-00.json").is_file())
        self.assertEqual(
            (raw_dir / "frame-00.json").read_text(encoding="utf-8"), '{"index": 0}'
        )
        self.assertIn(f"RESULT={raw_dir}", proc.stdout)

    def test_is_a_no_op_when_disabled_or_the_source_dir_does_not_exist(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="contact-sheet-raw-publish-noop-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pub_root = root / "pub"
        pub_root.mkdir()
        missing_dir = root / "does-not-exist"

        script = root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$disabled = Publish-AttrCudaContactSheetRawCaptures -Enabled $false "
            f"-SourceDir '{missing_dir}' -PubRoot '{pub_root}'\n"
            f"$missingSource = Publish-AttrCudaContactSheetRawCaptures -Enabled $true "
            f"-SourceDir '{missing_dir}' -PubRoot '{pub_root}'\n"
            "Write-Output \"DISABLED_IS_NULL=$($null -eq $disabled)\"\n"
            "Write-Output \"MISSING_SOURCE_IS_NULL=$($null -eq $missingSource)\"\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISABLED_IS_NULL=True", proc.stdout)
        self.assertIn("MISSING_SOURCE_IS_NULL=True", proc.stdout)
        # Neither call may have created the contact-sheet dir under $Pub.
        self.assertFalse((pub_root / "contact-sheet").exists())

    def test_both_early_refusal_exits_publish_raw_captures_before_saving_summary_and_exiting(
        self,
    ) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")

        backend_refusal_at = text.index("if (-not $verdict.admitted) {")
        backend_publish_at = text.index(
            "[void](Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled "
            "-SourceDir $contactSheetDir -PubRoot $Pub)",
            backend_refusal_at,
        )
        backend_save_at = text.index("Save-Json $refusal (Join-Path $Pub 'summary.json')", backend_refusal_at)
        backend_exit_at = text.index("exit $verdict.exitCode", backend_refusal_at)
        self.assertLess(backend_refusal_at, backend_publish_at)
        self.assertLess(backend_publish_at, backend_save_at)
        self.assertLess(backend_save_at, backend_exit_at)

        gpu_zero_refusal_at = text.index("if ($gpuFramesTotal -le 0) {")
        gpu_zero_publish_at = text.index(
            "[void](Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled "
            "-SourceDir $contactSheetDir -PubRoot $Pub)",
            gpu_zero_refusal_at,
        )
        gpu_zero_save_at = text.index(
            "Save-Json $fallback (Join-Path $Pub 'summary.json')", gpu_zero_refusal_at
        )
        gpu_zero_exit_at = text.index("exit 13", gpu_zero_refusal_at)
        self.assertLess(gpu_zero_refusal_at, gpu_zero_publish_at)
        self.assertLess(gpu_zero_publish_at, gpu_zero_save_at)
        self.assertLess(gpu_zero_save_at, gpu_zero_exit_at)

        # The two refusal branches' publish calls are genuinely distinct call sites, not one
        # shared position both indices above coincidentally matched.
        self.assertNotEqual(backend_publish_at, gpu_zero_publish_at)


@requires_pwsh
class ConvertToAttrCudaQuotedProcessArgumentBackslashTests(unittest.TestCase):
    """HARDENING fix (fable NOTE, CUDA-PLAYBACK-CONTACT-SHEET-2): ConvertTo-AttrCudaQuotedProcessArgument
    must correctly escape a value containing a backslash immediately before a double quote (and
    a value ending in a bare backslash, which sits immediately before the closing quote this
    function adds) per the CommandLineToArgvW/MSVCRT argv-quoting convention -- not just
    values that happen to contain neither."""

    @staticmethod
    def _argv_decode_one_quoted_argument(quoted: str) -> str:
        """An independent reference decoder for ONE argument produced by
        ConvertTo-AttrCudaQuotedProcessArgument, implementing the standard Microsoft C
        runtime / CommandLineToArgvW parsing rules directly from their public description --
        written independently of the ps1 implementation under test, so a shared bug in both
        would have to be a coincidence, not a shared assumption.

        Deliberately does NOT strip the closing quote before scanning: a run of backslashes
        immediately before THAT quote must be halved exactly like a run before an embedded
        one (this is precisely the case the backslash-before-quote hardening fixes), so the
        closing quote has to stay visible to the same backslash-run lookahead the rest of the
        scan uses, rather than being cut off first.
        """
        assert quoted[0] == '"' and quoted[-1] == '"', quoted
        out: list[str] = []
        i = 1  # skip the opening quote
        n = len(quoted)
        while i < n - 1:
            ch = quoted[i]
            if ch == "\\":
                j = i
                while j < n and quoted[j] == "\\":
                    j += 1
                run = j - i
                if j < n and quoted[j] == '"':
                    # j == n - 1 means this run sits right before the CLOSING quote; j < n - 1
                    # means it precedes an embedded, escaped quote. Both were doubled (plus one
                    # more backslash for an embedded quote) by the encoder, so both halve back
                    # the same way here.
                    out.append("\\" * (run // 2))
                    if run % 2 == 1:
                        out.append('"')
                        i = j + 1
                    else:
                        i = j
                else:
                    out.append("\\" * run)
                    i = j
            else:
                # A bare, unescaped quote here would mean the encoder emitted an embedded
                # quote without a preceding backslash -- ConvertTo-AttrCudaQuotedProcessArgument
                # always precedes an embedded quote with at least one backslash (see its own
                # `\"` append), so this must never be reached for real encoder output.
                assert ch != '"', f"unescaped bare quote in quoted body: {quoted!r}"
                out.append(ch)
                i += 1
        return "".join(out)

    def _quote(self, value: str) -> str:
        tmp = tempfile.TemporaryDirectory(prefix="attr-cuda-quote-arg-")
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "probe.ps1"
        value_literal = "'" + value.replace("'", "''") + "'"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$quoted = ConvertTo-AttrCudaQuotedProcessArgument -Value {value_literal}\n"
            "Write-Output \"BEGIN_QUOTED\"\n"
            "Write-Output $quoted\n"
            "Write-Output \"END_QUOTED\"\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        start = out.index("BEGIN_QUOTED\n") + len("BEGIN_QUOTED\n")
        end = out.index("END_QUOTED", start)
        # Strip exactly one trailing line separator, never rstrip() generally: the quoted
        # value can legitimately end in trailing content that rstrip() would also eat.
        quoted = out[start:end]
        for newline in ("\r\n", "\n", "\r"):
            if quoted.endswith(newline):
                quoted = quoted[: -len(newline)]
                break
        return quoted

    def _assert_round_trips(self, value: str) -> None:
        quoted = self._quote(value)
        self.assertEqual(
            self._argv_decode_one_quoted_argument(quoted),
            value,
            f"value={value!r} quoted={quoted!r}",
        )

    def test_plain_value_with_a_space_round_trips(self) -> None:
        self._assert_round_trips("CUDA / NVIDIA GeForce RTX 4090")

    def test_value_ending_in_a_single_trailing_backslash_round_trips(self) -> None:
        # e.g. a directory path with a trailing separator. Before this fix, a bare trailing
        # backslash reached the closing quote unescaped, letting the parser read it as an
        # escaped quote (\") instead of the argument terminator -- corrupting the boundary.
        self._assert_round_trips("C:\\mlvtmp\\contact-sheet\\")

    def test_value_ending_in_an_even_run_of_trailing_backslashes_round_trips(self) -> None:
        self._assert_round_trips("C:\\mlvtmp\\\\")

    def test_value_with_a_backslash_immediately_before_an_embedded_quote_round_trips(self) -> None:
        self._assert_round_trips('a\\"b')

    def test_value_with_two_backslashes_immediately_before_an_embedded_quote_round_trips(self) -> None:
        self._assert_round_trips('a\\\\"b')

    def test_value_with_a_bare_embedded_quote_and_no_backslash_round_trips(self) -> None:
        self._assert_round_trips('say "hello"')

    def test_empty_value_round_trips(self) -> None:
        self._assert_round_trips("")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
