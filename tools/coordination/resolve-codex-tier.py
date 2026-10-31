"""Resolve a Codex model TIER (e.g. 'sol'; also 'luna', 'astra' and the visual-judge 'terra') to the current highest-version slug.

LANE-MODEL-CURRENCY-1: the fleet lane table names a TIER, never a pinned model id, for
every codex-engine lane -- Codex has no floating alias the way the claude CLI's
'opus'/'sonnet'/'fable' do, so this script is the resolution step that keeps a codex
lane off a superseded model without anyone hand-editing a version string.

Reads ~/.codex/models_cache.json (overridable via --cache, for tests and for an
operator-forced cache location), finds every model slug whose form is
'gpt-<version>-<tier>' with <tier> matching EXACTLY (a whole-tier-name match: 'sol'
never matches 'solar'), and returns the one with the highest numeric <version>
('6' beats '5.6'). FAILS CLOSED (nonzero exit, one JSON object on stdout with
"ok": false and a typed "error") when the cache is missing, unreadable, malformed,
or has no match for the tier -- never guesses and never falls back to a stale slug.

MODEL-POSTURE-R16-1 (doctrine R16, "latest model of each family, at high effort"): a model is
a candidate only when its cache entry has visibility 'list' (the catalogue's own "offered to
users" flag; 'hide' entries such as gpt-reserve never qualify) AND its supported reasoning
levels include 'high' (every lane runs at high effort, so a model that cannot run at high is no
candidate). A cache whose `fetched_at` is older than MAX_CACHE_AGE_DAYS (7) is refused with the
typed error "cache-stale:..." -- a stale catalogue silently names a superseded model -- and one
with no parseable `fetched_at` is refused as "cache-malformed:...". The success object carries
`fetchedAt` so Invoke-Lane can record the catalogue's age in the receipt.

Usage: python resolve-codex-tier.py --tier sol [--cache PATH]
"""
import argparse
import datetime
import json
import os
import re
import sys

SLUG_RE = re.compile(r"^gpt-(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<tier>[a-z0-9]+)$")

REQUIRED_VISIBILITY = "list"
REQUIRED_REASONING_LEVEL = "high"
MAX_CACHE_AGE_DAYS = 7
# A fetched_at in the future by more than this is a broken clock or a forged cache, not "fresh".
MAX_CLOCK_SKEW = datetime.timedelta(days=1)


def _parse_version(version_text):
    return tuple(int(part) for part in version_text.split("."))


def default_cache_path():
    return os.path.join(os.path.expanduser("~"), ".codex", "models_cache.json")


def _parse_fetched_at(value):
    """ISO-8601 text ('2026-10-12T19:15:51.951223400Z', nanosecond fraction) -> aware datetime, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # datetime.fromisoformat accepts at most 6 fractional digits on the supported Pythons; the
    # Codex CLI writes 9. The extra digits are below the precision any age check needs.
    text = re.sub(r"(\.[0-9]{6})[0-9]+", r"\1", text)
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _supports_required_level(entry):
    levels = entry.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        return False
    for level in levels:
        effort = level.get("effort") if isinstance(level, dict) else level
        if effort == REQUIRED_REASONING_LEVEL:
            return True
    return False


def resolve_with_meta(tier, cache_path, now=None):
    """-> ({"model": slug, "fetchedAt": text, "clientVersion": text|None}, None) on success, or (None, errorToken)."""
    if not os.path.isfile(cache_path):
        return None, "cache-not-found:%s" % cache_path
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, "cache-unreadable:%s" % exc
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None, "cache-malformed:no-models-list"
    fetched_text = data.get("fetched_at")
    fetched = _parse_fetched_at(fetched_text)
    if fetched is None:
        return None, "cache-malformed:no-parseable-fetched_at:%r" % (fetched_text,)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = now - fetched
    if age < -MAX_CLOCK_SKEW:
        return None, "cache-malformed:fetched_at-in-the-future:%s" % fetched_text
    if age > datetime.timedelta(days=MAX_CACHE_AGE_DAYS):
        return None, "cache-stale:fetched_at=%s age_days=%.1f max_days=%d" % (
            fetched_text, age.total_seconds() / 86400.0, MAX_CACHE_AGE_DAYS)
    candidates = []
    for entry in models:
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not isinstance(slug, str):
            continue
        match = SLUG_RE.match(slug)
        if not match or match.group("tier") != tier:
            continue
        # MODEL-POSTURE-R16-1: only models the catalogue lists AND that can run at high effort.
        if entry.get("visibility") != REQUIRED_VISIBILITY or not _supports_required_level(entry):
            continue
        # CODEX-OVERSIZED-VERSION-TOKEN-1 (sol hardening): SLUG_RE has no length limit on the
        # version digits, but CPython's int() conversion refuses a decimal string beyond its
        # configured digit limit (ValueError, default ~4300 digits) as a DoS guard. A cache
        # entry with a version token that long is semantically malformed either way, so it is
        # skipped as a non-candidate -- the same treatment as a slug that fails SLUG_RE entirely
        # -- rather than raising past resolve()'s callers and breaking the one-JSON-object
        # failure contract every other malformed-cache path here already keeps.
        try:
            version = _parse_version(match.group("version"))
        except ValueError:
            continue
        candidates.append((version, slug))
    if not candidates:
        return None, "no-tier-match:%s" % tier
    candidates.sort(key=lambda pair: pair[0])
    # client_version names the Codex CLI that wrote this cache. The file is shared by every CLI on
    # the machine (global and pinned), each rewriting it with the catalogue ITS version was offered,
    # so the receipt records it beside the model it chose.
    client_version = data.get("client_version")
    return {"model": candidates[-1][1], "fetchedAt": fetched_text,
            "clientVersion": client_version if isinstance(client_version, str) else None}, None


def resolve(tier, cache_path, now=None):
    """-> (resolvedModel, None) on success, or (None, errorToken) on any failure."""
    meta, error = resolve_with_meta(tier, cache_path, now)
    if error:
        return None, error
    return meta["model"], None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", required=True)
    parser.add_argument("--cache", default=default_cache_path())
    args = parser.parse_args(argv)

    meta, error = resolve_with_meta(args.tier, args.cache)
    if error:
        print(json.dumps({"ok": False, "tier": args.tier, "cache": args.cache, "error": error}))
        return 1
    print(json.dumps({"ok": True, "tier": args.tier, "resolvedModel": meta["model"],
                      "fetchedAt": meta["fetchedAt"], "clientVersion": meta["clientVersion"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
