"""Text-level contract tests for the PLAYBACK-ATTR-3-CUDA split-build route.

WHY TEXT-LEVEL. Every surface under test is a PowerShell generator whose product runs on a
host this suite cannot reach -- Ultra-Magnus (CUDA), the board host (Qt/MinGW), Bachelor (the
measurement laptop). What can be proved here is what the emitted text says, and that is
exactly where the failures this route exists to prevent were visible in advance: a job
targeting sm_89 for a GPU that cannot run it, an export probe on a host with no compiler, a
verdict reached without checking whether the backend loaded at all.

WHY ONE TestCase METHOD PER CLAIM. The repo-hygiene job runs
``python -m unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``; under that runner a
module-level ``def test_*`` table collects ZERO cases and exits green. These are TestCase
methods so they run under both unittest discovery and pytest.

WHY THE MEDIA EXTENSIONS ARE COMPOSED FROM PIECES. The project PreToolUse gate
(tools/hooks/mlv-never-authorized.py) refuses to write a file carrying a bare media-extension
literal without a CLIP_OR_NONE authorization -- correctly, and including this test. The
extensions are therefore assembled at runtime; the assertion is unchanged.

Ruling: .claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

PWSH = shutil.which("pwsh")


def _pwsh_quote(value: str) -> str:
    """A PowerShell single-quoted literal; newlines survive as real newlines inside the quotes."""
    return "'" + value.replace("'", "''") + "'"


_ANSI_ESCAPE_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PWSH_ERROR_VIEW_CONTINUATION_RX = re.compile(r"(?m)^[ \t]*\|[ \t]?")


def normalize_pwsh_message_text(text: str) -> str:
    """Undo pwsh's console-width error-view wrapping before substring matching.

    An uncaught terminating error is rendered by pwsh's own default host formatter, which wraps
    the exception message at the console width and prefixes each continuation line with ANSI
    colour codes and a '|' gutter -- so a phrase can land split across two lines on a narrower
    console than this one, and assertIn looking for the unbroken phrase then fails even though
    the thrown message is correct. Strip the ANSI codes and the '|' continuation prefixes, then
    collapse whitespace runs (including the newline the wrap introduced) to one space.
    """
    text = _ANSI_ESCAPE_RX.sub("", text)
    text = _PWSH_ERROR_VIEW_CONTINUATION_RX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


ROOT = Path(__file__).resolve().parents[2]

DLL_PAIR_JOB = ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1"
ASSEMBLE = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1"
STAGE_JOB = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1"
SHARED_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
ATTRIBUTION_JOB = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
RETIRED_COMPILE_JOB = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-compile-job.ps1"
RUNBOOK = ROOT / "docs" / "playback-attr-3-cuda.md"

# The scripts written for this route. The footage/cache token table below is asserted against
# exactly these -- the attribution job legitimately names one owner-authorized path and is not
# in this set.
NEW_SCRIPTS = (DLL_PAIR_JOB, ASSEMBLE, STAGE_JOB, SHARED_MODULE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DllPairJobTargetsSm86Tests(unittest.TestCase):
    """The measurement host's GPU cannot run sm_89 cubins; sm_86 is not optional."""

    def setUp(self) -> None:
        self.text = _read(DLL_PAIR_JOB)

    def test_required_architecture_is_sm86(self) -> None:
        self.assertIn("$RequiredArchitecture = 'sm_86'", self.text)

    def test_default_architecture_request_is_sm86(self) -> None:
        self.assertIn("[string[]]$CudaArchitectures = @('sm_86')", self.text)

    def test_generator_refuses_a_request_without_the_required_architecture(self) -> None:
        self.assertRegex(
            self.text,
            r"if \(\$architectures -notcontains \$RequiredArchitecture\) \{\s*\n\s*throw",
        )

    def test_emitted_job_proves_the_architecture_on_the_built_bytes(self) -> None:
        # A requested nvcc flag is not an emitted cubin: the job re-derives the tokens from
        # cuobjdump --list-elf and refuses when the required one is absent.
        self.assertIn("--list-elf", self.text)
        self.assertIn("$tokens -notcontains $RequiredArchitecture", self.text)
        self.assertIn("'architectureProof'", self.text)


class DllPairJobBuildsBothDllsTests(unittest.TestCase):
    """Shipping only the recon DLL produced a package that silently fell back."""

    def setUp(self) -> None:
        self.text = _read(DLL_PAIR_JOB)

    def test_uses_the_tracked_recon_backend_script(self) -> None:
        self.assertIn("build-backend-dll.ps1", self.text)

    def test_uses_the_tracked_amaze_backend_script(self) -> None:
        self.assertIn("amaze-debayer-dll.ps1", self.text)

    def test_both_dll_artifacts_are_named_and_published(self) -> None:
        self.assertIn("$ReconDllName = 'igpu_recon_cuda.dll'", self.text)
        self.assertIn("$AmazeDllName = 'igpu_amaze_debayer_cuda.dll'", self.text)
        self.assertIn("name = $ReconDllName; source = $reconDll", self.text)
        self.assertIn("name = $AmazeDllName; source = $amazeDll", self.text)

    def test_a_failed_amaze_build_is_fatal(self) -> None:
        self.assertRegex(self.text, r"if \(\$amazeRc -ne 0[^\n]*\)\s*\{\s*\n\s*Complete-Failed 12")

    def test_ships_the_cuda_runtime_from_the_compiling_toolkit(self) -> None:
        self.assertIn("$CudartName = 'cudart64_12.dll'", self.text)
        self.assertIn("$cudartSource = Join-Path $cudaBin $CudartName", self.text)


class DllPairManifestFieldsTests(unittest.TestCase):
    """The manifest is what every downstream host reads instead of re-deriving."""

    def setUp(self) -> None:
        self.text = _read(DLL_PAIR_JOB)

    def test_manifest_is_named_dll_pair_manifest_json(self) -> None:
        self.assertIn("$ManifestName = 'dll-pair-manifest.json'", self.text)

    def test_manifest_carries_every_required_field(self) -> None:
        for field in (
            "sourceCommit = $SourceCommit",
            "cudaArch = @($CudaArchitectures)",
            "nvccVersion = $nvccVersion",
            "files = $files",
            "pendingSymbolPresence = $pendingSymbolPresence",
            "builtOnHost = $env:COMPUTERNAME",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.text)

    def test_hashes_are_lowercase_sha256(self) -> None:
        self.assertIn("function Get-ShaLower", self.text)
        self.assertIn("ToLowerInvariant()", self.text)

    def test_pending_symbol_presence_is_a_real_boolean_bound_to_the_exports(self) -> None:
        self.assertIn(
            "$pendingSymbolPresence = [bool](@($exports[$ReconDllName] | Select-String 'igpu_recon_')",
            self.text,
        )

    def test_publish_is_transactional_with_the_manifest_last(self) -> None:
        self.assertIn(".partial", self.text)
        publish_partials = self.text.index("$StepLog['publishPartials'] = 0")
        publish_rename = self.text.index("$StepLog['publishRename'] = 0")
        publish_manifest = self.text.index("$StepLog['publishManifest'] = 0")
        self.assertLess(publish_partials, publish_rename)
        self.assertLess(publish_rename, publish_manifest)

    def test_job_owns_its_temp(self) -> None:
        self.assertIn("$env:TEMP = $Scratch", self.text)
        self.assertIn("$env:TMP = $Scratch", self.text)

    def test_failure_codes_are_distinct(self) -> None:
        pairs = re.findall(r"Complete-Failed (\d+) '([a-zA-Z]+)'", self.text)
        self.assertTrue(pairs, "the job declares no failure codes at all")
        by_code: dict[str, set[str]] = {}
        for code, step in pairs:
            by_code.setdefault(code, set()).add(step)
        for code, steps in by_code.items():
            with self.subTest(code=code):
                self.assertEqual(len(steps), 1, f"exit code {code} is shared by steps {sorted(steps)}")


class AssemblerManifestFieldsTests(unittest.TestCase):
    """The board host emits the layout the attribution job already verifies, plus three fields."""

    def setUp(self) -> None:
        self.text = _read(ASSEMBLE)

    def test_emits_the_schema_the_attribution_job_reads(self) -> None:
        self.assertIn("schema = 'mlvapp.playback-attr-3-cuda-build-cache-manifest.v1'", self.text)
        for field in ("exe = [ordered]@{", "dll = [ordered]@{", "packageZip = [ordered]@{"):
            with self.subTest(field=field):
                self.assertIn(field, self.text)

    def test_carries_the_three_added_fields(self) -> None:
        for field in (
            "pendingSymbolPresence = $pendingSymbolPresence",
            "dllPairManifestSha256 = $dllPairManifestSha256",
            "exeBuiltOnHost = $env:COMPUTERNAME",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.text)

    def test_verifies_the_dll_pair_manifest_before_deploying_it(self) -> None:
        self.assertIn("dll-pair-manifest.json", self.text)
        self.assertIn("DLL pair was built from", self.text)
        self.assertIn("sha256 mismatch for", self.text)
        binding = self.text.index("$StepLog['dllPairBinding'] = 0")
        deploy = self.text.index("$StepLog['dllPairDeploy'] = 0")
        self.assertLess(binding, deploy, "the pair must be verified before it is deployed")

    def test_requires_both_dlls_and_the_cuda_runtime(self) -> None:
        self.assertIn("@('igpu_recon_cuda.dll', 'igpu_amaze_debayer_cuda.dll', 'cudart64_12.dll')", self.text)

    def test_builds_from_a_clean_archive_with_injected_build_identity(self) -> None:
        self.assertIn("git -C $RepoRoot archive", self.text)
        header = self.text.index("New-AttrCudaBuildInfoHeader -SourceCommit $SourceCommit")
        qmake = self.text.index("$qmakeOutput = @(& $qmakeExe $proFile")
        self.assertLess(header, qmake, "build_buildinfo.h must be written before qmake runs")

    def test_deploys_the_qt_and_mingw_runtime(self) -> None:
        self.assertIn("--release --no-translations --compiler-runtime", self.text)
        self.assertIn("'libgomp-1.dll', 'libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll'", self.text)

    def test_publish_is_transactional_with_the_build_manifest_last(self) -> None:
        publish_partials = self.text.index("$StepLog['publishPartials'] = 0")
        publish_rename = self.text.index("$StepLog['publishRename'] = 0")
        publish_manifest = self.text.index("$StepLog['publishManifest'] = 0")
        self.assertLess(publish_partials, publish_rename)
        self.assertLess(publish_rename, publish_manifest)

    def test_make_job_count_is_quoted(self) -> None:
        # Unquoted, PowerShell hands mingw32-make a bare -j and the value separately, and make
        # refuses with "the '-j' option requires a positive integer argument".
        self.assertIn('"-j$MakeJobs"', self.text)


class StageJobTests(unittest.TestCase):
    """Side-files in, cache out, build.json last."""

    def setUp(self) -> None:
        self.text = _read(STAGE_JOB)

    def test_moves_the_package_and_manifest_from_inbox_side_files(self) -> None:
        self.assertIn("$Inbox = Join-Path $AgentRoot 'inbox'", self.text)
        self.assertIn("$Cache = Join-Path $AgentRoot 'cache'", self.text)
        self.assertIn("expected side-file missing from the inbox", self.text)

    def test_reverifies_sha256_on_the_host(self) -> None:
        self.assertIn("$actual = Get-ShaLower ([string]$item.path)", self.text)
        self.assertIn("sha256 mismatch for", self.text)

    def test_publishes_the_build_manifest_last(self) -> None:
        rename = self.text.index("$StepLog['publishRename'] = 0")
        manifest = self.text.index("$StepLog['publishManifest'] = 0")
        cleanup = self.text.index("$StepLog['inboxCleanup'] = 0")
        self.assertLess(rename, manifest, "artifacts must be published before build.json")
        self.assertLess(manifest, cleanup, "side-files may only be removed after the publish")

    def test_side_files_are_addressed_by_exact_name(self) -> None:
        self.assertIn(
            'Assert-AttrCudaDirectChild -Root $Inbox -Path (Join-Path $Inbox $name)', self.text
        )
        self.assertNotIn("Get-ChildItem", self.text)

    def test_names_are_derived_from_the_commit_not_taken_from_the_manifest(self) -> None:
        # build.json supplies hashes; the basenames come from the convention applied to
        # -SourceCommit, in the generator AND again on the host from the baked commit.
        self.assertIn("$derived = Get-AttrCudaArtifactNames -SourceCommit $SourceCommit", self.text)
        self.assertIn("$canonicalNames -cnotcontains $name", self.text)
        self.assertIn("the canonical name for $SourceCommit", self.text)
        self.assertIn("Names are derived, never taken from the manifest.", self.text)

    def test_every_name_and_every_resolved_path_is_checked_before_any_write(self) -> None:
        # Ordering is a property of the EMITTED job, so compare inside the template only -- the
        # generator's header prose names these cmdlets too.
        template = self.text[self.text.index("$template = @'") :]
        safety = template.index("$StepLog['artifactNameSafety'] = 0")
        for mutation in ("Publish-AttrCudaFileCopy -Source", "Publish-AttrCudaFileMove -Source",
                         "Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path ([string]$item.path)"):
            with self.subTest(mutation=mutation):
                self.assertLess(safety, template.index(mutation))
        for root, label in (("$Inbox", "inbox"), ("$Cache", "cache")):
            with self.subTest(root=root):
                self.assertIn(f"Assert-AttrCudaDirectChild -Root {root}", self.text)
        self.assertIn("Assert-AttrCudaSafeArtifactName -Name $name", self.text)

    def test_traversal_and_non_basenames_are_refused_by_the_shared_guard(self) -> None:
        module = _read(SHARED_MODULE)
        for token in (
            "ATTRCUDA_NAME_TRAVERSAL",
            "ATTRCUDA_NAME_NOT_A_BASENAME",
            "ATTRCUDA_PATH_NOT_DIRECT_CHILD",
        ):
            with self.subTest(token=token):
                self.assertIn(token, module)

    def test_verify_only_stops_before_anything_is_written(self) -> None:
        stop = self.text.index("RESULT=VERIFY_ONLY_OK")
        self.assertLess(stop, self.text.index("New-Item -ItemType Directory -Path $Work"))
        self.assertLess(stop, self.text.index("$PubReady = $true"))


class AttributionJobTests(unittest.TestCase):
    """What the measurement host may no longer do, and what it must check first."""

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)

    def test_no_msvc_export_tooling_anywhere_in_the_job(self) -> None:
        # Bachelor has Visual Studio WITHOUT the VC tools component. The old probe could only
        # ever throw there, which is why the symbol test moved to the building host.
        lowered = self.text.lower()
        for token in ("dumpbin", "vswhere"):
            with self.subTest(token=token):
                self.assertNotIn(token, lowered)

    def test_pending_symbol_presence_is_read_from_the_build_manifest(self) -> None:
        self.assertIn("$pendingSymbolPresence = [bool]$buildManifest.pendingSymbolPresence", self.text)

    def test_a_missing_or_non_boolean_pending_symbol_presence_is_refused(self) -> None:
        # The check moved into the shared authentication call, which the job cannot get past.
        self.assertIn(
            "ATTRCUDA_BUILD_MANIFEST_SYMBOL_PRESENCE_INVALID", _read(SHARED_MODULE)
        )

    def test_the_build_manifest_is_authenticated_before_it_is_parsed(self) -> None:
        # The cache is mutable and this job does not own it: a same-named build.json with
        # matching artifacts would otherwise forge pendingSymbolPresence and the DLL association.
        self.assertIn("[ValidatePattern('^[0-9a-f]{64}$')]\n    [string]$BuildManifestSha256", self.text)
        self.assertIn(
            "$buildManifest = Assert-AttrCudaBuildManifest -Path $buildManifestPath "
            "-ExpectedSha256 $BuildManifestSha256 -ExpectedSourceCommit $SourceCommit",
            self.text,
        )
        module = _read(SHARED_MODULE)
        function = module[module.index("function Assert-AttrCudaBuildManifest") :]
        function = function[: function.index("\nfunction ")]
        # Statement positions, not prose: the doc comment names ConvertFrom-Json too.
        self.assertLess(
            function.index('throw "ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH'),
            function.index("$manifest = [IO.File]::ReadAllText($Path) | ConvertFrom-Json"),
            "the hash must be checked before the manifest is parsed",
        )

    def test_the_build_manifest_must_chain_to_the_dll_pair_manifest(self) -> None:
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_DLLPAIR_UNBOUND", _read(SHARED_MODULE))
        self.assertIn("dllPairManifestSha256=$dllPairManifestSha256", self.text)

    def test_the_build_manifest_source_commit_must_equal_the_generators(self) -> None:
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_COMMIT_MISMATCH", _read(SHARED_MODULE))

    def test_backend_availability_gate_is_present(self) -> None:
        # The parse and the decision live in the shared module (and are embedded verbatim into
        # the emitted job); the job wires them to its refusal path.
        module = _read(SHARED_MODULE)
        for token in ("cuda_backend_available", "r16_available", "r16_reason"):
            with self.subTest(token=token):
                self.assertIn(token, module)
        self.assertIn("BACKEND_NOT_AVAILABLE", self.text)
        self.assertIn("exit $verdict.exitCode", self.text)

    def test_the_gate_requires_both_fields_to_be_one(self) -> None:
        module = _read(SHARED_MODULE)
        self.assertIn(
            "$admitted = ($cudaBackendAvailable -eq '1' -and $r16Available -eq '1')", module
        )
        self.assertIn("exitCode = $(if ($admitted) { 0 } else { 15 })", module)
        self.assertIn("if (-not $verdict.admitted)", self.text)

    def test_the_gate_runs_before_every_verdict(self) -> None:
        gate = self.text.index("RESULT=BACKEND_NOT_AVAILABLE")
        for later_verdict in (
            "RESULT=GPU_RECON_FRAMES_ZERO",
            "RESULT=CPU_FALLBACK_DETECTED",
            "RESULT=$resultVerb",
        ):
            with self.subTest(verdict=later_verdict):
                self.assertLess(gate, self.text.index(later_verdict))

    def test_both_fields_and_the_reason_are_recorded(self) -> None:
        self.assertIn("cudaBackendAvailable = $verdict.cudaBackendAvailable", self.text)
        self.assertIn("r16Available = $verdict.r16Available", self.text)
        self.assertIn("r16Reason = $verdict.r16Reason", self.text)
        self.assertIn("diagnostics = $diagnostics", self.text)

    def test_the_log_comes_from_the_smoke_result_not_a_glob(self) -> None:
        # run-release-gui-smoke.ps1 writes into logs-<stem>-<GUID>, so the old search could
        # never match and the job threw before reaching any gate.
        self.assertIn(
            "Resolve-AttrCudaSmokeRunLog -ResultJsonPath $resultPath -ContainingRoot $Work",
            self.text,
        )
        self.assertNotIn("-Filter 'mlvapp-*.log'", self.text)

    def test_an_unavailable_log_fails_closed_with_its_own_code(self) -> None:
        self.assertIn("RESULT=SMOKE_LOG_UNAVAILABLE", self.text)
        self.assertIn("exit 16", self.text)
        refusal = self.text.index("RESULT=SMOKE_LOG_UNAVAILABLE")
        for later_verdict in ("RESULT=BACKEND_NOT_AVAILABLE", "RESULT=$resultVerb"):
            with self.subTest(verdict=later_verdict):
                self.assertLess(refusal, self.text.index(later_verdict))


class SmokeRunnerPinTests(unittest.TestCase):
    """ATTR3-SMOKE-RUNNER-DEPS-1: the runner's FULL dependency closure is pinned, not the
    runner alone.

    Round 1 (ATTR3-SMOKE-RUNNER-PIN-1) hash-pinned only run-release-gui-smoke.ps1; Bachelor
    still could not launch it, because the runner dot-sources four siblings and imports a
    module, all resolved via $PSScriptRoot, and none of those five was ever staged. Round 1/2
    then derived the closure by SCANNING; round 3 (NARROW BY REDESIGN) replaced discovery with
    an EXPLICITLY PINNED six-file manifest (Get-AttrCudaSmokeRunnerClosureManifest) -- never
    mechanically derived -- proved complete by an AST census (Assert-AttrCudaClosureComplete) at
    generation time -- every file in it is still hash-pinned from the committed git blob at
    -SourceCommit -- the same bytes attr3-stage-smoke-runner-job.ps1 stages -- never from a
    working-tree file.
    """

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)

    def test_existence_only_check_is_gone(self) -> None:
        self.assertNotIn(
            "foreach ($name in @($PresentMonName, 'run-release-gui-smoke.ps1')) {", self.text
        )

    def test_single_file_pin_check_is_gone(self) -> None:
        # ATTR3-SMOKE-RUNNER-PIN-1's single-file pin (round 2) is superseded by the closure.
        self.assertNotIn("$SmokeRunnerSha256 = '__SMOKE_RUNNER_SHA256__'", self.text)
        self.assertNotIn(
            '$SmokeRunnerCacheName = "run-release-gui-smoke-$($SmokeRunnerSha256.Substring(0, 16)).ps1"',
            self.text,
        )

    def test_the_scan_based_member_loop_is_gone(self) -> None:
        # round 3 (NARROW BY REDESIGN): the inline per-member loop is replaced by one call into
        # the shared, embedded Get-AttrCudaClosureDirectoryMismatch.
        self.assertNotIn("foreach ($closureEntry in $SmokeRunnerClosure) {", self.text)
        self.assertNotIn(
            "if ($closureEntryActualSha -ne $closureEntry.sha256.ToUpperInvariant()) {", self.text
        )

    def test_the_emitted_job_pins_the_closure_and_checks_it_with_the_shared_function(self) -> None:
        self.assertIn("$SmokeRunnerClosure = __SMOKE_RUNNER_CLOSURE__", self.text)
        self.assertIn("$SmokeRunnerClosureDirName = '__SMOKE_RUNNER_CLOSURE_DIR_NAME__'", self.text)
        self.assertIn(
            "$smokeRunnerClosureDir = Join-Path $Cache $SmokeRunnerClosureDirName", self.text
        )
        self.assertIn(
            "$smokeRunnerClosureMismatch = Get-AttrCudaClosureDirectoryMismatch -Dir "
            "$smokeRunnerClosureDir -Entries $SmokeRunnerClosure",
            self.text,
        )
        self.assertIn(
            'throw "ATTRCUDA_SMOKE_RUNNER_STALE $smokeRunnerClosureMismatch"', self.text
        )

    def test_the_shared_directory_mismatch_function_is_embedded(self) -> None:
        self.assertIn("'Get-AttrCudaClosureDirectoryMismatch',", self.text)

    def test_the_closure_digest_is_validated_before_it_is_trusted(self) -> None:
        # Fable minor (round 2, carried forward): validated the same way -PresentMonSha256 is,
        # before it is substituted into the emitted job.
        self.assertIn(
            "if ($smokeRunnerClosureDigest -notmatch '^[0-9a-f]{64}$') {", self.text
        )
        self.assertIn("ATTRCUDA_BLOB_SHA_MALFORMED", self.text)

    def test_the_pin_is_baked_from_the_committed_git_blob_not_the_working_tree(self) -> None:
        # round 3: the pinned manifest, never a -RepoRelativePath parameter -- and proved
        # complete against the real files before it is trusted (Assert-AttrCudaClosureComplete).
        self.assertIn(
            "Assert-AttrCudaClosureComplete -RepoRoot $RepoRoot -Commit $SourceCommit", self.text
        )
        self.assertIn(
            "Resolve-AttrCudaSmokeRunnerClosure -RepoRoot $RepoRoot -Commit $SourceCommit",
            self.text,
        )
        self.assertNotIn("-RepoRelativePath $SmokeRunnerRelativePath", self.text)
        self.assertNotIn("SmokeRunnerRelativePath", self.text)
        self.assertIn("Get-AttrCudaClosureDigestHex -Closure $smokeRunnerClosure", self.text)
        # ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): substitution is now a single
        # Expand-AttrCudaTemplate call over a token map, not a chained .Replace() sequence.
        self.assertIn("SMOKE_RUNNER_CLOSURE_DIR_NAME = $smokeRunnerClosureDirName", self.text)
        self.assertIn("Expand-AttrCudaTemplate -Template $template -Tokens", self.text)

    def test_the_refusal_runs_before_presentmon_and_before_deployment(self) -> None:
        refusal = self.text.index("ATTRCUDA_SMOKE_RUNNER_STALE")
        deploy = self.text.index(
            "[void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ExeName) -Destination $exePath)"
        )
        present_mon_start = self.text.index("Start-PresentMonCapture $presentMonPath")
        self.assertLess(refusal, deploy, "the pin must be checked before the package is deployed")
        self.assertLess(
            refusal, present_mon_start, "the pin must be checked before PresentMon starts"
        )


class SmokeRunFailedBeforePresentMonTests(unittest.TestCase):
    """ATTR3-SMOKE-RUNNER-DEPS-1 (D): the smoke run's own outcome is checked before PresentMon
    is waited on, so a smoke-side failure is never reported as PRESENTMON_TIMEOUT."""

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)

    def test_smoke_outcome_is_checked_before_waiting_on_presentmon(self) -> None:
        smoke_launch = self.text.index("$smokeLaunchException = $null")
        check = self.text.index(
            "if ($null -ne $smokeLaunchException -or $smokeRc -ne 0 -or -not "
            "(Test-Path -LiteralPath $resultPath)) {"
        )
        wait = self.text.index("$presentMonDoneResult = Wait-PresentMonCapture $presentMonProc")
        self.assertLess(smoke_launch, check)
        self.assertLess(check, wait, "the outcome check must run before PresentMon is waited on")

    def test_presentmon_is_stopped_not_waited_out_on_a_smoke_failure(self) -> None:
        check = self.text.index(
            "if ($null -ne $smokeLaunchException -or $smokeRc -ne 0 -or -not "
            "(Test-Path -LiteralPath $resultPath)) {"
        )
        stop = self.text.index("Stop-PresentMonCapture", check)
        wait = self.text.index("$presentMonDoneResult = Wait-PresentMonCapture $presentMonProc")
        self.assertLess(stop, wait)

    def test_smoke_run_failed_is_a_distinct_result_with_a_stderr_tail(self) -> None:
        self.assertIn("RESULT=SMOKE_RUN_FAILED", self.text)
        self.assertIn("smokeStderrTail", self.text)
        self.assertIn("exit 18", self.text)

    def test_presentmon_timeout_still_exists_for_its_own_case(self) -> None:
        # Unchanged: Wait-PresentMonCapture still throws PRESENTMON_TIMEOUT, now reached only
        # once the smoke run is already known to have succeeded.
        self.assertIn("PRESENTMON_TIMEOUT: did not exit within", self.text)
        failed = self.text.index("RESULT=SMOKE_RUN_FAILED")
        timeout_fn = self.text.index("function Wait-PresentMonCapture")
        self.assertLess(timeout_fn, failed, "PRESENTMON_TIMEOUT's own function is defined earlier in the file")

    def test_a_launch_exception_is_caught_and_mapped_to_smoke_run_failed(self) -> None:
        # sol, PR #144 major 3: a terminating exception starting the nested pwsh used to skip
        # $smokeRc and the whole SMOKE_RUN_FAILED branch, bypassing PresentMon cleanup entirely.
        launch = self.text.index("& \"$env:ProgramFiles\\PowerShell\\7\\pwsh.exe\"")
        try_start = self.text.rindex("try {", 0, launch)
        catch_start = self.text.index("} catch {", launch)
        exception_capture = self.text.index("$smokeLaunchException = $_", catch_start)
        check = self.text.index(
            "if ($null -ne $smokeLaunchException -or $smokeRc -ne 0 -or -not "
            "(Test-Path -LiteralPath $resultPath)) {"
        )
        self.assertLess(try_start, launch)
        self.assertLess(launch, catch_start)
        self.assertLess(catch_start, exception_capture)
        self.assertLess(exception_capture, check)

    def test_smoke_run_failed_records_the_capped_exception_type_and_presentmon_stop_outcome(self) -> None:
        self.assertIn("smokeLaunchExceptionType", self.text)
        self.assertIn("smokeLaunchExceptionMessage", self.text)
        self.assertIn("presentMonConfirmedExited", self.text)
        self.assertIn("presentMonKillError", self.text)
        self.assertIn("presentMonWaitError", self.text)
        self.assertIn("$MaxSmokeLaunchExceptionChars = 500", self.text)


class StopPresentMonCaptureReportsFailuresTests(unittest.TestCase):
    """ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 3): Kill()/WaitForExit() failures must be
    reported, not swallowed by an empty catch block, and confirmedExited must be read from the
    process afterward rather than assumed from "no exception"."""

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)

    def test_no_empty_catch_blocks_remain_in_the_stop_function(self) -> None:
        start = self.text.index("function Stop-PresentMonCapture(")
        end = self.text.index("\nfunction Get-FrameRows(", start)
        body = self.text[start:end]
        self.assertNotIn("catch { }", body)
        self.assertIn("$killError = $_.Exception.Message", body)
        self.assertIn("$waitError = ", body)

    def test_returns_a_status_object_not_void(self) -> None:
        start = self.text.index("function Stop-PresentMonCapture(")
        end = self.text.index("\nfunction Get-FrameRows(", start)
        body = self.text[start:end]
        self.assertIn("confirmedExited = [bool]$Proc.HasExited", body)
        self.assertIn("killError = $killError", body)
        self.assertIn("waitError = $waitError", body)


class Attr3JobNeverActivatesExcludedDependencyTests(unittest.TestCase):
    """ATTR3-SMOKE-RUNNER-DEPS-1 round 3: the AST-census pinned exclusion for the dormant
    detect-playback-artifacts.ps1 launch site (kept next to
    Test-AttrCudaClosureScanExclusionMatch in AttrCudaArtifacts.psm1) is sound only as long as
    the ATTR-3 attribution job never passes -DetectPlaybackArtifacts -- the one switch that
    would make run-release-gui-smoke.ps1 actually execute that reference. Required test (per the
    review): if this job ever starts passing that switch, this test must fail before the
    exclusion becomes unsound in production.
    """

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)

    def test_the_emitted_smoke_command_never_passes_detectplaybackartifacts(self) -> None:
        cmd_start = self.text.index('$cmd = "& $(ConvertTo-PsSingleQuoted $smoke)')
        cmd_end = self.text.index('\n', cmd_start)
        cmd_line = self.text[cmd_start:cmd_end]
        self.assertNotIn("-DetectPlaybackArtifacts", cmd_line)

    def test_no_other_reference_to_the_switch_exists_in_the_generator(self) -> None:
        # Belt and suspenders: the flag must not appear anywhere else in the generator either
        # (e.g. a second, less obvious invocation site added later).
        self.assertNotIn("-DetectPlaybackArtifacts", self.text)


class AttributionJobFixtureRehearsalTests(unittest.TestCase):
    """ATTR3-FIXTURE-REHEARSAL-1: -ClipId also admits the two tracked fixtures, unmistakably."""

    _CLIP_ID_PATTERN_RX = re.compile(
        r"\[ValidatePattern\('(?P<pattern>[^']+)'\)\]\s*\r?\n\s*\[string\]\$ClipId"
    )

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)
        match = self._CLIP_ID_PATTERN_RX.search(self.text)
        self.assertIsNotNone(match, "could not find the -ClipId ValidatePattern in the generator")
        self.clip_id_pattern = match.group("pattern")

    def match_in_powershell(self, candidates: dict[str, bool]) -> None:
        """Evaluate the pattern in the engine that ENFORCES it.

        The pattern is .NET's, not Python's: it carries a scoped inline flag `(?-i:...)` and the
        `\\z` anchor, and Python's `re` accepts neither on every supported version -- compiling it
        with `re` made these tests error on the CI runner while passing locally, and it would have
        been testing a different engine's semantics either way.
        """
        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        lines = ["$pattern = " + _pwsh_quote(self.clip_id_pattern)]
        for candidate in candidates:
            lines.append(
                "Write-Output ('CANDIDATE ' + " + _pwsh_quote(candidate) + " + ' -> ' + "
                "([bool](" + _pwsh_quote(candidate) + " -cmatch $pattern)))"
            )
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for candidate, expected in candidates.items():
            with self.subTest(clip_id=candidate):
                self.assertIn(f"CANDIDATE {candidate} -> {expected}", proc.stdout, proc.stdout)

    def test_owner_clip_id_pattern_still_works(self) -> None:
        self.match_in_powershell({"M16-1243": True, "a99-123": True, "Z00-9999": True})

    def test_fixture_clip_ids_are_accepted(self) -> None:
        self.match_in_powershell({"tiny_dual_iso": True, "large_dual_iso": True})

    def test_an_unknown_or_miscased_stem_is_refused(self) -> None:
        # TINY_DUAL_ISO and a trailing newline were both sol findings (PR #137 r1 and r2): each
        # validated once and then failed the case-sensitive membership test.
        self.match_in_powershell({
            "some_other_clip": False,
            "tiny_dual_iso_extra": False,
            "TINY_DUAL_ISO": False,
            "Tiny_Dual_Iso": False,
            "medium_dual_iso": False,
            "tiny_dual_iso\n": False,
        })

    def test_fixture_ids_are_a_literal_allowlist_not_a_loose_pattern(self) -> None:
        # The flag is decided by an exact membership test against the two literals, never by
        # re-deriving it from the ValidatePattern regex.
        self.assertIn("$FixtureClipIds = @('tiny_dual_iso', 'large_dual_iso')", self.text)
        self.assertIn("$FixtureClipIds -ccontains $ClipId", self.text)

    def test_emitted_job_carries_the_flag_from_the_generators_membership_test(self) -> None:
        self.assertIn("$FixtureRehearsal = __FIXTURE_REHEARSAL__", self.text)
        self.assertIn("$fixtureRehearsalLiteral = if ($isFixtureRehearsal)", self.text)
        # ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): substitution is now a single
        # Expand-AttrCudaTemplate call over a token map, not a chained .Replace() sequence -- see
        # that function's own header in AttrCudaArtifacts.psm1.
        self.assertIn("FIXTURE_REHEARSAL = $fixtureRehearsalLiteral", self.text)
        self.assertIn("Expand-AttrCudaTemplate -Template $template -Tokens", self.text)

    def test_flag_is_recorded_in_every_summary_and_in_the_evidence_manifest(self) -> None:
        # summary.json is written on every early-exit venue; evidence-manifest.json only on
        # the success path. Both carry the flag, so a fixture run is unmistakable either way.
        # early-exit venues, the artifact index, and the success summary: every reader-facing output.
        self.assertGreaterEqual(self.text.count("fixtureRehearsal=$FixtureRehearsal"), 6)
        self.assertGreaterEqual(self.text.count("fixtureRehearsal = $FixtureRehearsal"), 3)  # provenance, manifest, success summary

    def test_clip_path_cache_parent_and_basename_checks_are_unchanged(self) -> None:
        self.assertIn("if ((Split-Path -Parent $clipPath) -ine $Cache)", self.text)
        self.assertIn(
            "if ([IO.Path]::GetFileNameWithoutExtension($clipPath) -cne $ClipId)", self.text
        )
        self.assertIn(
            "if ($clipPath -notmatch '^[A-Za-z]:\\\\[A-Za-z0-9 _.\\\\-]+$')", self.text
        )

    def test_consent_receipt_is_never_cited_for_a_fixture_run(self) -> None:
        # Was queue card ATTR3-CONSENT-RECEIPT-TEST-1: pinned so a revert to the unconditional
        # form (consentReceipt = $ConsentReceiptFileName, cited for a fixture run too) goes red.
        self.assertIn(
            "consentReceipt = $(if ($FixtureRehearsal) { $null } else { $ConsentReceiptFileName })",
            self.text,
        )

    def test_clippath_is_optional_for_a_fixture_id_and_fixture_sha_gates_it(self) -> None:
        # ATTR3-FIXTURE-STAGE-1.
        self.assertIn("[Parameter(Mandatory = $false)]", self.text)
        self.assertIn("[string]$FixtureSha256 = ''", self.text)
        self.assertIn("PLAYBACK_ATTR3_FIXTURE_SHA_REQUIRED", self.text)
        self.assertIn("PLAYBACK_ATTR3_FIXTURE_SHA_REFUSED", self.text)
        # ATTR3-FOOTAGE-BIND-1 PR-B: -ClipPath is REFUSED for an owner id, never required --
        # the id is resolved through tools/gates/resolve_consented_clip.py instead.
        self.assertIn("PLAYBACK_ATTR3_CLIPPATH_REFUSED", self.text)
        self.assertNotIn("PLAYBACK_ATTR3_CLIPPATH_REQUIRED", self.text)

    def test_fixture_content_is_authenticated_before_the_package_is_deployed(self) -> None:
        # ATTR3-FIXTURE-STAGE-1: a cache file name proves nothing about its bytes.
        self.assertIn("RESULT=FIXTURE_CONTENT_MISMATCH", self.text)
        self.assertIn("exit 17", self.text)
        mismatch = self.text.index("RESULT=FIXTURE_CONTENT_MISMATCH")
        deploy = self.text.index(
            "Expand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)"
        )
        self.assertLess(mismatch, deploy, "the fixture content gate must run before deployment")

    def test_build_manifest_authentication_smoke_log_selection_and_eligibility_gate_are_unchanged(
        self,
    ) -> None:
        # A fixture run is still subject to the same three gates as an owner-clip run.
        self.assertIn(
            "$buildManifest = Assert-AttrCudaBuildManifest -Path $buildManifestPath "
            "-ExpectedSha256 $BuildManifestSha256 -ExpectedSourceCommit $SourceCommit",
            self.text,
        )
        self.assertIn(
            "Resolve-AttrCudaSmokeRunLog -ResultJsonPath $resultPath -ContainingRoot $Work",
            self.text,
        )
        self.assertIn("if (-not $verdict.admitted)", self.text)
        self.assertIn("exit $verdict.exitCode", self.text)


class AttributionJobOwnerClipRefusalTests(unittest.TestCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B: an owner id is resolved (never handed a path); -ClipPath and
    -FixtureSha256 are both REFUSED for one. The fixture arm is untouched."""

    def setUp(self) -> None:
        self.text = _read(ATTRIBUTION_JOB)

    def _generate(self, clip_id: str, out_file: Path, *, clip_path: str | None = None) -> subprocess.CompletedProcess:
        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        # ATTR3-FIXTURE-STAGE-1 made -FixtureSha256 mandatory for a fixture id.
        is_fixture = clip_id in ("tiny_dual_iso", "large_dual_iso")
        fixture_sha = ["-FixtureSha256", "b" * 64] if is_fixture else []
        if clip_path is None:
            clip_path = "C:\\synthetic-cache\\" + clip_id if is_fixture else None
        clip_path_args = ["-ClipPath", clip_path] if clip_path else []
        return subprocess.run(
            [
                PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ATTRIBUTION_JOB),
                "-SourceCommit", head, "-BuildManifestSha256", "0" * 64,
                "-ClipId", clip_id,
                *clip_path_args,
                *fixture_sha,
                "-OutFile", str(out_file),
            ],
            capture_output=True, text=True,
        )

    def test_the_owner_route_decision_precedes_the_write(self) -> None:
        decision = self.text.index("$ownerPartsForJob = @(Resolve-AttrCudaOwnerClipParts")
        self.assertLess(decision, self.text.index("[IO.File]::WriteAllText($OutFile"))
        self.assertNotIn("ContentVerifiedBy", self.text)  # unconditional: no override switch

    def test_an_owner_clip_id_with_a_typed_path_is_refused_before_resolving_anything(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "owner.job.ps1"
            proc = self._generate("M16-1243", out_file, clip_path="C:\\synthetic-cache\\M16-1243")
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(
                "PLAYBACK_ATTR3_CLIPPATH_REFUSED",
                normalize_pwsh_message_text(proc.stdout + proc.stderr),
            )
            self.assertFalse(out_file.exists())

    def test_a_fixture_id_is_still_emitted(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "fixture.job.ps1"
            proc = self._generate("tiny_dual_iso", out_file)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(out_file.exists())
            self.assertIn("$FixtureRehearsal = $true", out_file.read_text(encoding="utf-8"))


class OwnerDecisionOrderingAstTests(unittest.TestCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL), extended round 6. An AST census over the
    generator's own TOP-LEVEL statements only -- a FunctionDefinitionAst is one top-level
    statement whose BODY never executes at definition time, so this never descends into one.
    Proves: no Import-Module, Get-AttrCudaEmbeddedFunctionSource, git invocation or Test-Path
    precedes the statement that throws PLAYBACK_ATTR3_CLIPPATH_REFUSED, that the $RepoRoot
    resolution statement is the only thing standing between that decision and the resolver call
    (Resolve-AttrCudaOwnerClipParts), and (round 6, astra major) that the resolver call itself
    precedes the owner arm's own -AgentRoot shape-check statement -- so the complete owner-id
    decision (flags, RepoRoot, resolver) always runs before AgentRoot validation, never after."""

    def _census(self) -> dict:
        import json
        import tempfile

        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$text = [IO.File]::ReadAllText('{ATTRIBUTION_JOB}')\n"
            "$tokens = $null; $errors = $null\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)\n"
            "if ($errors.Count -gt 0) { throw ('PARSE_ERROR: ' + ($errors -join '; ')) }\n"
            "$statements = @($ast.EndBlock.Statements)\n"
            "$forbiddenNames = @('Import-Module', 'Get-AttrCudaEmbeddedFunctionSource', 'Test-Path', 'git')\n"
            "$forbiddenIndex = -1\n"
            "$throwIndex = -1\n"
            "$repoRootIndex = -1\n"
            "$resolverCallIndex = -1\n"
            "$ownerAgentRootIndex = -1\n"
            "for ($i = 0; $i -lt $statements.Count; $i++) {\n"
            "    $stmt = $statements[$i]\n"
            "    if ($stmt -is [System.Management.Automation.Language.FunctionDefinitionAst]) { continue }\n"
            "    if ($forbiddenIndex -lt 0) {\n"
            "        $commands = $stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)\n"
            "        foreach ($c in $commands) {\n"
            "            $name = $c.GetCommandName()\n"
            "            if ($null -ne $name -and ($forbiddenNames -icontains $name)) { $forbiddenIndex = $i; break }\n"
            "        }\n"
            "    }\n"
            "    if ($throwIndex -lt 0) {\n"
            "        $throwStatements = $stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.ThrowStatementAst] }, $true)\n"
            "        foreach ($t in $throwStatements) {\n"
            "            if ($t.Extent.Text -like '*PLAYBACK_ATTR3_CLIPPATH_REFUSED*') { $throwIndex = $i; break }\n"
            "        }\n"
            "    }\n"
            "    if ($repoRootIndex -lt 0 -and $stmt.Extent.Text -like '*Resolve-Path -LiteralPath $RepoRoot*') { $repoRootIndex = $i }\n"
            "    if ($resolverCallIndex -lt 0 -and $stmt.Extent.Text -like '*Resolve-AttrCudaOwnerClipParts -ClipId*') { $resolverCallIndex = $i }\n"
            "    # The LAST top-level statement carrying this throw is the owner arm's standalone,\n"
            "    # post-resolver AgentRoot check -- the fixture arm's own copy lives earlier, nested\n"
            "    # inside the owner/fixture flag-refusal if/else.\n"
            "    if ($stmt.Extent.Text -like '*PLAYBACK_ATTR3_AGENTROOT_INVALID*') { $ownerAgentRootIndex = $i }\n"
            "}\n"
            "[pscustomobject]@{ forbiddenIndex = $forbiddenIndex; throwIndex = $throwIndex; "
            "repoRootIndex = $repoRootIndex; resolverCallIndex = $resolverCallIndex; "
            "ownerAgentRootIndex = $ownerAgentRootIndex } | ConvertTo-Json -Compress\n"
        )
        with tempfile.TemporaryDirectory(prefix="attr3-ast-order-") as tmp:
            script_path = Path(tmp) / "census.ps1"
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_no_import_module_embedded_source_git_or_test_path_precedes_the_clippath_refusal(
        self,
    ) -> None:
        census = self._census()
        self.assertGreaterEqual(census["throwIndex"], 0, "CLIPPATH_REFUSED throw not found")
        self.assertGreaterEqual(census["forbiddenIndex"], 0, "no forbidden command found at all")
        self.assertLess(
            census["throwIndex"],
            census["forbiddenIndex"],
            "an Import-Module / Get-AttrCudaEmbeddedFunctionSource / git / Test-Path statement "
            "precedes the PLAYBACK_ATTR3_CLIPPATH_REFUSED throw",
        )

    def test_reporoot_resolution_is_the_only_io_before_the_resolver_call(self) -> None:
        census = self._census()
        self.assertGreaterEqual(census["repoRootIndex"], 0, "RepoRoot resolution statement not found")
        self.assertGreaterEqual(census["resolverCallIndex"], 0, "resolver call statement not found")
        self.assertLess(census["throwIndex"], census["repoRootIndex"])
        self.assertLess(census["repoRootIndex"], census["resolverCallIndex"])
        # None of Import-Module / Get-AttrCudaEmbeddedFunctionSource / git / Test-Path appears in
        # ANY top-level statement before the resolver call -- so the only I/O between the
        # CLIPPATH decision and the resolver call is RepoRoot's own Resolve-Path.
        self.assertGreater(census["forbiddenIndex"], census["resolverCallIndex"])

    def test_resolver_call_precedes_the_owner_arm_agentroot_validation(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 6 (astra major): the complete owner-id decision -- flag
        # refusals, then RepoRoot resolution, then the resolver call and its own typed refusal --
        # must run before AgentRoot validation (and anything else unrelated), never after it.
        census = self._census()
        self.assertGreaterEqual(census["resolverCallIndex"], 0, "resolver call statement not found")
        self.assertGreaterEqual(census["ownerAgentRootIndex"], 0, "owner-arm AgentRoot check not found")
        self.assertLess(
            census["resolverCallIndex"],
            census["ownerAgentRootIndex"],
            "the owner arm's AgentRoot validation precedes the resolver invocation",
        )


class FirstThrowCapableStatementAstTests(unittest.TestCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 5 (STRUCTURAL). Generalizes
    OwnerDecisionOrderingAstTests beyond the four forbidden command names: walking the
    generator's own $ast.EndBlock.Statements in textual order (skipping FunctionDefinitionAst,
    whose body never executes at definition time), the FIRST statement that CAN throw -- either
    it contains a ThrowStatementAst directly, or it calls a function (by name) whose own body
    contains a ThrowStatementAst -- must be the owner/fixture flag-refusal if/else block itself.
    Round 4 left -AgentRoot's shape check as a standalone `if` ahead of that block, so it was the
    first throw-capable statement instead; round 5 folded it into both arms of the if/else so the
    block remains the first thing that can throw. Round 6 moves the OWNER arm's copy out again --
    now to a standalone statement AFTER the resolver call, per OwnerDecisionOrderingAstTests --
    but the FIXTURE arm's own copy stays nested here, so this block is still the first thing that
    can throw for either arm."""

    def _first_throw_capable(self) -> dict:
        import json
        import tempfile

        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$text = [IO.File]::ReadAllText('{ATTRIBUTION_JOB}')\n"
            "$tokens = $null; $errors = $null\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)\n"
            "if ($errors.Count -gt 0) { throw ('PARSE_ERROR: ' + ($errors -join '; ')) }\n"
            "$statements = @($ast.EndBlock.Statements)\n"
            "$throwingFunctionNames = [System.Collections.Generic.HashSet[string]]::new(\n"
            "    [string[]]@(), [System.StringComparer]::OrdinalIgnoreCase)\n"
            "foreach ($stmt in $statements) {\n"
            "    if ($stmt -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) { continue }\n"
            "    $bodyThrows = @($stmt.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.ThrowStatementAst] }, $true))\n"
            "    if ($bodyThrows.Count -gt 0) { [void]$throwingFunctionNames.Add($stmt.Name) }\n"
            "}\n"
            "$firstIndex = -1\n"
            "$firstText = $null\n"
            "for ($i = 0; $i -lt $statements.Count; $i++) {\n"
            "    $stmt = $statements[$i]\n"
            "    if ($stmt -is [System.Management.Automation.Language.FunctionDefinitionAst]) { continue }\n"
            "    $canThrow = $false\n"
            "    $throwStatements = @($stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.ThrowStatementAst] }, $true))\n"
            "    if ($throwStatements.Count -gt 0) { $canThrow = $true }\n"
            "    if (-not $canThrow) {\n"
            "        $commands = @($stmt.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))\n"
            "        foreach ($c in $commands) {\n"
            "            $name = $c.GetCommandName()\n"
            "            if ($null -ne $name -and $throwingFunctionNames.Contains($name)) { $canThrow = $true; break }\n"
            "        }\n"
            "    }\n"
            "    if ($canThrow) { $firstIndex = $i; $firstText = $stmt.Extent.Text; break }\n"
            "}\n"
            "[pscustomobject]@{ firstIndex = $firstIndex; firstText = $firstText } | ConvertTo-Json -Compress\n"
        )
        with tempfile.TemporaryDirectory(prefix="attr3-first-throw-ast-") as tmp:
            script_path = Path(tmp) / "first_throw_census.ps1"
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_first_throw_capable_statement_is_the_owner_fixture_flag_refusal_block(self) -> None:
        census = self._first_throw_capable()
        self.assertGreaterEqual(census["firstIndex"], 0, "no throw-capable top-level statement found")
        text = census["firstText"] or ""
        self.assertIn(
            "PLAYBACK_ATTR3_CLIPPATH_REFUSED",
            text,
            "the first throw-capable top-level statement is not the owner/fixture flag-refusal "
            "block -- something else can throw before it",
        )
        self.assertIn(
            "PLAYBACK_ATTR3_AGENTROOT_INVALID",
            text,
            "the AgentRoot shape check must be nested inside the same flag-refusal if/else, not "
            "a standalone statement ahead of it",
        )


class ParamBlockBindingTimeAstTests(unittest.TestCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 4 (BINDING-TIME ORDERING). An AST census over the
    generator's own $ast.ParamBlock: no parameter DEFAULT expression may invoke
    Resolve-Path/Test-Path/Get-Item/Get-ChildItem/git/Join-Path (a default runs at BIND TIME,
    before this script's first statement -- see -RepoRoot's own round-4 comment), and no
    path-shaped parameter (-ClipPath/-RepoRoot/-AgentRoot/-OutFile) may carry a ValidatePattern or
    ValidateScript attribute (PowerShell's own binding-failure message echoes a rejected value
    verbatim, disclosing a path-shaped string before this file's own path-free refusal ever runs).
    Hash/filename-shaped parameters (-SourceCommit, -BuildManifestSha256, the .zip/.exe/.json
    name parameters) are exempt: a commit hash or a plain filename cannot itself carry a path.
    """

    def _param_census(self) -> dict:
        import json
        import tempfile

        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$text = [IO.File]::ReadAllText('{ATTRIBUTION_JOB}')\n"
            "$tokens = $null; $errors = $null\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)\n"
            "if ($errors.Count -gt 0) { throw ('PARSE_ERROR: ' + ($errors -join '; ')) }\n"
            "$pathParams = @('ClipPath', 'RepoRoot', 'AgentRoot', 'OutFile')\n"
            "$forbiddenDefaultNames = @('Resolve-Path', 'Test-Path', 'Get-Item', 'Get-ChildItem', 'git', 'Join-Path')\n"
            "$violations = [System.Collections.Generic.List[string]]::new()\n"
            "foreach ($p in $ast.ParamBlock.Parameters) {\n"
            "    $name = $p.Name.VariablePath.UserPath\n"
            "    if ($null -ne $p.DefaultValue) {\n"
            "        $commands = $p.DefaultValue.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)\n"
            "        foreach ($c in $commands) {\n"
            "            $cn = $c.GetCommandName()\n"
            "            if ($null -ne $cn -and ($forbiddenDefaultNames -icontains $cn)) { [void]$violations.Add(\"DEFAULT_IO:${name}:${cn}\") }\n"
            "        }\n"
            "    }\n"
            "    foreach ($attr in $p.Attributes) {\n"
            "        if ($attr -isnot [System.Management.Automation.Language.AttributeAst]) { continue }\n"
            "        $attrName = $attr.TypeName.Name\n"
            "        if ((@('ValidatePattern', 'ValidateScript') -icontains $attrName) -and ($pathParams -icontains $name)) {\n"
            "            [void]$violations.Add(\"VALIDATOR:${name}:${attrName}\")\n"
            "        }\n"
            "    }\n"
            "}\n"
            "$violations | ConvertTo-Json -Compress\n"
        )
        with tempfile.TemporaryDirectory(prefix="attr3-param-ast-") as tmp:
            script_path = Path(tmp) / "param_census.ps1"
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        raw = proc.stdout.strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]

    def test_no_param_default_does_io_and_no_path_param_carries_a_validator(self) -> None:
        violations = self._param_census()
        self.assertEqual(violations, [], f"param block violations: {violations}")


class RetiredCompileJobTests(unittest.TestCase):
    """The old route must refuse, and say where to go instead."""

    def setUp(self) -> None:
        self.text = _read(RETIRED_COMPILE_JOB)

    def test_refuses_to_emit(self) -> None:
        self.assertIn("is RETIRED and emits nothing", self.text)

    def test_refuses_before_writing_or_archiving_anything(self) -> None:
        refusal = self.text.index('throw @"')
        archive = self.text.index("git -C $RepoRoot archive")
        write = self.text.index("[IO.File]::WriteAllText($jobPath")
        self.assertLess(refusal, archive)
        self.assertLess(refusal, write)

    def test_names_the_replacement_route(self) -> None:
        for replacement in (
            "playback-attr-3-cuda-dll-job.ps1",
            "playback-attr-3-cuda-assemble.ps1",
            "playback-attr-3-cuda-stage-job.ps1",
            "playback-attr-3-cuda.md",
        ):
            with self.subTest(replacement=replacement):
                self.assertIn(replacement, self.text)


class RunbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read(RUNBOOK)

    def test_documents_the_route_in_order(self) -> None:
        positions = [
            self.text.index("Toolchain probe first"),
            self.text.index("playback-attr-3-cuda-dll-job.ps1"),
            self.text.index("playback-attr-3-cuda-assemble.ps1"),
            self.text.index("playback-attr-3-cuda-stage-job.ps1"),
            self.text.index("Attribution job (owner clip, id-only)"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_carries_the_trap_paragraph(self) -> None:
        lowered = self.text.lower()
        self.assertIn("a precedent script is not a precedent result", lowered)
        self.assertIn("result.json", lowered)
        self.assertIn("toolchain probe receipt", lowered)

    def _fixture_rehearsal_section(self) -> str:
        # sol, PR #139: section 4b must run exactly as printed in a workspace whose path
        # contains spaces -- quoted path placeholders and two DISTINCT job ids.
        start = self.text.index("## 4b. Fixture rehearsal")
        end = self.text.index("\n## ", start + 1)
        return self.text[start:end]

    def test_fixture_rehearsal_job_ids_are_distinct(self) -> None:
        section = self._fixture_rehearsal_section()
        self.assertIn("<stageJobId>", section)
        self.assertIn("<attrJobId>", section)
        # The old ambiguous placeholder must not survive: a reader copying <jobId> into both
        # submissions hits UMRUN_JOBID_IN_USE on the second one.
        self.assertNotIn("<jobId>", section)
        code_lines = [
            line
            for line in section.splitlines()
            if not line.strip().startswith("#")
        ]
        job_id_args = re.findall(r"-JobId (\S+)", "\n".join(code_lines))
        self.assertEqual(len(job_id_args), 2, "expected exactly two -JobId submissions in 4b")
        self.assertNotEqual(
            job_id_args[0], job_id_args[1], "the two -JobId values in 4b must differ"
        )

    def test_fixture_rehearsal_path_placeholders_are_quoted(self) -> None:
        section = self._fixture_rehearsal_section()
        for flag in ("-FixturePath", "-OutDir", "-ScriptPath", "-SideFile", "-OutFile"):
            with self.subTest(flag=flag):
                self.assertIn(
                    f'{flag} "',
                    section,
                    f"{flag}'s path argument is not double-quoted in runbook 4b, so a "
                    "workspace path containing spaces breaks argument parsing",
                )

    def test_says_the_old_route_is_retired(self) -> None:
        self.assertIn("retired and refuses to emit", self.text.lower())


class NoFootageTokensTests(unittest.TestCase):
    """None of the new scripts may name, glob or resolve footage, or sweep the agent cache.

    NA-4's id-addressed-consumer route admits a real clip only by RESOLVING an owner-clip id
    through tools/gates/resolve_consented_clip.py against the frozen consent table
    (ATTR3-FOOTAGE-BIND-1); the attribution job never takes a caller-typed path for one. The
    build-route scripts have no business knowing footage exists, and a cache sweep is how an
    id-to-file resolver gets reintroduced by accident.
    """

    # Assembled, not written literally: see the module docstring.
    MEDIA_EXTENSIONS = tuple("." + suffix for suffix in ("mlv", "raw", "dng", "cdng", "mp4", "mov"))
    FORBIDDEN_SUBSTRINGS = ("clip",) + MEDIA_EXTENSIONS

    def test_no_footage_or_media_extension_tokens(self) -> None:
        for path in NEW_SCRIPTS:
            lowered = _read(path).lower()
            for token in self.FORBIDDEN_SUBSTRINGS:
                with self.subTest(script=path.name, token=token):
                    self.assertNotIn(token, lowered)

    def test_no_cache_globbing(self) -> None:
        for path in NEW_SCRIPTS:
            for number, line in enumerate(_read(path).splitlines(), start=1):
                if "cache" not in line.lower():
                    continue
                with self.subTest(script=path.name, line=number):
                    self.assertNotIn("*", line, f"{path.name}:{number} globs over a cache path: {line.strip()}")


class SharedNamingContractTests(unittest.TestCase):
    """One definition of the names three surfaces must agree on without exchanging state."""

    def test_module_exports_the_naming_helper(self) -> None:
        text = _read(SHARED_MODULE)
        export = text[text.index("Export-ModuleMember") :]
        for name in ("Get-AttrCudaArtifactNames", "New-AttrCudaBuildInfoHeader"):
            with self.subTest(name=name):
                self.assertIn(name, export)

    def test_consumers_import_the_module_rather_than_restating_the_convention(self) -> None:
        for path in (ASSEMBLE, STAGE_JOB):
            with self.subTest(script=path.name):
                self.assertIn("Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1')", _read(path))

    def test_module_names_match_what_the_attribution_job_derives(self) -> None:
        module = _read(SHARED_MODULE)
        attribution = _read(ATTRIBUTION_JOB)
        for fragment in (
            '"MLVApp-playback-attr-3-cuda-$shortSha.exe"',
            '"igpu_recon_cuda-playback-attr-3-cuda-$shortSha.dll"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, module)
                self.assertIn(fragment, attribution)



class FixtureRehearsalVisibilityTests(unittest.TestCase):
    """sol PR #137 r1: a rehearsal must be unmistakable in EVERY reader-facing output."""

    GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"

    def setUp(self) -> None:
        self.text = self.GENERATOR.read_text(encoding="utf-8")
        self.template = self.text[self.text.index("$template = @'"):]

    def test_the_fixture_arm_of_the_clip_id_pattern_is_case_sensitive(self) -> None:
        # ValidatePattern is case-insensitive by default, so the fixture ids carry (?-i:...);
        # otherwise TINY_DUAL_ISO validates while the membership test calls it an owner clip.
        line = [l for l in self.text.splitlines() if "ValidatePattern" in l and "dual_iso" in l]
        self.assertEqual(len(line), 1, line)
        self.assertIn("(?-i:", line[0])

    def test_every_reader_facing_output_carries_the_flag(self) -> None:
        for artifact in ("summary.json", "provenance.json", "evidence-manifest.json", "artifact-index.json"):
            with self.subTest(artifact=artifact):
                index = self.template.index(artifact)
                window = self.template[max(0, index - 3000):index]
                self.assertIn("fixtureRehearsal", window, f"{artifact} is written without the flag nearby")

    def test_the_success_path_writes_a_summary_and_a_distinct_result_verb(self) -> None:
        self.assertIn("FIXTURE_REHEARSAL_CAPTURED", self.template)
        self.assertIn("FIXTURE_REHEARSAL=$FixtureRehearsal", self.template)
        # the success path writes summary.json too, not only the failure paths
        tail = self.template[self.template.index("$resultVerb ="):] if "$resultVerb =" in self.template else ""
        self.assertTrue(tail, "no success result verb found")
        # CUDA-PLAYBACK-CONTACT-SHEET-1 r1b widened this window (2500 -> 6000), r1c widened it
        # again (6000 -> 7000), r1d widened it again (7000 -> 10000): the contact-sheet compose
        # step (probe Python/Pillow/numpy via a small probe script file rather than an inline
        # -c program, run the composer with its host/GPU/scale identity and quoted arguments,
        # publish or record a typed unavailable marker plus any timed-out-child warning) now
        # sits between the two, and is legitimately that long.
        self.assertIn("summary.json", self.template[self.template.index("artifact-index.v1") - 10000:])


if __name__ == "__main__":
    unittest.main()
