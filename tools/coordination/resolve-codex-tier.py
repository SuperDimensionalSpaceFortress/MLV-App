"""Resolve a Codex model TIER (e.g. 'sol') to the current highest-version slug.

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

Usage: python resolve-codex-tier.py --tier sol [--cache PATH]
"""
import argparse
import json
import os
import re
import sys

SLUG_RE = re.compile(r"^gpt-(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<tier>[a-z0-9]+)$")


def _parse_version(version_text):
    return tuple(int(part) for part in version_text.split("."))


def default_cache_path():
    return os.path.join(os.path.expanduser("~"), ".codex", "models_cache.json")


def resolve(tier, cache_path):
    """-> (resolvedModel, None) on success, or (None, errorToken) on any failure."""
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
    candidates = []
    for entry in models:
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not isinstance(slug, str):
            continue
        match = SLUG_RE.match(slug)
        if not match or match.group("tier") != tier:
            continue
        candidates.append((_parse_version(match.group("version")), slug))
    if not candidates:
        return None, "no-tier-match:%s" % tier
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1], None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", required=True)
    parser.add_argument("--cache", default=default_cache_path())
    args = parser.parse_args(argv)

    resolved_model, error = resolve(args.tier, args.cache)
    if error:
        print(json.dumps({"ok": False, "tier": args.tier, "cache": args.cache, "error": error}))
        return 1
    print(json.dumps({"ok": True, "tier": args.tier, "resolvedModel": resolved_model}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
