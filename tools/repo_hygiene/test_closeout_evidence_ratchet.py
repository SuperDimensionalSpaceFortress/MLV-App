"""Validates the closeout capability ledger against its schema.

.closeout-evidence/ stays tracked and has no growth ceiling, only a floor; see
docs/closeout-evidence-policy.md. NA-2 denies deleting or moving it.
"""

import json
import subprocess
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_json(relative_path):
    return json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


# Tracked work-block directories at 18a9c60d, re-derive with:
#   git ls-tree -r --name-only <sha> -- .closeout-evidence | cut -d/ -f2 | sort -u | wc -l
# A FLOOR, never a ceiling: closeout adds a directory per finalized work block.
EVIDENCE_DIR_FLOOR = 617


class CloseoutEvidenceFloorTest(unittest.TestCase):
    def test_tracked_evidence_directories_never_fall_below_the_floor(self):
        # The TREE at HEAD, not the index (git ls-files reads the moving index).
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-tree", "-r", "--name-only", "HEAD", "--", ".closeout-evidence"],
            capture_output=True, text=True, check=True,
        )
        dirs = {line.split("/")[1] for line in result.stdout.splitlines() if line.count("/") >= 2}
        self.assertGreaterEqual(
            len(dirs), EVIDENCE_DIR_FLOOR,
            "tracked .closeout-evidence work-block directories fell below the floor; NA-2 is archive-never-delete "
            "(docs/closeout-evidence-policy.md)",
        )


class CloseoutCapabilityLedgerSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = _load_json("CLOSEOUT-CAPABILITY-LEDGER.schema.json")
        self.ledger = _load_json("CLOSEOUT-CAPABILITY-LEDGER.json")
        self.validator = Draft202012Validator(self.schema)

    def test_ledger_validates_against_schema(self):
        self.validator.validate(self.ledger)

    def test_ledger_declares_stale_status(self):
        self.assertEqual(self.ledger["status"], "STALE-2026-05-08")

    def test_schema_still_rejects_unknown_top_level_keys(self):
        hostile = dict(self.ledger)
        hostile["unexpectedTopLevelKey"] = True
        errors = list(self.validator.iter_errors(hostile))
        self.assertTrue(errors)
        additional_properties_errors = [
            error for error in errors if error.validator == "additionalProperties"
        ]
        self.assertTrue(
            additional_properties_errors,
            "Expected an additionalProperties violation, got: "
            f"{[error.validator for error in errors]}",
        )
        self.assertTrue(
            any(
                "unexpectedTopLevelKey" in error.message
                for error in additional_properties_errors
            ),
            "Expected the additionalProperties error to name "
            f"'unexpectedTopLevelKey', got messages: "
            f"{[error.message for error in additional_properties_errors]}",
        )


if __name__ == "__main__":
    unittest.main()
