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

# An R5 finding on `$local.property = ...` is inert only when the scan proves the receiver is an object
# this same body just BUILT: every assignment to the variable is a hashtable / [ordered] / [pscustomobject]
# literal ("literal") or a New-Object / [Type]::new of a type on INERT_LOCAL_TYPES ("constructed:<Type>").
# A receiver that came from a command, a member or a parameter ("other", "unbound", "nested", ...) can be a
# foreign object, and `$f = Get-Item $Pub; $f.Attributes = ...` is a file write: that stays a finding and
# needs a reviewed ALLOWLIST row. The scan reports the receiver's origin; this file decides.
LOCAL_PROPERTY_SET = re.compile(r"^\$(?!Work\b|Pub\b|Cache\b|AgentRoot\b|Root\b)[A-Za-z_]\w*(\.\w+)+$")
# Types a body builds only to fill in and hand to an effectful call that is itself a finding (Process.Start):
# setting their fields touches no file, process or registry. A variable that is ALSO bound some other way
# (foreach, [ref], -OutVariable, a parameter, a multi-target assignment) is no longer provably this object.
INERT_LOCAL_TYPES = frozenset("""
System.Diagnostics.ProcessStartInfo Diagnostics.ProcessStartInfo
""".split())


def local_property_set_is_inert(finding: dict) -> bool:
    if finding["rule"] != "R5" or not LOCAL_PROPERTY_SET.match(finding["token"]):
        return False
    origins = [o for o in finding.get("origin", "").split(";") if o]
    return bool(origins) and all(
        o == "literal" or (o.startswith("constructed:") and o[len("constructed:"):] in INERT_LOCAL_TYPES) for o in origins)

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

# ---- property sets whose receiver the scan could not prove is a fresh local object (ATTR3-SCAN-COVERAGE-LIMITS-1 L1) ----
_allow("Get-AttrCudaPresentMonWindowResult",
       "mutates a shallow PSObject.Copy() of the caller's parsed result and its nested process record; in-memory data only, nothing is written back to a file",
       ("R5", "$clone.process", 1), ("R5", "$clone.process.endedAtUtc", 1))
_allow("Get-AttrCudaWindowsDisplayInventory",
       "sets the size field of an AttrCudaDisplayDevice / AttrCudaDevMode struct this body built with New-Object, which EnumDisplay* then fills through [ref]; in-memory only",
       ("R5", "$adapter.cb", 1), ("R5", "$monitor.cb", 1), ("R5", "$mode.dmSize", 1))
_allow("Remove-AttrCudaFileByProof",
       "sets fields of the FileDispositionInfo / FileBasicInfo structs this body built with ::new() and hands to SetFileInformationByHandle through [ref]; the call is the finding",
       ("R5", "$disposition.DeleteFile", 1), ("R5", "$basic.FileAttributes", 1))
_allow("Invoke-AttrCudaBoundedProbe",
       "attaches the throwaway runspace to the [powershell]::Create() shell this body made; the runspace and its commands stay findings on their own",
       ("R5", "$probeShell.Runspace", 1))
_allow("Start-AttrCudaDisplayWakeKeepAlive",
       "writes counters and messages into the synchronized in-memory $NudgeState table the body builds and shares with its keep-alive runspace, and attaches the runspace to its shell",
       ("R5", "$NudgeState.count", 3), ("R5", "$NudgeState.failureCount", 4), ("R5", "$NudgeState.lastDesktopName", 1),
       ("R5", "$NudgeState.lastError", 4), ("R5", "$NudgeState.lastFailureUtc", 4), ("R5", "$NudgeState.sessionLockReason", 2),
       ("R5", "$NudgeState.successCount", 1), ("R5", "$shell.Runspace", 1))

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
        if local_property_set_is_inert(f):
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
        # ATTR3-SCAN-COVERAGE-LIMITS-1: the header states the module binding and the property-set origin exactly.
        for needle in ("MODULE BINDING", "the module the extractor will read for THAT call", "fail closed",
                       "PROPERTY SETS IN BODIES", "'constructed:<Type>'", "this script still decides nothing",
                       # r9: the class rule, and the rules a spliced body is held to (R8 included).
                       "UNPROVEN BINDING", "unproven-binding", "$PSDefaultParameterValues", "R1 R2 R3 R4 R5 R6 R8"):
            self.assertIn(needle, flat, f"the scan header no longer states: {needle}")
        self.assertNotIn("security boundary", flat.replace("NOT a security boundary", "").replace("not a security boundary", ""),
                         "the scan is a regression tripwire; its header must not claim it is a security boundary")
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
        # r9: no real call, body or module binds by a route the scan cannot prove.
        unproven = [v for v in self.result["splicedViolations"] if "unproven-binding" in (v["rule"], v["token"])]
        self.assertEqual(unproven, [], describe(unproven))
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


@requires_pwsh
@unittest.skipUnless(os.name == "nt", "the ATTR-3 host jobs and their helpers are Windows-only")
class SplicedBodyScanCoverageLimitTests(unittest.TestCase):
    """ATTR3-SCAN-COVERAGE-LIMITS-1: the two limits Dot recorded on #655, pinned with concrete inputs.

    L1: a `$local.Property = ...` finding inside a spliced helper body was dropped by SHAPE alone, so a helper
    that sets a property on a foreign object (`$f = Get-Item $Pub; $f.Attributes = ...`) passed the policy.
    L2: a splice call site was resolved against EVERY -ModulePath module instead of the module its own
    extractor call reads, so a same-named helper in another module could be scanned in its place.
    This is a regression tripwire, not a security boundary (see the scan's header).

    Five scans cover every case (one generator holds many call sites, one helper name per case), so the class
    costs five pwsh starts, not one per test: the Windows shard budget has no slack for more.
    """

    RAW_WRITE = "    New-Item -ItemType Directory -Path (Join-Path $Pub 'sneaky') -Force | Out-Null"
    CLEAN = "    Get-Date"
    TEMPLATE = "$Work = Join-Path 'C:\\mlvtmp' $JobId\n__EMBEDDED_FUNCTIONS__\n"

    L1_BODIES = [
        ("L1Foreign", "    param([string]$Target)\n    $item = Get-Item -LiteralPath $Target\n    $item.Attributes = 'Hidden'"),
        ("L1Param", "    param($Target)\n    $Target.IsReadOnly = $true"),
        ("L1Mixed", "    param([string]$Target)\n    $r = [ordered]@{}\n    $r = Get-Item -LiteralPath $Target\n    $r.Attributes = 'Hidden'"),
        # New-Object itself is judged on its own (R1, per type); a property set on a FileInfo writes the file's attributes.
        ("L1FileInfo", "    param([string]$Target)\n    $fi = New-Object System.IO.FileInfo $Target\n    $fi.IsReadOnly = $true"),
        ("L1Nested", "    $r = [ordered]@{ inner = $null }\n    $r.inner.Attributes = 'Hidden'"),
        ("L1Root", "    $Pub.Attributes = 'Hidden'"),
        ("L1Fresh", "    $r = [ordered]@{}\n    $r.a = 1\n    $o = [pscustomobject]@{ a = 1 }\n    $o.a = 2\n"
                    "    $psi = New-Object System.Diagnostics.ProcessStartInfo\n    $psi.FileName = 'x'"),
        ("L1IndexRoot", "    $Pub['k'] = 1"),
        ("L1IndexLocal", "    $h = @{}\n    $h['k'] = 1\n    $list = New-Object 'System.Collections.Generic.List[object]'"),
        # Adversary A1: a variable is also bound by foreach, -OutVariable, a multi-target assignment, [ref] and a parameter.
        ("L1Foreach", "    param([string]$Target)\n    $r = @{}\n    foreach ($r in (Get-Item -LiteralPath $Target)) { $r.Attributes = 'Hidden' }"),
        ("L1OutVariable", "    param([string]$Target)\n    $r = @{}\n    Get-Item -LiteralPath $Target -ov r | Out-Null\n    $r.Attributes = 'Hidden'"),
        ("L1MultiTarget", "    param([string]$Target)\n    $r = @{}\n    $x, $r = Get-Item -LiteralPath $Target\n    $r.Attributes = 'Hidden'"),
        ("L1Ref", "    $r = @{}\n    $null = [int]::TryParse('1', [ref]$r)\n    $r.Attributes = 'Hidden'"),
        ("L1ParamAlso", "    param($r)\n    $r = @{}\n    $r.Attributes = 'Hidden'"),
        # Adversary round 2. A1: automatic pipeline variables; A2: scope-qualified binders; A3: a type conversion is not a
        # literal; A4: an assignment must come first, in an enclosing block, outside any nested function.
        ("L1AutoUnderscore", "    param($Target)\n    $_ = @{}\n    Get-Item -LiteralPath $Target | ForEach-Object { $_.Attributes = 'Hidden' }"),
        ("L1AutoPSItem", "    param($Target)\n    $PSItem = @{}\n    Get-Item -LiteralPath $Target | ForEach-Object { $PSItem.Attributes = 'Hidden' }"),
        ("L1ScopedForeach", "    param($Target)\n    $r = @{}\n    foreach ($local:r in (Get-Item -LiteralPath $Target)) { $r.Attributes = 'Hidden' }"),
        ("L1ScopedPipeVar", "    param($Target)\n    $r = @{}\n    Get-Item -LiteralPath $Target -PipelineVariable local:r | ForEach-Object { $r.Attributes = 'Hidden' }"),
        ("L1CastRhs", "    $r = [System.Diagnostics.EventLog]@{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        ("L1CastLhs", "    [System.Diagnostics.EventLog]$r = @{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        ("L1NestedAssign", "    function G { $r = @{} }\n    $r.Attributes = 'Hidden'"),
        ("L1LateAssign", "    $r.Attributes = 'Hidden'\n    $r = @{}"),
        ("L1ConditionalAssign", "    param($Cond)\n    if ($Cond) { $r = @{} }\n    $r.Attributes = 'Hidden'"),
        ("L1SetterInNestedFunction", "    $r = @{}\n    function G { $r.Attributes = 'Hidden' }"),
        # Still fresh: the assignment comes first in an enclosing block, and the setter sits in a deeper block.
        ("L1FreshNested", "    $r = [ordered]@{}\n    if ($true) { $r.a = 1 }"),
        # Round 2 (third adversary pass, A2-A5): only an UNSCOPED assignment makes a receiver fresh; a trap or a deferred
        # script block runs when the caller says, not where it is written; a local function named like a command the
        # scan reasons about (New-Object) changes what that command does.
        ("L1ScopedAssign", "    $script:r = @{}\n    $r.Attributes = 2"),
        ("L1TrapSetter", "    throw 'boom'\n    $r = @{}\n    trap { $r.Attributes = 2; continue }"),
        ("L1DeferredBlock", "    $r = @{}\n    { $r.Attributes = 2 }"),
        ("L1ShadowNew", "    param([string]$Target)\n    function New-Object { param($TypeName) Get-Item -LiteralPath $Target }\n"
                        "    $r = New-Object System.Diagnostics.ProcessStartInfo\n    $r.Attributes = 2"),
        # Still fresh: a script block handed to a command runs in line.
        ("L1InlineBlock", "    $r = [ordered]@{}\n    1 | ForEach-Object { $r.a = $_ }"),
        # r4 (adversary r3 A3 = Dot B4): a typed parameter named WITH a scope qualifier binds the same variable, so the
        # hashtable assigned after it is converted to the parameter's type (an EventLog) and the setter writes the log.
        ("L1TypedLocal", "    param([System.Diagnostics.EventLog]$local:r)\n    $r = @{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        ("L1TypedScript", "    param([System.Diagnostics.EventLog]$script:r)\n    $r = @{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        ("L1TypedPrivate", "    param([System.Diagnostics.EventLog]$private:r)\n    $r = @{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        ("L1TypedGlobal", "    param([System.Diagnostics.EventLog]$global:r)\n    $r = @{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        # Control: a scoped parameter with ANOTHER name does not touch $r, which stays a fresh literal.
        ("L1TypedOther", "    param([System.Diagnostics.EventLog]$local:other)\n    $r = @{ Log = 'Application' }\n    $r.MaximumKilobytes = 1024"),
        # r5 (Dot B8 = r4 adversary A3): a data section binds its variable to a command result, so the setter below it reaches a
        # FileInfo, not the hashtable assigned first.
        # r5 adversary pass 2 (A2): a trap that continues lets the setter run although the throwing construction never assigned $r,
        # so the receiver is the caller's object, not the literal.
        ("L1TrapConstruct", "    trap { continue }\n    $r = @{ x = $(throw 'bad') }\n    $r.Attributes = 'Hidden'"),
        ("L1DataStatement","    $r = @{}\n    data r -SupportedCommand Get-Item { Get-Item -LiteralPath 'C:\\fixture\\target' }\n    $r.Attributes = 'Hidden'"),    ]

    # Adversary A8 (same class as A3): template shapes that name a parameter by alias or abbreviation.
    TEMPLATE_ALIASES = (
        "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c echo x' -RSO 'C:\\outside\\stdout.txt'\n"
        "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c echo x' -RSE 'C:\\outside\\stderr.txt'\n"
        "New-Item -ItemType Directory -Path $Work -N:'..\\escape' -Force\n"
        # Third adversary pass A7: a template function named like a command the proof evaluates changes the proof.
        "function Join-Path { param($Path, $ChildPath) 'C:\\outside' }\n"
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="attr3-coverage-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.tmp = _long_path(Path(cls._tmp.name))
        names = ", ".join(repr(n) for n, _ in cls.L1_BODIES)
        cls.l1 = cls._scan("l1", [("Body.psm1", cls.L1_BODIES)], ["Body.psm1"],
                           [f"Get-AttrCudaEmbeddedFunctionSource -Name @({names})"], template_extra=cls.TEMPLATE_ALIASES)
        outside = cls.tmp / "outside"
        outside.mkdir()
        (outside / "Clean.psm1").write_text(f"function Collide {{\n{cls.RAW_WRITE}\n}}\n", encoding="utf-8")
        l2_dir = cls.tmp / "l2"
        # No -ModulePath module defines the extractor here, so every module is a candidate for a call that names none.
        cls.l2 = cls._scan("l2", [
            ("Clean.psm1", [(n, cls.CLEAN) for n in ("LitClean", "LitDirty", "OffList", "Ambig", "AmbigDyn", "Spl", "Collide", "CollideSub", "IdxAmbig", "Nm", "RedirA", "RedirB")]),
            ("Dirty.psm1", [(n, cls.RAW_WRITE) for n in ("LitClean", "LitDirty", "Ambig", "AmbigDyn", "IdxAmbig", "NmNm", "RedirA", "RedirB")]),
            ("Only.psm1", [("OnlyHere", cls.CLEAN)]),
        ], ["Clean.psm1", "Dirty.psm1", "Only.psm1"], [
            "Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1') -Name @('LitClean')",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('LitDirty') -ModulePath (Join-Path $PSScriptRoot 'Dirty.psm1')",
            "Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'Off.psm1') -Name @('OffList')",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('OffOnly')",
            "Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'Off.psm1') -Name @('OffOnly')",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('Ambig')",
            "Get-AttrCudaEmbeddedFunctionSource -ModulePath $somePath -Name @('AmbigDyn')",
            "Get-AttrCudaEmbeddedFunctionSource -ModulePath $somePath -Name @('OnlyHere')",
            "Get-AttrCudaEmbeddedFunctionSource @('Spl') (Join-Path $PSScriptRoot 'Dirty.psm1')",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('Spl') @extra",
            # A2: a same-named file OUTSIDE the scanned set must not be matched by its base name.
            f"Get-AttrCudaEmbeddedFunctionSource -Name @('Collide') -ModulePath '{outside / 'Clean.psm1'}'",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('CollideSub') -ModulePath (Join-Path $PSScriptRoot 'sub\\Clean.psm1')",
            # A4: an expression that evaluates to a module path is not a literal one.
            f"Get-AttrCudaEmbeddedFunctionSource -Name @('IdxAmbig') -ModulePath (@('{l2_dir / 'Dirty.psm1'}', '{l2_dir / 'Clean.psm1'}')[0])",
            # A7: a -Name that is an expression is not a literal name list.
            "Get-AttrCudaEmbeddedFunctionSource -Name ('Nm' + 'Nm')",
            # r7 (adversary r6 A5): a redirection discards the value, so the extractor gets no -ModulePath at all; the call binds to no module.
            "Get-AttrCudaEmbeddedFunctionSource -Name @('RedirA') -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1' > $null)",
            f"Get-AttrCudaEmbeddedFunctionSource -Name @('RedirB') -ModulePath ('{l2_dir / 'Clean.psm1'}' > $null)",
            # r7 (adversary r6 A4 advisory): a comma list inside a comma list binds the inner list as one joined string.
            "Get-AttrCudaEmbeddedFunctionSource -Name (('Nes','ted'),'Safe') -ModulePath 'C:\\fixture\\Bodies.psm1'",
        ], extra_files=[("sub/Clean.psm1", [("CollideSub", cls.RAW_WRITE)])])
        # The extractor's own module is on the list, so a call that names no -ModulePath reads exactly that module.
        cls.l2_default = cls._scan("l2-default", [
            ("Extractor.psm1", [("Get-AttrCudaEmbeddedFunctionSource", "    param($Name)"), ("DefHelper", cls.CLEAN), ("AbbrevM", cls.CLEAN), ("NameAbbrev", cls.CLEAN), ("CaseRoot", cls.CLEAN)]),
            ("Dirty.psm1", [("DefHelper", cls.RAW_WRITE), ("DirtyOnly", cls.RAW_WRITE), ("AbbrevM", cls.RAW_WRITE), ("EmptyOnly", cls.RAW_WRITE), ("CaseRoot", cls.RAW_WRITE)]),
        ], ["Extractor.psm1", "Dirty.psm1", "Notes.psm1"], [
            "Get-AttrCudaEmbeddedFunctionSource -Name @('DefHelper')",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('DirtyOnly')",
            # A3: PowerShell binds -M to -ModulePath and -N to -Name (unique prefixes).
            "Get-AttrCudaEmbeddedFunctionSource -Name @('AbbrevM') -M (Join-Path $PSScriptRoot 'Dirty.psm1')",
            "Get-AttrCudaEmbeddedFunctionSource -N @('NameAbbrev')",
            # A5: an empty, blank or null -ModulePath makes the extractor fall back to its own module.
            "Get-AttrCudaEmbeddedFunctionSource -Name @('EmptyOnly') -ModulePath ''",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('EmptyOnly') -ModulePath ' '",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('EmptyOnly') -ModulePath $null",
            # Round 2 A5: PowerShell variable and command names are case-insensitive.
            "Get-AttrCudaEmbeddedFunctionSource -Name @('EmptyOnly') -ModulePath $NULL",
            "Get-AttrCudaEmbeddedFunctionSource -Name @('CaseRoot') -ModulePath (join-path $psscriptroot 'Dirty.psm1')",
        ], raw_modules={"Notes.psm1": "<#\nfunction Get-AttrCudaEmbeddedFunctionSource {\n}\n#>\n"})
        # Third adversary pass A1 / A6: a positional path after a common-parameter ALIAS (-vb, -db) binds -ModulePath, and a
        # generator that defines its own Join-Path / Split-Path / alias changes what the scan's own evaluation means.
        cls.shadow = cls._scan("shadow", [
            ("Clean.psm1", [("PosVb", cls.CLEAN), ("PosDb", cls.CLEAN), ("ShadowF", cls.CLEAN)]),
            ("Dirty.psm1", [("ShadowF", cls.RAW_WRITE)]),
        ], ["Clean.psm1", "Dirty.psm1"], [
            "Get-AttrCudaEmbeddedFunctionSource -Name 'PosVb' -vb (Join-Path $PSScriptRoot 'Dirty.psm1')",
            "Get-AttrCudaEmbeddedFunctionSource -Name 'PosDb' -db (Join-Path $PSScriptRoot 'Dirty.psm1')",
            "Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1') -Name 'ShadowF'",
        ], generator_preamble=("function Join-Path { param($Path, $ChildPath) 'C:\\fixture\\Dirty.psm1' }\n"
                               "function global:Split-Path { param($Path) 'C:\\fixture' }\n"
                               "Set-Alias -Name Get-Item -Value Get-Date\n"
                               "New-Alias New-Object Get-Date\n"))
        # r3 (Dot B1): the colon form `-Name:Join-Path` keeps the name in the parameter's own Argument, so a scan that read
        # only the command's string elements never saw it. One scan covers every spelling, computed name and provider route.
        cls.alias = cls._scan("alias", [
            ("Clean.psm1", [(n, cls.CLEAN) for n in ("DotF", "QualF", "DriveF", "RootF", "RenF")]),
            ("Dirty.psm1", [(n, cls.RAW_WRITE) for n in ("DotF", "QualF", "DriveF", "RootF", "RenF")]),
        ], ["Clean.psm1", "Dirty.psm1"], [
            "Get-AttrCudaEmbeddedFunctionSource -Name 'DotF' -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1')",
            # Third-pass adversary A2 / A3: a module-qualified extractor runs another module's extractor, and a drive-relative
            # or root-relative literal is "rooted" yet resolves against the working directory of whoever runs it.
            "Dirty\\Get-AttrCudaEmbeddedFunctionSource -Name 'QualF'",
            "Get-AttrCudaEmbeddedFunctionSource -Name 'DriveF' -ModulePath 'C:Clean.psm1'",
            "Get-AttrCudaEmbeddedFunctionSource -Name 'RootF' -ModulePath '\\Clean.psm1'",
            # r4 (A1): the rename in the preamble (`-NewName ('Join-Path')`) makes runtime read Dirty.psm1 here.
            "Get-AttrCudaEmbeddedFunctionSource -Name 'RenF' -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1')",
        ], generator_preamble=cls.ALIAS_PREAMBLE, template_extra=cls.ALIAS_TEMPLATE)

    # Every line is its own finding, found again by its exact text. Redirect is the Dot counterexample's replacement command.
    ALIAS_SHADOWS = (
        "Set-Alias -Name:Join-Path -Value:Redirect",
        "New-Alias -Name:Join-Path -Value:Redirect",
        "Set-Alias -N:Join-Path -Value:Redirect",
        "Set-Alias -Na:Split-Path -Value:Redirect",
        "Set-Alias -Name:'Join-Path' -Value Redirect",
        'Set-Alias -Name:"Split-Path" -Value:Redirect',
        "New-Alias -Name:New-Object -Value:Redirect",
        "Set-Alias -Name:global:Join-Path -Value:Redirect",
        "sal -Name:JOIN-PATH Redirect",
        "Microsoft.PowerShell.Utility\\New-Alias -Value:Redirect -Name:Split-Path",
    )
    # A name the scan cannot read as a string constant could be any command, so each of these is a finding of its own kind.
    ALIAS_COMPUTED = (
        "Set-Alias -Name:$aliasName -Value:Redirect",
        "Set-Alias -Name ($aliasPrefix + '-Path') Redirect",
        'New-Alias "$aliasPrefix-Path" Redirect',
        "Set-Alias -Value:Redirect $aliasName",
        "Set-Alias @aliasSplat",
        # Adversary A1: -vb / -db are switches, so the computed name after them is still the alias name.
        "Set-Alias -vb $aliasVb Redirect",
        "New-Alias -db $aliasDb Redirect",
    )
    # Other routes to a command called Join-Path / Split-Path / New-Object: the alias: and function: drives.
    PROVIDER_ROUTES = (
        "Set-Item -Path alias:Join-Path -Value Redirect",
        "New-Item -Path function:\\Split-Path -Value { 'C:\\fixture' }",
        "Set-Item -LiteralPath:Alias::New-Object Redirect",
        "${function:Join-Path} = { 'C:\\fixture\\Dirty.psm1' }",
        "${alias:global:Split-Path} = 'Redirect'",
        "New-Item -Path alias: -Name Join-Path -Value Redirect",
        'Set-Item -Path "function:$routeName" -Value { 1 }',
        # Adversary pass 2: a leading separator after the drive, and a rename or move whose NEW name is the evaluated command.
        "${function:\\Join-Path} = { 'C:\\fixture\\Dirty.psm1' }",
        "Rename-Item -LiteralPath function:Redirect -NewName Join-Path",
        "Rename-Item -LiteralPath alias:RedirectAlias -NewName:Split-Path",
        "Move-Item function:Redirect New-Object",
        # r4 (adversary r3 A1 = Dot B2): a constant wrapped in parentheses, a sub-expression, a cast or a concatenation is still
        # a constant, and a source or new name written that way is the same rename.
        "Rename-Item -LiteralPath function:Redirect -NewName ('Join-Path')",
        "Rename-Item -LiteralPath ('function:Redirect') -NewName Split-Path",
        "Rename-Item -LiteralPath function:Redirect -NewName:$('Split-Path')",
        "Move-Item function:Redirect (('New-' + 'Object'))",
        "Rename-Item -LiteralPath function:Redirect -NewName ([string]'Join-Path')",
        "Copy-Item -LiteralPath function:Redirect -Destination ('function:Join-Path')",
        # r4 adversary pass 1 (A2): a single-element array expression is the constant it holds.
        "Rename-Item -LiteralPath ([string]@('function:Redirect')) -NewName ([string]@('Join-Path'))",
        # r5 (Dot B5): a route built from constants ends in a protected name the plain-string check never sees; the tail is read
        # after the route is stripped, and an empty or wildcard tail fails closed.
        "Copy-Item function:Redirect ('fun'+'ction:Join-Path')",
        "Copy-Item function:Redirect ('function:' + 'Split-Path')",
        "Copy-Item -LiteralPath function:Redirect -Destination ('function:' + '*')",
        "Copy-Item function:Redirect ('function:' + '')",
        # r5 (Dot B6 = r4 adversary A1): './', '.\' and doubled separators after the drive name the same command, as does a scope
        # qualifier among them.
        "Set-Item -LiteralPath function:./Join-Path -Value { param($p, $c) 'C:\\fixture\\Dirty.psm1' }",
        "Set-Item -LiteralPath function:.\\Join-Path -Value { param($p, $c) 'C:\\fixture\\Dirty.psm1' }",
        "Set-Item -LiteralPath function:\\\\Join-Path -Value { param($p, $c) 'C:\\fixture\\Dirty.psm1' }",
        "Set-Item -LiteralPath function:.\\local:Split-Path -Value { 'C:\\fixture' }",
        "Set-Item -Path alias:./New-Object -Value Redirect",
        "${function:./Join-Path} = { 'C:\\fixture\\Dirty.psm1' }",
        # r5 adversary pass 1 (A1): a braced function: / alias: variable with a wildcard or empty tail names a command the scan cannot
        # read, so it fails closed like the string form does (the tail is a wildcard against the extractor's own name).
        "${function:Get-AttrCudaEmbeddedFunctionSourc?} = { param($Name, $ModulePath) 'function F { }' }",
        "${function:Join-Pa*} = { 'C:\\fixture\\Dirty.psm1' }",
        "${alias:./Split-Pat[h]} = 'Redirect'",
        "${function:} = { 1 }",
        # r5 adversary pass 2 (A1): '..' segments resolve to the same provider item, and a separator left inside the tail is a path
        # the scan cannot read as one name.
        "Set-Item -LiteralPath function:../Join-Path -Value { 'C:\\fixture\\Dirty.psm1' }",
        "Set-Item -LiteralPath alias:../Join-Path -Value Redirect",
        "${function:../Join-Path} = { 'C:\\fixture\\Dirty.psm1' }",
        "Set-Item -LiteralPath function:Redirect/../Split-Path -Value { 'C:\\fixture' }",
    )
    # r4: a rename or move that touches the function: / alias: drive, or names an evaluated command, with an operand the scan cannot
    # reduce to a constant, could be installing a shadow: it fails closed, in a kind of its own.
    RENAME_COMPUTED = (
        "Rename-Item -LiteralPath function:Redirect -NewName ($renameBase + '-Path')",
        "Move-Item function:Redirect $moveTarget",
        "Rename-Item -LiteralPath $renameSource -NewName 'Join-Path'",
        # r4 adversary pass 1 (A2): an operand built ONLY from literals is decided by the source text, not at run time, so one the scan
        # cannot reduce fails closed on its own, whether or not a route or an evaluated name sits beside it.
        "Rename-Item -LiteralPath ('function:Redirect') -NewName (('Join', 'Path') -join '-')",
        "Move-Item function:Redirect ('{0}-Path' -f 'Join')",
        "Rename-Item -LiteralPath (('function', 'Redirect') -join ':') -NewName (('Join', 'Path') -join '-')",
        # r6 (Dot B9): a cast over a comma list is ONE string at run time (joined by $OFS), so it is never split into the elements the
        # scan would read as harmless constants; the whole operand is built only from literals the scan cannot reduce.
        "Copy-Item -LiteralPath function:Redirect -Destination ([string]@('fun','ction:Join-Path'))",
        "Rename-Item -LiteralPath function:Redirect -NewName ([string]@('Join','-Path'))",
        "Move-Item function:Redirect ([string]('a','b'))",
        # Elements that no scope-qualified-name reading flags on its own ('function:Join', '-Path'): only the whole cast operand is a finding.
        "Copy-Item -LiteralPath function:Redirect -Destination ([string]@('function:Join','-Path'))",
        # r7 (adversary r6 A1): the same cast over a comma list on an item command (Set-Item, New-Item) that writes the drive directly.
        "Set-Item -LiteralPath ([string]@('function:Join','-Path')) -Value { 'C:\\fixture\\Dirty.psm1' }",
        "New-Item -Path ([string]('alias:Split','-Path')) -Value Redirect",
        # r7 (adversary r6 A4): $true / $false are 'True' / 'False' after a string and are an error before one, never ''.
        "Rename-Item -LiteralPath function:Redirect -NewName ($true + 'Join-Path')",
        "Rename-Item -LiteralPath function:Redirect -NewName (1 + 2)",
        # r7 (adversary r6 A3): a comma list inside a comma list is one joined string (OFS), so it is never split into harmless fragments.
        "Set-Item -Path (('function:Jo','in-Path'),'function:Harmless') -Value { 'C:\\fixture\\Dirty.psm1' }",
    )
    # Not shadows: the alias is for a command the scan does not evaluate, and a read of an unrelated function.
    ALIAS_CONTROLS = (
        "Set-Alias -Name:Get-Item -Value:Get-Date",
        "New-Alias -Name:'Get-Thing' -Value Get-Date",
        "Get-Item -Path function:Get-Date",
        "Rename-Item -LiteralPath function:Redirect -NewName Other-Name",
        "Rename-Item -LiteralPath function:Redirect -NewName ('Other-Name')",
        "Move-Item -LiteralPath $someFile -Destination $someFolder -Force",
        "Move-Item -LiteralPath 'C:\\a.txt', 'C:\\b.txt' -Destination 'C:\\dir' -Force:$true",
        # r6 (Dot B9): a plain comma list (no cast) is still split into its elements and is not over-refused.
        "Copy-Item function:Redirect 'a','b'",
        # r7 (adversary r6 A4): $false / $true read as 'False' / 'True', so these name 'Join-PathFalse' / 'Join-PathTrue', not Join-Path.
        "Rename-Item -LiteralPath function:Redirect -NewName ('Join-Path' + $false)",
        "Copy-Item function:Redirect ('function:Join-Pat' + $true + 'h')",
        # r7: a literal hashtable value on an item command is not a name.
        "Set-Item -Path variable:harmless -Value @{ a = 1 }",
    )
    ALIAS_PREAMBLE = ("function Redirect { param($Path, $ChildPath) 'C:\\fixture\\Dirty.psm1' }\n"
                      + "".join(f"{line}\n" for line in ALIAS_SHADOWS + ALIAS_COMPUTED + PROVIDER_ROUTES + RENAME_COMPUTED + ALIAS_CONTROLS))
    # The template is held to the same rule; only the colon-form lines (the ones the old scan missed) go in it.
    # Advisory A4 (same class): a mutator's positional source path after -vb must not be read as that switch's value.
    ALIAS_TEMPLATE = ("Set-Alias -Name:Join-Path -Value:Redirect\nNew-Alias -N:'New-Object' -Value:Redirect\nSet-Alias -Name:$templateName -Value:Redirect\n"
                      "Set-Alias -Name:Get-Item -Value:Get-Date\nExpand-Archive -DestinationPath $Work -vb 'C:\\archive.zip'\n"
                      "Rename-Item -LiteralPath function:Redirect -NewName ('Join-Path')\nMove-Item function:Redirect $moveTarget\n")

    @classmethod
    def _scan(cls, label: str, modules: list, on_list: list, calls: list, *, extra_files=(), template_extra: str = "", raw_modules=None,
              generator_preamble: str = "") -> dict:
        directory = cls.tmp / label
        directory.mkdir()
        for file_name, text in (raw_modules or {}).items():
            (directory / file_name).write_text(text, encoding="utf-8")
        # extra_files are written but NOT given to the scan (-ModulePath): the files the job would read and the scan does not.
        for file_name, functions in list(modules) + list(extra_files):
            target = directory / file_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(f"function {n} {{\n{b}\n}}\n\n" for n, b in functions), encoding="utf-8")
        generator = directory / "fixture-gen.ps1"
        template = cls.TEMPLATE.replace("__EMBEDDED_FUNCTIONS__", template_extra + "__EMBEDDED_FUNCTIONS__")
        generator.write_text(generator_preamble + "".join(f"$e = {call}\n" for call in calls) + "$template = @'\n" + template + "'@\n", encoding="utf-8")
        return run_scan(generators=[generator], modules=[directory / m for m in on_list], splice_sources=[generator])

    @staticmethod
    def of(result: dict, function: str) -> list[dict]:
        return [f for f in result["splicedViolations"] if f["function"] == function]

    @staticmethod
    def call_findings(result: dict, needle: str) -> list[dict]:
        """Findings about a call as a whole (no function name), picked by the call text they carry."""
        return [f for f in result["splicedViolations"] if f["rule"] == "splice" and f["function"] == "" and needle in f["text"]]

    @staticmethod
    def modules_of(result: dict, name: str) -> list[str]:
        return sorted(s["module"].rsplit("/", 1)[-1] for s in result["spliced"] if s["name"] == name)

    @staticmethod
    def unlisted(findings: list[dict]) -> list[tuple[str, str]]:
        """What the real policy (in_scope + the reviewed allowlist) leaves of body findings, as (rule, token)."""
        return [(f["rule"], f["token"]) for f in apply_allowlist(in_scope([x for x in findings if x["rule"] != "splice"]))[0]]

    # ---- L1: property and index assignments in a spliced body ----

    def test_l1_a_property_set_on_an_object_a_command_returned_stays_a_finding(self) -> None:
        found = self.of(self.l1, "L1Foreign")
        self.assertEqual(self.unlisted(found), [("R5", "$item.Attributes")], describe(found))
        self.assertEqual([f["origin"] for f in found if f["rule"] == "R5"], ["other"])

    def test_l1_a_property_set_on_a_parameter_stays_a_finding(self) -> None:
        self.assertEqual(self.unlisted(self.of(self.l1, "L1Param")), [("R5", "$Target.IsReadOnly")])

    def test_l1_a_variable_that_is_sometimes_a_foreign_object_stays_a_finding(self) -> None:
        found = self.of(self.l1, "L1Mixed")
        self.assertEqual(self.unlisted(found), [("R5", "$r.Attributes")], describe(found))

    def test_l1_a_property_set_on_a_constructed_file_object_stays_a_finding(self) -> None:
        found = self.of(self.l1, "L1FileInfo")
        self.assertIn(("R5", "$fi.IsReadOnly"), self.unlisted(found), describe(found))

    def test_l1_a_nested_property_chain_stays_a_finding(self) -> None:
        self.assertEqual(self.unlisted(self.of(self.l1, "L1Nested")), [("R5", "$r.inner.Attributes")])

    def test_l1_a_property_set_on_a_root_variable_stays_a_finding(self) -> None:
        self.assertEqual(self.unlisted(self.of(self.l1, "L1Root")), [("R5", "$Pub.Attributes")])

    def test_l1_property_sets_on_freshly_built_in_memory_objects_are_not_findings(self) -> None:
        found = self.of(self.l1, "L1Fresh")
        r5 = [f for f in found if f["rule"] == "R5"]
        self.assertEqual(sorted(f["origin"] for f in r5),
                         ["constructed:System.Diagnostics.ProcessStartInfo", "literal", "literal"], describe(r5))
        self.assertEqual([k for k in self.unlisted(found) if k[0] == "R5"], [])

    def test_l1_index_assignments_to_a_root_variable_are_findings_and_to_a_local_are_not(self) -> None:
        # Pinned, not new behaviour: the index half of R5 already worked; this keeps it from regressing.
        self.assertEqual([(f["rule"], f["token"]) for f in self.of(self.l1, "L1IndexRoot")], [("R5", "$Pub['k']")])
        self.assertEqual([f for f in self.of(self.l1, "L1IndexLocal") if f["rule"] == "R5"], [])

    def test_l1_a_variable_bound_by_foreach_out_variable_a_multi_target_assignment_ref_or_a_parameter_is_not_fresh(self) -> None:
        for name in ("L1Foreach", "L1OutVariable", "L1MultiTarget", "L1Ref", "L1ParamAlso", "L1DataStatement", "L1TrapConstruct"):
            found = self.of(self.l1, name)
            self.assertIn(("R5", "$r.Attributes"), self.unlisted(found), f"{name}\n" + describe(found))

    def test_l1_automatic_variables_scope_qualified_binders_conversions_and_unordered_assignments_are_not_fresh(self) -> None:
        for name in ("L1AutoUnderscore", "L1AutoPSItem", "L1ScopedForeach", "L1ScopedPipeVar", "L1NestedAssign", "L1LateAssign",
                     "L1ConditionalAssign", "L1SetterInNestedFunction"):
            found = self.of(self.l1, name)
            self.assertTrue([k for k in self.unlisted(found) if k[0] == "R5"], f"{name} was judged fresh\n" + describe(found))
        for name in ("L1CastRhs", "L1CastLhs"):
            found = self.of(self.l1, name)
            self.assertIn(("R5", "$r.MaximumKilobytes"), self.unlisted(found), f"{name}\n" + describe(found))

    def test_r4_a3_a_typed_parameter_named_with_a_scope_qualifier_is_the_same_variable_so_the_receiver_is_not_fresh(self) -> None:
        # Adversary r3 A3 / Dot B4: `param([EventLog]$local:r)` binds $r; the hashtable assigned later is converted to an EventLog.
        for name in ("L1TypedLocal", "L1TypedScript", "L1TypedPrivate", "L1TypedGlobal"):
            found = self.of(self.l1, name)
            self.assertIn(("R5", "$r.MaximumKilobytes"), self.unlisted(found), f"{name} was judged a literal\n" + describe(found))
            self.assertIn("unbound", [o for f in found if f["rule"] == "R5" for o in f["origin"].split(";")], f"{name}\n" + describe(found))
        found = self.of(self.l1, "L1TypedOther")
        self.assertEqual([f["origin"] for f in found if f["rule"] == "R5"], ["literal"], describe(found))

    def test_l1_an_assignment_that_comes_first_in_an_enclosing_block_still_makes_a_receiver_fresh(self) -> None:
        found = self.of(self.l1, "L1FreshNested")
        self.assertEqual([f["origin"] for f in found if f["rule"] == "R5"], ["literal"], describe(found))
        self.assertEqual([k for k in self.unlisted(found) if k[0] == "R5"], [])

    def test_l1_a_scoped_assignment_a_trap_a_deferred_block_or_a_shadowed_constructor_does_not_make_a_receiver_fresh(self) -> None:
        for name in ("L1ScopedAssign", "L1TrapSetter", "L1DeferredBlock"):
            found = self.of(self.l1, name)
            self.assertIn(("R5", "$r.Attributes"), self.unlisted(found), f"{name} was judged fresh\n" + describe(found))
        found = self.of(self.l1, "L1ShadowNew")
        self.assertTrue([f for f in found if "shadow" in f["reason"]], "a local New-Object was trusted\n" + describe(found))
        self.assertIn(("R1", "function New-Object"), self.unlisted(found), describe(found))

    def test_l1_a_script_block_handed_to_a_command_still_runs_in_line_so_a_fresh_receiver_stays_fresh(self) -> None:
        found = self.of(self.l1, "L1InlineBlock")
        self.assertEqual([f["origin"] for f in found if f["rule"] == "R5"], ["literal"], describe(found))
        self.assertEqual([k for k in self.unlisted(found) if k[0] == "R5"], [])

    def test_l1_a_template_function_named_like_join_path_is_a_finding(self) -> None:
        shadows = [v for v in self.l1["violations"] if "shadow" in v["reason"] and "Join-Path" in v["text"]]
        self.assertEqual(len(shadows), 1, json.dumps(self.l1["violations"], indent=2))

    def test_l2_a_positional_module_path_after_a_common_parameter_alias_is_a_finding(self) -> None:
        for name, flag in (("PosVb", "-vb"), ("PosDb", "-db")):
            self.assertEqual(len(self.call_findings(self.shadow, f"'{name}' {flag}")), 1, describe(self.shadow["splicedViolations"]))
            self.assertEqual(self.modules_of(self.shadow, name), [], f"{flag}: the clean default module was scanned in place of the path")

    def test_l2_a_splice_source_that_defines_join_path_split_path_or_an_alias_of_a_command_the_scan_evaluates_is_a_finding(self) -> None:
        shadows = [f for f in self.shadow["splicedViolations"] if f["rule"] == "splice" and "shadow" in f["reason"]]
        texts = " | ".join(f["text"] for f in shadows)
        for needle in ("Join-Path", "Split-Path", "New-Object"):
            self.assertIn(needle, texts, f"no shadow finding names {needle}\n" + describe(self.shadow["splicedViolations"]))
        self.assertNotIn("Get-Item", texts, "Get-Item is not a command the scan evaluates")

    def alias_findings(self) -> list[dict]:
        return [f for f in self.alias["splicedViolations"] if f["rule"] == "splice" and "shadow" in f["reason"]]

    def alias_texts(self) -> list[str]:
        return [f["text"] for f in self.alias_findings()]

    def test_b1_a_colon_form_prefixed_quoted_scoped_or_reordered_alias_of_an_evaluated_command_is_a_finding(self) -> None:
        texts = self.alias_texts()
        for line in self.ALIAS_SHADOWS:
            self.assertIn(line, texts, f"no shadow finding for `{line}`\n" + describe(self.alias["splicedViolations"]))

    def test_b1_the_dot_counterexample_end_to_end_is_a_finding_while_the_call_still_resolves_to_the_clean_module(self) -> None:
        # Runtime would read Dirty.psm1 (the alias redirects Join-Path); the scan evaluates Join-Path by its stock behaviour
        # and reads Clean.psm1. The shadow finding is what stops that divergence from passing.
        self.assertIn("Set-Alias -Name:Join-Path -Value:Redirect", self.alias_texts(), describe(self.alias["splicedViolations"]))
        self.assertEqual(self.modules_of(self.alias, "DotF"), ["Clean.psm1"])

    def test_b1_an_alias_whose_name_is_not_a_string_constant_is_a_finding_of_its_own_kind(self) -> None:
        by_text = {f["text"]: f for f in self.alias_findings()}
        for line in self.ALIAS_COMPUTED:
            self.assertIn(line, by_text, f"no finding for the computed alias name in `{line}`\n" + describe(self.alias["splicedViolations"]))
            self.assertIn("not a string constant", by_text[line]["reason"], line)

    def test_b1_a_function_or_alias_drive_route_to_an_evaluated_command_is_a_finding(self) -> None:
        texts = self.alias_texts()
        for line in self.PROVIDER_ROUTES:
            self.assertIn(line, texts, f"no shadow finding for `{line}`\n" + describe(self.alias["splicedViolations"]))

    def test_b1_an_alias_of_a_command_the_scan_does_not_evaluate_and_a_read_of_an_unrelated_function_are_not_findings(self) -> None:
        texts = " | ".join(self.alias_texts())
        for line in self.ALIAS_CONTROLS:
            self.assertNotIn(line, texts, f"`{line}` is not a shadow\n" + describe(self.alias["splicedViolations"]))

    def test_r4_a1_a_parenthesized_rename_operand_still_ends_in_a_shadow_finding_while_the_call_resolves_to_the_clean_module(self) -> None:
        # Adversary r3 A1 / Dot B2, end to end: runtime Join-Path returns Dirty.psm1 after the rename; the scan binds Clean.psm1.
        self.assertIn("Rename-Item -LiteralPath function:Redirect -NewName ('Join-Path')", self.alias_texts(), describe(self.alias["splicedViolations"]))
        self.assertEqual(self.modules_of(self.alias, "RenF"), ["Clean.psm1"])

    def test_r4_a1_a_rename_or_move_with_an_operand_the_scan_cannot_reduce_to_a_constant_fails_closed(self) -> None:
        by_text = {f["text"]: f for f in self.alias_findings()}
        for line in self.RENAME_COMPUTED:
            with self.subTest(line=line):
                self.assertIn(line, by_text, f"no finding for the computed rename operand in `{line}`\n" + describe(self.alias["splicedViolations"]))
                self.assertIn("not a string constant", by_text[line]["reason"], line)

    def test_r4_a1_the_template_is_held_to_the_same_rename_rule(self) -> None:
        shadows = {v["text"] for v in self.alias["violations"] if "shadow" in v["reason"] or "not a string constant" in v["reason"]}
        for line in ("Rename-Item -LiteralPath function:Redirect -NewName ('Join-Path')", "Move-Item function:Redirect $moveTarget"):
            self.assertIn(line, shadows, f"template line `{line}` was not flagged\n" + json.dumps(self.alias["violations"], indent=2))

    def test_b1_the_template_is_held_to_the_same_rule(self) -> None:
        shadows = {v["text"]: v for v in self.alias["violations"] if "shadow" in v["reason"]}
        for line in ("Set-Alias -Name:Join-Path -Value:Redirect", "New-Alias -N:'New-Object' -Value:Redirect", "Set-Alias -Name:$templateName -Value:Redirect"):
            self.assertIn(line, shadows, f"template line `{line}` was not flagged\n" + json.dumps(self.alias["violations"], indent=2))
        self.assertNotIn("Set-Alias -Name:Get-Item -Value:Get-Date", shadows)

    def test_b1_a_module_qualified_extractor_and_a_drive_or_root_relative_literal_module_path_are_findings(self) -> None:
        for needle, reason in (("Dirty\\Get-AttrCudaEmbeddedFunctionSource", "module-qualified"),
                               ("'DriveF' -ModulePath 'C:Clean.psm1'", "relative"),
                               ("'RootF' -ModulePath '\\Clean.psm1'", "relative")):
            found = self.call_findings(self.alias, needle)
            self.assertEqual(len(found), 1, f"{needle}\n" + describe(self.alias["splicedViolations"]))
            self.assertIn(reason, found[0]["reason"], needle)
        for name in ("QualF", "DriveF", "RootF"):
            self.assertEqual(self.modules_of(self.alias, name), [], f"{name}: a module was scanned in place of the one the call reads")

    def test_b1_a_mutators_positional_path_after_the_common_switch_alias_vb_is_a_finding(self) -> None:
        texts = [v["text"] for v in self.alias["violations"]]
        self.assertTrue([t for t in texts if t.startswith("Expand-Archive") and "-vb" in t], json.dumps(self.alias["violations"], indent=2))

    def test_l1_template_parameters_named_by_alias_or_abbreviation_are_still_judged(self) -> None:
        # Advisory A8, same class as A3: -RSO / -RSE are aliases of Start-Process -RedirectStandardOutput / -Error, -N: abbreviates -Name.
        texts = " | ".join(v["text"] for v in self.l1["violations"])
        for needle in ("-RSO", "-RSE", "-N:"):
            self.assertIn(needle, texts, f"the template line using {needle} was not flagged\n" + json.dumps(self.l1["violations"], indent=2))

    def test_l1_every_reviewed_property_set_row_names_a_plain_local_receiver(self) -> None:
        for (function, rule, token) in ALLOWLIST:
            if rule == "R5":
                self.assertRegex(token, LOCAL_PROPERTY_SET, f"{function}: an R5 row must name a plain local receiver")

    # ---- L2: a splice call site is bound to the module its extractor call reads ----

    def test_l2_a_literal_module_path_binds_the_call_to_that_module_not_to_a_same_named_helper_elsewhere(self) -> None:
        self.assertEqual(self.modules_of(self.l2, "LitClean"), ["Clean.psm1"])
        self.assertEqual(self.of(self.l2, "LitClean"), [], describe(self.of(self.l2, "LitClean")))

    def test_l2_a_literal_module_path_to_the_dirty_module_reports_its_body_only(self) -> None:
        self.assertEqual(self.modules_of(self.l2, "LitDirty"), ["Dirty.psm1"])
        self.assertEqual([(f["rule"], f["token"]) for f in self.of(self.l2, "LitDirty")], [("R2", "New-Item")])

    def test_l2_a_module_path_off_the_scanners_module_list_is_a_finding_even_if_another_module_defines_the_name(self) -> None:
        # OffList is defined (clean) in Clean.psm1, which IS on the list; the call reads Off.psm1, which is not.
        self.assertEqual(len(self.call_findings(self.l2, "'OffList'")), 1, describe(self.l2["splicedViolations"]))
        self.assertEqual(self.modules_of(self.l2, "OffList"), [], "the scan must not read Clean.psm1's body in place of Off.psm1's")

    def test_l2_a_helper_defined_only_off_the_module_list_is_a_finding_not_a_silent_pass(self) -> None:
        self.assertEqual([(f["rule"], f["function"]) for f in self.of(self.l2, "OffOnly")], [("splice", "OffOnly")])
        self.assertEqual(len(self.call_findings(self.l2, "Off.psm1') -Name @('OffOnly')")), 1)
        self.assertEqual(self.modules_of(self.l2, "OffOnly"), [])

    def test_l2_a_name_two_candidate_modules_define_and_the_call_does_not_bind_is_ambiguous(self) -> None:
        for name in ("Ambig", "AmbigDyn"):
            self.assertEqual([(f["rule"], f["function"]) for f in self.of(self.l2, name)], [("splice", name)], describe(self.of(self.l2, name)))
            self.assertEqual(self.modules_of(self.l2, name), [])

    def test_l2_a_dynamic_module_path_resolves_a_name_only_one_module_defines(self) -> None:
        self.assertEqual(self.modules_of(self.l2, "OnlyHere"), ["Only.psm1"])
        self.assertEqual(self.of(self.l2, "OnlyHere"), [])

    def test_l2_a_positional_module_path_or_a_splat_is_a_finding_because_the_binding_is_unknown(self) -> None:
        self.assertEqual(len(self.call_findings(self.l2, "@('Spl') (Join-Path")), 1, describe(self.l2["splicedViolations"]))
        self.assertEqual(len(self.call_findings(self.l2, "-Name @('Spl') @extra")), 1)
        self.assertEqual(self.modules_of(self.l2, "Spl"), [])

    def test_l2_a_same_named_module_file_outside_the_scanned_set_is_not_matched_by_its_base_name(self) -> None:
        self.assertEqual(self.modules_of(self.l2, "Collide"), [], "outside\\Clean.psm1 was matched to the listed l2\\Clean.psm1")
        self.assertEqual(len(self.call_findings(self.l2, "'Collide'")), 1, describe(self.l2["splicedViolations"]))
        self.assertEqual(self.modules_of(self.l2, "CollideSub"), [])
        self.assertEqual(len(self.call_findings(self.l2, "'CollideSub'")), 1)

    def test_l2_a_module_path_expression_that_merely_contains_a_path_is_not_a_literal_path(self) -> None:
        self.assertEqual([(f["rule"], f["function"]) for f in self.of(self.l2, "IdxAmbig")], [("splice", "IdxAmbig")], describe(self.of(self.l2, "IdxAmbig")))
        self.assertEqual(self.modules_of(self.l2, "IdxAmbig"), [])

    def test_l2_a_name_list_that_is_an_expression_is_a_finding_not_a_scan_of_different_names(self) -> None:
        self.assertEqual(len(self.call_findings(self.l2, "('Nm' + 'Nm')")), 1, describe(self.l2["splicedViolations"]))
        self.assertEqual(self.modules_of(self.l2, "Nm"), [])
        self.assertEqual(self.modules_of(self.l2, "NmNm"), [])

    def test_r7_a5_a_redirected_module_path_is_not_the_path_it_names(self) -> None:
        for name in ("RedirA", "RedirB"):
            self.assertEqual([(f["rule"], f["function"]) for f in self.of(self.l2, name)], [("splice", name)], describe(self.of(self.l2, name)))
            self.assertEqual(self.modules_of(self.l2, name), [], name)

    def test_r7_a4_a_comma_list_inside_the_name_list_is_not_a_literal_list_of_names(self) -> None:
        found = self.call_findings(self.l2, "(('Nes','ted'),'Safe')")
        self.assertEqual(len(found), 1, describe(self.l2["splicedViolations"]))
        self.assertIn("not a literal list of function names", found[0]["reason"])

    def test_l2_an_abbreviated_parameter_name_binds_like_the_full_one(self) -> None:
        # PowerShell binds -M to -ModulePath and -N to -Name, by prefix against the extractor's parameter set AT RUN TIME. r9 (hub
        # ruling, rule 3): the scan does not prove that set, so an abbreviation is an unproven binding (fails closed), never a scan of
        # the module the prefix merely looks like. Before r9, -M was read as -ModulePath and Dirty.psm1's body was scanned.
        for name, needle in (("AbbrevM", "-M (Join-Path"), ("NameAbbrev", "-N @('NameAbbrev')")):
            found = self.call_findings(self.l2_default, needle)
            self.assertEqual([f["token"] for f in found], ["unproven-binding"], describe(self.l2_default["splicedViolations"]))
            self.assertEqual(self.modules_of(self.l2_default, name), [], f"{name}: a module was scanned for an unproven binding")

    def test_l2_an_empty_blank_or_null_module_path_is_the_default_module(self) -> None:
        found = self.of(self.l2_default, "EmptyOnly")
        self.assertEqual([(f["rule"], f["function"]) for f in found], [("splice", "EmptyOnly")] * 4, describe(found))  # '', ' ', $null, $NULL
        self.assertEqual(self.modules_of(self.l2_default, "EmptyOnly"), [])

    def test_l2_variable_and_command_names_in_a_module_path_are_case_insensitive(self) -> None:
        self.assertEqual(self.modules_of(self.l2_default, "CaseRoot"), ["Dirty.psm1"], json.dumps(self.l2_default["spliced"], indent=2))

    def test_l2_a_comment_that_mentions_the_extractor_does_not_make_a_second_default_module(self) -> None:
        # Notes.psm1 holds the extractor's definition inside a block comment only; DefHelper still resolves to Extractor.psm1.
        self.assertEqual(self.modules_of(self.l2_default, "DefHelper"), ["Extractor.psm1"])
        self.assertEqual([f for f in self.l2_default["splicedViolations"] if f["rule"] == "splice" and "default module is ambiguous" in f["reason"]], [])

    def test_l2_without_a_module_path_the_call_reads_the_extractors_own_module(self) -> None:
        self.assertEqual(self.modules_of(self.l2_default, "DefHelper"), ["Extractor.psm1"])
        self.assertEqual(self.of(self.l2_default, "DefHelper"), [], describe(self.of(self.l2_default, "DefHelper")))

    def test_l2_without_a_module_path_a_name_the_extractors_module_lacks_is_a_finding(self) -> None:
        # At run time the extractor throws for a name its own module does not define; the scan must not find it elsewhere.
        self.assertEqual([(f["rule"], f["function"]) for f in self.of(self.l2_default, "DirtyOnly")], [("splice", "DirtyOnly")])
        self.assertEqual(self.modules_of(self.l2_default, "DirtyOnly"), [])


@requires_pwsh
class SplicedBodyScanPsScriptRootTests(unittest.TestCase):
    """r4 (adversary r3 A2 = Dot B3): `Join-Path $PSScriptRoot '<child>'` is read as the generator's own directory, which holds only
    while `$PSScriptRoot` is the automatic value. A generator that rebinds it (any assignment, scope qualifier, parameter, loop
    variable, [ref], -OutVariable, Set-Variable / New-Variable, the variable: drive) makes runtime read another directory's module.

    One scan covers every variant: each lives in its own generator file with its own function name, and the scan takes them all as
    splice sources.
    """

    CALL = "Get-AttrCudaEmbeddedFunctionSource -Name '{name}' -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1')\n"
    REBINDS = {
        "PsrAssign": "$PSScriptRoot = 'C:\\dirty'\n",
        "PsrScoped": "$script:PSScriptRoot = 'C:\\dirty'\n",
        "PsrGlobal": "$global:PSScriptRoot = 'C:\\dirty'\n",
        "PsrTyped": "[string]$PSScriptRoot = 'C:\\dirty'\n",
        "PsrCompound": "$PSScriptRoot += 'x'\n",
        "PsrMulti": "$a, $PSScriptRoot = 'C:\\x', 'C:\\dirty'\n",
        "PsrForeach": "foreach ($PSScriptRoot in @('C:\\dirty')) { }\n",
        "PsrRef": "$null = [int]::TryParse('1', [ref]$PSScriptRoot)\n",
        "PsrOutVar": "Get-Date -OutVariable PSScriptRoot | Out-Null\n",
        "PsrSetVariable": "Set-Variable -Name PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrNewVariable": "New-Variable PSScriptRoot 'C:\\dirty' -Force\n",
        "PsrSv": "sv PSScriptRoot 'C:\\dirty' -Scope Script\n",
        "PsrDrive": "Set-Item -Path variable:PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrBraced": "${variable:PSScriptRoot} = 'C:\\dirty'\n",
        # r4 adversary pass 1 (A1): the variable: drive takes a scope qualifier of its own after the drive name.
        "PsrDriveLocal": "Set-Item -LiteralPath variable:local:PSScriptRoot 'C:\\dirty'\n",
        "PsrDriveScript": "Set-Item -Path 'variable:\\script:PSScriptRoot' -Value 'C:\\dirty'\n",
        "PsrBracedGlobal": "${variable:global:PSScriptRoot} = 'C:\\dirty'\n",
        "PsrProviderQualified": "Set-Item -LiteralPath Microsoft.PowerShell.Core\\Variable::PSScriptRoot 'C:\\dirty'\n",
        "PsrParam": "function Rebind { param($PSScriptRoot) }\n",
        "PsrComputed": "Set-Variable -Name $someName -Value 'C:\\dirty'\n",
        # r5 (Dot B7 = r4 adversary A2): './', '.\' and doubled separators, with or without a scope qualifier, name the same variable.
        "PsrDriveDotSlash": "Set-Item -LiteralPath variable:./PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrDriveDotBack": "Set-Item -LiteralPath variable:.\\PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrDriveDoubledLocal": "Set-Item -LiteralPath variable:\\\\local:PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrDriveDotLocal": "Set-Item -LiteralPath variable:local:./PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrBracedDot": "${variable:./PSScriptRoot} = 'C:\\dirty'\n",
        # A route built from constants has no string node of its own.
        "PsrDriveReduced": "Set-Item -LiteralPath ('vari'+'able:PSScriptRoot') -Value 'C:\\dirty'\n",
        # r5 (Dot B8 = r4 adversary A3): a data section assigns its result to the named variable.
        "PsrDataStatement": "data PSScriptRoot { 'C:\\dirty' }\n",
        "PsrDriveParent": "Set-Item -LiteralPath variable:../PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrDriveInnerParent": "Set-Item -LiteralPath variable:x/../PSScriptRoot -Value 'C:\\dirty'\n",
        "PsrBracedParent": "${variable:../PSScriptRoot} = 'C:\\dirty'\n",
        "PsrDataStatementCommand":"data PSScriptRoot -SupportedCommand Get-Item { Get-Item -LiteralPath 'C:\\dirty' }\n",
        # r7 (Dot B10 = r6 adversary A2): an assignment target wrapped in more than one type conversion, or in a validation attribute, is
        # still an assignment to the variable; every wrapper is stripped, not just the outermost.
        "PsrDoubleTyped": "[object][string]$PSScriptRoot = 'C:\\dirty'\n",
        "PsrDoubleTypedScoped": "[object][string]$script:PSScriptRoot = 'C:\\dirty'\n",
        "PsrDoubleTypedGlobal": "[object][string]$global:PSScriptRoot = 'C:\\dirty'\n",
        "PsrTripleTyped": "[object][psobject][string]$PSScriptRoot = 'C:\\dirty'\n",
        "PsrAttributed": "[ValidateNotNull()][string]$PSScriptRoot = 'C:\\dirty'\n",
        "PsrAttributedTwice": "[ValidateNotNull()][ValidateNotNullOrEmpty()][object][string]$script:PSScriptRoot = 'C:\\dirty'\n",
        "PsrDoubleTypedCompound": "[object][string]$PSScriptRoot += 'x'\n",
        # r7 (adversary r6 A1): a cast over a comma list is one string at run time (joined by $OFS); on an item command it is never split.
        "PsrDriveCastList": "Set-Item -LiteralPath ([string]@('variable:PSScript','Root')) -Value 'C:\\dirty'\n",
        "PsrDriveCastListNew": "New-Item -Path ([string]('variable:PSScript','Root')) -Value 'C:\\dirty' -Force\n",
        # r7 (adversary r6 A3): a comma list INSIDE a comma list binds as one string (joined by $OFS), so it is not split into fragments.
        "PsrDriveNestedList": "$OFS = ''\nSet-Item -Path (('variable:PSScript','Root'),'variable:other') -Value 'C:\\dirty'\n",
        # r7 (adversary r6 A1): `set` is an alias of Set-Variable.
        "PsrSetAlias": "set PSScriptRoot 'C:\\dirty'\n",
        "PsrSetAliasNamed": "set -Name PSScriptRoot -Value 'C:\\dirty'\n",
        # r7 (adversary r6 A2): a cmdlet's own variable parameter binds the variable like -OutVariable does.
        "PsrTeeVariable": "'C:\\dirty' | Tee-Object -Variable PSScriptRoot | Out-Null\n",
        "PsrTeeVar": "'C:\\dirty' | Tee-Object -Var PSScriptRoot | Out-Null\n",
        "PsrSessionVariable": "Invoke-WebRequest -Uri 'http://example.invalid' -SessionVariable PSScriptRoot | Out-Null\n",
    }
    CONTROLS = {
        # No rebinding: the call still binds to Clean.psm1 by full path.
        "PsrPlain": "$other = 'C:\\x'\nSet-Variable -Name Unrelated -Value 1\nGet-Date -OutVariable unrelated | Out-Null\n",
        # r7: a wrapped target that is some OTHER variable, and a cast list on an item command that names no variable, are not rebindings.
        "PsrTeeOther": "'x' | Tee-Object -Variable keep | Out-Null\nset other 1\n",
        "PsrWrappedOther":"[object][string]$other = 'C:\\x'\n[ValidateNotNull()][string]$script:unrelated = 'y'\nSet-Item -Path variable:harmless -Value 5\n",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="attr3-psroot-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.dir = _long_path(Path(cls._tmp.name))
        names = list(cls.REBINDS) + list(cls.CONTROLS)
        (cls.dir / "Clean.psm1").write_text("".join(f"function {n} {{\n    Get-Date\n}}\n\n" for n in names), encoding="utf-8")
        sources = []
        for name, line in {**cls.REBINDS, **cls.CONTROLS}.items():
            generator = cls.dir / f"gen-{name}.ps1"
            generator.write_text(line + "$e = " + cls.CALL.format(name=name), encoding="utf-8")
            sources.append(generator)
        # r5 (Dot B8): a data section also binds a destination variable and $Work, which the template proofs read as assignments.
        cls.template = cls.dir / "template-data.ps1"
        cls.template.write_text("$Work = Join-Path 'C:\\mlvtmp' $JobId\n$d = Join-Path $Work 'ok'\ndata d { 'C:\\outside' }\n"
                                "New-Item -ItemType Directory -Path $d -Force | Out-Null\n"
                                "data Work { 'C:\\outside' }\n", encoding="utf-8")
        cls.result = run_scan(templates=[cls.template], modules=[cls.dir / "Clean.psm1"], splice_sources=sources)

    def test_r5_b8_a_data_section_binding_a_destination_variable_or_work_is_a_finding(self) -> None:
        found = [(v["rule"], v["reason"]) for v in self.result["violations"] if v["source"] == str(self.template)]
        self.assertTrue([r for rule, r in found if rule == "R2" and "destination is not provably under" in r], f"{found}")
        self.assertTrue([r for rule, r in found if rule == "R5" and "data statement" in r], f"{found}")

    def findings_for(self, name: str) -> list[dict]:
        return [f for f in self.result["splicedViolations"] if f["rule"] == "splice" and f"'{name}'" in f["text"]]

    def test_r4_a2_a_rebound_psscriptroot_makes_the_module_binding_a_finding_not_a_scan_of_the_generators_directory(self) -> None:
        for name in self.REBINDS:
            found = self.findings_for(name)
            self.assertEqual(len(found), 1, f"{name}\n" + describe(self.result["splicedViolations"]))
            self.assertIn("PSScriptRoot", found[0]["reason"], name)
            self.assertEqual([s for s in self.result["spliced"] if s["name"] == name], [], f"{name}: Clean.psm1 was scanned in place of the module runtime reads")

    def test_r4_a2_an_unrebound_psscriptroot_still_binds_to_the_generators_directory(self) -> None:
        for name in self.CONTROLS:
            self.assertEqual(self.findings_for(name), [], describe(self.result["splicedViolations"]))
            self.assertEqual([s["module"].rsplit("/", 1)[-1] for s in self.result["spliced"] if s["name"] == name], ["Clean.psm1"])


@requires_pwsh
@unittest.skipUnless(os.name == "nt", "the ATTR-3 host jobs and their helpers are Windows-only")
class SplicedBodyScanUnprovenBindingTests(unittest.TestCase):
    """r9 (hub ruling; opus r8 B1/B2, sol r8 L2): the UNPROVEN-BINDING class rule. Eight rounds each closed one construct that changes
    WHICH module or object a name binds to at run time, and the next key found another. The rule is the class: a route the scan
    cannot evaluate statically makes the binding unproven, and the scan reports that (token 'unproven-binding') instead of reading
    the module or object it would merely assume.

    One scan covers every case: each generator case lives in its own file (a $PSDefaultParameterValues reference is per file), each
    body case is its own function. Extractor.psm1 (clean, defines the extractor) is what an unchanged reading binds; Dirty.psm1 is what
    runtime reads.
    """

    RAW_WRITE = "    New-Item -ItemType Directory -Path (Join-Path $Pub 'sneaky') -Force | Out-Null"
    PSR_CALL = "Get-AttrCudaEmbeddedFunctionSource -Name '{name}' -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1')"
    DEF_CALL = "Get-AttrCudaEmbeddedFunctionSource -Name '{name}'"
    # name -> (preamble, call). {dirty} is Dirty.psm1's full path, {dir} the fixture directory.
    CALLS = {
        # sol r8, verbatim shapes: a literal default redirects the extractor (named, positional, wildcard) or Join-Path.
        "SolNamed": ("$PSDefaultParameterValues = @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }}", DEF_CALL),
        "SolPositional": ("$PSDefaultParameterValues = @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }}", "Get-AttrCudaEmbeddedFunctionSource '{name}'"),
        "SolWildcard": ("$PSDefaultParameterValues = @{{ '*:ModulePath' = '{dirty}' }}", DEF_CALL),
        "SolJoinPath": ("$PSDefaultParameterValues = @{{ 'Join-Path:AdditionalChildPath' = '..\\Dirty.psm1' }}",
                        "Get-AttrCudaEmbeddedFunctionSource -Name '{name}' -ModulePath (Join-Path $PSScriptRoot 'Extractor.psm1')"),
        # rule 1: every form that reads or changes $PSDefaultParameterValues.
        "DpvIndex": ("$PSDefaultParameterValues['Get-AttrCudaEmbeddedFunctionSource:ModulePath'] = '{dirty}'", DEF_CALL),
        "DpvAdd": ("$PSDefaultParameterValues.Add('Get-AttrCudaEmbeddedFunctionSource:ModulePath', '{dirty}')", DEF_CALL),
        "DpvGlobal": ("$global:PSDefaultParameterValues = @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }}", DEF_CALL),
        "DpvAliasRef": ("$d = $PSDefaultParameterValues\n$d['Get-AttrCudaEmbeddedFunctionSource:ModulePath'] = '{dirty}'", DEF_CALL),
        "DpvMember": ("$script:PSDefaultParameterValues.'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}'", DEF_CALL),
        "DpvSetVar": ("Set-Variable -Name PSDefaultParameterValues -Value @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }}", DEF_CALL),
        "DpvBraced": ("${{variable:PSDefaultParameterValues}} = @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }}", DEF_CALL),
        "DpvGetVarWild": ("(Get-Variable PSDefault*).Value['Get-AttrCudaEmbeddedFunctionSource:ModulePath'] = '{dirty}'", DEF_CALL),
        "DpvPsVariable": ("$ExecutionContext.SessionState.PSVariable.Set('PSDefaultParameterValues', @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }})", DEF_CALL),
        "DpvReduced": ("Set-Variable -Name ('PSDefault' + 'ParameterValues') -Value @{{ 'Get-AttrCudaEmbeddedFunctionSource:ModulePath' = '{dirty}' }}", DEF_CALL),
        # opus r8 B2, verbatim shapes: a prefix unique for THAT command rebinds $PSScriptRoot (Tee-Object -Va, Invoke-WebRequest -Ses).
        "OpusTeeVa": ("'{dir}' | Tee-Object -Va PSScriptRoot | Out-Null", PSR_CALL),
        "OpusSes": ("Invoke-WebRequest -Uri 'http://example.invalid' -Ses PSScriptRoot", PSR_CALL),
        # rule 2 on the extractor input $PSScriptRoot: the variable object, the session-state API, a computed name after a short prefix.
        "PsrGetVar": ("(Get-Variable PSScriptRoot).Value = '{dir}\\sub'", PSR_CALL),
        "PsrPsVariable": ("$ExecutionContext.SessionState.PSVariable.Set('PSScriptRoot', '{dir}\\sub')", PSR_CALL),
        "PsrTeeVaComp": ("'{dir}\\sub' | Tee-Object -Va $someName | Out-Null", PSR_CALL),
        # rule 3: an abbreviated extractor parameter, and a splat.
        "AbbrevMod": ("", "Get-AttrCudaEmbeddedFunctionSource -Name '{name}' -Mod (Join-Path $PSScriptRoot 'Clean.psm1')"),
        "AbbrevNa": ("", "Get-AttrCudaEmbeddedFunctionSource -Na '{name}'"),
        "SplatBound": ("", "Get-AttrCudaEmbeddedFunctionSource -Name '{name}' @PSBoundParameters"),
    }
    # Not unproven: an unrelated variable parameter, the default module, an exact common-parameter alias.
    CALL_CONTROLS = {
        "CtlPlain": ("$other = @{{ a = 1 }}\n'x' | Tee-Object -Variable keep | Out-Null", PSR_CALL, "Clean.psm1"),
        "CtlDefault": ("", DEF_CALL, "Extractor.psm1"),
        "CtlAlias": ("", "Get-AttrCudaEmbeddedFunctionSource -Name '{name}' -vb -ModulePath (Join-Path $PSScriptRoot 'Clean.psm1')", "Clean.psm1"),
    }
    # opus r8 B1 and rule 2 on an R5 receiver: a binder the origin model did not follow left the receiver 'literal'.
    BODIES = {
        "B1SetVariable": "    param([string]$Target)\n    $r = @{}\n    Set-Variable -Name r -Value (Get-Item -LiteralPath $Target)\n    $r.Attributes = 'Hidden'",
        "B1NewItemVar": "    param([string]$Target)\n    $r = @{}\n    New-Item -Path variable:r -Value (Get-Item $Target) -Force\n    $r.Attributes = 1",
        "B1Sv": "    param([string]$Target)\n    $r = @{}\n    sv r (Get-Item -LiteralPath $Target)\n    $r.Attributes = 'Hidden'",
        "B1Set": "    param([string]$Target)\n    $r = @{}\n    set r (Get-Item -LiteralPath $Target)\n    $r.Attributes = 'Hidden'",
        "B1NewVariable": "    param([string]$Target)\n    $r = @{}\n    New-Variable -Name r -Value (Get-Item -LiteralPath $Target) -Force\n    $r.Attributes = 'Hidden'",
        "B1PsVarSet": "    param([string]$Target)\n    $r = @{}\n    $ExecutionContext.SessionState.PSVariable.Set('r', (Get-Item $Target))\n    $r.Attributes = 1",
        "B1GetVarValue": "    param([string]$Target)\n    $r = @{}\n    (Get-Variable r).Value = Get-Item -LiteralPath $Target\n    $r.Attributes = 1",
        "B1SetItemVar": "    param([string]$Target)\n    $r = @{}\n    Set-Item -Path variable:r -Value (Get-Item $Target)\n    $r.Attributes = 1",
        "B1Computed": "    param([string]$Target, [string]$n)\n    $r = @{}\n    Set-Variable -Name $n -Value (Get-Item $Target)\n    $r.Attributes = 1",
        "B1RemoveVar": "    $r = @{}\n    Remove-Variable -Name r\n    $r.Attributes = 1",
        "B1TeeVa": "    param([string]$Target)\n    $r = @{}\n    Get-Item $Target | Tee-Object -Va r | Out-Null\n    $r.Attributes = 1",
    }
    BODY_CONTROLS = {
        "B1CtlOther": "    $r = @{}\n    Set-Variable -Name other -Value 1\n    $r.a = 1",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="attr3-unproven-")
        cls.addClassCleanup(cls._tmp.cleanup)
        d = _long_path(Path(cls._tmp.name))
        dirty = d / "Dirty.psm1"
        names = list(cls.CALLS) + list(cls.CALL_CONTROLS)
        clean = "".join(f"function {n} {{\n    Get-Date\n}}\n\n" for n in names)
        (d / "Extractor.psm1").write_text("function Get-AttrCudaEmbeddedFunctionSource {\n    param([string[]]$Name, [string]$ModulePath)\n}\n\n" + clean, encoding="utf-8")
        (d / "Clean.psm1").write_text(clean, encoding="utf-8")
        dirty.write_text("".join(f"function {n} {{\n{cls.RAW_WRITE}\n}}\n\n" for n in names), encoding="utf-8")
        (d / "sub").mkdir()
        (d / "sub" / "Clean.psm1").write_text("".join(f"function {n} {{\n{cls.RAW_WRITE}\n}}\n\n" for n in names), encoding="utf-8")
        sources = []
        cases = {n: (pre, call) for n, (pre, call) in cls.CALLS.items()}
        cases.update({n: (pre, call) for n, (pre, call, _) in cls.CALL_CONTROLS.items()})
        for name, (pre, call) in cases.items():
            generator = d / f"gen-{name}.ps1"
            preamble = pre.format(dirty=dirty, dir=d)
            generator.write_text((preamble + "\n" if preamble else "") + "$e = " + call.format(name=name) + "\n", encoding="utf-8")
            sources.append(generator)
        bodies = {**cls.BODIES, **cls.BODY_CONTROLS}
        (d / "Body.psm1").write_text("".join(f"function {n} {{\n{b}\n}}\n\n" for n, b in bodies.items()), encoding="utf-8")
        body_gen = d / "gen-body.ps1"
        body_gen.write_text("$e = Get-AttrCudaEmbeddedFunctionSource -Name @(" + ", ".join(f"'{n}'" for n in bodies)
                            + ") -ModulePath (Join-Path $PSScriptRoot 'Body.psm1')\n", encoding="utf-8")
        sources.append(body_gen)
        # A module the generator imports runs its top-level code in the generator's session; a template is held to rule 1 too
        # ('New-Item:Name' hands New-Item the -Name R2 forbids).
        (d / "Imported.psm1").write_text("$global:PSDefaultParameterValues['Get-AttrCudaEmbeddedFunctionSource:ModulePath'] = 'C:\\fixture\\Dirty.psm1'\n"
                                         "function ImportedHelper {\n    Get-Date\n}\n", encoding="utf-8")
        cls.template = d / "template-defaults.ps1"
        cls.template.write_text("$Work = Join-Path 'C:\\mlvtmp' $JobId\n$PSDefaultParameterValues['New-Item:Name'] = '..\\..\\escape'\n"
                                "New-Item -ItemType Directory -Path $Work -Force | Out-Null\n", encoding="utf-8")
        modules = [d / "Extractor.psm1", d / "Clean.psm1", dirty, d / "Body.psm1", d / "Imported.psm1"]
        cls.result = run_scan(templates=[cls.template], modules=modules, splice_sources=sources)

    def call_findings(self, name: str) -> list[dict]:
        return [f for f in self.result["splicedViolations"] if f["rule"] == "splice" and f["function"] == "" and f"'{name}'" in f["text"]]

    def bound(self, name: str) -> list[str]:
        return sorted(s["module"].rsplit("/", 1)[-1] for s in self.result["spliced"] if s["name"] == name)

    def test_r9_a_binding_redirect_on_an_extractor_call_is_an_unproven_binding_not_a_scan_of_the_assumed_module(self) -> None:
        for name in self.CALLS:
            with self.subTest(name=name):
                found = self.call_findings(name)
                self.assertEqual([f["token"] for f in found], ["unproven-binding"], f"{name}\n" + describe(self.result["splicedViolations"]))
                self.assertEqual(self.bound(name), [], f"{name}: a module was scanned in place of the one runtime reads")

    def test_r9_a_call_with_no_redirect_still_binds_its_module(self) -> None:
        for name, (_, _, module) in self.CALL_CONTROLS.items():
            with self.subTest(name=name):
                self.assertEqual(self.call_findings(name), [], describe(self.result["splicedViolations"]))
                self.assertEqual(self.bound(name), [module])

    def test_r9_a_receiver_bound_by_a_route_the_origin_cannot_follow_is_an_unproven_binding_and_stays_a_finding(self) -> None:
        for name in self.BODIES:
            with self.subTest(name=name):
                found = [f for f in self.result["splicedViolations"] if f["function"] == name]
                self.assertTrue([f for f in found if f["rule"] == "unproven-binding" and f["token"] == "unproven-binding"], describe(found))
                setters = [f for f in found if f["rule"] == "R5" and f["token"] == "$r.Attributes"]
                self.assertEqual(len(setters), 1, describe(found))
                self.assertIn("unproven-binding", setters[0]["origin"].split(";"), describe(setters))
                self.assertIn(("R5", "$r.Attributes"), SplicedBodyScanCoverageLimitTests.unlisted(found), f"{name}: the policy dropped the setter")

    def test_r9_a_receiver_no_route_reaches_stays_a_fresh_literal(self) -> None:
        found = [f for f in self.result["splicedViolations"] if f["function"] == "B1CtlOther"]
        self.assertEqual([f["origin"] for f in found if f["rule"] == "R5"], ["literal"], describe(found))
        self.assertEqual([f for f in found if f["rule"] == "unproven-binding"], [], describe(found))

    def test_r9_an_imported_module_or_a_template_that_refers_to_psdefaultparametervalues_is_an_unproven_binding(self) -> None:
        module = [f for f in self.result["splicedViolations"] if f["token"] == "unproven-binding" and f["source"].endswith("Imported.psm1")]
        self.assertEqual(len(module), 1, describe(self.result["splicedViolations"]))
        template = [v for v in self.result["violations"] if v["source"] == str(self.template) and v["rule"] == "unproven-binding"]
        self.assertEqual(len(template), 1, json.dumps(self.result["violations"], indent=2))


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
