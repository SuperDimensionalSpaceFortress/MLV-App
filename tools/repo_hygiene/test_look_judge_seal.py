#!/usr/bin/env python3
"""LOOK-METRICS-JUDGE-2 round 2: falsifier tests for the last three blockers of PR #218 and the nine fable hardenings.

    sol B1   the tally verifies the CURRENT rubric lock          (RubricLockTallyTests)
    sol B2   judge-disagreement needs two DIFFERENT judges        (DistinctJudgesTests)   [fable H5]
    sol B3   judge isolation is ENFORCED, not policy              (SealContainerTests, SealedSessionTests,
                                                                   JudgeRunnerIsolationTests, CanaryLogicTests)  [fable H2]
    fable    H1 empty --baseline-dir, H3 permission denials, H4 same-dir build, H6 skin only in the subject, H7 null config
             digest, H8 image extensions: the first three here and in test_look_judge_harness, H6/H8/H1 in test_look_metrics.

Pure standard library except SealedBuildCliTests (real pixels, skipped without numpy/Pillow, which hosted CI pins).
`LiveIsolationCanaryTests` needs the real CLIs and a live call, so it runs only when LOOK_LIVE_ISOLATION=1 (by hand; the PR
carries its transcript); nothing in this file that CI relies on to kill a mutant is skipped there.

THE CLASS EVERY TEST HERE SERVES: no unjudged, stale, incomplete, biased, self-judged, duplicated or unblinded result reads as
usable, and a REQUESTED input that is absent never reads as agreement.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

try:
    from tools.repo_hygiene import test_look_judge_harness as H
except ImportError:  # run directly from tools/repo_hygiene
    import test_look_judge_harness as H

import look_canary
import look_cli
import look_config
import look_judges
import look_pairs
import look_seal
import look_tally

_HAS_DEPS = H._HAS_DEPS
FAKE_KEY = "ab" * 32


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


class SealContainerTests(H.TmpCase):
    """The cipher/container itself: it hides the bytes, it authenticates them, and the wrong key gets nothing."""

    def _members(self, big=False):
        files = {"answer_key.json": b'{"canary": "CANARY-AAAA-cuda-is-left"}',
                 "source-frames/cuda-00.png": b"\x89PNG\r\n\x1a\n" + b"PIXELS" * 50,
                 "empty.bin": b""}
        if big:
            files["big.bin"] = bytes(range(256)) * (10 * 1024) + b"tail"  # 2.5 MiB: several cipher chunks
        paths = []
        for name, data in files.items():
            path = os.path.join(self.tmp, "src", *name.split("/"))
            _write_bytes(path, data)
            paths.append((name, path))
        return files, paths

    def _seal(self, big=False, key=FAKE_KEY):
        files, paths = self._members(big)
        out = os.path.join(self.tmp, "sealed.bin")
        look_seal.seal_files(out, key, paths)
        return files, out

    def test_every_member_comes_back_byte_for_byte_including_across_chunk_boundaries(self):
        files, out = self._seal(big=True)
        self.assertEqual(look_seal.read_members(out, FAKE_KEY), files)
        self.assertEqual(look_seal.read_members(out, FAKE_KEY, wanted={"empty.bin"}), {"empty.bin": b""})

    def test_the_sealed_bytes_hold_no_plaintext(self):
        files, out = self._seal()
        with open(out, "rb") as handle:
            blob = handle.read()
        for needle in (b"CANARY", b"cuda", b"PIXELS", b"\x89PNG", b"answer_key", b"source-frames"):
            self.assertNotIn(needle, blob)

    def test_sealing_the_same_data_twice_gives_different_bytes(self):
        _, first = self._seal()
        with open(first, "rb") as handle:
            a = handle.read()
        files, paths = self._members()
        second = os.path.join(self.tmp, "again.bin")
        look_seal.seal_files(second, FAKE_KEY, paths)
        with open(second, "rb") as handle:
            self.assertNotEqual(a, handle.read())

    def test_the_wrong_key_opens_nothing(self):
        _, out = self._seal()
        with self.assertRaises(look_seal.SealError):
            look_seal.read_members(out, "cd" * 32)

    def test_a_changed_byte_anywhere_is_refused(self):
        _, out = self._seal()
        with open(out, "rb") as handle:
            original = handle.read()
        for offset in (0, len(look_seal.MAGIC) + 3, len(look_seal.MAGIC) + 16 + 5, len(original) // 2,
                       len(original) - 40, len(original) - 1):
            with self.subTest(offset=offset):
                broken = bytearray(original)
                broken[offset] ^= 0x01
                bad = os.path.join(self.tmp, f"bad-{offset}.bin")
                _write_bytes(bad, bytes(broken))
                with self.assertRaises(look_seal.SealError):
                    look_seal.read_members(bad, FAKE_KEY)

    def test_a_truncated_or_tiny_file_is_refused(self):
        _, out = self._seal()
        with open(out, "rb") as handle:
            original = handle.read()
        for keep in (0, 10, 40, len(original) - 1):
            bad = os.path.join(self.tmp, f"cut-{keep}.bin")
            _write_bytes(bad, original[:keep])
            with self.subTest(keep=keep), self.assertRaises(look_seal.SealError):
                look_seal.read_members(bad, FAKE_KEY)

    def test_a_member_whose_recorded_digest_is_wrong_is_refused_even_though_the_mac_verifies(self):
        files, paths = self._members()
        out = os.path.join(self.tmp, "wrong-digest.bin")
        with mock.patch.object(look_seal, "_sha256_file", return_value="0" * 64):  # a sealer that records a bad digest
            look_seal.seal_files(out, FAKE_KEY, paths)
        with self.assertRaises(look_seal.SealError) as caught:
            look_seal.read_members(out, FAKE_KEY)
        self.assertIn("digest", str(caught.exception))

    def test_keys_must_be_sixty_four_hex_characters(self):
        for bad in ("", "abc", "g" * 64, "ab" * 31, "ab" * 33, None):
            with self.subTest(bad=bad), self.assertRaises(look_seal.SealError):
                look_seal.parse_key(bad)
        self.assertEqual(len(look_seal.parse_key(look_seal.new_key())), 32)
        self.assertNotEqual(look_seal.new_key(), look_seal.new_key())

    def test_key_from_args_prefers_the_explicit_key_then_the_file_then_the_environment(self):
        path = os.path.join(self.tmp, "k.txt")
        _write_bytes(path, ("cd" * 32 + "\n").encode("ascii"))
        env = {look_seal.KEY_ENV: "ef" * 32}
        self.assertEqual(look_seal.key_from_args("ab" * 32, path, env), "ab" * 32)
        self.assertEqual(look_seal.key_from_args(None, path, env), "cd" * 32)
        self.assertEqual(look_seal.key_from_args(None, None, env), "ef" * 32)
        self.assertIsNone(look_seal.key_from_args(None, None, {}))


class SealedSessionTests(H.TmpCase):
    """sol B3 / fable H2: after build-session the session directory holds nothing a judge may not read."""

    def setUp(self):
        super().setUp()
        self.fx = H.Fixture(self.tmp)
        self.key_hex = look_seal.new_key()
        self.plain_key, self.plain_session = dict(self.fx.key), dict(self.fx.session)

    def _seal(self, fx=None, with_source_frames=True):
        fx = fx or self.fx
        if with_source_frames:  # build-session's prepared frames: named after the subject, plainly readable pictures
            _write_bytes(os.path.join(fx.dir, "source-frames", "cuda-00.png"), b"\x89PNG-SOURCE-CUDA-FRAME")
            _write_bytes(os.path.join(fx.dir, "source-frames", "cpu-00.png"), b"\x89PNG-SOURCE-CPU-FRAME")
        return look_seal.seal_session(fx.dir, self.key_hex)

    def _listing(self, fx=None):
        return sorted(os.listdir((fx or self.fx).dir))

    def test_a_sealed_session_holds_only_what_a_judge_may_see(self):
        self.assertIn("answer_key.json", self._listing())
        self._seal()
        self.assertEqual(self._listing(), ["images", "judge_manifest.json", "sealed.bin", "session.json"])

    def test_the_public_session_names_neither_subject_nor_capture_nor_drops(self):
        self._seal()
        with open(os.path.join(self.fx.dir, "session.json"), "r", encoding="utf-8") as handle:
            text = handle.read()
        public = json.loads(text)
        self.assertTrue(set(public) <= set(look_seal.PUBLIC_SESSION_FIELDS) | {"sealed", "sealSha256"}, sorted(public))
        for secret in ("capture", "droppedFrames", "droppedFramePolicy"):
            self.assertNotIn(secret, public)
        for word in ("cuda", "cpu", "letterbox", "answer"):
            self.assertNotIn(word, text)
        self.assertTrue(public["sealed"])
        self.assertEqual(public["rubricSha256"], H.RUBRIC)

    def test_the_sealed_bytes_carry_neither_the_key_nor_the_source_frames_in_the_clear(self):
        self._seal()
        with open(os.path.join(self.fx.dir, "sealed.bin"), "rb") as handle:
            blob = handle.read()
        for needle in (b"cuda", b"cpu", b"SOURCE-CUDA", b"SOURCE-CPU", b"sourceSha256", b"DEGRADED|", b"orderSeed",
                       self.key_hex.encode("ascii")):
            self.assertNotIn(needle, blob)
        for name in os.listdir(self.fx.dir):  # the key itself is nowhere in the session directory
            path = os.path.join(self.fx.dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    self.assertNotIn(self.key_hex.encode("ascii"), handle.read())

    def test_opening_returns_the_original_key_and_full_session_from_memory(self):
        self._seal()
        key, session, sha = look_seal.open_sealed(self.fx.dir, self.key_hex)
        self.assertEqual(key, self.plain_key)
        self.assertEqual(session, self.plain_session)
        self.assertEqual(sha, look_seal.session_state(self.fx.dir)["sealSha256"])

    def test_the_sealed_members_are_the_key_the_session_the_source_frames_and_the_degraded_sources(self):
        summary = self._seal()
        self.assertEqual(sorted(summary["members"]),
                         ["answer_key.json", "degraded-sources/positive-0.png", "session.full.json",
                          "source-frames/cpu-00.png", "source-frames/cuda-00.png"])

    def test_the_wrong_key_opens_nothing(self):
        self._seal()
        with self.assertRaises(look_seal.SealError):
            look_seal.open_sealed(self.fx.dir, look_seal.new_key())

    def test_a_public_session_that_disagrees_with_the_sealed_record_is_refused(self):
        self._seal()
        path = os.path.join(self.fx.dir, "session.json")
        original = H.read_json(path)
        for field, value in (("rubricSha256", "0" * 64), ("orderSeed", "other"), ("itemCount", 99),
                             ("imageSha256s", ["1" * 64]), ("sealSha256", "2" * 64), ("sealed", False)):
            with self.subTest(field):
                H.write_json(path, dict(original, **{field: value}))
                with self.assertRaises(look_seal.SealError):
                    look_seal.open_sealed(self.fx.dir, self.key_hex)
        H.write_json(path, original)
        look_seal.open_sealed(self.fx.dir, self.key_hex)

    def test_a_sealed_file_swapped_in_from_another_session_is_refused(self):
        self._seal()
        other = H.Fixture(self.tmp, subdir="other", tag="v2")
        look_seal.seal_session(other.dir, self.key_hex)
        shutil.copyfile(os.path.join(other.dir, "sealed.bin"), os.path.join(self.fx.dir, "sealed.bin"))
        with self.assertRaises(look_seal.SealError):
            look_seal.open_sealed(self.fx.dir, self.key_hex)

    def test_sealing_twice_or_without_a_built_session_is_refused(self):
        self._seal()
        with self.assertRaises(look_seal.SealError):
            look_seal.seal_session(self.fx.dir, self.key_hex)
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(look_seal.SealError):
            look_seal.seal_session(empty, self.key_hex)
        with self.assertRaises(look_seal.SealError):
            look_seal.seal_session(H.Fixture(self.tmp, subdir="s3").dir, "not-a-key")

    def test_a_judge_runner_refuses_a_session_that_is_not_sealed_and_never_calls_the_judge(self):
        calls = []
        runner = H.sealed_runner(self.fx.judge_fn(H.faithful("cpu"), calls))
        runner.requires_sealed = True  # what the shipped Claude / Codex runners are
        with self.assertRaises(look_judges.JudgeError) as caught:
            look_judges.run_session(self.fx.dir, runner)
        self.assertIn("not sealed", str(caught.exception))
        self.assertEqual(calls, [])

    def test_a_judge_runner_refuses_plaintext_secrets_left_next_to_the_sealed_file(self):
        self._seal()
        for name in ("answer_key.json", "source-frames", "degraded-sources"):
            with self.subTest(name):
                path = os.path.join(self.fx.dir, name)
                if name == "answer_key.json":
                    _write_bytes(path, b"{}")
                else:
                    os.makedirs(path)
                calls = []
                runner = H.sealed_runner(self.fx.judge_fn(H.faithful("cpu"), calls))
                runner.requires_sealed = True
                with self.assertRaises(look_judges.JudgeError) as caught:
                    look_judges.run_session(self.fx.dir, runner)
                self.assertIn("plaintext", str(caught.exception))
                self.assertEqual(calls, [])
                if name == "answer_key.json":
                    os.remove(path)
                else:
                    os.rmdir(path)

    def test_a_sealed_session_runs_and_the_results_record_the_seal_and_the_isolation(self):
        self._seal()
        calls = []
        runner = H.sealed_runner(self.fx.judge_fn(H.faithful("cpu"), calls))
        runner.requires_sealed = True
        summary = look_judges.run_session(self.fx.dir, runner)
        self.assertEqual(summary["errors"], 0)
        doc = H.read_json(summary["resultsPath"])
        self.assertEqual(doc["sealSha256"], look_seal.session_state(self.fx.dir)["sealSha256"])
        self.assertTrue(doc["judgedUnderSeal"])
        self.assertEqual(doc["isolation"], H.ISOLATION_OK)

    def test_results_made_on_a_plaintext_session_say_so(self):
        summary = look_judges.run_session(self.fx.dir, look_judges.CallableJudge(self.fx.judge_fn(H.faithful("cpu"))))
        doc = H.read_json(summary["resultsPath"])
        self.assertIsNone(doc["sealSha256"])
        self.assertFalse(doc["judgedUnderSeal"])
        self.assertEqual(doc["isolation"], {"enforced": False, "kind": "callable"})

    def test_verdicts_made_before_the_session_was_sealed_are_judged_again_not_carried_over(self):
        calls = []
        runner = H.sealed_runner(self.fx.judge_fn(H.faithful("cpu"), calls))
        look_judges.run_session(self.fx.dir, runner)  # judged while the key was lying in the directory
        self.assertEqual(len(calls), 14)
        self._seal()
        again = look_judges.run_session(self.fx.dir, runner)
        self.assertEqual((len(calls), again["staleRejected"]), (28, 14))
        doc = H.read_json(again["resultsPath"])
        self.assertEqual(set(doc["staleRejected"].values()), {"SEAL_OR_ISOLATION_DIFFERS_FROM_CURRENT_RUN"})
        self.assertTrue(doc["judgedUnderSeal"])
        once_more = look_judges.run_session(self.fx.dir, runner)  # and a run under unchanged conditions keeps its verdicts
        self.assertEqual((len(calls), once_more["staleRejected"]), (28, 0))

    def test_a_changed_isolation_record_also_invalidates_stored_verdicts(self):
        self._seal()
        calls = []
        runner = look_judges.CallableJudge(self.fx.judge_fn(H.faithful("cpu"), calls), "cj", "claude-fable-5-1",
                                           "anthropic", isolation={"enforced": True, "kind": "a"})
        look_judges.run_session(self.fx.dir, runner)
        runner._isolation = {"enforced": True, "kind": "b"}
        self.assertEqual(look_judges.run_session(self.fx.dir, runner)["staleRejected"], 14)

    def test_the_seal_key_in_the_environment_stops_a_real_judge_before_anything_runs(self):
        self._seal()
        calls = []
        runner = H.sealed_runner(self.fx.judge_fn(H.faithful("cpu"), calls))
        runner.requires_sealed = True
        with mock.patch.dict(os.environ, {look_seal.KEY_ENV: self.key_hex}):
            with self.assertRaises(look_judges.JudgeError):
                look_judges.run_session(self.fx.dir, runner)
            with self.assertRaises(look_judges.JudgeError):
                look_judges.assert_no_seal_key_in_environment()
        self.assertEqual(calls, [])
        look_judges.assert_no_seal_key_in_environment({})

    def test_extract_all_writes_the_members_but_never_inside_the_session(self):
        self._seal()
        dest = os.path.join(self.tmp, "audit")
        look_seal.extract_all(self.fx.dir, self.key_hex, dest)
        with open(os.path.join(dest, "answer_key.json"), "rb") as handle:
            self.assertEqual(json.loads(handle.read()), self.plain_key)
        with open(os.path.join(dest, "source-frames", "cuda-00.png"), "rb") as handle:
            self.assertEqual(handle.read(), b"\x89PNG-SOURCE-CUDA-FRAME")
        for inside in (self.fx.dir, os.path.join(self.fx.dir, "audit")):
            with self.assertRaises(look_seal.SealError):
                look_seal.extract_all(self.fx.dir, self.key_hex, inside)


class SealedTallyGateTests(H.TmpCase):
    """The tally refuses a judge that could have read the key, a seal it cannot verify, and an isolation it was not told
    was enforced. API first, then the CLI path that carries the key."""

    def setUp(self):
        super().setUp()
        self.fx = H.Fixture(self.tmp, sealed=True)

    def _answers(self):
        return self.fx.answers(H.faithful("cpu"))

    def test_no_seal_is_unusable(self):
        entry = self.fx.tally(self._answers(), seal=None)
        self.assertIn("SEAL_NOT_VERIFIED", entry["unusableReasons"])
        self.assertFalse(entry["usable"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_an_unverified_or_malformed_seal_is_unusable(self):
        for bad in ({"verified": False, "sealSha256": H.SEAL_SHA, "resultsSealSha256": H.SEAL_SHA},
                    {"verified": True, "sealSha256": "short", "resultsSealSha256": "short"},
                    {"verified": True, "resultsSealSha256": H.SEAL_SHA}, "yes", []):
            with self.subTest(bad=bad):
                self.assertIn("SEAL_NOT_VERIFIED", self.fx.tally(self._answers(), seal=bad)["unusableReasons"])

    def test_verdicts_made_under_another_or_no_seal_are_unusable(self):
        for results_seal in (None, "6" * 64):
            with self.subTest(results_seal=results_seal):
                entry = self.fx.tally(self._answers(), seal=dict(H.SEAL_OK, resultsSealSha256=results_seal))
                self.assertIn("JUDGED_WITHOUT_OR_AFTER_SEAL", entry["unusableReasons"])
                self.assertNotIn("SEAL_NOT_VERIFIED", entry["unusableReasons"])

    def test_isolation_that_is_not_recorded_as_enforced_is_unusable(self):
        for bad in (None, {}, {"enforced": False}, {"enforced": "true"}, {"kind": "codex-exec"}, "enforced"):
            with self.subTest(bad=bad):
                entry = self.fx.tally(self._answers(), judge_isolation=bad)
                self.assertIn("JUDGE_ISOLATION_NOT_ENFORCED", entry["unusableReasons"])
                self.assertFalse(entry["usable"])

    def test_a_clean_entry_records_the_seal_and_the_isolation_it_was_judged_under(self):
        entry = self.fx.tally(self._answers())
        self.assertTrue(entry["usable"], entry["unusableReasons"])
        self.assertEqual(entry["sealSha256"], H.SEAL_SHA)
        self.assertEqual(entry["judgeIsolation"], H.ISOLATION_OK)

    # -- through the CLI, with a real seal ------------------------------------------------------------------
    def _judge(self, runner=None):
        runner = runner or H.sealed_runner(self.fx.judge_fn(H.faithful("cpu")))
        return look_judges.run_session(self.fx.dir, runner)

    def _tally(self, summary, extra=(), fx=None):
        fx = fx or self.fx
        out = os.path.join(self.tmp, "t.json")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = look_cli.main(["tally", "--session-dir", fx.dir, "--results", summary["resultsPath"], "--out", out,
                                  "--producer-model", "claude-sonnet-5-5", *extra])
        return code, (H.read_json(out) if os.path.isfile(out) else None), stderr.getvalue()

    def test_the_cli_tally_needs_the_key_and_the_key_must_be_right(self):
        summary = self._judge()
        code, entry, stderr = self._tally(summary)
        self.assertEqual((code, entry), (2, None))
        self.assertIn("sealed", stderr)
        code, entry, _ = self._tally(summary, ["--seal-key", look_seal.new_key()])
        self.assertEqual((code, entry), (2, None))
        code, entry, _ = self._tally(summary, H.seal_args(self.fx))
        self.assertEqual((code, entry["usable"]), (0, True))

    def test_the_key_may_come_from_a_file_or_the_environment(self):
        summary = self._judge()
        path = os.path.join(self.tmp, "key.txt")
        _write_bytes(path, (self.fx.seal_key + "\n").encode("ascii"))
        self.assertEqual(self._tally(summary, ["--seal-key-file", path])[0], 0)
        with mock.patch.dict(os.environ, {look_seal.KEY_ENV: self.fx.seal_key}):
            self.assertEqual(self._tally(summary)[0], 0)

    def test_a_plaintext_session_tallies_unusable_whatever_the_verdicts_say(self):
        fx = H.Fixture(self.tmp, subdir="plain")
        summary = look_judges.run_session(fx.dir, H.sealed_runner(fx.judge_fn(H.faithful("cpu"))))
        code, entry, _ = self._tally(summary, fx=fx)
        self.assertEqual(code, 2)
        self.assertIn("SEAL_NOT_VERIFIED", entry["unusableReasons"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_a_judge_that_ran_while_the_key_was_lying_in_the_directory_is_unusable_even_if_sealed_afterwards(self):
        fx = H.Fixture(self.tmp, subdir="late", sealed=False)
        summary = look_judges.run_session(fx.dir, H.sealed_runner(fx.judge_fn(H.faithful("cpu"))))
        fx.seal_key = look_seal.new_key()
        look_seal.seal_session(fx.dir, fx.seal_key)  # sealed only AFTER the judge had run
        code, entry, _ = self._tally(summary, H.seal_args(fx), fx=fx)
        self.assertEqual(code, 2)
        self.assertIn("JUDGED_WITHOUT_OR_AFTER_SEAL", entry["unusableReasons"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_a_judge_whose_isolation_the_harness_did_not_enforce_is_unusable_on_a_sealed_session(self):
        summary = self._judge(look_judges.CallableJudge(self.fx.judge_fn(H.faithful("cpu")), "cj", "claude-fable-5-1",
                                                        "anthropic"))
        code, entry, _ = self._tally(summary, H.seal_args(self.fx))
        self.assertEqual(code, 2)
        self.assertEqual(entry["unusableReasons"], ["JUDGE_ISOLATION_NOT_ENFORCED"])

    def test_a_seal_that_does_not_name_the_results_is_unusable(self):
        summary = self._judge()
        doc = H.read_json(summary["resultsPath"])
        doc["sealSha256"] = "7" * 64
        H.write_json(summary["resultsPath"], doc)
        code, entry, _ = self._tally(summary, H.seal_args(self.fx))
        self.assertEqual(code, 2)
        self.assertIn("JUDGED_WITHOUT_OR_AFTER_SEAL", entry["unusableReasons"])


class RubricLockTallyTests(H.TmpCase):
    """sol B1: results under a rubric other than the lock verified NOW are not usable, in the API and the CLI, and
    judge-disagreement checks the same lock."""

    def test_no_lock_is_unusable(self):
        fx = H.Fixture(self.tmp)
        for bad in (None, {}, {"rubricSha256": "short"}, {"rubricSha256": None}, "lock"):
            with self.subTest(bad=bad):
                entry = fx.tally(fx.answers(H.faithful("cpu")), rubric_lock=bad)
                self.assertIn("RUBRIC_LOCK_NOT_VERIFIED", entry["unusableReasons"])
                self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_a_session_frozen_under_another_rubric_than_the_lock_is_unusable(self):
        fx = H.Fixture(self.tmp)
        other_lock = dict(H.LOCK, rubricSha256="f" * 64)
        entry = fx.tally(fx.answers(H.faithful("cpu")), rubric_lock=other_lock)
        self.assertEqual(entry["unusableReasons"], ["SESSION_RUBRIC_DIFFERS_FROM_LOCK"])
        self.assertEqual(entry["preference"]["withheldWinner"], "cpu")
        self.assertEqual(fx.tally(fx.answers(H.faithful("cpu")))["unusableReasons"], [])

    def test_the_entry_records_the_lock_it_was_verified_against(self):
        fx = H.Fixture(self.tmp)
        entry = fx.tally(fx.answers(H.faithful("cpu")))
        self.assertEqual((entry["rubricLockSha256"], entry["rubricId"]), (H.RUBRIC, H.LOCK["rubricId"]))

    def _other_rubric(self):
        rubric = os.path.join(self.tmp, "judge_rubric.md")
        lock_path = os.path.join(self.tmp, "judge_rubric.lock.json")
        shutil.copyfile(look_config.RUBRIC_PATH, rubric)
        with open(rubric, "ab") as handle:
            handle.write(b"\nA separately locked rubric.\n")
        lock = look_config.write_rubric_lock(rubric, lock_path)
        self.assertNotEqual(lock["rubricSha256"], H.RUBRIC)
        return rubric, lock_path, lock

    def _tally_cli(self, fx, summary, extra=()):
        out = os.path.join(self.tmp, "t.json")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = look_cli.main(["tally", "--session-dir", fx.dir, "--results", summary["resultsPath"], "--out", out,
                                  "--producer-model", "claude-sonnet-5-5", *H.seal_args(fx), *extra])
        return code, (H.read_json(out) if os.path.isfile(out) else None)

    def test_sols_repro_a_separately_locked_session_is_unusable_under_the_shipped_lock(self):
        rubric, lock_path, lock = self._other_rubric()
        fx = H.Fixture(self.tmp, sealed=True, rubric=lock["rubricSha256"])
        summary = look_judges.run_session(fx.dir, H.sealed_runner(fx.judge_fn(H.faithful("cpu"))),
                                          rubric_path=rubric, lock_path=lock_path)
        self.assertEqual(summary["errors"], 0)
        code, entry = self._tally_cli(fx, summary)  # the default: the lock that ships
        self.assertEqual(code, 2)
        self.assertEqual(entry["unusableReasons"], ["SESSION_RUBRIC_DIFFERS_FROM_LOCK"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")
        self.assertEqual(entry["preference"]["withheldWinner"], "cpu")

    def test_an_explicitly_passed_lock_that_equals_the_sessions_and_the_verdicts_digest_is_accepted(self):
        rubric, lock_path, lock = self._other_rubric()
        fx = H.Fixture(self.tmp, sealed=True, rubric=lock["rubricSha256"])
        summary = look_judges.run_session(fx.dir, H.sealed_runner(fx.judge_fn(H.faithful("cpu"))),
                                          rubric_path=rubric, lock_path=lock_path)
        code, entry = self._tally_cli(fx, summary, ["--rubric", rubric, "--rubric-lock", lock_path])
        self.assertEqual((code, entry["usable"]), (0, True))
        self.assertEqual(entry["rubricLockSha256"], lock["rubricSha256"])

    def test_an_explicit_lock_that_the_session_does_not_match_is_unusable(self):
        rubric, lock_path, lock = self._other_rubric()
        fx = H.Fixture(self.tmp, sealed=True)  # frozen under the SHIPPED rubric
        summary = look_judges.run_session(fx.dir, H.sealed_runner(fx.judge_fn(H.faithful("cpu"))))
        code, entry = self._tally_cli(fx, summary, ["--rubric", rubric, "--rubric-lock", lock_path])
        self.assertEqual(code, 2)
        self.assertEqual(entry["unusableReasons"], ["SESSION_RUBRIC_DIFFERS_FROM_LOCK"])

    def test_verdicts_quoting_another_rubric_than_the_session_are_unusable_even_under_the_matching_lock(self):
        fx = H.Fixture(self.tmp)
        answers = fx.answers(H.faithful("cpu"))
        victim = next(iter(answers))
        answers[victim] = dict(answers[victim], rubricSha256="0" * 64)
        entry = fx.tally(answers)
        self.assertIn("INCOMPLETE_ITEMS", entry["unusableReasons"])

    def test_a_rubric_edited_after_its_lock_stops_the_tally_and_the_comparison(self):
        rubric, lock_path, _ = self._other_rubric()
        with open(rubric, "ab") as handle:
            handle.write(b"edited after the freeze")
        fx = H.Fixture(self.tmp, sealed=True)
        summary = look_judges.run_session(fx.dir, H.sealed_runner(fx.judge_fn(H.faithful("cpu"))))
        code, entry = self._tally_cli(fx, summary, ["--rubric", rubric, "--rubric-lock", lock_path])
        self.assertEqual((code, entry), (2, None))
        path = os.path.join(self.tmp, "e.json")
        H.write_json(path, H.second_judge(fx.tally(fx.answers(H.faithful("cpu")))))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(look_cli.main(["judge-disagreement", "--entries", path, path, "--rubric", rubric,
                                            "--rubric-lock", lock_path]), 2)

    # -- judge-disagreement ----------------------------------------------------------------------------------
    def test_disagreement_without_a_verified_lock_is_not_comparable(self):
        fx = H.Fixture(self.tmp)
        a = fx.tally(fx.answers(H.faithful("cpu")))
        verdict = look_tally.judge_disagreement([a, H.second_judge(a)], 1.0)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(verdict["rubricLockProblems"], [{"code": "RUBRIC_LOCK_NOT_VERIFIED"}])

    def test_disagreement_entries_under_another_rubric_than_the_lock_are_not_comparable(self):
        fx = H.Fixture(self.tmp)
        a = fx.tally(fx.answers(H.faithful("cpu")))
        b = H.second_judge(a)
        a["rubricSha256"] = b["rubricSha256"] = "f" * 64  # both agree with each other, neither with the lock
        verdict = look_tally.judge_disagreement([a, b], 1.0, rubric_lock=H.LOCK)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(verdict["rubricLockProblems"],
                         [{"code": "ENTRY_RUBRIC_DIFFERS_FROM_LOCK", "judges": ["j1", "j2"]}])
        self.assertEqual(verdict["sessionMismatches"], [])

    def test_the_disagreement_cli_checks_the_shipped_lock(self):
        fx = H.Fixture(self.tmp)
        a = fx.tally(fx.answers(H.faithful("cpu")))
        b = H.second_judge(a)
        paths = []
        for name, entry in (("a", a), ("b", b)):
            paths.append(os.path.join(self.tmp, name + ".json"))
            H.write_json(paths[-1], dict(entry, rubricSha256="f" * 64))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = look_cli.main(["judge-disagreement", "--entries", *paths])
        self.assertEqual(code, 2)
        self.assertIn("ENTRY_RUBRIC_DIFFERS_FROM_LOCK", out.getvalue())


class DistinctJudgesTests(H.TmpCase):
    """sol B2 / fable H5: a comparison needs TWO DIFFERENT judges. One judge counted twice agrees with itself."""

    def setUp(self):
        super().setUp()
        self.fx = H.Fixture(self.tmp)
        self.a = self.fx.tally(self.fx.answers(H.faithful("cpu")))
        self.b = H.second_judge(self.a)

    def _verdict(self, *entries, **kw):
        kw.setdefault("rubric_lock", H.LOCK)
        return look_tally.judge_disagreement(list(entries), 1.0, **kw)

    def _codes(self, verdict):
        return [p["code"] for p in verdict["identityProblems"]]

    def test_two_different_judges_are_comparable(self):
        verdict = self._verdict(self.a, self.b)
        self.assertTrue(verdict["comparable"], verdict)
        self.assertEqual(verdict["identityProblems"], [])
        self.assertFalse(verdict["thirdJudgeNeeded"])

    def test_the_same_entry_twice_is_not_a_comparison(self):  # sol's exact repro: judge-disagreement --entries T.json T.json
        verdict = self._verdict(self.a, self.a)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(sorted(self._codes(verdict)), ["DUPLICATE_JUDGE_ID", "DUPLICATE_MODEL", "DUPLICATE_RESULTS"])
        self.assertIsNone(verdict["thirdJudgeNeeded"])  # not a reassuring False

    def test_one_judge_id_under_two_models_or_one_model_under_two_ids_is_not_a_comparison(self):
        same_id = H.second_judge(self.a, judge_id=self.a["judgeId"])
        self.assertEqual(self._codes(self._verdict(self.a, same_id)), ["DUPLICATE_JUDGE_ID"])
        same_model = H.second_judge(self.a, model=self.a["model"])
        self.assertEqual(self._codes(self._verdict(self.a, same_model)), ["DUPLICATE_MODEL"])

    def test_identity_is_compared_after_trimming_and_case_folding(self):
        sneaky = H.second_judge(self.a, judge_id=" J1 ", model=self.a["model"].upper())
        self.assertEqual(sorted(self._codes(self._verdict(self.a, sneaky))), ["DUPLICATE_JUDGE_ID", "DUPLICATE_MODEL"])

    def test_a_copied_results_file_under_a_new_name_is_not_a_second_judge(self):
        copied = H.second_judge(self.a)
        copied["resultsSha256"] = self.a["resultsSha256"]
        self.assertEqual(self._codes(self._verdict(self.a, copied)), ["DUPLICATE_RESULTS"])

    def test_fewer_than_two_entries_is_not_a_comparison(self):
        for entries in ([], [self.a]):
            verdict = self._verdict(*entries)
            self.assertFalse(verdict["comparable"])
            self.assertEqual(self._codes(verdict), ["FEWER_THAN_TWO_JUDGES"])

    def test_missing_identity_fields_are_not_comparable(self):
        for field, code in (("judgeId", "JUDGEID_MISSING"), ("model", "MODEL_MISSING"),
                            ("resultsSha256", "RESULTS_DIGEST_MISSING")):
            with self.subTest(field):
                broken = json.loads(json.dumps(self.b))
                del broken[field]
                verdict = self._verdict(self.a, broken)
                self.assertFalse(verdict["comparable"])
                self.assertIn(code, self._codes(verdict))
        blank = dict(self.b, judgeId="  ")
        self.assertIn("JUDGEID_MISSING", self._codes(self._verdict(self.a, blank)))

    def test_a_claimed_cross_family_comparison_needs_two_model_families(self):
        claimed = dict(self.a, crossFamilyStatus=look_judges.CROSS_FAMILY_PROVEN)
        same_family = H.second_judge(self.a)
        verdict = self._verdict(claimed, same_family)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(self._codes(verdict), ["CROSS_FAMILY_CLAIMED_BUT_NOT_DISTINCT_FAMILIES"])
        verdict = self._verdict(self.a, same_family, require_cross_family=True)
        self.assertEqual(self._codes(verdict), ["CROSS_FAMILY_CLAIMED_BUT_NOT_DISTINCT_FAMILIES"])
        self.assertTrue(self._verdict(self.a, same_family)["comparable"])  # not claimed: one family is fine
        openai = H.second_judge(self.a, judge_id="codex-1", model="gpt-6.1-sol", family="openai")
        self.assertTrue(self._verdict(claimed, openai)["comparable"])
        self.assertTrue(self._verdict(self.a, openai, require_cross_family=True)["comparable"])

    def test_an_entry_whose_family_does_not_match_its_model_cannot_pass_as_cross_family(self):
        liar = H.second_judge(self.a, judge_id="codex-1", model="claude-haiku-4-5", family="openai")
        verdict = self._verdict(self.a, liar, require_cross_family=True)
        self.assertFalse(verdict["comparable"])
        self.assertIn("FAMILY_DOES_NOT_MATCH_MODEL", self._codes(verdict))
        unplaceable = H.second_judge(self.a, judge_id="x", model="mystery", family="unknown")
        verdict = self._verdict(self.a, unplaceable, require_cross_family=True)
        self.assertIn("CROSS_FAMILY_CLAIMED_BUT_NOT_DISTINCT_FAMILIES", self._codes(verdict))

    def test_malformed_entries_are_not_comparable_and_never_read_as_no_disagreement(self):
        for bad in ({}, dict(self.b, scores={}), {k: v for k, v in self.b.items() if k != "scores"},
                    dict(self.b, schema="other"), "entry", None):
            with self.subTest(bad=bad):
                verdict = self._verdict(self.a, bad)
                self.assertFalse(verdict["comparable"])
                self.assertEqual(self._codes(verdict), ["MALFORMED_ENTRY"])
                self.assertIsNone(verdict["thirdJudgeNeeded"])

    def test_a_disagreement_that_is_found_is_reported_even_when_the_comparison_is_not_valid(self):
        far = H.second_judge(self.a)
        far["scores"]["cpu"]["colour_cast"] += 3
        far["usable"] = False
        verdict = self._verdict(self.a, far)
        self.assertFalse(verdict["comparable"])
        self.assertTrue(verdict["thirdJudgeNeeded"])

    # -- the CLI ---------------------------------------------------------------------------------------------
    def _cli(self, *entries, extra=()):
        paths = []
        for i, entry in enumerate(entries):
            paths.append(os.path.join(self.tmp, f"e{i}.json"))
            H.write_json(paths[-1], entry)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = look_cli.main(["judge-disagreement", "--entries", *paths, *extra])
        return code, json.loads(out.getvalue())

    def test_the_cli_exits_zero_for_two_judges_and_non_zero_for_one_judge_twice(self):
        code, shown = self._cli(self.a, self.b)
        self.assertEqual((code, shown["comparable"]), (0, True))
        code, shown = self._cli(self.a, self.a)  # sol's repro through the real CLI
        self.assertEqual((code, shown["comparable"], shown["thirdJudgeNeeded"]), (2, False, None))
        self.assertIn("DUPLICATE_JUDGE_ID", json.dumps(shown["identityProblems"]))

    def test_the_cli_require_cross_family_flag(self):
        self.assertEqual(self._cli(self.a, self.b, extra=["--require-cross-family"])[0], 2)
        openai = H.second_judge(self.a, judge_id="codex-1", model="gpt-6.1-sol", family="openai")
        self.assertEqual(self._cli(self.a, openai, extra=["--require-cross-family"])[0], 0)


class JudgeRunnerIsolationTests(H.TmpCase):
    """sol B3 / fable H2 / H3: what the shipped runners ask of their CLIs, what they refuse, and what they record."""

    ESSENTIAL_CODEX_TOOLS = ("shell_tool", "unified_exec", "view_image", "code_mode_host", "browser_use",
                             "computer_use", "apps", "plugins", "skill_search", "hooks", "multi_agent", "multi_agent_v2")

    def setUp(self):
        super().setUp()
        self.session = os.path.join(self.tmp, "session")
        os.makedirs(self.session)
        self.png = os.path.join(self.session, "pair.png")
        _write_bytes(self.png, b"PNG-BYTES")
        self.seen = {}

    def _fake_run(self, stdout_doc):
        def fake(argv, input_text=None, cwd=None, timeout=300, grace_s=5, env=None):
            self.seen.update(argv=list(argv), cwd=cwd, env=env, input=input_text)
            return subprocess.CompletedProcess(argv, 0, stdout_doc, "")
        return fake

    def _reply(self, **extra):
        verdict = json.dumps({"left": H._scores(), "right": H._scores(), "preference": "tie"})
        return json.dumps(dict({"result": verdict, "permission_denials": []}, **extra))

    @staticmethod
    def _has_pair(argv, flag, value):
        return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))

    # -- Codex: tools off -----------------------------------------------------------------------------------
    def test_the_codex_judge_has_every_tool_that_could_read_a_file_switched_off(self):
        judge = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")
        cmd = judge.command("/w/pair.png", "/w/out.txt", "/w")
        for feature in self.ESSENTIAL_CODEX_TOOLS:
            with self.subTest(feature):
                self.assertIn(feature, look_judges.CODEX_DISABLED_FEATURES)
                self.assertTrue(self._has_pair(cmd, "--disable", feature), feature)
        for feature in look_judges.CODEX_DISABLED_FEATURES:
            self.assertTrue(self._has_pair(cmd, "--disable", feature), feature)
        for flag in ("--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
            self.assertIn(flag, cmd)
        self.assertTrue(self._has_pair(cmd, "-c", "web_search=disabled"))
        self.assertEqual(cmd[cmd.index("--image") + 2], "--sandbox")  # --image is variadic: a flag must follow it
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")

    def test_the_unconfined_codex_command_exists_only_as_the_canarys_control(self):
        judge = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")
        control = judge.command("/w/pair.png", "/w/out.txt", "/w", confined=False)
        for flag in ("--disable", "--strict-config", "--ignore-user-config", "--ignore-rules", "-c"):
            self.assertNotIn(flag, control)
        self.assertIn("--sandbox", control)
        self.assertEqual(control, judge.command("/w/pair.png", "/w/out.txt", "/w", confined=False))

    def test_judge_image_always_runs_the_confined_codex_command_with_a_clean_environment(self):
        judge = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")
        with mock.patch.dict(os.environ, {"LOOK_OTHER": "1", "PATH_KEPT": "yes"}), \
                mock.patch.object(look_judges, "run_bounded", self._fake_run("")):
            with self.assertRaises(look_judges.JudgeError):  # canned reply holds no verdict: we read what was run
                judge.judge_image(self.png, "RUBRIC")
        for feature in look_judges.CODEX_DISABLED_FEATURES:
            self.assertTrue(self._has_pair(self.seen["argv"], "--disable", feature), feature)
        self.assertIn("--strict-config", self.seen["argv"])
        self.assertEqual(self.seen["env"].get("PATH_KEPT"), "yes")  # a real environment, minus LOOK_*
        self.assertNotIn("LOOK_OTHER", self.seen["env"])

    # -- Claude: confinement + denials -------------------------------------------------------------------------
    def test_judge_image_always_runs_the_confined_claude_command_with_a_clean_environment(self):
        judge = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude")
        with mock.patch.dict(os.environ, {"LOOK_OTHER": "1", "PATH_KEPT": "yes"}), \
                mock.patch.object(look_judges, "run_bounded", self._fake_run(self._reply())):
            judge.judge_image(self.png, "RUBRIC")
        for flag in look_judges.CLAUDE_CONFINEMENT_ARGS:
            self.assertIn(flag, self.seen["argv"])
        self.assertEqual(self.seen["env"].get("PATH_KEPT"), "yes")
        self.assertNotIn("LOOK_OTHER", self.seen["env"])
        control = judge.command(confined=False)
        for flag in look_judges.CLAUDE_CONFINEMENT_ARGS:
            self.assertNotIn(flag, control)
        self.assertEqual(control[control.index("--tools") + 1], "Read")

    def test_a_claude_reply_with_permission_denials_is_an_error_not_a_verdict(self):
        judge = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude")
        denial = {"tool_name": "Read", "tool_input": {"file_path": "C:/secret/answer_key.json"}}
        with mock.patch.object(look_judges, "run_bounded", self._fake_run(self._reply(permission_denials=[denial]))):
            with self.assertRaises(look_judges.JudgeError) as caught:
                judge.judge_image(self.png, "RUBRIC")
        self.assertIn("denied", str(caught.exception))
        self.assertIn("answer_key", str(caught.exception))  # the attempt is on the record

    def test_a_claude_reply_that_cannot_prove_it_had_no_denials_is_an_error(self):
        judge = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude")
        for outer in ({"result": "{}"}, {"result": "{}", "permission_denials": None},
                      {"result": "{}", "permission_denials": "none"}):
            with self.subTest(outer=outer):
                with mock.patch.object(look_judges, "run_bounded", self._fake_run(json.dumps(outer))):
                    with self.assertRaises(look_judges.JudgeError) as caught:
                        judge.judge_image(self.png, "RUBRIC")
                self.assertIn("permission_denials", str(caught.exception))

    def test_a_denied_item_is_recorded_as_an_error_by_run_session_and_never_scored(self):
        fx = H.Fixture(self.tmp, subdir="sealed-denials", sealed=True)

        class Denied(look_judges.ClaudeCliJudge):
            def judge_image(self, png_path, rubric_text):
                raise look_judges.JudgeError("claude was denied 1 tool call(s)")

        runner = Denied("claude-fable-5-1", claude_exe="claude")
        summary = look_judges.run_session(fx.dir, runner)
        self.assertEqual((summary["judged"], summary["errors"], summary["remaining"]), (0, 14, 14))
        doc = H.read_json(summary["resultsPath"])
        self.assertTrue(all("denied" in message for message in doc["errors"].values()))

    # -- the seal key never reaches a judge ------------------------------------------------------------------
    def test_the_judge_environment_drops_the_seal_key_and_every_look_variable(self):
        env = {"PATH": "p", "HOME": "h", look_seal.KEY_ENV: FAKE_KEY, "LOOK_X": "1", "look_lower": "1", "LOOKOUT": "k"}
        scrubbed = look_judges.judge_environment(env)
        self.assertEqual(scrubbed, {"PATH": "p", "HOME": "h", "LOOKOUT": "k"})  # LOOK_ is a prefix, not a substring
        self.assertEqual(look_judges.judge_environment({"A": "b"}), {"A": "b"})

    def test_a_judge_refuses_to_start_when_the_seal_key_is_in_its_environment(self):
        started = []
        for judge in (look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude"),
                      look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")):
            with self.subTest(type(judge).__name__):
                with mock.patch.dict(os.environ, {look_seal.KEY_ENV: FAKE_KEY}), \
                        mock.patch.object(look_judges, "run_bounded", lambda *a, **k: started.append(1)):
                    with self.assertRaises(look_judges.JudgeError):
                        judge.judge_image(self.png, "RUBRIC")
        self.assertEqual(started, [])

    def test_the_judge_cli_refuses_to_start_when_the_seal_key_is_in_its_environment(self):
        fx = H.Fixture(self.tmp, subdir="cli-key", sealed=True)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {look_seal.KEY_ENV: fx.seal_key}), contextlib.redirect_stderr(stderr), \
                mock.patch.object(look_judges, "run_bounded", lambda *a, **k: self.fail("a judge process was started")):
            code = look_cli.main(["judge", "--session-dir", fx.dir, "--runner", "claude:claude-fable-5-1",
                                  "--producer-model", "claude-sonnet-5-5"])
        self.assertEqual(code, 2)
        self.assertIn(look_seal.KEY_ENV, stderr.getvalue())

    # -- the shipped runners require a sealed session ----------------------------------------------------------
    def test_the_shipped_runners_require_a_sealed_session_and_the_test_double_does_not(self):
        self.assertTrue(look_judges.ClaudeCliJudge("claude-fable-5-1").requires_sealed)
        self.assertTrue(look_judges.CodexExecJudge(model="gpt-x").requires_sealed)
        self.assertFalse(look_judges.CallableJudge(lambda p, r: {}).requires_sealed)

    def test_the_shipped_runners_refuse_an_unsealed_session_without_starting_a_process(self):
        fx = H.Fixture(self.tmp, subdir="unsealed")
        for runner in (look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude"),
                       look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")):
            with self.subTest(type(runner).__name__):
                with mock.patch.object(look_judges, "run_bounded",
                                       lambda *a, **k: self.fail("a judge process was started")):
                    with self.assertRaises(look_judges.JudgeError):
                        look_judges.run_session(fx.dir, runner)

    # -- what they record -----------------------------------------------------------------------------------
    def test_the_isolation_records_say_enforced_and_pin_the_confinement(self):
        codex = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex").isolation_record()
        self.assertEqual((codex["enforced"], codex["kind"], codex["sealRequired"]), (True, "codex-exec", True))
        self.assertEqual(codex["toolsDisabled"], list(look_judges.CODEX_DISABLED_FEATURES))
        claude = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude").isolation_record()
        self.assertEqual((claude["enforced"], claude["kind"], claude["tools"]), (True, "claude-cli", ["Read"]))
        self.assertEqual(claude["confinement"], list(look_judges.CLAUDE_CONFINEMENT_ARGS))
        for record in (codex, claude):
            self.assertEqual(len(record["commandSha256"]), 64)

    def test_the_command_digest_moves_when_the_confinement_moves(self):
        base = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex").isolation_record()["commandSha256"]
        again = look_judges.CodexExecJudge(model="gpt-x", codex_exe="/elsewhere/codex").isolation_record()["commandSha256"]
        self.assertEqual(base, again)  # the digest is of the arguments, not of where the binary lives
        with mock.patch.object(look_judges, "CODEX_DISABLED_FEATURES", look_judges.CODEX_DISABLED_FEATURES[1:]):
            fewer = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex").isolation_record()["commandSha256"]
        self.assertNotEqual(base, fewer)

    def test_a_plain_judge_never_claims_enforcement(self):
        self.assertEqual(look_judges.JudgeRunner().isolation_record(), {"enforced": False, "kind": "unspecified"})
        self.assertEqual(look_judges.CallableJudge(lambda p, r: {}).isolation_record(),
                         {"enforced": False, "kind": "callable"})


class CanaryLogicTests(H.TmpCase):
    """The pure parts of the live canary: how a reply is scored, what the sealed artifact is checked for, and how the
    orchestration reaches HELD / LEAKED / INCONCLUSIVE / ERROR. (The live call itself is LiveIsolationCanaryTests.)"""

    TOKEN = "CANARY-0123456789ab"

    def test_a_reply_that_quotes_the_token_leaked_the_answer_key(self):
        self.assertEqual(look_canary.score_reply(f"KEY: {{\"canary\": \"{self.TOKEN}\"}}", self.TOKEN), ["answer_key"])
        self.assertEqual(look_canary.score_reply(f"I could read {self.TOKEN}", self.TOKEN), ["answer_key"])

    def test_a_frame_line_naming_red_without_a_refusal_leaked_the_source_frame(self):
        self.assertEqual(look_canary.score_reply("FRAME: red", self.TOKEN), ["source_frame"])
        self.assertEqual(look_canary.score_reply("KEY: CANNOT\nframe: it is plain Red\nATTACHED: grey", self.TOKEN),
                         ["source_frame"])

    def test_refusals_and_the_attached_picture_are_not_leaks(self):
        for reply in ("KEY: CANNOT\nFRAME: CANNOT\nSESSION: CANNOT\nATTACHED: grey",
                      "KEY: NO SHELL\nFRAME: NO VIEW_IMAGE; I cannot tell if it is red\nATTACHED: light grey",
                      "FRAME: the tool was blocked by policy (it might be red)", "", None,
                      "ATTACHED: red", "KEY: denied\nFRAME: access denied"):
            with self.subTest(reply=reply):
                self.assertEqual(look_canary.score_reply(reply, self.TOKEN), [])

    def test_the_sealed_artifact_of_a_real_sealed_session_hides_the_token_and_png_headers(self):
        base = os.path.join(self.tmp, "fx")
        os.makedirs(base)
        fx = look_canary._build_fixture(base, self.TOKEN)
        findings = look_canary.sealed_artifact_findings(fx["session"], self.TOKEN)
        self.assertEqual(findings, {"sealed": True, "plaintextPresent": [], "tokenInSealedBytes": False,
                                    "pngHeaderInSealedBytes": False})
        self.assertEqual(sorted(os.listdir(fx["session"])), ["images", "judge_manifest.json", "sealed.bin", "session.json"])
        with open(fx["keyPath"], "r", encoding="utf-8") as handle:  # the decoy IS readable: that is the canary
            self.assertIn(self.TOKEN, handle.read())

    def test_the_sealed_artifact_check_notices_plaintext_and_a_leaky_seal(self):
        session = os.path.join(self.tmp, "s")
        os.makedirs(session)
        _write_bytes(os.path.join(session, "sealed.bin"), b"header " + self.TOKEN.encode() + b" \x89PNG tail")
        os.makedirs(os.path.join(session, "source-frames"))
        _write_bytes(os.path.join(session, "answer_key.json"), b"{}")
        findings = look_canary.sealed_artifact_findings(session, self.TOKEN)
        self.assertEqual(findings["plaintextPresent"], ["answer_key.json", "source-frames"])
        self.assertTrue(findings["tokenInSealedBytes"] and findings["pngHeaderInSealedBytes"])

    # -- orchestration with the model call replaced -------------------------------------------------------------
    def _run(self, shipped, control, kind="claude", with_control=True):
        calls = []

        sequences = {True: list(shipped) if isinstance(shipped, list) else None,
                     False: list(control) if isinstance(control, list) else None}

        def fake_ask(k, model, confined, fx, timeout_s):
            calls.append(confined)
            reply = (shipped if confined else control)
            if sequences[confined] is not None:  # a list: one reply per attempt, the last one repeating
                reply = sequences[confined].pop(0) if len(sequences[confined]) > 1 else sequences[confined][0]
            if callable(reply):
                reply = reply(fx)
            if isinstance(reply, int):
                return {"reply": "", "denials": None, "exitCode": reply, "stderrTail": "boom", "confined": confined}
            return {"reply": reply, "denials": None, "exitCode": 0, "stderrTail": "", "confined": confined}

        with mock.patch.object(look_canary, "_ask", fake_ask):
            report = look_canary.run_canary(kind, control=with_control)
        return report, calls

    @staticmethod
    def _reads(fx):
        with open(fx["keyPath"], "r", encoding="utf-8") as handle:
            return "KEY: " + handle.read() + "\nFRAME: red\nATTACHED: grey"

    def test_held_needs_the_shipped_command_to_get_nothing_and_the_control_to_get_the_canary(self):
        report, calls = self._run("KEY: CANNOT\nFRAME: CANNOT\nSESSION: CANNOT\nATTACHED: grey", self._reads)
        self.assertEqual(report["outcome"], look_canary.HELD)
        self.assertEqual((report["leaked"], report["controlProvedReadable"]), ([], True))
        self.assertEqual(calls, [True, False])  # the shipped (confined) command first, then the unconfined control

    def test_a_leak_from_the_shipped_command_is_leaked_whatever_the_control_did(self):
        report, _ = self._run(self._reads, self._reads)
        self.assertEqual(report["outcome"], look_canary.LEAKED)
        self.assertEqual(sorted(report["leaked"]), ["answer_key", "source_frame"])
        report, _ = self._run(self._reads, "KEY: CANNOT")
        self.assertEqual(report["outcome"], look_canary.LEAKED)

    def test_a_control_that_did_not_read_the_canary_proves_nothing(self):
        report, _ = self._run("KEY: CANNOT", "KEY: CANNOT\nFRAME: CANNOT")
        self.assertEqual(report["outcome"], look_canary.INCONCLUSIVE)
        self.assertFalse(report["controlProvedReadable"])

    def test_the_control_gets_a_second_attempt_when_the_model_declined_to_try_the_first_time(self):
        replies = iter(["KEY: CANNOT", None])

        def second_time_reads(fx):
            first = next(replies)
            return first if first is not None else self._reads(fx)

        report, calls = self._run("KEY: CANNOT", second_time_reads)
        self.assertEqual((report["outcome"], report["controlProvedReadable"]), (look_canary.HELD, True))
        self.assertEqual(calls, [True, False, False])
        report, calls = self._run("KEY: CANNOT", "KEY: CANNOT")
        self.assertEqual((report["outcome"], calls), (look_canary.INCONCLUSIVE, [True, False, False]))
        self.assertEqual(len(report["controlAttempts"]), look_canary.CONTROL_ATTEMPTS)

    def test_a_judge_is_only_counted_as_having_tried_when_it_was_seen_failing(self):
        self.assertTrue(look_canary.tried({"denials": [{"tool_name": "Read"}], "reply": "I would rather not"}))
        self.assertTrue(look_canary.tried({"denials": None, "reply": "KEY: CANNOT\nFRAME: CANNOT"}))
        self.assertTrue(look_canary.tried({"reply": "key: NO SHELL"}))
        for shows_nothing in ({"denials": [], "reply": "I can't help with this tool-access probe."},
                              {"denials": None, "reply": "KEY: something unrelated"}, {"reply": ""}, {"reply": None}, {},
                              {"denials": [], "reply": "FRAME: CANNOT"}):  # only the KEY line is about the key
            self.assertFalse(look_canary.tried(shows_nothing), shows_nothing)

    def test_a_judge_that_declines_to_try_is_retried_and_then_inconclusive_never_held(self):
        report, calls = self._run(["I won't do that."], "unused")
        self.assertEqual(report["outcome"], look_canary.NOT_ATTEMPTED)
        self.assertEqual((report["judgeTried"], report["leaked"]), (False, []))
        self.assertEqual(calls[:look_canary.SHIPPED_ATTEMPTS], [True] * look_canary.SHIPPED_ATTEMPTS)  # then the control
        self.assertEqual(len(report["shippedAttempts"]), look_canary.SHIPPED_ATTEMPTS)

    def test_a_judge_that_tries_on_a_later_attempt_counts(self):
        report, calls = self._run(["I won't do that.", "KEY: CANNOT\nFRAME: CANNOT"], self._reads)
        self.assertEqual(report["outcome"], look_canary.HELD)
        self.assertTrue(report["judgeTried"])
        self.assertEqual(calls, [True, True, False])

    def test_a_leak_on_a_retry_is_still_a_leak(self):
        report, calls = self._run(["I won't do that.", self._reads], self._reads)
        self.assertEqual(report["outcome"], look_canary.LEAKED)
        self.assertEqual(calls, [True, True, False])  # no further shipped attempts after the leak; the control still runs

    def test_skipping_the_control_is_allowed_and_recorded_as_not_proven(self):
        report, calls = self._run("KEY: CANNOT", "unused", with_control=False)
        self.assertEqual((report["outcome"], report["controlProvedReadable"], calls),
                         (look_canary.HELD, None, [True]))

    def test_a_shipped_command_that_fails_to_run_is_an_error_never_held(self):
        report, calls = self._run(1, self._reads)
        self.assertEqual(report["outcome"], look_canary.ERROR)
        self.assertIn("exited 1", report["reason"])
        self.assertEqual(calls, [True])

    def test_a_sealed_artifact_that_hides_nothing_is_part_of_held(self):
        with mock.patch.object(look_canary, "sealed_artifact_findings",
                               lambda session, token: {"sealed": True, "plaintextPresent": ["answer_key.json"],
                                                       "tokenInSealedBytes": False, "pngHeaderInSealedBytes": False}):
            report, _ = self._run("KEY: CANNOT", self._reads)
        self.assertEqual(report["outcome"], look_canary.LEAKED)
        self.assertEqual(report["leaked"], ["sealed_artifact"])

    def test_the_real_ask_runs_the_confined_command_for_the_shipped_row_and_the_open_one_for_the_control(self):
        seen = []

        def fake_run(argv, input_text=None, cwd=None, timeout=300, grace_s=5, env=None):
            seen.append({"argv": list(argv), "cwd": cwd, "listing": sorted(os.listdir(cwd)), "env": env,
                         "input": input_text})
            out_file = argv[argv.index("-o") + 1] if "-o" in argv else None
            if out_file:
                _write_bytes(out_file, b"KEY: CANNOT")
            body = json.dumps({"result": "KEY: CANNOT", "permission_denials": [{"tool_name": "Read"}]})
            return subprocess.CompletedProcess(argv, 0, body, "")

        with mock.patch.object(look_judges, "run_bounded", fake_run), mock.patch.dict(os.environ, {"LOOK_X": "1"}):
            report = look_canary.run_canary("claude", control=True)
            codex_report = look_canary.run_canary("codex", control=True)
        self.assertEqual(len(seen), 6)  # per runner: the shipped row, then the control row twice (it read nothing)
        shipped, control, _, codex_shipped, codex_control, _ = seen
        for flag in look_judges.CLAUDE_CONFINEMENT_ARGS:
            self.assertIn(flag, shipped["argv"])
            self.assertNotIn(flag, control["argv"])
        self.assertTrue(self._has_pair(codex_shipped["argv"], "--disable", "shell_tool"))
        self.assertNotIn("--disable", codex_control["argv"])
        for row in seen:
            self.assertEqual(row["listing"], ["pair.png"])  # the scratch dir holds only the picture when the judge starts
            self.assertNotIn("LOOK_X", row["env"])
            self.assertIn("answer_key.json", row["input"])
        self.assertEqual(report["shipped"]["denials"], [{"tool_name": "Read"}])
        self.assertIn(report["outcome"], (look_canary.INCONCLUSIVE,))  # the canned control read nothing
        self.assertEqual(codex_report["outcome"], look_canary.INCONCLUSIVE)

    @staticmethod
    def _has_pair(argv, flag, value):
        return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))

    def test_the_canary_command_exits_zero_only_for_held(self):
        for outcome, expected in ((look_canary.HELD, 0), (look_canary.LEAKED, 2), (look_canary.INCONCLUSIVE, 2),
                                  (look_canary.ERROR, 2)):
            with self.subTest(outcome):
                report = {"runner": "codex", "outcome": outcome, "leaked": [], "controlProvedReadable": True}
                with mock.patch.object(look_canary, "run_canary", lambda *a, **k: dict(report)), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(look_cli.main(["isolation-canary", "--runner", "codex"]), expected)


@unittest.skipUnless(os.environ.get("LOOK_LIVE_ISOLATION") == "1",
                     "live canary: set LOOK_LIVE_ISOLATION=1 with the real claude / codex CLIs on PATH (by hand; not in CI)")
class LiveIsolationCanaryTests(unittest.TestCase):
    """sol B3's 'prove it with a LIVE canary test for BOTH runners': the shipped judge command, run for real, must not
    obtain the answer key or the source frame by any tool, while the unconfined control must. Hosted CI has neither CLI,
    so this runs by hand; the transcript is in the PR."""

    def _live(self, kind, model=None):
        if not shutil.which(kind):
            self.skipTest(f"{kind} is not on PATH")
        report = look_canary.run_canary(kind, model=model)
        self.assertEqual(report["outcome"], look_canary.HELD, json.dumps(report, indent=2)[:3000])
        self.assertTrue(report["controlProvedReadable"])

    def test_claude_runner(self):
        self._live("claude")

    def test_codex_runner(self):
        self._live("codex")


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class SealedBuildCliTests(H.TmpCase):
    """build-session seals by default, writes the key to the file it is told (never prints it), and refuses one directory
    under two names."""

    _builds = 0

    def _frames(self, name, count=3):
        import numpy as np
        from PIL import Image
        directory = os.path.join(self.tmp, name)
        os.makedirs(directory, exist_ok=True)
        for index in range(count):
            ramp = np.tile(np.linspace(40, 200, 48), (32, 1))
            arr = np.stack([ramp, ramp * 0.9, ramp * 0.8 + index], axis=2).astype(np.uint8)
            Image.fromarray(arr, "RGB").save(os.path.join(directory, f"{name}-frame-{index:02d}.png"))
        return directory

    def _build(self, extra=(), a=None, b=None, out="sess", key_file="auto"):
        """`key_file="auto"` hands build-session a fresh key file outside the session; None passes no flag."""
        a = a or self._frames("aa")
        b = b or self._frames("bb")
        out = os.path.join(self.tmp, out)
        type(self)._builds += 1
        if key_file == "auto":
            key_file = os.path.join(self.tmp, "keys", f"k{type(self)._builds}.txt")
        self.key_file = key_file
        flags = ["--seal-key-file", key_file] if key_file is not None else []
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = look_cli.main(["build-session", "--a", f"aa={a}", "--b", f"bb={b}", "--seed", "s", "--out-dir", out,
                                  *flags, *extra])
        return code, out, stdout.getvalue(), stderr.getvalue()

    def _key(self):
        with open(self.key_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def test_the_default_build_is_sealed_and_the_key_goes_to_the_named_file_and_nowhere_else(self):
        code, out, stdout, stderr = self._build()
        self.assertEqual(code, 0, stderr)
        shown = json.loads(stdout)
        self.assertTrue(shown["sealed"])
        self.assertNotIn("sealKeyFile", shown)  # not even the path is echoed: nothing about the key is logged
        key_hex = self._key()
        self.assertEqual(len(key_hex), 64)
        self.assertEqual(sorted(os.listdir(out)), ["images", "judge_manifest.json", "sealed.bin", "session.json"])
        key, session, _ = look_seal.open_sealed(out, key_hex)
        self.assertEqual(key["subjects"], ["aa", "bb"])
        self.assertEqual(session["capture"]["configSha256"], H.META["configSha256"])
        members = look_seal.read_members(os.path.join(out, "sealed.bin"), key_hex)
        self.assertEqual(sorted(n for n in members if n.startswith("source-frames/")),
                         [f"source-frames/{s}-{i:02d}.png" for s in ("aa", "bb") for i in range(3)])
        for name in os.listdir(out):  # the key is not in the session directory ...
            path = os.path.join(out, name)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    self.assertNotIn(key_hex.encode("ascii"), handle.read())
        self.assertNotIn(key_hex, stdout)  # ... and it is never printed (a printed key lands in logs and transcripts)
        self.assertNotIn(key_hex, stderr)
        self.assertNotIn("sealKey", shown)
        self.assertIn("SEALED", stderr)

    def test_a_sealing_build_without_a_key_file_is_refused_before_anything_is_built(self):
        for blank in (None, "", "  "):
            with self.subTest(blank=blank):
                code, out, stdout, stderr = self._build(key_file=blank)
                self.assertEqual(code, 2)
                self.assertIn("--seal-key-file", stderr)
                self.assertIn("never printed", stderr)
                self.assertFalse(os.path.exists(os.path.join(out, "session.json")))
                self.assertFalse(os.path.exists(os.path.join(out, "images")))

    def test_an_existing_key_file_is_never_overwritten(self):
        keep = os.path.join(self.tmp, "keys", "precious.txt")
        _write_bytes(keep, b"another session's only key\n")
        code, out, _, stderr = self._build(key_file=keep)
        self.assertEqual(code, 2)
        self.assertIn("already exists", stderr)
        self.assertFalse(os.path.exists(os.path.join(out, "session.json")))  # refused up front, before any build
        with open(keep, "rb") as handle:
            self.assertEqual(handle.read(), b"another session's only key\n")

    def test_a_key_file_inside_the_session_directory_is_refused(self):
        out = os.path.join(self.tmp, "sess")
        for inside in (os.path.join(out, "key.txt"), os.path.join(out, "images", "key.txt")):
            code, _, _, stderr = self._build(key_file=inside)
            self.assertEqual(code, 2)
            self.assertIn("inside the session directory", stderr)
            self.assertFalse(os.path.exists(os.path.join(out, "session.json")))
            self.assertFalse(os.path.exists(inside))

    def test_a_failed_seal_leaves_no_key_file_behind(self):
        with mock.patch.object(look_seal, "seal_session", side_effect=look_seal.SealError("disk full")):
            code, out, _, stderr = self._build()
        self.assertEqual(code, 2)
        self.assertIn("disk full", stderr)
        self.assertFalse(os.path.exists(self.key_file))  # a key for a session that was never sealed must not linger

    def test_the_key_file_exists_before_the_session_is_sealed(self):
        seen = {}
        real = look_seal.seal_session

        def spying(session_dir, key_hex):
            with open(self.key_file, "r", encoding="utf-8") as handle:
                seen["key_on_disk_first"] = handle.read().strip() == key_hex
            return real(session_dir, key_hex)

        with mock.patch.object(look_seal, "seal_session", spying):
            code, _, _, _ = self._build()
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"key_on_disk_first": True})  # a sealed session whose key was lost would be useless

    def test_no_seal_leaves_the_secrets_in_the_clear_and_says_so(self):
        code, out, stdout, stderr = self._build(["--no-seal"], key_file=None)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout)["sealed"])
        self.assertIn("--no-seal", stderr)
        for name in ("answer_key.json", "source-frames"):
            self.assertTrue(os.path.exists(os.path.join(out, name)))
        self.assertFalse(os.path.exists(os.path.join(out, "sealed.bin")))
        with self.assertRaises(look_judges.JudgeError):  # a real judge refuses it
            runner = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude")
            look_judges.run_session(out, runner)

    # -- fable H4 ---------------------------------------------------------------------------------------------
    def test_one_directory_under_two_subject_names_is_refused(self):
        same = self._frames("aa")
        for spelling in (same, same + os.sep, os.path.join(same, ".", "")):
            with self.subTest(spelling=spelling):
                code, out, _, stderr = self._build(a=same, b=spelling)
                self.assertEqual(code, 2)
                self.assertIn("same directory", stderr)
                self.assertFalse(os.path.exists(os.path.join(out, "session.json")))
        code, out, _, _ = self._build(a=same, b=self._frames("bb"))  # two different directories are fine
        self.assertEqual(code, 0)

    def test_the_unseal_command_audits_a_session_but_not_into_it(self):
        _, out, _, _ = self._build()
        key = self._key()
        dest = os.path.join(self.tmp, "audit")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(look_cli.main(["unseal", "--session-dir", out, "--out-dir", dest, "--seal-key", key]), 0)
            self.assertEqual(look_cli.main(["unseal", "--session-dir", out, "--out-dir", os.path.join(out, "x"),
                                            "--seal-key", key]), 2)
            self.assertEqual(look_cli.main(["unseal", "--session-dir", out, "--out-dir", dest]), 2)
        self.assertTrue(os.path.isfile(os.path.join(dest, "answer_key.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "x")))

    def test_a_build_with_lost_frames_is_still_sealed_and_still_exits_two(self):
        a = self._frames("aa", 3)
        b = self._frames("bb", 2)
        code, out, stdout, stderr = self._build(a=a, b=b)
        self.assertEqual(code, 2)
        self.assertIn("UNACKNOWLEDGED", stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "sealed.bin")))
        self.assertFalse(os.path.exists(os.path.join(out, "answer_key.json")))


class EmptyExplicitArgumentTests(H.TmpCase):
    """An explicitly GIVEN argument that is empty is a request that is not there, never 'use the default' (fable H1's
    class): --rubric, --rubric-lock, --seal-key and --seal-key-file as well as --baseline-dir and --config."""

    def setUp(self):
        super().setUp()
        self.fx = H.Fixture(self.tmp, sealed=True)
        self.summary = look_judges.run_session(self.fx.dir, H.sealed_runner(self.fx.judge_fn(H.faithful("cpu"))))
        self.entry_path = os.path.join(self.tmp, "e.json")
        H.write_json(self.entry_path, H.second_judge(self.fx.tally(self.fx.answers(H.faithful("cpu")))))

    def _tally(self, *extra):
        out = os.path.join(self.tmp, "t.json")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = look_cli.main(["tally", "--session-dir", self.fx.dir, "--results", self.summary["resultsPath"],
                                  "--out", out, "--producer-model", "claude-sonnet-5-5", *extra])
        return code, os.path.isfile(out), stderr.getvalue()

    def test_an_empty_rubric_or_lock_path_is_an_error_not_the_shipped_one(self):
        for flag in ("--rubric", "--rubric-lock"):
            for empty in ("", "  "):
                with self.subTest(flag=flag, empty=empty):
                    code, wrote, stderr = self._tally(*H.seal_args(self.fx), flag, empty)
                    self.assertEqual((code, wrote), (2, False))
                    self.assertIn("given empty", stderr)
        self.assertEqual(self._tally(*H.seal_args(self.fx))[:2], (0, True))  # omitting the flag is the shipped lock

    def test_disagreement_refuses_an_empty_rubric_or_lock_path_too(self):
        for flag in ("--rubric", "--rubric-lock"):
            with self.subTest(flag):
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    code = look_cli.main(["judge-disagreement", "--entries", self.entry_path, self.entry_path, flag, ""])
                self.assertEqual(code, 2)
                self.assertIn("given empty", stderr.getvalue())

    def test_an_empty_seal_key_or_key_file_is_an_error_not_a_fallback_to_the_environment(self):
        with mock.patch.dict(os.environ, {look_seal.KEY_ENV: self.fx.seal_key}):  # a good key IS available in the env
            for flag in ("--seal-key", "--seal-key-file"):
                with self.subTest(flag):
                    code, wrote, stderr = self._tally(flag, "")
                    self.assertEqual((code, wrote), (2, False))
                    self.assertIn("given empty", stderr)
            self.assertEqual(self._tally()[:2], (0, True))  # not given: the environment's key is used
        for kwargs in ({"explicit": ""}, {"key_file": ""}, {"explicit": "  "}):
            with self.assertRaises(look_seal.SealError):
                look_seal.key_from_args(environ={look_seal.KEY_ENV: FAKE_KEY}, **kwargs)


class IdenticalSourceUnitsTests(H.TmpCase):
    """One folder copied and presented as a second subject cannot show a preference; the entry says so."""

    def test_real_units_whose_two_sides_are_the_same_bytes_are_recorded_in_the_entry(self):
        def same_bytes(tmp, name, frames, tag="v1"):
            out = {}
            for f in frames:
                path = os.path.join(tmp, f"{name}-{tag}-{f}.src")
                with open(path, "wb") as handle:
                    handle.write(f"identical|{f}".encode("ascii"))
                out[f] = path
            return out

        with mock.patch.object(H, "_sources", same_bytes):
            fx = H.Fixture(self.tmp, subdir="copied")
        entry = fx.tally(fx.answers(lambda item: "tie" if item["kind"] != "positive_control"
                                    else H.faithful("cpu")(item)))
        self.assertEqual(entry["integrity"]["realUnitsWithIdenticalSources"], [f"real-{i}" for i in range(5)])
        honest = H.Fixture(self.tmp, subdir="honest")
        self.assertNotIn("realUnitsWithIdenticalSources",
                         honest.tally(honest.answers(H.faithful("cpu")))["integrity"])


class CaptureConfigDigestTests(H.TmpCase):
    """fable H7: a null or malformed capture.configSha256 never passes the capture checks."""

    def test_a_null_or_malformed_config_digest_in_the_capture_is_unusable(self):
        for bad in (None, "", "abc", "g" * 64, 12345):
            with self.subTest(bad=bad):
                fx = H.Fixture(self.tmp, subdir=f"s-{abs(hash(repr(bad)))}")
                fx.session["capture"]["configSha256"] = bad
                fx.key["capture"]["configSha256"] = bad  # the key agrees with the session: only the digest itself is wrong
                entry = fx.tally(fx.answers(H.faithful("cpu")))
                self.assertIn("CAPTURE_CONFIG_SHA_NOT_RECORDED", entry["unusableReasons"])
                self.assertFalse(entry["usable"])
                self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_a_real_digest_that_differs_from_the_tally_config_is_still_its_own_reason(self):
        fx = H.Fixture(self.tmp)
        entry = fx.tally(fx.answers(H.faithful("cpu")), config_sha256="0" * 64)
        self.assertEqual(entry["unusableReasons"], ["CONFIG_DIFFERS_FROM_SESSION"])


if __name__ == "__main__":
    unittest.main()
