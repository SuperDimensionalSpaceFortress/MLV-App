"""Scratch copies of the dual-venue PowerShell modules for the mutation fixtures (BACHELOR-THERMAL-GATE-WAIT-1 r14, CI-B1).

A mutation fixture copies one module (or the chain runner) into a temp dir with ONE line changed. Every sibling that file imports from its own directory
(`Import-Module (Join-Path $PSScriptRoot 'X.psm1')`, `$here` in the runner) must come along, or the import stops the run before the mutation under test and the
assertion fails for the wrong reason (hosted Tests run 41099141103: VenueChain.psm1 imports VenueRawJson.psm1). The sibling set is DERIVED from the source text,
transitively, never listed by hand, so the next sibling a module imports is copied without touching a fixture.

VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r5 (hosted Tests run 43580040634): a module may also READ a module of a neighbouring tools/profiling folder
(`Join-Path $PSScriptRoot '..\\bachelor\\AttrCudaArtifacts.psm1'`: VenueChain.psm1 reads the claim-shape predicate's text from it). Those are derived the same way
(cross_tree_modules) and copied to the same relative place beside the scratch dir, or the staged-manifest read fails before the behaviour under test.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

_SIBLING_IMPORT = re.compile(r"^\s*Import-Module\s+\(Join-Path\s+\$(?:PSScriptRoot|here)\s+'([^']+\.psm1)'\)", re.MULTILINE)
# a module of a NEIGHBOURING folder, by a relative path that climbs one level: '..\<folder>\<name>.psm1' (imported or read as text)
_CROSS_TREE_MODULE = re.compile(r"Join-Path\s+\$(?:PSScriptRoot|here)\s+'(\.\.\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+\.psm1)'")


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


def cross_tree_modules(source: Path) -> list[str]:
    """The '..\\<folder>\\<name>.psm1' paths `source` and its siblings import or read, relative to `source`'s folder, in first-seen order."""
    found: list[str] = []
    for current in [source, *sibling_modules(source)]:
        for rel in _CROSS_TREE_MODULE.findall(current.read_text(encoding="utf-8")):
            if rel not in found:
                found.append(rel)
    return found


def copy_cross_tree_modules(source: Path, dest_dir: Path) -> list[Path]:
    """Copy every cross_tree_modules(source) file to the same path relative to `dest_dir` (a '..\\bachelor\\X.psm1' read lands in dest_dir.parent/bachelor). Returns the copies."""
    copies: list[Path] = []
    for rel in cross_tree_modules(source):
        parts = rel.split("\\")
        real = source.parent.joinpath(*parts).resolve()
        copy = dest_dir.joinpath(*parts).resolve()
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, copy)
        copies.append(copy)
    return copies


def copy_with_siblings(source: Path, dest_dir: Path, text: str | None = None) -> Path:
    """Copy `source` into `dest_dir` (its text replaced by `text` when given) with every sibling module it imports, byte for byte, and every
    neighbouring-folder module it reads (copy_cross_tree_modules: written beside `dest_dir`, so give it a dir of its own). Returns the copy of `source`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for sibling in sibling_modules(source):
        shutil.copy2(sibling, dest_dir / sibling.name)
    copy_cross_tree_modules(source, dest_dir)
    copy = dest_dir / source.name
    if text is None:
        shutil.copy2(source, copy)
    else:
        copy.write_text(text, encoding="utf-8")
    return copy
