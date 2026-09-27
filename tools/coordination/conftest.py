"""Hermetic doctrine brief for every pytest run under tools/coordination.

Composition of an implementer/editing prompt (compose-lane-prompt-core.ps1) is
fail-closed on the doctrine brief: with no fixture it fetches the live bus via
``gh api`` and REFUSES when that fails. That is correct in production and wrong in
a test, where it made every editing-dispatch and fields-card compose test depend
on network plus a ``gh`` token (hosted CI has no GH_TOKEN, so they all refused with
``doctrine-brief-failed`` before reaching the check they exist to prove).

Each test therefore runs with MLV_DOCTRINE_FIXTURE_ROOT pointing at the checked-in
offline tree beside this file. Production is unchanged: nothing outside pytest sets
the variable, and get_doctrine_brief.py REFUSES a fixture unless PYTEST_CURRENT_TEST
is set and the root lies inside tools/coordination/fixtures/ (a stray production
MLV_DOCTRINE_FIXTURE_ROOT therefore fails closed; see NonLiveDoctrineRefusedOutsidePytestTests).
The fail-closed path is still exercised: a test that needs the
no-fixture behaviour removes the variable, and a fixture tree missing a required
file still refuses (both in test_get_doctrine_brief.py).
"""

from pathlib import Path

import pytest

DOCTRINE_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "doctrine-offline"


@pytest.fixture(autouse=True)
def _hermetic_doctrine_brief(monkeypatch):
    monkeypatch.setenv("MLV_DOCTRINE_FIXTURE_ROOT", str(DOCTRINE_FIXTURE_ROOT))
