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

    def test_the_public_session_does_not_carry_the_order_seed(self):
        # fable r2 H3: with the seed and the pair images a reader re-derives each item's kind, ordering and slot
        self._seal()
        with open(os.path.join(self.fx.dir, "session.json"), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("orderSeed", text)
        self.assertNotIn(H.SEED, text)
        self.assertNotIn("orderSeed", look_seal.PUBLIC_SESSION_FIELDS)
        key, full, _ = look_seal.open_sealed(self.fx.dir, self.key_hex)  # ... and the seal still holds it
        self.assertEqual((key["orderSeed"], full["orderSeed"]), (H.SEED, H.SEED))
        for name in os.listdir(self.fx.dir):  # nor does any other judge-readable file in the session directory
            path = os.path.join(self.fx.dir, name)
            if os.path.isfile(path) and name != "sealed.bin":
                with open(path, "rb") as handle:
                    self.assertNotIn(H.SEED.encode("ascii"), handle.read(), name)

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
        for field, value in (("rubricSha256", "0" * 64), ("itemCount", 99),
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
        with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"), calls)) as runner:  # a shipped runner: it needs a seal
            with self.assertRaises(look_judges.JudgeError) as caught:
                look_judges.run_session(self.fx.dir, runner)
        self.assertIn("not sealed", str(caught.exception))
        self.assertEqual(calls, [])

    def test_a_judge_runner_refuses_plaintext_secrets_left_next_to_the_sealed_file(self):
        self._seal()
        for name in ("answer_key.json", "source-frames", "degraded-sources", "ANSWER_KEY.JSON"):
            with self.subTest(name):
                path = os.path.join(self.fx.dir, name)
                if name.lower() == "answer_key.json":
                    _write_bytes(path, b"{}")
                else:
                    os.makedirs(path)
                calls = []
                with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"), calls)) as runner:
                    with self.assertRaises(look_judges.JudgeError) as caught:
                        look_judges.run_session(self.fx.dir, runner)
                self.assertIn("plaintext", str(caught.exception))
                self.assertEqual(calls, [])
                if name.lower() == "answer_key.json":
                    os.remove(path)
                else:
                    os.rmdir(path)

    def test_a_sealed_session_runs_and_the_results_record_the_runner_class_not_a_claim_of_isolation(self):
        self._seal()
        calls = []
        with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"), calls)) as runner:
            summary = look_judges.run_session(self.fx.dir, runner)
        self.assertEqual(summary["errors"], 0)
        doc = H.read_json(summary["resultsPath"])
        self.assertEqual(doc["runner"], H.RUNNER)  # class, model, CLI version and command digest, measured by the harness
        for claim in ("isolation", "sealSha256", "judgedUnderSeal", "enforced"):
            self.assertNotIn(claim, doc)  # nothing in the file for a tally to be tempted to believe

    def test_a_runner_outside_the_closed_set_is_recorded_as_unlisted_whatever_it_calls_itself(self):
        summary = look_judges.run_session(self.fx.dir, H.CallableJudge(self.fx.judge_fn(H.faithful("cpu"))))
        doc = H.read_json(summary["resultsPath"])
        self.assertTrue(doc["runner"]["class"].startswith("UNLISTED:"), doc["runner"])
        self.assertIsNone(doc["runner"]["cliVersion"])

        class ClaudeCliJudge(H.CallableJudge):  # a look-alike borrowing the listed NAME
            pass
        liar = ClaudeCliJudge(self.fx.judge_fn(H.faithful("cpu")), "liar", "claude-fable-5-1", "anthropic")
        self.assertTrue(look_judges.runner_record(liar)["class"].startswith("UNLISTED:"))
        sub = type("Sub", (look_judges.ClaudeCliJudge,), {})("claude-fable-5-1")  # nor can a subclass of a listed one
        self.assertTrue(look_judges.runner_record(sub)["class"].startswith("UNLISTED:"))

    def test_verdicts_stored_for_another_cli_version_or_command_are_judged_again(self):
        self._seal()
        calls = []
        with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"), calls)) as runner:
            look_judges.run_session(self.fx.dir, runner)
            self.assertEqual(look_judges.run_session(self.fx.dir, runner)["staleRejected"], 0)  # unchanged: kept
            with mock.patch.object(look_judges.ClaudeCliJudge, "cli_version", lambda self: "9.9.10-upgraded"):
                again = look_judges.run_session(self.fx.dir, runner)
        self.assertEqual((again["staleRejected"], len(calls)), (14, 28))
        self.assertEqual(set(H.read_json(again["resultsPath"])["staleRejected"].values()),
                         {"RUNNER_DIFFERS_FROM_CURRENT_RUN"})

    def test_the_seal_key_in_the_environment_stops_a_real_judge_before_anything_runs(self):
        self._seal()
        calls = []
        with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"), calls)) as runner:
            with mock.patch.dict(os.environ, {look_seal.KEY_ENV: self.key_hex}):
                with self.assertRaises(look_judges.JudgeError):
                    look_judges.run_session(self.fx.dir, runner)
                with self.assertRaises(look_judges.JudgeError):
                    look_judges.assert_no_seal_key_in_environment()
        self.assertEqual(calls, [])
        look_judges.assert_no_seal_key_in_environment({})

    def test_every_path_comparison_is_case_folded_on_a_case_insensitive_host(self):  # fable r2 H4
        session = os.path.join(self.tmp, "Sess")
        key_file = os.path.join(self.tmp, "sess", "k.txt")  # neither exists: the typed case is all there is
        with mock.patch.object(os.path, "normcase", lambda p: p.lower()):  # what ntpath does on Windows
            self.assertTrue(look_seal.path_inside(key_file, session))
            self.assertEqual(look_seal.canon_path(session), look_seal.canon_path(os.path.join(self.tmp, "SESS")))
            with self.assertRaises(ValueError):
                look_cli._refuse_key_file_in_session(key_file, session)
            with self.assertRaises(ValueError):
                look_cli._refuse_same_directory(session, os.path.join(self.tmp, "SESS"), "x")
            with self.assertRaises(look_seal.SealError):
                look_seal.extract_all(session, self.key_hex, key_file)
            with self.assertRaises(look_judges.JudgeError):
                look_judges.assert_isolated(os.path.join(self.tmp, "SESS", "work"), [session])
        with mock.patch.object(os.path, "normcase", lambda p: p):  # a case-sensitive host: two different directories
            self.assertFalse(look_seal.path_inside(key_file, session))
            look_cli._refuse_key_file_in_session(key_file, session)

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
    """The tally refuses a judge that could have read the key, a seal it cannot verify, and an isolation it could not derive.
    API first, then the CLI path that carries the key. NOTHING here reads an `isolation` / `enforced` / `sealSha256` claim
    from the judge side: the seal comes from sealed.bin and the key, the isolation from the runner CLASS."""

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
        for bad in ({"verified": False, "sealSha256": H.SEAL_SHA, "plaintextPresent": []},
                    {"verified": True, "sealSha256": "short", "plaintextPresent": []},
                    {"verified": True, "plaintextPresent": []}, "yes", []):
            with self.subTest(bad=bad):
                self.assertIn("SEAL_NOT_VERIFIED", self.fx.tally(self._answers(), seal=bad)["unusableReasons"])

    def test_a_plaintext_secret_beside_the_seal_at_tally_time_is_unusable(self):
        entry = self.fx.tally(self._answers(), seal=dict(H.SEAL_OK, plaintextPresent=["answer_key.json"]))
        self.assertEqual(entry["unusableReasons"], ["PLAINTEXT_SECRETS_BESIDE_THE_SEAL"])

    def test_the_isolation_is_derived_from_the_runner_class_and_recorded_with_the_command_and_cli_version(self):
        entry = self.fx.tally(self._answers())
        self.assertTrue(entry["usable"], entry["unusableReasons"])
        self.assertEqual(entry["sealSha256"], H.SEAL_SHA)
        iso = entry["judgeIsolation"]
        self.assertEqual((iso["enforced"], iso["runnerClass"], iso["kind"], iso["cliVersion"]),
                         (True, "ClaudeCliJudge", "claude-cli", H.CLI_VERSION))
        self.assertEqual(iso["commandSha256"], look_judges.ClaudeCliJudge("claude-fable-5-1").command_sha256())
        codex = H.listed_runner(look_judges.CodexExecJudge, "gpt-6.1-sol")
        entry = self.fx.tally(self._answers(), runner=codex, canary=H.canary_for(codex))
        self.assertTrue(entry["usable"], entry["unusableReasons"])
        self.assertEqual((entry["judgeIsolation"]["kind"], entry["family"], entry["judgeId"]),
                         ("codex-exec", "openai", "codex-exec:openai:gpt:sol"))

    def test_a_runner_outside_the_closed_set_is_never_usable_whatever_it_says(self):  # sol r2 B1, at the API
        for cls in ("UNLISTED:test.CallableJudge", "CallableJudge", "ClaudeCliJudge ", "claudeclijudge", None, 7, ""):
            with self.subTest(cls=cls):
                runner = dict(H.RUNNER, **{"class": cls, "isolation": {"enforced": True, "kind": "external"},
                                           "enforced": True})
                entry = self.fx.tally(self._answers(), runner=runner)
                self.assertEqual(entry["unusableReasons"], ["RUNNER_NOT_ALLOWLISTED"])
                self.assertEqual(entry["preference"]["winner"], "UNUSABLE")
                self.assertFalse(entry["judgeIsolation"]["enforced"])
        self.assertEqual(set(look_judges.RUNNER_CLASSES), {"ClaudeCliJudge", "CodexExecJudge"})
        self.assertFalse(hasattr(look_judges, "CallableJudge"))  # the double lives in the tests; no production path builds one

    def test_a_listed_class_whose_recorded_command_or_version_is_not_the_shipped_one_is_unusable(self):
        for field, value, reason in (("commandSha256", "0" * 64, "RUNNER_COMMAND_DIFFERS_FROM_SHIPPED"),
                                     ("commandSha256", None, "RUNNER_COMMAND_DIFFERS_FROM_SHIPPED"),
                                     ("cliVersion", None, "CLI_VERSION_NOT_RECORDED"),
                                     ("cliVersion", "  ", "CLI_VERSION_NOT_RECORDED")):
            with self.subTest(field=field, value=value):
                entry = self.fx.tally(self._answers(), runner=dict(H.RUNNER, **{field: value}))
                self.assertIn(reason, entry["unusableReasons"])
                self.assertFalse(entry["judgeIsolation"]["enforced"])
        entry = self.fx.tally(self._answers(), runner=dict(H.RUNNER, model="mystery"))
        self.assertIn("JUDGE_MODEL_UNRECOGNISED", entry["unusableReasons"])
        self.assertIn("JUDGE_IS_PRODUCER_OR_UNVERIFIABLE", entry["unusableReasons"])
        self.assertEqual(self.fx.tally(self._answers(), runner=None)["unusableReasons"][0], "RUNNER_NOT_ALLOWLISTED")

    def test_the_identity_is_derived_not_read(self):
        runner = dict(H.RUNNER, judgeId="someone-else", family="openai", model="claude-sonnet-5-5-20261001")
        entry = self.fx.tally(self._answers(), runner=runner, forbidden_models=["claude-opus-5-5"])
        self.assertEqual((entry["judgeId"], entry["family"]), ("claude-cli:anthropic:sonnet", "anthropic"))

    # -- the canary is bound to THIS run ----------------------------------------------------------------------
    def test_without_a_canary_bound_to_this_class_cli_version_and_command_the_entry_is_unusable(self):
        cases = (("no canary", None, ["ISOLATION_CANARY_MISSING"]),
                 ("empty report", {}, ["ISOLATION_CANARY_MISSING"]),
                 ("not a report", "held", ["ISOLATION_CANARY_MISSING"]),
                 ("another CLI version", dict(H.CANARY, cliVersion="9.9.8-older"), ["ISOLATION_CANARY_FOR_ANOTHER_CLI_VERSION"]),
                 ("another command", dict(H.CANARY, commandSha256="0" * 64), ["ISOLATION_CANARY_FOR_ANOTHER_COMMAND"]),
                 ("another runner", dict(H.CANARY, runnerClass="CodexExecJudge"), ["ISOLATION_CANARY_FOR_ANOTHER_RUNNER"]))
        for name, canary, expected in cases:
            with self.subTest(name):
                entry = self.fx.tally(self._answers(), canary=canary)
                self.assertEqual(entry["unusableReasons"], expected)
                self.assertFalse(entry["judgeIsolation"]["enforced"])

    def test_a_canary_that_says_held_but_whose_evidence_does_not_is_unusable(self):
        # the outcome / leaked / controlProvedReadable fields of a report are NEVER read: the raw evidence is re-judged
        leaky = json.loads(json.dumps(H.CANARY))
        leaky["shippedAttempts"][0]["reply"] = "KEY: CANARY-TEST"
        liar = dict(leaky, outcome=look_canary.HELD, leaked=[], controlProvedReadable=True, judgeTried=True)
        self.assertEqual(self.fx.tally(self._answers(), canary=liar)["unusableReasons"], ["ISOLATION_CANARY_NOT_HELD"])

    # -- through the CLI, with a real seal --------------------------------------------------------------------
    def _judge(self):
        with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"))) as runner:
            return look_judges.run_session(self.fx.dir, runner)

    def _tally(self, summary, extra=(), fx=None, canary="auto"):
        fx = fx or self.fx
        out = os.path.join(self.tmp, "t.json")
        stderr = io.StringIO()
        canary_args = ["--canary", H.write_canary(self.tmp)] if canary == "auto" else []
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = look_cli.main(["tally", "--session-dir", fx.dir, "--results", summary["resultsPath"], "--out", out,
                                  "--producer-model", "claude-sonnet-5-5", *canary_args, *extra])
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

    def test_the_cli_tally_without_a_canary_report_is_unusable(self):
        code, entry, _ = self._tally(self._judge(), H.seal_args(self.fx), canary=None)
        self.assertEqual((code, entry["unusableReasons"]), (2, ["ISOLATION_CANARY_MISSING"]))

    def test_the_key_may_come_from_a_file_or_the_environment(self):
        summary = self._judge()
        path = os.path.join(self.tmp, "key.txt")
        _write_bytes(path, (self.fx.seal_key + "\n").encode("ascii"))
        self.assertEqual(self._tally(summary, ["--seal-key-file", path])[0], 0)
        with mock.patch.dict(os.environ, {look_seal.KEY_ENV: self.fx.seal_key}):
            self.assertEqual(self._tally(summary)[0], 0)

    def test_a_plaintext_session_tallies_unusable_whatever_the_verdicts_say(self):
        fx = H.Fixture(self.tmp, subdir="plain")
        summary = look_judges.run_session(fx.dir, H.CallableJudge(fx.judge_fn(H.faithful("cpu"))))
        code, entry, _ = self._tally(summary, fx=fx)
        self.assertEqual(code, 2)
        self.assertIn("SEAL_NOT_VERIFIED", entry["unusableReasons"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_a_judge_that_ran_while_the_key_was_lying_in_the_directory_can_only_be_an_unlisted_runner_and_is_unusable(self):
        fx = H.Fixture(self.tmp, subdir="late", sealed=False)
        calls = []
        with H.shipped_claude(fx.judge_fn(H.faithful("cpu"), calls)) as runner:  # a shipped runner will not start here
            with self.assertRaises(look_judges.JudgeError):
                look_judges.run_session(fx.dir, runner)
        self.assertEqual(calls, [])
        summary = look_judges.run_session(fx.dir, H.CallableJudge(fx.judge_fn(H.faithful("cpu"))))  # only a double can
        fx.seal_key = look_seal.new_key()
        look_seal.seal_session(fx.dir, fx.seal_key)  # sealed only AFTER the judge had run
        code, entry, _ = self._tally(summary, H.seal_args(fx), fx=fx)
        self.assertEqual((code, entry["unusableReasons"]), (2, ["RUNNER_NOT_ALLOWLISTED"]))
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_sols_repro_a_runner_that_asserts_its_own_isolation_is_not_usable_through_the_unchanged_cli(self):
        # r2 B1: an unconfined callable judge read the key during its callback and claimed isolation={"enforced": true}.
        summary = look_judges.run_session(self.fx.dir, H.CallableJudge(self.fx.judge_fn(H.faithful("cpu"))))
        doc = H.read_json(summary["resultsPath"])
        doc.update({"isolation": {"enforced": True, "kind": "external"}, "sealSha256": look_seal.session_state(self.fx.dir)["sealSha256"],
                    "judgedUnderSeal": True})
        doc["runner"]["isolation"] = {"enforced": True}
        H.write_json(summary["resultsPath"], doc)  # and the file even forges the very fields the old tally read
        code, entry, _ = self._tally(summary, H.seal_args(self.fx))
        self.assertEqual((code, entry["unusableReasons"]), (2, ["RUNNER_NOT_ALLOWLISTED"]))
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")
        self.assertEqual(entry["preference"]["withheldWinner"], "cpu")

    def test_a_results_file_that_is_not_the_v2_shape_is_a_typed_refusal_not_a_traceback(self):
        summary = self._judge()
        for broken in ({}, {"schema": "mlv-app/look-judge-results/v1", "items": {}, "judge": {}},
                       {"schema": look_judges.RESULTS_SCHEMA, "judge": {}, "runner": {}},
                       {"schema": look_judges.RESULTS_SCHEMA, "items": {}, "judge": {}}, []):
            with self.subTest(broken=broken):
                H.write_json(summary["resultsPath"], broken)
                code, entry, stderr = self._tally(summary, H.seal_args(self.fx))
                self.assertEqual((code, entry), (2, None))
                self.assertIn("judge results file", stderr)


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
                                  "--producer-model", "claude-sonnet-5-5", *H.seal_args(fx),
                                  "--canary", H.write_canary(self.tmp), *extra])
        return code, (H.read_json(out) if os.path.isfile(out) else None)

    def _judge(self, fx, **kw):
        with H.shipped_claude(fx.judge_fn(H.faithful("cpu"))) as runner:
            return look_judges.run_session(fx.dir, runner, **kw)

    def test_sols_repro_a_separately_locked_session_is_unusable_under_the_shipped_lock(self):
        rubric, lock_path, lock = self._other_rubric()
        fx = H.Fixture(self.tmp, sealed=True, rubric=lock["rubricSha256"])
        summary = self._judge(fx, rubric_path=rubric, lock_path=lock_path)
        self.assertEqual(summary["errors"], 0)
        code, entry = self._tally_cli(fx, summary)  # the default: the lock that ships
        self.assertEqual(code, 2)
        self.assertEqual(entry["unusableReasons"], ["SESSION_RUBRIC_DIFFERS_FROM_LOCK"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")
        self.assertEqual(entry["preference"]["withheldWinner"], "cpu")

    def test_an_explicitly_passed_lock_that_equals_the_sessions_and_the_verdicts_digest_is_accepted(self):
        rubric, lock_path, lock = self._other_rubric()
        fx = H.Fixture(self.tmp, sealed=True, rubric=lock["rubricSha256"])
        summary = self._judge(fx, rubric_path=rubric, lock_path=lock_path)
        code, entry = self._tally_cli(fx, summary, ["--rubric", rubric, "--rubric-lock", lock_path])
        self.assertEqual((code, entry["usable"]), (0, True))
        self.assertEqual(entry["rubricLockSha256"], lock["rubricSha256"])

    def test_an_explicit_lock_that_the_session_does_not_match_is_unusable(self):
        rubric, lock_path, lock = self._other_rubric()
        fx = H.Fixture(self.tmp, sealed=True)  # frozen under the SHIPPED rubric
        summary = self._judge(fx)
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
        summary = self._judge(fx)
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
                         [{"code": "ENTRY_RUBRIC_DIFFERS_FROM_LOCK", "judges": [a["judgeId"], b["judgeId"]]}])
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
    """A comparison needs TWO DIFFERENT judges, whatever spelling their model names use, and the SAME score grid. One judge
    counted twice agrees with itself; a criterion only one of them scored must never read as agreement."""

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
        self.assertEqual((verdict["identityProblems"], verdict["coverageProblems"]), ([], []))
        self.assertFalse(verdict["thirdJudgeNeeded"])

    def test_the_same_entry_twice_is_not_a_comparison(self):  # sol r1's exact repro: judge-disagreement --entries T.json T.json
        verdict = self._verdict(self.a, self.a)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(sorted(self._codes(verdict)), ["DUPLICATE_JUDGE_ID", "DUPLICATE_MODEL", "DUPLICATE_RESULTS"])
        self.assertIsNone(verdict["thirdJudgeNeeded"])  # not a reassuring False

    def test_sols_r2_repro_one_model_under_two_spellings_is_one_model(self):
        spellings = ("sonnet", "claude-sonnet-5-5", "Claude Sonnet 5.5", "CLAUDE-SONNET-5-5-20261001", " sonnet[1m] ",
                     "us.anthropic.claude-sonnet-5-5-v1:0", "claude-sonnet-4-5")
        base = H.second_judge(self.a, model="sonnet")
        for spelling in spellings:
            with self.subTest(spelling):
                other = H.second_judge(self.a, model=spelling)  # its own results digest; the id is derived, so it equals
                self.assertIn("DUPLICATE_MODEL", self._codes(self._verdict(base, other)))
                self.assertFalse(self._verdict(base, other)["comparable"])
                # and a hand-edited, DIFFERENT judgeId cannot make the same model two judges either
                other["judgeId"] = "claude-cli:" + spelling
                codes = self._codes(self._verdict(base, other))
                self.assertIn("DUPLICATE_MODEL", codes)

    def test_the_duplicate_model_check_also_covers_the_other_family(self):
        sol, luna = (H.second_judge(self.a, model=m) for m in ("gpt-6.1-sol", "gpt-6.1-luna"))
        self.assertTrue(self._verdict(sol, luna)["comparable"])  # two OpenAI models that are not one model
        for same in ("gpt-6.1-sol", "GPT-6.1-SOL", "gpt-6.2-sol", "gpt-6.1", "codex-default", "codex", "openai"):
            with self.subTest(same):
                self.assertIn("DUPLICATE_MODEL", self._codes(self._verdict(sol, H.second_judge(self.a, model=same))))

    def test_one_judge_id_under_two_models_or_one_model_under_two_ids_is_not_a_comparison(self):
        same_id = dict(self.b, judgeId=self.a["judgeId"])
        self.assertIn("DUPLICATE_JUDGE_ID", self._codes(self._verdict(self.a, same_id)))
        self.assertIn("JUDGE_ID_DOES_NOT_MATCH_MODEL", self._codes(self._verdict(self.a, same_id)))
        same_model = H.second_judge(self.a, model=self.a["model"])
        self.assertEqual(sorted(self._codes(self._verdict(self.a, same_model))), ["DUPLICATE_JUDGE_ID", "DUPLICATE_MODEL"])

    def test_the_judge_id_must_be_the_one_the_resolver_derives_from_the_model(self):
        for bad in ("fable", "claude-cli:fable", "claude-cli:claude-haiku-4-5", " ", None, 7):
            with self.subTest(bad=bad):
                self.assertIn("JUDGE_ID_DOES_NOT_MATCH_MODEL", self._codes(self._verdict(self.a, dict(self.b, judgeId=bad))))

    def test_a_copied_results_file_under_a_new_name_is_not_a_second_judge(self):
        copied = H.second_judge(self.a)
        copied["resultsSha256"] = self.a["resultsSha256"]
        self.assertEqual(self._codes(self._verdict(self.a, copied)), ["DUPLICATE_RESULTS"])

    def test_fewer_than_two_entries_is_not_a_comparison(self):
        for entries in ([], [self.a]):
            verdict = self._verdict(*entries) if entries else look_tally.judge_disagreement([], 1.0, rubric_lock=H.LOCK)
            self.assertFalse(verdict["comparable"])

    def test_missing_identity_fields_are_not_comparable(self):
        for field, code in (("model", "MODEL_UNRECOGNISED"), ("judgeId", "JUDGE_ID_DOES_NOT_MATCH_MODEL"),
                            ("resultsSha256", "RESULTS_DIGEST_MISSING"), ("family", "FAMILY_DOES_NOT_MATCH_MODEL")):
            with self.subTest(field):
                broken = json.loads(json.dumps(self.b))
                del broken[field]
                verdict = self._verdict(self.a, broken)
                self.assertFalse(verdict["comparable"])
                self.assertIn(code, self._codes(verdict))

    def test_an_unrecognised_model_is_refused_in_every_spelling_with_no_claim_needed(self):
        for model in ("mystery", "", "   ", None, 5, ["sonnet"], {"m": "sonnet"}, "sonnet5", "sonnetsonnet", "sonnet opus",
                      "gpt-5 sonnet", "claude", "ｓｏｎｎｅｔ", "sonnеt", "claude-sonet-5-5"):
            with self.subTest(model=model):
                odd = dict(self.b, model=model)
                verdict = self._verdict(self.a, odd)
                self.assertFalse(verdict["comparable"])
                self.assertIn("MODEL_UNRECOGNISED", self._codes(verdict))

    def test_the_family_is_derived_from_the_model_always_not_only_when_cross_family_is_claimed(self):
        liar = dict(self.b, family="openai")  # a Claude model that says it is OpenAI; nobody claimed cross-family
        verdict = self._verdict(self.a, liar)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(self._codes(verdict), ["FAMILY_DOES_NOT_MATCH_MODEL"])
        self.assertEqual(self._codes(self._verdict(self.a, dict(self.b, family=None))), ["FAMILY_DOES_NOT_MATCH_MODEL"])

    def test_a_cross_family_claim_in_an_entry_changes_nothing_and_the_flag_needs_two_derived_families(self):
        claimed = dict(self.a, crossFamilyStatus="CROSS_FAMILY_PROVEN_LIVE")  # a free-text field no code reads any more
        self.assertTrue(self._verdict(claimed, self.b)["comparable"])
        verdict = self._verdict(self.a, self.b, require_cross_family=True)
        self.assertEqual(self._codes(verdict), ["CROSS_FAMILY_REQUIRED_BUT_NOT_DISTINCT_FAMILIES"])
        openai = H.second_judge(self.a, model="gpt-6.1-sol")
        self.assertTrue(self._verdict(self.a, openai, require_cross_family=True)["comparable"])
        liar = dict(self.b, family="openai")  # claims a second family; the model says Claude
        self.assertIn("FAMILY_DOES_NOT_MATCH_MODEL", self._codes(self._verdict(self.a, liar, require_cross_family=True)))

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

    def test_an_entry_that_says_usable_without_the_facts_that_make_it_usable_is_not_comparable(self):
        for name, change in (("unlisted class", {"judgeIsolation": dict(self.b["judgeIsolation"], runnerClass="UNLISTED:x.Y")}),
                             ("not enforced", {"judgeIsolation": dict(self.b["judgeIsolation"], enforced=False)}),
                             ("no isolation", {"judgeIsolation": None}), ("no seal", {"sealSha256": None}),
                             ("reasons left", {"unusableReasons": ["SLOT_BIAS"]}),
                             ("reasons missing", {"unusableReasons": None}),
                             ("withheld winner", {"preference": {"winner": "UNUSABLE"}})):
            with self.subTest(name):
                forged = dict(self.b, **change)
                self.assertIn("ENTRY_USABLE_WITHOUT_THE_FACTS_THAT_MAKE_IT_SO", self._codes(self._verdict(self.a, forged)))
                self.assertFalse(self._verdict(self.a, forged)["comparable"])
        unusable = dict(self.b, usable=False, unusableReasons=["SLOT_BIAS"])  # an honest unusable entry is not this problem
        self.assertNotIn("ENTRY_USABLE_WITHOUT_THE_FACTS_THAT_MAKE_IT_SO", self._codes(self._verdict(self.a, unusable)))

    # -- identical subject x criterion coverage ------------------------------------------------------------------
    def test_a_criterion_one_judge_did_not_score_cannot_hide_a_disagreement(self):
        far = H.second_judge(self.a)
        far["scores"]["cpu"]["colour_cast"] += 3  # a plain disagreement is found ...
        self.assertTrue(self._verdict(self.a, far)["thirdJudgeNeeded"])
        for hide in ("delete", "null"):  # ... and removing / nulling the criterion must not make it disappear
            with self.subTest(hide):
                hidden = json.loads(json.dumps(far))
                if hide == "delete":
                    del hidden["scores"]["cpu"]["colour_cast"]
                else:
                    hidden["scores"]["cpu"]["colour_cast"] = None
                verdict = self._verdict(self.a, hidden)
                self.assertFalse(verdict["comparable"])
                self.assertIsNone(verdict["thirdJudgeNeeded"])  # NOT a reassuring False
                self.assertTrue(verdict["coverageProblems"])

    def test_a_subject_only_one_judge_has_is_a_gap_too(self):
        extra = json.loads(json.dumps(self.b))
        extra["scores"]["gpu"] = dict(extra["scores"]["cpu"])
        verdict = self._verdict(self.a, extra)
        self.assertFalse(verdict["comparable"])
        self.assertEqual([p["code"] for p in verdict["coverageProblems"]], ["SUBJECT_GRID_DIFFERS"])
        self.assertEqual([p["code"] for p in self._verdict(extra, self.a)["coverageProblems"]], ["SUBJECT_GRID_DIFFERS"])

    def test_scores_that_are_not_numbers_are_not_a_grid(self):
        for bad in ("3", True, [3], {"v": 3}):
            with self.subTest(bad=bad):
                odd = json.loads(json.dumps(self.b))
                odd["scores"]["cpu"]["colour_cast"] = bad
                verdict = self._verdict(self.a, odd)
                self.assertFalse(verdict["comparable"])
                self.assertEqual([p["code"] for p in verdict["coverageProblems"]], ["SCORES_NOT_THE_FULL_GRID"])
        extra_criterion = json.loads(json.dumps(self.b))
        extra_criterion["scores"]["cpu"]["vibes"] = 3
        self.assertFalse(self._verdict(self.a, extra_criterion)["comparable"])
        both = json.loads(json.dumps(self.a))  # ... and the SAME unknown criterion in both is still not the grid
        both["scores"]["cpu"]["vibes"] = 3
        verdict = self._verdict(both, extra_criterion)
        self.assertEqual((verdict["comparable"], [p["code"] for p in verdict["coverageProblems"]]),
                         (False, ["SCORES_NOT_THE_FULL_GRID"]))

    def test_a_criterion_that_is_null_for_both_judges_is_not_a_gap(self):
        self.assertIsNone(self.a["scores"]["cpu"]["skin"])  # the fixture's judges both found no skin
        self.assertIsNone(self.b["scores"]["cpu"]["skin"])
        self.assertEqual(self._verdict(self.a, self.b)["coverageProblems"], [])

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
        code, shown = self._cli(self.a, self.a)  # sol r1's repro through the real CLI
        self.assertEqual((code, shown["comparable"], shown["thirdJudgeNeeded"]), (2, False, None))
        self.assertIn("DUPLICATE_JUDGE_ID", json.dumps(shown["identityProblems"]))

    def test_the_cli_refuses_one_model_under_two_spellings_and_a_gap(self):  # sol r2 B2 and fable r2 C, through the real CLI
        code, shown = self._cli(H.second_judge(self.a, model="sonnet"), H.second_judge(self.a, model="claude-sonnet-5-5"))
        self.assertEqual((code, shown["comparable"], shown["thirdJudgeNeeded"]), (2, False, None))
        self.assertIn("DUPLICATE_MODEL", json.dumps(shown["identityProblems"]))
        gap = json.loads(json.dumps(self.b))
        del gap["scores"]["cpu"]["colour_cast"]
        code, shown = self._cli(self.a, gap)
        self.assertEqual((code, shown["comparable"]), (2, False))
        self.assertTrue(shown["coverageProblems"])

    def test_the_cli_require_cross_family_flag(self):
        self.assertEqual(self._cli(self.a, self.b, extra=["--require-cross-family"])[0], 2)
        openai = H.second_judge(self.a, model="gpt-6.1-sol")
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
    def test_the_shipped_runners_refuse_an_unsealed_session_without_starting_a_process(self):
        fx = H.Fixture(self.tmp, subdir="unsealed")
        for runner in (look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe="claude"),
                       look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")):
            with self.subTest(type(runner).__name__):
                with mock.patch.object(look_judges, "run_bounded",
                                       lambda *a, **k: self.fail("a judge process was started")):
                    with self.assertRaises(look_judges.JudgeError):
                        look_judges.run_session(fx.dir, runner)

    # -- the closed runner set -------------------------------------------------------------------------------
    def test_the_closed_set_is_exactly_the_two_shipped_runners_and_they_take_no_way_to_widen_their_confinement(self):
        self.assertEqual(look_judges.RUNNER_CLASSES, {"ClaudeCliJudge": look_judges.ClaudeCliJudge,
                                                      "CodexExecJudge": look_judges.CodexExecJudge})
        for build in (lambda: look_judges.ClaudeCliJudge("sonnet", extra_args=["--add-dir", "/"]),
                      lambda: look_judges.ClaudeCliJudge("sonnet", judge_id="mine"),
                      lambda: look_judges.CodexExecJudge("gpt-x", judge_id="mine"),
                      lambda: look_judges.ClaudeCliJudge("sonnet", requires_sealed=False)):
            with self.assertRaises(TypeError):
                build()
        self.assertFalse(hasattr(look_judges.JudgeRunner, "requires_sealed"))
        self.assertFalse(hasattr(look_judges.JudgeRunner, "isolation_record"))

    def test_a_shipped_runner_takes_its_identity_from_the_resolver_and_refuses_an_unplaceable_model(self):
        for model, judge_id in (("sonnet", "claude-cli:anthropic:sonnet"), ("claude-sonnet-5-5", "claude-cli:anthropic:sonnet"),
                                ("Claude Fable 5.1", "claude-cli:anthropic:fable")):
            self.assertEqual(look_judges.ClaudeCliJudge(model).identity()["judgeId"], judge_id)
        self.assertEqual(look_judges.CodexExecJudge("gpt-6.1-sol").identity(),
                         {"judgeId": "codex-exec:openai:gpt:sol", "model": "gpt-6.1-sol", "family": "openai"})
        self.assertEqual(look_judges.CodexExecJudge(None, codex_home=self.tmp).judge_id, "codex-exec:openai:*")  # unresolved
        for build in (look_judges.ClaudeCliJudge, look_judges.CodexExecJudge):
            for model in ("mystery", "claude-sonet-5-5", "sonnet opus"):
                with self.subTest(build=build.__name__, model=model):
                    with self.assertRaises(look_judges.ProducerJudgeError):
                        build(model)

    def test_the_command_digest_describes_the_confinement_not_the_model_or_the_binary(self):
        for cls, a, b in ((look_judges.CodexExecJudge, "gpt-x", "gpt-6.1-sol"),
                          (look_judges.ClaudeCliJudge, "sonnet", "claude-haiku-4-5")):
            base = cls(a).command_sha256()
            self.assertEqual(len(base), 64)
            self.assertEqual(base, cls(b).command_sha256())
            self.assertEqual(base, (cls(a, codex_exe="/elsewhere/x") if cls is look_judges.CodexExecJudge
                                    else cls(a, claude_exe="/elsewhere/x")).command_sha256())
        self.assertNotEqual(look_judges.CodexExecJudge("gpt-x").command_sha256(),
                            look_judges.ClaudeCliJudge("sonnet").command_sha256())

    def test_the_command_digest_moves_when_the_confinement_moves(self):
        base = look_judges.CodexExecJudge("gpt-x").command_sha256()
        with mock.patch.object(look_judges, "CODEX_DISABLED_FEATURES", look_judges.CODEX_DISABLED_FEATURES[1:]):
            self.assertNotEqual(base, look_judges.CodexExecJudge("gpt-x").command_sha256())
        claude = look_judges.ClaudeCliJudge("sonnet").command_sha256()
        with mock.patch.object(look_judges, "CLAUDE_CONFINEMENT_ARGS", look_judges.CLAUDE_CONFINEMENT_ARGS[1:]):
            self.assertNotEqual(claude, look_judges.ClaudeCliJudge("sonnet").command_sha256())

    def test_the_cli_version_is_measured_by_running_the_cli_and_a_failure_is_not_a_version(self):
        judge = look_judges.ClaudeCliJudge("sonnet", claude_exe="claude")
        ok = subprocess.CompletedProcess(["claude"], 0, "2.1.286 (Claude Code)\n", "")
        with mock.patch.object(look_judges, "run_bounded", lambda *a, **k: ok):
            self.assertEqual(judge.cli_version(), "2.1.286 (Claude Code)")
        for bad in (subprocess.CompletedProcess(["claude"], 1, "", "boom"), subprocess.CompletedProcess(["claude"], 0, "  ", "")):
            with mock.patch.object(look_judges, "run_bounded", lambda *a, **k: bad):
                with self.assertRaises(look_judges.JudgeError):
                    judge.cli_version()
        with mock.patch.object(look_judges, "run_bounded", side_effect=OSError("no such file")):
            with self.assertRaises(look_judges.JudgeError):
                judge.cli_version()

    def test_run_session_records_the_measured_version_and_refuses_to_run_when_it_cannot_be_measured(self):
        fx = H.Fixture(self.tmp, subdir="v", sealed=True)
        calls = []
        with H.shipped_claude(fx.judge_fn(H.faithful("cpu"), calls)) as runner:
            with mock.patch.object(look_judges.ClaudeCliJudge, "cli_version",
                                   side_effect=look_judges.JudgeError("no version")):
                with self.assertRaises(look_judges.JudgeError):
                    look_judges.run_session(fx.dir, runner)
        self.assertEqual(calls, [])

    def test_the_tally_derivation_is_the_same_for_a_runner_record_the_harness_wrote(self):
        for cls, model in ((look_judges.ClaudeCliJudge, "sonnet"), (look_judges.CodexExecJudge, "gpt-6.1-sol")):
            record = H.listed_runner(cls, model)
            iso, reasons = look_judges.derive_isolation(record)
            self.assertEqual((reasons, iso["runnerClass"], iso["kind"]), ([], cls.__name__, cls.kind))
            self.assertFalse(iso["enforced"])  # only the tally, after the canary check, may say so
            self.assertEqual(look_judges.derived_identity(record), cls(model).identity())
        self.assertEqual(look_judges.derived_identity({"class": "Nope", "model": "x"}),
                         {"judgeId": "unlisted:unrecognised", "model": "x", "family": "unknown"})
        self.assertEqual(look_judges.derived_identity(None)["family"], "unknown")


class CanaryLogicTests(H.TmpCase):
    """The pure parts of the live canary: how a reply is scored, what the sealed artifact is checked for, how a report is
    RE-JUDGED from its raw evidence, and how the orchestration reaches HELD / LEAKED / INCONCLUSIVE / ERROR. (The live call
    itself is LiveIsolationCanaryTests.)"""

    TOKEN = "CANARY-0123456789ab"
    DENIED = {"reply": "KEY: CANNOT\nFRAME: CANNOT", "denials": [{"tool_name": "Read"}]}
    REFUSED_BY_CLI = {"reply": "KEY: CANNOT", "stderrTail": "2026 ERROR codex_core::tools::router: error=code-mode host is disabled"}
    DECLINED = {"reply": "KEY: CANNOT\nFRAME: CANNOT\nSESSION: CANNOT\nATTACHED: grey", "denials": None}

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(look_judges._CliJudge, "cli_version", lambda self: H.CLI_VERSION)
        patch.start()
        self.addCleanup(patch.stop)

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

    # -- "tried": only the CLI's own record counts --------------------------------------------------------------
    def test_a_judge_is_only_counted_as_having_tried_when_the_cli_recorded_a_refused_call(self):
        self.assertTrue(look_canary.tried({"denials": [{"tool_name": "Read"}], "reply": "I would rather not"}))
        self.assertTrue(look_canary.tried(self.REFUSED_BY_CLI))
        self.assertTrue(look_canary.tried({"stderrTail": "tool call denied by policy"}))
        # fable r2 H2: the model's OWN words do not count; the prompt tells it to write CANNOT, so declining says the same
        for shows_nothing in ({"denials": None, "reply": "KEY: CANNOT\nFRAME: CANNOT"}, {"reply": "key: NO SHELL"},
                              {"denials": [], "reply": "I can't help with this tool-access probe."},
                              {"denials": None, "reply": "KEY: something unrelated", "stderrTail": "ordinary log line"},
                              {"reply": ""}, {"reply": None}, {}, {"denials": [], "reply": "FRAME: CANNOT"}):
            self.assertFalse(look_canary.tried(shows_nothing), shows_nothing)

    # -- orchestration with the model call replaced -------------------------------------------------------------
    def _run(self, shipped, control, kind="claude", model=None):
        calls = []
        sequences = {True: list(shipped) if isinstance(shipped, list) else None,
                     False: list(control) if isinstance(control, list) else None}

        def fake_ask(judge, confined, fx, timeout_s):
            calls.append(confined)
            reply = shipped if confined else control
            if sequences[confined] is not None:  # a list: one reply per attempt, the last one repeating
                reply = sequences[confined].pop(0) if len(sequences[confined]) > 1 else sequences[confined][0]
            if callable(reply):
                reply = reply(fx)
            if isinstance(reply, int):
                return {"reply": "", "denials": None, "exitCode": reply, "stderrTail": "boom", "confined": confined}
            if isinstance(reply, str):
                reply = {"reply": reply, "denials": None}
            return dict({"denials": None, "exitCode": 0, "stderrTail": "", "confined": confined}, **reply)

        with mock.patch.object(look_canary, "_ask", fake_ask):
            report = look_canary.run_canary(kind, model=model)
        return report, calls

    def _reads(self, fx, key=True, frame=True):
        with open(fx["keyPath"], "r", encoding="utf-8") as handle:
            return {"reply": ("KEY: " + handle.read() if key else "KEY: CANNOT") + ("\nFRAME: red" if frame else "\nFRAME: CANNOT")
                    + "\nATTACHED: grey"}

    def _reads_key_only(self, fx):
        return self._reads(fx, frame=False)

    def _reads_frame_only(self, fx):
        return self._reads(fx, key=False)

    def test_held_needs_the_shipped_command_seen_failing_and_the_control_to_read_both_decoys(self):
        report, calls = self._run(self.DENIED, self._reads)
        self.assertEqual(report["outcome"], look_canary.HELD)
        self.assertEqual((report["leaked"], report["judgeTried"], report["controlRead"]),
                         ([], True, ["answer_key", "source_frame"]))
        self.assertEqual(calls, [True, False])  # the shipped (confined) command first, then the unconfined control

    def test_the_report_is_bound_to_the_runner_class_the_cli_version_and_the_command(self):
        report, _ = self._run(self.DENIED, self._reads, model="claude-haiku-4-5")
        self.assertEqual((report["runnerClass"], report["cliVersion"], report["model"]),
                         ("ClaudeCliJudge", H.CLI_VERSION, "claude-haiku-4-5"))
        self.assertEqual(report["commandSha256"], look_judges.ClaudeCliJudge("sonnet").command_sha256())  # model-free
        codex, _ = self._run(self.REFUSED_BY_CLI, self._reads, kind="codex", model="gpt-x")
        self.assertEqual((codex["runnerClass"], codex["commandSha256"]),
                         ("CodexExecJudge", look_judges.CodexExecJudge("gpt-x").command_sha256()))

    def test_a_leak_from_the_shipped_command_is_leaked_whatever_the_control_did(self):
        report, _ = self._run(self._reads, self._reads)
        self.assertEqual(report["outcome"], look_canary.LEAKED)
        self.assertEqual(sorted(report["leaked"]), ["answer_key", "source_frame"])
        report, _ = self._run(self._reads, "KEY: CANNOT")
        self.assertEqual(report["outcome"], look_canary.LEAKED)

    def test_sols_r2_hardening_a_control_that_read_only_the_frame_does_not_prove_the_key_readable(self):
        # the saved Codex control: KEY: CANNOT / FRAME: Red. It used to set controlProvedReadable and permit HELD.
        report, _ = self._run(self.DENIED, "KEY: CANNOT\nFRAME: red")
        self.assertEqual(report["outcome"], look_canary.INCONCLUSIVE)
        self.assertEqual(report["controlRead"], ["source_frame"])
        report, _ = self._run(self.DENIED, self._reads_key_only)
        self.assertEqual((report["outcome"], report["controlRead"]), (look_canary.INCONCLUSIVE, ["answer_key"]))

    def test_each_decoy_has_to_be_shown_readable_on_its_own_and_the_attempts_may_split_them(self):
        report, calls = self._run(self.DENIED, [self._reads_key_only, self._reads_frame_only])
        self.assertEqual((report["outcome"], calls), (look_canary.HELD, [True, False, False]))
        report, calls = self._run(self.DENIED, "KEY: CANNOT\nFRAME: CANNOT")
        self.assertEqual((report["outcome"], calls), (look_canary.INCONCLUSIVE, [True] + [False] * look_canary.CONTROL_ATTEMPTS))
        self.assertEqual(len(report["controlAttempts"]), look_canary.CONTROL_ATTEMPTS)

    def test_the_control_always_runs_and_there_is_no_way_to_skip_it(self):
        import inspect
        self.assertNotIn("control", inspect.signature(look_canary.run_canary).parameters)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):  # `--no-control` no longer exists
                look_cli.main(["isolation-canary", "--runner", "claude", "--no-control"])
        report, calls = self._run(self.DENIED, "unused")
        self.assertNotEqual(report["outcome"], look_canary.HELD)
        self.assertIn(False, calls)

    def test_a_judge_that_declines_to_try_is_retried_and_then_inconclusive_never_held(self):  # sol r2 / fable r2 H2
        report, calls = self._run(self.DECLINED, self._reads)
        self.assertEqual(report["outcome"], look_canary.NOT_ATTEMPTED)
        self.assertEqual((report["judgeTried"], report["leaked"]), (False, []))
        self.assertEqual(calls[:look_canary.SHIPPED_ATTEMPTS], [True] * look_canary.SHIPPED_ATTEMPTS)  # then the control
        self.assertEqual(len(report["shippedAttempts"]), look_canary.SHIPPED_ATTEMPTS)
        report, _ = self._run(["I won't do that."], self._reads)
        self.assertEqual(report["outcome"], look_canary.NOT_ATTEMPTED)

    def test_a_judge_that_tries_on_a_later_attempt_counts(self):
        report, calls = self._run([self.DECLINED, self.REFUSED_BY_CLI], self._reads)
        self.assertEqual(report["outcome"], look_canary.HELD)
        self.assertTrue(report["judgeTried"])
        self.assertEqual(calls, [True, True, False])

    def test_a_leak_on_a_retry_is_still_a_leak(self):
        report, calls = self._run([self.DECLINED, self._reads], self._reads)
        self.assertEqual(report["outcome"], look_canary.LEAKED)
        self.assertEqual(calls, [True, True, False])  # no further shipped attempts after the leak; the control still runs

    def test_a_shipped_command_that_fails_to_run_is_an_error_never_held(self):
        report, calls = self._run(1, self._reads)
        self.assertEqual(report["outcome"], look_canary.ERROR)
        self.assertIn("exited 1", report["reason"])
        self.assertEqual(calls[0], True)

    def test_a_sealed_artifact_that_hides_nothing_is_part_of_held(self):
        with mock.patch.object(look_canary, "sealed_artifact_findings",
                               lambda session, token: {"sealed": True, "plaintextPresent": ["answer_key.json"],
                                                       "tokenInSealedBytes": False, "pngHeaderInSealedBytes": False}):
            report, _ = self._run(self.DENIED, self._reads)
        self.assertEqual(report["outcome"], look_canary.LEAKED)
        self.assertEqual(report["leaked"], ["sealed_artifact"])

    # -- a report is RE-JUDGED from its raw evidence --------------------------------------------------------------
    def test_judge_canary_never_reads_a_field_that_says_held(self):
        held, _ = self._run(self.DENIED, self._reads)
        self.assertEqual(look_canary.judge_canary(held)["outcome"], look_canary.HELD)
        forged = json.loads(json.dumps(held))
        forged["shippedAttempts"][0]["reply"] = "KEY: " + forged["token"]
        forged.update({"outcome": look_canary.HELD, "leaked": [], "judgeTried": True, "controlRead": ["answer_key", "source_frame"]})
        self.assertEqual(look_canary.judge_canary(forged)["outcome"], look_canary.LEAKED)
        for mutate, expected in ((lambda r: r.update(controlAttempts=[]), look_canary.INCONCLUSIVE),
                                 (lambda r: r.update(shippedAttempts=[]), look_canary.ERROR),
                                 (lambda r: r["shippedAttempts"][0].update(denials=None), look_canary.NOT_ATTEMPTED),
                                 (lambda r: r["shippedAttempts"][0].update(exitCode=2), look_canary.ERROR),
                                 (lambda r: r.update(sealedArtifact=None), look_canary.LEAKED),
                                 (lambda r: r["sealedArtifact"].pop("tokenInSealedBytes"), look_canary.LEAKED),
                                 (lambda r: r["sealedArtifact"].update(sealed=False), look_canary.LEAKED)):
            with self.subTest(expected=expected):
                broken = json.loads(json.dumps(held))
                mutate(broken)
                self.assertEqual(look_canary.judge_canary(broken)["outcome"], expected)

    def test_verify_canary_accepts_only_a_held_report_bound_to_this_run(self):
        isolation = look_judges.derive_isolation(H.RUNNER)[0]
        self.assertEqual(look_canary.verify_canary(H.CANARY, isolation), [])
        self.assertEqual(look_canary.verify_canary(None, isolation), ["ISOLATION_CANARY_MISSING"])
        self.assertEqual(look_canary.verify_canary({"shippedAttempts": []}, isolation), ["ISOLATION_CANARY_MISSING"])
        for field, code in (("runnerClass", "ISOLATION_CANARY_FOR_ANOTHER_RUNNER"),
                            ("cliVersion", "ISOLATION_CANARY_FOR_ANOTHER_CLI_VERSION"),
                            ("commandSha256", "ISOLATION_CANARY_FOR_ANOTHER_COMMAND")):
            for bad in ("other", None, ""):
                with self.subTest(field=field, bad=bad):
                    self.assertEqual(look_canary.verify_canary(dict(H.CANARY, **{field: bad}), isolation), [code])
        leaky = json.loads(json.dumps(H.CANARY))
        leaky["controlAttempts"] = []
        self.assertEqual(look_canary.verify_canary(leaky, isolation), ["ISOLATION_CANARY_NOT_HELD"])
        self.assertEqual(look_canary.verify_canary(H.CANARY, dict(isolation, cliVersion=None)),
                         ["ISOLATION_CANARY_FOR_ANOTHER_CLI_VERSION"])

    def test_the_real_ask_runs_the_confined_command_for_the_shipped_row_and_the_open_one_for_the_control(self):
        seen = []

        def fake_run(argv, input_text=None, cwd=None, timeout=300, grace_s=5, env=None):
            seen.append({"argv": list(argv), "cwd": cwd, "listing": sorted(os.listdir(cwd)), "env": env,
                         "input": input_text})
            out_file = argv[argv.index("-o") + 1] if "-o" in argv else None
            stderr = ""
            if out_file:
                _write_bytes(out_file, b"KEY: CANNOT")
                stderr = "ERROR codex_core::tools::router: error=code-mode host is disabled"
            body = json.dumps({"result": "KEY: CANNOT", "permission_denials": [{"tool_name": "Read"}]})
            return subprocess.CompletedProcess(argv, 0, body, stderr)

        with mock.patch.object(look_judges, "run_bounded", fake_run), mock.patch.dict(os.environ, {"LOOK_X": "1"}):
            report = look_canary.run_canary("claude")
            codex_report = look_canary.run_canary("codex")
        per_runner = 1 + look_canary.CONTROL_ATTEMPTS  # seen failing at once; the canned control read nothing: all attempts
        self.assertEqual(len(seen), 2 * per_runner)
        shipped, control = seen[0], seen[1]
        codex_shipped, codex_control = seen[per_runner], seen[per_runner + 1]
        for flag in look_judges.CLAUDE_CONFINEMENT_ARGS:
            self.assertIn(flag, shipped["argv"])
            self.assertNotIn(flag, control["argv"])
        self.assertTrue(self._has_pair(codex_shipped["argv"], "--disable", "shell_tool"))
        self.assertNotIn("--disable", codex_control["argv"])
        for row in seen:
            self.assertEqual(row["listing"], ["pair.png"])  # the scratch dir holds only the picture when the judge starts
            self.assertNotIn("LOOK_X", row["env"])
            self.assertIn("answer_key.json", row["input"])
        self.assertEqual(report["shippedAttempts"][0]["denials"], [{"tool_name": "Read"}])
        self.assertEqual(report["outcome"], look_canary.INCONCLUSIVE)  # the canned control read nothing
        self.assertEqual((codex_report["outcome"], codex_report["judgeTried"]), (look_canary.INCONCLUSIVE, True))

    @staticmethod
    def _has_pair(argv, flag, value):
        return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))

    def test_the_canary_command_exits_zero_only_for_held(self):
        for outcome, expected in ((look_canary.HELD, 0), (look_canary.LEAKED, 2), (look_canary.INCONCLUSIVE, 2),
                                  (look_canary.NOT_ATTEMPTED, 2), (look_canary.ERROR, 2)):
            with self.subTest(outcome):
                report = {"runner": "codex", "outcome": outcome, "leaked": []}
                with mock.patch.object(look_canary, "run_canary", lambda *a, **k: dict(report)), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(look_cli.main(["isolation-canary", "--runner", "codex"]), expected)


@unittest.skipUnless(os.environ.get("LOOK_LIVE_ISOLATION") == "1",
                     "live canary: set LOOK_LIVE_ISOLATION=1 with the real claude / codex CLIs on PATH (by hand; not in CI)")
class LiveIsolationCanaryTests(unittest.TestCase):
    """The shipped judge command, run for real, must not obtain the answer key or the source frame by any tool and must be
    SEEN trying, while the unconfined control must read BOTH decoys. Hosted CI has neither CLI, so this runs by hand; the
    transcript is in the PR."""

    def _live(self, kind, model=None):
        if not shutil.which(kind):
            self.skipTest(f"{kind} is not on PATH")
        report = look_canary.run_canary(kind, model=model)
        self.assertEqual(report["outcome"], look_canary.HELD, json.dumps(report, indent=2)[:3000])

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
        with H.shipped_claude(self.fx.judge_fn(H.faithful("cpu"))) as runner:
            self.summary = look_judges.run_session(self.fx.dir, runner)
        self.canary = H.write_canary(self.tmp)
        self.entry_path = os.path.join(self.tmp, "e.json")
        H.write_json(self.entry_path, H.second_judge(self.fx.tally(self.fx.answers(H.faithful("cpu")))))

    def _tally(self, *extra):
        out = os.path.join(self.tmp, "t.json")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = look_cli.main(["tally", "--session-dir", self.fx.dir, "--results", self.summary["resultsPath"],
                                  "--out", out, "--producer-model", "claude-sonnet-5-5", "--canary", self.canary, *extra])
        return code, os.path.isfile(out), stderr.getvalue()

    def test_an_empty_canary_path_is_an_error_not_no_canary(self):
        code, wrote, stderr = self._tally(*H.seal_args(self.fx), "--canary", "")
        self.assertEqual((code, wrote), (2, False))
        self.assertIn("given empty", stderr)

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
