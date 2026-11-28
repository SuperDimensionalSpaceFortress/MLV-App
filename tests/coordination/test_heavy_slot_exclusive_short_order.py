"""VENUE-STAGE-HEAVY-SLOT-RESERVE-FOR-VENUE-CHAINS-1: what the installed heavy-slot does with an exclusive-short ticket.

Read-only proof of the mechanism the build guard now asks for. C:\\ProgramData\\HeavySlot\\bin\\heavy-slot.ps1 is dot-sourced in a pwsh child
(its line "if ($MyInvocation.InvocationName -eq '.') { return }" means dot-sourcing runs no command) with HEAVY_SLOT_DIR pointed at a scratch
directory, and Get-QueueOrder / Get-LabelKey are called on synthetic queue entries. Nothing is written under C:\\ProgramData\\HeavySlot, no
slot mutex is opened, and no ticket is taken.

Before (class shared): a venue stage assemble enqueued after two young shared pytest tickets orders LAST. Now (exclusive-short, promotable): it
orders FIRST. Not promotable (eta 16 min, or a demoted key): it orders as shared. A shared ticket that has waited 60 min or more stays ahead.

"promotable" is a stored field on each queue entry; heavy-slot.ps1 computes it at enqueue as
`class -eq 'exclusive-short' -and key not demoted -and (eta -le $ExclusiveShortLimit)`. The tests set it by that rule, and pin the limit
itself (15 min) and the starvation bound (60 min) from the tool's own source, so a change to either makes this file red and sends the
reviewer to build-guard.ps1's 15 min cap.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HEAVY_SLOT_PS1 = Path(r"C:\ProgramData\HeavySlot\bin\heavy-slot.ps1")
PWSH = shutil.which("pwsh.exe") or shutil.which("pwsh")

if os.name != "nt":
    pytest.skip("heavy-slot is a Windows tool (C:\\ProgramData\\HeavySlot)", allow_module_level=True)
if not PWSH:
    pytest.skip("pwsh is not on PATH, so the installed heavy-slot cannot be dot-sourced", allow_module_level=True)
if not HEAVY_SLOT_PS1.is_file():
    pytest.skip(f"the machine heavy-slot is not installed at {HEAVY_SLOT_PS1}", allow_module_level=True)

ORDER_SCRIPT = r"""
param([string]$Tool, [string]$Entries)
$ErrorActionPreference = 'Stop'
. $Tool
$now = [DateTime]::UtcNow
$list = foreach ($e in @($Entries | ConvertFrom-Json)) {
    [pscustomobject]@{ ticket = $e.ticket; class = $e.class; promotable = [bool]$e.promotable
                       enqueuedAt = $now.AddMinutes(-[double]$e.ageMin).ToString('o') }
}
$out = [ordered]@{
    order = @(Get-QueueOrder @($list) $now | ForEach-Object { [string]$_.ticket })
    keys  = [ordered]@{ stage = (Get-LabelKey 'mlv:venue-stage:CARD-X-1'); build = (Get-LabelKey 'mlv:CARD-X-1:build'); hub = (Get-LabelKey 'mlv:hub:build') }
}
$out | ConvertTo-Json -Compress
"""


def _order(tmp_path: Path, entries: list) -> dict:
    script = tmp_path / "order.ps1"
    script.write_text(ORDER_SCRIPT, encoding="ascii")
    state = tmp_path / "state"
    state.mkdir()
    env = dict(os.environ)
    env["HEAVY_SLOT_DIR"] = str(state)  # scratch: dot-sourcing must never reach C:\ProgramData\HeavySlot state
    for name in ("HEAVY_SLOT_NAME", "HEAVY_SLOT_COUNT", "HEAVY_SLOT_HELD", "HEAVY_SLOT_HELD_START", "HEAVY_SLOT_HELD_NAME"):
        env.pop(name, None)
    done = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-Tool", str(HEAVY_SLOT_PS1), "-Entries", json.dumps(entries)],
        capture_output=True, text=True, timeout=120, env=env, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _e(ticket: str, age_min: float, klass: str = "shared", promotable: bool = False) -> dict:
    return {"ticket": ticket, "ageMin": age_min, "class": klass, "promotable": promotable}


def test_a_shared_stage_assemble_enqueued_behind_two_young_pytest_batches_orders_last(tmp_path) -> None:
    # TODAY: build-guard sent --class shared, so the stage waited for every older shared ticket (23-83 min on 2026-11-25..28).
    got = _order(tmp_path, [_e("pytest-a", 25), _e("pytest-b", 12), _e("stage", 1)])
    assert got["order"] == ["pytest-a", "pytest-b", "stage"], got


def test_a_promotable_exclusive_short_stage_assemble_orders_first(tmp_path) -> None:
    got = _order(tmp_path, [_e("pytest-a", 25), _e("pytest-b", 12), _e("stage", 1, "exclusive-short", True)])
    assert got["order"] == ["stage", "pytest-a", "pytest-b"], got


def test_an_exclusive_short_ticket_that_is_not_promotable_orders_as_shared(tmp_path) -> None:
    # --eta 16m (over the 15 min limit) or a demoted key stores promotable=false: no promotion, so build-guard refuses eta > 15 up front.
    got = _order(tmp_path, [_e("pytest-a", 25), _e("pytest-b", 12), _e("stage", 1, "exclusive-short", False)])
    assert got["order"] == ["pytest-a", "pytest-b", "stage"], got


def test_a_shared_ticket_that_has_waited_an_hour_stays_ahead_of_the_promoted_stage(tmp_path) -> None:
    # The starvation bound (M3): a promoted ticket jumps only shared tickets that have waited under 60 min.
    got = _order(tmp_path, [_e("pytest-old", 61), _e("pytest-young", 30), _e("stage", 1, "exclusive-short", True)])
    assert got["order"] == ["pytest-old", "stage", "pytest-young"], got


def test_the_demotion_key_of_a_stage_assemble_is_its_own_and_never_a_builds_or_the_hubs(tmp_path) -> None:
    # heavy-slot demotes the first two ':' segments of the label (README.md:103-109): mlv:venue-stage:<lane> -> mlv:venue-stage.
    got = _order(tmp_path, [_e("stage", 1, "exclusive-short", True)])
    assert got["keys"] == {"stage": "mlv:venue-stage", "build": "mlv:CARD-X-1", "hub": "mlv:hub"}, got


def test_the_tools_own_limits_are_the_ones_the_guard_caps_on() -> None:
    # build-guard.ps1 refuses exclusive-short with an eta above 15 min and the assembler uses 12 min. If the owner moves either bound in
    # heavy-slot, this goes red so the guard's cap is re-derived instead of silently out of step.
    text = HEAVY_SLOT_PS1.read_text(encoding="utf-8", errors="replace")
    limit = re.search(r"\$ExclusiveShortLimit\s*=\s*\[TimeSpan\]::FromMinutes\((\d+)\)", text)
    bound = re.search(r"\$StarvationBound\s*=\s*\[TimeSpan\]::FromMinutes\((\d+)\)", text)
    assert limit and int(limit.group(1)) == 15, "heavy-slot's exclusive-short promotion limit is no longer 15 min"
    assert bound and int(bound.group(1)) == 60, "heavy-slot's starvation bound is no longer 60 min"
    assert "$o.class -eq 'exclusive-short'" in text and "$ExclusiveShortLimit" in text.split("$promotable =", 1)[1].split("\n", 1)[0]
