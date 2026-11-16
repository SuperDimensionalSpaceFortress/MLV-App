"""DOCTRINE-OUTBOX-WMI-SCANNER-DENY-BY-DEFAULT-1: a tracked PowerShell script may not name the WMI process class without a reviewed allowlist row.

A process made by WMI Win32_Process Create is not a child of the caller: its parent is WmiPrvSE, so it inherits none of the error mode Invoke-Lane sets, and a
crash in it can raise a modal box on the owner's desktop (trap card doctrine-outbox/20261114-wmi-create-child-inherits-gpfault-error-box.md). The first answer
was an AST rule scanner (98eee615, PR 815): 30 adversary passes over 12 rounds found 90 major bypasses and the trend never fell, because every rule leaked to a
new spelling. This guard changes the SHAPE: it denies the NAME by default and lets a person add a row.

GRAMMAR (what this test reads)
  1. Files: every git-tracked file whose name ends in .ps1 or .psm1, in any letter case (`git ls-files`, the index, with every GIT_* variable but GIT_EXEC_PATH
     dropped so a hook's GIT_DIR cannot point it at another repository).
  2. Text: the file's bytes with NUL bytes, backticks, quote characters and a backtick-newline continuation removed, read as Latin-1. So UTF-16 / UTF-32 with or
     without a BOM, `Win32_Pro`cess` and `Win32_Pro"cess"` read as the plain name. Comments, strings and here-strings are NOT skipped: a name anywhere counts.
  3. Refused name (case-insensitive substring): `win32_process` (this also reads Win32_Processor, Win32_ProcessStartup and Win32_ProcessTrace), `wmic` (this also
     reads `wmiclass`, the [wmiclass] cast), and a wildcard or character-class prefix of the class name (`Win32_Proc*`, `Win32[_]Process`, `Win32_P%`, `Win32*`).
  4. A file that names one is refused unless tools/coordination/wmi-process-allowlist.txt holds a row `<repo-relative path> | <reason>` for it. The reason is
     `query-only` or `sanctioned-launcher` and nothing else; `sanctioned-launcher` is accepted only for a file named Start-HiddenDetached.ps1.
  5. The allowlist is checked too: a malformed or duplicate row, a path that is not spelled the way git lists it, a path that is not a tracked .ps1 / .psm1, a
     reason outside the two words, and a STALE row (the file no longer names a refused name) each fail the test.
  6. Defence in depth: every `query-only` file is also run through tests/coordination/Test-LaunchSites.ps1, the repository's PowerShell-AST launch-site scanner,
     and a Win32_Process Create site (its rule D2: Invoke-CimMethod / Invoke-WmiMethod with Create, a .Create(...) call on a WMI class, a [wmiclass] cast)
     fails the test. That layer needs pwsh and is skipped without it; the deny-by-default layer needs only Python and git.

NOT covered (declared, not promised)
  - A name built from pieces: `'Win32_' + 'Process'`, `-f`, `[string]::Join`, `$env:` values, reflection. The name is never literal in the file.
  - Text produced at run time and then evaluated: Invoke-Expression, [scriptblock]::Create, a decoded -EncodedCommand payload, a downloaded script.
  - A class reached without naming it: a WQL meta_class / __CLASS / __DERIVATION selector, `Get-CimClass *`, `Get-WmiObject -List` with no name.
  - Files that are not .ps1 / .psm1: .cmd, .bat, .py, .cs, .psd1, .ps1xml, workflow YAML, and untracked or gitignored files (the lane scripts under
    .claude-state are guarded board-side).
  - Encodings that hide ASCII: UTF-7, a code page that does not keep ASCII bytes, a ZIP or gzip inside a tracked file.
  - A `query-only` row for a file that does call Create in a way the AST scanner (rule D2) cannot see, for example a method name assembled at run time. The row
    is a person's claim and the AST layer a second look, not a proof.
  - Whether a `sanctioned-launcher` file still has the sanctioned shape: no copy is tracked today, so the row exists only so the shape has a place to go.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / "tools" / "coordination" / "wmi-process-allowlist.txt"
SITE_SCANNER = Path(__file__).resolve().with_name("Test-LaunchSites.ps1")
REASONS = ("query-only", "sanctioned-launcher")
LAUNCHER_NAME = "Start-HiddenDetached.ps1"
PWSH = shutil.which("pwsh")

# The class name, a wildcard / character-class prefix of it, and wmic (which also begins `wmiclass`). Applied to text already reduced by _readable().
_REFUSED = re.compile(r"win32_process|win32(?:_|\[_\])(?:p(?:r(?:o(?:c(?:e(?:s)?)?)?)?)?)?[*?%\[]|win32[*?%\[]|wmic", re.IGNORECASE)
# A site the AST scanner reports as `detached` that is a WMI create (rule D2) and not a hidden Start-Process (rule D1).
_D2_SITE = re.compile(r"create|win32_process|wmiclass|managementclass|invoke-(?:cim|wmi)method|\bicim\b|\biwmi\b", re.IGNORECASE)


def _readable(raw):
    """The text a reader sees once PowerShell's own spellings are undone: NUL bytes (UTF-16 / UTF-32), a backtick-newline continuation, backticks, quotes."""
    text = raw.replace(b"\x00", b"").decode("latin-1")
    text = re.sub(r"`\r?\n", "", text)
    return text.replace("`", "").replace("'", "").replace('"', "")


def names_the_process_class(raw):
    return _REFUSED.search(_readable(raw)) is not None


def _git_env():
    """Every GIT_* variable dropped except GIT_EXEC_PATH, so `git -C root` reads ROOT's repository and not the one a hook exported (GIT_DIR, GIT_INDEX_FILE ...),
    and with the ordinary pathspec rules (GIT_LITERAL_PATHSPECS ...) and config (GIT_CONFIG_*)."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") or k.upper() == "GIT_EXEC_PATH"}


def tracked_powershell(root=ROOT):
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", ":(icase)*.ps1", ":(icase)*.psm1"], capture_output=True, check=True, env=_git_env()).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def parse_allowlist(text):
    """({path: reason}, [problem]) from the allowlist text. A line is blank, a # comment, or `<path> | <reason>`."""
    rows, problems = {}, []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            problems.append(f"allowlist line {number}: not `<path> | <reason>`: {line!r}")
            continue
        path, reason = parts
        if path in rows:
            problems.append(f"allowlist line {number}: {path} is listed twice")
            continue
        if reason not in REASONS:
            problems.append(f"allowlist line {number}: {path}: reason {reason!r} is not one of {', '.join(REASONS)}")
        rows[path] = reason
    return rows, problems


def deny_by_default_problems(root, tracked, allow_text):
    """Every way the tree under `root` breaks the rule: [problem text]. `tracked` is the repo-relative listing, `allow_text` the allowlist file's text."""
    rows, problems = parse_allowlist(allow_text)
    listed = set(tracked)
    naming = set()
    for rel in tracked:
        try:
            raw = (Path(root) / rel).read_bytes()
        except OSError as exc:
            problems.append(f"{rel}: tracked but unreadable ({exc.__class__.__name__}): a script that cannot be read cannot be shown clean")
            continue
        if names_the_process_class(raw):
            naming.add(rel)
            if rel not in rows:
                problems.append(f"{rel}: names Win32_Process or wmic and has no row in tools/coordination/wmi-process-allowlist.txt "
                                "(read it first: a WMI Create is not `query-only`; launch through Start-HiddenDetached.ps1)")
    for path, reason in rows.items():
        if path not in listed:
            problems.append(f"allowlist row {path}: not a tracked .ps1 / .psm1 file (missing, renamed, untracked, another type, or not spelled as git lists it): stale")
        elif path not in naming:
            problems.append(f"allowlist row {path}: the file no longer names Win32_Process or wmic: stale, delete the row")
        elif reason == "sanctioned-launcher" and Path(path).name != LAUNCHER_NAME:
            problems.append(f"allowlist row {path}: `sanctioned-launcher` is only for a file named {LAUNCHER_NAME}")
    return problems


def _decode_for_scanner(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def create_site_problems(root, query_only):
    """Defence in depth: the AST scanner's WMI create sites (rule D2) in the `query-only` files, [problem text]. Raises when the scanner itself fails: a
    layer that cannot scan never passes."""
    sources = {rel: _decode_for_scanner((Path(root) / rel).read_bytes()) for rel in query_only}
    if not sources:
        return []
    done = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SITE_SCANNER)], input=json.dumps(sources), capture_output=True, text=True,
                          encoding="utf-8", timeout=900)
    assert done.returncode == 0, f"launch-site scanner failed (exit {done.returncode}): {done.stderr[-2000:]}"
    found = json.loads(done.stdout)
    problems = []
    for rel in sorted(sources):
        for site in found[rel]:
            if site["kind"] == "detached" and _D2_SITE.search(site["text"]):
                problems.append(f"{rel}:{site['line']}: a `query-only` file has a WMI Create site: {site['text'][:160]!r}")
    return problems


def _query_only(allow_text):
    return sorted(p for p, r in parse_allowlist(allow_text)[0].items() if r == "query-only")


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# GREEN: the real tree
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_tracked_tree_names_the_process_class_only_in_allowlisted_files():
    problems = deny_by_default_problems(ROOT, tracked_powershell(), ALLOWLIST.read_text(encoding="utf-8"))
    assert problems == [], "\n".join(problems)


@pytest.mark.skipif(PWSH is None, reason="the Create-site layer reads PowerShell's own AST and needs pwsh")
def test_no_query_only_file_has_a_wmi_create_site():
    allow = ALLOWLIST.read_text(encoding="utf-8")
    problems = create_site_problems(ROOT, _query_only(allow))
    assert problems == [], "\n".join(problems)


def test_the_allowlist_is_sorted_so_a_review_sees_one_row_per_file():
    paths = [p for p, _ in (line.split(" | ") for line in ALLOWLIST.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#"))]
    assert len(paths) == len(set(paths)), "a file is listed twice"
    assert paths, "the seeded allowlist is empty"


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# RED / GREEN fixtures: written under a throwaway tempfile directory, never in the repository tree
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def tree():
    with tempfile.TemporaryDirectory(prefix="wmi-allowlist-") as raw:
        root = Path(raw)

        def put(rel, content):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
            return rel

        put.root = root
        yield put


NAMING_SPELLINGS = [
    pytest.param(b"Get-CimInstance Win32_Process\n", id="plain"),
    pytest.param(b"Get-CimInstance WIN32_PROCESS\n", id="upper"),
    pytest.param(b"# Win32_Process only in a comment\n", id="comment"),
    pytest.param(b"$q = 'select * from Win32_Process'\n", id="string"),
    pytest.param(b"@'\nWin32_Process\n'@\n", id="here-string"),
    pytest.param(b"Get-CimInstance Win32_Processor\n", id="processor-class"),
    pytest.param(b"Get-CimInstance Win32_ProcessStartup\n", id="startup-class"),
    pytest.param(b"wmic process call create 'cmd.exe'\n", id="wmic"),
    pytest.param(b"WMIC.EXE process get name\n", id="wmic-exe-upper"),
    pytest.param(b"[wmiclass]'root\\cimv2:Win32_Proc'\n", id="wmiclass-cast"),
    pytest.param(b"Get-CimInstance Win32_Pro`cess\n", id="backtick-escape"),
    pytest.param(b"Get-CimInstance Win32_Pro`\ncess\n", id="backtick-newline"),
    pytest.param(b'Get-CimInstance Win32_Pro"cess"\n', id="spliced-quotes"),
    pytest.param(b'Get-CimInstance "Win32_""Process"\n', id="adjacent-quotes"),
    pytest.param(b"Get-WmiObject -List Win32_Proc*\n", id="wildcard-prefix"),
    pytest.param(b"Get-WmiObject -List Win32[_]Process\n", id="char-class-underscore"),
    pytest.param(b"Get-WmiObject -List Win32_Pro[c]ess\n", id="char-class-letter"),
    pytest.param(b"Get-WmiObject -List Win32*\n", id="win32-wildcard"),
    pytest.param("Get-CimInstance Win32_Process\n".encode("utf-16"), id="utf16-le-bom"),
    pytest.param("Get-CimInstance Win32_Process\n".encode("utf-16-le"), id="utf16-le-no-bom"),
    pytest.param("Get-CimInstance Win32_Process\n".encode("utf-16-be"), id="utf16-be-no-bom"),
    pytest.param("Get-CimInstance Win32_Process\n".encode("utf-32"), id="utf32-bom"),
    pytest.param(b"\xef\xbb\xbfGet-CimInstance Win32_Process\n", id="utf8-bom"),
]


@pytest.mark.parametrize("content", NAMING_SPELLINGS)
def test_red_an_unlisted_script_that_names_the_class_is_refused(tree, content):
    rel = tree("tools/new/probe.ps1", content)
    problems = deny_by_default_problems(tree.root, [rel], "")
    assert len(problems) == 1 and problems[0].startswith(f"{rel}: names Win32_Process or wmic"), problems


@pytest.mark.parametrize("rel", ["tools/new/Probe.PS1", "tools/new/Mod.PSM1", "tools/new/mod.psm1", "tools/new/Mixed.Ps1"])
def test_red_the_refusal_does_not_depend_on_the_extension_case(tree, rel):
    tree(rel, b"Get-CimInstance Win32_Process\n")
    assert deny_by_default_problems(tree.root, [rel], "")


def test_green_scripts_that_do_not_name_the_class_pass_without_a_row(tree):
    rels = [tree("a.ps1", b"Get-CimInstance Win32_OperatingSystem\n"), tree("b.psm1", b"Get-Process | Select-Object -First 1\n"),
            tree("c.ps1", b"# the word Win32 alone, and 'Win 32_Process' with a space, are not the class\n$x = 'Win 32_Process'\n")]
    assert deny_by_default_problems(tree.root, rels, "") == []


def test_green_a_listed_script_passes(tree):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    assert deny_by_default_problems(tree.root, [rel], f"# note\n\n{rel} | query-only\n") == []


def test_red_a_stale_row_for_a_file_that_no_longer_names_the_class_is_refused(tree):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_OperatingSystem\n")
    problems = deny_by_default_problems(tree.root, [rel], f"{rel} | query-only\n")
    assert len(problems) == 1 and "no longer names Win32_Process or wmic: stale" in problems[0], problems


def test_red_a_stale_row_for_a_file_that_is_gone_is_refused(tree):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    problems = deny_by_default_problems(tree.root, [rel], f"{rel} | query-only\ntools/new/deleted.ps1 | query-only\n")
    assert len(problems) == 1 and problems[0].startswith("allowlist row tools/new/deleted.ps1: not a tracked") and "stale" in problems[0], problems


@pytest.mark.parametrize("path", ["tools/new/Probe.ps1", "tools\\new\\probe.ps1", "./tools/new/probe.ps1", "tools/new/../new/probe.ps1", "/tools/new/probe.ps1"])
def test_red_a_row_must_spell_the_path_as_git_lists_it(tree, path):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    problems = deny_by_default_problems(tree.root, [rel], f"{path} | query-only\n")
    assert any(p.startswith(f"allowlist row {path}: not a tracked") for p in problems) and any(p.startswith(f"{rel}: names") for p in problems), problems


def test_red_a_row_for_a_file_that_is_not_a_tracked_script_is_refused(tree):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    tree("notes.txt", b"Win32_Process\n")
    problems = deny_by_default_problems(tree.root, [rel], f"{rel} | query-only\nnotes.txt | query-only\n")
    assert len(problems) == 1 and problems[0].startswith("allowlist row notes.txt: not a tracked"), problems


@pytest.mark.parametrize("reason", ["", "read-only", "Query-Only", "query_only", "query-only extra", "launcher", "sanctioned", "ok", "sanctioned-launcher,query-only"])
def test_red_a_reason_that_is_not_one_of_the_two_words_is_refused(tree, reason):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    problems = deny_by_default_problems(tree.root, [rel], f"{rel} | {reason}\n")
    assert any(p.startswith("allowlist line 1:") for p in problems), problems


@pytest.mark.parametrize("line", ["tools/new/probe.ps1", "tools/new/probe.ps1 query-only", "tools/new/probe.ps1 | query-only | extra", " | query-only", "tools/new/probe.ps1 |"])
def test_red_a_malformed_row_is_refused(tree, line):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    problems = deny_by_default_problems(tree.root, [rel], line + "\n")
    assert any("allowlist line 1: not `<path> | <reason>`" in p for p in problems), problems


def test_red_a_file_listed_twice_is_refused(tree):
    rel = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    problems = deny_by_default_problems(tree.root, [rel], f"{rel} | query-only\n{rel} | query-only\n")
    assert problems == [f"allowlist line 2: {rel} is listed twice"], problems


def test_the_launcher_reason_is_only_for_the_sanctioned_launcher_file(tree):
    other = tree("tools/new/probe.ps1", b"Get-CimInstance Win32_Process\n")
    launcher = tree("hub-tools/Start-HiddenDetached.ps1", b"Invoke-CimMethod -ClassName Win32_Process -MethodName Create\n")
    assert deny_by_default_problems(tree.root, [launcher], f"{launcher} | sanctioned-launcher\n") == []
    problems = deny_by_default_problems(tree.root, [other], f"{other} | sanctioned-launcher\n")
    assert len(problems) == 1 and "only for a file named Start-HiddenDetached.ps1" in problems[0], problems


def test_red_a_tracked_script_that_cannot_be_read_is_refused(tree):
    problems = deny_by_default_problems(tree.root, ["tools/new/missing.ps1"], "")
    assert len(problems) == 1 and "tracked but unreadable" in problems[0], problems


def test_the_allowlist_comment_and_blank_lines_are_ignored():
    rows, problems = parse_allowlist("# c\n\n   \n  a/b.ps1 |  query-only  \n")
    assert rows == {"a/b.ps1": "query-only"} and problems == []


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def _tmp_repo(tree, names):
    for name in names:
        tree(name, b"# x\n")
    subprocess.run(["git", "init", "-q", str(tree.root)], check=True, capture_output=True, env=_git_env())
    subprocess.run(["git", "-C", str(tree.root), "add", "-A"], check=True, capture_output=True, env=_git_env())


def test_the_listing_reads_any_extension_case_and_no_other_extension(tree):
    _tmp_repo(tree, ["lower.ps1", "UPPER.PS1", "Mixed.Ps1", "mod.psm1", "Other.PSM1", "sub/deep.PS1", "notes.txt", "x.ps1.bak", "data.psd1"])
    assert tracked_powershell(tree.root) == ["Mixed.Ps1", "Other.PSM1", "UPPER.PS1", "lower.ps1", "mod.psm1", "sub/deep.PS1"]


def test_the_listing_ignores_every_git_variable_a_hook_exports(tree, monkeypatch):
    _tmp_repo(tree, ["real.ps1"])
    decoy = tempfile.mkdtemp(prefix="wmi-allowlist-decoy-")
    try:
        subprocess.run(["git", "init", "-q", decoy], check=True, capture_output=True, env=_git_env())
        monkeypatch.setenv("GIT_DIR", str(Path(decoy) / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", decoy)
        monkeypatch.setenv("GIT_INDEX_FILE", str(Path(decoy) / ".git" / "index"))
        monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
        assert tracked_powershell(tree.root) == ["real.ps1"]
    finally:
        shutil.rmtree(decoy, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# Defence in depth: the AST layer, for files the allowlist calls query-only
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------

_needs_pwsh = pytest.mark.skipif(PWSH is None, reason="the Create-site layer reads PowerShell's own AST and needs pwsh")

CREATE_SPELLINGS = [
    pytest.param("Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = 'cmd.exe' }\n", id="invoke-cimmethod"),
    pytest.param("Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList 'cmd.exe'\n", id="invoke-wmimethod"),
    pytest.param("([wmiclass]'Win32_Process').Create('cmd.exe')\n", id="wmiclass-create"),
    pytest.param("$c = Get-WmiObject -List Win32_Process\n$c.Create('cmd.exe')\n", id="variable-create"),
    pytest.param("(Get-CimClass -ClassName Win32_Process) | Invoke-CimMethod -MethodName Create -Arguments @{ CommandLine = 'x' }\n", id="pipeline-create"),
]


@_needs_pwsh
@pytest.mark.parametrize("source", CREATE_SPELLINGS)
def test_red_a_query_only_file_that_starts_calling_create_is_refused(tree, source):
    rel = tree("tools/new/probe.ps1", source)
    problems = create_site_problems(tree.root, [rel])
    assert problems and all(p.startswith(f"{rel}:") and "WMI Create site" in p for p in problems), problems


@_needs_pwsh
def test_green_a_query_only_file_that_only_reads_is_accepted(tree):
    rel = tree("tools/new/probe.ps1", "Get-CimInstance Win32_Process -Filter \"ProcessId=1\" | Select-Object Name\n"
                                      "$o = Invoke-CimMethod -InputObject $p -MethodName GetOwner\n"
                                      "[scriptblock]::Create('1')\n[IO.File]::Create($f)\n# Invoke-CimMethod -MethodName Create in a comment\n")
    assert create_site_problems(tree.root, [rel]) == []
