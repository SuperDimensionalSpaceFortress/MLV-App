"""NA4-OWNER-CONSENTED-FOOTAGE-1 round 2: the shared content verifier.

Every row uses SYNTHETIC temp files and a fixture table; no row reads real footage.  The rows
about the default table only assert that it IS the hook's frozen table, and use an id it lacks.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERIFIER = os.path.join(REPO_ROOT, "tools", "gates", "verify_consented_footage.py")
HOOK = os.path.join(REPO_ROOT, "tools", "hooks", "mlv-never-authorized.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifyConsentedFootageTests(unittest.TestCase):
    def setUp(self):
        self.verifier = _load(VERIFIER, "_verify_consented_footage_under_test")
        self.tmp = tempfile.mkdtemp(prefix="mlv-na4v-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.contents = (b"synthetic part zero " * 97, b"synthetic part one")
        self.parts = []
        for index, content in enumerate(self.contents):
            path = os.path.join(self.tmp, "clip-fixture.part%d" % index)
            with open(path, "wb") as handle:
                handle.write(content)
            self.parts.append(path)
        self.table = {
            "clips": {
                "FIX-0001": tuple(
                    (len(content), hashlib.sha256(content).hexdigest().upper(), "0" * 64)
                    for content in self.contents
                )
            }
        }

    def _rewrite(self, index, content):
        with open(self.parts[index], "wb") as handle:
            handle.write(content)

    def test_pass_when_every_part_matches(self):
        result = self.verifier.verify("FIX-0001", self.parts, table=self.table)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, self.verifier.PASS)
        self.assertEqual([part.status for part in result.parts], ["PASS", "PASS"])

    def test_streaming_hash_across_many_chunks(self):
        saved = self.verifier.CHUNK_BYTES
        self.verifier.CHUNK_BYTES = 7
        try:
            result = self.verifier.verify("FIX-0001", self.parts, table=self.table)
        finally:
            self.verifier.CHUNK_BYTES = saved
        self.assertTrue(result.ok, result)

    def test_length_mismatch(self):
        self._rewrite(1, self.contents[1] + b"!")
        result = self.verifier.verify("FIX-0001", self.parts, table=self.table)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, self.verifier.LENGTH_MISMATCH)
        self.assertEqual(result.parts[1].actual_length, len(self.contents[1]) + 1)
        self.assertIsNone(result.parts[1].actual_sha256, "a length mismatch is not hashed")

    def test_sha256_mismatch_at_the_same_length(self):
        swapped = bytes(reversed(self.contents[1]))
        self.assertEqual(len(swapped), len(self.contents[1]))
        self._rewrite(1, swapped)
        result = self.verifier.verify("FIX-0001", self.parts, table=self.table)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, self.verifier.SHA256_MISMATCH)
        self.assertEqual(result.parts[0].status, self.verifier.PASS)

    def test_missing_part(self):
        os.remove(self.parts[0])
        result = self.verifier.verify("FIX-0001", self.parts, table=self.table)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, self.verifier.MISSING)

    def test_parts_out_of_order_fail(self):
        result = self.verifier.verify("FIX-0001", list(reversed(self.parts)), table=self.table)
        self.assertFalse(result.ok)

    def test_unknown_id_and_wrong_part_count(self):
        unknown = self.verifier.verify("M02-1344", self.parts, table=self.table)
        self.assertEqual((unknown.ok, unknown.status), (False, self.verifier.UNKNOWN_ID))
        short = self.verifier.verify("FIX-0001", self.parts[:1], table=self.table)
        self.assertEqual((short.ok, short.status), (False, self.verifier.PART_COUNT_MISMATCH))

    def test_default_table_is_the_hooks_frozen_table(self):
        hook = _load(HOOK, "_mlv_never_authorized_for_verifier_test")
        table = self.verifier.load_consent_table()
        self.assertEqual(dict(table["clips"]), dict(hook.OWNER_CONSENTED_FOOTAGE["clips"]))
        self.assertNotIn("M02-1344", table["clips"])

    def test_cli_exit_codes_and_json_without_paths(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.verifier.main(["--clip-id", "M02-1344", "--part", self.parts[0]])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "UNKNOWN_ID")
        self.assertFalse(payload["ok"])
        self.assertNotIn(os.path.basename(self.parts[0]), out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.verifier.main(["--clip-id", "FIX-0001"]), 2)

    def test_result_json_never_echoes_a_part_path(self):
        result = self.verifier.verify("FIX-0001", self.parts, table=self.table)
        text = json.dumps(result.to_json())
        self.assertNotIn("clip-fixture", text)


if __name__ == "__main__":
    unittest.main()
