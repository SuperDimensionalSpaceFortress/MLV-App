"""THIRD-PARTY-PRODUCER-LANES-1 r1: the `neon` (agy / Gemini 4 Neon) and `kimi` (Kimi Code) producer rows.

Fixtures only, never a live provider call: classification is pure (Get-ThirdPartyProducerOutcome), and the
receipt/exit-code/timeout path runs a tiny .cmd stand-in for the CLI through the real
Invoke-ThirdPartyProducerLane. The one live smoke per row is a separate, receipted run (see the PR body).
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LANE = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"
HELPER = ROOT / "tools" / "coordination" / "lane-third-party-producer.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the producer CLIs and the .cmd stand-in are Windows-only")


def pwsh(script, env=None, timeout=180):
    full = ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); . '%s'; %s" % (HELPER.as_posix(), script))
    run_env = dict(os.environ)
    run_env.update(env or {})
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full],
                          capture_output=True, text=True, encoding="utf-8", env=run_env, timeout=timeout)


def pwsh_json(script, env=None):
    done = pwsh(script, env)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def outcome(engine, exit_code, stdout="", stderr="", timed_out=False):
    return pwsh_json(
        "Get-ThirdPartyProducerOutcome -Engine $env:T_ENGINE -ExitCode ([int]$env:T_EXIT) -TimedOut ($env:T_TO -eq '1') "
        "-Stdout $env:T_OUT -Stderr $env:T_ERR | ConvertTo-Json -Depth 6 -Compress",
        {"T_ENGINE": engine, "T_EXIT": str(exit_code), "T_OUT": stdout, "T_ERR": stderr, "T_TO": "1" if timed_out else "0"})


AGY_SUCCESS = json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "OK", "duration_seconds": 2.6,
                          "num_turns": 1, "usage": {"input_tokens": 15000, "output_tokens": 3, "total_tokens": 15003}})
AGY_ERROR = json.dumps({"conversation_id": "c1", "status": "ERROR", "response": "",
                        "error": "authentication failed or timed out", "duration_seconds": 1.0})
AGY_SOFT_DENY_STDERR = "warning: tool call write_file(C:/x/y.txt) was denied; add it to permissions.allow to approve\n"
# The REAL kimi 2.8.0 stream-json of the live smoke (2026-11-25, -m kimi-code/k3.1-pro): no model id, no usage.
KIMI_STREAM = "\n".join([
    '{"role":"meta","type":"system.version","version":"2.8.0"}',
    '{"role":"assistant","tool_calls":[{"type":"function","id":"tool_U1tc77T6zx4csghphReyaPfe","function":{"name":"Write","arguments":"{\\"path\\":\\"smoke-kimi.txt\\",\\"content\\":\\"OK\\"}"}}]}',
    '{"role":"tool","tool_call_id":"tool_U1tc77T6zx4csghphReyaPfe","content":"Wrote 2 bytes to smoke-kimi.txt"}',
    '{"role":"assistant","content":"DONE"}',
    '{"role":"meta","type":"session.resume_hint","session_id":"session_6ace16bb","command":"kimi -r session_6ace16bb","content":"To resume this session: kimi -r session_6ace16bb"}',
])


# ------------------------------------------------------------------ the table rows
def test_neon_and_kimi_rows_exist_pinned_at_high_effort_as_trial_implementers():
    body = LANE.read_text(encoding="utf-8")
    assert "neon   = @{ engine = 'antigravity'; model = 'gemini-4-neon-high';  effort = 'high'; role = 'trial-implementer' }" in body
    assert "kimi   = @{ engine = 'kimicode';    model = 'kimi-code/k3.1-pro'; effort = 'high'; role = 'trial-implementer' }" in body
    assert "'terra', 'neon', 'kimi')]" in body


def test_producer_lanes_are_in_no_seat_map_and_no_scheduler_lane_list():
    """Producers only: a neon/kimi APPROVE counts for nothing, because nothing that reads review seats can see them."""
    allowed = {"Invoke-Lane.ps1", "lane-third-party-producer.ps1", "check-lane-model-currency.py",
               "test_check_lane_model_currency.py", "test_coordination_guardrails.py"}
    pattern = re.compile(r"antigravity|kimicode|['\"]neon['\"]|['\"]kimi['\"]")
    offenders = []
    for path in (ROOT / "tools").rglob("*"):
        if path.suffix not in {".ps1", ".py", ".json", ".psm1"} or path.name in allowed or ".claude-state" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], "a producer-only lane is named outside the launcher: %s" % offenders
    workstream = (ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1").read_text(encoding="utf-8")
    loop = (ROOT / "tools" / "coordination" / "Invoke-WorkstreamLoop.ps1").read_text(encoding="utf-8")
    assert "ValidateSet('opus','sonnet','fable','sol','luna')" in workstream
    assert "ValidateSet('', 'opus', 'sonnet', 'fable', 'sol', 'luna')" in loop


def test_the_helper_never_grants_blanket_approval_or_touches_the_global_agy_settings():
    code = "\n".join(line for line in HELPER.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))
    assert "--dangerously-skip-permissions" not in code
    assert "'--yolo'" not in code and "'-y'" not in code and "'--auto'" not in code, "no approval-mode flag is ever passed as an argument"
    # the only mention is the receipt's prose `scope` string; no path to the global file is ever built or opened
    assert not re.search(r"Join-Path[^\n]*\.gemini|'\.gemini|\\\.gemini\\", code), "the global settings.json must never be written"
    assert code.count("antigravity-cli") == 1 and "is never written" in code[code.index("antigravity-cli"):][:80]
    assert not re.search(r"[^\x00-\x7f]", HELPER.read_text(encoding="utf-8")), "ASCII-only by project convention"


def test_delegation_precedes_every_claude_and_codex_launch_path_and_is_keyed_on_engine():
    body = LANE.read_text(encoding="utf-8")
    delegate = body.index("if (Test-ThirdPartyProducerEngine $cfg.engine) {")
    assert delegate < body.index("if ($cfg.engine -eq 'claude') {\n    $exe  = $CLAUDE_EXE")
    assert delegate < body.index("[MlvLaneJob]::CreateKillOnClose()")
    assert body.index("local-playback-prompt-refused") < delegate
    block = body[delegate:body.index("# ---------------------------------------------------------------- model identity")]
    assert "$Lane -eq" not in block and "'neon'" not in block and "'kimi'" not in block


# ------------------------------------------------------------------ argv
def argv(engine, model, prompt="do the thing", timeout=900, settings="C:/r/neon-001.agy-settings.json"):
    return pwsh_json(
        "@(Get-ThirdPartyProducerArgv -Engine $env:T_ENGINE -Model $env:T_MODEL -Prompt $env:T_PROMPT -TimeoutSec %d "
        "-SettingsPath $env:T_SET) | ConvertTo-Json -Compress" % timeout,
        {"T_ENGINE": engine, "T_MODEL": model, "T_PROMPT": prompt, "T_SET": settings})


def test_agy_argv_is_headless_json_pinned_with_timeout_and_a_per_session_settings_file():
    a = argv("antigravity", "gemini-4-neon-high", prompt="two words\nand a newline", timeout=900)
    assert a == ["-p", "two words\nand a newline", "--model", "gemini-4-neon-high", "--output-format", "json",
                 "--print-timeout", "900s", "--settings", "C:/r/neon-001.agy-settings.json"]
    assert "--dangerously-skip-permissions" not in a


def test_agy_refuses_a_model_that_is_not_a_high_id():
    done = pwsh("Get-ThirdPartyProducerArgv -Engine antigravity -Model gemini-4-neon-xhigh -Prompt x -TimeoutSec 5 -SettingsPath s")
    assert done.returncode != 0 and "producer-model-not-high" in (done.stdout + done.stderr)


def test_kimi_argv_always_pins_the_model_and_has_no_yolo_or_auto_flag():
    a = argv("kimicode", "kimi-code/k3.1-pro", prompt="hello world")
    assert a == ["-p", "hello world", "-m", "kimi-code/k3.1-pro", "--output-format", "stream-json"]
    done = pwsh("Get-ThirdPartyProducerArgv -Engine kimicode -Model '' -Prompt x -TimeoutSec 5")
    assert done.returncode != 0, "an unpinned kimi model must be refused (the CLI default is K2.8 Preview at effort max)"


def test_a_prompt_that_starts_with_a_dash_cannot_read_as_a_flag_and_an_oversized_one_is_refused():
    assert argv("kimicode", "kimi-code/k3.1-pro", prompt="--yolo please")[1].startswith("\n--yolo")
    done = pwsh("Get-ThirdPartyProducerArgv -Engine kimicode -Model m -Prompt ('x' * 28001) -TimeoutSec 5")
    assert done.returncode != 0 and "producer-prompt-too-long" in (done.stdout + done.stderr)


def test_the_agy_settings_file_grants_only_the_producer_tool_set_scoped_to_the_worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    info = pwsh_json("New-AgyProducerSettings -WorkDir $env:T_WT -Path $env:T_P | ConvertTo-Json -Depth 5 -Compress",
                     {"T_WT": str(wt), "T_P": str(tmp_path / "s.json")})
    written = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert set(written) == {"permissions"} and set(written["permissions"]) == {"allow"}
    allow = written["permissions"]["allow"]
    assert "command(git)" in allow and "command(pwsh)" in allow
    writes = [r for r in allow if r.startswith("write_file(")]
    assert writes and all(str(wt).replace("\\", "/").lower() in r.replace("\\", "/").lower() for r in writes)
    assert not any(r in ("write_file(*)", "command(*)") for r in allow)
    assert info["sha256"]


# ------------------------------------------------------------------ outcome classification (fixtures)
def test_agy_success_json_is_complete():
    o = outcome("antigravity", 0, AGY_SUCCESS)
    assert o["state"] == "complete" and o["complete"] is True and o["final"] == "OK"
    assert o["status"] == "SUCCESS" and o["numTurns"] == 1 and o["usage"]["total_tokens"] == 15003


def test_agy_error_status_is_not_complete_even_at_exit_0():
    o = outcome("antigravity", 0, AGY_ERROR)
    assert o["complete"] is False and o["state"] == "ended-incomplete" and o["reason"] == "status-ERROR"
    assert o["error"] == "authentication failed or timed out"


@pytest.mark.parametrize("status", ["CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"])
def test_every_agy_status_other_than_success_is_not_complete(status):
    o = outcome("antigravity", 0, json.dumps({"status": status, "response": "partial"}))
    assert o["complete"] is False and o["reason"] == "status-" + status


def test_agy_soft_deny_on_stderr_is_refused_never_complete_even_with_status_success_and_exit_0():
    o = outcome("antigravity", 0, AGY_SUCCESS, AGY_SOFT_DENY_STDERR)
    assert o["state"] == "refused" and o["complete"] is False and o["reason"] == "tool-soft-deny"
    assert o["providerRefusal"]["kind"] == "tool-soft-deny" and "write_file" in o["providerRefusal"]["match"]


def test_ordinary_agy_stderr_noise_is_not_a_soft_deny():
    o = outcome("antigravity", 0, AGY_SUCCESS, "info: using cached credentials\nwarn: unsupported stream event\n")
    assert o["state"] == "complete"


def test_agy_unknown_model_exit_1_fails_closed_typed():
    o = outcome("antigravity", 1, "", "error: unknown model 'gemini-4-neon-hgih'\n")
    assert o["state"] == "failed" and o["failure"] == "producer-model-unknown" and o["complete"] is False


def test_agy_unauthenticated_exit_1_fails_closed_typed():
    o = outcome("antigravity", 1, AGY_ERROR, "visit https://accounts.google.com/o/oauth2/auth?...\n")
    assert o["state"] == "failed" and o["failure"] == "producer-auth-required"


def test_agy_other_nonzero_exit_unparseable_output_and_empty_response_are_not_complete():
    assert outcome("antigravity", 2, "", "unexpected argument")["reason"] == "agy-exit-2"
    assert outcome("antigravity", 0, "not json at all")["reason"] == "json-unparseable"
    assert outcome("antigravity", 0, json.dumps({"status": "SUCCESS", "response": "  "}))["reason"] == "empty-response"
    assert outcome("antigravity", 0, json.dumps({"response": "x"}))["reason"] == "status-missing"


def test_timeout_is_ended_incomplete_for_both_engines():
    for engine in ("antigravity", "kimicode"):
        o = outcome(engine, -1, "", "", timed_out=True)
        assert o["state"] == "ended-incomplete" and o["reason"] == "timed-out" and o["complete"] is False


def test_kimi_clean_stream_exit_0_is_complete_and_records_no_model_claim():
    o = outcome("kimicode", 0, KIMI_STREAM)
    assert o["state"] == "complete" and o["complete"] is True and o["events"] == 5
    # the answer is the last ASSISTANT content, never the trailing meta resume hint (which also carries `content`)
    assert o["final"] == "DONE"


def test_kimi_exit_1_is_not_complete_and_no_model_configured_is_typed():
    o = outcome("kimicode", 1, "", "Error: No model configured. Run kimi login.\n")
    assert o["complete"] is False and o["state"] == "failed" and o["failure"] == "producer-model-unconfigured"
    o = outcome("kimicode", 1, "", "boom")
    assert o["complete"] is False and o["state"] == "ended-incomplete" and o["reason"] == "kimi-exit-1"


def test_kimi_exit_4_is_refused_session_held():
    o = outcome("kimicode", 4, "", "session is held by another process")
    assert o["state"] == "refused" and o["providerRefusal"]["kind"] == "session-held" and o["complete"] is False


def test_kimi_exit_0_without_events_or_with_an_error_event_is_not_complete():
    assert outcome("kimicode", 0, "plain text, no json")["reason"] == "no-stream-json-events"
    err = json.dumps({"type": "error", "error": "rate limited"})
    o = outcome("kimicode", 0, KIMI_STREAM + "\n" + err)
    assert o["complete"] is False and o["reason"] == "stream-error-event"


# ------------------------------------------------------------------ the real lane function against a stand-in CLI
def make_cli(tmp_path, stdout="", stderr="", code=0, hang=False):
    d = tmp_path / "cli"
    d.mkdir()
    (d / "stdout.txt").write_text(stdout, encoding="utf-8")
    (d / "stderr.txt").write_text(stderr, encoding="utf-8")
    lines = ["@echo off"]
    if hang:
        lines.append("ping -n 120 127.0.0.1 >nul")
    lines += ['type "%~dp0stdout.txt"', 'type "%~dp0stderr.txt" 1>&2', "exit /b %d" % code]
    cli = d / "fixture-cli.cmd"
    cli.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return cli


def run_lane(tmp_path, engine, cli, timeout=60, extra=""):
    wt = tmp_path / "wt"
    run = tmp_path / "run"
    wt.mkdir(exist_ok=True)
    run.mkdir(exist_ok=True)
    cfg = ("@{ engine = 'antigravity'; model = 'gemini-4-neon-high'; effort = 'high'; role = 'trial-implementer' }"
           if engine == "antigravity" else
           "@{ engine = 'kimicode'; model = 'kimi-code/k3.1-pro'; effort = 'high'; role = 'trial-implementer' }")
    lane = "neon" if engine == "antigravity" else "kimi"
    script = ("$r = Invoke-ThirdPartyProducerLane -Lane %s -Cfg %s -Prompt 'say ok' -WorkDir $env:T_WT -RunDir $env:T_RUN "
              "-TimeoutSec %d -Card T-1 -ExeForTest $env:T_CLI %s; "
              "[ordered]@{ exit = $r.ExitCode; state = $r.Receipt.state } | ConvertTo-Json -Compress") % (lane, cfg, timeout, extra)
    res = pwsh_json(script, {"T_WT": str(wt), "T_RUN": str(run), "T_CLI": str(cli)})
    receipt = json.loads((run / ("%s-001.receipt.json" % lane)).read_text(encoding="utf-8"))
    return res, receipt


def test_lane_receipt_for_a_successful_neon_run(tmp_path):
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stdout=AGY_SUCCESS))
    assert res == {"exit": 0, "state": "complete"}
    assert r["complete"] is True and r["engine"] == "antigravity" and r["role"] == "trial-implementer"
    assert r["effort"] == "high" and r["requestedModel"] == "gemini-4-neon-high"
    assert r["resolvedModel"] == "pinned, not reported"
    assert r["producerArgv"][0] == "-p" and r["producerArgv"][1] == "<prompt: see promptPath>"
    assert "--dangerously-skip-permissions" not in r["producerArgv"]
    assert r["spend"]["usage"]["total_tokens"] == 15003 and r["spend"]["numTurns"] == 1
    assert r["authority"]["settings"]["allow"][0] == "command(git)"
    assert Path(r["promptPath"]).read_text(encoding="utf-8") == "say ok"


def test_lane_receipt_for_a_soft_denied_neon_run_is_refused_and_exits_125(tmp_path):
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stdout=AGY_SUCCESS, stderr=AGY_SOFT_DENY_STDERR))
    assert res == {"exit": 125, "state": "refused"}
    assert r["complete"] is False and r["providerRefusal"]["kind"] == "tool-soft-deny" and r["resolvedModel"] == "unknown"


def test_lane_receipt_for_an_unknown_model_fails_closed_with_a_nonzero_exit(tmp_path):
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stderr="error: unknown model\n", code=1))
    assert res["state"] == "failed" and res["exit"] != 0
    assert r["failure"] == "producer-model-unknown" and r["complete"] is False


def test_lane_receipt_for_a_neon_error_status_is_not_complete_and_never_exits_0(tmp_path):
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stdout=json.dumps({"status": "ERROR", "error": "x"})))
    assert r["complete"] is False and res["state"] == "ended-incomplete" and res["exit"] != 0


def test_lane_receipt_for_kimi_complete_exit_1_and_exit_4(tmp_path):
    res, r = run_lane(tmp_path, "kimicode", make_cli(tmp_path, stdout=KIMI_STREAM))
    assert res == {"exit": 0, "state": "complete"} and r["requestedModel"] == "kimi-code/k3.1-pro"
    assert r["resolvedModel"] == "pinned, not reported" and "no effort flag" in r["effortNote"]
    assert r["producerArgv"][2:4] == ["-m", "kimi-code/k3.1-pro"]
    sub = tmp_path / "one"
    sub.mkdir()
    res, r = run_lane(sub, "kimicode", make_cli(sub, stderr="boom", code=1))
    assert res["exit"] == 1 and r["complete"] is False and res["state"] == "ended-incomplete"
    sub = tmp_path / "four"
    sub.mkdir()
    res, r = run_lane(sub, "kimicode", make_cli(sub, code=4))
    assert res == {"exit": 125, "state": "refused"} and r["providerRefusal"]["kind"] == "session-held"


def test_the_lane_enforces_its_own_timeout_by_killing_the_process_tree(tmp_path):
    res, r = run_lane(tmp_path, "kimicode", make_cli(tmp_path, hang=True), timeout=3)
    assert res == {"exit": 124, "state": "ended-incomplete"}
    assert r["timedOut"] is True and r["complete"] is False
    assert r["killOutcome"] == "killed-tree"
    assert r["durationSec"] < 30, "the watchdog must end a hung child at the deadline, not wait for it"


def test_a_producer_lane_that_edits_tracked_files_without_committing_is_not_complete(tmp_path):
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    extra = ("-AllowEdits $true -BaseSha 'abc' -ReadHead { 'abc' } "
             "-SnapshotDirty { $script:n = 1 + $(if (Test-Path variable:script:n) { $script:n } else { 0 }); "
             "@{ ok = $true; map = $(if ($script:n -eq 1) { [ordered]@{} } else { [ordered]@{ 'src/a.cpp' = 'h1' } }); reason = $null } }")
    res, r = run_lane(tmp_path, "antigravity", cli, extra=extra)
    assert r["dirtyCheck"] == "dirty" and r["complete"] is False and res["state"] == "ended-incomplete"
    assert "dirty-worktree-no-commit" in r["workEvidence"]["reason"]


# ------------------------------------------------------------------ the launcher's shared gates still run first
def invoke_lane(args, cwd):
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(LANE)] + args,
                          capture_output=True, text=True, encoding="utf-8", cwd=str(cwd), timeout=120)


@pytest.mark.parametrize("lane", ["neon", "kimi"])
def test_retire_worktree_is_refused_for_a_producer_lane_before_any_run_dir(tmp_path, lane):
    done = invoke_lane(["-Lane", lane, "-Prompt", "x", "-WorkDir", str(tmp_path), "-RetireWorktree"], tmp_path)
    assert "producer-retire-worktree-unsupported" in done.stdout + done.stderr
    assert not (tmp_path / ".claude-state" / "fleet-runs").exists()


@pytest.mark.parametrize("lane", ["neon", "kimi"])
def test_the_allowlist_gate_and_the_image_gate_still_apply_to_a_producer_lane(tmp_path, lane):
    done = invoke_lane(["-Lane", lane, "-Prompt", "x", "-WorkDir", str(tmp_path), "-AllowEdits", "-AllowedTools", "ALL"], tmp_path)
    assert "allowlist-required" in done.stdout + done.stderr
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG")
    done = invoke_lane(["-Lane", lane, "-Prompt", "x", "-WorkDir", str(tmp_path), "-ImagePaths", str(png)], tmp_path)
    assert "image-paths-codex-only" in done.stdout + done.stderr
