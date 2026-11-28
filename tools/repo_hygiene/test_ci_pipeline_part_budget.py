"""Pins the part budget of `Windows Product Oracles part N/3` (CI-FLAKE-PRODUCT-ORACLES-PART1-CHECK-GOLDEN-CAP-1).

Part 1/3 was cancelled at the 60-minute job cap inside `Run pipeline_tests --check-golden (bounded
shards)` three times in 48 hours (#915, #924, the #925 merge_group). No shard hung: every shard was
bounded at 240 s and the part was still finishing shards two minutes before the cap. Part 1 simply
carried more than the others: its seed for the other work it owns (a console_tests build and
--check-golden, about 1,000 s) was 200 s, and the flat 30 s / 17 s shard weights hid that the
LookAssist fixture renders take 60-90 s each. The assignment now uses measured weights
(`tools/testing/pipeline_test_weights.json`), and this test prices every part with them.
"""

from __future__ import annotations

import re
import unicodedata
import unittest

from tools.repo_hygiene import ci_pipeline_part_plan as plan

# What one part may weigh: its seed plus its shards, from the table, in seconds. The job cap is
# 3,600 s, so a part may run 1.5x its table weight before the cap cancels it. The balanced plan
# prices each part at about 2,050 s; the rest is headroom for new tests (each unlisted test costs
# `defaultTestMillis`). Raising this number is a decision to accept less slack against runner
# variance, not a way to make a red test green: rebalance, or split a 4th part, instead.
PART_BUDGET_SECONDS = 2400
JOB_CAP_SECONDS = 3600
MIN_SLACK_FACTOR = 1.5


def _read_workflow() -> str:
    return plan.WORKFLOW_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _workflow_job(workflow: str, job_id: str) -> str:
    start = workflow.index(f"\n  {job_id}:\n")
    following = re.search(r"\n  [A-Za-z0-9_-]+:\n", workflow[start + 1 :])
    return workflow[start : start + 1 + following.start()] if following else workflow[start:]


# The WHOLE step: its header keys (`- name:`, `if:`, `run: |`) and every line of its run block, through
# the part-2 golden and stress legs to the expected-failure check, comment and blank lines dropped and
# indentation stripped. Header and tail are pinned because an `if:` false on pull_request, or a second
# loop over `$shards` after the pinned one, changes the work a part does as surely as a weight does
# (adversary r2 pass 5 A1, A2). The `$goldenKeyToProducer` rows are data, compared by shape.
# The mirror in ci_pipeline_part_plan.py repeats the discovery, the windows and the assignment
# below; a changed or added line anywhere in this body (a scaled weight, another tie-break, a second
# `$myShards`, a `$script:` or `Set-Variable` spelling of a pinned variable, a new `$testsPerShard`)
# would make the budget describe a plan CI no longer runs. Pinning the whole body, rather than named
# lines or counts of named assignments, is what closes that whole class (adversary r1 passes 1-3 each
# found one more spelling of it). Any edit here must be made in ci_pipeline_part_plan.py too.
#
# Two lines are compared by shape, not text, because a card legitimately changes them:
# `$soloPattern = '<regex>'` (a card that adds a solo class edits the regex) and
# `$testsPerShard = <n>` (another test pins n to 12). Each must still occur exactly once, so the
# placeholder appears exactly once in the tuple.
SOLO_PLACEHOLDER = "$soloPattern = '<regex>'"
TESTS_PER_SHARD_PLACEHOLDER = "$testsPerShard = <n>"
GOLDEN_ENTRY_PLACEHOLDER = "'<golden key>' = '<producer test>'"
EXPECTED_STEP_BODY: tuple[str, ...] = (
    "- name: Run pipeline_tests --check-golden (bounded shards)",
    "if: needs.protected-check-route.outputs.product == 'true'",
    "run: |",
    "$env:PATH = \"$env:QT_ROOT_DIR\\bin;$env:MINGW_ROOT\\bin;$env:PATH\"",
    "$env:MLVAPP_CRASH_FORENSICS_AUTOINSTALL = \"1\"",
    "$env:MLVAPP_CRASH_FORENSICS_LOG_DIR = Join-Path $env:RUNNER_TEMP \"pipeline-crash-forensics\"",
    "New-Item -ItemType Directory -Force $env:MLVAPP_CRASH_FORENSICS_LOG_DIR | Out-Null",
    "$suiteOutput = Join-Path $env:RUNNER_TEMP \"pipeline-tests-output.txt\"",
    "New-Item -ItemType File -Force $suiteOutput | Out-Null",
    "$testNames = @(",
    "foreach ($file in Get-ChildItem tests\\pipeline -Filter *.cpp) {",
    "$text = Get-Content -LiteralPath $file.FullName -Raw",
    "foreach ($match in [regex]::Matches($text, 'TEST(?:_[FP])?\\(\\s*([A-Za-z0-9_]+)\\s*,\\s*([A-Za-z0-9_]+)')) {",
    "\"{0}.{1}\" -f $match.Groups[1].Value.Trim(), $match.Groups[2].Value.Trim()",
    "}",
    "}",
    ") | Sort-Object -Unique",
    "if ($testNames.Count -eq 0) { throw \"pipeline test registry discovery returned no tests\" }",
    TESTS_PER_SHARD_PLACEHOLDER,
    SOLO_PLACEHOLDER,
    "$shards = @()",
    "for ($offset = 0; $offset -lt $testNames.Count; $offset += $testsPerShard) {",
    "$end = [Math]::Min($offset + $testsPerShard, $testNames.Count)",
    "$window = @($testNames[$offset..($end - 1)])",
    "$windowIndex = [int]($offset / $testsPerShard)",
    "$shared = @($window | Where-Object { $_ -notmatch $soloPattern })",
    "if ($shared.Count -gt 0) {",
    "$shards += @{ Name = (\"exact-{0:D2}\" -f $windowIndex); Filter = ($shared -join ':'); Index = $shards.Count }",
    "}",
    "$solo = @($window | Where-Object { $_ -match $soloPattern })",
    "for ($i = 0; $i -lt $solo.Count; $i++) {",
    "$shards += @{ Name = (\"exact-{0:D2}-solo{1}\" -f $windowIndex, ($i + 1)); Filter = $solo[$i]; Index = $shards.Count }",
    "}",
    "}",
    "$shardedNames = @($shards | ForEach-Object { $_.Filter -split ':' })",
    "if ($shardedNames.Count -ne $testNames.Count -or",
    "@($shardedNames | Sort-Object -Unique).Count -ne $testNames.Count -or",
    "@(Compare-Object $testNames ($shardedNames | Sort-Object -Unique)).Count -ne 0) {",
    "throw \"pipeline shards do not cover each test exactly once (tests=$($testNames.Count) sharded=$($shardedNames.Count))\"",
    "}",
    "Write-Output (\"pipeline shard plan: tests={0} shards={1}\" -f $testNames.Count, $shards.Count)",
    "$partCount = 3",
    "$part = [int]'${{ matrix.part }}'",
    "$weightTable = Get-Content -LiteralPath 'tools\\testing\\pipeline_test_weights.json' -Raw | ConvertFrom-Json -AsHashtable",
    "if ($weightTable.schema -ne 1 -or $weightTable.unit -ne 'milliseconds') { throw \"pipeline test weights have an unexpected schema or unit\" }",
    "$partLoad = @($weightTable.partSeedMillis | ForEach-Object { [long]$_ })",
    "if ($partLoad.Count -ne $partCount) { throw \"pipeline part seeds must have one entry per part (seeds=$($partLoad.Count) parts=$partCount)\" }",
    "$defaultTestMillis = [long]$weightTable.defaultTestMillis",
    "foreach ($planned in $shards) {",
    "$weight = [long]0",
    "foreach ($plannedTest in ($planned.Filter -split ':')) {",
    "$weight += $(if ($weightTable.testMillis.ContainsKey($plannedTest)) { [long]$weightTable.testMillis[$plannedTest] } else { $defaultTestMillis })",
    "}",
    "$planned.Weight = $weight",
    "}",
    "$partOf = @{}",
    "$planOrder = @($shards | Sort-Object @{ Expression = { $_.Weight }; Descending = $true }, @{ Expression = { $_.Index }; Descending = $false })",
    "foreach ($planned in $planOrder) {",
    "$target = 0",
    "for ($candidate = 1; $candidate -lt $partCount; $candidate++) {",
    "if ($partLoad[$candidate] -lt $partLoad[$target]) { $target = $candidate }",
    "}",
    "$partOf[$planned.Name] = $target + 1",
    "$partLoad[$target] += $planned.Weight",
    "}",
    "$partShardCounts = @(1..$partCount | ForEach-Object { $number = $_; @($shards | Where-Object { $partOf[$_.Name] -eq $number }).Count })",
    "if ($partOf.Count -ne $shards.Count -or",
    "(($partShardCounts | Measure-Object -Sum).Sum) -ne $shards.Count -or",
    "@($partShardCounts | Where-Object { $_ -eq 0 }).Count -ne 0) {",
    "throw \"pipeline parts do not cover each shard exactly once (shards=$($shards.Count) parts=$($partShardCounts -join ','))\"",
    "}",
    "$myShards = @($shards | Where-Object { $partOf[$_.Name] -eq $part })",
    "$myTestNames = @($myShards | ForEach-Object { $_.Filter -split ':' })",
    "Write-Output (\"PIPELINE-PART-PLAN part={0}/{1} shards={2}/{3} tests={4}/{5} estimatedSeconds={6}\" -f $part, $partCount, $myShards.Count, $shards.Count, $myTestNames.Count, $testNames.Count, ($partLoad[$part - 1] / 1000))",
    "Write-Output (\"PIPELINE-PART-SHARDS part={0} {1}\" -f $part, (($myShards | ForEach-Object { $_.Name }) -join ','))",
    "Write-Output (\"PIPELINE-PART-TESTS part={0} {1}\" -f $part, ($myTestNames -join ','))",
    "function Invoke-BoundedPipelineShard {",
    "param(",
    "[string]$Name,",
    "[string[]]$Arguments,",
    "[string]$OutputPath,",
    "[int]$WaitMilliseconds = 240000",
    ")",
    "$psi = [System.Diagnostics.ProcessStartInfo]::new()",
    "$psi.FileName = (Resolve-Path \"tests\\build-ci-pipeline\\release\\pipeline_tests.exe\").Path",
    "$psi.WorkingDirectory = $env:GITHUB_WORKSPACE",
    "$psi.UseShellExecute = $false",
    "$psi.RedirectStandardOutput = $true",
    "$psi.RedirectStandardError = $true",
    "foreach ($argument in $Arguments) {",
    "[void]$psi.ArgumentList.Add($argument)",
    "}",
    "$process = [System.Diagnostics.Process]::new()",
    "$process.StartInfo = $psi",
    "if (-not $process.Start()) { throw \"failed to start pipeline shard $Name\" }",
    "$stdoutTask = $process.StandardOutput.ReadToEndAsync()",
    "$stderrTask = $process.StandardError.ReadToEndAsync()",
    "$timedOut = -not $process.WaitForExit($WaitMilliseconds)",
    "if ($timedOut) {",
    "try { $process.Kill($true) } catch { }",
    "}",
    "$process.WaitForExit()",
    "$stdout = $stdoutTask.GetAwaiter().GetResult()",
    "$stderr = $stderrTask.GetAwaiter().GetResult()",
    "$combined = $stdout + $stderr",
    "Set-Content -Path $OutputPath -Value $combined -Encoding utf8",
    "return [pscustomobject]@{",
    "ExitCode = if ($timedOut) { 124 } else { $process.ExitCode }",
    "Output = $combined",
    "TimedOut = $timedOut",
    "}",
    "}",
    "foreach ($shard in $myShards) {",
    "$shardOutput = Join-Path $env:RUNNER_TEMP (\"pipeline-tests-{0}.txt\" -f $shard.Name)",
    "Add-Content -Path $suiteOutput -Value (\"===== SHARD {0} FILTER {1} =====\" -f $shard.Name, $shard.Filter)",
    "$shardResult = Invoke-BoundedPipelineShard -Name $shard.Name -Arguments @(\"--gtest_filter=$($shard.Filter)\") -OutputPath $shardOutput",
    "Add-Content -Path $suiteOutput -Value (\"===== SHARD {0} FILTER {1} EXIT {2} =====\" -f $shard.Name, $shard.Filter, $shardResult.ExitCode)",
    "Add-Content -Path $suiteOutput -Value $shardResult.Output",
    "Write-Output (\"===== SHARD {0} FILTER {1} EXIT {2} =====\" -f $shard.Name, $shard.Filter, $shardResult.ExitCode)",
    "Write-Output $shardResult.Output",
    "$shardExit = $shardResult.ExitCode",
    "if ($shardResult.TimedOut) {",
    "throw \"pipeline_tests shard $($shard.Name) exceeded 240 seconds (exit 124; output retained)\"",
    "}",
    "if ($shardExit -notin @(0, 1)) {",
    "throw \"pipeline_tests shard $($shard.Name) failed outside the expected-failure contract (exit $shardExit)\"",
    "}",
    "}",
    "if ($part -eq 2) {",
    "$goldenKeyToProducer = [ordered]@{",
    GOLDEN_ENTRY_PLACEHOLDER,
    "}",
    "$goldenPath = (Resolve-Path 'tests\\fixtures\\golden\\pipeline_hashes.json').Path",
    "$goldenExpected = Get-Content -LiteralPath $goldenPath -Raw | ConvertFrom-Json -AsHashtable",
    "$expectedGoldenKeys = @($goldenExpected.Keys | Sort-Object)",
    "$mappedGoldenKeys = @($goldenKeyToProducer.Keys | Sort-Object)",
    "$mappingDelta = @(Compare-Object -ReferenceObject $expectedGoldenKeys -DifferenceObject $mappedGoldenKeys)",
    "if ($mappingDelta.Count -ne 0) {",
    "throw \"golden key-to-producer coverage mismatch: $($mappingDelta | Out-String)\"",
    "}",
    "$goldenProducerTests = @($goldenKeyToProducer.Values | Sort-Object -Unique)",
    "if ($goldenProducerTests.Count -ne 7) {",
    "throw \"golden producer count changed: expected 7, actual $($goldenProducerTests.Count)\"",
    "}",
    "$goldenProducerFilter = $goldenProducerTests -join ':'",
    "$goldenOutput = Join-Path $env:RUNNER_TEMP 'pipeline-tests-golden-producers.txt'",
    "$goldenActualOutput = Join-Path $env:RUNNER_TEMP 'pipeline-golden-actual.json'",
    "$goldenResult = Invoke-BoundedPipelineShard -Name 'golden-producers' -Arguments @(",
    "'--check-golden',",
    "'--hash-output', $goldenActualOutput,",
    "\"--gtest_filter=$goldenProducerFilter\"",
    ") -OutputPath $goldenOutput",
    "Add-Content -Path $suiteOutput -Value (\"===== GOLDEN PRODUCERS FILTER {0} EXIT {1} =====\" -f $goldenProducerFilter, $goldenResult.ExitCode)",
    "Add-Content -Path $suiteOutput -Value $goldenResult.Output",
    "Write-Output (\"===== GOLDEN PRODUCERS FILTER {0} EXIT {1} =====\" -f $goldenProducerFilter, $goldenResult.ExitCode)",
    "Write-Output $goldenResult.Output",
    "if ($goldenResult.TimedOut) {",
    "throw 'pipeline golden producer job exceeded 240 seconds (exit 124; output retained)'",
    "}",
    "switch ($goldenResult.ExitCode) {",
    "0 { }",
    "2 { throw 'pipeline golden producer job could not write its actual-key artifact (exit 2)' }",
    "3 { throw 'pipeline golden producer job found a missing key, mismatch, or unexpected key (exit 3)' }",
    "5 { throw 'pipeline golden producer job refused a dirty or unverifiable oracle (exit 5)' }",
    "default { throw \"pipeline golden producer job failed outside the golden contract (exit $($goldenResult.ExitCode))\" }",
    "}",
    "$stressFilter = 'ProcessingFilters.AggressiveX*'",
    "$stressBatchCount = 4",
    "$stressBatchSize = 6",
    "$stressPlannedAttempts = $stressBatchCount * $stressBatchSize",
    "$stressAchievedAttempts = 0",
    "$powerDisclosure = 'STRESS-PREREG targetPower=95% historicalContendedRate=2/72 historicalLoad=24-way derivedN=107 cp95Lower=0.0033819 lowerBoundN=885 ciLoad=6-way ciN=24 pointPower=49.1% lowerBoundPower=7.8% dependence=within-batch-correlated'",
    "Add-Content -Path $suiteOutput -Value $powerDisclosure",
    "Write-Output $powerDisclosure",
    "for ($batch = 1; $batch -le $stressBatchCount; $batch++) {",
    "$stressProcesses = @()",
    "for ($run = 1; $run -le $stressBatchSize; $run++) {",
    "$attempt = (($batch - 1) * $stressBatchSize) + $run",
    "$name = \"stress-b{0:D2}-r{1:D2}-a{2:D3}\" -f $batch, $run, $attempt",
    "$psi = [System.Diagnostics.ProcessStartInfo]::new()",
    "$psi.FileName = (Resolve-Path \"tests\\build-ci-pipeline\\release\\pipeline_tests.exe\").Path",
    "$psi.WorkingDirectory = $env:GITHUB_WORKSPACE",
    "$psi.UseShellExecute = $false",
    "$psi.RedirectStandardOutput = $true",
    "$psi.RedirectStandardError = $true",
    "[void]$psi.ArgumentList.Add(\"--gtest_filter=$stressFilter\")",
    "$process = [System.Diagnostics.Process]::new()",
    "$process.StartInfo = $psi",
    "if (-not $process.Start()) { throw \"failed to start stress repetition $name\" }",
    "$stressProcesses += [pscustomobject]@{",
    "Name = $name",
    "Process = $process",
    "Stdout = $process.StandardOutput.ReadToEndAsync()",
    "Stderr = $process.StandardError.ReadToEndAsync()",
    "}",
    "}",
    "$batchFailures = @()",
    "foreach ($stress in $stressProcesses) {",
    "$timedOut = -not $stress.Process.WaitForExit(240000)",
    "if ($timedOut) {",
    "try { $stress.Process.Kill($true) } catch { }",
    "}",
    "$stress.Process.WaitForExit()",
    "$stressOutput = $stress.Stdout.GetAwaiter().GetResult() + $stress.Stderr.GetAwaiter().GetResult()",
    "$stressExit = if ($timedOut) { 124 } else { $stress.Process.ExitCode }",
    "$stressOutputPath = Join-Path $env:RUNNER_TEMP (\"pipeline-tests-{0}.txt\" -f $stress.Name)",
    "Set-Content -Path $stressOutputPath -Value $stressOutput -Encoding utf8",
    "Add-Content -Path $suiteOutput -Value (\"===== STRESS {0} FILTER {1} EXIT {2} =====\" -f $stress.Name, $stressFilter, $stressExit)",
    "Add-Content -Path $suiteOutput -Value $stressOutput",
    "Write-Output (\"===== STRESS {0} FILTER {1} EXIT {2} =====\" -f $stress.Name, $stressFilter, $stressExit)",
    "Write-Output $stressOutput",
    "$stressAchievedAttempts++",
    "if ($stressExit -ne 0) {",
    "$batchFailures += \"$($stress.Name):$stressExit\"",
    "}",
    "}",
    "if ($batchFailures.Count -ne 0) {",
    "$abort = \"STRESS-ABORT achieved=$stressAchievedAttempts planned=$stressPlannedAttempts failures=$($batchFailures -join ',')\"",
    "Add-Content -Path $suiteOutput -Value $abort",
    "throw $abort",
    "}",
    "}",
    "$stressComplete = \"STRESS-COMPLETE achieved=$stressAchievedAttempts planned=$stressPlannedAttempts failures=0\"",
    "Add-Content -Path $suiteOutput -Value $stressComplete",
    "Write-Output $stressComplete",
    "}",
    "& tools\\testing\\check-expected-failures.ps1 -SuiteOutput $suiteOutput",
    "if ($LASTEXITCODE -ne 0) {",
    "exit $LASTEXITCODE",
    "}",
)

STEP_NAME = "Run pipeline_tests --check-golden (bounded shards)"
# The one free-text line may hold only characters that PowerShell's lexer and `-match` (.NET regex)
# read exactly as the mirror's Python `re` does. Letters, digits, `_`, `|`, `^`, `$`, `(`, `)` and an
# escaped dot (`\.`) are enough to name solo classes and tests. Anything else is refused, not
# policed one spelling at a time (adversary r2 pass 4): PowerShell reads U+2018..U+201B as single
# quotes, so `'..’; $testsPerShard *= 20; ..‘..'` hid a second statement (A1); it decodes `''` to
# `'` while Python kept both (A2); and `(?`, `\d`, `\w`, `\p{..}`, `[..]`, `{n}`, `*`, `+`, `?` and
# non-ASCII letters mean different things in the two engines. A card that really needs more
# widens this set and the mirror together.
_SOLO_SHAPE = re.compile(r"\$soloPattern = '((?:[A-Za-z0-9_|^$()]|\\\.)*)'")
_TESTS_PER_SHARD_SHAPE = re.compile(r"\$testsPerShard = [0-9]+")
# `$goldenKeyToProducer` rows (part 2): data, compared by shape and collapsed to one placeholder, so a
# new golden key does not need an edit here. The workflow compares the keys with pipeline_hashes.json
# itself and pins the producer count on its own (pinned) line.
_GOLDEN_ENTRY_SHAPE = re.compile(r"'[a-z0-9_.]+' = '[A-Za-z0-9_.]+'")


def _ps_lines(text: str) -> list[str]:
    """Split the way PowerShell does: on LF only, one trailing CR dropped per line (adversary r2 pass 6 A1).

    `str.splitlines()` also breaks on U+2028, U+2029, U+0085, \\v, \\f and \\x1c-\\x1e, which
    PowerShell reads as ordinary characters: `# comment<U+2028>$myShards = ...` is one comment line
    there and two lines here, so the guard saw an assignment that never runs.
    """
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _unsafe_char(char: str) -> bool:
    """A character the step may not hold: any control (Cc) or line/paragraph separator (Zl, Zp) but a tab."""
    return char != "\t" and unicodedata.category(char) in ("Cc", "Zl", "Zp")


def _is_dropped(text: str) -> bool:
    """A blank line, or a line comment: `#` alone, or `#` and a space. Nothing else is skipped.

    `#requires -PSEdition Desktop` starts with `#` but is a directive that stops the script under
    pwsh Core (adversary r2 pass 6 A2), and `<#` opens a block comment that can hide whole lines; both
    stay in the body, where they differ from the pinned text.
    """
    return not text or text == "#" or text.startswith("# ")


def _step_span(lines: list[str]) -> tuple[int, int, int]:
    """Index of the step's `- name:` line, of its `run: |` line and of its last line (ValueError if it moved).

    The whole step is pinned, not only the planning prefix and the shard loop (adversary r2 pass 5):
    its header keys (`if:` decides whether any shard runs on pull_request, push and merge_group) and
    everything after the loop (a second loop over `$shards`, an early `exit`) change the work a part
    does as surely as a changed weight.
    """
    named = [i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP_NAME}"]
    if len(named) != 1:
        raise ValueError(f"step {STEP_NAME!r} must appear exactly once in the job (found {len(named)})")
    step_end = next((i for i in range(named[0] + 1, len(lines)) if lines[i].startswith("      - name:")), len(lines))
    run = next((i for i in range(named[0] + 1, step_end) if lines[i].strip() == "run: |"), None)
    if run is None:
        raise ValueError("the pipeline step lost its `run: |` block")
    return named[0], run, step_end - 1


def _step_body(part_job: str) -> tuple[str, ...]:
    lines = _ps_lines(part_job)
    try:
        first, _run, last = _step_span(lines)
    except ValueError as error:
        return (f"MISSING: {error}",)
    body: list[str] = []
    for line in lines[first : last + 1]:
        unsafe = sorted({f"U+{ord(char):04X}" for char in line if _unsafe_char(char)})
        if unsafe:
            return (f"REFUSED: control or line-separator character {unsafe} in the step",)
        text = line.strip(" \t")  # str.strip() would also eat U+0085, U+2028 and the other Unicode spaces
        if _is_dropped(text):
            continue
        if _SOLO_SHAPE.fullmatch(text):
            text = SOLO_PLACEHOLDER
        elif _TESTS_PER_SHARD_SHAPE.fullmatch(text):
            text = TESTS_PER_SHARD_PLACEHOLDER
        elif _GOLDEN_ENTRY_SHAPE.fullmatch(text):
            if body and body[-1] == GOLDEN_ENTRY_PLACEHOLDER:
                continue  # the map may gain or lose entries; the workflow checks it against the golden file
            text = GOLDEN_ENTRY_PLACEHOLDER
        body.append(text)
    return tuple(body)


def _guard_flags(part_job: str) -> bool:
    """True when the guard would refuse this job text."""
    return _step_body(part_job) != EXPECTED_STEP_BODY


def _step_code_lines(part_job: str) -> list[str]:
    lines = _ps_lines(part_job)
    first, _run, last = _step_span(lines)
    return [line.strip(" \t") for line in lines[first : last + 1]]


def _solo_pattern(part_job: str) -> str:
    """The regex the mirror uses: read from the one line the body pin lets through by shape."""
    found = [m.group(1) for m in map(_SOLO_SHAPE.fullmatch, _step_code_lines(part_job)) if m]
    assert len(found) == 1, "the pipeline step must set $soloPattern exactly once, from the allowed characters"
    return found[0]


def _tests_per_shard(part_job: str) -> int:
    found = [line for line in _step_code_lines(part_job) if line.startswith("$testsPerShard = ")]
    assert len(found) == 1 and _TESTS_PER_SHARD_SHAPE.fullmatch(found[0]), "the pipeline step must set $testsPerShard exactly once"
    return int(found[0].rsplit(" ", 1)[1])


class PipelinePartBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = _read_workflow()
        cls.part_job = _workflow_job(cls.workflow, "windows-product-oracles-part")
        cls.weights = plan.load_weights()
        cls.part_count = int(re.findall(r"(?m)^\s*\$partCount = (\d+)$", cls.part_job)[0])
        cls.test_names = plan.discover_test_names()
        cls.shards = plan.build_shards(
            cls.test_names, _tests_per_shard(cls.part_job), _solo_pattern(cls.part_job)
        )
        cls.part_of, cls.load = plan.assign_parts(cls.shards, cls.weights, cls.part_count)

    def test_every_part_stays_inside_the_budget(self) -> None:
        for part, millis in enumerate(self.load, start=1):
            with self.subTest(part=part):
                self.assertLessEqual(
                    millis / 1000,
                    PART_BUDGET_SECONDS,
                    f"part {part}/{self.part_count} is priced at {millis / 1000:.0f} s "
                    f"(seed + measured shards) against a budget of {PART_BUDGET_SECONDS} s; "
                    f"all parts: {[round(m / 1000) for m in self.load]}. Add a part or move work, "
                    "do not widen the job timeout",
                )

    def test_the_budget_leaves_the_job_cap_real_slack(self) -> None:
        timeout = re.search(r"(?m)^    timeout-minutes: (\d+)$", self.part_job)
        self.assertIsNotNone(timeout)
        self.assertEqual(int(timeout.group(1)) * 60, JOB_CAP_SECONDS)
        self.assertGreaterEqual(JOB_CAP_SECONDS / PART_BUDGET_SECONDS, MIN_SLACK_FACTOR)

    def test_the_parts_are_balanced(self) -> None:
        # Part 1 was priced at 41 min against 28 min for part 3 before the measured weights.
        spread = (max(self.load) - min(self.load)) / 1000
        self.assertLessEqual(spread, 300, f"part loads {[round(m / 1000) for m in self.load]} differ by {spread:.0f} s")

    def test_the_parts_run_every_test_exactly_once(self) -> None:
        ran: list[str] = []
        for part in range(1, self.part_count + 1):
            for shard in self.shards:
                if self.part_of[shard.name] == part:
                    ran.extend(shard.tests)
        self.assertEqual(sorted(ran), sorted(self.test_names))
        self.assertEqual(len(set(ran)), len(ran))
        self.assertEqual({self.part_of[s.name] for s in self.shards}, set(range(1, self.part_count + 1)))

    def test_the_seeds_say_part_one_carries_the_console_oracle(self) -> None:
        seeds = self.weights["partSeedMillis"]
        self.assertEqual(len(seeds), self.part_count)
        # Part 1 builds console_tests and runs its --check-golden (about 11 min on a hosted runner)
        # on top of what every part does; the old seeds were 200 / 140 / 0 s.
        self.assertGreaterEqual(seeds[0] - seeds[2], 500_000)
        self.assertGreater(seeds[1], seeds[2])

    def test_an_unlisted_test_costs_the_default_and_a_listed_one_its_row(self) -> None:
        listed = next(iter(self.weights["testMillis"]))
        one = plan.Shard("x", (listed,), 0)
        unknown = plan.Shard("y", ("NoSuchSuite.NoSuchTest",), 1)
        two = plan.Shard("z", (listed, "NoSuchSuite.NoSuchTest"), 2)
        self.assertEqual(plan.shard_millis(one, self.weights), self.weights["testMillis"][listed])
        self.assertEqual(plan.shard_millis(unknown, self.weights), self.weights["defaultTestMillis"])
        self.assertEqual(plan.shard_millis(two, self.weights), self.weights["testMillis"][listed] + self.weights["defaultTestMillis"])

    def test_the_weight_table_is_well_formed(self) -> None:
        table = self.weights["testMillis"]
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in table.values()))
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in self.weights["partSeedMillis"]))
        self.assertGreater(self.weights["defaultTestMillis"], 0)
        # PowerShell reads this table into a case-insensitive hashtable; two keys that differ only
        # by case would be one key there and two here.
        self.assertEqual(len({name.lower() for name in table}), len(table))

    def test_a_heavy_fixture_render_is_priced_as_heavy(self) -> None:
        # The flat 30 s solo weight priced a 90 s LookAssist render like a 10 s one.
        table = self.weights["testMillis"]
        heavy = [v for name, v in table.items() if name.startswith(("LookAssistFixtureScene.", "LookAssistFlavorsFixture."))]
        self.assertTrue(heavy)
        self.assertGreaterEqual(max(heavy), 60_000)

    def test_the_workflow_uses_the_table_the_mirror_prices(self) -> None:
        # Every line below is one the mirror repeats; changing any of them without
        # ci_pipeline_part_plan.py would make this budget describe a plan CI no longer runs.
        for needed in (
            "$weightTable = Get-Content -LiteralPath 'tools\\testing\\pipeline_test_weights.json' -Raw | ConvertFrom-Json -AsHashtable",
            "$partLoad = @($weightTable.partSeedMillis | ForEach-Object { [long]$_ })",
            "$defaultTestMillis = [long]$weightTable.defaultTestMillis",
            "$weightTable.testMillis.ContainsKey($plannedTest)",
            "$planOrder = @($shards | Sort-Object @{ Expression = { $_.Weight }; Descending = $true }, @{ Expression = { $_.Index }; Descending = $false })",
            "$partLoad[$target] += $planned.Weight",
            "if ($partLoad[$candidate] -lt $partLoad[$target]) { $target = $candidate }",
            "$myShards = @($shards | Where-Object { $partOf[$_.Name] -eq $part })",
        ):
            self.assertIn(needed, self.part_job)
        self.assertEqual(_tests_per_shard(self.part_job), 12)
        self.assertNotIn("$partLoad = @(200.0, 140.0, 0.0)", self.part_job)
        # Shards carry their plan index, which breaks weight ties the same way here and there.
        self.assertEqual(self.part_job.count("Index = $shards.Count"), 2)

    def test_the_step_body_is_exactly_what_the_mirror_repeats(self) -> None:
        self.assertEqual(_step_body(self.part_job), EXPECTED_STEP_BODY)

    def test_a_scaled_weight_in_the_workflow_is_refused(self) -> None:
        # adversary A1: `$planned.Weight = $weight * 0.4` priced part 3 at 2,541 s while every other
        # pinned line, and the budget, still passed.
        for original, mutated in (
            ("$planned.Weight = $weight", "$planned.Weight = $weight * 0.4"),
            ("$weight += $(if", "$weight += 0.4 * $(if"),
            ("else { $defaultTestMillis })", "else { 0 })"),
            ("$partLoad[$target] += $planned.Weight", "$partLoad[$target] += $planned.Weight / 2"),
            ("Descending = $true }", "Descending = $false }"),
        ):
            with self.subTest(mutated=mutated):
                self.assertIn(original, self.part_job)
                self.assertTrue(_guard_flags(self.part_job.replace(original, mutated, 1)))

    def test_a_fractional_default_is_refused(self) -> None:
        # adversary A2: PowerShell `[long]0.5` is 0 while Python kept 0.5; every number must be a
        # whole millisecond so both read the same value.
        for key in ("defaultTestMillis",):
            self.assertIsInstance(self.weights[key], int)
            self.assertNotIsInstance(self.weights[key], bool)
        for seed in self.weights["partSeedMillis"]:
            self.assertIsInstance(seed, int)
        for value in self.weights["testMillis"].values():
            self.assertIsInstance(value, int)
        with self.assertRaises(ValueError):
            plan.load_weights_from_text('{"schema": 1, "unit": "milliseconds", "partSeedMillis": [1, 2, 3], "defaultTestMillis": 0.5, "testMillis": {}}')

    def test_a_weight_the_workflow_cannot_read_is_refused(self) -> None:
        # adversary pass 2 A1: a default of 2**63 loads here but is a [long] conversion error in
        # the workflow, which converts every seed and the default unconditionally.
        base = '{"schema": 1, "unit": "milliseconds", "partSeedMillis": [1, 2, 3], "defaultTestMillis": %s, "testMillis": {"A.B": %s}}'
        plan.load_weights_from_text(base % (1400, 3000))
        for default, listed in ((2**63, 3000), (1400, 2**63), (-1, 3000), (1400, plan.MAX_MILLIS + 1), (True, 3000)):
            with self.subTest(default=default, listed=listed):
                with self.assertRaises(ValueError):
                    plan.load_weights_from_text(base % (str(default).lower(), listed))
        with self.assertRaises(ValueError):
            plan.load_weights_from_text(base.replace("[1, 2, 3]", "[1, 2, 9223372036854775808]") % (1400, 3000))

    def test_the_solo_match_is_case_insensitive_like_powershell(self) -> None:
        # adversary pass 2 A3: PowerShell -match ignores case, so does the mirror.
        pattern = _solo_pattern(self.part_job)
        shards = plan.build_shards(["lookassistfixturescene.A", "lookassistfixturescene.B"], 12, pattern)
        self.assertEqual([shard.name for shard in shards], ["exact-00-solo1", "exact-00-solo2"])

    def test_the_mirror_reproduces_the_window_cut(self) -> None:
        # Windows are consecutive slices of the sorted names; a solo test leaves its window's
        # shared shard, so the shard tests of one window are exactly that window's names.
        per_window: dict[str, list[str]] = {}
        for shard in self.shards:
            per_window.setdefault(shard.name.split("-solo")[0], []).extend(shard.tests)
        flat = [name for key in sorted(per_window) for name in per_window[key]]
        self.assertEqual(sorted(flat), sorted(self.test_names))
        self.assertTrue(all(len(v) <= 12 for v in per_window.values()))


def _insert_before(part_job: str, anchor: str, new_line: str) -> str:
    """Put `new_line` on its own line, at the step's indentation, just before the line starting `anchor`."""
    marker = "          " + anchor
    assert marker in part_job, f"no line starts with {anchor!r}"
    return part_job.replace(marker, "          " + new_line + "\n" + marker, 1)


class PipelineStepBodyPinTests(unittest.TestCase):
    """Adversary r1 pass 3 (A1, A2): a NEW line anywhere in the step body changed the plan unseen."""

    # (label, line the new line goes before, the new line)
    INSERTIONS = (
        ("A1 upper-case $MYSHARDS", "$myTestNames = ", "$MYSHARDS = @($shards)"),
        ("A1 $script: scope", "$myTestNames = ", "$script:myShards = @($shards)"),
        ("A1 Set-Variable", "$myTestNames = ", "Set-Variable -Name myShards -Value $shards"),
        ("A1 New-Variable", "$myTestNames = ", "New-Variable -Name myShards -Value $shards -Force"),
        ("A2 second $soloPattern before $shards", "$shards = @()", "$soloPattern = '^$'"),
        ("A2 $testsPerShard *= before $shards", "$shards = @()", "$testsPerShard *= 20"),
        ("case-variant $TestsPerShard", "$shards = @()", "$TestsPerShard = 240"),
        ("second $soloPattern after $shards", "for ($offset = 0;", "$soloPattern = '^$'"),
        ("second $soloPattern before the first", "$soloPattern = '", "$soloPattern = '^$'"),
        ("second $testsPerShard", "$shards = @()", "$testsPerShard = 240"),
        ("a pinned name reassigned late", "$myTestNames = ", "$partOf = @{}"),
        ("a function shadowing a cmdlet", "$shards = @()", "function Sort-Object { $input }"),
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.part_job = _workflow_job(_read_workflow(), "windows-product-oracles-part")
        cls.lines = _ps_lines(cls.part_job)
        cls.first, _run, cls.last = _step_span(cls.lines)
        cls.code = [i for i in range(cls.first, cls.last + 1) if not _is_dropped(cls.lines[i].strip(" \t"))]

    def _flags(self, lines: list[str]) -> bool:
        return _guard_flags("\n".join(lines))

    def test_the_real_workflow_is_accepted(self) -> None:
        self.assertFalse(_guard_flags(self.part_job))

    def test_a_line_added_to_the_step_is_refused_whatever_its_spelling(self) -> None:
        for label, anchor, new_line in self.INSERTIONS:
            with self.subTest(label):
                self.assertTrue(_guard_flags(_insert_before(self.part_job, anchor, new_line)))

    def test_a_second_statement_on_the_solo_pattern_line_is_refused(self) -> None:
        smuggled = self.part_job.replace(
            "$soloPattern = '^(", "$soloPattern = 'x'; $testsPerShard = 240; $y = '^(", 1
        )
        self.assertNotEqual(smuggled, self.part_job)
        self.assertTrue(_guard_flags(smuggled))

    def test_the_step_header_and_everything_after_the_shard_loop_are_pinned(self) -> None:
        # adversary r2 pass 5: A1 a second `foreach ($again in $shards)` after the pinned loop ran
        # every shard on every part (3 x the priced work); A2 an `if:` that is false on pull_request,
        # push and merge_group skipped the step entirely. Both sat outside the span pinned before.
        header = "- name: " + STEP_NAME + "\n        if: needs.protected-check-route.outputs.product == 'true'"
        self.assertIn(header, self.part_job)
        second_loop = (
            "foreach ($again in $shards) {\n"
            '            $extra = Invoke-BoundedPipelineShard -Name $again.Name -Arguments @("--gtest_filter=$($again.Filter)") -OutputPath $suiteOutput\n'
            "          }"
        )
        cases = [
            ("A1 second shard loop after the first", _insert_before(self.part_job, "# Part 2 owns", second_loop)),
            ("A1 second shard loop at the end of the step", _insert_before(self.part_job, "& tools\\testing\\check-expected-failures.ps1", second_loop)),
            ("A1 exit before the expected-failure check", _insert_before(self.part_job, "& tools\\testing\\check-expected-failures.ps1", "exit 0")),
            ("A2 if: limited to workflow_dispatch", self.part_job.replace(header, header + " && github.event_name == 'workflow_dispatch'", 1)),
            ("A2 if: removed", self.part_job.replace("\n        if: needs.protected-check-route.outputs.product == 'true'\n        run: |\n          $env:PATH", "\n        run: |\n          $env:PATH", 1)),
            ("A2 continue-on-error added", self.part_job.replace(header, header + "\n        continue-on-error: true", 1)),
            ("golden producer count changed", self.part_job.replace("-ne 7", "-ne 8", 1)),
            ("stress batches changed", self.part_job.replace("$stressBatchCount = 4", "$stressBatchCount = 1", 1)),
        ]
        for label, mutated in cases:
            with self.subTest(label):
                self.assertNotEqual(mutated, self.part_job)
                self.assertTrue(_guard_flags(mutated))

    def test_the_golden_map_may_grow_and_a_value_may_change(self) -> None:
        # The map's entries are data the workflow itself checks against pipeline_hashes.json.
        entry = "              'tiny_dual_iso.preview_processing.cpu.frame0' = "
        self.assertIn(entry, self.part_job)
        grown = self.part_job.replace(entry, "              'tiny_dual_iso.some_new_key.frame0' = 'NewSuite.NewTest'\n" + entry, 1)
        self.assertNotEqual(grown, self.part_job)
        self.assertFalse(_guard_flags(grown))
        shrunk = "\n".join(
            line for line in _ps_lines(self.part_job) if not line.strip().startswith("'tiny_dual_iso.gpu_preview_subset.signature.frame1'")
        )
        self.assertNotEqual(shrunk, self.part_job)
        self.assertFalse(_guard_flags(shrunk))

    def test_a_solo_pattern_the_two_engines_could_read_differently_is_refused(self) -> None:
        # adversary r2 pass 4 A1: PowerShell reads U+2018..U+201B as single quotes, so a "second
        # statement" hid behind curly quotes; A2: PowerShell decodes `''` to `'` and the mirror
        # kept both. The free-text line may hold only characters both engines read the same way.
        original = _solo_pattern(self.part_job)
        literal = "$soloPattern = '" + original + "'"
        self.assertIn(literal, self.part_job)
        values = [("A1 curly-quote statement", original + "’; $testsPerShard *= 20; $soloPattern = 0; $y = ‘z")]
        values += [(f"quote U+{code:04X}", original + chr(code) + "x") for code in (0x2018, 0x2019, 0x201A, 0x201B)]
        values += [
            ("A2 doubled apostrophe", "^(?!''?)(?:" + original + ")"),
            ("doubled apostrophe", original + "''"),
            ("look-ahead", "^(?!x)" + original),
            ("inline option", "(?i)" + original),
            ("digit class", original + "|^\\d"),
            ("word class", original + "|^\\w"),
            ("unicode class", original + "|^\\p{L}"),
            ("set", original + "|^[A-Z]"),
            ("quantifier", original + "|^x{2}"),
            ("star", original + "|^x*"),
            ("plus", original + "|^x+"),
            ("optional", original + "|^x?"),
            ("non-ASCII letter", original + "|^é"),
            ("space", original + "| ^x"),
        ]
        for label, value in values:
            with self.subTest(label):
                mutated = self.part_job.replace(literal, "$soloPattern = '" + value + "'", 1)
                self.assertNotEqual(mutated, self.part_job)
                self.assertTrue(_guard_flags(mutated))

    def test_every_body_line_is_pinned_against_deletion_duplication_and_case_change(self) -> None:
        for index in self.code:
            line = self.lines[index]
            if _GOLDEN_ENTRY_SHAPE.fullmatch(line.strip()):
                continue  # data rows of the golden map; test_the_golden_map_may_grow_and_a_value_may_change
            with self.subTest(line=line.strip()[:70]):
                self.assertTrue(self._flags(self.lines[:index] + self.lines[index + 1 :]), "deleted")
                self.assertTrue(self._flags(self.lines[: index + 1] + [line] + self.lines[index + 1 :]), "duplicated")
                if line.swapcase() != line:
                    self.assertTrue(self._flags(self.lines[:index] + [line.swapcase()] + self.lines[index + 1 :]), "case changed")

    def test_a_statement_inserted_at_any_position_in_the_body_is_refused(self) -> None:
        for position in range(self.first + 1, self.last + 1):  # before `- name:` is outside the step
            with self.subTest(position=position):
                self.assertTrue(self._flags(self.lines[:position] + ["          $injected = 1"] + self.lines[position:]))

    def test_what_a_card_may_change_stays_accepted(self) -> None:
        # A card that adds a solo class edits the regex; comments and blank lines are not code.
        wider = self.part_job.replace("|^Debayered16ReconReuse\\.", "|^Debayered16ReconReuse\\.|^ANewSoloClass\\.", 1)
        self.assertNotEqual(wider, self.part_job)
        self.assertFalse(_guard_flags(wider))
        self.assertFalse(_guard_flags(_insert_before(self.part_job, "$shards = @()", "# a comment")))
        self.assertFalse(_guard_flags(_insert_before(self.part_job, "$shards = @()", "")))
        # The shape of $testsPerShard is free here; test_the_workflow_uses_the_table_the_mirror_prices
        # holds the value itself at 12.
        self.assertFalse(_guard_flags(self.part_job.replace("$testsPerShard = 12", "$testsPerShard = 14", 1)))

    def test_a_step_that_moved_or_lost_its_loop_is_refused(self) -> None:
        for original, mutated in (
            (STEP_NAME, STEP_NAME + " renamed"),
            ("foreach ($shard in $myShards) {", "foreach ($shard in @($myShards)) {"),
        ):
            with self.subTest(original=original):
                self.assertTrue(_guard_flags(self.part_job.replace(original, mutated, 1)))

    # adversary r2 pass 6 -----------------------------------------------------------------------

    # Every character str.splitlines() breaks a line on besides LF/CR-LF, and a lone CR and a NUL.
    BREAK_CHARS = (" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\r", "\x00")
    ASSIGNMENT = "$myShards = @($shards | Where-Object { $partOf[$_.Name] -eq $part })"

    def test_a_line_break_powershell_does_not_read_cannot_hide_code_in_a_comment(self) -> None:
        # A1, the verdict's literal input: `# accepted comment<U+2028>$myShards = ...`. PowerShell reads
        # all of it as ONE comment, so the assignment never runs (zero shards); splitlines() saw two lines.
        line = "          " + self.ASSIGNMENT
        self.assertIn(line + "\n", self.part_job)
        for char in self.BREAK_CHARS:
            with self.subTest(char=f"U+{ord(char):04X}"):
                mutated = self.part_job.replace(line, "          # accepted comment" + char + line, 1)
                self.assertNotEqual(mutated, self.part_job)
                self.assertTrue(_guard_flags(mutated))

    def test_a_break_or_control_character_is_refused_on_every_code_line_and_at_its_end(self) -> None:
        self.assertFalse(_guard_flags(self.part_job))
        for index in self.code:
            line = self.lines[index]
            for char in (" ", " ", "\x85"):
                with self.subTest(line=line.strip()[:50], char=f"U+{ord(char):04X}"):
                    hidden = self.lines[:index] + ["          # accepted" + char + line] + self.lines[index + 1 :]
                    self.assertTrue(self._flags(hidden), "comment + separator + the line")
                    self.assertTrue(self._flags(self.lines[:index] + [line + char] + self.lines[index + 1 :]), "separator at the end")

    def test_every_character_splitlines_breaks_on_is_refused_and_does_not_split_here(self) -> None:
        # Differential, exact oracle: Python's own str.splitlines() over every code point.
        for code in range(0x110000):
            char = chr(code)
            if char in "\n\t":
                continue
            if len(("a" + char + "b").splitlines()) != 1:
                self.assertTrue(_unsafe_char(char), f"U+{code:04X} splits a line in Python and is not refused")
            if char != "\r":
                self.assertEqual(_ps_lines("a" + char + "b"), ["a" + char + "b"], f"U+{code:04X} split a line")
        self.assertEqual(_ps_lines("a\r\nb\nc\r"), ["a", "b", "c"])
        self.assertEqual(_ps_lines("a\rb"), ["a\rb"])

    def test_a_requires_directive_or_a_block_comment_is_not_a_comment(self) -> None:
        # A2: `#requires -PSEdition Desktop` stops the script under pwsh Core; `<#` hides whole lines.
        for label, new_line in (
            ("A2 the verdict's literal", "#requires -PSEdition Desktop"),
            ("upper case", "#REQUIRES -Version 99"),
            ("mixed case", "#Requires -Modules NoSuchModule"),
            ("a space after the hash is not a directive but a comment", None),
            ("no space after the hash", "#not-a-pinned-comment"),
            ("hash then tab", "#\trequires -PSEdition Desktop"),
            ("block comment opened", "<#"),
            ("block comment on one line", "<# hidden #>"),
            ("block comment after code", "$shards = @() <# hidden #>"),
        ):
            with self.subTest(label):
                if new_line is None:
                    self.assertFalse(_guard_flags(_insert_before(self.part_job, "$shards = @()", "# requires is only a word here")))
                    continue
                self.assertTrue(_guard_flags(_insert_before(self.part_job, "$shards = @()", new_line)))
        block = _insert_before(self.part_job, "$shards = @()", "<#")
        block = _insert_before(block, "& tools\\testing\\check-expected-failures.ps1", "#>")
        self.assertTrue(_guard_flags(block), "a block comment spanning the shard loop")

    def test_a_lone_hash_line_stays_a_comment(self) -> None:
        self.assertFalse(_guard_flags(_insert_before(self.part_job, "$shards = @()", "#")))


if __name__ == "__main__":
    unittest.main()
