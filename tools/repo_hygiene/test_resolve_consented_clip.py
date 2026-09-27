"""ATTR3-FOOTAGE-BIND-1 PR-A: the id-only, content-bound clip resolver.

Every row uses a SYNTHETIC spec and a SYNTHETIC consent table (``spec_bytes=``/``table=``); no
row reads the real repo's spec or the hook's real table except the two rows that explicitly say
so.  Synthetic paths carry a distinctive token so the path-free-output rows can assert it never
reaches stdout or stderr.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESOLVER = os.path.join(REPO_ROOT, "tools", "gates", "resolve_consented_clip.py")
HOOK = os.path.join(REPO_ROOT, "tools", "hooks", "mlv-never-authorized.py")

TOKEN = "ZZSYNTHETICPATHTOKENZZ"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProc:
    """Stands in for ``subprocess.Popen`` with a fixed, already-known stdout/stderr payload."""

    def __init__(self, stdout_bytes, stderr_bytes=b"", returncode=0):
        self.stdout = io.BytesIO(stdout_bytes)
        self.stderr = io.BytesIO(stderr_bytes)
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self):
        return self.returncode


class _InfiniteStdout:
    """A ``stdout`` whose ``read(n)`` never runs dry -- proves the caller stops asking for more
    once the cap is exceeded, rather than draining the (here, effectively unbounded) stream.
    """

    def __init__(self, chunk_size):
        self.chunk_size = chunk_size
        self.calls = 0

    def read(self, n):
        self.calls += 1
        if self.calls > 10000:
            raise AssertionError("read() called too many times; the size cap was not enforced")
        return b"x" * self.chunk_size

    def close(self):
        pass


class _FakeInfiniteProc:
    def __init__(self, chunk_size):
        self.stdout = _InfiniteStdout(chunk_size)
        self.stderr = io.BytesIO(b"")
        self.returncode = 0
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self):
        return self.returncode


class ResolveConsentedClipTests(unittest.TestCase):
    def setUp(self):
        self.rcc = _load(RESOLVER, "_resolve_consented_clip_under_test")
        hook = _load(HOOK, "_mlv_never_authorized_for_resolver_test")
        self.norm = hook.norm
        self.paths = (
            "C:/temp/%s/clip.raw" % TOKEN,
            "C:/temp/%s/clip.raw.part1" % TOKEN,
        )
        self.contents = (b"synthetic part zero " * 97, b"synthetic part one")
        self.spec_parts = [
            {"path": path, "length": len(content), "sha256": hashlib.sha256(content).hexdigest().upper()}
            for path, content in zip(self.paths, self.contents)
        ]
        self.table_parts = tuple(
            (
                len(content),
                hashlib.sha256(content).hexdigest(),
                hashlib.sha256(self.norm(path).encode("utf-8")).hexdigest(),
            )
            for path, content in zip(self.paths, self.contents)
        )
        self.spec = {
            "clips": [
                {"id": "FIX-0001", "parts": self.spec_parts},
                {"id": "FIX-ZERO", "parts": []},
            ]
        }
        self.spec_bytes = json.dumps(self.spec).encode("utf-8")
        self.table = {"clips": {"FIX-0001": self.table_parts}}

    def _resolve(self, clip_id, spec_bytes=None, table=None):
        return self.rcc.resolve(
            clip_id,
            ".",
            spec_bytes=self.spec_bytes if spec_bytes is None else spec_bytes,
            table=self.table if table is None else table,
        )

    # -- pass -----------------------------------------------------------------------------------
    def test_pass_when_every_part_matches(self):
        parts = self._resolve("FIX-0001")
        self.assertEqual([(p.index, p.status) for p in parts], [(0, self.rcc.PASS), (1, self.rcc.PASS)])
        self.assertEqual([p.path for p in parts], list(self.paths))

    def test_case_normalisation_uppercase_spec_vs_lowercase_table_passes(self):
        # self.spec_parts already carries uppercase sha256 (as the real spec does); self.table
        # already carries lowercase (as the hook's real table does). test_pass_when_every_part_
        # matches already proves this, but assert it explicitly so the fold is never accidental.
        for part in self.spec_parts:
            self.assertTrue(part["sha256"].isupper())
        for _length, sha, path_norm_sha in self.table_parts:
            self.assertTrue(sha.islower())
            self.assertTrue(path_norm_sha.islower())
        parts = self._resolve("FIX-0001")
        self.assertTrue(all(p.status == self.rcc.PASS for p in parts))

    # -- unknown id -------------------------------------------------------------------------------
    def test_unknown_id(self):
        with self.assertRaises(self.rcc.UnknownClipError) as ctx:
            self._resolve("NO-SUCH-ID")
        self.assertEqual(ctx.exception.code, "UNKNOWN_ID")

    # -- not in consent table -----------------------------------------------------------------------
    def test_id_absent_from_consent_table(self):
        spec = {"clips": [{"id": "FIX-UNCONSENTED", "parts": self.spec_parts}]}
        with self.assertRaises(self.rcc.NotConsentedError) as ctx:
            self._resolve("FIX-UNCONSENTED", spec_bytes=json.dumps(spec).encode("utf-8"))
        self.assertEqual(ctx.exception.code, "NOT_CONSENTED")

    # -- zero parts -------------------------------------------------------------------------------
    def test_zero_parts(self):
        with self.assertRaises(self.rcc.ZeroPartsError) as ctx:
            self._resolve("FIX-ZERO")
        self.assertEqual(ctx.exception.code, "ZERO_PARTS")

    # -- part-count mismatch ------------------------------------------------------------------------
    def test_part_count_mismatch(self):
        spec = {"clips": [{"id": "FIX-0001", "parts": self.spec_parts[:1]}]}
        with self.assertRaises(self.rcc.PartCountMismatchError) as ctx:
            self._resolve("FIX-0001", spec_bytes=json.dumps(spec).encode("utf-8"))
        self.assertEqual((ctx.exception.spec_count, ctx.exception.table_count), (1, 2))

    # -- length mismatch ----------------------------------------------------------------------------
    def test_length_mismatch(self):
        spec_parts = [dict(part) for part in self.spec_parts]
        spec_parts[1]["length"] += 1
        spec = {"clips": [{"id": "FIX-0001", "parts": spec_parts}]}
        with self.assertRaises(self.rcc.PartMismatchError) as ctx:
            self._resolve("FIX-0001", spec_bytes=json.dumps(spec).encode("utf-8"))
        self.assertEqual(ctx.exception.parts[1].status, self.rcc.LENGTH_MISMATCH)
        self.assertEqual(ctx.exception.parts[0].status, self.rcc.PASS)

    # -- sha mismatch ----------------------------------------------------------------------------
    def test_sha256_mismatch(self):
        spec_parts = [dict(part) for part in self.spec_parts]
        spec_parts[1]["sha256"] = "F" * 64
        spec = {"clips": [{"id": "FIX-0001", "parts": spec_parts}]}
        with self.assertRaises(self.rcc.PartMismatchError) as ctx:
            self._resolve("FIX-0001", spec_bytes=json.dumps(spec).encode("utf-8"))
        self.assertEqual(ctx.exception.parts[1].status, self.rcc.SHA256_MISMATCH)

    # -- path_norm mismatch -------------------------------------------------------------------------
    def test_path_norm_mismatch(self):
        table_parts = list(self.table_parts)
        length, sha, _path_norm_sha = table_parts[0]
        table_parts[0] = (length, sha, "0" * 64)
        table = {"clips": {"FIX-0001": tuple(table_parts)}}
        with self.assertRaises(self.rcc.PartMismatchError) as ctx:
            self._resolve("FIX-0001", table=table)
        self.assertEqual(ctx.exception.parts[0].status, self.rcc.PATH_NORM_MISMATCH)
        self.assertEqual(ctx.exception.parts[1].status, self.rcc.PASS)

    # -- size cap -------------------------------------------------------------------------------
    def test_size_cap_refuses_oversized_spec(self):
        oversized = b" " * (self.rcc.SPEC_CAP_BYTES + 1)
        with self.assertRaises(self.rcc.SpecTooLargeError):
            self._resolve("FIX-0001", spec_bytes=oversized)

    def test_size_cap_boundary_is_inclusive(self):
        # Exactly the cap parses fine (as JSON it will fail to parse -- pad with valid JSON
        # padding instead so only the cap arm is exercised, not the parser).
        padding_len = self.rcc.SPEC_CAP_BYTES - len(self.spec_bytes)
        self.assertGreater(padding_len, 0)
        padded = self.spec_bytes[:-1] + b" " * padding_len + self.spec_bytes[-1:]
        self.assertEqual(len(padded), self.rcc.SPEC_CAP_BYTES)
        parts = self._resolve("FIX-0001", spec_bytes=padded)
        self.assertTrue(all(p.status == self.rcc.PASS for p in parts))

    # -- the full ref is used -----------------------------------------------------------------------
    def test_default_ref_is_full_not_short(self):
        self.assertEqual(self.rcc.DEFAULT_REF, "refs/remotes/fork/master")
        self.assertTrue(self.rcc.DEFAULT_REF.startswith("refs/"))

    def test_git_argv_uses_the_full_ref(self):
        argv = self.rcc._git_show_argv("C:/repo", "refs/remotes/fork/master")
        self.assertEqual(
            argv,
            [
                "git",
                "-C",
                "C:/repo",
                "--no-replace-objects",
                "show",
                "refs/remotes/fork/master:tools/gates/output-budget.json",
            ],
        )

    def test_resolve_invokes_git_with_the_full_ref_when_no_spec_bytes_given(self):
        fake = _FakeProc(self.spec_bytes)
        with mock.patch.object(self.rcc.subprocess, "Popen", return_value=fake) as popen:
            # spec_bytes genuinely omitted here (not routed through the _resolve helper, which
            # substitutes self.spec_bytes for a bare None) so the git codepath actually fires.
            self.rcc.resolve("FIX-0001", ".", table=self.table)
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args[0][0]
        self.assertIn("refs/remotes/fork/master:tools/gates/output-budget.json", argv[-1])
        self.assertNotIn("fork/master", [a for a in argv if a != argv[-1]])

    def test_git_show_failure_raises_spec_read_error_without_holding_a_run_result(self):
        fake = _FakeProc(b"", stderr_bytes=b"fatal: bad object", returncode=128)
        with mock.patch.object(self.rcc.subprocess, "Popen", return_value=fake):
            with self.assertRaises(self.rcc.SpecReadError) as ctx:
                self.rcc.resolve("FIX-0001", ".", table=self.table)
        self.assertIn("fatal: bad object", str(ctx.exception))

    # -- streaming size cap: the child is read incrementally and killed the instant the cap is
    # exceeded, never buffered in full first (round 4, defect 1) ------------------------------
    def test_size_cap_streams_incrementally_and_kills_the_child_without_buffering_it_all(self):
        fake = _FakeInfiniteProc(chunk_size=65536)
        with mock.patch.object(self.rcc.subprocess, "Popen", return_value=fake):
            with self.assertRaises(self.rcc.SpecTooLargeError):
                self.rcc.resolve("FIX-0001", ".", table=self.table)
        self.assertTrue(fake.killed)
        # A correct implementation stops reading a small, bounded number of chunks past the cap
        # (cap / chunk size, plus a small constant). An implementation that buffered the whole
        # (here, effectively unbounded) stream before checking the length would call read() far
        # more times than this -- or hang -- before ever raising.
        max_expected_calls = (self.rcc.SPEC_CAP_BYTES // fake.stdout.chunk_size) + 4
        self.assertLessEqual(fake.stdout.calls, max_expected_calls)

    # -- the ref is validated as a FULL ref, never a short one or any other selector (round 4,
    # defect 2: the CLI's public --ref option is gone; resolve()'s own ref= parameter is
    # validated the same way regardless of caller) --------------------------------------------
    def test_cli_has_no_ref_option(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = self.rcc.main(["--clip-id", "FIX-0001", "--ref", "refs/remotes/fork/master"])
        self.assertEqual(code, 2)

    def test_resolve_rejects_a_short_ref(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve("FIX-0001", ".", ref="master", spec_bytes=self.spec_bytes, table=self.table)

    def test_resolve_rejects_head(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve("FIX-0001", ".", ref="HEAD", spec_bytes=self.spec_bytes, table=self.table)

    def test_resolve_rejects_a_dotdot_range(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve(
                "FIX-0001", ".", ref="refs/remotes/fork/master..refs/remotes/fork/other",
                spec_bytes=self.spec_bytes, table=self.table,
            )

    def test_resolve_rejects_a_colon_path_suffix(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve(
                "FIX-0001", ".", ref="refs/remotes/fork/master:some/other/file",
                spec_bytes=self.spec_bytes, table=self.table,
            )

    def test_resolve_rejects_an_at_brace_reflog_selector(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve(
                "FIX-0001", ".", ref="refs/remotes/fork/master@{yesterday}",
                spec_bytes=self.spec_bytes, table=self.table,
            )

    def test_resolve_rejects_a_caret_parent_selector(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve(
                "FIX-0001", ".", ref="refs/remotes/fork/master^{tree}",
                spec_bytes=self.spec_bytes, table=self.table,
            )

    def test_resolve_rejects_a_tilde_ancestry_selector(self):
        with self.assertRaises(self.rcc.InvalidRefError):
            self.rcc.resolve(
                "FIX-0001", ".", ref="refs/remotes/fork/master~1",
                spec_bytes=self.spec_bytes, table=self.table,
            )

    def test_resolve_accepts_the_default_full_ref(self):
        # Does not raise InvalidRefError; other assertions belong to the PASS-path tests above.
        self._resolve("FIX-0001")

    # -- CLI never prints a path --------------------------------------------------------------------
    def test_cli_summary_never_prints_a_path_on_pass(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.rcc, "resolve", return_value=self._resolve("FIX-0001")):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.rcc.main(["--clip-id", "FIX-0001"])
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(payload["ok"])
        self.assertEqual([p["status"] for p in payload["parts"]], ["PASS", "PASS"])

    def test_cli_summary_never_prints_a_path_on_mismatch(self):
        spec_parts = [dict(part) for part in self.spec_parts]
        spec_parts[1]["sha256"] = "F" * 64
        spec = {"clips": [{"id": "FIX-0001", "parts": spec_parts}]}

        real_resolve = self.rcc.resolve

        def fake_resolve(clip_id, repo_root, ref=self.rcc.DEFAULT_REF, spec_bytes=None, table=None):
            return real_resolve(
                clip_id, repo_root, ref=ref, spec_bytes=json.dumps(spec).encode("utf-8"), table=self.table
            )

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.rcc, "resolve", side_effect=fake_resolve):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.rcc.main(["--clip-id", "FIX-0001"])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "PART_MISMATCH")
        self.assertFalse(payload["ok"])

    # -- a malformed spec field never reaches the summary, not even truncated (round 4, defect 3)
    def test_cli_summary_never_leaks_a_path_smuggled_in_the_length_field(self):
        spec_parts = [dict(part) for part in self.spec_parts]
        spec_parts[1]["length"] = "C:/temp/%s/sneaky.raw" % TOKEN
        spec = {"clips": [{"id": "FIX-0001", "parts": spec_parts}]}

        real_resolve = self.rcc.resolve

        def fake_resolve(clip_id, repo_root, ref=self.rcc.DEFAULT_REF, spec_bytes=None, table=None):
            return real_resolve(
                clip_id, repo_root, ref=ref, spec_bytes=json.dumps(spec).encode("utf-8"), table=self.table
            )

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.rcc, "resolve", side_effect=fake_resolve):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.rcc.main(["--clip-id", "FIX-0001"])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["parts"][1]["length"], "<malformed>")
        self.assertEqual(payload["parts"][1]["status"], self.rcc.LENGTH_MISMATCH)
        self.assertEqual(payload["parts"][0]["length"], len(self.contents[0]))

    def test_cli_summary_never_leaks_a_path_smuggled_in_the_sha256_field(self):
        spec_parts = [dict(part) for part in self.spec_parts]
        spec_parts[1]["sha256"] = "C:/temp/%s/sneaky.raw" % TOKEN
        spec = {"clips": [{"id": "FIX-0001", "parts": spec_parts}]}

        real_resolve = self.rcc.resolve

        def fake_resolve(clip_id, repo_root, ref=self.rcc.DEFAULT_REF, spec_bytes=None, table=None):
            return real_resolve(
                clip_id, repo_root, ref=ref, spec_bytes=json.dumps(spec).encode("utf-8"), table=self.table
            )

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.rcc, "resolve", side_effect=fake_resolve):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.rcc.main(["--clip-id", "FIX-0001"])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["parts"][1]["sha256_12"], "<malformed>")
        self.assertEqual(payload["parts"][1]["status"], self.rcc.SHA256_MISMATCH)

    def test_safe_summary_length_rejects_bool_and_non_int(self):
        self.assertEqual(self.rcc._safe_summary_length(True), "<malformed>")
        self.assertEqual(self.rcc._safe_summary_length("123"), "<malformed>")
        self.assertEqual(self.rcc._safe_summary_length(123), 123)

    def test_safe_summary_sha_prefix_rejects_non_hex_and_wrong_length(self):
        self.assertEqual(self.rcc._safe_summary_sha_prefix("not-hex" * 10), "<malformed>")
        self.assertEqual(self.rcc._safe_summary_sha_prefix("a" * 63), "<malformed>")
        self.assertEqual(self.rcc._safe_summary_sha_prefix("A" * 64), "<malformed>")  # uppercase refused
        self.assertEqual(self.rcc._safe_summary_sha_prefix("a" * 64), "a" * 12)

    def test_cli_usage_error_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.rcc.main([]), 2)

    def test_emit_json_writes_full_parts_with_paths(self):
        import tempfile

        with mock.patch.object(self.rcc, "resolve", return_value=self._resolve("FIX-0001")):
            with tempfile.TemporaryDirectory() as tmp:
                out_path = os.path.join(tmp, "emitted.json")
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = self.rcc.main(["--clip-id", "FIX-0001", "--emit-json", out_path])
                self.assertEqual(code, 0)
                self.assertNotIn(TOKEN, out.getvalue())
                with open(out_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
        self.assertEqual([p["path"] for p in payload["parts"]], list(self.paths))


if __name__ == "__main__":
    unittest.main()
