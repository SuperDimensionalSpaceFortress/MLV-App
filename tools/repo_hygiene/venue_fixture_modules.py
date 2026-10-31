"""Scratch copies of the dual-venue PowerShell modules for the mutation fixtures (BACHELOR-THERMAL-GATE-WAIT-1 r14, CI-B1).

A mutation fixture copies one module (or the chain runner) into a temp dir with ONE line changed. Every sibling that file imports from its own directory
(`Import-Module (Join-Path $PSScriptRoot 'X.psm1')`, `$here` in the runner) must come along, or the import stops the run before the mutation under test and the
assertion fails for the wrong reason (hosted Tests run 41099141103: VenueChain.psm1 imports VenueRawJson.psm1). The sibling set is DERIVED from the source text,
transitively, never listed by hand, so the next sibling a module imports is copied without touching a fixture.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

_SIBLING_IMPORT = re.compile(r"^\s*Import-Module\s+\(Join-Path\s+\$(?:PSScriptRoot|here)\s+'([^']+\.psm1)'\)", re.MULTILINE)


def sibling_modules(source: Path) -> list[Path]:
    """The modules `source` imports from its own directory, transitively (source itself excluded), in first-seen order."""
    found: list[Path] = []
    pending = [source]
    while pending:
        current = pending.pop(0)
        for name in _SIBLING_IMPORT.findall(current.read_text(encoding="utf-8")):
            sibling = source.parent / name
            if sibling != source and sibling not in found:
                found.append(sibling)
                pending.append(sibling)
    return found


def copy_with_siblings(source: Path, dest_dir: Path, text: str | None = None) -> Path:
    """Copy `source` into `dest_dir` (its text replaced by `text` when given) with every sibling module it imports, byte for byte. Returns the copy of `source`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for sibling in sibling_modules(source):
        shutil.copy2(sibling, dest_dir / sibling.name)
    copy = dest_dir / source.name
    if text is None:
        shutil.copy2(source, copy)
    else:
        copy.write_text(text, encoding="utf-8")
    return copy
