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

from tools.repo_hygiene.synthetic_mlv import (
    FRAMES_30S_AT_23976,
    MLV_EXTENSION,
    write_synthetic_mlv,
)


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

# UM-DISPLAY-SELECT-AND-LOG-1 round 3: the shared display-identity parser is a pinned
# smoke-runner closure sibling AND the job embeds its functions from the committed blob,
# so a fixture repo must carry its REAL text, not a stand-in.
_CLIP_LENGTH_TEXT = (Path(__file__).resolve().parents[2] / "tools" / "profiling" / "gui-smoke-length-gate.ps1").read_text(encoding="utf-8")
_DISPLAY_IDENTITY_TEXT = (Path(__file__).resolve().parents[2] / "tools" / "profiling" / "gui-smoke-display-identity.ps1").read_text(encoding="utf-8")

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


def _run_pwsh_file(script: Path, timeout: float | None = None) -> subprocess.CompletedProcess:
    # timeout=None (the default) keeps every existing caller unbounded exactly as before; a caller
    # that passes one gets subprocess.TimeoutExpired (a test error) instead of a hung shard.
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# Bound for the keep-alive hung-probe tests' pwsh children: they normally finish in ~6-8s (up to
# ~40s under 4x CPU oversubscription); 180s is far past that yet still fails a wedged pwsh instead
# of hanging the CI shard.
_KEEPALIVE_TEST_TIMEOUT_SECONDS = 180


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
    # fable, PR #140 r2d MINOR: this repo's own text fixture (llrawproc.c below) is written via
    # Path.write_text, which translates '\n' to os.linesep -- CRLF on Windows -- so its committed
    # blob is only DIFFERENT from a naive raw-byte SHA1 whenever git actually normalises CRLF on
    # the way in. Left to the HOST's global/system config, that normalisation (and therefore
    # whether FixtureCommittedBytesTests.test_an_unmodified_tracked_file_is_accepted actually
    # discriminates the round-2d self-caught defect -- a local SHA1 reimplementation that ignored
    # git's clean filters entirely -- from a correct implementation) is UNPINNED: on a host whose
    # global core.autocrlf is false, this fixture's bytes never get normalised, so a naive
    # raw-byte hash and the real committed blob hash agree anyway, and the exact reimplementation
    # bug this repo caught here once would pass silently. Pinned true so every host applies the
    # same normalisation this suite depends on to catch that class of bug, regardless of the
    # user's own global git config.
    _git_run(["config", "core.autocrlf", "true"], path)
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
        ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
        ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n",
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
    (path / "tools" / "profiling" / "gui-smoke-display-identity.ps1").write_text(_DISPLAY_IDENTITY_TEXT, encoding="utf-8")
    (path / "tools" / "profiling" / "gui-smoke-length-gate.ps1").write_text(_CLIP_LENGTH_TEXT, encoding="utf-8")
    # PLAYBACK-CLIP-LENGTH-ENFORCE-1: the generator refuses a fixture id whose tracked header is
    # under 20 s at GENERATION time, so a throwaway repo that exercises the fixture arm carries a
    # synthetic ~30 s header-only stand-in for each tracked fixture (never real footage).
    for _fixture_stem in ("tiny_dual_iso", "large_dual_iso"):
        write_synthetic_mlv(path / "tests" / "fixtures" / "clips" / (_fixture_stem + MLV_EXTENSION),
                            FRAMES_30S_AT_23976)
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


@requires_pwsh
class StageJobIdSuffixTests(_PwshCase):
    """STAGE-JOBID-SINGLE-USE-RESTAGE-1: a JobId is single-use, so a re-stage needs a new one.

    UmRunDrop refuses any reused JobId (UMRUN_JOBID_IN_USE), and the default stage id carries only
    the short sha. -JobIdSuffix is how a caller re-stages one sha under a fresh id; absent, the id
    must be exactly what it always was. STAGE_JOBID_TEST_GENERATOR lets the RED run point these
    tests at a copy of the pre-change script.
    """

    GENERATOR = Path(os.environ.get("STAGE_JOBID_TEST_GENERATOR") or STAGE_GENERATOR)

    def setUp(self) -> None:
        super().setUp()
        self.build = self.tmp / "build"
        self.staging = self.tmp / "staging"
        self.staging.mkdir(parents=True)
        self.agent = self.tmp / "agent"
        (self.agent / "inbox").mkdir(parents=True)
        (self.agent / "cache").mkdir(parents=True)
        self.names = _fake_build_dir(self.build, SHA_FIXTURE)
        self.default_id = f"playback-attr-3-cuda-stage-{SHA_FIXTURE[:12]}"

    def generate(self, suffix: str | None = None) -> subprocess.CompletedProcess:
        extra = "" if suffix is None else " -JobIdSuffix '" + suffix.replace("'", "''") + "'"
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{self.GENERATOR}' -SourceCommit '{SHA_FIXTURE}' -BuildDir '{self.build}' "
            f"-OutDir '{self.staging}' -AgentRoot '{self.agent}'{extra} | ConvertTo-Json -Depth 5\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def generate_ok(self, suffix: str | None = None) -> dict:
        proc = self.generate(suffix)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    def drop_side_files(self) -> None:
        for name in self.names.values():
            shutil.copy2(self.build / name, self.agent / "inbox" / name)

    def test_no_suffix_keeps_the_default_job_id_everywhere(self) -> None:
        generated = self.generate_ok()

        self.assertEqual(generated["jobId"], self.default_id)
        self.assertEqual(Path(generated["jobFile"]).name, f"{self.default_id}.job.ps1")
        text = Path(generated["jobFile"]).read_text(encoding="utf-8")
        self.assertIn(f"$JobId = '{self.default_id}'", text)

    def test_a_suffix_reaches_the_job_file_name_the_baked_id_and_the_result(self) -> None:
        new_id = f"{self.default_id}-rst2"
        generated = self.generate_ok("rst2")

        self.assertEqual(generated["jobId"], new_id)
        job = Path(generated["jobFile"])
        self.assertEqual(job.name, f"{new_id}.job.ps1")
        self.assertIn(f"$JobId = '{new_id}'", job.read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in self.staging.iterdir()], [job.name])

        self.drop_side_files()
        proc = _run_job(job)

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn(f"[{new_id}] START", proc.stdout)
        result = json.loads((self.agent / "outbox" / f"{new_id}.artifacts" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["jobId"], new_id)
        self.assertEqual(result["exitCode"], 0)
        self.assertFalse(
            (self.agent / "outbox" / f"{self.default_id}.artifacts").exists(),
            "the default-id outbox was used by a suffixed run",
        )

    def test_a_suffixed_and_a_default_stage_of_one_sha_have_different_ids(self) -> None:
        default = self.generate_ok()
        suffixed = self.generate_ok("again")

        self.assertNotEqual(default["jobId"], suffixed["jobId"])
        self.assertEqual(len(list(self.staging.iterdir())), 2)

    def test_the_longest_admitted_suffix_is_accepted(self) -> None:
        suffix = "a-Z9" * 8
        self.assertEqual(len(suffix), 32)

        self.assertEqual(self.generate_ok(suffix)["jobId"], f"{self.default_id}-{suffix}")

    def test_a_bad_suffix_is_refused_with_a_typed_error_and_nothing_is_emitted(self) -> None:
        for bad in ("a b", "x/y", r"x\y", "..", "a.b", "a_b", "a'b", "x" * 33, "é"):
            with self.subTest(suffix=bad):
                proc = self.generate(bad)

                self.assertNotEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
                self.assertIn("STAGE_JOBID_SUFFIX_INVALID", normalize_pwsh_message_text(proc.stderr + proc.stdout))
                self.assertEqual(list(self.staging.iterdir()), [], "a job was emitted for a bad suffix")


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
class AttributionJobTelemetryArmTests(_PwshCase):
    """CUDA-PLAYBACK-PRESENT-CADENCE-1 round 2: -TelemetryArm HEAVY|LIGHT must actually reach the
    emitted job, not just exist as a branch in the generator's own template text. This class
    exists because a first version of -TelemetryArm referenced $TelemetryArm inside the emitted
    job's $envs-building if/else without ever substituting a __TELEMETRY_ARM__ token for it --
    the emitted job's $TelemetryArm was therefore always $null, silently taking the else (HEAVY)
    branch regardless of what was requested, discovered only by running a live LIGHT leg on
    Bachelor and finding it produced HEAVY-sized logs. These tests run the REAL generator (not
    read its source as text) so that class of defect cannot recur unnoticed."""

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
            "FixtureSha256": "b" * 64,
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
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out_file.read_text(encoding="utf-8")

    def test_default_arm_is_heavy_and_substituted_not_left_undefined(self) -> None:
        job_text = self._generate()
        self.assertIn("$TelemetryArm = 'HEAVY'", job_text)
        # The token must actually be gone -- a leftover __TELEMETRY_ARM__ means the substitution
        # was never wired up and $TelemetryArm stayed the literal string '__TELEMETRY_ARM__'
        # (still non-null, but never equal to 'LIGHT' either -- a quieter version of the same bug).
        self.assertNotIn("__TELEMETRY_ARM__", job_text)

    def test_light_arm_is_substituted_and_drops_the_heavy_only_env_vars(self) -> None:
        job_text = self._generate(TelemetryArm="LIGHT")
        self.assertIn("$TelemetryArm = 'LIGHT'", job_text)
        self.assertIn("MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1", job_text)

    def test_heavy_arm_keeps_the_per_frame_diagnostic_env_vars(self) -> None:
        job_text = self._generate(TelemetryArm="HEAVY")
        self.assertIn("$TelemetryArm = 'HEAVY'", job_text)
        self.assertIn("MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY=1", job_text)
        self.assertIn("MLVAPP_STAGE_TIMING=1", job_text)
        self.assertIn("MLVAPP_PERF_FIELD_LOG=1", job_text)


@requires_pwsh
@requires_git
class AttributionJobDisablePaintPerSubmitTests(_PwshCase):
    """CUDA-PLAYBACK-PRESENT-CADENCE-2: -DisablePaintPerSubmit must actually reach the emitted
    job as a real boolean, not a leftover __DISABLE_PAINT_PER_SUBMIT__ token -- the same class of
    wiring bug AttributionJobTelemetryArmTests exists to catch for -TelemetryArm."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def _generate(self, extra_args=()):
        out_file = self.staging / "job.ps1"
        parts = [
            f"-SourceCommit '{self.shas[1]}'",
            f"-BuildManifestSha256 '{'a' * 64}'",
            "-ClipId 'tiny_dual_iso'",
            f"-FixtureSha256 '{'b' * 64}'",
            f"-OutFile '{out_file}'",
            f"-RepoRoot '{self.repo}'",
        ] + list(extra_args)
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' " + " ".join(parts) + "\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out_file.read_text(encoding="utf-8")

    def test_default_is_false_and_substituted_not_left_undefined(self) -> None:
        job_text = self._generate()
        self.assertIn("$DisablePaintPerSubmit = $false", job_text)
        self.assertNotIn("__DISABLE_PAINT_PER_SUBMIT__", job_text)

    def test_switch_sets_true(self) -> None:
        job_text = self._generate(extra_args=["-DisablePaintPerSubmit"])
        self.assertIn("$DisablePaintPerSubmit = $true", job_text)

    def test_opt_out_env_var_is_wired_into_the_envs_list(self) -> None:
        job_text = self._generate()
        self.assertIn("MLVAPP_GPU_WINDOW_PAINT_PER_SUBMIT=0", job_text)

    def test_an_invalid_arm_is_refused_at_parameter_binding(self) -> None:
        proc_script = self.tmp / "generate-invalid.ps1"
        proc_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -TelemetryArm 'MEDIUM' "
            f"-OutFile '{self.staging / 'job.ps1'}' -RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(proc_script)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.staging / "job.ps1").exists())

    # A "dot-source just the $envs-building block and evaluate it" test was tried here and
    # dropped: that block sits inside the emitted job's single enclosing try/finally, so slicing
    # it out by text markers breaks brace balance in a ParserError before the intended assertion
    # ever runs. The substring checks above already exercise the real generator end to end (not
    # a copy of its logic) and pin both the substitution and the per-arm env-var membership this
    # bug needed; that is the regression coverage for this defect class.


@requires_pwsh
@requires_git
class DisplayIdentityEmbedsFromCommittedBlobTests(_PwshCase):
    """UM-DISPLAY-SELECT-AND-LOG-1 round 4 (fable DISPLAY-IDENTITY-EMBED-FROM-BLOB-TEST-1).

    The emitted job embeds the shared display-identity parser from its COMMITTED bytes at
    -SourceCommit -- the bytes the smoke-runner closure stages -- never the generator's working
    tree. The generator runs against a throwaway repo whose committed parser is the real file and
    whose WORKING-TREE copy is then edited without committing; the emitted job must carry the
    committed text and none of the edit."""

    MARKER = "WORKING_TREE_ONLY_MARKER_4F1C"

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.shas = _make_fixture_repo(self.repo)
        self.staging = self.tmp / "staging"
        self.staging.mkdir()

    def _generate(self) -> str:
        out_file = self.staging / "job.ps1"
        script = self.tmp / "generate.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& '{ATTRIBUTION_GENERATOR}' -SourceCommit '{self.shas[1]}' "
            f"-BuildManifestSha256 '{'a' * 64}' -ClipId 'tiny_dual_iso' "
            f"-FixtureSha256 '{'b' * 64}' -OutFile '{out_file}' -RepoRoot '{self.repo}'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out_file.read_text(encoding="utf-8")

    def test_the_job_embeds_the_committed_parser_not_the_working_tree(self) -> None:
        # MUTATION CAUGHT: extracting from `Join-Path $RepoRoot 'tools/profiling/gui-smoke-
        # display-identity.ps1'` (the working tree) instead of the blob at -SourceCommit.
        baseline = self._generate()
        for name in ("ConvertFrom-GuiSmokeLogFields", "ConvertFrom-GuiSmokeDisplayLog",
                     "Get-GuiSmokeDisplayIdentity", "Find-GuiSmokeDisplayScreen"):
            with self.subTest(embedded=name):
                self.assertIn(f"function {name} {{", baseline)
        self.assertNotIn(self.MARKER, baseline)

        shared = self.repo / "tools" / "profiling" / "gui-smoke-display-identity.ps1"
        text = shared.read_bytes().decode("utf-8")
        anchor = "function Get-GuiSmokeDisplayIdentity {"
        self.assertIn(anchor, text.replace("\r\n", "\n"))
        edited = text.replace("\r\n", "\n").replace(anchor, anchor + f"\n    # {self.MARKER}", 1)
        shared.write_bytes(edited.encode("utf-8"))  # working tree only -- deliberately NOT committed
        self.addCleanup(lambda: shared.write_bytes(text.encode("utf-8")))

        job_text = self._generate()
        self.assertNotIn(self.MARKER, job_text)
        self.assertIn(f"function Get-GuiSmokeDisplayIdentity {{", job_text)


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
        # residence-check block. CUDA-PERF-DISPLAY-WAKE-2 round 1c: the anchor is this section's
        # own unique comment, not "[void](New-AttrCudaDirectory -Path $Pub)" -- that exact text
        # now ALSO appears earlier, in the claim-time SCREENSAVER_SECURE_OWNER_ONLY early-exit
        # block, which would make a plain text.index() land there instead.
        pub_created = text.index(
            "ATTR3-FIXTURE-STAGE-1: a fixture run authenticates the cached clip's CONTENT")
        start_marker = "if ($FixtureRehearsal) {"
        # ATTR3-FOOTAGE-BIND-1 PR-B round 4: everything from the owner/fixture if/else through the
        # end of the job runs inside one `try` (see the generator's own comment above it), whose
        # opening `try {` now sits well before this slice starts -- CUDA-PERF-DISPLAY-WAKE-3 round
        # 1b moved it up to Start-AttrCudaDisplayWake, ahead of $pub_created above -- so the slice
        # stays self-contained (no dangling brace) whether it stops here or later; this Expand-
        # Archive marker is kept as the cutoff since it is already a stable, unique anchor.
        end_marker = "\nExpand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)"
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
            "function Write-JobTrace([string]$Message) { }\n"
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

    def _generate_outside_root_job(self, name: str) -> Path:
        # A LITERAL path is used for the outside root rather than self.tmp/self.agent: self.tmp
        # can itself land under the real C:\mlvtmp (e.g. a fleet lane whose own scratch root is
        # C:\mlvtmp\lane-scratch\...), which would make self.agent the wrong fixture for an
        # "outside mlvtmp" assertion and is exactly how this bug went unnoticed before.
        outside_root = "C:\\attr3-guard-check-outside-mlvtmp"
        out_file = self.tmp / f"{name}-job.ps1"
        script = self.tmp / f"generate-{name}.ps1"
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
        return out_file

    def test_the_default_agent_root_guard_still_refuses_a_root_outside_mlvtmp(self) -> None:
        # Proves the check above does not paper over a real regression: with NO override,
        # the emitted job's Assert-UnderMlvTmp guard still refuses an -AgentRoot that
        # resolves outside C:\mlvtmp, before it ever looks at the cache.
        out_file = self._generate_outside_root_job("outside-root")

        proc = _run_job(out_file)

        self.assertEqual(proc.returncode, 1, f"{proc.stdout}\n{proc.stderr}")
        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertIn("job-owned path 'Root' resolves outside", combined)
        self.assertIn("C:\\mlvtmp", combined)

    def test_the_root_guard_refuses_an_outside_root_before_the_session_lock_probe(self) -> None:
        # CI-FLAKE-ATTR3-ROOT-GUARD-BEFORE-LOCK-READ-1: when the job reads the session lock first,
        # the test above passes or fails on the RUNNER'S session state -- a hosted runner that
        # answers locked/unknown makes an outside root exit 30 (SESSION_LOCKED_OWNER_ONLY), not 1.
        # Here the lock probe is forced to report LOCKED (the generated job's one
        # Start-AttrCudaDisplayWake call is replaced by a stub carrying sessionLocked=$true), so the
        # verdict can no longer depend on the machine: every job-owned path guard must refuse
        # (exit 1) before that read.
        out_file = self._generate_outside_root_job("outside-root-locked")
        job_text = out_file.read_text(encoding="utf-8")
        probe_call = "$displayWake = Start-AttrCudaDisplayWake"
        self.assertEqual(
            job_text.count(probe_call), 1, "the lock probe call moved in the generator")
        locked_stub = (
            "$displayWake = [ordered]@{ sessionLocked = $true; "
            "screensaverSecureOwnerOnly = $false; dismissFailed = $false }"
        )
        out_file.write_text(job_text.replace(probe_call, locked_stub), encoding="utf-8")

        proc = _run_job(out_file)

        combined = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        self.assertNotIn("RESULT=SESSION_LOCKED_OWNER_ONLY", combined)
        self.assertEqual(proc.returncode, 1, f"{proc.stdout}\n{proc.stderr}")
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
        # CUDA-PERF-DISPLAY-WAKE-2 round 1c: anchored on this section's own unique comment, not
        # "[void](New-AttrCudaDirectory -Path $Pub)" -- see
        # AttributionJobFixtureContentAuthenticationTests._extract_fixture_content_check's own
        # comment for why that exact text is no longer unique enough.
        pub_created = text.index(
            "ATTR3-FIXTURE-STAGE-1: a fixture run authenticates the cached clip's CONTENT")
        start = text.index("if ($FixtureRehearsal) {", pub_created)
        # ATTR3-FOOTAGE-BIND-1 PR-B round 4: this range needs to include the round-4 private-
        # directory linking logic, which lives inside the SAME if/else block as the content
        # check. The `try` that wraps everything from here to the end of the job (see the
        # generator's own comment above it) now opens well before this slice starts -- CUDA-PERF-
        # DISPLAY-WAKE-3 round 1b moved it up to Start-AttrCudaDisplayWake -- so this Expand-
        # Archive marker is just a stable, unique cutoff, not a dangling-brace concern.
        end = text.index("\nExpand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)", start)
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
            "$OwnerJournal = Join-Path $Work '.attrcuda-owned.jsonl'\n"
            "$ownerViewHandles = [System.Collections.Generic.List[object]]::new()\n"
            "$ownerViews = [System.Collections.Generic.List[object]]::new()\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            "function Write-JobTrace([string]$Message) { }\n"
            + block + "\n"
            "Write-Output ('RESULT=NO_MISMATCH clipPath=' + $clipPath)\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_both_parts_pass_and_a_private_directory_is_built_from_neutral_views(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 4, restated for OWNER-FOOTAGE-NO-HARDLINK-1: the path this
        # job opens is no longer the owner's own part path -- it is a neutrally-named VIEW under a
        # private directory built inside $Work (a symbolic link to the original where the host can
        # create one, else a byte copy), byte-identical to the verified source part. It is NEVER a hard
        # link: each original keeps exactly one name, and a copy is a different file object.
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
        view0_path = work / private_dir_name / neutral0
        view1_path = work / private_dir_name / neutral1
        self.assertIn(f"clipPath={view0_path}", proc.stdout)
        # Never the owner's real part path, anywhere in the job's own stdout.
        self.assertNotIn(part0, proc.stdout)
        self.assertNotIn(part1, proc.stdout)
        self.assertTrue(view0_path.is_file())
        self.assertTrue(view1_path.is_file())
        self.assertEqual(view0_path.read_bytes(), part0_bytes)
        self.assertEqual(view1_path.read_bytes(), part1_bytes)
        # NEVER a second name of the owner's bytes: each original still has exactly one name.
        self.assertEqual(os.stat(part0_path).st_nlink, 1)
        self.assertEqual(os.stat(part1_path).st_nlink, 1)
        for view_path, source_path in ((view0_path, part0_path), (view1_path, part1_path)):
            if view_path.is_symlink():
                self.assertEqual(Path(os.path.realpath(view_path)), Path(os.path.realpath(source_path)))
            else:
                self.assertNotEqual(view_path.stat().st_ino, source_path.stat().st_ino,
                                    "a copy view must be a different file object from the original")
                self.assertEqual(os.stat(view_path).st_nlink, 1)

    def test_a_mismatched_part_fails_closed_before_the_package_is_touched(self) -> None:
        # STAGE-STALL card: the content hash is no longer a separate up-front pass -- the one full
        # read happens on the held link, AFTER the naming/contiguity assertion -- so this part must
        # carry a valid neutral-name extension (composed, never spelled whole) for the sha mismatch
        # to be the first defect the job can find. A wrongly-named part is refused at exit 20.
        part0_path = self.tmp / ("owner-content-mismatch-part0" + "." + "MLV")
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
    """ATTR3-FOOTAGE-BIND-1 PR-B round 4, rebuilt by OWNER-FOOTAGE-NO-HARDLINK-1: the leak proof,
    the sibling-isolation proof, and the view / pin / refusal paths for the private per-job
    directory, run through the real attribution-job block against synthetic NTFS folders.

    Each part is viewed through a symbolic link where the host may create one and through a
    verified byte copy otherwise -- NEVER a hard link -- and the original is pinned read-only for
    the run. The copy path is forced where a test needs it by replacing Test-AttrCudaSymlinkCapability
    at script scope (the spliced block asks it there), which stands in an ultra-magnus-shaped venue
    on any host; the symlink path runs only where the host can really create one.
    """

    def _extract_content_check(self) -> str:
        # Duplicated from AttributionJobOwnerContentAuthenticationTests rather than shared, in
        # keeping with this file's own convention of keeping each test class self-contained.
        # CUDA-PERF-DISPLAY-WAKE-2 round 1c: anchored on this section's own unique comment, not
        # "[void](New-AttrCudaDirectory -Path $Pub)" -- see
        # AttributionJobFixtureContentAuthenticationTests._extract_fixture_content_check's own
        # comment for why that exact text is no longer unique enough.
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        pub_created = text.index(
            "ATTR3-FIXTURE-STAGE-1: a fixture run authenticates the cached clip's CONTENT")
        start = text.index("if ($FixtureRehearsal) {", pub_created)
        end = text.index("\nExpand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip)", start)
        self.assertGreater(end, start, "owner/fixture content-check markers moved in the generator")
        return text[start:end]

    def _extract_cmd_build(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("$envList = \"'\" + ($envs -join")
        # CUDA-PERF-DISPLAY-WAKE-3 round 1: ends right before the first keep-alive health
        # checkpoint (previously "$presentMonProc = Start-PresentMonCapture" itself) -- that
        # checkpoint needs $displayWake/$displayWakeKeepAlive/$Pub/$FixtureRehearsal, none of which
        # this minimal $cmd-construction probe defines, and is unrelated to what this test proves.
        end = text.index("\n$keepAliveHealthAtMeasurementStart = Get-AttrCudaDisplayWakeKeepAliveHealth", start)
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

    def _run_owner_content_check(self, *, parts, pub: Path, work: Path, prelude: str = "", epilogue: str = "") -> subprocess.CompletedProcess:
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
            "$OwnerJournal = Join-Path $Work '.attrcuda-owned.jsonl'\n"
            "$ownerViewHandles = [System.Collections.Generic.List[object]]::new()\n"
            "$ownerViews = [System.Collections.Generic.List[object]]::new()\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            "function Write-JobTrace([string]$Message) { }\n"
            + prelude
            + block + "\n"
            "Write-Output ('CLIPPATH=' + $clipPath)\n"
            + epilogue,
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

    def _part(self, directory: Path, name: str, payload: bytes) -> dict:
        path = directory / name
        path.write_bytes(payload)
        return {
            "index": None,
            "path": str(path).replace("\\", "/"),
            "length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "file": path,
            "bytes": payload,
        }

    @staticmethod
    def _force_copy_mode() -> str:
        # The capability probe is asked at script scope by the spliced block, so replacing it here
        # stands in a venue that cannot create a symbolic link (ultra-magnus: no Developer Mode, not
        # elevated) on a host that can.
        return (
            "Set-Item -Path function:Test-AttrCudaSymlinkCapability -Value {\n"
            "    param([string]$Directory, [string]$Journal)\n"
            "    [pscustomobject]@{ Capable = $false; Result = 'SYMLINK_UNAVAILABLE' }\n"
            "}\n"
        )

    def test_copy_mode_builds_a_lone_verified_copy_and_the_original_keeps_its_one_name(self) -> None:
        # The copy fallback: a separate file with its own file index and exactly one name; the
        # owner's original is untouched and gains no second name.
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-copy-source"
        src_dir.mkdir()
        part = self._part(src_dir, "source" + base_extension, b"copy mode original bytes " * 64)
        part["index"] = 0
        pub = self.tmp / "agent" / "outbox" / "owner-copy.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(
            parts=[part], pub=pub, work=work, prelude=self._force_copy_mode(),
            epilogue=(
                "Write-Output ('VIEWMODE=' + $ownerViewMode)\n"
                f"$o = Get-AttrCudaFileId -Path '{part['file']}'\n"
                "$v = Get-AttrCudaFileId -Path $clipPath\n"
                "Write-Output ('ORIGINAL_LINKS=' + $o.NumberOfLinks)\n"
                "Write-Output ('VIEW_LINKS=' + $v.NumberOfLinks)\n"
                "Write-Output ('VIEW_REPARSE=' + $v.IsReparsePoint)\n"
                "Write-Output ('SAME_OBJECT=' + (($o.VolumeSerialNumber -eq $v.VolumeSerialNumber) -and ($o.FileIndexHigh -eq $v.FileIndexHigh) -and ($o.FileIndexLow -eq $v.FileIndexLow)))\n"
            ),
        )

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("VIEWMODE=copy", proc.stdout)
        self.assertIn("ORIGINAL_LINKS=1", proc.stdout)
        self.assertIn("VIEW_LINKS=1", proc.stdout)
        self.assertIn("VIEW_REPARSE=False", proc.stdout)
        self.assertIn("SAME_OBJECT=False", proc.stdout)
        private_dir = work / ("owner-" + "clip")
        view = private_dir / ("owner-" + "clip" + base_extension)
        self.assertEqual(view.read_bytes(), part["bytes"])
        self.assertEqual(part["file"].read_bytes(), part["bytes"])
        self.assertEqual(os.stat(part["file"]).st_nlink, 1)

    def test_symlink_mode_points_at_the_original_and_adds_no_name_to_it(self) -> None:
        # Needs a host that may create a symbolic link (Developer Mode or elevated) -- bachelor and
        # the Windows CI runners; skipped where the job itself would take the copy path.
        base_extension = "." + "MLV"
        probe = self.tmp / "symlink-capability-probe"
        probe.mkdir()
        capability = self.run_with_module(
            f"Write-Output ('CAPABILITY=' + (Test-AttrCudaSymlinkCapability -Directory '{probe}' -Journal '{probe.parent / 'probe-journal.jsonl'}').Result)\n")
        self.assertEqual(capability.returncode, 0, f"{capability.stdout}\n{capability.stderr}")
        self.assertEqual(list(probe.iterdir()), [], "the capability probe left a file behind")
        if "CAPABILITY=SYMLINK_CAPABLE" not in capability.stdout:
            self.skipTest("this host cannot create a symbolic link (no Developer Mode, not elevated)")
        src_dir = self.tmp / "owner-symlink-source"
        src_dir.mkdir()
        part = self._part(src_dir, "source" + base_extension, b"symlink mode original bytes " * 64)
        part["index"] = 0
        pub = self.tmp / "agent" / "outbox" / "owner-symlink.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(
            parts=[part], pub=pub, work=work, epilogue="Write-Output ('VIEWMODE=' + $ownerViewMode)\n")

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("VIEWMODE=symlink", proc.stdout)
        view = work / ("owner-" + "clip") / ("owner-" + "clip" + base_extension)
        self.assertTrue(view.is_symlink())
        self.assertEqual(Path(os.path.realpath(view)), Path(os.path.realpath(part["file"])))
        self.assertEqual(view.read_bytes(), part["bytes"])
        self.assertEqual(os.stat(part["file"]).st_nlink, 1)

    def test_the_pin_refuses_a_write_a_rename_and_a_delete_of_the_original_while_held(self) -> None:
        # Hub ruling item 3: the original is opened read-only with FileShare.Read for the whole
        # job, so another process cannot truncate, replace, rename or delete it under a run.
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-pin-source"
        src_dir.mkdir()
        part = self._part(src_dir, "source" + base_extension, b"pin proof original bytes " * 64)
        part["index"] = 0
        pub = self.tmp / "agent" / "outbox" / "owner-pin.artifacts"
        work = self.tmp / "work"
        renamed = src_dir / "renamed-away"

        proc = self._run_owner_content_check(
            parts=[part], pub=pub, work=work, prelude=self._force_copy_mode(),
            epilogue=(
                f"$orig = '{part['file']}'\n"
                "try { $w = [IO.File]::Open($orig, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite); $w.Dispose(); Write-Output 'WRITE_ALLOWED' } catch { Write-Output 'WRITE_REFUSED' }\n"
                "try { $t = [IO.File]::Open($orig, [IO.FileMode]::Truncate, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite); $t.Dispose(); Write-Output 'TRUNCATE_ALLOWED' } catch { Write-Output 'TRUNCATE_REFUSED' }\n"
                f"try {{ [IO.File]::Move($orig, '{renamed}'); Write-Output 'RENAME_ALLOWED' }} catch {{ Write-Output 'RENAME_REFUSED' }}\n"
                "try { [IO.File]::Delete($orig); Write-Output 'DELETE_ALLOWED' } catch { Write-Output 'DELETE_REFUSED' }\n"
            ),
        )

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        for token in ("WRITE_REFUSED", "TRUNCATE_REFUSED", "RENAME_REFUSED", "DELETE_REFUSED"):
            self.assertIn(token, proc.stdout)
        self.assertTrue(part["file"].is_file())
        self.assertFalse(renamed.exists())
        self.assertEqual(part["file"].read_bytes(), part["bytes"])

    def test_no_space_for_the_copy_fallback_refuses_before_anything_is_created(self) -> None:
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-no-space-source"
        src_dir.mkdir()
        part = self._part(src_dir, "source" + base_extension, b"no space bytes")
        part["index"] = 0
        pub = self.tmp / "agent" / "outbox" / "owner-no-space.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(
            parts=[part], pub=pub, work=work,
            prelude=self._force_copy_mode() + (
                "Set-Item -Path function:Assert-AttrCudaOwnerFootageCopySpace -Value {\n"
                "    param($Directory, $Parts)\n"
                "    throw 'OWNER_FOOTAGE_VIEW_NO_SPACE the job scratch volume cannot hold a copy of every part'\n"
                "}\n"),
        )

        self.assertEqual(proc.returncode, 21, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_VIEW_NO_SPACE", proc.stdout)
        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "OWNER_FOOTAGE_VIEW_NO_SPACE")
        self.assertEqual(summary["viewMode"], "copy")
        self.assertEqual(list((work / ("owner-" + "clip")).iterdir()), [])
        self.assertEqual(part["file"].read_bytes(), part["bytes"])

    def test_the_copy_space_check_itself_refuses_an_impossible_size(self) -> None:
        proc = self.run_with_module(_guard(
            f"Assert-AttrCudaOwnerFootageCopySpace -Directory '{self.tmp}' -Parts @([pscustomobject]@{{ length = [int64]1152921504606846976 }})"))
        self.assert_throws(proc, "OWNER_FOOTAGE_VIEW_NO_SPACE")
        proc = self.run_with_module(_guard(
            f"Assert-AttrCudaOwnerFootageCopySpace -Directory '{self.tmp}' -Parts @([pscustomobject]@{{ length = [int64]10 }})"))
        self.assertIn("NO_THROW", proc.stdout)

    def test_a_pin_failure_after_the_first_part_cleans_up_and_refuses(self) -> None:
        # ATTR3-FOOTAGE-BIND-1 PR-B round 5 (astra major), restated for OWNER-FOOTAGE-NO-HARDLINK-1.
        # Part 0 is pinned and viewed normally; part 1's pin is synthetically failed (a sharing
        # violation, raised from the script-scope Open-AttrCudaReadOnlyHandle the block calls).
        # Proves the enclosing try/catch closes the part-0 handles, removes the part-0 view entry
        # through the identity-checked primitive, leaves both real sources untouched and reports
        # the typed refusal by index only.
        base_extension = "." + "MLV"
        continuation_extension = "." + "M00"
        src_dir = self.tmp / "owner-pin-failure-source"
        src_dir.mkdir()
        part0 = self._part(src_dir, "source" + base_extension, b"pin failure part zero " * 40)
        part0["index"] = 0
        part1 = self._part(src_dir, "source" + continuation_extension, b"pin failure part one")
        part1["index"] = 1
        pub = self.tmp / "agent" / "outbox" / "owner-pin-failure.artifacts"
        work = self.tmp / "work"

        proc = self._run_owner_content_check(
            parts=[part0, part1], pub=pub, work=work,
            prelude=self._force_copy_mode() + (
                "Set-Item -Path function:Open-AttrCudaReadOnlyHandle -Value {\n"
                "    param([string]$Path)\n"
                "    if ($Path.EndsWith('.M00')) { throw [IO.IOException]::new('SYNTHETIC_PIN_FAILURE') }\n"
                "    [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)\n"
                "}\n"),
            epilogue="Write-Output 'UNREACHABLE_NO_THROW'\n",
        )

        self.assertEqual(proc.returncode, 22, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_VIEW_FAILED", proc.stdout)
        self.assertIn("PART=1", proc.stdout)
        self.assertNotIn("UNREACHABLE_NO_THROW", proc.stdout)
        private_dir = work / ("owner-" + "clip")
        self.assertTrue(private_dir.exists())
        self.assertEqual(sorted(p.name for p in private_dir.iterdir()), [])
        self.assertTrue(part0["file"].is_file())
        self.assertEqual(part0["file"].read_bytes(), part0["bytes"])
        self.assertTrue(part1["file"].is_file())
        self.assertEqual(part1["file"].read_bytes(), part1["bytes"])
        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "OWNER_FOOTAGE_VIEW_FAILED")
        self.assertEqual(summary["partIndex"], 1)
        dumped = json.dumps(summary)
        self.assertNotIn(part0["path"], dumped)
        self.assertNotIn(part1["path"], dumped)

    def test_a_view_that_changed_since_it_was_built_is_refused_by_index_only(self) -> None:
        base_extension = "." + "MLV"
        src_dir = self.tmp / "owner-changed-source"
        src_dir.mkdir()
        original = src_dir / ("source" + base_extension)
        original.write_bytes(b"changed view original bytes " * 16)
        directory = self.tmp / "owner-changed-directory"
        directory.mkdir()
        script = self.tmp / "changed-view-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"$pin = Open-AttrCudaReadOnlyHandle -Path '{original}'\n"
            f"$view = New-AttrCudaOwnerFootageView -Directory '{directory}' -Index 0 -SourcePath '{original}' -PinStream $pin -Mode copy -Journal '{directory.parent / 'changed-journal.jsonl'}'\n"
            "try { Assert-AttrCudaOwnerFootageViewsIntact -Views @($view); Write-Output 'INTACT' } catch { Write-Output ('THREW ' + $_.Exception.Message) }\n"
            # Tamper with the RECORD, as a swapped entry would present: a different file index.
            "$forged = [pscustomobject]@{ Index = $view.Index; Path = $view.Path; Mode = $view.Mode; PinId = $view.PinId; ViewStream = $view.ViewStream;\n"
            "    EntryId = [pscustomobject]@{ VolumeSerialNumber = $view.EntryId.VolumeSerialNumber; FileIndexHigh = $view.EntryId.FileIndexHigh; FileIndexLow = ($view.EntryId.FileIndexLow + 1); NumberOfLinks = 1; IsReparsePoint = $false } }\n"
            "try { Assert-AttrCudaOwnerFootageViewsIntact -Views @($forged); Write-Output 'FORGED_INTACT' } catch { Write-Output ('THREW ' + $_.Exception.Message) }\n"
            "Close-AttrCudaOwnerFootageWorkspace -Handles @($view.ViewStream, $pin) -Views @($view)\n"
            "Write-Output ('ENTRIES_AFTER_CLOSE=' + @(Get-ChildItem -LiteralPath $view.Path.Substring(0, $view.Path.LastIndexOf([char]92)) -Force).Count)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("INTACT", proc.stdout.splitlines()[0])
        self.assertIn("THREW OWNER_FOOTAGE_VIEW_CHANGED part 0", proc.stdout)
        self.assertNotIn("FORGED_INTACT", proc.stdout)
        self.assertNotIn(str(original), proc.stdout)
        self.assertIn("ENTRIES_AFTER_CLOSE=0", proc.stdout)
        self.assertEqual(original.read_bytes(), b"changed view original bytes " * 16)

    def test_a_leftover_view_is_cleared_only_if_the_journal_names_it_and_a_legacy_entry_is_left(self) -> None:
        # Clear-AttrCudaOwnerFootageLeftovers (OWNER-FOOTAGE-NO-HARDLINK-1 round 2, creator-recorded
        # ownership): a killed job never reached its cleanup, and the next run of the same job id
        # removes the entries its own journal names -- by identity. A neutral-named entry NOBODY
        # journalled is a legacy artifact (a pre-#211 hard link) and is LEFT, whatever its link count:
        # NumberOfLinks = 1 is also what the last name of old footage looks like.
        base_extension = "." + "MLV"
        work = self.tmp / "owner-leftovers-work"
        directory = work / "owner-clip"
        directory.mkdir(parents=True)
        journal = work / ".attrcuda-owned.jsonl"
        made = directory / ("owner-" + "clip" + base_extension)
        # created like the job creates a view: CreateNew, identity from the creating handle, journalled
        creator = self.run_with_module(
            f"$s = [IO.File]::Open('{made}', [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)\n"
            f"Add-AttrCudaOwnedRecord -Journal '{journal}' -Path '{made}' -FileId (Get-AttrCudaFileId -Stream $s)\n"
            "$s.Dispose()\n")
        self.assertEqual(creator.returncode, 0, f"{creator.stdout}\n{creator.stderr}")
        legacy_lone = directory / ("owner-" + "clip" + "." + "M00")
        legacy_lone.write_bytes(b"a legacy entry nobody journalled -- may be the last name of old footage")
        legacy_linked = directory / ("owner-" + "clip" + "." + "M01")
        legacy_linked.write_bytes(b"pretend this is a name of owner bytes")
        second_name = self.tmp / "the-other-name"
        os.link(legacy_linked, second_name)

        proc = self.run_with_module(
            f"$left = @(Clear-AttrCudaOwnerFootageLeftovers -Directory '{directory}' -Journal '{journal}' -WarningAction SilentlyContinue)\n"
            "Write-Output ('LEFT=' + ($left -join ','))\n")

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("LEFT=LEFT_LEGACY,LEFT_LEGACY", proc.stdout)
        self.assertFalse(made.exists(), "the journalled entry this job created is cleared")
        self.assertTrue(legacy_lone.exists(), "an unjournalled one-name entry is never adopted")
        self.assertTrue(legacy_linked.exists())
        self.assertTrue(second_name.exists())
        self.assertEqual(second_name.read_bytes(), b"pretend this is a name of owner bytes")
        # OWNER-FOOTAGE-NO-HARDLINK-2: there is no journal-less mode any more -- the call cannot be made...
        sweep = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.tmp}' -Path '{work}'"))
        self.assertIn("missing mandatory parameters", sweep.stdout + sweep.stderr)
        self.assertTrue(legacy_lone.exists() and legacy_linked.exists())
        # ...and the journalled sweep removes nothing it did not create and keeps the directories.
        sweep = self.run_with_module(
            f"$r = Remove-AttrCudaTree -TrustedRoot '{self.tmp}' -Path '{work}' -OwnedJournal '{journal}'\n"
            "Write-Output ('LEFT=' + (($r.Left | ForEach-Object { $_.Token }) -join ','))\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")
        self.assertEqual(sweep.returncode, 0, f"{sweep.stdout}\n{sweep.stderr}")
        self.assertIn("TREE_REMOVED=False", sweep.stdout)
        self.assertIn("LEFT_UNOWNED", sweep.stdout)
        self.assertIn("LEFT_MULTI_LINK", sweep.stdout)
        self.assertTrue(legacy_lone.exists())
        self.assertTrue(legacy_linked.exists())
        self.assertTrue(directory.is_dir())


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

    def test_a_corrupted_committed_object_is_indeterminate_not_not_committed(self) -> None:
        # sol/fable, PR #140 r2e MAJOR: `git rev-parse HEAD:<path>` used to throw
        # ATTR3_FIXTURE_NOT_COMMITTED for ANY failure -- non-zero exit or empty stdout -- which
        # classified an outright git/repository operational failure (a corrupted object, disk
        # I/O, an unexpected git message) identically to a genuinely untracked path. This
        # corrupts the tracked file's own TREE object in the object database (not the working
        # copy, and not the file's own blob) so `git rev-parse --show-toplevel` still succeeds
        # (this repo is not generally broken) but the HEAD:<path> lookup fails with git's own
        # "loose object ... is corrupt" -- a message that matches neither of the genuine
        # not-committed patterns this function recognises.
        tree_sha = _git_run(["rev-parse", "HEAD^{tree}"], self.repo)
        object_path = self.repo / ".git" / "objects" / tree_sha[:2] / tree_sha[2:]
        self.assertTrue(object_path.is_file(), object_path)
        object_path.chmod(0o600)
        object_path.write_bytes(bytes(range(10)))

        proc = self.run_with_module(_guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{self.tracked_path}'"))

        self.assert_throws(proc, "ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE")
        self.assertNotIn("THREW ATTR3_FIXTURE_NOT_COMMITTED", proc.stdout, proc.stdout + proc.stderr)

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

    def test_a_corrupted_repo_config_is_indeterminate_not_not_in_a_repo(self) -> None:
        # sol/fable, PR #140 r2g/r2h MAJOR, independently found and previously undriven by any
        # test: `git rev-parse --show-toplevel` used to fold ANY failure (stderr discarded via
        # 2>$null) into the definite refusal ATTR3_FIXTURE_NOT_IN_A_REPO -- classifying a
        # corrupted repository config identically to a path genuinely outside any repository.
        # This corrupts .git/config with an invalid line so `git rev-parse --show-toplevel` fails
        # with git's own "fatal: bad config line ... in file .git/config" (verified empirically --
        # exit 128, that exact stderr text) -- a real repository git simply cannot read right now,
        # which must classify as ATTR3_FIXTURE_GIT_UNAVAILABLE (could not determine), never as the
        # definite ATTR3_FIXTURE_NOT_IN_A_REPO verdict. This is the falsifier for the round-2g fix:
        # reverting the show-toplevel stderr inspection makes this test fail (it would throw
        # ATTR3_FIXTURE_NOT_IN_A_REPO instead).
        config_path = self.repo / ".git" / "config"
        with config_path.open("a", encoding="utf-8") as handle:
            handle.write("[this is not valid\n")

        proc = self.run_with_module(_guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{self.tracked_path}'"))

        self.assert_throws(proc, "ATTR3_FIXTURE_GIT_UNAVAILABLE")
        self.assertNotIn("THREW ATTR3_FIXTURE_NOT_IN_A_REPO", proc.stdout, proc.stdout + proc.stderr)

    def test_an_explicit_trusted_root_is_accepted_and_returns_the_committed_hash(self) -> None:
        # The positive control for -RepoRoot: passing the SAME repository the nearest-repo
        # discovery would have found anyway must still admit and still return the verified hash.
        proc = self.run_with_module(
            f"Write-Output (Assert-AttrCudaFixtureCommittedBytes -Path '{self.tracked_path}' -RepoRoot '{self.repo}')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        expected = _git_run(["hash-object", "--", "src/mlv/llrawproc/llrawproc.c"], self.repo)
        self.assertIn(expected, proc.stdout)

    def test_a_nested_repository_cannot_authorize_foreign_bytes(self) -> None:
        # sol, PR #140 r2 BLOCKER (ATTR3-ADMIT-CONTENT-PIN-1): without -RepoRoot pinned to the
        # caller's trusted root, the removed nearest-repo discovery walked up from the FILE'S OWN
        # DIRECTORY via `git rev-parse --show-toplevel` -- so a nested repository committed
        # under the same path could authorize bytes the outer repository's HEAD never held. A
        # nested repo is created directly in the tracked file's own directory, inside self.repo,
        # and commits DIFFERENT bytes for that file than the outer repo's HEAD holds.
        nested = self.tracked_path.parent
        _git_run(["init", "-q", "-b", "main"], nested)
        _git_run(["config", "commit.gpgsign", "false"], nested)
        _git_run(["config", "user.email", "lane@example.invalid"], nested)
        _git_run(["config", "user.name", "attr3 behaviour fixture"], nested)
        self.tracked_path.write_text("/* bytes only the nested repo ever committed */\n", encoding="utf-8")
        _git_run(["add", "-A"], nested)
        _git_run(["commit", "-q", "-m", "foreign"], nested)

        proc = self.run_with_module(
            _guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{self.tracked_path}' -RepoRoot '{self.repo}'")
        )
        self.assert_throws(proc, "ATTR3_FIXTURE_FOREIGN_REPO")

    def test_a_root_reached_through_a_junction_is_falsely_refused_known_limitation(self) -> None:
        # KNOWN LIMITATION documented on Assert-AttrCudaFixtureCommittedBytes's docstring (round-2
        # recon): neither side of the -RepoRoot comparison canonicalises a junction/symlink or an
        # 8.3 short-name component, so an EQUIVALENT root reached through one of those is falsely
        # rejected as ATTR3_FIXTURE_FOREIGN_REPO rather than accepted. Empirically confirmed here
        # (not merely asserted from the docstring's prose): `git rev-parse --show-toplevel` run
        # from inside a junction alias resolves to the REAL target path, while this function's own
        # -RepoRoot/-Path containment check compares un-resolved path TEXT -- so -Path and
        # -RepoRoot both passed consistently through the SAME alias still pass that first textual
        # containment check, then fail the second (discoveredRoot-vs-trustedRoot) comparison
        # purely because git silently resolved the reparse point and this function did not. This
        # pins the documented behaviour so a future change cannot silently start accepting -- or
        # silently start crashing on -- a junction-reached root without the docstring being
        # updated alongside it (round-2 addendum: document AND test the refusal, or canonicalise;
        # this is the testing half).
        alias = self.tmp / "repo-alias"
        proc = self.run_with_module(
            f"New-Item -ItemType Junction -Path '{alias}' -Target '{self.repo}' | Out-Null\n"
        )
        if proc.returncode != 0 or not alias.exists():
            self.skipTest(f"cannot create a junction here: {proc.stderr}")
        aliased_tracked_path = alias / "src" / "mlv" / "llrawproc" / "llrawproc.c"

        proc = self.run_with_module(
            _guard(f"Assert-AttrCudaFixtureCommittedBytes -Path '{aliased_tracked_path}' -RepoRoot '{alias}'")
        )
        self.assert_throws(proc, "ATTR3_FIXTURE_FOREIGN_REPO")


@requires_pwsh
@requires_git
class StageFixtureJobCommittedBytesWiringTests(_PwshCase):
    """attr3-stage-fixture-job.ps1 calls the new check and prints the baked hash."""

    def test_generator_source_calls_the_committed_bytes_check(self) -> None:
        # sol, PR #140 r2c: the generator no longer calls Assert-AttrCudaFixtureCommittedBytes
        # directly -- it reuses Get-UmRunFixtureAdmission (tools/profiling/UmRunDrop.psm1), which
        # already runs that identical check and hands back a SHA256 pinned to the bytes it
        # verified, so the generator's own later read binds back to THAT value instead of
        # re-deriving an independent one with a second, separately racy read.
        text = STAGE_FIXTURE_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("Get-UmRunFixtureAdmission -SourcePath $FixturePath -RepoRoot $RepoRoot", text)
        self.assertIn("$admission.ContentSha256", text)
        self.assertIn("ATTR3_FIXTURE_CONTENT_PIN_RACE", text)

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
    "gui-smoke-display-identity.ps1",
    "gui-smoke-length-gate.ps1",
)


@requires_pwsh
class SmokeRunnerClosureManifestTests(_PwshCase):
    """Get-AttrCudaSmokeRunnerClosureManifest: the EXPLICIT, pinned list that replaced discovery
    (ATTR3-SMOKE-RUNNER-DEPS-1 round 3, NARROW BY REDESIGN)."""

    def test_the_manifest_is_the_eight_pinned_repo_relative_paths_in_order(self) -> None:
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
                "tools/profiling/gui-smoke-display-identity.ps1",
                "tools/profiling/gui-smoke-length-gate.ps1",
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
            "gui-smoke-display-identity.ps1",
            "gui-smoke-length-gate.ps1",
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

    def test_all_pinned_dependency_loads_classify_cleanly(self) -> None:
        repo = self.tmp / "repo"
        runner = (
            ". (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')\n"
            ". (Join-Path $PSScriptRoot 'provenance-stamp.ps1')\n"
            "Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            ". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')\n"
            ". (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')\n"
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
            "function Write-JobTrace([string]$Message) { }\n"
            # CUDA-PERF-DISPLAY-WAKE-3 round 1: the extracted failure block now includes the
            # "before smoke launch" keep-alive health checkpoint, which reads $displayWake and
            # $displayWakeKeepAlive and calls Get-AttrCudaDisplayWakeKeepAliveHealth (already
            # available -- this script Import-Modules MODULE above, and that function is exported)
            # -- none of which this SMOKE_RUN_FAILED/PresentMon-cleanup-focused probe otherwise
            # needs. A fabricated, already-healthy handle (no real background runspace) keeps that
            # checkpoint a no-op here, so this test still proves what it always proved.
            "$displayWake = [ordered]@{}\n"
            "$displayWakeKeepAlive = [ordered]@{ setupError = $null; asyncResult = $null; stopEvent = $null; "
            "nudgeState = [ordered]@{ failureCount = 0; lastError = $null; lastFailureUtc = $null } }\n"
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

    def _display_result_tail_function(self) -> str:
        # UM-DISPLAY-SELECT-AND-LOG-1: the spawn guard's RESULT= line now appends the display tail
        # too (the pre-smoke, all-unknown display block), so the probe carries the job's REAL
        # Get-AttrCudaDisplayResultTail extracted verbatim -- never a stand-in.
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("function Get-AttrCudaDisplayResultTail")
        end = text.index("\n}\n", start) + len("\n}\n")
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
            # DVE-PRESENTMON-EVIDENCE-1: the job defines PresentMon's two stream-file paths (beside its CSV) before the span this probe extracts.
            f"$presentMonStdoutPath = '{self.tmp / 'presentmon-stdout.txt'}'\n"
            f"$presentMonStderrPath = '{self.tmp / 'presentmon-stderr.txt'}'\n"
            "$FixtureRehearsal = $true\n"
            "$SourceCommit = ('1' * 40)\n"
            "$ClipId = 'tiny_dual_iso'\n"
            f"$Pub = '{pub}'\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    $Object | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Path -Encoding UTF8\n"
            "}\n"
            "function Write-JobTrace([string]$Message) { }\n"
            "$displayBlock = [ordered]@{ displayDegraded = 'unknown' }\n"
            + self._display_result_tail_function() + "\n"
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
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{partial}' -OwnedJournal '{self.root / 'j.jsonl'}'))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("removed=False", proc.stdout)
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")
        self.assertTrue(partial.exists())

    def test_a_partial_that_is_itself_a_junction_is_left_and_its_target_survives(self) -> None:
        link = self.plant_junction(self.root, "build.json.partial")
        proc = self.run_with_module(
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{link}' -OwnedJournal '{self.root / 'j.jsonl'}'))\n"
        )
        self.assertIn("removed=False", proc.stdout)
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")

    def test_a_plain_partial_file_the_job_created_is_removed(self) -> None:
        # OWNER-FOOTAGE-NO-HARDLINK-2: removed because the job CREATED it through the journalled publish,
        # not because it is a plain file.
        partial = self.root / "exe.partial"
        proc = self.run_with_module(
            f"[void](Publish-AttrCudaText -Path '{partial}' -Value 'half written' -OwnedJournal '{self.root / 'j.jsonl'}')\n"
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{partial}' -OwnedJournal '{self.root / 'j.jsonl'}'))\n"
        )
        self.assertIn("removed=True", proc.stdout, proc.stdout + proc.stderr)
        self.assertFalse(partial.exists())

    def test_a_work_tree_containing_a_junction_never_follows_it_and_leaves_what_it_cannot_prove(self) -> None:
        # Was "refused whole" (a throw). The junction is now an entry like any other: never followed,
        # never deleted, reported LEFT_NOT_A_FILE; nothing the journal does not prove goes either.
        work = self.root / "work"
        (work / "nested").mkdir(parents=True)
        (work / "nested" / "file.txt").write_bytes(b"x")
        self.plant_junction(work / "nested")
        proc = self.run_with_module(
            f"$r = Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{work}' -OwnedJournal '{self.root / 'j.jsonl'}'\n"
            "Write-Output ('LEFT=' + ((@($r.Left | ForEach-Object { $_.Token }) | Sort-Object) -join ','))\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # the junction itself and the plain file are both unproven here: both LEFT, neither followed
        self.assertIn("LEFT=LEFT_UNOWNED,LEFT_UNOWNED", proc.stdout)
        self.assertIn("TREE_REMOVED=False", proc.stdout)
        self.assertEqual(self.sentinel.read_bytes(), b"must survive")
        self.assertTrue((work / "nested" / "file.txt").exists(), "a tree the journal does not prove must be left intact")

    def test_a_tree_reached_through_a_linked_ancestor_is_refused_and_the_target_survives(self) -> None:
        # sol PR #133 r6: AgentRoot\outbox is a junction to an outside directory holding <job>.artifacts.
        (self.outside / "job.artifacts").mkdir()
        (self.outside / "job.artifacts" / "keep.txt").write_bytes(b"outside the agent root")
        self.plant_junction(self.root, "outbox")
        target = self.root / "outbox" / "job.artifacts"
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{target}' -OwnedJournal '{self.root / 'j.jsonl'}'"))
        self.assert_throws(proc, "ATTRCUDA_ANCESTOR_IS_LINK")
        self.assertEqual((self.outside / "job.artifacts" / "keep.txt").read_bytes(), b"outside the agent root")

    def test_an_absent_tree_under_a_linked_ancestor_is_still_refused(self) -> None:
        self.plant_junction(self.root, "work")
        missing = self.root / "work" / "never-created"
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{missing}' -OwnedJournal '{self.root / 'j.jsonl'}'"))
        self.assert_throws(proc, "ATTRCUDA_ANCESTOR_IS_LINK")

    def test_a_path_outside_the_trusted_root_is_refused(self) -> None:
        escaping = str(self.root) + "\\..\\outside"
        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{escaping}' -OwnedJournal '{self.root / 'j.jsonl'}'"))
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
            f"Write-Output ('removed=' + (Remove-AttrCudaPartialFile -TrustedRoot '{self.root}' -Path '{side}' -OwnedJournal '{self.root / 'j.jsonl'}'))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("removed=False", proc.stdout)
        self.assertEqual((self.outside / "side.zip").read_bytes(), b"outside the agent root")

    def test_a_link_free_work_tree_the_job_created_fresh_is_removed(self) -> None:
        work = self.root / "work"
        made = self.run_with_module(f"[void](New-AttrCudaOwnedRoot -TrustedRoot '{self.root}' -Path '{work}' -OwnedJournal '{self.root / 'j.jsonl'}')\n")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        (work / "a" / "b").mkdir(parents=True)
        (work / "a" / "b" / "f.txt").write_bytes(b"x")
        proc = self.run_with_module(
            f"$r = Remove-AttrCudaTree -TrustedRoot '{self.root}' -Path '{work}' -OwnedJournal '{self.root / 'j.jsonl'}'\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TREE_REMOVED=True", proc.stdout)
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
            # OWNER-FOOTAGE-NO-HARDLINK-1: Remove-AttrCudaTree reads each file's link count.
            "Initialize-AttrCudaFileIdNative",
            "ConvertTo-AttrCudaFileIdObject",
            "Get-AttrCudaFileId",
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
            # OWNER-FOOTAGE-NO-HARDLINK-1: Remove-AttrCudaTree reads each file's link count.
            "Initialize-AttrCudaFileIdNative",
            "ConvertTo-AttrCudaFileIdObject",
            "Get-AttrCudaFileId",
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
            # OWNER-FOOTAGE-NO-HARDLINK-1: Remove-AttrCudaTree reads each file's link count.
            "Initialize-AttrCudaFileIdNative",
            "ConvertTo-AttrCudaFileIdObject",
            "Get-AttrCudaFileId",
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
            # OWNER-FOOTAGE-NO-HARDLINK-1: the tree delete's link count read, and the identity-checked
            # delete the view cleanup uses.
            "Initialize-AttrCudaFileIdNative",
            "ConvertTo-AttrCudaFileIdObject",
            "Get-AttrCudaFileId",
            "Remove-AttrCudaFileById",
            # UM-DISPLAY-SELECT-AND-LOG-1 round 2b/2: venue-quiescence (CPU busy time, not
            # utility) and the Windows display inventory, both embedded verbatim like every
            # other shared function.
            "Get-AttrCudaQuiescenceSample",
            "Get-AttrCudaProcessCpuSnapshot",
            "Get-AttrCudaTopCpuProcesses",
            "Get-AttrCudaWindowsDisplayInventory",
            "Register-AttrCudaDisplayWakeNativeMethods",
            "Get-AttrCudaScreensaverRunning",
            "Get-AttrCudaScreensaverTimeoutSeconds",
            "Get-AttrCudaScreensaverActive",
            "Get-AttrCudaScreensaverSecure",
            "Wait-AttrCudaScreensaverDismissed",
            "Invoke-AttrCudaInputDesktopNudge",
            "Start-AttrCudaDisplayWake",
            "Stop-AttrCudaDisplayWake",
            "Start-AttrCudaDisplayWakeKeepAlive",
            "Get-AttrCudaDisplayWakeKeepAliveHealth",
            "Stop-AttrCudaDisplayWakeKeepAlive",
            "Invoke-AttrCudaBoundedProbe",
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


# --------------------------------------------------------------------------------------------
# CUDA-PERF-DISPLAY-WAKE-1: the display-wake functions themselves, and their wiring into the
# attribution job template (wake before the smoke launch, release in `finally`, evidence recorded
# on every outcome).
# --------------------------------------------------------------------------------------------


@requires_pwsh
class DisplayWakeFunctionTests(_PwshCase):
    """Start-/Stop-AttrCudaDisplayWake and their helpers: shape and never-throws, on a real host."""

    def test_start_display_wake_never_throws_and_has_the_documented_shape(self) -> None:
        proc = self.run_with_module(
            "$w = Start-AttrCudaDisplayWake\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'w.json')}', ($w | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["attempted"], True)
        self.assertIn("SetThreadExecutionState", result["method"])
        self.assertIn(result["screensaverRunningBefore"], (True, False, None))
        self.assertIn(result["screensaverRunningAfter"], (True, False, None))
        # CUDA-PERF-DISPLAY-WAKE-3 round 1: $null/'secure'/'unknown', matching whichever of the
        # two gated screensaverSecureOwnerOnly (or neither, when it is $false).
        self.assertIn(result["screensaverSecureReason"], (None, "secure", "unknown"))
        self.assertIn("utc", result)
        # On a real Windows CI host both native calls succeed; a failure is recorded (never
        # thrown), which the never-throws test below exercises by forcing the native type absent.
        self.assertIsNone(result["sendInputError"])
        self.assertIsNone(result["executionStateError"])
        # CUDA-PERF-DISPLAY-WAKE-2: SPI_GETSCREENSAVETIMEOUT/SPI_GETSCREENSAVEACTIVE, read-only.
        self.assertTrue(
            result["screensaverTimeoutSeconds"] is None
            or isinstance(result["screensaverTimeoutSeconds"], int)
        )
        self.assertIn(result["screensaverActive"], (True, False, None))

    def test_screensaver_timeout_and_active_getters_never_throw(self) -> None:
        proc = self.run_with_module(
            "$t = Get-AttrCudaScreensaverTimeoutSeconds\n"
            "$a = Get-AttrCudaScreensaverActive\n"
            "Write-Output \"TIMEOUT=$($null -eq $t ? 'NULL' : $t)\"\n"
            "Write-Output \"ACTIVE=$($null -eq $a ? 'NULL' : $a)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"TIMEOUT=(\d+|NULL)")
        self.assertRegex(proc.stdout, r"ACTIVE=(True|False|NULL)")

    def test_stop_display_wake_never_throws_and_releases(self) -> None:
        proc = self.run_with_module(
            "[void](Start-AttrCudaDisplayWake)\n"
            "$r = Stop-AttrCudaDisplayWake\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["released"], True)
        self.assertIsNone(result["error"])
        self.assertIn("utc", result)

    def test_get_screensaver_running_never_throws(self) -> None:
        proc = self.run_with_module(
            "$s = Get-AttrCudaScreensaverRunning\n"
            "Write-Output \"RESULT=$($null -eq $s ? 'NULL' : $s)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"RESULT=(True|False|NULL)")

    def test_a_native_load_failure_is_recorded_not_thrown(self) -> None:
        # Simulates Register-AttrCudaDisplayWakeNativeMethods failing (a Win32 P/Invoke surface
        # that could not load): the real job never Import-Modules this file -- it embeds the
        # verbatim function text into ONE flat script (see Get-AttrCudaEmbeddedFunctionSource's
        # own header), so a later definition in that same scope wins. Extracted the same way here,
        # with a redefinition appended, so this proves Start-/Stop-AttrCudaDisplayWake's own error
        # handling -- not Add-Type's -- is what keeps this path from throwing, in the same flat-
        # scope shape the deployed job actually runs in (Import-Module's module-scope boundary
        # would not let a caller-side redefinition intercept the module's own internal call).
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverTimeoutSeconds','Get-AttrCudaScreensaverActive',"
            "'Get-AttrCudaScreensaverSecure','Wait-AttrCudaScreensaverDismissed',"
            "'Invoke-AttrCudaInputDesktopNudge',"
            # VENUE-SESSION-LOCKED-REFUSAL-1 r4: the claim-time lock reads go through the bounded reader.
            "'Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWake','Stop-AttrCudaDisplayWake')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "function Register-AttrCudaDisplayWakeNativeMethods { $false }\n"
                # CUDA-PERF-DISPLAY-WAKE-3 round 1b: pinned to a CONFIRMED not-running screen
                # saver, so this test isolates the native-load-failure path it actually means to
                # exercise from the fail-closed-on-unknown-running gate (a separate behavior,
                # covered by its own tests below) -- this test host's real
                # Get-AttrCudaScreensaverRunning can itself read as unknown in a non-interactive
                # session, which would otherwise make this test's outcome depend on the host.
                "function Get-AttrCudaScreensaverRunning { $false }\n"
                # VENUE-SESSION-LOCKED-REFUSAL-1: pinned unlocked, so the lock gate (its own tests
                # below) does not pre-empt the native-load-failure path this test exercises.
                "function Get-AttrCudaSessionLocked { $false }\n"
                "$w = Start-AttrCudaDisplayWake\n"
                "$r = Stop-AttrCudaDisplayWake\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'w.json')}', ($w | ConvertTo-Json -Depth 5))\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        wake = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        release = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(wake["attempted"], True)
        self.assertIs(wake["screensaverSecureOwnerOnly"], False)
        self.assertIn("ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE", wake["sendInputError"])
        self.assertIn("ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE", wake["executionStateError"])
        self.assertIs(release["released"], False)
        self.assertIn("ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE", release["error"])

    def test_get_screensaver_secure_never_throws(self) -> None:
        proc = self.run_with_module(
            "$s = Get-AttrCudaScreensaverSecure\n"
            "Write-Output \"RESULT=$($null -eq $s ? 'NULL' : $s)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"RESULT=(True|False|NULL)")

    def test_input_desktop_nudge_never_throws_and_has_the_documented_shape(self) -> None:
        proc = self.run_with_module(
            "$n = Invoke-AttrCudaInputDesktopNudge\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'n.json')}', ($n | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "n.json").read_text(encoding="utf-8"))
        self.assertIs(result["attempted"], True)
        for key in ("openInputDesktopError", "setThreadDesktopError", "sendInputError",
                    "closeDesktopError"):
            self.assertTrue(
                result[key] is None or isinstance(result[key], str), f"{key}={result[key]!r}")
        self.assertIn(result["threadJoined"], (True, False))
        # CUDA-PERF-DISPLAY-WAKE-3 round 1: on a real host, OpenInputDesktop/SetThreadDesktop
        # succeed regardless of whether SendInput itself does (a headless/locked session can still
        # deny SendInput), and CloseDesktop -- now switched-back-to first -- must then succeed too.
        if result["openInputDesktopError"] is None and result["setThreadDesktopError"] is None:
            self.assertIsNone(result["closeDesktopError"])

    def test_input_desktop_nudge_does_not_throw_when_native_type_is_unavailable(self) -> None:
        # Same simulated-failure shape as test_a_native_load_failure_is_recorded_not_thrown.
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Invoke-AttrCudaInputDesktopNudge')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "function Register-AttrCudaDisplayWakeNativeMethods { $false }\n"
                "$n = Invoke-AttrCudaInputDesktopNudge\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'n.json')}', ($n | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "n.json").read_text(encoding="utf-8"))
        self.assertIs(result["attempted"], False)
        self.assertIn("ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE", result["openInputDesktopError"])
        self.assertIs(result["threadJoined"], False)

    _SAME_AS_BEFORE = object()

    def _wake_with_overrides(self, *, running, secure, nudge_override: str,
                              running_after=_SAME_AS_BEFORE) -> tuple:
        """Extract Start-AttrCudaDisplayWake's real closure, then override
        Get-AttrCudaScreensaverRunning/-Secure and Invoke-AttrCudaInputDesktopNudge -- same
        flat-scope override technique as test_a_native_load_failure_is_recorded_not_thrown, since
        the real deployed job is one flat script, never an Import-Module boundary.
        Get-AttrCudaScreensaverRunning is called TWICE by Start-AttrCudaDisplayWake (before and
        after the dismiss attempt); a call counter lets the override answer them differently.
        -running_after defaults to the same value as -running, matching every pre-round-2 caller
        that only cared about a single before/after reading."""
        if running_after is self._SAME_AS_BEFORE:
            running_after = running
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverTimeoutSeconds','Get-AttrCudaScreensaverActive',"
            "'Get-AttrCudaScreensaverSecure','Wait-AttrCudaScreensaverDismissed',"
            "'Invoke-AttrCudaInputDesktopNudge',"
            # VENUE-SESSION-LOCKED-REFUSAL-1 r4: the claim-time lock reads go through the bounded reader.
            "'Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWake')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        running_literal = "$null" if running is None else ("$true" if running else "$false")
        running_after_literal = "$null" if running_after is None else ("$true" if running_after else "$false")
        secure_literal = "$null" if secure is None else ("$true" if secure else "$false")
        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "$script:__wakeRunningCall = 0\n"
                "function Get-AttrCudaScreensaverRunning {\n"
                "    $script:__wakeRunningCall++\n"
                f"    if ($script:__wakeRunningCall -le 1) {{ {running_literal} }} else {{ {running_after_literal} }}\n"
                "}\n"
                f"function Get-AttrCudaScreensaverSecure {{ {secure_literal} }}\n"
                f"function Invoke-AttrCudaInputDesktopNudge {{ {nudge_override} }}\n"
                # VENUE-SESSION-LOCKED-REFUSAL-1: pinned unlocked -- these tests are about the
                # screen-saver gates; the lock gate has its own tests (SessionLockedWakeTests).
                "function Get-AttrCudaSessionLocked { $false }\n"
                "$w = Start-AttrCudaDisplayWake\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'w.json')}', ($w | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        return proc

    def test_secure_and_running_stops_before_any_dismiss_attempt(self) -> None:
        # The redefined Invoke-AttrCudaInputDesktopNudge THROWS: if Start-AttrCudaDisplayWake ever
        # called it on this (secure) path, the probe itself would fail with a nonzero exit --
        # this is the mutation-style proof that the secure gate actually short-circuits, not just
        # that the returned flag happens to read true.
        proc = self._wake_with_overrides(
            running=True, secure=True,
            nudge_override="throw 'MUST NOT BE CALLED when the screen saver is secure'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], True)
        self.assertIs(result["screensaverSecure"], True)
        self.assertIs(result["screensaverSecureOwnerOnly"], True)
        self.assertEqual(result["screensaverSecureReason"], "secure")
        self.assertIsNone(result["inputDesktopNudge"])
        self.assertIn("ATTRCUDA_SCREENSAVER_SECURE_OWNER_ONLY", result["sendInputError"])
        self.assertIn("SecureScreensaverNoDismissAttempted", result["method"])

    def test_unknown_secure_state_stops_before_any_dismiss_attempt(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol BLOCKER fix): a probe failure (Get-
        # AttrCudaScreensaverSecure returning $null) must gate exactly like a confirmed-secure
        # screen saver, never fall through to a dismiss attempt. Same mutation-style proof as the
        # confirmed-secure test above: the redefined nudge THROWS if this branch is ever reached.
        proc = self._wake_with_overrides(
            running=True, secure=None,
            nudge_override="throw 'MUST NOT BE CALLED when the secure state is unknown'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], True)
        self.assertIsNone(result["screensaverSecure"])
        self.assertIs(result["screensaverSecureOwnerOnly"], True)
        self.assertEqual(result["screensaverSecureReason"], "unknown")
        self.assertIsNone(result["inputDesktopNudge"])
        self.assertIn("ATTRCUDA_SCREENSAVER_SECURE_OWNER_ONLY", result["sendInputError"])
        self.assertIn("SecureScreensaverNoDismissAttempted", result["method"])

    def test_running_and_not_secure_dispatches_to_the_input_desktop_nudge(self) -> None:
        # The canary string proves the DEDICATED-THREAD path ran, not the plain SendInput path --
        # a mutation that made this branch fall through to plain SendInput would still record
        # sendInputError=$null on a real host, which this canary distinguishes from.
        proc = self._wake_with_overrides(
            running=True, secure=False,
            nudge_override=(
                "[ordered]@{ attempted=$true; openInputDesktopError=$null; "
                "setThreadDesktopError=$null; sendInputError='CANARY_INPUT_DESKTOP_NUDGE_CALLED'; "
                "threadJoined=$true }"
            ))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverSecureOwnerOnly"], False)
        self.assertEqual(result["sendInputError"], "CANARY_INPUT_DESKTOP_NUDGE_CALLED")
        self.assertEqual(result["inputDesktopNudge"]["sendInputError"], "CANARY_INPUT_DESKTOP_NUDGE_CALLED")
        self.assertIn("OpenInputDesktop", result["method"])

    def test_not_running_never_calls_the_input_desktop_nudge(self) -> None:
        # Mirrors the secure test's mutation-style proof, for the opposite branch: when the
        # screen saver is not already running, the plain SendInput path must be used, never the
        # dedicated-thread one -- the redefined Invoke-AttrCudaInputDesktopNudge throws.
        proc = self._wake_with_overrides(
            running=False, secure=False,
            nudge_override="throw 'MUST NOT BE CALLED when the screen saver is not running'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], False)
        self.assertIsNone(result["inputDesktopNudge"])
        self.assertNotIn("OpenInputDesktop", result["method"])

    def test_unknown_running_state_stops_before_any_dismiss_attempt_even_when_confirmed_not_secure(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 1b (sol BLOCKER fix): a RUNNING-state probe failure
        # (Get-AttrCudaScreensaverRunning returning $null) must gate exactly like a confirmed-
        # running+secure screen saver, never fall through to the plain SendInput branch that is
        # reached when running reads $false -- secure dismissal cannot be ruled out if this job
        # does not even know whether a screen saver is running. Pinned with secure=False (a
        # CONFIRMED not-secure reading): the prior code's `-eq $true` gate on screensaverBefore let
        # exactly this combination fall through, since it only ever looked at .screensaverSecure,
        # never at whether .screensaverBefore itself was confirmed. Same mutation-style proof as
        # the other unknown-state test: the redefined nudge THROWS if this branch is ever reached.
        proc = self._wake_with_overrides(
            running=None, secure=False,
            nudge_override="throw 'MUST NOT BE CALLED when whether the screen saver is running is unknown'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["screensaverRunningBefore"])
        self.assertIs(result["screensaverSecure"], False)
        self.assertIs(result["screensaverSecureOwnerOnly"], True)
        self.assertEqual(result["screensaverSecureReason"], "running_unknown")
        self.assertIsNone(result["inputDesktopNudge"])
        self.assertIn("ATTRCUDA_SCREENSAVER_SECURE_OWNER_ONLY", result["sendInputError"])
        self.assertIn("whether a screen saver is running could not be read", result["sendInputError"])
        self.assertIn("SecureScreensaverNoDismissAttempted", result["method"])

    def test_unknown_running_state_stops_before_any_dismiss_attempt_when_secure_also_unknown(self) -> None:
        # Same gate, both probes failing at once -- the reason must still read running_unknown
        # (running is the more specific diagnosis), not the generic "unknown" a secure-only
        # failure would report.
        proc = self._wake_with_overrides(
            running=None, secure=None,
            nudge_override="throw 'MUST NOT BE CALLED when both probes are unknown'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["screensaverRunningBefore"])
        self.assertIsNone(result["screensaverSecure"])
        self.assertIs(result["screensaverSecureOwnerOnly"], True)
        self.assertEqual(result["screensaverSecureReason"], "running_unknown")
        self.assertIsNone(result["inputDesktopNudge"])

    def test_confirmed_not_running_still_dismisses_regardless_of_secure_reading(self) -> None:
        # The other half of "only a definite running=$false ... may nudge": a CONFIRMED
        # not-running screen saver still takes the plain SendInput path even when the secure
        # probe itself is unknown -- there is no screen saver to be secure about. Mutation-style
        # proof via the canary, same technique as test_not_running_never_calls_the_input_desktop_nudge.
        proc = self._wake_with_overrides(
            running=False, secure=None,
            nudge_override="throw 'MUST NOT BE CALLED when the screen saver is confirmed not running'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], False)
        self.assertIs(result["screensaverSecureOwnerOnly"], False)
        self.assertIsNone(result["inputDesktopNudge"])
        self.assertNotIn("OpenInputDesktop", result["method"])

    def test_dismiss_failed_is_false_when_no_screen_saver_was_running(self) -> None:
        proc = self._wake_with_overrides(
            running=False, secure=False,
            nudge_override="throw 'MUST NOT BE CALLED when the screen saver is not running'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["dismissFailed"], False)

    def test_dismiss_failed_is_false_when_secure_owner_only_already_gated(self) -> None:
        proc = self._wake_with_overrides(
            running=True, secure=True,
            nudge_override="throw 'MUST NOT BE CALLED when the screen saver is secure'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["dismissFailed"], False)

    def test_dismiss_failed_is_false_when_the_dismiss_actually_ended_the_screen_saver(self) -> None:
        proc = self._wake_with_overrides(
            running=True, secure=False, running_after=False,
            nudge_override=(
                "[ordered]@{ attempted=$true; openInputDesktopError=$null; "
                "setThreadDesktopError=$null; sendInputError=$null; threadJoined=$true }"
            ))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], True)
        self.assertIs(result["screensaverRunningAfter"], False)
        self.assertIs(result["dismissFailed"], False)

    def test_dismiss_failed_is_true_when_still_running_after_a_dismiss_attempt(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence): both Ultra-Magnus legs recorded
        # screensaverRunningAfter=TRUE with SendInput denied on every keep-alive tick -- the exact
        # shape this pins, regardless of whether OpenInputDesktop/SetThreadDesktop themselves
        # succeeded (they did, on both legs).
        proc = self._wake_with_overrides(
            running=True, secure=False, running_after=True,
            nudge_override=(
                "[ordered]@{ attempted=$true; openInputDesktopError=$null; "
                "setThreadDesktopError=$null; "
                "sendInputError='SendInput sent 0 of 2 events (lastError=5)'; threadJoined=$true }"
            ))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], True)
        self.assertIs(result["screensaverRunningAfter"], True)
        self.assertIs(result["dismissFailed"], True)

    def test_dismiss_failed_is_true_when_the_after_probe_itself_fails(self) -> None:
        # Fail-closed, same shape as the secure/running-unknown gates above: an unknown after-state
        # must never be read as "dismissed successfully" just because nothing threw.
        proc = self._wake_with_overrides(
            running=True, secure=False, running_after=None,
            nudge_override=(
                "[ordered]@{ attempted=$true; openInputDesktopError=$null; "
                "setThreadDesktopError=$null; sendInputError=$null; threadJoined=$true }"
            ))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))
        self.assertIs(result["screensaverRunningBefore"], True)
        self.assertIsNone(result["screensaverRunningAfter"])
        self.assertIs(result["dismissFailed"], True)
        self.assertNotIn("SecureScreensaverNoDismissAttempted", result["method"])


@requires_pwsh
class ScreensaverDismissWaitTests(_PwshCase):
    """CUDA-PERF-DISPLAY-WAKE-3 round 3 (live UM evidence, defect class fix): three UM legs all
    recorded every nudge step succeeding (OpenInputDesktop/SetThreadDesktop/SendInput, thread
    joined) yet screensaverRunningAfter stayed true, because the prior after-read called
    Get-AttrCudaScreensaverRunning exactly once, IMMEDIATELY after the nudge, with no allowance
    for the screen saver process to actually receive and act on the injected input. These are
    direct tests of Wait-AttrCudaScreensaverDismissed, the bounded poll Start-AttrCudaDisplayWake
    now calls instead -- by the embedding contract EmbeddedFunctionContractTests proves (byte-
    identical extraction), testing it directly here is equivalent to testing it through
    Start-AttrCudaDisplayWake, at a fraction of the wall-clock cost: a small
    -PollIntervalMilliseconds/-TimeoutMilliseconds here rather than Start-AttrCudaDisplayWake's own
    real 250ms/5000ms defaults (which DisplayWakeFunctionTests' still-running/unknown-after-probe
    dismissFailed tests exercise unmodified, at their real cost, so the wiring itself is proven
    too)."""

    def _wait_with_stub(self, *, stub_body: str, timeout_ms: int = 2000,
                         interval_ms: int = 10) -> dict:
        # Same flat-scope override technique as DisplayWakeFunctionTests._wake_with_overrides:
        # Wait-AttrCudaScreensaverDismissed calls Get-AttrCudaScreensaverRunning INTERNALLY, and an
        # Import-Module boundary would keep a caller-side redefinition from ever reaching that
        # internal call -- the real deployed job never has that boundary (both functions are
        # embedded verbatim into one flat script), so this proves the same thing the real job runs.
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Get-AttrCudaScreensaverRunning','Wait-AttrCudaScreensaverDismissed')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                f"function Get-AttrCudaScreensaverRunning {{ {stub_body} }}\n"
                f"$w = Wait-AttrCudaScreensaverDismissed -TimeoutMilliseconds {timeout_ms} "
                f"-PollIntervalMilliseconds {interval_ms}\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'w.json')}', ($w | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))

    def test_stops_the_instant_a_poll_reads_confirmed_false(self) -> None:
        # Mutation-style proof: a stub that turns false only on its THIRD call. A premature break
        # (polling once and trusting it) would read pollCount=1/running=True here; an off-by-one
        # would read pollCount=2 or 4. Only the real "poll until false or timed out" loop lands on
        # exactly 3.
        result = self._wait_with_stub(stub_body=(
            "$script:__pollCount = [int]$script:__pollCount + 1\n"
            "if ($script:__pollCount -ge 3) { return $false }\n"
            "return $true\n"
        ))
        self.assertIs(result["running"], False)
        self.assertEqual(result["pollCount"], 3)

    def test_a_stub_that_never_turns_false_exhausts_the_full_timeout_window(self) -> None:
        # The other half of the same mutation-style proof: a stub that ALWAYS reads running=true
        # must not be trusted as dismissed just because it was called a bounded number of times --
        # the wait must actually spend (at least) its whole timeout budget polling before giving up,
        # never return early on a merely-large poll count.
        result = self._wait_with_stub(stub_body="return $true\n", timeout_ms=300, interval_ms=50)
        self.assertIs(result["running"], True)
        self.assertGreaterEqual(result["elapsedMilliseconds"], 300)
        self.assertGreaterEqual(result["pollCount"], 2)

    def test_an_unknown_reading_is_never_coerced_into_a_guess(self) -> None:
        # Fail-closed, same shape as Start-AttrCudaDisplayWake's own secure/running-unknown gates:
        # a probe failure ($null) must be returned exactly as read, never silently treated as
        # either a confirmed dismiss or a confirmed still-running.
        result = self._wait_with_stub(stub_body="return $null\n", timeout_ms=200, interval_ms=50)
        self.assertIsNone(result["running"])

    def test_default_parameters_match_the_documented_contract(self) -> None:
        # "up to 5 s at 250 ms" -- pinned against the real module text (never against a redefined
        # copy), so a default drifting away from the contract this round promised is caught here.
        text = MODULE.read_text(encoding="utf-8")
        func_at = text.index("function Wait-AttrCudaScreensaverDismissed {")
        param_block_at = text.index("param(", func_at)
        param_end_at = text.index(")\n", param_block_at)
        block = text[param_block_at:param_end_at]
        self.assertIn("[int]$TimeoutMilliseconds = 5000", block)
        self.assertIn("[int]$PollIntervalMilliseconds = 250", block)


@requires_pwsh
class WakeLifetimeFinallyHardeningTests(_PwshCase):
    """CUDA-PERF-DISPLAY-WAKE-3 round 1b (sol HARDENING): a LIVE proof that the outer try/finally
    actually runs Stop-AttrCudaDisplayWakeKeepAlive/Stop-AttrCudaDisplayWake on a terminating error
    struck in the stretch between the keep-alive starting and the job's FORMER try boundary --
    not just that the source text places that stretch inside the try (DisplayWakeJobOrderingTests'
    own static test already proves the placement). Same flat-scope embedding technique as
    DisplayWakeFunctionTests, since the real deployed job never has an Import-Module boundary."""

    def test_a_terminating_error_after_the_keep_alive_starts_still_stops_both(self) -> None:
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverTimeoutSeconds','Get-AttrCudaScreensaverActive',"
            "'Get-AttrCudaScreensaverSecure','Wait-AttrCudaScreensaverDismissed',"
            "'Invoke-AttrCudaInputDesktopNudge',"
            "'Start-AttrCudaDisplayWake','Stop-AttrCudaDisplayWake',"
            "'Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWakeKeepAlive','Stop-AttrCudaDisplayWakeKeepAlive')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        marker = self.tmp / "stopped.txt"
        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                # Pinned deterministic: not the branch under test here (that is
                # DisplayWakeFunctionTests' own job), just kept out of the way.
                "function Get-AttrCudaScreensaverRunning { $false }\n"
                # Last-definition-wins, same flat scope: these canaries replace the real
                # Stop-* functions, so this test proves ONLY that they get CALLED on this
                # path -- their own internals are already proven non-throwing elsewhere
                # (DisplayWakeKeepAliveFunctionTests/DisplayWakeFunctionTests).
                "function Stop-AttrCudaDisplayWakeKeepAlive { param($Handle) "
                f"Add-Content -LiteralPath '{marker}' -Value 'KEEPALIVE_STOPPED'\n"
                "  [ordered]@{ stopped = $true; error = $null } }\n"
                "function Stop-AttrCudaDisplayWake { "
                f"Add-Content -LiteralPath '{marker}' -Value 'WAKE_RELEASED'\n"
                "  [ordered]@{ released = $true; error = $null } }\n"
                "try {\n"
                "$displayWake = Start-AttrCudaDisplayWake\n"
                "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive\n"
                # Simulates a terminating error in the stretch this round closes -- package
                # expansion, footage resolution, build-manifest verification all sit here in the
                # real job, none of which are worth reproducing just to prove this.
                "throw 'SIMULATED_TERMINATING_ERROR_BEFORE_THE_FORMER_TRY_BOUNDARY'\n"
                "} finally {\n"
                "[void](Stop-AttrCudaDisplayWakeKeepAlive -Handle $displayWakeKeepAlive)\n"
                "[void](Stop-AttrCudaDisplayWake)\n"
                "}\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertNotEqual(proc.returncode, 0,
                             "the simulated terminating error should still propagate past the finally")
        self.assertIn("SIMULATED_TERMINATING_ERROR_BEFORE_THE_FORMER_TRY_BOUNDARY", proc.stderr)
        self.assertTrue(marker.exists(), "the finally block never ran at all")
        content = marker.read_text(encoding="utf-8")
        self.assertIn("KEEPALIVE_STOPPED", content)
        self.assertIn("WAKE_RELEASED", content)


class DisplayWakeNeverChangesScreensaverOrPowerSettingsTests(unittest.TestCase):
    """CUDA-PERF-DISPLAY-WAKE-2 round 1c: codifies round 1b's manual check ("No SPI_SET* calls
    exist anywhere in the diff") as an automated text scan, over both the module and every
    generator that embeds it. Matches full SPI_SET<ACTION> constant names, never the bare
    "SPI_SET*" wildcard prose this file's own comments use to state the invariant itself."""

    _SPI_SET_ACTION_RX = re.compile(r"SPI_SET[A-Z]")

    def test_no_spi_set_action_appears_anywhere(self) -> None:
        for label, path in (("module", MODULE), ("generator", ATTRIBUTION_GENERATOR)):
            with self.subTest(label=label):
                text = path.read_text(encoding="utf-8")
                match = self._SPI_SET_ACTION_RX.search(text)
                self.assertIsNone(match, f"found {match and match.group(0)!r} in {path.name}")


class InputDesktopNudgeMaskTests(unittest.TestCase):
    """CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence): OpenInputDesktop/SetThreadDesktop
    succeeded on both live legs (openInputDesktopError=null, setThreadDesktopError=null) but every
    SendInput on the reassigned thread still returned lastError=5 (ERROR_ACCESS_DENIED) --
    DESKTOP_JOURNALPLAYBACK is the access bit Microsoft documents SendInput as needing on the
    desktop it injects into, which the round 1 mask (READOBJECTS|WRITEOBJECTS|SWITCHDESKTOP) did
    not include."""

    def setUp(self) -> None:
        self.text = MODULE.read_text(encoding="utf-8")

    def test_mask_includes_desktop_journalplayback(self) -> None:
        self.assertIn("uint DESKTOP_JOURNALPLAYBACK = 0x0020;", self.text)
        desired_access_at = self.text.index("uint desiredAccess = DESKTOP_READOBJECTS")
        line_end_at = self.text.index(";", desired_access_at)
        line = self.text[desired_access_at:line_end_at]
        self.assertIn("DESKTOP_JOURNALPLAYBACK", line,
                       "DESKTOP_JOURNALPLAYBACK must be OR'd into the mask actually passed to "
                       "OpenInputDesktop, not just declared")

    def test_mask_stays_minimal_exactly_the_four_named_bits(self) -> None:
        # The fable-note constraint from round 1 still holds: no fallback to a broader mask (e.g.
        # GENERIC_ALL) on a continued denial -- a continued failure with this bit present points at
        # UIPI instead. Pinned to the exact OR expression, not just "GENERIC_ALL absent somewhere",
        # so an unrelated broadening would still fail this test.
        desired_access_at = self.text.index("uint desiredAccess =")
        line_end_at = self.text.index(";", desired_access_at)
        line = self.text[desired_access_at:line_end_at]
        self.assertEqual(
            "uint desiredAccess = DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | "
            "DESKTOP_SWITCHDESKTOP | DESKTOP_JOURNALPLAYBACK",
            line)


class InputDesktopNudgeKeyboardTapTests(unittest.TestCase):
    """CUDA-PERF-DISPLAY-WAKE-3 round 3 (live UM evidence, defect class fix): OpenInputDesktop/
    SetThreadDesktop/SendInput all succeeded on three live UM legs, yet screensaverRunningAfter
    stayed true on every one -- the net-zero 1-pixel mouse move the InputDesktopNudge.Run body used
    to send is exactly the kind of sub-threshold WM_MOUSEMOVE a classic scrnsave window procedure
    is documented to ignore. Static text checks on the C# nudge array itself (never evaluated --
    the real Win32 call cannot be proven this way in CI), so a regression that drops the keyboard
    tap or shrinks the mouse move back to 1px fails here rather than only showing up as another
    live UM DISPLAY_WAKE_DISMISS_FAILED leg."""

    def setUp(self) -> None:
        self.text = MODULE.read_text(encoding="utf-8")
        nudge_at = self.text.index("INPUT[] nudge = new INPUT[] {")
        array_end_at = self.text.index("};", nudge_at)
        self.nudge_array = self.text[nudge_at:array_end_at]

    def test_nudge_includes_a_key_down_and_a_key_up_of_the_same_inert_key(self) -> None:
        # Removing either event, or the key tap entirely, reds this: the array must carry exactly
        # one INPUT_KEYBOARD entry with dwFlags=0 (down) and exactly one with KEYEVENTF_KEYUP (up),
        # both naming the SAME virtual key so a mutation that tapped two different keys is also
        # caught.
        self.assertIn("ushort VK_F15 = 0x7E;", self.text,
                       "VK_F15 (0x7E) is the pinned inert key -- one of the F13-F24 block Windows "
                       "binds to nothing by default, and unlike Shift can never trigger the "
                       "Sticky Keys prompt")
        self.assertEqual(
            1, self.nudge_array.count("wVk = VK_F15, wScan = 0, dwFlags = 0, time = 0"),
            "exactly one key-DOWN event (dwFlags=0) for VK_F15")
        self.assertEqual(
            1, self.nudge_array.count(
                "wVk = VK_F15, wScan = 0, dwFlags = KEYEVENTF_KEYUP, time = 0"),
            "exactly one key-UP event (KEYEVENTF_KEYUP) for VK_F15")

    def test_key_tap_is_never_repeated_within_a_single_nudge_call(self) -> None:
        # Sticky Keys triggers on FIVE presses of Shift specifically; VK_F15 cannot trigger it at
        # all, but this pins the deliberate one-tap-per-call design choice itself, independent of
        # which key is chosen -- a mutation that sent the down+up pair twice (e.g. "for extra
        # reliability") would still pass the down/up-count test above only if it also duplicated
        # both, so this counts INPUT_KEYBOARD entries directly.
        keyboard_events = self.nudge_array.count("type = (int)INPUT_KEYBOARD")
        self.assertEqual(2, keyboard_events, "exactly one down + one up, never more")

    def test_mouse_move_is_above_a_single_pixel_and_still_net_zero(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 3 diagnosis (b): a screen saver ignores sub-threshold
        # mouse motion, which is why the round-2 net-zero 1-pixel move alone was not enough. The
        # move must still be net-zero (dx=8 then dx=-8) so the owner's cursor never visibly ends up
        # somewhere else.
        self.assertEqual(1, self.nudge_array.count("dx = 8,"))
        self.assertEqual(1, self.nudge_array.count("dx = -8,"))
        self.assertNotIn("dx = 1,", self.nudge_array)
        self.assertNotIn("dx = -1,", self.nudge_array)

    def test_nudge_array_has_exactly_two_mouse_and_two_keyboard_events(self) -> None:
        self.assertEqual(2, self.nudge_array.count("type = (int)INPUT_MOUSE"))
        self.assertEqual(2, self.nudge_array.count("type = (int)INPUT_KEYBOARD"))

    def test_input_struct_declares_both_mouse_and_keyboard_union_arms_at_the_same_offset(self) -> None:
        # The INPUT struct is a real Win32 union: mi and ki must overlay the SAME FieldOffset(8),
        # never sit at distinct offsets (which would silently corrupt whichever one SendInput reads
        # based on .type).
        struct_at = self.text.index("public struct INPUT")
        struct_end_at = self.text.index("}", struct_at)
        block = self.text[struct_at:struct_end_at]
        self.assertIn("[FieldOffset(8)] public MOUSEINPUT mi;", block)
        self.assertIn("[FieldOffset(8)] public KEYBDINPUT ki;", block)


class DisplayWakeJobOrderingTests(unittest.TestCase):
    """Static ordering/shape checks on the generator's own template text (no pwsh required)."""

    def setUp(self) -> None:
        self.text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")

    def test_wake_runs_before_the_smoke_launch_and_release_runs_in_finally(self) -> None:
        wake_at = self.text.index("$displayWake = Start-AttrCudaDisplayWake")
        # The smoke launch: the nested pwsh.exe invocation that actually runs MLVApp.exe via
        # run-release-gui-smoke.ps1 (see the generator's own $cmd construction above it).
        launch_at = self.text.index(
            '& "$env:ProgramFiles\\PowerShell\\7\\pwsh.exe" -NoLogo -NoProfile -NonInteractive '
            '-ExecutionPolicy Bypass -Command $cmd', wake_at)
        release_at = self.text.index("[void](Stop-AttrCudaDisplayWake)", launch_at)
        finally_at = self.text.rindex("} finally {", wake_at, release_at)
        self.assertGreater(launch_at, wake_at, "the leg must launch MLVApp only after the wake")
        self.assertGreater(release_at, finally_at, "the release must run in the `finally` block")

    def test_the_outer_try_opens_immediately_before_the_wake_so_the_secure_exit_is_covered_too(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 1b (sol HARDENING): the outer try/finally used to open
        # only at the owner-footage boundary, well after Start-AttrCudaDisplayWake, the secure-
        # owner-only `exit 25`, and Start-AttrCudaDisplayWakeKeepAlive -- a terminating error
        # anywhere in that stretch (package expansion, footage resolution, build-manifest
        # verification) released neither the wake nor the keep-alive explicitly, relying only on
        # process termination. The `try` now opens on the line immediately before the wake, so
        # every one of those three sits inside it.
        combined = "try {\n$displayWake = Start-AttrCudaDisplayWake"
        try_at = self.text.index(combined)
        self.assertEqual(1, self.text.count(combined),
                          "exactly one `try` should open directly on top of the wake")
        wake_at = try_at + len("try {\n")
        secure_exit_at = self.text.index(
            "if ($displayWake.screensaverSecureOwnerOnly) {", wake_at)
        keep_alive_at = self.text.index(
            "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive", secure_exit_at)
        # Bounded to start searching after the keep-alive: an unrelated, embedded function
        # earlier in this same script has its own inner "} finally {" (a different try entirely),
        # which an unbounded .index() would match first.
        finally_at = self.text.index("} finally {", keep_alive_at)
        self.assertLess(wake_at, secure_exit_at)
        self.assertLess(secure_exit_at, keep_alive_at)
        self.assertLess(keep_alive_at, finally_at,
                         "the keep-alive start must sit inside the same try the finally closes")

    def test_release_is_the_last_statement_in_finally_so_it_always_runs(self) -> None:
        finally_at = self.text.index("} finally {")
        close_workspace_at = self.text.index("Close-AttrCudaOwnerFootageWorkspace", finally_at)
        release_at = self.text.index("[void](Stop-AttrCudaDisplayWake)", close_workspace_at)
        end_template_at = self.text.index("'@", release_at)
        self.assertLess(release_at, end_template_at)

    def test_display_wake_evidence_is_recorded_on_every_outcome_after_the_wake(self) -> None:
        wake_at = self.text.index("$displayWake = Start-AttrCudaDisplayWake")
        end_template_at = self.text.index("'@", wake_at)
        body = self.text[wake_at:end_template_at]
        # Every Save-Json ... 'summary.json' call in this leg's body, after the wake, carries
        # displayWake -- CUDA-PERF-DISPLAY-WAKE-2 round 1c moved the wake to the very top of the
        # job (before footage resolution and package verification), so the failure paths this now
        # covers grew from 9 to 15: SCREENSAVER_SECURE_OWNER_ONLY, FIXTURE_CONTENT_MISMATCH, the
        # two OWNER_FOOTAGE_NOT_VERIFIED sites, OWNER_PARTS_NOT_CONTIGUOUS, the owner-link failure
        # (OWNER_FOOTAGE_VIEW_FAILED / _VIEW_NO_SPACE / _VIEW_CHANGED), VENUE_NOT_QUIESCENT,
        # SMOKE_RUN_FAILED, SMOKE_LOG_UNAVAILABLE, PRESENTMON_UNAVAILABLE, BACKEND_NOT_AVAILABLE,
        # GPU_RECON_FRAMES_ZERO, CPU_FALLBACK_DETECTED, the displayReport failure including
        # DISPLAY_ASLEEP itself, and the success summary.json. CUDA-PERF-DISPLAY-WAKE-3 round 1
        # adds two more: the two KEEPALIVE_FAILED checkpoints (before the smoke launch, and again
        # at the start of the measured interval) -- 15 grows to 17. Round 1b adds a third
        # KEEPALIVE_FAILED checkpoint (after the measured interval ends) -- 17 grows to 18. Round 2
        # adds the DISPLAY_WAKE_DISMISS_FAILED gate -- 18 grows to 19. Round 4 (fork/master merge,
        # PRESENTMON-HARNESS-ROBUSTNESS-1) adds a 20th: the PRESENTMON_UNAVAILABLE early exit when
        # Start-PresentMonCapture itself throws on spawn, before this branch's own wake existed on
        # master -- displayWake=$displayWake was added to that block to match every sibling site.
        # OWNER-FOOTAGE-NO-HARDLINK-1 adds a 21st: OWNER_FOOTAGE_VIEW_CHANGED, the last look at the
        # owner-footage views right before the smoke launch (the old owner-link failure site stays a
        # single site, now OWNER_FOOTAGE_VIEW_NO_SPACE / _FAILED / _CHANGED by token). Round 2 adds a
        # 22nd: OWNER_WORK_NOT_CLEAN (exit 28), the typed refusal when the start-of-run sweep left an
        # entry the job did not create (a legacy neutral entry, an unjournalled file) -- 21 grows to 22.
        # PLAYBACK-CLIP-LENGTH-ENFORCE-3 adds a 23rd: SOURCE_FRAMES_INVALID (exit 29), the receipt oracle that ends the
        # leg when the measured session did not advance its required source frames (or wrapped, or was override-paced).
        # VENUE-SESSION-LOCKED-REFUSAL-1 adds a 24th: SESSION_LOCKED_OWNER_ONLY (exit 30), the locked-console refusal.
        # r2 adds 25-27: SESSION_LOCKED_OWNER_ONLY at the three keep-alive checkpoints (start_of_measured_interval, before_smoke_launch, after_measured_interval).
        summary_writes = body.count("(Join-Path $Pub 'summary.json')")
        display_wake_fields = body.count("displayWake=$displayWake") + body.count("displayWake = $displayWake")
        self.assertEqual(27, summary_writes, "a summary.json write site was added/removed after the wake")
        # +1: the success path also stamps displayWake into evidence-manifest.json, a second file.
        self.assertEqual(summary_writes + 1, display_wake_fields)

    def test_claim_time_wake_and_keep_alive_run_before_footage_package_and_quiescence(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-2 round 1c: the wake must be the job's very first action after
        # claim -- ahead of footage resolution, package/build-manifest verification, and the
        # CPU-quiescence sleeps -- closing round 1b's 4.5-minute gap. CUDA-PERF-DISPLAY-WAKE-3
        # round 1 (sol BLOCKER fix): the secure/unknown gate now runs BEFORE the keep-alive starts
        # (previously the keep-alive started first), so the keep-alive is armed only once the gate
        # has already passed.
        embedded_end_at = self.text.index("# --- end embedded verifiers")
        wake_at = self.text.index("$displayWake = Start-AttrCudaDisplayWake")
        secure_exit_at = self.text.index(
            "if ($displayWake.screensaverSecureOwnerOnly) {", wake_at)
        keep_alive_at = self.text.index(
            "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive", secure_exit_at)
        package_verification_at = self.text.index("foreach ($item in @(", keep_alive_at)
        footage_marker_at = self.text.index(
            "ATTR3-FIXTURE-STAGE-1: a fixture run authenticates the cached clip's CONTENT",
            package_verification_at)
        # UM-DISPLAY-SELECT-AND-LOG-1 round 2b: the gate's sample accumulators moved from
        # `$loads = @()` (LoadPercentage) to `$cpuUtilitySamples = @()` (utility + busy-time pair).
        quiescence_at = self.text.index("$cpuUtilitySamples = @()", footage_marker_at)
        self.assertGreater(wake_at, embedded_end_at,
                            "the wake must run only after its own functions are embedded")
        self.assertGreater(secure_exit_at, wake_at)
        self.assertGreater(keep_alive_at, secure_exit_at,
                            "the keep-alive must start only after the secure/unknown gate has passed")
        self.assertLess(keep_alive_at, package_verification_at,
                         "the keep-alive start must still run before package verification")
        self.assertLess(package_verification_at, footage_marker_at,
                         "package verification must still run before footage content is touched")
        self.assertLess(footage_marker_at, quiescence_at,
                         "footage resolution must still run before the CPU-quiescence check")

    def test_screensaver_secure_owner_only_publishes_a_typed_result_and_exits_25(self) -> None:
        secure_exit_at = self.text.index("if ($displayWake.screensaverSecureOwnerOnly) {")
        block_end_at = self.text.index(
            "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive", secure_exit_at)
        block = self.text[secure_exit_at:block_end_at]
        self.assertIn("result='SCREENSAVER_SECURE_OWNER_ONLY'", block)
        self.assertIn("displayWake=$displayWake", block)
        self.assertIn("exit 25", block)

    def test_dismiss_failed_publishes_a_typed_result_and_exits_27_before_the_keep_alive_starts(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence, defect class fix): a screen saver
        # this job could not confirm it actually dismissed must stop the leg here, typed, before the
        # periodic keep-alive is even armed -- mirrors the secure-owner-only gate immediately above
        # it in the generator.
        secure_exit_at = self.text.index("if ($displayWake.screensaverSecureOwnerOnly) {")
        dismiss_exit_at = self.text.index("if ($displayWake.dismissFailed) {", secure_exit_at)
        keep_alive_at = self.text.index(
            "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive", dismiss_exit_at)
        self.assertGreater(dismiss_exit_at, secure_exit_at,
                            "the dismiss-failed gate must run after the secure/unknown gate")
        block = self.text[dismiss_exit_at:keep_alive_at]
        self.assertIn("result='DISPLAY_WAKE_DISMISS_FAILED'", block)
        self.assertIn("displayWake=$displayWake", block)
        self.assertIn("exit 27", block)

    def test_keep_alive_health_checkpoints_bracket_the_presentmon_spawn_and_stop_the_leg_typed(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 1: two checkpoints, in this order -- the first right
        # before Start-PresentMonCapture (the start of the measured interval), the second right
        # before the smoke launch, bracketing the gap where PresentMon has already spawned but
        # MLVApp has not yet been told to play. Round 1b adds a third, right after
        # Wait-PresentMonCapture is known to have succeeded -- the measured interval itself is
        # unambiguously over by then, and a failure discovered only here must still stop the leg
        # typed rather than reach the eligibility/backend gate below it.
        wake_at = self.text.index("$displayWake = Start-AttrCudaDisplayWake")
        measurement_checkpoint_at = self.text.index(
            "$keepAliveHealthAtMeasurementStart = Get-AttrCudaDisplayWakeKeepAliveHealth", wake_at)
        present_mon_capture_at = self.text.index(
            "$presentMonProc = Start-PresentMonCapture $presentMonPath", measurement_checkpoint_at)
        smoke_launch_checkpoint_at = self.text.index(
            "$keepAliveHealthBeforeSmokeLaunch = Get-AttrCudaDisplayWakeKeepAliveHealth",
            present_mon_capture_at)
        launch_at = self.text.index(
            '& "$env:ProgramFiles\\PowerShell\\7\\pwsh.exe" -NoLogo -NoProfile -NonInteractive '
            '-ExecutionPolicy Bypass -Command $cmd', smoke_launch_checkpoint_at)
        after_measured_checkpoint_at = self.text.index(
            "$keepAliveHealthAfterMeasuredInterval = Get-AttrCudaDisplayWakeKeepAliveHealth",
            launch_at)
        backend_gate_at = self.text.index(
            "$verdict = Get-AttrCudaEligibilityVerdict", after_measured_checkpoint_at)
        self.assertGreater(measurement_checkpoint_at, wake_at)
        self.assertGreater(present_mon_capture_at, measurement_checkpoint_at,
                            "PresentMon must start only after the first keep-alive health check")
        self.assertGreater(smoke_launch_checkpoint_at, present_mon_capture_at)
        self.assertGreater(launch_at, smoke_launch_checkpoint_at,
                            "MLVApp must launch only after the second keep-alive health check")
        self.assertGreater(after_measured_checkpoint_at, launch_at,
                            "the third checkpoint must read only after the smoke launch")
        self.assertGreater(backend_gate_at, after_measured_checkpoint_at,
                            "the backend-eligibility gate must run only after the third keep-alive health check")

        measurement_block = self.text[
            measurement_checkpoint_at:present_mon_capture_at]
        self.assertIn("result='KEEPALIVE_FAILED'", measurement_block)
        self.assertIn("keepAliveCheckpoint='start_of_measured_interval'", measurement_block)
        self.assertIn("displayWake=$displayWake", measurement_block)
        self.assertIn("exit 26", measurement_block)
        # CUDA-PERF-DISPLAY-WAKE-3 round 1b: only the FIRST checkpoint requires a success so far
        # -- see Get-AttrCudaDisplayWakeKeepAliveHealth's own .PARAMETER doc for why only here.
        self.assertIn("-RequireSuccessSoFar", measurement_block)

        smoke_launch_block = self.text[smoke_launch_checkpoint_at:launch_at]
        self.assertIn("result='KEEPALIVE_FAILED'", smoke_launch_block)
        self.assertIn("keepAliveCheckpoint='before_smoke_launch'", smoke_launch_block)
        self.assertIn("displayWake=$displayWake", smoke_launch_block)
        self.assertIn("exit 26", smoke_launch_block)
        self.assertNotIn("-RequireSuccessSoFar", smoke_launch_block)
        # PresentMon is already running by this second checkpoint: a refusal here must not orphan
        # it, the same way SMOKE_RUN_FAILED further down never does.
        self.assertIn("Stop-PresentMonCapture -Proc $presentMonProc", smoke_launch_block)

        after_measured_block = self.text[after_measured_checkpoint_at:backend_gate_at]
        self.assertIn("result='KEEPALIVE_FAILED'", after_measured_block)
        self.assertIn("keepAliveCheckpoint='after_measured_interval'", after_measured_block)
        self.assertIn("displayWake=$displayWake", after_measured_block)
        self.assertIn("exit 26", after_measured_block)
        self.assertNotIn("-RequireSuccessSoFar", after_measured_block)

    def test_display_asleep_outcome_specifically_carries_the_wake_evidence(self) -> None:
        status_at = self.text.index("schema='playback-attr-3-cuda-venue.v1'; result=$displayReport.status")
        display_asleep_exit_at = self.text.index(
            "$displayExitCode = if ($displayReport.status -eq 'DISPLAY_ASLEEP') { 24 } else { 23 }")
        block = self.text[status_at:display_asleep_exit_at]
        self.assertIn("displayWake=$displayWake", block)


# --------------------------------------------------------------------------------------------
# CUDA-PERF-DISPLAY-WAKE-2: the periodic keep-alive, on top of WAKE-1's one-time nudge (sol's
# BLOCKER on PR #167 -- SetThreadExecutionState alone does not stop the screen saver, and a short
# timeout can re-engage during the leg's long sleeps/playback after the single initial nudge).
# --------------------------------------------------------------------------------------------


@requires_pwsh
class DisplayWakeKeepAliveFunctionTests(_PwshCase):
    """Start-/Stop-AttrCudaDisplayWakeKeepAlive: shape, never-throws, and it actually nudges."""

    def test_keep_alive_starts_nudges_and_stops_without_throwing(self) -> None:
        # A 1-second interval so the test doesn't wait 15-20s; the contract
        # (ValidateRange(1,20)) permits it, and the deployed job's own default (15s) is a
        # separate, unexercised-here parameter choice.
        proc = self.run_with_module(
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "Start-Sleep -Milliseconds 2500\n"
            "$midCount = [int]$h.nudgeState.count\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'mid.txt')}', \"$midCount\")\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        mid_count = int((self.tmp / "mid.txt").read_text(encoding="utf-8"))
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        # >=2 ticks are expected in 2.5s at a 1s interval; >=1 tolerates a slow CI host without
        # letting a keep-alive that never nudges at all pass silently.
        self.assertGreaterEqual(mid_count, 1, "the keep-alive should have nudged at least once")
        self.assertIs(result["stopped"], True)
        self.assertIsNone(result["error"])
        self.assertGreaterEqual(result["nudgeCount"], mid_count)
        # CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol BLOCKER fix): every recorded attempt is either a
        # success or a failure -- on a real host every tick here is expected to succeed, so
        # failureCount stays 0 and successCount tracks nudgeCount exactly.
        self.assertIsNone(result["setupError"])
        self.assertEqual(result["failureCount"], 0)
        self.assertEqual(result["successCount"], result["nudgeCount"])
        self.assertIsNone(result["lastError"])
        self.assertIsNone(result["lastFailureUtc"])

    def test_stop_keep_alive_tolerates_a_null_handle(self) -> None:
        # A job's `finally` calls Stop-AttrCudaDisplayWakeKeepAlive even when the try above threw
        # before Start- was ever reached -- must not throw on an uninitialized/$null handle.
        proc = self.run_with_module(
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $null\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["stopped"], True)
        self.assertIsNone(result["error"])

    def test_keep_alive_does_not_throw_when_native_type_is_unavailable(self) -> None:
        # Same simulated-failure shape as DisplayWakeFunctionTests.
        # test_a_native_load_failure_is_recorded_not_thrown. CUDA-PERF-DISPLAY-WAKE-3 round 1b
        # (sol BLOCKER): a native load failure is no longer a healthy-looking zero-attempt loop --
        # Start-AttrCudaDisplayWakeKeepAlive now checks Register-AttrCudaDisplayWakeNativeMethods'
        # own Boolean result itself and returns a handle with .setupError set and no background
        # pipeline at all, rather than starting a Runspace whose loop body could never do
        # anything. Stop-AttrCudaDisplayWakeKeepAlive must still tolerate that shape without
        # throwing (it already tolerates a CreateRunspace/Open/BeginInvoke .setupError the same
        # way).
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods',"
            "'Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWakeKeepAlive','Stop-AttrCudaDisplayWakeKeepAlive')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "function Register-AttrCudaDisplayWakeNativeMethods { $false }\n"
                "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
                "$noPipeline = ($null -eq $h.runspace) -and ($null -eq $h.powershell) -and ($null -eq $h.asyncResult)\n"
                "Start-Sleep -Milliseconds 1500\n"
                "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'nopipeline.txt')}', \"$noPipeline\")\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertEqual("True", (self.tmp / "nopipeline.txt").read_text(encoding="utf-8"))
        self.assertIs(result["stopped"], True)
        self.assertIsNone(result["error"])
        # No Runspace was ever started for a loop that could never do anything -- zero nudges,
        # not a crash.
        self.assertEqual(0, result["nudgeCount"])
        self.assertEqual(0, result["successCount"])
        self.assertEqual(0, result["failureCount"])
        self.assertIn("ATTRCUDA_KEEPALIVE_NATIVE_UNAVAILABLE", result["setupError"])


class DisplayWakeKeepAlivePerTickDesktopSwitchTests(unittest.TestCase):
    """CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence, defect class fix): each keep-alive tick
    must follow the input desktop the same way the one-time dismiss does -- a plain SendInput on
    the Runspace's own (Default) desktop is exactly what both live Ultra-Magnus legs showed failing
    on every tick once a screen saver had taken the input desktop. Static text-scan, since a live
    screen saver cannot be reproduced in this test environment -- see the round-2 brief."""

    def setUp(self) -> None:
        self.text = MODULE.read_text(encoding="utf-8")
        loop_start_at = self.text.index("$loopScript = {")
        loop_end_at = self.text.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke",
            loop_start_at)
        self.loop_body = self.text[loop_start_at:loop_end_at]

    def test_each_tick_calls_the_dedicated_thread_input_desktop_nudge(self) -> None:
        self.assertIn("[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run(", self.loop_body)

    def test_the_loop_body_never_calls_sendinput_directly(self) -> None:
        # A mutation reverting to the round-1 plain SendInput call (on the Runspace's own thread,
        # which never follows the input desktop) must red this test even if it happens to leave
        # the InputDesktopNudge.Run call in place too.
        self.assertNotIn("NativeMethods]::SendInput(", self.loop_body)

    def test_start_keep_alive_passes_a_join_timeout_into_the_loop(self) -> None:
        self.assertIn("AddArgument($NudgeJoinTimeoutMilliseconds)", self.text)
        # CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol hardening): $ProbeTimeoutMilliseconds joins the
        # loop's own parameter list -- the invariant under test here (the join timeout is threaded
        # through) is unchanged.
        self.assertIn(
            "param($StopEvent, $IntervalSeconds, $NudgeState, $JoinTimeoutMilliseconds, $ProbeTimeoutMilliseconds)",
            self.loop_body)
        self.assertIn("InputDesktopNudge]::Run([int]$JoinTimeoutMilliseconds)", self.loop_body)

    def test_close_desktop_error_is_a_tick_failure_candidate(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-4 round 1 (sol hardening): a leaked desktop handle every tick used
        # to be invisible to keep-alive health because only Open/SetThreadDesktop/SendInput were
        # ever selected into $tickError. Live CloseDesktop failure is not reproducible on a CI host
        # (same reasoning as the round-2 desktop-switch tests above), so this pins the SHAPE.
        self.assertIn(
            "@($result.OpenInputDesktopError, $result.SetThreadDesktopError, $result.SendInputError, "
            "$result.CloseDesktopError)",
            self.loop_body)

    def test_the_secure_gate_check_runs_before_the_dedicated_thread_nudge(self) -> None:
        # BLOCKER fix ordering: the mutually-exclusive if/elseif means InputDesktopNudge.Run can
        # never execute on the same tick the owner-only gate trips.
        gate_at = self.loop_body.index("$tickSecureOwnerOnly = if")
        if_at = self.loop_body.index("if ($tickSecureOwnerOnly) {", gate_at)
        elseif_at = self.loop_body.index(
            'elseif ("MLVAppAttrCudaDisplayWake.InputDesktopNudge" -as [type]) {', if_at)
        nudge_at = self.loop_body.index("[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run(", elseif_at)
        self.assertLess(gate_at, if_at)
        self.assertLess(if_at, elseif_at)
        self.assertLess(elseif_at, nudge_at)


@requires_pwsh
class DisplayWakeKeepAlivePerTickScreensaverGateTests(unittest.TestCase):
    """CUDA-PERF-DISPLAY-WAKE-4 round 1: the keep-alive loop re-reads the running/secure probes
    before EVERY tick -- not just once at claim time -- with the same fail-closed rule
    Start-AttrCudaDisplayWake's own claim-time gate uses, and only counts a tick as success once the
    screen saver is CONFIRMED not running afterward.

    The loop runs in a brand-new Runspace (RunspaceFactory.CreateRunspace) that inherits none of
    this scope's PowerShell functions, only .NET types loaded via Add-Type (AppDomain-wide) --
    Get-AttrCudaScreensaverRunning/-Secure are copied into it (Start-AttrCudaDisplayWakeKeepAlive's
    own SessionStateFunctionEntry setup) from THEIR CURRENT DEFINITION in the calling scope, late-
    bound by name. Overriding them therefore requires the SAME flat-script embedding technique
    DisplayWakeKeepAliveHealthTests.test_native_wake_surface_unavailable_is_unhealthy_not_a_healthy_zero_attempt_loop
    already uses (Get-AttrCudaEmbeddedFunctionSource into one scope with no module boundary,
    override appended after) -- Import-Module's own module-scope isolation would let the override
    shadow nothing the module's own internal calls actually resolve to."""

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 keep-alive is Windows-only (P/Invoke, drive-letter paths)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-keepalive-gate-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _run_with_probe_overrides(self, *, running_script: str, secure_script: str,
                                   interval_seconds: int = 1, sleep_ms: int = 1500) -> dict:
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverSecure','Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWakeKeepAlive',"
            "'Stop-AttrCudaDisplayWakeKeepAlive')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                # Overrides defined AFTER the extracted originals, in this SAME flat scope, replace
                # them outright (last-definition-wins, no module boundary) -- the exact mechanism
                # this module's own _wake_with_overrides test helper already relies on.
                f"function Get-AttrCudaScreensaverRunning {{ {running_script} }}\n"
                f"function Get-AttrCudaScreensaverSecure {{ {secure_script} }}\n"
                # VENUE-SESSION-LOCKED-REFUSAL-1: pinned unlocked (the per-tick lock read has its
                # own tests, SessionLockedKeepAliveTests).
                "function Get-AttrCudaSessionLocked { $false }\n"
                f"$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds {interval_seconds}\n"
                f"Start-Sleep -Milliseconds {sleep_ms}\n"
                "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))

    def test_secure_screensaver_mid_leg_blocks_injection_and_is_typed(self) -> None:
        result = self._run_with_probe_overrides(running_script="$true", secure_script="$true")
        self.assertGreaterEqual(result["nudgeCount"], 1)
        self.assertEqual(result["successCount"], 0)
        self.assertGreater(result["failureCount"], 0)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED", result["lastError"])
        self.assertIn("reason=secure_screensaver_mid_leg", result["lastError"])

    def test_unknown_running_state_mid_leg_blocks_injection_and_is_typed(self) -> None:
        result = self._run_with_probe_overrides(running_script="$null", secure_script="$false")
        self.assertGreaterEqual(result["nudgeCount"], 1)
        self.assertEqual(result["successCount"], 0)
        self.assertGreater(result["failureCount"], 0)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED", result["lastError"])
        self.assertIn("reason=state_unknown", result["lastError"])

    def test_confirmed_not_running_ticks_succeed(self) -> None:
        result = self._run_with_probe_overrides(running_script="$false", secure_script="$false")
        self.assertGreaterEqual(result["nudgeCount"], 1)
        self.assertEqual(result["failureCount"], 0)
        self.assertEqual(result["successCount"], result["nudgeCount"])
        self.assertIsNone(result["lastError"])

    def test_still_running_after_a_structurally_clean_nudge_is_a_counted_failure(self) -> None:
        # running=$true with secure=$false clears the owner-only gate (not secure, so a dismiss IS
        # attempted) and reaches the real InputDesktopNudge.Run on this host's live desktop -- the
        # same real nudge DisplayWakeKeepAliveFunctionTests.test_keep_alive_starts_nudges_and_stops_without_throwing
        # already proves succeeds cleanly here (Open/SetThreadDesktop/SendInput/CloseDesktop all
        # $null, thread joined). Because Get-AttrCudaScreensaverRunning is stubbed to ALWAYS read
        # running=true, the post-nudge confirmation can never see a $false -- exactly the "SendInput
        # delivered but the screen saver never actually reacted" class fable's r2 hardening targets.
        # sleep_ms=4500 gives margin over the worst case per tick (1s interval wait + the up-to-2s
        # bounded post-nudge poll, which always maxes out here since running never reads $false) so
        # the first tick's failureCount write is not still in flight when Stop- reads it.
        result = self._run_with_probe_overrides(
            running_script="$true", secure_script="$false", sleep_ms=4500)
        self.assertGreaterEqual(result["nudgeCount"], 1)
        self.assertEqual(result["successCount"], 0)
        self.assertGreater(result["failureCount"], 0)
        self.assertIn("ATTRCUDA_KEEPALIVE_STILL_RUNNING", result["lastError"])


# --------------------------------------------------------------------------------------------
# CUDA-PERF-DISPLAY-WAKE-3 round 1: Get-AttrCudaDisplayWakeKeepAliveHealth, the non-throwing
# health read checked at the job's two checkpoints (before the smoke launch, and again at the
# start of the measured interval). Exercised against both a real handle from
# Start-AttrCudaDisplayWakeKeepAlive and fabricated stand-in handles (a plain PSCustomObject
# duck-types the same shape without a real background pipeline), so every one of .healthy's
# false-making conditions is independently provable.
# --------------------------------------------------------------------------------------------


@requires_pwsh
class DisplayWakeKeepAliveHealthTests(_PwshCase):
    """Get-AttrCudaDisplayWakeKeepAliveHealth: never-throws and every unhealthy reason."""

    def test_missing_handle_is_unhealthy(self) -> None:
        proc = self.run_with_module(
            "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $null\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_MISSING", result["reason"])

    def test_a_real_freshly_started_keep_alive_is_healthy(self) -> None:
        proc = self.run_with_module(
            "$k = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
            "[void](Stop-AttrCudaDisplayWakeKeepAlive -Handle $k)\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], True)
        self.assertIsNone(result["reason"])
        self.assertEqual(result["failureCount"], 0)
        self.assertIsNone(result["setupError"])
        self.assertIs(result["runspaceStopped"], False)

    def test_a_recorded_setup_error_is_unhealthy(self) -> None:
        # Fabricated handle: a real setup failure (CreateRunspace/Open/BeginInvoke throwing) is
        # not reliably reproducible on a live host, so the health check's own reading of
        # .setupError is proven directly against a stand-in of the exact shape
        # Start-AttrCudaDisplayWakeKeepAlive's own catch branch returns.
        proc = self.run_with_module(
            "$k = [pscustomobject]@{ setupError = 'boom'; nudgeState = $null; "
            "asyncResult = $null; stopEvent = $null }\n"
            "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_SETUP_FAILED", result["reason"])
        self.assertIn("boom", result["reason"])

    def test_native_wake_surface_unavailable_is_unhealthy_not_a_healthy_zero_attempt_loop(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 1b (sol BLOCKER): a REAL Start-AttrCudaDisplayWakeKeepAlive
        # handle, with Register-AttrCudaDisplayWakeNativeMethods redefined to fail -- proves the
        # end-to-end path, not just the health function's own reading of a fabricated .setupError.
        # Before this fix, .setupError stayed $null (only CreateRunspace/Open/BeginInvoke ever set
        # it) and the loop's own ticks were silent no-ops, so this reported healthy with zero
        # attempts and zero failures -- a keep-alive that could never do anything, waved through.
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWakeKeepAlive',"
            "'Get-AttrCudaDisplayWakeKeepAliveHealth','Stop-AttrCudaDisplayWakeKeepAlive')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "function Register-AttrCudaDisplayWakeNativeMethods { $false }\n"
                "$k = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
                "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
                "[void](Stop-AttrCudaDisplayWakeKeepAlive -Handle $k)\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_SETUP_FAILED", result["reason"])
        self.assertIn("ATTRCUDA_KEEPALIVE_NATIVE_UNAVAILABLE", result["reason"])
        self.assertIsNotNone(result["setupError"])
        self.assertEqual(result["failureCount"], 0)
        self.assertEqual(result["successCount"], 0)

    def test_zero_successes_so_far_is_unhealthy_only_when_required(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-3 round 1b: -RequireSuccessSoFar is the job's FIRST-checkpoint-
        # only requirement -- a genuinely healthy loop (no setupError, no failure, no dead
        # pipeline) that simply has not ticked yet is unhealthy WITH the switch, healthy WITHOUT
        # it. A fabricated handle isolates "zero so far" from "will it ever succeed" -- proven
        # separately by the real end-to-end tests above/below.
        proc = self.run_with_module(
            "$k = [pscustomobject]@{ setupError = $null; asyncResult = $null; stopEvent = $null; "
            "nudgeState = [pscustomobject]@{ failureCount = 0; successCount = 0; lastError = $null; "
            "lastFailureUtc = $null } }\n"
            "$withoutRequire = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
            "$withRequire = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k -RequireSuccessSoFar\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'without.json')}', ($withoutRequire | ConvertTo-Json -Depth 5))\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'with.json')}', ($withRequire | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        without_require = json.loads((self.tmp / "without.json").read_text(encoding="utf-8"))
        with_require = json.loads((self.tmp / "with.json").read_text(encoding="utf-8"))
        self.assertIs(without_require["healthy"], True)
        self.assertIs(with_require["healthy"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_NO_SUCCESSFUL_NUDGE_YET", with_require["reason"])

    def test_a_recorded_nudge_failure_is_unhealthy(self) -> None:
        proc = self.run_with_module(
            "$k = [pscustomobject]@{ setupError = $null; asyncResult = $null; stopEvent = $null; "
            "nudgeState = [pscustomobject]@{ failureCount = 3; lastError = 'ACCESS_DENIED'; "
            "lastFailureUtc = '2026-01-01T00:00:00Z' } }\n"
            "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_NUDGE_FAILED", result["reason"])
        self.assertEqual(result["failureCount"], 3)
        self.assertEqual(result["lastError"], "ACCESS_DENIED")

    def test_a_pipeline_that_completed_without_a_stop_request_is_unhealthy(self) -> None:
        # Duck-typed stand-ins for .asyncResult.IsCompleted and .stopEvent.IsSet -- the health
        # check only ever reads those two properties, never the real types, so a PSCustomObject of
        # the same shape proves the branch without needing to actually kill a background runspace.
        proc = self.run_with_module(
            "$k = [pscustomobject]@{ setupError = $null; "
            "nudgeState = [pscustomobject]@{ failureCount = 0; lastError = $null; lastFailureUtc = $null }; "
            "asyncResult = [pscustomobject]@{ IsCompleted = $true }; "
            "stopEvent = [pscustomobject]@{ IsSet = $false } }\n"
            "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_RUNSPACE_STOPPED", result["reason"])
        self.assertIs(result["runspaceStopped"], True)

    def test_a_stopped_pipeline_after_an_explicit_stop_request_is_not_flagged(self) -> None:
        # Same IsCompleted=$true shape as above, but stopEvent.IsSet=$true -- the caller DID ask
        # it to stop, so a completed pipeline here is the expected, healthy outcome.
        proc = self.run_with_module(
            "$k = [pscustomobject]@{ setupError = $null; "
            "nudgeState = [pscustomobject]@{ failureCount = 0; lastError = $null; lastFailureUtc = $null }; "
            "asyncResult = [pscustomobject]@{ IsCompleted = $true }; "
            "stopEvent = [pscustomobject]@{ IsSet = $true } }\n"
            "$h = Get-AttrCudaDisplayWakeKeepAliveHealth -Handle $k\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'h.json')}', ($h | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "h.json").read_text(encoding="utf-8"))
        self.assertIs(result["healthy"], True)
        self.assertIs(result["runspaceStopped"], False)


@requires_pwsh
class KeepAliveAfterMeasuredIntervalCheckpointTests(_PwshCase):
    """CUDA-PERF-DISPLAY-WAKE-3 round 1b: executes the REAL third-checkpoint source text (lifted
    verbatim out of the generator template, same technique as SmokeRunFailedPresentMonCleanupTests'
    own _failure_block) against a fabricated $displayWakeKeepAlive -- proving the checkpoint itself
    stops the leg typed on a nudge failure recorded during the measured interval, and does NOT
    fire on a healthy one, using the module's real Get-AttrCudaDisplayWakeKeepAliveHealth (Import-
    Module'd, not embedded -- this test does not need the flat-scope embedding technique the
    display-wake mocking tests above use, since nothing here needs to be redefined out from under
    it)."""

    def _extract_checkpoint(self) -> str:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index(
            "$keepAliveHealthAfterMeasuredInterval = Get-AttrCudaDisplayWakeKeepAliveHealth")
        end = text.index("$verdict = Get-AttrCudaEligibilityVerdict", start)
        return text[start:end]

    def _run(self, *, failure_count: int) -> subprocess.CompletedProcess:
        pub = self.tmp / "pub"
        pub.mkdir()
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$Pub = '{pub}'\n"
            f"$presentMonPath = '{(self.tmp / 'presentmon.csv')}'\n"
            "$FixtureRehearsal = $true\n"
            "$SourceCommit = ('1' * 40)\n"
            "$ClipId = 'tiny_dual_iso'\n"
            "$displayWake = [ordered]@{}\n"
            f"$displayWakeKeepAlive = [ordered]@{{ setupError = $null; asyncResult = $null; "
            f"stopEvent = $null; nudgeState = [ordered]@{{ failureCount = {failure_count}; "
            "successCount = 5; lastError = 'SIMULATED_TICK_FAILURE'; "
            "lastFailureUtc = '2026-01-01T00:00:00Z' } }\n"
            "function Save-Json($Object, [string]$Path) {\n"
            "    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))\n"
            "}\n"
            "function Write-JobTrace([string]$Message) { }\n"
            + self._extract_checkpoint() + "\n"
            "Write-Output 'RESULT=FELL_THROUGH_TO_BACKEND_GATE'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_a_failed_tick_during_the_measured_interval_ends_the_leg_never_a_clean_measurement(self) -> None:
        proc = self._run(failure_count=1)
        self.assertEqual(proc.returncode, 26, proc.stdout + proc.stderr)
        self.assertIn("RESULT=KEEPALIVE_FAILED", proc.stdout)
        self.assertIn("CHECKPOINT=after_measured_interval", proc.stdout)
        self.assertNotIn("RESULT=FELL_THROUGH_TO_BACKEND_GATE", proc.stdout)
        summary = json.loads((self.tmp / "pub" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "KEEPALIVE_FAILED")
        self.assertEqual(summary["keepAliveCheckpoint"], "after_measured_interval")
        # Never anything that could be mistaken for a clean measurement.
        self.assertNotEqual(summary["result"], "MEASUREMENT_CAPTURED")
        self.assertNotEqual(summary["result"], "FIXTURE_REHEARSAL_CAPTURED")

    def test_a_healthy_keep_alive_at_this_checkpoint_falls_through_to_the_backend_gate(self) -> None:
        proc = self._run(failure_count=0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT=FELL_THROUGH_TO_BACKEND_GATE", proc.stdout)
        self.assertNotIn("KEEPALIVE_FAILED", proc.stdout)


class DisplayWakeKeepAliveSetupHardeningTests(unittest.TestCase):
    """Static shape check: CreateRunspace/Open/BeginInvoke wrapped in the non-throwing contract.

    A live repro of RunspaceFactory.CreateRunspace()/Open() actually throwing is not reliably
    reproducible on a CI host (it would need real resource exhaustion), so this pins the SHAPE
    instead -- deleting the try/catch or moving $setupError's assignment out of it fails this
    test, the same static-shape technique DisplayWakeJobOrderingTests already uses for
    hard-to-execute invariants."""

    def setUp(self) -> None:
        self.text = MODULE.read_text(encoding="utf-8")

    def test_runspace_setup_is_wrapped_in_a_try_catch_that_records_setup_error(self) -> None:
        func_at = self.text.index("function Start-AttrCudaDisplayWakeKeepAlive {")
        func_end_at = self.text.index("\nfunction Get-AttrCudaDisplayWakeKeepAliveHealth {", func_at)
        body = self.text[func_at:func_end_at]
        try_at = body.index("try {")
        # CUDA-PERF-DISPLAY-WAKE-4 round 1: CreateRunspace now takes an InitialSessionState (so the
        # loop's Runspace can see the per-tick screensaver probe functions) instead of the
        # parameterless overload -- the invariant under test (wrapped in try/catch, .setupError set
        # in the catch) is unchanged.
        create_at = body.index("RunspaceFactory]::CreateRunspace($initialSessionState)", try_at)
        begin_invoke_at = body.index(".BeginInvoke()", create_at)
        catch_at = body.index("} catch {", begin_invoke_at)
        setup_error_at = body.index("$setupError = $_.Exception.Message", catch_at)
        self.assertGreater(create_at, try_at)
        self.assertGreater(begin_invoke_at, create_at)
        self.assertGreater(catch_at, begin_invoke_at)
        self.assertGreater(setup_error_at, catch_at)


class DisplayWakeKeepAliveJobOrderingTests(unittest.TestCase):
    """Static ordering/shape checks on the generator's own template text (no pwsh required)."""

    def setUp(self) -> None:
        self.text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")

    def test_keep_alive_starts_right_after_the_one_time_wake_before_the_smoke_launch(self) -> None:
        wake_at = self.text.index("$displayWake = Start-AttrCudaDisplayWake")
        keep_alive_at = self.text.index(
            "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive", wake_at)
        launch_at = self.text.index(
            '& "$env:ProgramFiles\\PowerShell\\7\\pwsh.exe" -NoLogo -NoProfile -NonInteractive '
            '-ExecutionPolicy Bypass -Command $cmd', keep_alive_at)
        self.assertGreater(keep_alive_at, wake_at)
        self.assertGreater(launch_at, keep_alive_at,
                            "the leg must launch MLVApp only after the keep-alive has started")

    def test_keep_alive_stop_runs_in_finally_before_the_one_time_release(self) -> None:
        finally_at = self.text.index("} finally {")
        keep_alive_stop_at = self.text.index(
            "[void](Stop-AttrCudaDisplayWakeKeepAlive -Handle $displayWakeKeepAlive)", finally_at)
        release_at = self.text.index("[void](Stop-AttrCudaDisplayWake)", keep_alive_stop_at)
        self.assertGreater(keep_alive_stop_at, finally_at)
        self.assertGreater(release_at, keep_alive_stop_at,
                            "the keep-alive must stop before the execution-state release")

    def test_keep_alive_nudge_state_is_folded_into_display_wake_by_reference(self) -> None:
        # $displayWake['keepAliveNudgeState'] = $displayWakeKeepAlive.nudgeState (a reference, not
        # a copy) is what makes every one of the 9+1 existing displayWake=$displayWake write sites
        # automatically carry live keep-alive evidence without editing each site -- see
        # test_display_wake_evidence_is_recorded_on_every_outcome_after_the_wake, whose count is
        # unchanged by this card for exactly that reason.
        keep_alive_at = self.text.index(
            "$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive")
        fold_at = self.text.index(
            "$displayWake['keepAliveNudgeState'] = $displayWakeKeepAlive.nudgeState", keep_alive_at)
        self.assertGreater(fold_at, keep_alive_at)


# --------------------------------------------------------------------------------------------
# CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol BLOCKER fix): a secure saver can become active between
# the per-tick state read and InputDesktopNudge.Run's own SendInput call -- Windows has no atomic
# check-and-inject, so this narrows the window to the injecting thread itself: it reads the
# ATTACHED desktop's own name right before SendInput and refuses to inject into anything but the
# expected non-secure ("Default") desktop.
# --------------------------------------------------------------------------------------------


class InputDesktopNudgeSecureDesktopGateTests(unittest.TestCase):
    """Static shape/ordering checks on InputDesktopNudge.Run's desktop-name gate. A live
    secure-desktop switch is not reproducible in this test environment (same reasoning
    DisplayWakeKeepAlivePerTickDesktopSwitchTests already uses for its own live-UM-only class of
    evidence), so the mechanism itself is proven live below (InputDesktopNudgeDesktopNameLiveTests)
    against the one desktop name this host CAN produce ("Default"), and the refusal shape is pinned
    here, mutation-checked. CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet hardening): round 1b's
    original allow-list of "Default" alone was a HUB-BLOCKER regression -- a read-only hub probe on
    Ultra-Magnus measured a RUNNING, non-secure screen saver's input desktop as named
    "Screen-saver", not "Default", so that allow-list refused every real dismiss (the golden path
    this whole helper exists for). "Screen-saver" now joins the allow-list; see
    InputDesktopNudgeSecureScreensaverGateTests immediately below for the state re-read that
    separates a secure screen saver from a non-secure one on that SAME desktop name, since the name
    alone cannot."""

    def setUp(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start_at = text.index("public static InputDesktopNudgeResult Run(int joinTimeoutMilliseconds)")
        end_at = text.index("'@ -ErrorAction Stop", start_at)
        self.body = text[start_at:end_at]

    def test_desktop_name_is_read_after_setthreaddesktop_and_before_sendinput(self) -> None:
        set_thread_desktop_at = self.body.index("NativeMethods.SetThreadDesktop(hDesktop)")
        read_name_at = self.body.index("GetUserObjectInformation(", set_thread_desktop_at)
        send_input_at = self.body.index("NativeMethods.SendInput(", read_name_at)
        self.assertLess(set_thread_desktop_at, read_name_at)
        self.assertLess(read_name_at, send_input_at)

    def test_default_and_screensaver_desktop_names_are_both_treated_as_safe(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-4 round 1c regression guard: a mutation that narrows this
        # allow-list back to "Default" alone (round 1b's shape, proven live-broken by the hub's own
        # Ultra-Magnus probe) must red this test.
        safe_at = self.body.index("bool desktopNameSafe = desktopNameRead && (")
        refused_at = self.body.index("if (!desktopNameSafe)", safe_at)
        safe_expr = self.body[safe_at:refused_at]
        self.assertIn('string.Equals(desktopName, "Default", StringComparison.Ordinal)', safe_expr)
        self.assertIn('string.Equals(desktopName, "Screen-saver", StringComparison.Ordinal)', safe_expr)
        self.assertIn("||", safe_expr)
        # Exactly two names allow-listed -- guards against silently widening this to "Winlogon" or
        # any other name instead of relying on default-deny.
        self.assertEqual(2, safe_expr.count("string.Equals(desktopName,"))

    def test_an_unreadable_desktop_name_is_refused_with_a_typed_reason(self) -> None:
        self.assertIn("ATTRCUDA_SECURE_DESKTOP_REFUSED reason=desktop_name_unknown", self.body)

    def test_an_unexpected_desktop_name_is_refused_with_a_typed_reason(self) -> None:
        self.assertIn("ATTRCUDA_SECURE_DESKTOP_REFUSED reason=secure_desktop", self.body)

    def test_a_refused_desktop_name_returns_before_the_nudge_input_array_is_built(self) -> None:
        # A mutation that lets the refusal fall through to SendInput anyway (e.g. removing the
        # `return;`) must red this test even if the refusal is still recorded.
        refused_at = self.body.index("result.DesktopNameRefused = true;")
        nudge_array_at = self.body.index("INPUT[] nudge = new INPUT[]", refused_at)
        return_at = self.body.index("return;", refused_at)
        self.assertLess(refused_at, return_at)
        self.assertLess(return_at, nudge_array_at)

    def test_the_refusal_is_written_to_senderror_so_existing_tick_failure_detection_still_catches_it(self) -> None:
        # The keep-alive loop's own tickError candidate list (pinned by
        # DisplayWakeKeepAlivePerTickDesktopSwitchTests.test_close_desktop_error_is_a_tick_failure_candidate)
        # already selects $result.SendInputError -- routing the refusal through a different,
        # unselected field would make it invisible to keep-alive health despite being "recorded".
        self.assertIn("result.SendInputError = desktopNameRead", self.body)


@requires_pwsh
class InputDesktopNudgeDesktopNameLiveTests(_PwshCase):
    """Live proof, against the one desktop name this host can actually produce ("Default"): the
    dedicated thread's new name read does not regress the golden path DisplayWakeKeepAliveFunctionTests
    and DisplayWakeKeepAlivePerTickScreensaverGateTests already exercise, and both the C# result and
    the Invoke-AttrCudaInputDesktopNudge wrapper surface the new fields."""

    def test_the_dedicated_thread_reports_the_default_desktop_name_and_is_not_refused(self) -> None:
        proc = self.run_with_module(
            "[void](Register-AttrCudaDisplayWakeNativeMethods)\n"
            "$result = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run(5000)\n"
            "$out = [ordered]@{ desktopName = $result.DesktopName; desktopNameRefused = [bool]$result.DesktopNameRefused; "
            "sendInputError = $result.SendInputError; threadJoined = [bool]$result.ThreadJoined; "
            "screensaverRunningAtInject = $result.ScreensaverRunningAtInject; "
            "screensaverSecureAtInject = $result.ScreensaverSecureAtInject; secureRefused = [bool]$result.SecureRefused }\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(result["desktopName"], "Default")
        self.assertIs(result["desktopNameRefused"], False)
        self.assertIsNone(result["sendInputError"])
        self.assertIs(result["threadJoined"], True)
        # CUDA-PERF-DISPLAY-WAKE-4 round 1c: on this host's quiet (no screen saver running)
        # desktop, the in-thread secure re-read must confirm running=false and never refuse.
        self.assertIs(result["screensaverRunningAtInject"], False)
        self.assertIs(result["secureRefused"], False)

    def test_invoke_attr_cuda_input_desktop_nudge_surfaces_desktop_name_fields(self) -> None:
        proc = self.run_with_module(
            "$r = Invoke-AttrCudaInputDesktopNudge -JoinTimeoutMilliseconds 5000\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(result["desktopName"], "Default")
        self.assertIs(result["desktopNameRefused"], False)
        self.assertIs(result["screensaverRunningAtInject"], False)
        self.assertIs(result["secureRefused"], False)

    def test_getuserobjectinformation_fails_closed_on_an_invalid_handle(self) -> None:
        # Grounds the "unreadable name" mechanism in a real Win32 call this host CAN exercise, even
        # though forcing InputDesktopNudge.Run's own hDesktop into an unreadable state needs a real
        # secure-desktop switch this environment cannot produce.
        proc = self.run_with_module(
            "[void](Register-AttrCudaDisplayWakeNativeMethods)\n"
            "$len = 0\n"
            "$sb = [System.Text.StringBuilder]::new(256)\n"
            "$ok = [MLVAppAttrCudaDisplayWake.NativeMethods]::GetUserObjectInformation([IntPtr]::Zero, 2, $sb, $sb.Capacity, [ref]$len)\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ ok = [bool]$ok }} | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["ok"], False)


# --------------------------------------------------------------------------------------------
# CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet hardening, hub-blocker correction): round 1b's
# desktop-name-only gate refused every real dismiss once a hub probe on Ultra-Magnus proved a
# RUNNING non-secure screen saver's input desktop is named "Screen-saver", not "Default". The
# name allow-list now admits both -- but a SECURE screen saver can run on that same
# "Screen-saver" name, so the name check alone cannot separate secure from non-secure. This
# section pins the new in-thread SPI_GETSCREENSAVERRUNNING/-SECURE re-read that does.
# --------------------------------------------------------------------------------------------


class InputDesktopNudgeSecureScreensaverGateTests(unittest.TestCase):
    """Static shape/ordering checks on InputDesktopNudge.Run's new in-thread secure-state
    re-read. A live secure screen saver (or a live "Screen-saver" desktop name at all, on this
    non-UM host) is not reproducible in this test environment -- same reasoning
    InputDesktopNudgeSecureDesktopGateTests already uses for its own class of evidence -- so this
    is mutation-checked against the source the same way that class's refusal shape is."""

    def setUp(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start_at = text.index("public static InputDesktopNudgeResult Run(int joinTimeoutMilliseconds)")
        end_at = text.index("'@ -ErrorAction Stop", start_at)
        self.body = text[start_at:end_at]
        self.name_gate_return_at = self.body.index(
            'result.SendInputError = "ATTRCUDA_SECURE_DESKTOP_REFUSED reason=" + secureRefusalReason')
        self.name_gate_return_at = self.body.rindex("return;", 0, self.name_gate_return_at)

    def test_secure_state_is_reread_after_the_desktop_name_gate_passes_and_before_sendinput(self) -> None:
        # The desktop-name gate's own `return;` (on refusal) must precede the secure re-read, and
        # the secure re-read must precede SendInput -- a mutation that reorders these (e.g. reading
        # secure state before the name is even known safe) must red this test.
        name_return_at = self.body.index("result.DesktopNameRefused = true;")
        name_return_at = self.body.index("return;", name_return_at)
        running_read_at = self.body.index(
            'NativeMethods.SystemParametersInfo(0x0072 /* SPI_GETSCREENSAVERRUNNING */', name_return_at)
        secure_read_at = self.body.index(
            'NativeMethods.SystemParametersInfo(0x0076 /* SPI_GETSCREENSAVESECURE */', running_read_at)
        send_input_at = self.body.index("NativeMethods.SendInput(", secure_read_at)
        self.assertLess(name_return_at, running_read_at)
        self.assertLess(running_read_at, secure_read_at)
        self.assertLess(secure_read_at, send_input_at)

    def test_a_running_and_confirmed_secure_screensaver_is_refused_with_a_typed_reason(self) -> None:
        # The reason is built as "...REFUSED reason=" + secureRefusalReason -- the literal prefix
        # and the "secure_screensaver" value (assigned only when runningNow && secureNow) are
        # pinned separately since they are never adjacent text in the source.
        self.assertIn('result.SendInputError = "ATTRCUDA_SECURE_DESKTOP_REFUSED reason=" + secureRefusalReason', self.body)
        self.assertIn('secureNow ? "secure_screensaver" : null', self.body)

    def test_an_unreadable_running_or_secure_state_is_refused_as_state_unknown(self) -> None:
        # Both branches that fail closed on an unreadable probe (running unreadable at all, and
        # running=true with secure unreadable) must assign the SAME "state_unknown" literal.
        self.assertIn('secureRefusalReason = "state_unknown";', self.body)
        self.assertIn('!secureRead ? "state_unknown"', self.body)
        self.assertEqual(2, self.body.count('"state_unknown"'))

    def test_a_running_and_confirmed_not_secure_screensaver_is_not_refused_by_this_gate(self) -> None:
        # The regression this gate must never introduce: a running, NON-secure screen saver (the
        # golden path -- the whole reason this dismiss helper exists) must still reach SendInput.
        # secureRefusalReason stays null exactly when runningNow is true and secureRead/secureNow
        # confirm not-secure -- pinned by the ternary shape itself, since there is no live host in
        # this environment that can force a real secure screen saver to exercise this end to end.
        self.assertIn(
            "secureRefusalReason = !secureRead ? \"state_unknown\" : (secureNow ? \"secure_screensaver\" : null);",
            self.body)

    def test_a_secure_refusal_returns_before_the_nudge_input_array_is_built(self) -> None:
        # A mutation that lets this refusal fall through to SendInput anyway (e.g. removing the
        # `return;`) must red this test even if SecureRefused is still recorded.
        refused_at = self.body.index("result.SecureRefused = true;")
        nudge_array_at = self.body.index("INPUT[] nudge = new INPUT[]", refused_at)
        return_at = self.body.index("return;", refused_at)
        self.assertLess(refused_at, return_at)
        self.assertLess(return_at, nudge_array_at)

    def test_the_secure_refusal_is_written_to_senderror_so_tick_failure_detection_still_catches_it(self) -> None:
        # Same reasoning as the desktop-name gate's own equivalent test: the keep-alive loop's
        # tickError candidate list only ever selects $result.SendInputError, so routing this
        # refusal through a different field would make it invisible to keep-alive health.
        refused_at = self.body.index("result.SecureRefused = true;")
        send_error_at = self.body.index("result.SendInputError = ", refused_at)
        self.assertLess(refused_at, send_error_at)
        self.assertLess(send_error_at, self.body.index("return;", refused_at))

    def test_the_running_and_secure_state_read_are_recorded_on_the_result_regardless_of_outcome(self) -> None:
        # ScreensaverRunningAtInject/ScreensaverSecureAtInject must be assigned BEFORE the refusal
        # branch's early return, so a refused tick still carries the state that refused it (not
        # just $null-by-omission).
        running_assign_at = self.body.index("result.ScreensaverRunningAtInject = runningRead")
        secure_assign_at = self.body.index("result.ScreensaverSecureAtInject = secureRead", running_assign_at)
        refused_at = self.body.index("result.SecureRefused = true;", secure_assign_at)
        self.assertLess(running_assign_at, secure_assign_at)
        self.assertLess(secure_assign_at, refused_at)


class InvokeAttrCudaInputDesktopNudgeSecureFieldWiringTests(unittest.TestCase):
    """Static check: all three return branches of Invoke-AttrCudaInputDesktopNudge (native
    unavailable, success, exception) surface the three new secure-gate fields -- a mutation that
    adds them to only the success branch would silently drop them to $null on the native-
    unavailable/exception paths instead of raising a missing-member error, since this module's
    ordered hashtables are duck-typed."""

    def setUp(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start_at = text.index("function Invoke-AttrCudaInputDesktopNudge {")
        end_at = text.index("\nfunction Start-AttrCudaDisplayWake {", start_at)
        self.body = text[start_at:end_at]

    def test_all_three_branches_declare_the_new_secure_fields(self) -> None:
        for field in ("screensaverRunningAtInject", "screensaverSecureAtInject", "secureRefused"):
            self.assertEqual(
                3, self.body.count(field),
                f"expected '{field}' in all three ordered-hashtable branches, found "
                f"{self.body.count(field)}")


@requires_pwsh
class InvokeAttrCudaInputDesktopNudgeNativeUnavailableSecureFieldsTests(unittest.TestCase):
    """Live proof of the native-unavailable branch specifically -- the success branch's shape is
    already proven by InputDesktopNudgeDesktopNameLiveTests. Same flat-scope override technique
    as test_input_desktop_nudge_does_not_throw_when_native_type_is_unavailable: Import-Module's
    own module-scope isolation means a top-level redefinition of Register-AttrCudaDisplayWakeNativeMethods
    never reaches Invoke-AttrCudaInputDesktopNudge's own internal call to it."""

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 keep-alive is Windows-only (P/Invoke, drive-letter paths)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-nudge-native-unavailable-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_native_unavailable_branch_still_reports_the_secure_fields(self) -> None:
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Invoke-AttrCudaInputDesktopNudge')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "function Register-AttrCudaDisplayWakeNativeMethods { $false }\n"
                "$r = Invoke-AttrCudaInputDesktopNudge -JoinTimeoutMilliseconds 5000\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["attempted"], False)
        self.assertIsNone(result["screensaverRunningAtInject"])
        self.assertIsNone(result["screensaverSecureAtInject"])
        self.assertIs(result["secureRefused"], False)


class DisplayWakeKeepAlivePerTickDesktopNameStateTests(unittest.TestCase):
    """CUDA-PERF-DISPLAY-WAKE-4 round 1c: the keep-alive loop records the desktop name each tick's
    dedicated thread actually read, not just the tick's pass/fail outcome -- static text-scan,
    same reasoning DisplayWakeKeepAlivePerTickDesktopSwitchTests already uses for this loop body
    (a live desktop switch is not reproducible here)."""

    def setUp(self) -> None:
        self.text = MODULE.read_text(encoding="utf-8")
        loop_start_at = self.text.index("$loopScript = {")
        loop_end_at = self.text.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke",
            loop_start_at)
        self.loop_body = self.text[loop_start_at:loop_end_at]

    def test_each_tick_records_the_dedicated_threads_desktop_name(self) -> None:
        run_at = self.loop_body.index("[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run([int]$JoinTimeoutMilliseconds)")
        record_at = self.loop_body.index("$NudgeState.lastDesktopName = $result.DesktopName", run_at)
        self.assertLess(run_at, record_at)

    def test_the_desktop_name_is_recorded_regardless_of_tick_outcome(self) -> None:
        # Recorded before the success/failure branch below it decides count/success/failure --
        # a mutation that moves this recording inside only the success or only the failure arm
        # must red this test.
        record_at = self.loop_body.index("$NudgeState.lastDesktopName = $result.DesktopName")
        tick_error_computed_at = self.loop_body.index(
            "@($result.OpenInputDesktopError, $result.SetThreadDesktopError, $result.SendInputError, "
            "$result.CloseDesktopError)")
        self.assertLess(record_at, tick_error_computed_at)

    def test_nudge_state_initializers_declare_last_desktop_name(self) -> None:
        # Both the early-return (native-unavailable) handle and the real handle's nudgeState must
        # declare the key up front -- Set-StrictMode -Version Latest (this module's own contract)
        # turns a missing-key read into a terminating error rather than $null.
        self.assertEqual(2, self.text.count("lastDesktopName = $null"))


@requires_pwsh
class DisplayWakeKeepAliveLastDesktopNameLiveTests(_PwshCase):
    """Live proof: a real keep-alive tick on this host's quiet ("Default", not running) desktop
    populates .nudgeState.lastDesktopName."""

    def test_a_healthy_tick_records_the_default_desktop_name(self) -> None:
        proc = self.run_with_module(
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "Start-Sleep -Milliseconds 2500\n"
            "$lastDesktopName = $h.nudgeState.lastDesktopName\n"
            "[void](Stop-AttrCudaDisplayWakeKeepAlive -Handle $h)\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ lastDesktopName = $lastDesktopName }} | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(result["lastDesktopName"], "Default")


# --------------------------------------------------------------------------------------------
# CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol hardening): bound a stalled keep-alive probe -- each
# per-tick probe now has a timeout (Invoke-AttrCudaBoundedProbe), and stopping the keep-alive
# (Stop-AttrCudaDisplayWakeKeepAlive) never waits unbounded on EndInvoke either.
# --------------------------------------------------------------------------------------------


@requires_pwsh
class InvokeAttrCudaBoundedProbeTests(_PwshCase):
    """Invoke-AttrCudaBoundedProbe in isolation: a fast probe's own result passes through
    untouched, and a hung probe returns $null within the bound instead of blocking."""

    # CI-FLAKE-BOUNDED-PROBE-3S-1: every case that asserts a probe's RESULT (True/False, or the real
    # production probe resolving its native dependency) passes a generous 30000 ms bound. What those
    # cases prove is result pass-through / dependency resolution, not timing, and a 3000 ms bound
    # lets a loaded hosted runner starve the nested Runspace into the timeout path ($null), failing
    # "None not found in (True, False)" (merge_group run 37367073051). The timeout path itself stays
    # pinned by the hanging-probe case (800 ms) and the unknown-function case (500 ms) below; the
    # production 3000 ms default in AttrCudaArtifacts.psm1 is unchanged.

    def test_a_fast_probe_returns_its_own_result_within_the_timeout(self) -> None:
        proc = self.run_with_module(
            "function Get-AttrCudaFakeFastProbe { return $true }\n"
            "$r = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaFakeFastProbe' -TimeoutMilliseconds 30000\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ r = $r }} | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["r"], True)

    def test_a_probe_that_returns_false_is_not_confused_with_a_timeout(self) -> None:
        # A timeout and a clean $false result must be distinguishable -- both are falsy, but only
        # one is "could not determine" ($null).
        proc = self.run_with_module(
            "function Get-AttrCudaFakeFalseProbe { return $false }\n"
            "$r = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaFakeFalseProbe' -TimeoutMilliseconds 30000\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ r = $r }} | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["r"], False)

    def test_a_hanging_probe_times_out_and_returns_null_within_the_bound(self) -> None:
        proc = self.run_with_module(
            "function Get-AttrCudaFakeHangingProbe { Start-Sleep -Milliseconds 999999; return $true }\n"
            "$sw = [System.Diagnostics.Stopwatch]::StartNew()\n"
            "$r = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaFakeHangingProbe' -TimeoutMilliseconds 800\n"
            "$sw.Stop()\n"
            "$out = [ordered]@{ r = $r; elapsedMs = $sw.ElapsedMilliseconds }\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["r"])
        # Comfortably below the 999999ms the probe itself sleeps for -- the bound, not the probe,
        # decided when this call returned. A generous upper margin tolerates a slow CI host.
        self.assertLess(result["elapsedMs"], 10000)

    def test_an_unknown_function_name_returns_null_not_a_throw(self) -> None:
        proc = self.run_with_module(
            "$r = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaDoesNotExistAtAll' -TimeoutMilliseconds 500\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ r = $r }} | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["r"])

    def test_the_real_production_probe_resolves_its_own_native_dependency(self) -> None:
        # CUDA-PERF-DISPLAY-WAKE-4 round 1d (sol PRE-REVIEW #2 BLOCKER): the fake probes above never
        # exercised Get-AttrCudaScreensaverRunning/-Secure themselves, so they never caught that the
        # nested Runspace above lacks Register-AttrCudaDisplayWakeNativeMethods, which both call
        # internally. Called here as the FIRST screen-saver-related call in this fresh process --
        # nothing has registered [MLVAppAttrCudaDisplayWake.NativeMethods] into this AppDomain yet --
        # the exact ordering sol's repro hit: a direct call elsewhere in the process happening to run
        # first was masking this, since Add-Type is AppDomain-wide and a later bounded-probe call
        # would then find the type already there regardless of its own missing dependency.
        proc = self.run_with_module(
            "$r = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverRunning' -TimeoutMilliseconds 30000\n"
            "$direct = Get-AttrCudaScreensaverRunning\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ r = $r; direct = $direct }} | ConvertTo-Json))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        # A real host always resolves this to a real True/False; only a broken dependency chain
        # produces $null here (the same "could not determine" value a genuine native-load failure
        # returns, so a regression would fail closed rather than throw -- but must not happen on a
        # real host with the dependency actually wired through).
        self.assertIn(result["r"], (True, False))
        self.assertEqual(result["r"], result["direct"])

    def test_the_emitted_jobs_bounded_probe_resolves_its_dependency_too(self) -> None:
        # Same proof as above, but through the SAME embedding mechanism the deployed job actually
        # uses (Get-AttrCudaEmbeddedFunctionSource splicing verbatim function text into one flat
        # script, never Import-Module) -- sol's own repro used exactly this shape. The bounded probe
        # call is still the first screen-saver-related call in this fresh process.
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Invoke-AttrCudaBoundedProbe')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)

        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$r = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverRunning' -TimeoutMilliseconds 30000\n"
                "$direct = Get-AttrCudaScreensaverRunning\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', (@{{ r = $r; direct = $direct }} | ConvertTo-Json))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIn(result["r"], (True, False))
        self.assertEqual(result["r"], result["direct"])


class DisplayWakeKeepAlivePerTickProbeBoundTests(unittest.TestCase):
    """Static shape checks: the keep-alive loop's per-tick probes (and the after-nudge
    confirmation poll) go through Invoke-AttrCudaBoundedProbe, never a direct, unbounded call --
    mutation-checked the same way DisplayWakeKeepAlivePerTickDesktopSwitchTests already pins the
    dedicated-thread nudge call."""

    def setUp(self) -> None:
        self.text = MODULE.read_text(encoding="utf-8")
        loop_start_at = self.text.index("$loopScript = {")
        loop_end_at = self.text.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke",
            loop_start_at)
        self.loop_body = self.text[loop_start_at:loop_end_at]

    def test_top_of_tick_probes_go_through_the_bounded_wrapper(self) -> None:
        self.assertIn(
            "$tickRunning = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverRunning'",
            self.loop_body)
        self.assertIn(
            "$tickSecure = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverSecure'",
            self.loop_body)
        self.assertNotIn("$tickRunning = Get-AttrCudaScreensaverRunning\n", self.loop_body)
        self.assertNotIn("$tickSecure = Get-AttrCudaScreensaverSecure\n", self.loop_body)

    def test_after_nudge_confirmation_poll_also_goes_through_the_bounded_wrapper(self) -> None:
        self.assertIn(
            "$afterRunning = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverRunning'",
            self.loop_body)
        self.assertNotIn("$afterRunning = Get-AttrCudaScreensaverRunning\n", self.loop_body)

    def test_probe_timeout_is_threaded_through_the_loop_parameters(self) -> None:
        self.assertIn(
            "param($StopEvent, $IntervalSeconds, $NudgeState, $JoinTimeoutMilliseconds, $ProbeTimeoutMilliseconds)",
            self.loop_body)
        self.assertIn("AddArgument($ProbeTimeoutMilliseconds)", self.text)

    def test_bounded_probe_is_registered_in_the_loop_runspaces_initial_session_state(self) -> None:
        foreach_at = self.text.index("foreach ($tickProbeFunctionName in @(")
        foreach_end_at = self.text.index(")) {", foreach_at)
        foreach_list = self.text[foreach_at:foreach_end_at]
        self.assertIn("'Invoke-AttrCudaBoundedProbe'", foreach_list)


class StopKeepAliveBoundedWaitTests(unittest.TestCase):
    """Static shape check: Stop-AttrCudaDisplayWakeKeepAlive waits on the async handle directly,
    bounded, before ever calling EndInvoke -- defense in depth alongside the per-tick probe bound
    above, for the exact repro sol's review described (a hung probe leaving EndInvoke to wait
    unbounded)."""

    def setUp(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start_at = text.index("function Stop-AttrCudaDisplayWakeKeepAlive {")
        end_at = text.index("\nfunction Get-AttrCudaAppSwapTelemetry {", start_at)
        self.body = text[start_at:end_at]

    def test_endinvoke_is_gated_behind_a_bounded_async_wait(self) -> None:
        wait_at = self.body.index("AsyncWaitHandle.WaitOne([int]$TimeoutMilliseconds)")
        end_invoke_at = self.body.index("EndInvoke($Handle.asyncResult)", wait_at)
        self.assertLess(wait_at, end_invoke_at)

    def test_a_timeout_is_recorded_as_a_typed_stop_error(self) -> None:
        self.assertIn("ATTRCUDA_KEEPALIVE_STOP_TIMEOUT", self.body)


@requires_pwsh
class KeepAliveHungProbeDoesNotBlockStopTests(unittest.TestCase):
    """End-to-end reproduction of sol's exact hardening repro: Get-AttrCudaScreensaverRunning
    blocks forever starting on the next tick. Before this fix, the tick itself never returned (no
    recorded failure, health stays "healthy" on its prior success) and
    Stop-AttrCudaDisplayWakeKeepAlive then hung in EndInvoke. Same flat-scope override technique as
    DisplayWakeKeepAlivePerTickScreensaverGateTests."""

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 keep-alive is Windows-only (P/Invoke, drive-letter paths)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-keepalive-probe-bound-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _extracted_script(self) -> Path:
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverSecure','Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWakeKeepAlive','Stop-AttrCudaDisplayWakeKeepAlive')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)
        # VENUE-SESSION-LOCKED-REFUSAL-1: pinned unlocked, so each tick reaches the (hung) screen-saver
        # probe these tests are about.
        with (self.tmp / "extracted.ps1").open("a", encoding="utf-8") as f:
            f.write("\nfunction Get-AttrCudaSessionLocked { $false }\n")
        return self.tmp / "extracted.ps1"

    def test_a_hung_running_probe_is_a_counted_failure_and_stop_returns_promptly(self) -> None:
        probe_script = self._extracted_script()
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                "function Get-AttrCudaScreensaverRunning { Start-Sleep -Milliseconds 999999; return $true }\n"
                "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1 -ProbeTimeoutMilliseconds 1000\n"
                # CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1: poll for the failure itself instead of a
                # fixed 3500ms sleep (first tick ~1000ms + bounded probe(s) ~1000ms each + runspace
                # setup, which stretches on a loaded runner). lastError is written AFTER failureCount
                # in the loop, so wait for both. Generous 20s deadline; the assertions below still
                # fail (not hang) if the failure never arrives.
                "$pollDeadline = [DateTime]::UtcNow.AddSeconds(20)\n"
                "while (-not ([int]$h.nudgeState.failureCount -ge 1 -and $h.nudgeState.lastError) "
                "-and [DateTime]::UtcNow -lt $pollDeadline) { Start-Sleep -Milliseconds 50 }\n"
                "$midFailureCount = [int]$h.nudgeState.failureCount\n"
                "$midLastError = $h.nudgeState.lastError\n"
                "$stopSw = [System.Diagnostics.Stopwatch]::StartNew()\n"
                "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h -TimeoutMilliseconds 15000\n"
                "$stopSw.Stop()\n"
                "$out = [ordered]@{ midFailureCount = $midFailureCount; midLastError = $midLastError; "
                "stopElapsedMs = $stopSw.ElapsedMilliseconds; stopStopped = [bool]$r.stopped; stopError = $r.error }\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script, timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        # Before this fix: 0, with no recorded failure at all (the tick never returned).
        self.assertGreaterEqual(result["midFailureCount"], 1)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED", result["midLastError"])
        self.assertIn("reason=state_unknown", result["midLastError"])
        # Before this fix: hangs for the full 999999ms sleep (or forever). A generous upper margin
        # tolerates a slow CI host while still proving this is bounded, not unbounded.
        self.assertLess(result["stopElapsedMs"], 10000)
        self.assertIs(result["stopStopped"], True)
        self.assertIsNone(result["stopError"])

    def test_stop_itself_times_out_typed_when_its_own_bound_is_shorter_than_the_stuck_probe(self) -> None:
        # Proves the Stop-level bound is a REAL, independent guarantee -- not just a side effect of
        # the probe bound happening to be tight enough: a probe bound (5000ms) comfortably longer
        # than Stop's own -TimeoutMilliseconds (300ms). -IntervalSeconds 1 means the loop's FIRST
        # tick does not even start until ~1000ms in (it waits on -StopEvent.Wait(IntervalSeconds*1000)
        # before ever probing) -- so the sleep here is 1200ms, long enough for that first tick to
        # have entered its bounded (and, here, permanently stuck) probe call, short enough that it
        # has nowhere near returned by the time Stop- is called.
        # CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1: the fixed 1200ms sleep (only ~200ms past the first
        # tick's 1000ms StopEvent.Wait) is replaced by a marker the stuck probe stub writes as its
        # first statement, polled with a generous deadline -- Stop- is now called only once the tick
        # is KNOWN to be inside its stuck probe, never before it has entered one.
        probe_script = self._extracted_script()
        marker = self.tmp / "probe-entered.marker"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                f"function Get-AttrCudaScreensaverRunning {{ [IO.File]::WriteAllText('{marker}', '1'); "
                "Start-Sleep -Milliseconds 999999; return $true }\n"
                "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1 -ProbeTimeoutMilliseconds 5000\n"
                "$pollDeadline = [DateTime]::UtcNow.AddSeconds(20)\n"
                f"while (-not (Test-Path -LiteralPath '{marker}') -and [DateTime]::UtcNow -lt $pollDeadline) "
                "{ Start-Sleep -Milliseconds 50 }\n"
                "$stopSw = [System.Diagnostics.Stopwatch]::StartNew()\n"
                "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h -TimeoutMilliseconds 300\n"
                "$stopSw.Stop()\n"
                "$out = [ordered]@{ stopElapsedMs = $stopSw.ElapsedMilliseconds; stopStopped = [bool]$r.stopped; stopError = $r.error }\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script, timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["stopStopped"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_STOP_TIMEOUT", result["stopError"])
        # Bounded by Stop's OWN -TimeoutMilliseconds (300ms), not by the probe's much longer 5000ms
        # bound or the probe's 999999ms sleep. A generous upper margin tolerates a slow CI host.
        self.assertLess(result["stopElapsedMs"], 5000)

    def test_stop_returns_within_its_own_bound_even_when_the_probe_bound_is_far_longer(self) -> None:
        # CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1: pins the product fix. Stop- used to Dispose() the
        # still-running pipeline after its timed-out wait, and Dispose blocks until the stuck tick's
        # probe bound elapses -- so -TimeoutMilliseconds 300 really took ~the probe bound (the sibling
        # test's `< 5000` against a 5000ms probe bound only passed on ~200ms of timing luck). A probe
        # bound of 10000ms makes that failure unmistakable: Stop- must still return in well under it.
        probe_script = self._extracted_script()
        marker = self.tmp / "probe-entered.marker"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n"
                f"function Get-AttrCudaScreensaverRunning {{ [IO.File]::WriteAllText('{marker}', '1'); "
                "Start-Sleep -Milliseconds 999999; return $true }\n"
                "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1 -ProbeTimeoutMilliseconds 10000\n"
                "$pollDeadline = [DateTime]::UtcNow.AddSeconds(20)\n"
                f"while (-not (Test-Path -LiteralPath '{marker}') -and [DateTime]::UtcNow -lt $pollDeadline) "
                "{ Start-Sleep -Milliseconds 50 }\n"
                f"$entered = Test-Path -LiteralPath '{marker}'\n"
                "$stopSw = [System.Diagnostics.Stopwatch]::StartNew()\n"
                "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h -TimeoutMilliseconds 300\n"
                "$stopSw.Stop()\n"
                "$out = [ordered]@{ entered = $entered; stopElapsedMs = $stopSw.ElapsedMilliseconds; "
                "stopStopped = [bool]$r.stopped; stopError = $r.error }\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script, timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))
        self.assertIs(result["entered"], True)
        self.assertIs(result["stopStopped"], False)
        self.assertIn("ATTRCUDA_KEEPALIVE_STOP_TIMEOUT", result["stopError"])
        self.assertLess(result["stopElapsedMs"], 5000)


# --------------------------------------------------------------------------------------------
# VENUE-SESSION-LOCKED-REFUSAL-1: a locked console is a typed, owner-only refusal, detected before
# any input is injected. Bachelor stayed locked after a Windows Update reboot (2026-10-09); a locked
# console's input desktop still reads "Default", so the desktop-name gate let the keep-alive's
# VK_F15 tap through, which raised the sign-in screen.
# --------------------------------------------------------------------------------------------

# A fake MLVAppAttrCudaDisplayWake native surface. Loaded into a FRESH pwsh process before the real
# one ever is, it takes the real type names (Register-AttrCudaDisplayWakeNativeMethods then finds the
# type already loaded and returns $true without Add-Type), so every SendInput and InputDesktopNudge.Run
# the code under test makes is COUNTED here and nothing reaches the desktop -- on the fork base too
# (the RED run), where the lock gate does not exist yet.
_FAKE_WAKE_NATIVE_SURFACE = r"""
Add-Type -TypeDefinition @'
using System;
namespace MLVAppAttrCudaDisplayWake
{
    public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    public struct INPUT { public int type; public MOUSEINPUT mi; public KEYBDINPUT ki; }
    public static class NativeMethods
    {
        public static int SendInputCalls;
        public static string SendInputLog = "";
        public static uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize)
        {
            System.Threading.Interlocked.Increment(ref SendInputCalls);
            foreach (INPUT i in pInputs) { SendInputLog += i.type + ":" + i.mi.dx + "," + i.mi.dy + ";"; }
            return nInputs;
        }
        public static bool SystemParametersInfo(uint uiAction, uint uiParam, ref bool pvParam, uint fWinIni) { return false; }
        public static bool SystemParametersInfoInt(uint uiAction, uint uiParam, ref int pvParam, uint fWinIni) { return false; }
        public static uint SetThreadExecutionState(uint esFlags) { return 1; }
    }
    public class InputDesktopNudgeResult
    {
        public string OpenInputDesktopError; public string SetThreadDesktopError; public string SendInputError; public string CloseDesktopError;
        public bool ThreadJoined; public string DesktopName; public bool DesktopNameRefused;
        public bool? ScreensaverRunningAtInject; public bool? ScreensaverSecureAtInject; public bool SecureRefused;
        public bool? SessionLockedAtInject; public bool SessionLockRefused;
    }
    public static class InputDesktopNudge
    {
        public static int RunCalls;
        // r2: what the real thread's last lock read (just before SendInput) found. 0 = unlocked (the
        // fake "sends"), 1 = locked, 2 = unreadable; 1 and 2 refuse the way the real Run does.
        public static int LockAtInject;
        public static InputDesktopNudgeResult Run(int joinTimeoutMilliseconds)
        {
            System.Threading.Interlocked.Increment(ref RunCalls);
            InputDesktopNudgeResult r = new InputDesktopNudgeResult();
            r.ThreadJoined = true;
            r.DesktopName = "Default";
            if (LockAtInject != 0)
            {
                r.SessionLockRefused = true;
                r.SessionLockedAtInject = LockAtInject == 1 ? (bool?)true : null;
                r.SendInputError = "ATTRCUDA_SESSION_LOCKED_OWNER_ONLY reason=" + (LockAtInject == 1 ? "session_locked" : "session_lock_unknown") + " (fake)";
            }
            return r;
        }
    }
}
'@
"""


@requires_pwsh
class SessionLockedWakeTests(_PwshCase):
    """Start-AttrCudaDisplayWake reads the console lock FIRST: locked or unknown sends no input of any
    kind on either nudge path; unlocked behaves exactly as before plus the added .sessionLocked."""

    # The pre-change field order; the only addition is sessionLocked after method.
    _BEFORE_KEYS = ["attempted", "method", "screensaverRunningBefore", "screensaverRunningAfter", "screensaverSecure",
                    "screensaverSecureOwnerOnly", "screensaverSecureReason", "dismissFailed", "dismissWait", "inputDesktopNudge",
                    "sendInputError", "executionStateError", "screensaverTimeoutSeconds", "screensaverActive", "utc"]

    def _wake(self, *, session: str, running_before: str, running_after: str | None = None, secure: str = "$false") -> dict:
        running_after = running_before if running_after is None else running_after
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverTimeoutSeconds','Get-AttrCudaScreensaverActive',"
            "'Get-AttrCudaScreensaverSecure','Wait-AttrCudaScreensaverDismissed',"
            "'Invoke-AttrCudaInputDesktopNudge','Invoke-AttrCudaBoundedProbe','Start-AttrCudaDisplayWake')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)
        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n" + _FAKE_WAKE_NATIVE_SURFACE +
                "$script:__runningCall = 0\n"
                "function Get-AttrCudaScreensaverRunning { $script:__runningCall++; "
                f"if ($script:__runningCall -le 1) {{ {running_before} }} else {{ {running_after} }} }}\n"
                f"function Get-AttrCudaScreensaverSecure {{ {secure} }}\n"
                f"function Get-AttrCudaSessionLocked {{ {session} }}\n"
                "$w = Start-AttrCudaDisplayWake\n"
                "$out = [ordered]@{ wake = $w; keys = @($w.Keys); "
                "sendInputCalls = [MLVAppAttrCudaDisplayWake.NativeMethods]::SendInputCalls; "
                "sendInputLog = [MLVAppAttrCudaDisplayWake.NativeMethods]::SendInputLog; "
                "nudgeRunCalls = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls }\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'w.json')}', ($out | ConvertTo-Json -Depth 6))\n"
            )
        proc = _run_pwsh_file(probe_script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))

    def test_a_locked_session_refuses_with_zero_send_input_calls(self) -> None:
        # Both nudge paths: running=$false would take the plain SendInput pair, running=$true the
        # dedicated-thread dismiss. Neither may run.
        for running in ("$false", "$true"):
            with self.subTest(running=running):
                out = self._wake(session="$true", running_before=running)
                self.assertEqual(out["sendInputCalls"], 0, out)
                self.assertEqual(out["nudgeRunCalls"], 0, out)
                wake = out["wake"]
                self.assertIs(wake["sessionLocked"], True)
                self.assertTrue(wake["sendInputError"].startswith("ATTRCUDA_SESSION_LOCKED_OWNER_ONLY this session is locked"), wake)
                self.assertIn("SessionLockedNoInputAttempted(sessionLocked=true)", wake["method"])
                self.assertIsNone(wake["inputDesktopNudge"])
                self.assertIsNone(wake["dismissWait"])
                self.assertIs(wake["dismissFailed"], False)

    def test_an_unknown_lock_state_fails_closed(self) -> None:
        # A probe that returns $null, and one that throws: both are unknown, refused like a lock.
        for session in ("$null", "throw 'LOCK_PROBE_FAILED'"):
            with self.subTest(session=session):
                out = self._wake(session=session, running_before="$false")
                self.assertEqual(out["sendInputCalls"], 0, out)
                self.assertEqual(out["nudgeRunCalls"], 0, out)
                wake = out["wake"]
                self.assertIsNone(wake["sessionLocked"])
                self.assertTrue(wake["sendInputError"].startswith("ATTRCUDA_SESSION_LOCKED_OWNER_ONLY"), wake)
                self.assertIn("could not be read; treated as locked", wake["sendInputError"])
                self.assertIn("SessionLockedNoInputAttempted(sessionLocked=unknown)", wake["method"])

    def test_an_unlocked_session_is_unchanged_apart_from_the_added_field(self) -> None:
        # Plain path: the same net-zero 1-px pair, one SendInput call, the same method text.
        out = self._wake(session="$false", running_before="$false")
        self.assertEqual(out["sendInputCalls"], 1)
        self.assertEqual(out["sendInputLog"], "0:1,0;0:-1,0;")
        self.assertEqual(out["nudgeRunCalls"], 0)
        wake = out["wake"]
        self.assertIs(wake["sessionLocked"], False)
        self.assertIsNone(wake["sendInputError"])
        self.assertEqual(wake["method"], "SendInputPointerNudge+SetThreadExecutionState(ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)")
        self.assertEqual(out["keys"], self._BEFORE_KEYS[:2] + ["sessionLocked"] + self._BEFORE_KEYS[2:])
        # Dismiss path: one dedicated-thread nudge, no plain SendInput, dismissed.
        out = self._wake(session="$false", running_before="$true", running_after="$false")
        self.assertEqual(out["nudgeRunCalls"], 1)
        self.assertEqual(out["sendInputCalls"], 0)
        wake = out["wake"]
        self.assertIs(wake["sessionLocked"], False)
        self.assertEqual(wake["method"], "OpenInputDesktop+SetThreadDesktop+SendInputPointerNudge(dedicated thread)+SetThreadExecutionState(ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)")
        self.assertIs(wake["dismissFailed"], False)
        self.assertIsNone(wake["sendInputError"])

    def test_the_real_lock_read_never_throws(self) -> None:
        # Read-only (ProcessIdToSessionId + WTSQuerySessionInformationW); sends nothing.
        proc = self.run_with_module(
            "$s = Get-AttrCudaSessionLocked\n"
            "Write-Output \"RESULT=$($null -eq $s ? 'NULL' : $s)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"RESULT=(True|False|NULL)")


@requires_pwsh
class SessionLockedKeepAliveTests(unittest.TestCase):
    """The keep-alive loop re-reads the console lock every tick, BEFORE InputDesktopNudge.Run: locked
    or unknown is a typed ATTRCUDA_KEEPALIVE_BLOCKED with no injection. The lock state lives in a static
    field so a test can flip it mid-leg (each bounded probe runs in a fresh Runspace, so a script-scope
    counter would not survive between ticks)."""

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 keep-alive is Windows-only (P/Invoke, drive-letter paths)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-keepalive-lock-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _run(self, body: str) -> dict:
        extract_script = self.tmp / "extract.ps1"
        extract_script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "Import-Module '" + str(MODULE) + "' -Force\n"
            "$src = Get-AttrCudaEmbeddedFunctionSource -Name @("
            "'Register-AttrCudaDisplayWakeNativeMethods','Get-AttrCudaScreensaverRunning',"
            "'Get-AttrCudaScreensaverSecure','Invoke-AttrCudaBoundedProbe',"
            "'Start-AttrCudaDisplayWakeKeepAlive','Stop-AttrCudaDisplayWakeKeepAlive')\n"
            f"Set-Content -LiteralPath '{(self.tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
            encoding="utf-8",
        )
        extract_proc = _run_pwsh_file(extract_script)
        self.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)
        probe_script = self.tmp / "extracted.ps1"
        with probe_script.open("a", encoding="utf-8") as f:
            f.write(
                "\n$ErrorActionPreference = 'Stop'\n" + _FAKE_WAKE_NATIVE_SURFACE +
                "Add-Type -TypeDefinition 'namespace MLVAppTestSessionLock { public static class State { public static int Value; public static int Reads; } }'\n"
                # 0 = unlocked, 1 = locked, anything else = unknown
                "function Get-AttrCudaSessionLocked { [MLVAppTestSessionLock.State]::Reads++; "
                "switch ([MLVAppTestSessionLock.State]::Value) { 0 { $false } 1 { $true } default { $null } } }\n"
                "function Get-AttrCudaScreensaverRunning { $false }\n"
                "function Get-AttrCudaScreensaverSecure { $false }\n"
                "function Wait-Until([scriptblock]$Condition, [int]$Seconds = 30) { "
                "$deadline = [DateTime]::UtcNow.AddSeconds($Seconds); "
                "while (-not (& $Condition) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }; [bool](& $Condition) }\n"
                + body +
                "$out['runCalls'] = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls\n"
                "$out['sendInputCalls'] = [MLVAppAttrCudaDisplayWake.NativeMethods]::SendInputCalls\n"
                "$out['lockReads'] = [MLVAppTestSessionLock.State]::Reads\n"
                f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json -Depth 5))\n"
            )
        proc = _run_pwsh_file(probe_script, timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))

    def _from_start(self, state: int) -> dict:
        return self._run(
            f"[MLVAppTestSessionLock.State]::Value = {state}\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "$blocked = Wait-Until { [int]$h.nudgeState.failureCount -ge 2 -and $h.nudgeState.lastError }\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
            "$out = [ordered]@{ blocked = $blocked; stop = $r }\n"
        )

    def test_a_locked_session_blocks_every_tick_with_no_injection(self) -> None:
        out = self._from_start(1)
        self.assertTrue(out["blocked"], out)
        self.assertEqual(out["runCalls"], 0, out)
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["stop"]["successCount"], 0)
        self.assertGreaterEqual(out["stop"]["failureCount"], 2)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED reason=session_locked ", out["stop"]["lastError"])

    def test_an_unknown_lock_state_fails_closed_every_tick(self) -> None:
        out = self._from_start(2)
        self.assertTrue(out["blocked"], out)
        self.assertEqual(out["runCalls"], 0, out)
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED reason=session_lock_unknown", out["stop"]["lastError"])

    def test_a_mid_leg_lock_stops_injection_with_reason_session_locked(self) -> None:
        out = self._run(
            "[MLVAppTestSessionLock.State]::Value = 0\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "$ranUnlocked = Wait-Until { [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls -ge 1 -and [int]$h.nudgeState.successCount -ge 1 }\n"
            "[MLVAppTestSessionLock.State]::Value = 1\n"
            "$blocked = Wait-Until { \"$($h.nudgeState.lastError)\" -like '*reason=session_locked *' }\n"
            "$runsAtBlock = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls\n"
            "$failuresAtBlock = [int]$h.nudgeState.failureCount\n"
            # two more locked ticks
            "$moreBlocked = Wait-Until { [int]$h.nudgeState.failureCount -ge ($failuresAtBlock + 2) }\n"
            "$runsAfter = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
            "$out = [ordered]@{ ranUnlocked = $ranUnlocked; blocked = $blocked; moreBlocked = $moreBlocked; "
            "runsAtBlock = $runsAtBlock; runsAfter = $runsAfter; stop = $r }\n"
        )
        self.assertTrue(out["ranUnlocked"], out)
        self.assertTrue(out["blocked"], out)
        self.assertTrue(out["moreBlocked"], out)
        self.assertGreaterEqual(out["runsAtBlock"], 1)
        self.assertEqual(out["runsAfter"], out["runsAtBlock"], "no nudge may run once the console is locked")
        self.assertGreaterEqual(out["stop"]["successCount"], 1)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED reason=session_locked ", out["stop"]["lastError"])

    def test_an_unlocked_session_ticks_exactly_as_before_and_the_lock_is_read_every_tick(self) -> None:
        out = self._run(
            "[MLVAppTestSessionLock.State]::Value = 0\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "$ticked = Wait-Until { [int]$h.nudgeState.successCount -ge 2 }\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
            "$out = [ordered]@{ ticked = $ticked; stop = $r }\n"
        )
        self.assertTrue(out["ticked"], out)
        stop = out["stop"]
        self.assertEqual(stop["failureCount"], 0)
        self.assertIsNone(stop["lastError"])
        self.assertEqual(stop["successCount"], stop["nudgeCount"])
        self.assertGreaterEqual(out["runCalls"], stop["nudgeCount"])
        self.assertGreaterEqual(out["lockReads"], stop["nudgeCount"], "every tick must read the lock")


class SessionLockedJobAndLoopShapeTests(unittest.TestCase):
    """Static shape: the job's typed exit-30 refusal sits before every other gate, exit 30 is named in
    the header and used once, the lock read is embedded, the loop reads the lock first, and the read
    uses the measured WTSINFOEXW layout."""

    def setUp(self) -> None:
        self.job = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        self.module = MODULE.read_text(encoding="utf-8")
        loop_start_at = self.module.index("$loopScript = {")
        loop_end_at = self.module.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke", loop_start_at)
        self.loop_body = self.module[loop_start_at:loop_end_at]

    def test_the_job_stops_a_locked_session_typed_with_exit_30_before_any_other_gate(self) -> None:
        wake_at = self.job.index("$displayWake = Start-AttrCudaDisplayWake")
        lock_at = self.job.index("if ($displayWake.sessionLocked -ne $false) {", wake_at)
        secure_at = self.job.index("if ($displayWake.screensaverSecureOwnerOnly) {", wake_at)
        keep_alive_at = self.job.index("$displayWakeKeepAlive = Start-AttrCudaDisplayWakeKeepAlive", wake_at)
        self.assertLess(wake_at, lock_at)
        self.assertLess(lock_at, secure_at)
        self.assertLess(secure_at, keep_alive_at)
        block = self.job[lock_at:secure_at]
        self.assertIn("result='SESSION_LOCKED_OWNER_ONLY'", block)
        self.assertIn("displayWake=$displayWake", block)
        self.assertIn('Write-Output "RESULT=SESSION_LOCKED_OWNER_ONLY ARTIFACTS=$Pub"', block)
        self.assertIn("exit 30", block)

    def test_exit_30_is_named_in_the_header_and_used_once(self) -> None:
        # "Used once" = used for one result only. r2 (sol blocker 2): the claim gate plus the three
        # keep-alive checkpoints, and every one of the four is a SESSION_LOCKED_OWNER_ONLY exit.
        header = self.job[:self.job.index("[CmdletBinding()]")]
        self.assertIn("exit 30 = SESSION_LOCKED_OWNER_ONLY", header)
        exits = re.findall(r"(?m)^\s*exit\s+(\d+)\s*$", self.job)
        self.assertEqual(exits.count("30"), 4, "exit 30: the claim gate and the three keep-alive checkpoints")
        for m in re.finditer(r"(?m)^\s*exit\s+30\s*$", self.job):
            preceding = self.job[:m.start()].rstrip().rsplit("\n", 1)[-1]
            self.assertIn('Write-Output "RESULT=SESSION_LOCKED_OWNER_ONLY ', preceding, "exit 30 is SESSION_LOCKED_OWNER_ONLY only")

    def test_the_job_embeds_the_lock_read(self) -> None:
        # Spliced inside its own sentinel brackets (test_dual_venue_evidence's byte-identity strip removes it).
        self.assertIn(
            "$embeddedFunctions = $embeddedFunctions + \"`r`n# VENUE-SESSION-LOCKED-REFUSAL-1 >>>`r`n\" + "
            "(Get-AttrCudaEmbeddedFunctionSource -Name @(\n    'Get-AttrCudaSessionLocked'\n)) +\"`r`n# VENUE-SESSION-LOCKED-REFUSAL-1 <<<\"",
            self.job)

    def test_every_tick_reads_the_lock_first_and_gates_before_the_nudge(self) -> None:
        read_at = self.loop_body.index(
            "$tickSessionLocked = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked'")
        running_at = self.loop_body.index("$tickRunning = Invoke-AttrCudaBoundedProbe")
        gate_at = self.loop_body.index("if ($tickSessionLocked -ne $false) {")
        run_at = self.loop_body.index("[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run(")
        self.assertLess(read_at, running_at)
        self.assertLess(gate_at, run_at)
        self.assertIn("'session_locked'", self.loop_body[gate_at:run_at])
        foreach_at = self.module.index("foreach ($tickProbeFunctionName in @(")
        runspace_at = self.module.index("$runspace = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($initialSessionState)", foreach_at)
        self.assertIn("'Get-AttrCudaSessionLocked', (Get-Command -Name 'Get-AttrCudaSessionLocked' -CommandType Function).Definition",
                      self.module[foreach_at:runspace_at])

    def test_the_lock_read_uses_the_measured_wtsinfoex_layout(self) -> None:
        start = self.module.index("function Get-AttrCudaSessionLocked {")
        fn = self.module[start:self.module.index("\nfunction Start-AttrCudaDisplayWake {", start)]
        for needle in ("ProcessIdToSessionId([uint32]$PID, [ref]$sessionId)",
                       "WTSQuerySessionInformationW([IntPtr]::Zero, $sessionId, 25, [ref]$buffer, [ref]$bytes)",
                       "ReadInt32($buffer, 0)", "ReadInt32($buffer, 8)", "ReadInt32($buffer, 16)",
                       "if ($level -ne 1 -or [int64]$reportedSessionId -ne [int64]$sessionId) { return $null }",
                       "if ($sessionFlags -eq 0) { return $true }", "if ($sessionFlags -eq 1) { return $false }",
                       "WTSFreeMemory($buffer)"):
            self.assertIn(needle, fn)


# --------------------------------------------------------------------------------------------
# VENUE-SESSION-LOCKED-REFUSAL-1 r2 (sol r1b blockers 1 and 2). Blocker 1: the lock read was taken
# before the screen-saver probes, so a lock arriving during them still reached SendInput. Blocker 2:
# a lock refused by the keep-alive ended the job KEEPALIVE_FAILED (exit 26), not owner-only.
# The lock state lives in a static field so a fake probe can flip it, and so it survives the fresh
# Runspace every bounded keep-alive probe runs in.
# --------------------------------------------------------------------------------------------

_LATE_LOCK_STATE = (
    "Add-Type -TypeDefinition 'namespace MLVAppTestLateLock { public static class State "
    "{ public static int Value; public static int Reads; public static int RunningNull; } }'\n"
    # 0 = unlocked, 1 = locked, anything else = unknown
    "function Get-AttrCudaSessionLocked { [MLVAppTestLateLock.State]::Reads++; "
    "switch ([MLVAppTestLateLock.State]::Value) { 0 { $false } 1 { $true } default { $null } } }\n"
    "function Wait-Until([scriptblock]$Condition, [int]$Seconds = 45) { "
    "$deadline = [DateTime]::UtcNow.AddSeconds($Seconds); "
    "while (-not (& $Condition) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }; [bool](& $Condition) }\n"
)


def _extract_then_append(case: unittest.TestCase, tmp: Path, functions: list[str], body: str, timeout: int | None = None):
    extract_script = tmp / "extract.ps1"
    extract_script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "Import-Module '" + str(MODULE) + "' -Force\n"
        "$src = Get-AttrCudaEmbeddedFunctionSource -Name @(" + ",".join(f"'{n}'" for n in functions) + ")\n"
        f"Set-Content -LiteralPath '{(tmp / 'extracted.ps1')}' -Value $src -Encoding utf8\n",
        encoding="utf-8",
    )
    extract_proc = _run_pwsh_file(extract_script)
    case.assertEqual(extract_proc.returncode, 0, extract_proc.stdout + extract_proc.stderr)
    probe_script = tmp / "extracted.ps1"
    with probe_script.open("a", encoding="utf-8") as f:
        f.write("\n$ErrorActionPreference = 'Stop'\n" + _FAKE_WAKE_NATIVE_SURFACE + _LATE_LOCK_STATE + body)
    if timeout is None:
        return _run_pwsh_file(probe_script)
    return _run_pwsh_file(probe_script, timeout=timeout)


@requires_pwsh
class SessionLockedLateLockWakeTests(unittest.TestCase):
    """r2 blocker 1, claim wake: the lock is read again after every probe, again as the statement before
    the plain SendInput, and the dedicated thread's own last read is honoured. A lock that arrives after
    the first read sends nothing."""

    _FUNCTIONS = ["Register-AttrCudaDisplayWakeNativeMethods", "Get-AttrCudaScreensaverRunning",
                  "Get-AttrCudaScreensaverTimeoutSeconds", "Get-AttrCudaScreensaverActive",
                  "Get-AttrCudaScreensaverSecure", "Wait-AttrCudaScreensaverDismissed",
                  "Invoke-AttrCudaInputDesktopNudge", "Invoke-AttrCudaBoundedProbe", "Start-AttrCudaDisplayWake"]

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 display wake is Windows-only (P/Invoke)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-late-lock-wake-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _wake(self, overrides: str, timeout: int | None = None) -> dict:
        proc = _extract_then_append(self, self.tmp, self._FUNCTIONS, overrides + (
            "$w = Start-AttrCudaDisplayWake\n"
            "$out = [ordered]@{ wake = $w; "
            "sendInputCalls = [MLVAppAttrCudaDisplayWake.NativeMethods]::SendInputCalls; "
            "nudgeRunCalls = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls; "
            "lockReads = [MLVAppTestLateLock.State]::Reads }\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'w.json')}', ($out | ConvertTo-Json -Depth 6))\n"), timeout=timeout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "w.json").read_text(encoding="utf-8"))

    def _assert_owner_only_locked(self, out: dict) -> None:
        wake = out["wake"]
        self.assertIs(wake["sessionLocked"], True, out)
        self.assertTrue(wake["sendInputError"].startswith("ATTRCUDA_SESSION_LOCKED_OWNER_ONLY this session is locked"), wake)
        self.assertIn("SessionLockedNoInputAttempted(sessionLocked=true)", wake["method"])
        self.assertIs(wake["dismissFailed"], False)
        self.assertIsNone(wake["dismissWait"])

    def test_a_lock_arriving_during_the_screensaver_probe_sends_no_input(self) -> None:
        # Sol's repro: the first read is unlocked, then the running probe flips the console to locked.
        out = self._wake(
            "function Get-AttrCudaScreensaverRunning { [MLVAppTestLateLock.State]::Value = 1; $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n")
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["nudgeRunCalls"], 0, out)
        self._assert_owner_only_locked(out)

    def test_a_lock_arriving_during_the_secure_probe_skips_the_dedicated_thread_nudge(self) -> None:
        # Running screen saver (the dismiss path); the console locks while the secure state is read.
        out = self._wake(
            "function Get-AttrCudaScreensaverRunning { $true }\n"
            "function Get-AttrCudaScreensaverSecure { [MLVAppTestLateLock.State]::Value = 1; $false }\n")
        self.assertEqual(out["nudgeRunCalls"], 0, out)
        self.assertEqual(out["sendInputCalls"], 0, out)
        self._assert_owner_only_locked(out)
        self.assertIsNone(out["wake"]["inputDesktopNudge"])

    def test_the_plain_nudge_reads_the_lock_as_the_statement_before_send_input(self) -> None:
        # The first two reads (claim, then after the probes) are unlocked; the third -- the read right
        # before the plain SendInput -- is locked. Only that last read can stop this injection.
        out = self._wake(
            "function Get-AttrCudaSessionLocked { [MLVAppTestLateLock.State]::Reads++; "
            "if ([MLVAppTestLateLock.State]::Reads -ge 3) { $true } else { $false } }\n"
            "function Get-AttrCudaScreensaverRunning { $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n")
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["lockReads"], 3, out)
        self._assert_owner_only_locked(out)

    def test_a_refusal_by_the_dedicated_threads_own_lock_read_is_owner_only(self) -> None:
        for lock_at_inject, expected_locked, text in ((1, True, "this session is locked"),
                                                      (2, None, "could not be read; treated as locked")):
            with self.subTest(lock_at_inject=lock_at_inject):
                out = self._wake(
                    f"[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::LockAtInject = {lock_at_inject}\n"
                    "$script:__r = 0\n"
                    "function Get-AttrCudaScreensaverRunning { $script:__r++; $script:__r -le 1 }\n"
                    "function Get-AttrCudaScreensaverSecure { $false }\n")
                self.assertEqual(out["nudgeRunCalls"], 1, out)
                self.assertEqual(out["sendInputCalls"], 0, out)
                wake = out["wake"]
                self.assertIs(wake["sessionLocked"], expected_locked, out)
                self.assertTrue(wake["sendInputError"].startswith("ATTRCUDA_SESSION_LOCKED_OWNER_ONLY "), wake)
                self.assertIn(text, wake["sendInputError"])
                self.assertIs(wake["inputDesktopNudge"]["sessionLockRefused"], True)
                self.assertIs(wake["dismissFailed"], False)
                self.assertIsNone(wake["dismissWait"])

    def test_a_claim_time_lock_read_that_never_returns_is_unknown_and_refused_promptly(self) -> None:
        # r4 (CLAIM-TIME-LOCK-READ-BOUNDED-1): the claim-time read is bounded like the per-tick read, so a
        # lock read that never returns is unknown within the bound: refused, with zero input.
        out = self._wake(
            "function Get-AttrCudaSessionLocked { [MLVAppTestLateLock.State]::Reads++; Start-Sleep -Seconds 600; $false }\n"
            "function Get-AttrCudaScreensaverRunning { $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n",
            timeout=60)   # an unbounded read would hold the claim for the full 600 s
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["nudgeRunCalls"], 0, out)
        self.assertEqual(out["lockReads"], 1, "the first (hung) read already refuses; nothing is read after it")
        wake = out["wake"]
        self.assertIsNone(wake["sessionLocked"], out)
        self.assertTrue(wake["sendInputError"].startswith("ATTRCUDA_SESSION_LOCKED_OWNER_ONLY "), wake)
        self.assertIn("could not be read; treated as locked", wake["sendInputError"])
        self.assertIn("SessionLockedNoInputAttempted(sessionLocked=unknown)", wake["method"])
        self.assertIs(wake["dismissFailed"], False)

    def test_every_claim_time_lock_read_goes_through_the_bounded_reader(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        start = module.index("function Start-AttrCudaDisplayWake {")
        fn = module[start:module.index("\nfunction Stop-AttrCudaDisplayWake {", start)]
        body = fn[fn.index("#>"):]   # past the help block
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(code.count("Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked'"), 3, code)
        self.assertEqual(code.count("Get-AttrCudaSessionLocked"), 3, "no unbounded direct call may remain")


@requires_pwsh
class SessionLockedLateLockKeepAliveTests(unittest.TestCase):
    """r2 blocker 1, keep-alive: a tick that would inject reads the lock again as its last step before
    InputDesktopNudge.Run, and a refusal by that thread's own read is the same typed, sticky failure."""

    _FUNCTIONS = ["Register-AttrCudaDisplayWakeNativeMethods", "Get-AttrCudaScreensaverRunning",
                  "Get-AttrCudaScreensaverSecure", "Invoke-AttrCudaBoundedProbe",
                  "Start-AttrCudaDisplayWakeKeepAlive", "Stop-AttrCudaDisplayWakeKeepAlive"]

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 keep-alive is Windows-only (P/Invoke, drive-letter paths)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-late-lock-keepalive-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _run(self, body: str) -> dict:
        proc = _extract_then_append(self, self.tmp, self._FUNCTIONS, body + (
            "$out['runCalls'] = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::RunCalls\n"
            "$out['sendInputCalls'] = [MLVAppAttrCudaDisplayWake.NativeMethods]::SendInputCalls\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($out | ConvertTo-Json -Depth 5))\n"),
            timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))

    def test_a_lock_arriving_during_the_tick_probes_gets_no_nudge(self) -> None:
        # Sol's repro: the tick's first lock read is unlocked, then the running probe locks the console.
        out = self._run(
            "function Get-AttrCudaScreensaverRunning { [MLVAppTestLateLock.State]::Value = 1; $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "$blocked = Wait-Until { [int]$h.nudgeState.failureCount -ge 2 }\n"
            "$reason = $h.nudgeState.sessionLockReason\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
            "$out = [ordered]@{ blocked = $blocked; reason = $reason; stop = $r }\n")
        self.assertTrue(out["blocked"], out)
        self.assertEqual(out["runCalls"], 0, out)
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["stop"]["successCount"], 0, out)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED reason=session_locked ", out["stop"]["lastError"])
        self.assertEqual(out["reason"], "session_locked")

    def test_a_refusal_by_the_nudge_threads_own_lock_read_is_a_typed_sticky_failure(self) -> None:
        out = self._run(
            "[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::LockAtInject = 2\n"
            "function Get-AttrCudaScreensaverRunning { $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "$blocked = Wait-Until { [int]$h.nudgeState.failureCount -ge 2 }\n"
            "$reason = $h.nudgeState.sessionLockReason\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h\n"
            "$out = [ordered]@{ blocked = $blocked; reason = $reason; stop = $r }\n")
        self.assertTrue(out["blocked"], out)
        self.assertGreaterEqual(out["runCalls"], 2, out)
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["stop"]["successCount"], 0, out)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED reason=session_lock_unknown ", out["stop"]["lastError"])
        self.assertEqual(out["reason"], "session_lock_unknown")

    def test_a_hung_lock_probe_blocks_every_tick_as_unknown_and_stop_returns_promptly(self) -> None:
        # Fable r1b hardening 2 (KEEPALIVE-HUNG-LOCK-PROBE-TEST-1): the bounded probe turns a lock read
        # that never returns into unknown, every tick, and Stop is not held up by it.
        out = self._run(
            "function Get-AttrCudaSessionLocked { Start-Sleep -Seconds 60; $false }\n"
            "function Get-AttrCudaScreensaverRunning { $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1 -ProbeTimeoutMilliseconds 500\n"
            "$blocked = Wait-Until { [int]$h.nudgeState.failureCount -ge 2 }\n"
            "$reason = $h.nudgeState.sessionLockReason\n"
            "$sw = [System.Diagnostics.Stopwatch]::StartNew()\n"
            "$r = Stop-AttrCudaDisplayWakeKeepAlive -Handle $h -TimeoutMilliseconds 3000\n"
            "$sw.Stop()\n"
            "$out = [ordered]@{ blocked = $blocked; reason = $reason; stop = $r; stopElapsedMs = $sw.ElapsedMilliseconds }\n")
        self.assertTrue(out["blocked"], out)
        self.assertEqual(out["runCalls"], 0, out)
        self.assertEqual(out["sendInputCalls"], 0, out)
        self.assertEqual(out["stop"]["successCount"], 0, out)
        self.assertIn("ATTRCUDA_KEEPALIVE_BLOCKED reason=session_lock_unknown ", out["stop"]["lastError"])
        self.assertLess(out["stopElapsedMs"], 10000, out)


@requires_pwsh
class SessionLockedKeepAliveCheckpointTests(unittest.TestCase):
    """r2 blocker 2 (= fable r1b hardening 1): a lock refused by the keep-alive ends the job at its next
    keep-alive checkpoint as SESSION_LOCKED_OWNER_ONLY with exit 30. Any other keep-alive failure keeps
    KEEPALIVE_FAILED / exit 26. Runs the real keep-alive (fake native surface) and then the job's own
    checkpoint text, lifted verbatim from the template, with Save-Json replaced by a recorder."""

    _FUNCTIONS = ["Register-AttrCudaDisplayWakeNativeMethods", "Get-AttrCudaScreensaverRunning",
                  "Get-AttrCudaScreensaverSecure", "Invoke-AttrCudaBoundedProbe",
                  "Start-AttrCudaDisplayWakeKeepAlive", "Stop-AttrCudaDisplayWakeKeepAlive",
                  "Get-AttrCudaDisplayWakeKeepAliveHealth"]
    # (first line of the checkpoint, the text that follows its block)
    _CHECKPOINTS = {
        "start_of_measured_interval": ("$keepAliveHealthAtMeasurementStart = Get-AttrCudaDisplayWakeKeepAliveHealth",
                                       "# OWNER-FOOTAGE-NO-HARDLINK-1"),
        "before_smoke_launch": ("$keepAliveHealthBeforeSmokeLaunch = Get-AttrCudaDisplayWakeKeepAliveHealth", "$smokeRc = $null"),
        "after_measured_interval": ("$keepAliveHealthAfterMeasuredInterval = Get-AttrCudaDisplayWakeKeepAliveHealth",
                                    "# Backend-availability gate"),
    }

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 keep-alive is Windows-only (P/Invoke, drive-letter paths)")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-lock-checkpoint-")
        self.tmp = _long_path(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _checkpoint(self, name: str, *, lock_value: int | None = None, running_null: bool = False):
        job = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        first, after = self._CHECKPOINTS[name]
        start = job.index(first)
        block = job[start:job.index(after, start)]
        flip = (f"[MLVAppTestLateLock.State]::Value = {lock_value}\n" if lock_value is not None else "") + \
               ("[MLVAppTestLateLock.State]::RunningNull = 1\n" if running_null else "")
        saved = self.tmp / "saved.json"
        body = (
            "function Get-AttrCudaScreensaverRunning { if ([MLVAppTestLateLock.State]::RunningNull -eq 1) { $null } else { $false } }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n"
            "[MLVAppTestLateLock.State]::Value = 0\n"
            "$h = Start-AttrCudaDisplayWakeKeepAlive -IntervalSeconds 1\n"
            "if (-not (Wait-Until { [int]$h.nudgeState.successCount -ge 1 })) { throw 'no unlocked tick succeeded' }\n"
            + flip +
            "if (-not (Wait-Until { [int]$h.nudgeState.failureCount -ge 3 })) { throw 'three failed ticks never arrived' }\n"
            "[void](Stop-AttrCudaDisplayWakeKeepAlive -Handle $h)\n"
            "$displayWakeKeepAlive = $h\n"
            "$displayWake = [ordered]@{ attempted = $true }\n"
            f"$FixtureRehearsal = $true; $SourceCommit = 'r2test'; $ClipId = 'clip'; $Pub = '{(self.tmp / 'pub')}'\n"
            "$presentMonProc = $null; $PresentMonSessionName = 'r2test'\n"
            f"$presentMonPath = '{(self.tmp / 'absent-presentmon.csv')}'\n"
            f"function Save-Json($Object, $Path) {{ [IO.File]::WriteAllText('{saved}', ($Object | ConvertTo-Json -Depth 8)) }}\n"
            "function Write-JobTrace { param($Message) }\n"
            "function Stop-PresentMonCapture { param($Proc, $SessionName) "
            "[ordered]@{ confirmedExited = $true; killError = $null; waitError = $null; postKillSessionTerminate = $null } }\n"
            "function Format-PresentMonSessionTerminateText { param($Value) 'none' }\n"
            "function Publish-AttrCudaFileCopy { param($Source, $Destination) $true }\n"
            + block +
            "\nWrite-Output 'CHECKPOINT_FELL_THROUGH'\nexit 0\n"
        )
        proc = _extract_then_append(self, self.tmp, self._FUNCTIONS, body, timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        summary = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None
        return proc, summary

    def _assert_owner_only(self, name: str, proc, summary, reason: str) -> None:
        self.assertEqual(proc.returncode, 30, proc.stdout + proc.stderr)
        self.assertIn(f"RESULT=SESSION_LOCKED_OWNER_ONLY CHECKPOINT={name} REASON={reason} ", proc.stdout)
        self.assertNotIn("KEEPALIVE_FAILED", proc.stdout)
        self.assertEqual(summary["result"], "SESSION_LOCKED_OWNER_ONLY", summary)
        self.assertEqual(summary["keepAliveCheckpoint"], name)
        self.assertEqual(summary["sessionLockReason"], reason)
        self.assertGreaterEqual(summary["displayWake"]["keepAliveHealth"]["successCount"], 1)

    def test_a_mid_leg_lock_at_the_measurement_start_checkpoint_is_owner_only_exit_30(self) -> None:
        # Sol's repro: one unlocked success, then three ticks refused reason=session_locked.
        proc, summary = self._checkpoint("start_of_measured_interval", lock_value=1)
        self._assert_owner_only("start_of_measured_interval", proc, summary, "session_locked")

    def test_an_unknown_lock_mid_leg_is_owner_only_exit_30(self) -> None:
        proc, summary = self._checkpoint("start_of_measured_interval", lock_value=2)
        self._assert_owner_only("start_of_measured_interval", proc, summary, "session_lock_unknown")

    def test_a_non_lock_keepalive_failure_stays_keepalive_failed_exit_26(self) -> None:
        # Unchanged: an unreadable screen-saver state (state_unknown) on an unlocked console.
        proc, summary = self._checkpoint("start_of_measured_interval", running_null=True)
        self.assertEqual(proc.returncode, 26, proc.stdout + proc.stderr)
        self.assertIn("RESULT=KEEPALIVE_FAILED CHECKPOINT=start_of_measured_interval ", proc.stdout)
        self.assertEqual(summary["result"], "KEEPALIVE_FAILED", summary)
        self.assertIn("reason=state_unknown", summary["displayWake"]["keepAliveHealth"]["lastError"])
        self.assertIsNone(summary["displayWake"]["keepAliveHealth"]["sessionLockReason"])

    def test_the_before_smoke_launch_checkpoint_is_owner_only_exit_30(self) -> None:
        proc, summary = self._checkpoint("before_smoke_launch", lock_value=1)
        self._assert_owner_only("before_smoke_launch", proc, summary, "session_locked")
        self.assertIs(summary["presentMonConfirmedExited"], True, "PresentMon is stopped first, as on KEEPALIVE_FAILED")

    def test_the_after_measured_interval_checkpoint_is_owner_only_exit_30(self) -> None:
        proc, summary = self._checkpoint("after_measured_interval", lock_value=1)
        self._assert_owner_only("after_measured_interval", proc, summary, "session_locked")

    def test_every_checkpoint_tests_the_lock_before_the_generic_keepalive_failure(self) -> None:
        job = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        for name, (first, after) in self._CHECKPOINTS.items():
            with self.subTest(checkpoint=name):
                start = job.index(first)
                block = job[start:job.index(after, start)]
                variable = first.split(" = ", 1)[0]
                lock_at = block.index(f"if ({variable}.sessionLockReason) {{")
                generic_at = block.index(f"if (-not {variable}.healthy) {{")
                self.assertLess(lock_at, generic_at)
                lock_block = block[lock_at:generic_at]
                self.assertIn("result='SESSION_LOCKED_OWNER_ONLY'", lock_block)
                self.assertIn(f"keepAliveCheckpoint='{name}'", lock_block)
                self.assertIn("exit 30", lock_block)
                self.assertNotIn("exit 26", lock_block)

    # r4 (sol r3 blocker VENUE-SESSION-LOCK-STATE-PUBLISH-ATOMIC-1): a checkpoint can run while the
    # locked tick is still publishing. The production loop runs verbatim on a state table whose
    # failureCount setter pauses on the first failure; the checkpoint runs during that pause.
    _PAUSING_STATE = (
        "Add-Type -TypeDefinition @'\n"
        "namespace MLVAppTestPublish {\n"
        "    public class PausingState : System.Collections.Hashtable {\n"
        "        public static System.Threading.ManualResetEventSlim Paused = new System.Threading.ManualResetEventSlim(false);\n"
        "        public static System.Threading.ManualResetEventSlim Release = new System.Threading.ManualResetEventSlim(false);\n"
        "        public static object ReasonAtPause;\n"
        "        public static object LastErrorAtPause;\n"
        "        public static object LastFailureUtcAtPause;\n"
        "        static int pauses;\n"
        "        public override object this[object key] {\n"
        "            get { return base[key]; }\n"
        "            set {\n"
        "                base[key] = value;\n"
        "                int n;\n"
        "                if (\"failureCount\".Equals(key) && value != null && int.TryParse(value.ToString(), out n) && n >= 1\n"
        "                        && System.Threading.Interlocked.Increment(ref pauses) == 1) {\n"
        "                    ReasonAtPause = base[\"sessionLockReason\"];\n"
        "                    LastErrorAtPause = base[\"lastError\"];\n"
        "                    LastFailureUtcAtPause = base[\"lastFailureUtc\"];\n"
        "                    Paused.Set();\n"
        "                    Release.Wait(60000);\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
        "'@\n"
    )

    # KEEPALIVE-FAILURE-PUBLISH-LAST-1: the non-lock failure branches of the same loop. The bounded probe
    # is replaced by a function that answers from a static Mode, so a test can pick which branch fails:
    # 1 = screen-saver state unreadable (secure branch), 2 = screen saver still running after the nudge
    # (tickError branch), 3 = the probe throws (catch branch). Mode 0 is a healthy unlocked tick.
    _FAILURE_MODE_STATE = (
        "Add-Type -TypeDefinition 'namespace MLVAppTestFailurePublish { public static class State { public static int Mode; } }'\n"
        "function Invoke-AttrCudaBoundedProbe { param([string]$FunctionName, [int]$TimeoutMilliseconds)\n"
        "    $m = [MLVAppTestFailurePublish.State]::Mode\n"
        "    if ($m -eq 3) { throw 'KEEPALIVE_PUBLISH_TEST_FAULT' }\n"
        "    switch ($FunctionName) {\n"
        "        'Get-AttrCudaSessionLocked' { $false }\n"
        "        'Get-AttrCudaScreensaverRunning' { if ($m -eq 1) { $null } elseif ($m -eq 2) { $true } else { $false } }\n"
        "        'Get-AttrCudaScreensaverSecure' { $false }\n"
        "    }\n"
        "}\n"
    )

    def _checkpoint_mid_publication(self, name: str, lock_value: int | None = None, failure_mode: int | None = None):
        job = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        first, after = self._CHECKPOINTS[name]
        start = job.index(first)
        block = job[start:job.index(after, start)]
        module = MODULE.read_text(encoding="utf-8")
        loop_at = module.index("$loopScript = {")
        loop_text = module[loop_at:module.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke", loop_at)].rstrip()
        saved = self.tmp / "saved.json"
        if failure_mode is None:
            flip = f"[MLVAppTestLateLock.State]::Value = {lock_value}\n"
            probe_overrides = ""
        else:
            flip = f"[MLVAppTestFailurePublish.State]::Mode = {failure_mode}\n"
            probe_overrides = self._FAILURE_MODE_STATE
        body = (
            self._PAUSING_STATE +
            "function Get-AttrCudaScreensaverRunning { $false }\n"
            "function Get-AttrCudaScreensaverSecure { $false }\n"
            + probe_overrides +
            "[MLVAppTestLateLock.State]::Value = 0\n"
            + loop_text + "\n"
            # The same state keys and Runspace set-up Start-AttrCudaDisplayWakeKeepAlive uses.
            "$state = [MLVAppTestPublish.PausingState]::new()\n"
            "foreach ($k in 'count', 'successCount', 'failureCount') { $state[$k] = 0 }\n"
            "foreach ($k in 'lastError', 'lastFailureUtc', 'lastDesktopName', 'sessionLockReason') { $state[$k] = $null }\n"
            "$iss = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()\n"
            "foreach ($n in 'Register-AttrCudaDisplayWakeNativeMethods', 'Get-AttrCudaScreensaverRunning', 'Get-AttrCudaScreensaverSecure', "
            "'Invoke-AttrCudaBoundedProbe', 'Get-AttrCudaSessionLocked') {\n"
            "    $iss.Commands.Add([System.Management.Automation.Runspaces.SessionStateFunctionEntry]::new($n, (Get-Command -Name $n -CommandType Function).Definition))\n"
            "}\n"
            "$stopEvent = [System.Threading.ManualResetEventSlim]::new($false)\n"
            "$rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($iss); $rs.Open()\n"
            "$ps = [System.Management.Automation.PowerShell]::Create(); $ps.Runspace = $rs\n"
            "[void]$ps.AddScript($loopScript).AddArgument($stopEvent).AddArgument(1).AddArgument($state).AddArgument(5000).AddArgument(3000)\n"
            "$async = $ps.BeginInvoke()\n"
            "try {\n"
            "if (-not (Wait-Until { [int]$state['successCount'] -ge 1 })) { throw 'no unlocked tick succeeded' }\n"
            + flip +
            "if (-not [MLVAppTestPublish.PausingState]::Paused.Wait(45000)) { throw 'the failing tick never published failureCount' }\n"
            "Write-Output \"REASON_AT_PAUSE=$([MLVAppTestPublish.PausingState]::ReasonAtPause)\"\n"
            "Write-Output \"LAST_ERROR_AT_PAUSE=$([MLVAppTestPublish.PausingState]::LastErrorAtPause)\"\n"
            "Write-Output \"LAST_FAILURE_UTC_AT_PAUSE=$([MLVAppTestPublish.PausingState]::LastFailureUtcAtPause)\"\n"
            "$displayWakeKeepAlive = [ordered]@{ stopEvent = $stopEvent; runspace = $rs; powershell = $ps; asyncResult = $async; "
            "nudgeState = $state; setupError = $null }\n"
            "$displayWake = [ordered]@{ attempted = $true }\n"
            f"$FixtureRehearsal = $true; $SourceCommit = 'r4test'; $ClipId = 'clip'; $Pub = '{(self.tmp / 'pub')}'\n"
            "$presentMonProc = $null; $PresentMonSessionName = 'r4test'\n"
            f"$presentMonPath = '{(self.tmp / 'absent-presentmon.csv')}'\n"
            f"function Save-Json($Object, $Path) {{ [IO.File]::WriteAllText('{saved}', ($Object | ConvertTo-Json -Depth 8)) }}\n"
            "function Write-JobTrace { param($Message) }\n"
            "function Stop-PresentMonCapture { param($Proc, $SessionName) "
            "[ordered]@{ confirmedExited = $true; killError = $null; waitError = $null; postKillSessionTerminate = $null } }\n"
            "function Format-PresentMonSessionTerminateText { param($Value) 'none' }\n"
            "function Publish-AttrCudaFileCopy { param($Source, $Destination) $true }\n"
            + block +
            "\nWrite-Output 'CHECKPOINT_FELL_THROUGH'\nexit 0\n"
            "} finally {\n"
            "    [MLVAppTestPublish.PausingState]::Release.Set(); $stopEvent.Set(); [void]$async.AsyncWaitHandle.WaitOne(15000)\n"
            "}\n"
        )
        proc = _extract_then_append(self, self.tmp, self._FUNCTIONS, body, timeout=_KEEPALIVE_TEST_TIMEOUT_SECONDS)
        summary = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None
        return proc, summary

    def _assert_owner_only_mid_publication(self, name: str, lock_value: int, reason: str) -> None:
        proc, summary = self._checkpoint_mid_publication(name, lock_value)
        self.assertIn(f"REASON_AT_PAUSE={reason}", proc.stdout, "the classification must be visible no later than the failure")
        self._assert_owner_only(name, proc, summary, reason)

    def test_a_checkpoint_between_the_locked_ticks_publications_at_measurement_start_is_exit_30(self) -> None:
        self._assert_owner_only_mid_publication("start_of_measured_interval", 1, "session_locked")

    def test_a_checkpoint_between_the_locked_ticks_publications_before_smoke_launch_is_exit_30(self) -> None:
        self._assert_owner_only_mid_publication("before_smoke_launch", 1, "session_locked")

    def test_a_checkpoint_between_the_locked_ticks_publications_after_the_interval_is_exit_30(self) -> None:
        self._assert_owner_only_mid_publication("after_measured_interval", 1, "session_locked")

    def test_a_checkpoint_between_an_unknown_lock_ticks_publications_is_exit_30(self) -> None:
        self._assert_owner_only_mid_publication("start_of_measured_interval", 2, "session_lock_unknown")

    # KEEPALIVE-FAILURE-PUBLISH-LAST-1: the same concurrent checkpoint for every non-lock failure branch.
    # A checkpoint that lands between the branch's writes and failureCount must print a non-empty `last:`.
    def _assert_keepalive_failed_with_last_error(self, failure_mode: int, expected_last_error: str) -> None:
        proc, summary = self._checkpoint_mid_publication("start_of_measured_interval", failure_mode=failure_mode)
        self.assertIn(f"LAST_ERROR_AT_PAUSE={expected_last_error}", proc.stdout,
                      "lastError must be published no later than failureCount")
        self.assertRegex(proc.stdout, r"LAST_FAILURE_UTC_AT_PAUSE=\d{4}-\d\d-\d\dT", proc.stdout)
        self.assertEqual(proc.returncode, 26, proc.stdout + proc.stderr)
        self.assertIn("RESULT=KEEPALIVE_FAILED CHECKPOINT=start_of_measured_interval ", proc.stdout)
        self.assertRegex(proc.stdout, r"last: \S", "the checkpoint must not print an empty last error")
        self.assertEqual(summary["result"], "KEEPALIVE_FAILED", summary)
        self.assertIn(expected_last_error, summary["displayWake"]["keepAliveHealth"]["lastError"])
        self.assertIsNone(summary["displayWake"]["keepAliveHealth"]["sessionLockReason"])

    def test_a_checkpoint_between_the_secure_branch_publications_prints_a_non_empty_last_error(self) -> None:
        self._assert_keepalive_failed_with_last_error(1, "ATTRCUDA_KEEPALIVE_BLOCKED reason=state_unknown ")

    def test_a_checkpoint_between_the_tick_error_branch_publications_prints_a_non_empty_last_error(self) -> None:
        self._assert_keepalive_failed_with_last_error(2, "ATTRCUDA_KEEPALIVE_STILL_RUNNING ")

    def test_a_checkpoint_between_the_catch_branch_publications_prints_a_non_empty_last_error(self) -> None:
        self._assert_keepalive_failed_with_last_error(3, "KEEPALIVE_PUBLISH_TEST_FAULT")

    def test_every_failing_branch_of_the_loop_writes_failure_count_after_its_descriptive_fields(self) -> None:
        # Static twin, branch by branch and not by count, so a branch added later is covered too: within
        # the innermost block holding each failureCount increment, a lastError write precedes it, and no
        # lastError / lastFailureUtc / sessionLockReason write follows it.
        module = MODULE.read_text(encoding="utf-8")
        loop_at = module.index("$loopScript = {")
        loop_end = module.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke", loop_at)
        # Comment-only lines are blanked (same length) so a brace in prose cannot unbalance the scan.
        loop = "\n".join("" if line.lstrip().startswith("#") else line for line in module[loop_at:loop_end].split("\n"))
        blocks: list[tuple[int, int]] = []
        stack: list[int] = []
        for at, ch in enumerate(loop):
            if ch == "{":
                stack.append(at)
            elif ch == "}":
                blocks.append((stack.pop(), at))
        increment = re.compile(r"\$NudgeState\.failureCount\s*=\s*\[int\]\$NudgeState\.failureCount\s*\+\s*1")
        descriptive = re.compile(r"\$NudgeState\.(lastError|lastFailureUtc|sessionLockReason)\s*=")
        found = list(increment.finditer(loop))
        self.assertGreaterEqual(len(found), 4, "lock, secure-screen-saver, tick-error and catch branches")
        for match in found:
            with self.subTest(line=loop.count("\n", 0, match.start()) + 1):
                start, end = min((b for b in blocks if b[0] < match.start() < b[1]), key=lambda b: b[1] - b[0])
                before = loop[start:match.start()]
                after = loop[match.end():end]
                self.assertIsNotNone(re.search(r"\$NudgeState\.lastError\s*=", before),
                                     "a lastError write must precede the failureCount increment in its branch")
                self.assertIsNone(descriptive.search(after),
                                  "failureCount must be the last descriptive publish of its branch")

    def test_the_lock_tick_publishes_its_classification_before_its_failure(self) -> None:
        # Static twin of the four runs above: in the tick's lock branch the sticky reason is written
        # before failureCount, and the health read takes failureCount before the reason.
        module = MODULE.read_text(encoding="utf-8")
        loop_at = module.index("$loopScript = {")
        branch_at = module.index("if ($tickSessionLocked -ne $false) {", loop_at)
        branch = module[branch_at:module.index("# VENUE-SESSION-LOCKED-REFUSAL-1 <<<", branch_at)]
        self.assertLess(branch.index("$NudgeState.sessionLockReason = $sessionBlockedReason"),
                        branch.index("$NudgeState.failureCount = [int]$NudgeState.failureCount + 1"))
        thread_at = module.index("if ($tickSessionLockRefused) {", loop_at)
        self.assertLess(thread_at, module.index("$NudgeState.failureCount = [int]$NudgeState.failureCount + 1", thread_at))
        health_at = module.index("function Get-AttrCudaDisplayWakeKeepAliveHealth {")
        self.assertLess(module.index("$failureCount = [int]$Handle.nudgeState.failureCount", health_at),
                        module.index("$sessionLockReason = $Handle.nudgeState.sessionLockReason", health_at))


class SessionLockedNativeLastReadShapeTests(unittest.TestCase):
    """r2 blocker 1: InputDesktopNudge.Run reads the console lock on its own thread as the last step
    before SendInput, refuses with no SendInput on locked or unknown, and the keep-alive tick re-reads
    the lock after its screen-saver probes. Static, since a real locked console cannot be produced here."""

    def setUp(self) -> None:
        self.module = MODULE.read_text(encoding="utf-8")
        start = self.module.index("public static InputDesktopNudgeResult Run(int joinTimeoutMilliseconds)")
        self.run_body = self.module[start:self.module.index("'@ -ErrorAction Stop", start)]

    def test_the_lock_read_is_the_last_read_before_send_input_and_refuses_before_it(self) -> None:
        nudge_at = self.run_body.index("INPUT[] nudge = new INPUT[]")
        read_at = self.run_body.index("bool? sessionLockedNow = NativeMethods.ReadSessionLocked();", nudge_at)
        refuse_at = self.run_body.index("if (sessionLockedNow != false)", read_at)
        return_at = self.run_body.index("return;", refuse_at)
        send_at = self.run_body.index("NativeMethods.SendInput(", read_at)
        self.assertLess(read_at, refuse_at)
        self.assertLess(return_at, send_at)
        # Nothing but the refusal sits between the read and SendInput: no other native call.
        between = self.run_body[read_at:send_at]
        self.assertEqual(between.count("NativeMethods."), 1, between)
        self.assertIn("result.SessionLockRefused = true;", between)

    def test_the_native_read_uses_the_measured_wtsinfoex_layout(self) -> None:
        start = self.module.index("public static bool? ReadSessionLocked()")
        fn = self.module[start:self.module.index("// VENUE-SESSION-LOCKED-REFUSAL-1 <<<", start)]
        for needle in ("ProcessIdToSessionId(GetCurrentProcessId(), out sessionId)",
                       "WTSQuerySessionInformationW(IntPtr.Zero, sessionId, 25, out buffer, out bytes)",
                       "Marshal.ReadInt32(buffer, 0)", "Marshal.ReadInt32(buffer, 8)", "Marshal.ReadInt32(buffer, 16)",
                       "if (level != 1 || (long)reportedSessionId != (long)sessionId) { return null; }",
                       "if (sessionFlags == 0) { return true; }", "if (sessionFlags == 1) { return false; }",
                       "WTSFreeMemory(buffer)"):
            self.assertIn(needle, fn)

    def test_the_tick_rereads_the_lock_after_its_probes_and_before_the_nudge(self) -> None:
        loop_start = self.module.index("$loopScript = {")
        loop = self.module[loop_start:self.module.index(
            "# CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke", loop_start)]
        secure_at = loop.index("$tickSecure = Invoke-AttrCudaBoundedProbe")
        reread_at = loop.index("$tickSessionLocked = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked'", secure_at)
        gate_at = loop.index("if ($tickSessionLocked -ne $false) {", reread_at)
        run_at = loop.index("[MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run(", gate_at)
        self.assertLess(secure_at, reread_at)
        self.assertLess(reread_at, gate_at)
        self.assertLess(gate_at, run_at)


@requires_pwsh
class SessionLockedNativeReadAgreesTests(_PwshCase):
    """r2: the native in-thread read and Get-AttrCudaSessionLocked agree on this host. Read-only; nothing
    is sent (Run is never called)."""

    def test_the_native_read_matches_the_powershell_read(self) -> None:
        proc = self.run_with_module(
            "[void](Register-AttrCudaDisplayWakeNativeMethods)\n"
            "$n = [MLVAppAttrCudaDisplayWake.NativeMethods]::ReadSessionLocked()\n"
            "$p = Get-AttrCudaSessionLocked\n"
            "Write-Output \"NATIVE=$($null -eq $n ? 'NULL' : $n) PS=$($null -eq $p ? 'NULL' : $p)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        m = re.search(r"NATIVE=(\w+) PS=(\w+)", proc.stdout)
        self.assertIsNotNone(m, proc.stdout)
        self.assertEqual(m.group(1), m.group(2), proc.stdout)


# --------------------------------------------------------------------------------------------
# (z) foreground verification (CUDA-PLAYBACK-PRESENT-CADENCE-2 round 1c, sol BLOCKER)
# --------------------------------------------------------------------------------------------


def _foreground_line(
    *,
    telemetry_enabled: int = 1,
    fullscreen_at_begin: int = 1,
    fullscreen_at_gate: int = 1,
    foreground_at_begin: int = 1,
    foreground_at_gate: int = 1,
    foreground_lost_count: int = 0,
    fullscreen_lost_count: int = 0,
) -> str:
    """Builds a synthetic playback_smoke.foreground log line in the exact field order/shape
    MainWindow.cpp's finishPlaybackSmokeTelemetry emits it (platform/qt/MainWindow.cpp
    ~27010-27028); viewport_at_begin/_at_gate are along for the ride (unused by the verifier)
    but included so a test line is indistinguishable from a real one."""
    return (
        "playback_smoke.foreground session=1 "
        f"telemetry_enabled={telemetry_enabled} "
        f"foreground_at_begin={foreground_at_begin} foreground_at_gate={foreground_at_gate} "
        f"foreground_lost_count={foreground_lost_count} "
        f"fullscreen_at_begin={fullscreen_at_begin} fullscreen_at_gate={fullscreen_at_gate} "
        "viewport_at_begin=1920x1080 viewport_at_gate=1920x1080 "
        f"fullscreen_lost_count={fullscreen_lost_count}"
    )


@requires_pwsh
class ForegroundVerificationTests(_PwshCase):
    """Get-AttrCudaForegroundVerification (AttrCudaArtifacts.psm1 ~4072): the DISPLAY_ASLEEP
    override's app-side gate against a backgrounded-but-fullscreen run reporting
    new_frame_swaps > 0."""

    def _verify(self, log_text: str) -> dict:
        script = self.tmp / "verify.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$log = @'\n{log_text}\n'@\n"
            "$r = Get-AttrCudaForegroundVerification -LogText $log\n"
            f"[IO.File]::WriteAllText('{(self.tmp / 'r.json')}', ($r | ConvertTo-Json -Depth 5))\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))

    def test_sol_repro_backgrounded_fullscreen_run_is_not_verified(self) -> None:
        # sol's exact repro (r1 review, PR #185): fullscreen at both begin and gate, zero
        # losses, but the window was never in the foreground at either sample. Pre-fix: the
        # verifier never looked at foreground_at_begin/_at_gate at all, only the *_lost_count
        # deltas -- a run that starts AND ends backgrounded never "loses" foreground in
        # between, so this read verified=true and could feed the DISPLAY_ASLEEP override.
        result = self._verify(_foreground_line(
            fullscreen_at_begin=1, fullscreen_at_gate=1,
            foreground_at_begin=0, foreground_at_gate=0,
            foreground_lost_count=0, fullscreen_lost_count=0,
        ))
        self.assertIs(result["verified"], False)

    def test_all_good_line_is_verified(self) -> None:
        # The genuine positive: fullscreen and foreground at both begin and gate, zero losses.
        result = self._verify(_foreground_line(
            fullscreen_at_begin=1, fullscreen_at_gate=1,
            foreground_at_begin=1, foreground_at_gate=1,
            foreground_lost_count=0, fullscreen_lost_count=0,
        ))
        self.assertIs(result["verified"], True)
        self.assertIsNone(result["reason"])

    def test_foreground_lost_at_begin_only_is_not_verified(self) -> None:
        # Started backgrounded, ended in the foreground: foreground_at_begin=0 alone must
        # still fail closed even though foreground_at_gate=1 and the lost-count deltas read 0.
        result = self._verify(_foreground_line(
            fullscreen_at_begin=1, fullscreen_at_gate=1,
            foreground_at_begin=0, foreground_at_gate=1,
            foreground_lost_count=0, fullscreen_lost_count=0,
        ))
        self.assertIs(result["verified"], False)

    def test_missing_foreground_fields_is_unusable_not_verified(self) -> None:
        # A line from before this field existed (or any other malformed/partial line): missing
        # required fields must read as unusable, never as passing evidence.
        result = self._verify(
            "playback_smoke.foreground session=1 telemetry_enabled=1 "
            "fullscreen_at_begin=1 fullscreen_at_gate=1 "
            "foreground_lost_count=0 fullscreen_lost_count=0"
        )
        self.assertIs(result["verified"], False)
        self.assertIn("missing/unparseable", result["reason"])


if __name__ == "__main__":
    unittest.main()
