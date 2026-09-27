import json
import os
import base64
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "tools" / "coordination" / "invoke-mlv-exclusive.ps1"
GEN_BUILDINFO = REPO_ROOT / "tools" / "gen-buildinfo.ps1"
ARTIFACT_DETECTOR = REPO_ROOT / "tools" / "profiling" / "detect-playback-artifacts.ps1"
GUI_SMOKE_RUNNER = REPO_ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"
GUI_SMOKE_PROCESS_BOUNDARY = (
    REPO_ROOT / "tools" / "profiling" / "gui-smoke-process-boundary.psm1"
)
GUI_SMOKE_COMPARER = REPO_ROOT / "tools" / "profiling" / "compare-release-gui-smoke-ab.ps1"
BUILD_RELEASE = REPO_ROOT / "tools" / "build-release.ps1"
RELEASE_RUNTIME_ADMISSION = REPO_ROOT / "tools" / "release-runtime-admission.psm1"
MAIN_WINDOW = REPO_ROOT / "platform" / "qt" / "MainWindow.cpp"
NEUTRAL_RECEIPT = REPO_ROOT / "tests" / "fixtures" / "receipts" / "neutral_look_assist_off_v4.marxml"
SEALED_REAL_CLIP_RECEIPT = REPO_ROOT / "receipts" / "sealed-real-clip-ab-71d9f494-20260820.json"
STDOUT_SENTINEL = "EXCLUSIVE_STDOUT_SENTINEL"
STDERR_SENTINEL = "EXCLUSIVE_STDERR_SENTINEL"


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_exclusive_runner_preserves_child_stdout(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-RepoRoot",
            str(repo_root),
            "-Owner",
            "pytest-stdout",
            "-FilePath",
            os.environ.get("COMSPEC", "cmd.exe"),
            "-ArgumentListBase64",
            base64.b64encode(json.dumps(["/d", "/c", "echo", STDOUT_SENTINEL]).encode()).decode(),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert STDOUT_SENTINEL in completed.stdout

    records_path = repo_root / ".claude-state" / "coordination" / "build-ownership.jsonl"
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8-sig").splitlines()]
    assert records[-1]["owner"] == "pytest-stdout"
    assert records[-1]["outcome"] == "succeeded"
    assert records[-1]["exitCode"] == 0


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_exclusive_runner_preserves_argument_boundaries(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    probe = (
        "import json, sys; print(json.dumps(sys.argv[1:])); "
        f"print({STDERR_SENTINEL!r}, file=sys.stderr)"
    )
    expected = ["my clip.mlv", "second"]
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-RepoRoot",
            str(repo_root),
            "-Owner",
            "pytest-arguments",
            "-FilePath",
            sys.executable,
            "-ArgumentListBase64",
            base64.b64encode(json.dumps(["-c", probe, *expected]).encode()).decode(),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == expected
    assert STDERR_SENTINEL in completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_exclusive_runner_preserves_child_environment_and_path(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    path_marker = tmp_path / "nested-tool-bin"
    path_marker.mkdir()
    sentinel = "exclusive-environment-preserved"
    child_environment = os.environ.copy()
    child_environment["MLVAPP_EXCLUSIVE_ENV_SENTINEL"] = sentinel
    child_environment["PATH"] = os.pathsep.join(
        [str(path_marker), child_environment.get("PATH", "")]
    )
    probe = (
        "import json, os; "
        "print(json.dumps({"
        "'sentinel': os.environ.get('MLVAPP_EXCLUSIVE_ENV_SENTINEL'), "
        "'path': os.environ.get('PATH', '')"
        "}))"
    )
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-RepoRoot",
            str(repo_root),
            "-Owner",
            "pytest-environment",
            "-FilePath",
            sys.executable,
            "-ArgumentListBase64",
            base64.b64encode(json.dumps(["-c", probe]).encode()).decode(),
        ],
        cwd=REPO_ROOT,
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout.strip())
    assert observed["sentinel"] == sentinel
    assert str(path_marker).casefold() in observed["path"].casefold()


@pytest.mark.skipif(os.name != "nt", reason="MLV-App release provenance is Windows-specific")
def test_buildinfo_header_overrides_qmake_time_sha(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    output = tmp_path / "build_buildinfo.h"
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(GEN_BUILDINFO),
            "-SrcRoot",
            str(REPO_ROOT),
            "-OutHeader",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    header = output.read_text(encoding="ascii")
    assert "#ifdef MLVAPP_GIT_SHA\n#undef MLVAPP_GIT_SHA\n#endif" in header
    assert f'#define MLVAPP_GIT_SHA "{head}"' in header
    assert f'#define MLVAPP_BUILD_SHA "{head}"' in header


@pytest.mark.skipif(os.name != "nt", reason="MLV-App playback detector is PowerShell-based")
def test_playback_detector_cadence_advisory_exits_cleanly(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    trace = tmp_path / "trace.log"
    trace.write_text(
        "\n".join(
            f"[2026-08-20T00:00:0{i}.000Z] [INFO] [0x1] "
            f"draw_frame_ready.begin display_frame={i} play_checked=1 position={i}"
            for i in range(1, 7)
        )
        + "\n",
        encoding="ascii",
    )
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ARTIFACT_DETECTOR),
            "-TraceLog",
            str(trace),
            "-CadenceAdvisory",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ARTIFACT-CHECK verdict=PASS" in completed.stdout
    assert "ARTIFACT-CADENCE cadence_advisory=1" in completed.stdout


@pytest.mark.skipif(os.name != "nt", reason="MLV-App GUI smoke runner is PowerShell-based")
def test_gui_smoke_legacy_option_set_omits_new_cli_flags(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    receipt = tmp_path / "locked.marxml"
    receipt.write_text(
        '<?xml version="1.0"?><receipt version="4"><lookAssistEnabled>0</lookAssistEnabled></receipt>',
        encoding="ascii",
    )
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(GUI_SMOKE_RUNNER),
            "-RepoRoot",
            str(REPO_ROOT),
            "-ExePath",
            str(GUI_SMOKE_RUNNER),
            "-ClipPath",
            str(GUI_SMOKE_RUNNER),
            "-Output",
            str(tmp_path / "result.json"),
            "-Receipt",
            str(receipt),
            "-NoLoop",
            "-DisableLookAssist",
            "-LegacyGuiSmokeOptions",
            "-DryRun",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    dry_run = json.loads(completed.stdout)
    arguments = dry_run["arguments"]
    assert "--drop-frame-mode" not in arguments
    assert "--loop" not in arguments
    assert "--no-look-assist" not in arguments
    assert arguments[arguments.index("--receipt") + 1] == str(receipt)
    assert dry_run["validationPolicy"]["legacyGuiSmokeOptions"] is True
    assert dry_run["validationPolicy"]["requireLookAssist"] is False

    refused = subprocess.run(
        [
            *completed.args,
            "-TargetPresentedFrames",
            "2",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused.returncode != 0
    refused_text = " ".join((refused.stdout + refused.stderr).split())
    assert "cannot be combined with" in refused_text
    assert "-LegacyGuiSmokeOptions" in refused_text


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_gui_smoke_process_boundary_kills_hung_process_tree(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    child_pid_path = tmp_path / "child.pid"
    module_literal = str(GUI_SMOKE_PROCESS_BOUNDARY).replace("'", "''")
    child_pid_literal = str(child_pid_path).replace("'", "''")
    parent_code = (
        "import pathlib, subprocess, time; "
        f"p=subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(p.pid), encoding='ascii'); "
        "time.sleep(60)"
    )
    parent_code_literal = parent_code.replace("'", "''")
    python_literal = str(sys.executable).replace("'", "''")
    command = rf"""
Import-Module '{module_literal}' -Force
$childPidPath = '{child_pid_literal}'
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = '{python_literal}'
$start.UseShellExecute = $false
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
[void]$start.ArgumentList.Add('-c')
[void]$start.ArgumentList.Add('{parent_code_literal}')
$parent = [Diagnostics.Process]::new()
$parent.StartInfo = $start
[void]$parent.Start()
$stdoutTask = $parent.StandardOutput.ReadToEndAsync()
$stderrTask = $parent.StandardError.ReadToEndAsync()
$deadline = [DateTime]::UtcNow.AddSeconds(5)
while (-not (Test-Path -LiteralPath $childPidPath) -and [DateTime]::UtcNow -lt $deadline) {{
    Start-Sleep -Milliseconds 25
}}
if (-not (Test-Path -LiteralPath $childPidPath)) {{ throw 'child pid was not published' }}
$childPid = [int](Get-Content -LiteralPath $childPidPath -Raw)
$result = Wait-GuiSmokeProcessBounded -Process $parent -StandardOutputTask $stdoutTask -StandardErrorTask $stderrTask -TimeoutMs 500 -TerminationGraceMs 5000 -StreamDrainMs 5000
Start-Sleep -Milliseconds 200
[pscustomobject]@{{
    result = $result
    childPid = $childPid
    childAlive = [bool](Get-Process -Id $childPid -ErrorAction SilentlyContinue)
}} | ConvertTo-Json -Depth 8 -Compress
"""
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(completed.stdout.strip())
    boundary = observed["result"]
    assert boundary["timedOut"] is True
    assert boundary["exitCode"] == 124
    assert boundary["treeKillAttempted"] is True
    assert boundary["treeKillSucceeded"] is True
    assert boundary["terminationConfirmed"] is True
    assert boundary["stdoutDrained"] is True
    assert boundary["stderrDrained"] is True
    assert observed["childAlive"] is False


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_gui_smoke_process_boundary_no_longer_accepts_a_sampling_callback(tmp_path: Path) -> None:
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 7 (sol + astra MAJOR -- "the new budget does not
    # bound collection as a whole, and a non-returning sampler prevents both timeout handling and
    # receipt publication"): rounds 3-6 gave this module a -SampleIntervalMs/-OnSample pair so
    # run-release-gui-smoke.ps1 could sample host load DURING the wait, invoked synchronously
    # inside this module's own scope with no way to preempt a callback that never returned. Item 1
    # (replacing per-process CPU accounting with one GetSystemTimes syscall plus one process-handle
    # read) made a host-load sample cheap and self-bounded enough that the sampling loop moved OUT
    # of this module entirely and into run-release-gui-smoke.ps1's own scope, sampling inline
    # between chunked waits it drives itself -- see that script's sampling loop, immediately before
    # its (now parameter-less-for-sampling) call into this function. This function goes back to
    # being exactly its round-3 default branch: one bounded wait, then the stream-drain/kill-tree
    # finalize. Passing -SampleIntervalMs/-OnSample now fails to bind (the parameters no longer
    # exist) -- proving the removal is real, not merely unused.
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    module_literal = str(GUI_SMOKE_PROCESS_BOUNDARY).replace("'", "''")
    python_literal = str(sys.executable).replace("'", "''")
    command = rf"""
Import-Module '{module_literal}' -Force

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = '{python_literal}'
$start.UseShellExecute = $false
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
[void]$start.ArgumentList.Add('-c')
[void]$start.ArgumentList.Add('import time; time.sleep(0.2)')
$p = [Diagnostics.Process]::new()
$p.StartInfo = $start
[void]$p.Start()
$stdoutTask = $p.StandardOutput.ReadToEndAsync()
$stderrTask = $p.StandardError.ReadToEndAsync()
$onSample = {{ }}
$outcome = 'BOUND'
$errorMessage = ''
$errorId = ''
try {{
    Wait-GuiSmokeProcessBounded -Process $p -StandardOutputTask $stdoutTask -StandardErrorTask $stderrTask -TimeoutMs 3000 -TerminationGraceMs 3000 -StreamDrainMs 3000 -SampleIntervalMs 300 -OnSample $onSample | Out-Null
}} catch {{
    $outcome = 'REJECTED'
    $errorMessage = $_.Exception.Message
    $errorId = $_.FullyQualifiedErrorId
}}
if ($outcome -eq 'BOUND') {{ $p.Kill($true) }}

[pscustomobject]@{{
    outcome = $outcome
    errorMessage = $errorMessage
    errorId = $errorId
}} | ConvertTo-Json -Depth 8 -Compress
"""
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(completed.stdout.strip())
    assert observed["outcome"] == "REJECTED", observed
    error_message = observed["errorMessage"]
    assert "SampleIntervalMs" in error_message or "OnSample" in error_message, error_message
    assert observed["errorId"], observed
    assert "NamedParameterNotFound" in observed["errorId"], observed
    assert "Wait-GuiSmokeProcessBounded" in observed["errorId"], observed


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_gui_smoke_process_boundary_sanitizes_process_handle_exceptions(tmp_path: Path) -> None:
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 7 (sol MINOR -- "remaining process-boundary catches
    # still append raw Exception.Message"): three catches in Wait-GuiSmokeProcessBounded itself
    # (the main WaitForExit, Process.Kill, and the post-kill grace WaitForExit) still recorded the
    # raw exception message, same class already sanitized round 5/6 for OnSample and the
    # stdout/stderr task drain. A [Diagnostics.Process] that was constructed but never Start()'d
    # has no associated OS process -- WaitForExit/Kill both throw a real, natural (not mocked)
    # exception ("No process is associated with this object") for exactly this reason, giving a
    # live way to exercise all three catches in one call: the main wait fails closed into the
    # timeout branch, Kill then fails, and the post-kill grace wait fails identically.
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    module_literal = str(GUI_SMOKE_PROCESS_BOUNDARY).replace("'", "''")
    command = rf"""
Import-Module '{module_literal}' -Force

$p = [Diagnostics.Process]::new()
$stdoutTask = [System.Threading.Tasks.Task]::FromResult('')
$stderrTask = [System.Threading.Tasks.Task]::FromResult('')
$result = Wait-GuiSmokeProcessBounded -Process $p -StandardOutputTask $stdoutTask -StandardErrorTask $stderrTask -TimeoutMs 300 -TerminationGraceMs 300 -StreamDrainMs 3000

[pscustomobject]@{{
    failures = @($result.failures)
    timedOut = $result.timedOut
    exitCode = $result.exitCode
    treeKillAttempted = $result.treeKillAttempted
    treeKillSucceeded = $result.treeKillSucceeded
    terminationConfirmed = $result.terminationConfirmed
}} | ConvertTo-Json -Depth 8 -Compress
"""
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(completed.stdout.strip())
    failures = observed["failures"]
    if isinstance(failures, str):
        failures = [failures]
    joined = " ".join(failures)

    wait_failures = [f for f in failures if f.startswith("Process wait failed:")]
    assert len(wait_failures) == 1, failures
    wait_type_name = wait_failures[0].split(": ", 1)[1]
    assert " " not in wait_type_name, wait_failures[0]

    kill_failures = [f for f in failures if f.startswith("MLVApp process-tree termination failed:")]
    assert len(kill_failures) == 1, failures
    kill_type_name = kill_failures[0].split(": ", 1)[1]
    assert " " not in kill_type_name, kill_failures[0]

    postkill_failures = [f for f in failures if f.startswith("MLVApp post-kill wait failed:")]
    assert len(postkill_failures) == 1, failures
    postkill_type_name = postkill_failures[0].split(": ", 1)[1]
    assert " " not in postkill_type_name, postkill_failures[0]

    # All three arise from the identical root cause (no associated OS process), so they should be
    # the same underlying exception type -- a further structural check on the sanitized shape.
    assert wait_type_name == kill_type_name == postkill_type_name, failures

    # The sanitized shape must never leak the underlying .NET message text -- exactly what a
    # future catch reverting to raw $_.Exception.Message would reintroduce.
    assert "No process" not in joined, joined
    assert "associated" not in joined, joined

    # Unrelated invariants must still hold for this never-started-process case: the wait never
    # confirmed termination, so this is treated identically to a genuine hung-process timeout.
    assert observed["timedOut"] is True
    assert observed["exitCode"] == 124
    assert observed["treeKillAttempted"] is True
    assert observed["treeKillSucceeded"] is False
    assert observed["terminationConfirmed"] is False


@pytest.mark.skipif(os.name != "nt", reason="MLV-App process ownership is Windows-specific")
def test_gui_smoke_process_boundary_sanitizes_faulted_stdout_task_exception_message(tmp_path: Path) -> None:
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 6 (sol MINOR, same class as round 5's OnSample
    # sanitization above): Get-GuiSmokeTaskText's own catch block (used to drain BOTH the stdout
    # and stderr tasks) still recorded the raw $_.Exception.Message -- sol's exact repro: "a
    # faulted stdout task whose synthetic exception includes a sentinel path... result.failures
    # reproduces the raw sentinel." Passes a Task that is ALREADY faulted (constructed via
    # Task.FromException, carrying a sentinel path in its message) as -StandardOutputTask.
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    module_literal = str(GUI_SMOKE_PROCESS_BOUNDARY).replace("'", "''")
    python_literal = str(sys.executable).replace("'", "''")
    command = rf"""
Import-Module '{module_literal}' -Force

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = '{python_literal}'
$start.UseShellExecute = $false
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
[void]$start.ArgumentList.Add('-c')
[void]$start.ArgumentList.Add('import time; time.sleep(0.2)')
$p = [Diagnostics.Process]::new()
$p.StartInfo = $start
[void]$p.Start()
$sentinelException = [System.Exception]::new('stdout drain failed for host BACHELOR at C:\Users\owner\secret')
$faultedStdoutTask = [System.Threading.Tasks.Task]::FromException($sentinelException)
$stderrTask = $p.StandardError.ReadToEndAsync()
$result = Wait-GuiSmokeProcessBounded -Process $p -StandardOutputTask $faultedStdoutTask -StandardErrorTask $stderrTask -TimeoutMs 8000 -TerminationGraceMs 5000 -StreamDrainMs 5000

[pscustomobject]@{{
    stdoutDrained = $result.stdoutDrained
    failures = @($result.failures)
}} | ConvertTo-Json -Depth 8 -Compress
"""
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(completed.stdout.strip())
    assert observed["stdoutDrained"] is False
    failures = observed["failures"]
    assert any("stdout drain failed:" in f for f in failures), failures
    joined = " ".join(failures)
    assert "secret" not in joined
    assert "owner" not in joined
    assert "BACHELOR" not in joined
    assert "C:\\" not in joined


@pytest.mark.skipif(os.name != "nt", reason="MLV-App GUI smoke comparer is PowerShell-based")
def test_gui_smoke_ab_requires_same_last_presented_frame(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    clip = tmp_path / "clip.mlv"
    receipt = tmp_path / "locked.marxml"
    clip.write_bytes(b"same clip bytes")
    receipt.write_bytes(b"<receipt version=\"4\"/>")
    fixture_repo = tmp_path / "git-fixture"
    fixture_template = tmp_path / "git-template"
    fixture_hooks = fixture_template / "hooks"
    fixture_hooks.mkdir(parents=True)
    fixture_git_env = os.environ.copy()
    for key in tuple(fixture_git_env):
        if key.upper().startswith("GIT_"):
            fixture_git_env.pop(key)
    fixture_git_env["GIT_CONFIG_NOSYSTEM"] = "1"
    fixture_git_env["GIT_CONFIG_GLOBAL"] = os.devnull
    subprocess.run(
        [
            "git",
            "init",
            "--object-format=sha1",
            f"--template={fixture_template}",
            str(fixture_repo),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=fixture_git_env,
    )
    marker = fixture_repo / "marker.txt"
    commits: list[str] = []
    for index, value in enumerate(("before", "after")):
        marker.write_text(value, encoding="utf-8")
        subprocess.run(
            ["git", "add", "marker.txt"],
            cwd=fixture_repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=fixture_git_env,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=MLV-App Test",
                "-c",
                "user.email=mlv-app-test@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "-c",
                f"core.hooksPath={fixture_hooks}",
                "commit",
                "-m",
                f"fixture-{index}",
            ],
            cwd=fixture_repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=fixture_git_env,
        )
        commits.append(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=fixture_repo,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
                env=fixture_git_env,
            ).stdout.strip()
        )
    assert len(commits) == 2
    assert all(len(commit) == 40 and int(commit, 16) >= 0 for commit in commits)
    assert commits[0] != commits[1]
    trees = [
        subprocess.run(
            ["git", "show", "-s", "--format=%T", commit],
            cwd=fixture_repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=fixture_git_env,
        ).stdout.strip()
        for commit in commits
    ]
    assert all(len(tree) == 40 and int(tree, 16) >= 0 for tree in trees)
    assert trees[0] != trees[1]

    def write_smoke(path: Path, last_frame: int, build_sha: str) -> None:
        exe = tmp_path / f"{path.stem}.exe"
        log = tmp_path / f"{path.stem}.log"
        exe.write_bytes(f"exe:{build_sha}".encode("ascii"))
        log.write_bytes(f"log:{build_sha}".encode("ascii"))
        path.write_text(
            json.dumps(
                {
                    "repoRoot": str(fixture_repo),
                    "exePath": str(exe),
                    "clipPath": str(clip),
                    "launch": {
                        "arguments": [
                            "--input",
                            str(clip),
                            "--receipt",
                            str(receipt),
                        ]
                    },
                    "log": {
                        "path": str(log),
                        "runMetadata": {
                            "build_sha": build_sha,
                            "command_line": [
                                str(exe),
                                "--input",
                                str(clip),
                                "--receipt",
                                str(receipt),
                            ],
                        },
                    },
                    "visualQuality": {
                        "visualState": {
                            "look_assist_enabled": 0,
                            "temperature": 6000,
                            "tint": 0,
                            "raw_black": 20470,
                            "raw_white": 6000,
                            "chroma_smooth": 0,
                            "stretch_x": 3.0,
                            "stretch_y": 1.0,
                            "h_stretch_index": 0,
                            "v_stretch_index": 3,
                            "dual_iso_mode": 1,
                            "dual_iso_interp": 0,
                            "dual_iso_alias_map": 0,
                            "dual_iso_fullres": 1,
                            "drop_frame": 0,
                            "scale_request": 4,
                            "quality_mode": 2,
                            "receipt_supplied": 1,
                        },
                        "colorArtifactScan": {"verdict": "clear-heuristic"},
                    },
                    "validation": {
                        "ok": True,
                        "launchOnlyProbe": False,
                        "presentedFrames": 2,
                        "firstPresentedFrame": last_frame - 1,
                        "lastPresentedFrame": last_frame,
                    }
                }
            ),
            encoding="utf-8",
        )

    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    write_smoke(before, 104, commits[0])
    write_smoke(after, 105, commits[1])
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(GUI_SMOKE_COMPARER),
            "-Before",
            str(before),
            "-After",
            str(after),
            "-Output",
            str(tmp_path / "comparison.json"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    result = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8-sig"))
    assert result["verdict"] == "FAIL"
    assert result["presentedFrameEvidence"]["before"]["lastPresentedFrame"] == 104
    assert result["presentedFrameEvidence"]["after"]["lastPresentedFrame"] == 105
    assert not any("Declared Git binding failed" in failure for failure in result["failures"])
    assert any("not frame-locked" in failure for failure in result["failures"])


@pytest.mark.skipif(os.name != "nt", reason="MLV-App GUI smoke comparer is PowerShell-based")
def test_gui_smoke_ab_rejects_same_build_identity(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    build_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    clip = tmp_path / "clip.mlv"
    receipt = tmp_path / "locked.marxml"
    clip.write_bytes(b"same clip bytes")
    receipt.write_bytes(b"<receipt version=\"4\"/>")

    def write_smoke(path: Path, marker: str) -> None:
        exe = tmp_path / f"{marker}.exe"
        log = tmp_path / f"{marker}.log"
        exe.write_bytes(f"different executable {marker}".encode("ascii"))
        log.write_bytes(f"log {marker}".encode("ascii"))
        state = {
            "look_assist_enabled": 0,
            "temperature": 6000,
            "tint": 0,
            "raw_black": 20470,
            "raw_white": 6000,
            "chroma_smooth": 0,
            "stretch_x": 3.0,
            "stretch_y": 1.0,
            "h_stretch_index": 0,
            "v_stretch_index": 3,
            "dual_iso_mode": 1,
            "dual_iso_interp": 0,
            "dual_iso_alias_map": 0,
            "dual_iso_fullres": 1,
            "drop_frame": 0,
            "scale_request": 4,
            "quality_mode": 2,
            "receipt_supplied": 1,
        }
        path.write_text(
            json.dumps(
                {
                    "repoRoot": str(REPO_ROOT),
                    "exePath": str(exe),
                    "clipPath": str(clip),
                    "launch": {
                        "arguments": [
                            "--input",
                            str(clip),
                            "--receipt",
                            str(receipt),
                        ]
                    },
                    "log": {
                        "path": str(log),
                        "runMetadata": {
                            "build_sha": build_sha,
                            "command_line": [
                                str(exe),
                                "--input",
                                str(clip),
                                "--receipt",
                                str(receipt),
                            ],
                        },
                    },
                    "visualQuality": {
                        "visualState": state,
                        "colorArtifactScan": {"verdict": "clear-heuristic"},
                    },
                    "validation": {
                        "ok": True,
                        "launchOnlyProbe": False,
                        "presentedFrames": 2,
                        "firstPresentedFrame": 89,
                        "lastPresentedFrame": 90,
                    },
                }
            ),
            encoding="utf-8",
        )

    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    write_smoke(before, "before")
    write_smoke(after, "after")
    output = tmp_path / "comparison.json"
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(GUI_SMOKE_COMPARER),
            "-Before",
            str(before),
            "-After",
            str(after),
            "-Output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    result = json.loads(output.read_text(encoding="utf-8-sig"))
    assert result["verdict"] == "FAIL"
    assert any("same-arm A/B is forbidden" in failure for failure in result["failures"])


def test_release_builder_resolves_source_root_before_changing_directory() -> None:
    source = BUILD_RELEASE.read_text(encoding="utf-8")
    resolve_index = source.index("$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path")
    push_index = source.index("Push-Location $bd")
    assert resolve_index < push_index
    assert "Test-ReviewedReleaseRuntimeExtraName" in source
    assert "Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse" not in source
    assert "stale manifest, or unrelated file" in source


@pytest.mark.skipif(os.name != "nt", reason="Release packaging is PowerShell-based")
def test_release_runtime_donor_admission_is_exact_allowlist() -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    module_literal = str(RELEASE_RUNTIME_ADMISSION).replace("'", "''")
    names = [
        "pixel_maps",
        "releases",
        "cudart64_12.dll",
        "igpu_recon_cuda.dll",
        "igpu_recon_cuda.arch.json",
        "avcodec-61.dll",
        "swscale-8.dll",
        "MLVApp.exe",
        "MLVApp-deadbeef.exe",
        "build-manifest.json",
        "Qt6Core.dll",
        "unreviewed.dll",
        "avcodec-61.dll.exe",
        "AVFORMAT-61.DLL",
    ]
    names_json = json.dumps(names, separators=(",", ":")).replace("'", "''")
    command = rf"""
Import-Module '{module_literal}' -Force
$names = ConvertFrom-Json -InputObject '{names_json}'
$result = [ordered]@{{}}
foreach ($name in $names) {{
    $result[[string]$name] = [bool](Test-ReviewedReleaseRuntimeExtraName -Name ([string]$name))
}}
$result | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(completed.stdout)
    accepted = {name for name, value in observed.items() if value}
    assert accepted == {
        "pixel_maps",
        "releases",
        "cudart64_12.dll",
        "igpu_recon_cuda.dll",
        "igpu_recon_cuda.arch.json",
        "avcodec-61.dll",
        "swscale-8.dll",
    }


def test_gui_smoke_ab_v3_requires_clean_capture_time_evidence() -> None:
    comparer = GUI_SMOKE_COMPARER.read_text(encoding="utf-8")
    runner = GUI_SMOKE_RUNNER.read_text(encoding="utf-8")
    main_window = MAIN_WINDOW.read_text(encoding="utf-8")
    main_window_header = (REPO_ROOT / "platform" / "qt" / "MainWindow.h").read_text(
        encoding="utf-8"
    )
    main_cpp = (REPO_ROOT / "platform" / "qt" / "main.cpp").read_text(
        encoding="utf-8"
    )

    assert 'schema = "gui-smoke-ab-compare.v3"' in comparer
    assert 'schema = "mlvapp-gui-smoke-result.v2"' in runner
    assert "Assert-RecordedFileBinding" in comparer
    assert "Get-EmbeddedBuildStamp" in comparer
    assert "missing MZ header" in comparer
    assert "same-tree A/B is forbidden" in comparer
    assert "build manifest is dirty, unbound, or not independently runnable" in comparer
    assert "per-run log snapshot" in comparer
    assert 'Label "$Name screenshot event"' in comparer
    assert "screenshot bytes/frame/serial are not atomically bound" in comparer
    assert "MLVAPP_GUI_SMOKE_RUN_NONCE" in runner
    assert "logs-{0}-{1}" in runner
    assert "eventSha256" in runner
    assert "frame=%7 serial=%8 generation=%9" in main_window
    assert "QCryptographicHash::Sha256" in main_window
    assert "TargetPresentedFrames" in runner
    assert '"--presented-frames"' in runner
    assert "Wait-GuiSmokeProcessBounded" in runner
    assert "$process.WaitForExit()" not in runner
    assert "cannot be combined with -LegacyGuiSmokeOptions" in runner
    assert "treeKillSucceeded" in runner
    assert "terminationConfirmed" in runner
    assert "GUI smoke completed but did not write screenshot" in runner
    assert 'throw "GUI smoke completed but did not write screenshot' not in runner
    assert "targetPresentedFrames = 0" in main_window_header
    assert "m_playbackSmokeTargetPresentedFrames" in main_window_header
    assert 'QStringLiteral("presented-frames")' in main_cpp
    assert "exact presented-frame target was not met" in main_window
    assert "m_playbackSmokePresentedFrames >= m_playbackSmokeTargetPresentedFrames" in main_window
    note_start = main_window.index("void MainWindow::notePlaybackSmokePresentedFrame(")
    finish_start = main_window.index(
        "void MainWindow::finishPlaybackSmokeTelemetry", note_start
    )
    note_body = main_window[note_start:finish_start]
    stop_index = note_body.rindex("m_playbackSmokeTargetPresentedFrames > 0")
    assert stop_index > note_body.rindex("m_playbackSmokeRenderTotalSumMs +=")
    assert stop_index > note_body.rindex("m_playbackSmokeDrawTotalSumMs +=")
    assert "Accumulate the target frame before stopping" in note_body[stop_index:]
    stop_body = note_body[stop_index:]
    pin_index = stop_body.index("m_playbackInternalSliderAdvance = true")
    set_value_index = stop_body.index(
        "ui->horizontalSliderPosition->setValue(", pin_index
    )
    clear_guard_index = stop_body.index(
        "m_playbackInternalSliderAdvance = false", set_value_index
    )
    stop_playback_index = stop_body.index(
        "ui->actionPlay->setChecked( false )", clear_guard_index
    )
    assert "static_cast<int>( displayFrame )" in stop_body[
        set_value_index:clear_guard_index
    ]
    assert pin_index < set_value_index < clear_guard_index < stop_playback_index
    assert NEUTRAL_RECEIPT.read_text(encoding="utf-8") == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<receipt version="4">\n'
        '  <lookAssistEnabled>0</lookAssistEnabled>\n'
        '</receipt>\n'
    )


def test_sealed_real_clip_receipt_is_closed_and_source_bound() -> None:
    receipt = json.loads(SEALED_REAL_CLIP_RECEIPT.read_text(encoding="utf-8"))

    assert receipt["schema"] == "mlv-app/sealed-real-clip-ab-receipt/v1"
    assert receipt["disposition"] == (
        "LOCAL_SEALED_CORPUS_PASS_PENDING_HOSTED_AND_INDEPENDENT_RATIFICATION"
    )
    assert receipt["candidate"]["clean"] is True
    assert receipt["baseline"]["clean"] is True
    assert receipt["candidate"]["launchProbeExitCode"] == 0
    assert receipt["baseline"]["launchProbeExitCode"] == 0
    assert receipt["renderContract"]["targetPresentedFrames"] == 2
    assert receipt["verifier"]["failOnScreenshotDelta"] is True
    assert receipt["verifier"]["sourceHashConvention"] == (
        "GIT_BLOB_BYTES_CANONICAL_LF"
    )

    source_bindings = (
        (
            GUI_SMOKE_COMPARER,
            receipt["verifier"]["comparerSha256"],
            receipt["verifier"]["comparerGitBlob"],
        ),
        (
            GUI_SMOKE_RUNNER,
            receipt["verifier"]["runnerSha256"],
            receipt["verifier"]["runnerGitBlob"],
        ),
        (
            NEUTRAL_RECEIPT,
            receipt["renderContract"]["receiptSha256"],
            receipt["renderContract"]["receiptGitBlob"],
        ),
    )
    # Read each bound subject AT THE SEALED VERIFIER COMMIT, not out of the current
    # worktree.  A sealed receipt attests what ran AT SEAL TIME; asserting that today's
    # bytes still equal the seal does not strengthen that attestation -- it silently
    # converts every sealed receipt into a permanent FREEZE on the files it names.
    # There are two receipts here, so two freezes, and no future change to the smoke
    # runner could ever land.  sealed_real_clip_receipt._assert_source_binding already
    # binds at verifier["head"]; this loop was the only place that did not, and the two
    # disagreed: with an edited runner the library reported SEALED_RECEIPT_PASS while
    # this test failed on the very same tree.
    verifier_head = receipt["verifier"]["head"]
    # A sealed receipt names a HISTORICAL commit, and a shallow clone may simply not have
    # it -- CI's Factory Bridge Regressions job checked out at depth 1, which is exactly why
    # the original loop read the worktree instead: that worked in a shallow clone, at the
    # cost of the semantics.  Distinguish the two cases rather than papering over both:
    #   shallow clone + commit absent  -> missing INFRASTRUCTURE, skip loudly
    #   full clone   + commit absent   -> the receipt names history this repo does not have,
    #                                     which is a real defect and must fail
    # The workflow now checks this job out at fetch-depth: 0, so the strict path is the one
    # CI actually takes; without that pairing this skip would quietly retire the check.
    head_known = subprocess.run(
        ["git", "cat-file", "-e", f"{verifier_head}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    ).returncode == 0
    if not head_known:
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        assert shallow == "true", (
            f"receipt names verifier commit {verifier_head}, which is absent from a "
            f"NON-shallow clone -- the seal points at history this repository does not have"
        )
        pytest.skip(
            f"sealed verifier commit {verifier_head} is not present in this shallow clone; "
            f"check out with fetch-depth: 0 to verify the source bindings"
        )

    # The head is now the anchor for every binding below, so it has to be pinned itself.
    # Without this a receipt could name ANY commit whose copies of the three subjects
    # happen to match, and the bindings would still pass -- verified by control probe.
    # sealed_real_clip_receipt makes the same head/tree check for the same reason.
    assert (
        subprocess.run(
            ["git", "rev-parse", f"{verifier_head}^{{tree}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == receipt["verifier"]["tree"]
    )
    for path, expected_sha256, expected_git_blob in source_bindings:
        rel = path.relative_to(REPO_ROOT).as_posix()
        # Git objects are stored with LF, which is exactly the receipt's declared
        # GIT_BLOB_BYTES_CANONICAL_LF convention, so no autocrlf-dependent worktree
        # representation is involved and no normalization is needed.
        canonical_bytes = subprocess.run(
            ["git", "cat-file", "blob", f"{verifier_head}:{rel}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert (
            hashlib.sha256(canonical_bytes).hexdigest().upper()
            == expected_sha256
        )
        git_blob_header = f"blob {len(canonical_bytes)}\0".encode("ascii")
        git_blob_hasher = hashlib.sha1(usedforsecurity=False)
        git_blob_hasher.update(git_blob_header)
        git_blob_hasher.update(canonical_bytes)
        assert git_blob_hasher.hexdigest() == expected_git_blob

    clips = receipt["clips"]
    assert [clip["name"] for clip in clips] == [
        "M16-1347",
        "M16-1327",
        "M17-1207",
        "M15-1320",
        "M16-1210",
        "M16-1243",
    ]
    assert all(clip["verdict"] == "PASS" for clip in clips)
    assert all(clip["sameScreenshotSha256"] is True for clip in clips)
    assert all(clip["meanAbsRgbDelta"] == 0.0 for clip in clips)
    assert all(clip["maxAbsRgbDelta"] == 0 for clip in clips)
    assert all(clip["changedSampleRatio"] == 0.0 for clip in clips)
    assert all(clip["meanAbsRgbDelta"] <= 2.0 for clip in clips)
    assert all(clip["changedSampleRatio"] <= 0.01 for clip in clips)
    assert all(len(clip["comparisonJsonSha256"]) == 64 for clip in clips)
    assert receipt["coverage"]["availableAndPassed"] == len(clips)
    assert receipt["coverage"]["unavailable"] == ["M02-1344"]
    assert receipt["coverage"]["allAvailablePassed"] is True

    assert receipt["retention"]["rawArtifactsCommitted"] is False
    assert receipt["retention"]["hostedPromotionRequired"] is True
    assert receipt["scope"] == {
        "providerAuthority": False,
        "automaticLaunchGate": "CLOSED",
        "mergeAuthority": False,
        "releaseAuthority": False,
        "reblessAuthority": False,
        "humanBeforeAfterReviewRequired": True,
        "independentAuditRequired": True,
    }
