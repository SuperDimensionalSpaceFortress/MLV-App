"""EXPORT-PRORES-FFMPEG-EXE-PACKAGE-1: every venue package and release artifact carries ffmpeg.exe where the export job looks for it.

Every ProRes export venue leg ended VENUE_TOOLING / EXPORT_TOOLING_ABSENT (exit 45): tools/profiling/bachelor/export-stage-job.ps1
requires ffmpeg.exe beside MLVApp.exe in the expanded base package, and the package is the release dir that
tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1 zips, whose only FFmpeg step mirrored av*/sw* DLLs from an optional donor dir
the nightly never passes. tools/build-release.ps1 had the same gap. The hosted Windows release never had it: .github/workflows/Windows.yml
installs ffmpeg.exe from the sha-pinned in-repo payload with tools/repo_hygiene/extract_vendored_native_payload --verify-installed.

The fix installs the same payload, through Install-PinnedVendoredPayload (tools/release-runtime-admission.psm1), on every assemble and
every build-release run. This guard reads the job's expectation from the job itself (never re-typed here), binds it to the manifest
record, and checks through the PowerShell AST that each script installs into the exe's own folder, unconditionally (no if, loop, try,
function or donor branch around the call) and before the package zip or the manifest is written. No build and no venue.

The behavioural cases (Windows with py.exe) run the real helper on the real in-repo archive into a temp dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / "tools" / "profiling" / "bachelor" / "export-stage-job.ps1"
ASSEMBLER = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1"
ARTIFACTS = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
BUILD_RELEASE = ROOT / "tools" / "build-release.ps1"
HELPER_MODULE = ROOT / "tools" / "release-runtime-admission.psm1"
MANIFEST = ROOT / "tools" / "gates" / "vendored-native-payloads.json"
WORKFLOW = ROOT / ".github" / "workflows" / "Windows.yml"
PAYLOAD_ID = "ffmpeg-windows-x86_64"
HELPER = "Install-PinnedVendoredPayload"
PWSH = shutil.which("pwsh")

# Lists every CommandAst of one file with its offset, its enclosing AST node types and its named arguments (the static value
# of a constant string, the variable name of a plain variable, and the source text either way). Test code: it launches nothing.
AST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:FFX_AST_FILE, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "parse errors in $($env:FFX_AST_FILE): $($errors[0].Message)" }
$rows = foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $anc = @(); $p = $c.Parent
    while ($p) { $anc += $p.GetType().Name; $p = $p.Parent }
    $named = [ordered]@{}
    $els = $c.CommandElements
    for ($i = 1; $i -lt $els.Count; $i++) {
        $e = $els[$i]
        if ($e -is [System.Management.Automation.Language.CommandParameterAst]) {
            $arg = $e.Argument
            if (-not $arg -and $i + 1 -lt $els.Count -and $els[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $i++; $arg = $els[$i] }
            $named[$e.ParameterName] = [ordered]@{
                kind = $(if ($arg) { $arg.GetType().Name } else { 'Switch' })
                text = $(if ($arg) { $arg.Extent.Text } else { '' })
                value = $(if ($arg -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $arg.Value } else { $null })
                variable = $(if ($arg -is [System.Management.Automation.Language.VariableExpressionAst]) { $arg.VariablePath.UserPath } else { $null })
            }
        }
    }
    [ordered]@{ name = $c.GetCommandName(); offset = $c.Extent.StartOffset; line = $c.Extent.StartLineNumber; text = $c.Extent.Text; ancestors = $anc; named = $named }
}
$flowTypes = @([System.Management.Automation.Language.ExitStatementAst], [System.Management.Automation.Language.ReturnStatementAst],
               [System.Management.Automation.Language.ThrowStatementAst], [System.Management.Automation.Language.BreakStatementAst],
               [System.Management.Automation.Language.ContinueStatementAst])
$flow = foreach ($s in $ast.FindAll({ param($n) $flowTypes | Where-Object { $n -is $_ } }, $true)) {
    $anc = @(); $p = $s.Parent
    while ($p) { $anc += $p.GetType().Name; $p = $p.Parent }
    [ordered]@{ kind = $s.GetType().Name; offset = $s.Extent.StartOffset; line = $s.Extent.StartLineNumber; text = $s.Extent.Text; ancestors = $anc }
}
ConvertTo-Json -InputObject ([ordered]@{ commands = @($rows); flow = @($flow) }) -Depth 7 -Compress
"""

# Node types that make a statement conditional, repeated, deferred or swallowed: the install must sit under none of them.
GUARDING_NODES = {
    "IfStatementAst", "SwitchStatementAst", "ForEachStatementAst", "ForStatementAst", "WhileStatementAst", "DoWhileStatementAst",
    "DoUntilStatementAst", "TryStatementAst", "TrapStatementAst", "FunctionDefinitionAst", "ScriptBlockExpressionAst",
    "BinaryExpressionAst", "TernaryExpressionAst", "PipelineChainAst", "SubExpressionAst",
}

# A statement that ends the script (exit, return, throw, break, continue) ahead of the install is fine only under one of these: a
# conditional, a loop, a handler or a function body (adversary 6627beef-A3: a top-level `exit 0` before the install passed the guard).
CONTAINED_FLOW = GUARDING_NODES | {"CatchClauseAst", "StatementBlockAst"}


def _normalised(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


_SCANS: dict[tuple[str, int, int], dict] = {}


def _scan(path: Path) -> dict:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _SCANS:
        env = dict(os.environ, FFX_AST_FILE=str(path))
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", AST_SCRIPT],
                              capture_output=True, text=True, timeout=120, env=env)
        if proc.returncode != 0:
            raise AssertionError(f"AST scan of {path} failed: {proc.stdout}{proc.stderr}")
        _SCANS[key] = json.loads(proc.stdout)
    return _SCANS[key]


def _commands(path: Path) -> list[dict]:
    return _scan(path)["commands"]


def _flow(path: Path) -> list[dict]:
    return _scan(path)["flow"]


def _job_expectation() -> tuple[str, str]:
    """(base exe name, ffmpeg file name) as export-stage-job.ps1 itself declares them, with the beside-the-exe location proven."""
    text = _normalised(JOB)
    exe = re.search(r"^\$BasePackageExeName\s*=\s*'([^']+)'\s*$", text, re.M)
    assert exe, "export-stage-job.ps1 no longer declares $BasePackageExeName = '<name>'"
    assert re.search(r"\$baseExe\s*=\s*Get-ChildItem\b[^\n]*-Filter\s+\$BasePackageExeName\b", text), "the job no longer finds the base exe by $BasePackageExeName"
    assert re.search(r"\$pkgDir\s*=\s*\$baseExe\.Directory\.FullName\b", text), "the job's $pkgDir is no longer the base exe's own folder"
    ff = re.search(r"\$ffmpegPath\s*=\s*Join-Path\s+\$pkgDir\s+'([^']+)'", text)
    assert ff, "the job no longer looks for ffmpeg as Join-Path $pkgDir '<name>'"
    assert "'--rendered-ffmpeg', $ffmpegPath" in text, "the job no longer hands that ffmpeg to the app"
    return exe.group(1), ff.group(1)


def _ffmpeg_record() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = [r for r in manifest["payloads"] if r.get("id") == PAYLOAD_ID]
    assert len(rows) == 1, f"{PAYLOAD_ID} must be exactly one manifest record"
    return rows[0]


def _install_calls(commands: list[dict]) -> list[dict]:
    return [c for c in commands if (c["name"] or "").lower() == HELPER.lower()]


class JobAndManifestAgreeTests(unittest.TestCase):
    """The name and location are read from the job; the manifest's selected member must produce exactly that file."""

    def test_the_manifest_record_installs_the_file_the_job_looks_for(self) -> None:
        _, ffmpeg_name = _job_expectation()
        record = _ffmpeg_record()
        outputs = [m.get("output_name") for m in record["selected_members"]]
        self.assertIn(ffmpeg_name, outputs, f"{PAYLOAD_ID} installs {outputs}, the job looks for {ffmpeg_name}")
        member = next(m for m in record["selected_members"] if m.get("output_name") == ffmpeg_name)
        self.assertEqual(member["binary"], {"format": "pe", "machine": "x86_64"})
        self.assertRegex(member["sha256"], r"^[0-9a-f]{64}$")

    def test_the_package_exe_the_assembler_builds_is_the_base_exe_the_job_finds(self) -> None:
        # The ffmpeg must land in the folder of the exe the job finds; the assembler's package root is the release dir that holds
        # names.packageExeName, so the two names must agree.
        exe_name, _ = _job_expectation()
        declared = re.search(r"^\s*packageExeName\s*=\s*'([^']+)'", _normalised(ARTIFACTS), re.M)
        self.assertIsNotNone(declared, "AttrCudaArtifacts.psm1 no longer declares packageExeName")
        self.assertEqual(declared.group(1), exe_name)
        text = _normalised(ASSEMBLER)
        self.assertIn("$releaseExe = Join-Path $releaseDir $names.packageExeName", text)
        self.assertIn("Compress-Archive -Path (Join-Path $releaseDir '*') -DestinationPath $stagedPkgPath", text)

    def test_the_hosted_release_runs_the_same_install(self) -> None:
        record = _ffmpeg_record()
        archive = PurePosixPath(record["path"]).name
        workflow = _normalised(WORKFLOW)
        self.assertRegex(workflow, rf"extract_vendored_native_payload[^\n]*--payload-id {re.escape(PAYLOAD_ID)} --archive-reference {re.escape(archive)}[^\n]*--verify-installed")


@unittest.skipUnless(PWSH, "needs pwsh for the PowerShell AST")
class ScriptsInstallUnconditionallyTests(unittest.TestCase):
    # adversary 32bf39a9-A4: the output dir is pinned EXACTLY (a substring match accepted (Join-Path $releaseDir 'other')).
    # adversary 6627beef-A2: (Resolve-Path -LiteralPath $dest).Path threw (dest renamed) before the helper, past exit 9; the provider
    # path is resolved against the PowerShell location without requiring the folder, so nothing throws before the helper.
    EXACT_OUTPUT_DIR = {"releaseDir": "$releaseDir", "dest": "$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($dest)"}

    def _assert_one_unconditional_install(self, path: Path, output_variable: str) -> dict:
        record = _ffmpeg_record()
        calls = _install_calls(_commands(path))
        self.assertEqual(len(calls), 1, f"{path.name} must call {HELPER} exactly once, found {len(calls)}")
        call = calls[0]
        named = {k.lower(): v for k, v in call["named"].items()}
        self.assertEqual(named.get("payloadid", {}).get("value"), PAYLOAD_ID, call["text"])
        self.assertEqual(named.get("archivereference", {}).get("value"), PurePosixPath(record["path"]).name, call["text"])
        self.assertEqual(named.get("outputdir", {}).get("text", ""), self.EXACT_OUTPUT_DIR[output_variable], call["text"])
        self.assertIn("reporoot", named, call["text"])
        guarded = sorted(set(call["ancestors"]) & GUARDING_NODES)
        self.assertEqual(guarded, [], f"{path.name}:{call['line']} the install sits under {guarded}: it must run on every build")
        self.assertEqual(call["ancestors"].count("ScriptBlockAst"), 1, f"{path.name}:{call['line']} the install is not a top-level statement")
        early = [s for s in _flow(path) if s["offset"] < call["offset"] and not (set(s["ancestors"]) & CONTAINED_FLOW)]
        self.assertEqual(early, [], f"{path.name}: an unconditional {[(s['line'], s['text']) for s in early]} ends the script before the install")
        return call

    def test_the_assembler_installs_into_the_release_dir_before_the_launch_probe_and_the_zip(self) -> None:
        commands = _commands(ASSEMBLER)
        call = self._assert_one_unconditional_install(ASSEMBLER, "releaseDir")
        self.assertEqual(call["named"]["RepoRoot"]["variable"], "RepoRoot", "the install must use the git checkout $RepoRoot, not the archived $SourceTree")
        zips = [c for c in commands if (c["name"] or "") == "Compress-Archive" and "$releaseDir" in c["named"].get("Path", {}).get("text", "")]
        self.assertEqual(len(zips), 1, "expected one Compress-Archive of $releaseDir")
        self.assertLess(call["offset"], zips[0]["offset"], "ffmpeg.exe must be installed before the release dir is zipped")
        probes = [c for c in commands if (c["name"] or "") == "Start-Process" and c["named"].get("FilePath", {}).get("variable") == "releaseExe"]
        self.assertEqual(len(probes), 1)
        self.assertLess(call["offset"], probes[0]["offset"], "install before the launch probe, so the probe runs the shipped layout")
        text = _normalised(ASSEMBLER)
        self.assertLess(text.index("$releaseDir = Join-Path $buildReleaseDir 'release'"), text.index(HELPER + " -RepoRoot"))
        self.assertLess(text.index("$StepLog['dllPairDeploy'] = 0"), text.index(HELPER + " -RepoRoot"))
        imports = [c for c in commands if (c["name"] or "") == "Import-Module" and "release-runtime-admission.psm1" in c["text"]]
        self.assertEqual(len(imports), 1, "the assembler must import the module that defines the helper")
        self.assertLess(imports[0]["offset"], call["offset"])

    def test_the_assembler_fails_typed_with_its_own_exit_code_and_records_the_file(self) -> None:
        text = _normalised(ASSEMBLER)
        pairs = re.findall(r"Complete-Failed (\d+) '(\w+)'", text)
        steps_for_28 = {step for code, step in pairs if code == "28"}
        self.assertEqual(steps_for_28, {"ffmpegExeInstall"}, "exit 28 belongs to the ffmpeg.exe install alone")
        self.assertEqual({code for code, step in pairs if step == "ffmpegExeInstall"}, {"28"})
        self.assertIn("if (-not $ffmpegInstall.ok) { Complete-Failed 28 'ffmpegExeInstall'", text)
        header = text[: text.index("[CmdletBinding()]")]
        self.assertRegex(header, r"28 ffmpeg\.exe install")
        manifest_block = text[text.index("$buildManifest = [ordered]@{"): text.index("assembledAtUtc = ")]
        self.assertRegex(manifest_block, r"\n\s*ffmpegExeDeployed = \$true\n")
        self.assertRegex(manifest_block, r"\n\s*ffmpegExe = \$ffmpegExe\n")
        record_block = text[text.index("$ffmpegExe = [ordered]@{"):]
        record_block = record_block[: record_block.index("\n}") + 2]
        for key in ("payloadId = $ffmpegPayloadId", "bytes = [long](Get-Item -LiteralPath $ffmpegExePath", "sha256 = Get-ShaLower $ffmpegExePath"):
            self.assertIn(key, record_block)

    def test_build_release_installs_into_dest_before_the_launch_probe_and_the_manifest(self) -> None:
        commands = _commands(BUILD_RELEASE)
        call = self._assert_one_unconditional_install(BUILD_RELEASE, "dest")
        self.assertEqual(call["named"]["RepoRoot"]["variable"], "SourceRoot")
        writes = [c for c in commands if (c["name"] or "") == "Set-Content" and "build-manifest.json" in c["text"]]
        self.assertEqual(len(writes), 1)
        self.assertLess(call["offset"], writes[0]["offset"], "ffmpeg.exe must be installed before build-manifest.json is written")
        probes = [c for c in commands if (c["name"] or "") == "Start-Process" and c["named"].get("FilePath", {}).get("variable") == "destExe"]
        self.assertEqual(len(probes), 1)
        self.assertLess(call["offset"], probes[0]["offset"])
        text = _normalised(BUILD_RELEASE)
        self.assertLess(text.index("$dest = Join-Path $OutRoot $tag"), text.index(HELPER + " -RepoRoot"))
        self.assertIn("exit 9", text)
        self.assertRegex(text[: text.index("param(")], r"9=ffmpeg\.exe")
        self.assertIn("$deploy.ffmpegExe = [ordered]@{", text)

    def test_every_ffmpeg_step_that_can_throw_is_caught_and_typed(self) -> None:
        # adversary 32bf39a9-A2: a locked install log or a FileShare.None handle on the installed ffmpeg.exe threw past the typed failure
        # (exit 28 / exit 9). Every read of the installed file and the log write sit under a try.
        for path, needles in (
            (ASSEMBLER, ("Set-Content -LiteralPath (Join-Path $logDir 'ffmpeg-exe-install.log')", "Get-Item -LiteralPath $ffmpegExePath",
                         "Get-ShaLower $ffmpegExePath")),
            (BUILD_RELEASE, ("Get-Item -LiteralPath $ffmpegExePath", "Get-FileHash -LiteralPath $ffmpegExePath")),
        ):
            commands = _commands(path)
            for needle in needles:
                with self.subTest(script=path.name, site=needle):
                    sites = [c for c in commands if c["text"].startswith(needle.split(" ", 1)[0]) and needle in c["text"]]
                    self.assertTrue(sites, f"{needle} not found in {path.name}")
                    for site in sites:
                        self.assertIn("TryStatementAst", site["ancestors"], f"{path.name}:{site['line']} {needle} can throw past the typed failure")

    def test_a_timeout_reports_the_kill_only_when_it_is_confirmed(self) -> None:
        # adversary 32bf39a9-A3: a Kill that threw and a wait that did not see the exit were discarded and the reason still said "killed".
        text = _normalised(HELPER_MODULE)
        self.assertIn("function Stop-PinnedPayloadChild {", text)
        body = text[text.index(f"function {HELPER} {{"):]
        self.assertIn("$stop = Stop-PinnedPayloadChild -Process $process", body)
        # adversary 6627beef-A1: confirmation is the tree's (no kill error AND the exit seen), never the root's exit alone.
        self.assertIn("$result.killConfirmed = [bool]$stop.confirmed", body)
        self.assertIn("confirmed = ($exited -and -not $killError)", text)
        self.assertIn("the kill was NOT confirmed", body)
        release = _normalised(BUILD_RELEASE)
        self.assertNotIn("try { $p.Kill() } catch {}", release)
        self.assertIn("killConfirmed    = $probeKillConfirmed", release)
        self.assertIn("$probeKillConfirmed = [bool]$probeStop.confirmed", release)
        assembler = _normalised(ASSEMBLER)
        self.assertNotIn("try { $probe.Kill() } catch { }", assembler)
        self.assertIn("if ($probeStop.confirmed) {", assembler)
        self.assertNotIn("$probeStop.exited", release + assembler)

    def test_the_helper_runs_the_extractor_with_verify_installed_bounded(self) -> None:
        text = _normalised(HELPER_MODULE)
        start = text.index(f"function {HELPER} {{")
        body = text[start: text.index("\nExport-ModuleMember", start)]
        argv = re.search(r"foreach \(\$argument in @\(([^\n]+)\)\)", body)
        self.assertIsNotNone(argv, "the helper's extractor argv is no longer one literal list")
        items = re.findall(r"'([^']*)'|(\$\w+)", argv.group(1))
        flat = [a or b for a, b in items]
        self.assertEqual(flat[:3], ["-3", "-m", "tools.repo_hygiene.extract_vendored_native_payload"])
        self.assertNotIn("-I", flat, "-I drops the working directory from sys.path and with it the tools package")
        for flag, value in (("--repo-root", "$RepoRoot"), ("--payload-id", "$PayloadId"), ("--archive-reference", "$ArchiveReference"), ("--output-dir", "$OutputDir")):
            self.assertEqual(flat[flat.index(flag) + 1], value)
        self.assertEqual(flat[-1], "--verify-installed")
        self.assertIn("$psi.WorkingDirectory = $RepoRoot", body)
        self.assertIn("$process.WaitForExit($TimeoutSec * 1000)", body)
        stop = text[text.index("function Stop-PinnedPayloadChild {"): text.index(f"function {HELPER} {{")]
        self.assertIn("$null = $Process.Kill($true)", stop, "the timeout kill must take the whole tree")
        self.assertIn(f"Export-ModuleMember -Function Test-ReviewedReleaseRuntimeExtraName, {HELPER}, Stop-PinnedPayloadChild", text)
        # adversary 32bf39a9-A1: every inherited GIT_* variable is dropped from the child's environment.
        self.assertIn("if ([string]$name -like 'GIT_*') { [void]$psi.Environment.Remove([string]$name) }", body)

    def test_an_install_into_another_folder_is_refused_by_the_guard(self) -> None:
        # adversary 32bf39a9-A4: (Join-Path $releaseDir 'other') passed the substring check; the exact pin must refuse it.
        text = ASSEMBLER.read_text(encoding="utf-8")
        needle = "-OutputDir $releaseDir -TimeoutSec"
        self.assertEqual(text.count(needle), 1)
        with tempfile.TemporaryDirectory(prefix="ffx-mutant-") as tmp:
            mutant = Path(tmp) / ASSEMBLER.name
            mutant.write_text(text.replace(needle, "-OutputDir (Join-Path $releaseDir 'other') -TimeoutSec"), encoding="utf-8")
            with self.assertRaises(AssertionError):
                self._assert_one_unconditional_install(mutant, "releaseDir")

    def test_an_unconditional_exit_before_the_install_is_refused_by_the_guard(self) -> None:
        # adversary 6627beef-A3: a top-level `exit 0` right before the install kept every other assertion green.
        for path, anchor, variable in ((ASSEMBLER, "$ffmpegPayloadId = 'ffmpeg-windows-x86_64'", "releaseDir"),
                                       (BUILD_RELEASE, "$ffmpegInstall = Install-PinnedVendoredPayload", "dest")):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(anchor), 1, anchor)
            for early in ("exit 0", "return", "throw 'stop'"):
                with self.subTest(script=path.name, statement=early), tempfile.TemporaryDirectory(prefix="ffx-mutant-") as tmp:
                    mutant = Path(tmp) / path.name
                    mutant.write_text(text.replace(anchor, early + "\n" + anchor), encoding="utf-8")
                    with self.assertRaises(AssertionError):
                        self._assert_one_unconditional_install(mutant, variable)


@unittest.skipUnless(os.name == "nt" and PWSH and shutil.which("py"), "needs Windows, pwsh and py.exe")
class HelperBehaviourTests(unittest.TestCase):
    """The real helper on the real in-repo archive."""

    def _run(self, output_dir: str, archive_reference: str = "ffmpegWin64.zip", *, extra_env: dict | None = None, prelude: str = "") -> dict:
        module = str(HELPER_MODULE).replace("'", "''")
        script = (
            prelude
            + f"Import-Module '{module}' -Force\n"
            f"$r = {HELPER} -RepoRoot $env:FFX_REPO -PayloadId '{PAYLOAD_ID}' -ArchiveReference $env:FFX_ARCHIVE -OutputDir $env:FFX_OUT -TimeoutSec 600\n"
            "$r | Select-Object ok, reason, exitCode, timedOut, installed | ConvertTo-Json -Depth 4 -Compress\n"
        )
        env = dict(os.environ, FFX_REPO=str(ROOT), FFX_ARCHIVE=archive_reference, FFX_OUT=output_dir, **(extra_env or {}))
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=900, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ffx-layout-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_it_installs_the_pinned_bytes_where_the_job_looks(self) -> None:
        _, ffmpeg_name = _job_expectation()
        member = next(m for m in _ffmpeg_record()["selected_members"] if m["output_name"] == ffmpeg_name)
        out = self.tmp / "release dir with spaces"
        out.mkdir()
        result = self._run(str(out))
        self.assertTrue(result["ok"], result)
        installed = out / ffmpeg_name
        self.assertTrue(installed.is_file())
        digest = hashlib.sha256(installed.read_bytes()).hexdigest()
        self.assertEqual(digest, member["sha256"])
        self.assertEqual(installed.stat().st_size, member["bytes"])
        rows = result["installed"] if isinstance(result["installed"], list) else [result["installed"]]
        self.assertEqual([(r["name"], r["sha256"], r["bytes"]) for r in rows], [(ffmpeg_name, member["sha256"], member["bytes"])])
        self.assertEqual(sorted(p.name for p in out.iterdir()), [ffmpeg_name], "no transaction or staging debris may remain")

    def test_a_wrong_archive_reference_installs_nothing(self) -> None:
        out = self.tmp / "out"
        out.mkdir()
        result = self._run(str(out), archive_reference="notTheArchive.zip")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exitCode"], 1)
        self.assertIn("archive reference mismatch", result["reason"])
        self.assertEqual(list(out.iterdir()), [])

    def test_an_inherited_git_variable_never_reaches_the_extractor(self) -> None:
        # adversary 32bf39a9-A1: GIT_TRACE2_EVENT (and any GIT_*) survived into Python and its `git ls-files`, which appends to the named
        # file; aimed at a tracked script of the checkout, the install wrote into the checkout and still returned ok. The trace targets
        # here are temp files, so the test itself never writes into the repo.
        out = self.tmp / "out"
        out.mkdir()
        traces = {name: self.tmp / f"{name.lower()}.log" for name in ("GIT_TRACE2_EVENT", "GIT_TRACE", "GIT_TRACE2")}
        result = self._run(str(out), extra_env={name: str(path) for name, path in traces.items()})
        self.assertTrue(result["ok"], result)
        for name, path in traces.items():
            self.assertFalse(path.exists(), f"{name} reached a git child: {path} was written")

    def test_a_hash_that_throws_returns_a_typed_failure_instead_of_throwing(self) -> None:
        # adversary 32bf39a9-A2: a FileShare.None handle on the installed ffmpeg.exe makes the helper's own hash throw. Get-FileHash is
        # shadowed by a global function that throws the same way, so the helper must return ok=false with the reason, not throw.
        out = self.tmp / "out"
        out.mkdir()
        result = self._run(str(out), prelude="function global:Get-FileHash { throw 'simulated FileShare.None lock on the installed file' }\n")
        self.assertFalse(result["ok"], result)
        self.assertIn("simulated FileShare.None lock", result["reason"])

    def _stop(self, kill: str, wait: str) -> dict:
        module = str(HELPER_MODULE).replace("'", "''")
        script = (
            f"$m = Import-Module '{module}' -Force -PassThru\n"
            "$fake = [pscustomobject]@{ Id = 4242 }\n"
            f"$fake | Add-Member -MemberType ScriptMethod -Name Kill -Value {{ {kill} }}\n"
            f"$fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ {wait} }}\n"
            "$r = & $m { param($p) Stop-PinnedPayloadChild -Process $p } $fake\n"
            "$r | ConvertTo-Json -Compress\n"
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_a_kill_that_throws_and_a_wait_that_fails_are_reported_unconfirmed(self) -> None:
        # adversary 32bf39a9-A3: Kill(true) threw Win32Exception(5) and the confirming wait returned false; both were discarded.
        stop = self._stop("throw [System.ComponentModel.Win32Exception]::new(5)", "$false")
        self.assertFalse(stop["exited"], stop)
        self.assertFalse(stop["confirmed"], stop)
        self.assertTrue(stop["killError"], stop)

    def test_a_tree_kill_that_throws_is_unconfirmed_even_when_the_root_exits(self) -> None:
        # adversary 6627beef-A1: Kill(true) ended the root but threw (a descendant could not be terminated) and the root's wait returned
        # true; the tree must still read as NOT confirmed, since a descendant may survive.
        stop = self._stop("throw [System.AggregateException]::new('a git descendant could not be terminated')", "$true")
        self.assertTrue(stop["exited"], stop)
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn("descendant", stop["killError"])
        clean = self._stop("$null", "$true")
        self.assertTrue(clean["confirmed"], clean)

    def test_a_path_that_is_not_fully_qualified_is_refused_before_any_child(self) -> None:
        for bad in ("relative\\out", "C:drive-relative", "\\rooted-no-drive"):
            with self.subTest(path=bad):
                result = self._run(bad)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["exitCode"])
                self.assertIn("not a fully qualified path", result["reason"])


if __name__ == "__main__":
    unittest.main()
