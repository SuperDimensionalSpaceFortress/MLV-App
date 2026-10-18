"""No tracked tooling file may use the pre-transfer repository slug, except the named records.

The owner transferred the repository on 2026-10-04 (the old owner's MLV-App ->
SuperDimensionalSpaceFortress/MLV-App; repository id 1169162800). The old name keeps resolving only
because GitHub redirects it, and the redirect ends the day any account creates a repository at that
name. A live caller (``gh pr create -R``, ``gh api repos/...``, a curl API query, the prompt every
lane carries, a remote-URL check) must carry the new slug. The identity of RECORDED data (approvals,
receipts, pinned-target policy) is matched against both names through the one alias set in
tools/repo_hygiene/repository_identity.py.

The scan is case-insensitive (one gate once hid its slug in lower case) and covers every root that
holds executable or prompt text: tools/, tests/, agents/, .github/ and docs/lane-prompts/.

Three kinds of file may still name the old slug, each listed EXPLICITLY with its reason:

* ``ALIAS_DEFINITION``: the one file that spells the alias set.
* ``HISTORICAL``: a dated record, a schema identifier, or a fixture of a recorded approval. The
  validators of these records accept both names (see repository_identity.py).
* ``LIVE_DEFERRED``: a LIVE use that this change could not edit. Today that is the receipt-URL prefix and
  the protected-URL pattern of tools/hooks/mlv-never-authorized.py plus the test that mirrors it:
  lanes never edit tools/hooks/* (NA-10), so the hub files that as its own hub-at-board-venue card.
  This table may only shrink: it is pinned to exactly that entry below.

The allowlist is checked in both directions: an unlisted file naming the old slug fails, and a listed
file that no longer names it fails too. The token is assembled from two halves so this file never
matches its own scan.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OLD_SLUG = "layibabalola" + "/MLV-App"
SCAN_ROOTS = ("tools", "tests", "agents", ".github", "docs/lane-prompts")

_HISTORICAL_CARD = (
    "historical: prompt of a delivered card (a dated record, not composed into any live prompt)"
)

ALIAS_DEFINITION: dict[str, str] = {
    "tools/repo_hygiene/repository_identity.py": "the one alias set (both names of repository id 1169162800)",
}

HISTORICAL: dict[str, str] = {
    "tests/fixtures/golden/pipeline_hashes.provenance.json": "URL of a past Actions run",
    "tools/gates/payload-provenance-promotion-receipt.schema.json": "schema $id URL; an identifier, not a call",
    "tools/repo_hygiene/pipeline_golden_provenance.py": "the recorded 2026-08-19 owner approval claim, kept byte-exact",
    "tools/repo_hygiene/test_pipeline_golden_provenance.py": "fixtures of recorded approvals that must stay accepted",
    "tools/repo_hygiene/test_vendored_native_payloads.py": "fixtures of recorded approvals that must stay accepted",
    "tools/repo_hygiene/test_readme_fork_banner.py": "pins the root README wording, which this card did not edit",
    "tools/repo_hygiene/test_repo_hygiene.py": "pins the root SUPPORT/SECURITY wording, which this card did not edit",
    "docs/lane-prompts/v2/card-HYG-PROFILING-DNGS-1.md": _HISTORICAL_CARD,
    "docs/lane-prompts/v2/card-PROD-CLAUDEMD-TRUTH.md": _HISTORICAL_CARD,
    "docs/lane-prompts/v2/card-PROD-TLS-VERIFY-1.md": _HISTORICAL_CARD,
    "docs/lane-prompts/v2/card-TOOL-GH-PROBE-1.md": _HISTORICAL_CARD,
    "docs/lane-prompts/v2/card-TOOL-HOOK-ENFORCE-1.md": _HISTORICAL_CARD + "; quotes the gate's protected-URL pattern",
    "docs/lane-prompts/v2/card-TOOL-LOOP-PLUMBING-1.md": _HISTORICAL_CARD,
    "docs/lane-prompts/v2/fields-PROD-README-FORK-1.md": _HISTORICAL_CARD + "; greps the root README, which still names it",
    "docs/lane-prompts/v2/fields-PROD-TOOLCHAIN-1.md": _HISTORICAL_CARD,
    "docs/lane-prompts/v2/ratify-plan-OPUS.md": "historical: plan-ratification prompt of 2026-09",
    "docs/lane-prompts/v2/ratify-plan-SOL.md": "historical: plan-ratification prompt of 2026-09",
    # HUB-TOOLS-TRACKED-1: the CoS open-ask scanner reads RECORDED bus headers (cos-feedback/mlv-app/pr-N.md) that were written
    # before the transfer and still carry `repo: <old slug>`. Matching that name is its job (-RepoAlias), so the old slug IS the data
    # here, exactly like repository_identity.py; it is never used to call GitHub (the scan passes -Repo, the new slug, to gh).
    "tools/coordination/hub/cos-open-ask-scan.ps1": "recorded bus headers still carry the old repo name: the default -RepoAlias that keeps them scannable (gh is called with the new -Repo)",
    "tools/coordination/hub/hub-profile.json": "repoAliases: the former repo name that recorded bus headers carry; ownerLogin/repoSlug name the live repo",
    "tools/coordination/hub/Test-CosOpenAskScan.ps1": "fixtures of legacy bus headers (repo: <old slug>) that must stay scannable, plus the case-insensitive variant",
    "tools/coordination/hub/cos-scan-fixtures-bus/pr-413.md": "verbatim copy of a recorded bus feedback file (the ledger's 14:28Z false-positive set); editing it would void the verbatim claim",
    "tests/coordination/test_hub_cos_open_ask_scan.py": "passes the former repo name as -RepoAlias / header fixture to prove legacy bus headers still scan",
}

_HOOK_DEFERRAL = (
    "LIVE, DEFERRED (NA-10: a lane never edits tools/hooks/*): the receipt-URL prefix and the "
    "protected-URL pattern of the authorization gate; hub-at-board-venue card"
)
LIVE_DEFERRED: dict[str, str] = {
    "tools/hooks/mlv-never-authorized.py": _HOOK_DEFERRAL,
    "tools/repo_hygiene/test_mlv_never_authorized.py": "mirrors tools/hooks/mlv-never-authorized.py; moves with it",
}

ALLOWED_OLD_SLUG: dict[str, str] = {**ALIAS_DEFINITION, **HISTORICAL, **LIVE_DEFERRED}


def tracked_files(repo_root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", *SCAN_ROOTS],
        check=True, capture_output=True,
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def files_naming_old_slug(repo_root: Path, names: list[str]) -> list[str]:
    needle = OLD_SLUG.lower().encode("ascii")
    hits = []
    for name in names:
        path = repo_root / name
        if path.is_file() and needle in path.read_bytes().lower():
            hits.append(name)
    return sorted(hits)


def files_naming_old_slug_at_ref(repo_root: Path, ref: str) -> list[str]:
    """The same scan against a git ref's blobs, with no checkout (used to measure RED on a base)."""

    completed = subprocess.run(
        ["git", "-C", str(repo_root), "grep", "-i", "-l", "--fixed-strings", "-e", OLD_SLUG, ref, "--", *SCAN_ROOTS],
        capture_output=True,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(completed.stderr.decode("utf-8", "replace"))
    prefix = ref + ":"
    return sorted(line[len(prefix):] for line in completed.stdout.decode("utf-8").splitlines() if line.startswith(prefix))


class RepoSlugHygieneTests(unittest.TestCase):
    def test_no_unlisted_tracked_file_names_the_old_slug(self):
        hits = files_naming_old_slug(REPO_ROOT, tracked_files())
        unlisted = [name for name in hits if name not in ALLOWED_OLD_SLUG]
        self.assertEqual(
            [], unlisted,
            "these files name the pre-transfer repository slug; repoint live callers to "
            "SuperDimensionalSpaceFortress/MLV-App (tools/coordination/repo-slug.ps1 for PowerShell, "
            "tools/repo_hygiene/repository_identity.py for Python) or, for a record that must keep it, "
            "add the path to HISTORICAL with the reason",
        )

    def test_allowlist_has_no_stale_entries(self):
        hits = set(files_naming_old_slug(REPO_ROOT, sorted(ALLOWED_OLD_SLUG)))
        stale = sorted(name for name in ALLOWED_OLD_SLUG if name not in hits)
        self.assertEqual([], stale, "allowlisted files that no longer name the old slug: remove them from the allowlist")

    def test_live_deferred_table_is_pinned_to_the_one_hook_deferral(self):
        # A new live use must be FIXED, not parked here: this table may only shrink.
        self.assertEqual(
            {"tools/hooks/mlv-never-authorized.py", "tools/repo_hygiene/test_mlv_never_authorized.py"},
            set(LIVE_DEFERRED),
        )

    def test_scan_detects_a_live_hit_in_any_case_and_ignores_a_clean_file(self):
        # Falsification of the scanner itself, on a throwaway tree.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mlv-slug-scan-") as tmp:
            root = Path(tmp)
            (root / "live.ps1").write_text("gh pr create -R " + OLD_SLUG + " --head x\n", encoding="ascii")
            (root / "lower.py").write_text("urls = {'https://github.com/" + OLD_SLUG.lower() + "'}\n", encoding="ascii")
            (root / "clean.ps1").write_text("gh pr create -R SuperDimensionalSpaceFortress/MLV-App\n", encoding="ascii")
            self.assertEqual(
                ["live.ps1", "lower.py"],
                files_naming_old_slug(root, ["live.ps1", "lower.py", "clean.ps1", "absent.ps1"]),
            )

    def test_scan_roots_cover_agents_github_and_lane_prompts(self):
        for root in ("agents", ".github", "docs/lane-prompts"):
            self.assertIn(root, SCAN_ROOTS)
        names = tracked_files()
        self.assertTrue(any(name.startswith("agents/") for name in names))
        self.assertTrue(any(name.startswith(".github/") for name in names))
        self.assertTrue(any(name.startswith("docs/lane-prompts/") for name in names))

    def test_the_shared_definitions_name_the_new_slug(self):
        text = (REPO_ROOT / "tools" / "coordination" / "repo-slug.ps1").read_text(encoding="ascii")
        self.assertIn("return 'SuperDimensionalSpaceFortress/MLV-App'", text)
        self.assertNotIn(OLD_SLUG, text)
        from tools.repo_hygiene.repository_identity import CANONICAL_REPOSITORY, REPOSITORY_ID, REPOSITORY_SLUG_ALIASES

        self.assertEqual("SuperDimensionalSpaceFortress/MLV-App", CANONICAL_REPOSITORY)
        self.assertEqual(1169162800, REPOSITORY_ID)
        self.assertEqual({OLD_SLUG, CANONICAL_REPOSITORY}, set(REPOSITORY_SLUG_ALIASES))


if __name__ == "__main__":
    unittest.main()
