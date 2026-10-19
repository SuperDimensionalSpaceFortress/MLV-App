"""LANE-HOOK-DENY-RECURRENCE-1: count PARTIAL lane rounds by the tooling deny that stopped them.

Read-only.  Scans <fleet-runs>/lane-*/summary.md, keeps rounds whose run-dir stamp
(``-YYYYMMDDTHHMMZ`` at the end of the dir name) is on or after --since and whose
summary line 1 starts with PARTIAL, and puts each in ONE class, first match wins:

  placeholder        line 1 is still the early 'in-progress (lane started ...' placeholder
  STDIN-REPL         a python-on-stdin / -c / heredoc deny (check-python-stdin-trap)
  NA-7 lane-scratch  NA-7 on the harness scratchpad / TEMP / lane-scratch
  NA-7 outside-root  any other NA-7 (incl. C:\\Windows\\Temp, which is not the scratchpad)
  NA-2 shrink        NA-2 shrinking overwrite (incl. a shorter Edit replacement)
  NA-2 delete        NA-2 on a delete / move / cleanup
  NA-2 redirect      NA-2 on a shell redirect, tee, truncating write or --body-file
  NA-6               NA-6 (assert count falling, or a workflow edit)
  other-deny         any other hook deny (NA-2 unattributed, NA-4, NA-10, NA-11, no-background, ...)
  no-deny            PARTIAL for a reason that is not a hook deny

Line 1 decides first: the reason before its '->' successor arrow.  A reason that names no deny
('deny'/'denied', an NA-n token, 'hook') is 'no-deny' whatever the body recalls.  Only a line 1 that names
a deny without a class, or gives no reason, is refined by the later lines that name a deny.
Run dirs without a trailing -YYYYMMDDTHHMMZ stamp and summaries that cannot be read are reported as
their own counts, never dropped.  The day is the run dir's UTC stamp (the dispatch day).  --root defaults to
$MLV_BOARD_ROOT\\.claude-state\\fleet-runs, else the board on the hub host.  Usage:

  py -3 tools/coordination/lane_deny_census.py [--root <fleet-runs>] [--since 2026-10-12] [--rounds]
"""
import argparse
import glob
import os
import re
import sys

DEFAULT_ROOT = os.path.join(os.environ.get("MLV_BOARD_ROOT") or r"C:\!Layi Wkspc\MLV-App", ".claude-state", "fleet-runs")
CLASSES = (
    "NA-2 redirect", "NA-2 shrink", "NA-2 delete", "NA-7 outside-root", "NA-7 lane-scratch",
    "STDIN-REPL", "NA-6", "other-deny", "no-deny", "placeholder",
)
_STAMP_RX = re.compile(r"-(\d{4})(\d{2})(\d{2})T\d{4}Z$")
_DENY_WORD = re.compile(r"\bden(?:y|ied|ies)\b|hook[- ]blocked|blocked by (?:the )?(?:\S+ )?hook", re.I)
_SCRATCH_WORDS = r"scratchpad|lane-scratch|%TEMP%|\$env:TEMP|\bTEMP\b"   # the one definition both lane-scratch tests use
# CEN-01: the Windows Temp dir (C:\Windows\Temp, /c/windows/temp, %SystemRoot%\Temp) is outside every root and is not the
# harness scratchpad, so its 'Temp' is masked before any rule reads a line (the mask word matches no rule).
_WINDOWS_TEMP = re.compile(r"(?:%(?:SystemRoot|windir)%|\$env:(?:SystemRoot|windir)|(?<![\w-])windows)[\\/]+temp\b", re.I)
_RULES = (
    ("placeholder", re.compile(r"^PARTIAL in-progress \(lane started", re.I)),
    ("STDIN-REPL", re.compile(r"stdin|python3? -c\b|python3? - <<|heredoc|STDIN-REPL", re.I)),
    ("NA-7 lane-scratch", re.compile(r"NA-7[^\n]*(?:%s)" % _SCRATCH_WORDS, re.I)),
    ("NA-7 outside-root", re.compile(r"NA-7", re.I)),
    ("NA-2 shrink", re.compile(r"NA-2[^\n]*(?:shrink|shorter)|(?:shrink|shorter)[^\n]*NA-2", re.I)),
    ("NA-2 delete", re.compile(r"NA-2[^\n]*(?:delet|\brm\b|remove-item|clean|\bmv\b|move-item)|"
                               r"(?:delet|remove-item|cleanup|move-item)[^\n]*NA-2", re.I)),
    ("NA-2 redirect", re.compile(r"NA-2[^\n]*(?:redirect|\btee\b|truncat|body-file|(?<![-=])>)|"   # '->' / '=>' are arrows, not redirects
                                 r"(?:redirect|\btee\b)[^\n]*NA-2", re.I)),
    ("NA-6", re.compile(r"NA-6", re.I)),
)


_EVIDENCE = re.compile(r"\bden(?:y|ied|ies)\b|\bNA-\d+\b|\bhook\b|outside both the worktree", re.I)
_NA_TOKEN = re.compile(r"\bNA-\d+\b", re.I)
_NA7_LINE = re.compile(r"NA-7|outside both the worktree", re.I)
_SCRATCH = re.compile(_SCRATCH_WORDS, re.I)
_ARROW = re.compile(r"\s*(?:->|→)\s*(?=successor\b)", re.I)   # only the successor arrow ends the reason


def _first_rule(text, families=None):
    """First class whose rule matches; with families (NA-n tokens line 1 named), only that family's classes."""
    for name, rx in _RULES[1:]:
        if families is not None and name.split()[0].upper() not in families:
            continue
        if rx.search(text):
            return name
    return None


def _line_one(first):
    """-> (reason, text): the PARTIAL reason before the successor arrow, and the line-1 text the rules read
    (the reason, or everything after 'PARTIAL' when there is no reason before the arrow)."""
    head = re.sub(r"^PARTIAL\b[\s:;,.]*", "", first, flags=re.I)
    reason = _ARROW.split(head, 1)[0].strip(" \t:;,.-–—")
    return reason, (reason or head)


def classify(lines):
    """-> one class name for a PARTIAL summary given as a list of lines (line 1 first).

    Line 1's terminal reason decides first: a reason that names no deny is 'no-deny', whatever the body
    recalls.  Line 1 decides the class when it names one.  When it only says "hook deny", or gives no reason,
    the body lines that carry deny evidence (a deny word, an NA-n token, 'hook', or NA-7's own message)
    decide.  An NA-7 round is 'lane-scratch' when any NA-7 evidence line names the scratchpad, whichever
    line names the rule.  A Windows Temp path is not scratchpad evidence (CEN-01)."""
    lines = [_WINDOWS_TEMP.sub("WINDOWS-SYSTEM-DIR", ln) for ln in lines]
    first = lines[0].strip().lstrip("#* ").strip() if lines else ""
    if _RULES[0][1].search(first):
        return "placeholder"
    reason, text = _line_one(first)
    if reason and not _EVIDENCE.search(reason):
        return "no-deny"
    body = [ln for ln in lines[1:] if _EVIDENCE.search(ln)]
    if not _DENY_WORD.search("\n".join([text] + body)):
        return "no-deny"
    cls = _first_rule(text) if _DENY_WORD.search(text) else None
    if cls is None:
        # A line 1 that names an NA rule is refined by the body lines naming that same rule, and only by that
        # rule's classes (NA-10, NA-11, ... have none, so they are 'other-deny'): it is the stop.
        named = {t.upper() for t in _NA_TOKEN.findall(text)}
        pool = [ln for ln in body if named & {t.upper() for t in _NA_TOKEN.findall(ln)}] if named else []
        cls = _first_rule("\n".join(pool or body), named or None) or "other-deny"
    if cls == "NA-7 outside-root" and any(
        _NA7_LINE.search(ln) and _SCRATCH.search(ln) for ln in [text] + body
    ):
        cls = "NA-7 lane-scratch"
    return cls


def is_partial(lines):
    return bool(lines) and lines[0].strip().lstrip("#* ").upper().startswith("PARTIAL")


def run_day(dirname):
    m = _STAMP_RX.search(dirname)
    return "%s-%s-%s" % m.groups() if m else None


def census(root, since, stats=None):
    """-> (rows, table): rows = [(day, run, class)], table = {day: {class: n}}.

    stats, when given, gets {"unstamped": [run dir names], "unreadable": [run dir names]}: the lane dirs
    holding a summary.md whose name has no trailing -YYYYMMDDTHHMMZ stamp, and the ones whose summary.md
    could not be read.  Both are skipped from the table and counted, never dropped silently."""
    rows = []
    stats = {} if stats is None else stats
    unstamped, unreadable = stats.setdefault("unstamped", []), stats.setdefault("unreadable", [])
    for d in sorted(glob.glob(os.path.join(root, "lane-*"))):
        name = os.path.basename(d)
        day = run_day(name)
        summary = os.path.join(d, "summary.md")
        if day is None:
            if os.path.isfile(summary):
                unstamped.append(name)
            continue
        if day < since or not os.path.isfile(summary):
            continue
        try:
            with open(summary, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            unreadable.append(name)
            continue
        if is_partial(lines):
            rows.append((day, os.path.basename(d), classify(lines)))
    table = {}
    for day, _run, cls in rows:
        table.setdefault(day, dict.fromkeys(CLASSES, 0))[cls] += 1
    return rows, table


def render(table):
    head = ["day"] + list(CLASSES) + ["total"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    totals = dict.fromkeys(CLASSES, 0)
    for day in sorted(table):
        counts = table[day]
        for c in CLASSES:
            totals[c] += counts[c]
        out.append("| " + " | ".join([day] + [str(counts[c]) for c in CLASSES] + [str(sum(counts.values()))]) + " |")
    out.append("| **all** | " + " | ".join(str(totals[c]) for c in CLASSES) + " | %d |" % sum(totals.values()))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--since", default="2026-10-12")
    ap.add_argument("--rounds", action="store_true", help="also list every PARTIAL round and its class")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.root):
        print("no such fleet-runs root: %s" % args.root, file=sys.stderr)
        return 2
    stats = {}
    rows, table = census(args.root, args.since, stats)
    print("PARTIAL lane rounds by deny class, run-dir day >= %s, root %s" % (args.since, args.root))
    print()
    print(render(table))
    print()
    print("unstamped run dirs skipped: %d" % len(stats["unstamped"]))
    print("unreadable summary.md: %d" % len(stats["unreadable"]))
    if args.rounds:
        print()
        for day, run, cls in rows:
            print("%s  %-18s  %s" % (day, cls, run))
        for label, names in (("unstamped", stats["unstamped"]), ("unreadable", stats["unreadable"])):
            for run in names:
                print("%-10s  %s" % (label, run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
