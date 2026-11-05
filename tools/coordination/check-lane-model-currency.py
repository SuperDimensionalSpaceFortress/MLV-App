"""Report lanes that ran an older model than their family's newest, or any effort but 'high'.

MODEL-POSTURE-R16-1 (doctrine R16: always the latest model of each family, all at high effort).
Invoke-Lane.ps1 records, per run, the lane, the model id that actually ran (`resolvedModel`),
the effort and `startedUtc` in <runs-dir>/<run>/<lane>-001.receipt.json. This script reads those
receipts and answers two questions per lane, inside a recency window (--since-hours, default 72;
0 = every receipt):

  * STALE-MODEL  the lane's two most recent runs BOTH used an id older than the newest id seen
                 for that model FAMILY -- "twice in a row", so one run that was already in flight
                 when a new model shipped does not raise it. Families compare by family, never by
                 lane: 'claude-opus' / 'claude-sonnet' / 'claude-fable' for the claude CLI, the
                 tier ('sol', 'luna', 'astra', 'terra') for a codex slug 'gpt-<version>-<tier>'. Versions
                 compare numerically ('6.1' beats '6'; 'claude-opus-5-5' beats 'claude-opus-5').
  * BAD-EFFORT   any receipt in the window whose recorded effort is anything but 'high'. A receipt
                 that recorded no effort (an old schema) is listed under `unrecordedEffort`, never
                 silently counted as high and never flagged as a violation.

The newest id per family is the highest version among the receipts read, raised by any
--newest FAMILY=ID the caller supplies (the way to say "the CLI now offers claude-opus-5-6" before
any lane has run it). Only receipts that proved a model (a parseable resolvedModel; 'unknown' and
null never count) take part in the model rule.

Missing evidence is never CURRENT (r3 blocker B1; one hub-ruled exception, named after IN-FLIGHT below). A lane's verdict is, in order:

  STALE       a STALE-MODEL or BAD-EFFORT reason above.
  NO-DATA     the lane has no proven FINISHED run, or its newest run in the window that is not a live
              candidate is unproven (a failed run with resolvedModel null/'unknown', or a running receipt that is
              not a live candidate, i.e. an expired one, whatever model it carries): what the lane runs now is
              unproven. A receipt whose state is `running` is never a proven run (r5, B1).
  INCOMPLETE  the model is proven but at least one finished run in the window recorded no effort, so
              'high' is unproven; counted in `unrecordedEffort` and shown in the table. The one
              intentional exemption is a live-candidate running receipt (see below), which has no effort
              yet by design and is not counted.
  IN-FLIGHT   the lane's newest receipt is a LIVE CANDIDATE running run (an age window, not a process
              probe), AND the lane's newest finished run is proven and would itself read CURRENT. See the
              next paragraph.
  CURRENT     a proven model, every effort recorded and 'high'. CURRENT describes HISTORICAL model and
              effort evidence in the receipts; it is not a statement that any process is running.

IN-FLIGHT (CURRENCY-CHECK-WIRED-INTO-HUB-TICK-1). A running receipt records no resolvedModel and no
effort yet (Invoke-Lane.ps1 writes both at the end), so before this verdict a lane that was merely busy
read NO-DATA and pulled the overall verdict to INCOMPLETE. A receipt counts as a LIVE CANDIDATE when it
says state=running, has no endedUtc, has a finite timeoutSec between 0 (exclusive) and
MAX_LIVE_TIMEOUT_SEC (48 h; nan, inf, a non-positive or an absurd value is no bound, never a crash), is not
dated more than MAX_FUTURE_START_SKEW_SEC (300 s) past now (a receipt dated past the skew bound has no age, so it
is never a candidate), and now is no later than startedUtc + timeoutSec + LIVE_GRACE_SEC (300 s: the
wrapper's post-run cleanup and the hub clock's measured skew of up to ~20 s).
ONE STAMP SHAPE (r6, hub ruling 20261105T1245Z; it replaces the per-spelling rules of r3 to r5). The checker reads
a startedUtc only if the RAW text fullmatches YYYY-MM-DDTHH:MM:SS(.d{1,9})?(Z|+-HH:MM) (ASCII digits, upper-case T
and Z; LIVE_START_RE): no strip, no comma normalization, no basic, week or ordinal date, no other separator, no other
offset. Every other string, a date-only one included, is unreadable: counted in `unreadableReceipts` like any
malformed timestamp and left out of the verdict, never read as a failure or a success. So no spelling can be read as
something it was not, and the sort key is built from that one shape only. The cost is that a hand-written receipt that
an earlier version read can disappear into `unreadableReceipts`; a real one never does (a census of 8301 real receipts
found 8290 with a startedUtc, every one in the writer's shape). A live candidate additionally needs a timeoutSec that
is a JSON int or a number whose RAW token has at most 15 significant digits (exact_timeout: json.load rounds a
longer token before the script could see it, so the digits are counted on the token itself; padding zeros do not
count); anything else is no candidate and stays unproven, as before this PR. The arithmetic on a candidate stays
exact. A candidate is an AGE WINDOW derived
from the receipt's own start time and timeout, not a process probe: this script reads receipts only, it
never proves that a process exists, and the board's authoritative liveness rule (hub-queue-lib.ps1
Get-LaneRunState: child pid alive, command line names the run dir, start time matches within 10 s) needs
the process table. The cost of the weaker rule is bounded and one-sided: a lane that died inside its own
timeout window still reads IN-FLIGHT, and only ever when the finished run before it is CURRENT, so it can hide
nothing worse than a lane that is current anyway. A receipt left at state=running outside the window
(expired-unproven, or dated too far ahead) is not a candidate: it stays an unproven run (counted in
`deadRunning`, a name kept for the JSON contract), never IN-FLIGHT. A running receipt with no timeoutSec
cannot be bounded and is treated the same way.
A running receipt is never a proven run, live or expired (r5, B1): whatever resolvedModel and effort it carries,
it is left out of the proven runs, `latestModel`, `provenModelRuns`, `judgedOn` and the family's newest id (a
running receipt that already names a newer id cannot make finished runs STALE, and an expired one cannot hide a
STALE pair either), and the lane is judged on its finished runs alone, with the run it was judged on named in
`judgedOn` and in the table. A LIVE candidate is also left out of the effort rule and `unrecordedEffort`; an
EXPIRED running receipt is not (it is an unproven run that may have recorded no effort, so a lane whose
expired receipt is older than a proven finished run and recorded no effort reads INCOMPLETE, not CURRENT).
Two consequences, both NO-DATA unless a STALE-MODEL or BAD-EFFORT reason outranks them (that reads STALE): a lane
whose newest non-live run is an expired running receipt (the expired receipt alone, a finished run followed by
one, or an older live receipt beside one) and a lane with no proven finished run in the window. IN-FLIGHT is given only when the newest FINISHED run is proven too: a failed run in
between is not papered over (the lane stays NO-DATA), because missing evidence is never CURRENT.
ONE EXCEPTION to "missing evidence is never CURRENT" (hub-ruled trade, r4; r6 widens what counts as unreadable
to every stamp outside the one shape above): a newest FAILED run whose startedUtc is unreadable is dropped from
the lane (counted in `unreadableReceipts`), so the lane is judged on the runs that could be read and can read
CURRENT where the base read NO-DATA.

The overall verdict is STALE if any lane is, else INCOMPLETE if any lane is NO-DATA or INCOMPLETE,
else NO-DATA when no lane ran in the window, else CURRENT. IN-FLIGHT lanes count as CURRENT here and are
listed in `inFlightLanes`: being busy alone never makes the overall verdict INCOMPLETE.

Exit code (unchanged numbers; IN-FLIGHT adds none): 0 overall CURRENT (IN-FLIGHT lanes included), 1 at
least one STALE-MODEL or BAD-EFFORT, 2 the runs directory is unreadable, 3 overall INCOMPLETE or NO-DATA
(evidence missing, not a violation). One JSON object with --json, else a short table.

--posture swaps the receipt check for a BOUNDED TEXT LINT (MODEL-POSTURE-R19-ADOPT-1, bus R19.1
clause 3): Astra is an occasional diversity seat, never a default or a gate. It is NOT proof that Astra
cannot become required. It checks exactly three things: (1) every agents/orchestration-tiering.md table
row that mentions Astra contains the marker substrings optional / never a default / never a gate /
`astra: diversity` somewhere in the row and names the Sol tier ahead of Astra in its model cell; (2) the
first contiguous `# astra --` comment block in tools/coordination/Invoke-Lane.ps1 contains the same
markers; (3) that block does not contain the word tie-breaker. KNOWN LIMIT: it does not read the
meaning of the text, so a row or comment that keeps every marker but states a contradictory duty
(e.g. "Astra is required for every design pass"), or a duty stated outside the checked row or comment
block, still passes; the tests pin both as known limits. Exit 0 clean, 1 a violation, 2 a file
unreadable. The `role` string value in $LANES is not judged: a guardrail test pins it, and it is a
legacy receipt label, not routing authority.

Usage: python check-lane-model-currency.py [--runs-dir DIR] [--since-hours N] [--newest FAMILY=ID]... [--json]
       python check-lane-model-currency.py --posture [--repo-root DIR]
"""
import argparse
import datetime
import decimal
import fractions
import glob
import json
import math
import os
import re
import subprocess
import sys

CLAUDE_ID_RE = re.compile(r"^claude-(?P<family>[a-z]+)-(?P<version>[0-9]+(?:-[0-9]+)*?)(?:-[0-9]{8})?$")
CODEX_ID_RE = re.compile(r"^gpt-(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<family>[a-z0-9]+)$")
REQUIRED_EFFORT = "high"
CONSECUTIVE_STALE_RUNS = 2
DEFAULT_SINCE_HOURS = 72.0
LIVE_GRACE_SEC = 300.0
MAX_FUTURE_START_SKEW_SEC = 300     # a running receipt may be dated at most this far past now (clock skew)
MAX_LIVE_TIMEOUT_SEC = 172800.0     # 48 h; the longest timeoutSec seen on a real lane is 12600 s
# THE ONE startedUtc SHAPE (r6, hub ruling 20261105T1245Z): what Invoke-Lane.ps1 writes (ToString('o') of a UTC
# DateTime: 'Z' and 7 digits) plus the ordinary ISO 8601 neighbours (1-9 fraction digits, an HH:MM offset). ASCII
# digits only, hour 00-23 (3.14's fromisoformat reads 24 as the next day, 3.13's refuses it), matched with fullmatch
# against the RAW text: no strip, no comma normalization, no basic, week or ordinal date, no other separator, no
# other offset. Every other string is unreadable, so no spelling is ever read
# as something it was not (six rounds of per-spelling rules each left the next spelling for the next pass).
LIVE_START_RE = re.compile(r"(?P<head>[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-9]{2}:[0-9]{2})"
                           r"(?:\.(?P<fraction>[0-9]{1,9}))?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})")
MAX_TIMEOUT_SIGNIFICANT_DIGITS = 15  # a decimal this short survives the trip through a binary float unchanged
MAX_TIMEOUT_EXPONENT = 30            # |decimal exponent of the leading digit|; keeps Fraction() cheap on a hostile token
RECEIPT_GLOB = "*/*-001.receipt.json"
TIERING_REL = "agents/orchestration-tiering.md"
INVOKE_LANE_REL = "tools/coordination/Invoke-Lane.ps1"
ASTRA_ROW_MARKERS = ("optional", "never a default", "never a gate", "astra: diversity")
POSTURE_LINT_SCOPE = ("bounded text lint (marker substrings, their order, one comment block); "
                      "not proof that Astra cannot become required")
ASTRA_COMMENT_MARKERS = ("optional diversity seat", "never a default", "never a gate", "astra: diversity")


def parse_model_id(model_id):
    """'claude-opus-5-5' -> ('claude-opus', (5, 5)); 'gpt-6.1-sol' -> ('sol', (6, 1)); else None."""
    if not isinstance(model_id, str):
        return None
    text = model_id.strip()
    match = CLAUDE_ID_RE.match(text)
    if match:
        return "claude-" + match.group("family"), tuple(int(p) for p in match.group("version").split("-"))
    match = CODEX_ID_RE.match(text)
    if match:
        try:
            return match.group("family"), tuple(int(p) for p in match.group("version").split("."))
        except ValueError:
            return None
    return None


def sub_microsecond_digits(text):
    """The digits of the seconds fraction past the sixth, trailing zeros removed ('' when there are none, and for a
    stamp that is not in the one readable shape, which parse_utc cannot read either)."""
    match = LIVE_START_RE.fullmatch(text) if isinstance(text, str) else None
    fraction = match.group("fraction") if match else None
    return fraction[6:].rstrip("0") if fraction else ""


def parse_utc(text):
    """The aware datetime (microsecond-truncated) of a stamp in the one readable shape, LIVE_START_RE; else None."""
    match = LIVE_START_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    fraction = match.group("fraction")
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    micro = "." + fraction[:6].ljust(6, "0") if fraction else ""
    try:
        return datetime.datetime.fromisoformat(match.group("head") + micro + zone)
    except ValueError:
        return None


class RawFloat(float):
    """A float that remembers the JSON token it was read from (json.load alone would round a long one)."""
    raw = None


class RawInt(int):
    raw = None


def _raw_float(token):
    value = RawFloat(token)
    value.raw = token
    return value


def _raw_int(token):
    value = RawInt(token)
    value.raw = token
    return value


def exact_timeout(value):
    """The exact value of a timeoutSec that may take part in the liveness test, as a Fraction; else None.

    Judged on the RAW JSON token (r3, adversary pass 5 A1): json.load turns 172800.000000000001 into 172800.0 and
    0.000000099999999999999999 into 1e-07, so the digits must be counted before any float exists. Live only when the
    token is a JSON int or float with at most MAX_TIMEOUT_SIGNIFICANT_DIGITS significant digits (padding zeros do
    not count: 172800.000000000000 is exactly 172800) and a decimal exponent within MAX_TIMEOUT_EXPONENT. A value
    that did not come from a number token (nan, inf, a string, a bool, a list) is None."""
    raw = getattr(value, "raw", None)
    if not isinstance(raw, str):
        return None
    try:
        parts = decimal.Decimal(raw).as_tuple()
    except decimal.InvalidOperation:
        return None
    if not isinstance(parts.exponent, int):         # 'NaN' / 'Infinity' never come through parse_int/parse_float
        return None
    digits, exponent = list(parts.digits), parts.exponent
    while digits and digits[0] == 0:
        digits.pop(0)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return fractions.Fraction(0)    # any zero ('0', '0.0', '0e-999999999999999999'): never reaches a power below
    if len(digits) > MAX_TIMEOUT_SIGNIFICANT_DIGITS:
        return None
    if abs(exponent + len(digits) - 1) > MAX_TIMEOUT_EXPONENT:
        return None
    number = fractions.Fraction(int("".join(map(str, digits)) or "0")) * fractions.Fraction(10) ** exponent
    return -number if parts.sign else number


def default_runs_dir():
    """<repo>/.claude-state/fleet-runs of the MAIN checkout: .claude-state is untracked, so a
    linked worktree has none; git's common dir leads back to the checkout that does."""
    override = os.environ.get("MLV_FLEET_RUNS_DIR")
    if override:
        return override
    script_dir = os.path.dirname(os.path.abspath(__file__))
    common = ""
    # --path-format needs git >= 2.31; an older git rejects it, so retry bare.
    for flags in (["--path-format=absolute", "--git-common-dir"], ["--git-common-dir"]):
        try:
            done = subprocess.run(["git", "rev-parse"] + flags, capture_output=True, text=True,
                                  timeout=15, cwd=script_dir)
        except (OSError, subprocess.SubprocessError):
            break
        if done.returncode == 0 and done.stdout.strip():
            common = done.stdout.strip().splitlines()[-1]
            break
    # git prints a path relative to ITS cwd (the script dir) from the main checkout; join it to
    # script_dir, never to the caller's cwd (a no-op when it is already absolute).
    root = os.path.dirname(os.path.abspath(os.path.join(script_dir, common))) if common else os.getcwd()
    return os.path.join(root, ".claude-state", "fleet-runs")


def load_receipts(runs_dir):
    """-> list of normalized dicts, oldest first. Unreadable receipts are counted, not fatal."""
    receipts, unreadable = [], 0
    for path in glob.glob(os.path.join(runs_dir, RECEIPT_GLOB)):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle, parse_float=_raw_float, parse_int=_raw_int)
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("lane"), str):
            unreadable += 1
            continue
        started = parse_utc(data.get("startedUtc"))
        if started is None:
            unreadable += 1
            continue
        # parse_utc keeps microseconds; Invoke-Lane writes 7 fractional digits (100 ns ticks) and the one readable
        # shape allows up to 9. The 0-3 digits it drops are kept here (`subus`) as a string with trailing zeros
        # removed, which compares in the numeric order of the fraction ('09' < '1', '12' < '123', equal fractions
        # equal). `order` ties only on a true tie; `subus` also feeds the exact liveness test.
        subus = sub_microsecond_digits(data.get("startedUtc"))
        order = (started, subus)
        resolved = data.get("resolvedModel")
        timeout = data.get("timeoutSec")
        receipts.append({
            "path": path, "lane": data["lane"], "started": started, "order": order, "subus": subus,
            "state": data.get("state"),
            "ended": data.get("endedUtc"),
            "timeoutSec": timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None,
            # the exact timeout a live candidate needs, decided once here on the RAW token (r3)
            "timeoutExact": exact_timeout(timeout),
            "card": data.get("card"), "effort": data.get("effort"),
            "resolvedModel": resolved if isinstance(resolved, str) else None,
            "parsed": parse_model_id(resolved),
        })
    receipts.sort(key=lambda r: r["order"])
    return receipts, unreadable


def newest_per_family(receipts, overrides):
    newest = {}
    for receipt in receipts:
        if receipt["parsed"]:
            family, version = receipt["parsed"]
            if family not in newest or version > newest[family][0]:
                newest[family] = (version, receipt["resolvedModel"])
    for family, model_id in overrides.items():
        parsed = parse_model_id(model_id)
        if parsed and parsed[0] == family and (family not in newest or parsed[1] > newest[family][0]):
            newest[family] = (parsed[1], model_id)
    return newest


def is_live_running(receipt, now):
    """True only for a receipt that is a live CANDIDATE: its own start time and timeout allow a run in progress.
    This is an age window, not a process probe; it does not prove anything is running (see the docstring)."""
    if receipt["state"] != "running" or receipt["ended"]:
        return False
    # startedUtc is already in the one readable shape: a receipt with any other stamp is unreadable and never gets
    # here. timeoutSec counts only when its RAW token has at most 15 significant digits (exact_timeout; json.load
    # rounds a longer one), else the receipt is not provably inside the window and is no candidate.
    timeout = receipt.get("timeoutExact")
    try:
        # a missing, non-positive or over-cap value is not a bound at all (nan, inf and non-numbers are None here)
        if timeout is None or not 0 < timeout <= int(MAX_LIVE_TIMEOUT_SEC):
            return False
        # Bounded at both ends: not older than timeout + grace, and not dated past the future-skew bound. A
        # receipt dated past that bound has no age at all, so it is no evidence that a run is in progress.
        # The arithmetic is EXACT (fractions, no timedelta rounding), so the bounds hold to the last digit the
        # receipt wrote: the microsecond-truncated `started`, the digits it dropped (`subus`) and the timeout
        # (its exact decimal value, `timeoutExact`) all enter unrounded.
        micro = (receipt["started"] - now) // datetime.timedelta(microseconds=1)
        digits = receipt.get("subus") or ""
        start_minus_now = fractions.Fraction(micro, 10 ** 6)
        if digits:
            start_minus_now += fractions.Fraction(int(digits), 10 ** len(digits)) / 10 ** 6
        if start_minus_now > MAX_FUTURE_START_SKEW_SEC:
            return False
        return start_minus_now + timeout + int(LIVE_GRACE_SEC) >= 0
    except (OverflowError, ValueError, TypeError):
        return False        # not provably inside the window


def evaluate(receipts, since_hours=DEFAULT_SINCE_HOURS, now=None, overrides=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    # The family's newest id is judged over every receipt that is not `running`, however old; the window only decides
    # what is reported. A `running` receipt is never evidence, live or expired (r5, B1), so it cannot raise the newest id.
    live_ids = {id(r) for r in receipts if is_live_running(r, now)}
    newest = newest_per_family([r for r in receipts if r["state"] != "running"], overrides or {})
    window = list(receipts)
    if since_hours and since_hours > 0 and math.isfinite(since_hours):
        try:
            cutoff = now - datetime.timedelta(hours=since_hours)
            window = [r for r in receipts if r["started"] >= cutoff]
        except OverflowError:
            pass            # a window wider than any representable date reaches every receipt
    lanes = {}
    for receipt in window:
        lanes.setdefault(receipt["lane"], []).append(receipt)
    report = {}
    for lane in sorted(lanes):
        # A live running receipt has no model or effort yet: it is neither evidence nor a gap.
        live = [r for r in lanes[lane] if id(r) in live_ids]
        runs = [r for r in lanes[lane] if id(r) not in live_ids]
        dead_running = [r for r in runs if r["state"] == "running"]
        # Proven = a FINISHED run that named a parseable model. An expired running receipt is not finished, so
        # whatever model and effort it carries prove nothing (r5, B1); it stays in `runs` as an unproven run.
        proven = [r for r in runs if r["parsed"] and r["state"] != "running"]
        reasons = []
        # STALE-MODEL: the two most recent proven runs are both older than their family's newest.
        tail = proven[-CONSECUTIVE_STALE_RUNS:]
        stale_tail = len(tail) == CONSECUTIVE_STALE_RUNS and all(
            r["parsed"][1] < newest[r["parsed"][0]][0] for r in tail)
        if stale_tail:
            family = tail[-1]["parsed"][0]
            reasons.append({
                "kind": "STALE-MODEL", "family": family, "newest": newest[family][1],
                "ranModels": [r["resolvedModel"] for r in tail], "receipts": [r["path"] for r in tail]})
        bad = [r for r in runs if isinstance(r["effort"], str) and r["effort"].strip() and r["effort"] != REQUIRED_EFFORT]
        if bad:
            reasons.append({
                "kind": "BAD-EFFORT", "efforts": sorted({r["effort"] for r in bad}),
                "receipts": [r["path"] for r in bad[-5:]], "count": len(bad)})
        unrecorded = [r for r in runs if not (isinstance(r["effort"], str) and r["effort"].strip())]
        latest = proven[-1] if proven else None
        if reasons:
            verdict = "STALE"
        elif not proven or not runs[-1]["parsed"] or runs[-1]["state"] == "running":
            # No proven finished run, or the newest non-live run is unproven: a failed run, or an expired running
            # receipt (which never finished, so what it carries is no evidence and IN-FLIGHT has no foundation:
            # adversary pass 4 A3, and r5 B1 for the case with no live receipt on top).
            verdict = "NO-DATA"
        elif unrecorded:
            verdict = "INCOMPLETE"
        elif live and live[-1]["order"] >= runs[-1]["order"]:     # >=: an exact tie reads the running run as newer
            verdict = "IN-FLIGHT"      # newest receipt is running; the newest finished run is proven and CURRENT
        else:
            verdict = "CURRENT"
        report[lane] = {
            "verdict": verdict,
            "runsInWindow": len(lanes[lane]), "provenModelRuns": len(proven),
            "latestModel": latest["resolvedModel"] if latest else None,
            "familyNewest": newest[latest["parsed"][0]][1] if latest else None,
            "reasons": reasons, "unrecordedEffort": len(unrecorded),
            "inFlightRuns": len(live), "deadRunning": len(dead_running),
            "judgedOn": ({"model": runs[-1]["resolvedModel"], "startedUtc": runs[-1]["started"].isoformat(),
                          "receipt": runs[-1]["path"]}
                         if proven and runs[-1]["parsed"] and runs[-1]["state"] != "running" else None),
        }
    verdicts = {entry["verdict"] for entry in report.values()}
    in_flight = sorted(lane for lane, entry in report.items() if entry["verdict"] == "IN-FLIGHT")
    if "STALE" in verdicts:
        overall = "STALE"
    elif verdicts - {"CURRENT", "IN-FLIGHT"}:
        overall = "INCOMPLETE"
    else:
        overall = "CURRENT" if report else "NO-DATA"
    # Reported as the documented 0 (= every receipt) when the window is not a finite number: inf and nan
    # are not JSON.
    reported_hours = since_hours if isinstance(since_hours, (int, float)) and math.isfinite(since_hours) and since_hours > 0 else 0
    return {
        "sinceHours": reported_hours, "receiptsRead": len(receipts), "receiptsInWindow": len(window),
        "familyNewest": {family: pair[1] for family, pair in sorted(newest.items())},
        "lanes": report, "verdict": overall, "inFlightLanes": in_flight,
    }


def parse_overrides(items):
    overrides = {}
    for item in items or []:
        family, sep, model_id = item.partition("=")
        parsed = parse_model_id(model_id)
        if not sep or not parsed or parsed[0] != family:
            raise ValueError("--newest %r must be FAMILY=ID with the id belonging to that family "
                             "(e.g. claude-opus=claude-opus-5-6, sol=gpt-6.2-sol)" % item)
        overrides[family] = model_id
    return overrides


def render(result):
    lines = ["lane model currency (window %sh, %d of %d receipts) -> %s" % (
        result["sinceHours"], result["receiptsInWindow"], result["receiptsRead"], result["verdict"])]
    for lane, entry in result["lanes"].items():
        lines.append("  %-7s %-10s latest=%s family-newest=%s runs=%d%s%s%s" % (
            lane, entry["verdict"], entry["latestModel"], entry["familyNewest"], entry["runsInWindow"],
            " unrecordedEffort=%d" % entry["unrecordedEffort"] if entry["unrecordedEffort"] else "",
            " judged-on=%s" % entry["judgedOn"]["model"] if entry["verdict"] == "IN-FLIGHT" else "",
            " deadRunning=%d" % entry["deadRunning"] if entry["deadRunning"] else ""))
        for reason in entry["reasons"]:
            if reason["kind"] == "STALE-MODEL":
                lines.append("          STALE-MODEL: ran %s, newest %s %s" % (
                    ", ".join(reason["ranModels"]), reason["family"], reason["newest"]))
            else:
                lines.append("          BAD-EFFORT: %s x%d" % ("/".join(reason["efforts"]), reason["count"]))
    return "\n".join(lines)


def posture_violations(tiering_text, invoke_lane_text):
    """Bounded text lint of the two texts (bus R19.1 clause 3); [] when clean.

    Checks marker substrings, their order (Sol before Astra) and one comment block, nothing more. It is
    not proof that Astra cannot become required: a contradictory duty that keeps every marker passes."""
    problems = []
    for line in tiering_text.splitlines():
        if not line.lstrip().startswith("|") or "astra" not in line.lower():
            continue
        lowered = line.lower()
        cells = line.strip().strip("|").split("|")
        label = cells[0].strip()
        missing = [m for m in ASTRA_ROW_MARKERS if m not in lowered]
        if missing:
            problems.append("%s row %r names Astra without %s" % (TIERING_REL, label, ", ".join(missing)))
        model_cell = cells[1].lower() if len(cells) > 1 else lowered
        if "sol" not in model_cell or model_cell.find("astra") < model_cell.find("sol"):
            problems.append("%s row %r must name the Sol tier ahead of Astra in its model cell" % (TIERING_REL, label))
    block, grabbing = [], False
    for line in invoke_lane_text.splitlines():
        if re.match(r"^#\s+astra\s+--", line):
            grabbing = True
        elif grabbing and not re.match(r"^#\s{6,}\S", line):
            break
        if grabbing:
            block.append(line)
    joined = re.sub(r"\s+", " ", re.sub(r"(^|\s)#", " ", "\n".join(block))).strip().lower()
    if not joined:
        problems.append("%s has no `# astra --` comment block to carry the posture" % INVOKE_LANE_REL)
    else:
        missing = [m for m in ASTRA_COMMENT_MARKERS if m not in joined]
        if missing:
            problems.append("%s astra comment lacks %s" % (INVOKE_LANE_REL, ", ".join(missing)))
        if "tie-breaker" in joined:
            problems.append("%s astra comment still calls astra the tie-breaker" % INVOKE_LANE_REL)
    return problems


def check_posture(repo_root):
    """-> (problems, error). error is 'posture-file-unreadable:<path>' when a file cannot be read."""
    texts = []
    for rel in (TIERING_REL, INVOKE_LANE_REL):
        path = os.path.join(repo_root, *rel.split("/"))
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                texts.append(handle.read())
        except OSError:
            return [], "posture-file-unreadable:%s" % path
    return posture_violations(texts[0], texts[1]), None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs-dir", default=None)
    parser.add_argument("--since-hours", type=float, default=DEFAULT_SINCE_HOURS)
    parser.add_argument("--newest", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--posture", action="store_true")
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(argv)

    if args.posture:
        repo_root = args.repo_root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        problems, error = check_posture(repo_root)
        if error:
            print(json.dumps({"ok": False, "error": error}))
            return 2
        print(json.dumps({"ok": not problems, "problems": problems, "scope": POSTURE_LINT_SCOPE}, indent=2))
        return 1 if problems else 0

    runs_dir = args.runs_dir or default_runs_dir()
    if not os.path.isdir(runs_dir):
        print(json.dumps({"ok": False, "error": "runs-dir-not-found:%s" % runs_dir}))
        return 2
    try:
        overrides = parse_overrides(args.newest)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": "bad-newest:%s" % exc}))
        return 2
    receipts, unreadable = load_receipts(runs_dir)
    result = evaluate(receipts, args.since_hours, overrides=overrides)
    result["ok"] = True
    result["runsDir"] = runs_dir
    result["unreadableReceipts"] = unreadable
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    return {"CURRENT": 0, "STALE": 1}.get(result["verdict"], 3)


if __name__ == "__main__":
    sys.exit(main())
