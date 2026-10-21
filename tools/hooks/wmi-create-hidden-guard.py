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
Parser: a quote-aware tokenizer, not an AST. Comments are stripped before the exemption grammar runs: an unquoted # that
starts a word runs to the end of its line (PowerShell <#...#> blocks too), so a quote or heredoc opener inside one opens
nothing, and a PowerShell # that may or may not start a comment disables every exemption. Bash/PowerShell command text is split outside quotes on semicolon, ampersand,
double ampersand, pipe, double pipe and newline, and each segment is judged on its own: DENY when Win32_Process and Create
appear within the window with no ShowWindow=0. Data segments are not scanned: read and search commands, git
commit/log/show/diff/grep/blame/tag/notes, gh pr/issue, Add-LedgerLine.ps1. A data segment is code again when it redirects
with a greater-than sign, sits in a pipeline with a writer or interpreter sink, or holds a dollar-paren substitution (except
the quoted-delimiter commit heredoc idiom). An unbalanced quote disables every exemption. Write/Edit/NotebookEdit content is scanned only for
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
    "sh", "bash", "pwsh", "powershell", "cmd", "python", "python3", "py", "node", "wscript", "cscript",
))
GIT_DATA = frozenset(("grep", "log", "show", "diff", "commit", "blame", "tag", "notes"))
GH_DATA = frozenset(("pr", "issue"))
HELPER = r"C:\!Layi Wkspc\MLV-App\.claude-state\fleet-runs\hub-tools\Start-HiddenDetached.ps1"

_PROC = re.compile(r"(?i)\bWin32_Process\b(?!Startup)")
_CREATE = re.compile(r"(?i)\bCreate\b")
_CALL = re.compile(r"(?i)Invoke-(Cim|Wmi)Method|\[wmiclass\]|\.Create\(")
_SHOW = re.compile(r"(?i)ShowWindow")
_COMMENT = re.compile(r"(^|\s)#.*$")
_HEREDOC_META = ";&|<>()"  # an unquoted one of these ends a heredoc delimiter word, as whitespace does
_WORD_BREAK = " \t\n;&|()"  # an unquoted # at the start of the text or after one of these starts a comment
_BASH_WORD_BREAK = _WORD_BREAK + "<>"  # bash also starts a word after a redirection operator (echo x >#f is a syntax error)
_PS_WORD_BREAK = _WORD_BREAK + "\r{},'\""  # PowerShell also starts a token after a brace, a comma or a closing quote
_PS_WORD = "_$`#<\\"  # with letters and digits: a # after one of these is inside a PowerShell word
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_HEAD = re.compile(r"\"([^\"]*)\"\s*|'([^']*)'\s*|(\S+)\s*")
_SUBST_CAT = re.compile(r"\$\(\s*cat\s+(?=<<)")  # the head of the commit-message idiom; _heredoc_opener reads the delimiter
_CALL_OP = re.compile(r"""(?:&|\.)(?=['"])|&(?=[^\s&>|;])""")  # call operator or dot-source glued to the command word
_FD_DUP = re.compile(r">&[ \t]*(?:\d+-?|-)(?=[\s;&|)]|$)")
_NULL_TARGET = re.compile(r">>?[ \t]*(?:/dev/null|\$null|nul)(?![\w./\\-])", re.I)
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


def _writes_file(text, i):
    """True when the greater-than sign at text[i] sends output to a file (any fd, >>, >|, &>, *>, >& WORD).

    fd duplication (>&2, 2>&1, >&-) is not a file write, and neither is stderr (2>) sent to the null device.
    """
    if i and text[i - 1] == ">":
        return False  # the second sign of >>: the first one decided
    if _FD_DUP.match(text, i):
        return False
    if i and text[i - 1] == "2" and (i == 1 or text[i - 2] in " \t\n;&|("):
        return not _NULL_TARGET.match(text, i)
    return True


def _heredoc_opener(text, i):
    """Read the heredoc operator at text[i] (<< or <<-) as bash does: (tag, literal, strip_tabs, end), or None.

    The delimiter word runs to unquoted whitespace or an unquoted ; & | < > ( ), so punctuation stays in the word.
    Quote removal gives the tag, and any quoted part ('x', "x", \\x) makes the body literal. None for a here-string,
    a missing word, or a quote left open (the caller then treats the text as ordinary command text).
    """
    n = len(text)
    j = i + 2
    if text.startswith("<", j):
        return None
    strip = text.startswith("-", j)
    j += strip
    while j < n and text[j] in " \t":
        j += 1
    tag, literal = [], False
    while j < n and text[j] not in " \t\n" and text[j] not in _HEREDOC_META:
        ch = text[j]
        if ch == "'":
            k = text.find("'", j + 1)
            if k < 0:
                return None
            tag.append(text[j + 1:k])
            literal, j = True, k + 1
        elif ch == '"':
            k, part = j + 1, []
            while k < n and text[k] != '"':
                if text[k] == "\\" and k + 1 < n and text[k + 1] in '"\\$`':
                    k += 1
                part.append(text[k])
                k += 1
            if k >= n:
                return None
            tag.append("".join(part))
            literal, j = True, k + 1
        elif ch == "\\":
            if j + 1 >= n or text[j + 1] == "\n":
                return None
            tag.append(text[j + 1])
            literal, j = True, j + 2
        else:
            tag.append(ch)
            j += 1
    if not tag:
        return None
    return "".join(tag), literal, strip, j


def _heredoc_end(text, pos, tag, strip):
    """Index just past the terminator line of the heredoc whose body starts at pos, or -1: the exact line, with leading tabs only for <<-."""
    m = re.compile(r"(?m)^%s%s$" % ("\t*" if strip else "", re.escape(tag))).search(text, pos)
    return m.end() if m else -1


def _escaped(text, j, esc):
    """True when an odd run of the escape character esc stands right before text[j]."""
    k = j
    while k > 0 and text[k - 1] == esc:
        k -= 1
    return (j - k) % 2 == 1


def _starts_comment(text, i, powershell):
    """Whether the unquoted text[i] (a #, or the < of a PowerShell <#) starts a comment: True, False, or None when unknown.

    A comment starts a word: at the start of the text, or after unquoted, unescaped whitespace or ; & | ( ). The escape is the
    backslash in bash and the backtick in PowerShell, and an escaped newline (a line continuation) is skipped first. Bash also
    starts a word after < or >. PowerShell also starts a token after a brace (not ${, a braced variable name), a comma or a
    closing quote. A # glued to anything else
    in PowerShell (= > + ...) is a comment in expression mode and a word character in argument mode: None, so the caller
    fails closed.
    """
    esc = "`" if powershell else "\\"
    j = i
    while j >= 2 and text[j - 1] == "\n" and _escaped(text, j - 1, esc):
        j -= 2
    if j == 0:
        return True
    prev = text[j - 1]
    if not powershell:
        return prev in _BASH_WORD_BREAK and not _escaped(text, j - 1, esc)
    if prev == "{" and j >= 2 and text[j - 2] == "$":
        return False
    if prev in _PS_WORD_BREAK:
        return not _escaped(text, j - 1, esc)
    if prev.isalnum() or prev in _PS_WORD:
        return False
    return None


def _comment_end(text, i, powershell, tick):
    """Index where the comment starting at text[i] ends, or -1 for an unclosed PowerShell block comment.

    A <#...#> block ends after its #>. A # comment ends at its line's newline, which is not part of it, so the line still ends
    its segment; inside a bash backtick substitution (tick) it ends at the closing backtick, as bash finds that backtick first.
    """
    if powershell and text.startswith("<#", i):
        k = text.find("#>", i + 2)
        return k + 2 if k >= 0 else -1
    end = text.find("\n", i)
    end = len(text) if end < 0 else end
    k = i
    while tick:
        k = text.find("`", k + 1, end)
        if k < 0:
            break
        if not _escaped(text, k, "\\"):
            return k
    return end


def split_shell(text, powershell=False):
    """Split on ; & && | || and newlines outside quotes. Rows are [segment, separator_after, forces_code].

    forces_code is set by a file redirect or by an unquoted heredoc body holding a backtick substitution.
    A heredoc body belongs to the segment whose command line opened it, whatever segments follow on that line,
    and several heredocs on one line are read in order. The delimiter word is bash's (see _heredoc_opener).
    Comments are removed first (see _starts_comment and _comment_end), so a quote, separator or heredoc opener inside one
    does nothing, and a # inside a heredoc body is body text. A multi-line block comment leaves its newlines behind.
    Returns None when a quote is left open, a heredoc has no terminator line, a block comment is not closed,
    or PowerShell may or may not read a # as a comment.
    """
    rows, cur, quote, heredocs = [], [], None, []
    redirect = tick = False
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
        if ch == "#" or (powershell and text.startswith("<#", i)):
            start = _starts_comment(text, i, powershell)
            if start is None:
                return None  # PowerShell may or may not read a comment here: fail closed like an open quote
            if start:
                end = _comment_end(text, i, powershell, tick)
                if end < 0:
                    return None  # an unclosed block comment: fail closed
                cur.append("\n" * text.count("\n", i, end))
                i = end
                continue
        if ch == "`" and not powershell and not _escaped(text, i, "\\"):
            tick = not tick  # inside a bash backtick substitution a comment ends at the closing backtick
        if text.startswith("<<<", i):  # a here-string, not a heredoc
            cur.append("<<<")
            i += 3
            continue
        if text.startswith("<<", i):
            op = _heredoc_opener(text, i)
            if op:
                tag, literal, strip, end = op
                heredocs.append((tag, literal, strip, len(rows)))
                cur.append(text[i:end])
                i = end
                continue
        if ch in "'\"":
            quote = ch
        elif ch == "\n":
            if heredocs:
                tag, literal, strip, owner = heredocs.pop(0)
                end = _heredoc_end(text, i + 1, tag, strip)
                if end < 0:
                    return None  # no terminator line: fail closed, no exemption for anything after the opener
                body = text[i:end]
                code = not literal and "`" in body  # an unquoted heredoc body expands backtick substitutions, so the segment is code
                if owner < len(rows):  # the opener's segment was cut already: the body is still its own
                    rows[owner][0] += body
                    rows[owner][2] = rows[owner][2] or code
                else:
                    cur.append(body)
                    redirect = redirect or code
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
            if _writes_file(text, i):
                redirect = True
        cur.append(ch)
        i += 1
    if quote or heredocs:
        return None
    cut("")
    return rows


def _first_words(segment):
    """The command word (a quoted interpreter path counts as the command) followed by the rest with quoted operands as Q."""
    rest = segment.strip()
    while True:
        op = _CALL_OP.match(rest)
        if op:  # &'C:/x/pwsh.exe', &"pwsh", &pwsh, ."pwsh.exe": the operator needs no space before the command word
            rest = rest[op.end():]
            continue
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
    """The segment with each complete quoted-delimiter commit-heredoc substitution (dollar-paren cat heredoc ... terminator, close paren) removed.

    An unquoted delimiter expands its body, so that substitution is not removed and stays visible."""
    while True:
        m = _SUBST_CAT.search(segment)
        if not m:
            return segment
        op = _heredoc_opener(segment, m.end())
        nl = segment.find("\n", op[3]) if op and op[1] else -1
        end = _heredoc_end(segment, nl + 1, op[0], op[2]) if nl >= 0 and not segment[op[3]:nl].strip() else -1
        tail = segment[end:].lstrip() if end >= 0 else ""
        if not tail.startswith(")"):
            return segment  # unquoted, redirected, or unterminated: not the idiom, so its dollar-paren stays visible
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


def code_view(command, powershell=False):
    """The command text with comments removed and data segments blanked (newlines kept, so line windows still line up)."""
    rows = split_shell(command, powershell)
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
        text = code_view(ti.get("command") or "", tool == "PowerShell")
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
