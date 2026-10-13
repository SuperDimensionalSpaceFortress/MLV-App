"""No tracked file under tools/ or tests/ may name the pre-transfer repository slug.

The owner transferred the repository on 2026-10-04 (the old owner's MLV-App ->
SuperDimensionalSpaceFortress/MLV-App). The old name keeps resolving only because GitHub redirects
it, and the redirect ends the day any account creates a repository at that name. Live callers
(``gh pr create -R``, ``gh api repos/...``, the prompt every lane carries) must therefore carry the
new slug; tools/coordination/repo-slug.ps1 is the one shared definition.

``ALLOWED_OLD_SLUG`` is the EXPLICIT allowlist of tracked files that still name the old slug, each
with the reason. It is checked in both directions: a new file that names the old slug fails, and an
allowlisted file that no longer names it fails too, so the list can only shrink. Every entry in the
"governed identity" group is a deferred follow-up, not an endorsement: those files compare the slug
to recorded receipts or to closeout.config.json, so moving them is a coordinated change to the
closeout config and the registered approvals.

The token is assembled from two halves below so this file never matches its own scan.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OLD_SLUG = "layibabalola" + "/MLV-App"

_GOVERNED = (
    "governed identity pin: compared with closeout.config.json providerRepository and recorded "
    "approvals; moves together with them (follow-up card)"
)
_HISTORICAL = "historical record or data quoting a past run, PR comment or published URL"
_DOC_PIN = "pins the root README/SUPPORT/SECURITY wording, which this card did not edit"

ALLOWED_OLD_SLUG: dict[str, str] = {
    "tests/fixtures/golden/pipeline_hashes.provenance.json": _HISTORICAL,
    "tools/gates/payload-provenance-promotion-receipt.schema.json": "schema $id URL; an identifier, not a call",
    "tools/hooks/mlv-never-authorized.py": "security gate: receipt-URL prefix; loosening a gate is an owner act (follow-up)",
    "tools/repo_hygiene/test_mlv_never_authorized.py": "mirrors tools/hooks/mlv-never-authorized.py",
    "tools/repo_hygiene/brokered_closeout.py": _GOVERNED,
    "tools/repo_hygiene/candidate_acceptance.py": _GOVERNED,
    "tools/repo_hygiene/test_candidate_acceptance.py": _GOVERNED,
    "tools/repo_hygiene/vendored_native_payloads.py": _GOVERNED,
    "tools/repo_hygiene/test_vendored_native_payloads.py": _GOVERNED,
    "tools/repo_hygiene/pipeline_golden_provenance.py": _GOVERNED,
    "tools/repo_hygiene/test_pipeline_golden_provenance.py": _GOVERNED,
    "tools/repo_hygiene/test_readme_fork_banner.py": _DOC_PIN,
    "tools/repo_hygiene/test_repo_hygiene.py": _DOC_PIN,
}


def tracked_files(repo_root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "tools", "tests"],
        check=True, capture_output=True,
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def files_naming_old_slug(repo_root: Path, names: list[str]) -> list[str]:
    needle = OLD_SLUG.encode("ascii")
    hits = []
    for name in names:
        path = repo_root / name
        if path.is_file() and needle in path.read_bytes():
            hits.append(name)
    return sorted(hits)


class RepoSlugHygieneTests(unittest.TestCase):
    def test_no_unlisted_tracked_file_names_the_old_slug(self):
        hits = files_naming_old_slug(REPO_ROOT, tracked_files())
        unlisted = [name for name in hits if name not in ALLOWED_OLD_SLUG]
        self.assertEqual(
            [], unlisted,
            "these files name the pre-transfer repository slug; repoint them to "
            "SuperDimensionalSpaceFortress/MLV-App (tools/coordination/repo-slug.ps1 is the shared "
            "definition for PowerShell callers) or, for a record that must keep it, add the path to "
            "ALLOWED_OLD_SLUG with the reason",
        )

    def test_allowlist_has_no_stale_entries(self):
        hits = set(files_naming_old_slug(REPO_ROOT, sorted(ALLOWED_OLD_SLUG)))
        stale = sorted(name for name in ALLOWED_OLD_SLUG if name not in hits)
        self.assertEqual([], stale, "allowlisted files that no longer name the old slug: remove them from ALLOWED_OLD_SLUG")

    def test_scan_detects_a_live_hit_and_ignores_a_clean_file(self):
        # Falsification of the scanner itself, on a throwaway tree.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mlv-slug-scan-") as tmp:
            root = Path(tmp)
            (root / "live.ps1").write_text("gh pr create -R " + OLD_SLUG + " --head x\n", encoding="ascii")
            (root / "clean.ps1").write_text("gh pr create -R SuperDimensionalSpaceFortress/MLV-App\n", encoding="ascii")
            self.assertEqual(["live.ps1"], files_naming_old_slug(root, ["live.ps1", "clean.ps1", "absent.ps1"]))

    def test_the_shared_definition_names_the_new_slug(self):
        text = (REPO_ROOT / "tools" / "coordination" / "repo-slug.ps1").read_text(encoding="ascii")
        self.assertIn("return 'SuperDimensionalSpaceFortress/MLV-App'", text)
        self.assertNotIn(OLD_SLUG, text)


if __name__ == "__main__":
    unittest.main()
