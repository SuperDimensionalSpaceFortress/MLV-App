#!/usr/bin/env python3
"""LANE-RETIRE-LAUNCH-SITE-CENSUS-1 r7 (Grok CoS ask 6392967676, hub ruling HUB-TICK 20261025T0526Z): parameter-metadata meta-test for
the launch-site census scanner tests/coordination/Test-LaunchSites.ps1 (rule V3).

The scanner resolves `iex $cmd` to its literal only when nothing in the file can rebind `cmd`. A cmdlet parameter that takes a variable
NAME (Tee-Object -Variable, Import-LocalizedData -BindingVariable, the common -OutVariable, ...) rebinds it. r3, r4 and r5 each closed the
binders they were shown and missed the next one, so this test does not list binders: it walks the live Get-Command parameter metadata of
every cmdlet and function that ships with this pwsh (each module under $PSHOME, and Microsoft.PowerShell.Core), takes every parameter
named *Variable* (the common -*Variable ones included), and builds one
benign-marker fixture per way pwsh lets a caller reach it: the full name, `-Name:value`, each alias, the shortest prefix pwsh resolves to
it on that command, a splatted hashtable key, and its position when it has one. The scanner must leave `iex $cmd` unresolved for every
one, so the next unseen binder fails here instead of slipping through.

The payload is a marker that is not a constant ($env:CENSUS_FIXTURE_MARKER); the fixtures run nothing. The scanner's named-parameter rule
does not depend on the command, so a common parameter's fixtures are built once per distinct spelling (on one command that has it), and
every other *Variable* parameter is built on its own command.

Scope: the modules that ship with pwsh are the scope of the scanner's binder lists ($VarParamNames / $VarParamAliases, kept by hand so
the census reads the same on every platform), and they are the same on every runner. A parameter of another module is still a binder to
the scanner when its name holds `variable`; its alias or short prefix is the bound. CENSUS_BINDER_WALK=all walks every cmdlet and function
Get-Command lists instead, importing each module pwsh loads natively (about 40 s on the hub VM, 1328 commands, against 9 s for the
default scope: the hosted Windows unittest shards have about 12 s of budget left, test_ci_unittest_shard.ShardDurationBudgetTests).

Runs on the hosted Windows and ubuntu unittest shards. A missing pwsh skips locally and FAILS in hosted CI (GITHUB_ACTIONS=true), the
pattern of test_safety_lint_parity.py. Cost: one pwsh for the metadata walk and one pwsh scanner call for every fixture.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SCANNER = REPO_ROOT / "tests" / "coordination" / "Test-LaunchSites.ps1"
PWSH = shutil.which("pwsh")
MARKER = "$env:CENSUS_FIXTURE_MARKER"
COMMON = ("OutVariable", "ErrorVariable", "WarningVariable", "InformationVariable", "PipelineVariable")

# Every cmdlet and function, with each module Get-Command lists imported first (an unloaded command reports no parameters). For each
# command with a parameter named *Variable*: every parameter's name and aliases (the prefix rule needs them all), and the positions of
# each *Variable* one. The shortest prefix is worked out in Python (_shortest_prefix): a PowerShell loop over 6000+ parameters is slow.
WALK = r"""
$ErrorActionPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$everything = $env:CENSUS_BINDER_WALK -eq 'all'
$mods = if ($everything) { @(Get-Command -CommandType Cmdlet, Function | ForEach-Object Source | Where-Object { $_ } | Sort-Object -Unique) }
        else { @(Get-Module -ListAvailable | Where-Object { $_.ModuleBase.StartsWith($PSHOME, 'OrdinalIgnoreCase') } | ForEach-Object Name | Sort-Object -Unique) }
$unread = 0
foreach ($m in $mods) {
    # only modules pwsh loads itself: a Desktop-only module would come in through a Windows PowerShell compatibility session, whose
    # generated proxy functions re-declare the common parameters as plain ones and run the real command in another process
    $mi = Get-Module -ListAvailable -Name $m | Select-Object -First 1
    if ($null -eq $mi) { continue }
    $native = $IsCoreCLR -and (-not $IsWindows -or $mi.ModuleBase.StartsWith($PSHOME, 'OrdinalIgnoreCase') -or $mi.CompatiblePSEditions -contains 'Core')
    if (-not $native) { $unread++; continue }
    try { Import-Module $m -ErrorAction Stop 3>$null 4>$null } catch { $unread++ }
}
$loaded = @(Get-Module | ForEach-Object Name) + 'Microsoft.PowerShell.Core'
$cmds = @(Get-Command -CommandType Cmdlet, Function -Module $loaded)
$out = [System.Collections.Generic.List[object]]::new()
foreach ($c in $cmds) {
    $ps = $null
    try { $ps = $c.Parameters } catch { }
    if ($null -eq $ps) { $unread++; continue }
    $params = [System.Collections.Generic.List[object]]::new()
    $any = $false
    foreach ($p in $ps.Values) {
        $pos = $null
        if ($p.Name -like '*Variable*') {
            $any = $true
            $pos = [System.Collections.Generic.List[int]]::new()
            foreach ($s in $p.ParameterSets.Values) { if ($s.Position -ge 0 -and -not $pos.Contains($s.Position)) { $pos.Add($s.Position) } }
        }
        $params.Add([ordered]@{ n = $p.Name; a = [string[]]@($p.Aliases); v = $pos })
    }
    if ($any) { $out.Add([ordered]@{ c = $c.Name; p = $params }) }
}
[ordered]@{ commands = $cmds.Count; unread = $unread; table = $out } | ConvertTo-Json -Depth 6 -Compress
"""


def _shortest_prefix(name: str, params: list[dict]) -> str | None:
    """The shortest word pwsh binds to parameter `name` of a command: an exact name or alias wins; otherwise the word must be a prefix
    of the names and aliases of exactly one parameter."""
    words = [(q["n"], [q["n"], *(q["a"] or [])]) for q in params]
    for n in range(1, len(name) + 1):
        w = name[:n].lower()
        exact = [owner for owner, ws in words if any(x.lower() == w for x in ws)]
        if exact:
            if exact[0] == name:
                return name[:n]
            continue
        owners = {owner for owner, ws in words if any(x.lower().startswith(w) for x in ws)}
        if owners == {name}:
            return name[:n]
    return None


def _binders(walk: dict) -> list[dict]:
    """One row per *Variable* parameter: command, param, aliases, positions, shortest prefix."""
    out = []
    for row in walk["table"]:
        for q in row["p"]:
            if q["v"] is not None:
                out.append({"command": row["c"], "param": q["n"], "aliases": list(q["a"] or []), "positions": sorted(q["v"]),
                            "prefix": _shortest_prefix(q["n"], row["p"])})
    return out


def _fixture(binder: str) -> str:
    return "$cmd = 'Write-Output ok'\n" + binder + "\niex $cmd\n"


def _scan(texts: dict[str, str]) -> dict[str, list[dict]]:
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCANNER)], input=json.dumps(texts), capture_output=True,
                       text=True, encoding="utf-8", timeout=900)
    if r.returncode != 0:
        raise AssertionError(f"launch-site scanner failed (exit {r.returncode}): {r.stderr[-2000:]}")
    return json.loads(r.stdout)


def _iex_unresolved(sites: list[dict]) -> bool:
    return any(s["kind"] == "unresolved" and s["text"].startswith("iex $cmd") for s in sites)


def _forms(b: dict) -> dict[str, str]:
    """Every spelling of one *Variable* parameter on its command, as a benign-marker fixture: name -> binder statement."""
    cmd, name = b["command"], b["param"]
    words = [name, *b["aliases"]]
    if b["prefix"] and b["prefix"].lower() not in {w.lower() for w in words}:
        words.append(b["prefix"])
    forms = {}
    for w in words:
        forms[f"{cmd} -{w}"] = f"{MARKER} | {cmd} -{w} cmd"
        forms[f"{cmd} @{{ {w} }}"] = f"$h = @{{ {w} = 'cmd' }}\n{MARKER} | {cmd} @h"
    forms[f"{cmd} -{name}:"] = f"{MARKER} | {cmd} -{name}:cmd"
    for p in b["positions"]:
        forms[f"{cmd} position {p}"] = f"{cmd} {' '.join(['x'] * p)} cmd".replace("  ", " ")
    return forms


class CensusBinderParameterMetadataTests(unittest.TestCase):
    walk: dict = {}
    binders: list[dict] = []

    @classmethod
    def setUpClass(cls) -> None:
        if not PWSH:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise AssertionError("pwsh is required for the census binder meta-test -- hosted CI must not skip this test (the runner has pwsh)")
            raise unittest.SkipTest("pwsh is required for the census binder meta-test")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "walk-variable-parameters.ps1"
            script.write_text(WALK, encoding="utf-8")
            r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True,
                               encoding="utf-8", timeout=900)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            raise AssertionError(f"the parameter-metadata walk failed (exit {r.returncode}): {r.stderr[-2000:]}")
        cls.walk = json.loads(lines[-1])
        cls.binders = _binders(cls.walk)

    def _fixtures(self) -> dict[str, str]:
        out, seen_common = {}, set()
        binders = sorted(self.binders, key=lambda b: (b["command"] not in ("Write-Output", "Tee-Object"), b["command"], b["param"]))
        for b in binders:
            for label, binder in _forms(b).items():
                if b["param"] in COMMON:
                    spelling = label.split(" ", 1)[1]
                    if spelling in seen_common:
                        continue
                    seen_common.add(spelling)
                out[label] = _fixture(binder)
        return out

    def test_the_walk_reads_the_binders_it_must_see(self) -> None:
        # A walk that read nothing would pass the meta-test on nothing: it must see the common ones and Tee-Object -Variable.
        found = {(b["command"], b["param"]) for b in self.binders}
        self.assertGreater(self.walk["commands"], 100, f"the walk listed too few commands: {self.walk['commands']}")
        self.assertIn(("Tee-Object", "Variable"), found)
        for name in COMMON:
            self.assertIn(("Tee-Object", name), found, f"the walk did not read the common parameter -{name}")

    def test_every_variable_parameter_binder_leaves_the_executed_read_unresolved(self) -> None:
        fixtures = self._fixtures()
        got = _scan(fixtures)
        missed = [f"{label}: {fixtures[label].splitlines()[1]!r} -> {got[label]}" for label in fixtures if not _iex_unresolved(got[label])]
        params = {(b["command"], b["param"]) for b in self.binders}
        self.assertFalse(missed, f"{len(missed)} of {len(fixtures)} binder spellings ({len(params)} *Variable* parameters on "
                                 f"{self.walk['commands']} commands; {self.walk['unread']} modules or commands not read natively) left `iex $cmd` resolved to its literal; "
                                 "add the word to $VarParamNames / $VarParamAliases / $PositionalVarBinders in Test-LaunchSites.ps1:\n"
                                 + "\n".join(missed[:60]))

    def test_a_parameter_that_binds_no_variable_keeps_the_literal(self) -> None:
        # The same harness with binders that name no variable: a scanner that reported every read unresolved would pass the test above.
        controls = {
            "no binder": _fixture(f"{MARKER} | Out-Null"),
            "Write-Output -InputObject": _fixture(f"{MARKER} | Write-Output -InputObject cmd"),
            "Tee-Object -FilePath": _fixture(f"{MARKER} | Tee-Object -FilePath out.txt"),
            "splat of a plain key": _fixture(f"$h = @{{ InputObject = {MARKER} }}\nWrite-Output @h"),
        }
        got = _scan(controls)
        wrong = [label for label in controls if got[label]]
        self.assertFalse(wrong, f"a parameter that binds no variable made the safe literal unresolved: {wrong} -> {[got[w] for w in wrong]}")


if __name__ == "__main__":
    unittest.main()
