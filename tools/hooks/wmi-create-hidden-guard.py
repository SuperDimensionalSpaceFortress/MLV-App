"""PreToolUse guard: a detached Win32_Process Create must be hidden (WMI-CREATE-HIDDEN-GUARD-1, owner-approved 2026-10-21).

A Win32_Process Create without ProcessStartupInformation ShowWindow=0 starts a cmd.exe parented to WmiPrvSE that shows a
visible console on the owner's desktop (four of them on 2026-10-21 ~08:12Z, from lane-written run-dir scripts such as
swap-driver.ps1; 173 in two days on 2026-10-19). The fix is one helper: .claude-state\\fleet-runs\\hub-tools\\Start-HiddenDetached.ps1.

The rule mirrors hub-tools\\Test-WmiCreateHidden.ps1: a code line (text after ' #' ignored) that holds Win32_Process (not
Win32_ProcessStartup), the word Create and a call construct (Invoke-CimMethod / Invoke-WmiMethod / [wmiclass] / .Create( ) passes
only when ShowWindow appears within the 2 lines before or the 3 lines after it.

CODE vs PROSE (r2, PR #547). Only code is scanned:
  * Write `content`, Edit `new_string`, NotebookEdit `new_source` are scanned only when the target is an executable script type
    (SCRIPT_EXTS). Markdown, .txt, .json, .yml and the like are prose and always allowed, so a prompt or ledger line may quote
    the forbidden launch. A markdown NotebookEdit cell is prose too. Files named Test-*.ps1 or test_*.py are fixtures: allowed.
  * Bash / PowerShell `command` is scanned as code, one shell segment at a time (split on ; & && | || and newlines outside
    quotes). A segment is DATA, and is not scanned, when its command only reads or reports text: a search or read command
    (grep, rg, findstr, Select-String, cat, Get-Content, echo ...), `git commit|log|show|diff|grep ...`, `gh pr|issue ...`, or
    Add-LedgerLine.ps1. A segment stops being data when it redirects with `>`, sits in a pipeline that feeds a writer or an
    interpreter (Set-Content, tee, pwsh, python ...), or holds a `$(` substitution. An unbalanced quote disables every exemption.
Contract (declared for WMI-CREATE-HIDDEN-GUARD-HOOK-1 r3; the code below must say exactly this, and a difference is a defect in the code):
Parser: a quote-aware tokenizer, not an AST. Bash/PowerShell command text is split outside quotes on semicolon, ampersand,
double ampersand, pipe, double pipe and newline, and each segment is judged on its own: DENY when Win32_Process and Create
appear within the window with no ShowWindow=0. Data segments are not scanned: read and search commands, git
commit/log/show/diff/grep/blame/tag/notes, gh pr/issue, Add-LedgerLine.ps1. A data segment is code again when it redirects
with a greater-than sign, sits in a pipeline with a writer or interpreter sink, or holds a dollar-paren substitution (except
the commit heredoc idiom). An unbalanced quote disables every exemption. Write/Edit/NotebookEdit content is scanned only for
executable script types (.ps1 .psm1 .psd1 .bat .cmd .py .js .mjs .cjs .vbs .sh and .ipynb code cells); Test-* and test_*
fixtures are exempt.
Non-promises: it does not catch a non-zero ShowWindow value or a ShowWindow comment in the window; Win32_Process and Create
split across lines (backtick continuation or splatting); the class held in a variable
([wmiclass] then .Create()); a computed method name; -EncodedCommand; a space-hash literal hiding the call; an Edit that
removes only the ShowWindow line; Edit fragments judged without file context; real launchers named Test-* or test_*.
These are WMI-GUARD-EVASIONS-1 debt; the run-dir scanner meter wmi-create-offenders is the second layer.

Exit 2 blocks (the message goes to the model); exit 0 allows. Any unparseable input allows (fail-open: this is a lint, and the
never-authorized gate stays the safety boundary).
"""
import json
import re
import sys

BEFORE, AFTER = 2, 3
SEGMENT_MARK = "\x00"  # code_view puts this where a ; & && | || separator was, so each segment is judged on its own
SCRIPT_EXTS = (".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".py", ".js", ".mjs", ".cjs", ".vbs", ".sh", ".ipynb")
DATA_COMMANDS = frozenset((
    "grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "cat", "type", "get-content", "gc",
    "head", "tail", "ls", "dir", "get-childitem", "gci", "echo", "write-output", "write-host", "printf",
))
SINK_COMMANDS = frozenset((
    "set-content", "sc", "add-content", "ac", "out-file", "tee", "tee-object", "invoke-expression", "iex",
    "sh", "bash", "pwsh", "powershell", "cmd", "python", "py", "node",
))
GIT_DATA = frozenset(("grep", "log", "show", "diff", "commit", "blame", "tag", "notes"))
GH_DATA = frozenset(("pr", "issue"))
HELPER = r"C:\!Layi Wkspc\MLV-App\.claude-state\fleet-runs\hub-tools\Start-HiddenDetached.ps1"

_PROC = re.compile(r"(?i)\bWin32_Process\b(?!Startup)")
_CREATE = re.compile(r"(?i)\bCreate\b")
_CALL = re.compile(r"(?i)Invoke-(Cim|Wmi)Method|\[wmiclass\]|\.Create\(")
_SHOW = re.compile(r"(?i)ShowWindow")
_COMMENT = re.compile(r"(^|\s)#.*$")
_HEREDOC = re.compile(r"""<<-?[ \t]*(['"]?)([A-Za-z_]\w*)\1""")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_HEAD = re.compile(r"\"([^\"]*)\"\s*|'([^']*)'\s*|(\S+)\s*")
_HEREDOC_SUBST = re.compile(r"""\$\(\s*cat\s+<<-?[ \t]*(['"]?)([A-Za-z_]\w*)\1""")
_LEDGER = re.compile(
    r"""(?i)^(?:&\s*)?(?:(?:pwsh|powershell)(?:\.exe)?\s+(?:-\w+\s+(?:[^\s-]\S*\s+)?)*-File\s+)?"""
    r"""(?:"(?:[^"]*[\\/])?Add-LedgerLine\.ps1"|(?:[^\s"']*[\\/])?Add-LedgerLine\.ps1(?=\s|$))"""
)


def offending_lines(text):
    """Return 1-based line numbers of Win32_Process Create calls with no ShowWindow nearby."""
    lines = text.splitlines() or [text]
    out = []
    for i, raw in enumerate(lines):
        # Every segment holding Create closes a launch group (the segments since the previous Create) and is judged on its own:
        # ShowWindow in a neighbouring line counts; in another segment of the line it does not, so a hidden Create cannot mask a later one.
        group = []
        for seg in _COMMENT.sub("", raw).split(SEGMENT_MARK):
            group.append(seg)
            if not _CREATE.search(seg):
                continue
            joined, group = " ".join(group), []
            if not (_PROC.search(joined) and _CALL.search(joined)):
                continue
            lo, hi = max(0, i - BEFORE), min(len(lines) - 1, i + AFTER)
            near = [ln.replace(SEGMENT_MARK, " ") for ln in lines[lo:i]] + [seg] + [ln.replace(SEGMENT_MARK, " ") for ln in lines[i + 1:hi + 1]]
            if not _SHOW.search("\n".join(near)):
                out.append(i + 1)
                break
    return out


def split_shell(text):
    """Split on ; & && | || and newlines outside quotes. Rows are [segment, separator_after, has_redirect].

    A heredoc body stays in the segment that opened it. Returns None when a quote is left open.
    """
    rows, cur, quote, heredocs = [], [], None, []
    redirect = False
    i, n = 0, len(text)

    def cut(sep):
        nonlocal redirect
        rows.append(["".join(cur), sep, redirect])
        cur.clear()
        redirect = False

    while i < n:
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "`" and quote == '"' and i + 1 < n:
                cur.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
            i += 1
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            m = _HEREDOC.match(text, i)
            if m:
                heredocs.append(m.group(2))
                cur.append(m.group(0))
                i = m.end()
                continue
        if ch in "'\"":
            quote = ch
        elif ch == "\n":
            if heredocs:
                m = re.search(r"(?m)^[ \t]*%s[ \t]*$" % re.escape(heredocs.pop(0)), text[i + 1:])
                end = i + 1 + m.end() if m else n
                cur.append(text[i:end])
                i = end
                continue
            cut("\n")
            i += 1
            continue
        elif ch == ";":
            cut(";")
            i += 1
            continue
        elif ch == "|":
            sep = "||" if text.startswith("||", i) else "|"
            cut(sep)
            i += len(sep)
            continue
        elif ch == "&":
            prev = text[i - 1] if i else ""
            if text.startswith("&&", i):
                cut("&&")
                i += 2
                continue
            if "".join(cur).strip() and prev != ">" and not text.startswith("&>", i):
                cut("&")
                i += 1
                continue
        elif ch == ">":
            if not (text.startswith(">&", i) or (i and text[i - 1] == "2")):
                redirect = True
        cur.append(ch)
        i += 1
    if quote:
        return None
    cut("")
    return rows


def _first_words(segment):
    """The command word (a quoted interpreter path counts as the command) followed by the rest with quoted operands as Q."""
    rest = segment.strip()
    while True:
        m = _HEAD.match(rest)
        if not m:
            return []
        word = next(g for g in m.groups() if g is not None)
        if m.group(3) is not None and word in ("&", ".", "command", "sudo"):
            rest = rest[m.end():]
            continue
        return [word] + _QUOTED.sub("Q", rest[m.end():]).split()


def _command_name(word):
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _subcommand(words):
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
        elif word in ("-C", "-c"):
            skip = True
        elif not word.startswith("-"):
            return word.lower()
    return ""


def _without_commit_heredocs(segment):
    """The segment with each complete commit-heredoc substitution (dollar-paren cat heredoc ... terminator, close paren) removed."""
    while True:
        m = _HEREDOC_SUBST.search(segment)
        if not m:
            return segment
        end = re.search(r"(?m)^[ \t]*%s[ \t]*$" % re.escape(m.group(2)), segment[m.end():])
        tail = segment[m.end() + end.end():].lstrip() if end else ""
        if not tail.startswith(")"):
            return segment  # unterminated: not the idiom, so its dollar-paren stays visible
        segment = segment[:m.start()] + tail[1:]


def is_data_segment(segment):
    """True when the segment only reads or reports text, so its quoted operands are data."""
    if "$(" in _without_commit_heredocs(segment):
        return False
    if _LEDGER.match(segment.strip()):
        return True
    words = _first_words(segment)
    if not words:
        return False
    name = _command_name(words[0])
    if name in DATA_COMMANDS:
        return True
    if name == "git":
        return _subcommand(words) in GIT_DATA
    if name == "gh":
        return _subcommand(words) in GH_DATA
    return False


def is_sink_segment(segment):
    words = _first_words(segment)
    return bool(words) and _command_name(words[0]) in SINK_COMMANDS and not _LEDGER.match(segment.strip())


def code_view(command):
    """The command text with data segments blanked (newlines kept, so line windows still line up)."""
    rows = split_shell(command)
    if rows is None:
        return command
    out, group = [], []
    for row in rows:
        group.append(row)
        if row[1] == "|":
            continue
        sink = any(r[2] or is_sink_segment(r[0]) for r in group)
        for seg, sep, _ in group:
            out.append("\n" * seg.count("\n") if not sink and is_data_segment(seg) else seg)
            out.append(sep if sep in ("\n", "") else SEGMENT_MARK)
        group = []
    return "".join(out)


def notebook_code(text):
    """The code-cell sources of a whole .ipynb document; any other text (a fragment, bad JSON) is returned unchanged."""
    try:
        cells = json.loads(text)["cells"]
        sources = [c["source"] for c in cells if c.get("cell_type") == "code"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return text
    return "\n".join(s if isinstance(s, str) else "".join(s) for s in sources)


def decide(payload):
    """Return a block message, or None to allow."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool in ("Bash", "PowerShell"):
        text = code_view(ti.get("command") or "")
    elif tool in ("Write", "Edit", "NotebookEdit"):
        path = (ti.get("file_path") or ti.get("notebook_path") or "").replace("\\", "/")
        name = path.rsplit("/", 1)[-1].lower()
        if name.startswith("test-") or name.startswith("test_"):
            return None
        if not name.endswith(SCRIPT_EXTS) or ti.get("cell_type") == "markdown":
            return None
        text = ti.get("content") or ti.get("new_string") or ti.get("new_source") or ""
        if name.endswith(".ipynb"):
            text = notebook_code(text)
    else:
        return None
    bad = offending_lines(text)
    if not bad:
        return None
    return ("WMI-CREATE-HIDDEN-GUARD: a Win32_Process Create without ShowWindow=0 opens a VISIBLE console on the owner's desktop "
            "(line %s of this %s). Launch detached processes only through the helper, one launch per call:\n"
            "  pwsh -NoProfile -File \"%s\" -Exe <exe> -ArgumentList <a>,<b> -StdoutPath <out> -StderrPath <err>\n"
            "Do not hand-write Invoke-CimMethod / Invoke-WmiMethod / [wmiclass] Win32_Process Create, even with ShowWindow=0. "
            "Do not reshape the call to pass this check; use the helper." % (", ".join(str(n) for n in bad), "command" if tool in ("Bash", "PowerShell") else "file content", HELPER))


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    msg = decide(payload)
    if msg:
        sys.stderr.write(msg + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
