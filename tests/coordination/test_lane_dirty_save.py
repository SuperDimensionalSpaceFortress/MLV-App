"""LANE-DIRTY-SAVE-CORE-1: tools/coordination/lane-dirty-save.ps1 keeps a dead lane's uncommitted work.
Exit codes: 0 saved, 10 nothing to save, 20 lane alive (refused), 30 bad arguments, 40 git failed."""
import datetime, json, os, re, shutil, subprocess, sys, tempfile, time
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


def test_relative_paths_are_refused_not_resolved_against_either_current_directory(repo, tmp_path):  # adversary A6; r13 CHANGED MEANING (sol r12 F1): this asserted that a relative path resolved where PowerShell is and not where .NET is; no path is resolved against a current directory any more, so it is refused (exit 30 reason=not-fully-qualified) and nothing is saved
    other = tmp_path / "elsewhere"
    other.mkdir()
    cmd = f"Set-Location '{tmp_path}'; [Environment]::CurrentDirectory = '{other}'; & '{SCRIPT}' -WorkDir wt -RunDir run -LanePid {dead_pid()}; exit $LASTEXITCODE"
    dirty(repo)
    r = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    assert r.returncode == 30 and "reason=not-fully-qualified" in r.stdout, r.stdout + r.stderr  # r12 saver: 0
    assert saved(repo) == []


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
def test_a_run_dir_that_reaches_the_worktree_through_a_link_is_bad_arguments(repo, tmp_path, via_ancestor):  # r6 A2. r13 CHANGED MEANING (sol r12 F2): the link used to be resolved and the RunDir judged inside the worktree; no link is resolved now, any link in the chain is refused first (reason=link-in-path)
    (repo / "run").mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    alias = tmp_path / "alias"
    link_dir(alias, repo if via_ancestor else repo / "run")
    run_dir = alias / "run" if via_ancestor else alias
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(run_dir), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30, r.stdout + r.stderr
    assert "reason=link-in-path" in r.stdout and list((repo / "run").iterdir()) == []  # r12 saver: reason=run-dir-inside-worktree


def short_name(path):
    out = subprocess.run(["cmd", "/c", f'for %I in ("{path}") do @echo %~sI'], capture_output=True, text=True).stdout.strip()
    return out


@pytest.mark.skipif(sys.platform != "win32", reason="extended-length and 8.3 spellings are Windows path forms")
@pytest.mark.parametrize("spelling", ["extended", "short"])
def test_a_run_dir_spelled_extended_or_8dot3_inside_the_worktree_is_bad_arguments(repo, spelling):  # r7 A1
    inside = repo / "run"
    inside.mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    if spelling == "extended":
        spelled = "\\\\?\\" + str(inside)
    else:
        spelled = short_name(inside)
        if spelled.lower() == str(inside).lower() or "~" not in spelled:
            pytest.skip("8.3 short names are disabled on this volume")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", spelled, "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30, r.stdout + r.stderr
    assert list(inside.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a backslash is a path separator on Windows, so a directory name containing one cannot exist there; the source-pin test below covers Windows")
def test_a_posix_run_dir_symlink_whose_name_holds_a_backslash_is_refused_as_a_link_not_judged_by_a_decoy(repo, tmp_path):  # r7 dot B1 on #722 (comment 6565339764). r13 CHANGED MEANING: the link was resolved and judged inside the worktree (run-dir-inside-worktree); any link is refused now (link-in-path). Never run on the Windows hub VM (skipped): first run is ubuntu CI
    (repo / "run").mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    (tmp_path / "out" / "alias").mkdir(parents=True)  # the decoy the old resolver walked after splitting 'out\alias' on the backslash
    spelled = str(tmp_path / "out\\alias")  # ONE file name: a symlink to the real run dir inside the worktree
    os.symlink(repo / "run", spelled, target_is_directory=True)
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", spelled, "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30, r.stdout + r.stderr  # old code: exit 0, evidence written through the link into the worktree
    assert "reason=link-in-path" in r.stdout  # r12 saver: run-dir-inside-worktree
    assert list((repo / "run").iterdir()) == [] and list((tmp_path / "out" / "alias").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a backslash is a path separator on Windows, so a directory name containing one cannot exist there")
def test_a_posix_run_dir_with_a_literal_backslash_name_receives_the_artifacts_where_it_really_is(repo, tmp_path):  # r7 dot B1 class: the written directory is the checked one
    (repo / "a.txt").write_bytes(b"changed\n")
    real = tmp_path / "out\\run"
    real.mkdir()
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(real), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in real.iterdir()) and not (tmp_path / "out").exists()


def test_the_resolver_and_the_writer_use_only_the_platforms_own_separators():  # r7 dot B1 source pin, runs on every platform. r13 CHANGED MEANING (sol r12 F2): the resolver (Resolve-RealPath, which followed links) is deleted; the same separator rule is pinned on its replacement, the link-free chain walk
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(re.sub(r"\s#\s.*$", "", ln) for ln in text.splitlines() if not ln.lstrip().startswith("#"))  # comments may name the deleted resolver; code may not
    assert "Resolve-RealPath" not in code, "no code path may resolve a link any more"
    assert not re.search(r"ResolveLinkTarget", code)  # the link resolution itself is gone; LinkTarget is only READ (to refuse)
    m = re.search(r"^function Assert-LinkFree.*?^}\n", text, re.MULTILINE | re.DOTALL)
    assert m, "lane-dirty-save.ps1 has no Assert-LinkFree"
    body = m.group(0)
    assert "[char[]]@('\\', '/')" not in body and "DirectorySeparatorChar" in body and "AltDirectorySeparatorChar" in body  # old code: split on both characters on every platform
    assert "Get-FinalDir $RunDir" in text and "Get-FinalDir $WorkDir" in text  # Windows resolves through an open directory handle, not by walking names
    assert "Resolve-Path" not in text and "Join-Path" not in text and "Test-Path" not in text  # PowerShell's path layer may re-spell a backslash; .NET only
    assert "Assert-LinkFree $raw" in text and "$RunDir = $script:held[0].Path;" in text  # every argument is walked for links before any read; the checked directory is the written one, on both platforms


def ps_functions(*names):
    text = SCRIPT.read_text(encoding="utf-8")
    out = ""
    for n in names:
        m = re.search(rf"^function {n}\b.*?^}}\n", text, re.MULTILINE | re.DOTALL)
        assert m, f"lane-dirty-save.ps1 has no {n}"  # old code: no such helper
        out += m.group(0)
    return out


@pytest.mark.parametrize("raw, want", [
    ("\\\\?\\C:\\a\\b", "\\\\?\\C:\\a\\b"),                          # the extended prefix is KEPT: Win32 trims a trailing dot or space from a plain path (r7 fix-2 A1)
    ("\\\\?\\C:\\a\\run.", "\\\\?\\C:\\a\\run."),
    ("\\\\.\\C:\\a", "\\\\?\\C:\\a"),
    ("\\\\?\\UNC\\srv\\sh\\repo\\run", "\\\\?\\UNC\\srv\\sh\\repo\\run"),
    ("\\\\?\\unc\\srv\\sh\\repo\\run", "\\\\?\\UNC\\srv\\sh\\repo\\run"),   # r7 A1: the UNC marker is case-insensitive
    ("\\\\.\\UNC\\srv\\sh\\repo\\run", "\\\\?\\UNC\\srv\\sh\\repo\\run"),
    ("//?/unc/srv/sh/repo/run", "\\\\?\\UNC\\srv\\sh\\repo\\run"),          # forward slashes spell the same prefix
    ("C:/plain/x", "C:\\plain\\x"),
    ("C:\\plain\\x", "C:\\plain\\x"),
    ("\\\\srv\\sh\\x", "\\\\srv\\sh\\x"),
    ("\\\\?\\Volume{1234}\\x", None),                                # a form with no drive or UNC meaning is refused, not stripped to a local relative path
    ("\\\\?\\GLOBALROOT\\Device\\x", None),
    ("\\\\.\\PhysicalDrive0", None),
    ("\\\\?\\C:", None),
])
def test_an_extended_length_prefix_is_normalised_in_any_case_and_kept_or_the_path_is_refused(raw, want):  # r7 A1
    prog = "$script:onWin = $true; " + ps_functions("ConvertTo-NativePath") + "try { [Console]::Out.Write('[' + (ConvertTo-NativePath $env:LDS_P) + ']') } catch { [Console]::Out.Write('THROWN ' + $_.Exception.Message) }"
    r = subprocess.run([PWSH, "-NoProfile", "-Command", prog], capture_output=True, env={**os.environ, "LDS_P": raw})
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.decode("utf-8") == ("THROWN unsupported-extended-path" if want is None else "[" + want + "]")


@pytest.mark.skipif(sys.platform != "win32", reason="subst drives are Windows")
def test_a_run_dir_on_a_subst_drive_that_maps_into_the_worktree_is_bad_arguments(repo):  # r7 A2
    letter = next((c for c in "YXWVUTSR" if not os.path.exists(f"{c}:\\")), None)
    if letter is None:
        pytest.skip("no free drive letter")
    (repo / "run").mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    subprocess.run(["subst", f"{letter}:", str(repo)], check=True, capture_output=True)
    try:
        r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", f"{letter}:\\run", "-LanePid", str(dead_pid())], capture_output=True, text=True)
    finally:
        subprocess.run(["subst", f"{letter}:", "/d"], capture_output=True)
    assert r.returncode == 30, r.stdout + r.stderr  # old code: exit 0, evidence written into the worktree through the mapping
    assert "run-dir-inside-worktree" in r.stdout and list((repo / "run").iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="trailing-dot directories and extended-length paths are Windows")
def test_an_extended_run_dir_with_a_trailing_dot_receives_the_artifacts_not_its_dotless_sibling(repo, tmp_path):  # r7 fix-2 A1
    (repo / "a.txt").write_bytes(b"changed\n")
    ext = lambda p: "\\\\?\\" + str(p)
    dotted, plain = tmp_path / "evid.", tmp_path / "evid"
    os.mkdir(ext(dotted))
    plain.mkdir()
    try:
        r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", ext(dotted), "-LanePid", str(dead_pid())], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert os.listdir(ext(dotted)) != [] and list(plain.iterdir()) == []  # old code: the prefix was stripped before CreateFile, Win32 trimmed the dot, the sibling got the files
    finally:
        for name in os.listdir(ext(dotted)):
            os.remove(ext(dotted) + "\\" + name)
        os.rmdir(ext(dotted))


@pytest.mark.skipif(sys.platform != "win32", reason="administrative shares are Windows")
def test_a_run_dir_reached_through_a_loopback_share_of_the_worktree_drive_is_bad_arguments(repo):  # r7 fix-2 A2
    (repo / "run").mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    unc = "\\\\localhost\\" + str(repo / "run").replace(":", "$", 1)
    if not os.path.isdir(unc):
        pytest.skip("the administrative share of this drive is not reachable by this user")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", unc, "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30, r.stdout + r.stderr  # old code: the UNC and drive spellings were never compared, exit 0 and evidence in the worktree
    assert list((repo / "run").iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="the open directory handle is the Windows remedy; POSIX re-resolves names only (see summary OPEN A3-posix)")
def test_the_checked_run_dir_cannot_be_renamed_away_while_the_saver_holds_it(tmp_path):  # r7 A3
    target = tmp_path / "run"
    target.mkdir()
    prog = ("$script:onWin = $true; " + ps_functions("ConvertTo-NativePath", "Get-FinalDir") +
            "$h = Get-FinalDir $env:LDS_P; try { [System.IO.Directory]::Move($env:LDS_P, $env:LDS_P + '.moved'); $o = 'MOVED' } catch { $o = 'BLOCKED' }; "
            "$h.Handle.Dispose(); [Console]::Out.Write($o + ' ' + $h.Path)")
    r = subprocess.run([PWSH, "-NoProfile", "-Command", prog], capture_output=True, env={**os.environ, "LDS_P": str(target)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.decode("utf-8").split(" ", 1)[0] == "BLOCKED" and target.is_dir()  # old code: only a path string survived the check
    assert not (tmp_path / "run.moved").exists()


def test_a_changed_gitlink_is_saved_as_an_applicable_patch_even_when_diff_submodule_is_log(repo, tmp_path):  # r7 A2
    sha_a, sha_b = "a" * 40, "b" * 40
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha_a},sub")
    git(repo, "commit", "-q", "-m", "gitlink")
    git(repo, "config", "diff.submodule", "log")
    git(repo, "update-index", "--cacheinfo", f"160000,{sha_b},sub")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    staged = (tmp_path / "run" / last_receipt(r)["staged"]).read_bytes()
    assert staged.startswith(b"diff --git a/sub b/sub") and f"-Subproject commit {sha_a}".encode() in staged and f"+Subproject commit {sha_b}".encode() in staged, staged


def test_diff_ignore_submodules_config_cannot_hide_a_changed_gitlink(repo, tmp_path):  # r7 A2 class: diff.ignoreSubmodules=all
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},sub")
    git(repo, "commit", "-q", "-m", "gitlink")
    git(repo, "config", "diff.ignoreSubmodules", "all")
    git(repo, "update-index", "--cacheinfo", f"160000,{'b' * 40},sub")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr  # exit 10 would mean "nothing to save"


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


def test_a_configured_fsmonitor_callback_cannot_overwrite_the_dirt_being_saved(repo, tmp_path):  # r8 sol B1: git runs core.fsmonitor while it reads the index; the saver must not let it
    dirty(repo)
    want, a_before, marker = git(repo, "diff", "HEAD", "--binary"), (repo / "a.txt").read_bytes(), tmp_path / "fsmonitor-ran"
    git(repo, "config", "core.fsmonitor", f"printf X >> '{marker.as_posix()}'; printf REPLACED > '{(repo / 'a.txt').as_posix()}'; :")  # no git call from the test after this line
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before  # old code: the callback ran and rewrote the tracked file
    assert next(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".patch") and ".staged." not in p.name).read_bytes() == want


@pytest.mark.parametrize("child", [{"pid": 2147483647}, {"pid": 2147483647, "createdUtc": None}])
def test_child_json_without_a_created_utc_is_bad_arguments_and_saves_nothing(repo, tmp_path, child):  # r8 sol B3: a missing timestamp used to skip the reuse check and save with exit 0
    dirty(repo)
    (tmp_path / "child.json").write_text(json.dumps(child))
    r = save(repo, "-ChildJson", str(tmp_path / "child.json"))
    assert r.returncode == 30 and "bad-created-utc" in r.stdout, r.stdout + r.stderr
    assert saved(repo) == []


def test_a_relative_child_json_is_refused_and_neither_directorys_record_is_read(repo, tmp_path):  # r9 sol r8b B1: ReadAllText follows .NET's directory, where another lane's record (dead pid) may sit while the intended lane is alive. r13 CHANGED MEANING (sol r12 F1): the record used to be read from PowerShell's directory (exit 20 for the live lane); a relative -ChildJson is now refused outright, so neither the intended record nor the decoy can be picked by a current directory
    jobs, other = tmp_path / "jobs", tmp_path / "elsewhere"
    jobs.mkdir(); other.mkdir()
    (jobs / "child.json").write_text(json.dumps({"pid": os.getpid(), "createdUtc": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()}))  # the intended lane: alive (a stamp later than its start)
    (other / "child.json").write_text(json.dumps({"pid": 2147483647, "createdUtc": "2000-01-01T00:00:00Z"}))  # decoy: pid absent, so dead
    dirty(repo)
    cmd = f"Set-Location '{jobs}'; [Environment]::CurrentDirectory = '{other}'; & '{SCRIPT}' -WorkDir '{repo}' -RunDir '{repo.parent / 'run'}' -ChildJson child.json; exit $LASTEXITCODE"
    r = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    assert r.returncode == 30 and "reason=not-fully-qualified" in r.stdout, r.stdout + r.stderr  # r12 saver: 20 (the record of the live lane); r8 saver: read the decoy, exit 0
    assert saved(repo) == []


@pytest.mark.parametrize("created", ["2026-10-30T12:30:00\nZ", "2026-10-30T12:30:00Z\n", "\n2026-10-30T12:30:00Z", "2026-10-30T12:30:00Z ", "2026-13-45T12:30:00Z",
                                     "2026-10-30T12:30:00", "2026-10-30 12:30:00Z", "٢٠٢٦-10-30T12:30:00Z", ["2026-10-30T12:30:00Z"], {"v": "2026-10-30T12:30:00Z"}])
def test_a_created_utc_with_whitespace_or_not_exactly_iso_is_bad_arguments_and_saves_nothing(repo, tmp_path, created):  # r9 sol r8b B2: ^...$ lets a trailing LF through and DateTime.Parse then reads a newline inside the value
    dirty(repo)
    (tmp_path / "child.json").write_text(json.dumps({"pid": 2147483647, "createdUtc": created}))
    r = save(repo, "-ChildJson", str(tmp_path / "child.json"))
    assert r.returncode == 30 and "bad-created-utc" in r.stdout, r.stdout + r.stderr
    assert saved(repo) == []


def test_no_regex_in_the_saver_is_closed_with_a_dollar_that_lets_a_trailing_newline_through():  # r9: the class behind B2, ^...$ matches before a final LF; \A...\z does not
    assert not re.search(r"-(?:c?not)?match\s+'[^']*[^\\]\$'", SCRIPT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("caller_env", [{}, {"GIT_CONFIG": os.devnull}])  # GIT_CONFIG redirects only `git config`, so a discovery run through it saw no filter (second r8 pass A1)
def test_a_configured_clean_filter_cannot_overwrite_the_dirt_being_saved(repo, tmp_path, caller_env):  # r8 adversary A1: the same class as fsmonitor, a command git runs on every tracked file it diffs
    (repo / ".gitattributes").write_text("a.txt filter=rewrite\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs")
    dirty(repo)
    want, a_before, marker = git(repo, "diff", "HEAD", "--binary"), (repo / "a.txt").read_bytes(), tmp_path / "filter-ran"
    git(repo, "config", "filter.rewrite.clean", f"printf X >> '{marker.as_posix()}'; printf REPLACED > '{(repo / 'a.txt').as_posix()}'; cat")  # no git call from the test after this line
    r = save(repo, "-LanePid", str(dead_pid()), env={**os.environ, **caller_env})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before  # old code: the clean filter ran and rewrote the tracked file
    assert next(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".patch") and ".staged." not in p.name).read_bytes() == want


@pytest.mark.parametrize("benign,hostile", [("a", "a­"), ("rewrite", "Rewrite")])  # git treats these as two drivers; a culture-sensitive unique collapsed the first pair (the case pair is a guard: it already passed)
def test_two_filter_drivers_whose_names_differ_only_by_an_ignorable_char_or_case_are_both_blanked(repo, tmp_path, benign, hostile):  # r9 adversary A1: Select-Object -Unique kept one name, the other driver still rewrote a.txt
    (repo / ".gitattributes").write_text(f"a.txt filter={hostile}\n", encoding="utf-8")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs")
    dirty(repo)
    want, a_before, marker = git(repo, "diff", "HEAD", "--binary"), (repo / "a.txt").read_bytes(), tmp_path / "filter-ran"
    git(repo, "config", f"filter.{benign}.clean", "cat")  # first, so a unique-by-culture keeps this name
    git(repo, "config", f"filter.{hostile}.clean", f"printf X >> '{marker.as_posix()}'; printf REPLACED > '{(repo / 'a.txt').as_posix()}'; cat")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before
    assert next(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".patch") and ".staged." not in p.name).read_bytes() == want


def test_a_clean_filter_configured_only_inside_a_submodule_cannot_overwrite_the_parents_dirt(repo, tmp_path):  # r9 second adversary pass A1: the diff scans the submodule through the submodule's own config, which a root-only search never read
    src = tmp_path / "subsrc"
    src.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["config", "core.autocrlf", "false"]):
        git(src, *a)
    (src / ".gitattributes").write_text("f.txt filter=onlysub\n")
    (src / "f.txt").write_text("old\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "s")
    subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub"], capture_output=True, check=True)
    git(repo, "commit", "-q", "-m", "gitlink")
    marker = tmp_path / "filter-ran"
    git(repo / "sub", "config", "filter.onlysub.clean", f"printf X >> '{marker.as_posix()}'; printf REPLACED > '{(repo / 'a.txt').as_posix()}'; cat")
    dirty(repo)
    time.sleep(1.1)
    (repo / "sub" / "f.txt").write_text("new\n")  # same size as 'old\n', so git must read the content, which runs the clean filter
    a_before = (repo / "a.txt").read_bytes()
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before  # old code: the submodule's filter ran and rewrote a.txt


def test_an_inherited_ssh_command_cannot_run_when_git_fetches_a_missing_promised_blob(repo, tmp_path):  # r9 second adversary pass A3: a diff over a partial clone's missing blob starts a promisor fetch
    srv = tmp_path / "srv.git"
    git(tmp_path, "init", "-q", "--bare", str(srv))
    git(srv, "config", "uploadpack.allowFilter", "true")
    git(srv, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(repo, "push", "-q", str(srv), "HEAD:refs/heads/main")
    pc = tmp_path / "pc"
    subprocess.run(["git", "clone", "-q", "--no-checkout", "--filter=blob:none", "--branch", "main", "file:///" + srv.as_posix().lstrip("/"), str(pc)], capture_output=True, check=True)
    git(pc, "reset", "-q")  # index from HEAD's tree; a.txt is absent on disk and its blob is not here, so diffing it needs the promisor
    git(pc, "config", "remote.origin.url", "ssh://example.invalid/repo")
    marker = tmp_path / "ssh-ran"
    r = save(pc, "-LanePid", str(dead_pid()), env={**os.environ, "GIT_SSH_VARIANT": "ssh", "GIT_SSH_COMMAND": f"printf X >> '{marker.as_posix()}'; false"})
    assert r.returncode in (0, 40), r.stdout + r.stderr
    assert not marker.exists()  # old code: the caller's GIT_SSH_COMMAND ran inside the saver's git


def test_a_replace_ref_for_head_cannot_change_the_tree_the_patch_is_taken_from(repo, tmp_path):  # r9 adversary A2: with git replace F R, both diffs read R's tree while the receipt names F
    first = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "checkout", "-q", "-b", "alt")
    (repo / "a.txt").write_bytes(b"two\n")
    git(repo, "commit", "-q", "-am", "r")
    replacement = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "checkout", "-q", first)
    git(repo, "replace", first, replacement)
    (repo / "a.txt").write_bytes(b"one\r\ntwo\nthree\nfour\n")
    want = subprocess.run(["git", "-C", str(repo), "--no-replace-objects", "diff", first, "--binary"], capture_output=True, check=True).stdout
    assert want
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    names = saved(repo)
    assert not any(".staged." in n for n in names), names  # the index is F's: nothing is staged, but a replaced F made it look different
    assert next(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".patch")).read_bytes() == want


@pytest.mark.parametrize("name", ["­..", "..­"])  # culture compares ignore U+00AD, so these read as '..' and a RunDir inside the worktree looked outside it
def test_a_run_dir_inside_the_worktree_under_a_dotdot_lookalike_name_is_bad_arguments(repo, name):  # r9 adversary A1 class: ordinal path comparisons
    inner = repo / name / "run"
    try:
        inner.mkdir(parents=True)
    except OSError:
        pytest.skip("this file system cannot hold a directory with that name")  # Windows refuses a leading U+00AD before '..'
    dirty(repo)
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(inner), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30 and "run-dir-inside-worktree" in r.stdout, r.stdout + r.stderr
    assert list(inner.iterdir()) == []


def test_a_filter_whose_name_is_not_utf8_never_runs_and_the_dirt_is_still_saved(repo, tmp_path):  # second r8 pass A2 (byte FF became U+FFFD, so the driver git really runs stayed on); r10: no filter name is read at all, so there is nothing to refuse
    (repo / ".gitattributes").write_bytes(b"a.txt filter=\xff\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs")
    dirty(repo)
    a_before, marker = (repo / "a.txt").read_bytes(), tmp_path / "filter-ran"
    with open(repo / ".git" / "config", "ab") as f:  # no git call from the test after this
        f.write(b'[filter "\xff"]\n\tclean = printf X >> \'' + marker.as_posix().encode() + b"'; printf REPLACED > '" + (repo / "a.txt").as_posix().encode() + b"'; cat\n")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before and any(n.endswith(".patch") for n in saved(repo))


def test_an_inherited_git_redirect_cannot_write_git_output_into_a_tracked_file(repo, tmp_path):  # second r8 pass A4: Git for Windows writes stdout into the file GIT_REDIRECT_STDOUT names
    dirty(repo)
    want, a_before = git(repo, "diff", "HEAD", "--binary"), (repo / "a.txt").read_bytes()
    r = save(repo, "-LanePid", str(dead_pid()), env={**os.environ, "GIT_REDIRECT_STDOUT": str(repo / "a.txt"), "GIT_REDIRECT_STDERR": str(repo / "b.bin")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / "a.txt").read_bytes() == a_before and (repo / "b.bin").read_bytes() == BINARY[::-1]
    assert next(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".patch") and ".staged." not in p.name).read_bytes() == want


# ---- r10: sol r9 B1 (a filter added after the search) and B2 (a submodule below the depth cap): no collecting git call can run a configured callback ----
def rewriting_filter(marker, victim):
    return f"printf X >> '{marker.as_posix()}'; printf REPLACED > '{victim.as_posix()}'; cat"


def commit_attrs(repo, line):
    (repo / ".gitattributes").write_text(line)
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs")


def newest_patch(tmp_path):
    return next(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".patch") and ".staged." not in p.name).read_bytes()


def save_with_hook(wd, tmp_path, hook, env=None):
    """Run a COPY of the saver with `hook` (PowerShell) run just before its first collecting diff, standing in for another process acting between the saver's steps."""
    anchor = "$git.patch = Invoke-GitBytes"
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    hooked = tmp_path / "hooked-saver.ps1"
    hooked.write_text(text.replace(anchor, f"{hook}; {anchor}"), encoding="utf-8")
    return subprocess.run([PWSH, "-NoProfile", "-File", str(hooked), "-WorkDir", str(wd), "-RunDir", str(tmp_path / "run"), "-LanePid", str(dead_pid())], capture_output=True, text=True, env=env)


def test_a_filter_added_between_the_saver_s_steps_cannot_overwrite_the_dirt_being_saved(repo, tmp_path):  # r10 sol r9 B1: the hook stands where the old filter search ended and the first diff began
    commit_attrs(repo, "a.txt filter=late\n")
    dirty(repo)
    want, a_before, marker = git(repo, "diff", "HEAD", "--binary"), (repo / "a.txt").read_bytes(), tmp_path / "filter-ran"
    anchor = "$git.patch = Invoke-GitBytes"
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    cmd = rewriting_filter(marker, repo / "a.txt").replace("'", "''")
    hooked = tmp_path / "hooked-saver.ps1"  # a copy: the fixture hook is another process editing the config while the saver runs
    hooked.write_text(text.replace(anchor, f"& git -C $WorkDir config filter.late.clean '{cmd}'; {anchor}"), encoding="utf-8")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(hooked), "-WorkDir", str(repo), "-RunDir", str(tmp_path / "run"), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(repo, "config", "filter.late.clean")  # the hook did run, before the collecting calls
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before  # old code: the late filter was not in the override list, ran, and rewrote a.txt
    assert newest_patch(tmp_path) == want


def rmtree_force(path):  # git objects are read-only on Windows
    def fix(fn, p, _err):
        os.chmod(p, 0o700)
        fn(p)
    shutil.rmtree(path, **({"onexc": fix} if sys.version_info >= (3, 12) else {"onerror": fix}))


def chain_of_submodules(tmp_path, depth):
    """A repo that holds the next one down as submodule `s`, `depth` levels in all down to a leaf; returns the top one (the caller adds it to its own repo as `s`)."""
    def mk(name):
        d = tmp_path / "chain" / name
        d.mkdir(parents=True)
        for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["config", "core.autocrlf", "false"]):
            git(d, *a)
        return d
    add = ["-c", "protocol.file.allow=always", "submodule", "add", "-q"]
    leaf = mk("r0")
    (leaf / ".gitattributes").write_text("f.txt filter=deep\n")
    (leaf / "f.txt").write_text("old\n")
    git(leaf, "add", ".")
    git(leaf, "commit", "-q", "-m", "leaf")
    below = leaf
    for i in range(1, depth):
        mid = mk(f"r{i}")
        subprocess.run(["git", "-C", str(mid), *add, str(below), "s"], capture_output=True, check=True)
        git(mid, "commit", "-q", "-m", "mid")
        below = mid
    return below


@pytest.mark.parametrize("depth", [8, 9])  # 8 was the old discovery cap (the boundary), 9 the first level it skipped
def test_a_filter_configured_only_in_a_deeply_nested_submodule_cannot_overwrite_the_parents_dirt(depth):  # r10 sol r9 B2
    base = Path(tempfile.mkdtemp(prefix="lds"))  # the system temp dir, not pytest's long one: nine nested .git/modules paths pass Windows' 260 characters in that
    try:
        top_sub = chain_of_submodules(base, depth)
        repo = base / "wt"
        init_repo(repo, b"one\r\ntwo\nthree\n")
        (base / "run").mkdir()
        subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(top_sub), "s"], capture_output=True, check=True)
        git(repo, "commit", "-q", "-m", "gitlink")
        subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "-q"], capture_output=True, check=True)
        deepest = repo
        for _ in range(depth):
            deepest = deepest / "s"
        marker = base / "filter-ran"
        git(deepest, "config", "filter.deep.clean", rewriting_filter(marker, repo / "a.txt"))
        dirty(repo)
        time.sleep(1.1)
        (deepest / "f.txt").write_text("new\n")  # same size as 'old\n', so git must read the content to know it changed, which runs the clean filter
        a_before = (repo / "a.txt").read_bytes()
        r = save(repo, "-LanePid", str(dead_pid()))
        assert r.returncode == 0, r.stdout + r.stderr
        assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before  # old code at depth 9: the cap skipped this config and --ignore-submodules=none still recursed into it
        assert last_receipt(r)["submodules"] == "skipped 1"  # the submodule's contents are out of scope and the receipt says so
    finally:
        rmtree_force(base)


@pytest.mark.parametrize("how", ["env-count", "global-file"])
def test_a_filter_injected_through_the_environment_or_the_global_config_cannot_run(repo, tmp_path, how):  # r10 class: config can reach git from the environment and from files other than the repository's own
    commit_attrs(repo, "a.txt filter=rewrite\n")
    dirty(repo)
    want, a_before, marker = git(repo, "diff", "HEAD", "--binary"), (repo / "a.txt").read_bytes(), tmp_path / "filter-ran"
    cmd = rewriting_filter(marker, repo / "a.txt")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    if how == "env-count":
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.rewrite.clean", "GIT_CONFIG_VALUE_0": cmd})
    else:
        g = tmp_path / "global.gitconfig"
        g.write_text(f'[filter "rewrite"]\n\tclean = {cmd}\n')
        env["GIT_CONFIG_GLOBAL"] = str(g)
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists() and (repo / "a.txt").read_bytes() == a_before
    assert newest_patch(tmp_path) == want


def test_the_private_git_dir_is_removed_and_a_linked_worktree_is_saved_like_any_other(repo, tmp_path):  # r10: the seal finds the index and objects through --git-path, which differs for a linked worktree
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    (linked / "a.txt").write_bytes(b"one\r\nlinked dirt\n")
    (linked / "new.txt").write_text("u\n")
    want = git(linked, "diff", "HEAD", "--binary")
    r = save(linked, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert newest_patch(tmp_path) == want and last_receipt(r)["submodules"] == "skipped 0"
    for gd in (repo / ".git", repo / ".git" / "worktrees" / "linked"):
        assert [p.name for p in gd.iterdir() if p.name.startswith("lane-dirty-save-")] == []


@pytest.mark.parametrize("child", [{"pid": 2147483647, "createdUtc": ["2000-01-01T00:00:00Z"]},
                                   [{"pid": 2147483647, "createdUtc": "2000-01-01T00:00:00Z"}],
                                   {"pid": [2147483647], "createdUtc": "2000-01-01T00:00:00Z"},
                                   {"pid": os.getpid(), "createdUtc": "/Date(0)/"}])  # second r8 pass A3: PowerShell's JSON reader turns this into 1970 and the live lane then looks reused
def test_child_json_of_the_wrong_shape_is_bad_arguments_and_saves_nothing(repo, tmp_path, child):  # r8 adversary A2: a one-item list or root array was unwrapped into a valid-looking record
    dirty(repo)
    (tmp_path / "child.json").write_text(json.dumps(child))
    r = save(repo, "-ChildJson", str(tmp_path / "child.json"))
    assert r.returncode == 30, r.stdout + r.stderr
    assert saved(repo) == []


def test_a_toplevel_path_that_is_not_utf8_is_refused_not_decoded_into_a_sibling():  # r8 adversary A3: byte FF decoded to U+FFFD, which names a different, valid directory
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^function Remove-GitTerminator.*?^}\n", text, re.MULTILINE | re.DOTALL)
    assert m, "lane-dirty-save.ps1 has no Remove-GitTerminator"
    r = subprocess.run([PWSH, "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; " + m.group(0) + "$b = [Convert]::FromHexString('2f772f7774ff0a'); [Console]::Out.Write('[' + (Remove-GitTerminator $b) + ']')"], capture_output=True)
    assert r.returncode != 0 and b"[" not in r.stdout, r.stdout + r.stderr  # old code: '[/w/wt�]'


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS names are UTF-16, so a path with a byte that is not UTF-8 exists only on POSIX; the decoder test above runs everywhere")
def test_a_posix_worktree_with_a_non_utf8_name_is_refused_and_its_replacement_sibling_is_untouched(tmp_path):  # r8 adversary A3 end to end
    root = os.fsencode(str(tmp_path))
    real, sibling = root + b"/wt\xff", root + "/wt�".encode()
    (tmp_path / "run").mkdir()
    for p in (real, sibling):
        os.mkdir(p)
        subprocess.run(["git", "init", "-q", p], check=True)
        for kv in (("user.email", "t@example.invalid"), ("user.name", "t")):
            subprocess.run(["git", "-C", p, "config", *kv], check=True)
        Path(os.fsdecode(p), "a.txt").write_text("base\n")
        subprocess.run(["git", "-C", p, "add", "."], check=True)
        subprocess.run(["git", "-C", p, "commit", "-q", "-m", "base"], check=True)
    Path(os.fsdecode(sibling), "a.txt").write_text("SIBLING dirt\n")
    Path(os.fsdecode(real), "a.txt").write_text("REAL dirt\n")
    os.symlink(real, root + b"/selected")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(tmp_path / "selected"), "-RunDir", str(tmp_path / "run"), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    assert r.returncode == 30 and "toplevel-not-utf8" in r.stdout, r.stdout + r.stderr
    assert list((tmp_path / "run").iterdir()) == []


def bind_mount(src, dst):
    for pre in ([], ["sudo", "-n"]):
        if subprocess.run([*pre, "mount", "--bind", str(src), str(dst)], capture_output=True).returncode == 0:
            return pre
    return None


@pytest.mark.skipif(sys.platform == "win32", reason="a bind mount is a POSIX construct; the Windows aliases (links, junctions, subst, shares) are covered by the open-handle tests above")
def test_a_run_dir_that_is_a_bind_mount_of_a_directory_inside_the_worktree_is_bad_arguments(repo, tmp_path):  # r8 sol B2: no symlink LinkTarget, so the name walk saw an outside directory
    inside, alias = repo / "run", tmp_path / "evidence"
    inside.mkdir(); alias.mkdir()
    pre = bind_mount(inside, alias)
    if pre is None:
        pytest.skip("cannot create a bind mount here (needs root or passwordless sudo)")
    try:
        (repo / "a.txt").write_bytes(b"changed\n")
        r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(alias), "-LanePid", str(dead_pid())], capture_output=True, text=True)
        assert r.returncode == 30 and "run-dir-inside-worktree" in r.stdout, r.stdout + r.stderr
        assert list(inside.iterdir()) == []
    finally:
        subprocess.run([*pre, "umount", str(alias)], capture_output=True)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX identity check shells out to find and stat; Windows asks the kernel through an open handle")
def test_a_posix_run_dir_whose_identity_cannot_be_proven_is_bad_arguments_not_accepted(repo, tmp_path):  # r8 sol B2: unprovable is exit 30, never accept
    dirty(repo)
    tools = tmp_path / "onlygit"
    tools.mkdir()
    (tools / "git").symlink_to(shutil.which("git"))  # git present, find and stat absent
    r = save(repo, "-LanePid", str(dead_pid()), env={**os.environ, "PATH": str(tools)})
    assert r.returncode == 30 and "run-dir-identity-unprovable" in r.stdout, r.stdout + r.stderr
    assert saved(repo) == []


def test_the_toplevel_path_is_never_trimmed():
    assert not re.search(r"show-toplevel[^\n]*\n[^\n]*\.Trim\(\)", SCRIPT.read_text(encoding="utf-8"))  # r5 ADV-A4: the line after the toplevel call must not Trim the path


# ---- r5 B7: the walker fixture (subject-ledger.md:8547-8548: "walker fixture and its unit test" belong to CORE) ----
FIXTURE = SCRIPT.parents[2] / "tools" / "repo_hygiene" / "lane_dirty_save_walker_fixture.ps1.txt"
# The walker is a census over the saver's raw text for any call that can make a hard link or a native rename (OWNER-FOOTAGE-NO-HARDLINK-1): the saver may write only through CREATE_NEW streams.
LINK_CAPABLE = re.compile(r"\b(?:Nt|Zw)SetInformationFile|SetFileInformationByHandle|CreateHardLink|-(?:Item)?Type[\s:]+[\"']?HardLink|\bmklink\b|fsutil\s+hardlink|\bln\s+(?!-s)|\[(?:System\.)?IO\.(?:File|Directory)\]::\s*[\"']?(?:Move|Replace)|\bcmd(?:\.exe)?\s+/c\s+(?:move|ren(?:ame)?)\b|\.MoveTo\(|\bMove-Item\b|\bRename-Item\b", re.IGNORECASE)  # r6 A5/r7 A3: [IO.File] is [System.IO.File]; whitespace after ::, MoveTo, Move-Item, Rename-Item, a quoted HardLink


def walker_violations(text):
    return LINK_CAPABLE.findall(text)


def test_the_walker_fixture_is_the_saver_byte_for_byte_and_the_walker_reads_it_clean():
    live, frozen = (p.read_bytes().replace(b"\r\n", b"\n") for p in (SCRIPT, FIXTURE))
    assert frozen == live, f"re-copy: cp {SCRIPT} {FIXTURE}"
    assert walker_violations(frozen.decode("utf-8")) == []


@pytest.mark.parametrize("mutant", ["NtSetInformationFile(h, out io, info, 24, 11);", "CreateHardLinkW(a, b, 0);", "New-Item -ItemType HardLink -Path $a -Value $b",
                                    "cmd /c mklink /H a b", "fsutil hardlink create a b", "[System.IO.File]::Move($a, $b)", "SetFileInformationByHandle(h, 11, p, 8)",
                                    "[IO.File]::Move($a, $b)", "[IO.File]::Replace($a, $b, $null)", "[IO.File]:: Move($a, $b)", "[IO.FileInfo]::new($a).MoveTo($b)",
                                    "Move-Item -LiteralPath $a -Destination $b", 'New-Item -ItemType "HardLink" -Path $a -Value $b', "Rename-Item $a $b", "New-Item -ItemType:HardLink -Path $a -Value $b", "New-Item -ItemType: HardLink -Path $a -Value $b",
                                    "[IO.Directory]::Move($a, $b)", "[System.IO.Directory]::Move($a, $b)", "[IO.File]::'Move'($a,$b)", "New-Item -Type HardLink -Path $a -Value $b", "cmd /c move a b"])  # r8 sol hardening: directory moves
def test_the_walker_goes_red_on_every_link_capable_spelling_cut_from_the_fixture(mutant):
    assert walker_violations(FIXTURE.read_text(encoding="utf-8") + "\n" + mutant + "\n")


# ---- r10 adversary pass A1-A4 on eabdcc39 ----
def git_env(wd, env, *args):
    return subprocess.run(["git", "-C", str(wd), *args], capture_output=True, check=True, env=env).stdout


def test_the_global_configs_autocrlf_decides_what_is_dirt_exactly_as_it_does_for_git(repo, tmp_path):  # r10 adversary A1: the sealed git had no global config, so LF->CRLF looked like a change
    git(repo, "config", "--unset", "core.autocrlf")
    (repo / "a.txt").write_bytes(b"one\n")
    git(repo, "-c", "core.autocrlf=false", "commit", "-q", "-am", "lf")
    (repo / "a.txt").write_bytes(b"one\r\n")
    glob = tmp_path / "global.gitconfig"
    glob.write_text("[core]\n\tautocrlf = true\n")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(glob)}
    assert git_env(repo, env, "diff", "HEAD", "--binary") == b""  # git itself sees a clean tree
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 10 and saved(repo) == [], r.stdout + r.stderr  # old sealed code: exit 0 with a patch that only flips line endings


def test_a_local_excludes_file_is_not_replaced_by_the_default_ignore_file_in_the_sealed_git(repo, tmp_path):  # r10 adversary A2
    empty = tmp_path / "no-ignores"
    empty.write_text("")
    git(repo, "config", "core.excludesFile", empty.as_posix())
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "ignore").write_text("u.txt\n")
    (repo / "u.txt").write_text("u\n")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(tmp_path / "absent.gitconfig"), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    want = git_env(repo, env, "ls-files", "-z", "--others", "--exclude-standard")
    assert want == b"u.txt\0"
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 0, r.stdout + r.stderr  # old sealed code: 10, because the default ignore file hid u.txt
    assert next((tmp_path / "run").glob("*.untracked.txt")).read_bytes() == want


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS cannot hold a line feed in a directory name")
def test_a_posix_worktree_path_holding_a_line_feed_is_saved(tmp_path):  # r10 adversary A3: five paths shared one LF-separated answer
    base = tmp_path / "lds\ncase"
    base.mkdir()
    wd = base / "wt"
    init_repo(wd, b"one\n")
    (base / "run").mkdir()
    (wd / "a.txt").write_bytes(b"changed\n")
    r = save(wd, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert next((base / "run").glob("*.patch")).read_bytes() == git(wd, "diff", "HEAD", "--binary")


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete a directory holding a file another handle has open without delete sharing")
def test_a_private_git_dir_that_cannot_be_removed_is_named_not_swallowed(repo, tmp_path):  # r10 adversary A4
    (repo / "a.txt").write_bytes(b"changed\n")
    r = save_with_hook(repo, tmp_path, "$script:hold = [System.IO.File]::Open([System.IO.Path]::Combine($script:sealDir, 'config'), 'Open', 'Read', 'ReadWrite')")
    assert r.returncode == 0, r.stdout + r.stderr  # the patch is saved all the same
    left = last_receipt(r)["privateGitDirLeft"]
    assert Path(left).name.startswith("lane-dirty-save-") and repo.resolve() not in Path(left).resolve().parents  # r11 sol r10 B3: never inside the worktree or a git dir under it (the old assertion pinned it to .git)
    assert newest_patch(tmp_path) == git(repo, "diff", "HEAD", "--binary")


# ---- r10 adversary pass 2 on 7f29a91c ----
def test_a_boolean_git_accepts_in_another_spelling_and_a_path_with_non_ascii_still_decide_what_is_dirt(repo, tmp_path):  # pass 2 A1 (autocrlf = +1) and A2 (an attributes file by its own name)
    git(repo, "config", "--unset", "core.autocrlf")
    (repo / "a.txt").write_bytes(b"one\n")
    git(repo, "-c", "core.autocrlf=false", "commit", "-q", "-am", "lf")
    (repo / "a.txt").write_bytes(b"one\r\n")
    glob = tmp_path / "global.gitconfig"
    glob.write_text("[core]\n\tautocrlf = +1\n")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(glob)}
    assert git_env(repo, env, "diff", "HEAD", "--binary") == b""  # git reads +1 as true, so the CRLF file is clean
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 10 and saved(repo) == [], r.stdout + r.stderr  # pass-1 code dropped +1 and saved a spurious line-ending patch
    attrs = tmp_path / "attrs-é"  # not ASCII
    attrs.write_text("a.txt -text\n", encoding="utf-8")
    git(repo, "config", "core.attributesFile", str(attrs))
    git(repo, "config", "core.autocrlf", "true")
    want = git_env(repo, env, "diff", "HEAD", "--binary")
    assert want  # -text switches the conversion off, so now the CRLF is real dirt
    assert save(repo, "-LanePid", str(dead_pid()), env=env).returncode == 0
    assert newest_patch(tmp_path) == want


@pytest.mark.skipif(sys.platform == "win32", reason="a file name that is not UTF-8 needs a POSIX file system")
def test_an_attributes_file_whose_name_is_not_utf8_is_read_not_its_replacement_sibling(tmp_path):  # pass 2 A2: byte FF became U+FFFD, which names a different file
    base = tmp_path / "case"
    base.mkdir()
    wd = base / "wt"
    init_repo(wd, b"one\n")
    (base / "run").mkdir()
    (wd / "a.txt").write_bytes(b"one\r\n")
    real, sibling = os.fsencode(str(base)) + b"/attrs\xff", (str(base) + "/attrs\ufffd").encode("utf-8")
    Path(os.fsdecode(real)).write_text("a.txt -text\n")
    Path(os.fsdecode(sibling)).write_text("a.txt text\n")
    subprocess.run([b"git", b"-C", os.fsencode(str(wd)), b"config", b"core.autocrlf", b"true"], check=True)
    subprocess.run([b"git", b"-C", os.fsencode(str(wd)), b"config", b"core.attributesFile", real], check=True)
    want = git(wd, "diff", "HEAD", "--binary")
    assert want  # with `-text` the CRLF is dirt; with the sibling's `text` rule it would be normalised away
    r = save(wd, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert next((base / "run").glob("*.patch")).read_bytes() == want


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete a directory holding a file another handle has open without delete sharing")
def test_a_leftover_private_git_dir_is_named_even_when_the_artifacts_cannot_be_written(repo, tmp_path):  # pass 2 A3: exit 50 dropped the note
    (repo / "a.txt").write_bytes(b"changed\n")
    deny_writes(tmp_path / "run")
    try:
        r = save_with_hook(repo, tmp_path, "$script:hold = [System.IO.File]::Open([System.IO.Path]::Combine($script:sealDir, 'config'), 'Open', 'Read', 'ReadWrite')")
    finally:
        allow_writes(tmp_path / "run")
    assert r.returncode == 50 and "DIRTY_SAVE_WRITE_FAILED" in r.stdout and "privateGitDirLeft=" in r.stdout, r.stdout + r.stderr


# ---- r11: sol r10 B1-B3 on 16152f01 (three classes: comparison config = the caller's effective config; a warning is not completeness; the saver never observes itself) ----
def clean_git_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    env.update({"GIT_CONFIG_GLOBAL": str(tmp_path / "absent.gitconfig")})
    env.update(extra)
    return env


def commit_lf(repo, text=b"one\n"):
    git(repo, "config", "--unset", "core.autocrlf")
    (repo / "a.txt").write_bytes(text)
    git(repo, "-c", "core.autocrlf=false", "commit", "-q", "-am", "lf")


@pytest.mark.parametrize("how", ["env-count", "env-parameters", "system-file"])
def test_comparison_settings_the_caller_injects_decide_what_is_dirt_exactly_as_they_do_for_git(repo, tmp_path, how):  # r11 sol r10 B1: the injection was stripped before the effective config was read
    commit_lf(repo)
    (repo / "a.txt").write_bytes(b"one\r\n")
    if how == "env-count":
        git(repo, "config", "core.autocrlf", "true")  # the repository says true, the caller's environment says false
        env = clean_git_env(tmp_path, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="false")
    elif how == "env-parameters":
        git(repo, "config", "core.autocrlf", "true")
        env = clean_git_env(tmp_path, GIT_CONFIG_PARAMETERS="'core.autocrlf'='false'")
    else:
        system = tmp_path / "system.gitconfig"
        system.write_text("[core]\n\tautocrlf = false\n")
        env = clean_git_env(tmp_path, GIT_CONFIG_SYSTEM=str(system))
    want = git_env(repo, env, "diff", "HEAD", "--binary")
    assert want  # git itself sees the CRLF bytes as a change
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 0, r.stdout + r.stderr  # old code: 10 clean-tree (the injection was removed, the repository's true normalised the difference away)
    assert newest_patch(tmp_path) == want


def test_an_excludes_file_the_caller_injects_decides_which_untracked_names_are_listed(repo, tmp_path):  # r11 sol r10 B1 sibling: core.excludesFile through GIT_CONFIG_COUNT
    hide, show = tmp_path / "hide-u", tmp_path / "no-ignores"
    hide.write_text("u.txt\n")
    show.write_text("")
    git(repo, "config", "core.excludesFile", hide.as_posix())
    (repo / "u.txt").write_text("u\n")
    env = clean_git_env(tmp_path, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.excludesFile", GIT_CONFIG_VALUE_0=show.as_posix())
    want = git_env(repo, env, "ls-files", "-z", "--others", "--exclude-standard")
    assert want == b"u.txt\0"
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 0, r.stdout + r.stderr  # old code: 10, the repository's ignore file hid u.txt
    assert next((tmp_path / "run").glob("*.untracked.txt")).read_bytes() == want


def test_an_injection_git_cannot_read_is_a_typed_refusal_before_anything_is_collected(repo, tmp_path):  # r11 hub ruling B1: a setting that cannot be carried is refused, never collected under different settings
    (repo / "a.txt").write_bytes(b"changed\n")
    env = clean_git_env(tmp_path, GIT_CONFIG_COUNT="not-a-number")
    r = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert r.returncode == 40 and "DIRTY_SAVE_GIT_FAILED" in r.stdout and saved(repo) == [], r.stdout + r.stderr  # old code: stripped the variable and saved with exit 0


def deny_listing(path):
    if sys.platform == "win32":
        subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(RD)"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o000)


def allow_listing(path):
    if sys.platform == "win32":
        subprocess.run(["icacls", str(path), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o755)


ROOT_IGNORES_DIR_MODES = sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(ROOT_IGNORES_DIR_MODES, reason="root ignores directory permissions")
def test_an_unreadable_directory_is_an_incomplete_collection_never_a_clean_tree(repo, tmp_path):  # r11 sol r10 B2 (ACL fixture: icacls deny list on Windows, chmod 000 on POSIX)
    (repo / "hidden").mkdir()
    (repo / "hidden" / "draft.txt").write_text("d\n")
    deny_listing(repo / "hidden")
    try:
        r = save(repo, "-LanePid", str(dead_pid()))
    finally:
        allow_listing(repo / "hidden")
    assert r.returncode == 60, r.stdout + r.stderr  # old code: 10 DIRTY_SAVE_NOTHING reason=clean-tree
    assert "DIRTY_SAVE_NOTHING" not in r.stdout and "DIRTY_SAVE_INCOMPLETE" in r.stdout and "hidden" in r.stdout, r.stdout
    assert saved(repo) == []


@pytest.mark.skipif(ROOT_IGNORES_DIR_MODES, reason="root ignores directory permissions")
def test_visible_dirt_does_not_turn_an_incomplete_collection_into_unqualified_success(repo, tmp_path):  # r11 sol r10 B2: the warning rides in the receipt and the exit code
    (repo / "hidden").mkdir()
    (repo / "hidden" / "draft.txt").write_text("d\n")
    (repo / "a.txt").write_bytes(b"changed\n")
    (repo / "u.txt").write_text("u\n")
    deny_listing(repo / "hidden")
    try:
        r = save(repo, "-LanePid", str(dead_pid()))
    finally:
        allow_listing(repo / "hidden")
    assert r.returncode == 60, r.stdout + r.stderr  # old code: 0 with a receipt that says nothing was missed
    rec = last_receipt(r)
    assert rec["collection"] == "incomplete" and any("hidden" in w for w in rec["warnings"]), rec
    assert next((tmp_path / "run").glob("*.patch")).read_bytes() == git(repo, "diff", "HEAD", "--binary")  # what was readable is still saved
    assert (tmp_path / "run" / f"dirty-save-{head8(repo)}.untracked.txt").read_bytes() == b"u.txt\0"
    assert json.loads(next((tmp_path / "run").glob("*.receipt.json")).read_text()) == rec  # the persisted receipt carries it too


def test_a_line_ending_conversion_notice_is_not_an_incomplete_collection(repo, tmp_path):  # r11 B2 guard (green on the old code): git diff prints 'LF will be replaced by CRLF' with exit 0 for a file it did compare
    commit_lf(repo)
    git(repo, "config", "core.autocrlf", "true")
    (repo / "a.txt").write_bytes(b"one\nextra\n")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "collection" not in last_receipt(r)


def separate_git_dir_repo(tmp_path):
    base = tmp_path / "sep"
    base.mkdir()
    wd, admin = base / "wt", base / "wt" / "admin"
    wd.mkdir()
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={admin}", str(wd)], check=True, capture_output=True)
    for a in (["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["config", "core.autocrlf", "false"]):
        git(wd, *a)
    (wd / "a.txt").write_bytes(b"one\n")
    git(wd, "add", "a.txt")
    git(wd, "commit", "-q", "-m", "base")
    (wd / "u.txt").write_text("u\n")
    (base / "run").mkdir()
    return wd, base / "run"


@pytest.mark.parametrize("temp_inside_worktree", [False, True])
def test_the_saver_never_lists_its_own_temporary_files_and_a_repeat_save_is_byte_identical(tmp_path, temp_inside_worktree):  # r11 sol r10 B3: a separate git dir inside the worktree held the private git dir
    wd, run = separate_git_dir_repo(tmp_path)
    env = dict(os.environ)
    if temp_inside_worktree:  # the lane's own TEMP can sit in the worktree (it does on the hub VM): the saver must then use the RunDir, not it
        (wd / "tmpdir").mkdir()
        env.update({"TMP": str(wd / "tmpdir"), "TEMP": str(wd / "tmpdir"), "TMPDIR": str(wd / "tmpdir")})
    args = [PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(wd), "-RunDir", str(run), "-LanePid", str(dead_pid())]
    first = subprocess.run(args, capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    names_before = sorted(p.name for p in run.iterdir())
    second = subprocess.run(args, capture_output=True, text=True, env=env)
    assert second.returncode == 0 and second.stdout == first.stdout, second.stdout + second.stderr  # old code: a new GUID in the inventory, so new artifacts
    assert sorted(p.name for p in run.iterdir()) == names_before
    inventory = next(run.glob("*.untracked.txt")).read_bytes()
    assert b"lane-dirty-save-" not in inventory and b"u.txt" in inventory  # old code: the private git dir's HEAD, config and alternates were named
    assert [p for p in (wd / "admin").iterdir() if p.name.startswith("lane-dirty-save-")] == []
    if temp_inside_worktree:
        assert list((wd / "tmpdir").iterdir()) == []


# ---- r11 adversary pass A1-A3 on f8baa750 ----
def inventory_of(tmp_path):
    return next((tmp_path / "run").glob("*.untracked.txt")).read_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="administrative shares are Windows")
def test_a_temp_directory_reached_through_a_loopback_share_of_a_worktree_folder_is_not_used(repo, tmp_path):  # r11 adversary A1: the temp directory got a name compare only, so \\localhost\C$\...\wt\tmpdir looked like an outside directory
    (repo / "tmpdir").mkdir()
    (repo / "u.txt").write_text("u\n")
    unc = "\\\\localhost\\" + str(repo / "tmpdir").replace(":", "$", 1)
    if not os.path.isdir(unc):
        pytest.skip("the administrative share of this drive is not reachable by this user")
    r = save(repo, "-LanePid", str(dead_pid()), env={**os.environ, "TMP": unc, "TEMP": unc})
    assert r.returncode == 0, r.stdout + r.stderr
    assert b"lane-dirty-save-" not in inventory_of(tmp_path) and b"u.txt" in inventory_of(tmp_path)  # old code: the private git dir inside the worktree was listed


@pytest.mark.skipif(sys.platform == "win32", reason="a bind mount is a POSIX construct")
def test_a_temp_directory_that_is_a_bind_mount_of_a_worktree_folder_is_not_used(repo, tmp_path):  # r11 adversary A1, POSIX form
    inside, alias = repo / "tmpdir", tmp_path / "alias-temp"
    inside.mkdir(); alias.mkdir()
    pre = bind_mount(inside, alias)
    if pre is None:
        pytest.skip("cannot create a bind mount here (needs root or passwordless sudo)")
    try:
        (repo / "u.txt").write_text("u\n")
        r = save(repo, "-LanePid", str(dead_pid()), env={**os.environ, "TMPDIR": str(alias)})
        assert r.returncode == 0, r.stdout + r.stderr
        assert b"lane-dirty-save-" not in inventory_of(tmp_path)
    finally:
        subprocess.run([*pre, "umount", str(alias)], capture_output=True)


@pytest.mark.skipif(sys.platform == "win32" or ROOT_IGNORES_DIR_MODES, reason="a directory this user may not search is a POSIX mode, and root ignores it")
def test_a_rules_file_git_cannot_be_read_from_is_a_typed_refusal_not_a_silently_dropped_rule(tmp_path):  # r11 adversary A2: File.Exists answers false for an unreadable file, so its rules were dropped
    base = tmp_path / "case"
    base.mkdir()
    wd, admin = base / "wt", base / "wt.git"
    wd.mkdir()
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={admin}", str(wd)], check=True, capture_output=True)
    for a in (["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["config", "core.autocrlf", "true"]):
        git(wd, *a)
    (wd / "a.txt").write_bytes(b"one\n")
    git(wd, "add", "a.txt")
    git(wd, "-c", "core.autocrlf=false", "commit", "-q", "-m", "base")
    (admin / "info").mkdir(exist_ok=True)
    (admin / "info" / "attributes").write_text("a.txt -text\n")
    (wd / "a.txt").write_bytes(b"one\r\n")
    (base / "run").mkdir()
    os.chmod(admin / "info", 0o000)
    try:
        r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(wd), "-RunDir", str(base / "run"), "-LanePid", str(dead_pid())], capture_output=True, text=True)
    finally:
        os.chmod(admin / "info", 0o755)
    assert r.returncode == 40 and "DIRTY_SAVE_GIT_FAILED" in r.stdout and list((base / "run").iterdir()) == [], r.stdout + r.stderr  # old code: 10 clean-tree


@pytest.mark.skipif(sys.platform == "win32" or ROOT_IGNORES_DIR_MODES, reason="a parent this user may not search is a POSIX mode, and root ignores it")
def test_a_private_git_dir_whose_parent_cannot_be_searched_is_named_not_called_removed(repo, tmp_path):  # r11 adversary A2 cleanup sibling: Directory.Exists is false for it, which read as a clean removal
    base = tmp_path / "tmpbase"
    base.mkdir()
    (repo / "a.txt").write_bytes(b"changed\n")
    anchor = "$git.patch = Invoke-GitBytes"
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    hooked = tmp_path / "hooked-saver.ps1"
    hooked.write_text(text.replace(anchor, f"if ([System.IO.Path]::GetDirectoryName($script:sealDir) -ceq $env:LDS_BASE) {{ & chmod 000 $env:LDS_BASE }}; {anchor}"), encoding="utf-8")
    try:
        r = subprocess.run([PWSH, "-NoProfile", "-File", str(hooked), "-WorkDir", str(repo), "-RunDir", str(tmp_path / "run"), "-LanePid", str(dead_pid())],
                           capture_output=True, text=True, env={**os.environ, "TMPDIR": str(base), "LDS_BASE": str(base)})
    finally:
        os.chmod(base, 0o755)
    assert "privateGitDirLeft=" in r.stdout, r.stdout + r.stderr  # old code: nothing named, the directory was left behind
    shutil.rmtree(base, ignore_errors=True)


def run_ps_functions(names, expr, pre="", **env):
    prog = pre + ps_functions(*names) + f"[Console]::Out.Write({expr})"
    r = subprocess.run([PWSH, "-NoProfile", "-Command", prog], capture_output=True, env={**os.environ, **env})
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.decode("utf-8")


NOTICE_CRLF = "warning: CRLF will be replaced by LF in {name}.\nThe file will have its original line endings in your working directory\n"
NOTICE_LF = "warning: in the working copy of '{name}', LF will be replaced by CRLF the next time Git touches it\n"
DENIED = "warning: could not open directory 'hidden/': Permission denied\n"
NAME_WITH_TAIL = "x', LF will be replaced by CRLF the next time Git touches it"  # a legal file name on NTFS and POSIX


@pytest.mark.parametrize("stderr, want", [
    (NOTICE_CRLF.format(name="a.txt"), []),
    (NOTICE_LF.format(name="a.txt"), []),
    (NOTICE_CRLF.format(name="a\nb.txt"), []),  # r11 adversary A3: the file name holds a line feed, so the notice spans three lines
    (NOTICE_LF.format(name="a\nb.txt"), []),
    (NOTICE_LF.format(name="a.txt") + DENIED + NOTICE_CRLF.format(name="a\nb.txt"), [DENIED.strip()]),
    (DENIED, [DENIED.strip()]),
    ("The file will have its original line endings in your working directory\n", ["The file will have its original line endings in your working directory"]),  # an orphan second line is not a notice
    ("warning: LF will be replaced by CRLF in a.txt. but something else\n", ["warning: LF will be replaced by CRLF in a.txt. but something else"]),
    (NOTICE_LF.format(name=NAME_WITH_TAIL), []),  # r12 sol r11 B2: the name holds the notice's own tail text; the old lazy match ended inside it and left a fragment
    (NOTICE_LF.format(name=NAME_WITH_TAIL) + DENIED + NOTICE_LF.format(name=NAME_WITH_TAIL), [DENIED.strip()]),  # a warning between two such notices is neither swallowed nor hidden
    (NOTICE_LF.format(name=NAME_WITH_TAIL + "\nb.txt"), []),  # r12 adversary A1: a name holding a line feed AND the tail text; the tail closes a line in the middle of the name
    (NOTICE_CRLF.format(name="a.\nThe file will have its original line endings in your working directory\nb.txt"), []),  # the same for the two-line form
    (NOTICE_LF.format(name=NAME_WITH_TAIL + "\nb.txt") + DENIED + NOTICE_LF.format(name=NAME_WITH_TAIL + "\nc.txt"), [DENIED.strip()]),  # a warning between two of them is still the only warning
    (NOTICE_CRLF.format(name="x. a.txt"), []),
    ("warning: x', LF will be replaced by CRLF the next time Git touches it\n", ["warning: x', LF will be replaced by CRLF the next time Git touches it"]),  # the tail alone is no notice
    ("warning: in the working copy of 'a', LF will be replaced by CRLF the next time Git touches it extra\n", ["warning: in the working copy of 'a', LF will be replaced by CRLF the next time Git touches it extra"]),  # a notice ends at its line end
    ("junk warning: in the working copy of 'a', LF will be replaced by CRLF the next time Git touches it\n", ["junk warning: in the working copy of 'a', LF will be replaced by CRLF the next time Git touches it"]),  # and begins at its line start
])
def test_only_git_s_line_ending_notices_are_dropped_and_they_are_matched_over_the_whole_text(stderr, want):  # r11 adversary A3 (platform-independent: a file name with a line feed cannot exist on NTFS)
    out = run_ps_functions(["Get-WarningLines"], "(ConvertTo-Json -Compress -InputObject @(Get-WarningLines @($env:LDS_ERR)))", LDS_ERR=stderr)
    assert json.loads(out) == want


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS cannot hold a line feed in a file name")
def test_a_tracked_name_with_a_line_feed_does_not_make_the_conversion_notice_a_warning(repo, tmp_path):  # r11 adversary A3 end to end
    commit_lf(repo)
    git(repo, "config", "core.autocrlf", "true")
    odd = repo / "a\nb.txt"
    odd.write_bytes(b"one\n")
    git(repo, "add", "-A")
    git(repo, "-c", "core.autocrlf=false", "commit", "-q", "-m", "odd")
    odd.write_bytes(b"one\nextra\n")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0 and "collection" not in last_receipt(r), r.stdout + r.stderr  # old code: 60, the notice was split into two unmatched fragments


@pytest.mark.skipif(sys.platform != "win32", reason="git walks into a junction on Windows; on POSIX it lists a symlink as one file")
def test_a_junction_inside_the_worktree_to_the_temp_directory_cannot_make_the_saver_list_itself(repo, tmp_path):  # r11 adversary pass 2 A1 (and A2: any alias that appears after the placement check): where the private dir is cannot be proven unreachable, so what git lists is also filtered
    outside = tmp_path / "outside-temp"
    outside.mkdir()
    link_dir(repo / "jtemp", outside)
    (repo / "u.txt").write_text("u\n")
    env = {**os.environ, "TMP": str(outside), "TEMP": str(outside)}
    first = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert b"lane-dirty-save-" not in inventory_of(tmp_path) and b"u.txt" in inventory_of(tmp_path)  # old code: jtemp/lane-dirty-save-<guid>/HEAD ... were listed
    before = saved(repo)
    second = save(repo, "-LanePid", str(dead_pid()), env=env)
    assert second.returncode == 0 and second.stdout == first.stdout and saved(repo) == before  # old code: a new GUID, so a new artifact set


@pytest.mark.parametrize("raw, own, want", [
    (b"a.txt\0lane-dirty-save-0f\0lane-dirty-save-0f/HEAD\0x/lane-dirty-save-0f/objects/info/alternates\0y/lane-dirty-save-0f\0u.txt\0", "lane-dirty-save-0f", b"a.txt\0u.txt\0"),
    (b"lane-dirty-save-0f/HEAD\0", "lane-dirty-save-0f", b""),
    (b"lane-dirty-save-0fx/HEAD\0lane-dirty-save-0\0xlane-dirty-save-0f/a\0", "lane-dirty-save-0f", b"lane-dirty-save-0fx/HEAD\0lane-dirty-save-0\0xlane-dirty-save-0f/a\0"),  # only a whole path component is the saver's own
    (b"caf\xc3\xa9\0u\xffv\0", "lane-dirty-save-0f", b"caf\xc3\xa9\0u\xffv\0"),  # names that are not ASCII (or not UTF-8) pass through byte for byte
    (b"", "lane-dirty-save-0f", b""),
])
def test_the_savers_own_directory_is_removed_from_the_untracked_names_by_whole_path_component(raw, own, want):  # r11 adversary pass 2 A1/A2
    out = run_ps_functions(["Remove-OwnNames"], "([System.BitConverter]::ToString((Remove-OwnNames ([System.Convert]::FromHexString($env:LDS_RAW)) $env:LDS_OWN)))", LDS_RAW=raw.hex().upper(), LDS_OWN=own)
    assert out == "-".join(f"{b:02X}" for b in want)


def test_a_directory_is_reported_present_unless_it_is_really_gone(tmp_path):  # r11 adversary A2 guard (runs everywhere; the unsearchable-parent case is the POSIX test above)
    here, gone = tmp_path / "here", tmp_path / "gone"
    here.mkdir()
    out = run_ps_functions(["Test-DirPresent"], "('' + (Test-DirPresent $env:LDS_A) + ',' + (Test-DirPresent $env:LDS_B))", LDS_A=str(here), LDS_B=str(gone))
    assert out == "True,False"


# ---- r12: sol r11 B1-B4 on 288f6820 (path identity is decided after link resolution or not at all; a notice is a whole line; the saver never observes itself, by identity; names are compared byte for byte) ----
WIN = sys.platform == "win32"
JUNCTION_HOOK = "[void](New-Item -ItemType Junction -Path ([System.IO.Path]::Combine($WorkDir.Replace('/', '\\'), 'alias')) -Target \"{target}\")"  # git prints the toplevel with forward slashes, which cmd's mklink reads as switches


@pytest.mark.parametrize("raw, want", [
    ("/base/link/../child.json", True),
    ("C:/base/link/../child.json", True),
    ("../child.json", True),
    ("/base/link/..", True),
    ("/base/./link/child.json", False),
    ("/base/..x/x../child.json", False),
    ("/base/\u00ad../child.json", False),  # culture compares ignore U+00AD: these are names, not parent segments
    ("/base/..\u00ad/child.json", False),
    ("/base/.../child.json", WIN),  # Win32 trims a segment of only dots and spaces; a POSIX name '...' is a name
    ("/base/.. /child.json", WIN),
    ("C:\\base\\link\\..\\child.json", WIN),  # a backslash separates segments on Windows only
    ("\\\\?\\C:\\base\\..\\child.json", WIN),
])
def test_a_parent_segment_in_a_path_is_found_before_any_normalisation(raw, want):  # r12 sol r11 B1 (platform-independent unit)
    out = run_ps_functions(["Test-HasDotDot"], "('' + (Test-HasDotDot $env:LDS_P))", pre="$script:onWin = [System.IO.Path]::DirectorySeparatorChar -eq [char]92; ", LDS_P=raw)
    assert out == str(want)


@pytest.mark.parametrize("which", ["child-json", "work-dir", "run-dir"])
def test_a_path_with_a_parent_segment_is_bad_arguments_and_nothing_is_saved(repo, tmp_path, which):  # r12 sol r11 B1: the old saver normalised the path lexically and went on to collect (RED), the new one refuses before any read
    dirty(repo)
    (tmp_path / "sub").mkdir()
    child = tmp_path / "child.json"
    child.write_text(json.dumps({"pid": dead_pid(), "createdUtc": "2000-01-01T00:00:00Z"}))
    work, run, cj = str(repo), str(tmp_path / "run"), str(child)
    if which == "child-json":
        cj = str(tmp_path / "sub" / ".." / "child.json")
    elif which == "work-dir":
        work = str(tmp_path / "sub" / ".." / "wt")
    else:
        run = str(tmp_path / "sub" / ".." / "run")
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", work, "-RunDir", run, "-ChildJson", cj], capture_output=True, text=True)
    assert r.returncode == 30 and "reason=dotdot-path" in r.stdout, r.stdout + r.stderr
    assert saved(repo) == []
    ok = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(repo), "-RunDir", str(tmp_path / "run"), "-ChildJson", str(child)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr  # a plain absolute path still works


@pytest.mark.skipif(WIN, reason="Windows collapses a parent segment lexically in the OS too; only POSIX follows the link first")
def test_a_symlink_followed_by_a_parent_segment_cannot_select_a_dead_decoy_record_for_a_live_lane(repo, tmp_path):  # r12 sol r11 B1 end to end (POSIX; never run on the Windows hub VM, first run is ubuntu CI)
    dirty(repo)
    target, base = tmp_path / "target", tmp_path / "base"
    (target / "sub").mkdir(parents=True)
    base.mkdir()
    os.symlink(target / "sub", base / "link", target_is_directory=True)
    (target / "child.json").write_text(json.dumps({"pid": os.getpid(), "createdUtc": "2099-01-01T00:00:00Z"}))  # the OS opens this one: a live lane
    (base / "child.json").write_text(json.dumps({"pid": dead_pid(), "createdUtc": "2000-01-01T00:00:00Z"}))  # the lexical reading opens this one: a dead decoy
    r = save(repo, "-ChildJson", str(base / "link" / ".." / "child.json"))
    assert r.returncode in (20, 30) and saved(repo) == [], r.stdout + r.stderr  # old code: 0, the live lane was saved


def test_a_file_name_that_holds_the_notice_tail_does_not_make_the_conversion_notice_a_warning(repo, tmp_path):  # r12 sol r11 B2 end to end (the name is legal on NTFS too)
    commit_lf(repo)
    git(repo, "config", "core.autocrlf", "true")
    odd = repo / NAME_WITH_TAIL
    odd.write_bytes(b"one\n")
    git(repo, "add", "-A")
    git(repo, "-c", "core.autocrlf=false", "commit", "-q", "-m", "odd")
    odd.write_bytes(b"one\nextra\n")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0 and "collection" not in last_receipt(r), r.stdout + r.stderr  # old code: 60, a fragment of the name was counted as a warning


@pytest.mark.skipif(WIN, reason="NTFS cannot hold a line feed in a file name; the parser cases above carry the Windows evidence")
def test_a_file_name_with_a_line_feed_and_the_notice_tail_does_not_make_the_conversion_notice_a_warning(repo, tmp_path):  # r12 adversary A1 end to end (POSIX; never run on the Windows hub VM, first run is ubuntu CI)
    commit_lf(repo)
    git(repo, "config", "core.autocrlf", "true")
    odd = repo / (NAME_WITH_TAIL + "\nb.txt")
    odd.write_bytes(b"one\n")
    git(repo, "add", "-A")
    git(repo, "-c", "core.autocrlf=false", "commit", "-q", "-m", "odd")
    odd.write_bytes(b"one\nextra\n")
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0 and "collection" not in last_receipt(r), r.stdout + r.stderr


@pytest.mark.parametrize("caller", [None, "true", "warn"])
def test_the_private_git_dir_silences_line_ending_notices_at_the_source_whatever_the_caller_sets(repo, tmp_path, caller):  # r12 adversary pass 2 A1: a notice holds the raw file name, so text can imitate the next message; core.safecrlf only decides whether git SAYS something about a conversion, never the bytes it compares
    if caller:
        git(repo, "config", "core.safecrlf", caller)
    (repo / "u.txt").write_text("u\n")
    out = tmp_path / "private-config.txt"
    r = save_with_hook(repo, tmp_path, f"Copy-Item -LiteralPath ([System.IO.Path]::Combine($script:sealDir, 'config')) -Destination '{out}'")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = out.read_text(encoding="latin-1")
    assert re.search(r"^\s*safecrlf = false\s*$", cfg, re.MULTILINE) and not re.search(r"safecrlf = \"?(?:true|warn)", cfg), cfg  # old code: absent, or the caller's value


@pytest.mark.skipif(not WIN, reason="git walks into a directory junction on Windows; on POSIX it lists a symlink as one file")
def test_a_junction_made_straight_to_the_private_directory_cannot_put_the_savers_files_in_the_inventory(repo, tmp_path):  # r12 sol r11 B3: alias/HEAD, alias/config, alias/objects/info/alternates carry no GUID component
    (repo / "u.txt").write_text("u\n")
    hook = JUNCTION_HOOK.format(target="$($script:sealDir)")  # between the private directory's creation and ls-files, no sleep race
    r = save_with_hook(repo, tmp_path, hook)
    assert r.returncode == 0, r.stdout + r.stderr
    inventory = inventory_of(tmp_path)
    assert b"alias" not in inventory and b"u.txt" in inventory, inventory  # old code: alias/HEAD, alias/config, alias/objects/info/alternates were named, then deleted


@pytest.mark.skipif(not WIN, reason="git walks into a directory junction on Windows")
def test_a_junction_to_a_folder_inside_the_private_directory_is_also_the_savers_own(repo, tmp_path):  # r12 sol r11 B3 class: the target is inside, not the directory itself
    (repo / "u.txt").write_text("u\n")
    hook = JUNCTION_HOOK.format(target="$($script:sealDir)\\objects")
    r = save_with_hook(repo, tmp_path, hook)
    assert r.returncode == 0, r.stdout + r.stderr
    assert b"alias" not in inventory_of(tmp_path), inventory_of(tmp_path)


@pytest.mark.skipif(not WIN, reason="git walks into a directory junction on Windows")
def test_a_junction_to_some_other_folder_stays_in_the_inventory(repo, tmp_path):  # r12 B3 guard: only the savers own files are dropped
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("k\n")
    link_dir(repo / "alias", other)
    r = save(repo, "-LanePid", str(dead_pid()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert b"alias/keep.txt" in inventory_of(tmp_path)


@pytest.mark.parametrize("raw, own, want", [
    (b"lane-dirty-save-0f\xad\0", "lane-dirty-save-0f", b"lane-dirty-save-0f\xad\0"),  # r12 sol r11 B4: the name is the saver's own plus a soft hyphen byte, a different name
    (b"lane-dirty-save-0f\xad/HEAD\0x/lane-dirty-save-0f\xad\0", "lane-dirty-save-0f", b"lane-dirty-save-0f\xad/HEAD\0x/lane-dirty-save-0f\xad\0"),
    (b"lane-dirty-save-0f\0", "lane-dirty-save-0f", b""),  # the exact name is still dropped
])
def test_a_name_that_differs_from_the_savers_own_only_by_an_ignorable_byte_is_kept(raw, own, want):  # r12 sol r11 B4 (a POSIX name may hold byte AD on its own; the helper is platform-independent)
    out = run_ps_functions(["Remove-OwnNames"], "([System.BitConverter]::ToString((Remove-OwnNames ([System.Convert]::FromHexString($env:LDS_RAW)) $env:LDS_OWN)))", LDS_RAW=raw.hex().upper(), LDS_OWN=own)
    assert out == "-".join(f"{b:02X}" for b in want)


def test_no_name_in_the_saver_is_compared_by_culture():  # r12 sol r11 B4 sweep pin: the linguistic operators and string overloads ignore U+00AD and treat some control characters specially
    code = "\n".join(re.sub(r"\s#\s.*$", "", ln) for ln in SCRIPT.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"\s-(?:ceq|ccontains|clike|like|notlike)\s", code), "use [string]::Equals(a, b, [System.StringComparison]::Ordinal), Array.IndexOf, or a StartsWith/EndsWith with an explicit comparison"
    for m in re.finditer(r"\.(?:StartsWith|EndsWith)\(([^)]*)\)", code):
        assert "StringComparison" in m.group(1) or "[char]" in m.group(1), m.group(0)  # a bare string argument is a culture compare
    assert not re.search(r"\.IndexOf\(\s*[\"']", code)  # a string IndexOf is a culture search; use the char overload
    assert not re.search(r"\s-i?eq\s+['\"][A-Za-z_.]", code)  # an equality operator against a quoted name


# ---- r13: sol r12 F1-F3 on ec99ed20. The class "the saver computes a path's physical identity and the computation can be fooled" ends by REFUSING: only fully qualified arguments, no link anywhere in a chain, and an identity probe that fails is unknown, never "unrelated" ----
def child_record(tmp_path):
    child = tmp_path / "child.json"
    child.write_text(json.dumps({"pid": dead_pid(), "createdUtc": "2000-01-01T00:00:00Z"}))
    return child


def run_saver(work, run, child=None, cwd=None, env=None):
    args = [PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", str(work), "-RunDir", str(run)]
    args += ["-ChildJson", str(child)] if child is not None else ["-LanePid", str(dead_pid())]
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd, env=env)


@pytest.mark.parametrize("which", ["child-json", "work-dir", "run-dir"])
@pytest.mark.parametrize("form", ["relative", "root-relative", "drive-relative", "drive-relative-parent"])
def test_a_path_that_is_not_fully_qualified_is_bad_arguments_and_nothing_is_saved(repo, tmp_path, which, form):  # r13 sol r12 F1: `C:..\wt` has no `..` segment (its first segment is `C:..`), yet GetFullPath resolved it against the drive's current directory and collection went on (r12 saver: exit 0)
    if form != "relative" and not WIN:
        pytest.skip("root-relative and drive-relative are Windows path forms; on POSIX they are plain relative names, covered by form=relative")
    dirty(repo)
    (tmp_path / "sub").mkdir()
    child = child_record(tmp_path)
    real = {"work-dir": repo, "run-dir": tmp_path / "run", "child-json": child}[which]
    drive = os.path.splitdrive(str(real))[0]
    spelled, cwd = {
        "relative": (real.name, tmp_path),
        "root-relative": (str(real)[len(drive):], tmp_path),
        "drive-relative": (drive + real.name, tmp_path),
        "drive-relative-parent": (drive + "..\\" + real.name, tmp_path / "sub"),
    }[form]
    given = {"work-dir": str(repo), "run-dir": str(tmp_path / "run"), "child-json": str(child)}
    given[which] = spelled
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT), "-WorkDir", given["work-dir"], "-RunDir", given["run-dir"], "-ChildJson", given["child-json"]], capture_output=True, text=True, cwd=cwd)
    assert r.returncode == 30 and "reason=not-fully-qualified" in r.stdout, r.stdout + r.stderr
    assert saved(repo) == []
    ok = run_saver(repo, tmp_path / "run", child)
    assert ok.returncode == 0, ok.stdout + ok.stderr  # the fully qualified spelling of the same three paths still works


@pytest.mark.parametrize("which", ["work-dir", "run-dir", "child-json"])
def test_a_link_in_any_argument_chain_is_refused_not_resolved(repo, tmp_path, which):  # r13 sol r12 F2 (Windows junction / POSIX symlink): the old saver resolved the link and went on (Windows: Get-FinalDir, POSIX: ResolveLinkTarget); no link is resolved now
    dirty(repo)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    child = jobs / "child.json"
    child.write_text(json.dumps({"pid": dead_pid(), "createdUtc": "2000-01-01T00:00:00Z"}))
    link = tmp_path / "link"
    target = {"work-dir": repo, "run-dir": tmp_path / "run", "child-json": jobs}[which]
    link_dir(link, target)
    work, run, cj = repo, tmp_path / "run", child
    if which == "work-dir":
        work = link
    elif which == "run-dir":
        run = link
    else:
        cj = link / "child.json"
    r = run_saver(work, run, cj)
    assert r.returncode == 30 and "reason=link-in-path" in r.stdout, r.stdout + r.stderr  # r12 saver: 0
    assert list((tmp_path / "run").iterdir()) == []


@pytest.mark.skipif(WIN, reason="a symlink whose TARGET holds a parent segment needs a POSIX symlink (Windows junction targets are absolute and normalised at creation); the chain-walk unit below feeds the same shape through a stubbed attribute reader on every platform")
def test_a_symlink_target_holding_a_parent_segment_cannot_move_the_artifacts_to_an_outside_decoy(repo, tmp_path):  # r13 sol r12 F2 end to end (POSIX; never run on the Windows hub VM, first run is ubuntu CI): hop -> wt/sub, runlink -> hop/../evidence. The OS selects wt/evidence, ResolveLinkTarget().FullName gave the outside decoy
    dirty(repo)
    (repo / "sub").mkdir()
    (repo / "evidence").mkdir()
    decoy = tmp_path / "evidence"
    decoy.mkdir()
    hop, runlink = tmp_path / "hop", tmp_path / "runlink"
    os.symlink(repo / "sub", hop, target_is_directory=True)
    os.symlink(str(hop) + "/../evidence", runlink, target_is_directory=True)
    r = run_saver(repo, runlink)
    assert r.returncode == 30 and "reason=link-in-path" in r.stdout, r.stdout + r.stderr  # r12 saver: 0, the artifacts were written through the decoy
    assert list(decoy.iterdir()) == [] and list((repo / "evidence").iterdir()) == []


def test_the_private_base_is_never_reached_through_a_link_and_the_run_dir_is_used_instead(repo, tmp_path):  # r13 sol r12 F2 private-base sibling: TEMP/TMPDIR through a link was accepted when its final target was outside the worktree (and a link target holding `..` could put the private dir INSIDE it)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "templink"
    link_dir(link, elsewhere)
    (repo / "u.txt").write_text("u\n")
    rec = tmp_path / "seal-dir.txt"
    env = {**os.environ, "TMP": str(link), "TEMP": str(link), "TMPDIR": str(link)}
    r = save_with_hook(repo, tmp_path, f"Set-Content -LiteralPath '{rec}' -Value $script:sealDir -NoNewline", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    parent = Path(rec.read_text()).parent
    assert os.path.normcase(os.path.realpath(parent)) == os.path.normcase(os.path.realpath(tmp_path / "run")), parent  # r12 saver: the link
    assert list(elsewhere.iterdir()) == []


CHAIN_STUB = "{ param($c) if ($c.EndsWith('runlink')) { [System.IO.FileAttributes]::Directory -bor [System.IO.FileAttributes]::ReparsePoint } elseif ($c.EndsWith('boom')) { throw 'denied' } else { [System.IO.FileAttributes]::Directory } }"


@pytest.mark.parametrize("leaf, want", [("plain", "ok"), ("runlink", "link-in-path"), ("boom", "path-unreadable")])
def test_the_chain_walk_refuses_a_link_or_an_unreadable_component_read_through_a_stubbed_attribute_reader(tmp_path, leaf, want):  # r13 sol r12 F2 on every platform: the POSIX layout `runlink -> hop/../evidence` cannot be built on Windows, so the attribute reader is stubbed (a link is a ReparsePoint attribute; an unreadable component throws)
    p = str(tmp_path / "case" / "hop" / leaf / "evidence")
    expr = "$(try { Assert-LinkFree $env:LDS_P " + CHAIN_STUB + "; 'ok' } catch { $_.Exception.Message })"
    out = run_ps_functions(["Assert-LinkFree", "ConvertTo-NativePath"], expr, pre="$script:onWin = [System.IO.Path]::DirectorySeparatorChar -eq [char]92; ", LDS_P=p)
    assert out == want


def test_a_directory_that_is_read_and_is_not_a_link_is_false_and_one_that_cannot_be_read_is_unknown(tmp_path):  # r13 sol r12 F3: the probe has THREE states; the catch used to answer false
    (tmp_path / "sub").mkdir()
    probe = "('' + (Test-ResolvesInto $env:LDS_W 'sub' 'P') + ',' + $(if ($null -eq (Test-ResolvesInto $env:LDS_W 'gone' 'P')) { 'unknown' } else { 'known' }))"
    assert run_ps_functions(["Test-ResolvesInto"], probe, LDS_W=str(tmp_path)) == "False,unknown"  # r12 saver: False,known


def test_names_below_a_directory_whose_alias_probe_cannot_be_read_are_kept_and_the_failure_is_named():  # r13 sol r12 F3: unknown is neither "unrelated" nor silent
    pre = ("$script:onWin = $true; $script:probeWarnings = @(); function Get-FinalDir { @{ Path = 'P'; Handle = [System.IO.MemoryStream]::new() } }; "
           "function Test-ResolvesInto { param($w, $d, $p) if ($d -eq 'bad') { $null } elseif ($d -eq 'alias') { $true } else { $false } }; ")
    raw = b"alias/HEAD\0bad/x\0bad/y\0ok/z\0"
    expr = "(([System.Text.Encoding]::Latin1.GetString((Remove-AliasedNames ([System.Convert]::FromHexString($env:LDS_RAW)) 'W' 'P')) -replace \"`0\", ',') + '|' + ($script:probeWarnings -join ';'))"
    out = run_ps_functions(["Remove-AliasedNames"], expr, pre=pre, LDS_RAW=raw.hex().upper())
    assert out == "bad/x,bad/y,ok/z,|alias-inspection-failed dir=bad"  # one warning per directory, not per name


FAIL_ALIAS_PROBE = "$script:origFinal = ${function:Get-FinalDir}; function Get-FinalDir([string]$Path) { if ($Path.EndsWith('\\alias', [System.StringComparison]::Ordinal)) { throw 'cannot-open-directory' }; & $script:origFinal $Path }"  # the injection seam: only the alias directory's identity read fails, as a peer holding an incompatible handle would make it


@pytest.mark.skipif(not WIN, reason="git walks into a directory junction on Windows; on POSIX it lists a symlink as one file")
def test_an_alias_whose_identity_cannot_be_read_makes_the_collection_incomplete_not_complete(repo, tmp_path):  # r13 sol r12 F3 end to end: alias/HEAD, alias/config, alias/objects/info/alternates survived clean-up in a complete receipt with exit 0
    (repo / "u.txt").write_text("u\n")
    hook = JUNCTION_HOOK.format(target="$($script:sealDir)") + "; " + FAIL_ALIAS_PROBE
    r = save_with_hook(repo, tmp_path, hook)
    assert r.returncode == 60, r.stdout + r.stderr  # r12 saver: 0
    receipt = last_receipt(r)
    assert receipt["collection"] == "incomplete" and any("alias-inspection-failed dir=alias" in w for w in receipt["warnings"]), receipt
    assert b"u.txt" in inventory_of(tmp_path)


def test_no_identity_or_ownership_probe_in_the_saver_answers_false_from_a_bare_catch():  # r13 sol r12 F3 sweep pin: a failed read is unknown. The remaining catches that return a boolean are Test-DirPresent (unknown = present: the leftover is named) and the typed-exception arm that says a directory is really gone
    code = "\n".join(re.sub(r"\s#\s.*$", "", ln) for ln in SCRIPT.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"catch\s*\{\s*return\s+\$false\s*\}", code)
    assert "Remove-AliasedNames" in code and "probeWarnings" in code
