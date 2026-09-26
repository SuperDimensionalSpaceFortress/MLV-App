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


def _write_cache(path, slugs):
    path.write_text(json.dumps({"models": [{"slug": slug} for slug in slugs]}), encoding="utf-8")


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
