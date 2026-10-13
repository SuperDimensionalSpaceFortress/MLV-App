import datetime
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "resolve-codex-tier.py"

# The module's filename has a hyphen, which import machinery cannot import by name
# directly; load it by file path instead so these tests exercise the real function,
# not a re-implementation that could drift from it.
_spec = importlib.util.spec_from_file_location("resolve_codex_tier", SCRIPT)
resolve_codex_tier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_codex_tier)


def _now_text(delta=datetime.timedelta(0)):
    moment = datetime.datetime.now(datetime.timezone.utc) + delta
    # The Codex CLI writes nine fractional digits; the fixture does the same.
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def _entry(slug, visibility="list", levels=("low", "medium", "high")):
    return {"slug": slug, "visibility": visibility,
            "supported_reasoning_levels": [{"effort": level, "description": level} for level in levels]}


def _write_cache(path, slugs, fetched_at=None, entries=None):
    """slugs -> ordinary list-visible, high-capable entries; entries -> verbatim entry dicts."""
    models = entries if entries is not None else [_entry(slug) for slug in slugs]
    payload = {"fetched_at": _now_text() if fetched_at is None else fetched_at, "models": models}
    path.write_text(json.dumps(payload), encoding="utf-8")


class ResolveCodexTierUnitTests(unittest.TestCase):
    def test_picks_the_highest_numeric_version_among_tier_matches(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-5.6-sol", "gpt-6-sol", "gpt-5-sol"])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_rejects_a_near_name_tier(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-solar", "gpt-5.6-terra"])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertEqual(error, "no-tier-match:sol")

    def test_fails_closed_on_missing_cache(self):
        missing = Path(self._tmp()) / "does-not-exist.json"
        resolved, error = resolve_codex_tier.resolve("sol", str(missing))
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-not-found:"))

    def test_fails_closed_on_malformed_cache(self):
        cache = Path(self._tmp()) / "models_cache.json"
        cache.write_text("not json", encoding="utf-8")
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-unreadable:"))

    # sol pre-review (LANE-MODEL-CURRENCY-1 round 1a): a mocked open() raising
    # UnicodeDecodeError escaped resolve() because only OSError and json.JSONDecodeError
    # were caught, breaking this function's documented one-typed-error contract. A cache
    # written with invalid UTF-8 bytes exercises the real decode path (no mock), proving
    # this fails closed through the same "cache-unreadable:" channel as any other
    # unreadable cache, instead of raising an uncaught traceback.
    def test_fails_closed_on_non_utf8_cache(self):
        cache = Path(self._tmp()) / "models_cache.json"
        cache.write_bytes(b"\xff\xfe\x00invalid-utf8")
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-unreadable:"))

    def test_fails_closed_on_empty_models_list(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, [])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertEqual(error, "no-tier-match:sol")

    def test_fails_closed_on_models_list_missing_entirely(self):
        cache = Path(self._tmp()) / "models_cache.json"
        cache.write_text(json.dumps({"not_models": []}), encoding="utf-8")
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertEqual(error, "cache-malformed:no-models-list")

    # CODEX-OVERSIZED-VERSION-TOKEN-1 (sol hardening): SLUG_RE has no length cap on the version
    # digits, but CPython's int() conversion refuses a decimal string past its configured digit
    # limit (ValueError, default ~4300 digits) as a DoS guard -- a real-world stand-in is a
    # corrupted or adversarial models_cache.json entry. This must be skipped as a non-candidate,
    # never crash resolve() with an uncaught ValueError, whether or not a valid candidate exists
    # alongside it.
    def test_skips_a_candidate_whose_version_token_exceeds_the_int_conversion_limit(self):
        cache = Path(self._tmp()) / "models_cache.json"
        oversized = "gpt-" + ("9" * 5000) + "-sol"
        _write_cache(cache, [oversized, "gpt-6-sol", "gpt-5-sol"])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_fails_closed_when_only_candidate_has_an_oversized_version_token(self):
        cache = Path(self._tmp()) / "models_cache.json"
        oversized = "gpt-" + ("9" * 5000) + "-sol"
        _write_cache(cache, [oversized])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertEqual(error, "no-tier-match:sol")

    # ---- MODEL-POSTURE-R16-1: doctrine R16 conformance -------------------------------------

    def test_skips_a_hidden_model_even_when_it_is_the_newest(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, None, entries=[_entry("gpt-7-sol", visibility="hide"), _entry("gpt-6-sol")])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_an_entry_with_no_visibility_field_is_not_a_candidate(self):
        cache = Path(self._tmp()) / "models_cache.json"
        entry = _entry("gpt-7-sol")
        del entry["visibility"]
        _write_cache(cache, None, entries=[entry, _entry("gpt-6-sol")])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_skips_a_model_that_cannot_run_at_high_effort(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, None, entries=[_entry("gpt-7-sol", levels=("low", "medium")), _entry("gpt-6-sol")])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_an_entry_with_no_reasoning_levels_is_not_a_candidate(self):
        cache = Path(self._tmp()) / "models_cache.json"
        entry = _entry("gpt-7-sol")
        del entry["supported_reasoning_levels"]
        _write_cache(cache, None, entries=[entry])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertEqual(error, "no-tier-match:sol")

    def test_reasoning_levels_may_be_bare_strings(self):
        cache = Path(self._tmp()) / "models_cache.json"
        entry = {"slug": "gpt-6-sol", "visibility": "list", "supported_reasoning_levels": ["low", "high"]}
        _write_cache(cache, None, entries=[entry])
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_refuses_a_cache_fetched_more_than_seven_days_ago(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-sol"], fetched_at=_now_text(-datetime.timedelta(days=7, minutes=1)))
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-stale:"), error)
        self.assertIn("max_days=7", error)

    def test_accepts_a_cache_fetched_just_inside_seven_days(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-sol"], fetched_at=_now_text(-datetime.timedelta(days=6, hours=23)))
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(resolved, "gpt-6-sol")

    def test_the_age_check_uses_the_injected_clock(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-sol"], fetched_at="2026-10-01T00:00:00Z")
        eight_days = datetime.datetime(2026, 10, 9, 0, 0, 1, tzinfo=datetime.timezone.utc)
        resolved, error = resolve_codex_tier.resolve("sol", str(cache), now=eight_days)
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-stale:"), error)

    def test_fails_closed_when_fetched_at_is_missing(self):
        cache = Path(self._tmp()) / "models_cache.json"
        cache.write_text(json.dumps({"models": [_entry("gpt-6-sol")]}), encoding="utf-8")
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-malformed:no-parseable-fetched_at"), error)

    def test_fails_closed_when_fetched_at_is_unparseable_or_has_no_zone(self):
        for bad in ("yesterday", "2026-10-12T19:15:51", 12345, None):
            cache = Path(self._tmp()) / "models_cache.json"
            payload = {"fetched_at": bad, "models": [_entry("gpt-6-sol")]}
            cache.write_text(json.dumps(payload), encoding="utf-8")
            resolved, error = resolve_codex_tier.resolve("sol", str(cache))
            self.assertIsNone(resolved, bad)
            self.assertTrue(error.startswith("cache-malformed:no-parseable-fetched_at"), (bad, error))

    def test_fails_closed_when_fetched_at_is_far_in_the_future(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-sol"], fetched_at=_now_text(datetime.timedelta(days=3)))
        resolved, error = resolve_codex_tier.resolve("sol", str(cache))
        self.assertIsNone(resolved)
        self.assertTrue(error.startswith("cache-malformed:fetched_at-in-the-future:"), error)

    def test_resolve_with_meta_returns_the_fetched_at_text_verbatim(self):
        cache = Path(self._tmp()) / "models_cache.json"
        stamp = _now_text()
        _write_cache(cache, ["gpt-6-sol"], fetched_at=stamp)
        meta, error = resolve_codex_tier.resolve_with_meta("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(meta, {"model": "gpt-6-sol", "fetchedAt": stamp, "clientVersion": None})

    def test_resolve_with_meta_reports_the_cache_writers_client_version(self):
        # ~/.codex/models_cache.json is shared by every Codex CLI on the machine and each rewrites it
        # with the catalogue its own version was offered, so the writer's version travels with the model.
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-sol"])
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["client_version"] = "0.160.1"
        cache.write_text(json.dumps(payload), encoding="utf-8")
        meta, error = resolve_codex_tier.resolve_with_meta("sol", str(cache))
        self.assertIsNone(error)
        self.assertEqual(meta["clientVersion"], "0.160.1")
        payload["client_version"] = 160
        cache.write_text(json.dumps(payload), encoding="utf-8")
        meta, _ = resolve_codex_tier.resolve_with_meta("sol", str(cache))
        self.assertIsNone(meta["clientVersion"], "a non-string client_version is recorded as unknown, never coerced")

    def test_the_real_catalogue_shape_resolves_the_newest_listed_model_per_tier(self):
        # Shape lifted from the live ~/.codex/models_cache.json (2026-10-12): a 'hide' reserve
        # model, an older sol, and astra/luna families.
        cache = Path(self._tmp()) / "models_cache.json"
        full = ("low", "medium", "high", "xhigh", "max", "ultra")
        entries = [_entry("gpt-6.1-sol", levels=full), _entry("gpt-6-astra", levels=full),
                   _entry("gpt-6-sol", levels=full), _entry("gpt-6-luna"), _entry("gpt-reserve", "hide"),
                   _entry("gpt-5.6-sol", levels=full), _entry("gpt-5.6-luna"), _entry("gpt-5.5", "hide")]
        _write_cache(cache, None, entries=entries)
        got = {t: resolve_codex_tier.resolve(t, str(cache)) for t in ("sol", "astra", "luna")}
        self.assertEqual(got, {"sol": ("gpt-6.1-sol", None), "astra": ("gpt-6-astra", None),
                               "luna": ("gpt-6-luna", None)})

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="resolve-codex-tier-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class ResolveCodexTierCliTests(unittest.TestCase):
    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="resolve-codex-tier-cli-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def test_cli_success_exit_zero_and_json_on_stdout(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-5.6-luna", "gpt-6-luna"])
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--tier", "luna", "--cache", str(cache)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["resolvedModel"], "gpt-6-luna")
        self.assertRegex(payload["fetchedAt"], r"^\d{4}-\d\d-\d\dT")

    def test_cli_fail_closed_exit_nonzero_on_missing_cache(self):
        missing = Path(self._tmp()) / "nope.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--tier", "sol", "--cache", str(missing)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"].startswith("cache-not-found:"))

    def test_cli_skips_oversized_version_token_and_still_emits_one_json_object(self):
        cache = Path(self._tmp()) / "models_cache.json"
        oversized = "gpt-" + ("9" * 5000) + "-sol"
        _write_cache(cache, [oversized, "gpt-6-sol"])
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--tier", "sol", "--cache", str(cache)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["resolvedModel"], "gpt-6-sol")

    def test_cli_fail_closed_exit_nonzero_with_a_typed_error_on_a_stale_cache(self):
        cache = Path(self._tmp()) / "models_cache.json"
        _write_cache(cache, ["gpt-6-sol"], fetched_at=_now_text(-datetime.timedelta(days=30)))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--tier", "sol", "--cache", str(cache)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"].startswith("cache-stale:"), payload)

    def test_cli_fail_closed_exit_nonzero_on_non_utf8_cache(self):
        cache = Path(self._tmp()) / "models_cache.json"
        cache.write_bytes(b"\xff\xfe\x00invalid-utf8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--tier", "sol", "--cache", str(cache)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"].startswith("cache-unreadable:"))


if __name__ == "__main__":
    unittest.main()
