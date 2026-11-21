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
import sys
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


# PR 862 r10 (Grok G4(b)): ONE decision for every capability this file cannot get. Off Windows CI a missing capability skips with
# its reason; on Windows CI (os.name == "nt" and CI set non-empty) it FAILS, since a skip there would silently hide a behavioural run
# the hosted Windows shards are expected to make. Read at the moment of the skip, never cached at import.
CAPABILITY_FAILURE_MARKER = "WINDOWS_CI_CAPABILITY_MISSING"


def _capability_missing(reason: str) -> None:
    if os.name == "nt" and os.environ.get("CI"):
        raise AssertionError(f"{CAPABILITY_FAILURE_MARKER}: {reason} (a skip on Windows CI would hide this behavioural run)")
    raise unittest.SkipTest(reason)


def _requires(available: bool, reason: str):
    """Class gate: without the capability the whole class goes through _capability_missing (skip, or a failure on Windows CI)."""
    def gate(cls):
        if not available:
            def setUpClass(klass) -> None:  # noqa: N802 - unittest's hook name
                _capability_missing(reason)
            cls.setUpClass = classmethod(setUpClass)
        return cls
    return gate

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


@_requires(bool(PWSH), "needs pwsh for the PowerShell AST")
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
        # PR 862 r10 (the split, sol r9 B1 caller build-release.ps1:247): the kill is root-only, so the success text says the ROOT was
        # killed and the tree was SEEN gone; and a probe stop that is NOT confirmed is a typed FAIL with its own non-zero exit, never a
        # WARN (a confirmed-killed timeout keeps its WARN).
        self.assertIn("its root was killed (root-only kill) and its process tree was seen gone", body)
        self.assertNotIn("its process tree was killed", body)
        self.assertIn("$probeStopUnconfirmed = ($probeTimedOut -and -not $probeKillConfirmed)", release)
        self.assertRegex(release, r"\} elseif \(\$probeStopUnconfirmed\) \{\n\s*Write-Host \"FAIL: launch probe timed out")
        self.assertIn("if ($probeStopUnconfirmed) { exit 10 }", release)
        self.assertIn("10=the launch probe timed out and its stop was NOT confirmed", release)

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
        # adversary ae560106-A2: the local install validates the named record alone, never the tracked-file census.
        self.assertEqual(flat.count("--record-only"), 1, "the local install must pass --record-only")
        # adversary 78d626ed-A1: the child is told the transaction folder its timeout recovery will open.
        self.assertEqual(flat[flat.index("--transaction-id") + 1], "$transactionId")
        self.assertIn("$psi.WorkingDirectory = $RepoRoot", body)
        self.assertIn("$process.WaitForExit($TimeoutSec * 1000)", body)
        stop = text[text.index("function Stop-PinnedPayloadChild {"): text.index(f"function {HELPER} {{")]
        # PR 862 r10 (sol r9 B1 / 8d24187c-A1, the split): the timeout kill is ROOT-ONLY, through the held handle. A recursive kill picks
        # descendants by .NET's parent predicate over LOCAL start times, which a DST fold turns onto an unrelated baseline survivor.
        self.assertEqual(re.findall(r"\.Kill\(", stop), [".Kill("], "exactly one kill in Stop-PinnedPayloadChild")
        self.assertIn("$null = $Process.Kill($false)", stop, "the timeout kill must take the root alone")
        self.assertNotRegex(text, r"(?i)Kill\(\s*\$?true\s*\)", "no recursive kill anywhere in the module")
        self.assertIn(f"Export-ModuleMember -Function Test-ReviewedReleaseRuntimeExtraName, {HELPER}, Stop-PinnedPayloadChild, Open-PinnedFileHold, Test-PinnedZipMember", text)
        # adversary 32bf39a9-A1: every inherited GIT_* variable is dropped from the child's environment.
        self.assertIn("if ([string]$name -like 'GIT_*') { [void]$psi.Environment.Remove([string]$name) }", body)

    def test_the_assembler_checks_the_ffmpeg_entry_inside_the_staged_zip_before_publishing(self) -> None:
        # adversary ae560106-A1: ffmpeg.exe deleted after its readback zipped without it, the zip hash validated that incomplete zip,
        # and build.json still said ffmpegExeDeployed. The entry inside the zip is checked and the published zip hash is the checked one.
        commands = _commands(ASSEMBLER)
        checks = [c for c in commands if (c["name"] or "") == "Test-PinnedZipMember"]
        self.assertEqual(len(checks), 1, "the assembler must check the staged zip's ffmpeg.exe entry exactly once")
        named = checks[0]["named"]
        self.assertEqual(named.get("ZipPath", {}).get("variable"), "stagedPkgPath")
        self.assertEqual(named.get("MemberName", {}).get("value"), "ffmpeg.exe")
        self.assertEqual(named.get("ExpectedSha256", {}).get("text"), "$ffmpegExe.sha256")
        self.assertEqual(named.get("ExpectedBytes", {}).get("text"), "$ffmpegExe.bytes")
        self.assertEqual(sorted(set(checks[0]["ancestors"]) & GUARDING_NODES), [], "the zip check must run on every assemble")
        zips = [c for c in commands if (c["name"] or "") == "Compress-Archive"]
        publishes = [c for c in commands if (c["name"] or "") == "Publish-AttrCudaFileCopy"]
        self.assertTrue(zips and publishes)
        self.assertLess(zips[0]["offset"], checks[0]["offset"])
        self.assertLess(checks[0]["offset"], min(p["offset"] for p in publishes), "check the zip before any .partial is published")
        text = _normalised(ASSEMBLER)
        self.assertIn("if (-not $pkgFfmpegCheck.ok) { Complete-Failed 28 'ffmpegExeInstall'", text)
        self.assertEqual(re.findall(r"^\$stagedPkgSha = .*$", text, re.M), ["$stagedPkgSha = $pkgFfmpegCheck.zipSha256"],
                         "the published zip hash must be the one read with the entry check, never a second read")

    def test_build_release_rehashes_and_holds_ffmpeg_while_the_manifest_is_written(self) -> None:
        # adversary ae560106-A1 sibling: a delete after the readback (the launch probe runs in between) left build-manifest.json claiming
        # the deploy. The file is re-hashed right before the manifest and held (FileShare.Read) until the write is done.
        commands = _commands(BUILD_RELEASE)
        holds = [c for c in commands if (c["name"] or "") == "Open-PinnedFileHold"]
        self.assertEqual(len(holds), 1)
        named = holds[0]["named"]
        self.assertEqual(named.get("Path", {}).get("variable"), "ffmpegExePath")
        self.assertEqual(named.get("ExpectedSha256", {}).get("text"), "$deploy.ffmpegExe.sha256")
        self.assertEqual(named.get("ExpectedBytes", {}).get("text"), "$deploy.ffmpegExe.bytes")
        self.assertEqual(sorted(set(holds[0]["ancestors"]) & GUARDING_NODES), [])
        probes = [c for c in commands if (c["name"] or "") == "Start-Process" and c["named"].get("FilePath", {}).get("variable") == "destExe"]
        writes = [c for c in commands if (c["name"] or "") == "Set-Content" and "build-manifest.json" in c["text"]]
        self.assertEqual(len(writes), 1)
        self.assertLess(probes[0]["offset"], holds[0]["offset"], "the re-hash must come after the launch probe")
        self.assertLess(holds[0]["offset"], writes[0]["offset"])
        self.assertIn("TryStatementAst", writes[0]["ancestors"], "the manifest write must sit in the try whose finally releases the hold")
        text = _normalised(BUILD_RELEASE)
        block = text[text.index("$ffmpegHold = Open-PinnedFileHold"): text.index('Write-Host "OK -> $destExe"')]
        self.assertRegex(block, r"if \(-not \$ffmpegHold\.ok\) \{\n[^\n]*\n\s*exit 9\n\}")
        self.assertRegex(block, r"\} finally \{\n\s*\$ffmpegHold\.stream\.Dispose\(\)\n\}")

    def test_both_publication_checks_open_the_pathname_itself_never_a_link_target(self) -> None:
        # sol r8 B1 / adversary efddecf6-A1: File.Open(..., FileShare.Read) followed a symbolic link, so the hold was on the link's
        # target while the pathname stayed replaceable. One shared no-follow open (FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ
        # only) serves both checks; the reparse test reads the HANDLE's attributes, so an open-then-swap cannot pass.
        text = _normalised(HELPER_MODULE)
        self.assertIn("public const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;", text)
        self.assertIn("public const uint FILE_SHARE_READ = 0x00000001;", text)
        self.assertIn("public const uint GENERIC_READ = 0x80000000;", text)
        self.assertEqual(text.count("CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, IntPtr.Zero)"), 1)
        self.assertIn("if (!GetFileInformationByHandle(handle, out info)) {", text)
        self.assertIn("if ((info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) {", text)
        self.assertIn('result.Code = "reparse-point";', text)
        # adversary 2c649ed9-A1: an ancestor reparse point is refused by the handle's own resolved path, never a path-based walk.
        self.assertIn("uint length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);", text)
        self.assertIn("if (!SamePathname(held, requested, requestedRoot)) {", text)
        self.assertIn('result.Code = "reparse-ancestor";', text)
        # adversary d0ae83d6-A1: below the root the comparison is ordinal over on-disk names (a case-sensitive directory can hold
        # a junction PIN beside the real pin), and the requested spelling never comes from GetLongPathNameW (it keeps caller case).
        same = text[text.index("public static bool SamePathname("):]
        same = same[: same.index("\n        }\n")]
        self.assertIn("requested.Substring(requestedRoot.TrimEnd('\\\\').Length), StringComparison.Ordinal);", same)
        self.assertNotIn("extern uint GetLongPathNameW", text)
        self.assertIn("IntPtr find = FindFirstFileW(WithLongPrefix(built + \"\\\\\" + part), out data);", text)
        self.assertNotRegex(text, r"\[System\.IO\.File\]::Open\(", "no publication check may fall back to a link-following open")
        for name in ("Open-PinnedFileHold", "Test-PinnedZipMember"):
            body = text[text.index(f"function {name} {{"):]
            body = body[: body.index("\n}\n")]
            self.assertEqual(body.count("Open-PinnedNoFollowStream -LiteralPath $"), 1, name)
        self.assertNotIn("Open-PinnedNoFollowStream,", text[text.index("Export-ModuleMember"):], "the shared open stays private")

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


@_requires(os.name == "nt" and bool(PWSH) and bool(shutil.which("py")), "needs Windows, pwsh and py.exe")
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

    @staticmethod
    def _row(pid: int, parent: int, utc: str) -> str:
        return f"[pscustomobject]@{{ id = {pid}; parentId = {parent}; startUtc = [datetime]::Parse('{utc}').ToUniversalTime() }}"

    def _stop(self, kill: str, wait: str, before: str = "@()", after: str = "@()", *, root_start: str | None = "2026-11-19T10:00:00Z",
              prelude: str = "", env: dict | None = None, later: str | None = None, confirm_ms: int = 3000, baseline: str = "@()") -> dict:
        # The process table is injected (before the kill, then every later listing). The fake root is pid 4242; its handle-read creation
        # time is root_start (None = the handle cannot read it). Fake ids are not multiples of 4, so no real Windows pid can match one.
        # baseline is the caller's pre-start listing (PR 862 r8, adversary 500e2892-A1); "$null" means none was taken.
        module = str(HELPER_MODULE).replace("'", "''")
        start = "" if root_start is None else f"; StartTime = [datetime]::Parse('{root_start}').ToUniversalTime()"
        script = (
            prelude
            + f"$m = Import-Module '{module}' -Force -PassThru\n"
            "$global:ffxListings = 0\n"
            "$global:ffxSec = @()\n"
            # The listing's own time bound (seconds) the stop passes on each call is recorded in .listingSec.
            # Listing 1 = before the kill, listing 2 = after, listing 3 on = later (the same as after unless given).
            f"$table = {{ $global:ffxListings++; $global:ffxSec += [string]$args[0]; if ($global:ffxListings -eq 1) {{ {before} }} "
            f"elseif ($global:ffxListings -eq 2) {{ {after} }} else {{ {after if later is None else later} }} }}\n"
            f"$fake = [pscustomobject]@{{ Id = 4242{start} }}\n"
            f"$fake | Add-Member -MemberType ScriptMethod -Name Kill -Value {{ {kill} }}\n"
            f"$fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ {wait} }}\n"
            f"$baseline = {baseline}\n"
            f"$r = & $m {{ param($p, $t, $b) Stop-PinnedPayloadChild -Process $p -Baseline $b -ProcessTable $t -ConfirmMs {int(confirm_ms)} }} $fake $table $baseline\n"
            "$r | Add-Member -NotePropertyName listingSec -NotePropertyValue ($global:ffxSec -join ',')\n"
            "$r | ConvertTo-Json -Compress\n"
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                              timeout=120, env=dict(os.environ, **(env or {})))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def _unrelated_process(self) -> subprocess.Popen:
        # A real process outside the tree under test; the stop must never kill it.
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        return bystander

    # The bystander's row, with its REAL creation time read through its own handle in the test script, so a per-pid identity check passes.
    _BYSTANDER_ROW = ("[pscustomobject]@{ id = [int]$env:FFX_U; parentId = 4242; startUtc = $global:ffxU }")
    _BYSTANDER_PRELUDE = "$global:ffxU = ([System.Diagnostics.Process]::GetProcessById([int]$env:FFX_U).StartTime.ToUniversalTime())\n"

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
        clean = self._stop("$null", "$true", before=f"@({self._row(4242, 1, '2026-11-19T10:00:00Z')})")
        self.assertTrue(clean["confirmed"], clean)

    def test_a_descendant_kill_true_missed_across_the_dst_fold_leaves_the_kill_unconfirmed(self) -> None:
        # adversary b5fce5a9-A1: py.exe started 06:59:59Z and its extractor 07:00:01Z on 2026-11-01 (the Central fall-back fold), so by
        # LOCAL time the child is older than its parent; the root's exit alone then read as confirmed.
        # PR 862 r10 re-baseline (the split): the kill is root-only, so a descendant is NEVER killed (sol r9 B1: a recursive kill across
        # the fold can select a baseline survivor); any descendant still listed after the root's exit leaves the stop unconfirmed. The
        # assertions are unchanged: a listed descendant is unconfirmed, a tree seen gone is confirmed.
        fold = "2026-11-01T06:59:59Z"
        root = self._row(4242, 1, fold)
        child = self._row(5151, 4242, "2026-11-01T07:00:01Z")
        missed = self._stop("$null", "$true", before=f"@({root}, {child})", after=f"@({child})", root_start=fold)
        self.assertFalse(missed["confirmed"], missed)
        self.assertIn("5151 still running", missed["killError"])
        # the extractor's own child outlives a dead intermediate: still found through the listing taken while the root was alive.
        leaf = self._row(6262, 5151, "2026-11-01T07:00:03Z")
        orphaned = self._stop("$null", "$true", before=f"@({root}, {child}, {leaf})", after=f"@({leaf})", root_start=fold)
        self.assertFalse(orphaned["confirmed"], orphaned)
        self.assertIn("6262 still running", orphaned["killError"])
        # adversary 0f5308ad-A3 (r6): an "older" row on the root's parent id may be an older process holding a reused parent id OR the
        # root's real child after a backward clock step; only the wall clock tells them apart, so it is counted, never placed outside.
        older = self._row(7373, 4242, "2026-11-01T06:00:00Z")
        ambiguous = self._stop("$null", "$true", before=f"@({root}, {child}, {older})", after=f"@({older})", root_start=fold)
        self.assertFalse(ambiguous["confirmed"], ambiguous)
        self.assertIn("7373 still running", ambiguous["killError"])
        # a tree that is gone after the kill is confirmed.
        gone = self._stop("$null", "$true", before=f"@({root}, {child})", after="@()", root_start=fold)
        self.assertTrue(gone["confirmed"], gone)
        # a listing that fails is never read as an empty tree.
        unlisted = self._stop("$null", "$true", before="throw 'simulated Win32_Process access denied'", root_start=fold)
        self.assertFalse(unlisted["confirmed"], unlisted)
        self.assertIn("simulated Win32_Process access denied", unlisted["killError"])

    def test_a_reused_member_id_never_adopts_the_replacements_children(self) -> None:
        # adversary 183fe59d-A1: child 5151 exited between the listings and an unrelated process took id 5151 (parent 9000). The
        # replacement itself (another creation time) is not this tree's and is not counted.
        root = self._row(4242, 1, "2026-11-19T10:00:00Z")
        child = self._row(5151, 4242, "2026-11-19T10:00:01Z")
        replacement = self._row(5151, 9000, "2026-11-19T10:00:03Z")
        # PR 862 r8 (hub ruling 20261120T1007Z, fail closed): the replacement's parent 9000 is in no listing and it was created after the
        # root, so nothing shows it is not an orphaned descendant: never counted as a survivor, but the tree is not confirmed either.
        alone = self._stop("$null", "$true", before=f"@({root}, {child})", after=f"@({replacement})")
        self.assertFalse(alone["confirmed"], alone)
        self.assertIn("orphan-possible", alone["killError"])
        self.assertNotIn("still running", alone["killError"])
        # the same replacement whose parent 9000 is a live process from the pre-start baseline is placed outside: the tree is confirmed.
        old_parent = self._row(9000, 1, "2026-11-19T09:00:00Z")
        placed = self._stop("$null", "$true", before=f"@({root}, {child}, {old_parent})", after=f"@({old_parent}, {replacement})",
                            baseline=f"@({old_parent})")
        self.assertTrue(placed["confirmed"], placed)
        # adversary 0f5308ad-A3 (r6): a row on 5151's parent id whose creation time reads after the replacement's is placed outside the tree
        # only by the wall clock (a step back makes the original 5151's late child read that way), so it is counted, never killed.
        its_child = self._row(6262, 5151, "2026-11-19T10:00:04Z")
        reused = self._stop("$null", "$true", before=f"@({root}, {child})", after=f"@({replacement}, {its_child})")
        self.assertFalse(reused["confirmed"], reused)
        self.assertIn("6262 still running", reused["killError"])
        # a child the ORIGINAL 5151 started before the id was reused is still this tree's.
        late_replacement = self._row(5151, 9000, "2026-11-19T10:00:05Z")
        ours = self._row(6262, 5151, "2026-11-19T10:00:02Z")
        orphan = self._stop("$null", "$true", before=f"@({root}, {child})", after=f"@({late_replacement}, {ours})")
        self.assertFalse(orphan["confirmed"], orphan)
        self.assertIn("6262 still running", orphan["killError"])

    def test_an_orphan_from_before_the_first_listing_never_confirms_the_tree(self) -> None:
        # sol r7 B1 / b4826b52-A1: root 4242 started 5151; 5151 started leaf 6262 and exited BEFORE the first listing, so the listing shows
        # 6262 on a parent id (5151) it does not hold. Kill($true) misses 6262 and the root's wait is true. The tree was confirmed and
        # recovery ran. Hub ruling 20261120T1007Z: a row created at or after the root whose parent cannot be shown to be a live process
        # that is itself not a descendant is a POSSIBLE orphan descendant, and the tree is unconfirmed (typed orphan-possible).
        root = self._row(4242, 1, "2026-11-19T10:00:00Z")
        leaf = self._row(6262, 5151, "2026-11-19T10:00:02Z")
        stop = self._stop("$null", "$true", before=f"@({root}, {leaf})", after=f"@({leaf})")
        self.assertTrue(stop["exited"], stop)
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn("orphan-possible", stop["killError"])
        self.assertIn("6262", stop["killError"])
        # created at the root's very creation time: still a possible descendant (at OR after).
        same = self._row(6262, 5151, "2026-11-19T10:00:00Z")
        tie = self._stop("$null", "$true", before=f"@({root}, {same})", after=f"@({same})")
        self.assertFalse(tie["confirmed"], tie)
        self.assertIn("orphan-possible", tie["killError"])
        # control: the after listing holds only a pre-existing unrelated process (in the pre-start baseline, its parent long gone).
        unrelated = self._row(7001, 1, "2026-11-19T09:00:00Z")
        control = self._stop("$null", "$true", before=f"@({root}, {unrelated})", after=f"@({unrelated})", baseline=f"@({unrelated})")
        self.assertTrue(control["confirmed"], control)
        self.assertEqual(control["killError"], "", control)
        # the same process with NO baseline taken: nothing places it outside, so nothing is confirmed.
        blind = self._stop("$null", "$true", before=f"@({root}, {unrelated})", after=f"@({unrelated})", baseline="$null")
        self.assertFalse(blind["confirmed"], blind)
        self.assertIn("none was taken", blind["killError"])
        # a row whose creation time cannot be read is never placed outside the tree, baseline or not.
        unread = "[pscustomobject]@{ id = 7003; parentId = 1; startUtc = $null }"
        unknown = self._stop("$null", "$true", before=f"@({root}, {unread})", after=f"@({unread})", baseline=f"@({unread})")
        self.assertFalse(unknown["confirmed"], unknown)
        self.assertIn("orphan-possible", unknown["killError"])
        # a new process whose live parent is from the baseline is placed outside, and so is its child first listed AFTER it; a child
        # first listed in the SAME listing as its new parent (an earlier holder of that pid may have started it) is not, nor is one under
        # a possible orphan.
        anchor = self._row(8000, 1, "2026-11-19T09:00:00Z")
        newer_child = self._row(8008, 8000, "2026-11-19T10:00:05Z")
        grandchild = self._row(8016, 8008, "2026-11-19T10:00:06Z")
        chain = self._stop("$null", "$true", before=f"@({root}, {anchor}, {newer_child})", after=f"@({anchor}, {newer_child}, {grandchild})",
                           baseline=f"@({anchor})")
        self.assertTrue(chain["confirmed"], chain)
        same_listing = self._stop("$null", "$true", before=f"@({root}, {anchor})", after=f"@({anchor}, {newer_child}, {grandchild})",
                                  baseline=f"@({anchor})")
        self.assertFalse(same_listing["confirmed"], same_listing)
        self.assertIn("8016", same_listing["killError"])
        self.assertNotIn("8008", same_listing["killError"])
        under_orphan = self._row(8024, 6262, "2026-11-19T10:00:06Z")
        tainted = self._stop("$null", "$true", before=f"@({root}, {leaf})", after=f"@({leaf}, {under_orphan})", baseline=f"@({anchor})")
        self.assertFalse(tainted["confirmed"], tainted)
        self.assertIn("8024", tainted["killError"])

    def test_an_orphan_that_reads_older_than_its_root_after_a_clock_step_never_confirms_the_tree(self) -> None:
        # adversary 500e2892-A1: the root started 10:00:05Z, the clock was stepped back, and its intermediate 5151 started leaf 6262 at
        # 09:59:56Z and exited before the first listing. Ordering by creation time placed the leaf outside and confirmed the tree. Only
        # the pre-start baseline (identity equality, no clock) places a row outside, and the leaf is not in it.
        root = self._row(4242, 1, "2026-11-19T10:00:05Z")
        leaf = self._row(6262, 5151, "2026-11-19T09:59:56Z")
        bystander = self._row(7001, 1, "2026-11-19T09:00:00Z")
        stop = self._stop("$null", "$true", before=f"@({root}, {leaf}, {bystander})", after=f"@({leaf}, {bystander})",
                          root_start="2026-11-19T10:00:05Z", baseline=f"@({bystander})")
        self.assertTrue(stop["exited"], stop)
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn("orphan-possible", stop["killError"])
        self.assertIn("6262", stop["killError"])
        self.assertNotIn("7001", stop["killError"])
        # its parent id held by a baseline process that is still alive: the leaf (first listed after the baseline) is that process's.
        holder = self._row(5151, 1, "2026-11-19T08:00:00Z")
        held = self._stop("$null", "$true", before=f"@({root}, {holder}, {leaf})", after=f"@({holder}, {leaf})",
                          root_start="2026-11-19T10:00:05Z", baseline=f"@({holder})")
        self.assertTrue(held["confirmed"], held)

    def test_every_caller_lists_a_baseline_before_it_starts_the_child_it_may_stop(self) -> None:
        # adversary 500e2892-A1 siblings (psm1 install, build-release.ps1:245, assemble.ps1:385): each stop gets the listing its caller
        # took BEFORE starting the child; a stop without one confirms nothing.
        text = _normalised(HELPER_MODULE)
        body = text[text.index(f"function Invoke-PinnedPayloadInstall {{"):]
        self.assertLess(body.index("$baseline = Get-PinnedPayloadBaseline"), body.index("[System.Diagnostics.Process]::Start($psi)"))
        self.assertIn("$stop = Stop-PinnedPayloadChild -Process $process -Baseline $baseline", body)
        for path, start, stop in ((BUILD_RELEASE, "$p = Start-Process -FilePath $destExe", "Stop-PinnedPayloadChild -Process $p -Baseline $probeBaseline"),
                                  (ASSEMBLER, "$probe = Start-Process -FilePath $releaseExe", "Stop-PinnedPayloadChild -Process $probe -Baseline $probeBaseline")):
            with self.subTest(path=path.name):
                caller = _normalised(path)
                self.assertLess(caller.index("$probeBaseline = Get-PinnedPayloadBaseline"), caller.index(start))
                self.assertIn(stop, caller)
        self.assertIn("Get-PinnedPayloadBaseline", text[text.index("Export-ModuleMember -Function"):])

    def test_the_timeout_recovery_is_refused_for_an_orphan_possible_tree(self) -> None:
        # sol r7 B1 (psm1:410): recovery runs only on killConfirmed, which is the stop's confirmed; an orphan-possible stop says false.
        text = _normalised(HELPER_MODULE)
        self.assertIn("$result.killConfirmed = [bool]$stop.confirmed", text)
        stop = re.search(r"function Stop-PinnedPayloadChild \{.*?\n\}", text, re.S).group(0)
        self.assertIn("orphan-possible", stop)
        self.assertIn("confirmed = ($exited -and -not $killError)", stop)

    def test_a_row_whose_identity_and_ancestry_came_from_different_reads_is_never_killed(self) -> None:
        # adversary 0f5308ad-A1/A2 (r6): child 5151 of the root exited and an unrelated process U took its pid between the parent-id read
        # and the start-time read, so the listing joined the root's parent id to U's real creation time. U must stay alive (no per-pid
        # kill is ever made from a listing) and, still listed with that identity, it leaves the kill unconfirmed.
        bystander = self._unrelated_process()
        root = self._row(4242, 1, "2020-01-01T00:00:00Z")
        stop = self._stop("$null", "$true", before=f"@({root}, {self._BYSTANDER_ROW})", after=f"@({self._BYSTANDER_ROW})",
                          root_start="2020-01-01T00:00:00Z", prelude=self._BYSTANDER_PRELUDE, env={"FFX_U": str(bystander.pid)})
        self.assertIsNone(bystander.poll(), "an unrelated process was killed on a listing's word")
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn(f"{bystander.pid} still running", stop["killError"])

    def test_a_root_missing_from_the_listing_or_on_a_reused_pid_proves_no_tree(self) -> None:
        # key3 r5 B1: the root exited between the timeout and the listing, and a live OLDER process U carries the root's pid as a stale
        # parent id. With no root row the tree is unproven: nothing is adopted or killed and the kill is unconfirmed.
        bystander = self._unrelated_process()
        missing = self._stop("$null", "$true", before=f"@({self._BYSTANDER_ROW})", after=f"@({self._BYSTANDER_ROW})",
                             root_start="2099-01-01T00:00:00Z", prelude=self._BYSTANDER_PRELUDE, env={"FFX_U": str(bystander.pid)})
        self.assertIsNone(bystander.poll(), "an unrelated process was killed on a stale parent id")
        self.assertFalse(missing["confirmed"], missing)
        self.assertIn("not in the listing", missing["killError"])
        # key3 r5 sibling psm1:108: the root's pid was reused by Z (another creation time than the handle's); Z's child is not adopted.
        z_root = self._row(4242, 1, "2026-11-19T10:00:07Z")
        z_child = self._row(6363, 4242, "2026-11-19T10:00:08Z")
        reused = self._stop("$null", "$true", before=f"@({z_root}, {z_child})", after="@()")
        self.assertFalse(reused["confirmed"], reused)
        self.assertIn("not in the listing", reused["killError"])

    def test_a_backward_clock_step_never_confirms_a_surviving_child(self) -> None:
        # adversary 0f5308ad-A3: the root started 10:00:05Z, the clock was stepped back 10 s and its real child started 09:59:55.1Z.
        # Kill($true) misses it; ordering by creation time must not place it outside the tree, so the kill stays unconfirmed.
        root = self._row(4242, 1, "2026-11-19T10:00:05Z")
        child = self._row(5151, 4242, "2026-11-19T09:59:55.1Z")
        stop = self._stop("$null", "$true", before=f"@({root}, {child})", after=f"@({child})", root_start="2026-11-19T10:00:05Z")
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn("5151 still running", stop["killError"])

    def test_the_subtree_of_a_clock_stepped_child_is_tracked_after_that_child_exits(self) -> None:
        # adversary d7942d7f-A1: after a backward clock step the root's child C (09:59:55Z) and its child G (09:59:56Z) read older than
        # the root (10:00:05Z). Kill($true) misses C's subtree; C exits and G survives. G must still count, so the kill stays unconfirmed.
        root = self._row(4242, 4, "2026-11-19T10:00:05Z")
        c = self._row(5151, 4242, "2026-11-19T09:59:55Z")
        g = self._row(6262, 5151, "2026-11-19T09:59:56Z")
        stop = self._stop("$null", "$true", before=f"@({root}, {c}, {g})", after=f"@({g})", root_start="2026-11-19T10:00:05Z")
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn("6262 still running", stop["killError"])

    def test_a_tree_seen_gone_only_after_the_budget_is_not_confirmed_and_each_listing_is_bounded_by_it(self) -> None:
        # adversary d7942d7f-A2: each listing is bounded by what is left of the ONE ConfirmMs budget (1500 ms here, so at most 2 s), and
        # an empty listing that completes after the budget is spent cannot establish timely success.
        root = self._row(4242, 1, "2026-11-19T10:00:00Z")
        late = self._stop("$null", "$true", before=f"[System.Threading.Thread]::Sleep(900); @({root})",
                          after="[System.Threading.Thread]::Sleep(900); @()", confirm_ms=1500)
        self.assertFalse(late["confirmed"], late)
        self.assertIn("budget", late["killError"])
        bounds = [int(s) for s in late["listingSec"].split(",")]
        self.assertTrue(bounds and all(1 <= s <= 2 for s in bounds), late)
        # adversary df7e3c66-A2: no listing starts once less than one whole listing second is left (here 1500 - 900 ms), and none is
        # granted more whole seconds than are left: the only listing is the one before the kill, bounded to 1 s.
        self.assertEqual(late["listingSec"], "1", late)
        prompt = self._stop("$null", "$true", before=f"@({root})", after="@()")
        self.assertTrue(prompt["confirmed"], prompt)
        # a zero budget lists nothing at all and confirms nothing.
        none_left = self._stop("$null", "$true", before=f"@({root})", after="@()", confirm_ms=0)
        self.assertFalse(none_left["confirmed"], none_left)
        self.assertEqual(none_left["listingSec"], "", none_left)

    def test_a_survivor_counted_in_one_poll_keeps_its_subtree_counted_in_the_next(self) -> None:
        # adversary df7e3c66-A1: the first post-kill listing shows 5151 (on the root's parent id, reading older) and its child 6262; 5151
        # then exits. The next poll must still count 6262 through 5151, so the kill stays unconfirmed.
        root = self._row(4242, 4, "2026-11-19T10:00:00Z")
        c = self._row(5151, 4242, "2026-11-19T09:59:50Z")
        g = self._row(6262, 5151, "2026-11-19T09:59:51Z")
        stop = self._stop("$null", "$true", before=f"@({root})", after=f"@({c}, {g})", later=f"@({g})")
        self.assertFalse(stop["confirmed"], stop)
        self.assertIn("6262 still running", stop["killError"])

    def test_a_creation_time_that_cannot_be_read_never_proves_anything(self) -> None:
        # r6 property 1: the root's creation time is read through its own handle; unreadable, the listing cannot prove its tree.
        root = self._row(4242, 1, "2026-11-19T10:00:00Z")
        unread = self._stop("$null", "$true", before=f"@({root})", after="@()", root_start=None)
        self.assertFalse(unread["confirmed"], unread)
        self.assertIn("creation time could not be read", unread["killError"])
        # a still-listed row on the root's parent id whose creation time cannot be read is counted, never placed outside.
        unknown = "[pscustomobject]@{ id = 5151; parentId = 4242; startUtc = $null }"
        counted = self._stop("$null", "$true", before=f"@({root}, {unknown})", after=f"@({unknown})")
        self.assertFalse(counted["confirmed"], counted)
        self.assertIn("5151 still running", counted["killError"])

    # A real root started from PowerShell with every standard stream redirected; FFX_ARGS holds its arguments, one per line. Every
    # process here is a system binary that expires by itself (ping -n 12 is about 11 s); nothing is ever stopped by a pid from a listing.
    _REAL_ROOT = (
        "$psi = [System.Diagnostics.ProcessStartInfo]::new($env:FFX_EXE)\n"
        "foreach ($a in ($env:FFX_ARGS -split \"`n\")) { [void]$psi.ArgumentList.Add($a) }\n"
        "$psi.UseShellExecute = $false; $psi.CreateNoWindow = $true\n"
        "$psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true\n"
    )

    def _real_stop(self, exe: str, args: list[str], child_name: str | None) -> dict:
        module = str(HELPER_MODULE).replace("'", "''")
        script = (
            f"$m = Import-Module '{module}' -Force -PassThru\n"
            + self._REAL_ROOT
            + "$b = Get-PinnedPayloadBaseline\n"
            "$p = [System.Diagnostics.Process]::Start($psi)\n"
            "$kid = $null\n"
            "if ($env:FFX_CHILD) {\n"
            "  $deadline = [System.Diagnostics.Stopwatch]::StartNew()\n"
            "  do { [System.Threading.Thread]::Sleep(100); $kid = @(Get-CimInstance -ClassName Win32_Process -Filter \"ParentProcessId = $($p.Id) AND Name = '$($env:FFX_CHILD)'\")[0] } "
            "while (-not $kid -and $deadline.ElapsedMilliseconds -lt 10000)\n"
            "}\n"
            # The child's own handle, opened while it is known to be the root's child, proves it alive afterwards by identity (not by pid).
            "$kidHandle = if ($kid) { [System.Diagnostics.Process]::GetProcessById([int]$kid.ProcessId) } else { $null }\n"
            "$r = & $m { param($q, $s) Stop-PinnedPayloadChild -Process $q -Baseline $s -ConfirmMs 4000 } $p $b\n"
            "$kidAlive = if ($kidHandle) { -not $kidHandle.HasExited } else { $null }\n"
            "[ordered]@{ rootExited = $p.HasExited; kid = $(if ($kid) { [int]$kid.ProcessId } else { $null }); kidAlive = $kidAlive;"
            " confirmed = $r.confirmed; exited = $r.exited; killError = $r.killError } | ConvertTo-Json -Compress\n"
        )
        env = dict(os.environ, FFX_EXE=exe, FFX_ARGS="\n".join(args), FFX_CHILD=child_name or "")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                              timeout=120, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_a_real_root_only_kill_leaves_a_live_child_and_the_stop_unconfirmed(self) -> None:
        # PR 862 r10 (sol r9 B1 / 8d24187c-A1, the split; replaces test_a_real_tree_is_killed_and_confirmed_through_the_real_listing):
        # a real root (cmd.exe) with one long-lived child (PING.EXE, self-expiring), stopped with a pre-start baseline. The kill is
        # root-only, so the child is STILL alive after the stop and the stop is unconfirmed, naming the child's pid. On 8d24187c the
        # recursive kill ended the child and the stop confirmed.
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        result = self._real_stop(str(system32 / "cmd.exe"), ["/d", "/c", "ping", "-n", "12", "127.0.0.1"], "PING.EXE")
        self.assertIsNotNone(result["kid"], result)
        self.assertTrue(result["rootExited"], result)
        self.assertTrue(result["kidAlive"], f"the root-only kill must never end a descendant: {result}")
        self.assertFalse(result["confirmed"], result)
        self.assertIn(f"{result['kid']} still running", result["killError"])

    def test_a_real_root_with_no_descendant_is_killed_and_confirmed_through_the_real_listing(self) -> None:
        # r6, kept under r10: the identity rule must still prove a REAL root (handle creation time == listing creation time) and confirm
        # it gone when it has no descendant, or every real timeout would read unconfirmed and never reconcile.
        # On a busy host (a fleet host running other lanes) unrelated processes started after the baseline whose parent has exited are
        # read orphan-possible by the r8 fail-closed rule, so the stop can be unconfirmed for them alone. What must hold on any host: the
        # root is proven (no "not in the listing", no unreadable creation time), nothing of the tree is still running, the only possible
        # error is orphan-possible, and on a quiet host (hosted CI) the stop confirms.
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        result = self._real_stop(str(system32 / "PING.EXE"), ["-n", "12", "127.0.0.1"], None)
        self.assertTrue(result["rootExited"], result)
        self.assertTrue(result["exited"], result)
        error = result["killError"]
        for forbidden in ("not in the listing", "could not be read", "still running", "could not be listed", "budget"):
            self.assertNotIn(forbidden, error, result)
        self.assertEqual(result["confirmed"], error == "", result)
        if error:
            self.assertTrue(error.startswith("orphan-possible: "), result)
        if os.environ.get("CI"):
            self.assertTrue(result["confirmed"], result)

    def test_the_stop_kills_only_through_the_roots_handle_and_times_with_one_stopwatch(self) -> None:
        # r6 properties 1 and 2 at the source: the listing is ONE Win32_Process query carrying pid, parent id and creation time (no
        # GetProcesses join), the only kill is the root-only Kill through the held root handle (no per-pid open), and every wait comes from one
        # monotonic Stopwatch (no wall-clock reading).
        text = HELPER_MODULE.read_text(encoding="utf-8")
        table = re.search(r"function Get-PinnedPayloadProcessTable \{.*?\n\}", text, re.S).group(0)
        self.assertIn("CreationDate", table)
        self.assertIn("-OperationTimeoutSec", table)
        self.assertNotIn("GetProcesses", table)
        stop = re.search(r"function Stop-PinnedPayloadChild \{.*?\n\}", text, re.S).group(0)
        self.assertNotIn("GetProcessById", stop)
        # PR 862 r10: the one kill is the ROOT's alone (Kill($false)); no recursive kill exists in the module (8d24187c-A1).
        self.assertEqual(re.findall(r"\.Kill\(", stop), [".Kill("])
        self.assertIn("$Process.Kill($false)", stop)
        self.assertNotRegex(text, r"(?i)Kill\(\s*\$?true\s*\)")
        self.assertIn("[System.Diagnostics.Stopwatch]::StartNew()", stop)
        for wall in ("Get-Date", "::Now", "::UtcNow"):
            self.assertNotIn(wall, stop)
        self.assertNotRegex(stop, r"WaitForExit\(\$ConfirmMs\)")

    def test_the_real_process_table_lists_this_process_with_its_parent_and_a_utc_start(self) -> None:
        module = str(HELPER_MODULE).replace("'", "''")
        script = (
            f"$m = Import-Module '{module}' -Force -PassThru\n"
            "$row = @(& $m { Get-PinnedPayloadProcessTable } | Where-Object { $_.id -eq $PID })[0]\n"
            # r6: the listing's creation time must equal the one read through this process's own handle (to the microsecond, the CIM
            # precision), or no real root could ever be proven and every timeout would read unconfirmed.
            "$own = [System.Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks\n"
            "[ordered]@{ parent = $row.parentId; kind = [string]$row.startUtc.Kind; same = ($row.startUtc.Ticks -eq ($own - ($own % 10))) } | ConvertTo-Json -Compress\n"
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result, {"parent": os.getpid(), "kind": "Utc", "same": True})

    def test_a_path_that_is_not_fully_qualified_is_refused_before_any_child(self) -> None:
        for bad in ("relative\\out", "C:drive-relative", "\\rooted-no-drive"):
            with self.subTest(path=bad):
                result = self._run(bad)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["exitCode"])
                self.assertIn("not a fully qualified path", result["reason"])


def _short_spelling(path: Path) -> Path:
    """The 8.3 spelling of an existing path, or the path itself where the volume generates no short names."""
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return Path(buffer.value) if 0 < length < len(buffer) else path


@_requires(os.name == "nt" and bool(PWSH), "needs Windows and pwsh")
class PublicationCheckBehaviourTests(unittest.TestCase):
    """adversary ae560106-A1: the bytes are re-read at the point of publication (zip entry; held file during the manifest write)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ffx-publish-")
        self.addCleanup(self._tmp.cleanup)
        # PR 862 r4: the hosted runner's TEMP is an 8.3 spelling (C:\Users\RUNNER~1\...) and PowerShell reports a child's FullName by
        # its long name, so a string comparison of paths failed there only. Every test here runs under a short spelling where the
        # volume has one, so that difference reproduces on any Windows host.
        long_named = Path(self._tmp.name) / "a-folder-name-longer-than-eight-dot-three"
        long_named.mkdir()
        self.tmp = _short_spelling(long_named)

    def _assert_backup_reported(self, result: dict, backup: Path) -> None:
        # The error must name THIS backup file (same file on disk), however PowerShell spells the path (PR 862 r4 F1/F2).
        marker = "destination present; backup kept at "
        named = [e.split(marker, 1)[1] for e in result["errors"] if marker in e]
        self.assertTrue(any(os.path.exists(p) and os.path.samefile(p, backup) for p in named), result)

    def _ps(self, body: str, **env: str) -> dict:
        module = str(HELPER_MODULE).replace("'", "''")
        script = f"$m = Import-Module '{module}' -Force -PassThru\n" + body
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=120, env=dict(os.environ, **env))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def _zip(self, name: str, members: dict[str, bytes]) -> Path:
        import zipfile
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def _check_zip(self, zip_path: Path, expected: bytes) -> dict:
        return self._ps(
            "$r = Test-PinnedZipMember -ZipPath $env:FFX_ZIP -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "$r | ConvertTo-Json -Compress\n",
            FFX_ZIP=str(zip_path), FFX_SHA=hashlib.sha256(expected).hexdigest(), FFX_LEN=str(len(expected)))

    def test_the_zip_check_passes_only_when_the_entry_carries_the_recorded_bytes(self) -> None:
        payload = b"MZ pinned ffmpeg bytes" * 100
        good = self._zip("good.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": payload})
        result = self._check_zip(good, payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["zipSha256"], hashlib.sha256(good.read_bytes()).hexdigest())
        missing = self._zip("missing.zip", {"MLVApp.exe": b"exe"})
        result = self._check_zip(missing, payload)
        self.assertFalse(result["ok"], result)
        self.assertIn("0 root entries named ffmpeg.exe", result["reason"])
        stale = self._zip("stale.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": b"other bytes"})
        result = self._check_zip(stale, payload)
        self.assertFalse(result["ok"], result)
        self.assertIn("not the recorded", result["reason"])
        nested = self._zip("nested.zip", {"sub/ffmpeg.exe": payload})
        self.assertFalse(self._check_zip(nested, payload)["ok"], "only a root entry beside the exe counts")
        result = self._check_zip(self.tmp / "absent.zip", payload)
        self.assertFalse(result["ok"], result)
        self.assertIn("could not be read as a zip", result["reason"])

    def test_the_zip_check_refuses_an_entry_that_extracts_over_the_member(self) -> None:
        # adversary 9bd36192-A1: a zip holding the pinned ffmpeg.exe FOLLOWED by ./ffmpeg.exe (MZwrong) passed, because only the
        # literal name was counted; Expand-Archive -Force then wrote the alias over the pinned file. Every entry name must be
        # canonical (no ., .., empty, trailing-dot/space, 8.3 or stream segment), and no two entries may name one path.
        payload = b"MZ pinned ffmpeg bytes" * 100
        for alias in ("./ffmpeg.exe", ".\\ffmpeg.exe", "/ffmpeg.exe", "sub/../ffmpeg.exe", "ffmpeg.exe.", "ffmpeg.exe ",
                      "FFMPEG~1.EXE", "ffmpeg.exe::$DATA", "sub//x.dll"):
            with self.subTest(alias=alias):
                bad = self._zip(f"alias-{abs(hash(alias))}.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": payload, alias: b"MZwrong"})
                result = self._check_zip(bad, payload)
                self.assertFalse(result["ok"], result)
                self.assertIn("not a canonical entry name", result["reason"])
        twin = self._zip("twin.zip", {"ffmpeg.exe": payload, "pixel_maps/a.fpm": b"a", "PIXEL_MAPS/A.FPM": b"b"})
        result = self._check_zip(twin, payload)
        self.assertFalse(result["ok"], result)
        self.assertIn("name one extracted path", result["reason"])
        good = self._zip("good-tree.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": payload, "pixel_maps/": b"", "pixel_maps/a.fpm": b"a"})
        self.assertTrue(self._check_zip(good, payload)["ok"], "a canonical tree with a directory entry passes")

    MINE = "0123456789abcdef0123456789abcdef"
    OTHER = "fedcba9876543210fedcba9876543210"

    def _restore(self, out: Path, transaction_id: str, prelude: str = "") -> dict:
        module = str(HELPER_MODULE).replace("'", "''")
        script = (
            prelude
            + f"$m = Import-Module '{module}' -Force -PassThru\n"
            "$r = & $m { param($o, $t) Restore-PinnedPayloadTransaction -OutputDir $o -TransactionId $t } $env:FFX_OUT $env:FFX_TX\n"
            "$r | ConvertTo-Json -Compress\n"
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                              timeout=120, env=dict(os.environ, FFX_OUT=str(out), FFX_TX=transaction_id))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_a_killed_install_gets_its_original_back(self) -> None:
        # adversary f92303b7-A1: the extractor moved the original ffmpeg.exe into its transaction backup and was killed (timeout)
        # before the replacement landed; the Python rollback never ran, so ffmpeg.exe was absent and OLD stayed in the backup.
        # PR 862 r10 re-baseline: unchanged here. The restore runs only after a CONFIRMED stop, which with the root-only kill means a
        # root with no descendant still listed; a tree with a live descendant is unconfirmed and never reaches this restore.
        out = self.tmp / "out"
        mine = out / f".vendored-payload-transaction-{self.MINE}"
        mine.mkdir(parents=True)
        (mine / "ffmpeg.exe").write_bytes(b"OLD")
        result = self._restore(out, self.MINE)
        self.assertEqual(result["restored"], ["ffmpeg.exe"], result)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual((out / "ffmpeg.exe").read_bytes(), b"OLD")
        self.assertEqual(list(mine.iterdir()), [], "the backup must be moved out, not copied")
        # killed after the replacement was published but before the backup was cleaned: a file already sits at the destination, so
        # it is never overwritten (adversary 32501f50-A1); the original stays in the backup and the result says where.
        (mine / "ffmpeg.exe").write_bytes(b"ORIGINAL")
        (out / "ffmpeg.exe").write_bytes(b"NEW")
        result = self._restore(out, self.MINE)
        self.assertEqual(result["restored"], [], result)
        self.assertEqual((out / "ffmpeg.exe").read_bytes(), b"NEW")
        self.assertEqual((mine / "ffmpeg.exe").read_bytes(), b"ORIGINAL")
        self._assert_backup_reported(result, mine / "ffmpeg.exe")
        # killed before its transaction began: nothing to restore, and that is not an error.
        none = self._restore(self.tmp / "no-such-dir", self.MINE)
        self.assertEqual((none["restored"], none["errors"], none["found"]), ([], [], False))

    def test_recovery_never_touches_another_installs_transaction(self) -> None:
        # adversary 78d626ed-A1: a concurrent install Q sharing the output folder had moved OLD into ITS transaction and published the
        # pinned file; P's timeout recovery restored Q's backup over Q's file. Recovery now opens only the folder named by its own id.
        out = self.tmp / "shared"
        other = out / f".vendored-payload-transaction-{self.OTHER}"
        other.mkdir(parents=True)
        (other / "ffmpeg.exe").write_bytes(b"Q-OLD")
        (out / "ffmpeg.exe").write_bytes(b"Q-PINNED")
        result = self._restore(out, self.MINE)
        self.assertEqual((result["restored"], result["errors"], result["found"]), ([], [], False), result)
        self.assertEqual((out / "ffmpeg.exe").read_bytes(), b"Q-PINNED")
        self.assertEqual((other / "ffmpeg.exe").read_bytes(), b"Q-OLD")
        # adversary 32501f50-A1: P was killed after it moved OLD into ITS OWN transaction, and Q has since published and verified the
        # pinned file. P's recovery restores only onto an absent destination: Q-PINNED survives and P's backup stays, reported.
        mine = out / f".vendored-payload-transaction-{self.MINE}"
        mine.mkdir()
        (mine / "ffmpeg.exe").write_bytes(b"P-OLD")
        result = self._restore(out, self.MINE)
        self.assertEqual((result["restored"], result["found"]), ([], True), result)
        self.assertEqual((out / "ffmpeg.exe").read_bytes(), b"Q-PINNED")
        self.assertEqual((mine / "ffmpeg.exe").read_bytes(), b"P-OLD")
        self._assert_backup_reported(result, mine / "ffmpeg.exe")
        self.assertEqual((other / "ffmpeg.exe").read_bytes(), b"Q-OLD")

    def test_a_transaction_that_cannot_be_listed_is_an_error_never_silence(self) -> None:
        # adversary 78d626ed-A3: a backup listing that failed (ListDirectory denied) was dropped by SilentlyContinue and recovery
        # said "reconciled" with nothing restored. Get-ChildItem is shadowed by a global function that throws like a denied listing.
        out = self.tmp / "out"
        mine = out / f".vendored-payload-transaction-{self.MINE}"
        mine.mkdir(parents=True)
        (mine / "ffmpeg.exe").write_bytes(b"OLD")
        result = self._restore(out, self.MINE, prelude="function global:Get-ChildItem { throw 'simulated ListDirectory access denied' }\n")
        self.assertEqual(result["restored"], [], result)
        self.assertTrue(any("simulated ListDirectory access denied" in e for e in result["errors"]), result)
        self.assertTrue(result["found"])
        (out / f".vendored-payload-transaction-{self.OTHER}").write_bytes(b"not a folder")
        result = self._restore(out, self.OTHER)
        self.assertTrue(result["errors"], "a transaction path that is not a plain folder is reported")
        # adversary 9bd36192-A4: an output path on a missing drive threw from Join-Path before the first try.
        result = self._restore(Path("NO_SUCH_REVIEW_DRIVE:\\out"), self.MINE, prelude="$ErrorActionPreference = 'Stop'\n")
        self.assertEqual((result["restored"], result["found"]), ([], True), result)
        self.assertTrue(result["errors"], result)

    def test_relative_paths_mean_the_powershell_location(self) -> None:
        # adversary 78d626ed-A2: build-release -OutRoot '.\artifacts' from another location installed and read back the file through
        # PowerShell paths while File.Open resolved the same relative path against the process directory, refusing a good file.
        here = self.tmp / "ps-location"
        here.mkdir()
        (here / "ffmpeg.exe").write_bytes(b"pinned")
        good = self._zip("good.zip", {"ffmpeg.exe": b"pinned"})
        (here / "good.zip").write_bytes(good.read_bytes())
        elsewhere = self.tmp / "process-dir"
        elsewhere.mkdir()
        result = self._ps(
            "Set-Location -LiteralPath $env:FFX_HERE\n"
            "[System.Environment]::CurrentDirectory = $env:FFX_ELSEWHERE\n"
            "$h = Open-PinnedFileHold -Path '.\\ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes 6\n"
            "if ($h.stream) { $h.stream.Dispose() }\n"
            "$z = Test-PinnedZipMember -ZipPath 'good.zip' -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes 6\n"
            "[ordered]@{ hold = $h.ok; holdReason = $h.reason; zip = $z.ok; zipReason = $z.reason } | ConvertTo-Json -Compress\n",
            FFX_HERE=str(here), FFX_ELSEWHERE=str(elsewhere), FFX_SHA=hashlib.sha256(b"pinned").hexdigest())
        self.assertTrue(result["hold"], result)
        self.assertTrue(result["zip"], result)

    def test_the_timeout_branch_reconciles_only_after_a_confirmed_kill(self) -> None:
        text = _normalised(HELPER_MODULE)
        body = text[text.index(f"function {HELPER} {{"):]
        self.assertLess(body.index("$transactionId = [guid]::NewGuid().ToString('N')"), body.index("[System.Diagnostics.Process]::Start($psi)"))
        self.assertIn("'--transaction-id', $transactionId,", body, "the child must be told the transaction folder the recovery opens")
        self.assertNotIn("preexistingTransactions", body, "recovery is by exact id, never by which folders look new")
        branch = body[body.index("if ($result.timedOut) {"): body.index("if ($result.exitCode -ne 0) {")]
        self.assertRegex(branch, r"if \(\$result\.killConfirmed\) \{\n\s*\$restore = Restore-PinnedPayloadTransaction -OutputDir \$OutputDir -TransactionId \$transactionId\n")
        self.assertIn("was NOT reconciled", branch)
        # PR 862 r10 re-baseline: killConfirmed is the ROOT-ONLY stop's confirmation (the root killed, the tree seen gone); there is no
        # recovery after an unconfirmed stop: no second kill, no retry, no restore.
        self.assertIn("its root was killed (root-only kill) and its process tree was seen gone", branch)
        self.assertEqual(branch.count("Stop-PinnedPayloadChild"), 0, "no second stop or kill in the timeout branch")
        self.assertEqual(branch.count("Restore-PinnedPayloadTransaction"), 1)
        restore =text[text.index("function Restore-PinnedPayloadTransaction {"): text.index(f"function {HELPER} {{")]
        self.assertNotRegex(restore, r"Remove-Item|::Delete\(", "the reconciliation only moves files back, it never deletes")
        self.assertNotRegex(restore, r"::Move\(.*\$true\)", "a backup is never moved over a destination (adversary 32501f50-A1)")

    @staticmethod
    def _case_variants(sha: str) -> dict[str, str]:
        mixed = "".join(c.upper() if i % 2 else c for i, c in enumerate(sha))
        return {"lower": sha, "upper": sha.upper(), "mixed": mixed}

    def test_every_accepted_digest_case_matches_the_same_bytes(self) -> None:
        # sol r7 B2: both helpers' ValidatePattern is case-insensitive, so an uppercase digest (as Get-FileHash prints it) was ACCEPTED
        # and then compared case-sensitively to a lowercase hash: matching bytes were refused. Each accepted case variant must agree.
        payload = b"MZ pinned ffmpeg bytes" * 100
        target = self.tmp / "ffmpeg.exe"
        target.write_bytes(payload)
        good = self._zip("case.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": payload})
        for label, digest in self._case_variants(hashlib.sha256(payload).hexdigest()).items():
            with self.subTest(case=label):
                result = self._ps(
                    "$h = Open-PinnedFileHold -Path $env:FFX_FILE -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
                    "if ($h.stream) { $h.stream.Dispose() }\n"
                    "$z = Test-PinnedZipMember -ZipPath $env:FFX_ZIP -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
                    "[ordered]@{ hold = $h.ok; holdReason = $h.reason; zip = $z.ok; zipReason = $z.reason } | ConvertTo-Json -Compress\n",
                    FFX_FILE=str(target), FFX_ZIP=str(good), FFX_SHA=digest, FFX_LEN=str(len(payload)))
                self.assertTrue(result["hold"], result)
                self.assertTrue(result["zip"], result)
        # a wrong digest in any case is still refused.
        for label, digest in self._case_variants("0" * 63 + "a").items():
            with self.subTest(wrong=label):
                result = self._ps(
                    "$h = Open-PinnedFileHold -Path $env:FFX_FILE -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
                    "if ($h.stream) { $h.stream.Dispose() }\n"
                    "$z = Test-PinnedZipMember -ZipPath $env:FFX_ZIP -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
                    "[ordered]@{ hold = $h.ok; zip = $z.ok } | ConvertTo-Json -Compress\n",
                    FFX_FILE=str(target), FFX_ZIP=str(good), FFX_SHA=digest, FFX_LEN=str(len(payload)))
                self.assertEqual(result, {"hold": False, "zip": False})

    def test_the_real_pinned_zip_matches_its_member_digest_in_every_case(self) -> None:
        # sol r7 B2 repro: Test-PinnedZipMember on the real pinned ffmpegWin64.zip (ExpectedBytes=94240768) passed lowercase only.
        record = _ffmpeg_record()
        member = next(m for m in record["selected_members"] if m["output_name"] == "ffmpeg.exe")
        archive = ROOT / Path(record["path"])
        if archive.stat().st_size != record["bytes"]:
            # PR 862 r10 (Grok G4(b) sibling): the archive is a plain Git blob (no LFS filter) and the Windows shards check out the
            # whole tree, so on Windows CI a wrong size can only hide a broken checkout: it fails there; elsewhere it skips.
            _capability_missing(f"{archive} is not the pinned archive here (an LFS pointer or a partial checkout)")
        for label, digest in self._case_variants(member["sha256"]).items():
            with self.subTest(case=label):
                result = self._ps(
                    "$z = Test-PinnedZipMember -ZipPath $env:FFX_ZIP -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
                    "[ordered]@{ ok = $z.ok; reason = $z.reason; memberSha256 = $z.memberSha256 } | ConvertTo-Json -Compress\n",
                    FFX_ZIP=str(archive), FFX_SHA=digest, FFX_LEN=str(member["bytes"]))
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["memberSha256"], member["sha256"])

    def test_the_file_hold_refuses_a_changed_file_and_blocks_a_delete_while_held(self) -> None:
        target = self.tmp / "ffmpeg.exe"
        target.write_bytes(b"pinned")
        sha = hashlib.sha256(b"pinned").hexdigest()
        result = self._ps(
            "$h = Open-PinnedFileHold -Path $env:FFX_FILE -ExpectedSha256 $env:FFX_SHA -ExpectedBytes 6\n"
            "$deleteRefused = $false\n"
            "try { Remove-Item -LiteralPath $env:FFX_FILE -Force -ErrorAction Stop } catch { $deleteRefused = $true }\n"
            "$h.stream.Dispose()\n"
            "[ordered]@{ ok = $h.ok; deleteRefused = $deleteRefused; stillThere = (Test-Path -LiteralPath $env:FFX_FILE) } | ConvertTo-Json -Compress\n",
            FFX_FILE=str(target), FFX_SHA=sha)
        self.assertEqual(result, {"ok": True, "deleteRefused": True, "stillThere": True})
        result = self._ps(
            "$h = Open-PinnedFileHold -Path $env:FFX_FILE -ExpectedSha256 $env:FFX_SHA -ExpectedBytes 6\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason; stream = ($null -eq $h.stream) } | ConvertTo-Json -Compress\n",
            FFX_FILE=str(target), FFX_SHA="0" * 64)
        self.assertFalse(result["ok"])
        self.assertTrue(result["stream"], "a refused hold must not leak an open handle")
        target.unlink()
        result = self._ps(
            "$h = Open-PinnedFileHold -Path $env:FFX_FILE -ExpectedSha256 $env:FFX_SHA -ExpectedBytes 6\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason } | ConvertTo-Json -Compress\n",
            FFX_FILE=str(target), FFX_SHA=sha)
        self.assertFalse(result["ok"])
        self.assertIn("could not be opened", result["reason"])

    def test_a_missing_capability_fails_on_windows_ci_and_skips_elsewhere(self) -> None:
        # PR 862 r10 (Grok G4(b)): a symbolic link that cannot be created (here: its folder does not exist) under CI=true on Windows
        # must FAIL the test, never skip it; with CI unset it still skips with its reason. On 8d24187c it skipped on CI too.
        from unittest import mock
        target = self.tmp / "target.bin"
        target.write_bytes(b"x")
        link = self.tmp / "no-such-folder" / "link.bin"
        with mock.patch.dict(os.environ, {"CI": "true"}):
            try:
                self._symlink_or_skip(link, target)
            except unittest.SkipTest as exc:
                self.fail(f"a missing capability was skipped on Windows CI: {exc}")
            except AssertionError as exc:
                self.assertIn(CAPABILITY_FAILURE_MARKER, str(exc))
            else:
                self.fail("a link into a missing folder was created")
        with mock.patch.dict(os.environ, {"CI": ""}):
            with self.assertRaises(unittest.SkipTest):
                self._symlink_or_skip(link, target)
        # every capability skip in this file goes through the one decision: no direct skipTest/skipUnless/skipIf is left.
        source = _normalised(Path(__file__))
        self.assertNotRegex(source, r"self\.skipTest\(|@unittest\.skip(Unless|If)?\(")
        self.assertEqual(source.count("_capability_missing(f\"per-directory case sensitivity cannot be enabled here"), 1)

    def _symlink_or_skip(self, link: Path, target: Path) -> None:
        try:
            os.symlink(target, link)
        except OSError as exc:
            _capability_missing(f"this account cannot create a file symbolic link ({exc}); the source pin covers the open flags")

    def test_a_symbolic_link_to_the_pinned_bytes_is_never_held(self) -> None:
        # sol r8 B1 trace: the verified file is moved to pin\ffmpeg.exe and the output pathname becomes a symbolic link to it. The old
        # hold followed the link, verified the pinned bytes and returned ok=true; the link was then deleted and MZwrong written at the
        # pathname while the manifest kept the pinned digest. The hold must refuse the link itself (reason reparse-point).
        payload = b"MZ pinned ffmpeg bytes" * 100
        pin = self.tmp / "pin"
        out = self.tmp / "out"
        pin.mkdir()
        out.mkdir()
        (pin / "ffmpeg.exe").write_bytes(payload)
        link = out / "ffmpeg.exe"
        self._symlink_or_skip(link, pin / "ffmpeg.exe")
        good = self._zip("linked-target.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": payload})
        zip_link = out / "package.zip"
        self._symlink_or_skip(zip_link, good)
        result = self._ps(
            "$h = Open-PinnedFileHold -Path $env:FFX_LINK -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "$linkDeleteRefused = $false\n"
            "try { [System.IO.File]::Delete($env:FFX_LINK) } catch { $linkDeleteRefused = $true }\n"
            "if ($h.stream) { $h.stream.Dispose() }\n"
            "$z = Test-PinnedZipMember -ZipPath $env:FFX_ZIPLINK -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "$real = Open-PinnedFileHold -Path $env:FFX_REAL -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "if ($real.stream) { $real.stream.Dispose() }\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason; streamNull = ($null -eq $h.stream); linkDeleteRefused = $linkDeleteRefused;"
            " zip = $z.ok; zipReason = $z.reason; real = $real.ok } | ConvertTo-Json -Compress\n",
            FFX_LINK=str(link), FFX_ZIPLINK=str(zip_link), FFX_REAL=str(pin / "ffmpeg.exe"),
            FFX_SHA=hashlib.sha256(payload).hexdigest(), FFX_LEN=str(len(payload)))
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["reason"].startswith("reparse-point: "), result)
        self.assertTrue(result["streamNull"], "a refused hold must not leak an open handle")
        self.assertFalse(result["zip"], result)
        self.assertTrue(result["zipReason"].startswith("reparse-point: "), result)
        self.assertTrue(result["real"], "the regular file the link pointed at still holds (control)")

    def test_a_junction_on_the_way_to_the_pinned_bytes_is_never_held(self) -> None:
        # adversary 2c649ed9-A1: FILE_FLAG_OPEN_REPARSE_POINT covers only the final component. published\<sha> was a junction to good\,
        # the hold verified good\ffmpeg.exe, and the junction (not held) was then retargeted to bad\ holding MZwrong, so the manifest
        # landed beside the wrong bytes. A hold or zip check whose path crosses a junction is refused (reason reparse-ancestor).
        payload = b"MZ pinned ffmpeg bytes" * 100
        good = self.tmp / "good"
        good.mkdir()
        (good / "ffmpeg.exe").write_bytes(payload)
        self._zip("good/package.zip", {"MLVApp.exe": b"exe", "ffmpeg.exe": payload})
        published = self.tmp / "published"
        published.mkdir()
        junction = published / "2c649ed9a139"
        result = self._ps(
            "$null = New-Item -ItemType Junction -Path $env:FFX_JUNCTION -Target $env:FFX_GOOD\n"
            "$h = Open-PinnedFileHold -Path (Join-Path $env:FFX_JUNCTION 'ffmpeg.exe') -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "$junctionRemovable = $false\n"
            "try { [System.IO.Directory]::Delete($env:FFX_JUNCTION); $junctionRemovable = $true } catch { }\n"
            "if ($h.stream) { $h.stream.Dispose() }\n"
            "if (-not $junctionRemovable) { [System.IO.Directory]::Delete($env:FFX_JUNCTION) }\n"
            "$null = New-Item -ItemType Junction -Path $env:FFX_JUNCTION -Target $env:FFX_GOOD\n"
            "$z = Test-PinnedZipMember -ZipPath (Join-Path $env:FFX_JUNCTION 'package.zip') -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "[System.IO.Directory]::Delete($env:FFX_JUNCTION)\n"
            "$real = Open-PinnedFileHold -Path (Join-Path $env:FFX_GOOD 'ffmpeg.exe') -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "if ($real.stream) { $real.stream.Dispose() }\n"
            "$realZip = Test-PinnedZipMember -ZipPath (Join-Path $env:FFX_GOOD 'package.zip') -MemberName 'ffmpeg.exe' -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason; streamNull = ($null -eq $h.stream); junctionRemovable = $junctionRemovable;"
            " zip = $z.ok; zipReason = $z.reason; real = $real.ok; realReason = $real.reason; realZip = $realZip.ok } | ConvertTo-Json -Compress\n",
            FFX_JUNCTION=str(junction), FFX_GOOD=str(good), FFX_SHA=hashlib.sha256(payload).hexdigest(), FFX_LEN=str(len(payload)))
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["reason"].startswith("reparse-ancestor: "), result)
        self.assertTrue(result["streamNull"], "a refused hold must not leak an open handle")
        self.assertTrue(result["junctionRemovable"], "the junction itself is never held, which is why it is refused")
        self.assertFalse(result["zip"], result)
        self.assertTrue(result["zipReason"].startswith("reparse-ancestor: "), result)
        self.assertTrue(result["real"], result)
        self.assertTrue(result["realZip"], result)

    def test_the_pathname_comparison_is_ordinal_below_the_root(self) -> None:
        # adversary d0ae83d6-A1: with NTFS case sensitivity on C:\case, PIN (a junction) and pin (the real folder) differ only in case;
        # an OrdinalIgnoreCase comparison passed the junction. Below the root the comparison is ordinal over on-disk names; the drive
        # letter or \\server\share stays case-insensitive. A caller's own lowercase spelling of a real path still holds (control).
        payload = b"MZ pinned ffmpeg bytes" * 100
        real = self.tmp / "MixedCase" / "Inner"
        real.mkdir(parents=True)
        (real / "FFmpeg.EXE").write_bytes(payload)
        lowered = str(self.tmp / "mixedcase" / "inner" / "ffmpeg.exe")
        lowered = lowered[0].lower() + lowered[1:]
        result = self._ps(
            "$h = Open-PinnedFileHold -Path $env:FFX_LOWER -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "if ($h.stream) { $h.stream.Dispose() }\n"
            "$t = [MlvReleaseAdmission.PinnedNoFollowOpen]\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason;"
            " caseOnly = $t::SamePathname('C:\\case\\pin\\d0ae83d69070\\ffmpeg.exe', 'C:\\case\\PIN\\d0ae83d69070\\ffmpeg.exe', 'C:\\');"
            " leafCase = $t::SamePathname('C:\\case\\pin\\ffmpeg.exe', 'C:\\case\\pin\\FFMPEG.exe', 'C:\\');"
            " driveCase = $t::SamePathname('C:\\case\\pin\\ffmpeg.exe', 'c:\\case\\pin\\ffmpeg.exe', 'c:\\');"
            " uncCase = $t::SamePathname('\\\\Srv\\Share\\pin\\ffmpeg.exe', '\\\\srv\\share\\pin\\ffmpeg.exe', '\\\\srv\\share');"
            " uncDeep = $t::SamePathname('\\\\srv\\share\\pin\\ffmpeg.exe', '\\\\srv\\share\\PIN\\ffmpeg.exe', '\\\\srv\\share') } | ConvertTo-Json -Compress\n",
            FFX_LOWER=lowered, FFX_SHA=hashlib.sha256(payload).hexdigest(), FFX_LEN=str(len(payload)))
        self.assertEqual(result, {"ok": True, "reason": "", "caseOnly": False, "leafCase": False, "driveCase": True,
                                  "uncCase": True, "uncDeep": False}, result)

    def test_a_junction_differing_only_in_case_in_a_case_sensitive_folder_is_never_held(self) -> None:
        # adversary d0ae83d6-A1 end to end: needs per-directory case sensitivity (fsutil setCaseSensitiveInfo), which an account
        # without it cannot enable; the comparison test above and the source pin cover it there.
        payload = b"MZ pinned ffmpeg bytes" * 100
        case = self.tmp / "case"
        case.mkdir()
        flag = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(case), "enable"], capture_output=True, text=True)
        probe = subprocess.run(["fsutil.exe", "file", "queryCaseSensitiveInfo", str(case)], capture_output=True, text=True)
        if flag.returncode != 0 or "is enabled" not in probe.stdout:
            _capability_missing(f"per-directory case sensitivity cannot be enabled here: {(flag.stdout + flag.stderr).strip()}")
        (case / "pin" / "d0ae83d69070").mkdir(parents=True)
        (case / "pin" / "d0ae83d69070" / "ffmpeg.exe").write_bytes(payload)
        result = self._ps(
            "$null = New-Item -ItemType Junction -Path $env:FFX_UPPER -Target $env:FFX_PIN\n"
            "$h = Open-PinnedFileHold -Path (Join-Path $env:FFX_UPPER 'd0ae83d69070\\ffmpeg.exe') -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "if ($h.stream) { $h.stream.Dispose() }\n"
            "$real = Open-PinnedFileHold -Path (Join-Path $env:FFX_PIN 'd0ae83d69070\\ffmpeg.exe') -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "if ($real.stream) { $real.stream.Dispose() }\n"
            "[System.IO.Directory]::Delete($env:FFX_UPPER)\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason; real = $real.ok } | ConvertTo-Json -Compress\n",
            FFX_UPPER=str(case / "PIN"), FFX_PIN=str(case / "pin"), FFX_SHA=hashlib.sha256(payload).hexdigest(), FFX_LEN=str(len(payload)))
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["reason"].startswith("reparse-ancestor: "), result)
        self.assertTrue(result["real"], result)

    def test_while_held_the_entry_cannot_be_renamed_unlinked_or_overwritten(self) -> None:
        # sol r8 B1: the held handle must BE the pathname's file entry, and FILE_SHARE_READ alone keeps that entry in place: a rename,
        # an unlink and an overwrite of the held path all fail until the hold is released; the bytes are unchanged afterwards.
        # adversary 2c649ed9-A1: a plain ANCESTOR of the held file cannot be renamed either, so no junction can take its place.
        payload = b"MZ pinned ffmpeg bytes" * 100
        out = self.tmp / "out"
        out.mkdir()
        target = out / "ffmpeg.exe"
        target.write_bytes(payload)
        result = self._ps(
            "$h = Open-PinnedFileHold -Path $env:FFX_FILE -ExpectedSha256 $env:FFX_SHA -ExpectedBytes ([long]$env:FFX_LEN)\n"
            "$refused = [ordered]@{}\n"
            "try { [System.IO.File]::Move($env:FFX_FILE, $env:FFX_FILE + '.moved'); $refused.rename = $false } catch { $refused.rename = $true }\n"
            "try { [System.IO.File]::Delete($env:FFX_FILE); $refused.unlink = $false } catch { $refused.unlink = $true }\n"
            "try { [System.IO.File]::WriteAllBytes($env:FFX_FILE, [byte[]][char[]]'MZwrong'); $refused.overwrite = $false } catch { $refused.overwrite = $true }\n"
            "try { [System.IO.Directory]::Move($env:FFX_OUT, $env:FFX_OUT + '-moved'); $refused.parentRename = $false } catch { $refused.parentRename = $true }\n"
            "if ($h.stream) { $h.stream.Dispose() }\n"
            "[ordered]@{ ok = $h.ok; reason = $h.reason; refused = $refused; moved = (Test-Path -LiteralPath ($env:FFX_FILE + '.moved')) } | ConvertTo-Json -Compress\n",
            FFX_FILE=str(target), FFX_OUT=str(out), FFX_SHA=hashlib.sha256(payload).hexdigest(), FFX_LEN=str(len(payload)))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["refused"], {"rename": True, "unlink": True, "overwrite": True, "parentRename": True}, result)
        self.assertFalse(result["moved"], result)
        self.assertEqual(target.read_bytes(), payload)


class RecordOnlyInstallTests(unittest.TestCase):
    """adversary ae560106-A2: the local install validates the named record alone and opens no tracked file outside that payload.

    The full validate_manifest enumerates every Git-tracked file and opens a 512-byte prefix of each (owner clip fixtures included).
    Here the census and the full validator are replaced by functions that fail the test if called, and every file open is recorded:
    only the manifest, the named archive and the output folder may be touched. The real in-repo archive is used; no clip is opened.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ffx-record-only-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "out"
        self.out.mkdir()

    def _argv(self, *extra: str) -> list[str]:
        archive = PurePosixPath(_ffmpeg_record()["path"]).name
        return ["--repo-root", str(ROOT), "--payload-id", PAYLOAD_ID, "--archive-reference", archive,
                "--output-dir", str(self.out), "--verify-installed", *extra]

    def test_record_only_never_runs_the_tracked_file_census_or_opens_another_tracked_file(self) -> None:
        import io
        from unittest import mock
        from tools.repo_hygiene import extract_vendored_native_payload as extractor
        from tools.repo_hygiene import vendored_native_payloads as validator

        opened: list[Path] = []
        real_path_open = Path.open
        real_os_open = os.open

        def path_open(self, *args, **kwargs):
            opened.append(Path(self))
            return real_path_open(self, *args, **kwargs)

        def os_open(path, *args, **kwargs):
            opened.append(Path(os.fsdecode(path)))
            return real_os_open(path, *args, **kwargs)

        census = mock.Mock(side_effect=AssertionError("the tracked-file census ran on the install path"))
        full = mock.Mock(side_effect=AssertionError("the full manifest validation ran on the install path"))
        with mock.patch.object(validator, "_git_tracked_artifacts", census), mock.patch.object(extractor, "validate_manifest", full), \
                mock.patch.object(Path, "open", path_open), mock.patch.object(os, "open", os_open), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            code = extractor.main(self._argv("--record-only"))
        self.assertEqual(code, 0)
        census.assert_not_called()
        full.assert_not_called()
        member = next(m for m in _ffmpeg_record()["selected_members"] if m["output_name"] == "ffmpeg.exe")
        self.assertEqual(hashlib.sha256((self.out / "ffmpeg.exe").read_bytes()).hexdigest(), member["sha256"])
        allowed = {MANIFEST.resolve(), (ROOT / Path(_ffmpeg_record()["path"])).resolve()}
        out_root = self.out.resolve()
        stray = sorted({str(p) for p in opened if p.resolve() not in allowed and not p.resolve().is_relative_to(out_root)})
        self.assertEqual(stray, [], "the record-only install opened a file outside the manifest, the named archive and the output folder")

    def test_without_record_only_the_full_validation_still_runs(self) -> None:
        # The hosted workflows never pass --record-only: their full census check is unchanged.
        import io
        from unittest import mock
        from tools.repo_hygiene import extract_vendored_native_payload as extractor
        from tools.repo_hygiene.vendored_native_payloads import PayloadIntegrityError

        full = mock.Mock(side_effect=PayloadIntegrityError("full validation ran"))
        with mock.patch.object(extractor, "validate_manifest", full), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = extractor.main(self._argv())
        self.assertEqual(code, 1)
        full.assert_called_once()
        self.assertIn("full validation ran", err.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_the_transaction_folder_is_the_one_the_wrapper_names(self) -> None:
        # adversary 78d626ed-A1: the wrapper's timeout recovery opens .vendored-payload-transaction-<id> only, so the extractor must
        # publish through exactly that folder, and refuse an id that is not plain hex (no path can be smuggled in).
        import io
        from unittest import mock
        from tools.repo_hygiene import extract_vendored_native_payload as extractor
        from tools.repo_hygiene.vendored_native_payloads import PayloadIntegrityError

        tx = "0123456789abcdef0123456789abcdef"
        seen: list[str] = []

        def stop_after_publication(record, output_dir):
            seen.extend(p.name for p in Path(output_dir).iterdir())
            raise PayloadIntegrityError("stop after publication")

        with mock.patch.object(extractor, "verify_installed_payload", stop_after_publication), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = extractor.main(self._argv("--record-only", "--transaction-id", tx))
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn(f".vendored-payload-transaction-{tx}", seen)
        self.assertEqual(list(self.out.iterdir()), [], "the rollback restores the folder and removes its transaction")
        for bad in ("../escape", "0123456789ABCDEF0123456789ABCDEF", "short"):
            with self.subTest(transaction_id=bad), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(extractor.main(self._argv("--record-only", "--transaction-id", bad)), 1)
                self.assertIn("transaction id must be 32 lowercase hex characters", err.getvalue())

    def test_record_only_still_refuses_a_record_whose_member_hash_is_wrong(self) -> None:
        import io
        from unittest import mock
        from tools.repo_hygiene import extract_vendored_native_payload as extractor

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        record = next(r for r in manifest["payloads"] if r.get("id") == PAYLOAD_ID)
        record["selected_members"][0]["sha256"] = "0" * 64
        tampered = Path(self._tmp.name) / "tampered-manifest.json"
        tampered.write_text(json.dumps(manifest), encoding="utf-8")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = extractor.main(self._argv("--record-only", "--manifest", str(tampered)))
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn("sha256 mismatch", err.getvalue())
        self.assertEqual(list(self.out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
