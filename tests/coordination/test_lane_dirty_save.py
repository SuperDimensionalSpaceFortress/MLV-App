"""LANE-DIRTY-SAVE-CORE-1: tools/coordination/lane-dirty-save.ps1 keeps a dead lane's uncommitted work.
Exit codes: 0 saved, 10 nothing to save, 20 lane alive (refused), 30 bad arguments, 40 git failed."""
import datetime, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "coordination" / "lane-dirty-save.ps1"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
BINARY = bytes(range(256)) * 4


def git(wd, *args):
    return subprocess.run(["git", "-C", str(wd), *args], capture_output=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    wd = tmp_path / "wt"
    wd.mkdir()
    (tmp_path / "run").mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["config", "core.autocrlf", "false"]):
        git(wd, *a)
    (wd / "a.txt").write_bytes(b"one\r\ntwo\nthree\n")
    (wd / "b.bin").write_bytes(BINARY)
    git(wd, "add", ".")
    git(wd, "commit", "-q", "-m", "base")
    return wd


def dirty(wd):
    (wd / "a.txt").write_bytes(b"one\r\nTWO\nthree\nfour\xff\n")
    (wd / "b.bin").write_bytes(BINARY[::-1])
    (wd / "staged.txt").write_text("staged\n")
    git(wd, "add", "staged.txt")
    (wd / "untracked.txt").write_text("u\n")


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def save(wd, *extra, env=None):
    args = [PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(wd), "-RunDir", str(wd.parent / "run"), *extra]
    return subprocess.run(args, capture_output=True, text=True, env=env)


def saved(wd):
    return sorted(p.name for p in (wd.parent / "run").iterdir())


def test_dead_lane_patch_is_byte_exact_applies_cleanly_and_untracked_is_listed(repo, tmp_path):
    dirty(repo)
    before = (git(repo, "status", "--porcelain"), git(repo, "diff", "HEAD", "--binary"))
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    head8 = git(repo, "rev-parse", "HEAD").decode().strip()[:8]
    patch, listed = (tmp_path / "run" / f"dirty-save-{head8}.{e}" for e in ("patch", "untracked.txt"))
    assert patch.read_bytes() == git(repo, "diff", "HEAD", "--binary")
    assert listed.read_bytes() == git(repo, "ls-files", "-z", "--others", "--exclude-standard")  # NUL-separated (CoS G1 on #722)
    assert listed.read_bytes() == b"untracked.txt\0"
    assert json.loads(r.stdout.strip().splitlines()[-1])["patch"] == patch.name
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(repo), str(clone)], check=True, capture_output=True)
    git(clone, "apply", "--binary", str(patch))
    for name in ("a.txt", "b.bin", "staged.txt"):
        assert (clone / name).read_bytes() == (repo / name).read_bytes()
    assert before == (git(repo, "status", "--porcelain"), git(repo, "diff", "HEAD", "--binary"))  # tree unchanged
    assert git(repo, "stash", "list") == b""


def test_live_pid_is_refused_and_nothing_is_written(repo):
    dirty(repo)
    r = save(repo, "-LanePid", str(os.getpid()))
    assert r.returncode == 20, r.stdout + r.stderr
    assert saved(repo) == []


def test_child_json_names_the_lane_and_a_reused_pid_counts_as_dead(repo, tmp_path):
    dirty(repo)
    now, child = datetime.datetime.now(datetime.timezone.utc), tmp_path / "child.json"
    child.write_text(json.dumps({"pid": os.getpid(), "createdUtc": (now + datetime.timedelta(minutes=1)).isoformat()}))
    assert save(repo, "-ChildJson", str(child)).returncode == 20
    assert saved(repo) == []
    child.write_text(json.dumps({"pid": os.getpid(), "createdUtc": (now - datetime.timedelta(days=1)).isoformat()}))
    assert save(repo, "-ChildJson", str(child)).returncode == 0
    assert saved(repo) != []


def test_a_live_explicit_pid_is_never_judged_by_another_pids_created_utc(repo, tmp_path):  # dot B1 on #722
    dirty(repo)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        child = tmp_path / "child.json"
        child.write_text(json.dumps({"pid": dead_pid(), "createdUtc": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()}))
        r = save(repo, "-LanePid", str(sleeper.pid), "-ChildJson", str(child))
        assert r.returncode in (20, 30), r.stdout + r.stderr
        assert saved(repo) == []
    finally:
        sleeper.kill()


def test_a_non_ascii_untracked_name_is_listed_raw_not_octal_quoted(repo, tmp_path):  # dot CI-B3 on #722 (DG-GIT-PATHLIST)
    (repo / "café.txt").write_text("u\n")
    assert save(repo, "-LanePid", str(dead_pid())).returncode == 0
    assert "café.txt\0".encode() in next((tmp_path / "run").glob("*.untracked.txt")).read_bytes()


@pytest.mark.skipif(sys.platform == "win32", reason='NTFS forbids " TAB and LF in a file name; the NUL-format assertion in the first test is the Windows case')
def test_names_git_would_c_quote_are_listed_byte_exact(repo, tmp_path):  # CoS G1 on #722: core.quotepath=false does not stop quoting of ", \, TAB, LF
    names = ['a"b.txt', "tab\there.txt", "new\nline.txt", "back\\slash.txt"]
    for n in names:
        (repo / n).write_text("u\n")
    assert save(repo, "-LanePid", str(dead_pid())).returncode == 0
    listed = next((tmp_path / "run").glob("*.untracked.txt")).read_bytes()
    assert listed.endswith(b"\0") and sorted(listed.split(b"\0")[:-1]) == sorted(n.encode() for n in names)


def test_the_receipt_and_header_say_untracked_contents_are_not_saved(repo, tmp_path):  # CoS G2 on #722: the saver keeps untracked NAMES only
    (repo / "u.txt").write_text("secret contents\n")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    receipt = json.loads(r.stdout.strip().splitlines()[-1])
    assert receipt["untrackedContents"] == "not-saved" and receipt["untrackedFormat"] == "nul-separated"
    assert not any(b"secret contents" in p.read_bytes() for p in (tmp_path / "run").iterdir())
    header = "\n".join(SCRIPT.read_text(encoding="utf-8").splitlines()[:12])
    assert "NOT preserved" in header and "LANE-DIRTY-SAVE-INVENTORY-ROWS-1" in header


def test_clean_tree_writes_nothing(repo):
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 10, r.stdout + r.stderr
    assert saved(repo) == []


def test_a_second_save_with_different_dirt_never_overwrites_the_first(repo, tmp_path):
    pid = str(dead_pid())
    dirty(repo)
    assert save(repo, "-LanePid", pid).returncode == 0
    first = {n: (tmp_path / "run" / n).read_bytes() for n in saved(repo)}
    (repo / "a.txt").write_bytes(b"different dirt\n")
    assert save(repo, "-LanePid", pid).returncode == 0
    assert all((tmp_path / "run" / n).read_bytes() == b for n, b in first.items())
    second = len(saved(repo))
    assert second > len(first)
    assert save(repo, "-LanePid", pid).returncode == 0
    assert len(saved(repo)) == second  # the same dirt twice adds nothing


@pytest.mark.parametrize("caller_disable", [None, "0"])
def test_a_configured_trace2_target_cannot_write_into_a_tracked_file(repo, tmp_path, caller_disable):
    # sol r20 B1 on #698: the saver stripped the caller's GIT_TRACE2_EVENT=0 and git then appended to the tracked file the config named.
    dirty(repo)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[trace2]\n\teventTarget = {(repo / 'a.txt').as_posix()}\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(cfg)}
    env.pop("GIT_TRACE2_EVENT", None)
    if caller_disable:
        env["GIT_TRACE2_EVENT"] = caller_disable
    before = (repo / "a.txt").read_bytes()
    assert save(repo, "-LanePid", str(dead_pid()), env=env).returncode == 0
    assert (repo / "a.txt").read_bytes() == before


def test_staged_work_the_working_tree_no_longer_has_is_saved(repo, tmp_path):  # adversary A1
    (repo / "a.txt").write_bytes(b"two\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_bytes(b"one\r\ntwo\nthree\n")
    assert git(repo, "diff", "HEAD", "--binary") == b""
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(n.endswith(".staged.patch") and (tmp_path / "run" / n).read_bytes() == git(repo, "diff", "--cached", "--binary") for n in saved(repo))


def test_the_persisted_receipt_is_the_printed_receipt_of_the_saved_set(repo, tmp_path):  # adversary A3
    (repo / "u.txt").write_text("u\n")
    pid = str(dead_pid())
    assert save(repo, "-LanePid", pid).returncode == 0
    (repo / "a.txt").write_bytes(b"changed\n")
    r = save(repo, "-LanePid", pid)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() in [(tmp_path / "run" / n).read_text().strip() for n in saved(repo) if n.endswith(".receipt.json")]


def test_child_json_uses_its_root_created_utc_not_a_nested_one(repo, tmp_path):  # adversary A4
    dirty(repo)
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)).isoformat()
    child = tmp_path / "child.json"
    child.write_text(json.dumps({"pid": os.getpid(), "extra": {"createdUtc": "2000-01-01T00:00:00Z"}, "createdUtc": future}))
    assert save(repo, "-ChildJson", str(child)).returncode == 20


def test_a_pid_reused_two_seconds_after_created_utc_is_dead(repo, tmp_path):  # adversary A5
    dirty(repo)
    born = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        child = tmp_path / "child.json"
        child.write_text(json.dumps({"pid": sleeper.pid, "createdUtc": born.isoformat()}))
        assert save(repo, "-ChildJson", str(child)).returncode == 0
    finally:
        sleeper.kill()


def test_relative_paths_resolve_where_powershell_is_not_where_dotnet_is(repo, tmp_path):  # adversary A6
    other = tmp_path / "elsewhere"
    other.mkdir()
    cmd = f"Set-Location '{tmp_path}'; [Environment]::CurrentDirectory = '{other}'; & '{SCRIPT}' -WorkDir wt -RunDir run -LanePid {dead_pid()}; exit $LASTEXITCODE"
    dirty(repo)
    r = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert saved(repo) != []


def test_a_missing_git_is_exit_40_not_a_powershell_throw(repo, tmp_path):  # adversary A7; dot B2: an empty PATH dir, no Windows-only SystemRoot
    dirty(repo)
    (tmp_path / "nogit").mkdir()
    r = save(repo, "-LanePid", str(dead_pid()), env={**os.environ, "PATH": str(tmp_path / "nogit")})
    assert r.returncode == 40, r.stdout + r.stderr
    assert saved(repo) == []


def test_diff_context_config_cannot_make_the_patch_unappliable(repo, tmp_path):  # adversary A8
    git(repo, "config", "diff.context", "0")
    (repo / "a.txt").write_bytes(b"one\r\nTWO\nthree\n")
    assert save(repo, "-LanePid", str(dead_pid())).returncode == 0
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(repo), str(clone)], check=True, capture_output=True)
    git(clone, "apply", "--binary", "--check", str(next((tmp_path / "run").glob("*.patch"))))

# ---- r5: sol r4 B1-B7 on #722 ----
def head8(wd):
    return git(wd, "rev-parse", "HEAD").decode().strip()[:8]


def last_receipt(r):
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_an_inherited_git_diff_opts_cannot_make_the_patch_unappliable(repo, tmp_path):  # sol r4 B1
    (repo / "a.txt").write_bytes(b"one\r\nTWO\nthree\n")
    assert save(repo, "-LanePid", str(dead_pid()), env={**os.environ, "GIT_DIFF_OPTS": "--unified=0"}).returncode == 0
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(repo), str(clone)], check=True, capture_output=True)
    git(clone, "apply", "--binary", "--check", str(next((tmp_path / "run").glob("*.patch"))))


def test_a_process_that_started_after_created_utc_is_dead_even_by_half_a_second(repo, tmp_path):  # sol r4 B2a
    dirty(repo)
    born = datetime.datetime.now(datetime.timezone.utc)
    time.sleep(0.4)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        child = tmp_path / "child.json"
        child.write_text(json.dumps({"pid": sleeper.pid, "createdUtc": born.isoformat()}))
        r = save(repo, "-ChildJson", str(child))
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        sleeper.kill()


@pytest.mark.parametrize("created", [1, 0, "soon", True])
def test_a_created_utc_that_is_not_an_iso_timestamp_is_bad_arguments_and_saves_nothing(repo, tmp_path, created):  # sol r4 B2b
    dirty(repo)
    child = tmp_path / "child.json"
    child.write_text(json.dumps({"pid": os.getpid(), "createdUtc": created}))
    r = save(repo, "-ChildJson", str(child))
    assert r.returncode == 30, r.stdout + r.stderr
    assert saved(repo) == []


def test_a_directory_at_an_artifact_name_advances_the_suffix_and_the_receipt_is_written(repo, tmp_path):  # sol r4 B3
    (repo / "a.txt").write_bytes(b"two\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_bytes(b"three\n")
    (tmp_path / "run" / f"dirty-save-{head8(repo)}.staged.patch").mkdir()
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    receipt = last_receipt(r)
    assert receipt["staged"].endswith(".2.staged.patch") and (tmp_path / "run" / receipt["staged"]).is_file()
    assert any(n.endswith(".receipt.json") for n in saved(repo))


def test_an_unreadable_existing_artifact_is_never_called_different(repo, tmp_path):  # sol r4 B4
    (repo / "a.txt").write_bytes(b"changed\n")
    pid = str(dead_pid())
    first = last_receipt(save(repo, "-LanePid", pid))
    held = tmp_path / "run" / first["patch"]
    marker = tmp_path / "held.marker"
    holder = subprocess.Popen([PWSH, "-NoProfile", "-Command",
                               f"$fs = [System.IO.File]::Open('{held}', 'Open', 'Read', 'None'); Set-Content -LiteralPath '{marker}' x; Start-Sleep 60"])
    try:
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists()
        r = save(repo, "-LanePid", pid)
        assert r.returncode == 0, r.stdout + r.stderr
        second = last_receipt(r)
        assert second["comparisonUnreadable"] == [first["patch"]]  # it says the comparison was unreadable instead of claiming the bytes differ
        assert second["patch"] != first["patch"]  # the dirt is still saved (suffixed) rather than lost behind a lock
    finally:
        holder.kill()


def test_both_diffs_are_pinned_to_the_one_resolved_head_sha():  # sol r4 B5: a HEAD move between the git calls cannot be replayed black-box, so the source is pinned
    calls = re.findall(r"@\('diff'[^)]*\)", SCRIPT.read_text(encoding="utf-8"))
    assert len(calls) == 2 and all("$headFull" in c and "'HEAD'" not in c for c in calls), calls


def test_a_subdirectory_work_dir_collects_the_whole_worktree_even_with_diff_relative(repo, tmp_path):  # sol r4 B6
    (repo / "sub").mkdir()
    (repo / "sub" / "s.txt").write_text("s\n")
    git(repo, "add", "sub")
    git(repo, "commit", "-q", "-m", "sub")
    git(repo, "config", "diff.relative", "true")
    (repo / "a.txt").write_bytes(b"outside the subdirectory\n")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo / "sub"), "-RunDir", str(tmp_path / "run"), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert b"a/a.txt" in (tmp_path / "run" / last_receipt(r)["patch"]).read_bytes()


def test_a_run_dir_inside_the_worktree_is_bad_arguments(repo):  # sol r4 hardening: evidence written in the tree would be listed as untracked dirt on the next save
    (repo / "run").mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(repo / "run"), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30, r.stdout + r.stderr
    assert list((repo / "run").iterdir()) == []


# ---- r6: only git's output terminator is stripped from the toplevel path (r5 ADV-A4; Dot SHARED BLOCKER 6561896260) ----
def init_repo(wd, content):
    wd.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["config", "core.autocrlf", "false"]):
        git(wd, *a)
    (wd / "a.txt").write_bytes(content)
    git(wd, "add", ".")
    git(wd, "commit", "-q", "-m", "base")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows strips a trailing space from a path component, so git cannot even enter such a directory; the POSIX CI leg is the real-repo case and test_the_toplevel_path_loses_only_git_s_line_terminator covers the decoder on every platform")
@pytest.mark.parametrize("sibling_dirty", [False, True])
def test_a_worktree_whose_name_ends_in_a_space_is_saved_not_its_sibling(tmp_path, sibling_dirty):
    selected, sibling = tmp_path / "repo ", tmp_path / "repo"
    (tmp_path / "run").mkdir()
    init_repo(selected, b"selected base\n")
    init_repo(sibling, b"sibling base\n")
    (selected / "a.txt").write_bytes(b"SELECTED dirt\n")
    if sibling_dirty:
        (sibling / "a.txt").write_bytes(b"SIBLING dirt\n")
    r = save(selected, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr  # old code: toplevel .Trim() -> the sibling: exit 10 (clean) or the sibling's patch
    receipt = last_receipt(r)
    patch = tmp_path / "run" / receipt["patch"]
    assert receipt["head"] == git(selected, "rev-parse", "HEAD").decode().strip()
    assert b"SELECTED dirt" in patch.read_bytes() and b"SIBLING" not in patch.read_bytes()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(selected), str(clone)], check=True, capture_output=True)
    git(clone, "apply", "--binary", str(patch))  # applies in the SELECTED repo's history
    assert (clone / "a.txt").read_bytes() == b"SELECTED dirt\n"


@pytest.mark.parametrize("raw, want", [
    (b"/w/repo \n", "/w/repo "),         # trailing space survives; the LF is git's terminator
    (b"/w/repo\r\n", "/w/repo\r"),       # git never emits CRLF, so a CR before the LF is the last byte of the NAME (r6 A1)
    (b"/w/repo \r\n", "/w/repo \r"),
    (b"/w/repo\n", "/w/repo"),
    (b"/w/ repo\n", "/w/ repo"),         # leading space is part of the name too
    (b"/w/repo  \n\n", "/w/repo  \n"),   # exactly ONE terminator, never a run of whitespace
    (b"/w/repo ", "/w/repo "),           # no terminator, nothing to strip
])
def test_the_toplevel_path_loses_only_git_s_line_terminator(raw, want):
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^function Remove-GitTerminator.*?^}\n", text, re.MULTILINE | re.DOTALL)
    assert m, "lane-dirty-save.ps1 has no Remove-GitTerminator"  # old code: .Trim() inline, no helper
    prog = m.group(0) + f"$b = [Convert]::FromHexString('{raw.hex()}'); [Console]::Out.Write('[' + (Remove-GitTerminator $b) + ']')"
    r = subprocess.run([PWSH, "-NoProfile", "-Command", prog], capture_output=True)  # bytes: text=True would turn a CR into LF
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.decode("utf-8") == "[" + want + "]"


def link_dir(link, target):
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)  # a junction needs no privilege
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.parametrize("via_ancestor", [False, True])
def test_a_run_dir_that_reaches_the_worktree_through_a_link_is_bad_arguments(repo, tmp_path, via_ancestor):  # r6 A2
    (repo / "run").mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    alias = tmp_path / "alias"
    link_dir(alias, repo if via_ancestor else repo / "run")
    run_dir = alias / "run" if via_ancestor else alias
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(run_dir), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30, r.stdout + r.stderr
    assert "run-dir-inside-worktree" in r.stdout and list((repo / "run").iterdir()) == []


def test_a_receipt_whose_own_earlier_name_was_unreadable_says_so(repo, tmp_path):  # r6 A3: the receipt used to snapshot comparisonUnreadable before its own save
    (repo / "a.txt").write_bytes(b"changed\n")
    pid = str(dead_pid())
    assert save(repo, "-LanePid", pid).returncode == 0
    h8 = head8(repo)
    first_receipt = f"dirty-save-{h8}.receipt.json"
    held = tmp_path / "run" / first_receipt
    marker = tmp_path / "held.marker"
    holder = subprocess.Popen([PWSH, "-NoProfile", "-Command",
                               f"$fs = [System.IO.File]::Open('{held}', 'Open', 'Read', 'None'); Set-Content -LiteralPath '{marker}' x; Start-Sleep 60"])
    try:
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists()
        r = save(repo, "-LanePid", pid)
        assert r.returncode == 0, r.stdout + r.stderr
        persisted = (tmp_path / "run" / f"dirty-save-{h8}.2.receipt.json").read_text(encoding="utf-8")
        assert json.loads(persisted)["comparisonUnreadable"] == [first_receipt]
        assert persisted.strip() == r.stdout.strip()  # printed == persisted
    finally:
        holder.kill()


def deny_writes(path):
    if sys.platform == "win32":
        subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(WD,AD)"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o555)


def allow_writes(path):
    if sys.platform == "win32":
        subprocess.run(["icacls", str(path), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o755)


@pytest.mark.skipif(sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_run_dir_that_cannot_be_written_exits_50_not_a_powershell_error(repo, tmp_path):  # r6 A4
    (repo / "a.txt").write_bytes(b"changed\n")
    deny_writes(tmp_path / "run")
    try:
        r = save(repo, "-LanePid", str(dead_pid()))
    finally:
        allow_writes(tmp_path / "run")
    assert r.returncode == 50, r.stdout + r.stderr
    assert "DIRTY_SAVE_WRITE_FAILED" in r.stdout


def test_the_toplevel_path_is_never_trimmed():
    assert not re.search(r"show-toplevel[^\n]*\n[^\n]*\.Trim\(\)", SCRIPT.read_text(encoding="utf-8"))  # r5 ADV-A4: the line after the toplevel call must not Trim the path


# ---- r5 B7: the walker fixture (subject-ledger.md:8547-8548: "walker fixture and its unit test" belong to CORE) ----
FIXTURE = SCRIPT.parents[2] / "tools" / "repo_hygiene" / "lane_dirty_save_walker_fixture.ps1.txt"
# The walker is a census over the saver's raw text for any call that can make a hard link or a native rename (OWNER-FOOTAGE-NO-HARDLINK-1): the saver may write only through CREATE_NEW streams.
LINK_CAPABLE = re.compile(r"\b(?:Nt|Zw)SetInformationFile|SetFileInformationByHandle|CreateHardLink|-ItemType\s+HardLink|\bmklink\b|fsutil\s+hardlink|\bln\s+(?!-s)|\[(?:System\.)?IO\.File\]::(?:Move|Replace)", re.IGNORECASE)  # r6 A5: [IO.File] is the same type as [System.IO.File]


def walker_violations(text):
    return LINK_CAPABLE.findall(text)


def test_the_walker_fixture_is_the_saver_byte_for_byte_and_the_walker_reads_it_clean():
    live, frozen = (p.read_bytes().replace(b"\r\n", b"\n") for p in (SCRIPT, FIXTURE))
    assert frozen == live, f"re-copy: cp {SCRIPT} {FIXTURE}"
    assert walker_violations(frozen.decode("utf-8")) == []


@pytest.mark.parametrize("mutant", ["NtSetInformationFile(h, out io, info, 24, 11);", "CreateHardLinkW(a, b, 0);", "New-Item -ItemType HardLink -Path $a -Value $b",
                                    "cmd /c mklink /H a b", "fsutil hardlink create a b", "[System.IO.File]::Move($a, $b)", "SetFileInformationByHandle(h, 11, p, 8)",
                                    "[IO.File]::Move($a, $b)", "[IO.File]::Replace($a, $b, $null)"])
def test_the_walker_goes_red_on_every_link_capable_spelling_cut_from_the_fixture(mutant):
    assert walker_violations(FIXTURE.read_text(encoding="utf-8") + "\n" + mutant + "\n")
