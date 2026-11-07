"""Regression tests for the receipt-gated Phase 0.4c-ii bridge demotion actor.

All receipt and repository mutations use temporary fixtures. The bridge-job literal is the
LF-normalized body from fork/master fd8cc42145a1a5a51468defb82e518a73717487d,
whose SHA-256 is pinned by the actor.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "demote-factory-bridge.ps1"

GUARDRAIL_MOVE_NAME = "0.4c-guardrail-move.json"
REQUIRED_CHECKS_NAME = "0.4b-required-checks.json"

VALID_GUARDRAIL_MOVE = {"conclusion": "success"}
VALID_REQUIRED_CHECKS = {
    "headSha": "a" * 40,
    "preContexts": ["Factory Bridge Regressions"],
    "postContexts": [
        "Repo Hygiene Python (windows-latest)",
        "Repo Hygiene Python (ubuntu-latest)",
        "Batch Compile",
        "Windows GUI Pilot",
        "Windows Product Oracles",
    ],
    "snapshotRowSha256": "b" * 64,
}


JOB_BODY_LINES = '  # Factory-control regressions must stay independent from product oracles.\n  # A bridge failure is blocking, but it must never suppress product evidence.\n  factory-bridge-regressions:\n    name: Factory Bridge Regressions\n    runs-on: windows-latest\n    timeout-minutes: 45\n\n    defaults:\n      run:\n        shell: pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ". \'{0}\'"\n\n    steps:\n      - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5\n        with:\n          persist-credentials: false\n          # tests/coordination/test_process_hygiene_contract.py verifies the sealed A/B\n          # receipts against the historical commit each one names. At the default depth of\n          # 1 that commit is absent and the check cannot run. The two jobs above already\n          # check out at full depth.\n          fetch-depth: 0\n\n      - name: Set up Python\n        uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6\n        with:\n          python-version-file: ".python-version"\n\n      - name: Install factory bridge dependencies\n        run: |\n          python -m pip install --disable-pip-version-check --no-input --only-binary=:all: --require-hashes -r .github/requirements/pip.txt\n          python -m pip install --disable-pip-version-check --no-input --only-binary=:all: --require-hashes -r tools\\agent-bridge\\requirements-test.txt\n          python -m pip check\n\n      - name: Verify compatible MCP runtime\n        run: |\n          python -c "import importlib.metadata as m; version=m.version(\'mcp\'); major=int(version.split(\'.\')[0]); assert major == 1, f\'expected MCP 1.x, got {version}\'; from mcp.server.fastmcp import FastMCP; print(f\'MCP runtime verified: {version}\')"\n\n      - name: Run agent bridge PowerShell launcher regressions\n        run: |\n          python -m unittest `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_mcp_server_watchdog_throttles_wrapper_identity_checks `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_codex_hooks_assert_bridge_cli_contract `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_wake_codex_input_size_smoke_runs_under_powershell `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_configure_watcher_targeted_sendkeys_provider_writes_delivery_priority_helper_template `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_bootstrap_watcher_enumeration_prefers_pwsh_for_cim `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_windows_toast_uses_expiry_and_tray_cap_settings `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_windows_toast_tag_sanitizes_ps_line_breaks `\n            tools.agent-bridge.test_agent_bridge.AgentBridgeTests.test_windows_balloon_fallback_uses_encoded_command `\n            tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_queries_prefer_pwsh_for_cim `\n            tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_uses_pwsh_alias_when_exe_probe_is_absent `\n            tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_windows_process_table_queries_fall_back_to_windows_powershell `\n            tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_trampoline_host_env_queries_prefer_pwsh_for_cim `\n            tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_server_wrapper_process_entry_prefers_pwsh_for_cim `\n            tools.agent-bridge.test_server_wrapper_phase2.ServerWrapperPhase2Tests.test_supervisor_throttles_host_identity_revalidation `\n            -v\n\n      - name: Run full agent-bridge test suite\n        run: |\n          # Directory target: also collects tools/agent-bridge/test_phase0_contract.py\n          # and the full tools/agent-bridge/test_server_wrapper_phase2.py suite, neither\n          # of which the prior single-file invocation covered.\n          python -m pytest `\n            tools\\agent-bridge `\n            -q\n\n      - name: Run profiling test suite\n        run: |\n          python -m pytest `\n            tests\\profiling `\n            -q\n'

SOURCE_WITH_JOB = (
    "name: Tests\n\non:\n  workflow_dispatch:\n\njobs:\n"
    "  other-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n\n"
    + JOB_BODY_LINES
    + "\n  windows-product-oracles:\n    runs-on: windows-latest\n    steps:\n      - run: echo oracle\n"
)

SOURCE_WITHOUT_JOB = (
    "name: Tests\n\non:\n  workflow_dispatch:\n\njobs:\n"
    "  other-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n\n"
    "  windows-product-oracles:\n    runs-on: windows-latest\n    steps:\n      - run: echo oracle\n"
)

SOURCE_WITH_DUPLICATE_JOB = (
    "name: Tests\n\non:\n  workflow_dispatch:\n\njobs:\n"
    + JOB_BODY_LINES
    + "\n"
    + JOB_BODY_LINES
)


def write_receipt(receipts_dir, name, payload):
    """Writes `payload` as JSON to receipts_dir/name. `payload=None` writes nothing (absent).
    A string `payload` is written verbatim, so a caller can inject malformed JSON."""
    if payload is None:
        return
    path = receipts_dir / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


def run_gate(receipts_dir, board_root=None):
    args = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(SCRIPT),
        "-ReceiptsDir",
        str(receipts_dir),
    ]
    if board_root is not None:
        args += ["-BoardRoot", str(board_root)]
    result = subprocess.run(args, text=True, capture_output=True)
    return result


def demotion_args(receipts_dir, repo_root, apply, board_root):
    """The argv `run_demotion` hands to the actor. `board_root` must already be resolved."""
    args = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(SCRIPT),
        "-ReceiptsDir",
        str(receipts_dir),
        "-RepoRoot",
        str(repo_root),
    ]
    if board_root is not None:
        args += ["-BoardRoot", str(board_root)]
    if apply:
        args += ["-Apply"]
    return args


def run_demotion(receipts_dir, repo_root, apply=False, board_root=None):
    """Runs the actor. An apply-mode call without an explicit `board_root` gets a fresh,
    EXISTING temporary one (distinct from `repo_root`): the actor's own default is the owner
    path (`C:\\!Layi Wkspc\\MLV-App`) and -Apply runs `Resolve-Path` on it, so without this the
    test passes only on a host where that path happens to exist and fails on a hosted runner
    (CI-SHARD8-GUARDRAILS-COST-2-R2-RUN-BODY-SPLIT-1 r1: 10 failed, 34 passed). Preview calls
    never resolve -BoardRoot and keep their old argv. A caller-supplied `board_root` is passed
    through untouched, so the refusal and alias controls exercise exactly what they did."""
    if apply and board_root is None:
        with tempfile.TemporaryDirectory(prefix="demote-board-root-") as scratch_board:
            return subprocess.run(
                demotion_args(receipts_dir, repo_root, apply, scratch_board),
                text=True,
                capture_output=True,
            )
    return subprocess.run(
        demotion_args(receipts_dir, repo_root, apply, board_root), text=True, capture_output=True
    )


# ---------------------------------------------------------------------------------------
# Hermeticity guard. The actor's default -BoardRoot is the owner path; an apply-mode call
# that reaches the actor without an explicit, existing -BoardRoot distinct from -RepoRoot
# silently depends on that path existing on the host. `apply_call_violation` is the rule;
# the autouse fixture below applies it to every real actor invocation in this module.
# ---------------------------------------------------------------------------------------

# Tests that deliberately omit -BoardRoot or make it equal -RepoRoot, because the refusal is
# the thing under test. They are named here, not exempted by pattern.
DELIBERATE_BOARD_ROOT_CONTROLS = {
    "test_explicit_repo_root_required_for_apply",  # refuses before -BoardRoot is read
    "test_apply_refuses_repo_root_equal_to_board_root",  # -RepoRoot == -BoardRoot
    "test_apply_refuses_dotdot_alias_of_board_root",  # -RepoRoot resolves to -BoardRoot
}


ACTOR_PARAMETERS = ("BoardRoot", "ReceiptsDir", "RepoRoot", "Apply")


def _bound_parameters(args):
    """Maps the actor parameters PowerShell would bind from `args` to their value (None for a
    switch given bare). PowerShell matches parameter names case-insensitively, by unique prefix
    among the actor's parameters, and accepts `-Name:value`; an ambiguous prefix binds nothing."""
    bound = {}
    for index, token in enumerate(args):
        if not token.startswith("-") or len(token) < 2:
            continue
        name, colon, inline = token[1:].partition(":")
        matches = [p for p in ACTOR_PARAMETERS if name and p.lower().startswith(name.lower())]
        if len(matches) != 1:
            continue
        if colon:
            bound[matches[0]] = inline
        elif matches[0] == "Apply":
            bound["Apply"] = None
        elif index + 1 < len(args):
            bound[matches[0]] = args[index + 1]
    return bound


def apply_call_violation(args):
    """Returns a reason string if `args` is an apply-mode actor call that is not hermetic,
    else None. Calls that are not apply-mode actor calls are never violations."""
    bound = _bound_parameters(args)
    apply_on = "Apply" in bound and (bound["Apply"] or "").lower() not in ("$false", "false", "0")
    if str(SCRIPT) not in args or not apply_on:
        return None
    board_root = bound.get("BoardRoot")
    repo_root = bound.get("RepoRoot")
    if board_root is None:
        return "apply-mode call has no -BoardRoot (the actor would fall back to the owner path)"
    if not os.path.isdir(board_root):
        return "apply-mode call -BoardRoot does not exist: %s" % board_root
    if repo_root is not None and os.path.normcase(os.path.abspath(board_root)) == os.path.normcase(
        os.path.abspath(repo_root)
    ):
        return "apply-mode call -BoardRoot equals -RepoRoot: %s" % board_root
    return None


@pytest.fixture(autouse=True)
def _apply_calls_are_hermetic(request, monkeypatch):
    real_run = subprocess.run

    def checked_run(args, *a, **kw):
        if request.node.originalname not in DELIBERATE_BOARD_ROOT_CONTROLS:
            reason = apply_call_violation([str(x) for x in args])
            assert reason is None, "non-hermetic apply call in %s: %s" % (request.node.name, reason)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", checked_run)


def test_apply_calls_carry_an_isolated_existing_board_root(tmp_path, monkeypatch):
    """Guard for the helper itself: run_demotion(apply=True) with no board_root must hand the
    actor an existing -BoardRoot that is not -RepoRoot, and `apply_call_violation` must reject
    each way of getting that wrong. The recorder replaces subprocess.run, so no actor runs."""
    seen = []

    def record(args, *a, **kw):
        # Evaluated while the call is in flight: the temporary -BoardRoot is removed after.
        args = [str(x) for x in args]
        seen.append((args, apply_call_violation(args)))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", record)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_demotion(tmp_path, repo, apply=True)
    run_demotion(tmp_path, repo, apply=False)
    (apply_args, apply_violation), (preview_args, preview_violation) = seen
    assert "-BoardRoot" in apply_args
    assert apply_violation is None, apply_violation
    assert preview_violation is None
    assert "-BoardRoot" not in preview_args  # preview argv is unchanged

    def call(board, apply=True):
        args = [str(SCRIPT), "-RepoRoot", str(repo)]
        if board is not None:
            args += ["-BoardRoot", str(board)]
        return args + (["-Apply"] if apply else [])

    assert apply_call_violation(call(tmp_path)) is None
    assert apply_call_violation(call(None)) is not None  # no -BoardRoot
    assert apply_call_violation(call(tmp_path / "absent")) is not None  # does not exist
    assert apply_call_violation(call(repo)) is not None  # equals -RepoRoot
    assert apply_call_violation(call(None, apply=False)) is None  # preview is not apply-mode

    # PowerShell binds parameter names case-insensitively, by unique prefix, and as -Name:value
    # (adversary pass on 8e131986, A1): each spelling below enables -Apply, so each must be seen.
    for apply_spelling in ("-apply", "-APPLY", "-Ap", "-Apply:$true"):
        bare = [str(SCRIPT), "-RepoRoot", str(repo), apply_spelling]
        assert apply_call_violation(bare) is not None, apply_spelling
    assert apply_call_violation([str(SCRIPT), "-RepoRoot", str(repo), "-Apply:$false"]) is None
    assert apply_call_violation([str(SCRIPT), "-repo", str(repo), "-board", str(repo), "-apply"]) is not None  # equal
    assert apply_call_violation([str(SCRIPT), "-repo", str(repo), "-boardroot:" + str(tmp_path), "-apply"]) is None
    # -Re is ambiguous between -ReceiptsDir and -RepoRoot, so it names neither and PowerShell
    # would refuse the call before running anything.
    assert apply_call_violation([str(SCRIPT), "-Re", str(repo), "-BoardRoot", str(tmp_path), "-Apply"]) is None


def assert_refused(result, expected_substring):
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        "expected a non-zero (refused) exit, got %r; stdout=%r stderr=%r"
        % (result.returncode, result.stdout, result.stderr)
    )
    assert expected_substring in combined, (
        "expected %r in combined output; stdout=%r stderr=%r"
        % (expected_substring, result.stdout, result.stderr)
    )


@pytest.fixture
def receipts_dir(tmp_path):
    d = tmp_path / "receipts"
    d.mkdir()
    return d


def test_board_root_is_genuinely_wired_to_the_default_receipts_path(tmp_path):
    """sol round 2, PR #83, MAJOR (restated): -BoardRoot's ONLY effect in the script is
    deriving the default -ReceiptsDir (Join-Path $BoardRoot
    '.claude-state\\coordination\\dual-lane\\receipts') when -ReceiptsDir is not passed.
    Every other test in this file always passes -ReceiptsDir explicitly, which bypasses that
    code path entirely -- so no test proved -BoardRoot is wired to anything real. This test
    is the one that does: it passes ONLY -BoardRoot (no -ReceiptsDir at all), places a valid
    receipt pair at the path -BoardRoot's own derivation formula predicts, and asserts the
    gate finds and validates them -- succeeding all the way through the job-demotion preview
    (since -RepoRoot also defaults to -BoardRoot), which is only reachable if -BoardRoot
    genuinely routed to the right receipts directory. An isolated `.github/workflows/
    tests.yml` fixture is planted under board_root so the preview has a real source job to
    find (the former stub-success revision of this test needed no such fixture, because it
    only asserted the receipt-validation message; the job-demotion preview added by
    CI-GUARDRAIL-MOVE-2 now runs unconditionally after that message and would otherwise
    refuse on a missing source workflow).
    Measured to actually depend on -BoardRoot: renaming the script's -BoardRoot parameter
    (so it is silently unbound, per PowerShell's -File invocation behavior measured
    directly) makes this test fail, because the derived receipts directory would then be
    wrong and the receipts this test wrote would not be found."""
    board_root = tmp_path / "board"
    receipts_dir = board_root / ".claude-state" / "coordination" / "dual-lane" / "receipts"
    receipts_dir.mkdir(parents=True)
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    workflows_dir = board_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "tests.yml").write_text(SOURCE_WITH_JOB, encoding="utf-8")

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-BoardRoot", str(board_root)],
        text=True, capture_output=True,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        "expected the gate to find receipts via -BoardRoot's own derivation and succeed;"
        " got exit=%r stdout=%r stderr=%r" % (result.returncode, result.stdout, result.stderr)
    )
    assert "both receipts validate" in combined, (
        "expected the happy-path validation message, reachable only if -BoardRoot routed"
        " correctly; stdout=%r stderr=%r" % (result.stdout, result.stderr)
    )
    assert "demote-factory-bridge preview" in combined, (
        "expected the preview (default, no-write) path to run using -RepoRoot's own"
        " default-to-BoardRoot; stdout=%r stderr=%r" % (result.stdout, result.stderr)
    )
    # Preview must never write anything.
    assert not (workflows_dir / "factory-bridge.yml").exists()


def test_refuses_when_both_receipts_are_absent(receipts_dir):
    result = run_gate(receipts_dir)
    assert_refused(result, GUARDRAIL_MOVE_NAME)
    assert_refused(result, "is absent")


def test_refuses_when_only_guardrail_move_receipt_is_absent(receipts_dir):
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    result = run_gate(receipts_dir)
    assert_refused(result, GUARDRAIL_MOVE_NAME)
    assert_refused(result, "is absent")


def test_refuses_when_guardrail_move_receipt_is_malformed_json(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, "{not valid json")
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    result = run_gate(receipts_dir)
    assert_refused(result, GUARDRAIL_MOVE_NAME)
    assert_refused(result, "does not parse as JSON")


def test_refuses_when_guardrail_move_receipt_is_not_a_json_object(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, ["not", "an", "object"])
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    result = run_gate(receipts_dir)
    assert_refused(result, GUARDRAIL_MOVE_NAME)
    assert_refused(result, "is not a JSON object")


def test_refuses_when_guardrail_move_receipt_conclusion_is_missing(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, {"someOtherKey": "value"})
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    result = run_gate(receipts_dir)
    assert_refused(result, GUARDRAIL_MOVE_NAME)
    assert_refused(result, "conclusion=success")


def test_refuses_when_guardrail_move_receipt_conclusion_is_not_success(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, {"conclusion": "failure"})
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    result = run_gate(receipts_dir)
    assert_refused(result, GUARDRAIL_MOVE_NAME)
    assert_refused(result, "conclusion=success")


def test_refuses_when_only_required_checks_receipt_is_absent(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "is absent")


def test_refuses_when_required_checks_receipt_is_malformed_json(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, "{also not valid")
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "does not parse as JSON")


def test_refuses_when_required_checks_receipt_is_not_a_json_object(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, "42")
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "is not a JSON object")


def test_refuses_when_required_checks_receipt_is_missing_post_contexts(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(
        receipts_dir,
        REQUIRED_CHECKS_NAME,
        {"headSha": "a" * 40, "preContexts": [], "snapshotRowSha256": "b" * 64},
    )
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "missing postContexts")


def test_refuses_when_required_checks_receipt_still_lists_the_bridge_job(receipts_dir):
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    still_required = dict(VALID_REQUIRED_CHECKS)
    still_required["postContexts"] = list(VALID_REQUIRED_CHECKS["postContexts"]) + [
        "Factory Bridge Regressions"
    ]
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, still_required)
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "still lists 'Factory Bridge Regressions'")


def test_refuses_when_required_checks_receipt_post_contexts_is_a_single_bridge_string(
    receipts_dir,
):
    """A single-element JSON array can deserialize to a PowerShell scalar rather than an
    array in some ConvertFrom-Json shapes; this pins that the shape/containment check still
    catches the bridge job when postContexts holds exactly one (blocked) entry."""
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(
        receipts_dir,
        REQUIRED_CHECKS_NAME,
        {
            "headSha": "a" * 40,
            "preContexts": [],
            "postContexts": ["Factory Bridge Regressions"],
            "snapshotRowSha256": "b" * 64,
        },
    )
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "still lists 'Factory Bridge Regressions'")


def test_refuses_when_required_checks_post_contexts_is_a_json_object(receipts_dir):
    """sol round 1, PR #83: an object-valued postContexts was being silently @()-coerced
    into a one-element array and accepted. Must now be refused as a malformed shape."""
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(
        receipts_dir,
        REQUIRED_CHECKS_NAME,
        '{"headSha": "%s", "preContexts": [], '
        '"postContexts": {"name": "Factory Bridge Regressions"}, '
        '"snapshotRowSha256": "%s"}' % ("a" * 40, "b" * 64),
    )
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "postContexts is not a JSON array")


def test_refuses_when_required_checks_post_contexts_is_an_empty_array(receipts_dir):
    """sol round 1, PR #83: an empty postContexts trivially 'does not contain' the bridge
    context and was silently accepted. An empty required-context list is itself malformed
    for a repo with any required checks and must be refused."""
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(
        receipts_dir,
        REQUIRED_CHECKS_NAME,
        {
            "headSha": "a" * 40,
            "preContexts": [],
            "postContexts": [],
            "snapshotRowSha256": "b" * 64,
        },
    )
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "postContexts is an empty array")


def test_refuses_when_required_checks_post_contexts_has_a_non_string_element(receipts_dir):
    """sol round 2, PR #83, MINOR: no test exercised the 'contains a non-string element'
    refusal branch -- the implementation refused a direct synthetic repro, but deleting that
    production branch would not have failed any committed test."""
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(
        receipts_dir,
        REQUIRED_CHECKS_NAME,
        '{"headSha": "%s", "preContexts": [], '
        '"postContexts": ["Windows GUI Pilot", 42], '
        '"snapshotRowSha256": "%s"}' % ("a" * 40, "b" * 64),
    )
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "non-string element")


def test_refuses_when_required_checks_post_contexts_has_a_whitespace_near_match(receipts_dir):
    """sol round 1, PR #83: a whitespace-padded near-match to the blocked context name
    ('Factory Bridge Regressions ') was not an exact match and was silently accepted. A
    receipt whose elements need trimming to match is itself a data-quality problem and must
    be refused, not auto-corrected."""
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(
        receipts_dir,
        REQUIRED_CHECKS_NAME,
        {
            "headSha": "a" * 40,
            "preContexts": [],
            "postContexts": ["Factory Bridge Regressions "],
            "snapshotRowSha256": "b" * 64,
        },
    )
    result = run_gate(receipts_dir)
    assert_refused(result, REQUIRED_CHECKS_NAME)
    assert_refused(result, "whitespace near-match")


# ---------------------------------------------------------------------------------------
# sol round 1, PR #83, MAJOR 2: the prior single test here supplied no receipts, created no
# sentinel file, and left -BoardRoot at its real-repository default, so it only exercised
# the FIRST refusal path and could not have detected a write bug anywhere else. This
# version sandboxes -BoardRoot at tmp_path, creates a real sentinel workflow file there
# BEFORE running the gate, and re-checks the sentinel's content after EACH of several
# distinct refusal paths -- not just the first one checked.
# ---------------------------------------------------------------------------------------

_SENTINEL_WORKFLOW_CONTENT = "name: Tests\n# sentinel content the gate must never touch\n"


@pytest.fixture
def sandboxed_board_root(tmp_path):
    board_root = tmp_path / "board"
    receipts_dir = board_root / ".claude-state" / "coordination" / "dual-lane" / "receipts"
    receipts_dir.mkdir(parents=True)
    workflows_dir = board_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    sentinel_path = workflows_dir / "tests.yml"
    sentinel_path.write_text(_SENTINEL_WORKFLOW_CONTENT, encoding="utf-8")
    return board_root, receipts_dir, sentinel_path


REFUSAL_CASES = [
    ("both_receipts_absent", None, None, GUARDRAIL_MOVE_NAME + " is absent"),
    ("only_guardrail_move_present", VALID_GUARDRAIL_MOVE, None, REQUIRED_CHECKS_NAME + " is absent"),
    ("only_required_checks_present", None, VALID_REQUIRED_CHECKS, GUARDRAIL_MOVE_NAME + " is absent"),
    (
        "guardrail_move_conclusion_not_success",
        {"conclusion": "failure"},
        VALID_REQUIRED_CHECKS,
        "conclusion=success",
    ),
    (
        "required_checks_still_lists_bridge",
        VALID_GUARDRAIL_MOVE,
        {**VALID_REQUIRED_CHECKS, "postContexts": VALID_REQUIRED_CHECKS["postContexts"] + [
            "Factory Bridge Regressions"
        ]},
        "still lists \'Factory Bridge Regressions\'",
    ),
]


@pytest.mark.parametrize(
    "case_name,guardrail_payload,required_payload,expected_substring", REFUSAL_CASES
)
def test_does_not_touch_any_workflow_file_on_refusal(
    sandboxed_board_root, case_name, guardrail_payload, required_payload, expected_substring
):
    # sol round 2, PR #83, MAJOR: the previous version of this test asserted only a
    # nonzero exit code, which cannot distinguish a genuine business-logic refusal from a
    # PowerShell PARAMETER-BINDING failure -- if -BoardRoot were removed from the script or
    # otherwise broken, invoking it with -BoardRoot still set would throw a binding error,
    # which is ALSO a nonzero exit, and this test would have kept passing without -BoardRoot
    # ever being exercised at all. Asserting the SPECIFIC refusal-reason substring (the same
    # discipline every other test in this file already uses via assert_refused) closes that
    # gap: a binding error's message never contains these business-logic strings.
    board_root, receipts_dir, sentinel_path = sandboxed_board_root
    write_receipt(receipts_dir, GUARDRAIL_MOVE_NAME, guardrail_payload)
    write_receipt(receipts_dir, REQUIRED_CHECKS_NAME, required_payload)

    result = run_gate(receipts_dir, board_root=board_root)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        "case %r: expected refusal, got exit=%r stdout=%r stderr=%r"
        % (case_name, result.returncode, result.stdout, result.stderr)
    )
    assert expected_substring in combined, (
        "case %r: expected %r in combined output (a generic nonzero exit is not enough --"
        " it must be THIS specific refusal, not a parameter-binding failure or some other"
        " error); stdout=%r stderr=%r"
        % (case_name, expected_substring, result.stdout, result.stderr)
    )
    actual_content = sentinel_path.read_text(encoding="utf-8")
    assert actual_content == _SENTINEL_WORKFLOW_CONTENT, (
        "case %r: sentinel workflow file was modified on refusal (expected %r, got %r)"
        % (case_name, _SENTINEL_WORKFLOW_CONTENT, actual_content)
    )
    # No file besides the sentinel and whichever receipts this case wrote may exist.
    all_files = sorted(p.relative_to(board_root).as_posix() for p in board_root.rglob("*") if p.is_file())
    expected_files = {".github/workflows/tests.yml"}
    if guardrail_payload is not None:
        expected_files.add(
            ".claude-state/coordination/dual-lane/receipts/" + GUARDRAIL_MOVE_NAME
        )
    if required_payload is not None:
        expected_files.add(
            ".claude-state/coordination/dual-lane/receipts/" + REQUIRED_CHECKS_NAME
        )
    assert set(all_files) == expected_files, (
        "case %r: unexpected files after refusal: found %r, expected %r"
        % (case_name, all_files, sorted(expected_files))
    )


# ---------------------------------------------------------------------------------------
# CI-GUARDRAIL-MOVE-2 (0.4c-ii): the deterministic local job-extraction/demotion logic.
# Every test below writes both valid gate receipts (they are not this section's concern --
# proven above) and exercises -RepoRoot in an isolated fixture directory, never the real
# board.
# ---------------------------------------------------------------------------------------


@pytest.fixture
def valid_receipts_dir(tmp_path):
    d = tmp_path / "receipts"
    d.mkdir()
    write_receipt(d, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    write_receipt(d, REQUIRED_CHECKS_NAME, VALID_REQUIRED_CHECKS)
    return d


def make_repo(tmp_path, name, source_text):
    repo_root = tmp_path / name
    workflows_dir = repo_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "tests.yml").write_text(source_text, encoding="utf-8")
    return repo_root, workflows_dir


def test_valid_preview_makes_no_writes(tmp_path, valid_receipts_dir):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITH_JOB)
    result = run_demotion(valid_receipts_dir, repo_root, apply=False)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "demote-factory-bridge preview" in combined
    assert not (workflows_dir / "factory-bridge.yml").exists()
    assert (workflows_dir / "tests.yml").read_text(encoding="utf-8") == SOURCE_WITH_JOB


def test_valid_apply_preserves_job_body_exactly(tmp_path, valid_receipts_dir):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITH_JOB)
    result = run_demotion(valid_receipts_dir, repo_root, apply=True)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    dest_text = (workflows_dir / "factory-bridge.yml").read_text(encoding="utf-8")
    assert JOB_BODY_LINES.rstrip("\n") + "\n" in dest_text
    assert dest_text.startswith("name: Factory Bridge\n")
    assert "concurrency:" in dest_text
    source_text = (workflows_dir / "tests.yml").read_text(encoding="utf-8")
    assert "factory-bridge-regressions" not in source_text
    assert "other-job" in source_text
    assert "windows-product-oracles" in source_text


def test_apply_is_byte_identical_on_repeat(tmp_path, valid_receipts_dir):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITH_JOB)
    first = run_demotion(valid_receipts_dir, repo_root, apply=True)
    assert first.returncode == 0, first.stdout + first.stderr
    first_bytes = (workflows_dir / "factory-bridge.yml").read_bytes()

    second = run_demotion(valid_receipts_dir, repo_root, apply=True)
    combined = second.stdout + second.stderr
    assert second.returncode == 0, combined
    assert "already-migrated" in combined
    second_bytes = (workflows_dir / "factory-bridge.yml").read_bytes()
    assert first_bytes == second_bytes


def test_explicit_repo_root_required_for_apply(tmp_path, valid_receipts_dir):
    args = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
        "-ReceiptsDir", str(valid_receipts_dir), "-Apply",
    ]
    result = subprocess.run(args, text=True, capture_output=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "requires an explicit -RepoRoot" in combined


def test_apply_refuses_repo_root_equal_to_board_root(tmp_path, valid_receipts_dir):
    board_root = tmp_path / "same"
    workflows_dir = board_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "tests.yml").write_text(SOURCE_WITH_JOB, encoding="utf-8")
    args = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
        "-ReceiptsDir", str(valid_receipts_dir),
        "-BoardRoot", str(board_root),
        "-RepoRoot", str(board_root),
        "-Apply",
    ]
    result = subprocess.run(args, text=True, capture_output=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "refuses -RepoRoot equal to -BoardRoot" in combined
    assert not (workflows_dir / "factory-bridge.yml").exists()


def test_refuses_duplicate_jobs_in_source(tmp_path, valid_receipts_dir):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITH_DUPLICATE_JOB)
    result = run_demotion(valid_receipts_dir, repo_root, apply=False)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "duplicate job" in combined
    assert not (workflows_dir / "factory-bridge.yml").exists()


def test_refuses_conflicting_destination(tmp_path, valid_receipts_dir):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITH_JOB)
    (workflows_dir / "factory-bridge.yml").write_text(
        "name: Something Else Entirely\njobs:\n  unrelated-job:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    result = run_demotion(valid_receipts_dir, repo_root, apply=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "conflicting" in combined.lower() or "neither the computed migration" in combined
    source_text = (workflows_dir / "tests.yml").read_text(encoding="utf-8")
    assert "factory-bridge-regressions" in source_text


def test_refuses_when_no_source_job_and_no_valid_destination(tmp_path, valid_receipts_dir):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITHOUT_JOB)
    result = run_demotion(valid_receipts_dir, repo_root, apply=False)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "no '" in combined and "job and destination is not a valid" in combined


def test_refuses_stale_state_when_destination_already_migrated_but_source_still_has_job(
    tmp_path, valid_receipts_dir
):
    # First produce a genuinely completed migration in one fixture repo.
    completed_repo, completed_workflows = make_repo(tmp_path, "completed", SOURCE_WITH_JOB)
    first = run_demotion(valid_receipts_dir, completed_repo, apply=True)
    assert first.returncode == 0, first.stdout + first.stderr
    completed_dest_text = (completed_workflows / "factory-bridge.yml").read_text(encoding="utf-8")

    # Now build a stale fixture: source still has the job, AND destination already looks
    # like the finished migration.
    stale_repo, stale_workflows = make_repo(tmp_path, "stale", SOURCE_WITH_JOB)
    (stale_workflows / "factory-bridge.yml").write_text(completed_dest_text, encoding="utf-8")

    result = run_demotion(valid_receipts_dir, stale_repo, apply=False)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "stale state" in combined
    assert (stale_workflows / "tests.yml").read_text(encoding="utf-8") == SOURCE_WITH_JOB
    assert (stale_workflows / "factory-bridge.yml").read_text(encoding="utf-8") == completed_dest_text


def test_partial_io_failure_names_actual_changed_files_and_leaves_source_untouched(
    tmp_path, valid_receipts_dir
):
    repo_root, workflows_dir = make_repo(tmp_path, "repo", SOURCE_WITH_JOB)
    # Occupy the destination PATH with a directory so the destination-first write fails
    # before the source is ever touched.
    (workflows_dir / "factory-bridge.yml").mkdir()

    result = run_demotion(valid_receipts_dir, repo_root, apply=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "factory-bridge.yml" in combined
    assert "existing non-file" in combined or "not a file" in combined
    # The destination path is still the same directory (untouched), and the source still
    # has the job -- destination-first order means the failure happened before the source
    # rewrite could run.
    assert (workflows_dir / "factory-bridge.yml").is_dir()
    source_text = (workflows_dir / "tests.yml").read_text(encoding="utf-8")
    assert "factory-bridge-regressions" in source_text


@pytest.mark.parametrize("contexts", [
    ["Repo Hygiene Python (windows-latest)", "Repo Hygiene Python (ubuntu-latest)", "Windows GUI Pilot", "Windows Product Oracles"],
    ["Repo Hygiene Python (windows-latest)", "Repo Hygiene Python (ubuntu-latest)", "Batch Compile", "Windows GUI Pilot", "Windows Product Oracles", "Extra"],
    ["Repo Hygiene Python (windows-latest)", "Repo Hygiene Python (ubuntu-latest)", "Batch Compile", "Windows GUI Pilot", "Windows GUI Pilot"],
    ["Batch Compile", "Repo Hygiene Python (windows-latest)", "Repo Hygiene Python (ubuntu-latest)", "Windows GUI Pilot", "Windows Product Oracles"],
])
def test_refuses_noncanonical_required_context_tuple(tmp_path, contexts):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(receipts, GUARDRAIL_MOVE_NAME, VALID_GUARDRAIL_MOVE)
    payload = dict(VALID_REQUIRED_CHECKS)
    payload["postContexts"] = contexts
    write_receipt(receipts, REQUIRED_CHECKS_NAME, payload)
    repo, workflows = make_repo(tmp_path, "repo-canonical", SOURCE_WITH_JOB)
    before = (workflows / "tests.yml").read_bytes()
    result = run_demotion(receipts, repo, apply=True)
    assert_refused(result, "canonical")
    assert (workflows / "tests.yml").read_bytes() == before
    assert not (workflows / "factory-bridge.yml").exists()


def test_apply_refuses_dotdot_alias_of_board_root(tmp_path, valid_receipts_dir):
    board = tmp_path / "board-alias"
    (board / "child").mkdir(parents=True)
    workflows = board / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tests.yml").write_text(SOURCE_WITH_JOB, encoding="utf-8")
    result = run_demotion(valid_receipts_dir, board / "child" / "..", apply=True, board_root=board)
    assert_refused(result, "equal to -BoardRoot after path resolution")
    assert not (workflows / "factory-bridge.yml").exists()


def test_completed_destination_rejects_extra_job(tmp_path, valid_receipts_dir):
    repo, workflows = make_repo(tmp_path, "complete-extra", SOURCE_WITH_JOB)
    first = run_demotion(valid_receipts_dir, repo, apply=True)
    assert first.returncode == 0, first.stdout + first.stderr
    destination = workflows / "factory-bridge.yml"
    destination.write_text(destination.read_text(encoding="utf-8") + "  extra-job:\n    runs-on: ubuntu-latest\n", encoding="utf-8")
    result = run_demotion(valid_receipts_dir, repo, apply=False)
    assert_refused(result, "not a valid")


def test_second_write_failure_reports_destination_only(tmp_path, valid_receipts_dir):
    repo, workflows = make_repo(tmp_path, "second-write", SOURCE_WITH_JOB)
    source_before = (workflows / "tests.yml").read_bytes()
    harness = tmp_path / "second-write-harness.ps1"
    ps = """$actor = '__ACTOR__'
$all = Get-Content -LiteralPath $actor -Raw
$marker = '$guardrailMovePath = Join-Path $ReceiptsDir'
Invoke-Expression $all.Substring(0, $all.IndexOf($marker))
$script:writeCount = 0
function Write-AtomicFile {
  param([string]$Path,[string]$Content)
  $script:writeCount++
  if ($script:writeCount -eq 2) { throw 'INJECTED-SECOND-WRITE-FAILURE' }
  [IO.File]::WriteAllText($Path,$Content,(New-Object Text.UTF8Encoding($false)))
}
$g='__GUARD__'; $r='__REQUIRED__'; $repo='__REPO__'
$gs=Get-InputSnapshot $g; $rs=Get-InputSnapshot $r
Invoke-JobDemotion -RepoRoot $repo -Apply $true -GuardrailReceiptPath $g -GuardrailReceiptSnapshot $gs -RequiredReceiptPath $r -RequiredReceiptSnapshot $rs
"""
    quote = lambda value: str(value).replace("'", "''")
    ps = ps.replace("__ACTOR__", quote(SCRIPT)).replace("__GUARD__", quote(valid_receipts_dir / GUARDRAIL_MOVE_NAME)).replace("__REQUIRED__", quote(valid_receipts_dir / REQUIRED_CHECKS_NAME)).replace("__REPO__", quote(repo))
    harness.write_text(ps, encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)], text=True, capture_output=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "INJECTED-SECOND-WRITE-FAILURE" in combined
    changed = combined.split("changed files=", 1)[-1].split("; destination-first", 1)[0]
    assert "factory-bridge.yml" in changed
    assert "tests.yml" not in changed
    assert (workflows / "factory-bridge.yml").is_file()
    assert (workflows / "tests.yml").read_bytes() == source_before


def test_apply_refuses_junction_alias_of_board_root(tmp_path, valid_receipts_dir):
    board = tmp_path / "board-target"
    workflows = board / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tests.yml").write_text(SOURCE_WITH_JOB, encoding="utf-8")
    alias = tmp_path / "board-junction"
    command = "New-Item -ItemType Junction -Path '%s' -Target '%s' | Out-Null" % (
        str(alias).replace("'", "''"), str(board).replace("'", "''")
    )
    made = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True, capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("junction creation unavailable: " + made.stderr)
    result = run_demotion(valid_receipts_dir, alias, apply=True, board_root=board)
    assert_refused(result, "reparse-point path or ancestor")
    assert not (workflows / "factory-bridge.yml").exists()


@pytest.mark.parametrize("changed_input", ["source", "destination", "guardrail", "required"])
def test_actual_apply_refuses_input_drift_before_first_write(tmp_path, valid_receipts_dir, changed_input):
    repo, workflows = make_repo(tmp_path, "input-drift", SOURCE_WITH_JOB)
    paths = {
        "source": workflows / "tests.yml",
        "destination": workflows / "factory-bridge.yml",
        "guardrail": valid_receipts_dir / GUARDRAIL_MOVE_NAME,
        "required": valid_receipts_dir / REQUIRED_CHECKS_NAME,
    }
    before = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    harness = tmp_path / "input-drift.ps1"
    ps = r"""$actor='__ACTOR__'
$all=Get-Content -LiteralPath $actor -Raw
$marker='$guardrailMovePath = Join-Path $ReceiptsDir'
Invoke-Expression $all.Substring(0,$all.IndexOf($marker))
$script:originalNewDest=(Get-Command New-DestinationContent).ScriptBlock
$script:mutatePath='__MUTATE__'
function New-DestinationContent {
  param([string[]]$JobLines)
  [IO.File]::AppendAllText($script:mutatePath,"# concurrent edit`n")
  & $script:originalNewDest -JobLines $JobLines
}
$g='__GUARD__'; $r='__REQUIRED__'; $repo='__REPO__'
$gs=Get-InputSnapshot $g; $rs=Get-InputSnapshot $r
Invoke-JobDemotion -RepoRoot $repo -Apply $true -GuardrailReceiptPath $g -GuardrailReceiptSnapshot $gs -RequiredReceiptPath $r -RequiredReceiptSnapshot $rs
"""
    replacements = {"__ACTOR__": SCRIPT, "__MUTATE__": paths[changed_input],
                    "__GUARD__": paths["guardrail"], "__REQUIRED__": paths["required"], "__REPO__": repo}
    for token, value in replacements.items():
        ps = ps.replace(token, str(value).replace("'", "''"))
    harness.write_text(ps, encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                            text=True, capture_output=True)
    assert_refused(result, "changed after validation")
    for name, path in paths.items():
        if name == changed_input:
            assert path.read_bytes() == (before[name] or b"") + b"# concurrent edit\n"
        elif before[name] is None:
            assert not path.exists()
        else:
            assert path.read_bytes() == before[name]
