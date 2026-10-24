#!/usr/bin/env python3
"""Defect-dispositions evidence block for an MLV PR body (card FACTORY-ESCAPE-REGRESSION-DISCIPLINE-1).

EVIDENCE-ONLY, NOT a gate (owner ask 2026-10-24, hub ACK on MLV-App #660; re-scoped by the hub at
15:24Z: "works under the existing blocker rules and adds no new merge precondition"). A missing or
incomplete record yields one advisory WARN line and never a refusal. For every demonstrated defect
(a key BLOCKER, a confirmed Dot/Grok finding, or a merge_group failure that is not a flake) the landing
PR records (a) a regression test that FAILS on the old code and passes on the new, (b) adversarial
combination tests, and (c) a disposition: fixed / distinguished: <reason> / deferred: <CARD>.

Four subcommands, stdlib only:

  render  --pr N --head <40-hex> --records <json file>
          Print ONE markdown block between the begin/end markers, titled "Defect dispositions", one
          table row per record. Any field a record lacks renders MISSING; a value is never invented.
  splice  --body-file <in> --block-file <block> --out <out>
          Replace the existing begin/end block in a PR body, or append one. Idempotent; bytes outside
          the markers are untouched. A body whose markers are malformed is refused (exit 2) and nothing
          is written, so no body text can be duplicated or lost.
  check   --body-file <f>
          Print exactly one line: DEFECT_LEDGER ok n=<k> | WARN missing | WARN incomplete rows=<ids>
          | WARN unreadable <why>. ALWAYS exits 0. "incomplete" = a row with MISSING, a `fixed` row with
          no distinct RED-on-old / GREEN-on-new evidence, a `fixed` row with no combination test and no
          reason, or a disposition that is not one of the three forms.
  escapes --merged-log <file> [--records-dir <dir>] [--since-days 7] [--known-flakes <file>]
          [--reverts-from-git <repo>] [--now <iso utc>]
          Print `ESCAPES window=7d merged_defects=<a> reverts=<c> nonflake_merge_group_failures=<b>
          repeat_failures=<r>` then one `ESCAPE ...` line per counted item. ALWAYS exits 0. Inputs are
          plain files passed as arguments; the tool never reads .claude-state on its own.

Record schema `defect-ledger.v1`: a JSON list of objects (a single object is also accepted). Fields:

  pr (int)  head_sha (40-hex)  recorded_utc (ISO-8601 UTC)       -- binding; recorded_utc drives `escapes`
  defect_id        short id, unique inside the PR
  source           key seat + run dir, a Dot review uuid, a PR comment id, or a merge_group run id
  claim            one line
  regression_test  `path::name`
  old_red_sha      sha (full or 8+) where the test was observed RED on the OLD code
  new_green_sha    sha (full or 8+) where it was observed GREEN on the NEW code (must differ from old)
  combination_tests  list of `path::name`, or [] / "none" together with combination_reason
  combination_reason why no neighbouring-edge test applies (required when there is no combination test)
  disposition      `fixed` | `distinguished: <reason>` | `deferred: <CARD>`

A non-`fixed` row has no RED/GREEN to show; write an explicit `n/a` (or `n/a: <why>`) in those fields,
because an absent field renders MISSING and check flags any MISSING.

merged-log for `escapes` is JSON-lines, one event per line (blank and `#` lines ignored):
  {"event":"merged","pr":N,"utc":..}                              a PR landed on master
  {"event":"revert","utc":..,"sha":..,"title":..}                 a revert landed
  {"event":"merge_group_failure","utc":..,"run_id":..,"test_id":..,"pr":N}
known-flakes is one test id per line (`#` comments). `escapes` counts: merged_defects = in-window records
(not `distinguished`) whose PR has a `merged` event; reverts = log reverts plus, with --reverts-from-git,
merge commits whose subject or first body line starts `Revert "`; nonflake_merge_group_failures =
in-window failures whose test_id is not a known flake; repeat_failures = clusters of in-window records and
non-flake failures that share a test id or a normalised claim across two or more distinct PRs.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

BEGIN = "<!-- defect-dispositions:begin -->"
END = "<!-- defect-dispositions:end -->"
SCHEMA = "defect-ledger.v1"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
HEXISH_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
EVIDENCE_RE = re.compile(r"^old: RED on ([0-9a-fA-F]{7,40}) / new: GREEN on ([0-9a-fA-F]{7,40})$")
CARD_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")

MISSING = "MISSING"
UNREADABLE = "unreadable"
COLUMNS = ["Defect", "Source", "Claim", "Regression test", "Regression evidence", "Combination tests", "Disposition"]

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RECORD_FILES = 2000
GIT_TIMEOUT_S = 30


def _read_capped(path, cap=MAX_FILE_BYTES):
    with open(path, "rb") as fh:
        return fh.read(cap)


def _norm_sha(value):
    if isinstance(value, str) and SHA_RE.match(value.strip()):
        return value.strip().lower()
    return None


def parse_utc(text):
    """ISO-8601 -> epoch seconds (naive means UTC). Raises ValueError."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty timestamp")
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def fmt_utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(text):
    """One table cell: no pipes, no newlines, and no marker or comment text that could forge a block."""
    out = str(text).replace("|", "/").replace("\r", " ").replace("\n", " ")
    return out.replace("<!--", "&lt;!--").replace("-->", "--&gt;").strip()


def split_row(row):
    """The cells of one rendered table row (sanitised cells never contain a pipe)."""
    inner = row.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


class _Unreadable(Exception):
    pass


def _text(value):
    """Return the stripped string, MISSING when absent or blank; raise _Unreadable for a non-string."""
    if value is None:
        return MISSING
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _Unreadable()
    out = _cell(value)
    return out if out else MISSING


def _sha_text(value):
    """Evidence sha for the column: hex-looking values show their first eight characters, anything else as given."""
    shown = _text(value)
    if shown != MISSING and HEXISH_RE.match(shown):
        return shown[:8]
    return shown


def _combination(rec):
    tests, reason = rec.get("combination_tests"), rec.get("combination_reason")
    names = []
    if isinstance(tests, str):
        tests = [tests]
    if tests is not None and not isinstance(tests, list):
        raise _Unreadable()
    for item in tests or []:
        if not isinstance(item, str):
            raise _Unreadable()
        if item.strip() and item.strip().lower() != "none":
            names.append(_cell(item))
    why = _text(reason)
    if names:
        return "; ".join(names)
    if tests is None and why == MISSING:
        return MISSING
    return "none" if why == MISSING else "none (reason: %s)" % why


def _disposition(value):
    raw = _text(value)
    if raw == MISSING:
        return MISSING
    low = raw.lower()
    if low == "fixed":
        return "fixed"
    m = re.match(r"^(distinguished|deferred)\s*:?\s*(.*)$", raw, re.IGNORECASE)
    if m:
        rest = m.group(2).strip()
        return "%s: %s" % (m.group(1).lower(), rest if rest else MISSING)
    return raw


def _row_cells(rec):
    """Seven cells for one record, or None when the record cannot be read."""
    if not isinstance(rec, dict):
        return None
    try:
        evidence = "old: %s / new: %s" % (
            "MISSING" if _sha_text(rec.get("old_red_sha")) == MISSING else "RED on %s" % _sha_text(rec.get("old_red_sha")),
            "MISSING" if _sha_text(rec.get("new_green_sha")) == MISSING else "GREEN on %s" % _sha_text(rec.get("new_green_sha")))
        return [_text(rec.get("defect_id")), _text(rec.get("source")), _text(rec.get("claim")),
                _text(rec.get("regression_test")), evidence, _combination(rec), _disposition(rec.get("disposition"))]
    except _Unreadable:
        return None


def load_records(path):
    """The record list from a JSON file; any trouble yields [None] so render shows one unreadable row."""
    try:
        data = json.loads(_read_capped(path).decode("utf-8"))
    except Exception:  # absent, torn, undecodable
        return [None]
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else [None]


def render(pr, head, records):
    head = _norm_sha(head)
    if head is None:
        raise ValueError("--head must be a full 40-hex SHA")
    out = [BEGIN, "## Defect dispositions", "",
           "Evidence-only record (advisory, not a gate; schema `%s`). PR #%d, bound to head `%s`. One row per "
           "demonstrated defect: a regression test that fails on the old code and passes on the new, the "
           "adversarial combination tests, and a disposition (`fixed`, `distinguished: <reason>` or "
           "`deferred: <CARD>`). A field that is not recorded reads `MISSING`." % (SCHEMA, pr, head), "",
           "| %s |" % " | ".join(COLUMNS), "|%s" % ("---|" * len(COLUMNS))]
    rows = 0
    for rec in records:
        cells = _row_cells(rec) or [UNREADABLE] * len(COLUMNS)
        out.append("| %s |" % " | ".join(cells))
        rows += 1
    if not rows:
        out += ["", "No defect records supplied for this head."]
    out += ["", END]
    return "\n".join(out)


def _find_all(text, needle):
    spots, at = [], text.find(needle)
    while at >= 0:
        spots.append(at)
        at = text.find(needle, at + 1)
    return spots


def _locate(text):
    """Return ('none', None) | ('ok', (start, end_exclusive)) | ('bad', why)."""
    begins, ends = _find_all(text, BEGIN), _find_all(text, END)
    if not begins and not ends:
        return "none", None
    if len(begins) != 1 or len(ends) != 1:
        return "bad", "malformed markers (begin=%d end=%d)" % (len(begins), len(ends))
    if ends[0] < begins[0]:
        return "bad", "malformed markers (end before begin)"
    return "ok", (begins[0], ends[0] + len(END))


def splice(body_bytes, block_bytes):
    """Return new body bytes; raise ValueError when the block or the body's markers are unusable."""
    body = body_bytes.decode("utf-8")
    block = block_bytes.decode("utf-8").replace("\r\n", "\n")
    state, span = _locate(block)
    if state != "ok":
        raise ValueError("block file has no single begin/end pair")
    block = block[span[0]:span[1]]
    eol = "\r\n" if "\r\n" in body else "\n"
    block = block.replace("\n", eol)
    state, span = _locate(body)
    if state == "bad":
        raise ValueError("body has %s; refusing to touch it" % span)
    if state == "ok":
        new = body[:span[0]] + block + body[span[1]:]
    elif not body:
        new = block + eol
    else:
        new = body + ("" if body.endswith(eol) else eol) + eol + block + eol
    return new.encode("utf-8")


def _row_incomplete(cells):
    if any(MISSING in c for c in cells):
        return True
    ident, _source, _claim, test, evidence, combo, disposition = cells
    low = disposition.lower()
    if low == "fixed":
        m = EVIDENCE_RE.match(evidence)
        if not m or m.group(1).lower()[:8] == m.group(2).lower()[:8]:
            return True
        if test.lower().startswith("n/a") or combo.lower() == "none":
            return True
        return False
    if low.startswith("distinguished: "):
        return False
    if low.startswith("deferred: "):
        return not CARD_RE.match(disposition[len("deferred: "):].strip())
    return True


def check(body_bytes):
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "DEFECT_LEDGER WARN unreadable body is not utf-8"
    state, span = _locate(body)
    if state == "none":
        return "DEFECT_LEDGER WARN missing"
    if state == "bad":
        return "DEFECT_LEDGER WARN unreadable %s" % span
    section = body[span[0]:span[1]]
    rows = [ln for ln in section.splitlines()
            if ln.startswith("| ") and not ln.startswith("| Defect |")]
    parsed = []
    for n, line in enumerate(rows, 1):
        cells = split_row(line)
        if len(cells) != len(COLUMNS):
            return "DEFECT_LEDGER WARN unreadable row %d has %d cells (want %d)" % (n, len(cells), len(COLUMNS))
        if cells[0] == UNREADABLE:
            return "DEFECT_LEDGER WARN unreadable row %d is an unreadable record" % n
        parsed.append(cells)
    bad = []
    for n, cells in enumerate(parsed, 1):
        if _row_incomplete(cells):
            label = "row%d" % n if cells[0] == MISSING else cells[0]
            if label not in bad:
                bad.append(label)
    if bad:
        return "DEFECT_LEDGER WARN incomplete rows=%s" % ",".join(bad)
    return "DEFECT_LEDGER ok n=%d" % len(parsed)


# --- escapes -----------------------------------------------------------------------------------------

def _claim_key(claim):
    return re.sub(r"[^a-z0-9]+", " ", str(claim).lower()).strip()[:120]


def _load_events(path, notes):
    events = []
    try:
        text = _read_capped(path).decode("utf-8", "replace")
    except OSError as exc:
        notes.append("ESCAPE unreadable merged-log %s" % type(exc).__name__)
        return events
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            notes.append("ESCAPE unreadable merged-log line %d" % n)
            continue
        events.append(obj)
    return events


def _load_flakes(path, notes):
    if not path:
        return set()
    try:
        text = _read_capped(path).decode("utf-8", "replace")
    except OSError as exc:
        notes.append("ESCAPE unreadable known-flakes %s" % type(exc).__name__)
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def _load_record_dir(path, notes):
    records = []
    if not path or not os.path.isdir(path):
        return records
    seen = 0
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            if not name.lower().endswith(".json"):
                continue
            seen += 1
            if seen > MAX_RECORD_FILES:
                notes.append("ESCAPE unreadable records-dir more than %d files; the rest are skipped" % MAX_RECORD_FILES)
                return records
            full = os.path.join(root, name)
            try:
                data = json.loads(_read_capped(full).decode("utf-8"))
            except Exception as exc:
                notes.append("ESCAPE unreadable record file %s %s" % (name, type(exc).__name__))
                continue
            items = [data] if isinstance(data, dict) else data if isinstance(data, list) else None
            if items is None:
                notes.append("ESCAPE unreadable record file %s root is not a list or object" % name)
                continue
            for item in items:
                if isinstance(item, dict):
                    records.append(item)
                else:
                    notes.append("ESCAPE unreadable record in %s is not an object" % name)
    return records


def _as_pr(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _in_window(utc, lo, hi):
    try:
        t = parse_utc(utc)
    except (ValueError, TypeError):
        return None
    return lo <= t <= hi


def _git_reverts(repo, lo, hi, notes):
    found = []
    coarse = fmt_utc(lo - 86400)[:10]
    cmd = ["git", "-C", repo, "log", "--merges", "--since=%s" % coarse, "--format=%H%x1f%ct%x1f%s%x1f%b%x1e"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append("ESCAPE unreadable git %s" % type(exc).__name__)
        return found
    if proc.returncode != 0:
        notes.append("ESCAPE unreadable git exit=%d" % proc.returncode)
        return found
    for chunk in proc.stdout.split("\x1e"):
        parts = chunk.strip("\n").split("\x1f")
        if len(parts) < 4:
            continue
        sha, ct, subject, body = parts[0], parts[1], parts[2], parts[3]
        try:
            when = float(ct)
        except ValueError:
            continue
        first_body = (body.strip().splitlines() or [""])[0]
        if lo <= when <= hi and (subject.startswith('Revert "') or first_body.startswith('Revert "')):
            found.append({"sha": sha.strip(), "title": subject.strip(), "utc": fmt_utc(when)})
    return found


def escapes(merged_log, records_dir=None, since_days=7, known_flakes=None, now=None, git_repo=None):
    """Return the output lines: the ESCAPES summary first, then one ESCAPE line per counted item."""
    notes = []
    hi = parse_utc(now) if now else datetime.datetime.now(datetime.timezone.utc).timestamp()
    lo = hi - float(since_days) * 86400
    window = "%gd" % float(since_days)
    events = _load_events(merged_log, notes) if merged_log else []
    flakes = _load_flakes(known_flakes, notes)
    records = _load_record_dir(records_dir, notes)

    merged_prs = {_as_pr(e.get("pr")) for e in events if e.get("event") == "merged"} - {None}
    items, nodes = [], []  # nodes: (pr, label, keys) for repeat clustering

    for rec in records:
        ident = rec.get("defect_id") if isinstance(rec.get("defect_id"), str) and rec.get("defect_id") else "?"
        inside = _in_window(rec.get("recorded_utc"), lo, hi)
        if inside is None:
            notes.append("ESCAPE skipped record %s no usable recorded_utc" % ident)
            continue
        pr = _as_pr(rec.get("pr"))
        disposition = rec.get("disposition")
        if not inside or pr is None or (isinstance(disposition, str) and disposition.strip().lower().startswith("distinguished")):
            continue
        keys = set()
        test = rec.get("regression_test")
        if isinstance(test, str) and test.strip() and not test.strip().lower().startswith("n/a"):
            keys.add("t:" + test.strip())
        claim = _claim_key(rec.get("claim")) if isinstance(rec.get("claim"), str) else ""
        if claim:
            keys.add("c:" + claim)
        nodes.append((pr, ident, keys))
        if pr in merged_prs:
            src = rec.get("source") if isinstance(rec.get("source"), str) and rec.get("source").strip() else MISSING
            items.append("ESCAPE merged_defect %s pr=%d src=%s" % (ident, pr, _cell(src)))
    merged_defects = len(items)

    revert_lines, seen_reverts = [], set()
    for e in events:
        if e.get("event") != "revert":
            continue
        inside = _in_window(e.get("utc"), lo, hi)
        if inside is None:
            notes.append("ESCAPE skipped revert no usable utc")
        if not inside:
            continue
        sha = e.get("sha") if isinstance(e.get("sha"), str) else ""
        seen_reverts.add(sha[:8])
        revert_lines.append("ESCAPE revert sha=%s title=%s utc=%s" % (
            sha[:8] or "none", _cell(e.get("title") or ""), _cell(e.get("utc"))))
    if git_repo:
        for g in _git_reverts(git_repo, lo, hi, notes):
            if g["sha"][:8] in seen_reverts:
                continue
            seen_reverts.add(g["sha"][:8])
            revert_lines.append("ESCAPE revert sha=%s title=%s utc=%s" % (g["sha"][:8], _cell(g["title"]), g["utc"]))

    failure_lines = []
    for e in events:
        if e.get("event") != "merge_group_failure":
            continue
        inside = _in_window(e.get("utc"), lo, hi)
        if inside is None:
            notes.append("ESCAPE skipped merge_group_failure run=%s no usable utc" % _cell(e.get("run_id")))
        if not inside:
            continue
        test = e.get("test_id") if isinstance(e.get("test_id"), str) and e.get("test_id").strip() else None
        if test and test.strip() in flakes:
            continue
        pr = _as_pr(e.get("pr"))
        failure_lines.append("ESCAPE merge_group_failure run=%s test=%s pr=%s" % (
            _cell(e.get("run_id") if e.get("run_id") is not None else "unknown"), _cell(test or "unknown"),
            pr if pr is not None else "unknown"))
        if test and pr is not None:
            nodes.append((pr, "run-%s" % e.get("run_id"), {"t:" + test.strip()}))

    repeat_lines = _repeat_clusters(nodes)
    head = "ESCAPES window=%s merged_defects=%d reverts=%d nonflake_merge_group_failures=%d repeat_failures=%d" % (
        window, merged_defects, len(revert_lines), len(failure_lines), len(repeat_lines))
    return [head] + items + revert_lines + failure_lines + repeat_lines + notes


def _repeat_clusters(nodes):
    """One `ESCAPE repeat` line per cluster of nodes linked by a shared key that spans two or more PRs."""
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_by_key = {}
    for idx, (_pr, _label, keys) in enumerate(nodes):
        for key in keys:
            if key in first_by_key:
                parent[find(idx)] = find(first_by_key[key])
            else:
                first_by_key[key] = idx
    clusters = {}
    for idx in range(len(nodes)):
        clusters.setdefault(find(idx), []).append(idx)
    lines = []
    for members in clusters.values():
        prs = sorted({nodes[i][0] for i in members})
        if len(prs) < 2:
            continue
        shared = sorted(k for k in {k for i in members for k in nodes[i][2]}
                        if len({nodes[i][0] for i in members if k in nodes[i][2]}) >= 2)
        lines.append("ESCAPE repeat key=%s prs=%s" % (_cell(shared[0] if shared else "unknown"), ",".join(str(p) for p in prs)))
    return sorted(lines)


# --- CLI -----------------------------------------------------------------------------------------------

class _Quiet(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _parser():
    p = _Quiet(prog="defect_ledger", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Quiet)
    r = sub.add_parser("render")
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--head", required=True)
    r.add_argument("--records", required=True)
    s = sub.add_parser("splice")
    s.add_argument("--body-file", required=True)
    s.add_argument("--block-file", required=True)
    s.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--body-file", required=True)
    e = sub.add_parser("escapes")
    e.add_argument("--merged-log", required=True)
    e.add_argument("--records-dir", default=None)
    e.add_argument("--since-days", type=float, default=7)
    e.add_argument("--known-flakes", default=None)
    e.add_argument("--reverts-from-git", default=None)
    e.add_argument("--now", default=None, help="ISO-8601 UTC end of the window (default: the clock)")
    return p


def _first_line(exc):
    return ((str(exc).splitlines() or [type(exc).__name__])[0])[:80]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "check":  # evidence-only: nothing about check may ever exit non-zero
        try:
            args = _parser().parse_args(argv)
            try:
                line = check(_read_capped(args.body_file))
            except OSError as exc:
                line = "DEFECT_LEDGER WARN unreadable %s" % type(exc).__name__
        except Exception as exc:
            line = "DEFECT_LEDGER WARN unreadable %s" % _first_line(exc)
        sys.stdout.write(line + "\n")
        return 0
    if argv and argv[0] == "escapes":  # same promise for the daily count
        try:
            args = _parser().parse_args(argv)
            lines = escapes(args.merged_log, args.records_dir, args.since_days, args.known_flakes, args.now,
                            args.reverts_from_git)
        except Exception as exc:
            lines = ["ESCAPES window=? merged_defects=0 reverts=0 nonflake_merge_group_failures=0 repeat_failures=0",
                     "ESCAPE unreadable arguments %s" % _first_line(exc)]
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
        return 0
    try:
        args = _parser().parse_args(argv)
        if args.cmd == "render":
            text = render(args.pr, args.head, load_records(args.records))
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            return 0
        with open(args.body_file, "rb") as fh:
            body = fh.read()
        with open(args.block_file, "rb") as fh:
            block = fh.read()
        new = splice(body, block)
        with open(args.out, "wb") as fh:
            fh.write(new)
        return 0
    except (ValueError, OSError) as exc:
        sys.stderr.write("defect_ledger: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
