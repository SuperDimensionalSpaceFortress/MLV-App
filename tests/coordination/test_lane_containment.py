import hashlib, importlib.util, json, os, re, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CANDIDATE = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"
DOC = ROOT / "docs" / "lane-containment.md"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")
PWSH = "pwsh.exe"

# CI-FLAKE-LANE-CONTAINMENT-PROBE-TIMEOUT-1: the helper probes below each start a COLD `pwsh -NoProfile` just to
# read or end one process, and a fixed 5 s bound there is a pwsh cold-start race, not a property under test
# (merge_group run 37852625704, #323: subprocess.TimeoutExpired in identity() on windows shard 8/8, 1 failed / 616
# passed). The bound is generous because a healthy probe returns in well under a second -- it only ever binds on a
# loaded host -- and matches the 30 s per-run allowance the #316 startup-budget fixtures give a cold pwsh.
PWSH_PROBE_TIMEOUT_SEC = 30

# PR #105 round 4: Invoke-Lane.ps1 classifies containment.ownerAbsentReason into a
# CLOSED set of fixed tokens, chosen by WHERE a failure happened rather than by what
# its raw exception message said -- free text is not admissible evidence about a
# safety property. This test file cannot import a .ps1 file, so the tokens are
# pinned here as literals; keep them in sync BY HAND with Invoke-Lane.ps1's own
# $OWNER_ABSENT_* constants (declared beside $hostStarted, ~line 337-346).
NO_HOST_TOKENS = {"launch-budget-exhausted", "start-threw"}
POST_START_UNRECORDED = "post-start-unrecorded"

# PR #105 final: containment.ownerKillOutcome tokens, same hand-duplication problem
# as NO_HOST_TOKENS above -- kept in sync BY HAND with Invoke-Lane.ps1's own
# $OWNER_KILL_OUTCOME_* constants (declared beside $ownerKillAttempted, ~line 365-372).
# The cross-family review that added kill-wait-timeout noted this duplication drifts
# silently unless something pins the full set; test_kill_outcome_token_set_matches_
# producer_constants below asserts this literal against the source directly instead
# of trusting the hand-copy.
KILL_OUTCOME_TOKENS = {"already-exited", "killed", "kill-wait-timeout", "kill-threw"}

# LANE-NO-BACKGROUND-END-TURN-1: the deny list now blocks every tool that hands a
# headless lane a callback it has no later turn to receive, on top of the pre-existing
# nested-agent-fanout denial. Kept as one named constant instead of a literal per
# assertion site so this file has exactly one place to update if the list changes.
# Round 3 (fable minor 1): Workflow (background-orchestrated fan-out) and TaskCreate (the
# same background-promise shape as ScheduleWakeup/CronCreate) joined the list.
DISALLOWED_TOOLS_TOKEN = "Agent,Task,Monitor,ScheduleWakeup,CronCreate,CronDelete,RemoteTrigger,Workflow,TaskCreate"

_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}


def assert_owner_absence_is_legitimate(containment, context):
    # The one place round-3, round-4, and round-5 tests all funnel through. Since
    # PR #105 round 5, a POST_START_UNRECORDED receipt carries a NON-NULL ownerPid
    # (captured on its own non-throwing line before the construction that failed),
    # so ownerPid presence/absence no longer distinguishes the states -- only
    # ownerAbsentReason does. See Invoke-Lane.ps1's catch block (~line 679-711, and
    # the pre-assignment kill block around ~line 674-696) for the producer side.
    assert containment is not None, f"ambiguous containment receipt: containment itself is None: {context}"
    reason = containment.get("ownerAbsentReason")
    if reason in NO_HOST_TOKENS:
        assert containment.get("ownerPid") is None, (
            f"a no-host token must never carry a pid -- no host ever existed: {context}"
        )
        return  # legitimate: no host was ever created
    pytest.fail(f"ambiguous or unrecognised containment receipt: ownerAbsentReason is {reason!r}, which is "
                f"not one of the closed-set no-host tokens {NO_HOST_TOKENS!r} -- either it is "
                f"{POST_START_UNRECORDED!r} (a host EXISTED; its pid is recorded so the orphan is never "
                f"invisible, but the receipt is still ambiguous and must never be treated as a legitimate "
                f"absence) or it is unrecognised entirely: {context}")


def wait_json(path, pred=lambda x: True, seconds=12, encoding="utf-8"):
    # Polls for a PARSEABLE document: a file another process is still writing reads as empty or
    # partial (JSONDecodeError at char 0), and that is "not yet", not a failure -- the deadline still is.
    end=time.monotonic()+seconds; last=None
    while time.monotonic()<end:
        try:
            last=json.loads(path.read_text(encoding=encoding))
            if pred(last): return last
        except (FileNotFoundError,PermissionError,json.JSONDecodeError): pass
        time.sleep(.05)
    raise AssertionError(f"timeout {path}: {last!r}")


def identity(pid):
    # 1 s -TimeoutSec deadline race (evidence 2026-09-09): the fallback receipt
    # built in Invoke-Lane.ps1's catch block can carry containment.ownerPid=None
    # when the deadline fires before a contained host was ever started. There is
    # no process to look up in that case, so return None instead of int(None).
    if pid is None: return None
    q=f"$p=Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue;if($null-eq $p){{exit 3}};$p.StartTime.ToUniversalTime().ToString('o')"
    try:
        r=subprocess.run([PWSH,"-NoProfile","-NonInteractive","-Command",q],text=True,capture_output=True,timeout=PWSH_PROBE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # Never read a timed-out probe as "process absent": that would silently pass wait_absent / stop_exact.
        pytest.fail(f"StartTime probe for pid {int(pid)} did not answer within {PWSH_PROBE_TIMEOUT_SEC} s "
                    "(cold pwsh start); the process identity is UNKNOWN, not absent")
    return r.stdout.strip() if r.returncode==0 else None


def wait_absent(item, seconds=10):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if identity(item["pid"]) != item["createdUtc"]: return
        time.sleep(.05)
    raise AssertionError(f"still alive: {item}")


def stop_exact(item):
    if identity(item["pid"]) == item["createdUtc"]:
        subprocess.run([PWSH,"-NoProfile","-NonInteractive","-Command",f"Stop-Process -Id {int(item['pid'])} -Force"],timeout=PWSH_PROBE_TIMEOUT_SEC,check=False)


@pytest.fixture
def fixture_tree(tmp_path):
    grand=tmp_path/"grand.ps1"; child=tmp_path/"child.ps1"; shim=tmp_path/"fake-claude.cmd"
    # State files (child.json / grand.json) appear only once COMPLETE: temp + move. Set-Content creates the file EMPTY before its first byte, so a reader -- or a lane timeout that kills the writer in that window -- saw a zero-length file (CI-FLAKE-LANE-CONTAINMENT-OWNER-LOSS-MOVE-1: JSONDecodeError at char 0).
    # MLV_FIXTURE_GRAND_START_DELAY_MS (test-only, unset = no-op) stands in for a loaded host's slow pwsh cold start.
    grand.write_text("if($env:MLV_FIXTURE_GRAND_START_DELAY_MS){Start-Sleep -Milliseconds ([int]$env:MLV_FIXTURE_GRAND_START_DELAY_MS)};$me=Get-Process -Id $PID;$t=$env:MLV_FIXTURE_GRAND+'.tmp';@{pid=$PID;createdUtc=$me.StartTime.ToUniversalTime().ToString('o')}|ConvertTo-Json|Set-Content -Encoding utf8NoBOM $t;Move-Item -LiteralPath $t -Destination $env:MLV_FIXTURE_GRAND;Start-Sleep -Seconds 60\n",encoding="ascii")
    child.write_text(r'''$ErrorActionPreference='Stop'
# CODEX-KEY-MCP-ESCAPE-1: answers the launcher's pre-launch `codex mcp list --json` probe and
# exits BEFORE the child.json/args.json writes below, so it never stands in for the lane itself.
# MLV_FIXTURE_MCP_LIST_OUTPUT / MLV_FIXTURE_MCP_LIST_EXIT (test-only, unset = a readable 2-server list).
if($args.Count -ge 2 -and $args[0] -eq 'mcp' -and $args[1] -eq 'list'){
  $args|ConvertTo-Json|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_MCP_ARGS
  if($env:MLV_FIXTURE_MCP_LIST_OUTPUT){[Console]::Out.Write((Get-Content -LiteralPath $env:MLV_FIXTURE_MCP_LIST_OUTPUT -Raw))}
  else{[Console]::Out.Write('[{"name":"agent_bridge","enabled":true},{"name":"node_repl","enabled":true}]')}
  # MCP-LIST-STREAM-BOUND-1: MLV_FIXTURE_MCP_LIST_HOLD_PIPE_SECONDS (test-only, unset = no-op) leaves a
  # descendant that inherited the redirected stdout/stderr alive for that long AFTER this process exits.
  if($env:MLV_FIXTURE_MCP_LIST_HOLD_PIPE_SECONDS){
    $hp=[System.Diagnostics.ProcessStartInfo]::new([Environment]::ProcessPath); $hp.UseShellExecute=$false
    foreach($x in @('-NoProfile','-NonInteractive','-Command',"Set-Content -LiteralPath '$env:MLV_FIXTURE_MCP_HOLD_PID' -Value `$PID; Start-Sleep -Seconds $env:MLV_FIXTURE_MCP_LIST_HOLD_PIPE_SECONDS")){[void]$hp.ArgumentList.Add($x)}
    [void][System.Diagnostics.Process]::Start($hp)
    $end=(Get-Date).AddSeconds(30); while(-not (Test-Path -LiteralPath $env:MLV_FIXTURE_MCP_HOLD_PID) -and (Get-Date) -lt $end){Start-Sleep -Milliseconds 100}
  }
  exit ([int]$env:MLV_FIXTURE_MCP_LIST_EXIT)
}
# CODEX-KEY-PRIVATE-PIN-1: answers the launcher's `codex --version` stamp the same way (version
# from MLV_FIXTURE_CODEX_VERSION, unset = 0.0.0-fixture) and exits before any lane-side write.
if($args.Count -ge 1 -and $args[0] -eq '--version'){
  $v=if($env:MLV_FIXTURE_CODEX_VERSION){$env:MLV_FIXTURE_CODEX_VERSION}else{'0.0.0-fixture'}
  [Console]::Out.Write("codex-cli $v`n")
  exit 0
}
$me=Get-Process -Id $PID
$t=$env:MLV_FIXTURE_CHILD+'.tmp';@{pid=$PID;createdUtc=$me.StartTime.ToUniversalTime().ToString('o')}|ConvertTo-Json|Set-Content -Encoding utf8NoBOM $t;Move-Item -LiteralPath $t -Destination $env:MLV_FIXTURE_CHILD
$args|ConvertTo-Json|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_ARGS
$env:CLAUDE_CODE_EFFORT_LEVEL|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_EFFORT
$env:CLAUDE_CODE_DISABLE_BACKGROUND_TASKS|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_BGTASKS
# LANE-NO-BACKGROUND-END-TURN-1 round 2: simulate a lane that edits a tracked file WITHOUT
# committing (env-gated no-op for every other test).
if($env:MLV_FIXTURE_DIRTY_TRACKED_PATH){
  'lane-edit-uncommitted'|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_DIRTY_TRACKED_PATH
}
# Round 3 (sol minor / fable minor 2): simulate a lane that FURTHER edits a tracked file the
# test already dirtied BEFORE launch, without ever staging or committing either edit -- the
# porcelain status line for this path (' M path') is identical before and after, so only a
# content-identity comparison can see the lane's own edit landed on top of the pre-existing one.
if($env:MLV_FIXTURE_FURTHER_EDIT_TRACKED_PATH){
  'lane-further-edit-uncommitted'|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_FURTHER_EDIT_TRACKED_PATH
}
# Round 5 (sol minor 2, required case): further-edit a BINARY tracked file (contains a NUL byte,
# so git renders `git diff HEAD -- path` as the fixed text "Binary files a/path and b/path
# differ" no matter what the actual bytes are) with DIFFERENT bytes than whatever pre-dirtied it.
# A content-identity check built on a hash of that diff TEXT cannot see this edit at all -- the
# rendered text is identical regardless of which binary bytes are on disk. A check built on
# `git hash-object` of the working-tree bytes themselves can.
if($env:MLV_FIXTURE_FURTHER_EDIT_BINARY_TRACKED_PATH){
  [byte[]]$bytes=@(0x00,0x11,0x22,0x33,0x44,0x55,0xEE,0xFF)
  [IO.File]::WriteAllBytes($env:MLV_FIXTURE_FURTHER_EDIT_BINARY_TRACKED_PATH,$bytes)
}
# Simulate a lane that commits its edit (HEAD moves) and then leaves a further,
# still-uncommitted edit behind on top of that commit.
if($env:MLV_FIXTURE_COMMIT_TRACKED_PATH){
  $repoDir=Split-Path $env:MLV_FIXTURE_COMMIT_TRACKED_PATH -Parent
  'lane-edit-committed'|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_COMMIT_TRACKED_PATH
  git -C $repoDir add -A | Out-Null
  git -C $repoDir commit -q -m 'fixture lane commit' | Out-Null
  'leftover-dirty-after-commit'|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_COMMIT_TRACKED_PATH
}
# Round 8 (sol minor): simulate a lane that reaches out via its own Bash/Write grant and
# rewrites, or deletes, the per-run hook copy AFTER the launch self-test already proved it
# denies -- the exact tamper shape the post-run re-hash check exists to catch. The copy's path
# is not passed to this fixture directly (env-gated, like the other MLV_FIXTURE_* blocks), so
# it is located the same way the test itself locates it: the one *.lane-no-background.py file
# reserved beside the receipt in the run dir.
if($env:MLV_FIXTURE_TAMPER_HOOK_COPY){
  $hookCopy=Get-ChildItem -Path (Join-Path $env:MLV_BOARD_ROOT 'run') -Filter '*.lane-no-background.py' | Select-Object -First 1
  if($env:MLV_FIXTURE_TAMPER_HOOK_COPY -eq 'modify'){
    '# tampered'|Set-Content -Encoding utf8NoBOM $hookCopy.FullName
  } elseif($env:MLV_FIXTURE_TAMPER_HOOK_COPY -eq 'delete'){
    Remove-Item -LiteralPath $hookCopy.FullName -Force
  }
}
# Round 14 (sol major, r13): the mirror-image simulation for the INTERPRETER -- a lane that
# reaches out via its own Bash/Write grant and rewrites, or deletes, the resolved interpreter
# executable AFTER the launch self-test already proved it denies through that exact file. The
# launcher's self-test already exited (and released any handle) long before this fake provider
# process ever starts, so this is a plain file rewrite/delete, exactly like the hook-copy block
# above. The path is read from $env:MLV_LANE_PYTHON_EXE -- the same override this fixture's
# harness already sets to a disposable interpreter copy for these tests, never the real one.
if($env:MLV_FIXTURE_TAMPER_INTERPRETER){
  $interpreterPath=$env:MLV_LANE_PYTHON_EXE
  if($env:MLV_FIXTURE_TAMPER_INTERPRETER -eq 'modify'){
    [IO.File]::WriteAllBytes($interpreterPath,[Text.Encoding]::ASCII.GetBytes('tampered'))
  } elseif($env:MLV_FIXTURE_TAMPER_INTERPRETER -eq 'delete'){
    Remove-Item -LiteralPath $interpreterPath -Force
  }
}
if($env:MLV_FIXTURE_MODE -ne 'normal'){
  $g=Start-Process pwsh.exe -ArgumentList @('-NoProfile','-NonInteractive','-File',$env:MLV_FIXTURE_GRAND_SCRIPT) -WindowStyle Hidden -PassThru
  while(-not(Test-Path $env:MLV_FIXTURE_GRAND)){Start-Sleep -Milliseconds 20}
}
$text=[Console]::In.ReadToEnd()
$text|Set-Content -Encoding utf8NoBOM $env:MLV_FIXTURE_PROMPT
if($env:MLV_FIXTURE_MODE -ne 'normal'){Start-Sleep -Seconds 60}
# LANE-MODEL-CURRENCY-1: lets a test substitute the child's own JSON result (e.g. to add a
# modelUsage block) without touching the fixed default every other test still relies on.
if($env:MLV_FIXTURE_RESULT_JSON){
  [Console]::Out.Write((Get-Content -LiteralPath $env:MLV_FIXTURE_RESULT_JSON -Raw))
} else {
[Console]::Out.Write('{"type":"result","subtype":"success","is_error":false,"terminal_reason":"completed","result":"fixture-result","total_cost_usd":0,"num_turns":1}')
}
[Console]::Error.Write('fixture-err')
exit 0
''',encoding="ascii")
    shim.write_text(f'@echo off\r\n"{PWSH}" -NoProfile -NonInteractive -File "{child}" %*\r\n',encoding="ascii")
    yield {"root":tmp_path,"shim":shim,"child":child,"grand":grand}
    for name in ("child.json","grand.json"):
        p=tmp_path/name
        if p.exists():
            try: stop_exact(json.loads(p.read_text(encoding="utf-8-sig")))
            except Exception: pass


def prepare(tree, mode, assignment_failure=False, editing=False, allowed_tools="", lane="sonnet", mutation=None, allow_bulk_reads=False, result_json=None):
    root=tree["root"]; script=root/"Invoke-Lane.ps1"
    text=CANDIDATE.read_text(encoding="utf-8")
    text=text.replace("$CLAUDE_EXE = Join-Path $env:APPDATA 'npm\\claude.cmd'", "$CLAUDE_EXE = '"+str(tree['shim']).replace("'","''")+"'")
    text=text.replace("$CODEX_EXE  = Join-Path $env:APPDATA 'npm\\codex.cmd'", "$CODEX_EXE = '"+str(tree['shim']).replace("'","''")+"'")
    if assignment_failure:
        text=text.replace("[MlvLaneJob]::AssignOrThrow($jobHandle, $proc.Handle)", "throw [ComponentModel.Win32Exception]::new(5, 'fixture-assignment-failure')")
    if mutation: text=mutation(text)
    script.write_text(text,encoding="utf-8")
    (root/"lane-provider-refusal.ps1").write_bytes((ROOT/"tools"/"coordination"/"lane-provider-refusal.ps1").read_bytes())
    (root/"lane-no-background.py").write_bytes((ROOT/"tools"/"coordination"/"lane-no-background.py").read_bytes())
    (root/"resolve-codex-tier.py").write_bytes((ROOT/"tools"/"coordination"/"resolve-codex-tier.py").read_bytes())
    if editing:
        hook=root/"tools"/"hooks"/"mlv-never-authorized.py"; hook.parent.mkdir(parents=True); hook.write_text("# fixture hook\n",encoding="ascii")
        rec=root/".claude-state"/"coordination"/"dual-lane"/"receipts"/"0.05-hook-enforced.json"; rec.parent.mkdir(parents=True)
        rec.write_text(json.dumps({"hookSha256":hashlib.sha256(hook.read_bytes()).hexdigest()}),encoding="utf-8")
    run=root/"run"; run.mkdir()
    if result_json is not None:
        result_json_path=root/"result.json"; result_json_path.write_text(result_json,encoding="utf-8")
    default_codex_models_cache=root/"codex-models-cache.default.json"
    write_codex_models_cache(default_codex_models_cache)
    env=os.environ.copy(); env.update({
      "MLV_BOARD_ROOT":str(root),"MLV_FIXTURE_MODE":mode,
      "MLV_FIXTURE_CHILD":str(root/"child.json"),"MLV_FIXTURE_GRAND":str(root/"grand.json"),
      "MLV_FIXTURE_GRAND_SCRIPT":str(tree["grand"]),"MLV_FIXTURE_ARGS":str(root/"args.json"),
      "MLV_FIXTURE_PROMPT":str(root/"prompt.txt"),"MLV_FIXTURE_EFFORT":str(root/"effort.txt"),
      "MLV_FIXTURE_BGTASKS":str(root/"bgtasks.txt"),"MLV_FIXTURE_MCP_ARGS":str(root/"mcp-args.json"),
      # Round 10 (sol major 1b): pinned here, not left to whatever Python the launcher's own
      # known-locations/PATH search happens to find on the machine running this suite -- this
      # test process's OWN interpreter is by definition present and working, so every fixture
      # test that does not override MLV_LANE_PYTHON_EXE itself is deterministic regardless of
      # whether the host has a "board Python" installed at the dev-machine known-location or on
      # PATH at all. Round 13: this is the ONLY executable the background gate resolves at all --
      # the exec-form hook registration (command=$PYTHON_EXE, args=[hook copy]) has no shell to
      # resolve, classify, or fall back between.
      "MLV_LANE_PYTHON_EXE":sys.executable,
      # LANE-MODEL-CURRENCY-1 round 1c: pinned to a fixture cache this process writes itself
      # (see write_codex_models_cache above), never the real ~/.codex/models_cache.json -- a
      # hosted CI runner has no such file, so every codex-lane test must be hermetic to it. A
      # test exercising the fail-closed path overrides this entry after prepare() returns.
      "MLV_CODEX_MODELS_CACHE":str(default_codex_models_cache)})
    if result_json is not None:
        env["MLV_FIXTURE_RESULT_JSON"]=str(result_json_path)
    cmd=[PWSH,"-NoLogo","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(script),"-Lane",lane,"-Prompt","fixture prompt","-WorkDir",str(root),"-RunDir",str(run),"-TimeoutSec","3" if mode=="timeout" else "30","-Card","FIXTURE","-ReasoningEffort","low","-NoWorktreeSweep"]
    # -NoWorktreeSweep: the exit sweep defaults ON against C:\mlvtmp; a fixture lane must never
    # look at the host's real lane worktrees (Retire-LaneWorktree.ps1 is not copied here anyway).
    if editing: cmd += ["-AllowEdits","-AllowedTools",allowed_tools]
    if allow_bulk_reads: cmd += ["-AllowBulkReads"]
    return cmd,env,run/(lane+"-001.receipt.json")


# LANE-MODEL-CURRENCY-1 round 1c (hosted-CI hermeticity fix): every codex-lane launch now
# resolves its tier through resolve-codex-tier.py, which reads ~/.codex/models_cache.json by
# default -- a file that exists on a dev box (so these tests passed locally) but not on a
# hosted CI runner, where the tier resolution correctly fails closed and the launch never
# reaches the point any of these tests actually assert on. prepare() writes this fixture cache
# and points MLV_CODEX_MODELS_CACHE at it for every test, hermetically, regardless of what the
# real environment does or does not have -- a test that wants to exercise the fail-closed path
# instead overrides the env entry afterward (see test_codex_tier_resolution_fails_closed_when_
# the_cache_is_unavailable and test_codex_lane_resolves_tier_to_the_highest_version_slug_in_the_
# cache, both of which already do this).
DEFAULT_CODEX_MODELS_CACHE_SLUGS = ("gpt-6-sol", "gpt-6-luna", "gpt-6-astra")


def write_codex_models_cache(path, slugs=DEFAULT_CODEX_MODELS_CACHE_SLUGS):
    path.write_text(json.dumps({"models": [{"slug": slug} for slug in slugs]}), encoding="utf-8")


def settings_path_for(receipt):
    # Invoke-Lane.ps1 names every per-run artifact off the same reserved base
    # ("<lane>-NNN"); the settings file sits beside the receipt with the same base.
    assert receipt.name.endswith(".receipt.json")
    return receipt.parent / (receipt.name[: -len(".receipt.json")] + ".settings.json")


def test_read_only_argv_json_stdin_and_tool_denial(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8")); assert q["state"]=="complete" and q["complete"]
    assert q["containment"]["jobAssigned"] and q["containment"]["promptDelivered"]
    assert q["containment"]["childCreatedUtc"].endswith("Z") and "T" in q["containment"]["childCreatedUtc"]
    assert (fixture_tree["root"]/"prompt.txt").read_text(encoding="utf-8-sig").strip()=="fixture prompt"
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    i=argv.index("--disallowedTools"); assert argv[i+1]==DISALLOWED_TOOLS_TOKEN
    j=argv.index("--allowedTools"); assert argv[j+1]=="Read,Grep,Glob"
    assert "--append-system-prompt" in argv
    notice=argv[argv.index("--append-system-prompt")+1]
    assert argv.count("--append-system-prompt")==1
    assert "only Read, Grep, and Glob" in notice and "do not call or retry" in notice
    assert q["authority"]["capabilityNotice"]==notice
    assert q["outputBytes"]>0 and q["spend"]["costUsd"]==0
    assert q["effort"]=="low"
    assert (fixture_tree["root"]/"effort.txt").read_text(encoding="utf-8-sig").strip()=="low"
    # LANE-MODEL-CURRENCY-1: requestedModel is the table alias ('sonnet'); resolvedModel stays
    # the third state 'unknown' -- never the requested string copied over -- because this
    # fixture's child JSON carries no modelUsage field, so nothing proves what actually ran.
    assert q["requestedModel"]=="sonnet"
    assert q["resolvedModel"]=="unknown"
    assert q["auxiliaryModels"]==[]
    assert q["pinnedByOverride"] is False
    # LANE-NO-BACKGROUND-END-TURN-1 round 2 (hub ruling): NA-3 prohibits assigning ANY
    # CLAUDE_CODE_* variable, so the round-1 CLAUDE_CODE_DISABLE_BACKGROUND_TASKS child-env
    # flag was removed rather than narrowed. Assert it never reaches the child and never
    # reappears in the receipt -- a regression here would silently reintroduce NA-3-prohibited
    # behavior.
    assert (fixture_tree["root"]/"bgtasks.txt").read_text(encoding="utf-8-sig").strip()==""
    assert "backgroundTasks" not in q["authority"]


_RESULT_JSON_PREFIX = '{"type":"result","subtype":"success","is_error":false,"terminal_reason":"completed","result":"fixture-result","total_cost_usd":0,"num_turns":1,"modelUsage":'


# LANE-MODEL-CURRENCY-1 round 1b (sol blocker fix): modelUsage is AGGREGATE usage and can
# carry an auxiliary/tool-call model (e.g. haiku) beside the lane's own model. Only the
# entry whose canonicalModel belongs to the REQUESTED ALIAS'S FAMILY may ever become
# resolvedModel; every other entry must land in auxiliaryModels instead, never overwrite
# or get joined into resolvedModel.
def test_resolved_model_ignores_an_auxiliary_model_beside_the_lane_model(fixture_tree):
    result_json = _RESULT_JSON_PREFIX + json.dumps({
        "claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5-20251001"},
        "sonnet": {"canonicalModel": "claude-sonnet-5"},
    }) + "}"
    cmd,env,receipt=prepare(fixture_tree,"normal",result_json=result_json)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["requestedModel"]=="sonnet"
    assert q["resolvedModel"]=="claude-sonnet-5"
    assert q["auxiliaryModels"]==["claude-haiku-4-5-20251001"]


# DISK-MERGED-WORKTREE-SWEEP-1: the exit sweep is opt-out, and its outcome is always in the receipt.
def test_no_worktree_sweep_leaves_the_receipt_key_null(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    assert "-NoWorktreeSweep" in cmd
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert "worktreeSweep" in q and q["worktreeSweep"] is None


# The sweep must never block the receipt: here it cannot even load its helper (the fixture dir has
# no Retire-LaneWorktree.ps1), and the lane still exits 0 with the failure recorded, not raised.
def test_worktree_sweep_failure_is_recorded_and_never_blocks_the_receipt(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    cmd=[c for c in cmd if c!="-NoWorktreeSweep"]+["-WorktreeSweepRoot",str(fixture_tree["root"]/"no-lanes")]
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["worktreeSweep"]["error"].startswith("cannot-determine")


# No modelUsage entry's canonicalModel belongs to the requested alias's family (a run that
# silently fell back to a wholly different family) -- resolvedModel must stay 'unknown',
# never the requested alias, never a raw modelUsage key, and never the auxiliary entry's id.
def test_resolved_model_is_unknown_when_the_requested_family_is_absent(fixture_tree):
    result_json = _RESULT_JSON_PREFIX + json.dumps({
        "claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5-20251001"},
    }) + "}"
    cmd,env,receipt=prepare(fixture_tree,"normal",result_json=result_json)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["requestedModel"]=="sonnet"
    assert q["resolvedModel"]=="unknown"
    assert q["auxiliaryModels"]==["claude-haiku-4-5-20251001"]


# The matching family entry exists (keyed 'sonnet') but carries no canonicalModel -- the
# blocker sol found copies the raw key ('sonnet') into resolvedModel in this case, which is
# indistinguishable from the requested alias itself proving nothing. resolvedModel must stay
# 'unknown' and the raw key must land in auxiliaryModels, never in resolvedModel.
def test_resolved_model_is_unknown_when_canonical_model_is_missing(fixture_tree):
    result_json = _RESULT_JSON_PREFIX + json.dumps({
        "sonnet": {},
    }) + "}"
    cmd,env,receipt=prepare(fixture_tree,"normal",result_json=result_json)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["requestedModel"]=="sonnet"
    assert q["resolvedModel"]=="unknown"
    assert q["auxiliaryModels"]==["sonnet"]


# LANE-NO-BACKGROUND-END-TURN-1 round 6 (swarm ruling): --disallowedTools cannot reach
# `run_in_background` -- it is a parameter of a tool call, not a separate tool name -- so a
# per-lane Claude Code settings file now wires a per-run COPY of
# tools/coordination/lane-no-background.py as a PreToolUse hook for every Claude-engine lane,
# read-only and editing alike. No env-var mechanism, no NA-3 change: see
# docs/lane-containment.md.
# Round 7 (sol major 1 / fable major): the matcher now covers PowerShell too, which carries
# the same run_in_background parameter and is granted to every editing lane by
# docs/Start-EditingLane.ps1.
def test_read_only_settings_json_wires_lane_no_background_hook(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    i=argv.index("--settings")
    settings=json.loads(settings_path_for(receipt).read_text(encoding="utf-8-sig"))
    assert argv[i+1]==str(settings_path_for(receipt))
    pre=settings["hooks"]["PreToolUse"]
    assert len(pre)==1
    assert pre[0]["matcher"]=="Bash|PowerShell"
    hook=pre[0]["hooks"]
    assert len(hook)==1 and hook[0]["type"]=="command"
    # Round 13: EXEC FORM -- `args` present, `command` is the executable only (never a joined
    # string), and `shell` is absent (ignored when `args` is set per the vendor docs; omitted
    # rather than written misleadingly).
    assert "shell" not in hook[0]
    assert hook[0]["args"]==[hook[0]["args"][0]]
    assert "lane-no-background.py" in hook[0]["args"][0]
    # The Read deny rules stay conditional on -AllowBulkReads exactly as before this round --
    # a read-only lane without -AllowBulkReads still gets them, alongside the new hook.
    assert settings["permissions"]["deny"]
    # Round 7 (sol major 3): the registered command must name a COPY reserved beside this run's
    # receipt, never the source script beside Invoke-Lane.ps1 itself (which sits inside a
    # writable worktree for every editing lane -- docs/Start-EditingLane.ps1 runs Invoke-
    # Lane.ps1 FROM $WorkDir). The copy must actually exist and byte-match the source.
    hook_copy_path = hook[0]["args"][0]
    source_bytes = (fixture_tree["root"]/"lane-no-background.py").read_bytes()
    assert Path(hook_copy_path) != fixture_tree["root"]/"lane-no-background.py"
    assert Path(hook_copy_path).read_bytes() == source_bytes
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{64}", q["authority"]["backgroundHookSha256"])
    assert q["authority"]["backgroundHookSha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_bulk_reads_lane_still_gets_the_hook_without_the_deny_rules(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",allow_bulk_reads=True)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    settings=json.loads(settings_path_for(receipt).read_text(encoding="utf-8-sig"))
    assert "permissions" not in settings
    assert settings["hooks"]["PreToolUse"][0]["matcher"]=="Bash|PowerShell"
    assert "lane-no-background.py" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["args"][0]


def test_editing_settings_json_also_wires_lane_no_background_hook(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert "--settings" in argv
    settings=json.loads(settings_path_for(receipt).read_text(encoding="utf-8-sig"))
    assert settings["hooks"]["PreToolUse"][0]["matcher"]=="Bash|PowerShell"
    hook_entry=settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert "lane-no-background.py" in hook_entry["args"][0]
    assert "shell" not in hook_entry
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["backgroundGate"]=="denied-by-settings-hook"
    assert re.fullmatch(r"[0-9a-f]{64}", q["authority"]["backgroundHookSha256"])
    # Round 9: the receipt records the RESOLVED interpreter path and where it came from
    # (override/PATH/known-location) -- never a hardcoded pin -- so a receipt can be audited
    # against what actually ran the gate on the host that produced it. Round 13: there is no
    # shell kind to record any more (exec form has no shell) -- backgroundGateForm records
    # 'exec' instead, and backgroundGateHookArgs names the argv exec form actually spawns.
    assert q["authority"]["backgroundGateInterpreterPath"]==sys.executable
    assert q["authority"]["backgroundGateInterpreterSource"]=="override:MLV_LANE_PYTHON_EXE"
    assert q["authority"]["backgroundGateForm"]=="exec"
    assert q["authority"]["backgroundGateHookArgs"]==hook_entry["args"]
    assert hook_entry["command"]==q["authority"]["backgroundGateInterpreterPath"]


def test_codex_lane_resolves_tier_to_the_highest_version_slug_in_the_cache(fixture_tree):
    # LANE-MODEL-CURRENCY-1: the table names a TIER ('sol'), never a pinned slug. A fixture
    # cache (MLV_CODEX_MODELS_CACHE override, never the real ~/.codex/models_cache.json) makes
    # this hermetic: it proves the launch resolves the tier to the CACHE's highest version, not
    # that it merely passes the tier name straight through to `-m`.
    cache=fixture_tree["root"]/"models_cache.json"
    cache.write_text(json.dumps({"models":[{"slug":"gpt-5.6-sol"},{"slug":"gpt-6-sol"},{"slug":"gpt-6-solar"}]}),encoding="utf-8")
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    env=dict(env); env["MLV_CODEX_MODELS_CACHE"]=str(cache)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("-m")+1]=="gpt-6-sol"
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["requestedModel"]=="sol"
    assert q["resolvedModel"]=="gpt-6-sol"
    assert q["pinnedByOverride"] is False


def test_model_override_bypasses_tier_resolution_and_is_recorded(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="luna")
    cmd=cmd+["-ModelOverride","gpt-9-luna-pinned-for-test"]
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("-m")+1]=="gpt-9-luna-pinned-for-test"
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["requestedModel"]=="luna"
    assert q["resolvedModel"]=="gpt-9-luna-pinned-for-test"
    assert q["pinnedByOverride"] is True


def test_model_override_on_a_claude_lane_bypasses_the_alias(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sonnet")
    cmd=cmd+["-ModelOverride","claude-sonnet-5-pinned-for-test"]
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("--model")+1]=="claude-sonnet-5-pinned-for-test"
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["requestedModel"]=="sonnet"
    assert q["pinnedByOverride"] is True


def test_codex_tier_resolution_fails_closed_when_the_cache_is_unavailable(fixture_tree):
    # No fixture cache exists at this path, so resolution must refuse the launch entirely
    # (a 'failed' receipt naming the reason) rather than pass the bare tier name to `-m`,
    # which codex would silently reject or mis-launch.
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    env=dict(env); env["MLV_CODEX_MODELS_CACHE"]=str(fixture_tree["root"]/"no-such-cache.json")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert "codex-tier-resolution-failed" in (q["failure"] or "")
    assert q["resolvedModel"]=="unknown"


# LANE-MODEL-CURRENCY-1 round 2 (sol blocker 2): resolvedModel must never be assigned before the
# child is proven to have run. Tier resolution succeeds (selectedModel gets recorded), but
# Process.Start is made to throw in place of an unstartable codex executable -- the exact
# repro sol gave: "CODEX_EXE is missing or unstartable ... Process.Start then throws".
def test_codex_start_failure_leaves_resolved_model_unknown_with_selected_model_recorded(fixture_tree):
    def mutation(text):
        old = ("} else {\n"
               "    $psi.FileName = $exe\n"
               "    foreach ($a in $argv) { [void]$psi.ArgumentList.Add($a) }\n"
               "    $proc = [Diagnostics.Process]::Start($psi)\n"
               "}")
        assert old in text, "codex Process.Start block text has moved; update this fixture mutation"
        new = ("} else {\n"
               "    $psi.FileName = $exe\n"
               "    foreach ($a in $argv) { [void]$psi.ArgumentList.Add($a) }\n"
               "    throw [ComponentModel.Win32Exception]::new(2, 'fixture-codex-start-failure')\n"
               "}")
        return text.replace(old, new)
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol",mutation=mutation)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert q["requestedModel"]=="sol"
    assert q["selectedModel"]=="gpt-6-sol", "the pre-launch choice must still be recorded"
    assert q["resolvedModel"]=="unknown", "a model that never ran must never be named resolvedModel"


# CODEX-RESOLVER-UNTYPED-EDGE-1 (fable hardening): a resolver that emits well-formed JSON
# lacking an 'ok' key (e.g. a corrupted resolve-codex-tier.py) must still fail closed through
# the SAME typed 'codex-tier-resolution-failed' token as an ordinary ok:false result, not a raw
# StrictMode PropertyNotFoundException message.
def test_codex_resolver_degenerate_json_without_ok_field_fails_closed_with_typed_token(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    resolver=fixture_tree["root"]/"resolve-codex-tier.py"
    resolver.write_text("import json\nprint(json.dumps({}))\n", encoding="utf-8")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert "codex-tier-resolution-failed" in (q["failure"] or "")
    assert q["resolvedModel"]=="unknown"


# LANE-MODEL-CURRENCY-1 round 2 (sol blocker 1): a REAL lane launch (an unmodified
# $CLAUDE_EXE/$CODEX_EXE, i.e. equal to the launcher's own $REAL_CLAUDE_EXE/$REAL_CODEX_EXE
# twin) must refuse any effort other than 'high'. prepare() always replaces $CLAUDE_EXE/
# $CODEX_EXE with a disposable shim (so no test can ever launch a real model), so this test
# additionally points $REAL_CLAUDE_EXE at that SAME shim path -- the only way to make
# $ConfiguredExeForLane -eq $RealExeForLane true without ever touching a real CLI -- to prove
# the refusal fires for what the launcher considers a real launch, while every other test in
# this file (which never touches $REAL_CLAUDE_EXE) proves the refusal is skipped for the
# fixture's own provably-fake path.
def test_real_lane_launch_refuses_non_high_effort(fixture_tree):
    def mutation(text):
        old = "$REAL_CLAUDE_EXE = Join-Path $env:APPDATA 'npm\\claude.cmd'"
        assert old in text, "REAL_CLAUDE_EXE declaration has moved; update this fixture mutation"
        shim_literal = str(fixture_tree["shim"]).replace("'", "''")
        return text.replace(old, "$REAL_CLAUDE_EXE = '" + shim_literal + "'")
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sonnet",mutation=mutation)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert "lane-effort-must-be-high" in (q["failure"] or "")
    # Refused before the dispatch ledger row is ever written, so a refused launch is never
    # counted as an attempt.
    assert q["dispatchLedger"]["state"] is None
    assert q["dispatchLedger"]["reservationId"] is None


def test_codex_lane_gets_no_settings_file_or_flag(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert "--settings" not in argv
    assert not settings_path_for(receipt).exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert "backgroundGate" not in q["authority"]
    assert "backgroundBash" not in q["authority"]


# LANE-NO-BACKGROUND-END-TURN-1 round 7 (sol major 3): a live end-to-end proof that an
# editing lane cannot defeat its OWN gate by rewriting the source Invoke-Lane.ps1 copies
# from -- the launcher must have already reserved and hashed its per-run copy before the
# child (which is what would edit the source) ever starts, and the copy must be the one
# actually registered.
def test_hook_copy_is_reserved_in_run_dir_not_resolved_from_worktree(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    run_dir=fixture_tree["root"]/"run"
    copies=list(run_dir.glob("*.lane-no-background.py"))
    assert len(copies)==1, f"expected exactly one reserved hook copy in {run_dir}, found {copies}"
    assert copies[0].parent==run_dir


LANE_NO_BACKGROUND_SCRIPT = ROOT / "tools" / "coordination" / "lane-no-background.py"


def run_lane_no_background(payload_text):
    return subprocess.run(
        [sys.executable, str(LANE_NO_BACKGROUND_SCRIPT)],
        input=payload_text, text=True, capture_output=True, timeout=10)


def run_lane_no_background_json(payload):
    return run_lane_no_background(json.dumps(payload))


# Round 7 (sol major 1 / fable major, and sol major 2): the deny protocol is now exit 2 with
# one stderr line -- the SAME fail-closed protocol tools/hooks/mlv-never-authorized.py uses --
# instead of round 6's exit-0-plus-stdout-JSON, so a launcher self-test can prove the gate
# with one exit-code check. The deny check itself is now tool-name-agnostic (any tool_name
# with a truthy run_in_background), not limited to Bash.
def test_lane_no_background_script_denies_backgrounded_bash():
    r=run_lane_no_background_json({"tool_name":"Bash","tool_input":{"command":"x","run_in_background":True}})
    assert r.returncode==2,(r.stdout,r.stderr)
    assert r.stdout==""
    assert "headless lane" in r.stderr
    assert "later turn" in r.stderr


def test_lane_no_background_script_denies_backgrounded_powershell():
    r=run_lane_no_background_json({"tool_name":"PowerShell","tool_input":{"command":"x","run_in_background":True}})
    assert r.returncode==2,(r.stdout,r.stderr)
    assert "headless lane" in r.stderr


def test_lane_no_background_script_denies_any_tool_with_the_flag():
    # Round 7 required case: an UNKNOWN tool name carrying the flag must still be denied --
    # the registration matcher narrows which calls reach the script at all, but the script's
    # own check must not assume the matcher is the only thing standing in the way.
    r=run_lane_no_background_json({"tool_name":"SomeFutureTool","tool_input":{"run_in_background":True}})
    assert r.returncode==2,(r.stdout,r.stderr)
    assert "headless lane" in r.stderr


def test_lane_no_background_script_allows_bash_without_the_flag():
    r=run_lane_no_background_json({"tool_name":"Bash","tool_input":{"command":"x"}})
    assert r.returncode==0,(r.stdout,r.stderr)
    assert r.stdout=="" and r.stderr==""


def test_lane_no_background_script_allows_false_flag():
    r=run_lane_no_background_json({"tool_name":"Bash","tool_input":{"command":"x","run_in_background":False}})
    assert r.returncode==0,(r.stdout,r.stderr)
    assert r.stdout=="" and r.stderr==""


def test_lane_no_background_script_allows_other_tools_without_the_flag():
    r=run_lane_no_background_json({"tool_name":"Write","tool_input":{"file_path":"x"}})
    assert r.returncode==0,(r.stdout,r.stderr)
    assert r.stdout=="" and r.stderr==""


# Round 7 (sol major 2): fail CLOSED on malformed/missing/non-JSON stdin, never allow.
def test_lane_no_background_script_denies_empty_stdin():
    r=run_lane_no_background("")
    assert r.returncode==2,(r.stdout,r.stderr)
    assert "hook-error" in r.stderr


def test_lane_no_background_script_denies_non_json_stdin():
    r=run_lane_no_background("not json{{{")
    assert r.returncode==2,(r.stdout,r.stderr)
    assert "hook-error" in r.stderr


def test_lane_no_background_script_denies_non_object_json_stdin():
    r=run_lane_no_background("[1, 2, 3]")
    assert r.returncode==2,(r.stdout,r.stderr)
    assert "hook-error" in r.stderr


def test_lane_no_background_script_denies_non_object_tool_input():
    r=run_lane_no_background_json({"tool_name":"Bash","tool_input":"x"})
    assert r.returncode==2,(r.stdout,r.stderr)
    assert "hook-error" in r.stderr


def _load_lane_no_background_module():
    spec = importlib.util.spec_from_file_location("lane_no_background", LANE_NO_BACKGROUND_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# LANE-NO-BACKGROUND-END-TURN-1 round 12 (fable minor), narrowed round 13: the deny-reason
# substring "headless lane" is load-bearing in TWO files that must be hand-kept in sync --
# lane-no-background.py's DENY_REASON (the text ONLY the genuine deny branch ever prints, per
# its own docstring above) and Invoke-Lane.ps1's $backgroundGateExpectedDenySubstring (what the
# launcher's self-test requires in the captured output before it will trust the deny as genuine).
# Round 12 also cross-referenced this file's own _bash_candidate_runs_the_hook helper -- deleted
# in round 13 along with the rest of the shell-candidate machinery it classified, so that third
# site no longer exists; a real deny run against the actual hook script (below) replaces it as
# the check that the substring is not merely a matching constant but genuinely present in
# runtime output, not only a copy-pasted literal that happens to equal DENY_REASON. A reword of
# DENY_REASON alone would refuse every Claude lane launch -- fail-closed, never fail-open -- but
# silently, with nothing catching the drift before it reached a real launch.
def test_deny_reason_substring_matches_launcher_selftest():
    module = _load_lane_no_background_module()
    deny_reason = module.DENY_REASON
    assert deny_reason and isinstance(deny_reason, str), "DENY_REASON must be a non-empty string"
    ps1_text = CANDIDATE.read_text(encoding="utf-8")
    m = re.search(r"\$backgroundGateExpectedDenySubstring\s*=\s*'([^']*)'", ps1_text)
    assert m, "Invoke-Lane.ps1's $backgroundGateExpectedDenySubstring literal changed shape; update this test's regex to match"
    launcher_substring = m.group(1)
    assert launcher_substring, "launcher's expected deny substring must not be empty"
    assert launcher_substring in deny_reason, (
        f"Invoke-Lane.ps1 expects {launcher_substring!r} in the hook's deny output, but "
        f"lane-no-background.py's DENY_REASON is {deny_reason!r} -- these two sites must agree "
        f"or every Claude lane launch would refuse its self-test"
    )
    # The launcher's self-test only trusts a captured deny when this substring is in the
    # ACTUAL runtime stderr of the real deny branch, not merely equal to the DENY_REASON
    # constant read out of context -- run the real hook script and check the same thing the
    # launcher's self-test checks.
    r = run_lane_no_background_json({"tool_name":"Bash","tool_input":{"command":"echo x","run_in_background":True}})
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert launcher_substring in r.stderr
    # Round 14 (fable minor 2): lane-no-background.py's own cross-reference comment above
    # DENY_REASON drifted after round 13 deleted the shell-candidate machinery it named -- it
    # kept citing a deleted helper and a renamed test, and docs/lane-containment.md described the
    # comment inaccurately as a result (a comment about drift-prevention had itself drifted, and
    # nothing checked its TEXT). Assert the comment names the one site that genuinely still
    # hand-duplicates the substring, and never the stale names round 13 orphaned.
    py_text = LANE_NO_BACKGROUND_SCRIPT.read_text(encoding="utf-8")
    comment = py_text.split("DENY_REASON = (", 1)[0]
    assert "$backgroundGateExpectedDenySubstring" in comment, (
        "lane-no-background.py's DENY_REASON comment must name the launcher's literal site"
    )
    assert "test_deny_reason_substring_matches_launcher_selftest" in comment, (
        "lane-no-background.py's DENY_REASON comment must name the current enforcement test, "
        "not a stale one"
    )
    for stale in ("_bash_candidate_runs_the_hook", "test_deny_reason_substring_matches_launcher_and_test_helper"):
        assert stale not in comment, (
            f"lane-no-background.py's DENY_REASON comment still names {stale!r}, deleted/renamed "
            "in round 13 -- the drift-prevention comment has drifted again"
        )


# LANE-NO-BACKGROUND-END-TURN-1 round 7 (sol major 2): the launcher must PROVE the gate
# before ever starting the provider -- run the exact registered command against a synthetic
# background-Bash payload and require the fail-closed deny (exit 2). A self-test that does
# not pass refuses the launch and records why, rather than trusting an unproven hook.
def test_launch_refuses_when_background_gate_selftest_fails(fixture_tree):
    def install_broken_hook(text):
        return text  # Invoke-Lane.ps1 itself is untouched; the SOURCE hook script is broken below.
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit",
                             mutation=install_broken_hook)
    # Overwrite the SOURCE the launcher copies from (written by prepare() before the launcher
    # runs) with a script that always allows, simulating a gate that would fail open.
    (fixture_tree["root"]/"lane-no-background.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="ascii")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    assert not (fixture_tree["root"]/"args.json").exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert not q["complete"]
    assert q["failure"].startswith("background-gate-selftest-failed")


# LANE-NO-BACKGROUND-END-TURN-1 round 13 (hub ruling): the shell-candidate machinery rounds
# 8-12 built here -- $hookCommand string builders, per-shell-kind self-test loops, the
# MLV_GIT_BASH/MLV_LANE_POWERSHELL_EXE overrides, WSL-stub and PATH-precedence fixtures -- is
# DELETED along with the code it tested: exec-form registration (`args` present) has no shell to
# select, classify, or enumerate candidates for, so there is nothing left for those scenarios to
# exercise. What replaces them below: exec form's positive registration shape, a regression guard
# proving the deleted overrides are now inert, and the interpreter failure modes that still
# matter (a resolved-but-broken executable, and a copy that goes missing or gets corrupted
# between being hashed and being self-tested).
def test_launch_proceeds_and_registers_exec_form_exactly_when_python_override_resolves(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"]
    assert q["authority"]["backgroundGateInterpreterPath"]==sys.executable
    assert q["authority"]["backgroundGateInterpreterSource"]=="override:MLV_LANE_PYTHON_EXE"
    assert q["authority"]["backgroundGateForm"]=="exec"
    hook_copies=list((fixture_tree["root"]/"run").glob("*.lane-no-background.py"))
    assert len(hook_copies)==1
    assert q["authority"]["backgroundGateHookArgs"]==[str(hook_copies[0])]
    settings=json.loads(settings_path_for(receipt).read_text(encoding="utf-8-sig"))
    hook_entry=settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook_entry["type"]=="command"
    # Round 13: `command` names the executable directly -- never a quoted, concatenated string
    # the way shell form required -- and `args` is the argument vector, one element per argv slot.
    assert hook_entry["command"]==sys.executable
    assert hook_entry["args"]==[str(hook_copies[0])]
    assert "shell" not in hook_entry
    direct=run_lane_no_background_json({"tool_name":"Bash","tool_input":{"command":"echo x","run_in_background":True}})
    assert direct.returncode==2,(direct.stdout,direct.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert "--settings" in argv
    assert hook_copies[0].read_bytes()==(fixture_tree["root"]/"lane-no-background.py").read_bytes()
    assert q["outputBytes"]>0


# LANE-NO-BACKGROUND-END-TURN-1 round 13: regression guard for a HALF-reverted rollback -- if a
# future edit restored the deleted MLV_GIT_BASH/MLV_LANE_POWERSHELL_EXE overrides without also
# restoring the resolvers that read them (or restored the resolvers but left the exec-form
# registration in place), those env vars would silently do nothing while looking load-bearing.
# Setting both to paths that do not even exist must have NO effect on an exec-form launch: there
# is no shell resolution left for them to feed.
def test_shell_override_env_vars_no_longer_affect_the_launch(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_GIT_BASH"]=str(fixture_tree["root"]/"nonexistent-bash.exe")
    env["MLV_LANE_POWERSHELL_EXE"]=str(fixture_tree["root"]/"nonexistent-powershell.exe")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"]
    assert q["authority"]["backgroundGateForm"]=="exec"
    assert q["authority"]["backgroundGateInterpreterPath"]==sys.executable
    assert q["authority"]["backgroundGateInterpreterSource"]=="override:MLV_LANE_PYTHON_EXE"
    assert "backgroundGateShellKind" not in q["authority"]
    assert "backgroundGateShellPath" not in q["authority"]
    assert "backgroundGateShellSource" not in q["authority"]
    assert "backgroundGateShellValidatedCandidates" not in q["authority"]
    settings=json.loads(settings_path_for(receipt).read_text(encoding="utf-8-sig"))
    hook_entry=settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook_entry["command"]==sys.executable
    assert "shell" not in hook_entry


# LANE-NO-BACKGROUND-END-TURN-1 round 13: the exec-form self-test still cannot trust exit 2
# alone (round 11's finding survives unchanged -- see test_deny_reason_substring_matches_
# launcher_selftest above and the comment on $backgroundGateExpectedDenySubstring). This proves
# it against the one interpreter failure mode exec form actually has: a RESOLVED, present,
# runnable executable that is not a working Python for this hook's script -- never a shell to
# misresolve, since there is no shell left. findstr.exe (always present, reads stdin, exits
# deterministically, never hangs waiting for a TTY) stands in: given the self-test's JSON payload
# on stdin and the hook copy's path as its one argument, findstr treats the path as a search
# pattern, finds no match in the JSON, and exits 1 -- a real, distinct, non-2 exit that proves
# nothing about the hook's own deny branch.
def test_launch_refuses_when_python_override_points_at_a_present_but_non_python_executable(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    findstr=Path(os.environ.get("WINDIR", r"C:\Windows"))/"System32"/"findstr.exe"
    assert findstr.is_file(), "findstr.exe not found -- pick a different always-present decoy binary"
    env["MLV_LANE_PYTHON_EXE"]=str(findstr)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    assert not settings_path_for(receipt).exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert not q["complete"]
    assert q["failure"].startswith("background-gate-selftest-failed")
    # The failure MESSAGE itself names the expected substring ("...containing 'headless lane'
    # from the exact registered executable and argv..."), so only the captured OUTPUT segment
    # is checked for it -- that segment is what proves (or disproves) the deny branch actually ran.
    assert "headless lane" not in q["failure"].rsplit("output:", 1)[-1]
    assert "No such file" not in q["failure"]
    assert q["authority"]["permissionMode"]=="unset"


# LANE-NO-BACKGROUND-END-TURN-1 round 11 (sol minor / fable minor 2), re-shaped for exec form:
# a registration-only regression must still be distinguishable from a genuine deny. Under shell
# form this was a bad path baked into a concatenated command STRING; under exec form there is no
# string to bake a typo into ($hookCopyPath is used as-is, identically, for both the self-test and
# the settings `args` entry -- the round-8 divergence this test family originally guarded against
# is now structurally impossible, not merely fixed). What remains: the copy could still go missing
# between being hashed and being self-tested (a race, a cleanup script, disk pressure). Simulate
# that directly by deleting the copy right after the launcher hashes it -- Python's own "can't
# open file" is exit 2, the SAME exit code the hook's own fail-closed empty-stdin path uses, so
# only the deny-reason substring check (not the exit code alone) catches this.
def test_launch_refuses_when_hook_copy_goes_missing_before_selftest_even_though_python_exits_2_either_way(fixture_tree):
    def delete_hook_copy_before_selftest(text):
        needle = "$backgroundHookLaunchSha256 = (Get-FileHash -LiteralPath $hookCopyPath -Algorithm SHA256).Hash.ToLowerInvariant()"
        assert needle in text, "Invoke-Lane.ps1's hook-copy hash line changed shape; update this fixture's mutation to match"
        broken = needle + "\n        [System.IO.File]::Delete($hookCopyPath)"
        return text.replace(needle, broken)
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=delete_hook_copy_before_selftest)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    assert not (fixture_tree["root"]/"args.json").exists()
    assert not settings_path_for(receipt).exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert not q["complete"]
    assert q["failure"].startswith("background-gate-selftest-failed")
    assert "No such file or directory" in q["failure"]
    assert "a headless lane has no later turn" not in q["failure"]
    assert q["authority"]["permissionMode"]=="unset"
    # Sanity/documentation half, same reasoning as the round-8/9 predecessor this replaces: this
    # does NOT call the reserved per-run copy (which is deliberately missing here) -- it runs the
    # repo SOURCE script directly, proving the interpreter-and-script PAIR still denies correctly
    # when actually present, so the failure above is attributable only to the missing copy.
    direct=run_lane_no_background_json({"tool_name":"Bash","tool_input":{"command":"echo x","run_in_background":True}})
    assert direct.returncode==2,(direct.stdout,direct.stderr)
    assert "headless lane" in direct.stderr


# LANE-NO-BACKGROUND-END-TURN-1 round 13: the mirror-image regression -- the copy is present and
# correctly named, but its CONTENT is broken (a real-world shape: a lane's own Bash/Write grant
# rewrites the copy between the hash and the self-test, the same tamper window the post-run
# re-hash check exists to catch after the run -- this proves the self-test would ALSO refuse the
# launch outright if the tamper happened before the self-test rather than during the run).
def test_launch_refuses_when_hook_copy_is_syntactically_broken_before_selftest(fixture_tree):
    def corrupt_hook_copy_before_selftest(text):
        needle = "$backgroundHookLaunchSha256 = (Get-FileHash -LiteralPath $hookCopyPath -Algorithm SHA256).Hash.ToLowerInvariant()"
        assert needle in text, "Invoke-Lane.ps1's hook-copy hash line changed shape; update this fixture's mutation to match"
        broken = needle + "\n        [System.IO.File]::WriteAllText($hookCopyPath, 'this is not python(')"
        return text.replace(needle, broken)
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=corrupt_hook_copy_before_selftest)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    assert not (fixture_tree["root"]/"args.json").exists()
    assert not settings_path_for(receipt).exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert not q["complete"]
    assert q["failure"].startswith("background-gate-selftest-failed")
    # Same reasoning as the findstr-decoy test above: the failure message names the expected
    # substring in its own explanation text, so only the captured output segment is checked.
    assert "headless lane" not in q["failure"].rsplit("output:", 1)[-1]
    assert q["authority"]["permissionMode"]=="unset"


# LANE-NO-BACKGROUND-END-TURN-1 round 13: a resolved interpreter can still be unspawnable outright
# (a truncated or non-Win32 binary) rather than merely wrong-but-runnable -- this exercises the
# self-test's OWN try/catch around the invocation, not the exit-code/substring checks above.
def test_launch_refuses_when_python_override_is_not_a_valid_win32_executable_and_the_selftest_invocation_itself_throws(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    fake_python=fixture_tree["root"]/"corrupt-python.exe"
    fake_python.write_bytes(b"not a real executable")
    env["MLV_LANE_PYTHON_EXE"]=str(fake_python)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    assert not settings_path_for(receipt).exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert not q["complete"]
    assert q["failure"].startswith("background-gate-selftest-failed")
    assert "interpreter invocation threw" in q["failure"]
    assert "background-gate-copy-failed" not in q["failure"]
    assert q["authority"]["permissionMode"]=="unset"


def test_launch_refuses_when_python_override_is_unresolvable(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_LANE_PYTHON_EXE"]=str(fixture_tree["root"]/"nonexistent-python.exe")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert not q["complete"]
    assert q["failure"].startswith("background-gate-interpreter-not-found")
    assert "override:MLV_LANE_PYTHON_EXE" in q["failure"]


# LANE-NO-BACKGROUND-END-TURN-1 round 8 (sol minor): regression coverage for the post-run
# tamper check (Invoke-Lane.ps1, ~line 1299) that round 7 shipped but never had a dedicated
# test for. A fake provider (the fixture's child.ps1) stands in for a lane that reaches out via
# its own Bash/Write grant mid-run and rewrites, or deletes, the per-run hook copy after the
# launch self-test already proved it denies.
def test_post_run_tamper_check_catches_hook_copy_rewritten_during_the_run(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_FIXTURE_TAMPER_HOOK_COPY"]="modify"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["backgroundGate"]=="background-gate-tampered"
    assert q["authority"]["backgroundGate"]!="denied-by-settings-hook"
    assert q["authority"]["backgroundGateTamperDetail"].startswith("background-gate-tampered: sha256 was")


def test_post_run_tamper_check_catches_hook_copy_deleted_during_the_run(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_FIXTURE_TAMPER_HOOK_COPY"]="delete"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["backgroundGate"]=="background-gate-tampered"
    assert q["authority"]["backgroundGate"]!="denied-by-settings-hook"
    assert q["authority"]["backgroundGateTamperDetail"]=="background-gate-tampered: hook copy missing after run"


# LANE-NO-BACKGROUND-END-TURN-1 round 14 (sol major, r13 / fable minor 1): the interpreter
# named as exec form's `command` got NO post-run check at all before this round -- only the hook
# COPY did. A disposable byte-copy of this test process's own interpreter stands in for the
# resolved python.exe: it cannot be the REAL sys.executable (this fixture deletes/corrupts it,
# and that must never touch the interpreter actually running pytest), but a bare copy fails to
# start on its own (CPython locates its stdlib relative to its own file path, not any install
# registry) -- PYTHONHOME, pointed at the ORIGINAL interpreter's directory, is set alongside the
# override so the copy runs identically to sys.executable without duplicating the whole install.
def _disposable_interpreter_copy(root):
    copy_path = root / "lane-python-interpreter.exe"
    copy_path.write_bytes(Path(sys.executable).read_bytes())
    return copy_path


def test_post_run_tamper_check_catches_interpreter_replaced_during_the_run(fixture_tree):
    interpreter = _disposable_interpreter_copy(fixture_tree["root"])
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_LANE_PYTHON_EXE"]=str(interpreter)
    env["PYTHONHOME"]=str(Path(sys.executable).parent)
    env["MLV_FIXTURE_TAMPER_INTERPRETER"]="modify"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"]
    # The exact hazard sol/fable r13 described: the self-test proved the ORIGINAL bytes deny, so
    # a naive receipt would still read as a clean denial after those bytes were replaced.
    assert q["authority"]["backgroundGate"]=="background-gate-tampered"
    assert q["authority"]["backgroundGate"]!="denied-by-settings-hook"
    assert "interpreter sha256 was" in q["authority"]["backgroundGateTamperDetail"]
    assert re.fullmatch(r"[0-9a-f]{64}", q["authority"]["backgroundGateInterpreterSha256"])
    assert q["authority"]["backgroundGateInterpreterSha256"] == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


def test_post_run_tamper_check_catches_interpreter_deleted_during_the_run(fixture_tree):
    interpreter = _disposable_interpreter_copy(fixture_tree["root"])
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_LANE_PYTHON_EXE"]=str(interpreter)
    env["PYTHONHOME"]=str(Path(sys.executable).parent)
    env["MLV_FIXTURE_TAMPER_INTERPRETER"]="delete"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"]
    assert q["authority"]["backgroundGate"]=="background-gate-tampered"
    assert q["authority"]["backgroundGate"]!="denied-by-settings-hook"
    assert q["authority"]["backgroundGateTamperDetail"]=="background-gate-tampered: interpreter missing after run"


# Both tampers firing in the SAME run must not let one mask the other -- the append-not-overwrite
# behaviour the hook-copy check and workEvidence.reason already use (see Invoke-Lane.ps1).
def test_post_run_tamper_check_reports_both_hook_copy_and_interpreter_tamper_together(fixture_tree):
    interpreter = _disposable_interpreter_copy(fixture_tree["root"])
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_LANE_PYTHON_EXE"]=str(interpreter)
    env["PYTHONHOME"]=str(Path(sys.executable).parent)
    env["MLV_FIXTURE_TAMPER_INTERPRETER"]="delete"
    env["MLV_FIXTURE_TAMPER_HOOK_COPY"]="delete"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["backgroundGate"]=="background-gate-tampered"
    detail=q["authority"]["backgroundGateTamperDetail"]
    assert "hook copy missing after run" in detail
    assert "interpreter missing after run" in detail


def test_timeout_kills_owned_child_and_grandchild(fixture_tree):
    cmd,env,receipt=prepare_timeout_after_ready(fixture_tree)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==124,(r.stdout,r.stderr)
    # A timed-out run ENDED but did not complete its work (2026-09-14: complete means work evidence).
    q=json.loads(receipt.read_text(encoding="utf-8")); assert q["state"]=="ended-incomplete" and q["timedOut"]
    assert q["processEnded"] is True and q["complete"] is False
    wait_absent(wait_json(fixture_tree["root"]/"child.json",encoding="utf-8-sig"))
    wait_absent(wait_json(fixture_tree["root"]/"grand.json",encoding="utf-8-sig"))


def test_owner_loss_closes_job(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"ownerloss")
    outer=subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    q=wait_json(receipt,lambda x:x.get("state")=="running" and x.get("containment",{}).get("promptDelivered"))
    assert q["containment"]["childCreatedUtc"]==identity(q["containment"]["childPid"])
    child=wait_json(fixture_tree["root"]/"child.json"); grand=wait_json(fixture_tree["root"]/"grand.json")
    outer_identity={"pid":outer.pid,"createdUtc":identity(outer.pid)}
    stop_exact(outer_identity); outer.wait(timeout=8)
    wait_absent(child); wait_absent(grand)
    q=json.loads(receipt.read_text(encoding="utf-8")); assert q["state"]=="running" and not q["complete"]


# CI-FLAKE-LANE-CONTAINMENT-OWNER-LOSS-MOVE-1: a reader that holds the receipt open without
# FILE_SHARE_DELETE (Python's open(), an AV scan, the indexer) makes [IO.File]::Move(tmp, dest, overwrite)
# throw "Access to the path is denied" for as long as the handle lives. Unretried, that one throw became
# exitCode -999 / state=failed on a healthy lane (PR #232, #272, #289). The hold is injected deterministically
# just after the temp name is chosen, so it is live at the first Move attempt of EVERY receipt write, and a
# timer thread releases it after 700 ms -- well inside the 2 s retry budget.
_HOLD_RECEIPT_DESTINATION = (
    "if ($Path -like '*.receipt.json' -and (Test-Path -LiteralPath $Path)) {\n"
    "    if (-not ('MlvFixtureHold' -as [type])) { Add-Type -TypeDefinition 'using System; using System.IO; using System.Threading; "
    "public class MlvFixtureHold { static readonly System.Collections.ArrayList Keep = new System.Collections.ArrayList(); "
    "FileStream fs; Timer t; public MlvFixtureHold(string p, int ms) { fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite); "
    "t = new Timer(delegate { fs.Dispose(); }, null, ms, Timeout.Infinite); lock (Keep) { Keep.Add(this); } } }' }\n"
    "    [void][MlvFixtureHold]::new($Path, 700)\n"
    "    [IO.File]::AppendAllText($env:MLV_FIXTURE_HOLD_LOG, \"held`n\")\n"
    "}\n"
)


def test_receipt_write_retries_through_a_transient_destination_lock(fixture_tree):
    def hold_destination(text):
        anchor="    $tmp = \"$Path.$([guid]::NewGuid().ToString('N')).tmp\"\n"
        assert text.count(anchor)==1
        return text.replace(anchor,anchor+_HOLD_RECEIPT_DESTINATION)
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=hold_destination)
    hold_log=fixture_tree["root"]/"hold.log"; env["MLV_FIXTURE_HOLD_LOG"]=str(hold_log)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=40)
    assert r.returncode==0,(r.stdout,r.stderr)
    # The injection really engaged on the running (x2) and final receipt writes, so a pass is not vacuous.
    assert hold_log.read_text(encoding="utf-8").count("held")>=3
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["exitCode"]==0 and q["complete"] is True


def test_wait_json_waits_out_a_file_that_is_still_empty(tmp_path):
    # The reader half: a not-yet-written (zero-length) document is "not yet", never a JSONDecodeError.
    target=tmp_path/"state.json"; target.write_bytes(b"")
    with pytest.raises(json.JSONDecodeError): json.loads(target.read_text(encoding="utf-8"))
    threading.Timer(.4,lambda: target.write_text('{"pid": 7}',encoding="utf-8")).start()
    assert wait_json(target,seconds=5)=={"pid":7}


def test_assignment_failure_starts_no_provider(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",True)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==127,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8")); assert q["state"]=="failed" and not q["complete"]
    assert not q["containment"]["jobAssigned"] and q["containment"]["assignmentErrorCode"]==5
    assert not (fixture_tree["root"]/"child.json").exists()
    assert not (fixture_tree["root"]/"prompt.txt").exists()
    wait_absent({"pid":q["containment"]["ownerPid"],"createdUtc":q["containment"]["ownerCreatedUtc"]})


def test_editing_argv_preserves_allowlist_and_denies_nested_tools(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("--permission-mode")+1]=="acceptEdits"
    assert argv[argv.index("--allowedTools")+1]=="Read,Write,Edit"
    assert argv[argv.index("--disallowedTools")+1]==DISALLOWED_TOOLS_TOKEN
    assert "--append-system-prompt" not in argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["disallowedTools"]==DISALLOWED_TOOLS_TOKEN.split(",")
    assert "backgroundTasks" not in q["authority"]
    assert (fixture_tree["root"]/"bgtasks.txt").read_text(encoding="utf-8-sig").strip()==""


# LANE-NO-BACKGROUND-END-TURN-1 round 2 (sol minor 4 / fable minor 1): the pre-reservation
# rejection must cover every entry in DISALLOWED_TOOLS_TOKEN, not only Agent/Task -- round 1
# checked the deny-list argv/receipt but never exercised the guard against the five newer
# tools, so a drift between the guard and the deny list (exactly what sol found) went untested.
@pytest.mark.parametrize("bad",["Agent"," task ","Read, AGENT ,Write","Read,Task",
    "Monitor","Read,ScheduleWakeup","CronCreate,Write","Read,CronDelete,Write"," remotetrigger ",
    "Workflow","Read, WORKFLOW ,Write","TaskCreate","Read,taskcreate,Write"])
def test_editing_explicit_nested_tool_is_rejected_before_reservation(fixture_tree,bad):
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools=bad)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=10)
    assert r.returncode!=0 and "nested-agent-tool-forbidden" in r.stderr
    assert not receipt.exists() and not (fixture_tree["root"]/"child.json").exists()


@pytest.mark.parametrize("tool",DISALLOWED_TOOLS_TOKEN.split(","))
def test_every_disallowed_tool_is_individually_rejected_from_an_editing_allowlist(fixture_tree,tool):
    # Same guard, exercised one denied tool at a time (as opposed to the mixed-case/whitespace
    # variants above) so a future partial fix -- e.g. one that catches Agent/Task/Monitor but
    # misses a later addition to the deny list -- fails exactly the case it broke.
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools=f"Read,{tool}")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=10)
    assert r.returncode!=0 and "nested-agent-tool-forbidden" in r.stderr
    assert not receipt.exists() and not (fixture_tree["root"]/"child.json").exists()


def _read_denied_tools_display_from_source():
    text = CANDIDATE.read_text(encoding="utf-8")
    m = re.search(r"\$DENIED_TOOLS_DISPLAY\s*=\s*@\(([^)]*)\)", text)
    assert m, "could not find $DENIED_TOOLS_DISPLAY in Invoke-Lane.ps1"
    return [tok.strip().strip("'") for tok in m.group(1).split(",")]


def test_doc_disallowed_tools_token_and_count_word_match_the_constant():
    # Producer brief round 4 (sol + fable minor): docs/lane-containment.md quotes the
    # --disallowedTools token verbatim and names its length in English prose ("the N denied
    # tools"). Round 3 grew the constant from seven entries to nine (Workflow, TaskCreate) and
    # the doc was never updated to match -- it still said seven. Derive BOTH the token and the
    # count word from Invoke-Lane.ps1's own $DENIED_TOOLS_DISPLAY constant directly (not from
    # this file's own DISALLOWED_TOOLS_TOKEN hand-copy, which is itself just as capable of
    # drifting), so a future addition/removal to the deny list fails this test loudly instead of
    # leaving stale doc prose behind.
    tools = _read_denied_tools_display_from_source()
    token = ",".join(tools)
    assert token == DISALLOWED_TOOLS_TOKEN, (
        "this test file's own DISALLOWED_TOOLS_TOKEN hand-copy has drifted from "
        f"Invoke-Lane.ps1's $DENIED_TOOLS_DISPLAY: source={token!r} test-copy={DISALLOWED_TOOLS_TOKEN!r}"
    )
    doc_text = DOC.read_text(encoding="utf-8")
    assert f"`--disallowedTools {token}`" in doc_text, (
        "docs/lane-containment.md's quoted --disallowedTools token does not match "
        f"the Invoke-Lane.ps1 constant {token!r}"
    )
    count_word = _NUMBER_WORDS[len(tools)]
    assert f"the {count_word} denied tools" in doc_text, (
        f"docs/lane-containment.md does not say 'the {count_word} denied tools' "
        f"(the constant currently has {len(tools)} entries)"
    )


def test_codex_editing_lane_with_denied_tool_in_allowlist_is_not_rejected_by_the_claude_only_preflight(fixture_tree):
    # Producer brief round 4, required case (sol major 1): the denied-tool preflight at
    # Invoke-Lane.ps1 (~line 303-309) is now explicitly gated on `$LANES[$Lane].engine -eq
    # 'claude'`, not merely on -AllowEdits. In production a codex+-AllowEdits combination is
    # already refused earlier by the codex-lane-never-edits check (~line 290-292) before this
    # preflight is ever reached, so without neutralising that EARLIER, UNRELATED throw there is
    # no way to exercise this specific gate for a codex invocation at all -- and the whole point
    # of this test is to prove the gate is an explicit engine check, not an accident of check
    # ORDER that a future refactor could silently undo. Neutralise ONLY the codex-never-edits
    # throw (every other check, including the preflight under test, is untouched) and prove a
    # codex allowlist naming a denied tool is NOT rejected here.
    def bypass_codex_never_edits(text):
        old = ("if ($AllowEdits -and $LANES[$Lane].engine -eq 'codex') {\n"
               "    throw \"codex-lane-never-edits: -Lane $Lane with -AllowEdits "
               "(no Claude hook is visible to codex exec)\"\n"
               "}")
        assert text.count(old) == 1
        return text.replace(old, "# fixture: codex-never-edits neutralised for this test only")
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Agent,Write",
                             lane="sol",mutation=bypass_codex_never_edits)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert "nested-agent-tool-forbidden" not in r.stderr, (r.stdout, r.stderr)
    assert r.returncode==0,(r.stdout,r.stderr)


def test_codex_launch_stays_direct_without_claude_flags(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[0]=="exec" and "--disallowedTools" not in argv and "--allowedTools" not in argv
    assert "--append-system-prompt" not in argv
    assert argv[argv.index("-s")+1]=="read-only"
    assert 'model_reasoning_effort=low' in argv or 'model_reasoning_effort="low"' in argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["containment"] is None and q["effort"]=="low" and q["complete"]
    # The background-tasks deny is a claude-only concept (Monitor/ScheduleWakeup/Cron*/
    # RemoteTrigger are claude CLI tools) -- codex must not receive the env var.
    assert (fixture_tree["root"]/"bgtasks.txt").read_text(encoding="utf-8-sig").strip()==""


# CODEX-READONLY-SANDBOX-UNELEVATED-1 (HUB RULING 2026-10-09T00:20:00Z): on Windows the
# elevated sandbox setup fails ("setup refresh had errors", os error 32 on an in-use
# node_repl.exe), so a read-only codex lane could run no shell command at all. The launcher
# passes -c windows.sandbox="unelevated" per call for read-only lanes only. The fake shim's
# cmd.exe -> pwsh -File hop may strip the inner quotes, so both spellings are accepted (the
# same tolerance the effort assertion above already uses).
_UNELEVATED_ARG_FORMS = ('windows.sandbox="unelevated"', 'windows.sandbox=unelevated')


def _windows_sandbox_overrides(argv):
    return [argv[i+1] for i,a in enumerate(argv[:-1]) if a=="-c" and argv[i+1].startswith("windows.sandbox")]


def test_codex_read_only_lane_runs_unelevated_windows_sandbox(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("-s")+1]=="read-only"
    overrides=_windows_sandbox_overrides(argv)
    assert len(overrides)==1 and overrides[0] in _UNELEVATED_ARG_FORMS, argv
    # '-' (prompt on stdin) must stay the LAST argument; see the comment beside $argv.
    assert argv[-1]=="-", argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["sandbox"]=="read-only"
    assert q["authority"]["windowsSandbox"]=="unelevated"


def test_codex_workspace_write_lane_keeps_default_windows_sandbox(fixture_tree):
    # A codex+-AllowEdits launch is refused in production before argv is built, so (exactly as
    # test_codex_editing_lane_with_denied_tool_in_allowlist_... above) only that throw is
    # neutralised, to prove the unelevated override is keyed on read-only, not on engine alone.
    def bypass_codex_never_edits(text):
        old = ("if ($AllowEdits -and $LANES[$Lane].engine -eq 'codex') {\n"
               "    throw \"codex-lane-never-edits: -Lane $Lane with -AllowEdits "
               "(no Claude hook is visible to codex exec)\"\n"
               "}")
        assert text.count(old) == 1
        return text.replace(old, "# fixture: codex-never-edits neutralised for this test only")
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write",
                             lane="sol",mutation=bypass_codex_never_edits)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("-s")+1]=="workspace-write"
    assert _windows_sandbox_overrides(argv)==[], argv
    assert argv[-1]=="-", argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["sandbox"]=="workspace-write"
    assert q["authority"]["windowsSandbox"]=="default"


# CODEX-KEY-MCP-ESCAPE-1 (HUB RULING wf_d36c1040-11d): MCP tools (node_repl, ...) run OUTSIDE
# the codex sandbox, so a read-only codex lane gets none. Measured on codex-cli 0.160.1:
# -c features.plugins=false drops plugin-provided servers, -c features.apps=false drops the
# codex_apps connector, and every server left in `codex mcp list --json` (read at call time,
# never a hard-coded list) gets -c mcp_servers.<name>.enabled=false.
def _mcp_overrides(argv):
    return [argv[i+1] for i,a in enumerate(argv[:-1])
            if a=="-c" and (argv[i+1].startswith("mcp_servers") or argv[i+1].startswith("features."))]


def _bypass_codex_never_edits(text):
    old = ("if ($AllowEdits -and $LANES[$Lane].engine -eq 'codex') {\n"
           "    throw \"codex-lane-never-edits: -Lane $Lane with -AllowEdits "
           "(no Claude hook is visible to codex exec)\"\n"
           "}")
    assert text.count(old) == 1
    return text.replace(old, "# fixture: codex-never-edits neutralised for this test only")


def test_codex_read_only_lane_disables_every_mcp_server(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    listed=json.loads((fixture_tree["root"]/"mcp-args.json").read_text(encoding="utf-8-sig"))
    assert listed[:3]==["mcp","list","--json"] and "features.plugins=false" in listed, listed
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[0]=="exec" and argv[argv.index("-s")+1]=="read-only"
    assert _mcp_overrides(argv)==["features.plugins=false","features.apps=false",
                                  "mcp_servers.agent_bridge.enabled=false",
                                  "mcp_servers.node_repl.enabled=false"], argv
    assert argv[-1]=="-", argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["mcpServers"]=="disabled"
    assert q["authority"]["mcpDisabledServers"]==["agent_bridge","node_repl"]
    assert q["authority"]["mcpDisabledFeatures"]==["plugins","apps"]


def test_codex_read_only_lane_with_no_listed_mcp_server_still_disables_plugins_and_apps(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    listing=fixture_tree["root"]/"mcp-list.json"; listing.write_text("[]",encoding="utf-8")
    env["MLV_FIXTURE_MCP_LIST_OUTPUT"]=str(listing)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert _mcp_overrides(argv)==["features.plugins=false","features.apps=false"], argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["mcpServers"]=="disabled" and q["authority"]["mcpDisabledServers"]==[]


def test_codex_workspace_write_lane_keeps_mcp_servers_unchanged(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write",
                             lane="sol",mutation=_bypass_codex_never_edits)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"mcp-args.json").exists(), "workspace-write must not probe mcp list"
    argv=json.loads((fixture_tree["root"]/"args.json").read_text(encoding="utf-8-sig"))
    assert argv[argv.index("-s")+1]=="workspace-write"
    assert _mcp_overrides(argv)==[], argv
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["authority"]["mcpServers"]=="default"


@pytest.mark.parametrize("output,exit_code", [
    ('[{"name":"node_repl","enabled":true}]', "1"),     # list command failed
    ("Error: failed to load configuration", "0"),       # not JSON
    ('{"name":"node_repl"}', "0"),                      # not a JSON array
    ('[{"enabled":true}]', "0"),                        # entry without a name
    ('[{"name":"a.b","enabled":true}]', "0"),           # name unsafe in a dotted -c key
], ids=["exit-nonzero","unparseable","not-an-array","nameless-entry","unsafe-name"])
def test_codex_read_only_lane_refuses_launch_when_mcp_list_is_unreadable(fixture_tree, output, exit_code):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    listing=fixture_tree["root"]/"mcp-list.json"; listing.write_text(output,encoding="utf-8")
    env["MLV_FIXTURE_MCP_LIST_OUTPUT"]=str(listing); env["MLV_FIXTURE_MCP_LIST_EXIT"]=exit_code
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert "codex-mcp-list-unreadable" in (q["failure"] or ""), q["failure"]
    assert not (fixture_tree["root"]/"args.json").exists(), "the codex exec lane must never start"


# MCP-LIST-STREAM-BOUND-1: the list probe's stream reads share ONE deadline with its exit wait, and each
# stream is capped. (a) the wrapper exits 0 while a descendant keeps the inherited stdout open for far
# longer than the probe budget -> a named stream refusal inside budget plus margin, not an unbounded wait.
def test_codex_read_only_lane_refuses_when_a_descendant_holds_the_mcp_list_pipe(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    budget_sec=10; hold_sec=40
    cmd[cmd.index("-TimeoutSec")+1]=str(budget_sec)
    hold_pid=fixture_tree["root"]/"hold.pid"
    env["MLV_FIXTURE_MCP_LIST_HOLD_PIPE_SECONDS"]=str(hold_sec); env["MLV_FIXTURE_MCP_HOLD_PID"]=str(hold_pid)
    # Files, not pipes: the lingering descendant may inherit the launcher's own handles, and only the
    # launcher's exit time is under test (as in the Promote-CodexPin orphan test).
    out=fixture_tree["root"]/"launcher.out.txt"
    started=time.monotonic()
    try:
        with open(out,"wb") as f:
            r=subprocess.run(cmd,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=hold_sec+30)
    finally:
        if hold_pid.exists():
            subprocess.run(["taskkill","/F","/PID",hold_pid.read_text(encoding="utf-8-sig").strip()],capture_output=True,check=False)
    elapsed=time.monotonic()-started
    assert r.returncode!=0,out.read_text(encoding="utf-8",errors="replace")
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert "codex-mcp-list-unreadable: output streams did not close within" in (q["failure"] or ""), q["failure"]
    assert "a descendant may hold the pipe" in q["failure"], q["failure"]
    assert elapsed<budget_sec+10, f"refusal took {elapsed:.1f}s against a {budget_sec}s probe budget (descendant held the pipe {hold_sec}s)"
    assert not (fixture_tree["root"]/"args.json").exists(), "the codex exec lane must never start"


# (b) a stream larger than the 1 MiB-character cap is refused by name, never truncated and parsed.
def test_codex_read_only_lane_refuses_launch_when_mcp_list_output_exceeds_the_cap(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    listing=fixture_tree["root"]/"mcp-list.json"
    listing.write_text("["+" "*(1048576+1024)+"]",encoding="utf-8")   # valid JSON array, but over the cap
    env["MLV_FIXTURE_MCP_LIST_OUTPUT"]=str(listing); env["MLV_FIXTURE_MCP_LIST_EXIT"]="0"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=60)
    assert r.returncode!=0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="failed"
    assert "codex-mcp-list-unreadable: 'codex mcp list --json' stdout exceeded the 1048576 character cap" in (q["failure"] or ""), q["failure"]
    assert not (fixture_tree["root"]/"args.json").exists(), "the codex exec lane must never start"


# CODEX-KEY-PRIVATE-PIN-1: a read-only codex lane runs the private install named by the tracked
# codex-pin.json (beside the launcher, i.e. the fixture root here) under -CodexPinRoot, falling back
# to the global codex with a named reason. prepare() swaps $CODEX_EXE for the shim, which by design
# never consults the pin (PIN_SKIPPED_EXE_OVERRIDDEN); these tests also point $REAL_CODEX_EXE at the
# shim -- exactly as test_real_lane_launch_refuses_non_high_effort does for claude -- so the launcher
# treats the shim as its genuine global codex, and drop prepare()'s -ReasoningEffort low, which that
# same real-launch rule refuses.
PIN_VERSION = "9.9.9-fixture"


def _real_codex_is_shim(fixture_tree):
    def mutation(text):
        old = "$REAL_CODEX_EXE  = Join-Path $env:APPDATA 'npm\\codex.cmd'"
        assert text.count(old) == 1, "REAL_CODEX_EXE declaration has moved; update this fixture mutation"
        return text.replace(old, "$REAL_CODEX_EXE = '" + str(fixture_tree["shim"]).replace("'", "''") + "'")
    return mutation


def _pin_case(fixture_tree, pin=None, exe_version=PIN_VERSION, create_exe=True, real_is_shim=True,
              editing=False, keep_low_effort=False):
    root = fixture_tree["root"]; pin_root = root / "codex-pin"
    pinned = pin_root / PIN_VERSION / "node_modules" / ".bin" / "codex.cmd"
    if create_exe:
        pinned.parent.mkdir(parents=True)
        pinned.write_text("@echo off\r\n"
                          f'if "%~1"=="--version" (echo codex-cli {exe_version}& exit /b 0)\r\n'
                          'echo pinned> "%MLV_FIXTURE_VIA%"\r\n'
                          f'"{PWSH}" -NoProfile -NonInteractive -File "{fixture_tree["child"]}" %*\r\n', encoding="ascii")
    if pin is None:
        pin = {"schema": "mlv-app/codex-pin/v1", "version": PIN_VERSION, "windowsSandbox": "elevated"}
    mutations = []
    if real_is_shim: mutations.append(_real_codex_is_shim(fixture_tree))
    if editing: mutations.append(_bypass_codex_never_edits)
    def mutation(text):
        for m in mutations: text = m(text)
        return text
    cmd, env, receipt = prepare(fixture_tree, "normal", lane="sol", mutation=mutation, editing=editing,
                                allowed_tools="Read,Write" if editing else "")
    if pin is not False:
        (root / "codex-pin.json").write_text(pin if isinstance(pin, str) else json.dumps(pin), encoding="utf-8")
    if not keep_low_effort:
        i = cmd.index("-ReasoningEffort"); del cmd[i:i+2]
    cmd += ["-CodexPinRoot", str(pin_root)]
    env = dict(env); env["MLV_FIXTURE_VIA"] = str(root / "via.txt")
    return cmd, env, receipt, pinned


def _run_pin_case(cmd, env, receipt):
    r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=40)
    return r, json.loads(receipt.read_text(encoding="utf-8"))


def test_codex_pin_present_and_matching_runs_the_pinned_exe_on_its_promoted_sandbox(fixture_tree):
    cmd, env, receipt, pinned = _pin_case(fixture_tree)
    r, q = _run_pin_case(cmd, env, receipt)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (fixture_tree["root"] / "via.txt").exists(), "the lane must have been launched through the pinned exe"
    assert q["codexExe"]["pinState"] == "PINNED"
    assert q["codexExe"]["path"] == str(pinned)
    assert q["codexExe"]["version"] == PIN_VERSION and q["codexExe"]["pinVersion"] == PIN_VERSION
    assert q["codexExe"]["pinWindowsSandbox"] == "elevated"
    argv = json.loads((fixture_tree["root"] / "args.json").read_text(encoding="utf-8-sig"))
    assert _windows_sandbox_overrides(argv) in (['windows.sandbox="elevated"'], ["windows.sandbox=elevated"]), argv
    assert q["authority"]["windowsSandbox"] == "elevated"
    assert q["authority"]["mcpServers"] == "disabled", "the MCP probe must still run, on the pinned exe"
    assert r.stdout.count("[codex-pin]") == 1 and "[codex-pin] PINNED" in r.stdout, r.stdout


@pytest.mark.parametrize("case,state", [
    ("no-pin", "PIN_ABSENT"),
    ("exe-missing", "PIN_EXE_MISSING"),
    ("version-mismatch", "PIN_VERSION_MISMATCH"),
    ("unparseable-pin", "PIN_INVALID"),
    ("path-in-version", "PIN_INVALID"),
    ("bad-sandbox", "PIN_INVALID"),
    # PR #339 r2 (CODEX-PIN-CASE-EXACT-1): pin validation is case-exact.
    ("sandbox-case", "PIN_INVALID"),
    ("version-case", "PIN_INVALID"),
])
def test_codex_pin_unusable_falls_back_to_the_global_codex_with_the_named_reason(fixture_tree, case, state):
    kw = {"no-pin": dict(pin=False), "exe-missing": dict(create_exe=False),
          "version-mismatch": dict(exe_version="9.9.8-fixture"), "unparseable-pin": dict(pin="{not json"),
          "path-in-version": dict(pin={"version": "..\\..\\evil", "windowsSandbox": "elevated"}),
          "bad-sandbox": dict(pin={"version": PIN_VERSION, "windowsSandbox": "danger-full-access"}),
          "sandbox-case": dict(pin={"version": PIN_VERSION, "windowsSandbox": "Elevated"}),
          # the exe directory is found case-insensitively and reports PIN_VERSION's own spelling
          "version-case": dict(pin={"version": PIN_VERSION.upper(), "windowsSandbox": "elevated"})}[case]
    cmd, env, receipt, pinned = _pin_case(fixture_tree, **kw)
    env["MLV_FIXTURE_CODEX_VERSION"] = "1.2.3"
    r, q = _run_pin_case(cmd, env, receipt)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not (fixture_tree["root"] / "via.txt").exists(), "the pinned exe must not run"
    assert q["codexExe"]["pinState"] == state, q["codexExe"]
    assert q["codexExe"]["path"] == str(fixture_tree["shim"])
    assert q["codexExe"]["version"] == "1.2.3", "the global codex's own version is stamped on a fallback"
    assert q["authority"]["windowsSandbox"] == "unelevated", "a fallback keeps #329's unelevated sandbox"
    lines = [l for l in r.stdout.splitlines() if "[codex-pin]" in l]
    assert len(lines) == 1 and state in lines[0], r.stdout


def test_codex_pin_keeps_the_high_effort_refusal_for_the_pinned_exe(fixture_tree):
    # The pinned exe IS a real CLI: $REAL_CODEX_EXE follows it, so a non-high effort is refused
    # exactly as for the global codex, before the ledger row or any provider process.
    cmd, env, receipt, pinned = _pin_case(fixture_tree, keep_low_effort=True)
    r, q = _run_pin_case(cmd, env, receipt)
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert q["state"] == "failed" and "lane-effort-must-be-high" in (q["failure"] or ""), q["failure"]
    assert q["codexExe"]["pinState"] == "PINNED"
    assert q["dispatchLedger"]["state"] is None
    assert not (fixture_tree["root"] / "args.json").exists()


def test_codex_pin_is_never_consulted_by_a_launcher_whose_exe_was_swapped(fixture_tree):
    # A valid pin is present, but $CODEX_EXE no longer equals $REAL_CODEX_EXE (prepare()'s own shim
    # swap): the swapped exe runs, the pin is not promoted over it, and the low-effort launch is
    # still allowed only because the swapped exe provably cannot be a real CLI.
    cmd, env, receipt, pinned = _pin_case(fixture_tree, real_is_shim=False, keep_low_effort=True)
    r, q = _run_pin_case(cmd, env, receipt)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not (fixture_tree["root"] / "via.txt").exists()
    assert q["codexExe"]["pinState"] == "PIN_SKIPPED_EXE_OVERRIDDEN"
    assert q["codexExe"]["path"] == str(fixture_tree["shim"])


def test_codex_pin_does_not_apply_to_a_producer_lane(fixture_tree):
    cmd, env, receipt, pinned = _pin_case(fixture_tree, editing=True)
    r, q = _run_pin_case(cmd, env, receipt)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not (fixture_tree["root"] / "via.txt").exists()
    assert q["codexExe"]["pinState"] == "PIN_NOT_APPLICABLE"
    assert q["authority"]["windowsSandbox"] == "default"


def test_codex_pin_rule_is_shared_by_the_promoter_and_the_tracked_pin_is_well_formed():
    # Promote-CodexPin.ps1 installs where Invoke-Lane.ps1 looks: both derive the exe from the same
    # two literals. The tracked pin, when present, must pass the launcher's own validation.
    promoter = (ROOT / "tools" / "coordination" / "Promote-CodexPin.ps1").read_text(encoding="utf-8")
    launcher = CANDIDATE.read_text(encoding="utf-8")
    for name in ("$CODEX_PIN_EXE_RELATIVE", "$CODEX_PIN_VERSION_PATTERN"):
        decl = [l.strip() for l in launcher.splitlines() if l.startswith(name + " =")]
        assert len(decl) == 1, name
        assert decl[0] in [l.strip() for l in promoter.splitlines()], (name, decl[0])
    pin_path = ROOT / "tools" / "coordination" / "codex-pin.json"
    if pin_path.exists():
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?", pin["version"]), pin
        assert pin["windowsSandbox"] in ("elevated", "unelevated"), pin


# PR #339 r2: Promote-CodexPin.ps1 against a fake private codex (no npm, no tokens). The fake
# answers --version, `sandbox ... -- git ...`, `mcp list` and `exec` (an EXEC_OK transcript); the
# sandboxed write probe's behaviour comes from MLV_FAKE_CODEX_WRITE. The promoter's positive
# control is the REAL git on PATH, run in the same environment.
PROMOTER = ROOT / "tools" / "coordination" / "Promote-CodexPin.ps1"
FAKE_CODEX_PS1 = r'''$ErrorActionPreference='Stop'
$a=@($args)
if($a.Count -ge 1 -and $a[0] -eq '--version'){[Console]::Out.Write("codex-cli $env:MLV_FAKE_CODEX_VERSION`n"); exit 0}
if($a[0] -eq 'sandbox'){
  $i=[array]::IndexOf($a,'--'); $cmd=@($a[($i+1)..($a.Count-1)])
  if($cmd[1] -eq '--version'){ & git --version; exit $LASTEXITCODE }
  switch($env:MLV_FAKE_CODEX_WRITE){
    'passthrough' { & $cmd[0] @($cmd[1..($cmd.Count-1)]); exit $LASTEXITCODE }
    'deny' { [Console]::Error.WriteLine("fatal: cannot mkdir $($cmd[-1]): Permission denied"); exit 128 }
    'nosig' { [Console]::Error.WriteLine('fatal: the sandboxed process could not be started'); exit 1 }
    'hang' { exit 77 }
    'orphan-hang' {
      # a grandchild that inherits the stdout/stderr pipes and outlives this process, so the
      # promoter's Kill(true) of the shim's tree cannot reach it
      $psi=[System.Diagnostics.ProcessStartInfo]::new('pwsh.exe'); $psi.UseShellExecute=$false
      foreach($x in @('-NoProfile','-NonInteractive','-Command',"Set-Content -LiteralPath '$env:MLV_FAKE_CODEX_ORPHAN_PID' -Value `$PID; Start-Sleep -Seconds 120")){[void]$psi.ArgumentList.Add($x)}
      [void][System.Diagnostics.Process]::Start($psi)
      $end=(Get-Date).AddSeconds(30); while(-not (Test-Path -LiteralPath $env:MLV_FAKE_CODEX_ORPHAN_PID) -and (Get-Date) -lt $end){Start-Sleep -Milliseconds 100}
      exit 77
    }
  }
  exit 98
}
if($a[0] -eq 'mcp'){[Console]::Out.Write('[]'); exit 0}
if($a[0] -eq 'exec'){
  $nonce=[regex]::Match([Console]::In.ReadToEnd(),'PINPROBE[0-9a-f]+').Value
  Set-Content -LiteralPath $a[[array]::IndexOf($a,'-o')+1] -Value $nonce
  [Console]::Error.WriteLine('exec'); [Console]::Error.WriteLine(' succeeded in 5ms:'); [Console]::Out.Write("$nonce`n"); exit 0
}
exit 99
'''


def _promote(tmp_path, write_mode, extra_env=None, probe_timeout=60, timeout=120):
    pin_root = tmp_path / "pin-root"; bin_dir = pin_root / PIN_VERSION / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    fake = tmp_path / "fake-codex.ps1"; fake.write_text(FAKE_CODEX_PS1, encoding="ascii")
    # exit 77 = "stay alive": the shim then sleeps so the promoter's probe timeout fires
    (bin_dir / "codex.cmd").write_text(
        "@echo off\r\n"
        f'"{PWSH}" -NoProfile -NonInteractive -File "{fake}" %*\r\n'
        f'if errorlevel 77 if not errorlevel 78 "{PWSH}" -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 120"\r\n'
        "exit /b %errorlevel%\r\n", encoding="ascii")
    work = tmp_path / "work"; work.mkdir(); ev = tmp_path / "evidence"; pin_file = tmp_path / "codex-pin.json"
    env = dict(os.environ); env.update(extra_env or {})
    env.update(MLV_FAKE_CODEX_VERSION=PIN_VERSION, MLV_FAKE_CODEX_WRITE=write_mode,
               MLV_FAKE_CODEX_ORPHAN_PID=str(tmp_path / "orphan.pid"))
    cmd = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(PROMOTER), "-Version", PIN_VERSION,
           "-PinRoot", str(pin_root), "-PinFile", str(pin_file), "-WorkDir", str(work), "-EvidenceDir", str(ev),
           "-WindowsSandbox", "elevated", "-Model", "fake-model", "-ProbeTimeoutSec", str(probe_timeout)]
    # Files, not pipes: an orphan that inherited the promoter's handles must not hold the test's own
    # capture open (only the promoter's exit is under test).
    out = tmp_path / "promote.out.txt"; orphan = tmp_path / "orphan.pid"
    started = time.monotonic()
    try:
        with open(out, "wb") as f:
            r = subprocess.run(cmd, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
    finally:
        if orphan.exists():
            subprocess.run(["taskkill", "/F", "/PID", orphan.read_text(encoding="utf-8-sig").strip()],
                           capture_output=True, check=False)
    elapsed = time.monotonic() - started
    evidence = json.loads((ev / "promotion.json").read_text(encoding="utf-8"))
    step = {s["name"]: s for s in evidence["steps"]}
    return r.returncode, out.read_text(encoding="utf-8", errors="replace"), evidence, step, pin_file, elapsed


def _assert_unproven(code, log, evidence, step, pin_file, reasons):
    assert code == 19, log
    wp = step["write-probe"]
    assert wp["outcome"].startswith("UNPROVEN"), wp["outcome"]
    assert "DENIED" not in wp["outcome"], wp["outcome"]
    assert sorted(wp["unprovenReasons"]) == sorted(reasons), wp
    assert evidence["exitCode"] == 19 and "exec-probe" not in step, "promotion must stop at the write probe"
    assert not pin_file.exists(), "no pin may be written"


def test_codex_pin_promoter_config_failure_is_not_a_denial(tmp_path):
    # sol's repro: GIT_CONFIG_COUNT=bogus -> `git --version` rc 0 but `git init` rc 128 on a config
    # parse error, nothing created. Both the signature and the control refuse DENIED.
    code, log, ev, step, pin, _ = _promote(tmp_path, "passthrough", {"GIT_CONFIG_COUNT": "bogus"})
    assert step["write-probe"]["exit"] == 128 and "GIT_CONFIG_COUNT" in step["write-probe"]["stderrTail"], step["write-probe"]
    _assert_unproven(code, log, ev, step, pin, ["control-failed", "no-denial-signature"])


def test_codex_pin_promoter_denial_without_a_working_control_is_unproven(tmp_path):
    # a denial-shaped stderr, but the un-sandboxed control cannot `git init` in this environment
    code, log, ev, step, pin, _ = _promote(tmp_path, "deny", {"GIT_CONFIG_COUNT": "bogus"})
    assert step["write-probe"]["denialSignature"] == "Permission denied"
    assert step["write-probe"]["control"]["exit"] == 128 and step["write-probe"]["control"]["created"] is False
    _assert_unproven(code, log, ev, step, pin, ["control-failed"])


def test_codex_pin_promoter_refusal_without_a_denial_signature_is_unproven(tmp_path):
    code, log, ev, step, pin, _ = _promote(tmp_path, "nosig")
    assert step["write-probe"]["control"]["exit"] == 0 and step["write-probe"]["control"]["created"] is True
    _assert_unproven(code, log, ev, step, pin, ["no-denial-signature"])


def test_codex_pin_promoter_timed_out_write_probe_is_unproven(tmp_path):
    code, log, ev, step, pin, _ = _promote(tmp_path, "hang", probe_timeout=8)
    assert step["write-probe"]["timedOut"] is True
    _assert_unproven(code, log, ev, step, pin, ["timed-out", "no-denial-signature"])


def test_codex_pin_promoter_genuine_denial_with_control_promotes(tmp_path):
    code, log, ev, step, pin, _ = _promote(tmp_path, "deny")
    assert code == 0, log
    wp = step["write-probe"]
    assert wp["outcome"].startswith("DENIED") and wp["unprovenReasons"] == [], wp
    assert wp["denialSignature"] == "Permission denied"
    assert wp["control"]["exit"] == 0 and wp["control"]["created"] is True and wp["control"]["timedOut"] is False
    written = json.loads(pin.read_text(encoding="utf-8"))
    assert written["windowsSandbox"] == "elevated" and written["version"] == PIN_VERSION
    assert "Permission denied" in written["gate"]["writeProbe"] and "control" in written["gate"]["writeProbe"]


def test_codex_pin_promoter_child_holding_the_pipes_after_timeout_fails_within_budget(tmp_path):
    # CODEX-PIN-PROMOTION-BOUNDS-1: after the write probe times out, Kill(true) cannot reach a
    # grandchild that escaped the tree and holds the stdout/stderr pipes. The drain must give up
    # within the cleanup budget (10 s) with a named reason, never hang.
    code, log, ev, step, pin, elapsed = _promote(tmp_path, "orphan-hang", probe_timeout=8, timeout=90)
    assert code == 20, log
    wp = step["write-probe"]
    assert wp["outcome"] == "FAILED: cleanup stream-drain-expired", wp
    assert wp["cleanupFailure"] == "stream-drain-expired" and wp["timedOut"] is True
    assert "stream-drain-expired" in ev["result"] and not pin.exists()
    assert elapsed < 60, elapsed


def test_claude_lane_receipt_carries_a_null_codex_exe(fixture_tree):
    cmd, env, receipt = prepare(fixture_tree, "normal")
    r = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=20)
    assert r.returncode == 0, (r.stdout, r.stderr)
    q = json.loads(receipt.read_text(encoding="utf-8"))
    assert "codexExe" in q and q["codexExe"] is None
    assert "[codex-pin]" not in r.stdout


def test_startup_consumes_same_deadline_without_starting_provider(fixture_tree):
    def delay(text):
        return text.replace("$line=[Console]::In.ReadLine(); if([string]::IsNullOrWhiteSpace($line)){throw 'launch-frame-missing'}", "Start-Sleep -Seconds 8\n$line=[Console]::In.ReadLine(); if([string]::IsNullOrWhiteSpace($line)){throw 'launch-frame-missing'}")
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=delay)
    cmd[cmd.index("-TimeoutSec")+1]="1"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==124,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["timedOut"]
    assert not (fixture_tree["root"]/"child.json").exists()
    # PR #105 round 2 (sol blocker): Invoke-Lane.ps1 now records containedHost.pid
    # the instant Process::Start returns, before any call that can throw -- so a
    # null ownerPid means no host exists (either the launch budget was already
    # gone, or Start itself threw). This fixture's delayed stdin read happens
    # AFTER a successful Start, so it must land in the "owner present" branch;
    # ownerPid==None here would itself be the round-2 regression.
    #
    # PR #105 round 3: a null ownerPid alone still can't tell "budget exhausted
    # before Start was ever called" (Invoke-Lane.ps1:470-472, legitimate) apart
    # from an ambiguous, unexplained absence. containment.ownerAbsentReason
    # (Invoke-Lane.ps1's catch block, ~661-676) now names WHY, so classify on
    # that instead of guessing from ownerPid alone.
    containment=q.get("containment")
    owner_pid=containment.get("ownerPid") if containment else None
    if containment is not None and owner_pid is not None:
        # Unchanged from round 2: a pid was recorded, so a host definitely exists
        # (or existed) and must be reaped.
        if containment["ownerCreatedUtc"] is None:
            # A pid with no createdUtc means Start succeeded but StartTime read threw;
            # there is no createdUtc to compare against, so the only provable check is
            # that the pid is not (or no longer) an alive process.
            assert identity(owner_pid) is None
        else:
            wait_absent({"pid":q["containment"]["ownerPid"],"createdUtc":q["containment"]["ownerCreatedUtc"]})
    else:
        # PR #105 round 4: ownerAbsentReason must be one of the closed-set no-host
        # tokens, never an arbitrary truthy string (round 3's "if reason: pass" let
        # a POST_START_UNRECORDED-shaped failure pose as a legitimate absence).
        assert_owner_absence_is_legitimate(containment, q)


def test_zero_timeout_exhausts_budget_before_spawn_and_names_the_reason(fixture_tree):
    # Reachability proof for the legitimate null-owner branch (PR #105 round 3,
    # task item 4): -TimeoutSec 0 means the budget is already spent by the time
    # execution reaches Invoke-Lane.ps1:470, so that line's throw fires BEFORE
    # Process::Start is ever called -- no mutation/mock needed, this is the real
    # code path. No host exists, so ownerPid must be null with ownerAbsentReason
    # naming why.
    cmd,env,receipt=prepare(fixture_tree,"normal")
    cmd[cmd.index("-TimeoutSec")+1]="0"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==124,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["timedOut"]
    assert not (fixture_tree["root"]/"child.json").exists()
    containment=q["containment"]
    assert containment["ownerPid"] is None
    assert containment["ownerAbsentReason"]=="launch-budget-exhausted"


def test_start_threw_is_classified_as_no_host(fixture_tree):
    # Reachability proof for the other no-host token (PR #105 round 4): make
    # Process::Start itself throw for the claude engine. $hostStarted is never set
    # (it is assigned on the line immediately AFTER Start returns), so the catch
    # block must land in the "Start never returned" branch and pick the generic
    # start-threw token, not the budget token (this is not a TimeoutException) and
    # not POST_START_UNRECORDED (no host ever existed).
    def break_start(text):
        old = "$proc = [Diagnostics.Process]::Start($psi)"
        assert text.count(old) == 2
        # Replace ONLY the first occurrence -- the claude-engine branch (~line 517),
        # which runs before $hostStarted is set. The second occurrence is the
        # non-claude branch and must stay untouched.
        return text.replace(old, "throw 'fixture-start-threw'", 1)
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=break_start)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==127,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    containment=q["containment"]
    assert containment["ownerPid"] is None
    assert containment["ownerAbsentReason"]=="start-threw"
    assert containment["ownerAbsentDetail"]=="fixture-start-threw"
    assert_owner_absence_is_legitimate(containment, q)


def test_post_start_unrecorded_is_named_not_hidden(fixture_tree):
    # Reachability proof for the genuinely-ambiguous branch (PR #105 round 4, task
    # item 4): inject a throw between Process::Start returning (a host now EXISTS)
    # and $containedHost being built, using the same fixture-mutation mechanism
    # test_setup_origin_and_expired_budget_are_deterministic already uses to inject
    # text at a specific line. This is exactly the failure the cross-family review
    # found: without $hostStarted, this exception's message would pose as a
    # legitimate no-host reason. With it, the catch block must name it
    # POST_START_UNRECORDED instead -- and the test below must FAIL LOUD on that
    # receipt if a caller naively treated it as legitimate (proven by calling the
    # shared assertion helper and expecting it to raise).
    #
    # PR #105 round 5 (this packet): $hostPid is now captured on its own
    # non-throwing line BEFORE the $containedHost build that this fixture breaks,
    # so a post-start-unrecorded receipt must carry the REAL host pid, not null --
    # a null ownerPid here would itself be the round-5 regression, since the pid
    # is the only channel by which anyone later learns the orphan existed. The
    # mutation also drops a marker file with $hostPid's value (via the same
    # Write-Utf8NoBom helper the production code already uses) so the test can
    # assert the receipt's ownerPid equals the REAL pid, not merely "non-null".
    marker = fixture_tree["root"] / "host-pid.txt"
    def break_containedHost_build(text):
        old = "$containedHost = [ordered]@{ pid=$hostPid; createdUtc=$null }"
        assert text.count(old) == 1
        marker_literal = str(marker).replace("'", "''")
        return text.replace(old, "Write-Utf8NoBom '%s' ([string]$hostPid)\n    throw 'fixture-post-start-unrecorded'" % marker_literal)
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=break_containedHost_build)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==127,(r.stdout,r.stderr)
    assert not (fixture_tree["root"]/"child.json").exists()
    q=json.loads(receipt.read_text(encoding="utf-8"))
    containment=q["containment"]
    real_pid = int(marker.read_text(encoding="utf-8-sig").strip())
    assert containment["ownerPid"] == real_pid
    assert containment["ownerAbsentReason"]==POST_START_UNRECORDED
    assert containment["ownerAbsentDetail"]=="fixture-post-start-unrecorded"
    assert containment["ownerAbsentReason"] not in NO_HOST_TOKENS
    # The whole point: this receipt must NOT be accepted as a legitimate absence,
    # even though a pid is now present.
    with pytest.raises(pytest.fail.Exception):
        assert_owner_absence_is_legitimate(containment, q)


def test_pre_assignment_kill_failure_is_recorded_not_swallowed(fixture_tree):
    # Falsifier for the OTHER half of this packet (PR #105 round 5, sol PR #105
    # blocker): at the pre-assignment site (Invoke-Lane.ps1 ~line 674-696), the
    # host is still OUTSIDE the job, so a swallowed Kill failure there leaves a
    # GENUINE orphan -- unlike the post-timeout kill at ~line 602, where the job
    # is kill-on-close and the tree is already terminated. Combine the same
    # post-start-unrecorded trigger (so the pre-assignment kill path is reached
    # at all: $jobAssigned is still false) with a forced Kill failure, and prove
    # the receipt records ownerKillAttempted/ownerKillOutcome instead of the bare
    # `catch { }` this repo used to have there silently discarding it.
    def break_kill(text):
        old_throw = "$containedHost = [ordered]@{ pid=$hostPid; createdUtc=$null }"
        assert text.count(old_throw) == 1
        text = text.replace(old_throw, "throw 'fixture-post-start-unrecorded'")
        # 1b4a82ab split the old single `Kill($true); [void]WaitForExit(5000)`
        # statement into two lines so WaitForExit's bool return could be
        # captured instead of discarded -- the anchor now spans both lines.
        # `$proc.Kill($true)` alone is NOT unique (the post-timeout kill at
        # ~line 626 also calls it), so the second line's exact indentation is
        # part of the anchor, same discipline as every other anchor here.
        old_kill = "$proc.Kill($true)\n                $exitedWithinWait = $proc.WaitForExit(5000)"
        assert text.count(old_kill) == 1
        return text.replace(old_kill, "throw 'fixture-kill-failed'")
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=break_kill)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==127,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    containment=q["containment"]
    # The whole point: the failed kill is VISIBLE, never silent.
    assert containment["ownerKillAttempted"] is True
    assert containment["ownerKillOutcome"]=="kill-threw"
    assert containment["ownerKillDetail"]=="fixture-kill-failed"
    # Forcing the kill to throw means the real kill never ran -- this test, not
    # production code, is responsible for reaping the host it just orphaned.
    # ownerPid is guaranteed non-null by this same packet's other remedy.
    owner_pid = containment["ownerPid"]
    assert owner_pid is not None
    subprocess.run([PWSH,"-NoProfile","-NonInteractive","-Command",
                     f"Stop-Process -Id {int(owner_pid)} -Force -ErrorAction SilentlyContinue"],
                    timeout=PWSH_PROBE_TIMEOUT_SEC, check=False)


def test_pre_assignment_kill_wait_timeout_is_recorded_not_killed(fixture_tree):
    # Falsifier for PR #105 final (cross-family review of 0f8ba40a): WaitForExit(Int32)
    # RETURNS a bool -- true iff the process exited within the timeout -- and round 5
    # discarded that return with [void], recording 'killed' unconditionally. A host
    # that outlives the bounded wait -- exactly the orphan this whole change exists to
    # make visible -- was therefore reported as killed: manufactured evidence, worse
    # than the bare `catch { }` this whole packet replaced.
    #
    # PR #105 round 6 (falsifier hardening, 2026-09-09): the prior construction shrank
    # the real wait to WaitForExit(0), racing a real WaitForExit call against real OS
    # process teardown on the bet that 0ms wouldn't be enough time for the process to
    # actually exit. It was not reliable: reproduced 3 of 3 in isolation on this host
    # (36 processes, 27% CPU -- well below saturation) as an ASSERTION failure, not a
    # timeout, because WaitForExit(0) sometimes observed the process as already gone.
    # A falsifier for "false observations are classified correctly" that can itself
    # observe true is not a proof.
    #
    # Constructing a real process that genuinely SURVIVES Process.Kill(entireProcessTree:
    # true) plus a real bounded wait is not achievable on demand on this platform --
    # TerminateProcess cannot be caught, ignored, or reliably outlasted by the target,
    # so there is no cheap, reliable way to make a real teardown race land on the false
    # branch every time. Per this packet's own instructions, an unreliable variant is
    # worse than no variant (a 3-of-3-failing test teaches a reader to ignore red), so
    # instead of shrinking the wait, this test now mutates the runner's own source to
    # force the OBSERVED boolean itself to $false -- the same fixture-mutation
    # discipline every other test in this file already uses to reach its own branch
    # (see test_start_threw_is_classified_as_no_host,
    # test_post_start_unrecorded_is_named_not_hidden). The real Kill($true) call is
    # left untouched; only the captured WaitForExit result is forced.
    #
    # WHAT THIS PROVES: the CLASSIFICATION LOGIC -- that a false WaitForExit observation
    # is recorded as 'kill-wait-timeout' and is never silently upgraded to 'killed'.
    # WHAT THIS DOES NOT PROVE: that a real process can outlive a real Kill($true) plus
    # a real five-second WaitForExit on this platform. Whether a genuinely slow-to-die
    # host is always caught inside a realistic multi-second window is a timing property
    # of the OS, not a property of this code path, and is not exercised here.
    def force_wait_false(text):
        old = "$exitedWithinWait = $proc.WaitForExit(5000)"
        assert text.count(old) == 1
        return text.replace(old, "$exitedWithinWait = $false")
    cmd,env,receipt=prepare(fixture_tree,"normal",assignment_failure=True,mutation=force_wait_false)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==127,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    containment=q["containment"]
    # The whole point: an observed-not-exited result must never be reported as killed.
    assert containment["ownerKillAttempted"] is True
    assert containment["ownerKillOutcome"]=="kill-wait-timeout", (
        f"expected the observed-timeout token, got {containment['ownerKillOutcome']!r} -- "
        "this is the exact false-evidence defect this test exists to catch"
    )
    # Kill() itself was real and unmodified (only the captured wait result is forced),
    # so the host is gone or about to be -- reap defensively like the sibling
    # kill-threw test does.
    owner_pid = containment["ownerPid"]
    assert owner_pid is not None
    subprocess.run([PWSH,"-NoProfile","-NonInteractive","-Command",
                     f"Stop-Process -Id {int(owner_pid)} -Force -ErrorAction SilentlyContinue"],
                    timeout=PWSH_PROBE_TIMEOUT_SEC, check=False)


def test_kill_outcome_token_set_matches_producer_constants():
    # Guard against the exact drift the cross-family review flagged: this file's
    # KILL_OUTCOME_TOKENS is a hand-copy of Invoke-Lane.ps1's $OWNER_KILL_OUTCOME_*
    # constants because this file cannot import a .ps1. Pin the full set against the
    # source directly so an added/renamed/removed token fails this test loudly
    # instead of only failing closed by accident via an exact-string assertion
    # elsewhere.
    text = CANDIDATE.read_text(encoding="utf-8")
    found = set(re.findall(r"\$OWNER_KILL_OUTCOME_\w+\s*=\s*'([^']+)'", text))
    assert found == KILL_OUTCOME_TOKENS, f"producer constants {found!r} != pinned set {KILL_OUTCOME_TOKENS!r}"


def test_owner_absence_helper_fails_loud_on_post_start_or_unrecognised_tokens():
    # Prove the test-side guardrail itself is reachable and fires (not just
    # written): both the named-ambiguous token and a wholly unrecognised string
    # must be rejected, never silently tolerated the way round 3's bare
    # "if reason: pass" tolerated any truthy string.
    for reason in (POST_START_UNRECORDED, "something-unrecognised", None):
        with pytest.raises(pytest.fail.Exception):
            assert_owner_absence_is_legitimate({"ownerPid": None, "ownerAbsentReason": reason}, {"case": reason})
    # And the closed set itself must still pass.
    for reason in NO_HOST_TOKENS:
        assert_owner_absence_is_legitimate({"ownerPid": None, "ownerAbsentReason": reason}, {"case": reason})


@pytest.mark.parametrize("elapsed_ms,expected_exit", [(0, 0), (4000, 124)])
def test_setup_origin_and_expired_budget_are_deterministic(fixture_tree, elapsed_ms, expected_exit):
    marker = fixture_tree["root"] / "start-attempt.txt"
    def clocks(text):
        text = text.replace("$startedUtc = (Get-Date).ToUniversalTime()",
            "$startedUtc = [datetime]::Parse('2000-01-01T00:00:00Z').ToUniversalTime()", 1)
        old = "$sw         = [System.Diagnostics.Stopwatch]::StartNew()"
        assert text.count(old) == 1
        text = text.replace(old, "$sw = [pscustomobject]@{Elapsed=[timespan]::FromMilliseconds(%d)}\n$sw | Add-Member ScriptMethod Stop {}" % elapsed_ms)
        old = "$proc = [Diagnostics.Process]::Start($psi)"
        assert text.count(old) == 2
        return text.replace(old, "Write-Utf8NoBom '%s' 'start'\n    %s" % (str(marker).replace("'", "''"), old))
    cmd, env, receipt = prepare(fixture_tree, "normal", mutation=clocks)
    cmd[cmd.index("-TimeoutSec") + 1] = "3"
    result = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == expected_exit, (result.stdout, result.stderr)
    assert marker.exists() == (expected_exit == 0)
    q = json.loads(receipt.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(q["startedUtc"]) == datetime.fromisoformat("2000-01-01T00:00:00+00:00")
    assert datetime.fromisoformat(q["containment"]["deadlineUtc"]) == datetime.fromisoformat("2000-01-01T00:00:03+00:00")
    assert q["timedOut"] == (expected_exit == 124)
    if expected_exit == 124:
        assert not q["containment"]["jobAssigned"]
        assert q["containment"]["ownerPid"] is None
        # PR #105 round 3: this parametrization mocks $sw.Elapsed to already exceed
        # the budget, so Invoke-Lane.ps1:470-472 throws before $proc = ...Start()
        # (proven by marker.exists() is False above) -- the same code path
        # -TimeoutSec 0 reproduces for real in
        # test_zero_timeout_exhausts_budget_before_spawn_and_names_the_reason.
        assert q["containment"]["ownerAbsentReason"]=="launch-budget-exhausted"
        assert not (fixture_tree["root"] / "child.json").exists()


def _fail_final_receipt_write(text):
    old='Write-Utf8NoBomAtomic $rcptPath (($receipt | ConvertTo-Json -Depth 6))'
    assert text.count(old)==1
    return text.replace(old,"throw 'fixture-final-receipt-write-failed'")


# CI-FLAKE-LANE-CONTAINMENT-STARTUP-BUDGET-1: a fixed 3 s lane deadline also had to cover two nested pwsh cold
# starts, so a loaded host killed the tree before the grandchild wrote grand.json and the test timed out
# waiting for evidence that never existed (PR #303 run 37726495793). The deadline is now started by READINESS:
# the copy of the launcher waits (bounded) for the grandchild's own state file, then lets a 1.5 s deadline run
# out through the unchanged WaitForExit -> $timedOut -> job-close -> final-write path.
def _deadline_after_descendants_ready(text):
    old='$remainingMs = [math]::Max(0, [math]::Floor(($TimeoutSec * 1000.0) - $sw.Elapsed.TotalMilliseconds))'
    assert text.count(old)==1
    ready=("$readyWatch = [Diagnostics.Stopwatch]::StartNew(); "
           "while (-not (Test-Path -LiteralPath $env:MLV_FIXTURE_GRAND) -and $readyWatch.Elapsed.TotalSeconds -lt 15) { Start-Sleep -Milliseconds 20 }\n"
           "$remainingMs = 1500")
    return text.replace(old,ready)


def prepare_timeout_after_ready(tree, *extra_mutations):
    def mutate(text):
        for m in (_deadline_after_descendants_ready,)+extra_mutations: text=m(text)
        return text
    cmd,env,receipt=prepare(tree,"timeout",mutation=mutate)
    # The wall-clock budget is no longer what triggers the timeout, only the launcher's own pre-wait gates
    # (child start, prompt delivery) read it, so give those the same room every non-timeout fixture run has.
    cmd[cmd.index("-TimeoutSec")+1]="30"
    return cmd,env,receipt


def _final_receipt_io_failure_run(fixture_tree, grand_start_delay_ms=None):
    cmd,env,receipt=prepare_timeout_after_ready(fixture_tree,_fail_final_receipt_write)
    if grand_start_delay_ms is not None: env["MLV_FIXTURE_GRAND_START_DELAY_MS"]=str(grand_start_delay_ms)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode!=0 and 'fixture-final-receipt-write-failed' in r.stderr
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q['state']=='running' and not q['complete']
    wait_absent(wait_json(fixture_tree["root"]/"child.json",encoding="utf-8-sig"))
    wait_absent(wait_json(fixture_tree["root"]/"grand.json",encoding="utf-8-sig"))


def test_final_receipt_io_failure_cannot_keep_descendants_alive(fixture_tree):
    _final_receipt_io_failure_run(fixture_tree)


# CI-FLAKE-LANE-CONTAINMENT-STARTUP-BUDGET-1: the grandchild's start is delayed past the old fixed 3 s lane
# deadline, exactly what a loaded hosted runner's pwsh cold start does.
def test_final_receipt_io_failure_cannot_keep_descendants_alive_when_descendant_start_is_slow(fixture_tree):
    _final_receipt_io_failure_run(fixture_tree, grand_start_delay_ms=5000)


def ledger_rows(root):
    ledger=root/".claude-state"/"coordination"/"dual-lane"/"receipts"/"dispatch-reservations.jsonl"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_direct_launch_writes_a_reserved_versioned_ledger_row_naming_its_receipt(fixture_tree):
    # TOOL-GUARD-COVERAGE-ARM-UNSATISFIABLE-1: a direct Invoke-Lane launch is a dispatch the
    # product-ratio guard must see, so it writes its own 'reserved' row.
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env.pop("MLV_DISPATCH_RESERVATION_ID",None)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=30)
    assert r.returncode==0,(r.stdout,r.stderr)
    rows=ledger_rows(fixture_tree["root"])
    assert len(rows)==1
    row=rows[0]
    assert row["schemaVersion"]==2 and row["venue"]=="invoke-lane" and row["state"]=="reserved"
    assert Path(row["receiptPath"]).resolve()==receipt.resolve()
    assert row["recordedUtc"].endswith("Z") and row["allowEdits"] is False and row["lane"]=="sonnet"
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["dispatchLedger"]=={"state":"reserved","reservationId":row["reservationId"]}


def test_dispatcher_launch_writes_a_linked_row_not_a_second_reservation(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env["MLV_DISPATCH_RESERVATION_ID"]="11111111-2222-3333-4444-555555555555"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=30)
    assert r.returncode==0,(r.stdout,r.stderr)
    rows=ledger_rows(fixture_tree["root"])
    assert [(x["state"],x["reservationId"]) for x in rows]==[("linked","11111111-2222-3333-4444-555555555555")]
    assert Path(rows[0]["receiptPath"]).resolve()==receipt.resolve()


def test_unwritable_ledger_refuses_the_launch_with_a_named_failure(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal")
    env.pop("MLV_DISPATCH_RESERVATION_ID",None)
    # A DIRECTORY where the ledger file should be makes every append fail.
    (fixture_tree["root"]/".claude-state"/"coordination"/"dual-lane"/"receipts"/"dispatch-reservations.jsonl").mkdir(parents=True)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=60)
    assert r.returncode!=0
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["failure"].startswith("dispatch-ledger-write-failed") and not q["complete"]
    assert not (fixture_tree["root"]/"child.json").exists(), "no provider may start without a ledger row"


def _git(root, *args):
    r=subprocess.run(["git","-C",str(root)]+list(args),capture_output=True,text=True,timeout=10)
    assert r.returncode==0,(args,r.stdout,r.stderr)
    return r.stdout


def _seed_git_repo(root):
    _git(root,"init","-q")
    _git(root,"config","user.email","fixture@example.com")
    _git(root,"config","user.name","fixture")
    (root/"tracked.txt").write_text("original\n",encoding="ascii")
    _git(root,"add","tracked.txt")
    _git(root,"commit","-q","-m","seed")


# Round 5 (sol minor 1 / fable minor 1), required cases: inject a git-capture failure at exactly
# the PRE-launch snapshot or exactly the POST-exit snapshot, and prove each is recorded as
# dirtyCheck=='unavailable' without ever flipping the receipt's state -- neither a false
# 'ended-incomplete' nor a silent claim of 'clean'. The injection keys on MLV_FIXTURE_CHILD's
# existence (written as literally the fixture child's first action, before any dirty-file
# simulation runs) rather than a call counter, because that marker is already exactly the
# pre-launch/post-exit boundary this script cares about: the PRE snapshot always runs before the
# child process exists, and the POST snapshot always runs after the child has already exited.
def _inject_git_capture_failure(text):
    marker = "function Invoke-GitCaptureUtf8([string]$WorkDir, [string[]]$GitArgs) {\n"
    assert text.count(marker) == 1, "Invoke-GitCaptureUtf8 signature not found or not unique"
    injected = marker + (
        "    if ($env:MLV_FIXTURE_GIT_FAIL_MODE -and $GitArgs.Count -gt 0 -and $GitArgs[0] -eq 'status') {\n"
        "        $childStarted = Test-Path -LiteralPath $env:MLV_FIXTURE_CHILD\n"
        "        if ((($env:MLV_FIXTURE_GIT_FAIL_MODE -eq 'pre') -and -not $childStarted) -or "
        "(($env:MLV_FIXTURE_GIT_FAIL_MODE -eq 'post') -and $childStarted)) {\n"
        "            return [ordered]@{ ok = $false; stdout = $null; exitCode = $null; error = 'fixture-injected-git-failure' }\n"
        "        }\n"
        "    }\n"
    )
    return text.replace(marker, injected)


def test_pre_launch_git_capture_failure_is_unavailable_and_never_flips_state(fixture_tree):
    # Producer brief round 5, required case 1 (fail-closed git capture): if the PRE-launch
    # tracked-dirt snapshot cannot be taken at all, the check must never fall back to treating
    # that as "nothing was dirty" -- it must record dirtyCheck=='unavailable' and leave the
    # receipt's state exactly as the envelope says (never a false ended-incomplete).
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit",
                             mutation=_inject_git_capture_failure)
    env["MLV_FIXTURE_GIT_FAIL_MODE"]="pre"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["dirtyCheck"]=="unavailable"
    assert "pre-launch snapshot" in q["dirtyCheckReason"]
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


def test_post_exit_git_capture_failure_is_unavailable_and_never_claims_clean(fixture_tree):
    # Producer brief round 5, required case 1: the lane DOES introduce uncommitted tracked dirt
    # (MLV_FIXTURE_DIRTY_TRACKED_PATH), so a working check would flip this to ended-incomplete --
    # but the POST-exit snapshot is the one that fails here, so there is no positive evidence
    # either way. The receipt must show dirtyCheck=='unavailable', never 'clean' (which would be
    # a false claim the tree was actually verified) and never force ended-incomplete (which would
    # be treating an unreachable git as if it were positive evidence of a dirty tree).
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit",
                             mutation=_inject_git_capture_failure)
    env["MLV_FIXTURE_GIT_FAIL_MODE"]="post"
    env["MLV_FIXTURE_DIRTY_TRACKED_PATH"]=str(root/"tracked.txt")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["dirtyCheck"]=="unavailable"
    assert "post-exit snapshot" in q["dirtyCheckReason"]
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


# Round 7 (sol minor): a failing pre- or post- `git rev-parse HEAD` must surface as the same
# 'unavailable' state as a status/hash-object capture failure, not as 'not-applicable' (which
# the old uncaptured-failure code path produced by reading a failed call and a legitimately
# inapplicable gate as the same falsy $BaseSha). Same marker-based pre/post injection as the
# status-capture failure tests above, keyed on GitArgs[0] -eq 'rev-parse' instead of 'status'.
def _inject_git_revparse_failure(text):
    marker = "function Invoke-GitCaptureUtf8([string]$WorkDir, [string[]]$GitArgs) {\n"
    assert text.count(marker) == 1, "Invoke-GitCaptureUtf8 signature not found or not unique"
    injected = marker + (
        "    if ($env:MLV_FIXTURE_GIT_FAIL_MODE -and $GitArgs.Count -gt 0 -and $GitArgs[0] -eq 'rev-parse') {\n"
        "        $childStarted = Test-Path -LiteralPath $env:MLV_FIXTURE_CHILD\n"
        "        if ((($env:MLV_FIXTURE_GIT_FAIL_MODE -eq 'pre') -and -not $childStarted) -or "
        "(($env:MLV_FIXTURE_GIT_FAIL_MODE -eq 'post') -and $childStarted)) {\n"
        "            return [ordered]@{ ok = $false; stdout = $null; exitCode = $null; error = 'fixture-injected-git-revparse-failure' }\n"
        "        }\n"
        "    }\n"
    )
    return text.replace(marker, injected)


def test_pre_launch_revparse_head_failure_is_unavailable_not_not_applicable(fixture_tree):
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit",
                             mutation=_inject_git_revparse_failure)
    env["MLV_FIXTURE_GIT_FAIL_MODE"]="pre"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["dirtyCheck"]=="unavailable"
    assert "pre-launch rev-parse HEAD failed" in q["dirtyCheckReason"]
    assert q["baseSha"] is None


def test_post_exit_revparse_head_failure_is_unavailable_not_head_moved(fixture_tree):
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit",
                             mutation=_inject_git_revparse_failure)
    env["MLV_FIXTURE_GIT_FAIL_MODE"]="post"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["dirtyCheck"]=="unavailable"
    assert "post-exit rev-parse HEAD failed" in q["dirtyCheckReason"]
    assert q["baseSha"] is not None


def test_further_edit_of_pre_dirty_binary_tracked_file_is_ended_incomplete(fixture_tree):
    # Producer brief round 5, required case 2 (content identity): a binary tracked file that was
    # ALREADY dirty before the lane started, and that the lane edits AGAIN with DIFFERENT binary
    # bytes without staging or committing either edit, must still flip the receipt. `git diff
    # HEAD -- <path>` renders any binary difference as the fixed text "Binary files a/<path> and
    # b/<path> differ" -- identical no matter which bytes are actually on disk -- so a content
    # identity built on hashing that diff TEXT (the pre-round-5 mechanism) cannot distinguish the
    # pre-dirty bytes from the lane's further edit at all. `git hash-object` of the working-tree
    # bytes themselves can.
    root=fixture_tree["root"]
    _seed_git_repo(root)
    binary_name="tracked.bin"
    binary_path=root/binary_name
    binary_path.write_bytes(bytes([0x00,0x01,0x02,0x7F,0x80,0xFF]))
    _git(root,"add",binary_name)
    _git(root,"commit","-q","-m","seed binary tracked file")
    # Pre-dirty it, uncommitted, before the lane ever starts -- different bytes than the seed.
    binary_path.write_bytes(bytes([0x00,0xAA,0xBB,0xCC,0xDD,0xFF]))
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    env["MLV_FIXTURE_FURTHER_EDIT_BINARY_TRACKED_PATH"]=str(binary_path)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="ended-incomplete"
    assert q["complete"] is False
    assert q["workEvidence"]["reason"]=="dirty-worktree-no-commit"
    assert q["dirtyCheck"]=="dirty"


# LANE-NO-BACKGROUND-END-TURN-1 round 2 (sol major 1 / fable minor 3): the check is now
# gated to -AllowEdits Claude lanes, and it fires only on tracked dirt the LANE ITSELF
# introduces during its run -- compared against a snapshot taken before the child ever
# starts -- never on dirt that merely pre-exists the worktree. The fixture's fake child
# writes to MLV_FIXTURE_DIRTY_TRACKED_PATH mid-run, without committing, simulating exactly
# the "I'll resume when the background job completes" shape this check exists to catch:
# HEAD never moves and the edit is never committed.
def test_dirty_tracked_worktree_introduced_by_editing_lane_is_ended_incomplete(fixture_tree):
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    env["MLV_FIXTURE_DIRTY_TRACKED_PATH"]=str(root/"tracked.txt")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="ended-incomplete"
    assert q["complete"] is False
    assert q["workEvidence"]["reason"]=="dirty-worktree-no-commit"


def test_further_edit_of_already_dirty_tracked_file_is_ended_incomplete(fixture_tree):
    # Producer brief round 3, required case (sol minor / fable minor 2): a path that was
    # ALREADY dirty before the lane started, and that the lane edits AGAIN without staging or
    # committing either edit, must still flip the receipt -- even though the porcelain status
    # line for that path (' M tracked.txt') never changes text across either edit. Only a
    # per-path content-identity comparison (not a status-line comparison) can see this.
    root=fixture_tree["root"]
    _seed_git_repo(root)
    (root/"tracked.txt").write_text("dirty-before-the-lane-ever-ran\n",encoding="ascii")
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    env["MLV_FIXTURE_FURTHER_EDIT_TRACKED_PATH"]=str(root/"tracked.txt")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="ended-incomplete"
    assert q["complete"] is False
    assert q["workEvidence"]["reason"]=="dirty-worktree-no-commit"


def test_further_edit_of_already_dirty_tracked_path_with_space_and_non_ascii_name_is_ended_incomplete(fixture_tree):
    # Producer brief round 4, required case (sol + fable minor): round 3's parser read
    # `git status --porcelain` (no -z) and stripped one leading/trailing '"' per path -- git's
    # default quoting wraps a path in '"' and C-style-octal-escapes non-ASCII bytes whenever it
    # quotes at all, so Trim('"') alone cannot restore a path containing BOTH a space and a
    # non-ASCII character (a space alone needs no quoting; a non-ASCII byte alone gets quoted
    # AND escaped). `-z` disables quoting entirely, so the real path must round-trip exactly.
    # Exercises the exact "pre-dirty, then further edited" shape as the round-3 ASCII test
    # above, on a path this repo's real fleet-runs tree can plausibly contain.
    root=fixture_tree["root"]
    _seed_git_repo(root)
    tracked_name = "trac ked éè.txt"  # embedded space + non-ASCII (accented) characters
    tracked_path = root / tracked_name
    tracked_path.write_text("seed\n", encoding="utf-8")
    _git(root, "add", tracked_name)
    _git(root, "commit", "-q", "-m", "seed space/non-ASCII tracked file")
    # Pre-dirty it, uncommitted, before the lane ever starts.
    tracked_path.write_text("dirty-before-the-lane-ever-ran\n", encoding="utf-8")
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    env["MLV_FIXTURE_FURTHER_EDIT_TRACKED_PATH"]=str(tracked_path)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="ended-incomplete"
    assert q["complete"] is False
    assert q["workEvidence"]["reason"]=="dirty-worktree-no-commit"


def test_untracked_only_dirt_does_not_trigger_dirty_no_commit(fixture_tree):
    root=fixture_tree["root"]
    _seed_git_repo(root)
    (root/"scratch.log").write_text("untracked scratch output\n",encoding="ascii")
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


def test_clean_git_worktree_is_still_marked_complete(fixture_tree):
    # The check must not misfire on the common healthy case: a git worktree with nothing
    # dirty at all, exercised through an editing lane so the gated code path actually runs.
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"
    assert q["failure"] is None


def test_pre_existing_tracked_dirt_unchanged_by_editing_lane_stays_complete(fixture_tree):
    # Producer brief round 2, required case: dirt that existed BEFORE the lane ever ran, and
    # that the lane's own run leaves byte-for-byte unchanged, must never flip a receipt -- only
    # dirt the lane itself introduces counts. The fixture's fake child touches nothing here (no
    # MLV_FIXTURE_DIRTY_TRACKED_PATH), so the pre-existing modification survives unchanged.
    root=fixture_tree["root"]
    _seed_git_repo(root)
    (root/"tracked.txt").write_text("dirty-before-the-lane-ever-ran\n",encoding="ascii")
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


def test_committed_work_with_leftover_tracked_dirt_stays_complete(fixture_tree):
    # Producer brief round 2, required case: pins the HEAD guard. The fixture's fake child
    # commits its edit (moving HEAD away from BaseSha) and then leaves a further,
    # still-uncommitted edit on top -- a regression that dropped the `headAfter -eq $BaseSha`
    # condition would misclassify this as dirty-worktree-no-commit (fable minor 4 / sol minor 3:
    # no prior test pinned this suppression direction).
    root=fixture_tree["root"]
    _seed_git_repo(root)
    cmd,env,receipt=prepare(fixture_tree,"normal",editing=True,allowed_tools="Read,Write,Edit")
    env["MLV_FIXTURE_COMMIT_TRACKED_PATH"]=str(root/"tracked.txt")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


def test_codex_lane_with_dirty_tracked_worktree_is_never_ended_incomplete_by_this_rule(fixture_tree):
    # Producer brief round 2, required case: the override is a Claude-only, -AllowEdits-only
    # concept ($InitialTrackedDirt is captured only when engine=='claude' and $AllowEdits) -- a
    # codex lane must never be forced into ended-incomplete by it, no matter how dirty the
    # tracked worktree is.
    root=fixture_tree["root"]
    _seed_git_repo(root)
    (root/"tracked.txt").write_text("dirty-tracked-file\n",encoding="ascii")
    cmd,env,receipt=prepare(fixture_tree,"normal",lane="sol")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


def test_read_only_claude_lane_with_dirty_tracked_worktree_is_never_ended_incomplete_by_this_rule(fixture_tree):
    # Producer brief round 2, required case: a read-only Claude lane can never move HEAD by
    # construction, so without the -AllowEdits gate this degenerated into "was the surrounding
    # checkout dirty" -- a fact outside a review lane's control (sol major 1 / fable minor 3:
    # the round-1 positive test for this check actually used a read-only lane).
    root=fixture_tree["root"]
    _seed_git_repo(root)
    (root/"tracked.txt").write_text("dirty-but-uncommitted\n",encoding="ascii")
    cmd,env,receipt=prepare(fixture_tree,"normal")
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==0,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="complete" and q["complete"] is True
    assert q["workEvidence"]["reason"]!="dirty-worktree-no-commit"


# LANE-LAUNCH-NO-CHILD-TYPED-1: a claude lane whose child is never created (the contained host does not
# produce the child control file inside the 10 s control window) used to be receipted as
# state=ended-incomplete, timedOut=true, failure=null -- indistinguishable from a round that ran and
# stopped (5 receipts in 3 days, 11-48 s into a 2400-7200 s budget). It is now failure=launch-no-child.
def _no_child_launch_mutation(text):
    # The host's pwsh cold start stands in for the loaded machine; the 10 s control window is shortened
    # so the real code path (contained-child-start-timeout) is reached inside a test budget.
    old = "$line=[Console]::In.ReadLine(); if([string]::IsNullOrWhiteSpace($line)){throw 'launch-frame-missing'}"
    assert text.count(old) == 1
    text = text.replace(old, "Start-Sleep -Seconds 6\n" + old)
    win = "$sw.Elapsed.TotalMilliseconds + 10000.0"
    assert text.count(win) == 1
    return text.replace(win, "$sw.Elapsed.TotalMilliseconds + 1500.0")


def test_child_never_created_is_typed_launch_no_child_not_a_timeout(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=_no_child_launch_mutation)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=40)
    assert r.returncode==127,(r.stdout,r.stderr)   # launch failure, not 124 (timed out)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["timeoutSec"]==30 and q["durationSec"]<30, "the lane's own deadline must NOT have passed"
    assert q["failure"]=="launch-no-child"
    assert q["state"]=="failed" and q["complete"] is False
    assert q["timedOut"] is False and q["processEnded"] is False
    c=q["containment"]
    assert c["childPid"] is None and c["promptDelivered"] is False and c["jobAssigned"] is True
    assert not (fixture_tree["root"]/"child.json").exists()
    cap=q["launchNoChild"]
    assert cap["hostPid"]==c["ownerPid"]
    assert cap["hostExitedBeforeRunnerAction"] is False   # the host was alive and silent, then reaped by the runner
    assert cap["captureError"] is None and "hostStderrHead" in cap and "hostExitCode" in cap
    wait_absent({"pid":c["ownerPid"],"createdUtc":c["ownerCreatedUtc"]})


def test_ran_and_timed_out_lane_keeps_ended_incomplete_unchanged(fixture_tree):
    # CONTROL for the test above: a lane whose child DID run and then hit its deadline is still
    # ended-incomplete / timedOut / exit 124 with failure null and no launchNoChild block.
    cmd,env,receipt=prepare_timeout_after_ready(fixture_tree)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=20)
    assert r.returncode==124,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["state"]=="ended-incomplete" and q["timedOut"] is True and q["failure"] is None
    assert q["containment"]["childPid"] is not None
    assert "launchNoChild" not in q


def test_exhausted_budget_before_child_stays_a_timeout_not_launch_no_child(fixture_tree):
    # CONTROL: the lane's OWN deadline really passed (-TimeoutSec 0): that is a timeout and must not be retyped.
    cmd,env,receipt=prepare(fixture_tree,"normal")
    cmd[cmd.index("-TimeoutSec")+1]="0"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=15)
    assert r.returncode==124,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["timedOut"] is True and q["failure"] is None and "launchNoChild" not in q


# LANE-LAUNCH-NO-CHILD-TYPED-1 r2 (catalogue row C-01, sol r1 BLOCKER): the OTHER no-child path. A contained
# host that EXITS before creating the child (live: PLAYBACK-LJ92-DECODE-THROUGHPUT-1 r3, 2.2 s in, exit 1,
# stderr never captured) was receipted as failure='contained-host-exited-before-child: 1', untyped.
def _host_startup_failure_mutation(text):
    # Real host failure: the host reads the launch frame, then dies while starting the child, exactly as
    # a missing/unstartable claude executable would (ErrorActionPreference=Stop -> stderr + exit 1).
    old = "$child=[Diagnostics.Process]::Start($p);"
    assert text.count(old) == 1
    return text.replace(old, "throw 'fixture-startup-failure: claude executable could not start';" + old)


def test_host_exiting_before_child_is_typed_launch_no_child_with_exit_and_stderr(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=_host_startup_failure_mutation)
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=40)
    assert r.returncode==127,(r.stdout,r.stderr)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["failure"]=="launch-no-child",q["failure"]
    assert q["state"]=="failed" and q["complete"] is False and q["timedOut"] is False
    c=q["containment"]
    assert c["childPid"] is None and c["promptDelivered"] is False
    assert not (fixture_tree["root"]/"child.json").exists()
    cap=q["launchNoChild"]
    assert cap["hostPid"]==c["ownerPid"] and cap["captureError"] is None
    assert cap["hostExitedBeforeRunnerAction"] is True and cap["hostExitCode"]==1
    assert "fixture-startup-failure" in cap["hostStderrHead"]
    # nothing is lost: the pre-r2 failure text survives as the cause
    assert cap["cause"]=="contained-host-exited-before-child: 1"
    assert cap["causeType"]=="RuntimeException"


# LANE-LAUNCH-NO-CHILD-TYPED-1 r3 (catalogue row C-02, sol r2 BLOCKER): the deadline exclusion must hold on EVERY
# no-child path, not only TimeoutException. Absent control file + an exited host (code 1) + elapsed >= TimeoutSec:
# the host-exit branch used to be evaluated before the deadline and the RuntimeException bypassed the elapsed guard.
def _host_exit_after_deadline_mutation(text):
    text = _host_startup_failure_mutation(text)
    # The runner is held (deterministically, no race) past the lane's own deadline while the host dies, so the
    # loop sees HasExited=true, control file absent, elapsed >= TimeoutSec -- the exact state sol reproduced.
    old = "        if ($proc.HasExited) { throw \"contained-host-exited-before-child: $($proc.ExitCode)\" }"
    assert text.count(old) == 1
    return text.replace(old, "        Start-Sleep -Seconds 6\n" + old)


def test_host_exit_after_the_lane_deadline_stays_a_timeout_not_launch_no_child(fixture_tree):
    cmd,env,receipt=prepare(fixture_tree,"normal",mutation=_host_exit_after_deadline_mutation)
    cmd[cmd.index("-TimeoutSec")+1]="5"
    r=subprocess.run(cmd,env=env,text=True,capture_output=True,timeout=60)
    q=json.loads(receipt.read_text(encoding="utf-8"))
    assert q["durationSec"]>=5, "precondition: the lane's own deadline must have passed"
    assert q["failure"]!="launch-no-child",q["failure"]
    assert "launchNoChild" not in q
    assert q["timedOut"] is True
    assert r.returncode==124,(r.returncode,r.stdout,r.stderr)
    assert q["containment"]["childPid"] is None and q["containment"]["promptDelivered"] is False
