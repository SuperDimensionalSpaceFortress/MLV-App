"""Render docs/roadmap.md from the board's card-queue.json.

The roadmap used to mirror the frozen dual-lane queue.json, 14 of whose 16 rows have
landed, so it read as drained while the live dispatch surface held dozens of open cards.
card-queue.json (schema mlv-app/card-queue/v1) is the live surface: this renders its open
`product`/`playback` rows and the rate at which they are landing versus arriving.

The card-queue file is board-local (gitignored) and absent from hosted CI, so the path is
a required argument. The output is a pure function of that file: no wall clock, the
7-day window is anchored on its `generated_utc`, so `--check` is stable.

Landing time of a merged row is its `merged_utc` when the row carries one. Without it,
the landing time is the newest of `source_ledger_stamp`, `pinned_utc` and every
`declared@<utc>` in `evidence`, which is the last time the hub touched the row. Declaration
and pin stamps are not merge events, so that fallback is an approximation (a lower bound
on the real merge time), not a measurement. Intake time is `source_ledger_stamp`.

Scope rulings (for example a deferral) are data, not code: an optional top-level `scope_notes`
list of `{"title": ..., "text": ...}` in card-queue.json is rendered as `**title.** text` under
the open table and under "Parked with reason". This file holds no PR number or card id; a queue
without `scope_notes` renders no note. The hub owns the block and removes a note when it lapses.

Owner asks (OWNER-ASK-PARK-GUARD-1): a row with `origin` of `owner` is counted whatever its
`kind` (an owner ask filed as unclassified or hub-tooling used to vanish from this file). On
the open table its kind cell reads `<kind> (owner)` when the kind is not product or playback.
Parked owner asks get their own `## Parked owner asks` section, longest-parked first, with the
park reason and the age in whole days from the park stamp (`pinned_utc` when present, else
`source_ledger_stamp`) to `generated_utc`; they are not repeated under "Parked with reason".
At most OWNER_LIST_MAX (25) are listed, then one `- ... and N more owner asks parked` line, and
the open table lists at most 25 owner asks of another kind the same way, so docs/roadmap.md
stays inside its 20000-byte budget however many owner asks the board holds.
Deny by default (r2): a parked owner ask is OWED unless its row carries a structured
`resolution` (one of done, duplicate, superseded, no_go, refused), which only
`Update-CardQueue.ps1 -Set <CARD> -State parked -Resolution <r>` writes. The park note is
never read for that: free text that says "shipped" or "DONE" is still owed. A resolved owner
ask of product or playback kind is listed under "Parked with reason" with its resolution; one
of another kind is tooling churn and is counted on a single line there. Any other
`resolution` value is bad input (exit 2).

Usage:
    py -3 tools/coordination/render_roadmap.py --card-queue <card-queue.json> --out docs/roadmap.md
    py -3 tools/coordination/render_roadmap.py --card-queue <card-queue.json> --out docs/roadmap.md --check
    py -3 tools/coordination/render_roadmap.py --card-queue <card-queue.json> --drain-line

Exit: 0 ok / written, 1 `--check` and the file differs (or is missing), 2 bad input.
Bad input includes a malformed stamp (`generated_utc`, `source_ledger_stamp`, `pinned_utc`,
`merged_utc`, a `declared@` token in `evidence`) or a malformed `order_chain`; the message
names the file, the row index and the field. A `declared@` token is malformed unless it is
exactly `declared@YYYY-MM-DDTHH:MM:SSZ` (trailing `.,;:)]}'"` is ignored): a non-zero-padded
stamp would otherwise lose or win the string `max()` that picks the landing day.
r2: a stamp is checked whole, not by its date prefix (an impossible clock, an offset past 23:59,
more than 100 fractional digits, or a UTC day outside 0001-01-01..9999-12-31 exits 2), and a
card id or kind that is not a short pipe-free token, or a card that is not a string, exits 2.
r3: a stamp holds no whitespace except the one T-or-space date/time separator; any string anywhere in the queue (keys included)
that is not UTF-8 text (a lone surrogate) exits 2; and when the owner lists at cap 0 still leave the file over 20000 bytes, all
owner output collapses to one line no longer than the "(no owner ask is parked)" placeholder, so owner asks add zero bytes
(a closed owner ask is then left out of the drain counts too; an open one of product/playback kind is a product row and stays).
r3 pass 7: `state`, `order_chain.chain` and `order_chain.predecessor` must each fit one table cell (a string of at most 120
printable characters with no pipe), a scope note is one printable line, and rows that tie on every sort key are ordered by
their canonical JSON, so the output never depends on the input row order.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import calendar
from datetime import date, datetime, timedelta
from fractions import Fraction   # exact instants: a rational number has no precision context to round in
import math
from pathlib import Path

SCHEMA = "mlv-app/card-queue/v1"
KINDS = ("product", "playback")
CLOSED_STATES = ("merged", "parked")
WINDOW_DAYS = 7
NOTE_MAX = 140
REQUIRED = ("card", "kind", "priority", "state", "source_ledger_stamp")
DECLARED_RX = re.compile(r"declared@(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
# Every declared@ token, however it is spelled; validation compares it to DECLARED_RX.
DECLARED_TOKEN_RX = re.compile(r"declared@(\S*)")
TOKEN_TRAILING_PUNCT = ".,;:)]}'\""

HEADER = """\
# MLV-App product roadmap

Rendered by `tools/coordination/render_roadmap.py` from the board's `card-queue.json`
(schema `mlv-app/card-queue/v1`), the live dispatch surface. It replaces the earlier
mirror of the frozen dual-lane `queue.json`, whose 16 rows had mostly landed. The
card-queue file lives under `.claude-state/hub-autopilot/`, which is gitignored and absent
from every hosted checkout, so this file is the only place a reviewer without local state
can see the open product backlog. Where the two disagree, `card-queue.json` wins:
re-render, do not edit by hand. The hub checks freshness with `--check`.

Only rows with `kind` of `product` or `playback` are shown, plus owner asks
(`origin` = `owner`) of any kind. Other hub-tooling, factory, hygiene and ci-flake cards are
tooling churn and are not counted here. Rows the board has not classified are not counted
either, even when the card name says PLAYBACK, unless the owner asked for them.
"""
OWNER = "owner"


def _counted(r: dict) -> bool:
    """A product/playback row, or an owner ask of any kind."""
    return r["kind"] in KINDS or r.get("origin") == OWNER


def _kind_cell(r: dict) -> str:
    return r["kind"] if r["kind"] in KINDS else "%s (owner)" % r["kind"]


def _parked_since(r: dict) -> str:
    return r.get("pinned_utc") or r["source_ledger_stamp"]


# Deny by default (OWNER-ASK-PARK-GUARD-1 r2, adversary A1 on 8ba4f8ab; fleet card
# mlv-app/spelling-chase-scanner-needs-a-deny-by-default-name, softwarefactory-fleet-doctrine specs/mlv-app/cards.md:663).
# r1 read the park NOTE for completion words and then chased each way prose can say "not yet": "after #123 has shipped",
# "hasn't shipped", "blocker #123 shipped; still waiting for #456", "not\nDONE". Every new rule leaked to a new
# spelling, which is the trap that card records. So the note is never read here. A parked owner ask is owed unless its
# row carries a structured `resolution` from the closed vocabulary below, and only the hub writes one, through
# Update-CardQueue.ps1 -Set <CARD> -State parked -Resolution <r> (a value outside the vocabulary exits 2 there and here).
# The price is over-reporting: a delivered ask stays on the owed list until the hub states its resolution once.
RESOLUTIONS = ("done", "duplicate", "superseded", "no_go", "refused")


def _resolution(r: dict) -> str:
    """The row's structured resolution, or '' when it has none (absent or null): that row is not closed."""
    return r.get("resolution") or ""


def _undelivered_owner_ask(r: dict) -> bool:
    """An owner ask still owed: origin owner and no resolution. Its notes are display text only."""
    return r.get("origin") == OWNER and not _resolution(r)


# At most this many owner asks are listed per section (longest-parked first for the parked ones, table order for the
# open ones); the rest are counted on one line. docs/roadmap.md has a 20000-byte budget (test_roadmap_queue_parity.py).
OWNER_LIST_MAX = 25
# (r2 pass-4 A2) render() shrinks every owner list (never a product row) one row at a time until the file fits this budget, so owner
# asks alone can never push docs/roadmap.md over it. It is the same number tools/repo_hygiene/test_roadmap_queue_parity.py holds the file to.
ROADMAP_BUDGET_BYTES = 20000
# (r2 pass-4 A3/A4) a card id and a kind land in a table cell and in sort keys: a pipe or a line break would split the row, a non-string
# cannot sort, and an unbounded length breaks the budget. Anything else is bad input (exit 2), not a render-time crash.
CARD_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", re.ASCII)
KIND_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}", re.ASCII)
# (r2 pass-4 A1) a fraction is read exactly, so its length is bounded (int() refuses more than 4300 digits): longer is bad input.
FRACTION_MAX_DIGITS = 100


# (adversary pass 2 A3/A4/A6/A7) a stamp is read exactly: every fractional digit is kept, an explicit offset is applied, and the
# instant is a Fraction count of UTC seconds, so a 7-digit fraction, a -02:00 offset or a far-future year never truncates,
# rounds or overflows. A stamp in no such shape (date only, an odd tail) reads as that date's UTC midnight, as before.
# (r2 A2 on 8ba4f8ab) the fraction is digits / 10**len(digits), an exact rational: no arithmetic context caps the digits, so
# 2026-10-22T00:00:00.0000000000000000001Z is later than 00:00:00Z by exactly 1e-19 s and never rounds into the next whole second.
# (r3 A3 on 6c9167ce) no gap before the zone: the old `\s*` there let CR, TAB, VT, FF or a trailing space through, and a CR then reached the
# generated_from line raw. The one date/time separator (T or a single space) is the only whitespace a stamp may hold (_stamp_problem).
_STAMP_RX = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d+))?)?(Z|[+-]\d\d:?\d\d)?$", re.IGNORECASE | re.ASCII)
_DATE_ONLY_RX = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _instant(stamp: str) -> Fraction:
    """Exact UTC seconds since the epoch, as a Fraction (every fractional digit kept)."""
    m = _STAMP_RX.fullmatch(stamp)   # (r3 A3 sibling) as written, like _stamp_problem: nothing stripped, no `$` before a final LF
    if not m:
        d = _day(stamp)
        return Fraction(calendar.timegm((d.year, d.month, d.day, 0, 0, 0)))
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    secs = calendar.timegm((y, mo, d, h, mi, int(m.group(6) or 0)))
    tz = m.group(8)
    if tz and tz.upper() != "Z":
        sign = -1 if tz[0] == "-" else 1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        secs -= sign * (hh * 3600 + mm * 60)
    digits = m.group(7) or ""
    return Fraction(secs) + (Fraction(int(digits), 10 ** len(digits)) if digits else Fraction(0))


def _utc_day(stamp: str) -> date:
    """The UTC calendar day of a stamp (an offset can move it off the date its text starts with)."""
    whole = math.floor(_instant(stamp))   # exact floor of a rational: 23:59:59.999.. stays on its own day
    try:
        return date(1970, 1, 1) + timedelta(seconds=whole)
    except OverflowError:   # (r2 pass-4 A7) never a substitute day: _load already refused a stamp whose UTC day is out of range
        raise ValueError("stamp %r is outside the UTC date range 0001-01-01..9999-12-31" % stamp) from None


def _die(message: str) -> "None":
    print("render_roadmap: " + message, file=sys.stderr)
    raise SystemExit(2)


def _day(stamp: str) -> date:
    return datetime.strptime(stamp[:10], "%Y-%m-%d").date()


def _landed_stamp(r: dict) -> str:
    if r.get("merged_utc"):
        return r["merged_utc"]
    stamps = [r["source_ledger_stamp"]]
    if r.get("pinned_utc"):
        stamps.append(r["pinned_utc"])
    stamps += DECLARED_RX.findall(r.get("evidence") or "")
    return max(stamps, key=_instant)   # newest INSTANT: a string max picks the older of 08:00:00Z and 08:00:00.100Z


def _canonical(r: dict) -> str:
    """(r3 pass-7 A3) the row as canonical JSON: the last sort key, so rows that tie on every other key (a duplicate card id
    whose stamps name the same instant, e.g. 00:00:00Z and 01:00:00+01:00) render in one order whatever the input order."""
    return json.dumps(r, sort_keys=True, ensure_ascii=True)


def _sort_key(r: dict) -> tuple:
    return (r["priority"], _instant(r["source_ledger_stamp"]), r["card"], _canonical(r))


def _chain(r: dict) -> str:
    oc = r.get("order_chain")
    if not oc:
        return "-"
    pred = oc.get("predecessor")
    return "%s #%s %s" % (oc["chain"], oc["index"], "after " + pred if pred else "first")


def _scope_notes(data: dict) -> list:
    """Scope-note paragraphs (each followed by a blank line) from the queue's optional scope_notes block."""
    out = []
    for n in data.get("scope_notes") or []:
        out += ["**%s.** %s" % (n["title"], n["text"]), ""]
    return out


def _note(r: dict, limit: int = NOTE_MAX) -> str:
    text = " ".join((r.get("notes") or "").split())
    if not text:
        return "(no note)"
    return text if len(text) <= limit else text[: limit - 3] + "..."


# An owner-ask line already carries kind and age, so its park reason is cut shorter (docs/roadmap.md has a 20000-byte budget).
OWNER_NOTE_MAX = 100


def _drain_line(open_n: int, landed: int, queued: int) -> str:
    if open_n == 0:
        return "product is drained (0 open)"
    if queued >= landed:
        return "does not drain (queued >= landed)"
    # net per day = (landed - queued) / WINDOW_DAYS; days = ceil(open / net), in integers.
    net = landed - queued
    days = -(-open_n * WINDOW_DAYS // net)
    return "at the 7-day mean, product drains in %d days" % days


def _stats(data: dict, collapsed: bool = False) -> tuple:
    """(open_rows, parked_rows, days, landed_by_day, queued_by_day) for product/playback rows and owner asks.
    collapsed (r3 A1): product/playback rows only, as the base counted them, so an owner ask of another kind adds no count digit."""
    rows = [r for r in data["rows"] if (r["kind"] in KINDS if collapsed else _counted(r))]
    if collapsed:   # (r3 pass-7 A1) a closed (parked or merged) owner ask of any kind is not in the counts either: zero bytes
        rows = [r for r in rows if r.get("origin") != OWNER or r["state"] not in CLOSED_STATES]
    open_rows = sorted((r for r in rows if r["state"] not in CLOSED_STATES), key=_sort_key)
    parked = sorted((r for r in rows if r["state"] == "parked"), key=_sort_key)
    last = _utc_day(data["generated_utc"])
    days = [last - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]
    landed = {d: 0 for d in days}
    queued = {d: 0 for d in days}
    for r in rows:
        d = _utc_day(r["source_ledger_stamp"])
        if d in queued:
            queued[d] += 1
        if r["state"] == "merged":
            d = _utc_day(_landed_stamp(r))
            if d in landed:
                landed[d] += 1
    return open_rows, parked, days, landed, queued


def drain_line(data: dict) -> str:
    """One line for the board snapshot (inflight.md); no trailing newline."""
    open_rows, _parked, _days, landed, queued = _stats(data)
    landed_total, queued_total = sum(landed.values()), sum(queued.values())
    return "Product drain: %d open, %d landed vs %d queued over %d UTC days (card-queue %s): %s" % (
        len(open_rows), landed_total, queued_total, WINDOW_DAYS, data["generated_utc"],
        _drain_line(len(open_rows), landed_total, queued_total))


def render(data: dict) -> str:
    """The roadmap text. Each owner list starts at OWNER_LIST_MAX rows and all of them shrink, one row at a time, while the file is
    over ROADMAP_BUDGET_BYTES; product and playback rows are never dropped (a file over budget at cap 0 is over on product alone)."""
    for cap in range(OWNER_LIST_MAX, -1, -1):
        text = _render(data, cap)
        if len(text.encode("utf-8")) <= ROADMAP_BUDGET_BYTES:
            return text
    # (r3 A1 on 6c9167ce, hub ruling: FIX) cap 0 is still over budget, yet its count lines, the "(incl. N ...)" suffix and the
    # owner rows' drain counts still cost bytes. Collapse: render the board as the base did (product/playback rows only, an owner
    # ask of another kind not counted) and replace ALL owner output with OWNER_COLLAPSED_LINE, no longer than the placeholder it
    # replaces. Owner asks then add zero bytes to what the board renders without them; product rows are still never dropped.
    return _render(data, 0, collapsed=True)


# The one owner line in the collapsed state; never longer than "- (no owner ask is parked)", so it adds no byte to the board.
OWNER_COLLAPSED_LINE = "- (see card-queue.json)"


def _render(data: dict, cap: int, collapsed: bool = False) -> str:
    open_rows, parked, days, landed, queued = _stats(data, collapsed)
    if collapsed:   # (r3 A1) no parked owner ask is listed anywhere: OWNER_COLLAPSED_LINE stands for all of them
        parked = [r for r in parked if r.get("origin") != OWNER]
    generated = data["generated_utc"]
    last = days[-1]
    landed_total, queued_total = sum(landed.values()), sum(queued.values())

    scope = _scope_notes(data)
    out = [HEADER, "generated_from: %s" % generated, "", "## Open product and playback cards", "", *scope]
    out.append("| P | card | kind | state | queued since | order_chain |")
    out.append("|---|---|---|---|---|---|")
    other_kind_seen = 0
    for r in open_rows:
        if r["kind"] not in KINDS:   # an owner ask of another kind (r2 A3 sibling): the first OWNER_LIST_MAX in table order are listed
            other_kind_seen += 1
            if other_kind_seen > cap:
                continue
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            r["priority"], r["card"], _kind_cell(r), r["state"], _utc_day(r["source_ledger_stamp"]).isoformat(), _chain(r)))
    if not open_rows:
        out.append("| - | (none open) | - | - | - | - |")
    other_kind_asks = sum(1 for r in open_rows if r["kind"] not in KINDS)
    if other_kind_asks > cap:   # no leading digit cell, so the parity check never reads it as a card row
        out.append("| - | ... and %d more open owner asks of another kind (card-queue.json) | - | - | - | - |" % (
            other_kind_asks - cap))
    open_line = "product+playback open: %d" % len(open_rows)
    if other_kind_asks:
        open_line += " (incl. %d owner ask%s of another kind)" % (other_kind_asks, "" if other_kind_asks == 1 else "s")

    out += ["", "## Drain", "", open_line,
            "", "Window: the %d UTC days ending %s (the last day is partial)." % (WINDOW_DAYS, last.isoformat()),
            "Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp "
            "(approximate: a lower bound on the merge time, not a measurement); "
            "queued = rows of any state by `source_ledger_stamp`.", "",
            "| day (UTC) | landed | queued |", "|---|---|---|"]
    for d in days:
        out.append("| %s | %d | %d |" % (d.isoformat(), landed[d], queued[d]))
    out.append("| total | %d | %d |" % (landed_total, queued_total))
    owner_parked = sorted((r for r in parked if _undelivered_owner_ask(r)),
                          key=lambda r: (_instant(_parked_since(r)), r["priority"], r["card"], _canonical(r)))   # (r3 pass-7 A3) total order
    generated_at = _instant(generated)
    other_parked = [r for r in parked if not _undelivered_owner_ask(r)]
    out += ["", _drain_line(len(open_rows), landed_total, queued_total), "",
            "## Parked owner asks", ""]
    for r in owner_parked[:cap]:   # r2 A3: the longest-parked `cap` (OWNER_LIST_MAX unless the budget is short), then one count line
        since = _parked_since(r)
        age = max(0, math.floor((generated_at - _instant(since)) / 86400))   # elapsed whole days between exact instants, not a date difference
        out.append("- %s (P%s, %s): parked %d d (since %s): %s" % (
            r["card"], r["priority"], r["kind"], age, _utc_day(since).isoformat(), _note(r, OWNER_NOTE_MAX)))
    if len(owner_parked) > cap:
        out.append("- ... and %d more owner asks parked (card-queue.json)" % (len(owner_parked) - cap))
    if collapsed and any(r.get("origin") == OWNER for r in data["rows"]):
        out.append(OWNER_COLLAPSED_LINE)   # (r3 A1) the one owner line: no list, no count, no suffix
    elif not owner_parked:
        out.append("- (no owner ask is parked)")
    out += ["", "## Parked with reason", "", *scope]
    # every owner row here carries a resolution (an unresolved one is owed, above). One of another kind is tooling churn: counted, not listed.
    closed_other = [r for r in other_parked if r.get("origin") == OWNER and r["kind"] not in KINDS]
    closed_product_seen = 0   # (r2 pass-4 A2) resolved owner asks of product/playback kind are an owner list too: capped like the others
    for r in other_parked:
        if r.get("origin") == OWNER:
            if r["kind"] in KINDS:
                closed_product_seen += 1
                if closed_product_seen <= cap:
                    out.append("- %s (P%s, owner ask, %s): %s" % (r["card"], r["priority"], _resolution(r), _note(r)))
            continue
        out.append("- %s (P%s): %s" % (r["card"], r["priority"], _note(r)))
    if closed_product_seen > cap:
        out.append("- ... and %d more owner asks closed with a resolution (card-queue.json)" % (closed_product_seen - cap))
    if closed_other:
        counts = ["%s %d" % (v, n) for v in RESOLUTIONS for n in [sum(1 for r in closed_other if _resolution(r) == v)] if n]
        out.append("- %d owner ask%s of another kind %s closed with a resolution (%s; card-queue.json)" % (
            len(closed_other), "" if len(closed_other) == 1 else "s", "is" if len(closed_other) == 1 else "are", ", ".join(counts)))
    if not other_parked:
        out.append("- (none parked)")
    return "\n".join(out) + "\n"


def _stamp_problem(value: object) -> str:
    """'' when `value` is a stamp the renderer reads exactly, else why it is not (r2 pass-4 A1/A5/A7: the whole stamp is checked,
    not its date prefix). Accepted: YYYY-MM-DD (that day's UTC midnight), or YYYY-MM-DD[T ]HH:MM[:SS[.f]][Z|+HH:MM|-HH:MM]."""
    if not isinstance(value, str):
        return "not a string"
    spaces = [i for i, c in enumerate(value) if c.isspace()]   # (r3 A3) str.isspace(): CR, LF, TAB, VT, FF, NBSP, U+2003 ...
    if spaces and not (spaces == [10] and value[10] == " "):   # only the single-space date/time separator at index 10
        return "whitespace inside or around the stamp"
    if _DATE_ONLY_RX.fullmatch(value):
        try:
            _day(value)
        except ValueError:
            return "no such date"
        return ""
    m = _STAMP_RX.fullmatch(value)   # (r2 pass-5 A2) the stamp as written: a CR, LF or space around it is not stripped into validity
    if not m or "\n" in value:
        return "not a date or a date-time stamp"
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    if h > 23 or mi > 59 or int(m.group(6) or 0) > 59:
        return "hour, minute or second out of range"
    try:
        date(y, mo, d)
    except ValueError:
        return "no such date"
    if len(m.group(7) or "") > FRACTION_MAX_DIGITS:
        return "more than %d fractional digits" % FRACTION_MAX_DIGITS
    tz = m.group(8)
    if tz and tz.upper() != "Z" and (int(tz[1:3]) > 23 or int(tz[-2:]) > 59):
        return "offset out of range"
    try:
        date(1970, 1, 1) + timedelta(seconds=math.floor(_instant(value)))
    except OverflowError:
        return "its UTC day is outside 0001-01-01..9999-12-31"
    return ""


def _utf8_ok(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _non_utf8_text(data: object) -> str:
    """'' when every string in the parsed queue (dict keys included, at any depth) encodes as UTF-8, else the JSON path of the
    first one that does not (r3 A2 on 6c9167ce: a lone surrogate in `state` or `order_chain` passed _load and then crashed the
    UTF-8 budget measure). Iterative, so a deep queue cannot add a RecursionError. The path is ascii() text, safe for stderr."""
    stack = [("$", data)]
    while stack:
        where, value = stack.pop()
        if isinstance(value, str):
            if not _utf8_ok(value):
                return where
        elif isinstance(value, dict):
            for k, v in value.items():
                key_where = "%s[%s]" % (where, ascii(k))
                if isinstance(k, str) and not _utf8_ok(k):
                    return key_where + " (the key)"
                stack.append((key_where, v))
        elif isinstance(value, list):
            stack.extend(("%s[%d]" % (where, i), v) for i, v in enumerate(value))
    return ""


# (r3 pass-7 A2) text that lands in a table cell (state, order_chain.chain, order_chain.predecessor) is one printable line with
# no pipe: a line break there wrote a forged "| 3 | FORGED | ..." row that the parity reader counted as a card.
CELL_MAX = 120


def _cell_problem(value: object) -> str:
    """'' when `value` can stand in one table cell, else why not. str.isprintable() is False for CR, LF, TAB, U+2028, ESC ..."""
    if not isinstance(value, str):
        return "is not a string"
    if "|" in value or not value.isprintable():
        return "holds a pipe, a line break or another unprintable character"
    if len(value) > CELL_MAX:
        return "is longer than %d characters" % CELL_MAX
    return ""


def _valid_stamp(value: object) -> bool:
    return not _stamp_problem(value)


def _check_row(path: Path, i: int, r: dict) -> None:
    where = "%s row %d (%s)" % (path, i, r["card"])
    for key in ("source_ledger_stamp", "pinned_utc", "merged_utc"):
        if r.get(key) is not None and not _valid_stamp(r[key]):
            _die("%s has a malformed %s: %r (want YYYY-MM-DD...)" % (where, key, r[key]))
    for key in ("evidence", "notes", "origin"):
        if r.get(key) is not None and not isinstance(r[key], str):
            _die("%s has a non-string %s" % (where, key))
        if isinstance(r.get(key), str) and not _utf8_ok(r[key]):   # (r2 pass-5 A3) a lone surrogate cannot be written as UTF-8
            _die("%s has a %s that is not valid UTF-8 text (a lone surrogate)" % (where, key))
    res = r.get("resolution")   # r2 A1: a closed vocabulary; null is the same as absent (no resolution, so an owner ask stays owed)
    if res is not None and not (isinstance(res, str) and res in RESOLUTIONS):
        _die("%s has a bad resolution: %r (want one of %s, or none)" % (where, res, "|".join(RESOLUTIONS)))
    for token in DECLARED_TOKEN_RX.findall(r.get("evidence") or ""):
        token = token.rstrip(TOKEN_TRAILING_PUNCT)
        stamp = DECLARED_RX.fullmatch("declared@" + token)
        if not stamp or not _valid_stamp(stamp.group(1)):
            _die("%s has a malformed declared@ stamp in evidence: 'declared@%s' "
                 "(want declared@YYYY-MM-DDTHH:MM:SSZ)" % (where, token))
    oc = r.get("order_chain")
    if oc is None:
        return
    if not isinstance(oc, dict):
        _die("%s has an order_chain that is not an object: %r" % (where, oc))
    if not isinstance(oc.get("chain"), str):
        _die("%s has an order_chain without a string chain" % where)
    if not isinstance(oc.get("index"), int) or isinstance(oc["index"], bool):
        _die("%s has an order_chain without an integer index" % where)
    if oc.get("predecessor") is not None and not isinstance(oc["predecessor"], str):
        _die("%s has an order_chain with a non-string predecessor" % where)
    for key in ("chain", "predecessor"):   # (r3 pass-7 A2) both land in the order_chain cell of the open table
        if oc.get(key) is not None and _cell_problem(oc[key]):
            _die("%s has an order_chain whose %s %s: %r" % (where, key, _cell_problem(oc[key]), oc[key]))


def _check_scope_notes(path: Path, notes: object) -> None:
    if notes is None:
        return
    if not isinstance(notes, list):
        _die("%s has a scope_notes that is not a list: %r" % (path, notes))
    for i, n in enumerate(notes):
        if not isinstance(n, dict) or not all(isinstance(n.get(k), str) and n[k].strip() and _utf8_ok(n[k]) for k in ("title", "text")):
            _die("%s scope_notes entry %d needs a non-empty string title and text: %r" % (path, i, n))
        if not all(n[k].isprintable() for k in ("title", "text")):   # (r3 pass-7 A2 sibling) one paragraph line: no forged table row
            _die("%s scope_notes entry %d has a line break or an unprintable character in its title or text: %r" % (path, i, n))


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _die("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("rows"), list):
        _die("%s is not a %s file" % (path, SCHEMA))
    if not isinstance(data.get("generated_utc"), str):
        _die("%s has no generated_utc" % path)
    if not _valid_stamp(data["generated_utc"]):
        _die("%s has a malformed generated_utc: %r (want YYYY-MM-DD...)" % (path, data["generated_utc"]))
    try:   # (r2 pass-4 A6) the 7-day window ends on generated_utc's UTC day: its first day must exist too
        _utc_day(data["generated_utc"]) - timedelta(days=WINDOW_DAYS - 1)
    except OverflowError:
        _die("%s has a generated_utc whose %d-day window starts before 0001-01-01: %r" % (path, WINDOW_DAYS, data["generated_utc"]))
    _check_scope_notes(path, data.get("scope_notes"))
    for i, r in enumerate(data["rows"]):
        missing = [k for k in REQUIRED if not isinstance(r, dict) or r.get(k) is None]
        if missing:
            _die("%s row %d lacks %s" % (path, i, ", ".join(missing)))
        if not isinstance(r["card"], str) or not CARD_RX.fullmatch(r["card"]):
            _die("%s row %d has a bad card id: %r (want letters, digits, '.', '_', '-'; at most 120)" % (path, i, r["card"]))
        if not isinstance(r["kind"], str) or not KIND_RX.fullmatch(r["kind"]):
            _die("%s row %d (%s) has a bad kind: %r (want letters, digits, ' ', '.', '_', '-'; at most 40)" % (path, i, r["card"], r["kind"]))
        if not isinstance(r["priority"], int):
            _die("%s row %d (%s) has a non-integer priority" % (path, i, r["card"]))
        if _cell_problem(r["state"]):   # (r3 pass-7 A2) the state cell of the open table
            _die("%s row %d (%s) has a bad state: %r %s" % (path, i, r["card"], r["state"], _cell_problem(r["state"])))
        _check_row(path, i, r)
    # (r3 A2) the class fix: the named per-field checks above keep their messages; this walk refuses every other string too.
    bad = _non_utf8_text(data)
    if bad:
        _die("%s has text that is not valid UTF-8 (a lone surrogate) at %s" % (path, bad))
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render docs/roadmap.md from card-queue.json.")
    ap.add_argument("--card-queue", required=True, type=Path, help="board-local card-queue.json")
    ap.add_argument("--out", type=Path, help="the roadmap file to write or check (required unless --drain-line)")
    ap.add_argument("--check", action="store_true", help="exit 1 when --out differs; write nothing")
    ap.add_argument("--drain-line", action="store_true",
                    help="print the one-line drain summary for the board snapshot; write nothing")
    args = ap.parse_args(argv)

    data = _load(args.card_queue)
    if args.drain_line:
        print(drain_line(data))
        return 0
    if args.out is None:
        ap.error("--out is required unless --drain-line")
    text = render(data)
    if args.check:
        try:
            on_disk = args.out.read_bytes().decode("utf-8").replace("\r\n", "\n")
        except OSError:
            print("render_roadmap: %s is missing" % args.out, file=sys.stderr)
            return 1
        if on_disk != text:
            print("render_roadmap: %s is stale; re-run without --check" % args.out, file=sys.stderr)
            return 1
        return 0
    args.out.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
