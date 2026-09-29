"""Parity tests: ONE parser for the display identity, shared by the attribution job and the
smoke runner (UM-DISPLAY-SELECT-AND-LOG-1 round 3, binding design-review item opus-blocker-1).

WHY. Two consumers read the app's own gui_smoke.display_screen / display_target /
window_placement lines: the attribution job (through Build-AttrCudaDisplayBlock's
`presentationIdentity`, fed by AttrCudaArtifacts.psm1's Get-AttrCudaGuiSmokeDisplaySelection) and
the smoke runner (its `display` block on result.json, which compare-release-gui-smoke-ab.ps1's
comparator consumes). Each used to carry its own regexes and they DISAGREED -- a reordered
display_screen line parsed in the runner but not in the job; two display_screen lines for one
screen name resolved to the FIRST in the job and the LAST in the runner; both window_placement
regexes were field-order strict (sol flagged the latter). Two parsers that can disagree publish two
identities for one leg.

These tests feed the SAME log text to both real code paths (executed, never reimplemented) and
assert the published identity is identical -- and, for the well-formed cases, that it is the
KNOWN expected identity, so two parsers that both fail to parse can never pass as "agreeing".
The shared parser lives in tools/profiling/gui-smoke-display-identity.ps1.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
JOB_SCRIPT = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
RUNNER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"
SHARED = ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

SHARED_FUNCTIONS = ("ConvertFrom-GuiSmokeLogFields", "ConvertFrom-GuiSmokeDisplayLog", "Get-GuiSmokeDisplayIdentity")

D1 = '\\\\.\\DISPLAY1'

SCREEN = (
    f'gui_smoke.display_screen index=0 name="{D1}" manufacturer="ASUS" model="PA329C" serial="S-9" '
    'geometry=0,0 3840x2160 physical=3840x2160 dpr=1.00 refresh_hz=59.997 primary=1'
)
PLACEMENT = (
    f'gui_smoke.window_placement mode=fullscreen screen="{D1}" verified=1 window=0,0 3840x2160 '
    f'preview=3840x2160 target_screen="{D1}" presentation_screen="{D1}" presentation_physical=3840x2160'
)
TARGET = f'gui_smoke.display_target screen="{D1}" reason=max_physical_pixels candidates=1 fallback=0'

KNOWN_ASUS = {
    "presentationScreenName": D1,
    "presentationManufacturer": "ASUS",
    "presentationModel": "PA329C",
    "presentationSerial": "S-9",
    "physicalWidth": 3840,
    "physicalHeight": 2160,
    "refreshHzRounded": 60,
    "dpr": 1.0,
    "windowMode": "fullscreen",
    "previewWidth": 3840,
    "previewHeight": 2160,
    "verified": True,
    "identityUnknownReason": None,
}


def _extract_function(text: str, name: str) -> str:
    start = text.index(f"function {name} {{")
    depth = 0
    i = text.index("{", start)
    j = i
    while True:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return text[start:j + 1]


def _runner_span() -> str:
    text = RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(
        "# UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 1): this leg's own display"
    )
    end = text.index("\n$result = [pscustomobject]@{", start)
    return text[start:end]


def _dot_source_shared() -> str:
    return f". '{SHARED}'\n"


class _ParityCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gui-smoke-display-identity-parity-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _run(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" + body, encoding="utf-8")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True,
        )

    def _write_log(self, lines: list[str]) -> Path:
        path = self.tmp / "smoke-run.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def job_identity(self, lines: list[str]) -> dict:
        """The job's path: psm1 Get-AttrCudaGuiSmokeDisplaySelection -> the job template's own
        Build-AttrCudaDisplayBlock -> its published `presentationIdentity`."""
        log = self._write_log(lines)
        job_text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        build = _extract_function(job_text, "Build-AttrCudaDisplayBlock")
        proc = self._run(
            f"Import-Module '{MODULE}' -Force\n"
            + _dot_source_shared()
            + build + "\n"
            f"$log = [IO.File]::ReadAllText('{log}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "$inv = [pscustomobject]@{ collected = $false; devices = @(); error = 'stub' }\n"
            "$b = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'bachelor' -AppSelection $sel\n"
            "Write-Output ('JSON=' + ($b.presentationIdentity | ConvertTo-Json -Compress))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return self._json(proc)

    def runner_identity(self, lines: list[str]) -> dict:
        """The runner's path: the runner's own display-block span, extracted verbatim."""
        literal = "@(" + ",".join("'" + ln.replace("'", "''") + "'" for ln in lines) + ")"
        proc = self._run(
            _dot_source_shared()
            + f"$recentLines = {literal}\n"
            + _runner_span()
            + "\nWrite-Output ('JSON=' + ($displayBlock | ConvertTo-Json -Compress))\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return self._json(proc)

    @staticmethod
    def _json(proc: subprocess.CompletedProcess) -> dict:
        for line in proc.stdout.splitlines():
            if line.startswith("JSON="):
                return json.loads(line[len("JSON="):])
        raise AssertionError("no JSON= line in probe output:\n" + proc.stdout + proc.stderr)

    def assert_parity(self, lines: list[str], expected: dict | None = None) -> dict:
        job = self.job_identity(lines)
        runner = self.runner_identity(lines)
        self.assertEqual(job, runner, "job and runner published different identities for the same log")
        if expected is not None:
            self.assertEqual(job, expected)
        return job


@requires_pwsh
class JobAndRunnerPublishTheSameIdentityTests(_ParityCase):
    def test_canonical_block_is_the_same_known_identity(self) -> None:
        self.assert_parity([SCREEN, TARGET, PLACEMENT], KNOWN_ASUS)

    def test_a_reordered_window_placement_line_still_parses_in_both(self) -> None:
        # sol's field-order case: both window_placement regexes were positional. MUTATION CAUGHT:
        # returning to a positional window_placement regex in either consumer.
        placement = (
            f'gui_smoke.window_placement preview=3840x2160 window=0,0 3840x2160 verified=1 '
            f'presentation_physical=3840x2160 presentation_screen="{D1}" target_screen="{D1}" '
            f'screen="{D1}" mode=fullscreen'
        )
        self.assert_parity([SCREEN, TARGET, placement], KNOWN_ASUS)

    def test_a_reordered_display_screen_line_still_parses_in_both(self) -> None:
        # The job's display_screen pattern was positional and required every field; the runner
        # read keys by name. MUTATION CAUGHT: a positional display_screen regex in the job path.
        screen = (
            f'gui_smoke.display_screen primary=1 refresh_hz=59.997 dpr=1.00 physical=3840x2160 '
            f'geometry=0,0 3840x2160 serial="S-9" model="PA329C" manufacturer="ASUS" name="{D1}" index=0'
        )
        self.assert_parity([screen, TARGET, PLACEMENT], KNOWN_ASUS)

    def test_two_display_screen_lines_for_one_name_resolve_the_same_way_last_wins(self) -> None:
        # The job took the FIRST record of a screen name, the runner the LAST. MUTATION CAUGHT:
        # First-vs-Last divergence between the two consumers.
        first = SCREEN.replace("refresh_hz=59.997", "refresh_hz=30.000")
        identity = self.assert_parity([first, SCREEN, TARGET, PLACEMENT], KNOWN_ASUS)
        self.assertEqual(identity["refreshHzRounded"], 60)

    def test_a_display_screen_line_that_predates_manufacturer_model_serial_is_unknown_in_both(self) -> None:
        legacy = (
            f'gui_smoke.display_screen index=0 name="{D1}" geometry=0,0 3840x2160 physical=3840x2160 '
            'dpr=1.00 refresh_hz=59.997 primary=1'
        )
        identity = self.assert_parity([legacy, TARGET, PLACEMENT])
        self.assertIsNotNone(identity["identityUnknownReason"])
        self.assertFalse(identity["verified"])
        self.assertIsNone(identity["presentationManufacturer"])

    def test_a_name_that_differs_only_in_case_matches_in_both(self) -> None:
        placement = PLACEMENT.replace(f'presentation_screen="{D1}"', 'presentation_screen="\\\\.\\display1"')
        identity = self.assert_parity([SCREEN, TARGET, placement])
        self.assertIsNone(identity["identityUnknownReason"])

    def test_a_log_prefix_with_its_own_key_value_tokens_does_not_change_the_identity(self) -> None:
        prefix = "[2026-09-29 10:00:00.123] pid=42 mode=stale screen=bogus "
        self.assert_parity([prefix + SCREEN, prefix + TARGET, prefix + PLACEMENT], KNOWN_ASUS)

    def test_a_value_cannot_inject_a_key(self) -> None:
        # A quoted value containing key-shaped text is consumed whole by the tokenizer.
        screen = SCREEN.replace('model="PA329C"', 'model="PA329C serial=EVIL"')
        identity = self.assert_parity([screen, TARGET, PLACEMENT])
        self.assertEqual(identity["presentationSerial"], "S-9")
        self.assertEqual(identity["presentationModel"], "PA329C serial=EVIL")

    def test_an_unreadable_presentation_physical_is_unknown_in_both(self) -> None:
        placement = PLACEMENT.replace("presentation_physical=3840x2160", "presentation_physical=garbage")
        identity = self.assert_parity([SCREEN, TARGET, placement])
        self.assertIsNotNone(identity["identityUnknownReason"])

    def test_a_legacy_placement_line_is_unknown_in_both(self) -> None:
        placement = (f'gui_smoke.window_placement mode=fullscreen screen="{D1}" verified=1 '
                     'window=0,0 3840x2160 preview=3840x2160')
        identity = self.assert_parity([SCREEN, TARGET, placement])
        self.assertIn("legacy binary", identity["identityUnknownReason"])

    def test_no_placement_line_is_unknown_in_both(self) -> None:
        identity = self.assert_parity([SCREEN, TARGET])
        self.assertIn("no gui_smoke.window_placement line found", identity["identityUnknownReason"])

    def test_no_display_screen_record_for_the_presentation_screen_is_unknown_in_both(self) -> None:
        identity = self.assert_parity([TARGET, PLACEMENT])
        self.assertIn("no matching gui_smoke.display_screen line", identity["identityUnknownReason"])


@requires_pwsh
class OnlyOneParserExistsTests(_ParityCase):
    """Structural pins: the consumers must DELEGATE, not carry a regex of their own."""

    def test_the_runner_span_uses_the_shared_parser_and_has_no_display_regex_of_its_own(self) -> None:
        # MUTATION CAUGHT: re-inlining a private window_placement/display_screen regex.
        span = _runner_span()
        self.assertIn("ConvertFrom-GuiSmokeDisplayLog", span)
        self.assertIn("Get-GuiSmokeDisplayIdentity", span)
        self.assertNotIn("[regex]::Match", span)
        self.assertNotIn("mode=(?<mode>", span)

    def test_the_psm1_display_selection_function_is_a_thin_delegate(self) -> None:
        text = MODULE.read_text(encoding="utf-8").replace("\r\n", "\n")
        body = _extract_function(text, "Get-AttrCudaGuiSmokeDisplaySelection")
        self.assertIn("ConvertFrom-GuiSmokeDisplayLog", body)
        self.assertNotIn("[regex]::Match", body)
        self.assertNotIn("-match", body.replace("-notmatch", ""))

    def test_the_runner_dot_sources_the_shared_file_as_a_closure_sibling(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn(". (Join-Path $PSScriptRoot 'gui-smoke-display-identity.ps1')", text)
        manifest = MODULE.read_text(encoding="utf-8")
        self.assertIn("'tools/profiling/gui-smoke-display-identity.ps1'", manifest)

    def test_the_job_generator_embeds_the_shared_functions_from_the_committed_sibling(self) -> None:
        text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        placeholder_at = text.index("__EMBEDDED_FUNCTIONS__")
        before = text[:placeholder_at]
        self.assertIn("gui-smoke-display-identity.ps1", before)
        for name in ("ConvertFrom-GuiSmokeLogFields", "ConvertFrom-GuiSmokeDisplayLog", "Get-GuiSmokeDisplayIdentity"):
            with self.subTest(function=name):
                self.assertIn(f"'{name}'", before)

    def test_every_shared_function_round_trips_through_the_embedding_extractor(self) -> None:
        # The extractor's contract (function at column 0, closing brace at column 0, no other
        # unindented brace inside) must hold for this file, or the job embeds a truncated body.
        proc = self._run(
            f"Import-Module '{MODULE}' -Force\n"
            "foreach ($n in @('" + "','".join(SHARED_FUNCTIONS) + "')) {\n"
            f"    $src = Get-AttrCudaEmbeddedFunctionSource -ModulePath '{SHARED}' -Name $n\n"
            "    $tokens = $null; $errs = $null\n"
            "    $ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errs)\n"
            "    $defs = @($ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false))\n"
            "    Write-Output \"FN=$n ERR=$($errs.Count) DEFS=$($defs.Count) NAME=$($defs[0].Name)\"\n"
            "}\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in SHARED_FUNCTIONS:
            self.assertIn(f"FN={name} ERR=0 DEFS=1 NAME={name}", proc.stdout)


if __name__ == "__main__":
    unittest.main()
