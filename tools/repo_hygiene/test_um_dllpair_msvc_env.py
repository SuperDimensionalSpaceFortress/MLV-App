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

    def test_the_incident_include_is_refused_as_lacking_ucrt(self) -> None:
        v = self.verdict(INCLUDE_NO_SDK)
        self.assertFalse(v["Ok"])
        self.assertEqual(v["Reason"], "INCLUDE_LACKS_UCRT")
        self.assertIn("entries=2", v["Detail"])

    def test_a_full_include_passes(self) -> None:
        v = self.verdict(INCLUDE_FULL)
        self.assertTrue(v["Ok"], v)
        self.assertEqual(v["Reason"], "")

    def test_ucrt_without_um_or_shared_is_refused_by_name(self) -> None:
        self.assertEqual(self.verdict(INCLUDE_NO_SDK + f";{KITS}\\ucrt")["Reason"], "INCLUDE_LACKS_UM")
        self.assertEqual(self.verdict(INCLUDE_NO_SDK + f";{KITS}\\ucrt;{KITS}\\um")["Reason"], "INCLUDE_LACKS_SHARED")

    def test_an_empty_include_is_refused(self) -> None:
        self.assertEqual(self.verdict("")["Reason"], "INCLUDE_EMPTY")
        self.assertEqual(self.verdict(" ; ;")["Reason"], "INCLUDE_EMPTY")

    def test_ucrt_must_be_a_whole_path_segment(self) -> None:
        # a dir merely CONTAINING the letters (\ucrtx, \myucrt) is not the SDK's ucrt include dir
        v = self.verdict(INCLUDE_NO_SDK + r";C:\x\notucrt;C:\x\ucrtx;" + f"{KITS}\\um;{KITS}\\shared")
        self.assertEqual(v["Reason"], "INCLUDE_LACKS_UCRT")


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

    def test_the_minimal_path_entries_are_derived_never_a_hosts_user_dirs(self) -> None:
        text, _ = self.job_text()
        line = [l for l in text.splitlines() if "$env:PATH = Get-AttrCudaMinimalBuildPath" in l]
        self.assertEqual(len(line), 1, line)
        self.assertIn("-SystemRoot ([Environment]::GetEnvironmentVariable('SystemRoot'))", line[0])
        self.assertIn("$cudaBin", line[0])
        for leak in ("Users\\", ".dotnet", "obabalola", "AppData"):
            self.assertNotIn(leak, line[0])


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
        for bad in ("r2;x", "r 2", "../r2", "r" * 33, "-r2"):
            with self.subTest(bad=bad):
                self.assertIn("DLLPAIR_JOBID_SUFFIX_INVALID", self.ids(bad).get("threw", ""), bad)

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
        self.assertIn("DLLPAIR_JOBID_SUFFIX_INVALID", self.reuse(base, "..\\x").get("threw", ""))


if __name__ == "__main__":
    unittest.main()
