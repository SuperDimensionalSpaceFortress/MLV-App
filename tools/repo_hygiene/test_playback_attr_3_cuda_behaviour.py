"""Behavioural tests for the PLAYBACK-ATTR-3-CUDA split-build route: these EXECUTE pwsh.

WHY THIS FILE EXISTS. Its sibling test_playback_attr_3_cuda_split_route.py asserts what the
generators' text SAYS. That is worth having -- the sm_86 target and the absence of MSVC tooling
on the measurement host are text properties -- but sol's r2 review on PR #133 landed four
blockers that a text-level suite was green against, because "the script mentions a hash" and
"the script refuses the wrong bytes" are different claims. Everything here runs the real code
against real directories and asserts on exit codes and on what ended up on disk.

WHAT IS AND IS NOT EXERCISED. The verification logic lives in
tools/profiling/bachelor/AttrCudaArtifacts.psm1 (build-route verification, shared by every
PLAYBACK-ATTR-3-CUDA generator) and tools/profiling/bachelor/AttrCudaOwnerFootage.psm1 (the
private owner-footage workspace, embedded only by the attribution job), spliced VERBATIM into
each emitted job by Get-AttrCudaEmbeddedFunctionSource, so a test that imports the module runs
the same characters the job runs on Bachelor or Ultra-Magnus. What cannot run here is the
compiling and measuring --
no CUDA toolkit, no Qt/MinGW, no 4090, no measurement laptop -- so the jobs are exercised through
their `-VerifyOnly` prefix and through the staging job end to end, which touches only files.

SKIPS. Everything is skipped cleanly when pwsh (or git, for the fixture repositories) is absent,
and on non-Windows platforms (the jobs validate drive-letter paths); CI Windows runs it all.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
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
# ATTR3-FOOTAGE-BIND-1 PR-B round 4b: the private owner-footage workspace functions (neutral
# naming, part-contiguity, Win32 file identity, hard-link creation, held read handles, link-only
# cleanup) moved out of MODULE into their own module, so playback-attr-3-cuda-job.ps1's own
# NoFootageTokensTests-exempt embedding is the only caller and MODULE stays free of composed
# footage-word fragments. See that module's own header for why.
OWNER_FOOTAGE_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaOwnerFootage.psm1"
STAGE_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1"
DLL_GENERATOR = ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1"
ATTRIBUTION_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
STAGE_FIXTURE_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-fixture-job.ps1"
SMOKE_RUNNER_STAGE_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-smoke-runner-job.ps1"
# ATTR3-FOOTAGE-STAGE-1 round 3: these two also do a nested `Import-Module AttrCudaArtifacts.psm1`
# from inside their own module body -- same fix, same regression coverage as OWNER_FOOTAGE_MODULE.
STAGE_JOB_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "Attr3FootageStageJob.psm1"
PRESENCE_JOB_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "Attr3FootagePresenceJob.psm1"

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


_ANSI_ESCAPE_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PWSH_ERROR_VIEW_CONTINUATION_RX = re.compile(r"(?m)^[ \t]*\|[ \t]?")


def normalize_pwsh_message_text(text: str) -> str:
    """Undo pwsh's console-width error-view wrapping before substring matching.

    An uncaught terminating error (no try/catch around the probe) is rendered by pwsh's own
    default host formatter, which wraps the exception message at the console width and prefixes
    each continuation line with ANSI colour codes and a '|' gutter -- so a phrase like "is
    missing gui-smoke-screenshot-provenance.ps1" can land split across two lines on a narrower
    console than this one, and assertIn looking for the unbroken phrase then fails even though
    the thrown message is correct. Strip the ANSI codes and the '|' continuation prefixes, then
    collapse whitespace runs (including the newline the wrap introduced) to one space so a
    once-wrapped phrase matches again regardless of the host's console width.
    """
    text = _ANSI_ESCAPE_RX.sub("", text)
    text = _PWSH_ERROR_VIEW_CONTINUATION_RX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


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
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n" + body,
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
    (path / "tools" / "profiling").mkdir(parents=True)
    # ATTR3-SMOKE-RUNNER-PIN-1: the attribution generator resolves this path's committed blob
    # unconditionally (before the fixture/owner-clip branch), so every test that generates a
    # job through it needs the path to exist in the throwaway repo too.
    # ATTR3-SMOKE-RUNNER-DEPS-1 round 3 (NARROW BY REDESIGN): the closure is now a PINNED,
    # explicit manifest, never discovered -- so the stand-in runner's own loads only need to
    # classify cleanly under Assert-AttrCudaClosureComplete, and there is no more recursion to
    # exercise (a manifest entry is never reached by following another entry's own loads).
    (path / "tools" / "profiling" / "run-release-gui-smoke.ps1").write_text(
        "# fixture stand-in for run-release-gui-smoke.ps1\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
        "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
        ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n",
        encoding="utf-8",
    )
    (path / "tools" / "profiling" / "gui-smoke-screenshot-provenance.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "provenance-stamp.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-process-boundary.psm1").write_text(
        "# fixture stand-in sibling (imported directly by the runner).\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-color-artifact-scan.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-gpu-texture-route-validation.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    # CUDA-PLAYBACK-CONTACT-SHEET-1 r1b: the attribution generator also resolves this path's
    # committed blob unconditionally (to embed it in the emitted job for the contact-sheet
    # compose step), same reason as the smoke-runner closure comment above -- every test that
    # generates a job through it needs the path to exist in the throwaway repo too.
    (path / "tools" / "profiling" / "make-contact-sheet.py").write_text(
        "# fixture stand-in for make-contact-sheet.py\n", encoding="utf-8"
    )
    shas = []
    for index, text in enumerate(("first", "second")):
        (path / "src" / "mlv" / "llrawproc" / "llrawproc.c").write_text(
            f"/* fixture revision {text} */\n", encoding="utf-8"
        )
        _git_run(["add", "-A"], path)
        _git_run(["commit", "-q", "-m", f"fixture {index}"], path)
        shas.append(_git_run(["rev-parse", "HEAD"], path))
    return shas


def _git_blob_sha256(repo: Path, commit: str, rel_path: str) -> str:
    """sha256 of a repo-relative path's exact COMMITTED bytes -- the ground truth this route's
    pin is defined against (ATTR3-SMOKE-RUNNER-PIN-1). capture_output without text=True keeps
    the bytes raw, so this is not itself subject to the newline-translation pitfall it exists
    to catch elsewhere."""
    completed = subprocess.run(
        [GIT, "-C", str(repo), "show", f"{commit}:{rel_path}"], capture_output=True, check=True
    )
    return hashlib.sha256(completed.stdout).hexdigest()


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
        self.assertIn("canonical names", normalize_pwsh_message_text(proc.stdout))
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
        self.assertIn("the canonical name for", normalize_pwsh_message_text(proc.stderr + proc.stdout))
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
# (f) ATTR3-FIXTURE-STAGE-1: -ClipPath becomes optional for a fixture id, gated on
#     -FixtureSha256, and the emitted job authenticates the cached clip's CONTENT before it
#     ever opens it.
# --------------------------------------------------------------------------------------------


@requires_pwsh
@requires_git
class AttributionJobOptionalClipPathTests(_PwshCase):
    """Generation-time behaviour of the new -ClipPath / -FixtureSha256 rules."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def _generate(self, **overrides):
        out_file = self.staging / "job.ps1"
        args = {
            "SourceCommit": self.shas[1],
            "BuildManifestSha256": "a" * 64,
            "ClipId": "tiny_dual_iso",
            "OutFile": str(out_file),
            "RepoRoot": str(self.repo),
        }
        args.update(overrides)
        parts = [f"-{key} '{value}'" for key, value in args.items() if value is not None]
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' " + " ".join(parts) + "\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script), out_file

    def test_a_fixture_id_without_clippath_derives_the_cache_path(self) -> None:
        fixture_sha = "b" * 64
        proc, out_file = self._generate(FixtureSha256=fixture_sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job_text = out_file.read_text(encoding="utf-8")
        clip_id = "tiny_dual_iso"
        extension = "." + "mlv"
        expected = "C:\\mlvtmp\\mlv-agent\\cache\\" + clip_id + extension
        self.assertIn(f"$AuthorizedClipPath = '{expected}'", job_text)
        self.assertIn(f"$FixtureSha256 = '{fixture_sha}'", job_text)

    def test_a_fixture_id_without_fixture_sha_is_refused(self) -> None:
        proc, out_file = self._generate()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "PLAYBACK_ATTR3_FIXTURE_SHA_REQUIRED",
            normalize_pwsh_message_text(proc.stdout + proc.stderr),
        )
        self.assertFalse(out_file.exists())

    # ATTR3-FOOTAGE-BIND-1 PR-B: an owner-clip id is RESOLVED, not blanket-refused. -ClipPath and
    # -FixtureSha256 stay REFUSED for one (cheap, no resolver call needed); an id with neither
    # reaches the resolver, which this throwaway repo (no refs/remotes/fork/master) always
    # refuses -- deterministic, and never touches real consent data.
    def test_an_owner_id_without_clippath_is_resolved_and_refused_by_the_resolver(self) -> None:
        proc, out_file = self._generate(ClipId="M16-1243")
        self.assertNotEqual(proc.returncode, 0)
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_OWNER_RESOLVE_REFUSED", message)
        self.assertFalse(out_file.exists())

    def test_an_owner_id_with_fixture_sha_is_refused(self) -> None:
        proc, out_file = self._generate(ClipId="M16-1243", FixtureSha256="c" * 64)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "PLAYBACK_ATTR3_FIXTURE_SHA_REFUSED", normalize_pwsh_message_text(proc.stdout + proc.stderr)
        )
        self.assertFalse(out_file.exists())

    def test_an_owner_id_with_clippath_is_refused(self) -> None:
        owner_path = "C:\\mlvtmp\\mlv-agent\\cache\\M16-1243.raw"
        proc, out_file = self._generate(ClipId="M16-1243", ClipPath=owner_path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "PLAYBACK_ATTR3_CLIPPATH_REFUSED", normalize_pwsh_message_text(proc.stdout + proc.stderr)
        )
        self.assertFalse(out_file.exists())

    # ATTR3-FOOTAGE-BIND-1 PR-B round 6 (sol minor): -ClipPath/-FixtureSha256 are refused by flag
    # PRESENCE ($PSBoundParameters.ContainsKey), not by truthiness -- so explicitly binding either
    # flag to an empty string is refused too, matching the "refused outright" documented contract
    # instead of being silently read as omission.
    def test_an_owner_id_with_an_explicit_empty_clippath_is_still_refused(self) -> None:
        proc, out_file = self._generate(ClipId="M16-1243", ClipPath="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "PLAYBACK_ATTR3_CLIPPATH_REFUSED", normalize_pwsh_message_text(proc.stdout + proc.stderr)
        )
        self.assertFalse(out_file.exists())

    def test_an_owner_id_with_an_explicit_empty_fixture_sha_is_still_refused(self) -> None:
        proc, out_file = self._generate(ClipId="M16-1243", FixtureSha256="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "PLAYBACK_ATTR3_FIXTURE_SHA_REFUSED",
            normalize_pwsh_message_text(proc.stdout + proc.stderr),
        )
        self.assertFalse(out_file.exists())

    def test_a_fixture_id_with_an_explicit_clippath_is_still_accepted(self) -> None:
        clip_id = "tiny_dual_iso"
        extension = "." + "mlv"
        explicit_path = "C:\\mlvtmp\\mlv-agent\\cache\\" + clip_id + extension
        proc, out_file = self._generate(FixtureSha256="d" * 64, ClipPath=explicit_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"$AuthorizedClipPath = '{explicit_path}'", out_file.read_text(encoding="utf-8"))

    # ATTR3-FOOTAGE-BIND-1 PR-B round 2 (sol BLOCKER / astra MAJOR): -ClipPath used to carry a
    # PARAMETER-LEVEL ValidatePattern that ran before owner/fixture classification, so a
    # forward-slash value (the real owner-part path shape) failed PowerShell's own binding
    # validation and got ECHOED in the diagnostic instead of reaching the path-free
    # PLAYBACK_ATTR3_CLIPPATH_REFUSED throw. Validation now happens in the body, after
    # classification, so an owner id's -ClipPath value never appears in any message.
    def test_an_owner_id_with_a_forward_slash_clippath_is_refused_not_echoed(self) -> None:
        owner_path = "C:/mlvtmp/mlv-agent/cache/M16-1243.raw"
        proc, out_file = self._generate(ClipId="M16-1243", ClipPath=owner_path)
        self.assertNotEqual(proc.returncode, 0)
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_CLIPPATH_REFUSED", message)
        self.assertNotIn(owner_path, message)
        self.assertFalse(out_file.exists())


@requires_pwsh
@requires_git
class GeneratorBakeTokenInjectionTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 2 (astra BLOCKER): every __TOKEN__ this generator bakes
    into the emitted job's single-quoted template literals must be either strictly validated to
    a safe alphabet or escaped for its quoting context -- never neither. -ConsentReceiptFileName
    was the astra-cited concrete repro (a value shaped like "'; $OwnerPartsJson = '<forged
    parts>'; #" closed the literal early and re-assigned OwnerPartsJson before the job's own
    content gate ever ran); -BasePackageZip, -BasePackageExeName and -PresentMonName carried the
    identical defect (no ValidatePattern, no escaping at the Replace() call site) and are fixed
    and tested the same way. Each is now restricted to a plain basename via ValidatePattern
    (belt) and additionally escaped at bake time (suspenders), so a value containing a quote and
    a newline is refused at parameter binding -- never silently baked."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def _generate_raw(self, extra_flag: str, malicious_value: str):
        # Bypasses the simple `-Key 'Value'` formatter in AttributionJobOptionalClipPathTests
        # (that formatter cannot carry a value containing a literal quote): the malicious value
        # is doubled here exactly the way a caller's own shell/quoting layer would have to do it
        # to deliver a literal `'` through to the generator's PowerShell parameter binder.
        out_file = self.staging / "job.ps1"
        escaped_for_outer_literal = malicious_value.replace("'", "''")
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' "
            f"{extra_flag} '{escaped_for_outer_literal}' "
            f"-OutFile '{out_file}' -RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script), out_file

    def test_consent_receipt_filename_with_quote_and_newline_is_refused(self) -> None:
        malicious = "x'; $Global:Pwned = 'yes'; #\n.json"
        proc, out_file = self._generate_raw("-ConsentReceiptFileName", malicious)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(out_file.exists())

    def test_base_package_zip_with_quote_and_newline_is_refused(self) -> None:
        malicious = "x'; $Global:Pwned = 'yes'; #\n.zip"
        proc, out_file = self._generate_raw("-BasePackageZip", malicious)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(out_file.exists())

    def test_base_package_exe_name_with_quote_and_newline_is_refused(self) -> None:
        malicious = "x'; $Global:Pwned = 'yes'; #\n.exe"
        proc, out_file = self._generate_raw("-BasePackageExeName", malicious)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(out_file.exists())

    def test_presentmon_name_with_quote_and_newline_is_refused(self) -> None:
        malicious = "x'; $Global:Pwned = 'yes'; #\n.exe"
        proc, out_file = self._generate_raw("-PresentMonName", malicious)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(out_file.exists())

    def test_consent_receipt_filename_shaped_like_a_template_token_is_not_re_expanded(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL). -ConsentReceiptFileName's own
        # ValidatePattern is alnum/underscore/dot/hyphen, so 'a__EMBEDDED_FUNCTIONS__b.json'
        # passes it -- and was the astra-cited concrete repro of the collision class this round
        # closes: the old chained .Replace() calls substituted this value BEFORE
        # __EMBEDDED_FUNCTIONS__, so the later call rescanned the already-substituted text and
        # spliced ~600 lines of verifier source into the middle of the $ConsentReceiptFileName
        # string literal, breaking the emitted job's own parse. A single-pass
        # Expand-AttrCudaTemplate substitution never rescans a substituted value, so the job must
        # now emit, parse cleanly, and carry the value literally.
        colliding = "a__EMBEDDED_FUNCTIONS__b.json"
        proc, out_file = self._generate_raw("-ConsentReceiptFileName", colliding)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(out_file.exists())
        job_text = out_file.read_text(encoding="utf-8")
        self.assertIn(f"$ConsentReceiptFileName = '{colliding}'", job_text)
        parse_script = self.tmp / "parse_check.ps1"
        parse_script.write_text(
            "$t=$null; $e=$null\n"
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{out_file}', [ref]$t, [ref]$e)\n"
            "Write-Output $e.Count\n",
            encoding="utf-8",
        )
        parse = _run_pwsh_file(parse_script)
        self.assertEqual(parse.stdout.strip(), "0", parse.stdout + parse.stderr)

    def test_consent_receipt_filename_accepts_a_plain_name(self) -> None:
        # Proves the ValidatePattern above is not so strict it rejects the real default shape.
        proc, out_file = self._generate_raw(
            "-ConsentReceiptFileName", "owner-footage-consent-20260916.json"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(out_file.exists())
        job_text = out_file.read_text(encoding="utf-8")
        self.assertIn(
            "$ConsentReceiptFileName = 'owner-footage-consent-20260916.json'", job_text
        )


@requires_pwsh
class ExpandAttrCudaTemplateTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): unit tests for the shared single-pass
    templater every generator now calls instead of a chained .Replace() sequence."""

    def test_a_value_containing_another_tokens_spelling_is_not_re_expanded(self) -> None:
        proc = self.run_with_module(
            "$tokens = [ordered]@{ FOO = 'hello'; BAR = 'a__FOO__b'; BAZ = 'z' }\n"
            "$out = Expand-AttrCudaTemplate -Template 'X=__FOO__ Y=__BAR__ Z=__BAZ__' -Tokens $tokens\n"
            "Write-Output $out\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("X=hello Y=a__FOO__b Z=z", proc.stdout)

    def test_an_unknown_template_placeholder_throws(self) -> None:
        proc = self.run_with_module(
            _guard(
                "Expand-AttrCudaTemplate -Template '__NOPE__' -Tokens ([ordered]@{ FOO = 'x' })"
            )
        )
        self.assert_throws(proc, "ATTRCUDA_TEMPLATE_UNKNOWN_TOKEN")

    def test_an_unconsumed_tokens_entry_throws(self) -> None:
        proc = self.run_with_module(
            _guard(
                "Expand-AttrCudaTemplate -Template 'plain text, no placeholders' "
                "-Tokens ([ordered]@{ FOO = 'x' })"
            )
        )
        self.assert_throws(proc, "ATTRCUDA_TEMPLATE_UNUSED_TOKEN")

    def test_every_token_consumed_and_no_unknown_placeholder_succeeds(self) -> None:
        proc = self.run_with_module(
            "$tokens = [ordered]@{ A = '1'; B = '2' }\n"
            "$out = Expand-AttrCudaTemplate -Template '__A__-__B__' -Tokens $tokens\n"
            "Write-Output $out\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("1-2", proc.stdout)


@requires_pwsh
@requires_git
class AttributionJobFixtureContentAuthenticationTests(_PwshCase):
    """The emitted job hashes a fixture's cached bytes before it ever opens the clip.

    ATTR3-FIXTURE-STAGE-1 r2: this class used to generate a full job.ps1 and run it end to
    end via _run_job, exactly like AttributionJob*Tests elsewhere in this file. That made it
    depend on -AgentRoot resolving under the real C:\\mlvtmp, because the job's OWN
    Assert-UnderMlvTmp guard (a deliberate hard floor independent of -AgentRoot -- see
    playback-attr-3-cuda-job.ps1's "TEMP boundary (BLOCKER fix)" comment) throws before the
    fixture-content check ever runs if $Root/$Work/$Pub are not under C:\\mlvtmp. A lane
    whose scratch root happens to sit under C:\\mlvtmp (Invoke-Lane) passed by accident; a
    normal TEMP does not (exit 1, "job-owned path 'Root' resolves outside C:\\mlvtmp"), and
    CI/other reviewers run from a normal TEMP. That guard is production behaviour and is not
    touched here (test_the_default_agent_root_guard_still_refuses_a_root_outside_mlvtmp below
    proves it still fires). Instead, the fixture-content check itself -- inline top-level
    code in the template, not a named module function, so there is nothing to Import-Module
    -- is sliced VERBATIM out of the generator's own $template text by _extract_fixture_
    content_check and run standalone, with just the handful of variables and the Save-Json
    helper it actually reads. This is the same "run the real characters, not a
    re-implementation" guarantee Get-AttrCudaEmbeddedFunctionSource gives the psm1-based
    checks, applied to a block that has no function name to splice by. It also never touches
    $Root/$Work/-AgentRoot/C:\\mlvtmp at all, so it is portable regardless of ambient TEMP.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.agent = self.tmp / "agent"
        self.cache = self.agent / "cache"
        self.cache.mkdir(parents=True)

    def _extract_fixture_content_check(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        # ATTR3-FOOTAGE-BIND-1 PR-B: an EARLIER "if ($FixtureRehearsal) {" now also gates the
        # clip-path residence checks (before $Pub exists) -- bound the search to start AFTER
        # $Pub is created, so this always finds the CONTENT-hash-check block, never the
        # residence-check block.
        pub_created = text.index("[void](New-AttrCudaDirectory -Path $Pub)")
        start_marker = "if ($FixtureRehearsal) {"
        # ATTR3-FOOTAGE-BIND-1 PR-B round 4: everything from the owner/fixture if/else through the
        # end of the job now runs inside one `try` (see the generator's own comment above it), so
        # the slice must stop BEFORE that `try {` line -- stopping at the old Expand-Archive marker
        # alone would now include an unclosed `try {` in the extracted text.
        end_marker = "\ntry {\nExpand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)"
        start = text.index(start_marker, pub_created)
        end = text.index(end_marker, start)
        self.assertGreater(end, start, "fixture-content-check markers moved in the generator")
        return text[start:end]

    def _run_fixture_content_check(
        self, *, clip_path: Path, fixture_sha256: str, pub: Path
    ) -> subprocess.CompletedProcess:
        # The real job creates $Pub (New-AttrCudaDirectory) before this block ever runs; this
        # probe stands in for that one step so Save-Json has somewhere to write.
        pub.mkdir(parents=True)
        block = self._extract_fixture_content_check()
        script = self.tmp / "fixture-content-check-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            "$FixtureRehearsal = $true\n"
            f"$clipPath = '{clip_path}'\n"
            f"$FixtureSha256 = '{fixture_sha256}'\n"
            f"$SourceCommit = '{self.shas[1]}'\n"
            "$ClipId = 'tiny_dual_iso'\n"
            f"$Pub = '{pub}'\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            + block + "\n"
            "Write-Output 'RESULT=NO_MISMATCH'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_a_mismatched_cached_clip_fails_closed_before_the_package_is_touched(self) -> None:
        import hashlib

        extension = "." + "mlv"
        clip_name = "tiny_dual_iso" + extension
        wrong_bytes = b"not the fixture the hub thinks is cached"
        clip_path = self.cache / clip_name
        clip_path.write_bytes(wrong_bytes)
        expected_sha = hashlib.sha256(b"the real fixture bytes").hexdigest()
        pub = self.agent / "outbox" / "fake-job.artifacts"

        proc = self._run_fixture_content_check(
            clip_path=clip_path, fixture_sha256=expected_sha, pub=pub
        )

        self.assertEqual(proc.returncode, 17, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=FIXTURE_CONTENT_MISMATCH", proc.stdout)
        self.assertIn(expected_sha, proc.stdout)
        self.assertIn(hashlib.sha256(wrong_bytes).hexdigest(), proc.stdout)
        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "FIXTURE_CONTENT_MISMATCH")
        self.assertTrue(summary["fixtureRehearsal"])
        self.assertEqual(summary["expectedSha256"], expected_sha)
        self.assertEqual(summary["actualSha256"], hashlib.sha256(wrong_bytes).hexdigest())

    def test_matching_cached_clip_content_passes_the_gate(self) -> None:
        import hashlib

        extension = "." + "mlv"
        clip_name = "tiny_dual_iso" + extension
        clip_bytes = b"exactly the bytes the hub staged"
        clip_path = self.cache / clip_name
        clip_path.write_bytes(clip_bytes)
        matching_sha = hashlib.sha256(clip_bytes).hexdigest()
        pub = self.agent / "outbox" / "fake-job-2.artifacts"

        proc = self._run_fixture_content_check(
            clip_path=clip_path, fixture_sha256=matching_sha, pub=pub
        )

        self.assertNotIn("FIXTURE_CONTENT_MISMATCH", proc.stdout)
        self.assertNotEqual(proc.returncode, 17, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=NO_MISMATCH", proc.stdout, f"{proc.stdout}\n{proc.stderr}")

    def test_the_default_agent_root_guard_still_refuses_a_root_outside_mlvtmp(self) -> None:
        # Proves the check above does not paper over a real regression: with NO override,
        # the emitted job's Assert-UnderMlvTmp guard still refuses an -AgentRoot that
        # resolves outside C:\mlvtmp, before it ever looks at the cache. A LITERAL path is
        # used here rather than self.tmp/self.agent: self.tmp can itself land under the real
        # C:\mlvtmp (e.g. a fleet lane whose own scratch root is
        # C:\mlvtmp\lane-scratch\...), which would make self.agent the wrong fixture for an
        # "outside mlvtmp" assertion and is exactly how this bug went unnoticed before.
        outside_root = "C:\\attr3-guard-check-outside-mlvtmp"
        out_file = self.tmp / "outside-root-job.ps1"
        script = self.tmp / "generate-outside-root.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' -RepoRoot '{self.repo}' "
            f"-AgentRoot '{outside_root}' -PresentMonSha256 '{'c' * 64}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")

        proc = _run_job(out_file)

        self.assertEqual(proc.returncode, 1, f"{proc.stdout}\n{proc.stderr}")
        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertIn("job-owned path 'Root' resolves outside", combined)
        self.assertIn("C:\\mlvtmp", combined)


@requires_pwsh
class AttributionJobOwnerContentAuthenticationTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B: the emitted job verifies EVERY owner part's content -- via the
    same extraction technique AttributionJobFixtureContentAuthenticationTests uses for the
    fixture arm of the SAME if/else block -- before the package is deployed, PresentMon starts,
    or the smoke child runs. Every part here is SYNTHETIC, built directly (never through the real
    resolver), exactly the way test_attr3_footage_presence_job.py exercises the identical
    per-part verifier for the presence-probe job.
    """

    def _extract_content_check(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        pub_created = text.index("[void](New-AttrCudaDirectory -Path $Pub)")
        start = text.index("if ($FixtureRehearsal) {", pub_created)
        # ATTR3-FOOTAGE-BIND-1 PR-B round 4: stop BEFORE the `try {` that now wraps everything
        # from here to the end of the job (see the generator's own comment above it) -- the old
        # Expand-Archive marker alone would now include that unclosed `try {` in the slice, and
        # this range also needs to include the round-4 private-directory linking logic, which
        # lives inside the SAME if/else block, before that `try {`.
        end = text.index("\ntry {\nExpand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)", start)
        self.assertGreater(end, start, "owner/fixture content-check markers moved in the generator")
        return text[start:end]

    def _owner_parts_json(self, parts) -> str:
        baked = [
            {
                "index": part["index"],
                "pathBase64": base64.b64encode(part["path"].encode("utf-8")).decode("ascii"),
                "length": part["length"],
                "sha256": part["sha256"],
            }
            for part in parts
        ]
        return json.dumps(baked)

    def _run_owner_content_check(self, *, parts, pub: Path, work: Path | None = None) -> subprocess.CompletedProcess:
        # The real job creates $Pub (New-AttrCudaDirectory) and $Work before this block ever
        # runs; this probe stands in for those two steps so Save-Json and the round-4 private
        # directory (built under $Work) both have somewhere to write.
        pub.mkdir(parents=True)
        if work is None:
            work = self.tmp / "work"
        work.mkdir(parents=True, exist_ok=True)
        block = self._extract_content_check()
        owner_parts_json = self._owner_parts_json(parts).replace("'", "''")
        script = self.tmp / "owner-content-check-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "$FixtureRehearsal = $false\n"
            f"$OwnerPartsJson = '{owner_parts_json}'\n"
            "$ClipId = 'FIX-OWNER-CONTENT-0001'\n"
            f"$SourceCommit = '{'d' * 40}'\n"
            f"$Pub = '{pub}'\n"
            f"$Work = '{work}'\n"
            "$ownerLinkHandles = [System.Collections.Generic.List[object]]::new()\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            + block + "\n"
            "Write-Output ('RESULT=NO_MISMATCH clipPath=' + $clipPath)\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_both_parts_pass_and_a_private_directory_is_built_from_neutral_hard_links(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 4: the path this job opens is no longer the owner's own
        # part path -- it is a neutrally-named HARD LINK under a private directory built inside
        # $Work, byte-identical to (and the SAME file object as) the verified source part.
        base_extension = "." + "MLV"
        continuation_extension = "." + "M00"
        part0_dir = self.tmp / "owner-content-part0"
        part0_dir.mkdir()
        part0_path = part0_dir / ("source" + base_extension)
        part0_bytes = b"synthetic owner content part zero " * 97
        part0_path.write_bytes(part0_bytes)
        part1_path = self.tmp / ("source" + continuation_extension)
        part1_bytes = b"synthetic owner content part one"
        part1_path.write_bytes(part1_bytes)
        part0 = str(part0_path).replace("\\", "/")
        part1 = str(part1_path).replace("\\", "/")
        parts = [
            {"index": 0, "path": part0, "length": len(part0_bytes), "sha256": hashlib.sha256(part0_bytes).hexdigest()},
            {"index": 1, "path": part1, "length": len(part1_bytes), "sha256": hashlib.sha256(part1_bytes).hexdigest()},
        ]
        pub = self.tmp / "agent" / "outbox" / "owner-pass.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(parts=parts, pub=pub, work=work)

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=NO_MISMATCH", proc.stdout)
        private_dir_name = "owner-" + "clip"
        neutral0 = private_dir_name + base_extension
        neutral1 = private_dir_name + continuation_extension
        link0_path = work / private_dir_name / neutral0
        link1_path = work / private_dir_name / neutral1
        self.assertIn(f"clipPath={link0_path}", proc.stdout)
        # Never the owner's real part path, anywhere in the job's own stdout.
        self.assertNotIn(part0, proc.stdout)
        self.assertNotIn(part1, proc.stdout)
        self.assertTrue(link0_path.is_file())
        self.assertTrue(link1_path.is_file())
        self.assertEqual(link0_path.read_bytes(), part0_bytes)
        self.assertEqual(link1_path.read_bytes(), part1_bytes)
        # The SAME file object as its source, not a copy -- proven the same way the job proves it.
        self.assertEqual(link0_path.stat().st_ino, part0_path.stat().st_ino)
        self.assertEqual(link1_path.stat().st_ino, part1_path.stat().st_ino)

    def test_a_mismatched_part_fails_closed_before_the_package_is_touched(self) -> None:
        part0_path = self.tmp / "owner-content-mismatch-part0.raw"
        part0_bytes = b"synthetic owner content that will not match"
        part0_path.write_bytes(part0_bytes)
        parts = [
            {
                "index": 0,
                "path": str(part0_path).replace("\\", "/"),
                "length": len(part0_bytes),
                "sha256": hashlib.sha256(b"not the bytes the resolver saw").hexdigest(),
            },
        ]
        pub = self.tmp / "agent" / "outbox" / "owner-mismatch.artifacts"

        proc = self._run_owner_content_check(parts=parts, pub=pub)

        self.assertEqual(proc.returncode, 19, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_NOT_VERIFIED", proc.stdout)
        self.assertIn("PARTS=0=SHA256_MISMATCH", proc.stdout)
        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "OWNER_FOOTAGE_NOT_VERIFIED")
        self.assertEqual(summary["parts"], [{"index": 0, "status": "SHA256_MISMATCH"}])
        # Never a path in a reader-facing output.
        self.assertNotIn(str(part0_path).replace("\\", "/"), json.dumps(summary))

    def test_a_missing_part_is_reported_by_index_and_status_only(self) -> None:
        missing_path = self.tmp / "owner-content-missing-part0.raw"
        parts = [
            {"index": 0, "path": str(missing_path).replace("\\", "/"), "length": 10, "sha256": "a" * 64},
        ]
        pub = self.tmp / "agent" / "outbox" / "owner-missing.artifacts"

        proc = self._run_owner_content_check(parts=parts, pub=pub)

        self.assertEqual(proc.returncode, 19, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_NOT_VERIFIED", proc.stdout)
        self.assertIn("PARTS=0=NOT_FOUND", proc.stdout)


@requires_pwsh
class OwnerFootagePrivateDirectoryLeakAndRefusalTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 4: the leak proof, the sibling-isolation proof, and the
    cross-volume / identity-mismatch refusal paths for the private per-job directory.

    The cross-volume and identity-mismatch cases are exercised by MOCKING
    Get-AttrCudaFileIdentity inside AttrCudaOwnerFootage.psm1's own scope (`& $module { Set-Item
    -Path function:... }`) rather than by requiring a second real volume or a real hardware race --
    New-AttrCudaOwnerFootageLink is called directly, so this is a statement about the CODE PATH
    taken when the identity comparison itself reports a mismatch, not about the real OS. The mock
    and the mocked-from call MUST share one module (round 4b, verified empirically): PowerShell
    resolves an unqualified command called from within a module function through that module's OWN
    session-state function table, looked up fresh per call, so Set-Item only takes effect for
    callers in the SAME module as the function it replaces -- a caller in a module that merely
    imported the mocked one holds a snapshot from import time and never observes the override.
    """

    def _extract_content_check(self) -> str:
        # Duplicated from AttributionJobOwnerContentAuthenticationTests rather than shared, in
        # keeping with this file's own convention of keeping each test class self-contained.
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        pub_created = text.index("[void](New-AttrCudaDirectory -Path $Pub)")
        start = text.index("if ($FixtureRehearsal) {", pub_created)
        end = text.index("\ntry {\nExpand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)", start)
        self.assertGreater(end, start, "owner/fixture content-check markers moved in the generator")
        return text[start:end]

    def _extract_cmd_build(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("$envList = \"'\" + ($envs -join")
        end = text.index("\n$presentMonSpawnError = $null", start)
        self.assertGreater(end, start, "cmd-build markers moved in the generator")
        return text[start:end]

    def _owner_parts_json(self, parts) -> str:
        baked = [
            {
                "index": part["index"],
                "pathBase64": base64.b64encode(part["path"].encode("utf-8")).decode("ascii"),
                "length": part["length"],
                "sha256": part["sha256"],
            }
            for part in parts
        ]
        return json.dumps(baked)

    def _run_owner_content_check(self, *, parts, pub: Path, work: Path) -> subprocess.CompletedProcess:
        pub.mkdir(parents=True)
        work.mkdir(parents=True, exist_ok=True)
        block = self._extract_content_check()
        owner_parts_json = self._owner_parts_json(parts).replace("'", "''")
        script = self.tmp / "owner-leak-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "$FixtureRehearsal = $false\n"
            f"$OwnerPartsJson = '{owner_parts_json}'\n"
            "$ClipId = 'FIX-OWNER-LEAK-0001'\n"
            f"$SourceCommit = '{'d' * 40}'\n"
            f"$Pub = '{pub}'\n"
            f"$Work = '{work}'\n"
            "$ownerLinkHandles = [System.Collections.Generic.List[object]]::new()\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            + block + "\n"
            "Write-Output ('CLIPPATH=' + $clipPath)\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def _build_cmd(self, *, clip_path: str) -> str:
        block = self._extract_cmd_build()
        script = self.tmp / "cmd-build-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$envs = @('FAKE_ENV=1')\n"
            "$smoke = 'C:\\fake\\smoke.ps1'\n"
            "$exePath = 'C:\\fake\\app.exe'\n"
            "$resultPath = 'C:\\fake\\result.json'\n"
            f"$clipPath = '{clip_path}'\n"
            + block + "\n"
            "Write-Output ('CMD=' + $cmd)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        marker = "CMD="
        line = next(line for line in proc.stdout.splitlines() if line.startswith(marker))
        return line[len(marker) :]

    def test_no_sentinel_leak_in_command_line_or_publish_directory(self) -> None:
        # SENTINEL: a unique string embedded ONLY in the owner's real (never-published) directory.
        sentinel = "SENTINELOWNERPATH0001"
        base_extension = "." + "MLV"
        src_dir = self.tmp / f"real-owner-dir-{sentinel}"
        src_dir.mkdir()
        part0_path = src_dir / ("source" + base_extension)
        part0_bytes = b"leak proof sentinel bytes " * 40
        part0_path.write_bytes(part0_bytes)
        part0 = str(part0_path).replace("\\", "/")
        parts = [{"index": 0, "path": part0, "length": len(part0_bytes), "sha256": hashlib.sha256(part0_bytes).hexdigest()}]
        pub = self.tmp / "agent" / "outbox" / "owner-sentinel.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(parts=parts, pub=pub, work=work)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertNotIn(sentinel, proc.stdout)
        marker = "CLIPPATH="
        line = next(line for line in proc.stdout.splitlines() if line.startswith(marker))
        clip_path = line[len(marker) :]
        self.assertNotIn(sentinel, clip_path)

        # Nothing this stage publishes (there is nothing to publish on the success path at this
        # extraction boundary, but the directory is checked anyway) names the sentinel.
        for candidate in pub.rglob("*"):
            if candidate.is_file():
                self.assertNotIn(sentinel, candidate.read_text(encoding="utf-8", errors="ignore"))

        cmd = self._build_cmd(clip_path=clip_path)
        self.assertNotIn(sentinel, cmd)
        self.assertIn(clip_path, cmd)

    def test_sibling_and_sidecar_files_are_never_linked_into_the_private_directory(self) -> None:
        base_extension = "." + "MLV"
        continuation_extension = "." + "M00"
        sidecar_extension = "." + "MAPP"
        src_dir = self.tmp / "owner-source-with-siblings"
        src_dir.mkdir()
        part0_path = src_dir / ("source" + base_extension)
        part0_bytes = b"sibling isolation part zero " * 40
        part0_path.write_bytes(part0_bytes)
        # An UNVERIFIED sibling continuation part -- never named in the parts list -- and a stray
        # sidecar file, both beside the verified source.
        (src_dir / ("source" + continuation_extension)).write_bytes(b"unverified sibling continuation part")
        (src_dir / ("source" + sidecar_extension)).write_bytes(b"stray sidecar bytes")
        part0 = str(part0_path).replace("\\", "/")
        parts = [{"index": 0, "path": part0, "length": len(part0_bytes), "sha256": hashlib.sha256(part0_bytes).hexdigest()}]
        pub = self.tmp / "agent" / "outbox" / "owner-siblings.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(parts=parts, pub=pub, work=work)

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        private_dir_name = "owner-" + "clip"
        private_dir = work / private_dir_name
        entries = sorted(p.name for p in private_dir.iterdir())
        self.assertEqual(entries, [private_dir_name + base_extension])

    def test_more_than_100_parts_is_refused_as_not_contiguous(self) -> None:
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-too-many-parts"
        src_dir.mkdir()
        parts = []
        for index in range(101):
            extension = base_extension if index == 0 else ("." + "M{0:02d}".format(index - 1))
            part_path = src_dir / (f"source-{index}" + extension)
            part_bytes = f"part {index}".encode("utf-8")
            part_path.write_bytes(part_bytes)
            parts.append({
                "index": index,
                "path": str(part_path).replace("\\", "/"),
                "length": len(part_bytes),
                "sha256": hashlib.sha256(part_bytes).hexdigest(),
            })
        pub = self.tmp / "agent" / "outbox" / "owner-too-many.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(parts=parts, pub=pub, work=work)

        self.assertEqual(proc.returncode, 20, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_PARTS_NOT_CONTIGUOUS", proc.stdout)
        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "OWNER_PARTS_NOT_CONTIGUOUS")

    def test_a_gap_in_part_indices_is_refused_as_not_contiguous(self) -> None:
        base_extension = "." + "MLV"
        continuation_extension = "." + "M01"
        src_dir = self.tmp / "owner-gap-parts"
        src_dir.mkdir()
        part0_path = src_dir / ("source" + base_extension)
        part0_bytes = b"gap test part zero"
        part0_path.write_bytes(part0_bytes)
        # Index 1 is MISSING: index 0 then jumps straight to index 2.
        part2_path = src_dir / ("source" + continuation_extension)
        part2_bytes = b"gap test part two"
        part2_path.write_bytes(part2_bytes)
        parts = [
            {"index": 0, "path": str(part0_path).replace("\\", "/"), "length": len(part0_bytes), "sha256": hashlib.sha256(part0_bytes).hexdigest()},
            {"index": 2, "path": str(part2_path).replace("\\", "/"), "length": len(part2_bytes), "sha256": hashlib.sha256(part2_bytes).hexdigest()},
        ]
        pub = self.tmp / "agent" / "outbox" / "owner-gap.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(parts=parts, pub=pub, work=work)

        self.assertEqual(proc.returncode, 20, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_PARTS_NOT_CONTIGUOUS", proc.stdout)

    def test_cross_volume_refusal_when_source_and_directory_report_different_volumes(self) -> None:
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-cross-volume-source"
        src_dir.mkdir()
        source_path = src_dir / ("source" + base_extension)
        source_path.write_bytes(b"cross volume refusal bytes")
        directory = self.tmp / "owner-cross-volume-directory"
        directory.mkdir()

        script = self.tmp / "cross-volume-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "$mod = Get-Module AttrCudaOwnerFootage\n"
            f"$global:AttrCudaTestSourcePath = '{source_path}'\n"
            "& $mod {\n"
            "    Set-Item -Path function:Get-AttrCudaFileIdentity -Value {\n"
            "        param([string]$Path)\n"
            "        $serial = if ($Path -eq $global:AttrCudaTestSourcePath) { 111 } else { 222 }\n"
            "        [pscustomobject]@{ VolumeSerialNumber = $serial; FileIndexHigh = 1; FileIndexLow = 1; NumberOfLinks = 1 }\n"
            "    }\n"
            "}\n"
            f"try {{ [void](New-AttrCudaOwnerFootageLink -Directory '{directory}' -Index 0 -SourcePath $global:AttrCudaTestSourcePath); Write-Output 'NO_THROW' }}\n"
            "catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("THREW OWNER_FOOTAGE_LINK_CROSS_VOLUME", proc.stdout)
        # No hard link was left behind by the refused attempt.
        self.assertEqual(list(directory.iterdir()), [])

    def test_identity_mismatch_after_link_creation_is_refused(self) -> None:
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-identity-mismatch-source"
        src_dir.mkdir()
        source_path = src_dir / ("source" + base_extension)
        source_path.write_bytes(b"identity mismatch refusal bytes")
        directory = self.tmp / "owner-identity-mismatch-directory"
        directory.mkdir()

        script = self.tmp / "identity-mismatch-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "$mod = Get-Module AttrCudaOwnerFootage\n"
            f"$sourcePath = '{source_path}'\n"
            f"$directory = '{directory}'\n"
            "$global:AttrCudaTestExpectedLinkPath = Join-Path $directory (Get-AttrCudaOwnerFootageNeutralName -Index 0)\n"
            "& $mod {\n"
            "    Set-Item -Path function:Get-AttrCudaFileIdentity -Value {\n"
            "        param([string]$Path)\n"
            "        if ($Path -eq $global:AttrCudaTestExpectedLinkPath) {\n"
            "            [pscustomobject]@{ VolumeSerialNumber = 1; FileIndexHigh = 999; FileIndexLow = 999; NumberOfLinks = 2 }\n"
            "        } else {\n"
            "            [pscustomobject]@{ VolumeSerialNumber = 1; FileIndexHigh = 1; FileIndexLow = 1; NumberOfLinks = 1 }\n"
            "        }\n"
            "    }\n"
            "}\n"
            "try { [void](New-AttrCudaOwnerFootageLink -Directory $directory -Index 0 -SourcePath $sourcePath); Write-Output 'NO_THROW' }\n"
            "catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("THREW OWNER_FOOTAGE_LINK_FAILED", proc.stdout)

    def test_a_handle_acquisition_failure_after_the_first_link_cleans_up_and_refuses(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 5 (astra major). Part 0 links and its handle opens
        # normally; part 1's handle acquisition is synthetically failed AFTER part 1's own link
        # was already created. Proves the round-5 enclosing try/catch (not a per-part try around
        # New-AttrCudaOwnerFootageLink alone) closes the part-0 handle, deletes BOTH neutral link
        # entries, leaves the real sources untouched, and reports the typed refusal.
        base_extension = "." + "MLV"
        continuation_extension = "." + "M00"
        src_dir = self.tmp / "owner-handle-failure-source"
        src_dir.mkdir()
        part0_path = src_dir / ("source" + base_extension)
        part0_bytes = b"handle failure cleanup part zero " * 40
        part0_path.write_bytes(part0_bytes)
        part1_path = src_dir / ("source" + continuation_extension)
        part1_bytes = b"handle failure cleanup part one"
        part1_path.write_bytes(part1_bytes)
        parts = [
            {"index": 0, "path": str(part0_path).replace("\\", "/"), "length": len(part0_bytes), "sha256": hashlib.sha256(part0_bytes).hexdigest()},
            {"index": 1, "path": str(part1_path).replace("\\", "/"), "length": len(part1_bytes), "sha256": hashlib.sha256(part1_bytes).hexdigest()},
        ]
        pub = self.tmp / "agent" / "outbox" / "owner-handle-failure.artifacts"
        work = self.tmp / "work"
        pub.mkdir(parents=True)
        work.mkdir(parents=True, exist_ok=True)
        block = self._extract_content_check()
        owner_parts_json = self._owner_parts_json(parts).replace("'", "''")
        script = self.tmp / "owner-handle-failure-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "$FixtureRehearsal = $false\n"
            f"$OwnerPartsJson = '{owner_parts_json}'\n"
            "$ClipId = 'FIX-OWNER-HANDLEFAIL-0001'\n"
            f"$SourceCommit = '{'d' * 40}'\n"
            f"$Pub = '{pub}'\n"
            f"$Work = '{work}'\n"
            "$ownerLinkHandles = [System.Collections.Generic.List[object]]::new()\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            # Real handle for part 0's link; synthetic IOException for part 1's -- the block calls
            # this unqualified at SCRIPT scope (it is spliced in directly, not called from inside
            # the module), so overriding it here is enough without the module-scope mock trick.
            "$script:AttrCudaHandleOpenCount = 0\n"
            "Set-Item -Path function:Open-AttrCudaReadOnlyHandle -Value {\n"
            "    param([string]$Path)\n"
            "    $script:AttrCudaHandleOpenCount++\n"
            "    if ($script:AttrCudaHandleOpenCount -eq 1) {\n"
            "        return [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)\n"
            "    }\n"
            "    throw [IO.IOException]::new('SYNTHETIC_HANDLE_ACQUISITION_FAILURE')\n"
            "}\n"
            + block + "\n"
            "Write-Output 'UNREACHABLE_NO_THROW'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)

        self.assertEqual(proc.returncode, 22, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_LINK_FAILED", proc.stdout)
        self.assertIn("PART=1", proc.stdout)
        self.assertNotIn("UNREACHABLE_NO_THROW", proc.stdout)

        private_dir_name = "owner-" + "clip"
        private_dir = work / private_dir_name
        self.assertTrue(private_dir.exists())
        self.assertEqual(sorted(p.name for p in private_dir.iterdir()), [])

        # The real sources are untouched -- never linked away, never truncated, never renamed.
        self.assertTrue(part0_path.is_file())
        self.assertEqual(part0_path.read_bytes(), part0_bytes)
        self.assertTrue(part1_path.is_file())
        self.assertEqual(part1_path.read_bytes(), part1_bytes)

        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "OWNER_FOOTAGE_LINK_FAILED")
        self.assertEqual(summary["partIndex"], 1)
        dumped = json.dumps(summary)
        self.assertNotIn(str(part0_path).replace("\\", "/"), dumped)
        self.assertNotIn(str(part1_path).replace("\\", "/"), dumped)


@requires_pwsh
class OwnerFootageModuleImportOrderTests(_PwshCase):
    """ATTR3-FOOTAGE-STAGE-1 round 3: AttrCudaOwnerFootage.psm1's own nested
    `Import-Module AttrCudaArtifacts.psm1 -Force` used to strip an already-loaded caller's global
    copy of AttrCudaArtifacts.psm1 out from under it (Get-AttrCudaClosureDirectoryMismatch and
    every other AttrCudaArtifacts export became unrecognized after the nested reimport), because a
    -Force reimport from inside a module being loaded nests the target module under the loader's
    own session state instead of leaving the caller's existing global module alone. Both import
    orders must leave a global, callable copy of BOTH modules' exports behind."""

    def test_artifacts_then_owner_footage_leaves_artifacts_globally_callable(self) -> None:
        script = self.tmp / "order-artifacts-first.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "Write-Output ('MISMATCH_RESULT=' + (Get-AttrCudaClosureDirectoryMismatch "
            "-Dir 'C:\\attr3-no-such-directory' -Entries @(@{name='x';sha256=('a'*64)})))\n"
            "Write-Output ('STAGING_NAME=' + (Get-AttrCudaOwnerFootageStagingName -Index 0))\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("MISMATCH_RESULT=", proc.stdout)
        self.assertIn("STAGING_NAME=part-0", proc.stdout)

    def test_owner_footage_then_artifacts_leaves_artifacts_globally_callable(self) -> None:
        script = self.tmp / "order-owner-first.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"Import-Module '{MODULE}' -Force\n"
            "Write-Output ('MISMATCH_RESULT=' + (Get-AttrCudaClosureDirectoryMismatch "
            "-Dir 'C:\\attr3-no-such-directory' -Entries @(@{name='x';sha256=('a'*64)})))\n"
            "Write-Output ('STAGING_NAME=' + (Get-AttrCudaOwnerFootageStagingName -Index 0))\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("MISMATCH_RESULT=", proc.stdout)
        self.assertIn("STAGING_NAME=part-0", proc.stdout)

    def test_production_import_order_leaves_every_module_globally_callable(self) -> None:
        # The exact sequence attr3-footage-stage.ps1 itself uses: AttrCudaArtifacts.psm1, then
        # AttrCudaOwnerFootage.psm1, then Attr3FootageStageJob.psm1 -- each of the latter two
        # nested-`-Force`-reimports AttrCudaArtifacts.psm1 from inside its own module body, so a
        # fix to only one of them still leaves the third import stripping the global copy again.
        script = self.tmp / "production-order.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"Import-Module '{STAGE_JOB_MODULE}' -Force\n"
            f"Import-Module '{PRESENCE_JOB_MODULE}' -Force\n"
            "Write-Output ('MISMATCH_RESULT=' + (Get-AttrCudaClosureDirectoryMismatch "
            "-Dir 'C:\\attr3-no-such-directory' -Entries @(@{name='x';sha256=('a'*64)})))\n"
            "Write-Output ('STAGING_NAME=' + (Get-AttrCudaOwnerFootageStagingName -Index 0))\n"
            "Write-Output ('SAFE_NAME=' + (Assert-AttrCudaSafeArtifactName -Name 'x.job.ps1'))\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("MISMATCH_RESULT=", proc.stdout)
        self.assertIn("STAGING_NAME=part-0", proc.stdout)
        self.assertIn("SAFE_NAME=x.job.ps1", proc.stdout)

    def test_stage_job_module_first_leaves_artifacts_globally_callable(self) -> None:
        script = self.tmp / "order-stagejob-first.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{STAGE_JOB_MODULE}' -Force\n"
            f"Import-Module '{MODULE}' -Force\n"
            "Write-Output ('MISMATCH_RESULT=' + (Get-AttrCudaClosureDirectoryMismatch "
            "-Dir 'C:\\attr3-no-such-directory' -Entries @(@{name='x';sha256=('a'*64)})))\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("MISMATCH_RESULT=", proc.stdout)

    def test_presence_job_module_first_leaves_artifacts_globally_callable(self) -> None:
        script = self.tmp / "order-presencejob-first.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{PRESENCE_JOB_MODULE}' -Force\n"
            f"Import-Module '{MODULE}' -Force\n"
            "Write-Output ('MISMATCH_RESULT=' + (Get-AttrCudaClosureDirectoryMismatch "
            "-Dir 'C:\\attr3-no-such-directory' -Entries @(@{name='x';sha256=('a'*64)})))\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("MISMATCH_RESULT=", proc.stdout)


@requires_pwsh
class SharedFootagePartVerifierEmbeddingTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B: Test-AttrCudaFootagePart (and the base64 decode it rides on)
    must be the SAME characters in both emitted jobs -- never two copies that can drift."""

    def test_both_generators_embed_the_same_shared_function_names(self) -> None:
        presence_module_text = (
            ROOT / "tools" / "profiling" / "bachelor" / "Attr3FootagePresenceJob.psm1"
        ).read_text(encoding="utf-8")
        attribution_text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        for name in ("Read-AttrCudaBase64Payload", "Test-AttrCudaFootagePart"):
            with self.subTest(name=name):
                self.assertIn(name, presence_module_text)
                self.assertIn(name, attribution_text)

    def test_the_extracted_function_source_is_byte_identical_regardless_of_caller(self) -> None:
        # Get-AttrCudaEmbeddedFunctionSource extracts VERBATIM text keyed only by function name
        # from the ONE module file -- calling it twice with the same names, independent of which
        # job is being generated, always returns the same string. This is the guarantee the two
        # emitted jobs' embeddings rely on.
        proc = self.run_with_module(
            "$a = Get-AttrCudaEmbeddedFunctionSource -Name @('Read-AttrCudaBase64Payload', 'Test-AttrCudaFootagePart')\n"
            "$b = Get-AttrCudaEmbeddedFunctionSource -Name @('Read-AttrCudaBase64Payload', 'Test-AttrCudaFootagePart')\n"
            "Write-Output ('IDENTICAL=' + ($a -ceq $b))\n"
            "Write-Output ('NONEMPTY=' + ($a.Length -gt 0))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("IDENTICAL=True", proc.stdout)
        self.assertIn("NONEMPTY=True", proc.stdout)


def _make_fixture_repo_missing_closure_sibling(path: Path) -> list[str]:
    """Like _make_fixture_repo, but omits ONE smoke-runner closure sibling file
    (gui-smoke-process-boundary.psm1), so Assert-AttrCudaClosureComplete would throw
    ATTRCUDA_BLOB_UNRESOLVED if it ever ran against this commit -- used to prove a resolver
    refusal surfaces even when the closure resolution would ALSO fail (ATTR3-FOOTAGE-BIND-1 PR-B,
    round 1's ordering requirement)."""
    path.mkdir(parents=True, exist_ok=True)
    _git_run(["init", "-q", "-b", "main"], path)
    _git_run(["config", "commit.gpgsign", "false"], path)
    _git_run(["config", "user.email", "lane@example.invalid"], path)
    _git_run(["config", "user.name", "attr3 behaviour fixture"], path)
    (path / "src" / "mlv" / "llrawproc").mkdir(parents=True)
    (path / "tools" / "profiling").mkdir(parents=True)
    (path / "tools" / "profiling" / "run-release-gui-smoke.ps1").write_text(
        "# fixture stand-in for run-release-gui-smoke.ps1\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
        "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
        ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n",
        encoding="utf-8",
    )
    (path / "tools" / "profiling" / "gui-smoke-screenshot-provenance.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "provenance-stamp.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    (path / "tools" / "profiling" / "gui-smoke-color-artifact-scan.ps1").write_text(
        "# fixture stand-in sibling (dot-sourced directly by the runner)\n", encoding="utf-8"
    )
    # Deliberately OMITTED: gui-smoke-process-boundary.psm1.
    shas = []
    for index, text in enumerate(("first", "second")):
        (path / "src" / "mlv" / "llrawproc").mkdir(parents=True, exist_ok=True)
        (path / "src" / "mlv" / "llrawproc" / "llrawproc.c").write_text(
            f"/* fixture revision {text} */\n", encoding="utf-8"
        )
        _git_run(["add", "-A"], path)
        _git_run(["commit", "-q", "-m", f"fixture {index}"], path)
        shas.append(_git_run(["rev-parse", "HEAD"], path))
    return shas


@requires_pwsh
@requires_git
class OwnerClipResolverOrderingTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B, round 1: a resolver refusal for an owner id must surface even
    when the smoke-runner closure resolution would ALSO fail -- the resolver decision runs
    first, so its refusal is never masked by an unrelated closure/build-manifest failure that
    would also have fired had the code reached that far."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo_missing_closure_sibling(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def test_resolver_refusal_surfaces_not_the_closure_failure(self) -> None:
        out_file = self.staging / "job.ps1"
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'M16-1243' "
            f"-OutFile '{out_file}' -RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_OWNER_RESOLVE_REFUSED", message)
        self.assertNotIn("ATTRCUDA_BLOB_UNRESOLVED", message)
        self.assertNotIn("ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE", message)
        self.assertFalse(out_file.exists())

    # round 2 (sol MAJOR / astra MAJOR): round 1 pinned only that a resolver refusal survives
    # a closure failure. Source-commit/blob resolution and the PresentMon hash check sat ahead
    # of the owner decision too, so a resolver refusal could ALSO be masked by an unrelated
    # SourceCommit or PresentMon failure. Both are deliberately wrong here at once.
    def test_resolver_refusal_surfaces_not_bogus_source_commit_or_presentmon_hash(self) -> None:
        out_file = self.staging / "job.ps1"
        script = self.tmp / "generate.ps1"
        bogus_source_commit = "f" * 40  # well-formed 40-hex, not a commit in self.repo
        bad_presentmon_sha = "not-a-valid-presentmon-sha256-value"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{bogus_source_commit}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'M16-1243' "
            f"-PresentMonSha256 '{bad_presentmon_sha}' "
            f"-OutFile '{out_file}' -RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_OWNER_RESOLVE_REFUSED", message)
        self.assertNotIn("is not a commit known to the local repo", message)
        self.assertNotIn("PresentMonSha256 is not a 64-hex sha256", message)
        self.assertFalse(out_file.exists())

    def test_the_closure_would_indeed_fail_on_its_own(self) -> None:
        # Proves this fixture repo genuinely breaks closure resolution -- so the test above is
        # not vacuously green because the closure would have passed anyway.
        proc = self.run_with_module(
            _guard(
                f"Assert-AttrCudaClosureComplete -RepoRoot '{self.repo}' -Commit '{self.shas[1]}'"
            )
        )
        self.assert_throws(proc, "ATTRCUDA_BLOB_UNRESOLVED")


@requires_pwsh
class RepoRootOrderingTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): the owner/fixture-clip FLAG refusals run
    before $RepoRoot is ever resolved. A caller who supplies both a bogus -RepoRoot and a refused
    -ClipPath for an owner id must see PLAYBACK_ATTR3_CLIPPATH_REFUSED, never a RepoRoot error
    that only fires first because RepoRoot resolution used to sit ahead of this classification.
    No git or real repository is needed here: -RepoRoot never resolves far enough to touch one."""

    def setUp(self) -> None:
        super().setUp()
        self.staging = self.tmp / "staging"
        self.staging.mkdir()
        self.bogus_repo_root = self.tmp / "does-not-exist" / "at-all"

    def _generate(self, *, clip_path: str | None = None, clip_path_explicit_empty: bool = False):
        out_file = self.staging / "job.ps1"
        if clip_path_explicit_empty:
            clip_path_args = "-ClipPath '' "
        elif clip_path:
            clip_path_args = f"-ClipPath '{clip_path}' "
        else:
            clip_path_args = ""
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{'a' * 40}' "
            f"-BuildManifestSha256 '{'b' * 64}' -ClipId 'M16-1243' "
            f"{clip_path_args}"
            f"-OutFile '{out_file}' -RepoRoot '{self.bogus_repo_root}'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script), out_file

    def test_a_bogus_reporoot_with_an_owner_id_and_clippath_surfaces_clippath_refused(self) -> None:
        proc, out_file = self._generate(clip_path="C:\\synthetic-cache\\M16-1243")
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_CLIPPATH_REFUSED", message)
        self.assertNotIn("PLAYBACK_ATTR3_REPOROOT_INVALID", message)
        self.assertNotIn(str(self.bogus_repo_root), message)
        self.assertFalse(out_file.exists())

    # ATTR3-FOOTAGE-BIND-1 PR-B round 6 (sol minor): an EXPLICITLY bound empty -ClipPath must be
    # refused the same way, not read as omission and left to fall through to RepoRoot handling.
    def test_a_bogus_reporoot_with_an_owner_id_and_an_explicit_empty_clippath_surfaces_clippath_refused(
        self,
    ) -> None:
        proc, out_file = self._generate(clip_path_explicit_empty=True)
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_CLIPPATH_REFUSED", message)
        self.assertNotIn("PLAYBACK_ATTR3_REPOROOT_INVALID", message)
        self.assertNotIn(str(self.bogus_repo_root), message)
        self.assertFalse(out_file.exists())

    def test_a_bogus_reporoot_alone_surfaces_reporoot_invalid_without_echoing_the_path(self) -> None:
        proc, out_file = self._generate()
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_REPOROOT_INVALID", message)
        self.assertNotIn(str(self.bogus_repo_root), message)
        self.assertFalse(out_file.exists())


@requires_pwsh
class AgentRootOrderingTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 5 (STRUCTURAL), narrowed round 6: -AgentRoot's shape check
    for the OWNER arm now runs after the complete owner-id decision (flag refusals, RepoRoot
    resolution, the resolver call) -- see AgentRootAfterResolverOrderingTests below for that
    behavioural proof, which needs a git fixture the resolver can be pointed at. This class keeps
    the ordering proof that needs no git fixture: a caller who supplies both a malformed
    -AgentRoot and a refused -ClipPath for an owner id must see PLAYBACK_ATTR3_CLIPPATH_REFUSED,
    never PLAYBACK_ATTR3_AGENTROOT_INVALID masking it (the flag refusal never even reaches
    RepoRoot/the resolver), and that a malformed -AgentRoot ALONE, for a FIXTURE id, still throws
    PLAYBACK_ATTR3_AGENTROOT_INVALID -- the fixture arm's own copy of this check is unaffected by
    round 6 and still runs first, needing no RepoRoot resolution or resolver call at all."""

    def setUp(self) -> None:
        super().setUp()
        self.staging = self.tmp / "staging"
        self.staging.mkdir()
        self.bad_agent_root = "not-a-drive-rooted-path"

    def _generate(
        self,
        *,
        clip_id: str = "M16-1243",
        clip_path: str | None = None,
        agent_root: str | None = None,
        fixture_sha256: str | None = None,
    ):
        out_file = self.staging / "job.ps1"
        clip_path_args = f"-ClipPath '{clip_path}' " if clip_path else ""
        agent_root_args = f"-AgentRoot '{agent_root}' " if agent_root is not None else ""
        fixture_sha_args = f"-FixtureSha256 '{fixture_sha256}' " if fixture_sha256 is not None else ""
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{'a' * 40}' "
            f"-BuildManifestSha256 '{'b' * 64}' -ClipId '{clip_id}' "
            f"{clip_path_args}{agent_root_args}{fixture_sha_args}"
            f"-OutFile '{out_file}'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script), out_file

    def test_a_malformed_agentroot_with_an_owner_id_and_clippath_surfaces_clippath_refused(
        self,
    ) -> None:
        proc, out_file = self._generate(
            clip_path="C:\\synthetic-cache\\M16-1243", agent_root=self.bad_agent_root
        )
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_CLIPPATH_REFUSED", message)
        self.assertNotIn("PLAYBACK_ATTR3_AGENTROOT_INVALID", message)
        self.assertFalse(out_file.exists())

    def test_a_malformed_agentroot_alone_for_a_fixture_id_surfaces_agentroot_invalid(self) -> None:
        # A fixture id's own -AgentRoot check is unaffected by round 6 (it still runs before
        # -FixtureSha256/-ClipPath, needing no RepoRoot resolution or resolver call), so this
        # stays deterministic without a git fixture -- unlike the owner arm's copy, which now
        # depends on the resolver call's own outcome (see AgentRootAfterResolverOrderingTests).
        proc, out_file = self._generate(
            clip_id="tiny_dual_iso", agent_root=self.bad_agent_root, fixture_sha256="c" * 64
        )
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_AGENTROOT_INVALID", message)
        self.assertFalse(out_file.exists())


@requires_pwsh
@requires_git
class AgentRootAfterResolverOrderingTests(_PwshCase):
    """ATTR3-FOOTAGE-BIND-1 PR-B round 6 (astra major): the owner arm's -AgentRoot shape check now
    runs AFTER the resolver call, not before it -- round 5 left it inside the same if/else as the
    flag refusals, but still ahead of $RepoRoot resolution and the resolver call, so a malformed
    -AgentRoot masked a resolver refusal that would otherwise have fired first. Uses the same
    fixture-repo seam as OwnerClipResolverOrderingTests (a throwaway repo with no
    refs/remotes/fork/master, which the resolver always refuses deterministically) so the resolver
    outcome never depends on real consent data."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo_missing_closure_sibling(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()
        self.bad_agent_root = "not-a-drive-rooted-path"

    def test_a_malformed_agentroot_with_a_resolver_refusal_surfaces_resolve_refused(self) -> None:
        out_file = self.staging / "job.ps1"
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'M16-1243' "
            f"-AgentRoot '{self.bad_agent_root}' "
            f"-OutFile '{out_file}' -RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PLAYBACK_ATTR3_OWNER_RESOLVE_REFUSED", message)
        self.assertNotIn("PLAYBACK_ATTR3_AGENTROOT_INVALID", message)
        self.assertFalse(out_file.exists())


@requires_pwsh
@requires_git
class FixtureCommittedBytesTests(_PwshCase):
    """Assert-AttrCudaFixtureCommittedBytes, exercised against a throwaway git repository."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.tracked_path = self.repo / "src" / "mlv" / "llrawproc" / "llrawproc.c"

    def test_an_unmodified_tracked_file_is_accepted(self) -> None:
        proc = self.run_with_module(
            f"Write-Output (Assert-AttrCudaFixtureCommittedBytes -Path '{self.tracked_path}')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        expected = _git_run(["hash-object", "--", "src/mlv/llrawproc/llrawproc.c"], self.repo)
        self.assertIn(expected, proc.stdout)

    def test_a_dirtied_working_tree_copy_is_refused(self) -> None:
        self.tracked_path.write_text("/* dirtied after the commit */\n", encoding="utf-8")
        proc = self.run_with_module(_guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{self.tracked_path}'"))
        self.assert_throws(proc, "ATTR3_FIXTURE_WORKING_TREE_DIRTY")

    def test_a_missing_file_is_refused(self) -> None:
        missing = self.repo / "src" / "mlv" / "llrawproc" / "absent.c"
        proc = self.run_with_module(_guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{missing}'"))
        self.assert_throws(proc, "ATTR3_FIXTURE_MISSING")

    def test_a_file_outside_any_git_repository_is_refused(self) -> None:
        outside = self.tmp / "loose.txt"
        outside.write_text("not in a repo\n", encoding="utf-8")
        proc = self.run_with_module(_guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{outside}'"))
        self.assert_throws(proc, "ATTR3_FIXTURE_NOT_IN_A_REPO")


@requires_pwsh
@requires_git
class StageFixtureJobCommittedBytesWiringTests(_PwshCase):
    """attr3-stage-fixture-job.ps1 calls the new check and prints the baked hash."""

    def test_generator_source_calls_the_committed_bytes_check(self) -> None:
        text = STAGE_FIXTURE_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("Assert-AttrCudaFixtureCommittedBytes -Path $fixture.FullName", text)

    def test_generator_prints_a_fixture_sha256_result_line(self) -> None:
        text = STAGE_FIXTURE_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("RESULT=FIXTURE_STAGE_JOB_EMITTED FIXTURE_SHA256=$fixtureSha", text)


# --------------------------------------------------------------------------------------------
# ATTR3-SMOKE-RUNNER-DEPS-1: the runner is not standalone -- it dot-sources three siblings and
# imports a module, all resolved through $PSScriptRoot at runtime. Round 1 staged the runner
# alone (ATTR3-SMOKE-RUNNER-PIN-1) and Bachelor could not even launch it: PresentMon never saw
# a target and PRESENTMON_TIMEOUT masked the real cause. The full dependency CLOSURE is an
# explicitly pinned five-file manifest (Get-AttrCudaSmokeRunnerClosureManifest, resolved via
# Resolve-AttrCudaSmokeRunnerClosure) -- never mechanically derived or scanned for -- staged into
# one content-addressed subdirectory (smoke-runner-<digest16>), and every file in it is
# hash-pinned before launch.
# --------------------------------------------------------------------------------------------

# Expected order for the shared fixture repo's runner (see _make_fixture_repo): the pinned,
# explicit manifest order from Get-AttrCudaSmokeRunnerClosureManifest -- root first, then each
# sibling in the order it is listed in the manifest, never mechanically discovered or scanned
# for (ATTR3-SMOKE-RUNNER-DEPS-1 round 3, NARROW BY REDESIGN).
SMOKE_RUNNER_CLOSURE_NAMES = (
    "run-release-gui-smoke.ps1",
    "gui-smoke-screenshot-provenance.ps1",
    "provenance-stamp.ps1",
    "gui-smoke-process-boundary.psm1",
    "gui-smoke-color-artifact-scan.ps1",
    "gui-smoke-gpu-texture-route-validation.ps1",
)


@requires_pwsh
class SmokeRunnerClosureManifestTests(_PwshCase):
    """Get-AttrCudaSmokeRunnerClosureManifest: the EXPLICIT, pinned list that replaced discovery
    (ATTR3-SMOKE-RUNNER-DEPS-1 round 3, NARROW BY REDESIGN)."""

    def test_the_manifest_is_the_six_pinned_repo_relative_paths_in_order(self) -> None:
        proc = self.run_with_module(
            "Get-AttrCudaSmokeRunnerClosureManifest | ForEach-Object { Write-Output \"PATH=$_\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        paths = [line.split("=", 1)[1] for line in proc.stdout.splitlines() if line.startswith("PATH=")]
        self.assertEqual(
            paths,
            [
                "tools/profiling/run-release-gui-smoke.ps1",
                "tools/profiling/gui-smoke-screenshot-provenance.ps1",
                "tools/profiling/provenance-stamp.ps1",
                "tools/profiling/gui-smoke-process-boundary.psm1",
                "tools/profiling/gui-smoke-color-artifact-scan.ps1",
                "tools/profiling/gui-smoke-gpu-texture-route-validation.ps1",
            ],
        )


@requires_pwsh
class ScriptLoadSiteCensusTests(_PwshCase):
    """Get-AttrCudaScriptLoadSites: the AST census that replaced the literal/regex scanners
    (ATTR3-SMOKE-RUNNER-DEPS-1 round 3). Finds every SITE that can load or generate code,
    regardless of how its target is spelled -- classification is a separate concern
    (ClosureCompletenessTests, below)."""

    def _kinds(self, text: str) -> list[tuple[str, str]]:
        proc = self.run_with_module(
            "$text = @'\n" + text + "\n'@\n"
            "Get-AttrCudaScriptLoadSites -ScriptText $text | "
            "ForEach-Object { Write-Output \"SITE=$($_.kind)|$($_.text)\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = []
        for line in proc.stdout.splitlines():
            if not line.startswith("SITE="):
                continue
            kind, _, text_ = line[len("SITE="):].partition("|")
            out.append((kind, text_))
        return out

    def test_finds_dot_source_and_call_operator_invocations(self) -> None:
        self.assertEqual(
            self._kinds(". (Join-Path $PSScriptRoot 'a.ps1')\n"),
            [("InvocationOperator", ". (Join-Path $PSScriptRoot 'a.ps1')")],
        )
        self.assertEqual(
            self._kinds('& "$PSScriptRoot\\x"\n'),
            [("InvocationOperator", '& "$PSScriptRoot\\x"')],
        )

    def test_finds_a_bareword_import_module_with_no_string_literal(self) -> None:
        # ATTR3-SMOKE-RUNNER-DEPS-1 round 3: exactly the shape a literal-based scanner cannot
        # see -- there is no quoted string here at all for a text scan to match on.
        sites = self._kinds("Import-Module $PSScriptRoot/x\n")
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0][0], "CommandName")

    def test_finds_named_loader_commands(self) -> None:
        for command in (
            "Start-Process notepad.exe",
            "pwsh -File x.ps1",
            "powershell.exe -File x.ps1",
            "Invoke-Expression $code",
            "Invoke-Command -ScriptBlock $sb",
            "Start-Job -ScriptBlock $sb",
            "Add-Type -AssemblyName System.Drawing",
        ):
            with self.subTest(command=command):
                sites = self._kinds(command + "\n")
                self.assertEqual(len(sites), 1, sites)
                self.assertEqual(sites[0][0], "CommandName")

    def test_ignores_an_ordinary_command_that_is_not_a_loader(self) -> None:
        self.assertEqual(self._kinds("Get-ChildItem -Path C:\\x\n"), [])

    def test_finds_a_using_statement(self) -> None:
        self.assertEqual(
            self._kinds("using namespace System.Text\n"),
            [("UsingStatement", "using namespace System.Text")],
        )

    def test_a_csharp_using_keyword_inside_an_add_type_string_is_not_a_powershell_using_statement(self) -> None:
        # The parser never descends into a StringConstantExpressionAst as PowerShell code, so a
        # C# `using System;` sitting inert inside a -TypeDefinition string is not itself a site --
        # only the Add-Type CommandAst that carries the string is.
        text = 'Add-Type -TypeDefinition "using System;\nclass X {}"\n'
        sites = self._kinds(text)
        self.assertEqual([kind for kind, _ in sites], ["CommandName"])

    def test_finds_named_member_calls(self) -> None:
        for member in ("Create", "AddScript", "AddCommand", "InvokeScript", "NewScriptBlock"):
            with self.subTest(member=member):
                sites = self._kinds(f"$x.{member}('foo')\n")
                self.assertEqual(len(sites), 1, sites)
                self.assertEqual(sites[0][0], "MemberCall")

    def test_ignores_an_unrelated_member_call(self) -> None:
        self.assertEqual(self._kinds("$x.ToString()\n"), [])

    def test_throws_a_distinguishable_error_on_a_parse_error(self) -> None:
        proc = self.run_with_module(_guard("Get-AttrCudaScriptLoadSites -ScriptText 'function ('"))
        self.assert_throws(proc, "ATTRCUDA_SCRIPT_PARSE_ERROR")


@requires_pwsh
@requires_git
class ClosureCompletenessTests(_PwshCase):
    """Assert-AttrCudaClosureComplete: the generator-time proof that the pinned manifest still
    matches what the real files load, via the AST census -- never a scan, never trusted at
    runtime. Every negative fixture here is the concrete gap a text/literal scanner left open
    (sol's PR #144 review, carried into the round 3 design swarm)."""

    def _repo_with_runner_text(self, path: Path, runner_text: str) -> str:
        path.mkdir(parents=True, exist_ok=True)
        _git_run(["init", "-q", "-b", "main"], path)
        _git_run(["config", "commit.gpgsign", "false"], path)
        _git_run(["config", "user.email", "lane@example.invalid"], path)
        _git_run(["config", "user.name", "attr3 census fixture"], path)
        (path / "tools" / "profiling").mkdir(parents=True)
        (path / "tools" / "profiling" / "run-release-gui-smoke.ps1").write_text(runner_text, encoding="utf-8")
        for sibling in (
            "gui-smoke-screenshot-provenance.ps1",
            "provenance-stamp.ps1",
            "gui-smoke-color-artifact-scan.ps1",
            "gui-smoke-gpu-texture-route-validation.ps1",
        ):
            (path / "tools" / "profiling" / sibling).write_text(f"# {sibling} stand-in\n", encoding="utf-8")
        (path / "tools" / "profiling" / "gui-smoke-process-boundary.psm1").write_text(
            "# gui-smoke-process-boundary.psm1 stand-in\n", encoding="utf-8"
        )
        _git_run(["add", "-A"], path)
        _git_run(["commit", "-q", "-m", "fixture"], path)
        return _git_run(["rev-parse", "HEAD"], path)

    def _assert_complete(self, repo: Path, sha: str) -> subprocess.CompletedProcess:
        return self.run_with_module(
            _guard(f"Assert-AttrCudaClosureComplete -RepoRoot '{repo}' -Commit '{sha}'")
        )

    def test_the_real_current_repo_at_head_classifies_cleanly(self) -> None:
        """The production manifest and exclusion list, proved against the real, current
        run-release-gui-smoke.ps1 -- the fail-closed census must not itself break the route it
        protects."""
        head = _git_run(["rev-parse", "HEAD"], ROOT)
        proc = self.run_with_module(
            _guard(f"Assert-AttrCudaClosureComplete -RepoRoot '{ROOT}' -Commit '{head}'")
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_all_six_pinned_dependency_loads_classify_cleanly(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_an_absolute_path_sharing_a_basename_is_refused_never_classified_by_basename(self) -> None:
        # Round 2's basename-only classifier would have accepted this; full-path matching refuses it.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". 'C:\\evil\\provenance-stamp.ps1'\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_root_relative_not_psscriptroot_relative_load_is_refused(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $root 'provenance-stamp.ps1')\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_traversal_name_is_refused_never_a_bare_file_name(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot '..\\evil\\provenance-stamp.ps1')\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_an_extensionless_ampersand_load_is_refused(self) -> None:
        # The exact concrete gap this redesign exists to close: no string literal here at all,
        # so Get-AttrCudaScriptFileLiteralReferences (round 2's scanner) found nothing.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            '& "$PSScriptRoot\\helper"\n'
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_bareword_import_module_load_is_refused(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "Import-Module $PSScriptRoot/modx\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_dynamic_invocation_target_is_refused(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "$name = 'Get-Process'\n"
            "& $name\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_parse_error_is_refused(self) -> None:
        repo = self.tmp / "repo"
        sha = self._repo_with_runner_text(repo, "function (\n")
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_SCRIPT_PARSE_ERROR")

    def test_a_manifest_entry_never_loaded_is_a_completeness_mismatch(self) -> None:
        # The manifest names gui-smoke-process-boundary.psm1, but the runner never loads it: the
        # census finds nothing wrong with what IS there, but the manifest is over-complete.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")
        self.assertIn("completeness mismatch", proc.stdout)
        self.assertIn("gui-smoke-process-boundary.psm1", proc.stdout)

    def test_a_scriptblock_typed_parameter_invocation_classifies_cleanly(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            "function f([scriptblock]$p) { & $p 1 }\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_a_scriptblock_only_variable_invocation_classifies_cleanly(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            "$predicate = { param($x) $x -eq 1 }\n"
            "& $predicate 1\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_a_variable_with_a_non_scriptblock_assignment_is_refused(self) -> None:
        # Proves class (b) is not a rubber stamp for every `&`/`.` on a variable: a variable that
        # is EVER assigned something other than a scriptblock literal falls through to unclassified.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "$cmd = 'Get-Process'\n"
            "& $cmd\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_add_type_with_no_path_classifies_cleanly(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            "Add-Type -AssemblyName System.Drawing\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_add_type_with_a_path_is_refused(self) -> None:
        # Class (c) is deliberately narrow: an Add-Type that loads FROM a file is a real load
        # site and must be classified some other way (or excluded), never auto-passed.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "Add-Type -Path 'C:\\some\\extra.cs'\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_the_pinned_exclusion_matches_the_real_dormant_detector_launch(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            "$detectorPwsh = 'pwsh.exe'\n"
            "$detectorArgs = @()\n"
            "$detectorOut = & $detectorPwsh @detectorArgs 2>&1\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_an_exclusion_does_not_widen_to_a_slightly_different_site_text(self) -> None:
        # Keyed on EXACT site text: a near-identical but not-identical invocation must not match.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "$detectorPwsh = 'pwsh.exe'\n"
            "$detectorArgs = @()\n"
            "$detectorOut = & $detectorPwsh @detectorArgs\n"  # no trailing 2>&1
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_module_qualified_import_module_load_classifies_the_same_as_unqualified(self) -> None:
        # ATTR3-SMOKE-RUNNER-DEPS-1 round 4 (sol PR #144 major 1): GetCommandName() returns the
        # QUALIFIED string for a module-qualified invocation, which never matched the census's
        # exact-name loader list. A qualified Import-Module of a pinned sibling must classify
        # exactly like the unqualified form.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            "Microsoft.PowerShell.Core\\Import-Module "
            "(Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_a_module_qualified_import_module_of_an_unpinned_file_is_refused(self) -> None:
        # The positive case above must not become a blanket pass for anything qualified: an extra,
        # unpinned load through the same qualified spelling is still refused.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "Microsoft.PowerShell.Core\\Import-Module (Join-Path $PSScriptRoot 'x.psm1')\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_an_alias_defining_statement_is_always_unclassified(self) -> None:
        # ATTR3-SMOKE-RUNNER-DEPS-1 round 4 (sol PR #144 major 2): an alias's target is
        # undecidable statically, so Set-Alias/sal/New-Alias/nal (qualified or not) is ALWAYS a
        # site the census refuses to classify -- never resolved at census time -- regardless of
        # what the alias is named or what it is aliased to.
        cases = {
            "Set-Alias": "Set-Alias load Import-Module\n",
            "sal": "sal load Import-Module\n",
            "New-Alias": "New-Alias load Import-Module\n",
            "nal": "nal load Import-Module\n",
            "module_qualified": "Microsoft.PowerShell.Utility\\Set-Alias load Import-Module\n",
        }
        for label, alias_line in cases.items():
            with self.subTest(alias=label):
                repo = self.tmp / f"repo-{label}"
                runner = (
                    ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
                    ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
                    "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
                    ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
                    + alias_line
                )
                sha = self._repo_with_runner_text(repo, runner)
                proc = self._assert_complete(repo, sha)
                self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_add_type_with_an_abbreviated_path_parameter_is_refused(self) -> None:
        # ATTR3-SMOKE-RUNNER-DEPS-1 round 4 (sol PR #144 major 3): PowerShell binds any
        # unambiguous parameter-name PREFIX, so `-Pat`/`-Lit` reach -Path/-LiteralPath at runtime
        # even though the old exact `-ieq` comparison read them as pathless.
        for abbreviation in ("-Pat", "-Lit", "-PSPath", "-LP"):
            with self.subTest(abbreviation=abbreviation):
                repo = self.tmp / f"repo-{abbreviation.strip('-')}"
                runner = (
                    ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
                    ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
                    "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
                    ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
                    f"Add-Type {abbreviation} 'C:\\some\\extra.cs'\n"
                )
                sha = self._repo_with_runner_text(repo, runner)
                proc = self._assert_complete(repo, sha)
                self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")

    def test_a_pinned_process_start_call_classifies_cleanly(self) -> None:
        # fable's round-3 minor, fixed round 4: the exact real site (a static
        # [System.Diagnostics.Process]::Start($startInfo) launching the deployed exe) is the one
        # pinned class-(d) exclusion; it must classify without needing $startInfo to be provable.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            "$startInfo = [System.Diagnostics.ProcessStartInfo]::new()\n"
            "$process = [System.Diagnostics.Process]::Start($startInfo)\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)

    def test_an_unpinned_process_start_call_is_refused(self) -> None:
        # The exclusion is keyed on EXACT site text: a different argument expression is a
        # different, unreviewed site and must not silently match the pinned one.
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            "$otherStartInfo = [System.Diagnostics.ProcessStartInfo]::new()\n"
            "$process = [System.Diagnostics.Process]::Start($otherStartInfo)\n"
        )
        sha = self._repo_with_runner_text(repo, runner)
        proc = self._assert_complete(repo, sha)
        self.assert_throws(proc, "ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE")


@requires_pwsh
@requires_git
class SmokeRunnerClosureResolutionTests(_PwshCase):
    """Resolve-AttrCudaSmokeRunnerClosure: resolution only, against the pinned manifest. No scan,
    no recursion (ATTR3-SMOKE-RUNNER-DEPS-1 round 3) -- proved independent of what the runner's
    own text says by pointing it at a fixture repo whose runner dot-sources nothing at all."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)

    def test_resolves_exactly_the_pinned_manifest_in_order(self) -> None:
        proc = self.run_with_module(
            f"$closure = @(Resolve-AttrCudaSmokeRunnerClosure -RepoRoot '{self.repo}' "
            f"-Commit '{self.shas[1]}')\n"
            "$closure | ForEach-Object { Write-Output \"NAME=$($_.name)\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = [line.split("=", 1)[1] for line in proc.stdout.splitlines() if line.startswith("NAME=")]
        self.assertEqual(names, list(SMOKE_RUNNER_CLOSURE_NAMES))

    def test_resolution_does_not_depend_on_the_runners_own_text(self) -> None:
        # The exact behavioural break from round 1/2: resolution is unaffected even when the
        # runner file dot-sources nothing recognizable at all -- there is nothing left to scan.
        (self.repo / "tools" / "profiling" / "run-release-gui-smoke.ps1").write_text(
            "Write-Output 'no loads at all, on purpose'\n", encoding="utf-8"
        )
        _git_run(["add", "-A"], self.repo)
        _git_run(["commit", "-q", "-m", "rewrite runner with no loads"], self.repo)
        sha = _git_run(["rev-parse", "HEAD"], self.repo)

        proc = self.run_with_module(
            f"$closure = @(Resolve-AttrCudaSmokeRunnerClosure -RepoRoot '{self.repo}' -Commit '{sha}')\n"
            "$closure | ForEach-Object { Write-Output \"NAME=$($_.name)\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = [line.split("=", 1)[1] for line in proc.stdout.splitlines() if line.startswith("NAME=")]
        self.assertEqual(names, list(SMOKE_RUNNER_CLOSURE_NAMES))

    def test_each_entry_carries_its_own_committed_blob_sha256(self) -> None:
        proc = self.run_with_module(
            f"$closure = @(Resolve-AttrCudaSmokeRunnerClosure -RepoRoot '{self.repo}' "
            f"-Commit '{self.shas[1]}')\n"
            "$closure | ForEach-Object { Write-Output \"$($_.name)=$($_.sha256)\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in SMOKE_RUNNER_CLOSURE_NAMES:
            expected = _git_blob_sha256(self.repo, self.shas[1], f"tools/profiling/{name}")
            with self.subTest(name=name):
                self.assertIn(f"{name}={expected}", proc.stdout)


@requires_pwsh
class ClosureDirectoryMismatchTests(_PwshCase):
    """Get-AttrCudaClosureDirectoryMismatch: the ONE shared exact-set definition both emitted
    jobs now embed (ATTR3-SMOKE-RUNNER-DEPS-1 round 3) -- replacing two inline copies that could
    silently drift apart."""

    def _entries_literal(self, entries: dict[str, str]) -> str:
        return "@(" + ",".join(
            f"[pscustomobject]@{{ name = '{name}'; sha256 = '{sha256}' }}" for name, sha256 in entries.items()
        ) + ")"

    def _write(self, directory: Path, contents: dict[str, bytes]) -> dict[str, str]:
        directory.mkdir(parents=True, exist_ok=True)
        shas = {}
        for name, data in contents.items():
            (directory / name).write_bytes(data)
            shas[name] = hashlib.sha256(data).hexdigest()
        return shas

    def test_an_exact_match_returns_null(self) -> None:
        directory = self.tmp / "dir"
        shas = self._write(directory, {"a.ps1": b"a bytes", "b.ps1": b"b bytes"})
        proc = self.run_with_module(
            f"$result = Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
            f"-Entries {self._entries_literal(shas)}\n"
            "Write-Output \"RESULT=[$result]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT=[]", proc.stdout)

    def test_a_missing_directory_is_a_mismatch(self) -> None:
        directory = self.tmp / "does-not-exist"
        proc = self.run_with_module(
            f"$result = Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
            f"-Entries {self._entries_literal({'a.ps1': 'a' * 64})}\n"
            "Write-Output \"RESULT=[$result]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("missing closure directory", proc.stdout)

    def test_an_extra_file_is_a_mismatch(self) -> None:
        directory = self.tmp / "dir"
        shas = self._write(directory, {"a.ps1": b"a bytes"})
        (directory / "extra.ps1").write_bytes(b"unexpected")
        proc = self.run_with_module(
            f"$result = Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
            f"-Entries {self._entries_literal(shas)}\n"
            "Write-Output \"RESULT=[$result]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("entries, expected", proc.stdout)

    def test_an_extra_subdirectory_is_a_mismatch(self) -> None:
        # Required test (D): "extra subdirectory", distinct from "extra file".
        directory = self.tmp / "dir"
        shas = self._write(directory, {"a.ps1": b"a bytes"})
        (directory / "nested").mkdir()
        proc = self.run_with_module(
            f"$result = Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
            f"-Entries {self._entries_literal(shas)}\n"
            "Write-Output \"RESULT=[$result]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("entries, expected", proc.stdout)

    def test_a_missing_entry_is_named(self) -> None:
        # Named specifically (not just "wrong count"): every expected entry is checked by name
        # BEFORE the broader extra-entries sweep, so a refusal always says WHICH file is absent.
        directory = self.tmp / "dir"
        self._write(directory, {"a.ps1": b"a bytes"})
        proc = self.run_with_module(
            f"$result = Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
            f"-Entries {self._entries_literal({'a.ps1': hashlib.sha256(b'a bytes').hexdigest(), 'missing.ps1': 'b' * 64})}\n"
            "Write-Output \"RESULT=[$result]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("is missing missing.ps1", proc.stdout)

    def test_a_hash_mismatch_is_named(self) -> None:
        directory = self.tmp / "dir"
        self._write(directory, {"a.ps1": b"tampered bytes"})
        wrong_sha = hashlib.sha256(b"the real bytes").hexdigest()
        proc = self.run_with_module(
            f"$result = Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
            f"-Entries {self._entries_literal({'a.ps1': wrong_sha})}\n"
            "Write-Output \"RESULT=[$result]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sha256 mismatch", proc.stdout)

    def test_never_throws_on_a_mismatch(self) -> None:
        directory = self.tmp / "does-not-exist"
        proc = self.run_with_module(
            _guard(
                f"Get-AttrCudaClosureDirectoryMismatch -Dir '{directory}' "
                f"-Entries {self._entries_literal({'a.ps1': 'a' * 64})}"
            )
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NO_THROW", proc.stdout)


@requires_pwsh
class SmokeRunnerStaleRefusalTests(_PwshCase):
    """The attribution job's pre-flight closure-pin check, run standalone.

    Mirrors AttributionJobFixtureContentAuthenticationTests's approach: the check is inline
    top-level code in the generator's $template text, not a named module function, so it is
    sliced VERBATIM out of the generator source and run with just the couple of variables it
    reads. This never touches $Root/$Work/-AgentRoot/C:\\mlvtmp, so it needs no fabricated
    build manifest, package or PresentMon binary to reach -- exactly the code that decides
    ATTRCUDA_SMOKE_RUNNER_STALE, and nothing else.

    ATTR3-SMOKE-RUNNER-DEPS-1 round 3: the check is now ONE call into the shared, embedded
    Get-AttrCudaClosureDirectoryMismatch -- so this class also proves NEW behaviour the old
    per-member loop never had: an EXTRA file or subdirectory in the closure cache now fails
    closed here too, not just in the stager's "already staged" check.
    """

    def _extract_pin_check(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start_marker = "$smokeRunnerClosureDir = Join-Path $Cache $SmokeRunnerClosureDirName"
        end_marker = "\n# Fixture runs open the cached clip named at generation time"
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        self.assertGreater(end, start, "smoke-runner pin check markers moved in the generator")
        return text[start:end]

    def _closure_literal(self, closure) -> str:
        return "@(" + ",".join(
            f"[pscustomobject]@{{ name = '{name}'; sha256 = '{sha256}' }}" for name, sha256 in closure
        ) + ")"

    def _run_pin_check(
        self, *, cache: Path, closure_dir_name: str, closure
    ) -> subprocess.CompletedProcess:
        block = self._extract_pin_check()
        script = self.tmp / "pin-check-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            # ATTR3-SMOKE-RUNNER-DEPS-1 round 3: the pin check block now calls the shared
            # Get-AttrCudaClosureDirectoryMismatch (which itself calls
            # Test-AttrCudaPathIsReparsePoint); imported here so the sliced-out block resolves
            # both the same way the emitted job does.
            f"Import-Module '{MODULE}' -Force\n"
            f"$Cache = '{cache}'\n"
            f"$SmokeRunnerClosureDirName = '{closure_dir_name}'\n"
            f"$SmokeRunnerClosure = {self._closure_literal(closure)}\n"
            + block + "\n"
            "Write-Output 'RESULT=NO_REFUSAL'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_a_missing_closure_directory_is_refused(self) -> None:
        cache = self.tmp / "cache"
        cache.mkdir()
        closure = [("run-release-gui-smoke.ps1", hashlib.sha256(b"x").hexdigest())]

        proc = self._run_pin_check(cache=cache, closure_dir_name="smoke-runner-deadbeefdeadbeef", closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("missing closure directory", combined, combined)

    def test_a_stale_or_swapped_file_in_the_closure_is_refused_and_named(self) -> None:
        cache = self.tmp / "cache"
        dir_name = "smoke-runner-deadbeefdeadbeef"
        closure_dir = cache / dir_name
        closure_dir.mkdir(parents=True)
        pinned_sha = hashlib.sha256(b"the real, current, tracked runner bytes").hexdigest()
        (closure_dir / "run-release-gui-smoke.ps1").write_bytes(b"# a stale pre-f401bf9a copy\n")
        closure = [("run-release-gui-smoke.ps1", pinned_sha)]

        proc = self._run_pin_check(cache=cache, closure_dir_name=dir_name, closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("sha256 mismatch", combined, combined)
        self.assertIn("run-release-gui-smoke.ps1", combined, combined)
        self.assertNotIn("RESULT=NO_REFUSAL", proc.stdout)

    def test_a_missing_single_dependency_is_refused_and_named_even_when_the_runner_is_present(self) -> None:
        # The directory exists and the FIRST file is correct; only a later dependency is absent.
        # A whole-directory existence check would have missed this; the loop must check each file.
        cache = self.tmp / "cache"
        dir_name = "smoke-runner-deadbeefdeadbeef"
        closure_dir = cache / dir_name
        closure_dir.mkdir(parents=True)
        runner_bytes = b"runner bytes"
        runner_sha = hashlib.sha256(runner_bytes).hexdigest()
        (closure_dir / "run-release-gui-smoke.ps1").write_bytes(runner_bytes)
        missing_sha = hashlib.sha256(b"a dependency that never got staged").hexdigest()
        closure = [
            ("run-release-gui-smoke.ps1", runner_sha),
            ("gui-smoke-screenshot-provenance.ps1", missing_sha),
        ]

        proc = self._run_pin_check(cache=cache, closure_dir_name=dir_name, closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("is missing gui-smoke-screenshot-provenance.ps1", combined, combined)

    def test_the_fully_pinned_closure_passes_the_gate(self) -> None:
        cache = self.tmp / "cache"
        dir_name = "smoke-runner-deadbeefdeadbeef"
        closure_dir = cache / dir_name
        closure_dir.mkdir(parents=True)
        closure = []
        for name in SMOKE_RUNNER_CLOSURE_NAMES:
            content = f"# {name} bytes\n".encode("utf-8")
            (closure_dir / name).write_bytes(content)
            closure.append((name, hashlib.sha256(content).hexdigest()))

        proc = self._run_pin_check(cache=cache, closure_dir_name=dir_name, closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertNotIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("RESULT=NO_REFUSAL", proc.stdout, combined)

    def test_a_reparse_point_closure_directory_is_refused_even_when_its_target_has_matching_bytes(self) -> None:
        """ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 2): Test-Path/Get-FileHash resolve
        THROUGH a reparse point, so without an explicit check a junction whose TARGET happens to
        carry the pinned bytes would pass every existing check. Refused before the hash is ever
        read."""
        cache = self.tmp / "cache"
        cache.mkdir()
        dir_name = "smoke-runner-deadbeefdeadbeef"
        real_dir = self.tmp / "real-target"
        real_dir.mkdir(parents=True)
        content = b"the real, current, tracked runner bytes"
        (real_dir / "run-release-gui-smoke.ps1").write_bytes(content)
        junction_path = cache / dir_name
        junction = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"New-Item -ItemType Junction -Path '{junction_path}' -Target '{real_dir}'"],
            capture_output=True, text=True,
        )
        self.assertEqual(junction.returncode, 0, junction.stdout + junction.stderr)
        closure = [("run-release-gui-smoke.ps1", hashlib.sha256(content).hexdigest())]

        proc = self._run_pin_check(cache=cache, closure_dir_name=dir_name, closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("is a reparse point", combined, combined)
        self.assertNotIn("RESULT=NO_REFUSAL", proc.stdout)

    def test_an_extra_file_in_the_closure_directory_is_now_refused(self) -> None:
        # ATTR3-SMOKE-RUNNER-DEPS-1 round 3: NEW behaviour -- the old per-member loop only ever
        # checked that each expected file was present and correct; it never noticed an EXTRA
        # file sitting alongside them. The shared exact-set function closes that gap here too.
        cache = self.tmp / "cache"
        dir_name = "smoke-runner-deadbeefdeadbeef"
        closure_dir = cache / dir_name
        closure_dir.mkdir(parents=True)
        content = b"runner bytes"
        (closure_dir / "run-release-gui-smoke.ps1").write_bytes(content)
        (closure_dir / "unexpected-extra-file.txt").write_bytes(b"not part of the closure")
        closure = [("run-release-gui-smoke.ps1", hashlib.sha256(content).hexdigest())]

        proc = self._run_pin_check(cache=cache, closure_dir_name=dir_name, closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("entries, expected", combined, combined)
        self.assertNotIn("RESULT=NO_REFUSAL", proc.stdout)

    def test_an_extra_subdirectory_in_the_closure_directory_is_refused(self) -> None:
        # Required test (D): "extra subdirectory", distinct from "extra file" above.
        cache = self.tmp / "cache"
        dir_name = "smoke-runner-deadbeefdeadbeef"
        closure_dir = cache / dir_name
        closure_dir.mkdir(parents=True)
        content = b"runner bytes"
        (closure_dir / "run-release-gui-smoke.ps1").write_bytes(content)
        (closure_dir / "nested").mkdir()
        closure = [("run-release-gui-smoke.ps1", hashlib.sha256(content).hexdigest())]

        proc = self._run_pin_check(cache=cache, closure_dir_name=dir_name, closure=closure)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("ATTRCUDA_SMOKE_RUNNER_STALE", combined, combined)
        self.assertIn("entries, expected", combined, combined)
        self.assertNotIn("RESULT=NO_REFUSAL", proc.stdout)


@requires_pwsh
@requires_git
class SmokeRunnerStageJobTests(_PwshCase):
    """attr3-stage-smoke-runner-job.ps1: the FULL dependency closure in, one content-addressed
    cache DIRECTORY out, every file's sha256 == its committed blob.

    ATTR3-SMOKE-RUNNER-DEPS-1: staging the runner alone left Bachelor unable to launch it at all
    (round 1 PRESENTMON_TIMEOUT). The closure is the explicitly pinned five-file manifest
    (Get-AttrCudaSmokeRunnerClosureManifest) -- never mechanically derived or scanned for; the
    shared fixture repo's $PSScriptRoot loads (see _make_fixture_repo) exist to prove the pinned
    list still matches what the real files load, not to discover the list.

    No side file, no inbox: the emitted job carries every closure file's bytes INLINE (base64),
    exactly as the single-file stager did (ATTR3-SMOKE-RUNNER-PIN-1 round 2) -- there is no inbox
    in this design at all, so the agent root here only ever needs to exist.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()
        self.agent = self.tmp / "agent"
        self.agent.mkdir()

    def _generate(self, **overrides) -> subprocess.CompletedProcess:
        args = {
            "SourceCommit": self.shas[1],
            "OutDir": str(self.staging),
            "AgentRoot": str(self.agent),
            "RepoRoot": str(self.repo),
        }
        args.update(overrides)
        parts = [f"-{key} '{value}'" for key, value in args.items()]
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{SMOKE_RUNNER_STAGE_GENERATOR}' " + " ".join(parts) + " | ConvertTo-Json -Depth 5\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def _expected_closure_digest16(self, commit: str) -> str:
        entries = []
        for name in SMOKE_RUNNER_CLOSURE_NAMES:
            entries.append((name, _git_blob_sha256(self.repo, commit, f"tools/profiling/{name}")))
        lines = sorted(f"{sha}  {name}" for name, sha in entries)
        digest = hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
        return digest[:16]

    def test_generator_stages_the_full_closure_under_a_content_addressed_directory(self) -> None:
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # The generator both Write-Output's a RESULT= line and returns the pscustomobject; piped
        # through ConvertTo-Json that is an array of the two -- the object is the last element.
        payload = json.loads(proc.stdout)[-1]

        expected_digest16 = self._expected_closure_digest16(self.shas[1])
        self.assertEqual(payload["cacheDirName"], f"smoke-runner-{expected_digest16}")

        run_proc = _run_job(Path(payload["jobFile"]))
        self.assertEqual(run_proc.returncode, 0, run_proc.stdout + run_proc.stderr)
        self.assertIn("RESULT=SMOKE_RUNNER_STAGE_OK", run_proc.stdout)

        cache_dir = self.agent / "cache" / f"smoke-runner-{expected_digest16}"
        self.assertTrue(cache_dir.is_dir())
        for name in SMOKE_RUNNER_CLOSURE_NAMES:
            staged = cache_dir / name
            with self.subTest(name=name):
                self.assertTrue(staged.is_file(), f"{name} was not staged")
                expected_sha = _git_blob_sha256(self.repo, self.shas[1], f"tools/profiling/{name}")
                self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(), expected_sha)
        self.assertFalse((self.agent / "inbox").exists(), "no inbox should ever be created")

    def test_agent_root_shaped_like_a_template_token_is_not_re_expanded(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL). -AgentRoot's own ValidatePattern
        # admits underscores, so a value shaped like '...__EMBEDDED_FUNCTIONS__' used to collide
        # with the LATER embedded-function substitution in the old chained .Replace() calls,
        # splicing verifier source into the middle of the $AgentRoot string literal and breaking
        # the emitted job's own parse. A single-pass Expand-AttrCudaTemplate substitution cannot
        # do that regardless of which token a caller-controlled value happens to spell.
        colliding_agent_root = str(self.agent) + "__EMBEDDED_FUNCTIONS__"
        proc = self._generate(AgentRoot=colliding_agent_root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]
        job_text = Path(payload["jobFile"]).read_text(encoding="utf-8")
        self.assertIn(f"$AgentRoot = '{colliding_agent_root}'", job_text)
        parse = subprocess.run(
            [
                PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "$t=$null; $e=$null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{payload['jobFile']}', [ref]$t, [ref]$e); "
                "Write-Output $e.Count",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(parse.stdout.strip(), "0", parse.stdout + parse.stderr)

    def test_generation_refuses_when_a_pinned_sibling_is_no_longer_referenced(self) -> None:
        """ATTR3-SMOKE-RUNNER-DEPS-1 round 3: the stage generator calls
        Assert-AttrCudaClosureComplete before it ever resolves or stages anything, so rewriting
        the runner to stop referencing a pinned sibling is refused at GENERATION time -- never
        silently staged as an incomplete closure. (Pure resolution's independence from the
        runner's own text is proved separately, at the module level, in
        SmokeRunnerClosureResolutionTests.test_resolution_does_not_depend_on_the_runners_own_text.)
        """
        (self.repo / "tools" / "profiling" / "run-release-gui-smoke.ps1").write_text(
            "Write-Output 'no loads at all, on purpose'\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "rewrite runner with no loads"],
            check=True, capture_output=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        proc = self._generate(SourceCommit=sha)

        self.assertNotEqual(proc.returncode, 0)
        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertIn("ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE", combined)
        self.assertIn("completeness mismatch", combined)
        self.assertFalse((self.agent / "cache").exists(), "nothing should be staged on a refused generation")

    def test_an_extra_subdirectory_in_the_cache_dir_is_not_already_staged(self) -> None:
        # Required test (D): "extra subdirectory", distinct from the existing extra-FILE case
        # (test_an_extra_file_in_the_directory_is_not_already_staged, below).
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]

        first_run = _run_job(Path(payload["jobFile"]))
        self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)

        cache_dir = self.agent / "cache" / payload["cacheDirName"]
        (cache_dir / "unexpected-nested-dir").mkdir()

        second_run = _run_job(Path(payload["jobFile"]))

        self.assertEqual(second_run.returncode, 21, second_run.stdout + second_run.stderr)
        self.assertIn("DIFFERENT content", normalize_pwsh_message_text(second_run.stdout + second_run.stderr))

    def test_a_staged_runner_dot_sources_its_sibling_from_the_staged_directory(self) -> None:
        """Required test: run a synthetic runner that dot-sources a sibling from $PSScriptRoot
        out of the staged directory -- directly disproving the round-1 failure mode (the runner
        died at its own dot-source line because its siblings were never staged alongside it)."""
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]
        run_proc = _run_job(Path(payload["jobFile"]))
        self.assertEqual(run_proc.returncode, 0, run_proc.stdout + run_proc.stderr)

        staged_runner = self.agent / "cache" / payload["cacheDirName"] / "run-release-gui-smoke.ps1"
        self.assertTrue(staged_runner.is_file())
        launch = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(staged_runner)],
            capture_output=True, text=True,
        )
        combined = normalize_pwsh_message_text(launch.stdout + launch.stderr)
        self.assertEqual(launch.returncode, 0, combined)
        self.assertNotIn("is not recognized", combined, combined)

    def test_concurrent_invocations_of_the_same_job_use_distinct_partial_directories(self) -> None:
        # ATTR3-SMOKE-RUNNER-DEPS-1 round 4 (sol PR #144 minor): every same-name stager used to
        # share ONE deterministic partial directory name, so two overlapping invocations of the
        # SAME emitted job could remove-and-recreate each other's not-yet-complete scratch
        # directory. Proved two ways: the emitted job text computes the partial suffix from a
        # GUID generated at RUNTIME (never baked in once by the generator, which is what would
        # let two invocations of the identical job file still collide), and two actual runs of
        # the identical job file report two different partial directory names.
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]
        job_path = Path(payload["jobFile"])
        text = job_path.read_text(encoding="utf-8")

        self.assertIn("[Guid]::NewGuid()", text, "the partial suffix must be generated at job runtime")

        marker = (
            '$partialDirPath = Assert-AttrCudaDirectChild -Root $Cache -Path '
            '(Join-Path $Cache "$CacheDirName.partial-$partialDirSuffix") '
            '-Label "cache/$CacheDirName.partial-$partialDirSuffix"'
        )
        self.assertIn(marker, text)
        instrumented = text.replace(marker, marker + '\nWrite-Output "PARTIAL_DIR=$partialDirPath"')
        self.assertNotEqual(instrumented, text)

        instrumented_path = self.tmp / "instrumented.job.ps1"
        partial_dirs = []
        for _ in range(2):
            instrumented_path.write_text(instrumented, encoding="utf-8")
            run_proc = _run_job(instrumented_path)
            self.assertEqual(run_proc.returncode, 0, run_proc.stdout + run_proc.stderr)
            lines = [line for line in run_proc.stdout.splitlines() if line.startswith("PARTIAL_DIR=")]
            self.assertEqual(len(lines), 1, run_proc.stdout)
            partial_dirs.append(lines[0].split("=", 1)[1])
            # Remove the published cache dir between runs so the second run rebuilds fresh
            # (rather than short-circuiting through Complete-AlreadyStaged, which never computes
            # a new partial directory at all) and its own partial-suffix computation is exercised
            # again, not skipped.
            shutil.rmtree(self.agent / "cache" / payload["cacheDirName"])

        self.assertEqual(len(partial_dirs), 2)
        self.assertNotEqual(partial_dirs[0], partial_dirs[1])

    def test_a_different_commit_stages_that_commits_bytes_and_a_different_directory_name(self) -> None:
        proc = self._generate(SourceCommit=self.shas[0])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]

        first_digest16 = self._expected_closure_digest16(self.shas[0])
        second_digest16 = self._expected_closure_digest16(self.shas[1])
        # The runner/dependency bytes are identical across both fixture commits (only
        # llrawproc.c differs between them), so the two directory names being EQUAL here is
        # the correct, expected behaviour -- content-addressing, not commit-addressing.
        self.assertEqual(first_digest16, second_digest16)
        self.assertEqual(payload["cacheDirName"], f"smoke-runner-{first_digest16}")

    def test_a_tampered_embedded_payload_is_refused_and_nothing_is_published(self) -> None:
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]
        job_path = Path(payload["jobFile"])
        text = job_path.read_text(encoding="utf-8")
        marker = "base64 = '"
        start = text.index(marker) + len(marker)
        end = text.index("'", start)
        tampered_b64 = base64.b64encode(b"swapped after the hash was baked").decode("ascii")
        job_path.write_text(text[:start] + tampered_b64 + text[end:], encoding="utf-8")

        run_proc = _run_job(job_path)

        self.assertEqual(run_proc.returncode, 4, run_proc.stdout + run_proc.stderr)
        self.assertFalse(
            (self.agent / "cache" / payload["cacheDirName"]).exists(),
            "a hash-mismatched payload must publish nothing at all",
        )

    def test_the_emitted_job_carries_every_closure_file_inline_with_no_side_file_instruction(self) -> None:
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]
        text = Path(payload["jobFile"]).read_text(encoding="utf-8")

        for match in re.finditer(r"sha256 = '([0-9a-f]{64})'; base64 = '([^']*)'", text):
            expected_sha, embedded_b64 = match.group(1), match.group(2)
            decoded = base64.b64decode(embedded_b64)
            self.assertEqual(hashlib.sha256(decoded).hexdigest(), expected_sha)

        self.assertNotIn("-SideFile", text)
        self.assertNotIn("$Inbox", text)
        self.assertNotIn("$side", text)

    def test_staging_fails_closed_when_the_directory_holds_different_content(self) -> None:
        """A different existing directory under the same content-addressed name fails closed."""
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]
        cache_dir = self.agent / "cache"
        closure_dir = cache_dir / payload["cacheDirName"]
        closure_dir.mkdir(parents=True)
        corrupted_bytes = b"corrupted -- different bytes under the content-addressed name"
        (closure_dir / "run-release-gui-smoke.ps1").write_bytes(corrupted_bytes)

        run_proc = _run_job(Path(payload["jobFile"]))

        self.assertEqual(run_proc.returncode, 21, run_proc.stdout + run_proc.stderr)
        self.assertIn("DIFFERENT content", normalize_pwsh_message_text(run_proc.stdout + run_proc.stderr))
        self.assertEqual((closure_dir / "run-release-gui-smoke.ps1").read_bytes(), corrupted_bytes)

    def test_identical_existing_content_counts_as_already_staged(self) -> None:
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]

        first_run = _run_job(Path(payload["jobFile"]))
        self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)
        self.assertNotIn("ALREADY=1", first_run.stdout)

        second_run = _run_job(Path(payload["jobFile"]))
        self.assertEqual(second_run.returncode, 0, second_run.stdout + second_run.stderr)
        self.assertIn("RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1", second_run.stdout)

    def test_an_extra_file_in_the_directory_is_not_already_staged(self) -> None:
        """ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 2): every pinned file present and
        correct, PLUS one extra file, must NOT count as already-staged -- "already staged" means
        the directory is EXACTLY the expected closure, no extra entries."""
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]

        first_run = _run_job(Path(payload["jobFile"]))
        self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)

        cache_dir = self.agent / "cache" / payload["cacheDirName"]
        (cache_dir / "unexpected-extra-file.txt").write_bytes(b"not part of the closure")

        second_run = _run_job(Path(payload["jobFile"]))

        self.assertEqual(second_run.returncode, 21, second_run.stdout + second_run.stderr)
        self.assertIn("DIFFERENT content", normalize_pwsh_message_text(second_run.stdout + second_run.stderr))

    def test_a_reparse_point_at_the_closure_directory_is_not_already_staged(self) -> None:
        """ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 2): a junction whose target is a
        byte-identical mirror of the closure must still fail closed -- "already staged" refuses
        a link at the directory itself, not just wrong bytes."""
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]

        first_run = _run_job(Path(payload["jobFile"]))
        self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)

        cache_dir = self.agent / "cache" / payload["cacheDirName"]
        mirror_dir = self.tmp / "mirror-of-cache-dir"
        shutil.copytree(cache_dir, mirror_dir)
        shutil.rmtree(cache_dir)
        junction = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"New-Item -ItemType Junction -Path '{cache_dir}' -Target '{mirror_dir}'"],
            capture_output=True, text=True,
        )
        self.assertEqual(junction.returncode, 0, junction.stdout + junction.stderr)

        second_run = _run_job(Path(payload["jobFile"]))

        self.assertEqual(second_run.returncode, 21, second_run.stdout + second_run.stderr)
        self.assertIn("DIFFERENT content", normalize_pwsh_message_text(second_run.stdout + second_run.stderr))

    def test_verify_only_stops_before_anything_is_written(self) -> None:
        proc = self._generate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)[-1]

        run_proc = _run_job(Path(payload["jobFile"]), "-VerifyOnly")

        self.assertEqual(run_proc.returncode, 0, run_proc.stdout + run_proc.stderr)
        self.assertIn("RESULT=VERIFY_ONLY_OK", run_proc.stdout)
        self.assertFalse((self.agent / "cache").exists(), "-VerifyOnly must not create the cache")


@requires_pwsh
class SmokeRunFailedPresentMonCleanupTests(_PwshCase):
    """ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 3), EXECUTED rather than merely asserted as
    text ordering: PresentMon must actually be stopped -- confirmed exited by re-reading the
    process afterward, never assumed from "no exception" -- both when the smoke child returns an
    ordinary nonzero exit and when STARTING it raises a terminating exception (the previous
    ordering fix only covered a normal child return). The PresentMon stand-in is a REAL
    background process, not a fake object, so Stop-PresentMonCapture's Kill()/WaitForExit() do
    real work; only Start-PresentMonCapture is swapped out, so the launcher and the boundary
    condition (JobId, artifacts) never need a real cache, build manifest or GPU.
    """

    def _presentmon_functions(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("function Start-PresentMonCapture(")
        end = text.index("\nfunction Get-FrameRows(", start)
        return text[start:end]

    def _failure_block(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        # PRESENTMON-HARNESS-ROBUSTNESS-1: starts at the spawn-guard's own flush-left assignment,
        # not at the (now indented, inside its own try) Start-PresentMonCapture call itself --
        # extracting from mid-try would leave this span's leading "} catch {" with no opening
        # "try {" of its own, an unbalanced-brace syntax error in the synthetic script below.
        # This boundary is a self-contained superset instead: the whole spawn-guard try/catch/if
        # is included (a no-op here, since the stub below always succeeds), then everything the
        # predecessor boundary already covered.
        start_marker = "$presentMonSpawnError = $null"
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 1): the span now ends right after the
        # SMOKE_RUN_FAILED if-block's own closing brace -- everything hoisted above the
        # PresentMon wait (reading result.json, resolving the run log, publishing smoke evidence,
        # and the Wait call itself, now inside a try block) is deliberately EXCLUDED here, since
        # extracting a truncated try/catch would leave the synthetic script below with an
        # unbalanced brace. $rawResult is the first statement of that hoisted block, so it is a
        # stable boundary that keeps the extracted span self-contained.
        end_marker = "$rawResult = [IO.File]::ReadAllText($resultPath)"
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        return text[start:end]

    def _run(self, *, cmd: str, program_files_has_pwsh: bool) -> tuple[subprocess.CompletedProcess, dict]:
        pub = self.tmp / "pub"
        pub.mkdir()
        leg_out = self.tmp / "legOut"
        leg_out.mkdir()
        result_path = leg_out / "result.json"  # never created: every scenario here is a failure
        real_pwsh_dir = str(Path(PWSH).resolve().parent)
        stub_root = self.tmp / "pf-stub"
        cmd_literal = "'" + cmd.replace("'", "''") + "'"
        junction_line = (
            f"New-Item -ItemType Junction -Path (Join-Path $stubRoot 'PowerShell\\7') "
            f"-Target '{real_pwsh_dir}' | Out-Null\n"
            if program_files_has_pwsh else ""
        )
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$stubRoot = '{stub_root}'\n"
            "New-Item -ItemType Directory -Path (Join-Path $stubRoot 'PowerShell') -Force | Out-Null\n"
            + junction_line
            + "$env:ProgramFiles = $stubRoot\n"
            f"$cmd = {cmd_literal}\n"
            f"$legOut = '{leg_out}'\n"
            f"$resultPath = '{result_path}'\n"
            f"$presentMonPath = '{self.tmp / 'presentmon.csv'}'\n"
            "$FixtureRehearsal = $true\n"
            "$SourceCommit = ('1' * 40)\n"
            "$ClipId = 'tiny_dual_iso'\n"
            f"$Pub = '{pub}'\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            + self._presentmon_functions() + "\n"
            # Swaps out only the launcher: a real PresentMon binary and ETW rights are not
            # available in this test environment, but Stop-PresentMonCapture (under test) must
            # act on a REAL child process, not a fake object, to prove it truly terminates one.
            "function Start-PresentMonCapture([string]$CsvPath) {\n"
            "    Start-Process -FilePath 'powershell.exe' "
            "-ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') "
            "-PassThru -WindowStyle Hidden\n"
            "}\n"
            + self._failure_block() + "\n"
            "Write-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc, summary

    def test_an_ordinary_nonzero_smoke_exit_stops_a_real_presentmon_process(self) -> None:
        proc, summary = self._run(cmd="exit 7", program_files_has_pwsh=True)
        self.assertEqual(proc.returncode, 18, proc.stdout + proc.stderr)
        self.assertIn("RESULT=SMOKE_RUN_FAILED", proc.stdout)
        self.assertEqual(summary.get("smokeExitCode"), 7)
        self.assertIsNone(summary.get("smokeLaunchExceptionType"))
        self.assertIs(summary.get("presentMonConfirmedExited"), True, summary)
        self.assertIsNone(summary.get("presentMonKillError"))
        self.assertIsNone(summary.get("presentMonWaitError"))

    def test_a_launch_exception_also_stops_a_real_presentmon_process_and_is_captured(self) -> None:
        # ProgramFiles points at a directory with no PowerShell\7\pwsh.exe at all, so the
        # production launch line itself throws CommandNotFoundException before $smokeRc is ever
        # set -- the exact bypass sol's major 3 describes.
        proc, summary = self._run(cmd="exit 0", program_files_has_pwsh=False)
        self.assertEqual(proc.returncode, 18, proc.stdout + proc.stderr)
        self.assertIn("RESULT=SMOKE_RUN_FAILED", proc.stdout)
        self.assertIsNone(summary.get("smokeExitCode"))
        self.assertIsNotNone(summary.get("smokeLaunchExceptionType"))
        self.assertIn("CommandNotFoundException", summary["smokeLaunchExceptionType"])
        self.assertIs(summary.get("presentMonConfirmedExited"), True, summary)


@requires_pwsh
class PresentMonSpawnFailureTests(_PwshCase):
    """PRESENTMON-HARNESS-ROBUSTNESS-1: Start-PresentMonCapture's own throws (a pre-existing
    output file, or PresentMon exiting nonzero within its 3s startup check) sat inside the outer
    try/finally with NO catch of its own -- so either would crash the whole job with a raw
    PowerShell error and publish nothing, one step before the smoke run (and any app-side
    measurement) had even started. This EXECUTES the real spawn-guard span extracted verbatim
    from the generator, with only Start-PresentMonCapture itself swapped for a stub that throws
    exactly as the real function does -- never a hand-reimplemented guard."""

    def _spawn_guard(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        # Ends right before $presentMonPostSpawnUtc is (re)assigned on the success path -- the
        # guard's own try/catch/if is fully closed within this span, so it stays balanced whether
        # or not the stubbed Start-PresentMonCapture throws.
        start_marker = "$presentMonPreSpawnUtc = (Get-Date).ToUniversalTime()"
        end_marker = "$presentMonPostSpawnUtc = (Get-Date).ToUniversalTime()"
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        return text[start:end]

    def _run(self, *, stub: str, present_mon_path: Path) -> tuple[subprocess.CompletedProcess, dict]:
        pub = self.tmp / "pub"
        pub.mkdir()
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            # PRESENTMON-HARNESS-ROBUSTNESS-2: the spawn guard's RESULT= line now calls the
            # module's own ConvertTo-AttrCudaResultLineSafeText -- imported here (never
            # hand-reimplemented) so this probe exercises the real sanitizer, not a stand-in.
            f"Import-Module '{MODULE}' -Force\n"
            + stub + "\n"
            f"$presentMonPath = '{present_mon_path}'\n"
            "$FixtureRehearsal = $true\n"
            "$SourceCommit = ('1' * 40)\n"
            "$ClipId = 'tiny_dual_iso'\n"
            f"$Pub = '{pub}'\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    $Object | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Path -Encoding UTF8\n"
            "}\n"
            + self._spawn_guard() + "\n"
            "Write-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc, summary

    def test_a_startup_check_throw_is_a_typed_terminal_not_an_uncaught_exception(self) -> None:
        stub = (
            "function Start-PresentMonCapture([string]$CsvPath) {\n"
            "    throw \"PRESENTMON_FAILED rc=6 (6 = ETW access denied: the agent account needs "
            "'Performance Log Users')\"\n"
            "}\n"
        )
        proc, summary = self._run(stub=stub, present_mon_path=self.tmp / "presentmon.csv")

        self.assertEqual(proc.returncode, 23, proc.stdout + proc.stderr)
        self.assertIn("RESULT=PRESENTMON_UNAVAILABLE", proc.stdout)
        self.assertNotIn("RESULT=NO_FAILURE_BRANCH_TAKEN", proc.stdout)
        self.assertEqual(summary.get("result"), "PRESENTMON_UNAVAILABLE")
        self.assertEqual(summary.get("presentMonStatus"), "unavailable")
        self.assertIn("PresentMon failed to start", summary.get("reason", ""))
        self.assertIn("ETW access denied", summary.get("reason", ""))

    def test_a_pre_existing_output_file_is_also_a_typed_terminal(self) -> None:
        existing_csv = self.tmp / "presentmon.csv"
        existing_csv.write_text("stale", encoding="utf-8")
        # The real function's own guard, not a hand-picked message -- proves this call site's
        # try/catch actually catches THIS throw, not just a stub written to match the assertion.
        stub = (
            "function Start-PresentMonCapture([string]$CsvPath) {\n"
            "    if (Test-Path -LiteralPath $CsvPath) { throw \"PresentMon output already exists: $CsvPath\" }\n"
            "}\n"
        )
        proc, summary = self._run(stub=stub, present_mon_path=existing_csv)

        self.assertEqual(proc.returncode, 23, proc.stdout + proc.stderr)
        self.assertEqual(summary.get("presentMonStatus"), "unavailable")
        self.assertIn("already exists", summary.get("reason", ""))

    def test_a_clean_spawn_falls_through_to_the_rest_of_the_job(self) -> None:
        # Regression guard for the guard itself: a Start-PresentMonCapture that succeeds must
        # never take the typed-refusal branch, so $presentMonPostSpawnUtc (excluded from the
        # extracted span, deliberately -- see _spawn_guard) is reached and this probe's own
        # trailing marker prints.
        stub = (
            "function Start-PresentMonCapture([string]$CsvPath) {\n"
            "    [pscustomobject]@{ HasExited = $false; ExitCode = 0 }\n"
            "}\n"
        )
        proc, summary = self._run(stub=stub, present_mon_path=self.tmp / "presentmon.csv")

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT=NO_FAILURE_BRANCH_TAKEN", proc.stdout)
        self.assertEqual(summary, {})

    def test_a_quote_in_the_exception_message_does_not_garble_the_result_line(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note): a '"' inside the exception message used to
        # land raw inside this stdout line's quoted REASON="..." field, garbling naive downstream
        # parsing. summary.json's own `reason` (real JSON, already properly escaped) keeps the
        # quote verbatim -- only the plain-text RESULT= line is sanitized.
        stub = (
            "function Start-PresentMonCapture([string]$CsvPath) {\n"
            '    throw \'bad arg: "--foo" was rejected\'\n'
            "}\n"
        )
        proc, summary = self._run(stub=stub, present_mon_path=self.tmp / "presentmon.csv")

        self.assertEqual(proc.returncode, 23, proc.stdout + proc.stderr)
        self.assertIn('bad arg: \'--foo\' was rejected', proc.stdout)
        self.assertNotIn('bad arg: "--foo" was rejected', proc.stdout)
        self.assertIn('bad arg: "--foo" was rejected', summary.get("reason", ""))


@requires_pwsh
class WaitPresentMonCaptureTimeoutWaitsAfterKillTests(_PwshCase):
    """ATTR3-SMOKE-RUNNER-DEPS-1 round 4, item 5 (fable round-3 minor). Kill() is asynchronous --
    Stop-PresentMonCapture already waited bounded after it before sampling HasExited;
    Wait-PresentMonCapture's timeout path used to sample HasExited in the very next statement,
    which could still read false for a process that exits milliseconds later. It now mirrors
    Stop-PresentMonCapture and reports killError, waitError and confirmedExited alike, against a
    REAL background process."""

    def _presentmon_functions(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("function Start-PresentMonCapture(")
        end = text.index("\nfunction Get-FrameRows(", start)
        return text[start:end]

    def test_the_timeout_path_waits_after_kill_and_reports_all_three_fields(self) -> None:
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            + self._presentmon_functions() + "\n"
            "$proc = Start-Process -FilePath 'powershell.exe' "
            "-ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') "
            "-PassThru -WindowStyle Hidden\n"
            "try { [void](Wait-PresentMonCapture $proc -TimeoutSeconds 1); Write-Output 'NO_THROW' } "
            "catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        message = normalize_pwsh_message_text(proc.stdout)
        self.assertIn("THREW PRESENTMON_TIMEOUT", message)
        self.assertIn("confirmedExited=True", message)
        self.assertIn("killError=<none>", message)
        self.assertIn("waitError=<none>", message)


@requires_git
class SmokeRunnerBlobHelperSpacedPathTests(unittest.TestCase):
    """Required test (ATTR3-SMOKE-RUNNER-PIN-1 round 2 BLOCKER): Save-AttrCudaCommittedBlobBytes
    against a real repository whose path CONTAINS A SPACE -- the real repository root
    (`C:\\!Layi Wkspc\\MLV-App`) does, and the pre-fix -ArgumentList joined `-C $RepoRoot` into an
    unquoted command line that git could not parse, so no generator could ever stage anything
    from the real checkout.
    """

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 host jobs are Windows-only (drive-letter path parameters)")
        if not PWSH:
            self.skipTest("pwsh is not on PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3 space ")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_blob_bytes_written_from_a_spaced_repo_path_match_git_cat_file(self) -> None:
        repo = self.tmp / "repo with space"
        shas = _make_fixture_repo(repo)
        self.assertIn(" ", str(repo), "the fixture repo path must itself contain a space")

        rel_path = "src/mlv/llrawproc/llrawproc.c"
        blob_id = _git_run(["rev-parse", f"{shas[1]}:{rel_path}"], repo)
        destination = self.tmp / "extracted-blob.bin"

        script = self.tmp / "extract.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Save-AttrCudaCommittedBlobBytes -RepoRoot '{repo}' -BlobId '{blob_id}' "
            f"-Destination '{destination}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(
            "ATTRCUDA_BLOB_READ_FAILED", normalize_pwsh_message_text(proc.stdout + proc.stderr)
        )
        self.assertTrue(destination.is_file())
        expected_sha256 = _git_blob_sha256(repo, shas[1], rel_path)
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), expected_sha256)


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
# (g) ATTR3-FIXTURE-STAGE-1 (sol, PR #139 r1 MAJOR): the final cache publish must be a truly
#     non-overwriting rename. These call the module functions directly -- both the OLD helper
#     the fixture job used to call and the NEW one it calls now -- against the exact race sol
#     described: a different-bytes file lands at the destination AFTER the job's own absence
#     check has already passed and BEFORE the rename runs.
# --------------------------------------------------------------------------------------------


@requires_pwsh
class NonOverwritingPublishRaceTests(_PwshCase):
    """RED: the old helper overwrites a raced destination. GREEN: the new one never does."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.tmp / "fixture.partial"
        self.source.write_bytes(b"this run's own bytes")
        self.destination = self.tmp / "fixture.bin"

    def test_RED_the_old_helper_deletes_and_overwrites_a_destination_that_raced_in(self) -> None:
        # Publish-AttrCudaFileMove is still exactly what it was: still used, unchanged, by
        # playback-attr-3-cuda-stage-job.ps1 (the package stager) -- ATTR3-SCANNER's own r7
        # note that a hostile SHAPE can't be caught by static lint applies just as much to a
        # RACE, which no lint of any kind can see. This is the vulnerability sol reported,
        # reproduced directly against the helper the fixture job used to call.
        self.destination.write_bytes(b"a concurrent publisher's DIFFERENT bytes")
        proc = self.run_with_module(
            f"Write-Output ('MOVED=' + (Publish-AttrCudaFileMove -Source '{self.source}' "
            f"-Destination '{self.destination}'))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MOVED=", proc.stdout)
        self.assertEqual(
            self.destination.read_bytes(),
            b"this run's own bytes",
            "RED: the old helper silently deleted the raced-in file and overwrote it",
        )

    def test_GREEN_the_new_helper_refuses_a_destination_that_raced_in_with_different_bytes(self) -> None:
        self.destination.write_bytes(b"a concurrent publisher's DIFFERENT bytes")
        proc = self.run_with_module(
            _guard(f"Publish-AttrCudaFileMoveNonOverwriting -Source '{self.source}' "
                   f"-Destination '{self.destination}'")
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_throws(proc, "ATTRCUDA_NONOVERWRITE_DESTINATION_EXISTS")
        self.assertEqual(
            self.destination.read_bytes(),
            b"a concurrent publisher's DIFFERENT bytes",
            "the raced-in file must survive completely untouched",
        )
        self.assertTrue(self.source.exists(), "a refused move must leave the source in place")

    def test_GREEN_the_new_helper_refuses_a_destination_that_raced_in_with_identical_bytes(self) -> None:
        # Even identical bytes are never silently accepted as "the same move" by the helper
        # itself -- it throws either way. Distinguishing "someone already finished this exact
        # fixture" from "something else is there" is the CALLER's job (attr3-stage-fixture-
        # job.ps1 re-hashes on catch), never this helper's.
        self.destination.write_bytes(b"this run's own bytes")
        proc = self.run_with_module(
            _guard(f"Publish-AttrCudaFileMoveNonOverwriting -Source '{self.source}' "
                   f"-Destination '{self.destination}'")
        )
        self.assert_throws(proc, "ATTRCUDA_NONOVERWRITE_DESTINATION_EXISTS")
        self.assertEqual(self.destination.read_bytes(), b"this run's own bytes")

    def test_a_missing_destination_is_still_moved_cleanly(self) -> None:
        proc = self.run_with_module(
            f"Publish-AttrCudaFileMoveNonOverwriting -Source '{self.source}' "
            f"-Destination '{self.destination}' | Out-Null\n"
            "Write-Output 'ok'\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok", proc.stdout)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.destination.read_bytes(), b"this run's own bytes")

    def test_a_linked_parent_is_refused_without_ever_calling_move(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        cache = self.tmp / "cache"
        proc = self.run_with_module(
            f"New-Item -ItemType Junction -Path '{cache}' -Target '{outside}' | Out-Null\n"
        )
        if proc.returncode != 0 or not cache.exists():
            self.skipTest(f"cannot create a junction here: {proc.stderr}")
        target = cache / "fixture.bin"
        proc = self.run_with_module(
            _guard(f"Publish-AttrCudaFileMoveNonOverwriting -Source '{self.source}' -Destination '{target}'")
        )
        self.assert_throws(proc, "ATTRCUDA_SLOT_PARENT_IS_LINK")
        self.assertEqual(list(outside.iterdir()), [], "nothing may be written through the link")


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
    ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-smoke-runner-job.ps1",
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
        ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-smoke-runner-job.ps1": (
            "Assert-AttrCudaSafeArtifactName",
            "Assert-AttrCudaDirectChild",
            "Assert-AttrCudaNoLinkBelowRoot",
            "Assert-AttrCudaWritableFileSlot",
            "Assert-AttrCudaNonOverwritingFileSlot",
            "Test-AttrCudaPathIsReparsePoint",
            "Read-AttrCudaBase64Payload",
            "Publish-AttrCudaBytes",
            "Publish-AttrCudaText",
            "Publish-AttrCudaDirectoryMoveNonOverwriting",
            "New-AttrCudaDirectory",
            "Remove-AttrCudaTree",
        ),
        ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1": (
            "Assert-AttrCudaBuildManifest",
            "Resolve-AttrCudaSmokeRunLog",
            "Get-AttrCudaLastEligibilityLine",
            "Get-AttrCudaEligibilityVerdict",
            "Assert-AttrCudaWritableFileSlot",
            "Test-AttrCudaPathIsReparsePoint",
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
