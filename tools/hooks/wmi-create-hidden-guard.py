"""PreToolUse guard: a detached Win32_Process Create must be hidden (WMI-CREATE-HIDDEN-GUARD-1, owner-approved 2026-10-21).

A Win32_Process Create without ProcessStartupInformation ShowWindow=0 starts a cmd.exe parented to WmiPrvSE that shows a
visible console on the owner's desktop (four of them on 2026-10-21 ~08:12Z, from lane-written run-dir scripts such as
swap-driver.ps1; 173 in two days on 2026-10-19). The fix is one helper: .claude-state\\fleet-runs\\hub-tools\\Start-HiddenDetached.ps1.

The rule mirrors hub-tools\\Test-WmiCreateHidden.ps1: a code line (text after ' #' ignored) that holds Win32_Process (not
Win32_ProcessStartup), the word Create and a call construct (Invoke-CimMethod / Invoke-WmiMethod / [wmiclass] / .Create( ) passes
only when ShowWindow appears within the 2 lines before or the 3 lines after it.

Checked: Bash / PowerShell `command`, Write `content`, Edit `new_string`, NotebookEdit `new_source`. Not checked: a read-only
search command (first word grep / rg / git grep / Select-String / findstr), and files named Test-*.ps1 or test_*.py (fixtures).
Exit 2 blocks (the message goes to the model); exit 0 allows. Any parse failure allows (fail-open: this is a lint, and the
never-authorized gate stays the safety boundary).
"""
import json
import re
import sys

BEFORE, AFTER = 2, 3
SEARCH_FIRST_WORDS = ("grep", "rg", "findstr", "select-string")
HELPER = r"C:\!Layi Wkspc\MLV-App\.claude-state\fleet-runs\hub-tools\Start-HiddenDetached.ps1"

_PROC = re.compile(r"(?i)\bWin32_Process\b(?!Startup)")
_CREATE = re.compile(r"(?i)\bCreate\b")
_CALL = re.compile(r"(?i)Invoke-(Cim|Wmi)Method|\[wmiclass\]|\.Create\(")
_SHOW = re.compile(r"(?i)ShowWindow")
_COMMENT = re.compile(r"(^|\s)#.*$")


def offending_lines(text):
    """Return 1-based line numbers of Win32_Process Create calls with no ShowWindow nearby."""
    lines = text.splitlines() or [text]
    out = []
    for i, raw in enumerate(lines):
        code = _COMMENT.sub("", raw)
        if not (_PROC.search(code) and _CREATE.search(code) and _CALL.search(code)):
            continue
        lo, hi = max(0, i - BEFORE), min(len(lines) - 1, i + AFTER)
        if not _SHOW.search("\n".join(lines[lo:hi + 1])):
            out.append(i + 1)
    return out


def is_search_command(command):
    words = command.strip().split()
    if not words:
        return False
    first = words[0].lower()
    if first in SEARCH_FIRST_WORDS:
        return True
    return first == "git" and len(words) > 1 and words[1].lower() == "grep"


def decide(payload):
    """Return a block message, or None to allow."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool in ("Bash", "PowerShell"):
        text = ti.get("command") or ""
        if is_search_command(text):
            return None
    elif tool in ("Write", "Edit", "NotebookEdit"):
        path = (ti.get("file_path") or ti.get("notebook_path") or "").replace("\\", "/")
        name = path.rsplit("/", 1)[-1].lower()
        if name.startswith("test-") or name.startswith("test_"):
            return None
        text = ti.get("content") or ti.get("new_string") or ti.get("new_source") or ""
    else:
        return None
    bad = offending_lines(text)
    if not bad:
        return None
    return ("WMI-CREATE-HIDDEN-GUARD: a Win32_Process Create without ShowWindow=0 opens a VISIBLE console on the owner's desktop "
            "(line %s of this %s). Launch detached processes only through the helper, one launch per call:\n"
            "  pwsh -NoProfile -File \"%s\" -Exe <exe> -ArgumentList <a>,<b> -StdoutPath <out> -StderrPath <err>\n"
            "Do not hand-write Invoke-CimMethod / Invoke-WmiMethod / [wmiclass] Win32_Process Create. Do not reshape the call to "
            "pass this check; use the helper." % (", ".join(str(n) for n in bad), "command" if tool in ("Bash", "PowerShell") else "file content", HELPER))


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
