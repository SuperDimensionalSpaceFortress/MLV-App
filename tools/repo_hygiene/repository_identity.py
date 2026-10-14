"""The ONE definition of which GitHub repository slugs name this repository.

The owner transferred the repository on 2026-10-04 (the old owner's MLV-App ->
SuperDimensionalSpaceFortress/MLV-App). The immutable GitHub repository id is 1169162800 and
never changes across a rename or transfer.

Identity and transport are split here on purpose:

* TRANSPORT (any live GitHub API or gh query) always uses ``CANONICAL_REPOSITORY``. The old name
  only answers 301 (the closeout curl does not follow redirects, so it fails closed) and stops
  answering the day any account creates a repository at the old name.
* IDENTITY (recorded approvals, receipts, provenance witnesses, and a candidateAcceptance policy
  read from a pinned target commit) was written when the old slug was current. Those records are
  never rewritten, so every validator accepts BOTH names in ``REPOSITORY_SLUG_ALIASES`` as the same
  repository.

test_repo_slug_hygiene.py rejects the old slug anywhere else in tracked tooling, so this module
is the single place that spells it.
"""

from __future__ import annotations

from typing import Any

REPOSITORY_ID = 1169162800
CANONICAL_REPOSITORY = "SuperDimensionalSpaceFortress/MLV-App"
# The 2026-10 transfer: pre-transfer name first. Both name repository id 1169162800.
REPOSITORY_SLUG_ALIASES = frozenset({"layibabalola/MLV-App", CANONICAL_REPOSITORY})


def is_repository_alias(slug: Any) -> bool:
    return isinstance(slug, str) and slug in REPOSITORY_SLUG_ALIASES


def canonical_repository(slug: Any) -> str:
    """Return the transport slug for a recorded or configured alias; fail closed on any other name."""

    if not is_repository_alias(slug):
        raise ValueError("repository must be one of the known names of this repository")
    return CANONICAL_REPOSITORY


def alias_urls(template: str) -> frozenset[str]:
    """Expand a ``{repository}`` URL template once per known repository name."""

    return frozenset(template.format(repository=slug) for slug in REPOSITORY_SLUG_ALIASES)
