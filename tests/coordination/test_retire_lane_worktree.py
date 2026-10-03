"""Falsifier tests for tools/coordination/Retire-LaneWorktree.ps1 (SAFE-gate worktree retirement).

Each case builds a throwaway repository with a bare remote, so nothing depends on this
checkout's refs. Every case asserts both the action AND the reason prefix, so a gate that
passes for the wrong reason fails the test.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "tools" / "coordination" / "Retire-LaneWorktree.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(not (PWSH and GIT), reason="requires pwsh and git")


def _git(cwd, *args):
    subprocess.run([GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
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
        f". '{HELPER}'; "
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
        f". '{HELPER}'; "
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

def _gate_with_snapshot(workdir, snapshot_expr):
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; "
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
