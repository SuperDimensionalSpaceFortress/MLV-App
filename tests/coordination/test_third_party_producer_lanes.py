"""THIRD-PARTY-PRODUCER-LANES-1 r1: the `neon` (agy / Gemini 4 Neon) and `kimi` (Kimi Code) producer rows.

Fixtures only, never a live provider call: classification is pure (Get-ThirdPartyProducerOutcome), and the
receipt/exit-code/timeout path runs a tiny .cmd stand-in for the CLI through the real
Invoke-ThirdPartyProducerLane. The one live smoke per row is a separate, receipted run (see the PR body).

The A<n> comments name the adversary finding (pass 1 on a0fa61cb) each test pins.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LANE = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"
HELPER = ROOT / "tools" / "coordination" / "lane-third-party-producer.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the producer CLIs and the .cmd stand-in are Windows-only")

# The tree walker the direct Codex path uses (Stop-CodexDirectChildTree), loaded from Invoke-Lane.ps1 itself the way
# test_lane_detached_child.py does, so the verified-kill path runs here too.
WALKER = ("$src = [IO.File]::ReadAllText('%s'); "
          "$m = [regex]::Match($src, \"(?s)if \\(-not \\('MlvLaneJob' -as \\[type\\]\\)\\) \\{\\s*Add-Type -TypeDefinition @'\\r?\\n(.*?)\\r?\\n'@\"); "
          "if (-not $m.Success) { throw 'job-class-not-found' }; if (-not ('MlvLaneJob' -as [type])) { Add-Type -TypeDefinition $m.Groups[1].Value }; "
          % LANE.as_posix())


def pwsh(script, env=None, timeout=240, walker=False):
    full = ("$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; "
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); %s. '%s'; %s"
            % (WALKER if walker else "", HELPER.as_posix(), script))
    run_env = dict(os.environ)
    run_env.update(env or {})
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full],
                          capture_output=True, text=True, encoding="utf-8", env=run_env, timeout=timeout)


def pwsh_json(script, env=None, walker=False):
    done = pwsh(script, env, walker=walker)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def outcome(engine, exit_code, stdout="", stderr="", timed_out=False, incomplete=False):
    return pwsh_json(
        "Get-ThirdPartyProducerOutcome -Engine $env:T_ENGINE -ExitCode ([int]$env:T_EXIT) -TimedOut ($env:T_TO -eq '1') "
        "-Stdout $env:T_OUT -Stderr $env:T_ERR -OutputIncomplete ($env:T_INC -eq '1') | ConvertTo-Json -Depth 6 -Compress",
        {"T_ENGINE": engine, "T_EXIT": str(exit_code), "T_OUT": stdout, "T_ERR": stderr,
         "T_TO": "1" if timed_out else "0", "T_INC": "1" if incomplete else "0"})


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
    assert not re.search(r"\.gemini|antigravity-cli", code), "no path to the global agy settings.json is ever built or opened"
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
    # r2 A1: --sandbox is always on the agy argv (it is NOT sufficient: the r2 gate showed it does not confine; see the fence tests).
    assert a == ["-p", "two words\nand a newline", "--model", "gemini-4-neon-high", "--sandbox", "--output-format", "json",
                 "--print-timeout", "900s", "--settings", "C:/r/neon-001.agy-settings.json"]
    assert "--dangerously-skip-permissions" not in a


@pytest.mark.parametrize("model", ["gemini-4-neon-xhigh", "gemini-4-neon-high\n", "gemini-4-neon-high ", " gemini-4-neon-high", "gemini-4-neon"])
def test_agy_refuses_a_model_that_is_not_exactly_a_high_id(model):
    # A18: `$` accepted a terminal newline; the anchor is \z now.
    done = pwsh("Get-ThirdPartyProducerArgv -Engine antigravity -Model $env:T_MODEL -Prompt x -TimeoutSec 5 -SettingsPath s", {"T_MODEL": model})
    assert done.returncode != 0 and "producer-model-not-high" in (done.stdout + done.stderr)


def test_kimi_argv_always_pins_the_model_and_has_no_yolo_or_auto_flag():
    a = argv("kimicode", "kimi-code/k3.1-pro", prompt="hello world")
    assert a == ["-p", "hello world", "-m", "kimi-code/k3.1-pro", "--output-format", "stream-json"]
    done = pwsh("Get-ThirdPartyProducerArgv -Engine kimicode -Model '' -Prompt x -TimeoutSec 5")
    assert done.returncode != 0, "an unpinned kimi model must be refused (the CLI default is K2.8 Preview at effort max)"


@pytest.mark.parametrize("engine,model", [("kimicode", "kimi-code/k3.1-pro"), ("antigravity", "gemini-4-neon-high")])
@pytest.mark.parametrize("timeout", [0, -1])
def test_a_budget_below_one_second_is_refused_before_any_argv_exists(engine, model, timeout):
    # A3: kimi accepted -1 / 0 and started a child it could not bound.
    done = pwsh("Get-ThirdPartyProducerArgv -Engine %s -Model %s -Prompt x -TimeoutSec %d -SettingsPath s" % (engine, model, timeout))
    assert done.returncode != 0 and "producer-timeout-invalid" in (done.stdout + done.stderr)


def test_a_prompt_that_starts_with_a_dash_cannot_read_as_a_flag_and_an_oversized_one_is_refused():
    assert argv("kimicode", "kimi-code/k3.1-pro", prompt="--yolo please")[1].startswith("\n--yolo")
    done = pwsh("Get-ThirdPartyProducerArgv -Engine kimicode -Model m -Prompt ('x' * 28001) -TimeoutSec 5")
    assert done.returncode != 0 and "producer-prompt-too-long" in (done.stdout + done.stderr)


def test_the_serialized_command_line_is_bounded_not_just_the_prompt_characters(tmp_path):
    # A10: 20,000 quote characters pass the 28,000-character check but serialize to over 40,000.
    n = pwsh_json("Get-ProducerCommandLineLength -Exe 'C:\\x\\agy.exe' -Argv @('-p', ('\"' * 20000), '--model', 'm') | ConvertTo-Json")
    assert n > 40000
    assert pwsh_json("Get-ProducerCommandLineLength -Exe 'C:\\x\\agy.exe' -Argv @('-p', 'hi there') | ConvertTo-Json") == len('"C:\\x\\agy.exe" -p "hi there"')  # A11: the program is always quoted; no trailing separator
    res, r = run_lane(tmp_path, "kimicode", make_cli(tmp_path, stdout=KIMI_STREAM), prompt_expr="('\"' * 20000)")
    assert r["failure"].startswith("producer-command-line-too-long") and res["state"] == "failed"
    assert not (tmp_path / "cli" / "started.txt").exists(), "no child may start for an over-long command line"


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


@pytest.mark.parametrize("stdout", [
    '[{"status":"SUCCESS","response":"OK"}]',            # a one-element array root (ConvertFrom-Json unrolled it)
    '{"status":["SUCCESS"],"response":["OK"]}',          # one-element field arrays (unrolled to their element)
    '{"status":"success","response":"OK"}',              # -ne is case-insensitive in PowerShell
    '{"status":"SUCCESS","status":"ERROR","response":"OK"}',   # a duplicate key
    '{"status":"SUCCESS","response":{"text":"OK"}}',     # response is not a string
    '"SUCCESS"',
    '[\n{"status":"SUCCESS","response":"OK"}\n]',        # A3: a pretty-printed array whose inner line looked like a result
    '{"status":"SUCCESS","response":"OK"}\nnull',         # A3: a second value after the object
    '{"STATUS":"SUCCESS","RESPONSE":"OK"}',               # A5: field names are exact, not case-folded aliases
])
def test_agy_completion_needs_an_object_with_a_string_status_SUCCESS_and_a_string_response(stdout):
    # A7
    o = outcome("antigravity", 0, stdout)
    assert o["complete"] is False and o["state"] == "ended-incomplete", o


def test_an_iso_looking_answer_keeps_its_text_and_type_for_both_engines():
    # A8: ConvertFrom-Json turned a quoted ISO timestamp into DateTime, dropping the answer.
    o = outcome("antigravity", 0, json.dumps({"status": "SUCCESS", "response": "2026-11-25T12:00:00Z"}))
    assert o["complete"] is True and o["final"] == "2026-11-25T12:00:00Z"
    stream = "\n".join(['{"role":"assistant","content":"earlier"}', '{"role":"assistant","content":"2026-11-25T12:00:00Z"}'])
    o = outcome("kimicode", 0, stream)
    assert o["complete"] is True and o["final"] == "2026-11-25T12:00:00Z"


def test_agy_soft_deny_on_stderr_is_refused_never_complete_even_with_status_success_and_exit_0():
    o = outcome("antigravity", 0, AGY_SUCCESS, AGY_SOFT_DENY_STDERR)
    assert o["state"] == "refused" and o["complete"] is False and o["reason"] == "tool-soft-deny"
    assert o["providerRefusal"]["kind"] == "tool-soft-deny" and "write_file" in o["providerRefusal"]["match"]


@pytest.mark.parametrize("stderr", [
    "warning: write_file(C:/w/fixture.txt) requires your approval",     # A9: 'requires your approval'
    "notice: tool command(git push) needs explicit permission",
    "warn: command(rm) has not been approved; add it to permissions.allow",
    "info: write_file blocked by policy",
    "tool write_file was rejected: approval is required",
    "WARNING: action write_file skipped, permission not granted",
    "warning: tool call write_file(C:/w/a.txt)\nwas denied by policy",       # A4: the notice wraps across lines
    "warning: tool read_file was refused; add it to permissions.allow",   # A4: 'refused'
    "error: write_file is forbidden in this workspace",
])
def test_agy_soft_deny_notices_are_recognised_in_more_than_one_wording(stderr):
    o = outcome("antigravity", 0, AGY_SUCCESS, stderr + "\n")
    assert o["state"] == "refused" and o["complete"] is False, stderr


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


@pytest.mark.parametrize("engine,stdout", [("antigravity", AGY_SUCCESS), ("kimicode", KIMI_STREAM)])
def test_output_that_was_not_fully_read_is_never_complete(engine, stdout):
    # A5: a drain that timed out (or faulted) kept the prefix, which then read as a clean success.
    o = outcome(engine, 0, stdout, incomplete=True)
    assert o["complete"] is False and o["reason"] == "output-incomplete" and o["state"] == "ended-incomplete"


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


@pytest.mark.parametrize("stdout,reason", [
    ("plain text, no json", "unparseable-stream-line"),
    ("{}", "no-assistant-answer"),                                                     # A6
    ('{"role":"meta","type":"system.version","version":"2.8.0"}', "no-assistant-answer"),   # A6
    ('{"role":"assistant","content":"OK"}\n{"type":"error"', "unparseable-stream-line"),    # A6: a torn error line
    ('{"role":"assistant","content":"OK"}\n{"role":"assistant","content":"x"', "unparseable-stream-line"),
    ('{"role":"assistant","content":["OK"]}', "no-assistant-answer"),                  # A7: a one-element content array
    ('{"role":"assistant","content":"OK"}\n{"type":"error","error":"rate limited"}', "stream-error-event"),
    ('{"role":"assistant","content":"OK"}\n{"role":"meta","error":"x"}', "stream-error-event"),
    ('{"ROLE":"assistant","CONTENT":"OK"}', "no-assistant-answer"),                    # A5: exact field names
    ('{"role":"assistant","content":"OK"}\n{"is_error":"true"}', "stream-error-event"),  # A5: a malformed error indicator
    ('{"role":"assistant","content":"OK"}\n{"is_error":1}', "stream-error-event"),
])
def test_kimi_needs_a_clean_object_stream_with_an_assistant_answer_and_no_error_event(stdout, reason):
    o = outcome("kimicode", 0, stdout)
    assert o["complete"] is False and o["reason"] == reason, o


# ------------------------------------------------------------------ the real lane function against a stand-in CLI
def make_cli(tmp_path, stdout="", stderr="", code=0, hang=False, background=False, touch=None, slow=False):
    d = tmp_path / "cli"
    d.mkdir(exist_ok=True)
    (d / "stdout.txt").write_text(stdout, encoding="utf-8")
    (d / "stderr.txt").write_text(stderr, encoding="utf-8")
    lines = ["@echo off", 'echo started> "%~dp0started.txt"']
    if touch:
        lines.append('echo x> "%s"' % touch)
    if slow:
        lines.append("ping -n 3 127.0.0.1 >nul")
    if background:
        lines.append("start \"\" /b ping -n 25 127.0.0.1")
    if hang:
        lines.append("ping -n 120 127.0.0.1 >nul")
    lines += ['type "%~dp0stdout.txt"', 'type "%~dp0stderr.txt" 1>&2', "exit /b %d" % code]
    cli = d / "fixture-cli.cmd"
    cli.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return cli


def run_lane(tmp_path, engine, cli, timeout=60, extra="", prompt_expr="'say ok'", prelude="", wt_name="wt", run_name="run", walker=False,
             effort="high", receipt_name="receipt.json"):
    wt = tmp_path / wt_name
    run = tmp_path / run_name
    wt.mkdir(exist_ok=True)
    run.mkdir(exist_ok=True)
    cfg = ("@{ engine = 'antigravity'; model = 'gemini-4-neon-high'; effort = '%s'; role = 'trial-implementer' }" % effort
           if engine == "antigravity" else
           "@{ engine = 'kimicode'; model = 'kimi-code/k3.1-pro'; effort = '%s'; role = 'trial-implementer' }" % effort)
    lane = "neon" if engine == "antigravity" else "kimi"
    allow = "" if "-AllowEdits" in extra else "-AllowEdits $true"
    script = ("%s$r = Invoke-ThirdPartyProducerLane -Lane %s -Cfg %s -Prompt %s -WorkDir $env:T_WT -RunDir $env:T_RUN "
              "-TimeoutSec %d -Card T-1 -ExeForTest $env:T_CLI %s %s; "
              "[ordered]@{ exit = $r.ExitCode; state = $r.Receipt.state } | ConvertTo-Json -Compress") % (prelude, lane, cfg, prompt_expr, timeout, allow, extra)
    res = pwsh_json(script, {"T_WT": str(wt), "T_RUN": str(run), "T_CLI": str(cli)}, walker=walker)
    receipt = json.loads((run / ("%s-001.%s" % (lane, receipt_name))).read_text(encoding="utf-8"))
    return res, receipt


def test_lane_receipt_for_a_successful_neon_run(tmp_path):
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stdout=AGY_SUCCESS))
    assert res == {"exit": 0, "state": "complete"}
    assert r["complete"] is True and r["engine"] == "antigravity" and r["role"] == "trial-implementer"
    assert r["effort"] == "high" and r["requestedModel"] == "gemini-4-neon-high"
    assert r["resolvedModel"] == "pinned, not reported" and r["allowEdits"] is True
    assert r["producerArgv"][0] == "-p" and r["producerArgv"][1] == "<prompt: see promptPath>"
    assert "--dangerously-skip-permissions" not in r["producerArgv"]
    assert r["spend"]["usage"]["total_tokens"] == 15003 and r["spend"]["numTurns"] == 1
    assert r["authority"]["settings"]["allow"][0] == "command(git)"
    assert Path(r["promptPath"]).read_text(encoding="utf-8") == "say ok"
    assert Path(r["receiptPath"]) == tmp_path / "run" / "neon-001.receipt.json"


def test_the_receipt_redacts_only_the_prompt_argument_by_position(tmp_path):
    # A17: redaction by value replaced the format value too when the prompt was `json` / `stream-json`.
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stdout=AGY_SUCCESS), prompt_expr="'json'")
    assert r["producerArgv"][1] == "<prompt: see promptPath>" and r["producerArgv"][6] == "json"
    sub = tmp_path / "k"
    sub.mkdir()
    res, r = run_lane(sub, "kimicode", make_cli(sub, stdout=KIMI_STREAM), prompt_expr="'stream-json'")
    assert r["producerArgv"][1] == "<prompt: see promptPath>" and r["producerArgv"][5] == "stream-json"


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
    assert "kimi-code/k3.1-pro" in r["effortNote"]
    assert r["producerArgv"][2:4] == ["-m", "kimi-code/k3.1-pro"]
    assert Path(r["outputPath"]).read_text(encoding="utf-8") == "DONE"
    sub = tmp_path / "one"
    sub.mkdir()
    res, r = run_lane(sub, "kimicode", make_cli(sub, stderr="boom", code=1))
    assert res["exit"] == 1 and r["complete"] is False and res["state"] == "ended-incomplete"
    sub = tmp_path / "four"
    sub.mkdir()
    res, r = run_lane(sub, "kimicode", make_cli(sub, code=4))
    assert res == {"exit": 125, "state": "refused"} and r["providerRefusal"]["kind"] == "session-held"


def test_the_lane_enforces_its_own_timeout_by_killing_the_process_tree_and_says_what_it_observed(tmp_path):
    res, r = run_lane(tmp_path, "kimicode", make_cli(tmp_path, hang=True), timeout=3, walker=True)
    assert res == {"exit": 124, "state": "ended-incomplete"}
    assert r["timedOut"] is True and r["complete"] is False and r["processEnded"] is True
    assert r["killOutcome"] == "killed", "with the tree walker loaded every walked descendant was observed to exit"
    assert r["durationSec"] < 30, "the watchdog must end a hung child at the deadline, not wait for it"


def test_without_a_tree_walker_the_kill_label_does_not_claim_descendants_died(tmp_path):
    # A4: 'killed-tree' was claimed as soon as the root exited.
    res, r = run_lane(tmp_path, "kimicode", make_cli(tmp_path, hang=True), timeout=3)
    assert r["killOutcome"] == "killed-root-descendants-unverified"


def test_a_kill_that_is_not_known_to_have_worked_is_a_typed_failure_and_the_process_is_not_called_ended(tmp_path):
    # A4: a Kill that throws left exitCode -1 and processEnded true. The stand-in kill really stops the child, but
    # reports what a failed kill would.
    override = "function Stop-ProducerProcessTree { param($Proc, $LimitMs) try { $Proc.Kill($true) } catch { }; 'uncertain:kill-failed' }; "
    res, r = run_lane(tmp_path, "kimicode", make_cli(tmp_path, hang=True), timeout=3, prelude=override)
    assert res["state"] == "failed" and r["failure"].startswith("producer-kill-uncertain: uncertain:kill-failed")
    assert r["complete"] is False and res["exit"] == 124


def test_stop_producer_process_tree_never_calls_an_exited_root_killed():
    out = pwsh_json("$p = Start-Process -FilePath cmd.exe -ArgumentList '/c','exit 0' -PassThru -WindowStyle Hidden; $p.WaitForExit(); "
                    "Stop-ProducerProcessTree -Proc $p | ConvertTo-Json", walker=True)
    assert out == "uncertain:already-exited"


def test_a_child_that_outlives_the_drain_makes_the_run_incomplete_never_a_clean_success(tmp_path):
    # A5: a descendant keeps the stdout pipe open; the bounded drain keeps only a prefix, which must not read as success.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS, background=True)
    out = pwsh_json(
        "$r = Invoke-ThirdPartyProducerProcess -Exe $env:T_CLI -Argv @('-p','x') -WorkDir $env:T_WT -TimeoutSec 30 -DrainGraceMs 700; "
        "$o = Get-ThirdPartyProducerOutcome -Engine antigravity -ExitCode $r.exitCode -Stdout $r.stdout -Stderr $r.stderr -OutputIncomplete $r.outputIncomplete; "
        "[ordered]@{ drainTimedOut = $r.drainTimedOut; incomplete = $r.outputIncomplete; complete = $o.complete; reason = $o.reason } | ConvertTo-Json -Compress",
        {"T_CLI": str(cli), "T_WT": str(tmp_path)})
    # the descendant's own output shares the pipe, so strict parsing may reject the stream before the drain flag is even
    # consulted; either way the run is not complete (the pure test above pins output-incomplete for a clean prefix)
    assert out["drainTimedOut"] is True and out["incomplete"] is True and out["complete"] is False
    assert out["reason"] in ("output-incomplete", "json-unparseable")


def test_a_producer_lane_without_allow_edits_starts_no_child(tmp_path):
    # A1: the default call ran write-capable while the receipt said allowEdits=false.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    res, r = run_lane(tmp_path, "antigravity", cli, extra="-AllowEdits $false")
    assert res["state"] == "failed" and r["failure"].startswith("producer-allow-edits-required")
    assert not (tmp_path / "cli" / "started.txt").exists()


def test_the_lane_budget_is_one_stopwatch_setup_spends_it_and_a_spent_budget_starts_nothing(tmp_path):
    # A2: the ledger write took 2 s of a 1 s budget and the child was still started with a fresh second.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    res, r = run_lane(tmp_path, "antigravity", cli, timeout=1, extra="-LedgerWriter { param($row) Start-Sleep -Seconds 2 }")
    assert res == {"exit": 124, "state": "ended-incomplete"} and r["timedOut"] is True and r["complete"] is False
    assert not (tmp_path / "cli" / "started.txt").exists(), "a spent budget must not start the provider"
    # A2: no child existed, so no process can be reported as ended.
    assert r["processEnded"] is False


def test_the_runner_measures_what_is_left_on_the_lane_clock_after_the_child_exists(tmp_path):
    # A1: the stale remainder (and, for neon, a fixed +15 s) was waited after Process.Start; a result after the deadline completed.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS, slow=True)
    script = ("$c = [System.Diagnostics.Stopwatch]::StartNew(); Start-Sleep -Milliseconds 1300; "
              "$out = try { Invoke-ThirdPartyProducerProcess -Exe $env:T_CLI -Argv @('-p','x') -WorkDir $env:T_WT -TimeoutSec 1 -Clock $c | Out-Null; 'ran' } "
              "catch [System.TimeoutException] { 'timeout:' + $_.Exception.Message }; $out | ConvertTo-Json")
    assert pwsh_json(script, {"T_CLI": str(cli), "T_WT": str(tmp_path)}) == "timeout:launch-budget-exhausted"
    assert not (tmp_path / "cli" / "started.txt").exists()
    # 0.7 s of the 1 s budget already spent on the lane clock; the child needs ~2 s, so it is killed after ~0.3 s, not 1 s.
    script = ("$c = [System.Diagnostics.Stopwatch]::StartNew(); Start-Sleep -Milliseconds 700; "
              "$r = Invoke-ThirdPartyProducerProcess -Exe $env:T_CLI -Argv @('-p','x') -WorkDir $env:T_WT -TimeoutSec 1 -Clock $c; "
              "[ordered]@{ timedOut = $r.timedOut; exit = $r.exitCode; late = $r.finishedAfterDeadline } | ConvertTo-Json -Compress")
    assert pwsh_json(script, {"T_CLI": str(cli), "T_WT": str(tmp_path)}, walker=True) == {"timedOut": True, "exit": -1, "late": False}


def test_a_nan_budget_and_a_nonpositive_drain_are_refused_by_the_standalone_runner(tmp_path):
    # A13
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    done = pwsh("Invoke-ThirdPartyProducerProcess -Exe $env:T_CLI -Argv @('-p','x') -WorkDir $env:T_WT -TimeoutSec ([double]::NaN)",
                {"T_CLI": str(cli), "T_WT": str(tmp_path)})
    assert done.returncode != 0 and "producer-timeout-invalid" in done.stdout + done.stderr
    done = pwsh("Invoke-ThirdPartyProducerProcess -Exe $env:T_CLI -Argv @('-p','x') -WorkDir $env:T_WT -TimeoutSec 5 -DrainGraceMs -1",
                {"T_CLI": str(cli), "T_WT": str(tmp_path)})
    assert done.returncode != 0
    assert not (tmp_path / "cli" / "started.txt").exists(), "neither refusal may start the child"


def test_deliberate_pre_provider_refusals_still_follow_a_ledger_row(tmp_path):
    # A9: a refusal (here -ReasoningEffort medium reaching the helper) with no ledger row reads as an unreserved launch.
    ledger = tmp_path / "ledger.txt"
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    res, r = run_lane(tmp_path, "antigravity", cli, effort="medium", prelude="$env:T_LED = '%s'; " % ledger,
                      extra="-LedgerWriter { param($row) 'row' | Out-File -LiteralPath $env:T_LED }")
    assert res["state"] == "failed" and r["failure"].startswith("lane-effort-must-be-high")
    assert ledger.exists(), "the ledger row is written BEFORE the effort refusal"
    sub = tmp_path / "noedit"
    sub.mkdir()
    ledger2 = sub / "ledger.txt"
    res, r = run_lane(sub, "antigravity", make_cli(sub, stdout=AGY_SUCCESS), prelude="$env:T_LED = '%s'; " % ledger2,
                      extra="-AllowEdits $false -LedgerWriter { param($row) 'row' | Out-File -LiteralPath $env:T_LED }")
    assert r["failure"].startswith("producer-allow-edits-required") and ledger2.exists()


def test_a_manifest_this_lane_did_not_write_is_not_cleaned_up(tmp_path):
    # A6: scratch-paths.txt is shared by every lane in a run dir; a later lane that registered nothing must not clean
    # (or override the keep decision of) an earlier lane's paths.
    manifest = tmp_path / "scratch-paths.txt"
    manifest.write_text("C:\\mlvtmp\\audit-a\n", encoding="utf-8")
    os.utime(manifest, (1_700_000_000, 1_700_000_000))
    cleanup = "-BulkManifestPath $env:T_MAN -BulkScratchCleanup { [ordered]@{ entries = @('cleaned'); error = $null } }"
    cli = make_cli(tmp_path, stdout=KIMI_STREAM)
    _, r = run_lane(tmp_path, "kimicode", cli, run_name="stale", extra=cleanup, prelude="$env:T_MAN = '%s'; " % manifest)
    assert r["bulkScratch"]["entries"] == [] and "no manifest written by this lane" in r["bulkScratch"]["skipped"]
    sub = tmp_path / "fresh"
    sub.mkdir()
    cli = make_cli(sub, stdout=KIMI_STREAM, touch=str(manifest))
    _, r = run_lane(sub, "kimicode", cli, run_name="fresh", extra=cleanup, prelude="$env:T_MAN = '%s'; " % manifest)
    assert r["bulkScratch"]["entries"] == ["cleaned"]


def test_a_receipt_that_cannot_be_published_is_written_beside_the_slot_and_the_typed_exit_survives(tmp_path):
    # A8: the replace threw after its 2 s retry and the typed exit and the result were lost, leaving a reserved marker.
    override = "function Write-ProducerFileAtomic { param($Path, $Content) throw 'sharing violation held by a reader' }; "
    res, r = run_lane(tmp_path, "antigravity", make_cli(tmp_path, stdout=AGY_SUCCESS, stderr=AGY_SOFT_DENY_STDERR), prelude=override,
                      receipt_name="receipt.terminal.json")
    assert res == {"exit": 125, "state": "refused"}, "the typed exit survives a receipt I/O problem"
    assert r["publishError"].startswith("producer-receipt-publish-failed") and r["providerRefusal"]["kind"] == "tool-soft-deny"
    assert r["receiptPath"].endswith("neon-001.receipt.terminal.json")


def test_head_moved_to_a_commit_that_is_not_a_descendant_is_not_complete(tmp_path):
    # A7: switching to an existing parent commit was read as "a commit landed", and uncommitted edits went unseen.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    clean = "-SnapshotDirty { @{ ok = $true; map = [ordered]@{}; reason = $null } }"
    res, r = run_lane(tmp_path, "antigravity", cli, extra="-BaseSha 'abc' -ReadHead { 'def' } -GitAncestor { param($a, $b) $false } " + clean)
    assert r["dirtyCheck"] == "head-not-descended" and r["complete"] is False and res["state"] == "ended-incomplete"
    sub = tmp_path / "ok"
    sub.mkdir()
    res, r = run_lane(sub, "antigravity", make_cli(sub, stdout=AGY_SUCCESS),
                      extra="-BaseSha 'abc' -ReadHead { 'def' } -GitAncestor { param($a, $b) $true } " + clean)
    assert r["dirtyCheck"] == "clean" and r["complete"] is True, "a real descendant commit with a clean tree completes"
    sub = tmp_path / "left"
    sub.mkdir()
    left = ("-BaseSha 'abc' -ReadHead { 'def' } -GitAncestor { param($a, $b) $true } "
            "-SnapshotDirty { $script:n = 1 + $(if (Test-Path variable:script:n) { $script:n } else { 0 }); "
            "@{ ok = $true; map = $(if ($script:n -eq 1) { [ordered]@{} } else { [ordered]@{ 'a.txt' = 'h' } }); reason = $null } }")
    res, r = run_lane(sub, "antigravity", make_cli(sub, stdout=AGY_SUCCESS), extra=left)
    assert r["dirtyCheck"] == "dirty" and r["complete"] is False, "committing part and leaving the rest edited is not finished"


def test_the_helper_refuses_a_zero_or_negative_budget_before_any_child(tmp_path):
    # A3
    for budget in (0, -1):
        sub = tmp_path / ("b%d" % abs(budget))
        sub.mkdir()
        res, r = run_lane(sub, "kimicode", make_cli(sub, stdout=KIMI_STREAM), timeout=budget)
        assert res["state"] == "failed" and r["failure"].startswith("producer-timeout-invalid") and r["complete"] is False
        assert not (sub / "cli" / "started.txt").exists()


def test_two_runs_in_one_worktree_get_distinct_scratch_dirs_and_each_removes_only_its_own(tmp_path):
    # A11: the scratch leaf hashed only the run-dir leaf and slot, so two run dirs in one worktree shared a TEMP.
    cli = make_cli(tmp_path, stdout=KIMI_STREAM)
    _, a = run_lane(tmp_path, "kimicode", cli, extra="-KeepScratch $true", run_name="runA")
    _, b = run_lane(tmp_path, "kimicode", cli, extra="-KeepScratch $true", run_name="runB")
    assert a["scratch"]["path"] != b["scratch"]["path"]
    assert Path(a["scratch"]["path"]).is_dir() and Path(b["scratch"]["path"]).is_dir()
    _, c = run_lane(tmp_path, "kimicode", cli, run_name="runC")
    assert c["scratch"]["removed"] is True and not Path(c["scratch"]["path"]).exists()
    assert Path(a["scratch"]["path"]).is_dir() and Path(b["scratch"]["path"]).is_dir()


def test_the_scratch_overrides_and_bulk_cleanup_apply_to_a_producer_lane(tmp_path):
    # A12: -KeepScratch, -ScratchRoot (including ''), and the bulk-scratch manifest were all ignored.
    cli = make_cli(tmp_path, stdout=KIMI_STREAM)
    custom = tmp_path / "custom-scratch"
    _, r = run_lane(tmp_path, "kimicode", cli, extra="-ScratchBase $env:T_CUSTOM -KeepScratch $true", run_name="r1", prelude="$env:T_CUSTOM = '%s'; " % custom)
    assert Path(r["scratch"]["path"]).parent == custom and Path(r["scratch"]["path"]).is_dir()
    _, r = run_lane(tmp_path, "kimicode", cli, extra="-ScratchBase ''", run_name="r2")
    assert r["scratch"] is None, "-ScratchRoot '' means no scratch directory at all"
    _, r = run_lane(tmp_path, "kimicode", cli, run_name="r3", extra="-BulkScratchCleanup { [ordered]@{ entries = @('cleaned'); error = $null } }")
    assert r["bulkScratch"]["entries"] == ["cleaned"]


def test_a_reader_holding_the_receipt_open_does_not_lose_the_terminal_receipt(tmp_path):
    # A13: one unguarded Move threw on a transient sharing violation and left a reserved receipt.
    target = tmp_path / "t.json"
    target.write_text("{}", encoding="utf-8")
    holder = subprocess.Popen(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                               "$f = [IO.File]::Open('%s','Open','Read','Read'); 'held' | Out-File '%s'; Start-Sleep -Milliseconds 700; $f.Dispose()"
                               % (target.as_posix(), (tmp_path / "held.flag").as_posix())])
    try:
        for _ in range(200):
            if (tmp_path / "held.flag").exists():
                break
            time.sleep(0.05)
        assert (tmp_path / "held.flag").exists()
        done = pwsh("Write-ProducerFileAtomic -Path $env:T_P -Content '{\"final\":true}'", {"T_P": str(target)})
        assert done.returncode == 0, done.stdout + done.stderr
    finally:
        holder.wait(timeout=30)
    assert json.loads(target.read_text(encoding="utf-8")) == {"final": True}
    assert not list(tmp_path.glob("t.json.*.tmp")), "the temp file is removed"


def test_an_artifact_that_cannot_be_written_still_yields_a_terminal_failed_receipt(tmp_path):
    # A13: a stderr artifact write that threw skipped receipt construction and left only the reserved marker.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    run = tmp_path / "run"
    run.mkdir()
    holder = subprocess.Popen(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                               "$f = [IO.File]::Open('%s','OpenOrCreate','ReadWrite','None'); 'held' | Out-File '%s'; Start-Sleep -Seconds 25; $f.Dispose()"
                               % ((run / "neon-001.err.txt").as_posix(), (tmp_path / "held2.flag").as_posix())])
    try:
        for _ in range(300):
            if (tmp_path / "held2.flag").exists():
                break
            time.sleep(0.05)
        res, r = run_lane(tmp_path, "antigravity", cli)
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert res["state"] == "failed" and r["failure"].startswith("producer-artifact-write-failed") and r["complete"] is False
    assert r["state"] == "failed", "the receipt is terminal, not the reserved marker"


def test_the_fsmonitor_stop_runs_after_the_last_git_read_and_is_recorded(tmp_path):
    # A14: the producer left the worktree's fsmonitor daemon running after its git snapshots.
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    extra = ("-BaseSha 'abc' -ReadHead { $global:CALLS += 'head'; 'abc' } "
             "-SnapshotDirty { $global:CALLS += 'snap'; @{ ok = $true; map = [ordered]@{}; reason = $null } } "
             "-AfterGit { $global:CALLS += 'stop'; [ordered]@{ action = 'stopped'; reason = $null } }")
    res, r = run_lane(tmp_path, "antigravity", cli, extra=extra, prelude="$global:CALLS = @(); ")
    assert r["fsmonitorStop"] == {"action": "stopped", "reason": None}
    assert r["dirtyCheck"] == "clean"


def test_a_failed_prelaunch_git_read_is_unavailable_not_not_applicable(tmp_path):
    # A16
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    res, r = run_lane(tmp_path, "antigravity", cli, extra="-BaseShaFailureReason 'pre-launch rev-parse HEAD failed: boom'")
    assert r["dirtyCheck"] == "unavailable" and "boom" in r["dirtyCheckReason"]


def test_a_producer_lane_that_edits_tracked_files_without_committing_is_not_complete(tmp_path):
    cli = make_cli(tmp_path, stdout=AGY_SUCCESS)
    extra = ("-BaseSha 'abc' -ReadHead { 'abc' } "
             "-SnapshotDirty { $script:n = 1 + $(if (Test-Path variable:script:n) { $script:n } else { 0 }); "
             "@{ ok = $true; map = $(if ($script:n -eq 1) { [ordered]@{} } else { [ordered]@{ 'src/a.cpp' = 'h1' } }); reason = $null } }")
    res, r = run_lane(tmp_path, "antigravity", cli, extra=extra)
    assert r["dirtyCheck"] == "dirty" and r["complete"] is False and res["state"] == "ended-incomplete"
    assert "dirty-worktree-no-commit" in r["workEvidence"]["reason"]


# ------------------------------------------------------------------ the launcher's shared gates still run first
def invoke_lane(args, cwd):
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(LANE)] + args,
                          capture_output=True, text=True, encoding="utf-8", cwd=str(cwd), timeout=120)


# ------------------------------------------------------------------ r2: THE LAUNCH FENCE (hub ruling 2026-11-26)
# `kimi` has no mechanical confinement; `neon` FAILED the A1 sandbox gate (b: a write outside the worktree was not refused,
# c: a read of a credential file under the profile was not refused; receipt gate\neon-001.receipt.json in the r2 run dir).
# Both rows stay in the table and refuse to launch: typed line, non-zero exit, before any process, ledger row, git write or run dir.
FENCE_LINES = {
    "kimi": "LANE_REFUSED kimi reason=no-mechanical-confinement card=KIMI-PRODUCER-CONFINEMENT-1",
    "neon": "LANE_REFUSED neon reason=sandbox-gate-failed card=THIRD-PARTY-PRODUCER-LANES-1",
}
# Every argument shape the r1 launcher tests used (a plain edit launch, no -AllowEdits, -RetireWorktree, -ModelOverride, a
# sub-second budget, -AllowedTools ALL, an image): the fence answers all of them identically and first.
FENCE_SHAPES = [
    ["-AllowEdits", "-AllowedTools", "Read"],
    [],
    ["-RetireWorktree", "-AllowEdits", "-AllowedTools", "Read"],
    ["-AllowEdits", "-AllowedTools", "Read", "-ModelOverride", "fixture-other-model"],
    ["-AllowEdits", "-AllowedTools", "Read", "-TimeoutSec", "0"],
    ["-AllowEdits", "-AllowedTools", "ALL"],
]


@pytest.mark.parametrize("lane", ["neon", "kimi"])
@pytest.mark.parametrize("shape", FENCE_SHAPES, ids=lambda s: " ".join(s) or "no-flags")
def test_a_fenced_producer_lane_prints_the_typed_refusal_and_exits_nonzero_before_any_side_effect(tmp_path, lane, shape):
    ledger_dir = tmp_path / ".claude-state" / "coordination"
    done = invoke_lane(["-Lane", lane, "-Prompt", "x", "-WorkDir", str(tmp_path)] + shape, tmp_path)
    assert done.returncode == 126, done.stdout + done.stderr
    assert FENCE_LINES[lane] in done.stdout.splitlines(), done.stdout + done.stderr
    assert not (tmp_path / ".claude-state").exists() and not ledger_dir.exists(), "no run dir, no ledger row, no scratch"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.mark.parametrize("lane", ["neon", "kimi"])
def test_a_refused_producer_launch_writes_no_git_config_into_the_named_worktree(tmp_path, lane):
    # A15: the worktree fsmonitor disable (a git config write, and a daemon stop) used to run before the producer refusal.
    main = tmp_path / "main"
    main.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "base"],
                 ["config", "extensions.worktreeConfig", "true"]):
        assert _git(main, *args).returncode == 0
    linked = tmp_path / "linked"
    assert _git(main, "worktree", "add", "-q", "--detach", str(linked)).returncode == 0
    assert _git(linked, "config", "--worktree", "core.fsmonitor", "true").returncode == 0
    done = invoke_lane(["-Lane", lane, "-Prompt", "x", "-WorkDir", str(linked)], linked)
    assert done.returncode == 126 and FENCE_LINES[lane] in done.stdout
    assert _git(linked, "config", "--worktree", "--get", "core.fsmonitor").stdout.strip() == "true", "the refusal must not touch the worktree's git config"


def test_the_fence_line_precedes_the_worktree_fsmonitor_git_write_and_every_run_dir():
    # A15 (mooted for the fenced rows): the refusal sits before `Disable-LaneWorktreeFsmonitor`, which writes worktree git config.
    body = LANE.read_text(encoding="utf-8")
    fence = body.index("Get-ThirdPartyProducerLaunchRefusal -Lane $Lane")
    assert fence < body.index("Disable-LaneWorktreeFsmonitor -WorkDir $WorkDir")
    assert fence < body.index("New-Item -ItemType Directory -Path $RunDir -Force")
    assert fence < body.index("Add-DispatchLedgerRow -Path $DispatchLedgerPath")
    assert fence < body.index("[MlvLaneJob]::CreateKillOnClose()")


def test_the_launch_refusal_is_pure_and_names_only_the_fenced_engines():
    out = pwsh_json("[ordered]@{ kimi = (Get-ThirdPartyProducerLaunchRefusal -Lane kimi -Engine kimicode); neon = (Get-ThirdPartyProducerLaunchRefusal -Lane neon -Engine antigravity); "
                    "opus = (Get-ThirdPartyProducerLaunchRefusal -Lane opus -Engine claude); sol = (Get-ThirdPartyProducerLaunchRefusal -Lane sol -Engine codex); "
                    "exit = $THIRD_PARTY_PRODUCER_REFUSED_EXIT } | ConvertTo-Json -Compress")
    assert out == {"kimi": FENCE_LINES["kimi"], "neon": FENCE_LINES["neon"], "opus": None, "sol": None, "exit": 126}


@pytest.mark.parametrize("engine,lane", [("antigravity", "neon"), ("kimicode", "kimi")])
def test_the_helper_refuses_a_direct_call_and_the_real_cli_path_even_with_the_test_seam(tmp_path, engine, lane):
    # A direct caller of Invoke-ThirdPartyProducerLane reaches the same fence: no slot file, no ledger callback, no process.
    run = tmp_path / "run"
    run.mkdir()
    cfg = ("@{ engine = '%s'; model = '%s'; effort = 'high'; role = 'trial-implementer' }"
           % (engine, "gemini-4-neon-high" if engine == "antigravity" else "kimi-code/k3.1-pro"))
    real = ("(Join-Path $env:LOCALAPPDATA 'agy\\bin\\agy.exe')" if engine == "antigravity" else "(Join-Path $env:USERPROFILE '.kimi-code\\bin\\kimi.exe')")
    # r2 pass-4 A1: the seam is decided by what the file IS (a small .cmd), so no alias of the real CLI gets through: the plain
    # path, the extended-length prefix, an 8.3-style spelling, a missing file and a non-.cmd file are ALL refused.
    (tmp_path / "not-a-fixture.exe").write_bytes(b"MZ")
    # pass 5 A1: a small .cmd that FORWARDS to the real CLI is not a fixture either (file, folder and bare-command spellings).
    for name, body in (("forward-kimi.cmd", '@echo off\r\n"%USERPROFILE%\\.kimi-code\\bin\\kimi.exe" %*\r\nexit /b %errorlevel%\r\n'),
                       ("forward-neon.cmd", '@echo off\r\n"%LOCALAPPDATA%\\agy\\bin\\agy.exe" %*\r\nexit /b %errorlevel%\r\n'),
                       ("forward-path.cmd", '@echo off\r\nagy %*\r\n')):
        (tmp_path / name).write_text(body, encoding="ascii")
    for exe_arg in ("", "-ExeForTest %s" % real, "-ExeForTest ('\\\\?\\' + %s)" % real, "-ExeForTest ((%s) -replace 'layib', 'LAYIB')" % real,
                    "-ExeForTest (Join-Path $env:T_WT 'missing-fixture.cmd')", "-ExeForTest (Join-Path $env:T_WT 'not-a-fixture.exe')",
                    "-ExeForTest (Join-Path $env:T_WT 'forward-kimi.cmd')", "-ExeForTest (Join-Path $env:T_WT 'forward-neon.cmd')",
                    "-ExeForTest (Join-Path $env:T_WT 'forward-path.cmd')"):
        script = ("$script:ledgerCalls = 0; try { Invoke-ThirdPartyProducerLane -Lane %s -Cfg %s -Prompt x -WorkDir $env:T_WT -RunDir $env:T_RUN -TimeoutSec 30 -AllowEdits $true "
                  "-LedgerWriter { $script:ledgerCalls++ } %s | Out-Null; 'NO-THROW' } catch { $_.Exception.Message }; \"calls=$($script:ledgerCalls)\"") % (lane, cfg, exe_arg)
        done = pwsh(script, {"T_WT": str(tmp_path), "T_RUN": str(run)})
        assert done.returncode == 0, done.stdout + done.stderr
        assert FENCE_LINES[lane] in done.stdout and "calls=0" in done.stdout and "NO-THROW" not in done.stdout
        assert list(run.iterdir()) == [], "the fence runs before the receipt slot is reserved"


def test_the_agy_sandbox_gate_evidence_is_pinned_in_the_helper():
    # The neon row reopens only through a successor card that re-runs the gate (a ok, b refused, c refused) and edits this entry.
    code = HELPER.read_text(encoding="utf-8")
    assert "antigravity = @{ reason = 'sandbox-gate-failed'; card = 'THIRD-PARTY-PRODUCER-LANES-1' }" in code
    assert "kimicode    = @{ reason = 'no-mechanical-confinement'; card = 'KIMI-PRODUCER-CONFINEMENT-1' }" in code
    assert "gate\\neon-001.receipt.json" in code and "NOT refused" in code


def test_invoke_lane_never_sets_the_helper_test_seam():
    # -ExeForTest is for fixtures only; the launcher must never pass it, or the fence would be a switch.
    assert "ExeForTest" not in LANE.read_text(encoding="utf-8")
