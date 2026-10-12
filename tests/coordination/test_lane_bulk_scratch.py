"""LANE-RECEIPT-SCRATCH-CLEANUP-1: bulk scratch a lane registers in <RunDir>\\scratch-paths.txt is copied
(small text receipts only) and removed when the lane exits; unsafe paths are refused and recorded.

Every path lives under a pytest tmp root; nothing here touches the real C:\\mlvtmp.
"""
import importlib.util, json, os, shutil, subprocess, sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HELPER = ROOT / "tools" / "coordination" / "Remove-LaneBulkScratch.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions / pwsh")
PWSH = "pwsh.exe"

_spec = importlib.util.spec_from_file_location("lane_containment_fixtures", HERE / "test_lane_containment.py")
_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lc)
fixture_tree = _lc.fixture_tree
prepare = _lc.prepare


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                   capture_output=True, text=True, timeout=60)


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=path)
    git("checkout", "-q", "--detach", cwd=path)   # no branch -> no PR lookup, no network
    return path


def junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout


@pytest.fixture
def world(tmp_path):
    w = {"work": make_repo(tmp_path / "work"), "bulk": tmp_path / "bulk", "run": tmp_path / "run", "tmp": tmp_path}
    w["bulk"].mkdir(); w["run"].mkdir()
    w["scratch"] = w["bulk"] / "lane-scratch"
    return w


def call_helper(w, paths, pr="none", keep=False, work=None, workdir_in=None):
    """Dot-source the helper in a fresh pwsh and return its disposition as a dict."""
    (w["run"] / "scratch-paths.txt").write_text("\n".join(str(p) for p in paths) + "\n", encoding="utf-8")
    driver = w["tmp"] / "driver.ps1"
    driver.write_text(
        ". '%s'\n$d = Invoke-RemoveLaneBulkScratch -RunDir '%s' -WorkDir '%s' -Root '%s' -LaneScratchRoot '%s' "
        "-PrProbe { '%s' } %s\n$d | ConvertTo-Json -Depth 6\n"
        % (HELPER, w["run"], work or w["work"], w["bulk"], w["scratch"], pr, "-Keep" if keep else ""),
        encoding="ascii")
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(r.stdout)


def entry(d, i=0):
    return d["entries"][i]


def test_registered_dir_is_removed_after_small_text_files_are_copied(world):
    asm = world["bulk"] / "cpg1-x-asm"
    (asm / "sub").mkdir(parents=True)
    (asm / "a.txt").write_text("alpha"); (asm / "sub" / "b.json").write_text("{}"); (asm / "c.log").write_text("log")
    (asm / "big.log").write_bytes(b"x" * (2 * 1024 * 1024)); (asm / "tool.exe").write_bytes(b"MZ")
    d = call_helper(world, [asm])
    e = entry(d)
    assert e["action"] == "removed" and e["reason"] is None, e
    assert e["bytes"] == len("alpha") + 2 + 3 + 2 * 1024 * 1024 + 2
    assert not asm.exists()
    copied = world["run"] / "scratch-receipts" / "1-cpg1-x-asm"
    assert (copied / "a.txt").read_text() == "alpha" and (copied / "sub" / "b.json").read_text() == "{}"
    assert (copied / "c.log").exists()
    assert not (copied / "big.log").exists() and not (copied / "tool.exe").exists()
    assert e["copiedFiles"] == 3


def test_missing_path_is_recorded_absent_and_no_manifest_returns_null(world):
    d = call_helper(world, [world["bulk"] / "gone"])
    assert entry(d)["action"] == "absent"
    (world["run"] / "scratch-paths.txt").unlink()
    driver = world["tmp"] / "driver2.ps1"
    driver.write_text(". '%s'\n$d = Invoke-RemoveLaneBulkScratch -RunDir '%s' -WorkDir '%s' -Root '%s'\nif ($null -eq $d) { 'NULL' }\n"
                      % (HELPER, world["run"], world["work"], world["bulk"]), encoding="ascii")
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(driver)], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "NULL", (r.stdout, r.stderr)


def test_refuses_path_outside_root(world):
    outside = world["tmp"] / "outside"; outside.mkdir(); (outside / "f.txt").write_text("keep")
    d = call_helper(world, [outside, world["bulk"]])
    assert [e["reason"] for e in d["entries"]] == ["outside-root", "outside-root"]
    assert (outside / "f.txt").exists() and world["bulk"].exists()


@pytest.mark.parametrize("raw,reason", [("relative\\dir", "relative-path"), ("{bulk}\\a\\..\\..\\x", "dotdot"), ("{bulk}\\LANE-R~1", "short-name-alias")])
def test_refuses_non_plain_absolute_paths(world, raw, reason):
    d = call_helper(world, [raw.format(bulk=world["bulk"])])
    assert entry(d)["action"] == "refused" and entry(d)["reason"] == reason


def test_refuses_the_lane_scratch_root_and_anything_inside_it(world):
    inner = world["scratch"] / "sonnet-001"; inner.mkdir(parents=True); (inner / "t.txt").write_text("x")
    d = call_helper(world, [world["scratch"], inner])
    assert [e["reason"] for e in d["entries"]] == ["lane-scratch", "lane-scratch"]
    assert (inner / "t.txt").exists()


def test_refuses_the_lanes_own_workdir(world):
    work = make_repo(world["bulk"] / "lane-work")
    (work / "keep.txt").write_text("k")
    sub = work / "sub"; sub.mkdir()
    d = call_helper(world, [work, sub, world["bulk"] / "lane-work"], work=work)
    assert [e["reason"] for e in d["entries"]][:2] == ["workdir", "workdir"]
    assert (work / "keep.txt").exists() and sub.exists()


def test_refuses_a_git_worktree_and_a_dir_that_contains_one(world):
    wt = world["bulk"] / "lane-other-wt"
    git("worktree", "add", "-q", "--detach", str(wt), cwd=world["work"])
    holder = world["bulk"] / "holder"; holder.mkdir()
    git("worktree", "add", "-q", "--detach", str(holder / "nested"), cwd=world["work"])
    marker = world["bulk"] / "has-dot-git"; (marker / ".git").mkdir(parents=True)
    d = call_helper(world, [wt, holder, marker])
    assert [e["reason"] for e in d["entries"]] == ["git-worktree"] * 3
    assert wt.exists() and (holder / "nested").exists() and marker.exists()


def test_never_follows_a_junction(world):
    target = world["tmp"] / "precious"; (target / "child").mkdir(parents=True); (target / "p.txt").write_text("precious")
    link = world["bulk"] / "link"; junction(link, target)
    holder = world["bulk"] / "holder"; holder.mkdir(); (holder / "f.txt").write_text("x")
    junction(holder / "inner-link", target)
    under = world["bulk"] / "underlink-parent"; junction(under, target)
    d = call_helper(world, [link, holder, under / "child"])
    assert [e["reason"] for e in d["entries"]] == ["reparse-point", "reparse-point", "reparse-point"]
    assert (target / "p.txt").read_text() == "precious" and (holder / "f.txt").exists()


@pytest.mark.parametrize("pr,reason", [("open", "producer-pr-open"), ("unknown", "pr-state-unknown")])
def test_refuses_when_the_producer_pr_is_open_or_unknown(world, pr, reason):
    asm = world["bulk"] / "asm"; asm.mkdir(); (asm / "a.txt").write_text("a")
    d = call_helper(world, [asm], pr=pr)
    assert entry(d)["action"] == "refused" and entry(d)["reason"] == reason
    assert asm.exists()


def test_opt_out_keeps_everything_and_says_so(world):
    asm = world["bulk"] / "asm"; asm.mkdir(); (asm / "a.txt").write_text("a")
    d = call_helper(world, [asm], keep=True)
    assert d["optOut"] is True and entry(d)["action"] == "kept" and entry(d)["reason"] == "opt-out"
    assert (asm / "a.txt").exists() and not (world["run"] / "scratch-receipts").exists()


# ---- through Invoke-Lane.ps1: the receipt carries the outcome and the lane's exit/state never change ----

def lane(fixture_tree, tmp_path, paths, extra=(), with_helper=True, mutate=None):
    root = fixture_tree["root"]
    cmd, env, receipt = prepare(fixture_tree, "normal")
    make_repo(root)
    bulk = tmp_path.with_name(tmp_path.name + "-bulk"); bulk.mkdir()
    for p in paths(bulk):
        p.mkdir(parents=True, exist_ok=True); (p / "n.txt").write_text("note")
    (root / "run" / "scratch-paths.txt").write_text("\n".join(str(p) for p in paths(bulk)) + "\n", encoding="utf-8")
    if with_helper:
        (root / "Remove-LaneBulkScratch.ps1").write_bytes(HELPER.read_bytes())
    cmd += ["-BulkScratchRoot", str(bulk), "-ScratchRoot", str(bulk / "lane-scratch"), *extra]
    if mutate: mutate(bulk)
    return cmd, env, receipt, bulk


def test_invoke_lane_removes_registered_scratch_and_records_it(fixture_tree, tmp_path):
    cmd, env, receipt, bulk = lane(fixture_tree, tmp_path, lambda b: [b / "cpg1-asm"])
    try:
        r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=120)
        assert r.returncode == 0, (r.stdout, r.stderr)
        q = json.loads(receipt.read_text(encoding="utf-8"))
        e = q["bulkScratch"]["entries"][0]
        assert e["action"] == "removed" and q["bulkScratch"]["error"] is None
        assert not (bulk / "cpg1-asm").exists()
        assert (fixture_tree["root"] / "run" / "scratch-receipts" / "1-cpg1-asm" / "n.txt").read_text() == "note"
        assert q["complete"] is True and q["exitCode"] == 0
    finally:
        shutil.rmtree(bulk, ignore_errors=True)


def test_invoke_lane_keep_bulk_scratch_switch_is_honoured(fixture_tree, tmp_path):
    cmd, env, receipt, bulk = lane(fixture_tree, tmp_path, lambda b: [b / "cpg1-asm"], extra=["-KeepBulkScratch"])
    try:
        r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=120)
        assert r.returncode == 0, (r.stdout, r.stderr)
        q = json.loads(receipt.read_text(encoding="utf-8"))
        assert q["bulkScratch"]["optOut"] is True and q["bulkScratch"]["entries"][0]["action"] == "kept"
        assert (bulk / "cpg1-asm" / "n.txt").exists()
    finally:
        shutil.rmtree(bulk, ignore_errors=True)


def test_invoke_lane_without_a_manifest_leaves_the_receipt_key_null(fixture_tree):
    cmd, env, receipt = prepare(fixture_tree, "normal")
    r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert json.loads(receipt.read_text(encoding="utf-8"))["bulkScratch"] is None


def test_cleanup_failure_never_changes_the_lane_exit_or_receipt_state(fixture_tree, tmp_path):
    held = {}
    def hold(bulk):
        # Python opens files without FILE_SHARE_DELETE, so Remove-Item on this tree hits a sharing violation.
        held["fh"] = open(bulk / "locked-asm" / "n.txt", "r")
    cmd, env, receipt, bulk = lane(fixture_tree, tmp_path, lambda b: [b / "locked-asm"], mutate=hold)
    try:
        r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=120)
        assert r.returncode == 0, (r.stdout, r.stderr)
        q = json.loads(receipt.read_text(encoding="utf-8"))
        assert q["bulkScratch"]["entries"][0]["action"] == "failed"
        assert q["bulkScratch"]["entries"][0]["reason"] == "remove-failed"
        assert q["complete"] is True and q["exitCode"] == 0 and q["failure"] is None
    finally:
        held["fh"].close()
        shutil.rmtree(bulk, ignore_errors=True)


def test_missing_helper_is_recorded_and_never_blocks_the_receipt(fixture_tree, tmp_path):
    cmd, env, receipt, bulk = lane(fixture_tree, tmp_path, lambda b: [b / "asm"], with_helper=False)
    try:
        r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=120)
        assert r.returncode == 0, (r.stdout, r.stderr)
        q = json.loads(receipt.read_text(encoding="utf-8"))
        assert q["bulkScratch"]["error"].startswith("cannot-determine")
        assert q["complete"] is True and q["exitCode"] == 0
        assert (bulk / "asm").exists()
    finally:
        shutil.rmtree(bulk, ignore_errors=True)
