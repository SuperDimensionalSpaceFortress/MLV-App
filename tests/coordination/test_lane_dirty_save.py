"""LANE-DIRTY-SAVE-CORE-1: tools/coordination/lane-dirty-save.ps1 keeps a dead lane's uncommitted work.
Exit codes: 0 saved, 10 nothing to save, 20 lane alive (refused), 30 bad arguments, 40 git failed."""
import datetime, json, os, shutil, subprocess, sys
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
    assert listed.read_bytes() == git(repo, "ls-files", "--others", "--exclude-standard")
    assert listed.read_bytes() == b"untracked.txt\n"
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
    assert "café.txt\n".encode() in next((tmp_path / "run").glob("*.untracked.txt")).read_bytes()


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