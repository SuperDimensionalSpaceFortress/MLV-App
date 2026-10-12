"""Offline test of the OWNER-RUN prune script's selection logic (GPU-PREF-REGISTRY-STOP-AND-PRUNE-1).

``tools/profiling/venue/Prune-MlvGpuPreferences.ps1`` is never executed here (NA-8: no agent or lane runs
it) and no test touches the registry. What is driven is ``MlvGpuPreferencePrune.psm1``, whose file-system
questions are injected script blocks, against a FAKE value list. The script itself is only parsed and
read, to pin that it is a dry run unless ``-Apply`` is passed and that it can only ever remove.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENUE_DIR = REPO_ROOT / "tools" / "profiling" / "venue"
MODULE = VENUE_DIR / "MlvGpuPreferencePrune.psm1"
SCRIPT = VENUE_DIR / "Prune-MlvGpuPreferences.ps1"
VENUES_JSON = REPO_ROOT / "tools" / "profiling" / "dual-venue" / "venues.json"
PWSH = shutil.which("pwsh")

ROOTS = [
    r"C:\mlvtmp\mlv-agent", r"G:\Temp\mlv-gpu-profile\agent", r"G:\Temp\mlv-gpu-profile",
    r"C:\mlvtmp", r"C:\dell-bench", r"C:\temp",
]
ALL_MOUNTED = (r"C:\mlvtmp", r"C:\mlvtmp\mlv-agent", r"C:\dell-bench", r"C:\temp")


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class SelectionLogic(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gpuprefprune-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def judge(self, names, *, existing=(), mounted=(r"C:\mlvtmp", r"C:\mlvtmp\mlv-agent"), roots=ROOTS,
              include_existing=False):
        payload = {"names": list(names), "existing": list(existing), "mounted": list(mounted), "roots": list(roots)}
        data = self.tmp / "in.json"
        data.write_text(json.dumps(payload), encoding="utf-8")
        script = (
            f"Import-Module {_quote(MODULE)} -Force; "
            f"$in = Get-Content -LiteralPath {_quote(data)} -Raw | ConvertFrom-Json; "
            "$existing = @($in.existing); $mounted = @($in.mounted); "
            "$r = @(Select-MlvStaleGpuPreferenceValue -Names @($in.names) -Roots @($in.roots) "
            "-FileExists { param($p) $existing -contains $p } -RootExists { param($r) $mounted -contains $r }"
            + (" -IncludeExisting" if include_existing else "") + "); "
            "ConvertTo-Json -InputObject @($r | Select-Object Name, Verdict, Root) -Depth 4"
        )
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = json.loads(proc.stdout)
        return {row["Name"]: row["Verdict"] for row in rows}

    def test_stale_values_under_the_roots_are_pruned_and_everything_else_is_kept(self) -> None:
        stale_lane = r"C:\mlvtmp\lane-FOO-20261011\platform\qt\build-release\release\MLVApp.exe"
        live_lane = r"C:\mlvtmp\lane-LIVE-20261011\MLVApp.exe"
        stale_agent = r"C:\mlvtmp\mlv-agent\pkg\abc123\MLVApp.exe"
        installed = r"C:\Program Files\Adobe\Premiere\Adobe Premiere Pro.exe"
        verdicts = self.judge(
            [stale_lane, live_lane, stale_agent, installed],
            existing=[live_lane, installed],
        )
        self.assertEqual(
            verdicts,
            {
                stale_lane: "prune",
                live_lane: "keep-file-exists",
                stale_agent: "prune",
                installed: "keep-outside-roots",
            },
        )

    def test_matching_is_case_insensitive_and_accepts_forward_slashes(self) -> None:
        verdicts = self.judge([r"c:/MLVTMP/Lane-X/MLVApp.exe"])
        self.assertEqual(list(verdicts.values()), ["prune"])

    def test_a_file_that_exists_is_kept_whatever_its_spelling(self) -> None:
        verdicts = self.judge([r"c:/mlvtmp/lane-x/MLVApp.exe"], existing=[r"C:\mlvtmp\lane-x\MLVApp.exe"])
        self.assertEqual(list(verdicts.values()), ["keep-file-exists"])

    def test_a_sibling_directory_sharing_the_root_prefix_is_not_under_the_root(self) -> None:
        sibling = r"C:\mlvtmp-owner-tools\Thing.exe"
        verdicts = self.judge([sibling])
        self.assertEqual(verdicts, {sibling: "keep-outside-roots"})

    def test_the_root_itself_and_its_direct_trailing_separator_are_not_values_under_it(self) -> None:
        verdicts = self.judge([r"C:\mlvtmp", "C:\\mlvtmp\\"])
        self.assertEqual(set(verdicts.values()), {"keep-outside-roots"})

    def test_traversal_relative_and_empty_names_are_never_judged(self) -> None:
        names = [r"C:\mlvtmp\..\Windows\System32\evil.exe", r"C:\mlvtmp\.\x.exe", r"relative\MLVApp.exe", r"MLVApp.exe"]
        verdicts = self.judge(names)
        self.assertEqual(set(verdicts.values()), {"keep-unjudged-path"})
        self.assertEqual(set(verdicts), set(names))

    def test_a_root_that_is_not_mounted_keeps_its_values(self) -> None:
        on_g = r"G:\Temp\mlv-gpu-profile\agent\pkg\x\MLVApp.exe"
        verdicts = self.judge([on_g], mounted=[r"C:\mlvtmp"])
        self.assertEqual(verdicts, {on_g: "keep-root-unavailable"})
        verdicts = self.judge([on_g], mounted=[r"G:\Temp\mlv-gpu-profile\agent"])
        self.assertEqual(verdicts, {on_g: "prune"})

    def test_an_empty_value_list_selects_nothing(self) -> None:
        self.assertEqual(self.judge([]), {})

    def test_absent_exes_under_each_owner_named_root_are_pruned(self) -> None:
        under_mlvtmp = r"C:\mlvtmp\lane-A\MLVApp.exe"
        under_dell = r"C:\dell-bench\build\MLVApp.exe"
        under_temp = r"C:\temp\build\MLVApp-1.2.3.exe"
        verdicts = self.judge([under_mlvtmp, under_dell, under_temp], mounted=ALL_MOUNTED)
        self.assertEqual(verdicts, {under_mlvtmp: "prune", under_dell: "prune", under_temp: "prune"})

    def test_a_dell_bench_or_temp_root_that_is_not_mounted_keeps_its_values(self) -> None:
        under_dell = r"C:\dell-bench\build\MLVApp.exe"
        under_temp = r"C:\temp\build\MLVApp.exe"
        verdicts = self.judge([under_dell, under_temp], mounted=[r"C:\mlvtmp"])
        self.assertEqual(verdicts, {under_dell: "keep-root-unavailable", under_temp: "keep-root-unavailable"})

    def test_an_existing_mlvapp_build_is_kept_without_include_existing_and_pruned_with_it(self) -> None:
        build = r"C:\dell-bench\builds\MLVApp-20261011.exe"
        other = r"C:\temp\tools\Helper.exe"
        names = [build, other]
        kept = self.judge(names, existing=names, mounted=ALL_MOUNTED)
        self.assertEqual(kept, {build: "keep-file-exists", other: "keep-file-exists"})
        pruned = self.judge(names, existing=names, mounted=ALL_MOUNTED, include_existing=True)
        # only the MLVApp-*.exe shape is relaxed; an existing exe of any other name is still kept
        self.assertEqual(pruned, {build: "prune", other: "keep-file-exists"})

    def test_include_existing_never_reaches_an_exe_outside_the_roots(self) -> None:
        outside = r"C:\Program Files\MLV\MLVApp-9.exe"
        sibling = r"C:\temporary\MLVApp-9.exe"
        names = [outside, sibling]
        verdicts = self.judge(names, existing=names, mounted=ALL_MOUNTED, include_existing=True)
        self.assertEqual(verdicts, {outside: "keep-outside-roots", sibling: "keep-outside-roots"})

    def test_include_existing_does_not_reach_a_root_that_is_not_mounted(self) -> None:
        build = r"C:\dell-bench\MLVApp-1.exe"
        verdicts = self.judge([build], existing=[build], mounted=[r"C:\mlvtmp"], include_existing=True)
        self.assertEqual(verdicts, {build: "keep-root-unavailable"})

    def test_roots_come_from_venues_json_plus_the_always_included_build_roots(self) -> None:
        script = (
            f"Import-Module {_quote(MODULE)} -Force; "
            f"ConvertTo-Json -InputObject @(Get-MlvGpuPreferenceRoots -VenuesJsonPath {_quote(VENUES_JSON)}) -Depth 3"
        )
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        roots = {r.lower() for r in json.loads(proc.stdout)}
        venues = json.loads(VENUES_JSON.read_text(encoding="utf-8"))["venues"]
        expected = {r"c:\mlvtmp", r"c:\dell-bench", r"c:\temp"}
        for venue in venues.values():
            expected.add(venue["agentRoot"].rstrip("\\").lower())
            expected.add(venue["scratchRoot"].rstrip("\\").lower())
        self.assertEqual(roots, expected)
        self.assertEqual(len(roots), len(json.loads(proc.stdout)), "roots must be distinct")


class ScriptContract(unittest.TestCase):
    """Reads the script as text; nothing here runs it."""

    def setUp(self) -> None:
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_files_are_ascii_only(self) -> None:
        for path in (SCRIPT, MODULE):
            path.read_bytes().decode("ascii")

    def test_apply_is_an_explicit_switch_and_the_default_changes_nothing(self) -> None:
        self.assertRegex(self.text, r"\[switch\]\$Apply")
        # the one removal call sits inside the -Apply branch
        branch = re.search(r"if \(\$Apply -and \$candidates\.Count -gt 0\) \{(.*?)\n\}\n", self.text, re.DOTALL)
        self.assertIsNotNone(branch, "the removal block must be guarded by -Apply")
        self.assertEqual(self.text.count("DeleteValue("), 1)
        self.assertIn("DeleteValue(", branch.group(1))

    def test_include_existing_is_an_explicit_switch_forwarded_to_the_selection(self) -> None:
        self.assertRegex(self.text, r"\[switch\]\$IncludeExisting")
        self.assertIn("-IncludeExisting:$IncludeExisting", self.text)
        header = self.text.split("[CmdletBinding()]")[0]
        for root in (r"C:\mlvtmp", r"C:\dell-bench", r"C:\temp"):
            self.assertIn(root, header, f"the header must name the {root} root")

    def test_it_removes_only_the_values_it_judged_prunable(self) -> None:
        self.assertIn("foreach ($c in $candidates)", self.text)
        self.assertIn("$_.Verdict -eq 'prune'", self.text)
        self.assertIn("$key.DeleteValue($c.Name, $true)", self.text)

    def test_it_prints_counts_before_and_after_and_writes_a_json_receipt(self) -> None:
        for needle in ("values before", "values after", "countBefore", "countAfter", "ConvertTo-Json", "WriteAllText"):
            self.assertIn(needle, self.text)

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_the_script_parses_without_errors(self) -> None:
        probe = (
            "$e = $null; $t = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile({_quote(SCRIPT)}, [ref]$t, [ref]$e) | Out-Null; "
            "if ($e -and $e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", probe], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ReceiptBuild(unittest.TestCase):
    """Executes the script's own receipt literal (sliced from the file, never the whole script) with a NON-EMPTY
    removed/failed list. The receipt is built after the -Apply deletions, so a throw here means values are gone
    and no receipt exists. Bachelor review 2026-10-11: @(List[object]) threw "Argument types do not match"."""

    def receipt_block(self) -> str:
        text = SCRIPT.read_text(encoding="utf-8")
        lists = re.search(r"^\$removed = New-Object.*?\n\$failed = New-Object[^\n]*\n", text, re.MULTILINE | re.DOTALL)
        block = re.search(r"^\$receipt = \[ordered\]@\{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(lists, "the removed/failed list declarations moved; update this test")
        self.assertIsNotNone(block, "the receipt literal moved; update this test")
        return lists.group(0) + block.group(0)

    def build(self, shell: str) -> dict:
        stubs = (
            "$subKeyPath = 'Software\\Example'; "
            f"$VenuesJson = {_quote(VENUES_JSON)}; "
            "$roots = @('C:\\mlvtmp'); $namesBefore = @('a', 'b', 'c'); $namesAfter = @('c'); "
            "$candidates = @([pscustomobject]@{ Name = 'a' }, [pscustomobject]@{ Name = 'b' }); "
            "$Apply = $true; $IncludeExisting = $true; "
        )
        fill = (
            "$removed.Add([pscustomobject]@{ name = 'a'; data = 'GpuPreference=2;' }); "
            "$removed.Add([pscustomobject]@{ name = 'b'; data = 'GpuPreference=2;' }); "
            "$failed.Add([pscustomobject]@{ name = 'x'; error = 'denied' }); "
        )
        lists, receipt = self.receipt_block().split("$receipt = ", 1)
        script = stubs + lists + fill + "$receipt = " + receipt + "$receipt | ConvertTo-Json -Depth 6"
        proc = subprocess.run([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def check(self, shell: str) -> None:
        receipt = self.build(shell)
        self.assertEqual([r["name"] for r in receipt["removed"]], ["a", "b"])
        self.assertEqual(receipt["removed"][0]["data"], "GpuPreference=2;")
        self.assertEqual([r["name"] for r in receipt["failed"]], ["x"])
        self.assertEqual(receipt["toRemove"], ["a", "b"])

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_the_receipt_builds_with_removed_values_under_pwsh(self) -> None:
        self.check(PWSH)

    @unittest.skipIf(shutil.which("powershell") is None, "Windows PowerShell 5.1 is not on PATH")
    def test_the_receipt_builds_with_removed_values_under_windows_powershell(self) -> None:
        self.check(shutil.which("powershell"))


if __name__ == "__main__":
    unittest.main()
