"""LANE-RECEIPT-SCRATCH-CLEANUP-1: bulk scratch a lane registers in <RunDir>\\scratch-paths.txt is copied
(small text receipts only) and removed when the lane exits; unsafe paths are refused and recorded.

Every path lives under a pytest tmp root; nothing here touches the real C:\\mlvtmp.
"""
import importlib.util, json, os, shutil, subprocess, sys, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HELPER = ROOT / "tools" / "coordination" / "Remove-LaneBulkScratch.ps1"
REGISTER = ROOT / "tools" / "coordination" / "Register-LaneBulkScratch.ps1"
RETIRE = ROOT / "tools" / "coordination" / "Retire-LaneWorktree.ps1"
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


def call_helper(w, paths, pr="none", keep=False, work=None, workdir_in=None, extra="", timeout=120, real_snapshot=False):
    """Dot-source the helper in a fresh pwsh and return its disposition as a dict.

    pr=None leaves the PR probe at its default (real `gh`, or the -GhPath seam in `extra`).
    The process snapshot is an empty injected one unless `extra` brings its own or real_snapshot=True: a real scan on a busy
    host sees other lanes' short-lived compilers with unreadable cwds, which the fail-closed guard (rightly) cannot rule out."""
    if not real_snapshot and "-SnapshotProvider" not in extra:
        extra = (extra + " " + provider()).strip()
    (w["run"] / "scratch-paths.txt").write_text("\n".join(str(p) for p in paths) + "\n", encoding="utf-8")
    driver = w["tmp"] / "driver.ps1"
    probe = "-PrProbe { '%s' } " % pr if pr is not None else ""
    driver.write_text(
        ". '%s'\n$d = Invoke-RemoveLaneBulkScratch -RunDir '%s' -WorkDir '%s' -Root '%s' -LaneScratchRoot '%s' "
        "%s%s %s\n$d | ConvertTo-Json -Depth 6\n"
        % (HELPER, w["run"], work or w["work"], w["bulk"], w["scratch"], probe, "-Keep" if keep else "", extra),
        encoding="ascii")
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver)],
                       capture_output=True, text=True, timeout=timeout)
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
    """paths=None: the manifest is NOT pre-written (the fake agent registers through the helper itself)."""
    root = fixture_tree["root"]
    cmd, env, receipt = prepare(fixture_tree, "normal")
    make_repo(root)
    bulk = tmp_path.with_name(tmp_path.name + "-bulk"); bulk.mkdir()
    if paths is not None:
        for p in paths(bulk):
            p.mkdir(parents=True, exist_ok=True); (p / "n.txt").write_text("note")
        (root / "run" / "scratch-paths.txt").write_text("\n".join(str(p) for p in paths(bulk)) + "\n", encoding="utf-8")
    if with_helper:
        (root / "Remove-LaneBulkScratch.ps1").write_bytes(HELPER.read_bytes())
        (root / "repo-slug.ps1").write_bytes((HELPER.parent / "repo-slug.ps1").read_bytes())
        # The fixture copy only: a real Win32_Process scan on a busy host sees other lanes' short-lived compilers with
        # unreadable cwds, which the fail-closed holder guard cannot rule out. The real scan is exercised by the live-cwd row.
        (root / "Retire-LaneWorktree.ps1").write_bytes(RETIRE.read_bytes() +
            b"\r\nfunction Get-LaneProcessSnapshot { [pscustomobject]@{ Procs = @(); SelfPids = @($PID); CapturedUtc = (Get-Date).ToUniversalTime() } }\r\n")
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


# ---- r2: registration, live-holder guard, bounds ----

def register(w, path, root=None, run=None):
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(REGISTER), "-RunDir",
                        str(run if run is not None else w["run"]), "-Path", str(path), "-Root", str(root or w["bulk"])],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout.strip()


def manifest_lines(w):
    f = w["run"] / "scratch-paths.txt"
    return [x for x in f.read_text(encoding="utf-8").splitlines() if x] if f.exists() else []


def test_register_helper_appends_and_dedupes(world):
    a = world["bulk"] / "cpg1-a-asm"
    assert register(world, a) == (0, "REGISTERED path=%s" % a)
    assert register(world, str(a) + "\\")[0] == 0 and register(world, str(a).upper())[1].startswith("ALREADY-REGISTERED")
    assert register(world, world["bulk"] / "b")[1].startswith("REGISTERED")
    assert manifest_lines(world) == [str(a), str(world["bulk"] / "b")]


def test_register_helper_refuses_unsafe_paths_and_leaves_the_manifest_alone(world):
    outside = world["tmp"] / "outside"
    for path, reason in [("relative\\dir", "relative-path"), (outside, "outside-root"), (world["bulk"], "outside-root"),
                         ("%s\\a\\..\\x" % world["bulk"], "dotdot")]:
        code, out = register(world, path)
        assert code == 2 and out.startswith("REFUSED reason=%s " % reason), (path, code, out)
    code, out = register(world, world["bulk"] / "ok", run=world["tmp"] / "no-such-run")
    assert code == 2 and out.startswith("REFUSED reason=run-dir-missing"), out
    assert manifest_lines(world) == []


def test_register_helper_defaults_to_the_real_mlvtmp_root(world):
    # No -Root: the default is C:\mlvtmp, so a path elsewhere on the disk is refused (the pytest tmp itself may sit under it).
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(REGISTER), "-RunDir", str(world["run"]),
                        "-Path", "C:\\Users\\Public\\not-mlvtmp"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "reason=outside-root" in r.stdout
    assert manifest_lines(world) == []


def test_invoke_lane_exports_the_manifest_and_a_registered_dir_is_cleaned_at_exit(fixture_tree, tmp_path):
    root = fixture_tree["root"]
    cmd, env, receipt, bulk = lane(fixture_tree, tmp_path, None)
    try:
        (root / "Register-LaneBulkScratch.ps1").write_bytes(REGISTER.read_bytes())
        pre = root / "pre.ps1"
        pre.write_text(
            "if($env:MLV_LANE_SCRATCH_MANIFEST){\n"
            "  $d='%s\\reg-asm'; New-Item -ItemType Directory -Force -Path $d | Out-Null; 'note' | Set-Content \"$d\\n.txt\"\n"
            "  \"$env:MLV_LANE_RUN_DIR|$env:MLV_LANE_SCRATCH_MANIFEST\" | Set-Content '%s\\seen-env.txt'\n"
            "  & '%s' -RunDir $env:MLV_LANE_RUN_DIR -Path $d -Root '%s' | Set-Content '%s\\register-out.txt'\n}\n"
            % (bulk, root, root / "Register-LaneBulkScratch.ps1", bulk, root), encoding="ascii")
        fixture_tree["shim"].write_text('@echo off\r\n"%s" -NoProfile -NonInteractive -File "%s"\r\n"%s" -NoProfile -NonInteractive -File "%s" %%*\r\n'
                                        % (PWSH, pre, PWSH, fixture_tree["child"]), encoding="ascii")
        r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=180)
        assert r.returncode == 0, (r.stdout, r.stderr)
        seen = (root / "seen-env.txt").read_text().strip()
        assert seen == "%s|%s" % (root / "run", root / "run" / "scratch-paths.txt"), seen
        q = json.loads(receipt.read_text(encoding="utf-8"))
        assert [e["action"] for e in q["bulkScratch"]["entries"]] == ["removed"], q["bulkScratch"]
        assert not (bulk / "reg-asm").exists()
        assert (root / "run" / "scratch-receipts" / "1-reg-asm" / "n.txt").read_text().strip() == "note"
    finally:
        shutil.rmtree(bulk, ignore_errors=True)


def test_a_live_process_holding_the_dir_keeps_it(world):
    asm = world["bulk"] / "cpg1-held-asm"; asm.mkdir(); (asm / "a.txt").write_text("a")
    ready = world["tmp"] / "ready.txt"
    p = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                          "Set-Content -LiteralPath '%s' -Value up; Start-Sleep -Seconds 300" % ready], cwd=str(asm))
    try:
        for _ in range(300):
            if ready.exists(): break
            time.sleep(0.1)
        assert ready.exists()
        d = call_helper(world, [asm], real_snapshot=True)
        e = entry(d)
        assert e["action"] == "kept" and e["reason"] == "live-process", e
        assert any("via=cwd" in h for h in e["holders"]), e
        assert (asm / "a.txt").exists() and not (world["run"] / "scratch-receipts").exists()
    finally:
        p.kill(); p.wait()


def test_a_holder_probe_that_cannot_answer_refuses(world):
    asm = world["bulk"] / "asm"; asm.mkdir(); (asm / "a.txt").write_text("a")
    d = call_helper(world, [asm], extra="-SnapshotProvider { throw 'scan-failed' }")
    e = entry(d)
    assert e["action"] == "refused" and e["reason"] == "holder-probe-failed" and "scan-failed" in e["error"], e
    assert (asm / "a.txt").exists()


def test_a_stalled_pr_probe_is_bounded_and_refuses(world):
    git("checkout", "-q", "-B", "feat", cwd=world["work"])   # a branch, so the probe really asks gh
    fake = world["tmp"] / "fake-gh.cmd"
    fake.write_text("@echo off\r\nping -n 60 127.0.0.1 >nul\r\n", encoding="ascii")
    asm = world["bulk"] / "asm"; asm.mkdir(); (asm / "a.txt").write_text("a")
    t0 = time.time()
    d = call_helper(world, [asm], pr=None, extra="-GhPath '%s' -PrTimeoutSeconds 2" % fake, timeout=50)
    assert time.time() - t0 < 40
    e = entry(d)
    assert e["action"] == "refused" and e["reason"] == "pr-state-unknown" and e["error"] == "gh-timeout", e
    assert d["error"] is None and (asm / "a.txt").exists()


def test_more_than_200_entries_report_the_dropped_count(world):
    d = call_helper(world, [world["bulk"] / ("p%03d" % i) for i in range(201)])
    assert len(d["entries"]) == 200 and d["truncated"] == 1
    d = call_helper(world, [world["bulk"] / ("p%03d" % i) for i in range(5)])
    assert d["truncated"] == 0


def test_a_failing_size_walk_is_walk_failed_not_remove_failed(world):
    asm = world["bulk"] / "asm"; sub = asm / "sub"; sub.mkdir(parents=True); (asm / "a.txt").write_text("a")
    user = os.environ.get("USERNAME", "")
    r = subprocess.run(["icacls", str(sub), "/deny", "%s:(RD)" % user], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        d = call_helper(world, [asm])
        e = entry(d)
        assert e["action"] == "failed" and e["reason"] == "walk-failed", e
        assert asm.exists()
    finally:
        subprocess.run(["icacls", str(sub), "/reset"], capture_output=True, text=True)


# ---- r3: the holder guard fails closed on unresolved or stale process evidence ----

def provider(procs="@()", unknown="@()", pre=""):
    """A -SnapshotProvider scriptblock argument: a snapshot with fixed rows and a pre-built holder index (no real scan)."""
    return ("-SnapshotProvider { " + pre + " $idx = [pscustomobject]@{ Cwd = @{}; Scripts = @(); ScriptsByPid = @{}; CwdProbe = 'ok'; "
            "CwdUnknown = " + unknown + " }; [pscustomobject]@{ Procs = " + procs + "; SelfPids = @($PID); "
            "CapturedUtc = (Get-Date).ToUniversalTime(); HolderIndex = $idx } }")


def proc_row(pid, cmdline, name="pwsh.exe"):
    return "[pscustomobject]@{ ProcessId = %d; Name = '%s'; CommandLine = '%s'; CreationDate = (Get-Date) }" % (pid, name, cmdline)


def unknown_row(pid, created):
    return "[pscustomobject]@{ ProcessId = %d; Name = 'mystery.exe'; Why = 'cwd-unreadable'; CreatedUtc = %s }" % (pid, created)


def make_asm(w, name="cpg1-r3-asm"):
    asm = w["bulk"] / name; asm.mkdir(); (asm / "a.txt").write_text("a")
    return asm


def test_a_live_process_with_an_unreadable_cwd_keeps_the_dir(world):
    asm = make_asm(world)
    later = "(Get-Date).ToUniversalTime().AddHours(1)"
    d = call_helper(world, [asm], extra=provider(unknown="@(%s)" % unknown_row(999001, later)))
    e = entry(d)
    assert e["action"] == "kept" and e["reason"] == "holder-cwd-unknown", e
    assert any("999001" in h and "cwd-unknown" in h for h in e["holders"]), e
    assert (asm / "a.txt").exists() and not (world["run"] / "scratch-receipts").exists()


def test_an_unreadable_cwd_of_a_process_older_than_the_dir_is_exempt_as_in_worktree_retirement(world):
    asm = make_asm(world)
    earlier = "(Get-Date).ToUniversalTime().AddDays(-1)"
    d = call_helper(world, [asm], extra=provider(unknown="@(%s)" % unknown_row(999001, earlier)))
    assert entry(d)["action"] == "removed", entry(d)


def test_a_holder_that_starts_during_the_pr_probe_is_seen(world):
    asm = make_asm(world)
    flag = world["tmp"] / "pr-probe-ran.flag"
    late = proc_row(999002, "pwsh -File build.ps1 -Out %s" % asm)
    extra = ("-PrProbe { Set-Content -LiteralPath '%s' -Value up; 'none' } " % flag +
             provider(procs="@(if (Test-Path -LiteralPath '%s') { %s })" % (flag, late)))
    d = call_helper(world, [asm], pr=None, extra=extra)
    e = entry(d)
    assert e["action"] == "kept" and e["reason"] == "live-process", e
    assert any("999002" in h and "via=cmdline" in h for h in e["holders"]), e
    assert (asm / "a.txt").exists()


def test_a_live_process_naming_the_run_dir_holds_every_registered_path(world):
    asm = make_asm(world)
    chain = proc_row(999003, "pwsh -File %s\\tools\\run-chain.ps1 -HeadManifest abc" % world["run"])
    d = call_helper(world, [asm], extra=provider(procs="@(%s)" % chain))
    e = entry(d)
    assert e["action"] == "kept" and e["reason"] == "run-dir-active", e
    assert any("999003" in h for h in e["holders"]), e
    assert (asm / "a.txt").exists() and not (world["run"] / "scratch-receipts").exists()


def test_a_probe_that_failed_once_is_not_retried_for_the_remaining_entries(world):
    dirs = [make_asm(world, "asm%02d" % i) for i in range(50)]
    count = world["tmp"] / "probe-attempts.txt"
    d = call_helper(world, dirs, extra=provider(pre="Add-Content -LiteralPath '%s' -Value x; throw 'scan-failed';" % count))
    assert len(d["entries"]) == 50
    assert {(e["action"], e["reason"]) for e in d["entries"]} == {("refused", "holder-probe-failed")}
    assert all("scan-failed" in e["error"] for e in d["entries"])
    assert len(count.read_text().split()) == 1
    assert all((x / "a.txt").exists() for x in dirs)


def test_a_failed_git_branch_lookup_is_unknown_not_no_pr(world):
    driver = world["tmp"] / "branch-driver.ps1"
    driver.write_text(". '%s'\nGet-LaneScratchPrState '%s' 'gh-never-called' 5\n" % (HELPER, world["work"]), encoding="ascii")
    env = dict(os.environ, GIT_DIR=str(world["tmp"] / "no-such-git-dir"))   # every git call exits 128
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver)], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.stdout.strip() == "unknown:branch-lookup-failed", (r.stdout, r.stderr)


def test_two_case_spellings_of_one_path_are_one_entry(world):
    asm = make_asm(world, "cpg1-case-asm")
    d = call_helper(world, [asm, str(asm).upper(), str(asm).lower() + "\\"])
    assert len(d["entries"]) == 1 and entry(d)["action"] == "removed", d["entries"]
    assert d["truncated"] == 0
