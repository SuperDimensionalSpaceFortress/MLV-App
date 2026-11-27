"""Tests for BACHELOR-LEASE-BATCH-LEGS-1: tools/profiling/dual-venue/Invoke-VenueLeaseBatch.ps1 (bench legs INSIDE one quiet lease per batch), its
VenueChain.psm1 helpers, and the job-id contract it shares with the Bachelor agent (tools/profiling/bachelor/ultra-magnus-agent.ps1, lease-batch block).

WHAT IS PINNED
  * LEASE FIRST: the batch's open job (the agent requests the lease for it) is submitted before any quiet probe or leg, and ONE open serves every leg.
  * IN-LEASE RETRY: a busy probe (COOLDOWN_UNMET, GATE_BUSY) or a leg whose own re-check failed (VENUE_NOT_QUIESCENT) is retried inside the same lease:
    no second open, no release.
  * A lease too short for the next leg ends the batch cleanly (LEASE_REMAINING_SHORT) and the rest go to the next batch; a leg that did not run on the
    batch's lease is LEASE_LOST (uncounted) and ends the batch; an agent with no lease batch stops the run before any probe or leg (exit 10).
  * Typed receipts per batch (batch-<nn>.json, batches.jsonl) and the close job that releases the lease.
  * r3 (sol r2 blockers): an open still in the agent's gate at the end of a batch is CANCELLED and reconciled by name before the claim is released, so
    it can never take a lease after the runner exits (or the run says UNRECONCILED, exit 11); a leg still running at the lease's end is killed with its
    whole process tree and typed LEASE_EXPIRED (uncounted, never re-run), and a leg the agent stopped at its lease's bound is typed by the agent's reason.
    Also the LEG_FAILED exit-7 path at run time, the busy-probe spin bound, and a leg's own exit-75 refusals in the batch receipt (fable r2).
  * The agent's own pieces (its RED/GREEN cooldown case lives in tools/testing/test-ultra-magnus-agent-quiet-lease.ps1): a leg job id built by
    Invoke-VenueLeg -LeaseBatch carries the token the agent reads, and its health probe stays a light job.

Offline only: Wait-VenueQuiet, Invoke-VenueLeg and um-run.ps1 are fake scripts passed through the runner's seams, the agent share is a temp directory and every
claim lives in a temp -ClaimDir. Nothing here reaches a venue. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import ctypes
import ast
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.repo_hygiene.venue_fixture_modules import copy_cross_tree_modules

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
RUNNER = DV / "Invoke-VenueLeaseBatch.ps1"
MODULE = DV / "VenueChain.psm1"
LEG = DV / "Invoke-VenueLeg.ps1"
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
MANIFEST = "b" * 64
# r13 (adversary r13 A1): the three artifact digests the leg job hashes the cached artifacts against (playback-attr-3-cuda-job.ps1 $manifestChecks), in every staged manifest
ARTIFACT_SHAS = {"packageZip": {"sha256": "a" * 64}, "exe": {"sha256": "b" * 64}, "dll": {"sha256": "c" * 64}}
ABSENT = object()   # a staged build manifest field left out (stage(): dllPairManifestSha256=ABSENT)

# r13 (PR #729, hosted shard 4/8 job 124969603389): the DOCUMENTED reserved device names, hard-coded from Microsoft's "Naming Files, Paths, and Namespaces" page
# (https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file): CON, PRN, AUX, NUL, COM1-COM9, COM1-3 superscript, LPT1-LPT9, LPT1-3 superscript; "also avoid these names
# followed immediately by an extension" (NUL.txt and NUL.tar.gz are NUL). COM0 and LPT0 are not on the list. This is the PRIMARY oracle for the helper's device rule: it does not
# depend on the host. RtlIsDosDeviceName_U is host-dependent (Windows Server 2025 10.0.26100 returned 0 for every COM/LPT name inside `C:\x\.claude-state\<name>`, where
# 10.0.19045 returns non-zero), so the native classifier is only a corroborating check, in its own test method.
DOCUMENTED_DOS_DEVICE_STEMS = frozenset(["CON", "PRN", "AUX", "NUL"] + [f"{p}{d}" for p in ("COM", "LPT") for d in "123456789\u00b9\u00b2\u00b3"])


# r9 (Grok G14 prevention on #817): the mixed wall/monotonic clock lint. Three findings on this card (cc90eef2-A1, 5fc3bf72-A2, G14) were one class: a [DateTime]::UtcNow
# read that a deadline (or a -WaitUntilUtc end) was later counted from, so a step of this host's clock, or blocking work after the read, moved the end. Rules, per code line:
#   WAIT_UNTIL      a -WaitUntilUtc value must be placed in its own statement from a fresh [DateTime]::UtcNow AND a Stopwatch read (the monotonic time spent), never from a
#                   captured wall instant.
#   CAPTURED_WALL   a captured wall instant ($x = [DateTime]::UtcNow) with no Stopwatch frame in its file must not be extended (.AddSeconds/.AddMinutes/...) into a deadline-like
#                   variable, a comparison or a -WaitUntilUtc.
#   FRAME_WALL_READ in a file whose wall anchor IS paired with a Stopwatch frame (Get-WaitNowUtc), a deadline-like variable must never be read against [DateTime]::UtcNow.
# KNOWN_WALL_DEADLINES is the exact pre-existing set (batch-level deadlines in hours and one cancel reconcile window, outside the in-lease wait chain): a new hit fails the lint,
# and a fixed one fails it too until it is removed here (a ratchet, never a growing allowlist).
# the standing adversary checklist line (Grok G14 prevention): the tracked portable template, row 5 (clock and time)
ADVERSARY_CHECKLIST = ROOT / "tools" / "coordination" / "quality_lift" / "adversary-pass-prompt.md"
MIXED_CLOCK_CHECKLIST_LINE = "mixed wall/monotonic clock: every deadline derives from a Stopwatch, never from a wall instant read before blocking work"
# r9b (adversary-9a56fa7a.json A2): PowerShell names are case-insensitive ([datetime]::utcnow, $DEADLINE, .addseconds, -WAITUNTILUTC, -cge), so every pattern here is too.
# [DateTimeOffset]::UtcNow and [System.DateTime]::UtcNow are the same wall clock.
MIXED_CLOCK_WALL_RX = re.compile(r"\[(?:System\.)?DateTime(?:Offset)?\]::UtcNow\b", re.IGNORECASE)
MIXED_CLOCK_MONO_RX = re.compile(r"\[(?:System\.)?(?:Diagnostics\.)?Stopwatch\]::GetTimestamp\(\)", re.IGNORECASE)
MIXED_CLOCK_DEADLINE_RX = re.compile(r"\$\w*(?:deadline|until|gateEnd)\w*\b", re.IGNORECASE)
# r9b (adversary-74e14dda.json A2): and in parentheses, $t0 = ([DateTime]::UtcNow)
MIXED_CLOCK_ANCHOR_RX = re.compile(r"\$(\w+)\s*=\s*(?:\(\s*)*\[(?:System\.)?DateTime(?:Offset)?\]::UtcNow(?:\s*\))*(?=\s*(?:;|\}|$))", re.IGNORECASE)
MIXED_CLOCK_WAIT_UNTIL_RX = re.compile(r"""['"]-WaitUntilUtc['"]\s*,|-WaitUntilUtc\s+[\$\(\[]""", re.IGNORECASE)
KNOWN_WALL_DEADLINES = frozenset({
    ("Invoke-VenueBaseline.ps1", "CAPTURED_WALL", "$deadline = $startedUtc.AddSeconds($MaxTotalSec)"),
    ("Invoke-VenueChain.ps1", "CAPTURED_WALL", "$deadline = $(if ($DeadlineUtc) { [DateTimeOffset]::Parse($DeadlineUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { $t0.AddHours(6) })"),
    ("Invoke-VenueLeaseBatch.ps1", "CAPTURED_WALL", "$deadline = $(if ($DeadlineUtc) { [DateTimeOffset]::Parse($DeadlineUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { $t0.AddHours(6) })"),
})


def _mixed_clock_code(line: str) -> str:
    # r10 (sol r9b 13e99849-A2): a PowerShell comment is a `#` outside a quoted string at the line start or after whitespace, with or without a space after it
    # (`$t0 = [DateTime]::UtcNow #captured`); the r9 strip wanted `# ` and kept `#captured`, so the anchor read missed it. A `#` inside a string or a word stays.
    s = line.strip()
    quote = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1].isspace()):
            return s[:i].rstrip()
    return s


def _mixed_clock_split(stmt: str) -> list[str]:
    # r10 (adversary-49bf0ddc.json A4): `Start-Sleep -Seconds 90; $deadline = $t0.AddSeconds(240)` is two statements: split at a `;` outside quotes, parentheses and braces
    # (a `;` inside `{ L "x"; break }` stays, so a comparison keeps its break)
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for i, ch in enumerate(stmt):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            parts.append(stmt[start:i].strip())
            start = i + 1
    parts.append(stmt[start:].strip())
    return [p for p in parts if p]


def _mixed_clock_statements(text: str) -> list[str]:
    # r10 (sol r9b 13e99849-A2): PowerShell continues a statement onto the next line after an assignment operator (`$t0 =` then `[DateTime]::UtcNow`) or a trailing
    # backtick; those lines are joined into the one statement they are, so a multiline anchor or deadline reads like the single-line one.
    # r10 (adversary-49bf0ddc.json A4): and inside an open parenthesis (`$t0 = (` / `[DateTime]::UtcNow` / `)`); a `<# ... #>` block comment is dropped first.
    text = re.sub(r"<#.*?#>", " ", text, flags=re.S)
    out: list[str] = []
    pending = ""
    for c in (_mixed_clock_code(x) for x in text.splitlines()):
        if not c:
            continue
        if pending:
            c = f"{pending} {c}"
            pending = ""
        if c.endswith("`"):
            pending = c[:-1].rstrip()
        elif re.search(r"(?<![=!<>])=$", c) or (c.count("(") > c.count(")") and re.search(r"=\s*\(", c)):
            pending = c
        else:
            out.extend(_mixed_clock_split(c))
    if pending:
        out.extend(_mixed_clock_split(pending))
    return out


def mixed_clock_deadline_hits(files: dict[str, str]) -> set[tuple[str, str, str]]:
    """(file, rule, code line) for every mixed wall/monotonic deadline in the given {file name: text}."""
    hits: set[tuple[str, str, str]] = set()
    for name, text in files.items():
        code = _mixed_clock_statements(text)
        anchors = {m.group(1).lower() for c in code for m in MIXED_CLOCK_ANCHOR_RX.finditer(c)}
        # a FRAME: a function or statement that adds the Stopwatch time since an anchor to it (Get-WaitNowUtc), on a line that is not itself an anchor read
        framed = {a for a in anchors if any(re.search(rf"\${a}\b", c, re.I) and MIXED_CLOCK_MONO_RX.search(c) and not MIXED_CLOCK_ANCHOR_RX.search(c) for c in code)}
        for c in code:
            if MIXED_CLOCK_WAIT_UNTIL_RX.search(c):
                fresh = bool(MIXED_CLOCK_WALL_RX.search(c) and MIXED_CLOCK_MONO_RX.search(c))
                if not fresh or any(re.search(rf"\${a}\.Add\w*\(", c, re.I) for a in anchors):
                    hits.add((name, "WAIT_UNTIL", c))
            for a in anchors - framed:
                # extended into a deadline-like variable, a -WaitUntilUtc, or a comparison that ends a wait (break / return / continue on the same line)
                if re.search(rf"\${a}\.Add(?:Seconds|Minutes|Hours|Milliseconds|Ticks)\(", c, re.I) and (
                        re.match(rf"\s*{MIXED_CLOCK_DEADLINE_RX.pattern}\s*=", c, re.I) or MIXED_CLOCK_WAIT_UNTIL_RX.search(c)
                        or (re.search(r"\s-[ci]?[gl][te]\s", c, re.I) and re.search(r"\b(?:break|return|continue)\b", c, re.I))):
                    hits.add((name, "CAPTURED_WALL", c))
            if framed and MIXED_CLOCK_WALL_RX.search(c) and MIXED_CLOCK_DEADLINE_RX.search(c):
                hits.add((name, "FRAME_WALL_READ", c))
    return hits


# r9b: LEASE_BATCH_RED_REF=<commit> makes the r9 clock behaviour tests parse the dual-venue script from that commit instead of the working tree (git show, captured into
# a PowerShell variable and parsed in memory with Parser::ParseInput; nothing is copied to disk). It is the RED half of their RED/GREEN proof
# (LEASE_BATCH_RED_REF=2854b231 is the r8 head). Unset, as in CI and every normal run, they parse the working tree.
RED_REF_RX = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,99}")


def ps_parse_source(path: Path) -> str:
    """A PowerShell statement that sets $ast to the parsed script at `path`: the working tree, or the LEASE_BATCH_RED_REF commit's blob."""
    ref = os.environ.get("LEASE_BATCH_RED_REF", "").strip()
    if not ref:
        return f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(path)}', [ref]$null, [ref]$null)"
    if not RED_REF_RX.fullmatch(ref):
        raise ValueError(f"LEASE_BATCH_RED_REF is not a commit name: {ref!r}")
    rel = path.resolve().relative_to(ROOT).as_posix()
    return (f"$src = (& git -C '{psq(ROOT)}' show '{psq(ref)}:{psq(rel)}') -join \"`n\"; if ($LASTEXITCODE -ne 0 -or -not $src) {{ throw 'git show {psq(ref)}:{psq(rel)} failed' }}\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$null, [ref]$null)")


def documented_dos_device(name: str) -> bool:
    """True when the documented reserved set names this component: the stem before the first dot, with the spaces Win32 drops before an extension removed, compared case-insensitively."""
    return name.split(".", 1)[0].rstrip(" ").upper() in DOCUMENTED_DOS_DEVICE_STEMS


# the device-rule matrix: COM/LPT with every ASCII digit and the three superscripts, bare, with an extension and with spaces before it; the other documented names; lower case;
# and near misses that are ordinary names (COM10, CONSOLE, NULL, COM1x, a superscript 0/4 and a fullwidth 1 are not on the documented list)
DEVICE_MATRIX_NAMES = (
    [f"{p}{d}{tail}" for p in ("COM", "LPT") for d in [str(n) for n in range(10)] + ["\u00b9", "\u00b2", "\u00b3"] for tail in ("", ".txt", " .txt")]
    + ["CON", "PRN", "AUX", "NUL", "CON.txt", "NUL .txt", "NUL.tar.gz", "AUX  .log", "prn.x", "com1", "lpt\u00b2.log", "nul"]
    + ["COM10", "LPT10", "CONSOLE", "NULL", "AUXX", "COM1x", "LPT9-a", "COM\u2070", "COM\u2074", "COM\uff11", "CON_", "XCOM1"]
)

# Wait-VenueQuiet stand-in: mode per call from FAKE_QUIET_MODES (comma list, the last repeats), counted in the run dir; each call is logged with UTC ticks.
FAKE_QUIET = r"""
param([string]$Venue, [string]$WorkDir, [string]$GateLog, [double]$ThresholdPercent = 20, [int]$MaxWaitSec = 1800, [int]$RecheckSec = 90, [int]$MaxAdmissionWaitSec = 0, [string]$WaitUntilUtc = '', [long]$WaitUntilTs = 0)
$runDir = Split-Path -Parent $WorkDir
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$countFile = Join-Path $runDir 'fake-quiet-count.txt'
$n = $(if (Test-Path -LiteralPath $countFile) { [int](Get-Content -LiteralPath $countFile) } else { 0 })
Set-Content -LiteralPath $countFile -Value ($n + 1)
$modes = @(([string]$env:FAKE_QUIET_MODES) -split ',' | Where-Object { $_ })
if ($modes.Count -eq 0) { $modes = @('QUIET') }
$mode = $modes[[Math]::Min($n, $modes.Count - 1)]
# r5 pass 3: until= is -WaitUntilUtc as passed ('-' when absent), before maxWait= (which _in_lease_waits reads as the line's last field)
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} QUIET {1} until={3} maxWait={2}" -f [DateTime]::UtcNow.Ticks, $mode, $MaxWaitSec, $(if ($WaitUntilUtc) { $WaitUntilUtc } else { '-' }))
# r4 pass 2 (adversary A1): FAKE_QUIET_SLOW_SEC = the probe child's own time around its agent jobs (a slow gate-log append), which the runner cannot see
if ($env:FAKE_QUIET_SLOW_SEC) { Start-Sleep -Seconds ([int]$env:FAKE_QUIET_SLOW_SEC) }
switch ($mode) {
    'QUIET' { "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'COOLDOWN' { "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'FOREIGN' { 'FOREIGN_FLOOR owners=explorer:1,textinputhost:2,bloody7:3 floorMean=32.0% waitedSec=1 probes=3'; "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'BUSY' { 'GATE_BUSY quiet-probe waitedSec=1'; exit 3 }
    'PASTDEADLINE' {
        # LEASE-BATCH-IDLE-END-UNSEEN-1 A1: a probe that returns QUIET only after the run's deadline (FAKE_DEADLINE_UTC): the time the runner spends between the probe and the leg's launch
        $until = [DateTime]::Parse($env:FAKE_DEADLINE_UTC, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal).AddSeconds(1)
        while ([DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 100 }
        "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0
    }
    'IDLE' {
        # LEASE-BATCH-IDLE-END-UNSEEN-1: the wait outlasted the agent's idle bound (it submitted no job for 300 s): the agent released the parked batch lease and wrote its receipt line
        # (endReason=idle, ultra-magnus-agent.ps1 Close-LeaseBatch), and the probe still reads QUIET.
        $stq = Get-Content -LiteralPath (Join-Path $env:FAKE_SHARE 'fake-agent-state.json') -Raw | ConvertFrom-Json
        $lg = Join-Path $env:FAKE_SHARE 'logs'
        New-Item -ItemType Directory -Force -Path $lg | Out-Null
        Add-Content -LiteralPath (Join-Path $lg "lease-batch-$($stq.token).jsonl") -Value (([ordered]@{ schema = 'mlv-agent-lease-batch.v1'; token = $stq.token; leaseId = $stq.leaseId; endedUtc = [DateTimeOffset]::UtcNow.ToString('o'); heldSec = 301; endReason = 'idle'; why = 'no job for 301s > 300s'; jobsRun = 1 }) | ConvertTo-Json -Compress)
        "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=301"; exit 0
    }
    'IDLEBUSY' {
        # r3 adversary pass 2 A2: the agent released the lease idle while the gate stayed busy (no job ran): the idle line, and a GATE_BUSY probe
        $stq = Get-Content -LiteralPath (Join-Path $env:FAKE_SHARE 'fake-agent-state.json') -Raw | ConvertFrom-Json
        $lg = Join-Path $env:FAKE_SHARE 'logs'
        New-Item -ItemType Directory -Force -Path $lg | Out-Null
        Add-Content -LiteralPath (Join-Path $lg "lease-batch-$($stq.token).jsonl") -Value (([ordered]@{ schema = 'mlv-agent-lease-batch.v1'; token = $stq.token; leaseId = $stq.leaseId; endedUtc = [DateTimeOffset]::UtcNow.ToString('o'); heldSec = 301; endReason = 'idle'; why = 'no job for 301s > 300s'; jobsRun = 1 }) | ConvertTo-Json -Compress)
        'GATE_BUSY quiet-probe waitedSec=1'; exit 3
    }
    { $_ -in 'IDLEFOREIGN', 'FOREIGNIDLE', 'NOTOKENIDLE', 'FOREIGNEND' } {
        # r2 (sol key r1 BLOCKER, receipt token binding): lines in THIS batch's receipt file for THIS lease that another token wrote, or no token at all. IDLEFOREIGN: a foreign
        # closed line, then the agent's own idle line. FOREIGNIDLE / NOTOKENIDLE: a lone idle line under another token / under none. FOREIGNEND: a lone foreign non-idle line.
        $stq = Get-Content -LiteralPath (Join-Path $env:FAKE_SHARE 'fake-agent-state.json') -Raw | ConvertFrom-Json
        $lg = Join-Path $env:FAKE_SHARE 'logs'
        New-Item -ItemType Directory -Force -Path $lg | Out-Null
        $rf = Join-Path $lg "lease-batch-$($stq.token).jsonl"
        $at = [DateTimeOffset]::UtcNow.ToString('o')
        $rows = switch ($mode) {
            'IDLEFOREIGN' { [ordered]@{ token = 'ffffffffffff'; leaseId = $stq.leaseId; endReason = 'closed' }, [ordered]@{ token = $stq.token; leaseId = $stq.leaseId; endedUtc = $at; endReason = 'idle' } }
            'FOREIGNIDLE' { , [ordered]@{ token = 'ffffffffffff'; leaseId = $stq.leaseId; endedUtc = $at; endReason = 'idle' } }
            'NOTOKENIDLE' { , [ordered]@{ leaseId = $stq.leaseId; endedUtc = $at; endReason = 'idle' } }
            'FOREIGNEND' { , [ordered]@{ token = 'ffffffffffff'; leaseId = $stq.leaseId; endReason = 'foreign' } }
        }
        foreach ($row in $rows) { Add-Content -LiteralPath $rf -Value ($row | ConvertTo-Json -Compress) }
        "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=301"; exit 0
    }
    'UNKNOWN' { "VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=$ThresholdPercent% waitedSec=1 reason=probe-failed"; exit 6 }
    'STEAL' {
        # r5b (Grok G1): another owner's claim replaces the run's mid-batch (another nonce; its owner pid is dead, so the next batch may break it). The probe
        # itself reads QUIET; it waits long enough for a 1 s claim keeper to see the claim NOT_OURS.
        for ($i = 0; $i -lt 20; $i++) {
            try { $t = [IO.File]::ReadAllText($env:FAKE_CLAIM_PATH); [IO.File]::WriteAllText($env:FAKE_CLAIM_PATH, ($t -replace '"nonce":"[^"]*"', '"nonce":"stolen00000000"' -replace '"ownerPid":\d+', '"ownerPid":99999999')); break } catch { Start-Sleep -Milliseconds 100 }
        }
        Start-Sleep -Seconds 3
        "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0
    }
}
"""

# um-run.ps1 stand-in, invoked IN-PROCESS by the runner exactly as um-run is. It plays the lease-batch agent for the open and close jobs: an open job gets a
# result whose admission.leaseBatch names lease-<n> (FAKE_OPEN_MODES per open: batch | nobatch | retracted; FAKE_LEASE_REMAIN per open: seconds left), and a
# close job appends the agent's receipt line to <share>\logs\lease-batch-<token>.jsonl.
FAKE_UMRUN = r"""
param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec, [int]$MaxAdmissionWaitSec, [string]$NotAfterUtc = '<unset>')
if ($JobId -like 'venue-quiet-probe-caps-*') {
    # BACHELOR-DEDICATED-CAPS-RUNNER-1: the exempt light probe that prints Bachelor's heavy-slot.lease.json. Logged apart from the lease-batch events (FAKE_CAPS_EVENTS)
    # so every other case reads the same event order. FAKE_CAPS_MODE: fail (um-run threw: the default, so a case that does not care reads the fallback) | live
    # (stdout = FAKE_CAPS_JSON) | exit1 (the probe job exited 1 with the JSON on stdout).
    Add-Content -LiteralPath $env:FAKE_CAPS_EVENTS -Value ("{0} CAPS {1} claimHeld={2} notAfter={3} body={4}" -f [DateTime]::UtcNow.Ticks, $JobId, [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH)), $NotAfterUtc, ((Get-Content -LiteralPath $ScriptPath -Raw) -replace '\s+', ' ').Trim())
    $cm = $(if ($env:FAKE_CAPS_MODE) { $env:FAKE_CAPS_MODE } else { 'fail' })
    if ($cm -eq 'fail') { throw "UNRESOLVED: $JobId (fake) was not taken by the agent before -MaxQueueWaitSec" }
    return [pscustomobject]@{ jobId = $JobId; exitCode = $(if ($cm -eq 'exit1') { 1 } else { 0 }); endedUtc = [DateTimeOffset]::UtcNow.ToString('o'); admission = [pscustomobject]@{ label = 'exempt-light' }; stdout = [string]$env:FAKE_CAPS_JSON }
}
$stateFile = Join-Path $AgentShare 'fake-agent-state.json'
$st = $(if (Test-Path -LiteralPath $stateFile) { Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json } else { [pscustomobject]@{ opens = 0; leaseId = $null; token = $null } })
$held = [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH))
# r4: -NotAfterUtc as passed ('<unset>' when the runner did not pass it), which the real um-run writes as meta.json notAfterUtc
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} UMRUN {1} claimHeld={2} notAfter={3} body={4}" -f [DateTime]::UtcNow.Ticks, $JobId, $held, $NotAfterUtc, ((Get-Content -LiteralPath $ScriptPath -Raw) -replace '\s+', ' ').Trim())
# r5b: the job is dropped into <share>\inbox\<jobId>.job.ps1 first, as the real um-run does (um-run.ps1 $jobFile)
New-Item -ItemType Directory -Force -Path (Join-Path $AgentShare 'inbox') | Out-Null
Copy-Item -LiteralPath $ScriptPath -Destination (Join-Path $AgentShare "inbox\$JobId.job.ps1")
$now = [DateTimeOffset]::UtcNow
$token = $JobId.Substring($JobId.Length - 12)
function Pick([string]$List, [int]$I, [string]$Default) { $v = @(([string]$List) -split ',' | Where-Object { $_ }); if ($v.Count -eq 0) { return $Default }; $v[[Math]::Min($I, $v.Count - 1)] }
if ($JobId -like 'lease-batch-open-*') {
    $n = [int]$st.opens
    $mode = Pick $env:FAKE_OPEN_MODES $n 'batch'
    $remain = [int](Pick $env:FAKE_LEASE_REMAIN $n '2700')
    if ($mode -eq 'retracted') { throw 'RETRACTED: fake queue ceiling' }
    $lid = "lease-$($n + 1)"
    # r13 (adversary r12 A1): FAKE_OPEN_LEASE_ID answers with that exact lease id (a timestamp-shaped one, which ConvertFrom-Json turns into a DateTime)
    if ($env:FAKE_OPEN_LEASE_ID) { $lid = $env:FAKE_OPEN_LEASE_ID }
    # r5 pass 2 (adversary r5 A1): FAKE_OPEN_LEASE_ID_NUM answers with a JSON NUMBER lease id (VenueRawJson then clears every identity in the reading)
    if ($env:FAKE_OPEN_LEASE_ID_NUM) { $lid = [int]$env:FAKE_OPEN_LEASE_ID_NUM }
    $adm = [ordered]@{ gate = 'fake'; mode = 'lease'; label = 'clear'; heavySlotHeld = "1:$(1001 + $n)"; slot = 'C:\t\.heavy-slots\1'; leaseMinutes = 45; waitedSec = 7; leaseRequests = 2
        leaseRefusals = @([ordered]@{ reason = 'refused'; rc = 75; why = 'quiet lease cooldown' }) }
    # r12 (adversary A2): FAKE_OPEN_TOKEN_SHY=1 answers with the batch's token plus a soft hyphen (U+00AD): culture-aware equality says it is the same token, ordinal says it is not
    $tokOut = $(if ($env:FAKE_OPEN_TOKEN_SHY -eq '1') { $token + [string][char]0x00AD } else { $token })
    if ($mode -ne 'nobatch') { $adm['leaseBatch'] = [ordered]@{ token = $tokOut; leaseId = $lid; seq = 1; reused = $false; untilEpoch = $now.ToUnixTimeSeconds() + $remain; remainingSec = $remain } }
    # LEASE-BATCH-IDLE-END-UNSEEN-1 r3 pass 2 (adversary A2): FAKE_IDLE_SEC is the running agent's own idle bound, published as admission.leaseBatch.idleSec
    # r4 (Grok G7): 'str:<s>' publishes a string, 'dbl:<d>' a double, 'null' a JSON null; anything else an int
    if ($mode -ne 'nobatch' -and $env:FAKE_IDLE_SEC) {
        $iv = [string]$env:FAKE_IDLE_SEC
        $adm['leaseBatch']['idleSec'] = $(if ($iv -like 'str:*') { $iv.Substring(4) } elseif ($iv -like 'dbl:*') { [double]$iv.Substring(4) } elseif ($iv -eq 'null') { $null } else { [int]$iv })
    }
    # r5 (adversary r4 pass 3 A1): FAKE_OPEN_LB_WRAP publishes the leaseBatch inside an array: array ([{..}]) | nullplus ([null, {..}]) | nested ([[{..}]])
    if ($mode -ne 'nobatch' -and $env:FAKE_OPEN_LB_WRAP) {
        $lb0 = $adm['leaseBatch']; $w = [string]$env:FAKE_OPEN_LB_WRAP
        if ($w -eq 'array') { $adm['leaseBatch'] = @($lb0) } elseif ($w -eq 'nullplus') { $adm['leaseBatch'] = @($null, $lb0) } elseif ($w -eq 'nested') { $adm['leaseBatch'] = @(,@($lb0)) }
    }
    # LEASE-BATCH-IDLE-END-UNSEEN-1 r10 (adversary-49bf0ddc.json A1): FAKE_OPEN_LB_EMPTY=1 publishes leaseBatch as {} (there, naming no lease); FAKE_OPEN_LABEL overrides the label
    if ($env:FAKE_OPEN_LB_EMPTY -eq '1') { $adm['leaseBatch'] = [ordered]@{} }
    if ($env:FAKE_OPEN_LABEL) { $adm['label'] = [string]$env:FAKE_OPEN_LABEL }
    [pscustomobject]@{ opens = $n + 1; leaseId = $lid; token = $token } | ConvertTo-Json | Set-Content -LiteralPath $stateFile
    # r5: FAKE_OPEN_SLEEP per open: um-run blocks this long first, as it does while the agent re-asks a refused (exit 75) request for the open
    $sleep = [int](Pick $env:FAKE_OPEN_SLEEP $n '0')
    # r5b: FAKE_OPEN_CLAIM_READONLY per open (1 = the claim file is read-only for that wait, so every renewal comes back BUSY; writable again after it)
    $ro = (Pick $env:FAKE_OPEN_CLAIM_READONLY $n '0') -eq '1'
    if ($ro) { Set-ItemProperty -LiteralPath $env:FAKE_CLAIM_PATH -Name IsReadOnly -Value $true }
    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 r8: FAKE_OPEN_RELEASE (a path) ends that wait the moment the file exists, with FAKE_OPEN_SLEEP its upper bound, so a case that acts
    # during the wait releases the open when it is done instead of sleeping out a fixed margin
    if ($sleep -gt 0) {
        Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} OPENWAIT {1} sec={2}" -f [DateTime]::UtcNow.Ticks, $JobId, $sleep)
        if ($env:FAKE_OPEN_RELEASE) { $until = [DateTime]::UtcNow.AddSeconds($sleep); while (-not (Test-Path -LiteralPath $env:FAKE_OPEN_RELEASE) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 50 } } else { Start-Sleep -Seconds $sleep }
        $now = [DateTimeOffset]::UtcNow
    }
    if ($ro) { Set-ItemProperty -LiteralPath $env:FAKE_CLAIM_PATH -Name IsReadOnly -Value $false }
    # r6 (adversary-848f6559.json A2): FAKE_OPEN_ENDED_AHEAD_SEC = the agent's clock runs that far ahead of this host's (the open's endedUtc lies in the host's future)
    $openEnded = $now.AddSeconds($(if ($env:FAKE_OPEN_ENDED_AHEAD_SEC) { [int]$env:FAKE_OPEN_ENDED_AHEAD_SEC } else { 0 })).ToString('o')
    $res = [pscustomobject]@{ jobId = $JobId; exitCode = 0; endedUtc = $openEnded; admission = [pscustomobject]$adm; stdout = 'LEASE_BATCH_OPEN' }
    # r5 pass 3 (adversary r5 pass 2 A1): FAKE_OPEN_ADM_WRAP=array publishes the whole admission inside a one-element array
    if ($env:FAKE_OPEN_ADM_WRAP -eq 'array') { $res.admission = @($res.admission) }
    if ($env:FAKE_OPEN_ADM_WRAP -eq 'empty') { $res.admission = @() }   # r10 (adversary-49bf0ddc.json A1): admission:[]
    if ($mode -eq 'exit1') { $res.exitCode = 1 }   # r14: the open job's receipt says it failed (OPEN_FAILED)
    if ($mode -eq 'gatehold') {
        # r3: um-run gave up while the agent still holds the open in its gate, and NO result lands: the agent keeps re-asking until it is cancelled.
        throw "UNRESOLVED: $JobId was held unlaunched in the agent's heavy-slot admission gate (fake, held) for the full -MaxAdmissionWaitSec"
    }
    if ($mode -eq 'gatewait') {
        # um-run gave up while the agent still held the open in its gate (exit-75 refusals); the agent admits it later and its result lands by name.
        $outbox = Join-Path $AgentShare 'outbox'
        New-Item -ItemType Directory -Force -Path $outbox | Out-Null
        $res | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outbox "$JobId.result.json")
        throw "UNRESOLVED: $JobId was held unlaunched in the agent's heavy-slot admission gate (fake) for the full -MaxAdmissionWaitSec"
    }
    if ($mode -eq 'errlanded') {
        # r5 (adversary r4 pass 3 A1 sibling): the agent ran the open and its result landed by name, but um-run itself failed (outcome ERROR): the batch's finally cancels the open
        # and reconciles it by name (RAN_BEFORE_CANCEL when its admission names the lease)
        $outbox = Join-Path $AgentShare 'outbox'
        New-Item -ItemType Directory -Force -Path $outbox | Out-Null
        $res | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outbox "$JobId.result.json")
        throw "fake um-run failure after $JobId's result landed"
    }
    if ($env:FAKE_OPEN_VIA_REAL_UMRUN -eq '1') {
        # r14 (adversary r13 A1): the open's result reaches the runner through the REAL um-run.ps1 reader, the way it does in production: the agent's result file is staged in the outbox
        # at um-run's first poll and um-run returns what it read. The fake's own drop of the job file is withdrawn first (the real um-run drops it itself).
        Remove-Item -LiteralPath (Join-Path $AgentShare "inbox\$JobId.job.ps1") -Force
        [IO.File]::WriteAllText((Join-Path $AgentShare 'heartbeat.txt'), 'alive')
        $json = $res | ConvertTo-Json -Depth 6
        $outbox = Join-Path $AgentShare 'outbox'
        $hook = { New-Item -ItemType Directory -Force -Path $outbox | Out-Null; [IO.File]::WriteAllText((Join-Path $outbox "$JobId.result.json"), $json) }.GetNewClosure()
        return (& $env:FAKE_REAL_UMRUN -ScriptPath $ScriptPath -JobId $JobId -AgentShare $AgentShare -TimeoutSec 60 -PollSeconds 1 -TestHookAtLoopTop $hook 6>$null)
    }
    return $res
}
if ($JobId -like 'lease-batch-close-*') {
    # r3: FAKE_CLOSE_MODES per close: ok | timeout (um-run's queue ceiling: the close never ran)
    $cf = Join-Path $AgentShare 'fake-close-count.txt'
    $c = $(if (Test-Path -LiteralPath $cf) { [int](Get-Content -LiteralPath $cf) } else { 0 })
    Set-Content -LiteralPath $cf -Value ($c + 1)
    # r5b: the cancel files already present when the close was submitted
    Add-Content -LiteralPath (Join-Path $AgentShare 'fake-close-cancels.txt') -Value ("{0} {1}" -f $JobId, (@(Get-ChildItem -LiteralPath (Join-Path $AgentShare 'cancel') -Filter '*.cancel' -Name) -join ','))
    if ((Pick $env:FAKE_CLOSE_MODES $c 'ok') -eq 'timeout') { throw "UNRESOLVED: $JobId (fake) was not taken by the agent before -MaxQueueWaitSec" }
    $logs = Join-Path $AgentShare 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    if ($st.token -eq $token) {
        Add-Content -LiteralPath (Join-Path $logs "lease-batch-$token.jsonl") -Value (([ordered]@{ schema = 'mlv-agent-lease-batch.v1'; token = $token; leaseId = $st.leaseId; endReason = 'closed'; jobsRun = 1 }) | ConvertTo-Json -Compress)
    }
    return [pscustomobject]@{ jobId = $JobId; exitCode = 0; endedUtc = $now.ToString('o'); admission = [pscustomobject]@{ label = 'exempt-light' }; stdout = 'LEASE_BATCH_CLOSE' }
}
throw "fake um-run: unexpected job $JobId"
"""

# Invoke-VenueLeg stand-in: prints DVE_JOB_ID lines (the health probe, then the leg job: "<stem>-lb<token>"), writes the leg job's agent result into the fake
# share's outbox (admission.leaseBatch on the current lease, or another one when the call index is in FAKE_LEG_LOSE), a receipt and its summary.json
# (pre-load FAKE_LEG_PRELOADS), and DVE_OUTCOME from FAKE_LEG_OUTCOMES. Each call is logged with UTC ticks and its -LeaseBatch.
FAKE_LEG = r"""
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '', [string]$LeaseBatch = '', [string]$ReceiptRoot = '', [string]$ExpectSpecSha256 = '')
$share = $env:FAKE_SHARE
# r19 (sol r18 B1): FAKE_LEG_SPEC_SHA records the -ExpectSpecSha256 each call was given beside the sha256 of the spec bytes it would run
if ($env:FAKE_LEG_SPEC_SHA) { Add-Content -LiteralPath $env:FAKE_LEG_SPEC_SHA -Value ("expect={0} read={1}" -f $ExpectSpecSha256, (Get-FileHash -LiteralPath $LegSpec -Algorithm SHA256).Hash.ToLowerInvariant()) }
# LEASE-BATCH-RECEIPT-ROOT-1: one line per call saying whether -ReceiptRoot was passed at all (absent and empty are different: the batch must forward NOTHING when it has none)
Add-Content -LiteralPath (Join-Path $env:FAKE_LEG_ROOT 'leg-receiptroot.log') -Value ("bound={0} root={1}" -f $PSBoundParameters.ContainsKey('ReceiptRoot'), $ReceiptRoot)
$st = Get-Content -LiteralPath (Join-Path $share 'fake-agent-state.json') -Raw | ConvertFrom-Json
$cf = Join-Path $share 'fake-leg-count.txt'
$k = $(if (Test-Path -LiteralPath $cf) { [int](Get-Content -LiteralPath $cf) } else { 0 })
Set-Content -LiteralPath $cf -Value ($k + 1)
function Pick([string]$List, [int]$I, [string]$Default) { $v = @(([string]$List) -split ',' | Where-Object { $_ }); if ($v.Count -eq 0) { return $Default }; $v[[Math]::Min($I, $v.Count - 1)] }
$leaf = [IO.Path]::GetFileNameWithoutExtension($LegSpec)
$held = [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH))
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} LEG {1} leaseBatch={2} claimHeld={3}" -f [DateTime]::UtcNow.Ticks, $leaf, $LeaseBatch, $held)
$jobId = "dve-$leaf-$Venue-cpu-2026102300000$k$(if ($LeaseBatch) { "-lb$LeaseBatch" })"
[Console]::Out.WriteLine("DVE_JOB_ID=$jobId-health"); [Console]::Out.WriteLine("DVE_JOB_ID=$jobId"); [Console]::Out.Flush()
# r3: FAKE_LEG_MODES per call: ok | exit3 (died without a receipt) | hang (still running at lease end: it starts a grandchild, records both pids, and sleeps)
$legMode = Pick $env:FAKE_LEG_MODES $k 'ok'
if ($legMode -eq 'exit3') { exit 3 }
# r6 (adversary-848f6559.json A1 sibling): exit3late = the child works 6 s and dies without its agent job ever landing a result (no evidence the agent's idle clock restarted)
if ($legMode -eq 'exit3late') { Start-Sleep -Seconds 6; exit 3 }
if ($legMode -eq 'hang') {
    $gc = Start-Process -FilePath ([Environment]::ProcessPath) -ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 300' -PassThru -NoNewWindow
    Add-Content -LiteralPath (Join-Path $share 'fake-leg-pids.txt') -Value "$PID $($gc.Id) $jobId"
    Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} LEGHANG {1} pid={2}" -f [DateTime]::UtcNow.Ticks, $leaf, $PID)
    Start-Sleep -Seconds 150
    exit 0
}
$lose =@(([string]$env:FAKE_LEG_LOSE) -split ',' | Where-Object { $_ } | ForEach-Object { [int]$_ })
# LEASE-BATCH-IDLE-END-UNSEEN-1: FAKE_LEG_IDLE_END=1 is the idle release landing AFTER the runner's pre-launch read (the agent wrote its endReason=idle line for the batch lease while the leg's job
# was being admitted, so the job took a fresh lease under the token: pair it with FAKE_LEG_LOSE for that call)
if ($env:FAKE_LEG_IDLE_END -in 'foreign-then-idle', 'foreign') {
    # r2 (sol key r1 BLOCKER): a line ANOTHER token wrote for this lease lands first (closed before the agent's own idle line; or idle, alone)
    New-Item -ItemType Directory -Force -Path (Join-Path $share 'logs') | Out-Null
    Add-Content -LiteralPath (Join-Path $share "logs\lease-batch-$($st.token).jsonl") -Value (([ordered]@{ token = 'ffffffffffff'; leaseId = $st.leaseId; endedUtc = [DateTimeOffset]::UtcNow.ToString('o'); endReason = $(if ($env:FAKE_LEG_IDLE_END -eq 'foreign') { 'idle' } else { 'closed' }) }) | ConvertTo-Json -Compress)
}
if ($env:FAKE_LEG_IDLE_END -in '1', 'foreign-then-idle') {
    New-Item -ItemType Directory -Force -Path (Join-Path $share 'logs') | Out-Null
    Add-Content -LiteralPath (Join-Path $share "logs\lease-batch-$($st.token).jsonl") -Value (([ordered]@{ schema = 'mlv-agent-lease-batch.v1'; token = $st.token; leaseId = $st.leaseId; endedUtc = [DateTimeOffset]::UtcNow.ToString('o'); heldSec = 301; endReason = 'idle'; why = 'no job for 301s > 300s'; jobsRun = 1 }) | ConvertTo-Json -Compress)
}
# r12 (adversary A2): FAKE_LEG_LEASE_SHY=1 reports the batch's lease id plus a soft hyphen (U+00AD): culture-aware equality says it is the same lease, ordinal says it is not
$lid = $(if ($lose -contains ($k + 1)) { 'lease-somebody-else' } elseif ($env:FAKE_LEG_LEASE_SHY -eq '1') { $st.leaseId + [string][char]0x00AD } elseif ($env:FAKE_LEG_LEASE_ID) { $env:FAKE_LEG_LEASE_ID } else { $st.leaseId })
# r13 (adversary r12 A1): FAKE_LEG_LEASE_ID reports that exact lease id (a different spelling of the batch's instant), instead of the batch's own
$now = [DateTimeOffset]::UtcNow
$remain = [int](Pick $env:FAKE_LEG_REMAIN $k '2600')
$outbox = Join-Path $share 'outbox'
New-Item -ItemType Directory -Force -Path $outbox | Out-Null
# r6 (adversary-848f6559.json A2): FAKE_LEG_ENDED_AHEAD_SEC per call = the agent's clock runs that far ahead (its endedUtc lies in this host's future); FAKE_LEG_ENDED_RAW = an
# endedUtc that is no instant at all
$legEnded = $(if ($env:FAKE_LEG_ENDED_RAW) { [string]$env:FAKE_LEG_ENDED_RAW } else { $now.AddSeconds([int](Pick $env:FAKE_LEG_ENDED_AHEAD_SEC $k '0')).ToString('o') })
# r8 (Grok G12): FAKE_LEG_JOB_SEC per call = the leg job's own runtime on the agent's clock: the child sleeps that long first, and startedUtc is that much before endedUtc (same
# skew). FAKE_LEG_STARTED=null publishes startedUtc as JSON null (the queue-expired result's shape)
$legJobSec = [int](Pick $env:FAKE_LEG_JOB_SEC $k '0')
if ($legJobSec -gt 0) { Start-Sleep -Seconds $legJobSec; $now = [DateTimeOffset]::UtcNow; $legEnded = $(if ($env:FAKE_LEG_ENDED_RAW) { [string]$env:FAKE_LEG_ENDED_RAW } else { $now.AddSeconds([int](Pick $env:FAKE_LEG_ENDED_AHEAD_SEC $k '0')).ToString('o') }) }
$legStarted = $(if ($env:FAKE_LEG_STARTED -eq 'null') { $null } else { $now.AddSeconds([int](Pick $env:FAKE_LEG_ENDED_AHEAD_SEC $k '0') - $legJobSec).ToString('o') })
# r8 (adversary-cc9275e3.json A2): FAKE_LEG_STARTED=array publishes startedUtc inside a one-element array (no usable instant)
if ($env:FAKE_LEG_STARTED -eq 'array') { $legStarted = @($legStarted) }
$res = [ordered]@{ jobId = $jobId; exitCode = 0; startedUtc = $legStarted; endedUtc = $legEnded; admission = [ordered]@{ mode = 'lease'; label = 'clear'; leaseBatch = [ordered]@{ token = $LeaseBatch; leaseId = $lid; seq = $k + 2; reused = $true; untilEpoch = $now.ToUnixTimeSeconds() + $remain } } }
# the agent's renew record at the leg's end: okMaxGapSec from FAKE_LEG_GAPS (default 3 s; 'none' = no record)
$gap = Pick $env:FAKE_LEG_GAPS $k '3'
if ($gap -ne 'none') { $res.admission['leaseRenew'] = [ordered]@{ intervalSec = 60; ok = 5; okMaxGapSec = [double]$gap; staleSec = 120 } }
# r3: FAKE_LEG_REFUSALS per call: the exit-75 refusals of a fresh lease the leg's own job asked for; FAKE_LEG_STOPS per call: the agent stopped the job at its
# batch lease's bound (lease-end | demoted | lease-lost; 'none' = it ended on its own)
$nRef = [int](Pick $env:FAKE_LEG_REFUSALS $k '0')
if ($nRef -gt 0) { $res.admission['leaseRefusals'] = @(1..$nRef | ForEach-Object { [ordered]@{ reason = 'refused'; rc = 75; why = 'quiet lease cooldown' } }) }
$stop = Pick $env:FAKE_LEG_STOPS $k 'none'
if ($stop -ne 'none') { $res.exitCode = 124; $res.admission['slotContinuity'] = 'lost'; $res.admission['leaseStop'] = [ordered]@{ reason = $stop; why = 'fake' } }
# r5 (adversary r4 pass 3 A1 sibling): FAKE_LEG_LB_WRAP per call: array publishes the leg's leaseBatch (this batch's lease) inside a one-element array | none
if ((Pick $env:FAKE_LEG_LB_WRAP $k 'none') -eq 'array') { $res.admission['leaseBatch'] = @($res.admission['leaseBatch']) }
# r5 pass 3 (adversary r5 pass 2 A1): FAKE_LEG_ADM_WRAP per call: array publishes the leg's whole admission inside a one-element array | none
if ((Pick $env:FAKE_LEG_ADM_WRAP $k 'none') -eq 'array') { $res['admission'] = @($res['admission']) }
# r5b: FAKE_LEG_MODES 'unresolved' = um-run said UNRESOLVED for the leg's job (um-run.ps1:480-501): no agent result lands by name, and Invoke-VenueLeg still
# writes its receipt (DVE_OUTCOME=UNRESOLVED) and exits 0
if ($legMode -ne 'unresolved') { $res | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outbox "$jobId.result.json") }
$id = [guid]::NewGuid().ToString()
# LEASE-BATCH-RECEIPT-ROOT-1: like the real leg, the receipt goes under -ReceiptRoot when given and its evidence in the directory beside the root (the default root is <case>\receipts)
$rroot = $(if ($ReceiptRoot) { $ReceiptRoot } else { Join-Path $env:FAKE_LEG_ROOT 'receipts' })
$rdir = Join-Path $rroot "CARD-1\$leaf\$Venue"
New-Item -ItemType Directory -Force -Path $rdir | Out-Null
$rp = Join-Path $rdir "$id.json"; Set-Content -LiteralPath $rp -Value '{}'
$edir = Join-Path (Join-Path (Split-Path -Parent $rroot) 'evidence') $id; New-Item -ItemType Directory -Force -Path $edir | Out-Null
$pre = Pick $env:FAKE_LEG_PRELOADS $k '5'
if ($pre -ne 'none') { Set-Content -LiteralPath (Join-Path $edir 'summary.json') -Value ('{"cpuQuiescence":{"timeMeanPercent":' + $pre + '}}') }
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: FAKE_LEG_HOSTLOADS per call = the smoke result.json's hostLoad: quiet (default) | cim (the 2026-11-21 a134532e shape:
# before/after collected=false CimException, state unknown, provisional true) | missing (a result.json with no hostLoad block) | none (no result.json)
$hlMode = Pick $env:FAKE_LEG_HOSTLOADS $k 'quiet'
# r6 (sol r5 B2): expshape = a CIM-failed result.json and, in the receipt slot, a receipt shaped like an export receipt that binds nothing
$hlBlock = switch ($hlMode) {
    { $_ -in 'cim', 'expshape' } { '{"before":{"collected":false,"error":"collection failed: CimException"},"after":{"collected":false,"error":"collection failed: CimException"},"state":"unknown","provisional":true,"reason":"Host load telemetry could not be collected"}' }
    'missing' { $null }
    default { '{"before":{"collected":true},"after":{"collected":true},"state":"quiet","provisional":false,"reason":"quiet"}' }
}
if ($hlMode -ne 'none') {
    $resPath = Join-Path $edir 'result.json'
    Set-Content -LiteralPath $resPath -Value ('{"schema":"mlvapp-gui-smoke-result.v2"' + $(if ($hlBlock) { ',"hostLoad":' + $hlBlock }) + '}')
    # like the real leg, the receipt binds the smoke result it was written from (evidence.resultJsonSha256; adversary-b0c05520.json A1)
    Set-Content -LiteralPath $rp -Value ('{"evidence":{"resultJsonSha256":"' + (Get-FileHash -LiteralPath $resPath -Algorithm SHA256).Hash.ToLowerInvariant() + '"}}')
}
if ($hlMode -eq 'expshape') { Set-Content -LiteralPath $rp -Value ('{"schema":"mlv-app/dual-venue-receipt/v1","scale":null,"display":null,"evidence":{},"export":{"format":"prores","receiptSha256":"' + ('0' * 64) + '","verdict":null}}') }
# r4 (adversary r4 A1): FAKE_LEG_TAIL_SLEEP = the leg child's own work after its agent job ended (its receipt, its evidence), before it exits
if ($env:FAKE_LEG_TAIL_SLEEP) { Start-Sleep -Seconds ([int]$env:FAKE_LEG_TAIL_SLEEP) }
# r6 (adversary-848f6559.json A1): FAKE_LEG_EXIT per call = the child exits with that code AFTER its agent job's result landed (a receipt write denied, Invoke-VenueLeg.ps1:314)
$legExit = [int](Pick $env:FAKE_LEG_EXIT $k '0'); if ($legExit -ne 0) { exit $legExit }
"DVE_OUTCOME=$(if ($legMode -eq 'unresolved') { 'UNRESOLVED' } else { Pick $env:FAKE_LEG_OUTCOMES $k 'PASS' })"
"DVE_RECEIPT_PATH=$rp"
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (A5): like the real leg, the sha256 of the receipt bytes it wrote; FAKE_LEG_SWAP_RECEIPT per call = 1 then swaps in a
# quiet receipt + result.json pair that binds itself (the adversary's input), which the batch must read unknown
"DVE_RECEIPT_SHA256=$((Get-FileHash -LiteralPath $rp -Algorithm SHA256).Hash)"
# R2-NARROW-1 r2 (adversary-7c5bfe86.json A5): FAKE_LEG_STDERR_SHA per call = 1 then writes a DVE_RECEIPT_SHA256 line of zeroes to STDERR, after the real one on stdout
if ((Pick $env:FAKE_LEG_STDERR_SHA $k '0') -eq '1') { [Console]::Error.WriteLine('DVE_RECEIPT_SHA256=' + ('0' * 64)) }
# (adversary-baf2e69a.json A3) FAKE_LEG_STDERR_PATH per call = 1 then writes a DVE_RECEIPT_PATH line naming ANOTHER receipt to STDERR
if ((Pick $env:FAKE_LEG_STDERR_PATH $k '0') -eq '1') { [Console]::Error.WriteLine('DVE_RECEIPT_PATH=' + (Join-Path $rdir 'another-receipt.json')) }
if ((Pick $env:FAKE_LEG_SWAP_RECEIPT $k '0') -eq '1') {
    $swapRes = Join-Path $edir 'result.json'; Set-Content -LiteralPath $swapRes -Value '{"schema":"mlvapp-gui-smoke-result.v2","hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'
    Set-Content -LiteralPath $rp -Value ('{"evidence":{"resultJsonSha256":"' + (Get-FileHash -LiteralPath $swapRes -Algorithm SHA256).Hash.ToLowerInvariant() + '"}}')
}
exit 0
"""


class _FakeAgentGate(threading.Thread):
    """r3 (sol r2 B1): the agent holding a batch's open in its admission gate, re-asking a refused (exit 75) lease request. It answers a cancel file for
    the open with the agent's cancelled-in-gate result (Publish-GateUnlaunchedResult). Told the runner has exited, it takes the lease it was asking for
    if the open was never cancelled: heavy-slot.py admits the pending request once the foreign holder clears."""

    def __init__(self, share: Path, claim_path: Path) -> None:
        super().__init__(daemon=True)
        self.share, self.claim_path = share, claim_path
        self.runner_exited = threading.Event()
        self.cancel_seen: tuple[str, bool] | None = None   # (open job id, was the runner's claim still held then)
        self.acquired_after_exit = False

    def run(self) -> None:
        while not self.runner_exited.is_set():
            for f in (self.share / "cancel").glob("lease-batch-open-*.cancel"):
                if self.cancel_seen is None:
                    job = f.name[: -len(".cancel")]
                    self.cancel_seen = (job, self.claim_path.exists())
                    outbox = self.share / "outbox"
                    outbox.mkdir(exist_ok=True)
                    res = {"jobId": job, "exitCode": 130, "cancelled": True, "endedUtc": utc_iso(datetime.now(timezone.utc)),
                           "admission": {"gate": "fake", "mode": "lease", "label": "cancelled-in-gate", "state": "cancelled", "why": "cancel file present before launch",
                                         "leaseRefusals": [{"reason": "refused", "rc": 75, "why": "quiet lease cooldown"}]}}
                    tmp = outbox / f"{job}.result.json.tmp"
                    tmp.write_text(json.dumps(res), encoding="utf-8")
                    os.replace(tmp, outbox / f"{job}.result.json")
            time.sleep(0.1)
        if self.cancel_seen is None:
            self.acquired_after_exit = True


def _pid_alive(pid: int) -> bool:
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259   # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def _wait_pid_gone(pid: int, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while _pid_alive(pid) and time.monotonic() < end:
        time.sleep(0.1)
    return not _pid_alive(pid)


PS_SINGLE_QUOTES = "'‘’‚‛"   # the five characters PowerShell's tokenizer treats as a single quote


def psq(value: object) -> str:
    """`value` for the inside of a PowerShell single-quoted literal: every embedded single-quote character (the ASCII one and the four typographic ones PowerShell also accepts) is
    doubled, so a temp dir or a message that carries an apostrophe cannot end the literal."""
    return "".join(c * 2 if c in PS_SINGLE_QUOTES else c for c in str(value))


_WALL_CLOCK_READ = re.compile(r"UtcNow|Get-Date|\[DateTime\]::Now|\[datetime\]::Now", re.IGNORECASE)
_WAIT_TIMING_NAME = re.compile(r"\$deadline\b|\$gateEnd\b|\$waitCutoff\b|\$now\b|waitedSec|-\s*\$t0\b|\$wu\b|\$wts\b", re.IGNORECASE)


def wvq_wall_clock_timing_lines(text: str) -> list[str]:
    """Code lines of Wait-VenueQuiet.ps1 that read the wall clock (UtcNow, Get-Date, [DateTime]::Now) on the same line as a deadline, the gate end, the caller's cutoff, `$now`, `$t0`
    arithmetic or a waitedSec report. Those must all read Get-WaitNowUtc ($t0 plus the Stopwatch time since $t0Ts). Comment lines are skipped; a HH:mm:ss log stamp or a job id
    on a line with none of those names is not timing and is not flagged."""
    return [ln.strip() for ln in text.replace("\r\n", "\n").splitlines()
            if not ln.strip().startswith("#") and _WALL_CLOCK_READ.search(ln) and _WAIT_TIMING_NAME.search(ln)]


def leg_receipt_root_guard_ps(leg: Path, root: str, offline: bool = False) -> str:
    """PowerShell (to follow an Import-Module of DualVenueRunner.psm1) that runs the REAL receipt-root admission of Invoke-VenueLeg.ps1 on `root`: its own three statements, from
    `$rrCert = ...` (certify) through the refusal to `$ReceiptRoot = $rrCert` (replace the raw value). Prints `ACCEPTED <the root the leg would write to>` or `REFUSED <message>`."""
    return (
        f"$legText = [IO.File]::ReadAllText('{psq(leg)}')\n"
        "$m = [regex]::Match($legText, '(?s)\\$rrCert = .*?\\$ReceiptRoot = \\$rrCert')\n"
        "if (-not $m.Success) { throw 'the leg receipt-root guard was not found' }\n"
        f"$ReceiptRoot = '{psq(root)}'; $OfflineTestMode = ${'true' if offline else 'false'}\n"
        "try { . ([scriptblock]::Create($m.Value)); 'ACCEPTED ' + $ReceiptRoot } catch { 'REFUSED ' + $_.Exception.Message }\n"
    )


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    """An ISO stamp as PowerShell's 'o' writes it (7 fraction digits, Z or an offset), as an aware UTC datetime."""
    body, frac = text.replace("Z", "+00:00"), ""
    if "." in body:
        head, rest = body.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        frac, tz = "." + digits[:6], rest[len(digits):]
        body = head + frac + tz
    dt = datetime.fromisoformat(body)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def read_claim_json(path: Path, timeout_s: float = 10.0, sleep_s: float = 0.1) -> dict:
    """A claim file read that waits out the runner's own write (CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-1, merge_group 40211588250).

    The runner takes the claim with CreateNew/FileShare.None and renews it in place under FileShare.None (VenueChain.psm1 Request-/Update-VenueClaim), so on Windows a
    reader that lands in that window gets PermissionError, on a half-written file JSONDecodeError, before the take FileNotFoundError. Retries those three for
    timeout_s, then raises the last one with the path.
    """
    end = time.monotonic() + timeout_s
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError, FileNotFoundError) as e:
            if time.monotonic() >= end:
                if isinstance(e, json.JSONDecodeError):
                    raise json.JSONDecodeError(f"{path}: {e.msg}", e.doc, e.pos) from e
                raise type(e)(e.errno, f"{e.strerror} (still failing after {timeout_s}s): {path}") from e
            time.sleep(sleep_s)


class ExclusiveHold:
    """`path` held open with share mode 0 (what Request-/Update-VenueClaim's FileShare.None does) until release() closes it.

    No thread and no timer: the hold ends only when release() runs, on whichever thread calls it, synchronously. A hold that expires on its own can lapse
    before the assertion that needs it, and a release that waits for another thread to be scheduled can land after a reader's deadline; neither is possible
    here (CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-HARDEN-1 r2, sol r1). release() is idempotent and thread-safe; callers put it in a finally."""

    def __init__(self, path: Path) -> None:
        import ctypes
        from ctypes import wintypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32.CloseHandle.restype = wintypes.BOOL
        self._handle = self._k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)   # GENERIC_READ|GENERIC_WRITE, share none, OPEN_EXISTING
        if self._handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._lock = threading.Lock()
        self.closed = False

    def release(self) -> bool:
        """Close the handle; True only for the call that closed it. A CloseHandle that fails raises OSError and leaves closed False: the hold may still be up."""
        import ctypes
        with self._lock:
            if self.closed:
                return False
            if not self._k32.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.closed = True
            return True


def batch_result(stdout: str) -> dict:
    lines = [l for l in stdout.splitlines() if l.startswith("LEASE_BATCH_RESULT=")]
    if not lines:
        raise AssertionError("no LEASE_BATCH_RESULT line in:\n" + stdout)
    return json.loads(lines[-1][len("LEASE_BATCH_RESULT="):])


# LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 r10: numeric-contract scanners for the dual-venue PowerShell. Both are tested on their own spellings below, so a pin cannot go blind
# to the one adjacent spelling it was written against (fable key3 r9 P-02).
def _ps_balanced(text: str, open_at: int, open_ch: str, close_ch: str) -> int:
    """Index of the bracket that closes the one at open_at, or -1."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _ps_balanced_back(text: str, close_at: int, open_ch: str, close_ch: str) -> int:
    """Index of the bracket that opens the one closing at close_at, or -1."""
    depth = 0
    for i in range(close_at, -1, -1):
        if text[i] == close_ch:
            depth += 1
        elif text[i] == open_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _ps_split_args(text: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


# an operand that is (or contains) a [double]: a double literal, a [double] cast, a TimeSpan total, a monotonic clock, or the runner's own double budget names
_PS_DOUBLE_OPERAND = re.compile(r"Total(?:Seconds|Milliseconds|Minutes)\b|\.Elapsed\b|\[(?:system\.)?double\]|(?<![\w.$])\d+\.\d+\b|\$(?:left|share)\b|\.sec\b", re.I)


# a second operand that is whole by construction (an [int] / [long] cast, or a Floor / Ceiling / Truncate): the Int32 overload loses nothing on it
_PS_INTEGRAL_OPERAND = re.compile(r"\s*(?:\[(?:int|int32|long|int64)\]|\[(?:system\.)?math\]::(?:floor|ceiling|truncate)\()", re.I)


def ps_integer_first_minmax(text: str) -> list[tuple[int, str]]:
    """Every [Math]::Min/Max call whose first operand is not a double while its second one is: PowerShell picks the Int32 overload for it and ROUNDS the double (2.3 -> 2).
    Heuristic: a second operand is a double only when it names one (a TimeSpan total, a clock, a double literal or cast, the runner's budget names). An untyped variable product such as
    `$f * $BudgetBasisSec` is invisible to it (VenueChain.psm1 Get-VenueLeaseCaps, carried as VENUE-LEASE-CAPS-MINMAX-OVERLOAD-1)."""
    hits = []
    for m in re.finditer(r"\[(?:system\.)?math\]::(?:min|max)\(", text, re.I):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if "#" in text[line_start:m.start()]:
            continue  # a comment
        close = _ps_balanced(text, m.end() - 1, "(", ")")
        args = _ps_split_args(text[m.end():close]) if close > 0 else []
        if len(args) == 2 and not _PS_DOUBLE_OPERAND.search(args[0]) and _PS_DOUBLE_OPERAND.search(args[1]) and not _PS_INTEGRAL_OPERAND.match(args[1]):
            hits.append((text.count("\n", 0, m.start()) + 1, text[m.start():close + 1]))
    return hits


_PS_FLOAT_TYPE = re.compile(r"(?:system\.)?(?:double|single|float|decimal)", re.I)


def ps_float_validaterange_params(text: str) -> list[tuple[int, str, bool]]:
    """Every [ValidateRange(..)] on a floating-point parameter, as (line, text, has_integer_endpoint). The type may follow the attribute, precede it ([double][ValidateRange(..)]),
    sit past other attributes ([Parameter()]) and the attribute may span lines."""
    found = []
    for m in re.finditer(r"\[ValidateRange\(", text, re.I):
        close = _ps_balanced(text, m.end() - 1, "(", ")")
        if close < 0:
            continue
        end = close + 1
        while end < len(text) and text[end].isspace():
            end += 1
        if text[end:end + 1] != "]":
            continue
        groups = []
        j = end + 1
        while True:
            while j < len(text) and text[j].isspace():
                j += 1
            if text[j:j + 1] != "[":
                break
            e = _ps_balanced(text, j, "[", "]")
            if e < 0:
                break
            groups.append(text[j + 1:e].strip())
            j = e + 1
        i = m.start() - 1
        while True:
            while i >= 0 and text[i].isspace():
                i -= 1
            if i < 0 or text[i] != "]":
                break
            b = _ps_balanced_back(text, i, "[", "]")
            if b < 0:
                break
            groups.append(text[b + 1:i].strip())
            i = b - 1
        if any(_PS_FLOAT_TYPE.fullmatch(g) for g in groups):
            ends = _ps_split_args(text[m.end():close])
            found.append((text.count("\n", 0, m.start()) + 1, text[m.start():end + 1], any(re.fullmatch(r"-?\d+", e) for e in ends)))
    return found


@requires_windows_pwsh
class VenueLeaseBatchRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="venue-lease-batch-"))
        for name, text in (("fake-quiet.ps1", FAKE_QUIET), ("fake-umrun.ps1", FAKE_UMRUN), ("fake-leg.ps1", FAKE_LEG)):
            (cls.tmp / name).write_text(text, encoding="utf-8")
        cls.specs = []
        for name in ("fx-a", "fx-b", "fx-c"):
            p = cls.tmp / f"{name}.json"
            # the shape Invoke-VenueLeg accepts (schema + the backends it may run + the scaleFactor every real leg spec carries and its receipt block reads), so a -LegPlan item can be checked against it before any lease
            # (card + legId are what Invoke-VenueLeg hands New-DvReceipt's Mandatory parameters; every committed leg spec carries both)
            p.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": name, "backends": ["cpu", "cuda"], "scaleFactor": 1}), encoding="utf-8")
            cls.specs.append(p)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))

    def _init_case(self, case: Path) -> None:
        self.case = case
        self.share = self.case / "share"
        self.share.mkdir()
        (self.share / "cancel").mkdir()   # the agent's cooperative cancel folder (the agent creates it on the real share)
        (self.share / "notafter").mkdir()   # r5: the agent's runner-liveness sidecar folder (#663; the agent creates it on the real share)
        self.claims = self.case / "claims"
        self.claim_path = self.claims / "bachelor.claim.json"
        self.events = self.case / "events.log"
        self.caps_events = self.case / "caps-events.log"
        self.run_dir = self.case / "run"
        self.runner = RUNNER

    def run_batch(self, specs: list[Path] | None = None, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args, e = self.batch_cmd(specs, *extra, **env)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def batch_cmd(self, specs: list[Path] | None = None, *extra: str, **env: str) -> tuple[list[str], dict]:
        e = dict(os.environ)
        e.update({"FAKE_EVENTS": str(self.events), "FAKE_SHARE": str(self.share), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case),
                  "FAKE_CAPS_EVENTS": str(self.caps_events)})
        omit = [x for x in env.pop("OMIT_DEFAULTS", "").split(",") if x]
        e.update(env)
        # Bachelor's 30 min cooldown is waited out between batches; the offline cases (a stub um-run) relax it unless they test it. A case may override any default
        # (in extra) or drop one (OMIT_DEFAULTS=-LeaseCooldownSec), which is how a case reads the caps the runner takes from the live probe.
        defaults = {"-LegBudgetSec": "60", "-LeaseMarginSec": "0", "-ClaimPollSec": "1", "-RecheckSec": "1",
                    "-DeadlineUtc": utc_iso(datetime.now(timezone.utc) + timedelta(seconds=240)), "-LeaseCooldownSec": "0"}
        for k in omit:
            defaults.pop(k)
        args = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.runner), "-LegSpec", ",".join(str(s) for s in (specs or self.specs[:2])),
                "-RunDir", str(self.run_dir), "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST, "-ClaimDir", str(self.claims),
                "-QuietScript", str(self.tmp / "fake-quiet.ps1"), "-LegScript", str(self.tmp / "fake-leg.ps1"), "-UmRunScript", str(self.tmp / "fake-umrun.ps1"),
                "-AgentShare", str(self.share), *[x for k, v in defaults.items() if k not in extra for x in (k, v)], *extra]
        return args, e

    def kinds(self) -> list[str]:
        """The event order: OPEN / CLOSE (um-run), QUIET:<mode>, LEG:<leaf>:<leaseBatch>."""
        rows = []
        for line in self.events.read_text(encoding="utf-8").splitlines() if self.events.exists() else []:
            ticks, kind, rest = line.split(" ", 2)
            if kind == "UMRUN":
                job = rest.split()[0]
                rows.append((int(ticks), "OPEN:" + job[-12:] if job.startswith("lease-batch-open-") else "CLOSE:" + job[-12:]))
            elif kind == "QUIET":
                rows.append((int(ticks), "QUIET:" + rest.split()[0]))
            elif kind == "LEG":
                leaf, lb, _held = rest.split()
                rows.append((int(ticks), f"LEG:{leaf}:{lb.split('=', 1)[1]}"))
        return [k for _, k in sorted(rows)]

    def events_raw(self) -> list[str]:
        return self.events.read_text(encoding="utf-8").splitlines()

    def batches(self) -> list[dict]:
        return [json.loads(l) for l in (self.run_dir / "batches.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---------------------------------------------------------------- lease first, one lease for every leg
    def test_lease_first_and_one_lease_for_every_leg(self) -> None:
        r = self.run_batch(self.specs)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        opens = [x for x in k if x.startswith("OPEN:")]
        self.assertEqual(len(opens), 1, k)
        token = opens[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-b:{token}", "QUIET:QUIET", f"LEG:fx-c:{token}", f"CLOSE:{token}"])
        # the claim is held from the open to the close, and released after
        held = [l for l in self.events_raw() if " UMRUN " in l or " LEG " in l]
        self.assertEqual(len(held), 5)
        self.assertTrue(all("claimHeld=True" in l for l in held), held)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"], res["notRun"]), (3, 3, 0))
        [b] = self.batches()
        self.assertEqual(b["schema"], "mlv-app/venue-lease-batch/v1")
        self.assertEqual(b["token"], token)
        self.assertEqual((b["leaseId"], b["held"], b["endReason"]), ("lease-1", "1:1001", "ALL_LEGS_DONE"))
        self.assertEqual((b["legsAttempted"], b["legsRan"], b["legsCounted"], b["retries"]), (3, 3, 3, 0))
        self.assertEqual((b["openWaitedSec"], b["openRequests"], len(b["refusals"])), (7, 2, 1))
        self.assertEqual(b["agent"]["endReason"], "closed", "the agent's own receipt for the lease is attached")
        self.assertTrue(b["startedUtc"] and b["endedUtc"] and b["leaseEndUtc"])
        self.assertTrue(all(l["onLease"] and l["jobLease"] == "lease-1" and l["class"] == "QUIET_PRELOAD_OK" for l in b["legs"]), b["legs"])
        self.assertTrue((self.run_dir / "batch-01.json").exists())
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(" BATCH_OPEN batch=1 ", log)
        self.assertIn(" BATCH_END batch=1 ", log)
        # the open job is a trivial script and the close job too: nothing else rides on them
        bodies = [l for l in self.events_raw() if " UMRUN " in l]
        self.assertIn("LEASE_BATCH_OPEN token=", bodies[0])
        self.assertIn("LEASE_BATCH_CLOSE token=", bodies[-1])
        # r5: the open's runner-liveness sidecar was written while the open was owned and removed once its result was in hand; the claim had a keeper
        self.assertEqual((b["openLiveness"]["end"], b["openLiveness"]["writeFailures"]), ("REMOVED", 0), b["openLiveness"])
        self.assertEqual(list((self.share / "notafter").iterdir()), [], "the open's sidecar was left behind")
        self.assertIsNone(b["claimRenew"]["lost"], b["claimRenew"])
        self.assertIn(f" BATCH_OPEN_LIVENESS batch=1 job=lease-batch-open-", log)

    # ---------------------------------------------------------------- VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1
    def test_a_leg_whose_smoke_host_load_is_unknown_is_recorded_but_never_counted(self) -> None:
        # The 2026-11-21 a134532e shape: pre-load 5 % (QUIET_PRELOAD_OK by the pre-load alone), but the smoke's hostLoad was not collected
        # (CimException): state unknown, provisional true. RED: the leg counted, and no row or line said its host-load state.
        r = self.run_batch(self.specs[:3], FAKE_LEG_HOSTLOADS="cim,quiet,missing")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        got = [(l["status"], l["class"], l["counted"], l.get("hostLoadState"), l.get("hostLoadProvisional")) for l in b["legs"]]
        self.assertEqual(got, [("RAN", "HOSTLOAD_PROVISIONAL", False, "unknown", True), ("RAN", "QUIET_PRELOAD_OK", True, "quiet", False),
                               ("RAN", "HOSTLOAD_PROVISIONAL", False, "unknown", True)], b["legs"])
        self.assertEqual((b["legsRan"], b["legsCounted"]), (3, 1), "a provisional leg is recorded as run, never counted")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"]), (3, 1))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        rid = Path(b["legs"][0]["receipt"]).stem
        self.assertIn(f"REFERENCE_PROVISIONAL leg={rid} reason=hostload-unknown", log)
        self.assertIn("class=HOSTLOAD_PROVISIONAL counted=False", log)
        self.assertIn("hostLoadState=unknown hostLoadProvisional=True", log)
        self.assertIn("hostLoadState=quiet hostLoadProvisional=False", log)

    def test_a_provisional_host_load_is_named_whatever_the_preload_class(self) -> None:
        # adversary-b0c05520.json A5, its input: pre-load 31 % (QUIET_PRELOAD_HIGH) and a CIM-failed smoke host load. The class stays the pre-load's
        # (already uncounted), and the typed REFERENCE_PROVISIONAL line is still printed. RED: the line was printed only for HOSTLOAD_PROVISIONAL.
        r = self.run_batch(self.specs[:1], FAKE_LEG_PRELOADS="31", FAKE_LEG_HOSTLOADS="cim")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [l] = b["legs"]
        self.assertEqual((l["class"], l["counted"], l["hostLoadState"], l["hostLoadProvisional"]), ("QUIET_PRELOAD_HIGH", False, "unknown", True), l)
        self.assertIn(f"REFERENCE_PROVISIONAL leg={Path(l['receipt']).stem} reason=hostload-unknown", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    def test_r6_b2_an_export_shaped_receipt_in_a_playback_leg_is_never_counted(self) -> None:
        # sol r5 B2, its input: a playback leg whose receipt slot holds the production schema shaped as an export receipt (scale/display null, prores,
        # 64 zeroes, verdict null, no identity, no playback hashes), a CIM-failed smoke result and pre-load 5. RED: not-applicable, QUIET_PRELOAD_OK, counted.
        r = self.run_batch(self.specs[:1], FAKE_LEG_HOSTLOADS="expshape")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [l] = b["legs"]
        self.assertEqual((l["class"], l["counted"], l["hostLoadState"], l["hostLoadProvisional"]), ("HOSTLOAD_PROVISIONAL", False, "unknown", True), l)
        self.assertIn(f"REFERENCE_PROVISIONAL leg={Path(l['receipt']).stem} reason=hostload-unknown", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    def test_residue_r2_a5_a_receipt_and_result_swapped_in_together_after_the_leg_is_never_counted(self) -> None:
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2, adversary-07b6a932.json A5, its input: an uncollected smoke result, pre-load 5, and after the child printed its
        # receipt path a quiet receipt + result.json pair that binds itself replaces both. RED: QUIET_PRELOAD_OK, counted.
        r = self.run_batch(self.specs[:1], FAKE_LEG_HOSTLOADS="cim", FAKE_LEG_SWAP_RECEIPT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [l] = b["legs"]
        self.assertEqual((l["class"], l["counted"], l["hostLoadState"], l["hostLoadProvisional"]), ("HOSTLOAD_PROVISIONAL", False, "unknown", True), l)

    def test_r2n1_a5_a_receipt_sha_line_on_stderr_never_replaces_the_one_the_leg_published_on_stdout(self) -> None:
        # adversary-7c5bfe86.json A5 (R2-NARROW-1 r2), its input: a qualifying quiet leg (pre-load 5, collected quiet host load) whose child prints the true DVE_RECEIPT_SHA256 on stdout and then
        # a DVE_RECEIPT_SHA256 line of 64 zeroes on STDERR. RED on 7c5bfe86: stdout and stderr were read together, the last line won, the true hash was discarded, the genuine receipt was refused and the
        # leg was HOSTLOAD_PROVISIONAL (uncounted). The hash is now taken from the child's stdout alone (as Invoke-VenueChain does), so the leg is counted.
        r = self.run_batch(self.specs[:1], FAKE_LEG_STDERR_SHA="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [l] = b["legs"]
        self.assertEqual((l["class"], l["counted"], l["hostLoadState"], l["hostLoadProvisional"]), ("QUIET_PRELOAD_OK", True, "quiet", False), l)

    def test_r2n1_pass1_a3_a_receipt_path_line_on_stderr_never_replaces_the_one_the_leg_published_on_stdout(self) -> None:
        # adversary-baf2e69a.json A3, its input: a qualifying quiet leg whose child prints DVE_RECEIPT_PATH and DVE_RECEIPT_SHA256 for its own receipt on stdout and then a DVE_RECEIPT_PATH line naming ANOTHER
        # receipt on STDERR. RED on baf2e69a: the sha came from stdout but the path from stdout and stderr together (the last line won), so the host-load read was asked for the wrong path with the right
        # digest, refused the pair, and a genuinely quiet counted leg became HOSTLOAD_PROVISIONAL. The path and the hash now come from the same stream, the child's stdout.
        r = self.run_batch(self.specs[:1], FAKE_LEG_STDERR_PATH="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [l] = b["legs"]
        self.assertEqual((l["class"], l["counted"], l["hostLoadState"], l["hostLoadProvisional"]), ("QUIET_PRELOAD_OK", True, "quiet", False), l)
        self.assertNotIn("another-receipt", str(l["receipt"]))

    def test_a_quiet_host_load_leaves_todays_counting_unchanged(self) -> None:
        r = self.run_batch(self.specs[:1])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [l] = b["legs"]
        self.assertEqual((l["class"], l["counted"], l["hostLoadState"], l["hostLoadProvisional"]), ("QUIET_PRELOAD_OK", True, "quiet", False), l)
        self.assertNotIn("REFERENCE_PROVISIONAL", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- in-lease retry
    def test_a_busy_probe_is_retried_inside_the_same_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_QUIET_MODES="COOLDOWN,BUSY,FOREIGN,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "QUIET", "QUIET", "QUIET", "QUIET", "LEG", "CLOSE"], k)
        [b] = self.batches()
        # r5 (V-01): the FOREIGN probe among this leg's attempts sticks, so the leg ran but is not counted (Invoke-VenueChain's rule).
        self.assertEqual((b["probeRetries"], b["retries"], b["legsRan"], b["legsCounted"], b["endReason"]), (3, 3, 1, 0, "ALL_LEGS_DONE"))
        self.assertEqual((b["legs"][0]["class"], b["legs"][0]["foreignFloor"]), ("COOLDOWN_UNMET", True), b["legs"])
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        for reason in ("COOLDOWN_UNMET", "GATE_BUSY"):
            self.assertIn(f"RETRY_IN_LEASE leg=01-fx-a-spec batch=1 reason={reason}", log)

    def test_a_foreign_floor_then_a_quiet_retry_leaves_the_leg_uncounted_and_never_the_next_leg(self) -> None:
        # r5 (V-01, key3): a FOREIGN_FLOOR on the first in-lease probe, then QUIET on the retry. The leg runs on the lease, but the floor seen in an earlier
        # attempt of the SAME leg sticks: class COOLDOWN_UNMET, uncounted. The next leg's own QUIET probe is clean, so it counts. RED: the leg counted.
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="FOREIGN,QUIET,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        token = k[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:FOREIGN", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-b:{token}", f"CLOSE:{token}"])
        [b] = self.batches()
        a, nxt = b["legs"]
        self.assertEqual((a["status"], a["class"], a["counted"], a["quietVerdict"]), ("RAN", "COOLDOWN_UNMET", False, "QUIET"), a)
        self.assertEqual((b["probeRetries"], b["legsRan"], b["legsCounted"]), (1, 2, 1))
        self.assertEqual((nxt["class"], nxt["counted"]), ("QUIET_PRELOAD_OK", True), nxt)
        self.assertEqual((a["foreignFloor"], nxt["foreignFloor"]), (True, False), b["legs"])
        self.assertTrue(a["foreignFloorLine"].startswith("FOREIGN_FLOOR owners="), a)
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"]), (2, 1))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn("LEG 01-fx-a-spec RAN batch=1 ", log)
        self.assertIn("class=COOLDOWN_UNMET counted=False", log)

    def test_a_leg_whose_own_recheck_failed_is_retried_inside_the_same_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEG_OUTCOMES="VENUE_NOT_QUIESCENT,PASS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        token = k[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", f"CLOSE:{token}"])
        [b] = self.batches()
        self.assertEqual((b["legRetries"], b["retries"], b["legsAttempted"], b["legsRan"], b["legsCounted"]), (1, 1, 2, 1, 1))
        self.assertEqual([l["status"] for l in b["legs"]], ["RETRY", "RAN"])
        res = batch_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN"])

    # ---------------------------------------------------------------- batch ends
    def test_a_lease_too_short_for_the_next_leg_ends_the_batch_and_the_rest_go_to_the_next(self) -> None:
        # lease 1 has room for one 60 s leg (70 s), then the leg's own result says 50 s are left: the second leg goes to batch 2.
        r = self.run_batch(self.specs[:2], FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        t1, t2 = [x.split(":")[1] for x in k if x.startswith("OPEN:")]
        self.assertNotEqual(t1, t2)
        self.assertEqual(k, [f"OPEN:{t1}", "QUIET:QUIET", f"LEG:fx-a:{t1}", f"CLOSE:{t1}", f"OPEN:{t2}", "QUIET:QUIET", f"LEG:fx-b:{t2}", f"CLOSE:{t2}"])
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"], b2["endReason"], b2["legsRan"]), ("LEASE_REMAINING_SHORT", 1, "ALL_LEGS_DONE", 1))
        self.assertIn("goes to the next batch", b1["endDetail"])
        self.assertEqual((b1["leaseId"], b2["leaseId"]), ("lease-1", "lease-2"))

    def test_a_leg_that_did_not_run_on_the_batch_lease_is_uncounted_and_ends_the_batch(self) -> None:
        r = self.run_batch(self.specs[:2], FAKE_LEG_LOSE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"]), ("LEASE_LOST", 1, 0))
        self.assertEqual((b1["legs"][0]["class"], b1["legs"][0]["counted"], b1["legs"][0]["jobLease"]), ("LEASE_LOST", False, "lease-somebody-else"))
        self.assertEqual((b2["legsRan"], b2["legsCounted"]), (1, 1))
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"]), (2, 1))

    # ---------------------------------------------------------------- LEASE-BATCH-IDLE-END-UNSEEN-1: the agent's idle release of the parked batch lease
    def test_an_idle_end_of_the_lease_during_a_quiet_wait_ends_the_batch_before_the_leg_launches(self) -> None:
        # ledger QUEUED 2026-11-05T20:06:29Z (venue-42012aec): the agent released the idle batch lease (no job for 301 s > 300 s) while the leg's quiet probe waited, the runner kept counting
        # its room, and the leg then ran on a fresh lease under the token: LEASE_LOST, a clean leg thrown away. The runner now reads the agent's receipt after the probe.
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="QUIET,IDLE,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        t1, t2 = b1["token"], b2["token"]
        self.assertEqual(self.kinds(), [f"OPEN:{t1}", "QUIET:QUIET", f"LEG:fx-a:{t1}", "QUIET:IDLE", f"CLOSE:{t1}", f"OPEN:{t2}", "QUIET:QUIET", f"LEG:fx-b:{t2}", f"CLOSE:{t2}"])
        self.assertEqual((b1["endReason"], b1["legsAttempted"], b1["legsRan"], b1["legsCounted"]), ("LEASE_IDLE_ENDED", 1, 1, 1), b1)
        self.assertNotIn("LEASE_LOST", [l.get("class") for b in (b1, b2) for l in b["legs"]], "an idle end is never typed LEASE_LOST")
        self.assertEqual((b2["endReason"], b2["legsRan"], b2["legsCounted"]), ("ALL_LEGS_DONE", 1, 1), "the leg goes to the next batch and counts there")
        self.assertEqual(b1["agent"]["endReason"], "idle", "the agent's own receipt for the lease is attached")
        self.assertIn("the agent released lease lease-1 as idle", b1["endDetail"])
        # the room accounting stops at the idle end: the lease's end is clamped to the agent's endedUtc, and no later line of batch 1 counts room
        ended = datetime.fromisoformat(b1["agent"]["endedUtc"].replace("Z", "+00:00"))
        lease_end = datetime.fromisoformat(b1["leaseEndUtc"].replace("Z", "+00:00"))
        self.assertLessEqual(lease_end, ended + timedelta(seconds=1), (b1["leaseEndUtc"], b1["agent"]["endedUtc"]))
        lines = (self.run_dir / "batch.log").read_text(encoding="utf-8").splitlines()
        at = next(i for i, l in enumerate(lines) if " LEASE_IDLE_ENDED batch=1 " in l)
        self.assertFalse([l for l in lines[at + 1:] if "batch=1 " in l and "roomSec=" in l and " BATCH_END " not in l], lines[at:])
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"]), (2, 2))

    def test_lease_batch_idle_end_r2_adv_a2_the_idle_clamp_is_the_agents_instant_on_any_host_offset(self) -> None:
        # adversary r2 pass 2 A2 / opus r1 H1: the clamp cast ConvertFrom-Json's endedUtc (an offset stamp becomes a Local DateTime) through [string], dropping the zone, so on a
        # host west or east of UTC leaseEndUtc moved by the host's offset. The fake agent stamps endedUtc with '+00:00' at the probe; the clamp must land on that instant.
        before = datetime.now(timezone.utc)
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="QUIET,IDLE,QUIET")
        after = datetime.now(timezone.utc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        lease_end = datetime.fromisoformat(b1["leaseEndUtc"].replace("Z", "+00:00"))
        self.assertTrue(before - timedelta(seconds=5) <= lease_end <= after + timedelta(seconds=5), (b1["leaseEndUtc"], before, after))

    def test_a_leg_that_finished_after_the_agent_idled_its_lease_is_typed_idle_ended_not_lease_lost(self) -> None:
        # the idle release landing after the pre-launch read: the leg's job took a fresh lease under the token (not the batch's), and the agent's receipt for the batch lease says idle
        r = self.run_batch(self.specs[:2], FAKE_LEG_LOSE="1", FAKE_LEG_IDLE_END="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"]), ("LEASE_IDLE_ENDED", 1, 0), b1)
        self.assertEqual((b1["legs"][0]["class"], b1["legs"][0]["counted"], b1["legs"][0]["onLease"]), ("LEASE_IDLE_ENDED", False, False), b1["legs"][0])
        # r4 (adversary r4 A2 sibling of Grok G9): this site types the end through the same helper: the lease's end clamped to the agent's endedUtc and the LEASE_IDLE_ENDED line
        self._assert_idle_end_clamped_and_logged(b1)

    def _assert_idle_end_clamped_and_logged(self, b: dict) -> None:
        rows = [json.loads(l) for l in (self.share / "logs" / f"lease-batch-{b['token']}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        idle = [x for x in rows if x.get("endReason") == "idle" and x.get("token") == b["token"]]
        self.assertEqual(len(idle), 1, rows)
        ended = datetime.fromisoformat(idle[0]["endedUtc"].replace("Z", "+00:00"))
        lease_end = datetime.fromisoformat(b["leaseEndUtc"].replace("Z", "+00:00"))
        self.assertLess(abs((lease_end - ended).total_seconds()), 1.0, (b["leaseEndUtc"], idle[0]["endedUtc"]))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" LEASE_IDLE_ENDED batch={b['batch']} ", log)

    def test_a_leg_never_launches_after_the_run_deadline_passed_between_its_probe_and_its_launch(self) -> None:
        # adversary A1 (adversary-7ce11cb9.json): the launch guard checked the claim and the lease's room only, so the receipt read after a probe (or any slow guard) could carry the
        # run past -DeadlineUtc and the leg still launched. The launch is the dispatch: a passed deadline starts nothing (DEADLINE, exit 3, the leg goes unrun).
        dl = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=25))
        r = self.run_batch(self.specs[:1], "-DeadlineUtc", dl, FAKE_QUIET_MODES="PASTDEADLINE", FAKE_DEADLINE_UTC=dl)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        b = self.batches()[0]
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("DEADLINE", 0, 0), b)
        self.assertNotIn("LEG:fx-a", " ".join(k.rsplit(":", 1)[0] for k in self.kinds()), self.kinds())
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (3, "DEADLINE", 0, 1))

    def test_a_lost_lease_with_no_idle_receipt_is_still_lease_lost(self) -> None:
        # control: the typed idle end needs the agent's endReason=idle line for THIS lease; another end reason on it leaves LEASE_LOST as it was
        r = self.run_batch(self.specs[:2], FAKE_LEG_LOSE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["endReason"], b1["legs"][0]["class"], b1["agent"]["endReason"]), ("LEASE_LOST", "LEASE_LOST", "closed"), b1)

    # ---------------------------------------------------------------- r2 (sol key r1 BLOCKER): a receipt line is this batch's only when its token AND lease id are this batch's
    def test_r2_a_foreign_token_line_before_the_agents_own_idle_line_does_not_hide_the_idle_end(self) -> None:
        # sol r1 repro: a foreign-token closed line for this lease, then the agent's idle line under this token. The reader returned the first lease match, so no idle end was seen
        # and the next leg launched on an ended lease.
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="QUIET,IDLEFOREIGN,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        t1, t2 = b1["token"], b2["token"]
        self.assertEqual(self.kinds(), [f"OPEN:{t1}", "QUIET:QUIET", f"LEG:fx-a:{t1}", "QUIET:IDLEFOREIGN", f"CLOSE:{t1}", f"OPEN:{t2}", "QUIET:QUIET", f"LEG:fx-b:{t2}", f"CLOSE:{t2}"])
        self.assertEqual((b1["endReason"], b1["legsAttempted"], b1["legsRan"]), ("LEASE_IDLE_ENDED", 1, 1), b1)
        self.assertEqual((b1["agent"]["token"], b1["agent"]["endReason"]), (t1, "idle"), b1["agent"])

    def test_r2_an_idle_line_under_another_token_or_none_is_no_idle_end_of_this_batch(self) -> None:
        for mode in ("FOREIGNIDLE", "NOTOKENIDLE"):
            with self.subTest(mode=mode):
                self.setUp()
                r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES=f"QUIET,{mode},QUIET")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                [b1] = self.batches()
                t1 = b1["token"]
                self.assertEqual(self.kinds(), [f"OPEN:{t1}", "QUIET:QUIET", f"LEG:fx-a:{t1}", f"QUIET:{mode}", f"LEG:fx-b:{t1}", f"CLOSE:{t1}"])
                self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"]), ("ALL_LEGS_DONE", 2, 2), b1)
                self.assertEqual((b1["agent"]["token"], b1["agent"]["endReason"]), (t1, "closed"), b1["agent"])

    def test_r2_the_batch_attaches_only_its_own_tokens_receipt_line(self) -> None:
        # the class sweep's third consumer (the receipt attached after the close): a foreign-token line for this lease came first and was attached as the agent's receipt
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="QUIET,FOREIGNEND,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b1] = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"]), ("ALL_LEGS_DONE", 2), b1)
        self.assertEqual((b1["agent"]["token"], b1["agent"]["endReason"]), (b1["token"], "closed"), b1["agent"])

    # ---------------------------------------------------------------- r3 (Grok CoS G1 on #817): the probe's wait vs the agent's idle bound, and the churn it caused
    def _in_lease_waits(self) -> list[int]:
        return [int(l.rsplit("maxWait=", 1)[1]) for l in self.events_raw() if " QUIET " in l]

    def test_lease_batch_idle_end_r3_grok_g1_consecutive_zero_leg_idle_ends_stop_the_run_typed(self) -> None:
        # G1: a venue whose probe outlasts the agent's idle bound every time ended batch after batch LEASE_IDLE_ENDED with no leg, each one a fresh lease (a ledger and budget charge),
        # until the deadline. Batch 1 runs fx-a and idles on fx-b's probe (a leg was attempted: no churn yet); batches 2 and 3 open, idle and launch nothing: the second consecutive
        # zero-leg idle end stops the run typed (LEASE_IDLE_CHURN, exit 14), three leases in all, fx-b NOT_RUN, never a fourth open.
        dl = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=120))
        r = self.run_batch(self.specs[:2], "-DeadlineUtc", dl, FAKE_QUIET_MODES="QUIET,IDLE,IDLE,IDLE,IDLE,IDLE,IDLE,IDLE")
        self.assertEqual(r.returncode, 14, r.stdout + r.stderr)
        bs = self.batches()
        self.assertEqual(len(bs), 3, [(b["endReason"], b["legsAttempted"]) for b in bs])
        t1, t2, t3 = (b["token"] for b in bs)
        self.assertEqual(self.kinds(), [f"OPEN:{t1}", "QUIET:QUIET", f"LEG:fx-a:{t1}", "QUIET:IDLE", f"CLOSE:{t1}", f"OPEN:{t2}", "QUIET:IDLE", f"CLOSE:{t2}",
                                        f"OPEN:{t3}", "QUIET:IDLE", f"CLOSE:{t3}"])
        self.assertEqual([(b["endReason"], b["legsAttempted"]) for b in bs], [("LEASE_IDLE_ENDED", 1), ("LEASE_IDLE_ENDED", 0), ("LEASE_IDLE_ENDED", 0)])
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"], res["notRun"]), (14, "LEASE_IDLE_CHURN", 1, 1, 1))
        self.assertIn("LEASE_IDLE_CHURN", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    def test_lease_batch_idle_end_r3_grok_g1_one_zero_leg_idle_end_between_legs_is_no_churn(self) -> None:
        # control: a zero-leg idle end followed by a batch that launches a leg resets the count, so the run finishes (exit 0, 3 leases): only CONSECUTIVE zero-leg idle ends stop it
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="QUIET,IDLE,IDLE,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bs = self.batches()
        self.assertEqual([(b["endReason"], b["legsAttempted"]) for b in bs], [("LEASE_IDLE_ENDED", 1), ("LEASE_IDLE_ENDED", 0), ("ALL_LEGS_DONE", 1)])
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (0, None, 2, 0))

    def test_lease_batch_idle_end_r3_grok_g1_the_in_lease_probe_wait_stays_under_the_agents_idle_bound(self) -> None:
        # G1: -ProbeMaxWaitSec (600 by default) let one probe wait twice the agent's idle bound (LeaseBatchIdleSec 300) with no job, so the lease idled out under it. The in-lease
        # wait is cut to the agent's bound less -ProbeIdleMarginSec (60 by default): 240 s, with the lease's room far larger. -CooldownSec does NOT cut this wait (r4, Grok G6: a cooldown
        # at or above the bound less the margin is refused before any lease instead, see test_lease_batch_idle_end_r4_grok_g6_*).
        # r3 pass 2 (adversary A1): counted from the last agent job seen to end, so a probe a few seconds after the open or a leg gets a few seconds less than the bound
        def within(waits: list[int], top: int) -> bool:
            return len(waits) == 2 and all(top - 15 <= w <= top for w in waits)
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(within(self._in_lease_waits(), 240), self.events_raw())
        self.setUp()
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", "-ProbeIdleMarginSec", "100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(within(self._in_lease_waits(), 200), self.events_raw())
        # a shorter -ProbeMaxWaitSec is kept as it is
        self.setUp()
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "30")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._in_lease_waits(), [30, 30], self.events_raw())

    def _tree_with_agent_idle(self, idle: bytes) -> None:
        tree = self.case / "tree" / "tools" / "profiling"
        shutil.copytree(DV, tree / "dual-venue", ignore=shutil.ignore_patterns("look-receipts", "legs", "legsets"))
        (tree / "bachelor").mkdir()
        agent = AGENT.read_bytes()
        self.assertEqual(agent.count(b"[int]$LeaseBatchIdleSec = 300,"), 1, "the agent's idle bound moved: update this fixture")
        (tree / "bachelor" / AGENT.name).write_bytes(agent.replace(b"[int]$LeaseBatchIdleSec = 300,", b"[int]$LeaseBatchIdleSec = " + idle + b","))
        self.runner = tree / "dual-venue" / RUNNER.name

    def test_lease_batch_idle_end_r3_grok_g1_the_idle_bound_is_read_from_the_agent_script_not_a_second_copy(self) -> None:
        # the bound has ONE home, the agent's own param default (LeaseBatchIdleSec): a copy of the tree whose agent says 200 cuts the wait to about 200 - 60 = 140
        self._tree_with_agent_idle(b"200")
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = self._in_lease_waits()
        self.assertTrue(len(waits) == 2 and all(125 <= w <= 140 for w in waits), self.events_raw())
        self.assertNotRegex(RUNNER.read_text(encoding="utf-8"), r"(?i)IdleSec\s*=\s*300\b", "the runner keeps no second copy of the agent's 300 s")

    def test_lease_batch_idle_end_r3_adv_a2_the_running_agents_published_idle_bound_wins_over_the_script_default(self) -> None:
        # adversary r3 A2 (adversary-55df4367.json): an agent started with -LeaseBatchIdleSec 100 releases after 100 s whatever its script's default says. The agent publishes its
        # effective bound (admission.leaseBatch.idleSec, Get-LeaseBatchAdmission) and the runner bounds its waits by it: about 100 - 60 = 40, never the default's 240.
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", FAKE_IDLE_SEC="100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = self._in_lease_waits()
        self.assertTrue(len(waits) == 2 and all(25 <= w <= 40 for w in waits), self.events_raw())
        # and the agent publishes it
        self.assertIn("idleSec = $LeaseBatchIdleSec", AGENT.read_text(encoding="utf-8")[AGENT.read_text(encoding="utf-8").index("function Get-LeaseBatchAdmission"):][:900])

    def test_lease_batch_idle_end_r3_adv_a1_busy_gate_retries_share_one_idle_allowance(self) -> None:
        # adversary r3 A1: a GATE_BUSY probe runs no agent job, so the agent's idle clock keeps running across the retries; each retry used to get a fresh full allowance. Agent
        # bound 70, margin 60: one 10 s allowance from the open, spent by the retries (2 s sleeps), never renewed by a busy probe; the QUIET probe after a leg starts a new one.
        self._tree_with_agent_idle(b"70")
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", "-ClaimPollSec", "2", FAKE_QUIET_MODES="BUSY,BUSY,BUSY,BUSY,QUIET,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = self._in_lease_waits()
        self.assertEqual(len(waits), 6, self.events_raw())
        self.assertLessEqual(waits[0], 10, waits)
        self.assertTrue(all(b <= a for a, b in zip(waits[:4], waits[1:5])), f"a busy retry got a fresh allowance: {waits}")
        self.assertLessEqual(waits[4], 10 - 6, f"three 2 s busy sleeps spent at least 6 s of the allowance: {waits}")
        self.assertGreaterEqual(waits[5], 5, f"a leg's job restarts the allowance: {waits}")

    def _patch_tree_runner(self, old: str, new: str) -> None:
        text = self.runner.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, "the runner line moved: update this fixture")
        self.runner.write_text(text.replace(old, new), encoding="utf-8")

    def test_lease_batch_idle_end_r3_adv_p2_a1_the_idle_allowance_starts_at_the_opens_read_not_after_its_log(self) -> None:
        # adversary r3 pass 2 A1 (adversary-4c198b23.json): the anchor was taken after the BATCH_OPEN log append, so a stalled append was not spent from the allowance. Agent bound
        # 70, margin 60 (a 10 s allowance), a 6 s stall before BATCH_OPEN: the first probe may wait at most about 4 s, never the full 10.
        self._tree_with_agent_idle(b"70")
        self._patch_tree_runner('            L "BATCH_OPEN batch=$batchNo', '            Start-Sleep -Seconds 6; L "BATCH_OPEN batch=$batchNo')
        r = self.run_batch(self.specs[:1], "-ProbeMaxWaitSec", "600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = self._in_lease_waits()
        self.assertEqual(len(waits), 1, self.events_raw())
        self.assertLessEqual(waits[0], 4, f"the stalled open log was not spent from the idle allowance: {waits}")

    def test_lease_batch_idle_end_r3_adv_p2_a2_a_room_short_end_after_an_idle_release_is_typed_idle_ended(self) -> None:
        # adversary r3 pass 2 A2: the idle release landed while a busy probe's sleep ran (the post-probe read missed it: simulated by a first read that finds nothing), the loop top
        # then ended the batch LEASE_REMAINING_SHORT and the churn count was reset. The batch end now reads the agent's receipt first: LEASE_IDLE_ENDED, the leg runs in batch 2.
        self._tree_with_agent_idle(b"300")
        self._patch_tree_runner("    if ([string]::IsNullOrWhiteSpace($LeaseId)) { return $null }\n    $ar = Read-VenueLeaseBatchReceipt",
                                "    if ([string]::IsNullOrWhiteSpace($LeaseId)) { return $null }\n    $script:ieReads++; if ($script:ieReads -eq 1) { return $null }\n    $ar = Read-VenueLeaseBatchReceipt")
        # -RecheckSec 30 with 75 s of lease: the busy probe leaves under one recheck of room after the leg budget, so the batch ends room-short right there (no further probe)
        r = self.run_batch(self.specs[:1], "-ClaimPollSec", "20", "-RecheckSec", "30", FAKE_QUIET_MODES="IDLEBUSY,QUIET", FAKE_LEASE_REMAIN="75,2700")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsAttempted"]), ("LEASE_IDLE_ENDED", 0), b1)
        self.assertIn("as idle", b1["endDetail"])
        self.assertEqual((b2["endReason"], b2["legsRan"]), ("ALL_LEGS_DONE", 1), b2)
        # r4 (Grok G9; adversary r4 A2): this retype is the same end as the in-loop one, so it carries every field that one sets: leaseEndUtc clamped to the agent's endedUtc
        # (to the second) and the LEASE_IDLE_ENDED batch=1 line in batch.log (on 253ac155 it set only the reason and the detail)
        self._assert_idle_end_clamped_and_logged(b1)

    def test_lease_batch_idle_end_r4_grok_g9_lease_idle_ended_is_typed_in_one_helper(self) -> None:
        # Grok G9 prevention: a second site that sets an existing end reason by hand drifts from the first. The literal assignment appears once, inside Set-BatchLeaseIdleEnded,
        # and every site that finds the agent's idle release calls it (the probe's receipt read, the room-short batch end, a leg that ran off the lease: RETRY and RAN).
        text = RUNNER.read_text(encoding="utf-8")
        self.assertEqual(text.count("endReason = 'LEASE_IDLE_ENDED'"), 1)
        start = text.index("function Set-BatchLeaseIdleEnded")
        body = text[start:text.index("\n}\n", start)]
        self.assertIn("endReason = 'LEASE_IDLE_ENDED'", body)
        self.assertIn("ConvertTo-AgentUtc $Ie.endedUtc", body)
        self.assertIn('LogBest "LEASE_IDLE_ENDED batch=', body)
        self.assertEqual(text.count("Set-BatchLeaseIdleEnded $b "), 4, "the probe read, the room-short end, and the RETRY and RAN off-lease ends")

    # ---------------------------------------------------------------- r4 (Grok G6 on #817): the run's own in-lease waits vs the agent's idle bound, refused before any lease
    def test_lease_batch_idle_end_r4_grok_g6_a_cooldown_or_margin_past_the_idle_bound_is_refused_before_any_lease(self) -> None:
        # -CooldownSec sleeps inside the lease with no job and is not bounded by the idle allowance: at or above the bound less the margin (300 - 60 = 240 with the agent script's
        # default), the agent releases the lease under every cooldown. -ProbeIdleMarginSec at or above the bound leaves every probe no wait. Refused typed (exit 15) before
        # anything is submitted: no open, no batch receipt. On 253ac155 these ran (exit 0).
        for extra in (("-CooldownSec", "240"), ("-CooldownSec", "3600"), ("-ProbeIdleMarginSec", "300"), ("-ProbeIdleMarginSec", "1800")):
            with self.subTest(extra=extra):
                self.setUp()
                r = self.run_batch(self.specs[:2], *extra)
                self.assertEqual(r.returncode, 15, r.stdout + r.stderr)
                self.assertIn("BATCH_REFUSED IDLE_BOUND_CONFIG_REFUSED", r.stderr)
                self.assertIn(extra[0], r.stderr)
                self.assertEqual(self.kinds(), [], "nothing was submitted")
                self.assertFalse((self.run_dir / "batches.jsonl").exists())

    def test_lease_batch_idle_end_r4_grok_g6_a_config_just_under_the_idle_bound_runs(self) -> None:
        # control: one second under each refusal runs (exit 0)
        for extra in (("-CooldownSec", "239"), ("-ProbeIdleMarginSec", "299")):
            with self.subTest(extra=extra):
                self.setUp()
                r = self.run_batch(self.specs[:1], *extra)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.batches()[0]["endReason"], "ALL_LEGS_DONE")

    def test_lease_batch_idle_end_r4_grok_g6_the_running_agents_tighter_bound_is_checked_once_the_lease_is_in_hand(self) -> None:
        # the running agent publishes 100: -CooldownSec 40 (= 100 - 60) passes the pre-lease check against the script default (300) but not the bound the run uses. No probe or
        # leg runs on the lease, the lease is closed, the run stops IDLE_BOUND_CONFIG_REFUSED (exit 15). The same for -ProbeIdleMarginSec 100.
        for extra in (("-CooldownSec", "40"), ("-ProbeIdleMarginSec", "100")):
            with self.subTest(extra=extra):
                self.setUp()
                r = self.run_batch(self.specs[:2], *extra, FAKE_IDLE_SEC="100")
                self.assertEqual(r.returncode, 15, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["endReason"], b["legsAttempted"]), ("IDLE_BOUND_CONFIG_REFUSED", 0), b)
                self.assertIn("published by the running agent", b["endDetail"])
                self.assertEqual(self.kinds(), [f"OPEN:{b['token']}", f"CLOSE:{b['token']}"])
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["notRun"]), (15, "IDLE_BOUND_CONFIG_REFUSED", 2))

    # ---------------------------------------------------------------- r4 (Grok G7 on #817): a published idleSec that is there but invalid fails closed
    def test_lease_batch_idle_end_r4_grok_g7_an_invalid_published_idle_bound_fails_closed_not_to_the_script_default(self) -> None:
        # only an ABSENT field (an agent that predates it) falls back to the agent script's default; 0, a string, a double, a JSON null, a negative or an out-of-range value from
        # the RUNNING agent means its bound is unknown: AGENT_IDLE_BOUND_UNKNOWN (exit 10), nothing runs, the lease is closed. On 253ac155 each fell back to 300 and ran.
        for val in ("0", "str:100", "dbl:100.0", "null", "-5", "86401"):
            with self.subTest(idleSec=val):
                self.setUp()
                r = self.run_batch(self.specs[:2], FAKE_IDLE_SEC=val)
                self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["endReason"], b["legsAttempted"]), ("AGENT_IDLE_BOUND_UNKNOWN", 0), b)
                self.assertIn("published admission.leaseBatch.idleSec=", b["endDetail"])
                self.assertEqual(self.kinds(), [f"OPEN:{b['token']}", f"CLOSE:{b['token']}"])

    # ---------------------------------------------------------------- r5 (adversary r4 pass 3 A1): a leaseBatch that is not ONE object names no lease and no idle bound
    def test_lease_batch_idle_end_r5_adv_a1_an_array_wrapped_lease_batch_is_not_batch_capable(self) -> None:
        # adversary-493c4fff.json A1: admission.leaseBatch published as [{..}] (and [null, {..}], [[{..}]]) passed the capability check through member enumeration (.token and
        # .leaseId answered from inside the array) while Get-PublishedIdleBound read the array's own properties: idleSec ABSENT, the script default 300, and the legs ran although the
        # agent published idleSec 0. Now nothing runs (AGENT_NOT_BATCH_CAPABLE, exit 10), the close releases the token, and the lease it may hold is charged open to close, unnamed.
        real = str(RUNNER.parent.parent / "um-run.ps1")
        # r5 pass 3 (adversary r5 pass 2 A1): and the whole ADMISSION in a one-element array, which `$(...)` unrolled to the object inside before the reader saw it
        cases = [("array", {"FAKE_OPEN_LB_WRAP": "array"}), ("nullplus", {"FAKE_OPEN_LB_WRAP": "nullplus"}), ("nested", {"FAKE_OPEN_LB_WRAP": "nested"}),
                 ("array-real-umrun", {"FAKE_OPEN_LB_WRAP": "array", "FAKE_OPEN_VIA_REAL_UMRUN": "1", "FAKE_REAL_UMRUN": real}), ("admission-array", {"FAKE_OPEN_ADM_WRAP": "array"})]
        for wrap, extra in cases:
            with self.subTest(wrap=wrap):
                self.setUp()
                r = self.run_batch(self.specs[:2], FAKE_IDLE_SEC="0", **extra)
                self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["endReason"], b["legsAttempted"], b["leaseId"]), ("AGENT_NOT_BATCH_CAPABLE", 0, None), b)
                self.assertIn("not one admission.leaseBatch object", b["endDetail"])
                self.assertEqual(self.kinds(), [f"OPEN:{b['token']}", f"CLOSE:{b['token']}"])
                self.assertEqual((b["ledger"]["basis"], b["ledger"]["leaseId"]), ("open-lease-unnamed", None), b["ledger"])
                res = batch_result(r.stdout)
                self.assertEqual((res["stopReason"], res["notRun"]), ("AGENT_NOT_BATCH_CAPABLE", 2))

    def test_lease_batch_idle_end_r5_adv_p2_a1_a_lease_batch_with_no_usable_identity_is_charged_unnamed(self) -> None:
        # adversary-ff81d798.json A1: the open's result {"leaseBatch":{"token":..,"leaseId":1,..}} reaches the runner through the real um-run.ps1 reader, whose VenueRawJson clears
        # every identity of a reading that holds a non-string one. The leaseBatch is still ONE object, so the r5 MALFORMED charge did not fire: AGENT_NOT_BATCH_CAPABLE with no
        # ledger row for a lease the agent took. It is charged open-lease-unnamed now; only an ABSENT leaseBatch (the nobatch agent) charges nothing.
        real = str(RUNNER.parent.parent / "um-run.ps1")
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID_NUM="1", FAKE_OPEN_VIA_REAL_UMRUN="1", FAKE_REAL_UMRUN=real)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["leaseId"]), ("AGENT_NOT_BATCH_CAPABLE", 0, None), b)
        self.assertIn("names no usable lease for token", b["endDetail"])
        self.assertEqual(self.kinds(), [f"OPEN:{b['token']}", f"CLOSE:{b['token']}"])
        self.assertIsNotNone(b["ledger"], b)
        self.assertEqual((b["ledger"]["basis"], b["ledger"]["leaseId"]), ("open-lease-unnamed", None), b["ledger"])
        # control: an agent with no lease batches parks no lease, so nothing is charged
        self.setUp()
        r = self.run_batch(self.specs[:2], FAKE_OPEN_MODES="nobatch")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["ledger"]), ("AGENT_NOT_BATCH_CAPABLE", None), b)

    def test_lease_batch_idle_end_r5_adv_p2_a2_the_in_lease_cooldown_never_sleeps_past_the_idle_allowance(self) -> None:
        # adversary-ff81d798.json A2 sibling: the cooldown slept the full -CooldownSec after the leg child had already spent part of the allowance after its job ended. Agent
        # bound 70, margin 60 (10 s), -CooldownSec 9 (accepted: under 10), the child works 6 s after its job: the cooldown is cut to at most about 4 s, and the cut is logged.
        self._tree_with_agent_idle(b"70")
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", "-CooldownSec", "9", FAKE_LEG_TAIL_SLEEP="6")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        m = re.search(r"COOLDOWN 9 s \(inside the lease\) cut to (\d+) s by the agent's idle allowance", log)
        self.assertIsNotNone(m, [l for l in log.splitlines() if "COOLDOWN" in l])
        self.assertLessEqual(int(m.group(1)), 4, m.group(0))
        src = RUNNER.read_text(encoding="utf-8")
        self.assertNotIn('Start-Sleep -Seconds $CooldownSec', src)

    def test_lease_batch_idle_end_r5_adv_p3_a2_an_in_lease_probes_wait_ends_at_an_instant_fixed_at_its_dispatch(self) -> None:
        # adversary-6e3937c6.json A2: the runner passed the idle allowance as -MaxWaitSec, which Wait-VenueQuiet counts from its OWN start, after the process start, so the
        # queue-gate wait (no agent job) outlived the allowance by the startup. Every probe now also gets -WaitUntilUtc, the dispatch instant plus that wait on the runner's
        # clock, and Wait-VenueQuiet ends every wait (the gate's included) by it. Agent bound 70, margin 60 (10 s): the in-lease probe's end lies inside the allowance.
        self._tree_with_agent_idle(b"70")
        r = self.run_batch(self.specs[:1], "-ProbeMaxWaitSec", "600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = [l for l in self.events_raw() if " QUIET " in l]
        self.assertGreaterEqual(len(rows), 1, self.events_raw())
        for l in rows:
            ticks, until, wait = int(l.split(" ", 1)[0]), l.split(" until=", 1)[1].split(" ", 1)[0], int(l.rsplit("maxWait=", 1)[1])
            self.assertNotEqual(until, "-", f"no -WaitUntilUtc was passed: {l}")
            end = datetime.fromisoformat(until.replace("Z", "+00:00"))
            seen = datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
            self.assertLessEqual(wait, 10, l)
            self.assertLessEqual((end - seen).total_seconds(), wait + 0.5, f"the probe's end is past its dispatch plus its wait: {l}")
        # the probe script ends every wait, the queue gate's included, by -WaitUntilUtc
        wvq = (RUNNER.parent / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        for line in ("[Parameter(ParameterSetName = 'Live')][string]$WaitUntilUtc = '',", "    if ($wu -lt $deadline) { $deadline = $wu }", "    if ($wu -lt $gateEnd) { $gateEnd = $wu }"):
            self.assertEqual(wvq.count(line), 1, line)
        self.assertLess(wvq.index("$gateEnd = $t0.AddSeconds("), wvq.index("    if ($wu -lt $gateEnd) { $gateEnd = $wu }"))
        self.assertLess(wvq.index("    if ($wu -lt $gateEnd) { $gateEnd = $wu }"), wvq.index("function Wait-QueueGate"))

    # ---------------------------------------------------------------- r6 (adversary-848f6559.json A1/A2): the idle anchor after an agent job comes from that job's own result
    def test_lease_batch_idle_end_r6_adv_a1_a_leg_that_exits_nonzero_after_its_job_still_spends_its_post_job_work(self) -> None:
        # adversary-848f6559.json A1: a leg child that exits nonzero (the real Invoke-VenueLeg.ps1:314 exits 2 when its receipt write is denied) took the LEG_FAILED path, which skipped
        # the move of the anchor to the job's endedUtc, so the child's 6 s after its job were never spent. Agent bound 70, margin 60 (10 s): the next probe waits at most about 4 s.
        # Sibling: a child that dies 6 s in with NO result landed (exit3late) gives no evidence the agent's idle clock restarted, so the anchor stays where it was.
        for name, env in (("exit2-after-its-job", {"FAKE_LEG_TAIL_SLEEP": "6", "FAKE_LEG_EXIT": "2,0"}), ("exit3-no-result", {"FAKE_LEG_MODES": "exit3late,ok"})):
            with self.subTest(case=name):
                self.setUp()
                self._tree_with_agent_idle(b"70")
                r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", **env)
                self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual(b["legs"][0]["status"], "LEG_FAILED", b["legs"][0])
                waits = self._in_lease_waits()
                self.assertEqual(len(waits), 2, self.events_raw())
                self.assertLessEqual(waits[1], 4, f"the failed leg child's time after its job was not spent from the idle allowance: {waits}")

    def test_lease_batch_idle_end_r6_adv_a2_an_agent_clock_ahead_or_an_unreadable_end_falls_back_to_the_jobs_send(self) -> None:
        # adversary-848f6559.json A2: an agent clock AHEAD of this host's gives a negative `ago`, and Get-AgentJobEndTs kept the observation instant: the time the job had already
        # been over was never spent. With no usable endedUtc (ahead, or not an instant) the only safe bound is the job's send. Agent bound 70, margin 60 (10 s).
        #   open: um-run returns 6 s after the send, the agent's clock 30 s ahead: the first probe waits at most about 4 s (it waited about 10).
        #   leg (the :2574 sibling): the child works 6 s after its job, the agent's clock 30 s ahead, or endedUtc 'not-a-time': the next probe waits at most about 4 s.
        for name, env, which in (("open-ahead", {"FAKE_OPEN_SLEEP": "6", "FAKE_OPEN_ENDED_AHEAD_SEC": "30"}, 0),
                                 ("leg-ahead", {"FAKE_LEG_TAIL_SLEEP": "6", "FAKE_LEG_ENDED_AHEAD_SEC": "30"}, 1),
                                 ("leg-unreadable", {"FAKE_LEG_TAIL_SLEEP": "6", "FAKE_LEG_ENDED_RAW": "not-a-time"}, 1)):
            with self.subTest(case=name):
                self.setUp()
                self._tree_with_agent_idle(b"70")
                r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", **env)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                waits = self._in_lease_waits()
                self.assertEqual(len(waits), 2, self.events_raw())
                self.assertLessEqual(waits[which], 4, f"{name}: the time since the job's send was not spent from the idle allowance: {waits}")

    def test_lease_batch_idle_end_r6_adv_a2_get_agent_job_end_ts_never_keeps_the_observation_on_an_unusable_end(self) -> None:
        # the helper itself, with the adversary's input: endedUtc 5 s in this host's future (ago < 0), unreadable, absent: the floor (the send). A usable end in the past moves the
        # anchor earlier, never before the floor and never after the observation.
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'ConvertTo-AgentUtc', 'Get-AgentJobEndTs') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$f = [Diagnostics.Stopwatch]::Frequency; $now = [Diagnostics.Stopwatch]::GetTimestamp(); $floor = $now - 20 * $f; $obs = $now - 1 * $f\n"
            "$out = [ordered]@{}\n"
            "$out['ahead'] = (Get-AgentJobEndTs $obs $floor ([DateTime]::UtcNow.AddSeconds(5).ToString('o'))) -eq $floor\n"
            "$out['unreadable'] = (Get-AgentJobEndTs $obs $floor 'not-a-time') -eq $floor\n"
            "$out['absent'] = (Get-AgentJobEndTs $obs $floor $null) -eq $floor\n"
            "$p = Get-AgentJobEndTs $obs $floor ([DateTime]::UtcNow.AddSeconds(-6).ToString('o')); $out['past'] = ($p -ge $floor) -and ($p -le $now - 5 * $f)\n"
            "$out['beforeFloor'] = (Get-AgentJobEndTs $obs $floor ([DateTime]::UtcNow.AddSeconds(-60).ToString('o'))) -eq $floor\n"
            "$out | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"ahead": True, "unreadable": True, "absent": True, "past": True, "beforeFloor": True})

    def test_lease_batch_idle_end_r7_adv_a1_a_pause_between_the_two_clock_reads_never_places_the_end_late(self) -> None:
        # adversary-cc90eef2.json A1: Get-AgentJobEndTs read the wall clock (`ago`) and THEN the monotonic one (`nowTs`), so a scheduling pause between the two reads was added to
        # the anchor: the job's end landed that pause late and the idle allowance got that time back (a 6 s pause granted 3 s past an exhausted allowance). The helper is rebuilt
        # here with a 1.5 s sleep after EVERY statement of its body (whatever order the reads are in, a pause sits between them), the job ended 2 s before the call. The anchor
        # may land earlier than the true end (spent, conservative), never more than 0.3 s later. Siblings: every Get-AgentJobEndTs caller (:2324 open, :2559 leg) and every
        # Get-IdleAllowanceSec reader (:1517 probe wait, :2469 thermal sleep, :2638 cooldown) read the anchor this helper places; no other wall-to-monotonic conversion exists.
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "$fa = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'ConvertTo-AgentUtc' }, $true); . ([scriptblock]::Create($fa.Extent.Text))\n"
            "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Get-AgentJobEndTs' }, $true)\n"
            "$stmts = @($fn.Body.EndBlock.Statements | ForEach-Object { $_.Extent.Text })\n"
            "$text = 'function Get-AgentJobEndTs(' + (@($fn.Parameters | ForEach-Object { $_.Extent.Text }) -join ', ') + \") {`n\" + ($stmts -join \"`nStart-Sleep -Milliseconds 1500`n\") + \"`n}\"\n"
            ". ([scriptblock]::Create($text))\n"
            "$f = [Diagnostics.Stopwatch]::Frequency; $t0Ts = [Diagnostics.Stopwatch]::GetTimestamp(); $t0 = [DateTime]::UtcNow\n"
            "$trueEnd = $t0Ts - 2 * $f; $floor = $t0Ts - 100 * $f\n"
            "$a = Get-AgentJobEndTs $t0Ts $floor ($t0.AddSeconds(-2).ToString('o'))\n"
            "[ordered]@{ statements = $stmts.Count; lateSec = [Math]::Round(($a - $trueEnd) / [double]$f, 3); atOrAboveFloor = ($a -ge $floor) } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        # r10 (sol r9b 9a56fa7a-A1): the helper reads no clock at all now (the send, floored at the observation), so its body may be one statement; the pause bound still holds
        self.assertGreaterEqual(got["statements"], 1, got)
        self.assertTrue(got["atOrAboveFloor"], got)
        self.assertLessEqual(got["lateSec"], 0.3, f"a pause between the clock reads placed the agent job's end late (time handed back to the idle allowance): {got}")

    def test_lease_batch_idle_end_r7_adv_a2_a_receipt_or_ledger_line_that_is_not_one_json_object_is_never_read(self) -> None:
        # adversary-065db820.json A2: a lease-batch receipt line [null,{token,leaseId,endReason:idle}] reached Read-VenueLeaseBatchReceipt as an array, and member enumeration
        # answered .token/.leaseId/.endReason from the object inside it: FOUND, then Get-LeaseIdleEnd typed LEASE_IDLE_ENDED (repeated: LEASE_IDLE_CHURN, exit 14). ConvertFrom-Json
        # also unrolls [{..}] and [[{..}]] to the object inside, so only a line whose text is ONE JSON object ({ first) is a record. Sibling (VenueChain.psm1:1155): the lease
        # ledger reader, same rule. The plain object line still answers FOUND and the plain ledger line is still read.
        case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        (case / "share" / "logs").mkdir(parents=True)
        tok = "0123456789ab"
        rec = '{"token":"0123456789ab","leaseId":"L1","endReason":"idle","endedUtc":"2026-11-14T18:00:00Z","heldSec":301}'
        led = '{"venue":"bachelor","token":"0123456789ab","leaseId":"L1","startUtc":"2026-11-14T17:00:00Z","endUtc":"2026-11-14T17:30:00Z"}'
        script = (
            f"Import-Module '{psq(DV / 'VenueChain.psm1')}' -Force -DisableNameChecking\n"
            f"$share = '{psq(case / 'share')}'; $log = Join-Path $share 'logs\\lease-batch-{tok}.jsonl'; $cd = '{psq(case / 'claims')}'\n"
            "New-Item -ItemType Directory -Force -Path $cd | Out-Null\n"
            f"$rec = '{psq(rec)}'; $led = '{psq(led)}'\n"
            "$out = [ordered]@{}\n"
            "foreach ($k in 'nullThenObject', 'oneElement', 'nested', 'plain') {\n"
            "    $line = switch ($k) { 'nullThenObject' { \"[null,$rec]\" } 'oneElement' { \"[$rec]\" } 'nested' { \"[[$rec]]\" } 'plain' { $rec } }\n"
            "    [IO.File]::WriteAllText($log, $line + \"`n\")\n"
            f"    $out[\"receipt_$k\"] = (Read-VenueLeaseBatchReceipt -AgentShare $share -Token '{tok}' -LeaseId 'L1').state\n"
            "    $lp = Get-VenueLeaseLedgerPath -Venue 'bachelor' -ClaimDir $cd; New-Item -ItemType Directory -Force -Path (Split-Path -Parent $lp) | Out-Null\n"
            "    $lline = switch ($k) { 'nullThenObject' { \"[null,$led]\" } 'oneElement' { \"[$led]\" } 'nested' { \"[[$led]]\" } 'plain' { $led } }\n"
            "    [IO.File]::WriteAllText($lp, $lline + \"`n\")\n"
            "    $out[\"ledger_$k\"] = @(Read-VenueLeaseLedger -Venue 'bachelor' -ClaimDir $cd).Count\n"
            "}\n"
            "$out | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {
            "receipt_nullThenObject": "ABSENT", "ledger_nullThenObject": 0,
            "receipt_oneElement": "ABSENT", "ledger_oneElement": 0,
            "receipt_nested": "ABSENT", "ledger_nested": 0,
            "receipt_plain": "FOUND", "ledger_plain": 1,
        })

    # ---------------------------------------------------------------- r8 (adversary-f52a32d6.json A1): an in-lease probe's wait end is counted from an instant read BEFORE its cuts
    def test_lease_batch_idle_end_r8_adv_a1_a_stall_before_the_probes_dispatch_never_places_its_wait_end_late(self) -> None:
        # adversary-f52a32d6.json A1: Invoke-QuietProbe cut the wait at Get-ProbeLaunchVerdict (:1533) and fixed its end at -WaitUntilUtc (:1550) from UtcNow read LATER, so a
        # stall in between (the thermal admission, scheduling) was added on top of the idle allowance: idle 300, margin 60, a 90 s pause gave an end 30 s past the agent's bound.
        # Siblings: the lease room cut (:1517, read at the same call) and the thermal cap cut (:1540) are the same stale duration. The function is rebuilt here with a 1 s sleep
        # after EVERY top-level statement before the probe starts, so a pause sits between any read and any use; the wait end may land early (spent), never more than 0.3 s
        # past the idle allowance, the lease room or the thermal cap, and -MaxWaitSec is never above the duration the end was cut from.
        case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            f"Import-Module '{psq(DV / 'VenueThermalGate.psm1')}' -Force -DisableNameChecking\n"
            "foreach ($n in 'Get-ProbeLaunchVerdict', 'Get-IdleAllowanceSec', 'Get-ThermalElapsedSec') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Invoke-QuietProbe' }, $true)\n"
            "$stmts = @($fn.Body.EndBlock.Statements | ForEach-Object { $_.Extent.Text })\n"
            "$cut = 0; while ($cut -lt $stmts.Count -and -not $stmts[$cut].StartsWith('$captured')) { $cut++ }\n"
            "$body = @(for ($i = 0; $i -lt $stmts.Count; $i++) { $stmts[$i]; if ($i -lt $cut) { 'Start-Sleep -Milliseconds 1000' } }) -join \"`n\"\n"
            ". ([scriptblock]::Create('function Invoke-QuietProbe(' + (@($fn.Parameters | ForEach-Object { $_.Extent.Text }) -join ', ') + \") {`n\" + $body + \"`n}\"))\n"
            "function Test-RunDirWrite { $true }; function Get-VenueClaimKeeperLoss { $null }; function L { }\n"
            "function ConvertFrom-VenueProbeHeatLine { [pscustomobject]@{ heat = 'unknown'; text = ''; thresholdsSource = $null; reason = $null } }\n"
            "function Invoke-FakeProbe { $global:seenArgs = @($args); $global:LASTEXITCODE = 3 }\n"
            f"$RunDir = '{psq(case)}'; $QuietScript = '{psq(RUNNER)}'; $Venue = 'bachelor'; $ThresholdPercent = 15; $RecheckSec = 30; $pwshExe = 'Invoke-FakeProbe'\n"
            "$claimKeeper = $null; $claim = [pscustomobject]@{ expiresUtc = [DateTime]::UtcNow.AddHours(1) }; $ThermalDispatchGraceSec = 1; $ThermalPollSec = 30\n"
            "$f = [Diagnostics.Stopwatch]::Frequency\n"
            "$out = [ordered]@{}\n"
            "foreach ($k in 'idle', 'room', 'thermal') {\n"
            "    $t0Ts = [Diagnostics.Stopwatch]::GetTimestamp(); $t0 = [DateTime]::UtcNow\n"
            "    $idleAnchorTs = $t0Ts; $probeIdleCap = $(if ($k -eq 'idle') { 30 } else { 600 }); $LeaseMarginSec = 60; $legBudget = 600\n"
            "    $leaseEnd = $t0.AddSeconds($(if ($k -eq 'room') { 60 + 600 + 30 } else { 7200 }))\n"
            "    $ThermalWaitCapSec = 30; $th = $(if ($k -eq 'thermal') { @{ firstHotUtc = $t0; firstHotTs = $t0Ts; slowProbeSec = 0 } } else { $null })\n"
            "    $global:seenArgs = @()\n"
            "    $q = Invoke-QuietProbe 'leg' \"r8-$k\" 600 $th\n"
            "    $a = @($global:seenArgs); $iu = [Array]::IndexOf($a, '-WaitUntilUtc'); $im = [Array]::IndexOf($a, '-MaxWaitSec')\n"
            "    if ($iu -lt 0) { $out[$k] = \"no -WaitUntilUtc ($($q | ConvertTo-Json -Compress))\"; continue }\n"
            "    $until = [DateTime]::Parse($a[$iu + 1], [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()\n"
            "    $out[$k] = [ordered]@{ statements = $cut; lateSec = [Math]::Round(($until - $t0.AddSeconds(30)).TotalSeconds, 3); maxWaitSec = [int]$a[$im + 1]; waitSec = $q.maxWaitSec }\n"
            "}\n"
            "$out | ConvertTo-Json -Compress -Depth 4\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=240)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        for k in ("idle", "room", "thermal"):
            with self.subTest(bound=k):
                g = got[k]
                self.assertIsInstance(g, dict, g)
                self.assertGreaterEqual(g["statements"], 8, g)
                self.assertLessEqual(g["lateSec"], 0.3, f"{k}: a stall before the dispatch placed the probe's wait end past the {k} bound: {g}")
                self.assertEqual(g["maxWaitSec"], g["waitSec"], g)
                self.assertLessEqual(g["maxWaitSec"], 30, g)

    # ---------------------------------------------------------------- r9 (Grok G14 on #817): the probe's wait end is counted on the MONOTONIC clock from its anchor
    def test_lease_batch_idle_end_r9_grok_g14_a_wall_clock_step_back_before_the_probes_dispatch_never_places_its_wait_end_late(self) -> None:
        # Grok G14: r8 counted -WaitUntilUtc from $dispatchUtc = [DateTime]::UtcNow, read before the cuts (which can block on a share), so this host's clock stepping BACK S between
        # that read and the dispatch put the end S later in the child's frame (Wait-VenueQuiet compares it with its own wall clock): a 60 s step ran the wait 60 s past the idle
        # allowance. The function is rebuilt with every [DateTime]::UtcNow read in it going through a fake wall clock that steps back 60 s after ONE top-level statement; each
        # statement before the one that places -WaitUntilUtc is tried in turn, wherever the anchor sits. In the stepped frame the idle bound is t0 + 30 - 60: the end may land
        # early (spent), never more than 0.3 s past it. Only the child's own startup, after the dispatch, stays exposed (disclosed). LEASE_BATCH_RED_REF=2854b231: RED, ~60 s late.
        case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        script = (
            ps_parse_source(RUNNER) + "\n"
            f"Import-Module '{psq(DV / 'VenueThermalGate.psm1')}' -Force -DisableNameChecking\n"
            "foreach ($n in 'Get-ProbeLaunchVerdict', 'Get-IdleAllowanceSec', 'Get-ThermalElapsedSec') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Invoke-QuietProbe' }, $true)\n"
            "$stmts = @($fn.Body.EndBlock.Statements | ForEach-Object { $_.Extent.Text.Replace('[DateTime]::UtcNow', '(Get-FakeWallUtc)') })\n"
            "$params = @($fn.Parameters | ForEach-Object { $_.Extent.Text }) -join ', '\n"
            "$cut = 0; while ($cut -lt $stmts.Count -and -not $stmts[$cut].Contains(\"'-WaitUntilUtc'\")) { $cut++ }\n"
            "function Get-FakeWallUtc { [DateTime]::UtcNow.AddSeconds($global:wallStepSec) }\n"
            "function Test-RunDirWrite { $true }; function Get-VenueClaimKeeperLoss { $null }; function L { }\n"
            "function ConvertFrom-VenueProbeHeatLine { [pscustomobject]@{ heat = 'unknown'; text = ''; thresholdsSource = $null; reason = $null } }\n"
            "function Invoke-FakeProbe { $global:seenArgs = @($args); $global:LASTEXITCODE = 3 }\n"
            f"$RunDir = '{psq(case)}'; $QuietScript = '{psq(RUNNER)}'; $Venue = 'bachelor'; $ThresholdPercent = 15; $RecheckSec = 30; $pwshExe = 'Invoke-FakeProbe'\n"
            "$claimKeeper = $null; $claim = [pscustomobject]@{ expiresUtc = [DateTime]::UtcNow.AddHours(1) }; $ThermalDispatchGraceSec = 1; $ThermalPollSec = 30; $ThermalWaitCapSec = 30\n"
            "$f = [Diagnostics.Stopwatch]::Frequency\n"
            "$runs = @(for ($k = 0; $k -lt $cut; $k++) {\n"
            "    $body = @(for ($i = 0; $i -lt $stmts.Count; $i++) { $stmts[$i]; if ($i -eq $k) { '$global:wallStepSec = -60' } }) -join \"`n\"\n"
            "    . ([scriptblock]::Create('function Invoke-QuietProbe(' + $params + \") {`n\" + $body + \"`n}\"))\n"
            "    $global:wallStepSec = 0; $global:seenArgs = @()\n"
            "    $t0Ts = [Diagnostics.Stopwatch]::GetTimestamp(); $t0 = [DateTime]::UtcNow\n"
            "    $idleAnchorTs = $t0Ts; $probeIdleCap = 30; $LeaseMarginSec = 60; $legBudget = 600; $leaseEnd = $t0.AddSeconds(7200)\n"
            "    $q = Invoke-QuietProbe 'leg' \"r9-g14-$k\" 600 $null\n"
            "    $a = @($global:seenArgs); $iu = [Array]::IndexOf($a, '-WaitUntilUtc'); $im = [Array]::IndexOf($a, '-MaxWaitSec')\n"
            "    if ($iu -lt 0) { \"after statement ${k}: no -WaitUntilUtc ($($q | ConvertTo-Json -Compress))\"; continue }\n"
            "    $until = [DateTime]::Parse($a[$iu + 1], [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()\n"
            "    $it = [Array]::IndexOf($a, '-WaitUntilTs'); $tsLate = $(if ($it -ge 0) { [Math]::Round(([long]$a[$it + 1] - ($t0Ts + 30 * $f)) / [double]$f, 3) } else { $null })\n"
            "    [ordered]@{ after = $k; lateSec = [Math]::Round(($until - $t0.AddSeconds(30 - 60)).TotalSeconds, 3); tsLateSec = $tsLate; maxWaitSec = [int]$a[$im + 1]; waitSec = $q.maxWaitSec }\n"
            "})\n"
            "[ordered]@{ cut = $cut; runs = $runs } | ConvertTo-Json -Compress -Depth 4\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=240)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertGreaterEqual(got["cut"], 8, got)
        self.assertEqual(len(got["runs"]), got["cut"], got)
        for g in got["runs"]:
            with self.subTest(step_after_statement=g["after"] if isinstance(g, dict) else g):
                self.assertIsInstance(g, dict, g)
                self.assertLessEqual(g["lateSec"], 0.3, f"a 60 s wall-clock step back after statement {g['after']} placed the probe's wait end past the idle bound: {g}")
                self.assertGreater(g["lateSec"], -5.0, f"the wait end was placed far earlier than the bound (the spent time counted twice?): {g}")
                # r9b (adversary-74e14dda.json A1): the same end on the monotonic counter, which no wall step moves
                self.assertIsNotNone(g["tsLateSec"], f"no -WaitUntilTs was passed: {g}")
                self.assertLessEqual(g["tsLateSec"], 0.3, g)
                self.assertGreater(g["tsLateSec"], -5.0, g)
                self.assertEqual(g["maxWaitSec"], g["waitSec"], g)
                self.assertLessEqual(g["maxWaitSec"], 30, g)

    # ---------------------------------------------------------------- r8 (Grok G12 on #817): an agent clock ahead charges the send plus the job's own runtime, not the bare send
    def test_lease_batch_idle_end_r8_grok_g12_get_agent_job_end_ts_ahead_floor_is_the_send_plus_the_jobs_runtime(self) -> None:
        # Get-AgentJobEndTs returned the bare send whenever the agent's endedUtc lay in this host's future, so one second of skew past the job-to-observation gap charged the whole
        # job to the idle allowance. endedUtc - startedUtc is a duration on the agent's one clock (the skew cancels) and the job cannot end before send + that: the floor is that,
        # never past the observation. startedUtc absent, JSON null (the queue-expired result), unreadable, or after endedUtc: the send alone. A clock behind is unchanged.
        # r10 (sol r9b 9a56fa7a-A1) SUPERSEDES the send-plus-runtime floor: endedUtc - startedUtc is a difference on the agent's wall clock, which a step during the job inflates
        # (a +90 s correction in a 100 s job read 190 s). Every input below now gives the send (the helper no longer takes the agent's stamps; extra arguments are ignored).
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'ConvertTo-AgentUtc', 'Get-AgentJobEndTs') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$f = [Diagnostics.Stopwatch]::Frequency; $now = [Diagnostics.Stopwatch]::GetTimestamp(); $floor = $now - 20 * $f; $obs = $now - 1 * $f\n"
            "$e = [DateTime]::UtcNow.AddSeconds(5); $es = $e.ToString('o')\n"
            "$out = [ordered]@{}\n"
            "$out['aheadRuntimeIsTheSend'] = (Get-AgentJobEndTs $obs $floor $es ($e.AddSeconds(-6).ToString('o'))) -eq $floor\n"
            "$out['aheadLongRuntimeIsTheSend'] = (Get-AgentJobEndTs $obs $floor $es ($e.AddSeconds(-60).ToString('o'))) -eq $floor\n"
            "$out['aheadStartedNull'] = (Get-AgentJobEndTs $obs $floor $es $null) -eq $floor\n"
            "$out['aheadStartedAbsent'] = (Get-AgentJobEndTs $obs $floor $es) -eq $floor\n"
            "$out['aheadStartedUnreadable'] = (Get-AgentJobEndTs $obs $floor $es 'not-a-time') -eq $floor\n"
            "$out['aheadStartedAfterEnd'] = (Get-AgentJobEndTs $obs $floor $es ($e.AddSeconds(3).ToString('o'))) -eq $floor\n"
            "$out['aheadStartedArray'] = (Get-AgentJobEndTs $obs $floor $es @($e.AddSeconds(-6).ToString('o'))) -eq $floor\n"
            "$p = Get-AgentJobEndTs $obs $floor ([DateTime]::UtcNow.AddSeconds(-6).ToString('o')) ([DateTime]::UtcNow.AddSeconds(-8).ToString('o')); $out['pastUnchanged'] = ($p -ge $floor) -and ($p -le $now - 5 * $f)\n"
            "$out | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"aheadRuntimeIsTheSend": True, "aheadLongRuntimeIsTheSend": True, "aheadStartedNull": True, "aheadStartedAbsent": True,
                                                  "aheadStartedUnreadable": True, "aheadStartedAfterEnd": True, "aheadStartedArray": True, "pastUnchanged": True})
        # r10: neither caller passes an agent stamp any more (the r10 grid test pins both call lines); none unrolls one either
        src = RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("$openStartedUtc", src)
        self.assertNotIn("$legStartedUtc", src)
        self.assertNotIn("{ $op.result.startedUtc }", src)
        self.assertNotIn("{ $jr.result.startedUtc }", src)

    # ---------------------------------------------------------------- r10 (sol r9b 9a56fa7a-A1): an agent-clock step during a job never returns spent idle time
    def test_lease_batch_idle_end_r10_sol_9a56fa7a_a1_an_agent_clock_step_during_a_job_never_extends_the_next_idle_allowance(self) -> None:
        # The agent times its idle release on ITS wall clock and starts that clock AFTER the job ends (ultra-magnus-agent.ps1: `lastActivity = Get-Date` in the inbox loop's
        # finally after Invoke-QueuedJob and at the park in Exit-HeavySlot, compared by `(Get-Date) - $b.lastActivity` in Update-LeaseBatchHold), so a step of that clock
        # DURING the job does not move the release: it is the job's real end plus idleSec. The runner placed the end from endedUtc (and, ahead, endedUtc - startedUtc), both
        # agent-wall-clock readings, so a forward step put the anchor late and handed spent time back. sol's input: send 12:00:00Z, agent +30 s (startedUtc 12:00:30Z), a real
        # 100 s job with a +90 s correction (endedUtc 12:03:40Z), observed at send+180, idleSec 300, margin 60, ProbeMaxWaitSec 600: the next wait ended at send+420, past the
        # release at send+400. For EVERY offset/step/queue on the grid, the next wait must end no later than the release less the margin, and the anchor no later than the true end.
        agent = AGENT.read_text(encoding="utf-8")
        self.assertIn("Exit-HeavySlot\n                if ($null -ne $script:LeaseBatch) { $script:LeaseBatch.lastActivity = Get-Date }", agent.replace("\r\n", "\n"))
        self.assertIn("$idle = ((Get-Date) - $b.lastActivity).TotalSeconds", agent)
        script = (
            f"{ps_parse_source(RUNNER)}\n"
            "foreach ($n in 'ConvertTo-AgentUtc', 'Get-AgentJobEndTs', 'Get-IdleAllowanceSec') { $d = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true); if ($d) { . ([scriptblock]::Create($d.Extent.Text)) } }\n"
            "$f = [double][Diagnostics.Stopwatch]::Frequency; $idleSec = 300; $margin = 60; $probeIdleCap = $idleSec - $margin; $obsSec = 180\n"
            "$bad = [System.Collections.Generic.List[string]]::new(); $n = 0; $sol = $null\n"
            "foreach ($offset in @(-60, 0, 30, 120)) { foreach ($step in @(-120, -30, 0, 30, 50, 79, 90, 150)) { foreach ($queue in @(0, 40)) {\n"
            "  $nowTs = [Diagnostics.Stopwatch]::GetTimestamp(); $nowUtc = [DateTime]::UtcNow\n"
            "  $sendTs = $nowTs - [long]($obsSec * $f); $sendUtc = $nowUtc.AddSeconds(-$obsSec); $realEnd = $queue + 100\n"
            "  $startedUtc = $sendUtc.AddSeconds($queue + $offset).ToString('o'); $endedUtc = $sendUtc.AddSeconds($realEnd + $offset + $step).ToString('o')\n"
            "  $idleAnchorTs = Get-AgentJobEndTs $nowTs $sendTs $endedUtc $startedUtc\n"
            "  $waitEnd = $obsSec + [Math]::Min(600, (Get-IdleAllowanceSec)); $lateSec = ($idleAnchorTs - ($sendTs + [long]($realEnd * $f))) / $f; $n++\n"
            "  if ($offset -eq 30 -and $step -eq 90 -and $queue -eq 0) { $sol = [ordered]@{ waitEndSec = $waitEnd; releaseSec = $realEnd + $idleSec; anchorLateSec = [Math]::Round($lateSec, 2) } }\n"
            "  if ($waitEnd -gt $realEnd + $idleSec - $margin -or $lateSec -gt 0.5) { $bad.Add(\"offset=$offset step=$step queue=$queue waitEnd=$waitEnd release=$($realEnd + $idleSec) late=$([Math]::Round($lateSec, 1))\") }\n"
            "} } }\n"
            "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Get-AgentJobEndTs' }, $true)\n"
            "[ordered]@{ cases = $n; bad = @($bad); sol = $sol; readsAgentOrWallClock = [bool](((@($fn.Parameters | ForEach-Object { $_.Extent.Text }) + @($fn.Body.EndBlock.Statements | ForEach-Object { $_.Extent.Text })) -join \"`n\") -match 'UtcNow|Get-Date|ConvertTo-AgentUtc|EndedUtc|StartedUtc') } | ConvertTo-Json -Compress -Depth 4\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["cases"], 64, got)
        self.assertLessEqual(got["sol"]["waitEndSec"], got["sol"]["releaseSec"] - 60, f"sol's input: the next wait runs past the agent's idle release less the margin: {got['sol']}")
        self.assertEqual(got["bad"], [], "an agent-clock step the runner cannot see moved the idle anchor past the job's true end")
        self.assertFalse(got["readsAgentOrWallClock"], "the job-end anchor reads an agent wall-clock stamp (or this host's wall clock): a step of either moves it")
        # both callers hand the anchor only monotonic instants: the open (its result's return, floored at its submit) and a leg (its child's return, floored at its launch)
        src = RUNNER.read_text(encoding="utf-8")
        self.assertIn("$idleAnchorTs = Get-AgentJobEndTs $(if ($null -ne $openResultTs) { $openResultTs } else { $openReadTs }) $openSubmitTs\n", src.replace("\r\n", "\n"))
        self.assertIn("{ $idleAnchorTs = Get-AgentJobEndTs $legReturnTs $tLegTs }", src)
        self.assertNotRegex(src, r"Get-AgentJobEndTs [^\n]*(?:endedUtc|startedUtc)")

    def test_lease_batch_idle_end_r10_sol_9a56fa7a_a1_a_forward_agent_step_under_the_observation_gap_is_spent_end_to_end(self) -> None:
        # the ago >= 0 branch of the same class, run end to end: agent bound 70, margin 60 (10 s). The leg child works 6 s after its job and the agent's endedUtc reads 3 s late
        # (a forward step smaller than the job-to-observation gap, so `ago` stays positive): placed from endedUtc the next probe waited about 7 s; from the leg's launch at most 4.
        # Control: the same leg with no step. The open sibling is the same helper (the grid test above pins both call sites); the fake open's job ends at its return.
        for name, env, which in (("leg-step", {"FAKE_LEG_TAIL_SLEEP": "6", "FAKE_LEG_ENDED_AHEAD_SEC": "3"}, 1),
                                 ("leg-no-step", {"FAKE_LEG_TAIL_SLEEP": "6"}, 1)):
            with self.subTest(case=name):
                self.setUp()
                self._tree_with_agent_idle(b"70")
                r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", **env)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                waits = self._in_lease_waits()
                self.assertEqual(len(waits), 2, self.events_raw())
                self.assertLessEqual(waits[which], 4, f"{name}: a forward agent-clock step handed spent time back to the idle allowance: {waits}")

    def test_lease_batch_idle_end_r8_adv_5fc3bf72_a2_wait_venue_quiet_times_every_deadline_on_the_monotonic_clock(self) -> None:
        # adversary-5fc3bf72.json A2: Wait-VenueQuiet compared [DateTime]::UtcNow with its deadline and gate end, so this host's clock stepping BACK 120 s mid-wait stretched a
        # 240 s gate wait to about 360 s (past the agent's idle bound) while waitedSec said 240. Every deadline check and waitedSec now reads Get-WaitNowUtc: $t0 plus the
        # Stopwatch time since it. Run with a $t0 far from the wall clock (a step of years), it still answers $t0 + the monotonic elapsed time.
        wvq_path = RUNNER.parent / "Wait-VenueQuiet.ps1"
        wvq = wvq_path.read_text(encoding="utf-8")
        self.assertEqual(wvq_wall_clock_timing_lines(wvq), [], "a deadline check or waitedSec still reads the wall clock")
        # r13 (hosted CI, run 43789411276): the lint above must go RED on a body with a wall-clock deadline left in it, whichever spelling it uses.
        for what, old, new in (
            ("the caller's cutoff gate on Get-Date", "(Get-WaitNowUtc) -ge $waitCutoff", "(Get-Date).ToUniversalTime() -ge $waitCutoff"),
            ("the gate poll's $now on Get-Date", "        $now = Get-WaitNowUtc\n", "        $now = (Get-Date).ToUniversalTime()\n"),
            ("the re-probe deadline on UtcNow", "if ((Get-WaitNowUtc).AddSeconds($RecheckSec) -gt $deadline) { break }",
             "if ([DateTime]::UtcNow.AddSeconds($RecheckSec) -gt $deadline) { break }"),
            ("the read-retry deadline on Get-Date", "if ((Get-WaitNowUtc).AddSeconds($delay) -gt $deadline) { return }",
             "if ((Get-Date).AddSeconds($delay) -gt $deadline) { return }"),
            ("a waitedSec report on UtcNow", "waitedSec=$([int]((Get-WaitNowUtc) - $t0).TotalSeconds) probes=", "waitedSec=$([int]([DateTime]::UtcNow - $t0).TotalSeconds) probes="),
        ):
            norm = wvq.replace("\r\n", "\n")
            self.assertIn(old, norm, what)
            self.assertNotEqual(wvq_wall_clock_timing_lines(norm.replace(old, new, 1)), [], f"the lint missed: {what}")
        for frag in ("if ((Get-WaitNowUtc).AddSeconds($delay) -gt $deadline) { return }", "        $now = Get-WaitNowUtc\n",
                     "if ((Get-WaitNowUtc).AddSeconds($RecheckSec) -gt $deadline) { break }",
                     "(Get-WaitNowUtc) -ge $waitCutoff }"):
            self.assertEqual(wvq.replace("\r\n", "\n").count(frag), 1, frag)
        # r13: the monotonic `waitedSec=` form occurs once per REPORT, not once per deadline. Pin WHAT each occurrence guards (a count alone went stale when r11/r12 added the three
        # GATE_CUTOFF phases): the three end-of-wait reports, then the three cutoff refusals by phase. A new report line must be added here on purpose.
        mono = [ln for ln in wvq.replace("\r\n", "\n").splitlines() if not ln.strip().startswith("#") and "(Get-WaitNowUtc) - $t0).TotalSeconds" in ln]
        tags = []
        for ln in mono:
            m = re.search(r'Write-Gate "(\$verdict|[A-Z_]+)', ln)
            self.assertIsNotNone(m, ln)
            tag = m.group(1)
            if tag == "GATE_CUTOFF":
                tag += ":" + re.search(r"phase=([a-z-]+)", ln).group(1)
            tags.append(tag)
        self.assertEqual(sorted(tags), sorted([
            "GATE_CUTOFF:before-write", "GATE_CUTOFF:before-submit", "GATE_CUTOFF:after-probe",
            "VENUE_QUIET_UNKNOWN", "FOREIGN_FLOOR", "$verdict",
        ]), mono)
        # every other waitedSec report (GATE_BUSY) subtracts $t0 from $now, which is itself Get-WaitNowUtc (asserted above): no third spelling
        for ln in wvq.replace("\r\n", "\n").splitlines():
            if not ln.strip().startswith("#") and "waitedSec=$(" in ln and ln not in mono:
                self.assertIn("waitedSec=$([int]($now - $t0).TotalSeconds)", ln)
        # r9 (adversary-2854b231.json A1): the MONOTONIC anchor is read first and the wall clock after it (a pause between them puts Get-WaitNowUtc ahead, never behind)
        self.assertLess(wvq.index("$t0Ts = [Diagnostics.Stopwatch]::GetTimestamp()"), wvq.index("$t0 = [DateTime]::UtcNow"))
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(wvq_path)}', [ref]$null, [ref]$null)\n"
            ". ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Get-WaitNowUtc' }, $true).Extent.Text))\n"
            "$f = [Diagnostics.Stopwatch]::Frequency; $t0 = [DateTime]::new(2000, 1, 1, 0, 0, 0, [DateTimeKind]::Utc); $t0Ts = [Diagnostics.Stopwatch]::GetTimestamp() - 5 * $f\n"
            "[Math]::Round(((Get-WaitNowUtc) - $t0).TotalSeconds, 1)\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertLess(abs(float(out.stdout.strip()) - 5.0), 0.5, out.stdout)

    def test_lease_batch_idle_end_r9_adv_2854b231_a1_a_pause_between_wait_venue_quiets_two_clock_reads_never_puts_its_clock_behind(self) -> None:
        # adversary-2854b231.json A1 (r8 OPEN, introduced by 2854b231): Wait-VenueQuiet read $t0 (wall) BEFORE $t0Ts (Stopwatch), so a pause P between the two put Get-WaitNowUtc
        # P BEHIND real time and every deadline it checks (the caller's -WaitUntilUtc included) ended P late. The two top-level anchor statements are taken from the script in the
        # order it has them, with a 1.5 s sleep between them: Get-WaitNowUtc may run ahead of the wall clock (the wait ends sooner), never more than 0.3 s behind it.
        # LEASE_BATCH_RED_REF=2854b231: RED, about 1.5 s behind.
        wvq_path = DV / "Wait-VenueQuiet.ps1"
        script = (
            ps_parse_source(wvq_path) + "\n"
            "$top = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and @('$t0', '$t0Ts') -ccontains $_.Left.Extent.Text } | ForEach-Object { $_.Extent.Text })\n"
            "if ($top.Count -ne 2) { throw \"expected the two top-level anchor statements, found $($top.Count)\" }\n"
            ". ([scriptblock]::Create($top[0] + \"`nStart-Sleep -Milliseconds 1500`n\" + $top[1]))\n"
            ". ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Get-WaitNowUtc' }, $true).Extent.Text))\n"
            "[ordered]@{ order = @($top | ForEach-Object { ($_ -split '=')[0].Trim() }); aheadSec = [Math]::Round(((Get-WaitNowUtc) - [DateTime]::UtcNow).TotalSeconds, 3) } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertGreaterEqual(got["aheadSec"], -0.3, f"a pause between Wait-VenueQuiet's two clock reads put Get-WaitNowUtc behind real time (every deadline ends that late): {got}")

    def test_lease_batch_idle_end_r9_adv_74e14dda_a1_a_suspend_and_a_clock_step_before_the_childs_start_never_move_its_wait_end(self) -> None:
        # adversary-74e14dda.json A1: the runner placed -WaitUntilUtc (a WALL instant) and -MaxWaitSec 239 (counted from the child's own start), then was suspended 90 s while
        # this host's clock stepped back 120 s: the child started with its wall clock 30 s behind the caller's reading, so its deadline was min(t0 + 239, wall end) = t0 + 239,
        # 90 + 239 = 330 s after the caller's end was placed (past an idle bound of 300). -WaitUntilTs carries the end on the monotonic counter the child shares with the runner:
        # Wait-VenueQuiet's own deadline statements are run here (taken from the script) with that input: the deadline and the gate end are the caller's end, 239 - 90 = 149 s
        # after the child's start. Not given (0) changes nothing; an end long past ends the wait at once; [long]::MaxValue cannot overflow. LEASE_BATCH_RED_REF=74e14dda: RED, 239.
        wvq_path = DV / "Wait-VenueQuiet.ps1"
        script = (
            ps_parse_source(wvq_path) + "\n"
            "$st = @($ast.EndBlock.Statements)\n"
            "$i0 = 0; while ($i0 -lt $st.Count -and -not $st[$i0].Extent.Text.StartsWith('$deadline = $t0.AddSeconds($MaxWaitSec)')) { $i0++ }\n"
            "$i1 = $i0; while ($i1 -lt $st.Count -and -not ($st[$i1] -is [System.Management.Automation.Language.FunctionDefinitionAst])) { $i1++ }\n"
            "if ($i0 -ge $st.Count -or $i1 -ge $st.Count) { throw \"deadline statements not found ($i0, $i1)\" }\n"
            "$block = [scriptblock]::Create((@($st[$i0..($i1 - 1)] | ForEach-Object { $_.Extent.Text }) -join \"`n\"))\n"
            "$f = [Diagnostics.Stopwatch]::Frequency; $MaxWaitSec = 239; $MaxGateSec = 2400\n"
            "$t0Ts = [Diagnostics.Stopwatch]::GetTimestamp(); $t0 = [DateTime]::UtcNow\n"
            "$parentTs = $t0Ts - 90 * $f; $parentWall = $t0.AddSeconds(30)\n"
            "$out = [ordered]@{}\n"
            "foreach ($k in 'suspendAndStep', 'notGiven', 'longPast', 'maxValue') {\n"
            "    $WaitUntilUtc = $(if ($k -eq 'notGiven') { '' } else { $parentWall.AddSeconds(239).ToString('o') })\n"
            "    $WaitUntilTs = switch ($k) { 'suspendAndStep' { $parentTs + [long](239 * $f) } 'notGiven' { [long]0 } 'longPast' { [long]1 } 'maxValue' { [long]::MaxValue } }\n"
            "    . $block\n"
            "    $out[$k] = [ordered]@{ deadlineSec = [Math]::Round(($deadline - $t0).TotalSeconds, 3); gateEndSec = [Math]::Round(($gateEnd - $t0).TotalSeconds, 3) }\n"
            "}\n"
            "$out | ConvertTo-Json -Compress -Depth 4\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        for key in ("deadlineSec", "gateEndSec"):
            with self.subTest(case="suspendAndStep", bound=key):
                self.assertLessEqual(got["suspendAndStep"][key], 149.3, f"a suspend and a clock step before the child's start moved its wait end past the caller's: {got}")
                self.assertGreater(got["suspendAndStep"][key], 148.0, got)
            with self.subTest(case="notGiven", bound=key):
                self.assertEqual(got["notGiven"][key], 239.0, got)
            with self.subTest(case="longPast", bound=key):
                self.assertEqual(got["longPast"][key], -1.0, got)
            with self.subTest(case="maxValue", bound=key):
                self.assertLessEqual(got["maxValue"][key], 239.0, got)
                self.assertGreater(got["maxValue"][key], 200.0, got)

    # ---------------------------------------------------------------- r9 (Grok G14 prevention on #817): the mixed wall/monotonic clock lint
    def test_lease_batch_idle_end_r9_grok_g14_mixed_clock_lint_the_dual_venue_scripts_hold_exactly_the_known_wall_deadlines(self) -> None:
        # Every .ps1/.psm1 under tools/profiling/dual-venue: a NEW wall-clock deadline (or -WaitUntilUtc counted from a captured wall instant) fails here; a known one that is
        # fixed fails too until it leaves KNOWN_WALL_DEADLINES (a ratchet). The lease room ($leaseEnd - [DateTime]::UtcNow) compares an agent-published wall instant with this
        # host's wall clock, which no rule here covers: successor LEASE-END-MONOTONIC-ROOM-1.
        files = {p.name: p.read_text(encoding="utf-8") for p in sorted(DV.iterdir()) if p.is_file() and p.suffix in (".ps1", ".psm1")}
        self.assertIn(RUNNER.name, files)
        self.assertIn("Wait-VenueQuiet.ps1", files)
        hits = mixed_clock_deadline_hits(files)
        self.assertEqual(sorted(hits - KNOWN_WALL_DEADLINES), [], "a new mixed wall/monotonic deadline: derive it from a Stopwatch, never from a wall instant read before blocking work")
        self.assertEqual(sorted(KNOWN_WALL_DEADLINES - hits), [], "a known wall deadline is gone: remove it from KNOWN_WALL_DEADLINES (the ratchet only shrinks)")

    def test_lease_batch_idle_end_r9_grok_g14_mixed_clock_lint_is_red_on_each_shape_this_card_fixed(self) -> None:
        # The lint on inline fixtures: the 2854b231 probe anchor (G14), the pre-r9 open-cancel reconcile window, the pre-r8 Wait-VenueQuiet deadline read (5fc3bf72-A2), each
        # flagged; the shapes that replaced them, clean.
        red = {
            "g14-2854b231.ps1": ("$dispatchUtc = [DateTime]::UtcNow\n"
                                 "$lv = Get-ProbeLaunchVerdict $MaxWaitSec; $wait = [int]$lv.maxWait\n"
                                 "$qArgs += @('-WaitUntilUtc', $dispatchUtc.AddSeconds($wait).ToString('o'))   # r8 (adversary-f52a32d6.json A1): from the instant read before the cuts\n"),
            "reconcile-pre-r9.ps1": ("$tc = [DateTime]::UtcNow; $cPoll = [Math]::Min($ClaimPollSec, 5); $cr = $null\n"
                                     "while ($true) {\n"
                                     "    if ($cr.state -ceq 'FOUND' -or [DateTime]::UtcNow.AddSeconds($cPoll) -ge $tc.AddSeconds($CancelReconcileSec)) { break }\n"
                                     "}\n"),
            "wvq-5fc3bf72.ps1": ("$t0Ts = [Diagnostics.Stopwatch]::GetTimestamp()\n"
                                 "$t0 = [DateTime]::UtcNow\n"
                                 "function Get-WaitNowUtc { $t0.AddSeconds(([Diagnostics.Stopwatch]::GetTimestamp() - $t0Ts) / [double][Diagnostics.Stopwatch]::Frequency) }\n"
                                 "if ([DateTime]::UtcNow.AddSeconds($RecheckSec) -gt $deadline) { break }\n"),
        }
        # adversary-9a56fa7a.json A2: PowerShell is case-insensitive, so are these (its input, and the sibling spellings it named)
        red.update({
            "case-lower-anchor.ps1": "$t0 = [datetime]::UtcNow\n$deadline = $t0.AddSeconds(30)\n",
            "case-upper-var.ps1": "$T0 = [DATETIME]::UTCNOW\n$DEADLINE = $t0.addseconds(30)\n",
            "case-wait-until.ps1": "$d = [datetime]::utcnow\n$qArgs += @('-WAITUNTILUTC', $D.ADDSECONDS($wait).ToString('o'))\n",
            "case-cge-break.ps1": "$tc = [System.DateTime]::UtcNow\nif ([datetime]::utcnow -cge $TC.addSeconds(5)) { BREAK }\n",
            "offset-anchor.ps1": "$t0 = [DateTimeOffset]::UtcNow\n$gateEnd = $t0.AddSeconds(30)\n",
            # adversary-74e14dda.json A2: a parenthesized anchor
            "paren-anchor.ps1": "$t0 = ([DateTime]::UtcNow)\n$deadline = $t0.AddSeconds(30)\nif ([DateTime]::UtcNow -gt $deadline) { break }\n",
            "case-frame.ps1": ("$t0Ts = [diagnostics.stopwatch]::gettimestamp()\n$t0 = [datetime]::utcnow\n"
                               "function Get-WaitNowUtc { $T0.AddSeconds(([System.Diagnostics.Stopwatch]::GetTimestamp() - $t0Ts) / 1.0) }\n"
                               "if ([DateTime]::UTCNOW -gt $Deadline) { break }\n"),
        })
        # r10 (sol r9b 13e99849-A2, its two strings verbatim): a trailing comment with no space after `#`, and a multiline assignment (the operator ends the line); the
        # single-line control (case-lower-anchor above) stays detected. Siblings of the same shape: a backtick continuation, and a comment on the multiline anchor.
        red.update({
            "sol-r9b-trailing-comment.ps1": "$t0 = [DateTime]::UtcNow #captured\n$deadline = $t0.AddSeconds(30)\n",
            "sol-r9b-multiline.ps1": "$t0 =\n [DateTime]::UtcNow\n$deadline = $t0.AddSeconds(30)\n",
            "backtick-continuation.ps1": "$t0 = `\n    [DateTime]::UtcNow\n$deadline = $t0.AddSeconds(30)\n",
            "multiline-commented.ps1": "$t0 =   # the anchor\n    [DateTime]::UtcNow #captured\n$deadline = $t0.AddSeconds(30)\n",
            # adversary-49bf0ddc.json A4 (its input and two siblings): a parenthesized multiline anchor, a block comment after the anchor, a deadline after `;`
            "adv-a4-paren-multiline.ps1": "$t0 = (\n [DateTime]::UtcNow\n)\nStart-Sleep -Seconds 90\n$deadline = $t0.AddSeconds(240)\n",
            "adv-a4-block-comment.ps1": "$t0 = [DateTime]::UtcNow <# captured #>\n$deadline = $t0.AddSeconds(240)\n",
            "adv-a4-semicolon.ps1": "$t0 = [DateTime]::UtcNow\nStart-Sleep -Seconds 90; $deadline = $t0.AddSeconds(240)\n",
        })
        want = {"g14-2854b231.ps1": {"WAIT_UNTIL", "CAPTURED_WALL"}, "reconcile-pre-r9.ps1": {"CAPTURED_WALL"}, "wvq-5fc3bf72.ps1": {"FRAME_WALL_READ"},
                "case-lower-anchor.ps1": {"CAPTURED_WALL"}, "case-upper-var.ps1": {"CAPTURED_WALL"}, "case-wait-until.ps1": {"WAIT_UNTIL", "CAPTURED_WALL"},
                "case-cge-break.ps1": {"CAPTURED_WALL"}, "offset-anchor.ps1": {"CAPTURED_WALL"}, "case-frame.ps1": {"FRAME_WALL_READ"},
                "paren-anchor.ps1": {"CAPTURED_WALL"}, "sol-r9b-trailing-comment.ps1": {"CAPTURED_WALL"}, "sol-r9b-multiline.ps1": {"CAPTURED_WALL"},
                "backtick-continuation.ps1": {"CAPTURED_WALL"}, "multiline-commented.ps1": {"CAPTURED_WALL"},
                "adv-a4-paren-multiline.ps1": {"CAPTURED_WALL"}, "adv-a4-block-comment.ps1": {"CAPTURED_WALL"}, "adv-a4-semicolon.ps1": {"CAPTURED_WALL"}}
        for name, text in red.items():
            with self.subTest(fixture=name):
                self.assertEqual({rule for (n, rule, _c) in mixed_clock_deadline_hits({name: text})}, want[name])
        green = {
            "g14-r9.ps1": ("$dispatchTs = [Diagnostics.Stopwatch]::GetTimestamp()\n"
                           "$qArgs += @('-WaitUntilUtc', [DateTime]::UtcNow.AddSeconds([Math]::Max(0.0, $wait - ([Diagnostics.Stopwatch]::GetTimestamp() - $dispatchTs) / [double][Diagnostics.Stopwatch]::Frequency)).ToString('o'))\n"),
            "reconcile-r9.ps1": ("$tcTs = [Diagnostics.Stopwatch]::GetTimestamp(); $cPoll = [Math]::Min($ClaimPollSec, 5); $cr = $null\n"
                                 "    if ($cr.state -ceq 'FOUND' -or (([Diagnostics.Stopwatch]::GetTimestamp() - $tcTs) / [double][Diagnostics.Stopwatch]::Frequency) + $cPoll -ge $CancelReconcileSec) { break }\n"),
            "wvq-r8.ps1": ("$t0Ts = [Diagnostics.Stopwatch]::GetTimestamp()\n"
                           "$t0 = [DateTime]::UtcNow\n"
                           "function Get-WaitNowUtc { $t0.AddSeconds(([Diagnostics.Stopwatch]::GetTimestamp() - $t0Ts) / [double][Diagnostics.Stopwatch]::Frequency) }\n"
                           "if ((Get-WaitNowUtc).AddSeconds($RecheckSec) -gt $deadline) { break }\n"),
        }
        for name, text in green.items():
            with self.subTest(fixture=name):
                self.assertEqual(mixed_clock_deadline_hits({name: text}), set())

    def test_lease_batch_idle_end_r9_grok_g14_the_adversary_checklist_carries_the_mixed_clock_line(self) -> None:
        # Grok G14 prevention, second half: the standing adversary checklist line, so a cross-provider pass asks the question on every diff
        self.assertIn(MIXED_CLOCK_CHECKLIST_LINE, ADVERSARY_CHECKLIST.read_text(encoding="utf-8"))

    def test_lease_batch_idle_end_r8_adv_a2_a_published_member_is_read_as_it_was_published(self) -> None:
        # adversary-cc9275e3.json A2: `$(if (Test-JsonObject $r) { $r.startedUtc })` unrolled startedUtc=["<instant>"] into the instant, so an unusable value supplied a runtime.
        # Get-JsonObjectMember keeps an array an array (then Get-AgentJobEndTs gives the send), for a ConvertFrom-Json object and for a dictionary alike; a scalar is the scalar;
        # a missing member, or a result that is not one object ([{..}]), is $null.
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Test-JsonObject', 'Get-JsonObjectMember', 'ConvertTo-AgentUtc', 'Get-AgentJobEndTs') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$out = [ordered]@{}\n"
            "$arr = '{\"startedUtc\":[\"2026-11-14T18:00:00Z\"],\"endedUtc\":\"2026-11-14T18:05:00Z\"}' | ConvertFrom-Json\n"
            "$v = Get-JsonObjectMember $arr 'startedUtc'; $out['psoArray'] = ($v -is [array]) -and @($v).Count -eq 1\n"
            "$d = [ordered]@{ startedUtc = @('2026-11-14T18:00:00Z') }; $v = Get-JsonObjectMember $d 'startedUtc'; $out['dictArray'] = ($v -is [array]) -and @($v).Count -eq 1\n"
            "$s = '{\"startedUtc\":\"2026-11-14T18:00:00Z\"}' | ConvertFrom-Json; $v = Get-JsonObjectMember $s 'startedUtc'; $out['scalar'] = ($null -ne $v) -and -not ($v -is [array])\n"
            "$out['missing'] = $null -eq (Get-JsonObjectMember $s 'nope')\n"
            "$w = '[{\"startedUtc\":\"2026-11-14T18:00:00Z\"}]' | ConvertFrom-Json -NoEnumerate; $out['notOneObject'] = $null -eq (Get-JsonObjectMember $w 'startedUtc')\n"
            "$f = [Diagnostics.Stopwatch]::Frequency; $now = [Diagnostics.Stopwatch]::GetTimestamp(); $floor = $now - 20 * $f; $obs = $now - 1 * $f; $e = [DateTime]::UtcNow.AddSeconds(5)\n"
            "$st = Get-JsonObjectMember ([pscustomobject]@{ startedUtc = @($e.AddSeconds(-6).ToString('o')) }) 'startedUtc'\n"
            "$out['arrayGivesTheSend'] = (Get-AgentJobEndTs $obs $floor ($e.ToString('o')) $st) -eq $floor\n"
            "$out | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"psoArray": True, "dictArray": True, "scalar": True, "missing": True, "notOneObject": True, "arrayGivesTheSend": True})

    def test_lease_batch_idle_end_r8_grok_g12_a_long_leg_under_an_agent_clock_ahead_leaves_the_next_probe_its_allowance(self) -> None:
        # Grok G12, run end to end: agent bound 70, margin 60 (10 s). The first leg's job runs 15 s (longer than the allowance), the agent's clock is 30 s ahead (more than the
        # job-to-observation gap), and the child works 2 s after its job. Charged from the bare send, the next probe's wait was 0; from the send plus the job's own runtime it is
        # about the allowance less the post-job gap (at most 8 s). startedUtc JSON null (the queue-expired shape): the send alone, so 0.
        # r8 (adversary-cc9275e3.json A2): startedUtc inside a one-element array is no instant either: the send alone, so 0 (the caller's `$(...)` unrolled it into the string inside)
        # r10 (sol r9b 9a56fa7a-A1) SUPERSEDES the "runtime" case: the job's runtime as the agent's stamps tell it is an agent wall-clock difference a step during the job inflates,
        # so a long leg is charged from its launch: the 15 s job and the 2 s tail exceed the 10 s allowance and the next probe's wait is 0 (giving the runtime back needs an
        # agent-published monotonic runtime, successor BACHELOR-AGENT-REDEPLOY-IDLESEC-1).
        for name, env, lo, hi in (("runtime", {}, 0, 0),("started-null", {"FAKE_LEG_STARTED": "null"}, 0, 0), ("started-array", {"FAKE_LEG_STARTED": "array"}, 0, 0)):
            with self.subTest(case=name):
                self.setUp()
                self._tree_with_agent_idle(b"70")
                r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", FAKE_LEG_JOB_SEC="15,0", FAKE_LEG_ENDED_AHEAD_SEC="30", FAKE_LEG_TAIL_SLEEP="2", **env)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                waits = self._in_lease_waits()
                self.assertEqual(len(waits), 2, self.events_raw())
                self.assertGreaterEqual(waits[1], lo, f"{name}: the next probe's wait after a long leg under a clock ahead: {waits}")
                self.assertLessEqual(waits[1], hi, f"{name}: the next probe's wait after a long leg under a clock ahead: {waits}")

    def test_lease_batch_idle_end_r6_grok_g11_every_idle_end_call_names_its_parameters_in_full(self) -> None:
        # Grok G11 on #817: a call that abbreviates a parameter (-Append for -AppendDetail) binds today by prefix and turns into an ambiguous-parameter error (BATCH_ERROR on a rare
        # path) the day the helper gains another -Append* parameter. Every Set-BatchLeaseIdleEnded call names each parameter exactly as the helper declares it (AST on both sides).
        # Mutation: a copy whose RETRY site says -Append is caught.
        def offenders(path: Path) -> list:
            script = (
                f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(path)}', [ref]$null, [ref]$null)\n"
                "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Set-BatchLeaseIdleEnded' }, $true)\n"
                "$declared = @($fn.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })\n"
                "$calls = @($ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.CommandAst] -and $x.GetCommandName() -eq 'Set-BatchLeaseIdleEnded' }, $true))\n"
                "$bad = @(foreach ($c in $calls) { foreach ($e in $c.CommandElements) { if ($e -is [System.Management.Automation.Language.CommandParameterAst] -and $declared -cnotcontains $e.ParameterName) { \"$($c.Extent.StartLineNumber):-$($e.ParameterName)\" } } })\n"
                "[ordered]@{ calls = $calls.Count; declared = $declared; bad = $bad } | ConvertTo-Json -Compress\n"
            )
            out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            return json.loads(out.stdout)
        r = offenders(RUNNER)
        self.assertEqual(r["calls"], 4, r)
        self.assertIn("AppendDetail", r["declared"])
        self.assertEqual(r["bad"], [], "a Set-BatchLeaseIdleEnded call abbreviates (or misspells) a parameter")
        src = RUNNER.read_text(encoding="utf-8")
        self.assertEqual(src.count("'a leg ran off the lease: uncounted' -AppendDetail; break }"), 2, "the RETRY and RAN off-lease ends append with the full name")
        mut = self.case / "mutant-g11.ps1"
        mut.write_text(src.replace("'a leg ran off the lease: uncounted' -AppendDetail; break }", "'a leg ran off the lease: uncounted' -Append; break }", 1), encoding="utf-8")
        self.assertEqual(len(offenders(mut)["bad"]), 1, "the mutant's -Append must be caught")

    def test_lease_batch_idle_end_r5_adv_a1_a_leg_whose_lease_batch_is_array_wrapped_is_not_on_the_lease(self) -> None:
        # the :2526 sibling: a leg result whose leaseBatch is [{.. this batch's lease ..}] gave jobLease through member enumeration, so the leg counted on the lease. It names no
        # lease now: not on this batch's lease (LEASE_LOST, never counted), as a leg on another lease is (the FAKE_LEG_LOSE case).
        # r5 pass 3 (adversary r5 pass 2 A1): the leg's whole admission in a one-element array ($ja was unrolled to the object inside it): the same
        for knob in ("FAKE_LEG_LB_WRAP", "FAKE_LEG_ADM_WRAP"):
            with self.subTest(knob=knob):
                self.setUp()
                r = self.run_batch(self.specs[:2], **{knob: "array,none"})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                b1, b2 = self.batches()
                self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"]), ("LEASE_LOST", 1, 0), b1)
                self.assertEqual((b1["legs"][0]["counted"], b1["legs"][0]["onLease"], b1["legs"][0]["jobLease"]), (False, False, None), b1["legs"][0])
                self.assertEqual((b2["legsRan"], b2["legsCounted"]), (1, 1))
                res = batch_result(r.stdout)
                self.assertEqual((res["ran"], res["counted"]), (2, 1))

    def test_lease_batch_idle_end_r5_adv_a1_an_open_reconciled_by_name_with_an_array_wrapped_lease_batch_is_charged_unnamed(self) -> None:
        # the :2646 sibling: the open's um-run failed after the agent ran it (its result landed by name), so the finally reconciles it. Its leaseBatch [{..}] read as
        # RAN_BEFORE_CANCEL with the lease id enumerated out of the array; it is RAN_LEASE_UNNAMED now, charged open to close with no lease id. Control: one object is still
        # RAN_BEFORE_CANCEL on its own lease.
        # r5 pass 2 (adversary r5 A1): a numeric lease id (VenueRawJson clears every identity of that reading) leaves an OBJECT with no lease id: RAN_LEASE_UNNAMED too, never ENDED
        for shape, env, want_state, want_lease, want_basis in (("array", {"FAKE_OPEN_LB_WRAP": "array"}, "RAN_LEASE_UNNAMED", None, "open-lease-unnamed"),
                                                               ("numericLeaseId", {"FAKE_OPEN_LEASE_ID_NUM": "1"}, "RAN_LEASE_UNNAMED", None, "open-lease-unnamed"),
                                                               ("admissionArray", {"FAKE_OPEN_ADM_WRAP": "array"}, "RAN_LEASE_UNNAMED", None, "open-lease-unnamed"),
                                                               ("object", {}, "RAN_BEFORE_CANCEL", "lease-1", "open-to-close")):
            with self.subTest(shape=shape):
                self.setUp()
                r = self.run_batch(self.specs[:1], "-CancelReconcileSec", "3", FAKE_OPEN_MODES="errlanded", **env)
                self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["endReason"], b["openOutcome"], b["legsAttempted"]), ("OPEN_FAILED", "ERROR", 0), b)
                self.assertEqual((b["openCancel"]["state"], b["leaseId"]), (want_state, want_lease), b["openCancel"])
                self.assertEqual((b["ledger"]["basis"], b["ledger"]["leaseId"]), (want_basis, want_lease), b["ledger"])

    def test_lease_batch_idle_end_r10_adv_49bf0ddc_a1_an_unnamed_or_malformed_lease_is_charged_whatever_the_open_classifies_as(self) -> None:
        # adversary-49bf0ddc.json A1: the OPEN_FAILED branch (exit, label, or `admission:[]`, which the `$adm` read unrolls to $null) won before the malformed/unnamed branches that
        # set the unnamed charge, and in the cancel reconcile a cancelled-in-gate label won before a leaseBatch that is there: both closed the token with no ledger row for a
        # lease the agent may hold under it. Now charged open-lease-unnamed, open to close, with no lease id.
        for shape, env, extra, want_code, want_cancel in (
                ("immediate-admission-empty-array", {"FAKE_OPEN_ADM_WRAP": "empty"}, (), 9, None),
                ("immediate-cancelled-label-empty-leasebatch", {"FAKE_OPEN_LABEL": "cancelled-in-gate", "FAKE_OPEN_LB_EMPTY": "1"}, (), 9, None),
                ("reconciled-cancelled-label-empty-leasebatch", {"FAKE_OPEN_MODES": "errlanded", "FAKE_OPEN_LABEL": "cancelled-in-gate", "FAKE_OPEN_LB_EMPTY": "1"},
                 ("-CancelReconcileSec", "3"), 9, "RAN_LEASE_UNNAMED")):
            with self.subTest(shape=shape):
                self.setUp()
                r = self.run_batch(self.specs[:1], *extra, **env)
                self.assertEqual(r.returncode, want_code, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["endReason"], b["legsAttempted"], b["leaseId"]), ("OPEN_FAILED", 0, None), b)
                if want_cancel:
                    self.assertEqual(b["openCancel"]["state"], want_cancel, b["openCancel"])
                self.assertIsNotNone(b["ledger"], f"{shape}: a lease the agent may hold under the token was closed with no ledger row")
                self.assertEqual((b["ledger"]["basis"], b["ledger"]["leaseId"]), ("open-lease-unnamed", None), b["ledger"])

    def test_lease_batch_idle_end_r5_adv_a1_the_lease_batch_reader_takes_one_object_or_none(self) -> None:
        # the class: Get-AdmissionLeaseBatch reads admission.leaseBatch as ONE object (OBJECT), none (ABSENT) or something else that is there (MALFORMED); Get-PublishedIdleBound
        # reads a non-object leaseBatch INVALID (never ABSENT), and Get-LeaseEndUtc places no lease end from one. An admission that is itself an array is MALFORMED too.
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Test-JsonObject', 'Get-AdmissionLeaseBatch', 'Get-PublishedIdleBound', 'Get-LeaseEndUtc') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$lb = '{\"token\":\"0123456789ab\",\"leaseId\":\"L1\",\"untilEpoch\":1794681900,\"remainingSec\":2700,\"idleSec\":0}'\n"
            "$out = [ordered]@{}\n"
            "foreach ($c in @(@('object', $lb), @('array', \"[$lb]\"), @('nullplus', \"[null,$lb]\"), @('nested', \"[[$lb]]\"), @('string', '\"x\"'), @('null', 'null'))) {\n"
            "  $r = ('{\"exitCode\":0,\"endedUtc\":\"2026-11-14T18:00:00Z\",\"admission\":{\"label\":\"clear\",\"leaseBatch\":' + $c[1] + '}}') | ConvertFrom-Json\n"
            "  $out[$c[0]] = @((Get-AdmissionLeaseBatch $r).state, (Get-PublishedIdleBound $r.admission.leaseBatch).state, ($null -ne (Get-LeaseEndUtc $r))) -join '|'\n"
            "}\n"
            # r5 pass 3 (adversary r5 pass 2 A1): an admission that is itself a one-element array, read from the RESULT (never unrolled to the object inside it first)
            "$out['admissionArray'] = (Get-AdmissionLeaseBatch ('{\"exitCode\":0,\"admission\":[{\"label\":\"clear\",\"leaseBatch\":' + $lb + '}]}' | ConvertFrom-Json)).state\n"
            "$out['absent'] = (Get-AdmissionLeaseBatch ('{\"admission\":{\"label\":\"clear\"}}' | ConvertFrom-Json)).state\n"
            "$out['dict'] = (Get-AdmissionLeaseBatch ([pscustomobject]@{ admission = [pscustomobject]@{ leaseBatch = [ordered]@{ token = 't'; leaseId = 'L1' } } })).state\n"
            "$out | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {
            "object": "OBJECT|INVALID|True", "array": "MALFORMED|INVALID|False", "nullplus": "MALFORMED|INVALID|False", "nested": "MALFORMED|INVALID|False",
            "string": "MALFORMED|INVALID|False", "null": "ABSENT|ABSENT|False", "admissionArray": "MALFORMED", "absent": "ABSENT", "dict": "OBJECT"})
        # and no reader in the runner reads a leaseBatch by member access (which enumerates a collection): every one goes through Get-AdmissionLeaseBatch
        src = RUNNER.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"\$\w+(?:\.\w+)*\.leaseBatch\b", src), [])
        self.assertEqual(src.count("Get-AdmissionLeaseBatch $"), 5)
        # r5 pass 3: every caller hands it the RESULT, never an admission already read (unrolled) out of it
        for call in ("Get-AdmissionLeaseBatch $Result)", "Get-AdmissionLeaseBatch $op.result", "Get-AdmissionLeaseBatch $jr.result)", "Get-AdmissionLeaseBatch $cr.result }", "Get-AdmissionLeaseBatch $null"):
            self.assertEqual(src.count(call), 1, call)

    # ---------------------------------------------------------------- r4 (adversary r4 A1): the idle anchor is the agent job's end, not when this runner got round to it
    def test_lease_batch_idle_end_r4_adv_a1_the_idle_allowance_starts_before_the_opens_liveness_cleanup(self) -> None:
        # adversary-253ac155.json A1: the anchor was sampled after Stop-VenueJobLiveness -Remove, so a slow cleanup was not spent from the allowance. Agent bound 70, margin 60
        # (10 s), a 6 s stall in that cleanup: the first probe may wait at most about 4 s.
        self._tree_with_agent_idle(b"70")
        self._patch_tree_runner("        if ($op.outcome -ceq 'RECEIPT' -or $op.outcome -ceq 'RECEIPT_BY_NAME' -or $op.outcome -ceq 'RETRACTED'",
                                "        Start-Sleep -Seconds 6; if ($op.outcome -ceq 'RECEIPT' -or $op.outcome -ceq 'RECEIPT_BY_NAME' -or $op.outcome -ceq 'RETRACTED'")
        r = self.run_batch(self.specs[:1], "-ProbeMaxWaitSec", "600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = self._in_lease_waits()
        self.assertEqual(len(waits), 1, self.events_raw())
        self.assertLessEqual(waits[0], 4, f"the open's liveness cleanup was not spent from the idle allowance: {waits}")

    def test_lease_batch_idle_end_r4_adv_p2_a1_a_measuring_probe_moves_the_idle_anchor_only_to_its_dispatch(self) -> None:
        # adversary-d23711a7.json A1: a measuring probe set the anchor at its RETURN (QUIET) or one -RecheckSec before it (COOLDOWN_UNMET), so the child's own time after its last
        # agent job (a slow gate-log append) was not spent. Agent bound 70, margin 60 (10 s); the first probe's child takes 6 s and reads COOLDOWN_UNMET: the retry may wait at
        # most about 10 - 6 = 4 s (anchored at the dispatch), not about 8 (anchored one second before the return).
        self._tree_with_agent_idle(b"70")
        r = self.run_batch(self.specs[:1], "-ProbeMaxWaitSec", "600", FAKE_QUIET_MODES="COOLDOWN,QUIET", FAKE_QUIET_SLOW_SEC="6")
        waits = self._in_lease_waits()
        self.assertGreaterEqual(len(waits), 2, self.events_raw() + [r.stdout, r.stderr])
        self.assertLessEqual(waits[1], 4, f"the probe child's time after its dispatch was not spent from the idle allowance: {waits}")
        src = RUNNER.read_text(encoding="utf-8")
        self.assertIn("{ $idleAnchorTs = [Math]::Max($idleAnchorTs, $q.startTs) }", src)
        self.assertNotIn("$idleAnchorTs = $q.endTs", src)

    def test_lease_batch_idle_end_r4_adv_p2_a2_the_log_lines_before_an_idle_typing_are_best_effort(self) -> None:
        # adversary-d23711a7.json A2: a throwing L just before Set-BatchLeaseIdleEnded (the LEG RAN and RETRY_IN_LEASE lines) or before the probe's receipt read (the QUIET line)
        # turned a typed idle end into BATCH_ERROR (exit 12). Those three lines are LogBest: a log failure is counted, the typed end stands.
        src = RUNNER.read_text(encoding="utf-8")
        for line in ('LogBest "QUIET leg=$($leg.name) batch=$batchNo phase=in-lease', 'LogBest "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=LEG_NOT_QUIESCENT',
                     'LogBest "LEG $($leg.name) RAN batch=$batchNo'):
            self.assertEqual(src.count(line), 1, line)
            self.assertNotIn(" L " + line[len("LogBest "):], src)

    def test_lease_batch_idle_end_r4_adv_a1_the_idle_allowance_after_a_leg_starts_at_its_jobs_end(self) -> None:
        # the sibling at the leg: the anchor was the leg child's return, after its own receipt work. Agent bound 70, margin 60, the child works 6 s after its job ended: the
        # next probe may wait at most about 4 s (the job's endedUtc), not about 10.
        self._tree_with_agent_idle(b"70")
        r = self.run_batch(self.specs[:2], "-ProbeMaxWaitSec", "600", FAKE_LEG_TAIL_SLEEP="6")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = self._in_lease_waits()
        self.assertEqual(len(waits), 2, self.events_raw())
        self.assertLessEqual(waits[1], 4, f"the leg child's work after its job was not spent from the idle allowance: {waits}")

    def test_lease_batch_idle_end_r3_adv_a1_a_long_claim_poll_sleep_stops_just_past_the_agents_idle_bound(self) -> None:
        # adversary r3 A1: the busy sleep used the loop top's room and -ClaimPollSec only, so a long poll slept far past the idle release (into a room-short end that hid it).
        # Agent bound 12, margin 5, -ClaimPollSec 120: the sleep after the busy probe ends within a few seconds past 12 s, not 120 s later.
        self._tree_with_agent_idle(b"12")
        r = self.run_batch(self.specs[:1], "-ProbeMaxWaitSec", "600", "-ProbeIdleMarginSec", "5", "-ClaimPollSec", "120", FAKE_QUIET_MODES="BUSY,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ticks = [int(l.split(" ", 1)[0]) for l in self.events_raw() if " QUIET " in l]
        self.assertEqual(len(ticks), 2, self.events_raw())
        self.assertLess((ticks[1] - ticks[0]) / 1e7, 40, f"the busy sleep ran past the agent's idle bound: {(ticks[1] - ticks[0]) / 1e7:.1f} s")

    def test_lease_batch_idle_end_r3_grok_g1_an_unreadable_idle_bound_launches_nothing_on_the_lease(self) -> None:
        # fail closed: an agent script whose LeaseBatchIdleSec has no readable default gives no bound, so no probe or leg runs on the lease (AGENT_IDLE_BOUND_UNKNOWN, exit 10);
        # the lease is still closed and the claim released
        tree = self.case / "tree" / "tools" / "profiling"
        shutil.copytree(DV, tree / "dual-venue", ignore=shutil.ignore_patterns("look-receipts", "legs", "legsets"))
        (tree / "bachelor").mkdir()
        agent = AGENT.read_bytes()
        (tree / "bachelor" / AGENT.name).write_bytes(agent.replace(b"[int]$LeaseBatchIdleSec = 300,", b"[int]$LeaseBatchIdleSec,"))
        self.runner = tree / "dual-venue" / RUNNER.name
        r = self.run_batch(self.specs[:2])
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"]), ("AGENT_IDLE_BOUND_UNKNOWN", 0), b)
        self.assertEqual(self.kinds(), [f"OPEN:{b['token']}", f"CLOSE:{b['token']}"])
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["notRun"]), (10, "AGENT_IDLE_BOUND_UNKNOWN", 2))

    def test_r2_a_leg_off_the_lease_reads_only_its_own_tokens_idle_line(self) -> None:
        # the in-flight consumer: a foreign closed line before the agent's idle line still types LEASE_IDLE_ENDED; a lone foreign idle line is no idle end (LEASE_LOST)
        for mode, want in (("foreign-then-idle", "LEASE_IDLE_ENDED"), ("foreign", "LEASE_LOST")):
            with self.subTest(mode=mode):
                self.setUp()
                r = self.run_batch(self.specs[:2], FAKE_LEG_LOSE="1", FAKE_LEG_IDLE_END=mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                b1 = self.batches()[0]
                self.assertEqual((b1["endReason"], b1["legs"][0]["class"], b1["legs"][0]["counted"]), (want, want, False), b1)

    def test_r12_adversary_a2_a_job_lease_that_differs_only_by_an_ignorable_character_is_another_lease(self) -> None:
        # adversary r11 A2 on e5ff0114 (sibling Invoke-VenueLeaseBatch.ps1:2013): jobLease -ceq $b.leaseId is culture-aware, so lease-1 + U+00AD read as the batch's own lease
        # and a leg that ran on ANOTHER lease was counted. Ordinal equality: it is LEASE_LOST and uncounted, exactly like a different lease id.
        r = self.run_batch(self.specs[:2], FAKE_LEG_LEASE_SHY="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["legs"][0]["class"], b1["legs"][0]["onLease"]), ("LEASE_LOST", 1, 0, "LEASE_LOST", False), b1)

    def test_r12_adversary_a2_an_open_whose_token_differs_only_by_an_ignorable_character_is_not_this_batch(self) -> None:
        # sibling Invoke-VenueLeaseBatch.ps1:1762: [string]$adm.leaseBatch.token -cne $token is culture-aware, so token + U+00AD was accepted as this batch's token
        r = self.run_batch(self.specs[:2], FAKE_OPEN_TOKEN_SHY="1")
        b = self.batches()[0]
        self.assertEqual(b["endReason"], "AGENT_NOT_BATCH_CAPABLE", (r.stdout, r.stderr, b))

    def test_r12_adversary_a2_the_agents_batch_receipt_is_found_by_the_exact_lease_id_only(self) -> None:
        # sibling VenueChain.psm1:1087: [string]$rec.leaseId -ceq $LeaseId was culture-aware, and an empty or whitespace lease id matched a record with no lease id
        (self.share / "logs").mkdir()
        # r2: every line carries this batch's token (the reader binds token AND lease id), so only the lease id differs here
        lines = [{"token": "0123456789ab", "leaseId": "L1" + chr(0xAD), "endReason": "other"}, {"token": "0123456789ab", "endReason": "noid"}, {"token": "0123456789ab", "leaseId": "L1", "endReason": "closed"}]
        (self.share / "logs" / "lease-batch-0123456789ab.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'; $shy = [string][char]0x00AD\n"
            "$rd = { param($id) $x = Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId $id; \"$($x.state):$($x.record.endReason)\" }\n"
            "[ordered]@{ exact = (& $rd 'L1'); shyMid = (& $rd ('L' + $shy + '1')); lead = (& $rd ' L1'); empty = (& $rd ''); blank = (& $rd ' ') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"exact": "FOUND:closed", "shyMid": "ABSENT:", "lead": "ABSENT:", "empty": "ABSENT:", "blank": "ABSENT:"})

    def test_r13_adversary_a1_a_job_lease_id_that_reads_as_the_batchs_instant_but_is_spelled_otherwise_is_another_lease(self) -> None:
        # adversary r12 A1 on 08c9aef5 (the sibling of VenueThermalGate.psm1:249 in the runner): the agent's results were read through ConvertFrom-Json, which turns a timestamp-shaped
        # leaseId into a DateTime, so the batch's lease '...:00Z' and a leg job's '...:00.0Z' both became the same culture-formatted string and the leg was counted on a lease it was not on.
        batch_lease = "2026-10-30T10:00:00Z"
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID="2026-10-30T10:00:00.0Z")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["legs"][0]["class"], b1["legs"][0]["onLease"]), (batch_lease, "LEASE_LOST", 1, 0, "LEASE_LOST", False), b1)
        self.assertEqual(b1["legs"][0]["jobLease"], "2026-10-30T10:00:00.0Z", b1)

    def test_r13_adversary_a1_a_timestamp_shaped_lease_id_is_recorded_and_matched_as_written(self) -> None:
        # control for the test above: the same raw timestamp-shaped lease id is the batch's own lease, and the receipt keeps the id as the agent wrote it
        batch_lease = "2026-10-30T10:00:00Z"
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID=batch_lease)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["legsCounted"], b1["legs"][0]["onLease"], b1["legs"][0]["jobLease"]), (batch_lease, 2, True, batch_lease), b1)

    def test_r14_adversary_a1_the_open_read_by_the_real_um_run_and_a_leg_naming_the_same_timestamp_lease_id_count_on_lease(self) -> None:
        # adversary r13 A1 on 778fe1aa: um-run.ps1 still ConvertFrom-Json'd the open's result, so the batch held the lease as DateTime text ('10/30/2026 10:00:00') while the leg result's
        # raw reader returned '2026-10-30T10:00:00Z': onLease false, LEASE_LOST, valid work uncounted. Both now carry the raw string.
        batch_lease = "2026-10-30T10:00:00Z"
        real = str(RUNNER.parent.parent / "um-run.ps1")
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID=batch_lease, FAKE_OPEN_VIA_REAL_UMRUN="1", FAKE_REAL_UMRUN=real)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["legsCounted"], b1["legs"][0]["onLease"], b1["legs"][0]["jobLease"]), (batch_lease, 2, True, batch_lease), b1)

    def test_r14_adversary_a1_control_through_the_real_um_run_a_leg_on_another_spelling_of_the_same_instant_is_another_lease(self) -> None:
        batch_lease = "2026-10-30T10:00:00Z"
        real = str(RUNNER.parent.parent / "um-run.ps1")
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID="2026-10-30T10:00:00.0Z", FAKE_OPEN_VIA_REAL_UMRUN="1", FAKE_REAL_UMRUN=real)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["endReason"], b1["legsCounted"], b1["legs"][0]["onLease"]), (batch_lease, "LEASE_LOST", 0, False), b1)

    def test_r13_adversary_a1_the_agents_batch_receipt_and_the_lease_ledger_keep_the_lease_id_as_written(self) -> None:
        # siblings VenueChain.psm1 Read-VenueLeaseBatchReceipt / Read-VenueLeaseLedger: a timestamp-shaped lease id was a DateTime, so '...:00.0Z' was FOUND for '...:00Z' and the ledger row's id was reformatted
        (self.share / "logs").mkdir()
        lines = [{"token": "0123456789ab", "leaseId": "2026-10-30T10:00:00.0Z", "endReason": "other"}, {"token": "0123456789ab", "leaseId": "2026-10-30T10:00:00Z", "endReason": "closed"}]
        (self.share / "logs" / "lease-batch-0123456789ab.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        claim_dir = self.share / "claims"
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'; $cd = '{psq(claim_dir)}'\n"
            "$rd = { param($id) $x = Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId $id; \"$($x.state):$($x.record.endReason)\" }\n"
            "$p = Get-VenueLeaseLedgerPath -Venue 'v' -ClaimDir $cd; New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null\n"
            "[IO.File]::WriteAllText($p, '{\"startUtc\":\"2026-10-30T10:00:00Z\",\"endUtc\":\"2026-10-30T10:45:00Z\",\"leaseId\":\"2026-10-30T10:00:00.0Z\"}' + \"`n\")\n"
            "$led = @(Read-VenueLeaseLedger -Venue 'v' -ClaimDir $cd -SinceUtc '2000-01-01T00:00:00Z')\n"
            "[ordered]@{ exact = (& $rd '2026-10-30T10:00:00Z'); dotZero = (& $rd '2026-10-30T10:00:00.0Z'); offset = (& $rd '2026-10-30T10:00:00+00:00'); ledger = [string]$led[0].leaseId } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"exact": "FOUND:closed", "dotZero": "FOUND:other", "offset": "ABSENT:", "ledger": "2026-10-30T10:00:00.0Z"})

    def test_an_agent_without_lease_batches_stops_before_any_probe_or_leg(self) -> None:
        r = self.run_batch(self.specs[:2], FAKE_OPEN_MODES="nobatch")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "CLOSE"], k)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "AGENT_NOT_BATCH_CAPABLE")
        self.assertFalse(self.claim_path.exists())
        res = batch_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["NOT_RUN", "NOT_RUN"])

    def test_an_open_that_never_got_its_lease_stops_with_exit_9(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_OPEN_MODES="retracted")
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"]), ("OPEN_FAILED", "RETRACTED"))
        self.assertNotIn("QUIET:", " ".join(self.kinds()))

    def test_an_unmeasured_probe_stops_the_run_and_the_lease_is_still_closed(self) -> None:
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="UNKNOWN")
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "QUIET", "CLOSE"], k)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "PROBE_VENUE_QUIET_UNKNOWN")
        self.assertFalse(self.claim_path.exists())

    def test_a_leg_budget_no_fresh_lease_can_hold_stops_instead_of_opening_lease_after_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEASE_REMAIN="30")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"]), ("LEG_BUDGET_EXCEEDS_LEASE", 0))
        self.assertEqual(len([x for x in self.kinds() if x.startswith("OPEN:")]), 1)

    def test_a_high_preload_leg_ran_but_is_not_counted(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEG_PRELOADS="45")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["class"], b["legs"][0]["counted"], b["legsRan"], b["legsCounted"]), ("QUIET_PRELOAD_HIGH", False, 1, 0))

    # ---------------------------------------------------------------- r2: Bachelor's lease caps (CAPS-ADDENDUM 2026-10-23), each a BLOCKER for the keys
    def test_a_renew_gap_over_120_s_demotes_the_lease_and_the_legs_after_it_are_uncounted(self) -> None:
        # leg 1 ends with 3 s between successful renews; by leg 2's end there was a 130 s gap (heavy-slot.py demoted the lease): leg 2 is typed
        # LEASE_DEMOTED, never counted, the batch ends, and leg 3 runs in a fresh batch.
        r = self.run_batch(self.specs, FAKE_LEG_GAPS="3,130,3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual([(l["class"], l["counted"]) for l in b1["legs"]], [("QUIET_PRELOAD_OK", True), ("LEASE_DEMOTED", False)])
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["renewOkMaxGapSec"]), ("LEASE_DEMOTED", 2, 1, 130.0))
        self.assertEqual(b1["legs"][1]["renewOkMaxGapSec"], 130.0)
        self.assertEqual((b2["legsRan"], b2["legsCounted"], b2["endReason"]), (1, 1, "ALL_LEGS_DONE"))
        self.assertEqual((batch_result(r.stdout)["ran"], batch_result(r.stdout)["counted"]), (3, 2))
        # no JobId twice in one run (test_attr3_footage_stage.py's same-JobId premise for this um-run caller): two batches, four distinct jobs
        jobs = [l.split(" ", 3)[2] for l in self.events_raw() if " UMRUN " in l]
        self.assertEqual((len(jobs), len(set(jobs))), (4, 4), jobs)

    def test_a_leg_with_no_renew_record_is_never_counted(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEG_GAPS="none")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["class"], b["legs"][0]["counted"], b["endReason"]), ("LEASE_RENEW_UNKNOWN", False, "LEASE_RENEW_UNKNOWN"))

    def test_exit_75_refusals_are_typed_wait_and_retry_not_a_failure(self) -> None:
        r = self.run_batch([self.specs[0]])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["exit75Refusals"], b["exit75Class"]), (1, "WAIT_RETRY"))
        self.assertEqual(b["legsCounted"], 1)

    def test_an_open_still_waiting_in_the_agent_gate_is_read_by_name_not_failed(self) -> None:
        # um-run stops UNRESOLVED while the agent still holds the open in its gate (exit-75 refusals); the runner waits for the open's result by
        # name instead of stopping with exit 9, and the batch runs.
        r = self.run_batch([self.specs[0]], FAKE_OPEN_MODES="gatewait")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openOutcome"], b["openLeaseWait"], b["exit75Class"], b["legsCounted"]), ("RECEIPT_BY_NAME", "EXIT_75_WAIT", "WAIT_RETRY", 1))
        self.assertEqual(len([x for x in self.kinds() if x.startswith("OPEN:")]), 1, "the open is never resubmitted")

    def test_the_cooldown_is_waited_out_before_the_next_open_and_without_the_claim(self) -> None:
        r = self.run_batch(self.specs[:2], "-LeaseCooldownSec", "4", FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual(b1["preOpenWait"]["reason"], "NONE")
        self.assertEqual(b2["preOpenWait"]["reason"], "COOLDOWN")
        self.assertGreaterEqual(b2["preOpenWait"]["waitSec"], 1)
        ticks = {}
        for line in self.events_raw():
            t, kind, rest = line.split(" ", 2)
            if kind == "UMRUN":
                ticks.setdefault(rest.split()[0].split("-")[2], []).append(int(t))
        close1, open2 = ticks["close"][0], ticks["open"][1]
        self.assertGreaterEqual((open2 - close1) / 1e7, 3.5, "the second open came inside the cooldown")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertLess(log.index(" BATCH_WAIT batch=2 reason=COOLDOWN"), log.index(" CLAIM_TAKEN batch=2 "), "the claim was held through the cooldown")
        rows = [json.loads(l) for l in (self.claims / "bachelor.lease-ledger.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual([x["leaseId"] for x in rows], ["lease-1", "lease-2"], "every lease this runner took is in the venue's ledger")

    def test_the_3_hour_budget_from_an_earlier_run_is_waited_out_or_typed_at_the_deadline(self) -> None:
        # an earlier run's two 45-min leases (ledger) leave no room for a third in this 3 h window: the runner never opens, and says why.
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        r = self.run_batch([self.specs[0]])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["preOpenWait"]["reason"]), ("DEADLINE", "BUDGET"))
        self.assertIn("lease-budget-wait", b["endDetail"])
        # the first lease leaves the window ending at open + 45 min only about 75 min from now; old-2 (44 min) is still in it then
        self.assertGreater(b["preOpenWait"]["waitSec"], 4000)
        self.assertGreater(b["preOpenWait"]["usedSec"], 2500)
        self.assertEqual(self.kinds(), [], "nothing is submitted when the budget has no room before the deadline")

    def test_the_caps_cannot_be_relaxed_against_the_real_um_run(self) -> None:
        base = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RUNNER), "-LegSpec", str(self.specs[0]), "-RunDir", str(self.run_dir),
                "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST, "-ClaimDir", str(self.claims), "-QuietScript", str(self.tmp / "fake-quiet.ps1"),
                "-LegScript", str(self.tmp / "fake-leg.ps1"), "-AgentShare", str(self.share)]
        # (a shortened -LeaseCooldownSec is judged against the LIVE caps after the probe: test_a_caller_may_only_tighten_the_live_caps)
        for extra, needle in ((["-LeaseBudgetWindowSec", "3600"], "LEASE_CAP_RELAXED"),
                              (["-LeaseCapSec", "2701"], "LeaseCapSec"), (["-RenewGapMaxSec", "121"], "RenewGapMaxSec"), (["-LeaseBudgetSec", "10801"], "LeaseBudgetSec")):
            with self.subTest(extra=extra):
                out = subprocess.run(base + extra, capture_output=True, text=True, timeout=120)
                self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn(needle, out.stdout + out.stderr)
        self.assertFalse(self.claim_path.exists())
        self.assertFalse(self.events.exists(), "nothing was submitted")

    def test_the_budget_wait_rule(self) -> None:
        # Get-VenueLeaseBudgetWait: a lease is opened only when the 3 h window ending at its LAST possible second (open + 45 min) holds at most
        # 90 min of leased time, and never within 30 min of the last lease's end.
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        def m(x: float) -> str:
            return utc_iso(now + timedelta(minutes=x))
        cases = {"none": [], "cooldown": [(m(-40), m(-10))], "budget": [(m(-120), m(-75)), (m(-45), m(0))], "old": [(m(-400), m(-355))]}
        script = f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n$now = '{utc_iso(now)}'\n$o = [ordered]@{{}}\n"
        for name, leases in cases.items():
            arr = ", ".join(f"[pscustomobject]@{{ startUtc = '{s}'; endUtc = '{e}' }}" for s, e in leases)
            script += f"$w = Get-VenueLeaseBudgetWait -Leases @({arr}) -NowUtc $now; $o['{name}'] = [ordered]@{{ reason = $w.reason; waitSec = $w.waitSec; usedSec = $w.usedSec }}\n"
        script += "$o | ConvertTo-Json -Compress -Depth 4\n"
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["none"], {"reason": "NONE", "waitSec": 0, "usedSec": 0})
        self.assertEqual((got["cooldown"]["reason"], got["cooldown"]["waitSec"]), ("COOLDOWN", 1200))
        # leases at -120..-75 and -45..0 min: the cooldown ends at +30, and the first lease leaves the window ending at open + 45 only at +60
        self.assertEqual((got["budget"]["reason"], got["budget"]["waitSec"], got["budget"]["usedSec"]), ("BUDGET", 3600, 2700))
        self.assertEqual(got["old"], {"reason": "NONE", "waitSec": 0, "usedSec": 0})

    # ---------------------------------------------------------------- BACHELOR-DEDICATED-CAPS-RUNNER-1: the caps are read live, not mirrored
    LIVE_CAPS = {"enabled": True, "max_minutes": 45, "pending_max_minutes": 30, "cooldown_minutes": 5, "max_fraction_per_3h": 0.9, "renew_stale_s": 120}

    def caps_events_raw(self) -> list[str]:
        return self.caps_events.read_text(encoding="utf-8").splitlines() if self.caps_events.exists() else []

    def seed_ledger(self, *spans: tuple[float, float]) -> None:
        """Earlier leases of this venue, as (start, end) minutes from now (negative = past)."""
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": f"old-{i}", "startUtc": utc_iso(now + timedelta(minutes=s)), "endUtc": utc_iso(now + timedelta(minutes=e))}
                for i, (s, e) in enumerate(spans, 1)]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")

    def test_the_live_caps_come_from_one_exempt_probe_job_and_are_in_the_receipt(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS), OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        probes = self.caps_events_raw()
        self.assertEqual(len(probes), 1, probes)
        self.assertRegex(probes[0].split(" ", 3)[2], r"^venue-quiet-probe-caps-\d{8}T\d{6}Z-[0-9a-f]{8,}$")
        self.assertIn("heavy-slot.lease.json", probes[0], "the probe prints Bachelor's lease config")
        self.assertIn(r"C:\Users\layib\.claude\ops\heavy-slot.lease.json", probes[0])
        self.assertIn("notAfter=<unset>", probes[0])
        [b] = self.batches()
        self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"], b["capsCapSec"]), ("live", 300, 9720, 2700), b)
        self.assertEqual(batch_result(r.stdout)["capsSource"], "live")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertRegex(log, r" CAPS source=live cooldownSec=300 budgetSec=9720 capSec=2700 ")
        self.assertLess(log.index(" CAPS source="), log.index(" BATCH_OPEN batch=1 "), "the caps are read before the first lease is requested")

    def test_a_live_5_minute_cooldown_is_waited_not_the_old_30(self) -> None:
        # the last lease ended 4 min 50 s ago: Bachelor's live cooldown (5 min) leaves about 10 s, the old mirrored 30 min would pass the 240 s deadline.
        self.seed_ledger((-50.0, -4.0 - 50 / 60))
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS), OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual(b["preOpenWait"]["reason"], "COOLDOWN")
        self.assertTrue(1 <= b["preOpenWait"]["waitSec"] <= 300, b["preOpenWait"])
        self.assertEqual(len([x for x in self.kinds() if x.startswith("OPEN:")]), 1)

    def test_the_live_budget_fraction_paces_the_3_hour_window_not_50_percent(self) -> None:
        # two 45 min leases (90 min) in the window: 50 % (90 min) leaves no room for a third, live 0.9 (162 min) does, so the open is not delayed.
        self.seed_ledger((-130.0, -85.0), (-51.0, -6.0))
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS), OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["preOpenWait"]["reason"], b["preOpenWait"]["waitSec"]), ("NONE", 0))
        self.assertEqual(b["capsBudgetSec"], 9720)
        self.assertEqual(b["preOpenWait"]["budgetRoomSec"], 2700)

    def test_live_caps_that_cannot_be_read_or_do_not_validate_fall_back_to_the_old_constants(self) -> None:
        bad = {
            "probe-failed": ("fail", ""),
            "file-missing": ("live", "HEAVY_SLOT_LEASE_JSON_MISSING"),
            "not-json": ("live", "{ not json"),
            "empty": ("live", ""),
            "not-an-object": ("live", "[1, 2]"),
            "probe-exit-1": ("exit1", json.dumps(self.LIVE_CAPS)),
            "disabled": ("live", json.dumps({**self.LIVE_CAPS, "enabled": False})),
            "enabled-missing": ("live", json.dumps({k: v for k, v in self.LIVE_CAPS.items() if k != "enabled"})),
            "negative-cooldown": ("live", json.dumps({**self.LIVE_CAPS, "cooldown_minutes": -1})),
            "zero-max-minutes": ("live", json.dumps({**self.LIVE_CAPS, "max_minutes": 0})),
            "text-fraction": ("live", json.dumps({**self.LIVE_CAPS, "max_fraction_per_3h": "0.9"})),
            "missing-renew": ("live", json.dumps({k: v for k, v in self.LIVE_CAPS.items() if k != "renew_stale_s"})),
        }
        for name, (mode, text) in bad.items():
            with self.subTest(name):
                self.setUp()
                r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE=mode, FAKE_CAPS_JSON=text, OMIT_DEFAULTS="-LeaseCooldownSec")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"], b["capsCapSec"]), ("fallback", 1800, 5400, 2700), b)
                self.assertTrue(b["capsFallbackReason"], b)
                self.assertEqual(batch_result(r.stdout)["capsSource"], "fallback")
                self.assertRegex((self.run_dir / "batch.log").read_text(encoding="utf-8"), r" CAPS source=fallback cooldownSec=1800 budgetSec=5400 capSec=2700 ")

    def test_the_old_constants_still_pace_a_fallback_run(self) -> None:
        # unreadable caps: a lease that ended 4 min ago still holds the run for the old 30 min cooldown, which passes the deadline.
        self.seed_ledger((-45.0, -4.0))
        r = self.run_batch([self.specs[0]], OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["preOpenWait"]["reason"], b["capsSource"]), ("DEADLINE", "COOLDOWN", "fallback"))

    def test_live_caps_tighter_than_the_old_ones_bind_too(self) -> None:
        # the owner may also tighten: a 20 min lease cap and a 60 min cooldown read live bind the run the same way.
        caps = {**self.LIVE_CAPS, "max_minutes": 20, "cooldown_minutes": 60, "max_fraction_per_3h": 0.25}
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(caps), OMIT_DEFAULTS="-LeaseCooldownSec", FAKE_LEASE_REMAIN="1200", FAKE_LEG_REMAIN="1100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"], b["capsCapSec"]), ("live", 3600, 2700, 1200), b)

    def test_a_caller_may_only_tighten_the_live_caps(self) -> None:
        # Resolve-VenueLeaseCaps: against the real um-run an explicit cooldown below the live one, or a budget above it, is LEASE_CAP_RELAXED; tighter is kept;
        # only an offline stub may go looser. Get-VenueLeaseCaps reads the probe's text with heavy-slot.py's rules.
        live = json.dumps(self.LIVE_CAPS)
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$live = Get-VenueLeaseCaps -Text '{live}'
$fb = Get-VenueLeaseCaps -Text 'HEAVY_SLOT_LEASE_JSON_MISSING'
$o = [ordered]@{{ live = "$($live.source) $($live.cooldownSec) $($live.budgetSec) $($live.capSec)"; fb = "$($fb.source) $($fb.cooldownSec) $($fb.budgetSec) $($fb.capSec)" }}
function Test-Caps($caps, [int]$cd, [bool]$cdx, [int]$bud, [bool]$budx, [bool]$relax) {{
    $r = Resolve-VenueLeaseCaps -Caps $caps -CooldownSec $cd -CooldownExplicit $cdx -BudgetSec $bud -BudgetExplicit $budx -CapSec 2700 -AllowRelax $relax
    "$($r.ok) $($r.cooldownSec) $($r.budgetSec) $($r.capSec) $($r.error)".Trim()
}}
$o['default'] = Test-Caps $live 1800 $false 5400 $false $false
$o['equal'] = Test-Caps $live 300 $true 9720 $true $false
$o['tighter'] = Test-Caps $live 600 $true 5000 $true $false
$o['cooldown-looser'] = Test-Caps $live 60 $true 5400 $false $false
$o['budget-looser'] = Test-Caps $live 1800 $false 10000 $true $false
$o['stub-looser'] = Test-Caps $live 0 $true 10000 $true $true
$o['fb-cooldown-looser'] = Test-Caps $fb 300 $true 5400 $false $false
$o['fb-budget-looser'] = Test-Caps $fb 1800 $false 9000 $true $false
$o['fb-tighter'] = Test-Caps $fb 2400 $true 3000 $true $false
$o | ConvertTo-Json -Compress
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["live"], "live 300 9720 2700")
        self.assertEqual(got["fb"], "fallback 1800 5400 2700")
        self.assertEqual(got["default"], "True 300 9720 2700", "not explicit: the live caps apply")
        self.assertEqual(got["equal"], "True 300 9720 2700")
        self.assertEqual(got["tighter"], "True 600 5000 2700")
        self.assertTrue(got["cooldown-looser"].startswith("False") and "LEASE_CAP_RELAXED" in got["cooldown-looser"], got["cooldown-looser"])
        self.assertTrue(got["budget-looser"].startswith("False") and "LEASE_CAP_RELAXED" in got["budget-looser"], got["budget-looser"])
        self.assertEqual(got["stub-looser"], "True 0 10000 2700", "the offline stub keeps the old shortening rule")
        self.assertIn("LEASE_CAP_RELAXED", got["fb-cooldown-looser"])
        self.assertIn("LEASE_CAP_RELAXED", got["fb-budget-looser"])
        self.assertEqual(got["fb-tighter"], "True 2400 3000 2700")

    def test_r2_b1_a_non_default_window_never_loosens_the_budget_of_the_fixed_3_hour_basis(self) -> None:
        # CoS/Dot B1 on affa0d1a: budget = max_fraction_per_3h * 10800 whatever -LeaseBudgetWindowSec says (it used to be fraction * the CALLER's window, so 21600 doubled
        # it). A longer window can only count MORE of the earlier leases against the same budget; a shorter one may only shrink the budget, never raise it.
        caps = json.dumps({**self.LIVE_CAPS, "cooldown_minutes": 0, "max_fraction_per_3h": 0.5})
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$now = [DateTime]::UtcNow
$o = [ordered]@{{}}
foreach ($w in 10800, 21600, 86400, 3600, 600) {{
    $c = Get-VenueLeaseCaps -Text '{caps}' -WindowSec $w
    # 5400 s of leases that ended just now (the exact CoS example): a 45 min lease needs 5400 - 2700 or less already used in its window
    $leases = @([pscustomobject]@{{ startUtc = $now.AddSeconds(-5400).ToString('o'); endUtc = $now.ToString('o') }})
    $b = Get-VenueLeaseBudgetWait -Leases $leases -NowUtc $now -CapSec $c.capSec -CooldownSec $c.cooldownSec -WindowSec $w -BudgetSec $c.budgetSec
    $o["w$w"] = [ordered]@{{ source = $c.source; budgetSec = $c.budgetSec; reason = $b.reason; waitSec = $b.waitSec }}
}}
$o | ConvertTo-Json -Compress -Depth 4
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["w10800"]["budgetSec"], 5400)
        for w in (21600, 86400):
            with self.subTest(window=w):
                self.assertEqual(got[f"w{w}"]["budgetSec"], 5400, "a longer window must not raise the budget")
                self.assertEqual(got[f"w{w}"]["reason"], "BUDGET", "5400 s taken, fraction 0.5: the next lease WAITS")
                self.assertGreater(got[f"w{w}"]["waitSec"], 0)
        for w in (3600, 600):
            with self.subTest(window=w):
                self.assertLessEqual(got[f"w{w}"]["budgetSec"], 5400, "a shorter window may only tighten")
                self.assertEqual(got[f"w{w}"]["budgetSec"], w // 2, "the fraction of the shorter window")
        self.assertEqual(got["w10800"]["reason"], "BUDGET")

    def test_r2_b1b_a_configured_cooldown_is_never_silently_shortened(self) -> None:
        # CoS/Dot B1b on affa0d1a: cooldown_minutes 2880 (2 days) was clipped to 86400 s. The exact value is kept up to what an int of seconds holds; beyond that the
        # config is unrepresentable and is the FALLBACK with a typed reason (never a shorter cooldown than the one configured).
        def caps(minutes: float) -> str:
            return json.dumps({**self.LIVE_CAPS, "cooldown_minutes": minutes})
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$o = [ordered]@{{}}
$i = 0
foreach ($t in '{caps(1440)}', '{caps(2880)}', '{caps(10080)}', '{caps(0.5)}', '{caps(1e9)}') {{
    $c = Get-VenueLeaseCaps -Text $t
    $o["c$i"] = "$($c.source) $($c.cooldownSec) $($c.reason)".Trim(); $i++
}}
$o | ConvertTo-Json -Compress
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["c0"], "live 86400")
        self.assertEqual(got["c1"], "live 172800", "2880 min is 172800 s, not the old 86400 clip")
        self.assertEqual(got["c2"], "live 604800")
        self.assertEqual(got["c3"], "live 30")
        self.assertTrue(got["c4"].startswith("fallback 1800 cooldown_minutes-unrepresentable"), got["c4"])

    def test_r2_i1_two_caps_probe_ids_built_in_the_same_second_differ_and_keep_the_agents_shapes(self) -> None:
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$ids = @(1..40 | ForEach-Object {{ New-VenueCapsProbeId }})
[ordered]@{{ ids = $ids; distinct = @($ids | Select-Object -Unique).Count }} | ConvertTo-Json -Compress
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["distinct"], 40, "40 ids built back to back (the same second) must all differ")
        for i in got["ids"]:
            self.assertRegex(i, r"^venue-quiet-probe-caps-\d{8}T\d{6}Z-[0-9a-f]{8,}$")
            self.assertRegex(i, r"^(venue-quiet-probe|agent-swap)-", "the agent's GateExemptJobPattern")
            self.assertRegex(i, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", "UmRunDrop's job id rule")

    def test_an_explicit_tighter_cooldown_and_budget_win_over_the_live_caps_in_a_run(self) -> None:
        r = self.run_batch([self.specs[0]], "-LeaseCooldownSec", "900", "-LeaseBudgetSec", "6000", FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"]), ("live", 900, 6000), b)

    # ---------------------------------------------------------------- r3: sol r2 B1 (no lease after the runner exits) and B2 (no leg past the lease's end)
    def test_r3_an_open_held_in_the_gate_through_the_deadline_is_cancelled_and_reconciled_before_the_claim_is_released(self) -> None:
        # The agent holds the open in its gate (exit-75 refusals) past the deadline, and the close job times out (um-run's queue ceiling: the agent never
        # took it). The runner must CANCEL the open and read the agent's answer by name before it releases the claim, so the pending request can never
        # take an unused lease once the runner is gone. This is also the gate-wait-reaches-deadline path (DEADLINE, exit 3, openLeaseWait EXIT_75_WAIT).
        agent = _FakeAgentGate(self.share, self.claim_path)
        agent.start()
        try:
            r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)),
                               FAKE_OPEN_MODES="gatehold", FAKE_CLOSE_MODES="timeout")
        finally:
            agent.runner_exited.set()
            agent.join(10)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(agent.acquired_after_exit, "the open was never cancelled: its pending request takes an unused lease after the runner exited")
        self.assertIsNotNone(agent.cancel_seen)
        [b] = self.batches()
        job, claim_held = agent.cancel_seen
        self.assertEqual(job, b["openJobId"])
        self.assertTrue(claim_held, "the open was cancelled only after the runner released its claim")
        self.assertEqual((b["endReason"], b["openOutcome"], b["openLeaseWait"], b["exit75Class"]), ("DEADLINE", "UNRESOLVED", "EXIT_75_WAIT", "WAIT_RETRY"))
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"], b["openCancel"]["resultLabel"]), ("REQUESTED", "CANCELLED", "cancelled-in-gate"))
        self.assertEqual(b["closeOutcome"], "UNRESOLVED")
        self.assertEqual((b["leaseId"], b["ledger"]), (None, None), "a cancelled open took no lease, so the ledger has no row for it")
        self.assertFalse((self.claims / "bachelor.lease-ledger.jsonl").exists())
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertLess(log.index(" BATCH_OPEN_CANCEL batch=1 "), log.index(" BATCH_OPEN_RECONCILED batch=1 state=CANCELLED "))
        self.assertLess(log.index(" BATCH_OPEN_RECONCILED batch=1 "), log.index(" CLAIM_RELEASED batch=1 "), "the claim was released before the open was reconciled")
        self.assertNotIn("QUIET:", " ".join(self.kinds()))
        self.assertFalse(self.claim_path.exists())

    def test_r3_an_open_the_agent_never_answers_is_typed_unreconciled_and_exits_11(self) -> None:
        # Nothing answers the cancel: the run says so (exit 11, openCancel UNRECONCILED), its cancel file stays for the agent, and the ledger counts a full
        # lease for it. r4 (sol r3 B1): NO LEASE AFTER notAfterUtc. The open carries the batch deadline as notAfterUtc (um-run.ps1 -NotAfterUtc), so an agent
        # that never answers the cancel still never requests, keeps or hands out a lease for it past the deadline (proven against the agent's real gate by
        # the quiet-lease harness case batch-open-notafter). The close job carries no bound.
        deadline = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", deadline, "-CancelReconcileSec", "3",
                           FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"))
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists(), "the cancel request stays for the agent")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled")
        self.assertEqual(batch_result(r.stdout)["stopReason"], "OPEN_CANCEL_UNRECONCILED")
        bound = {l.split()[2]: l.split()[4] for l in self.events_raw() if " UMRUN " in l}
        want = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        got = bound.get(b["openJobId"], "notAfter=<missing>").split("=", 1)[1]
        self.assertTrue(got.endswith("Z") and datetime.fromisoformat(got[:26].rstrip("Z") + "+00:00") == want,
                        f"the open's notAfterUtc is the batch deadline as an ISO-8601 UTC 'o' string: {got!r} != {deadline!r}")
        self.assertEqual(b["openNotAfterUtc"], got, "the batch receipt records the open's bound")
        self.assertEqual(bound.get(b["closeJobId"]), "notAfter=<unset>", "the close job carries no notAfterUtc")

    def test_r3_a_leg_still_running_at_lease_end_is_killed_as_a_whole_tree_and_uncounted(self) -> None:
        # sol r2 B2 at test scale: the leg is admitted with 63 s of lease left (>= the 60 s budget + 0 s margin; on Bachelor 360 + 120 = 480 s), then hangs
        # past the lease's end. It is killed by the lease's end with its whole process tree, typed LEASE_EXPIRED (uncounted), its agent job is asked to
        # cancel, and it is never re-run.
        self.addCleanup(self._kill_fake_leg_pids)
        r = self.run_batch([self.specs[0]], FAKE_LEASE_REMAIN="63", FAKE_LEG_MODES="hang")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        [leg] = b["legs"]
        self.assertEqual((leg["status"], leg["class"], leg["outcome"], leg["counted"], b["endReason"]), ("LEASE_EXPIRED", "LEASE_EXPIRED", "INVALID", False, "LEASE_EXPIRED"))
        self.assertLessEqual(leg["legSec"], 66, "the leg ran past the lease's end")
        self.assertEqual((b["legsAttempted"], b["legsRan"], b["legsCounted"]), (1, 0, 0))
        leg_pid, gc_pid, job = (self.share / "fake-leg-pids.txt").read_text(encoding="utf-8").split()
        for pid in (int(leg_pid), int(gc_pid)):
            self.assertTrue(_wait_pid_gone(pid, 5), f"pid {pid} of the leg's process tree outlived the lease")
        self.assertEqual((leg["jobId"], leg["cancel"]), (job, "REQUESTED"))
        self.assertTrue((self.share / "cancel" / f"{job}.cancel").exists(), "the leg's agent job was not asked to cancel")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"], res["notRun"], res["leaseExpired"]), (0, 0, 0, 1))
        self.assertEqual(len([k for k in self.kinds() if k.startswith("LEG:")]), 1, "a leg killed at the lease's end is never re-run")

    def test_r3_a_leg_the_agent_stopped_at_its_lease_bound_is_typed_and_uncounted(self) -> None:
        # The agent's own bound (lease-batch r3): a batch job still running at its lease's end, or on a demoted lease, is stopped and says why.
        r = self.run_batch(self.specs, FAKE_LEG_STOPS="lease-end,demoted,none")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2, b3 = self.batches()
        self.assertEqual([(b["legs"][0]["class"], b["legs"][0]["counted"], b["endReason"]) for b in (b1, b2)], [("LEASE_EXPIRED", False, "LEASE_EXPIRED"), ("LEASE_DEMOTED", False, "LEASE_DEMOTED")])
        self.assertEqual((b3["legsCounted"], b3["endReason"]), (1, "ALL_LEGS_DONE"))

    def test_r3_a_leg_process_that_exits_non_zero_is_leg_failed_never_counted_and_the_run_exits_7(self) -> None:
        # fable r2 hardening: the LEG_FAILED path at run time, not only by the static scans.
        r = self.run_batch(self.specs[:2], FAKE_LEG_MODES="exit3,ok")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual([l["status"] for l in b["legs"]], ["LEG_FAILED", "RAN"])
        self.assertEqual((b["legs"][0]["outcome"], b["legs"][0]["counted"], b["legs"][0]["legExit"]), ("INVALID", False, 3))
        self.assertEqual((b["legsRan"], b["legsCounted"]), (1, 1))
        res = batch_result(r.stdout)
        self.assertEqual((res["failed"], res["ran"], res["counted"]), (1, 1, 1))

    def test_r3_a_busy_probe_with_less_room_than_one_recheck_ends_the_batch_instead_of_spinning(self) -> None:
        # fable r2 note: once the room left after the leg's budget is under -RecheckSec, a busy probe ends the batch (the leg goes to the next one)
        # instead of re-probing back to back until the room runs out.
        r = self.run_batch([self.specs[0]], "-RecheckSec", "30", FAKE_QUIET_MODES="COOLDOWN,QUIET", FAKE_LEASE_REMAIN="70,2700")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["probeRetries"], b1["legsAttempted"]), ("LEASE_REMAINING_SHORT", 1, 0))
        self.assertIn("recheckSec=30", b1["endDetail"])
        self.assertEqual((b2["legsCounted"], b2["endReason"]), (1, "ALL_LEGS_DONE"))
        self.assertEqual(len([k for k in self.kinds() if k.startswith("QUIET:")]), 2)

    def test_r3_a_legs_own_exit_75_refusals_are_counted_into_the_batch_receipt(self) -> None:
        # fable r2 hardening: a leg that took a fresh lease under the batch token carries its exit-75 refusals; the batch receipt counts them too.
        r = self.run_batch([self.specs[0]], FAKE_LEG_REFUSALS="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openExit75Refusals"], b["legExit75Refusals"], b["exit75Refusals"], b["exit75Class"]), (1, 2, 3, "WAIT_RETRY"))
        self.assertEqual(b["legs"][0]["exit75Refusals"], 2)

    def _kill_fake_leg_pids(self) -> None:
        p = self.share / "fake-leg-pids.txt"
        if p.exists():
            for tok in p.read_text(encoding="utf-8").split()[:2]:
                try:
                    os.kill(int(tok), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    # ---------------------------------------------------------------- r5: the open's runner-liveness sidecar (sol B1 on an early runner exit)
    def _agent_verdict(self, notafter_dir: Path, job_id: str, not_after_utc: str) -> dict:
        """The agent's OWN reader and verdict (Read-JobNotAfterSidecar, Get-JobNotAfterVerdict, lifted from ultra-magnus-agent.ps1) on a sidecar folder."""
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(AGENT)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Get-JobNotAfterVerdict', 'Read-JobNotAfterSidecar') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            f"$notAfterDir = '{psq(notafter_dir)}'\n"
            f"$v = Get-JobNotAfterVerdict -Metadata ([pscustomobject]@{{ notAfterUtc = '{not_after_utc}' }}) -Sidecar (Read-JobNotAfterSidecar -JobId '{job_id}')\n"
            "[ordered]@{ state = $v.state; source = $v.source; livenessState = $v.livenessState; livenessSeq = $v.livenessSeq; why = $v.why } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout)

    def test_a_runner_killed_while_its_open_waits_stops_renewing_the_sidecar_and_the_agent_reads_it_lapsed(self) -> None:
        # r5 (sol B1; #644 + #663 + this): um-run blocks on the open (the agent re-asking a refused request). The runner keeps the open's sidecar 6 s ahead,
        # rewritten every 1 s, in the agent's format. Killed (process tree, as a lane timeout or a closed console does), it renews no more: the last
        # aliveUntilUtc passes and the agent's own verdict for the open turns passed (source=sidecar), so its wait ends and its request is released (the
        # agent side of this is batch-runner-killed-liveness in test-ultra-magnus-agent-quiet-lease.ps1). RED: the runner writes no sidecar.
        args, e = self.batch_cmd([self.specs[0]], "-LivenessWindowSec", "6", "-LivenessRenewSec", "1", FAKE_OPEN_SLEEP="200")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        side_dir = self.share / "notafter"
        try:
            def read_side() -> dict | None:
                files = [f for f in side_dir.glob("lease-batch-open-*.json")]
                if len(files) != 1:
                    return None
                try:
                    return json.loads(files[0].read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
            end = time.monotonic() + 60
            s = None
            while time.monotonic() < end:
                s = read_side()
                if s is not None and int(s["seq"]) >= 3:
                    break
                time.sleep(0.2)
            self.assertTrue(s is not None and int(s["seq"]) >= 3, f"the runner keeps the open's sidecar renewed while um-run blocks: {s} {list(side_dir.iterdir())}")
            job = s["jobId"]
            self.assertEqual((s["schema"], s["writerPid"]), ("mlv-app/job-notafter-sidecar/v1", p.pid), s)
            self.assertTrue(job.startswith("lease-batch-open-") and (side_dir / f"{job}.json").exists(), job)
            far = utc_iso(datetime.now(timezone.utc) + timedelta(hours=1))
            # while the runner lives, the agent reads its open as live
            live = self._agent_verdict(side_dir, job, far)
            self.assertEqual((live["state"], live["livenessState"], live["source"]), ("open", "live", "meta"), live)
            self.assertIsNone(p.poll(), "the runner is still waiting on its open")
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
            p.wait(timeout=60)
            killed_at = datetime.now(timezone.utc)
            time.sleep(1.5)   # a write in flight at the kill lands, or not
            s1 = read_side()
            until = parse_utc(s1["aliveUntilUtc"])
            self.assertLessEqual(until, killed_at + timedelta(seconds=7), f"the last aliveUntilUtc is within the window of the kill: {s1}")
            while datetime.now(timezone.utc) < until + timedelta(seconds=1):
                time.sleep(0.2)
            s2 = read_side()
            self.assertEqual(s2["seq"], s1["seq"], "nothing renews the sidecar after the runner is killed")
            dead = self._agent_verdict(side_dir, job, far)
            self.assertEqual((dead["state"], dead["livenessState"], dead["source"]), ("passed", "lapsed", "sidecar"), dead)
            self.assertFalse(_pid_alive(p.pid), "the killed runner is gone")
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()

    # ---------------------------------------------------------------- CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-1 (merge_group 40211588250)
    def _read_claim_while_writer_holds(self, *, reader_timeout_s: float, pause_after_start_s: float = 0.0, pause_before_direct_s: float = 0.0,
                                       reader=None, foreign_sleeper: bool = False) -> dict:
        """The race scenario with the parent's scheduling made injectable: the writer holds the claim, the direct read raises, and read_claim_json waits it out.

        The test's outcome must not depend on the parent being scheduled before any deadline, so the parent never releases the writer: the reader's own first
        retry sleep does, synchronously, on the reader's thread. The hold has no timer, so a pause before the direct read cannot end it, and a pause after the
        reader starts cannot make the release late against the reader's budget. pause_before_direct_s / pause_after_start_s are real parent pauses (the stress
        test passes values longer than reader_timeout_s to stand in for a descheduled runner). reader is the function under test (default read_claim_json);
        foreign_sleeper runs a time.sleep on another thread while the spy is installed and before the reader's first read, which must not count as the reader's
        retry. Returns the reader's {"body"|"error"} plus "retried", "held_at_retry", "reader_ident", "sleeps" ((thread ident, hold still held) per sleep the
        spy acted on) and "reads" (the reader thread's read_text outcomes in order: an exception's class name, or "ok")."""
        real_sleep = time.sleep
        real_read_text = Path.read_text
        reader_fn = read_claim_json if reader is None else reader
        hold = ExclusiveHold(self.claim_path)
        out: dict = {"held_at_retry": None, "retried": False, "reader_ident": None, "sleeps": [], "reads": []}
        reader_thread = None
        try:
            real_sleep(pause_before_direct_s)
            with self.assertRaises(PermissionError):
                self.claim_path.read_text(encoding="utf-8")

            def spy_sleep(s: float) -> None:
                # Runs on the reader's thread at its first retry: the file is still held here, and the release is this thread's own, so no parent step sits between
                # the reader's PermissionError and the file becoming readable.
                # time.sleep is patched process-wide, so only the reader's own thread may touch the scenario state: any other caller (a foreign thread, a timer)
                # goes straight to the real sleep (sol r2 on fefea29b).
                if threading.get_ident() != out["reader_ident"]:
                    real_sleep(s)
                    return
                out["sleeps"].append((threading.get_ident(), not hold.closed))
                if not out["retried"]:
                    out["held_at_retry"] = not hold.closed
                    out["retried"] = True
                    hold.release()
                real_sleep(s)

            def spy_read_text(path_self, *a, **k):
                if threading.get_ident() != out["reader_ident"]:
                    return real_read_text(path_self, *a, **k)
                try:
                    text = real_read_text(path_self, *a, **k)
                except BaseException as e:
                    out["reads"].append(type(e).__name__)
                    raise
                out["reads"].append("ok")
                return text

            def read() -> None:
                out["reader_ident"] = threading.get_ident()
                try:
                    out["body"] = reader_fn(self.claim_path, timeout_s=reader_timeout_s)
                except BaseException as e:   # surfaced on the test thread below
                    out["error"] = e

            reader_thread = threading.Thread(target=read, daemon=True)
            with mock.patch.object(time, "sleep", spy_sleep), mock.patch.object(Path, "read_text", spy_read_text):
                if foreign_sleeper:
                    foreign = threading.Thread(target=lambda: time.sleep(0.001), daemon=True)
                    foreign.start()
                    foreign.join(30)
                reader_thread.start()
                real_sleep(pause_after_start_s)
                reader_thread.join(reader_timeout_s + 30)
        finally:
            hold.release()   # a test that died above must not leave the claim held
            if reader_thread is not None:
                reader_thread.join(5)
        self.assertFalse(reader_thread.is_alive(), "the reader returned")
        self.assertTrue(hold.closed, "the writer's hold ended")
        return out

    def _assert_reader_waited_out(self, out: dict, body: dict) -> None:
        """The scenario's verdict: the reader itself met the held file (a PermissionError read, then a sleep on its own thread with the hold still up) and then
        returned the claim. A sleep on any other thread proves nothing about the reader (sol r2 on fefea29b)."""
        mine = [held for ident, held in out["sleeps"] if ident == out["reader_ident"]]
        self.assertTrue(out["retried"] and out["held_at_retry"], f"the reader hit PermissionError and went to sleep while the writer still held the file: {out}")
        self.assertTrue(mine and mine[0], f"the reader's own first sleep came while the writer held the file: {out}")
        self.assertEqual(out["reads"][:1], ["PermissionError"], f"the reader's first read was a locked read: {out}")
        self.assertEqual(out["reads"][-1:], ["ok"], f"the reader's last read succeeded: {out}")
        self.assertEqual({k: out[k] for k in ("body", "error") if k in out}, {"body": body}, f"returns the full JSON once the writer releases: {out}")

    def test_the_claim_reader_waits_out_a_writer_that_holds_the_file(self) -> None:
        # The race the merge_group flake hit: a reader that lands while the runner holds the claim (FileShare.None on the take or a renewal) got PermissionError;
        # one that lands on a half-written or not yet created file gets JSONDecodeError or FileNotFoundError. The direct read raises on each; read_claim_json waits.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        full = json.dumps(body)
        self.claim_path.write_text(full, encoding="utf-8")

        self._assert_reader_waited_out(self._read_claim_while_writer_holds(reader_timeout_s=120.0), body)

        self.claim_path.write_text('{"schema": "mlv-app/ven', encoding="utf-8")   # a half-written claim
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.claim_path.read_text(encoding="utf-8"))
        threading.Timer(1.0, lambda: self.claim_path.write_text(full, encoding="utf-8")).start()
        self.assertEqual(read_claim_json(self.claim_path), body)

        self.claim_path.unlink()   # not created yet
        with self.assertRaises(FileNotFoundError):
            self.claim_path.read_text(encoding="utf-8")
        threading.Timer(1.0, lambda: self.claim_path.write_text(full, encoding="utf-8")).start()
        self.assertEqual(read_claim_json(self.claim_path), body)

        self.claim_path.write_text("not json at all", encoding="utf-8")   # a claim that never becomes valid: bounded, then the last error with the path
        with self.assertRaises(json.JSONDecodeError) as cm:
            read_claim_json(self.claim_path, timeout_s=0.5, sleep_s=0.1)
        self.assertIn(str(self.claim_path), str(cm.exception))

    def test_the_claim_reader_scenario_survives_a_descheduled_parent(self) -> None:
        # sol r1 on effee436: a parent paused longer than the reader's budget after the reader's first retry failed a valid test (the parent owned the release,
        # the reader's own deadline kept running), and a pause longer than the hold's 60 s wait before the direct read let the hold lapse. Scaled down: a 2 s
        # reader budget and 3 s parent pauses at both points. The outcome must be the same as with no pause.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        self.claim_path.write_text(json.dumps(body), encoding="utf-8")
        self._assert_reader_waited_out(self._read_claim_while_writer_holds(reader_timeout_s=2.0, pause_after_start_s=3.0, pause_before_direct_s=3.0), body)

    def test_the_claim_reader_scenario_ignores_a_foreign_sleeper(self) -> None:
        # sol r2 on fefea29b: the sleep spy was process-wide, so another thread's time.sleep set retried/held_at_retry and released the hold before the reader's
        # first read; the reader then never met a locked file and the scenario still passed. A foreign sleep, before the reader starts, must change nothing.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        self.claim_path.write_text(json.dumps(body), encoding="utf-8")
        self._assert_reader_waited_out(self._read_claim_while_writer_holds(reader_timeout_s=30.0, foreign_sleeper=True), body)

    def test_the_claim_reader_scenario_rejects_a_reader_that_does_not_retry(self) -> None:
        # The mutation the scenario exists to catch: a reader that reads once. It must fail the scenario's verdict with and without a foreign sleeper.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        self.claim_path.write_text(json.dumps(body), encoding="utf-8")

        def read_once(path: Path, timeout_s: float = 0.0) -> dict:
            return json.loads(path.read_text(encoding="utf-8"))

        for foreign in (False, True):
            with self.subTest(foreign_sleeper=foreign):
                out = self._read_claim_while_writer_holds(reader_timeout_s=30.0, reader=read_once, foreign_sleeper=foreign)
                with self.assertRaises(AssertionError):
                    self._assert_reader_waited_out(out, body)

    def test_exclusive_hold_release_reports_a_close_handle_failure(self) -> None:
        # CloseHandle returns 0 on failure; the hold may then still be up while release() claimed it was over.
        target = self.case / "held.txt"
        target.write_text("x", encoding="utf-8")
        hold = ExclusiveHold(target)
        real_k32 = hold._k32

        class FailingK32:
            calls = 0

            def CloseHandle(self, handle) -> int:
                FailingK32.calls += 1
                return 0

        try:
            hold._k32 = FailingK32()
            with self.assertRaises(OSError):
                hold.release()
            self.assertFalse(hold.closed, "a failed CloseHandle is not a closed hold")
            self.assertEqual(FailingK32.calls, 1)
        finally:
            hold._k32 = real_k32
            hold.release()   # really close it so the file can be removed
        self.assertTrue(hold.closed)

    def test_the_flaky_test_reads_its_claims_through_read_claim_json(self) -> None:
        # CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-HARDEN-1 (b): read_claim_json is only a fix while the flaky test calls it. Reverting its c0/c1 reads to
        # json.loads(path.read_text()) stayed green, so pin the call site by source: c0 and c1 are assigned from read_claim_json and the method has no raw read_text.
        name = "test_a_long_foreign_refusal_wait_does_not_let_the_live_claim_expire"
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        fns = [n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == type(self).__name__ for n in c.body if isinstance(n, ast.FunctionDef) and n.name == name]
        self.assertEqual(len(fns), 1, f"{name} was renamed or removed: re-point this pin at the test that reads the live claim")
        fn = fns[0]
        via_helper = {t.id for a in ast.walk(fn) if isinstance(a, ast.Assign) and isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name) and a.value.func.id == "read_claim_json"
                      for t in a.targets if isinstance(t, ast.Name)}
        self.assertEqual(via_helper, {"c0", "c1"}, "c0 and c1 are read with read_claim_json")
        raw = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "read_text"]
        self.assertEqual(raw, [], "no raw read_text in the flaky test: a claim read there races the runner's FileShare.None write")

    # ---------------------------------------------------------------- r5: the claim is renewed for as long as it is held (dot 242171b3 B1)
    def test_a_long_foreign_refusal_wait_does_not_let_the_live_claim_expire(self) -> None:
        # -ClaimTtlSec 60 (its floor), and um-run blocks 80 s on the open while the agent re-asks a refused (exit 75) request: longer than the claim's TTL. At
        # TTL + 10 s VenueChain.psm1's own Get-VenueClaim (Get-VenueClaimStaleReason) still finds the claim LIVE, with the same nonce and a later expiresUtc,
        # and no waiting chain could break it. RED (no renewal): the claim reads STALE reason=expired while its owner is alive and holding the venue.
        # r8: the open is released as soon as the TTL + 10 s checks are made (FAKE_OPEN_RELEASE; 120 s is only its bound), not slept out to 80 s. The 70 s horizon itself stays:
        # -ClaimTtlSec has a 60 s floor, and a claim that survives its TTL can only be seen after it.
        release = self.case / "open.release"
        release.unlink(missing_ok=True)
        args, e = self.batch_cmd([self.specs[0]], "-ClaimTtlSec", "60", FAKE_OPEN_SLEEP="120", FAKE_OPEN_RELEASE=str(release))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            end = time.monotonic() + 60
            while not self.claim_path.exists() and time.monotonic() < end:
                time.sleep(0.2)
            c0 = read_claim_json(self.claim_path)
            claimed = parse_utc(c0["claimedUtc"])
            while datetime.now(timezone.utc) < claimed + timedelta(seconds=70):
                time.sleep(0.5)
            self.assertIsNone(p.poll(), "the run is still in its open's wait at TTL + 10 s")
            script = f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n$c = Get-VenueClaim -Venue bachelor -ClaimDir '{psq(self.claims)}'\n[ordered]@{{ state = $c.state; reason = $c.reason; nonce = $c.claim.nonce }} | ConvertTo-Json -Compress\n"
            out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            seen = json.loads(out.stdout)
            self.assertEqual((seen["state"], seen["reason"], seen["nonce"]), ("LIVE", None, c0["nonce"]), seen)
            c1 = read_claim_json(self.claim_path)
            self.assertGreater(parse_utc(c1["expiresUtc"]), datetime.now(timezone.utc), c1)
            self.assertEqual({k: v for k, v in c1.items() if k != "expiresUtc"}, {k: v for k, v in c0.items() if k != "expiresUtc"}, "only expiresUtc changes")
            release.write_text("go", encoding="utf-8")
            so, se = p.communicate(timeout=300)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(p.returncode, 0, so + se)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsCounted"]), ("ALL_LEGS_DONE", 1))
        self.assertGreaterEqual(b["claimRenew"]["renews"], 4, b["claimRenew"])
        self.assertEqual((b["claimRenew"]["renewSec"], b["claimRenew"]["ttlSec"], b["claimRenew"]["lost"], b["claimRenew"]["errors"]), (15, 60, None, 0), b["claimRenew"])
        self.assertFalse(self.claim_path.exists(), "the claim is released at the end")

    def test_the_claim_renewal_never_takes_over_another_owners_claim(self) -> None:
        # Update-VenueClaim renews only the claim with its nonce: another owner's claim (a new one after a break) is NOT_OURS and left byte for byte.
        self.claims.mkdir()
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$c = Request-VenueClaim -Venue bachelor -RunDir 'x' -TtlSec 60 -ClaimDir '{psq(self.claims)}'\n"
            f"$before = [IO.File]::ReadAllText('{psq(self.claim_path)}')\n"
            f"$other = Update-VenueClaim -Venue bachelor -Nonce 'not-the-nonce' -TtlSec 600 -ClaimDir '{psq(self.claims)}'\n"
            f"$same = ([IO.File]::ReadAllText('{psq(self.claim_path)}') -ceq $before)\n"
            f"$mine = Update-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -TtlSec 600 -ClaimDir '{psq(self.claims)}'\n"
            f"$after = Get-VenueClaim -Venue bachelor -ClaimDir '{psq(self.claims)}'\n"
            f"$rel = Release-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -ClaimDir '{psq(self.claims)}'\n"
            f"$gone = Update-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -TtlSec 600 -ClaimDir '{psq(self.claims)}'\n"
            "[ordered]@{ other = $other.state; same = $same; mine = $mine.state; live = $after.state; ttlLeft = [int](($after.claim.expiresUtc.ToUniversalTime() - [DateTime]::UtcNow).TotalSeconds); rel = $rel; gone = $gone.state } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual((got["other"], got["same"], got["mine"], got["live"], got["rel"], got["gone"]), ("NOT_OURS", True, "RENEWED", "LIVE", "RELEASED", "ABSENT"), got)
        self.assertGreater(got["ttlLeft"], 500, got)

    # ---------------------------------------------------------------- r5b: no sidecar, no open (dot ef5805a4 residual B1)
    def _deny_writes(self, folder: Path) -> None:
        """The first sidecar write fails: this user may not create files in the folder (a deny ACE). Removed again at cleanup."""
        who = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(["icacls", str(folder), "/deny", f"{who}:(OI)(CI)(WD,AD)"], check=True, capture_output=True, text=True, timeout=60)
        self.addCleanup(subprocess.run, ["icacls", str(folder), "/remove:d", who], capture_output=True, text=True, timeout=60)

    def _assert_no_open_submitted(self, r: subprocess.CompletedProcess, first: str) -> dict:
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"], b["leaseId"], b["openCancel"], b["closeJobId"], b["ledger"]), ("LIVENESS_UNAVAILABLE", "NOT_SUBMITTED", None, None, None, None), b)
        self.assertIn(f"first liveness write {first}", b["endDetail"])
        self.assertEqual(self.kinds(), [], "no open, close, probe or leg once the first sidecar write failed")
        self.assertEqual(list((self.share / "inbox").glob("*.job.ps1")) if (self.share / "inbox").exists() else [], [], "a job reached the inbox")
        self.assertEqual(b["openLiveness"]["end"], "ABSENT", b["openLiveness"])
        self.assertEqual(b["legsAttempted"], 0)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["ran"], res["counted"], res["notRun"]), ("LIVENESS_UNAVAILABLE", 0, 0, 2))
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" BATCH_OPEN_LIVENESS batch=1 job={b['openJobId']} first={first} ", log)
        return b

    def test_r5b_a_missing_notafter_folder_submits_no_open(self) -> None:
        # RED on 3da8db54: the open is submitted with no sidecar (the run goes on to its legs, exit 0).
        (self.share / "notafter").rmdir()
        b = self._assert_no_open_submitted(self.run_batch(self.specs[:2]), "NO_NOTAFTER_DIR")
        self.assertIn("no notafter folder", b["endDetail"])

    def test_r5b_a_failed_first_sidecar_write_submits_no_open(self) -> None:
        # The folder is there but the write is denied: UNKNOWN. RED on 3da8db54 twice over: the writer said WRITTEN for a denied write (a .NET exception in
        # try/finally with no catch ran on to '<WRITTEN>'), and the open was submitted anyway.
        self._deny_writes(self.share / "notafter")
        b = self._assert_no_open_submitted(self.run_batch(self.specs[:2]), "UNKNOWN")
        self.assertIn("denied", b["endDetail"])

    def test_r5b_a_runner_killed_right_after_a_failed_first_write_leaves_no_open_in_the_inbox(self) -> None:
        # The early-death case: the first write is denied, and the runner is killed (process tree) 2 s after it logs that write, while an open it had
        # submitted would be blocked in um-run (FAKE_OPEN_SLEEP). No OPEN job file is ever in the agent's inbox. RED on 3da8db54: the open is in the inbox.
        self._deny_writes(self.share / "notafter")
        args, e = self.batch_cmd([self.specs[0]], FAKE_OPEN_SLEEP="200")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            log = self.run_dir / "batch.log"
            end = time.monotonic() + 60
            while time.monotonic() < end and not (log.exists() and " BATCH_OPEN_LIVENESS " in log.read_text(encoding="utf-8")):
                time.sleep(0.1)
            time.sleep(2)
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
            p.wait(timeout=60)
            time.sleep(2)   # anything in flight at the kill lands, or not
            inbox = self.share / "inbox"
            self.assertEqual(sorted(f.name for f in inbox.glob("lease-batch-open-*")) if inbox.exists() else [], [], "an OPEN reached the inbox after a failed first write")
            self.assertNotIn("OPEN:", " ".join(self.kinds()))
            self.assertIn(" first=UNKNOWN ", log.read_text(encoding="utf-8"))
            self.assertFalse(_pid_alive(p.pid))
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()

    # ---------------------------------------------------------------- r6: the first sidecar write is read back with the agent's own parser (Grok G2 on #633)
    def _mirror_with_bad_sidecar(self, json_patch: str = "", writer_patch: tuple[str, str] | None = None,
                                 module_patches: tuple[tuple[str, str], ...] = (), agent_patches: tuple[tuple[str, str], ...] = (),
                                 runner_patches: tuple[tuple[str, str], ...] = ()) -> None:
        """A copy of tools/profiling/{dual-venue,bachelor/ultra-magnus-agent.ps1} in which the runner's sidecar writer still says WRITTEN but writes bytes the agent
        would not read as live. json_patch is PowerShell run on $json before it is written; writer_patch replaces a statement of the write itself. The runner under test
        is the copy's own Invoke-VenueLeaseBatch.ps1 (self.runner), reading the agent's functions from the copy's bachelor folder."""
        tree = self.case / "tree" / "tools" / "profiling"
        shutil.copytree(DV, tree / "dual-venue", ignore=shutil.ignore_patterns("look-receipts", "legs", "legsets"))
        (tree / "bachelor").mkdir()
        shutil.copy2(AGENT, tree / "bachelor" / AGENT.name)
        # VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r5 (CI run 43580040634): and every neighbouring-folder module the dual-venue modules read (..\bachelor\AttrCudaArtifacts.psm1)
        copy_cross_tree_modules(MODULE, tree / "dual-venue")
        if agent_patches:
            agent_copy = tree / "bachelor" / AGENT.name
            agent_text = agent_copy.read_text(encoding="utf-8")
            for old, new in agent_patches:
                self.assertEqual(agent_text.count(old), 1, "the agent line moved: update this fixture")
                agent_text = agent_text.replace(old, new)
            agent_copy.write_text(agent_text, encoding="utf-8")
        mod = tree / "dual-venue" / "VenueChain.psm1"
        text = mod.read_text(encoding="utf-8")
        for old, new in module_patches:
            self.assertEqual(text.count(old), 1, "the module line moved: update this fixture")
            text = text.replace(old, new)
        anchor = "[pscustomobject]@{ path = ($AgentShare.TrimEnd('\\') + '\\notafter\\' + $JobId + '.json'); json = $json }"
        self.assertEqual(text.count(anchor), 1, "the sidecar write argument moved: update this fixture")
        text = text.replace(anchor, (json_patch + "\n    " if json_patch else "") + anchor)
        if writer_patch:
            self.assertEqual(text.count(writer_patch[0]), 1, "the sidecar write moved: update this fixture")
            text = text.replace(writer_patch[0], writer_patch[1])
        mod.write_text(text, encoding="utf-8")
        self.runner = tree / "dual-venue" / RUNNER.name
        if runner_patches:
            run_text = self.runner.read_text(encoding="utf-8")
            for old, new in runner_patches:
                self.assertEqual(run_text.count(old), 1, "the runner line moved: update this fixture")
                run_text = run_text.replace(old, new)
            self.runner.write_text(run_text, encoding="utf-8")

    def _assert_readback_refused(self, r: subprocess.CompletedProcess, want_state: str, end_reason: str = "LIVENESS_UNAVAILABLE", code: int = 9,
                                 detail: str = "agent's own parser does not read it as live") -> dict:
        """The first write said WRITTEN, the agent's parser disagrees: the open is never submitted and the batch ends exactly as a failed write ends it
        (or, r2, DEADLINE exit 3 when the deadline crossed under a live sidecar). Nothing reaches Submit-AgentJob: the fake um-run's call log and the inbox stay empty."""
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"], b["leaseId"], b["openCancel"], b["closeJobId"], b["ledger"]), (end_reason, "NOT_SUBMITTED", None, None, None, None), b)
        self.assertEqual(self.kinds(), [], "no open, close, probe or leg once the read-back was not live")
        self.assertFalse([l for l in (self.events_raw() if self.events.exists() else []) if " UMRUN " in l], "the fake um-run was called")
        self.assertEqual(list((self.share / "inbox").glob("*.job.ps1")) if (self.share / "inbox").exists() else [], [], "a job reached the inbox")
        self.assertIn("first liveness write WRITTEN", b["endDetail"])
        self.assertIn(detail, b["endDetail"])
        rb = b["openLivenessReadback"]
        self.assertEqual((rb["live"], rb["livenessState"]), (False, want_state), rb)
        self.assertTrue(rb["why"], rb)
        self.assertIn(rb["why"], b["endDetail"])
        self.assertEqual(list((self.share / "notafter").glob("lease-batch-open-*")), [], "the refused sidecar was left behind")
        self.assertEqual(b["legsAttempted"], 0)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["ran"], res["counted"], res["notRun"]), (end_reason, 0, 0, 2))
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" BATCH_OPEN_LIVENESS batch=1 job={b['openJobId']} first=WRITTEN ", log)
        self.assertIn(f" BATCH_OPEN_LIVENESS_READBACK batch=1 job={b['openJobId']} live=False livenessState={want_state} ", log)
        return b

    # (what the bytes are, PowerShell that makes them, the agent's livenessState for them; the agent lines are ultra-magnus-agent.ps1 Get-JobNotAfterVerdict / Read-JobNotAfterSidecar)
    BAD_SIDECARS = (
        ("no aliveUntilUtc (:803)", "$json = $json -replace '\"aliveUntilUtc\":\"[^\"]*\",', ''", "unparseable"),
        ("aliveUntilUtc that is no time (:803)", "$json = $json -replace '\"aliveUntilUtc\":\"[^\"]*\"', '\"aliveUntilUtc\":\"not a time\"'", "unparseable"),
        ("aliveUntilUtc already passed (:815)", "$json = $json -replace '\"aliveUntilUtc\":\"[^\"]*\"', '\"aliveUntilUtc\":\"2001-01-01T00:00:00.0000000Z\"'", "lapsed"),
        ("another job's id (:799)", "$json = $json -replace '\"jobId\":\"[^\"]*\"', '\"jobId\":\"lease-batch-open-someone-else\"'", "unparseable"),
        ("another schema (:798)", "$json = $json -replace 'mlv-app/job-notafter-sidecar/v1', 'mlv-app/job-notafter-sidecar/v0'", "unparseable"),
        ("cut-off JSON (:791)", "$json = $json.Substring(0, 40)", "unparseable"),
        ("an array root (:790)", "$json = '[' + $json + ']'", "unparseable"),
        ("over 4096 bytes (:858)", "$json = $json + (' ' * 5000)", "unparseable"),
    )

    def test_r6_a_written_sidecar_the_agent_would_not_read_as_live_submits_no_open(self) -> None:
        # Grok G2 on #633: the first write reports WRITTEN but the agent would parse the bytes as absent / unparseable / lapsed. RED on 13d65263: the open is
        # submitted (the batch runs on to its legs, exit 0), then refused or lapsed on the agent.
        for label, patch, want in self.BAD_SIDECARS:
            with self.subTest(label):
                self.setUp()   # a fresh share, claim dir and run dir per case
                self._mirror_with_bad_sidecar(json_patch=patch)
                self._assert_readback_refused(self.run_batch(self.specs[:2]), want)

    def test_r6_a_sidecar_that_vanishes_after_a_written_write_submits_no_open(self) -> None:
        # The write returned WRITTEN and the file is gone when read back: the agent reads no sidecar (absent), so the open would have no liveness bound at all.
        self._mirror_with_bad_sidecar(writer_patch=("[IO.File]::Move($tmp, $A.path, $true)", "[IO.File]::Move($tmp, $A.path, $true); Remove-Item -LiteralPath $A.path -Force"))
        self._assert_readback_refused(self.run_batch(self.specs[:2]), "absent")

    def test_r6_the_mirror_with_an_unchanged_writer_still_submits_the_open(self) -> None:
        # Control: the copied tree is faithful, so the refusals above come from the bytes, not from the copy. A live sidecar is read back live and the batch runs.
        self._mirror_with_bad_sidecar()
        r = self.run_batch(self.specs[:2])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"], b["legsCounted"]), ("ALL_LEGS_DONE", "RECEIPT", 2), b)
        self.assertEqual((b["openLivenessReadback"]["live"], b["openLivenessReadback"]["livenessState"]), (True, "live"), b["openLivenessReadback"])
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1)

    def test_r6_the_readback_judges_with_the_agents_functions_and_fails_closed_without_them(self) -> None:
        # Test-VenueJobLivenessReadback lifts Get-JobNotAfterVerdict and Read-JobNotAfterSidecar from the agent script. An agent script that lacks them, or is
        # missing, gives a not-live answer rather than a guess.
        empty = self.case / "no-fns.ps1"
        empty.write_text("function Something-Else { 1 }\n", encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "[ordered]@{\n"
            f"  noFns = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1' -AgentScript '{psq(empty)}'\n"
            f"  noFile = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1' -AgentScript '{psq(self.case / 'missing.ps1')}'\n"
            f"  noSidecar = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1'\n"
            "} | ConvertTo-Json -Compress -Depth 4\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual((got["noFns"]["live"], got["noFile"]["live"], got["noSidecar"]["live"]), (False, False, False), got)
        self.assertIn("does not define both", got["noFns"]["error"])
        self.assertEqual((got["noSidecar"]["livenessState"], got["noSidecar"]["error"]), ("absent", None), got["noSidecar"])

    # ---------------------------------------------------------------- r2: the three regression cases hub ACK 6394063078 promised, the deadline typing, the agent-script sha
    def test_r2_a_stalled_readback_worker_is_not_live_and_submits_no_open(self) -> None:
        # (a) VenueChain.psm1 Invoke-VenueBoundedRead: the worker reading the sidecar back never answers (the lifted agent reader sleeps 120 s; the read-back's bound is
        # patched down to 2 s so the case stays fast). The bound expires -> not live (UNKNOWN) -> no Submit-AgentJob, exit 9 LIVENESS_UNAVAILABLE, keeper stopped, sidecar removed.
        # RED on 13d65263 (no read-back: the open is submitted, exit 0).
        self._mirror_with_bad_sidecar(
            module_patches=(("[string]$AgentScript = '', [ValidateRange(1, 600)][int]$TimeoutSec = 30)", "[string]$AgentScript = '', [ValidateRange(1, 600)][int]$TimeoutSec = 2)"),),
            agent_patches=(('$path = Join-Path $notAfterDir "$JobId.json"', '$path = Join-Path $notAfterDir "$JobId.json"; Start-Sleep -Seconds 120'),))
        started = time.monotonic()
        r = self.run_batch(self.specs[:2])
        elapsed = time.monotonic() - started
        b = self._assert_readback_refused(r, "UNKNOWN")
        self.assertEqual(b["openLivenessReadback"]["state"], "UNKNOWN")
        self.assertIn("no answer in 2 s", b["openLivenessReadback"]["why"])
        self.assertLess(elapsed, 100, "the abandoned worker held the runner (it sleeps 120 s)")

    def test_r2_the_readback_helper_gives_up_on_a_stalled_worker_within_its_timeout(self) -> None:
        # The helper on its own: an agent script whose reader never returns, -TimeoutSec 1 -> not live, UNKNOWN, 'no answer', in about a second (not the reader's 120 s).
        stall = self.case / "stalled-agent.ps1"
        stall.write_text("function Get-JobNotAfterVerdict { param($Metadata, $Sidecar) 1 }\nfunction Read-JobNotAfterSidecar { param($JobId) Start-Sleep -Seconds 120 }\n", encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$t = [Diagnostics.Stopwatch]::StartNew(); $r = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1' -AgentScript '{psq(stall)}' -TimeoutSec 1; $t.Stop()\n"
            "[ordered]@{ live = $r.live; state = $r.state; livenessState = $r.livenessState; error = $r.error; ms = $t.ElapsedMilliseconds } | ConvertTo-Json -Compress\n"
            "Complete-VenueProcess -ExitCode 0\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=100)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((got["live"], got["state"], got["livenessState"]), (False, "UNKNOWN", "UNKNOWN"), got)
        self.assertIn("no answer in 1 s", got["error"])
        self.assertLess(got["ms"], 20000, got)

    def test_r2_a_sidecar_holding_invalid_utf8_is_not_live_and_submits_no_open(self) -> None:
        # (b) ultra-magnus-agent.ps1 Read-JobNotAfterSidecar :859 reads with a strict UTF-8 decoder, so bytes that are not UTF-8 are 'unreadable' and the verdict
        # fails closed (:782). The writer still says WRITTEN; the file then holds 0xFF 0xFE bytes. -> not live, refuse, no open. RED on 13d65263 (the open is submitted).
        bad = "[IO.File]::Move($tmp, $A.path, $true); [IO.File]::WriteAllBytes($A.path, [byte[]](0x7B, 0x22, 0xFF, 0xFE, 0x22, 0x7D))"
        self._mirror_with_bad_sidecar(writer_patch=("[IO.File]::Move($tmp, $A.path, $true)", bad))
        b = self._assert_readback_refused(self.run_batch(self.specs[:2]), "unparseable")
        self.assertIn("could not be read", b["openLivenessReadback"]["why"])

    def test_r2_a_deadline_crossed_under_a_live_sidecar_ends_deadline_and_submits_no_open(self) -> None:
        # (c) The runner's own deadline check (Invoke-VenueLeaseBatch.ps1 :349) passed, then the deadline crossed before the read-back: the agent's verdict is
        # state=passed with livenessState=live (ultra-magnus-agent.ps1 :818-825). Simulated by handing the read-back a notAfterUtc 5 s in the past, the sidecar live.
        # r1 typed this LIVENESS_UNAVAILABLE exit 9 with "(read-back live)"; r2 types it DEADLINE exit 3 (the runner's deadline code) and the open is still never submitted.
        anchor = "    $ast = [System.Management.Automation.Language.Parser]::ParseFile($AgentScript, [ref]$null, [ref]$null)"
        self._mirror_with_bad_sidecar(module_patches=((anchor, "    $NotAfterUtc = [DateTime]::UtcNow.AddSeconds(-5).ToString('o')\n" + anchor),))
        b = self._assert_readback_refused(self.run_batch(self.specs[:2]), "live", end_reason="DEADLINE", code=3, detail="the batch deadline passed first")
        rb = b["openLivenessReadback"]
        self.assertEqual((rb["state"], rb["livenessState"], rb["live"]), ("passed", "live", False), rb)
        self.assertNotIn("does not read it as live", b["endDetail"])

    def test_r2_the_readback_record_carries_the_agent_scripts_sha256(self) -> None:
        # Opus H3: the read-back judges with the checkout's agent script; its sha256 in openLivenessReadback makes a later drift from the deployed agent visible.
        import hashlib
        self._mirror_with_bad_sidecar()
        r = self.run_batch(self.specs[:2])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        sha = b["openLivenessReadback"]["agentScriptSha256"]
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertEqual(sha, hashlib.sha256(AGENT.read_bytes()).hexdigest(), "the mirror's agent script is the checkout's, byte for byte")

    # ---------------------------------------------------------------- r3: a read-back that THROWS before the open is submitted is not a sent open (Dot B1 on 46768a21)
    def test_r3_a_readback_that_throws_before_the_open_is_submitted_cancels_nothing_closes_nothing_and_charges_nothing(self) -> None:
        # VenueChain.psm1 Test-VenueJobLivenessReadback: Test-Path on the agent script succeeds, then the hash (Get-FileHash) or the parse (ParseFile) throws because
        # the file is locked or vanished. RED on 46768a21: the exception left $op null, the finally block read a null $op as "maybe sent", requested a cancel, submitted
        # a close and wrote a full-LeaseCapSec open-unreconciled ledger row for an open that was never submitted, and the exception then escaped past the summary.
        # (Parser::ParseFile itself does not throw on a missing file: it returns an error AST, which the helper already treats as "functions not defined".) Case 1: the
        # path Get-FileHash reads is swapped for one that does not exist, so the .NET call really throws. Case 2: a simulated throw where the helper reads the script.
        # The batch ends typed, with its summary.
        hash_old = "(Get-FileHash -LiteralPath $AgentScript -Algorithm SHA256)"
        read_old = "    $ast = [System.Management.Automation.Language.Parser]::ParseFile($AgentScript, [ref]$null, [ref]$null)"
        for label, old, new in (
            ("the hash throws", hash_old, "(Get-FileHash -LiteralPath ($AgentScript + '.vanished') -Algorithm SHA256)"),
            ("the read throws", read_old, "    throw 'vanished: simulated read failure, the agent script is locked'\n" + read_old),
        ):
            with self.subTest(label):
                self.setUp()   # a fresh share, claim dir and run dir per case
                self._mirror_with_bad_sidecar(module_patches=((old, new),))
                r = self.run_batch(self.specs[:2], "-CancelReconcileSec", "3")
                b = self._assert_readback_refused(r, "UNKNOWN")
                self.assertIn("vanished", b["openLivenessReadback"]["why"], b["openLivenessReadback"])
                self.assertEqual(list((self.share / "cancel").iterdir()), [], "a cancel was requested for an open that was never submitted")
                self.assertFalse((self.claims / "bachelor.lease-ledger.jsonl").exists(), "the ledger was charged for an open that was never submitted")
                self.assertIn("LEASE_BATCH_RESULT=", r.stdout, "the original exception escaped past the batch summary")
                log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
                self.assertNotIn(" BATCH_OPEN_CANCEL ", log)
                self.assertNotIn(" BATCH_OPEN_RECONCILED ", log)

    def test_r3_a_submit_that_throws_is_an_uncertain_open_and_keeps_the_conservative_cancel_close_and_ledger_row(self) -> None:
        # Control for the case above: here the open IS (maybe) submitted. Submit-AgentJob throws for the open job before it reports anything; the
        # submitted flag, not $op, tells the finally block the open may be in the agent's gate: cancel requested, close submitted, open-unreconciled row charged.
        # r16 (sol r15 B1; hub ruling 08:18:44Z): "submitted" now starts when um-run.ps1 is called (a job file never written reached no one), so the throw is placed there.
        anchor = "        $script:umRunCalled[$JobId] = $true"
        self._mirror_with_bad_sidecar(runner_patches=((anchor, anchor + "\n        if ($JobId -like 'lease-batch-open-*') { throw 'simulated submit failure' }"),))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=30)), "-CancelReconcileSec", "3")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"), b)
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists(), "the cancel request stays for the agent")
        self.assertTrue(b["closeJobId"], "the close was not submitted")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled", b["ledger"])
        self.assertTrue((self.claims / "bachelor.lease-ledger.jsonl").exists())

    # ---------------------------------------------------------------- r16 (sol r15 B1): an open whose job file was never complete here never reached um-run
    # The local File.Open(CreateNew) of the open's job file, or the write into it, fails before um-run is called. RED on 5c73c842: $openSubmitted was already true, so
    # the finally requested a cancel, submitted a close, charged a full 2700 s open-unreconciled lease and ended OPEN_CANCEL_UNRECONCILED (exit 11).
    OPEN_JOB_CREATE = "$fs = [IO.File]::Open($f, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)"
    OPEN_JOB_WRITE = "$fs.Write($jb, 0, $jb.Length)"

    def _assert_open_never_submitted(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual(res["stopReason"], "OPEN_JOB_WRITE_FAILED", res)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"]), ("OPEN_JOB_WRITE_FAILED", "JOB_WRITE_FAILED"), b)
        self.assertEqual(self.kinds(), [], "um-run was called for the open or a close, or a probe or a leg ran")
        self.assertEqual(list((self.share / "cancel").iterdir()), [], "a cancel was requested for an open that never reached um-run")
        self.assertEqual((b["openCancel"], b["closeJobId"], b["ledger"]), (None, None, None), b)
        self.assertFalse((self.claims / "bachelor.lease-ledger.jsonl").exists(), "the lease budget was charged for an open that never reached um-run")
        self.assertEqual(list((self.share / "notafter").iterdir()), [], "the open's liveness sidecar was left behind")
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r16_b1_an_open_job_file_that_cannot_be_created_is_never_submitted(self) -> None:
        seam = "if ($JobId -like 'lease-batch-open-*') { throw [IO.IOException]::new(\"simulated: the file '$f' exists\") }; " + self.OPEN_JOB_CREATE
        self._mirror_with_bad_sidecar(runner_patches=((self.OPEN_JOB_CREATE, seam),))
        self._assert_open_never_submitted(self.run_batch([self.specs[0]], "-CancelReconcileSec", "3"))

    def test_r16_b1_an_open_job_file_whose_write_fails_is_never_submitted(self) -> None:
        seam = "if ($JobId -like 'lease-batch-open-*') { throw [IO.IOException]::new('simulated: there is not enough space on the disk') }; " + self.OPEN_JOB_WRITE
        self._mirror_with_bad_sidecar(runner_patches=((self.OPEN_JOB_WRITE, seam),))
        self._assert_open_never_submitted(self.run_batch([self.specs[0]], "-CancelReconcileSec", "3"))

    def test_r16_b1_a_job_folder_that_refuses_new_files_submits_no_open(self) -> None:
        # the real failure, no seam: <RunDir>\batch-jobs denies this user "create files" (the caps probe fails there too and reads the fallback caps)
        jobs = self.run_dir / "batch-jobs"
        jobs.mkdir(parents=True)
        user = subprocess.run(["whoami"], capture_output=True, text=True, timeout=60).stdout.strip()
        deny = subprocess.run(["icacls", str(jobs), "/deny", f"{user}:(WD)"], capture_output=True, text=True, timeout=60)
        if deny.returncode != 0:
            self.skipTest(f"icacls could not deny file creation: {deny.stdout} {deny.stderr}")
        try:
            r = self.run_batch([self.specs[0]], "-CancelReconcileSec", "3")
        finally:
            subprocess.run(["icacls", str(jobs), "/remove:d", user], capture_output=True, text=True, timeout=60)
        self._assert_open_never_submitted(r)

    def test_r16_b1_control_an_open_that_reached_um_run_and_stays_unreconciled_is_still_charged_a_full_lease(self) -> None:
        # the hub's control (ruling 08:18:44Z): um-run WAS called for the open and gave up while the agent still holds it in its gate, and nothing answers the cancel
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)), "-CancelReconcileSec", "3", FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        self.assertEqual(batch_result(r.stdout)["stopReason"], "OPEN_CANCEL_UNRECONCILED")
        [b] = self.batches()
        self.assertTrue([k for k in self.kinds() if k.startswith("OPEN:")], "um-run was not called for the open")
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"), b)
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists())
        self.assertTrue(b["closeJobId"], "the close was not submitted")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled", b["ledger"])
        span = datetime.fromisoformat(b["ledger"]["endUtc"].replace("Z", "+00:00")) - datetime.fromisoformat(b["ledger"]["startUtc"].replace("Z", "+00:00"))
        self.assertEqual(round(span.total_seconds()), 2700, b["ledger"])
        self.assertTrue((self.claims / "bachelor.lease-ledger.jsonl").exists())

    # ---------------------------------------------------------------- r5b: no probe or leg without the claim (Grok G1, 6389240991)
    def test_r5b_a_claim_taken_over_mid_batch_ends_the_batch_claim_lost_and_keeps_the_legs_already_run(self) -> None:
        # Leg 1 runs and counts. Leg 2's probe replaces the claim file with another owner's (NOT_OURS); the 1 s keeper sees it, and the check before leg 2
        # ends batch 1 CLAIM_LOST with leg 1 kept. Batch 2 breaks the dead owner's claim and runs leg 2. RED on 3da8db54: leg 2 runs in batch 1 with no claim.
        r = self.run_batch(self.specs[:2], "-ClaimRenewSec", "1", FAKE_QUIET_MODES="QUIET,STEAL,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["claimRenew"]["lost"]), ("CLAIM_LOST", 1, 1, "NOT_OURS"), b1)
        self.assertIn("claim=NOT_OURS", b1["endDetail"])
        self.assertTrue(b1["endDetail"].startswith("before leg 02-fx-b-spec:"), b1["endDetail"])
        self.assertEqual([(l["status"], l["counted"]) for l in b1["legs"]], [("RAN", True)], "the leg already run stays as measured")
        self.assertEqual((b2["endReason"], b2["legsCounted"]), ("ALL_LEGS_DONE", 1))
        legs = [k for k in self.kinds() if k.startswith("LEG:")]
        self.assertEqual(legs, [f"LEG:fx-a:{b1['token']}", f"LEG:fx-b:{b2['token']}"])
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(" CLAIM_LOST batch=1 leg=02-fx-b-spec phase=leg claim=NOT_OURS ", log)
        self.assertIn(" CLAIM_STALE_BROKEN venue=bachelor reason=dead-pid ", log)

    def test_r5b_a_claim_past_its_last_good_expiry_ends_the_batch_claim_lost(self) -> None:
        # -ClaimTtlSec 60, renewed every 20 s, but the claim file is read-only while um-run blocks 66 s on the open: every renewal comes back BUSY and the
        # last good expiry (the one the claim was taken with) passes. The check before the first probe ends batch 1 CLAIM_LOST with nothing probed or run;
        # batch 2 takes a fresh claim and runs the leg. RED on 3da8db54: batch 1 probes and runs the leg on an expired claim.
        r = self.run_batch([self.specs[0]], "-ClaimTtlSec", "60", "-ClaimRenewSec", "20", FAKE_OPEN_SLEEP="66,0", FAKE_OPEN_CLAIM_READONLY="1,0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsAttempted"], b1["probeRetries"], b1["claimRenew"]["renews"], b1["claimRenew"]["lost"]), ("CLAIM_LOST", 0, 0, 0, None), b1)
        self.assertIn("claim=EXPIRED", b1["endDetail"])
        self.assertIn("lastRenew=BUSY", b1["endDetail"])
        self.assertTrue(b1["endDetail"].startswith("before the probe of leg 01-fx-a-spec:"), b1["endDetail"])
        self.assertEqual((b2["endReason"], b2["legsCounted"]), ("ALL_LEGS_DONE", 1))
        k = self.kinds()
        self.assertEqual(k, [f"OPEN:{b1['token']}", f"CLOSE:{b1['token']}", f"OPEN:{b2['token']}", "QUIET:QUIET", f"LEG:fx-a:{b2['token']}", f"CLOSE:{b2['token']}"])

    # ---------------------------------------------------------------- r5b: an UNRESOLVED leg is withdrawn (hub 13:21Z B2 clause, dot 242171b3)
    def test_r5b_a_leg_with_no_agent_result_is_withdrawn_before_the_close(self) -> None:
        # um-run said UNRESOLVED for the leg's job (no result by name) and Invoke-VenueLeg exited 0 with its receipt. The leg is LEASE_LOST (uncounted) and its
        # job is WITHDRAWN: its cancel file is written before the batch's close job is submitted. RED on 3da8db54: no cancel file, the job is left in place.
        r = self.run_batch([self.specs[0]], FAKE_LEG_MODES="unresolved")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [leg] = b["legs"]
        self.assertEqual((leg["outcome"], leg["class"], leg["counted"], leg.get("withdraw"), b["endReason"]), ("UNRESOLVED", "LEASE_LOST", False, "REQUESTED", "LEASE_LOST"), leg)
        self.assertTrue((self.share / "cancel" / f"{leg['jobId']}.cancel").exists(), "the leg's job was left in place")
        [close] = (self.share / "fake-close-cancels.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{leg['jobId']}.cancel", close.split(" ", 1)[1], "the leg was withdrawn only after the close was submitted")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" LEG_WITHDRAWN leg=01-fx-a-spec batch=1 jobId={leg['jobId']} result=ABSENT printedOutcome=UNRESOLVED cancel=REQUESTED", log)

    def test_r5b_a_leg_with_its_result_is_never_withdrawn(self) -> None:
        # The guard: a leg whose result is in hand gets no cancel file.
        r = self.run_batch([self.specs[0]])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertNotIn("withdraw", b["legs"][0])
        self.assertEqual(list((self.share / "cancel").iterdir()), [])

    # ---------------------------------------------------------------- the job-id contract with the agent
    def test_invoke_venue_leg_tags_its_job_with_the_batch_token_the_agent_reads(self) -> None:
        leg = LEG.read_text(encoding="utf-8")
        self.assertIn("[Parameter(ParameterSetName = 'Leg')][ValidatePattern('^[0-9a-f]{12}$')][string]$LeaseBatch = ''", leg)
        self.assertIn('$jobStem = "dve-$($spec.legId)-$Venue-$Backend-$runStamp$(if ($LeaseBatch) { "-lb$LeaseBatch" })"', leg)
        self.assertIn('$probeJobId = "$jobStem-health"', leg)
        stem = "dve-m16-1243-display-windowed-s1-bachelor-cpu-20261023184200-lb0123456789ab"
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(AGENT)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Get-LeaseBatchToken', 'Test-LeaseBatchCloseJobId') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$p = ($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'GateExemptJobPattern' }).DefaultValue.Value\n"
            f"[ordered]@{{ leg = Get-LeaseBatchToken '{stem}'; health = Get-LeaseBatchToken '{stem}-health'; healthLight = ('{stem}-health' -match $p); legLight = ('{stem}' -match $p)\n"
            "  open = Get-LeaseBatchToken 'lease-batch-open-20261023T184200Z-lb0123456789ab'; close = Test-LeaseBatchCloseJobId 'lease-batch-close-20261023T184200Z-lb0123456789ab' } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got, {"leg": "0123456789ab", "health": None, "healthLight": True, "legLight": False, "open": "0123456789ab", "close": True})

    def test_the_runner_names_its_open_and_close_jobs_in_the_agent_shapes(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn('$b.openJobId = "lease-batch-open-$stamp-lb$token"', text)
        self.assertIn('$b.closeJobId = "lease-batch-close-$stamp-lb$token"', text)
        self.assertIn("'-LeaseBatch', $token", text)
        self.assertTrue(all(b < 128 for b in RUNNER.read_bytes()), "the runner stays ASCII-only")

    # ---------------------------------------------------------------- VenueChain.psm1 helpers
    def test_job_result_and_batch_receipt_are_read_by_name(self) -> None:
        (self.share / "outbox").mkdir()
        (self.share / "logs").mkdir()
        (self.share / "outbox" / "j1.result.json").write_text(json.dumps({"jobId": "j1", "admission": {"leaseBatch": {"leaseId": "L1"}}}), encoding="utf-8")
        (self.share / "logs" / "lease-batch-0123456789ab.jsonl").write_text(
            json.dumps({"token": "0123456789ab", "leaseId": "L0", "endReason": "idle"}) + "\n" + json.dumps({"token": "0123456789ab", "leaseId": "L1", "endReason": "closed"}) + "\n", encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'\n"
            "[ordered]@{ found = (Get-VenueJobResult -AgentShare $s -JobId 'j1').result.admission.leaseBatch.leaseId; absent = (Get-VenueJobResult -AgentShare $s -JobId 'j2').state\n"
            "  bad = (Get-VenueJobResult -AgentShare $s -JobId '..\\x').state; rec = (Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId 'L1').record.endReason\n"
            "  none = (Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId 'L9').state; nofile = (Read-VenueLeaseBatchReceipt -AgentShare $s -Token 'ffffffffffff' -LeaseId 'L1').state } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"found": "L1", "absent": "ABSENT", "bad": "UNKNOWN", "rec": "closed", "none": "ABSENT", "nofile": "ABSENT"})

    def test_r2_the_batch_receipt_line_is_bound_to_the_requested_token_and_lease(self) -> None:
        # sol key r1 BLOCKER (VenueChain.psm1 Read-VenueLeaseBatchReceipt): the first leaseId match won whatever its token. Fail closed: a token that is missing, not a string, another
        # token, or this token with another spelling (case, a soft hyphen) names no line of this batch; nor does a lease id that is not a string.
        (self.share / "logs").mkdir()
        t = "0123456789ab"
        lines = [
            {"token": "ffffffffffff", "leaseId": "L1", "endReason": "closed"}, {"token": t, "leaseId": "L1", "endReason": "idle"},
            {"leaseId": "L2", "endReason": "idle"}, {"token": None, "leaseId": "L3", "endReason": "idle"}, {"token": [t], "leaseId": "L4", "endReason": "idle"},
            {"token": 123456789012, "leaseId": "L5", "endReason": "idle"}, {"token": t.upper(), "leaseId": "L6", "endReason": "idle"},
            {"token": t + chr(0xAD), "leaseId": "L7", "endReason": "idle"}, {"token": t, "leaseId": ["L8"], "endReason": "idle"},
            {"token": "ffffffffffff", "leaseId": "L9", "endReason": "idle"},
        ]
        (self.share / "logs" / f"lease-batch-{t}.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'\n"
            f"$rd = {{ param($id) $x = Read-VenueLeaseBatchReceipt -AgentShare $s -Token '{t}' -LeaseId $id; \"$($x.state):$($x.record.endReason)\" }}\n"
            "$o = [ordered]@{}; foreach ($id in 'L1','L2','L3','L4','L5','L6','L7','L8','L9') { $o[$id] = & $rd $id }; $o | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        want = {"L1": "FOUND:idle"} | {f"L{i}": "ABSENT:" for i in range(2, 10)}
        self.assertEqual(json.loads(out.stdout), want)

    def test_r2_adversary_a1_a_non_string_identity_spelled_like_the_request_neither_answers_nor_hides_the_agents_line(self) -> None:
        # adversary r2 A1 on 92c08033 (adversary-92c08033.json): an all-digit token written as a JSON NUMBER became the text '123456789012' and matched the request, so a
        # closed line hid the agent's own idle line. Siblings: a numeric lease id 1 for -LeaseId '1', an array ["L1"] for -LeaseId '["L1"]'.
        (self.share / "logs").mkdir()
        t = "123456789012"
        lines = [
            {"token": 123456789012, "leaseId": "L1", "endReason": "closed"}, {"token": t, "leaseId": "L1", "endReason": "idle", "endedUtc": "2026-11-14T18:05:01Z", "heldSec": 301},
            {"token": t, "leaseId": 1, "endReason": "idle"}, {"token": t, "leaseId": ["L1"], "endReason": "idle"},
        ]
        (self.share / "logs" / f"lease-batch-{t}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'\n"
            f"$rd = {{ param($id) $x = Read-VenueLeaseBatchReceipt -AgentShare $s -Token '{t}' -LeaseId $id; \"$($x.state):$($x.record.endReason)\" }}\n"
            "[ordered]@{ L1 = (& $rd 'L1'); one = (& $rd '1'); arr = (& $rd '[\"L1\"]') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"L1": "FOUND:idle", "one": "ABSENT:", "arr": "ABSENT:"})


# LEASE-BATCH-MULTI-BUILD-1: a wrapper around fake-leg.ps1 that records the build, backend and spec each leg was called with, then runs the fake leg unchanged.
FAKE_LEG_ARGS = r"""
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '', [string]$LeaseBatch = '', [string]$ExpectSpecSha256 = '')
Add-Content -LiteralPath $env:FAKE_LEG_ARGS -Value (([ordered]@{ leaf = [IO.Path]::GetFileNameWithoutExtension($LegSpec); sourceCommit = $SourceCommit; buildManifestSha256 = $BuildManifestSha256; backend = $Backend; leaseBatch = $LeaseBatch }) | ConvertTo-Json -Compress)
& (Join-Path $PSScriptRoot 'fake-leg.ps1') @PSBoundParameters
exit $LASTEXITCODE
"""


@requires_windows_pwsh
class VenueLeaseBatchLegPlanTests(VenueLeaseBatchRunnerTests):
    """LEASE-BATCH-MULTI-BUILD-1: one batched lease carries legs of more than one build and backend (-LegPlan), validated before the lease is requested, and
    an interleaving order (A B A B) across arms. Same fakes and seams as the runner tests above; nothing reaches a venue."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        (cls.tmp / "fake-leg-args.ps1").write_text(FAKE_LEG_ARGS, encoding="utf-8")
        cls.commit_a, cls.commit_b = "1" * 40, "2" * 40

    def setUp(self) -> None:
        super().setUp()
        self._stage_case()

    def _stage_case(self) -> None:
        self.leg_args = self.case / "leg-args.jsonl"
        # two builds staged in the venue's cache, by the path Invoke-VenueLeg hashes
        self.manifests = {c: self.stage(c, f"build {c[:1]}") for c in (self.commit_a, self.commit_b)}

    def stage(self, commit: str, body: str, **override: object) -> str:
        # the three fields Assert-AttrCudaBuildManifest (the leg job's own validator) requires: the build's commit, its dll pair digest and a boolean symbol presence
        cache = self.share / "cache"
        cache.mkdir(exist_ok=True)
        p = cache / f"playback-attr-3-cuda-{commit[:12]}-build.json"
        import hashlib
        # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1: the three cached artifacts the manifest digests name are staged beside it, each hashing to its digest (an override replaces the
        # digest and leaves that artifact unstaged, as the digest-shape cases below need)
        digests = {}
        for label in ARTIFACT_SHAS:
            content = f"{label}:{commit}:{body}".encode("utf-8")
            self.artifact_path(commit, label).write_bytes(content)
            digests[label] = {"sha256": hashlib.sha256(content).hexdigest()}
        doc = {"sourceCommit": commit, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **digests, "body": body}
        doc.update(override)
        p.write_text(json.dumps({k: v for k, v in doc.items() if v is not ABSENT}), encoding="utf-8")
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def artifact_path(self, commit: str, label: str) -> Path:
        # the names the leg job reads from the agent cache (Get-AttrCudaArtifactNames)
        s = commit[:12]
        name = {"exe": f"MLVApp-playback-attr-3-cuda-{s}.exe", "dll": f"igpu_recon_cuda-playback-attr-3-cuda-{s}.dll", "packageZip": f"MLVApp-playback-attr-3-cuda-{s}-pkg.zip"}[label]
        return self.share / "cache" / name

    def item(self, spec: Path, arm: str, commit: str, backend: str = "cpu", manifest: str | None = None) -> dict:
        return {"legSpec": str(spec), "backend": backend, "sourceCommit": commit, "buildManifestSha256": manifest or self.manifests[commit], "arm": arm}

    def write_plan(self, items: list[dict]) -> Path:
        p = self.case / "plan.json"
        p.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": items}), encoding="utf-8")
        return p

    def plan_cmd(self, plan: Path, *extra: str, **env: str) -> tuple[list[str], dict]:
        e = dict(os.environ)
        e.update({"FAKE_EVENTS": str(self.events), "FAKE_SHARE": str(self.share), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case),
                  "FAKE_LEG_ARGS": str(self.leg_args)})
        e.update(env)
        defaults = {"-LegBudgetSec": "60", "-LeaseMarginSec": "0", "-ClaimPollSec": "1", "-RecheckSec": "1",
                    "-DeadlineUtc": utc_iso(datetime.now(timezone.utc) + timedelta(seconds=240)), "-LeaseCooldownSec": "0"}
        # plan_runner: a case that runs a patched mirror of the runner (_mirror_with_bad_sidecar) names it here; unset, it is the runner under test
        args = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(getattr(self, "plan_runner", RUNNER)), "-LegPlan", str(plan),
                "-RunDir", str(self.run_dir), "-ClaimDir", str(self.claims),
                "-QuietScript", str(self.tmp / "fake-quiet.ps1"), "-LegScript", str(self.tmp / "fake-leg-args.ps1"), "-UmRunScript", str(self.tmp / "fake-umrun.ps1"),
                "-AgentShare", str(self.share), *[x for k, v in defaults.items() if k not in extra for x in (k, v)], *extra]
        return args, e

    def run_plan(self, plan: Path, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args, e = self.plan_cmd(plan, *extra, **env)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def leg_calls(self) -> list[dict]:
        return [json.loads(l) for l in self.leg_args.read_text(encoding="utf-8").splitlines() if l.strip()] if self.leg_args.exists() else []

    def ab_block_plan(self) -> Path:
        # block order on purpose (A A B B): -PlanOrder Interleave must run it A B A B
        a, b = self.commit_a, self.commit_b
        return self.write_plan([self.item(self.specs[0], "A", a, "cpu"), self.item(self.specs[1], "A", a, "cpu"),
                                self.item(self.specs[0], "B", b, "cuda"), self.item(self.specs[1], "B", b, "cuda")])

    def test_two_builds_interleaved_in_one_lease_each_row_carrying_its_own_build(self) -> None:
        # RED on 3723b76d: the runner has no -LegPlan (one build per call, so an A/B pair takes a lease per build).
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        opens = [x for x in k if x.startswith("OPEN:")]
        self.assertEqual(len(opens), 1, f"ONE lease request for both builds: {k}")
        token = opens[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}",
                             "QUIET:QUIET", f"LEG:fx-b:{token}", "QUIET:QUIET", f"LEG:fx-b:{token}", f"CLOSE:{token}"])
        a, b = self.commit_a, self.commit_b
        want = [("fx-a", "A", a, "cpu"), ("fx-a", "B", b, "cuda"), ("fx-b", "A", a, "cpu"), ("fx-b", "B", b, "cuda")]
        # each leg process was handed its own build and backend
        calls = self.leg_calls()
        self.assertEqual([(c["leaf"], c["sourceCommit"], c["buildManifestSha256"], c["backend"], c["leaseBatch"]) for c in calls],
                         [(leaf, c, self.manifests[c], be, token) for leaf, _arm, c, be in want])
        # each batch row and each run result records its own build, backend and arm, in A B A B order
        [bt] = self.batches()
        self.assertEqual((bt["endReason"], bt["legsRan"], bt["legsCounted"], bt["leaseId"]), ("ALL_LEGS_DONE", 4, 4, "lease-1"))
        self.assertEqual([(l["arm"], l["sourceCommit"], l["buildManifestSha256"], l["backend"]) for l in bt["legs"]],
                         [(arm, c, self.manifests[c], be) for _leaf, arm, c, be in want])
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"], res["legPlan"]["order"]), (4, 4, "Interleave"))
        self.assertEqual([(l["name"], l["arm"], l["sourceCommit"], l["buildManifestSha256"], l["backend"]) for l in res["legs"]],
                         [(f"{i + 1:02d}-{leaf}-{be}-{arm}", arm, c, self.manifests[c], be) for i, (leaf, arm, c, be) in enumerate(want)])
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f"LEG 02-fx-a-cuda-B RUN batch=1 ", log)
        self.assertIn(f" arm=B sourceCommit={b} buildManifestSha256={self.manifests[b]}", log)
        # the validated, ordered plan is kept beside the receipts
        kept = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([x["arm"] for x in kept["legs"]], ["A", "B", "A", "B"])
        self.assertEqual(sorted((s["sourceCommit"], s["state"]) for s in kept["builds"]), [(a, "STAGED"), (b, "STAGED")])

    def test_the_plan_runs_as_given_without_the_interleave_option(self) -> None:
        r = self.run_plan(self.ab_block_plan())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [bt] = self.batches()
        self.assertEqual([l["arm"] for l in bt["legs"]], ["A", "A", "B", "B"])
        self.assertEqual(batch_result(r.stdout)["legPlan"]["order"], "AsGiven")

    def test_a_build_that_is_not_staged_is_refused_before_the_lease_is_requested(self) -> None:
        unstaged = "3" * 40
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[0], "B", unstaged, manifest="c" * 64)])
        r = self.run_plan(plan, "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=BUILD_NOT_STAGED item=2 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted (no open, no probe, no leg) when one build is not staged")
        self.assertFalse((self.run_dir / "batches.jsonl").exists())
        self.assertFalse(self.claim_path.exists())

    def test_every_item_is_validated_before_the_lease(self) -> None:
        a = self.commit_a
        bad = {
            "BUILD_MANIFEST_MISMATCH": self.item(self.specs[0], "B", a, manifest="d" * 64),
            "BAD_BACKEND": self.item(self.specs[0], "B", a, backend="metal"),
            "BAD_SOURCE_COMMIT": self.item(self.specs[0], "B", "xyz", manifest=self.manifests[a]),
            "BAD_MANIFEST_SHA256": self.item(self.specs[0], "B", a, manifest="e" * 63),
            "BAD_ARM": self.item(self.specs[0], "", a),
            "LEG_SPEC_MISSING": self.item(self.case / "no-such-spec.json", "B", a),
        }
        for reason, item in bad.items():
            with self.subTest(reason=reason):
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a), item]))
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(f"LEG_PLAN_REFUSED reason={reason} item=2 ", r.stderr)
                self.assertEqual(self.kinds(), [])

    def write_raw_plan(self, doc: object) -> Path:
        p = self.case / "plan.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def assert_whole_plan_refused(self, plan: Path, want: str) -> None:
        # subtests share one fixture directory: clear what an earlier subtest's run left, so a run that wrongly proceeds cannot fail the next case instead of itself
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        r = self.run_plan(plan)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"LEG_PLAN_REFUSED reason={want}", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse((self.run_dir / "batches.jsonl").exists(), "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_a_null_or_non_object_entry_refuses_the_whole_plan_at_its_original_position(self) -> None:
        # Dot B1 (e2160c2d): the null was dropped BEFORE the shape check, so [A, null, B] ran as a two-item plan (B became item 2) and finished ALL_LEGS_DONE.
        a, b = self.commit_a, self.commit_b
        ok_a, ok_b = self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", b, "cuda")
        cases = {
            "null in the middle": ([ok_a, None, ok_b], 2),
            "null first": ([None, ok_a, ok_b], 1),
            "null last": ([ok_a, ok_b, None], 3),
            "string in the middle": ([ok_a, "leg-b", ok_b], 2),
            "number in the middle": ([ok_a, 7, ok_b], 2),
            "array in the middle": ([ok_a, [ok_b], ok_b], 2),
            "only a null": ([None], 1),
        }
        for label, (legs, n) in cases.items():
            for container in ("object", "bare-array"):
                with self.subTest(case=label, container=container):
                    doc = {"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": legs} if container == "object" else legs
                    self.assert_whole_plan_refused(self.write_raw_plan(doc), f"BAD_ITEM_SHAPE item={n} ")

    def test_a_legs_value_that_is_not_an_array_is_refused_not_wrapped_into_a_one_item_plan(self) -> None:
        one = self.item(self.specs[0], "A", self.commit_a)
        schema = "mlv-app/venue-lease-leg-plan/v1"
        cases = {
            "a single item object as legs": {"schema": schema, "legs": one},
            "legs is a string": {"schema": schema, "legs": "leg-a"},
            "legs is a number": {"schema": schema, "legs": 3},
            "legs is null": {"schema": schema, "legs": None},
            "legs is an empty array": {"schema": schema, "legs": []},
            "a bare item object with no legs container": one,
            "the document is a string": "plan",
            "the document is a number": 5,
            "the document is null": None,
            "the document is an empty array": [],
            "the legs container is named something else": {"schema": schema, "items": [one]},
        }
        for label, doc in cases.items():
            with self.subTest(case=label):
                self.assert_whole_plan_refused(self.write_raw_plan(doc), "LEG_PLAN_UNREADABLE")

    def test_r3_a1_a_plan_order_in_any_letter_case_still_interleaves(self) -> None:
        # sol r3 A1: -PlanOrder is bound case-insensitively but was dispatched case-sensitively, so 'interleave' was accepted and ran A A B B.
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [bt] = self.batches()
        self.assertEqual([l["arm"] for l in bt["legs"]], ["A", "B", "A", "B"])
        self.assertEqual(batch_result(r.stdout)["legPlan"]["order"], "Interleave", "the receipt names the canonical order, not the spelling typed")

    def test_r3_a2_arms_that_differ_only_in_case_are_separate_arms(self) -> None:
        # sol r3 A2: the arm label is validated case-sensitively (A and a are both legal) but the interleave keyed its queues case-insensitively, merging them.
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "$items = 'AAaa'.ToCharArray() | ForEach-Object -Begin { $i = 0 } -Process { $i++; [pscustomobject]@{ arm = [string]$_; n = $i } }\n"
            "(@(Get-VenueInterleavedLegPlan -Items @($items)) | ForEach-Object { \"$($_.arm)$($_.n)\" }) -join ' '\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "A1 a3 A2 a4")

    def test_r3_a3_a_trailing_line_feed_never_passes_a_plan_item_pattern(self) -> None:
        # sol r3 A3: '$' also matches before a final LF, so arm "A\n" (an illegal Windows file name once it is in a leg output name) and a 40-hex commit or
        # 64-hex manifest with a trailing LF passed the item patterns. \z is the end of the string.
        a = self.commit_a
        bad = {
            "BAD_ARM": self.item(self.specs[0], "B\n", a),
            "BAD_SOURCE_COMMIT": self.item(self.specs[0], "B", a + "\n", manifest=self.manifests[a]),
            "BAD_MANIFEST_SHA256": self.item(self.specs[0], "B", a, manifest=self.manifests[a] + "\n"),
        }
        for reason, item in bad.items():
            with self.subTest(reason=reason):
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), item]), f"{reason} item=2 ")

    def test_r3_a3_a_spec_legid_with_a_trailing_line_feed_falls_back_to_the_file_name(self) -> None:
        spec = self.case / "fx-nl.json"
        spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "bad\n", "backends": ["cpu"], "scaleFactor": 1}), encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([l["name"] for l in batch_result(r.stdout)["legs"]], ["01-fx-nl-cpu-A"])

    def test_r3_a4_an_item_the_leg_script_would_throw_on_is_refused_before_the_lease(self) -> None:
        # sol r3 A4: Invoke-VenueLeg throws DVE_LEG_SPEC_INVALID when the item's backend is not one the spec lists (or the spec is not a leg spec), AFTER the
        # lease is open, which ends as LEG_FAILED / exit 7 instead of the whole plan being refused with exit 2 and nothing submitted.
        a = self.commit_a

        def spec(name: str, text: str) -> Path:
            p = self.case / name
            p.write_text(text, encoding="utf-8")
            return p
        cpu_only = spec("cpu-only.json", json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "legId": "cpu-only", "backends": ["cpu"]}))
        wrong_schema = spec("wrong-schema.json", json.dumps({"schema": "nope", "legId": "ws", "backends": ["cpu"]}))
        not_json = spec("not-json.json", "this is not json")
        no_backends = spec("no-backends.json", json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "legId": "nb"}))
        cases = {
            "BAD_BACKEND": self.item(cpu_only, "B", a, backend="cuda"),
            "LEG_SPEC_INVALID wrong schema": self.item(wrong_schema, "B", a),
            "LEG_SPEC_INVALID not json": self.item(not_json, "B", a),
            "BAD_BACKEND no backends listed": self.item(no_backends, "B", a),
        }
        for label, item in cases.items():
            with self.subTest(case=label):
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), item]), f"{label.split(' ')[0]} item=2 ")

    def test_r3_a5_a_refusal_whose_log_line_cannot_be_written_still_says_so_on_stderr_and_exits_2(self) -> None:
        # sol r3 A5: Stop-PlanRefused wrote batch.log first; with the log held by another process that write threw, so the typed refusal never reached stderr and
        # the exit was 1, not 2.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        hold = ExclusiveHold(log)
        try:
            r = self.run_plan(self.case / "missing-plan.json")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("BATCH_USAGE LEG_PLAN_REFUSED reason=LEG_PLAN_UNREADABLE", r.stderr)
            self.assertEqual(self.kinds(), [])
        finally:
            hold.release()

    def test_r4_a1_a_spec_the_leg_process_would_throw_on_is_refused_before_the_claim_and_the_lease(self) -> None:
        # sol r3 second pass A1: Invoke-VenueLeg also throws on a malformed visualCapture / opt-out pair, a playSeconds outside [int], a scaleFactor that is not a
        # number and a bad acceptedEffectiveScale. Preflight checked only the schema and the backend, so each of these opened a lease and ended LEG_FAILED / exit 7.
        a = self.commit_a
        base = {"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-bad", "backends": ["cpu"], "scaleFactor": 1}
        cases = {
            "visualCapture is a string": {"visualCapture": "true"},
            "visualCapture false without a reason": {"visualCapture": False},
            "visualCapture null": {"visualCapture": None},
            "a reason without the opt-out": {"visualCaptureOptOutReason": "because"},
            "playSeconds beyond int": {"playSeconds": 2147483648},
            "scaleFactor is not a number": {"scaleFactor": "bad"},
            "acceptedEffectiveScale is not a number": {"scaleFactor": 1, "acceptedEffectiveScale": {"cpu": "bad"}},
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                p = self.case / "fx-bad.json"
                p.write_text(json.dumps({**base, **extra}), encoding="utf-8")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(p, "B", a)]), "LEG_SPEC_INVALID item=2 ")

    def test_r2n1_a7_plan_a_spec_the_strict_reader_cannot_type_is_refused_before_the_claim_and_the_lease(self) -> None:
        # adversary-7c5bfe86.json A7 (R2-NARROW-1 r2), the -LegPlan route (Get-LegSpecRefusal): a spec declaring legType twice (both shapes valid by the last member) is refused whole-plan, typed, before any claim
        a = self.commit_a
        base = json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-bad", "backends": ["cpu"], "scaleFactor": 1})
        for case, tail in (("dup-speed-speed", ', "legType": "speed", "legType": "speed"}'), ("dup-export-speed", ', "legType": "export", "legType": "speed"}')):
            with self.subTest(case=case):
                p = self.case / "fx-bad.json"
                p.write_text(base[:-1] + tail, encoding="utf-8")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(p, "B", a)]), "LEG_SPEC_INVALID item=2 ")

    def test_r4_a1_a_valid_opt_out_and_scale_still_run(self) -> None:
        # the added preflight is the leg process's own predicates, no stricter: a well-formed opt-out, scale and playSeconds are accepted
        p = self.case / "fx-ok.json"
        p.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-ok", "backends": ["cpu"], "visualCapture": False, "visualCaptureOptOutReason": "no window",
                                 "playSeconds": 30, "scaleFactor": 2, "acceptedEffectiveScale": {"cpu": 1}}), encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(p, "A", self.commit_a)]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r4_a2_a_plan_item_whose_path_cannot_be_probed_is_a_typed_refusal(self) -> None:
        # sol r3 second pass A2: Test-Path throws on a NUL in legSpec; the throw bypassed Stop-PlanRefused and exited 1 with no BATCH_USAGE line.
        a = self.commit_a
        for label, ref in {"NUL in the middle": "a\u0000b", "NUL only": "\u0000", "a wildcard-only name": "*", "a drive-only name": "Z:"}.items():
            with self.subTest(case=label):
                item = {"legSpec": ref, "backend": "cpu", "sourceCommit": a, "buildManifestSha256": self.manifests[a], "arm": "B"}
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), item]), "LEG_SPEC_MISSING item=2 ")

    def test_r4_a3_a_second_invocation_on_the_same_run_dir_never_overwrites_the_plan_before_it_holds_the_claim(self) -> None:
        # sol r3 second pass A3: leg-plan.json was written during validation, before the venue claim. A second invocation that is still waiting for another
        # run's claim replaced the first run's preserved plan. Here a live foreign claim holds the venue and the run ends DEADLINE claim-held: the file stays.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        keep = self.run_dir / "leg-plan.json"
        keep.write_text('{"preserved":"first invocation"}\n', encoding="utf-8")
        script = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                  f"$r = Request-VenueClaim -Venue bachelor -RunDir 'C:\\other-run' -Purpose foreign -TtlSec 3600 -ClaimDir '{psq(self.claims)}' -OwnerPid {os.getpid()}\n"
                  "$r.granted\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip(), "True", out.stdout + out.stderr)
        r = self.run_plan(self.ab_block_plan(), "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=8)))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.kinds(), [], "no open: the venue claim was never held")
        self.assertEqual(keep.read_text(encoding="utf-8"), '{"preserved":"first invocation"}\n')

    def test_r4_a3_the_plan_is_kept_once_the_claim_is_held(self) -> None:
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kept = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([x["arm"] for x in kept["legs"]], ["A", "B", "A", "B"])

    def test_r5_f2_a_spec_without_a_card_or_a_legid_is_refused_before_the_claim_and_the_lease(self) -> None:
        # key3 F2 (fable, opus, sol, Dot B3): Invoke-VenueLeg binds [string]$spec.card and [string]$spec.legId to New-DvReceipt's Mandatory [string] parameters before it
        # submits anything; an empty one is a binding error. Preflight never read either, so such a spec opened a lease and ended LEG_FAILED / exit 7.
        a = self.commit_a
        base = {"schema": "mlv-app/dual-venue-leg/v1", "backends": ["cpu"], "scaleFactor": 1}
        cases = {
            "no card (the key3 repro)": {"legId": "fx-a"},
            "card is empty": {"card": "", "legId": "fx-a"},
            "card is null": {"card": None, "legId": "fx-a"},
            "card present, no legId": {"card": "TEST-CARD"},
            "card present, legId is empty": {"card": "TEST-CARD", "legId": ""},
            "card present, legId is null": {"card": "TEST-CARD", "legId": None},
            "neither": {},
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                p = self.case / "fx-noid.json"
                p.write_text(json.dumps({**base, **extra}), encoding="utf-8")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(p, "B", a)]), "LEG_SPEC_INVALID item=2 ")

    def test_r5_f2_a_spec_with_a_card_and_a_legid_still_runs(self) -> None:
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r5_f1_a_throw_after_the_claim_keeper_starts_stops_the_keeper_releases_the_claim_and_ends_the_process(self) -> None:
        # key3 F1 (fable, opus, sol, Dot B2): leg-plan.json was written after Start-VenueClaimKeeper and before the batch try, so a throw there skipped Stop-VenueKeeper and
        # Release-VenueClaim: the keeper runspace kept renewing the claim and the process never exited. A directory of that name makes the write throw.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "leg-plan.json").mkdir()
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s (the claim keeper outlived the throw): {ex.stdout!r}")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        self.assertEqual(self.kinds(), [], "no open was submitted: the throw came before the lease")
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual((res["ran"], res["notRun"]), (0, 4))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(" CLAIM_RELEASED batch=1 result=", log)
        self.assertIn(" BATCH_ERROR batch=1 ", log)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "BATCH_ERROR")

    _EXPIRED_MARKER, _ACK_MARKER = "runner-pause-expired.marker", "runner-ack.marker"

    def _pause_snippet(self, message: str, pause_s: float) -> str:
        """The PowerShell the runner copy runs at its first log line starting `message` (see _runner_paused_at_claim_taken); its own method so a test can run it alone."""
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        expired, ack = self.case / self._EXPIRED_MARKER, self.case / self._ACK_MARKER
        return (f"    if (-not $script:PauseDone -and $Message.StartsWith('{psq(message)}', [StringComparison]::Ordinal)) {{ $script:PauseDone = $true; $n = [guid]::NewGuid().ToString('N'); Set-Content -LiteralPath '{psq(paused)}' -Value $n; "
                 f"$sw = [Diagnostics.Stopwatch]::StartNew(); $seen = $false; "
                 f"while ($sw.Elapsed.TotalSeconds -lt {pause_s}) {{ if (Test-Path -LiteralPath '{psq(locked)}') {{ $seen = $sw.Elapsed.TotalSeconds -lt {pause_s}; break }}; Start-Sleep -Milliseconds 20 }}\n"
                 f"        if ($seen) {{ $held = $false; try {{ [IO.File]::Open($batchLog, 'Open', 'Read', 'ReadWrite').Dispose() }} catch [IO.IOException] {{ $held = $true }}; "
                 f"if ($held -and $sw.Elapsed.TotalSeconds -lt {pause_s}) {{ Set-Content -LiteralPath '{psq(ack)}' -Value ($n + ' held') }} elseif ($held) {{ $seen = $false }} }} "
                 f"if (-not $seen) {{ Set-Content -LiteralPath '{psq(expired)}' -Value ($n + ' expired') }} }}\n")

    def _runner_paused_at_claim_taken(self, message: str = "CLAIM_TAKEN", release_at: str = "", more: tuple = (), pause_s: float = 60) -> tuple[Path, Path, Path]:
        """A copy of the runner that PAUSES when it is about to log a line starting `message` (CLAIM_TAKEN by default): it writes `paused` (holding an attempt nonce it generates) and waits (at most
        `pause_s`, 60 by default) for `locked` to appear. Seeing `locked` inside the window (the clock is read after the look, and again after the probe: adversary r4 A2), it probes batch.log itself and, only if the log really is held, writes `ack` = `<nonce> held`; a pause that
        runs out with no `locked` writes `expired` = `<nonce> expired` and the runner carries on. The pause happens once per run (a later matching line, e.g. batch 2's, never re-pauses or re-acks). A pause marker that survives the expiry is therefore never a handshake (sol r3 B1; Dot on #704).
        With `release_at`, the first line starting with it once `locked` exists writes `release-wanted` and waits (at most 60 s) for `released`: the lock holder lets go there.
        Returns (runner, paused, locked). Dot on #684: the lock helper used to poll for the claim file and hope to win the race with the runner's next log line; the handshake makes the order a fact.
        r3 (sol r2b hardening; adversary r2 A5): the prefix is compared ordinally (a `[` in it is no -like wildcard) and both waits run on a monotonic Stopwatch, not wall-clock UTC."""
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        wanted, released = self.case / "release-wanted.marker", self.case / "log-released.marker"
        anchor = "function L([string]$Message) {\n"
        pause = self._pause_snippet(message, pause_s)
        if release_at:
            pause += (f"    if ($Message.StartsWith('{psq(release_at)}', [StringComparison]::Ordinal) -and (Test-Path -LiteralPath '{psq(locked)}') -and -not (Test-Path -LiteralPath '{psq(released)}')) {{ "
                      f"Set-Content -LiteralPath '{psq(wanted)}' -Value 'release'; $sw = [Diagnostics.Stopwatch]::StartNew(); "
                      f"while (-not (Test-Path -LiteralPath '{psq(released)}') -and $sw.Elapsed.TotalSeconds -lt 60) {{ Start-Sleep -Milliseconds 20 }} }}\n")
        return self._patched_runner(anchor, anchor + pause, *more), paused, locked

    def _lock_log_while_the_runner_is_paused(self, log: Path, paused: Path, locked: Path, give_up_s: float = 120) -> tuple[threading.Event, threading.Thread, dict]:
        """Wait (at most give_up_s, monotonic) for the runner's `paused` marker, take `log` with share mode 0, write the `locked` marker (the runner resumes only now), and hold the lock
        until the returned event is set or the runner writes `release-wanted` (then the lock is let go and `log-released.marker` written). Windows only (CreateFileW). Returns
        (done, thread, state); state["outcome"] is locked / lock-failed (`locked` still written, so the runner never waits out its 60 s) / never-paused / cancelled, and
        state["released"] says whether the runner asked for the release. r3 (sol r2b B2): a wait that expired or was cancelled before `paused` appeared takes NO lock and writes NO
        `locked`: the caller fails on _assert_handshake's named message instead of a log locked ahead of the pause; a pause the runner already gave up on (its `expired` marker) takes no
        lock either (outcome pause-expired). The handle is closed in a finally."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        done = threading.Event()
        state: dict = {"outcome": "pending", "released": False, "give_up_s": give_up_s}
        wanted, released = paused.parent / "release-wanted.marker", paused.parent / "log-released.marker"
        expired, ack = paused.parent / self._EXPIRED_MARKER, paused.parent / self._ACK_MARKER
        state["markers"] = {"paused": paused, "expired": expired, "ack": ack}
        invalid = wintypes.HANDLE(-1).value

        def hold() -> None:
            # the decision is the loop's own observation (adversary r3 A1): a marker that appears only after the wait expired or was cancelled takes no lock either
            end, seen = time.monotonic() + give_up_s, False
            while not done.is_set() and time.monotonic() < end:
                if paused.exists():
                    seen = not done.is_set() and time.monotonic() < end   # adversary r3 pass 2 A2: a look that returned after the deadline does not count
                    break
                done.wait(0.01)
            if seen and expired.exists():   # the runner's own pause ran out and it carried on: a surviving `paused` marker is no handshake (sol r3 B1)
                state["outcome"] = "pause-expired"
                return
            if not seen:
                state["outcome"] = "cancelled" if done.is_set() else "never-paused"
                return
            h = k32.CreateFileW(str(log), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)   # GENERIC_READ|GENERIC_WRITE, share none, OPEN_EXISTING
            try:
                state["outcome"] = "locked" if h != invalid else "lock-failed"
                locked.write_text(state["outcome"], encoding="utf-8")
                if h != invalid:
                    end = time.monotonic() + give_up_s
                    while not done.is_set() and not wanted.exists() and time.monotonic() < end:
                        done.wait(0.01)
                    state["released"] = wanted.exists()
            finally:
                if h != invalid:
                    k32.CloseHandle(h)
            if state["released"]:
                released.write_text("released", encoding="utf-8")

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        return done, t, state

    def _assert_handshake(self, state: dict, message: str, r: subprocess.CompletedProcess | None = None) -> None:
        """r3 (sol r2b B2): the lock was taken only after the runner paused at `message`; a runner that never paused (slow start, early death) fails here by name."""
        out = "" if r is None else "\n" + r.stdout + r.stderr
        self.assertNotEqual(state["outcome"], "never-paused", f"the runner never paused at {message!r} within {state['give_up_s']} s: the lock was not taken{out}")
        self.assertNotEqual(state["outcome"], "cancelled", f"the runner ended before it paused at {message!r}: the lock was not taken{out}")
        self.assertNotEqual(state["outcome"], "pause-expired", f"the runner's pause at {message!r} expired with no lock: the surviving pause marker is no handshake{out}")
        self.assertEqual(state["outcome"], "locked", f"the handshake: the runner paused at {message!r} and resumed only once the lock was held{out}")
        # sol r3 B1 / Dot on #704: the runner must itself have seen the lock WHILE still paused. Its `expired` marker (the pause ran out first; a helper delayed past it still locks) refuses the
        # attempt; so does an `ack` that is missing or carries another attempt's nonce (the runner writes `<nonce> held` only after probing batch.log and finding it held).
        mk = state["markers"]
        self.assertFalse(mk["expired"].exists(), f"the runner's pause at {message!r} expired before the lock was taken: no handshake{out}")
        nonce = mk["paused"].read_text(encoding="utf-8").strip() if mk["paused"].exists() else ""
        got = mk["ack"].read_text(encoding="utf-8").strip() if mk["ack"].exists() else None
        self.assertTrue(nonce and got == f"{nonce} held", f"no attempt-specific acknowledgment from the paused runner at {message!r} (want {nonce!r} + ' held', got {got!r}){out}")

    def test_r6_b5_a_batch_log_another_process_holds_after_the_claim_cannot_skip_the_release_the_receipt_or_the_summary(self) -> None:
        # Dot r5 B5: the finally logged CLAIM_RENEW before Release-VenueClaim; with batch.log held exclusively after the claim was taken, that log call threw out of the
        # finally, so the claim stayed, no receipt or summary was written and the typed exit 12 was lost (an unhandled exit 1). Every mandatory step runs, logging or not.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        runner, paused, locked = self._runner_paused_at_claim_taken()
        args[args.index("-File") + 1] = str(runner)
        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", paused, locked)
        t0 = time.monotonic()
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s: {ex.stdout!r}")
        finally:
            elapsed = time.monotonic() - t0
            done.set()
            t.join(5)
        self._assert_handshake(state, "CLAIM_TAKEN", r)
        self.assertLess(elapsed, 60, "bounded: the lock must not stretch the run")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        self.assertEqual(self.kinds(), [], "no open was submitted")
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual(res["ran"], 0)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "BATCH_ERROR")
        self.assertGreaterEqual(b["logWriteFailures"], 1, "the swallowed logging failures are recorded in the receipt")
        self.assertIn("BATCH_LOG_FAILED", r.stderr)

    def test_r6_b5_sibling_a_log_failure_right_after_a_stale_claim_is_broken_does_not_leave_the_new_claim_behind(self) -> None:
        # B5's class: Request-VenueClaim had granted the claim when CLAIM_STALE_BROKEN was logged, and that line sat outside the try, so a log failure there left the new
        # claim (and no keeper yet) behind with no release. The line is best effort now; the batch carries on and ends normally. The failure is injected into a copy of the runner.
        self.claims.mkdir(parents=True, exist_ok=True)
        seed = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                f"$r = Request-VenueClaim -Venue bachelor -RunDir 'C:\\dead-run' -Purpose dead -TtlSec 3600 -ClaimDir '{psq(self.claims)}' -OwnerPid {os.getpid()}\n"
                "$r.granted\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", seed], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip(), "True", out.stdout + out.stderr)
        # the owner is gone (a pid no process has): the next claim request breaks it as dead-pid, the way the STEAL fake leaves a foreign claim
        self.claim_path.write_text(re.sub(r'"ownerPid":\s*\d+', '"ownerPid":99999999', self.claim_path.read_text(encoding="utf-8")), encoding="utf-8")
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'CLAIM_STALE_BROKEN*') { throw 'simulated log failure' }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["notRun"]), (4, 0))
        self.assertIn("BATCH_LOG_FAILED", r.stderr)

    def _patched_runner(self, old: str, new: str, *more: tuple[str, str]) -> Path:
        """A copy of the dual-venue tree whose runner has `old` (found exactly once) replaced by `new`, and each further (old, new) pair in `more` likewise; returns the copy's runner."""
        tree = self.case / "tree" / "tools" / "profiling"
        shutil.copytree(DV, tree / "dual-venue", ignore=shutil.ignore_patterns("look-receipts", "legs", "legsets"))
        (tree / "bachelor").mkdir()
        shutil.copy2(AGENT, tree / "bachelor" / AGENT.name)
        # VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r5 (CI run 43580040634): and every neighbouring-folder module the dual-venue modules read (..\bachelor\AttrCudaArtifacts.psm1)
        copy_cross_tree_modules(MODULE, tree / "dual-venue")
        runner = tree / "dual-venue" / RUNNER.name
        text = runner.read_text(encoding="utf-8")
        for o, n in ((old, new), *more):
            self.assertEqual(text.count(o), 1, "the runner line moved: update this fixture")
            text = text.replace(o, n)
        runner.write_text(text, encoding="utf-8")
        return runner

    def test_r6_adv_a3_a_result_line_that_cannot_be_appended_to_the_log_is_still_said_on_stderr(self) -> None:
        # adversary r6 A3: the final LEASE_BATCH_RESULT append failed inside an empty catch, so no BATCH_LOG_FAILED diagnostic was ever emitted for it. The run still ends
        # 0 with the result on stdout; the failure is injected into a copy of the runner (the real cause is a batch.log another process holds at that instant).
        anchor = "try { Add-Content -LiteralPath $batchLog -Value $resultLine }"
        runner = self._patched_runner(anchor, "try { throw 'simulated result-line append failure'; Add-Content -LiteralPath $batchLog -Value $resultLine }")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(batch_result(r.stdout)["ran"], 4)
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (simulated result-line append failure)", r.stderr)

    def test_lease_batch_idle_end_r2_adv_a1_a_log_failure_at_the_idle_end_keeps_the_typed_stop_and_the_leg(self) -> None:
        # adversary r2 pass 2 A1 (adversary-bd508a0e.json): the LEASE_IDLE_ENDED line was logged with L, which rethrows a batch.log append failure, so the typed end became
        # BATCH_ERROR/12 and the pending leg NOT_RUN. The line is best effort: the batch ends LEASE_IDLE_ENDED and the leg runs in the next batch.
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'LEASE_IDLE_ENDED*') { throw [IO.IOException]::new('simulated: batch.log held') }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        e = dict(e, FAKE_QUIET_MODES="QUIET,IDLE,QUIET")
        r = subprocess.run(args, capture_output=True, text=True, timeout=180, env=e)
        res = batch_result(r.stdout)
        # r3 (Grok G5): the stop reason is pinned (this slot compared it with itself): the idle end is a batch end, never a run stop, so the run ends with none
        self.assertEqual((r.returncode, res["stopReason"], res["ran"], res["notRun"]), (0, None, 4, 0), r.stdout + r.stderr)
        self.assertEqual(self.batches()[0]["endReason"], "LEASE_IDLE_ENDED", self.batches()[0])
        self.assertIn("BATCH_LOG_FAILED", r.stderr)

    def test_lease_batch_idle_end_r2_adv_a1_sibling_a_log_failure_at_the_launch_deadline_keeps_deadline_exit_3(self) -> None:
        dl = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=25))
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'DEADLINE batch=*phase=launch*') { throw [IO.IOException]::new('simulated: batch.log held') }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave", "-DeadlineUtc", dl)
        args[args.index("-File") + 1] = str(runner)
        e = dict(e, FAKE_QUIET_MODES="PASTDEADLINE", FAKE_DEADLINE_UTC=dl)
        r = subprocess.run(args, capture_output=True, text=True, timeout=180, env=e)
        res = batch_result(r.stdout)
        self.assertEqual((r.returncode, res["stopReason"], res["ran"]), (3, "DEADLINE", 0), r.stdout + r.stderr)
        self.assertEqual(self.batches()[0]["endReason"], "DEADLINE", self.batches()[0])

    def test_r7_sol_a_final_result_append_that_fails_is_counted_in_the_logwritefailures_the_result_line_reports(self) -> None:
        # sol r6b blocker: logWriteFailures was serialized into the result line BEFORE the final append was attempted, so an IOException on that append left the live
        # counter at 1 and the stdout summary at 0 (exit 0, ran 4). The count the summary reports includes the append that just failed.
        anchor = "try { Add-Content -LiteralPath $batchLog -Value $resultLine }"
        runner = self._patched_runner(anchor, "try { throw [IO.IOException]::new('locked'); Add-Content -LiteralPath $batchLog -Value $resultLine }")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual(res["ran"], 4)
        self.assertEqual(res["logWriteFailures"], 1, "the failed final append is in the count the result line reports")
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (locked)", r.stderr)

    def test_r7_sol_a_batch_end_log_failure_is_counted_in_the_batch_receipt_and_the_summary(self) -> None:
        # sol r6b sibling: the batch receipt was written BEFORE the BATCH_END line was logged, so a failure logging BATCH_END was missing from the receipt's logWriteFailures
        # (and the receipt claimed 0 failures for a batch that had one). The receipt and the summary both carry it.
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'BATCH_END*') { throw [IO.IOException]::new('locked') }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual(res["ran"], 4)
        [b] = self.batches()
        self.assertEqual(b["logWriteFailures"], 1, "the BATCH_END failure is in the receipt written for that batch")
        self.assertEqual(res["logWriteFailures"], 1)
        self.assertIn("BATCH_END", r.stdout, "the line still reaches stdout")
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (locked)", r.stderr)

    def test_r7_sol_b_the_pre_claim_deadline_batch_end_failure_is_counted_in_the_receipt_written_for_that_batch(self) -> None:
        # sol r6b sibling class: the two pre-claim DEADLINE exits wrote the batch receipt BEFORE logging BATCH_END (and never carried logWriteFailures at all), so a
        # failure logging BATCH_END was missing from the receipt, and an unprotected L threw out of the batch. The receipt carries the count after the line is tried.
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'BATCH_END*') { throw [IO.IOException]::new('locked') }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["preOpenWait"]["reason"]), ("DEADLINE", "BUDGET"))
        self.assertEqual(b["logWriteFailures"], 1, "the BATCH_END failure is in the receipt written for that batch")
        self.assertEqual(batch_result(r.stdout)["logWriteFailures"], 1)

    # LEASE-BATCH-RECEIPT-PERSIST-FAILURE-REPORTING-1 (Dot B6 on #684, DISTINGUISHed to this card): a receipt file the run cannot write (batches.jsonl, batch-NN.json or leg-plan.json
    # occupied by a DIRECTORY) used to throw out of Write-BatchReceipt, which sits in the batch's finally (or, pre-claim, outside any try): the summary was skipped and the exit was
    # an unhandled 1 instead of the typed code the outcome had selected. The error evidence is kept, said once as RECEIPT_PERSIST_FAILED, and the typed code is still the exit.
    def _occupy_with_directory(self, *names: str) -> None:
        for n in names:
            (self.run_dir / n).mkdir(parents=True, exist_ok=True)

    def _assert_persist_failure_said(self, r: subprocess.CompletedProcess, res: dict, *names: str) -> None:
        for n in names:
            want = f"RECEIPT_PERSIST_FAILED file={self.run_dir / n} reason="
            self.assertIn(want, r.stdout, f"the typed line for {n}: {r.stdout}")
            self.assertTrue(any(f["file"] == str(self.run_dir / n) and f["reason"] for f in res["receiptPersistFailures"]), res.get("receiptPersistFailures"))
        self.assertEqual(len(res["receiptPersistFailures"]), len(names), res["receiptPersistFailures"])
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_persist_a_post_claim_batch_error_with_batches_jsonl_a_directory_still_emits_the_summary_and_exits_12(self) -> None:
        # leg-plan.json is a directory: the plan write after the claim throws (BATCH_ERROR, exit 12). batches.jsonl is a directory too, so the receipt append in the finally threw
        # out of it: no summary, exit 1. Now: the summary names the unwritten receipt, the in-memory results are kept, and the exit is the BATCH_ERROR 12.
        self._occupy_with_directory("leg-plan.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (12, "BATCH_ERROR", 0, 4))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertEqual([x["endReason"] for x in res["batches"]], ["BATCH_ERROR"], "the in-memory batch row is kept")
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "the receipt that could be written was written")

    def test_persist_a_post_claim_batch_error_with_only_leg_plan_json_a_directory_is_still_exit_12_with_a_summary(self) -> None:
        # the sibling that already held: leg-plan.json alone is the BATCH_ERROR trigger and every receipt writes, so nothing is reported as a persist failure
        self._occupy_with_directory("leg-plan.json")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual(res["receiptPersistFailures"], [])
        [b] = self.batches()
        self.assertEqual(b["endReason"], "BATCH_ERROR")

    def test_persist_a_pre_claim_deadline_with_batches_jsonl_and_leg_plan_json_directories_still_emits_the_summary_and_exits_3(self) -> None:
        # the pre-claim DEADLINE exit (the 3 h budget is spent) wrote its receipt outside any try: an unwritable batches.jsonl threw out of the script. leg-plan.json as a directory is
        # never touched on this path (it is written once the claim is held) and must change nothing.
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        self._occupy_with_directory("leg-plan.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (3, "DEADLINE", 0, 4))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertEqual(self.kinds(), [], "no claim, no open")

    def test_persist_an_otherwise_successful_batch_whose_receipt_cannot_be_written_exits_13_with_the_summary(self) -> None:
        # nothing else went wrong: every leg ran. The receipt is not on disk, so the run must not claim success: exit 13, the legs still reported as RAN in the summary.
        self._occupy_with_directory("batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"], res["notRun"]), (13, "RECEIPT_PERSIST_FAILED", 4, 4, 0))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertEqual([x["endReason"] for x in res["batches"]], ["ALL_LEGS_DONE"])
        self.assertTrue((self.run_dir / "batch-01.json").is_file())

    def test_persist_a_batch_receipt_file_that_is_a_directory_is_reported_and_the_jsonl_line_is_still_written(self) -> None:
        # the two receipt writes are independent: batch-01.json failing must not skip the batches.jsonl line (the other copy of the same record)
        self._occupy_with_directory("batch-01.json")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["ran"]), (13, 4))
        self._assert_persist_failure_said(r, res, "batch-01.json")
        [b] = self.batches()
        self.assertEqual(b["endReason"], "ALL_LEGS_DONE")

    def test_persist_adv_a1_a_log_failure_in_the_persist_diagnostic_is_counted_in_the_receipt_written_after_it(self) -> None:
        # adversary r1 A1: the batch snapshots logWriteFailures before Write-BatchReceipt; the RECEIPT_PERSIST_FAILED line for batch-01.json (a directory) then failed to log (batch.log held),
        # which bumped the live counter, and the batches.jsonl line was serialized from the stale snapshot (0) while the summary said more. Each receipt is serialized with the live count.
        self._occupy_with_directory("batch-01.json")
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        runner, paused, locked = self._runner_paused_at_claim_taken("RECEIPT_PERSIST_FAILED")
        args[args.index("-File") + 1] = str(runner)
        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", paused, locked)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s: {ex.stdout!r}")
        finally:
            done.set()
            t.join(5)
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertGreaterEqual(res["logWriteFailures"], 1)
        [b] = self.batches()
        self.assertGreaterEqual(b["logWriteFailures"], 1, "the surviving batches.jsonl line carries the failure the diagnostic itself hit")

    def test_persist_a_leg_failure_keeps_its_exit_7_when_the_receipt_cannot_be_written_either(self) -> None:
        # 13 replaces only a clean 0: a run that already selected a failing code keeps it, and the summary still names the unwritten receipt
        self._occupy_with_directory("batches.jsonl")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), FAKE_LEG_MODES="exit3")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["failed"]), (7, None, 1))
        self._assert_persist_failure_said(r, res, "batches.jsonl")

    def test_persist_a_clean_run_reports_no_persist_failure(self) -> None:
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["receiptPersistFailures"], res["logWriteFailures"]), (0, [], 0))
        self.assertNotIn("RECEIPT_PERSIST_FAILED", r.stdout)

    # r2 (Dot advisory on #704): the generated-runner pause helper embedded its paths unescaped in a PowerShell single-quoted literal (an apostrophe in the temp dir broke the runner),
    # and two receipt-write paths had no test: the batch-NN.json rewrite when only the diagnostic's own log write fails, and both receipt destinations failing at once.
    def _run_with_log_held_at(self, args: list[str], e: dict, message: str, release_at: str = "", give_up_s: float = 120, more: tuple = ()) -> tuple[subprocess.CompletedProcess, dict]:
        """Run `args` on a runner copy that pauses at the first log line starting `message`, with batch.log held exclusively from that moment (until the first line starting `release_at`
        once held, else until the run ends); `more` is further (old, new) runner patches. Returns (result, the lock helper's state); the caller asserts it with _assert_handshake."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        runner, paused, locked = self._runner_paused_at_claim_taken(message, release_at, more)
        args[args.index("-File") + 1] = str(runner)
        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", paused, locked, give_up_s)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s: {ex.stdout!r}")
        finally:
            done.set()
            t.join(5)
        return r, state

    def _run_plan_with_log_held_at(self, message: str, give_up_s: float = 120) -> tuple[subprocess.CompletedProcess, dict]:
        """The A/B plan under _run_with_log_held_at."""
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        return self._run_with_log_held_at(args, e, message, give_up_s=give_up_s)

    def test_r2_nit_a_case_dir_with_an_apostrophe_does_not_break_the_generated_runner_or_its_pause_markers(self) -> None:
        # Dot nit (source-confirmed at 1a880fee): `-LiteralPath '{paused}'` and `'{locked}'` were embedded unescaped, so a temp dir such as C:\...\case-o'brien-x made the generated runner
        # unparseable (the process died before it paused). Every path embedded in a generated PowerShell literal now goes through psq (the quote doubled).
        self._init_case(Path(tempfile.mkdtemp(prefix="case-o'brien-", dir=self.tmp)))
        self._stage_case()
        self.assertIn("'", str(self.case))
        self.assertEqual(psq("a'b''c"), "a''b''''c")
        # r3 (sol r2b B2): no short give-up any more; a slow start is waited out (the run's own 90 s bound ends the wait), and a runner that never pauses fails by name
        r, state = self._run_plan_with_log_held_at("CLAIM_TAKEN")
        self._assert_handshake(state, "CLAIM_TAKEN", r)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r2_adv_a1_psq_escapes_every_quote_character_powershell_treats_as_a_single_quote(self) -> None:
        # adversary r2 A1 (advisory, introduced by psq): PowerShell's tokenizer also takes U+2018, U+2019, U+201A and U+201B as single quotes, so a temp dir such as C:\temp\o<U+2019>Brien
        # ended the literal early even with the ASCII quote doubled. Each character, alone and inside a path, must parse and come back unchanged.
        samples = ["C:\\temp\\o'Brien", "C:\\temp\\o\u2018Brien", "C:\\temp\\o\u2019Brien", "C:\\temp\\o\u201aBrien", "C:\\temp\\o\u201bBrien", "\u2019\u2019", "plain"]
        lines = [f"$v = '{psq(s)}'; ($v.ToCharArray() | ForEach-Object {{ [int]$_ }}) -join ','" for s in samples]
        script = self.case / "psq-probe.ps1"
        script.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), [",".join(str(ord(c)) for c in s) for s in samples], r.stdout + r.stderr)

    def test_r2_cov_a_jsonl_append_failure_whose_diagnostic_cannot_be_logged_rewrites_batch_receipt_with_the_count_and_exits_13(self) -> None:
        # batch-01.json is written first (count 0) and succeeds; the batches.jsonl append fails (a directory); its RECEIPT_PERSIST_FAILED line cannot be logged (batch.log held), which bumps
        # logWriteFailures; so the already-written batch-01.json is rewritten with the live count (Write-BatchReceipt's second per-batch write). The typed record and exit 13 are unchanged.
        self._occupy_with_directory("batches.jsonl")
        r, state = self._run_plan_with_log_held_at("RECEIPT_PERSIST_FAILED")
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"]), (13, "RECEIPT_PERSIST_FAILED", 4, 4))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertGreaterEqual(res["logWriteFailures"], 1, "the diagnostic's own log failure is counted")
        per_batch = json.loads((self.run_dir / "batch-01.json").read_text(encoding="utf-8"))
        self.assertEqual(per_batch["endReason"], "ALL_LEGS_DONE")
        self.assertGreaterEqual(per_batch["logWriteFailures"], 1, "batch-01.json was rewritten once the diagnostic's log failure was counted")
        self.assertLessEqual(per_batch["logWriteFailures"], res["logWriteFailures"], "lines logged after the rewrite (the result line) may add more, never fewer")

    def test_r2_cov_both_receipt_destinations_failing_are_both_named_and_the_clean_run_exits_13(self) -> None:
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"], res["notRun"]), (13, "RECEIPT_PERSIST_FAILED", 4, 4, 0))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")
        self.assertEqual(sorted(Path(f["file"]).name for f in res["receiptPersistFailures"]), ["batch-01.json", "batches.jsonl"])
        self.assertEqual([x["endReason"] for x in res["batches"]], ["ALL_LEGS_DONE"], "the in-memory row is kept")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_12_for_a_batch_error(self) -> None:
        self._occupy_with_directory("leg-plan.json", "batch-01.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (12, "BATCH_ERROR", 0, 4))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_7_for_a_failed_leg(self) -> None:
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), FAKE_LEG_MODES="exit3")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["failed"]), (7, 1))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_3_for_a_pre_claim_deadline(self) -> None:
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (3, "DEADLINE", 0, 4))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_11_for_an_unreconciled_open(self) -> None:
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        deadline = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", deadline, "-CancelReconcileSec", "3", FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (11, "OPEN_CANCEL_UNRECONCILED"))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    # r3 (sol r2b B1 = catalogue RPF-01): the receipt recovery returned, but the NEXT batch's BATCH_WAIT went through the throwing L outside the batch try, so a batch.log still held
    # after the first RECEIPT_PERSIST_FAILED ended the script with an untyped exit 1 and no LEASE_BATCH_RESULT. This is a FULL-RUNNER run (the real runner, a copy that only adds the
    # pause/release handshake to L), not an extracted replay: two batches (the first lease is too short for the second leg), a 4 s cooldown between them, batches.jsonl a directory,
    # batch.log held from batch 1's RECEIPT_PERSIST_FAILED through batch 2's BATCH_WAIT and let go at batch 2's CLAIM_TAKEN.
    def test_r3_b1_receipt_recovery_survives_a_later_batch_wait_whose_log_line_cannot_be_written(self) -> None:
        self._occupy_with_directory("batches.jsonl")
        args, e = self.batch_cmd(self.specs[:2], "-LeaseCooldownSec", "4", FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        r, state = self._run_with_log_held_at(args, e, "RECEIPT_PERSIST_FAILED", release_at="CLAIM_TAKEN batch=2 ")
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        self.assertTrue(state["released"], "the lock was held through BATCH_WAIT and let go only at batch 2's CLAIM_TAKEN\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (13, "RECEIPT_PERSIST_FAILED", 2, 0))
        self.assertEqual([(f["batch"], Path(f["file"]).name) for f in res["receiptPersistFailures"]], [(1, "batches.jsonl"), (2, "batches.jsonl")])
        self.assertEqual([x["endReason"] for x in res["batches"]], ["LEASE_REMAINING_SHORT", "ALL_LEGS_DONE"])
        self.assertIn(" BATCH_WAIT batch=2 reason=COOLDOWN", r.stdout, "the wait line still reaches stdout")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertNotIn(" BATCH_WAIT batch=2 ", log, "the wait line was tried while batch.log was held (the case under test)")
        self.assertIn(" CLAIM_TAKEN batch=2 ", log)
        self.assertGreaterEqual(res["logWriteFailures"], 2, "the persist diagnostic and the wait line both failed to log")
        self.assertIn("BATCH_LOG_FAILED", r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r3_b1_class_no_direct_log_call_sits_between_batches(self) -> None:
        # the class of B1 (siblings CLAIM_LAPSED_IN_GUARD and CLAIM_HELD): between the top of the batch loop and the batch try, a log line is best effort (LogBest), never the throwing L
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("while ($pending.Count -gt 0 -and $null -eq $stopReason) {")
        between = text[start:text.index('    try {\n        L "CLAIM_TAKEN', start)]
        self.assertEqual(re.findall(r'(?<![\w-])L "[A-Z_]+', between), [], "a direct L call outside the batch try throws out of the script on a held batch.log")
        self.assertIn('LogBest "BATCH_WAIT batch=', between)

    def _two_batch_plan(self, inject: str, occupy: tuple = ("batches.jsonl",), extra: tuple = ()) -> tuple[list[str], dict, tuple[str, str]]:
        """A two-leg plan whose first lease holds one leg (two batches), batches.jsonl a directory: (args, env, the runner patch that runs `inject` (PowerShell) in batch 2 just before
        the claim-time input guard)."""
        self._occupy_with_directory(*occupy)
        spec2 = self.case / "fx-b-copy.json"
        spec2.write_text(self.specs[1].read_text(encoding="utf-8"), encoding="utf-8")
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(spec2, "A", self.commit_a)])
        # the claim-time guard (r12, #705 merge: bounded by Invoke-InputGuard, found once); independent of what follows it, so the same fixture runs on a runner without the sticky hit
        anchor = "        try { $hit = Invoke-InputGuard }\n"
        args, e = self.plan_cmd(plan, *extra, FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        return args, e, (anchor, f"        if ($batchNo -eq 2) {{ {inject} }}\n" + anchor)

    def _two_batch_plan_refused_at_batch_2(self, inject: str) -> tuple[subprocess.CompletedProcess, Path]:
        args, e, patch = self._two_batch_plan(inject)
        args[args.index("-File") + 1] = str(self._patched_runner(*patch))
        return subprocess.run(args, capture_output=True, text=True, timeout=120, env=e), self.case / "fx-b-copy.json"

    def test_r3_adv_a1_a_refusal_line_that_cannot_be_logged_is_in_the_count_the_summary_reports(self) -> None:
        # adversary r3 pass 2 A1: the summary was printed BEFORE Stop-PlanRefused tried its own log line, and that append's failure was swallowed uncounted, so the authoritative
        # result under-reported logWriteFailures. batch.log is held from batch 1's RECEIPT_PERSIST_FAILED to the end: that diagnostic, the refusal line, the NOT_RUN line and the
        # result line all fail, and the summary, now the last thing written, counts all four.
        args, e, patch = self._two_batch_plan("Set-Content -LiteralPath '{}' -Value '{{}}'".format(psq(self.case / "fx-b-copy.json")))
        r, state = self._run_with_log_held_at(args, e, "RECEIPT_PERSIST_FAILED", more=(patch,))
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        m = re.search(r"LEG_PLAN_REFUSED reason=(\S+) item=2 ", r.stderr)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self._assert_refused_at_batch_2_with_the_summary(r, m.group(1))
        self.assertEqual(batch_result(r.stdout)["logWriteFailures"], 4, "the diagnostic, the refusal line, the NOT_RUN line and the result line\n" + r.stdout + r.stderr)
        self.assertNotIn("LEG_PLAN_REFUSED", (self.run_dir / "batch.log").read_text(encoding="utf-8"), "the refusal line was tried while batch.log was held (the case under test)")

    def _assert_refused_at_batch_2_with_the_summary(self, r: subprocess.CompletedProcess, reason: str) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"LEG_PLAN_REFUSED reason={reason} item=2 ", r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, reason, 1, 1))
        self.assertEqual([(f["batch"], Path(f["file"]).name) for f in res["receiptPersistFailures"]], [(1, "batches.jsonl")], "batch 1's lost receipt is summarized")
        self.assertEqual([x["endReason"] for x in res["batches"]], ["LEASE_REMAINING_SHORT"])
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, "no lease is opened for batch 2")
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r3_h3_an_input_refused_between_batches_after_a_persist_failure_still_prints_the_summary(self) -> None:
        # opus r2b H3: Stop-InputIsOutput (exit 2) after batch 1's receipt could not be written printed no LEASE_BATCH_RESULT, so receiptPersistFailures was never summarized.
        # A content refusal: the second leg's spec stops being a valid leg spec while batch 1 ran.
        r, spec2 = self._two_batch_plan_refused_at_batch_2("Set-Content -LiteralPath '{}' -Value '{{}}'".format(psq(self.case / "fx-b-copy.json")))
        m = re.search(r"LEG_PLAN_REFUSED reason=(\S+) item=2 ", r.stderr)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self._assert_refused_at_batch_2_with_the_summary(r, m.group(1))

    def test_r3_h3_an_input_that_became_a_run_output_between_batches_prints_the_summary_and_writes_nothing_into_the_run_dir(self) -> None:
        # the location refusal: the second leg's spec is now a hard link to batch-01.json. The summary is printed, and (Test-OpenOutputAlias's rule) nothing more is written into
        # -RunDir: batch.log and the linked receipt are byte-for-byte what they were when the guard ran.
        snap_log, snap_rec = self.case / "batch.log.snap", self.case / "batch-01.json.snap"
        spec2 = psq(self.case / "fx-b-copy.json")
        inject = (f"[IO.File]::Delete('{spec2}'); New-Item -ItemType HardLink -Path '{spec2}' -Value (Join-Path $RunDir 'batch-01.json') | Out-Null; "
                  f"Copy-Item -LiteralPath $batchLog -Destination '{psq(snap_log)}'; Copy-Item -LiteralPath (Join-Path $RunDir 'batch-01.json') -Destination '{psq(snap_rec)}'")
        r, _ = self._two_batch_plan_refused_at_batch_2(inject)
        self._assert_refused_at_batch_2_with_the_summary(r, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap_log.read_bytes(), "nothing was appended to batch.log after the alias was found")
        self.assertEqual((self.run_dir / "batch-01.json").read_bytes(), snap_rec.read_bytes(), "the linked input is byte-unchanged")

    def test_r4_adv_a1_the_batch_receipt_rewrite_does_not_overwrite_an_input_that_became_a_link_to_it_during_the_diagnostic(self) -> None:
        # adversary r4 A1: batch-01.json is rewritten when the batches.jsonl diagnostic's own log failure bumped logWriteFailures. The diagnostic is the window: the next leg's spec becomes
        # a hard link to batch-01.json there, after every alias guard has run, and the unconditional rewrite then overwrote the accepted input. The guard now runs right before the rewrite.
        # The diagnostic's log line is made to fail (counted) and to link the spec first, inside a copy of the runner; the linked receipt must stay what the first write left.
        snap = self.case / "batch-01.json.snap"
        spec2 = psq(self.case / "fx-b-copy.json")
        inject = (f"if (-not $script:LinkDone -and $Message.StartsWith('RECEIPT_PERSIST_FAILED', [StringComparison]::Ordinal)) {{ $script:LinkDone = $true; [IO.File]::Delete('{spec2}'); "
                  f"New-Item -ItemType HardLink -Path '{spec2}' -Value (Join-Path $RunDir 'batch-01.json') | Out-Null; Copy-Item -LiteralPath (Join-Path $RunDir 'batch-01.json') -Destination '{psq(snap)}'; "
                  "throw [IO.IOException]::new('locked') }")
        args, e, _ = self._two_batch_plan("")
        anchor_l = "function L([string]$Message) {\n"
        args[args.index("-File") + 1] = str(self._patched_runner(anchor_l, anchor_l + "    " + inject + "\n"))
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertTrue(snap.exists(), "the link was made inside the diagnostic (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual((self.run_dir / "batch-01.json").read_bytes(), snap.read_bytes(), "the receipt rewrite went through the new link and overwrote the accepted input\n" + r.stdout + r.stderr)
        self.assertIn("BATCH_RECEIPT_WRITE_SKIPPED batch=1 ", r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_SPEC_IS_RUN_OUTPUT", r.stderr + r.stdout)

    def _link_spec2_to_a_receipt_inside_the_first_diagnostic(self, target: str, occupy: tuple, restore: bool = False, extra: tuple = ()) -> tuple[subprocess.CompletedProcess, Path, Path]:
        """Two batches (batch 1 ran one leg); `occupy` receipt files are directories. The first RECEIPT_PERSIST_FAILED line, inside a copy of the runner, makes the second leg's spec a hard link to
        RunDir\\<target> (snapshotting the receipt `target` first when it exists); with `restore`, batch 2's claim guard first unlinks it and puts the original spec bytes back.
        Returns (result, the spec, the snapshot of the spec's original bytes)."""
        spec2, original = self.case / "fx-b-copy.json", self.case / "fx-b-copy.json.orig"
        ps2 = psq(spec2)
        args, e, (guard_old, guard_new) = self._two_batch_plan("", occupy, extra)
        original.write_bytes(spec2.read_bytes())
        # a receipt that does not exist yet (batches.jsonl, batch-02.json) is created AS the link to the spec; one that exists (batch-01.json) is linked from the spec's path
        make = (f"New-Item -ItemType HardLink -Path (Join-Path $RunDir '{target}') -Value '{ps2}' | Out-Null" if target != "batch-01.json"
                else f"[IO.File]::Delete('{ps2}'); New-Item -ItemType HardLink -Path '{ps2}' -Value (Join-Path $RunDir '{target}') | Out-Null")
        link = (f"if (-not $script:LinkDone -and $Message.StartsWith('RECEIPT_PERSIST_FAILED', [StringComparison]::Ordinal)) {{ $script:LinkDone = $true; {make}; "
                "throw [IO.IOException]::new('locked') }")
        anchor_l = "function L([string]$Message) {\n"
        patches = [(anchor_l, anchor_l + "    " + link + "\n")]
        if restore:
            undo = f"[IO.File]::Delete('{ps2}'); [IO.File]::Copy('{psq(original)}', '{ps2}')"
            patches.append((guard_old, f"        if ($batchNo -eq 2) {{ {undo} }}\n" + guard_old))
        args[args.index("-File") + 1] = str(self._patched_runner(*patches[0], *patches[1:]))
        return subprocess.run(args, capture_output=True, text=True, timeout=120, env=e), spec2, original

    def test_r4_adv2_a1_the_jsonl_line_is_not_appended_to_an_input_that_became_a_link_to_batches_jsonl_during_the_diagnostic(self) -> None:
        # adversary r4 pass 2 A1: the batches.jsonl append (and the pre-claim deadline path's receipt writes) had no alias guard, only the rewrite did. batch-01.json is a directory, so its
        # diagnostic runs first; in it the second leg's spec becomes batches.jsonl itself. Every receipt write now asks (Write-ReceiptFile), so the append is skipped.
        r, spec2, original = self._link_spec2_to_a_receipt_inside_the_first_diagnostic("batches.jsonl", ("batch-01.json",))
        self.assertEqual(spec2.read_bytes(), original.read_bytes(), "the receipt line was appended into the accepted input\n" + r.stdout + r.stderr)
        self.assertIn("BATCH_RECEIPT_WRITE_SKIPPED batch=1 ", r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_SPEC_IS_RUN_OUTPUT", r.stderr + r.stdout)

    def test_r4_adv2_a1_the_pre_claim_deadline_receipt_is_not_written_through_an_input_that_became_a_link_to_it(self) -> None:
        # adversary r4 pass 2 A1, sibling (the pre-claim deadline path, which writes batch-02.json before any claim guard): the second leg's spec becomes batch-02.json inside batch 1's
        # diagnostic, and a 1800 s cooldown sends batch 2 to the deadline path, whose receipt write would overwrite the spec.
        r, spec2, original = self._link_spec2_to_a_receipt_inside_the_first_diagnostic("batch-02.json", ("batch-01.json",), extra=("-LeaseCooldownSec", "1800"))
        self.assertEqual(spec2.read_bytes(), original.read_bytes(), "the deadline path's receipt was written into the accepted input\n" + r.stdout + r.stderr)
        self.assertIn("BATCH_RECEIPT_WRITE_SKIPPED batch=1 ", r.stderr)   # batch-01.json is a directory (no rewrite guard behind it): the batches.jsonl append is the guarded write
        self.assertIn("BATCH_RECEIPT_NOT_WRITTEN batch=2 ", r.stderr)

    def test_r4_adv2_a2_an_alias_seen_while_a_receipt_was_written_stays_seen_after_the_link_is_gone(self) -> None:
        # adversary r4 pass 2 A2: the skipped rewrite turned the suppression on but saved no hit, so a link removed (and the spec restored) before batch 2's claim guard let batch 2 write
        # leg-plan.json, open a lease and run a probe and a leg despite the announced suppression. The hit is sticky: batch 2's guard re-reports it, so the run ends in the typed refusal.
        r, spec2, original = self._link_spec2_to_a_receipt_inside_the_first_diagnostic("batch-01.json", ("batches.jsonl",), restore=True)
        self.assertEqual(spec2.read_bytes(), original.read_bytes(), "the spec was restored by the test before batch 2's guard (the case under test)")
        m = re.search(r"LEG_PLAN_REFUSED reason=(\S+) item=2 ", r.stderr)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self.assertEqual(m.group(1), "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, "no lease is opened for batch 2")
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, "LEG_SPEC_IS_RUN_OUTPUT", 1, 1))

    def test_r4_adv_a1_the_batch_receipt_rewrite_still_happens_when_no_input_is_a_run_output(self) -> None:
        # the control: the same diagnostic log failure with no link rewrites batch-01.json with the count (r2's second per-batch write), so the new guard changes nothing else
        anchor_l = "function L([string]$Message) {\n"
        args, e, _ = self._two_batch_plan("")
        args[args.index("-File") + 1] = str(self._patched_runner(anchor_l, anchor_l + "    if ($Message.StartsWith('RECEIPT_PERSIST_FAILED', [StringComparison]::Ordinal)) { throw [IO.IOException]::new('locked') }\n"))
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        self.assertGreaterEqual(json.loads((self.run_dir / "batch-01.json").read_text(encoding="utf-8"))["logWriteFailures"], 1, "batch-01.json was rewritten with the diagnostic's log failure")
        self.assertNotIn("BATCH_RECEIPT_WRITE_SKIPPED", r.stderr)

    # r5 (opus r4b B1 = sol r4b B1 = adversary r4b OPEN A1; catalogue RPF-04, RPF-05; folds LEASE-BATCH-FINAL-RECEIPT-ALIAS-EXIT-1): a receipt write the input guard skipped recorded nothing,
    # and only the NEXT batch's claim guard read the saved hit, so on the final batch the run exited 0 with neither receipt on disk. And an input that could not be opened (a spec that already
    # ran, deleted) was taken for "an input is a runner output". Each case changes an input inside a copy of the runner, at the receipt write it names.
    _FINAL_RECEIPT = "        Write-BatchReceipt $b\n    }\n}\nWrite-RunSummary\n"
    _PRECLAIM_DEADLINE = "reason=DEADLINE detail=$($b.endDetail)\"   # r7 (sol r6b): the line is tried before the receipt snapshots logWriteFailures\n"
    _CLAIM_HELD_DEADLINE = "reason=DEADLINE detail=claim-held\"   # r7 (sol r6b): the line is tried before the receipt snapshots logWriteFailures\n"

    def _r5_spec(self, name: str, src: Path) -> Path:
        p = self.case / "r5" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(src.read_bytes())
        return p

    def _r5_link_to_batch_log(self, spec: Path) -> tuple[str, Path]:
        """PowerShell that makes `spec` a hard link to RunDir\\batch.log and snapshots batch.log; (the code, the snapshot)."""
        snap, s = self.case / "r5-batch.log.snap", psq(spec)
        return (f"[IO.File]::Delete('{s}'); New-Item -ItemType HardLink -Path '{s}' -Value (Join-Path $RunDir 'batch.log') | Out-Null; "
                f"Copy-Item -LiteralPath (Join-Path $RunDir 'batch.log') -Destination '{psq(snap)}'"), snap

    def _r5_run(self, args: list[str], e: dict, anchor: str, inject: str, batch: int = 0) -> subprocess.CompletedProcess:
        """Run `args` on a runner copy that runs `inject` at `anchor` (before the final receipt write, in batch `batch` when > 0; after a deadline anchor's line)."""
        if anchor == self._FINAL_RECEIPT:
            new = f"        if ({'$true' if batch <= 0 else f'$batchNo -eq {batch}'}) {{ {inject} }}\n" + anchor
        else:
            new = anchor + ("            " if anchor == self._PRECLAIM_DEADLINE else "        ") + inject + "\n"
        args[args.index("-File") + 1] = str(self._patched_runner(anchor, new))
        return subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)

    def _r5_assert_suppressed(self, res: dict, reason: str, batches: tuple = (1,)) -> None:
        got = sorted((f["batch"], Path(f["file"]).name) for f in res["receiptPersistFailures"])
        self.assertEqual(got, sorted((b, n) for b in batches for n in (f"batch-{b:02d}.json", "batches.jsonl")), res["receiptPersistFailures"])
        for f in res["receiptPersistFailures"]:
            self.assertTrue(f["reason"].startswith(f"WRITE_SUPPRESSED {reason}"), f)
            self.assertEqual(f["suppressedBy"], reason, f)

    def test_r5_b1_rpf04_a_final_batch_alias_seen_at_the_receipt_write_is_a_typed_nonzero_exit_naming_both_receipts(self) -> None:
        for form in ("plan", "scalar"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                link, snap = self._r5_link_to_batch_log(spec)
                args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)])) if form == "plan" else self.batch_cmd([spec])
                r = self._r5_run(args, e, self._FINAL_RECEIPT, link)
                self.assertTrue(snap.exists(), "the link was made before the final receipt write (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, "a lost receipt was an exit 0\n" + r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "LEG_SPEC_IS_RUN_OUTPUT", 1))
                self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
                self.assertFalse((self.run_dir / "batch-01.json").exists())
                self.assertFalse((self.run_dir / "batches.jsonl").exists())
                self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes(), "nothing was appended to the linked batch.log")
                self.assertEqual(res["logWriteFailures"], 0, "the suppressed result-line append is no log failure")
                self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
                self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r5_b1_rpf05_a_final_batch_spec_that_already_ran_and_was_deleted_is_input_vanished_with_the_receipts_written(self) -> None:
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(spec)}')")
        self.assertFalse(spec.exists(), "the spec was deleted (the case under test)")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"], res["logWriteFailures"]), (2, "INPUT_VANISHED", 1, [], 0))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr, "a deleted input is not a runner output")
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "a deleted input suppresses no receipt")
        [b] = self.batches()
        self.assertEqual(b["endReason"], "ALL_LEGS_DONE")
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=INPUT_VANISHED item=1 ", r.stderr)

    def test_r5b_b1_a_final_batch_spec_that_is_there_but_will_not_open_is_input_unreadable_with_the_receipts_written(self) -> None:
        # r5b (item (c)'s other half): a spec that already ran is replaced by a junction to a folder that does not exist: the name is there, nothing opens through it. INPUT_UNREADABLE, not a
        # runner output: the receipts are written and the run still ends on it (exit 2), never exit 0.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        nowhere = psq(self.case / "r5" / "no-such-folder")
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(spec)}'); [void][IO.Directory]::CreateDirectory('{nowhere}'); "
                         f"New-Item -ItemType Junction -Path '{psq(spec)}' -Value '{nowhere}' | Out-Null; [IO.Directory]::Delete('{nowhere}')")
        self.assertTrue(os.path.lexists(spec) and not spec.exists(), "the spec is a dangling junction (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        # r7 (sol r6 B2; RPF-09): an input that is there and will not resolve may reach any destination through a link on its side, so no existing receipt destination is written
        # any more: both receipts are typed suppressed (batch.log, which exists, turns the suppression on), never written. The typing r5b set is unchanged.
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["logWriteFailures"]), (2, "INPUT_UNREADABLE", 1, 0))
        self._r5_assert_suppressed(res, "INPUT_UNREADABLE")
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr, "an input that will not open is not a runner output")
        self.assertFalse((self.run_dir / "batch-01.json").exists(), "no receipt is written while an input that is there will not resolve")
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=INPUT_UNREADABLE item=1 ", r.stderr)

    def test_r5b_adv_a1_an_input_replaced_by_a_folder_during_the_final_batch_is_input_unreadable_never_exit_0(self) -> None:
        # adversary r5b A1: a folder opens like a file (BACKUP_SEMANTICS), is no runner output and (scalar form, the plan file) has no content check, so an accepted input replaced by an
        # empty folder after its leg ran was no hit at all and the run ended exit 0. Each input kind: the spec (both forms), the plan file, the staged build manifest.
        for form in ("scalar spec", "plan spec", "plan file", "build manifest"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                if form == "scalar spec":
                    args, e = self.batch_cmd([spec])
                else:
                    args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                target = {"plan file": self.case / "plan.json", "build manifest": self._staged_manifest(self.commit_a)}.get(form, spec)
                r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(target)}'); [void][IO.Directory]::CreateDirectory('{psq(target)}')")
                self.assertTrue(target.is_dir(), "the input is a folder now (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, "an input that is a folder now was an exit 0\n" + r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"], res["logWriteFailures"]), (2, "INPUT_UNREADABLE", 1, [], 0))
                self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                self.assertTrue((self.run_dir / "batch-01.json").is_file(), "a folder at an input's name suppresses no receipt")
                self.assertEqual(len(self.batches()), 1)
                self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=INPUT_UNREADABLE", r.stderr)

    def test_r5b_adv2_a1_a_staged_manifest_gone_after_the_final_leg_ends_the_run_on_it_never_exit_0(self) -> None:
        # adversary r5b pass 2 A1: a staged manifest that is gone skips the location guard (BUILD_NOT_STAGED is the content guard's), and the receipt guard kept only unresolved hits,
        # so that content hit was dropped and the run ended exit 0. Every hit the receipt guard sees is kept: the receipts are written and the run ends on it.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        manifest = self._staged_manifest(self.commit_a)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(manifest)}')")
        self.assertFalse(manifest.exists(), "the staged manifest was deleted (the case under test)")
        self.assertEqual(r.returncode, 2, "a manifest gone after the final leg was an exit 0\n" + r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"]), (2, "BUILD_NOT_STAGED", 1, []))
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        self.assertEqual(len(self.batches()), 1)
        self.assertIn("BATCH_RECEIPT_INPUT_CHANGED batch=1 reason=BUILD_NOT_STAGED", r.stderr)
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=BUILD_NOT_STAGED", r.stderr)

    def test_r5b_adv2_a2_a_spec_whose_data_cannot_be_read_is_input_unreadable_never_exit_0(self) -> None:
        # adversary r5b pass 2 A2: the guard's opens ask for no access, so a spec whose FILE_READ_DATA is denied (metadata still readable) passed them, and the scalar form has no
        # content check: exit 0. The input is now opened for reading: INPUT_UNREADABLE, the receipts written, exit 2.
        for form in ("scalar", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                self.addCleanup(subprocess.run, ["icacls", str(spec), "/reset"], capture_output=True)
                args, e = self.batch_cmd([spec]) if form == "scalar" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                deny = f"$null = icacls '{psq(spec)}' /deny ([Security.Principal.WindowsIdentity]::GetCurrent().Name + ':(RD)')"
                r = self._r5_run(args, e, self._FINAL_RECEIPT, deny)
                with self.assertRaises(PermissionError, msg="the spec's data cannot be read (the case under test)"):
                    spec.read_bytes()
                self.assertEqual(r.returncode, 2, "an unreadable spec was an exit 0\n" + r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"]), (2, "INPUT_UNREADABLE", 1, []))
                self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                self.assertTrue((self.run_dir / "batch-01.json").is_file())
                subprocess.run(["icacls", str(spec), "/reset"], capture_output=True)

    def test_r5b_adv_a2_a_plan_missing_only_while_one_guard_ran_is_no_alias_and_suppresses_nothing(self) -> None:
        # adversary r5b A2: the guard failed to open the plan (moved away in a save-as-rename), the plan was back before Get-InputUnresolvedCode probed it, and that stale failure was
        # labelled LEG_PLAN_IS_RUN_OUTPUT: both receipts suppressed, exit 2. Only a refusal reached with the input itself open is an alias; this one is asked again and is clean.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        p, away = psq(plan), psq(plan) + ".away"
        # the probe's own signature, whichever form the runner has (r5b pass 2 added -SkipRead)
        anchor = next(a for a in ("function Get-InputUnresolvedCode([string]$Path, [switch]$SkipRead) {\n", "function Get-InputUnresolvedCode([string]$Path) {\n")
                      if a in RUNNER.read_text(encoding="utf-8"))
        restore = anchor + f"    if ([IO.File]::Exists('{away}')) {{ [IO.File]::Move('{away}', '{p}') }}\n"
        new = f"        if ($true) {{ [IO.File]::Move('{p}', '{away}') }}\n" + self._FINAL_RECEIPT
        args[args.index("-File") + 1] = str(self._patched_runner(self._FINAL_RECEIPT, new, (anchor, restore)))
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        self.assertTrue(plan.is_file() and not Path(str(plan) + ".away").exists(), "the plan was moved away and back (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["receiptPersistFailures"], res["logWriteFailures"]), (0, None, [], 0))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        self.assertNotIn("BATCH_RECEIPT_WRITE_SKIPPED", r.stderr)
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        self.assertEqual(len(self.batches()), 1)

    def test_r5_b1_a_leg_plan_moved_away_during_the_final_batch_is_input_vanished_and_one_replaced_in_full_is_clean(self) -> None:
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Move('{psq(plan)}', '{psq(plan)}.away')")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["receiptPersistFailures"]), ("INPUT_VANISHED", []))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        # the control: the plan replaced in one step (rename over it) is never missing, so the run is clean: exit 0, both receipts on disk
        self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
        self._stage_case()
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Copy('{psq(plan)}', '{psq(plan)}.new'); [IO.File]::Move('{psq(plan)}.new', '{psq(plan)}', $true)")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["receiptPersistFailures"]), (0, None, []))
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        self.assertEqual(len(self.batches()), 1)

    # PR 704 r6 (sol r5b B1 = Dot A1 = adversary dc9b06a6 OPEN A1; card LEASE-BATCH-ZONE-UNRESOLVED-LABEL-1; catalogue RPF-06): an OUTPUT the guard could not inspect at the final receipt write
    # (a dangling runner-owned link in -RunDir, a link that will not resolve, a zone that will not list, an output that will not identify) was caught as "the input cannot be resolved" and
    # labelled an input alias: both writable receipts suppressed, stop LEG_*_IS_RUN_OUTPUT, though no input aliased an output. It is OUTPUT_ZONE_UNRESOLVED now, exit 2, the receipts written.
    _RPF06_ZONE_LIST = "            foreach ($c in @(Get-ChildItem -LiteralPath $z.root -Force -ErrorAction Stop | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) {\n"
    _RPF06_SCAN_ID = "                    if (((Get-FileIdentity $c.FullName).key -ceq $id.key)"
    _RPF06_RESOLVE = "[VenueLeaseBatchFinalPath]::Resolve([IO.Path]::GetFullPath($Path))"

    def _rpf06_dangling(self, at: Path) -> str:
        """PowerShell that makes `at` a junction to a folder that no longer exists (the link is there; nothing resolves through it)."""
        nowhere = psq(self.case / "rpf06-no-such-folder")
        return f"[void][IO.Directory]::CreateDirectory('{nowhere}'); New-Item -ItemType Junction -Path '{psq(at)}' -Value '{nowhere}' | Out-Null; [IO.Directory]::Delete('{nowhere}')"

    def _rpf06_run(self, args: list[str], e: dict, inject: str, *more: tuple[str, str]) -> subprocess.CompletedProcess:
        args[args.index("-File") + 1] = str(self._patched_runner(self._FINAL_RECEIPT, f"        if ($true) {{ {inject} }}\n" + self._FINAL_RECEIPT, *more))
        return subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)

    def _rpf06_assert_typed_and_written(self, r: subprocess.CompletedProcess) -> dict:
        self.assertEqual(r.returncode, 2, "an output that could not be inspected was not its own typed stop\n" + r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"]), (2, "OUTPUT_ZONE_UNRESOLVED", 1, []), r.stderr)
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr, "no input aliases an output")
        self.assertNotIn("BATCH_RECEIPT_WRITE_SKIPPED", r.stderr)
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "a receipt no real alias threatens is written")
        self.assertEqual(len(self.batches()), 1, "batches.jsonl has its line")
        self.assertIn("BATCH_RECEIPT_OUTPUT_UNRESOLVED batch=1 reason=OUTPUT_ZONE_UNRESOLVED", r.stderr)
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=OUTPUT_ZONE_UNRESOLVED", r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        return res

    def test_rpf06_b1_a_dangling_runner_owned_link_at_the_final_receipt_is_output_zone_unresolved_never_an_input_alias(self) -> None:
        # (a) scalar spec, (b) plan: RunDir\gate.log (runner-owned) becomes a link to nothing after the final BATCH_END; the inputs are untouched and open
        for form in ("scalar spec", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.batch_cmd([spec]) if form == "scalar spec" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                gate = self.run_dir / "gate.log"
                r = self._rpf06_run(args, e, f"[IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate))
                self.assertTrue(os.path.lexists(gate) and not gate.exists(), "gate.log is a dangling link (the case under test)\n" + r.stdout + r.stderr)
                self._rpf06_assert_typed_and_written(r)

    def test_rpf06_c_an_output_that_will_not_identify_in_a_build_manifests_hard_link_scan_is_output_zone_unresolved(self) -> None:
        # (c) the staged build manifest has a second name (outside every zone), so only its guard scans the outputs by identity, and that scan throws: BUILD_MANIFEST_IS_RUN_OUTPUT before
        manifest = self._staged_manifest(self.commit_a)
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        other = self.case / "rpf06-manifest-second-name.json"
        r = self._rpf06_run(args, e, f"New-Item -ItemType HardLink -Path '{psq(other)}' -Value '{psq(manifest)}' | Out-Null; $script:rpf06ScanThrow = $true",
                            (self._RPF06_SCAN_ID, "                    if ($script:rpf06ScanThrow) { throw 'injected: an output will not identify' }\n" + self._RPF06_SCAN_ID))
        self.assertTrue(other.is_file(), "the manifest has a second name (the case under test)\n" + r.stdout + r.stderr)
        self._rpf06_assert_typed_and_written(r)
        self.assertIn("injected: an output will not identify", r.stderr, "the reason names the output's failure")

    def test_rpf06_d_an_output_link_whose_resolution_throws_after_the_inputs_open_is_output_zone_unresolved(self) -> None:
        # (d) gate.log is a junction to a folder that exists (outside every input), and resolving it throws: the inputs opened first, so the throw is the output's
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        gate, there = self.run_dir / "gate.log", self.case / "rpf06-gate-target"
        r = self._rpf06_run(args, e, f"[IO.File]::Delete('{psq(gate)}'); [void][IO.Directory]::CreateDirectory('{psq(there)}'); New-Item -ItemType Junction -Path '{psq(gate)}' -Value '{psq(there)}' | Out-Null; $script:rpf06ResolveThrow = $true",
                            (self._RPF06_RESOLVE, f"if ($script:rpf06ResolveThrow -and $Path -like '*\\gate.log') {{ throw 'injected: the output link will not resolve' }}; " + self._RPF06_RESOLVE))
        self.assertTrue(gate.is_dir(), "gate.log is a junction (the case under test)\n" + r.stdout + r.stderr)
        self._rpf06_assert_typed_and_written(r)

    def test_rpf06_e_an_output_zone_that_will_not_list_at_the_final_receipt_is_output_zone_unresolved(self) -> None:
        # (e) the -RunDir zone's listing throws (scalar and plan)
        for form in ("scalar spec", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.batch_cmd([spec]) if form == "scalar spec" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                r = self._rpf06_run(args, e, "$script:rpf06ZoneThrow = $true",
                                    (self._RPF06_ZONE_LIST, "            if ($script:rpf06ZoneThrow) { throw 'injected: the zone will not list' }\n" + self._RPF06_ZONE_LIST))
                self._rpf06_assert_typed_and_written(r)

    def test_rpf06_f_a_dangling_link_at_a_receipts_own_name_suppresses_that_receipt_only(self) -> None:
        # the file-by-file check: batch-01.json itself is a link to nothing, so where its write lands is unknown: that receipt is suppressed (typed), batches.jsonl is still written
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        per = self.run_dir / "batch-01.json"
        r = self._rpf06_run(args, e, self._rpf06_dangling(per))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "OUTPUT_ZONE_UNRESOLVED", 1))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        self.assertEqual([Path(f["file"]).name for f in res["receiptPersistFailures"]], ["batch-01.json"], res["receiptPersistFailures"])
        [f] = res["receiptPersistFailures"]
        self.assertEqual(f["suppressedBy"], "OUTPUT_ZONE_UNRESOLVED")
        self.assertIn("this receipt file only", f["reason"])
        self.assertEqual(len(self.batches()), 1, "batches.jsonl is still written")

    def test_rpf06_g_a_real_alias_the_unlistable_zone_hides_still_suppresses_every_write(self) -> None:
        # the control the per-file check must keep: the zone will not list, so the guard cannot see that the spec is a hard link to batch.log; the batch.log check sees it and turns the
        # suppression on: no receipt is written, nothing is appended to batch.log, both lost receipts are named, exit 2
        # r9 (sol r8 B1; RPF-10): typed by batch.log's own hit (the spec IS batch.log: LEG_SPEC_IS_RUN_OUTPUT), no longer by the earlier OUTPUT_ZONE_UNRESOLVED that hid it
        spec = self._r5_spec("fx-a.json", self.specs[0])
        link, snap = self._r5_link_to_batch_log(spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        r = self._rpf06_run(args, e, link + "; $script:rpf06ZoneThrow = $true",
                            (self._RPF06_ZONE_LIST, "            if ($script:rpf06ZoneThrow) { throw 'injected: the zone will not list' }\n" + self._RPF06_ZONE_LIST))
        self.assertTrue(snap.exists(), "the link was made (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (2, "LEG_SPEC_IS_RUN_OUTPUT"))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertFalse((self.run_dir / "batch-01.json").exists())
        self.assertFalse((self.run_dir / "batches.jsonl").exists())
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes(), "nothing was appended to the linked batch.log")
        self.assertIn("BATCH_OUTPUT_UNVOUCHED batch=1 reason=LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
        self.assertIn("overall guard hit OUTPUT_ZONE_UNRESOLVED", r.stderr)

    # PR 704 r9 (sol r8 B1 = producer OPEN r8p2-A1; catalogue RPF-10): with an output the guard could not inspect, the receipt guard asked batch.log about its own destination but kept
    # only the sentence of the hit it found (Get-ReceiptDestinationRefusal), and typed the suppression and the stop by the earlier OUTPUT_ZONE_UNRESOLVED hit: a batch.log that IS an
    # accepted input was reported as an output nobody could inspect. The destination's own typed hit (kind, code, item) is the suppression's and the stop's now.
    def test_r9_rpf10_a_batch_log_that_is_an_accepted_input_behind_an_unresolved_zone_is_typed_by_its_own_alias(self) -> None:
        for form, code in (("spec", "LEG_SPEC_IS_RUN_OUTPUT"), ("plan", "LEG_PLAN_IS_RUN_OUTPUT"), ("build", "BUILD_MANIFEST_IS_RUN_OUTPUT")):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                plan = self.write_plan([self.item(spec, "A", self.commit_a)])
                args, e = self.plan_cmd(plan)
                target = {"spec": spec, "plan": plan, "build": self._staged_manifest(self.commit_a)}[form]
                gate, log, snap = self.run_dir / "gate.log", self.run_dir / "batch.log", self.case / "r9-target.snap"
                # after the final BATCH_END: gate.log (runner-owned) is a link to nothing, and batch.log is replaced by a hard link to the accepted input
                inject = (f"[IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate) + f"; [IO.File]::Delete('{psq(log)}'); "
                          f"New-Item -ItemType HardLink -Path '{psq(log)}' -Value '{psq(target)}' | Out-Null; Copy-Item -LiteralPath '{psq(target)}' -Destination '{psq(snap)}'")
                r = self._rpf06_run(args, e, inject)
                self.assertTrue(snap.exists() and os.path.lexists(gate) and not gate.exists(), "the case under test was set up\n" + r.stdout + r.stderr)
                self.assertEqual(os.stat(log).st_ino, os.stat(target).st_ino, "batch.log is the accepted input (the case under test)")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"]), (2, code), r.stderr)
                self._r5_assert_suppressed(res, code)
                self.assertEqual(target.read_bytes(), snap.read_bytes(), "nothing was written into the input through batch.log")
                self.assertIn(f"BATCH_RECEIPT_GUARD_STOP reason={code}", r.stderr)
                self.assertIn(f"BATCH_WRITE_REFUSED file={log} reason={code}", r.stderr)

    def test_r9_adv_a1_an_earlier_input_that_will_not_resolve_never_hides_a_later_inputs_alias(self) -> None:
        # adversary r9 A1 (the RPF-10 class inside Get-WriteDestinationHit): the destination check returned INPUT_UNREADABLE for the first input that would not resolve (the plan) before
        # comparing a later input's accepted identity, so a batch.log that IS the accepted spec was typed INPUT_UNREADABLE and the stop stayed OUTPUT_ZONE_UNRESOLVED. RED on a01ce8f0.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        gate, log, snap = self.run_dir / "gate.log", self.run_dir / "batch.log", self.case / "r9-spec.snap"
        inject = (f"[IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate) + f"; [IO.File]::Delete('{psq(log)}'); "
                  f"New-Item -ItemType HardLink -Path '{psq(log)}' -Value '{psq(spec)}' | Out-Null; Copy-Item -LiteralPath '{psq(spec)}' -Destination '{psq(snap)}'; $script:r9DenyPlan = $true")
        deny = (self._RPF06_RESOLVE, f"if ($script:r9DenyPlan -and $Path -ieq '{psq(plan)}') {{ throw [UnauthorizedAccessException]::new('injected: the plan path will not resolve') }}; " + self._RPF06_RESOLVE)
        r = self._rpf06_run(args, e, inject, deny)
        self.assertTrue(snap.exists() and os.path.lexists(gate) and not gate.exists(), "the case under test was set up\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (2, "LEG_SPEC_IS_RUN_OUTPUT"), r.stderr)
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual(spec.read_bytes(), snap.read_bytes(), "nothing was written into the spec through batch.log")

    # PR 704 r9 (sol r8 B2 = producer OPEN r8p2-A2; catalogue RPF-11): a receipt destination that is there but will not resolve (renamed between Test-NothingAtName and Get-PhysicalPath)
    # says nothing about any input or any other file, yet with no overall hit (or a content one) the receipt guard asked the general write guard, which turned the suppression on for
    # every write: the healthy sibling receipt and the summary's batch.log line were lost too. Only that destination's write is suppressed now, whatever the overall hit; a destination
    # that IS an accepted input still suppresses every write. Injected I/O seam, no real race.
    _R9_RESOLVE = "    try { $phys = (Get-PhysicalPath $Path).TrimEnd('\\'); $di = Get-FileIdentity $Path }\n"
    _R9_JSONL_WRITE = "    Write-ReceiptFile $B.batch $jsonl { $rec.logWriteFailures"

    def test_r9_rpf11_a_receipt_destination_that_will_not_resolve_suppresses_that_receipt_only(self) -> None:
        # (form, the destination that will not resolve, made before the write, the overall hit's setup, expected exit and stop)
        forms = (("initial, no overall hit", "batch-01.json", True, "", 13, "RECEIPT_PERSIST_FAILED"),
                 ("jsonl, no overall hit", "batches.jsonl", True, "", 13, "RECEIPT_PERSIST_FAILED"),
                 ("rewrite, no overall hit", "batch-01.json", False, "", 13, "RECEIPT_PERSIST_FAILED"),
                 ("initial, content hit", "batch-01.json", True, "content", 2, "BUILD_NOT_STAGED"),
                 ("jsonl, content hit", "batches.jsonl", True, "content", 2, "BUILD_NOT_STAGED"),
                 ("initial, output hit (control)", "batch-01.json", True, "output", 2, "OUTPUT_ZONE_UNRESOLVED"))
        for form, leaf, made, overall, exit_code, stop in forms:
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                dest, gate = self.run_dir / leaf, self.run_dir / "gate.log"
                inject = f"$script:r9Throw = '{leaf}'"
                if made:
                    inject += f"; [IO.File]::WriteAllText('{psq(dest)}', '')"
                else:
                    inject += "; $script:r9Bump = $true"   # a log failure counted after the first write, so batch-NN.json is written once more (the rewrite destination)
                if overall == "content":
                    inject += f"; [IO.File]::Delete('{psq(self._staged_manifest(self.commit_a))}')"
                elif overall == "output":
                    inject += f"; [IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate)
                r = self._rpf06_run(args, e, inject,
                                    (self._R9_RESOLVE, "    try { if ($script:r9Throw -and [IO.Path]::GetFileName($Path) -ieq $script:r9Throw) { throw 'injected: the receipt was renamed between its observation and its resolution' }; $phys = (Get-PhysicalPath $Path).TrimEnd('\\'); $di = Get-FileIdentity $Path }\n"),
                                    (self._R9_JSONL_WRITE, "    if ($script:r9Bump) { $script:logWriteFailures++ }\n" + self._R9_JSONL_WRITE))
                self.assertEqual(r.returncode, exit_code, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (exit_code, stop, 1), r.stderr)
                self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                self.assertEqual([Path(f["file"]).name for f in res["receiptPersistFailures"]], [leaf], res["receiptPersistFailures"])
                [f] = res["receiptPersistFailures"]
                self.assertEqual(f["suppressedBy"], "OUTPUT_ZONE_UNRESOLVED", f)
                self.assertIn("injected: the receipt was renamed", f["reason"], "the reason is the destination's own hit")
                self.assertIn("this receipt file only", f["reason"])
                if leaf == "batches.jsonl":
                    self.assertEqual(self.batches(), [], "the destination that will not resolve is not written")
                    self.assertTrue((self.run_dir / "batch-01.json").is_file(), "the healthy batch-01.json is written")
                else:
                    self.assertEqual(len(self.batches()), 1, "the healthy batches.jsonl is written")
                    if made:
                        self.assertEqual((self.run_dir / "batch-01.json").read_bytes(), b"", "the destination that will not resolve is not written")
                    else:
                        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "the first write landed before its rewrite was refused")
                self.assertNotIn("BATCH_RESULT_NOT_LOGGED", r.stderr, "a destination-only refusal turned the suppression on for every write")
                self.assertIn("LEASE_BATCH_RESULT=", (self.run_dir / "batch.log").read_text(encoding="utf-8"), "the summary line is still appended to batch.log")

    def test_r6_adv_a1_an_input_whose_lookup_is_denied_is_input_unreadable_never_input_vanished(self) -> None:
        # adversary r6 A1: the input is still there, but its own ACL denies everything and its folder denies listing, so resolving it AND looking it up are access denied. Get-Item (and,
        # for a staged manifest, Test-Path) read that as "nothing there": INPUT_VANISHED / skipped as gone. Only "not found" is gone: INPUT_UNREADABLE, the receipts written, exit 2.
        for form in ("scalar spec", "build manifest"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                if form == "scalar spec":
                    args, e = self.batch_cmd([spec])
                    target = spec
                else:
                    args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                    target = self._staged_manifest(self.commit_a)
                me = "([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
                for p in (target, target.parent):
                    self.addCleanup(subprocess.run, ["icacls", str(p), "/remove:d", os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")], capture_output=True)
                deny = (f"$null = icacls '{psq(target)}' /deny ({me} + ':(F)'); $null = icacls '{psq(target.parent)}' /deny ({me} + ':(RD)')")
                r = self._rpf06_run(args, e, deny)
                try:
                    with self.assertRaises(PermissionError, msg="the input's lookup is denied (the case under test)"):
                        os.stat(target)
                    self.assertEqual(r.returncode, 2, "an input whose lookup was denied ended exit 0\n" + r.stdout + r.stderr)
                    res = batch_result(r.stdout)
                    # r7 (sol r6 B2; RPF-09): the input is there and will not resolve, so no existing destination is written (typed suppressed, never written); the typing is unchanged
                    self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "INPUT_UNREADABLE", 1), r.stderr)
                    self._r5_assert_suppressed(res, "INPUT_UNREADABLE")
                    self.assertNotIn("INPUT_VANISHED", r.stdout + r.stderr, "an input that is still there is not gone")
                    self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                    self.assertFalse((self.run_dir / "batch-01.json").exists())
                finally:
                    for p in (target.parent, target):
                        subprocess.run(["icacls", str(p), "/remove:d", os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")], capture_output=True)

    def test_r6_adv2_a1_a_log_that_is_an_input_whose_own_path_will_not_resolve_is_never_written(self) -> None:
        # adversary r6 pass 2 A1: the spec's own path stops resolving (as a share that now denies it; injected, since a share needs admin) while the file is batch.log by another name.
        # The guard has no answer for that input (INPUT_UNREADABLE, no alias), so nothing compared batch.log with it and the summary was appended INTO the spec. batch.log is now
        # asked on its own: as the input was accepted (batch.log re-linked to it), or, while the input does not resolve, any batch.log with another name: nothing is written into -RunDir.
        for form in ("input re-linked to batch.log", "batch.log re-linked to the input"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                s, log = psq(spec), psq(self.run_dir / "batch.log")
                snap = self.case / "rpf06-spec.snap"
                relink = (f"[IO.File]::Delete('{s}'); New-Item -ItemType HardLink -Path '{s}' -Value '{log}' | Out-Null" if form.startswith("input")
                          else f"[IO.File]::Copy('{log}', '{s}', $true); [IO.File]::Delete('{log}'); New-Item -ItemType HardLink -Path '{log}' -Value '{s}' | Out-Null")
                inject = f"{relink}; Copy-Item -LiteralPath '{s}' -Destination '{psq(snap)}'; $script:rpf06DenySpec = $true"
                deny = (self._RPF06_RESOLVE, f"if ($script:rpf06DenySpec -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE)
                r = self._rpf06_run(args, e, inject, deny)
                self.assertTrue(snap.exists(), "the case under test was set up\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                # r9 (sol r8 B1; RPF-10): typed by batch.log's own hit. Re-linked to the input, batch.log IS the spec as it was accepted (same file identity): LEG_SPEC_IS_RUN_OUTPUT,
                # which the earlier INPUT_UNREADABLE hid. With the input re-linked to batch.log, batch.log keeps its own identity (the accepted spec file is gone) and the spec's path will not resolve: INPUT_UNREADABLE.
                code = "INPUT_UNREADABLE" if form.startswith("input") else "LEG_SPEC_IS_RUN_OUTPUT"
                self.assertEqual((res["exitCode"], res["stopReason"]), (2, code), r.stderr)
                self.assertEqual(spec.read_bytes(), snap.read_bytes(), "nothing was written into the input through batch.log")
                self._r5_assert_suppressed(res, code)
                self.assertIn(f"BATCH_OUTPUT_UNVOUCHED batch=1 reason={code}", r.stderr)

    # PR 704 r7 (key3 opus r6 = sol r6 B1/B2 = Dot 16:48Z; catalogue RPF-08, RPF-09; folds LEASE-BATCH-INPUT-ALIAS-RESOLUTION-FAIL-REGRESSION-1): an accepted input that no longer resolves
    # (INPUT_VANISHED / INPUT_UNREADABLE) turned no write suppression on at the open or at a later claim, so the finally's log lines and the summary were appended into a batch.log that was
    # that input; and a destination with one name and no link was written while an input that is there would not resolve. Every writer into -RunDir now asks one guard right before it writes.
    def _r7_wait_for_event(self, p: subprocess.Popen, kind: str) -> None:
        end = time.monotonic() + 120
        while time.monotonic() < end and p.poll() is None and not (self.events.exists() and f" {kind} " in self.events.read_text(encoding="utf-8")):
            time.sleep(0.05)
        self.assertTrue(self.events.exists() and f" {kind} " in self.events.read_text(encoding="utf-8"), f"no {kind} event (the case under test)")

    def test_r7_dot_a_spec_renamed_and_batch_log_relinked_during_the_real_open_wait_is_never_appended_to(self) -> None:
        # Dot 16:48Z, no resolver mock: spec A is accepted; while the open waits in the agent's gate (FAKE_OPEN_SLEEP) A is renamed to A.old, batch.log is replaced by a hard link to
        # A.old and A stays absent. The guard reads A as INPUT_VANISHED, which suppressed nothing, so the finally's log lines went into A.old.
        for form in ("plan", "scalar"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                old = spec.with_name("fx-a.json.old")
                args, e = (self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), FAKE_OPEN_SLEEP="8") if form == "plan"
                           else self.batch_cmd([spec], FAKE_OPEN_SLEEP="8"))
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                try:
                    self._r7_wait_for_event(p, "OPENWAIT")
                    os.rename(spec, old)
                    log = self.run_dir / "batch.log"
                    log.unlink()
                    os.link(old, log)
                    snap = old.read_bytes()
                    out, err = p.communicate(timeout=300)
                finally:
                    if p.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                        p.communicate(timeout=60)
                self.assertFalse(spec.exists(), "A stays absent (the case under test)")
                self.assertEqual(old.read_bytes(), snap, "a write landed in the accepted input through batch.log\n" + out + err)
                self.assertEqual(p.returncode, 2, out + err)
                res = batch_result(out)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "INPUT_VANISHED", 0), err)
                self.assertNotIn("IS_RUN_OUTPUT", out + err)

    def test_r7_sol_b2_a_one_name_receipt_is_not_written_while_an_input_that_is_there_will_not_resolve(self) -> None:
        # sol r6 B2 (RPF-09): the spec is accepted through a junction (cur -> orig). Before the final receipt the junction is retargeted at -RunDir, where a one-name copy of the spec sits
        # at the receipt's name, and resolving the input path is denied (injected, as a share that now denies it). links == 1 and no reparse point prove nothing about the input side.
        orig, cur, snap = self.case / "r7" / "orig", self.case / "r7" / "cur", self.case / "r7-spec.snap"
        orig.mkdir(parents=True)
        (orig / "batch-01.json").write_bytes(self.specs[0].read_bytes())
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"New-Item -ItemType Junction -Path '{psq(cur)}' -Value '{psq(orig)}' | Out-Null"], check=True, capture_output=True)
        spec = cur / "batch-01.json"
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        s, c = psq(spec), psq(cur)
        retarget = (f"[IO.File]::Copy('{psq(orig / 'batch-01.json')}', (Join-Path $RunDir 'batch-01.json'), $true); [IO.Directory]::Delete('{c}'); "
                    f"New-Item -ItemType Junction -Path '{c}' -Value $RunDir | Out-Null; Copy-Item -LiteralPath '{s}' -Destination '{psq(snap)}'; $script:rpf06DenySpec = $true")
        deny = (self._RPF06_RESOLVE, f"if ($script:rpf06DenySpec -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE)
        r = self._rpf06_run(args, e, retarget, deny)
        self.assertTrue(snap.exists(), "the junction was retargeted (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(spec.read_bytes(), snap.read_bytes(), "a receipt was written into the file the accepted input's path now reaches\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (2, "INPUT_UNREADABLE"), r.stderr)
        self._r5_assert_suppressed(res, "INPUT_UNREADABLE")
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)

    def test_r7_sol_b1_a_later_claims_unresolved_input_refusal_appends_no_summary_into_the_input(self) -> None:
        # sol r6 B1 = key3 opus r6 (RPF-08): batches.jsonl is a directory (a receipt-persistence failure), then before batch 2's claim guard batch.log becomes a hard link to the next
        # leg's spec and the spec's own path stops resolving (denied: injected) or the spec is renamed away (vanished: no mock). The refusal printed the summary into batch.log: the
        # NOT_RUN line and LEASE_BATCH_RESULT landed in the input.
        for how in ("denied", "vanished"):
            with self.subTest(how=how):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec2 = self.case / "fx-b-copy.json"
                target = spec2 if how == "denied" else self.case / "fx-b-copy.json.old"
                s, t, log, snap = psq(spec2), psq(target), psq(self.run_dir / "batch.log"), self.case / "r7-b1.snap"
                move = "" if how == "denied" else f"[IO.File]::Move('{s}', '{t}'); "
                inject = (f"{move}[IO.File]::Delete('{log}'); New-Item -ItemType HardLink -Path '{log}' -Value '{t}' | Out-Null; Copy-Item -LiteralPath '{t}' -Destination '{psq(snap)}'"
                          + ("; $env:MLV_TEST_RPF06_DENY_SPEC = '1'" if how == "denied" else ""))
                args, e, patch = self._two_batch_plan(inject)
                patches = [patch]
                if how == "denied":
                    # r12 (#705 merge): the claim guard runs on Invoke-InputGuard's worker runspace, which gets the patched function but not this run's script variables, so the
                    # injected denial is a process-wide flag (as a real denied lookup reaches every thread)
                    patches.append((self._RPF06_RESOLVE, f"if ($env:MLV_TEST_RPF06_DENY_SPEC -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE))
                args[args.index("-File") + 1] = str(self._patched_runner(*patches[0], *patches[1:]))
                r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
                self.assertTrue(snap.exists(), "batch.log was relinked to the input (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(target.read_bytes(), snap.read_bytes(), "the summary was appended into the accepted input\n" + r.stdout + r.stderr)
                code = "INPUT_UNREADABLE" if how == "denied" else "INPUT_VANISHED"
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(f"LEG_PLAN_REFUSED reason={code} item=2 ", r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, code, 1, 1))

    # r7: one case per writer into -RunDir (and the claim dir's ledger): right before the writer, its destination becomes a hard link to the second leg's spec (an accepted input not yet
    # run). No guard ran in between, so only the writer's own guard can see it. (anchor kept by the runner, PowerShell run once just before it)
    _R7_WRITERS = {
        "batch.log (L)": ('                L "LEG $($leg.name) RUN batch=',
                          "[IO.File]::Delete((Join-Path $RunDir 'batch.log')); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch.log') -Value '{b}' | Out-Null"),
        "leg output": ("                try { $lp = Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs -TimeoutSec $legBoundSec } catch {\n",
                       "New-Item -ItemType HardLink -Path $out -Value '{b}' | Out-Null"),
        "probe output": ("    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript,", "New-Item -ItemType HardLink -Path $out -Value '{b}' | Out-Null"),
        "leg-plan.json": ('        L "CLAIM_TAKEN batch=$batchNo',
                          "New-Item -ItemType HardLink -Path (Join-Path $RunDir 'leg-plan.json') -Value '{b}' | Out-Null"),
        "lease ledger": ("                $b.ledger = $row\n",
                         "$lg = Get-VenueLeaseLedgerPath -Venue $Venue -ClaimDir $ClaimDir; if (Test-Path -LiteralPath $lg) {{ [IO.File]::Delete($lg) }}; New-Item -ItemType HardLink -Path $lg -Value '{b}' | Out-Null"),
        # adversary r7 A2: a name inside the probe's work folder (Wait-VenueQuiet writes its job files there) is already a hard link to the spec
        "probe work folder (a name inside it)": ("    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript,",
                                                 "$null = New-Item -ItemType Directory -Force -Path (Join-Path $RunDir 'quiet'); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'quiet\\venue-quiet-probe-r7.job.ps1') -Value '{b}' | Out-Null"),
        # adversary r7 pass 2 A1: the open's own job folder holds a hard link to the spec when the open is submitted
        "open job folder": ("            $op = Submit-AgentJob $b.openJobId ",
                            "New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch-jobs\\keep.json') -Value '{b}' | Out-Null"),
    }

    def test_r7_every_writer_asks_the_guard_right_before_it_writes(self) -> None:
        for name, (anchor, make) in self._R7_WRITERS.items():
            with self.subTest(writer=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                a, b = self._r5_spec("fx-a.json", self.specs[0]), self._r5_spec("fx-b.json", self.specs[1])
                snap = self.case / "r7-writer.snap"
                args, e = self.plan_cmd(self.write_plan([self.item(a, "A", self.commit_a), self.item(b, "A", self.commit_a)]))
                indent = re.match(r"\s*", anchor).group(0)
                inject = f"{indent}if (-not $script:r7Done) {{ $script:r7Done = $true; {make.format(b=psq(b))}; Copy-Item -LiteralPath '{psq(b)}' -Destination '{psq(snap)}' }}\n"
                args[args.index("-File") + 1] = str(self._patched_runner(anchor, inject + anchor))
                r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
                self.assertTrue(snap.exists(), "the destination was linked to the spec (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(b.read_bytes(), snap.read_bytes(), f"the {name} writer wrote into the accepted input\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_WRITE_REFUSED ", r.stderr)
                self.assertEqual(batch_result(r.stdout)["stopReason"], "LEG_SPEC_IS_RUN_OUTPUT")
                self.assertFalse(self.claim_path.exists(), "the claim was left behind")
                if name in ("leg-plan.json", "open job folder"):
                    # adversary r7 A3 / pass 2 A1: a refusal before the open stopped the claim keeper, so no open is submitted after it (it could outlive the claim)
                    self.assertNotIn("OPEN:", " ".join(self.kinds()), "an open was submitted after a write was refused before it")

    # r8 (Dot residual B2 on 9bbfaf75): a write refused before the open, by whichever writer, means no open is sent. The open's dispatch asks one predicate (Get-OpenDispatchRefusal) right
    # before um-run is called, so a refusal found AFTER the pre-open check (a log line on the way to the open, the open's own job folder) ends the batch the same way.
    _R8_OPEN_ANCHOR = "            $op = Submit-AgentJob $b.openJobId "

    def _r8_run(self, anchor: str, make: str, *extra: str, legs: int = 1) -> tuple[subprocess.CompletedProcess, Path, Path]:
        """One plan run with `make` (PowerShell; {b} = the last spec) run once just before `anchor`; (the run, the last spec, its snapshot taken right after `make`)."""
        self._stage_case()
        specs = [self._r5_spec(n, s) for n, s in (("fx-a.json", self.specs[0]), ("fx-b.json", self.specs[1]))][:max(1, legs)]
        b, snap = specs[-1], self.case / "r8.snap"
        args, e = self.plan_cmd(self.write_plan([self.item(s, "A", self.commit_a) for s in specs]), *extra)
        indent = re.match(r"\s*", anchor).group(0)
        inject = f"{indent}if (-not $script:r8Done) {{ $script:r8Done = $true; {make.format(b=psq(b))}; Copy-Item -LiteralPath '{psq(b)}' -Destination '{psq(snap)}' }}\n"
        args[args.index("-File") + 1] = str(self._patched_runner(anchor, inject + anchor))
        return subprocess.run(args, capture_output=True, text=True, timeout=240, env=e), b, snap

    def _r8_open_jobs_written(self) -> list[str]:
        jobs = self.run_dir / "batch-jobs"
        return sorted(p.name for p in jobs.glob("lease-batch-open-*")) if jobs.is_dir() else []

    def test_r8_b2_a_log_refusal_after_the_pre_open_check_sends_no_open(self) -> None:
        # Dot B2 steps 1-3: batch.log becomes a hard link to the spec after the pre-open check, while the open's sidecar is written. The next log line refuses it (the keeper is
        # stopped, the TEMP redirect made), and Submit-AgentJob then accepted the redirect and sent the OPEN. RED on dae24318: OPEN in the um-run calls.
        relink = "[IO.File]::Delete((Join-Path $RunDir 'batch.log')); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch.log') -Value '{b}' | Out-Null"
        for name, anchor in (("before BATCH_OPEN_SUBMIT", '        L "BATCH_OPEN_SUBMIT batch='),
                             ("while the sidecar is written (Dot's route)", '        L "BATCH_OPEN_LIVENESS batch='),
                             ("before the sidecar read-back line", '            L "BATCH_OPEN_LIVENESS_READBACK batch=')):
            with self.subTest(at=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r, b, snap = self._r8_run(anchor, relink)
                out = r.stdout + r.stderr
                self.assertTrue(snap.exists(), "batch.log was relinked to the spec (the case under test)\n" + out)
                self.assertEqual(b.read_bytes(), snap.read_bytes(), "a log line was appended into the accepted input\n" + out)
                self.assertNotIn("OPEN:", " ".join(self.kinds()), "an OPEN was sent after a write was refused before it\n" + out)
                self.assertEqual(self._r8_open_jobs_written(), [], "the open's job file was written under -RunDir")
                self.assertIn("BATCH_WRITE_REFUSED ", r.stderr)
                self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
                self.assertEqual(r.returncode, 2, out)
                res = batch_result(r.stdout)
                self.assertEqual((res["stopReason"], res["batches"][0]["endReason"], res["batches"][0]["leaseId"]), ("LEG_SPEC_IS_RUN_OUTPUT", "LEG_SPEC_IS_RUN_OUTPUT", None), res)

    def test_r8_b2_suppression_first_found_by_the_open_job_folder_guard_sends_no_open(self) -> None:
        # Dot B2 (the :925 continuation): the open's job folder holds a hard link to the spec when the open is submitted; Submit-AgentJob's own guard is the first to see it.
        # RED on 9bbfaf75 (the guard turned the suppression on and the open went out through the TEMP redirect); fixed on dae24318 by r7 pass 3 A1, kept by the r8 predicate.
        r, b, snap = self._r8_run(self._R8_OPEN_ANCHOR, "New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch-jobs\\keep.json') -Value '{b}' | Out-Null")
        out = r.stdout + r.stderr
        self.assertTrue(snap.exists(), out)
        self.assertEqual(b.read_bytes(), snap.read_bytes(), out)
        self.assertNotIn("OPEN:", " ".join(self.kinds()), "an OPEN was sent after its own job folder was refused\n" + out)
        self.assertEqual(self._r8_open_jobs_written(), [], out)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
        self.assertEqual(r.returncode, 2, out)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["batches"][0]["endReason"]), ("LEG_SPEC_IS_RUN_OUTPUT", "LEG_SPEC_IS_RUN_OUTPUT"), res)

    def test_r8_a_suppression_after_the_open_was_sent_still_submits_the_close(self) -> None:
        # The close the run owes is kept: batch.log becomes a hard link to the spec once the open has returned with its lease. The guard refuses it, nothing is probed or run,
        # and the CLOSE still goes out (its job file under the TEMP redirect, never under -RunDir).
        r, b, snap = self._r8_run("        $adm = $(if ($null -ne $op.result) { $op.result.admission } else { $null })",
                                  "[IO.File]::Delete((Join-Path $RunDir 'batch.log')); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch.log') -Value '{b}' | Out-Null")
        out = r.stdout + r.stderr
        self.assertTrue(snap.exists(), out)
        self.assertEqual(b.read_bytes(), snap.read_bytes(), out)
        k = [x.split(":")[0] for x in self.kinds()]
        self.assertEqual(k, ["OPEN", "CLOSE"], "the open was sent, so its close is owed (and nothing ran)\n" + out)
        jobs = self.run_dir / "batch-jobs"
        self.assertEqual(sorted(p.name for p in jobs.glob("lease-batch-close-*")) if jobs.is_dir() else [], [], "the close job was written under -RunDir")
        self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
        self.assertEqual(r.returncode, 2, out)
        self.assertEqual(batch_result(r.stdout)["stopReason"], "LEG_SPEC_IS_RUN_OUTPUT")

    def test_r8_an_open_that_could_be_admitted_after_the_claim_expires_is_not_sent(self) -> None:
        # Dot B2 step 4: the agent may admit the open until its notAfterUtc (the batch deadline, ~240 s here) and -ClaimTtlSec is 60. That is sent only while the keeper renews
        # the claim on schedule; a keeper that is stopped or whose renewal failed refuses the open OPEN_OUTLIVES_CLAIM (exit 9), nothing sent, the claim released.
        # RED on dae24318: the open is sent. Controls: a renewal that came back RENEWED, and a stopped keeper whose claim outlasts the open's notAfterUtc, still open.
        stop = "$script:keeperStoppedEarly = $true; $script:keeperStoppedInTime = Stop-VenueKeeper -Keeper $claimKeeper; $script:keeperLive = $false"
        cases = {
            "keeper stopped": (stop, ("-ClaimTtlSec", "60"), False),
            "last renewal BUSY": ("$claimKeeper.state.runs = 1; $claimKeeper.state.ok = 1; $claimKeeper.state.last = 'BUSY'", ("-ClaimTtlSec", "60"), False),
            "a renewal threw": ("$claimKeeper.state.runs = 1; $claimKeeper.state.errors = 1; $claimKeeper.state.lastError = 'injected'", ("-ClaimTtlSec", "60"), False),
            "control: last renewal RENEWED": ("$claimKeeper.state.runs = 1; $claimKeeper.state.ok = 1; $claimKeeper.state.renews = 1; $claimKeeper.state.last = 'RENEWED'",
                                              ("-ClaimTtlSec", "60"), True),
            "control: keeper stopped, the claim outlasts notAfterUtc": (stop, (), True),
            # adversary r8 A2: a keeper that has not renewed yet (runs=0) or RENEWED long ago does not vouch for a claim already expired or within one renewal of it
            "first renewal overdue, the claim already expired": ("$claim.expiresUtc = [DateTime]::UtcNow.AddSeconds(-1).ToString('o')", ("-ClaimTtlSec", "60"), "CLAIM_LOST"),
            "RENEWED long ago, the expiry within one renewal": ("$claimKeeper.state.runs = 1; $claimKeeper.state.ok = 1; $claimKeeper.state.renews = 1; $claimKeeper.state.last = 'RENEWED'; "
                                                                "$claimKeeper.state.expiresUtc = [DateTime]::UtcNow.AddSeconds(5).ToString('o')", ("-ClaimTtlSec", "60"), False),
        }
        for name, (make, extra, sent) in cases.items():
            with self.subTest(case=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r, _b, snap = self._r8_run(self._R8_OPEN_ANCHOR, make, *extra)
                out = r.stdout + r.stderr
                self.assertTrue(snap.exists(), out)
                self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
                k = [x.split(":")[0] for x in self.kinds()]
                res = batch_result(r.stdout)
                if sent is True:
                    self.assertEqual((r.returncode, k[0], k[-1]), (0, "OPEN", "CLOSE"), out)
                    continue
                reason = sent or "OPEN_OUTLIVES_CLAIM"
                self.assertEqual(k, [], "an open that could be admitted after the claim's expiry was sent\n" + out)
                self.assertEqual(self._r8_open_jobs_written(), [], out)
                self.assertEqual(r.returncode, 9, out)
                [b1] = self.batches()
                self.assertEqual((res["stopReason"], b1["endReason"], b1["leaseId"]), (reason, reason, None), b1)
                self.assertIn("notAfterUtc=", b1["endDetail"])

    def test_r8_adv2_a3_the_claim_and_the_lease_are_asked_again_at_the_launch_of_the_probe_and_the_leg(self) -> None:
        # adversary r8 pass 2 A3: the guards between the top-of-loop checks and a launch (the leg's output and RUN line, the probe's gate log, work folder and output) can block on a
        # share, so the claim can be lost and the lease end while they run. Injected right at the launch: nothing starts in batch 1, and batch 2 takes a fresh claim and lease and runs
        # the leg. RED on f26a2f1c: the leg (or the probe) is started on a lost claim or an ended lease.
        leg_anchor = "                if ($script:RunOutputAliased) { $b.legsAttempted--; Stop-BatchOnSuppression $b; break }   # r7: the RUN line itself was refused: the leg is not started"
        cases = {
            "leg: claim lost while its guards ran": (leg_anchor, "$claimKeeper.state.lost = 'NOT_OURS'", "CLAIM_LOST", ["OPEN", "QUIET", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
            "leg: the lease ended while its guards ran": (leg_anchor, "$leaseEnd = [DateTime]::UtcNow.AddSeconds(-31)", "LEASE_REMAINING_SHORT", ["OPEN", "QUIET", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
            "probe: claim lost while its guards ran": ("    if (-not ($asked -and (Test-RunDirWrite $out))) {","$claimKeeper.state.lost = 'NOT_OURS'", "CLAIM_LOST", ["OPEN", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
            # r9 (adversary r9 A2 = OPEN r8p3-A1; LEASE-BATCH-PROBE-LAUNCH-LEASE-ROOM-1): the fourth combination. RED on a01ce8f0: the probe starts after the lease ended.
            "probe: the lease ended while its guards ran": ("    if (-not ($asked -and (Test-RunDirWrite $out))) {", "$leaseEnd = [DateTime]::UtcNow.AddSeconds(-31)", "LEASE_REMAINING_SHORT", ["OPEN", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
        }
        for name, (anchor, make, reason, kinds) in cases.items():
            with self.subTest(case=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r, _b, _snap = self._r8_run(anchor, make)
                out = r.stdout + r.stderr
                self.assertEqual([x.split(":")[0] for x in self.kinds()], kinds, out)
                self.assertEqual(r.returncode, 0, out)
                b1, b2 = self.batches()
                self.assertEqual((b1["endReason"], b1["legsAttempted"], b2["endReason"], b2["legsCounted"]), (reason, 0, "ALL_LEGS_DONE", 1), b1)
                self.assertIn("launch", b1["endDetail"])

    def test_r8_adv_a1_an_input_that_cannot_be_identified_as_accepted_is_refused_before_the_claim(self) -> None:
        # adversary r8 A1: the spec is renamed away while its accepted identity is taken and put back after. The snapshot failure was swallowed (accepted = null), so every destination
        # check skipped that input as accepted, and once renamed and re-linked at the end, a receipt was written through it. Now: asked again, then refused before any claim.
        # RED on 81ad0482: the run goes on (exit 0) with an input no destination can be compared with.
        self._stage_case()
        a = self._r5_spec("fx-a.json", self.specs[0])
        away = a.with_name("fx-a.away")
        args, e = self.plan_cmd(self.write_plan([self.item(a, "A", self.commit_a)]))
        before = "# r6 pass 3 (adversary r6 pass 2 A1): each input's physical path and file identity as accepted"
        after = "if ($LegSet) { $hit = Get-InputOutputRefusal; if ($hit) { Stop-InputIsOutput $hit } }"
        args[args.index("-File") + 1] = str(self._patched_runner(before, f"Move-Item -LiteralPath '{psq(a)}' -Destination '{psq(away)}'\n" + before,
                                                                 (after, f"Move-Item -LiteralPath '{psq(away)}' -Destination '{psq(a)}'\n" + after)))
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("LEG_PLAN_REFUSED reason=INPUT_VANISHED item=1 ", r.stderr)
        self.assertIn("could not be identified as it was accepted", r.stderr)
        self.assertEqual(self.kinds(), [], "a claim, open or leg followed an input with no accepted identity\n" + out)
        self.assertFalse(self.claim_path.exists(), out)

    def test_r7_adv2_a4_a_preflight_refusal_with_an_uninspectable_zone_appends_nothing_to_the_plan(self) -> None:
        # adversary r7 pass 2 A4: the plan file IS RunDir\batch.log (one name) and RunDir\gate.log is a link to nothing, so the preflight guard types OUTPUT_ZONE_UNRESOLVED; before any input
        # is accepted nothing is compared, and the refusal line was appended into the plan
        self.run_dir.mkdir(parents=True, exist_ok=True)
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        log = self.run_dir / "batch.log"
        log.write_bytes(plan.read_bytes())
        snap = log.read_bytes()
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", self._rpf06_dangling(self.run_dir / "gate.log")], check=True, capture_output=True)
        args, e = self.plan_cmd(log)
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=OUTPUT_ZONE_UNRESOLVED", r.stderr)
        self.assertEqual(log.read_bytes(), snap, "the refusal line was appended into the plan file\n" + r.stdout + r.stderr)

    def test_r7_adv_a1_a_receipt_file_that_is_an_accepted_input_as_accepted_suppresses_every_write(self) -> None:
        # adversary r7 A1: after the final leg the spec is renamed to a.old and RunDir\batch-01.json is made a hard link to a.old (batch.log plain, batches.jsonl absent). The guard reads the
        # spec as INPUT_VANISHED and asks file by file: batch-01.json is the accepted input as accepted, which suppressed that one receipt only and kept the writes on.
        for form in ("scalar spec", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                old = spec.with_name("fx-a.json.old")
                args, e = self.batch_cmd([spec]) if form == "scalar spec" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                r = self._rpf06_run(args, e, f"[IO.File]::Move('{psq(spec)}', '{psq(old)}'); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch-01.json') -Value '{psq(old)}' | Out-Null")
                self.assertTrue(old.is_file() and not spec.exists(), "the spec was renamed and linked (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(old.read_bytes(), self.specs[0].read_bytes(), "a receipt was written into the accepted input")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"]), (2, "LEG_SPEC_IS_RUN_OUTPUT"), r.stderr)
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), "the writes stayed on after an alias was found")
                self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")

    # r7: the static half: every write primitive in the runner is either asked about first (Test-RunDirWrite on the same line or the two before it) or listed here with why it needs no
    # ask. A new writer that is neither fails this test, so the next unguarded writer is found here, not by a reviewer.
    _R7_UNGUARDED_OK = {
        "New-Item -ItemType Directory -Force -Path $RunDir": "creates -RunDir before any input is accepted",
        "New-Item -ItemType Directory -Force -Path $jobDir": "creates batch-jobs before any input is accepted",
        "$null = New-Item -ItemType Directory -Path $redirect": "the output redirect: a NEW folder under TEMP, never -RunDir",
        "[IO.File]::Open($f, [IO.FileMode]::CreateNew": "the job file: CreateNew in a folder the guard just asked about (or the TEMP redirect); it never opens an existing file",
        "[IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read": "a read",
        "Add-Content -LiteralPath $batchLog -Value $resultLine": "the summary line: asked ($logOk) before the summary is serialized, or the TEMP redirect",
        "$putPerBatch = {": "a receipt: run only by Write-ReceiptFile, right after Test-ReceiptWriteSuppressed asks about its file",
        "Write-ReceiptFile $B.batch $jsonl {": "a receipt: run only by Write-ReceiptFile, right after Test-ReceiptWriteSuppressed asks about its file",
        "$captured = @(& $pwshExe @qArgs *>&1": "the quiet probe's streams merged into the pipeline (BACHELOR-THERMAL-GATE-WAIT-1 r5 pass 1 A2): no file; its evidence write is asked about right before it",
    }

    def test_r7_every_write_primitive_in_the_runner_is_guarded_or_listed(self) -> None:
        lines = RUNNER.read_text(encoding="utf-8").splitlines()
        prim = re.compile(r"Add-Content|Set-Content|Out-File|WriteAllText|AppendAllText|WriteAllBytes|WriteAllLines|\[IO\.File\]::(Open|Create|Copy|Move)\(|New-Item\b|Copy-Item|Move-Item|\*>|(?<![<>-])>\s*\$(?!null)|Add-VenueLeaseLedger")
        bad = []
        for i, line in enumerate(lines):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if not prim.search(code) or "Add-Type" in code:
                continue
            if any(k in code for k in self._R7_UNGUARDED_OK):
                continue
            if any("Test-RunDirWrite" in l for l in lines[max(0, i - 2):i + 1]):
                continue
            bad.append(f"{i + 1}: {line.strip()}")
        self.assertEqual(bad, [], "a write primitive with no Test-RunDirWrite right before it (guard it, or list it in _R7_UNGUARDED_OK with why)")

    def _r5_two_batch_args(self) -> tuple[list[str], dict, Path, Path]:
        a, b = self._r5_spec("fx-a.json", self.specs[0]), self._r5_spec("fx-b.json", self.specs[1])
        args, e = self.plan_cmd(self.write_plan([self.item(a, "A", self.commit_a), self.item(b, "A", self.commit_a)]), FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        return args, e, a, b

    def test_r5_b1_mid_run_an_alias_seen_at_batch_1s_receipt_write_names_both_lost_receipts_and_stops_typed(self) -> None:
        args, e, _a, b = self._r5_two_batch_args()
        link, snap = self._r5_link_to_batch_log(b)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, link, batch=1)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, "LEG_SPEC_IS_RUN_OUTPUT", 1, 1))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, "no lease is opened for batch 2")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes())

    def test_r5_b1_mid_run_a_spec_that_already_ran_and_was_deleted_stops_input_vanished_not_as_a_run_output(self) -> None:
        args, e, a, _b = self._r5_two_batch_args()
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(a)}')", batch=1)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=INPUT_VANISHED item=1 ", r.stderr)
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"], res["receiptPersistFailures"]), (2, "INPUT_VANISHED", 1, 1, []))
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "batch 1's receipt is written")
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1)

    def test_r5_b1_the_pre_claim_deadline_path_records_a_receipt_the_guard_suppressed(self) -> None:
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        spec = self._r5_spec("fx-a.json", self.specs[0])
        link, snap = self._r5_link_to_batch_log(spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        r = self._r5_run(args, e, self._PRECLAIM_DEADLINE, link)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (3, "DEADLINE"))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes())

    def test_r5_b1_the_claim_held_deadline_path_records_a_receipt_the_guard_suppressed(self) -> None:
        self._foreign_live_claim()
        spec = self._r5_spec("fx-a.json", self.specs[0])
        link, snap = self._r5_link_to_batch_log(spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)))
        r = self._r5_run(args, e, self._CLAIM_HELD_DEADLINE, link)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (3, "DEADLINE"))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes())

    # r3 (sol r2b B2 = catalogue RPF-02): the lock helper took batch.log after its marker wait expired or was cancelled, whether or not the runner had paused, so a slow start was
    # locked ahead of the pause (and died on its next direct L), and 'locked' was written for a runner that never paused. These drive the helper alone: no runner, no pause marker.
    def test_r3_b2_an_expired_or_cancelled_marker_wait_takes_no_lock_and_signals_nothing(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        for how in ("expired", "cancelled"):
            with self.subTest(how=how):
                done, t, *rest = self._lock_log_while_the_runner_is_paused(log, paused, locked, give_up_s=0.5 if how == "expired" else 30)
                try:
                    if how == "cancelled":
                        done.set()
                    time.sleep(1.5)
                    self.assertFalse(locked.exists(), f"a {how} marker wait wrote the locked marker")
                    with open(log, "a", encoding="utf-8") as f:   # a sharing violation (PermissionError) here = the lock was taken with no pause
                        f.write("")
                    if rest:
                        self.assertEqual(rest[0]["outcome"], "never-paused" if how == "expired" else "cancelled")
                finally:
                    done.set()
                    t.join(5)
                    locked.unlink(missing_ok=True)

    def test_r3_adv_a1_a_pause_marker_that_appears_only_after_the_wait_expired_takes_no_lock(self) -> None:
        # adversary r3 A1: the helper re-checked the marker AFTER its wait loop, so a marker that appeared between the expiry and that check was locked on. The marker double is
        # absent on its first look and present on every later one; with give_up_s=0 the wait has expired before any later look.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        locked = self.case / "log-locked.marker"

        class LateMarker:
            parent = self.case
            looks = 0

            def exists(self) -> bool:
                LateMarker.looks += 1
                return LateMarker.looks > 1

        done, t, *rest = self._lock_log_while_the_runner_is_paused(log, LateMarker(), locked, give_up_s=0)
        try:
            t.join(5)
            self.assertFalse(locked.exists(), "a marker seen only after the wait expired was locked on")
            with open(log, "a", encoding="utf-8") as f:   # PermissionError here = the lock was taken
                f.write("")
            if rest:
                self.assertEqual(rest[0]["outcome"], "never-paused")
        finally:
            done.set()
            t.join(5)

    # r4 (sol r3 B1; Dot 6457592181 on #704): the pause marker survived the runner's 60 s pause, so a helper delayed past it still locked, recorded `locked`, and the test certified a handshake
    # the runner never took part in. The runner now records its own expiry and acknowledges only a lock it probed while still paused; _assert_handshake demands that acknowledgment.
    def _handshake_state(self, outcome: str = "locked", paused: str | None = "n-1", ack: str | None = "n-1 held", expired: str | None = None) -> dict:
        """A lock-helper state over real marker files (the runner and the helper are not run): the handshake assert is decided by what the markers say."""
        d = Path(tempfile.mkdtemp(prefix="hs-", dir=self.tmp))
        mk = {"paused": d / "p.marker", "expired": d / self._EXPIRED_MARKER, "ack": d / self._ACK_MARKER}
        for k, v in (("paused", paused), ("ack", ack), ("expired", expired)):
            if v is not None:
                mk[k].write_text(v, encoding="utf-8")
        return {"outcome": outcome, "released": False, "give_up_s": 1, "markers": mk}

    def test_r4_b1_the_handshake_assert_accepts_only_a_lock_the_paused_runner_acknowledged_with_its_own_nonce(self) -> None:
        self._assert_handshake(self._handshake_state(), "CLAIM_TAKEN")   # the control: locked, acknowledged with the runner's nonce, no expiry
        cases = {
            "the runner's pause expired and the helper locked after it (sol r3 B1)": self._handshake_state(expired="n-1 expired", ack=None),
            "the pause expired although an ack exists": self._handshake_state(expired="n-1 expired"),
            "locked with no acknowledgment at all": self._handshake_state(ack=None),
            "an ack carrying another attempt's nonce": self._handshake_state(ack="n-0 held"),
            "an ack that is not a held lock": self._handshake_state(ack="n-1 free"),
            "no pause marker to bind the ack to": self._handshake_state(paused=None),
            "the helper itself refused the expired pause": self._handshake_state(outcome="pause-expired", expired="n-1 expired", ack=None),
        }
        for label, st in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(AssertionError):
                    self._assert_handshake(st, "CLAIM_TAKEN")

    def test_r4_b1_a_lock_helper_that_sees_the_pause_marker_after_the_runners_pause_expired_takes_no_lock(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        paused.write_text("n-1", encoding="utf-8")
        (self.case / self._EXPIRED_MARKER).write_text("n-1 expired", encoding="utf-8")   # the runner gave up and carried on; its pause marker is left behind
        done, t, state = self._lock_log_while_the_runner_is_paused(log, paused, locked, give_up_s=5)
        try:
            t.join(10)
            self.assertEqual(state["outcome"], "pause-expired")
            self.assertFalse(locked.exists(), "a pause the runner had already given up on was locked on")
            with open(log, "a", encoding="utf-8") as f:   # PermissionError here = the lock was taken
                f.write("")
            with self.assertRaises(AssertionError):
                self._assert_handshake(state, "CLAIM_TAKEN")
        finally:
            done.set()
            t.join(5)

    def test_r4_b1_a_helper_delayed_past_the_runners_pause_is_refused_and_the_runner_resumes_without_a_lock(self) -> None:
        # the real runner pauses for 2 s here (60 s in the other tests); the helper first sees the pause marker at 12 s (the runner starts its pause a few seconds in), the case sol r3 B1 describes at t=61 s of 60 s. The runner has
        # recorded its expiry by then: no lock is taken, no acknowledgment exists, and the attempt is refused by name.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        runner, paused, locked = self._runner_paused_at_claim_taken(pause_s=2)
        args[args.index("-File") + 1] = str(runner)
        t0 = time.monotonic()

        class Delayed:
            parent = paused.parent

            def exists(self) -> bool:
                return time.monotonic() - t0 >= 12 and paused.exists()

        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", Delayed(), locked, give_up_s=60)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
            t.join(30)
        finally:
            done.set()
            t.join(5)
        state["markers"]["paused"] = paused
        self.assertEqual(state["outcome"], "pause-expired", r.stdout + r.stderr)
        self.assertFalse(locked.exists(), "no lock was taken for an expired pause")
        self.assertTrue(paused.exists() and (self.case / self._EXPIRED_MARKER).read_text(encoding="utf-8").strip() == paused.read_text(encoding="utf-8").strip() + " expired",
                        "the runner recorded that its pause expired with no lock, under its own nonce")
        self.assertFalse((self.case / self._ACK_MARKER).exists(), "an expired pause is never acknowledged")
        with self.assertRaises(AssertionError):
            self._assert_handshake(state, "CLAIM_TAKEN", r)
        self.assertEqual(r.returncode, 0, "the runner resumed without a lock and the batch ran normally\n" + r.stdout + r.stderr)

    def test_r4_adv_a2_a_lock_the_runner_looks_at_after_its_pause_ran_out_is_expired_not_acknowledged(self) -> None:
        # adversary r4 A2: the pause loop checked the clock BEFORE the look, so a look that returned after the window (here 2 s into a 1 s pause) still counted as seen, was probed and
        # acknowledged, and no expiry was recorded. The clock is read after the look, and again after the probe. The pause snippet runs alone; Test-Path is a function that returns late.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        snippet = self._pause_snippet("CLAIM_TAKEN", 1)
        script = self.case / "pause-snippet.ps1"
        script.write_text("function Test-Path { param([string]$LiteralPath) Start-Sleep -Seconds 2; $true }\n"
                          f"$batchLog = '{psq(log)}'\n$Message = 'CLAIM_TAKEN batch=1'\n" + snippet + "\n'done'\n", encoding="utf-8-sig")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        expired, ack = self.case / self._EXPIRED_MARKER, self.case / self._ACK_MARKER
        self.assertTrue(expired.exists(), "a look that returned after the window is an expired pause")
        self.assertFalse(ack.exists(), "a late look is never acknowledged")

    def test_r4_b1_a_prompt_helper_is_acknowledged_by_the_runner_with_its_own_nonce_while_the_log_is_held(self) -> None:
        # the successful-handshake control on the real runner: paused, locked, the runner probes batch.log (held), writes `<nonce> held`, and only then resumes
        r, state = self._run_plan_with_log_held_at("CLAIM_TAKEN")
        self._assert_handshake(state, "CLAIM_TAKEN", r)
        mk = state["markers"]
        self.assertEqual(mk["ack"].read_text(encoding="utf-8").strip(), mk["paused"].read_text(encoding="utf-8").strip() + " held")
        self.assertFalse(mk["expired"].exists())
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)

    def test_r3_b2_the_lock_is_let_go_when_the_locked_marker_cannot_be_written(self) -> None:
        # sol r2b hardening: an exception writing the marker after CreateFileW skipped CloseHandle. The marker path is a directory, so the write raises; the handle is closed anyway.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        paused.write_text("paused", encoding="utf-8")
        locked.mkdir()
        raised: list = []
        hook = threading.excepthook
        threading.excepthook = lambda a: raised.append(a.exc_type)
        try:
            done, t, *_ = self._lock_log_while_the_runner_is_paused(log, paused, locked, give_up_s=5)
            t.join(10)
        finally:
            threading.excepthook = hook
            done.set()
        self.assertTrue(raised, "the marker write raised (the case under test)")
        with open(log, "a", encoding="utf-8") as f:   # PermissionError here = the handle leaked
            f.write("")

    def test_r7_adv_a1_a_direct_log_line_that_cannot_be_appended_is_counted_and_said(self) -> None:
        # adversary r7 A1: L's own append threw into the batch catch without Register-LogFailure, so a held batch.log at CLAIM_TAKEN turned a healthy batch into
        # BATCH_ERROR / exit 12 (unchanged, r6's typed stop) with logWriteFailures=0 and no BATCH_LOG_FAILED line. Every append failure is counted and said, and counted once.
        anchor = "Add-Content -LiteralPath $batchLog -Value $line"
        runner = self._patched_runner(anchor, "if ($Message -like 'CLAIM_TAKEN*') { throw [IO.IOException]::new('locked') }; " + anchor)
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual(res["logWriteFailures"], 1, "one failed append is one failure, not two")
        [b] = self.batches()
        self.assertEqual(b["logWriteFailures"], 1)
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (locked)", r.stderr)

    def test_r7_adv_a2_a_leg_whose_process_cannot_be_created_stays_not_run(self) -> None:
        # adversary r7 A2: the in-flight marker was set before Process.Start, so a command line past Windows' 32767 characters (an unrestricted -SheetCopyDir) failed at
        # process creation and the leg that never started was recorded LEG_FAILED. A launch that throws a Win32Exception started nothing: the leg stays NOT_RUN.
        # The 40000-character -SheetCopyDir cannot be fed to the runner itself (its own command line is past the limit), so the launch is made to fail the same way:
        # Process.Start on a file that does not exist throws the same Win32Exception, before any child exists.
        anchor = "Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs"
        runner = self._patched_runner(anchor, "Invoke-VenueBoundedProcess -FilePath (Join-Path $RunDir 'no-such-pwsh.exe') -ArgumentList $lArgs")
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertEqual(self.leg_calls(), [], "no child was started")
        res = batch_result(r.stdout)
        self.assertEqual([(l["name"], l["status"]) for l in res["legs"]], [("01-fx-a-cpu-A", "NOT_RUN")], res["legs"])
        self.assertEqual((res["ran"], res["failed"], res["notRun"], res["stopReason"]), (0, 0, 1, "BATCH_ERROR"))

    def test_r7_adv2_a1_a_leg_spec_that_is_a_file_the_run_writes_is_refused_before_the_claim(self) -> None:
        # adversary r7 pass 2 A1: preflight accepted a spec at <RunDir>\leg-plan.json, the claim keeper then wrote the run plan over it, and the leg failed after the lease
        # opened with the accepted input destroyed. A spec that is one of the runner's own output files is refused with the whole plan, before any claim.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        spec = self.run_dir / "leg-plan.json"
        spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-a", "backends": ["cpu"], "scaleFactor": 1}), encoding="utf-8")
        before = spec.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    # r9 (Sol r8 / Dot B7 / producer OPEN A1; card LEASE-BATCH-RUNDIR-OUTPUT-COLLISION-GUARD-1): the r7 guard named five of the runner's outputs and missed quiet-*.out.txt and
    # gate.log, so a valid spec there passed preflight and was overwritten after the lease opened. One case per output name the runner writes under -RunDir, plus the two
    # directories it owns (batch-jobs\, quiet\). A new output that is not in the runner's one list is a failing case here, not a quiet hole.
    RUN_OUTPUT_PATHS = (
        "quiet-01-fx-a-cpu-A-leg.out.txt", "quiet-caps.out.txt", "leg-01-fx-a-cpu-A-b01-a1.out.txt", "gate.log", "batch-01.json", "batches.jsonl", "batch.log",
        "leg-plan.json", "batch-jobs/lease-batch-open-x.job.ps1", "quiet/venue-quiet-probe-x.json",
    )

    def _valid_leg_spec_text(self) -> str:
        return json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-a", "backends": ["cpu"], "scaleFactor": 1})

    def test_r9_a_leg_spec_at_any_runner_output_path_is_refused_before_the_claim(self) -> None:
        for leaf in self.RUN_OUTPUT_PATHS:
            with self.subTest(output=leaf):
                for p in (self.events, self.leg_args, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertEqual(self.leg_calls(), [])
                self.assertFalse(self.claim_path.exists(), "no claim was written")
                self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    def test_r9_a_plan_file_at_a_runner_output_path_is_refused_before_the_claim(self) -> None:
        # the plan is an input too: leg-plan.json is written over it once the claim is held, and batch.log / gate.log are appended to
        for leaf in ("leg-plan.json", "batch.log", "gate.log", "quiet-01-fx-a-cpu-A-leg.out.txt", "batch-jobs/plan.json"):
            with self.subTest(output=leaf):
                for p in (self.events, self.leg_args, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                plan = self.run_dir / leaf
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
                before = plan.read_text(encoding="utf-8")
                r = self.run_plan(plan)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")
                self.assertEqual(plan.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    def test_r9_a_scalar_leg_spec_at_a_runner_output_path_is_refused_before_the_claim(self) -> None:
        for leaf in ("gate.log", "quiet-01-fx-a-cpu-A-leg.out.txt", "leg-plan.json", "batch-03.json"):
            with self.subTest(output=leaf):
                for p in (self.events, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_batch([spec])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")
                self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    def test_r8_dualiso_a_scalar_leg_spec_the_leg_would_refuse_by_shape_is_refused_before_the_claim(self) -> None:
        # CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r8 (adversary r8 A3): scalar -LegSpec mode skipped Get-LegSpecRefusal, so a look leg whose look is an array (or
        # absent) opened the lease and the leg then threw DVE_LEG_SPEC_INVALID (LEG_FAILED, no receipt). The leg's shape predicate now runs before any claim.
        # fix 2 (adversary r8 fix-1 A1): a JSON null root and bytes that are not JSON, which the leg rejects after the lease too
        base = json.loads(self._valid_leg_spec_text())
        for case, text in (("look-array", json.dumps({**base, "legType": "look", "look": []})), ("look-absent", json.dumps({**base, "legType": "look"})),
                           ("generatorArgs-array", json.dumps({**base, "generatorArgs": [{"qualityMode": "hq"}]})),
                           ("null-root", "null"), ("not-json", "this is not json")):
            with self.subTest(case=case):
                for p in (self.events, self.claim_path):
                    p.unlink(missing_ok=True)
                spec = self.case / f"shape-{case}.json"
                spec.write_text(text, encoding="utf-8")
                r = self.run_batch([spec])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_USAGE LEG_SPEC_INVALID", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_r2n1_a7_a_spec_the_strict_reader_cannot_type_is_refused_before_the_claim(self) -> None:
        # adversary-7c5bfe86.json A7 (R2-NARROW-1 r2), its input: a leg spec that declares `legType` twice. The shape check and the export budget read it last-wins (ConvertFrom-Json keeps the
        # LAST member), the strict reader (Get-VenuePlannedLegType: a repeated member in any spelling is 'unknown') refuses to type it, so one set of bytes was planned as one thing and classified as
        # another. RED on 7c5bfe86: the batch took a claim and ran the leg. The set planner already refuses such bytes (DVE_LEGSET_INVALID); the scalar and plan routes now refuse them too, typed,
        # before any claim.
        base = self._valid_leg_spec_text()
        # (the schema's legType enum is speed | look | export; both cases are valid shapes by the last member, so only the strict reader tells them apart from a clean spec)
        for case, text in (("dup-speed-speed", base[:-1] + ', "legType": "speed", "legType": "speed"}'), ("dup-export-speed", base[:-1] + ', "legType": "export", "legType": "speed"}')):
            with self.subTest(case=case):
                for p in (self.events, self.claim_path):
                    p.unlink(missing_ok=True)
                spec = self.case / f"dup-{case}.json"
                spec.write_text(text, encoding="utf-8")
                r = self.run_batch([spec])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_USAGE LEG_SPEC_INVALID", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")

    # VENUE-LEASE-BATCH-LEGSPEC-COMMITTED-PREFLIGHT-1: the leg refuses a spec that is not COMMITTED (LEG_SPEC_NOT_COMMITTED, Invoke-VenueLeg.ps1 via Find-DvCommittedLegSpec) only
    # after the lease is open: on 2026-11-27T19:11:55Z an untracked swap0 spec spent about 2 min of Bachelor lease and a quiet probe before that refusal. The batch preflight now
    # asks the SAME function before any claim, open or lease. -CommittedSpecRepoRoot is the offline seam that turns the preflight on against a repo's HEAD while the stubs stand in
    # for the leg (the check is on by default only when the leg script is the real Invoke-VenueLeg.ps1, as the leg's own check is on outside -OfflineTestMode).
    _COMMITTED_SPEC = DV / "legs" / "m16-1243-chainattr-l-fullscreen-s1.json"

    def _untracked_copy_of_a_committed_spec(self) -> Path:
        doc = json.loads(self._COMMITTED_SPEC.read_text(encoding="utf-8"))
        doc["playSeconds"] = 26   # one field changed: the bytes are no committed blob's bytes
        p = self.case / "untracked-swap0.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def _reset_committed_case(self) -> None:
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def _assert_nothing_claimed_or_leased(self, mode: str, r: subprocess.CompletedProcess) -> None:
        out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
        self.assertEqual(r.returncode, 2, out)
        self.assertEqual(self.kinds(), [], f"{mode}: nothing is submitted: no open, no probe, no leg. " + out)
        self.assertEqual(self.leg_calls(), [], f"{mode}: no leg ran. " + out)
        self.assertFalse(self.claim_path.exists(), f"{mode}: a claim was written for a spec the leg refuses. " + out)
        self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: a lease was opened. " + out)

    def test_legspec_committed_an_uncommitted_spec_is_refused_before_any_claim_in_every_mode(self) -> None:
        untracked = self._untracked_copy_of_a_committed_spec()
        seam = ("-CommittedSpecRepoRoot", str(ROOT))
        self._reset_committed_case()
        r = self.run_batch([untracked], *seam)
        self._assert_nothing_claimed_or_leased("-LegSpec", r)
        self.assertIn("BATCH_USAGE LEG_SPEC_NOT_COMMITTED", r.stderr)
        # the plan form: the whole plan is refused at the item (item 2: item 1 is a committed spec and is fine), naming the index
        committed_item = self.item(self._COMMITTED_SPEC, "A", self.commit_a)
        self._reset_committed_case()
        r = self.run_plan(self.write_plan([committed_item, self.item(untracked, "B", self.commit_a)]), *seam)
        self._assert_nothing_claimed_or_leased("-LegPlan", r)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_NOT_COMMITTED item=2 ", r.stderr)
        # -LegSet: a set under legsets/ naming an untracked copy beside the committed legs (both removed in the finally)
        name = f"zz-legspec-committed-{uuid.uuid4().hex[:8]}"
        legset_path = DV / "legsets" / f"{name}.json"
        spec_in_legs = DV / "legs" / f"{name}.json"
        doc = json.loads(untracked.read_text(encoding="utf-8"))
        spec_in_legs.write_text(json.dumps(dict(doc, legId=name)), encoding="utf-8")
        legset_path.write_text(json.dumps({"schema": "mlv-app/dual-venue-legset/v1", "legSet": name, "card": doc["card"], "clipId": doc["clipId"],
                                           "description": "legspec-committed test stand-in, removed by the test", "legs": [f"legs/{name}.json"]}), encoding="utf-8")
        try:
            self._reset_committed_case()
            args, e = self.batch_cmd([untracked], *seam)
            args[args.index("-LegSpec"):args.index("-LegSpec") + 2] = ["-LegSet", name]
            r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)
            self._assert_nothing_claimed_or_leased("-LegSet", r)
            self.assertIn("BATCH_USAGE LEG_SPEC_NOT_COMMITTED", r.stderr)
        finally:
            legset_path.unlink(missing_ok=True)
            spec_in_legs.unlink(missing_ok=True)

    def test_legspec_committed_the_real_leg_script_turns_the_preflight_on_without_the_seam(self) -> None:
        # production: the batch runs the real Invoke-VenueLeg.ps1, whose own check refuses an uncommitted spec, so the preflight is on with no seam at all
        self._reset_committed_case()
        # (-ReceiptRoot: a regression here lets the real leg run, and it would write its refusal receipt into the shared receipt root; a scratch root under the repo's .claude-state keeps that out)
        scratch = ROOT / ".claude-state" / f"zz-legspec-committed-{uuid.uuid4().hex[:8]}"
        try:
            args, e = self.batch_cmd([self._untracked_copy_of_a_committed_spec()], "-ReceiptRoot", str(scratch / "receipts"))
            args[args.index("-LegScript") + 1] = str(LEG)
            r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self._assert_nothing_claimed_or_leased("real leg script", r)
        self.assertIn("BATCH_USAGE LEG_SPEC_NOT_COMMITTED", r.stderr)

    def test_legspec_committed_a_committed_spec_still_passes_the_preflight(self) -> None:
        seam = ("-CommittedSpecRepoRoot", str(ROOT))
        self._reset_committed_case()
        r = self.run_batch([self._COMMITTED_SPEC], *seam)
        self.assertNotIn("LEG_SPEC_NOT_COMMITTED", r.stdout + r.stderr, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self._reset_committed_case()
        r = self.run_plan(self.write_plan([self.item(self._COMMITTED_SPEC, "A", self.commit_a, "cpu")]), *seam)
        self.assertNotIn("LEG_SPEC_NOT_COMMITTED", r.stdout + r.stderr, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_legspec_committed_the_seam_is_refused_with_the_real_um_run(self) -> None:
        self._reset_committed_case()
        args, e = self.batch_cmd([self._COMMITTED_SPEC], "-CommittedSpecRepoRoot", str(ROOT))
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-CommittedSpecRepoRoot is an offline test seam", r.stderr)
        self.assertEqual(self.kinds(), [])

    def test_r8_export_a_window_the_agent_job_cap_refuses_is_refused_before_the_claim_in_every_mode(self) -> None:
        # EXPORT-VENUE-LEG-1 r8 (adversary r7 A3): maxFrames=20000 on the shipped 3.4 GB ProRes leg is a job the agent refuses (submitted timeout past 86400 s), but only the
        # generator Invoke-VenueLeg runs AFTER the lease opened said so. The batch now asks the export generator's -PreflightBudgetOnly mode in its preflight; -ExportBudgetScript
        # is the offline seam (a stand-in that refuses past 19000 frames, as the real budget does for that clip), and is refused with the real um-run.ps1.
        fake = self.case / "fake-export-budget.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames)\n"
                        "if ($MaxFrames -gt 19000) { throw \"DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED $MaxFrames frames of $ClipId need a job timeout past the agent's cap\" }\n"
                        "[pscustomobject]@{ jobTimeoutSec = 1 }\n", encoding="utf-8")
        shipped = json.loads((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"))

        def spec_with(frames: int) -> Path:
            doc = json.loads(json.dumps(shipped))
            doc["export"]["maxFrames"] = frames
            p = self.case / f"export-{frames}.json"
            p.write_text(json.dumps(doc), encoding="utf-8")
            return p
        over = spec_with(20000)
        for p in (self.events, self.claim_path):
            p.unlink(missing_ok=True)
        r = self.run_batch([over], "-ExportBudgetScript", str(fake))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE EXPORT_TIME_BUDGET_EXCEEDED", r.stderr)
        self.assertIn("DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        # the plan form: the whole plan is refused at the item, with nothing submitted
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(over, "B", self.commit_a)])
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        r = self.run_plan(plan, "-ExportBudgetScript", str(fake))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=EXPORT_TIME_BUDGET_EXCEEDED item=2 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse((self.run_dir / "batches.jsonl").exists(), "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        # a window inside the cap is not refused by this check (it may go on to run, which is not this test's concern)
        ok = spec_with(480)
        for p in (self.events, self.claim_path):
            p.unlink(missing_ok=True)
        r = self.run_batch([ok], "-ExportBudgetScript", str(fake))
        self.assertNotIn("EXPORT_TIME_BUDGET_EXCEEDED", r.stdout + r.stderr, r.stdout + r.stderr)

    def test_r8_export_the_budget_seam_is_refused_with_the_real_um_run(self) -> None:
        # a production run can never swap the budget check out: with the real um-run.ps1 the seam is a usage error
        fake = self.case / "fake-export-budget-real.ps1"
        fake.write_text("[pscustomobject]@{ jobTimeoutSec = 1 }\n", encoding="utf-8")
        for p in (self.events, self.claim_path):
            p.unlink(missing_ok=True)
        spec = self.case / "plain.json"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        args, e = self.batch_cmd([spec], "-ExportBudgetScript", str(fake))
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-ExportBudgetScript is an offline test seam", r.stderr)
        self.assertEqual(self.kinds(), [])

    def test_r18_export_a_spec_read_failure_in_the_budget_preflight_is_refused_before_the_claim_in_every_mode(self) -> None:
        # EXPORT-VENUE-LEG-1 r18 (sol r17 B1 = EXPORT-VENUE-LEG-1-OPEN-18-2 = adversary pass18 A2): Get-ExportBudgetRefusal caught a failed read of the spec and returned two empty
        # strings, which every caller (-LegPlan, -LegSet, -LegSpec) read as "no refusal". The shape check's own read had succeeded, so an over-cap window (here 20000 frames, which the
        # stand-in refuses past 19000 as the real budget does for that clip) went on to a claim and a lease. The seam -ExportBudgetSpecReadFault makes ONLY the preflight's read throw,
        # as a transient sharing violation would. A read failure is now the typed refusal SPEC_READ_FAILED before any claim or lease, and the budget child is never started.
        called = self.case / "budget-child-r18.called"
        fake = self.case / "fake-export-budget-r18.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames)\n"
                        "Set-Content -LiteralPath $env:FAKE_BUDGET_PID_FILE -Value $PID\n"
                        "if ($MaxFrames -gt 19000) { throw \"DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED $MaxFrames frames of $ClipId need a job timeout past the agent's cap\" }\n"
                        "[pscustomobject]@{ jobTimeoutSec = 1 }\n", encoding="utf-8")
        doc = json.loads((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"))
        doc["export"]["maxFrames"] = 20000
        over = self.case / "export-r18-over.json"
        over.write_text(json.dumps(doc), encoding="utf-8")
        legset_name = f"zz-r18-spec-read-{uuid.uuid4().hex[:8]}"
        legset_path = DV / "legsets" / f"{legset_name}.json"

        def reset() -> None:
            for p in (self.events, self.leg_args, self.claim_path, called):
                p.unlink(missing_ok=True)
            shutil.rmtree(self.run_dir, ignore_errors=True)

        def assert_refused(mode: str, r: subprocess.CompletedProcess, line: str) -> None:
            out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
            self.assertFalse(self.claim_path.exists(), f"{mode}: a claim was written for an over-cap window whose budget was never checked. " + out)
            self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: a lease was opened. " + out)
            self.assertEqual(self.kinds(), [], f"{mode}: nothing is submitted: no open, no probe, no leg. " + out)
            self.assertEqual(r.returncode, 2, out)
            self.assertIn(line, r.stderr, out)
            self.assertFalse(called.exists(), f"{mode}: the budget child was started on a spec the preflight could not read. " + out)

        reset()
        r = self.run_batch([over], "-ExportBudgetScript", str(fake), "-ExportBudgetSpecReadFault", FAKE_BUDGET_PID_FILE=str(called))
        assert_refused("-LegSpec", r, "BATCH_USAGE SPEC_READ_FAILED")
        reset()
        r = self.run_plan(self.write_plan([self.item(over, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), "-ExportBudgetSpecReadFault", FAKE_BUDGET_PID_FILE=str(called))
        assert_refused("-LegPlan", r, "LEG_PLAN_REFUSED reason=SPEC_READ_FAILED item=1 ")
        # -LegSet: a set under legsets/ naming an over-cap copy beside it (the real legsets/ directory is the only place the batch reads one; both removed in the finally)
        over_in_legs = DV / "legs" / f"{legset_name}.json"
        # (named by its legId: M16-1457-VENUE-BASELINE-DRIFT-1 r11, the set planner refuses a spec whose file name is not <legId>.json)
        over_in_legs.write_text(json.dumps(dict(doc, legId=legset_name)), encoding="utf-8")
        legset_path.write_text(json.dumps({"schema": "mlv-app/dual-venue-legset/v1", "legSet": legset_name, "card": doc["card"], "clipId": doc["clipId"],
                                           "description": "r18 test stand-in, removed by the test", "legs": [f"legs/{legset_name}.json"]}), encoding="utf-8")
        try:
            reset()
            args, e = self.batch_cmd([over], "-ExportBudgetScript", str(fake), "-ExportBudgetSpecReadFault", FAKE_BUDGET_PID_FILE=str(called))
            args[args.index("-LegSpec"):args.index("-LegSpec") + 2] = ["-LegSet", legset_name]
            r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)
            assert_refused("-LegSet", r, "BATCH_USAGE SPEC_READ_FAILED")
        finally:
            legset_path.unlink(missing_ok=True)
            over_in_legs.unlink(missing_ok=True)
        # the seam is refused with the real um-run.ps1, as every other offline seam is
        reset()
        args, e = self.batch_cmd([over], "-ExportBudgetSpecReadFault")
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-ExportBudgetSpecReadFault is an offline test seam", r.stderr)
        self.assertEqual(self.kinds(), [])

    def test_r18_sibling_an_over_cap_window_whose_clip_does_not_resolve_is_refused_by_the_clip_independent_floor(self) -> None:
        # EXPORT-VENUE-LEG-1 r18 sibling sweep (class: an unreadable input read as "no refusal" on the way to a claim): a clip the preflight child cannot resolve made the child fail with
        # no budget marker, and Get-ExportBudgetRefusal returned '', '' -- yet maxFrames=100000 needs 400000 s of export wait alone, past the agent's 86400 s cap whatever the clip's size
        # (sol r17: even a one-byte clip needs 400788 s submitted). The batch now asks the generator's -DescribeTimeBudgetBytes 0 (the clip-independent floor) when the clip did not
        # resolve, and refuses EXPORT_TIME_BUDGET_EXCEEDED before any claim when that floor alone passes the cap. A window whose floor fits is still left to the leg's generator.
        floor_calls = self.case / "budget-floor-r18.calls"
        fake = self.case / "fake-export-budget-r18-unresolved.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames, [long]$DescribeTimeBudgetBytes = -1)\n"
                        "if ($DescribeTimeBudgetBytes -ge 0) {\n"
                        "    Add-Content -LiteralPath $env:FAKE_FLOOR_FILE -Value \"$DescribeTimeBudgetBytes $MaxFrames\"\n"
                        "    if ($MaxFrames -gt 19000) { throw \"DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED $MaxFrames frames of a $DescribeTimeBudgetBytes-byte clip need a job timeout past the agent's cap\" }\n"
                        "    return [pscustomobject]@{ jobTimeoutSec = 1 }\n"
                        "}\n"
                        "throw \"DUAL_VENUE_EXPORT_RESOLVER_ABSENT the clip $ClipId does not resolve on this host\"\n", encoding="utf-8")
        shipped = json.loads((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"))

        def spec_with(frames: int) -> Path:
            doc = json.loads(json.dumps(shipped))
            doc["export"]["maxFrames"] = frames
            p = self.case / f"export-r18-unresolved-{frames}.json"
            p.write_text(json.dumps(doc), encoding="utf-8")
            return p

        def reset() -> None:
            for p in (self.events, self.leg_args, self.claim_path, floor_calls):
                p.unlink(missing_ok=True)
            shutil.rmtree(self.run_dir, ignore_errors=True)
        over = spec_with(20000)
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=mode):
                reset()
                if mode == "-LegSpec":
                    r = self.run_batch([over], "-ExportBudgetScript", str(fake), FAKE_FLOOR_FILE=str(floor_calls))
                    line = "BATCH_USAGE EXPORT_TIME_BUDGET_EXCEEDED"
                else:
                    r = self.run_plan(self.write_plan([self.item(over, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), FAKE_FLOOR_FILE=str(floor_calls))
                    line = "LEG_PLAN_REFUSED reason=EXPORT_TIME_BUDGET_EXCEEDED item=1 "
                out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: a lease was opened for an over-cap window whose clip did not resolve. " + out)
                self.assertEqual(self.kinds(), [], f"{mode}: nothing is submitted. " + out)
                self.assertEqual(r.returncode, 2, out)
                self.assertIn(line, r.stderr, out)
                self.assertIn("clip-independent", r.stderr, out)
                self.assertEqual(floor_calls.read_text(encoding="utf-8").split(), ["0", "20000"], out)
        # control: a window whose floor fits is not refused by the budget check when the clip does not resolve (the leg's generator still refuses the clip, typed, as before)
        reset()
        r = self.run_batch([spec_with(480)], "-ExportBudgetScript", str(fake), FAKE_FLOOR_FILE=str(floor_calls))
        self.assertNotIn("EXPORT_TIME_BUDGET_EXCEEDED", r.stdout + r.stderr, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertNotIn("EXPORT_BUDGET_PREFLIGHT_TIMEOUT", r.stdout + r.stderr, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertEqual(floor_calls.read_text(encoding="utf-8").split(), ["0", "480"])

    def test_r18_adversary_a1_a_budget_floor_that_cannot_be_evaluated_is_refused_before_the_claim(self) -> None:
        # adversary pass 19 A1 (on 410b5e09, introduced): the floor child itself can fail with no marker (its module import held without read sharing, as the first child's was), and
        # the floor fallback read only the marker, never the floor's exit, so an over-cap window still returned "no refusal" and reached a claim and a lease. The floor reads no clip,
        # so a floor that cannot be evaluated is not "unknowable": it is the typed refusal EXPORT_BUDGET_PREFLIGHT_FAILED before any claim or lease.
        fake = self.case / "fake-export-budget-r18-a1.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames, [long]$DescribeTimeBudgetBytes = -1)\n"
                        "throw \"Import-Module: the process cannot access the file 'AttrCudaArtifacts.psm1' because it is being used by another process\"\n", encoding="utf-8")
        doc = json.loads((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"))
        doc["export"]["maxFrames"] = 100000
        over = self.case / "export-r18-a1-over.json"
        over.write_text(json.dumps(doc), encoding="utf-8")
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=mode):
                for p in (self.events, self.leg_args, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                if mode == "-LegSpec":
                    r = self.run_batch([over], "-ExportBudgetScript", str(fake))
                    line = "BATCH_USAGE EXPORT_BUDGET_PREFLIGHT_FAILED"
                else:
                    r = self.run_plan(self.write_plan([self.item(over, "A", self.commit_a)]), "-ExportBudgetScript", str(fake))
                    line = "LEG_PLAN_REFUSED reason=EXPORT_BUDGET_PREFLIGHT_FAILED item=1 "
                out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: a lease was opened on a window whose budget floor could not be evaluated. " + out)
                self.assertEqual(self.kinds(), [], f"{mode}: nothing is submitted. " + out)
                self.assertEqual(r.returncode, 2, out)
                self.assertIn(line, r.stderr, out)

    def test_r19_b1_a_spec_changed_after_the_budget_judged_it_never_reaches_a_lease_or_a_leg(self) -> None:
        # EXPORT-VENUE-LEG-1 r19 (sol r18 B1 = pass20-A1): the budget preflight judged a 480-frame window, then the same file became maxFrames=100000 (sol's repro: it still passes the
        # shape predicate, and its zero-byte budget floor is past the agent's cap) while the batch waited. The plan guard repeated the shape check only and the scalar forms checked
        # no content at all, so the lease opened and the leg ran on bytes the budget never judged. The stand-in budget child rewrites the spec right after the batch read it, as a
        # change during the wait would. The batch records the judged bytes' sha256 and refuses SPEC_CHANGED_AFTER_PREFLIGHT before any lease or leg.
        fake = self.case / "fake-export-budget-r19-b1.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames, [long]$DescribeTimeBudgetBytes = -1)\n"
                        "if ($MaxFrames -gt 19000) { throw \"DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED $MaxFrames frames of $ClipId need a job timeout past the agent's cap\" }\n"
                        "if ($env:FAKE_MUTATE_SPEC) { Copy-Item -LiteralPath $env:FAKE_MUTATE_FROM -Destination $env:FAKE_MUTATE_SPEC -Force }\n"
                        "[pscustomobject]@{ jobTimeoutSec = 1 }\n", encoding="utf-8")
        doc = json.loads((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"))
        doc["export"]["maxFrames"] = 480
        judged_text = json.dumps(doc)
        doc["export"]["maxFrames"] = 100000
        changed = self.case / "export-r19-b1-changed.json"
        changed.write_text(json.dumps(doc), encoding="utf-8")
        spec = self.case / "export-r19-b1.json"
        shas = self.case / "leg-spec-sha-r19.log"

        def reset() -> None:
            for p in (self.events, self.leg_args, self.claim_path, shas):
                p.unlink(missing_ok=True)
            shutil.rmtree(self.run_dir, ignore_errors=True)
            spec.write_text(judged_text, encoding="utf-8")
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=mode):
                reset()
                env = {"FAKE_MUTATE_SPEC": str(spec), "FAKE_MUTATE_FROM": str(changed), "FAKE_LEG_SPEC_SHA": str(shas)}
                if mode == "-LegSpec":
                    r = self.run_batch([spec], "-ExportBudgetScript", str(fake), **env)
                else:
                    r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), **env)
                out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertIn('"maxFrames": 100000', spec.read_text(encoding="utf-8"), "the stand-in did not change the spec, so the test proves nothing. " + out)
                self.assertEqual([k for k in self.kinds() if k.startswith("LEG")], [], f"{mode}: a leg ran on a spec the budget never judged. " + out)
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: a lease was opened on a spec the budget never judged. " + out)
                self.assertEqual(r.returncode, 2, out)
                self.assertIn("SPEC_CHANGED_AFTER_PREFLIGHT", r.stderr, out)
        # control: an unchanged spec runs, and the leg is handed the sha256 of exactly the bytes the budget judged (the leg re-checks its own read against it)
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=f"control {mode}"):
                reset()
                if mode == "-LegSpec":
                    r = self.run_batch([spec], "-ExportBudgetScript", str(fake), FAKE_LEG_SPEC_SHA=str(shas))
                else:
                    r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), FAKE_LEG_SPEC_SHA=str(shas))
                out = f"control {mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertNotIn("SPEC_CHANGED_AFTER_PREFLIGHT", r.stderr, out)
                want = hashlib.sha256(spec.read_bytes()).hexdigest()
                rows = shas.read_text(encoding="utf-8").splitlines() if shas.exists() else []
                self.assertTrue(rows, "no leg ran on the unchanged spec. " + out)
                for row in rows:
                    self.assertEqual(row, f"expect={want} read={want}", out)

    def test_r19_b1_the_leg_refuses_spec_bytes_that_are_not_the_ones_the_budget_judged(self) -> None:
        # r19 (sol r18 B1, the dispatch site Invoke-VenueLeg.ps1:225): the bytes the LEG reads are the bytes run, so the leg compares them with -ExpectSpecSha256 and throws
        # DVE_LEG_SPEC_CHANGED before it generates or submits anything. Structural: the parameter, the comparison right after the one read, and the batch passing it at the launch.
        leg = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("[Parameter(ParameterSetName = 'Leg')][ValidatePattern('^[0-9a-f]{64}\\z')][string]$ExpectSpecSha256 = ''", leg)
        read_at = leg.index("$specBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $LegSpec).Path)")
        check_at = leg.index("if ($readSpecSha -cne $ExpectSpecSha256) { throw \"DVE_LEG_SPEC_CHANGED")
        self.assertLess(read_at, check_at)
        self.assertLess(check_at, leg.index("$spec = [Text.Encoding]::UTF8.GetString($specBytes) | ConvertFrom-Json -NoEnumerate"), "the check runs on the one read, before the spec is used")
        batch = RUNNER.read_text(encoding="utf-8")
        # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r3 (adversary pass 2 A2): the entry's OWN judged sha256 (proved equal to its path's binding before any claim), never a path-keyed lookup
        self.assertIn("$lArgs += @('-ExpectSpecSha256', [string]$leg.specSha256)", batch)

    # DV-PLANNED-SPEC-HASH-BIND-R10-DIGEST-KEY-RESIDUE-1 (key r10 B1, fable + opus on ca27678b): Get-SpecDigestKey picked the file's final path or the spelled full path PER CALL. A spec
    # spelled off its on-disk name (here: another case) whose lookup got no answer in 1 s on ONE call stored its judged digest under one key and was looked up under the other: the
    # entry's specSha256 read '' (which passed '' -cne ''), the content guard skipped its re-read and the leg was launched with no -ExpectSpecSha256. -SpecDigestKeyDelay '<n>:<ms>' makes
    # the n-th lookup of the run answer late (its bound is 1 s). -LegSpec's lookups run in the order shape-read-remove (1), shape store (2), budget store (3), entry lookup (4), per-leg
    # check (5); -LegPlan's in the order shape store (1), budget store (2), entry lookup (3), per-leg check (4). The seam delays the lookups that split the key on the old code.
    R10_KEY_SEAM = {"-LegSpec": "1:1500,3:1500", "-LegPlan": "1:1500,2:1500"}

    def _r10_spelled_spec(self) -> tuple[Path, Path]:
        """(the spec on disk, the same path spelled in another case): one file, two spellings; the batch keeps the spelling it was given."""
        spec = self.case / "r10-digest-key.json"
        spec.write_text(self.specs[0].read_text(encoding="utf-8"), encoding="utf-8")
        return spec, spec.with_name("R10-DIGEST-KEY.JSON")

    def _r10_run(self, mode: str, spelled: Path, *extra: str, **env: str) -> subprocess.CompletedProcess:
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        if mode == "-LegSpec":
            return self.run_batch([spelled], *extra, **env)
        return self.run_plan(self.write_plan([self.item(spelled, "A", self.commit_a)]), *extra, **env)

    def test_r10_a_spec_spelled_off_its_on_disk_name_whose_first_key_lookup_times_out_is_never_launched_without_its_hash(self) -> None:
        spec, spelled = self._r10_spelled_spec()
        want = hashlib.sha256(spec.read_bytes()).hexdigest()
        shas = self.case / "leg-spec-sha-r10.log"
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=mode):
                shas.unlink(missing_ok=True)
                r = self._r10_run(mode, spelled, "-SpecDigestKeyDelay", self.R10_KEY_SEAM[mode], FAKE_LEG_SPEC_SHA=str(shas))
                out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                rows = shas.read_text(encoding="utf-8").splitlines() if shas.exists() else []
                # old: the key split, the entry's digest read '' and the leg ran with 'expect=' (no -ExpectSpecSha256)
                self.assertEqual([x for x in rows if not x.startswith(f"expect={want} ")], [], "a leg ran with no bound hash (or another one). " + out)
                self.assertEqual(rows, [], "a leg ran on a spec whose key lookup got no answer. " + out)
                self.assertEqual(r.returncode, 2, "refused before any claim (SPEC_KEY_UNRESOLVED: a readable spec with no final path cannot be told from a twin spelling). " + out)
                self.assertIn("SPEC_KEY_UNRESOLVED", r.stderr, out)
                self.assertNotIn("SPEC_CHANGED_AFTER_PREFLIGHT", r.stderr, out)
                self.assertEqual([k for k in self.kinds() if k.startswith(("OPEN", "LEG"))], [], out)
                self.assertFalse(self.claim_path.exists(), f"{mode}: no claim was written. " + out)
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: no lease was opened. " + out)
        # control: no lookup is late, so the same spelling runs, bound to the bytes it was judged as
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=f"control {mode}"):
                shas.unlink(missing_ok=True)
                r = self._r10_run(mode, spelled, FAKE_LEG_SPEC_SHA=str(shas))
                out = f"control {mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertEqual(r.returncode, 0, out)
                self.assertEqual(shas.read_text(encoding="utf-8").splitlines()[:1], [f"expect={want} read={want}"], out)

    def test_r10_a1_a_key_lookup_that_gets_no_answer_never_leaves_a_twin_spellings_swap_to_the_guard_after_the_claim(self) -> None:
        # adversary pass 1 on 5a73d8e8, A1: entry 1 (S.JSON) fell back to its spelled path and entry 2 (s.json, the same file) resolved to the final one: two keys, so the file's bytes swapped between the two
        # entries passed the per-entry checks, a claim was taken and only the content guard then refused it. A key that fell back is refused before any claim (SPEC_KEY_UNRESOLVED).
        spec = self.case / "r10-a1-twin.json"
        spec.write_text(self.specs[0].read_text(encoding="utf-8"), encoding="utf-8")
        other = self.case / "r10-a1-other-bytes.json"
        other.write_text(self.specs[1].read_text(encoding="utf-8"), encoding="utf-8")
        swap = ("        if ($env:DVT_SWAP_FROM -and [IO.Path]::GetFileName($path) -ceq 'r10-a1-twin.json') { [IO.File]::Copy($env:DVT_SWAP_FROM, $path, $true) }   # TEST FIXTURE: the twin's bytes change before entry 2\n"
                "        $shapeRefusal = Get-ScalarLegShapeRefusal $path\n")
        self._mirror_with_bad_sidecar(runner_patches=(("        $shapeRefusal = Get-ScalarLegShapeRefusal $path\n", swap),))
        shas = self.case / "leg-spec-sha-r10-a1.log"
        r = self.run_batch([spec.with_name("R10-A1-TWIN.JSON"), spec], "-SpecDigestKeyDelay", "1:1500", FAKE_LEG_SPEC_SHA=str(shas), DVT_SWAP_FROM=str(other))
        out = r.stdout[-3000:] + r.stderr[-3000:]
        self.assertEqual(spec.read_bytes(), other.read_bytes(), "the fixture really swapped the twin's bytes between the two entries. " + out)
        self.assertFalse(shas.exists(), "a leg ran. " + out)
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("SPEC_KEY_UNRESOLVED", r.stderr, "old: the claim was taken and the content guard refused it after (SPEC_CHANGED_AFTER_PREFLIGHT). " + out)
        self.assertNotIn("SPEC_CHANGED_AFTER_PREFLIGHT", r.stderr, out)
        self.assertEqual(self.kinds(), [], "no claim, lease or leg. " + out)
        self.assertFalse(self.claim_path.exists(), out)
        self.assertFalse((self.claims / "bachelor.tickets").exists() and any((self.claims / "bachelor.tickets").iterdir()), "no ticket was taken: the claim was never attempted. " + out)

    def test_r10_p2_a1_two_hard_links_of_one_spec_share_a_key_so_a_swap_between_them_is_refused_before_any_claim(self) -> None:
        # adversary pass 2 on 0cffc10b, A1: the key was the file's FINAL PATH, which a hard link does not share with its twin name: a.json and b.json (one file) had two keys, so the bytes rewritten
        # in place between the two entries passed the per-entry checks, the claim was taken and only the content guard then refused it. The key is the file's identity now: one file, one key.
        a = self.case / "r10-p2-a1-a.json"
        a.write_text(self.specs[0].read_text(encoding="utf-8"), encoding="utf-8")
        b = self.case / "r10-p2-a1-b.json"
        os.link(a, b)
        other = self.case / "r10-p2-a1-other-bytes.json"
        other.write_text(self.specs[1].read_text(encoding="utf-8"), encoding="utf-8")
        planned = hashlib.sha256(a.read_bytes()).hexdigest()
        swap = ("        if ($env:DVT_SWAP_FROM -and [IO.Path]::GetFileName($path) -ceq 'r10-p2-a1-b.json') { [IO.File]::Copy($env:DVT_SWAP_FROM, $path, $true) }   # TEST FIXTURE: the shared file's bytes change before entry 2\n"
                "        $shapeRefusal = Get-ScalarLegShapeRefusal $path\n")
        self._mirror_with_bad_sidecar(runner_patches=(("        $shapeRefusal = Get-ScalarLegShapeRefusal $path\n", swap),))
        shas = self.case / "leg-spec-sha-r10-p2-a1.log"
        r = self.run_batch([a, b], FAKE_LEG_SPEC_SHA=str(shas), DVT_SWAP_FROM=str(other))
        out = r.stdout[-3000:] + r.stderr[-3000:]
        self.assertEqual(a.read_bytes(), other.read_bytes(), "the fixture rewrote the one file through its second name. " + out)
        self.assertFalse(shas.exists(), "a leg ran. " + out)
        self.assertEqual(r.returncode, 2, out)
        self.assertIn(f"was judged as sha256 {planned} but a later preflight read of the same path judged sha256 {hashlib.sha256(other.read_bytes()).hexdigest()}", r.stderr,
                      "old: the two names had two keys and the claim was taken before the content guard refused. " + out)
        self.assertEqual(self.kinds(), [], out)

    def test_r10_a2_a_key_lookup_is_bounded_by_what_is_left_before_the_deadline(self) -> None:
        # adversary pass 1 on 5a73d8e8, A2: the lookup waited a fixed 1 s, so a spec preflight that started it with 250 ms left refused about 750 ms past -DeadlineUtc. The mirror sets the deadline 250 ms
        # away right before the -LegSpec shape check and stamps the refusal's own time; the lookup (made to answer 3 s late) must wait only what the read bound allows (nothing here).
        spec, spelled = self._r10_spelled_spec()
        patches = (("        $shapeRefusal = Get-ScalarLegShapeRefusal $path\n",
                    "        $deadline = [DateTime]::UtcNow.AddMilliseconds(250); [Console]::Error.WriteLine('DVT_DEADLINE_UTC=' + $deadline.ToString('o'))   # TEST FIXTURE\n"
                    "        $shapeRefusal = Get-ScalarLegShapeRefusal $path\n"),
                   ("function Stop-Usage([string]$Message) { [Console]::Error.WriteLine(\"BATCH_USAGE $Message\");",
                    "function Stop-Usage([string]$Message) { [Console]::Error.WriteLine('DVT_USAGE_UTC=' + [DateTime]::UtcNow.ToString('o') + ' DVT_KEYCALLS=' + $script:SpecDigestKeyCalls); [Console]::Error.WriteLine(\"BATCH_USAGE $Message\");"))
        self._mirror_with_bad_sidecar(runner_patches=patches)
        r = self.run_batch([spelled], "-SpecDigestKeyDelay", "1:3000")
        out = r.stdout[-3000:] + r.stderr[-3000:]
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("SPEC_READ_TIMEOUT", r.stderr, out)
        stamps = {k: datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", re.search(rf"{k}=(\S+)", r.stderr).group(1)).replace("Z", "+00:00")) for k in ("DVT_DEADLINE_UTC", "DVT_USAGE_UTC")}
        late = (stamps["DVT_USAGE_UTC"] - stamps["DVT_DEADLINE_UTC"]).total_seconds()
        self.assertLess(late, 0.4, f"old: the fixed 1 s lookup ended the preflight about 0.75 s past -DeadlineUtc ({late:.2f} s). " + out)
        # adversary pass 2 on 0cffc10b, A3: with no room no lookup STARTS (a zero wait still handed the pool a task that opened the file after the caller gave up)
        self.assertIn("DVT_KEYCALLS=0", r.stderr, "old: one lookup was started with a zero wait. " + out)
        # adversary pass 2, A4: the deadline is the script's own variable, never a Get-Variable call that a caller's $PSDefaultParameterValues can blank (structural: the key function reads $script:deadline)
        body = RUNNER.read_text(encoding="utf-8")
        body = body[body.index("function Get-SpecDigestKey("):body.index("function Get-UnresolvedSpecKeyRefusal(")]
        self.assertIn("$script:deadline -is [datetime]", body)
        self.assertNotIn("Get-Variable", body)
        self.assertNotIn("Get-SpecReadBoundSec", body)

    def test_r10_an_entry_with_no_judged_digest_is_refused_before_any_claim_in_every_form(self) -> None:
        # the second half of the fix, whatever made the digest empty: the runner's own store of the budget read's digest is removed in a mirror, so the entry has none. Old: the leg ran
        # with no -ExpectSpecSha256 ('' -cne '' is false). New: the typed SPEC_NOT_JUDGED before any claim or lease.
        spec, spelled = self._r10_spelled_spec()
        for mode in ("-LegSpec", "-LegPlan"):
            with self.subTest(mode=mode):
                shutil.rmtree(self.case / "tree", ignore_errors=True)
                self._mirror_with_bad_sidecar(runner_patches=(("    $script:SpecBudgetSha256[$digestKey] = $judgedSha\n", "    $null = $judgedSha   # TEST FIXTURE: the budget read's digest is never stored\n"),))
                self.plan_runner = self.runner
                shas = self.case / "leg-spec-sha-r10-b.log"
                shas.unlink(missing_ok=True)
                r = self._r10_run(mode, spelled, FAKE_LEG_SPEC_SHA=str(shas))
                out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertFalse(shas.exists(), "a leg ran on bytes nothing judged. " + out)
                self.assertEqual([k for k in self.kinds() if k.startswith("LEG")], [], out)
                self.assertEqual(r.returncode, 2, out)
                self.assertIn("SPEC_NOT_JUDGED", r.stderr, out)
                self.assertFalse(self.claim_path.exists(), f"{mode}: no claim was written. " + out)
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: no lease was opened. " + out)

    def test_r10_the_digest_key_delay_seam_is_refused_with_the_real_um_run_and_is_checked(self) -> None:
        spec, spelled = self._r10_spelled_spec()
        args, e = self.batch_cmd([spec], "-SpecDigestKeyDelay", "1:5")
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-SpecDigestKeyDelay is an offline test seam", r.stderr)
        for bad in ("soon", "١:1", "0:5"):   # (pass 2 A9: the ordinal counts from 1, so 0 names no lookup)   # (adversary pass 1 A4: an Arabic-Indic digit is not a digit of this seam; \d matched it and the Int32 cast threw instead of the typed usage)
            r = self.run_batch([spec], "-SpecDigestKeyDelay", bad)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("is not <n>:<ms>", r.stderr)

    def test_r19_b2_a_stalled_budget_spec_read_is_bounded_by_the_deadline_and_refused_before_the_claim(self) -> None:
        # EXPORT-VENUE-LEG-1 r19 (sol r18 B2 = pass20-A2): the budget preflight's own read of the spec was a synchronous ReadAllBytes BEFORE the remaining-room check, so a share that
        # stopped answering there (every earlier read of the spec had answered) held the batch past -DeadlineUtc. The seam -ExportBudgetSpecReadDelaySec makes only that read answer
        # after 300 s. It is bounded by the room before -DeadlineUtc and refused EXPORT_BUDGET_PREFLIGHT_TIMEOUT before any claim, long before the read would have answered.
        fake = self.case / "fake-export-budget-r19-b2.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames, [long]$DescribeTimeBudgetBytes = -1)\n"
                        "[pscustomobject]@{ jobTimeoutSec = 1 }\n", encoding="utf-8")
        spec = self.case / "export-r19-b2.json"
        spec.write_text((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"), encoding="utf-8")
        for mode, room in (("-LegSpec", 30), ("-LegPlan", 100)):
            with self.subTest(mode=mode):
                for p in (self.events, self.leg_args, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                deadline = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=room))
                t0 = time.monotonic()
                if mode == "-LegSpec":
                    r = self.run_batch([spec], "-ExportBudgetScript", str(fake), "-ExportBudgetSpecReadDelaySec", "300", "-DeadlineUtc", deadline)
                    line = "BATCH_USAGE EXPORT_BUDGET_PREFLIGHT_TIMEOUT"
                else:
                    r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), "-ExportBudgetSpecReadDelaySec", "300", "-DeadlineUtc", deadline)
                    line = "LEG_PLAN_REFUSED reason=EXPORT_BUDGET_PREFLIGHT_TIMEOUT item=1 "
                took = time.monotonic() - t0
                out = f"{mode}: " + r.stdout[-3000:] + r.stderr[-3000:]
                self.assertEqual(r.returncode, 2, out)
                self.assertIn(line, r.stderr, out)
                self.assertIn("read of the leg spec did not answer", r.stderr, out)
                self.assertLess(took, room + 15, f"{mode}: the stalled read held the batch past -DeadlineUtc. " + out)
                self.assertEqual(self.kinds(), [], f"{mode}: nothing is submitted. " + out)
                self.assertFalse(self.claim_path.exists(), f"{mode}: no claim was written. " + out)
        # the seam is refused with the real um-run.ps1
        args, e = self.batch_cmd([spec], "-ExportBudgetSpecReadDelaySec", "5")
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-ExportBudgetSpecReadDelaySec is an offline test seam", r.stderr)

    def test_r11_export_a_budget_preflight_that_never_returns_is_killed_and_refused_before_the_claim_in_every_mode(self) -> None:
        # EXPORT-VENUE-LEG-1 r11 (sol r9 B1, EXPORT-BUDGET-PREFLIGHT-CHILD-DEADLINE-1; fable H1 EXPORT-LEGSET-BUDGET-PREFLIGHT-TEST-1): the preflight child (the export generator's
        # -PreflightBudgetOnly mode, which resolves the clip) was waited for with no deadline, so a hung resolver held the batch before any claim forever. It now runs as a bounded
        # child (the room left before -DeadlineUtc, under a hard cap), is killed and confirmed dead on expiry, and the batch ends with the typed refusal before any claim or lease,
        # in -LegSpec, -LegPlan and -LegSet. The stand-in sleeps forever and records its pid, so the test also proves the child did not outlive the batch.
        pid_file = self.case / "budget-child.pid"
        fake = self.case / "fake-export-budget-hang.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames)\n"
                        "Set-Content -LiteralPath $env:FAKE_BUDGET_PID_FILE -Value $PID\n"
                        "while ($true) { Start-Sleep -Seconds 60 }\n", encoding="utf-8")
        shipped = (DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8")
        spec = self.case / "export-hang.json"
        spec.write_text(shipped, encoding="utf-8")
        shipped_doc = json.loads(shipped)
        legset_name = f"zz-r11-budget-hang-{uuid.uuid4().hex[:8]}"
        legset_path = DV / "legsets" / f"{legset_name}.json"
        short = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=30))
        # the plan and set forms spend their preamble (module import, spec predicates) before the preflight on a loaded host: more room, so the child is started and then bound by it
        longer = lambda: utc_iso(datetime.now(timezone.utc) + timedelta(seconds=100))  # noqa: E731

        def assert_child_gone(mode: str, r: subprocess.CompletedProcess) -> None:
            self.assertTrue(pid_file.exists(), f"{mode}: the stand-in never started, so the kill path was not exercised: {r.stderr}")
            pid = pid_file.read_text(encoding="utf-8").strip().lstrip("﻿")
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=60).stdout
            self.assertNotRegex(alive, rf"\b{re.escape(pid)}\b", f"{mode}: the hung preflight child {pid} was left running")

        def assert_nothing_started(mode: str, r: subprocess.CompletedProcess) -> None:
            self.assertEqual(r.returncode, 2, f"{mode}: " + r.stdout + r.stderr)
            self.assertIn("EXPORT_BUDGET_PREFLIGHT_TIMEOUT", r.stderr, f"{mode}: " + r.stdout + r.stderr)
            self.assertEqual(self.kinds(), [], f"{mode}: nothing is submitted: no open, no probe, no leg")
            self.assertFalse(self.claim_path.exists(), f"{mode}: no claim was written")
            self.assertFalse((self.run_dir / "batches.jsonl").exists(), f"{mode}: no lease was opened")
            assert_child_gone(mode, r)

        def reset() -> None:
            for p in (self.events, self.leg_args, self.claim_path, pid_file):
                p.unlink(missing_ok=True)
            shutil.rmtree(self.run_dir, ignore_errors=True)

        reset()
        t0 = time.monotonic()
        r = self.run_batch([spec], "-ExportBudgetScript", str(fake), "-DeadlineUtc", short, FAKE_BUDGET_PID_FILE=str(pid_file))
        assert_nothing_started("-LegSpec", r)
        self.assertIn("BATCH_USAGE EXPORT_BUDGET_PREFLIGHT_TIMEOUT", r.stderr)
        self.assertLess(time.monotonic() - t0, 60, "the preflight was bounded by the room before -DeadlineUtc, not by the child")
        reset()
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), "-DeadlineUtc", longer(), FAKE_BUDGET_PID_FILE=str(pid_file))
        assert_nothing_started("-LegPlan", r)
        self.assertIn("LEG_PLAN_REFUSED reason=EXPORT_BUDGET_PREFLIGHT_TIMEOUT item=1 ", r.stderr)
        # -LegSet: a set under legsets/ naming the shipped export leg (the real legsets/ directory is the only place the batch reads one; removed in the finally)
        legset_path.write_text(json.dumps({"schema": "mlv-app/dual-venue-legset/v1", "legSet": legset_name, "card": shipped_doc["card"], "clipId": shipped_doc["clipId"],
                                           "description": "r11 test stand-in, removed by the test", "legs": ["legs/m16-1243-export-prores.json"]}), encoding="utf-8")
        try:
            reset()
            args, e = self.batch_cmd([spec], "-ExportBudgetScript", str(fake), "-DeadlineUtc", longer(), FAKE_BUDGET_PID_FILE=str(pid_file))
            args[args.index("-LegSpec"):args.index("-LegSpec") + 2] = ["-LegSet", legset_name]
            r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)
            assert_nothing_started("-LegSet", r)
            self.assertIn("BATCH_USAGE EXPORT_BUDGET_PREFLIGHT_TIMEOUT", r.stderr)
        finally:
            legset_path.unlink(missing_ok=True)

    def test_r11_export_a_budget_preflight_with_no_room_before_the_deadline_never_starts_the_child(self) -> None:
        # the same class from the other side: with no room left before -DeadlineUtc the child is not started at all (no process to leave behind), and the refusal is the same typed one
        pid_file = self.case / "budget-child-noroom.pid"
        fake = self.case / "fake-export-budget-noroom.ps1"
        fake.write_text("param([switch]$PreflightBudgetOnly, [string]$ClipId, [string]$Format, [int]$MaxFrames)\n"
                        "Set-Content -LiteralPath $env:FAKE_BUDGET_PID_FILE -Value $PID\n[pscustomobject]@{ jobTimeoutSec = 1 }\n", encoding="utf-8")
        spec = self.case / "export-noroom.json"
        spec.write_text((DV / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"), encoding="utf-8")
        for p in (self.events, self.claim_path, pid_file):
            p.unlink(missing_ok=True)
        r = self.run_batch([spec], "-ExportBudgetScript", str(fake), "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=1)), FAKE_BUDGET_PID_FILE=str(pid_file))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        # r19 (adversary pass 22 A1): with no room left no spec read starts either, so the refusal can now come at the first read (SPEC_READ_TIMEOUT), before the budget's own
        noroom = r"BATCH_USAGE (EXPORT_BUDGET_PREFLIGHT_TIMEOUT|SPEC_READ_TIMEOUT) "
        self.assertRegex(r.stderr, noroom)
        self.assertFalse(pid_file.exists(), "no preflight child was started with no room left")
        self.assertEqual(self.kinds(), [])
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        # adversary r11 A4: a -DeadlineUtc far in the past is a room of billions of negative seconds; narrowing that to an Int32 threw before the no-room test, so the scalar and set forms
        # lost the typed refusal and the plan form reported a generic LEG_PLAN_INVALID. The room is compared as a double first.
        for p in (self.events, self.claim_path, pid_file):
            p.unlink(missing_ok=True)
        r = self.run_batch([spec], "-ExportBudgetScript", str(fake), "-DeadlineUtc", "0001-01-01T00:00:00Z", FAKE_BUDGET_PID_FILE=str(pid_file))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        # adversary pass 22 A1: a deadline long gone starts NO read (a 1 s floor read the spec anyway and refused only at the budget): the first read is the typed refusal
        self.assertIn("BATCH_USAGE SPEC_READ_TIMEOUT ", r.stderr)
        self.assertFalse(pid_file.exists(), "no preflight child was started with no room left")
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]), "-ExportBudgetScript", str(fake), "-DeadlineUtc", "0001-01-01T00:00:00Z", FAKE_BUDGET_PID_FILE=str(pid_file))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        # (r19: the plan file's own bounded read refuses first with no room: LEG_PLAN_UNREADABLE, still typed and never LEG_PLAN_INVALID)
        self.assertRegex(r.stderr, r"LEG_PLAN_REFUSED reason=(EXPORT_BUDGET_PREFLIGHT_TIMEOUT item=1 |SPEC_READ_TIMEOUT item=1 |LEG_PLAN_UNREADABLE )")
        self.assertNotIn("LEG_PLAN_INVALID", r.stderr)
        self.assertFalse(pid_file.exists(), "no preflight child was started with no room left")

    def test_dualvenue_bool_args_a_non_boolean_generatorargs_switch_is_refused_by_the_batch_entrypoint_before_the_claim(self) -> None:
        # DUAL-VENUE-RUNNER-BOOL-ARGS-STRICT-1 r2 (inherited A1, Dot pr-757): the strict-boolean refusal lives in the shared shape predicate; this drives it through
        # the BATCH entrypoint (scalar -LegSpec mode and -LegPlan mode), not only the predicate and the leg. A string, 0/1 and null are each refused, naming the field.
        base = json.loads(self._valid_leg_spec_text())
        cases = (("disableLookAssist", "false"), ("utilSample", "true"), ("unpaced", 1))   # few launches: the predicate test covers every switch and value
        for name, value in cases:
            with self.subTest(mode="scalar", name=name, value=value):
                for p in (self.events, self.claim_path):
                    p.unlink(missing_ok=True)
                spec = self.case / f"bool-{name}.json"
                spec.write_text(json.dumps({**base, "generatorArgs": {name: value}}), encoding="utf-8")
                r = self.run_batch([spec])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_USAGE LEG_SPEC_INVALID", r.stderr)
                self.assertIn(f"generatorArgs.{name} must be JSON true or false", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")
        for name, value in cases[1:2]:
            with self.subTest(mode="plan", name=name, value=value):
                p = self.case / f"bool-plan-{name}.json"
                p.write_text(json.dumps({**base, "generatorArgs": {name: value}}), encoding="utf-8")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(p, "B", self.commit_a)]), "LEG_SPEC_INVALID item=2 ")

    def test_r9_dualiso_a_scalar_leg_spec_whose_legtype_is_not_one_enumerated_string_is_refused_before_the_claim(self) -> None:
        # CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r9 (adversary r8 cc8061a2 A1): legType ["look","speed"] with no look stringified to 'look speed', passed the shared
        # shape predicate, opened the lease, and the leg then indexed the absent look (LEG_FAILED, no receipt). Every non-enum legType is refused before any claim.
        # fix 1 (adversary r9 a0936637 A2): and a look leg whose look.contactSheetFrames is outside the generator's 1..60, or not a whole number
        base = {k: v for k, v in json.loads(self._valid_leg_spec_text()).items() if k != "look"}
        cases = [(case, {**base, "legType": value}, "legType must be one of")
                 for case, value in (("array", ["look", "speed"]), ("object", {"type": "look"}), ("number", 1), ("null", None), ("empty", ""), ("case", "LOOK"))]
        cases += [(f"count-{n}", {**base, "legType": "look", "look": {"contactSheetFrames": n, "lookFlavor": "classic"}}, "look.contactSheetFrames must be a whole number")
                  for n in (61, 0, 1.4)]
        for case, doc, needle in cases:
            with self.subTest(case=case):
                for p in (self.events, self.claim_path):
                    p.unlink(missing_ok=True)
                spec = self.case / f"legtype-{case}.json"
                spec.write_text(json.dumps(doc), encoding="utf-8")
                r = self.run_batch([spec])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_USAGE LEG_SPEC_INVALID", r.stderr)
                self.assertIn(needle, r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_r9_a_spec_in_the_run_dir_under_another_name_is_not_refused(self) -> None:
        # the guard names the runner's outputs, not the directory: a spec kept beside them under any other name still runs
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir.mkdir(parents=True)
        spec = self.run_dir / "gate.log.spec.json"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertNotIn("LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
        self.assertNotEqual(r.returncode, 2, r.stdout + r.stderr)

    # r10 (adversary r9 A1): the r9 guard compared LEXICAL parents, so an input reached through a junction that targets -RunDir (or an ancestor of it) passed preflight and was
    # overwritten after the claim. One case per call site of the one helper (the plan file, a plan item's spec, a scalar spec), the aliases that matter (the input, an ancestor
    # of it, -RunDir itself, a runner-owned directory that is a link) and the fail-closed case. Each asserts the physical file is byte-unchanged and nothing was submitted.
    def _junction(self, link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _assert_alias_refused(self, r: subprocess.CompletedProcess, physical: Path, before: str, marker: str) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(marker, r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        self.assertEqual(physical.read_text(encoding="utf-8"), before, "the physical file is byte-unchanged")

    def _fresh_run_dir(self) -> None:
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir.mkdir(parents=True)

    def test_r10_a_leg_spec_reached_through_a_junction_to_the_run_dir_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "gate.log", "batch.log", "batch-jobs/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                alias = self.case / "alias"
                if alias.exists():
                    os.rmdir(alias)
                self._junction(alias, self.run_dir)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(alias / leaf, "A", self.commit_a)]))
                self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                os.rmdir(alias)

    def test_r10_a_leg_spec_reached_through_a_junction_to_an_ancestor_of_the_run_dir_is_refused(self) -> None:
        self._fresh_run_dir()
        alias = self.tmp / f"up-{self.case.name}"
        self._junction(alias, self.case)
        try:
            spec = self.run_dir / "leg-plan.json"
            spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
            before = spec.read_text(encoding="utf-8")
            r = self.run_plan(self.write_plan([self.item(alias / "run" / "leg-plan.json", "A", self.commit_a)]))
            self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
        finally:
            os.rmdir(alias)

    def test_r10_a_plan_file_reached_through_a_junction_to_the_run_dir_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "batch.log", "batch-jobs/plan.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                alias = self.case / "alias"
                if alias.exists():
                    os.rmdir(alias)
                self._junction(alias, self.run_dir)
                plan = self.run_dir / leaf
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
                before = plan.read_text(encoding="utf-8")
                r = self.run_plan(alias / leaf)
                self._assert_alias_refused(r, plan, before, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")
                os.rmdir(alias)

    def test_r10_a_scalar_leg_spec_reached_through_a_junction_to_the_run_dir_is_refused_before_the_claim(self) -> None:
        for leaf in ("gate.log", "leg-plan.json", "quiet/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                alias = self.case / "alias"
                if alias.exists():
                    os.rmdir(alias)
                self._junction(alias, self.run_dir)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_batch([alias / leaf])
                self._assert_alias_refused(r, spec, before, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")
                os.rmdir(alias)

    def test_r10_a_run_dir_given_as_a_junction_alias_still_refuses_the_physical_output(self) -> None:
        # the other direction: -RunDir is the alias, the input is spelled with the physical path
        real = self.case / "real-run"
        real.mkdir()
        spec = real / "gate.log"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        before = spec.read_text(encoding="utf-8")
        alias = self.case / "run-alias"
        self._junction(alias, real)
        try:
            self.run_dir = alias
            r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
            self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
        finally:
            os.rmdir(alias)

    def test_r10_a_spec_inside_a_runner_owned_directory_that_is_a_link_is_refused(self) -> None:
        # <RunDir>\quiet is a junction elsewhere: Wait-VenueQuiet -WorkDir writes into the target, so a spec there is a runner output
        self._fresh_run_dir()
        elsewhere = self.case / "elsewhere"
        self._junction(self.run_dir / "quiet", elsewhere)
        spec = elsewhere / "venue-quiet-probe-x.json"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        before = spec.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")

    # r10 second pass (adversary A1): a HARD link to a runner output has its own final path and is not a reparse point, so a path comparison, however physical, accepts it
    # while the runner overwrites the same file through the output's name. The guard also compares file identity (volume + file index).
    def _hard_linked(self, output: Path, name: Path) -> str:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        name.parent.mkdir(parents=True, exist_ok=True)
        os.link(output, name)
        return output.read_text(encoding="utf-8")

    def test_r10_a_plan_item_spec_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "gate.log", "batch-jobs/x.json", "quiet/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                safe = self.case / "safe" / "fx-a.json"
                safe.unlink(missing_ok=True)
                before = self._hard_linked(self.run_dir / leaf, safe)
                r = self.run_plan(self.write_plan([self.item(safe, "A", self.commit_a)]))
                self._assert_alias_refused(r, safe, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                self.assertIn("is a hard link to", r.stderr)

    def test_r10_a_plan_file_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        self._fresh_run_dir()
        plan = self.case / "safe-plan.json"
        plan.unlink(missing_ok=True)
        output = self.run_dir / "leg-plan.json"
        output.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
        os.link(output, plan)
        before = plan.read_text(encoding="utf-8")
        r = self.run_plan(plan)
        self._assert_alias_refused(r, plan, before, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")

    def test_r10_a_scalar_spec_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        self._fresh_run_dir()
        safe = self.case / "safe" / "fx-a.json"
        safe.unlink(missing_ok=True)
        before = self._hard_linked(self.run_dir / "gate.log", safe)
        r = self.run_batch([safe])
        self._assert_alias_refused(r, safe, before, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")

    def test_r10_a_hard_linked_spec_whose_other_name_is_not_a_runner_output_is_not_refused(self) -> None:
        # more than one name is not the test: the other name must be a file this run writes
        self._fresh_run_dir()
        safe = self.case / "safe" / "fx-a.json"
        safe.unlink(missing_ok=True)
        self._hard_linked(self.run_dir / "gate.log.spec.json", safe)
        r = self.run_plan(self.write_plan([self.item(safe, "A", self.commit_a)]))
        self.assertNotIn("IS_RUN_OUTPUT", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r10_an_input_whose_physical_path_cannot_be_resolved_is_refused_before_the_claim(self) -> None:
        # fail closed: the resolver is made to fail (as it does for a path the file system will not open); the plan file, a plan item's spec and a scalar spec each refuse
        anchor = "[VenueLeaseBatchFinalPath]::Resolve([IO.Path]::GetFullPath($Path))"
        runner = self._patched_runner(anchor, "throw 'injected: the path will not open'")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a)])
        spec_text = self.specs[0].read_text(encoding="utf-8")
        args, e = self.plan_cmd(plan)
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self._assert_alias_refused(r, self.specs[0], spec_text, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")
        self.assertIn("cannot be resolved to a physical path", r.stderr)
        args, e = self.batch_cmd([self.specs[0]])
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self._assert_alias_refused(r, self.specs[0], spec_text, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")
        self.assertIn("cannot be resolved to a physical path", r.stderr)

    def test_r10_a_junction_that_does_not_reach_the_run_dir_is_not_refused(self) -> None:
        # the guard resolves paths, it does not distrust links: a spec kept behind a junction elsewhere still runs
        self._fresh_run_dir()
        kept = self.case / "kept-specs"
        self._junction(self.case / "spec-alias", kept)
        shutil.copy2(self.specs[0], kept / "fx-a.json")
        r = self.run_plan(self.write_plan([self.item(self.case / "spec-alias" / "fx-a.json", "A", self.commit_a)]))
        self.assertNotIn("IS_RUN_OUTPUT", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # r11 (sol r10 blockers 1-3 = producer OPEN r10-A1..A3). Round one item 2: every input is validated BEFORE the lease opens, and no accepted input is overwritten, deleted or made
    # unusable later. A1: the runner's outputs are not only <RunDir>\ files: VenueChain.psm1 also writes <ClaimDir>\bachelor.claim.json (deleted as a stale claim when unparseable),
    # <ClaimDir>\bachelor.lease-ledger.jsonl (appended to) and <ClaimDir>\bachelor.tickets\ (ticket files). A2: every generated output name must be openable. A3: the guard runs again
    # once the claim is held, because an alias can appear while the batch waits on a foreign claim.
    CLAIM_DIR_OUTPUTS = ("bachelor.claim.json", "bachelor.lease-ledger.jsonl", "bachelor.tickets/0000000000000000001-p1-1.ticket.json")

    def _claim_dir_input(self, leaf: str, age_s: int = 31) -> tuple[Path, str]:
        spec = self.claims / leaf
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        old = time.time() - age_s
        os.utime(spec, (old, old))
        return spec, spec.read_text(encoding="utf-8")

    def _assert_nothing_submitted_input_intact(self, r: subprocess.CompletedProcess, path: Path, before: str, marker: str) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(marker, r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertTrue(path.exists(), "the accepted input was not removed")
        self.assertEqual(path.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    def test_r11_a1_a_plan_item_spec_at_a_claim_dir_file_is_refused_when_run_dir_is_the_claim_dir(self) -> None:
        # sol r10 blocker 1, literal: RunDir=ClaimDir, a valid spec at bachelor.claim.json (age 31 s: past the 30 s grace, so the claim reader types it STALE/unreadable and
        # Invoke-VenueClaimAttempt removes it) and at its ledger sibling (Add-VenueLeaseLedger appends to it).
        for leaf in self.CLAIM_DIR_OUTPUTS:
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                self.run_dir = self.claims
                spec, before = self._claim_dir_input(leaf)
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")

    def test_r11_a1_a_claim_dir_file_is_refused_whatever_the_run_dir_is(self) -> None:
        # the claim dir is a runner output location in its own right, not only when it is also -RunDir
        for leaf in self.CLAIM_DIR_OUTPUTS:
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                spec, before = self._claim_dir_input(leaf)
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                shutil.rmtree(self.claims, ignore_errors=True)
                spec, before = self._claim_dir_input(leaf)
                r = self.run_batch([spec])
                self._assert_nothing_submitted_input_intact(r, spec, before, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")

    def test_r11_a1_a_plan_file_at_a_claim_dir_file_is_refused_before_the_claim(self) -> None:
        for leaf in ("bachelor.claim.json", "bachelor.lease-ledger.jsonl"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                self.run_dir = self.claims
                plan = self.claims / leaf
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
                old = time.time() - 31
                os.utime(plan, (old, old))
                before = plan.read_text(encoding="utf-8")
                r = self.run_plan(plan)
                self._assert_nothing_submitted_input_intact(r, plan, before, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")

    def test_r11_a1_a_spec_hard_linked_to_a_claim_dir_file_is_refused_before_the_claim(self) -> None:
        for leaf in ("bachelor.claim.json", "bachelor.lease-ledger.jsonl"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                safe = self.case / "safe" / "fx-a.json"
                safe.unlink(missing_ok=True)
                before = self._hard_linked(self.claims / leaf, safe)
                r = self.run_plan(self.write_plan([self.item(safe, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, safe, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                self.assertIn("is a hard link to", r.stderr)

    def test_r11_a1_a_spec_in_the_claim_dir_under_another_name_is_not_refused(self) -> None:
        self._fresh_run_dir()
        shutil.rmtree(self.claims, ignore_errors=True)
        self.run_dir = self.claims
        spec, _ = self._claim_dir_input("fx-a.spec.json")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertNotIn("IS_RUN_OUTPUT", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _long_name_spec(self, basename_len: int) -> Path:
        # a spec whose legId is not a usable leg name (82 chars, over the 81 the runner takes), so the file's own basename becomes the leg name (sol r10 blocker 2, literal)
        # (kept off the deep test temp folder: the spec's own path must stay under MAX_PATH, so the only over-long name is the one the RUNNER generates from it)
        d = None
        for root in (os.environ.get("PUBLIC", ""), (os.environ.get("SystemDrive", "C:") + "\\"), tempfile.gettempdir()):
            try:
                cand = Path(tempfile.mkdtemp(prefix="lb", dir=root)) if root else None
            except OSError:
                continue
            if cand is not None and len(str(cand / ("a" * basename_len + ".json"))) < 259:
                d = cand
                break
            if cand is not None:
                shutil.rmtree(cand, ignore_errors=True)
        if d is None:
            self.skipTest("no folder is shallow enough to keep the spec's own path under MAX_PATH")
        self.addCleanup(shutil.rmtree, d, True)
        spec = d / ("a" * basename_len + ".json")
        spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "x" * 82, "backends": ["cpu"], "scaleFactor": 1}), encoding="utf-8")
        return spec

    def test_r11_a2_a_generated_output_name_over_the_volume_limit_is_refused_before_the_claim(self) -> None:
        # 205-char basename + 32-char arm: the quiet output is 266 characters, the volume's component limit 255; redirecting into it failed AFTER the lease opened.
        spec = self._long_name_spec(205)
        r = self.run_plan(self.write_plan([self.item(spec, "A" * 32, self.commit_a)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=OUTPUT_NAME_TOO_LONG item=1 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_r11_a2_the_scalar_form_refuses_a_generated_output_name_over_the_limit_too(self) -> None:
        spec = self._long_name_spec(220)   # no -Backend: the leg name ends in "-spec", and quiet-01-<220>-spec-b9999-a999999.out.txt is 256 characters
        r = self.run_batch([spec])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE OUTPUT_NAME_TOO_LONG", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_r11_a2_a_short_name_with_a_full_length_arm_still_runs(self) -> None:
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A" * 32, self.commit_a)]))
        self.assertNotIn("OUTPUT_NAME_TOO_LONG", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _foreign_live_claim(self) -> None:
        # a LIVE claim of another owner: a real sleeping process, its real start time (Get-VenueClaimStaleReason finds a live owner and the claim not yet expired)
        holder = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 300"])
        self.addCleanup(lambda: holder.poll() is None and subprocess.run(["taskkill", "/PID", str(holder.pid), "/T", "/F"], capture_output=True, text=True, timeout=60))
        t = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {holder.pid}).StartTime.ToUniversalTime().ToString('o')"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(t.returncode, 0, t.stdout + t.stderr)
        now = datetime.now(timezone.utc)
        self.claims.mkdir(parents=True, exist_ok=True)
        self.claim_path.write_text(json.dumps({"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "ownerPid": holder.pid, "ownerCreatedUtc": t.stdout.strip(),
                                               "host": os.environ.get("COMPUTERNAME", ""), "runDir": str(self.case / "foreign"), "purpose": "foreign", "claimedUtc": utc_iso(now),
                                               "expiresUtc": utc_iso(now + timedelta(hours=1)), "nonce": "f" * 32}, separators=(",", ":")) + "\n", encoding="utf-8")

    def _wait_for_log(self, text: str, timeout_s: float = 90) -> None:
        end = time.monotonic() + timeout_s
        log = self.run_dir / "batch.log"
        while time.monotonic() < end:
            try:
                if text in log.read_text(encoding="utf-8"):
                    return
            except OSError:
                pass
            time.sleep(0.2)
        self.fail(f"{text!r} never reached {log}")

    def _open_release(self) -> Path:
        """The file whose existence ends the fake um-run's open wait (FAKE_OPEN_RELEASE); absent until the case releases the open."""
        rel = self.case / "open.release"
        rel.unlink(missing_ok=True)
        return rel

    def _wait_for_open_wait(self, timeout_s: float = 90) -> None:
        """Returns once the fake um-run is blocked in the open's wait (its OPENWAIT event, logged just before it blocks), so what the case does next lands inside that wait."""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                if any(" OPENWAIT " in line for line in self.events_raw()):
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("the open never reached its wait")

    def _alias_during_claim_wait(self, args: list[str], e: dict, spec: Path, alias: Path) -> tuple[int, str, str]:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("CLAIM_HELD")
            os.link(spec, alias)
            self.claim_path.unlink()   # the foreign owner lets go: this run takes the claim next poll
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        return p.returncode, so, se

    def test_r11_a3_an_output_alias_added_while_the_batch_waits_on_a_foreign_claim_is_refused_after_the_wait(self) -> None:
        # sol r10 blocker 3, literal: ordinary single-link spec; while the runner waits (CLAIM_HELD) a hard link <RunDir>\leg-plan.json -> the spec appears; the foreign claim
        # is then released. The guard ran only before the wait, so leg-plan.json was written THROUGH the link, over the accepted spec, before the lease opened.
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = spec.read_text(encoding="utf-8")
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        code, so, se = self._alias_during_claim_wait(args, e, spec, self.run_dir / "leg-plan.json")
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    def test_r11_a3_a_scalar_spec_aliased_while_waiting_is_refused_after_the_wait(self) -> None:
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = spec.read_text(encoding="utf-8")
        self._foreign_live_claim()
        args, e = self.batch_cmd([spec])
        code, so, se = self._alias_during_claim_wait(args, e, spec, self.run_dir / "gate.log")
        self.assertEqual(code, 2, so + se)
        self.assertIn("BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    # r12 (adversary r12 A1): the staged build manifest is an input too. A hard link <RunDir>\<output> -> the manifest made the runner write the output through it, over the accepted build.
    def _staged_manifest(self, commit: str) -> Path:
        return self.share / "cache" / f"playback-attr-3-cuda-{commit[:12]}-build.json"

    def test_r12_a1_a_staged_build_manifest_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "gate.log", "batch.log", "batch-01.json", "batch-jobs/x.json", "quiet/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                manifest = self._staged_manifest(self.commit_a)
                (self.run_dir / leaf).parent.mkdir(parents=True, exist_ok=True)
                os.link(manifest, self.run_dir / leaf)
                before = manifest.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, manifest, before, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ")
                self.assertIn("is a hard link to", r.stderr)
                os.unlink(self.run_dir / leaf)

    def test_r12_a1_the_refusal_names_the_item_that_reads_the_build(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_b)
        os.link(manifest, self.run_dir / "leg-plan.json")
        before = manifest.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b)]))
        self._assert_nothing_submitted_input_intact(r, manifest, before, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=2 ")
        os.unlink(self.run_dir / "leg-plan.json")

    def test_r12_a1_a_staged_build_manifest_aliased_while_the_batch_waits_on_a_foreign_claim_is_refused_after_the_wait(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        before = manifest.read_text(encoding="utf-8")
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._alias_during_claim_wait(args, e, manifest, self.run_dir / "leg-plan.json")
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")
        self.assertEqual(manifest.read_text(encoding="utf-8"), before, "the accepted build manifest is byte-unchanged")

    # r12 pass 2 (adversary r12 A1'): a link BELOW a runner-owned directory writes through to its target whatever that target's link count, so the root-only reparse listing missed it.
    def test_r12_a1b_a_spec_behind_a_junction_nested_in_a_runner_owned_directory_is_refused_before_the_claim(self) -> None:
        for owned in ("quiet", "batch-jobs"):
            with self.subTest(owned=owned):
                self._fresh_run_dir()
                (self.run_dir / owned).mkdir()
                elsewhere = self.case / f"elsewhere-{owned}"
                shutil.rmtree(elsewhere, ignore_errors=True)
                self._junction(self.run_dir / owned / "sub", elsewhere)
                spec = elsewhere / "fx-a.json"
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                os.rmdir(self.run_dir / owned / "sub")

    def test_r12_a1b_a_spec_that_a_file_symlink_in_a_runner_owned_directory_points_at_is_refused_before_the_claim(self) -> None:
        self._fresh_run_dir()
        (self.run_dir / "quiet").mkdir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        try:
            os.symlink(spec, self.run_dir / "quiet" / "venue-quiet-probe-20261026T180000000Z.job.ps1")
        except OSError as exc:
            self.skipTest(f"file symlinks need a privilege this account lacks here: {exc}")
        before = spec.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")

    # r12 pass 2 (adversary r12 A2'): what the preflight validated must still hold when the claim is taken, or the leg throws / refuses after the lease opened.
    def _during_claim_wait(self, args: list[str], e: dict, change, release: bool = True) -> tuple[int, str, str]:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("CLAIM_HELD")
            change()
            if release:
                self.claim_path.unlink()   # the foreign owner lets go: this run takes the claim next poll
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        return p.returncode, so, se

    def _assert_refused_after_the_wait(self, code: int, so: str, se: str, marker: str) -> None:
        self.assertEqual(code, 2, so + se)
        self.assertIn(marker, se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")

    def test_r12_a2b_a_leg_spec_the_leg_would_throw_on_after_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        # playSeconds past Int32: Invoke-VenueLeg's [int] cast throws, which the preflight (Get-LegSpecRefusal) already refuses up front
        changed = json.loads(spec.read_text(encoding="utf-8"))
        changed["playSeconds"] = 2147483648
        code, so, se = self._during_claim_wait(args, e, lambda: spec.write_text(json.dumps(changed), encoding="utf-8"))
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=LEG_SPEC_INVALID item=1 ")

    def test_r12_a2b_a_staged_build_manifest_that_changed_during_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: manifest.write_bytes(manifest.read_bytes() + b" "))
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_MISMATCH item=1 ")

    def test_r12_a2b_a_staged_build_manifest_removed_during_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: manifest.unlink())
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=BUILD_NOT_STAGED item=1 ")

    def test_r12_a2b_an_unchanged_plan_still_runs_after_a_claim_wait(self) -> None:
        self._fresh_run_dir()
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: None)
        self.assertEqual(code, 0, so + se)
        self.assertNotIn("LEG_PLAN_REFUSED", se)

    # r13 (Dot A1''): a staged manifest whose bytes cannot be read after the wait (write-locked: the location and metadata still read) was "not a hit", so the lease opened on a build
    # nobody re-validated. Its digest cannot be checked, so it is refused before the lease, typed, like the preflight's own unreadable manifest.
    def _lock_exclusive(self, path: Path) -> None:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        # a writer that still lets others read: GENERIC_READ | GENERIC_WRITE, share READ, OPEN_EXISTING. Test-Path and the location still answer; ReadAllBytes (share READ, which a
        # handle with write access breaks) does not. (Share NONE hides the file from Test-Path too, which is the BUILD_NOT_STAGED refusal r12 already covers.)
        h = k32.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x80, None)
        self.assertNotIn(h, (None, ctypes.c_void_p(-1).value), f"could not lock {path}: {ctypes.get_last_error()}")
        self.addCleanup(k32.CloseHandle, h)

    def test_r13_a1_a_staged_build_manifest_write_locked_during_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: self._lock_exclusive(manifest))
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ")
        self.assertIn("could not be read after the claim wait", se)

    # r13 (Dot A2''): one file, two spellings. A loopback UNC name (\\localhost\C$\...) and the local name reach the same single-link file, and the guard compared spellings, so the
    # UNC spelling of a runner output was accepted as an input. It now compares file and directory identity (volume serial + file index), which no spelling changes.
    def _loopback_unc(self, p: Path) -> Path:
        drive, rest = os.path.splitdrive(str(p.resolve()))
        if len(drive) != 2 or drive[1] != ":":
            self.skipTest(f"{p} is not on a drive letter")
        unc = Path("\\\\localhost\\" + drive[0] + "$" + rest)
        if not unc.exists():
            self.skipTest(f"no loopback admin share reaches {p} as {unc}")
        return unc

    def _plan_with_share(self, share: Path) -> subprocess.CompletedProcess:
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        args[args.index("-AgentShare") + 1] = str(share)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def test_r13_a2_a_staged_build_manifest_reached_by_a_loopback_unc_agent_share_inside_a_runner_owned_directory_is_refused(self) -> None:
        # the share is <RunDir>\quiet (a directory the runner writes), named \\localhost\C$\...\quiet: the manifest's UNC path shares no prefix with the local -RunDir, one link, no reparse point
        self._fresh_run_dir()
        inside = self.run_dir / "quiet"
        shutil.copytree(self.share, inside)
        manifest = inside / "cache" / f"playback-attr-3-cuda-{self.commit_a[:12]}-build.json"
        before = manifest.read_text(encoding="utf-8")
        r = self._plan_with_share(self._loopback_unc(inside))
        self._assert_nothing_submitted_input_intact(r, manifest, before, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ")
        self.assertIn("inside quiet\\", r.stderr)

    def test_r13_a2_a_loopback_unc_agent_share_outside_the_run_still_runs(self) -> None:
        self._fresh_run_dir()
        r = self._plan_with_share(self._loopback_unc(self.share))
        self.assertNotIn("LEG_PLAN_REFUSED", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r13_a2_a_leg_spec_named_by_a_loopback_unc_alias_of_a_runner_output_is_refused_before_the_claim(self) -> None:
        # (already refused on b9b2f710: Resolve-LegSpecPath hands a UNC spec back provider-qualified, which the guard cannot resolve; this pins that the refusal holds)
        for case in ("gate.log", "quiet/x.json", "junction quiet"):
            with self.subTest(output=case):
                self._fresh_run_dir()
                self.run_dir.mkdir(parents=True, exist_ok=True)
                if case == "junction quiet":
                    elsewhere = self.case / "elsewhere-unc"
                    shutil.rmtree(elsewhere, ignore_errors=True)
                    self._junction(self.run_dir / "quiet", elsewhere)
                    local = elsewhere / "x.json"
                else:
                    local = self.run_dir / case
                    local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = local.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(self._loopback_unc(local), "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, local, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                if case == "junction quiet":
                    os.rmdir(self.run_dir / "quiet")

    # r13 (adversary r13 A1): the leg job also reads packageZip.sha256, exe.sha256 and dll.sha256 from the manifest and hashes the cached artifacts against them
    # (playback-attr-3-cuda-job.ps1 $manifestChecks); a manifest without one passed preflight and the job threw after the lease opened.
    def test_r13_adv_a1_a_staged_build_without_an_artifact_digest_is_refused_before_the_claim_and_the_lease(self) -> None:
        a, c = self.commit_a, "3" * 40
        cases = {
            "no packageZip": {"packageZip": ABSENT},
            "exe.sha256 blank": {"exe": {"sha256": ""}},
            "dll.sha256 a number": {"dll": {"sha256": 1}},
            "exe.sha256 not hex": {"exe": {"sha256": "z" * 64}},
            "dll is a string": {"dll": "c" * 64},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                manifest = self.stage(c, "bad build", **override)
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_MANIFEST_MISMATCH item=2 ")

    def test_r13_adv_a1_the_artifact_refusal_names_the_missing_digest(self) -> None:
        c = "3" * 40
        manifest = self.stage(c, "bad build", exe={"sha256": ""})
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=manifest)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_ARTIFACT_SHA_MISSING", r.stderr)
        self.assertIn("for exe ", r.stderr)

    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 (Dot 6440180395 on #684): the preflight checked that the manifest's artifact digests were well-formed, never that the cached artifacts
    # exist and hash to them; the leg job found out after the lease opened (playback-attr-3-cuda-job.ps1 $manifestChecks), so the lease was spent and nothing was measured.
    def test_artifact_preflight_a_staged_build_whose_cached_artifact_is_absent_is_refused_before_the_claim_and_the_lease(self) -> None:
        a, c = self.commit_a, "3" * 40
        for label in ("exe", "dll", "packageZip"):
            with self.subTest(absent=label):
                manifest = self.stage(c, f"no {label}")
                self.artifact_path(c, label).unlink()
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_ARTIFACT_MISSING item=2 ")

    def test_artifact_preflight_a_staged_build_whose_cached_artifact_differs_from_its_digest_is_refused_before_the_claim_and_the_lease(self) -> None:
        a, c = self.commit_a, "3" * 40
        for label in ("exe", "dll", "packageZip"):
            with self.subTest(differs=label):
                manifest = self.stage(c, f"stale {label}")
                self.artifact_path(c, label).write_bytes(b"a different build's bytes")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_ARTIFACT_MISMATCH item=2 ")

    # r8b adversary A1 (pass 1 on a2c32874): the verifier opened each cached artifact with FileShare.ReadWrite, but the leg job hashes it with FileShare.Read
    # (AttrCudaArtifacts.psm1 Get-Sha), which a retained writer handle refuses. A build with a writer open was STAGED here and threw after the lease opened. It is now read the way
    # the leg job reads it, so the writer makes the read fail and the plan is refused SHARE_UNREACHABLE (an unreadable file is not a missing or matching one) before any claim.
    def test_artifact_preflight_r8b_adv_a1_a_cached_artifact_a_writer_holds_open_is_refused_as_the_leg_job_would_fail_on_it(self) -> None:
        c = "3" * 40
        manifest = self.stage(c, "writer open")
        with open(self.artifact_path(c, "packageZip"), "r+b"):
            self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", c, manifest=manifest)]), "SHARE_UNREACHABLE item=1 ")

    # r8b adversary pass 2 on 29c63516, A1: the hash worker checked -ArtifactMinSec before the existence check only, so a Test-Path that used the time still opened and hashed the file
    # with less than the minimum left. The minimum is now checked again between the existence check and the open.
    def test_artifact_preflight_r8b_adv_a1_the_hash_worker_checks_the_minimum_again_after_the_existence_check(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start = text.index("# a read that fails ends the read (UNREACHABLE), never \"absent\"")
        # r12/r16 (VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1): the bounded read is handed $probeText (the worker text, optionally behind the test delay), no longer $probe.ToString() itself
        worker = text[start:text.index("$probeText = $(if ($TestArtifactDelayMs", start)]
        exists, opened = worker.index("Test-Path -LiteralPath $i.path"), worker.index("[IO.File]::Open($i.path")
        checks = [m.start() for m in re.finditer(r"-lt \$i\.minSec\) \{ 'NOTIME:'", worker)]
        self.assertEqual(len(checks), 3, "the minimum is checked before the existence check, again before the open, and again once the file is open (r10 adversary A1)")
        self.assertTrue(checks[0] < exists < checks[1] < opened < checks[2] < worker.index("ComputeHash"), (checks, exists, opened))

    # r9 B1 (sol r8b = opus = fable H2 = Dot): `[ValidateRange(1, 600)]` on a `[double]` has Int32 endpoints, so PowerShell validates a ROUNDED INTEGER copy of the value: 0.6 and 600.5
    # bound while the original value went on to the helper (a 0.6 s bound under the 1 s the contract names, a 600.5 s one over the 600 s cap). Double endpoints validate the value as given.
    # Sites: Get-VenueStagedBuildManifest -TimeoutSec / -ArtifactMinSec and Invoke-VenueBoundedRead -TimeoutSec. Fractional budgets in range (2.8) must still bind.
    def _binding_outcomes(self, calls: dict[str, str]) -> dict[str, str]:
        lines = ["Import-Module '" + str(MODULE) + "' -Force -DisableNameChecking", "$out = [ordered]@{}"]
        for key, call in calls.items():
            lines.append("try { $null = " + call + "; $out['" + key + "'] = 'BOUND' } catch { $out['" + key + "'] = if ($_.FullyQualifiedErrorId -like 'ParameterArgumentValidationError*') { 'REFUSED' } else { 'OTHER:' + $_.FullyQualifiedErrorId } }")
        lines += ["ConvertTo-Json -Compress -InputObject $out", "Complete-VenueProcess -ExitCode 0"]
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_artifact_preflight_r9_b1_a_fractional_timeout_outside_the_range_is_refused_at_binding_not_rounded_into_it(self) -> None:
        manifest = "Get-VenueStagedBuildManifest -AgentShare '" + str(self.share) + "' -SourceCommit ('a' * 40) -ProbeScript { param($p) '' } "
        bounded = "& (Get-Module VenueChain) { Invoke-VenueBoundedRead -ScriptText '1' -Argument 0 "
        got = self._binding_outcomes({
            "manifest_timeout_0.6": manifest + "-TimeoutSec 0.6",
            "manifest_timeout_600.5": manifest + "-TimeoutSec 600.5",
            "manifest_minsec_0.6": manifest + "-ArtifactMinSec 0.6",
            "manifest_minsec_600.5": manifest + "-ArtifactMinSec 600.5",
            "bounded_timeout_0.6": bounded + "-TimeoutSec 0.6 }",
            "bounded_timeout_600.5": bounded + "-TimeoutSec 600.5 }",
            # controls: the range's own ends and a fractional budget inside it still bind
            "manifest_timeout_1": manifest + "-TimeoutSec 1",
            "manifest_timeout_2.8": manifest + "-TimeoutSec 2.8",
            "manifest_timeout_600": manifest + "-TimeoutSec 600",
            "manifest_minsec_1": manifest + "-ArtifactMinSec 1",
            "manifest_minsec_2.8": manifest + "-ArtifactMinSec 2.8",
            "bounded_timeout_1": bounded + "-TimeoutSec 1 }",
            "bounded_timeout_2.8": bounded + "-TimeoutSec 2.8 }",
            "bounded_timeout_600": bounded + "-TimeoutSec 600 }",
        })
        refused = {k: v for k, v in got.items() if k.rsplit("_", 1)[1] in ("0.6", "600.5")}
        bound = {k: v for k, v in got.items() if k not in refused}
        self.assertEqual(set(refused.values()), {"REFUSED"}, got)
        self.assertEqual(set(bound.values()), {"BOUND"}, got)

    def test_artifact_preflight_r9_b1_no_double_parameter_of_the_dual_venue_scripts_has_integer_range_endpoints(self) -> None:
        # the class, not the three sites: any `[ValidateRange(<int>, <int>)]` on a floating-point parameter in the dual-venue scripts validates a rounded copy. r10 (fable key3 r9 P-02):
        # whatever order, spelling or layout the declaration takes ([Parameter()] between, [double] before the attribute, [Double] / [System.Double], a multi-line attribute)
        bad, seen = [], 0
        for path in sorted(DV.glob("*.ps*1")):
            for line, text, integer_end in ps_float_validaterange_params(path.read_text(encoding="utf-8")):
                seen += 1
                if integer_end:
                    bad.append(f"{path.name}:{line}: {text}")
        self.assertEqual(bad, [], "a floating-point parameter's ValidateRange endpoints must be doubles (1.0, 600.0)")
        self.assertGreaterEqual(seen, 3, "the scan found the r9 sites (Get-VenueStagedBuildManifest -TimeoutSec / -ArtifactMinSec, Invoke-VenueBoundedRead -TimeoutSec): it is not vacuous")

    def test_artifact_preflight_r10_the_validaterange_scanner_sees_every_declaration_spelling(self) -> None:
        bad = [
            "param([ValidateRange(1, 600)][double]$T)",
            "param([ValidateRange(1, 600)]  [Double]$T)",
            "param([ValidateRange(1, 600)][System.Double]$T)",
            "param([Parameter()][ValidateRange(1, 600)][double]$T)",
            "param([Parameter(Mandatory)]\n  [ValidateRange(1, 600)]\n  [double]\n  $T)",
            "param([double][ValidateRange(1, 600)]$T)",
            "param([Parameter()][double][ValidateRange(1, 600)]$T)",
            "param([ValidateRange(\n    1,\n    600.0)][double]$T)",
            "param([ValidateRange(0.5, 600)][single]$T)",
            "param([ValidateRange( 1 , 600 ) ][decimal]$T)",
        ]
        for text in bad:
            self.assertEqual([x[2] for x in ps_float_validaterange_params(text)], [True], text)
        good = [
            "param([ValidateRange(1.0, 600.0)][double]$T)",
            "param([double][ValidateRange(1.0, 600.0)]$T)",
            "param([Parameter()]\n[ValidateRange(\n  1.0,\n  600.0)]\n[double]$T)",
        ]
        for text in good:
            self.assertEqual([x[2] for x in ps_float_validaterange_params(text)], [False], text)
        for text in ("param([ValidateRange(1, 600)][int]$T)", "param([int][ValidateRange(1, 600)]$T)", "param([ValidateRange(1, 600)][string]$T)"):
            self.assertEqual(ps_float_validaterange_params(text), [], text)

    # r9 hardening (sol r7 + sol r8b + fable H3 + Dot P3): the per-file minimum re-check was pinned by source position only, so a label-gated mutation (the NOTIME check restricted to
    # packageZip) passed the pin. Behavioural: the production worker scriptblock (lifted from the module's AST) runs on three files with a fake clock (no real sleeps) and a Test-Path
    # shadow that spends fake time, window 4 s / minimum 2 s. Every file has the minimum checked before its existence check AND again after it, for EACH label; the wrong digest makes
    # a file that was opened and hashed visible as a MISMATCH line, so "no open" is an observable absence. The shadow also records which files had an existence read STARTED: the check
    # before it is what keeps a file from even being looked at with the window spent (a UNC Test-Path can itself block), and an absent file shows it as NOTIME, never MISSING.
    def _artifact_worker_run(self, case: str, end_sec: float = 4.0, start_now: float = 0.0, delay_label: str = "", consume_label: str = "", absent_label: str = "", open_label: str = "",
                             match_labels: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
        # match_labels: the files whose digest is the real one (they hash clean and print nothing); every other file has the wrong digest, so it prints a MISMATCH line
        import hashlib
        shas = {label: (hashlib.sha256(f"{label} bytes".encode("utf-8")).hexdigest() if label in match_labels else "0" * 64) for label in ("packageZip", "exe", "dll")}
        d = self.case / ("worker-files-" + case)
        d.mkdir(exist_ok=True)
        for label in ("packageZip", "exe", "dll"):
            if label != absent_label:
                (d / f"{label}.bin").write_bytes(f"{label} bytes".encode("utf-8"))
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile('" + str(MODULE) + "', [ref]$null, [ref]$null)\n"
            "$w = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -and $x.Extent.Text -match '^\\{\\s*param\\(\\$items\\)' }, $true).ScriptBlock.GetScriptBlock()\n"
            "$global:clk = [pscustomobject]@{ now = " + repr(start_now) + " }\n"
            "$clock = [pscustomobject]@{}\n"
            "Add-Member -InputObject $clock -MemberType ScriptProperty -Name Elapsed -Value { [pscustomobject]@{ TotalSeconds = $global:clk.now } }\n"
            "$global:delayPath = '" + (str(d / f"{delay_label}.bin") if delay_label else "") + "'\n"
            "$global:tp = [System.Collections.Generic.List[string]]::new()\n"
            "$global:shas = @{ " + "; ".join(f"{k} = '{v}'" for k, v in shas.items()) + " }\n"
            "function Test-Path { param([string]$LiteralPath, [string]$PathType) $global:tp.Add((Split-Path -Leaf $LiteralPath)); if ($global:delayPath -and $LiteralPath -ceq $global:delayPath) { $global:clk.now += 3 }; Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath -PathType $PathType }\n"
            "$items = foreach ($label in 'packageZip', 'exe', 'dll') {\n"
            "    $it = [pscustomobject]@{ label = $label; name = \"$label.bin\"; path = '" + str(d) + "\\' + $label + '.bin'; clock = $clock; endSec = " + repr(end_sec) + "; minSec = 2.0 }\n"
            # r10 adversary A1: the open spends the window -- the 2nd read of .path is [IO.File]::Open's own argument (the 1st is Test-Path's), so the fake clock moves between the
            # existence check and the open's return, exactly where a slow SMB open spends it
            "    if ($label -ceq '" + open_label + "') { $p0 = $it.path; $it.PSObject.Properties.Remove('path'); Add-Member -InputObject $it -MemberType NoteProperty -Name _reads -Value 0; Add-Member -InputObject $it -MemberType NoteProperty -Name _p -Value $p0; Add-Member -InputObject $it -MemberType ScriptProperty -Name path -Value { $this._reads++; if ($this._reads -eq 2) { $global:clk.now += 3 }; $this._p } }\n"
            "    if ($label -ceq '" + consume_label + "') { Add-Member -InputObject $it -MemberType ScriptProperty -Name sha -Value { $global:clk.now += 3; $global:shas[$this.label] } }\n"
            "    else { Add-Member -InputObject $it -MemberType NoteProperty -Name sha -Value $global:shas[$label] }\n"
            "    $it\n"
            "}\n"
            "$lines = @(& $w $items | ForEach-Object { [string]$_ })\n"
            "ConvertTo-Json -Compress -InputObject @{ lines = $lines; tp = @($global:tp) }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, case + "\n" + out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        return [":".join(line.split(":")[:2]) for line in got["lines"]], [t.removesuffix(".bin") for t in got["tp"]]

    def test_artifact_preflight_r9_the_hash_worker_checks_the_minimum_behaviourally_before_and_after_the_existence_check_for_every_file(self) -> None:
        order = ["packageZip", "exe", "dll"]
        # control: nothing spends time, so every file is looked at, opened and hashed (the wrong digest shows as MISMATCH) and none is NOTIME
        self.assertEqual(self._artifact_worker_run("control"), ([f"MISMATCH:{k}" for k in order], order))
        # the existence check of file L takes 3 s of the 4 s window: L is NOTIME and was not opened (no MISMATCH:L); the files before it were hashed
        for i, label in enumerate(order):
            with self.subTest(existence_check_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("exists-" + label, delay_label=label), ([f"MISMATCH:{k}" for k in order[:i]] + [f"NOTIME:{label}"], order[:i + 1]))
        # the hash of file L spends the window: the NEXT file starts with 1 s left, under the 2 s minimum, so it is NOTIME with NO existence read started on it, and a file that is
        # also absent is still NOTIME (not MISSING: nothing was looked at)
        for i, label in enumerate(order[:-1]):
            nxt = order[i + 1]
            with self.subTest(hash_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("hash-" + label, consume_label=label), ([f"MISMATCH:{k}" for k in order[:i + 1]] + [f"NOTIME:{nxt}"], order[:i + 1]))
                self.assertEqual(self._artifact_worker_run("hash-" + label + "-next-absent", consume_label=label, absent_label=nxt), ([f"MISMATCH:{k}" for k in order[:i + 1]] + [f"NOTIME:{nxt}"], order[:i + 1]))
        # the first file with the window already spent is NOTIME and never looked at
        self.assertEqual(self._artifact_worker_run("late", start_now=3.0), (["NOTIME:packageZip"], []))
        # the contract's own boundary stays: under the minimum left (0.9 s of a 2 s minimum) is NOTIME and nothing is read; exactly the minimum left is read
        self.assertEqual(self._artifact_worker_run("boundary-under", end_sec=0.9), (["NOTIME:packageZip"], []))
        self.assertEqual(self._artifact_worker_run("boundary-at", end_sec=2.0), ([f"MISMATCH:{k}" for k in order], order))

    # r10 adversary A1 (pass 2 on 3c341d79): the minimum was checked before the existence check and again before the open, but the OPEN can take the time too (an SMB open): a file
    # whose open spent the window went on to ComputeHash with under the minimum left, and as the last matching artifact it was STAGED. The worker checks again once the file is open.
    def test_artifact_preflight_r10_adv_a1_the_hash_worker_checks_the_minimum_again_after_the_open_for_every_file(self) -> None:
        order = ["packageZip", "exe", "dll"]
        for i, label in enumerate(order):
            with self.subTest(open_spends_the_window=label):
                # file L was looked at and opened (its Test-Path ran), then its open spent 3 of the 4 s: NOTIME:L, no hash of it (no MISMATCH:L), nothing after it touched
                self.assertEqual(self._artifact_worker_run("open-" + label, open_label=label), ([f"MISMATCH:{k}" for k in order[:i]] + [f"NOTIME:{label}"], order[:i + 1]))

    # LEASE-BATCH-PREFLIGHT-TEST-ISOLATION-1 item 2 (Dot on #705): every depletion row above gives its files the WRONG digest, so the first file is always a MISMATCH -- a refusal -- before the
    # window runs out. These rows give the files before the depleted one the REAL digest (they hash clean and print nothing) and spend the window afterwards: the next file is still
    # NOTIME, with no existence read started on it and no hash, and the clean files are not reported at all. Control: nothing spent, every file clean, nothing printed.
    def test_artifact_preflight_iso1_item2_a_window_spent_after_a_file_that_hashed_clean_still_stops_the_next_file_unread(self) -> None:
        order = ["packageZip", "exe", "dll"]
        self.assertEqual(self._artifact_worker_run("clean-control", match_labels=tuple(order)), ([], order), "the control: every file clean and nothing spent prints nothing")
        # the hash of a clean file spends the window: the NEXT file starts with 1 s left, under the 2 s minimum: NOTIME, no existence read on it, and the clean file(s) print nothing
        for i, label in enumerate(order[:-1]):
            nxt = order[i + 1]
            with self.subTest(hash_of_a_clean_file_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("clean-hash-" + label, consume_label=label, match_labels=tuple(order[:i + 1])), ([f"NOTIME:{nxt}"], order[:i + 1]))
                self.assertEqual(self._artifact_worker_run("clean-hash-" + label + "-next-absent", consume_label=label, match_labels=tuple(order[:i + 1]), absent_label=nxt), ([f"NOTIME:{nxt}"], order[:i + 1]))
        # the existence check of the file after a clean one spends the window: it is NOTIME and was not opened or hashed (its existence read did start)
        for i, label in enumerate(order[1:], start=1):
            with self.subTest(existence_check_after_a_clean_file_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("clean-exists-" + label, delay_label=label, match_labels=tuple(order[:i])), ([f"NOTIME:{label}"], order[:i + 1]))
        # the open of the file after a clean one spends the window: NOTIME and not hashed
        for i, label in enumerate(order[1:], start=1):
            with self.subTest(open_after_a_clean_file_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("clean-open-" + label, open_label=label, match_labels=tuple(order[:i])), ([f"NOTIME:{label}"], order[:i + 1]))

    def test_artifact_preflight_the_refusal_names_the_artifact_and_a_fully_staged_build_still_opens_the_lease(self) -> None:
        c = "3" * 40
        manifest = self.stage(c, "named")
        self.artifact_path(c, "dll").unlink()
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=manifest)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("igpu_recon_cuda-playback-attr-3-cuda-333333333333.dll", r.stderr)
        # the control: every artifact staged and matching
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.events.unlink(missing_ok=True)
        ok = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b, "cuda")]))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, self.kinds())

    def test_artifact_preflight_an_artifact_that_changes_while_the_batch_waits_on_a_foreign_claim_is_refused_after_the_wait(self) -> None:
        self._fresh_run_dir()
        exe = self.artifact_path(self.commit_a, "exe")
        original = exe.read_bytes()
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        try:
            code, so, se = self._during_claim_wait(args, e, lambda: exe.write_bytes(b"replaced while the batch waited"))
        finally:
            exe.write_bytes(original)
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=BUILD_ARTIFACT_MISMATCH item=1 ", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")

    # adversary r1 A1: the supplementary route calls with -TimeoutSec 30, and the artifact hash had its own 300 s default, so a stalled share held the primary chain for five minutes.
    def test_artifact_preflight_adv_a1_the_artifact_hash_stays_inside_the_calls_own_timeout(self) -> None:
        c = "3" * 40
        self.stage(c, "slow share")
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{c}' -TimeoutSec 3 -VerifyArtifacts -ArtifactProbeScript {{ param($i) [Threading.Thread]::Sleep(25000); 'late' }}\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; sec = [int]$sw.Elapsed.TotalSeconds; error = [string]$r.error }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertLessEqual(got["sec"], 6, f"the call (timeout 3 s) took {got['sec']} s: {got}")
        self.assertIn("could not be hashed", got["error"])

    # adversary r2 A2: with no explicit bound the hash gets what is left of -TimeoutSec, and a 1 s floor ran a 1 s call to 2 s (850 ms manifest read + 900 ms hash, then STAGED). Now less than a whole second
    # left is UNREACHABLE at once, so no call outlasts its own -TimeoutSec by more than the manifest read itself.
    def test_artifact_preflight_adv_r2_a2_a_call_with_under_a_second_left_for_the_hash_ends_unreachable_not_over_its_timeout(self) -> None:
        c = "3" * 40
        self.stage(c, "tight")
        manifest_probe = ("param($p) [Threading.Thread]::Sleep(850); ('a' * 64); 'C:" + c + "'; 'D:' + ('f' * 64); 'S:bool'; "
                          "'A:packageZip=ok'; 'H:packageZip=' + ('1' * 64); 'A:exe=ok'; 'H:exe=' + ('2' * 64); 'A:dll=ok'; 'H:dll=' + ('3' * 64)")
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{c}' -TimeoutSec 1 -VerifyArtifacts "
                  f"-ProbeScript {{ {manifest_probe} }} -ArtifactProbeScript {{ param($i) [Threading.Thread]::Sleep(900) }}\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; ms = [int]$sw.Elapsed.TotalMilliseconds; error = [string]$r.error }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertLess(got["ms"], 1500, f"the call (timeout 1 s) ran {got['ms']} ms: {got}")
        self.assertIn("no time is left", got["error"])

    # adversary r1 A2: the post-claim guard runs between the grant and the claim keeper's start. A hash that outlasts ClaimTtlSec - ClaimRenewSec made every guard "lapse" the claim, release it and
    # ask again until the deadline. The runner has no seam that slows a real hash, so the budget's derivation is pinned in the source: set from the grant time, handed to the artifact read, cleared after.
    def test_artifact_preflight_adv_a2_the_post_claim_guard_hashes_inside_the_claim_guard_budget(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        # r3: the budget is New-GuardBudget's (a Stopwatch started at the grant read), so the claim guard and the post-open guard count on one monotonic clock each
        # r7: less 1 s for the guard's own end, now that its reads get the exact remainder
        grant = text.index("$script:guardBudget = $(if ($req.granted) { New-GuardBudget $claimGuardSec ")
        self.assertIn("$claimGuardSec = [double]($ClaimTtlSec - $ClaimRenewSec - 1);", text)
        # r8 (LEASE-BATCH-ADV-A2-GRANT-STAMP-PIN-1, sol r7): a bounded window again, not "anywhere before". The stamp is the one assignment of $grantReadUtc in the file, and the
        # first statement after the request's try/catch: after its REQUEST_THREW stand-in and before the first read of the grant, at most 200 characters on, in the same loop as the guard.
        self.assertEqual(text.count("$grantReadUtc = "), 1)
        request_end = text.rindex("state = 'REQUEST_THREW' }", 0, grant) + len("state = 'REQUEST_THREW' }")
        stamp = text.index("$grantReadUtc = [DateTime]::UtcNow", request_end)
        first_read = text.index("if (-not $req.granted) { if ($script:claimWait.denied -eq 0)", request_end)
        self.assertLess(stamp, first_read)
        self.assertLessEqual(first_read - request_end, 200, "the grant-stamp window grew: the stamp must stay right after the claim request")
        self.assertLess(first_read, grant)
        # r4 (sol r3 B1): the whole guard runs under that budget (Invoke-InputGuard), not only its manifest and artifact reads
        call, clear = text.index("try { $hit = Invoke-InputGuard }", grant), text.index("$script:guardBudget = $null", grant)
        self.assertLess(grant, call)
        self.assertLess(call, clear)
        self.assertLess(clear, text.index("if ($hit) {", grant))
        self.assertIn("clock = [Diagnostics.Stopwatch]::StartNew()", text[text.index("function New-GuardBudget"):text.index("function Get-InputContentRefusal")])
        guard = text[text.index("function Get-InputContentRefusal"):text.index("function Stop-InputIsOutput")]
        # adversary r2 A1: the manifest read AND the hash come out of ONE share of the budget, and a share too small for both is a typed refusal. r4 (hub ruling 17:18:14Z): ONE deadline
        # through both phases, so no fixed split: the timed r4 cases below hold the behaviour (Dot's literal case, key3 row L-04, the post-open 500 s room)
        self.assertIn("($script:guardBudget.sec - $script:guardBudget.clock.Elapsed.TotalSeconds)", guard)
        # r5 (key3 r4 B1): -VerifyArtifacts is off only under a budget that says so (the claim wait, no claim held)
        # r7 (sol r6 B1): the guard's minimum and the module's -ArtifactMinSec are ONE value, and the share is the unfloored remainder
        self.assertIn("-TimeoutSec $share -VerifyArtifacts:$verifyArtifacts -ArtifactMinSec $script:GuardReadMinSec -ProbeScript", guard)
        self.assertIn("$verifyArtifacts = -not ($null -ne $script:guardBudget -and $script:guardBudget.artifactsOff)", guard)
        self.assertNotIn("$manifestSec", guard)
        self.assertNotIn("-ArtifactTimeoutSec", guard)
        self.assertIn("if ($share -lt $script:GuardReadMinSec) { return", guard)
        self.assertNotIn("Floor", guard)
        self.assertIn("$script:GuardReadMinSec = 2\n", text)
        self.assertNotIn("-TimeoutSec 30", guard)
        # and the pre-claim preflight, which holds no claim, asks for its own explicit bound
        self.assertIn("-VerifyArtifacts -ArtifactTimeoutSec 300 }", text)

    # r2 (Dot fd08838 cos-feedback/mlv-app/pr-705.md): the test above pins the source; this one TIMES the guard through the runner's offline probe seams. With -ClaimTtlSec 60 and
    # -ClaimRenewSec 30 (their floors) the guard has 30 s from the grant. Inside the guard (the claim file exists; the preflight before it is fast) the manifest read takes 18 s and the
    # artifact hash 18 s more. On 22d5ad64 the hash had a whole share of the budget, taken BEFORE the manifest read: it answered at about 36 s, the claim lapsed in the guard and was
    # asked for again until DEADLINE (exit 3). The manifest read and the hash must share ONE bound: the hash runs out first and the plan is refused SHARE_UNREACHABLE, inside 30 s.
    def test_artifact_preflight_r2_a_slow_manifest_read_and_a_slow_hash_end_typed_inside_the_claim_guard_budget(self) -> None:
        c = self.commit_a
        doc = json.loads((self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json").read_text(encoding="utf-8"))
        started = self.case / "guard-read-started.txt"
        in_guard = f"if (Test-Path -LiteralPath '{self.claim_path}' -PathType Leaf) {{ "
        facts = [self.manifests[c], "C:" + c, "D:" + "f" * 64, "S:bool"] + [x for k in ("packageZip", "exe", "dll") for x in (f"A:{k}=ok", f"H:{k}={doc[k]['sha256']}")]
        manifest_probe, artifact_probe = self.case / "manifest-probe.ps1", self.case / "artifact-probe.ps1"
        manifest_probe.write_text("param($p)\n" + in_guard + f"[IO.File]::AppendAllText('{started}', [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffff', [Globalization.CultureInfo]::InvariantCulture) + [Environment]::NewLine); [Threading.Thread]::Sleep(18000) }}\n"
                                  + "".join(f"'{f}'\n" for f in facts), encoding="utf-8")
        artifact_probe.write_text("param($items)\n" + in_guard + "[Threading.Thread]::Sleep(18000) }\n", encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "30",
                          "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=75)),
                          "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        ended = datetime.now(timezone.utc)
        log = self.run_dir / "batch.log"
        text = r.stdout + r.stderr + (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
        self.assertNotIn("CLAIM_LAPSED_IN_GUARD", text, "the guard outlasted the claim's TTL less one renew interval and the claim was asked for again")
        self.assertEqual(r.returncode, 2, text)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("could not be hashed", r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        starts = started.read_text(encoding="utf-8").split()
        self.assertEqual(len(starts), 1, f"one guard read, no release-and-retry: {starts}")
        guard_sec = (ended - datetime.fromisoformat(starts[0]).replace(tzinfo=timezone.utc)).total_seconds()
        self.assertLess(guard_sec, 30, f"the guard (and the refusal after it) took {guard_sec:.1f} s of a 30 s claim-guard budget")

    def test_artifact_preflight_r2_the_probe_seams_are_refused_with_the_real_um_run(self) -> None:
        probe = self.case / "probe.ps1"
        probe.write_text("param($p) ''\n", encoding="utf-8")
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-ArtifactProbeScript", str(probe))
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE -StagedBuildProbeScript and -ArtifactProbeScript are offline test seams", r.stderr)
        self.assertEqual(self.kinds(), [])

    # r3 (Dot 780783c4; hub ACK 6449516541): once the open returns, the guard re-reads and re-hashes every build on the LEASE's time. On f0e12522 it had a fixed 300 s for that while
    # the lease end is clamped (-LeaseCapSec), so a healthy but slow re-hash left less than the leg budget: LEG_BUDGET_EXCEEDS_LEASE, a lease charged with no leg. The guard is now
    # bounded by the lease's room less -LeaseMarginSec, the leg budget and 1 s, and a re-hash that cannot fit is refused typed while the leg budget is still left. The card's numbers
    # (cap 120, leg 60, margin 0, a 70 s hash) are scaled to cap 95 and a 40 s hash so the case stays near 30 s (-LegBudgetSec cannot go under 60). The hash is slow only once the
    # open was answered (the fake agent's state file exists), so the preflight and the claim guard stay fast.
    def _post_open_hash_run(self, hash_sec: float, *extra: str, cap: str = "95", leg: str = "60", margin: str = "0", items: list | None = None, post_open: str | None = None,
                            **env: str) -> tuple[subprocess.CompletedProcess, float]:
        # post_open: PowerShell run in the artifact probe once the open was answered, in place of the hash_sec sleep
        state = self.share / "fake-agent-state.json"
        self.assertFalse(state.exists())
        probe = self.case / "artifact-probe-post-open.ps1"
        body = post_open if post_open is not None else f"[Threading.Thread]::Sleep({int(hash_sec * 1000)})"
        probe.write_text("param($items)\n" + f"if (Test-Path -LiteralPath '{state}' -PathType Leaf) {{ {body} }}\n", encoding="utf-8")
        r = self.run_plan(self.write_plan(items or [self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", cap, "-LegBudgetSec", leg, "-LeaseMarginSec", margin,
                          "-ArtifactProbeScript", str(probe), *extra, **env)
        exited = datetime.now(timezone.utc)   # subprocess.run returned: the process is gone and its pipes closed (Dot I1 on #750)
        ticks = {}
        for line in self.events_raw():
            t, kind, rest = line.split(" ", 2)
            if kind == "UMRUN":
                ticks.setdefault("open" if rest.startswith("lease-batch-open-") else "close", int(t))
        self.assertIn("open", ticks, r.stdout + r.stderr)
        # LEASE-BATCH-PREFLIGHT-TEST-ISOLATION-1 item 3: the second number returned is a CLOSE-submitted measurement ONLY when close_substituted is False. When no close was submitted
        # (NO_CLOSE_EXPECTED) the observed process exit stands in for it, and the number is then how long the RUN took after the open: it proves termination (and, with the claim file
        # check beside it, claim cleanup), never a successful close and never a physical release of the lease.
        # Dot I1 on #750: this was the runner's finishedUtc, which is stamped when the summary is built, before Write-RunSummary, Complete-VenueProcess and exit, so a stall after it passed.
        def ticks_of(t: datetime) -> int:
            return (t - datetime(1, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1) * 10
        self.close_substituted = False
        if "close" not in ticks and env.get("NO_CLOSE_EXPECTED"):
            # r6: a close redirect that failed closed submits no close job (JOB_WRITE_FAILED); the run's end then stands in for the close
            self.close_substituted = True
            self.summary_after_open = (ticks_of(datetime.fromisoformat(batch_result(r.stdout)["finishedUtc"].replace("Z", "+00:00"))) - ticks["open"]) / 1e7
            ticks["close"] = ticks_of(exited)
        self.assertIn("close", ticks, r.stdout + r.stderr)
        return r, (ticks["close"] - ticks["open"]) / 1e7

    # r8: the slow-rehash cases run in the cap 75 / leg 60 room (14 s) the guard-read cases already use, with the hash 6 s over the room as the 40 s hash was over the 34 s room of
    # cap 95: the refusal must land inside the room (held < 75 - 60 + 4), never after the hash has spent the leg budget (LEG_BUDGET_EXCEEDS_LEASE)
    def test_artifact_preflight_r3_a_slow_post_open_rehash_is_refused_typed_before_the_leg_budget_is_spent(self) -> None:
        r, held = self._post_open_hash_run(20, cap="75")
        log = self.run_dir / "batch.log"
        text = r.stdout + r.stderr + (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
        self.assertEqual(r.returncode, 2, text)
        self.assertNotIn("LEG_BUDGET_EXCEEDS_LEASE", text, "the re-hash spent the lease's leg budget and the lease was charged with no leg")
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("could not be hashed", r.stderr)
        self.assertIn("the lease's room after the open", r.stderr)
        # #705 r14 (adversary r14 A1 sibling): the hash given up on is a read still held on the manifest's share, so the manifest's location is left unknown and nothing more is
        # written into -RunDir (the receipt included); on 80df9163 the receipt was written through unbounded re-reads of every input's path
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertFalse((self.run_dir / "batches.jsonl").exists() and self.batches(), "no receipt was written into -RunDir after a share read was given up on")
        self.assertEqual(self.leg_calls(), [])
        # refused while a leg budget was still left in the 75 s lease, not after the 20 s hash
        self.assertLess(held, 75 - 60 + 4, f"the lease was held {held:.1f} s before the close: the re-hash ran into the leg budget")

    def test_artifact_preflight_r3_a_fast_post_open_rehash_still_runs_the_leg(self) -> None:
        r, held = self._post_open_hash_run(0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))
        self.assertEqual(len(self.leg_calls()), 1)

    def test_artifact_preflight_r3_a_lease_with_no_room_for_the_rehash_and_one_leg_is_refused_at_once(self) -> None:
        # a lease shorter than the leg budget cannot start the re-read: LEG_BUDGET_EXCEEDS_LEASE at once, typed by the guard, before any probe and without the 40 s re-hash
        r, held = self._post_open_hash_run(40, FAKE_LEASE_REMAIN="30")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE item=1 ", r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"]), ("LEG_BUDGET_EXCEEDS_LEASE", 0))
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        self.assertLess(held, 20, f"the lease was held {held:.1f} s: the 40 s re-hash ran on a lease that could hold no leg")

    # r3 adversary A1 (pass 1 on 5c3407ff): the guard skipped the content re-check when the lease's room (a float) was under the leg budget, but the leg loop rounds the room to whole
    # seconds, so a room of 59.9 s with a 60 s budget skipped the check AND let a probe and a leg run on a manifest changed in the open wait. The content is never skipped now.
    def test_artifact_preflight_r3_adv_a1_a_lease_room_just_under_the_leg_budget_runs_no_probe_on_an_unchecked_build(self) -> None:
        manifest = self.share / "cache" / f"playback-attr-3-cuda-{self.commit_a[:12]}-build.json"
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "95", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                FAKE_OPEN_SLEEP="6", FAKE_LEASE_REMAIN="60")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
            with open(manifest, "a", encoding="utf-8") as f:
                f.write("\n")
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(p.returncode, 2, so + se)
        self.assertRegex(se, r"LEG_PLAN_REFUSED reason=(LEG_BUDGET_EXCEEDS_LEASE|BUILD_MANIFEST_MISMATCH) item=1 ")
        self.assertIn("found after the open", se)
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        self.assertEqual(self.leg_calls(), [])

    # r3 adversary A2 (pass 1 on 5c3407ff): an open held in the agent's gate ran the guard at once with no budget (300 s), so a slow re-hash ran while the agent had already admitted
    # the open and published its result, and the lease was held through it before the lease-aware guard below ever started. That call now checks locations only.
    def test_artifact_preflight_r3_adv_a2_a_gate_held_open_is_not_rehashed_before_its_result_is_read(self) -> None:
        r, held = self._post_open_hash_run(20, cap="75", FAKE_OPEN_MODES="gatewait")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 75 - 60 + 4, f"the lease was held {held:.1f} s before the close: a re-hash ran outside the lease's room")

    # r4 ITEM 1 (hub ruling 2026-10-27T17:18:14Z: one absolute deadline through both phases; key3 and fable r3 B1). On 02612714 each build's share was split into a fixed manifest
    # part (min(30, share - 1)) and the rest for the hash, so read time the manifest did not use never reached the hash and a check that fits was refused SHARE_UNREACHABLE.
    def _timed_probes(self, manifest_ms: int, hash_ms: int, when: str = "$true", no_digests_when: str = "$false") -> tuple[Path, Path]:
        """Manifest and artifact probe seams for both staged builds: the manifest read sleeps manifest_ms and the hash hash_ms while `when` holds (a PowerShell condition). While
        `no_digests_when` holds the manifest read emits A:<k>=ok without the paired H:<k>= digest lines (key3 r3 H3's throw)."""
        manifest_probe, artifact_probe = self.case / "timed-manifest-probe.ps1", self.case / "timed-artifact-probe.ps1"
        body = ["param($p)", f"if ({when}) {{ [Threading.Thread]::Sleep({manifest_ms}) }}", f"$noDigests = {no_digests_when}"]
        for c in (self.commit_a, self.commit_b):
            doc = json.loads((self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json").read_text(encoding="utf-8"))
            facts = [self.manifests[c], "C:" + c, "D:" + "f" * 64, "S:bool"] + [f"A:{k}=ok" for k in ("packageZip", "exe", "dll")]
            digests = [f"H:{k}={doc[k]['sha256']}" for k in ("packageZip", "exe", "dll")]
            body.append(f"if ($p -like '*{c[:12]}*') {{ " + "; ".join(f"'{f}'" for f in facts) + "; if (-not $noDigests) { " + "; ".join(f"'{f}'" for f in digests) + " } }")
        manifest_probe.write_text("\n".join(body) + "\n", encoding="utf-8")
        artifact_probe.write_text("param($items)\n" + f"if ({when}) {{ [Threading.Thread]::Sleep({hash_ms}) }}\n", encoding="utf-8")
        return manifest_probe, artifact_probe

    def test_artifact_preflight_r4_item1_dots_literal_case_two_builds_fast_manifest_and_a_2s_hash_runs_the_legs(self) -> None:
        # Dot's literal case: -ClaimTtlSec 60, -ClaimRenewSec 15, two builds, a 0.1 s manifest read and a 2 s hash. The claim guard has 45 s, 22 s per build. On 02612714: manifest
        # bound 21 s, hash bound 1 s, so the 2 s hash was refused SHARE_UNREACHABLE with ~41 s of the budget unused.
        manifest_probe, artifact_probe = self._timed_probes(100, 2000)
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "15",
                          "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        self.assertNotIn("SHARE_UNREACHABLE", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 2, 2))

    def test_artifact_preflight_r4_item1_key3_row_l04_a_10s_post_open_hash_in_a_34s_room_runs_the_leg(self) -> None:
        # key3 row L-04: cap 95 / leg 60 / margin 0, a fast manifest read, a 10 s re-hash once the lease is open. The guard's room is 34 s. On 02612714: manifest bound 30 s, hash
        # bound 3 s, so SHARE_UNREACHABLE, exit 2, legsAttempted 0, the lease charged with no leg.
        r, held = self._post_open_hash_run(10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))
        self.assertEqual(len(self.leg_calls()), 1)

    def test_artifact_preflight_r4_item1_a_healthy_3s_rehash_in_a_500s_lease_runs_the_leg(self) -> None:
        # fable r3 B1's post-open sibling: a lease with 500 s of room, -LeaseMarginSec 120, -LegBudgetSec 360: the guard has 19 s. On 02612714 the manifest got 18 s and the hash 1 s,
        # so a healthy 3 s re-hash of the cached artifacts closed a lease charged with no leg. The leg runs, and the lease is not charged without one.
        r, held = self._post_open_hash_run(3, cap="2700", leg="360", margin="120", FAKE_LEASE_REMAIN="500")
        self.assertNotIn("SHARE_UNREACHABLE", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))
        self.assertEqual(len(self.leg_calls()), 1)

    def test_artifact_preflight_r4_item1_control_a_hash_over_the_remainder_is_still_refused_inside_the_room(self) -> None:
        # the control: the one deadline still binds. A 0.1 s manifest read and a 40 s re-hash in the 34 s room end SHARE_UNREACHABLE while the leg budget is left (the r3 40 s case
        # above holds the same with a fast manifest read).
        r, held = self._post_open_hash_run(20, cap="75")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("could not be hashed", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 75 - 60 + 4, f"the lease was held {held:.1f} s: the re-hash ran into the leg budget")

    # r4 ITEM 2 (sol r3 B1; Dot 6454756990): the guard's own file system reads -- a build manifest's existence (Test-Path), each input's physical path and identity
    # (Get-RunDirOutputRefusal), each leg spec's bytes (Get-LegSpecRefusal) -- ran OUTSIDE the bounded reader, so one that blocked spent the lease's leg reserve and the refusal came
    # late (sol: 45.01 s against a 34 s budget). Every guard read now runs under the guard's one deadline. -GuardReadDelay delays exactly one kind of read in one guard. The room is
    # cap 75 / leg 60 / margin 0: the post-open guard has 14 s, and the delay is 25 s.
    def _delayed_guard_read_run(self, read: str) -> tuple[subprocess.CompletedProcess, float]:
        return self._post_open_hash_run(0, "-GuardReadDelay", f"open:{read}=25", cap="75")

    def _assert_delayed_read_refused_inside_the_room(self, r: subprocess.CompletedProcess, held: float, extra: float = 0) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("the guard's reads did not finish within the lease's room after the open", r.stderr)
        self.assertNotIn("LEG_BUDGET_EXCEEDS_LEASE", r.stdout + r.stderr, "the delayed read spent the leg budget before the refusal")
        self.assertEqual(self.leg_calls(), [])
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        # the refusal came inside the 14 s room (plus the close), never after the 25 s read: the leg reserve was never spent
        # r5 (adversary r5 A1): a location left unknown adds the documented bounded re-read of -RunDir (at most 2 s) before the close
        self.assertLess(held, 75 - 60 + 4 + extra, f"the lease was held {held:.1f} s before the close: the delayed read ran past the guard's room")
        self.assertFalse(self.claim_path.exists(), "the claim was released (full cleanup)")

    def test_artifact_preflight_r4_item2_a_delayed_existence_read_after_the_open_ends_share_unreachable_inside_the_room(self) -> None:
        r, held = self._delayed_guard_read_run("existence")
        self._assert_delayed_read_refused_inside_the_room(r, held, extra=2)
        # where an input is was never verified: nothing more is written into -RunDir (fail closed, as an unresolvable location is)
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)

    def test_artifact_preflight_r4_item2_a_delayed_identity_and_path_read_after_the_open_ends_share_unreachable_inside_the_room(self) -> None:
        r, held = self._delayed_guard_read_run("identity")
        self._assert_delayed_read_refused_inside_the_room(r, held, extra=2)
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)

    def test_artifact_preflight_r4_item2_a_delayed_spec_read_after_the_open_ends_share_unreachable_inside_the_room(self) -> None:
        r, held = self._delayed_guard_read_run("spec")
        self._assert_delayed_read_refused_inside_the_room(r, held, extra=2)
        # #705 r14 (adversary r14 A1): every location was verified BEFORE the spec read, but the worker given up on is still in a read of the spec's share, so the spec's location
        # cannot be read again on the main thread inside the room (each later L and receipt write would, through Get-WriteDestinationHit): it is left unknown and nothing more is
        # written into -RunDir. On 80df9163 the suppression stayed off and the receipt was written through those unbounded reads.
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertFalse((self.run_dir / "batches.jsonl").exists() and self.batches(), "no receipt was written into -RunDir after the guard gave up on a share read")

    def test_artifact_preflight_r4_item2_fast_control_the_same_room_with_no_delay_runs_the_leg(self) -> None:
        r, held = self._post_open_hash_run(0, cap="75")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))

    def test_artifact_preflight_r4_item2_no_room_control_a_lease_shorter_than_the_leg_is_refused_at_once(self) -> None:
        # no room: the location checks still run (alias detection kept), then LEG_BUDGET_EXCEEDS_LEASE before any content read, as on r3
        r, held = self._post_open_hash_run(40, cap="75", FAKE_LEASE_REMAIN="30")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE item=1 ", r.stderr)
        self.assertNotIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertLess(held, 20)
        self.assertFalse(self.claim_path.exists())

    def test_artifact_preflight_r4_adv_a1_no_room_refuses_before_a_slow_spec_read(self) -> None:
        # r4 adversary A1 (pass 1 on 31d1fdaf): cap 62 / leg 60 / margin 0 leaves the post-open guard about 1 s. Invoke-InputGuard's 10 s floor for that case (meant for the location
        # checks) let the content check read a spec that took 5 s before it noticed no time was left, so the refusal came after the leg reserve was spent. Now no content read starts.
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:spec=5", cap="62")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE ", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 4.5, f"the lease was held {held:.1f} s: the 5 s spec read ran on a lease with no room for it")

    def test_artifact_preflight_r4_adv_p2_a1_no_room_bounds_a_slow_location_read_to_2s_not_10s(self) -> None:
        # r4 adversary pass 2 A1 (on 336d318d): with about 1 s of room the location checks were given 10 s, so a 5 s identity read ran to its end before the refusal. They get 2 s
        # now; one still running then fails closed (no more writes into -RunDir), typed with the no-room reason.
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:identity=5", cap="62")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE", r.stderr)
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 4.5, f"the lease was held {held:.1f} s: the 5 s identity read ran on a lease with no room for it")

    def test_artifact_preflight_r4_adv_p2_a2_no_spec_read_starts_once_an_earlier_read_used_the_room(self) -> None:
        # r4 adversary pass 2 A2 (on 336d318d): cap 66 / leg 60 / margin 0 leaves about 5 s; two leg specs whose reads take 3 s each. The no-room check ran once, before the first
        # read, so the second started with under 2 s left. It is checked before each read now: the first read runs, the second is refused before it starts.
        items = [self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "A", self.commit_a)]
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:spec=3", cap="66", items=items)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE item=2 ", r.stderr)
        self.assertNotIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 5.5, f"the lease was held {held:.1f} s: a second 3 s spec read started with under 2 s of room")

    def test_artifact_preflight_r4_item2_a_delayed_read_in_the_claim_guard_ends_typed_inside_the_claim_budget(self) -> None:
        # the claim guard's sibling: -ClaimTtlSec 60 / -ClaimRenewSec 30 gives 30 s; a 40 s identity read ends SHARE_UNREACHABLE inside it, the claim released, no lease opened,
        # never CLAIM_LAPSED_IN_GUARD and a release-and-retry
        t0 = time.monotonic()
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "30", "-GuardReadDelay", "claim:identity=40")
        took = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("the guard's reads did not finish within the claim guard", r.stderr)
        self.assertNotIn("CLAIM_LAPSED_IN_GUARD", r.stdout + r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        self.assertLess(took, 40, f"the run took {took:.1f} s: the 40 s read was waited for")
        # LEASE-BATCH-CLAIM-WAIT-GUARD-READ-REFUSAL-1: the refusal says what each read took and that no claim wait came before it
        self.assertRegex(r.stderr, self.GUARD_READS_RE.format("location:plan"))
        self.assertIn("claimWaitBefore=no", r.stderr)

    # LEASE-BATCH-CLAIM-WAIT-GUARD-READ-REFUSAL-1: #748's matrix and retry plans each stopped on one no-claim read that did not answer in its 30 s, while another chain held the venue, and
    # every remaining leg went NOT_RUN. No lease, leg or write hangs on that read: the attempt that gets the claim guards again on its own budget before anything runs. So a read with
    # no answer while no claim is held is read again next poll, writing nothing meanwhile, and only three in a row are the typed refusal, which then still says SHARE_UNREACHABLE.
    # what each read took; '+' marks the one still running when the guard gave up on it
    GUARD_READS_RE = r"guardReads\(sec\): start=[0-9.]+ (\S+=[0-9.]+ )*{}=[0-9.]+\+"

    def _claim_wait_cmd(self, *extra: str, manifest_probe: Path | None = None, **env: str) -> tuple[list[str], dict]:
        self._fresh_run_dir()
        self._foreign_live_claim()
        probes = ("-StagedBuildProbeScript", str(manifest_probe)) if manifest_probe else ()
        return self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), *probes, *extra, **env)

    def _assert_read_again_and_the_plan_ran(self, code: int, so: str, se: str, line: str = "CLAIM_WAIT_GUARD_NO_ANSWER", no_answers: int = 1) -> None:
        self.assertEqual(code, 0, so + se)
        self.assertNotIn("LEG_PLAN_REFUSED", se)
        self.assertIn(f"{line} batch=1 unfinished=1/3 noAnswers={no_answers} ", se)
        self.assertEqual(se.count(line), 1, se)
        self.assertIn("claimWaitBefore=yes deniedPolls=1 ", se)
        self.assertEqual(len(self.leg_calls()), 1, so + se)
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertLess(log.index(f"CLAIM_WAIT_GUARD_ANSWERED batch=1 unfinished=1 noAnswers={no_answers} "), log.index("CLAIM_HELD"), "a line was written before a read vouched for the input's location")
        self.assertEqual(self.batches()[0]["endReason"], "ALL_LEGS_DONE")

    def _run_acting_on_stderr(self, args: list[str], e: dict, marker: str, act, timeout_s: float = 240) -> tuple[int, str, str, float]:
        # the run's stderr is read as it comes, and act() runs once on the first line carrying marker: a no-answer poll writes nothing into -RunDir, so stderr is all a case can wait on
        t0 = time.monotonic()
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        lines: list[str] = []
        out: list[str] = []
        acted = threading.Event()

        def drain() -> None:
            for line in p.stderr:
                lines.append(line)
                if marker in line and not acted.is_set():
                    acted.set()
                    act()
        readers = [threading.Thread(target=drain, daemon=True), threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True)]
        for t in readers:
            t.start()
        try:
            p.wait(timeout=timeout_s)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        for t in readers:
            t.join(timeout=30)
        return p.returncode, "".join(out), "".join(lines), time.monotonic() - t0

    def _claim_loop_snippet(self, setup: str) -> dict:
        # the runner's own no-answer branch and helpers, run in memory: only the sleep is replaced, so a wakeup that comes late is a fact of the case and not of the host
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "foreach ($n in 'Test-ClaimWaitRetryRoom', 'Get-ClaimWaitUnfinishedText') { $f = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true); if ($f) { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
                  "$branch = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.IfStatementAst] -and $x.Extent.Text.StartsWith('if ($hit -and $hit.noAnswer -and -not $req.granted)') }, $true)\n"
                  "$script:ClaimWaitUnfinishedMax = 3; $script:ClaimWaitNoAnswerMax = 3; $script:ClaimWaitGuardSec = 1; $ClaimPollSec = 1; $batchNo = 1; $unfinished = 0; $noAnswers = 0; $Venue = 'bachelor'\n"
                  "$req = [pscustomobject]@{ granted = $false; broken = @() }\n"
                  "$hit = [pscustomobject]@{ item = 1; why = 'WHY'; noAnswer = $true; readAbandoned = $true }\n"
                  "$script:slept = 0\n"
                  "function Start-Sleep { param([int]$Seconds) $script:slept++; [Threading.Thread]::Sleep(($Seconds + $overshootSec) * 1000) }\n"
                  f"{setup}\n"
                  "$outcome = 'READ_AGAIN'\n"
                  "foreach ($i in 1) { . ([scriptblock]::Create($branch.Extent.Text)); $outcome = 'REFUSAL' }\n"
                  "[pscustomobject]@{ outcome = $outcome; slept = $script:slept; why = $hit.why } | ConvertTo-Json -Compress\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_artifact_preflight_claim_wait_one_location_read_with_no_answer_costs_no_leg(self) -> None:
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:existence=40x1")
        code, so, se = self._during_claim_wait(args, e, lambda: None)
        self._assert_read_again_and_the_plan_ran(code, so, se)
        self.assertRegex(se, self.GUARD_READS_RE.format("location:build1"))
        self.assertIn("still reading the location of the build input", se)

    def test_artifact_preflight_claim_wait_one_manifest_read_given_up_on_costs_no_leg(self) -> None:
        calls = self.case / "manifest-calls.txt"
        manifest_probe, _ = self._timed_probes(0, 0)
        # the preflight is the first read; the first denied poll's is the second, and it does not answer inside the guard's 30 s
        manifest_probe.write_text(manifest_probe.read_text(encoding="utf-8").replace(
            "param($p)\n", f"param($p)\n[IO.File]::AppendAllText('{calls}', 'm' + [Environment]::NewLine)\nif ([IO.File]::ReadAllLines('{calls}').Count -eq 2) {{ [Threading.Thread]::Sleep(40000) }}\n", 1), encoding="utf-8")
        args, e = self._claim_wait_cmd(manifest_probe=manifest_probe)
        code, so, se = self._during_claim_wait(args, e, lambda: None)
        self._assert_read_again_and_the_plan_ran(code, so, se)
        self.assertIn("could not be read after the claim wait", se)
        self.assertRegex(se, self.GUARD_READS_RE.format("content:build1"), "a read really given up on keeps its '+'")

    def test_artifact_preflight_claim_wait_one_slow_read_that_left_no_room_costs_no_leg(self) -> None:
        # the read answered, but the guard's 30 s went on it: the next read has no room to start (or, on a host so slow that the worker took over 2.5 s to reach it, the guard gave up on
        # this one). Neither is a refusal yet. Sol and fable r2: the pass with no read given up on carries no '+' and says no read went unanswered; the other keeps both.
        # r4 (the keys' control, INV-1): the manifest goes and comes back before the next pass, which no pass saw, so this is still a read again: the row below is the one that saw it gone.
        manifest = self._staged_manifest(self.commit_a)
        staged = manifest.read_bytes()

        def gone_and_back_then_the_foreign_owner_lets_go() -> None:
            manifest.unlink()
            manifest.write_bytes(staged)
            self._wait_for_log("CLAIM_HELD")
            self.claim_path.unlink()
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:spec=28x1", "-ClaimPollSec", "3")
        code, so, se, _ = self._run_acting_on_stderr(args, e, "CLAIM_WAIT_GUARD_NO_", gone_and_back_then_the_foreign_owner_lets_go)
        if "CLAIM_WAIT_GUARD_NO_ANSWER" in se:
            self._assert_read_again_and_the_plan_ran(code, so, se)
            self.assertRegex(se, self.GUARD_READS_RE.format("content:spec1"))
            return
        self._assert_read_again_and_the_plan_ran(code, so, se, line="CLAIM_WAIT_GUARD_NO_ROOM", no_answers=0)
        [line] = [l for l in se.splitlines() if "CLAIM_WAIT_GUARD_NO_ROOM" in l]
        self.assertRegex(line, r"guardReads\(sec\): start=[0-9.]+ (\S+=[0-9.]+ )*content:spec1=[0-9.]+ claimWaitBefore=yes")
        self.assertNotRegex(line, r"guardReads\(sec\): [^\]]*\+", "every read that started had answered")
        self.assertNotIn("no answer", line.lower())
        self.assertIn("no time is left in the claim wait", line)

    def test_artifact_preflight_claim_wait_a_share_that_never_answers_still_refuses_typed_and_writes_nothing(self) -> None:
        # the control: every read stalls past the guard's 30 s, the foreign claim never lets go. The same typed reason, after three reads in a row and never at the deadline.
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:existence=40")
        t0 = time.monotonic()
        r = subprocess.run(args, capture_output=True, text=True, timeout=200, env=e)
        took = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 the guard's reads did not finish within the claim wait", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertIn("(no answer on 3 reads in a row while no claim was held)", r.stderr)
        self.assertRegex(r.stderr, self.GUARD_READS_RE.format("location:build1"))
        self.assertIn("claimWaitBefore=yes deniedPolls=3 ", r.stderr)
        self.assertEqual(r.stderr.count("CLAIM_WAIT_GUARD_NO_ANSWER"), 2, r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertEqual(self.leg_calls(), [])
        self.assertGreater(took, 3 * 30, f"the run took {took:.1f} s: each read keeps its whole 30 s")
        self.assertLess(took, 3 * 30.5 + 2 * 1 + 30, f"the run took {took:.1f} s: three reads and two polls, then the refusal")
        self.assertNotIn("CLAIM_HELD", (self.run_dir / "batch.log").read_text(encoding="utf-8"), "a line was written while no read had vouched for the input's location")

    def test_artifact_preflight_claim_wait_a_read_with_no_answer_at_the_deadline_refuses_typed_not_deadline(self) -> None:
        # no poll is left to read again in: the run ends in the refusal, because DEADLINE's own log line and receipt would write into a -RunDir nobody vouched for
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:existence=40", "-ClaimPollSec", "20", "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=45)))
        r = subprocess.run(args, capture_output=True, text=True, timeout=200, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("(no answer on 1 read in a row while no claim was held)", r.stderr)
        self.assertNotIn("CLAIM_WAIT_GUARD_NO_ANSWER", r.stderr)
        self.assertNotIn("BATCH_END", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    def test_artifact_preflight_claim_wait_a_read_the_share_refused_at_once_is_still_the_refusal(self) -> None:
        # the sibling control: a read that ANSWERED with a failure (the manifest write-locked) is an answer, so it is refused on that poll, as #705 r13 made it
        manifest = self._staged_manifest(self.commit_a)
        args, e = self._claim_wait_cmd()
        code, so, se = self._during_claim_wait(args, e, lambda: self._lock_exclusive(manifest), release=False)
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", se)
        self.assertIn("could not be read after the claim wait", se)
        self.assertNotIn("CLAIM_WAIT_GUARD_NO_ANSWER", se)
        self.assertEqual(self.leg_calls(), [])

    def test_artifact_preflight_claim_wait_r3_no_retry_when_a_whole_guard_does_not_fit_before_the_deadline(self) -> None:
        # sol r2 B-01: 40 s left and a poll of 1 s. The first read gives up at about 31 s, and a second would be granted a fresh 30 s past the deadline (62 s on 38d61585). It is refused
        # typed, at the first read, and nothing else is read.
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:existence=40", "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=40)))
        t0 = time.monotonic()
        r = subprocess.run(args, capture_output=True, text=True, timeout=200, env=e)
        took = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("(no answer on 1 read in a row while no claim was held)", r.stderr)
        self.assertNotIn("CLAIM_WAIT_GUARD_NO_ANSWER", r.stderr)
        self.assertNotIn("BATCH_END", (self.run_dir / "batch.log").read_text(encoding="utf-8"))
        self.assertLess(took, 40, f"the run took {took:.1f} s: a second guard ran past the 40 s deadline")

    def test_artifact_preflight_claim_wait_r3_the_room_is_asked_again_after_the_sleep(self) -> None:
        # sol r2 B-01's second repro: the room was there before the sleep (1 s poll + 1 s guard + 0.5 s against 5 s), and a wakeup 4 s late spent it. No request starts after that.
        late = self._claim_loop_snippet("$overshootSec = 4; $deadline = [DateTime]::UtcNow.AddSeconds(5)")
        self.assertEqual((late["outcome"], late["slept"]), ("REFUSAL", 1), late)
        self.assertIn("(no answer on 1 read in a row while no claim was held)", late["why"])
        # the control: the same sleep on time, with room left
        on_time = self._claim_loop_snippet("$overshootSec = 0; $deadline = [DateTime]::UtcNow.AddSeconds(20)")
        self.assertEqual((on_time["outcome"], on_time["slept"]), ("READ_AGAIN", 1), on_time)
        # and no room at all: no sleep, no read again
        none_left = self._claim_loop_snippet("$overshootSec = 0; $deadline = [DateTime]::UtcNow.AddSeconds(2)")
        self.assertEqual((none_left["outcome"], none_left["slept"]), ("REFUSAL", 0), none_left)

    def test_artifact_preflight_claim_wait_r3_an_answered_poll_starts_no_request_after_the_deadline(self) -> None:
        # the class sweep of B-01: the poll check before a sleep is only a plan. The runner's own claim loop, in memory: every pass answers, the foreign claim stays, and a wakeup 4 s late
        # lands past the deadline. On 38d61585 that started another request; here the loop ends (DEADLINE, as ever), and with an on-time sleep it still polls.
        def requests(setup: str) -> int:
            script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                      "$loop = $ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.WhileStatementAst] -and $x.Body.Extent.Text.Contains('Request-VenueClaim') }, $true) | Select-Object -Last 1\n"
                      "foreach ($n in 'Test-ClaimWaitRetryRoom', 'Get-ClaimWaitUnfinishedText') { $f = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true); if ($f) { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
                      "$script:requests = 0; $script:ClaimWaitUnfinishedMax = 3; $script:ClaimWaitNoAnswerMax = 3; $script:ClaimWaitGuardSec = 30; $script:receiptGuardHit = $null; $script:claimWait = @{ denied = 0; startUtc = $null; endUtc = $null }\n"
                      "$claim = $null; $lastHolder = ''; $unfinished = 0; $noAnswers = 0; $batchNo = 1; $Venue = 'bachelor'; $RunDir = 'r'; $Purpose = 'p'; $ClaimDir = 'c'; $ClaimTtlSec = 60; $ClaimRenewSec = 15; $ClaimPollSec = 1\n"
                      "function Request-VenueClaim { $script:requests++; [pscustomobject]@{ granted = $false; broken = @(); claim = $null; state = 'held' } }\n"
                      "function New-GuardBudget { param($a, $b, $c, $d, $e, $f) [pscustomobject]@{} }\n"
                      "function Invoke-InputGuard { $null }\n"
                      "function Test-AliasHit { param($h) $false }\n"
                      "function LogBest { param($m) }\n"
                      "function Start-Sleep { param([int]$Seconds) [Threading.Thread]::Sleep(($Seconds + $overshootSec) * 1000) }\n"
                      f"{setup}\n"
                      ". ([scriptblock]::Create($loop.Extent.Text))\n"
                      "\"requests=$script:requests\"\n")
            r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return int(re.search(r"requests=([0-9]+)", r.stdout).group(1))
        self.assertEqual(requests("$overshootSec = 4; $deadline = [DateTime]::UtcNow.AddSeconds(3)"), 1)
        self.assertGreaterEqual(requests("$overshootSec = 0; $deadline = [DateTime]::UtcNow.AddSeconds(4)"), 2)

    def test_artifact_preflight_claim_wait_r3_a_slow_claim_request_spends_the_retrys_room(self) -> None:
        # adversary r3 A1: the room is there after the sleep, and the claim request itself then spends it (BUSY retries on a locked claim file). The runner's own claim loop, in memory: the
        # second request takes 2.5 s of the ~5 s left, so no second guard (3 s here) fits, and the first pass's hit is the refusal, its strikes counted once.
        # r6: the retry's request now runs on its bounded worker (~4 s here, the room less 1 s), which this 2.5 s request stays inside, so the room asked again after it is still what stops it.
        d = self._retry_request_loop("[Threading.Thread]::Sleep(2500); " + self.DENIED, 6, extra="$script:ClaimWaitGuardSec = 3")
        self.assertEqual((d["requests"], d["workerRequests"], d["guards"], d["thrown"], d["abandoned"]), (2, 1, 1, "", 0), d)
        self.assertEqual(d["refused"], "WHY (no answer on 1 read in a row while no claim was held)", d)

    def test_artifact_preflight_claim_wait_r3_an_answered_failure_before_a_stalled_read_still_refuses_on_that_poll(self) -> None:
        # adversary r3 A2: the second pass finds the spec gone (an ANSWERED failure, kept in the worker) and then stalls on the build's existence read. That is the refusal on that poll, not a
        # read to try again, however healthy the next one would look; the first pass, which only stalled, is still read again.
        self._fresh_run_dir()
        self._foreign_live_claim()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), "-GuardReadDelay", "claim:existence=40x2")
        code, so, se, _ = self._run_acting_on_stderr(args, e, "CLAIM_WAIT_GUARD_NO_ANSWER", spec.unlink)
        self.assertEqual(code, 2, so + se)
        self.assertEqual(se.count("CLAIM_WAIT_GUARD_NO_ANSWER"), 1, se)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", se)
        self.assertIn("already answered with a failure: INPUT_VANISHED", se)
        self.assertRegex(se, self.GUARD_READS_RE.format("location:build1"))
        self.assertEqual(self.kinds(), [], "no lease was opened")

    def test_artifact_preflight_claim_wait_r3_a_manifest_found_gone_before_a_stalled_read_still_refuses_on_that_poll(self) -> None:
        # adversary r3 pass 2 A1: the sibling of the answered failure above. The location step confirms the manifest is gone, and leaves its typing to the content step, which a stalled spec
        # read then never reaches. That is an answer too: the poll refuses, whatever the next read would find.
        manifest = self._staged_manifest(self.commit_a)
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:spec=40x2")
        code, so, se, _ = self._run_acting_on_stderr(args, e, "CLAIM_WAIT_GUARD_NO_ANSWER", manifest.unlink)
        self.assertEqual(code, 2, so + se)
        self.assertEqual(se.count("CLAIM_WAIT_GUARD_NO_ANSWER"), 1, se)
        self.assertIn("already answered with a failure: BUILD_NOT_STAGED", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")

    def test_artifact_preflight_claim_wait_r4_a_manifest_found_gone_then_no_room_is_refused_on_that_poll(self) -> None:
        # r4 (sol, fable, opus r3 blocker; sol ran it at 28.01, 29 and 29.99 s): the row above with a pass that COMPLETES. The spec read answers at 28 s of the 30, so the next read has no
        # room, and the worker returns its no-room hit instead of being given up on. The manifest was still found gone first: refused on that poll, never read again.
        # (On a host so slow that the guard gives up on the spec read instead, the row above is what runs, and this says the same.)
        manifest = self._staged_manifest(self.commit_a)
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:spec=28x2")
        code, so, se, _ = self._run_acting_on_stderr(args, e, "CLAIM_WAIT_GUARD_NO_", manifest.unlink)
        self.assertEqual(code, 2, so + se)
        self.assertEqual(se.count("CLAIM_WAIT_GUARD_NO_"), 1, se)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", se)
        self.assertIn("already answered with a failure: BUILD_NOT_STAGED", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertEqual(self.leg_calls(), [])

    def _guard_passes_in_memory(self, passes: str, stall_commit: str) -> list[dict]:
        # one no-claim pass per row, in one process, through the runner's own Invoke-InputGuard, worker runspace, location and content steps, and VenueChain's bounded read. Only the
        # alias and spec checks are stubbed, and a pass's room is cut at the named read's progress line (no read has to be slow, so no host is too slow or too fast for it).
        flag = self.case / "stall-on"
        manifest_probe, _ = self._timed_probes(8000, 0, when=f"$p -like '*{stall_commit[:12]}*' -and [IO.File]::Exists('{psq(flag)}')")
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "function Get-Def([string]$n) { $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text }\n"
                  "foreach ($n in 'New-GuardBudget', 'Invoke-InputGuard', 'Get-GuardTimingText', 'Get-InputOutputRefusal', 'Get-InputContentRefusal', 'Wait-GuardReadSeam', 'Get-OutputZones', 'Get-PhysicalPath', 'Get-FileIdentity', 'Remove-TrailingSeparator', 'Test-NothingAtName') { . ([scriptblock]::Create((Get-Def $n))) }\n"
                  "foreach ($v in '$script:GuardReadMinSec', '$script:GuardWorkerFunctions', '$script:GuardWorker') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] -and $x.Left.Extent.Text -ceq $v }, $true).Extent.Text)) }\n"
                  "$t = Get-Def 'Set-GuardProgress'\n"
                  "$cut = 'if ($Phase -ceq ''content'') { $b = $script:guardBudget; if ($b.cutKind -ceq $Input_.kind -and $b.cutItem -eq $Input_.item) { $b.sec = $b.clock.Elapsed.TotalSeconds + $b.room } }'\n"
                  ". ([scriptblock]::Create($t.Substring(0, $t.LastIndexOf('}')) + \"`n$cut`n}\"))\n"
                  "function Get-RunDirOutputRefusal { param($p) '' }\n"
                  "function Get-InputUnresolvedCode { param($p, [switch]$SkipRead) '' }\n"
                  "function Get-LegSpecRefusal { param($p, $b) @('', '') }\n"
                  f"$here = '{psq(RUNNER.parent)}'; Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking\n"
                  f"$legPlanMode = $true; $AgentShare = '{psq(self.share)}'; $RunDir = 'r'; $Venue = 'bachelor'; $ClaimDir = 'c'; $artifactProbe = $null; $script:guardDelaySeam = $null; $script:claimWait = $null\n"
                  f"$stagedBuildProbe = [scriptblock]::Create([IO.File]::ReadAllText('{psq(manifest_probe)}'))\n"
                  "function Build-Input([int]$item, [string]$commit, [string]$sha) { [pscustomobject]@{ kind = 'build'; item = $item; commit = $commit; sha256 = $sha; path = ($AgentShare.TrimEnd('\\') + '\\cache\\playback-attr-3-cuda-' + $commit.Substring(0, 12) + '-build.json') } }\n"
                  f"$a = Build-Input 2 '{self.commit_a}' '{self.manifests[self.commit_a]}'; $b1 = Build-Input 1 '{self.commit_b}' '{self.manifests[self.commit_b]}'\n"
                  "$spec = [pscustomobject]@{ kind = 'spec'; item = 1; path = 's'; backend = 'cpu' }\n"
                  f"$rows = @(\n{passes}\n)\n"
                  "$out = foreach ($row in $rows) {\n"
                  "    $gone = $row.gone\n"
                  f"    if ($row.stall) {{ [IO.File]::WriteAllText('{psq(flag)}', '') }} else {{ [IO.File]::Delete('{psq(flag)}') }}\n"
                  "    if ($gone) { [IO.File]::Move($a.path, \"$($a.path).away\") }\n"
                  "    $script:guardedInputs = @($row.inputs)\n"
                  "    $script:guardBudget = New-GuardBudget 60 'the test room' 'SHARE_UNREACHABLE' $false 'claim' $true\n"
                  "    $script:guardBudget | Add-Member cutKind $row.kind; $script:guardBudget | Add-Member cutItem $row.item; $script:guardBudget | Add-Member room $row.room\n"
                  "    $h = Invoke-InputGuard\n"
                  "    $script:guardBudget = $null\n"
                  "    if ($gone) { [IO.File]::Move(\"$($a.path).away\", $a.path) }\n"
                  "    [pscustomobject]@{ site = $row.site; gone = $gone; code = $h.code; item = $h.item; noAnswer = [bool]$h.noAnswer; readAbandoned = [bool]$h.readAbandoned; locationUnknown = [bool]$h.locationUnknown; why = $h.why }\n"
                  "}\n"
                  # a read given up on is a live thread the host would wait for
                  "[Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @($out))); [Console]::Out.Flush(); [Environment]::Exit(0)\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_artifact_preflight_claim_wait_r4_a_manifest_found_gone_is_refused_at_each_exit_that_completes_the_pass(self) -> None:
        # r4 catalogue row (sol, fable, opus r3): manifest found gone, then the pass ends on a no-room hit or a bounded manifest read given up on: the failure is named and noAnswer is off, so
        # the claim loop refuses on that poll. Each exit has a row, its control next to it: the same exit with the manifest back answers nothing, stays noAnswer and is read again (INV-1).
        # The no-room exit before a read also has the real-process row above. The build read's own room check has no read before it to delay, so only a cut room reaches it, and the A/B read
        # given up on (sol's two-build form) is here because a real run of it costs two whole 30 s passes.
        rows = {"no_room_next_read": ("@($spec, $a)", "'spec'", 1, 1, 0), "no_room_build_read": ("@($b1, $a)", "'build'", 2, 1, 0), "manifest_read_given_up": ("@($b1, $a)", "'build'", 1, 2.5, 1)}
        passes = ",\n".join(f"[pscustomobject]@{{ site = '{site}'; gone = ${'true' if gone else 'false'}; inputs = {inputs}; kind = {kind}; item = {item}; room = {room}; stall = {'$true' if stall else '$false'} }}"
                            for site, (inputs, kind, item, room, stall) in rows.items() for gone in (True, False))
        got = {(d["site"], d["gone"]): d for d in self._guard_passes_in_memory(passes, self.commit_b)}
        self.assertEqual(len(got), 6, got)
        says = {"no_room_next_read": "to re-read the build input", "no_room_build_read": "to read and hash the staged build for", "manifest_read_given_up": "could not be read after the claim wait"}
        for site, text in says.items():
            given_up = site == "manifest_read_given_up"
            with self.subTest(site=site, gone=True):
                d = got[(site, True)]
                self.assertEqual(d["code"], "SHARE_UNREACHABLE", d)
                self.assertIn(text, d["why"])
                self.assertFalse(d["noAnswer"], f"read again, not refused: {d}")
                self.assertRegex(d["why"], r"already answered with a failure: BUILD_NOT_STAGED \S+ \[guardReads\(sec\): ", "named, before the timing, as the worker-given-up branch says it")
                self.assertEqual((d["readAbandoned"], d["locationUnknown"]), (given_up, given_up), d)
                self.assertEqual("+ claimWaitBefore" in d["why"], given_up, "'+' is only for a read really given up on")
            with self.subTest(site=site, gone=False):
                d = got[(site, False)]
                self.assertEqual(d["code"], "SHARE_UNREACHABLE", d)
                self.assertIn(text, d["why"])
                self.assertTrue(d["noAnswer"], f"a pass that really did not finish is read again: {d}")
                self.assertNotIn("already answered", d["why"])
                self.assertEqual(d["readAbandoned"], given_up, d)

    def test_artifact_preflight_claim_wait_r5_a_manifest_gone_then_back_in_the_same_pass_is_refused_on_that_poll(self) -> None:
        # r5 catalogue row (sol r4 B1; hub ruling: sticky, fail closed): the location step finds the manifest gone, and it is back, STAGED with the original digest, before the content
        # step reads it. On 30ecd3a4 the content read answered null and the plan was accepted; the pass saw it gone, so the poll refuses naming BUILD_NOT_STAGED. In memory, through the
        # runner's own Get-InputOutputRefusal (direct) and Invoke-InputGuard (its worker), with VenueChain's real manifest read; only the alias and spec checks are stubbed, and the manifest
        # is put back at the content step's progress line. Controls: never back (NOT_STAGED), back with other bytes (BUILD_MANIFEST_MISMATCH), never gone (accepted).
        changed = self.case / "changed-on"
        manifest = self._staged_manifest(self.commit_a)
        hook = self.case / "r5-back-hook.ps1"
        hook.write_text("if ($Phase -ceq 'content' -and [IO.File]::Exists($Input_.path + '.away') -and [IO.File]::Exists($Input_.path + '.back')) { [IO.File]::Move($Input_.path + '.away', $Input_.path); "
                        f"if ([IO.File]::Exists('{psq(changed)}')) {{ [IO.File]::AppendAllText($Input_.path, ' ') }} }}\n", encoding="utf-8")
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "function Get-Def([string]$n) { $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text }\n"
                  "foreach ($n in 'New-GuardBudget', 'Invoke-InputGuard', 'Get-GuardTimingText', 'Get-InputOutputRefusal', 'Get-InputContentRefusal', 'Wait-GuardReadSeam', 'Get-OutputZones', 'Get-PhysicalPath', 'Get-FileIdentity', 'Remove-TrailingSeparator', 'Test-NothingAtName') { . ([scriptblock]::Create((Get-Def $n))) }\n"
                  "foreach ($v in '$script:GuardReadMinSec', '$script:GuardWorkerFunctions', '$script:GuardWorker') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] -and $x.Left.Extent.Text -ceq $v }, $true).Extent.Text)) }\n"
                  # the manifest is put back at the content step's progress line, after the location step found it gone and before the content read
                  "$t = Get-Def 'Set-GuardProgress'\n"
                  f". ([scriptblock]::Create($t.Substring(0, $t.LastIndexOf('}}')) + \"`n\" + [IO.File]::ReadAllText('{psq(hook)}') + \"`n}}\"))\n"
                  "function Get-RunDirOutputRefusal { param($p) '' }\n"
                  "function Get-InputUnresolvedCode { param($p, [switch]$SkipRead) '' }\n"
                  "function Get-LegSpecRefusal { param($p, $b) @('', '') }\n"
                  f"$here = '{psq(RUNNER.parent)}'; Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking\n"
                  f"$legPlanMode = $true; $AgentShare = '{psq(self.share)}'; $RunDir = 'r'; $Venue = 'bachelor'; $ClaimDir = 'c'; $artifactProbe = $null; $stagedBuildProbe = $null; $script:guardDelaySeam = $null; $script:claimWait = $null\n"
                  f"$a = [pscustomobject]@{{ kind = 'build'; item = 1; commit = '{self.commit_a}'; sha256 = '{self.manifests[self.commit_a]}'; path = '{psq(manifest)}' }}\n"
                  "$orig = [IO.File]::ReadAllBytes($a.path)\n"
                  "$out = foreach ($mode in 'direct', 'wrapper') { foreach ($case in 'back_same', 'back_changed', 'gone_stays', 'never_gone') {\n"
                  f"    if ($case -ceq 'back_changed') {{ [IO.File]::WriteAllText('{psq(changed)}', '') }} else {{ [IO.File]::Delete('{psq(changed)}') }}\n"
                  "    if ($case -like 'back_*') { [IO.File]::WriteAllText($a.path + '.back', '') } else { [IO.File]::Delete($a.path + '.back') }\n"
                  "    if ($case -cne 'never_gone') { [IO.File]::Move($a.path, $a.path + '.away') }\n"
                  "    $script:guardedInputs = @($a)\n"
                  "    $script:guardBudget = New-GuardBudget 60 'the test room' 'SHARE_UNREACHABLE' $false 'claim' $true\n"
                  "    if ($mode -ceq 'direct') { $script:guardProgress = [hashtable]::Synchronized(@{ locationsDone = $false; item = 0; at = 'x'; steps = [Collections.ArrayList]::Synchronized([Collections.ArrayList]::new()) }); $h = Get-InputOutputRefusal; $script:guardProgress = $null }\n"
                  "    else { $h = Invoke-InputGuard }\n"
                  "    $script:guardBudget = $null\n"
                  "    if ([IO.File]::Exists($a.path + '.away')) { [IO.File]::Delete($a.path); [IO.File]::Move($a.path + '.away', $a.path) }\n"
                  "    [IO.File]::WriteAllBytes($a.path, $orig); [IO.File]::Delete($a.path + '.back')\n"
                  "    [pscustomobject]@{ mode = $mode; case = $case; code = $h.code; item = $h.item; noAnswer = [bool]$h.noAnswer; why = $h.why }\n"
                  "} }\n"
                  "[Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @($out)))\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = {(d["mode"], d["case"]): d for d in json.loads(r.stdout.strip().splitlines()[-1])}
        self.assertEqual(len(got), 8, got)
        for mode in ("direct", "wrapper"):
            with self.subTest(mode=mode, case="back_same"):
                d = got[(mode, "back_same")]
                self.assertEqual((d["code"], d["item"], d["noAnswer"]), ("BUILD_NOT_STAGED", 1, False), f"a manifest the pass saw gone is refused on that poll: {d}")
                self.assertIn(f"{manifest} was gone when this guard pass looked for it", d["why"])
            with self.subTest(mode=mode, case="controls"):
                self.assertEqual(got[(mode, "back_changed")]["code"], "BUILD_MANIFEST_MISMATCH", got[(mode, "back_changed")])
                self.assertEqual(got[(mode, "gone_stays")]["code"], "BUILD_NOT_STAGED", got[(mode, "gone_stays")])
                self.assertIn("no staged build manifest for", got[(mode, "gone_stays")]["why"], "the content step's own NOT_STAGED wording, unchanged")
                self.assertIsNone(got[(mode, "never_gone")]["code"], got[(mode, "never_gone")])

    def test_artifact_preflight_claim_wait_r5_a_no_room_refusal_reads_no_input_before_it_prints(self) -> None:
        # r5 catalogue row (sol r4 B2): a terminal no-room hit, and one that names an answered failure, refused through Stop-InputIsOutput with every input's path and identity read
        # delayed 1500 ms. On 30ecd3a4 the refusal went through the write guard first (Test-RunDirWrite re-read the inputs with no bound) and printed 1.5 s late or more; here it reads
        # nothing, writes nothing into -RunDir, and says the line is on stderr only, with batch.log there and not. The hits are the runner's own (Invoke-InputGuard, its room cut to 1 s at
        # the first content read's progress line, so the next read has none: the spec exit and the build exit); only the final exit is a sentinel. The no-room hit keeps noAnswer, so a
        # pass that truly did not finish is still read again (INV-1).
        spec = self.case / "r5-spec.json"
        spec.write_text("{}", encoding="utf-8")
        manifest_probe, _ = self._timed_probes(4000, 0)
        spec2 = self.case / "r5-spec2.json"
        spec2.write_text("{}", encoding="utf-8")
        gone = self.case / "r5-gone-build.json"
        run_dir = self.case / "r5-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "function Get-Def([string]$n) { $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text }\n"
                  "foreach ($n in 'New-GuardBudget', 'Invoke-InputGuard', 'Get-GuardTimingText', 'Get-InputOutputRefusal', 'Wait-GuardReadSeam', 'Get-OutputZones', 'Get-PhysicalPath', 'Get-FileIdentity', 'Remove-TrailingSeparator',\n"
                  "    'Test-NothingAtName', 'Get-HitRefusal', 'Get-WriteDestinationHit', 'Test-RunDirWrite', 'Register-WriteRefusal', 'Stop-InputIsOutput') { . ([scriptblock]::Create((Get-Def $n))) }\n"
                  # adversary r5 pass 2 A1: a build read's share is capped at 330 s, under a larger budget; the cap is 6 s here (the extracted function's literal), so a share runs out in seconds
                  "$c = Get-Def 'Get-InputContentRefusal'; if (-not $c.Contains('[Math]::Min(330.0,')) { throw 'the 330 s share cap moved' }; . ([scriptblock]::Create($c.Replace('[Math]::Min(330.0,', '[Math]::Min(6.0,')))\n"
                  "$t = Get-Def 'Set-GuardProgress'\n"
                  ". ([scriptblock]::Create($t.Substring(0, $t.LastIndexOf('}')) + \"`nif (`$Phase -ceq 'content') { `$b = `$script:guardBudget; `$b.sec = `$b.clock.Elapsed.TotalSeconds + `$b.room }`n}\"))\n"
                  ". ([scriptblock]::Create(((Get-Def 'Stop-PlanRefused') -replace '(?m)^\\s*exit 2\\s*$', \"throw 'EXIT2'\")))\n"
                  "foreach ($v in '$script:GuardReadMinSec', '$script:GuardWorkerFunctions', '$script:GuardWorker') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] -and $x.Left.Extent.Text -ceq $v }, $true).Extent.Text)) }\n"
                  "function Get-RunDirOutputRefusal { param($p) '' }\n"
                  "function Get-InputUnresolvedCode { param($p, [switch]$SkipRead) '' }\n"
                  "function Get-LegSpecRefusal { param($p, $b) @('', '') }\n"
                  "function Wait-RunDirReadSeam { }\n"
                  "function Complete-VenueProcess { param($ExitCode) }\n"
                  "function Register-LogFailure { param($w) }\n"
                  "function Test-AliasHit { param($h) $false }\n"
                  "function Enable-WriteSuppression { param($h) $script:RunOutputAliased = $true }\n"
                  f"$here = '{psq(RUNNER.parent)}'; Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking\n"
                  f"$legPlanMode = $true; $AgentShare = '{psq(self.share)}'; $RunDir = '{psq(run_dir)}'; $Venue = 'bachelor'; $ClaimDir = 'c'; $artifactProbe = $null; $script:guardDelaySeam = $null; $script:claimWait = $null\n"
                  f"$stagedBuildProbe = [scriptblock]::Create([IO.File]::ReadAllText('{psq(manifest_probe)}'))\n"
                  "$script:summaryBeforeExit = $null; $script:receiptGuardHit = $null\n"
                  f"$spec = [pscustomobject]@{{ kind = 'spec'; item = 1; path = '{psq(spec)}'; backend = 'cpu' }}; $spec2 = [pscustomobject]@{{ kind = 'spec'; item = 3; path = '{psq(spec2)}'; backend = 'cpu' }}\n"
                  f"$goneBuild = [pscustomobject]@{{ kind = 'build'; item = 2; commit = '{self.commit_a}'; sha256 = '{self.manifests[self.commit_a]}'; path = '{psq(gone)}' }}\n"
                  f"$build = [pscustomobject]@{{ kind = 'build'; item = 1; commit = '{self.commit_a}'; sha256 = '{self.manifests[self.commit_a]}'; path = '{psq(self._staged_manifest(self.commit_a))}' }}\n"
                  "function Get-Inputs([string]$k) { if ($k -ceq 'no_room') { @($spec, $spec2) } elseif ($k -ceq 'answered_no_room') { @($goneBuild, $spec) } else { @($build) } }\n"
                  "$hits = [ordered]@{}\n"
                  "foreach ($k in 'no_room', 'answered_no_room', 'hash_no_room') {\n"
                  "    $script:guardedInputs = @(Get-Inputs $k)\n"
                  "    $script:guardBudget = New-GuardBudget 60 'the test room' 'SHARE_UNREACHABLE' $false 'claim' ($k -cne 'hash_no_room')\n"
                  # the hash row: a 60 s room, the build's 6 s share, a manifest read that answers at 4 s: the hash has under its 2 s minimum of the share while the budget has some 56 s left
                  # (VenueChain: UNREACHABLE, artifactsUnhashed, no read given up on; adversary r5 A1 and its pass 2 form)
                  "    $script:guardBudget | Add-Member room $(if ($k -ceq 'hash_no_room') { 60 } else { 1 })\n"
                  "    $hits[$k] = Invoke-InputGuard; $script:guardBudget = $null\n"
                  "}\n"
                  # from here on every read of an input's (or batch.log's) path or identity is a share that answers 1.5 s late
                  "function Get-PhysicalPath { param($p) $script:reads++; [Threading.Thread]::Sleep(1500); $p }\n"
                  "function Get-FileIdentity { param($p) $script:reads++; [Threading.Thread]::Sleep(1500); [pscustomobject]@{ key = \"k:$p\"; links = 1 } }\n"
                  "$out = foreach ($k in $hits.Keys) { foreach ($log in 'present', 'missing') {\n"
                  "    $script:guardedInputs = @(Get-Inputs $k)\n"
                  "    $batchLog = Join-Path $RunDir 'batch.log'\n"
                  "    if ($log -ceq 'present') { [IO.File]::WriteAllText($batchLog, \"before`n\") } elseif ([IO.File]::Exists($batchLog)) { [IO.File]::Delete($batchLog) }\n"
                  "    $script:RunOutputAliased = $false; $script:reads = 0; $err = [IO.StringWriter]::new(); $old = [Console]::Error; [Console]::SetError($err)\n"
                  "    $sw = [Diagnostics.Stopwatch]::StartNew(); $ended = ''\n"
                  "    try { Stop-InputIsOutput $hits[$k] } catch { $ended = $_.Exception.Message } finally { $ms = $sw.ElapsedMilliseconds; [Console]::SetError($old) }\n"
                  "    [pscustomobject]@{ hit = $k; log = $log; ended = $ended; ms = $ms; reads = $script:reads; noAnswer = [bool]$hits[$k].noAnswer; readAbandoned = [bool]$hits[$k].readAbandoned; stderr = $err.ToString()\n"
                  "        logText = $(if ([IO.File]::Exists($batchLog)) { [IO.File]::ReadAllText($batchLog) } else { $null }) }\n"
                  "} }\n"
                  "[Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @($out)))\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(len(got), 6, got)
        for d in got:
            with self.subTest(hit=d["hit"], log=d["log"]):
                self.assertEqual(d["ended"], "EXIT2", d)
                self.assertEqual(d["reads"], 0, f"the refusal read an input (or batch.log) with no bound before it printed: {d}")
                self.assertLess(d["ms"], 1000, d)
                self.assertIn("BATCH_USAGE LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=", d["stderr"])
                self.assertIn("no time is left in the test room" if d["hit"] != "hash_no_room" else "no time is left of -TimeoutSec", d["stderr"])
                self.assertIn("batch.log is not written", d["stderr"])
                self.assertIn("this refusal is on stderr only", d["stderr"])
                self.assertEqual(d["logText"], "before\n" if d["log"] == "present" else None, "nothing was written into -RunDir")
                answered = d["hit"] == "answered_no_room"
                self.assertEqual(d["noAnswer"], d["hit"] == "no_room", "a pass that truly did not finish is still read again; one that saw a failure is refused (INV-1, INV-2)")
                self.assertFalse(d["readAbandoned"], f"no read was given up on: the no-room exits themselves are under test: {d}")
                self.assertEqual("already answered with a failure: BUILD_NOT_STAGED" in d["stderr"], answered, d)

    def test_artifact_preflight_claim_wait_r3_a_claim_request_that_throws_on_a_retry_is_the_typed_refusal(self) -> None:
        # adversary r3 pass 2 A2: the retry goes into a claim request nothing guarded, and a throw there ended the script with a read given up on still stuck on a live thread, which the host
        # then waits for. The runner's own claim loop, in memory, with a request that really throws (a file at the claim folder reads BUSY): the first pass's hit is the refusal, the
        # request's error named in it, and no second guard runs. With nothing unfinished the throw stays the throw it always was.
        def run(setup: str) -> dict:
            script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                      "$loop = $ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.WhileStatementAst] -and $x.Body.Extent.Text.Contains('Request-VenueClaim') }, $true) | Select-Object -Last 1\n"
                      "foreach ($n in 'Test-ClaimWaitRetryRoom', 'Get-ClaimWaitUnfinishedText') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
                      "Import-Module ([IO.Path]::Combine('" + psq(RUNNER.parent) + "', 'VenueChain.psm1')) -Force -DisableNameChecking\n"
                      "$script:requests = 0; $script:guards = 0; $script:refused = ''; $script:thrown = ''; $script:ClaimWaitUnfinishedMax = 3; $script:ClaimWaitGuardSec = 1; $script:receiptGuardHit = $null; $script:claimWait = @{ denied = 0; startUtc = $null; endUtc = $null }\n"
                      "$claim = $null; $lastHolder = ''; $unfinished = 0; $noAnswers = 0; $batchNo = 1; $Venue = 'bachelor'; $RunDir = 'r'; $Purpose = 'p'; $ClaimDir = 'c'; $ClaimTtlSec = 60; $ClaimRenewSec = 15; $ClaimPollSec = 1; $batches = @()\n"
                      "function New-GuardBudget { param($a, $b, $c, $d, $e, $f) [pscustomobject]@{} }\n"
                      "function Invoke-InputGuard { $script:guards++; [pscustomobject]@{ kind = 'content'; item = 1; code = 'SHARE_UNREACHABLE'; noAnswer = $true; readAbandoned = $true; locationUnknown = $true; why = 'WHY' } }\n"
                      "function Test-AliasHit { param($h) $false }\n"
                      "function LogBest { param($m) }\n"
                      "function Stop-InputIsOutput { param($h) $script:refused = $h.why; throw 'REFUSED' }\n"
                      "$deadline = [DateTime]::UtcNow.AddSeconds(60)\n"
                      f"{setup}\n"
                      "try { . ([scriptblock]::Create($loop.Extent.Text)) } catch { if ($_.Exception.Message -cne 'REFUSED') { $script:thrown = $_.Exception.Message } }\n"
                      "[pscustomobject]@{ requests = $script:requests; guards = $script:guards; refused = $script:refused; thrown = $script:thrown } | ConvertTo-Json -Compress\n")
            r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return json.loads(r.stdout.strip().splitlines()[-1])
        # r6: the retry's request runs on its bounded worker, which rethrows what the request threw
        retry = self._retry_request_loop("throw 'claim folder went'", 60, extra="$script:ClaimWaitGuardSec = 1")
        self.assertEqual((retry["requests"], retry["workerRequests"], retry["guards"], retry["thrown"]), (2, 1, 1, ""), retry)
        self.assertEqual(retry["refused"], "WHY (the claim request then threw: claim folder went) (no answer on 1 read in a row while no claim was held)", retry)
        first = run("function Request-VenueClaim { $script:requests++; throw 'claim folder went' }")
        self.assertEqual((first["requests"], first["guards"], first["refused"], first["thrown"]), (1, 0, "", "claim folder went"), first)

    DENIED = "[pscustomobject]@{ granted = $false; broken = @(); claim = $null; state = 'held' }"

    def _retry_request_loop(self, retry: str, deadline_sec: float, extra: str = "", settle_sec: float = 0.0, release: str = "") -> dict:
        # r6 (sol r5 blocker): the runner's own claim loop, in memory. The first request is master's (in this runspace, denied at once); every RETRY request answers with `retry`. The
        # retry's request runs on a worker that imports VenueChain.psm1 from $here, so the same `retry` is a stub module there; on a head that still calls it inline, the in-runspace stub
        # answers it the same way. The first guard pass does not finish (noAnswer), so the loop retries; a granted attempt's guard answers. `settle_sec` waits after the refusal for a
        # worker given up on to end (a late grant it must release itself).
        stub = Path(tempfile.mkdtemp(prefix="retry-req-", dir=self.case))
        log = psq(stub)
        (stub / "VenueChain.psm1").write_text(
            "function Request-VenueClaim { param($Venue, $RunDir, $Purpose, $TtlSec, $ClaimDir) [IO.File]::AppendAllText('" + log + "\\requests.log', \"w`n\"); " + retry + " }\n"
            "function Release-VenueClaim { param($Venue, $Nonce, $ClaimDir) " + release + "[IO.File]::AppendAllText('" + log + "\\releases.log', \"worker:$Nonce`n\"); 'RELEASED' }\n", encoding="utf-8")
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "$loop = $ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.WhileStatementAst] -and $x.Body.Extent.Text.Contains('Request-VenueClaim') }, $true) | Select-Object -Last 1\n"
                  "foreach ($n in 'Test-ClaimWaitRetryRoom', 'Get-ClaimWaitUnfinishedText', 'Invoke-ClaimRetryRequest', 'Invoke-ClaimRetryRelease') { $f = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true); if ($f) { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
                  # r7: every $script:ClaimRetry* value (the workers, the grace and tail) and the guard's read minimum, as the runner sets them
                  "foreach ($a in $ast.EndBlock.Statements) { if ($a -is [System.Management.Automation.Language.AssignmentStatementAst] -and ($a.Left.Extent.Text -clike '$script:ClaimRetry*' -or $a.Left.Extent.Text -ceq '$script:GuardReadMinSec')) { . ([scriptblock]::Create($a.Extent.Text)) } }\n"
                  "Import-Module ([IO.Path]::Combine('" + psq(RUNNER.parent) + "', 'VenueChain.psm1')) -Force -DisableNameChecking\n"
                  f"$here = '{log}'\n"
                  "$script:budgets = [Collections.Generic.List[double]]::new(); $script:budgetRooms = [Collections.Generic.List[double]]::new()\n"
                  "$script:requests = 0; $script:guards = 0; $script:refused = ''; $script:refusedAt = -1.0; $script:thrown = ''; $script:ClaimWaitUnfinishedMax = 3; $script:ClaimWaitGuardSec = 0; $script:receiptGuardHit = $null; $script:claimWait = @{ denied = 0; startUtc = $null; endUtc = $null }\n"
                  "$claim = $null; $lastHolder = ''; $unfinished = 0; $noAnswers = 0; $batchNo = 1; $Venue = 'bachelor'; $RunDir = 'r'; $Purpose = 'p'; $ClaimDir = 'c'; $ClaimTtlSec = 60; $ClaimRenewSec = 15; $ClaimPollSec = 1; $batches = @()\n"
                  "function Request-VenueClaim { $script:requests++; if ($script:requests -eq 1) { " + self.DENIED + " } else { " + retry + " } }\n"
                  "function Release-VenueClaim { param($Venue, $Nonce, $ClaimDir) " + release + "[IO.File]::AppendAllText('" + log + "\\releases.log', \"main:$Nonce`n\"); 'RELEASED' }\n"
                  "function New-GuardBudget { param($a, $b, $c, $d, $e, $f) [pscustomobject]@{} }\n"
                  "function Invoke-InputGuard { $script:guards++; if ($req.granted) { $null } else { [pscustomobject]@{ kind = 'content'; item = 1; code = 'SHARE_UNREACHABLE'; noAnswer = $true; readAbandoned = $true; locationUnknown = $true; why = 'WHY' } } }\n"
                  "function Test-AliasHit { param($h) $false }\n"
                  "function LogBest { param($m) }\n"
                  "function Start-Sleep { param([int]$Seconds) [Threading.Thread]::Sleep($Seconds * 1000) }\n"
                  "function Stop-InputIsOutput { param($h) $script:refusedAt = $clock.Elapsed.TotalSeconds; $script:refused = $h.why; $script:aliasedAtRefusal = [bool]$script:RunOutputAliased; throw 'REFUSED' }\n"
                  f"{extra}\n"
                  f"$clock = [Diagnostics.Stopwatch]::StartNew(); $deadline = [DateTime]::UtcNow.AddSeconds({deadline_sec})\n"
                  "try { . ([scriptblock]::Create($loop.Extent.Text)) } catch { if ($_.Exception.Message -cne 'REFUSED') { $script:thrown = $_.Exception.Message } }\n"
                  "$ended = $clock.Elapsed.TotalSeconds\n"
                  f"$settle = [Diagnostics.Stopwatch]::StartNew(); while ($settle.Elapsed.TotalSeconds -lt {settle_sec} -and -not (Test-Path '{log}\\releases.log')) {{ [Threading.Thread]::Sleep(100) }}\n"
                  f"$w = @(if (Test-Path '{log}\\requests.log') {{ [IO.File]::ReadAllLines('{log}\\requests.log') }}); $rel = @(if (Test-Path '{log}\\releases.log') {{ [IO.File]::ReadAllLines('{log}\\releases.log') }})\n"
                  "[Console]::Out.WriteLine(([pscustomobject]@{ requests = $script:requests + $w.Count; workerRequests = $w.Count; guards = $script:guards; refused = $script:refused; refusedAt = $script:refusedAt; ended = $ended; deadlineSec = "
                  f"{deadline_sec}; thrown = $script:thrown; claim = $(if ($claim) {{ [string]$claim.nonce }}); releases = $rel; abandoned = (& (Get-Module VenueChain) {{ $script:VenueAbandonedReads }})\n"
                  "    budgets = @($script:budgets); budgetRooms = @($script:budgetRooms); aliasedAtRefusal = [bool]$script:aliasedAtRefusal; probes = [int]$script:probes } | ConvertTo-Json -Compress)); [Console]::Out.Flush()\n"
                  "[Environment]::Exit(0)\n")
        try:
            r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail(f"the claim loop did not end in 60 s (a deadline of {deadline_sec} s): the retry's claim request is not bounded")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_artifact_preflight_claim_wait_r6_a_retry_claim_request_that_overruns_the_room_is_the_saved_refusal_by_the_deadline(self) -> None:
        # sol r5 blocker (key-attack row B-01b): an unfinished no-claim pass, then a retry request that takes longer than the room left (a claim folder on a share that stalls). The request
        # is bounded by that room less 1 s, so the saved typed refusal prints before -DeadlineUtc, the worker given up on is counted, and no lease or leg follows. ~4 s per row.
        for name, retry in (("slow", "[Threading.Thread]::Sleep(6000); " + self.DENIED), ("never", "[Threading.Thread]::Sleep(600000); " + self.DENIED)):
            with self.subTest(retry=name):
                d = self._retry_request_loop(retry, 4)
                self.assertEqual(d["thrown"], "", d)
                self.assertEqual((d["requests"], d["guards"], d["claim"]), (2, 1, None), d)
                self.assertGreaterEqual(d["refusedAt"], 0, d)
                self.assertLess(d["refusedAt"], d["deadlineSec"], f"the saved refusal printed after the deadline: {d}")
                self.assertTrue(d["refused"].startswith("WHY (the claim request did not answer in "), d)
                self.assertIn("(no answer on 1 read in a row while no claim was held)", d["refused"])
                self.assertEqual(d["abandoned"], 1, f"the request's worker given up on is an abandoned read (Complete-VenueProcess exits at once): {d}")
                self.assertEqual(d["releases"], [], d)
        # adversary r6 A1: a deadline 30 days out overflowed WaitOne (past about 24.8 days), and the throw disposed the still-running worker synchronously. The wait is capped (600 s; 2 s
        # here, the cap lifted from the runner and lowered in memory so the row is quick), so the never-returning request is given up on at the cap. ~4 s.
        far = self._retry_request_loop("[Threading.Thread]::Sleep(600000); " + self.DENIED, 30 * 86400, extra="$script:ClaimRetryRequestMaxSec = 2")
        self.assertEqual((far["requests"], far["guards"], far["claim"], far["thrown"], far["abandoned"]), (2, 1, None, "", 1), far)
        self.assertTrue(far["refused"].startswith("WHY (the claim request did not answer in 2 s, the most a retry's request is waited for;"), far)

    def test_artifact_preflight_claim_wait_r6_a_retry_with_less_room_than_the_floor_requests_nothing(self) -> None:
        # the room left at the retry is about 1 s, under the 2 s floor: no request starts, the saved refusal prints now. The room checks are forced true so only the floor can stop it. ~2 s.
        d = self._retry_request_loop(self.DENIED, 4, extra="function Test-ClaimWaitRetryRoom { $true }\n"
                                     "function Start-Sleep { param([int]$Seconds) [Threading.Thread]::Sleep([Math]::Max(0, [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds - 1000))) }")
        self.assertEqual((d["requests"], d["workerRequests"], d["guards"], d["claim"], d["thrown"]), (1, 0, 1, None, ""), d)
        self.assertLess(d["refusedAt"], d["deadlineSec"], d)
        self.assertTrue(d["refused"].startswith("WHY (no claim request was made: "), d)
        self.assertEqual(d["abandoned"], 0, d)

    def test_artifact_preflight_claim_wait_r6_a_late_grant_is_released_and_refused(self) -> None:
        # a retry request that GRANTS after its bound: the loop has already printed the saved refusal; the worker, finding itself given up on, releases its own grant, so no claim is left
        # behind and no guard or lease runs on it. ~7 s. And the grant that comes back inside the bound but at or past the deadline (a late wakeup): released by the worker, never used. ~2 s.
        late = self._retry_request_loop("[Threading.Thread]::Sleep(4000); [pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'late-n' }; state = 'TAKEN' }", 4, settle_sec=6)
        self.assertEqual((late["requests"], late["guards"], late["claim"], late["thrown"]), (2, 1, None, ""), late)
        self.assertLess(late["refusedAt"], late["deadlineSec"], late)
        self.assertEqual(late["releases"], ["worker:late-n"], f"the late grant was not released by its worker: {late}")
        self.assertEqual(late["abandoned"], 1, late)
        # inside the bound, past the deadline: Invoke-ClaimRetryRequest itself, a grant at once, with the worker's late cutoff moved to yesterday IN MEMORY (the function text lifted from
        # the runner and that one argument replaced; adversary r6 A2: the runner takes no such parameter), as a caller woken late past the deadline would find it
        stub = Path(tempfile.mkdtemp(prefix="retry-late-", dir=self.case))
        (stub / "VenueChain.psm1").write_text(
            "function Request-VenueClaim { param($Venue, $RunDir, $Purpose, $TtlSec, $ClaimDir) [pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'edge-n' }; state = 'TAKEN' } }\n"
            "function Release-VenueClaim { param($Venue, $Nonce, $ClaimDir) [IO.File]::AppendAllText('" + psq(stub) + "\\releases.log', \"worker:$Nonce`n\"); 'RELEASED' }\n", encoding="utf-8")
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "$f = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Invoke-ClaimRetryRequest' }, $true)\n"
                  "$anchor = 'lateAfterUtc = $DeadlineUtc'; if (([regex]::Matches($f.Extent.Text, [regex]::Escape($anchor))).Count -ne 1) { throw 'the late cutoff anchor moved' }\n"
                  ". ([scriptblock]::Create($f.Extent.Text.Replace($anchor, 'lateAfterUtc = [DateTime]::UtcNow.AddDays(-1)')))\n"
                  "foreach ($a in $ast.EndBlock.Statements) { if ($a -is [System.Management.Automation.Language.AssignmentStatementAst] -and $a.Left.Extent.Text -clike '$script:ClaimRetryRequest*') { . ([scriptblock]::Create($a.Extent.Text)) } }\n"
                  "Import-Module ([IO.Path]::Combine('" + psq(RUNNER.parent) + "', 'VenueChain.psm1')) -Force -DisableNameChecking\n"
                  f"$here = '{psq(stub)}'\n"
                  "$r = Invoke-ClaimRetryRequest -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(30)) -RequestArgs @{ Venue = 'bachelor'; RunDir = 'r'; Purpose = 'p'; TtlSec = 60; ClaimDir = 'c' }\n"
                  f"$s = [Diagnostics.Stopwatch]::StartNew(); while ($s.Elapsed.TotalSeconds -lt 8 -and -not (Test-Path '{psq(stub)}\\releases.log')) {{ [Threading.Thread]::Sleep(100) }}\n"
                  f"[Console]::Out.WriteLine(([pscustomobject]@{{ granted = [bool]$r.granted; state = $r.state; why = $r.why; releases = @(if (Test-Path '{psq(stub)}\\releases.log') {{ [IO.File]::ReadAllLines('{psq(stub)}\\releases.log') }}) }} | ConvertTo-Json -Compress)); [Console]::Out.Flush(); [Environment]::Exit(0)\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        edge = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((edge["granted"], edge["state"], edge["releases"]), (False, "REQUEST_LATE_GRANT", ["worker:edge-n"]), edge)

    def test_artifact_preflight_claim_wait_r6_a_retry_grant_let_go_on_a_stalled_claim_share_still_refuses_by_the_deadline(self) -> None:
        # adversary r6 pass 2 A1: the retry is granted at once, its guard refuses, and the claim share then stops answering: the release (inline, unbounded, before the refusal
        # printed) held the refusal past the deadline. A claim granted on a retry is let go on a bounded worker, given up on (r7: at the reserve at most) and counted. ~4 s.
        # r7: a 6 s deadline, so the grant (about 4.6 s left) has room for its cut guard (under 2 s of it, no guard runs at all: the 1.2 s row below)
        d = self._retry_request_loop("[pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'stuck-n' }; state = 'TAKEN' }", 6,
                                     extra="$script:RunOutputAliased = $false; $script:suppressionHit = $null\nfunction Invoke-InputGuard { $script:guards++; if ($req.granted) { return [pscustomobject]@{ kind = 'content'; item = 1; code = 'LEG_SPEC_INVALID'; why = 'WHY' } }; "
                                           "[pscustomobject]@{ kind = 'content'; item = 1; code = 'SHARE_UNREACHABLE'; noAnswer = $true; readAbandoned = $true; locationUnknown = $true; why = 'WHY' } }",
                                     release="[Threading.Thread]::Sleep(600000); ")
        self.assertEqual((d["requests"], d["guards"], d["claim"], d["thrown"], d["refused"]), (2, 2, None, "", "WHY"), d)
        self.assertLess(d["refusedAt"], d["deadlineSec"], f"the refusal waited on the release past the deadline: {d}")
        self.assertEqual(d["abandoned"], 1, f"the release given up on is an abandoned read: {d}")
        # adversary r7 A1: the granted guard ANSWERED a failure (LEG_SPEC_INVALID: no location unknown, no suppression of its own), so the refusal would probe batch.log in -RunDir,
        # unbounded, on the share that just stopped answering the release: the release given up on turns the write suppression on before the refusal
        self.assertTrue(d["aliasedAtRefusal"], f"the refusal after a release given up on could still probe -RunDir: {d}")

    # r7 (adversary r6 pass 3 A1): the guard of a claim granted on a retry. The stub guard waits exactly what Invoke-InputGuard's worker wait is for the budget it is handed (the
    # budget plus 0.5 s, or a fixed 2 s under 2 s: :872) and then refuses, as a share that stays stuck makes it; Start-Sleep sleeps until `left` s remain.
    RETRY_GUARD = ("function New-GuardBudget { param($a, $b, $c, $d, $e, $f) $script:budgets.Add([double]$a); $script:budgetRooms.Add(($deadline - [DateTime]::UtcNow).TotalSeconds); [pscustomobject]@{} }\n"
                   "function Invoke-InputGuard { $script:guards++; if (-not $req.granted) { return [pscustomobject]@{ kind = 'content'; item = 1; code = 'SHARE_UNREACHABLE'; noAnswer = $true; readAbandoned = $true; locationUnknown = $true; why = 'WHY' } }\n"
                   "    $s = $script:budgets[$script:budgets.Count - 1]; [Threading.Thread]::Sleep([int][Math]::Max(0.0, 1000.0 * $(if ($s -lt 2) { 2.0 } else { $s + 0.5 })))\n"
                   "    [pscustomobject]@{ kind = 'content'; item = 1; code = 'SHARE_UNREACHABLE'; noAnswer = $true; readAbandoned = $true; locationUnknown = $true; why = 'GRANTED-GUARD' } }\n")

    @staticmethod
    def _sleep_until_left(left: float, grant_left: float | None = None) -> str:
        grant = f"$env:MLV_TEST_RETRY_GRANT_AT = [string]$deadline.AddSeconds(-{grant_left}).Ticks; " if grant_left is not None else ""
        return ("function Start-Sleep { param([int]$Seconds) " + grant +
                f"[Threading.Thread]::Sleep([Math]::Max(0, [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds - {int(left * 1000)}))) }}\n")

    GRANT_AT = ("$t = [DateTime]::new([long]$env:MLV_TEST_RETRY_GRANT_AT, [DateTimeKind]::Utc); $ms = ($t - [DateTime]::UtcNow).TotalMilliseconds; if ($ms -gt 0) { [Threading.Thread]::Sleep([int]$ms) }; "
                "[pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'g-n' }; state = 'TAKEN' }")

    def test_artifact_preflight_claim_wait_r7_a_retry_granted_with_no_room_for_its_guard_is_refused_by_the_deadline(self) -> None:
        # the retry's request starts with 3.5 s left (its bound is 2.5 s) and grants with 1.2 s left. The guard it was given (the whole 1.2 s) waited a fixed 2 s, then the release:
        # the refusal came at least 0.8 s after -DeadlineUtc. Now no guard runs, the grant is released on its worker, and the saved refusal prints before the deadline. ~4 s.
        d = self._retry_request_loop(self.GRANT_AT, 4, extra=self.RETRY_GUARD + self._sleep_until_left(3.5, grant_left=1.2))
        self.assertEqual((d["requests"], d["workerRequests"], d["claim"], d["thrown"]), (2, 1, None, ""), d)
        self.assertGreaterEqual(d["refusedAt"], 0, d)
        self.assertLess(d["refusedAt"], d["deadlineSec"], f"the refusal printed after the deadline: {d}")
        self.assertEqual(d["guards"], 1, f"a guard ran on a grant with no room for one: {d}")
        self.assertEqual(d["releases"], ["worker:g-n"], f"the grant was not released (bounded, on its worker): {d}")
        self.assertEqual(d["abandoned"], 0, d)
        self.assertTrue(d["refused"].startswith("WHY (the claim was then granted with "), d)
        self.assertIn("so it was released and no guard ran) (no answer on 1 read in a row while no claim was held)", d["refused"])

    def test_artifact_preflight_claim_wait_r7_a_retry_granted_with_room_runs_its_guard_cut_and_releases_by_the_deadline(self) -> None:
        # granted at once with about 3.7 s left: its guard is cut to that room less 0.5 s and 1 s (it was the whole room, waited for plus 0.5 s: 0.5 s past the deadline before the
        # release), so the guard gives up 1 s before -DeadlineUtc, and the claim is released and the refusal printed in that second. ~5 s.
        d = self._retry_request_loop("[pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'room-n' }; state = 'TAKEN' }", 4.5,
                                     extra=self.RETRY_GUARD + self._sleep_until_left(4.0))
        self.assertEqual((d["requests"], d["guards"], d["claim"], d["thrown"], d["refused"]), (2, 2, None, "", "GRANTED-GUARD"), d)
        self.assertEqual(len(d["budgets"]), 2, d)
        cut, room = d["budgets"][-1], d["budgetRooms"][-1]
        self.assertGreaterEqual(cut, 2.0, f"the row needs a guard that runs: {d}")
        self.assertLessEqual(cut, room - 1.5 + 0.05, f"the granted retry's guard was not cut to the room less 0.5 s and 1 s: {d}")
        self.assertLess(d["refusedAt"], d["deadlineSec"], f"the refusal printed after the deadline: {d}")
        self.assertEqual((d["releases"], d["abandoned"]), (["worker:room-n"], 0), d)

    def test_artifact_preflight_claim_wait_r7_a_retry_release_with_no_room_is_given_up_on_at_once(self) -> None:
        # Invoke-ClaimRetryRelease lifted from the runner, a claim share that never answers: with the deadline gone it was still given 1 s (the old floor); now it is given up on at
        # once and counted, never started. With 0.6 s left it waits under that room. With room, a healthy release answers (INV-1). One process, ~3 s.
        stub = Path(tempfile.mkdtemp(prefix="retry-rel-", dir=self.case))
        (stub / "VenueChain.psm1").write_text(
            "function Release-VenueClaim { param($Venue, $Nonce, $ClaimDir) [IO.File]::AppendAllText('" + psq(stub) + "\\releases.log', \"start:$Nonce`n\"); if ($Nonce -like 'stall*') { [Threading.Thread]::Sleep(600000) }; 'RELEASED' }\n", encoding="utf-8")
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  ". ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Invoke-ClaimRetryRelease' }, $true).Extent.Text))\n"
                  "foreach ($a in $ast.EndBlock.Statements) { if ($a -is [System.Management.Automation.Language.AssignmentStatementAst] -and $a.Left.Extent.Text -clike '$script:ClaimRetry*') { . ([scriptblock]::Create($a.Extent.Text)) } }\n"
                  "Import-Module ([IO.Path]::Combine('" + psq(RUNNER.parent) + "', 'VenueChain.psm1')) -Force -DisableNameChecking\n"
                  f"$here = '{psq(stub)}'; $Venue = 'bachelor'; $ClaimDir = 'c'\n"
                  "$out = foreach ($c in @(@('stall-past', -1.0), @('stall-short', 0.6), @('ok-n', 30.0))) {\n"
                  "    $script:RunOutputAliased = $false; $script:suppressionHit = $null; $script:batchLog = 'b'; $before = & (Get-Module VenueChain) { $script:VenueAbandonedReads }; $deadline = [DateTime]::UtcNow.AddSeconds($c[1]); $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "    $r = Invoke-ClaimRetryRelease $deadline $c[0]\n"
                  "    [pscustomobject]@{ nonce = $c[0]; sec = $sw.Elapsed.TotalSeconds; result = [string]$r; aliased = [bool]$script:RunOutputAliased; abandoned = (& (Get-Module VenueChain) { $script:VenueAbandonedReads }) - $before } }\n"
                  "[Threading.Thread]::Sleep(300)\n"
                  f"[Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @{{ cases = @($out); log = @(if (Test-Path '{psq(stub)}\\releases.log') {{ [IO.File]::ReadAllLines('{psq(stub)}\\releases.log') }}) }})); [Console]::Out.Flush(); [Environment]::Exit(0)\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        past, short, ok = got["cases"]
        self.assertLess(past["sec"], 0.3, f"a release with the deadline gone waited: {got}")
        self.assertEqual(past["abandoned"], 1, f"a release given up on at once is counted: {got}")
        self.assertTrue(past["result"].startswith("NO_ROOM "), got)
        self.assertNotIn("start:stall-past", got["log"], f"a release with no room was started: {got}")
        self.assertLess(short["sec"], 0.6, f"a release with 0.6 s left waited past that room: {got}")
        self.assertEqual(short["abandoned"], 1, got)
        self.assertEqual((ok["result"], ok["abandoned"], ok["aliased"]), ("RELEASED", 0, False), got)
        # adversary r7 A1: a release given up on (or never started) turns the write suppression on
        self.assertEqual((past["aliased"], short["aliased"]), (True, True), got)
        self.assertIn("start:ok-n", got["log"], got)

    # r8 (sol r7 blocker): a retry grant's release that THROWS (the claim share's ReadToEnd or Delete raising IOException, through EndInvoke) returned NO_ANSWER with the write
    # suppression still off, so the refusal or log line after it probed -RunDir unbounded (Stop-PlanRefused's batch.log look and write guard, L's write guard). The runner's own
    # Stop-InputIsOutput, Get-HitRefusal, Stop-PlanRefused (its exit made a throw), L and Test-RunDirWrite are lifted; every -RunDir probe they make (Wait-RunDirReadSeam, the write
    # guard's Get-WriteDestinationHit) stalls 5 s, as a -RunDir on the share that just failed would. One row per caller of Invoke-ClaimRetryRelease.
    RELEASE_PROBE_MS = 5000

    def _retry_release_throws(self, site: str, release: str = "throw [IO.IOException]::new('the claim share went'); ") -> dict:
        stub = Path(tempfile.mkdtemp(prefix="retry-rel-throw-", dir=self.case))
        lift = ("foreach ($n in 'Stop-InputIsOutput', 'Get-HitRefusal', 'L', 'Test-RunDirWrite') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
                "$sp = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Stop-PlanRefused' }, $true).Extent.Text\n"
                "if ([regex]::Matches($sp, '(?m)^\\s*exit 2\\s*$').Count -ne 1) { throw 'the refusal exit anchor moved' }\n"
                "$m = [regex]::Match($sp, '(?m)^\\s*exit 2\\s*$'); . ([scriptblock]::Create($sp.Remove($m.Index, $m.Length).Insert($m.Index, ' $script:refusedAt = $clock.Elapsed.TotalSeconds; $script:refused = $line; $script:aliasedAtRefusal = [bool]$script:RunOutputAliased; throw ''REFUSED''')))\n"
                "function Complete-VenueProcess { param($ExitCode) }\n"
                "function Wait-RunDirReadSeam { $script:probes++; [Threading.Thread]::Sleep(" + str(self.RELEASE_PROBE_MS) + ") }\n"
                "function Get-WriteDestinationHit { param($Path, [switch]$Directory) $script:probes++; [Threading.Thread]::Sleep(" + str(self.RELEASE_PROBE_MS) + "); $null }\n"
                "$script:RunOutputAliased = $false; $script:suppressionHit = $null; $script:summaryBeforeExit = $null; $script:probes = 0; $legPlanMode = $false; $batchLog = '" + psq(stub) + "\\batch.log'\n")
        retry_guard = "function Invoke-InputGuard { $script:guards++; if (-not $req.granted) { return [pscustomobject]@{ kind = 'content'; item = 1; code = 'SHARE_UNREACHABLE'; noAnswer = $true; readAbandoned = $true; locationUnknown = $true; why = 'WHY' } }; "
        granted = {
            # :1925 -- the granted guard ANSWERS a failure (no location unknown, no suppression of its own); the refusal is Stop-InputIsOutput's
            "answered": (retry_guard + "[pscustomobject]@{ kind = 'content'; item = 1; code = 'LEG_SPEC_INVALID'; why = 'WHY' } }\n", 6),
            # :1898 -- the granted guard throws; the refusal is Stop-PlanRefused LEG_PLAN_INVALID
            "threw": (retry_guard + "throw 'the guard went' }\n", 6),
            # :1943 -- the granted guard answers clean but outlasts -ClaimTtlSec less -ClaimRenewSec (4 less 1 here): the claim is let go and CLAIM_LAPSED_IN_GUARD is logged (L)
            "lapsed": (retry_guard + "[Threading.Thread]::Sleep(3100); $null }\n$ClaimTtlSec = 4; $ClaimRenewSec = 1\n"
                       "function LogBest { param($m) if ($m -clike 'CLAIM_LAPSED_IN_GUARD*') { L $m | Out-Null; $script:refusedAt = $clock.Elapsed.TotalSeconds; $script:refused = $m; $script:aliasedAtRefusal = [bool]$script:RunOutputAliased; throw 'REFUSED' } }\n", 8),
        }[site]
        d = self._retry_request_loop("[pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'throw-n' }; state = 'TAKEN' }", granted[1],
                                     extra=lift + granted[0],
                                     release=release)
        return d

    def test_artifact_preflight_claim_wait_r8_a_retry_release_that_throws_turns_the_suppression_on_at_every_caller(self) -> None:
        # sol r7 blocker, at each of Invoke-ClaimRetryRelease's three callers: the retry is granted at once, the release throws IOException through EndInvoke, and what follows
        # (the typed refusal, or CLAIM_LAPSED_IN_GUARD's log line) must make no -RunDir probe and print before -DeadlineUtc. Before r8 each made 1-2 probes (5 s each here), past
        # the deadline. ~3 s, ~3 s and ~6 s green.
        for site, start in (("answered", "LEG_PLAN_REFUSED reason=LEG_SPEC_INVALID item=1 WHY"), ("threw", "LEG_PLAN_REFUSED reason=LEG_PLAN_INVALID the plan could not be re-validated once the claim was held: the guard went"),
                            ("lapsed", "CLAIM_LAPSED_IN_GUARD batch=1 ")):
            with self.subTest(site=site):
                d = self._retry_release_throws(site)
                self.assertEqual((d["requests"], d["workerRequests"], d["guards"], d["claim"], d["thrown"]), (2, 1, 2, None, ""), d)
                self.assertTrue(d["refused"].startswith(start), d)
                self.assertEqual((d["abandoned"], d["releases"]), (0, []), f"the release threw (it answered, with an exception): nothing was given up on: {d}")
                self.assertTrue(d["aliasedAtRefusal"], f"a release that threw left the write suppression off: {d}")
                self.assertEqual(d["probes"], 0, f"the refusal after a release that threw probed -RunDir: {d}")
                self.assertLess(d["refusedAt"], d["deadlineSec"], f"the refusal after a release that threw printed past the deadline: {d}")
        # r8 pass 1 (adversary r8 A1): a release that ANSWERS but releases nothing (BUSY: the claim is still there, five opens of it refused, in 750 ms here) is not a release
        # either: only RELEASED, NOT_OURS or ABSENT leave the suppression off. ~3 s.
        d = self._retry_release_throws("answered", release="[Threading.Thread]::Sleep(750); return 'BUSY'; ")
        self.assertEqual((d["guards"], d["claim"], d["thrown"], d["abandoned"], d["releases"]), (2, None, "", 0, []), d)
        self.assertTrue(d["refused"].startswith("LEG_PLAN_REFUSED reason=LEG_SPEC_INVALID item=1 WHY"), d)
        self.assertTrue(d["aliasedAtRefusal"], f"a release that answered BUSY left the write suppression off: {d}")
        self.assertEqual(d["probes"], 0, f"the refusal after a release that answered BUSY probed -RunDir: {d}")
        self.assertLess(d["refusedAt"], d["deadlineSec"], d)

    def test_artifact_preflight_claim_wait_r6_a_fast_retry_request_is_unchanged(self) -> None:
        # INV-1 controls: a retry request that is denied at once still loops (three unfinished passes, then the refusal, as in r5); one that grants at once with room proceeds, its guard
        # answered and the claim taken, nothing released. ~4 s and ~2 s. The worker counts are asserted last and apart: on a head that asks inline they are the only difference.
        denied = self._retry_request_loop(self.DENIED, 60)
        self.assertEqual((denied["requests"], denied["guards"], denied["claim"], denied["thrown"]), (3, 3, None, ""), denied)
        self.assertEqual(denied["refused"], "WHY (no answer on 3 reads in a row while no claim was held)", denied)
        self.assertEqual((denied["abandoned"], denied["releases"]), (0, []), denied)
        granted = self._retry_request_loop("[pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'fast-n' }; state = 'TAKEN' }", 60)
        self.assertEqual((granted["requests"], granted["guards"], granted["claim"], granted["refused"], granted["thrown"]), (2, 2, "fast-n", "", ""), granted)
        self.assertEqual((granted["abandoned"], granted["releases"]), (0, []), granted)
        # adversary r6 A1 and A2: a grant at once still proceeds with a deadline 30 days out (WaitOne overflowed there), and with a default parameter value naming a late cutoff
        # (yesterday) in the session, which marked that timely grant late while the helper took one. ~2 s.
        far = self._retry_request_loop("[pscustomobject]@{ granted = $true; broken = @(); claim = [pscustomobject]@{ nonce = 'far-n' }; state = 'TAKEN' }", 30 * 86400,
                                       extra="$PSDefaultParameterValues['Invoke-ClaimRetryRequest:LateAfterUtc'] = [DateTime]::UtcNow.AddDays(-1)")
        self.assertEqual((far["requests"], far["guards"], far["claim"], far["refused"], far["thrown"], far["abandoned"], far["releases"]), (2, 2, "far-n", "", "", 0, []), far)
        self.assertEqual(denied["workerRequests"], 2, f"the two retries did not go through the bounded worker: {denied}")
        self.assertEqual(granted["workerRequests"], 1, f"the retry did not go through the bounded worker: {granted}")

    def test_artifact_preflight_claim_wait_r3_the_refusal_says_no_answer_only_for_reads_given_up_on(self) -> None:
        # fable r2: three passes that ran out of room while every read answered are not "no answer on 3 reads", and a run of both says how many of each
        cases = [(3, 3, "(no answer on 3 reads in a row while no claim was held)"),
                 (3, 0, "(3 guard passes in a row ran out of time while no claim was held; every read that started answered)"),
                 (3, 2, "(3 guard passes in a row did not finish while no claim was held: no answer on 2 reads, and the others ran out of time)"),
                 (1, 0, "(1 guard pass in a row ran out of time while no claim was held; every read that started answered)")]
        for strikes, given_up, want in cases:
            ran = self._claim_loop_snippet(f"$overshootSec = 0; $deadline = [DateTime]::UtcNow.AddSeconds(600); $unfinished = {strikes - 1}; $noAnswers = {given_up - 1 if given_up else 0}; $hit.readAbandoned = {'$true' if given_up else '$false'}"
                                           if strikes == 3 else "$overshootSec = 0; $deadline = [DateTime]::UtcNow.AddSeconds(1); $hit.readAbandoned = $false")
            self.assertEqual(ran["outcome"], "REFUSAL", ran)
            self.assertEqual(ran["why"], f"WHY {want}")

    def test_artifact_preflight_claim_wait_r3_a_hit_with_no_room_is_not_a_read_given_up_on(self) -> None:
        # in memory, so no host is too slow for it: the spec read answers at 1.5 s of a 3 s budget, the build read has no room to start. noAnswer keeps the retry; the '+' is only for
        # a read given up on, and the seconds each read took are the same either way.
        script = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
                  "foreach ($n in 'New-GuardBudget', 'Get-InputContentRefusal', 'Set-GuardProgress', 'Get-GuardTimingText') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
                  "$script:GuardReadMinSec = 2; $legPlanMode = $true; $script:claimWait = $null\n"
                  "function Wait-GuardReadSeam { param($r) }\n"
                  "function Get-LegSpecRefusal { param($p, $b) [Threading.Thread]::Sleep(1500); @($null, $null) }\n"
                  "$script:guardedInputs = @([pscustomobject]@{ kind = 'spec'; item = 1; path = 's'; backend = 'cuda' }, [pscustomobject]@{ kind = 'build'; item = 1; path = 'm'; commit = ('a' * 40); sha256 = ('a' * 64) })\n"
                  "$prog = [hashtable]::Synchronized(@{ locationsDone = $true; item = 0; at = 'x'; steps = [Collections.ArrayList]::Synchronized([Collections.ArrayList]::new()) })\n"
                  "$script:guardProgress = $prog\n"
                  "$script:guardBudget = New-GuardBudget 3 'the test room' 'SHARE_UNREACHABLE' $false 'claim' $true\n"
                  "$h = Get-InputContentRefusal\n"
                  "[pscustomobject]@{ noAnswer = [bool]$h.noAnswer; hasFlag = ($null -ne $h.PSObject.Properties['readAbandoned']); readAbandoned = [bool]$h.readAbandoned; answered = (Get-GuardTimingText $prog $script:guardBudget ([bool]$h.readAbandoned)); givenUp = (Get-GuardTimingText $prog $script:guardBudget $true) } | ConvertTo-Json -Compress\n")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(d["noAnswer"], "it still reads again next poll")
        self.assertTrue(d["hasFlag"], d)
        self.assertFalse(d["readAbandoned"], d)
        self.assertRegex(d["answered"], r"content:spec1=[0-9.]+ claimWaitBefore=no$")
        self.assertRegex(d["givenUp"], r"content:spec1=[0-9.]+\+ claimWaitBefore=no$")

    def test_artifact_preflight_claim_wait_r3_good_polls_between_no_answers_never_add_up_to_the_refusal(self) -> None:
        # dot I2: "three in a row" is three in a row. Manifest reads 2, 4 and 6 are given up on, 3, 5 and 7 answer, then the foreign claim lets go: the plan runs.
        calls = self.case / "manifest-calls.txt"
        manifest_probe, _ = self._timed_probes(0, 0)
        manifest_probe.write_text(manifest_probe.read_text(encoding="utf-8").replace(
            "param($p)\n", f"param($p)\n[IO.File]::AppendAllText('{calls}', 'm' + [Environment]::NewLine)\nif (@(2, 4, 6) -contains [IO.File]::ReadAllLines('{calls}').Count) {{ [Threading.Thread]::Sleep(40000) }}\n", 1), encoding="utf-8")
        args, e = self._claim_wait_cmd(manifest_probe=manifest_probe)

        def until_three_were_given_up_on() -> None:
            end = time.monotonic() + 180
            while time.monotonic() < end and (not calls.exists() or len(calls.read_text(encoding="utf-8").splitlines()) < 7):
                time.sleep(0.2)
        code, so, se = self._during_claim_wait(args, e, until_three_were_given_up_on)
        self.assertEqual(code, 0, so + se)
        self.assertNotIn("LEG_PLAN_REFUSED", se)
        self.assertEqual(se.count("CLAIM_WAIT_GUARD_NO_ANSWER batch=1 unfinished=1/3 noAnswers=1 "), 3, se)
        self.assertNotIn("unfinished=2/3", se)
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("CLAIM_WAIT_GUARD_ANSWERED batch=1 unfinished=1 noAnswers=1 "), 3, log)
        self.assertEqual(len(self.leg_calls()), 1, so + se)

    def test_artifact_preflight_claim_wait_r3_a_hung_share_reacquired_after_no_answer_stops_at_the_deadline(self) -> None:
        # fable r2 hardening 3: the foreign claim lets go between no-answers and the input is still bad. The granted attempt keeps first-failure behaviour (no second chance, the claim
        # released, no lease), and its guard ends at the run's deadline, not on the claim's 10199 s budget. Fixture only: the 90 s read stands in for a share that does not answer.
        args, e = self._claim_wait_cmd("-GuardReadDelay", "claim:existence=90", "-ClaimPollSec", "3", "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=75)))
        code, so, se, took = self._run_acting_on_stderr(args, e, "CLAIM_WAIT_GUARD_NO_ANSWER", self.claim_path.unlink)
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 the guard's reads did not finish within the claim guard", se)
        self.assertIn("cut to the run's deadline after an unfinished read", se)
        self.assertEqual(se.count("CLAIM_WAIT_GUARD_NO_ANSWER"), 1, se)
        self.assertNotIn("no answer on", se, "a granted attempt refuses on its first failure, not as a strike")
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        # r7 (adversary r6 pass 3 A1): the granted retry's guard gives up 1 s before the 75 s deadline and the release and the refusal fit in that second, so the process ends by the
        # deadline plus its own exit (Complete-VenueProcess exits at once: the guard's worker was given up on). 2 s for that exit, where 10 s hid a refusal up to 3 s late.
        self.assertLess(took, 77, f"the run took {took:.1f} s: the refusal came after the 75 s deadline (or the 90 s read was waited for)")

    def test_artifact_preflight_claim_wait_r3_waited_sec_ends_at_the_grant(self) -> None:
        # opus r2 B2: 30 s in the open, then the open guard gives up. waitedSec said the time since the first denied poll, open included; it is the claim wait only.
        args, e = self._claim_wait_cmd("-LeaseCapSec", "75", "-GuardReadDelay", "open:identity=25", FAKE_OPEN_SLEEP="30")
        code, so, se = self._during_claim_wait(args, e, lambda: None)
        self.assertEqual(code, 2, so + se)
        self.assertIn("the guard's reads did not finish within the lease's room", se)
        m = re.search(r"claimWaitBefore=yes deniedPolls=[0-9]+ waitedSec=([0-9]+)", se)
        self.assertIsNotNone(m, se)
        self.assertLess(int(m.group(1)), 25, f"waitedSec={m.group(1)} counts the 30 s open: {se}")

    def test_artifact_preflight_claim_wait_r3_a_usage_stop_does_not_wait_on_a_read_given_up_on(self) -> None:
        # fable r2 hardening 5: the scalar form ends a hit through Stop-Usage. One read is given up on and stays stuck, the spec then goes, and exit 2 comes without waiting out that read.
        self._fresh_run_dir()
        self._foreign_live_claim()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        args, e = self.batch_cmd([spec], "-GuardReadDelay", "claim:identity=120x1")
        code, so, se, took = self._run_acting_on_stderr(args, e, "CLAIM_WAIT_GUARD_NO_ANSWER", spec.unlink)
        self.assertEqual(code, 2, so + se)
        self.assertIn("BATCH_USAGE ", se)
        self.assertLess(took, 100, f"the run took {took:.1f} s: exit 2 waited on the stuck read")

    # r5 ITEM 1 (key3 r4 B1, catalogue row L-05; Dot 6457724432): an attempt the claim was NOT granted for ran the guard inline with no budget, and the guard hashed every cached
    # artifact (-VerifyArtifacts) on the venue's share on every -ClaimPollSec poll while a foreign chain held the venue (and measured). Master's wait read the manifests only.
    def test_artifact_preflight_r5_l05_no_artifact_read_while_a_foreign_claim_is_waited_out_then_one_hash_under_the_claim(self) -> None:
        self._fresh_run_dir()
        state = self.share / "fake-agent-state.json"
        self.assertFalse(state.exists())
        manifest_calls, artifact_calls = self.case / "manifest-calls.txt", self.case / "artifact-calls.txt"
        manifest_probe, artifact_probe = self._timed_probes(0, 0)
        # every manifest read (one per build per guard, a denied poll's included) and every artifact hash (one per build per -VerifyArtifacts call) appends a line;
        # a hash says whether the open was answered yet (the fake agent's state file), so the claim guard's hash is told from the post-open guard's
        manifest_probe.write_text(manifest_probe.read_text(encoding="utf-8").replace("param($p)\n", f"param($p)\n[IO.File]::AppendAllText('{manifest_calls}', 'm' + [Environment]::NewLine)\n", 1),
                                  encoding="utf-8")
        artifact_probe.write_text("param($items)\n" + f"$w = $(if (Test-Path -LiteralPath '{state}' -PathType Leaf) {{ 'open' }} else {{ 'before-open' }})\n"
                                  + f"[IO.File]::AppendAllText('{artifact_calls}', $w + [Environment]::NewLine)\n", encoding="utf-8")

        def lines(p: Path) -> list[str]:
            return p.read_text(encoding="utf-8").split() if p.exists() else []

        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]),
                                "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("CLAIM_HELD")
            # the reset after the one-time preflight (Dot): CLAIM_HELD is logged after the first denied poll's guard, so the preflight's one hash is all there may be by now
            at_held = len(lines(artifact_calls))
            m0 = len(lines(manifest_calls))
            end = time.monotonic() + 120
            while len(lines(manifest_calls)) < m0 + 3 and time.monotonic() < end and p.poll() is None:
                time.sleep(0.2)
            denied_polls = len(lines(manifest_calls)) - m0
            during = len(lines(artifact_calls)) - at_held
            mark = len(lines(artifact_calls))
            self.claim_path.unlink()   # the foreign owner lets go: this run takes the claim next poll
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertGreaterEqual(denied_polls, 3, f"three more denied polls re-read the manifest (the manifest check is kept while no claim is held): {so + se}")
        self.assertEqual(at_held, 1, f"only the one-time preflight hashed before the first denied poll ended; the denied poll hashed too: {lines(artifact_calls)}")
        self.assertEqual(during, 0, f"{during} artifact hashes during {denied_polls} denied polls while a foreign claim held the venue")
        self.assertEqual(p.returncode, 0, so + se)
        after = lines(artifact_calls)[mark:]
        self.assertEqual(after.count("before-open"), 1, f"exactly one hash under the claim guard's budget once the claim was granted: {after}")
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsRan"]), ("ALL_LEGS_DONE", 1))

    # r12 (#704 merge): #704's receipt guard (Test-ReceiptWriteSuppressed) re-runs Get-InputOutputRefusal before each receipt file, with no guard budget, so on the merged tree it
    # hashed every cached artifact again (300 s bound) once per receipt file after the legs ran, and an artifact changed then would end a finished run in a typed stop. Neither parent
    # did that: the receipt guard keeps its location and manifest checks, and the artifacts are hashed only by the preflight, the claim guard and the post-open guard.
    def test_artifact_preflight_r12_the_receipt_guard_hashes_no_artifact(self) -> None:
        self._fresh_run_dir()
        state = self.share / "fake-agent-state.json"
        artifact_calls = self.case / "r12-artifact-calls.txt"
        manifest_probe, artifact_probe = self._timed_probes(0, 0)
        artifact_probe.write_text("param($items)\n" + f"$w = $(if (Test-Path -LiteralPath '{state}' -PathType Leaf) {{ 'open' }} else {{ 'before-open' }})\n"
                                  + f"[IO.File]::AppendAllText('{artifact_calls}', $w + [Environment]::NewLine)\n", encoding="utf-8")
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]),
                                "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = artifact_calls.read_text(encoding="utf-8").split() if artifact_calls.exists() else []
        # the preflight and the claim guard (before the open), then the post-open guard: nothing at the receipts
        self.assertEqual(calls, ["before-open", "before-open", "open"], r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsRan"]), ("ALL_LEGS_DONE", 1))

    # r5 ITEM 2 (sol r4 B1; key3 r4 H-B): once Invoke-InputGuard gave up on a location read (locationUnknown), Test-OpenOutputAlias re-resolved -RunDir (Get-PhysicalPath) and read
    # its identity on the main thread, unbounded, and the claim-time refusal probed batch.log: a -RunDir that stopped answering (a UNC share, a junction whose target went away) then
    # held the run before the typed refusal and the lease close. '<guard>:rundir=<sec>' stalls every read of -RunDir in that guard and after it; GUARD_READ_DELAY_SEAM on stderr names
    # each main-thread read that reached the stall.
    # LEASE-BATCH-PREFLIGHT-TEST-ISOLATION-1 item 3: renamed from ..._reaches_the_typed_refusal_and_the_close_inside_the_room. No close job is submitted here (JOB_WRITE_FAILED), so
    # the measurement is the run's own end (the observed process exit), which proves termination and the claim release, not a close.
    def test_artifact_preflight_r5_item2_a_stalled_rundir_after_the_open_reaches_the_typed_refusal_and_the_run_ends_with_the_claim_released_inside_the_room(self) -> None:
        # sol's repro: cap 75, leg budget 60, margin 0 (the post-open guard has 14 s); -RunDir's metadata reads stall 40 s once the open is answered
        r, ended_after_open = self._post_open_hash_run(0, "-GuardReadDelay", "open:rundir=40", cap="75", NO_CLOSE_EXPECTED="1")
        self.assertTrue(self.close_substituted, "a close was submitted: this row measures the run's end only when none was")
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, "a -RunDir read ran on the main thread after the guard gave up on that location")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        # the 14 s room and the documented 2 s bounded re-read of -RunDir (adversary r5 A1), never the 40 s stall: the run ENDED inside that (termination; no close was submitted)
        self.assertLess(ended_after_open, 75 - 60 + 4 + 2, f"the run ended {ended_after_open:.1f} s after the open (the observed process exit stands in for a close that was never submitted): the stalled -RunDir was waited for")
        self.assertFalse(self.claim_path.exists(), "the claim file was removed (claim cleanup; says nothing of the lease's physical release)")
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        # r7 (sol r6 B2): a -RunDir re-read that does not answer leaves its location unknown, never the preflight's: the redirect fails closed and the close job is typed unwritable
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, "the redirect was made against -RunDir's preflight location")
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', r.stderr)

    def test_artifact_preflight_r5_item2_a_stalled_rundir_in_the_claim_guard_refuses_without_probing_batch_log(self) -> None:
        # the claim-time sibling (sol r4 B1, Stop-PlanRefused): -ClaimTtlSec 60 / -ClaimRenewSec 30 gives the claim guard 30 s; -RunDir stalls 40 s
        t0 = time.monotonic()
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "30", "-GuardReadDelay", "claim:rundir=40")
        took = time.monotonic() - t0
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, "batch.log was probed after the guard gave up on -RunDir's location")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        self.assertLess(took, 40, f"the run took {took:.1f} s: the stalled -RunDir was read again after the 30 s guard")

    # r5 adversary A1 (pass 1 on 8559ef31): the location-unknown redirect check compared TEMP with -RunDir's preflight location only, so a -RunDir junction re-pointed AT TEMP before
    # the open returned got its close redirect (and the close job) made inside the current -RunDir. -RunDir is now re-read on a pool thread bounded at 2 s.
    def test_artifact_preflight_r5_adv_a1_a_rundir_junction_repointed_at_temp_gets_no_close_redirect_inside_it(self) -> None:
        old, new, link = self.case / "rd-old", self.case / "rd-new", self.case / "run-link"
        old.mkdir(parents=True, exist_ok=True)
        new.mkdir(parents=True, exist_ok=True)
        self._junction(link, old)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        self.run_dir = link
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "75", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                "-GuardReadDelay", "open:identity=25", FAKE_OPEN_SLEEP="6", TEMP=str(new), TMP=str(new))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            # r15 (hosted CI 40963950528): the open's job file is written into -RunDir\batch-jobs AFTER the read-back line, so a repoint made at that line raced it and, on a slow
            # runner, the job write met the new target first (OUTPUT_ZONE_UNRESOLVED, the open never submitted). The repoint lands inside the open's wait, as the case means.
            self._wait_for_open_wait()
            os.rmdir(link)                 # the junction itself, not its target
            self._junction(link, new)      # -RunDir now IS TEMP
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        made = [x.name for x in new.iterdir() if x.name.startswith("venue-lease-batch-close-")]
        self.assertEqual(made, [], f"the close redirect was made inside the current -RunDir: {made}\n{se}")
        self.assertIn("an input whose location is unknown is refused", se)
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", se)
        self.assertIn("is inside -RunDir", se)

    # r7 ITEM 2 (sol r6 B2 = adversary r6 A1 = fable/key3 r6 H1; new catalogue row): the same repoint, but the bounded re-read of -RunDir times out (open:rundir=40). The cleanup fell
    # back to where the PREFLIGHT found -RunDir (rd-old), TEMP (rd-new) was outside that, and the close redirect was MADE inside the current -RunDir while the run said it wrote nothing
    # more there. A re-read that does not answer leaves the location unknown: the redirect fails closed (FAILED, no job folder, the close typed JOB_WRITE_FAILED).
    def _repointed_rundir_run(self, guard_delay: str, repoint: bool) -> tuple[str, str, Path, int]:
        old, new, link = self.case / "rd-old", self.case / "rd-new", self.case / "run-link"
        old.mkdir(parents=True, exist_ok=True)
        new.mkdir(parents=True, exist_ok=True)
        self._junction(link, old)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        self.run_dir = link
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "75", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                "-GuardReadDelay", guard_delay, FAKE_OPEN_SLEEP="6", TEMP=str(new), TMP=str(new))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            # r15: inside the open's wait, after its job file is written (see the r5 adversary A1 case above)
            self._wait_for_open_wait()
            if repoint:
                os.rmdir(link)                 # the junction itself, not its target
                self._junction(link, new)      # -RunDir now IS TEMP
            try:
                so, se = p.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                so, se = p.communicate(timeout=60)
                self.fail(f"the run did not end in 120 s:\n{so}\n{se}")
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        return so, se, new, p.returncode

    def test_artifact_preflight_r7_b2_a_repointed_rundir_whose_reread_times_out_gets_no_close_redirect_inside_it(self) -> None:
        so, se, new, code = self._repointed_rundir_run("open:rundir=40", repoint=True)
        made = [x.name for x in new.iterdir() if x.name.startswith("venue-lease-batch-close-")]
        self.assertEqual(made, [], f"the close redirect was made inside the current -RunDir: {made}\n{se}")
        self.assertEqual(code, 2, so + se)
        self.assertIn("an input whose location is unknown is refused", se)
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", se)
        self.assertIn("did not answer within 2 s", se)
        res = batch_result(so)
        self.assertEqual(res["outputRedirect"]["state"], "FAILED", res)
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', se, "the failed redirect leaves the close job unwritable, typed")
        self.assertEqual(self.leg_calls(), [])

    def test_artifact_preflight_r7_b2_control_a_rundir_reread_that_answers_still_gets_the_redirect_in_an_independent_temp(self) -> None:
        # the guard gives up on a location (its identity reads stall 25 s in a 14 s room), but -RunDir's own re-read answers: TEMP (rd-new) is not -RunDir (rd-old), so the redirect is made
        so, se, new, code = self._repointed_rundir_run("open:identity=25", repoint=False)
        self.assertEqual(code, 2, so + se)
        self.assertIn("an input whose location is unknown is refused", se)
        self.assertNotIn("BATCH_OUTPUT_REDIRECT_FAILED", se, se)
        self.assertEqual(len([x for x in new.iterdir() if x.name.startswith("venue-lease-batch-close-")]), 1, se)
        self.assertEqual(batch_result(so)["outputRedirect"]["state"], "MADE")

    # r5 adversary pass 2 A2 (on 4a8e2e87): a -RunDir junction removed after the open was submitted made the worker's resolve FAIL (not time out), so the hit was a known location
    # hit and Test-OpenOutputAlias read -RunDir again on the main thread, unbounded. A location that cannot be resolved under a guard budget is a location left unknown.
    def test_artifact_preflight_r5_adv_p2_a2_a_rundir_junction_removed_after_the_open_is_not_read_again_on_the_main_thread(self) -> None:
        old, link = self.case / "rd-old", self.case / "run-link"
        self._junction(link, old)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        self.run_dir = link
        # r8: the open blocks until the junction is removed (FAKE_OPEN_RELEASE), not for a fixed 6 s; the 3 s -GuardReadDelay is only what a main-thread -RunDir read would stall on
        # (GUARD_READ_DELAY_SEAM on stderr says it did), so it does not have to outlast anything
        release = self._open_release()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "75", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                "-GuardReadDelay", "open:rundir=3", FAKE_OPEN_SLEEP="60", FAKE_OPEN_RELEASE=str(release))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()
            os.rmdir(link)   # the junction itself; its target stays
            release.write_text("go", encoding="utf-8")
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertNotIn("GUARD_READ_DELAY_SEAM", se, "-RunDir was read again on the main thread after the guard could not resolve it")
        self.assertEqual(p.returncode, 2, so + se)
        # r12 (#704 merge, RPF-06): -RunDir's zone no longer resolves while the plan itself opens, so the hit is typed OUTPUT_ZONE_UNRESOLVED (no longer "cannot be resolved to a
        # physical path"), and under the guard budget it is still a location left unknown (no main-thread re-read, asserted above)
        self.assertIn("LEG_PLAN_REFUSED reason=OUTPUT_ZONE_UNRESOLVED ", se)
        self.assertIn("an output location could not be resolved", se)
        self.assertIn("BATCH_OUTPUT_ALIASED", se)
        self.assertEqual(self.leg_calls(), [])

    # r6 (sol r5 B1 = key3 r5 B1; new catalogue row: TEMP reaches a -RunDir that stalls after the open): after a location-unknown guard result -RunDir was re-read bounded, but TEMP
    # was not. The pair list resolved TEMP (CreateFileW) before the spelling check that rejects a nested TEMP at once, and the identity walk from TEMP up ran on the main thread too,
    # so a TEMP inside the stalled -RunDir held the typed refusal and the close. '<guard>:rundir=<sec>' now stalls every TEMP read that reaches -RunDir (TEMP_READ_DELAY_SEAM on
    # stderr says it was armed). RED on 52bd3050's logic with that seam: the 40 s stall is waited for before the close.
    def _stalled_rundir_temp_run(self, temp: Path, expect: str, armed: bool = True) -> None:
        r, ended_after_open = self._post_open_hash_run(0, "-GuardReadDelay", "open:rundir=40", cap="75", TEMP=str(temp), TMP=str(temp), NO_CLOSE_EXPECTED="1")
        self.assertTrue(self.close_substituted, "a close was submitted: this row measures the run's end only when none was")
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, "a -RunDir read ran on the main thread after the guard gave up on that location")
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', r.stderr, "the failed redirect leaves the close job unwritable, typed")
        if armed:
            self.assertIn("TEMP_READ_DELAY_SEAM", r.stderr, "the seam never stalled a TEMP read: this row would pass vacuously")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        # the 14 s room and the 2 s bound on the -RunDir re-read and the TEMP walk (raced, one bound), never the 40 s stall: the run ENDED inside that (termination; no close was submitted)
        self.assertLess(ended_after_open, 75 - 60 + 4 + 2, f"the run ended {ended_after_open:.1f} s after the open (the observed process exit stands in for a close that was never submitted): a TEMP read that reached the stalled -RunDir was waited for")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, "the redirect fails closed")
        self.assertIn(expect, r.stderr)
        self.assertEqual([str(x) for x in self.run_dir.rglob("venue-lease-batch-close-*")], [], "a close redirect was made inside -RunDir")
        self.assertFalse(self.claim_path.exists(), "the claim file was removed (claim cleanup; says nothing of the lease's physical release)")

    def test_artifact_preflight_r6_b1_a_temp_nested_in_a_stalled_rundir_fails_the_redirect_closed_by_spelling_before_any_temp_read(self) -> None:
        temp = self.run_dir / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        # spelling alone rejects it now, before any TEMP read, so the seam is never reached on the fixed code
        self._stalled_rundir_temp_run(temp, "is inside -RunDir", armed=False)

    def test_artifact_preflight_r6_b1_a_temp_junction_into_a_stalled_rundir_fails_the_redirect_closed_within_2s(self) -> None:
        alias = self.case / "r6-temp-junction"
        self._junction(alias, self.run_dir / "tmp")
        self.addCleanup(lambda: os.path.lexists(alias) and os.rmdir(alias))
        self._stalled_rundir_temp_run(alias, "did not answer within 2 s")

    def test_artifact_preflight_r6_b1_a_temp_whose_identity_read_stalls_fails_the_redirect_closed_within_2s(self) -> None:
        # TEMP's final path answers and is not -RunDir's (\\localhost\C$\<RunDir>\tmp); the identity read of the folder that IS -RunDir stalls with it
        (self.run_dir / "tmp").mkdir(parents=True, exist_ok=True)
        self._stalled_rundir_temp_run(self._loopback_unc(self.run_dir) / "tmp", "did not answer within 2 s")

    def test_artifact_preflight_r6_b1_control_an_independent_answering_temp_still_gets_the_redirect_fast(self) -> None:
        # sol r5 INV-4 row 1: TEMP elsewhere and answering gets the redirect. r7 (sol r6 B2): only when -RunDir's own re-read answers too (here the guard gives up on its identity
        # reads, open:identity=25 in the 14 s room); a -RunDir re-read that times out is no longer replaced by the preflight's location (the r7_b2 rows)
        temp = self.case / "r6-temp-elsewhere"
        temp.mkdir(parents=True, exist_ok=True)
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:identity=25", cap="75", TEMP=str(temp), TMP=str(temp))
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("TEMP_READ_DELAY_SEAM", r.stderr)
        self.assertNotIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, r.stderr)
        self.assertEqual(len(list(temp.glob("venue-lease-batch-close-*"))), 1, r.stderr)
        self.assertLess(held, 75 - 60 + 4 + 2, f"the lease was held {held:.1f} s before the close")

    # LEASE-BATCH-PREFLIGHT-TEST-ISOLATION-1 item 1 (Dot on #705, ledger 2026-10-28T13:07:37Z): the r6_b1 rows above stall -RunDir AND TEMP together. '<guard>:rundir=<sec>' is the one
    # seam that stalls TEMP's walk, and only because TEMP reaches -RunDir, while the redirect gives up on its 2 s re-read of -RunDir BEFORE it awaits TEMP's walk (Finish). The
    # "did not answer within 2 s" those rows assert is therefore -RunDir's own throw, and the Finish branch that throws for TEMP is never reached by them. These rows keep -RunDir
    # responsive and current (the guard gives up on a location through open:identity=25, a stall that is not on the main thread, as the r6_b1 control does) and stall ONLY TEMP's walk:
    # a patched mirror of the runner sets $tempSeam itself, because the runner's seam cannot name TEMP alone. The no-stall mirror is the control for the harness.
    # (The responsive -RunDir controls with an independent TEMP already exist: r6_b1_control_an_independent_answering_temp_... and r7_b2_control_a_rundir_reread_that_answers_...)
    TEMP_SEAM_ANCHOR = "$tempSeam = @(0, 0)"
    TEMP_WALK_REFUSAL_TAIL = "once the guard gave up on a location: it is not known to be outside -RunDir"
    RUNDIR_REREAD_REFUSAL_TAIL = "once the guard gave up on a location: where it is now is unknown, so TEMP is not known to be outside it"

    def _responsive_rundir_temp_run(self, resolve_ms: int, identity_ms: int, stalled: bool, extra_patches: tuple[tuple[str, str], ...] = ()) -> tuple[subprocess.CompletedProcess, float, Path]:
        temp = self.case / "iso-temp-elsewhere"
        temp.mkdir(parents=True, exist_ok=True)
        self._mirror_with_bad_sidecar(runner_patches=((self.TEMP_SEAM_ANCHOR, f"$tempSeam = @({resolve_ms}, {identity_ms})"), *extra_patches))
        self.plan_runner = self.runner
        env = {"NO_CLOSE_EXPECTED": "1"} if stalled else {}
        r, measured = self._post_open_hash_run(0, "-GuardReadDelay", "open:identity=25", cap="75", TEMP=str(temp), TMP=str(temp), **env)
        # the stall is the injected TEMP-only one: -RunDir answered, so neither of the runner's own '<guard>:rundir' seams was armed
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, r.stderr)
        self.assertNotIn("TEMP_READ_DELAY_SEAM", r.stderr, r.stderr)
        self.assertNotIn(self.RUNDIR_REREAD_REFUSAL_TAIL, r.stderr, "-RunDir's own re-read did not answer: this row would not reach TEMP's walk")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "the claim file was removed (claim cleanup; says nothing of the lease's physical release)")
        return r, measured, temp

    def _assert_temp_walk_stalled(self, resolve_ms: int, identity_ms: int) -> None:
        r, ended_after_open, temp = self._responsive_rundir_temp_run(resolve_ms, identity_ms, stalled=True)
        self.assertTrue(self.close_substituted, "a close was submitted: this row measures the run's end only when none was")
        # TEMP's own branch: the redirect fails closed with TEMP's message, not -RunDir's
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr)
        self.assertIn(self.TEMP_WALK_REFUSAL_TAIL, r.stderr, "the redirect did not fail closed on TEMP's own walk")
        self.assertEqual(batch_result(r.stdout)["outputRedirect"]["state"], "FAILED", r.stdout)
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', r.stderr, "the failed redirect leaves the close job unwritable, typed")
        self.assertEqual(list(temp.glob("venue-lease-batch-close-*")), [], "a close redirect was made in a TEMP that did not answer")
        self.assertEqual([str(x) for x in self.run_dir.rglob("venue-lease-batch-close-*")], [], "a close redirect was made inside -RunDir")
        # the 14 s room and TEMP's 2 s bound, never the 40 s stall: the run ENDED inside that (termination; no close was submitted)
        self.assertLess(ended_after_open, 75 - 60 + 4 + 2, f"the run ended {ended_after_open:.1f} s after the open (the observed process exit stands in for a close that was never submitted): TEMP's stalled walk was waited for")

    def test_artifact_preflight_claim_wait_dot_i1_the_measured_end_is_the_process_exit_not_the_summary_stamp(self) -> None:
        # Dot I1 on #750: finishedUtc is stamped when the summary is built, before Write-RunSummary, Complete-VenueProcess and exit, so a stall after it passed the bound. A mirror stalls
        # 8 s between the summary and the exit: the measured end must lie past the stamp by that much.
        exit_line = "Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned"
        r, ended_after_open, _ = self._responsive_rundir_temp_run(40000, 0, stalled=True, extra_patches=((exit_line, "Start-Sleep -Seconds 8; " + exit_line),))
        self.assertTrue(self.close_substituted, r.stdout + r.stderr)
        self.assertGreaterEqual(ended_after_open - self.summary_after_open, 8, f"the process exit {ended_after_open:.1f} s after the open is not past the summary stamp {self.summary_after_open:.1f} s by the 8 s stall")

    def test_artifact_preflight_iso1_item1_control_a_responsive_rundir_and_an_unstalled_temp_walk_on_the_patched_mirror_still_gets_the_redirect(self) -> None:
        r, held, temp = self._responsive_rundir_temp_run(0, 0, stalled=False)
        self.assertFalse(self.close_substituted, "the control ran no close job")
        self.assertNotIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, r.stderr)
        self.assertNotIn(self.TEMP_WALK_REFUSAL_TAIL, r.stderr)
        self.assertEqual(len(list(temp.glob("venue-lease-batch-close-*"))), 1, r.stderr)
        self.assertEqual(batch_result(r.stdout)["outputRedirect"]["state"], "MADE", r.stdout)
        self.assertLess(held, 75 - 60 + 4 + 2, f"the lease was held {held:.1f} s before the close")

    def test_artifact_preflight_iso1_item1_a_temp_whose_final_path_resolve_alone_stalls_fails_the_redirect_closed_on_temps_own_walk(self) -> None:
        self._assert_temp_walk_stalled(40000, 0)

    def test_artifact_preflight_iso1_item1_a_temp_whose_identity_check_alone_stalls_fails_the_redirect_closed_on_temps_own_walk(self) -> None:
        self._assert_temp_walk_stalled(0, 40000)

    # r5 adversary A2 (pass 1 on 8559ef31): no content read starts with under 2 s of the guard's room, but Get-VenueStagedBuildManifest started the hash with 1 s left of its call.
    # cap 66 / leg 60 / margin 0 leaves the post-open guard about 5 s (one build: a 4 s read); the manifest read takes 2.2 s once the open is answered, so the hash would start with 1.8 s.
    # r7 (sol r6 B1): the share is no longer floored (4.x s, not 4), so the read takes 3 s to leave the hash under 2 s, as 2.2 s did of a floored 4
    def test_artifact_preflight_r5_adv_a2_no_artifact_hash_starts_with_under_2s_of_the_guard_room(self) -> None:
        state = self.share / "fake-agent-state.json"
        manifest_probe, _ = self._timed_probes(3000, 0, when=f"(Test-Path -LiteralPath '{state}' -PathType Leaf)")
        r, held = self._post_open_hash_run(0, "-StagedBuildProbeScript", str(manifest_probe), cap="66")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("no time is left of -TimeoutSec", r.stderr)
        self.assertEqual(self.leg_calls(), [])

    # r5 adversary A3 (pass 1 on 8559ef31): each build got floor(room / builds), so N healthy fast builds in a room under 2N s were refused before any read and the opened lease charged
    # with no leg. Two builds, cap 65 / leg 60 / margin 0: about 4 s of room, 1 s each on 8559ef31; the real reads take well under a second.
    def test_artifact_preflight_r5_adv_a3_two_fast_builds_in_a_4s_room_are_read_not_refused(self) -> None:
        items = [self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b)]
        r, held = self._post_open_hash_run(0, cap="65", items=items)
        self.assertNotIn("to read and hash the staged build", r.stdout + r.stderr, "a build was refused for its split of the room before any read")
        self.assertNotIn("LEG_PLAN_REFUSED", r.stderr)
        self.assertGreaterEqual(len(self.leg_calls()), 1, r.stdout + r.stderr)

    # r7 ITEM 1 (sol r6 B1 = adversary r6 A2; new catalogue row): the guard floored its room to whole seconds (2.8 -> 2) and Get-VenueStagedBuildManifest floored the remainder after
    # the manifest read again (1.87 -> 1), under its -ArtifactMinSec 2: a room of 2.8 or 2.99 s, a 100 ms manifest read and a 100 ms matching hash were refused SHARE_UNREACHABLE
    # with no hash (after the open, a lease closed and charged with no leg). Run on the runner's own functions (New-GuardBudget, Get-InputContentRefusal, lifted from its AST) with
    # offline probes; each manifest read and each hash appends a line.
    def _guard_room_run(self, room: float, manifest_ms: int = 100, hash_ms: int = 100, utc_step: float = 0) -> dict:
        c = "3" * 40
        reads, hashes = self.case / f"room-{room}-{manifest_ms}-{utc_step}-reads.txt", self.case / f"room-{room}-{manifest_ms}-{utc_step}-hashes.txt"
        bounds = self.case / f"room-{room}-{manifest_ms}-{utc_step}-bounds.txt"
        facts = ("('a' * 64); 'C:" + c + "'; 'D:' + ('f' * 64); 'S:bool'; "
                 "'A:packageZip=ok'; 'H:packageZip=' + ('1' * 64); 'A:exe=ok'; 'H:exe=' + ('2' * 64); 'A:dll=ok'; 'H:dll=' + ('3' * 64)")
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)\n"
                  # r6 (adversary r6 A3): Get-InputContentRefusal calls Set-GuardProgress (a no-op with no progress object), so the harness lifts it too, never a CommandNotFound it ignores
                  "foreach ($n in 'New-GuardBudget', 'Get-InputContentRefusal', 'Set-GuardProgress') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
                  # the runner's own top-level threshold, when it has one
                  "$min = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] -and $x.Left.Extent.Text -ceq '$script:GuardReadMinSec' }, $true)\n"
                  "if ($min) { . ([scriptblock]::Create($min.Extent.Text)) }\n"
                  f"$legPlanMode = $true; $AgentShare = '{self.share}'; $script:guardProgress = $null\n"
                  f"$script:guardedInputs = @([pscustomobject]@{{ kind = 'build'; item = 1; path = 'manifest'; commit = '{c}'; sha256 = ('a' * 64) }})\n"
                  f"$stagedBuildProbe = {{ param($p) [IO.File]::AppendAllText('{reads}', 'm' + [Environment]::NewLine); [Threading.Thread]::Sleep({manifest_ms}); {facts} }}\n"
                  f"$artifactProbe = {{ param($i) [IO.File]::AppendAllText('{hashes}', 'h' + [Environment]::NewLine); [IO.File]::AppendAllText('{bounds}', ([string]($i[0].endSec - $i[0].clock.Elapsed.TotalSeconds) + [Environment]::NewLine)); [Threading.Thread]::Sleep({hash_ms}) }}\n"
                  # r11 (hosted CI 40805584834): the room is measured on the real clock, and Invoke-VenueBoundedRead starts a runspace per read -- a cost the injected probes do not
                  # control (warming the first one, adversary r7 A3, left every later start). A 2.3 s room holds only 0.3 s of it above the hash's 2 s minimum, and a loaded hosted runner
                  # spent 190 ms there ("1.99 s left, under 2"). The room tests pin the BUDGET ARITHMETIC, so the reads run in-process here (the same script text, argument and result
                  # shape; the runspace itself and its timeout are pinned by the bounded-read tests) and the only time a read takes is the probe's own sleep.
                  "& (Get-Module VenueChain) { function script:Invoke-VenueBoundedRead { param([string]$ScriptText, $Argument, [double]$TimeoutSec) "
                  "[pscustomobject]@{ answered = $true; value = @(& ([scriptblock]::Create($ScriptText)) $Argument); error = $null } } }\n"
                  # r15 (hosted CI 40963950528: "bound 1.91397" in a 2.3 s room; reproduced on Virtual-Ten as "2.25 s share ... 1.93 s left"): the first pass through the guard, the
                  # module and the probes pays PowerShell's one-time parse and compile cost, up to 0.3 s on a loaded host, on the room's real clock. One identical pass in a 30 s room
                  # first, its probe lines discarded, so the measured pass is charged only what the probes sleep and the budget arithmetic is what the room tests pin.
                  "$script:guardBudget = New-GuardBudget 30 'the warm-up room' 'LEG_BUDGET_EXCEEDS_LEASE' $false 'open'\n"
                  "$null = Get-InputContentRefusal\n"
                  f"foreach ($f in '{reads}', '{hashes}', '{bounds}') {{ if (Test-Path -LiteralPath $f) {{ Remove-Item -LiteralPath $f }} }}\n"
                  f"$script:guardBudget = New-GuardBudget {room} 'the test room' 'LEG_BUDGET_EXCEEDS_LEASE' $false 'open'\n"
                  # r22 (sol r21 blocker, adversary pass-23 A1): the wall clock stepped forward `utc_step` s after the measured pass's budget began (a NTP / VM-resume step); 0 = none
                  f"& (Get-Module VenueChain) {{ $script:VenueTestUtcStepSec = {utc_step} }}\n"
                  "$hit = Get-InputContentRefusal\n"
                  "ConvertTo-Json -Compress -InputObject @{ code = [string]$hit.code; why = [string]$hit.why; noAnswer = [bool]$hit.noAnswer; ms = [int]$script:guardBudget.clock.Elapsed.TotalMilliseconds }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        got["reads"] = len(reads.read_text(encoding="utf-8").split()) if reads.exists() else 0
        got["hashes"] = len(hashes.read_text(encoding="utf-8").split()) if hashes.exists() else 0
        # the bound the (first) hash was given, on the call's own clock: what was left of the room after the manifest read
        given = bounds.read_text(encoding="utf-8").split() if bounds.exists() else []
        got["bound"] = float(given[0]) if given else None
        return got

    def test_artifact_preflight_r7_b1_a_healthy_build_in_a_2_8s_guard_room_is_validated_with_one_hash(self) -> None:
        got = self._guard_room_run(2.8)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), f"a 2.8 s room, a 100 ms read and a 100 ms hash: {got}")

    def test_artifact_preflight_r7_b1_a_healthy_build_in_a_2_99s_guard_room_is_validated_with_one_hash(self) -> None:
        got = self._guard_room_run(2.99)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), f"a 2.99 s room, a 100 ms read and a 100 ms hash: {got}")

    def test_artifact_preflight_r7_b1_control_a_3_1s_guard_room_is_validated_with_one_hash(self) -> None:
        got = self._guard_room_run(3.1)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), got)

    def test_artifact_preflight_r7_b1_control_a_room_under_the_minimum_is_refused_typed_before_any_read(self) -> None:
        got = self._guard_room_run(1.9)
        self.assertEqual((got["code"], got["reads"], got["hashes"]), ("LEG_BUDGET_EXCEEDS_LEASE", 0, 0), got)
        self.assertIn("no time is left in the test room", got["why"])

    # r22 (sol r21 blocker = producer adversary pass-23 A1, VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1): the guard's share was a REMAINING MONOTONIC budget turned into a UTC instant (Get-InputContentRefusal)
    # and back into a duration by the module's own second UtcNow read, so a forward wall-clock step between the two reads (NTP, a VM resume) read as the share spent: the cap clamped to 0.001 s,
    # the read refused without reading and the caller ended SHARE_UNREACHABLE with noAnswer = $false, skipping the no-claim retry, with about 29.8 monotonic seconds left of a 30 s guard.
    # The budget now goes over as the caller's own Stopwatch and its end on it (-CallBudgetClock/-CallBudgetEndSec), so no wall-clock read sits between the two monotonic clocks.
    # RED on a8e76648 (seam added, code unchanged): code SHARE_UNREACHABLE, noAnswer False, 0 reads, 0 hashes.
    def test_r22_a1_a_60s_forward_wall_clock_step_never_exhausts_a_30s_guard_share(self) -> None:
        got = self._guard_room_run(30, utc_step=60)
        self.assertEqual((got["code"], got["reads"], got["hashes"]), ("", 1, 1), f"a 60 s UTC step read as the 30 s share spent: {got}")
        self.assertFalse(got["noAnswer"], got)

    def test_r22_a1_control_no_step_validates_the_same_build(self) -> None:
        got = self._guard_room_run(30, utc_step=0)
        self.assertEqual((got["code"], got["reads"], got["hashes"]), ("", 1, 1), got)

    def test_r22_a1_control_a_backward_wall_clock_step_never_lengthens_the_share_past_the_budget(self) -> None:
        # a backward step used to ADD its seconds to the cap (an instant 60 s earlier is 60 s more room); the budget's own end is what the call is held to now
        got = self._guard_room_run(2.3, utc_step=-60)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), got)
        self.assertLessEqual(got["bound"], 2.3, f"the hash was given more than the guard's whole room: {got}")

    def test_r22_a1_control_a_spent_budget_stays_the_typed_refusal_and_the_nightlys_utc_deadline_still_feels_a_step(self) -> None:
        # the real budget spent (the caller's clock is past its end): UNREACHABLE with no read, as before. And -CallDeadlineUtc (the nightly's, a UTC deadline end to end) is still
        # stepped by the seam, so the seam is live and the lease batch's immunity above is the budget's own doing, not a dead seam.
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  "$probe = { param($p) ('a' * 64) }\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew(); [Threading.Thread]::Sleep(700)\n"
                  f"$spent = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{self.commit_a}' -TimeoutSec 30 -ProbeScript $probe -CallBudgetClock $sw -CallBudgetEndSec 0.2\n"
                  f"$room = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{self.commit_a}' -TimeoutSec 30 -ProbeScript $probe -CallBudgetClock $sw -CallBudgetEndSec ($sw.Elapsed.TotalSeconds + 30)\n"
                  "& (Get-Module VenueChain) { $script:VenueTestUtcStepSec = 60 }\n"
                  f"$nightly = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{self.commit_a}' -TimeoutSec 30 -ProbeScript $probe -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(30))\n"
                  f"$stepped = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{self.commit_a}' -TimeoutSec 30 -ProbeScript $probe -CallBudgetClock $sw -CallBudgetEndSec ($sw.Elapsed.TotalSeconds + 30)\n"
                  "ConvertTo-Json -Compress -InputObject @{ spent = $spent.state; spentError = [string]$spent.error; room = $room.state; nightly = $nightly.state; nightlyError = [string]$nightly.error; stepped = $stepped.state }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["spent"], "UNREACHABLE", got)
        self.assertIn("no time is left of -CallCapSec", got["spentError"], got)
        self.assertEqual(got["room"], "STAGED", got)
        self.assertEqual(got["stepped"], "STAGED", f"a UTC step reached a call that holds a monotonic budget: {got}")
        self.assertEqual(got["nightly"], "UNREACHABLE", f"the seam no longer steps -CallDeadlineUtc: {got}")
        self.assertIn("no time is left of -CallCapSec", got["nightlyError"], got)

    # r22 fix iteration 1 (adversary pass-24 A1 on e3e61e8e, VenueChain.psm1:659): the budget's remainder was taken on the CALLER's clock and held against the call's own clock, which had run
    # since its start, so a stall after that start and before the sample was charged twice: a 3 s stall under a 4 s end left a cap of 1 s with 3 s already on the call's clock, and a healthy
    # read was refused with 1 s truly left. RED on e3e61e8e (seam only): UNREACHABLE, no read, for both the budget and the UTC deadline. The stall INSIDE the bracket is taken again.
    def _r22_stall_run(self, seam: str, budget_args: str) -> dict:
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"& (Get-Module VenueChain) {{ {seam} }}\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{self.commit_a}' -TimeoutSec 30 -ProbeScript {{ param($p) ('a' * 64) }} {budget_args}\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; error = [string]$r.error; sec = $sw.Elapsed.TotalSeconds }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_r22_a1_fix1_a_stall_after_the_call_clock_starts_is_charged_once_to_the_budget(self) -> None:
        got = self._r22_stall_run("$script:VenueTestPostClockStartDelayMs = 3000", "-CallBudgetClock $sw -CallBudgetEndSec 6.0")
        self.assertEqual(got["state"], "STAGED", f"a 3 s stall under a 6 s budget end (3 s truly left) was charged twice: {got}")

    def test_r22_a1_fix1_a_stall_after_the_call_clock_starts_is_charged_once_to_a_utc_deadline(self) -> None:
        got = self._r22_stall_run("$script:VenueTestPostClockStartDelayMs = 3000", "-CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(6))")
        self.assertEqual(got["state"], "STAGED", f"a 3 s stall under a 6 s deadline (3 s truly left) was charged twice: {got}")

    def test_r22_a1_fix1_a_stall_between_the_two_samples_is_sampled_again_for_the_budget(self) -> None:
        got = self._r22_stall_run("$script:VenueTestDeadlineSampleDelayMs = 3000", "-CallBudgetClock $sw -CallBudgetEndSec 6.0")
        self.assertEqual(got["state"], "STAGED", f"a 3 s stall between the call clock read and the budget read (3 s truly left) refused the read: {got}")

    def test_r22_a1_fix1_a_stall_between_the_two_samples_is_sampled_again_for_a_utc_deadline(self) -> None:
        got = self._r22_stall_run("$script:VenueTestDeadlineSampleDelayMs = 3000", "-CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(6))")
        self.assertEqual(got["state"], "STAGED", f"a 3 s stall between the call clock read and the wall clock read (3 s truly left) refused the read: {got}")

    def test_r22_a1_fix1_control_a_stall_that_really_spends_the_budget_stays_the_typed_refusal(self) -> None:
        got = self._r22_stall_run("$script:VenueTestPostClockStartDelayMs = 3000", "-CallBudgetClock $sw -CallBudgetEndSec 2.0")
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertIn("no time is left of -CallCapSec", got["error"], got)

    def test_r22_a1_control_a_caller_stall_before_the_call_never_moves_the_budgets_end_later(self) -> None:
        # r12's property on the monotonic path: the end is fixed ON THE CALLER'S CLOCK, so a caller descheduled between its sample and the call (3 s here, under a 4 s end) leaves the
        # call what is left (about 1 s), never the whole share counted again from the call's own later start: the stuck 3 s read is given up on at the end, not waited for.
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew(); $end = 4.0; [Threading.Thread]::Sleep(3000)\n"
                  "$t = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{self.commit_a}' -TimeoutSec 20 -ProbeScript {{ param($p) [Threading.Thread]::Sleep(3000); ('a' * 64) }} -CallBudgetClock $sw -CallBudgetEndSec $end\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; sec = $t.Elapsed.TotalSeconds }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertLess(got["sec"], 1.6, f"the call ran past the caller's budget end: {got}")

    # r10 B1 (sol r9 = Dot cc5b2a97): `[Math]::Min(330, <double>)` takes the Int32 overload (an integer first operand), so a guard room of 2.3 / 2.4 / 2.49 s became a 2 s share, the hash was
    # left under its 2 s minimum after a 100 ms manifest read and a healthy staged build was refused SHARE_UNREACHABLE with no hash (after the open, a lease closed and charged with no leg).
    # A room of 2.8 rounded UP to 3 and hid it. The share keeps its fraction, and the hash is given what is left of the room, never more.
    def _assert_fractional_room_validates(self, room: float) -> None:
        got = self._guard_room_run(room)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), f"a {room} s room, a 100 ms read and a 100 ms hash: {got}")
        self.assertIsNotNone(got["bound"], got)
        self.assertGreaterEqual(got["bound"], 2.0, f"the hash started under its 2 s minimum: {got}")
        self.assertLessEqual(got["bound"], room - 0.1, f"the hash was given more than the room left after the 100 ms read: {got}")

    def test_artifact_preflight_r10_b1_a_healthy_build_in_a_2_3s_guard_room_is_validated_with_one_hash(self) -> None:
        self._assert_fractional_room_validates(2.3)

    def test_artifact_preflight_r10_b1_a_healthy_build_in_a_2_4s_guard_room_is_validated_with_one_hash(self) -> None:
        self._assert_fractional_room_validates(2.4)

    def test_artifact_preflight_r10_b1_a_healthy_build_in_a_2_49s_guard_room_is_validated_with_one_hash(self) -> None:
        self._assert_fractional_room_validates(2.49)

    def test_artifact_preflight_r10_b1_a_room_that_rounded_up_is_not_overshot_by_the_hash_bound(self) -> None:
        # the other direction of the same defect: 2.6 and 2.8 rounded UP to a 3 s share, and the hash was given more than the room held
        for room in (2.6, 2.8):
            got = self._guard_room_run(room)
            self.assertEqual((got["code"], got["hashes"]), ("", 1), got)
            self.assertLessEqual(got["bound"], room - 0.1, f"a {room} s room: the hash was given more than it held: {got}")

    def test_artifact_preflight_r10_b1_the_refusal_after_a_slow_read_names_the_fractional_bound_and_time_left(self) -> None:
        # room 2.4 admits the read (2 s minimum) but a 500 ms read leaves the hash about 1.9 s: refused typed, and the message carries the sub-second quantities it measured
        got = self._guard_room_run(2.4, manifest_ms=500)
        self.assertEqual((got["code"], got["hashes"]), ("SHARE_UNREACHABLE", 0), got)
        m = re.search(r"no time is left of -TimeoutSec (\d+(?:\.\d+)?) after the manifest read \((\d+(?:\.\d+)?) s left, under 2\)", got["why"])
        self.assertIsNotNone(m, got["why"])
        self.assertTrue(2.3 <= float(m.group(1)) <= 2.4, f"-TimeoutSec should name the 2.4 s room less the read start, not a rounded integer: {got['why']}")
        self.assertTrue(1.5 < float(m.group(2)) < 2.0 and "." in m.group(2), f"the time left is under 2 and fractional, never rounded up to '2' or down to '1': {got['why']}")

    def test_artifact_preflight_r10_b1_sibling_a_time_left_just_under_the_minimum_is_not_reported_as_the_minimum(self) -> None:
        # [Math]::Max(0, $left) with an integer first operand rounded 1.9 to "2 s left, under 2"; room 3.0 and a 1 s read leave the hash just under 2 s
        got = self._guard_room_run(3.0, manifest_ms=1000)
        self.assertEqual((got["code"], got["hashes"]), ("SHARE_UNREACHABLE", 0), got)
        m = re.search(r"\((\d+(?:\.\d+)?) s left, under 2\)", got["why"])
        self.assertIsNotNone(m, got["why"])
        self.assertLess(float(m.group(1)), 2.0, f"'{m.group(0)}' reports the time left rounded up to the minimum it is under: {got['why']}")

    def test_artifact_preflight_r7_b1_control_no_hash_starts_with_under_the_minimum_left_after_the_read(self) -> None:
        # the read is admitted (2.3 s left) but takes 600 ms: the hash would start with about 1.7 s, so none starts, and the call still ends inside the room
        got = self._guard_room_run(2.3, manifest_ms=600)
        self.assertEqual((got["code"], got["reads"], got["hashes"]), ("SHARE_UNREACHABLE", 1, 0), got)
        self.assertIn("no time is left of -TimeoutSec", got["why"])
        self.assertLess(got["ms"], 2300, got)

    # r7 adversary A2 (pass 1 on dbe8a7a0): -ArtifactMinSec was checked once, before the three-file hash worker, so after a slow packageZip the exe and the dll started with under
    # the minimum. The worker now checks it before EACH file. A hash bound (-ArtifactTimeoutSec 1) under the minimum (-ArtifactMinSec 2) starts no file at all; RED on dbe8a7a0,
    # which hashed all three (STAGED, no problem) inside that 1 s.
    def test_artifact_preflight_r7_adv_a2_no_artifact_file_starts_hashing_with_under_the_minimum_left(self) -> None:
        c = self.commit_a
        for k in ("packageZip", "exe", "dll"):
            self.assertTrue(self.artifact_path(c, k).exists(), k)
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{c}' -TimeoutSec 30 -VerifyArtifacts -ArtifactTimeoutSec 1 -ArtifactMinSec 2\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; error = [string]$r.error; problem = [string]$r.artifactProblem }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        # r12 (VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1, adversary A1 on 7754d16e): the hash's end is fixed once and what is left of it is taken again right before the launch, so a
        # bound under the minimum is refused there, before any worker starts (the per-file NOTIME check inside the worker stays, pinned by the r8b test above); no file is hashed either way
        self.assertRegex(got["error"], r"no time is left before the hash's end \([\d.]+ s into the call\) to start it \([\d.]+ s left, under 2\)")
        # control: the same build with room for every file is STAGED and matches
        script = script.replace(" -ArtifactTimeoutSec 1 -ArtifactMinSec 2", "")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((got["state"], got["problem"]), ("STAGED", ""), got)

    # r7 adversary A1 (pass 1 on dbe8a7a0): the guard capped a build's share at 300 s while the room was larger, so a 310 s post-open hash (one the preflight's 30 s + 300 s
    # bounds accept) in a 519 s room was refused SHARE_UNREACHABLE and the lease charged with no leg. The cap is now the preflight's own 330 s. The adversary's input, literally.
    # r8: the hash bound is read, not waited out. Invoke-VenueBoundedRead gives the post-open hash exactly (endSec - clock) seconds, so the probe records that bound at its start and
    # hashes at once: a 310 s hash is accepted iff the bound it was given is over 310 s. RED on a 300 s cap: the bound is at most 300 s and this fails, as the 310 s sleep did.
    def test_artifact_preflight_r7_adv_a1_a_310s_post_open_hash_in_a_519s_room_runs_the_leg(self) -> None:
        bounds = self.case / "post-open-hash-bounds.txt"
        bounds.unlink(missing_ok=True)
        record = f"[IO.File]::AppendAllText('{bounds}', ([string]($items[0].endSec - $items[0].clock.Elapsed.TotalSeconds) + [Environment]::NewLine))"
        r, held = self._post_open_hash_run(0, "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=900)), cap="1000", leg="360", margin="120", post_open=record)
        self.assertNotIn("LEG_PLAN_REFUSED", r.stderr, r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsRan"]), ("ALL_LEGS_DONE", 1), b)
        given = [float(x) for x in bounds.read_text(encoding="utf-8").split()]
        self.assertTrue(given, "the post-open hash probe ran")
        self.assertGreater(max(given), 310, f"the post-open hash was given at most {max(given):.1f} s: a 310 s hash the preflight accepts would be refused")
        self.assertLessEqual(max(given), 330, f"the post-open hash was given {max(given):.1f} s: over the preflight's own 330 s bounds")

    def test_artifact_preflight_r7_adv_a1_the_guard_share_cap_is_the_preflights_own_bounds(self) -> None:
        # r10: the cap is pinned by its VALUE and its type, not by one spelling: `Min(330, ...)` with an integer first operand was the r9 B1 defect (Int32 overload), and any double
        # spelling of the same cap (330.0, [double]330, 330.00) is the contract
        text = RUNNER.read_text(encoding="utf-8")
        guard = text[text.index("function Get-InputContentRefusal"):text.index("function Stop-InputIsOutput")]
        # r26 (CI heavy 1/11, run 43883120765): since r22/r23 the budget's elapsed time is sampled ONCE, in the same guarded statement, into a variable the share and the share's end
        # (`$shareEndSec = $shareAtSec + ...`) both read, so the two cannot disagree: `$share = Min(<cap>, <budget> - <that one sample>)`. The share is still not a second clock read.
        m = re.search(r"if \(\$null -ne \$script:guardBudget\) \{\s*\$(?P<at>\w+) = \$script:guardBudget\.clock\.Elapsed\.TotalSeconds\s*;\s*"
                      r"\$share = \[Math\]::Min\((?P<dbl>\[double\]\s*)?(?P<cap>\d+(?:\.\d+)?)\s*,\s*\$script:guardBudget\.sec\s*-\s*\$(?P=at)\)", guard)
        self.assertIsNotNone(m, "the guard's share is Min(<cap>, <budget> - <one sample of the budget's elapsed seconds>), sampled in the same `if ($null -ne $script:guardBudget) { ... }` block")
        self.assertEqual(float(m.group("cap")), 330.0, "the cap is the preflight's own 30 s + 300 s bounds")
        self.assertTrue(m.group("dbl") or "." in m.group("cap"), "the cap is a double: an integer first operand takes the Int32 overload and rounds the room (2.3 s -> 2)")
        # the one sample: assigned exactly once in the guard, from the budget's own Stopwatch, and the share's end reads that same variable (no second clock read for either)
        at = m.group("at")
        self.assertEqual(len(re.findall(r"\$" + at + r" = \$script:guardBudget\.clock\.Elapsed\.TotalSeconds", guard)), 1, "the elapsed sample is assigned once")
        self.assertRegex(guard, r"\$shareEndSec = \$" + at + r" \+ \[Math\]::Max\(0\.0, \$share\)", "the share's end is the same sample plus the share")
        self.assertIn("-TimeoutSec 30 -VerifyArtifacts -ArtifactTimeoutSec 300 }", text, "the preflight's bounds the cap is the sum of")

    def test_artifact_preflight_r10_no_dual_venue_math_min_or_max_rounds_a_double_through_an_integer_first_operand(self) -> None:
        # the class, not the :771 instance: every [Math]::Min/Max in the dual-venue folder whose second operand is a double (a TimeSpan total, a clock, a budget) has a double first operand too
        bad = []
        for path in sorted(DV.glob("*.ps*1")):
            for line, call in ps_integer_first_minmax(path.read_text(encoding="utf-8")):
                bad.append(f"{path.name}:{line}: {call}")
        self.assertEqual(bad, [], "make the first operand a double (330.0, [double]$x): an integer one selects the Int32 overload and rounds the double")

    def test_artifact_preflight_r10_adv_a3_the_windows_pwsh_skip_stays_on_the_runner_class_and_no_helper_carries_it(self) -> None:
        # a helper block inserted between `@requires_windows_pwsh` and its class moved the skip onto the helper's def (the helper then raised SkipTest, and the class ran unguarded
        # on a host with no pwsh). The decorator belongs on VenueLeaseBatchRunnerTests; the scanners above it are plain functions.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        decorated = {n.name: [ast.unparse(d) for d in n.decorator_list] for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
        self.assertIn("requires_windows_pwsh", decorated["VenueLeaseBatchRunnerTests"])
        self.assertEqual({name: d for name, d in decorated.items() if name.startswith(("ps_", "_ps_")) and d}, {})

    def test_artifact_preflight_r10_adv_a3_a_remainder_just_under_the_minimum_is_not_printed_as_the_minimum(self) -> None:
        # the module's own refusal line, run with a remainder of 1.999 s (Round(.., 2) printed "2 s left, under 2"); the line is lifted from the source, not re-spelled here
        line = next(l for l in MODULE.read_text(encoding="utf-8").splitlines() if "after the manifest read (" in l and "$left -lt $ArtifactMinSec" in l)
        script = ("$left = 1.999; $TimeoutSec = 3; $ArtifactMinSec = 2; $path = 'p'\n"
                  "$r = & { " + line.strip() + " }\n"
                  "ConvertTo-Json -Compress -InputObject @{ error = [string]$r.error }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        error = json.loads(out.stdout.strip().splitlines()[-1])["error"]
        m = re.search(r"\((\d+(?:\.\d+)?) s left, under 2\)", error)
        self.assertIsNotNone(m, error)
        self.assertLess(float(m.group(1)), 2.0, error)

    def test_artifact_preflight_r10_the_math_overload_scanner_sees_every_spelling(self) -> None:
        bad = [
            "[Math]::Min(330, $b.sec - $b.clock.Elapsed.TotalSeconds)",
            "[math]::MAX( 0 , $left )",
            "[System.Math]::Max(0, $left)",
            "[Math]::Min($Wait, ($d - [DateTime]::UtcNow).TotalSeconds)",
            "[int][Math]::Max(60, [Math]::Min($w, ($d - [DateTime]::UtcNow).TotalSeconds))",
            "$x = [Math]::Max(1000, ($deadline - [DateTime]::UtcNow).TotalMilliseconds + 10)",
            "[Math]::Min(2, [double]$remaining)",
        ]
        for text in bad:
            self.assertTrue(ps_integer_first_minmax(text), text)
        good = [
            "[Math]::Min(330.0, $b.sec - $b.clock.Elapsed.TotalSeconds)",
            "[Math]::Max(0.0, $left)",
            "[Math]::Min([double]$Wait, ($d - [DateTime]::UtcNow).TotalSeconds)",
            "[Math]::Min([double]330, $left)",
            "[Math]::Max($a.TotalSeconds, 0)",
            "[Math]::Min($LeaseCapSec, [Math]::Max(0, $bw.budgetRoomSec))",
            "[Math]::Min($ClaimPollSec, 5)",
            "[Math]::Min($Cap, [int][Math]::Floor(($d - [DateTime]::UtcNow).TotalSeconds))",
            "[Math]::Max(1, [Math]::Floor(($e - [DateTime]::UtcNow).TotalSeconds))",
            "# [Math]::Min(330, $b.sec - $b.clock.Elapsed.TotalSeconds) is the defect",
            "$x = 1 # [Math]::Max(0, $left)",
        ]
        for text in good:
            self.assertEqual(ps_integer_first_minmax(text), [], text)

    def test_artifact_preflight_r4_the_guard_read_delay_seam_is_refused_with_the_real_um_run_and_checked_for_shape(self) -> None:
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-GuardReadDelay", "open:spec=5")
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE -GuardReadDelay is an offline test seam", r.stderr)
        # r4 adversary A3: a non-ASCII decimal digit (U+0661) is refused typed, not accepted by \d and thrown on by [int]
        for bad in ("open:hash=5", "open:spec=١", "open:rundir=40x1"):
            r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-GuardReadDelay", bad)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("is not <claim|open>:<existence|identity|spec|rundir>=<sec>", r.stderr)
        self.assertEqual(self.kinds(), [])

    # r4 ITEM 3 (key3 r3 H3): the claim guard runs before the batch try, so a throw in it once the claim was granted left the claim held, with no keeper, until its TTL (and the run
    # ended exit 1). Input: a manifest read that emits A:<k>=ok without the paired H:<k>= digest lines, only inside the guard (the claim file exists), so the preflight passes and
    # Get-VenueStagedBuildManifest's .Substring on the missing digest throws in the guard.
    def test_artifact_preflight_r4_item3_a_throw_in_the_claim_guard_releases_the_claim_and_ends_typed(self) -> None:
        in_guard = f"(Test-Path -LiteralPath '{self.claim_path}' -PathType Leaf)"
        manifest_probe, artifact_probe = self._timed_probes(0, 0, no_digests_when=in_guard)
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]),
                          "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_PLAN_INVALID ", r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim granted before the throw was released")
        self.assertEqual(self.kinds(), [], "no lease was opened")

    def test_artifact_preflight_the_artifact_names_are_the_ones_the_leg_job_derives(self) -> None:
        script = (f"Import-Module '{ROOT / 'tools' / 'profiling' / 'bachelor' / 'AttrCudaArtifacts.psm1'}' -Force -DisableNameChecking\n"
                  "$n = Get-AttrCudaArtifactNames -SourceCommit ('3' * 40)\n"
                  "ConvertTo-Json -Compress -InputObject ([ordered]@{ exe = $n.exeName; dll = $n.reconName; packageZip = $n.packageZipName })\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for label, name in json.loads(out.stdout).items():
            self.assertEqual(self.artifact_path("3" * 40, label).name, name, label)

    # r13 (adversary r13 A2): the open can wait long in the agent's gate, and a link to an output made in that wait was written through by the first probe (gate.log).
    def test_r13_adv_a2_an_output_alias_added_while_the_open_waits_is_refused_before_any_probe(self) -> None:
        # gate.log: the probe's -GateLog, which Wait-VenueQuiet appends to. batch.log (r13 pass 2 A1): the runner's own log, which the finally wrote through after the refusal.
        for leaf in ("gate.log", "batch.log"):
            with self.subTest(output=leaf):
                self._open_wait_alias(leaf)

    def _open_wait_alias(self, leaf: str) -> None:
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = spec.read_text(encoding="utf-8")
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), FAKE_OPEN_SLEEP="12")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            # the last line before um-run blocks (FAKE_OPEN_SLEEP); a link made before it is written through at once, the race no re-check closes
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
            (self.run_dir / leaf).unlink(missing_ok=True)
            os.link(spec, self.run_dir / leaf)
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(p.returncode, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", se)
        self.assertIn("found after the open", se)
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        self.assertEqual(self.leg_calls(), [])
        self.assertTrue(any(k.startswith("CLOSE") for k in self.kinds()), "the lease was closed")
        self.assertFalse(self.claim_path.exists(), "the claim was released")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    # r14 (Dot 2e6eb21d; adversary r13 pass 3 A1, A2): the post-open guard ran only on a clean receipt, and its write suppression was set only in the refusal branch. An open that
    # failed (OPEN_FAILED), came back without a lease batch (AGENT_NOT_BATCH_CAPABLE), or was held in the agent's gate (logged before the guard; or held to the deadline, the guard
    # skipped) won the classification, and the cleanup log lines and the result line were appended through a batch.log that had become a hard link to the accepted spec.
    def test_r14_an_alias_made_in_the_open_wait_is_never_written_through_whatever_the_open_returned(self) -> None:
        cases = {
            # mode: (exit code, the typed reason the run still reports, extra args)
            "exit1": (9, "OPEN_FAILED", ()),
            "nobatch": (10, "AGENT_NOT_BATCH_CAPABLE", ()),
            "gatewait": (2, "LEG_SPEC_IS_RUN_OUTPUT", ()),
            # r8: the held open is waited for until the deadline, so the deadline is the case's whole run time: 20 s (it was 60) is still 6-7 times what the run needs to reach the open
            "gatehold": (11, "OPEN_CANCEL_UNRECONCILED", ("-CancelReconcileSec", "3", "-DeadlineUtc", "+20")),
        }
        for mode, (code, reason, extra) in cases.items():
            with self.subTest(open=mode):
                # the deadline is placed when this subtest starts, not when the table was built (the subtests before it take a minute or more under load)
                extra = tuple(utc_iso(datetime.now(timezone.utc) + timedelta(seconds=int(x[1:]))) if x.startswith("+") else x for x in extra)
                self._alias_in_open_wait(mode, code, reason, extra)

    def _alias_in_open_wait(self, mode: str, code: int, reason: str, extra: tuple[str, ...]) -> None:
        import hashlib
        self._fresh_run_dir()
        (self.share / "fake-agent-state.json").unlink(missing_ok=True)
        shutil.rmtree(self.share / "outbox", ignore_errors=True)
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = hashlib.sha256(spec.read_bytes()).hexdigest()
        # r8: the open blocks until the link is made (FAKE_OPEN_RELEASE) -- 60 s is only its bound -- instead of for a fixed 12 s
        release = self._open_release()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), *extra, FAKE_OPEN_SLEEP="60", FAKE_OPEN_RELEASE=str(release), FAKE_OPEN_MODES=mode)
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()
            (self.run_dir / "batch.log").unlink(missing_ok=True)
            os.link(spec, self.run_dir / "batch.log")
            release.write_text("go", encoding="utf-8")
            so, se = p.communicate(timeout=300)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(hashlib.sha256(spec.read_bytes()).hexdigest(), before, "the accepted input is byte-unchanged: nothing was appended through the alias")
        self.assertEqual(p.returncode, code, so + se)
        self.assertEqual(batch_result(so)["stopReason"], reason, "the typed reason still reaches stdout")
        self.assertIn("BATCH_OUTPUT_ALIASED ", se, "the alias is said on stderr")
        self.assertEqual(self.leg_calls(), [], "no leg ran")
        self.assertFalse(self.claim_path.exists(), "the claim was released")
        self.assertTrue(any(k.startswith("CLOSE") for k in self.kinds()), "the lease was closed")

    # adversary r14 A1, A2, A3 (on 2ea93e0f): the redirect folder was named from the token alone and reused with -Force, so links prepared in it were written through; TEMP inside -RunDir
    # put the redirect back in it; a redirect that could not be made threw, and the close then threw out of the finally (no claim release, no result); and the claim keeper went on
    # rewriting a claim file that had become the input until the close returned.
    def _spawn_open_wait(self, mode: str, *extra: str, env: dict | None = None, claim_dir: Path | None = None):
        import hashlib
        self._fresh_run_dir()
        (self.share / "fake-agent-state.json").unlink(missing_ok=True)
        shutil.rmtree(self.share / "outbox", ignore_errors=True)
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), *extra, FAKE_OPEN_SLEEP="12", FAKE_OPEN_MODES=mode, **(env or {}))
        if claim_dir is not None:
            args[args.index("-ClaimDir") + 1] = str(claim_dir)
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
        self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
        return p, spec, hashlib.sha256

    def _finish(self, p) -> tuple[str, str]:
        try:
            return p.communicate(timeout=300)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)

    def _open_token(self) -> str:
        [job] = [j.name for j in (self.share / "inbox").iterdir() if j.name.startswith("lease-batch-open-")]
        return job[: -len(".job.ps1")][-12:]

    def test_r14_adv_a1_a_redirect_folder_prepared_with_links_is_never_written_through(self) -> None:
        tmp = self.case / "r14-temp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        shutil.rmtree(self.share / "inbox", ignore_errors=True)
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(tmp), "TMP": str(tmp)})
        before = sha(spec.read_bytes()).hexdigest()
        token = self._open_token()
        prepared = tmp / f"venue-lease-batch-close-{token}"
        prepared.mkdir()
        os.link(spec, prepared / "batch.log")
        [close] = [f"lease-batch-close-{j.name[len('lease-batch-open-'):]}" for j in (self.share / "inbox").iterdir() if j.name.startswith("lease-batch-open-")]
        os.link(spec, prepared / close)
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before, "nothing was written through a link prepared in the redirect folder")
        self.assertEqual(p.returncode, 9, so + se)
        self.assertIn("BATCH_OUTPUT_ALIASED ", se)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r14_adv_a1_a_temp_inside_the_run_dir_gets_no_redirect_there(self) -> None:
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(self.run_dir), "TMP": str(self.run_dir)})
        before = sha(spec.read_bytes()).hexdigest()
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual([d.name for d in self.run_dir.iterdir() if d.name.startswith("venue-lease-batch-close-")], [], "the redirect was made inside -RunDir")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)
        self.assertIn("LEASE_BATCH_RESULT=", so)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r14_adv_a2_a_redirect_that_cannot_be_made_still_releases_the_claim_and_reports_a_typed_exit(self) -> None:
        blocker = self.case / "r14-not-a-dir"
        blocker.unlink(missing_ok=True)
        blocker.write_text("a file, so no folder can be made below it", encoding="utf-8")
        bad = blocker / "temp"
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(bad), "TMP": str(bad)})
        before = sha(spec.read_bytes()).hexdigest()
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual(batch_result(so)["stopReason"], "OPEN_FAILED")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r14_adv2_a2_a_temp_junction_into_the_run_dir_gets_no_redirect_there(self) -> None:
        alias = self.case / "r14-temp-junction"
        if alias.exists():
            os.rmdir(alias)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._junction(alias, self.run_dir)
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(alias), "TMP": str(alias)})
        before = sha(spec.read_bytes()).hexdigest()
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual([d.name for d in self.run_dir.iterdir() if d.name.startswith("venue-lease-batch-close-")], [], "the redirect was made inside -RunDir through the junction")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)

    def test_r14_adv2_a1_an_alias_found_in_the_gate_wait_still_refuses_after_the_link_is_removed(self) -> None:
        import hashlib
        self._fresh_run_dir()
        (self.share / "fake-agent-state.json").unlink(missing_ok=True)
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = hashlib.sha256(spec.read_bytes()).hexdigest()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), FAKE_OPEN_SLEEP="12", FAKE_OPEN_MODES="gatehold")
        err = self.case / "r14-adv2-a1.err.txt"
        with open(err, "w", encoding="utf-8") as ef:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=ef, text=True, env=e)
            try:
                self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
                self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
                (self.run_dir / "batch.log").unlink(missing_ok=True)
                os.link(spec, self.run_dir / "batch.log")
                end = time.monotonic() + 90
                while "BATCH_OUTPUT_ALIASED " not in err.read_text(encoding="utf-8") and time.monotonic() < end:
                    time.sleep(0.2)
                (self.run_dir / "batch.log").unlink()   # the link is gone before the open's result lands by name
                [job] = [j.name[: -len(".job.ps1")] for j in (self.share / "inbox").iterdir() if j.name.startswith("lease-batch-open-")]
                token, now = job[-12:], datetime.now(timezone.utc)
                adm = {"gate": "fake", "mode": "lease", "label": "clear", "heavySlotHeld": "1:1001", "slot": "C:\\t\\.heavy-slots\\1", "leaseMinutes": 45, "waitedSec": 7,
                       "leaseRequests": 2, "leaseRefusals": [], "leaseBatch": {"token": token, "leaseId": "lease-1", "seq": 1, "reused": False,
                                                                                "untilEpoch": int(now.timestamp()) + 2700, "remainingSec": 2700}}
                (self.share / "outbox").mkdir(parents=True, exist_ok=True)
                (self.share / "outbox" / f"{job}.result.json").write_text(json.dumps({"jobId": job, "exitCode": 0, "endedUtc": utc_iso(now), "admission": adm, "stdout": "LEASE_BATCH_OPEN"}), encoding="utf-8")
                so, _ = p.communicate(timeout=300)
            finally:
                if p.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                    p.wait(timeout=60)
        se = err.read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(self.leg_calls(), [], "a leg ran after the alias was found: " + so + se)
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe ran")
        self.assertEqual(p.returncode, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", se)

    def test_r14_adv_a3_the_claim_keeper_stops_writing_a_claim_file_that_became_the_input_once_the_alias_is_found(self) -> None:
        claim = self.run_dir / "bachelor.claim.json"
        p, spec, sha = self._spawn_open_wait("gatehold", "-ClaimTtlSec", "60", "-ClaimRenewSec", "1", "-CancelReconcileSec", "3",
                                             "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=45)), claim_dir=self.run_dir)
        # r7 (CI run 42209009621, shard 3/11): the keeper renews this file every second, and a read that lands inside its write is a Windows sharing violation, not an answer: read again
        for attempt in range(20):
            try:
                nonce = json.loads(claim.read_text(encoding="utf-8"))["nonce"]
                break
            except (PermissionError, json.JSONDecodeError):
                if attempt == 19:
                    raise
                time.sleep(0.05)
        body = json.loads(spec.read_text(encoding="utf-8"))
        body.update({"nonce": nonce, "expiresUtc": "2026-10-26T12:00:00Z"})
        spec.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        claim.unlink()
        os.link(spec, claim)
        so, se = self._finish(p)
        self.assertIn("BATCH_OUTPUT_ALIASED ", se, so + se)
        cancel = self.share / "cancel" / f"lease-batch-open-{[j.name for j in (self.share / 'inbox').iterdir() if j.name.startswith('lease-batch-open-')][0][len('lease-batch-open-'):-len('.job.ps1')]}.cancel"
        self.assertTrue(cancel.exists(), so + se)
        self.assertIn("close=RECEIPT", so + se, "the lease close was still submitted")
        # the keeper renews every second; stopped at the alias, its last write is long before the cancel the finally requests at the deadline
        self.assertLess(spec.stat().st_mtime, cancel.stat().st_mtime - 5, "the keeper went on rewriting the input after the alias was found")

    # r16 (sol r15 B2 = fable r15 H2 = adversary r14 A2): the redirect's path was built with Join-Path BEFORE its try, so TEMP on a drive that does not exist threw into the batch
    # catch: BATCH_ERROR (exit 12) replaced the open's typed stop, and the close job was still written into <RunDir>\batch-jobs. fable r15 H1: the containment test compared spellings
    # and final paths only, so a loopback UNC TEMP (\\localhost\C$\<RunDir>) put the redirect inside -RunDir.
    def _assert_redirect_refused_and_nothing_more_in_the_run_dir(self, temp: str) -> None:
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": temp, "TMP": temp})
        before = sha(spec.read_bytes()).hexdigest()
        jobs_before = sorted(j.name for j in (self.run_dir / "batch-jobs").iterdir())
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before, "the accepted input is byte-unchanged")
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual([d.name for d in self.run_dir.iterdir() if d.name.startswith("venue-lease-batch-close-")], [], "the redirect was made inside -RunDir")
        self.assertEqual(sorted(j.name for j in (self.run_dir / "batch-jobs").iterdir()), jobs_before, "a job file was written into -RunDir after the alias was found")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)
        res = batch_result(so)
        self.assertEqual(res["stopReason"], "OPEN_FAILED", "the open's typed stop is kept")
        self.assertEqual(res["outputRedirect"]["state"], "FAILED", res)
        self.assertTrue(res["outputRedirect"]["error"], res)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    # r16 (adversary r16 A1 on fd258854): the staged manifest's digest and content refusals ran BEFORE its location guard, so a manifest that IS <RunDir>\batch.log (a hard link) was
    # appended to by the BUILD_MANIFEST_MISMATCH refusal's own log line: the refused input changed. The guard now comes first, and no refusal appends through a linked batch.log.
    def test_r16_adv_a1_a_staged_manifest_that_is_batch_log_is_never_appended_to_by_its_own_refusal(self) -> None:
        import hashlib
        c = "7" * 40
        cases = {
            "a wrong (well-formed) digest": ({}, "0" * 64),
            "a content problem": ({"pendingSymbolPresence": "false"}, None),
        }
        for label, (override, digest) in cases.items():
            with self.subTest(case=label):
                staged = self.stage(c, "linked build", **override)
                manifest = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
                self._fresh_run_dir()
                os.link(manifest, self.run_dir / "batch.log")
                before = hashlib.sha256(manifest.read_bytes()).hexdigest()
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=digest or staged)]))
                self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), before, "the refused staged manifest was appended to through batch.log: " + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted")

    def test_artifact_preflight_r14_adv_p2_a1_a_manifest_that_links_to_batch_log_is_not_appended_to_when_its_artifacts_cannot_be_hashed(self) -> None:
        # #705 r14 adversary pass 2 A1: the cached build.json is a symbolic link to a plain one-name batch.log, and its artifacts cannot be hashed (the read throws, as a package zip
        # held with FileShare.None does). On 31ba19ee the preflight refused SHARE_UNREACHABLE BEFORE the manifest's run-output guard, and that refusal's log line was appended to
        # batch.log, which is the manifest. The guard now comes first, as it did before the artifact hash existed. The hard-link case (two names, so never appended to) shows the same
        # order on any host: it was refused SHARE_UNREACHABLE, never named as the run output it is; the symbolic-link case needs the privilege to make one (hosted CI has it).
        import hashlib
        c = "7" * 40
        probe = self.case / "artifact-probe-throws.ps1"
        probe.write_text("param($items)\nthrow 'the package zip is held with FileShare.None'\n", encoding="utf-8")
        for how in ("hard link", "symbolic link"):
            with self.subTest(how=how):
                staged = self.stage(c, "linked build")
                manifest = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
                self._fresh_run_dir()
                log = self.run_dir / "batch.log"
                if how == "hard link":
                    os.link(manifest, log)
                else:
                    log.write_bytes(manifest.read_bytes())
                    manifest.unlink()
                    try:
                        os.symlink(log, manifest)
                    except OSError as e:
                        self.skipTest(f"file symlinks need the privilege here: {e}")
                before = hashlib.sha256(log.read_bytes()).hexdigest()
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=staged)]), "-ArtifactProbeScript", str(probe))
                self.assertEqual(hashlib.sha256(log.read_bytes()).hexdigest(), before, "the staged manifest was appended to through batch.log: " + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted")

    def test_r16_b2_a_temp_on_a_drive_that_does_not_exist_keeps_the_open_s_stop_and_writes_no_job_into_the_run_dir(self) -> None:
        free = [c for c in "ZYXWVUTSRQPONMLKJIHGFED" if not os.path.exists(f"{c}:\\")]
        if not free:
            self.skipTest("every drive letter is in use")
        self._assert_redirect_refused_and_nothing_more_in_the_run_dir(f"{free[0]}:\\missing")

    def test_r16_h1_a_loopback_unc_temp_that_is_the_run_dir_gets_no_redirect_there(self) -> None:
        self._fresh_run_dir()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._assert_redirect_refused_and_nothing_more_in_the_run_dir(str(self._loopback_unc(self.run_dir)))

    # r12 (adversary r12 A2): TrimEnd('\') turned the drive root C:\ into C:, the drive's current directory, so a root zone was listed from the wrong folder. A real volume root cannot be
    # written to by a test, so the function that decides it is run on its own: its source is taken from the runner, so this cannot drift from what the runner does.
    def test_r12_a2_a_drive_root_keeps_its_backslash_when_trailing_separators_are_removed(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("function Remove-TrailingSeparator")
        end = text.index("\nfunction ", start + 1)
        script = self.case / "trim-probe.ps1"
        script.write_text(text[start:end] + "\n" + "\n".join(
            f"'{raw}' + ' => ' + (Remove-TrailingSeparator '{raw}')" for raw in ("C:\\", "C:\\\\", "d:\\", "C:\\run\\", "C:\\run", "\\\\srv\\share\\", "C:\\a\\b\\\\")) + "\n", encoding="ascii")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = [l for l in r.stdout.splitlines() if " => " in l]
        self.assertEqual(got, ["C:\\ => C:\\", "C:\\\\ => C:\\", "d:\\ => d:\\", "C:\\run\\ => C:\\run", "C:\\run => C:\\run", "\\\\srv\\share\\ => \\\\srv\\share", "C:\\a\\b\\\\ => C:\\a\\b"])

    def test_r12_a2_no_zone_root_or_parent_is_trimmed_with_a_bare_trimend(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("function Get-OutputZones")
        end = text.index("function Get-OutputNameRefusal")
        self.assertNotIn(".TrimEnd('\\')", text[start:end].replace(".root.TrimEnd('\\')", "").replace("$parent.TrimEnd('\\')", "").replace("$l.physical.TrimEnd('\\')", ""))

    def test_r5_adv_a1_a_throw_after_a_leg_ran_does_not_record_that_leg_as_not_run(self) -> None:
        # adversary r5 A1: the new batch catch left the leg whose child had completed (its output file could not be written) in pending, and the summary then said
        # NOT_RUN for a leg that ran. The leg is recorded LEG_FAILED / reason BATCH_ERROR (its result is unknown); only the legs never started are NOT_RUN.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "leg-01-fx-a-cpu-A-b01-a1.out.txt").mkdir()
        a = self.commit_a
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "A", a)]))
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertEqual(len(self.leg_calls()), 1, "the first leg's child ran")
        res = batch_result(r.stdout)
        self.assertEqual([(l["name"], l["status"]) for l in res["legs"]], [("01-fx-a-cpu-A", "LEG_FAILED"), ("02-fx-b-cpu-A", "NOT_RUN")], res["legs"])
        self.assertEqual((res["ran"], res["failed"], res["notRun"], res["stopReason"]), (0, 1, 1, "BATCH_ERROR"))
        self.assertEqual(res["legs"][0]["reason"], "BATCH_ERROR")
        [b] = self.batches()
        self.assertEqual([(l["name"], l["status"]) for l in b["legs"]], [("01-fx-a-cpu-A", "LEG_FAILED")])
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r5_adv_a2_a_staged_build_the_leg_job_would_reject_is_refused_before_the_claim_and_the_lease(self) -> None:
        # adversary r5 A2: preflight read the staged manifest's sha256 only. The leg job's validator (Assert-AttrCudaBuildManifest) also needs the manifest to declare
        # the same sourceCommit, a well-formed dllPairManifestSha256 and a boolean pendingSymbolPresence, so a hash-matching manifest that fails any of them opened a lease first.
        a, c = self.commit_a, "3" * 40
        cases = {
            "declares another commit": {"sourceCommit": "4" * 40},
            "no sourceCommit": {"sourceCommit": ABSENT},
            "no dllPairManifestSha256": {"dllPairManifestSha256": ABSENT},
            "dllPairManifestSha256 malformed": {"dllPairManifestSha256": "xyz"},
            "no pendingSymbolPresence": {"pendingSymbolPresence": ABSENT},
            "pendingSymbolPresence is a string": {"pendingSymbolPresence": "false"},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                manifest = self.stage(c, "bad build", **override)
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_MANIFEST_MISMATCH item=2 ")

    def test_r5_adv_a2_a_staged_build_that_is_not_json_is_refused(self) -> None:
        c = "3" * 40
        cache = self.share / "cache"
        p = cache / f"playback-attr-3-cuda-{c[:12]}-build.json"
        p.write_text("this is not json", encoding="utf-8")
        import hashlib
        manifest = hashlib.sha256(p.read_bytes()).hexdigest()
        self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_MANIFEST_MISMATCH item=2 ")

    def test_r5_adv2_a2_a_leg_is_marked_in_flight_at_the_launch_not_before_its_run_line_is_logged(self) -> None:
        # adversary r5 pass 2 A2: the marker was set before the "LEG .. RUN" log line, so a throw while that line was appended recorded a leg whose child never started as
        # LEG_FAILED. It is set on the line before the child is launched, after everything that can throw without having started it.
        text = RUNNER.read_text(encoding="utf-8")
        run_line, launch = text.index('L "LEG $($leg.name) RUN batch='), text.index("$lp = Invoke-VenueBoundedProcess -FilePath $pwshExe")
        marks = [i for i in range(len(text)) if text.startswith("$inFlightLeg = $leg", i)]
        self.assertEqual(len(marks), 1, marks)
        self.assertTrue(run_line < marks[0] < launch, (run_line, marks, launch))
        self.assertEqual(text[marks[0]:launch].count("\n"), 1, "nothing between the marker and the launch but the launch's own line")

    def test_r5_adv2_a3_the_staged_manifest_digest_and_facts_come_from_one_read(self) -> None:
        # adversary r5 pass 2 A3: Get-FileHash and a second read of the same file could pair one version's digest with another's content.
        text = MODULE.read_text(encoding="utf-8")
        body = text[text.index("function Get-VenueStagedBuildManifest"):text.index("$script:VenueAbandonedReads = 0")]
        self.assertEqual(body.count("ReadAllBytes"), 1, body)
        self.assertNotIn("Get-FileHash", body)
        self.assertNotIn("ReadAllText", body)

    def test_r5_adv2_a3_a_staged_manifest_with_a_byte_order_mark_is_still_read(self) -> None:
        c = "5" * 40
        p = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"sourceCommit": c, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **ARTIFACT_SHAS}).encode("utf-8"))
        script = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{psq(self.share)}' -SourceCommit '{c}' -TimeoutSec 30\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; sha = $r.sha256; problem = $r.contentProblem }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        import hashlib
        self.assertEqual(json.loads(out.stdout), {"state": "STAGED", "sha": hashlib.sha256(p.read_bytes()).hexdigest(), "problem": ""})

    def test_r6_adv_a1_preflight_decodes_the_staged_manifest_exactly_as_the_leg_jobs_validator_does(self) -> None:
        # adversary r6 A1: preflight stripped every leading U+FEFF from a UTF-8 decode and never honoured a UTF-16 BOM, while Assert-AttrCudaBuildManifest reads with
        # [IO.File]::ReadAllText (one BOM, UTF-16/32 by BOM). A doubled BOM was accepted here and rejected by the leg job after the lease opened; a UTF-16 manifest the
        # job accepts was refused here. The oracle below is that very read; preflight must agree with it on every input.
        c = "6" * 40
        body = json.dumps({"sourceCommit": c, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **ARTIFACT_SHAS})
        cases = {
            "no BOM": (body.encode("utf-8"), ""),
            "one UTF-8 BOM": (b"\xef\xbb\xbf" + body.encode("utf-8"), ""),
            "two UTF-8 BOMs": (b"\xef\xbb\xbf\xef\xbb\xbf" + body.encode("utf-8"), "ATTRCUDA_BUILD_MANIFEST_UNPARSEABLE"),
            "UTF-16LE with a BOM": (b"\xff\xfe" + body.encode("utf-16-le"), ""),
            "UTF-16BE with a BOM": (b"\xfe\xff" + body.encode("utf-16-be"), ""),
        }
        p = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
        for label, (data, expected) in cases.items():
            with self.subTest(case=label):
                p.write_bytes(data)
                script = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                          f"$r = Get-VenueStagedBuildManifest -AgentShare '{psq(self.share)}' -SourceCommit '{c}' -TimeoutSec 30\n"
                          f"$oracle = try {{ $null -ne ([IO.File]::ReadAllText('{psq(p)}') | ConvertFrom-Json) }} catch {{ $false }}\n"
                          "ConvertTo-Json -Compress -InputObject @{ state = $r.state; problem = [string]$r.contentProblem; validatorParses = $oracle }\n")
                out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                got = json.loads(out.stdout)
                self.assertEqual(got["state"], "STAGED")
                self.assertEqual(got["validatorParses"], expected == "", "the oracle (the validator's own read) disagrees with the fixture's expectation")
                self.assertTrue(got["problem"].startswith(expected), got)
                self.assertEqual(got["problem"] == "", got["validatorParses"], "preflight and the validator disagree")

    def test_r5_h2_the_interleave_helper_keeps_an_array_valued_item_whole(self) -> None:
        # key3 H2 (sol): the bare Dequeue() output was enumerated, so one array-valued item came back as its elements.
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "$items = [object[]]::new(2); $items[0] = @([pscustomobject]@{ arm = 'A' }, [pscustomobject]@{ arm = 'B' }); $items[1] = [pscustomobject]@{ arm = 'A' }\n"
            "$one = [object[]]::new(1); $one[0] = $items[0]\n"
            "[ordered]@{ one = @(Get-VenueInterleavedLegPlan -Items $one).Count; two = @(Get-VenueInterleavedLegPlan -Items $items).Count } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"one": 1, "two": 2})

    def test_the_plan_and_the_scalar_form_are_mutually_exclusive(self) -> None:
        args, e = self.plan_cmd(self.ab_block_plan(), "-SourceCommit", SHA)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.kinds(), [])

    def test_the_interleave_helper(self) -> None:
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "function P([string]$arms) { @($arms.ToCharArray() | ForEach-Object -Begin { $i = 0 } -Process { $i++; [pscustomobject]@{ arm = [string]$_; n = $i } }) }\n"
            "function S($items) { (@(Get-VenueInterleavedLegPlan -Items $items) | ForEach-Object { \"$($_.arm)$($_.n)\" }) -join ' ' }\n"
            "[ordered]@{ block = S (P 'AABB'); uneven = S (P 'AAAB'); three = S (P 'AABBCC'); one = S (P 'AAA'); mixed = S (P 'ABBA') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"block": "A1 B3 A2 B4", "uneven": "A1 B4 A2 A3", "three": "A1 B3 C5 A2 B4 C6", "one": "A1 A2 A3", "mixed": "A1 B2 A4 B3"})


# the runner cases are inherited only for their fixtures: they run once, in their own class (unittest collects callable test_* attributes only)
for _name in [n for n in vars(VenueLeaseBatchRunnerTests) if n.startswith("test_")]:
    setattr(VenueLeaseBatchLegPlanTests, _name, None)
del _name


# CI-WINDOWS-UNITTEST-HEAVY-PARTS-3-1: ci_unittest_shard places whole classes, and VenueLeaseBatchLegPlanTests measured 1039-1121 s on hosted windows (master
# runs 40858861385, 40864893269, 40871917397, 40878296653), past the 1074 s one windows shard or heavy part holds under ShardDurationBudgetTests. Its test_r1_* ..
# test_r8_* cases move, unchanged, into a sibling class on the same fixtures (the split #705 r13 staged), so no class is too heavy to place. Each case runs once.
# LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 r14: the 69 test_artifact_preflight_* cases measured 545.9 s on hosted windows (run 40861273862), which with the 79 cases
# left in VenueLeaseBatchLegPlanTests is 1131 s, past the same 1074 s, so they move, unchanged, into a third sibling class on the same fixtures.
@requires_windows_pwsh
class VenueLeaseBatchArtifactPreflightTests(VenueLeaseBatchLegPlanTests):
    """The LegPlan cases named test_artifact_preflight_* (LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1)."""


@requires_windows_pwsh
class VenueLeaseBatchLegPlanEarlyRoundTests(VenueLeaseBatchLegPlanTests):
    """The LegPlan cases named test_r1_* through test_r8_* (letter suffixes included)."""


def _leg_plan_home(name: str) -> type:
    if name.startswith("test_artifact_preflight_"):
        return VenueLeaseBatchArtifactPreflightTests
    lettered = re.match(r"test_r(\d+)", name)
    if lettered and int(lettered.group(1)) <= 8:
        return VenueLeaseBatchLegPlanEarlyRoundTests
    return VenueLeaseBatchLegPlanTests


for _name, _case in [(n, f) for n, f in vars(VenueLeaseBatchLegPlanTests).items() if n.startswith("test_") and callable(f)]:
    for _cls in (VenueLeaseBatchLegPlanTests, VenueLeaseBatchArtifactPreflightTests, VenueLeaseBatchLegPlanEarlyRoundTests):
        setattr(_cls, _name, _case if _cls is _leg_plan_home(_name) else None)
del _name, _case, _cls


# 2026-10-25 LEASE-BATCH-LEASE-END-TZ-1: the agent's result reaches the runner through ConvertFrom-Json, which on PowerShell 7 turns "endedUtc": "...Z" into a
# [DateTime] of Kind Utc. Get-LeaseEndUtc used to run it through [string] + DateTimeOffset.Parse, which drops the Kind and assumes the HOST's local offset, so on a
# Central host every lease end landed 5 h early (roomSec=-15301) and no leg ever started. CI hosts are UTC (offset 0), where the old code passes, so the behavioural
# cases below only go red on a non-UTC host; the source case is the one that guards on UTC.
@requires_windows_pwsh
class VenueLeaseBatchReceiptRootTests(unittest.TestCase):
    """LEASE-BATCH-RECEIPT-ROOT-1: -ReceiptRoot reaches every Invoke-VenueLeg call, an empty one forwards nothing, and a root outside .claude-state is refused before any lease.
    Same fakes and harness as VenueLeaseBatchRunnerTests (borrowed, not inherited, so its cases are not run a second time); nothing reaches a venue."""

    setUpClass = VenueLeaseBatchRunnerTests.__dict__["setUpClass"]
    tearDownClass = VenueLeaseBatchRunnerTests.__dict__["tearDownClass"]
    setUp = VenueLeaseBatchRunnerTests.setUp
    _init_case = VenueLeaseBatchRunnerTests._init_case
    run_batch = VenueLeaseBatchRunnerTests.run_batch
    batch_cmd = VenueLeaseBatchRunnerTests.batch_cmd
    kinds = VenueLeaseBatchRunnerTests.kinds
    batches = VenueLeaseBatchRunnerTests.batches

    def scratch_root(self) -> Path:
        # a root the way a calibration round gives one: <scratch>\.claude-state\<dual-venue dir>\<receipts dir> (the evidence directory lands beside it, also under .claude-state;
        # the spelling avoids the literal path the receipt-reader inventory in test_dual_venue_evidence.py scans for: this file reads no real receipt, only the fake leg's)
        return self.case / "scratch" / ".claude-state" / "dual-venue" / "receipts"

    def clean_base(self) -> Path:
        """A fresh directory whose path has no .claude-state segment (the temp dir may have one: a lane runs with TEMP under its worktree's .claude-state)."""
        for parent in (Path(tempfile.gettempdir()), ROOT.parent, Path(os.environ.get("SystemDrive", "C:") + "\\")):
            if ".claude-state" in parent.resolve().parts:
                continue
            try:
                d = Path(tempfile.mkdtemp(prefix="venue-rr-clean-", dir=parent))
            except OSError:
                continue
            self.addCleanup(shutil.rmtree, d, True)
            return d
        self.skipTest("no writable directory without a .claude-state segment")

    def long_form(self, p: Path) -> Path:
        """The fully resolved long form of a path under the case dir. The script resolves -ReceiptRoot through [IO.Path]::GetFullPath, which on Windows expands 8.3 short names
        (a hosted runner's temp dir is C:/Users/RUNNER~1/..., the script forwards C:/Users/runneradmin/...): expect the long form, as the script and the real leg see it."""
        return Path(os.path.realpath(self.case)) / p.relative_to(self.case)

    def default_root(self) -> Path:
        return self.case / "receipts"   # the fake leg's default root (the real leg's is under the main checkout's .claude-state)

    def leg_calls(self) -> list[str]:
        f = self.case / "leg-receiptroot.log"
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []

    @staticmethod
    def files_under(root: Path) -> list[Path]:
        return sorted(p for p in root.rglob("*.json")) if root.exists() else []

    def assert_nothing_started(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.events_text(), "", "a refused root must reach no um-run, quiet probe or leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "a refused root took the venue claim")
        self.assertFalse(self.run_dir.exists(), "a refused root created the run dir")

    def events_text(self) -> str:
        return self.events.read_text(encoding="utf-8") if self.events.exists() else ""

    # (a) RED on the old script: it has no -ReceiptRoot, so the leg wrote into the default root
    def test_a_receipt_root_sends_every_leg_receipt_to_it_and_none_to_the_default_root(self) -> None:
        root = self.scratch_root()
        r = self.run_batch(self.specs, "-ReceiptRoot", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.files_under(self.default_root()), [], "a receipt landed in the default root")
        self.assertFalse(self.default_root().exists(), "the default root was created")
        landed = self.files_under(root)
        self.assertEqual(len(landed), 3, landed)
        self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(root)}"] * 3)
        [b] = self.batches()
        self.assertTrue(all(Path(os.path.realpath(l["receipt"])).parent.parent.parent.parent == self.long_form(root) for l in b["legs"]), [l["receipt"] for l in b["legs"]])
        # the read-back: the pre-load mean is found in the evidence directory beside THIS root (a default-root read would find none and leave the leg uncounted)
        self.assertEqual([l["class"] for l in b["legs"]], ["QUIET_PRELOAD_OK"] * 3, b["legs"])
        self.assertEqual((b["legsRan"], b["legsCounted"]), (3, 3))
        self.assertIn(f" receiptRoot={self.long_form(root)}", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    def test_a_receipt_root_is_forwarded_to_the_in_lease_retry_too(self) -> None:
        root = self.scratch_root()
        r = self.run_batch([self.specs[0]], "-ReceiptRoot", str(root), FAKE_LEG_OUTCOMES="VENUE_NOT_QUIESCENT,PASS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual([l["status"] for l in b["legs"]], ["RETRY", "RAN"])
        self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(root)}"] * 2)
        self.assertEqual(len(self.files_under(root)), 2)
        self.assertFalse(self.default_root().exists())

    def test_a_relative_receipt_root_is_forwarded_as_a_full_path(self) -> None:
        args, e = self.batch_cmd(None, "-ReceiptRoot", os.path.join("rel", ".claude-state", "dual-venue", "receipts"))
        r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e, cwd=str(self.case))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        want = self.long_form(self.case / "rel" / ".claude-state" / "dual-venue" / "receipts")
        self.assertEqual(self.leg_calls(), [f"bound=True root={want}"] * 2)

    def test_adversary_a1_a_relative_receipt_root_resolves_against_the_powershell_location_like_rundir_does(self) -> None:
        # adversary f1c42e80 A1: the process directory (what [IO.Path]::GetFullPath uses) and the PowerShell location (what Resolve-Path -RunDir uses) differ once a caller has
        # done Set-Location. The script is run in-process from a session whose location is cwd-b while the process directory is cwd-a: the root belongs under cwd-b.
        proc_dir, ps_dir = self.case / "cwd-a", self.case / "cwd-b"
        proc_dir.mkdir()
        ps_dir.mkdir()
        rel = os.path.join("rel", ".claude-state", "dual-venue", "receipts")
        args, e = self.batch_cmd(None, "-ReceiptRoot", rel)
        i = args.index("-File")
        runner, rest = args[i + 1], args[i + 2:]
        call = " ".join(x if re.fullmatch(r"-[A-Za-z][A-Za-z0-9]*", x) else f"'{psq(x)}'" for x in rest)
        command = f"Set-Location -LiteralPath '{psq(ps_dir)}'; & '{psq(runner)}' {call}; exit $LASTEXITCODE"
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=600, env=e, cwd=str(proc_dir))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        want = ps_dir / "rel" / ".claude-state" / "dual-venue" / "receipts"
        self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(want)}"] * 2, r.stdout + r.stderr)
        self.assertEqual(len(self.files_under(want)), 2)
        self.assertFalse((proc_dir / "rel").exists(), "the root was resolved against the process directory")

    # (b) RED on the old script for the explicit empty/blank spellings (an unknown parameter); the omitted spelling is the byte-for-byte guard
    def test_an_empty_or_blank_receipt_root_forwards_no_argument(self) -> None:
        for spelling in (None, "", "   "):
            with self.subTest(spelling=spelling):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, *(() if spelling is None else ("-ReceiptRoot", spelling)))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.leg_calls(), ["bound=False root="] * 2, "the leg was handed a -ReceiptRoot it should not have been")
                self.assertEqual(len(self.files_under(self.default_root())), 2)
                self.assertNotIn("receiptRoot=", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    # (c) RED on the old script: a root outside .claude-state is refused before the run dir, the claim, the lease or a leg
    def test_a_receipt_root_outside_claude_state_is_refused_before_any_lease_or_leg(self) -> None:
        # the case dirs sit in the temp dir, which on a lane (TEMP under the worktree's .claude-state) is itself under .claude-state: the refused roots live in a base with no such segment
        base = self.clean_base()
        cases = {
            "outside": base / "plain" / "dual-venue" / "receipts",
            # a root that IS .claude-state: the root passes, the evidence directory beside it (<parent>\evidence) does not
            "evidence-beside-it-outside": base / "scratch" / ".claude-state",
            # the segment must be a whole directory name, not a substring
            "substring-only": base / "x.claude-state" / "receipts",
        }
        for name, root in cases.items():
            with self.subTest(name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(root))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)

    # Dot A1 (PR #729 comment 6540489428): a path component was compared with -ceq, which is culture-aware (InvariantCulture) and skips ignorable code points, so a directory
    # named ".claude-state<U+00AD>" passed as the gitignored ".claude-state". Each spelling below is one the old comparison accepts on this host (the first assertion proves it).
    IGNORABLE = {"soft-hyphen": "\u00ad", "zero-width-joiner": "\u200d", "zero-width-space": "\u200b", "bom": "\ufeff"}

    @staticmethod
    def old_comparison_accepts(component: str) -> bool:
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); ('{psq(component)}' -ceq '.claude-state')"],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        return out.stdout.strip() == "True"

    def ignorable_roots(self) -> dict:
        base = self.clean_base()
        roots = {}
        for name, cp in self.IGNORABLE.items():
            roots[f"{name}-suffix"] = (f".claude-state{cp}", base / f".claude-state{cp}" / "dual-venue" / "receipts")
            roots[f"{name}-prefix"] = (f"{cp}.claude-state", base / f"{cp}.claude-state" / "dual-venue" / "receipts")
        return roots

    def test_adversary_dot_a1_an_ignorable_code_point_in_the_boundary_name_is_not_claude_state_at_the_batch_entry(self) -> None:
        for name, (component, root) in self.ignorable_roots().items():
            with self.subTest(name=name):
                self.assertTrue(self.old_comparison_accepts(component), f"{name}: the culture-aware comparison no longer accepts it on this host, so this case proves nothing")
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(root))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)

    def test_adversary_dot_a1_an_ignorable_code_point_in_the_boundary_name_is_refused_at_the_real_leg_entry(self) -> None:
        # the real Invoke-VenueLeg.ps1 receipt-root check (DVE_RECEIPT_ROOT_MUST_STAY_LOCAL), reached by its own text: the shared helper it calls decides
        leg = DV / "Invoke-VenueLeg.ps1"
        for name, (component, root) in self.ignorable_roots().items():
            with self.subTest(name=name):
                script = (
                    f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                    + leg_receipt_root_guard_ps(leg, root)
                )
                out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=120)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertTrue(out.stdout.strip().startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), out.stdout + out.stderr)

    def test_adversary_dot_a1_a_plain_claude_state_root_still_passes_the_shared_helper(self) -> None:
        script = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "[ordered]@{ plain = (Test-DvUnderClaudeState -Path 'C:\\x\\.claude-state\\dual-venue\\receipts'); fwd = (Test-DvUnderClaudeState -Path 'C:/x/.claude-state/a'); "
            "caseDiffers = (Test-DvUnderClaudeState -Path 'C:\\x\\.Claude-State\\a'); substring = (Test-DvUnderClaudeState -Path 'C:\\x\\x.claude-state\\a') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"plain": True, "fwd": True, "caseDiffers": False, "substring": False})

    # Dot A2 (PR #729 comment 6542192087) + adversary A1: a path component named .claude-state is satisfied INSIDE a network share. The refusal must come before any write:
    # these roots name a share that does not exist for the test, and a UNC GetFullPath does no network I/O, so a regression here would fail on a missing share, never write one.
    NETWORK_ROOTS = {
        "production-agent-share": "\\\\bachelor\\mlv-agent\\.claude-state\\receipts",
        "admin-share-of-localhost": "\\\\localhost\\c$\\mlvtmp\\.claude-state\\receipts",
        "forward-slash-unc": "//bachelor/mlv-agent/.claude-state/receipts",
        "extended-unc": "\\\\?\\UNC\\bachelor\\mlv-agent\\.claude-state\\receipts",
        "agent-share-itself": "\\\\bachelor\\mlv-agent\\receipts\\.claude-state",
    }

    def test_adversary_dot_a2_a_receipt_root_on_a_network_share_is_refused_at_the_batch_entry(self) -> None:
        for name, root in self.NETWORK_ROOTS.items():
            with self.subTest(name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", root)
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)

    def test_adversary_dot_a2_a_receipt_root_on_a_network_share_is_refused_at_the_real_leg_entry(self) -> None:
        leg = DV / "Invoke-VenueLeg.ps1"
        for name, root in self.NETWORK_ROOTS.items():
            with self.subTest(name=name):
                script = (
                    f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                    + leg_receipt_root_guard_ps(leg, root)
                )
                out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=120)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertTrue(out.stdout.strip().startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), out.stdout + out.stderr)

    def test_adversary_dot_a2_the_shared_locality_helper_refuses_a_venue_share_prefix_and_keeps_local_roots(self) -> None:
        script = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "$shares = @('C:\\fake\\agent-share', '\\\\bachelor\\mlv-agent')\n"
            "function L($p) { $null -ne (Test-DvLocalReceiptRoot -Path $p -AgentShares $shares) }\n"
            "[ordered]@{\n"
            "  localPlain = (L 'C:\\x\\.claude-state\\dual-venue\\receipts'); localFwd = (L 'C:/x/.claude-state/a'); localLongPrefix = (L '\\\\?\\C:\\x\\.claude-state\\a')\n"
            "  underShare = (L 'C:\\fake\\agent-share\\.claude-state\\receipts'); underShareCase = (L 'c:\\FAKE\\Agent-Share\\.claude-state\\receipts'); underShareFwd = (L 'C:/fake/agent-share/.claude-state/r')\n"
            "  shareItself = (L 'C:\\fake\\agent-share'); siblingOfShare = (L 'C:\\fake\\agent-share2\\.claude-state\\receipts')\n"
            "  unc = (L '\\\\bachelor\\mlv-agent\\.claude-state\\r'); uncOther = (L '\\\\localhost\\c$\\x\\.claude-state\\r'); deviceNonDrive = (L '\\\\?\\GLOBALROOT\\x\\.claude-state\\r')\n"
            "  noMarker = (L 'C:\\x\\receipts'); ignorable = (L \"C:\\x\\.claude-state$([char]0xAD)\\r\")\n"
            "  configured = ($null -ne (Test-DvLocalReceiptRoot -Path '\\\\bachelor\\mlv-agent\\.claude-state\\r'))\n"
            "} | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {
            "localPlain": True, "localFwd": True, "localLongPrefix": True,
            "underShare": False, "underShareCase": False, "underShareFwd": False,
            "shareItself": False, "siblingOfShare": True,
            "unc": False, "uncOther": False, "deviceNonDrive": False,
            "noMarker": False, "ignorable": False, "configured": False,
        })

    def test_adversary_dot_a2_every_write_root_guard_goes_through_the_locality_helper(self) -> None:
        # the class, not the instance: no script outside the helper itself asks Test-DvUnderClaudeState alone about a root that is written to
        offenders = []
        for p in sorted(DV.glob("*.ps1")):
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "Test-DvUnderClaudeState" in line and not line.lstrip().startswith("#") and "Test-DvLocalReceiptRoot" not in line and "Import-Module" not in line:
                    offenders.append(f"{p.name}:{n}: {line.strip()[:140]}")
        self.assertEqual(offenders, [], "a write-root guard that tests only for the marker directory name accepts the same name inside a network share")

    # ---- r4: the four MAJOR gaps the r3 adversary pass found in the invariant r3 claims ("every owner-evidence write root is local"): review-adversary-LEASE-BATCH-RECEIPT-ROOT-1-r3-28991373, successor cards ADV-A1..A4.
    # The helper (Test-DvLocalReceiptRoot) now RETURNS the normalised full path it certified, or $null; every caller writes to that value.
    def judge(self, shares: list, cases: dict, location: Path | None = None, process_dir: Path | None = None) -> dict:
        """Run Test-DvLocalReceiptRoot on each named path with `shares` as the venue agentShares; the value is the certified path or None (a refusal). `location` is the PowerShell location
        and `process_dir` the process directory the helper runs with (they differ once a caller has done Set-Location)."""
        script = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"   # r7 G9: a certified path may carry a non-ASCII name (a superscript digit); the OEM code page is not UTF-8
        script += f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
        if location is not None:
            script += f"Set-Location -LiteralPath '{psq(location)}'\n[Environment]::CurrentDirectory = '{psq(process_dir)}'\n"
        script += (
            f"$shares = @('{psq(json.dumps(shares))}' | ConvertFrom-Json)\n"
            f"$cases = '{psq(json.dumps(cases))}' | ConvertFrom-Json\n"
            "$out = [ordered]@{}\n"
            "foreach ($p in $cases.PSObject.Properties) { $out[$p.Name] = Test-DvLocalReceiptRoot -Path ([string]$p.Value) -AgentShares $shares }\n"
            "$out | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout)

    @staticmethod
    def same_path(a: str | None, b: Path | str) -> bool:
        return a is not None and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(str(b)))

    def pair_guard_ps(self, script_path: Path, out_dir: str) -> str:
        """PowerShell that runs the REAL -OutDir admission of a pair script (its own text, from `$outFull = Test-DvLocalReceiptRoot` to `$OutDir = $outFull`) on `out_dir`."""
        return (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            f"$text = [IO.File]::ReadAllText('{psq(script_path)}')\n"
            "$m = [regex]::Match($text, '(?s)\\$outFull = Test-DvLocalReceiptRoot.*?\\$OutDir = \\$outFull')\n"
            "if (-not $m.Success) { throw 'the pair OutDir guard was not found' }\n"
            f"$OutDir = '{psq(out_dir)}'\n"
            "try { . ([scriptblock]::Create($m.Value)); 'ACCEPTED ' + $OutDir } catch { 'REFUSED ' + $_.Exception.Message }\n"
        )

    def run_ps(self, script: str, cwd: Path | None = None) -> str:
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=180, cwd=None if cwd is None else str(cwd))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout.strip()

    def test_adversary_r3_a1_both_pair_scripts_refuse_a_unc_or_device_out_dir_before_any_write(self) -> None:
        # adversary A1: New-VenueSheetPair.ps1 tested only for the .claude-state name, so a UNC -OutDir with that name created staging directories and wrote captured frame bytes on the venue share.
        # The guard text is taken from each script itself; the refusal comes before any New-Item / WriteAllBytes (a UNC GetFullPath does no network I/O, so a regression fails on a missing share, never writes one).
        for script in ("New-VenueSheetPair.ps1", "New-VenueFlavorPair.ps1"):
            for label, out_dir in (("unc", "\\\\bachelor\\mlv-agent\\.claude-state\\pair-r3"), ("forward-slash-unc", "//bachelor/mlv-agent/.claude-state/pair-r3"),
                                   ("extended-unc", "\\\\?\\UNC\\bachelor\\mlv-agent\\.claude-state\\pair-r3"), ("device", "\\\\.\\C:\\x\\.claude-state\\pair-r3")):
                with self.subTest(script=script, case=label):
                    got = self.run_ps(self.pair_guard_ps(DV / script, out_dir))
                    self.assertTrue(got.startswith("REFUSED PAIR_OWNER_SHEET_MUST_STAY_LOCAL"), got)
            with self.subTest(script=script, case="local-relative-is-replaced-by-the-certified-full-path"):
                base = self.clean_base()
                got = self.run_ps(self.pair_guard_ps(DV / script, os.path.join("rel", ".claude-state", "pair")), cwd=base)
                self.assertTrue(got.startswith("ACCEPTED "), got)
                self.assertTrue(self.same_path(got[len("ACCEPTED "):], base / "rel" / ".claude-state" / "pair"), got)
        text = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        guard = text.index("Test-DvLocalReceiptRoot -Path $OutDir")
        self.assertLess(guard, text.index("New-Item -ItemType Directory -Force -Path $OutDir"), "the guard must come before the first write")

    def test_adversary_r3_a2_a_relative_root_is_judged_where_the_writer_resolves_it(self) -> None:
        # adversary A2: the helper judged C:\review\r3\.claude-state\sheets (process directory) while the writer resolved the SAME relative value against the PowerShell location (the share).
        share = self.clean_base() / "agent-share"
        share.mkdir()
        elsewhere = self.clean_base() / "elsewhere"
        elsewhere.mkdir()
        rel = "r3\\.claude-state\\sheets"
        got = self.judge([str(share)], {"rel": rel}, location=share, process_dir=elsewhere)
        self.assertIsNone(got["rel"], "PowerShell location is the venue share: the relative root lands on the share and must be refused")
        got = self.judge([str(share)], {"rel": rel}, location=elsewhere, process_dir=share)
        self.assertTrue(self.same_path(got["rel"], elsewhere / "r3" / ".claude-state" / "sheets"), f"the certified path is resolved against the PowerShell location: {got}")
        # the leg uses the RETURNED value from then on (it replaces -SheetCopyDir), so the value judged is the value written
        leg = DV / "Invoke-VenueLeg.ps1"
        script = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            f"Set-Location -LiteralPath '{psq(elsewhere)}'; [Environment]::CurrentDirectory = '{psq(share)}'\n"
            f"$legText = [IO.File]::ReadAllText('{psq(leg)}')\n"
            "$m = [regex]::Match($legText, '(?s)if \\(-not \\[string\\]::IsNullOrWhiteSpace\\(\\$SheetCopyDir\\)\\) \\{\\s*\\$SheetCopyDir = Test-DvLocalReceiptRoot.*?\\r?\\n\\}')\n"
            "if (-not $m.Success) { throw 'the leg SheetCopyDir guard was not found' }\n"
            f"$SheetCopyDir = '{psq(rel)}'\n"
            ". ([scriptblock]::Create($m.Value))\n"
            "$SheetCopyDir\n"
        )
        self.assertTrue(self.same_path(self.run_ps(script), elsewhere / "r3" / ".claude-state" / "sheets"))

    def test_adversary_r3_a3_a_local_spelling_or_link_that_reaches_a_venue_share_is_refused(self) -> None:
        base = self.clean_base()
        share = base / "agent-share-long-name"
        (share / "inner").mkdir(parents=True)
        other = base / "other-local-dir"
        other.mkdir()
        state = base / ".claude-state"
        state.mkdir()
        links = {}
        for name, target in (("to-share", share), ("to-other", other)):
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(state / name), str(target)], capture_output=True, text=True)
            if made.returncode != 0:
                self.skipTest("cannot create a directory junction: " + made.stdout + made.stderr)
            links[name] = state / name
        cases = {
            "junction-onto-share": str(links["to-share"] / "receipts"),
            "junction-onto-share-subdir": str(links["to-share"] / "inner" / "deeper" / "receipts"),
            # r6 G7 / ADV-A3 (inverted from r4's "stays-allowed"): the junction's target has no .claude-state component, so the location actually written is outside the marker
            "junction-onto-a-local-dir-outside-claude-state-is-refused": str(links["to-other"] / "receipts"),
            "share-extended-spelling": "\\\\?\\" + str(share / ".claude-state" / "r"),
            "share-upper-case-and-trailing-separator": str(share / ".claude-state" / "r").upper() + "\\",
            "share-forward-slashes": str(share / ".claude-state" / "r").replace("\\", "/"),
            "share-dot-dot-detour": str(share / "inner" / ".." / ".claude-state" / "r"),
            "sibling-of-share-stays-allowed": str(base / "agent-share-long-name-2" / ".claude-state" / "r"),
        }
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(share), buf, 1024) and buf.value.lower() != str(share).lower():
            cases["share-8dot3-alias"] = str(Path(buf.value) / ".claude-state" / "r")
        got = self.judge([str(share)], cases)
        for name in ("junction-onto-share", "junction-onto-share-subdir", "share-extended-spelling", "share-upper-case-and-trailing-separator", "share-forward-slashes", "share-dot-dot-detour", "share-8dot3-alias"):
            if name in cases:
                self.assertIsNone(got[name], f"{name}: {cases[name]} reaches the venue share and must be refused")
        self.assertIsNone(got["junction-onto-a-local-dir-outside-claude-state-is-refused"], got)
        self.assertTrue(self.same_path(got["sibling-of-share-stays-allowed"], base / "agent-share-long-name-2" / ".claude-state" / "r"), got)

    def test_adversary_r3_a4_a_device_path_is_refused_everywhere_and_only_the_c_drive_extended_form_is_local(self) -> None:
        bad = {"dot-device": "\\\\.\\C:\\x\\.claude-state\\receipts", "dot-device-forward": "//./C:/x/.claude-state/receipts", "question-forward": "//?/C:/x/.claude-state/receipts",
               "extended-unc": "\\\\?\\UNC\\bachelor\\mlv-agent\\.claude-state\\r", "globalroot": "\\\\?\\GLOBALROOT\\x\\.claude-state\\r", "volume-guid": "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\x\\.claude-state\\r"}
        got = self.judge(["C:\\fake\\agent-share"], {**bad, "extended-c-drive": "\\\\?\\C:\\x\\.claude-state\\receipts"})
        for name in bad:
            self.assertIsNone(got[name], name)
        self.assertEqual(got["extended-c-drive"].lower(), "c:\\x\\.claude-state\\receipts", "the one allowed extended form is certified AND returned as the plain drive path")
        # the batch entry
        for name in ("dot-device", "dot-device-forward"):
            with self.subTest(entry="batch", name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", bad[name])
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_", r.stderr)
        # the real leg entry
        leg = DV / "Invoke-VenueLeg.ps1"
        for name in ("dot-device", "dot-device-forward", "extended-unc"):
            with self.subTest(entry="leg", name=name):
                got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(leg, bad[name]))
                self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)

    def test_adversary_r4_local_roots_still_pass_and_come_back_as_full_paths(self) -> None:
        base = self.clean_base()
        local = str(base / ".claude-state" / "dual-venue" / "receipts")
        cases = {"plain": local, "forward": local.replace("\\", "/"), "extended": "\\\\?\\" + local, "trailing": local + "\\", "relative": "rel\\.claude-state\\receipts"}
        got = self.judge(["C:\\fake\\agent-share"], cases, location=base, process_dir=base)
        for name, path in (("plain", local), ("forward", local), ("extended", local), ("trailing", local), ("relative", base / "rel" / ".claude-state" / "receipts")):
            self.assertTrue(self.same_path(got[name], path), f"{name}: {got[name]}")
            self.assertFalse(got[name].endswith("\\"), got[name])
            self.assertTrue(os.path.isabs(got[name]) and not got[name].startswith("\\\\"), got[name])
        # the real leg entry keeps the in-lease root: certified, and replaces the raw value
        got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", "\\\\?\\" + local))
        self.assertTrue(got_leg.startswith("ACCEPTED "), got_leg)
        self.assertTrue(self.same_path(got_leg[len("ACCEPTED "):], local), got_leg)

    def test_adversary_r4_every_caller_writes_to_the_returned_path_and_no_inline_guard_is_left(self) -> None:
        # the class, not the instance: a call of the helper that does not keep its return value would test one spelling and write another (A2); an inline `.claude-state` split guard is a
        # second, weaker admission rule (A1: New-VenueSheetPair.ps1 and New-VenueFlavorPair.ps1 each carried one). Wait-VenueQuiet.ps1 -GateLog is the one reviewed exception: it is a probe
        # log (not owner footage), it already refuses every venue agentShare, and its alternative scope is the run's own -WorkDir.
        offenders = []
        for p in sorted(DV.glob("*.ps1")):
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                code = line.lstrip()
                if code.startswith("#"):
                    continue
                if "Test-DvLocalReceiptRoot" in line and not re.search(r"\$\w+\s*=\s*(?:\$\(.*)?Test-DvLocalReceiptRoot", line) and "Import-Module" not in line:
                    offenders.append(f"{p.name}:{n}: result not kept: {code[:120]}")
                if re.search(r"Equals\(\$_, '\.claude-state'", line) and p.name != "Wait-VenueQuiet.ps1":
                    offenders.append(f"{p.name}:{n}: inline marker guard: {code[:120]}")
        self.assertEqual(offenders, [])

    def test_adversary_r5_a1_a_raw_unc_or_device_spelling_of_either_slash_is_refused_before_any_normalisation(self) -> None:
        # ADV-A1: the provider turned `\/bachelor\...` into a ONE-separator path and GetFullPath rooted it on the current drive (C:\bachelor\...), so the raw refusal never saw it.
        # The refusal is on the RAW text: two leading separators of EITHER slash is a UNC; the only device spelling kept is `\\?\C:\...` written with backslashes.
        bad = {
            "mixed-bs-fs": "\\/bachelor\\mlv-agent\\.claude-state\\r", "mixed-fs-bs": "/\\bachelor\\mlv-agent\\.claude-state\\r", "forward-unc": "//bachelor/mlv-agent/.claude-state/r",
            "half-forward-unc": "\\\\bachelor/mlv-agent/.claude-state/r", "mixed-device-q": "\\/?\\C:\\x\\.claude-state\\r", "mixed-device-dot": "/\\.\\C:\\x\\.claude-state\\r",
            "forward-device-q": "//?/C:/x/.claude-state/receipts", "forward-device-dot": "//./C:/x/.claude-state/receipts", "half-forward-device": "\\\\?/C:/x/.claude-state/r",
            "nt-prefix": "\\??\\C:\\x\\.claude-state\\r", "nt-prefix-fwd": "/??/C:/x/.claude-state/r", "extended-unc-fwd": "//?/UNC/bachelor/mlv-agent/.claude-state/r",
            "admin-share": "\\\\localhost\\c$\\x\\.claude-state\\r",
        }
        got = self.judge(["C:\\fake\\agent-share"], {**bad, "extended-c-drive-still-local": "\\\\?\\C:\\x\\.claude-state\\receipts"})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {bad[name]!r} was certified as {got[name]!r}")
        self.assertEqual(got["extended-c-drive-still-local"].lower(), "c:\\x\\.claude-state\\receipts")
        # the batch entry hands the RAW value to the helper (no normalisation of its own first): the forward-slash device spelling is refused there too
        for name in ("forward-device-q", "mixed-bs-fs", "forward-unc"):
            with self.subTest(entry="batch", name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", bad[name])
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_", r.stderr)
        for name in ("forward-device-q", "mixed-bs-fs", "nt-prefix"):
            with self.subTest(entry="leg", name=name):
                got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", bad[name]))
                self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)
        batch_text = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertNotIn("GetUnresolvedProviderPathFromPSPath", batch_text, "the batch guard must not normalise -ReceiptRoot before the one helper judges the raw spelling")
        # a provider-qualified or drive-relative spelling stays refused (the helper resolves a relative value through the file system only)
        base = self.clean_base()
        odd = {"registry": "HKLM:\\Software\\.claude-state\\r", "env-drive": "Env:\\x\\.claude-state\\r", "drive-relative": "C:rel\\.claude-state\\r",
               "provider-qualified": "Microsoft.PowerShell.Core\\FileSystem::" + str(base / ".claude-state" / "r")}
        got = self.judge(["C:\\fake\\agent-share"], odd, location=base, process_dir=base)
        for name in odd:
            self.assertIsNone(got[name], f"{name}: {odd[name]!r} was certified as {got[name]!r}")

    def test_adversary_r5_a2_a_path_equal_to_or_under_a_share_is_refused_across_links_and_extended_spellings(self) -> None:
        # ADV-A2: the share keys got lexical normalisation only, while the candidate had its extended prefix removed and its links followed, so a share named through a link
        # (C:\Users\All Users -> C:\ProgramData) or through `\\?\C:\x` never matched the candidate that sits under it.
        base = self.clean_base()
        real = base / "real-share-dir"
        (real / "inner").mkdir(parents=True)
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(base / "share-link"), str(real)], capture_output=True, text=True)
        if made.returncode != 0:
            self.skipTest("cannot create a directory junction: " + made.stdout + made.stderr)
        elsewhere = base / "elsewhere"
        elsewhere.mkdir()
        cand_real = str(real / ".claude-state" / "r")
        cand_under = str(real / "inner" / "deeper" / ".claude-state" / "r")
        cand_other = str(elsewhere / ".claude-state" / "r")
        # the share is NAMED through the junction; the candidate is spelled with the junction's target
        got = self.judge([str(base / "share-link")], {"equal-via-link-share": cand_real, "under-via-link-share": cand_under, "elsewhere": cand_other})
        self.assertIsNone(got["equal-via-link-share"], got)
        self.assertIsNone(got["under-via-link-share"], got)
        self.assertTrue(self.same_path(got["elsewhere"], cand_other), got)
        # the share is spelled with the extended prefix / forward slashes / device prefix; the candidate is plain
        for label, share in (("extended-share", "\\\\?\\" + str(real)), ("forward-share", str(real).replace("\\", "/")), ("trailing-share", str(real) + "\\"), ("upper-share", str(real).upper())):
            got = self.judge([share], {"cand": cand_real, "under": cand_under, "elsewhere": cand_other})
            self.assertIsNone(got["cand"], f"{label}: {got}")
            self.assertIsNone(got["under"], f"{label}: {got}")
            self.assertTrue(self.same_path(got["elsewhere"], cand_other), f"{label}: {got}")
        # the candidate itself goes through the junction onto the share (r4 A3, kept)
        got = self.judge([str(real)], {"via-link": str(base / "share-link" / ".claude-state" / "r")})
        self.assertIsNone(got["via-link"], got)

    def test_adversary_r5_g5_the_helper_returns_the_resolved_path_it_judged(self) -> None:
        # Grok G5: the helper judged the link-resolved path and returned the original spelling, so a writer followed the link on its own. The value checked is the value written.
        # r6 G7 / Dot 6546717175 / ADV-A3 (INVERTED): a junction target with no .claude-state component is REFUSED (r5 certified it, outside the marker); the resolved-path return is kept
        # for a target that is itself under .claude-state.
        base = self.clean_base()
        target = base / "link-target-dir"
        target.mkdir()
        marked = base / "marked" / ".claude-state" / "link-target-dir"
        marked.mkdir(parents=True)
        state = base / ".claude-state"
        state.mkdir()
        for name, tgt in (("to-target", target), ("to-marked", marked)):
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(state / name), str(tgt)], capture_output=True, text=True)
            if made.returncode != 0:
                self.skipTest("cannot create a directory junction: " + made.stdout + made.stderr)
        got = self.judge(["C:\\fake\\agent-share"], {"via-junction": str(state / "to-target" / "receipts"), "via-junction-marked": str(state / "to-marked" / "receipts"),
                                                     "plain": str(state / "plain" / "receipts")})
        self.assertIsNone(got["via-junction"], f"the junction's target is outside every .claude-state component: {got}")
        # compared by real path (a runner's temp dir is spelled with an 8.3 short name, e.g. RUNNER~1, while the link target comes back in long form), plus the junction's own name must be gone
        self.assertTrue(self.same_path(got["via-junction-marked"], marked / "receipts"), f"the certified path is the junction's TARGET: {got}")
        self.assertNotIn("to-marked", got["via-junction-marked"].lower(), f"the certified path must not keep the spelling that reached the junction: {got}")
        self.assertTrue(self.same_path(got["plain"], state / "plain" / "receipts"), got)

    def test_adversary_r5_g6_the_real_leg_strip_dir_is_the_certified_spelling_before_any_branch_uses_it(self) -> None:
        # Grok G6: `$stripDir = $stripCert` ran only inside the primary-strip branch, so the self-heal loop (which tests `$stripCert`, not the primary strip) could write to the UNCERTIFIED spelling.
        text = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        cert = text.index("$stripCert = $(if ($OfflineTestMode)")
        assign = re.search(r"if \(\$stripCert\) \{ \$stripDir = \$stripCert \}", text[cert:])
        self.assertIsNotNone(assign, "the certified strip dir must replace $stripDir as soon as it is certified")
        primary = text.index("if ($receipt.visual.stripPath -and $stripCert)", cert)
        self.assertLess(cert + assign.start(), primary, "the replacement must come BEFORE the primary-strip branch")
        for m in re.finditer(r"Join-Path \$stripDir ", text):
            self.assertGreater(m.start(), cert + assign.start(), "no strip copy path may be built from $stripDir before it is the certified spelling")

    # ---- r6 CONVERGENCE (Grok G7 6546677256 = Dot 6546717175 = r5 ADV-A3; r5 ADV-A1, ADV-A2, ADV-A4): ONE invariant, judged on the spelling AND on the FINAL link-resolved path,
    # which is the path returned and the path written. Fixtures are real junctions under a clean base OUTSIDE .claude-state.
    def make_junction(self, link: Path, target: Path) -> Path:
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if made.returncode != 0:
            self.skipTest("cannot create a directory junction: " + made.stdout + made.stderr)
        return link

    @staticmethod
    def files_in(d: Path) -> list:
        return sorted(str(p) for p in d.rglob("*")) if d.exists() else []

    def link_fixture(self) -> dict:
        base = self.clean_base()
        outside = base / "published"   # no .claude-state component anywhere on its path
        outside.mkdir()
        real = base / "other" / ".claude-state" / "real"   # a target that is itself under .claude-state
        real.mkdir(parents=True)
        state = base / "review" / ".claude-state"
        state.mkdir(parents=True)
        self.make_junction(state / "to-public", outside)
        self.make_junction(state / "to-real", real)
        (base / "cs").mkdir()
        cs = self.make_junction(base / "cs" / ".claude-state", outside)   # a `.claude-state` component that IS a junction onto the outside directory
        return {"base": base, "outside": outside, "real": real, "state": state, "cs": cs}

    def test_r6_g7_a3_the_marker_is_judged_on_the_final_link_resolved_path(self) -> None:
        f = self.link_fixture()
        got = self.judge(["C:\\fake\\agent-share"], {
            "a3-literal-junction-out": str(f["state"] / "to-public" / "sheets"),
            "claude-state-is-a-junction-out": str(f["cs"] / "dual-venue" / "receipts"),
            "junction-onto-a-claude-state-target": str(f["state"] / "to-real" / "sheets"),
        })
        self.assertIsNone(got["a3-literal-junction-out"], f"certified outside the marker: {got}")
        self.assertIsNone(got["claude-state-is-a-junction-out"], f"certified outside the marker: {got}")
        self.assertTrue(self.same_path(got["junction-onto-a-claude-state-target"], f["real"] / "sheets"), got)
        self.assertNotIn("to-real", got["junction-onto-a-claude-state-target"].lower(), got)
        self.assertEqual(self.files_in(f["outside"]), [], "the helper writes nothing")

    def test_r6_every_certificate_re_judges_to_itself(self) -> None:
        # idempotence: helper(helper(x)) == helper(x), so a caller that re-judges a certified value (the leg's strip dir re-judges -SheetCopyDir) accepts its own certificate
        f = self.link_fixture()
        base = f["base"]
        local = str(base / "plain" / ".claude-state" / "dual-venue" / "receipts")
        cases = {"plain": local, "forward": local.replace("\\", "/"), "extended": "\\\\?\\" + local, "trailing": local + "\\", "relative": "rel\\.claude-state\\receipts",
                 "dot-dot": str(base / "plain" / "x" / ".." / ".claude-state" / "r"), "via-junction-onto-a-claude-state-target": str(f["state"] / "to-real" / "sheets")}
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(base), buf, 1024) and buf.value.lower() != str(base).lower():
            cases["short-name-base"] = str(Path(buf.value) / "plain" / ".claude-state" / "r")
        shares = ["C:\\fake\\agent-share"]
        first = self.judge(shares, cases, location=base, process_dir=base)
        for name, value in first.items():
            self.assertIsNotNone(value, f"{name} must be accepted: {first}")
        self.assertEqual(self.judge(shares, first), first, "a certificate must re-judge to itself")

    def leg_strip_block_ps(self, sheet_copy_dir: str, receipt_root: str, evidence_dir: Path, png: Path) -> str:
        """PowerShell that runs the REAL strip-copy and self-heal block of Invoke-VenueLeg.ps1 (its own text, from `$stripDir = $(if` through the self-heal loop) with stub captures."""
        return (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            f"$legText = [IO.File]::ReadAllText('{psq(DV / 'Invoke-VenueLeg.ps1')}')\n"
            "$a = $legText.IndexOf('    $stripDir = $(if')\n"
            "$e = $(if ($a -ge 0) { $legText.IndexOf('Write-Output \"DVE_VISUAL_$($preDir.Name)', $a) } else { -1 })\n"
            "if ($a -lt 0 -or $e -lt 0) { throw 'the leg strip block was not found' }\n"
            "$e = $legText.IndexOf(\"`n\", $e)\n"
            "$block = $legText.Substring($a, $e - $a) + \"`n        }`n    }`n\"\n"
            # CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r12: the block may call a helper the LEG defines (Invoke-DvCollect wraps every copy). Carry each
            # top-level leg function the block names, from the leg's own text, so the block runs as the leg runs it; and any error the block raises
            # fails the test instead of printing a copy path for a copy that never happened (the r11 hosted red: PRIMARY=<path>, 0 PNGs written).
            "$legAst = [System.Management.Automation.Language.Parser]::ParseInput($legText, [ref]$null, [ref]$null)\n"
            "foreach ($fn in @($legAst.FindAll({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false))) {\n"
            "    if ($block -match ('(?<![\\w-])' + [regex]::Escape($fn.Name) + '(?![\\w-])')) { . ([scriptblock]::Create($fn.Extent.Text)) } }\n"
            "function global:Read-DvVisualCaptures { param($Requested, $ArtifactsDir, $EvidenceDir, $LegId, $Header) "
            f"[pscustomobject]@{{ stripPath = '{psq(png)}'; stripCopyPath = $null; verdict = 'OK'; reason = '' }} }}\n"
            f"$receipt = [ordered]@{{ visual = [pscustomobject]@{{ stripPath = '{psq(png)}'; stripCopyPath = $null }} }}\n"
            "$spec = [pscustomobject]@{ legId = 'leg-r6' }; $Venue = 'bachelor'; $Backend = 'cuda'; $SourceCommit = '0123456789abcdef0123'; $runStamp = '20261031T000000Z'; $OfflineTestMode = $false\n"
            f"$SheetCopyDir = '{psq(sheet_copy_dir)}'; $ReceiptRoot = '{psq(receipt_root)}'; $evidenceDir = '{psq(evidence_dir)}'\n"
            "$Error.Clear()\n"
            "$null = . ([scriptblock]::Create($block))\n"
            "if ($Error.Count -gt 0) { throw ('the leg strip block raised: ' + [string]$Error[0]) }\n"
            "'PRIMARY=' + [string]$receipt.visual.stripCopyPath\n"
            "foreach ($k in @($receipt['selfHealVisual'].Keys)) { 'SELFHEAL ' + $k + '=' + [string]$receipt['selfHealVisual'][$k].stripCopyPath }\n"
        )

    def strip_inputs(self, base: Path) -> tuple:
        png = base / "captures" / "strip.png"
        png.parent.mkdir(parents=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\nr6")
        evidence = base / "ev" / ".claude-state" / "evidence" / "rid"
        (evidence / "self-heal" / "pre-1").mkdir(parents=True)
        return png, evidence

    def test_r6_g7_the_real_leg_strip_copy_and_self_heal_write_nothing_through_a_marker_junction_onto_an_outside_dir(self) -> None:
        f = self.link_fixture()
        png, evidence = self.strip_inputs(f["base"])
        # (1) -SheetCopyDir through the junction: the leg's own entry guard throws before anything
        bad_sheets = str(f["state"] / "to-public" / "sheets")
        guard = self.run_ps(
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            f"$legText = [IO.File]::ReadAllText('{psq(DV / 'Invoke-VenueLeg.ps1')}')\n"
            "$m = [regex]::Match($legText, '(?s)if \\(-not \\[string\\]::IsNullOrWhiteSpace\\(\\$SheetCopyDir\\)\\) \\{\\s*\\$SheetCopyDir = Test-DvLocalReceiptRoot.*?\\r?\\n\\}')\n"
            "if (-not $m.Success) { throw 'the leg SheetCopyDir guard was not found' }\n"
            f"$SheetCopyDir = '{psq(bad_sheets)}'\n"
            "try { . ([scriptblock]::Create($m.Value)); 'ACCEPTED ' + $SheetCopyDir } catch { 'REFUSED ' + $_.Exception.Message }\n")
        self.assertTrue(guard.startswith("REFUSED DVE_SHEET_COPY_MUST_STAY_LOCAL"), guard)
        # (2) the strip block itself, primary strip AND self-heal pre-launch, by both routes: -SheetCopyDir given, and the default <receipts>\..\sheets beside a junctioned root
        for label, sheet_dir, root in (("sheet-copy-dir", bad_sheets, str(f["base"] / "x" / ".claude-state" / "receipts")),
                                       ("default-beside-root", "", str(f["state"] / "to-public" / "receipts"))):
            with self.subTest(route=label):
                out = self.run_ps(self.leg_strip_block_ps(sheet_dir, root, evidence, png))
                self.assertIn("PRIMARY=\n", out + "\n", out)
                self.assertIn("SELFHEAL pre-1=", out)
                self.assertNotRegex(out, r"SELFHEAL pre-1=\S", out)
                self.assertEqual(self.files_in(f["outside"]), [], f"{label}: owner strip images were written outside .claude-state")

    def test_r6_g7_the_real_leg_strip_copy_writes_through_a_junction_whose_target_is_under_claude_state(self) -> None:
        f = self.link_fixture()
        png, evidence = self.strip_inputs(f["base"])
        out = self.run_ps(self.leg_strip_block_ps("", str(f["state"] / "to-real" / "receipts"), evidence, png))
        written = [Path(p) for p in self.files_in(f["base"] / "other") if p.endswith(".png")]
        self.assertEqual(len(written), 2, out)   # the primary strip and the self-heal pre-1 strip
        for p in written:
            self.assertTrue(self.same_path(str(p.parent), f["real"] / "sheets"), written)
        self.assertRegex(out, r"PRIMARY=\S")
        self.assertNotIn("to-real", out.lower(), "the copy path is built from the certificate (the resolved target), not the spelling that reached the junction")

    def test_r6_d_a_batch_whose_claude_state_is_a_junction_onto_an_outside_dir_refuses_before_side_effects(self) -> None:
        f = self.link_fixture()
        r = self.run_batch(None, "-ReceiptRoot", str(f["cs"] / "dual-venue" / "receipts"))
        self.assert_nothing_started(r)
        self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)
        self.assertEqual(self.files_in(f["outside"]), [])
        # the real leg entry refuses the same root
        got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", str(f["cs"] / "dual-venue" / "receipts")))
        self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)

    def test_r6_adv_a1_an_aliased_spelling_of_a_share_is_refused(self) -> None:
        # ADV-A1 literal: AgentShares = the lane worktree; Path = '<worktree> \.claude-state\receipt-r5' (trailing space) and '<worktree>. \...'. Win32 strips a trailing space or dot
        # from a name, so such a component is an alias: every component must be a name the file system writes as spelled.
        wt = str(ROOT)
        got = self.judge([wt], {"literal-trailing-space": wt + " \\.claude-state\\receipt-r5", "literal-dot-space": wt + ". \\.claude-state\\receipt-r5",
                                "trailing-dot": wt + ".\\.claude-state\\receipt-r5", "as-spelled": wt + "\\.claude-state\\receipt-r5"})
        for name, value in got.items():
            self.assertIsNone(value, f"{name}: {got}")
        base = self.clean_base()
        share = base / "lane-share"
        (share / ".claude-state").mkdir(parents=True)
        cases = {"space": str(share) + " \\.claude-state\\r", "dot-space": str(share) + ". \\.claude-state\\r", "space-in-tail": str(base / "x" / ".claude-state" / "r ") + "\\a",
                 "dot-in-tail": str(base / "x" / ".claude-state" / "r. ") + "\\a"}
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(share), buf, 1024) and buf.value.lower() != str(share).lower():
            cases["short-name"] = str(Path(buf.value) / ".claude-state" / "r")
        got = self.judge([str(share)], {**cases, "sibling-stays-allowed": str(base / "lane-share-2" / ".claude-state" / "r")})
        for name in cases:
            self.assertIsNone(got[name], f"{name}: {got}")
        self.assertTrue(self.same_path(got["sibling-stays-allowed"], base / "lane-share-2" / ".claude-state" / "r"), got)

    def test_r6_adv_a2_a_powershell_drive_naming_a_unc_is_refused_in_every_spelling(self) -> None:
        # ADV-A2 literal: a non-persistent FileSystem PSDrive Q: whose Root is \\bachelor\mlv-agent (stubbed, as the adversary did: no share is touched), SheetCopyDir '\\?\Q:\.claude-state\sheets'
        stub = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "function global:Get-PSDrive { [CmdletBinding()] param([string]$Name)\n"
            "  if ($Name -eq 'Q') { return [pscustomobject]@{ Name = 'Q'; Provider = [pscustomobject]@{ Name = 'FileSystem' }; Root = '\\\\bachelor\\mlv-agent\\'; DisplayRoot = '' } }\n"
            "  Microsoft.PowerShell.Management\\Get-PSDrive @PSBoundParameters }\n"
        )
        cases = {"extended-literal": "\\\\?\\Q:\\.claude-state\\sheets", "plain": "Q:\\.claude-state\\sheets", "forward": "Q:/.claude-state/sheets"}
        script = stub + "$o = [ordered]@{}\n" + "".join(f"$o['{k}'] = Test-DvLocalReceiptRoot -Path '{psq(v)}' -AgentShares @('C:\\fake\\agent-share')\n" for k, v in cases.items())
        script += "$o['c-drive-still-local'] = Test-DvLocalReceiptRoot -Path 'C:\\x\\.claude-state\\sheets' -AgentShares @('C:\\fake\\agent-share')\n$o | ConvertTo-Json -Compress\n"
        got = json.loads(self.run_ps(script))
        for name in cases:
            self.assertIsNone(got[name], f"{name}: {got}")
        self.assertEqual(got["c-drive-still-local"].lower(), "c:\\x\\.claude-state\\sheets")
        # the real leg -SheetCopyDir entry, on the literal input
        leg = self.run_ps(stub +
            f"$legText = [IO.File]::ReadAllText('{psq(DV / 'Invoke-VenueLeg.ps1')}')\n"
            "$m = [regex]::Match($legText, '(?s)if \\(-not \\[string\\]::IsNullOrWhiteSpace\\(\\$SheetCopyDir\\)\\) \\{\\s*\\$SheetCopyDir = Test-DvLocalReceiptRoot.*?\\r?\\n\\}')\n"
            "if (-not $m.Success) { throw 'the leg SheetCopyDir guard was not found' }\n"
            "$SheetCopyDir = '\\\\?\\Q:\\.claude-state\\sheets'\n"
            "try { . ([scriptblock]::Create($m.Value)); 'ACCEPTED ' + $SheetCopyDir } catch { 'REFUSED ' + $_.Exception.Message }\n")
        self.assertTrue(leg.startswith("REFUSED DVE_SHEET_COPY_MUST_STAY_LOCAL"), leg)

    def test_r6_adv_a4_an_unparsable_destination_fails_closed_before_any_batch_side_effect(self) -> None:
        # ADV-A4 literal: ReceiptRoot 'C:\x\.claude-state\bad<root>' (also bad|root, bad"root, bad?root); plus DOS device names, which the file system never writes as a directory
        bad = {"lt": "C:\\x\\.claude-state\\bad<root>", "pipe": "C:\\x\\.claude-state\\bad|root", "quote": "C:\\x\\.claude-state\\bad\"root", "question": "C:\\x\\.claude-state\\bad?root",
               "star": "C:\\x\\.claude-state\\bad*root", "control": "C:\\x\\.claude-state\\bad\u0001root", "nul": "C:\\x\\.claude-state\\NUL\\r", "con-ext": "C:\\x\\.claude-state\\con.txt\\r",
               "com1": "C:\\x\\.claude-state\\COM1",
               # r7 G9: Windows also reserves COM and LPT followed by a superscript one, two or three (U+00B9, U+00B2, U+00B3), bare or with an extension
               "com-sup1": "C:\\x\\.claude-state\\COM\u00b9", "com-sup2-ext": "C:\\x\\.claude-state\\com\u00b2.txt\\r", "com-sup3": "C:\\x\\.claude-state\\COM\u00b3",
               "lpt-sup1": "C:\\x\\.claude-state\\LPT\u00b9", "lpt-sup2-lower": "C:\\x\\.claude-state\\lpt\u00b2\\r", "lpt-sup3-ext": "C:\\x\\.claude-state\\LPT\u00b3.log"}
        got = self.judge(["C:\\fake\\agent-share"], {**bad, "fine": "C:\\x\\.claude-state\\good-root"})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {got}")
        self.assertEqual(got["fine"].lower(), "c:\\x\\.claude-state\\good-root")
        base = self.clean_base()
        for name in ("lt", "pipe"):
            with self.subTest(entry="batch", name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(base / ".claude-state" / bad[name].rsplit("\\", 1)[1]))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)
            with self.subTest(entry="leg", name=name):
                got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", bad[name]))
                self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)

    # ---- r8 (PR #729 r7 keys; sol B1/B2/B3, opus+sol A3, fable G9): the helper's component rule and PowerShell-drive rule, stated against the round-1 contract "a bad root takes no batch resources".
    def test_r8_b1_a_dos_device_name_with_spaces_before_the_extension_is_refused_on_every_component(self) -> None:
        # sol B1 literal: RtlIsDosDeviceName_U treats `COM1 .txt`, `NUL .txt`, `LPT<sup2> .log` as devices (a run of spaces between the stem and the extension is dropped). Not only the last component.
        s1, s2, s3 = "\u00b9", "\u00b2", "\u00b3"
        bad = {
            "com1-space-ext-last": "C:\\x\\.claude-state\\COM1 .txt\\receipts", "com1-space-ext-middle": "C:\\x\\.claude-state\\a\\COM1 .txt\\b\\receipts",
            "com1-space-ext-final": "C:\\x\\.claude-state\\COM1 .txt", "com1-two-spaces-ext": "C:\\x\\.claude-state\\COM1   .txt\\r",
            "nul-space-ext": "C:\\x\\.claude-state\\NUL .txt\\receipts", "nul-lower-space-ext": "C:\\x\\.claude-state\\nul  .log\\r",
            "lpt-sup2-space-ext": f"C:\\x\\.claude-state\\LPT{s2} .log\\receipts", "com-sup1-space-ext": f"C:\\x\\.claude-state\\COM{s1} .txt\\receipts", "com-sup3-space-ext": f"C:\\x\\.claude-state\\com{s3} .dat\\r",
            "con-space-ext": "C:\\x\\.claude-state\\CON .x\\r", "prn-space-ext": "C:\\x\\.claude-state\\PRN .x\\r", "aux-space-ext": "C:\\x\\.claude-state\\AUX .x\\r",
            "conin-space-ext": "C:\\x\\.claude-state\\CONIN$ .x\\r", "conout-space-ext": "C:\\x\\.claude-state\\CONOUT$ .x\\r",
            "lpt9-space-dotted-ext": "C:\\x\\.claude-state\\LPT9 .a.b\\r",
        }
        got = self.judge(["C:\\fake\\agent-share"], {**bad, "com10-is-not-a-device": "C:\\x\\.claude-state\\COM10 .txt\\r", "console-is-not-a-device": "C:\\x\\.claude-state\\CONSOLE .txt\\r",
                                                      "fine": "C:\\x\\.claude-state\\good-root"})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {got}")
        self.assertEqual(got["com10-is-not-a-device"].lower(), "c:\\x\\.claude-state\\com10 .txt\\r", got)
        self.assertEqual(got["console-is-not-a-device"].lower(), "c:\\x\\.claude-state\\console .txt\\r", got)
        self.assertEqual(got["fine"].lower(), "c:\\x\\.claude-state\\good-root")
        # the batch entry refuses before the run dir, the claim or a leg; the real leg entry refuses the same root
        base = self.clean_base()
        for label, comp in (("com1-space-ext", "COM1 .txt"), ("nul-space-ext", "NUL .txt")):
            with self.subTest(entry="batch", name=label):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(base / ".claude-state" / comp / "receipts"))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)
            with self.subTest(entry="leg", name=label):
                got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", str(base / ".claude-state" / comp / "receipts")))
                self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)

    def test_r8_g9_a_dos_device_name_as_a_middle_component_is_refused_and_the_superscript_class_is_exercised(self) -> None:
        # fable G9: .NET GetFullPath refuses a superscript device name as the LAST component, so the regex never ran on it; a MIDDLE component reaches the regex
        s1, s2, s3 = "\u00b9", "\u00b2", "\u00b3"
        bad = {"com1-middle": "C:\\x\\.claude-state\\COM1\\r", "lpt3-ext-middle": "C:\\x\\.claude-state\\LPT3.log\\r",
               "com-sup1-middle": f"C:\\x\\.claude-state\\COM{s1}\\r", "com-sup2-middle": f"C:\\x\\.claude-state\\COM{s2}\\r", "com-sup3-middle": f"C:\\x\\.claude-state\\COM{s3}\\r",
               "lpt-sup1-middle": f"C:\\x\\.claude-state\\LPT{s1}\\r", "lpt-sup2-middle": f"C:\\x\\.claude-state\\LPT{s2}\\r", "lpt-sup3-ext-middle": f"C:\\x\\.claude-state\\LPT{s3}.log\\r",
               "com-sup1-ext-middle": f"C:\\x\\.claude-state\\com{s1}.txt\\r", "com-sup3-deep-middle": f"C:\\x\\.claude-state\\a\\COM{s3}\\b\\r"}
        got = self.judge(["C:\\fake\\agent-share"], {**bad, "fine": "C:\\x\\.claude-state\\good-root"})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {got}")
        self.assertEqual(got["fine"].lower(), "c:\\x\\.claude-state\\good-root")

    def test_r8_g9_the_superscript_class_in_the_device_rule_is_all_three_digits_not_one(self) -> None:
        # mutation proof: with the class cut back to U+00B2 only, the middle-component rows above must go red (the rule is read from the module text, not assumed)
        text = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertEqual(text.count("COM[1-9\\u00B9\\u00B2\\u00B3]"), 1, "the device rule's COM class changed shape; update this mutation")   # r12: the digit class starts at 1 (COM0 is not a device)
        mutated = text.replace("COM[1-9\\u00B9\\u00B2\\u00B3]", "COM[1-9\\u00B2]").replace("LPT[1-9\\u00B9\\u00B2\\u00B3]", "LPT[1-9\\u00B2]")
        self.assertNotEqual(mutated, text)
        d = Path(tempfile.mkdtemp(prefix="venue-rr-mut-", dir=self.clean_base()))
        shutil.copy2(DV / "venues.json", d / "venues.json")
        (d / "DualVenueRunner.psm1").write_text(mutated, encoding="utf-8")
        script = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" f"Import-Module '{psq(d / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                  "$o = [ordered]@{}\n"
                  + "".join(f"$o['{k}'] = [bool](Test-DvLocalReceiptRoot -Path ('C:\\x\\.claude-state\\COM' + [char]0x{c:04X} + '\\r') -AgentShares @('C:\\fake'))\n"
                            for k, c in (("com-sup1", 0xB9), ("com-sup3", 0xB3)))
                  + "$o | ConvertTo-Json -Compress\n")
        got = json.loads(self.run_ps(script))
        self.assertEqual(got, {"com-sup1": True, "com-sup3": True}, f"the mutated rule still refused a superscript device name (it should accept U+00B9/U+00B3 once cut back): {got}")

    def test_r9_b1_a_component_that_ends_in_a_dot_or_a_space_is_refused_on_the_raw_spelling_terminal_and_middle(self) -> None:
        # sol r8 B1 literals: GetFullPath strips a trailing dot / space, so the r8 `EndsWith` check ran on an already-normalised name and never fired. The RAW components are judged before any normalisation.
        pre = "C:\\x\\.claude-state\\"
        bad = {
            "terminal-dot": pre + "receipts.", "terminal-space": pre + "receipts ", "middle-dot": pre + "r.\\receipts", "middle-space": pre + "r \\receipts",
            "terminal-two-dots": pre + "receipts..", "terminal-three-dots": pre + "receipts...", "terminal-dot-trailing-sep": pre + "receipts.\\",
            "forward-slash-middle-dot": "C:/x/.claude-state/r./receipts", "extended-prefix-middle-dot": "\\\\?\\C:\\x\\.claude-state\\r.\\receipts",
            "marker-component-with-dot": "C:\\x\\.claude-state.\\receipts", "outer-component-with-space": "C:\\x \\.claude-state\\receipts",
            "dotted-before-parent-nav": pre + "r.\\..\\receipts",
        }
        controls = {"fine": pre + "good-root", "dot-nav": pre + "a\\.\\r", "dotdot-nav": pre + "a\\..\\r", "inner-dot-name": pre + "r.x\\receipts", "dotted-extension-name": pre + "receipts.d"}
        got = self.judge(["C:\\fake\\agent-share"], {**bad, **controls})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {got}")
        self.assertEqual(got["fine"].lower(), "c:\\x\\.claude-state\\good-root")
        self.assertEqual(got["dot-nav"].lower(), "c:\\x\\.claude-state\\a\\r", got)
        self.assertEqual(got["dotdot-nav"].lower(), "c:\\x\\.claude-state\\r", got)
        self.assertEqual(got["inner-dot-name"].lower(), "c:\\x\\.claude-state\\r.x\\receipts", got)
        self.assertEqual(got["dotted-extension-name"].lower(), "c:\\x\\.claude-state\\receipts.d", got)
        # a relative spelling is judged raw too (resolved against the PowerShell location only after the components are checked)
        loc = self.clean_base() / ".claude-state"
        loc.mkdir()
        rel = self.judge(["C:\\fake\\agent-share"], {"rel-middle-dot": "r.\\receipts", "rel-terminal-dot": "receipts.", "rel-fine": "receipts"}, location=loc, process_dir=loc)
        self.assertIsNone(rel["rel-middle-dot"], rel)
        self.assertIsNone(rel["rel-terminal-dot"], rel)
        # r10 C2: the helper returns the link-resolved (8.3-expanded) path it judged, and a hosted runner's temp dir is the short `RUNNER~1` spelling of `runneradmin`: compare the resolved long
        # path of BOTH sides (lower-casing is not file-system identity)
        self.assertEqual(os.path.normcase(os.path.realpath(rel["rel-fine"])), os.path.normcase(os.path.join(os.path.realpath(str(loc)), "receipts")), rel)
        # the batch entry refuses before the run dir, the claim or a leg; the real leg entry refuses the same root
        base = self.clean_base()
        for label, rootp in (("terminal-dot", base / ".claude-state" / "receipts."), ("middle-dot", base / ".claude-state" / "r." / "receipts")):
            with self.subTest(entry="batch", name=label):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(rootp))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)
            with self.subTest(entry="leg", name=label):
                got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", str(rootp)))
                self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)

    def _tr_tr_child(self, body: str) -> dict:
        """Run `body` in a FRESH child pwsh whose thread culture is tr-TR BEFORE the module (and so any regex it builds) exists; a regex built under en-US is cached and would hide the defect."""
        script = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
                  "[System.Threading.Thread]::CurrentThread.CurrentCulture = 'tr-TR'\n[System.Threading.Thread]::CurrentThread.CurrentUICulture = 'tr-TR'\n"
                  f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                  "$o = [ordered]@{ culture = [Globalization.CultureInfo]::CurrentCulture.Name }\n" + body + "$o | ConvertTo-Json -Compress\n")
        return json.loads(self.run_ps(script))

    def test_r9_b2_the_dos_device_refusal_does_not_depend_on_the_process_culture(self) -> None:
        # sol r8 B2 literals: under tr-TR the inline `(?i:...)` folded `i` to U+0130, so lowercase `conin$` and `conin$ .txt` were not devices; uppercase `CONIN$` was (the control).
        pre = "C:\\x\\.claude-state\\"
        names = {"conin-lower": "conin$", "conin-lower-space-ext": "conin$ .txt", "conin-upper": "CONIN$", "conin-mixed": "Conin$", "conout-lower": "conout$", "conout-upper-space-ext": "CONOUT$ .x",
                 "nul-lower": "nul", "con-lower": "con", "aux-lower": "aux", "prn-lower": "prn", "com1-lower-space-ext": "com1 .txt", "lpt-upper-middle": "LPT9"}
        body = ("$cases = '" + psq(json.dumps({k: pre + v + "\\r" for k, v in names.items()}))
                + "' | ConvertFrom-Json\nforeach ($p in $cases.PSObject.Properties) { $o[$p.Name] = [bool](Test-DvLocalReceiptRoot -Path ([string]$p.Value) -AgentShares @('C:\\fake\\agent-share')) }\n"
                + "$o['fine'] = [bool](Test-DvLocalReceiptRoot -Path 'C:\\x\\.claude-state\\good-root' -AgentShares @('C:\\fake\\agent-share'))\n"
                + "$o['console-not-a-device'] = [bool](Test-DvLocalReceiptRoot -Path 'C:\\x\\.claude-state\\console\\r' -AgentShares @('C:\\fake\\agent-share'))\n")
        got = self._tr_tr_child(body)
        self.assertEqual(got["culture"], "tr-TR", got)
        for name in names:
            self.assertFalse(got[name], f"{name}: a DOS device name was certified under tr-TR: {got}")
        self.assertTrue(got["fine"], got)
        self.assertTrue(got["console-not-a-device"], got)

    def test_r9_b2_the_consent_path_shaped_refusal_does_not_depend_on_the_process_culture(self) -> None:
        # the same class at DualVenueRunner.psm1 (Read-DvClipConsent): `(?i)[a-z]:` under tr-TR does not match the capital I drive letter, so `I:\clip` was not "path-shaped"
        body = ("$t = [pscustomobject]@{ venues = [pscustomobject]@{} }\n"
                "foreach ($txt in @('I:\\a', 'i:\\a', 'C:\\a', 'Z:\\a')) { $o[$txt] = [string](Read-DvClipConsent -Text $txt -Table $t).reason }\n")   # the path-shape check runs before the JSON parse, so a bare value is enough (and no double quote for the command line to mangle)
        got = self._tr_tr_child(body)
        for k, v in got.items():
            if k != "culture":
                self.assertIn("path-shaped", v, f"{k}: {got}")

    def test_r9_b2_the_runner_output_name_match_does_not_depend_on_the_process_culture(self) -> None:
        # the same class at Invoke-VenueLeaseBatch.ps1 (the runner-owned leaf names): `-imatch` of `quiet-...` under tr-TR missed the upper-case spelling the file system treats as the same file
        text = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertNotIn("-imatch $p", text, "the runner-owned leaf match is a culture-dependent -imatch")
        opt = "[System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor [System.Text.RegularExpressions.RegexOptions]::CultureInvariant"
        self.assertEqual(text.count(f"[regex]::IsMatch($c.Name, $p, {opt})"), 2, "both leaf-name matches go through the culture-invariant option")
        self.assertEqual(text.count(f"[regex]::IsMatch($leaf, $p, {opt})"), 1, "the leaf match in the same guard goes through it too")
        # r10 C1: the claim guard runs these functions in a separate runspace that never receives a $script: variable, so the option is spelled out at the call (a $null third argument has no IsMatch overload)
        self.assertNotIn("$script:DvCultureFreeIgnoreCase", text, "no $script: option variable: the guard worker runspace would see $null")
        pat = "'^quiet-.+\\.out\\.txt\\z'"
        script = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n[System.Threading.Thread]::CurrentThread.CurrentCulture = 'tr-TR'\n"
                  "$opt = [Text.RegularExpressions.RegexOptions]'IgnoreCase,CultureInvariant'\n"
                  f"$o = [ordered]@{{ upper = [regex]::IsMatch('QUIET-A.OUT.TXT', {pat}, $opt); lower = [regex]::IsMatch('quiet-a.out.txt', {pat}, $opt) }}\n$o | ConvertTo-Json -Compress\n")
        self.assertEqual(json.loads(self.run_ps(script)), {"upper": True, "lower": True})

    def test_r8_b2_a_powershell_drive_rooted_in_a_subdirectory_of_its_own_letter_is_not_certified(self) -> None:
        # sol B2 literal, run for real in a CHILD pwsh over a temp directory: the C PSDrive is re-rooted so PowerShell cmdlets resolve `C:\x\...` UNDER that directory (here a venue agentShare), while the
        # helper certified the literal `C:\x\...`. Nothing is created: the helper only reads. The drive root must be exactly `<letter>:\`.
        base = self.clean_base()
        share = base / "agent-share"
        share.mkdir()
        letter = os.environ.get("SystemDrive", "C:")[0]
        script = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "$o = [ordered]@{}\n"
            f"$o['before'] = [bool](Test-DvLocalReceiptRoot -Path '{letter}:\\x\\.claude-state\\sheets' -AgentShares @('{psq(share)}'))\n"
            "[void](1 | ConvertTo-Json)\n"   # preload the cmdlet: once the drive is re-rooted, command discovery on it fails
            "Set-Location Env:\\\n"
            f"Remove-PSDrive -Name {letter} -Force -ErrorAction Stop\n"
            f"New-PSDrive -Name {letter} -PSProvider FileSystem -Root '{psq(share)}' -Scope Global -ErrorAction Stop | Out-Null\n"
            f"$o['writerResolves'] = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath('{letter}:\\x\\.claude-state\\sheets')\n"
            f"$o['rerooted'] = [bool](Test-DvLocalReceiptRoot -Path '{letter}:\\x\\.claude-state\\sheets' -AgentShares @('{psq(share)}'))\n"
            f"$o['rerootedAnyShare'] = [bool](Test-DvLocalReceiptRoot -Path '{letter}:\\x\\.claude-state\\sheets' -AgentShares @('{letter}:\\fake\\agent-share'))\n"
            f"$o['rerootedExtended'] = [bool](Test-DvLocalReceiptRoot -Path '\\\\?\\{letter}:\\x\\.claude-state\\sheets' -AgentShares @('{letter}:\\fake\\agent-share'))\n"
            f"$o['rerootedForward'] = [bool](Test-DvLocalReceiptRoot -Path '{letter}:/x/.claude-state/sheets' -AgentShares @('{letter}:\\fake\\agent-share'))\n"
            "$o | ConvertTo-Json -Compress\n"
        )
        got = json.loads(self.run_ps(script))
        self.assertTrue(got["before"], f"the control: an un-remapped drive certifies the path: {got}")
        self.assertTrue(got["writerResolves"].lower().startswith(str(share).lower()), f"the fixture did not re-root the drive: {got}")
        for k in ("rerooted", "rerootedAnyShare", "rerootedExtended", "rerootedForward"):
            self.assertFalse(got[k], f"{k}: a drive rooted in a subdirectory of its own letter was certified: {got}")

    def test_r8_b2_the_drive_root_must_be_exactly_the_letter_root_in_any_case(self) -> None:
        stub = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "$script:rootFor = 'C:\\'\n"
            "function global:Get-PSDrive { [CmdletBinding()] param([string]$Name)\n"
            "  if ($Name -eq 'C') { return [pscustomobject]@{ Name = 'C'; Provider = [pscustomobject]@{ Name = 'FileSystem' }; Root = $global:r8Root; DisplayRoot = '' } }\n"
            "  Microsoft.PowerShell.Management\\Get-PSDrive @PSBoundParameters }\n"
            "$o = [ordered]@{}\n"
        )
        for key, root in (("exact", "C:\\"), ("lower", "c:\\"), ("windows", "C:\\Windows\\"), ("windows-bare", "C:\\Windows"), ("one-deep", "C:\\x"), ("backslash-less", "C:")):
            stub += f"$global:r8Root = '{root}'; $o['{key}'] = [bool](Test-DvLocalReceiptRoot -Path 'C:\\x\\.claude-state\\sheets' -AgentShares @('C:\\fake\\agent-share'))\n"
        got = json.loads(self.run_ps(stub + "$o | ConvertTo-Json -Compress\n"))
        self.assertEqual(got, {"exact": True, "lower": True, "windows": False, "windows-bare": False, "one-deep": False, "backslash-less": False})

    def test_r8_b3_a_component_longer_than_the_volume_maximum_is_refused_before_any_side_effect(self) -> None:
        # sol B3 literal: a 256-character component is certified though GetVolumeInformationW reports 255 for the volume (such a directory cannot be created)
        drive = os.environ.get("SystemDrive", "C:") + "\\"
        buf = ctypes.c_uint32(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0, None, ctypes.byref(buf), None, None, 0)
        limit = min(255, buf.value) if ok and buf.value else 255
        pre = f"{drive}x\\.claude-state\\"
        astral = "\U0001F600"   # two UTF-16 code units
        bad = {"over-last": pre + "a" * (limit + 1) + "\\receipts", "over-middle": pre + "a" * (limit + 1) + "\\inner\\receipts", "over-final": pre + "a" * (limit + 1),
               "over-by-surrogates": pre + astral * (limit // 2 + 1) + "\\r",
               "far-over": pre + "a" * 5000 + "\\r"}
        edge = {"at-limit-last": pre + "b" * limit + "\\receipts", "at-limit-final": pre + "b" * limit, "at-limit-surrogates": pre + astral * (limit // 2) + "\\r"}
        got = self.judge(["C:\\fake\\agent-share"], {**bad, **edge})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {str(got)[:300]}")
        for name in edge:
            self.assertIsNotNone(got[name], f"{name} (a name of exactly the volume's maximum is legal): {str(got)[:300]}")
        base = self.clean_base()
        with self.subTest(entry="batch"):
            self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
            r = self.run_batch(None, "-ReceiptRoot", str(base / ".claude-state" / ("a" * (limit + 1)) / "receipts"))
            self.assert_nothing_started(r)
            self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)
        with self.subTest(entry="leg"):
            got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", str(base / ".claude-state" / ("a" * (limit + 1)) / "receipts")))
            self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)

    def test_r11_a_malformed_original_component_followed_by_parent_navigation_is_refused_on_every_entry(self) -> None:
        # Dot (pr-729.md) / sol 6559605502 / adversary A1 (r10 e0104f07): GetFullPath removes `bad\..` BEFORE the character, device and length checks ran, so a malformed ORIGINAL component
        # followed by `..` was certified (leg ACCEPTED, batch PASSED_BEFORE_RUNDIR) while the same component without the `..` refused. Every ORIGINAL component is judged before any normalisation.
        drive = os.environ.get("SystemDrive", "C:") + "\\"
        buf = ctypes.c_uint32(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0, None, ctypes.byref(buf), None, None, 0)
        limit = min(255, buf.value) if ok and buf.value else 255
        classes = {
            "bad-root-angle": "bad<root>", "bad-root-quote": 'bad"root', "bad-root-pipe": "bad|root", "bad-root-star": "bad*root", "bad-root-question": "bad?root", "bad-root-colon": "bad:root",
            "device-nul": "NUL", "device-con-ext": "CON.txt", "device-com1-space-ext": "COM1 .txt", "device-superscript": "COM\u00b9", "device-conin": "conin$",
            "over-volume-max": "a" * (limit + 1),
            "control-x01": "ctl\x01x", "control-x1f": "ctl\x1fx", "control-tab": "ctl\tx", "control-newline": "ctl\nx", "nul-char": "n\x00x",
        }
        pre = "C:\\x\\.claude-state\\"
        bad: dict[str, str] = {}
        for name, comp in classes.items():
            bad[f"{name}/after-marker"] = pre + comp + "\\..\\receipts"
            bad[f"{name}/after-marker-forward-slash"] = "C:/x/.claude-state/" + comp + "/../receipts"
            bad[f"{name}/nested-then-two-parents"] = pre + "a\\" + comp + "\\..\\..\\receipts"
            bad[f"{name}/before-marker"] = "C:\\x\\" + comp + "\\..\\.claude-state\\receipts"
            bad[f"{name}/dot-then-parent"] = pre + comp + "\\.\\..\\receipts"
            bad[f"{name}/terminal-with-parent"] = pre + "receipts\\" + comp + "\\.."
            bad[f"{name}/extended-prefix"] = "\\\\?\\" + pre + comp + "\\..\\receipts"
        bad["far-over-volume-max/after-marker"] = pre + "a" * 5000 + "\\..\\receipts"   # one case only: the judge script travels on the command line
        controls = {
            "fine": pre + "good-root", "parent-nav-over-a-good-name": pre + "a\\..\\receipts", "dot-nav": pre + "a\\.\\r",
            "com10-is-not-a-device": pre + "COM10\\..\\receipts", "console-is-not-a-device": pre + "CONSOLE\\..\\receipts",
            "at-limit-then-parent": pre + "b" * limit + "\\..\\receipts", "ordinary-dotted-name": pre + "r.x\\..\\receipts",
        }
        got = self.judge(["C:\\fake\\agent-share"], {**bad, **controls})
        for name in bad:
            self.assertIsNone(got[name], f"{name}: {str(got[name])[:200]}")
        self.assertEqual(got["fine"].lower(), "c:\\x\\.claude-state\\good-root")
        self.assertEqual(got["parent-nav-over-a-good-name"].lower(), "c:\\x\\.claude-state\\receipts")
        self.assertEqual(got["dot-nav"].lower(), "c:\\x\\.claude-state\\a\\r")
        self.assertEqual(got["com10-is-not-a-device"].lower(), "c:\\x\\.claude-state\\receipts")
        self.assertEqual(got["console-is-not-a-device"].lower(), "c:\\x\\.claude-state\\receipts")
        self.assertEqual(got["at-limit-then-parent"].lower(), "c:\\x\\.claude-state\\receipts")
        self.assertEqual(got["ordinary-dotted-name"].lower(), "c:\\x\\.claude-state\\receipts")
        # a RELATIVE spelling (resolved once against the PowerShell location) is judged component by component too
        here = self.clean_base()
        rel_bad = {f"{n}/relative": ".claude-state\\" + c + "\\..\\receipts" for n, c in classes.items() if n in ("bad-root-angle", "device-nul", "over-volume-max", "control-x01", "nul-char")}
        rel_bad["bad-root-angle/rooted-on-current-drive"] = "\\" + str(here)[3:] + "\\.claude-state\\bad<root>\\..\\receipts"
        rel_ctl = {"relative-fine": ".claude-state\\a\\..\\receipts"}
        got_rel = self.judge(["C:\\fake\\agent-share"], {**rel_bad, **rel_ctl}, location=here, process_dir=here)
        for name in rel_bad:
            self.assertIsNone(got_rel[name], f"{name}: {str(got_rel[name])[:200]}")
        self.assertIsNotNone(got_rel["relative-fine"], got_rel)
        # the batch entry (Invoke-VenueLeaseBatch.ps1 -ReceiptRoot) refuses before the run dir, the claim or any leg; the real leg entry (Invoke-VenueLeg.ps1) refuses the same root.
        # argv cannot carry a NUL character, so that class is judged by the helper above only.
        base = self.clean_base()
        entries = {k: v for k, v in classes.items() if k in ("bad-root-angle", "device-nul", "over-volume-max", "control-x01", "control-x1f", "device-com1-space-ext")}
        for label, comp in entries.items():
            root = str(base) + "\\.claude-state\\" + comp + "\\..\\receipts"
            with self.subTest(entry="batch", name=label):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", root)
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)
            with self.subTest(entry="leg", name=label):
                got_leg = self.run_ps(f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n" + leg_receipt_root_guard_ps(DV / "Invoke-VenueLeg.ps1", root))
                self.assertTrue(got_leg.startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), got_leg)
        # the same root with the malformed component REMOVED is certified (the refusal is the component, not the `..`)
        with self.subTest(entry="batch", name="control-good-component-then-parent"):
            self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
            r = self.run_batch(None, "-ReceiptRoot", str(base) + "\\.claude-state\\a\\..\\receipts")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # ---- r12 (PR #729 r11 keys; sol INV-5 blocker): the device rule is the Windows reserved set exactly. COM0 and LPT0 are NOT devices (RtlIsDosDeviceName_U = 0), so a root that
    # names one (bare, with an extension, with spaces before the extension, followed by `..`) is ordinary and certifies; COM1-COM9, LPT1-LPT9 and the superscript forms still refuse.
    def test_r12_com0_and_lpt0_are_ordinary_names_and_certify_on_every_entry(self) -> None:
        pre = "C:\\x\\.claude-state\\"
        legal = {"com0-then-parent": pre + "COM0\\..\\receipts", "lpt0-then-parent": pre + "LPT0\\..\\receipts",
                 "com0-ext-then-parent": pre + "COM0.txt\\..\\receipts", "lpt0-space-ext-then-parent": pre + "LPT0 .txt\\..\\receipts"}   # sol r11's four literal inputs
        legal_kept = {"com0-middle": pre + "COM0\\r", "lpt0-lower-middle": pre + "lpt0\\r", "com0-terminal": pre + "COM0", "lpt0-ext-terminal": pre + "LPT0.log"}
        refused = {"com1-then-parent": pre + "COM1\\..\\receipts", "lpt9-then-parent": pre + "LPT9\\..\\receipts", "nul-then-parent": pre + "NUL\\..\\receipts",
                   "con-ext-then-parent": pre + "CON.txt\\..\\receipts", "com1-middle": pre + "COM1\\r", "lpt9-middle": pre + "LPT9\\r", "nul-middle": pre + "NUL\\r",
                   "con-ext-middle": pre + "CON.txt\\r", "com-sup1-then-parent": pre + "COM\u00b9\\..\\receipts", "lpt-sup3-space-ext-then-parent": pre + "LPT\u00b3 .log\\..\\receipts",
                   # r11 is not weakened: a malformed component followed by `..` still refuses
                   "ctl-then-parent": pre + "ctl\x01x\\..\\receipts", "over-max-then-parent": pre + "a" * 256 + "\\..\\receipts", "bad-root-then-parent": pre + "bad<root>\\..\\receipts"}
        got = self.judge(["C:\\fake\\agent-share"], {**legal, **legal_kept, **refused})
        for name in legal:
            self.assertEqual(str(got[name]).lower(), "c:\\x\\.claude-state\\receipts", f"{name}: a non-device name was refused: {got[name]}")
        for name, value in legal_kept.items():
            self.assertEqual(str(got[name]).lower(), value.lower(), f"{name}: {got[name]}")
        for name in refused:
            self.assertIsNone(got[name], f"{name}: {str(got[name])[:200]}")
        # r13: the rule is judged against the DOCUMENTED reserved set (host-independent; the native classifier corroborates in its own method below, so a host whose
        # RtlIsDosDeviceName_U differs can no longer stop the caller sweep and the batch runs after this point). The oracle's own positive and negative controls first.
        self.assertTrue(all(documented_dos_device(n) for n in ("COM1", "LPT9", "COM\u00b9", "LPT\u00b3 .log", "NUL", "CON.txt", "aux", "PRN.tar.gz")))
        self.assertFalse(any(documented_dos_device(n) for n in ("COM0", "LPT0 .txt", "COM10", "CONSOLE", "COM\uff11", "COM\u2070")))
        # keyed by position: PowerShell property names are case-insensitive, and the matrix holds `COM1` and `com1`
        got_pos = self.judge(["C:\\fake\\agent-share"], {f"m{i:03d}": pre + n + "\\r" for i, n in enumerate(DEVICE_MATRIX_NAMES)})
        got_all = {n: got_pos[f"m{i:03d}"] for i, n in enumerate(DEVICE_MATRIX_NAMES)}
        mismatch = {n: (documented_dos_device(n), got_all[n]) for n in DEVICE_MATRIX_NAMES if documented_dos_device(n) != (got_all[n] is None)}
        self.assertEqual(mismatch, {}, "the helper's device rule and the documented reserved set disagree (name: (documented device, certificate))")
        self.assertGreaterEqual(sum(documented_dos_device(n) for n in DEVICE_MATRIX_NAMES), 80, "the matrix lost its reserved-name refusals")
        self.assertGreaterEqual(sum(not documented_dos_device(n) for n in DEVICE_MATRIX_NAMES), 18, "the matrix lost its ordinary-name positives")
        # CONIN$ and CONOUT$ are console device names the helper also refuses (stricter than the documented list), middle and terminal
        got_con = self.judge(["C:\\fake\\agent-share"], {"conin-middle": pre + "CONIN$\\r", "conout-ext-terminal": pre + "conout$.txt"})
        self.assertEqual(got_con, {"conin-middle": None, "conout-ext-terminal": None})
        # every admission site Sol swept goes through the one helper: the real guard statements of the leg (-ReceiptRoot, -SheetCopyDir), both pair scripts (-OutDir), the baseline
        # (-TrendFile / -RunDir) and the stage nightly (-ReceiptFile), each taken from its own script text
        guards = {
            "leg-receipt-root": (DV / "Invoke-VenueLeg.ps1", "(?s)\\$rrCert = .*?\\$ReceiptRoot = \\$rrCert", "$ReceiptRoot = $v; $OfflineTestMode = $false", "$ReceiptRoot"),
            "leg-sheet-copy-dir": (DV / "Invoke-VenueLeg.ps1", "(?s)if \\(-not \\[string\\]::IsNullOrWhiteSpace\\(\\$SheetCopyDir\\)\\) \\{\\s*\\$SheetCopyDir = Test-DvLocalReceiptRoot.*?\\r?\\n\\}", "$SheetCopyDir = $v", "$SheetCopyDir"),
            "sheet-pair-out-dir": (DV / "New-VenueSheetPair.ps1", "(?s)\\$outFull = Test-DvLocalReceiptRoot.*?\\$OutDir = \\$outFull", "$OutDir = $v", "$OutDir"),
            "flavor-pair-out-dir": (DV / "New-VenueFlavorPair.ps1", "(?s)\\$outFull = Test-DvLocalReceiptRoot.*?\\$OutDir = \\$outFull", "$OutDir = $v", "$OutDir"),
            "baseline-trend-and-run-dir": (DV / "Invoke-VenueBaseline.ps1", "(?s)\\$trendCert = Test-DvLocalReceiptRoot.*?\\$RunDir = \\$runDirCert", "$TrendFile = $v; $RunDir = $v", "$TrendFile + '|' + $RunDir"),
            "nightly-receipt-file": (DV / "Invoke-VenueStageNightly.ps1", "(?s)\\$receiptCert = Test-DvLocalReceiptRoot.*?\\$ReceiptFile = \\$receiptCert", "$ReceiptFile = $v", "$ReceiptFile"),
        }
        inputs = {**legal, "com1-then-parent": refused["com1-then-parent"], "nul-then-parent": refused["nul-then-parent"]}
        script = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                  "function Stop-Baseline { param($k, $m) throw \"$k $m\" }\nfunction Stop-Nightly { param($k, $m) throw \"$k $m\" }\n$o = [ordered]@{}\n")
        for g, (path, pattern, assign, result) in guards.items():
            script += (f"$m = [regex]::Match([IO.File]::ReadAllText('{psq(path)}'), '{psq(pattern)}')\nif (-not $m.Success) {{ throw 'guard {g} was not found' }}\n$sb = [scriptblock]::Create($m.Value)\n")
            for k, v in inputs.items():
                script += f"$v = '{psq(v)}'; {assign}\ntry {{ . $sb; $o['{g}|{k}'] = 'ACCEPTED ' + ({result}) }} catch {{ $o['{g}|{k}'] = 'REFUSED ' + $_.Exception.Message }}\n"
        script += "$o | ConvertTo-Json -Compress\n"
        out = json.loads(self.run_ps(script))
        for g in guards:
            for k in inputs:
                with self.subTest(guard=g, name=k):
                    if k in legal:
                        self.assertTrue(out[f"{g}|{k}"].startswith("ACCEPTED "), out[f"{g}|{k}"])
                        self.assertNotIn("..", out[f"{g}|{k}"], "the guard went on with the raw spelling, not the certified one")
                    else:
                        self.assertTrue(out[f"{g}|{k}"].startswith("REFUSED "), out[f"{g}|{k}"])
        # the batch entry runs to the end on a COM0 root and every leg is handed the certified root (the `..` resolved away)
        for label, comp in (("com0", "COM0"), ("lpt0-space-ext", "LPT0 .txt")):
            with self.subTest(entry="batch", name=label):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                root = self.case / "scratch" / ".claude-state" / comp / ".." / "receipts"
                r = self.run_batch(None, "-ReceiptRoot", str(root))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(self.case / 'scratch' / '.claude-state' / 'receipts')}"] * 2)

    # ---- r13 (hosted shard 4/8 job 124969603389, CoS 6563973648): the native classifier CORROBORATES the documented set, in its own method so a host difference cannot hide the
    # caller sweep. RtlIsDosDeviceName_U's answer depends on the input form and the OS build, MEASURED:
    #   10.0.19045 (Virtual-Ten): every form (bare, `.\`, `C:\x\`, the full .claude-state path) classifies all 84 documented spellings, extensions and `NAME .ext` included.
    #   10.0.26100 (hosted windows-2025-vs2026, Tests 41358897524 job 125004796490): bare classifies exactly the 30 bare documented stems (any case, superscripts included) and no
    #   extension form (CON.txt, `COM1 .txt` are ordinary); every path form classifies NUL only.
    # So the host-independent corroboration is: the BARE STEMS are devices and agree exactly with the documented set; and in EVERY form, whatever the kernel calls a device is in
    # the documented set (the helper refuses that set, so it is never looser than the kernel). A form that also classifies the extension controls must agree in full (19045).
    def test_r13_native_classifier_corroborates_the_documented_device_set_on_the_bare_component(self) -> None:
        is_dev = ctypes.windll.ntdll.RtlIsDosDeviceName_U
        is_dev.argtypes, is_dev.restype = [ctypes.c_wchar_p], ctypes.c_uint32
        forms = {"bare": "{n}", "dot-relative": ".\\{n}", "drive-dir": "C:\\x\\{n}", "full-claude-state": "C:\\x\\.claude-state\\{n}"}
        native = {f: {n: bool(is_dev(pat.format(n=n))) for n in DEVICE_MATRIX_NAMES} for f, pat in forms.items()}
        v = sys.getwindowsversion()
        host = f"os={v.major}.{v.minor}.{v.build} platform={sys.platform}"
        fact = host + " | " + " | ".join(f"{f}: {sum(m.values())} devices [{' '.join(sorted(n for n, d in m.items() if d))}]" for f, m in native.items())
        print(f"\nNATIVE-DOS-DEVICE-HOST-FACT {fact}".encode("ascii", "backslashreplace").decode(), file=sys.stderr)
        positives = ("COM1", "LPT9", "COM\u00b9", "LPT\u00b3", "NUL", "CON", "PRN", "AUX", "com1", "nul")
        self.assertTrue(all(native["bare"][n] for n in positives),
                        f"HOST-FACT-NATIVE-CLASSIFIER-BLIND-ON-BARE-NAMES: RtlIsDosDeviceName_U does not classify the documented positive controls {positives} even as a bare name on {fact}")
        # (1) bare stems (no dot, no space): the kernel and the documented set agree EXACTLY, both directions (COM0, LPT0, COM10, CONSOLE, fullwidth 1 ... are ordinary)
        stems = [n for n in DEVICE_MATRIX_NAMES if "." not in n and " " not in n]
        self.assertGreaterEqual(len(stems), 40)   # 30 documented stems, 14 ordinary near misses
        mismatch = {n: native["bare"][n] for n in stems if native["bare"][n] != documented_dos_device(n)}
        self.assertEqual(mismatch, {}, f"the documented reserved set and RtlIsDosDeviceName_U on the bare stem disagree (name: native device) on {fact}")
        # (2) every form: the kernel never calls a name a device that the documented set (so the helper) treats as ordinary
        for f, m in native.items():
            with self.subTest(form=f, check="native-device-is-documented"):
                self.assertEqual(sorted(n for n, d in m.items() if d and not documented_dos_device(n)), [], f"{f}: the kernel classifies a name the helper would certify, on {fact}")
        # (3) a form that also classifies the extension controls must agree with the documented set in full (10.0.19045: every form)
        ext_positives = ("CON.txt", "NUL .txt", "COM1.txt", "LPT\u00b2 .txt")
        for f, m in native.items():
            if all(m[n] for n in positives + ext_positives):
                with self.subTest(form=f, check="full-agreement"):
                    self.assertEqual({n: d for n, d in m.items() if d != documented_dos_device(n)}, {}, f"{f} on {fact}")

    def test_r8_a3_a_link_chain_is_bounded_per_link_sixteen_pass_seventeen_are_refused(self) -> None:
        # opus/sol A3 (ADV-A3): ResolveLinkTarget($true) collapsed a whole chain inside one outer hop, so the stated 16-link bound was never enforced. Real junctions: j00 -> j01 -> ... -> j16 -> final (17 links).
        base = self.clean_base()
        state = base / "chain" / ".claude-state"
        final = state / "final"
        final.mkdir(parents=True)
        prev = final
        for n in range(16, -1, -1):
            prev = self.make_junction(state / f"j{n:02d}", prev)
        got = self.judge(["C:\\fake\\agent-share"], {"seventeen-links": str(state / "j00" / "receipts"), "sixteen-links": str(state / "j01" / "receipts"), "one-link": str(state / "j16" / "receipts")})
        self.assertIsNone(got["seventeen-links"], got)
        self.assertTrue(self.same_path(got["sixteen-links"], final / "receipts"), f"a chain of 16 links settles on its target: {got}")
        self.assertTrue(self.same_path(got["one-link"], final / "receipts"), got)
        text = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertNotIn("ResolveLinkTarget($true)", text, "the walk must follow ONE link per hop (ResolveLinkTarget($false)) or the hop bound counts outer rewrites only")

    def test_adversary_dot_a1_no_boundary_directory_name_is_compared_with_a_culture_aware_operator(self) -> None:
        # the class, not the instance: every PowerShell file under tools/ that tests a path component against .claude-state goes through [string]::Equals(..., Ordinal)
        offenders = []
        for p in sorted((ROOT / "tools").rglob("*.ps*1")):
            if p.suffix not in (".ps1", ".psm1"):
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if re.search(r"-[ci]?(?:eq|ne|like|notlike|match|notmatch|contains|in|notin)\s+['\"]\.claude-state['\"]|['\"]\.claude-state['\"]\s+-[ci]?(?:eq|ne|like|contains)\b", line, re.I):
                    offenders.append(f"{p.relative_to(ROOT).as_posix()}:{n}: {line.strip()[:140]}")
        self.assertEqual(offenders, [], "a path component compared with .claude-state by a culture-aware operator accepts ignorable code points (U+00AD ...)")


@requires_windows_pwsh
class ScalarSpecGuardSeamTests(unittest.TestCase):
    """EXPORT-VENUE-LEG-1 r20 (adversary pass 24 A1 on 9b8c4753): the scalar forms (-LegSpec, -LegSet) skip a spec re-read they have no room for ONLY at the open guard,
    where the lease is held and the leg's own -ExpectSpecSha256 binds the bytes it runs (master read no spec there, so a lease whose room after the open held the leg
    and no re-read still ran the leg: test_r3_a_leg_still_running_at_lease_end_is_killed_as_a_whole_tree_and_uncounted). Before a lease (the claim guard) no room to
    re-read the spec is the typed refusal, so a spec changed after the export budget judged it never reaches a lease. With room, every seam compares the bytes.
    Run on the runner's own functions, lifted from its AST."""

    def _guard(self, seam: str, room: float) -> dict:
        tmp = tempfile.TemporaryDirectory(prefix="r20-seam-")
        self.addCleanup(tmp.cleanup)
        spec = Path(tmp.name) / "changed.json"
        spec.write_text('{"schema":"mlv-app/dual-venue-leg/v1","note":"the bytes after the preflight"}', encoding="utf-8")
        names = "'New-GuardBudget', 'Get-InputContentRefusal', 'Set-GuardProgress', 'Wait-GuardReadSeam', 'Read-SpecBytesWithin', 'Get-BytesSha256', 'Get-SpecReadBoundSec'"
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)\n"
                  f"foreach ($n in {names}) {{ . ([scriptblock]::Create($ast.Find({{ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }}, $true).Extent.Text)) }}\n"
                  "$min = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] -and $x.Left.Extent.Text -ceq '$script:GuardReadMinSec' }, $true)\n"
                  "if ($min) { . ([scriptblock]::Create($min.Extent.Text)) }\n"
                  "$legPlanMode = $false; $script:guardProgress = $null; $script:guardDelaySeam = $null\n"
                  # the export budget judged other bytes (all zeros is no file's sha256): the spec on disk changed after it
                  f"$script:guardedInputs = @([pscustomobject]@{{ kind = 'spec'; item = 0; path = '{spec}'; backend = ''; specSha256 = ('0' * 64) }})\n"
                  f"$script:guardBudget = New-GuardBudget {room} 'the {seam} room' 'SHARE_UNREACHABLE' $false '{seam}'\n"
                  "$hit = Get-InputContentRefusal\n"
                  "ConvertTo-Json -Compress -InputObject @{ code = [string]$hit.code; noRoom = [bool]$hit.noRoom }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_r20_a1_the_claim_guard_with_no_room_to_reread_a_scalar_spec_refuses_before_any_lease(self) -> None:
        got = self._guard("claim", 1.0)
        self.assertEqual((got["code"], got["noRoom"]), ("SHARE_UNREACHABLE", True), f"a 1 s claim-guard room under the 2 s read minimum: {got}")

    def test_r20_a1_the_open_guard_with_no_room_skips_the_scalar_reread_and_leaves_the_binding_to_the_leg(self) -> None:
        got = self._guard("open", 1.0)
        self.assertEqual(got["code"], "", f"master parity after the open (the leg's -ExpectSpecSha256 refuses changed bytes): {got}")

    def test_r20_a1_with_room_every_seam_refuses_a_scalar_spec_changed_after_the_budget(self) -> None:
        for seam in ("claim", "open"):
            with self.subTest(seam=seam):
                self.assertEqual(self._guard(seam, 30.0)["code"], "SPEC_CHANGED_AFTER_PREFLIGHT")


@requires_windows_pwsh
class LeaseEndUtcTimezoneTests(unittest.TestCase):
    ENDED = datetime(2026, 10, 25, 1, 19, 14, 720994, tzinfo=timezone.utc)   # the r7 open result's endedUtc
    LEFT = 2699                                                              # a 45 min lease less the open job's run (the r7 figure)
    WANT = LEFT - (ENDED.timestamp() % 1)                                    # until - ended, to the sub-second

    @classmethod
    def lease_end_deltas(cls) -> dict:
        """Seconds from now to the lease end Get-LeaseEndUtc returns, per input shape. The function is the runner's own text (AST), not a copy."""
        until = int(cls.ENDED.timestamp()) + cls.LEFT
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            # r6: and the readers it calls since r5 (Get-AdmissionLeaseBatch, Test-JsonObject); without them every shape read $null (hosted CI on 848f6559)
            "foreach ($n in 'Test-JsonObject', 'Get-AdmissionLeaseBatch', 'Get-LeaseEndUtc') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            f"$until = {until}\n"
            "function Delta($ended) {\n"
            "  $r = Get-LeaseEndUtc ([pscustomobject]@{ endedUtc = $ended; admission = [pscustomobject]@{ leaseBatch = [pscustomobject]@{ untilEpoch = $until } } })\n"
            "  if ($null -eq $r) { $null } else { [Math]::Round(($r - [DateTime]::UtcNow).TotalSeconds, 1) }\n"
            "}\n"
            # the way the runner receives it: the agent's result JSON through ConvertFrom-Json
            "$fromJson = ('{\"endedUtc\": \"2026-10-25T01:19:14.7209943Z\", \"admission\": {}}' | ConvertFrom-Json).endedUtc\n"
            "$utc = [DateTime]::SpecifyKind([DateTime]'2026-10-25T01:19:14.7209943', [DateTimeKind]::Utc)\n"
            "[ordered]@{\n"
            "  jsonKind = [string]$fromJson.Kind; json = Delta $fromJson\n"
            "  utcKind = Delta $utc\n"
            "  unspecifiedKind = Delta ([DateTime]::SpecifyKind($utc, [DateTimeKind]::Unspecified))\n"
            "  localKind = Delta $utc.ToLocalTime()\n"
            "  offset = Delta ([DateTimeOffset]::new($utc).ToOffset([TimeSpan]::FromHours(-5)))\n"
            "  stringZ = Delta '2026-10-25T01:19:14.7209943Z'\n"
            "  stringOffset = Delta '2026-10-25T03:19:14.7209943+02:00'\n"
            "  stringNoZone = Delta '2026-10-25T01:19:14.7209943'\n"
            "  garbage = Delta 'not a time'\n"
            "  missing = Delta $null\n"
            "} | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stdout + out.stderr
        return json.loads(out.stdout)

    def test_a_json_utc_timestamp_is_the_same_instant_on_any_host_zone(self) -> None:
        got = self.lease_end_deltas()
        self.assertEqual(got["jsonKind"], "Utc", "ConvertFrom-Json hands the runner a Kind=Utc DateTime for a 'Z' string")
        # the host's UTC offset must not enter (the bug read -15301 on a Central host); 3 s covers the fractional second and a slow host, no real offset is that small
        self.assertAlmostEqual(got["json"], self.WANT, delta=3, msg=f"roomSec on this host (UTC offset {datetime.now().astimezone().utcoffset()}): {got}")

    def test_every_input_shape_resolves_to_the_same_utc_instant(self) -> None:
        got = self.lease_end_deltas()
        for shape in ("utcKind", "unspecifiedKind", "localKind", "offset", "stringZ", "stringOffset", "stringNoZone"):
            with self.subTest(shape=shape):
                self.assertAlmostEqual(got[shape], self.WANT, delta=3, msg=f"{shape}: {got}")

    def test_an_unreadable_or_missing_endedutc_is_no_lease_end(self) -> None:
        got = self.lease_end_deltas()
        self.assertIsNone(got["garbage"])
        self.assertIsNone(got["missing"])

    # ---- host-independent guard (the one a UTC CI host can fail)
    def test_the_runner_never_parses_a_json_timestamp_through_string_and_the_host_offset(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?<!Try)Parse\(\[string\]", text), "[string] drops the DateTime Kind and a bare Parse then assumes the host's local offset")
        fn = text[text.index("function Get-LeaseEndUtc"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("-is [DateTime]", fn)
        self.assertIn(".ToUniversalTime()", fn)
        self.assertIn("AssumeUniversal", fn)
        self.assertIn("AdjustToUniversal", fn)
        self.assertNotIn("::Parse(", fn, "a zone-less string must go through TryParse with AssumeUniversal, never a bare Parse")

    # ---- r3 (Grok G4 on #817): the idle clamp's guards that a UTC CI host can fail (the behavioural A2 case goes red only off UTC)
    def test_lease_batch_idle_end_r3_grok_g4_the_idle_clamp_reads_endedutc_through_the_helper_never_string(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        at = text.index("$ie = Get-LeaseIdleEnd $token")
        self.assertIn("Set-BatchLeaseIdleEnded $b $ie ", text[at:text.index("break", at)], "r4 (Grok G9): the probe's idle end is typed through the one helper")
        # r4 (Grok G9): the clamp lives in Set-BatchLeaseIdleEnded, shared by every site that types LEASE_IDLE_ENDED
        at = text.index("function Set-BatchLeaseIdleEnded")
        clamp = text[at:text.index("\n}\n", at)]
        self.assertIn("$ieAt = ConvertTo-AgentUtc $Ie.endedUtc", clamp, "the idle clamp takes the agent's instant from ConvertTo-AgentUtc")
        self.assertIsNone(re.search(r"(?i)\[string\]\s*\$ie\w*\.endedUtc", text), "[string] drops a DateTime's zone (and its fraction): the agent's endedUtc never goes through it")
        self.assertNotIn("TryParse", clamp, "the clamp parses nothing itself: ConvertTo-AgentUtc is the one reader")
        fn = text[text.index("function ConvertTo-AgentUtc"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIsNone(re.search(r"\[string\]\s*\$Value", fn), "the helper never casts its input through [string]")
        for needle in ("$Value -is [DateTime]", "[DateTimeKind]::Unspecified", ".ToUniversalTime()", "$Value -is [DateTimeOffset]", ".UtcDateTime", "$Value -isnot [string]",
                       "AssumeUniversal", "AdjustToUniversal"):
            self.assertIn(needle, fn)

    def test_lease_batch_idle_end_r3_grok_g4_convertto_agentutc_gives_the_exact_utc_instant_for_every_shape(self) -> None:
        # adversarial direct case on the runner's own function (AST): every shape ConvertFrom-Json or a caller can hand it lands on the same instant, TO THE TICK and of Kind Utc.
        # Exact ticks are what make this red on a UTC host too: a [string] round trip drops the fractional second whatever the host's offset is.
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'ConvertTo-AgentUtc' }, $true)\n"
            ". ([scriptblock]::Create($fn.Extent.Text))\n"
            "function Show($v) { $r = ConvertTo-AgentUtc $v; if ($null -eq $r) { $null } elseif ($r -isnot [DateTime]) { 'type:' + $r.GetType().Name } else { '{0}|{1}' -f $r.Kind, $r.Ticks } }\n"
            "$utc = [DateTime]::SpecifyKind([DateTime]::new(2026, 10, 25, 1, 19, 14).AddTicks(7209943), [DateTimeKind]::Utc)\n"
            "[ordered]@{\n"
            "  want = '{0}|{1}' -f [DateTimeKind]::Utc, $utc.Ticks\n"
            "  utcKind = Show $utc\n"
            "  localKind = Show $utc.ToLocalTime()\n"
            "  unspecifiedKind = Show ([DateTime]::SpecifyKind($utc, [DateTimeKind]::Unspecified))\n"
            "  offsetMinus5 = Show ([DateTimeOffset]::new($utc).ToOffset([TimeSpan]::FromHours(-5)))\n"
            "  offsetPlus530 = Show ([DateTimeOffset]::new($utc).ToOffset([TimeSpan]::FromMinutes(330)))\n"
            "  stringOffset = Show '2026-10-25T03:19:14.7209943+02:00'\n"
            "  stringZ = Show '2026-10-25T01:19:14.7209943Z'\n"
            "  stringNoZone = Show '2026-10-25T01:19:14.7209943'\n"
            "  json = Show (('{\"t\": \"2026-10-25T01:19:14.7209943Z\"}' | ConvertFrom-Json).t)\n"
            "  jsonOffset = Show (('{\"t\": \"2026-10-24T20:19:14.7209943-05:00\"}' | ConvertFrom-Json).t)\n"
            "  null = Show $null\n"
            "  garbage = Show 'not a time'\n"
            "  number = Show 42\n"
            "  array = Show @('2026-10-25T01:19:14.7209943Z')\n"
            "} | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        for shape in ("utcKind", "localKind", "unspecifiedKind", "offsetMinus5", "offsetPlus530", "stringOffset", "stringZ", "stringNoZone", "json", "jsonOffset"):
            with self.subTest(shape=shape):
                self.assertEqual(got[shape], got["want"], f"{shape}: {got}")
        for shape in ("null", "garbage", "number", "array"):
            with self.subTest(shape=shape):
                self.assertIsNone(got[shape], f"{shape} is no instant: {got}")


if __name__ == "__main__":
    unittest.main()
