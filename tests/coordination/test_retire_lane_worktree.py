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


def _gate_real(workdir, *, whatif=True, prelude=""):
    """Gate with the REAL process snapshot. Returns (disposition, stderr). `prelude` runs after the helper is dot-sourced."""
    quarantine = Path(workdir).parent / "q"
    script = (
        "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
        f". '{HELPER}'; {prelude} "
        f"Invoke-RetireLaneWorktree -WorkDir '{workdir}' -MergeTarget 'origin/master' -QuarantineRoot '{quarantine}'"
        f"{' -WhatIf' if whatif else ''} | ConvertTo-Json -Depth 4"
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


def _fake_row(pid_expr, cmd="shell.exe", session=1, name="shell.exe", self_pids="@(1)"):
    return ("[pscustomobject]@{ Procs = @([pscustomobject]@{ ProcessId = " + pid_expr + "; Name = '" + name + "'; "
            "CommandLine = '" + cmd + "'; SessionId = " + str(session) + " }); SelfPids = " + self_pids + " }")


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


def test_a_pid_whose_cwd_read_fails_is_refused_as_cwd_unknown(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-cwd-unknown")
    p = _spawn(["-Command", "Start-Sleep -Seconds 120"], cwd=tmp)   # does not hold the worktree, but the gate cannot tell
    try:
        d, err = _gate_real(wt, prelude=_cwd_unreadable_for(p.pid))
        control, _ = _gate_real(wt)                                  # same process, cwd readable: no refusal for it
    finally:
        _stop(p)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown"), d
    assert d["cwdProbe"] == "ok" and any(u.startswith(f"{p.pid} ") for u in d["cwdUnknown"]), d
    assert f"pid={p.pid}" in err and "via=cwd-unknown" in err, err
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


def test_snapshot_user_session_process_with_unreadable_cwd_is_cwd_unknown(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-sess1")
    d = _gate_with_snapshot(wt, _fake_row("$PID"), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert d["action"] == "kept" and d["reason"].startswith("cwd-unknown"), d


def test_snapshot_session0_process_with_unreadable_cwd_is_not_a_candidate(repo):
    tmp, main = repo
    wt = _add_wt(main, tmp / "wt-sess0")
    d = _gate_with_snapshot(wt, _fake_row("$PID", session=0), prelude=CWD_UNREADABLE_FOR_EVERY_PID)
    assert (d["action"], d["reason"]) == ("would-retire", "ok"), d


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
    # control: the same session-0 process without a relative script path is not a candidate
    ctl = _gate_with_snapshot(wt, _fake_row("$PID", cmd="pwsh.exe -Command Start-Sleep", session=0, name="pwsh.exe"),
                              prelude=CWD_UNREADABLE_FOR_EVERY_PID)
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
