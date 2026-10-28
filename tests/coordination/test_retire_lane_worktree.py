"""Falsifier tests for tools/coordination/Retire-LaneWorktree.ps1 (SAFE-gate worktree retirement).

Each case builds a throwaway repository with a bare remote, so nothing depends on this
checkout's refs. Every case asserts both the action AND the reason prefix, so a gate that
passes for the wrong reason fails the test.
"""
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "tools" / "coordination" / "Retire-LaneWorktree.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(not (PWSH and GIT), reason="requires pwsh and git")


# r3: with the session-0 exemption gone, ANY process the host starts after a test worktree exists and whose cwd we cannot read
# (a svchost or SearchProtocolHost spawned mid-run) refuses a removal, by design. These cases test git/quarantine/sweep logic and
# real HOLDERS the test itself spawns, not the host's service churn, so their real snapshot is limited to (a) processes that
# started before this pytest session (a stable population, always older than any test worktree) and (b) children of pytest.
SESSION_START_UTC = datetime.datetime.now(datetime.timezone.utc).isoformat()
PYTEST_PID = os.getpid()
HOST_SNAPSHOT_FILTER = (
    "$origSnapshot = ${function:Get-LaneProcessSnapshot}; "
    "function Get-LaneProcessSnapshot { $s = & $origSnapshot; "
    f"$t0 = [datetime]::Parse('{SESSION_START_UTC}').ToUniversalTime(); "
    "$keep = @($s.Procs | Where-Object { $_.ParentProcessId -eq " + str(PYTEST_PID) + " -or -not $_.CreationDate -or "
    "([datetime]$_.CreationDate).ToUniversalTime() -lt $t0 }); "
    "[pscustomobject]@{ Procs = $keep; SelfPids = $s.SelfPids; CapturedUtc = $s.CapturedUtc } }; "
)


def _git(cwd, *args):
    subprocess.run([GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True)


# COST (CI-SHARD8-GUARDRAILS-COST-1): the repository below took eight git processes to build and was rebuilt for
# each of this file's ~125 cases. It is now built once per pytest session by `_build_repo` (body unchanged) and
# each case gets a private copy of that directory tree; the one absolute path a copy carries, main's `origin` url,
# is repointed at the copy's own bare remote.
_REPO_TEMPLATE = {}


@pytest.fixture()
def repo(tmp_path, tmp_path_factory):
    if "dir" not in _REPO_TEMPLATE:
        template = tmp_path_factory.mktemp("retire-repo-template")
        _build_repo(template)
        _REPO_TEMPLATE["dir"] = template
    remote = tmp_path / "remote.git"
    main = tmp_path / "main"
    shutil.copytree(_REPO_TEMPLATE["dir"] / "remote.git", remote)
    shutil.copytree(_REPO_TEMPLATE["dir"] / "main", main)
    _git(main, "remote", "set-url", "origin", str(remote))
    return tmp_path, main


def _build_repo(tmp_path):
    remote = tmp_path / "remote.git"
    main = tmp_path / "main"
    _git(tmp_path, "init", "--bare", "-b", "master", str(remote))
    _git(tmp_path, "init", "-b", "master", str(main))
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _git(main, "config", k, v)
    (main / ".gitignore").write_text(".claude-state/\n__pycache__/\n", encoding="utf-8")
    (main / "a.txt").write_text("a\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    _git(main, "remote", "add", "origin", str(remote))
    _git(main, "push", "-q", "origin", "master")
    return tmp_path, main


def _retire(workdir, **kw):
    args = [f"-WorkDir '{workdir}'"]
    for k, v in kw.items():
        args.append(f"-{k} '{v}'" if v is not True else f"-{k}")
    script = (
        # Invoke-Lane.ps1 dot-sources the helper under StrictMode Latest; test under the same
        # mode (an unset $LASTEXITCODE read only throws there).
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}"
        f"Invoke-RetireLaneWorktree {' '.join(args)} | ConvertTo-Json -Depth 4"
    )
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _add_wt(main, path):
    _git(main, "worktree", "add", "--detach", str(path), "origin/master")
    return path


def test_main_checkout_is_skipped(repo):
    _, main = repo
    d = _retire(main, MergeTarget="origin/master")
    assert d["action"] == "skipped" and d["reason"].startswith("not-a-linked-worktree: main")


def test_clean_linked_worktree_is_retired_and_branchless_safe(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-clean")
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"))
    assert (d["action"], d["reason"]) == ("retired", "ok")
    assert not wt.exists()


def test_dirty_worktree_is_kept(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-dirty")
    (wt / "new.txt").write_text("x", encoding="utf-8")
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"))
    assert d["action"] == "kept" and d["reason"].startswith("dirty")
    assert wt.exists()


def test_unpushed_commit_is_kept(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-unpushed")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "local")
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"))
    assert d["action"] == "kept" and d["reason"].startswith("unpushed")


def test_ignored_evidence_is_quarantined_not_deleted(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-ignored")
    (wt / ".claude-state").mkdir()
    (wt / ".claude-state" / "receipt.json").write_text("{}", encoding="utf-8")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
    q = tmp / "q"
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(q))
    assert (d["action"], d["reason"]) == ("retired", "ok")
    moved = list(q.glob("wt-ignored-*/.claude-state/receipt.json"))
    assert len(moved) == 1 and moved[0].is_file()


def test_ignored_evidence_without_quarantine_root_is_kept(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-noq")
    (wt / ".claude-state").mkdir()
    (wt / ".claude-state" / "e.txt").write_text("x", encoding="utf-8")
    d = _retire(wt, MergeTarget="origin/master")
    # Pin the explicit guard, not just the fail-closed outcome a later throw would also give.
    assert d["action"] == "kept" and "no -QuarantineRoot" in d["reason"]
    assert (wt / ".claude-state" / "e.txt").is_file()


def test_nested_registered_worktree_is_kept(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-outer")
    (wt / ".claude-state").mkdir()
    _git(main, "worktree", "add", "--detach", str(wt / ".claude-state" / "inner"), "origin/master")
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"))
    assert d["action"] == "kept" and d["reason"].startswith("nested-worktree")
    assert (wt / ".claude-state" / "inner" / "a.txt").is_file()


def test_non_empty_stash_keeps_worktree(repo):
    tmp, main = repo
    (main / "a.txt").write_text("changed\n", encoding="utf-8")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@t", "stash")
    wt = _add_wt(main, tmp / "wt-stash")
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"))
    assert d["action"] == "kept" and d["reason"].startswith("stash")


def test_protected_run_dir_inside_worktree_is_kept(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rundir")
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"),
                ProtectPath=str(wt / ".claude-state" / "fleet-runs" / "x"))
    assert d["action"] == "kept" and d["reason"].startswith("rundir-inside")


def test_unresolvable_merge_target_is_cannot_determine_not_pass(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-badref")
    d = _retire(wt, MergeTarget="refs/heads/no-such-ref", QuarantineRoot=str(tmp / "q"))
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine")
    assert wt.exists()


# --- Invoke-SweepMergedLaneWorktrees: every lane exit re-asks the SAFE gate about the others ---
# Each case asserts what was retired AND, for the kept ones, the reason prefix the gate gave
# (kept{prefix: count}), so a sweep that keeps a worktree for the wrong reason fails too.

def _ps_arg(v):
    if isinstance(v, (list, tuple)):
        return "@(" + ",".join(f"'{x}'" for x in v) + ")"
    if isinstance(v, int):
        return str(v)
    return f"'{v}'"


def _sweep(main, **kw):
    args = [f"-RepoRoot '{main}'"]
    for k, v in kw.items():
        args.append(f"-{k}" if v is True else f"-{k} {_ps_arg(v)}")
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}"
        f"Invoke-SweepMergedLaneWorktrees {' '.join(args)} | ConvertTo-Json -Depth 5"
    )
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _norm(p):
    return os.path.normcase(os.path.normpath(str(p)))


def _swept_paths(summary):
    return sorted(_norm(p) for p in summary["retired"])


def test_sweep_retires_merged_clean_idle_worktree_under_root(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-merged")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"))
    assert s["schema"] == "mlv-app/merged-worktree-sweep/v1"
    assert (s["considered"], s["young"], s["notReached"], s["error"]) == (1, 0, 0, None)
    assert _swept_paths(s) == [_norm(wt)] and s["kept"] == {}
    assert not wt.exists()


def test_sweep_leaves_a_worktree_outside_root_untouched(repo):
    tmp, main = repo
    root = tmp / "lanes"
    inside = _add_wt(main, root / "lane-in")
    outside = _add_wt(main, tmp / "elsewhere" / "wt-out")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"))
    assert s["considered"] == 1 and _swept_paths(s) == [_norm(inside)]
    assert outside.exists() and not inside.exists()


def test_sweep_leaves_an_excluded_path_untouched(repo):
    tmp, main = repo
    root = tmp / "lanes"
    keep = _add_wt(main, root / "lane-excluded")
    gone = _add_wt(main, root / "lane-other")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
               QuarantineRoot=str(tmp / "q"), Exclude=[str(keep).upper()])
    assert s["considered"] == 1 and _swept_paths(s) == [_norm(gone)]
    assert keep.exists() and not gone.exists()


def test_sweep_skips_a_young_worktree_before_the_gate(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-young")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=1000, QuarantineRoot=str(tmp / "q"))
    assert (s["considered"], s["young"], s["retired"], s["kept"]) == (1, 1, [], {})
    assert wt.exists()


def test_sweep_keeps_an_unmerged_worktree_with_the_gate_reason(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-unmerged")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "side work")
    _git(wt, "push", "-q", "origin", "HEAD:refs/heads/side")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"))
    assert (s["considered"], s["retired"], s["kept"]) == (1, [], {"unmerged": 1})
    assert wt.exists()


def test_sweep_budget_zero_reports_everything_not_reached_and_removes_nothing(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wts = [_add_wt(main, root / f"lane-b{i}") for i in range(2)]
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
               QuarantineRoot=str(tmp / "q"), BudgetSeconds=0)
    assert (s["considered"], s["notReached"], s["retired"], s["young"]) == (2, 2, [], 0)
    assert all(w.exists() for w in wts)


def test_sweep_whatif_lists_the_worktree_but_removes_nothing(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-whatif")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
               QuarantineRoot=str(tmp / "q"), WhatIf=True)
    assert _swept_paths(s) == [_norm(wt)] and s["kept"] == {}
    assert wt.exists()


# --- DISK-SWEEP-FAIR-ORDER-1: a budgeted sweep in a STABLE order starves its tail ---
# `git worktree list` order is stable, the SAFE gate costs ~36 s per worktree on the real host
# (measured: ~93% is the two CIM process scans), so the 180 s budget reaches ~5-10 worktrees. The same
# long-lived unmerged/dirty head ate every budget and a merged worktree in the tail was never retired.
# The sweep now visits least-recently-examined first, using a stamp file in each worktree's git
# admin dir. `-MaxExamine` caps GATE CALLS per sweep: a count is deterministic where a wall-clock
# budget (the gate's cost varies 2x run to run) would make these tests flaky or slow.

STAMP = "mlv-sweep-examined"


def _admin_dir(main, wt):
    # `git worktree add` names the admin dir after the worktree's leaf (no collisions in these tests).
    return Path(main) / ".git" / "worktrees" / Path(wt).name


def _stamped(main, wts):
    return [Path(w).name for w in wts if (_admin_dir(main, w) / STAMP).is_file()]


def _listed_order(main):
    out = subprocess.run([GIT, "-C", str(main), "worktree", "list", "--porcelain"],
                         check=True, capture_output=True, text=True).stdout
    return [_norm(ln[len("worktree "):]) for ln in out.splitlines() if ln.startswith("worktree ")]


def _three_kept_then_merged(repo):
    """Two unmerged worktrees plus a MERGED one that sorts last in `git worktree list` order."""
    tmp, main = repo
    root = tmp / "lanes"
    kept = []
    for name in ("lane-a-unmerged", "lane-b-unmerged"):
        wt = _add_wt(main, root / name)
        _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", name)
        _git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
        kept.append(wt)
    merged = _add_wt(main, root / "lane-z-merged")
    assert _listed_order(main)[-1] == _norm(merged), "fixture precondition: merged worktree last in list order"
    return root, kept, merged


def test_sweep_with_one_gate_call_per_sweep_reaches_a_merged_worktree_placed_last(repo):
    tmp, main = repo
    root, kept, merged = _three_kept_then_merged(repo)
    everyone = kept + [merged]
    seen = []
    for n in (1, 2, 3):
        s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
                   QuarantineRoot=str(tmp / "q"), MaxExamine=1)
        assert (s["considered"], s["examined"], s["error"]) == (3, 1, None), s
        now = [x for x in _stamped(main, everyone) if x not in seen]
        if n < 3:
            # a kept worktree is examined, stamped, and NOT the same one as in a previous sweep
            assert len(now) == 1 and s["retired"] == [] and s["kept"] == {"unmerged": 1} and s["notReached"] == 2, (n, s, now)
            seen += now
        else:
            # the third sweep reaches the tail: the merged worktree, never examined before, is retired
            assert _swept_paths(s) == [_norm(merged)] and s["kept"] == {} and s["notReached"] == 2, s
    assert seen == ["lane-a-unmerged", "lane-b-unmerged"]
    assert not merged.exists() and all(w.exists() for w in kept)
    # retiring removes the admin dir with the stamp in it; the survivors keep theirs
    assert not _admin_dir(main, merged).exists()
    assert _stamped(main, kept) == ["lane-a-unmerged", "lane-b-unmerged"]


def test_sweep_orders_by_stamp_ascending_with_missing_oldest(repo):
    tmp, main = repo
    root, kept, merged = _three_kept_then_merged(repo)
    # Pre-stamp the FIRST-listed worktree as examined far in the future: a stable order would visit it first.
    (_admin_dir(main, kept[0]) / STAMP).write_text("2999-01-01T00:00:00.0000000Z", encoding="utf-8")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
               QuarantineRoot=str(tmp / "q"), MaxExamine=1, WhatIf=True)
    # the unstamped worktrees outrank it; list order breaks the tie, so lane-b is the one examined
    assert s["examined"] == 1 and s["retired"] == [] and s["kept"] == {"unmerged": 1}, s
    assert _stamped(main, kept) == ["lane-a-unmerged", "lane-b-unmerged"]
    assert (_admin_dir(main, kept[0]) / STAMP).read_text(encoding="utf-8").startswith("2999-")  # untouched
    assert not (_admin_dir(main, merged) / STAMP).exists()


def test_sweep_stamps_a_would_retire_worktree_and_reports_examined_and_elapsed(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-whatif-stamp")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
               QuarantineRoot=str(tmp / "q"), WhatIf=True)
    assert (s["examined"], s["stampWriteFailed"]) == (1, 0) and _swept_paths(s) == [_norm(wt)]
    assert isinstance(s["elapsedMs"], int) and s["elapsedMs"] > 0
    stamp = (_admin_dir(main, wt) / STAMP).read_text(encoding="utf-8").strip()
    assert stamp.endswith("Z") and stamp[:4].isdigit()  # UTC ISO-8601
    assert not any(p.name == STAMP for p in wt.rglob("*")), "nothing is ever written into the worktree itself"


def test_sweep_young_worktrees_never_reach_the_gate_and_are_not_stamped(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-young2")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=1000, QuarantineRoot=str(tmp / "q"))
    assert (s["young"], s["examined"]) == (1, 0)
    assert not (_admin_dir(main, wt) / STAMP).exists()


def test_stamp_file_never_makes_a_worktree_look_young(repo):
    """The young guard reads worktree-dir CreationTime + <gitdir>\\HEAD + <gitdir>\\index mtimes ONLY."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-aged")
    gdir = _admin_dir(main, wt)

    def age():  # back-date the three stamps the guard reads, to 10 h ago
        ps = ("$t=(Get-Date).ToUniversalTime().AddHours(-10); "
              f"(Get-Item -LiteralPath '{wt}').CreationTimeUtc=$t; "
              f"(Get-Item -LiteralPath '{gdir / 'HEAD'}').LastWriteTimeUtc=$t; "
              f"(Get-Item -LiteralPath '{gdir / 'index'}').LastWriteTimeUtc=$t")
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", ps], check=True, capture_output=True)

    age()
    first = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=6,
                   QuarantineRoot=str(tmp / "q"), WhatIf=True)
    assert (first["young"], first["examined"]) == (0, 1), first
    assert (gdir / STAMP).is_file()
    age()  # the gate's own `git status` may refresh the index mtime; undo that, keep the stamp
    second = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=6,
                    QuarantineRoot=str(tmp / "q"), WhatIf=True)
    assert (second["young"], second["examined"]) == (0, 1), "the stamp file made an aged worktree look young"


def test_sweep_stamp_write_failure_is_counted_and_never_fails_the_sweep(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "lane-nostamp")
    (_admin_dir(main, wt) / STAMP).mkdir()  # a directory where the stamp file belongs: the write must fail
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0,
               QuarantineRoot=str(tmp / "q"), WhatIf=True)
    assert (s["error"], s["examined"], s["stampWriteFailed"]) == (None, 1, 1), s
    # the verdict is the gate's, untouched by the bookkeeping failure, and it is not double-counted in kept{}
    assert _swept_paths(s) == [_norm(wt)] and s["kept"] == {}


# --- Invoke-RetireLaneWorktree -ProcessSnapshot: one CIM scan per sweep instead of two-plus per worktree ---

def _gate_with_snapshot(workdir, snapshot_expr, prelude=""):
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {prelude} "
        f"$snap = {snapshot_expr}; "
        f"Invoke-RetireLaneWorktree -WorkDir '{workdir}' -MergeTarget 'origin/master' -WhatIf -ProcessSnapshot $snap "
        "| ConvertTo-Json -Depth 4"
    )
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_process_snapshot_naming_the_worktree_keeps_it_as_live_process(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-snap-live")
    # pid 999999 does not exist on the host, so a live-process verdict can only come from the SNAPSHOT.
    snap = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999999; Name = 'fake-editor.exe'; "
            f"CommandLine = 'fake-editor.exe --open {wt}' }}); SelfPids = @($PID) }}")
    d = _gate_with_snapshot(wt, snap)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: 999999 fake-editor.exe"), d
    assert wt.exists()


def test_process_snapshot_matches_the_forward_slash_spelling_too(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-snap-slash")
    slash = str(wt).replace("\\", "/")
    snap = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999998; Name = 'git.exe'; "
            f"CommandLine = 'git -C {slash} status' }}); SelfPids = @($PID) }}")
    assert _gate_with_snapshot(wt, snap)["reason"].startswith("live-process: 999998 git.exe")


def test_process_snapshot_naming_another_path_does_not_block_retirement(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-snap-other")
    snap = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999997; Name = 'x.exe'; "
            "CommandLine = 'x.exe C:\\somewhere\\else' }); SelfPids = @($PID) }")
    d = _gate_with_snapshot(wt, snap)
    assert (d["action"], d["reason"]) == ("would-retire", "ok")


def test_process_snapshot_self_pids_are_excluded_from_the_live_check(repo):
    """The caller's own ancestors name the worktree legitimately (a lane running inside it)."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-snap-self")
    snap = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999996; Name = 'pwsh.exe'; "
            f"CommandLine = 'pwsh {wt}' }}); SelfPids = @(999996) }}")
    assert _gate_with_snapshot(wt, snap)["action"] == "would-retire"


# --- WORKTREE-REMOVED-UNDER-LIVE-CHAIN-1: a live HOLDER that never names the path on its command line ---
# 2026-10-09 00:19:54Z the lane-exit sweep retired C:\mlvtmp\lane-PLAYBACK-GL-PRESENT-BACKLOG-1-20261008 under a live
# measure chain. The chain's command lines named its RUN DIR; the worktree was named only inside a dot-sourced
# arms.ps1 ($script:Wt) and on the command line of a probe child that exists for ~40 s of every wait loop.
# The gate must also refuse on (a) a process whose CURRENT DIRECTORY is the worktree, (b) a live script process
# whose own script or a sibling script it dot-sources / calls by name (transitively) names the worktree path.

def _spawn(args, cwd):
    return subprocess.Popen([PWSH, "-NoProfile", "-NonInteractive", *args], cwd=str(cwd),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(p):
    p.kill()
    p.wait(timeout=30)


def _gate_real(workdir, *, whatif=True, prelude="", extra=""):
    """Gate with the REAL process snapshot. Returns (disposition, stderr). `prelude` runs after the helper is dot-sourced; `extra` is
    appended to the Invoke-RetireLaneWorktree arguments."""
    quarantine = Path(workdir).parent / "q"
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}{prelude} "
        f"Invoke-RetireLaneWorktree -WorkDir '{workdir}' -MergeTarget 'origin/master' -QuarantineRoot '{quarantine}'"
        f"{' -WhatIf' if whatif else ''}{extra} | ConvertTo-Json -Depth 4"
    )
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                       check=True, capture_output=True, text=True)
    return json.loads(r.stdout), r.stderr


def test_process_whose_cwd_is_the_worktree_keeps_it_and_the_refusal_names_the_pid(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-cwd")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=wt)   # command line does NOT name the worktree
    try:
        d, err = _gate_real(wt)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d
    assert "cwd" in d["reason"], d
    assert "REFUSED" in err and f"pid={p.pid}" in err, err
    assert wt.exists()


def test_process_whose_cwd_is_inside_the_worktree_keeps_it(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-cwd-sub")
    sub = wt / "deep" / "er"
    sub.mkdir(parents=True)
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=sub)
    try:
        d, _ = _gate_real(wt)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d


def _chain_scripts(tmp, wt_text):
    """chain.ps1 -> quiet.ps1 -> arms.ps1 (the incident's shape); only arms.ps1 names the worktree."""
    sd = tmp / "chain-run" / "tools"
    sd.mkdir(parents=True)
    (sd / "arms.ps1").write_text(f"$script:Wt = '{wt_text}'\n", encoding="utf-8")
    (sd / "quiet.ps1").write_text('. "$PSScriptRoot\\arms.ps1"\nStart-Sleep -Seconds 120\n', encoding="utf-8")
    (sd / "chain.ps1").write_text('& "$PSScriptRoot\\quiet.ps1"\n', encoding="utf-8")
    return sd


def test_live_script_whose_dot_sourced_sibling_names_the_worktree_keeps_it(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-script")
    sd = _chain_scripts(tmp, str(wt))
    p = _spawn(["-File", str(sd / "chain.ps1")], cwd=tmp)           # cwd elsewhere; command line names the RUN DIR only
    try:
        d, err = _gate_real(wt)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d
    assert "script" in d["reason"] and "arms.ps1" in d["reason"], d
    assert f"pid={p.pid}" in err, err
    assert wt.exists()


def test_live_script_naming_the_worktree_with_forward_slashes_keeps_it(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-script-slash")
    sd = _chain_scripts(tmp, str(wt).replace("\\", "/"))
    p = _spawn(["-File", str(sd / "chain.ps1")], cwd=tmp)
    try:
        d, _ = _gate_real(wt)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d


def test_live_script_that_does_not_name_the_worktree_does_not_block_retirement(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-script-other")
    sd = _chain_scripts(tmp, str(tmp / "some-other-place"))
    p = _spawn(["-File", str(sd / "chain.ps1")], cwd=tmp)
    try:
        d, err = _gate_real(wt)
    finally:
        _stop(p)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert "REFUSED" not in err, err


def test_snapshot_cwd_inside_the_worktree_counts_but_a_sibling_with_the_same_prefix_does_not(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-cwdsnap")
    inside = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999995; Name = 'shell.exe'; "
              f"CommandLine = 'shell.exe'; CurrentDirectory = '{wt}\\sub' }}); SelfPids = @($PID) }}")
    d = _gate_with_snapshot(wt, inside)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: 999995 shell.exe"), d
    sibling = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = 999994; Name = 'shell.exe'; "
               f"CommandLine = 'shell.exe'; CurrentDirectory = '{wt}2' }}); SelfPids = @($PID) }}")
    assert _gate_with_snapshot(wt, sibling)["action"] == "would-retire"


def test_holder_exit_releases_the_worktree_for_real_retirement(repo):
    """End to end, not WhatIf: refused while the holder lives, retired once it is gone."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-release")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=wt)
    try:
        d, _ = _gate_real(wt, whatif=False)
        assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d
        assert wt.exists()
    finally:
        _stop(p)
    d, _ = _gate_real(wt, whatif=False)
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert not wt.exists()


# --- CI-FLAKE-RETIRE-HOLDER-EXIT-PERMISSION-DENIED-1: a handle that outlives the holder's gate-visible life ---
# 2026-10-17 PR #481 windows shard 8/8 (run 39049834615, job 117460346707): the holder had exited and the gate saw no holder, but Windows
# still held a handle inside the worktree when `git worktree remove` ran: "failed to delete ...\wt-release: Permission denied".
# git half-fails there (the worktree is deregistered, its files stay), so the same command cannot be re-run; the tool must finish the
# removal itself, bounded, keyed to the access-denied / sharing-violation condition, and report `retired` only with the directory gone.

def _spawn_file_handle_holder(path, ready, release, tmp, trigger=None, mode="Open", access="Read", share="Read"):
    """A child that opens `path` (default: read, no delete share) and keeps it open until `release` exists. The path goes in the
    ENVIRONMENT, not on the command line, so the gate (which reads command lines and cwds) does not see a holder, exactly
    like a handle that outlives its process's visibility.
    r7 (rename probe): a handle open BEFORE the gate runs now keeps the worktree as in-use (the rename is refused), so the cases that
    exercise the removal RETRY need the handle to appear AFTER the rename. With `trigger` the child waits for that file, then opens
    `path` (the renamed location) and writes `ready`; the caller does not wait for it (see _handle_appears_after_the_rename).
    r4 (sol r2 hardening on PR #603): the wait for `trigger` is bounded (60 s) and also ends on `release`, so a retirement that returns
    before the rename never leaves the child polling; callers clean up with _finish_holder, which kills a child that does not exit.
    r5: mode / access / share are the [IO.File]::Open arguments (share 'ReadWrite, Delete' = a holder that allows deletion; mode
    'OpenOrCreate', share 'None' = a live operation holding a rename record's owner.lock)."""
    script = (("$w = [Diagnostics.Stopwatch]::StartNew(); while (-not (Test-Path -LiteralPath $env:HOLD_TRIGGER)) { "
               "if ((Test-Path -LiteralPath $env:HOLD_RELEASE) -or $w.Elapsed.TotalSeconds -ge 60) { exit 0 }; Start-Sleep -Milliseconds 20 }; "
               if trigger else "")
              + "$f = [IO.File]::Open($env:HOLD_FILE, $env:HOLD_MODE, $env:HOLD_ACCESS, $env:HOLD_SHARE); "
              "[IO.File]::WriteAllText($env:HOLD_READY, 'open'); "
              "$t = [Diagnostics.Stopwatch]::StartNew(); "
              "while (-not (Test-Path -LiteralPath $env:HOLD_RELEASE) -and $t.Elapsed.TotalSeconds -lt 90) { Start-Sleep -Milliseconds 50 }; "
              "$f.Dispose()")
    env = dict(os.environ, HOLD_FILE=str(path), HOLD_READY=str(ready), HOLD_RELEASE=str(release), HOLD_TRIGGER=str(trigger or ""),
               HOLD_MODE=mode, HOLD_ACCESS=access, HOLD_SHARE=share)
    p = subprocess.Popen([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], cwd=str(tmp), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if trigger:
        return p
    try:
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=60)
        while not Path(ready).exists():
            assert p.poll() is None and datetime.datetime.now() < deadline, "holder child did not open the file"
            time.sleep(0.05)
    except BaseException:
        _finish_holder(p, release)
        raise
    return p


def _finish_holder(p, release):
    """Let a holder child go and reap it. A child still alive after 60 s is killed, so no case can leak a pwsh process."""
    try:
        Path(release).write_text("go")
        p.wait(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=30)


def _handle_appears_after_the_rename(trigger, ready):
    """Prelude: wraps the rename seam. Right after the rename to <root>.retiring succeeds it creates `trigger` (the holder child opens its
    file in the renamed directory) and waits for `ready`, so a handle exists by the time the removal runs, as one that outlives its
    holder (a scanner, an indexer) does. The rename BACK is passed through untouched."""
    return ("$origMoveRoot = ${function:Move-LaneWorktreeRoot}; "
            "function Move-LaneWorktreeRoot { param([string]$From, [string]$To) $e = & $origMoveRoot -From $From -To $To; "
            f"if (-not $e -and $To -like '*.retiring') {{ [IO.File]::WriteAllText('{trigger}', 'go'); $t = [Diagnostics.Stopwatch]::StartNew(); "
            f"while (-not (Test-Path -LiteralPath '{ready}') -and $t.Elapsed.TotalSeconds -lt 30) {{ Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds 20 }} }}; $e }}; ")


# The retry window scans the host again before EVERY attempt (r2), and a long-lived host pwsh caught exiting mid-scan reads as
# cwd-unknown (relative-script-unresolved) and refuses a removal, by design (reproduced 1 in 4 in isolation). These cases are about the
# TEST'S OWN holders, so their snapshot is limited to pytest's direct children; the real-holder children are all that matters to them.
ONLY_PYTEST_CHILDREN = (
    "$origChildrenOnly = ${function:Get-LaneProcessSnapshot}; "
    "function Get-LaneProcessSnapshot { $s = & $origChildrenOnly; "
    "[pscustomobject]@{ Procs = @($s.Procs | Where-Object { $_.ParentProcessId -eq " + str(PYTEST_PID) + " }); SelfPids = $s.SelfPids; CapturedUtc = $s.CapturedUtc } }; "
)


def _release_on_first_wait(release, budget_ms=20000, delay_ms=50):
    """Prelude: the tool's own first back-off creates `release`, so the child's handle drops (and the child exits) after the
    first failed removal. No timing guess: a tool that never waits never lets go, and stays red."""
    return (ONLY_PYTEST_CHILDREN + f"$script:RemoveRetryBudgetMs = {budget_ms}; $script:RemoveRetryDelayMs = {delay_ms}; "
            "function Start-Sleep { param([int]$Milliseconds = 0, [double]$Seconds = 0) "
            f"if (-not (Test-Path -LiteralPath '{release}')) {{ [IO.File]::WriteAllText('{release}', 'go') }}; "
            "Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds ([Math]::Max($Milliseconds, 400)) }; ")


def test_a_handle_that_outlives_the_holder_is_waited_out_and_the_worktree_is_really_retired(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-handle")
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=_release_on_first_wait(release) + _handle_appears_after_the_rename(trigger, ready))
    finally:
        _finish_holder(p, release)
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert not wt.exists() and not _retiring_of(wt).exists(), "retired must mean the directory is gone"
    assert d["removeAttempts"] >= 2 and d["removeNotes"], d


def _release_on_first_failed_rename_back(release):
    """Prelude: wraps the rename seam. The first rename BACK that fails (the late handle still open) creates `release`, so the holder lets
    go while the tool is still inside its rename-back budget. A tool that tries the rename back only once never sees it drop."""
    return ("$origMoveBack = ${function:Move-LaneWorktreeRoot}; "
            "function Move-LaneWorktreeRoot { param([string]$From, [string]$To) $e = & $origMoveBack -From $From -To $To; "
            f"if ($e -and $From -like '*.retiring' -and -not (Test-Path -LiteralPath '{release}')) {{ [IO.File]::WriteAllText('{release}', 'go') }}; $e }}; ")


def _assert_back_at_its_own_path(main, wt):
    """The acceptance end state of a kept worktree after a rename probe: at its own path, registered there, usable, no .retiring."""
    assert wt.exists() and (wt / ".git").exists() and (wt / "a.txt").exists(), "the worktree must be back at its own path"
    assert not _retiring_of(wt).exists(), "no .retiring directory may be left behind"
    paths = _worktree_paths(main)
    assert _norm(wt) in paths and not any(p.endswith(".retiring") for p in paths), paths
    st = subprocess.run([GIT, "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True)
    assert st.returncode == 0 and st.stdout.strip() == "", (st.returncode, st.stdout, st.stderr)
    assert _pending_records(main) == [], "a settled rename leaves no rename record"


def _pending_records(main):
    root = Path(main) / ".git" / "mlv-retire-pending"
    return sorted(os.listdir(root)) if root.exists() else []


def _strand_with_a_late_handle(wt, tmp, restore_budget_ms):
    """Rename ok -> a handle opens beneath <wt>.retiring and is still open through the removal budget AND the rename-back budget.
    Returns (disposition, stderr, holder, release); the holder is still holding."""
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    try:
        d, err = _gate_real(wt, whatif=False, prelude=ONLY_PYTEST_CHILDREN + "$script:RemoveRetryBudgetMs = 1500; $script:RemoveRetryDelayMs = 100; "
                            f"$script:RestoreRetryBudgetMs = {restore_budget_ms}; $script:RestoreRetryDelayMs = 100; "
                            + _handle_appears_after_the_rename(trigger, ready))
    except BaseException:
        _finish_holder(p, release)
        raise
    return d, err, p, release


def test_a_late_handle_that_fails_the_removal_is_waited_out_by_the_rename_back(repo):
    """NEW CATALOGUE ROW (sol r2 blocker on PR #603): successful rename -> persistent late handle beneath .retiring -> failed removal.
    The handle outlives the whole removal budget, so the removal fails (git half-removes: deregistered, `.git` link gone, files left);
    it lets go while the rename back is being retried. End state: kept / remove-failed, back at its own path, registered and clean."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-late-restore")
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=ONLY_PYTEST_CHILDREN + "$script:RemoveRetryBudgetMs = 1500; $script:RemoveRetryDelayMs = 100; "
                          "$script:RestoreRetryBudgetMs = 15000; $script:RestoreRetryDelayMs = 300; "
                          + _handle_appears_after_the_rename(trigger, ready) + _release_on_first_failed_rename_back(release))
    finally:
        _finish_holder(p, release)
    assert d["action"] == "kept" and d["reason"].startswith("remove-failed: "), d
    assert "attempt" in d["reason"] and d["removeAttempts"] >= 2, d
    assert d["renameRestore"]["state"] == "restored" and d["renameRestore"]["attempts"] >= 2, d
    assert "admin entry restored" in d["renameRestore"]["note"], d   # git had half-removed it; the registration is back too
    _assert_back_at_its_own_path(main, wt)


def test_a_handle_that_outlives_the_rename_back_budget_is_restored_first_by_the_next_sweep(repo):
    """The same row, with a handle that outlives the rename-back budget too: this call cannot put it back, says so loudly, and leaves a
    rename record; once the handle is gone the NEXT sweep restores <wt>.retiring to <wt> before any gate decision (registration and the
    files git had deleted included), and only then judges the worktree at its own path."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-stuck")
    d, err, p, release = _strand_with_a_late_handle(wt, tmp, restore_budget_ms=800)
    _finish_holder(p, release)
    assert d["action"] == "kept" and d["reason"].startswith("remove-failed: "), d
    assert d["renameRestore"]["state"] == "failed" and d["renameRestore"]["attempts"] >= 2, d
    assert "rename back" in d["renameRestore"]["note"] and "next sweep" in d["renameRestore"]["note"], d
    assert "LEFT-RETIRING" in err, err
    assert _retiring_of(wt).exists() and not wt.exists() and len(_pending_records(main)) == 1
    # -WhatIf renames nothing, not even back: it only reports the stranded tree.
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"), WhatIf=True)
    assert [r["state"] for r in s["strandedRestore"]] == ["not-run: WhatIf"] and _retiring_of(wt).exists(), s
    # A real sweep restores it before any candidate is read. r5 (H1): the restore now counts against -BudgetSeconds (see
    # test_restore_first_counts_against_the_sweep_budget), so the restored tree is kept from the gate by the young guard instead.
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=24, QuarantineRoot=str(tmp / "q"))
    rows = s["strandedRestore"]
    assert len(rows) == 1 and _norm(rows[0]["original"]) == _norm(wt) and rows[0]["state"] == "restored", s
    assert "admin entry restored" in rows[0]["note"] and "status clean" in rows[0]["note"], rows   # git had half-removed it
    assert s["retired"] == [] and s["young"] == 1, s   # judged at its own path in the same sweep (listed there, not as .retiring)
    _assert_back_at_its_own_path(main, wt)
    d, _ = _gate_real(wt, whatif=True)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d   # judged at its own path once restored: clean and merged


def _strand_registered(main, wt):
    """A cheap stranded state with git still registering the tree (at <wt>.retiring): a LOCKED worktree makes `git worktree remove` refuse
    up front (nothing deleted), and a seam makes every rename back fail with a sharing violation."""
    _git(main, "worktree", "lock", "--reason", "strand-test", str(wt))
    # ONLY_PYTEST_CHILDREN: this fixture is about the rename back, not the host; an unrelated live pwsh whose cwd cannot be read would otherwise
    # refuse the removal as cwd-unknown here, in setup, before any case reaches the behaviour it is written for.
    prelude = (ONLY_PYTEST_CHILDREN + "$script:RestoreRetryBudgetMs = 300; $script:RestoreRetryDelayMs = 50; $origMoveSeam = ${function:Move-LaneWorktreeRoot}; "
               "function Move-LaneWorktreeRoot { param([string]$From, [string]$To) if ($From -like '*.retiring') { "
               "return 'The process cannot access the file because it is being used by another process. (seam)' }; & $origMoveSeam -From $From -To $To }; ")
    d, _ = _gate_real(wt, whatif=False, prelude=prelude)
    assert d["action"] == "kept" and d["reason"].startswith("remove-failed: "), d
    assert d["renameRestore"]["state"] == "failed" and d["renameRestore"]["attempts"] >= 2, d
    assert _retiring_of(wt).exists() and not wt.exists()
    _git(main, "worktree", "unlock", str(_retiring_of(wt)))


def test_the_next_call_for_a_stranded_worktree_restores_it_before_the_gate(repo):
    """Per candidate: the next Invoke-RetireLaneWorktree for <wt> finds <wt>.retiring and its rename record, restores it first (receipt
    field strandedRestore), and only then runs the gate on <wt>."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-strand-call")
    _strand_registered(main, wt)
    d, _ = _gate_real(wt, whatif=True)
    assert d["strandedRestore"]["state"] == "not-run: WhatIf" and d["reason"].startswith("stranded-retiring: "), d
    assert _retiring_of(wt).exists() and not wt.exists(), "-WhatIf renames nothing"
    d, _ = _gate_real(wt, whatif=False)
    assert d["strandedRestore"]["state"] == "restored", d
    # restored first, then the whole gate ran on <wt> itself: rename probe, removal, nothing left under either name
    assert (d["action"], d["reason"], d["renameProbe"]) == ("retired", "ok", "ok"), d
    assert not wt.exists() and not _retiring_of(wt).exists()
    assert not any(p.startswith(_norm(wt)) for p in _worktree_paths(main)) and _pending_records(main) == [], _worktree_paths(main)


def test_a_stranded_retiring_whose_original_path_exists_again_is_refused_and_left_alone(repo):
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-strand-taken")
    _strand_registered(main, wt)
    wt.mkdir()
    (wt / "newcomer.txt").write_text("someone else's", encoding="utf-8")
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"))
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "refused" and "exists again" in rows[0]["note"], s
    assert (wt / "newcomer.txt").read_text(encoding="utf-8") == "someone else's" and sorted(os.listdir(wt)) == ["newcomer.txt"]
    assert (_retiring_of(wt) / ".git").exists() and (_retiring_of(wt) / "a.txt").exists(), "the .retiring tree must be untouched"
    assert s["retired"] == [] and s["kept"] == {"stranded-retiring": 1}, s   # the listed .retiring entry is never gated


def test_a_retiring_directory_this_tool_did_not_rename_is_never_restored_or_gated(repo):
    tmp, main = repo
    wt = tmp / "wt-strand-foreign"
    _retiring_of(wt).mkdir()
    (_retiring_of(wt) / "marker.txt").write_text("not ours", encoding="utf-8")
    d, _ = _gate_real(wt, whatif=False, extra=f" -RepoRoot '{main}'")
    assert d["action"] == "kept" and d["reason"].startswith("stranded-retiring: ") and "no rename record" in d["reason"], d
    assert d["strandedRestore"]["state"] == "refused", d
    assert not wt.exists() and (_retiring_of(wt) / "marker.txt").read_text(encoding="utf-8") == "not ours"
    d2, _ = _gate_real(_retiring_of(wt), whatif=False)
    assert d2["action"] == "kept" and d2["reason"].startswith("stranded-retiring: "), d2
    assert (_retiring_of(wt) / "marker.txt").exists()


# --- r5 (fable r4 + sol r4 blocker on PR #603): a rename record belongs to the operation that wrote it until that operation decides ---
# Two lane exits sweep at once (Invoke-Lane.ps1 runs one per exit, no mutex) and the fair order gives both the same first candidate. A renames
# X to X.retiring and its removal half-fails on a late handle; while A backs off, B's restore-first read A's record, moved X.retiring back
# and deleted the record, and A then reported retired/ok because its path was gone, with X present and registered. The record now carries
# owner.lock, held open (FileShare.None) by the operation from before the rename until its final decision; a restorer must claim it first.

def _two_sweep_prelude(release, holder_pid, b_script):
    """Prelude for A: at its FIRST removal back-off the late handle is let go (and its process reaped), then sweep B runs to completion as a
    separate process, exactly inside A's window. No timing guess: B runs while A is provably between two removal attempts."""
    return (ONLY_PYTEST_CHILDREN + "$script:RemoveRetryBudgetMs = 20000; $script:RemoveRetryDelayMs = 50; $script:BRan = $false; "
            "function Start-Sleep { param([int]$Milliseconds = 0, [double]$Seconds = 0) if (-not $script:BRan) { $script:BRan = $true; "
            f"[IO.File]::WriteAllText('{release}', 'go'); Wait-Process -Id {holder_pid} -Timeout 60 -ErrorAction SilentlyContinue; "
            f"& '{PWSH}' -NoProfile -NonInteractive -File '{b_script}' | Out-Null }}; "
            "Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds $Milliseconds }; ")


def test_an_overlapping_sweep_never_restores_a_live_retirements_record(repo):
    """Acceptance (both keys' catalogue row): overlapping sweeps with a live pending record. B never restores A's rename or deletes its
    record (its row says 'owned'), A never reports retired while X is present or registered, and no deregistered orphan or record is left."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-overlap")
    b_out, b_script = tmp / "b-sweep.json", tmp / "b-sweep.ps1"
    b_script.write_text(
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}"
        f"Invoke-SweepMergedLaneWorktrees -RepoRoot '{main}' -Root '{root}' -MergeTarget 'origin/master' -MinIdleHours 0 "
        f"-QuarantineRoot '{tmp / 'q'}' | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath '{b_out}' -Encoding utf8\n", encoding="utf-8")
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=_two_sweep_prelude(release, p.pid, b_script) + _handle_appears_after_the_rename(trigger, ready))
    finally:
        _finish_holder(p, release)
    b = json.loads(b_out.read_text(encoding="utf-8-sig"))
    rows = b["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "owned" and _norm(rows[0]["original"]) == _norm(wt), b   # B left A's record alone
    assert b["retired"] == [], b
    assert d["removeAttempts"] >= 2, d   # B really ran inside A's removal retry window
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert not wt.exists() and not _retiring_of(wt).exists(), "retired must mean neither path exists"
    assert not any(p_.startswith(_norm(wt)) for p_ in _worktree_paths(main)), _worktree_paths(main)
    assert _pending_records(main) == [], "the owner settles its own record"


def test_a_tree_moved_back_by_another_operation_during_the_removal_is_never_reported_retired(repo):
    """(b) The retry loop used to declare retired whenever its .retiring path was gone, whoever moved it. An actor that ignores the owner
    lock (an older copy of this tool) moves X.retiring back to X in A's back-off: A keeps it (cannot-determine, 'exists again'), re-registers
    the tree from its own record (git had half-removed it) and leaves it at its own path, registered and clean."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-moved-back")
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    prelude = (ONLY_PYTEST_CHILDREN + "$script:RemoveRetryBudgetMs = 20000; $script:RemoveRetryDelayMs = 50; $script:Moved = $false; "
               "function Start-Sleep { param([int]$Milliseconds = 0, [double]$Seconds = 0) if (-not $script:Moved) { $script:Moved = $true; "
               f"[IO.File]::WriteAllText('{release}', 'go'); Wait-Process -Id {p.pid} -Timeout 60 -ErrorAction SilentlyContinue; "
               f"[IO.Directory]::Move('{_retiring_of(wt)}', '{wt}') }}; Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds $Milliseconds }}; "
               + _handle_appears_after_the_rename(trigger, ready))
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=prelude)
    finally:
        _finish_holder(p, release)
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine: ") and "exists again" in d["reason"], d
    assert d["renameRestore"]["state"] == "restored", d
    _assert_back_at_its_own_path(main, wt)


def test_a_record_whose_owner_lock_is_held_is_never_restored_and_is_restored_once_released(repo):
    """The claim itself: a live process holds the record's owner.lock (FileShare.None). The sweep reports the record 'owned' and touches
    neither path nor the record; once the holder lets go (exit or crash, the OS closes the handle) the next sweep restores it."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-owned")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    assert len(recs) == 1, recs
    lock = Path(main) / ".git" / "mlv-retire-pending" / recs[0] / "owner.lock"
    ready, release = tmp / "lock.ready", tmp / "lock.release"
    p = _spawn_file_handle_holder(lock, ready, release, tmp, mode="OpenOrCreate", access="ReadWrite", share="None")
    try:
        s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=24, QuarantineRoot=str(tmp / "q"))
    finally:
        _finish_holder(p, release)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "owned" and "live operation" in rows[0]["note"], s
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "nothing may be touched"
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=24, QuarantineRoot=str(tmp / "q"))
    assert [r["state"] for r in s["strandedRestore"]] == ["restored"], s
    _assert_back_at_its_own_path(main, wt)


def test_restore_first_counts_against_the_sweep_budget(repo):
    """H1 (RETIRE-RESTORE-FIRST-BUDGET-1): restore-first ran outside -BudgetSeconds, so N stranded records with persistent handles cost
    N x 15 s per lane exit. With the budget spent, a record is not restored: typed row, record and tree kept for the next sweep."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-budget")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"), BudgetSeconds=0)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "not-reached: budget" and rows[0]["attempts"] == 0, s
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "a budget stop keeps the record"


def test_a_failed_repair_after_a_good_rename_back_is_restored_unrepaired_and_retried(repo):
    """H2 (RETIRE-RESTORE-REPAIR-FAILED-STATE-1): the rename back works but `git worktree repair` fails. That is not 'restored': the state is
    restored-unrepaired and the record stays. The next sweep sees no .retiring but recognises its own tree at the original path, repairs it
    and drops the record, instead of marking it gone and leaving the tree unregistered."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-unrepaired")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}"
        "function Repair-LaneWorktreePath { param([string]$Main, [string]$Path) 'git worktree repair: seam failure' }; "
        f"Invoke-SweepMergedLaneWorktrees -RepoRoot '{main}' -Root '{root}' -MergeTarget 'origin/master' -MinIdleHours 24 "
        f"-QuarantineRoot '{tmp / 'q'}' | ConvertTo-Json -Depth 5")
    s = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True, text=True).stdout)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "restored-unrepaired" and "seam failure" in rows[0]["note"], s
    assert wt.exists() and not _retiring_of(wt).exists() and _pending_records(main) == recs, "the record stays until the repair succeeds"
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=24, QuarantineRoot=str(tmp / "q"))
    assert [r["state"] for r in s["strandedRestore"]] == ["restored"], s
    _assert_back_at_its_own_path(main, wt)


def _sweep_script(main, root, tmp, prelude=""):
    return ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
            f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}{prelude}"
            f"Invoke-SweepMergedLaneWorktrees -RepoRoot '{main}' -Root '{root}' -MergeTarget 'origin/master' -MinIdleHours 24 "
            f"-QuarantineRoot '{tmp / 'q'}' | ConvertTo-Json -Depth 5")


def test_a_half_removed_tree_whose_first_repair_fails_is_re_checked_out_by_the_retry(repo):
    """Dot R18 B1 on PR #603 (RETIRE-RESTORE-PENDING-CHECKOUT-PERSIST-1): git half-removed the tree (admin entry, `.git` link and tracked
    files deleted). The first restore re-creates the admin entry and the link, then its repair fails: restored-unrepaired, record kept. The
    retry finds both present, so a re-registration flag local to one call said nothing was owed and checkout-index never ran: 'restored'
    and the record dropped with the tracked files still missing. The debt now lives in the record until the re-checkout has run."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-half-unrepaired")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    ret = _retiring_of(wt)
    # What a half-failed `git worktree remove` leaves: the admin entry and the `.git` link gone, tracked files deleted.
    shutil.rmtree(_admin_dir(main, wt))   # named at `worktree add` time; the rename does not change it
    for f in (".git", "a.txt", ".gitignore"):
        (ret / f).unlink()
    seam = "function Repair-LaneWorktreePath { param([string]$Main, [string]$Path) 'git worktree repair: seam failure' }; "
    s = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", _sweep_script(main, root, tmp, seam)],
                                  check=True, capture_output=True, text=True).stdout)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "restored-unrepaired" and "seam failure" in rows[0]["note"], s
    assert "admin entry restored" in rows[0]["note"] and ".git link rewritten" in rows[0]["note"], rows
    assert wt.exists() and not ret.exists() and _pending_records(main) == recs, "the record stays until the repair succeeds"
    assert not (wt / "a.txt").exists(), "the fixture must leave the tracked files missing into the retry"
    rec = json.loads((Path(main) / ".git" / "mlv-retire-pending" / recs[0] / "record.json").read_text(encoding="utf-8-sig"))
    assert rec.get("checkoutOwed") is True, rec   # the debt outlives the call that incurred it
    s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=24, QuarantineRoot=str(tmp / "q"))
    rows = s["strandedRestore"]
    assert [r["state"] for r in rows] == ["restored"] and "status clean" in rows[0]["note"], s
    assert (wt / "a.txt").exists() and (wt / ".gitignore").exists(), "every tracked file is back"
    _assert_back_at_its_own_path(main, wt)


def _git_failure_shim(log, flag, sub, cap=None):
    """Prelude: a `git.exe` FUNCTION (PowerShell resolves a function before the executable, so the script's `& git.exe ...` lands here, no
    seam in the script). While `flag` exists, a git call carrying the subcommand `sub` (status | checkout-index) appends a line to `log`,
    exits 128 and prints a fatal line; every other call, and every call once `flag` is deleted, goes to the real git. `cap` shrinks
    $script:RestoreCheckoutMaxAttempts (the owed-checkout attempt cap) so the capped case needs few sweeps."""
    return ("$global:realGit = (Get-Command git.exe -CommandType Application | Select-Object -First 1).Source; "
            + (f"$script:RestoreCheckoutMaxAttempts = {cap}; " if cap else "")
            + "function git.exe { "
            f"if ((Test-Path -LiteralPath '{flag}') -and (@($args) -contains '{sub}')) {{ "
            f"[IO.File]::AppendAllText('{log}', '{sub}' + [Environment]::NewLine); $global:LASTEXITCODE = 128; 'fatal: seam failure'; return }}; "
            "& $global:realGit @args }; ")


def _owed_checkout_fixture(repo, name):
    """A half-removed, stranded tree (admin entry, `.git` link and tracked files gone) with its rename record. Returns (tmp, main, root, wt, record dir)."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / name)
    _strand_registered(main, wt)
    recs = _pending_records(main)
    assert len(recs) == 1, recs
    shutil.rmtree(_admin_dir(main, wt))
    for f in (".git", "a.txt", ".gitignore"):
        (_retiring_of(wt) / f).unlink()
    return tmp, main, root, wt, Path(main) / ".git" / "mlv-retire-pending" / recs[0]


def _sweep_with(main, root, tmp, prelude):
    return json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", _sweep_script(main, root, tmp, prelude)],
                                     check=True, capture_output=True, text=True).stdout)


def _record_of(rec_dir):
    return json.loads((rec_dir / "record.json").read_text(encoding="utf-8-sig"))


@pytest.mark.parametrize("failing,files_back,note", [("status", True, "status after the re-checkout failed"),
                                                     ("checkout-index", False, "checkout-index after the re-registration failed")])
def test_a_git_failure_after_the_re_registration_keeps_the_owed_checkout_and_the_retry_settles_it(repo, failing, files_back, note):
    """Dot R18 advisory 4e08bf9a on PR #632 (RETIRE-RESTORE-CHECKOUT-STATUS-FAILURE-FIXTURES-1, card A). The admin entry and link are
    re-created and the repair works, then `git checkout-index -a` (or the `git status` after it) fails. The call is restored-unrepaired,
    the record keeps checkoutOwed and survives, and a retry with git working re-checks the tree out and drops the record. COVERAGE: the
    branch already behaves this way on c1e0da63; no committed fixture forced it."""
    tmp, main, root, wt, rec_dir = _owed_checkout_fixture(repo, f"wt-git-{failing}")
    log, flag = tmp / "git-fail.log", tmp / "git-fail.flag"
    flag.write_text("fail", encoding="utf-8")
    s = _sweep_with(main, root, tmp, _git_failure_shim(log, flag, failing))
    rows = s["strandedRestore"]
    assert [r["state"] for r in rows] == ["restored-unrepaired"], s
    assert note in rows[0]["note"], rows
    assert "seam failure" in rows[0]["note"] and "re-checks it out" in rows[0]["note"], rows
    assert log.read_text(encoding="utf-8").split() == [failing], "the shim must have forced exactly the one failure"
    assert rec_dir.is_dir() and _record_of(rec_dir).get("checkoutOwed") is True, "the record and its debt survive the failure"
    assert (wt / "a.txt").exists() is files_back, "checkout-index ran before the failing status, or never ran"
    flag.unlink()
    s = _sweep_with(main, root, tmp, _git_failure_shim(log, flag, failing))
    rows = s["strandedRestore"]
    assert [r["state"] for r in rows] == ["restored"] and "status clean" in rows[0]["note"], rows
    assert (wt / "a.txt").exists() and (wt / ".gitignore").exists(), "every tracked file is back"
    _assert_back_at_its_own_path(main, wt)


def test_an_owed_checkout_that_keeps_failing_gives_up_visibly_after_the_cap_and_never_re_runs_git(repo):
    """RETIRE-RESTORE-OWED-CHECKOUT-ATTEMPT-CAP-1 (card B; fable r1 hardening on PR #632). An owed re-checkout whose git call keeps failing
    used to stay restored-unrepaired for ever, running both git calls on every later call or sweep. Each failing run now counts in
    record.json (checkoutAttempts); the run that reaches the cap reports restored-checkout-gave-up, the record stays on disk as a visible
    terminal state, and later sweeps report the same state WITHOUT running git again, even once git works."""
    cap = 3
    tmp, main, root, wt, rec_dir = _owed_checkout_fixture(repo, "wt-attempt-cap")
    log, flag = tmp / "git-fail.log", tmp / "git-fail.flag"
    flag.write_text("fail", encoding="utf-8")
    shim = _git_failure_shim(log, flag, "checkout-index", cap=cap)
    states = [_sweep_with(main, root, tmp, shim)["strandedRestore"][0]["state"] for _ in range(cap)]
    assert states == ["restored-unrepaired"] * (cap - 1) + ["restored-checkout-gave-up"], states
    assert _record_of(rec_dir).get("checkoutAttempts") == cap and _record_of(rec_dir).get("checkoutOwed") is True
    assert len(log.read_text(encoding="utf-8").split()) == cap
    for working in (False, True):   # still failing, then git works again: terminal either way
        if working:
            flag.unlink()
        s = _sweep_with(main, root, tmp, shim)
        row = s["strandedRestore"][0]
        assert row["state"] == "restored-checkout-gave-up" and "checkout-index -a" in row["note"] and str(rec_dir) in row["note"], s
        assert rec_dir.is_dir(), "a terminal record is kept on disk, never silently dropped"
    assert len(log.read_text(encoding="utf-8").split()) == cap, "no git call after the cap"
    assert not (wt / "a.txt").exists(), "the tree is left as it is for the owner; nothing re-ran"


RECORD_WRITE_FAILS = ("function Write-RetirePendingRecordFile { param([string]$Dir, [object]$Record) "
                      "throw 'seam: record.json is held open by a reader' }; ")


def _seed_record(rec_dir, **fields):
    rec = _record_of(rec_dir)
    rec.update(fields)
    (rec_dir / "record.json").write_text(json.dumps(rec), encoding="utf-8")


def test_a_record_write_that_fails_never_yields_a_terminal_claim_that_the_next_call_contradicts(repo):
    """RETIRE-RESTORE-CHECKOUT-STATUS-FAILURE-FIXTURES-1 r2 (sol blocker on PR #645; catalogue row R-03: never claim terminal suppression while
    later calls still execute git). checkoutOwed with 4 of 5 attempts recorded, git checkout-index failing, and record.json cannot be
    rewritten (a reader holds it). The count used to be saved AFTER git ran: the failed save was only a note, the call still returned
    restored-checkout-gave-up ('git is not run again'), and the next call ran git again from the unchanged count of 4. The count is now saved
    BEFORE git runs; when that save fails git is not run at all and the state is the non-terminal restored-unrepaired, naming the save failure."""
    cap = 5
    tmp, main, root, wt, rec_dir = _owed_checkout_fixture(repo, "wt-attempt-save-fails")
    _seed_record(rec_dir, checkoutOwed=True, checkoutAttempts=cap - 1)
    log, flag = tmp / "git-fail.log", tmp / "git-fail.flag"
    flag.write_text("fail", encoding="utf-8")
    shim = _git_failure_shim(log, flag, "checkout-index", cap=cap) + RECORD_WRITE_FAILS
    rows = [_sweep_with(main, root, tmp, shim)["strandedRestore"][0] for _ in range(2)]
    ran = len(log.read_text(encoding="utf-8").split()) if log.exists() else 0
    assert not any(r["state"] == "restored-checkout-gave-up" for r in rows) or ran == 0, \
        f"a terminal claim was made while git ran {ran} time(s): {rows}"
    assert [r["state"] for r in rows] == ["restored-unrepaired"] * 2, rows
    assert all("attempt count could not be saved" in r["note"] and "git was not run" in r["note"] for r in rows), rows
    assert ran == 0, "an attempt that cannot be recorded is not made"
    assert _record_of(rec_dir).get("checkoutAttempts") == cap - 1 and rec_dir.is_dir(), "the record is untouched and kept"


def test_a_record_that_never_saves_cannot_run_git_for_ever_nor_lose_the_cap(repo):
    """Opus hardening RETIRE-RESTORE-ATTEMPT-COUNT-PERSIST-FAIL-1 on PR #645: if the record cannot be written on any run, the count never
    advanced past 1 and the cap never fired, so git re-ran on every call. Now git does not run while the count cannot be saved (bounded by
    zero, not by the cap), every call says so, and once the record can be written again the cap counts from where it was and still fires."""
    cap = 3
    tmp, main, root, wt, rec_dir = _owed_checkout_fixture(repo, "wt-attempt-never-saves")
    _seed_record(rec_dir, checkoutOwed=True)
    log, flag = tmp / "git-fail.log", tmp / "git-fail.flag"
    flag.write_text("fail", encoding="utf-8")
    shim = _git_failure_shim(log, flag, "checkout-index", cap=cap)
    states = []
    for _ in range(cap + 2):
        row = _sweep_with(main, root, tmp, shim + RECORD_WRITE_FAILS)["strandedRestore"][0]
        states.append(row["state"])
        assert "attempt count could not be saved" in row["note"], row
    assert states == ["restored-unrepaired"] * (cap + 2), states
    assert not log.exists() and "checkoutAttempts" not in _record_of(rec_dir), "no git call, nothing counted"
    states = [_sweep_with(main, root, tmp, shim)["strandedRestore"][0]["state"] for _ in range(cap)]   # the record can be written again
    assert states == ["restored-unrepaired"] * (cap - 1) + ["restored-checkout-gave-up"], states
    assert len(log.read_text(encoding="utf-8").split()) == cap and _record_of(rec_dir).get("checkoutAttempts") == cap


def test_the_owed_checkout_attempt_cap_is_a_named_script_constant_of_five():
    # Exact: a substring match also accepted 50 or 500.
    assert re.search(r"(?m)^\$script:RestoreCheckoutMaxAttempts = 5\s*(#.*)?$", HELPER.read_text(encoding="utf-8"))


def test_the_helper_requires_powershell_7():
    """RETIRE-RESTORE-PWSH7-REQUIRES-PIN-1 (card C). Write-RetirePendingRecordFile calls [IO.File]::Move($src, $dst, $true), a .NET Core-only
    overload; every caller (Invoke-Lane, Invoke-Workstream, Remove-LaneBulkScratch) starts under pwsh 7, so the file says so instead of
    failing inside a restore under Windows PowerShell 5.1."""
    text = HELPER.read_text(encoding="utf-8")
    assert any(l.strip() == "#Requires -Version 7" for l in text.splitlines()), "missing '#Requires -Version 7'"
    assert text.isascii()


@pytest.mark.skipif(not shutil.which("powershell"), reason="requires Windows PowerShell 5.1")
def test_dot_sourcing_the_helper_under_windows_powershell_5_1_is_refused_up_front():
    r = subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-Command", f". '{HELPER}'"], capture_output=True, text=True)
    assert r.returncode != 0 and "requires" in (r.stderr + r.stdout).lower(), (r.returncode, r.stdout, r.stderr)


def _restore_with_snapshot(rec_dir, snapshot):
    """Restore-LaneWorktreeRename as a sweep calls it: with the record it read BEFORE claiming (here, the JSON text in `snapshot`)."""
    script = ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
              f". '{HELPER}'; $old = Get-Content -LiteralPath '{snapshot}' -Raw | ConvertFrom-Json; "
              f"Restore-LaneWorktreeRename -Record $old -RecordDir '{rec_dir}' | ConvertTo-Json -Depth 5")
    return json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True, text=True).stdout)


def _half_removed_with_a_snapshot(tmp, main, wt):
    """A stranded, half-removed tree (as in the test above), plus a copy of its record.json as a sweep B reads it before any restore ran:
    no checkoutOwed in it. Returns (record dir, snapshot path)."""
    _strand_registered(main, wt)
    recs = _pending_records(main)
    assert len(recs) == 1, recs
    rec_dir = Path(main) / ".git" / "mlv-retire-pending" / recs[0]
    shutil.rmtree(_admin_dir(main, wt))
    for f in (".git", "a.txt", ".gitignore"):
        (_retiring_of(wt) / f).unlink()
    snapshot = tmp / "b-snapshot.json"
    snapshot.write_text((rec_dir / "record.json").read_text(encoding="utf-8-sig"), encoding="utf-8")
    assert "checkoutOwed" not in json.loads(snapshot.read_text(encoding="utf-8")), "B's snapshot predates the debt"
    return rec_dir, snapshot


def test_a_restore_holding_a_stale_snapshot_still_performs_the_checkout_owed_under_its_claim(repo):
    """NEW CATALOGUE ROW (sol r1 blocker on PR #632): stale record snapshot before a debt update -> reload under the claim and perform the
    owed checkout. Sweep B reads the record (no checkoutOwed). Restorer A then claims it, persists checkoutOwed, re-creates the admin entry
    and the link, fails its repair and lets go. B claims with its OLD snapshot: admin and link exist and the repair succeeds, so deciding
    from the snapshot skipped checkout-index, reported 'restored' and dropped the record with tracked files missing. Ordered by the test
    itself, no sleeps: snapshot first, A's whole round second, B's restore third."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-stale-snapshot")
    rec_dir, snapshot = _half_removed_with_a_snapshot(tmp, main, wt)
    # A's round: a sweep whose repair fails. It persists the debt, re-creates admin entry and link, and releases its claim on exit.
    seam = "function Repair-LaneWorktreePath { param([string]$Main, [string]$Path) 'git worktree repair: seam failure' }; "
    s = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", _sweep_script(main, root, tmp, seam)],
                                  check=True, capture_output=True, text=True).stdout)
    assert [r["state"] for r in s["strandedRestore"]] == ["restored-unrepaired"], s
    assert json.loads((rec_dir / "record.json").read_text(encoding="utf-8-sig")).get("checkoutOwed") is True
    assert _admin_dir(main, wt).exists() and (wt / ".git").exists() and not (wt / "a.txt").exists(), "A left nothing to re-create"
    # B: claims now, holding the snapshot it read before A's round.
    r = _restore_with_snapshot(rec_dir, snapshot)
    assert r["state"] == "restored" and "status clean" in r["note"], r   # checkout-index and status ran before the drop
    assert (wt / "a.txt").exists() and (wt / ".gitignore").exists(), "the owed checkout ran: every tracked file is back"
    _assert_back_at_its_own_path(main, wt)


def test_a_record_unreadable_under_the_claim_fails_closed_and_is_kept(repo):
    """r2 on PR #632: the record is re-read under the claim. Present but unparseable then is not decided from the caller's snapshot: the
    restore fails closed, touches neither path, and the record stays."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-garbled")
    rec_dir, snapshot = _half_removed_with_a_snapshot(tmp, main, wt)
    (rec_dir / "record.json").write_text("{ not json", encoding="utf-8")
    r = _restore_with_snapshot(rec_dir, snapshot)
    assert r["state"] == "failed" and "cannot be read under the claim" in r["note"], r
    assert _retiring_of(wt).exists() and not wt.exists() and (rec_dir / "record.json").exists(), "nothing touched, the record stays"


def test_a_record_settled_by_its_owner_between_the_listing_and_the_read_is_gone_not_failed(repo):
    """Fable/Grok note on PR #603: the owner settles a record by emptying it (record.json first) while it holds owner.lock, then deletes the
    directory. A sweep that listed the directory before that and reads record.json after it must not report a 'failed' (unreadable) row:
    the record is gone, settled by its owner, as the restore's own claim-time check already says."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-settled-midread")
    _strand_registered(main, wt)
    assert len(_pending_records(main)) == 1
    # Get-Content seam: on the first read of a record.json, the owner's settle happens first (everything but owner.lock deleted).
    seam = ("$script:Settled = $false; function Get-Content { "
            "$hit = @($args | Where-Object { $_ -is [string] -and $_ -like '*\\record.json' }); "
            "if ($hit.Count -and -not $script:Settled) { $script:Settled = $true; "
            "Get-ChildItem -LiteralPath (Split-Path $hit[0] -Parent) -Force | Where-Object { $_.Name -ne 'owner.lock' } | "
            "Remove-Item -Recurse -Force }; Microsoft.PowerShell.Management\\Get-Content @args }; ")
    s = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", _sweep_script(main, root, tmp, seam)],
                                  check=True, capture_output=True, text=True).stdout)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "gone" and "settled by its owner" in rows[0]["note"], s
    assert _pending_records(main) == [], "the emptied record is dropped, as the claim-time branch drops it"


def _sweep_publish_race(main, root, tmp, published, hold_lock, sweep_args=""):
    """Sweep with a Get-Content seam: the FIRST read of a record.json finds it absent (the owner has created the directory, as
    New-RetirePendingRecord does, but not yet written record.json), and before the read returns the owner publishes `published` (the record
    text, or None for the original text) and, with `hold_lock`, keeps owner.lock open (FileShare.None) through the rest of the sweep.
    Ordered by the seam, no sleeps. Every claim attempt is counted. Returns (sweep summary, claim attempts)."""
    text = "[IO.File]::ReadAllText($rj)" if published is None else "'" + published.replace("'", "''") + "'"
    lock = ("$script:OwnerLock = [IO.File]::Open((Join-Path (Split-Path $rj -Parent) 'owner.lock'), 'OpenOrCreate', 'ReadWrite', 'None'); "
            if hold_lock else "")
    seam = ("$script:Raced = $false; $script:ClaimCalls = 0; $script:OwnerLock = $null; $origEnter = ${function:Enter-RetirePendingRecord}; "
            "function Enter-RetirePendingRecord { $script:ClaimCalls++; & $origEnter @args }; "
            "function Get-Content { $hit = @($args | Where-Object { $_ -is [string] -and $_ -like '*\\record.json' }); "
            "if ($hit.Count -and -not $script:Raced) { $script:Raced = $true; $rj = $hit[0]; "
            f"$text = {text}; [IO.File]::Delete($rj); "
            f"try {{ Microsoft.PowerShell.Management\\Get-Content @args }} finally {{ {lock}[IO.File]::WriteAllText($rj, $text) }} }} "
            "else { Microsoft.PowerShell.Management\\Get-Content @args } }; ")
    script = ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
              f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}{seam}"
              f"$s = Invoke-SweepMergedLaneWorktrees -RepoRoot '{main}' -Root '{root}' -MergeTarget 'origin/master' -MinIdleHours 24 "
              f"-QuarantineRoot '{tmp / 'q'}' {sweep_args}; if ($script:OwnerLock) {{ $script:OwnerLock.Dispose() }}; "
              "[pscustomobject]@{ sweep = $s; claimCalls = $script:ClaimCalls } | ConvertTo-Json -Depth 6")
    out = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                                    check=True, capture_output=True, text=True).stdout)
    return out["sweep"], out["claimCalls"]


def test_a_record_published_after_the_sweep_read_it_while_its_owner_holds_the_lock_is_owned_not_failed(repo):
    """NEW CATALOGUE ROW (sol r2 blocker on PR #632): record absent during enumeration, published before sweep classification, owner.lock
    held -> owned, never failed. The sweep checked record.json's existence AFTER its failed read and called a record present then corrupt,
    without ever trying the claim. A record missing at the read goes through the claim: held by a live owner it is 'owned' and untouched."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-published-owned")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    s, claims = _sweep_publish_race(main, root, tmp, published=None, hold_lock=True)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "owned", s
    assert claims >= 1, f"the sweep must try the claim before classifying the record (claimCalls={claims})"
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "nothing renamed, the record is kept"
    assert (Path(main) / ".git" / "mlv-retire-pending" / recs[0] / "record.json").exists()


def test_a_record_published_after_the_sweep_read_it_with_the_lock_free_is_restored_from_the_record_read_under_the_claim(repo):
    """Companion: the same race, but the owner has let go of the lock. The claim is taken and record.json, re-read under it, is a good
    record: the restore runs as normal from it."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-published-free")
    _strand_registered(main, wt)
    s, claims = _sweep_publish_race(main, root, tmp, published=None, hold_lock=False)
    rows = s["strandedRestore"]
    assert [r["state"] for r in rows] == ["restored"] and claims >= 1, s
    _assert_back_at_its_own_path(main, wt)


def test_a_record_published_garbled_after_the_sweep_read_it_with_the_lock_free_fails_closed_and_is_kept(repo):
    """Companion: the record published in the race is present but unparseable, and the lock is free. That is decided under the claim, as
    any record unreadable there is: failed, nothing touched, the record kept."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-published-garbled")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    s, claims = _sweep_publish_race(main, root, tmp, published="{ not json", hold_lock=False)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "failed" and "cannot be read under the claim" in rows[0]["note"] and claims >= 1, s
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "nothing touched, the record stays"


def test_a_record_published_after_the_sweep_read_it_naming_an_excluded_path_is_never_restored(repo):
    """Companion: the sweep's -Root / -Exclude filter could only run on a record it had read. A record first read under the claim is
    filtered there, before any rename: one naming an excluded path gets no row, and neither path nor the record is touched."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-published-excluded")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    s, _ = _sweep_publish_race(main, root, tmp, published=None, hold_lock=False, sweep_args=f"-Exclude '{wt}'")
    assert s["strandedRestore"] == [], s
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "nothing touched, the record stays"


# r4 on PR #632 (sol r3 blocker): the record directory exists when the restore checks it, and is gone before its claim opens owner.lock.
# The Enter-RetirePendingRecord seam runs `claim` (PowerShell; $dir is the record directory) in place of the claim's first step, so the
# disappearance lands exactly between the existence check and the open. Ordered by the seam, no sleeps.
OWNER_SETTLES_BEFORE_THE_CLAIM = "[IO.Directory]::Delete($dir, $true); & $origEnter $dir"


def _sweep_claim_seam(main, root, tmp, claim):
    """Sweep in which the enumeration read of every record.json fails (Record = $null, as for a record not yet published) and each claim
    attempt is counted and runs `claim`. Returns (sweep summary, claim attempts)."""
    seam = ("$script:ClaimCalls = 0; $origEnter = ${function:Enter-RetirePendingRecord}; "
            f"function Enter-RetirePendingRecord {{ param([string]$dir) $script:ClaimCalls++; {claim} }}; "
            "$script:Hid = $false; function Get-Content { $hit = @($args | Where-Object { $_ -is [string] -and $_ -like '*\\record.json' }); "
            "if ($hit.Count -and -not $script:Hid) { $script:Hid = $true; throw [IO.FileNotFoundException]::new('seam: record.json not yet published') }; "
            "Microsoft.PowerShell.Management\\Get-Content @args }; ")
    script = ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
              f". '{HELPER}'; {HOST_SNAPSHOT_FILTER}{seam}"
              f"$s = Invoke-SweepMergedLaneWorktrees -RepoRoot '{main}' -Root '{root}' -MergeTarget 'origin/master' -MinIdleHours 24 "
              f"-QuarantineRoot '{tmp / 'q'}'; [pscustomobject]@{{ sweep = $s; claimCalls = $script:ClaimCalls }} | ConvertTo-Json -Depth 6")
    out = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                                    check=True, capture_output=True, text=True).stdout)
    return out["sweep"], out["claimCalls"]


def test_a_record_directory_its_owner_removes_between_the_existence_check_and_the_claim_is_gone_not_failed(repo):
    """NEW CATALOGUE ROW (sol r3 blocker on PR #632): missing record.json during enumeration; the owner deletes the emptied record
    directory after the restore's Test-Path succeeds but before owner.lock opens -> gone, never failed. Opening owner.lock in a directory
    that no longer exists throws DirectoryNotFoundException; that, with the directory confirmed absent afterwards, is the owner's
    settlement, not a failure. Nothing is renamed."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-dir-vanished")
    _strand_registered(main, wt)
    assert len(_pending_records(main)) == 1
    s, claims = _sweep_claim_seam(main, root, tmp, OWNER_SETTLES_BEFORE_THE_CLAIM)
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "gone" and "settled by its owner" in rows[0]["note"], s
    assert claims == 1 and s["error"] is None, (claims, s)
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == [], "nothing renamed; nothing left to keep"


def test_a_claim_refused_for_permission_while_the_record_directory_exists_stays_failed_and_the_record_is_kept(repo):
    """Companion: only the owner's settlement is 'gone'. A claim that fails for any other reason, here access denied with the record
    directory still present, stays 'failed' with its error text, and the record and both paths are kept."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-claim-denied")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    s, claims = _sweep_claim_seam(main, root, tmp, "throw [UnauthorizedAccessException]::new('seam: access to owner.lock is denied')")
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "failed" and "access to owner.lock is denied" in rows[0]["note"] and claims == 1, s
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "nothing touched, the record stays"


def test_a_claim_reporting_directory_not_found_while_the_record_directory_still_exists_stays_failed(repo):
    """Companion: DirectoryNotFoundException alone is not the settlement. With the record directory still there after the failure the
    exception is not masked: 'failed' with its error text, the record kept."""
    tmp, main = repo
    root = tmp / "lanes"
    wt = _add_wt(main, root / "wt-claim-dnf-present")
    _strand_registered(main, wt)
    recs = _pending_records(main)
    s, claims = _sweep_claim_seam(main, root, tmp, "throw [IO.DirectoryNotFoundException]::new('seam: a part of the path was not found')")
    rows = s["strandedRestore"]
    assert len(rows) == 1 and rows[0]["state"] == "failed" and "a part of the path was not found" in rows[0]["note"] and claims == 1, s
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == recs, "nothing touched, the record stays"


def test_the_next_call_for_a_stranded_worktree_whose_record_directory_vanishes_before_the_claim_reports_gone(repo):
    """The same race on the per-call restore entry (Restore-StrandedRetiring -> Restore-LaneWorktreeRename): its record read fine, then
    the owner settles it before the claim. strandedRestore is 'gone', the call keeps the worktree with that reason, and nothing is renamed."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-call-dir-vanished")
    _strand_registered(main, wt)
    prelude = ("$origEnter = ${function:Enter-RetirePendingRecord}; "
               f"function Enter-RetirePendingRecord {{ param([string]$dir) {OWNER_SETTLES_BEFORE_THE_CLAIM} }}; ")
    d, _ = _gate_real(wt, whatif=False, prelude=prelude)
    assert d["strandedRestore"]["state"] == "gone" and "settled by its owner" in d["strandedRestore"]["note"], d
    assert d["action"] == "kept" and d["reason"].startswith("stranded-retiring: "), d
    assert _retiring_of(wt).exists() and not wt.exists() and _pending_records(main) == [], "nothing renamed; nothing left to keep"


# r2 (Sol r1 B1/B2): a holder that appears AFTER the first decision must stop the retry too. The retry window is where the worktree is
# half-removed and a handle still blocks it, so a process that starts in it is exactly the one the gate was written to refuse.
# Scans are numbered: 1 = the gate, 2 = the last look before removal, 3.. = one fresh scan before each retry attempt that may delete.

def _counted_scans_and_sleeps(release, release_at_sleep):
    """Prelude: count every process scan in $script:SnapN, and create `release` (the file holder lets go) at the Nth back-off."""
    return (ONLY_PYTEST_CHILDREN + "$script:SnapN = 0; $script:SleepN = 0; $script:RemoveRetryBudgetMs = 20000; $script:RemoveRetryDelayMs = 50; "
            "$origSnapCount = ${function:Get-LaneProcessSnapshot}; "
            "function Get-LaneProcessSnapshot { $script:SnapN++; & $origSnapCount }; "
            "function Start-Sleep { param([int]$Milliseconds = 0, [double]$Seconds = 0) $script:SleepN++; "
            f"if ($script:SleepN -ge {release_at_sleep} -and -not (Test-Path -LiteralPath '{release}')) {{ [IO.File]::WriteAllText('{release}', 'go') }}; "
            "Microsoft.PowerShell.Utility\\Start-Sleep -Milliseconds ([Math]::Max($Milliseconds, 400)) }; ")


def test_an_unreadable_cwd_process_that_appears_during_the_retry_no_longer_stops_it(repo):
    """R-03, r7: sharing-violation half-removal, then a process whose cwd cannot be read shows up. It used to be refused as cwd-unknown
    at every retry scan; now the retry names it as evidence and carries on (a process that really sat in the tree would be the very
    handle that fails the removal, and the rename already proved none did), so the worktree is retired once the handle drops."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-late-unknown")
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    late = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=tmp)   # not the holder of anything; its cwd just cannot be read
    prelude = (_counted_scans_and_sleeps(release, 1) + _handle_appears_after_the_rename(trigger, ready)
               + "$origCwdLate = ${function:Get-ProcessCurrentDirectory}; "
               f"function Get-ProcessCurrentDirectory {{ param([int]$ProcessId) if ($script:SnapN -ge 3 -and $ProcessId -eq {late.pid}) {{ return $null }}; & $origCwdLate -ProcessId $ProcessId }}; ")
    try:
        d, err = _gate_real(wt, whatif=False, prelude=prelude)
    finally:
        _stop(late)
        _finish_holder(p, release)
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert any(u.startswith(f"{late.pid} ") for u in d["cwdUnknown"]), d   # named at the retry scan that saw it
    assert f"pid={late.pid}" not in err, err                               # but not a REFUSED line
    assert d["removeAttempts"] >= 2 and not wt.exists() and not _retiring_of(wt).exists(), d


def test_a_cwd_holder_that_appears_after_the_first_leftover_deletion_keeps_the_worktree(repo):
    """R-04: the first leftover deletion hits the sharing violation, then a process whose cwd is the worktree appears. The retry
    that follows must look again (it used to look once), and keep the worktree before deleting anything more."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-late-cwd")
    ready, release, trigger = tmp / "hold.ready", tmp / "hold.release", tmp / "hold.trigger"
    p = _spawn_file_handle_holder(_retiring_of(wt) / "a.txt", ready, release, tmp, trigger=trigger)
    # Scan 3 (attempt 2) is clear and its deletion fails on the file handle; the handle drops at the 2nd back-off; scan 4 (attempt 3) sees the cwd holder.
    # r7: the removal runs on the RENAMED root, so a process already in the tree has its cwd under <wt>.retiring.
    prelude = (_counted_scans_and_sleeps(release, 2) + _handle_appears_after_the_rename(trigger, ready)
               + "$origSnapLate = ${function:Get-LaneProcessSnapshot}; "
               "function Get-LaneProcessSnapshot { $s = & $origSnapLate; if ($script:SnapN -ge 4) { "
               f"$row = [pscustomobject]@{{ ProcessId = 999990; Name = 'late-holder.exe'; CommandLine = 'late-holder.exe'; SessionId = 1; CurrentDirectory = '{wt}.retiring' }}; "
               "return [pscustomobject]@{ Procs = @($s.Procs) + @($row); SelfPids = $s.SelfPids; CapturedUtc = $s.CapturedUtc } }; $s }; ")
    try:
        d, err = _gate_real(wt, whatif=False, prelude=prelude)
    finally:
        _finish_holder(p, release)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: 999990 late-holder.exe"), d
    assert "999990 late-holder.exe cwd" in d["holders"], d
    assert d["removeAttempts"] == 3, d   # stopped at the third attempt, before its deletion


def test_the_removal_retry_is_keyed_to_access_denied_and_sharing_violation_only():
    """Source pin: the loop is entered only for a failure message matching RemoveRetryablePattern, and that pattern names the two
    Windows conditions (git renders them as "Permission denied" / "Invalid argument"; native tools as "Access is denied" / "being used
    by another process"). Anything else is the first and only attempt."""
    src = HELPER.read_text(encoding="utf-8")
    assert "-notmatch $script:RemoveRetryablePattern" in src
    pat = src[src.index("$script:RemoveRetryablePattern"):].splitlines()[0]
    for needle in ("Permission denied", "Access is denied", "being used by another process", "Invalid argument"):
        assert needle in pat, pat


def test_sweep_keeps_a_worktree_held_only_by_a_process_cwd(repo):
    tmp, main = repo
    root = tmp / "lanes"
    held = _add_wt(main, root / "lane-held")
    free = _add_wt(main, root / "lane-free")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=held)
    try:
        s = _sweep(main, Root=str(root), MergeTarget="origin/master", MinIdleHours=0, QuarantineRoot=str(tmp / "q"))
    finally:
        _stop(p)
    assert _swept_paths(s) == [_norm(free)] and s["kept"] == {"live-process": 1}, s
    assert held.exists() and not free.exists()


# --- r2 (WORKTREE-REMOVED-UNDER-LIVE-CHAIN-1 hub ruling B + C): the cwd probe is LOUD, and relative script paths are followed ---
# Ruling B: when the cwd probe cannot run, or cannot read a candidate holder, the answer is `kept` naming
# cwd-probe-unavailable / cwd-unknown, never a plain would-retire with holders=[].
# Ruling C: a script path on a holder's command line is resolved against THAT holder's cwd when relative.

FORCE_PROBE_UNAVAILABLE = "function Test-CwdProbeAvailable { $false };"
CWD_UNREADABLE_FOR_EVERY_PID = "function Get-ProcessCurrentDirectory { param([int]$ProcessId) return $null };"


def _cwd_unreadable_for(*pids):
    """Prelude: Get-ProcessCurrentDirectory returns $null for these pids and reads the real cwd for every other."""
    ids = ",".join(str(p) for p in pids)
    return ("$orig = ${function:Get-ProcessCurrentDirectory}; "
            "function Get-ProcessCurrentDirectory { param([int]$ProcessId) "
            f"if (@({ids}) -contains $ProcessId) {{ return $null }}; & $orig -ProcessId $ProcessId }};")


def _fake_row(pid_expr, cmd="shell.exe", session=1, name="shell.exe", self_pids="@(1)", created=None, cwd=None):
    """One live process row. cmd=None -> CommandLine is $null; created = a PowerShell [datetime] expression for
    CreationDate (omitted = the row has no CreationDate at all); cwd = a CurrentDirectory the gate takes as read."""
    cmd_expr = "$null" if cmd is None else "'" + cmd + "'"
    extra = (f"; CreationDate = {created}" if created else "") + (f"; CurrentDirectory = '{cwd}'" if cwd else "")
    return ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = " + pid_expr + "; Name = '" + name + "'; "
            "CommandLine = " + cmd_expr + "; SessionId = " + str(session) + extra + " }); SelfPids = " + self_pids + " }")


def _wt_created(wt, minutes):
    """PowerShell expression: the worktree root's CreationTimeUtc shifted by `minutes` (negative = before the worktree existed)."""
    return f"(Get-Item -LiteralPath '{wt}').CreationTimeUtc.AddMinutes({minutes})"


def _assert_deferred_to_rename(d):
    """r7 (LANE-RETIRE-CWD-UNKNOWN-RENAME-PROBE-1): a process whose cwd cannot be read is evidence (listed in cwdUnknown), no longer a refusal.
    The gate says would-retire and the rename of the root settles it for real (-WhatIf never renames)."""
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["renameProbe"].startswith("not-run: WhatIf"), d


def test_healthy_cwd_probe_is_reported_ok_and_backs_the_would_retire(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-probe-ok")
    d, _ = _gate_real(wt)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["cwdProbe"] == "ok" and d["cwdUnknown"] == [], d


def test_probe_unavailable_with_a_cwd_only_holder_is_refused_not_would_retire(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-probe-off")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=wt)   # its ONLY link to the worktree is its cwd
    try:
        d, err = _gate_real(wt, prelude=FORCE_PROBE_UNAVAILABLE)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-probe-unavailable"), d
    assert d["cwdProbe"] == "unavailable" and any(u.startswith(f"{p.pid} ") for u in d["cwdUnknown"]), d
    assert "REFUSED" in err and "via=cwd-probe-unavailable" in err, err
    assert wt.exists()


def test_probe_unavailable_refusal_holds_on_a_real_removal_too(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-probe-off-real")
    d, _ = _gate_real(wt, whatif=False, prelude=FORCE_PROBE_UNAVAILABLE)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-probe-unavailable"), d
    assert wt.exists()


def _spawn_proven_alive(tmp, name, cwd, attempts=3):
    """A sleeping holder that has PROVEN it is running: it writes a ready file as its first act and the caller waits for it. A bare
    Popen only proves CreateProcess returned; a pwsh that dies at startup under host load (no stderr, DEVNULL) leaves a pid the sweep
    rightly never lists, which reads as a gate miss (CI-FLAKE-RETIRE-CWD-UNKNOWN-PID-1, merge_group 40157682070). Only the holder's
    STARTUP is retried, before any product code runs. The 600 s sleep outlives any scan shorter than 600 s, but _gate_real passes no
    subprocess timeout, so that is a bound on the fixture and not a guarantee; a longer scan is caught by the caller's post-scan
    p.poll() assertion, which fails loudly as a fixture error and can never turn into a false pass."""
    for attempt in range(attempts):
        ready = Path(tmp) / f"{name}-{attempt}.ready"
        p = _spawn(["-Command", f"[IO.File]::WriteAllText('{ready}', 'up'); Start-Sleep -Seconds 600"], cwd=cwd)
        deadline = time.monotonic() + 60
        while p.poll() is None and not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if p.poll() is None and ready.exists():
            return p
        _stop(p)
    raise AssertionError(f"fixture holder {name} did not reach its ready file in {attempts} attempts")


def test_a_pid_whose_cwd_read_fails_is_listed_and_left_to_the_rename_probe(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-cwd-unknown")
    p = _spawn_proven_alive(tmp, "cwd-unknown-holder", cwd=tmp)      # does not hold the worktree, but the gate cannot tell
    # ONLY_PYTEST_CHILDREN: this case is about its own holder; an unrelated host pwsh/cmd with an unreadable cwd and a relative script
    # (relative-script-unresolved, never exempt) would otherwise turn the would-retire into a cwd-unknown refusal (reproduced 2 in ~100).
    try:
        d, err = _gate_real(wt, prelude=ONLY_PYTEST_CHILDREN + _cwd_unreadable_for(p.pid))
        control, _ = _gate_real(wt, prelude=ONLY_PYTEST_CHILDREN)    # same process, cwd readable: not listed at all
        # The holder was alive for both scans (it is killed only below), so a missing pid is the gate's, never a vanished fixture.
        assert p.poll() is None, f"fixture holder {p.pid} exited before the scans finished"
    finally:
        _stop(p)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), json.dumps(d)   # r7: no longer a refusal (the rename settles it)
    assert d["cwdProbe"] == "ok" and any(u.startswith(f"{p.pid} ") for u in d["cwdUnknown"]), json.dumps(d)
    assert f"pid={p.pid}" not in err, err                            # and no REFUSED line for it
    assert not any(u.startswith(f"{p.pid} ") for u in control["cwdUnknown"]), control
    assert wt.exists()


def test_a_named_holder_is_reported_as_live_process_when_the_probe_works(repo):
    """Same holder as the probe-unavailable case: with a working probe it is NAMED (live-process), not just doubted."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-named")
    holder = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=wt)
    try:
        d, _ = _gate_real(wt)
    finally:
        _stop(holder)
    assert d["reason"].startswith(f"live-process: {holder.pid} ") and d["cwdProbe"] == "ok", d


def test_snapshot_pid_that_has_exited_is_not_a_candidate_holder(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-vanished")
    d = _gate_with_snapshot(wt, _fake_row("999993"))               # no such pid: nothing left to hold the path
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


def test_snapshot_user_session_process_with_unreadable_cwd_is_listed_and_left_to_the_rename(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-sess1")
    d = _gate_with_snapshot(wt, _fake_row("$PID"), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    _assert_deferred_to_rename(d)


def test_snapshot_session0_process_with_unreadable_cwd_is_no_longer_exempt_by_session_id(repo):
    """The one sanctioned expectation change (r3, hub ruling B): this was would-retire on 2d261a18; a session-0 row with an
    unreadable cwd and no readable creation time is now cwd-unknown."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-sess0")
    d = _gate_with_snapshot(wt, _fake_row("$PID", session=0), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    _assert_deferred_to_rename(d)


# --- r3 (WORKTREE-REMOVED-UNDER-LIVE-CHAIN-1 hub ruling HUB-TICK 20261009T1004Z, sol blockers on 2d261a18) ---
# A: a null command line is a LIVE process.  B: a session id is not evidence; creation time before the worktree is.

@pytest.mark.parametrize("cmd", ["pwsh.exe -File C:\\\\elsewhere\\\\quiet.ps1", "svchost.exe -k netsvcs"], ids=["absolute-script", "native-command"])
def test_snapshot_session0_process_with_unreadable_cwd_created_after_the_worktree_is_listed_not_exempt(repo, cmd):
    """Sol r2 row 2. REPLACES the r2 expectation (session 0 + unreadable cwd = would-retire): a session id proves nothing."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-sess0")
    row = _fake_row("$PID", cmd=cmd, session=0, name=cmd.split()[0], created=_wt_created(wt, 5))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    _assert_deferred_to_rename(d)
    assert any("cwd-unreadable" in u for u in d["cwdUnknown"]) and d["cwdExempt"] == 0, d


def test_snapshot_process_with_null_command_line_and_unreadable_cwd_is_listed_and_left_to_the_rename(repo):
    """Sol r2 row 1: CommandLine = $null used to be filtered out of the snapshot before any cwd check."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-nullcmd")
    row = _fake_row("$PID", cmd=None, created=_wt_created(wt, 5))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    _assert_deferred_to_rename(d)
    assert any(" cwd-unreadable created=" in u for u in d["cwdUnknown"]), d


def test_snapshot_process_with_null_command_line_and_cwd_inside_the_worktree_is_a_cwd_holder(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-nullcmd-cwd")
    d = _gate_with_snapshot(wt, _fake_row("$PID", cmd=None, cwd=str(wt / "sub")))
    assert d["action"] == "kept" and d["reason"].startswith("live-process: ") and "[cwd]" in d["reason"], d


@pytest.mark.parametrize("session", [0, 1])
def test_snapshot_unreadable_cwd_created_before_the_worktree_is_exempt(repo, session):
    """Hub ruling B: the one exemption. A process older than the directory cannot have had it as its startup cwd."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-older")
    row = _fake_row("$PID", cmd=None, session=session, created=_wt_created(wt, -60))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["cwdExempt"] == 1 and d["worktreeCreatedUtc"], d


def test_snapshot_exempt_process_whose_command_line_names_the_worktree_is_still_refused(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-older-named")
    row = _fake_row("$PID", cmd=f"svc.exe --root {wt}", session=0, name="svc.exe", created=_wt_created(wt, -60))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: ") and "[cmdline]" in d["reason"], d


@pytest.mark.parametrize("created", [None, "$null", "'not a date'"], ids=["no-property", "null", "not-a-datetime"])
def test_snapshot_unreadable_cwd_with_unreadable_creation_date_is_listed_not_exempt(repo, created):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-nodate")
    d = _gate_with_snapshot(wt, _fake_row("$PID", created=created), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    _assert_deferred_to_rename(d)
    assert any("created=unreadable" in u for u in d["cwdUnknown"]), d


def test_snapshot_unreadable_cwd_created_exactly_when_the_worktree_was_is_listed_not_exempt(repo):
    """Strictly earlier is required: equal is not earlier."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-same-instant")
    d = _gate_with_snapshot(wt, _fake_row("$PID", created=_wt_created(wt, 0)), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    _assert_deferred_to_rename(d)


def test_creation_time_does_not_exempt_a_relative_script_whose_cwd_is_unknown(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rel-older")
    row = _fake_row("$PID", cmd="pwsh.exe -File tools\\quiet.ps1", name="pwsh.exe", created=_wt_created(wt, -60))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown"), d
    assert any("relative-script-unresolved" in u for u in d["cwdUnknown"]) and d["cwdExempt"] == 0, d


def test_creation_time_does_not_exempt_when_the_cwd_probe_is_unavailable(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-probe-off-older")
    d = _gate_with_snapshot(wt, _fake_row("$PID", created=_wt_created(wt, -60)), prelude=FORCE_PROBE_UNAVAILABLE)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-probe-unavailable"), d


def test_kernel_pseudo_processes_are_not_candidate_holders(repo):
    """Pids 0 (System Idle) and 4 (System) have no user-mode cwd and no creation date worth reading."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-kernel")
    for pid in ("0", "4"):
        d = _gate_with_snapshot(wt, _fake_row(pid, cmd=None), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
        assert (d["action"], d["reason"]) == ("would-retire", "ok"), (pid, d)


def test_real_process_snapshot_keeps_processes_whose_command_line_is_null():
    """The Win32_Process row for System (pid 4) has a null CommandLine; the snapshot used to drop every such row."""
    script = (f"$ErrorActionPreference='Stop'; . '{HELPER}'; $s = Get-LaneProcessSnapshot; "
              "@($s.Procs | Where-Object { -not $_.CommandLine }).Count")
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                         check=True, capture_output=True, text=True).stdout
    assert int(out.strip()) > 0


def test_snapshot_self_chain_with_unreadable_cwd_is_not_cwd_unknown(repo):
    """r1 self-pid exclusion still holds under the loud probe."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-self-cwd")
    d = _gate_with_snapshot(wt, _fake_row("$PID", self_pids="@($PID)"), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


def test_snapshot_cwd_that_is_a_sibling_prefix_is_still_not_a_hit_under_the_loud_probe(repo):
    """r1 sibling-prefix exclusion still holds: <wt>2 is a known cwd outside the worktree, so neither a hit nor unknown."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-sib")
    row = ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = $PID; Name = 'shell.exe'; CommandLine = 'shell.exe'; "
           f"SessionId = 1; CurrentDirectory = '{wt}2' }}); SelfPids = @(1) }}")
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


def test_session0_process_running_a_relative_script_with_unknown_cwd_is_cwd_unknown(repo):
    """Ruling C: a relative script path cannot be followed without the holder's cwd, so the holder may name the worktree."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rel-unknown")
    row = _fake_row("$PID", cmd="pwsh.exe -File tools\\quiet.ps1", session=0, name="pwsh.exe")
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown"), d
    assert any("relative-script-unresolved" in u for u in d["cwdUnknown"]), d
    # control (r3: a session id no longer exempts; creation before the worktree does): the same process without a relative
    # script path, created before the worktree existed, is not a candidate
    ctl = _gate_with_snapshot(wt, _fake_row("$PID", cmd="pwsh.exe -Command Start-Sleep", session=0, name="pwsh.exe",
                                            created=_wt_created(wt, -60)), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert (ctl["action"], ctl["reason"]) == ("would-retire", "ok"), ctl


def _relative_chain(tmp, wt_text, dirname):
    """<tmp>/relrun/<dirname>/quiet.ps1 dot-sources arms.ps1; only arms.ps1 names the worktree."""
    base = tmp / "relrun"
    sd = base / dirname
    sd.mkdir(parents=True)
    (sd / "arms.ps1").write_text(f"$script:Wt = '{wt_text}'\n", encoding="utf-8")
    (sd / "quiet.ps1").write_text('. "$PSScriptRoot\\arms.ps1"\nStart-Sleep -Seconds 120\n', encoding="utf-8")
    return base


@pytest.mark.parametrize("dirname", ["tools", "chain tools"], ids=["unquoted-relative", "quoted-relative"])
def test_relative_script_path_is_resolved_against_the_holders_cwd(repo, dirname):
    """`pwsh -File tools\\quiet.ps1` (cwd = run dir). A directory with a space makes the OS command line quote the path."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rel-script")
    base = _relative_chain(tmp, str(wt), dirname)
    p = _spawn(["-File", f"{dirname}\\quiet.ps1"], cwd=base)    # the gate runs from elsewhere, where this relative path does not exist
    try:
        d, err = _gate_real(wt)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d
    assert "script:arms.ps1" in d["reason"] and f"pid={p.pid}" in err, (d, err)
    assert wt.exists()


@pytest.mark.parametrize("dirname", ["tools", "chain tools"], ids=["unquoted-relative", "quoted-relative"])
def test_relative_script_path_that_does_not_name_the_worktree_does_not_block(repo, dirname):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rel-other")
    base = _relative_chain(tmp, str(tmp / "some-other-place"), dirname)
    p = _spawn(["-File", f"{dirname}\\quiet.ps1"], cwd=base)
    try:
        d, _ = _gate_real(wt)
    finally:
        _stop(p)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


# --- r4 (RETIRE-CWD-UNKNOWN-STARVES-SWEEP-1): an unreadable-cwd process whose OWNER the SCM can name as a well-known
# service identity (S-1-5-18/19/20) is not a candidate holder. Everything else stays cwd-unknown, session 0 included.

def _svc_rows(*rows):
    """Prelude: Get-RunningServiceRows returns these Win32_Service-shaped rows. Each row is
    (pid_expr, name, start_name, service_type, path_name)."""
    items = ", ".join(
        "[pscustomobject]@{ ProcessId = " + pid + "; Name = '" + name + "'; StartName = '" + start + "'; ServiceType = '"
        + stype + "'; PathName = '" + path + "' }" for pid, name, start, stype, path in rows)
    return "function Get-RunningServiceRows { @(" + items + ") };"


SVCHOST = "C:\\WINDOWS\\System32\\svchost.exe -k netsvcs -p"


def _svchost_row(wt, **kw):
    """An unreadable-cwd svchost.exe in session 0, created AFTER the worktree (so the creation-time exemption cannot apply)."""
    kw.setdefault("cmd", None)
    return _fake_row("$PID", session=0, name="svchost.exe", created=_wt_created(wt, 5), **kw)


@pytest.mark.parametrize("start,sid", [("LocalSystem", "S-1-5-18"), ("NT AUTHORITY\\LocalService", "S-1-5-19"),
                                       ("NT AUTHORITY\\NetworkService", "S-1-5-20")], ids=["system", "local-service", "network-service"])
def test_service_sid_owned_unreadable_cwd_process_created_after_the_worktree_is_exempt_and_listed(repo, start, sid):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", start, "Share Process", SVCHOST),
                                                       ("$PID", "gpsvc", start, "Share Process", SVCHOST)) + _key_written(wt, -60)
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["cwdUnknown"] == [] and d["cwdOwnerProbe"] == "scm", d
    assert len(d["cwdExemptOwner"]) == 1 and f"exempt=service-sid:{sid} via=scm:BITS,gpsvc" in d["cwdExemptOwner"][0], d


def test_same_user_session0_process_with_no_service_owner_is_still_not_exempt(repo):
    """A lane started by a Scheduled Task runs as THIS user in session 0: no SCM row names it, so its owner is unread."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-lane")
    row = _fake_row("$PID", cmd="pwsh.exe -File C:\\\\elsewhere\\\\lane.ps1", session=0, name="pwsh.exe", created=_wt_created(wt, 5))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("4321", "BITS", "LocalSystem", "Share Process", SVCHOST)))
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [], d


@pytest.mark.parametrize("start,stype", [(".\\obabalola", "Own Process"), ("", "Unknown")], ids=["service-as-this-user", "per-user-service-instance"])
def test_service_running_as_a_user_account_is_still_not_exempt(repo, start, stype):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-user")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "CDPUserSvc_6d6616", start, stype, SVCHOST))
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [], d


def test_owner_unreadable_process_is_still_not_exempt(repo):
    """SearchProtocolHost: session 0, cwd and token unreadable, hosts no service, so the SCM cannot name its owner."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-unread")
    row = _fake_row("$PID", cmd=None, session=0, name="SearchProtocolHost.exe", created=_wt_created(wt, 5))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows())
    _assert_deferred_to_rename(d)


def test_service_owner_query_failure_is_still_not_exempt(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-fail")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + "function Get-RunningServiceRows { throw 'scm unavailable' };"
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdOwnerProbe"] == "scm-failed" and d["cwdExemptOwner"] == [], d


def test_another_user_service_process_is_still_not_exempt(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-other")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "TBSage", "VIRTUAL-TEN\\tbco903149bf4109", "Own Process", SVCHOST))
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)


def test_service_pid_whose_snapshot_image_differs_is_still_not_exempt(repo):
    """Pid reuse: the SCM says pid N hosts svchost.exe, the snapshot row at pid N is pwsh.exe. Not the same process."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-reuse")
    row = _fake_row("$PID", session=0, name="pwsh.exe", created=_wt_created(wt, 5))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST)))
    _assert_deferred_to_rename(d)


def test_service_pid_with_mixed_accounts_is_still_not_exempt(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-mixed")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST),
                                                       ("$PID", "Odd", ".\\obabalola", "Share Process", SVCHOST))
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)


@pytest.mark.parametrize("via", ["cwd", "cmdline"])
def test_service_sid_owned_process_that_holds_the_worktree_is_still_kept(repo, via):
    """The exemption only answers 'unknown cwd'; a readable cwd inside the worktree or a command line naming it is a holder."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-holder")
    kw = {"cwd": str(wt / "sub")} if via == "cwd" else {"cmd": f"svchost.exe --root {wt}"}
    d = _gate_with_snapshot(wt, _svchost_row(wt, **kw),
                            prelude=_svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST)))
    assert d["action"] == "kept" and d["reason"].startswith("live-process: ") and f"[{via}]" in d["reason"], d


def test_service_sid_does_not_exempt_a_relative_script_or_an_unavailable_probe(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-rel")
    svc = _svc_rows(("$PID", "Svc", "LocalSystem", "Own Process", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"))
    row = _fake_row("$PID", cmd="powershell.exe -File tools\\quiet.ps1", session=0, name="powershell.exe", created=_wt_created(wt, 5))
    d = _gate_with_snapshot(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID + svc)
    assert d["action"] == "kept" and any("relative-script-unresolved" in u for u in d["cwdUnknown"]), d
    off = _gate_with_snapshot(wt, _svchost_row(wt), prelude=FORCE_PROBE_UNAVAILABLE + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST)))
    assert off["action"] == "kept" and off["reason"].startswith("cwd-probe-unavailable"), off


def test_real_service_owner_map_holds_only_well_known_service_sids():
    script = (f"$ErrorActionPreference='Stop'; . '{HELPER}'; $m = Get-ServiceAccountByPid; "
              "@($m.Values | ForEach-Object { $_.Sid } | Sort-Object -Unique) -join ','")
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out and set(out.split(",")) <= {"S-1-5-18", "S-1-5-19", "S-1-5-20"}, out


# --- r5 (RETIRE-CWD-UNKNOWN-STARVES-SWEEP-1 r2, sol blocker 1 on PR #353): the SCM StartName is the CONFIGURED account; it
# exempts a pid only when every service key at that pid was last written strictly before the process started.

def _key_written(wt, minutes, **by_name):
    """Prelude: Get-ServiceKeyLastWriteUtc returns the worktree's CreationTimeUtc shifted by `minutes` (None -> $null, an
    unreadable key) for every service, or by by_name[<service>] for the named ones."""
    def expr(m):
        return "$null" if m is None else _wt_created(wt, m)
    arms = "".join(f"if ($Name -eq '{n}') {{ return {expr(m)} }}; " for n, m in by_name.items())
    return "function Get-ServiceKeyLastWriteUtc { param([string]$Name) " + arms + "return " + expr(minutes) + " };"


NSSM = "C:\\tools\\nssm\\nssm.exe"


@pytest.mark.parametrize("key_minutes", [10, 5], ids=["key-written-after-start", "key-written-at-start"])
def test_service_reconfigured_to_localsystem_after_the_process_started_is_still_not_exempt(repo, key_minutes):
    """sol r1 blocker 1: a user-owned nssm.exe service reconfigured to LocalSystem and not yet restarted. The process was
    created at worktree+5 min; its service key was written at/after that, so the configured account is not the live one."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-reconf")
    row = _fake_row("$PID", cmd=None, session=0, name="nssm.exe", created=_wt_created(wt, 5))
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "LaneSvc", "LocalSystem", "Own Process", NSSM)) + _key_written(wt, key_minutes)
    d = _gate_with_snapshot(wt, row, prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [] and any("scm-config-newer-than-process:LaneSvc" in u for u in d["cwdUnknown"]), d


def test_service_key_time_unreadable_is_still_not_exempt(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-keyunread")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST)) + _key_written(wt, None)
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [] and any("scm-config-time-unreadable:BITS" in u for u in d["cwdUnknown"]), d


def test_service_process_creation_time_unreadable_is_still_not_exempt(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-procunread")
    row = _fake_row("$PID", cmd=None, session=0, name="svchost.exe")   # no CreationDate at all
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST)) + _key_written(wt, -60)
    d = _gate_with_snapshot(wt, row, prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [] and any("scm-process-time-unreadable" in u for u in d["cwdUnknown"]), d


def test_share_process_with_one_service_key_newer_than_the_process_is_still_not_exempt(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-sharenewer")
    prelude = (CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST),
                                                        ("$PID", "gpsvc", "LocalSystem", "Share Process", SVCHOST))
               + _key_written(wt, -60, gpsvc=30))
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)
    assert any("scm-config-newer-than-process:gpsvc" in u for u in d["cwdUnknown"]), d


def test_service_key_written_before_the_process_started_is_exempt_and_bound(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-bound")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST)) + _key_written(wt, -60)
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert len(d["cwdExemptOwner"]) == 1 and "exempt=service-sid:S-1-5-18 via=scm:BITS" in d["cwdExemptOwner"][0], d


def test_live_binding_does_not_rewrite_the_shared_index_row_for_the_next_worktree(repo):
    """A sweep reuses one snapshot index across worktrees: a pid refused by the binding for an OLDER worktree must still get
    the creation-time exemption for a worktree created after the process started."""
    tmp, main = repo
    old = _add_wt(main, tmp / "wt-svc-sweep-old")
    new = _add_wt(main, tmp / "wt-svc-sweep-new")
    row = _fake_row("$PID", cmd=None, session=0, name="nssm.exe", created=_wt_created(old, 5))
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; " + CWD_UNREADABLE_FOR_EVERY_PID
        + _svc_rows(("$PID", "LaneSvc", "LocalSystem", "Own Process", NSSM)) + _key_written(old, 10)
        + f" (Get-Item -LiteralPath '{new}').CreationTimeUtc = {_wt_created(old, 60)}; $snap = {row}; "
        f"$a = Get-CwdUnknownRefusal -Snapshot $snap -WorktreePath '{old}'; "
        f"$b = Get-CwdUnknownRefusal -Snapshot $snap -WorktreePath '{new}'; "
        "[pscustomobject]@{ a = @($a.Unknown); b = @($b.Unknown); bExempt = $b.Exempt } | ConvertTo-Json -Depth 3"
    )
    out = json.loads(subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                                    check=True, capture_output=True, text=True).stdout)
    assert len(out["a"]) == 1 and "scm-config-newer-than-process:LaneSvc" in out["a"][0], out
    assert out["b"] == [] and out["bExempt"] == 1, out


@pytest.mark.parametrize("stype", ["Unknown", "Interactive Process"], ids=["unknown", "interactive"])
def test_localsystem_service_of_a_non_own_or_share_type_is_still_not_exempt(repo, stype):
    """fable r1 h1: pins the ServiceType guard. StartName, image and the key binding all pass, so only the type refuses."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-type")
    prelude = CWD_UNREADABLE_FOR_EVERY_PID + _svc_rows(("$PID", "BITS", "LocalSystem", stype, SVCHOST)) + _key_written(wt, -60)
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [], d


GOOD_BITS_ROW = ("[pscustomobject]@{ ProcessId = $PID; Name = 'BITS'; StartName = 'LocalSystem'; ServiceType = 'Share Process'; "
                 "PathName = '" + SVCHOST + "' }")


@pytest.mark.parametrize("bad", [
    "[pscustomobject]@{ ProcessId = 4321; Name = 'BadSvc'; StartName = 'LocalSystem'; ServiceType = 'Own Process' }",
    "[pscustomobject]@{ ProcessId = 'n/a'; Name = 'BadSvc'; StartName = 'LocalSystem'; ServiceType = 'Own Process'; PathName = '" + SVCHOST + "' }",
], ids=["pathname-missing", "pid-unparsable"])
def test_one_unparsable_scm_row_is_skipped_and_named_and_the_other_rows_still_map(repo, bad):
    """fable r1 h2: a malformed row used to throw out of the whole query (scm-failed, nothing exempt host-wide)."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-badrow")
    prelude = (CWD_UNREADABLE_FOR_EVERY_PID + "function Get-RunningServiceRows { @(" + bad + ", " + GOOD_BITS_ROW + ") };"
               + _key_written(wt, -60))
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["cwdOwnerProbe"] == "scm" and len(d["cwdExemptOwner"]) == 1 and "via=scm:BITS" in d["cwdExemptOwner"][0], d
    assert len(d["cwdOwnerSkipped"]) == 1 and d["cwdOwnerSkipped"][0].startswith("BadSvc ") and "scm-row-unparsed" in d["cwdOwnerSkipped"][0], d


def test_unparsable_scm_row_at_the_same_pid_drops_that_pid(repo):
    """The bad row's account is unknown, so the pid it names is not exempt even when another row there looks fine."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-badrow-same")
    bad = "[pscustomobject]@{ ProcessId = $PID; Name = 'BadSvc'; StartName = 'LocalSystem'; ServiceType = 'Share Process' }"
    prelude = (CWD_UNREADABLE_FOR_EVERY_PID + "function Get-RunningServiceRows { @(" + GOOD_BITS_ROW + ", " + bad + ") };"
               + _key_written(wt, -60))
    d = _gate_with_snapshot(wt, _svchost_row(wt), prelude=prelude)
    _assert_deferred_to_rename(d)
    assert d["cwdExemptOwner"] == [] and len(d["cwdOwnerSkipped"]) == 1, d


def test_owner_probe_is_null_when_no_candidate_reaches_the_owner_check(repo):
    """fable r1 h3: cwdOwnerProbe is scm | scm-failed only when some candidate reached the owner check."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-probe-null")
    svc = "function Get-RunningServiceRows { throw 'must not be queried' };"
    old = _fake_row("$PID", cmd=None, session=0, name="svchost.exe", created=_wt_created(wt, -60))
    d = _gate_with_snapshot(wt, old, prelude=CWD_UNREADABLE_FOR_EVERY_PID + svc)
    assert (d["action"], d["reason"]) == ("would-retire", "ok") and d["cwdExempt"] == 1, d
    assert d["cwdOwnerProbe"] is None and d["cwdExemptOwner"] == [] and d["cwdOwnerSkipped"] == [], d
    off = _gate_with_snapshot(wt, _svchost_row(wt), prelude=FORCE_PROBE_UNAVAILABLE + svc)
    assert off["action"] == "kept" and off["cwdOwnerProbe"] is None, off


def test_merged_clean_worktree_whose_only_unreadable_pids_are_live_bound_service_owners_would_retire(repo):
    """sol r1 acceptance row: merged clean worktree, zero real holders, every unreadable-cwd pid is a permitted service owner
    whose configured account is bound to the live process -> would-retire / ok (WhatIf), each pid listed."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-svc-accept")
    created = _wt_created(wt, 5)
    snap = ("[pscustomobject]@{ Procs = @("
            "[pscustomobject]@{ ProcessId = $PID; Name = 'svchost.exe'; CommandLine = $null; SessionId = 0; CreationDate = " + created + " }, "
            "[pscustomobject]@{ ProcessId = $parentPid; Name = 'svchost.exe'; CommandLine = $null; SessionId = 0; CreationDate = " + created + " }"
            "); SelfPids = @(1) }")
    prelude = ("$parentPid = [int](Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId; "
               + CWD_UNREADABLE_FOR_EVERY_PID
               + _svc_rows(("$PID", "BITS", "LocalSystem", "Share Process", SVCHOST),
                           ("$PID", "gpsvc", "LocalSystem", "Share Process", SVCHOST),
                           ("$parentPid", "Dnscache", "NT AUTHORITY\\NetworkService", "Share Process", SVCHOST))
               + _key_written(wt, -60))
    d = _gate_with_snapshot(wt, snap, prelude=prelude)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["holders"] == [] and d["cwdUnknown"] == [] and d["cwdOwnerProbe"] == "scm" and d["cwdOwnerSkipped"] == [], d
    owners = sorted(d["cwdExemptOwner"])
    assert len(owners) == 2, d
    assert any("exempt=service-sid:S-1-5-18 via=scm:BITS,gpsvc bound=config-older-than-process" in o for o in owners), d
    assert any("exempt=service-sid:S-1-5-20 via=scm:Dnscache bound=config-older-than-process" in o for o in owners), d


# --- RETIRE-HOLDER-ENCODEDCOMMAND-DECODE-1 (r6): a holder whose ONLY link to the worktree is a pwsh -EncodedCommand ---
# Fleet doctrine specs/dng-auto-processor.md section 10: agent tools launch a shell as `pwsh -EncodedCommand <base64>`, so a
# command line must be read WITH the encoded argument decoded (UTF-16LE); a raw match misses a path that sits only in base64.
# A snapshot row carries its own CurrentDirectory (a readable cwd elsewhere), so the encoded argument is the only link.

def _b64(text):
    return base64.b64encode(text.encode("utf-16-le")).decode("ascii")


def _enc_row(wt_cwd_home, cmd, name="pwsh.exe"):
    """A live pwsh row (pid = the gate's own, so it is alive) with a readable cwd that is NOT the worktree."""
    return _fake_row("$PID", cmd=cmd, name=name, cwd=str(wt_cwd_home))


def _enc_gate(repo, tag, cmd_for):
    tmp, main = repo
    wt = _add_wt(main, tmp / ("wt-enc-" + tag))
    return wt, _gate_with_snapshot(wt, _enc_row(tmp, cmd_for(wt)))


ENCODED_SWITCHES = ["-EncodedCommand", "-encodedcommand", "-EnCodedCommand", "-enc", "-e", "-ec", "-en", "-encodedc",
                    "/e", "/enc", "-ENC"]


@pytest.mark.parametrize("switch", ENCODED_SWITCHES, ids=lambda s: s.replace("/", "slash").replace("-", "dash"))
def test_holder_whose_only_link_is_an_encoded_command_argument_is_refused_as_live_process(repo, switch):
    wt, d = _enc_gate(repo, "named", lambda wt: f"pwsh.exe -NoProfile {switch} " + _b64(f"Set-Location '{wt}'; Start-Sleep 60"))
    assert d["action"] == "kept" and d["reason"].startswith("live-process: "), d
    assert "[cmdline-encoded]" in d["reason"], d
    assert wt.exists()


@pytest.mark.parametrize("shape", ["colon", "quoted"])
def test_encoded_command_argument_in_colon_or_quoted_form_is_decoded_too(repo, shape):
    def cmd(wt):
        b = _b64(f"Get-ChildItem {wt}")
        return f"pwsh.exe -NoProfile -enc:{b}" if shape == "colon" else f'"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoProfile -EncodedCommand "{b}"'
    _, d = _enc_gate(repo, "shape-" + shape, cmd)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: ") and "[cmdline-encoded]" in d["reason"], d


def test_encoded_command_naming_the_worktree_with_forward_slashes_is_refused(repo):
    _, d = _enc_gate(repo, "fwd", lambda wt: "powershell.exe -NoProfile -EncodedCommand " + _b64("git -C " + str(wt).replace("\\", "/") + " status"))
    assert d["action"] == "kept" and d["reason"].startswith("live-process: "), d


def test_encoded_command_that_dot_sources_a_script_naming_the_worktree_is_followed_into_the_corpus(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-enc-corpus")
    sd = _chain_scripts(tmp, str(wt))                                # only arms.ps1 names the worktree
    row = _enc_row(tmp, "pwsh.exe -NoProfile -EncodedCommand " + _b64(f". '{sd / 'quiet.ps1'}'"))
    d = _gate_with_snapshot(wt, row)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: ") and "script:arms.ps1" in d["reason"], d


def test_encoded_command_naming_another_path_does_not_block_retirement(repo):
    _, d = _enc_gate(repo, "other", lambda wt: "pwsh.exe -NoProfile -EncodedCommand " + _b64("Get-ChildItem C:\\somewhere\\else"))
    assert (d["action"], d["reason"]) == ("would-retire", "ok") and d["cwdUnknown"] == [], d


def test_encoded_command_naming_a_sibling_prefix_of_another_worktree_is_matched_like_the_raw_line(repo):
    """The decoded text gets the SAME path test as the raw command line (a plain case-insensitive substring), nothing wider."""
    _, d = _enc_gate(repo, "case", lambda wt: "pwsh.exe -EncodedCommand " + _b64("cd " + str(wt).upper()))
    assert d["action"] == "kept" and d["reason"].startswith("live-process: "), d


UNDECODABLE = {
    "not-base64": "this-is-not-base64!!",
    "odd-byte-count": base64.b64encode(b"A\x00B").decode("ascii"),
    "lone-surrogate": base64.b64encode(b"\x00\xd8A\x00").decode("ascii"),
    "single-byte": base64.b64encode(b"\x00").decode("ascii"),
    "whitespace-only": _b64("   "),
}


@pytest.mark.parametrize("case", sorted(UNDECODABLE))
def test_undecodable_encoded_command_argument_is_kept_not_retired(repo, case):
    arg = UNDECODABLE[case]
    wt, d = _enc_gate(repo, "bad-" + case, lambda wt: f"pwsh.exe -NoProfile -EncodedCommand {arg}")
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown"), d
    assert any(" encoded-command-undecodable created=" in u for u in d["cwdUnknown"]), d
    assert d["cwdExempt"] == 0 and wt.exists(), d


def test_encoded_command_switch_with_no_value_is_kept_not_retired(repo):
    _, d = _enc_gate(repo, "noval", lambda wt: "pwsh.exe -NoProfile -EncodedCommand")
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown"), d


def test_undecodable_encoded_command_is_not_waved_through_by_creation_time(repo):
    """The r3 creation-time exemption is for an unreadable CWD; it must not excuse a command line the gate cannot read."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-enc-old")
    row = _fake_row("$PID", cmd="pwsh.exe -EncodedCommand ###", name="pwsh.exe", cwd=str(tmp), created=_wt_created(wt, -5))
    d = _gate_with_snapshot(wt, row)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown") and d["cwdExempt"] == 0, d


def test_undecodable_encoded_command_of_a_pid_that_has_exited_is_not_a_candidate(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-enc-gone")
    d = _gate_with_snapshot(wt, _fake_row("999992", cmd="pwsh.exe -EncodedCommand ###", name="pwsh.exe", cwd=str(tmp)))
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


def test_the_same_holder_without_an_encoded_command_is_unchanged(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-enc-regress")
    plain = _gate_with_snapshot(wt, _enc_row(tmp, "pwsh.exe -NoProfile -Command Start-Sleep"))
    assert (plain["action"], plain["reason"]) == ("would-retire", "ok") and plain["cwdUnknown"] == [], plain
    raw = _gate_with_snapshot(wt, _enc_row(tmp, f"pwsh.exe -NoProfile -File {wt}\\x.ps1"))
    assert raw["action"] == "kept" and raw["reason"].startswith("live-process: ") and "[cmdline]" in raw["reason"], raw


@pytest.mark.parametrize("switch", ["-ExecutionPolicy", "-executionpolicy", "-ex", "-exec", "-EX"])
def test_execution_policy_is_not_read_as_encoded_command(repo, switch):
    """-ex / -exec are -ExecutionPolicy. Neither the plain value nor a base64-looking value is decoded or refused."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-enc-ex")
    bypass = _gate_with_snapshot(wt, _enc_row(tmp, f"pwsh.exe -NoProfile {switch} Bypass -File C:\\x\\y.ps1"))
    assert (bypass["action"], bypass["reason"]) == ("would-retire", "ok") and bypass["cwdUnknown"] == [], bypass
    named = _gate_with_snapshot(wt, _enc_row(tmp, f"pwsh.exe -NoProfile {switch} " + _b64(f"cd '{wt}'")))
    assert (named["action"], named["reason"]) == ("would-retire", "ok"), named   # base64 after -ex is NOT decoded


def test_non_powershell_process_with_an_e_switch_is_not_decoded(repo):
    """python -e / node -e take code or a path, not UTF-16LE base64; only pwsh and powershell are read."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-enc-node")
    d = _gate_with_snapshot(wt, _enc_row(tmp, "node.exe -e ###", name="node.exe"))
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


@pytest.mark.skipif(not (PWSH and GIT), reason="requires pwsh and git")
def test_encoded_command_switch_table_matches_pwsh_own_parameter_resolution():
    ok, bad = _b64("Get-Date"), "not-base64!!"
    table = [
        ("pwsh.exe -NoProfile -e " + ok, True, False), ("pwsh.exe -ec " + ok, True, False), ("pwsh.exe -en " + ok, True, False),
        ("pwsh.exe -EnCodedCommand " + ok, True, False), ("pwsh.exe /enc " + ok, True, False),
        ("pwsh.exe -enc:" + ok, True, False), ("pwsh.exe -enc " + bad, True, True), ("pwsh.exe -enc", True, True),
        ("pwsh.exe -ex Bypass", False, False), ("pwsh.exe -exec " + ok, False, False), ("pwsh.exe -ea " + ok, False, False),
        ("pwsh.exe -File C:\\a.ps1", False, False), ("pwsh.exe -NoProfile -Command Get-Date", False, False),
        ("powershell.exe -ec " + ok, True, False), ("node.exe -e " + bad, False, False),
    ]
    items = ", ".join("@('" + c.split(" ")[0] + "', '" + c + "')" for c, _, _ in table)
    script = ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; " f". '{HELPER}'; "
              f"@({items}) | ForEach-Object {{ $r = Get-EncodedCommandText -Name $_[0] -CommandLine $_[1]; "
              "[pscustomobject]@{ Present = $r.Present; Failed = $r.Failed } } | ConvertTo-Json -AsArray")
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True, text=True).stdout
    got = [(r["Present"], r["Failed"]) for r in json.loads(out)]
    assert got == [(p, f) for _, p, f in table]


# --- LANE-RETIRE-CWD-UNKNOWN-RENAME-PROBE-1 (r1): an unreadable cwd no longer refuses; the rename of the worktree root is the probe ---
# NTFS refuses a directory rename while ANY handle beneath it is open (a cwd, a file, whatever the share mode: measured), and it does so
# whatever the ACL of the process that holds it. So a process whose cwd this user cannot read (an IIS app pool, TeamViewer, an elevated
# shell) is neither a reason to keep nor a way to hide: if it holds the tree the rename fails (kept / in-use), if not the retire goes ahead.

def _worktree_paths(main):
    out = subprocess.run([GIT, "-C", str(main), "worktree", "list", "--porcelain"], check=True, capture_output=True, text=True).stdout
    return [_norm(l[len("worktree "):]) for l in out.splitlines() if l.startswith("worktree ")]


def _retiring_of(wt):
    return Path(str(wt) + ".retiring")


def test_rename_probe_refuses_an_unseen_cwd_holder_and_leaves_the_worktree_intact(repo):
    """(a) A real process whose cwd is INSIDE the worktree, with a cwd the gate cannot read and a command line that does not name it:
    the cwd and cmdline checks have nothing to see, so only the rename can tell. Kept / in-use, worktree untouched at its own path."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-inuse")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=wt)
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=_cwd_unreadable_for(p.pid))
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith("in-use"), d
    assert d["renameProbe"].startswith("in-use"), d
    assert (wt / ".git").exists() and (wt / "a.txt").exists(), "the worktree must be intact"
    assert not _retiring_of(wt).exists(), "no .retiring directory may be left behind"
    assert _norm(wt) in _worktree_paths(main), "git must still know the worktree at its own path"


def test_unreadable_cwd_with_no_handle_in_the_worktree_no_longer_blocks_retirement(repo):
    """(b) The 2026-10-21 incident shape: a live process whose cwd cannot be read, nowhere near the worktree. Retired."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-free")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=tmp)
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=_cwd_unreadable_for(p.pid))
    finally:
        _stop(p)
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert d["renameProbe"] == "ok", d
    assert any(u.startswith(f"{p.pid} ") for u in d["cwdUnknown"]), "the unreadable pid is still named as evidence"
    assert not wt.exists() and not _retiring_of(wt).exists()


def test_readable_cwd_inside_the_worktree_is_still_refused_ahead_of_the_rename_probe(repo):
    """(c) Unchanged rule: a readable cwd is a named holder (live-process), found before any rename is attempted."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-readable")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=wt)
    try:
        d, _ = _gate_real(wt, whatif=False)
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith(f"live-process: {p.pid} "), d
    assert d["renameProbe"] == "not-run", d
    assert wt.exists() and not _retiring_of(wt).exists()


def test_a_retire_that_fails_after_a_good_rename_renames_back_and_keeps(repo):
    """(d) The rename succeeds, then `git worktree remove` is refused (a LOCKED worktree: a non-retryable failure). The directory
    goes back to its own name, git is repointed at it, and no .retiring directory is left."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-back")
    _git(main, "worktree", "lock", "--reason", "rename-probe-test", str(wt))
    d, _ = _gate_real(wt, whatif=False)
    assert d["action"] == "kept" and d["reason"].startswith("remove-failed"), d
    assert d["renameProbe"] == "ok", d
    assert wt.exists() and (wt / ".git").exists() and (wt / "a.txt").exists()
    assert not _retiring_of(wt).exists(), "no .retiring directory may be left behind"
    assert _norm(wt) in _worktree_paths(main) and not any(p.endswith(".retiring") for p in _worktree_paths(main)), _worktree_paths(main)
    st = subprocess.run([GIT, "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True)
    assert st.returncode == 0, st.stderr   # git can still use the checkout at its original path


def test_a_successful_retire_leaves_no_worktree_entry_and_no_retiring_directory(repo):
    """(e) The git-consistency pin: after a rename-then-remove, `git worktree list` has no entry for the worktree (not the original
    path, not the .retiring path), its admin dir is gone, and `git worktree prune` has nothing left to prune."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-ok")
    admin = _admin_dir(main, wt)
    d, _ = _gate_real(wt, whatif=False)
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert d["renameProbe"] == "ok", d
    assert not wt.exists() and not _retiring_of(wt).exists()
    assert _norm(wt) not in _worktree_paths(main) and not any(p.endswith(".retiring") for p in _worktree_paths(main)), _worktree_paths(main)
    assert not Path(admin).exists(), "the worktree admin dir must be gone, not dangling"
    pr = subprocess.run([GIT, "-C", str(main), "worktree", "prune", "--verbose"], check=True, capture_output=True, text=True)
    assert (pr.stdout + pr.stderr).strip() == "", pr


def test_whatif_does_not_rename_and_says_so(repo):
    """A would-retire is never backed by a rename: -WhatIf must not move a real lane worktree, even for an instant."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-whatif")
    d, _ = _gate_real(wt, whatif=True)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert d["renameProbe"].startswith("not-run: WhatIf"), d
    assert wt.exists() and not _retiring_of(wt).exists()


def test_an_existing_retiring_directory_is_not_overwritten_and_keeps_the_worktree(repo):
    """A leftover `<root>.retiring` (a crashed earlier run) must not be merged into or clobbered: kept, cannot-determine, untouched."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-rename-leftover")
    _retiring_of(wt).mkdir()
    (_retiring_of(wt) / "marker.txt").write_text("leftover", encoding="utf-8")
    d, _ = _gate_real(wt, whatif=False)
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine: ") and ".retiring" in d["reason"], d
    assert wt.exists() and (wt / ".git").exists()
    assert (_retiring_of(wt) / "marker.txt").read_text(encoding="utf-8") == "leftover"


# r5 (H3, RETIRE-RENAME-PROBE-NESTED-HOLDER-PIN-1, opus r1 / fable r4 / sol r4): the rename probe's claim is "ANY handle beneath the root",
# not just one at the root. A holder one directory down, whose cwd the gate cannot read or whose file allows deletion, still refuses it.

def test_rename_probe_refuses_a_cwd_holder_in_a_subdirectory_of_the_worktree(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-nested-cwd")
    sub = wt / "sub"
    sub.mkdir()
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=sub)
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=_cwd_unreadable_for(p.pid))
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith("in-use"), d
    assert d["renameProbe"].startswith("in-use"), d
    assert sub.is_dir() and (wt / ".git").exists() and (wt / "a.txt").exists(), "the worktree must be intact"
    assert not _retiring_of(wt).exists() and _norm(wt) in _worktree_paths(main) and _pending_records(main) == []


def test_rename_probe_refuses_a_delete_sharing_file_holder_in_a_subdirectory_of_the_worktree(repo):
    """The file is ignored debris (sub/__pycache__/held.pyc), so the gate passes it; the handle allows read, write AND delete sharing."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-nested-file")
    held = wt / "sub" / "__pycache__" / "held.pyc"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"x")
    ready, release = tmp / "nest.ready", tmp / "nest.release"
    p = _spawn_file_handle_holder(held, ready, release, tmp, share="ReadWrite, Delete")
    try:
        d, _ = _gate_real(wt, whatif=False, prelude=ONLY_PYTEST_CHILDREN)
    finally:
        _finish_holder(p, release)
    assert d["action"] == "kept" and d["reason"].startswith("in-use"), d
    assert held.exists() and (wt / ".git").exists() and (wt / "a.txt").exists(), "the worktree must be intact"
    assert not _retiring_of(wt).exists() and _norm(wt) in _worktree_paths(main) and _pending_records(main) == []


# --- LANE-RETIRE-LIVE-DETACHED-DRIVER-GUARD-1: a live DETACHED driver that names only its RUN DIR ---
# 2026-10-24 06:02Z the mlvtmp retention pass removed C:\mlvtmp\lane-CPU-17V9-FPS-GAP-AB-1-20261024 while the detached r5 venue
# driver (pid 43484, `pwsh -File <run dir>\venue-cpu17v9-ab.ps1`, pid in <run dir>\venue-cpu17v9-ab.pid) still ran Wait-VenueQuiet.ps1
# from it: its command line named the run dir, never the worktree, and the lane that launched it had exited (receipt state=complete).
# Get-WorktreeLiveRefusal is the one guard every remover calls; the process table is always FAKED here (pids 99998x are not real).

def _live_refusal(wt, snapshot_expr=None, runs_root=None, prelude=""):
    args = f"-WorktreePath '{wt}'"
    if snapshot_expr:
        args += f" -Snapshot ({snapshot_expr})"
    if runs_root:
        args += f" -RunsRoot '{runs_root}'"
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {prelude} "
        f"$r = Get-WorktreeLiveRefusal {args}; "
        "if ($null -eq $r) { 'FREE' } else { $r | ConvertTo-Json -Depth 4 -Compress }"
    )
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    out = p.stdout.strip()
    return (None if out == "FREE" else json.loads(out)), p.stderr


def _fake_proc(pid, cmdline, name="pwsh.exe"):
    cl = str(cmdline).replace("'", "''")
    return f"[pscustomobject]@{{ ProcessId = {pid}; Name = '{name}'; CommandLine = '{cl}' }}"


def _fake_snapshot(*procs):
    return f"[pscustomobject]@{{ Procs = @({', '.join(procs)}); SelfPids = @($PID) }}"


def _driver_run_dir(runs, name, pid, *, workdir=None, pid_file="venue-driver.pid", child_json=False):
    """A run dir as a detached venue driver leaves it: its driver script (which does NOT name the worktree), its pid file, and the
    launching lane's TERMINAL receipt naming the worktree as its workDir."""
    rd = runs / name
    rd.mkdir(parents=True)
    (rd / "venue-driver.ps1").write_text("Start-Sleep -Seconds 1\n", encoding="utf-8")
    if child_json:
        (rd / pid_file).write_text(json.dumps({"schema": "mlv-lane-child/v1", "pid": pid}), encoding="utf-8")
    else:
        (rd / pid_file).write_text(f"{pid}\n", encoding="utf-8")
    if workdir is not None:
        (rd / "opus-001.receipt.json").write_text(json.dumps({"state": "complete", "exitCode": 0, "workDir": str(workdir)}), encoding="utf-8")
    return rd


def _driver_cmdline(rd):
    return f'"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoProfile -NonInteractive -File "{rd}\\venue-driver.ps1"'


def test_live_refusal_a_process_naming_the_worktree_is_refused_with_a_typed_line(tmp_path):
    """(a) a live process whose command line names the worktree."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    r, err = _live_refusal(wt, _fake_snapshot(_fake_proc(999981, f"git -C {wt} status", "git.exe")))
    assert r is not None and r["reason"] == "live-process" and r["processId"] == 999981, r
    assert r["matchedPath"].lower() == str(wt).lower(), r
    assert re.search(r"REFUSED worktree-removal reason=live-process path=.* pid=999981 .*matched=", err), err


def test_live_refusal_a_driver_named_only_by_its_run_dir_pid_file_is_refused(tmp_path):
    """(b) the 2026-10-24 shape: only the run dir's pid file + terminal receipt tie the live driver to the worktree."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    runs = tmp_path / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-X-1-r5-20261024T0110Z", 999982, workdir=wt)
    r, err = _live_refusal(wt, _fake_snapshot(_fake_proc(999982, _driver_cmdline(rd))), runs)
    assert r is not None and r["reason"] == "live-driver" and r["processId"] == 999982, r
    assert r["via"] == "rundir-pid:venue-driver.pid", r
    assert r["matchedPath"].lower() == str(rd).lower(), r
    assert "reason=live-driver" in err and "pid=999982" in err and f"matched={rd}".lower() in err.lower(), err


def test_live_refusal_a_receipt_child_pid_record_counts_like_a_pid_file(tmp_path):
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    runs = tmp_path / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-X-1-r2", 999983, workdir=wt, pid_file="opus-001.child.json", child_json=True)
    r, _ = _live_refusal(wt, _fake_snapshot(_fake_proc(999983, _driver_cmdline(rd))), runs)
    assert r is not None and r["reason"] == "live-driver" and r["via"] == "rundir-pid:opus-001.child.json", r


def test_gate_keeps_a_worktree_whose_live_driver_names_only_its_run_dir(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-DRV-1")
    runs = tmp / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-DRV-1-r5", 999984, workdir=wt)
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; $snap = {_fake_snapshot(_fake_proc(999984, _driver_cmdline(rd)))}; "
        f"Invoke-RetireLaneWorktree -WorkDir '{wt}' -MergeTarget 'origin/master' -WhatIf -ProcessSnapshot $snap -RunsRoot '{runs}' "
        "| ConvertTo-Json -Depth 4"
    )
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    d = json.loads(p.stdout)
    assert d["action"] == "kept" and d["reason"].startswith("live-process: 999984 pwsh.exe [rundir-pid:venue-driver.pid"), d
    assert wt.exists()


def test_a_dead_driver_pid_and_no_live_reference_still_retire_the_worktree(repo):
    """(c) no regression: a stale pid file (its pid is not running) never holds a worktree."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-DEAD-1")
    runs = tmp / "fleet-runs"
    _driver_run_dir(runs, "lane-DEAD-1-r5", 999985, workdir=wt)
    r, _ = _live_refusal(wt, _fake_snapshot(_fake_proc(999986, "x.exe C:\\somewhere\\else", "x.exe")), runs)
    assert r is None, r
    d = _retire(wt, MergeTarget="origin/master", QuarantineRoot=str(tmp / "q"), RunsRoot=str(runs))
    assert (d["action"], d["reason"]) == ("retired", "ok"), d
    assert not wt.exists()


def test_prefix_sibling_lane_x_10_never_holds_lane_x_1(repo):
    """(d) C:\\mlvtmp\\lane-X-1 must not match C:\\mlvtmp\\lane-X-10 on a command line or through a run dir's receipt."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-X-1")
    sibling = tmp / "lane-X-10"
    runs = tmp / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-X-10-r1", 999987, workdir=sibling)
    snap = _fake_snapshot(_fake_proc(999988, f"git -C {sibling} status", "git.exe"),
                          _fake_proc(999989, f"git -C {str(sibling).replace(chr(92), '/').upper()} log", "git.exe"),
                          _fake_proc(999987, _driver_cmdline(rd)))
    r, _ = _live_refusal(wt, snap, runs)
    assert r is None, r
    d = _gate_with_snapshot(wt, snap)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


def test_live_refusal_matches_case_and_separator_variants_of_the_worktree(tmp_path):
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    variant = str(wt).replace("\\", "/").upper() + "/tools/x.ps1"
    r, _ = _live_refusal(wt, _fake_snapshot(_fake_proc(999990, f"pwsh -File {variant}")))
    assert r is not None and r["reason"] == "live-process", r


def test_live_refusal_fails_closed_when_the_process_census_fails(tmp_path):
    """(e) an unreadable process table is never 'free'."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    r, err = _live_refusal(wt, prelude="function Get-LaneProcessSnapshot { throw 'WMI provider down' };")
    assert r is not None and r["reason"] == "census-failed" and "WMI provider down" in r["detail"], r
    assert "reason=census-failed" in err, err


def test_live_refusal_fails_closed_when_the_run_dir_census_fails(tmp_path):
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    runs = tmp_path / "fleet-runs"
    runs.write_text("not a directory", encoding="utf-8")
    r, _ = _live_refusal(wt, _fake_snapshot(), runs)
    assert r is not None and r["reason"] == "rundir-census-failed", r


def test_gate_fails_closed_with_a_typed_reason_when_the_process_census_fails(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-CENSUS-1")
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; function Get-LaneProcessSnapshot {{ throw 'WMI provider down' }}; "
        f"Invoke-RetireLaneWorktree -WorkDir '{wt}' -MergeTarget 'origin/master' -QuarantineRoot '{tmp / 'q'}' | ConvertTo-Json -Depth 4"
    )
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    d = json.loads(p.stdout)
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine: process census failed"), d
    assert wt.exists()


# --- r2 (PR #654 sol r1 BLOCKER + key3 hardening): a run-dir record that cannot be READ is unknown, never absent ---
# Sol's repro: a live driver named only by its run dir is live-driver while its pid record reads, and was FREE once the same read threw a
# sharing IOException (the catch dropped the error and cached an empty index). Every read failure other than "the file is gone" now refuses
# reason=rundir-census-failed naming the file and the error class. The lock below is a real FileShare.None handle held by the test's own
# pwsh on a temp file; the seam (Read-RunDirRecordText) fakes the error classes a lock cannot produce. No real driver, no real process table.

def _locked(path):
    p = str(path).replace("'", "''")
    return f"$lock = [IO.File]::Open('{p}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None);"


def _seam_throws(leaf, exc_expr):
    return ("function Read-RunDirRecordText { param([string]$Path) "
            f"if ((Split-Path -Leaf $Path) -eq '{leaf}') {{ throw ({exc_expr}) }}; return [IO.File]::ReadAllText($Path) }};")


def _blocker_fixture(tmp_path, *, workdir=None):
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    runs = tmp_path / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-X-1-r5-20261024T0110Z", 999982, workdir=wt if workdir is None else workdir)
    return wt, runs, rd, _fake_snapshot(_fake_proc(999982, _driver_cmdline(rd)))


def test_live_refusal_rundir_record_readable_is_live_driver(tmp_path):
    """(a) the readable half of sol's pair."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    r, _ = _live_refusal(wt, snap, runs)
    assert r is not None and r["reason"] == "live-driver" and r["processId"] == 999982, r


def test_live_refusal_rundir_pid_record_held_by_a_sharing_violation_is_census_failed_never_free(tmp_path):
    """(b) sol's repro with a REAL sharing violation: the pid record is open FileShare.None while the guard reads it."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    rec = rd / "venue-driver.pid"
    r, err = _live_refusal(wt, snap, runs, prelude=_locked(rec))
    assert r is not None, "an unreadable pid record of a live run-dir driver was read as FREE"
    assert r["reason"] == "rundir-census-failed", r
    assert str(rec).lower() in r["detail"].lower() and "IOException" in r["detail"], r
    assert "REFUSED worktree-removal reason=rundir-census-failed" in err and "venue-driver.pid" in err, err


@pytest.mark.parametrize("exc", ["[UnauthorizedAccessException]::new('Access to the path is denied.')",
                                 "[IO.IOException]::new('The process cannot access the file because it is being used by another process.')",
                                 "[InvalidOperationException]::new('anything else')"])
def test_live_refusal_rundir_pid_record_read_error_of_any_class_is_census_failed(tmp_path, exc):
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    r, _ = _live_refusal(wt, snap, runs, prelude=_seam_throws("venue-driver.pid", exc))
    assert r is not None and r["reason"] == "rundir-census-failed", r
    cls = exc.split("]")[0].strip("[").split(".")[-1]
    assert cls in r["detail"] and "venue-driver.pid" in r["detail"], r


def test_live_refusal_rundir_tie_receipt_held_by_a_sharing_violation_is_census_failed_never_free(tmp_path):
    """The pid record reads, the only file tying the run dir to the worktree (the receipt) cannot be read."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    rec = rd / "opus-001.receipt.json"
    r, err = _live_refusal(wt, snap, runs, prelude=_locked(rec))
    assert r is not None and r["reason"] == "rundir-census-failed", r
    assert "opus-001.receipt.json" in r["detail"] and "IOException" in r["detail"], r
    assert "reason=rundir-census-failed" in err, err


def test_live_refusal_rundir_record_gone_between_listing_and_read_is_absent_not_census_failed(tmp_path):
    """A file that no longer exists is absent evidence, not a failure: the dead-record case stays FREE."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    r, _ = _live_refusal(wt, snap, runs, prelude=_seam_throws("venue-driver.pid", "[IO.FileNotFoundException]::new('gone')"))
    assert r is None, r


def test_live_refusal_unreadable_record_in_a_run_dir_naming_another_worktree_does_not_refuse_this_one(tmp_path):
    """The unreadable record is read as naming EVERY live pid: it refuses exactly the worktrees such a pid could hold, not every worktree."""
    other = tmp_path / "lane-Y-1"
    wt, runs, rd, snap = _blocker_fixture(tmp_path, workdir=other)
    r, _ = _live_refusal(wt, snap, runs, prelude=_locked(rd / "venue-driver.pid"))
    assert r is None, r
    other.mkdir()
    r2, _ = _live_refusal(other, snap, runs, prelude=_locked(rd / "venue-driver.pid"))
    assert r2 is not None and r2["reason"] == "rundir-census-failed", r2


def test_live_refusal_rundir_tie_cap_reached_without_a_tie_is_census_failed_never_free(tmp_path):
    """(c) key3 H3: RunDirTieMaxFiles files read without finding the tie is unknown, not free."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    for i in range(5):
        (rd / f"leg-{i:02d}.json").write_text('{"leg": %d}' % i, encoding="utf-8")
    r, err = _live_refusal(wt, snap, runs, prelude="$script:RunDirTieMaxFiles = 3;")
    assert r is not None and r["reason"] == "rundir-census-failed", r
    assert "rundir-tie-cap" in r["detail"] and str(rd).lower() in r["detail"].lower(), r
    assert "reason=rundir-census-failed" in err, err


def test_live_refusal_rundir_tie_reads_the_receipt_before_bulk_leg_output(tmp_path):
    """key3 H3 repro shape: NTFS lists leg-*.out.txt before opus-001.receipt.json; the receipt is read first, so the cap is not reached."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    for i in range(5):
        (rd / f"leg-{i:02d}.out.txt").write_text(f"leg {i}\n", encoding="utf-8")
    r, _ = _live_refusal(wt, snap, runs, prelude="$script:RunDirTieMaxFiles = 3;")
    assert r is not None and r["reason"] == "live-driver", r


def _gate_err(wt, snapshot_expr, runs=None, prelude="", whatif=True):
    extra = (f" -RunsRoot '{runs}'" if runs is not None else "") + (" -WhatIf" if whatif else "")
    snap = f"-ProcessSnapshot ({snapshot_expr})" if snapshot_expr else ""
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {prelude} "
        f"Invoke-RetireLaneWorktree -WorkDir '{wt}' -MergeTarget 'origin/master' {snap}{extra} | ConvertTo-Json -Depth 4"
    )
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(p.stdout), p.stderr


def test_gate_refused_line_carries_reason_live_driver_or_live_process(repo):
    """(d) key3 H4: the gate's REFUSED line carries reason= like the shared guard's line."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-DRV-2")
    runs = tmp / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-DRV-2-r5", 999991, workdir=wt)
    d, err = _gate_err(wt, _fake_snapshot(_fake_proc(999991, _driver_cmdline(rd))), runs)
    assert d["action"] == "kept", d
    assert re.search(r"REFUSED worktree-removal reason=live-driver path=.* pid=999991 .*via=rundir-pid:venue-driver\.pid", err), err
    d, err = _gate_err(wt, _fake_snapshot(_fake_proc(999992, f"git -C {wt} status", "git.exe")), runs)
    assert d["action"] == "kept", d
    assert re.search(r"REFUSED worktree-removal reason=live-process path=.* pid=999992 .*via=cmdline", err), err


def test_gate_census_failures_write_a_typed_refused_line(repo):
    """key3 H4: a gate census failure writes a REFUSED line, not only the disposition reason."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-CENSUS-2")
    d, err = _gate_err(wt, None, prelude="function Get-LaneProcessSnapshot { throw 'WMI provider down' };", whatif=False)
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine: process census failed"), d
    assert re.search(r"REFUSED worktree-removal reason=census-failed path=.* pid=unknown .*WMI provider down", err), err
    runs = tmp / "fleet-runs-file"
    runs.write_text("not a directory", encoding="utf-8")
    d, err = _gate_err(wt, _fake_snapshot(), runs)
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine: run-dir census failed"), d
    assert re.search(r"REFUSED worktree-removal reason=rundir-census-failed path=.* pid=unknown ", err), err
    assert wt.exists()


def test_gate_keeps_a_worktree_whose_live_driver_record_is_locked(repo):
    """The blocker through Invoke-RetireLaneWorktree: kept as cannot-determine, with a typed REFUSED line."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-DRV-3")
    runs = tmp / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-DRV-3-r5", 999993, workdir=wt)
    d, err = _gate_err(wt, _fake_snapshot(_fake_proc(999993, _driver_cmdline(rd))), runs, prelude=_locked(rd / "venue-driver.pid"))
    assert d["action"] == "kept" and d["reason"].startswith("cannot-determine: run-dir census failed"), d
    assert "venue-driver.pid" in d["reason"], d
    assert "REFUSED worktree-removal reason=rundir-census-failed" in err, err


# --- r3 (PR #654 key3 r2 BLOCKER B1): a file that is only held open for WRITING is read, never refused ---
# r2 read run-dir files with [IO.File]::ReadAllText, which asks for FileShare.Read and so cannot open a file another process holds open for
# writing. Every live Invoke-Lane lane (dispatch.out.txt) and every Start-HiddenDetached driver (cmd /c ... > out.txt 2> err.txt) keeps its
# logs write-held for its whole life, and their command lines name their run dirs, so r2 refused EVERY unrelated free worktree while anything
# ran. The run-dir read now opens with FileShare.ReadWrite|Delete: a write-held file reads; a real FileShare.None lock still fails closed.
# The write handle is held by the test's own pwsh on a temp file; the process table is faked (pids 99997x are not real).

def _write_held(path):
    p = str(path).replace("'", "''")
    return f"$wh_{re.sub(r'[^A-Za-z0-9]', '_', path.name)} = [IO.File]::Open('{p}', [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::Read);"


def _lane_cmdline(rd):
    return f'"C:\\Users\\x\\.local\\bin\\claude.exe" -p --settings "{rd}\\opus-001.settings.json" --model opus'


def _unrelated_live_run_dir(tmp_path, shape):
    """A live lane or detached driver working on ANOTHER worktree (lane-Y-1), with its log write-held, as on the real board."""
    other = tmp_path / "lane-Y-1"
    runs = tmp_path / "fleet-runs"
    if shape == "detached-driver":
        rd = _driver_run_dir(runs, "lane-Y-1-r5-20261024T0110Z", 999971, workdir=other)
        log, cl = rd / "driver.out.txt", _driver_cmdline(rd)
    else:
        rd = _driver_run_dir(runs, "lane-Y-1-r1-20261024T1100Z", 999971, workdir=other, pid_file="opus-001.child.json", child_json=True)
        (rd / "opus-001.settings.json").write_text('{"hooks": {}}', encoding="utf-8")
        log, cl = rd / "dispatch.out.txt", _lane_cmdline(rd)
    log.write_text("lane output so far\n", encoding="utf-8")
    return runs, rd, log, _fake_proc(999971, cl)


@pytest.mark.parametrize("shape", ["detached-driver", "invoke-lane"])
def test_live_refusal_write_held_log_in_an_unrelated_live_run_dir_is_free_for_another_worktree(tmp_path, shape):
    """(a) B1: an unrelated live run dir whose log is held open for writing leaves a different free worktree FREE."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    runs, rd, log, proc = _unrelated_live_run_dir(tmp_path, shape)
    r, err = _live_refusal(wt, _fake_snapshot(proc), runs, prelude=_write_held(log))
    assert r is None, f"a write-held log in an unrelated live run dir refused a free worktree: {r}"
    assert "REFUSED" not in err, err


def test_gate_retires_a_free_worktree_while_an_unrelated_live_run_dir_holds_its_log_open(repo):
    """(a) B1 through Invoke-RetireLaneWorktree: would-retire, not cannot-determine."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-FREE-1")
    runs, rd, log, proc = _unrelated_live_run_dir(tmp, "detached-driver")
    d, err = _gate_err(wt, _fake_snapshot(proc), runs, prelude=_write_held(log))
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d
    assert "REFUSED" not in err, err


def test_live_refusal_tied_record_under_a_real_lock_is_census_failed_beside_a_write_held_log(tmp_path):
    """(b) sol r1's case still holds after the share change: a tied record under FileShare.None is unknown, never FREE."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    log = rd / "driver.out.txt"
    log.write_text("driver output\n", encoding="utf-8")
    rec = rd / "opus-001.receipt.json"
    r, err = _live_refusal(wt, snap, runs, prelude=_write_held(log) + _locked(rec))
    assert r is not None and r["reason"] == "rundir-census-failed", r
    assert "opus-001.receipt.json" in r["detail"] and "IOException" in r["detail"] and "driver.out.txt" not in r["detail"], r
    assert "reason=rundir-census-failed" in err, err


@pytest.mark.parametrize("leaf", ["opus-001.receipt.json", "venue-driver.pid"])
def test_live_refusal_write_held_tied_record_is_still_read_and_refuses_as_live_driver(tmp_path, leaf):
    """(c) a write-held record that ties the run dir to the worktree is READ: the holder is seen as live-driver, not census-failed."""
    wt, runs, rd, snap = _blocker_fixture(tmp_path)
    r, err = _live_refusal(wt, snap, runs, prelude=_write_held(rd / leaf))
    assert r is not None and r["reason"] == "live-driver" and r["processId"] == 999982, r
    assert r["matchedPath"].lower() == str(rd).lower(), r
    assert "reason=live-driver" in err, err


# --- LANE-RETIRE-GUARD-UNKNOWN-CWD-AND-ENCODED-1 (Dot reconciliation 663162dd on PR #654, hub ruling 2026-10-24T14:47:02Z) ---
# B1: Get-WorktreeLiveRefusal answered FREE for a candidate holder whose cwd could not be read (probe unavailable, or unreadable and newer
# than the worktree) or whose -EncodedCommand did not decode: the holder index listed it as unknown and nothing read that list. Its callers
# (repo_hygiene apply_worktree_remove) have no rename probe, so unknown now refuses, typed cwd-unknown / cwd-probe-unavailable.
# B2: a driver started as `pwsh -EncodedCommand <base64>` whose run dir is named only inside the base64 was not tied to its run dir.
# The holder rows below are synthetic: $PID (this test's own pwsh, listed as NOT self) stands in for a live holder, pids 99996x are not real.

NO_SCM_SERVICES = "function Get-RunningServiceRows { @() };"


def _b64_utf16(text):
    return base64.b64encode(text.encode("utf-16-le")).decode("ascii")


def _encoded_driver_cmdline(script_text):
    return f'"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoProfile -NonInteractive -EncodedCommand {_b64_utf16(script_text)}'


def test_b1_live_refusal_holder_with_unreadable_cwd_newer_than_the_worktree_refuses_cwd_unknown(tmp_path):
    """Dot B1 (a): the worktree is named nowhere but in the holder's cwd, which cannot be read. Was FREE."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    row = _fake_row("$PID", cmd="shell.exe --serve", created=_wt_created(wt, 5))
    r, err = _live_refusal(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID + NO_SCM_SERVICES)
    assert r is not None, "a live holder whose cwd cannot be read was answered FREE"
    assert r["reason"] == "cwd-unknown" and r["via"] == "cwd-unknown" and r["name"] == "shell.exe", r
    assert "cwd-unreadable" in r["detail"], r
    assert re.search(r"REFUSED worktree-removal reason=cwd-unknown path=.* pid=\d+ name=shell\.exe via=cwd-unknown", err), err


def test_b1_live_refusal_holder_when_the_cwd_probe_is_unavailable_refuses_cwd_probe_unavailable(tmp_path):
    """Dot B1 (a), the probe-unavailable half: no exemption applies when the probe itself cannot run. Was FREE."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    row = _fake_row("$PID", cmd="shell.exe --serve", created=_wt_created(wt, -60))
    r, err = _live_refusal(wt, row, prelude=FORCE_PROBE_UNAVAILABLE + NO_SCM_SERVICES)
    assert r is not None and r["reason"] == "cwd-probe-unavailable" and r["via"] == "cwd-probe-unavailable", r
    assert "reason=cwd-probe-unavailable" in err, err


def test_b1_live_refusal_holder_whose_encoded_command_does_not_decode_refuses(tmp_path):
    """Grammar: a holder whose encoded command cannot be read refuses removal with a typed reason. Was FREE."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    row = _fake_row("$PID", cmd="pwsh.exe -NoProfile -EncodedCommand !!not-base64!!", name="pwsh.exe", cwd=str(tmp_path))
    r, err = _live_refusal(wt, row, prelude=NO_SCM_SERVICES)
    assert r is not None and r["reason"] == "cwd-unknown" and r["via"] == "encoded-command-undecodable", r
    assert "encoded-command-undecodable" in r["detail"], r


def test_b1_live_refusal_unreadable_cwd_holder_older_than_the_worktree_stays_exempt_and_free(tmp_path):
    """No over-refusal: the r3 creation-time exemption still applies (a process older than the directory cannot have started in it)."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    row = _fake_row("$PID", cmd="shell.exe --serve", created=_wt_created(wt, -60))
    r, err = _live_refusal(wt, row, prelude=CWD_UNREADABLE_FOR_EVERY_PID + NO_SCM_SERVICES)
    assert r is None, r
    assert "REFUSED" not in err, err


def _encoded_driver_fixture(tmp_path, *, workdir=None):
    """A live pwsh driver whose ONLY link to its run dir is inside -EncodedCommand; the run dir's pid file names it and its receipt
    names the worktree. The driver script does not name the worktree, so no command-line, cwd or script hit can see it."""
    wt = tmp_path / "lane-X-1"
    wt.mkdir()
    runs = tmp_path / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-X-1-r5-20261024T0110Z", 999961, workdir=wt if workdir is None else workdir)
    cl = _encoded_driver_cmdline(f"Set-Location -LiteralPath '{tmp_path}'; & '{rd}\\venue-driver.ps1'")
    assert str(rd).lower() not in cl.lower() and "fleet-runs" not in cl
    return wt, runs, rd, _fake_snapshot(_fake_proc(999961, cl))


def test_b2_live_refusal_driver_naming_its_run_dir_only_inside_encoded_command_is_live_driver(tmp_path):
    """Dot B2 (b), readable record: was FREE."""
    wt, runs, rd, snap = _encoded_driver_fixture(tmp_path)
    r, err = _live_refusal(wt, snap, runs)
    assert r is not None, "a live -EncodedCommand driver named only by its run dir was answered FREE"
    assert r["reason"] == "live-driver" and r["processId"] == 999961, r
    assert r["via"] == "rundir-pid:venue-driver.pid+cmdline-encoded", r
    assert r["matchedPath"].lower() == str(rd).lower(), r
    assert "reason=live-driver" in err and "via=rundir-pid:venue-driver.pid+cmdline-encoded" in err, err


def test_b2_live_refusal_encoded_driver_with_an_unreadable_pid_record_is_census_failed(tmp_path):
    """Dot B2 (b), unreadable record: the locked record is read as naming every live pid; the encoded driver ties through it. Was FREE."""
    wt, runs, rd, snap = _encoded_driver_fixture(tmp_path)
    r, err = _live_refusal(wt, snap, runs, prelude=_locked(rd / "venue-driver.pid"))
    assert r is not None and r["reason"] == "rundir-census-failed", r
    assert "rundir-record-unreadable" in r["detail"] and "venue-driver.pid" in r["detail"], r


def test_b2_live_refusal_encoded_driver_of_another_worktree_does_not_refuse_this_one(tmp_path):
    """No over-refusal: the decoded run dir ties to lane-Y-1, so lane-X-1 stays FREE."""
    wt, runs, rd, snap = _encoded_driver_fixture(tmp_path, workdir=tmp_path / "lane-Y-1")
    r, _ = _live_refusal(wt, snap, runs)
    assert r is None, r


def test_b2_gate_keeps_a_worktree_whose_encoded_driver_names_only_its_run_dir(repo):
    """B2 through Invoke-RetireLaneWorktree (it shares Find-RunDirDriverHolders)."""
    tmp, main = repo
    wt = _add_wt(main, tmp / "lane-ENC-1")
    runs = tmp / "fleet-runs"
    rd = _driver_run_dir(runs, "lane-ENC-1-r5", 999962, workdir=wt)
    snap = _fake_snapshot(_fake_proc(999962, _encoded_driver_cmdline(f"& '{rd}\\venue-driver.ps1'")))
    d, err = _gate_err(wt, snap, runs)
    assert d["action"] == "kept" and "rundir-pid:venue-driver.pid+cmdline-encoded" in d["reason"], d
    assert "reason=live-driver" in err, err


# --- Launcher census: the guard ties a lane driver to its run dir through its COMMAND LINE (raw, or decoded since B2). Every tracked
# launch site that starts PowerShell from base64 or detaches a process is listed here with the reason it is safe, so a new one fails
# this census until someone classifies it, instead of slipping past Get-WorktreeLiveRefusal.

# LAUNCHER-CENSUS-ROOTS-AND-DOCTRINE-SCOPE-1: the census used to walk only tools/coordination and tools/profiling, so tracked launch sites
# elsewhere (tools/agent-bridge, tools/closeout, ...) were invisible. A git pathspec `*` crosses `/`, so these cover every tracked script.
CENSUS_PATHSPECS = ("*.ps1", "*.psm1")
CENSUS_EXCLUDED = ("tools/coordination/Retire-LaneWorktree.ps1",)  # the guard decodes -EncodedCommand; it launches nothing
# Sites are read from the PowerShell AST by tests/coordination/Test-LaunchSites.ps1 (rules in its header; seam: _encoded_sites,
# _detached_sites, _unresolved_sites below). Per-SITE inventories: {path: [(pin, reason), ...]}. The pin is a stable substring of the site
# text (the launching pipeline, or the statement around a string, with comments and line continuations dropped and whitespace collapsed);
# the list length is the expected site COUNT in that file. A new site in a classified file, or a pinned site that moved or vanished, fails
# the census just as a new file does.
ENCODED_LAUNCH_SITES = {
    "tools/agent-bridge/wake_codex.ps1": [
        ('"-EncodedCommand",',
         "the UIA wake helper (-RunInnerWake body): a synchronous child, waited for at $TotalRuntimeTimeoutSeconds and killed on overrun; it drives "
         "the Codex Desktop window and holds no lane worktree, so it is never a lane driver the retire guard has to find"),
    ],
    "tools/coordination/Invoke-Lane.ps1": [
        ("'-ExecutionPolicy','Bypass','-EncodedCommand',$encoded",
         "the Claude-engine job host (pwsh -EncodedCommand $hostSource) is not a driver: it waits on its claude.exe child, whose argv "
         "names <RunDir> through --settings <RunDir>\\<lane>-NNN.settings.json and whose pid is written to <RunDir>\\<lane>-NNN.child.json"),
    ],
    "tools/profiling/bachelor/playback-attr-3-cuda-job.ps1": [
        ("-EncodedCommand $CorePartitionHelperEncoded",
         "BACHELOR-CORE-PARTITION-AB-1: the venue-side core-partition telemetry helper (a bounded pwsh the playback job spawns, killed by pid "
         "on a stop timeout and ended by its own deadline); read-only counters plus its ready / record files in the job's $Work, never a lane "
         "driver and never run against a hub lane worktree"),
    ],
    "tools/profiling/dual-venue/Invoke-VenueStageNightly.ps1": [
        ("'-NonInteractive', '-EncodedCommand', [Convert]::ToBase64String",
         "Invoke-Child -CommandText: a synchronous child, waited for and killed at -MaxTotalSec, never a detached driver"),
    ],
    "tools/profiling/bachelor/ultra-magnus-agent.ps1": [
        ('"-EncodedCommand", (New-HeavySlotSentinelCommand',
         "the heavy-slot sentinel runs on the venue host, never against a hub lane worktree"),
    ],
}
# Sites below that earlier censuses never saw (multi-line Start-Process under a backtick, or a root outside tools/coordination + tools/profiling)
# are classified from reading the launching statement: none starts a PowerShell driver that holds a lane worktree, and none is OPEN.
DETACHED_LAUNCH_SITES = {
    "tools/agent-bridge/wake_codex.ps1": [
        ('Start-Process -FilePath "powershell.exe" -ArgumentList $argumentList -WindowStyle Hidden -PassThru',
         "carries the -EncodedCommand site above: the wake helper is waited on with a hard timeout and killed on overrun, not left running"),
    ],
    "tools/build-release.ps1": [
        ("Start-Process -FilePath $destExe -ArgumentList '--batch','--help'",
         "smoke probe of the freshly built MLV-App exe with redirected output; not PowerShell, no lane worktree"),
    ],
    "tools/closeout/start-closeout-dashboard.ps1": [
        ("Start-Process -FilePath $pythonExe -ArgumentList $argumentString -WorkingDirectory $resolvedRepo",
         "python closeout dashboard server (-m tools.repo_hygiene.closeout_dashboard --repo-root): not a PowerShell lane driver; its cwd is the "
         "repo it serves, which the guard's cwd probe reads"),
    ],
    "tools/coordination/autopilot/product-share.ps1": [
        ("Start-Process -FilePath $exe -ArgumentList $argList -WindowStyle Hidden",
         "the verdict worker re-launches this script with -File; it holds no lane worktree"),
    ],
    "tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1": [
        ("Start-Process -FilePath $releaseExe -ArgumentList '--batch', '--help'",
         "venue-side smoke probe of the assembled release exe; not PowerShell, no lane worktree"),
    ],
    "tools/profiling/bachelor/playback-attr-3-cuda-job.ps1": [
        ("Start-Process -FilePath (Join-Path $Cache $PresentMonName) -ArgumentList $pmArgs",
         "venue-side job: PresentMon / python helpers, not PowerShell drivers"),
        ("-ArgumentList @('--session_name', $SessionName",
         "venue-side job: PresentMon session teardown helper, not a PowerShell driver"),
        ("Start-Process -FilePath $candidate.exe -ArgumentList $depsArgs",
         "venue-side job: dependency probe exe, not a PowerShell driver"),
        ("Start-Process -FilePath $contactSheetPyExe -ArgumentList $composeArgs",
         "venue-side job: python contact-sheet composer, not a PowerShell driver"),
    ],
    "tools/profiling/install-ultra-magnus-agent.ps1": [
        ('"-File",$AgentScript) -WindowStyle Hidden',
         "installs the venue-host agent with -File and its script path on the command line; runs on the venue host, never against a hub lane worktree"),
    ],
    "tools/profiling/measure-gpu-vram.ps1": [
        ('Start-Process -FilePath $probe -ArgumentList @("$($r.w)"',
         "venue-side VRAM probe exe with redirected output; not PowerShell, no lane worktree"),
        ('-ArgumentList @("1808","2268","4000")',
         "venue-side VRAM probe exe with redirected output; not PowerShell, no lane worktree"),
    ],
    "tools/profiling/ultra-magnus-agent.ps1": [
        ('"-File", $jobPathArgument)',
         "the venue-host agent runs a dropped job with -File <job path> under a wall-clock timeout that kills the root and its descendants; "
         "it never runs against a hub lane worktree"),
    ],
}
# r3: strings that are EXECUTED ([scriptblock]::Create, .AddScript, iex, pwsh -Command) but built at run time, so no scanner can read the
# code they will run. Each is classified from reading how the string is built: its literal parts are scanned by rule S (and hold no launch),
# and the variable parts are paths, numbers or a caller's { } literal. A new one fails the census until it is classified here.
UNRESOLVED_EXECUTED_SITES = {
    "tools/coordination/build-guard.ps1": [
        ('[scriptblock]::Create("[CmdletBinding()] param()`n" + $fragment)',
         "the -Command build fragment its caller hands build-guard (mingw32-make / qmake lines), run in-process and waited on under the host "
         "memory guard; a tracked caller passes it as a literal, which rule S scans in the caller's own file"),
    ],
    "tools/profiling/bachelor/AttrCudaArtifacts.psm1": [
        ("[void]$shell.AddScript($loopScript)",
         "r5 rule V2: $loopScript is the { } literal at :6242 in the same function (Start-AttrCudaDisplayWakeKeepAlive), its only writer, "
         "which the AST scans as code; it is unresolved because this module's item cmdlets take non-constant paths (Copy-Item / "
         "Move-Item / Remove-Item -LiteralPath $var), none of them on the variable: drive"),
    ],
    "tools/profiling/bachelor/playback-attr-3-cuda-job.ps1": [
        ("-ExecutionPolicy Bypass -Command $cmd 1>",
         "$cmd is the smoke-script call `& <smoke.ps1> -ExePath .. -Input .. -Output ..` built at :2701 (+ :2740 / :4696 argument appends): "
         "quoted paths and numbers, a synchronous venue-side child that is waited on, never a hub lane driver"),
        ("-ExecutionPolicy Bypass -Command $preCmd 1>",
         "inside the self-heal pre-launch block this job writes into its own copy: $preCmd is $cmd with the output path and environment list "
         "swapped (.Replace), the same synchronous venue-side smoke call"),
        ("-ExecutionPolicy Bypass -Command $cmd 1>",
         "BACHELOR-CORE-PARTITION-AB-1: the generator's anchor here-string ($cpLaunchOld) holding the smoke launch above, byte for byte, "
         "so the post-expansion edit can find it exactly once; the generator runs it never (it is text, not a statement it executes)"),
        ("-ExecutionPolicy Bypass -Command $cmd 1>",
         "BACHELOR-CORE-PARTITION-AB-1: the same smoke launch inside the bracketed replacement the generator emits (only with "
         "-CorePartitionTelemetry), wrapped in a try / finally; the same synchronous venue-side child"),
    ],
    "tools/profiling/bachelor/reference-frame-runner-job.ps1": [
        ("-Command $captureCommand",
         "$captureCommand is `& '<bundle capture script>' -Exe .. -Clip .. -OutDir ..` plus an exit-code suffix (:585, :588): quoted paths and "
         "numbers, a synchronous venue-side capture"),
    ],
    "tools/profiling/dual-venue/Invoke-VenueBaseline.ps1": [
        ("[void]$ps.AddScript($Script.ToString())",
         "Invoke-Bounded runs its caller's [scriptblock] on a worker runspace; both callers in this file pass a { } literal (a Test-Path / "
         "Get-FileHash read), which the AST scans as code"),
    ],
    "tools/profiling/dual-venue/Invoke-VenueStageNightly.ps1": [
        ("[void]$ps.AddScript($Script.ToString())",
         "the same Invoke-Bounded pattern: every caller in this file hands it a { } literal share read, which the AST scans as code"),
    ],
    "tools/profiling/dual-venue/Summarize-VenueBusyProbes.ps1": [
        (". ([scriptblock]::Create($fn[0].Extent.Text))",
         "r4 (from #661): dot-sources the text of the one FunctionDefinitionAst named Get-VenueQuietDecision (-ceq, exactly one) parsed "
         "from -DecisionSource, which defaults to the tracked Wait-VenueQuiet.ps1 beside it (:42); that pure function (:140-154) is scanned "
         "in its own file. Dot-sourcing a definition only defines the function, which is then called in-process at :118"),
        (". ([scriptblock]::Create($f[0].Extent.Text))",
         "r3 (from #692): dot-sources the text of exactly one FunctionDefinitionAst per name (-ceq; refuses unless exactly one is found) for "
         "Test-PerCoreNumber and Get-VenuePerCoreVerdict (:101-105), parsed from -DecisionSource, which defaults to the tracked Wait-VenueQuiet.ps1 "
         "beside it (:47); both are pure functions (Wait-VenueQuiet.ps1:1306-1345) scanned in their own file. Only reached with -PerCoreEnforce. "
         "Dot-sourcing a definition only defines the function, which is then called in-process at :132"),
    ],
    "tools/profiling/dual-venue/VenueChain.psm1": [
        ("[void]$ps.AddScript($ScriptText)",
         "Invoke-VenueBoundedRead runs a share read on a worker runspace; its callers in this module pass a { } literal's text or a "
         "single-quoted Test-Path literal, both scanned as code"),
        ("$step = [scriptblock]::Create($Body)",
         "r4 (from #633): inside the keeper-loop here-string ($script:VenueKeeperLoop); $Body is Start-VenueKeeper -StepText, and both "
         "callers in this module (Start-VenueClaimKeeper, Start-VenueJobLiveness) pass a single-quoted here-string $step that rule S scans "
         "in place; it runs on an in-process worker runspace"),
        ("& ([scriptblock]::Create($A.writeText)) $w",
         "r4 (from #633): inside Start-VenueJobLiveness's step here-string; $A.writeText is $script:LivenessWriteText, the single-quoted "
         "here-string liveness-sidecar write at :822 that rule S scans in place"),
        ("[void]$ps.AddScript($script:VenueKeeperLoop)",
         "r5 rule V2: Start-VenueKeeper runs $script:VenueKeeperLoop, the single-quoted here-string assigned once at top level (:744), "
         "which rule S scans in place (the keeper-step row above is inside it); it is unresolved because this module's New-Item / "
         "Remove-Item take non-constant paths, none of them on the variable: drive"),
        (". ([scriptblock]::Create($t))",
         "r4 (from #673): inside the single-quoted here-string $script:LivenessReadbackText (:860-866) that rule S scans in place; $t iterates "
         "$A.functions, which Test-VenueJobLivenessReadback (:887-891) fills with the Extent.Text of exactly the FunctionDefinitionAsts named "
         "Get-JobNotAfterVerdict and Read-JobNotAfterSidecar (it refuses unless both are found; the name match is -eq, case-insensitive, like "
         "PowerShell's own function lookup) parsed from the tracked tools/profiling/bachelor/ultra-magnus-agent.ps1 (:722 and :832), which the "
         "census scans in its own file. Dot-sourcing a definition only defines the function, which is then called on the same bounded worker runspace"),
    ],
    "tools/profiling/dual-venue/Wait-VenueQuiet.ps1": [
        ("[void]$pp.AddScript($IdentityWorkerPrelude)",
         "r4 rule V: $IdentityWorkerPrelude is assigned '' at script scope (:733) and read inside Get-IdentWorker, another scope. Empty in "
         "production (and guarded by `if ($IdentityWorkerPrelude)`); only test_dual_venue_evidence.py rewrites that literal in its own "
         "copy, to re-declare command shadows in the in-process identity worker runspace"),
        ("[void]$ps.AddScript($Script)",
         "r4 (from #633): Invoke-IdentBounded runs one identity read on its worker runspace; its four callers pass $IdentCimScript, "
         "$IdentOwnerScript, $IdentScriptCheckScript or $IdentGpuScript, each a { } literal's .ToString() (:857-900) that the AST scans as code"),
        ("[void]$pp.AddScript($TelemetryWorkerPrelude)",
         "VENUE-PROBE-HEAT-GPU-TELEMETRY-1: $TelemetryWorkerPrelude is assigned '' at script scope (:527) and read inside Invoke-TelBounded, "
         "another scope. Empty in production (and guarded by `if ($TelemetryWorkerPrelude)`); only test_dual_venue_evidence.py rewrites that "
         "literal in its own copy, to declare Get-PnpDevice / Get-PnpDeviceProperty / nvidia-smi shadows in the telemetry worker runspace"),
        ("[void]$ps.AddScript($Script)",
         "VENUE-PROBE-HEAT-GPU-TELEMETRY-1: Invoke-TelBounded runs one telemetry read on its worker runspace; its four callers pass "
         "$TelTailScript (twice), $TelGpuStateScript or $TelSmiScript, each a { } literal's .ToString() (:530-563) that the AST scans as code"),
    ],
    "tools/profiling/ultramagnus/playback-attr-3-cuda-dll-job.ps1": [
        ("-Command $reconCommand",
         "$reconCommand is `& <recon script> -Dir <backend> -CudaArchitectures <list>` (:347): a synchronous venue-side build step"),
        ("-Command $amazeCommand",
         "$amazeCommand is `& <amaze script> -Dir <backend> -Arch <label>` (:368): a synchronous venue-side build step"),
    ],
}


def _census_sources():
    out = subprocess.run([GIT, "-C", str(REPO), "ls-files", "-z", "--", *CENSUS_PATHSPECS],
                         check=True, capture_output=True, text=True).stdout.split("\0")
    return {p: (REPO / p).read_text(encoding="utf-8", errors="replace") for p in out
            if p and p not in CENSUS_EXCLUDED and "/test" not in p.lower() and not Path(p).name.lower().startswith("test")}


def test_launcher_census_every_encoded_command_launch_site_is_classified():
    src = _census_sources()
    assert src, "git ls-files returned no PowerShell scripts: the census would pass on nothing"
    problems = _census_problems(src, ENCODED_LAUNCH_SITES, {}, kinds=("encoded",))
    assert not problems, ("\n".join(problems) + "\nStart a lane driver with -File and its run dir on the command line, or classify the "
                          "site in ENCODED_LAUNCH_SITES with the reason the retire guard still sees it (drop an entry whose site is gone).")


def test_launcher_census_every_detached_launch_site_is_classified():
    src = _census_sources()
    assert src, "git ls-files returned no PowerShell scripts: the census would pass on nothing"
    problems = _census_problems(src, {}, DETACHED_LAUNCH_SITES, kinds=("detached",))
    assert not problems, ("\n".join(problems) + "\nLaunch PowerShell drivers with -File and the run dir on the command line, then "
                          "classify the site in DETACHED_LAUNCH_SITES (drop an entry whose site is gone).")
    for p, entries in DETACHED_LAUNCH_SITES.items():
        for pin, why in entries:
            site = next(ln for _, ln in _detached_sites(src[p]) if pin in ln)
            assert not _encoded_sites(site), f"{p}: a detached launch now carries -EncodedCommand ({why}); a detached driver must start with -File"
    assert "'-File', \"`\"$PSCommandPath`\"\"" in src["tools/coordination/autopilot/product-share.ps1"], "product-share no longer re-launches itself with -File"


def test_launcher_census_every_executed_string_built_at_run_time_is_classified():
    """r3: a string run by [scriptblock]::Create / .AddScript / iex / pwsh -Command that the scanner cannot resolve could carry any launch."""
    src = _census_sources()
    assert src, "git ls-files returned no PowerShell scripts: the census would pass on nothing"
    problems = _census_problems(src, {}, {}, kinds=("unresolved",), unresolved_inventory=UNRESOLVED_EXECUTED_SITES)
    assert not problems, ("\n".join(problems) + "\nBuild the executed string from literals the census can read, or classify the site in "
                          "UNRESOLVED_EXECUTED_SITES with what the string is built from (drop an entry whose site is gone).")


def test_launcher_census_invoke_lane_ties_its_encoded_host_child_to_the_run_dir():
    """The one Invoke-Lane -EncodedCommand site is safe only while its child names the run dir and is recorded in it."""
    t = (REPO / "tools/coordination/Invoke-Lane.ps1").read_text(encoding="utf-8")
    assert len(re.findall(r"'-EncodedCommand'", t)) == 1, "Invoke-Lane.ps1 gained another -EncodedCommand launch: classify it"
    assert re.search(r"\$candidate = Join-Path \$RunDir", t) and "$base = $candidate" in t, "lane slot base is no longer under $RunDir"
    assert '$settingsPath = "$base.settings.json"' in t and "$argv += @('--settings', $settingsPath)" in t, "child argv no longer names the run dir"
    assert '$controlPath = "$base.child.json"' in t and "pid=$child.Id" in t, "child pid is no longer recorded in the run dir"
    host = t.split("$hostSource = @'", 1)[1].split("'@", 1)[0]
    assert "-File" not in host and "Start-Process" not in host and "WaitForExit()" in host, "the encoded host must only wait on its child"


# --- LANE-RETIRE-LAUNCH-SITE-CENSUS-1 / LAUNCHER-CENSUS-ROOTS-AND-DOCTRINE-SCOPE-1: the census classifies launch SITES, not files.
# These fixtures run against synthetic source text through the same seam the real census uses (_encoded_sites, _detached_sites,
# _census_problems), so a site the census cannot see fails here without anyone adding a real launcher script to the repo.

B64 = "UwB0AGEAcgB0AC0AUwBsAGUAZQBwACAAMQAw"


# r3 (hub ruling): sites are read from the PowerShell parser's own AST by Test-LaunchSites.ps1 beside this file, not by a Python tokenizer;
# its header writes down every rule (pwsh's -EncodedCommand prefix rule, Start-Process's own WindowStyle prefixes, executed strings).
# One pwsh process scans a whole batch of sources; results are cached per source text.
SITE_SCANNER = Path(__file__).resolve().with_name("Test-LaunchSites.ps1")
_SITE_CACHE = {}


def _scan_sites(texts):
    """Fill _SITE_CACHE for every text not yet scanned, in one pwsh call. Raises if the scanner fails: a census that cannot scan never passes."""
    todo = list(dict.fromkeys(t for t in texts if t not in _SITE_CACHE))
    if not todo:
        return
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SITE_SCANNER)], input=json.dumps({str(i): t for i, t in enumerate(todo)}),
                       capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert r.returncode == 0, f"launch-site scanner failed (exit {r.returncode}): {r.stderr[-2000:]}"
    got = json.loads(r.stdout)
    for i, t in enumerate(todo):
        _SITE_CACHE[t] = tuple((s["kind"], s["line"], s["text"]) for s in got[str(i)])


def _sites(text, kind):
    _scan_sites([text])
    return [(n, ln) for k, n, ln in _SITE_CACHE[text] if k == kind]


def _encoded_sites(text):
    """Launch sites that hand PowerShell a base64 program: one (line, site text) per site (Test-LaunchSites.ps1 rules E1/E2/S)."""
    return _sites(text, "encoded")


def _detached_sites(text):
    """Hidden Start-Process and Win32_Process Create sites: one (line, site text) per site (Test-LaunchSites.ps1 rules D1/D2/S)."""
    return _sites(text, "detached")


def _unresolved_sites(text):
    """Executed strings built at run time, splats with no literal behind them, and unparsable sources (Test-LaunchSites.ps1 rule X)."""
    return _sites(text, "unresolved")


def _census_problems(sources, encoded_inventory, detached_inventory, kinds=("encoded", "detached", "unresolved"), unresolved_inventory=None):
    """Per-SITE comparison: inventory[path] is a list of (pin, reason), one entry per expected site, the pin a substring of the site text.
    A site no entry claims is unclassified; an entry no site claims has vanished. Two identical sites need two entries. An `unresolved`
    site (a string executed but built at run time) fails like an unclassified launch until UNRESOLVED_EXECUTED_SITES classifies it."""
    _scan_sites(sources.values())
    problems = []
    for kind, finder, inventory in (("encoded", _encoded_sites, encoded_inventory), ("detached", _detached_sites, detached_inventory),
                                    ("unresolved", _unresolved_sites, unresolved_inventory or {})):
        if kind not in kinds:
            continue
        for path in sorted(set(sources) | set(inventory)):
            left = list(finder(sources[path])) if path in sources else []
            for pin, _why in inventory.get(path, ()):
                hit = next((s for s in left if pin in s[1]), None)
                if hit is None:
                    problems.append(f"{kind}: {path}: classified site no longer found: {pin!r}")
                else:
                    left.remove(hit)
            problems += [f"{kind}: {path}:{n}: unclassified launch site: {ln.strip()[:140]}" for n, ln in left]
    return problems


_ENC_LAUNCH_A = "$args1 = @('-NoLogo', '-NoProfile', '-EncodedCommand', $encodedA)"
_ENC_LAUNCH_B = "$args2 = @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encodedB)"
_DET_LAUNCH_A = "Start-Process -FilePath $exeA -ArgumentList $listA -WindowStyle Hidden | Out-Null"
_DET_LAUNCH_B = "Start-Process -FilePath $exeB -ArgumentList $listB -WindowStyle Hidden | Out-Null"
_SYNTH = "tools/synthetic/Launch.ps1"


def test_launcher_census_second_encoded_launch_in_a_classified_file_is_unclassified():
    inventory = {_SYNTH: [(_ENC_LAUNCH_A, "classified")]}
    one = {_SYNTH: _ENC_LAUNCH_A + "\n"}
    assert _census_problems(one, inventory, {}) == [], "the fixture's one classified site must pass"
    two = {_SYNTH: _ENC_LAUNCH_A + "\n# between\n" + _ENC_LAUNCH_B + "\n"}
    assert _census_problems(two, inventory, {}), "a second -EncodedCommand launch added to a classified file slipped through the census"


def test_launcher_census_identical_second_encoded_launch_line_is_counted():
    inventory = {_SYNTH: [(_ENC_LAUNCH_A, "classified")]}
    twice = {_SYNTH: _ENC_LAUNCH_A + "\n" + _ENC_LAUNCH_A + "\n"}
    assert _census_problems(twice, inventory, {}), "a duplicated classified launch line slipped through the census"


def test_launcher_census_second_detached_launch_in_a_classified_file_is_unclassified():
    inventory = {_SYNTH: [(_DET_LAUNCH_A, "classified")]}
    assert _census_problems({_SYNTH: _DET_LAUNCH_A + "\n"}, {}, inventory) == []
    two = {_SYNTH: _DET_LAUNCH_A + "\n" + _DET_LAUNCH_B + "\n"}
    assert _census_problems(two, {}, inventory), "a second detached launch added to a classified file slipped through the census"


def test_launcher_census_detached_launch_split_over_backtick_continuation_lines_is_found():
    text = "$p = Start-Process -FilePath $exe -ArgumentList $list `\n    -PassThru -WindowStyle Hidden\n"
    assert len(_detached_sites(text)) == 1, "a hidden Start-Process split across continuation lines is not seen"


# --- r2 (Dot advisory 6390752487): the census scanner is statement- and quote-aware.
# B1: `;` separates statements, so `<pinned launch A>; <launch B>` on ONE line is two sites, not one.
# B2: `<#` / `#>` only open and close a block comment outside a string, so a quoted '<#' ... '#>' pair cannot hide the code between them.

def test_launcher_census_b1_same_line_semicolon_second_encoded_launch_is_unclassified():
    inventory = {_SYNTH: [(_ENC_LAUNCH_A, "classified")]}
    text = _ENC_LAUNCH_A + "; " + _ENC_LAUNCH_B + "\n"
    assert len(_encoded_sites(text)) == 2, "a `;`-separated second -EncodedCommand launch was folded into the first site"
    problems = _census_problems({_SYNTH: text}, inventory, {})
    assert problems and any("unclassified" in p for p in problems), "the pinned first launch consumed the unpinned second one on the same line"


def test_launcher_census_b1_same_line_semicolon_second_detached_launch_is_unclassified():
    inventory = {_SYNTH: [(_DET_LAUNCH_A, "classified")]}
    text = _DET_LAUNCH_A + "; " + _DET_LAUNCH_B + "\n"
    assert len(_detached_sites(text)) == 2, "a `;`-separated second detached launch was folded into the first site"
    problems = _census_problems({_SYNTH: text}, {}, inventory)
    assert problems and any("unclassified" in p for p in problems), "the pinned first launch consumed the unpinned second one on the same line"


def test_launcher_census_b2_quoted_block_comment_markers_cannot_hide_an_encoded_launch():
    text = "Write-Host '<#'\n" + _ENC_LAUNCH_B + "\nWrite-Host '#>'\n"
    assert len(_encoded_sites(text)) == 1, "a quoted '<#' ... '#>' pair hid the -EncodedCommand launch between them"
    assert _census_problems({_SYNTH: text}, {}, {}), "the census passes a file whose only launch sits between quoted comment markers"


def test_launcher_census_b2_quoted_block_comment_markers_cannot_hide_a_detached_launch():
    text = "Write-Host '<#'\n" + _DET_LAUNCH_B + "\nWrite-Host '#>'\n"
    assert len(_detached_sites(text)) == 1, "a quoted '<#' ... '#>' pair hid the detached launch between them"
    assert _census_problems({_SYNTH: text}, {}, {}), "the census passes a file whose only launch sits between quoted comment markers"


def test_launcher_census_b1_semicolon_inside_a_quoted_string_is_not_a_statement_split():
    first = "Start-Process -FilePath $exeA -ArgumentList 'x;y' -WindowStyle Hidden"
    sites = _detached_sites(first + "; " + _DET_LAUNCH_B + "\n")
    assert len(sites) == 2, "the `;` after the quoted argument was not treated as a statement separator"
    assert "'x;y'" in sites[0][1] and sites[0][1].endswith("-WindowStyle Hidden"), "a `;` inside a quoted string split its own statement"
    assert len(_detached_sites(first + "\n")) == 1, "a `;` inside a quoted string produced a false second site"


def test_launcher_census_b2_real_comments_and_comment_markers_inside_strings_and_here_strings():
    block = "<#\n" + _ENC_LAUNCH_A + "\n#>\n# " + _ENC_LAUNCH_B + "\n"
    assert _encoded_sites(block) == [], "a launch inside a real comment was counted"
    quoted = '$m = "see <# not a comment"\n' + _DET_LAUNCH_A + "\n"
    assert len(_detached_sites(quoted)) == 1, "a `<#` inside a double-quoted string swallowed the launch after it"
    here = "$h = @'\n<# not a comment in a here-string\n'@\n" + _DET_LAUNCH_B + "\n"
    assert len(_detached_sites(here)) == 1, "a `<#` inside a here-string swallowed the launch after it"
    trailing = _DET_LAUNCH_A + " # trailing comment; " + _DET_LAUNCH_B + "\n"
    assert len(_detached_sites(trailing)) == 1, "a launch inside a trailing # comment was counted"


@pytest.mark.parametrize("line", [
    "& pwsh -NoProfile -e " + B64,
    "& pwsh.exe -NoProfile -enc " + B64,
    "powershell.exe -NoProfile -ec " + B64,
    "pwsh -encodedc " + B64,
    "pwsh /EncodedCommand " + B64,
    "pwsh /enc " + B64,
    "pwsh /e " + B64,
    "$a = @('-NoProfile', '-enc', $encoded)",
    "$a = @('-NoProfile', '-e', $encoded)",
    "$a = @('-ec', $encoded)",
    "$cl = '\"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -NoProfile -EncodedCommand ' + $encoded",
])
def test_launcher_census_every_pwsh_spelling_of_the_encoded_switch_is_matched(line):
    assert len(_encoded_sites(line + "\n")) == 1, f"encoded-command spelling not matched: {line}"


@pytest.mark.parametrize("line", [
    "Get-Process -ErrorAction SilentlyContinue",
    "Get-ChildItem -Path $p -Exclude '*.log'",
    "if ($a -eq 1) { $b }",
    "Set-Content -Path $f -Encoding utf8 -Value $v",
    "Set-Content -Path $f -enc utf8 -Value $v",
    "# pwsh -NoProfile -e " + B64,
    "git cat-file -e abcdef1234",
    "$r = Invoke-Git @('cat-file', '-e', \"$sha^{commit}\")",
    "$msg = 'usage: tool -e " + B64 + "'",
    "Write-Host 'the -EncodedCommand switch is documented elsewhere'",
])
def test_launcher_census_look_alikes_of_the_encoded_switch_are_not_matched(line):
    assert _encoded_sites(line + "\n") == [], f"look-alike matched as an encoded-command launch: {line}"


def test_launcher_census_roots_cover_tracked_launch_sites_outside_the_old_roots():
    src = _census_sources()
    for p in ("tools/agent-bridge/wake_codex.ps1", "tools/closeout/start-closeout-dashboard.ps1"):
        assert p in src, f"{p} is a tracked launch site outside the census roots"
    assert "tools/coordination/Retire-LaneWorktree.ps1" not in src
    assert not [p for p in src if "/test" in p.lower() or Path(p).name.lower().startswith("test")], "test scripts must stay out of the census"


# --- r3 (sol r2 D-01, Dot 64dc38fd B1/B2, Grok 6390930513 G1/G2/G3, opus r2 H1/H3): the census reads sites from the PowerShell AST.
# Each fixture below is a launch that some hand-written tokenizer round hid. Two launches in ONE statement are two sites; a `$()` in a
# double-quoted string is code; a here-string run through [scriptblock]::Create is code, `;` inside its quotes included; an encoded switch
# on `& $exe` needs no pwsh word beside it; Start-Process takes -WindowStyle in its colon, prefix, alias and splatted forms.

_ENC_CMD_A = "& $exeA -NoProfile -EncodedCommand $encodedA"
_ENC_CMD_B = "& $exeB -NoProfile -EncodedCommand $encodedB"


def _second_site_is_unclassified(text, kind):
    """`text` holds two launches of `kind`; with only the A launch pinned, return (sites, census problems)."""
    pin = next(p for p in ((_ENC_LAUNCH_A, _ENC_CMD_A) if kind == "encoded" else (_DET_LAUNCH_A,)) if p in text)
    inv = {_SYNTH: [(pin, "classified")]}
    if kind == "encoded":
        return _encoded_sites(text), _census_problems({_SYNTH: text}, inv, {})
    return _detached_sites(text), _census_problems({_SYNTH: text}, {}, inv)


@pytest.mark.parametrize("kind", ["encoded", "detached"])
def test_launcher_census_r3_d01_same_line_if_else_branches_are_two_sites(kind):
    a, b = (_ENC_LAUNCH_A, _ENC_LAUNCH_B) if kind == "encoded" else (_DET_LAUNCH_A, _DET_LAUNCH_B)
    sites, problems = _second_site_is_unclassified("if ($x) { " + a + " } else { " + b + " }\n", kind)
    assert len(sites) == 2, f"two {kind} launches in one if/else statement were folded into one site: {sites}"
    assert any("unclassified" in p for p in problems), f"the pinned if-branch launch consumed the else-branch launch: {problems}"


@pytest.mark.parametrize("kind", ["encoded", "detached"])
def test_launcher_census_r3_d01_pipeline_chain_and_and_is_two_sites(kind):
    a, b = (_ENC_CMD_A, _ENC_CMD_B) if kind == "encoded" else (_DET_LAUNCH_A, _DET_LAUNCH_B)
    sites, problems = _second_site_is_unclassified(a + " && " + b + "\n", kind)
    assert len(sites) == 2, f"`A && B` folded two {kind} launches into one site: {sites}"
    assert any("unclassified" in p for p in problems), problems


def test_launcher_census_r3_d01_launch_inside_a_pipeline_scriptblock_is_its_own_site():
    text = _DET_LAUNCH_A + " | ForEach-Object { " + _DET_LAUNCH_B + " }\n"
    sites, problems = _second_site_is_unclassified(text, "detached")
    assert len(sites) == 2, f"`A | % {{ B }}` folded two detached launches into one site: {sites}"
    assert any("unclassified" in p for p in problems), problems


def test_launcher_census_r3_b1_subexpression_in_a_double_quoted_string_is_code():
    text = '$s = "$(' + _DET_LAUNCH_A + "; " + _DET_LAUNCH_B + ')"\n'
    sites, problems = _second_site_is_unclassified(text, "detached")
    assert len(sites) == 2, f"two launches inside $( ) in a double-quoted string were read as one opaque string: {sites}"
    assert any("unclassified" in p for p in problems), problems


def test_launcher_census_r3_b2_here_string_run_by_scriptblock_create_keeps_its_quoted_semicolon():
    text = ("$script = @'\nStart-Process -FilePath $exe -ArgumentList 'x;y' -WindowStyle Hidden\n'@\n"
            "& ([scriptblock]::Create($script))\n")
    sites = _detached_sites(text)
    assert len(sites) == 1 and "'x;y'" in sites[0][1], f"a quoted `;` inside a here-string split the hidden launch it carries: {sites}"
    assert sites[0][0] == 2, f"the inner launch is reported at its own line in the outer file: {sites}"
    assert _census_problems({_SYNTH: text}, {}, {}), "an executed here-string's hidden launch passed an empty inventory"


@pytest.mark.parametrize("sep", ["; ", "\n"])
def test_launcher_census_r3_g1_encoded_switch_on_amp_exe_after_get_command_pwsh(sep):
    text = "$exe = (Get-Command pwsh).Source" + sep + "& $exe -enc $b64\n"
    assert len(_encoded_sites(text)) == 1, f"`& $exe -enc` with $exe resolved from Get-Command pwsh was not a site (sep={sep!r})"
    assert _census_problems({_SYNTH: text}, {}, {}), "an unclassified encoded launch on `& $exe` passed the census"


def test_launcher_census_r3_g2_unquoted_long_switch_on_amp_exe_counts_alone():
    text = "& $exe -EncodedCommand $b64\n"
    assert len(_encoded_sites(text)) == 1, "an unquoted -EncodedCommand on `& $exe` with no pwsh word beside it was not a site"
    assert _census_problems({_SYNTH: text}, {}, {}), "an unclassified `& $exe -EncodedCommand` passed the census"


def test_launcher_census_r3_h3_quote_inside_subexpression_cannot_open_a_block_comment():
    text = '$m = "$(if ($x) { "it\'s" })"\nWrite-Host \'<#\'\n' + _ENC_LAUNCH_B + "\nWrite-Host '#>'\n"
    assert len(_encoded_sites(text)) == 1, "a quote inside $( ) desynchronised the scanner and a quoted '<#' hid the launch after it"
    assert _census_problems({_SYNTH: text}, {}, {}), "the census passes a file whose only launch sits behind a $( ) quote desync"


@pytest.mark.parametrize("line", [
    "Start-Process -FilePath $x -WindowStyle:Hidden",
    "Start-Process -FilePath $x -WindowStyle:'Hidden'",
    "Start-Process -FilePath $x -Win Hidden",
    "Start-Process -FilePath $x -windowst hidden",
    "saps -FilePath $x -WindowStyle Hidden",
    "start $x -WindowStyle Hidden",
    "Microsoft.PowerShell.Management\\Start-Process $x -WindowStyle Hidden",
    "Start-Process $x -WindowStyle ([System.Diagnostics.ProcessWindowStyle]::Hidden)",
    "$sp = @{ FilePath = $x; WindowStyle = 'Hidden' }\nStart-Process @sp",
    "$sp = @{ FilePath = $x }\n$sp.WindowStyle = 'Hidden'\nStart-Process @sp",
])
def test_launcher_census_r3_g3_every_form_of_a_hidden_start_process_is_a_site(line):
    assert len(_detached_sites(line + "\n")) == 1, f"hidden Start-Process form not seen: {line!r}"


@pytest.mark.parametrize("text", [
    "& ([scriptblock]::Create($prefix + $body))\n",
    "Invoke-Expression $cmd\n",
    "iex \"$a; $b\"\n",
    "param($cmd)\n$sb = [scriptblock]::Create($cmd)\n",
    "$cmd | Invoke-Expression\n",
    "& pwsh -NoProfile -Command $cmd\n",
])
def test_launcher_census_r3_an_executed_string_built_at_run_time_is_unresolved_and_fails_the_census(text):
    assert _census_problems({_SYNTH: text}, {}, {}), f"an executed string the census cannot resolve passed silently: {text!r}"


# Guards (pass on every scanner): the shapes r2's DETACHED_RX regex matched stay one site each, and the look-alikes stay zero.
@pytest.mark.parametrize("line", [
    "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cl }",
    "Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList $cl",
    "$r = ([wmiclass]'Win32_Process').Create($cl)",
    "$c = [wmiclass]'Win32_Process'",
])
def test_launcher_census_r3_win32_process_create_shapes_stay_one_site_each(line):
    assert len(_detached_sites(line + "\n")) == 1, f"Win32_Process Create shape not counted exactly once: {line!r}"


@pytest.mark.parametrize("text", [
    "Start-Process -FilePath $x -WindowStyle Normal\n",
    "Start-Process -FilePath $x -WindowStyle Minimized -PassThru\n",
    "Start-Process -FilePath $x -wi\n",
    "Start-Process -FilePath $x -NoNewWindow -Wait\n",
    "Write-Host 'Start-Process is forbidden here'\n",
    "& $winget install --id Microsoft.VisualStudio.2022.BuildTools -e --silent\n",
    "& $dumpbin /EXPORTS $dll\n",
])
def test_launcher_census_r3_look_alikes_of_a_launch_are_not_sites(text):
    assert _detached_sites(text) == [] and _encoded_sites(text) == [], f"look-alike counted as a launch site: {text!r}"


# --- r4 (Dot advisory 6391702961 B1, hub ACK 6392004927): a variable resolves to a literal only through assignments in the scope where
# it is read. r3 kept one file-wide assignment map, so a parameter, a foreach variable or a caller's variable that shares a name with an
# unrelated literal was read as that literal and its executed string passed as resolved.

def test_launcher_census_r4_b1_dot_parameter_shadowing_an_outer_literal_is_unresolved():
    text = ("$cmd='Write-Output ok'; function Run-Code { param($cmd) iex $cmd }; "
            "Run-Code ('Start-' + 'Process -FilePath pwsh -WindowStyle Hidden')\n")
    sites = _unresolved_sites(text)
    assert [ln for _, ln in sites] == ["iex $cmd"], f"the parameter $cmd was resolved from the unrelated outer literal: {sites}"
    assert _census_problems({_SYNTH: text}, {}, {}), "Dot's static counterexample passed an empty inventory"


@pytest.mark.parametrize("text", [
    "$cmd = 'Write-Output ok'\nfunction Run-Code($cmd) { iex $cmd }\n",
    "$cmd = 'Write-Output ok'\n& { param($cmd) iex $cmd } $built\n",
    "$cmd = 'Write-Output ok'\nforeach ($cmd in $list) { iex $cmd }\n",
    "$cmd = 'Write-Output ok'\n$cmd, $rest = $parts\niex $cmd\n",
    "$cmd = 'Write-Output ok'\nGet-Thing -OutVariable cmd\niex $cmd\n",
    "$cmd = 'Write-Output ok'\n$sb = [scriptblock]::Create($global:cmd)\n",
    "$sp = @{ FilePath = 'x' }\nfunction Start-It { param($sp) Start-Process @sp }\n",
])
def test_launcher_census_r4_b1_a_name_bound_by_something_other_than_a_literal_is_unresolved(text):
    assert _unresolved_sites(text), f"a parameter / loop / multi-assignment / -OutVariable binding was read as the outer literal: {text!r}"


# Nested scope (stated choice: conservatively unresolved). A { } body or a function reads a variable it never assigns from whatever scope
# calls it at run time, so the outer literal is not evidence of the value.
@pytest.mark.parametrize("text", [
    "$cmd = 'Write-Output ok'\n& { iex $cmd }\n",
    "$cmd = 'Write-Output ok'\nfunction Run-Code { iex $cmd }\n",
])
def test_launcher_census_r4_b1_a_nested_scope_reading_an_outer_literal_is_unresolved(text):
    assert _unresolved_sites(text), f"a nested scope read an outer literal as its own: {text!r}"


# Dot-sourced script reading a caller variable: a value the file does not definitely assign before the read comes from the caller.
@pytest.mark.parametrize("text", [
    "iex $cmd\n",
    "if ($Force) { $cmd = 'Write-Output ok' }\niex $cmd\n",
    "iex $cmd\n$cmd = 'Write-Output ok'\n",
])
def test_launcher_census_r4_b1_a_caller_variable_read_by_a_dot_sourced_script_is_unresolved(text):
    assert _unresolved_sites(text), f"a variable the file does not assign before reading it was resolved: {text!r}"


# Guards (pass on every scanner): a literal assigned in the scope that reads it still resolves, so these stay site-free.
@pytest.mark.parametrize("text", [
    "$cmd = 'Write-Output ok'\niex $cmd\n",
    "function Run-Code { $c = 'Write-Output ok'; iex $c }\n",
    "$a = 'Write-Output ok'\n$b = $a\niex $b\n",
    "$script:T = 'Write-Output ok'\nfunction Run-Code { iex $script:T }\n",
    "$sp = @{ FilePath = 'x' }\nStart-Process @sp\n",
])
def test_launcher_census_r4_a_literal_assigned_in_the_reading_scope_still_resolves(text):
    assert _unresolved_sites(text) == [], f"a same-scope literal was reported unresolved: {text!r}"


# --- r5 (Dot advisory 6392164637 items 1-3, Grok CoS ask 6392177927 G1, hub ACK 6392234100): rule V still read a safe literal where
# pwsh rebinds the name some other way: a scope-qualified write re-homed to the lexical scope, a later literal borrowed by an earlier
# $script: read, Set-Variable's name taken from -Scope's value, and writes through the variable: drive, PSVariable or a Get-Variable
# result. Every form below was run in pwsh with a harmless payload and does change what `iex $cmd` runs. Each must report the iex site.
_R5_BUILT = "('Start-' + 'Process -FilePath pwsh -WindowStyle Hidden')"


def _r5(writer):
    return "$cmd='Write-Output ok'; " + writer + "; iex $cmd\n"


def _r5_iex_is_unresolved(text):
    sites = _unresolved_sites(text)
    assert [ln for _, ln in sites if ln.startswith("iex $")], f"the executed $cmd was resolved to the safe literal: {text!r} -> {sites}"
    assert _census_problems({_SYNTH: text}, {}, {}), f"the counterexample passed an empty inventory: {text!r}"


@pytest.mark.parametrize("text", [
    pytest.param("$cmd='Start-'+'Process -FilePath pwsh -WindowStyle Hidden'; function Run-Code { $global:cmd='Write-Output ok'; iex $cmd }; "
                 "Run-Code\n", id="dot1-global-write-in-a-function"),
    pytest.param("iex $script:cmd; $script:cmd='Write-Output ok'\n", id="dot2-later-script-literal"),
    pytest.param("$cmd='Write-Output ok'; Set-Variable -Scope Local cmd ('Start-'+'Process -FilePath pwsh -WindowStyle Hidden'); iex $cmd\n",
                 id="dot3-set-variable-scope-then-positional-name"),
])
def test_launcher_census_r5_dot_qualified_write_ordering_and_set_variable_binding(text):
    _r5_iex_is_unresolved(text)


@pytest.mark.parametrize("writer", [
    pytest.param("Set-Item -Path variable:cmd -Value " + _R5_BUILT, id="g1a-set-item"),
    pytest.param("New-Item -Path variable:cmd -Value " + _R5_BUILT + " -Force", id="g1b-new-item"),
    pytest.param("${variable:cmd} = " + _R5_BUILT, id="g1c-braced-variable-drive"),
    pytest.param("$ExecutionContext.SessionState.PSVariable.Set('cmd', " + _R5_BUILT + ")", id="g1d-psvariable-set"),
    pytest.param("(Get-Variable cmd).Value = " + _R5_BUILT, id="g1e-get-variable-value"),
])
def test_launcher_census_r5_g1_variable_drive_and_psvariable_writers(writer):
    _r5_iex_is_unresolved(_r5(writer))


# Further forms of the same classes (lane-found, each run in pwsh): spellings of the variable: path, the other item cmdlets, a path or a
# PSVariable held in a variable, the provider API, aliases, and a removal that lets a function read its caller's value.
@pytest.mark.parametrize("writer", [
    pytest.param("Set-Item variable:\\cmd " + _R5_BUILT, id="variable-drive-backslash"),
    pytest.param("Set-Item -LiteralPath 'Microsoft.PowerShell.Core\\Variable::cmd' -Value " + _R5_BUILT, id="provider-qualified-path"),
    pytest.param("$variable:cmd = " + _R5_BUILT, id="dollar-variable-drive"),
    pytest.param("$other = " + _R5_BUILT + "; Clear-Item variable:cmd; Rename-Item variable:other cmd -Force", id="rename-item"),
    pytest.param("$other = " + _R5_BUILT + "; Copy-Item variable:other variable:cmd -Force", id="copy-item"),
    pytest.param("Set-Content variable:cmd " + _R5_BUILT, id="set-content"),
    pytest.param("$p = 'variable:cmd'; Set-Item $p " + _R5_BUILT, id="path-held-in-a-variable"),
    pytest.param("$pv = $ExecutionContext.SessionState.PSVariable; $pv.Set('cmd', " + _R5_BUILT + ")", id="psvariable-held-in-a-variable"),
    pytest.param("$v = Get-Variable cmd; $v.Value = " + _R5_BUILT, id="get-variable-result-held-in-a-variable"),
    pytest.param("(Get-Item variable:cmd).Value = " + _R5_BUILT, id="get-item-variable-value"),
    pytest.param("$ExecutionContext.SessionState.InvokeProvider.Item.Set('variable:cmd', " + _R5_BUILT + ")", id="invoke-provider"),
    pytest.param("New-Variable -Force -Scope Local cmd " + _R5_BUILT, id="new-variable-switch-and-scope-first"),
    pytest.param("Set-Variable -Scope 0 -Option None cmd " + _R5_BUILT, id="set-variable-two-named-first"),
    pytest.param("set cmd " + _R5_BUILT, id="set-alias"),
])
def test_launcher_census_r5_more_writers_that_rebind_the_name(writer):
    _r5_iex_is_unresolved(_r5(writer))


@pytest.mark.parametrize("text", [
    # a function that removes its own $cmd reads its caller's
    pytest.param("$cmd = " + _R5_BUILT + "; function F { $cmd='Write-Output ok'; Remove-Variable cmd; iex $cmd }; F\n", id="remove-variable-fallback"),
    # dot-sourced into a function, the file's top level is that function's scope, not the script scope
    pytest.param("$cmd='Write-Output ok'; iex $script:cmd\n", id="script-read-after-an-unqualified-literal"),
    pytest.param("$script:cmd='Write-Output ok'; iex $cmd\n", id="unqualified-read-after-a-script-literal"),
])
def test_launcher_census_r5_scope_crossing_reads_are_unresolved(text):
    _r5_iex_is_unresolved(text)


# Code that runs in this scope from text, from another file, or under another name writes names too (lane-found, each run in pwsh).
@pytest.mark.parametrize("text", [
    pytest.param("$cmd='Write-Output ok'; . ./lib.ps1; iex $cmd\n", id="dot-sourced-file"),
    pytest.param("$sb = { $cmd = $env:PAYLOAD }; $cmd='Write-Output ok'; . $sb; iex $cmd\n", id="dot-sourced-variable"),
    pytest.param("$cmd='Write-Output ok'; iex '$cmd = $env:PAYLOAD'; iex $cmd\n", id="iex-literal-that-assigns"),
    pytest.param("$script:cmd='Write-Output ok'; & ([scriptblock]::Create('$script:cmd = $env:PAYLOAD')); iex $script:cmd\n",
                 id="scriptblock-create-literal-that-assigns"),
    pytest.param("$c = 'Set-Variable'; $cmd='Write-Output ok'; & $c cmd $env:PAYLOAD; iex $cmd\n", id="dynamic-call-of-a-named-cmdlet"),
    pytest.param("$cmd='Write-Output ok'; & (Get-Command sv) cmd $env:PAYLOAD; iex $cmd\n", id="call-of-get-command"),
    pytest.param("Set-Alias w Set-Variable; $cmd='Write-Output ok'; w cmd $env:PAYLOAD; iex $cmd\n", id="alias-defined-here"),
    pytest.param("$cmd='Write-Output ok'; Push-Location variable:; Set-Item cmd $env:PAYLOAD; Pop-Location; iex $cmd\n", id="location-in-variable-drive"),
])
def test_launcher_census_r5_code_run_in_this_scope_can_rebind_the_name(text):
    _r5_iex_is_unresolved(text)


# Controls (pass on every scanner): the two writers rule V already saw stay one site, and a safe literal with no other writer of its name
# (another name bound by Set-Variable -Scope, or a local:/private: write in the reading scope) stays resolved.
@pytest.mark.parametrize("writer", ["$cmd = " + _R5_BUILT, "Set-Variable cmd " + _R5_BUILT])
def test_launcher_census_r5_control_writers_stay_one_site(writer):
    sites = _unresolved_sites(_r5(writer))
    assert [ln for _, ln in sites] == ["iex $cmd"], f"a writer rule V already saw is no longer exactly one site: {sites}"


@pytest.mark.parametrize("text", [
    "$cmd='Write-Output ok'; iex $cmd\n",
    "$cmd='Write-Output ok'; Set-Variable -Scope Local other " + _R5_BUILT + "; iex $cmd\n",
    "$local:cmd='Write-Output ok'; iex $cmd\n",
    "function Run-Code { $private:c = 'Write-Output ok'; iex $c }\n",
    "$script:cmd='Write-Output ok'; iex $script:cmd\n",
    "$cmd='Write-Output ok'; Remove-Item -LiteralPath 'C:\\tmp\\x.txt' -Force; iex $cmd\n",
    "$cmd='Write-Output ok'; $p = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath('x'); iex $cmd\n",
    "$cmd='Write-Output ok'; iex 'Write-Output hi'; iex $cmd\n",
    "$cmd='Write-Output ok'; . { $other = 1 }; iex $cmd\n",
])
def test_launcher_census_r5_control_a_safe_same_scope_literal_stays_resolved(text):
    assert _unresolved_sites(text) == [], f"a safe same-scope literal with no other writer was reported unresolved: {text!r}"


# --- r7 (Dot advisory 6392963508, Grok CoS ask 6392967676, hub ruling HUB-TICK 20261025T0526Z): rule V is closed by an allowlist
# (Test-LaunchSites.ps1 header, V3). One fixture per allowlist boundary, not one per known writer. The payload is a benign marker that
# is not a constant: the only question is whether `$cmd` still resolves to its literal. Parameter coverage is proven by the metadata
# meta-test in tools/repo_hygiene/test_launch_site_census_binders.py, which runs on the hosted Windows and ubuntu shards.
_R7_MARKER = "$env:CENSUS_FIXTURE_MARKER"


def _r7(binder):
    return "$cmd = 'Write-Output ok'; " + binder + "; iex $cmd\n"


@pytest.mark.parametrize("binder", [
    pytest.param(_R7_MARKER + " | Tee-Object -Variable cmd", id="i-variable-named-parameter"),
    pytest.param("$m = 'Value'; (Get-Variable cmd).$m = " + _R7_MARKER, id="ii-dynamic-member-name"),
    pytest.param("(Get-Variable cmd -ValueOnly:$false).Value = " + _R7_MARKER, id="iii-valueonly-not-true"),
    pytest.param(". { param($cmd) } " + _R7_MARKER, id="iv-dot-sourced-block-param"),
])
def test_launcher_census_r7_a_binder_off_the_allowlist_leaves_the_read_unresolved(binder):
    _r5_iex_is_unresolved(_r7(binder))


# Controls: the writers rule V always saw stay one site, and binders that are on the allowlist (a param of a block run with `&` or of a
# function that is not dot-sourced, -ValueOnly given as a switch or :$true, a constant member name, a parameter that binds no variable)
# leave a safe same-scope literal resolved.
@pytest.mark.parametrize("binder", ["$cmd = " + _R7_MARKER, "Set-Variable cmd " + _R7_MARKER])
def test_launcher_census_r7_control_writers_stay_one_site(binder):
    sites = _unresolved_sites(_r7(binder))
    assert [ln for _, ln in sites] == ["iex $cmd"], f"a writer rule V already saw is no longer exactly one site: {sites}"


@pytest.mark.parametrize("binder", [
    pytest.param("& { param($cmd) } " + _R7_MARKER, id="amp-block-param"),
    pytest.param("function Use-It { param($cmd) }; Use-It " + _R7_MARKER, id="function-param"),
    pytest.param("$o = Get-Variable other -ValueOnly; $p = Get-Variable more -ValueOnly:$true", id="valueonly-switch-and-true"),
    pytest.param("$o = @{ a = 1 }; $o.a = " + _R7_MARKER, id="constant-member-name"),
    pytest.param(_R7_MARKER + " | Write-Output -InputObject cmd", id="parameter-binding-no-variable"),
])
def test_launcher_census_r7_control_an_allowlisted_binder_keeps_the_literal(binder):
    assert _unresolved_sites(_r7(binder)) == [], f"an allowlisted binder made a safe same-scope literal unresolved: {binder!r}"


# --- r8 (Dot advisory 6394696125, hub ruling HUB-TICK 20261025T0706Z): splat sources join the rule V allowlist. A splat `@h` has a
# known key set only when every use of `h` in the file is on the allowlist (a constant-key hashtable literal assigned in the reading
# scope before the splat; .Add / indexer / member writes with a constant key). Any other use makes the key set unknown, and an unknown
# splat taints every name. Same benign marker as r7; each shape was run in pwsh and does rebind `$cmd` (run dir proof\r8-runtime-*.ps1).
def _r8(mutation, splat=_R7_MARKER + " | Tee-Object @h"):
    return "$cmd = 'Write-Output ok'; $h = @{}; $key = " + _R7_MARKER + "; " + mutation + "; " + splat + "; iex $cmd\n"


@pytest.mark.parametrize("mutation", [
    pytest.param("$h.Add($key, 'cmd')", id="i-add-with-a-dynamic-key"),
    pytest.param("$h[$key] = 'cmd'", id="ii-indexer-with-a-dynamic-key"),
    pytest.param("function Fill($t, $k) { $t.Add($k, 'cmd') }; Fill $h $key", id="iii-passed-to-a-function"),
    pytest.param("$g = $h; $g.Add($key, 'cmd')", id="aliased-by-assignment"),
    pytest.param("$h | ForEach-Object { $_.Add($key, 'cmd') }", id="piped-into-a-command"),
    pytest.param("$h.set_Item($key, 'cmd')", id="set-item-with-a-dynamic-key"),
    pytest.param("$h.psbase.Add($key, 'cmd')", id="reached-through-a-member"),
    pytest.param("$r = [ref]$h; $r.Value.Add($key, 'cmd')", id="ref"),
])
def test_launcher_census_r8_a_splat_source_off_the_allowlist_leaves_the_read_unresolved(mutation):
    _r5_iex_is_unresolved(_r8(mutation))


# Controls: a constant-key literal with no *Variable* key, and constant-key .Add / indexer / member writes, keep the splat's key set
# known, so a safe same-scope literal stays resolved.
@pytest.mark.parametrize("mutation", [
    pytest.param("$h = @{ FilePath = 'x.txt' }", id="constant-key-literal"),
    pytest.param("$h.Add('Encoding', 'utf8')", id="add-constant-key"),
    pytest.param("$h['Encoding'] = 'utf8'", id="indexer-constant-key"),
    pytest.param("$h.Encoding = 'utf8'", id="member-constant-key"),
])
def test_launcher_census_r8_control_an_allowlisted_splat_source_keeps_the_literal(mutation):
    text = _r8(mutation, splat=_R7_MARKER + " | Out-File -FilePath x.txt @h")
    assert _unresolved_sites(text) == [], f"an allowlisted splat source made a safe same-scope literal unresolved: {text!r}"


# --- r9 (CoS/Dot 6397324853 on #669, hub ACK 6397373319; LAUNCH-SITE-CENSUS-PSDEFAULTPARAMETERVALUES-BINDER-1): a default written to
# $PSDefaultParameterValues binds every later command that has the parameter, with no splat in the file. Before r9 its *Variable* keys
# were read only in a file with a splat. Shapes 1 and 2 were run in pwsh and do rebind `$cmd` (run dir proof\runtime-witness.txt); there
# exact names, aliases (`*:ov`) and a parameter wildcard that matches one parameter (`*:OutVa*`) bind, a parameter prefix does not.
def _r9(write, use=_R7_MARKER + " | Tee-Object"):
    return "$cmd = 'Write-Output ok'; $key = " + _R7_MARKER + "; " + write + "; " + use + "; iex $cmd\n"


@pytest.mark.parametrize("write,use", [
    pytest.param("$PSDefaultParameterValues = @{'Tee-Object:Variable' = 'cmd'}", _R7_MARKER + " | Tee-Object", id="1-literal-hashtable"),
    pytest.param("$PSDefaultParameterValues['*:OutVariable'] = 'cmd'", _R7_MARKER + " | Write-Output", id="2-indexer-wildcard-command"),
    pytest.param("$PSDefaultParameterValues.Add('Tee-Object:Variable', 'cmd')", _R7_MARKER + " | Tee-Object", id="3-add"),
    pytest.param("$PSDefaultParameterValues[$key] = 'cmd'", _R7_MARKER + " | Write-Output", id="4-non-constant-key"),
    pytest.param("$PSDefaultParameterValues.set_Item('*:ov', 'cmd')", _R7_MARKER + " | Write-Output", id="set-item-alias"),
    pytest.param("$PSDefaultParameterValues.'*:OutVa*' = 'cmd'", _R7_MARKER + " | Write-Output", id="member-parameter-wildcard"),
    pytest.param("$PSDefaultParameterValues += @{'*:OutVariable' = 'cmd'}", _R7_MARKER + " | Write-Output", id="plus-equals-literal"),
    pytest.param("$global:PSDefaultParameterValues['Tee-Object:Variable'] = 'cmd'", _R7_MARKER + " | Tee-Object", id="global-qualified"),
    pytest.param("$PSDefaultParameterValues.Add($key, 'cmd')", _R7_MARKER + " | Write-Output", id="add-non-constant-key"),
    pytest.param("$d = $PSDefaultParameterValues; $d['*:OutVariable'] = 'cmd'", _R7_MARKER + " | Write-Output", id="aliased-by-assignment"),
    pytest.param("Set-Variable PSDefaultParameterValues @{'*:OutVariable' = 'cmd'}", _R7_MARKER + " | Write-Output", id="set-variable-by-name"),
])
def test_launcher_census_r9_a_default_parameter_binder_leaves_the_read_unresolved(write, use):
    _r5_iex_is_unresolved(_r9(write, use))


def test_launcher_census_r9_a_default_parameter_binder_leaves_a_scriptblock_create_unresolved():
    # -OutVariable binds an ArrayList, which iex's [string] -Command refuses at run time; [scriptblock]::Create takes it as its text
    text = ("$cmd = 'Write-Output ok'; $PSDefaultParameterValues['*:OutVariable'] = 'cmd'; " + _R7_MARKER + " | Write-Output; "
            "& ([scriptblock]::Create($cmd))\n")
    assert [ln for _, ln in _unresolved_sites(text) if "Create($cmd)" in ln], f"the created $cmd was resolved to the safe literal: {text!r}"


# Controls: a default whose key names no *Variable* parameter, and reads and removals of $PSDefaultParameterValues, bind nothing, so a
# safe same-scope literal stays resolved with no splat in the file.
@pytest.mark.parametrize("write", [
    pytest.param("$PSDefaultParameterValues['*:Encoding'] = 'utf8'", id="indexer-encoding"),
    pytest.param("$PSDefaultParameterValues = @{'Out-File:Encoding' = 'utf8'; 'Disabled' = $false}", id="literal-no-binder-key"),
    pytest.param("$PSDefaultParameterValues.Add('*:Encoding', 'utf8')", id="add-encoding"),
    pytest.param("$global:PSDefaultParameterValues['*:Encoding'] = 'utf8'", id="global-encoding"),
    pytest.param("$PSDefaultParameterValues.Remove('*:Encoding'); $n = $PSDefaultParameterValues.Count", id="remove-and-count"),
])
def test_launcher_census_r9_control_a_non_binder_default_keeps_the_literal(write):
    text = _r9(write, use=_R7_MARKER + " | Out-File -FilePath x.txt")
    assert _unresolved_sites(text) == [], f"a default that binds no variable made a safe same-scope literal unresolved: {text!r}"


# --- r9 round 2 (sol r1 BLOCKER and Fable r1 items 1-2 on #678, CoS dot B1, hub ACK 6398138185): a compound write (`+=`) to a
# $PSDefaultParameterValues entry builds a name its right side does not hold, and an assignment of the table whose value another name takes
# (`$x = ($PSDefaultParameterValues = @{})`) aliases it. Each shape was run in pwsh and does rebind `$cmd` (r2 run dir
# proof\runtime-witness-r2.txt). Catalogue row S-06 is the first case.
@pytest.mark.parametrize("write,use", [
    pytest.param("$PSDefaultParameterValues = @{'Tee-Object:Variable' = 'cm'}; $PSDefaultParameterValues['Tee-Object:Variable'] += 'd'",
                 _R7_MARKER + " | Tee-Object", id="s06-index-plus-equals"),
    pytest.param("$PSDefaultParameterValues = @{'Tee-Object:Variable' = 'cm'}; $PSDefaultParameterValues.'Tee-Object:Variable' += 'd'",
                 _R7_MARKER + " | Tee-Object", id="s06-member-plus-equals"),
    pytest.param("$PSDefaultParameterValues['*:OutVariable'] = 'c'; $PSDefaultParameterValues['*:OutVariable'] += 'md'",
                 _R7_MARKER + " | Write-Output", id="index-write-then-plus-equals"),
    pytest.param("$x = ($PSDefaultParameterValues = @{}); $x['Tee-Object:Variable'] = 'cmd'", _R7_MARKER + " | Tee-Object",
                 id="chained-assignment-alias"),
    pytest.param("$x = ($PSDefaultParameterValues = @{}); $x['*:OutVariable'] = 'cmd'", _R7_MARKER + " | Write-Output",
                 id="chained-assignment-alias-outvariable"),
    pytest.param("$x = $PSDefaultParameterValues = @{}; $x['Tee-Object:Variable'] = 'cmd'", _R7_MARKER + " | Tee-Object",
                 id="chained-assignment-alias-no-parens"),
])
def test_launcher_census_r9_round2_a_compound_or_aliasing_default_write_leaves_the_read_unresolved(write, use):
    _r5_iex_is_unresolved(_r9(write, use))


# Policy (hub ACK 6398138185): the census does not model the value a compound operator leaves, so any operator other than `=` on an entry
# taints every name, whatever its right side. `??= 'other'` does not rebind `$cmd` at run time (witness c-null-coalesce-other); it is
# reported unresolved on purpose.
@pytest.mark.parametrize("write", [
    pytest.param("$PSDefaultParameterValues = @{}; $PSDefaultParameterValues['Tee-Object:Variable'] ??= 'other'", id="index-null-coalescing"),
    pytest.param("$PSDefaultParameterValues = @{}; $PSDefaultParameterValues.'Tee-Object:Variable' ??= 'other'", id="member-null-coalescing"),
])
def test_launcher_census_r9_round2_any_compound_entry_write_taints_every_name(write):
    _r5_iex_is_unresolved(_r9(write))


# Controls: a plain `=` entry write whose literal names another variable binds only that name, and a table assignment in a statement block
# (its value taken by no one) is still read as a literal, so a safe same-scope literal stays resolved.
@pytest.mark.parametrize("write", [
    pytest.param("$PSDefaultParameterValues = @{}; $PSDefaultParameterValues['Tee-Object:Variable'] = 'other'", id="index-plain-other-name"),
    pytest.param("$PSDefaultParameterValues = @{}; $PSDefaultParameterValues.'Tee-Object:Variable' = 'other'", id="member-plain-other-name"),
    pytest.param("if ($true) { $PSDefaultParameterValues = @{'Tee-Object:Variable' = 'other'} }", id="statement-block-assignment"),
])
def test_launcher_census_r9_round2_control_a_plain_entry_write_keeps_the_literal(write):
    text = _r9(write)
    assert _unresolved_sites(text) == [], f"a plain default naming another variable made a safe same-scope literal unresolved: {text!r}"
