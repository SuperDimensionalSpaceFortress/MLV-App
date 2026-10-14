#requires -Version 7.2
<#
.SYNOPSIS
Preview or apply the receipt-gated Phase 0.4b transition on SuperDimensionalSpaceFortress/MLV-App.
.DESCRIPTION
Default: validate local prerequisites and print the exact proposed body, with no
network calls or writes. -Apply additionally requires the merged actor receipt,
checks live protection, PATCHes only required_status_checks, verifies a fresh GET,
and appends evidence. An immutable intent permits recovery after a successful
PATCH whose verification or receipt write was interrupted. No receipt is replaced.
#>
[CmdletBinding()]
param(
    [string]$BoardRoot = 'C:\!Layi Wkspc\MLV-App',
    [string]$ReceiptsDir,
    [switch]$Apply,
    [string]$GhExe = 'gh'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $ReceiptsDir) { $ReceiptsDir = Join-Path $BoardRoot '.claude-state/coordination/dual-lane/receipts' }
$endpoint = 'repos/SuperDimensionalSpaceFortress/MLV-App/branches/master/protection/required_status_checks'
$contexts = @('Repo Hygiene Python (windows-latest)', 'Repo Hygiene Python (ubuntu-latest)',
              'Batch Compile', 'Windows GUI Pilot', 'Windows Product Oracles')
$legacy = @($contexts | ForEach-Object { if ($_ -ceq 'Batch Compile') { 'Factory Bridge Regressions' } else { $_ } })
$body = @{ strict = $true; checks = @($contexts | ForEach-Object { @{context = $_; app_id = 15368} }) }
$utf8 = [Text.UTF8Encoding]::new($false)
function Refuse([string]$Reason) {
    # Emit one plain, unwrapped stderr line so a machine-facing caller (e.g. a
    # test harness on a narrow hosted CI terminal) can substring-match the
    # exact reason. A `throw` here would instead surface via PowerShell's
    # default terminating-error host view, which adds ANSI color codes and
    # wraps at the console width -- splitting the phrase across lines on
    # narrow/ANSI-enabled hosts (observed on Ubuntu-hosted CI, not Windows).
    # `exit` still unwinds through any pending `finally` (e.g. the transition
    # lock dispose) before the process terminates, so no cleanup is skipped
    # and no statement after the call site ever runs.
    [Console]::Error.WriteLine("set-required-checks refused: $Reason")
    exit 1
}
function Stamp { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture) }
function ObjectJson([string]$Text, [string]$Label) {
    try { $value = ConvertFrom-Json -InputObject $Text -AsHashtable -NoEnumerate }
    catch { Refuse "$Label is malformed JSON" }
    if ($value -isnot [System.Collections.IDictionary]) { Refuse "$Label is not a JSON object" }
    return $value
}
function Receipt([string]$Name) {
    $path = Join-Path $ReceiptsDir $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Refuse "$Name is absent" }
    ObjectJson ([IO.File]::ReadAllText($path)) $Name
}
function Sha([object]$Value, [string]$Label) {
    if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9a-f]{40}$') { Refuse "$Label is not a full commit SHA" }
}
function EqualNames($Actual, $Expected) {
    if ($Actual -isnot [array] -or $Actual.Count -ne $Expected.Count) { return $false }
    if (@($Actual | Where-Object { $_ -isnot [string] }).Count) { return $false }
    return ((@($Actual | Sort-Object -CaseSensitive) -join "`n") -ceq (@($Expected | Sort-Object -CaseSensitive) -join "`n"))
}
function CheckSet($Value, $Expected, [string]$Label) {
    if ($Value -isnot [System.Collections.IDictionary] -or $Value.strict -isnot [bool] -or -not $Value.strict) {
        Refuse "$Label must retain strict=true"
    }
    if ($Value.checks -isnot [array]) { Refuse "$Label checks must be an array" }
    $names = @()
    foreach ($check in $Value.checks) {
        if ($check -isnot [System.Collections.IDictionary] -or
            ($check.app_id -isnot [long] -and $check.app_id -isnot [int]) -or $check.app_id -ne 15368) {
            Refuse "$Label must bind every check to app_id 15368"
        }
        $names += $check.context
    }
    if (-not (EqualNames $names $Expected) -or -not (EqualNames $Value.contexts $Expected)) {
        Refuse "$Label contexts differ from the exact expected set"
    }
}
function Api([string]$Method, [string]$Payload = '') {
    if ($Payload) { $output = $Payload | & $GhExe api $endpoint --method $Method --input - }
    else { $output = & $GhExe api $endpoint --method $Method }
    if ($LASTEXITCODE -ne 0) { Refuse "GitHub $Method failed (exit=$LASTEXITCODE)" }
    ObjectJson ($output -join "`n") "GitHub $Method response"
}
function CreateJson([string]$Path, $Value) {
    $bytes = $utf8.GetBytes((ConvertTo-Json -InputObject $Value -Depth 20) + "`n")
    $file = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $file.Write($bytes); $file.Flush($true) } finally { $file.Dispose() }
}

$base = Receipt '0.4a-workflow-base.json'
$falsifier = Receipt '0.4a-batch-compile-falsifier.json'
$guardrail = Receipt '0.4c-guardrail-move.json'
$guardrailControl = Receipt 'execution-control-0.4c-i.json'
Sha $base.mergeSha 'workflow base mergeSha'
Sha $falsifier.workflowBaseSha 'falsifier workflowBaseSha'
Sha $falsifier.headSha 'falsifier headSha'
Sha $guardrail.headSha 'guardrail headSha'
Sha $guardrailControl.mergeSha 'guardrail control mergeSha'
Sha $guardrailControl.reviewedHeadSha 'guardrail control reviewedHeadSha'
if ($falsifier.workflowBaseSha -cne $base.mergeSha -or $falsifier.headSha -ceq $base.mergeSha -or
    $falsifier.failingContext -cne 'Batch Compile' -or $falsifier.failingStep -cne 'Build MLVApp' -or
    $falsifier.conclusion -cne 'failure') { Refuse 'falsifier does not prove Batch Compile at the pinned workflow base' }
# The hosted guardrail workflow runs against the PR HEAD that Sol independently
# reviewed (reviewedHeadSha), not against the later merge commit (mergeSha) --
# those two are legitimately different SHAs on a valid receipt chain.
if ($guardrail.headSha -cne $guardrailControl.reviewedHeadSha) { Refuse 'guardrail head is not bound to its execution-control-0.4c-i receipt' }
if ($guardrail.conclusion -cne 'success' -or
    -not (EqualNames $guardrail.requiredJobs @('Repo Hygiene Python (windows-latest)')) -or
    ($guardrail.collectedTests -isnot [long] -and $guardrail.collectedTests -isnot [int]) -or
    $guardrail.collectedTests -le 0) { Refuse 'guardrail receipt lacks successful nonempty Windows coverage' }
if (-not $Apply) {
    @{state = 'preview'; endpoint = $endpoint; body = $body; applies = $false} | ConvertTo-Json -Depth 8
    exit 0
}

$control = Receipt 'execution-control-0.4b-i.json'
Sha $control.mergeSha 'actor mergeSha'
Sha $control.reviewedHeadSha 'actor reviewedHeadSha'
$scriptHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($control.hashes.'tools/coordination/set-required-checks.ps1' -cne $scriptHash) { Refuse 'actor hash differs from merged control receipt' }
# The independently reviewed control receipt is produced by the Phase 0 hub, not
# this actor. A missing approval path is never treated as permission to PATCH.
$verdictPath = [string]$control.solVerdictPath
if (-not [IO.Path]::IsPathRooted($verdictPath)) { $verdictPath = Join-Path $BoardRoot $verdictPath }
if (-not (Test-Path -LiteralPath $verdictPath -PathType Leaf)) { Refuse 'actor approval is absent' }
$verdictText = [IO.File]::ReadAllText($verdictPath)
$fences = [regex]::Matches($verdictText, '(?s)```json\s*(.*?)\s*```')
if ($fences.Count) { $verdictText = $fences[$fences.Count - 1].Groups[1].Value }
else {
    $starts = [regex]::Matches($verdictText, '(?m)^\{')
    if (-not $starts.Count) { Refuse 'actor approval has no JSON object' }
    $verdictText = $verdictText.Substring($starts[$starts.Count - 1].Index)
}
$verdict = ObjectJson $verdictText 'actor approval'
if ($verdict.verdict -cne 'APPROVE' -or $verdict.subject_sha -cne $control.reviewedHeadSha) { Refuse 'actor approval is not bound to reviewed head' }
$finalPath = Join-Path $ReceiptsDir '0.4b-required-checks.json'
$intentPath = Join-Path $ReceiptsDir '0.4b-transition-intent.json'
$snapshotPath = Join-Path $ReceiptsDir 'required-checks-live.jsonl'
$lockDir = Join-Path $BoardRoot '.claude-state/coordination/locks'
[IO.Directory]::CreateDirectory($lockDir) | Out-Null
$lock = [IO.File]::Open((Join-Path $lockDir 'required-checks-transition.lock'), [IO.FileMode]::OpenOrCreate,
                       [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    if (Test-Path -LiteralPath $finalPath) { Refuse 'transition receipt already exists; preserve the completed evidence' }
    if (-not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) { Refuse 'required-checks snapshot is absent' }
    $snapshotBytes = [IO.File]::ReadAllBytes($snapshotPath)
    $rows = @($utf8.GetString($snapshotBytes) -split '\r?\n' | Where-Object { $_.Trim() })
    if (-not $rows.Count) { Refuse 'required-checks snapshot is empty' }
    foreach ($row in $rows) { $null = ObjectJson $row 'required-checks snapshot row' }
    $last = ObjectJson $rows[-1] 'last required-checks snapshot row'
    $live = Api 'GET'
    if (Test-Path -LiteralPath $intentPath) {
        $intent = Receipt '0.4b-transition-intent.json'
        if ($intent.headSha -cne $control.mergeSha -or $intent.actorSha256 -cne $scriptHash) { Refuse 'transition intent belongs to a different actor' }
        CheckSet $intent.before $legacy 'saved pre-transition protection'
    } else {
        CheckSet $live $legacy 'live pre-transition protection'
        CheckSet $last $legacy 'last pre-transition snapshot'
        $intent = @{recordedUtc = (Stamp); headSha = $control.mergeSha; actorSha256 = $scriptHash; before = $live}
        CreateJson $intentPath $intent
    }
    if (EqualNames $live.contexts $legacy) {
        CheckSet $live $legacy 'live pre-transition protection'
        $null = Api 'PATCH' (ConvertTo-Json -InputObject $body -Depth 8 -Compress)
    } else {
        # A previous attempt may have PATCHed successfully before losing its GET.
        CheckSet $live $contexts 'recoverable post-transition protection'
    }
    $after = Api 'GET'
    CheckSet $after $contexts 'post-transition protection'
    $stamp = Stamp
    $snapshot = @{recordedUtc = $stamp; strict = $true; checks = $body.checks; contexts = $contexts;
                  source = "gh api $endpoint"; writer = 'set-required-checks.ps1'; headSha = $control.mergeSha}
    $rowText = ConvertTo-Json -InputObject $snapshot -Depth 8 -Compress
    $rowBytes = $utf8.GetBytes($rowText)
    $prefix = if ($snapshotBytes.Length -and $snapshotBytes[-1] -ne 10) { "`n" } else { '' }
    $append = [IO.File]::Open($snapshotPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $append.Write($utf8.GetBytes($prefix + $rowText + "`n")); $append.Flush($true) } finally { $append.Dispose() }
    $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($rowBytes)).ToLowerInvariant()
    CreateJson $finalPath @{recordedUtc = $stamp; headSha = $control.mergeSha; preContexts = $legacy;
                            postContexts = $contexts; snapshotRowSha256 = $hash}
    Write-Output "APPLIED: exact five checks verified; append-only snapshot and completion receipt recorded"
} finally { $lock.Dispose() }
