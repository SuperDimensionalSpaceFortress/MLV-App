"""LANE-HOOK-DENY-RECURRENCE-1: the scratch dir Invoke-Lane.ps1 gives a lane is a path the lane's own hook admits.

The child's TEMP/TMP/TMPDIR is the dir the harness names to the lane as its scratchpad. Through fork/master
caf2c612 it was C:\\mlvtmp\\lane-scratch\\<run>-<lane>, outside both roots tools/hooks/mlv-never-authorized.py
NA-7 admits (the hook's own worktree and the board), so every scratchpad write was denied. These tests run a
fixture lane, read the TEMP it really got, and ask the hook's own decide() -- loaded from the lane's WorkDir,
exactly as the registered `${CLAUDE_PROJECT_DIR}/tools/hooks/...` command loads it -- about a Write there.
"""
import importlib.util, json, os, shutil, subprocess, sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HOOK = ROOT / "tools" / "hooks" / "mlv-never-authorized.py"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract / pwsh")
PWSH = "pwsh.exe"

_spec = importlib.util.spec_from_file_location("lane_containment_fixtures", HERE / "test_lane_containment.py")
_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lc)
fixture_tree = _lc.fixture_tree
prepare = _lc.prepare
# RED runs only: MLV_TEST_INVOKE_LANE=<path> runs these tests against another Invoke-Lane.ps1 (e.g. an older one).
if os.environ.get("MLV_TEST_INVOKE_LANE"):
    _lc.CANDIDATE = Path(os.environ["MLV_TEST_INVOKE_LANE"])
LAUNCHER = _lc.CANDIDATE


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True, timeout=60)


def run_lane(tree, extra=()):
    """A fixture lane whose WorkDir is a git repo carrying THIS repo's .gitignore. Its first act records
    TEMP/TMP/TMPDIR, writes a file into TEMP, and records what `git status` then sees."""
    root = tree["root"]
    cmd, env, receipt = prepare(tree, "normal")
    shutil.copyfile(ROOT / ".gitignore", root / ".gitignore")
    git("init", "-q", cwd=root)
    git("add", ".gitignore", cwd=root)
    git("commit", "-q", "-m", "x", cwd=root)
    git("checkout", "-q", "--detach", cwd=root)   # no branch -> no PR lookup, no network
    seen = root / "seen-temp.json"
    pre = root / "pre.ps1"
    pre.write_text(
        "$f = Join-Path $env:TEMP 'claude\\scratchpad\\note.txt'\n"
        "New-Item -ItemType Directory -Force -Path (Split-Path $f) | Out-Null\n"
        "'note' | Set-Content -LiteralPath $f\n"
        "$st = @(git -C '%s' -c core.quotepath=false status --porcelain -uall)\n"
        "[ordered]@{ TEMP = $env:TEMP; TMP = $env:TMP; TMPDIR = $env:TMPDIR; note = $f; status = $st } | "
        "ConvertTo-Json | Set-Content -LiteralPath '%s' -Encoding utf8NoBOM\n" % (root, seen), encoding="ascii")
    tree["shim"].write_text('@echo off\r\n"%s" -NoProfile -NonInteractive -File "%s"\r\n"%s" -NoProfile -NonInteractive -File "%s" %%*\r\n'
                            % (PWSH, pre, PWSH, tree["child"]), encoding="ascii")
    r = subprocess.run(cmd + list(extra), env=env, text=True, capture_output=True, timeout=180)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(seen.read_text(encoding="utf-8-sig")), json.loads(receipt.read_text(encoding="utf-8"))


def hook_decide(worktree, board, monkeypatch, tool, tool_input):
    """The hook's own decide(), from a copy at <worktree>/tools/hooks/ so its NA-7 worktree root IS <worktree>."""
    copy = Path(worktree) / "tools" / "hooks" / "mlv-never-authorized.py"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(HOOK.read_bytes())
    spec = importlib.util.spec_from_file_location("_mlv_never_authorized_scratch_probe", copy)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv("MLV_BOARD_ROOT", str(board))   # a board that is NOT the worktree, as on the real host
    code, line = hook.decide({"tool_name": tool, "tool_input": tool_input}, str(worktree))
    return code == hook.EXIT_ALLOW, line


def test_the_lane_scratchpad_is_a_path_its_hook_admits_and_git_does_not_see(fixture_tree, tmp_path, monkeypatch):
    seen, receipt = run_lane(fixture_tree)
    root = fixture_tree["root"]
    temp = seen["TEMP"]
    assert seen["TMP"] == temp and seen["TMPDIR"] == temp, seen
    board = tmp_path.with_name(tmp_path.name + "-board"); board.mkdir()
    allowed, line = hook_decide(root, board, monkeypatch, "Write", {"file_path": seen["note"], "content": "x"})
    assert allowed, "the scratchpad the lane was given is denied by its own hook: %s (TEMP=%s)" % (line, temp)
    allowed, line = hook_decide(root, board, monkeypatch, "Edit", {"file_path": seen["note"], "old_string": "note", "new_string": "n"})
    assert allowed, "a shrinking Edit of the lane's own scratch file is denied: %s" % line
    # Inside the lane's own WorkDir, invisible to git while the lane runs, and gone when it exits.
    assert os.path.commonpath([os.path.normcase(temp), os.path.normcase(str(root))]) == os.path.normcase(str(root)), temp
    assert not [s for s in seen["status"] or [] if ".claude-state" in s or "lane-scratch" in s], seen["status"]
    assert receipt["scratch"]["path"] == temp and receipt["scratch"]["removed"] is True and receipt["scratch"]["error"] is None, receipt["scratch"]
    assert receipt["scratch"]["bytes"] > 0
    assert not Path(temp).exists()


def test_an_explicit_scratch_root_still_wins_and_keep_scratch_still_keeps(fixture_tree, tmp_path):
    alt = tmp_path.with_name(tmp_path.name + "-alt") / "lane-scratch"
    try:
        seen, receipt = run_lane(fixture_tree, ["-ScratchRoot", str(alt), "-KeepScratch"])
        assert Path(seen["TEMP"]).parent == alt, seen
        assert receipt["scratch"]["path"] == seen["TEMP"] and receipt["scratch"]["removed"] is False, receipt["scratch"]
        assert Path(seen["note"]).is_file()
    finally:
        shutil.rmtree(alt.parent, ignore_errors=True)


def test_this_repo_ignores_the_worktree_scratch_root():
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", ".claude-state/lane-scratch/run-sonnet-001/claude/note.txt"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "the worktree-local lane scratch root is not git-ignored here: %r" % (r.stderr,)


def test_bulk_scratch_cleanup_still_refuses_the_shared_lane_scratch_root():
    # The manifest cleanup is handed $ScratchRoot, whose declared value is still the shared root, so a lane that
    # registers C:\mlvtmp\lane-scratch (other lanes' TEMP) is refused exactly as before this change.
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "[string]$ScratchRoot = 'C:\\mlvtmp\\lane-scratch'," in text
    assert "-LaneScratchRoot $ScratchRoot" in text
