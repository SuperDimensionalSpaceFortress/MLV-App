"""UM-DLLPAIR-VCVARS-PATH-OVERFLOW-1: the Ultra-Magnus CUDA DLL-pair job survives an overgrown agent PATH, fails TYPED when vcvars64
leaves INCLUDE without the Windows SDK, and can retry a spent JobId under an explicit suffix.

The incident (2026-11-15T01:09Z, job playback-attr-3-cuda-dllpair-65701d27ec5f, exit 11): the agent process PATH was 10583 characters (125 entries,
84 of them another project's per-run .dotnet\\tools dirs), over cmd.exe's 8191-character limit, so vcvars64 / winsdk.bat lost powershell.exe and
the SDK lookup; INCLUDE then lacked ucrt / um / shared and the build died with a bare `C1083 corecrt.h`. The failed result then made
Get-DllPairReuse refuse that sha forever (`exitCode-not-0 (11)`), because the JobId was derived from the sha alone.

Fixtures only: no UNC path, no agent share, no real job. The functions under test are imported from the module the emitted job embeds VERBATIM
(test_playback_attr_3_cuda_behaviour.EmbeddedFunctionContractTests proves the embedding), so a pass here is a statement about the code the job runs.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
REUSE_MODULE = ROOT / "tools" / "profiling" / "dual-venue" / "VenueStageReuse.psm1"
DLL_GENERATOR = ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1"
NIGHTLY = ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueStageNightly.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")
requires_windows_pwsh = unittest.skipIf(PWSH is None or os.name != "nt", "needs pwsh on Windows")

SHA = "a" * 40
S12 = SHA[:12]

# The INCLUDE probe 2 recorded on Ultra-Magnus after vcvars64 ran under the 10583-character PATH (MSVC dirs only, no SDK).
INCLUDE_NO_SDK = (r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.37.32822\include;"
                  r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\VS\include")
KITS = r"C:\Program Files (x86)\Windows Kits\10\include\10.0.22621.0"
INCLUDE_FULL = INCLUDE_NO_SDK + f";{KITS}\\ucrt;{KITS}\\um;{KITS}\\shared;{KITS}\\winrt;{KITS}\\cppwinrt"


@contextlib.contextmanager
def exclusive_lock(path: Path):
    """Hold `path` open with share mode 0 (FileShare.None), as another process would: its name still resolves and its attributes still read, but no
    other open succeeds. Windows only; the callers are all under requires_windows_pwsh."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share 0, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"could not lock {path}")
    try:
        yield
    finally:
        k32.CloseHandle(handle)


def overgrown_path() -> str:
    """An inherited PATH shaped like the incident's: the system dirs, then 84 per-run .dotnet\\tools dirs of another project, > 8191 chars."""
    base = [r"C:\Windows\system32", r"C:\Windows", r"C:\Windows\System32\Wbem", r"C:\Windows\System32\WindowsPowerShell\v1.0",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin", r"C:\Program Files\Git\cmd"]
    other = [f"D:\\OtherProjectGate\\{n:05d}\\cli-home\\.dotnet\\tools" for n in range(84)]
    other += [f"C:\\OtherProjectWorktrees\\wt-{n:03d}\\dotnet-home\\.dotnet\\tools" for n in range(84)]
    value = ";".join(base + other)
    assert len(value) > 8191, len(value)
    return value


class _Pwsh(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="um-dllpair-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))

    def pwsh(self, body: str, env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" + body, encoding="utf-8")
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True, timeout=timeout, env=e, stdin=subprocess.DEVNULL)

    def json_of(self, proc: subprocess.CompletedProcess) -> dict:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = [l for l in proc.stdout.splitlines() if l.startswith("JSON=")]
        self.assertTrue(line, proc.stdout + proc.stderr)
        return json.loads(line[-1][len("JSON="):])


@requires_windows_pwsh
class MinimalBuildPathTests(_Pwsh):
    """Part 1: the child environment's PATH is built from a minimal, derived set, never from the inherited value."""

    def build(self, system_root: str, tool_dirs: list[str], inherited: str | None = None) -> subprocess.CompletedProcess:
        dirs = ",".join("'" + d.replace("'", "''") + "'" for d in tool_dirs)
        return self.pwsh(
            f"Import-Module '{MODULE}' -Force\n"
            "try {\n"
            f"  $p = Get-AttrCudaMinimalBuildPath -SystemRoot '{system_root}' -ToolDirs @({dirs})\n"
            "  'JSON=' + (@{ value = $p; inheritedLength = ([string]$env:PATH).Length } | ConvertTo-Json -Compress)\n"
            "} catch { 'THREW ' + $_.Exception.Message }\n",
            env={"PATH": inherited} if inherited is not None else None,
        )

    def test_drops_an_overlong_inherited_path_and_keeps_the_required_entries(self) -> None:
        inherited = overgrown_path()
        cuda = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin"
        installer = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer"
        # pwsh itself must still start: its own dir is prepended to the fixture PATH by nothing, so the inherited value is what the child sees.
        out = self.json_of(self.build(r"C:\Windows", [cuda, installer], inherited=inherited + ";" + str(Path(PWSH).parent)))
        self.assertGreater(out["inheritedLength"], 8191)
        entries = out["value"].split(";")
        self.assertEqual(entries[:4], [r"C:\Windows\System32", r"C:\Windows", r"C:\Windows\System32\Wbem",
                                       r"C:\Windows\System32\WindowsPowerShell\v1.0"])
        self.assertIn(cuda, entries)
        self.assertIn(installer, entries)
        self.assertNotIn(".dotnet", out["value"])
        self.assertNotIn("OtherProject", out["value"])
        self.assertLess(len(out["value"]), 1024)

    def test_duplicates_and_blank_tool_dirs_collapse(self) -> None:
        out = self.json_of(self.build("C:\\Windows\\", [r"C:\Windows\System32", "", r"C:\cuda\bin\\", r"c:\CUDA\bin"]))
        self.assertEqual(out["value"].split(";"), [r"C:\Windows\System32", r"C:\Windows", r"C:\Windows\System32\Wbem",
                                                    r"C:\Windows\System32\WindowsPowerShell\v1.0", r"C:\cuda\bin"])

    def test_adversary_530a447d_a2_a_drive_root_stays_a_drive_root(self) -> None:
        # adversary pass on 530a447d, A2: trailing-'\' removal turned 'C:\' into 'C:' (drive-relative), which the absolute-path rule then refused.
        # A drive root keeps its one '\', as a tool dir and as -SystemRoot (whose own entry is a candidate too); it still dedups with 'c:\\'.
        out = self.json_of(self.build(r"C:\Windows", ["C:\\", "c:\\\\", r"D:\tools\\"]))
        self.assertEqual(out["value"].split(";")[4:], ["C:\\", r"D:\tools"])
        out = self.json_of(self.build("C:\\", []))
        self.assertEqual(out["value"].split(";"), [r"C:\System32", "C:\\", r"C:\System32\Wbem", r"C:\System32\WindowsPowerShell\v1.0"])
        p = self.build(r"C:\Windows", ["C:"])
        self.assertIn("THREW ATTRCUDA_MINIMAL_PATH_BAD_ENTRY", p.stdout, p.stdout + p.stderr)

    def test_adversary_8f761e39_a3_a_bare_drive_system_root_is_refused_like_a_bare_drive_tool_dir(self) -> None:
        # adversary pass on 8f761e39, A3 (advisory, introduced): -SystemRoot 'C:' was admitted and produced C:\System32;C:\;... although 'C:' in -ToolDirs is
        # refused. A bare drive is drive-relative (the process's current dir on that drive), so it is refused as -SystemRoot too; 'C:\' stays valid.
        for bare in ("C:", " c: "):
            with self.subTest(system_root=bare):
                p = self.build(bare, [])
                self.assertIn("THREW ATTRCUDA_MINIMAL_PATH_BAD_ENTRY", p.stdout, p.stdout + p.stderr)
        self.assertEqual(self.json_of(self.build("C:\\", []))["value"].split(";")[1], "C:\\")

    def test_an_entry_that_would_split_or_quote_the_path_is_refused(self) -> None:
        for bad in (r"C:\a;C:\b", 'C:\\a"b'):
            with self.subTest(bad=bad):
                p = self.build(r"C:\Windows", [bad])
                self.assertIn("THREW ATTRCUDA_MINIMAL_PATH_BAD_ENTRY", p.stdout, p.stdout + p.stderr)

    def test_a_relative_tool_dir_is_refused(self) -> None:
        p = self.build(r"C:\Windows", [r"tools\bin"])
        self.assertIn("THREW ATTRCUDA_MINIMAL_PATH_BAD_ENTRY", p.stdout, p.stdout + p.stderr)

    def test_a_blank_system_root_is_refused(self) -> None:
        p = self.build("", [])
        self.assertIn("THREW ATTRCUDA_MINIMAL_PATH_NO_SYSTEMROOT", p.stdout, p.stdout + p.stderr)


@requires_windows_pwsh
class MsvcIncludeTests(_Pwsh):
    """Part 2: after vcvars64, an INCLUDE without the Windows SDK is a typed msvcEnvironment refusal, never a bare C1083 at compile time."""

    def verdict(self, include: str) -> dict:
        return self.json_of(self.pwsh(
            f"Import-Module '{MODULE}' -Force\n"
            "$v = Test-AttrCudaMsvcInclude -Include $env:FIXTURE_INCLUDE\n"
            "'JSON=' + ($v | ConvertTo-Json -Compress)\n",
            env={"FIXTURE_INCLUDE": include},
        ))

    def kits(self, headers: bool = True, omit: str = "") -> str:
        """A FRESH fixture Windows Kits include root per call: ucrt / um / shared dirs, each holding its sentinel header unless headers=False
        (or it is `omit`)."""
        root = Path(tempfile.mkdtemp(prefix="kits-", dir=self.tmp)) / "Windows Kits" / "10" / "include" / "10.0.22621.0"
        for leaf, header in (("ucrt", "corecrt.h"), ("um", "windows.h"), ("shared", "sdkddkver.h")):
            (root / leaf).mkdir(parents=True, exist_ok=True)
            if headers and leaf != omit:
                (root / leaf / header).write_text("/* fixture */\n", encoding="ascii")
        return str(root)

    def test_the_incident_include_is_refused_as_lacking_ucrt(self) -> None:
        v = self.verdict(INCLUDE_NO_SDK)
        self.assertFalse(v["Ok"])
        self.assertEqual(v["Reason"], "INCLUDE_LACKS_UCRT")
        self.assertIn("entries=2", v["Detail"])

    def test_adversary_6c4053d9_a1_quoted_entries_are_judged_without_their_quotes(self) -> None:
        # adversary pass on 6c4053d9, A1: Trim() kept the closing quote, so '"<k>\ucrt";"<k>\um";"<k>\shared"' (headers present) read as INCLUDE_LACKS_UCRT and
        # the job exited 9 at msvcEnvironment before building. Quotes are stripped per entry, the same rule as the backend .cmd guards.
        amp_root = Path(tempfile.mkdtemp(prefix="kits-", dir=self.tmp)) / "SDK & Kits"
        for leaf, header in (("ucrt", "corecrt.h"), ("um", "windows.h"), ("shared", "sdkddkver.h")):
            (amp_root / leaf).mkdir(parents=True)
            (amp_root / leaf / header).write_text("/* fixture */\n", encoding="ascii")
        k = self.kits()
        for name, include in (("adversary-amp", f'"{amp_root}\\ucrt";"{amp_root}\\um";"{amp_root}\\shared"'),
                              ("kits-quoted-trailing-slash", f'"{k}\\ucrt\\";"{k}\\um";  "{k}\\shared"  ;'),
                              ("mixed", f'{INCLUDE_NO_SDK};"{k}\\ucrt";{k}\\um;"{k}\\shared"')):
            with self.subTest(case=name):
                v = self.verdict(include)
                self.assertTrue(v["Ok"], v)
        with self.subTest(case="quoted-no-headers"):
            bare = self.kits(headers=False)
            v = self.verdict(f'"{bare}\\ucrt";"{bare}\\um";"{bare}\\shared"')
            self.assertFalse(v["Ok"], v)
            self.assertEqual(v["Reason"], "INCLUDE_UCRT_HEADER_MISSING")
        with self.subTest(case="only-quotes"):
            v = self.verdict('"";" "')
            self.assertEqual((v["Ok"], v["Reason"]), (False, "INCLUDE_EMPTY"))

    def test_a_full_include_passes(self) -> None:
        k = self.kits()
        v = self.verdict(INCLUDE_NO_SDK + f";{k}\\ucrt;{k}\\um;{k}\\shared;{k}\\winrt")
        self.assertTrue(v["Ok"], v)
        self.assertEqual(v["Reason"], "")

    def test_ucrt_without_um_or_shared_is_refused_by_name(self) -> None:
        k = self.kits()
        self.assertEqual(self.verdict(INCLUDE_NO_SDK + f";{k}\\ucrt")["Reason"], "INCLUDE_LACKS_UM")
        self.assertEqual(self.verdict(INCLUDE_NO_SDK + f";{k}\\ucrt;{k}\\um")["Reason"], "INCLUDE_LACKS_SHARED")

    def test_an_empty_include_is_refused(self) -> None:
        self.assertEqual(self.verdict("")["Reason"], "INCLUDE_EMPTY")
        self.assertEqual(self.verdict(" ; ;")["Reason"], "INCLUDE_EMPTY")

    def test_ucrt_must_be_a_whole_path_segment(self) -> None:
        # a dir merely CONTAINING the letters (\ucrtx, \myucrt) is not the SDK's ucrt include dir
        k = self.kits()
        v = self.verdict(INCLUDE_NO_SDK + r";C:\x\notucrt;C:\x\ucrtx;" + f"{k}\\um;{k}\\shared")
        self.assertEqual(v["Reason"], "INCLUDE_LACKS_UCRT")

    def test_adversary_a1_named_sdk_dirs_without_their_headers_are_refused(self) -> None:
        # adversary A1 (r1): 'C:\missing\ucrt;C:\missing\um;C:\missing\shared' was Ok=true. A name is not a header: each leaf must hold its sentinel.
        v = self.verdict(r"C:\missing\ucrt;C:\missing\um;C:\missing\shared")
        self.assertFalse(v["Ok"], v)
        self.assertEqual(v["Reason"], "INCLUDE_UCRT_HEADER_MISSING")
        k = self.kits(headers=False)
        self.assertEqual(self.verdict(f"{k}\\ucrt;{k}\\um;{k}\\shared")["Reason"], "INCLUDE_UCRT_HEADER_MISSING")
        for leaf in ("um", "shared"):
            with self.subTest(leaf=leaf):
                k = self.kits(omit=leaf)
                self.assertEqual(self.verdict(f"{k}\\ucrt;{k}\\um;{k}\\shared")["Reason"], f"INCLUDE_{leaf.upper()}_HEADER_MISSING")
                (Path(k) / leaf / {"um": "windows.h", "shared": "sdkddkver.h"}[leaf]).write_text("/* fixture */\n", encoding="ascii")

    def test_adversary_f7c5a3c0_a1_a_probe_that_throws_is_a_verdict_never_an_exception(self) -> None:
        # adversary pass on f7c5a3c0, A1: Test-Path inside the Where-Object predicate could throw under ErrorActionPreference=Stop, and the job's
        # unguarded call then died untyped instead of exit 9 at msvcEnvironment. An access-denied ACL does NOT throw on this host (probe-testpath-throw.ps1
        # returned True), but a NUL in an entry does (ArgumentException), so it stands for the class: the throwing entry counts as not holding the header.
        proc = self.pwsh(
            f"Import-Module '{MODULE}' -Force\n"
            "$v = Test-AttrCudaMsvcInclude -Include ('C:\\x' + [char]0 + 'y\\ucrt;C:\\x\\um;C:\\x\\shared')\n"
            "'JSON=' + ($v | ConvertTo-Json -Compress)\n")
        v = self.json_of(proc)
        self.assertEqual((v["Ok"], v["Reason"]), (False, "INCLUDE_UCRT_HEADER_MISSING"), v)
        self.assertIn("probeErrors=1", v["Detail"])
        # a throwing entry beside a real one does not hide the real header
        k = self.kits()
        proc = self.pwsh(
            f"Import-Module '{MODULE}' -Force\n"
            f"$v = Test-AttrCudaMsvcInclude -Include ('C:\\x' + [char]0 + 'y\\ucrt;{k}\\ucrt;{k}\\um;{k}\\shared')\n"
            "'JSON=' + ($v | ConvertTo-Json -Compress)\n")
        self.assertTrue(self.json_of(proc)["Ok"])

    def test_adversary_8f761e39_a1_a_header_another_process_holds_exclusively_is_not_a_usable_header(self) -> None:
        # adversary pass on 8f761e39, A1: Test-Path reads attributes, which an exclusive open does not block, so a corecrt.h held with FileShare.None passed
        # the judge and the compile then died with a bare C1083 (generic exit 11). The judge now OPENS the header for reading, sharing read/write/delete, so a
        # header nobody else can read is the typed INCLUDE_<LEAF>_HEADER_MISSING (the open's failure is counted in probeErrors).
        for leaf, header in (("ucrt", "corecrt.h"), ("um", "windows.h"), ("shared", "sdkddkver.h")):
            with self.subTest(leaf=leaf):
                k = self.kits()
                include = f"{k}\\ucrt;{k}\\um;{k}\\shared"
                self.assertTrue(self.verdict(include)["Ok"])
                with exclusive_lock(Path(k) / leaf / header):
                    v = self.verdict(include)
                self.assertEqual((v["Ok"], v["Reason"]), (False, f"INCLUDE_{leaf.upper()}_HEADER_MISSING"), v)
                self.assertIn("probeErrors=1", v["Detail"])
                self.assertTrue(self.verdict(include)["Ok"], "the lock is released, so the same INCLUDE passes again")
        # a locked header beside a readable one in another entry does not hide the readable one
        k = self.kits()
        spare = self.kits()
        with exclusive_lock(Path(k) / "ucrt" / "corecrt.h"):
            self.assertTrue(self.verdict(f"{k}\\ucrt;{spare}\\ucrt;{k}\\um;{k}\\shared")["Ok"])

    def test_a_header_in_any_matching_entry_suffices_and_a_missing_drive_does_not_throw(self) -> None:
        k = self.kits()
        v = self.verdict(rf"Q:\no-such-drive\ucrt;{k}\ucrt;{k}\um;{k}\shared")
        self.assertTrue(v["Ok"], v)


BACKEND_SCRIPTS = (ROOT / "tools" / "gpu" / "backend" / "build-backend-dll.ps1", ROOT / "tools" / "gpu" / "backend" / "amaze-debayer-dll.ps1")


@requires_windows_pwsh
class BackendCmdUcrtCheckTests(_Pwsh):
    """Adversary A1 siblings: the tracked backend scripts' own .cmd files refuse a post-vcvars INCLUDE without corecrt.h at exit 9 BEFORE nvcc,
    so a standalone run (over SSH, no job around it) also never meets C1083 first. The cmd lines are taken from each script's text and run in cmd.exe."""

    def check_lines(self, script: Path) -> list[str]:
        lines = re.findall(r"^\s*'((?:[^']|'')*MLV_(?:UCRT_OK|INC)(?:[^']|'')*)',?\s*$", script.read_text(encoding="utf-8"), re.M)
        return [l.replace("''", "'") for l in lines]

    def test_adversary_f7c5a3c0_a2_a_bang_in_systemroot_still_finds_where(self) -> None:
        # adversary pass on f7c5a3c0, A2: inside EnableDelayedExpansion the percent-expanded %SystemRoot% was re-parsed, so SystemRoot=C:\...\Win!OS!
        # became ...\WinWindows_NT, both where calls ran a missing exe, and a valid corecrt.h was refused at exit 9. where.exe's path is now read into a
        # variable BEFORE setlocal and used as !MLV_WHERE!, whose value delayed expansion does not re-parse. The fixture copies where.exe there.
        win = self.tmp / "Win!OS!"
        (win / "System32").mkdir(parents=True)
        shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", win / "System32" / "where.exe")
        self.assertFalse((self.tmp / "WinWindows_NT").exists())
        kit = self.tmp / "Kits-bang-root" / "ucrt"
        kit.mkdir(parents=True)
        (kit / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
        bare = self.tmp / "Kits-bang-root-bare" / "ucrt"
        bare.mkdir(parents=True)
        for script in BACKEND_SCRIPTS:
            for name, include in (("plain", f"{INCLUDE_NO_SDK};{kit}"), ("quoted", f'{INCLUDE_NO_SDK};"{kit}"')):
                with self.subTest(script=script.name, case=name, header=True):
                    p = self.run_check(script, include, via_env=True, system_root=str(win))
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("REACHED_NVCC", p.stdout)
                    self.assertIn("WHERE_VAR_AT_NVCC=undefined", p.stdout)
            with self.subTest(script=script.name, case="bare", header=False):
                p = self.run_check(script, f"{INCLUDE_NO_SDK};{bare}", via_env=True, system_root=str(win))
                self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)

    def run_check(self, script: Path, include: str | None, via_env: bool = False, pathext: str | None = None,
                  system_root: str | None = None) -> subprocess.CompletedProcess:
        # via_env: INCLUDE arrives in the process environment exactly as given (quotes and '&' included), as vcvars64 leaves it, not via a set line
        cmd = self.tmp / "check.cmd"
        head = [] if via_env else ([f'set "INCLUDE={include}"'] if include is not None else ['set "INCLUDE="'])
        # where nvcc would run: PATHEXT is the caller's again and delayed expansion is off (a literal !MLV_INC! is not expanded)
        tail = ["echo REACHED_NVCC", "echo PATHEXT_AT_NVCC=%PATHEXT%", "echo DELAYED_AT_NVCC=!MLV_INC!",
                "if defined MLV_WHERE (echo WHERE_VAR_AT_NVCC=defined) else (echo WHERE_VAR_AT_NVCC=undefined)", "exit /b 0"]
        body = ["@echo off"] + head + self.check_lines(script) + tail
        cmd.write_text("\r\n".join(body) + "\r\n", encoding="ascii")
        env = dict(os.environ)
        if via_env:
            env["INCLUDE"] = include or ""
        if pathext is not None:
            env["PATHEXT"] = pathext
        if system_root is not None:
            env["SYSTEMROOT"] = system_root  # dict(os.environ) keys are upper-case on Windows; a second 'SystemRoot' key would be ambiguous
        return subprocess.run(["cmd.exe", "/d", "/c", str(cmd)], capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)

    def test_adversary_9f89_a1_a_quoted_entry_with_an_ampersand_still_reaches_the_typed_refusal(self) -> None:
        # adversary pass on 9f89f0a1, A1: INCLUDE=C:\missing;"C:\SDK & Kits\ucrt" broke the for-list ('& was unexpected') before the exit-9 guard
        amp = self.tmp / "SDK & Kits" / "ucrt"
        amp.mkdir(parents=True)
        for script in BACKEND_SCRIPTS:
            with self.subTest(script=script.name, header=False):
                p = self.run_check(script, f'C:\\missing;"{amp}"', via_env=True)
                self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                self.assertNotIn("REACHED_NVCC", p.stdout)
        (amp / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
        for script in BACKEND_SCRIPTS:
            with self.subTest(script=script.name, header=True):
                p = self.run_check(script, f'C:\\missing;"{amp}"', via_env=True)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("REACHED_NVCC", p.stdout)

    def test_adversary_63580c71_a1_a_long_include_reaches_the_typed_refusal(self) -> None:
        # adversary pass on 63580c71, A1: INCLUDE = ('C:\missing;' * 640).TrimEnd(';') (7039 chars) grew past cmd.exe's 8191-character line limit
        # once ';' became '" "' in the for-list: exit 1, no typed line. The check must never expand INCLUDE onto a command line; 32 KB values too.
        kit = self.tmp / "Kits-long" / "ucrt"
        kit.mkdir(parents=True)
        (kit / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
        long_inc = ";".join([r"C:\missing"] * 640)
        self.assertEqual(len(long_inc), 7039)
        huge_inc = ";".join([r"C:\missing\a\much\longer\directory\name"] * 800)
        for script in BACKEND_SCRIPTS:
            for name, include, via_env in (("adversary-7039", long_inc, False), ("adversary-7039-env", long_inc, True), ("huge-env", huge_inc, True)):
                with self.subTest(script=script.name, case=name, header=False):
                    p = self.run_check(script, include, via_env=via_env)
                    self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                    self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                    self.assertNotIn("REACHED_NVCC", p.stdout)
            for name, include in (("adversary-7039-quoted", f'{long_inc};"{kit}"'), ("adversary-7039", f"{long_inc};{kit}"), ("huge", f"{huge_inc};{kit}")):
                with self.subTest(script=script.name, case=name, header=True):
                    p = self.run_check(script, include, via_env=True)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("REACHED_NVCC", p.stdout)
            bang = self.tmp / "SDK & Kits!v10!" / "ucrt"
            if not bang.exists():
                bang.mkdir(parents=True)
                (bang / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
            with self.subTest(script=script.name, case="quoted-amp-bang", header=True):
                # the quote-stripped copy goes through DELAYED expansion: a '!' (and '&') inside the value must survive it, not be re-parsed
                p = self.run_check(script, f'C:\\missing;"{bang}"', via_env=True)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("REACHED_NVCC", p.stdout)
            with self.subTest(script=script.name, case="huge-quoted", header=True):
                # the one shape the check cannot admit: an INCLUDE past cmd's limit whose header entry is QUOTED (the quote-stripped copy is cmd-bound).
                # It is refused TYPED, never exit 1. vcvars64 builds INCLUDE through cmd set lines, so a post-vcvars INCLUDE this long does not arise.
                p = self.run_check(script, f'{huge_inc};"{kit}"', via_env=True)
                self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                self.assertNotIn("REACHED_NVCC", p.stdout)

    def test_each_script_checks_after_vcvars_and_before_nvcc(self) -> None:
        for script in BACKEND_SCRIPTS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                self.assertEqual(len(self.check_lines(script)), 9, script.name)
                vcvars = text.index('"call `"$vcvars`" >nul"')
                check = text.index("MLV_UCRT_OK")
                nvcc = text.index('"`"$nvcc`" $archArgLine')
                self.assertLess(vcvars, check)
                self.assertLess(check, nvcc)

    def test_the_check_refuses_the_incident_include_and_an_empty_one(self) -> None:
        for script in BACKEND_SCRIPTS:
            for include in (INCLUDE_NO_SDK, r"C:\missing\ucrt;C:\missing\um", "", None):
                with self.subTest(script=script.name, include=include):
                    p = self.run_check(script, include)
                    self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                    self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                    self.assertNotIn("REACHED_NVCC", p.stdout)

    def test_adversary_pass2_a1_a_directory_named_corecrt_h_is_not_the_header(self) -> None:
        # adversary pass 2 A1 (a5bfd431): cmd's `if exist` is true for a DIRECTORY, so ucrt\corecrt.h as a dir passed and nvcc ran without the header.
        root = self.tmp / "Kits-dir" / "ucrt"
        (root / "corecrt.h").mkdir(parents=True)
        for script in BACKEND_SCRIPTS:
            with self.subTest(script=script.name):
                p = self.run_check(script, INCLUDE_NO_SDK + ";" + str(root))
                self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                self.assertNotIn("REACHED_NVCC", p.stdout)

    def test_adversary_530a447d_a1_a_pathext_suffixed_file_is_not_the_header(self) -> None:
        # adversary pass on 530a447d, A1: where.exe appends PATHEXT, so ucrt\corecrt.h.cmd (no corecrt.h) set MLV_UCRT_OK=1 and nvcc ran without the
        # header. PATHEXT is cleared for BOTH where calls inside one setlocal, and endlocal restores it before nvcc. Every PATHEXT extension is tried,
        # in a plain INCLUDE (first where) and a quoted one (second where, after the quote strip).
        pathext = ".COM;.EXE;.BAT;.CMD;.VBS;.JS"
        for ext in (".cmd", ".exe", ".bat", ".com"):
            root = self.tmp / f"Kits-pathext{ext}" / "ucrt"
            root.mkdir(parents=True)
            (root / f"corecrt.h{ext}").write_text("@rem fixture\n", encoding="ascii")
            for script in BACKEND_SCRIPTS:
                for name, include in (("plain", f"{INCLUDE_NO_SDK};{root}"), ("quoted", f'{INCLUDE_NO_SDK};"{root}"')):
                    with self.subTest(script=script.name, ext=ext, case=name):
                        p = self.run_check(script, include, via_env=True, pathext=pathext)
                        self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                        self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                        self.assertNotIn("REACHED_NVCC", p.stdout)
        # the real header beside a suffixed decoy still passes, and PATHEXT is back to its value when nvcc would run
        both = self.tmp / "Kits-pathext-both" / "ucrt"
        both.mkdir(parents=True)
        (both / "corecrt.h.exe").write_text("x\n", encoding="ascii")
        (both / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
        for script in BACKEND_SCRIPTS:
            for name, include in (("plain", f"{INCLUDE_NO_SDK};{both}"), ("quoted", f'{INCLUDE_NO_SDK};"{both}"')):
                with self.subTest(script=script.name, case=f"real-{name}"):
                    p = self.run_check(script, include, via_env=True, pathext=pathext)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("REACHED_NVCC", p.stdout)
                    self.assertIn(f"PATHEXT_AT_NVCC={pathext}", p.stdout)
                    self.assertIn("DELAYED_AT_NVCC=!MLV_INC!", p.stdout)

    def test_adversary_8f761e39_a1_a_header_another_process_holds_exclusively_is_refused_at_exit_9(self) -> None:
        # adversary pass on 8f761e39, A1 (siblings build-backend-dll.ps1:166,168 and amaze-debayer-dll.ps1:85,87): where /q finds a name without opening
        # it, so a corecrt.h held with FileShare.None set MLV_UCRT_OK=1 and nvcc ran into a bare C1083. The check now TYPEs each hit (opens it for reading),
        # so an unreadable header is exit 9 at msvcEnvironment, in a plain and in a quoted INCLUDE.
        for script in BACKEND_SCRIPTS:
            root = self.tmp / f"Kits-locked-{script.stem}" / "ucrt"
            root.mkdir(parents=True)
            header = root / "corecrt.h"
            header.write_text("/* fixture */\n", encoding="ascii")
            for name, include in (("plain", f"{INCLUDE_NO_SDK};{root}"), ("quoted", f'{INCLUDE_NO_SDK};"{root}"')):
                with self.subTest(script=script.name, case=name, locked=True):
                    with exclusive_lock(header):
                        p = self.run_check(script, include, via_env=True)
                    self.assertEqual(p.returncode, 9, p.stdout + p.stderr)
                    self.assertIn("FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT", p.stdout)
                    self.assertNotIn("REACHED_NVCC", p.stdout)
                with self.subTest(script=script.name, case=name, locked=False):
                    p = self.run_check(script, include, via_env=True)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("REACHED_NVCC", p.stdout)

    def test_a_locked_header_beside_a_readable_one_still_passes(self) -> None:
        for script in BACKEND_SCRIPTS:
            locked = self.tmp / f"Kits-lockedfirst-{script.stem}" / "ucrt"
            good = self.tmp / f"Kits-goodsecond-{script.stem}" / "ucrt"
            for d in (locked, good):
                d.mkdir(parents=True)
                (d / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
            with self.subTest(script=script.name):
                with exclusive_lock(locked / "corecrt.h"):
                    p = self.run_check(script, f"{INCLUDE_NO_SDK};{locked};{good}", via_env=True)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("REACHED_NVCC", p.stdout)

    def test_the_check_admits_an_include_whose_ucrt_holds_corecrt(self) -> None:
        root = self.tmp / "Windows Kits (x86)" / "10" / "include" / "10.0.22621.0" / "ucrt"
        root.mkdir(parents=True)
        (root / "corecrt.h").write_text("/* fixture */\n", encoding="ascii")
        for script in BACKEND_SCRIPTS:
            with self.subTest(script=script.name):
                p = self.run_check(script, INCLUDE_NO_SDK + ";" + str(root) + ";")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("REACHED_NVCC", p.stdout)


def _head() -> str:
    return subprocess.run([GIT, "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


def _emit(tmp: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the REAL generator for this checkout's HEAD into <tmp>\\out (a git archive of the commit; nothing is submitted)."""
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DLL_GENERATOR), "-SourceCommit", _head(), "-OutDir", str(tmp / "out"),
                           "-AgentRoot", str(tmp / "agent"), *extra],
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)


@requires_windows_pwsh
@unittest.skipIf(GIT is None, "git is not on PATH")
class EmittedJobWiringTests(unittest.TestCase):
    """Parts 1-2 in the emitted job: the minimal PATH is in place, and INCLUDE proven, BEFORE the first backend build child starts.
    One default (unsuffixed) emit for the class: a git archive of the whole tree is the slow part."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="um-dllpair-emit-")
        cls.tmp = Path(os.path.realpath(cls._tmp.name))
        cls.proc = _emit(cls.tmp)
        cls.jobs = list((cls.tmp / "out").glob("*.job.ps1"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def job_text(self) -> tuple[str, Path]:
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)
        self.assertEqual(len(self.jobs), 1, self.jobs)
        return self.jobs[0].read_text(encoding="utf-8"), self.jobs[0]

    def test_without_a_suffix_the_default_names_are_unchanged(self) -> None:
        _, job = self.job_text()
        self.assertEqual(job.name, f"playback-attr-3-cuda-dllpair-{_head()[:12]}.job.ps1")
        self.assertTrue((self.tmp / "out" / f"playback-attr-3-cuda-dllpair-{_head()[:12]}-source.zip").is_file())

    def test_msvc_environment_is_proven_before_the_first_build_child(self) -> None:
        text, _ = self.job_text()
        set_path = text.index("$env:PATH = Get-AttrCudaMinimalBuildPath")
        probe = text.index("'msvcEnvironment'")
        include_check = text.index("Test-AttrCudaMsvcInclude -Include")
        first_build = text.index("& $psExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $reconCommand")
        self.assertLess(set_path, include_check)
        self.assertLess(include_check, first_build)
        self.assertLess(probe, first_build)
        # the embedded definitions precede their first use
        self.assertLess(text.index("function Get-AttrCudaMinimalBuildPath {"), set_path)
        self.assertLess(text.index("function Test-AttrCudaMsvcInclude {"), include_check)
        # stale compiler env is cleared, so the INCLUDE judged is the one vcvars64 produced
        for var in ("INCLUDE", "LIB", "LIBPATH"):
            self.assertIn(f"$env:{var} = $null", text)
        self.assertRegex(text, r"Complete-Failed 9 'msvcEnvironment' \"reason=")

    def test_adversary_f7c5a3c0_a1_the_judge_and_the_vcvars_probe_never_escape_untyped(self) -> None:
        # adversary pass on f7c5a3c0, A1 (call site :403 and sibling :379): under ErrorActionPreference=Stop a throw from the INCLUDE judge or from the
        # vcvars64 Test-Path ended the job untyped. Both are caught and become exit 9 at msvcEnvironment, before the log and the typed refusal.
        text, _ = self.job_text()
        self.assertRegex(text, r"try \{\s*\$msvcVerdict = Test-AttrCudaMsvcInclude -Include \$msvcInclude\s*\} catch \{[^}]*'INCLUDE_JUDGE_FAILED'")
        self.assertRegex(text, r"try \{ \$vcvarsPresent = Test-Path -LiteralPath \$vcvars \} catch \{ Complete-Failed 9 'msvcEnvironment' \"reason=VCVARS_PROBE_FAILED")
        self.assertNotIn("if (-not (Test-Path -LiteralPath $vcvars)) { Complete-Failed 9", text)
        self.assertLess(text.index("'INCLUDE_JUDGE_FAILED'"), text.index("Complete-Failed 9 'msvcEnvironment' \"reason=$($msvcVerdict.Reason)"))

    def test_adversary_a2_a_log_write_never_preempts_the_typed_exit(self) -> None:
        # adversary A2 (r1): a refused msvc-environment.log write threw before Complete-Failed 9, so the job ended untyped. Every build/probe LOG
        # publish (msvc, recon, amaze: the class) goes through Publish-JobLog, which reports a failed write and returns.
        text, _ = self.job_text()
        for raw in ("[void](Publish-AttrCudaText -Path (Join-Path $Pub 'msvc-environment.log')", "[void](Publish-AttrCudaText -Path $reconLog",
                    "[void](Publish-AttrCudaText -Path $amazeLog"):
            self.assertNotIn(raw, text)
        for logged in ("Publish-JobLog (Join-Path $Pub 'msvc-environment.log') $msvcLog", "Publish-JobLog $reconLog $reconOutput",
                       "Publish-JobLog $amazeLog $amazeOutput"):
            self.assertIn(logged, text)
        self.assertLess(text.index("Publish-JobLog (Join-Path $Pub 'msvc-environment.log')"),
                        text.index("Complete-Failed 9 'msvcEnvironment' \"reason=$($msvcVerdict.Reason)"))
        # and the probe's own file write / cmd run are typed too
        self.assertIn("Complete-Failed 9 'msvcEnvironment' \"reason=PROBE_FAILED", text)
        # adversary pass 2 A2 (advisory): a backend script's OWN msvcEnvironment refusal (exit 9 + its FAIL line) stays exit 9 at msvcEnvironment,
        # for recon AND amaze, checked before the generic exit 11 / 12 mapping
        for rc, out, dll, generic in (("$reconRc", "$reconOutput", "$reconDll", "Complete-Failed 11 'reconBackendBuild'"),
                                      ("$amazeRc", "$amazeOutput", "$amazeDll", "Complete-Failed 12 'amazeBackendBuild'")):
            typed = f"Test-BackendMsvcRefusal {rc} {out}"
            self.assertIn(typed, text)
            self.assertLess(text.index(typed), text.index(generic))
        fn2 = re.search(r"(?ms)^function Test-BackendMsvcRefusal\b.*?^\}", text)
        self.assertIsNotNone(fn2)
        # Publish-JobLog itself, run from the EMITTED text against a publisher that throws: no throw out, one LOG_NOT_WRITTEN line
        fn = re.search(r"(?ms)^function Publish-JobLog\b.*?^\}", text)
        self.assertIsNotNone(fn)
        tmp = Path(tempfile.mkdtemp(prefix="um-joblog-", dir=self.tmp))
        probe = tmp / "p.ps1"
        probe.write_text("$ErrorActionPreference = 'Stop'\nfunction Say([string]$m) { Write-Output \"SAY $m\" }\n"
                         "function Publish-AttrCudaText { param($Path, $Value) throw 'ATTRCUDA_SLOT_NOT_WRITABLE fixture lock' }\n"
                         + fn.group(0) + "\nPublish-JobLog 'G:\\x\\outbox\\j.artifacts\\msvc-environment.log' @('a', 'b')\n'AFTER'\n", encoding="utf-8")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("SAY LOG_NOT_WRITTEN msvc-environment.log ATTRCUDA_SLOT_NOT_WRITABLE fixture lock", p.stdout)
        self.assertIn("AFTER", p.stdout)

    def test_the_minimal_path_entries_are_derived_never_a_hosts_user_dirs(self) -> None:
        text, _ = self.job_text()
        line = [l for l in text.splitlines() if "$env:PATH = Get-AttrCudaMinimalBuildPath" in l]
        self.assertEqual(len(line), 1, line)
        self.assertIn("-SystemRoot ([Environment]::GetEnvironmentVariable('SystemRoot'))", line[0])
        self.assertIn("$cudaBin", line[0])
        for leak in ("Users\\", ".dotnet", "obabalola", "AppData"):
            self.assertNotIn(leak, line[0])


@requires_windows_pwsh
class VcvarsPathDerivationTests(_Pwsh):
    """Adversary A2 on 8f761e39: the vcvars64 path is derived BEFORE the try that types its failures, so it must not be able to throw."""

    def vcvars_line(self) -> str:
        lines = [l for l in DLL_GENERATOR.read_text(encoding="utf-8").splitlines() if l.startswith("$vcvars = ")]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_adversary_8f761e39_a2_an_unavailable_drive_in_the_vs_root_does_not_throw(self) -> None:
        # vswhere answers a nonempty installationPath on a drive that is not mounted (Q:\VS); Join-Path throws DriveNotFound for it under
        # ErrorActionPreference=Stop, before the try that turns a missing vcvars into exit 9 at msvcEnvironment. The path is built by string concatenation, so
        # the later Test-Path (inside that try) is what reports it, typed. Sibling Join-Path sites in OTHER scripts are pre-existing and out of this card.
        self.assertNotIn("Join-Path $vsRoot 'VC\\Auxiliary\\Build\\vcvars64.bat'", DLL_GENERATOR.read_text(encoding="utf-8"))
        line = self.vcvars_line()
        for root, want in ((r"Q:\VS", r"Q:\VS\VC\Auxiliary\Build\vcvars64.bat"), ("Q:\\VS\\", r"Q:\VS\VC\Auxiliary\Build\vcvars64.bat"),
                           (r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools", r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat")):
            with self.subTest(vs_root=root):
                out = self.json_of(self.pwsh(f"$vsRoot = '{root}'\n{line}\n'JSON=' + (@{{ vcvars = $vcvars }} | ConvertTo-Json -Compress)\n"))
                self.assertEqual(out["vcvars"], want)
        probe = self.json_of(self.pwsh(f"$vsRoot = 'Q:\\VS'\n{line}\n"
                                       "$present = $true\ntry { $present = Test-Path -LiteralPath $vcvars } catch { $present = 'threw' }\n"
                                       "'JSON=' + (@{ present = $present } | ConvertTo-Json -Compress)\n"))
        self.assertIn(probe["present"], (False, "threw"))


SCANNER = ROOT / "tools" / "repo_hygiene" / "attr3_publish_write_scan.ps1"


@requires_windows_pwsh
class ScannerEnvRuleTests(_Pwsh):
    """The template tripwire admits the job's PATH / compiler-variable lines in exactly one shape each, and nothing wider."""

    def env_violations(self, line: str) -> list[dict]:
        tpl = self.tmp / "template.ps1"
        tpl.write_text("$Work = Join-Path $AgentRoot \"work\\$JobId\"\n" + line + "\n", encoding="utf-8")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(SCANNER), "-TemplateFile", str(tpl)],
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        start = p.stdout.find("{")
        self.assertGreaterEqual(start, 0, p.stdout + p.stderr)
        report = json.loads(p.stdout[start:])
        return [v for v in report["violations"] if "env:" in json.dumps(v)]

    def test_the_two_admitted_shapes_pass(self) -> None:
        self.assertEqual(self.env_violations("$env:PATH = Get-AttrCudaMinimalBuildPath -SystemRoot 'C:\\Windows' -ToolDirs @('C:\\x')"), [])
        for var in ("INCLUDE", "LIB", "LIBPATH"):
            self.assertEqual(self.env_violations(f"$env:{var} = $null"), [], var)

    def test_every_other_shape_is_still_r5(self) -> None:
        for line in ("$env:PATH = 'C:\\x'", "$env:PATH += ';C:\\x'", "$env:PATH = Get-AttrCudaMinimalBuildPath | Select-Object -First 1",
                     "$env:PATH = & Get-AttrCudaMinimalBuildPath -SystemRoot 'C:\\Windows'", "$env:PATH = $other",
                     "$env:INCLUDE = 'C:\\x'", "$env:LIB += 'x'", "$env:Path = Get-OtherPath", "$env:CUDA_PATH = $null"):
            with self.subTest(line=line):
                self.assertTrue(any(v.get("rule") == "R5" for v in self.env_violations(line)), line)


@requires_windows_pwsh
class DllPairJobIdTests(_Pwsh):
    """Part 3: an explicit, opt-in retry suffix; the default id is unchanged."""

    def ids(self, suffix: str | None) -> dict:
        arg = "" if suffix is None else f" -JobIdSuffix '{suffix}'"
        return self.json_of(self.pwsh(
            f"Import-Module '{MODULE}' -Force\n"
            "try {\n"
            f"  'JSON=' + (@{{ id = (Get-AttrCudaDllPairJobId -SourceCommit '{SHA}'{arg}) }} | ConvertTo-Json -Compress)\n"
            "} catch { 'JSON=' + (@{ threw = $_.Exception.Message } | ConvertTo-Json -Compress) }\n"))

    def test_default_id_is_unchanged(self) -> None:
        self.assertEqual(self.ids(None)["id"], f"playback-attr-3-cuda-dllpair-{S12}")
        self.assertEqual(self.ids("")["id"], f"playback-attr-3-cuda-dllpair-{S12}")

    def test_suffix_appends(self) -> None:
        self.assertEqual(self.ids("r2")["id"], f"playback-attr-3-cuda-dllpair-{S12}-r2")

    def test_a_bad_suffix_is_refused(self) -> None:
        # adversary A3 (r1, advisory): 'r--' and a trailing '-' made an id ending in '--' / '-'; a hyphen now only joins alphanumeric runs.
        for bad in ("r2;x", "r 2", "../r2", "r" * 33, "-r2", "r--", "r-", "a--b"):
            with self.subTest(bad=bad):
                self.assertIn("DLLPAIR_JOBID_SUFFIX_INVALID", self.ids(bad).get("threw", ""), bad)
        self.assertEqual(self.ids("r2-b")["id"], f"playback-attr-3-cuda-dllpair-{S12}-r2-b")
        self.assertEqual(self.ids("r" * 32)["id"], f"playback-attr-3-cuda-dllpair-{S12}-" + "r" * 32)

    def test_the_nightly_refuses_the_same_bad_suffixes_at_binding(self) -> None:
        for bad in ("r--", "r-", "-r2"):
            with self.subTest(bad=bad):
                p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(NIGHTLY), "-SourceCommit", SHA, "-WhatIf",
                                    "-DllJobIdSuffix", bad], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                self.assertNotEqual(p.returncode, 0, p.stdout)
                self.assertIn("DllJobIdSuffix", p.stdout + p.stderr)

    def test_generator_emits_the_suffixed_job_and_zip(self) -> None:
        if GIT is None:
            self.skipTest("git is not on PATH")
        p = _emit(self.tmp, "-JobIdSuffix", "r2")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        jid = f"playback-attr-3-cuda-dllpair-{_head()[:12]}-r2"
        self.assertEqual([j.name for j in (self.tmp / "out").glob("*.job.ps1")], [f"{jid}.job.ps1"])
        self.assertTrue((self.tmp / "out" / f"{jid}-source.zip").is_file())
        self.assertIn(f"$JobId = '{jid}'", (self.tmp / "out" / f"{jid}.job.ps1").read_text(encoding="utf-8"))

    def test_generator_refuses_a_bad_suffix_before_emitting(self) -> None:
        if GIT is None:
            self.skipTest("git is not on PATH")
        p = _emit(self.tmp, "-JobIdSuffix", "r2;x")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("DLLPAIR_JOBID_SUFFIX_INVALID", p.stdout + p.stderr)
        self.assertEqual(list((self.tmp / "out").glob("*")) if (self.tmp / "out").exists() else [], [])

    def test_nightly_plan_names_the_suffixed_dll_pair_id(self) -> None:
        # -WhatIf: plan only, nothing probed or written; the -DllJobIdSuffix reaches the step-1 line. An empty checkout through the -RepoRoot seam,
        # as test_venue_stage_nightly does, because a lane's TEMP can sit inside the worktree (the driver refuses a -BuildRoot inside its checkout).
        if GIT is None:
            self.skipTest("git is not on PATH")
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        subprocess.run([GIT, "-C", str(checkout), "init", "-q"], check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(NIGHTLY), "-SourceCommit", SHA, "-WhatIf", "-RepoRoot", str(checkout),
                            "-AgentShare", str(self.tmp / "no-such-share"), "-ReceiptFile", str(self.tmp / ".claude-state" / "r.jsonl"),
                            "-BuildRoot", str(self.tmp / "build"), "-NowUtc", "2026-10-19T21:00:00Z", "-DllJobIdSuffix", "r2"],
                           capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        step1 = [l for l in p.stdout.splitlines() if l.startswith("STAGE_NIGHTLY_PLAN_STEP 1/4")]
        self.assertEqual(len(step1), 1, p.stdout)
        self.assertIn(f"-JobId playback-attr-3-cuda-dllpair-{S12}-r2 ", step1[0])
        self.assertIn(f"-JobIdSuffix r2", step1[0])


@requires_windows_pwsh
class DllPairReuseSuffixTests(_Pwsh):
    """Part 3: Get-DllPairReuse refuses a FAILED id (the default stays spent) and accepts a SUCCEEDED suffixed id."""

    def setUp(self) -> None:
        super().setUp()
        self.outbox = self.tmp / "outbox"
        self.outbox.mkdir()

    def failed(self, jid: str) -> None:
        art = self.outbox / f"{jid}.artifacts"
        art.mkdir()
        (self.outbox / f"{jid}.result.json").write_text(json.dumps({
            "jobId": jid, "exitCode": 11, "timedOut": False, "endedUtc": "2026-11-15T01:09:17Z",
            "stdout": "FAIL step=reconBackendBuild exit=11 igpu_recon_cuda.dll not produced (script exit=1)\nRESULT=DLL_PAIR_FAILED STEP=reconBackendBuild EXIT=11"}),
            encoding="utf-8")

    def built(self, jid: str, sha: str = SHA) -> None:
        art = self.outbox / f"{jid}.artifacts"
        art.mkdir()
        (art / "dll-pair-manifest.json").write_text(json.dumps({"sourceCommit": sha, "jobId": jid}), encoding="utf-8")
        (self.outbox / f"{jid}.result.json").write_text(json.dumps({
            "jobId": jid, "exitCode": 0, "timedOut": False, "endedUtc": "2026-11-15T02:30:00Z",
            "stdout": f"RESULT=DLL_PAIR_OK SOURCE={sha} ARCH=sm_86 RECON=x AMAZE=y ARTIFACTS=z"}), encoding="utf-8")

    def reuse(self, jid: str, suffix: str | None = None) -> dict:
        arg = "" if suffix is None else f" -JobIdSuffix '{suffix}'"
        return self.json_of(self.pwsh(
            f"Import-Module '{REUSE_MODULE}' -Force\n"
            "try {\n"
            f"  $r = Get-DllPairReuse -Outbox '{self.outbox}' -JobId '{jid}' -Sha '{SHA}'{arg}\n"
            "  'JSON=' + ($r | ConvertTo-Json -Compress)\n"
            "} catch { 'JSON=' + (@{ threw = $_.Exception.Message } | ConvertTo-Json -Compress) }\n"))

    def test_a_failed_default_id_is_refused(self) -> None:
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        self.failed(base)
        r = self.reuse(base)
        self.assertFalse(r["Reusable"])
        self.assertEqual(r["Reason"], "exitCode-not-0 (11)")

    def test_a_succeeded_suffixed_id_is_accepted_beside_the_failed_default(self) -> None:
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        self.failed(base)
        self.built(f"{base}-r2")
        r = self.reuse(base, "r2")
        self.assertTrue(r["Reusable"], r)
        self.assertEqual(r["JobId"], f"{base}-r2")
        self.assertEqual(Path(r["ArtifactsDir"]).name, f"{base}-r2.artifacts")
        # the default lookup is unchanged: still refused
        self.assertEqual(self.reuse(base)["Reason"], "exitCode-not-0 (11)")

    def test_a_failed_suffixed_id_is_refused_too(self) -> None:
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        self.built(base)
        self.failed(f"{base}-r2")
        r = self.reuse(base, "r2")
        self.assertFalse(r["Reusable"])
        self.assertEqual(r["Reason"], "exitCode-not-0 (11)")
        self.assertEqual(r["JobId"], f"{base}-r2")

    def test_a_suffixed_lookup_with_no_result_is_result_missing(self) -> None:
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        self.failed(base)
        r = self.reuse(base, "r3")
        self.assertFalse(r["Reusable"])
        self.assertEqual(r["Reason"], "result-missing")

    def test_a_suffixed_manifest_naming_the_default_id_is_refused(self) -> None:
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        jid = f"{base}-r2"
        self.built(jid)
        (self.outbox / f"{jid}.artifacts" / "dll-pair-manifest.json").write_text(json.dumps({"sourceCommit": SHA, "jobId": base}), encoding="utf-8")
        r = self.reuse(base, "r2")
        self.assertFalse(r["Reusable"])
        self.assertTrue(r["Reason"].startswith("manifest-jobId-mismatch"), r)

    def test_a_bad_suffix_is_refused(self) -> None:
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        for bad in ("..\\x", "r--", "r-"):
            with self.subTest(bad=bad):
                self.assertIn("DLLPAIR_JOBID_SUFFIX_INVALID", self.reuse(base, bad).get("threw", ""))


if __name__ == "__main__":
    unittest.main()
