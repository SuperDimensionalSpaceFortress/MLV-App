"""Hygiene guard: the helper BODIES that generators splice into an emitted job are write-scanned.

tools/repo_hygiene/attr3_publish_write_scan.ps1 reads each generator's ``$template`` here-string with
its placeholders neutralised. A module helper that a generator splices into the emitted job
(``Get-AttrCudaEmbeddedFunctionSource -Name @(...)`` -> ``__EMBEDDED_FUNCTIONS__``) reached the scan
by NAME only, as a known command: its BODY was never read. A raw write into ``$Pub`` inside such a
body therefore passed ``test_every_emitted_template_parses_and_trips_no_rule``
(ATTR3-SCANNER-HARDEN-1; CoS Prevention note on #432).

The scan now takes ``-SpliceSource`` (generators / modules whose splice call sites enumerate the
spliced function names) and reports what its rules (R1 to R6) find in every body the real extractor
returns. This file pins that, and carries the policy: the INERT_* lists (pure computation, stream
plumbing, read-only members, admitted by member NAME, not by receiver type) and the reviewed ALLOWLIST. The helpers that ARE the
runtime safety boundary (Publish-AttrCuda*, New-AttrCudaDirectory, Remove-AttrCuda*) legitimately hold
the raw writes and deletes the templates may not, so each (function, rule, offending token) is listed
with a count and a one-line reason. A finding not on the list fails; a row whose count no longer
matches fails; a row that matches nothing fails.

Grammar and non-promises (declared before r2, written in the scan's header and pinned by
SplicedBodyScanHeaderTests): the scan is the PowerShell language AST over the template and over each
body Get-AttrCudaEmbeddedFunctionSource splices. It does no runtime evaluation, does not scan code a job
dot-sources or imports at runtime, and trusts the allowlist rows as reviewed. A test below that says a
body is "clean" means clean under that grammar, and no more.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "tools" / "repo_hygiene" / "attr3_publish_write_scan.ps1"
BACHELOR = ROOT / "tools" / "profiling" / "bachelor"
PWSH = shutil.which("pwsh")

# The scanned templates (the same set test_playback_attr_3_cuda_behaviour.JOB_TEMPLATES scans).
TEMPLATE_GENERATORS = (
    ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1",
    BACHELOR / "playback-attr-3-cuda-stage-job.ps1",
    BACHELOR / "playback-attr-3-cuda-job.ps1",
    BACHELOR / "attr3-stage-fixture-job.ps1",
    BACHELOR / "attr3-stage-smoke-runner-job.ps1",
)

# Every file that calls Get-AttrCudaEmbeddedFunctionSource to splice helpers into a job that runs
# unattended on a measurement host. The first five are the scanned templates above; the rest splice
# into jobs whose templates are NOT scanned today (see the scan's EXPLICIT LIMITS), but the helper
# text they carry is the same text, so their names are enumerated too.
SPLICE_SOURCES = TEMPLATE_GENERATORS + (
    BACHELOR / "reference-frame-runner-job.ps1",
    BACHELOR / "Attr3FootagePresenceJob.psm1",
    BACHELOR / "Attr3FootageReadRateJob.psm1",
    BACHELOR / "Attr3FootageStageJob.psm1",
    ROOT / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1",
)

# Files that name the extractor without calling it: the defining module (comments and the export
# list), the display-identity parser (its header documents how the job embeds it) and the scan itself
# (it looks for the call by name).
SPLICE_DEFINITION_FILES = {
    BACHELOR / "AttrCudaArtifacts.psm1",
    ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1",
    SCANNER,
}

# R4 members that are pure in-memory computation, enum constants or collection / string / date / hash
# plumbing: none of them opens, writes, moves or deletes a file, starts a process, or touches the
# registry or the network. They are what the scan's template-side R4 list already admits (double::Parse,
# math::*, regex::*, string::IsNullOrWhiteSpace, IO.Path::GetFullPath, ...) plus the same kind of
# member a helper body needs. Anything with a side effect (IO.File::*, IO.Directory::*, IO.FileStream::new,
# a stream .Write/.Read, a Win32 call, Process::Start, a runspace) is NOT here and needs an ALLOWLIST row.
INERT_STATIC = frozenset("""
BitConverter::ToString BitConverter::ToUInt16 Convert::FromBase64String DateTime::UtcNow DateTimeKind::Local
DateTimeKind::Unspecified DateTimeKind::Utc Environment::NewLine Globalization.DateTimeStyles::RoundtripKind
Globalization.NumberStyles::AllowDecimalPoint IO.FileAccess::Read IO.FileAccess::Write IO.FileAttributes::ReparsePoint
IO.FileMode::CreateNew IO.FileMode::Open IO.FileOptions::SequentialScan IO.FileShare::None IO.FileShare::Read
IO.FileShare::ReadWrite IO.MemoryStream::new IO.Path::GetDirectoryName IO.Path::GetExtension IO.Path::GetFileName
IO.Path::GetPathRoot IO.SeekOrigin::Begin IntPtr::Zero IntPtr::new Math::Abs Math::Ceiling Math::Round
Security.Cryptography.HashAlgorithmName::SHA256 Security.Cryptography.IncrementalHash::CreateHash
Security.Cryptography.SHA256::Create StringComparison::Ordinal
System.Collections.Generic.HashSet[int]::new System.Collections.Generic.List[System.IO.FileSystemInfo]::new
System.Collections.Generic.List[string]::new System.Collections.Generic.Stack[System.IO.FileSystemInfo]::new
System.Collections.Hashtable::Synchronized System.Diagnostics.Stopwatch::StartNew
System.Globalization.CultureInfo::InvariantCulture System.Globalization.DateTimeStyles::AdjustToUniversal
System.Globalization.DateTimeStyles::AssumeUniversal System.InvalidOperationException::new
System.Runtime.InteropServices.Marshal::GetLastWin32Error System.Runtime.InteropServices.Marshal::SizeOf
Runtime.InteropServices.Marshal::GetLastWin32Error System.Text.StringBuilder::new
System.Threading.ManualResetEventSlim::new Text.Encoding::ASCII Text.Encoding::UTF8
Text.RegularExpressions.RegexOptions::IgnoreCase Text.UTF8Encoding::new Text.UnicodeEncoding::new Diagnostics.Stopwatch::StartNew
byte[]::new datetime::MinValue datetime::Parse datetime::SpecifyKind datetime::TryParse double::IsFinite
double::IsInfinity guid::NewGuid int64::TryParse int::TryParse math::Max math::Min regex::IsMatch regex::Match
regex::Replace string::Equals string::IsNullOrEmpty
""".split())
INERT_INSTANCE = frozenset("""
.AddSeconds() .Append() .AppendData() .ComputeHash() .ExceptWith() .GetBytes() .GetHashAndReset() .GetString()
.IndexOf() .Pop() .Push() .Split() .ToArray() .ToCharArray() .ToFileTimeUtc() .ToLocalTime() .TrimEnd()
.Copy() .DangerousGetHandle() .EnumerateFileSystemInfos() .Remove()
""".split())
# Resource plumbing on an object a flagged call already produced: the side effect, if any, is the call
# that produced it (an open, a .Write, a process start), which stays a finding. By name, not by receiver.
INERT_INSTANCE |= frozenset("""
.Close() .Dispose() .EndInvoke() .Flush() .Read() .ReadByte() .ReadLine() .ReadToEndAsync() .Seek() .Set()
.Stop() .Wait() .WaitOne()
""".split())
# Read-only: they return data and cannot create, write, move or delete anything.
INERT_STATIC |= frozenset("""
Environment::GetFolderPath IO.DriveInfo::new IO.File::OpenRead IO.File::ReadAllBytes IO.File::ReadAllLines
""".split())
# R1 commands that only read or report (not on the template allowlist, which is deliberately narrower).
# New-Object is NOT here: it can build a StreamWriter, so each type it builds needs its own row.
INERT_COMMANDS = frozenset("Get-Command Get-Counter Get-Item Group-Object Write-Warning".split())

# An R5 finding on `$local.property = ...` sets a property of a helper's own local object (a result
# record, a ProcessStartInfo). It cannot be a publish destination; the template-side member-assignment
# rule exists to stop `(Get-Item $Pub).Attributes = ...` shapes, which keep their token (the full
# member text of a non-local receiver) and stay findings.
LOCAL_PROPERTY_SET = re.compile(r"^\$(?!Work\b|Pub\b|Cache\b|AgentRoot\b|Root\b)[A-Za-z_]\w*(\.\w+)+$")

# (function, rule, offending token) -> (how many times the body holds it, one-line reason). REVIEWED
# rows only: every row names a helper whose job is to be the runtime boundary, or a read-only / inert
# use of a flagged shape. The count pins the row: a second raw write of an already listed kind in the
# same body changes the count and fails. Never add a row to silence a finding without reading the body.
ALLOWLIST: dict[tuple[str, str, str], tuple[int, str]] = {}


def _allow(function: str, reason: str, *rows: tuple[str, str, int]) -> None:
    """One helper, one reason, every (rule, token, count) the body holds for it."""
    for rule, token, count in rows:
        assert (function, rule, token) not in ALLOWLIST, (function, rule, token)
        ALLOWLIST[(function, rule, token)] = (count, reason)


_IN_MEMORY = "in-memory object or buffer only; no file, process or registry access"
_TYPE_NAME = "reads the exception's type name inside a catch block to report it; nothing is reflected into or invoked"
_LOCAL_BLOCK = "invokes a script block bound to a local variable in this same body, so the block's text is inside the scanned body"

# ---- the runtime WRITE / DELETE boundary: every raw write or delete lives here, behind a slot or ownership proof ----
_allow("Assert-AttrCudaWritableFileSlot",
       "replace-in-place of a fixed derived publish name: removes the stale plain FILE in the slot after refusing a directory, a reparse point and a multi-link file",
       ("R1", "Remove-Item", 1))
_allow("Publish-AttrCudaText",
       "publish write into the slot Assert-AttrCudaWritableFileSlot just proved: a journalled CreateNew stream (.Write) with -OwnedJournal, else WriteAllText",
       ("R4", ".Write()", 1), ("R4", "IO.File::WriteAllText", 1))
_allow("Publish-AttrCudaBytes",
       "publish write of a byte array into the slot Assert-AttrCudaWritableFileSlot just proved (journalled stream, else WriteAllBytes)",
       ("R4", ".Write()", 1), ("R4", "IO.File::WriteAllBytes", 1))
_allow("Publish-AttrCudaFileCopy",
       "publish copy into the slot Assert-AttrCudaWritableFileSlot just proved: the source is opened read-only; journalled stream .CopyTo, else Copy-Item",
       ("R1", "Copy-Item", 1), ("R4", ".CopyTo()", 1), ("R4", "IO.File::Open", 1))
_allow("Publish-AttrCudaFileMove",
       "publish rename into the slot Assert-AttrCudaWritableFileSlot just proved",
       ("R1", "Move-Item", 1))
_allow("Publish-AttrCudaFileMoveNonOverwriting",
       "File.Move(overwrite=false) into the slot Assert-AttrCudaNonOverwritingFileSlot just proved; an occupied destination throws and nothing moves",
       ("R4", "IO.File::Move", 1))
_allow("Publish-AttrCudaDirectoryMoveNonOverwriting",
       "Directory.Move into the slot Assert-AttrCudaNonOverwritingFileSlot just proved; an occupied destination throws and nothing moves",
       ("R4", "IO.Directory::Move", 1))
_allow("New-AttrCudaDirectory",
       "the directory-creation boundary: parent proved to be a non-link directory and the name absent or a plain directory before New-Item",
       ("R2", "New-Item", 1))
_allow("New-AttrCudaOwnedRoot",
       "creates a FRESH journalled root: a standing tree is swept by proof or renamed aside (nothing deleted), then New-Item; -TrustedRoot forwards this function's own parameter",
       ("R2", "New-Item", 1), ("R2", "Remove-AttrCudaTree", 1), ("R4", "IO.Directory::Move", 1))
_allow("New-AttrCudaOwnedFileStream",
       "FileMode.CreateNew / FileAccess.Write: creates a NEW file (never touches an existing one) and journals the identity read off that creating handle",
       ("R4", "IO.FileStream::new", 1))
_allow("Add-AttrCudaOwnedRecord",
       "appends one record to the creator journal through CreateFileW with FILE_APPEND_DATA only (no truncate, no overwrite); a link or second-name journal is refused first",
       ("R4", ".Write()", 2), ("R4", "AttrCudaWin32.FileIdNative::CreateFileW", 2), ("R4", "IO.FileStream::new", 1),
       ("R4", "Microsoft.Win32.SafeHandles.SafeFileHandle::new", 1))
_allow("Add-AttrCudaTraceLine",
       "appends one line to the caller's -TracePath (the job's logs\\<JobId>.trace.txt under its provisioning root, never a publish destination) and creates that file's parent directory",
       ("R4", "IO.Directory::CreateDirectory", 1), ("R4", "IO.Directory::Exists", 1), ("R4", "IO.File::AppendAllText", 1))
_allow("Remove-AttrCudaTree",
       "deletes an EMPTY directory (recurse=false) the trusted-root tree walk already proved is below the root and not a reparse point; non-empty or linked trees are left",
       ("R4", "IO.Directory::Delete", 2))
_allow("Remove-AttrCudaFileByProof",
       "the identity-checked delete primitive: opens by handle, re-reads the file id, and sets delete-on-close only while it is still the proved object with one name",
       ("R4", "AttrCudaWin32.FileIdNative+FileBasicInfo::new", 1), ("R4", "AttrCudaWin32.FileIdNative+FileDispositionInfo::new", 1),
       ("R4", "AttrCudaWin32.FileIdNative+FileIdInfo::new", 1), ("R4", "AttrCudaWin32.FileIdNative::CloseHandle", 1),
       ("R4", "AttrCudaWin32.FileIdNative::CreateFileW", 1), ("R4", "AttrCudaWin32.FileIdNative::GetFileInformationByHandle", 1),
       ("R4", "AttrCudaWin32.FileIdNative::SetFileInformationByHandle", 3), ("R4", "IO.FileStream::new", 1),
       ("R4", "Microsoft.Win32.SafeHandles.SafeFileHandle::new", 1))
_allow("New-AttrCudaFileSymlink",
       "the module's only symbolic-link creation: one link in the job-private view directory naming a read-only owner original; no -Force, never overwrites",
       ("R2", "New-Item", 2))
_allow("New-AttrCudaOwnerFootageView",
       "view COPY created with CreateNew under a neutral name in the job-private view directory; the owner original is only read through the read-only pin",
       ("R4", ".CopyTo()", 1), ("R4", "IO.File::Open", 1))
_allow("Test-AttrCudaSymlinkCapability",
       "throwaway 4097-byte probe target created with CreateNew under a GUID name in the job's own view directory, read back through the probe link",
       ("R1", "New-Object byte[]", 1), ("R4", ".Write()", 1), ("R4", "IO.File::Open", 1))

# ---- read-only opens and identity queries ----
_allow("Get-AttrCudaFileId",
       "read-only identity query: CreateFileW with read-attribute access, GetFileInformationByHandle, CloseHandle; creates, writes and deletes nothing",
       ("R4", "AttrCudaWin32.FileIdNative+FileIdInfo::new", 1), ("R4", "AttrCudaWin32.FileIdNative::CloseHandle", 1),
       ("R4", "AttrCudaWin32.FileIdNative::CreateFileW", 1), ("R4", "AttrCudaWin32.FileIdNative::GetFileInformationByHandle", 1))
_allow("Get-AttrCudaFileSha256Blocks",
       "FileStream opened FileMode.Open / FileAccess.Read to hash a file in blocks; read-only",
       ("R4", "IO.FileStream::new", 1))
_allow("Get-AttrCudaPresentMonEventsLost",
       "reads the head and tail of PresentMon's stderr (File.Open Open/Read/ReadWrite-share); the byte buffers are in memory",
       ("R1", "New-Object byte[]", 2), ("R4", "IO.File::Open", 1))
_allow("Open-AttrCudaReadOnlyHandle",
       "File.Open with FileMode.Open / FileAccess.Read / FileShare.Read: the read-only pin on an owner original; it cannot write",
       ("R4", "IO.File::Open", 1))
_allow("Publish-AttrCudaBoundedTextCopy",
       "reads the tail of -Source (File.Open Open/Read); the bounded text is then published through Publish-AttrCudaText, never written here",
       ("R1", "New-Object byte[]", 1), ("R4", "IO.File::Open", 1))
_allow("Read-AttrCudaOwnedJournal",
       "reads the creator journal (File.Open Open/Read, ReadWrite share) through a StreamReader; read-only",
       ("R4", "IO.File::Open", 1), ("R4", "IO.StreamReader::new", 1))

# ---- Win32 / Add-Type: type definition, display and session queries, input nudges (none touches a file) ----
_allow("Initialize-AttrCudaFileIdNative",
       "defines the AttrCudaWin32.FileIdNative P/Invoke type (CreateFileW, GetFileInformationByHandle, SetFileInformationByHandle, CloseHandle) once per process",
       ("R1", "Add-Type", 1))
_allow("Test-AttrCudaPresentMonTraceReady",
       "defines the PresentMonProbe P/Invoke type and asks whether PresentMon's trace session is live for -Proc; read-only",
       ("R1", "Add-Type", 1), ("R4", "AttrCudaWin32.PresentMonProbe::Probe", 1))
_allow("Get-AttrCudaSessionLocked",
       "reads whether the console session is locked through WTS APIs and a native buffer; read-only",
       ("R1", "Add-Type", 1), ("R4", "MLVAppAttrCudaSessionLock.NativeMethods::ProcessIdToSessionId", 1),
       ("R4", "MLVAppAttrCudaSessionLock.NativeMethods::WTSFreeMemory", 1),
       ("R4", "MLVAppAttrCudaSessionLock.NativeMethods::WTSQuerySessionInformationW", 1),
       ("R4", "System.Runtime.InteropServices.Marshal::ReadInt32", 3))
_allow("Get-AttrCudaWindowsDisplayInventory",
       "enumerates display adapters and monitors through EnumDisplay* (Add-Type defines the structs); read-only, the New-Object instances are the in-memory structs",
       ("R1", "Add-Type", 1), ("R1", "New-Object AttrCudaDevMode", 1), ("R1", "New-Object AttrCudaDisplayDevice", 2),
       ("R1", "New-Object System.Collections.Generic.List[object]", 1), ("R4", ".GetType()", 1),
       ("R4", "AttrCudaNativeDisplay::EnumDisplayDevices", 1), ("R4", "AttrCudaNativeDisplay::EnumDisplayDevicesNamed", 1),
       ("R4", "AttrCudaNativeDisplay::EnumDisplaySettings", 1))
_allow("Register-AttrCudaDisplayWakeNativeMethods",
       "defines the display-wake P/Invoke types (SendInput, SetThreadExecutionState, SystemParametersInfo, the input-desktop nudge); declaration only",
       ("R1", "Add-Type", 1))
for _name, _token in (("Get-AttrCudaScreensaverActive", "SystemParametersInfo"), ("Get-AttrCudaScreensaverRunning", "SystemParametersInfo"),
                      ("Get-AttrCudaScreensaverSecure", "SystemParametersInfo"), ("Get-AttrCudaScreensaverTimeoutSeconds", "SystemParametersInfoInt")):
    _allow(_name, "reads one screen-saver setting through SystemParametersInfo (a SPI_GET code); read-only",
           ("R4", f"MLVAppAttrCudaDisplayWake.NativeMethods::{_token}", 1))
_allow("Start-AttrCudaDisplayWake",
       "wakes the display: one synthetic input event (SendInput) and SetThreadExecutionState to keep it awake; touches no file",
       ("R4", "MLVAppAttrCudaDisplayWake.NativeMethods::SendInput", 1),
       ("R4", "MLVAppAttrCudaDisplayWake.NativeMethods::SetThreadExecutionState", 1))
_allow("Stop-AttrCudaDisplayWake",
       "restores ES_CONTINUOUS through SetThreadExecutionState; touches no file",
       ("R4", "MLVAppAttrCudaDisplayWake.NativeMethods::SetThreadExecutionState", 1))
_allow("Invoke-AttrCudaInputDesktopNudge",
       "runs the input-desktop nudge type with a join timeout; touches no file",
       ("R4", "MLVAppAttrCudaDisplayWake.InputDesktopNudge::Run", 1))

# ---- runspaces and child processes ----
_allow("Start-AttrCudaDisplayWakeKeepAlive",
       "runs the display keep-alive loop in a background runspace; the loop is a script block in this body (scanned) and only probes and nudges input",
       ("R4", ".AddArgument()", 5), ("R4", ".AddScript()", 1), ("R4", ".BeginInvoke()", 1), ("R4", ".Open()", 1),
       ("R4", "MLVAppAttrCudaDisplayWake.InputDesktopNudge::Run", 1), ("R4", "System.Management.Automation.PowerShell::Create", 1),
       ("R4", "System.Management.Automation.Runspaces.InitialSessionState::CreateDefault", 1),
       ("R4", "System.Management.Automation.Runspaces.RunspaceFactory::CreateRunspace", 1),
       ("R4", "System.Management.Automation.Runspaces.SessionStateFunctionEntry::new", 2))
_allow("Invoke-AttrCudaBoundedProbe",
       "runs one named read-only probe function in a throwaway runspace to bound its wall time; that function is itself a spliced body scanned on its own",
       ("R4", ".AddCommand()", 1), ("R4", ".BeginInvoke()", 1), ("R4", ".Open()", 1),
       ("R4", "System.Management.Automation.PowerShell::Create", 1),
       ("R4", "System.Management.Automation.Runspaces.InitialSessionState::CreateDefault", 1),
       ("R4", "System.Management.Automation.Runspaces.RunspaceFactory::CreateRunspace", 1),
       ("R4", "System.Management.Automation.Runspaces.SessionStateFunctionEntry::new", 2))
_allow("Invoke-AttrCudaLogman",
       "runs System32\\logman.exe (path from GetFolderPath('System')) with the caller's query/stop arguments for an ETS session; output is read in memory",
       ("R4", "Diagnostics.Process::Start", 1), ("R4", "Diagnostics.ProcessStartInfo::new", 1))
_allow("Test-AttrCudaVisualCaptureSupport",
       "runs the staged app executable with its help flag to probe a capability and reads the output in memory; creates nothing",
       ("R1", "New-Object System.Diagnostics.ProcessStartInfo", 1), ("R4", ".GetType()", 1), ("R4", "System.Diagnostics.Process::Start", 1))

# ---- shape-only: reflection-free type-name reads, in-memory objects, in-body script blocks, a guarded dot-source ----
for _name, _count in (("ConvertFrom-GuiSmokeDisplayLog", 3), ("Find-AttrCudaFailedSmokeDisplayLog", 1), ("Get-AttrCudaQuiescenceSample", 1)):
    _allow(_name, _TYPE_NAME, ("R4", ".GetType()", _count))
_allow("ConvertFrom-GuiSmokeDisplayLog", _IN_MEMORY, ("R1", "New-Object System.Collections.Generic.List[object]", 1))
_allow("Find-AttrCudaFailedSmokeDisplayLog", _IN_MEMORY, ("R1", "New-Object System.Collections.Generic.List[string]", 1))
_allow("Get-AttrCudaEligibilityVerdict", _LOCAL_BLOCK, ("R3", "& $read", 8))
_allow("Get-AttrCudaPresentMonDisplayReport", _LOCAL_BLOCK, ("R3", "& $buildWindow", 2), ("R3", "& $unavailable", 7))
_allow("Get-AttrCudaSelfHealMetrics", _LOCAL_BLOCK,
       ("R3", "& $count", 4), ("R3", "& $field", 2), ("R3", "& $fps", 2), ("R3", "& $iniFlag", 2), ("R3", "& $readJson", 3),
       ("R3", "& $readText", 3), ("R3", "& $sessionLine", 4), ("R3", "& $setRead", 4), ("R3", "& $telemetry", 2))
_allow("Get-AttrCudaSelfHealMetrics",
       "numeric TryParse on a caller-chosen numeric type variable ([long] / [double]) with InvariantCulture; parse only",
       ("R4", "$Type::TryParse($raw, [Globalization.NumberStyles]::AllowDecimalPoint, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)", 1))
_allow("Get-AttrCudaGuiSmokeDisplaySelection",
       "fallback dot-source of the sibling gui-smoke-display-identity.ps1, taken only when ConvertFrom-GuiSmokeDisplayLog is NOT defined; the emitted job embeds that function, so the branch is dead there",
       ("R3", ". (dot-source)", 1))

requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _long_path(path: Path) -> Path:
    # tempfile can hand back an 8.3 short name (OBABAL~1); canonicalize so scan output paths compare.
    return Path(os.path.realpath(path))


def run_scan(*, generators=(), templates=(), modules=(), splice_sources=()) -> dict:
    command = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCANNER)]
    if generators:
        command += ["-GeneratorPath", ";".join(str(g) for g in generators)]
    if templates:
        command += ["-TemplateFile", ";".join(str(t) for t in templates)]
    if modules:
        command += ["-ModulePath", ";".join(str(m) for m in modules)]
    if splice_sources:
        command += ["-SpliceSource", ";".join(str(s) for s in splice_sources)]
    proc = run_bounded(command, SCAN_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise AssertionError(f"scan exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(proc.stdout)


def run_bounded(command: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    """subprocess.run with a bound: a hung pwsh fails this test instead of stalling the shard."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"{Path(command[0]).name} did not finish within {timeout:g}s: {' '.join(command[1:4])} ...") from exc


# The real scan parses ~100 helper bodies and five templates (tens of seconds on a loaded host);
# the bound only has to be finite and well above that.
SCAN_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 300


# A call to the extractor, however it is spelled: PowerShell command names are case-insensitive and
# a module-qualified name (Module\Get-AttrCudaEmbeddedFunctionSource) resolves to the same command.
# The argument shape is irrelevant (named, positional, splatted). A comment-only line is not a call.
EXTRACTOR_NAME = re.compile(r"(?i)(?<![\w-])Get-AttrCudaEmbeddedFunctionSource(?![\w-])")


def files_that_call_the_extractor(tools_root: Path, excluded=()) -> set[Path]:
    found = set()
    for path in tools_root.rglob("*"):
        if path.suffix.lower() not in (".ps1", ".psm1") or path in excluded:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.lstrip().startswith("#") and EXTRACTOR_NAME.search(line):
                found.add(path)
                break
    return found


@functools.lru_cache(maxsize=1)
def real_scan_result() -> dict:
    return run_scan(generators=TEMPLATE_GENERATORS, splice_sources=SPLICE_SOURCES)


def finding_key(finding: dict) -> tuple[str, str, str]:
    return (finding["function"], finding["rule"], finding["token"])


def in_scope(findings: list[dict]) -> list[dict]:
    """Drop the findings that are inert by construction (INERT_*, LOCAL_PROPERTY_SET); never a 'splice' one."""
    kept = []
    for f in findings:
        if f["rule"] == "R4" and (f["token"] in INERT_STATIC or f["token"] in INERT_INSTANCE):
            continue
        if f["rule"] == "R1" and f["token"] in INERT_COMMANDS:
            continue
        if f["rule"] == "R5" and LOCAL_PROPERTY_SET.match(f["token"]):
            continue
        kept.append(f)
    return kept


def apply_allowlist(findings: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Return (findings with no allowlist row, count mismatches, allowlist rows no finding matched)."""
    counts: dict[tuple[str, str, str], int] = {}
    for f in findings:
        counts[finding_key(f)] = counts.get(finding_key(f), 0) + 1
    unlisted = [f for f in findings if finding_key(f) not in ALLOWLIST]
    mismatched = [f"{key}: row expects {ALLOWLIST[key][0]}, body holds {n}" for key, n in sorted(counts.items())
                  if key in ALLOWLIST and ALLOWLIST[key][0] != n]
    stale = [str(k) for k in sorted(ALLOWLIST) if k not in counts]
    return unlisted, mismatched, stale


def describe(findings: list[dict]) -> str:
    return json.dumps(
        [{k: f[k] for k in ("function", "rule", "token", "line", "module", "text", "reason")} for f in findings],
        indent=2,
    )


class SplicedBodyScanHeaderTests(unittest.TestCase):
    """The scan's header declares its grammar and its non-promises (guard contract)."""

    def test_the_header_names_the_ast_grammar_and_lists_the_runtime_limits(self) -> None:
        text = SCANNER.read_text(encoding="ascii")
        header = text.split("[CmdletBinding()]", 1)[0]
        flat = re.sub(r"\s*\n#\s*", " ", header)
        self.assertIn("GRAMMAR", header)
        self.assertIn("System.Management.Automation.Language.Parser", header)
        self.assertRegex(flat, r"ParseInput\s*/\s*ParseFile")
        self.assertIn("Get-AttrCudaEmbeddedFunctionSource", header)
        self.assertRegex(flat, r"EXPLICIT LIMITS\s*/\s*NON-PROMISES")
        limits = flat[flat.index("EXPLICIT LIMITS"):]
        for needle in ("No runtime evaluation", "Invoke-Expression", "dot-sources or imports at runtime",
                       "reference-frame-runner-job.ps1", "Attr3Footage", "child executables",
                       "trusted as reviewed"):
            self.assertIn(needle, limits, f"the scan header's EXPLICIT LIMITS block lost: {needle}")
        # The parser calls the header names are the ones the scan really makes.
        self.assertGreaterEqual(text.count("[System.Management.Automation.Language.Parser]::ParseInput("), 1)
        self.assertGreaterEqual(text.count("[System.Management.Automation.Language.Parser]::ParseFile("), 1)


@requires_pwsh
@unittest.skipUnless(os.name == "nt", "the ATTR-3 host jobs and their helpers are Windows-only")
class SplicedBodyScanOnRealSourcesTests(unittest.TestCase):
    """The real generators and the real modules."""

    @property
    def result(self) -> dict:
        return real_scan_result()

    def test_every_file_that_splices_helpers_is_a_listed_splice_source(self) -> None:
        found = files_that_call_the_extractor(ROOT / "tools", SPLICE_DEFINITION_FILES)
        self.assertEqual(
            sorted(found), sorted(SPLICE_SOURCES),
            "a generator splices helper text into a job but is not a listed splice source (or the reverse)",
        )

    def test_the_scan_enumerates_a_nonempty_set_and_resolves_every_name(self) -> None:
        spliced = self.result["spliced"]
        self.assertGreater(len(spliced), 40, "suspiciously few spliced functions enumerated")
        unresolved = [v for v in self.result["splicedViolations"] if v["rule"] == "splice"]
        self.assertEqual(unresolved, [], describe(unresolved))
        names = {s["name"] for s in spliced}
        # Anchors from each generator family: an enumeration that lost a family would drop these.
        for anchor in ("Publish-AttrCudaText", "Remove-AttrCudaTree", "Get-AttrCudaSessionLocked",
                       "Publish-AttrCudaVisualCaptures", "Get-AttrCudaSelfHealMetrics", "New-AttrCudaOwnedRoot",
                       "ConvertFrom-GuiSmokeDisplayLog", "Test-AttrCudaFootagePart"):
            self.assertIn(anchor, names)

    def test_scanned_bodies_are_the_bytes_the_real_extractor_returns(self) -> None:
        by_module: dict[str, list[dict]] = {}
        for entry in self.result["spliced"]:
            by_module.setdefault(entry["module"], []).append(entry)
        probe_dir = _long_path(Path(tempfile.mkdtemp(prefix="attr3-splice-parity-")))
        self.addCleanup(shutil.rmtree, probe_dir, True)
        for module, entries in by_module.items():
            script = probe_dir / "parity.ps1"
            lines = [
                "$ErrorActionPreference = 'Stop'",
                f"Import-Module '{BACHELOR / 'AttrCudaArtifacts.psm1'}' -Force",
                "foreach ($n in @(" + ",".join("'" + e["name"] + "'" for e in entries) + ")) {",
                f"  $t = Get-AttrCudaEmbeddedFunctionSource -ModulePath '{ROOT / module}' -Name $n",
                "  $b = [Text.Encoding]::UTF8.GetBytes($t)",
                "  $h = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($b)).Replace('-', '').ToLowerInvariant()",
                "  Write-Output ($n + ' ' + $h)",
                "}",
            ]
            script.write_text("\n".join(lines) + "\n", encoding="utf-8")
            proc = run_bounded([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], PROBE_TIMEOUT_SECONDS)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            real = dict(line.split(" ", 1) for line in proc.stdout.split("\n") if line.strip())
            for entry in entries:
                self.assertEqual(real.get(entry["name"]), entry["bodySha256"],
                                 f"{module}::{entry['name']}: the scan read different text than the extractor returns")

    def test_every_spliced_body_is_clean_or_explicitly_allowlisted(self) -> None:
        findings = in_scope([v for v in self.result["splicedViolations"] if v["rule"] != "splice"])
        unlisted, mismatched, _ = apply_allowlist(findings)
        self.assertEqual(unlisted, [], "spliced helper bodies hold shapes the scan rejects and no reviewed "
                                       "allowlist row covers:\n" + describe(unlisted))
        self.assertEqual(mismatched, [], "a body holds a different number of a listed shape than its row pins; "
                                         "re-read the body and update the row")

    def test_no_allowlist_row_is_stale_or_unexplained(self) -> None:
        findings = in_scope([v for v in self.result["splicedViolations"] if v["rule"] != "splice"])
        _, _, stale = apply_allowlist(findings)
        self.assertEqual(stale, [], "allowlist rows that no current finding matches: delete them")
        for key, (count, reason) in ALLOWLIST.items():
            self.assertGreaterEqual(count, 1, f"{key}: count")
            self.assertTrue(reason.strip() and len(reason) <= 220 and "\n" not in reason, f"{key}: one-line reason required")
        for token in INERT_STATIC | INERT_INSTANCE:
            self.assertNotRegex(
                token,
                r"^IO\.File::(Write|Append|Move|Copy|Delete|Create|Open$|Replace)|^IO\.Directory::|FileStream::|Process::Start|"
                r"^\.(Write|CopyTo|Delete|Add(Script|Command)|BeginInvoke)\(\)$|InvokeMember|SetFileInformation",
                "an effectful member is on the inert list",
            )

    def test_a_raw_pub_write_added_to_a_real_spliced_helper_is_caught(self) -> None:
        # The gap itself, on the real module: a clean, genuinely spliced helper gains a raw write into $Pub.
        victim = "Get-AttrCudaLastEligibilityLine"
        module = BACHELOR / "AttrCudaArtifacts.psm1"
        text = module.read_text(encoding="utf-8")
        # The extractor's own pattern: the function ends at the first column-0 closing brace.
        body = re.search(r"(?ms)^function\s+" + re.escape(victim) + r"\s*\{.*?^\}", text)
        self.assertIsNotNone(body, f"{victim} moved; pick another clean spliced helper")
        tmp = _long_path(Path(tempfile.mkdtemp(prefix="attr3-splice-real-")))
        self.addCleanup(shutil.rmtree, tmp, True)
        mutated = tmp / "AttrCudaArtifacts.psm1"
        at = body.end() - 1  # just before the closing brace
        mutated.write_text(
            text[:at] + "    New-Item -ItemType Directory -Path (Join-Path $Pub 'sneaky') -Force | Out-Null\n" + text[at:],
            encoding="utf-8",
        )
        modules = (mutated, BACHELOR / "AttrCudaOwnerFootage.psm1", ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1")
        main_job = BACHELOR / "playback-attr-3-cuda-job.ps1"
        # The template-only scan of the real job over the mutated module: blind to it.
        self.assertEqual(run_scan(generators=[main_job], modules=modules)["violations"], [])
        found = in_scope([v for v in run_scan(generators=[main_job], modules=modules, splice_sources=[main_job])["splicedViolations"]
                          if v["rule"] != "splice"])
        unlisted, _, _ = apply_allowlist(found)
        self.assertEqual([(f["function"], f["rule"], f["token"]) for f in unlisted], [(victim, "R2", "New-Item")], describe(unlisted))

    def test_the_template_verdict_is_unchanged_by_the_new_mode(self) -> None:
        self.assertEqual(self.result["violations"], [], json.dumps(self.result["violations"], indent=2))
        self.assertEqual(len(self.result["templates"]), len(TEMPLATE_GENERATORS))


@requires_pwsh
@unittest.skipUnless(os.name == "nt", "the ATTR-3 host jobs and their helpers are Windows-only")
class SplicedBodyScanFixtureTests(unittest.TestCase):
    """Synthetic generator + module pairs: the scan must catch what the template-only scan cannot."""

    TEMPLATE = (
        "$Work = Join-Path 'C:\\mlvtmp' $JobId\n"
        "__EMBEDDED_FUNCTIONS__\n"
        "[void](Publish-AttrCudaFixtureHelper -Name 'x')\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-splice-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = _long_path(Path(self._tmp.name))

    def build(self, helper_body: str, *, name: str = "Publish-AttrCudaFixtureHelper", name_list: str | None = None):
        module = self.tmp / "FixtureModule.psm1"
        module.write_text(f"function {name} {{\n{helper_body}\n}}\n", encoding="utf-8")
        generator = self.tmp / "fixture-gen.ps1"
        names = name_list if name_list is not None else f"'{name}'"
        generator.write_text(
            f"$embedded = Get-AttrCudaEmbeddedFunctionSource -Name @({names})\n"
            "$template = @'\n" + self.TEMPLATE + "'@\n",
            encoding="utf-8",
        )
        return generator, module

    def template_only_scan(self, generator: Path, module: Path) -> dict:
        # Exactly the invocation test_every_emitted_template_parses_and_trips_no_rule makes.
        return run_scan(generators=[generator], modules=[module])

    def spliced_scan(self, generator: Path, module: Path) -> dict:
        return run_scan(generators=[generator], modules=[module], splice_sources=[generator])

    def test_raw_new_item_force_into_pub_in_a_spliced_helper_passes_the_old_scan_and_trips_the_new(self) -> None:
        generator, module = self.build(
            "    param([string]$Name)\n"
            "    New-Item -ItemType Directory -Path (Join-Path $Pub $Name) -Force | Out-Null"
        )
        old = self.template_only_scan(generator, module)
        self.assertEqual(old["violations"], [], "the template-only scan is expected to MISS this (that is the gap)")
        new = self.spliced_scan(generator, module)
        self.assertEqual(new["violations"], [], "the template verdict must not change")
        self.assertEqual([(f["function"], f["rule"]) for f in new["splicedViolations"]],
                         [("Publish-AttrCudaFixtureHelper", "R2")], describe(new["splicedViolations"]))
        self.assertEqual(new["splicedViolations"][0]["token"], "New-Item")
        self.assertEqual(new["spliced"][0]["name"], "Publish-AttrCudaFixtureHelper")

    def test_a_raw_set_content_into_pub_is_flagged_r1(self) -> None:
        generator, module = self.build("    Set-Content -LiteralPath (Join-Path $Pub 'x') -Value 1")
        found = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual([(f["rule"], f["token"]) for f in found], [("R1", "Set-Content")], describe(found))

    def test_a_dotnet_static_write_into_pub_is_flagged_r4(self) -> None:
        generator, module = self.build("    [IO.File]::WriteAllText((Join-Path $Pub 'x'), 'y')")
        found = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual([(f["rule"], f["token"]) for f in found], [("R4", "IO.File::WriteAllText")], describe(found))

    def test_int_min_and_max_value_scan_clean_but_the_allowlist_does_not_widen_to_the_whole_type(self) -> None:
        # The range-checked Int32 cast in Get-AttrCudaSelfHealMetrics (#659) reads both constants.
        generator, module = self.build(
            "    param([long]$code)\n"
            "    $code -ge [int]::MinValue -and $code -le [int]::MaxValue"
        )
        clean = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual(clean, [], describe(clean))
        # Another static member of the same type is still judged on its own name.
        generator, module = self.build("    [int]::Parse('1')")
        found = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual([(f["rule"], f["token"]) for f in found], [("R4", "int::Parse")], describe(found))

    def test_a_redirection_into_pub_is_flagged_r6(self) -> None:
        generator, module = self.build("    Get-Date > (Join-Path $Pub 'x')")
        found = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual([f["rule"] for f in found], ["R6"], describe(found))

    def test_a_clean_helper_is_not_flagged(self) -> None:
        generator, module = self.build(
            "    param([string]$Name)\n"
            "    $leaf = Join-Path $Work 'a'\n"
            "    New-Item -ItemType Directory -Path (Join-Path $leaf 'b') -Force | Out-Null\n"
            "    Test-Path -LiteralPath $leaf"
        )
        result = self.spliced_scan(generator, module)
        self.assertEqual(result["splicedViolations"], [])
        self.assertEqual([s["name"] for s in result["spliced"]], ["Publish-AttrCudaFixtureHelper"])

    def test_a_spliced_name_that_resolves_to_no_module_is_a_finding_not_a_silent_skip(self) -> None:
        generator, module = self.build("    Get-Date", name_list="'Publish-AttrCudaFixtureHelper', 'Publish-AttrCudaNoSuchHelper'")
        found = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual([(f["rule"], f["function"]) for f in found], [("splice", "Publish-AttrCudaNoSuchHelper")], describe(found))

    def test_a_nonliteral_name_list_is_a_finding_not_a_silent_skip(self) -> None:
        generator, module = self.build("    Get-Date", name_list="$dynamicNames")
        found = self.spliced_scan(generator, module)["splicedViolations"]
        self.assertEqual([f["rule"] for f in found], ["splice"], describe(found))

    def test_without_the_new_parameter_the_output_carries_no_splice_findings(self) -> None:
        generator, module = self.build("    New-Item -ItemType Directory -Path (Join-Path $Pub 'x') -Force | Out-Null")
        result = self.template_only_scan(generator, module)
        self.assertEqual(result["spliced"], [])
        self.assertEqual(result["splicedViolations"], [])

    SNEAKY = "    New-Item -ItemType Directory -Path (Join-Path $Pub 'sneaky') -Force | Out-Null"

    def build_beside_an_ordinary_call(self, variant_call: str):
        """An ordinary extractor call plus one more spelling of it; only the second names the sneaky helper."""
        module = self.tmp / "FixtureModule.psm1"
        module.write_text(
            "function Publish-AttrCudaFixtureOrdinary {\n    Get-Date\n}\n\n"
            f"function Publish-AttrCudaFixtureHelper {{\n{self.SNEAKY}\n}}\n",
            encoding="utf-8",
        )
        generator = self.tmp / "fixture-gen.ps1"
        generator.write_text(
            "$embedded = Get-AttrCudaEmbeddedFunctionSource -Name @('Publish-AttrCudaFixtureOrdinary')\n"
            f"$embedded = $embedded + (\"`r`n\" + ({variant_call}))\n"
            "$template = @'\n" + self.TEMPLATE + "'@\n",
            encoding="utf-8",
        )
        return generator, module

    def assert_the_variant_call_is_scanned(self, variant_call: str) -> None:
        generator, module = self.build_beside_an_ordinary_call(variant_call)
        result = self.spliced_scan(generator, module)
        self.assertEqual([(f["function"], f["rule"], f["token"]) for f in result["splicedViolations"]],
                         [("Publish-AttrCudaFixtureHelper", "R2", "New-Item")], describe(result["splicedViolations"]))
        self.assertEqual(sorted(s["name"] for s in result["spliced"]),
                         ["Publish-AttrCudaFixtureHelper", "Publish-AttrCudaFixtureOrdinary"])

    def test_a_lowercase_extractor_call_is_scanned_like_the_exact_case_one(self) -> None:
        self.assert_the_variant_call_is_scanned("get-attrcudaembeddedfunctionsource -Name @('Publish-AttrCudaFixtureHelper')")

    def test_a_module_qualified_extractor_call_is_scanned_like_the_bare_one(self) -> None:
        self.assert_the_variant_call_is_scanned("FixtureModule\\Get-AttrCudaEmbeddedFunctionSource -Name @('Publish-AttrCudaFixtureHelper')")

    def test_a_lowercase_module_qualified_extractor_call_with_a_positional_list_is_scanned(self) -> None:
        self.assert_the_variant_call_is_scanned("attrcudaartifacts\\GET-ATTRCUDAEMBEDDEDFUNCTIONSOURCE @('Publish-AttrCudaFixtureHelper')")


class SpliceInventoryCallShapeTests(unittest.TestCase):
    """The census of splice sources, and the bound on the subprocesses, in a throwaway tree (never the real one)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-splice-inventory-")
        self.addCleanup(self._tmp.cleanup)
        self.tools = _long_path(Path(self._tmp.name)) / "tools"
        self.tools.mkdir()

    def census(self, files: dict[str, str], excluded=()) -> set[str]:
        for name, text in files.items():
            (self.tools / name).write_text(text, encoding="utf-8")
        return {p.name for p in files_that_call_the_extractor(self.tools, {self.tools / e for e in excluded})}

    def test_every_spelling_and_argument_shape_of_the_call_is_found(self) -> None:
        found = self.census({
            "exact.ps1": "$e = Get-AttrCudaEmbeddedFunctionSource -Name @('A')\n",
            "lower.ps1": "$e = get-attrcudaembeddedfunctionsource -Name @('A')\n",
            "upper.psm1": "$e = GET-ATTRCUDAEMBEDDEDFUNCTIONSOURCE -Name @('A')\n",
            "positional.ps1": "$e = Get-AttrCudaEmbeddedFunctionSource @('A')\n",
            "splat.ps1": "$e = Get-AttrCudaEmbeddedFunctionSource @splat\n",
            "bare.ps1": "$e = Get-AttrCudaEmbeddedFunctionSource\n",
            "qualified.ps1": "$e = AttrCudaArtifacts\\Get-AttrCudaEmbeddedFunctionSource @('A')\n",
            "qualified-lower.psm1": "$e = attrcudaartifacts\\get-attrcudaembeddedfunctionsource $names\n",
            "parenthesised.ps1": "$j = $a + (Get-AttrCudaEmbeddedFunctionSource -ModulePath $m -Name @('A'))\n",
        })
        self.assertEqual(found, {
            "exact.ps1", "lower.ps1", "upper.psm1", "positional.ps1", "splat.ps1", "bare.ps1",
            "qualified.ps1", "qualified-lower.psm1", "parenthesised.ps1",
        })

    def test_a_file_that_only_mentions_the_name_or_is_not_powershell_is_not_a_splice_source(self) -> None:
        found = self.census({
            "comment.ps1": "# splices come from Get-AttrCudaEmbeddedFunctionSource\nGet-Date\n",
            "other-command.ps1": "Get-AttrCudaEmbeddedFunctionSourceCount\nInvoke-Get-AttrCudaEmbeddedFunctionSource\n",
            "notes.md": "Get-AttrCudaEmbeddedFunctionSource @splat\n",
            "defines.ps1": "function Get-AttrCudaEmbeddedFunctionSource { }\n",
        }, excluded=("defines.ps1",))
        self.assertEqual(found, set())

    def test_every_subprocess_run_in_this_file_carries_a_timeout(self) -> None:
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "run" and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertGreaterEqual(len(calls), 1)
        for call in calls:
            self.assertIn("timeout", [k.arg for k in call.keywords], f"subprocess.run at line {call.lineno} has no timeout=")

    def test_a_process_that_outlives_its_bound_fails_the_test_instead_of_hanging(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            run_bounded([sys.executable, "-I", "-c", "import time; time.sleep(60)"], 1)
        self.assertIn("did not finish within 1s", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
