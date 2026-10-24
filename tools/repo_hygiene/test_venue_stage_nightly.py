"""Tests for UM-REGULAR-BASELINE-STAGE-1: the tracked nightly venue stage driver tools/profiling/dual-venue/Invoke-VenueStageNightly.ps1 and the
promoted single-use JobId reuse helpers tools/profiling/dual-venue/VenueStageReuse.psm1.

Owner directive 2026-10-18 (Layi): "I'm curious how playback performs on ultramagnus so id like regular benchmarking there as well when possible". The baseline
(Invoke-VenueBaseline.ps1) only FINDS a staged build (BUILD_NOT_STAGED, exit 22, when it is not there); this driver puts fork/master there at most once per night by the
four tracked staging steps (DLL pair on Ultra-Magnus, local assemble under build-guard, stage job, smoke-runner stage job).

WHAT IS PINNED
  * idempotency: a sha whose cache build.json (well-formed, right sourceCommit), three named artifacts and smoke-runner dir are all on the share is STAGE_SKIP_ALREADY;
    nothing is generated (except the local smoke-runner generator that names the runner dir), assembled or submitted, and the row carries the by-name cache hash;
  * the #508 shape: a default stage JobId whose outbox result says STAGE_OK while the cache does not hold the expected manifest is re-staged under a -JobIdSuffix,
    never reported "reused"; after every STAGE_OK the cache file is re-hashed BY NAME, and a mismatch is STAGE_VERIFY_MISMATCH, never success;
  * capacity refusals (disk floor, commit ceiling, build-guard) exit 3 and print the gate's own text verbatim (hub-autopilot re-arms on that text);
  * typed refusals: a missing gate script, a live claim or queued ticket, an unreachable share, an UNRESOLVED um-run (no retry), a second build in one night;
  * the reservation (CoS Dot b21fb3be): a STAGE_RESERVED row is persisted before the first staging step and holds the night for its sha whether or not the
    call ever finished; another sha that night is NIGHT_RESERVED (52), the same sha resumes and names the reservation it reconciled; a receipt append or read
    that fails is RECEIPT_IO_FAILED (51), never STAGE_OK/0; the venue mutex does not depend on the caller's receipt path, and the real venue share takes only
    the canonical receipt;
  * a DLL pair that is already built is harvested, not re-submitted; every child is bounded by -MaxTotalSec and killed past it;
  * -WhatIf prints the plan and builds, submits, probes and writes nothing.

Offline only: um-run, the three generators, the assembler and the two gate scripts are stubs passed through the driver's seams; the agent share, the claim directory,
the build root and the receipt file are temp directories. Each stub appends what it was called with to one calls log, so a test can assert "submitted nothing".
Nothing here reaches a venue, builds, starts MLVApp or touches the board's claim path. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
# STAGE_NIGHTLY_DRIVER lets the red-first run point the SAME tests at a fixture driver (the copied pre-card behaviour); CI never sets it.
DRIVER = Path(os.environ.get("STAGE_NIGHTLY_DRIVER") or (DV / "Invoke-VenueStageNightly.ps1"))
REUSE_MODULE = DV / "VenueStageReuse.psm1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
OTHER_SHA = "c" * 40
S12 = SHA[:12]
RUNNER_DIR = "smoke-runner-" + "b" * 16
EXIT = {"OK": 0, "USAGE": 2, "CAPACITY": 3, "AGENT_BUSY": 40, "SHARE_UNREACHABLE": 41, "SOURCE_COMMIT_UNRESOLVED": 42, "GATE_UNAVAILABLE": 43,
        "DLLPAIR_FAILED": 44, "ASSEMBLE_FAILED": 45, "STAGE_FAILED": 46, "STAGE_VERIFY_MISMATCH": 47, "SMOKE_RUNNER_STAGE_FAILED": 48,
        "STAGE_UNRESOLVED": 49, "STAGE_CAP_EXCEEDED": 50, "RECEIPT_IO_FAILED": 51, "NIGHT_RESERVED": 52}
NOW = "2026-10-19T21:00:00Z"      # the night key of this instant is 2026-10-20 (the next 08:00Z)
NIGHT = "2026-10-20"


def names(sha: str = SHA) -> dict[str, str]:
    s = sha[:12]
    return {"exe": f"MLVApp-playback-attr-3-cuda-{s}.exe", "dll": f"igpu_recon_cuda-playback-attr-3-cuda-{s}.dll",
            "pkg": f"MLVApp-playback-attr-3-cuda-{s}-pkg.zip", "build": f"playback-attr-3-cuda-{s}-build.json"}


# ---- stubs. Every one appends "<ROLE> k=v ..." to $env:FAKE_CALLS. --------------------------------------------------------------------------------------------
FAKE_DLLGEN = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$OutDir)
Add-Content -LiteralPath $env:FAKE_CALLS -Value "DLLGEN sha=$SourceCommit out=$OutDir"
$id = "playback-attr-3-cuda-dllpair-$($SourceCommit.Substring(0, 12))"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Content -LiteralPath (Join-Path $OutDir "$id.job.ps1") -Value '# fixture dll job'
Set-Content -LiteralPath (Join-Path $OutDir "$id-source.zip") -Value 'fixture zip'
"RESULT=DLL_JOB_EMITTED $id"
"""

FAKE_STAGEGEN = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$BuildDir, [string]$OutDir, [string]$AgentRoot, [string]$JobIdSuffix = '')
Add-Content -LiteralPath $env:FAKE_CALLS -Value "STAGEGEN sha=$SourceCommit build=$BuildDir out=$OutDir root=$AgentRoot suffix=$JobIdSuffix"
$id = "playback-attr-3-cuda-stage-$($SourceCommit.Substring(0, 12))"
if ($JobIdSuffix) { $id = "$id-$JobIdSuffix" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Content -LiteralPath (Join-Path $OutDir "$id.job.ps1") -Value '# fixture stage job'
"""

FAKE_SMOKEGEN = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$OutDir, [string]$AgentRoot)
Add-Content -LiteralPath $env:FAKE_CALLS -Value "SMOKEGEN sha=$SourceCommit out=$OutDir root=$AgentRoot"
$id = "attr3-stage-smoke-runner-$($SourceCommit.Substring(0, 12))-bbbbbbbbbbbb"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$job = Join-Path $OutDir "$id.job.ps1"
Set-Content -LiteralPath $job -Value '# fixture smoke-runner job'
"RESULT=SMOKE_RUNNER_STAGE_JOB_EMITTED SOURCE=$SourceCommit CLOSURE_DIGEST=$('b' * 64) CACHE_DIR_NAME=smoke-runner-$('b' * 16) JOB=$job"
"""

FAKE_ASSEMBLE = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$DllPairDir, [string]$OutDir)
Add-Content -LiteralPath $env:FAKE_CALLS -Value "ASSEMBLE sha=$SourceCommit dll=$DllPairDir out=$OutDir pid=$PID"
switch ($env:FAKE_ASSEMBLE) {
    'guard' { "BUILD_MEM_REFUSED label=attr3-assemble:$($SourceCommit.Substring(0, 12)) freePhysGB=1.2 freeCommitGB=2.3 need=6/8 waitedSec=1200 utc=fixture"; exit 3 }
    'fail' { 'RESULT=ASSEMBLE_FAILED STEP=make EXIT=15'; exit 15 }
    'sleep' { Start-Sleep -Seconds 300; exit 0 }
}
$s = $SourceCommit.Substring(0, 12)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
foreach ($n in "MLVApp-playback-attr-3-cuda-$s.exe", "igpu_recon_cuda-playback-attr-3-cuda-$s.dll", "MLVApp-playback-attr-3-cuda-$s-pkg.zip") {
    Set-Content -LiteralPath (Join-Path $OutDir $n) -Value "fixture $n"
}
$bj = Join-Path $OutDir "playback-attr-3-cuda-$s-build.json"
# assembledAtUtc makes every assemble a NEW manifest, exactly like the real assembler (assemble.ps1:411)
[IO.File]::WriteAllText($bj, (@{ sourceCommit = $SourceCommit; assembledAtUtc = [DateTime]::UtcNow.ToString('o'); nonce = [guid]::NewGuid().ToString('N') } | ConvertTo-Json -Compress))
$h = (Get-FileHash -LiteralPath $bj -Algorithm SHA256).Hash.ToLowerInvariant()
"RESULT=ASSEMBLE_OK SOURCE=$SourceCommit EXE=x DLL=x PKG=x MANIFEST=playback-attr-3-cuda-$s-build.json MANIFEST_SHA256=$h OUT=$OutDir"
exit 0
"""

# um-run.ps1 stand-in: the agent is simulated in-process. A JobId whose outbox result exists is refused exactly as UmRunDrop refuses it (UMRUN_JOBID_IN_USE).
# FAKE_UMRUN_<DLLPAIR|STAGE|SMOKE>: ok (default) | unresolved | retracted | fail | wrongcache (stage only: STAGE_OK, but the cache keeps other bytes) | inuse | inuse-all.
# The line also records the other two waits, the -NotAfterUtc it was handed (LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1; empty = none) and when it was called.
FAKE_UMRUN = r"""
[CmdletBinding()] param([string]$ScriptPath, [string[]]$SideFile = @(), [string]$JobId = '', [string]$AgentShare = '', [int]$TimeoutSec = 0,
    [int]$MaxQueueWaitSec = 0, [int]$MaxClaimedWaitSec = 0, [int]$MaxAdmissionWaitSec = 0, [string]$NotAfterUtc = '')
$sides = @($SideFile | ForEach-Object { ([string]$_) -split ';' } | Where-Object { $_ })
Add-Content -LiteralPath $env:FAKE_CALLS -Value "UMRUN job=$JobId share=$AgentShare script=$ScriptPath side=$($sides -join ';') timeout=$TimeoutSec queue=$MaxQueueWaitSec admission=$MaxAdmissionWaitSec claimed=$MaxClaimedWaitSec notAfter=$NotAfterUtc called=$([DateTime]::UtcNow.ToString('o'))"
$outbox = Join-Path $AgentShare 'outbox'; $cache = Join-Path $AgentShare 'cache'
New-Item -ItemType Directory -Force -Path $outbox, $cache | Out-Null
$res = Join-Path $outbox "$JobId.result.json"
if (Test-Path -LiteralPath $res) { throw "UMRUN_JOBID_IN_USE outbox already holds $JobId.result.json" }
$kind = $(if ($JobId -like 'playback-attr-3-cuda-dllpair-*') { 'DLLPAIR' } elseif ($JobId -like 'playback-attr-3-cuda-stage-*') { 'STAGE' } else { 'SMOKE' })
# FAKE_RECEIPT_READONLY=<kind>: this submission makes the driver's receipt file read-only, so every later append of that call fails (FAKE_RECEIPT names it)
if ($env:FAKE_RECEIPT_READONLY -ceq $kind) {
    if (-not (Test-Path -LiteralPath $env:FAKE_RECEIPT)) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $env:FAKE_RECEIPT) | Out-Null; [IO.File]::WriteAllText($env:FAKE_RECEIPT, '') }
    (Get-Item -LiteralPath $env:FAKE_RECEIPT).IsReadOnly = $true
}
$mode = [Environment]::GetEnvironmentVariable("FAKE_UMRUN_$kind"); if (-not $mode) { $mode = 'ok' }
# inuse: the unsuffixed JobId turns out spent at submission (a racing submitter won it); inuse-all: every JobId does
if ($mode -eq 'inuse-all' -or ($mode -eq 'inuse' -and $JobId -notmatch '-n\d{12}$')) { throw "UMRUN_JOBID_IN_USE outbox already holds $JobId.result.json" }
$sha = $env:FAKE_SHA
if ($mode -eq 'unresolved') { throw "UNRESOLVED: $JobId was claimed by the agent, but it stopped proving liveness (fixture) -- this client is stopping, not failing" }
if ($mode -eq 'retracted') { throw "RETRACTED: $JobId reached its queue wait ceiling (fixture); not a failure, retry with a new -JobId if desired" }
$code = 0
if ($mode -eq 'fail') { $code = 5; $out = 'RESULT=STAGE_FAILED STEP=fixture EXIT=5' }
elseif ($kind -eq 'DLLPAIR') {
    $art = Join-Path $outbox "$JobId.artifacts"
    New-Item -ItemType Directory -Force -Path $art | Out-Null
    [IO.File]::WriteAllText((Join-Path $art 'dll-pair-manifest.json'), (@{ sourceCommit = $sha; jobId = $JobId } | ConvertTo-Json -Compress))
    $out = "RESULT=DLL_PAIR_OK SOURCE=$sha ARCH=sm_86 RECON=x AMAZE=x ARTIFACTS=$art"
} elseif ($kind -eq 'STAGE') {
    foreach ($f in $sides) { Copy-Item -LiteralPath $f -Destination (Join-Path $cache (Split-Path -Leaf $f)) -Force }
    if ($mode -eq 'wrongcache') {
        $bj = @($sides | Where-Object { $_ -like '*-build.json' })[0]
        [IO.File]::WriteAllText((Join-Path $cache (Split-Path -Leaf $bj)), '{"sourceCommit":"' + $sha + '","assembledAtUtc":"an-earlier-assemble"}')
    }
    $out = "RESULT=STAGE_OK SOURCE=$sha FILES=4 CACHE=$cache ARTIFACTS=x"
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $cache "smoke-runner-$('b' * 16)") | Out-Null
    $out = "RESULT=SMOKE_RUNNER_STAGE_OK CACHE_DIR=smoke-runner-$('b' * 16) PATH=x FILES=8"
}
$r = [ordered]@{ jobId = $JobId; exitCode = $code; stdout = $out; stderr = ''; timedOut = $false; cancelled = $false; queueExpired = $false; orphaned = $false
    startedUtc = [DateTime]::UtcNow.ToString('o'); endedUtc = [DateTime]::UtcNow.ToString('o') }
[IO.File]::WriteAllText($res, ($r | ConvertTo-Json -Compress))
[pscustomobject]$r
"""

FAKE_DISK_FLOOR = r"""
function Test-DiskFloor {
    param([double]$FreeGiB = -1, [double]$FloorGiB = 50, [string]$Caller = '', [string]$Card = '', [string]$ExemptReason = '')
    Add-Content -LiteralPath $env:FAKE_CALLS -Value "DISKFLOOR caller=$Caller card=$Card"
    if ($env:FAKE_DISK -eq 'refuse') {
        return [pscustomobject]@{ Ok = $false; FreeGiB = 42.1; FloorGiB = 50; Message = "DISK_FLOOR_REFUSED caller=$Caller card=$Card freeGiB=42.1 floorGiB=50 utc=fixture -- C: is under the long-run floor; free space first and re-dispatch" }
    }
    [pscustomobject]@{ Ok = $true; FreeGiB = 61.5; FloorGiB = 50; Message = "DISK_FLOOR_OK caller=$Caller card=$Card freeGiB=61.5 floorGiB=50 utc=fixture" }
}
"""

FAKE_COMMIT_GATE = r"""
function Test-HostCommitGate {
    param([double]$CommitGiB = -1, [double]$LimitGiB = -1, [double]$RamGiB = -1, [string]$Caller = '', [string]$Card = '', [string]$ExemptReason = '', [scriptblock]$Probe = $null)
    Add-Content -LiteralPath $env:FAKE_CALLS -Value "COMMITGATE caller=$Caller card=$Card"
    if ($env:FAKE_COMMIT -eq 'refuse') {
        return [pscustomobject]@{ Ok = $false; Code = 'HOST_COMMIT_REFUSED'; CommitGiB = 58.2; LimitGiB = 60; CeilingGiB = 56; Message = "HOST_COMMIT_REFUSED caller=$Caller card=$Card commitGiB=58.2 limitGiB=60 ceilingGiB=56 utc=fixture -- shared host over the commit ceiling; REFUSED: commit 97 % of limit" }
    }
    [pscustomobject]@{ Ok = $true; Code = 'HOST_COMMIT_OK'; CommitGiB = 20.0; LimitGiB = 60; CeilingGiB = 56; Message = "HOST_COMMIT_OK caller=$Caller card=$Card commitGiB=20 limitGiB=60 ceilingGiB=56 utc=fixture" }
}
"""


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def process_start_iso(pid: int) -> str:
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')"],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return out.stdout.strip()


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    return str(pid) in out.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_windows_pwsh
class VenueStageNightlyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="um-stage-")
        self.addCleanup(tmp.cleanup)
        self.case = Path(tmp.name)
        self.receipt = self.case / ".claude-state" / "dual-venue" / "stage-nightly.jsonl"
        self.claims = self.case / "claims"
        self.share = self.case / "share"
        (self.share / "cache").mkdir(parents=True)
        (self.share / "outbox").mkdir()
        self.build_root = self.case / "lane-um-nightly-stage"
        self.gates = self.case / "gates"
        self.gates.mkdir()
        (self.gates / "disk-floor.ps1").write_text(FAKE_DISK_FLOOR, encoding="utf-8")
        (self.gates / "host-commit-gate.ps1").write_text(FAKE_COMMIT_GATE, encoding="utf-8")
        self.stubs = self.case / "stubs"
        self.stubs.mkdir()
        for name, text in (("um-run.ps1", FAKE_UMRUN), ("dll-gen.ps1", FAKE_DLLGEN), ("assemble.ps1", FAKE_ASSEMBLE), ("stage-gen.ps1", FAKE_STAGEGEN),
                           ("smoke-gen.ps1", FAKE_SMOKEGEN)):
            (self.stubs / name).write_text(text, encoding="utf-8")
        self.calls_log = self.case / "calls.log"
        # The driver refuses a -BuildRoot inside its checkout. A lane's TEMP can sit inside the worktree (.claude-state\ls), so every case passes its own
        # empty git checkout through the -RepoRoot seam: the refusal is then judged against that checkout, never against where TEMP happens to live.
        self.checkout = self.case / "checkout"
        self.checkout.mkdir()
        subprocess.run(["git", "-C", str(self.checkout), "init", "-q"], check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)

    # ---------------------------------------------------------------- harness
    def args(self, *extra: str, share: Path | None = None, commit: str | None = SHA, now: str | None = NOW, driver: Path | None = None,
             repo: Path | None = None) -> list[str]:
        a = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver or DRIVER), "-RepoRoot", str(repo or self.checkout),
             "-ReceiptFile", str(self.receipt), "-BuildRoot", str(self.build_root),
             "-AgentShare", str(share or self.share), "-ClaimDir", str(self.claims), "-GateDir", str(self.gates),
             "-UmRunScript", str(self.stubs / "um-run.ps1"), "-DllJobScript", str(self.stubs / "dll-gen.ps1"), "-AssembleScript", str(self.stubs / "assemble.ps1"),
             "-StageJobScript", str(self.stubs / "stage-gen.ps1"), "-SmokeRunnerJobScript", str(self.stubs / "smoke-gen.ps1"), "-ProbeTimeoutSec", "20"]
        if commit:
            a += ["-SourceCommit", commit]
        if now:
            a += ["-NowUtc", now]
        return a + list(extra)

    def run_driver(self, *extra: str, timeout: int = 300, env: dict[str, str] | None = None, **kw) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        e.update({"FAKE_CALLS": str(self.calls_log), "FAKE_SHA": kw.get("commit") or SHA})
        e.update(env or {})
        return subprocess.run(self.args(*extra, **kw), capture_output=True, text=True, timeout=timeout, env=e, stdin=subprocess.DEVNULL)

    def calls(self, role: str | None = None) -> list[str]:
        lines = self.calls_log.read_text(encoding="utf-8").splitlines() if self.calls_log.exists() else []
        return [l for l in lines if role is None or l.startswith(role + " ")]

    def submissions(self) -> list[str]:
        return self.calls("UMRUN")

    def submitted_jobs(self) -> list[str]:
        return [re.search(r"job=(\S+)", c).group(1) for c in self.submissions()]

    def submitted_sides(self) -> dict[str, list[str]]:
        return {re.search(r"job=(\S+)", c).group(1): [s for s in re.search(r" side=(.*?) timeout=", c).group(1).split(";") if s] for c in self.submissions()}

    def assert_no_jobid_twice(self) -> None:
        # SameJobIdRulingPremiseTests (test_attr3_footage_stage.py) trusts this driver as a um-run caller because it never submits one JobId twice in a call.
        jobs = self.submitted_jobs()
        self.assertEqual(len(jobs), len(set(jobs)), jobs)

    def rows(self) -> list[dict]:
        return [json.loads(l) for l in self.receipt.read_text(encoding="utf-8").splitlines() if l.strip()] if self.receipt.exists() else []

    def result(self, stdout: str) -> dict:
        line = [l for l in stdout.splitlines() if l.startswith("STAGE_NIGHTLY_RESULT=")]
        self.assertTrue(line, stdout)
        return json.loads(line[-1][len("STAGE_NIGHTLY_RESULT="):])

    def assert_exit(self, p: subprocess.CompletedProcess, token: str) -> None:
        code = 0 if token in ("STAGE_OK", "STAGE_SKIP_ALREADY", "NIGHT_ALREADY_BUILT", "PLAN_ONLY") else \
            3 if token in ("DISK_FLOOR_REFUSED", "HOST_COMMIT_REFUSED", "BUILD_GUARD_REFUSED") else EXIT[token]
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        tail = [l for l in p.stdout.splitlines() if l.strip()][-2:]
        self.assertTrue(tail[0].startswith(f"STAGE_NIGHTLY_EXIT code={code} reason={token} "), tail)
        self.assertTrue(tail[1].startswith("STAGE_NIGHTLY_RESULT="), tail)

    def nothing_built_or_submitted(self) -> None:
        for role in ("DLLGEN", "ASSEMBLE", "STAGEGEN", "UMRUN"):
            self.assertEqual(self.calls(role), [], role)

    def stage_cache(self, sha: str = SHA, runner: bool = True, artifacts: bool = True, body: bytes | None = None) -> Path:
        n = names(sha)
        bj = self.share / "cache" / n["build"]
        bj.write_bytes(body if body is not None else json.dumps({"sourceCommit": sha, "assembledAtUtc": "2026-10-19T01:02:03Z"}).encode())
        if artifacts:
            for k in ("exe", "dll", "pkg"):
                (self.share / "cache" / n[k]).write_bytes(b"x")
        if runner:
            (self.share / "cache" / RUNNER_DIR).mkdir(exist_ok=True)
        return bj

    def built_dll_pair(self, sha: str = SHA) -> Path:
        jid = f"playback-attr-3-cuda-dllpair-{sha[:12]}"
        art = self.share / "outbox" / f"{jid}.artifacts"
        art.mkdir(parents=True)
        (art / "dll-pair-manifest.json").write_text(json.dumps({"sourceCommit": sha, "jobId": jid}), encoding="utf-8")
        (self.share / "outbox" / f"{jid}.result.json").write_text(json.dumps({"jobId": jid, "exitCode": 0, "stdout": f"RESULT=DLL_PAIR_OK SOURCE={sha} ARCH=sm_86",
                                                                              "timedOut": False, "endedUtc": "2026-10-18T10:00:00Z"}), encoding="utf-8")
        return art

    def assembled_manifest_sha(self, stdout_or_calls: str | None = None) -> str:
        bj = list(self.build_root.glob(f"build-{S12}*/playback-attr-3-cuda-{S12}-build.json"))
        self.assertEqual(len(bj), 1, bj)
        return sha256(bj[0])

    # ---------------------------------------------------------------- the plan (-WhatIf)
    def test_whatif_prints_the_four_steps_and_touches_nothing(self) -> None:
        p = self.run_driver("-WhatIf", share=self.case / "no-such-share")
        self.assert_exit(p, "PLAN_ONLY")
        self.assertIn(f"STAGE_NIGHTLY_PLAN venue=ultra-magnus sourceCommit={SHA} nightKey={NIGHT}", p.stdout)
        steps = re.findall(r"^STAGE_NIGHTLY_PLAN_STEP (\d)/4 (\S+)", p.stdout, re.M)
        self.assertEqual(steps, [("1", "dll-pair"), ("2", "assemble"), ("3", "stage"), ("4", "smoke-runner")], p.stdout)
        self.assertIn(str(self.build_root / f"build-{S12}"), p.stdout)
        self.assertIn(f"playback-attr-3-cuda-{S12}-build.json", p.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.build_root.exists())

    def test_default_source_commit_is_the_local_fork_master_ref(self) -> None:
        repo = self.case / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "unit@example.invalid")
        git("config", "user.name", "unit")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-q", "-m", "one")
        head = git("rev-parse", "HEAD")
        unresolved = self.run_driver("-WhatIf", repo=repo, commit=None)
        self.assert_exit(unresolved, "SOURCE_COMMIT_UNRESOLVED")
        git("update-ref", "refs/remotes/fork/master", head)
        ok = self.run_driver("-WhatIf", repo=repo, commit=None)
        self.assert_exit(ok, "PLAN_ONLY")
        self.assertIn(f"sourceCommit={head}", ok.stdout)

    def test_receipt_file_outside_claude_state_is_refused(self) -> None:
        if ".claude-state" in self.case.resolve().parts:
            self.skipTest("TEMP sits under a .claude-state directory (lane scratch), so no temp path is outside one; hosted CI runs this case")
        a = self.args()
        a[a.index("-ReceiptFile") + 1] = str(self.case / "elsewhere" / "stage.jsonl")
        p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=dict(os.environ, FAKE_CALLS=str(self.calls_log)), stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, EXIT["USAGE"], p.stdout + p.stderr)
        self.assertIn(".claude-state", p.stdout)
        self.assertEqual(self.calls(), [])

    # ---------------------------------------------------------------- a fresh night: the four tracked steps
    def test_a_fresh_sha_runs_the_four_steps_and_records_the_by_name_cache_hash(self) -> None:
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        self.assertEqual(len(self.calls("DLLGEN")), 1)
        self.assertEqual(len(self.calls("ASSEMBLE")), 1)
        jobs = [re.search(r"job=(\S+)", c).group(1) for c in self.submissions()]
        self.assertEqual(jobs, [f"playback-attr-3-cuda-dllpair-{S12}", f"playback-attr-3-cuda-stage-{S12}", f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"])
        for c in self.submissions():
            self.assertIn(f"share={self.share}", c)
        stage_call = self.submissions()[1]
        for n in names().values():
            self.assertIn(n, stage_call)      # the four inboxSideFiles travel with the stage job
        # the assembler built into <BuildRoot>\build-<sha12> from the DLL pair the agent published
        self.assertIn(f"out={self.build_root / ('build-' + S12)}", self.calls("ASSEMBLE")[0])
        self.assertIn(f"dll={self.share / 'outbox' / ('playback-attr-3-cuda-dllpair-' + S12 + '.artifacts')}", self.calls("ASSEMBLE")[0])
        cache_hash = sha256(self.share / "cache" / names()["build"])
        self.assertEqual(cache_hash, self.assembled_manifest_sha())
        rows = self.rows()
        # the reservation is persisted before the first staging step; the call's own row closes it
        self.assertEqual([x["outcome"] for x in rows], ["STAGE_RESERVED", "STAGE_OK"], rows)
        self.assertEqual((rows[0]["sourceCommit"], rows[0]["nightKey"], rows[0]["exitCode"]), (SHA, NIGHT, None))
        self.assertEqual(rows[1]["reservationId"], rows[0]["reservationId"])
        r = rows[1]
        self.assertEqual(r["schema"], "mlv-app/venue-stage-nightly/v1")
        for key in ("utc", "nightKey", "venue", "sourceCommit", "outcome", "exitCode", "buildManifestSha256", "smokeRunnerCacheDir", "jobIds", "timings",
                    "cFreeGiB", "commitGiB", "buildDir", "detail"):
            self.assertIn(key, r, key)
        self.assertEqual((r["outcome"], r["exitCode"], r["venue"], r["sourceCommit"], r["nightKey"]), ("STAGE_OK", 0, "ultra-magnus", SHA, NIGHT))
        self.assertEqual(r["buildManifestSha256"], cache_hash)
        self.assertEqual(r["smokeRunnerCacheDir"], RUNNER_DIR)
        self.assertEqual(r["jobIds"]["stage"], f"playback-attr-3-cuda-stage-{S12}")
        for k in ("dllPairSec", "assembleSec", "stageSec", "runnerSec", "totalSec"):
            self.assertIn(k, r["timings"], k)
        self.assertIn("start", r["cFreeGiB"])
        self.assertIn("end", r["cFreeGiB"])
        self.assertIn(f"manifest={cache_hash}", p.stdout)
        self.assertEqual(self.result(p.stdout)["buildManifestSha256"], cache_hash)
        # both floors were asked, before the first build step
        self.assertEqual(len(self.calls("DISKFLOOR")), 1)
        self.assertEqual(len(self.calls("COMMITGATE")), 1)
        log = self.calls()
        self.assertLess(log.index(self.calls("COMMITGATE")[0]), log.index(self.calls("ASSEMBLE")[0]))
        self.assert_no_jobid_twice()

    # ---------------------------------------------------------------- LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1: the notAfterUtc each submit hands um-run
    def test_the_stage_job_not_after_is_the_drivers_deadline_and_the_harvested_jobs_get_none(self) -> None:
        # The driver kills um-run at its -MaxTotalSec deadline, so the stage job (re-stageable under a suffix by a later call) gets that deadline as its
        # notAfterUtc: the remaining budget, never earlier than a wait it passes. The DLL pair and the smoke runner are harvested BY NAME by a later call
        # that cannot re-submit their single-use JobId, so they get the later bound: none (Get-DvJobNotAfter -LaterHarvest).
        t0 = datetime.now(timezone.utc)
        p = self.run_driver("-MaxTotalSec", "600")
        t1 = datetime.now(timezone.utc)
        self.assert_exit(p, "STAGE_OK")
        o_utc = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$")

        def parse_o(text: str) -> datetime:
            m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d{7})Z$", text)
            self.assertIsNotNone(m, text)
            return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(microseconds=int(m.group(2)[:6]))

        seen = {}
        for c in self.submissions():
            m = re.search(r" job=(\S+) .* timeout=(\d+) queue=(\d+) admission=(\d+) claimed=(\d+) notAfter=(\S*) called=(\S+)$", c)
            self.assertIsNotNone(m, c)
            seen[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), m.group(6), parse_o(m.group(7)))
        stage = seen[f"playback-attr-3-cuda-stage-{S12}"]
        timeout, queue, admission, claimed, not_after, called = stage
        self.assertRegex(not_after, o_utc, "the stage submit passes -NotAfterUtc in UTC 'o' form")
        na = parse_o(not_after)
        self.assertGreater(na, called)
        for w in (timeout, queue, admission, claimed):
            self.assertGreaterEqual(na, called + timedelta(seconds=w) - timedelta(seconds=1), f"never earlier than the {w} s wait it passes")
        # the deadline: startedUtc + 600, with startedUtc inside [t0, t1]; the submit floors what is left of it to whole seconds
        self.assertGreaterEqual(na, t0 + timedelta(seconds=600 - 1))
        self.assertLessEqual(na, t1 + timedelta(seconds=600))
        self.assertLessEqual(abs((na - called).total_seconds() - queue), 1.5, "notAfter is the remaining budget the queue wait is also given")
        for jid in (f"playback-attr-3-cuda-dllpair-{S12}", f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"):
            self.assertEqual(seen[jid][4], "", f"{jid} is harvested by name by a later call that cannot re-submit it: no notAfter")

    def test_the_driver_bounds_each_submit_through_the_one_helper(self) -> None:
        text = DRIVER.read_text(encoding="utf-8")
        fn = text[text.index("function Invoke-UmRun("):text.index("# ------------------------------------------------------------------ 6. DLL pair")]
        self.assertEqual(fn.count("Get-DvJobNotAfter"), 1)
        self.assertIn("-CallerCapSec $remain", fn)
        self.assertIn("-NotAfterUtc", fn)
        self.assertEqual(len(re.findall(r"Invoke-UmRun '(?:dllpair|smoke-runner)' .*-LaterHarvest", text)), 2, "the two harvested submits say so")
        self.assertEqual(len(re.findall(r"Invoke-UmRun 'stage' .*-LaterHarvest", text)), 0)

    # ---------------------------------------------------------------- the owner-footage gate: side files are the build package, never anything else
    def test_side_files_are_only_this_shas_build_package_under_the_build_root(self) -> None:
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        sides = self.submitted_sides()
        dll_id = f"playback-attr-3-cuda-dllpair-{S12}"
        self.assertEqual([Path(s).name for s in sides[dll_id]], [f"{dll_id}-source.zip"])
        stage = sides[f"playback-attr-3-cuda-stage-{S12}"]
        self.assertEqual(sorted(Path(s).name for s in stage), sorted(names().values()))
        for s in stage:
            self.assertEqual(Path(s).parent, self.build_root / f"build-{S12}", s)
        self.assertEqual(sides[f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"], [])
        for s in sides[dll_id] + stage:
            self.assertTrue(Path(s).resolve().is_relative_to(self.build_root.resolve()), s)

    SIDES_LINE = "$sides = @($names.packageZipName, $names.exeName, $names.reconName, $names.buildManifestName | ForEach-Object { Join-Path $buildDir $_ })"

    def assert_widened_side_files_are_refused(self, widened: str) -> None:
        # The driver's own refusal, exercised on a copy whose side-file list was widened (the copy loads the real modules from the real dual-venue dir).
        text = DRIVER.read_text(encoding="utf-8")
        here = "$here = $PSScriptRoot"
        self.assertEqual((text.count(here), text.count(self.SIDES_LINE)), (1, 1))
        mutated = self.case / "mutated-driver" / "Invoke-VenueStageNightly.ps1"
        mutated.parent.mkdir()
        mutated.write_text(text.replace(here, f"$here = '{DV}'").replace(self.SIDES_LINE, widened), encoding="utf-8")
        self.built_dll_pair()
        p = self.run_driver(driver=mutated)
        self.assert_exit(p, "STAGE_FAILED")
        self.assertIn("refused to hand um-run the side file", p.stdout)
        self.assertEqual(self.submissions(), [])

    def test_a_side_file_with_a_name_outside_the_build_package_is_refused_before_um_run(self) -> None:
        self.assert_widened_side_files_are_refused(self.SIDES_LINE + "; $sides += (Join-Path $buildDir 'not-the-build-package.txt')")

    def test_a_build_package_name_outside_the_build_root_is_refused_before_um_run(self) -> None:
        self.assert_widened_side_files_are_refused(self.SIDES_LINE.replace("Join-Path $buildDir $_", f"Join-Path '{self.case / 'elsewhere'}' $_"))

    # ---------------------------------------------------------------- the same-JobId premise: never one JobId twice in a call
    def test_a_stage_jobid_in_use_at_submission_is_restaged_once_under_a_new_id(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "inuse"})
        self.assert_exit(p, "STAGE_OK")
        stage_jobs = [j for j in self.submitted_jobs() if j.startswith("playback-attr-3-cuda-stage-")]
        self.assertEqual(len(stage_jobs), 2, stage_jobs)
        self.assertEqual(stage_jobs[0], f"playback-attr-3-cuda-stage-{S12}")
        self.assertRegex(stage_jobs[1], rf"^playback-attr-3-cuda-stage-{S12}-n\d{{12}}$")
        self.assert_no_jobid_twice()

    def test_a_second_in_use_refusal_is_stage_failed_with_no_third_submission(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "inuse-all"})
        self.assert_exit(p, "STAGE_FAILED")
        stage_jobs = [j for j in self.submitted_jobs() if j.startswith("playback-attr-3-cuda-stage-")]
        self.assertEqual(len(stage_jobs), 2, stage_jobs)
        self.assert_no_jobid_twice()
        self.assertFalse(any("smoke-runner" in j for j in self.submitted_jobs()))

    def test_a_dll_pair_already_built_is_harvested_not_resubmitted(self) -> None:
        art = self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        self.assertEqual(self.calls("DLLGEN"), [])
        self.assertFalse(any("dllpair" in c for c in self.submissions()), self.submissions())
        self.assertIn(f"dll={art}", self.calls("ASSEMBLE")[0])

    # ---------------------------------------------------------------- idempotency
    def test_an_already_staged_sha_is_skipped_with_no_build_and_no_submission(self) -> None:
        bj = self.stage_cache()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.nothing_built_or_submitted()
        self.assertEqual(len(self.calls("SMOKEGEN")), 1)       # the local generator only names the runner dir
        self.assertEqual(self.calls("DISKFLOOR"), [])          # nothing to build, so no floor is asked
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["exitCode"]), ("STAGE_SKIP_ALREADY", 0))
        self.assertEqual(r["buildManifestSha256"], sha256(bj))
        self.assertEqual(r["smokeRunnerCacheDir"], RUNNER_DIR)

    def test_a_staged_build_without_its_runner_stages_only_the_runner(self) -> None:
        bj = self.stage_cache(runner=False)
        before = sha256(bj)
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        for role in ("DLLGEN", "ASSEMBLE", "STAGEGEN"):
            self.assertEqual(self.calls(role), [], role)
        self.assertEqual([re.search(r"job=(\S+)", c).group(1) for c in self.submissions()], [f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"])
        self.assertEqual(self.rows()[-1]["buildManifestSha256"], before)

    # ---------------------------------------------------------------- the #508 shape (RED first)
    def assert_restaged_under_a_suffix(self, p: subprocess.CompletedProcess) -> None:
        self.assert_exit(p, "STAGE_OK")
        stage_jobs = [re.search(r"job=(\S+)", c).group(1) for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]
        self.assertEqual(len(stage_jobs), 1, self.submissions())
        self.assertRegex(stage_jobs[0], rf"^playback-attr-3-cuda-stage-{S12}-n\d{{12}}$")
        self.assertRegex(self.calls("STAGEGEN")[-1], r"suffix=n\d{12}$")
        self.assertNotIn("reused", p.stdout.lower().replace("not reused", ""))
        cache_hash = sha256(self.share / "cache" / names()["build"])
        self.assertEqual(cache_hash, self.assembled_manifest_sha())
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["buildManifestSha256"], r["jobIds"]["stage"]), ("STAGE_OK", cache_hash, stage_jobs[0]))

    def spend_default_stage_jobid(self) -> None:
        jid = f"playback-attr-3-cuda-stage-{S12}"
        (self.share / "outbox" / f"{jid}.result.json").write_text(json.dumps({"jobId": jid, "exitCode": 0, "timedOut": False,
                                                                              "stdout": f"RESULT=STAGE_OK SOURCE={SHA} FILES=4", "endedUtc": "2026-10-18T15:40:00Z"}), encoding="utf-8")

    def test_508_default_jobid_spent_and_cache_missing_restages_under_a_suffix(self) -> None:
        self.built_dll_pair()
        self.spend_default_stage_jobid()
        self.assert_restaged_under_a_suffix(self.run_driver())

    def test_508_default_jobid_spent_and_cache_holds_another_commit_restages_under_a_suffix(self) -> None:
        self.built_dll_pair()
        self.spend_default_stage_jobid()
        self.stage_cache(runner=False, body=json.dumps({"sourceCommit": OTHER_SHA}).encode())
        self.assert_restaged_under_a_suffix(self.run_driver())

    def test_a_cache_hash_that_differs_after_stage_ok_is_a_verify_mismatch(self) -> None:
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "wrongcache"})
        self.assert_exit(p, "STAGE_VERIFY_MISMATCH")
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["STAGE_RESERVED", "STAGE_VERIFY_MISMATCH"])
        self.assertIsNone(rows[-1]["buildManifestSha256"])
        self.assertIn("manifest=none", p.stdout)

    # ---------------------------------------------------------------- capacity (exit 3, the gate's text verbatim)
    def test_disk_floor_refusal_exits_3_with_the_gate_text_and_builds_nothing(self) -> None:
        p = self.run_driver(env={"FAKE_DISK": "refuse"})
        self.assert_exit(p, "DISK_FLOOR_REFUSED")
        self.assertIn("DISK_FLOOR_REFUSED caller=Invoke-VenueStageNightly card=venue-stage-nightly freeGiB=42.1 floorGiB=50 utc=fixture -- C: is under the long-run floor; free space first and re-dispatch", p.stdout.splitlines())
        self.nothing_built_or_submitted()
        self.assertEqual(self.rows()[-1]["outcome"], "DISK_FLOOR_REFUSED")

    def test_commit_ceiling_refusal_exits_3_with_the_gate_text_and_builds_nothing(self) -> None:
        p = self.run_driver(env={"FAKE_COMMIT": "refuse"})
        self.assert_exit(p, "HOST_COMMIT_REFUSED")
        line = [l for l in p.stdout.splitlines() if l.startswith("HOST_COMMIT_REFUSED ")]
        self.assertEqual(len(line), 1, p.stdout)
        self.assertTrue(line[0].endswith("REFUSED: commit 97 % of limit"), line)
        self.nothing_built_or_submitted()

    def test_a_missing_gate_script_is_a_typed_refusal_never_an_admit(self) -> None:
        for gone in ("disk-floor.ps1", "host-commit-gate.ps1"):
            with self.subTest(gone=gone):
                (self.gates / "disk-floor.ps1").write_text(FAKE_DISK_FLOOR, encoding="utf-8")
                (self.gates / "host-commit-gate.ps1").write_text(FAKE_COMMIT_GATE, encoding="utf-8")
                (self.gates / gone).unlink()
                self.calls_log.unlink(missing_ok=True)
                p = self.run_driver()
                self.assert_exit(p, "GATE_UNAVAILABLE")
                self.assertIn(gone, p.stdout)
                self.nothing_built_or_submitted()

    def test_a_build_guard_refusal_exits_3_with_its_line(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_ASSEMBLE": "guard"})
        self.assert_exit(p, "BUILD_GUARD_REFUSED")
        self.assertTrue(any(l.startswith(f"BUILD_MEM_REFUSED label=attr3-assemble:{S12} ") for l in p.stdout.splitlines()), p.stdout)
        self.assertFalse(any("stage" in c for c in self.submissions()), self.submissions())
        self.assertEqual(self.rows()[-1]["outcome"], "BUILD_GUARD_REFUSED")

    def test_an_assembler_failure_is_typed(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_ASSEMBLE": "fail"})
        self.assert_exit(p, "ASSEMBLE_FAILED")
        self.assertEqual(self.submissions(), [])

    # ---------------------------------------------------------------- the venue is in use / unreachable / unresolved
    def test_a_live_claim_is_agent_busy_and_nothing_is_submitted(self) -> None:
        self.claims.mkdir(parents=True)
        (self.claims / "ultra-magnus.claim.json").write_text(json.dumps({
            "schema": "mlv-app/venue-claim/v1", "venue": "ultra-magnus", "ownerPid": os.getpid(), "ownerCreatedUtc": process_start_iso(os.getpid()),
            "host": os.environ.get("COMPUTERNAME", ""), "runDir": "C:\\elsewhere\\lane-OTHER", "purpose": "fixture", "claimedUtc": utc_iso(datetime.now(timezone.utc)),
            "expiresUtc": utc_iso(datetime.now(timezone.utc) + timedelta(hours=3)), "nonce": "fixture-nonce"}), encoding="utf-8")
        p = self.run_driver()
        self.assert_exit(p, "AGENT_BUSY")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.rows()[-1]["outcome"], "AGENT_BUSY")

    def test_a_queued_ticket_is_agent_busy(self) -> None:
        mod = DV / "VenueChain.psm1"
        cmd = (f"Import-Module '{mod}' -DisableNameChecking; New-VenueTicket -Venue ultra-magnus -RunDir 'C:\\elsewhere\\lane-OTHER' -Purpose fixture "
               f"-ClaimDir '{self.claims}' -OwnerPid {os.getpid()} -StaleAfterSec 3600 | Out-Null")
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        p = self.run_driver()
        self.assert_exit(p, "AGENT_BUSY")
        self.assertEqual(self.calls(), [])

    def test_an_unreachable_share_is_typed_and_bounded(self) -> None:
        t0 = time.monotonic()
        p = self.run_driver(share=self.case / "no-such-share")
        self.assert_exit(p, "SHARE_UNREACHABLE")
        self.assertLess(time.monotonic() - t0, 90)
        self.assertEqual(self.calls(), [])

    def test_an_unresolved_um_run_is_typed_and_never_retried(self) -> None:
        p = self.run_driver(env={"FAKE_UMRUN_DLLPAIR": "unresolved"})
        self.assert_exit(p, "STAGE_UNRESOLVED")
        self.assertEqual(len(self.submissions()), 1, self.submissions())
        self.assertEqual(self.calls("ASSEMBLE"), [])
        self.assertEqual(self.rows()[-1]["outcome"], "STAGE_UNRESOLVED")

    def test_an_unresolved_stage_submission_is_typed_and_never_retried(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "unresolved"})
        self.assert_exit(p, "STAGE_UNRESOLVED")
        self.assertEqual(len([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]), 1)
        self.assertFalse(any("smoke-runner" in c for c in self.submissions()))

    def test_a_failed_stage_result_is_stage_failed(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "fail"})
        self.assert_exit(p, "STAGE_FAILED")

    # ---------------------------------------------------------------- once per night
    def seed_row(self, sha: str, outcome: str = "STAGE_OK", night: str = NIGHT, reservation_id: str | None = None) -> None:
        self.receipt.parent.mkdir(parents=True, exist_ok=True)
        row = {"schema": "mlv-app/venue-stage-nightly/v1", "utc": "2026-10-19T20:00:00Z", "nightKey": night, "venue": "ultra-magnus",
               "sourceCommit": sha, "outcome": outcome, "exitCode": None if outcome == "STAGE_RESERVED" else EXIT.get(outcome, 0)}
        if reservation_id:
            row["reservationId"] = reservation_id
        with self.receipt.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def test_a_second_build_in_one_night_is_refused_for_another_sha(self) -> None:
        self.seed_row(OTHER_SHA)
        p = self.run_driver()
        self.assert_exit(p, "NIGHT_ALREADY_BUILT")
        self.assertEqual(self.calls(), [])
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["exitCode"], r["nightKey"]), ("NIGHT_ALREADY_BUILT", 0, NIGHT))
        self.assertIn(OTHER_SHA, r["detail"])

    def test_the_same_sha_in_the_same_night_is_a_skip(self) -> None:
        self.seed_row(SHA)
        self.stage_cache()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.nothing_built_or_submitted()

    def test_a_build_from_an_earlier_night_does_not_block(self) -> None:
        self.seed_row(OTHER_SHA, night="2026-10-19")
        self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")

    def test_the_night_key_is_the_date_of_the_next_0800z(self) -> None:
        for now, night in (("2026-10-19T07:59:59Z", "2026-10-19"), ("2026-10-19T08:00:00Z", "2026-10-20"), ("2026-10-19T23:30:00Z", "2026-10-20")):
            with self.subTest(now=now):
                p = self.run_driver("-WhatIf", now=now)
                self.assertIn(f"nightKey={night}", p.stdout)

    # ---------------------------------------------------------------- the reservation and the fail-closed receipt (CoS Dot b21fb3be, PR #526)
    def clear_readonly(self) -> None:
        if self.receipt.is_file():
            os.chmod(self.receipt, stat.S_IREAD | stat.S_IWRITE)

    def receipt_goes_readonly_at_the_runner_submission(self) -> dict[str, str]:
        # the stage has landed in the cache by then; only the receipt append of the call's end can fail
        self.addCleanup(self.clear_readonly)
        return {"FAKE_RECEIPT_READONLY": "SMOKE", "FAKE_RECEIPT": str(self.receipt)}

    def test_a_receipt_append_failure_after_staging_is_a_typed_nonzero_exit_never_stage_ok(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env=self.receipt_goes_readonly_at_the_runner_submission())
        self.assert_exit(p, "RECEIPT_IO_FAILED")
        self.assertNotIn("reason=STAGE_OK", p.stdout)
        self.assertEqual(self.result(p.stdout)["unpersistedOutcome"], "STAGE_OK")
        self.clear_readonly()
        rows = self.rows()
        # the reservation written before the first staging step is the row that survives
        self.assertEqual([r["outcome"] for r in rows], ["STAGE_RESERVED"], rows)
        self.assertEqual((rows[0]["sourceCommit"], rows[0]["nightKey"], rows[0]["venue"]), (SHA, NIGHT, "ultra-magnus"))
        self.assertTrue(rows[0]["reservationId"])

    def test_a_lost_stage_ok_row_still_holds_the_night_against_a_second_sha(self) -> None:
        self.built_dll_pair()
        first = self.run_driver(env=self.receipt_goes_readonly_at_the_runner_submission())
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_readonly()
        self.calls_log.unlink(missing_ok=True)
        second = self.run_driver(commit=OTHER_SHA)
        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assert_exit(second, "NIGHT_RESERVED")
        self.assertEqual(self.calls(), [])
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["exitCode"], r["sourceCommit"]), ("NIGHT_RESERVED", EXIT["NIGHT_RESERVED"], OTHER_SHA))
        self.assertIn(SHA, r["detail"])

    def test_an_incomplete_reservation_for_the_same_sha_resumes_and_is_reconciled(self) -> None:
        self.seed_row(SHA, outcome="STAGE_RESERVED", reservation_id="r-incomplete-1")
        self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["STAGE_RESERVED", "STAGE_RESERVED", "STAGE_OK"], rows)
        new_id = rows[1]["reservationId"]
        self.assertNotEqual(new_id, "r-incomplete-1")
        self.assertEqual(rows[2]["reservationId"], new_id)
        self.assertEqual([(x["reservationId"], x["state"]) for x in rows[2]["reconciled"]], [("r-incomplete-1", "incomplete")])

    def test_an_incomplete_reservation_for_another_sha_holds_the_night(self) -> None:
        self.seed_row(OTHER_SHA, outcome="STAGE_RESERVED", reservation_id="r-incomplete-1")
        p = self.run_driver()
        self.assert_exit(p, "NIGHT_RESERVED")
        self.assertEqual(self.calls(), [])
        r = self.rows()[-1]
        self.assertIn(OTHER_SHA, r["detail"])
        self.assertIn("r-incomplete-1", r["detail"])
        self.assertEqual([(x["reservationId"], x["state"]) for x in r["reconciled"]], [("r-incomplete-1", "incomplete")])

    def test_a_failed_reservation_for_another_sha_still_holds_the_night(self) -> None:
        self.seed_row(OTHER_SHA, outcome="STAGE_RESERVED", reservation_id="r-failed-1")
        self.seed_row(OTHER_SHA, outcome="STAGE_UNRESOLVED", reservation_id="r-failed-1")
        p = self.run_driver()
        self.assert_exit(p, "NIGHT_RESERVED")
        self.assertEqual(self.calls(), [])
        self.assertIn("STAGE_UNRESOLVED", self.rows()[-1]["detail"])

    def test_a_reservation_from_an_earlier_night_does_not_block(self) -> None:
        self.seed_row(OTHER_SHA, outcome="STAGE_RESERVED", night="2026-10-19", reservation_id="r-yesterday")
        self.built_dll_pair()
        self.assert_exit(self.run_driver(), "STAGE_OK")

    def test_a_reservation_that_cannot_be_written_fails_closed_before_staging(self) -> None:
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text("", encoding="utf-8")
        os.chmod(self.receipt, stat.S_IREAD)
        self.addCleanup(self.clear_readonly)
        self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "RECEIPT_IO_FAILED")
        for role in ("DLLGEN", "ASSEMBLE", "STAGEGEN", "UMRUN"):
            self.assertEqual(self.calls(role), [], role)
        self.assertIn("staging did not start", p.stdout)

    # ---------------------------------------------------------------- venue exclusion does not depend on the caller's receipt path
    def test_a_second_receipt_path_cannot_take_the_venue_mutex(self) -> None:
        self.built_dll_pair()
        env = dict(os.environ, FAKE_CALLS=str(self.calls_log), FAKE_SHA=SHA, FAKE_ASSEMBLE="sleep")
        holder = subprocess.Popen(self.args("-MaxTotalSec", "45"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 120
            while not self.calls("ASSEMBLE") and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.5)
            self.assertTrue(self.calls("ASSEMBLE"), "the holder never reached its assemble")
            a = self.args("-MaxTotalSec", "20")
            a[a.index("-ReceiptFile") + 1] = str(self.case / "other" / ".claude-state" / "stage-nightly.jsonl")
            p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
            self.assert_exit(p, "AGENT_BUSY")
            self.assertIn("MLV-VenueStageNightly-ultra-magnus", p.stdout)
            self.assertEqual(len(self.calls("ASSEMBLE")), 1, self.calls("ASSEMBLE"))
        finally:
            try:
                holder.communicate(timeout=150)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate()

    def test_the_real_venue_share_takes_only_the_canonical_receipt(self) -> None:
        # No -AgentShare: the venues.json share. -WhatIf, so neither this driver nor an older one probes, builds or submits anything.
        a = self.args("-WhatIf")
        i = a.index("-AgentShare")
        del a[i:i + 2]
        p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=dict(os.environ, FAKE_CALLS=str(self.calls_log)), stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, EXIT["USAGE"], p.stdout + p.stderr)
        self.assertIn("canonical", p.stdout)
        self.assertIn(str(Path(".claude-state") / "dual-venue" / "stage-nightly.jsonl"), p.stdout)
        self.assertEqual(self.calls(), [])

    # ---------------------------------------------------------------- the cap
    def test_a_child_that_outlives_the_cap_is_killed_and_typed(self) -> None:
        self.built_dll_pair()
        t0 = time.monotonic()
        p = self.run_driver("-MaxTotalSec", "25", env={"FAKE_ASSEMBLE": "sleep"}, timeout=200)
        self.assertLess(time.monotonic() - t0, 120, "the driver waited past its cap; the assembler stub sleeps 300 s")
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        pid = int(re.search(r"pid=(\d+)", self.calls("ASSEMBLE")[0]).group(1))
        deadline = time.monotonic() + 15
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        self.assertFalse(pid_alive(pid), "the assembler outlived the cap")
        self.assertEqual(self.submissions(), [])

    # ---------------------------------------------------------------- static guards
    def test_the_driver_never_deletes_enumerates_schedules_or_starts_the_app(self) -> None:
        text = DRIVER.read_text(encoding="utf-8")
        self.assertTrue(text.isascii(), "ASCII-only .ps1")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("MLVApp.exe", "Register-ScheduledTask", "schtasks", "Start-Process", "Start-Job", "Remove-Item", "Get-ChildItem", "hub-autopilot.ps1",
                          "pending-actions", "-Recurse"):
            self.assertNotIn(forbidden, code, forbidden)
        for needed in ("Test-DiskFloor", "Test-HostCommitGate", "Get-VenueClaimStatus", "Get-DllPairReuse", "Get-StageReuse", "Test-UmRunJobIdInUseMessage",
                       "-JobIdSuffix", "mlv-app/venue-stage-nightly/v1", "STAGE_RESERVED", "reservationId"):
            self.assertIn(needed, code, needed)


@requires_windows_pwsh
class VenueStageReuseModuleTests(unittest.TestCase):
    """The three promoted functions, logic unchanged from the untracked venue-reuse-check.ps1 the lanes copied word for word."""

    def ps(self, body: str) -> str:
        p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"Import-Module '{REUSE_MODULE}' -Force; {body}"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.strip()

    def test_the_module_names_its_origin_and_is_ascii(self) -> None:
        text = REUSE_MODULE.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertIn("venue-reuse-check.ps1", text)
        for fn in ("Test-UmRunJobIdInUseMessage", "Get-StageReuse", "Get-DllPairReuse"):
            self.assertIn(f"function {fn}", text)

    def test_in_use_message_matches_only_the_outbox_refusal_for_that_jobid(self) -> None:
        out = self.ps("Test-UmRunJobIdInUseMessage -Message 'UMRUN_JOBID_IN_USE outbox already holds j-1.result.json' -JobId 'j-1'; "
                      "Test-UmRunJobIdInUseMessage -Message 'UMRUN_JOBID_IN_USE inbox already holds j-1.job.ps1' -JobId 'j-1'; "
                      "Test-UmRunJobIdInUseMessage -Message 'UMRUN_JOBID_IN_USE outbox already holds j-2.result.json' -JobId 'j-1'")
        self.assertEqual(out.split(), ["True", "False", "False"])

    def test_stage_reuse_reads_a_stage_ok_result_and_refuses_another_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            res = Path(d) / "r.json"
            res.write_text(json.dumps({"jobId": "j", "exitCode": 0, "stdout": f"RESULT=STAGE_OK SOURCE={SHA} FILES=4", "endedUtc": "2026-10-18T00:00:00Z"}), encoding="utf-8")
            ok = self.ps(f"(Get-StageReuse -ResultPath '{res}' -JobId 'j' -Sha '{SHA}' -Kind Stage).Staged")
            other = self.ps(f"(Get-StageReuse -ResultPath '{res}' -JobId 'j' -Sha '{OTHER_SHA}' -Kind Stage).Reason")
        self.assertEqual(ok, "True")
        self.assertTrue(other.startswith("SOURCE-mismatch"), other)


if __name__ == "__main__":
    unittest.main()
