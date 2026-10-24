#!/usr/bin/env python3
"""Provider/model topology section for an MLV PR body (card PR-PROVIDER-MODEL-TOPOLOGY-1).

RECORD-ONLY transparency, NOT a gate (owner ask 2026-10-24, hub ACK on MLV-App #660). A missing or
stale section yields one advisory WARN line and never a refusal; this tool adds no merge precondition.

Three subcommands, stdlib only:

  render  --pr N --head <40-hex> --card <CARD> --runs-root <dir> [--footer "role=text"]...
          Print ONE markdown block between the begin/end markers, titled "Provider/model topology".
  splice  --body-file <in> --block-file <block> --out <out>
          Replace the existing begin/end block in a PR body, or append one. Idempotent; bytes outside
          the markers are untouched. A body whose markers are malformed is refused (exit 2) and
          nothing is written, so no body text can be duplicated or lost.
  check   --body-file <f> --head <40-hex>
          Print exactly one line: PR_TOPOLOGY ok | WARN missing | WARN stale head=<sha8> section=<sha8>
          | WARN unreadable <why>. ALWAYS exits 0.

Where the facts live: lane receipts `<runs-root>/<run dir>/<lane>-NNN.receipt.json`
(schema mlv-app/fleet-lane-receipt/v1, written by tools/coordination/Invoke-Lane.ps1). The run-dir
NAME classifies the role; the receipt supplies engine, resolved model and effort; a review key's
`<lane>-NNN.last.txt` verdict JSON supplies `subject_sha`; a producer's `summary.md` first line
`DONE <sha>` supplies the round's resulting head. Anything that cannot be derived is written as
`unknown`, `unbound` or `claimed: ...`, never guessed. Independence is never inferred from a
Co-Authored-By footer.
"""
import argparse
import json
import os
import re
import sys

BEGIN = "<!-- pr-topology:begin -->"
END = "<!-- pr-topology:end -->"
HEAD_PREFIX = "<!-- pr-topology:head "
HEAD_RE = re.compile(r"<!-- pr-topology:head ([0-9a-fA-F]{40}) -->")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
DONE_RE = re.compile(r"^DONE\s+([0-9a-fA-F]{40})\b")

STALE = "stale (other head)"
UNBOUND = "unbound"
UNREADABLE_MODEL = "unknown (receipt unreadable)"
CI_LINE = "CI (GitHub Actions; not a model)"
PROVIDERS = {"claude": "Anthropic", "codex": "OpenAI"}
FAMILIES = {"claude": "Claude family", "codex": "Codex family"}

MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_BRACE_TRIES = 200

# Sort groups: implementer, fixers, design, keys, then roles named only by a receipt.
G_IMPL, G_FIX, G_DESIGN, G_KEY, G_OTHER = range(5)


def _read_capped(path, cap):
    with open(path, "rb") as fh:
        return fh.read(cap)


def _norm_sha(value):
    if isinstance(value, str) and SHA_RE.match(value.strip()):
        return value.strip().lower()
    return None


def _cell(text):
    return str(text).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _load_receipt(path):
    """Return (dict, None) or (None, reason). Never raises."""
    try:
        data = json.loads(_read_capped(path, MAX_RECEIPT_BYTES).decode("utf-8"))
    except Exception as exc:  # corrupt, truncated, undecodable, vanished
        return None, "%s" % type(exc).__name__
    if not isinstance(data, dict):
        return None, "root is not an object"
    return data, None


def _key_subject_sha(last_path):
    """The final verdict JSON's subject_sha (else verified_head) from a key's .last.txt, or None."""
    try:
        text = _read_capped(last_path, MAX_TEXT_BYTES).decode("utf-8", "replace")
    except Exception:
        return None
    dec = json.JSONDecoder()
    end = len(text)
    tries = 0
    while True:
        at = text.rfind('"subject_sha"', 0, end)
        if at < 0:
            return None
        end = at
        pos = at
        while tries < MAX_BRACE_TRIES:
            pos = text.rfind("{", 0, pos)
            if pos < 0:
                break
            tries += 1
            try:
                obj, stop = dec.raw_decode(text, pos)
            except ValueError:
                continue
            if isinstance(obj, dict) and stop > at and "subject_sha" in obj:
                return _norm_sha(obj.get("subject_sha")) or _norm_sha(obj.get("verified_head"))
        if tries >= MAX_BRACE_TRIES:
            return None


def _producer_head(run_dir):
    """The round's resulting head: `DONE <40-hex>` as the first line of summary.md, or None."""
    try:
        text = _read_capped(os.path.join(run_dir, "summary.md"), 4096).decode("utf-8", "replace")
    except Exception:
        return None
    first = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    m = DONE_RE.match(first.strip())
    return m.group(1).lower() if m else None


def _classify(name, card, pr):
    """Map a run-dir name to (group, k, label, seat, key3) or None when it is not ours."""
    m = re.match(r"^lane-%s-r(\d+)-" % re.escape(card), name)
    if m:
        k = int(m.group(1))
        return (G_IMPL, k, "implementer r1", None, False) if k == 1 else (G_FIX, k, "fixer r%d" % k, None, False)
    if re.match(r"^design-%s-" % re.escape(card), name):
        return (G_DESIGN, 0, "design reviewer", None, False)
    m = re.match(r"^review-pr%d-([a-z0-9]+?)(-key3)?-r(\d+)-" % pr, name)
    if m:
        return (G_KEY, int(m.group(3)), "review key", m.group(1), bool(m.group(2)))
    return None


def _role_from_receipt(role_text):
    low = str(role_text or "").lower()
    if "verif" in low or "test-design" in low:
        return "verifier/test designer"
    if "execut" in low:
        return "execution agent"
    return None


def _rows_for_dir(run_dir, name, cls):
    group, k, label, seat, key3 = cls
    receipts = sorted(f for f in os.listdir(run_dir) if f.endswith(".receipt.json"))
    producer_sha = _producer_head(run_dir) if group in (G_IMPL, G_FIX) else None
    rows = []
    for rname in receipts:
        data, why = _load_receipt(os.path.join(run_dir, rname))
        lane = rname.split("-")[0]
        engine = (data or {}).get("engine")
        provider = PROVIDERS.get(engine, "unknown") if data else "unknown"
        if data is None:
            model = UNREADABLE_MODEL
            effort = "unknown"
        else:
            resolved = data.get("resolvedModel")
            requested = data.get("requestedModel")
            if isinstance(resolved, str) and resolved.strip():
                model = resolved.strip()
            elif isinstance(requested, str) and requested.strip():
                model = "claimed: %s" % requested.strip()
            else:
                model = "unknown"
            eff = data.get("effort")
            effort = str(eff) if isinstance(eff, (str, int)) and str(eff).strip() else "unknown"
            lane = data.get("lane") if isinstance(data.get("lane"), str) and data.get("lane") else lane
        role_group, role = group, label
        override = _role_from_receipt((data or {}).get("role"))
        if override:
            role_group, role = G_OTHER, override
        if group == G_KEY:
            fam = FAMILIES.get(engine, "unknown family")
            role = "%s%s (%s; %s)" % (label, "3" if key3 or "key3" in str((data or {}).get("role")) else "", seat, fam)
            if override:
                role = "%s (%s)" % (override, seat)
        elif group in (G_IMPL, G_FIX) and not override:
            role = "%s (%s)" % (label, lane)
        if group == G_KEY:
            sha = _key_subject_sha(os.path.join(run_dir, rname.replace(".receipt.json", ".last.txt")))
        elif group in (G_IMPL, G_FIX):
            sha = producer_sha
        else:
            sha = None
        rows.append({"group": role_group, "k": k, "dir": name, "file": rname, "role": role,
                     "provider": provider, "model": model, "effort": effort, "sha": sha})
    return rows


def render(pr, head, card, runs_root, footers=()):
    head = _norm_sha(head)
    if head is None:
        raise ValueError("--head must be a full 40-hex SHA")
    rows = []
    try:
        names = sorted(e.name for e in os.scandir(runs_root) if e.is_dir())
    except OSError as exc:
        names = []
        sys.stderr.write("pr_topology: cannot scan runs root: %s\n" % exc)
    for name in names:
        cls = _classify(name, card, pr)
        if cls is None:
            continue
        try:
            rows.extend(_rows_for_dir(os.path.join(runs_root, name), name, cls))
        except OSError:
            rows.append({"group": cls[0], "k": cls[1], "dir": name, "file": "", "role": cls[2],
                         "provider": "unknown", "model": UNREADABLE_MODEL, "effort": "unknown", "sha": None})
    rows.sort(key=lambda r: (r["group"], r["k"], r["dir"], r["file"]))
    footer_rows = []
    for item in footers:
        role, _, text = str(item).partition("=")
        footer_rows.append({"role": _cell(role) or "unknown role", "provider": "unknown",
                            "model": "claimed by footer, unverified: %s" % _cell(text), "effort": "unknown",
                            "dir": "-", "sha": None})
    if not any(r["group"] == G_IMPL for r in rows) and not any("implementer" in r["role"] for r in footer_rows):
        footer_rows.insert(0, {"role": "implementer", "provider": "unknown", "model": "unknown (no receipt found)",
                               "effort": "unknown", "dir": "-", "sha": None})
    out = [BEGIN, "## Provider/model topology", "",
           "%s%s -->" % (HEAD_PREFIX, head),
           "Record-only transparency section (advisory, not a gate). Card `%s`, PR #%d, bound to head `%s`. "
           "A row whose bound SHA differs from that head is marked `%s`; `%s` means the SHA could not be derived."
           % (card, pr, head, STALE, UNBOUND), "",
           "| Role | Provider | Model (exact resolved id) | Effort | Run dir | Bound SHA |",
           "|---|---|---|---|---|---|"]
    for r in rows + footer_rows:
        if r["sha"] is None:
            bound = UNBOUND
        elif r["sha"] == head:
            bound = r["sha"]
        else:
            bound = "%s %s" % (r["sha"], STALE)
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            _cell(r["role"]), _cell(r["provider"]), _cell(r["model"]), _cell(r["effort"]), _cell(r["dir"]), bound))
    out += ["", "- %s" % CI_LINE,
            "- Independence is never inferred from a Co-Authored-By footer; a role with no receipt is "
            "`unknown` or `claimed by footer, unverified`.",
            END]
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


def check(body_bytes, head):
    head = _norm_sha(head)
    if head is None:
        return "PR_TOPOLOGY WARN unreadable bad --head"
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "PR_TOPOLOGY WARN unreadable body is not utf-8"
    state, span = _locate(body)
    if state == "none":
        return "PR_TOPOLOGY WARN missing"
    if state == "bad":
        return "PR_TOPOLOGY WARN unreadable %s" % span
    section = body[span[0]:span[1]]
    if section.count(HEAD_PREFIX) != 1:
        return "PR_TOPOLOGY WARN unreadable section head line count=%d" % section.count(HEAD_PREFIX)
    m = HEAD_RE.search(section)
    if not m:
        return "PR_TOPOLOGY WARN unreadable section head line malformed"
    recorded = m.group(1).lower()
    if recorded != head:
        return "PR_TOPOLOGY WARN stale head=%s section=%s" % (head[:8], recorded[:8])
    return "PR_TOPOLOGY ok"


class _Quiet(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _parser():
    p = _Quiet(prog="pr_topology", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Quiet)
    r = sub.add_parser("render")
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--head", required=True)
    r.add_argument("--card", required=True)
    r.add_argument("--runs-root", required=True)
    r.add_argument("--footer", action="append", default=[], help='"role=text" claimed only by a footer')
    s = sub.add_parser("splice")
    s.add_argument("--body-file", required=True)
    s.add_argument("--block-file", required=True)
    s.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--body-file", required=True)
    c.add_argument("--head", required=True)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "check":  # record-only: nothing about check may ever exit non-zero
        try:
            args = _parser().parse_args(argv)
            try:
                data = _read_capped(args.body_file, MAX_RECEIPT_BYTES * 8)
            except OSError as exc:
                line = "PR_TOPOLOGY WARN unreadable %s" % type(exc).__name__
            else:
                line = check(data, args.head)
        except Exception as exc:
            line = "PR_TOPOLOGY WARN unreadable %s" % ((str(exc).splitlines() or [type(exc).__name__])[0][:80])
        sys.stdout.write(line + "\n")
        return 0
    try:
        args = _parser().parse_args(argv)
        if args.cmd == "render":
            text = render(args.pr, args.head, args.card, args.runs_root, args.footer)
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
        sys.stderr.write("pr_topology: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
