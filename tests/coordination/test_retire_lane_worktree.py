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
