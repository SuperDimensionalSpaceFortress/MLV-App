"""Fail-closed tests for get_doctrine_brief.py (fixture + REFUSED path)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve().parent / "get_doctrine_brief.py"
# A fixture is honoured only inside this directory (and only under pytest), so every
# throwaway fixture tree is created here and removed by TemporaryDirectory.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CANDIDATE_ZERO = "agent-bridge-sot-suspend-mlv-in-tree-20260909.md"


def _run(*args: str, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        env=env,
    )


def _fixture_tmp():
    return tempfile.TemporaryDirectory(prefix="_pytest-tmp-", dir=str(FIXTURES_DIR))


def _outside_pytest_env():
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _write_fixture(root: Path, *, with_candidate_zero: bool = True) -> Path:
    (root / "ruling-candidates").mkdir(parents=True)
    (root / "specs").mkdir(parents=True)
    (root / "RULINGS.md").write_text(
        "# Rulings\n\n- Pull is the only direction.\n- Doctrine repo is the bus.\n",
        encoding="utf-8",
    )
    (root / "specs" / "mlv-app.md").write_text(
        "# MLV-App factory spec\n\nLanes are PROCESSES, not seats.\n",
        encoding="utf-8",
    )
    (root / "ruling-candidates" / "unrelated-other-project-r1.md").write_text(
        "# Other project candidate\n\nNo MLV content here.\n",
        encoding="utf-8",
    )
    (root / "ruling-candidates" / "mlv-fable-opus-model-failback-r1.md").write_text(
        "# MLV fable failback\n\nCANDIDATE for MLV-App model failback.\n",
        encoding="utf-8",
    )
    if with_candidate_zero:
        (root / "ruling-candidates" / CANDIDATE_ZERO).write_text(
            "# Agent Bridge SoT: suspend MLV in-tree package (2026-09-09)\n\n"
            "**CANDIDATE_ZERO_AUTHORITY**: no runtime activation.\n\n"
            "- Source of truth: layibabalola/agent-bridge\n"
            "- Suspend tools/agent-bridge/ in MLV-App\n",
            encoding="utf-8",
        )
    return root


class GetDoctrineBriefTests(unittest.TestCase):
    def test_fixture_brief_includes_candidate_zero_and_sot_strings(self):
        with _fixture_tmp() as tmp:
            root = _write_fixture(Path(tmp))
            result = _run("--fixture-root", str(root))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            out = result.stdout
            self.assertIn("CANDIDATE_ZERO_AUTHORITY", out)
            self.assertIn(CANDIDATE_ZERO, out)
            self.assertIn("layibabalola/agent-bridge", out)
            self.assertIn("tools/agent-bridge", out)
            self.assertIn("rulingsSha256:", out)
            self.assertIn("mlvSpecSha256:", out)
            self.assertIn("busHead:", out)
            self.assertIn("candidateZeroPresent: true", out)
            # Unrelated non-MLV candidate should not be forced in by name alone
            # (mlv-fable should appear; unrelated-other should not).
            self.assertIn("mlv-fable-opus-model-failback-r1.md", out)
            self.assertNotIn("unrelated-other-project-r1.md", out)

    def test_fixture_missing_required_file_refuses(self):
        with _fixture_tmp() as tmp:
            root = Path(tmp)
            (root / "ruling-candidates").mkdir()
            # Missing RULINGS.md and specs/mlv-app.md
            result = _run("--fixture-root", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertTrue(
                result.stdout.strip().startswith("REFUSED:"),
                result.stdout,
            )

    def test_gh_failure_refuses_when_no_fixture(self):
        # Force gh to fail by pointing at a nonsense repo; no fixture.
        env = os.environ.copy()
        env.pop("MLV_DOCTRINE_FIXTURE_ROOT", None)
        result = _run(
            "--repo",
            "layibabalola/this-repo-does-not-exist-doctrine-brief-test",
            "--ref",
            "master",
            "--no-candidate-fallback",
            env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stdout.strip().startswith("REFUSED:"), result.stdout)

    def test_candidate_zero_label_even_when_only_mlv_relevant_filter(self):
        with _fixture_tmp() as tmp:
            root = _write_fixture(Path(tmp), with_candidate_zero=True)
            result = _run("--fixture-root", str(root))
            self.assertEqual(result.returncode, 0, result.stdout)
            # Machine field + human label both present.
            self.assertIn("**CANDIDATE_ZERO_AUTHORITY**", result.stdout)
            self.assertIn("SoT pointer present: `layibabalola/agent-bridge`", result.stdout)


    def test_fixture_cos_feedback_included_when_present(self):
        with _fixture_tmp() as tmp:
            root = _write_fixture(Path(tmp))
            cos = root / "cos-feedback" / "mlv-app"
            cos.mkdir(parents=True)
            (cos / "pr-99.md").write_text(
                "schema: cos-feedback.v1\nproject: mlv-app\npr: 99\n\n"
                "## Blockers\n\n- Example blocker.\n\n## Improvements\n\n- Example improvement.\n",
                encoding="utf-8",
            )
            (cos / "README.md").write_text("# skip me\n", encoding="utf-8")
            result = _run("--fixture-root", str(root))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CoS feedback (data only, zero authority)", result.stdout)
            self.assertIn("cos-feedback/mlv-app/pr-99.md", result.stdout)
            self.assertIn("Example blocker", result.stdout)
            self.assertNotIn("README.md", result.stdout.split("CoS feedback")[-1])

    def test_fixture_missing_cos_feedback_omits_section_does_not_refuse(self):
        with _fixture_tmp() as tmp:
            root = _write_fixture(Path(tmp))
            # No cos-feedback/ dir at all
            result = _run("--fixture-root", str(root))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("### CoS feedback", result.stdout)


COMPOSE_CLI = Path(__file__).resolve().parent / "Compose-LanePrompt.ps1"
OFFLINE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "doctrine-offline"


def _compose_fields(tmp: Path, env):
    proc = tmp / "fields-DOCTRINE-FC-1.md"
    proc.write_text(
        "# FIELDS for DOCTRINE-FC-1\nCARD_ID: DOCTRINE-FC-1\nPRIORITY: 1\nCLIP_OR_NONE: none\n"
        "ALLOWED_PATHS: some/path.cpp\nDELIVERABLE: do the thing\nACCEPTANCE: run the test\n"
        "VERIFY_FIRST: check first\n",
        encoding="ascii",
    )
    return subprocess.run(
        ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(COMPOSE_CLI), "-ProcedurePath", str(proc), "-WorkDir", "C:\\mlvtmp\\lane-x",
         "-BaseSha", "a" * 40, "-RunDir", str(tmp / "run"), "-Ts", "20260101T000000Z",
         "-GhCapability", "no-pr-capability"],
        text=True, capture_output=True, env=env,
    )


class ComposeDoctrineFailClosedTests(unittest.TestCase):
    """The offline fixture keeps unrelated dispatch tests hermetic; it must not make the
    composer fail open. A brief that cannot be built still REFUSES an implementer prompt."""

    def test_compose_injects_the_offline_fixture_brief(self):
        env = os.environ.copy()
        env["MLV_DOCTRINE_FIXTURE_ROOT"] = str(OFFLINE_FIXTURE)
        with _fixture_tmp() as tmp:
            result = _compose_fields(Path(tmp), env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("## Doctrine brief (injected; do not fetch bus)", result.stdout)
        self.assertIn("busHead: fixture:", result.stdout)
        self.assertNotIn("{{DOCTRINE_BRIEF}}", result.stdout)

    def test_compose_refuses_when_the_brief_cannot_be_built(self):
        env = os.environ.copy()
        with _fixture_tmp() as tmp:
            empty = Path(tmp) / "empty-bus"
            empty.mkdir()
            env["MLV_DOCTRINE_FIXTURE_ROOT"] = str(empty)
            result = _compose_fields(Path(tmp), env)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertTrue(
            result.stdout.startswith("REFUSED: doctrine-brief-failed"), result.stdout
        )

    def test_compose_records_fixture_provenance_in_the_prompt(self):
        env = os.environ.copy()
        env["MLV_DOCTRINE_FIXTURE_ROOT"] = str(OFFLINE_FIXTURE)
        with _fixture_tmp() as tmp:
            result = _compose_fields(Path(tmp), env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("provenance: fixture", result.stdout)
        self.assertIn("- provenance: `fixture` -- OFFLINE TEST FIXTURE", result.stdout)
        self.assertNotIn("provenance: live", result.stdout)


REAL_FIELDS_CARD = ROOT / "docs" / "lane-prompts" / "v2" / "fields-PROD-ENVFLAG-1.md"


def _compose_real_card(tmp: Path, env, *extra: str):
    return subprocess.run(
        ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(COMPOSE_CLI), "-ProcedurePath", str(REAL_FIELDS_CARD),
         "-WorkDir", "C:\\mlvtmp\\lane-x", "-BaseSha", "a" * 40, "-RunDir", str(tmp / "run"),
         "-Ts", "20260101T000000Z", "-GhCapability", "no-pr-capability", *extra],
        text=True, capture_output=True, env=env,
    )


class NonLiveDoctrineRefusedOutsidePytestTests(unittest.TestCase):
    """sol r1 blocker on PR #107: outside pytest, production composition must carry the live
    fetch or refuse. "Outside pytest" is simulated by removing PYTEST_CURRENT_TEST from the
    child environment - that variable is the gate, and pytest sets it for every test."""

    def test_repro_a_fixture_env_with_bogus_repo_refuses_outside_pytest(self):
        env = _outside_pytest_env()
        env["MLV_DOCTRINE_FIXTURE_ROOT"] = str(OFFLINE_FIXTURE)
        env["MLV_DOCTRINE_REPO"] = "layibabalola/this-repo-does-not-exist-doctrine-brief-test"
        with tempfile.TemporaryDirectory(prefix="mlv-doctrine-") as tmp:
            result = _compose_real_card(Path(tmp), env)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("REFUSED: doctrine-brief-failed"), result.stdout)
        self.assertIn("doctrine-fixture-refused", result.stdout)
        self.assertNotIn("OFFLINE TEST FIXTURE", result.stdout)

    def test_repro_b_supplied_brief_text_is_not_accepted(self):
        env = _outside_pytest_env()
        env.pop("MLV_DOCTRINE_FIXTURE_ROOT", None)
        with tempfile.TemporaryDirectory(prefix="mlv-doctrine-") as tmp:
            result = _compose_real_card(
                Path(tmp), env, "-DoctrineBrief", "FAKE_DOCTRINE_WITHOUT_FETCH"
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAKE_DOCTRINE_WITHOUT_FETCH", result.stdout)
        self.assertNotIn("COMPOSED:", result.stdout)

    def test_getter_refuses_the_in_repo_fixture_outside_pytest(self):
        result = _run("--fixture-root", str(OFFLINE_FIXTURE), env=_outside_pytest_env())
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(
            result.stdout.startswith("REFUSED: doctrine-fixture-refused"), result.stdout
        )

    def test_getter_refuses_a_fixture_outside_the_fixtures_dir_even_under_pytest(self):
        with tempfile.TemporaryDirectory(prefix="mlv-doctrine-") as tmp:
            root = _write_fixture(Path(tmp))
            result = _run("--fixture-root", str(root))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(
            result.stdout.startswith("REFUSED: doctrine-fixture-refused"), result.stdout
        )
        self.assertIn("is not inside", result.stdout)


class LiveBriefProvenanceTests(unittest.TestCase):
    """The live path (gh stubbed in-process, no network) records repo, ref and busHead sha."""

    def test_live_brief_records_live_provenance(self):
        import base64
        import importlib.util

        spec = importlib.util.spec_from_file_location("_gdb_under_test", str(SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        head = "c0ffee" * 6 + "abcd"

        def blob(text):
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}

        def fake_gh_api(path):
            if "/git/ref/heads/" in path:
                return {"object": {"sha": head}}
            if "/contents/RULINGS.md" in path:
                return blob("# Rulings\n")
            if "/contents/specs/mlv-app.md" in path:
                return blob("# spec\n")
            if "/contents/ruling-candidates?" in path:
                return []
            raise RuntimeError("gh-api-failed: %s" % path)

        mod._gh_api = fake_gh_api
        brief = mod.build_brief("example/doctrine", "master")
        self.assertIn("provenance: live", brief)
        self.assertIn(
            "- provenance: `live` -- fetched read-only from `example/doctrine` at ref "
            "`master`, busHead `%s`" % head,
            brief,
        )
        self.assertNotIn("fixture", brief.split("### RULINGS.md")[0])


if __name__ == "__main__":
    unittest.main()
