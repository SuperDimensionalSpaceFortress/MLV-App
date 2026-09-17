"""Behavioural tests for the PLAYBACK-ATTR-3-CUDA split-build route: these EXECUTE pwsh.

WHY THIS FILE EXISTS. Its sibling test_playback_attr_3_cuda_split_route.py asserts what the
generators' text SAYS. That is worth having -- the sm_86 target and the absence of MSVC tooling
on the measurement host are text properties -- but sol's r2 review on PR #133 landed four
blockers that a text-level suite was green against, because "the script mentions a hash" and
"the script refuses the wrong bytes" are different claims. Everything here runs the real code
against real directories and asserts on exit codes and on what ended up on disk.

WHAT IS AND IS NOT EXERCISED. The verification logic lives in
tools/profiling/bachelor/AttrCudaArtifacts.psm1 and is spliced VERBATIM into each emitted job by
Get-AttrCudaEmbeddedFunctionSource, so a test that imports the module runs the same characters
the job runs on Bachelor or Ultra-Magnus. What cannot run here is the compiling and measuring --
no CUDA toolkit, no Qt/MinGW, no 4090, no measurement laptop -- so the jobs are exercised through
their `-VerifyOnly` prefix and through the staging job end to end, which touches only files.

SKIPS. Everything is skipped cleanly when pwsh (or git, for the fixture repositories) is absent,
and on non-Windows platforms (the jobs validate drive-letter paths); CI Windows runs it all.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
STAGE_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1"
DLL_GENERATOR = ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1"

PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")
requires_git = unittest.skipIf(GIT is None, "git is not on PATH")


def _long_path(path: Path) -> Path:
    """Canonicalize to the long-path form.

    tempfile.mkdtemp() can hand back an 8.3 short name (OBABAL~1) when the user profile
    directory has one, but pwsh subprocesses print the long form in their stdout/exception
    text -- so string assertions against a short-named test root never match. realpath()
    resolves 8.3 segments on this host; GetLongPathNameW is the fallback if it ever doesn't.
    """
    resolved = Path(os.path.realpath(path))
    if "~" not in str(resolved):
        return resolved
    buf = ctypes.create_unicode_buffer(4096)
    if ctypes.windll.kernel32.GetLongPathNameW(str(resolved), buf, len(buf)):
        return Path(buf.value)
    return resolved


def _run_pwsh_file(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True,
        text=True,
    )


def _run_job(job: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(job), *args],
        capture_output=True,
        text=True,
    )


class _PwshCase(unittest.TestCase):
    """Base: a temp directory per test, and a way to run a snippet against the module."""

    def setUp(self) -> None:
        # The emitted jobs run only on Windows hosts and their parameters validate drive-letter
        # paths (e.g. -AgentRoot), so a /tmp root on Linux fails parameter validation rather than
        # exercising the logic. Windows CI runs this suite; other platforms skip it.
        if os.name != "nt":
            self.skipTest("the ATTR-3 host jobs are Windows-only (drive-letter path parameters)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def run_with_module(self, body: str, name: str = "probe.ps1") -> subprocess.CompletedProcess:
        """Run `body` with AttrCudaArtifacts.psm1 imported.

        Imported, not re-implemented: the emitted jobs carry these same function bodies verbatim,
        so a pass here is a statement about the code that runs unattended on another machine.
        """
        script = self.tmp / name
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n" + body,
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def assert_throws(self, proc: subprocess.CompletedProcess, token: str) -> None:
        self.assertIn(
            f"THREW {token}",
            proc.stdout,
            f"expected {token}; stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )


def _guard(call: str) -> str:
    """A snippet that prints THREW <message> or NO_THROW, so a refusal is assertable."""
    return (
        "try { [void](" + call + "); Write-Output 'NO_THROW' } "
        "catch { Write-Output ('THREW ' + $_.Exception.Message) }\n"
    )


def _git_run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        [GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _make_fixture_repo(path: Path) -> list[str]:
    """A two-commit throwaway repository, so `git archive` is instant.

    The real generators archive the whole MLV-App tree, which is far too slow to do per test and
    proves nothing extra: what is under test is the BINDING between a job and an archive, not the
    contents of either.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git_run(["init", "-q", "-b", "main"], path)
    # Local to the fixture, never to the user's repo: an unattended signing prompt would hang.
    _git_run(["config", "commit.gpgsign", "false"], path)
    _git_run(["config", "user.email", "lane@example.invalid"], path)
    _git_run(["config", "user.name", "attr3 behaviour fixture"], path)
    (path / "src" / "mlv" / "llrawproc").mkdir(parents=True)
    (path / "tools" / "gpu" / "backend").mkdir(parents=True)
    shas = []
    for index, text in enumerate(("first", "second")):
        (path / "src" / "mlv" / "llrawproc" / "llrawproc.c").write_text(
            f"/* fixture revision {text} */\n", encoding="utf-8"
        )
        _git_run(["add", "-A"], path)
        _git_run(["commit", "-q", "-m", f"fixture {index}"], path)
        shas.append(_git_run(["rev-parse", "HEAD"], path))
    return shas


# --------------------------------------------------------------------------------------------
# (a) source archive binding
# --------------------------------------------------------------------------------------------


@requires_pwsh
@requires_git
class SourceArchiveBindingTests(_PwshCase):
    """A job generated for zip A must refuse zip B, before it expands anything."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.agent = self.tmp / "agent"
        (self.agent / "inbox").mkdir(parents=True)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def _generate(self, sha: str) -> dict:
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{DLL_GENERATOR}' -SourceCommit '{sha}' -OutDir '{self.staging}' "
            f"-RepoRoot '{self.repo}' -AgentRoot '{self.agent}' | ConvertTo-Json -Depth 5\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"generator failed: {proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    def test_the_generated_job_accepts_its_own_archive_and_writes_nothing(self) -> None:
        generated = self._generate(self.shas[1])
        inbox = self.agent / "inbox"
        shutil.copy2(generated["sourceArchive"], inbox / Path(generated["sourceArchive"]).name)

        proc = _run_job(Path(generated["jobFile"]), "-VerifyOnly")

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=VERIFY_ONLY_OK", proc.stdout)
        self.assertIn(generated["sourceArchiveSha256"], proc.stdout)
        # -VerifyOnly is a read: no work dir, no outbox, nothing but the inbox it was handed.
        self.assertEqual(
            sorted(entry.name for entry in self.agent.iterdir()),
            ["inbox"],
            "the verification prefix wrote something",
        )

    def test_a_job_generated_for_one_archive_refuses_another(self) -> None:
        generated = self._generate(self.shas[1])
        archive_name = Path(generated["sourceArchive"]).name
        # Same file NAME, different bytes: exactly the substitution the binding exists to catch.
        _git_run(
            ["archive", "--format=zip", "-o", str(self.agent / "inbox" / archive_name), self.shas[0]],
            self.repo,
        )

        proc = _run_job(Path(generated["jobFile"]), "-VerifyOnly")

        self.assertEqual(proc.returncode, 8, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("ATTRCUDA_ARCHIVE_SHA_MISMATCH", proc.stdout)
        self.assertIn("STEP=sourceArchiveBinding", proc.stdout)
        self.assertEqual(sorted(entry.name for entry in self.agent.iterdir()), ["inbox"])

    def test_a_missing_archive_is_refused_before_anything_is_created(self) -> None:
        generated = self._generate(self.shas[1])

        proc = _run_job(Path(generated["jobFile"]), "-VerifyOnly")

        self.assertEqual(proc.returncode, 3, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(sorted(entry.name for entry in self.agent.iterdir()), ["inbox"])

    def test_an_archive_of_another_commit_is_refused_even_when_the_hash_is_told_to_match(self) -> None:
        # The sha256 leg cannot catch this on its own -- whoever swapped the archive would simply
        # re-hash it. The zip comment git stamps in is what makes the commit claim checkable.
        archive = self.tmp / "other.zip"
        _git_run(["archive", "--format=zip", "-o", str(archive), self.shas[0]], self.repo)
        proc = self.run_with_module(
            f"$sha = (Get-FileHash -LiteralPath '{archive}' -Algorithm SHA256).Hash.ToLowerInvariant()\n"
            + _guard(
                f"Assert-AttrCudaSourceArchive -ArchivePath '{archive}' -ExpectedSha256 $sha "
                f"-ExpectedCommit '{self.shas[1]}'"
            )
            + f"Write-Output ('comment=' + (Get-AttrCudaZipArchiveComment -Path '{archive}'))\n"
        )
        self.assert_throws(proc, "ATTRCUDA_ARCHIVE_COMMIT_MISMATCH")
        self.assertIn(f"comment={self.shas[0]}", proc.stdout)


# --------------------------------------------------------------------------------------------
# (b) staging name safety and traversal
# --------------------------------------------------------------------------------------------


def _fake_build_dir(build: Path, sha: str, exe_name_override: str | None = None) -> dict:
    """Write the four files the assembler publishes, with fabricated contents."""
    short = sha[:12]
    names = {
        "exe": f"MLVApp-playback-attr-3-cuda-{short}.exe",
        "dll": f"igpu_recon_cuda-playback-attr-3-cuda-{short}.dll",
        "packageZip": f"MLVApp-playback-attr-3-cuda-{short}-pkg.zip",
        "manifest": f"playback-attr-3-cuda-{short}-build.json",
    }
    build.mkdir(parents=True, exist_ok=True)
    import hashlib

    shas = {}
    for key in ("exe", "dll", "packageZip"):
        payload = f"fabricated {key} for {sha}".encode("utf-8")
        (build / names[key]).write_bytes(payload)
        shas[key] = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema": "mlvapp.playback-attr-3-cuda-build-cache-manifest.v1",
        "sourceCommit": sha,
        "exe": {"name": exe_name_override or names["exe"], "sha256": shas["exe"]},
        "dll": {"name": names["dll"], "sha256": shas["dll"]},
        "packageZip": {"name": names["packageZip"], "sha256": shas["packageZip"]},
        "pendingSymbolPresence": True,
        "dllPairManifestSha256": "b" * 64,
    }
    (build / names["manifest"]).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return names


SHA_FIXTURE = "d4bdd335f793f8c9da914b75fb01671373d6ae27"


@requires_pwsh
class StagingNameSafetyTests(_PwshCase):
    """Traversal and non-canonical names are refused, and nothing is written outside the root."""

    def setUp(self) -> None:
        super().setUp()
        self.build = self.tmp / "build"
        self.staging = self.tmp / "staging"
        self.staging.mkdir(parents=True)
        self.agent = self.tmp / "agent"
        (self.agent / "inbox").mkdir(parents=True)
        (self.agent / "cache").mkdir(parents=True)
        self.names = _fake_build_dir(self.build, SHA_FIXTURE)

    def generate(self) -> dict:
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{STAGE_GENERATOR}' -SourceCommit '{SHA_FIXTURE}' -BuildDir '{self.build}' "
            f"-OutDir '{self.staging}' -AgentRoot '{self.agent}' | ConvertTo-Json -Depth 5\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    def drop_side_files(self) -> None:
        for name in self.names.values():
            shutil.copy2(self.build / name, self.agent / "inbox" / name)

    def test_a_clean_drop_verifies_without_writing_into_the_cache(self) -> None:
        generated = self.generate()
        self.drop_side_files()

        proc = _run_job(Path(generated["jobFile"]), "-VerifyOnly")

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=VERIFY_ONLY_OK", proc.stdout)
        self.assertEqual(list((self.agent / "cache").iterdir()), [])
        self.assertEqual(len(list((self.agent / "inbox").iterdir())), 4)

    def _tamper_baked_name(self, job: Path, replacement: str, name: str) -> Path:
        tampered = self.staging / name
        tampered.write_text(
            job.read_text(encoding="utf-8").replace(
                f"'{self.names['exe']}'", f"'{replacement}'"
            ),
            encoding="utf-8",
        )
        return tampered

    def test_a_traversal_name_baked_into_the_job_is_refused_and_escapes_nothing(self) -> None:
        # The guard has to live in the JOB, not only in the generator: the job is the thing that
        # runs unattended, and this is the finding -- Join-Path over a `..` name reached outside
        # a root whose own containment check had passed.
        generated = self.generate()
        self.drop_side_files()
        escaped = self.tmp / "ESCAPED.txt"
        tampered = self._tamper_baked_name(
            Path(generated["jobFile"]), r"..\..\ESCAPED.txt", "traversal.job.ps1"
        )

        proc = _run_job(tampered, "-VerifyOnly")

        self.assertEqual(proc.returncode, 6, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("STEP=artifactNameSafety", proc.stdout)
        self.assertFalse(escaped.exists(), "a path escaped the agent root")
        self.assertEqual(list((self.agent / "cache").iterdir()), [])

    def test_a_non_canonical_name_baked_into_the_job_is_refused(self) -> None:
        generated = self.generate()
        self.drop_side_files()
        tampered = self._tamper_baked_name(
            Path(generated["jobFile"]), "MLVApp-somethingelse.exe", "noncanonical.job.ps1"
        )

        proc = _run_job(tampered, "-VerifyOnly")

        self.assertEqual(proc.returncode, 6, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("canonical names", proc.stdout)
        self.assertEqual(list((self.agent / "cache").iterdir()), [])

    def test_the_generator_refuses_a_manifest_that_names_a_traversal(self) -> None:
        _fake_build_dir(self.build, SHA_FIXTURE, exe_name_override=r"..\..\evil.exe")
        script = self.tmp / "generate-evil.ps1"
        script.write_text(
            f"& '{STAGE_GENERATOR}' -SourceCommit '{SHA_FIXTURE}' -BuildDir '{self.build}' "
            f"-OutDir '{self.staging}' -AgentRoot '{self.agent}'\n",
            encoding="utf-8",
        )

        proc = _run_pwsh_file(script)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the canonical name for", proc.stderr + proc.stdout)
        self.assertEqual(list(self.staging.iterdir()), [], "a job was emitted for a bad manifest")

    def test_the_shared_guard_rejects_every_shape_of_unsafe_name(self) -> None:
        cases = {
            r"..\..\x.exe": "ATTRCUDA_NAME_TRAVERSAL",
            "sub/dir.exe": "ATTRCUDA_NAME_NOT_A_BASENAME",
            r"sub\dir.exe": "ATTRCUDA_NAME_NOT_A_BASENAME",
            "C:x.exe": "ATTRCUDA_NAME_NOT_A_BASENAME",
            "name.exe:stream": "ATTRCUDA_NAME_NOT_A_BASENAME",
            " padded.exe": "ATTRCUDA_NAME_PADDED",
            "": "ATTRCUDA_NAME_EMPTY",
        }
        for name, token in cases.items():
            with self.subTest(name=name):
                proc = self.run_with_module(
                    _guard(f"Assert-AttrCudaSafeArtifactName -Name '{name}'"),
                    name="name-guard.ps1",
                )
                self.assert_throws(proc, token)

    def test_the_shared_guard_requires_a_direct_child(self) -> None:
        root = self.agent / "cache"
        nested = root / "deeper"
        nested.mkdir()
        proc = self.run_with_module(
            _guard(f"Assert-AttrCudaDirectChild -Root '{root}' -Path '{nested / 'x.exe'}'")
            + _guard(f"Assert-AttrCudaDirectChild -Root '{root}' -Path '{root}\\..\\x.exe'")
            + f"Write-Output (Assert-AttrCudaDirectChild -Root '{root}' -Path '{root}\\ok.exe')\n",
            name="child-guard.ps1",
        )
        self.assertEqual(proc.stdout.count("THREW ATTRCUDA_PATH_NOT_DIRECT_CHILD"), 2, proc.stdout)
        self.assertIn(str(root / "ok.exe"), proc.stdout)


# --------------------------------------------------------------------------------------------
# (c) build.json authentication
# --------------------------------------------------------------------------------------------


@requires_pwsh
class BuildManifestAuthenticationTests(_PwshCase):
    """The attribution job's manifest check, run directly against fabricated manifests."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest_path = self.tmp / "build.json"

    def write(self, **overrides) -> str:
        import hashlib

        manifest = {
            "schema": "mlvapp.playback-attr-3-cuda-build-cache-manifest.v1",
            "sourceCommit": SHA_FIXTURE,
            "exe": {"name": "x.exe", "sha256": "c" * 64},
            "pendingSymbolPresence": True,
            "dllPairManifestSha256": "d" * 64,
        }
        manifest.update(overrides)
        payload = json.dumps(manifest, indent=2).encode("utf-8")
        self.manifest_path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def call(self, expected_sha: str, commit: str = SHA_FIXTURE) -> subprocess.CompletedProcess:
        return self.run_with_module(
            _guard(
                f"Assert-AttrCudaBuildManifest -Path '{self.manifest_path}' "
                f"-ExpectedSha256 '{expected_sha}' -ExpectedSourceCommit '{commit}'"
            )
        )

    def test_a_manifest_matching_the_baked_sha_is_accepted(self) -> None:
        proc = self.call(self.write())
        self.assertIn("NO_THROW", proc.stdout, proc.stdout + proc.stderr)

    def test_a_substituted_manifest_is_refused(self) -> None:
        # The forgery the finding describes: replace build.json (and its artifacts) with a
        # self-consistent set. Self-consistency is exactly what the baked sha does not care about.
        self.write()
        self.write(pendingSymbolPresence=False, exe={"name": "x.exe", "sha256": "e" * 64})
        proc = self.call("f" * 64)
        self.assert_throws(proc, "ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH")

    def test_a_manifest_for_another_commit_is_refused(self) -> None:
        sha = self.write(sourceCommit="0" * 40)
        self.assert_throws(self.call(sha), "ATTRCUDA_BUILD_MANIFEST_COMMIT_MISMATCH")

    def test_a_manifest_that_does_not_chain_to_the_dll_pair_is_refused(self) -> None:
        payload = json.dumps(
            {
                "sourceCommit": SHA_FIXTURE,
                "pendingSymbolPresence": True,
            },
            indent=2,
        ).encode("utf-8")
        self.manifest_path.write_bytes(payload)
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        self.assert_throws(self.call(sha), "ATTRCUDA_BUILD_MANIFEST_DLLPAIR_UNBOUND")

    def test_a_non_boolean_pending_symbol_presence_is_refused(self) -> None:
        sha = self.write(pendingSymbolPresence="true")
        self.assert_throws(self.call(sha), "ATTRCUDA_BUILD_MANIFEST_SYMBOL_PRESENCE_INVALID")

    def test_a_missing_manifest_is_refused(self) -> None:
        sha = self.write()
        self.manifest_path.unlink()
        self.assert_throws(self.call(sha), "ATTRCUDA_BUILD_MANIFEST_MISSING")

    def test_the_hash_is_checked_before_the_manifest_is_parsed(self) -> None:
        # Unparseable JSON with the right sha reaches the parse; the same bytes with a wrong sha
        # must fail at the hash instead -- which is the ordering the finding turns on.
        payload = b"{ this is not json"
        self.manifest_path.write_bytes(payload)
        import hashlib

        sha = hashlib.sha256(payload).hexdigest()
        self.assertNotIn(
            "ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH", self.call(sha).stdout
        )
        self.assert_throws(self.call("a" * 64), "ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH")


# --------------------------------------------------------------------------------------------
# (d) the smoke-runner log contract
# --------------------------------------------------------------------------------------------


ELIGIBILITY = (
    "[2026-09-16T23:00:{second:02d}.100Z] [I] [0x1] gpu_playback_recon.eligibility "
    "viewport_widget=1 gl_window=1 cuda_backend_attempted=1 cuda_backend_resolved={resolved} "
    "cuda_backend_available={cuda} r16_probe_ran=1 r16_available={r16} r16_reason=\"{reason}\""
)


@requires_pwsh
class SmokeRunLogSelectorTests(_PwshCase):
    """result.json log.path is the authority; a glob over logs\\mlvapp-*.log matches nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.work = self.tmp / "work"
        self.diagnostic = self.work / "out" / "diagnostic"
        self.diagnostic.mkdir(parents=True)
        # The shape run-release-gui-smoke.ps1 actually produces: a GUID-nonced log directory
        # NEXT TO the result, and the per-run snapshot as "<output>.run.log". The directory the
        # old code globbed -- out\diagnostic\logs -- is deliberately never created.
        self.log_root = self.diagnostic / "logs-result-0123456789abcdef0123456789abcdef"
        self.log_root.mkdir()
        (self.log_root / "mlvapp-20260916.log").write_text("aggregate\n", encoding="utf-8")
        self.result_json = self.diagnostic / "result.json"
        self.run_log = self.diagnostic / "result.json.run.log"

    def write_run(self, cuda: str, r16: str, reason: str = "ok", lines: int = 2) -> None:
        import hashlib

        body = "".join(
            ELIGIBILITY.format(second=index, resolved=1, cuda=cuda, r16=r16, reason=reason) + "\n"
            for index in range(lines)
        )
        self.run_log.write_text(body, encoding="utf-8")
        digest = hashlib.sha256(self.run_log.read_bytes()).hexdigest().upper()
        self.result_json.write_text(
            json.dumps(
                {
                    "log": {
                        "path": str(self.run_log),
                        "aggregateSourcePath": str(self.log_root / "mlvapp-20260916.log"),
                    },
                    "evidence": {
                        "runNonce": "0123456789abcdef0123456789abcdef",
                        "runLogSnapshot": {
                            "path": str(self.run_log),
                            "length": self.run_log.stat().st_size,
                            "sha256": digest,
                        },
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def resolve_and_judge(self) -> subprocess.CompletedProcess:
        return self.run_with_module(
            f"$runLog = Resolve-AttrCudaSmokeRunLog -ResultJsonPath '{self.result_json}' "
            f"-ContainingRoot '{self.work}'\n"
            "Write-Output ('log=' + $runLog.path)\n"
            "Write-Output ('aggregate=' + $runLog.aggregateSourcePath)\n"
            "$verdict = Get-AttrCudaEligibilityVerdict -LogText ([IO.File]::ReadAllText($runLog.path))\n"
            "Write-Output ('admitted=' + $verdict.admitted + ' exitCode=' + $verdict.exitCode + "
            "' linePresent=' + $verdict.linePresent + ' reason=' + $verdict.r16Reason)\n"
        )

    def test_the_selector_reads_the_snapshot_named_by_the_result(self) -> None:
        self.write_run(cuda="1", r16="1")
        proc = self.resolve_and_judge()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"log={self.run_log}", proc.stdout)
        self.assertIn("admitted=True exitCode=0", proc.stdout)
        # The aggregate log is reported, never read: it may grow after the run.
        self.assertIn("mlvapp-20260916.log", proc.stdout)

    def test_a_backend_that_never_loaded_is_gated_as_exit_15(self) -> None:
        self.write_run(cuda="0", r16="1", reason="backend dll did not load")
        proc = self.resolve_and_judge()
        self.assertIn("admitted=False exitCode=15", proc.stdout)
        self.assertIn("reason=backend dll did not load", proc.stdout)

    def test_an_unadmitted_r16_path_is_gated_as_exit_15(self) -> None:
        self.write_run(cuda="1", r16="0", reason="R16 texture probe refused")
        self.assertIn("admitted=False exitCode=15", self.resolve_and_judge().stdout)

    def test_a_log_with_no_eligibility_line_is_gated_as_exit_15(self) -> None:
        # Absence of the diagnostic is not evidence of eligibility.
        self.write_run(cuda="1", r16="1", lines=0)
        proc = self.resolve_and_judge()
        self.assertIn("admitted=False exitCode=15", proc.stdout)
        self.assertIn("linePresent=False", proc.stdout)

    def test_a_missing_log_is_refused(self) -> None:
        self.write_run(cuda="1", r16="1")
        self.run_log.unlink()
        proc = self.run_with_module(
            _guard(f"Resolve-AttrCudaSmokeRunLog -ResultJsonPath '{self.result_json}'")
        )
        self.assert_throws(proc, "ATTRCUDA_SMOKE_LOG_MISSING")

    def test_a_log_changed_after_the_run_is_refused(self) -> None:
        self.write_run(cuda="1", r16="1")
        with self.run_log.open("a", encoding="utf-8") as handle:
            handle.write(
                ELIGIBILITY.format(second=59, resolved=1, cuda=1, r16=1, reason="forged") + "\n"
            )
        proc = self.run_with_module(
            _guard(f"Resolve-AttrCudaSmokeRunLog -ResultJsonPath '{self.result_json}'")
        )
        self.assert_throws(proc, "ATTRCUDA_SMOKE_LOG_SHA_MISMATCH")

    def _rewrite_declared_length(self, value) -> None:
        data = json.loads(self.result_json.read_text(encoding="utf-8"))
        if value is None:
            del data["evidence"]["runLogSnapshot"]["length"]
        else:
            data["evidence"]["runLogSnapshot"]["length"] = value
        self.result_json.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_a_declared_length_that_disagrees_with_the_log_is_refused(self) -> None:
        # sol PR #133 r3: the runner binds the snapshot by sha256 AND length; the length is checked.
        self.write_run(cuda="1", r16="1")
        self._rewrite_declared_length(self.run_log.stat().st_size + 1)
        proc = self.run_with_module(
            _guard(f"Resolve-AttrCudaSmokeRunLog -ResultJsonPath '{self.result_json}'")
        )
        self.assert_throws(proc, "ATTRCUDA_SMOKE_LOG_LENGTH_MISMATCH")

    def test_a_result_with_no_declared_length_is_refused(self) -> None:
        self.write_run(cuda="1", r16="1")
        self._rewrite_declared_length(None)
        proc = self.run_with_module(
            _guard(f"Resolve-AttrCudaSmokeRunLog -ResultJsonPath '{self.result_json}'")
        )
        self.assert_throws(proc, "ATTRCUDA_SMOKE_LOG_LENGTH_UNBOUND")

    def test_a_result_declaring_no_log_path_is_refused(self) -> None:
        self.result_json.write_text(json.dumps({"log": {"aggregateSourcePath": None}}), encoding="utf-8")
        proc = self.run_with_module(
            _guard(f"Resolve-AttrCudaSmokeRunLog -ResultJsonPath '{self.result_json}'")
        )
        self.assert_throws(proc, "ATTRCUDA_SMOKE_LOG_PATH_ABSENT")

    def test_the_directory_the_old_code_globbed_does_not_exist(self) -> None:
        # Not a tautology about this fixture: the fixture mirrors the runner, and the runner's
        # log directory name carries a per-run GUID.
        self.assertFalse((self.diagnostic / "logs").exists())
        self.assertTrue(self.log_root.name.startswith("logs-result-"))


# --------------------------------------------------------------------------------------------
# (e) an interrupted stage never publishes build.json
# --------------------------------------------------------------------------------------------


@requires_pwsh
class InterruptedStagingTests(_PwshCase):
    """build.json is the completion marker; a run that does not finish must not leave one."""

    def setUp(self) -> None:
        super().setUp()
        self.build = self.tmp / "build"
        self.staging = self.tmp / "staging"
        self.staging.mkdir(parents=True)
        self.agent = self.tmp / "agent"
        self.inbox = self.agent / "inbox"
        self.cache = self.agent / "cache"
        self.inbox.mkdir(parents=True)
        self.cache.mkdir(parents=True)
        self.names = _fake_build_dir(self.build, SHA_FIXTURE)
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{STAGE_GENERATOR}' -SourceCommit '{SHA_FIXTURE}' -BuildDir '{self.build}' "
            f"-OutDir '{self.staging}' -AgentRoot '{self.agent}' | ConvertTo-Json -Depth 5\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.job = Path(json.loads(proc.stdout)["jobFile"])

    def drop(self, *, skip: str | None = None) -> None:
        for key, name in self.names.items():
            if key == skip:
                continue
            shutil.copy2(self.build / name, self.inbox / name)

    def cache_names(self) -> list[str]:
        return sorted(entry.name for entry in self.cache.iterdir())

    def test_a_leftover_partial_is_never_promoted_by_a_run_that_stops_early(self) -> None:
        stale = self.cache / f"{self.names['exe']}.partial"
        stale.write_bytes(b"leftover from a run that died mid-publish")
        self.drop(skip="manifest")

        proc = _run_job(self.job)

        self.assertEqual(proc.returncode, 3, f"{proc.stdout}\n{proc.stderr}")
        self.assertNotIn(self.names["manifest"], self.cache_names())
        self.assertNotIn(self.names["exe"], self.cache_names())
        self.assertEqual(stale.read_bytes(), b"leftover from a run that died mid-publish")

    def test_a_failure_during_the_manifest_publish_leaves_no_build_json(self) -> None:
        # Force the last step to fail by occupying its .partial path with a directory: the copy
        # lands inside it and the hash of a directory is an error. This is the interruption the
        # transactional order exists for -- the artifacts are already renamed into place.
        (self.cache / f"{self.names['manifest']}.partial").mkdir()
        self.drop()

        proc = _run_job(self.job)

        self.assertEqual(proc.returncode, 22, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("STEP=publishManifest", proc.stdout)
        self.assertNotIn(
            self.names["manifest"],
            self.cache_names(),
            "build.json was published by a run that failed",
        )
        # The side-files are still in the inbox, so the stage is simply re-runnable.
        self.assertIn(self.names["manifest"], sorted(e.name for e in self.inbox.iterdir()))

    def test_a_complete_run_publishes_all_four_with_build_json_present(self) -> None:
        self.drop()

        proc = _run_job(self.job)

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(self.cache_names(), sorted(self.names.values()))
        self.assertEqual(list(self.inbox.iterdir()), [], "side-files were left in the inbox")

    def test_a_cache_partial_that_is_a_junction_receives_no_write(self) -> None:
        # sol PR #133 r3: Copy-Item onto a junction .partial writes into the link target.
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.cache / f"{self.names['exe']}.partial"
        proc = self.run_with_module(
            f"New-Item -ItemType Junction -Path '{link}' -Target '{outside}' | Out-Null\n"
        )
        if proc.returncode != 0 or not link.exists():
            self.skipTest(f"cannot create a junction here: {proc.stderr}")
        self.drop()

        proc = _run_job(self.job)

        self.assertNotEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(list(outside.iterdir()), [], "the stage job wrote through a junction")
        self.assertNotIn(self.names["manifest"], self.cache_names())

    def test_a_tampered_side_file_publishes_nothing(self) -> None:
        self.drop()
        (self.inbox / self.names["exe"]).write_bytes(b"swapped after the hashes were baked")

        proc = _run_job(self.job)

        self.assertEqual(proc.returncode, 4, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(self.cache_names(), [])


# --------------------------------------------------------------------------------------------
# cleanup never follows a link out of the job root (sol PR #133 r3)
# --------------------------------------------------------------------------------------------


@requires_pwsh
class LinkSafeCleanupTests(_PwshCase):
    """A junction planted inside a .partial or a work tree must never lead a delete outside it."""

    def setUp(self) -> None:
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.sentinel = self.outside / "precious.txt"
        self.sentinel.write_bytes(b"must survive")
        self.root = self.tmp / "jobroot"
        self.root.mkdir()

    def plant_junction(self, parent: Path, name: str = "link") -> Path:
        link = parent / name
        proc = self.run_with_module(
            f"New-Item -ItemType Junction -Path '{link}' -Target '{self.outside}' | Out-Null\n"
        )
        if proc.returncode != 0 or not link.exists():
            self.skipTest(f"cannot create a junction here: {proc.stderr}")
        return link

    def test_a_partial_occupied_by_a_directory_holding_a_junction_is_left_and_nothing_outside_is_deleted(self) -> None:
        partial = self.root / "build.json.partial"
        partial.mkdir()
        self.plant_junction(partial)
        proc = self.run_with_module(
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{partial}'))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("removed=False", proc.stdout)
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")
        self.assertTrue(partial.exists())

    def test_a_partial_that_is_itself_a_junction_is_left_and_its_target_survives(self) -> None:
        link = self.plant_junction(self.root, "build.json.partial")
        proc = self.run_with_module(
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{link}'))\n"
        )
        self.assertIn("removed=False", proc.stdout)
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")

    def test_a_plain_partial_file_is_removed(self) -> None:
        partial = self.root / "exe.partial"
        partial.write_bytes(b"half written")
        proc = self.run_with_module(
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{partial}'))\n"
        )
        self.assertIn("removed=True", proc.stdout)
        self.assertFalse(partial.exists())

    def test_a_work_tree_containing_a_junction_is_refused_whole(self) -> None:
        work = self.root / "work"
        (work / "nested").mkdir(parents=True)
        (work / "nested" / "file.txt").write_bytes(b"x")
        self.plant_junction(work / "nested")
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{work}'"))
        self.assert_throws(proc, "ATTRCUDA_TREE_HAS_REPARSE_POINT")
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")
        self.assertTrue((work / "nested" / "file.txt").exists(), "a refused tree must be left intact")

    def test_a_tree_reached_through_a_linked_ancestor_is_refused_and_the_target_survives(self) -> None:
        # sol PR #133 r6: AgentRoot\outbox is a junction to an outside directory holding <job>.artifacts.
        (self.outside / "job.artifacts").mkdir()
        (self.outside / "job.artifacts" / "keep.txt").write_bytes(b"outside the agent root")
        self.plant_junction(self.root, "outbox")
        target = self.root / "outbox" / "job.artifacts"
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{target}'"))
        self.assert_throws(proc, "ATTRCUDA_ANCESTOR_IS_LINK")
        self.assertEqual((self.outside / "job.artifacts" / "keep.txt").read_bytes(), b"outside the agent root")

    def test_an_absent_tree_under_a_linked_ancestor_is_still_refused(self) -> None:
        self.plant_junction(self.root, "work")
        missing = self.root / "work" / "never-created"
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{missing}'"))
        self.assert_throws(proc, "ATTRCUDA_ANCESTOR_IS_LINK")

    def test_a_path_outside_the_trusted_root_is_refused(self) -> None:
        escaping = str(self.root) + "\\..\\outside"
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{escaping}'"))
        self.assert_throws(proc, "ATTRCUDA_PATH_NOT_UNDER_ROOT")
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")

    def test_every_module_helper_call_names_all_mandatory_parameters(self) -> None:
        # sol PR #133 r8: the assembler called Remove-AttrCudaTree without the newly mandatory
        # -TrustedRoot and would have failed before compiling; nothing executed that call site.
        scripts = [
            ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1",
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1",
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1",
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1",
        ]
        quoted = ",".join(f"'{s}'" for s in scripts)
        proc = self.run_with_module(
            "$mandatory = @{}\n"
            "foreach ($c in Get-Command -Module AttrCudaArtifacts) {\n"
            "    $mandatory[$c.Name] = @($c.Parameters.Values | Where-Object { $_.Attributes | Where-Object { $_ -is [System.Management.Automation.ParameterAttribute] -and $_.Mandatory } } | ForEach-Object { $_.Name })\n"
            "}\n"
            f"foreach ($file in @({quoted})) {{\n"
            "    $tokens = $null; $errors = $null\n"
            "    $text = [regex]::Replace([IO.File]::ReadAllText($file), '__[A-Z0-9_]+__', '$attrCudaPlaceholder')\n"
            "    $ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)\n"
            "    foreach ($call in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {\n"
            "        $name = $call.GetCommandName()\n"
            "        if (-not $name -or -not $mandatory.ContainsKey($name)) { continue }\n"
            "        $given = @($call.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } | ForEach-Object { $_.ParameterName })\n"
            "        foreach ($required in $mandatory[$name]) {\n"
            "            if (-not ($given | Where-Object { $required.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })) {\n"
            "                Write-Output ('MISSING ' + [IO.Path]::GetFileName($file) + ':' + $call.Extent.StartLineNumber + ' ' + $name + ' -' + $required)\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "}\n"
            "Write-Output 'SCAN_DONE'\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SCAN_DONE", proc.stdout)
        self.assertNotIn("MISSING", proc.stdout, proc.stdout)

    def test_a_plain_file_reached_through_a_linked_inbox_is_not_deleted(self) -> None:
        # sol PR #133 r8: AgentRoot\inbox is a junction; the side-file it resolves to is outside the root.
        (self.outside / "side.zip").write_bytes(b"outside the agent root")
        self.plant_junction(self.root, "inbox")
        side = self.root / "inbox" / "side.zip"
        proc = self.run_with_module(
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{side}'))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("removed=False", proc.stdout)
        self.assertEqual((self.outside / "side.zip").read_bytes(), b"outside the agent root")

    def test_a_link_free_work_tree_is_removed(self) -> None:
        work = self.root / "work"
        (work / "a" / "b").mkdir(parents=True)
        (work / "a" / "b" / "f.txt").write_bytes(b"x")
        proc = self.run_with_module(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{work}'\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(work.exists())

    def test_a_slot_that_is_a_junction_is_refused_before_anything_is_written(self) -> None:
        link = self.plant_junction(self.root, "exe.partial")
        proc = self.run_with_module(_guard(f"Assert-AttrCudaWritableFileSlot -Path '{link}'"))
        self.assert_throws(proc, "ATTRCUDA_SLOT_OCCUPIED")
        self.assertEqual(sorted(e.name for e in self.outside.iterdir()), ["precious.txt"])

    def test_a_slot_under_a_linked_parent_is_refused(self) -> None:
        link = self.plant_junction(self.root, "cache")
        proc = self.run_with_module(_guard(f"Assert-AttrCudaWritableFileSlot -Path '{link / 'x.partial'}'"))
        self.assert_throws(proc, "ATTRCUDA_SLOT_PARENT_IS_LINK")

    def test_a_plain_file_slot_is_cleared_and_an_absent_slot_is_accepted(self) -> None:
        stale = self.root / "exe.partial"
        stale.write_bytes(b"stale")
        proc = self.run_with_module(
            f"Assert-AttrCudaWritableFileSlot -Path '{stale}' | Out-Null\n"
            f"Assert-AttrCudaWritableFileSlot -Path '{self.root / 'absent.partial'}' | Out-Null\n"
            "Write-Output 'ok'\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(stale.exists())

    def test_every_embedded_function_is_defined_before_its_first_call_in_each_job(self) -> None:
        # sol PR #133 r3: the attribution job called Remove-AttrCudaTree above __EMBEDDED_FUNCTIONS__.
        import re

        jobs = {
            ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1": DLL_GENERATOR,
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1": STAGE_GENERATOR,
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1": None,
        }
        for script in jobs:
            text = script.read_text(encoding="utf-8").replace("\r\n", "\n")
            start = text.index("$template = @'")
            end = text.index("\n'@", start)
            template = text[start:end]
            placeholder = template.index("__EMBEDDED_FUNCTIONS__")
            names = re.findall(r"^\s*'((?:Get|Assert|Resolve|Remove)-AttrCuda\w+)',?\s*$", text[:start], re.M)
            self.assertTrue(names, f"no embedded function list found in {script.name}")
            for name in names:
                with self.subTest(script=script.name, function=name):
                    first_call = template.find(name)
                    if first_call != -1:
                        self.assertGreater(first_call, placeholder,
                                           f"{name} is called before it is embedded in {script.name}")

    def test_no_emitted_job_or_assembler_recurses_a_delete_outside_the_guarded_function(self) -> None:
        import re

        for script in (
            ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1",
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1",
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1",
            ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1",
        ):
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"Remove-Item[^\n]*-Recurse", text), script.name)


# --------------------------------------------------------------------------------------------
# regression tripwire: known-dangerous shapes in the emitted templates (not a soundness proof)
# --------------------------------------------------------------------------------------------

SCANNER = ROOT / "tools" / "repo_hygiene" / "attr3_publish_write_scan.ps1"
JOB_TEMPLATES = (
    ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1",
    ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1",
    ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1",
    # Every emitted job that runs unattended on a measurement host is scanned, including the
    # fixture stager: a template added without this line would run unscanned.
    ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-fixture-job.ps1",
)


@requires_pwsh
class PublishWriteScanTests(_PwshCase):
    """Tripwire over the templates; the runtime helpers, not this scan, are the safety boundary."""

    def scan(self, *, generators=(), templates=()) -> dict:
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCANNER),
             "-GeneratorPath", ";".join(str(g) for g in generators),
             "-TemplateFile", ";".join(str(f) for f in templates)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_every_emitted_template_parses_and_trips_no_rule(self) -> None:
        result = self.scan(generators=JOB_TEMPLATES)
        self.assertEqual(len(result["templates"]), len(JOB_TEMPLATES))
        self.assertEqual(result["violations"], [], json.dumps(result["violations"], indent=2))

    # Each fixture is one way sol showed a line scan could be evaded, plus the other write shapes the
    # scanner claims to cover. Every one must be flagged.
    BYPASSES = {
        "multiline": ("R1", "Copy-Item -LiteralPath $a `\n    -Destination (Join-Path $Pub 'x') -Force\n"),
        "aliased_destination": ("R1", "$d = Join-Path $Pub 'x'\nSet-Content -LiteralPath $d -Value 1\n"),
        "dotnet_static": ("R4", "[IO.File]::WriteAllText((Join-Path $Pub 'x'), 'y')\n"),
        "dotnet_full_name": ("R4", "[System.IO.File]::Copy($a, (Join-Path $Pub 'x'), $true)\n"),
        "guard_only_in_comment": ("R1", "# Assert-AttrCudaWritableFileSlot -Path (Join-Path $Pub 'x')\nSet-Content -LiteralPath (Join-Path $Pub 'x') -Value 1\n"),
        "out_file": ("R1", "'y' | Out-File -FilePath (Join-Path $Pub 'x')\n"),
        "redirection": ("R6", "Get-Date > (Join-Path $Pub 'x')\n"),
        "alias_positional": ("R1", "cp $a (Join-Path $Pub 'x')\n"),
        "positional_even_under_work": ("R1", "Set-Content (Join-Path $Work 'x') 1\n"),
        "abbreviated_parameter": ("R1", "Copy-Item -LiteralPath $a -Dest (Join-Path $Pub 'x')\n"),
        "reassigned_variable": ("R1", "$w = Join-Path $Work 'a'\n$w = Join-Path $Pub 'b'\nSet-Content -LiteralPath $w -Value 1\n"),
        "foreach_variable": ("R1", "foreach ($q in @((Join-Path $Work 'a'))) { Set-Content -LiteralPath $q -Value 1 }\n"),
        "new_directory_under_pub": ("R2", "New-Item -ItemType Directory -Path (Join-Path $Pub 'logs') | Out-Null\n"),
        "start_process_redirect": ("R2", "Start-Process -FilePath x.exe -RedirectStandardOutput (Join-Path $Pub 'o.txt')\n"),
        "tee_object": ("R1", "1 | Tee-Object -FilePath (Join-Path $Pub 'x')\n"),
        "instance_copyto": ("R4", "(Get-Item -LiteralPath $a).CopyTo((Join-Path $Pub 'x'))\n"),
        "dynamic_code": ("R1", "Invoke-Expression 'Set-Content -LiteralPath C:\\x -Value 1'\n"),
        "export_csv": ("R2", "$rows | Export-Csv -LiteralPath (Join-Path $Cache 'x.csv') -NoTypeInformation\n"),
        "expand_archive": ("R2", "Expand-Archive -LiteralPath $z -DestinationPath $Pub -Force\n"),
        "remove_outside": ("R1", "Remove-Item -LiteralPath (Join-Path $Cache 'x') -Force\n"),
        "compound_assignment": ("R2", "$w = Join-Path $Work 'a'\n$w += 'b'\nNew-Item -ItemType Directory -Path $w | Out-Null\n"),
        # sol PR #133 r6: each of these passed the r6 blocklist scanner.
        "work_reassigned": ("R5", "$Work = 'C:\\outside'\nNew-Item -ItemType Directory -Path (Join-Path $Work 'x') | Out-Null\n"),
        "work_script_scope": ("R5", "$script:Work = 'C:\\outside'\nNew-Item -ItemType Directory -Path (Join-Path $Work 'x') | Out-Null\n"),
        "join_path_traversal": ("R2", "New-Item -ItemType Directory -Path (Join-Path $Work '..\\..\\outside') | Out-Null\n"),
        "join_path_variable_child": ("R2", "$c = '..'\nNew-Item -ItemType Directory -Path (Join-Path $Work $c) | Out-Null\n"),
        "join_path_expandable_child": ("R2", "New-Item -ItemType Directory -Path (Join-Path $Work \"$c\") | Out-Null\n"),
        "set_variable": ("R1", "Set-Variable -Name d -Value 'C:\\outside'\nNew-Item -ItemType Directory -Path $d | Out-Null\n"),
        "new_object_streamwriter": ("R1", "$w = New-Object IO.StreamWriter 'C:\\outside\\x'\n"),
        "streamwriter_ctor": ("R4", "$w = [IO.StreamWriter]::new('C:\\outside\\x')\n"),
        "add_type": ("R1", "Add-Type -TypeDefinition 'public class X {}'\n"),
        "call_operator_cmdlet_string": ("R3", "& 'Set-Content' -LiteralPath C:\\x -Value 1\n"),
        "call_operator_variable": ("R3", "$cmd = 'Set-Content'\n& $cmd -LiteralPath C:\\x -Value 1\n"),
        "call_operator_scriptblock": ("R3", "& { New-Item -ItemType Directory -Path C:\\x }\n"),
        "dot_source": ("R3", ". 'C:\\outside\\x.ps1'\n"),
        "second_start_process_redirect": ("R2", "Start-Process -FilePath x.exe -RedirectStandardOutput (Join-Path $Work 'o.txt') -RedirectStandardError (Join-Path $Pub 'e.txt')\n"),
        "colon_bound_destination": ("R2", "New-Item -ItemType Directory -Path:(Join-Path $Pub 'x') | Out-Null\n"),
        "splatting": ("R2", "$p = @{ ItemType = 'Directory'; Path = 'C:\\outside' }\nNew-Item @p | Out-Null\n"),
        "env_scope_mutation": ("R5", "$env:TEMP = 'C:\\outside'\n"),
        "global_scope_assignment": ("R5", "$global:x = 1\n"),
        "member_assignment": ("R5", "$o = Get-Item -LiteralPath $Work\n$o.Attributes = 'Normal'\n"),
        "reflection": ("R4", "$m = 'x'.GetType().GetMethod('ToString')\n"),
        "wrong_case_command": ("R1", "new-item -ItemType Directory -Path (Join-Path $Work 'x') | Out-Null\n"),
        "pipeline_bound_path": ("R2", "(Join-Path $Pub 'x') | New-Item -ItemType Directory | Out-Null\n"),
        "positional_new_item": ("R2", "New-Item (Join-Path $Pub 'x') -ItemType Directory | Out-Null\n"),
        "foreach_member_name": ("R2", "Get-ChildItem -LiteralPath $Pub | ForEach-Object Delete\n"),
        "foreach_member_name_param": ("R2", "Get-ChildItem -LiteralPath $Pub | ForEach-Object -MemberName Delete\n"),
        "new_item_junction_under_work": ("R2", "New-Item -ItemType Junction -Path (Join-Path $Work 'j') -Value $Pub | Out-Null\n"),
        "new_item_missing_type": ("R2", "New-Item -Path (Join-Path $Work 'x') | Out-Null\n"),

        # sol PR #133 r7 enforcement gaps closed in this PR.
        "new_item_name_traversal": ("R2", "New-Item -ItemType File -Path $Work -Name '..\\outside.txt' -Force | Out-Null\n"),
        "using_module": ("R8", "using module 'C:\\outside\\evil.psm1'\n"),
        "requires_modules": ("R8", "#requires -Modules EvilModule\n"),
        "partial_untrusted_root": ("R2", "[void](Remove-AttrCudaPartialFile -TrustedRoot 'C:\\' -Path 'C:\\outside\\x')\n"),
        "tree_untrusted_root": ("R2", "Remove-AttrCudaTree -TrustedRoot 'C:\\' -Path 'C:\\outside'\n"),
    }

    CONTROLS = {
        "work_literal": "New-Item -ItemType Directory -Path (Join-Path $Work 'x') -Force | Out-Null\n",
        "work_chain": "$s = Join-Path $Work 'a'\n$u = Join-Path $s 'b\\c.csv'\n$rows | Export-Csv -LiteralPath $u -NoTypeInformation\n",
        "helper_to_pub": "[void](Publish-AttrCudaFileCopy -Source $a -Destination (Join-Path $Pub 'x'))\n",
        "discard": "Get-Date 2>$null\nGet-Date 2>&1 | Out-Null\n",
        "string_replace_is_not_io": "$v = $s.Replace('a', 'b')\n",
        "child_process_redirect_under_work": "$scratch = Join-Path $Work '.job-tmp'\n$env:TEMP = $scratch\n& $nvcc --version 1> (Join-Path $Work 'nvcc.txt')\n",
        "template_function_and_index": "function Say([string]$m) { Write-Output $m }\n$log = @{}\n$log['a'] = 1\nSay 'hi'\n",
    }

    # R5 demands exactly one canonical $Work assignment in every template, so every fixture starts
    # with it; the bypasses that attack $Work add a second, scoped or reshaped assignment.
    PRELUDE = "$Work = Join-Path 'C:\\mlvtmp' $JobId\n"

    def _write(self, name: str, body: str) -> Path:
        path = self.tmp / f"{name}.ps1"
        if body.startswith(("using ", "#requires")):
            first, _, rest = body.partition("\n")
            text = first + "\n" + self.PRELUDE + rest
        else:
            text = self.PRELUDE + body
        path.write_text(text, encoding="utf-8")
        return path

    def test_each_known_bypass_is_flagged_by_its_intended_rule(self) -> None:
        files = {name: self._write(name, body) for name, (_, body) in self.BYPASSES.items()}
        result = self.scan(templates=files.values())
        rules_by_fixture: dict[str, set[str]] = {}
        for violation in result["violations"]:
            rules_by_fixture.setdefault(Path(violation["source"]).stem, set()).add(violation["rule"])
        for name, (rule, _) in self.BYPASSES.items():
            with self.subTest(bypass=name, rule=rule):
                self.assertIn(rule, rules_by_fixture.get(name, set()), json.dumps(result["violations"], indent=2))

    def test_proven_work_writes_and_helper_calls_are_not_flagged(self) -> None:
        files = {name: self._write(name, body) for name, body in self.CONTROLS.items()}
        result = self.scan(templates=files.values())
        self.assertEqual(result["violations"], [], json.dumps(result["violations"], indent=2))


# --------------------------------------------------------------------------------------------
# the embedding contract the rest of this file rests on
# --------------------------------------------------------------------------------------------


@requires_pwsh
class EmbeddedFunctionContractTests(_PwshCase):
    """Every test above imports the module; the jobs embed it. Prove those are the same text."""

    EMBEDDED = {
        ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1": (
            "Get-AttrCudaZipArchiveComment",
            "Assert-AttrCudaSourceArchive",
            "Assert-AttrCudaWritableFileSlot",
            "Publish-AttrCudaText",
            "Publish-AttrCudaFileCopy",
            "Publish-AttrCudaFileMove",
            "New-AttrCudaDirectory",
            "Remove-AttrCudaPartialFile",
            "Remove-AttrCudaTree",
        ),
        ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1": (
            "Get-AttrCudaArtifactNames",
            "Assert-AttrCudaSafeArtifactName",
            "Assert-AttrCudaDirectChild",
            "Assert-AttrCudaWritableFileSlot",
            "Publish-AttrCudaText",
            "Publish-AttrCudaFileCopy",
            "Publish-AttrCudaFileMove",
            "New-AttrCudaDirectory",
            "Remove-AttrCudaPartialFile",
            "Remove-AttrCudaTree",
        ),
        ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1": (
            "Assert-AttrCudaBuildManifest",
            "Resolve-AttrCudaSmokeRunLog",
            "Get-AttrCudaLastEligibilityLine",
            "Get-AttrCudaEligibilityVerdict",
            "Assert-AttrCudaWritableFileSlot",
            "Publish-AttrCudaText",
            "Publish-AttrCudaFileCopy",
            "Publish-AttrCudaFileMove",
            "New-AttrCudaDirectory",
            "Remove-AttrCudaTree",
        ),
    }

    def test_extraction_returns_the_module_text_unchanged(self) -> None:
        names = sorted({name for names in self.EMBEDDED.values() for name in names})
        quoted = ",".join(f"'{name}'" for name in names)
        proc = self.run_with_module(
            f"$text = Get-AttrCudaEmbeddedFunctionSource -Name @({quoted})\n"
            f"[IO.File]::WriteAllText('{self.tmp / 'extracted.txt'}', $text)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        extracted = (self.tmp / "extracted.txt").read_text(encoding="utf-8")
        module_text = MODULE.read_text(encoding="utf-8")
        for name in names:
            with self.subTest(name=name):
                start = module_text.index(f"function {name} {{")
                end = module_text.index("\n}\n", start) + len("\n}")
                self.assertIn(module_text[start:end].replace("\r\n", "\n"),
                              extracted.replace("\r\n", "\n"))

    def test_every_generator_embeds_the_functions_it_relies_on(self) -> None:
        for generator, names in self.EMBEDDED.items():
            text = generator.read_text(encoding="utf-8")
            with self.subTest(generator=generator.name):
                self.assertIn("__EMBEDDED_FUNCTIONS__", text)
                for name in names:
                    self.assertIn(f"'{name}'", text)


if __name__ == "__main__":
    unittest.main()
